"""审批质询：审批人针对待审报告里的一句话提问，一个只读 agent 当场查库作答，问答写进这次运行。

审批卡上只有报告正文和核对概况。审批人对某句话有疑问（「排除测试账号会怎样」「这个数含不含退款」），以前只能驳回重跑，
或者自己去查库。质询让一个只读 agent（engine/verify_agent.py）当场作答，报告本身一个字不改：

1. 开放条件（_open）：运行停在审批上、问的是那张待处理的审批卡——问答落在以后的封存范围里，随运行结束一起封存；或者
   运行已经结束封存、核对一致、是探索运行——问答追加在封存之后（post_seal），是批注不是证据。正式运行封存之后不再追加
   （同按需裁判）；设置里关了「审批质询 · 启用」一律不答。同一次运行同时只答一条：进程内登记（_ANSWERING），登记期间
   这次运行的审批回复、继续运行也不受理（api/runs.py），免得运行往下走了、问答还在路上
2. 回答（_answer）：交给 agent 被问的那句原文、它挂的证据摘录（照 judge.prepare 的办法取，数据源遮罩的列照旧不给，原报告
   的编号标明「原报告」）、审批人选中的原文和问题；工具是这次运行用过的查询工具对应的数据源（同名重建的、没固定过版本的
   上传表格不算，见 _usable_sources）；上限按设置（每次的查询次数、时长，全局每日金额）。回答先给结论，数字只能引用 agent
   自己查询的格子，或者报告里已有的指标、运行输入；回答文档由 compose_doc 组装，裸数字被标出来（提问人自己写的数除外）。
   封存之后的追问查的是数据源现在的数据，任务里给报告生成的时间，回答末尾注明；触顶、查询失败、引用解析不了也在末尾注明
3. 遮罩（_plan）：哪些列要遮照证据面板的规则（查询当时记下的，加上数据源现在设的），另按 SQL 认出改了名字的遮罩列
   （起别名、套函数、经子查询转一道、和 * 写在同一支里，_derived_hidden；认不出落在哪一列的，整份快照遮掉）；快照按列
   遮，再按值遮（别的列里写着遮罩值的格子，_masked_view）。回答正文、查询清单里的遮罩值换成「已遮罩」（_Secrets、
   _scrub_sql）：按值认有门槛；SQL 里的字面量按语法树认用在哪（_literal_uses），数字照原样留着的只有范围比较的边界
   （没把某一个值夹住的）和 LIMIT 的行数，封存的是实际执行的语句；在找某一个值的（等值、IN、简单 CASE……）和别处
   够长的遮罩值都换。已经摆在读者眼前的值不换：问题和选中原文里的、报告列出的指标和运行输入的值；SQL 里照原样留着
   的阈值只在回答正文里不换，格子照旧换。组装之后还认得出遮罩值的，这份回答不交出去
4. 写入（_record）：一条 approval.inquiry 事件，node_id 为空，用量写在数据的 usage 里（计进运行用量，见 runner 的
   _usage_of_events）。质询的查询快照只放在 inquiry_artifacts / queries 里，绝不放在 query_artifact 下：证据接口按那个键
   收集封存的查询，会把质询的查询当成报告的证据。回答期间运行状态变了（被批准、被放弃、继续运行之后重新封存），
   append_events 不写，回答照样交回给提问的人，说明没有写进运行记录（这样的回答在进程里留一阵，_HELD）。触顶、失败
   照样记一条
5. 读取：列表读 approval.inquiry 事件（封存前后的都算，按先后）；质询工件只认质询引用过的（查询快照、回答文档、回答里
   引用的报告口径卡），遮罩照回答时的办法

不发 tool.start、llm.start 这类事件（run_verify_agent 不传 emit）：界面从接口的返回取回答；runner 的 _stale_replay
只看 tool.start，质询不影响恢复后的重放。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.data.catalog import QUERY_SNAPSHOT_KIND
from app.data.provenance_types import DOC_PROVENANCE
from app.db.base import SessionLocal
from app.db.models import Approval, Artifact, DataSource, Run, RunEvent
from app.engine import judge as judging
from app.engine.evidence import MARKER_RE, RenderError, compose_doc, iter_segments, iter_units, render_cell
from app.engine.expressions import numeric_text
from app.engine.issuance import extract_numbers
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
#: 触顶原因：前后端约定的四种。verify_agent 另有 max_steps（模型想了太多轮）：走到那一步时查询次数一定还没用完，
#: 不能说成查询次数到顶，记为 null，回答末尾注明（NOTE_ROUNDS）
LIMITS = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd")
#: 事件、接口里的用量只给这几项（runner 计进运行用量的也是这几项，_INQUIRY_KEYS）
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "calls", "unpriced_calls")
#: 查询清单每条给的几项：没有行，查询结果只在快照里（取的时候按遮罩遮好）
QUERY_KEYS = ("alias", "artifact", "sql", "row_count", "truncated", "source")
#: 回答文档存成工件时的种类；工件引用的 meta 里记下问题的前这么多字，运行的工件列表据此认得出是哪个问题的回答
ANSWER_KIND = "inquiry_answer"
QUESTION_BRIEF = 40
#: 没指明句子时交给 agent 的报告正文最多多少字
REPORT_CHARS = 4000
#: 列给 agent 的报告指标、运行输入最多几条
REFS_MAX = 40
#: 被问的是表格里的一格时，交给 agent 的表格最多几行（不含表头）
TABLE_ROWS = 20
#: 按值认遮罩值的门槛：文字至少这么多字（一个字的「是」「男」换掉，会把不相干的词也换得面目全非）；纯数字至少这么多位
#: （年龄、等级、小额金额这类短的数和阈值、天数、日期、LIMIT 里的数太容易撞上，按值换会把实际执行的 SQL、提问人自己
#: 的条件改得面目全非。它们只在 SQL 里和遮罩列做等值一类比较时才换，见 _scrub_sql；引用到的格子照旧按列遮）
SECRET_MIN = 2
SECRET_DIGITS = 5
#: 遮罩列的值最多收这么多种写法（一份快照最多上千行）：再多就按已经收到的换
SECRETS_MAX = 20_000
#: 没写进运行记录的回答在进程里留多久、最多留几条：这段时间里质询工件接口照样认它引用的工件
HELD_TTL_S = 3600
HELD_MAX = 200
#: 运行的终态。封存之后的追问只在这几种状态上
_TERMINAL = ("succeeded", "failed", "cancelled")
#: 按词边界认值的字符：值的头尾是这样的字时，前后不能紧挨着同类的字（13900000001 不换掉 139000000012 的一截）
_WORD = re.compile(r"[0-9A-Za-z_]")

INQUIRY_SYSTEM = (
    "你在帮人核对一份报告：提问人针对报告里的一句话提了问题，你查库作答。你不写报告，也不改报告。\n"
    "- 问题是「如果……会怎样」（排除测试账号、换个时间范围、只看某个区域）时，写只读查询把它算出来，不要凭印象推测\n"
    "- 回答直截了当：第一句直接给结论（是还是不是、是多少、差多少），再用一两句说依据；不要复述问题，不要客套\n"
    "- 回答里的每个数字都写成引用：你自己查询结果里的一格写 [[v:Qn.rX.列名]]，报告里已有的指标、运行输入照任务里列出的"
    "写法引用；不要直接写数字，没有出处的数字会被标出来\n"
    "- 数据源设了遮罩的列（手机号、姓名这类）：查询里不要选出原值，也不要给它们起别名；要数人数、笔数用 COUNT，回答里需要"
    "指明时说「该客户」「这一笔」，不要写出原值\n"
    "- 查不到、数据里没有判断所需的信息，就照实说查不到，不要编造"
)
INQUIRY_TASK_PROMPT = (
    "报告「{report}」{where}\n\n"
    "{evidence}"
    "{refs}"
    "可以查询的数据源：{sources}\n\n"
    "{since}"
    "{masked}"
    "提问人的问题：\n{question}\n\n"
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
STILL_RUNNING = "运行仍在执行。停在人工审批时，或运行结束并封存后，才能质询"
STILL_RUNNING_FORMAL = "运行仍在执行，停在人工审批时才能质询"
CARD_MOVED = "这张审批卡已处理，运行正在继续执行，请刷新页面查看最新状态"
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
# 没能给出回答的说法：说发生了什么、下一步。核查 agent 的原话（「交结果」「按结构」、裸写的字段名）只进日志
NO_ANSWER = "模型没有给出回答内容，请重新提问"
LEAKED = "回答中含有设了遮罩的值，无法遮住，已不予展示，请换个问法重新提问"
ERR_DAILY = "今日核查花费已达上限（{cap}），未能给出回答，可在设置的「核查」一节调高上限"
ERR_COST = "这次回答的花费已达上限，未能给出回答，可缩小问题范围后重新提问"
ERR_TIMEOUT = "回答超过时长上限{seconds}，未能给出回答，可缩小问题范围后重新提问"
ERR_SHAPE = "模型未能按要求给出回答，请重新提问；如反复出现，请在设置的「核查」一节更换核查模型"
ERR_SILENT = "模型没有给出回复，未能给出回答，请重新提问"
# 回答末尾的系统说明（with_note：另起一段，不挂出处、不查裸数字）
NOTE_LOST = "注：这次运行还用到了数据源{names}，已不存在、已停用或已被重建，未能查询，以上回答不含其中的数据。"
NOTE_ROUNDS = "注：调用模型的轮数已用完，以上回答只依据已经查到的结果，可能不完整。"
NOTE_FAILED = "注：有 {n} 次查询执行失败（查询清单只列成功的查询），以上回答可能缺少依据。"
NOTE_UNRESOLVED = "注：回答中有 {n} 处引用无法解析，相关的数没有出处。"
NOTE_LIVE = "注：以上回答依据的是数据源当前的数据，晚于报告生成的时间；与报告不一致时，可能是报告生成之后数据有更新。"


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


class _Held:
    """没写进运行记录的回答（回答期间运行状态变了）：进程内留 HELD_TTL_S 秒、最多 HELD_MAX 条，先进先出。

    提问人手里那份回答里的数要点得开，质询工件接口在这段时间里照样认它引用的工件（遮罩照旧）。过了时间、或者服务
    重启过，接口照常回 404：回答本身已经交回去了，只是数字不能再下钻。"""

    def __init__(self) -> None:
        self._items: OrderedDict[int, tuple[str, float, dict[str, Any]]] = OrderedDict()
        self._next = 0

    def keep(self, run_id: str, data: dict[str, Any]) -> None:
        self._next += 1
        self._items[self._next] = (run_id, time.monotonic() + HELD_TTL_S, data)
        while len(self._items) > HELD_MAX:
            self._items.popitem(last=False)

    def of(self, run_id: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        for key in [k for k, (_, until, _) in self._items.items() if until <= now]:
            del self._items[key]
        return [data for owner, _, data in self._items.values() if owner == run_id]


_HELD = _Held()


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
    unit = _blank_as_none(body.get("unit"))
    if unit is not None and not isinstance(unit, str):
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
    approval_id = _blank_as_none(body.get("approval_id"))
    if approval_id is not None and not isinstance(approval_id, str):
        raise _invalid("审批卡的 ID 需要是文字；不是在审批卡上提问时留空", "approval_id")
    return Ask(report=report.strip(), unit=unit.strip() if unit else None, quote=quote, question=question,
               approval_id=approval_id.strip() if approval_id else None)


def _blank_as_none(value: Any) -> Any:
    """不指明句子、不是在审批卡上问的：写 null 和写空串是一个意思。别的类型原样交回，由调用方报格式不对。"""
    return None if isinstance(value, str) and not value.strip() else value


# --------------------------------------------------------------------------
# 开放条件
# --------------------------------------------------------------------------

Events = list[tuple[int, str, str | None, Any]]


@dataclass
class _Opening:
    """开放了的这一次质询：往哪种运行里写（mode）、写的时候要核对什么（approval_id / seal），以及被问的报告文档。

    loader 是取证据的函数：封存之后只认封存范围内的工件（同按需裁判，api/evidence._sealed_loader），停在审批时和
    报告节点里的裁判一样直接读工件库。events 是这次运行的事件（封存之后只取封存范围内的）。finished 是运行结束的时间
    （UTC，封存之后的追问才有）：报告是那时生成的，之后数据可能有更新。"""

    run_id: str
    mode: str
    title: str
    versions: Any
    events: Events
    doc: dict[str, Any]
    loader: Callable[[str], Any]
    approval_id: str | None = None
    seal: tuple[int | None, str | None] | None = None
    finished: str | None = None


async def _open(run_id: str, ask: Ask) -> _Opening:
    """这条质询开不开放：开放就交回往哪写、问的是哪份文档；不开放抛 409（输入不对的抛 422、运行不在的抛 404）。"""
    cards: list[str] = []
    rows: Events = []
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise InquiryRefused(404, "运行记录不存在", code=RUN_NOT_FOUND)
        status, run_class, graph, versions = run.status, run.run_class, run.graph or {}, run.data_versions
        finished = _utc(run.finished_at)
        if status == "interrupted":
            cards = [str(a) for a in (await session.execute(
                select(Approval.id).where(Approval.run_id == run_id, Approval.status == "pending"))).scalars()]
            rows = [tuple(r) for r in await session.execute(
                select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
                .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]
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
        # 带着审批卡来的，多半是卡刚被别人批了、运行往下走了：叫人刷新。没带的按运行类别说什么时候能问
        raise _not_open(CARD_MOVED if ask.approval_id is not None
                        else STILL_RUNNING_FORMAL if run_class == "formal" else STILL_RUNNING)
    if ask.approval_id is not None:
        raise _not_open(CARD_DONE)
    if run_class == "formal":
        raise _not_open(FORMAL_SEALED)
    from app.api.evidence import _reports, _sealed, _sealed_loader   # api.evidence 导入了 engine 里的模块

    sealed = await _sealed(run_id)
    if sealed.status not in _TERMINAL:
        # 上面读完状态之后运行被继续了：记录上的封存是上一次结束时的，追加会被下一次封存收进去
        raise _not_open(STILL_RUNNING)
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
                    doc=report.doc, loader=loader, seal=(sealed.run_manifest_seq, sealed.manifest_hash),
                    finished=finished)


def _utc(ts: datetime | None) -> str | None:
    """运行记录上的时间 → UTC 的「2026-10-06T08:12:33」。带时区的换成 UTC，不带的本来就是 UTC。"""
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat(timespec="seconds")


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
    """报告目录里回答可以直接引用的出处：有值的口径卡指标（m:）和运行输入（i:）。它们的编号和 agent 自己的 Q1… 不会
    撞；报告的查询格子不行——agent 的查询也编 Q1，引用 Q1 说不清是哪一份。没有值的引用了也解析不了，不列。"""
    return {a: e for a, e in catalog.items()
            if isinstance(e, dict) and e.get("kind") in ("metric", "input") and e.get("value") not in (None, "")}


def _ref_values(refs: dict[str, dict[str, Any]]) -> list[str]:
    """报告里列出的指标、运行输入的值（原样和渲染出来的写法）：报告里就看得到，碰巧和遮罩列里的某个值一样，回答引用
    它也不算露出遮罩值（_Secrets.protect）。不免的话，引用「单笔目标」这样的指标整份回答都交不出去，换个问法也一样。"""
    out: list[str] = []
    for entry in refs.values():
        out += _forms(entry.get("value"))
        if isinstance(entry.get("rendered"), str):
            out.append(entry["rendered"])
    return out


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


async def _usable_sources(run_id: str, names: Iterable[str], versions: Any) -> dict[str, list[str]]:
    """这次运行用过、现在还能查的数据源 → 它现在设了遮罩的列。

    工具按名字建（核查建工具时只认存在、没有停用的源），名字背后的源却可能换过：运行用过的源被删了、又用同一个名字
    新建一个，查到的已经不是这次运行的数据。所以按名字找到现在的源之后，再核对是不是运行用过的那一个——运行里的查询
    快照在工件引用的 meta 里记着源 id（质询自己的查询不算，它们的 node_id 为空），上传表格还记在运行固定的版本里
    （data_versions）。没固定过版本的上传表格也不交给 agent：建工具时会把当前版本补固定进运行记录（tools/datasource
    的 _pin_late），质询不发事件，已经封存的运行就被悄悄改了。升级前发起的运行（data_versions 为 None）照旧按当前版本。"""
    from app.data.engine import masked_columns

    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    pins = versions if isinstance(versions, dict) else None
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(DataSource.id, DataSource.name, DataSource.origin, DataSource.options)
            .where(DataSource.name.in_(wanted), DataSource.enabled.is_(True)))).all()
        metas = (await session.execute(
            select(Artifact.meta).where(Artifact.run_id == run_id, Artifact.kind == QUERY_SNAPSHOT_KIND,
                                        func.coalesce(Artifact.node_id, "") != ""))).scalars().all()
    used: dict[str, set[str]] = {}
    for meta in metas:
        if isinstance(meta, dict) and meta.get("source") in wanted and meta.get("source_id"):
            used.setdefault(str(meta["source"]), set()).add(str(meta["source_id"]))
    for source_id, entry in (pins or {}).items():
        if isinstance(entry, dict) and entry.get("name") in wanted:
            used.setdefault(str(entry["name"]), set()).add(str(source_id))
    out: dict[str, list[str]] = {}
    for source_id, name, origin, options in rows:
        known = used.get(str(name))
        if known and str(source_id) not in known:
            continue
        if (origin or "manual") == "upload" and pins is not None and source_id not in pins:
            continue
        out[str(name)] = masked_columns(options)
    return out


def _quoted(names: Iterable[str]) -> str:
    return "、".join(f"「{n}」" for n in names)


def _task(op: _Opening, ask: Ask, target: _Target | None, excerpts: list[str], refs: dict[str, dict[str, Any]],
          live: dict[str, list[str]], lost: list[str]) -> str:
    if target is not None:
        cand = target.cand
        where = f"中的这句话{f'（所在小节「{cand.section}」）' if cand.section else ''}：\n「{cand.text}」"
        if target.table:
            where += f"\n\n这句话在一张表格里，表格是（每行一条，单元格用 | 隔开）：\n{target.table}"
    else:
        where = "，提问人没有指明是哪一句"
    if ask.quote:
        where += f"\n\n提问人选中的原文：\n「{ask.quote}」"
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
        sources += f"（这次运行还用到了{_quoted(lost)}，已不存在、已停用或已被重建，无法查询）"
    # 封存之后的追问：报告是运行结束时生成的，现在查到的数据可能已经变了
    since = (f"报告生成于 {op.finished}（UTC），之后数据可能有更新：查询结果和报告对不上时，先说明这一点。\n\n"
             if op.mode == "sealed" and op.finished else "")
    hidden = [f"「{name}」的 {'、'.join(cols)}" for name, cols in live.items() if cols]
    masked = (f"设了遮罩的列（查询里不要选出原值、不要起别名，回答里不要写出这些列的值）：{'；'.join(hidden)}\n\n"
              if hidden else "")
    return INQUIRY_TASK_PROMPT.format(report=op.title, where=where, evidence=evidence, refs=refs_text,
                                      sources=sources, since=since, masked=masked, question=ask.question)


# --------------------------------------------------------------------------
# 遮罩：哪些列
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


def _value_columns(node: Any) -> set[str]:
    """一个选出列的值取自哪些列（列名小写）。COUNT 里的只数个数，窗口的分区键、排序键只决定次序，都带不出值，不算；
    套在里面的子查询只看它选出来的列（它的条件、连接里写的列带不出值）。"""
    from sqlglot import exp

    out: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, exp.Column):
            if not isinstance(item.this, exp.Star) and item.name:
                out.add(item.name.lower())
        elif isinstance(item, exp.Count):
            continue
        elif isinstance(item, exp.Window):
            stack.append(item.this)
        elif isinstance(item, exp.Select):
            stack.extend(item.expressions)
        else:
            stack.extend(item.iter_expressions())
    return out


def _is_star(item: Any) -> bool:
    from sqlglot import exp

    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _branches(root: Any) -> list[list[Any]]:
    """一块查询选出的列：一条 SELECT 一支，UNION 这类的每一支各一支（结果的第 i 列来自每一支的第 i 项）。"""
    from sqlglot import exp

    out: list[list[Any]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            stack.extend((node.expression, node.this))
        elif isinstance(node, exp.Select):
            out.append(list(node.expressions))
    return out


def _output_name(item: Any) -> str | None:
    """一个选出项在结果里的列名（小写）：起了别名的是别名，直接选一列的是列名。别的表达式（函数、运算、CAST、括号、
    子查询）的列名各数据库起法不同（SQLite 照写法原样记「SUBSTR(buyer_phone, 4)」，Postgres 记函数名或 ?column?），
    认不准，返回 None。"""
    from sqlglot import exp

    if isinstance(item, exp.Alias):
        return item.alias.lower() or None
    if isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
        return item.name.lower() or None
    return None


def _places(branch: list[Any], width: int | None) -> list[int | None]:
    """一支里每一项落在结果的第几列。没有 * 的一一对上；有 * 的，第一个 * 前面的从头数，最后一个 * 后面的从结果末尾
    倒着数（width 是结果的列数，不知道就数不了）；两个 * 之间的、* 本身认不出位置（None）。"""
    stars = [i for i, item in enumerate(branch) if _is_star(item)]
    if not stars:
        return list(range(len(branch)))
    first, last = stars[0], stars[-1]
    tail = len(branch) - 1 - last
    # * 至少展开成 0 列：倒着数要是数到了 * 前面那几项的位置上，列数对不上，后面的一项都不认
    countable = width is not None and width - tail >= first
    return [i if i < first else width - (len(branch) - i) if i > last and countable else None
            for i in range(len(branch))]


def _blocks(root: Any) -> list[tuple[Any, str, list[str] | None]]:
    """SQL 里的每一块查询（一条 SELECT，或者 UNION 这类连起来的一串）和它的结果流到哪：
    - "result"：最外层，结果就是快照的列；
    - "source"：当成一张表用的——WITH 里的一段、FROM / JOIN 里的子查询、LATERAL，外层按列名取它的列，* 会把它的列
      全带出去。写了列名表的（t(p)）带上列名表；
    - "other"：标量子查询、IN / EXISTS 里的，值经外层的表达式带出（_value_columns 已经算上），列名无关紧要。
    套在外面的括号（可能好几层）不改变结果流到哪：先越过它们再看。UNION 里的一支不另算，整串按一块。"""
    from sqlglot import exp

    out: list[tuple[Any, str, list[str] | None]] = []
    for node in root.find_all(exp.Select, exp.SetOperation):
        wrappers: list[Any] = []
        outer = node
        while isinstance(outer.parent, exp.Subquery):
            outer = outer.parent
            wrappers.append(outer)
        parent = outer.parent
        if isinstance(parent, exp.SetOperation):
            continue
        if parent is None:
            out.append((node, "result", None))
        elif isinstance(parent, (exp.CTE, exp.Lateral)) or (wrappers and isinstance(parent, (exp.From, exp.Join))):
            # 别名（和列名表）记在 WITH 的那一段、LATERAL 上，FROM 里的子查询记在带别名的那层括号上
            alias = next((h.args["alias"] for h in [parent, *reversed(wrappers)] if h.args.get("alias")), None)
            listed = [c.name.lower() for c in alias.columns] if alias is not None and alias.columns else None
            out.append((node, "source", listed))
        else:
            out.append((node, "other", None))
    return out


def _source_names(top: Any, listed: list[str] | None, names: set[str]) -> tuple[set[str], bool]:
    """派生来源的结果里值取自遮罩列的那几列的名字，以及有没有认不出名字的（没起别名的表达式、两个 * 之间的项）。
    写了列名表的按位置对上列名表；没写的，单独一条 SELECT 取各项自己的列名，UNION 这类按位置对上第一支的列名。"""
    branches = _branches(top)
    found: set[str] = set()
    unknown = False

    def take(name: str | None) -> None:
        nonlocal unknown
        if name is None:
            unknown = True
        else:
            found.add(name)

    if listed is not None:
        for branch in branches:
            for item, place in zip(branch, _places(branch, len(listed))):
                if names & _value_columns(item):
                    take(listed[place] if place is not None and place < len(listed) else None)
        return found, unknown
    first = branches[0] if branches else []
    for item in first:
        if names & _value_columns(item):
            take(_output_name(item))
    if len(branches) > 1:
        # 每一支一样宽：拿一支没有 * 的定宽度，* 后面的项才数得出位置
        width = next((len(b) for b in branches if not any(_is_star(i) for i in b)), None)
        heads = {place: _output_name(item) for item, place in zip(first, _places(first, width)) if place is not None}
        for branch in branches[1:]:
            for item, place in zip(branch, _places(branch, width)):
                if names & _value_columns(item):
                    take(heads.get(place) if place is not None else None)
    return found, unknown


def _tainted(sql: str, kind: str | None, masked: set[str], width: int) -> tuple[set[str], set[int]] | None:
    """SQL 里指代遮罩列的名字（小写），和结果（width 列）里值取自遮罩列的那几列的位置。解析不了返回 None。

    名字从遮罩列本身起，一层层传下去：选出列的值取自这些名字的（buyer_phone AS 手机号、UPPER(buyer_phone) AS x），它的
    名字也算；派生来源（子查询、WITH 里的一段）按位置对上列名表、UNION 第一支的列名，也算，直到不再多出名字。同名
    不同层的也一并算上（宁可多遮）。
    位置是给没起别名的表达式用的（UPPER(buyer_phone) 的结果列名对不上任何名字）：和 * 写在同一支里的，* 前面的从头数、
    后面的从末尾倒着数。认不出的——两个 * 之间的项、派生来源里没起别名的表达式（外层的 * 会把它原样带出来，列名
    又认不准）——整份快照的列都算（同解析不了）。"""
    from sqlglot import exp

    from app.data.sql_semantics import _parse, _read

    try:
        root, _ = _parse(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 解析器的意外按解析不了处理
        root = None
    if root is None:
        return None
    try:
        names = set(masked)
        selects = list(root.find_all(exp.Select))
        blocks = _blocks(root)
        sources = [(top, listed) for top, role, listed in blocks if role == "source"]
        for _ in range(len(selects) + len(sources) + 1):
            before = len(names)
            for select_ in selects:
                for item in select_.expressions:
                    name = item.alias_or_name.lower()
                    if name and name not in names and names & _value_columns(item):
                        names.add(name)
            for top, listed in sources:
                names |= _source_names(top, listed, names)[0]
            if len(names) == before:
                break
        everything = any(_source_names(top, listed, names)[1] for top, listed in sources)
        positions: set[int] = set()
        for top, role, _listed in blocks:
            if role != "result":
                continue
            for branch in _branches(top):
                for item, place in zip(branch, _places(branch, width)):
                    if names & _value_columns(item):
                        if place is None or place >= width:
                            everything = True
                        else:
                            positions.add(place)
        return names, set(range(width)) if everything else positions
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能让回答出错：按解析不了处理（宁可多遮）
        logger.warning("质询的 SQL 遮罩分析失败", exc_info=True)
        return None


def _mentions(sql: str, names: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![0-9A-Za-z_]){re.escape(n)}(?![0-9A-Za-z_])", sql, re.I) for n in names if n)


def _derived_hidden(snapshot: dict[str, Any], hidden: set[str], kind: str | None) -> tuple[set[str], set[str]]:
    """(这份快照要遮的列, SQL 里指代遮罩列的名字)，都是小写。

    证据面板只按列名遮（hidden）；agent 的查询起了别名、套了函数，结果列名就对不上。本仓库的查询惯例是起中文别名
    （region AS 区域），所以按 SQL 认出改了名字的遮罩列（_tainted）。SQL 解析不了、里面又写着遮罩列的，整份快照的
    列都遮掉；合并查询的快照不看 SQL，它记着自己的来历（_hidden_columns 已按 _merge_masks 遮好）。"""
    base = set(hidden)
    sql = snapshot.get("sql")
    if not base or not isinstance(sql, str) or not sql.strip() or snapshot.get("source") == MERGE_SOURCE:
        return base, base
    columns = [str(c) for c in snapshot.get("columns") or []]
    found = _tainted(sql, kind, base, len(columns))
    if found is None:
        return (base | {c.lower() for c in columns}, base) if _mentions(sql, base) else (base, base)
    names, positions = found
    return names | {columns[i].lower() for i in positions if i < len(columns)}, names


# --------------------------------------------------------------------------
# 遮罩：哪些值
# --------------------------------------------------------------------------

#: 一个数的写法：可带正负号、千分位、小数、百分号
_NUMERIC = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
#: 正文里一个个「词」：字母数字下划线连成的一串。头尾都是这种字的遮罩值，只能整个对上这样一串
_WORDS = re.compile(r"[0-9A-Za-z_]+")
#: 正文里的一串数（可带千分位、小数）：带千分位的遮罩值按去掉逗号的样子比
_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: 中文这类遮罩值按开头几个字分桶找（_Secrets._partition）
_PREFIX = 8


def _plain_text(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
    return re.sub(r"\s+", " ", text).strip()


def _numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) or (isinstance(value, str) and numeric_text(value) is not None)


def _forms(value: Any) -> set[str]:
    """一格的值在正文里可能的写法：原样（压成一行），是数的再加上千分位的写法（13,900,000,001）。"""
    text = _plain_text(value)
    forms = {text} if text else set()
    if _numeric(value):
        with contextlib.suppress(RenderError, ValueError, ArithmeticError):
            forms.add(render_cell(value))
    return forms


def _strong(form: str) -> bool:
    """按值认得出的写法（见 SECRET_MIN、SECRET_DIGITS）。"""
    if _NUMERIC.fullmatch(form):
        return sum(c.isdigit() for c in form) >= SECRET_DIGITS
    return len(form) >= SECRET_MIN


def _digits_of(form: str) -> str:
    return form.replace(",", "").lstrip("+-").rstrip("%")


def _pattern(value: str) -> str:
    """一个值的正则：头尾是字母数字的按词边界认；数字还不能是更长的数、日期、时间的一截（12.5 里的 2.5、2026-01-25 里的
    25、12:30 里的 30），也不能是千分位写法里的一组（1,390 里的 390）。逗号隔开的一串长号码（13900000001,13900000002）
    各自照认：千分位一组只有三位。"""
    head = tail = ""
    if _WORD.match(value[0]):
        head = r"(?<![0-9A-Za-z_])"
        if value[0].isdigit():
            head += r"(?<!\d[.:/-])" + (r"(?<!\d,)" if len(re.match(r"\d*", value).group()) == 3 else "")
    if _WORD.match(value[-1]):
        tail = r"(?![0-9A-Za-z_])"
        if value[-1].isdigit():
            tail += r"(?![.:/-]\d)(?!,\d{3}(?!\d))"
    return head + re.escape(value) + tail


@dataclass
class _Secrets:
    """遮罩列里出现过的值。模型看得到原始的查询结果（工具交回的是原样），回答里照着复述、或者把值写进查询条件，
    都会把它带进回答正文和查询清单；按值找出来换成「已遮罩」。这只减少暴露，不是安全边界（同证据面板的遮罩）。

    strong 是按值认得出的写法（够长的数、两个字以上的文字），正文、SQL 里的字面量、名字和注释、别的列里的格子、列名都按
    它换；weak 是遮罩列里的全部写法（含短的数），只用在 SQL 里和遮罩列做等值比较的字面量上；shown 是 SQL 里照原样
    留着的阈值、行数（去掉千分位、正负号的数字），只在回答正文里不换（scrub 的 prose）。换的时候先按词、按开头两个
    字筛出正文里真有的那几个值，再拿它们现编正则：遮罩值上万个时，现编一个上万分支的正则要一秒上下，每次都卡住整个进程。"""

    strong: set[str] = field(default_factory=set)
    weak: set[str] = field(default_factory=set)
    shown: set[str] = field(default_factory=set)
    _index: Any = field(default=None, repr=False, compare=False)
    _compiled: dict[frozenset[str], re.Pattern[str]] = field(default_factory=dict, repr=False, compare=False)

    def add(self, snapshot: Any, hidden: set[str]) -> None:
        if not _query_shaped(snapshot) or not hidden:
            return
        columns = [str(c) for c in snapshot.get("columns") or []]
        index = [i for i, c in enumerate(columns) if c.lower() in hidden]
        for row in snapshot["rows"]:
            if len(self.weak) >= SECRETS_MAX:
                break
            if isinstance(row, list):
                cells = [row[i] for i in index if i < len(row)]
            elif isinstance(row, dict):
                cells = [v for k, v in row.items() if str(k).lower() in hidden]
            else:
                continue
            for value in cells:
                if value is None or isinstance(value, bool):
                    continue
                for form in _forms(value):
                    self.weak.add(form)
                    if _strong(form):
                        self.strong.add(form)
        self._index = None

    def protect(self, texts: Iterable[str | None]) -> None:
        """已经摆在读者眼前的值不换：提问人自己写进问题、选中原文里的（「13900000001 这位客户」「排除 300 以下的」），
        报告里列出的指标、运行输入的值——换了只会让回答、查询对不上问题和报告。正文、格子、SQL 里都不换。"""
        given = " ".join(t for t in texts if t)
        if not given:
            return
        numbers = {_digits_of(n) for n in _DIGITS.findall(given)}

        def asked(form: str) -> bool:
            return _digits_of(form) in numbers if _NUMERIC.fullmatch(form) else form in given

        self.strong = {f for f in self.strong if not asked(f)}
        self.weak = {f for f in self.weak if not asked(f)}
        self._index, self._compiled = None, {}

    def show(self, numbers: Iterable[str]) -> None:
        """SQL 里照原样留着的阈值、行数（_shown_literals）：回答正文里复述它们不换（「金额不低于 30000 元」），换了只会
        让回答和 SQL 对不上。只免正文：格子、列名、SQL 里照旧按值换——格子里的值正好是这个数，可能就是那个人
        （col >= v 再按 col 排序取第一行，查出来的就是 v）。"""
        self.shown = set(numbers)

    def is_shown(self, form: str) -> bool:
        return bool(_NUMERIC.fullmatch(form)) and _digits_of(form) in self.shown

    def hit(self, value: Any) -> bool:
        """一格（或 SQL 里一个字面量）的值是不是按值认得出的遮罩值。短的数不算（见 SECRET_DIGITS）。"""
        if value is None or isinstance(value, bool) or not self.strong:
            return False
        text = _plain_text(value)
        if text in self.strong:
            return True
        if _numeric(value) and sum(c.isdigit() for c in text) >= SECRET_DIGITS:
            with contextlib.suppress(RenderError, ValueError, ArithmeticError):
                return render_cell(value) in self.strong
        return False

    def weak_hit(self, text: str) -> bool:
        """SQL 里一个字面量是不是遮罩列里出现过的值（短的数也算）。"""
        return _plain_text(text) in self.weak

    def scrub(self, text: str, *, prose: bool = False) -> str:
        """text 里的遮罩值换成「已遮罩」。prose：这是回答正文，SQL 里照原样留着的阈值、行数不换（show）。"""
        if not text or not self.strong:
            return text
        pattern = self.pattern(text, prose=prose)
        return pattern.sub(_masked_word(), text) if pattern is not None else text

    def pattern(self, text: str, *, prose: bool = False) -> re.Pattern[str] | None:
        """认 text 里出现的遮罩值的正则；一个都没有时为 None。prose 同 scrub。"""
        found = self._found(text) if text and self.strong else set()
        if prose and self.shown:
            found = {v for v in found if not self.is_shown(v)}
        if not found:
            return None
        key = frozenset(found)
        if key not in self._compiled:
            if len(self._compiled) > 64:
                self._compiled = {}
            # 长的在前：13900000001 先于它的一截被认出来
            self._compiled[key] = re.compile("|".join(_pattern(v) for v in sorted(found, key=lambda v: (-len(v), v))))
        return self._compiled[key]

    def _found(self, text: str) -> set[str]:
        words, numbers, others = self._partition()
        found = {w for w in _WORDS.findall(text) if w in words}
        if numbers:
            for run in _DIGITS.findall(text):
                found |= numbers.get(_digits_of(run), set())
        for i in range(len(text) - 1) if others else ():
            for size, buckets in others.items():
                for length, values in buckets.get(text[i:i + size], {}).items():
                    if (piece := text[i:i + length]) in values:
                        found.add(piece)
        return found

    def prepare(self) -> None:
        """先把索引建好（收完值、去掉提问人写的之后）：上万个值建一次要几十毫秒，放在线程里建，别等到事件循环里第一次换。"""
        self._partition()

    def _partition(self) -> tuple[set[str], dict[str, set[str]], dict[int, dict[str, dict[int, set[str]]]]]:
        """strong 分成三类：只有字母数字的（按词比）、带千分位小数的数（按去掉逗号的数比）、其余的（中文、日期、
        邮箱）。其余的按开头至多 _PREFIX 个字分桶、桶里再按长短分：同一个市区的上万个地址开头七八个字都一样，只按开头
        分的话，正文里每碰到一次那几个字都要逐个比；再按长短分，碰到一次只是按几种长短各查一下。三类各自只在正文里
        真出现时才进正则。"""
        if self._index is None:
            words: set[str] = set()
            numbers: dict[str, set[str]] = {}
            others: dict[int, dict[str, dict[int, set[str]]]] = {}
            for value in self.strong:
                if _WORDS.fullmatch(value):
                    words.add(value)
                elif _NUMERIC.fullmatch(value):
                    numbers.setdefault(_digits_of(value), set()).add(value)
                else:
                    size = min(len(value), _PREFIX)
                    others.setdefault(size, {}).setdefault(value[:size], {}).setdefault(len(value), set()).add(value)
            self._index = (words, numbers, others)
        return self._index


#: 单元格引用的前一截（Q1.r0.）：后面才是列名
_CELL_HEAD = re.compile(r"^\s*[QK]\d+\.r\d+\.")


def _scrubbed_source(text: str, secrets: _Secrets) -> str:
    """回答原文里的遮罩值换成「已遮罩」：标记之外按正文的办法换（SQL 里照原样留着的阈值不换）；标记里的按段换（| 隔开
    的几段各自换），单元格引用只换列名那一截——编号、行号换不出东西，整段换的话「r0.」会让列名开头的长号码看着像小数
    的一截，反而认不出来。列名照快照、SQL 里的名字那样换（阈值也换），换得一样才对得上。
    标记里写着遮罩值的引用换完多半解析不了（列名里写着遮罩值的除外：快照的列名也换过了，正好对上），宁可弄坏一个
    引用，也不让值留在存下的原文、片段里，或者从解析不了的占位里露出来。"""
    if not text or not secrets.strong:
        return text
    out: list[str] = []
    last = 0
    for m in MARKER_RE.finditer(text):
        out.append(secrets.scrub(text[last:m.start()], prose=True))
        start, end = m.span("body")
        parts = text[start:end].split("|")
        head = _CELL_HEAD.match(parts[0]) if m.group("kind").strip() == "v" else None
        first = parts[0][:head.end()] + secrets.scrub(parts[0][head.end():]) if head \
            else secrets.scrub(parts[0], prose=True)
        parts = [first, *(secrets.scrub(p, prose=True) for p in parts[1:])]
        out.append(text[m.start():start] + "|".join(parts) + text[end:m.end()])
        last = m.end()
    out.append(secrets.scrub(text[last:], prose=True))
    return "".join(out)


def _masked_view(snapshot: dict[str, Any], hidden: set[str], secrets: _Secrets) -> tuple[dict[str, Any], list[str]]:
    """一份查询快照遮好的样子和遮掉的列：要遮的列（hidden，小写）每一格都换成「已遮罩」、类型记成文本（渲染时不当数）；
    别的列里写着按值认得出的遮罩值的格子也换（另一张表里没设遮罩的同一个手机号、改了名字没认出来的遮罩列）。
    列名里写着遮罩值的（agent 把遮罩值写进了别名，「13900000001 的单」），列名也换（同 SQL 里的名字，_scrub_sql）。
    原快照不动；没有要遮的返回原快照。"""
    columns = [str(c) for c in snapshot.get("columns") or []]
    index = {i for i, c in enumerate(columns) if c.lower() in hidden}
    by_value = bool(secrets.strong)
    if not index and not by_value:
        return snapshot, []
    word = _masked_word()
    touched = set(index)
    position = {c: i for i, c in enumerate(columns)}
    names = [secrets.scrub(c) for c in columns] if by_value else columns

    def cell(i: int | None, value: Any) -> Any:
        if i in index:
            return word
        if by_value and secrets.hit(value):
            if i is not None:
                touched.add(i)
            return word
        return value

    def key(k: Any) -> Any:
        i = position.get(str(k))
        return names[i] if i is not None else k

    rows: list[Any] = []
    for row in snapshot["rows"]:
        if isinstance(row, list):
            rows.append([cell(i, v) for i, v in enumerate(row)])
        elif isinstance(row, dict):
            rows.append({key(k): word if str(k).lower() in hidden else cell(position.get(str(k)), v)
                         for k, v in row.items()})
        else:
            rows.append(row)
    renamed = names != columns
    if not touched and not renamed:
        return snapshot, []
    types = {key(k): v for k, v in snapshot["column_types"].items()} \
        if isinstance(snapshot.get("column_types"), dict) else {}
    types.update({names[i]: "text" for i in index})
    view = {**snapshot, "rows": rows, "column_types": types, **({"columns": names} if renamed else {})}
    return view, [names[i] for i in sorted(touched)]


#: 按词认在找某一个值的比较（SQL 解析不了时的退路，见 _context）：col = 'x'、col <> 'x'、col LIKE 'x'、SQLite 的
#: col IS 'x'（IN 列表另外认）
_EQUAL_TOKENS = frozenset({"EQ", "NEQ", "NULLSAFE_EQ", "LIKE", "ILIKE", "GLOB", "RLIKE", "IS"})
#: 范围比较（BETWEEN 另外认）
_RANGE_TOKENS = frozenset({"GT", "GTE", "LT", "LTE"})
_NAME_TOKENS = frozenset({"VAR", "IDENTIFIER"})
#: 字面量前面可能连着的：正负号、MySQL 的字符集前缀（_utf8mb4'x'）
_LEADS = frozenset({"DASH", "PLUS", "INTRODUCER"})
#: 能结束一个值的词：减号跟在它们后面是两数相减，不是负号
_VALUE_ENDS = frozenset({"NUMBER", "VAR", "IDENTIFIER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE"})
#: 数字照原样留在 SQL 里的两种用法（_literal_uses）：范围比较的边界、LIMIT 这类的行数
_KEPT = frozenset({"range", "limit"})
#: 字面量写在左边时运算符翻过来：25 < age 就是 age > 25
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">="}
#: 一个纯粹的数（可带正负号、小数）：范围边界按数比大小，写成字符串的也一样
_PLAIN_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _is_literal(kind: str) -> bool:
    """字符串（含 N'…'、E'…'、$$…$$、U&'…' 这些带前缀的变体）和数字。"""
    return kind == "NUMBER" or kind.endswith("STRING")


def _operand_start(kinds: list[str], k: int) -> int:
    """第 k 个字面量连同它前面的负号、字符集前缀从哪个词开始（-25、_utf8mb4'x'）。"""
    while k >= 1 and kinds[k - 1] in _LEADS and (
            kinds[k - 1] == "INTRODUCER" or k < 2 or not (kinds[k - 2] in _VALUE_ENDS or _is_literal(kinds[k - 2]))):
        k -= 1
    return k


def _name_at(tokens: list[Any], kinds: list[str], k: int) -> str | None:
    return tokens[k].text.lower() if 0 <= k < len(kinds) and kinds[k] in _NAME_TOKENS else None


def _context(tokens: list[Any], kinds: list[str], i: int) -> tuple[str, str | None]:
    """按词认第 i 个字面量用在哪，只在 SQL 解析不了时用（_literals），只认得出简单的写法：("equal", 列名) 在找某一个
    值——col = 'x'、col NOT LIKE 'x'、col IS NOT 25、col IN ('x', …)、'x' = col；("range", None) 范围比较的边界——
    col > 25、col BETWEEN a AND b；("other", None) 别的。列名是比较另一边的那个名字（s.buyer_phone 取 buyer_phone，
    小写），另一边不是单独一个名字（函数、算式）时为 None。"""
    j = _operand_start(kinds, i) - 1
    if j >= 1 and kinds[j] == "NOT" and kinds[j - 1] == "IS":
        return "equal", _name_at(tokens, kinds, j - 2)
    if j >= 0 and kinds[j] in _EQUAL_TOKENS:
        return "equal", _name_at(tokens, kinds, j - 2 if j >= 1 and kinds[j - 1] == "NOT" else j - 1)
    if j >= 0 and (kinds[j] in _RANGE_TOKENS or kinds[j] == "BETWEEN"):
        return "range", None
    if j >= 1 and kinds[j] == "AND" and _is_literal(kinds[j - 1]):
        # BETWEEN a AND b 的 b
        lower = _operand_start(kinds, j - 1) - 1
        if lower >= 0 and kinds[lower] == "BETWEEN":
            return "range", None
    # IN 列表：往回越过逗号和别的字面量，找到「IN (」
    k = j
    while k >= 0 and (kinds[k] == "COMMA" or kinds[k] in _LEADS or _is_literal(kinds[k])):
        k -= 1
    if k >= 1 and kinds[k] == "L_PAREN" and kinds[k - 1] == "IN":
        return "equal", _name_at(tokens, kinds, k - 3 if k >= 2 and kinds[k - 2] == "NOT" else k - 2)
    # 字面量在左边：'x' = col、25 < t.col
    after = i + 1
    if after < len(kinds) and kinds[after] in _RANGE_TOKENS:
        return "range", None
    if after < len(kinds) and kinds[after] in _EQUAL_TOKENS:
        last = after + 1
        while last + 2 < len(kinds) and kinds[last + 1] == "DOT" and kinds[last + 2] in _NAME_TOKENS:
            last += 2
        return "equal", _name_at(tokens, kinds, last)
    return "other", None


def _column_of(node: Any, index: int | None) -> str | None:
    """比较另一边的列名（小写、不带表名）；另一边是行值时取第 index 项。不是单独一列（函数、算式、子查询）时为 None。"""
    from sqlglot import exp

    if index is not None and isinstance(node, exp.Tuple):
        items = node.expressions
        node = items[index] if index < len(items) else None
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
        return node.name.lower() or None
    return None


def _use_of(node: Any) -> tuple[str, str | None]:
    """语法树里一个字面量用在哪：(用法, 列名)，用法见 _literal_uses。

    先往上越过不改变「找的是哪个值」的外壳：括号、负号、CAST、字符集前缀、COLLATE；行值、VALUES、ARRAY、ANY / ALL
    也越过，记下「在一组值里」（行值另记下是第几项）。再看外壳的上一层：等值一类、IN、简单 CASE 的 WHEN 是在找这个值；
    范围比较、LIMIT 一类只认单独一个值（一组值里的、算式里的都不算）。"""
    from sqlglot import exp

    child, parent = node, node.parent
    index, grouped = None, False
    while parent is not None:
        if isinstance(parent, exp.Tuple):
            if index is None:
                index = next((k for k, item in enumerate(parent.expressions) if item is child), None)
            grouped = True
        elif isinstance(parent, (exp.Array, exp.Values, exp.Any, exp.All)) or (
                isinstance(parent, exp.Subquery) and isinstance(child, exp.Values)) or (
                isinstance(parent, exp.Anonymous) and str(parent.this).upper() in ("ANY", "ALL", "SOME")):
            # = ANY(ARRAY[…])、= ANY(VALUES …)；Postgres 的 <> ALL(…) 解析成名叫 ALL 的函数
            grouped = True
        elif not _wraps(parent, child):
            break
        child, parent = parent, parent.parent
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like, exp.ILike, exp.Glob,
                           exp.RegexpLike, exp.SimilarTo)):
        return "equal", _column_of(parent.expression if child is parent.this else parent.this, index)
    if isinstance(parent, exp.In):
        return "equal", None if child is parent.this else _column_of(parent.this, index)
    if isinstance(parent, exp.If) and child is parent.this and isinstance(parent.parent, exp.Case) \
            and parent.parent.this is not None:
        # 简单 CASE：CASE buyer_phone WHEN 13900000001 THEN …（CASE WHEN col = v 的写法走上面的等值）
        return "equal", _column_of(parent.parent.this, index)
    if grouped:
        return "other", None
    if type(parent) in _range_ops() and (child is parent.this or child is parent.expression):
        return "range", _column_of(parent.expression if child is parent.this else parent.this, None)
    if isinstance(parent, exp.Between) and child is not parent.this:
        return "range", _column_of(parent.this, None)
    if (isinstance(parent, (exp.Limit, exp.Offset)) and child is parent.expression) or (
            isinstance(parent, exp.Fetch) and child is parent.args.get("count")):
        return "limit", None
    return "other", None


def _wraps(parent: Any, child: Any) -> bool:
    """parent 只是 child 的外壳，不改变它是哪个值：括号、负号、CAST、字符集前缀（_utf8mb4'x'）、COLLATE。"""
    from sqlglot import exp

    return isinstance(parent, (exp.Paren, exp.Neg, exp.Cast)) or (
        isinstance(parent, exp.Introducer) and child is parent.expression) or (
        isinstance(parent, exp.Collate) and child is parent.this)


def _range_ops() -> dict[Any, str]:
    """范围比较的语法树节点 → 「列 运算符 值」方向的运算符。"""
    from sqlglot import exp

    return {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}


def _constant(node: Any) -> Any:
    """写死的值：数按数（几个数加减乘除算出来的也算，13900000002 - 1），字符串按原文（纯粹的数照样按数）；别的为 None。"""
    from sqlglot import exp

    if isinstance(node, exp.Neg):
        value = _constant(node.this)
        return -value if isinstance(value, Decimal) else None
    if isinstance(node, (exp.Paren, exp.Cast, exp.Collate)):
        return _constant(node.this)
    if isinstance(node, exp.Introducer):
        return _constant(node.expression)
    if isinstance(node, _literal_types()):
        text = str(node.this).strip()
        return Decimal(text) if _PLAIN_NUMBER.fullmatch(text) else text
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        a, b = _constant(node.this), _constant(node.expression)
        if not (isinstance(a, Decimal) and isinstance(b, Decimal)):
            return None
        if isinstance(node, exp.Div):
            return a / b if b else None
        return a + b if isinstance(node, exp.Add) else a - b if isinstance(node, exp.Sub) else a * b
    return None


def _bare_literal(node: Any) -> Any:
    """边界本身就是一个字面量（套了括号、负号、CAST 的也算）时交回那个字面量，否则 None。"""
    from sqlglot import exp

    while node is not None and not isinstance(node, _literal_types()):
        if isinstance(node, (exp.Paren, exp.Neg, exp.Cast, exp.Collate)):
            node = node.this
        elif isinstance(node, exp.Introducer):
            node = node.expression
        else:
            return None
    return node


def _bounds_of(node: Any) -> list[tuple[Any, str | None, Any, str]]:
    """一个范围比较里写死的边界：(字面量节点, 列名, 值, 「列 运算符 值」的运算符)。边界是几个数算出来的也算，
    字面量节点记 None：它照样能夹住另一头的值，换的是另一头的字面量。"""
    from sqlglot import exp

    if isinstance(node, exp.Between):
        sides = [(node.args.get("low"), node.this, ">="), (node.args.get("high"), node.this, "<=")]
    elif (op := _range_ops().get(type(node))) is not None:
        sides = [(node.expression, node.this, op), (node.this, node.expression, _FLIP[op])]
    else:
        return []
    out = []
    for operand, other, op in sides:
        value = _constant(operand) if operand is not None else None
        if value is not None:
            out.append((_bare_literal(operand), _column_of(other, None), value, op))
    return out


def _literal_types() -> tuple[Any, ...]:
    """语法树里的字面量：数字、字符串，以及字符串带前缀的几种变体（N'…'、$$…$$、U&'…'、E'…'、X'…'、B'…'）。"""
    from sqlglot import exp

    return (exp.Literal, exp.National, exp.RawString, exp.UnicodeString, exp.ByteString, exp.HexString, exp.BitString)


def _pinned(bounds: list[tuple[Any, str | None, Any, str]]) -> set[int]:
    """范围比较里把某一个值夹住了的边界字面量（交回 id(字面量节点)）。bounds 见 _bounds_of。

    一个值 v 被从下面隔开，是说 v 和比它小一点的值结果不同（col >= v、col < v）；被从上面隔开，是和比它大一点的值结果
    不同（col <= v、col > v）。同一列上 v 两边都被隔开，条件就只认 v 这一个值：col >= v AND col <= v、BETWEEN v AND v、
    col > v OR col < v（就是 col <> v）——这是在找 v，和等值比较一样换。整数的 col < v + 1 就是 col <= v、col > v - 1
    就是 col >= v，所以 col >= v AND col < v + 1 也算夹住了 v。只从一边隔开的是阈值：col >= v 和 col < v 各数一遍，
    是按 v 分成两段，照原样。另一边不是单独一列的（SUM(amount) >= 900）和哪一列都算同一列（宁可多换）。"""
    sides: dict[Any, list[tuple[str, str | None, Any]]] = {}
    for literal, column, value, op in bounds:
        whole = isinstance(value, Decimal) and value == value.to_integral_value()
        marks = {">=": [(value, "below")], "<=": [(value, "above")],
                 ">": [(value, "above"), *([(value + 1, "below")] if whole else [])],
                 "<": [(value, "below"), *([(value - 1, "above")] if whole else [])]}[op]
        for point, side in marks:
            sides.setdefault(point, []).append((side, column, literal))
    out: set[int] = set()
    for marks in sides.values():
        for side, column, literal in marks:
            for other, column2, literal2 in marks:
                if side == "below" and other == "above" and (column is None or column2 is None or column == column2):
                    out |= {id(n) for n in (literal, literal2) if n is not None}
    return out


def _literal_uses(sql: str, kind: str | None) -> dict[int, tuple[str, str | None]] | None:
    """SQL 里每个字面量（按它在原文里的起始位置）用在哪，按语法树认（写法怎么变都认得出）：
    - ("equal", 列名)：在找某一个值——等值、不等、IN（含 VALUES、行值）、LIKE 一类、IS [NOT] DISTINCT FROM、简单 CASE 的
      WHEN、= ANY(ARRAY[…])，套了括号、负号、CAST 的照认；把某一个值夹住的范围（_pinned）也算。列名是被比较的那一列
      （小写、不带表名），另一边不是单独一列时为 None；
    - ("range", 列名)：范围比较（> >= < <=、BETWEEN）的边界，是 agent 定的阈值；
    - ("limit", None)：LIMIT / OFFSET / FETCH / TOP 的行数；
    - ("other", None)：别的——选出列、函数参数、算式、THEN 后面的值、子查询里选出的值。
    解析不了返回 None。"""
    from sqlglot import exp

    from app.data.sql_semantics import _parse, _read

    try:
        root, _ = _parse(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 解析器的意外按解析不了处理
        root = None
    if root is None:
        return None
    # 解析的是去掉头尾空白的 SQL：位置补上开头的空白，才对得上原文里的词
    offset = len(sql) - len(sql.lstrip())
    uses: dict[int, tuple[str, str | None]] = {}
    try:
        literals = {id(n): n for n in root.walk() if isinstance(n, _literal_types())}
        pinned = _pinned([b for n in root.find_all(exp.Between, *_range_ops()) for b in _bounds_of(n)])
        for key, node in literals.items():
            start = (node.meta or {}).get("start")
            if isinstance(start, int):
                use, column = _use_of(node)
                uses[start + offset] = ("equal", column) if key in pinned and use == "range" else (use, column)
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能让回答出错：按解析不了处理（认不准的地方不免）
        logger.warning("质询的 SQL 字面量分析失败", exc_info=True)
        return None
    return uses


def _requote(raw: str, change: Callable[[str], str]) -> str:
    """一个字面量或名字的原文 → 只换里面的字：引号、N'' E'' U&'' 这类前缀、$tag$ 照留；不带引号的整个交给 change。"""
    if raw.startswith("$") and (end := raw.find("$", 1)) > 0:
        tag = raw[:end + 1]
        if len(raw) >= 2 * len(tag) and raw.endswith(tag):
            return tag + change(raw[len(tag):-len(tag)]) + tag
    for opening, closing in (("'", "'"), ('"', '"'), ("`", "`"), ("[", "]")):
        start = raw.find(opening)
        if start >= 0 and len(raw) - start >= 2 and raw.endswith(closing):
            return raw[:start + 1] + change(raw[start + 1:-1]) + closing
    return change(raw)


def _sql_tokens(sql: str, kind: str | None) -> tuple[list[Any], list[str]] | None:
    from app.data.sql_semantics import _read, _tokens

    try:
        tokens = _tokens(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 分词器认不了的写法（引号没闭合这类）
        return None
    return tokens, [t.token_type.name for t in tokens]


def _literals(sql: str, kind: str | None) -> tuple[list[Any], list[str], dict[int, tuple[str, str | None]]] | None:
    """分词，加上每个字面量（按词的下标）用在哪（_literal_uses）。语法树里对不上位置的字面量记 "other"；SQL 解析
    不了时只按词认简单的等值比较（_context），别的一律 "other"——认不准的地方不免。分词出错返回 None。

    照原样留着的数还得在这条 SQL 的字面量里处处都是阈值、行数：同一个数在别的字面量里（等值比较、算式、字符串）也
    出现过，那里会被换掉，这里留着就把它露了（BETWEEN 13900000001 AND 13900000001 + 0），所以这里也改记成在找这个值。
    名字、注释里的不算：那是 agent 给阈值起的说法（AS 超过30000的笔数），照常按值换，留着的阈值不必跟着换。"""
    split = _sql_tokens(sql, kind)
    if split is None:
        return None
    tokens, kinds = split
    uses = _literal_uses(sql, kind)
    found: dict[int, tuple[str, str | None]] = {}
    for i, token in enumerate(tokens):
        if not _is_literal(kinds[i]):
            continue
        if uses is not None:
            found[i] = uses.get(token.start, ("other", None))
        else:
            use, column = _context(tokens, kinds, i)
            found[i] = (use, column) if use == "equal" else ("other", None)
    kept = {i for i, (use, _) in found.items() if kinds[i] == "NUMBER" and use in _KEPT}
    elsewhere = _numbers_in(tokens[i].text for i in found if i not in kept)
    for i in kept:
        if _digits_of(tokens[i].text) in elsewhere:
            found[i] = ("equal", found[i][1])
    return tokens, kinds, found


def _numbers_in(texts: Iterable[str]) -> set[str]:
    """这几段字里写着的数（去掉千分位、正负号）。"""
    return {_digits_of(n) for text in texts for n in _DIGITS.findall(text)}


def _scrub_sql(sql: str, secrets: _Secrets, compared: set[str], kind: str | None) -> str:
    """SQL 里遮罩列的值换成「已遮罩」，别的一个字不动：查询清单随运行封存，它得是实际执行的那条，审批人看 what-if 的
    条件（阈值、日期、LIMIT、别的列的条件）看的就是它。字面量按用在哪换（_literals）：

    - 在找某一个值的（equal）：和遮罩列（或它的别名，compared）比较的，是遮罩列里出现过的值就换，短的数也换
      （age = 25）；和别的列比较的，够长的遮罩值也换（另一张表里没设遮罩的同一个手机号）；
    - 范围比较的边界（age > 31、BETWEEN 10000 AND 50000）、LIMIT / OFFSET 的行数，数字照原样：它们是 agent 自己定的
      阈值，不是在找某一个人，换了封存的就不是实际执行的语句；
    - 别处的数（选出列、函数参数、算式）认不准是不是在找某一个值，够长的遮罩值换；
    - 字符串在哪都按值换（范围边界也一样：写成字符串的长号码做边界，多半是在找那一个人），注释、名字（AS
      "13900000001 的单"、张伟的单数）也按值换（同正文：够长的数、两个字以上的文字）。
    分词出错就整条按正文的办法换；解析不了的认不准哪个数是阈值，范围边界、行数也不免（_literals）。"""
    if not sql or not (secrets.strong or secrets.weak):
        return sql
    split = _literals(sql, kind)
    if split is None:
        return secrets.scrub(sql)
    tokens, kinds, uses = split
    word = _masked_word()
    out: list[str] = []
    last = 0
    for i, token in enumerate(tokens):
        start, end = token.start, token.end + 1
        if start < last or end > len(sql):
            continue
        gap = sql[last:start]
        out.append(secrets.scrub(gap) if gap.strip() else gap)
        raw = sql[start:end]
        if i in uses:
            use, column = uses[i]
            if use == "equal" and ((column in compared and secrets.weak_hit(token.text)) or secrets.hit(token.text)):
                raw = _requote(raw, lambda _: word)
            elif kinds[i] != "NUMBER":
                raw = _requote(raw, secrets.scrub)
            elif use not in _KEPT and secrets.hit(token.text):
                raw = word
        elif kinds[i] in _NAME_TOKENS:
            raw = _requote(raw, secrets.scrub)
        out.append(raw)
        last = end
    rest = sql[last:]
    out.append(secrets.scrub(rest) if rest.strip() else rest)
    return "".join(out)


def _shown_literals(sqls: Iterable[tuple[str, str | None]]) -> set[str]:
    """几条 SQL（和各自数据源的类型）里照原样留着的数，去掉千分位、正负号：范围比较的边界、LIMIT / OFFSET 的行数
    （_scrub_sql 不换的那两种）。它们随 SQL 摆在读者眼前，回答正文里复述（「金额不低于 30000 元」）再遮也遮不住
    什么，只会让回答和 SQL 对不上（_Secrets.show）。同一个数在别的查询里出现在别的字面量里也不算（等值比较、选出列、
    函数参数、字符串；同一条里的 _literals 已经不留）：那里它会被换掉，正文写出来就把那里换掉的值露了。SQL 解析
    不了的一个都不算，里面写着的数也都不算。"""
    kept: set[str] = set()
    elsewhere: set[str] = set()
    for sql, kind in sqls:
        split = _literals(sql, kind) if sql else None
        if split is None:
            elsewhere |= _numbers_in([sql or ""])
            continue
        tokens, kinds, uses = split
        keep = {i for i, (use, _) in uses.items() if kinds[i] == "NUMBER" and use in _KEPT}
        kept |= {_digits_of(tokens[i].text) for i in keep}
        elsewhere |= _numbers_in(tokens[i].text for i in uses if i not in keep)
    return kept - elsewhere


@dataclass
class _Plan:
    """一次回答（或一次取质询工件）的遮罩：agent 每份查询快照遮好的样子（views）和遮掉的列（masked）、SQL 里指代
    遮罩列的名字（tainted）、数据源的类型（kinds，SQL 按它的方言分词）、遮罩列里出现过的值（secrets），以及已经不在的
    数据源（gone，按查询当时记下的遮罩处理）。"""

    views: dict[str, dict[str, Any]]
    masked: dict[str, list[str]]
    tainted: dict[str, set[str]]
    kinds: dict[str, str | None]
    secrets: _Secrets
    gone: dict[str, list[str]]


async def _source_kinds(names: Iterable[str]) -> dict[str, str | None]:
    wanted = sorted({n for n in names if n})
    if not wanted:
        return {}
    async with SessionLocal() as session:
        rows = (await session.execute(select(DataSource.name, DataSource.kind)
                                      .where(DataSource.name.in_(wanted)))).all()
    return {str(name): kind for name, kind in rows}


async def _plan(own: dict[str, dict[str, Any]], others: list[dict[str, Any]], given: list[str | None],
                shown: list[tuple[str, str]] | None = None) -> _Plan:
    """own：agent 自己的查询快照（工件 id → 内容）；others：报告自己的查询快照（被问的那句可能直接写着遮罩列的值，
    agent 会照着复述、写进查询条件）；given：问题、选中的原文、报告里列出的指标和运行输入的值（都已经摆在读者眼前，
    里面的值哪里都不换）；shown：agent 执行的 SQL 和它查的数据源名（照原样留在 SQL 里的阈值、LIMIT，回答正文里
    不换，见 _Secrets.show；取质询工件不用：快照里的格子照旧按值遮）。

    查数据源的遮罩要读库，在这里读；解析 SQL、收上万个值、逐格比对是纯计算，放进线程：放在事件循环里会挡住同一进程
    里别的运行（同 tools/datasource 的 _sql_checks）。"""
    from app.api.evidence import _Masks

    masks = _Masks()
    shown = shown or []
    named = {artifact: await _hidden_columns(snapshot, masks) for artifact, snapshot in own.items()}
    based = [(snapshot, (await _hidden_columns(snapshot, masks))[0]) for snapshot in others]
    kinds = await _source_kinds([*(str(s.get("source") or "") for s in [*own.values(), *others]),
                                 *(name for _, name in shown)])
    return await asyncio.to_thread(_settle_plan, own, named, based, kinds, given, shown)


def _settle_plan(own: dict[str, dict[str, Any]], named: dict[str, tuple[set[str], list[str]]],
                 based: list[tuple[dict[str, Any], set[str]]], kinds: dict[str, str | None],
                 given: list[str | None], shown: list[tuple[str, str]]) -> _Plan:
    secrets = _Secrets()
    hidden: dict[str, set[str]] = {}
    tainted: dict[str, set[str]] = {}
    for artifact, snapshot in own.items():
        hidden[artifact], tainted[artifact] = _derived_hidden(snapshot, named[artifact][0],
                                                              kinds.get(str(snapshot.get("source") or "")))
        secrets.add(snapshot, hidden[artifact])
    for snapshot, base in based:
        secrets.add(snapshot, _derived_hidden(snapshot, base, kinds.get(str(snapshot.get("source") or "")))[0])
    secrets.protect(given)
    secrets.show(_shown_literals((sql, kinds.get(name)) for sql, name in shown))
    secrets.prepare()
    views: dict[str, dict[str, Any]] = {}
    masked: dict[str, list[str]] = {}
    for artifact, snapshot in own.items():
        views[artifact], masked[artifact] = _masked_view(snapshot, hidden[artifact], secrets)
    return _Plan(views=views, masked=masked, tainted=tainted, kinds=kinds, secrets=secrets,
                 gone={artifact: named[artifact][1] for artifact in own})


def _masking_loader(views: dict[str, dict[str, Any]]) -> Callable[[str], Any]:
    """组装回答文档用的 loader：agent 自己的查询快照交遮好的那份（引用到遮罩的格子渲染成「已遮罩」）；别的查询快照
    不交（回答的目录里只有 agent 自己的查询）；口径卡这类没有行的原样，哈希照旧复验。"""

    def load(artifact: str) -> Any:
        if artifact in views:
            return views[artifact]
        content = artifact_store.load(artifact)
        return None if _query_shaped(content) else content

    return load


# --------------------------------------------------------------------------
# 回答
# --------------------------------------------------------------------------


class _Leak(Exception):
    """组装出来的正文里还认得出遮罩值（渲染出来才露的，比如别的列里一格文字夹着手机号）：这份回答不交出去。"""


def _limit(outcome: VerifyOutcome) -> str | None:
    return outcome.limit if outcome.limit in LIMITS else None


def _error_words(outcome: VerifyOutcome, *, timeout_s: Any, daily_max_usd: Any) -> str | None:
    """核查 agent 没交成结果的原话 → 提问人看得懂的话（发生了什么、下一步）。原话里有内部的说法（「交结果」「按结构」、
    裸写的字段名），只进日志；模型不可用、调用失败的说明本来就是给人看的，照用。"""
    error = outcome.error
    if not error:
        return None
    if error.startswith("已达") and error.endswith("没能交结果"):
        if "每日" in error:
            cap = f"核查每日金额上限 {judging._usd(daily_max_usd)}" if daily_max_usd is not None else "核查每日金额上限"
            return ERR_DAILY.format(cap=cap)
        if "时长" in error:
            return _timeout_words(timeout_s)
        return ERR_COST
    if error.startswith("收尾时超过时长上限"):
        return _timeout_words(timeout_s)
    if error.startswith("核查模型没有按结构交结果"):
        return ERR_SHAPE
    if error == "核查模型没有给出回复":
        return ERR_SILENT
    if error.startswith("核查出错（"):
        return f"回答时出错{error[len('核查出错'):]}，请重新提问"
    return error


def _timeout_words(timeout_s: Any) -> str:
    seconds = timeout_s if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool) else None
    return ERR_TIMEOUT.format(seconds=f"（{seconds:g} 秒）" if seconds is not None else "")


def _given_numbers(ask: Ask) -> list[str]:
    """问题、选中的原文里的数：提问人给的阈值、条件（「排除金额低于 500 的」），回答里复述它们不算没有出处的结论。
    查询结果里来的数照旧必须引用。"""
    return [token.raw for text in (ask.question, ask.quote) if text for token in extract_numbers(text)]


async def _answer(op: _Opening, ask: Ask, target: _Target | None, settings: dict[str, Any]) -> dict[str, Any]:
    """问 agent，组装回答文档，交回事件数据里回答的那几项。模型用不了、触顶、组装出错都落在 ok / limit / error 里：
    钱已经花了，回答没成也照样记一条。"""
    from app.api.settings import verify_model_spec

    doc = op.doc
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    masked = await judging.source_masks(catalog, loader=op.loader)
    excerpts = _excerpts(doc, catalog, target.cand, op.loader, masked) if target is not None else []
    refs = _report_refs(catalog)
    wanted = _run_sources(op.events)
    usable = await _usable_sources(op.run_id, wanted, op.versions)
    live, lost = {s: usable[s] for s in wanted if s in usable}, [s for s in wanted if s not in usable]
    part = settings.get("inquiry") if isinstance(settings.get("inquiry"), dict) else {}
    async with SessionLocal() as session:
        spec = await verify_model_spec(session, None, settings=settings)
    outcome = await run_verify_agent(
        spec=spec, system=INQUIRY_SYSTEM, task=_task(op, ask, target, excerpts, refs, live, lost), sources=list(live),
        output_schema=INQUIRY_SCHEMA, max_queries=part.get("max_queries"), timeout_s=part.get("timeout_s"),
        daily_max_usd=settings.get("daily_max_usd"), run_id=op.run_id, data_versions=op.versions)

    # 遮罩：agent 自己的查询快照、报告自己的查询快照（被问的那句可能直接写着遮罩列的值，agent 会照着复述）
    own: dict[str, dict[str, Any]] = {}
    for query in outcome.queries:
        snapshot = _quiet_load(artifact_store.load, query.get("artifact"))
        if _query_shaped(snapshot):
            own[str(query["artifact"])] = snapshot
    others = [snapshot for entry in catalog.values() if isinstance(entry, dict) and entry.get("kind") == "query"
              and _query_shaped(snapshot := _quiet_load(op.loader, entry.get("artifact")))]
    plan = await _plan(own, others, [ask.question, ask.quote, *_ref_values(refs)],
                       [(str(q.get("sql") or ""), str(q.get("source") or "")) for q in outcome.queries])

    text, doc_id, failed = "", None, None
    source = (outcome.rationale or {}).get("source") if outcome.result is not None else None
    if isinstance(source, str) and source.strip():
        try:
            answer = _compose(source, {**refs, **((outcome.rationale or {}).get("catalog") or {})}, run_id=op.run_id,
                              views=plan.views, secrets=plan.secrets, allow=_given_numbers(ask))
        except _Leak:
            logger.warning("质询的回答里有遮罩值遮不住，不交出去（运行 %s）", op.run_id)
            failed = LEAKED
        except Exception as e:  # noqa: BLE001 - 原文没组装成文档就不交出去：里面的引用没换成值，遮罩也没做
            logger.warning("质询的回答组装失败（运行 %s）", op.run_id, exc_info=True)
            failed = f"整理回答时出错（{describe_exception(e)}），请重新提问"
        else:
            answer = _with_notes(answer, op, outcome, part, lost, own)
            text = str(answer.get("markdown") or "")
            doc_id = await _store(answer, op, ask)
    ok = bool(text.strip())
    error = None
    if not ok:
        error = _error_words(outcome, timeout_s=part.get("timeout_s"), daily_max_usd=settings.get("daily_max_usd"))
        if outcome.error:
            logger.info("质询没能给出回答（运行 %s）：%s", op.run_id, outcome.error)
    queries = await asyncio.to_thread(_sealed_queries, outcome.queries, plan, live)
    return {
        "answer_text": text, "answer_doc": doc_id, "ok": ok, "limit": _limit(outcome),
        "error": None if ok else (error or failed or NO_ANSWER),
        # 结果一模一样的两次查询是同一份快照（工件按内容寻址）：引用列表里只记一次
        "inquiry_artifacts": list(dict.fromkeys(q["artifact"] for q in queries if q.get("artifact"))),
        "queries": queries,
        "model": outcome.model, "usage": {k: outcome.usage.get(k, 0) for k in USAGE_KEYS},
    }


def _sealed_queries(queries: list[dict[str, Any]], plan: _Plan, live: dict[str, list[str]]) -> list[dict[str, Any]]:
    """随运行封存的查询清单：每条只留约定的几项，SQL 里的遮罩值换掉。_scrub_sql 要解析 SQL，放在线程里（同 _plan）。"""
    out: list[dict[str, Any]] = []
    for q in queries:
        artifact, name = str(q.get("artifact") or ""), str(q.get("source") or "")
        # 快照没取到的（存不下、被删了），SQL 里按这个源设的遮罩列认比较
        compared = plan.tainted.get(artifact) or {c.lower() for c in live.get(name, [])}
        out.append({**{k: q.get(k) for k in QUERY_KEYS},
                    "sql": _scrub_sql(str(q.get("sql") or ""), plan.secrets, compared, plan.kinds.get(name))})
    return out


def _quiet_load(loader: Callable[[str], Any], artifact: Any) -> Any:
    """取一份快照；取不到、哈希不符都当没有（这里只是为了找遮罩列的值，取不到的那份本来也渲染不出来）。"""
    if not artifact:
        return None
    try:
        return loader(str(artifact))
    except Exception:  # noqa: BLE001
        return None


def _compose(source: str, catalog: dict[str, Any], *, run_id: str, views: dict[str, dict[str, Any]],
             secrets: _Secrets, allow: list[str]) -> dict[str, Any]:
    """agent 写的回答原文（带引用标记）→ 回答文档：目录是 agent 自己的查询，加上报告里的指标和运行输入；快照按遮罩
    遮好再渲染。遮罩值在组装之前换掉（_scrubbed_source）；组装出来还认得出遮罩值（渲染出来才露的，比如别的列里一格
    文字夹着手机号），抛 _Leak，不交出去。allow 是提问人自己写的数（_given_numbers），回答里复述它们不标「无出处」。"""
    loader = _masking_loader(views)
    allowed = list(allow) or None
    # 列名里写着遮罩值的查询：目录里的列名照快照换（_masked_view），整表展开、表头核对、存下的目录都按换过的名字
    catalog = {alias: _renamed_entry(entry, secrets) for alias, entry in catalog.items()}
    doc = compose_doc(_scrubbed_source(source, secrets), catalog, run_id=run_id, loader=loader,
                      allow_numbers=allowed)
    if _leaked(doc, secrets):
        raise _Leak()
    return doc


def _leaked(doc: dict[str, Any], secrets: _Secrets) -> bool:
    """组装出来的正文里还认得出遮罩值。SQL 里照原样留着的阈值写在正文里不算（_Secrets.show），渲染进单元格引用里的
    算：格子已经按值遮过，遮完还认得出，就是一格文字里夹着这个数，渲染出来才露。"""
    markdown = str(doc.get("markdown") or "")
    pattern = secrets.pattern(markdown)
    if pattern is None:
        return False
    cells = [seg["span"] for seg in iter_segments(doc) if str(seg.get("ref") or "").startswith("v:")]
    return any(not secrets.is_shown(m.group()) or any(a < m.end() and m.start() < b for a, b in cells)
               for m in pattern.finditer(markdown))


def _renamed_entry(entry: Any, secrets: _Secrets) -> Any:
    if not (isinstance(entry, dict) and entry.get("kind") == "query" and isinstance(entry.get("columns"), list)):
        return entry
    names = [secrets.scrub(str(c)) for c in entry["columns"]]
    return {**entry, "columns": names} if names != [str(c) for c in entry["columns"]] else entry


def _with_notes(answer: dict[str, Any], op: _Opening, outcome: VerifyOutcome, part: dict[str, Any], lost: list[str],
                own: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """回答末尾的系统说明：用不了的数据源、模型的轮数用完了、有查询执行失败、有引用解析不了、封存之后查的是现在的数据。
    审批人据此知道这份回答哪里不完整，不会看到「回答完成」却满屏 ⟦?…⟧。"""
    notes: list[str] = []
    if lost:
        notes.append(NOTE_LOST.format(names=_quoted(lost)))
    if outcome.limit == "max_steps":
        notes.append(NOTE_ROUNDS)
    cap = part.get("max_queries")
    # 查询次数到顶时次数一定用满了（被拒的、执行失败的也算一次），清单里少的就是失败的那几次
    if outcome.limit == "max_queries" and isinstance(cap, int) and not isinstance(cap, bool) \
            and len(outcome.queries) < cap:
        notes.append(NOTE_FAILED.format(n=cap - len(outcome.queries)))
    unresolved = sum(1 for seg in iter_segments(answer) if seg.get("issue") == "unresolved_ref")
    if unresolved:
        notes.append(NOTE_UNRESOLVED.format(n=unresolved))
    # 上传表格按运行固定的版本查（快照记着 data_version），手工登记的源查的是现在的库
    if op.mode == "sealed" and any(not snapshot.get("data_version") for snapshot in own.values()):
        notes.append(NOTE_LIVE)
    for note in notes:
        answer = with_note(answer, note)
    return answer


async def _store(doc: dict[str, Any], op: _Opening, ask: Ask) -> str | None:
    try:
        return await artifact_store.put_json(doc, kind=ANSWER_KIND, run_id=op.run_id,
                                             meta={"report": ask.report, "unit": ask.unit,
                                                   "question": ask.question[:QUESTION_BRIEF]})
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
    _HELD.keep(run_id, shown)
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


def _cited_cards(doc_id: Any) -> set[str]:
    """回答文档里引用的报告口径卡（m: 条目指向的工件）：界面按 doc.catalog[alias].artifact 取，和查询快照走同一个接口。"""
    doc = _quiet_load(artifact_store.load, doc_id)
    catalog = doc.get("catalog") if isinstance(doc, dict) and isinstance(doc.get("catalog"), dict) else {}
    return {str(e["artifact"]) for e in catalog.values()
            if isinstance(e, dict) and e.get("kind") in ("metric", "input") and e.get("artifact")}


async def _report_material(run_id: str, reports: Iterable[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """这几份报告（报告节点 id）的文档目录里的查询快照，和列出的指标、运行输入的值：和回答时收遮罩值、免遮的是同一批
    （报告最后一次 report.checked 的文档）。"""
    wanted = sorted({str(r) for r in reports if isinstance(r, str) and r})
    if not wanted:
        return [], []
    async with SessionLocal() as session:
        rows = await session.execute(
            select(RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.type == EventType.REPORT_CHECKED, RunEvent.node_id.in_(wanted))
            .order_by(RunEvent.seq))
    docs = {node_id: str(data["doc_artifact"]) for node_id, data in rows
            if isinstance(data, dict) and data.get("doc_artifact")}
    snapshots: list[dict[str, Any]] = []
    values: list[str] = []
    for artifact in docs.values():
        doc = _quiet_load(artifact_store.load, artifact)
        catalog = doc.get("catalog") if isinstance(doc, dict) and isinstance(doc.get("catalog"), dict) else {}
        values += _ref_values(_report_refs(catalog))
        for entry in catalog.values():
            if isinstance(entry, dict) and entry.get("kind") == "query":
                snapshot = _quiet_load(artifact_store.load, entry.get("artifact"))
                if _query_shaped(snapshot):
                    snapshots.append(snapshot)
    return snapshots, values


async def inquiry_artifact(run_id: str, artifact_id: str) -> dict[str, Any]:
    """质询引用过的工件：查询快照（遮罩照回答时的办法，SQL 用事件里记下的那条，两处一字不差）、回答文档（组装时就遮好
    了）、回答里引用的报告口径卡。{id, content, masked[, mask_note]}。

    认的是这次运行的质询事件引用过的，加上没写进运行记录、还留在进程里的回答（_HELD）引用过的；别的一律 404。"""
    from app.api.evidence import SOURCE_GONE

    datas = [data for _, _, data in await _inquiry_events(run_id)] + _HELD.of(run_id)
    referenced = [d for d in datas
                  if artifact_id in (d.get("inquiry_artifacts") or []) or d.get("answer_doc") == artifact_id]
    if not referenced:
        referenced = [d for d in datas if d.get("answer_doc") and artifact_id in _cited_cards(d.get("answer_doc"))]
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
    # 遮罩值照回答时收：引用它的那几条质询各自查到的全部快照（查询条件里写着的值，不一定在这一份的结果里），加上报告
    # 自己的查询快照；免遮的也照回答时：提问人自己写的、报告里列出的指标和运行输入的值。SQL 里照原样留着的阈值只在
    # 回答正文里不遮，格子照旧按值遮，这里不用
    own: dict[str, dict[str, Any]] = {artifact_id: content}
    for data in referenced:
        for other in dict.fromkeys(str(a) for a in data.get("inquiry_artifacts") or []):
            if other not in own and _query_shaped(snapshot := _quiet_load(artifact_store.load, other)):
                own[other] = snapshot
    others, values = await _report_material(run_id, (d.get("report") for d in referenced))
    asked = [t for d in referenced for t in (d.get("question"), d.get("quote")) if isinstance(t, str)]
    plan = await _plan(own, others, [*asked, *values])
    shown = plan.views[artifact_id]
    recorded = next((q["sql"] for d in referenced for q in d.get("queries") or []
                     if isinstance(q, dict) and q.get("artifact") == artifact_id and isinstance(q.get("sql"), str)),
                    None)
    if recorded is not None:
        shown = {**shown, "sql": recorded}
    elif isinstance(shown.get("sql"), str):
        name = str(content.get("source") or "")
        shown = {**shown, "sql": _scrub_sql(shown["sql"], plan.secrets, plan.tainted.get(artifact_id, set()),
                                            plan.kinds.get(name))}
    out: dict[str, Any] = {"id": artifact_id, "content": shown, "masked": plan.masked[artifact_id]}
    if gone := plan.gone.get(artifact_id):
        out["mask_note"] = SOURCE_GONE.format(source="、".join(gone))
    return out


__all__ = [
    "BUSY_CONTINUE", "BUSY_DECIDE", "INQUIRY_BUSY", "INQUIRY_DISABLED", "INQUIRY_NOT_OPEN", "INQUIRY_SCHEMA",
    "INQUIRY_SYSTEM", "NOT_RECORDED", "QUESTION_MAX", "QUOTE_MAX", "Ask", "InquiryRefused", "ask", "inquiry_artifact",
    "inquiry_busy", "inquiry_of", "list_inquiries", "parse_ask",
]
