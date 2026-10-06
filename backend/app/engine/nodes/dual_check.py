"""双盲复算节点：正式出具前，口径卡上输入来自查询的指标，另起一个看不到原 SQL 的只读 agent 独立算一遍再比对。

**按输入复算。** 指标由口径卡的原式算出，原式本身确定、可复算（metrics 的 recompute_ok）；会错的是喂给它的那几个数：
查哪张表、按哪天归属、排没排除退款……所以盲算的单位是「一个来自查询快照的输入」。盲算 agent（engine/verify_agent.py）
只看得到指标名、口径说明（节点配置的 definitions）、这个输入在计算式里是哪一项、本次运行的输入参数和数据源的数据目录，
看不到原 SQL 和原结果；它自己写只读 SQL，最后交一个格子引用，由代码从它自己的查询快照里取值。盲值是拿原式代入盲算的
输入算出来的（engine/recompute.py，取整同口径卡），和原值按相对误差比，原值为 0 时按绝对误差。两个指标用到同一格的
只盲算一次。几个盲算同时进行（BLIND_CONCURRENCY 个），先单独试第一个：核查模型用不了时不必个个去试。

**原流程的取法一点都不透。** 原流程结果的列名（gmv_by_order_date、下单金额）、同一行的维度列名（order_date）都是原流程
的取舍，告诉盲算就等于告诉它按哪一列归属，它照着算出同一个数，下单日和支付日这类陷阱就漏过去了。所以结果分了组时只给
这一行的取值（「华东」、2026-09-02 这一天），按哪一列分组请它按口径说明判断；原 SQL 里写死的文字列（'下单日' AS basis）
连取值也不给；遮罩列不给。计算式只有一项来自查询、或者写了口径说明时不给列名；几项都来自查询又没写口径说明的，列名
里看不出日期、口径的取舍时才给，好让它分得清哪一项是哪一项。

**一致就放行，不一致交给人。** 不一致时逐个对不上的输入说清两条 SQL 差在哪（data/sql_semantics.explain_sql_diff）：
两边先收窄到这一格（只留算出这一列的那一项；分组的按这一行的分组值改成等值条件，HAVING 一并去掉），一条查询算好几列、
分组后取一行时才不会混进「选出的列」「分组」这类和这一格无关的差别。分组的列设了遮罩时不收窄：这一行的分组值写进
SQL，就跟着差异说明进了审批卡、对照工件和目录日志。完整对照落成一件工件（审批 payload 经 runner._safe 会截断，
长 SQL 放不下），用 replay.ask 挂起。审核人逐个指标选采用原流程（a）、采用盲算（b）或两边都不对（neither）：a 没有
缺口；b、neither、没选的写进产出的 issuance_gaps，成果节点的出具契约据此降档（on_reject 为 fail 时节点直接失败）。
两边只差一个方面、又对得上数据目录事实的，选择写进数据目录（engine/dual_check_facts.py，指标自己的筛选范围不写）。
选了会怎样要在卡上看得出：payload 在约定的几项之外带上实际生效的 on_reject，摘要里每个指标带 catalog——选 a、选 b
按发起审批时的目录各会往目录里写什么、不写的为什么（dual_check_facts.preview，和真写同一套判断）；几个指标落到目录
同一项上时每种选择组合合起来会怎样写（preview_joint），放在对照工件的 catalog_joint 里。
盲算触顶、出错、交不出数，或者输入本身没法单独复算的指标按「无法复算」记缺口，发 dual_check_unchecked。运行这一段的
时长上限将至时不再开始新的盲算、开始的按剩余时间收紧时长（engine/deadline.py），没来得及的照此记，运行不被中止。

**产出的状态。** 全部一致是 agreed；没有不一致、但有无法复算的：一个都没核成是 unchecked，核成了一部分是 partial；
有不一致、人裁决过的是 resolved。产出和台账都带 counts（一致、不一致、无法复算各几个）。

**重放。** 审批恢复时节点从头重放。读设置（发起时没快照的才读）、盲算连同比对、差异说明连同工件、写数据目录各在一个
@task 里，重放时直接取 checkpoint 里的结果：不再调用模型、不再计费、不再写目录，事件也都在 task 里发、不会再发一遍；
task 外面的日志经 replay.once 发。task 的先后只取决于这次运行固定下来的东西（图、运行选项、上游的产出）。设置读发起
时的快照（launch_run 写进 run.options["verify"]）：审批停下期间改了设置，恢复后照旧按发起时的。

**不进写作目录。** 盲算的查询发 tool.end（role: verify），随运行封存、证据面板能下钻；台账只记一条 kind: dual_check，
绝不写 kind: query——报告的写作目录按台账里的 query 编号，盲算的查询混进去，写作者就会引用它们。
"""
from __future__ import annotations

import ast
import asyncio
import copy
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.func import task
from sqlalchemy import select

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine import deadline, recompute
from app.engine.approval import read_decision
from app.engine.context import NodeContext, NodeError
from app.engine.dual_check_facts import preview, preview_joint, record_choices
from app.engine.evidence import build_catalog, is_number, next_exec
from app.engine.expressions import ExpressionError, _cell_call, _chain_root, cell_value, column_kind, numeric_text, \
    parse_expression
from app.engine.replay import _in_graph, ask, once
from app.engine.labels import DUAL_CHECK_FIELD_LABEL, DUAL_CHECK_ON_REJECT_LABEL
from app.engine.schema import DUAL_CHECK_ON_REJECT, NodeType, dual_check_card
from app.engine.state import GraphState

#: 节点产出的状态：跳过、全部一致、核成了一部分（其余无法复算）、一个都没核成、人已裁决
SKIPPED, AGREED, PARTIAL, NONE_CHECKED, RESOLVED = "skipped", "agreed", "partial", "unchecked", "resolved"
#: 单个指标的结果：一致、不一致、无法复算
SAME, DIFFERENT, UNCHECKED = "agreed", "disagreed", "unchecked"
#: 审核人的三种选择：采用原流程、采用盲算、两边都不对
CHOICES = ("a", "b", "neither")
#: 盲算触顶的原因里交给界面认的几种（dual_check_limit 日志的 limit）；max_steps 只由核查本身的 verify_limit 日志说
LIMIT_CODES = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd")
#: 输入的出处：口径卡的 cell() 取的格、Agent 经 cite_fields 交来的字段，都落在一份查询快照上
QUERY_VIAS = ("tool_cell", "agent_field")
#: 差异说明里两边的叫法，先原流程后盲算：dual_check_facts 按 a / b 认这个顺序
SIDES = ("原流程", "盲算")
#: 审批卡摘要里每个指标最多列几条差异
SUMMARY_DIFFS = 3
#: 一次 SQL 比较最多等多久：解析是纯计算，放在线程里，超时就给兜底说明
DIFF_TIMEOUT_S = 15.0
#: 交给盲算 agent 的数据目录最多这么多字（表清单、各表的说明分开算），再多就请它自己用 db_schema 查
_INDEX_CHARS = 3_000
_NOTES_CHARS = 6_000
#: 交给盲算 agent 的同一行其他列、运行输入最多几项、每个值最多几个字
_DIMS = 6
_DIM_CHARS = 40
_INPUTS = 12
#: 几个盲算同时进行：串行时几个慢的输入就能把运行这一段的时长上限（max_run_seconds）耗光
BLIND_CONCURRENCY = 3
#: 交给盲算的取值像日期、月份时换个说法：「2026-09-02 这一天」，不说是哪一列
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
#: 原流程结果的列名里出现这些，就带着原流程的取舍（按哪天归属、排没排除、去没去重），不交给盲算
_LOADED_NAME = ("date", "day", "time", "dt", "_at", "month", "week", "year", "paid", "pay", "order", "refund",
                "cancel", "distinct", "dedup", "unique", "net", "gross", "valid", "test", "日", "月", "周", "年",
                "时间", "下单", "支付", "付款", "退款", "去重", "剔除", "排除", "不含", "含", "有效", "净", "毛")
#: 卡上给容器输入记的大概（metrics._brief）：「[12 项]」「{3 个字段}」，写进计算式时不当成字面量
_BRIEF = re.compile(r"^(?:\[\d+ 项\]|\{\d+ 个字段\})$")

SKIP_DISABLED = "双盲复算已在设置中关闭，本节点直接放行"
SKIP_NODE_OFF = "本节点已关闭双盲复算，直接放行"
SKIP_NO_INPUTS = "口径卡上没有输入来自查询的指标，无需双盲复算，直接放行"
#: 两条 SQL 规范化后一致、算出的值却不同：差在数据（两次取数之间数据变了、结果被截断），不在写法
SAME_SQL = "两条 SQL 规范化后一致，差异可能来自两次取数之间数据有变化或取数结果被截断"
SAME_SQL_ASPECT = "data"
DIFF_TIMEOUT = "两条 SQL 比较超时，未能归纳成具体差异，请对照原文"
DIFF_FAILED = "两条 SQL 比较时出错，未能归纳成具体差异，请对照原文"
NO_SQL = "有一边没有可比较的 SQL，请对照原文"
#: 交给成果节点的缺口。不带节点名，也不再说「双盲复算」：出具契约统一加「双盲复算「节点名」：」（没改名的节点只加
#: 「双盲复算：」），正文再说一遍就成了「双盲复算：指标「X」双盲复算不一致……」
GAP_BLIND = "指标「{name}」两路结果不一致，审核人选择了盲算结果"
GAP_NEITHER = "指标「{name}」两路结果不一致，审核人判断两边都不对"
GAP_UNANSWERED = "指标「{name}」两路结果不一致，审核人没有作出选择"
GAP_UNCHECKED = "指标「{name}」无法复算：{reason}"
#: 运行时长上限将至（engine/deadline.py）：没来得及开始的盲算、时长按剩余时间收紧后到了点的盲算，都按无法复算记
LATE = "本次运行即将达到 {seconds:g} 秒的时长上限，为保证运行按时完成，未进行盲算"
SQUEEZED = "本次运行即将达到 {seconds:g} 秒的时长上限，盲算时长已按剩余时间收紧，到时未能完成"

BLIND_SYSTEM = (
    "你在做双盲复算：另一条流程已经算出了口径卡上的一个指标，你要在看不到它的 SQL 和结果的情况下，按业务定义独立算出"
    "计算式里的一个数，交给人和原流程比对。\n"
    "- 业务含义只按下面给的指标名称、口径说明和数据目录理解。数据目录里人工确认过的说明（业务日期按哪一列、有效记录"
    "条件、去重规则）要遵守，标「推断，未确认」的谨慎使用\n"
    "- 时间范围（统计期看本次运行的输入）、要排除哪些行、要不要去重，都按口径说明来；口径说明没写到的，按数据目录和"
    "最常见的业务理解，并在理由里写明你的取舍\n"
    "- 尽量用一条 SQL 直接算出要交的那个数（聚合放在 SQL 里做），最后交出它所在的格子"
)
BLIND_TASK_PROMPT = (
    "指标：{name}{unit}\n"
    "口径说明：{definition}\n"
    "计算式：{formula}\n"
    "你要算的是计算式里的 {label}{whole}{hint}。{dims}只交这一个数。\n"
    "数据源：{source}\n"
    "{inputs}\n"
    "{catalog}"
)
BLIND_UNIT_PROMPT = "（单位：{unit}）"
BLIND_NO_DEFINITION_PROMPT = "（未提供，按指标名称和数据目录理解）"
BLIND_WHOLE_PROMPT = "（这个指标就是它本身）"
BLIND_HINT_PROMPT = "（原流程的结果里这一项叫「{column}」）"
BLIND_DIMS_PROMPT = "原流程的结果分了组，这个数属于对应 {dims} 的那一组；按哪一列分组、怎么归属，请按口径说明和数据目录判断。"
BLIND_DAY_PROMPT = "{value} 这一天"
BLIND_MONTH_PROMPT = "{value} 这个月"
BLIND_INPUTS_PROMPT = "本次运行的输入（统计期之类的参数在这里）：{inputs}\n"
BLIND_INDEX_PROMPT = "数据源里的表（表名｜中文名｜粒度）：\n{index}"
BLIND_NOTES_PROMPT = "数据目录（标「推断，未确认」的项是推断，使用前请核实）：\n{notes}"
BLIND_NO_CATALOG_PROMPT = "这个数据源还没有数据目录，请用 db_schema 工具查看表结构。"
BLIND_MORE_PROMPT = "\n…（其余的请用 db_schema 工具查看）"
#: 盲算交回的结构：一个数（交给模型时换成格子引用，由代码取值），verify_agent 另加理由
BLIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "number", "description": "要你算的那个数"}},
    "required": ["value"],
}


@dataclass(frozen=True)
class _Options:
    """这次复算按什么来：节点配置优先，没写的取发起时的核查设置快照。"""

    tolerance: float
    on_reject: str
    metrics: list[str] | None
    definitions: dict[str, str]
    max_queries: int | None
    timeout_s: float | None
    daily_max_usd: float | None


async def run_dual_check(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    settings = await _settings(ctx)
    part = settings.get("dual_check") if isinstance(settings.get("dual_check"), dict) else {}
    switch = ctx.config.get("enabled")
    if switch is False or (switch is not True and not part.get("enabled", True)):
        await once(ctx, EventType.LOG, level="info", code="dual_check_skipped", reason="disabled",
                   message=SKIP_NODE_OFF if switch is False else SKIP_DISABLED)
        return _skipped(ctx, "disabled")

    card_id, card = _card(state, ctx)
    options = _options(ctx, part, settings)
    targets = _targets(card, options, ctx) if card is not None else []
    if not targets:
        await once(ctx, EventType.LOG, level="info", code="dual_check_skipped", reason="no_inputs",
                   message=SKIP_NO_INPUTS)
        return _skipped(ctx, "no_inputs")

    checked = await _step("dual_check_blind", lambda: _check(state, ctx, targets, options, settings))
    rows = checked["rows"]
    disagreed = [r for r in rows if r["status"] == DIFFERENT]
    if not disagreed:
        agreed = sum(1 for r in rows if r["status"] == SAME)
        # agreed 只说「全部一致」：一个都没核成、只核成一部分的另记，时间线和证据面板才不会把降档的出具说成一致
        status = AGREED if agreed == len(rows) else PARTIAL if agreed else NONE_CHECKED
        return _finish(state, ctx, card_id, card, checked, status=status, gaps=_unchecked_gaps(rows))

    review = await _step("dual_check_review", lambda: _review(state, ctx, checked, options))
    response = await ask(ctx, review["payload"], **review["payload"])
    # 谁裁决的跟着事件走，和人工审批节点一样
    await once(ctx, EventType.HUMAN_RESOLVED, response=response, actor=ctx.actor())
    choices, note = _choices(ctx, response, disagreed)
    gaps = _gaps(rows, choices)
    await once(ctx, EventType.LOG, level="info", code="dual_check_resolved", choices=choices, gaps=gaps,
               message=_resolved_text(disagreed, choices))
    candidates = [{**review["facts"][r["metric_id"]], "choice": choices[r["metric_id"]]}
                  for r in disagreed if choices.get(r["metric_id"]) in ("a", "b") and r["metric_id"] in review["facts"]]
    catalog = await _step("dual_check_catalog", lambda: _catalog(ctx, candidates)) if candidates else None
    rejected = [r for r in disagreed if choices.get(r["metric_id"]) != "a"]
    if rejected and options.on_reject == "fail":
        # 目录照样写了：审核人的选择是对数据的判断，和这次运行停不停无关
        names = "、".join(f"「{r['name']}」" for r in rejected[:5])
        raise NodeError(ctx.node.id, f"双盲复算不一致，审核人没有采用原流程的结果（指标{names}）。"
                                     f"「{DUAL_CHECK_FIELD_LABEL['on_reject']}」设为「{DUAL_CHECK_ON_REJECT_LABEL['fail']}」，运行已停止；"
                                     "请按审核意见修正取数或口径后重新运行")
    return _finish(state, ctx, card_id, card, checked, status=RESOLVED, gaps=gaps, choices=choices, note=note,
                   artifact=review["card_artifact"], catalog=catalog)


# --------------------------------------------------------------------------
# 设置、口径卡、要复算的指标
# --------------------------------------------------------------------------


async def _step(name: str, work: Callable[[], Awaitable[Any]]) -> Any:
    """在一个 @task 里做 work：审批恢复、接着运行时节点从头重放，结果直接取 checkpoint 里的，不再做一遍；work 里发的
    事件也就只发一次。不在图里执行（单独调用执行器）时直接做。"""
    if not _in_graph():
        return await work()

    @task(name=name)
    async def step() -> Any:
        return await work()

    return await step()


async def _settings(ctx: NodeContext) -> dict[str, Any]:
    """这次运行的核查设置：发起时的快照（run.options["verify"]）。发起时没快照的（不经 launch_run 发起、子工作流里的
    节点）读当前设置，读一次记进 checkpoint：重放时不再读，审批停下期间改了设置也不会让重放走上另一条路。"""
    from app.api.settings import VERIFY_GROUP, verify_settings, verify_values
    from app.engine.runner import RUN_OPTIONS_KEY

    options = ctx.run.extra.get(RUN_OPTIONS_KEY)
    snapshot = options.get(VERIFY_GROUP) if isinstance(options, dict) else None
    if isinstance(snapshot, dict):
        return verify_values(snapshot)

    async def read() -> dict[str, Any]:
        async with SessionLocal() as session:
            return await verify_settings(session)

    return await _step("dual_check_settings", read)


def _card(state: GraphState, ctx: NodeContext) -> tuple[str, dict[str, Any] | None]:
    """要复算的口径卡：(节点 id, 它这次的产出)。配置不对（发起前的校验已经拦下，这里兜底）报节点错误；卡没执行到
    （在没走到的分支上）产出为 None。"""
    card_id, problem = dual_check_card(ctx.run.spec, ctx.node)
    if problem or not card_id:
        raise NodeError(ctx.node.id, problem or f"「{DUAL_CHECK_FIELD_LABEL['card']}」需要选择一个口径卡节点")
    node = ctx.run.spec.node_map().get(card_id)
    if node is None or node.type != NodeType.METRICS:
        raise NodeError(ctx.node.id, f"「{DUAL_CHECK_FIELD_LABEL['card']}」选择的「{card_id}」不是口径卡节点，"
                                     "请重新选择")
    output = (state.get("nodes") or {}).get(card_id)
    return card_id, output if isinstance(output, dict) and output.get("kind") == "metric_set" else None


def _options(ctx: NodeContext, part: Mapping[str, Any], settings: Mapping[str, Any]) -> _Options:
    cfg = ctx.config
    tolerance = cfg.get("tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) \
            or tolerance < 0:
        tolerance = part.get("tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        tolerance = 0.0
    wanted = cfg.get("metrics")
    definitions = cfg.get("definitions") if isinstance(cfg.get("definitions"), dict) else {}
    return _Options(
        tolerance=float(tolerance),
        on_reject=cfg.get("on_reject") if cfg.get("on_reject") in DUAL_CHECK_ON_REJECT else "degrade",
        metrics=[str(m) for m in wanted if isinstance(m, str) and m.strip()] if isinstance(wanted, list) else None,
        definitions={str(k): str(v) for k, v in definitions.items() if isinstance(v, str) and v.strip()},
        max_queries=part.get("max_queries"), timeout_s=part.get("timeout_s"),
        daily_max_usd=settings.get("daily_max_usd"),
    )


def _from_query(item: Any) -> bool:
    return isinstance(item, dict) and item.get("via") in QUERY_VIAS and isinstance(item.get("artifact"), str) \
        and bool(item["artifact"]) and isinstance(item.get("path"), str)


def _number(value: Any) -> bool:
    if not is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _targets(card: dict[str, Any], options: _Options, ctx: NodeContext) -> list[dict[str, Any]]:
    """要复算的指标，按配置的先后（没配就按卡上的先后）：原值是数、至少一个输入来自查询快照。配置里写了卡上没有的
    指标 ID 报节点错误（口径卡引用自别的工作流时，画图时查不出来）。"""
    metrics = [m for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id") is not None]
    by_id = {str(m["id"]): m for m in metrics}
    if options.metrics:
        if unknown := [m for m in options.metrics if m not in by_id]:
            raise NodeError(ctx.node.id, f"「{DUAL_CHECK_FIELD_LABEL['metrics']}」中的「{'、'.join(unknown[:5])}」"
                                         f"不是口径卡上的指标 ID，可选的指标 ID：{'、'.join(list(by_id)[:12])}")
        chosen = [by_id[m] for m in dict.fromkeys(options.metrics)]
    else:
        chosen = metrics
    return [m for m in chosen if _number(m.get("value")) and any(_from_query(i) for i in m.get("inputs") or [])]


def _skipped(ctx: NodeContext, reason: str) -> dict[str, Any]:
    return {"nodes": {ctx.node.id: {"status": SKIPPED, "reason": reason}}}


# --------------------------------------------------------------------------
# 盲算与比对（一个 @task）
# --------------------------------------------------------------------------


class _Sources:
    """一次复算里要读的查询快照和数据源：同一份只读一次。"""

    def __init__(self) -> None:
        self.snapshots: dict[str, tuple[Any, str | None]] = {}
        self.rows: dict[str, DataSource | None] = {}

    def snapshot(self, artifact: str) -> tuple[dict[str, Any] | None, str | None]:
        if artifact not in self.snapshots:
            try:
                content = artifact_store.load(artifact)
                self.snapshots[artifact] = (content, None if content is not None else "查询快照无法读取")
            except ValueError:
                self.snapshots[artifact] = (None, "查询快照与哈希不一致，疑似被修改")
        content, why = self.snapshots[artifact]
        if why is None and not (isinstance(content, dict) and isinstance(content.get("rows"), list)):
            return None, "查询快照无法读取"
        return content, why

    async def source(self, name: str) -> DataSource | None:
        if name not in self.rows:
            async with SessionLocal() as session:
                self.rows[name] = (await session.execute(
                    select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        return self.rows[name]

    async def hidden(self, artifact: str) -> frozenset[str]:
        """这份快照要遮的列（小写）：查询当时记下的，加上数据源现在设的。宁可多遮。"""
        from app.data.engine import masked_columns

        snap, _ = self.snapshot(artifact)
        recorded = (snap or {}).get("mask_columns")
        out = {c.lower() for c in recorded if isinstance(c, str)} if isinstance(recorded, list) else set()
        name = (snap or {}).get("source")
        if isinstance(name, str) and name:
            row = await self.source(name)
            out |= {c.lower() for c in masked_columns(row.options if row is not None else None)}
        return frozenset(out)


def _loader(state: GraphState) -> tuple[Callable[[str], Any], dict[str, str]]:
    """重算要的取工件函数和节点产出：nodes.* 的容器输入从这次运行的状态里取（口径卡执行时上游的产出就是这些）。"""
    prefix = "state:"
    outputs = {str(nid): f"{prefix}{nid}" for nid in state.get("nodes") or {}}

    def load(key: str) -> Any:
        if key.startswith(prefix):
            return (state.get("nodes") or {}).get(key[len(prefix):])
        return artifact_store.load(key)

    return load, outputs


async def _check(state: GraphState, ctx: NodeContext, targets: list[dict[str, Any]], options: _Options,
                 settings: Mapping[str, Any]) -> dict[str, Any]:
    """逐个指标认出能复算的输入、按输入盲算、代回原式比对。返回 {rows, results, model, same_model, usage}。

    rows 每个指标一项：{metric_id, name, unit, decimals, status, reason?, original: {value, rendered}, blind: {value,
    rendered}, delta_pct, inputs: [{path, key, value, sql, source, alias, artifact, row, column, node_id, via}],
    differing}；results 是每个盲算（按格子去重）的结果，见 _slim。
    """
    sources = _Sources()
    load, outputs = _loader(state)
    aliases = _original_aliases(state)
    inputs = _run_inputs(state)
    rows: list[dict[str, Any]] = []
    jobs: dict[str, dict[str, Any]] = {}
    sharers: dict[str, list[dict[str, str]]] = {}
    for metric in targets:
        row, metric_jobs = await _plan_metric(state, metric, options, sources, aliases, load, outputs)
        rows.append(row)
        for key, job in metric_jobs.items():
            job["inputs"] = inputs
            sharers.setdefault(key, []).append({"id": row["metric_id"], "name": row["name"]})
            # 两个指标用到同一格只盲算一次，按先出现的那个指标说清要算什么；先出现的没写口径说明、后一个写了，用后一个
            if key not in jobs or (not jobs[key]["definition"] and job["definition"]):
                jobs[key] = job
    for key, job in jobs.items():
        job["metrics"] = sharers[key]
    results, model, same, usage = await _blind(ctx, jobs, options, settings, _producer_models(state, rows))
    for row, metric in zip(rows, targets):
        if row["status"] != UNCHECKED:
            _compare(row, metric, results, options, load, outputs, row.pop("_masks"))
        else:
            row.pop("_masks", None)
    agreed = sum(1 for r in rows if r["status"] == SAME)
    if agreed and not any(r["status"] == DIFFERENT for r in rows):
        ctx.emit(EventType.LOG, level="info", code="dual_check_agreed", metrics=agreed,
                 message=f"双盲复算一致：{agreed} 个指标两边的结果相同，已放行")
    if unchecked := [r for r in rows if r["status"] == UNCHECKED]:
        # 结构上没法单独复算的（合并查询、遮罩列）以前一条 dual_check_* 日志都没有，时间线上看不出为什么没核成
        parts = "；".join(f"「{r['name']}」（{r.get('reason') or '原因不明'}）" for r in unchecked[:5])
        ctx.emit(EventType.LOG, level="warn", code="dual_check_unchecked",
                 metrics=[{"id": r["metric_id"], "name": r["name"], "reason": r.get("reason") or "原因不明"}
                          for r in unchecked],
                 message=f"双盲复算未能核对 {len(unchecked)} 个指标：{parts}"
                         + ("等" if len(unchecked) > 5 else "") + "，按无法复算计入缺口")
    return {"rows": rows, "results": results, "model": model, "same_model": same, "usage": usage}


def _run_inputs(state: GraphState) -> list[str]:
    """这次运行的输入里的标量（统计期、地区这类参数）：原流程的 SQL 常常是拿它们拼出来的，盲算 agent 不知道统计期
    就算不出同一个数。只给参数，不给 SQL。"""
    out: list[str] = []
    for key, value in (state.get("input") or {}).items():
        if isinstance(key, str) and key and (value is None or isinstance(value, (str, int, float, bool))):
            text = str(value).replace("\n", " ")
            out.append(f"{key} = {text[:_DIM_CHARS]}")
        if len(out) >= _INPUTS:
            break
    return out


def _original_aliases(state: GraphState) -> dict[str, str]:
    """原流程的查询快照在这次运行的证据台账里的编号（Q1、Q2…，和报告取全部上游查询时的编号一致）。"""
    ledger = [e for e in state.get("evidence") or [] if isinstance(e, dict)]
    catalog = build_catalog(nodes={}, ledger=ledger)
    return {str(entry.get("artifact")): alias for alias, entry in catalog.items()
            if entry.get("kind") == "query" and entry.get("artifact")}


async def _plan_metric(state: GraphState, metric: dict[str, Any], options: _Options, sources: _Sources,
                       aliases: dict[str, str], load: Callable[[str], Any], outputs: dict[str, str]
                       ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """一个指标：认出来自查询的输入能不能单独复算，能的话给每个输入建一个盲算任务。"""
    row: dict[str, Any] = {
        "metric_id": str(metric["id"]), "name": str(metric.get("name") or metric["id"]),
        "unit": str(metric.get("unit") or ""), "decimals": metric.get("decimals"),
        "status": "pending", "original": {"value": metric.get("value"), "rendered": metric.get("rendered")},
        "blind": {"value": None, "rendered": None}, "delta_pct": None, "inputs": [], "differing": [],
    }
    tops = _tops(metric)
    masks: dict[str, frozenset[str]] = {}
    problem: str | None = None
    for item in metric.get("inputs") or []:
        if not _from_query(item):
            continue
        info, why = await _query_input(item, sources, aliases, tops)
        masks[item["artifact"]] = await sources.hidden(item["artifact"])
        if why:
            problem = problem or why
            continue
        row["inputs"].append(info)
    row["_masks"] = masks
    if problem is None:
        # 什么都不改先重算一遍：和卡上记的对不上（输入还原不了、用到了遮罩列），换了盲算的输入也算不准
        check = recompute.recompute(metric, recompute.Changes(), recompute.Sources(
            load=load, outputs=outputs, masked=masks, queries=tuple(dict.fromkeys(masks))))
        if check["status"] != "ok" or check["changed"]:
            problem = f"无法按原表达式重算这个指标（{check.get('reason') or '重算结果与口径卡不一致'}）"
    if problem:
        row.update(status=UNCHECKED, reason=problem)
        return row, {}
    labels = {inp["path"]: f"输入{i}" for i, inp in enumerate(row["inputs"], 1)}
    formula = _formula(metric, labels)
    definition = options.definitions.get(row["metric_id"]) or ""
    jobs: dict[str, dict[str, Any]] = {}
    for inp in row["inputs"]:
        snap, _ = sources.snapshot(inp["artifact"])
        hidden = (masks.get(inp["artifact"]) or frozenset()) | await _constant_columns(snap or {}, sources)
        # 列名是原流程的取舍：只有一项来自查询、或者写了口径说明时用不着；几项又没口径说明的，看不出取舍才给
        hint = inp["column"] if len(row["inputs"]) > 1 and not definition and _neutral(inp["column"]) else None
        jobs[inp["key"]] = {
            "key": inp["key"], "metric_id": row["metric_id"], "metric": row["name"], "unit": row["unit"],
            "definition": definition, "formula": formula, "label": labels[inp["path"]], "hint": hint,
            "source": inp["source"], "dims": _dims(snap or {}, inp["row"], inp["column"], hidden),
        }
    return row, jobs


async def _query_input(item: dict[str, Any], sources: _Sources, aliases: dict[str, str], tops: list[str]
                       ) -> tuple[dict[str, Any], str | None]:
    """一个来自查询快照的输入：(盲算要用的信息, None) 或 (_, 为什么不能单独复算)。"""
    from app.engine.merge_query import MERGE_SOURCE

    artifact = item["artifact"]
    snap, why = sources.snapshot(artifact)
    if why:
        return {}, f"输入 {item['path']} 的{why}"
    source = snap.get("source")
    if source == MERGE_SOURCE:
        return {}, f"输入 {item['path']} 来自合并查询（跨数据源合并的结果），盲算只能在单个数据源里查询"
    if not isinstance(source, str) or not source:
        return {}, f"输入 {item['path']} 的查询快照没有记下数据源"
    locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
    row, column = locator.get("row"), locator.get("column")
    if isinstance(row, bool) or not isinstance(row, int) or row < 0 or not isinstance(column, str):
        return {}, f"输入 {item['path']} 取的是一整段查询结果，盲算只能复核单个格子"
    if item["path"] not in tops:
        return {}, f"输入 {item['path']} 决定取哪一格，不能单独复算"
    value = cell_value(item.get("value"))
    if not _number(value):
        return {}, f"输入 {item['path']} 不是数值"
    if column.lower() in await sources.hidden(artifact):
        return {}, f"输入 {item['path']} 取自设置了遮罩的列，不能用于复算"
    found = await sources.source(source)
    if found is None or not found.enabled:
        return {}, f"数据源「{source}」不存在或已停用"
    return {"path": item["path"], "key": f"{artifact}|{row}|{column}", "value": value,
            "sql": str(snap.get("sql") or ""), "source": source, "alias": aliases.get(artifact),
            "artifact": artifact, "row": row, "column": column, "node_id": item.get("node_id"),
            "via": item["via"]}, None


def _tops(metric: dict[str, Any]) -> list[str]:
    """表达式最外层的引用：只有它们的值直接参与计算，嵌在下标、cell() 行列里的决定取哪一格。"""
    try:
        tree, _, _ = parse_expression(str(metric.get("expression") or ""))
    except ExpressionError:
        return []
    tops, _ = recompute._refs(tree)
    return tops


class _Swap(ast.NodeTransformer):
    """把最外层的引用换成给人（给盲算 agent）看的写法：取值链、整个 cell() 调用，认法同 recompute._Bind。"""

    def __init__(self, shown: Callable[[str], ast.AST | None]) -> None:
        self.shown = shown

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _cell_call(node) and (swapped := self.shown(ast.unparse(node))) is not None:
            return ast.copy_location(swapped, node)
        return self.generic_visit(node)

    def _chain(self, node: ast.AST) -> ast.AST:
        if _chain_root(node) is None:
            return self.generic_visit(node)
        swapped = self.shown(ast.unparse(node))
        return ast.copy_location(swapped, node) if swapped is not None else node

    visit_Attribute = visit_Subscript = visit_Name = _chain


def _formula(metric: dict[str, Any], labels: dict[str, str]) -> str:
    """交给盲算 agent 的计算式：来自查询的输入写成「输入1」「输入2」，别的输入写当时代进去的值（容器写「其他输入」）。
    不露出取数节点的 id、cell() 的行列：那是原流程的取法，不是业务定义。"""
    expr = str(metric.get("expression") or "")
    try:
        tree, _, _ = parse_expression(expr)
    except ExpressionError:
        return "、".join(labels.values())
    values = {i.get("path"): i.get("value") for i in metric.get("inputs") or [] if isinstance(i, dict)}

    def shown(path: str) -> ast.AST | None:
        if path in labels:
            return ast.Name(id=labels[path], ctx=ast.Load())
        value = values.get(path)
        if value is None or isinstance(value, (bool, int, float)) or (
                isinstance(value, str) and len(value) <= _DIM_CHARS and not _BRIEF.match(value)):
            return ast.Constant(value=value)
        return ast.Name(id="其他输入", ctx=ast.Load())

    swapped = _Swap(shown).visit(copy.deepcopy(tree))
    return ast.unparse(ast.fix_missing_locations(swapped))


def _row_pairs(snapshot: Mapping[str, Any], row: int, column: str) -> list[tuple[str, Any]]:
    """原流程那一格所在行的其他列里不是数的那些：(列名, 取值)。数不算（同一行别的指标值）。"""
    rows = snapshot.get("rows") or []
    if not isinstance(row, int) or not 0 <= row < len(rows):
        return []
    record = rows[row]
    columns = [str(c) for c in snapshot.get("columns") or []]
    pairs = list(record.items()) if isinstance(record, dict) else list(zip(columns, record))
    out: list[tuple[str, Any]] = []
    for name, value in pairs:
        name = str(name)
        if name == column or value is None:
            continue
        kind = column_kind(snapshot, name)
        if kind == "number" or (kind is None and (is_number(value) or numeric_text(value) is not None)):
            continue
        out.append((name, value))
    return out


def _dims(snapshot: Mapping[str, Any], row: int, column: str, hidden: frozenset[str]) -> list[str]:
    """结果分了组时，交给盲算 agent 的这一行的分组取值（地区、日期这类维度）：它要知道算的是哪一组。只给取值、不给
    列名（原流程按哪一列分的组正是要它自己判断的），遮罩列、原 SQL 里写死的文字列（hidden，小写）不给。"""
    out: list[str] = []
    for name, value in _row_pairs(snapshot, row, column):
        if name.lower() in hidden:
            continue
        out.append(str(value).replace("\n", " ")[:_DIM_CHARS])
        if len(out) >= _DIMS:
            break
    return out


def _dim_text(value: str) -> str:
    """一个分组取值给盲算看的说法：日期、月份说「2026-09-02 这一天」，别的加「」。"""
    if _DAY.match(value):
        return BLIND_DAY_PROMPT.format(value=value)
    if _MONTH.match(value):
        return BLIND_MONTH_PROMPT.format(value=value)
    return f"「{value}」"


def _neutral(column: str) -> bool:
    """原流程结果的列名看不出取舍（按哪天归属、排没排除、去没去重）：可以交给盲算区分计算式里的几项。"""
    lowered = column.lower()
    return bool(column.strip()) and not any(word in lowered for word in _LOADED_NAME)


async def _constant_columns(snapshot: Mapping[str, Any], sources: _Sources) -> frozenset[str]:
    """原 SQL 最外层写死的文字列（'下单日' AS basis）的列名（小写）：那是原流程自己的标注，不是分组。解析不了为空。"""
    import sqlglot
    from sqlglot import exp

    from app.data.sqlcheck import dialect_of

    sql = snapshot.get("sql")
    name = snapshot.get("source")
    if not isinstance(sql, str) or not sql.strip():
        return frozenset()
    found = await sources.source(name) if isinstance(name, str) and name else None
    read = dialect_of(found.kind) if found is not None else None
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001 - 解析不了就当没有：交给盲算的只是取值，最坏多给一个
        return frozenset()
    out: set[str] = set()
    for select in [tree] if isinstance(tree, exp.Select) else list(tree.find_all(exp.Select))[:1]:
        for item in select.expressions:
            inner = item.this if isinstance(item, exp.Alias) else item
            if isinstance(inner, (exp.Literal, exp.Boolean, exp.Null)):
                out.add(_output_name(item, read).lower())
    return frozenset(out)


async def _catalog_text(sources: _Sources, name: str) -> str:
    """交给盲算 agent 的数据目录：表清单（表名、中文名、粒度）和有目录的表的业务说明（业务日期、有效记录条件、去重、
    字段含义、码值、关联关系）。太长的截断，其余请它用 db_schema 查。"""
    from app.data import catalog

    row = await sources.source(name)
    if row is None:
        return BLIND_NO_CATALOG_PROMPT
    async with SessionLocal() as session:
        entries = await catalog.read_catalog(session, row.id)
    cache = row.schema_cache if isinstance(row.schema_cache, dict) else {}
    tables = cache.get("tables") or {}
    notes = {table: entry.notes for table, entry in entries.items()}
    parts: list[str] = []
    if index := catalog.render_table_index(cache, notes):
        parts.append(BLIND_INDEX_PROMPT.format(index=_clip(index, _INDEX_CHARS)))
    blocks = []
    for table, body in notes.items():
        block = catalog.render_table_notes(body, meta=tables.get(table), system_notes=catalog.system_notes_source(row))
        # 每张表的说明去掉标题行，整段只在前面说一次「推断，未确认」的意思
        lines = block.split("\n", 1)[1] if "\n" in block else ""
        if lines:
            blocks.append(f"表 {table}：\n{lines}")
    if blocks:
        parts.append(BLIND_NOTES_PROMPT.format(notes=_clip("\n".join(blocks), _NOTES_CHARS)))
    return "\n\n".join(parts) or BLIND_NO_CATALOG_PROMPT


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + BLIND_MORE_PROMPT


def _task_text(job: dict[str, Any], catalog: str) -> str:
    dims = "、".join(_dim_text(v) for v in job["dims"])
    return BLIND_TASK_PROMPT.format(
        name=job["metric"], unit=BLIND_UNIT_PROMPT.format(unit=job["unit"]) if job["unit"] else "",
        definition=job["definition"] or BLIND_NO_DEFINITION_PROMPT, formula=job["formula"], label=job["label"],
        whole=BLIND_WHOLE_PROMPT if job["formula"].strip() == job["label"] else "",
        hint=BLIND_HINT_PROMPT.format(column=job["hint"]) if job.get("hint") else "",
        dims=BLIND_DIMS_PROMPT.format(dims=dims) if dims else "",
        source=job["source"], catalog=catalog,
        inputs=BLIND_INPUTS_PROMPT.format(inputs="；".join(job["inputs"])) if job.get("inputs") else "")


async def _blind(ctx: NodeContext, jobs: dict[str, dict[str, Any]], options: _Options, settings: Mapping[str, Any],
                 producers: set[str]) -> tuple[dict[str, Any], str | None, bool, dict[str, Any]]:
    """盲算：(各格子的结果, 盲算模型, 是否和产出原数据的模型相同, 用量)。producers 是产出原数据的模型。

    先单独做第一个：核查模型本身用不了（没配好、接入出错）时，后面的一样用不了，照这一次的原因记，不再一个个去试。
    其余的同时进行，最多 BLIND_CONCURRENCY 个：串行时几个慢的输入（每个最多 timeout_s 加收尾的宽限）就能把运行这一段
    的时长上限耗光，整段盲算没有 checkpoint，接着运行时全部重做、再花一次钱。指标多了并行也一样耗得光，所以每项开始
    之前还要看这一段还剩多少时间（engine/deadline.py）：不够就不再开始、按无法复算记，够的话时长上限按剩余时间收紧。
    结果按格子写回，日志按任务的先后发，和完成的先后无关。"""
    from app.api.settings import verify_model_spec
    from app.engine import verify_agent
    from app.engine.verify_agent import VerifyOutcome, run_verify_agent
    from app.tools.datasource import run_versions

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "calls": 0}
    if not jobs:
        return {}, None, False, usage
    async with SessionLocal() as session:
        spec = await verify_model_spec(session, ctx.config, settings=dict(settings))
    sources = _Sources()
    catalogs: dict[str, str] = {}
    for job in jobs.values():
        if job["source"] not in catalogs:
            catalogs[job["source"]] = await _catalog_text(sources, job["source"])
    outcomes: dict[str, Any] = {}
    squeezed: dict[str, float] = {}     # 时长按剩余时间收紧过的盲算 → 这一段执行的总时长（到了点时说明里写）

    async def one(key: str, job: dict[str, Any]) -> None:
        # 给下游留出余量后不够再做一项：不再开始，交回一个没有模型的结果，按无法复算记——第一项就不够时，和核查模型
        # 用不了一样，后面的不再一个个去试。在 @task 里定，重放时不再重新定
        allot = deadline.allot(options.timeout_s, grace=verify_agent.SETTLE_GRACE_S)
        if not allot.start:
            outcomes[key] = VerifyOutcome(result=None, cells=[], queries=[], rationale=None, usage={},
                                          error=LATE.format(seconds=allot.seconds))
            return
        if allot.tightened:
            squeezed[key] = allot.seconds
        outcomes[key] = await run_verify_agent(
            spec=spec, system=BLIND_SYSTEM, task=_task_text(job, catalogs[job["source"]]), sources=[job["source"]],
            output_schema=BLIND_SCHEMA, max_queries=options.max_queries, timeout_s=allot.timeout_s,
            daily_max_usd=options.daily_max_usd, emit=ctx.emit, run_id=ctx.run.run_id, node_id=ctx.node.id,
            data_versions=run_versions(ctx.run))

    keys = list(jobs)
    await one(keys[0], jobs[keys[0]])
    first = outcomes[keys[0]]
    if len(keys) > 1 and not (first.error and not first.model):
        gate = asyncio.Semaphore(BLIND_CONCURRENCY)

        async def gated(key: str) -> None:
            async with gate:
                await one(key, jobs[key])

        await asyncio.gather(*(gated(key) for key in keys[1:]))
    results: dict[str, Any] = {}
    model: str | None = None
    same = False
    for key in keys:
        outcome = outcomes.get(key)
        if outcome is None:
            results[key] = dict(results[keys[0]])
            continue
        results[key] = _slim(outcome)
        if key in squeezed and results[key]["limit"] == "timeout_s":
            # 收紧过的时长到了点：照样按无法复算记（同触了每项时长上限），原因说运行时长上限将至。不留 limit，免得发
            # dual_check_limit——它叫人调高每项的上限，调高了只会更快撞线
            results[key].update(value=None, limit=None, reason=SQUEEZED.format(seconds=squeezed[key]))
        for k in usage:
            usage[k] += outcome.usage.get(k) or 0
        if outcome.model and model is None:
            model = outcome.model
    if model is not None and model in producers:
        same = True
        ctx.emit(EventType.LOG, level="warn", code="dual_check_same_model", model=model,
                 message=f"盲算模型与产出原数据的模型相同（「{model}」），难以发现原流程自身的错误。"
                         "请在「设置 → 偏好设置 → 核查」中选择另一个核查模型，"
                         f"或在节点的「{DUAL_CHECK_FIELD_LABEL['model']}」中指定")
    for key in keys:
        found = results[key]
        if found.get("limit") in LIMIT_CODES and not _settled(found):
            # 几个指标共用这一格：每个指标都按无法复算记，各发一条，时间线上不会只说先出现的那个
            for metric in jobs[key]["metrics"]:
                ctx.emit(EventType.LOG, level="warn", code="dual_check_limit", limit=found["limit"],
                         metric=metric["id"],
                         message=f"指标「{metric['name']}」的盲算{_limit_text(found['limit'], options)}，按无法复算处理")
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    return results, model, same, usage


def _producer_models(state: GraphState, rows: list[dict[str, Any]]) -> set[str]:
    """产出原数据的模型：要复算的输入是 Agent 经 cite_fields 交来的，那个 Agent 用的模型（节点产出里的 model）。调用
    工具节点写死的 SQL 没有模型参与，不算。"""
    outputs = state.get("nodes") or {}
    out: set[str] = set()
    for row in rows:
        for inp in row["inputs"]:
            found = outputs.get(inp.get("node_id")) if inp.get("via") == "agent_field" else None
            if isinstance(found, dict) and isinstance(found.get("model"), str) and found["model"]:
                out.add(found["model"])
    return out


def _slim(outcome: Any) -> dict[str, Any]:
    """一次盲算的结果里要留进 checkpoint 的部分。"""
    cell = next((c for c in outcome.cells if c.get("path") == "value"), None) or {}
    query = next((q for q in outcome.queries if q.get("alias") == cell.get("alias")), None) or {}
    value = cell.get("value") if cell.get("status") == "resolved" else None
    reason = None
    if outcome.error:
        reason = outcome.error
    elif cell.get("status") == "missing":
        reason = "盲算没有查到这个数"
    elif cell.get("status") == "unresolved":
        reason = f"盲算交回的格子引用不可用（{cell.get('reason') or '没有交这个数'}）"
    elif value is None:
        reason = "盲算取到的那一格是空值"
    elif _cell_masked(cell):
        value, reason = None, "盲算取的是设置了遮罩的列，不采用"
    return {
        "value": value, "reason": reason, "limit": outcome.limit, "model": outcome.model,
        "alias": cell.get("alias"), "ref": cell.get("ref"), "artifact": cell.get("artifact"),
        "row": cell.get("row"), "column": cell.get("column"), "eid": cell.get("eid"),
        "sql": query.get("sql"), "source": query.get("source"),
        "rationale": (outcome.result or {}).get("rationale") or "",
        # 理由组装成的文档（evidence.compose_doc）：数字有出处的挂着盲算自己查询的格子，裸数字已标出。审批卡照它画理由
        "rationale_doc": outcome.rationale if isinstance(outcome.rationale, dict) else None,
        "queries": [{k: q.get(k) for k in ("alias", "artifact", "sql", "row_count", "truncated", "source")}
                    for q in outcome.queries],
        "notes": list(outcome.notes),
    }


def _cell_masked(cell: dict[str, Any]) -> bool:
    """盲算交的那一格在遮罩列上（查询当时数据源设的遮罩记在它自己的快照里）：值会进审批卡和对照工件，不能用。"""
    if not cell.get("artifact"):
        return False
    try:
        snap = artifact_store.load(str(cell["artifact"]))
    except (ValueError, OSError):
        return True
    recorded = snap.get("mask_columns") if isinstance(snap, dict) else None
    column = str(cell.get("column") or "").lower()
    return isinstance(recorded, list) and any(isinstance(c, str) and c.lower() == column for c in recorded)


def _settled(found: Mapping[str, Any]) -> bool:
    """触了查询次数的上限，但交回的那一格是真查出来的：照常比对。verify_agent 查满 max_queries 次就记触顶（哪怕最后
    一次正是答案），按无法复算处理的话，「每项最多查询次数」设成 1 时盲算永远核不成。时长、金额触顶的是半路被掐断，
    照旧按无法复算。"""
    return found.get("limit") == "max_queries" and found.get("value") is not None


def _limit_text(limit: str | None, options: _Options) -> str:
    """盲算为什么停下，接在「盲算」后面读：「盲算已达每项最多查询次数（6 次）」。"""
    if limit == "max_queries" and options.max_queries is not None:
        return f"已达每项最多查询次数（{options.max_queries} 次）"
    if limit == "timeout_s" and options.timeout_s is not None:
        return f"已达每项时长上限（{options.timeout_s:g} 秒）"
    if limit == "daily_max_usd" and options.daily_max_usd is not None:
        return f"已达核查每日金额上限（${float(options.daily_max_usd):g}）"
    if limit in LIMIT_CODES:
        return "已达核查的上限"
    return "轮数已用完"


def _agree(original: Any, blind: Any, tolerance: float) -> bool:
    """按相对误差比（原值为 0 时按绝对误差）。"""
    if original == blind:
        return True
    if not (_number(original) and _number(blind)):
        return False
    if original == 0:
        return abs(blind) <= tolerance
    return abs(blind - original) <= tolerance * abs(original)


def _delta_pct(original: Any, blind: Any) -> float | None:
    """相差比例，口径同运行对比：(盲算值 − 原流程值) ÷ |原流程值| × 100，两位小数；原流程值为 0 时为 None。"""
    if not (_number(original) and _number(blind)) or original == 0:
        return None
    return round((blind - original) / abs(original) * 100, 2)


def _compare(row: dict[str, Any], metric: dict[str, Any], results: dict[str, Any], options: _Options,
             load: Callable[[str], Any], outputs: dict[str, str], masks: dict[str, frozenset[str]]) -> None:
    """盲算的输入代回原式，和原值比。row 就地改成一致 / 不一致 / 无法复算。"""
    values: dict[str, Any] = {}
    for inp in row["inputs"]:
        found = results.get(inp["key"])
        if found is None:
            row.update(status=UNCHECKED, reason="盲算没有执行")
            return
        if found.get("limit") and not _settled(found):
            row.update(status=UNCHECKED, reason="盲算" + _limit_text(found["limit"], options))
            return
        if found.get("value") is None:
            row.update(status=UNCHECKED, reason=found.get("reason") or "盲算没有交回可用的数值")
            return
        values[inp["path"]] = found["value"]
    outcome = recompute.recompute(metric, recompute.Changes(inputs=values), recompute.Sources(
        load=load, outputs=outputs, masked=masks, queries=tuple(dict.fromkeys(masks))))
    if outcome["status"] != "ok":
        row.update(status=UNCHECKED, reason=f"用盲算的输入按原表达式算不出结果（{outcome.get('reason') or '无法计算'}）")
        return
    original = row["original"]["value"]
    blind = outcome["new"]
    row["blind"] = {"value": blind, "rendered": outcome.get("rendered_new")}
    row["delta_pct"] = _delta_pct(original, blind)
    if _agree(original, blind, options.tolerance):
        row["status"] = SAME
        return
    row["status"] = DIFFERENT
    # 对不上的输入：超出误差的；都没超出（误差经算式放大了）就取值不同的，再不行就全部
    differing = [i["key"] for i in row["inputs"] if not _agree(i["value"], values[i["path"]], options.tolerance)]
    differing = differing or [i["key"] for i in row["inputs"] if i["value"] != values[i["path"]]]
    row["differing"] = differing or [i["key"] for i in row["inputs"]]


def _unchecked_gaps(rows: list[dict[str, Any]]) -> list[str]:
    return [GAP_UNCHECKED.format(name=r["name"], reason=r.get("reason") or "原因不明")
            for r in rows if r["status"] == UNCHECKED]


# --------------------------------------------------------------------------
# 不一致：差异说明、对照工件、审批（一个 @task）
# --------------------------------------------------------------------------


async def _review(state: GraphState, ctx: NodeContext, checked: dict[str, Any], options: _Options) -> dict[str, Any]:
    """不一致的指标逐个说清差在哪，完整对照落成工件，组好审批的 payload。返回 {payload, facts, card_artifact}。

    两条 SQL 先收窄到这一格再比（_narrowed）：原流程一条查询算好几列、分组后取一行时，整条拿去比会混进「选出的列」
    「分组」这类和这一格无关的差别，审批卡上只列前几条，审核人看到的多半是噪声，只差一个方面的也就写不进目录。两边
    各按自己那份快照要遮的列收窄（原流程的、盲算的快照各记着查询当时的遮罩，再加数据源现在设的）。
    facts 是每个不一致指标写数据目录要用的东西（dual_check_facts.derive 的候选，少一个 choice）：只有一个输入对不上时是
    那一处完整的差异条目（带两边比较的写法和涉及的表、列），几个输入都对不上时只说「不止一处」；scope 是这个指标自己的
    范围（口径说明、所在分组、运行输入、分组的列），有效记录条件据此排除指标自己的筛选范围。scope 只在节点里用、不进
    payload 和工件（所在分组的取值里可能有遮罩列的）。
    payload 在约定的几项之外带 on_reject（实际生效的处置），摘要里每个指标带 catalog（选 a、选 b 各会往数据目录里写
    什么，dual_check_facts.preview）：审核人在卡上要看得出没采用原流程会不会让运行失败、选了会不会记成人工确认的目录项。
    """
    rows = [r for r in checked["rows"] if r["status"] == DIFFERENT]
    results = checked["results"]
    sources = _Sources()
    infos: dict[str, dict[str, Any]] = {}
    metrics: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    facts: dict[str, dict[str, Any]] = {}
    inputs = _run_inputs(state)
    for row in rows:
        per_input: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        compared: list[tuple[str, str]] = []
        scope_texts = [options.definitions.get(row["metric_id"]) or "", *inputs]
        scope_columns: set[str] = set()
        for inp in row["inputs"]:
            if inp["source"] not in infos:
                infos[inp["source"]] = await _source_info(sources, inp["source"])
            kind = infos[inp["source"]].get("kind")
            blind = results.get(inp["key"]) or {}
            original_snap, _ = sources.snapshot(inp["artifact"])
            pairs = _row_pairs(original_snap or {}, inp["row"], inp["column"])
            scope_texts += [str(v) for _, v in pairs]
            scope_columns |= {name.lower() for name, _ in pairs} | _group_columns(inp["sql"], kind) \
                | _group_columns(blind.get("sql"), kind)
            if inp["key"] not in row["differing"]:
                continue
            blind_snap, _ = sources.snapshot(str(blind["artifact"])) if blind.get("artifact") else (None, None)
            blind_hidden = await sources.hidden(str(blind["artifact"])) if blind.get("artifact") else frozenset()
            a_sql = _narrowed(inp["sql"], inp["column"], _record(original_snap, inp["row"]), kind,
                              await sources.hidden(inp["artifact"])) or inp["sql"]
            b_sql = _narrowed(blind.get("sql"), blind.get("column"), _record(blind_snap, blind.get("row")), kind,
                              blind_hidden) or blind.get("sql")
            diff = await _explain(a_sql, b_sql, infos[inp["source"]])
            per_input.append((inp, blind, diff))
            compared.append((a_sql, str(b_sql or "")))
        merged = _merge(per_input)
        metrics.append({
            "metric_id": row["metric_id"], "name": row["name"], "unit": row["unit"], "decimals": row["decimals"],
            "original": {**row["original"], "inputs": [
                {"path": i["path"], "value": i["value"], "sql": i["sql"], "source": i["source"], "alias": i["alias"]}
                for i in row["inputs"]]},
            "blind": {**row["blind"], "inputs": [
                {"path": i["path"], "value": (results.get(i["key"]) or {}).get("value"),
                 "sql": (results.get(i["key"]) or {}).get("sql"), "source": (results.get(i["key"]) or {}).get("source"),
                 "alias": (results.get(i["key"]) or {}).get("alias"),
                 "rationale": (results.get(i["key"]) or {}).get("rationale") or "",
                 "rationale_doc": (results.get(i["key"]) or {}).get("rationale_doc")}
                for i in row["inputs"]]},
            "diff": {"equivalent": merged["equivalent"],
                     "items": [{"aspect": i.get("aspect"), "text": i.get("text")} for i in merged["items"]],
                     "fallback": merged["fallback"], "text_diff": merged["text_diff"]},
        })
        first = per_input[0][0] if per_input else (row["inputs"][0] if row["inputs"] else {})
        single = compared[0] if len(per_input) == 1 else None
        info = infos.get(str(first.get("source"))) or {}
        facts[row["metric_id"]] = {
            "metric_id": row["metric_id"], "metric": row["name"], "source": first.get("source"),
            "dialect": info.get("kind"),
            "sql": {"a": single[0], "b": single[1]} if single else {},
            "diff": {"equivalent": merged["equivalent"], "fallback": merged["fallback"] or single is None,
                     "items": merged["items"]},
            "scope": {"texts": [t for t in dict.fromkeys(scope_texts) if t], "columns": sorted(scope_columns)},
        }
        summary.append({
            "metric_id": row["metric_id"], "name": row["name"], "unit": row["unit"],
            "original_rendered": row["original"]["rendered"], "blind_rendered": row["blind"]["rendered"],
            "delta_pct": row["delta_pct"], "diff": [str(i.get("text")) for i in merged["items"][:SUMMARY_DIFFS]],
            "fallback": not any(i.get("aspect") not in ("other", "parse") for i in merged["items"]),
            "catalog": preview(facts[row["metric_id"]], info.get("schema"), info.get("notes")),
        })
    content = {"metrics": metrics, "model": checked.get("model"), "same_model": bool(checked.get("same_model"))}
    # 同一次裁决里几个指标落到数据目录的同一项上时，按各自的选择合起来写（dual_check_facts._merged）：每个指标单独的
    # 预告说不准，把每种组合的结果都算好放进对照工件（payload 里放不下，审批卡打开时就取工件），卡上按当前的选择取
    catalogs = {name: (info.get("schema"), info.get("notes") or {}) for name, info in infos.items()}
    joint = await asyncio.to_thread(preview_joint, [facts[r["metric_id"]] for r in rows if r["metric_id"] in facts],
                                    catalogs)
    if joint:
        content["catalog_joint"] = joint
    try:
        artifact = await artifact_store.put_json(content, kind="dual_check", run_id=ctx.run.run_id,
                                                 node_id=ctx.node.id, meta={"metrics": len(metrics)})
    except Exception as e:  # noqa: BLE001 - 对照存不下，审核人就看不到完整的两条 SQL：不能让人凭摘要裁决
        raise NodeError(ctx.node.id, f"双盲复算的对照结果保存失败（{describe_exception(e)}），请稍后接着运行") from e
    payload = {
        "kind": "dual_check", "node_id": ctx.node.id, "mode": "input",
        "title": f"双盲复算：{len(rows)} 个指标两边结果不一致，请选择采用哪一边的结果",
        "card_artifact": artifact, "summary": summary, "on_reject": options.on_reject,
    }
    names = "、".join(f"「{r['name']}」" for r in rows[:5]) + (f"等 {len(rows)} 个" if len(rows) > 5 else "")
    ctx.emit(EventType.LOG, level="warn", code="dual_check_disagreed",
             metrics=[{"id": r["metric_id"], "name": r["name"]} for r in rows], card_artifact=artifact,
             message=f"双盲复算不一致：指标{names}两边的结果对不上，已暂停等待审核")
    return {"payload": payload, "facts": facts, "card_artifact": artifact}


async def _source_info(sources: _Sources, name: str) -> dict[str, Any]:
    """比较两条 SQL、预告目录写入要用的：方言、表结构、列的中文名和整份数据目录。数据源没了就都不给，照样比原文。"""
    from app.data import catalog
    from app.data.sql_semantics import catalog_labels

    row = await sources.source(name)
    if row is None:
        return {"kind": None, "schema": None, "names": {}, "notes": {}}
    async with SessionLocal() as session:
        entries = await catalog.read_catalog(session, row.id)
    notes = {table: entry.notes for table, entry in entries.items()}
    return {"kind": row.kind, "schema": row.schema_cache if isinstance(row.schema_cache, dict) else None,
            "names": catalog_labels(notes), "notes": notes}


async def _explain(original: Any, blind: Any, info: dict[str, Any]) -> dict[str, Any]:
    """两条 SQL 的差异说明（SqlDiff.as_dict 的形状，条目带两边的写法和 detail）。规范化后一致的换成「差在数据」的
    一句：值不同，写法却一样，具体差异一条都说不出。"""
    from app.data.sql_semantics import explain_sql_diff

    if not isinstance(original, str) or not original.strip() or not isinstance(blind, str) or not blind.strip():
        return {"equivalent": False, "items": [{"aspect": "other", "a": None, "b": None, "text": NO_SQL}],
                "fallback": True, "text_diff": []}
    try:
        diff = await asyncio.wait_for(asyncio.to_thread(
            explain_sql_diff, original, blind, info.get("kind"), schema=info.get("schema"), names=info.get("names"),
            labels=SIDES), timeout=DIFF_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - 比较本身出了意外（超时、规范化的缺陷）：给兜底说明，审核人对照原文
        text = DIFF_TIMEOUT if isinstance(e, asyncio.TimeoutError) else DIFF_FAILED
        return {"equivalent": False, "items": [{"aspect": "other", "a": None, "b": None, "text": text}],
                "fallback": True, "text_diff": [f"- {original.strip()}", f"+ {blind.strip()}"]}
    out = diff.as_dict()
    if out["equivalent"]:
        return {"equivalent": True, "items": [{"aspect": SAME_SQL_ASPECT, "a": None, "b": None, "text": SAME_SQL}],
                "fallback": False, "text_diff": []}
    return out


def _record(snapshot: Mapping[str, Any] | None, row: Any) -> dict[str, Any] | None:
    """快照里第 row 行，写成 {列名: 值}；没有这一行为 None。"""
    rows = (snapshot or {}).get("rows") or []
    if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < len(rows):
        return None
    record = rows[row]
    if isinstance(record, dict):
        return {str(k): v for k, v in record.items()}
    columns = [str(c) for c in (snapshot or {}).get("columns") or []]
    return dict(zip(columns, record)) if isinstance(record, (list, tuple)) else None


def _output_name(item: Any, read: str | None) -> str:
    """最外层 SELECT 一项的结果列名：有别名用别名，裸列用列名，别的是驱动按原文起的名。"""
    from sqlglot import exp

    if isinstance(item, exp.Alias):
        return item.alias
    if isinstance(item, exp.Column):
        return item.name
    return item.sql(dialect=read)


def _same_column(a: str, b: str) -> bool:
    return "".join(a.split()).lower() == "".join(b.split()).lower()


def _group_projection(key: Any, projections: list[Any]) -> int | None:
    """分组键是最外层 SELECT 的第几项：GROUP BY 1、GROUP BY 原样的表达式、GROUP BY 别名都认，认不出为 None。"""
    from sqlglot import exp

    if isinstance(key, exp.Literal) and not key.is_string and str(key.this).isdigit():
        index = int(key.this) - 1
        return index if 0 <= index < len(projections) else None
    for i, item in enumerate(projections):
        if (item.this if isinstance(item, exp.Alias) else item) == key:
            return i
    if isinstance(key, exp.Column) and not key.table:
        hits = [i for i, item in enumerate(projections) if isinstance(item, exp.Alias)
                and item.alias.lower() == key.name.lower()]
        return hits[0] if len(hits) == 1 else None
    return None


def _sql_literal(value: Any) -> Any:
    """快照里的取值写回 SQL 常量；写不回（容器、非有限数）为 None。"""
    from sqlglot import exp

    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return exp.Literal.string(value)
    return None


def _narrowed(sql: Any, column: Any, record: Mapping[str, Any] | None, kind: str | None,
              hidden: frozenset[str] = frozenset()) -> str | None:
    """把一条查询收窄到这一格：最外层 SELECT 只留算出这一列的那一项；分组的去掉分组，按这一行的分组值补上等值条件
    （华东那一行 → WHERE … AND region = '华东'），HAVING、排序、取行数跟着去掉——这一行既然在结果里，它所在的组就
    满足 HAVING，留着只会多出一条和这一格无关的差异。

    差异说明拿收窄后的两条比，才只说这一格是怎么算出来的。认不准的（SELECT *、DISTINCT、窗口函数、集合运算、分组键
    对不上结果列、这一行没有分组值）返回 None，调用方照整条比。hidden 是这份快照要遮的列（小写）：分组键的结果列、
    或者它用到的列在里面时也返回 None——分组值写进 SQL 就跟着差异说明进了审批卡、对照工件和目录日志，遮罩列的值
    不能出现在那些地方。"""
    import sqlglot
    from sqlglot import exp

    from app.data.sqlcheck import dialect_of

    if not isinstance(sql, str) or not sql.strip() or not isinstance(column, str) or not column.strip():
        return None
    read = dialect_of(kind)
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001 - 解析不了：照整条比，比较本身会说清
        return None
    if not isinstance(tree, exp.Select) or tree.args.get("distinct"):
        return None
    projections = list(tree.expressions)
    if not projections or any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                              or p.find(exp.Window) for p in projections):
        return None
    hits = [i for i, p in enumerate(projections) if _same_column(_output_name(p, read), column)]
    if len(hits) != 1:
        return None
    target = projections[hits[0]]
    group = tree.args.get("group")
    if group is not None:
        if record is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return None
        values = {k.lower(): v for k, v in record.items()}
        conditions = []
        for key in group.expressions:
            index = _group_projection(key, projections)
            name = _output_name(projections[index], read).lower() if index is not None else None
            if name is None or name not in values:
                return None
            expr = projections[index].this if isinstance(projections[index], exp.Alias) else projections[index]
            if name in hidden or any(col.name.lower() in hidden for col in expr.find_all(exp.Column)):
                return None
            if values[name] is None:
                conditions.append(exp.Is(this=expr.copy(), expression=exp.Null()))
                continue
            literal = _sql_literal(values[name])
            if literal is None:
                return None
            conditions.append(exp.EQ(this=expr.copy(), expression=literal))
        for arg in ("group", "having", "order", "limit", "offset"):
            tree.set(arg, None)
        tree = tree.where(*conditions, copy=False)
    elif tree.args.get("limit") is None and tree.args.get("offset") is None:
        # 不取前几行时，排序不影响算出来的数
        tree.set("order", None)
    tree.set("expressions", [target])
    try:
        return tree.sql(dialect=read)
    except Exception:  # noqa: BLE001
        return None


def _group_columns(sql: Any, kind: str | None) -> set[str]:
    """一条查询分组用到的列（小写）：有效记录条件不收分组列上的条件（那是指标自己的筛选范围）。"""
    import sqlglot
    from sqlglot import exp

    from app.data.sqlcheck import dialect_of

    if not isinstance(sql, str) or not sql.strip():
        return set()
    read = dialect_of(kind)
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for group in tree.find_all(exp.Group):
        select = group.parent if isinstance(group.parent, exp.Select) else None
        for key in group.expressions:
            index = _group_projection(key, list(select.expressions)) if select is not None else None
            node = select.expressions[index] if index is not None else key
            out |= {col.name.lower() for col in node.find_all(exp.Column) if col.name}
    return out


def _merge(per_input: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """一个指标几个对不上的输入的差异合成一份：一个输入时就是它的；几个时每条前面写明是哪一列的输入。"""
    if not per_input:
        return {"equivalent": False, "items": [{"aspect": "other", "a": None, "b": None, "text": NO_SQL}],
                "fallback": True, "text_diff": []}
    if len(per_input) == 1:
        diff = per_input[0][2]
        return {"equivalent": bool(diff["equivalent"]), "items": list(diff["items"]),
                "fallback": bool(diff["fallback"]), "text_diff": list(diff.get("text_diff") or [])}
    items: list[dict[str, Any]] = []
    lines: list[str] = []
    for inp, _, diff in per_input:
        items += [{**i, "text": f"输入「{inp['column']}」：{i.get('text')}"} for i in diff["items"]]
        lines += list(diff.get("text_diff") or [])
    return {"equivalent": all(d["equivalent"] for _, _, d in per_input), "items": items,
            "fallback": any(d["fallback"] for _, _, d in per_input), "text_diff": lines[:40]}


# --------------------------------------------------------------------------
# 恢复之后：审核人的选择、缺口、数据目录
# --------------------------------------------------------------------------


def _choices(ctx: NodeContext, response: Any, rows: list[dict[str, Any]]) -> tuple[dict[str, str], str]:
    """审核人的回复 → ({指标 id: a | b | neither}, 备注)。回复的 value 是 {choices, note}；某个指标没选、选了认不出的
    不进 choices：照实记成「没有作出选择」，不替人说「两边都不对」，也不替人认可任何一边（缺口照样记、出具照样降档）。
    通用的输入卡送来的是一句话，所有指标都按没选记。明确驳回（approved 为否）的按人工驳回让节点失败，和人工审批节点的
    填写模式一样：这一步没有驳回的出口。"""
    decision = read_decision(response)
    if isinstance(response, dict) and decision.rejected:
        raise NodeError(ctx.node.id, f"人工驳回：{decision.note or '未说明原因'}")
    value = decision.value if isinstance(decision.value, dict) else response if isinstance(response, dict) else {}
    raw = value.get("choices") if isinstance(value.get("choices"), dict) else {}
    choices = {r["metric_id"]: raw[r["metric_id"]] for r in rows if raw.get(r["metric_id"]) in CHOICES}
    note = value.get("note") if isinstance(value.get("note"), str) and value.get("note").strip() else decision.note
    return choices, str(note or "").strip()


def _gaps(rows: list[dict[str, Any]], choices: dict[str, str]) -> list[str]:
    out: list[str] = []
    for row in rows:
        choice = choices.get(row["metric_id"])
        if row["status"] == UNCHECKED:
            out.append(GAP_UNCHECKED.format(name=row["name"], reason=row.get("reason") or "原因不明"))
        elif row["status"] == DIFFERENT and choice != "a":
            text = GAP_BLIND if choice == "b" else GAP_NEITHER if choice == "neither" else GAP_UNANSWERED
            out.append(text.format(name=row["name"]))
    return out


_CHOICE_TEXT = {"a": "采用原流程", "b": "采用盲算", "neither": "两边都不对"}


def _resolved_text(rows: list[dict[str, Any]], choices: dict[str, str]) -> str:
    parts = "；".join(f"「{r['name']}」{_CHOICE_TEXT.get(choices.get(r['metric_id']), '没有作出选择')}"
                     for r in rows[:8])
    return f"审核人已裁决 {len(rows)} 个不一致的指标：{parts}" + ("等" if len(rows) > 8 else "")


async def _catalog(ctx: NodeContext, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    from app.data.catalog import TABLE_FIELD_LABEL

    result = await record_choices(candidates, run_id=ctx.run.run_id, node_title=ctx.node.title, actor=ctx.actor())
    written, skipped = result["written"], result["skipped"]
    parts = []
    if written:
        parts.append("已写入数据目录：" + "、".join(
            f"「{w['table']}」的{TABLE_FIELD_LABEL.get(w['item'], w['item'])}" for w in written))
    if skipped:
        parts.append("未写入数据目录：" + "；".join(s["reason"] for s in skipped[:3])
                     + (f"等 {len(skipped)} 条" if len(skipped) > 3 else ""))
    ctx.emit(EventType.LOG, level="info", code="dual_check_catalog", written=written, skipped=skipped,
             message="。".join(parts))
    return result


# --------------------------------------------------------------------------
# 产出
# --------------------------------------------------------------------------


def _finish(state: GraphState, ctx: NodeContext, card_id: str, card: dict[str, Any] | None, checked: dict[str, Any],
            *, status: str, gaps: list[str], choices: dict[str, str] | None = None, note: str = "",
            artifact: str | None = None, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = checked["rows"]
    picked = choices or {}
    counts = {"agreed": sum(1 for r in rows if r["status"] == SAME),
              "disagreed": sum(1 for r in rows if r["status"] == DIFFERENT),
              "unchecked": sum(1 for r in rows if r["status"] == UNCHECKED)}
    metrics = [{
        "metric_id": r["metric_id"], "name": r["name"], "unit": r["unit"], "status": r["status"],
        "original_rendered": r["original"]["rendered"], "blind_rendered": r["blind"]["rendered"],
        "delta_pct": r["delta_pct"],
        **({"reason": r["reason"]} if r.get("reason") else {}),
        **({"choice": picked[r["metric_id"]]} if r["metric_id"] in picked else {}),
    } for r in rows]
    result: dict[str, Any] = {"status": status, "counts": counts, "metrics": metrics, "model": checked.get("model"),
                              "same_model": bool(checked.get("same_model"))}
    if artifact:
        result["card_artifact"] = artifact
    if choices is not None:
        result.update(choices=choices, note=note)
    if catalog is not None:
        result["catalog"] = catalog
    if gaps:
        result["issuance_gaps"] = gaps
    entry: dict[str, Any] = {
        "kind": "dual_check", "node_id": ctx.node.id, "exec": next_exec(state, ctx.node.id), "card": card_id,
        "status": status, "counts": counts,
        "metrics": [{"id": m["metric_id"], "status": m["status"], **({"choice": m["choice"]} if "choice" in m else {})}
                    for m in metrics],
        **({"metric_set": card["artifact"]} if card and isinstance(card.get("artifact"), str) else {}),
        **({"artifact": artifact} if artifact else {}),
    }
    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}, "evidence": [entry]}
    usage = checked.get("usage") or {}
    if usage.get("calls"):
        updates["usage"] = dict(usage)
    return updates


__all__ = ["AGREED", "CHOICES", "NONE_CHECKED", "PARTIAL", "RESOLVED", "SKIPPED", "run_dual_check"]
