"""结论句裁判：另一个模型按证据逐句判断报告里的话站不站得住（可点击证据第四期）。

数字、实体、引文是确定性的，系统自己取值、自己核对。可「增长主要来自华东」说得对不对，规则判断不了，
只能交给模型——这是概率性的判断，所以这里每一步都说实话、留余地：

1. 预筛（claim_priority）：只把结论句送去判。挂了依据的排第一，有方向词、因果词的排第二，其余有数字、
   实体这类实质内容的排第三；标题、表格单元格、代码、连接性的话、只有过渡词的、短句不送
2. 证据摘录（prepare）：句子挂的每件证据摘一段。指标写「名称 = 值」加原式；查询给 SQL 前 300 字和被
   引用的行（最多 20 行，数据源遮罩的列不给；宽表先给被引用的列，再补满 12 列）；知识库片段最多 600 字。
   规则里写明数字已经由系统核对过
3. 调用（judge_doc）：一份报告合并成一次调用，超过每批的句数就分批；结构化输出失败退回 JSON 解析，
   再失败就记未裁判。thinking 关掉，max_tokens 4096
4. 预算：每份报告的句数、金额、时长和全局每日金额，每一项都能是 None（不限）。调用前按目录价估算，
   超了按优先级截断；触顶就停，已判的保留，没判的记未裁判并说明是哪个上限——不报错
5. 每日计数落在设置表（SPEND_KEY），按本地日期滚动。目录里没有价格的模型估不出金额，金额上限对它
   不起作用，照实说「按令牌估不出金额」

判定只标注、不改写正文。报告节点在正式运行里调它（结果写进文档、随文档封存）；探索运行按需裁判的
接口也调它（post_seal=True，结果落在封存之后）。所有模型调用都经 get_chat_model，测试换成剧本。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select

from app.core.artifact_store import canonical_json, content_hash
from app.core.artifact_store import load as load_artifact
from app.core.errors import describe_exception
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine.evidence import (
    _DIRECTIONAL,
    MISSING,
    RenderError,
    _auto_entity,
    _clean,
    _mask_names,
    iter_units,
    render_cell,
)
from app.engine.expressions import CellError, column_kind, locate_cell
from app.engine.nodes.llm import _split_structured, _usage_of
from app.engine.state import message_text
from app.providers import catalog as pricing
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 模型给的四种判定；unjudged 是系统记的（没判成），不是模型说的
JUDGED = ("supported", "partial", "unsupported", "not_a_claim")
VERDICTS = (*JUDGED, "unjudged")
#: 模型的理由最多几个字
RATIONALE_MAX = 60
#: 一次调用最多判几句：再多，4096 个输出令牌装不下每句一条判定加理由
BATCH_SIZE = 40
MAX_TOKENS = 4096
#: 证据摘录：SQL 前几个字、每次查询最多几行几列、每段知识库片段最多几个字、整份引用的检索给几段。
#: 列数上限只管没被引用的列：句子引用的格子所在的列一律给（遮罩的除外），再用别的列补满到这么多列
SQL_CHARS = 300
EXCERPT_ROWS = 20
EXCERPT_COLS = 12
CHUNK_CHARS = 600
EXCERPT_HITS = 3
#: 短句：看得见的字（不算标点、空白）少于这么多，又没挂依据、没有方向词的，不送裁判
SHORT_CHARS = 6
#: 估算输出令牌：每句一条判定（理由 60 字上下，加上 JSON 的键）
_OUT_PER_UNIT = 90
_OUT_BASE = 20

#: 未裁判的原因。前四个是上限，报告里写「已到上限」
LIMITS = ("max_claims", "max_cost_usd", "daily_max_usd", "timeout_s")
REASONS = (*LIMITS, "error", "format", "missing", "on_demand")

JUDGE_ROLE = "你是报告核查员：逐句判断报告里的话，能不能被它挂的证据支持。你不写报告，也不改报告。"

JUDGE_RULES = """判定（verdict）四选一：
- supported：证据足以支持这句话的说法——方向、比较、范围、因果都对得上
- partial：一部分说法有证据支持，另一部分（原因、范围、程度）证据里没有
- unsupported：证据不支持、和证据矛盾，或者这句在陈述数据事实却没有任何证据
- not_a_claim：这句不陈述数据事实（过渡、背景、建议、计划），不需要证据
规则：
1. 数字已经由系统核对过，不要复算：每个数都是系统从证据里取出来、按固定规则渲染的，不要因为数字本身或它的格式质疑这句话；你只判断句子的说法有没有证据支持。
2. 只看下面给的证据摘录，不用常识、行业经验或外部知识补证据；摘录里没有的就是没有。
3. rationale 用中文写，不超过 60 个字，说清依据是什么，或者缺了什么。
4. used 写你实际用到的证据编号（摘录里【】中的那个，比如 m:gmv、Q1），没用到就写空数组。
5. 每句都要给判定，unit 照抄句子前面方括号里的编号。"""

#: 报告节点 claims 为 judge 时拼在写作规则后面：写作者要知道还有一道按证据的核对
JUDGE_RULE = ("这份报告的结论句还会由另一个模型逐句核对：它只看你挂的依据，判断这句话站不站得住。"
              "说法不要超出证据——证据里没有的原因、范围、趋势，不要写成结论。")

JUDGE_SCHEMA: dict[str, Any] = {
    # 走工具调用的结构化输出要求函数名只含字母、数字、下划线
    "title": "claim_verdicts",
    "description": "逐句判断报告里的话能不能被它挂的证据支持",
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "unit": {"type": "string", "description": "句子编号，照抄 [u3] 里的 u3"},
            "verdict": {"type": "string", "enum": list(JUDGED)},
            "rationale": {"type": "string", "description": f"判定的理由，中文，不超过 {RATIONALE_MAX} 个字"},
            "used": {"type": "array", "items": {"type": "string"}, "description": "实际用到的证据编号"},
        },
        "required": ["unit", "verdict", "rationale", "used"],
    }}},
    "required": ["items"],
}

#: 只有过渡作用的说法。一句话去掉它们和标点之后什么都不剩，就是连接性的话
_TRANSITIONS = re.compile(
    r"综上所述|综上|总的来说|总体来看|总体而言|整体来看|整体而言|具体来看|具体如下|具体而言|如下所示|"
    r"详见下表|见下表|主要原因如下|原因如下|原因有以下几点|情况如下|结论如下|分析如下|"
    r"下面(?:来)?(?:看看|看|是|分析|说明|展开|列出)?|接下来(?:看)?|首先|其次|再次|最后|另外|此外|同时|"
    r"因此|所以|总之|如下|以下|(?<![A-Za-z])(?:in summary|as follows|overall|next)(?![A-Za-z])",
    re.I)
_INVISIBLE = re.compile(r"[\W_]+")


# --------------------------------------------------------------------------
# 预筛
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """一句要送裁判的结论。"""

    unit: str
    priority: int                           # 1 挂了依据 / 2 方向词、因果词 / 3 其余有实质内容的
    order: int                              # 在文档里的先后
    text: str                               # 句子（渲染后的正文，压成一行）
    section: str                            # 所在小节的标题，没有就是空串
    aliases: tuple[str, ...]                # 这句挂的依据（目录键）
    rows: tuple[tuple[str, int], ...] = ()  # 点名引用的查询行：(Q1, 3)
    cols: tuple[tuple[str, str], ...] = ()  # 行内引用的格子所在的列：(Q1, region)
    whole: tuple[str, ...] = ()             # 整份引用的查询、检索：Q1、K1
    hits: tuple[tuple[str, int], ...] = ()  # 引文所在的检索片段：(K1, 0)


def claim_priority(unit: dict[str, Any], block: dict[str, Any], markdown: str) -> int | None:
    """这句结论送不送裁判、排第几：1 / 2 / 3，不送是 None。只看文档本身，同一份文档永远判得一样。

    - 不是结论句（连接性的话、标题、代码）、或者在表格里（一格值不是一句话）：不送
    - 写作者挂了依据（行内引用或 [[see:]]，自动链接的名字不算）：1
    - 去掉过渡词和标点什么都不剩（「原因如下：」）：不送
    - 有方向词、因果词：2
    - 有数字、值、实体、引文，看得见的字又不少于 SHORT_CHARS 个：3
    - 其余（短句）：不送
    """
    if unit.get("kind") != "claim" or block.get("type") in ("table", "code", "heading"):
        return None
    if unit.get("cites"):
        return 1
    segments = unit.get("segments") or []
    words = "".join(str(s.get("text") or "") for s in segments if s.get("kind") == "text" or _auto_entity(s))
    body = _INVISIBLE.sub("", _TRANSITIONS.sub("", words))
    substance = [s for s in segments if s.get("kind") in ("number", "value", "entity", "quote") and not _auto_entity(s)]
    shown = body + "".join(_INVISIBLE.sub("", str(s.get("text") or "")) for s in substance)
    if not shown:
        return None
    if _DIRECTIONAL.search(body):
        return 2
    if (substance or unit.get("see")) and len(shown) >= SHORT_CHARS:
        return 3
    return None


def candidates(doc: dict[str, Any], units: Iterable[str] | None = None) -> list[Candidate]:
    """送裁判的句子，按优先级、再按文档里的先后排好。

    units 给了就是有人点名要判这几句（按需裁判）：预筛放掉的结论句照判（按最低优先级），不是结论句的
    （标题、连接性的话、代码、表格单元格）和不存在的编号不判。
    """
    wanted = None if units is None else {str(u) for u in units}
    markdown = str(doc.get("markdown") or "")
    out: list[Candidate] = []
    section = ""
    for order, (block, unit) in enumerate(iter_units(doc)):
        text = _unit_text(unit, markdown)
        if block.get("type") == "heading":
            section = text
        priority = claim_priority(unit, block, markdown)
        if wanted is not None:
            if unit.get("id") not in wanted or unit.get("kind") != "claim" or block.get("type") in ("table", "code"):
                continue
            priority = priority or 3
        if priority is None or not text:
            continue
        out.append(_candidate(unit, priority, order, text, section))
    return sorted(out, key=lambda c: (c.priority, c.order))


def _unit_text(unit: dict[str, Any], markdown: str) -> str:
    span = unit.get("span")
    if not (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) for x in span)):
        return ""
    return _clean(markdown[span[0]:span[1]])


def unit_texts(doc: dict[str, Any]) -> dict[str, str]:
    """{unit id: 这句的正文（压成一行）}，按文档里的先后；没有正文的不列。改写前后按句子认编号时用。"""
    markdown = str(doc.get("markdown") or "")
    out: dict[str, str] = {}
    for _, unit in iter_units(doc):
        if text := _unit_text(unit, markdown):
            out[str(unit.get("id"))] = text
    return out


def _candidate(unit: dict[str, Any], priority: int, order: int, text: str, section: str) -> Candidate:
    rows: list[tuple[str, int]] = []
    cols: list[tuple[str, str]] = []
    whole: list[str] = []
    hits: list[tuple[str, int]] = []
    cites = [s["cite"] for s in unit.get("segments") or [] if isinstance(s.get("cite"), dict) and not s.get("auto")]
    for cite in [*cites, *(unit.get("see") or [])]:
        if not isinstance(cite, dict) or cite.get("status") != "resolved" or not cite.get("alias"):
            continue
        alias, kind, locator = str(cite["alias"]), cite.get("kind"), cite.get("locator") or {}
        if kind == "cell" or (kind == "query" and isinstance(locator.get("row"), int)):
            rows.append((alias, int(locator["row"])))
            if kind == "cell" and locator.get("column") not in (None, ""):
                cols.append((alias, str(locator["column"])))
        elif kind in ("query", "retrieval"):
            whole.append(alias)
        elif kind == "quote" and isinstance(locator.get("hit"), int):
            hits.append((alias, int(locator["hit"])))
    return Candidate(unit=str(unit.get("id")), priority=priority, order=order, text=text, section=section,
                     aliases=tuple(dict.fromkeys(str(a) for a in unit.get("cites") or [])),
                     rows=tuple(dict.fromkeys(rows)), cols=tuple(dict.fromkeys(cols)),
                     whole=tuple(dict.fromkeys(whole)),
                     hits=tuple(dict.fromkeys(hits)))


# --------------------------------------------------------------------------
# 证据摘录与请求
# --------------------------------------------------------------------------


class _Fetch:
    """这一次请求里取过的工件（取回时复验哈希；取不到、被改过的一律当没有）。"""

    def __init__(self, loader: Callable[[str], Any] | None) -> None:
        self.loader = loader or load_artifact
        self.memo: dict[str, Any] = {}

    def __call__(self, artifact: Any) -> Any:
        key = str(artifact or "")
        if key not in self.memo:
            try:
                self.memo[key] = self.loader(key) if key else None
            except Exception:  # noqa: BLE001 - 取不回来、哈希不符：这段摘录不给，裁判照实看不到
                self.memo[key] = None
        return self.memo[key]


def _hidden(snapshot: dict[str, Any], source: Any, masked: dict[str, Any] | None) -> set[str]:
    """要遮住的列（小写）：查询当时记下的，加上数据源现在设的。"""
    names = _mask_names(snapshot.get("mask_columns"))
    for name, cols in (masked or {}).items():
        if str(name) == str(source or ""):
            names |= _mask_names(cols)
    return names


def _shown_columns(columns: list[str], cited: Iterable[str]) -> list[str]:
    """摘录给哪几列：被引用的列一律给（超过上限也给），再按原来的顺序用别的列补满到 EXCERPT_COLS 列。

    columns 已经去掉了遮罩的列：遮罩的列被引用了也不给。列名不分大小写。
    """
    wanted = {str(c).lower() for c in cited}
    first = [c for c in columns if c.lower() in wanted]
    rest = [c for c in columns if c.lower() not in wanted]
    return first + rest[:max(0, EXCERPT_COLS - len(first))]


def _query_excerpt(alias: str, entry: dict[str, Any], rows: set[int], cols: Iterable[str], whole: bool,
                   fetch: _Fetch, masked: dict[str, Any] | None) -> str | None:
    snapshot = fetch(entry.get("artifact"))
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("rows"), list):
        return None
    total = len(snapshot["rows"])
    # 旧形状的台账条目没记数据源：按快照自己记的找遮罩（证据面板同一个口径）
    source = entry.get("source") or snapshot.get("source")
    head = f"【{alias}】查询结果（{entry.get('tool') or '查询'} · 数据源 {source or '—'} · 共 {total} 行）"
    sql = _clean(str(snapshot.get("sql") or ""))
    lines = [head, f"SQL：{sql[:SQL_CHARS]}{'…' if len(sql) > SQL_CHARS else ''}"]
    hidden = _hidden(snapshot, source, masked)
    columns = [str(c) for c in snapshot.get("columns") or [] if str(c).lower() not in hidden]
    shown_cols = _shown_columns(columns, cols)
    # 点名引用的行先给，整份引用的再从头补满，一共不超过 EXCERPT_ROWS 行
    picked = sorted(r for r in rows if 0 <= r < total)[:EXCERPT_ROWS]
    if whole:
        picked += [r for r in range(total) if r not in picked][:EXCERPT_ROWS - len(picked)]
    omitted = len(columns) - len(shown_cols)
    lines.append("列：" + " | ".join(shown_cols) + (f" | …另有 {omitted} 列" if omitted > 0 else ""))
    for r in sorted(picked):
        cells = []
        for c in shown_cols:
            try:
                raw, _ = locate_cell(snapshot, r, c)
                cells.append(render_cell(raw, kind=column_kind(snapshot, c)))
            except (CellError, RenderError):
                cells.append(MISSING)
        lines.append(f"r{r}：" + " | ".join(cells))
    if hidden & {str(c).lower() for c in snapshot.get("columns") or []}:
        lines.append("（有的列按数据源的设置遮罩了，没有给出）")
    return "\n".join(lines)


def _metric_excerpt(alias: str, entry: dict[str, Any], fetch: _Fetch) -> str:
    name = entry.get("name") or (entry.get("locator") or {}).get("metric") or alias
    rendered = entry.get("rendered")
    where = " ".join(v for v in (f"口径「{entry['caliber']}」" if entry.get("caliber") else "",
                                str(entry.get("version") or "")) if v)
    value = f"{name}：这次没有值" if entry.get("value") is None or rendered in (None, MISSING) \
        else f"{name} = {rendered}"
    lines = [f"【{alias}】口径卡指标：{value}" + (f"（{where}）" if where else "")]
    card = fetch(entry.get("artifact"))
    wanted = (entry.get("locator") or {}).get("metric")
    for metric in (card.get("metrics") if isinstance(card, dict) else None) or []:
        if isinstance(metric, dict) and metric.get("id") == wanted and metric.get("expression"):
            lines.append(f"原式：{_clean(str(metric['expression']))}")
            break
    return "\n".join(lines)


def _retrieval_excerpt(alias: str, entry: dict[str, Any], picked: list[int], whole: bool, fetch: _Fetch) -> str | None:
    content = fetch(entry.get("artifact"))
    hits = content.get("hits") if isinstance(content, dict) else None
    if not isinstance(hits, list):
        return None
    chosen = [h for h in dict.fromkeys(picked) if 0 <= h < len(hits)]
    if whole:
        chosen += [h for h in range(len(hits)) if h not in chosen][:max(0, EXCERPT_HITS - len(chosen))]
    lines = [f"【{alias}】知识库检索（{entry.get('source') or '—'} · 共 {len(hits)} 段）"]
    for h in sorted(chosen):
        hit = hits[h]
        if not isinstance(hit, dict) or not isinstance(hit.get("content"), str):
            continue
        body = _clean(hit["content"])
        title = f"《{_clean(str(hit['title']))}》" if hit.get("title") else ""
        lines.append(f"片段 {h + 1}{title}：{body[:CHUNK_CHARS]}{'…' if len(body) > CHUNK_CHARS else ''}")
    return "\n".join(lines)


def _excerpt(alias: str, entry: dict[str, Any], cands: list[Candidate], fetch: _Fetch,
             masked: dict[str, Any] | None) -> str | None:
    kind = entry.get("kind")
    if kind == "metric":
        return _metric_excerpt(alias, entry, fetch)
    if kind == "input":
        field_name = (entry.get("locator") or {}).get("field") or alias
        return f"【{alias}】运行输入：{field_name} = {entry.get('rendered')}"
    if kind == "query":
        rows = {r for c in cands for a, r in c.rows if a == alias}
        cols = [col for c in cands for a, col in c.cols if a == alias]
        return _query_excerpt(alias, entry, rows, cols, any(alias in c.whole for c in cands), fetch, masked)
    if kind == "retrieval":
        picked = [h for c in cands for a, h in c.hits if a == alias]
        return _retrieval_excerpt(alias, entry, picked, any(alias in c.whole for c in cands), fetch)
    if kind == "table":
        return f"【{alias}】表 {entry.get('name')}" + (f"（数据源 {entry['source']}）" if entry.get("source") else "")
    if kind == "column":
        table = (entry.get("locator") or {}).get("table")
        return f"【{alias}】字段 {f'{table}.' if table else ''}{(entry.get('locator') or {}).get('column') or entry.get('name')}"
    return None


def _tokens(text: str) -> int:
    """估算令牌数：中日韩的字一个字算一个，其余大约 3.5 个字符一个。宁可估多，不估少。"""
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return wide + math.ceil((len(text) - wide) / 3.5)


@dataclass
class JudgeRequest:
    """一次裁判的全部输入：候选句、每件证据的摘录、预筛放掉的结论句、点名了却不能判的句子。"""

    cands: list[Candidate]
    excerpts: dict[str, str]
    screened: list[str]
    skipped: dict[str, str]

    def unit_line(self, c: Candidate) -> str:
        cited = "、".join(a for a in c.aliases if a in self.excerpts) or "无"
        where = f" ｜ 小节：{c.section}" if c.section else ""
        return f"[{c.unit}] {c.text}\n    依据：{cited}{where}"

    def messages(self, units: list[Candidate]) -> list[BaseMessage]:
        aliases = list(dict.fromkeys(a for c in units for a in c.aliases if a in self.excerpts))
        evidence = "\n\n".join(self.excerpts[a] for a in aliases) or "（这些句子都没有挂依据）"
        ordered = sorted(units, key=lambda c: c.order)
        return [
            SystemMessage(content=f"{JUDGE_ROLE}\n\n{JUDGE_RULES}"),
            HumanMessage(content=(
                f"证据摘录（【】里是编号，判定时 used 写它）：\n{evidence}\n\n"
                "要判断的句子（每句都要给判定）：\n" + "\n".join(self.unit_line(c) for c in ordered) + "\n\n"
                '只输出 JSON：{"items": [{"unit": "u1", "verdict": "supported", "rationale": "…", "used": ["m:gmv"]}]}'
            )),
        ]

    def cost(self, model_id: str, units: list[Candidate]) -> float | None:
        """这几句合成一次调用，按目录价估多少钱；目录里没有这个模型的价格时是 None（估不出金额）。"""
        if pricing.find_pricing(model_id) is None:
            return None
        prompt = "\n".join(str(m.content) for m in self.messages(units))
        out = min(MAX_TOKENS, _OUT_BASE + _OUT_PER_UNIT * len(units))
        return pricing.estimate_cost(model_id, _tokens(prompt), out)

    def unit_key(self, c: Candidate, model_id: str) -> str:
        """同一个模型、同一句话、同样的证据摘录：判定可以直接复用（改写之后没动的句子不再花钱判）。"""
        evidence = [self.excerpts.get(a) for a in c.aliases]
        return content_hash(canonical_json({"model": model_id, "text": c.text, "section": c.section,
                                            "evidence": evidence}))[:16]


def prepare(doc: dict[str, Any], catalog: dict[str, Any] | None = None, *, units: Iterable[str] | None = None,
            loader: Callable[[str], Any] | None = None, masked: dict[str, Any] | None = None) -> JudgeRequest:
    """裁判的输入：候选句和证据摘录。纯函数（快照经 loader 取，缺省读工件库），不调模型。

    catalog 缺省用文档自己存的目录；masked = {数据源名: 要遮的列}，和快照里记下的 mask_columns 合在一起。
    """
    catalog = catalog if catalog is not None else (doc.get("catalog") or {})
    cands = candidates(doc, units)
    fetch = _Fetch(loader)
    excerpts: dict[str, str] = {}
    for alias in dict.fromkeys(a for c in cands for a in c.aliases):
        entry = catalog.get(alias)
        if isinstance(entry, dict) and (text := _excerpt(alias, entry, cands, fetch, masked)):
            excerpts[alias] = text
    picked = {c.unit for c in cands}
    skipped: dict[str, str] = {}
    screened: list[str] = []
    ids = set()
    for _, unit in iter_units(doc):
        ids.add(unit.get("id"))
        if units is None and unit.get("kind") == "claim" and unit.get("id") not in picked:
            screened.append(str(unit.get("id")))
    if units is not None:
        for uid in dict.fromkeys(str(u) for u in units):
            if uid not in picked:
                skipped[uid] = "not_a_claim" if uid in ids else "not_found"
    return JudgeRequest(cands=cands, excerpts=excerpts, screened=screened, skipped=skipped)


def request_key(request: JudgeRequest, spec: ModelSpec, budget: Budget, known: dict[str, Any] | None = None) -> str:
    """一次裁判的输入指纹：报告节点拿它认 checkpoint 里缓存的判定是不是这一稿的。"""
    return content_hash(canonical_json({
        "units": [[c.unit, c.priority, c.text, c.section, list(c.aliases)] for c in request.cands],
        "excerpts": request.excerpts, "model": [spec.provider, spec.model], "budget": budget.as_dict(),
        "known": sorted(known or {}),
    }))


# --------------------------------------------------------------------------
# 预算与设置
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """一次裁判的上限。每一项都可以是 None：不限。"""

    max_claims: int | None = 40
    max_cost_usd: float | None = 0.05
    timeout_s: float | None = 30
    daily_max_usd: float | None = 2.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: 设置里 judge 这一组（DEFAULT_SETTINGS 引用这一份）。上限写 null 表示不限
SETTING_GROUP = "judge"
JUDGE_DEFAULTS: dict[str, Any] = {
    "provider": None,             # 证据裁判模型：空着依次用 Copilot 模型、默认 provider
    "model": None,
    "report_max_claims": 40,      # 每份报告最多判几句
    "report_max_cost_usd": 0.05,  # 每份报告的裁判金额上限
    "report_timeout_s": 30,       # 每份报告的裁判时长上限（每次调用的超时就是剩下的时长）
    "click_max_cost_usd": 0.01,   # 探索运行里每点一次「请模型判断」的金额上限
    "daily_max_usd": 2.0,         # 全局每日裁判金额上限（按本地日期）
}
#: 每日计数存在设置表的这一行：{date: 本地日期, usd, calls, unpriced_calls}。不从设置接口进出
SPEND_KEY = "judge_spend"
_COUNTS = frozenset({"report_max_claims"})
_SECONDS = frozenset({"report_timeout_s"})


def _limit_ok(key: str, value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return False
    return not (key in _COUNTS and int(value) != value)


def _limit_words(key: str) -> str:
    if key in _COUNTS:
        return "正整数（句数）"
    if key in _SECONDS:
        return "大于 0 的秒数"
    return "大于 0 的金额（美元）"


def settings_problem(value: Any) -> str | None:
    """设置页写进来的 judge 这一组有什么问题，没问题返回 None。"""
    if not isinstance(value, dict):
        return f"裁判设置（judge）要写成一个对象，写的是 {value!r}"
    for key, item in value.items():
        if key not in JUDGE_DEFAULTS:
            return f"裁判设置里不认识「{key}」：能写的是 {'、'.join(JUDGE_DEFAULTS)}"
        if key in ("provider", "model"):
            if item is not None and not isinstance(item, str):
                return f"{key} 要写模型接入的名字 / 模型 id（字符串），写 null 表示不指定，写的是 {item!r}"
        elif not _limit_ok(key, item):
            return f"{key} 要写{_limit_words(key)}，写 null 表示不限，写的是 {item!r}"
    return None


def sanitize_settings(stored: Any) -> dict[str, Any]:
    """库里存的 judge 一组 → 补齐默认的完整设置。存坏了的项（旧版本、手改）按默认，存了 null 的上限就是不限。"""
    stored = stored if isinstance(stored, dict) else {}
    out = dict(JUDGE_DEFAULTS)
    for key in JUDGE_DEFAULTS:
        if key not in stored:
            continue
        value = stored[key]
        if key in ("provider", "model"):
            out[key] = value.strip() or None if isinstance(value, str) else None
        elif _limit_ok(key, value):
            out[key] = value
    return out


async def judge_settings(session: Any) -> dict[str, Any]:
    """设置里的 judge 一组，没存过的键补默认（读法同 run_defaults）。"""
    row = await session.get(Setting, SETTING_GROUP)
    return sanitize_settings(row.value if row else None)


def _pick(node: dict[str, Any], key: str, settings: dict[str, Any], setting_key: str) -> Any:
    # 节点上写了（包括写 null：不限）就按节点，没写才取设置
    return node[key] if key in node else settings.get(setting_key)


def report_budget(settings: dict[str, Any], node_judge: dict[str, Any] | None = None) -> Budget:
    """一份报告的裁判上限：节点 judge 里写了的键盖过设置；每日上限只在设置里。"""
    node = node_judge if isinstance(node_judge, dict) else {}
    return Budget(max_claims=_pick(node, "max_claims", settings, "report_max_claims"),
                  max_cost_usd=_pick(node, "max_cost_usd", settings, "report_max_cost_usd"),
                  timeout_s=_pick(node, "timeout_s", settings, "report_timeout_s"),
                  daily_max_usd=settings.get("daily_max_usd"))


def click_budget(settings: dict[str, Any], node_judge: dict[str, Any] | None = None) -> Budget:
    """探索运行里点一次「请模型判断」的上限：金额按每次点击的上限，句数、时长按每份报告的。"""
    node = node_judge if isinstance(node_judge, dict) else {}
    return Budget(max_claims=_pick(node, "max_claims", settings, "report_max_claims"),
                  max_cost_usd=settings.get("click_max_cost_usd"),
                  timeout_s=_pick(node, "timeout_s", settings, "report_timeout_s"),
                  daily_max_usd=settings.get("daily_max_usd"))


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


async def judge_model_spec(session: Any, node_judge: dict[str, Any] | None = None, *,
                           settings: dict[str, Any] | None = None) -> ModelSpec:
    """裁判用哪个模型：节点上的 judge.model（连同 provider）> 设置里的证据裁判模型 > Copilot 模型 >
    默认 provider。哪一级写了就整组用哪一级，不跨级拼 provider 和模型。thinking 关、max_tokens 4096。

    建议和写作模型不同，否则等于自己审自己（报告节点发现相同会发 judge_same_model 警告）。
    """
    from app.api.copilot import COPILOT_SETTING_KEY

    node = node_judge if isinstance(node_judge, dict) else {}
    provider, model = _text(node.get("provider")), _text(node.get("model"))
    if not (provider or model):
        chosen = settings if settings is not None else await judge_settings(session)
        provider, model = _text(chosen.get("provider")), _text(chosen.get("model"))
    if not (provider or model):
        row = await session.get(Setting, COPILOT_SETTING_KEY)
        saved = row.value if row is not None and isinstance(row.value, dict) else {}
        provider, model = _text(saved.get("provider")), _text(saved.get("model"))
    return ModelSpec(provider=provider, model=model, max_tokens=MAX_TOKENS, thinking="off")


async def source_masks(catalog: dict[str, Any], *, loader: Callable[[str], Any] | None = None
                       ) -> dict[str, list[str]]:
    """目录里的查询来自的数据源现在设的遮罩：{数据源名: 列名}。和证据面板同一个口径。

    旧形状的台账条目没记数据源的，按快照自己记的数据源查（快照经 loader 取，缺省读工件库；
    按需裁判的接口要传只认封存工件的 loader，和 prepare 同一个）。
    """
    from app.data.engine import masked_columns
    from app.db.models import DataSource

    fetch = _Fetch(loader)
    found: set[str] = set()
    for entry in catalog.values():
        if not isinstance(entry, dict) or entry.get("kind") != "query":
            continue
        source = entry.get("source")
        if not source:
            snapshot = fetch(entry.get("artifact"))
            source = snapshot.get("source") if isinstance(snapshot, dict) else None
        if source:
            found.add(str(source))
    names = sorted(found)
    if not names:
        return {}
    async with SessionLocal() as session:
        rows = (await session.execute(select(DataSource.name, DataSource.options)
                                      .where(DataSource.name.in_(names)))).all()
    return {str(name): cols for name, options in rows if (cols := masked_columns(options))}


# --------------------------------------------------------------------------
# 每日计数
# --------------------------------------------------------------------------

_spend_lock = asyncio.Lock()


def _today() -> str:
    """本地日期：每日上限按它滚动。"""
    return datetime.now().astimezone().date().isoformat()


def _spend_of(row: Any) -> dict[str, Any]:
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    today = _today()
    if value.get("date") != today:
        return {"date": today, "usd": 0.0, "calls": 0, "unpriced_calls": 0}
    return {"date": today, "usd": float(value.get("usd") or 0.0), "calls": int(value.get("calls") or 0),
            "unpriced_calls": int(value.get("unpriced_calls") or 0)}


async def daily_spend() -> dict[str, Any]:
    """今天（本地日期）裁判已经花了多少：{date, usd, calls, unpriced_calls}。估不出金额的调用只计次数。"""
    async with SessionLocal() as session:
        return _spend_of(await session.get(Setting, SPEND_KEY))


async def _charge(amount: float, *, limit: float | None, priced: bool) -> str | None:
    """记一次调用、先按估算预留金额。加上这笔会超过每日上限就不记，返回 None；记下了返回记在哪天。"""
    async with _spend_lock:
        async with SessionLocal() as session:
            row = await session.get(Setting, SPEND_KEY)
            spend = _spend_of(row)
            if limit is not None and spend["usd"] + amount > limit + 1e-12:
                return None
            spend["usd"] = round(spend["usd"] + amount, 8)
            spend["calls"] += 1
            spend["unpriced_calls"] += 0 if priced else 1
            if row is None:
                session.add(Setting(key=SPEND_KEY, value=spend))
            else:
                row.value = spend
            await session.commit()
            return spend["date"]


async def _settle(date: str, delta: float) -> None:
    """调用结束后按实际用量找补预留的差额。跨了天就不找补：预留记在前一天，今天从零算。"""
    if not delta:
        return
    async with _spend_lock:
        async with SessionLocal() as session:
            row = await session.get(Setting, SPEND_KEY)
            spend = _spend_of(row)
            if row is None or spend["date"] != date:
                return
            spend["usd"] = max(0.0, round(spend["usd"] + delta, 8))
            row.value = spend
            await session.commit()


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------

Emit = Callable[..., None]


def _quiet(*_args: Any, **_data: Any) -> None:
    return None


def _json_in(text: str) -> Any:
    from app.api.copilot import _extract_json

    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return _extract_json(text)
    except ValueError:
        return None


def _items_of(value: Any) -> list[Any] | None:
    """判定清单：{"items": [...]}，或者直接一个数组。有的模型走工具调用时把嵌套的数组写成一段 JSON 文本
    （{"items": "[...]"}），也有把整个对象写进正文的：都认。"""
    if isinstance(value, str):
        value = _json_in(value)
    if isinstance(value, dict) and isinstance(value.get("items"), str):
        value = {**value, "items": _json_in(value["items"])}
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return value["items"]
    return value if isinstance(value, list) else None


async def _ask(model: Any, messages: list[BaseMessage]) -> tuple[list[Any] | None, list[BaseMessage], str | None,
                                                                  int]:
    """问一次裁判：(判定清单, 模型的原始回复, 没成的原因, 调了几次模型)。

    先走结构化输出；不成（模型不支持、给的不是合法结构）先从原始回复里找 JSON，再退回纯文本调用、
    从回复里抠 JSON。原因以 error: 开头的是调用本身失败，format 是格式不对。超时由调用方掐。
    """
    raws: list[BaseMessage] = []
    try:
        got = await model.with_structured_output(JUDGE_SCHEMA, include_raw=True).ainvoke(messages)
        raw, parsed = _split_structured(got)
        if raw is not None:
            raws.append(raw)
        if (items := _items_of(parsed)) is not None:
            return items, raws, None, 1
        if raw is not None:
            # 有的网关把 JSON 写在正文里、或者塞进了一个没按 schema 解析出来的工具调用
            for found in [message_text(raw), *(c.get("args") for c in getattr(raw, "tool_calls", None) or [])]:
                if (items := _items_of(found)) is not None:
                    return items, raws, None, 1
    except Exception:  # noqa: BLE001 - 不支持结构化输出的模型退回文本解析
        pass
    try:
        reply = await model.ainvoke(messages)
    except Exception as e:  # noqa: BLE001
        return None, raws, f"error:{describe_exception(e)}", 2
    raws.append(reply)
    items = _items_of(message_text(reply))
    return items, raws, None if items is not None else "format", 2


def _unjudged(reason: str, rationale: str, *, judge: str | None, post_seal: bool) -> dict[str, Any]:
    return {"status": "unjudged", "rationale": rationale, "judge": judge, "post_seal": post_seal, "reason": reason}


def _limit_label(reason: str, budget: Budget, *, click: bool = False) -> str:
    if reason == "max_claims":
        return f"每{'次' if click else '份报告'}最多判 {budget.max_claims} 句"
    if reason == "max_cost_usd":
        return f"{'这次点击' if click else '这份报告'}的裁判金额上限 ${budget.max_cost_usd:g}"
    if reason == "daily_max_usd":
        return f"全局每日裁判金额上限 ${budget.daily_max_usd:g}"
    return f"裁判时长上限 {budget.timeout_s:g} 秒"


def _why_unjudged(reason: str, budget: Budget, detail: str = "", *, click: bool = False) -> str:
    if reason in LIMITS:
        return f"已到上限（{_limit_label(reason, budget, click=click)}），这句没判"
    if reason == "error":
        return f"裁判没跑成：{detail}"[:120]
    if reason == "format":
        return detail or "裁判给的结果格式不对，这句没判"
    if reason == "missing":
        return "裁判没有给这句判定"
    return "探索运行按需裁判：点开这句、请模型判断时才判"


async def run_request(request: JudgeRequest, *, spec: ModelSpec, budget: Budget, known: dict[str, Any] | None = None,
                      post_seal: bool = False, emit: Emit | None = None, click: bool = False,
                      shown: Budget | None = None) -> dict[str, Any]:
    """按预算判一份请求。永远不抛：模型没配好、调用失败、格式不对、触顶，都落成未裁判和一条缺口。

    返回 outcome：{model, priced, verdicts:{unit: verdict}, keys:{unit: 复用键}, candidates, screened,
    skipped, counts, unjudged:{原因: 句数}, limits_hit, cost_usd, estimated_usd, calls, reused, duration_ms,
    usage, budget, gaps, notes}。click=True 时上限的说法按「这次点击」写。

    shown 只管说给人看的上限：同一份报告分几轮判时（rewrite_once 的第二轮），budget 是这一轮剩下的钱和
    时间，未裁判的理由、缺口、judge_limit 事件里写的却该是配置的上限（「$0.05」，不是剩下的零头）。
    """
    emit = emit or _quiet
    label = shown or budget
    started = time.monotonic()
    spec = spec.model_copy(update={"thinking": "off", "max_tokens": MAX_TOKENS,
                                   "timeout": math.ceil(budget.timeout_s) if budget.timeout_s else spec.timeout})
    verdicts: dict[str, dict[str, Any]] = {}
    details: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "calls": 0}
    notes: list[str] = []
    estimated = 0.0
    reused = 0
    model = None
    model_id = spec.model or ""
    try:
        async with SessionLocal() as session:
            model, model_id = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 裁判模型没配好：这回判不了，报告照常
        why = str(e) if isinstance(e, ProviderNotConfigured) else describe_exception(e)
        details["error"] = f"裁判模型用不了（{why}）"
    judge = model_id or None
    priced = model is not None and pricing.find_pricing(model_id) is not None
    keys = {c.unit: request.unit_key(c, model_id) for c in request.cands}

    def stop(units: Iterable[Candidate], reason: str, detail: str = "") -> None:
        for c in units:
            verdicts.setdefault(c.unit, _unjudged(reason, _why_unjudged(reason, label, detail, click=click),
                                                  judge=judge, post_seal=post_seal))

    ranked = list(request.cands)
    if budget.max_claims is not None:
        stop(ranked[budget.max_claims:], "max_claims")
        ranked = ranked[:budget.max_claims]
    pending: list[Candidate] = []
    for c in ranked:
        hit = (known or {}).get(keys[c.unit])
        if isinstance(hit, dict) and hit.get("status") in JUDGED:
            verdicts[c.unit] = dict(hit)
            reused += 1
        else:
            pending.append(c)

    if model is None:
        stop(pending, "error", details["error"])
        pending = []
    elif pending and not priced and (budget.max_cost_usd is not None or budget.daily_max_usd is not None):
        note = (f"模型「{model_id}」不在价格目录里，按令牌估不出金额：金额上限（每份报告、每次点击、每日）对它不起作用，"
                "费用只受句数、时长上限约束")
        notes.append(note)
        emit(EventType.LOG, level="warn", code="judge_unpriced", message=note)

    spent = 0.0
    while pending:
        remaining = None if budget.timeout_s is None else budget.timeout_s - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            stop(pending, "timeout_s")
            break
        batch = pending[:BATCH_SIZE]
        cost = request.cost(model_id, batch) if priced else None
        if cost is not None:
            caps = []
            if budget.max_cost_usd is not None:
                caps.append((budget.max_cost_usd - spent, "max_cost_usd"))
            if budget.daily_max_usd is not None:
                caps.append((budget.daily_max_usd - (await daily_spend())["usd"], "daily_max_usd"))
            if caps:
                cap, binding = min(caps, key=lambda x: x[0])
                trimmed = False
                while batch and (cost := request.cost(model_id, batch) or 0.0) > cap + 1e-12:
                    batch, trimmed = batch[:-1], True       # 按优先级从后往前丢：先判挂了依据的
                if not batch:
                    stop(pending, binding)
                    break
                if trimmed:
                    # 这一批已经是剩下的钱装得下的最多句子，后面的句子更装不下
                    stop(pending[len(batch):], binding)
                    pending = pending[:len(batch)]
        pending = pending[len(batch):]
        charged = cost or 0.0
        day = await _charge(charged, limit=budget.daily_max_usd if priced else None, priced=priced)
        if day is None:          # 估算之后、记账之前别的裁判先花掉了今天剩下的钱
            stop([*batch, *pending], "daily_max_usd")
            break
        estimated += charged
        began = time.perf_counter()
        emit(EventType.LLM_START, model=model_id, structured=True, purpose="judge", message_count=2,
             units=len(batch))
        items: list[Any] | None = None
        raws: list[BaseMessage] = []
        problem: str | None = None
        calls = 1
        try:
            async with asyncio.timeout(remaining):
                items, raws, problem, calls = await _ask(model, request.messages(batch))
        except TimeoutError:
            problem = "timeout_s"
        tokens = [_usage_of(r, model_id) for r in raws]
        inp, out = sum(t["input_tokens"] for t in tokens), sum(t["output_tokens"] for t in tokens)
        # 回复没带用量（有的网关不给、调用超时）就按估算记：宁可多记，不能让每日上限漏掉
        actual = (pricing.estimate_cost(model_id, inp, out) if inp or out else charged) if priced else 0.0
        await _settle(day, actual - charged)
        spent += actual
        usage["input_tokens"] += inp
        usage["output_tokens"] += out
        usage["calls"] += calls
        usage["cost_usd"] += actual
        end = {"model": model_id, "purpose": "judge", "duration_ms": int((time.perf_counter() - began) * 1000),
               "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out, "cost_usd": round(actual, 6),
               "calls": calls, "units": len(batch)}
        if problem is not None and problem != "format":
            reason = "timeout_s" if problem == "timeout_s" else "error"
            detail = problem.partition(":")[2]
            emit(EventType.LLM_END, **end, error=detail or _why_unjudged(reason, label, click=click))
            if reason == "error":
                # 调用本身失败（鉴权、网关挂了）：后面几批多半一样，不再一次次去撞
                details["error"] = detail
                stop([*batch, *pending], reason, detail)
                break
            stop(batch, reason, detail)
            continue
        emit(EventType.LLM_END, **end)
        if items is None:
            stop(batch, "format")
            continue
        _take(items, batch, request, verdicts, judge=judge, post_seal=post_seal)
        stop(batch, "missing")

    for c in request.cands:              # 兜底：每个候选都有判定
        verdicts.setdefault(c.unit, _unjudged("missing", _why_unjudged("missing", label), judge=judge,
                                              post_seal=post_seal))
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    outcome = _outcome(request, verdicts, budget, details, click=click, shown=label)
    outcome.update({
        "model": judge, "priced": priced, "keys": keys, "cost_usd": usage["cost_usd"],
        "estimated_usd": round(estimated, 6), "calls": usage["calls"], "reused": reused,
        "duration_ms": int((time.monotonic() - started) * 1000), "usage": usage, "notes": notes,
    })
    if hit := outcome["limits_hit"]:
        labels = "、".join(_limit_label(r, label, click=click) for r in hit)
        n = sum(outcome["unjudged"].get(r, 0) for r in hit)
        emit(EventType.LOG, level="warn", code="judge_limit", limits=hit, unjudged=n,
             message=f"结论句裁判已到上限（{labels}）：{n} 句没判，记为未裁判；已判的保留")
    if failed := [g for r, g in outcome["_gaps"] if r not in LIMITS]:
        emit(EventType.LOG, level="warn", code="judge_failed", message="结论句裁判没跑完：" + "；".join(failed))
    outcome.pop("_gaps")
    return outcome


def _take(items: list[Any], batch: list[Candidate], request: JudgeRequest, verdicts: dict[str, dict[str, Any]], *,
          judge: str | None, post_seal: bool) -> None:
    """模型给的判定逐条收下：不在这一批的编号不认，判定不认识的记格式不对，理由截到 60 字，
    used 只留这一批摘录里真有的编号。"""
    ids = {c.unit for c in batch}
    aliases = {a for c in batch for a in c.aliases if a in request.excerpts}
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("unit") or "").strip().strip("[]")
        if uid not in ids or uid in verdicts:
            continue
        status = str(item.get("verdict") or "").strip().lower()
        if status not in JUDGED:
            verdicts[uid] = _unjudged("format", f"裁判给的判定「{str(item.get('verdict'))[:20]}」不认识，这句没判",
                                      judge=judge, post_seal=post_seal)
            continue
        used = item.get("used") if isinstance(item.get("used"), list) else []
        verdicts[uid] = {"status": status, "rationale": _clean(str(item.get("rationale") or ""))[:RATIONALE_MAX],
                         "judge": judge, "post_seal": post_seal,
                         "used": list(dict.fromkeys(u.strip() for u in used if isinstance(u, str)
                                                    and u.strip() in aliases))}


def _outcome(request: JudgeRequest, verdicts: dict[str, dict[str, Any]], budget: Budget, details: dict[str, str], *,
             click: bool = False, shown: Budget | None = None) -> dict[str, Any]:
    """汇总判定。outcome.budget 是这一轮实际用的上限；缺口里写的上限按 shown（缺省同 budget）。"""
    label = shown or budget
    counts = {s: 0 for s in VERDICTS}
    why: dict[str, int] = {}
    for c in request.cands:
        v = verdicts[c.unit]
        counts[v["status"]] += 1
        if v["status"] == "unjudged":
            why[v["reason"]] = why.get(v["reason"], 0) + 1
    gaps: list[tuple[str, str]] = []
    for reason in REASONS:
        n = why.get(reason)
        if not n or reason == "on_demand":
            continue
        if reason in LIMITS:
            text = f"有 {n} 句结论没裁判：已到上限（{_limit_label(reason, label, click=click)}）"
        elif reason == "error":
            text = f"有 {n} 句结论没裁判：裁判调用失败（{details.get('error') or '原因不明'}）"
        elif reason == "format":
            text = f"有 {n} 句结论没裁判：裁判给的结果格式不对"
        else:
            text = f"有 {n} 句结论裁判漏判了"
        gaps.append((reason, text))
    return {
        "verdicts": {c.unit: verdicts[c.unit] for c in sorted(request.cands, key=lambda c: c.order)},
        "candidates": len(request.cands), "screened": list(request.screened), "skipped": dict(request.skipped),
        "counts": counts, "unjudged": {r: why[r] for r in REASONS if r in why},
        "limits_hit": [r for r in LIMITS if r in why], "budget": budget.as_dict(),
        "gaps": [text for _, text in gaps], "_gaps": gaps,
    }


async def judge_doc(doc: dict[str, Any], catalog: dict[str, Any] | None = None, *, spec: ModelSpec, budget: Budget,
                    units: Iterable[str] | None = None, loader: Callable[[str], Any] | None = None,
                    masked: dict[str, Any] | None = None, known: dict[str, Any] | None = None,
                    post_seal: bool = False, emit: Emit | None = None, click: bool = False) -> dict[str, Any]:
    """判一份报告文档（units 给了就只判这几句）。见 prepare 和 run_request。"""
    request = prepare(doc, catalog, units=units, loader=loader, masked=masked)
    return await run_request(request, spec=spec, budget=budget, known=known, post_seal=post_seal, emit=emit,
                             click=click)


# --------------------------------------------------------------------------
# 写回文档
# --------------------------------------------------------------------------

#: 文档的 judge 摘要里、report.checked 和节点产出的 judge 里都有的键
SUMMARY_KEYS = ("mode", "model", "on_unsupported", "rewrite_once", "limits", "candidates", "screened", "counts",
                "unjudged", "limits_hit", "complete", "priced", "cost_usd", "calls", "reused", "duration_ms",
                "gaps", "notes", "same_model", "rewrite")


def summarize(outcome: dict[str, Any], *, mode: str, on_unsupported: str = "degrade", rewrite_once: bool = False,
              same_model: bool = False, rewrite: dict[str, Any] | None = None) -> dict[str, Any]:
    """裁判摘要：写进文档的 judge，也原样放进 report.checked 和节点产出。"""
    counts = outcome["counts"]
    return {
        "mode": mode, "model": outcome.get("model"), "on_unsupported": on_unsupported, "rewrite_once": rewrite_once,
        "limits": outcome.get("budget"), "candidates": outcome["candidates"], "screened": outcome["screened"],
        "counts": counts, "unjudged": outcome["unjudged"], "limits_hit": outcome["limits_hit"],
        "complete": mode == "inline" and counts["unjudged"] == 0,
        "priced": outcome.get("priced"), "cost_usd": outcome.get("cost_usd", 0.0), "calls": outcome.get("calls", 0),
        "reused": outcome.get("reused", 0), "duration_ms": outcome.get("duration_ms", 0),
        "gaps": outcome["gaps"] if mode == "inline" else [], "notes": outcome.get("notes") or [],
        "same_model": same_model, "rewrite": rewrite,
    }


def apply_judgement(doc: dict[str, Any], outcome: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """判定写进文档：每句的 unit.verdict、文档的 judge 摘要、stats 里各判定的句数。返回同一份文档。"""
    units = {u.get("id"): u for _, u in iter_units(doc)}
    for uid, verdict in outcome["verdicts"].items():
        if uid in units:
            units[uid]["verdict"] = verdict
    doc["judge"] = summary
    doc["stats"] = {**(doc.get("stats") or {}), **summary["counts"]}
    return doc


def on_demand(doc: dict[str, Any], *, on_unsupported: str = "degrade", rewrite_once: bool = False) -> dict[str, Any]:
    """探索运行：节点里不花这笔钱。候选句标「未裁判 · 按需」，点开哪句再判哪句。返回摘要（已写进文档）。"""
    request = prepare(doc, {})
    budget = Budget()
    verdicts = {c.unit: _unjudged("on_demand", _why_unjudged("on_demand", budget), judge=None, post_seal=False)
                for c in request.cands}
    outcome = _outcome(request, verdicts, budget, {})
    outcome.pop("_gaps")
    outcome["budget"] = None
    summary = summarize(outcome, mode="on_demand", on_unsupported=on_unsupported, rewrite_once=rewrite_once)
    apply_judgement(doc, outcome, summary)
    return summary
