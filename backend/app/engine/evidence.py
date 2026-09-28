"""可点击的证据：报告里的每个数字由系统从证据里取、按规则渲染，出处写在数据结构里。

以前叙述是模型写的自由文本，出具时再按数值回头猜每个数来自哪个指标——5 个指标
都是 0.0 时，报告里所有的 0 全算给第一个。这里反过来：模型只写引用标记
`[[m:gmv]]`，数值由系统从口径卡取出、按口径卡的格式渲染。叙述层在结构上就写不出
数字，按数值猜出处这件事不再需要。

整个模块是纯函数（不碰数据库、不调模型），报告节点自查、出口契约复核、证据接口
共用这一份，免得三处各写一套规则、各判各的。

标记语法（本期支持的四种）：

    [[m:<指标 id>]]          口径卡指标，渲染成「值+单位」
    [[m:<指标 id>|万]]        换算显示：万 / 亿 / pct / int / .N（小数位），由渲染器完成
    [[i:<输入字段>]]          运行输入
    [[see:<ref>,<ref>…]]     依据：挂在句末，不渲染

`[[v:]]` `[[t:]]` `[[c:]]` `[[q:]]` `[[table:]]` 能解析，本期一律判为解析不了，
原因写「这种引用在后续版本支持」。

偏移一律按 Unicode 码点算（Python 的 str 下标）。前端的 JS 字符串按 UTF-16 计，
碰到码点在 BMP 之外的字符（emoji）要自己换算。
"""

from __future__ import annotations

import bisect
import math
import re
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any, Literal, TypedDict

from app.core.artifact_store import canonical_json, content_hash
from app.engine.issuance import extract_numbers, number_allowance

DOC_SCHEMA = "agentlab.report/1"

# --------------------------------------------------------------------------
# 数据结构（方案 1.5）。都是普通 dict，TypedDict 只用来写清形状
# --------------------------------------------------------------------------


class EvidenceEntry(TypedDict, total=False):
    """台账条目：state["evidence"] 里的一行，同时写进 node.finished.evidence。"""

    kind: Literal["query", "metric_set", "retrieval", "tool", "node_output", "schema"]
    node_id: str
    exec: int                 # 这个节点第几次执行（循环里会有多次），从 1 数
    artifact: str             # 被引用的那件工件
    via: str                  # 外层包装的工件（query_snapshot 外面的 tool_snapshot）
    call_id: str
    tool: str
    source: str
    columns: list[str]
    rows: int
    truncated: bool
    caliber: str              # metric_set 专有：口径名、版本、指标 id 清单
    version: str
    metrics: list[str]


class Citation(TypedDict, total=False):
    ref: str                  # 标记里写的原文：gmv、week、Q4.r0.amount
    alias: str                # 目录里的键：m:gmv、i:week、Q4
    locator: dict[str, Any]
    eid: str
    kind: str                 # metric / input / query / cell / table / column / quote
    role: Literal["value", "entity", "quote", "support"]
    status: Literal["resolved", "unresolved", "mismatch"]
    reason: str               # unresolved 时为什么
    conv: str                 # 换算：万 / 亿 / pct / int / .N
    value: Any
    rendered: str


class Segment(TypedDict, total=False):
    id: str
    kind: Literal["text", "number", "value", "entity", "quote", "structural"]
    text: str
    span: list[int]           # [start, end)，在 doc.markdown 里的码点偏移
    ref: str                  # 带前缀的原始引用：m:gmv、i:week
    cite: Citation
    state: Literal["deterministic", "probabilistic", "none", "neutral"]
    strong: bool
    issue: str                # 这一段本身的问题：uncited_number / unresolved_ref


class Unit(TypedDict, total=False):
    id: str
    kind: Literal["claim", "connective", "heading", "code"]
    span: list[int]           # 内容的起止（不含行首的 Markdown 语法）
    cites: list[str]          # 这一句引用过的 alias，行内和 [[see:]] 合在一起、去重
    see: list[Citation]       # [[see:]] 里的每一个引用，单独核对
    segments: list[Segment]
    loc: dict[str, int]       # 表格单元格：{row, col}，表头 row = -1
    depth: int                # 列表项的缩进层级


class Block(TypedDict, total=False):
    id: str
    type: Literal["heading", "paragraph", "list", "table", "quote", "code", "hr"]
    level: int
    ordered: bool
    start: int                # 有序列表的起始序号
    lang: str
    units: list[Unit]


class Violation(TypedDict, total=False):
    code: str
    message: str
    span: list[int]
    text: str
    segment: str
    unit: str
    ref: str
    context: str


# --------------------------------------------------------------------------
# eid：内容派生的全局标识
# --------------------------------------------------------------------------


def make_eid(kind: str, artifact: str | None, locator: dict[str, Any] | None) -> str:
    """ev:<kind>:<16 位 hex>。同样的 (kind, 工件, 定位) 永远得到同一个 eid。

    解析时重算一次，和目录里记的对不上就说明目录被改过。
    """
    digest = content_hash(canonical_json({"kind": kind, "artifact": artifact, "locator": locator or {}}))
    return f"ev:{kind}:{digest[:16]}"


def input_eid(field: str, value: Any) -> str:
    """运行输入的 eid。输入没有工件，工件那一格放值的指纹：同一个字段换了值
    （2026-W37 → 2026-W38）就是另一件证据，跨运行比对才有意义。

    目录条目里记着 value，拿它就能复算：make_eid("input", content_hash(canonical_json(value)), {"field": field})。
    """
    return make_eid("input", content_hash(canonical_json(value)), {"field": field})


# --------------------------------------------------------------------------
# 渲染：数值 → 报告里显示的那几个字
#
# 规则是固定的，同一个值永远渲染成同一串字：报告节点边写边渲染一次，出口契约
# 复核时再渲染一次，两边逐字比对。所以这里一律用 Decimal 按「四舍五入」算，不走
# float 的 round（2.675 在 float 里是 2.67499…，round 出来是 2.67）。
# --------------------------------------------------------------------------

FORMATS = ("plain", "thousands", "percent_of_ratio")
#: 口径卡没写 format 时按千分位
DEFAULT_FORMAT = "thousands"
#: 值拿不到时显示的字。不插值、不写 0
MISSING = "—"

_CONV_SCALE = {"万": Decimal(10) ** 4, "亿": Decimal(10) ** 8}
_CONV_PLACES = re.compile(r"^\.([0-6])$")
#: 小数位最多写到多少。负数是取整到十位、百位……（round(x, -2) 的意思）
MAX_PLACES = 100
#: 整数部分最多多少位。浮点数到头也就 309 位，再大的整数按规则一位位写出来没有意义
_MAX_DIGITS = 1000


class RenderError(ValueError):
    """这个值按这种写法渲染不出来。报告里算作解析不了的引用，原因就是异常消息。"""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decimal(value: int | float) -> Decimal:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RenderError(f"{value} 不是一个有限的数")
        return Decimal(repr(value))
    return Decimal(value)


def _ctx(d: Decimal, places: int = 10) -> Context:
    """精度跟着值走。定死一个精度的话，1e55 留两位小数就超出精度（抛 InvalidOperation），
    70 位的整数去尾零时会被悄悄舍入成 60 位有效数字——显示出来的就不是那个数了。"""
    need = max(len(d.as_tuple().digits), d.adjusted() + max(places, 0)) + 10
    return Context(prec=max(60, need), rounding=ROUND_HALF_UP)


def _shown(d: Decimal) -> str:
    text = format(d, "f")
    return text if len(text) <= 24 else format(d, ".6g")


def _digits(d: Decimal, places: int | None, *, group: bool, strip: bool = False,
            zero: str | None = None) -> str:
    """按小数位四舍五入后写出来。

    一个不是 0 的值写出来成了 0（29 单换算成「0万单」、0.004 取整成「0」），就不写：
    抛 RenderError，原因是 zero。系统渲染的数带着「有出处」的标记，一个假的 0 比模型
    自己编的数还难发现。
    """
    # 没写小数位时最多留 10 位再去掉尾零：0.1 + 0.2 显示 0.3，而不是 0.30000000000000004
    exp = -(10 if places is None else places)
    ctx = _ctx(d, -exp)
    q = d.quantize(Decimal(1).scaleb(exp), context=ctx)
    if q.is_zero() and not d.is_zero():
        raise RenderError(zero or f"{_shown(d)} 按 {-exp} 位小数显示为 0，看不出真实的值")
    if places is None or strip:
        q = q.normalize(context=ctx)
    if q.is_zero():
        q = q.copy_abs()              # 不显示 -0.0
    return format(q, ",f" if group else "f")


def _clean(text: str) -> str:
    """文本值进正文前压成一行：换行会被当成新的段落或列表。"""
    return re.sub(r"\s+", " ", text).strip()


def render_number(
    value: Any,
    *,
    unit: str = "",
    decimals: int | None = None,
    fmt: str | None = None,
    conv: str | None = None,
) -> str:
    """按口径卡的定义把一个值渲染成字。

    - format：thousands（默认，千分位）/ plain（不分组，年份、编号这类）/
      percent_of_ratio（值是比率 0.0235，显示成 2.35%，小数位按 decimals − 2）
    - decimals：固定小数位；不写就按值本来的样子（整数不带小数点）
    - 单位直接接在数字后面：45,678.5元、8.7%
    - conv 是报告里写的换算：万 / 亿（保留两位、去尾零，单位接在后面：4.57万元）、
      pct（比率 ×100 加 %）、int（取整）、.N（N 位小数）

    值是 None 显示「—」。文本值原样（压成一行），不能换算。

    不是 0 的值显示出来成了 0（换算、取整、小数位不够），抛 RenderError——报告里这个
    引用就算解析不了，写作者会被要求换一种写法，而不是给读者一个带出处的假 0。
    """
    fmt = fmt or DEFAULT_FORMAT
    if fmt not in FORMATS:
        raise RenderError(f"format 只能是 {' / '.join(FORMATS)}，写的是 {fmt!r}")
    if value is None:
        return MISSING
    if not is_number(value):
        if conv:
            raise RenderError(f"「{_clean(str(value))[:40]}」不是数，不能换算成 {conv}")
        if isinstance(value, bool):
            return "是" if value else "否"
        return _clean(str(value))
    if decimals is not None and (isinstance(decimals, bool) or not isinstance(decimals, int)
                                 or not -MAX_PLACES <= decimals <= MAX_PLACES):
        raise RenderError(f"小数位要是 -{MAX_PLACES} 到 {MAX_PLACES} 的整数，写的是 {decimals!r}")
    try:
        return _render_numeric(_decimal(value), unit=unit, decimals=decimals, fmt=fmt, conv=conv)
    except RenderError:
        raise
    except (ArithmeticError, ValueError) as e:       # decimal 的 InvalidOperation 也在这里
        raise RenderError(f"{value!r} 按规则渲染不出来（{type(e).__name__}）") from e


def _render_numeric(d: Decimal, *, unit: str, decimals: int | None, fmt: str, conv: str | None) -> str:
    if d.adjusted() >= _MAX_DIGITS:
        raise RenderError(f"这个数有 {d.adjusted() + 1} 位，太大了，没法按规则显示")
    ratio = fmt == "percent_of_ratio"
    group = fmt != "plain"
    shown = _shown(d)
    if conv in _CONV_SCALE:
        if ratio:
            raise RenderError(f"这个指标是比率，不能换算成 {conv}")
        scaled = _ctx(d).divide(d, _CONV_SCALE[conv])
        return _digits(scaled, 2, group=True, strip=True,
                       zero=f"{shown} 换算成「{conv}」后显示为 0，精度不够：去掉换算，或者换小一级的单位"
                       ) + conv + unit
    rounded = f"{shown} 按 |{conv} 显示为 0，精度不够：去掉换算，或者换成 |.N 多留几位小数"
    if conv == "pct":
        if "%" in unit or "％" in unit:
            raise RenderError("这个指标本身就是百分数（单位是 %），不能再换算成 pct")
        places = max(decimals - 2, 0) if decimals is not None else 1
        return _digits(_ctx(d).multiply(d, 100), places, group=group, zero=rounded) + "%"

    if conv == "int":
        places: int | None = 0
    elif conv and (m := _CONV_PLACES.match(conv)):
        places = int(m.group(1))
    elif conv:
        raise RenderError(f"不认识的换算 |{conv}，只支持 万 / 亿 / pct / int / .N（N 为 0–6）")
    else:
        rounded = (f"{shown} 按口径卡的小数位（{decimals}）显示为 0，精度不够：口径卡要多留几位小数"
                   if decimals is not None else
                   f"{shown} 太小：不写小数位时最多留 10 位小数，显示为 0。口径卡要写 decimals")
        places = (max(decimals - 2, 0) if ratio else decimals) if decimals is not None else None
    if ratio:
        return _digits(_ctx(d).multiply(d, 100), places, group=group, zero=rounded) + "%"
    return _digits(d, places, group=group, zero=rounded) + unit


def render_metric(metric: dict[str, Any], conv: str | None = None) -> str:
    """口径卡里的一个指标按它自己的格式渲染。"""
    return render_number(
        metric.get("value"),
        unit=str(metric.get("unit") or ""),
        decimals=metric.get("decimals"),
        fmt=metric.get("format"),
        conv=conv,
    )


# --------------------------------------------------------------------------
# 标记语法
# --------------------------------------------------------------------------

#: 流式渲染时，一个 [[ 之后攒多少字还没闭合，就认定它不是标记、原样放出去
_HOLD_LIMIT = 160
#: 一个标记（从 [[ 到 ]]，含两头）最长多少字。整篇解析和流式渲染用同一个上限：
#: 超长的两边都当普通文字，流里看到的才会和最终正文一模一样。标记本身最长也就几十个字
MARKER_MAX = _HOLD_LIMIT + 1
#: 紧跟在 [[ 后面：MARKER_MAX 以内就能碰到 ]]（中间不许有方括号和换行）
_MARKER_GUARD = rf"(?=[^\[\]\n]{{0,{MARKER_MAX - 4}}}\]\])"
#: [[kind:body]]。body 里不许有方括号和换行——这样一个没闭合的 [[ 最多吞到行尾
MARKER_RE = re.compile(
    r"\[\[" + _MARKER_GUARD + r"[ \t]*(?P<kind>[A-Za-z]+)[ \t]*:(?P<body>[^\[\]\n]*?)\]\]")
SUPPORTED_KINDS = frozenset({"m", "i", "see"})
LATER_KINDS = frozenset({"v", "t", "c", "q", "table"})
LATER_REASON = "这种引用在后续版本支持"
_ROLE = {"t": "entity", "c": "entity", "q": "quote"}
_SEG_KIND = {"t": "entity", "c": "entity", "q": "quote"}


def _parse_body(kind: str, body: str) -> dict[str, Any]:
    if kind == "see":
        return {"refs": [r.strip() for r in body.split(",") if r.strip()]}
    if kind == "table":
        head, *rest = body.split() or [""]
        return {"ref": head, "options": dict(p.split("=", 1) for p in rest if "=" in p)}
    ref, _, tail = body.partition("|")
    if kind == "q":
        return {"ref": ref.strip(), "quote": tail.strip()}
    return {"ref": ref.strip(), "conv": tail.strip() or None}


def parse_markers(text: str) -> list[dict[str, Any]]:
    """文本里所有的引用标记，按出现顺序：{kind, body, raw, start, end, ref / conv / refs / …}。"""
    out: list[dict[str, Any]] = []
    for m in MARKER_RE.finditer(text):
        kind, body = m.group("kind"), m.group("body").strip()
        out.append({"kind": kind, "body": body, "raw": m.group(0), "start": m.start(), "end": m.end(),
                    **_parse_body(kind, body)})
    return out


def _parse_ref(ref: str) -> dict[str, Any]:
    """片段上记的 ref（m:gmv|万）还原成标记的解析结果。"""
    kind, _, body = ref.partition(":")
    kind, body = kind.strip(), body.strip()
    return {"kind": kind, "body": body, **_parse_body(kind, body)}


def placeholder(kind: str, body: str) -> str:
    """解析不了的引用在正文里的占位：⟦?m:gmvx⟧。流里和最终文档里是同一个样子。"""
    return f"⟦?{kind}:{body}⟧"


# --------------------------------------------------------------------------
# 证据目录：alias → 条目
# --------------------------------------------------------------------------


def _metric_sources(
    nodes: dict[str, Any], ledger: list[dict[str, Any]], metrics_from: list[str] | None,
    allowed: set[str] | None,
) -> list[tuple[str, dict[str, Any], str | None]]:
    latest: dict[str, str] = {}
    order: list[str] = []
    for entry in ledger:
        if isinstance(entry, dict) and entry.get("kind") == "metric_set" and entry.get("node_id"):
            order.append(entry["node_id"])
            if entry.get("artifact"):
                latest[entry["node_id"]] = entry["artifact"]
    # 台账里没有、节点产出里有的：旧 checkpoint 续跑时台账是空的，口径卡的产出还在
    order.extend(nid for nid, p in nodes.items() if isinstance(p, dict) and p.get("kind") == "metric_set")
    if metrics_from is not None:
        order = list(metrics_from)
    out = []
    for nid in dict.fromkeys(order):
        payload = nodes.get(nid)
        if allowed is not None and nid not in allowed:
            continue
        if isinstance(payload, dict) and payload.get("kind") == "metric_set":
            out.append((nid, payload, payload.get("artifact") or latest.get(nid)))
    return out


def build_catalog(
    *,
    nodes: dict[str, Any],
    ledger: list[dict[str, Any]] | None = None,
    inputs: dict[str, Any] | None = None,
    metrics_from: list[str] | str | None = None,
    allowed: set[str] | frozenset[str] | list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """从证据台账、节点产出、运行输入建目录。

    - nodes：state["nodes"]，口径卡的指标值从这里取（和台账里的工件是同一份内容）
    - ledger：state["evidence"]，决定顺序、提供工件 id；查询、检索按台账顺序编 Q1… / K1…
    - metrics_from：只收这几张口径卡；不传（None、空列表、空字符串）就收全部——和
      validate_graph 的理解一致，那边空列表就是「所有上游口径卡」
    - allowed：只收这些节点的证据（报告节点传它的祖先），不传不限

    同一个指标 id 出现在两张卡里时不登记 m:<id>，只登记 m:<卡的节点 id>.<id>——
    按先来后到默认给一个，正是旧契约错配的那种错。
    """
    if not metrics_from:
        metrics_from = None
    elif isinstance(metrics_from, str):
        metrics_from = [metrics_from]
    allow = set(allowed) if allowed is not None else None
    entries = [e for e in (ledger or []) if isinstance(e, dict)]
    catalog: dict[str, dict[str, Any]] = {}

    sources = _metric_sources(nodes, entries, metrics_from, allow)
    seen: dict[str, int] = {}
    for _, payload, _ in sources:
        for metric in payload.get("metrics") or []:
            mid = str(metric.get("id") or "")
            seen[mid] = seen.get(mid, 0) + 1
    for node_id, payload, artifact in sources:
        for metric in payload.get("metrics") or []:
            mid = str(metric.get("id") or "")
            if not mid:
                continue
            alias = f"m:{mid}" if seen[mid] == 1 else f"m:{node_id}.{mid}"
            if alias in catalog:
                continue
            try:
                rendered = render_metric(metric)
            except RenderError:
                rendered = MISSING
            locator = {"metric": mid}
            catalog[alias] = {
                "alias": alias, "kind": "metric", "eid": make_eid("metric", artifact, locator),
                "node_id": node_id, "artifact": artifact, "locator": locator,
                "caliber": payload.get("caliber") or "", "version": payload.get("caliber_version") or "",
                "name": metric.get("name") or mid, "unit": metric.get("unit") or "",
                "decimals": metric.get("decimals"), "format": metric.get("format") or DEFAULT_FORMAT,
                "value": metric.get("value"),
                "status": metric.get("status") or ("ok" if metric.get("value") is not None else "missing_input"),
                "rendered": rendered, "label": f"{metric.get('name') or mid} = {rendered}",
            }

    queries = retrievals = 0
    for entry in entries:
        if allow is not None and entry.get("node_id") not in allow:
            continue
        if entry.get("kind") == "query":
            queries += 1
            alias, kind = f"Q{queries}", "query"
        elif entry.get("kind") == "retrieval":
            retrievals += 1
            alias, kind = f"K{retrievals}", "retrieval"
        else:
            continue
        rows = entry.get("rows")
        catalog[alias] = {
            "alias": alias, "kind": kind, "eid": make_eid(kind, entry.get("artifact"), {}),
            "locator": {},
            **{k: entry[k] for k in ("node_id", "exec", "artifact", "via", "call_id", "tool", "source",
                                     "columns", "rows", "truncated") if k in entry},
            "label": f"{entry.get('tool') or entry.get('source') or kind}"
                     + (f" · {rows} 行" if isinstance(rows, int) else ""),
        }

    for key, value in (inputs or {}).items():
        if not isinstance(key, str) or not key or isinstance(value, (dict, list, tuple)):
            continue
        locator = {"field": key}
        rendered = render_number(value, fmt="plain") if value not in (None, "") else MISSING
        catalog[f"i:{key}"] = {
            "alias": f"i:{key}", "kind": "input", "eid": input_eid(key, value),
            "locator": locator, "value": value, "rendered": rendered, "label": f"{key} = {rendered}",
        }
    return catalog


# --------------------------------------------------------------------------
# 解析一个引用
# --------------------------------------------------------------------------


def _unresolved(marker: dict[str, Any], alias: str, ev_kind: str, reason: str, *,
                rendered: str | None = None) -> dict[str, Any]:
    """解析不了的引用。rendered 是正文里显示的字：缺值显示「—」，其余是占位 ⟦?m:gmvx⟧。"""
    kind, body = marker["kind"], marker.get("body", "")
    return {"ref": marker.get("ref") or "", "alias": alias, "kind": ev_kind,
            "role": _ROLE.get(kind, "value"), "status": "unresolved", "reason": reason,
            "rendered": rendered if rendered is not None else placeholder(kind, body)}


def _metric_alias(ref: str, catalog: dict[str, Any]) -> tuple[str | None, str]:
    alias = f"m:{ref}"
    if alias in catalog:
        return alias, ""
    doubles = sorted(a for a in catalog if a.startswith("m:") and a.endswith(f".{ref}"))
    if doubles:
        return None, (f"指标 {ref} 同时出现在好几张口径卡里，要写成带卡名的形式："
                      + "、".join(f"[[{a}]]" for a in doubles[:4]))
    return None, f"目录里没有指标 {ref}（能用的指标见证据目录）"


def resolve_marker(marker: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """行内标记（m / i / v / t / c / q / table）→ Citation，带 rendered（正文里显示的字）。"""
    kind = marker["kind"]
    ref = marker.get("ref") or ""
    if kind in LATER_KINDS:
        alias = {"t": f"t:{ref}", "c": f"c:{ref}"}.get(kind, ref.split(".")[0])
        ev_kind = {"v": "cell", "t": "table", "c": "column", "q": "quote", "table": "query"}[kind]
        return _unresolved(marker, alias, ev_kind, LATER_REASON)
    if kind not in ("m", "i"):
        return _unresolved(marker, f"{kind}:{ref}", kind,
                           f"不认识的引用类型 {kind}：本期只支持 m（口径卡指标）、i（运行输入）、see（依据）")

    conv = marker.get("conv")
    if kind == "m":
        ev_kind = "metric"
        alias, why = _metric_alias(ref, catalog)
        if alias is None:
            return _unresolved(marker, f"m:{ref}", ev_kind, why)
        entry = catalog[alias]
        if entry.get("value") is None:
            return _unresolved(marker, alias, ev_kind, f"指标「{entry.get('name') or ref}」这次没有值（缺输入），"
                               "不能写进报告", rendered=MISSING)
        try:
            rendered = render_metric(entry, conv)
        except RenderError as e:
            return _unresolved(marker, alias, ev_kind, str(e))
    else:
        ev_kind, alias = "input", f"i:{ref}"
        entry = catalog.get(alias)
        if entry is None:
            return _unresolved(marker, alias, ev_kind, f"运行输入里没有 {ref}")
        if entry.get("value") in (None, ""):
            return _unresolved(marker, alias, ev_kind, f"运行输入 {ref} 是空的", rendered=MISSING)
        try:
            rendered = render_number(entry["value"], fmt="plain", conv=conv)
        except RenderError as e:
            return _unresolved(marker, alias, ev_kind, str(e))
    cite = {"ref": ref, "alias": alias, "locator": dict(entry.get("locator") or {}), "eid": entry["eid"],
            "kind": ev_kind, "role": "value", "status": "resolved", "value": entry.get("value"),
            "rendered": rendered}
    if conv:
        cite["conv"] = conv
    return cite


_ROW_REF = re.compile(r"^(?P<alias>[QK]\d+)(?:\.r(?P<row>\d+))?$")


def resolve_support(ref: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """[[see:]] 里的一个依据：m:gmv、i:week、Q4、Q4.r0、K2。只核对「这件证据存在」。"""
    ref = ref.strip()
    kind, sep, body = ref.partition(":")
    base = {"ref": ref, "role": "support"}
    if sep and kind in LATER_KINDS:
        return {**base, "alias": ref, "kind": kind, "status": "unresolved", "reason": LATER_REASON}
    alias: str | None = None
    locator: dict[str, Any] = {}
    why = f"目录里没有 {ref}"
    if sep and kind == "m":
        alias, why = _metric_alias(body, catalog)
    elif sep and kind == "i":
        alias = ref if ref in catalog else None
        why = f"运行输入里没有 {body}"
    elif sep:
        why = f"不认识的依据写法 {ref}：写 m:指标、i:输入，或者 Q1 / K1 这样的编号"
    elif m := _ROW_REF.match(ref):
        alias = m.group("alias") if m.group("alias") in catalog else None
        if alias and m.group("row") is not None:
            row, total = int(m.group("row")), catalog[alias].get("rows")
            if isinstance(total, int) and row >= total:
                alias, why = None, f"{m.group('alias')} 只有 {total} 行，没有第 {row} 行（从 0 数）"
            else:
                locator = {"row": row}
    elif f"m:{ref}" in catalog:
        alias = f"m:{ref}"          # 漏写了 m: 前缀的指标 id，意思没有歧义
    if alias is None:
        return {**base, "alias": ref, "kind": kind if sep else "unknown", "status": "unresolved",
                "reason": why}
    entry = catalog[alias]
    return {**base, "alias": alias, "kind": entry["kind"], "status": "resolved", "eid": entry["eid"],
            "locator": {**(entry.get("locator") or {}), **locator}}


def resolve_ref(ref: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """片段上记的 ref（m:gmv|万、i:week）→ Citation。证据接口点开片段时用它重新解析。"""
    return resolve_marker(_parse_ref(ref), catalog)


def render_markers(text: str, catalog: dict[str, Any]) -> str:
    """把一段文本里的标记全部换成字：m / i 渲染成值，see 去掉，解析不了的换成占位。"""
    out: list[str] = []
    last = 0
    for marker in parse_markers(text):
        out.append(text[last:marker["start"]])
        if marker["kind"] != "see":
            out.append(resolve_marker(marker, catalog)["rendered"])
        last = marker["end"]
    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------
# 流式渲染：让 llm.token 里出现的就是最终的数字
# --------------------------------------------------------------------------

def _safe_cut(text: str) -> int:
    """能放出去的前缀有多长：最后一个还可能长成标记的 [[ 之前都能放。

    一个标记最长 MARKER_MAX 个字，所以没闭合的时候最多攒 MARKER_MAX - 1 = _HOLD_LIMIT 个字；
    超过了、或者碰到换行，它就不可能再是标记。末尾单独一个 [ 也要留着——下一块的
    开头可能是另一个 [。
    """
    start = text.rfind("[[")
    if start != -1 and text.find("]]", start) == -1:
        tail = text[start:]
        if len(tail) <= _HOLD_LIMIT and "\n" not in tail:
            return start
    if text.endswith("["):
        return len(text) - 1
    return len(text)


class StreamRenderer:
    """边收模型的 token 边渲染：缓冲到没有未闭合的 [[ 为止，再把这一段里的标记换成字。

        renderer = StreamRenderer(catalog)
        async for chunk in model.astream(messages):
            if text := renderer.feed(message_text(chunk)):
                ctx.emit(EventType.LLM_TOKEN, delta=text)
        if tail := renderer.flush():
            ctx.emit(EventType.LLM_TOKEN, delta=tail)

    所有 feed / flush 的输出拼起来，等于 render_markers(全文)，不论模型怎么分块：
    整篇解析也只认 MARKER_MAX 以内的标记，超长的两边都原样当文字。
    """

    def __init__(self, catalog: dict[str, Any]) -> None:
        self.catalog = catalog
        self.pending = ""

    def feed(self, delta: str) -> str:
        self.pending += delta or ""
        cut = _safe_cut(self.pending)
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        return render_markers(ready, self.catalog) if ready else ""

    def flush(self) -> str:
        ready, self.pending = self.pending, ""
        return render_markers(ready, self.catalog) if ready else ""


# --------------------------------------------------------------------------
# 切块：和前端 Markdown.tsx 认同一套块语法（标题、段落、列表、表格、引用、代码、分隔线）
#
# 在模型的原文（带标记）上切，而不是在渲染后的正文上切：一个指标值渲染出来恰好是
# 「3. 」开头，不能因此把一句话当成有序列表。
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```([A-Za-z0-9_]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^(\s*>\s?)(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _is_block_start(line: str) -> bool:
    return bool(_FENCE.match(line) or _HR.match(line) or _HEADING.match(line) or _BULLET.match(line)
                or _ORDERED.match(line) or _QUOTE.match(line) or _TABLE_ROW.match(line))


class _Item:
    """一个将来的 unit（或者一段要再切成句子的内容）：原文上的若干片 (s|c, start, end)。"""

    __slots__ = ("pieces", "pos", "split", "kind", "loc", "depth")

    def __init__(self, pieces: list[tuple[str, int, int]], pos: int, *, split: bool = False,
                 kind: str | None = None, loc: dict[str, int] | None = None, depth: int | None = None):
        self.pieces, self.pos, self.split, self.kind = pieces, pos, split, kind
        self.loc, self.depth = loc, depth


class _Block:
    __slots__ = ("type", "meta", "items")

    def __init__(self, type_: str, meta: dict[str, Any], items: list[_Item]):
        self.type, self.meta, self.items = type_, meta, items


def _scan_blocks(text: str, protected: list[tuple[int, int]]) -> list[_Block]:
    lines = text.split("\n")
    starts: list[int] = []
    acc = 0
    for line in lines:
        starts.append(acc)
        acc += len(line) + 1

    def end(i: int) -> int:
        return starts[i] + len(lines[i])

    blocks: list[_Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            j = i + 1
            while j < n and not _FENCE.match(lines[j]):
                j += 1
            body = [("c", starts[i + 1], end(j - 1))] if j > i + 1 else []
            last = min(j, n - 1)
            blocks.append(_Block("code", {"lang": fence.group(1)},
                                 [_Item(body, body[0][1] if body else end(last), kind="code")]))
            i = j + 1
            continue

        if _HR.match(line):
            blocks.append(_Block("hr", {}, [_Item([], end(i), kind="connective")]))
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            cs = starts[i] + heading.start(2)
            pieces = [("s", starts[i], cs)] + ([("c", cs, end(i))] if cs < end(i) else [])
            blocks.append(_Block("heading", {"level": len(heading.group(1))},
                                 [_Item(pieces, starts[i], kind="heading")]))
            i += 1
            continue

        if _TABLE_ROW.match(line) and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            items = _table_cells(lines[i], starts[i], -1, protected)
            i += 2
            row = 0
            while i < n and _TABLE_ROW.match(lines[i]):
                items.extend(_table_cells(lines[i], starts[i], row, protected))
                row += 1
                i += 1
            blocks.append(_Block("table", {}, items))
            continue

        if _QUOTE.match(line):
            pieces: list[tuple[str, int, int]] = []
            first = i
            while i < n and (q := _QUOTE.match(lines[i])):
                if i > first:
                    pieces.append(("c", starts[i] - 1, starts[i]))     # 行与行之间的换行属于正文
                cs = starts[i] + len(q.group(1))
                pieces.append(("s", starts[i], cs))
                if cs < end(i):
                    pieces.append(("c", cs, end(i)))
                i += 1
            blocks.append(_Block("quote", {}, [_Item(pieces, starts[first], split=True)]))
            continue

        bullet, ordered = _BULLET.match(line), _ORDERED.match(line)
        if bullet or ordered:
            items = []
            meta: dict[str, Any] = {"ordered": bool(ordered)}
            if ordered:
                meta["start"] = int(ordered.group(2))
            while i < n:
                b = _BULLET.match(lines[i])
                o = None if b else _ORDERED.match(lines[i])
                if b or o:
                    m = b or o
                    cs = starts[i] + m.start(2 if b else 3)
                    pieces = [("s", starts[i], cs)] + ([("c", cs, end(i))] if cs < end(i) else [])
                    items.append(_Item(pieces, starts[i],
                                       depth=len(m.group(1).replace("\t", "  ")) // 2))
                    i += 1
                elif lines[i].strip() and not _is_block_start(lines[i]) and items:
                    # 悬挂缩进的续行接到上一项：换行算正文，行首缩进算结构（Markdown.tsx 会 trim 掉）
                    lead = len(lines[i]) - len(lines[i].lstrip())
                    stop = len(lines[i].rstrip())
                    items[-1].pieces.append(("c", starts[i] - 1, starts[i]))
                    if lead:
                        items[-1].pieces.append(("s", starts[i], starts[i] + lead))
                    items[-1].pieces.append(("c", starts[i] + lead, starts[i] + stop))
                    i += 1
                else:
                    break
            blocks.append(_Block("list", meta, items))
            continue

        first = i
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            i += 1
        blocks.append(_Block("paragraph", {}, [_Item([("c", starts[first], end(i - 1))], starts[first],
                                                     split=True)]))
    return blocks


def _table_cells(line: str, base: int, row: int, protected: list[tuple[int, int]]) -> list[_Item]:
    """一行表格拆成格。和 Markdown.tsx 的 splitRow 一样按 | 拆，只是标记里的 |（[[m:x|万]]）不算。"""
    lo = len(line) - len(line.lstrip())
    hi = len(line.rstrip())
    if line[lo:lo + 1] == "|":
        lo += 1
    if hi > lo and line[hi - 1] == "|":
        hi -= 1
    inside = [(s - base, e - base) for s, e in protected if base <= s and e <= base + len(line)]
    cuts = [p for p in range(lo, hi) if line[p] == "|" and not any(s <= p < e for s, e in inside)]
    items = []
    for col, (a, b) in enumerate(zip([lo, *[c + 1 for c in cuts]], [*cuts, hi])):
        ca = a + len(line[a:b]) - len(line[a:b].lstrip())
        cb = a + len(line[a:b].rstrip())
        pieces = [("c", base + ca, base + cb)] if ca < cb else []
        items.append(_Item(pieces, base + (ca if ca < cb else a), loc={"row": row, "col": col}))
    return items


def _attach_gaps(items: list[_Item], length: int) -> bool:
    """没被任何一项认领的原文（空行、块尾换行、表格分隔行、代码围栏）作为结构片挂到后一项上。

    挂好之后，所有项的片按顺序拼起来必须正好铺满原文——这是「片段拼起来等于正文」
    的来源。铺不满说明切块有漏洞，返回 False，调用方退回整篇一段。
    """
    covered = sorted((s, e) for item in items for _, s, e in item.pieces if e > s)
    gaps: list[tuple[int, int]] = []
    pos = 0
    for s, e in covered:
        if s > pos:
            gaps.append((pos, s))
        pos = max(pos, e)
    if pos < length:
        gaps.append((pos, length))
    if not items:
        return not gaps
    positions = [item.pos for item in items]
    for s, e in gaps:
        k = bisect.bisect_left(positions, e)
        (items[k] if k < len(items) else items[-1]).pieces.append(("s", s, e))
    for item in items:
        item.pieces = sorted((p for p in item.pieces if p[2] > p[1]), key=lambda p: p[1])
    pos = 0
    for item in items:
        for _, s, e in item.pieces:
            if s != pos:
                return False
            pos = e
    return pos == length


# --------------------------------------------------------------------------
# 切句：段落和引用按句子切成 unit；[[see:]] 挂在它前面那一句
# --------------------------------------------------------------------------

_TERMINATOR = re.compile(r"(?:[。！？!?]+|\.(?=\s|$)|\n)[”’\"'）)」』】》]*")
_SEE_TAIL = re.compile(r"(?:[ \t\u3000]*\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])*\s*")
#: 和 Markdown.tsx 的 INLINE 同一套：句号落在代码、链接、粗体里面时不切，不然一对 ** 会被拆到两句
_INLINE = re.compile("|".join([
    r"(`+)[^`]+?\1",
    r"\[[^\]]+\]\([^)\s]+[^)]*\)",
    r"\*\*[^*]+?\*\*",
    r"__[^_]+?__",
    r"(?<!\*)\*[^*\n]+?\*(?!\*)",
    r"~~[^~]+?~~",
]))
_ONLY_SEE = re.compile(r"^(?:\s|\[\[" + _MARKER_GUARD + r"[ \t]*see[ \t]*:[^\[\]\n]*?\]\])*$")


def _split_sentences(item: _Item, text: str, markers: list[dict[str, Any]]) -> list[list[tuple[str, int, int]]]:
    content = [(s, e) for kind, s, e in item.pieces if kind == "c"]
    if not item.split or not content:
        return [item.pieces]
    # 把内容片接成一条虚拟文本 V，记下 V 的每个位置对应原文哪里
    v_parts, v_map = [], []
    for s, e in content:
        v_map.append((sum(len(p) for p in v_parts), s, e))
        v_parts.append(text[s:e])
    virtual = "".join(v_parts)

    def to_raw(v: int) -> int:           # V 里第 v 个字符之前的那个边界，落在原文哪里
        for v0, s, e in v_map:
            if v0 < v <= v0 + (e - s):
                return s + (v - v0)
        return content[0][0]

    def to_v(raw: int) -> int | None:
        for v0, s, e in v_map:
            if s <= raw <= e:
                return v0 + (raw - s)
        return None

    protected = [(m.start(), m.end()) for m in _INLINE.finditer(virtual)]
    for marker in markers:
        a, b = to_v(marker["start"]), to_v(marker["end"])
        if a is not None and b is not None:
            protected.append((a, b))

    cuts: list[int] = []
    for m in _TERMINATOR.finditer(virtual):
        if any(s < m.start() < e or s < m.end() < e for s, e in protected):
            continue
        cut = _SEE_TAIL.match(virtual, m.end()).end()
        if 0 < cut < len(virtual) and not any(s < cut < e for s, e in protected):
            cuts.append(cut)
    cuts = sorted(set(cuts))
    # 只剩空白或者只有 [[see:]] 的一截不单独成句，并进相邻的那一句
    kept: list[int] = []
    last = 0
    for k, cut in enumerate(cuts):
        following = cuts[k + 1] if k + 1 < len(cuts) else len(virtual)
        if _ONLY_SEE.match(virtual[last:cut]) or _ONLY_SEE.match(virtual[cut:following]):
            continue
        kept.append(cut)
        last = cut
    raw_cuts = [to_raw(v) for v in kept]

    groups: list[list[tuple[str, int, int]]] = [[]]
    queue = list(raw_cuts)
    for kind, s, e in item.pieces:
        while queue and kind == "c" and s < queue[0] < e:
            groups[-1].append((kind, s, queue[0]))
            groups.append([])
            s = queue.pop(0)
        if queue and s >= queue[0]:
            groups.append([])
            queue.pop(0)
        groups[-1].append((kind, s, e))
    return [g for g in groups if g]


# --------------------------------------------------------------------------
# 裸数字
# --------------------------------------------------------------------------

_STRUCT_BEFORE = re.compile(r"(?:第|前|后|首|末|近|最近|Top|TOP|top|No\.|NO\.)\s*$")
_STRUCT_AFTER = re.compile(
    r"\s*(?:个(?:方面|原因|问题|建议|要点|维度|阶段|步骤|部分|层面|角度|环节|关键|特点|措施)"
    r"|点(?:建议|原因|看法)|条建议|步)")


def _structural_number(text: str, token: Any) -> bool:
    """「前 3 名」「第 2 季度」「从 3 个方面看」「3 月」：序号和结构，不是结论数字。限 20 以内。"""
    value = token.value
    if token.decimals or token.is_percent or not float(value).is_integer() or not 0 < value <= 20:
        return False
    before, after = text[max(0, token.start - 6):token.start], text[token.end:token.end + 8]
    if _STRUCT_BEFORE.search(before) or _STRUCT_AFTER.match(after):
        return True
    # 行文中间的列举：（1）…（2）…、1、…2、…（行首的「1. 」「2、」出具校验本来就放过）
    if (re.search(r"[（(]\s*$", before) and re.match(r"\s*[)）]", after)) or after.startswith("、"):
        return True
    return bool((value <= 12 and re.match(r"\s*月", after))
                or (value <= 31 and re.match(r"\s*[日号]", after))
                or (value <= 4 and re.match(r"\s*季度", after)))


#: 结构片段涂白时只涂 Markdown 符号本身，数字和字母原样留着：「100. 」前端照样显示成
#: <ol start=100>，「```45678」的语言标签显示在代码块头上——这些数字和正文过同一套规则
#: （1–2 位的行首序号照旧放过，py3 这类标识符照旧不算）。表格竖线涂成一个不是空白的字：
#: 表格第一格里的「1.」不在行首，不能沾行首序号免检的光
_SYNTAX_BLANK = {**{c: " " for c in "`>#*+-~=:"}, "|": "\u00a6"}


def _bare_numbers(markdown: str, masks: list[tuple[int, int]], allow: list[Any] | None,
                  syntax: list[tuple[int, int]] = ()) -> list[Any]:
    """正文里没有出处的数字，再用出具校验同一套规则抽数字。

    - masks：引用渲染出来的字，整段涂白（保留换行，行首序号的免检规则要认得行首）
    - syntax：结构片段，只涂 Markdown 符号（见 _SYNTAX_BLANK），里面的数字照样要查
    """
    chars = list(markdown)
    for s, e in masks:
        for k in range(max(s, 0), min(e, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    for s, e in syntax:
        for k in range(max(s, 0), min(e, len(chars))):
            chars[k] = _SYNTAX_BLANK.get(chars[k], chars[k])
    masked = "".join(chars)
    allowed = number_allowance(allow)
    return [t for t in extract_numbers(masked) if not allowed(t) and not _structural_number(masked, t)]


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - 18):min(len(text), end + 18)].replace("\n", " ")


# --------------------------------------------------------------------------
# 组装文档
# --------------------------------------------------------------------------

#: 粗体：**…** 或 __…__，里面可以有标记（标记里的下划线不算收尾，[[m:refund_rate]] 很常见）
_BOLD = re.compile(r"(\*\*|__)(?P<inner>(?:\[\[" + _MARKER_GUARD + r"[^\[\]\n]*?\]\]|(?!\1)[^\n])+?)\1")
_DIRECTIONAL = re.compile(
    r"增长|增加|上升|提升|提高|上涨|下降|下滑|减少|降低|回落|下跌|反弹|超过|高于|低于|领先|落后|最高|最低"
    r"|最多|最少|来自|导致|因为|由于|带动|拉动|拖累|驱动|原因|归因|贡献|占比|环比|同比|翻倍|持平|波动"
    r"|(?<![A-Za-z])(?:increase|decrease|grow|growth|decline|drop|rise|due to|because|driven)(?![A-Za-z])",
    re.I)


def _normalize_strong(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """包着引用标记的粗体去掉星号，记下哪一段要加粗：**销售额 [[m:gmv]]** → 销售额 [[m:gmv]]。

    星号留在正文里的话，数字一切成单独的片段，两边的文字片段各剩半对 **，前端只能
    原样显示星号。不含标记的粗体原样留着，它整个落在一个文字片段里，前端照常渲染。
    """
    out: list[str] = []
    ranges: list[tuple[int, int]] = []
    last = size = 0
    for m in _BOLD.finditer(raw):
        inner = m.group("inner")
        if not MARKER_RE.search(inner):
            continue
        out.append(raw[last:m.start()])
        size += m.start() - last
        ranges.append((size, size + len(inner)))
        out.append(inner)
        size += len(inner)
        last = m.end()
    out.append(raw[last:])
    return "".join(out), ranges


def _classify(kind: str | None, segments: list[dict[str, Any]], see: list[dict[str, Any]]) -> str:
    """unit 的种类。本期没有裁判，按确定的规则分：引用了证据、写了数字、或者有方向词、
    因果词的算结论；其余的是连接性的话。"""
    if kind:
        return kind
    if see or any(s["kind"] != "text" and s["kind"] != "structural" for s in segments):
        return "claim"
    if any(s["kind"] == "text" and _DIRECTIONAL.search(s["text"]) for s in segments):
        return "claim"
    return "connective"


def compose_doc(
    raw: str,
    catalog: dict[str, Any],
    *,
    node_id: str = "",
    run_id: str | None = None,
    allow_numbers: list[Any] | None = None,
) -> dict[str, Any]:
    """模型写的原文（带标记）→ 报告文档：渲染后的 markdown、块、句、片段、统计、违规。

    stats / violations 由 verify_doc 算出，和出口契约复核用的是同一个函数。
    """
    source = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text, strong = _normalize_strong(source)
    markers = parse_markers(text)
    for marker in markers:
        marker["strong"] = any(a <= marker["start"] and marker["end"] <= b for a, b in strong)

    blocks = _scan_blocks(text, [(m["start"], m["end"]) for m in markers])
    items = [item for block in blocks for item in block.items]
    if not _attach_gaps(items, len(text)):
        # 切块出了漏洞：整篇当一段，宁可版式粗糙，也不能让哪个字落在片段外面没人查
        blocks = [_Block("paragraph", {}, [_Item([("c", 0, len(text))], 0, split=True)])] if text else []
        items = [item for block in blocks for item in block.items]
        _attach_gaps(items, len(text))

    parts: list[str] = []
    size = 0
    out_blocks: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []

    def push(seg: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        nonlocal size
        prev = segments[-1] if segments else None
        if prev is not None and seg["kind"] in ("text", "structural") and prev["kind"] == seg["kind"] \
                and prev.get("strong") == seg.get("strong") and prev["span"][1] == size:
            prev["text"] += seg["text"]
            prev["span"][1] += len(seg["text"])
        else:
            seg["span"] = [size, size + len(seg["text"])]
            segments.append(seg)
        parts.append(seg["text"])
        size += len(seg["text"])

    for block in blocks:
        units: list[dict[str, Any]] = []
        for item in block.items:
            for pieces in _split_sentences(item, text, markers):
                segments: list[dict[str, Any]] = []
                see: list[dict[str, Any]] = []
                anchor = size
                for kind, s, e in pieces:
                    if kind == "s":
                        push({"kind": "structural", "text": text[s:e], "state": "neutral"}, segments)
                        continue
                    def plain(a: int, b: int) -> None:
                        # 文字按粗体的边界再切一刀：一半加粗一半不加粗的不能并成一段
                        cuts = sorted({a, b, *(x for r in strong for x in r if a < x < b)})
                        for x, y in zip(cuts, cuts[1:]):
                            seg = {"kind": "text", "text": text[x:y], "state": "neutral"}
                            if any(p <= x and y <= q for p, q in strong):
                                seg["strong"] = True
                            push(seg, segments)

                    cursor = s
                    for marker in markers:
                        if marker["start"] < s or marker["end"] > e:
                            continue
                        if cursor < marker["start"]:
                            plain(cursor, marker["start"])
                        cursor = marker["end"]
                        if marker["kind"] == "see":
                            see.extend(resolve_support(r, catalog) for r in marker["refs"])
                            continue
                        cite = resolve_marker(marker, catalog)
                        ok = cite["status"] == "resolved"
                        seg = {
                            "kind": _SEG_KIND.get(marker["kind"])
                            or ("number" if marker["kind"] == "m" or (ok and is_number(cite.get("value")))
                                else "value"),
                            "text": cite["rendered"], "ref": f"{marker['kind']}:{marker['body']}",
                            "cite": cite, "state": "deterministic" if ok else "none",
                        }
                        if not ok:
                            seg["issue"] = "unresolved_ref"
                        if marker["strong"]:
                            seg["strong"] = True
                        push(seg, segments)
                    if cursor < e:
                        plain(cursor, e)
                unit: dict[str, Any] = {"kind": item.kind, "segments": segments, "see": see,
                                        "_anchor": anchor}
                if item.loc is not None:
                    unit["loc"] = dict(item.loc)
                if item.depth is not None:
                    unit["depth"] = item.depth
                units.append(unit)
                all_segments.extend(segments)
        out_blocks.append({"type": block.type, **block.meta, "units": units})

    markdown = "".join(parts)
    # 裸数字单独切成片段：前端要在那几个字底下画「无出处」
    refs = [tuple(s["span"]) for s in all_segments if s.get("ref")]
    syntax = [tuple(s["span"]) for s in all_segments if s["kind"] == "structural"]
    bare = _bare_numbers(markdown, refs, allow_numbers, syntax)
    for block in out_blocks:
        for unit in block["units"]:
            unit["segments"] = _split_bare(unit["segments"], bare)

    seg_no = unit_no = 0
    for b_no, block in enumerate(out_blocks):
        block["id"] = f"b{b_no}"
        for unit in block["units"]:
            unit["id"] = f"u{unit_no}"
            unit_no += 1
            for seg in unit["segments"]:
                seg["id"] = f"s{seg_no}"
                seg_no += 1
            body = [s for s in unit["segments"] if s["kind"] != "structural"]
            anchor = unit.pop("_anchor")
            unit["span"] = [body[0]["span"][0], body[-1]["span"][1]] if body else [anchor, anchor]
            unit["kind"] = _classify(unit["kind"], unit["segments"], unit["see"])
            unit["cites"] = list(dict.fromkeys(
                c["alias"] for c in [*(s["cite"] for s in unit["segments"] if s.get("cite")), *unit["see"]]
                if c["status"] == "resolved"))
            # 字段顺序固定，文档内容寻址，同样的输入要得到同样的哈希
            ordered = {k: unit[k] for k in ("id", "kind", "span", "cites", "see", "segments", "loc", "depth")
                       if k in unit}
            unit.clear()
            unit.update(ordered)

    doc: dict[str, Any] = {
        "schema": DOC_SCHEMA, "run_id": run_id, "node_id": node_id,
        "markdown": markdown, "source": source, "catalog": catalog, "blocks": out_blocks,
    }
    checked = verify_doc(doc, catalog, allow_numbers=allow_numbers)
    doc["stats"], doc["violations"] = checked["stats"], checked["violations"]
    return doc


def _split_bare(segments: list[dict[str, Any]], bare: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        s0, e0 = seg["span"]
        hits = [t for t in bare if s0 <= t.start < e0] if seg["kind"] == "text" else []
        if not hits:
            out.append(seg)
            continue
        extra = {"strong": True} if seg.get("strong") else {}

        def piece(a: int, b: int, **fields: Any) -> None:
            out.append({"kind": "text", "text": seg["text"][a - s0:b - s0], "span": [a, b],
                        "state": seg["state"], **extra, **fields})

        cursor = s0
        for token in hits:
            end = min(token.end, e0)
            if cursor < token.start:
                piece(cursor, token.start)
            piece(token.start, end, kind="number", state="none", issue="uncited_number")
            cursor = end
        if cursor < e0:
            piece(cursor, e0)
    return out


# --------------------------------------------------------------------------
# 复核：不信任文档自己的说法，逐项重算
# --------------------------------------------------------------------------

_STRUCTURAL_REST = re.compile(r"^[\s|>#*+\-_:~=.]*$")


def _is_structural(text: str) -> bool:
    """结构片段里只能有 Markdown 语法：标题井号、列表符号和序号、引用号、表格竖线、围栏。

    这里只回答「是不是 Markdown 语法」，不回答「里面的数字要不要出处」——后者交给裸数字
    检查（结构片段只涂符号，数字照查，见 _SYNTAX_BLANK）。所以行首序号不限位数，和切块的
    _ORDERED、前端 Markdown.tsx 的 ORDERED 一致：「100. 」在前端就是 <ol start=100>，把它
    判成夹带内容会变成完整性问题（出口记 gap），而它真正的问题是一个没有出处的数字。
    """
    rest = re.sub(r"`{3,}[A-Za-z0-9_]*", "", text)
    rest = re.sub(r"(?m)^[ \t]*\d+[.)](?=\s)", "", rest)
    return bool(_STRUCTURAL_REST.match(rest))


def iter_units(doc: dict[str, Any]):
    for block in doc.get("blocks") or []:
        for unit in block.get("units") or []:
            yield block, unit


def iter_segments(doc: dict[str, Any]):
    """按正文顺序给出全部片段（含结构片段）。"""
    for _, unit in iter_units(doc):
        yield from unit.get("segments") or []


def find_segment(doc: dict[str, Any], segment_id: str) -> tuple[dict, dict, dict] | None:
    """(block, unit, segment)，找不到返回 None。"""
    for block, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if seg.get("id") == segment_id:
                return block, unit, seg
    return None


def _violation(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **{k: v for k, v in extra.items() if v is not None}}


def verify_doc(
    doc: dict[str, Any],
    catalog: dict[str, Any],
    *,
    allow_numbers: list[Any] | None = None,
) -> dict[str, Any]:
    """逐项复核一份报告文档：{ok, violations, stats}。

    报告节点自查用它（违规就让写作者重写），出口契约复核也用它（判档）——后者传的
    是自己从状态里重建的目录，不用文档里存的那份。复核什么：

    - 片段按顺序拼起来正好是 markdown（没有字落在片段外面）
    - 结构片段里只有 Markdown 语法
    - 每个带引用的片段：按给定目录重新解析、重新渲染，和片段上的字逐字相等，eid 一致
    - [[see:]] 里的每个依据都能解析
    - 引用涂白、结构片段只涂符号之后，正文里剩下的数字全部算裸数字（结构片段里的
      「100. 」「```45678」也在内）
    - 片段标的状态和核对结果一致（界面按状态画线，不能标着「有出处」其实没有）
    """
    violations: list[dict[str, Any]] = []
    stats = {"units": 0, "segments": 0, "claims": 0, "connective": 0, "headings": 0, "numbers": 0,
             "numbers_cited": 0, "values": 0, "uncited_numbers": 0, "unresolved": 0, "see": 0,
             "uncited_claims": 0, "violations": 0}
    if not isinstance(doc, dict):
        violations.append(_violation("bad_schema", "报告文档不是一个对象"))
        return {"ok": False, "violations": violations, "stats": stats}
    if doc.get("schema") != DOC_SCHEMA:
        violations.append(_violation("bad_schema", f"不认识的报告文档格式 {doc.get('schema')!r}"))

    markdown = str(doc.get("markdown") or "")
    masks: list[tuple[int, int]] = []                  # 引用渲染出来的字
    syntax: list[tuple[int, int]] = []                 # 结构片段：只涂符号，数字照查
    owners: list[tuple[int, int, str, str]] = []        # (start, end, segment id, unit id)
    pos, tiled = 0, True
    for _, unit in iter_units(doc):
        stats["units"] += 1
        kind = unit.get("kind")
        if kind in ("claim", "connective"):
            stats["claims" if kind == "claim" else "connective"] += 1
        elif kind == "heading":
            stats["headings"] += 1
        cited: list[str] = []
        for seg in unit.get("segments") or []:
            stats["segments"] += 1
            text, span = str(seg.get("text") or ""), seg.get("span")
            intact = (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) for x in span)
                      and markdown[span[0]:span[1]] == text and span[1] - span[0] == len(text))
            if tiled and (not intact or span[0] != pos):
                tiled = False
                violations.append(_violation("segment_mismatch",
                                             f"片段 {seg.get('id')} 和正文对不上：文档可能被改过",
                                             segment=seg.get("id"), unit=unit.get("id")))
            if intact:
                pos = span[1]
                owners.append((span[0], span[1], seg.get("id"), unit.get("id")))
            where = {"span": list(span) if intact else None, "text": text, "segment": seg.get("id"),
                     "unit": unit.get("id")}
            state, expected = seg.get("state"), None
            found = len(violations)
            if seg.get("kind") == "structural":
                expected = "neutral"
                if intact and _is_structural(text):
                    syntax.append((span[0], span[1]))
                else:
                    violations.append(_violation("structural_text",
                                                 f"结构片段里夹带了内容「{text[:30]}」", **where))
            elif seg.get("ref"):
                ref = str(seg["ref"])
                cite = resolve_ref(ref, catalog)
                ok = cite["status"] == "resolved"
                matches = cite["rendered"] == text
                if intact:
                    masks.append((span[0], span[1]))
                if not ok:
                    violations.append(_violation("unresolved_ref", f"引用 [[{ref}]] 解析不了：{cite['reason']}",
                                                 ref=ref, **where))
                elif not matches:
                    violations.append(_violation(
                        "render_mismatch", f"片段「{text}」和引用 [[{ref}]] 重新渲染出来的「{cite['rendered']}」"
                        "对不上", ref=ref, **where))
                elif (seg.get("cite") or {}).get("eid") not in (None, cite["eid"]):
                    violations.append(_violation("eid_mismatch", f"引用 [[{ref}]] 记的证据标识和目录里的不一致，"
                                                 "文档可能被改过", ref=ref, **where))
                good = ok and matches
                expected = "deterministic" if good else "none"
                if seg.get("kind") == "number":
                    stats["numbers"] += 1
                    stats["numbers_cited"] += int(good)
                elif good:
                    stats["values"] += 1
                if ok:
                    cited.append(cite["alias"])
            elif seg.get("kind") == "number":
                expected = "none"
            elif state == "deterministic":
                expected = "neutral"
            # 已经为这一段报过别的问题，状态不对是它的推论，不再重复
            if expected is not None and state != expected and len(violations) == found:
                violations.append(_violation("state_mismatch",
                                             f"片段「{text[:30]}」标的状态是 {state}，核对下来应该是 {expected}",
                                             **where))
        for support in unit.get("see") or []:
            stats["see"] += 1
            again = resolve_support(str(support.get("ref") or ""), catalog)
            if again["status"] == "resolved":
                cited.append(again["alias"])
            else:
                violations.append(_violation("unresolved_ref",
                                             f"依据 [[see:{again['ref']}]] 解析不了：{again['reason']}",
                                             ref=again["ref"], unit=unit.get("id"),
                                             span=list(unit.get("span") or []) or None))
        if kind == "claim" and not cited:
            stats["uncited_claims"] += 1
    if tiled and pos != len(markdown):
        violations.append(_violation("segment_mismatch", "正文末尾有一段不在任何片段里：文档可能被改过"))

    for token in _bare_numbers(markdown, masks, allow_numbers, syntax):
        owner = next(((sid, uid) for s, e, sid, uid in owners if s <= token.start < e), (None, None))
        violations.append(_violation(
            "uncited_number", f"数字「{token.raw}」没有出处：要写成引用标记（比如 [[m:指标id]]），不能直接写数字",
            span=[token.start, token.end], text=token.raw, context=_context(markdown, token.start, token.end),
            segment=owner[0], unit=owner[1]))

    order = {"bad_schema": 0, "segment_mismatch": 1}
    violations.sort(key=lambda v: (order.get(v["code"], 2), (v.get("span") or [-1])[0]))
    stats["uncited_numbers"] = sum(1 for v in violations if v["code"] == "uncited_number")
    stats["numbers"] += stats["uncited_numbers"]
    stats["unresolved"] = sum(1 for v in violations if v["code"] == "unresolved_ref")
    stats["violations"] = len(violations)
    return {"ok": not violations, "violations": violations, "stats": stats}


# --------------------------------------------------------------------------
# 给报告节点用的文字：证据目录、写作规则、违规清单
# --------------------------------------------------------------------------

MARKER_RULES = """写作规则（系统会逐字核对）：
1. 任何数字都只能用引用标记写：[[m:指标id]]。要换算显示就写 [[m:指标id|万]]，也可以用 |亿、|pct（比率显示成百分数）、|int（取整）、|.1（一位小数）。不要自己写数字，不要计算，不要改写数值——标记会被换成口径卡里的真实数值。
2. 运行输入用 [[i:字段名]]。
3. 每句陈述数据的结论，句末用 [[see:m:指标id,…]] 标出依据；过渡、连接性的话不用标。
4. 日期、ISO 周（2026-W37）、「前 3 名」「第 2 季度」这类序号可以直接写。
5. 目录里没有的指标不要编造引用；标着「没有值」的指标不要写进报告。"""


def catalog_prompt(catalog: dict[str, Any], *, budget: int = 12000) -> str:
    """证据目录的文字版，放进写作者的 prompt。超过预算就截断，并照实说截断了。"""
    lines = ["可引用的证据（写数字只能用下面的引用标记，系统会换成真实数值）："]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for entry in catalog.values():
        if entry.get("kind") == "metric":
            key = (entry.get("caliber") or "", entry.get("version") or "", entry.get("node_id") or "")
            groups.setdefault(key, []).append(entry)
    for (caliber, version, node_id), entries in groups.items():
        title = " @ ".join(v for v in (caliber, version) if v) or node_id
        lines.append(f"\n口径卡「{title}」：")
        for e in entries:
            if e.get("value") is None:
                lines.append(f"- [[{e['alias']}]] {e.get('name')}：这次没有值，不要引用")
            elif e.get("rendered") == MISSING:
                # 有值但按口径卡的格式显示不出来（太小、太大、不是有限的数）：引用了也是解析不了
                lines.append(f"- [[{e['alias']}]] {e.get('name')}：按口径卡的格式显示不出来，不要引用")
            else:
                lines.append(f"- [[{e['alias']}]] {e.get('name')} = {e.get('rendered')}")
    inputs = [e for e in catalog.values() if e.get("kind") == "input"]
    if inputs:
        lines.append("\n运行输入：")
        lines.extend(f"- [[{e['alias']}]] = {e.get('rendered')}" for e in inputs)
    others = [e for e in catalog.values() if e.get("kind") in ("query", "retrieval")]
    if others:
        lines.append("\n查询与检索（本期只能在 [[see:…]] 里当依据，不能直接引用其中的数）：")
        for e in others:
            cols = f"，列 {', '.join(map(str, e['columns'][:12]))}" if e.get("columns") else ""
            lines.append(f"- {e['alias']}：{e.get('label')}{cols}")
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n…（目录太长，后面的省略了）"
    return text


def describe_violations(violations: list[dict[str, Any]], *, limit: int = 20) -> str:
    """违规清单的文字版：给写作者的修复指令、给节点的报错，都用这一份。"""
    lines = []
    for v in violations[:limit]:
        where = f"（…{v['context']}…）" if v.get("context") else ""
        lines.append(f"- {v['message']}{where}")
    if len(violations) > limit:
        lines.append(f"- …另有 {len(violations) - limit} 处")
    return "\n".join(lines)
