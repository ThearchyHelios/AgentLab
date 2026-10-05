"""审批质询：审批人针对待审报告里的一句话提问，一个只读 agent 当场查库作答，问答写进这次运行。

审批卡上只有报告正文和核对概况。审批人对某句话有疑问（「排除测试账号会怎样」「这个数含不含退款」），以前只能驳回重跑，
或者自己去查库。质询让一个只读 agent（engine/verify_agent.py）当场作答，报告本身一个字不改：

1. 开放条件（_open）：运行停在审批上、问的是那张待处理的审批卡——问答落在以后的封存范围里，随运行结束一起封存；或者
   运行已经结束封存、核对一致、是探索运行——问答追加在封存之后（post_seal），是批注不是证据。正式运行封存之后不再追加
   （同按需裁判）；设置里关了「审批质询 · 启用」一律不答。同一次运行同时只答一条：进程内登记（_ANSWERING），登记期间
   这次运行的审批回复、继续运行也不受理（api/runs.py），免得运行往下走了、问答还在路上
2. 回答（_answer）：交给 agent 被问的那句原文、它挂的证据摘录（照 judge.prepare 的办法取，数据源遮罩的列照旧不给，原报告
   的编号标明「原报告」）、审批人选中的原文和问题；工具是这次运行用过的查询工具对应的数据源；上限按设置（每次的查询次数、
   时长，全局每日金额）。回答先给结论，数字只能引用 agent 自己查询的格子，或者报告里已有的指标、运行输入；回答文档由
   compose_doc 组装，裸数字被标出来
3. 遮罩：回答里引用到遮罩列的格子渲染成「已遮罩」（按遮罩过的快照组装，_masking_loader）；回答正文、查询清单里出现的
   遮罩列的值换成「已遮罩」（_Secrets）。哪些列要遮照证据面板的规则：查询当时记下的，加上数据源现在设的
4. 写入（_record）：一条 approval.inquiry 事件，node_id 为空，用量写在数据的 usage 里（计进运行用量，见 runner 的
   _usage_of_events）。质询的查询快照只放在 inquiry_artifacts / queries 里，绝不放在 query_artifact 下：证据接口按那个键
   收集封存的查询，会把质询的查询当成报告的证据。回答期间运行状态变了（被批准、被放弃、继续运行之后重新封存），
   append_events 不写，回答照样交回给提问的人，说明没有写进运行记录。触顶、失败照样记一条
5. 读取：列表读 approval.inquiry 事件（封存前后的都算，按先后）；质询工件只认质询事件引用过的，遮罩照证据面板

不发 tool.start、llm.start 这类事件（run_verify_agent 不传 emit）：界面从接口的返回取回答；runner 的 _stale_replay
只看 tool.start，质询不影响恢复后的重放。
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.data.provenance_types import DOC_PROVENANCE
from app.db.base import SessionLocal
from app.db.models import Approval, DataSource, Run, RunEvent
from app.engine import judge as judging
from app.engine.evidence import MARKER_RE, RenderError, compose_doc, iter_units, render_cell
from app.engine.expressions import numeric_text
from app.engine.merge_query import MERGE_SOURCE
from app.engine.nodes.challenge import mark_original, with_note
from app.engine.run_append import append_events
from app.engine.toolcalls import QUERY_PREFIX
from app.engine.verify_agent import RATIONALE, VerifyOutcome, run_verify_agent

logger = logging.getLogger(__name__)

#: 不受理时的机读码（前后端约定）。审批回复、继续运行在质询期间回的也是 INQUIRY_BUSY（api/runs.py）
INQUIRY_NOT_OPEN = "inquiry_not_open"
INQUIRY_BUSY = "inquiry_busy"
INQUIRY_DISABLED = "inquiry_disabled"
RUN_NOT_FOUND = "run_not_found"
ARTIFACT_NOT_FOUND = "inquiry_artifact_not_found"
ARTIFACT_TAMPERED = "snapshot_tampered"
#: 问题、选中的原文最长多少字
QUESTION_MAX = 1000
QUOTE_MAX = 500
#: 回答期间运行状态变了、问答没写进运行记录时给提问的人的说明
NOT_RECORDED = "运行状态已经变化，这次质询的问答没有写入运行记录"
BUSY = "这次运行另有一条质询正在回答，回答完再提问"
#: 质询正在回答时，这次运行的审批回复（批准 / 驳回）、继续运行回的话（api/runs.py）
BUSY_DECIDE = "正在回答质询，回答完再批准或驳回"
BUSY_CONTINUE = "正在回答质询，回答完再继续运行"
#: 触顶原因：前后端约定的四种。verify_agent 另有 max_steps（模型想了太多轮），轮数是按查询次数推出来的，算作查询次数到顶
LIMITS = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd")
#: 事件、接口里的用量只给这几项（runner 计进运行用量的也是这几项，_INQUIRY_KEYS）
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "calls", "unpriced_calls")
#: 查询清单每条给的几项：没有行，查询结果只在快照里（取的时候按遮罩遮好）
QUERY_KEYS = ("alias", "artifact", "sql", "row_count", "truncated", "source")
#: 回答文档存成工件时的种类
ANSWER_KIND = "inquiry_answer"
#: 没指明句子时交给 agent 的报告正文最多多少字
REPORT_CHARS = 4000
#: 列给 agent 的报告指标、运行输入最多几条
REFS_MAX = 40
#: 被问的是表格里的一格时，交给 agent 的表格最多几行（不含表头）
TABLE_ROWS = 20
#: 遮罩列的值短于这么多字就不在正文里换：一个字的值（「是」「男」）换掉，会把不相干的词也换得面目全非
SECRET_MIN = 2
#: 遮罩列的值最多收这么多个（一份快照最多上千行）：再多就按已经收到的换
SECRETS_MAX = 20_000
#: 运行的终态。封存之后的追问只在这几种状态上
_TERMINAL = ("succeeded", "failed", "cancelled")
#: 按词边界认值的字符：值的头尾是这样的字时，前后不能紧挨着同类的字（13900000001 不换掉 139000000012 的一截）
_WORD = re.compile(r"[0-9A-Za-z_]")

INQUIRY_SYSTEM = (
    "你在帮审批人核对一份待批准的报告：审批人针对报告里的一句话提了问题，你查库作答。你不写报告，也不改报告。\n"
    "- 问题是「如果……会怎样」（排除测试账号、换个时间范围、只看某个区域）时，写只读查询把它算出来，不要凭印象推测\n"
    "- 回答直截了当：第一句直接给结论（是还是不是、是多少、差多少），再用一两句说依据；不要复述问题，不要客套\n"
    "- 回答里的每个数字都写成引用：你自己查询结果里的一格写 [[v:Qn.rX.列名]]，报告里已有的指标、运行输入照任务里列出的"
    "写法引用；不要直接写数字，没有出处的数字会被标出来\n"
    "- 查不到、数据里没有判断所需的信息，就照实说查不到，不要编造"
)
INQUIRY_TASK_PROMPT = (
    "报告「{report}」{where}\n\n"
    "{evidence}"
    "{refs}"
    "可以查询的数据源：{sources}\n\n"
    "审批人的问题：\n{question}\n\n"
    "按上面的做法作答，回答写在 rationale 里。"
)
#: 交给核查 agent 的结果结构：只有回答一项（verify_agent 的理由字段，沿用这里写的说明）
INQUIRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {RATIONALE: {
        "type": "string",
        "description": "回答：第一句直接给结论，再用一两句说依据；数字一律写成引用（[[v:Qn.rX.列名]]，"
                       "或任务里列出的报告指标、运行输入）",
    }},
    "required": [RATIONALE],
    "additionalProperties": False,
}

# 不受理的说法
DISABLED_WORDS = "审批质询已关闭（设置中的「{label}」），如需质询，请在设置的「核查」一节开启"
STILL_RUNNING = "运行还在执行，停在人工审批时、或者运行结束封存之后才能质询"
NO_CARD = "运行没有停在人工审批上（可能是服务重启时中断的），没有可以质询的审批"
CARD_GONE = "这张审批卡已处理，或不是这次运行正在等待的审批，请刷新页面查看最新状态"
CARD_DONE = "这张审批卡已处理，运行已经结束，请刷新页面查看最新状态"
FORMAL_SEALED = "正式运行已结束并封存，问答不能再追加进运行记录；封存之后只有探索运行可以继续质询"
UNSEALED = "运行已结束，但还没有封存，暂时不能质询"
BROKEN_SEAL = "封存核对未通过：封存后有事件被修改、删除或插入，报告和证据已不可信，不能再质询"
NEED_CARD = "运行停在人工审批上，需要指明质询的是哪张审批卡"
BAD_BODY = ("请求格式有误：需要一个对象，写明报告（report，报告撰写节点的 ID）和问题（question），"
            "可选句子编号（unit）、选中的原文（quote）、审批卡（approval_id）")
DOC_TAMPERED = "报告文档与哈希不一致，疑似被修改，不能据此质询"
DOC_MISSING = "无法读取报告文档"
NO_ANSWER = "模型没有给出回答内容"


class InquiryRefused(Exception):
    """质询不受理：status 是 HTTP 状态码，message 是给人看的话；409 带机读码 code，422 带出错的那一项 field。"""

    def __init__(self, status: int, message: str, *, code: str | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.code, self.field = status, message, code, field


def _invalid(message: str, name: str | None) -> InquiryRefused:
    return InquiryRefused(422, message, field=name)


def _not_open(message: str) -> InquiryRefused:
    return InquiryRefused(409, message, code=INQUIRY_NOT_OPEN)


# --------------------------------------------------------------------------
# 同一次运行同时只答一条
# --------------------------------------------------------------------------


class _Answering:
    """正在回答质询的运行（进程内登记）。

    只在本进程里有效：另一个进程里的批准拦不住，那时追加按条件更新核对（append_events），写不进就照实说没写进。"""

    def __init__(self) -> None:
        self._runs: set[str] = set()

    def busy(self, run_id: str) -> bool:
        return run_id in self._runs

    @contextlib.contextmanager
    def hold(self, run_id: str) -> Iterator[None]:
        # 查和登记之间没有 await：同一个事件循环里的两条质询不会都看到「没人在答」
        if run_id in self._runs:
            raise InquiryRefused(409, BUSY, code=INQUIRY_BUSY)
        self._runs.add(run_id)
        try:
            yield
        finally:
            self._runs.discard(run_id)


_ANSWERING = _Answering()


def inquiry_busy(run_id: str) -> bool:
    """这次运行是不是正在回答质询（api/runs.py 据此让审批回复、继续运行回 409）。"""
    return _ANSWERING.busy(run_id)


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """一条质询：哪份报告（报告撰写节点的 id）的哪一句（句子单元 id，可以不指明）、选中的原文、问题、问的是哪张审批卡。"""

    report: str
    unit: str | None
    quote: str | None
    question: str
    approval_id: str | None


def parse_ask(body: Any) -> Ask:
    """请求体 → Ask。不合法时抛 422，field 指明是哪一项。"""
    if not isinstance(body, dict):
        raise _invalid(BAD_BODY, None)
    report = body.get("report")
    if not isinstance(report, str) or not report.strip():
        raise _invalid("请指明质询的是哪份报告（报告撰写节点的 ID）", "report")
    unit = body.get("unit")
    if unit is not None and (not isinstance(unit, str) or not unit.strip()):
        raise _invalid("句子编号需要是文字，例如「u12」；不指明句子时留空", "unit")
    quote = body.get("quote")
    if quote is not None and not isinstance(quote, str):
        raise _invalid("选中的原文需要是文字", "quote")
    quote = (quote or "").strip() or None
    if quote is not None and len(quote) > QUOTE_MAX:
        raise _invalid(f"选中的原文最长 {QUOTE_MAX} 字，当前为 {len(quote)} 字，请缩短选区", "quote")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise _invalid("请填写要质询的问题", "question")
    question = question.strip()
    if len(question) > QUESTION_MAX:
        raise _invalid(f"问题最长 {QUESTION_MAX} 字，当前为 {len(question)} 字", "question")
    approval_id = body.get("approval_id")
    if approval_id is not None and (not isinstance(approval_id, str) or not approval_id.strip()):
        raise _invalid("审批卡的 ID 需要是文字；不是在审批卡上提问时留空", "approval_id")
    return Ask(report=report.strip(), unit=unit.strip() if unit else None, quote=quote, question=question,
               approval_id=approval_id.strip() if approval_id else None)


# --------------------------------------------------------------------------
# 开放条件
# --------------------------------------------------------------------------

Events = list[tuple[int, str, str | None, Any]]


@dataclass
class _Opening:
    """开放了的这一次质询：往哪种运行里写（mode）、写的时候要核对什么（approval_id / seal），以及被问的报告文档。

    loader 是取证据的函数：封存之后只认封存范围内的工件（同按需裁判，api/evidence._sealed_loader），停在审批时和
    报告节点里的裁判一样直接读工件库。events 是这次运行的事件（封存之后只取封存范围内的）。"""

    run_id: str
    mode: str
    title: str
    versions: Any
    events: Events
    doc: dict[str, Any]
    loader: Callable[[str], Any]
    approval_id: str | None = None
    seal: tuple[int | None, str | None] | None = None


async def _open(run_id: str, ask: Ask) -> _Opening:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise InquiryRefused(404, "运行记录不存在", code=RUN_NOT_FOUND)
        status, run_class, graph, versions = run.status, run.run_class, run.graph or {}, run.data_versions
        cards = [str(a) for a in (await session.execute(
            select(Approval.id).where(Approval.run_id == run_id, Approval.status == "pending"))).scalars()] \
            if status == "interrupted" else []
        rows: Events = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))] if status == "interrupted" else []
    title = _node_title(graph, ask.report)
    if status == "interrupted":
        if not cards:
            raise _not_open(NO_CARD)
        if ask.approval_id is None:
            raise _invalid(NEED_CARD, "approval_id")
        if ask.approval_id not in cards:
            raise _not_open(CARD_GONE)
        doc = _waiting_doc(rows, run_id, ask.report, title)
        return _Opening(run_id=run_id, mode="interrupted", title=title, versions=versions, events=rows, doc=doc,
                        loader=artifact_store.load, approval_id=ask.approval_id)
    if status not in _TERMINAL:
        raise _not_open(STILL_RUNNING)
    if ask.approval_id is not None:
        raise _not_open(CARD_DONE)
    if run_class == "formal":
        raise _not_open(FORMAL_SEALED)
    from app.api.evidence import _reports, _sealed, _sealed_loader   # api.evidence 导入了 engine 里的模块

    sealed = await _sealed(run_id)
    if not sealed.sealed:
        raise _not_open(UNSEALED)
    if not sealed.trusted:
        raise _not_open(BROKEN_SEAL)
    report = next((r for r in _reports(sealed) if r.node_id == ask.report), None)
    if report is None:
        raise _invalid(_no_report(title), "report")
    if report.hash_ok is False:
        raise _invalid(DOC_TAMPERED, "report")
    if report.doc is None:
        raise _invalid(DOC_MISSING, "report")
    # 带期 4 标记的文档才让摘录取得到哈希链上的清单，和按需裁判同一个口径
    loader = _sealed_loader(sealed, chain=report.doc.get("provenance") == DOC_PROVENANCE)
    return _Opening(run_id=run_id, mode="sealed", title=title, versions=versions, events=list(sealed.events),
                    doc=report.doc, loader=loader, seal=(sealed.run_manifest_seq, sealed.manifest_hash))


def _node_title(graph: dict[str, Any], node_id: str) -> str:
    for n in graph.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            return str((n.get("data") or {}).get("label") or node_id)
    return node_id


def _no_report(title: str) -> str:
    return f"这次运行里没有报告「{title}」的报告文档，请刷新页面后重新选择"


def _waiting_doc(events: Events, run_id: str, report: str, title: str) -> dict[str, Any]:
    """停在审批时被问的报告文档：这个报告节点最后一次 report.checked 里的那份（同证据接口的 _reports）。文档要真是
    这次运行里这个节点写的——内容寻址只保证没被改，不保证没被张冠李戴。"""
    artifact = None
    for _, etype, node_id, data in events:
        if etype == EventType.REPORT_CHECKED and node_id == report and isinstance(data, dict) and data.get("doc_artifact"):
            artifact = str(data["doc_artifact"])
    if artifact is None:
        raise _invalid(_no_report(title), "report")
    try:
        doc = artifact_store.load(artifact)
    except ValueError as e:
        raise _invalid(DOC_TAMPERED, "report") from e
    if not isinstance(doc, dict):
        raise _invalid(DOC_MISSING, "report")
    if doc.get("node_id") != report or doc.get("run_id") != run_id:
        raise _invalid(DOC_TAMPERED, "report")
    return doc


# --------------------------------------------------------------------------
# 被问的那句
# --------------------------------------------------------------------------


@dataclass
class _Target:
    """被问的那句：按裁判的候选句建（judge._candidate，带它挂的依据），表格里的一格另带整张表。"""

    cand: judging.Candidate
    table: str | None = None


def _target(doc: dict[str, Any], unit: str) -> _Target:
    """文档里的这一句。按 judge.candidates 的走法记小节和先后；不是结论句的（表格里的一格、标题）也照样建，
    审批人问的不一定是结论句。没有这一句抛 422。"""
    markdown = str(doc.get("markdown") or "")
    section = ""
    for order, (block, item) in enumerate(iter_units(doc)):
        text = judging._unit_text(item, markdown)
        if block.get("type") == "heading":
            section = text
        if item.get("id") != unit:
            continue
        priority = judging.claim_priority(item, block, markdown) or 3
        cand = judging._candidate(item, priority, order, text, section)
        return _Target(cand, _table_text(block, markdown) if block.get("type") == "table" else None)
    raise _invalid(f"报告里没有这句（{unit}），请刷新页面后重新选择", "unit")


def _table_text(block: dict[str, Any], markdown: str) -> str:
    """表格整张压成几行（表头在前），一格的值离了行列说不清是什么。"""
    rows: dict[int, dict[int, str]] = {}
    for unit in block.get("units") or []:
        loc = unit.get("loc") if isinstance(unit, dict) else None
        if isinstance(loc, dict) and isinstance(loc.get("row"), int) and isinstance(loc.get("col"), int):
            rows.setdefault(loc["row"], {})[loc["col"]] = judging._unit_text(unit, markdown)
    lines = [" | ".join(cells[c] for c in sorted(cells)) for _, cells in sorted(rows.items())]
    kept = lines[:TABLE_ROWS + 1]
    return "\n".join(kept) + ("\n…（后面的行省略了）" if len(lines) > len(kept) else "")


def _excerpts(doc: dict[str, Any], catalog: dict[str, Any], cand: judging.Candidate, loader: Callable[[str], Any],
              masked: dict[str, Any]) -> list[str]:
    """这句挂的每件证据的摘录：和 judge.prepare 一样逐件摘（数据源遮罩的列不给，带期 4 标记的文档多来历行），
    指向原报告编号的写法标上「原报告」——agent 自己的查询也编 Q1、Q2…，不分开的话会把它们混成一回事。"""
    fetch = judging._Fetch(loader)
    provenance_on = doc.get("provenance") == DOC_PROVENANCE
    queries = {a for a, e in catalog.items() if isinstance(e, dict) and e.get("kind") == "query"}
    out: list[str] = []
    for alias in cand.aliases:
        entry = catalog.get(alias)
        if isinstance(entry, dict) and (text := judging._excerpt(alias, entry, [cand], fetch, masked, catalog,
                                                                 provenance_on=provenance_on)):
            out.append(mark_original(text, alias, queries))
    return out


def _report_refs(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """报告目录里回答可以直接引用的出处：口径卡指标（m:）和运行输入（i:）。它们的编号和 agent 自己的 Q1… 不会撞；
    报告的查询格子不行——agent 的查询也编 Q1，引用 Q1 说不清是哪一份。"""
    return {a: e for a, e in catalog.items() if isinstance(e, dict) and e.get("kind") in ("metric", "input")}


def _run_sources(events: Events) -> list[str]:
    """这次运行用过的查询工具对应的数据源，按第一次用到的先后。"""
    out: list[str] = []
    for _, etype, _, data in events:
        if etype not in (EventType.TOOL_START, EventType.TOOL_END) or not isinstance(data, dict):
            continue
        tool = str(data.get("tool") or "")
        name = tool[len(QUERY_PREFIX):] if tool.startswith(QUERY_PREFIX) else ""
        if name and name not in out:
            out.append(name)
    return out


async def _usable_sources(names: Iterable[str]) -> set[str]:
    """现在能查的：存在、没有停用（核查建工具时也只认这样的）。"""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return set()
    async with SessionLocal() as session:
        rows = await session.execute(
            select(DataSource.name).where(DataSource.name.in_(wanted), DataSource.enabled.is_(True)))
    return {str(name) for name in rows.scalars()}


def _quoted(names: Iterable[str]) -> str:
    return "、".join(f"「{n}」" for n in names)


def _task(op: _Opening, ask: Ask, target: _Target | None, excerpts: list[str], refs: dict[str, dict[str, Any]],
          live: list[str], lost: list[str]) -> str:
    if target is not None:
        cand = target.cand
        where = f"中的这句话{f'（所在小节「{cand.section}」）' if cand.section else ''}：\n「{cand.text}」"
        if target.table:
            where += f"\n\n这句话在一张表格里，表格是（每行一条，单元格用 | 隔开）：\n{target.table}"
    else:
        where = "，审批人没有指明是哪一句"
    if ask.quote:
        where += f"\n\n审批人选中的原文：\n「{ask.quote}」"
    if target is None:
        markdown = str(op.doc.get("markdown") or "")
        cut = markdown[:REPORT_CHARS] + ("\n…（后面的省略了）" if len(markdown) > REPORT_CHARS else "")
        where += f"\n\n报告正文：\n{cut}"
    evidence = ""
    if target is not None:
        evidence = ("这句话挂的证据摘录（编号属于原报告；你的查询另起编号，回答里只能引用你自己的查询）：\n"
                    + "\n\n".join(excerpts) + "\n\n") if excerpts else "这句话没有挂依据。\n\n"
    lines = [f"[[{alias}]] {entry.get('label') or alias}" for alias, entry in list(refs.items())[:REFS_MAX]]
    more = f"\n…另有 {len(refs) - REFS_MAX} 项" if len(refs) > REFS_MAX else ""
    refs_text = ("报告里已有的指标和运行输入（回答里可以照这个写法直接引用）：\n" + "\n".join(lines) + more + "\n\n"
                 if lines else "")
    sources = "、".join(live) if live else "（没有可以查询的数据源，只能依据上面的报告内容作答）"
    if lost:
        sources += f"（这次运行还用到了{_quoted(lost)}，已不存在或已停用，无法查询）"
    return INQUIRY_TASK_PROMPT.format(report=op.title, where=where, evidence=evidence, refs=refs_text,
                                      sources=sources, question=ask.question)


# --------------------------------------------------------------------------
# 遮罩
# --------------------------------------------------------------------------


def _query_shaped(content: Any) -> bool:
    return isinstance(content, dict) and isinstance(content.get("rows"), list)


async def _hidden_columns(snapshot: dict[str, Any], masks: Any) -> tuple[set[str], list[str]]:
    """一份查询快照要遮的列（小写）和已经不在的数据源，照证据面板的规则（api/evidence.py 的 _query_step）：查询当时
    记下的，加上数据源现在设的；合并查询按它合并的那几个数据源算。"""
    from app.api.evidence import _merge_masks, _recorded_masks

    if snapshot.get("source") == MERGE_SOURCE:
        return await _merge_masks(snapshot, masks)
    cols, found = await masks.of(snapshot.get("source"))
    return cols | _recorded_masks(snapshot), ([] if found or not snapshot.get("source") else [str(snapshot["source"])])


def _masked_word() -> str:
    from app.api.evidence import MASKED

    return MASKED


def masked_snapshot(snapshot: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """遮罩列的每一格换成「已遮罩」，这几列的类型记成文本（渲染时不当数）。没有要遮的列原样返回。"""
    columns = [str(c) for c in snapshot.get("columns") or []]
    index = {i for i, c in enumerate(columns) if c.lower() in hidden}
    if not index:
        return snapshot
    word = _masked_word()

    def row_of(row: Any) -> Any:
        if isinstance(row, list):
            return [word if i in index else v for i, v in enumerate(row)]
        if isinstance(row, dict):
            return {k: word if str(k).lower() in hidden else v for k, v in row.items()}
        return row

    types = dict(snapshot["column_types"]) if isinstance(snapshot.get("column_types"), dict) else {}
    types.update({columns[i]: "text" for i in index})
    return {**snapshot, "rows": [row_of(r) for r in snapshot["rows"]], "column_types": types}


def _masking_loader(hidden: dict[str, set[str]]) -> Callable[[str], Any]:
    """组装回答文档用的 loader：agent 自己的查询快照按遮罩遮好再交出去，引用到遮罩列的格子就渲染成「已遮罩」。
    别的查询快照不交（回答的目录里只有 agent 自己的查询），表结构快照这类没有行的原样。哈希照旧复验。"""

    def load(artifact: str) -> Any:
        content = artifact_store.load(artifact)
        if not _query_shaped(content):
            return content
        return masked_snapshot(content, hidden[artifact]) if artifact in hidden else None

    return load


def _forms(value: Any) -> set[str]:
    """一格的值在正文里可能的写法：原样（压成一行），是数的再加上千分位的写法（13,900,000,001）。"""
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    forms = {text}
    if isinstance(value, (int, float)) or numeric_text(value) is not None:
        with contextlib.suppress(RenderError, ValueError, ArithmeticError):
            forms.add(render_cell(value))
    return {f for f in forms if len(f) >= SECRET_MIN}


def _pattern(value: str) -> str:
    """一个值的正则：头尾是字母数字的按词边界认，数字还不能是更长的数的一截（1,390 里的 390、12.5 里的 2.5）。"""
    head = tail = ""
    if _WORD.match(value[0]):
        head = r"(?<![0-9A-Za-z_])" + (r"(?<!\d[.,])" if value[0].isdigit() else "")
    if _WORD.match(value[-1]):
        tail = r"(?![0-9A-Za-z_])" + (r"(?![.,]\d)" if value[-1].isdigit() else "")
    return head + re.escape(value) + tail


@dataclass
class _Secrets:
    """遮罩列里出现过的值。模型看得到原始的查询结果（工具交回的是原样），回答里照着复述、或者把值写进查询条件，
    都会把它带进回答正文和查询清单；按值找出来换成「已遮罩」。这只减少暴露，不是安全边界（同证据面板的遮罩）。"""

    values: set[str] = field(default_factory=set)

    def add(self, snapshot: Any, hidden: set[str]) -> None:
        if not _query_shaped(snapshot) or not hidden:
            return
        columns = [str(c) for c in snapshot.get("columns") or []]
        index = [i for i, c in enumerate(columns) if c.lower() in hidden]
        for row in snapshot["rows"]:
            if len(self.values) >= SECRETS_MAX:
                return
            if isinstance(row, list):
                cells = [row[i] for i in index if i < len(row)]
            elif isinstance(row, dict):
                cells = [v for k, v in row.items() if str(k).lower() in hidden]
            else:
                continue
            for value in cells:
                if value is not None and not isinstance(value, bool):
                    self.values |= _forms(value)

    def scrubber(self) -> Callable[[str], str]:
        if not self.values:
            return lambda text: text
        # 长的在前：13900000001 先于它的一截被认出来
        pattern = re.compile("|".join(_pattern(v) for v in sorted(self.values, key=lambda v: (-len(v), v))))
        word = _masked_word()
        return lambda text: pattern.sub(word, text) if text else text


def _outside_markers(text: str, scrub: Callable[[str], str]) -> str:
    """只换引用标记之外的文字：标记里是编号、行号、列名，换了会把一个好好的引用弄坏。"""
    out: list[str] = []
    last = 0
    for m in MARKER_RE.finditer(text):
        out += [scrub(text[last:m.start()]), m.group(0)]
        last = m.end()
    out.append(scrub(text[last:]))
    return "".join(out)


# --------------------------------------------------------------------------
# 回答
# --------------------------------------------------------------------------


def _limit(outcome: VerifyOutcome) -> str | None:
    if outcome.limit in LIMITS:
        return outcome.limit
    return "max_queries" if outcome.limit == "max_steps" else None


async def _answer(op: _Opening, ask: Ask, target: _Target | None, settings: dict[str, Any]) -> dict[str, Any]:
    """问 agent，组装回答文档，交回事件数据里回答的那几项。永远不抛：模型用不了、触顶都落在 ok / limit / error 里。"""
    from app.api.evidence import _Masks
    from app.api.settings import verify_model_spec

    doc = op.doc
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    masked = await judging.source_masks(catalog, loader=op.loader)
    excerpts = _excerpts(doc, catalog, target.cand, op.loader, masked) if target is not None else []
    refs = _report_refs(catalog)
    wanted = _run_sources(op.events)
    usable = await _usable_sources(wanted)
    live, lost = [s for s in wanted if s in usable], [s for s in wanted if s not in usable]
    part = settings.get("inquiry") if isinstance(settings.get("inquiry"), dict) else {}
    async with SessionLocal() as session:
        spec = await verify_model_spec(session, None, settings=settings)
    outcome = await run_verify_agent(
        spec=spec, system=INQUIRY_SYSTEM, task=_task(op, ask, target, excerpts, refs, live, lost), sources=live,
        output_schema=INQUIRY_SCHEMA, max_queries=part.get("max_queries"), timeout_s=part.get("timeout_s"),
        daily_max_usd=settings.get("daily_max_usd"), run_id=op.run_id, data_versions=op.versions)

    # 遮罩：agent 自己的查询快照、报告自己的查询快照（被问的那句可能直接写着遮罩列的值，agent 会照着复述）
    masks = _Masks()
    hidden: dict[str, set[str]] = {}
    secrets = _Secrets()
    for query in outcome.queries:
        snapshot = _quiet_load(artifact_store.load, query.get("artifact"))
        if _query_shaped(snapshot):
            hidden[query["artifact"]] = (await _hidden_columns(snapshot, masks))[0]
            secrets.add(snapshot, hidden[query["artifact"]])
    for entry in catalog.values():
        if isinstance(entry, dict) and entry.get("kind") == "query":
            snapshot = _quiet_load(op.loader, entry.get("artifact"))
            if _query_shaped(snapshot):
                secrets.add(snapshot, (await _hidden_columns(snapshot, masks))[0])
    scrub = secrets.scrubber()

    text, doc_id = "", None
    source = (outcome.rationale or {}).get("source") if outcome.result is not None else None
    if isinstance(source, str) and source.strip():
        answer = _compose(source, {**refs, **((outcome.rationale or {}).get("catalog") or {})}, op.run_id, hidden,
                          scrub)
        if lost:
            answer = with_note(answer, f"注：这次运行还用到了数据源{_quoted(lost)}，已不存在或已停用，未能查询，"
                                       "以上回答不含其中的数据。")
        text = str(answer.get("markdown") or "")
        doc_id = await _store(answer, op, ask)
    ok = bool(text.strip())
    queries = [{**{k: q.get(k) for k in QUERY_KEYS}, "sql": scrub(str(q.get("sql") or ""))} for q in outcome.queries]
    return {
        "answer_text": text, "answer_doc": doc_id, "ok": ok, "limit": _limit(outcome),
        "error": None if ok else (outcome.error or NO_ANSWER),
        "inquiry_artifacts": [q["artifact"] for q in queries if q.get("artifact")], "queries": queries,
        "model": outcome.model, "usage": {k: outcome.usage.get(k, 0) for k in USAGE_KEYS},
    }


def _quiet_load(loader: Callable[[str], Any], artifact: Any) -> Any:
    """取一份快照；取不到、哈希不符都当没有（这里只是为了找遮罩列的值，取不到的那份本来也渲染不出来）。"""
    if not artifact:
        return None
    try:
        return loader(str(artifact))
    except Exception:  # noqa: BLE001
        return None


def _compose(source: str, catalog: dict[str, Any], run_id: str, hidden: dict[str, set[str]],
             scrub: Callable[[str], str]) -> dict[str, Any]:
    """agent 写的回答原文（带引用标记）→ 回答文档：目录是 agent 自己的查询，加上报告里的指标和运行输入；快照按遮罩
    遮好再渲染。遮罩列的值先在标记之外的文字里换掉；换完正文里还认得出遮罩列的值（标记里写了值，解析不了，原样
    出现在正文里），就连标记一起换了再组装一遍——宁可弄坏一个引用，也不让值露出来。"""
    loader = _masking_loader(hidden)
    doc = compose_doc(_outside_markers(source, scrub), catalog, run_id=run_id, loader=loader)
    markdown = str(doc.get("markdown") or "")
    if scrub(markdown) != markdown:
        doc = compose_doc(scrub(source), catalog, run_id=run_id, loader=loader)
    return doc


async def _store(doc: dict[str, Any], op: _Opening, ask: Ask) -> str | None:
    try:
        return await artifact_store.put_json(doc, kind=ANSWER_KIND, run_id=op.run_id,
                                             meta={"report": ask.report, "unit": ask.unit})
    except Exception:  # noqa: BLE001 - 存不下不影响回答本身：正文照常交回，只是数字点不开
        logger.warning("质询的回答文档保存失败（运行 %s）", op.run_id, exc_info=True)
        return None


# --------------------------------------------------------------------------
# 写入与读取
# --------------------------------------------------------------------------


async def _record(op: _Opening, data: dict[str, Any]) -> tuple[Any, str | None]:
    """记一条 approval.inquiry。返回 (写入的事件, 没写进时的说明)。

    停在审批时：那张卡仍待审批、运行仍停着才写（批过了，运行可能已经停到下一张卡上）。封存之后：仍是开始回答时
    的那份封存才写。append_events 返回 None 就是运行在回答期间变了。"""
    items = [(EventType.APPROVAL_INQUIRY, None, data)]
    try:
        if op.mode == "interrupted":
            written = await append_events(op.run_id, items, require="interrupted", approval_id=op.approval_id)
        else:
            written = await append_events(op.run_id, items, require="sealed", seal=op.seal)
    except Exception as e:  # noqa: BLE001 - 写不进运行记录也要把回答交回给提问的人
        logger.warning("质询的问答写入运行记录失败（运行 %s）", op.run_id, exc_info=True)
        return None, f"这次质询的问答没能写入运行记录（{describe_exception(e)}）"
    return (written[0], None) if written else (None, NOT_RECORDED)


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def inquiry_of(data: Any, *, seq: int | None, ts: Any) -> dict[str, Any]:
    """事件数据 → 接口上的 Inquiry 对象（前后端约定的形状）。POST 的返回和 GET 的列表都经这里，两边一样。"""
    data = data if isinstance(data, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "seq": seq, "at": _iso(ts), "actor": data.get("actor"), "report": data.get("report"), "unit": data.get("unit"),
        "quote": data.get("quote"), "question": str(data.get("question") or ""),
        "answer": {"text": str(data.get("answer_text") or ""), "doc_artifact": data.get("answer_doc"),
                   "ok": data.get("ok") is True, "limit": data.get("limit"), "error": data.get("error")},
        "queries": [{k: q.get(k) for k in QUERY_KEYS} for q in data.get("queries") or [] if isinstance(q, dict)],
        "model": data.get("model"),
        "usage": {k: usage.get(k, 0) for k in USAGE_KEYS},
        "post_seal": bool(data.get("post_seal")), "approval_id": data.get("approval_id"),
    }


async def ask(run_id: str, request: Ask, *, actor: str | None = None) -> dict[str, Any]:
    """答一条质询：{inquiry, recorded, note}。不受理时抛 InquiryRefused。

    设置按现在的（不按发起时快照进运行选项的那份）：质询不在图里执行、不参与重放，关掉「审批质询」应当马上生效。"""
    from app.api.settings import VERIFY_SETTING_LABEL, verify_settings

    async with SessionLocal() as session:
        settings = await verify_settings(session)
    part = settings.get("inquiry") if isinstance(settings.get("inquiry"), dict) else {}
    if not part.get("enabled", True):
        raise InquiryRefused(409, DISABLED_WORDS.format(label=VERIFY_SETTING_LABEL["inquiry.enabled"]),
                             code=INQUIRY_DISABLED)
    with _ANSWERING.hold(run_id):
        opening = await _open(run_id, request)
        target = _target(opening.doc, request.unit) if request.unit else None
        data = {"question": request.question, "quote": request.quote, "report": request.report, "unit": request.unit,
                "approval_id": opening.approval_id, "actor": actor,
                **await _answer(opening, request, target, settings)}
        event, note = await _record(opening, data)
    if event is not None:
        return {"inquiry": inquiry_of(event.data, seq=event.seq, ts=event.ts), "recorded": True, "note": None}
    shown = {**data, "post_seal": True} if opening.mode == "sealed" else data
    return {"inquiry": inquiry_of(shown, seq=None, ts=time.time()), "recorded": False, "note": note}


async def _inquiry_events(run_id: str) -> list[tuple[int, float, dict[str, Any]]]:
    async with SessionLocal() as session:
        if await session.get(Run, run_id) is None:
            raise InquiryRefused(404, "运行记录不存在", code=RUN_NOT_FOUND)
        rows = await session.execute(
            select(RunEvent.seq, RunEvent.ts, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.type == EventType.APPROVAL_INQUIRY).order_by(RunEvent.seq))
    return [(seq, ts, data if isinstance(data, dict) else {}) for seq, ts, data in rows]


async def list_inquiries(run_id: str, *, report: str | None = None, unit: str | None = None) -> list[dict[str, Any]]:
    """这次运行的质询（封存前后的都算），按先后；给了 report / unit 只要问那份报告、那一句的。"""
    return [inquiry_of(data, seq=seq, ts=ts) for seq, ts, data in await _inquiry_events(run_id)
            if (not report or data.get("report") == report) and (not unit or data.get("unit") == unit)]


async def inquiry_artifact(run_id: str, artifact_id: str) -> dict[str, Any]:
    """质询引用过的工件：查询快照（遮罩列照证据面板遮好，SQL 里写着的遮罩列的值同样换掉）或回答文档（组装时就遮好了）。
    {id, content, masked[, mask_note]}。不是这次运行的质询引用过的一律 404：工件 id 不能拿来取任意快照。"""
    from app.api.evidence import SOURCE_GONE, _Masks

    referenced = [data for _, _, data in await _inquiry_events(run_id)
                  if artifact_id in (data.get("inquiry_artifacts") or []) or data.get("answer_doc") == artifact_id]
    if not referenced:
        raise InquiryRefused(404, "这次运行的质询没有引用这件工件", code=ARTIFACT_NOT_FOUND)
    try:
        content = artifact_store.load(artifact_id)
    except ValueError as e:
        raise InquiryRefused(409, "工件与哈希不一致，疑似被修改，不予展示", code=ARTIFACT_TAMPERED) from e
    if content is None:
        raise InquiryRefused(404, "无法读取这件工件，可能已被删除", code=ARTIFACT_NOT_FOUND)
    if not _query_shaped(content):
        return {"id": artifact_id, "content": content, "masked": []}
    masks = _Masks()
    hidden, gone = await _hidden_columns(content, masks)
    # 遮罩列的值按引用它的那几条质询各自查到的全部快照找：查询条件里写着的值，不一定在这一份快照的结果里
    secrets = _Secrets()
    for data in referenced:
        for other in dict.fromkeys(str(a) for a in data.get("inquiry_artifacts") or []):
            snapshot = content if other == artifact_id else _quiet_load(artifact_store.load, other)
            if _query_shaped(snapshot):
                secrets.add(snapshot, hidden if other == artifact_id else (await _hidden_columns(snapshot, masks))[0])
    shown = masked_snapshot(content, hidden)
    if isinstance(shown.get("sql"), str):
        shown = {**shown, "sql": secrets.scrubber()(shown["sql"])}
    columns = [str(c) for c in content.get("columns") or []]
    out: dict[str, Any] = {"id": artifact_id, "content": shown, "masked": [c for c in columns if c.lower() in hidden]}
    if gone:
        out["mask_note"] = SOURCE_GONE.format(source="、".join(gone))
    return out


__all__ = [
    "BUSY_CONTINUE", "BUSY_DECIDE", "INQUIRY_BUSY", "INQUIRY_DISABLED", "INQUIRY_NOT_OPEN", "INQUIRY_SCHEMA",
    "INQUIRY_SYSTEM", "NOT_RECORDED", "QUESTION_MAX", "QUOTE_MAX", "Ask", "InquiryRefused", "ask", "inquiry_artifact",
    "inquiry_busy", "inquiry_of", "list_inquiries", "masked_snapshot", "parse_ask",
]
