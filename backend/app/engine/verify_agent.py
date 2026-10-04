"""核查用的只读 agent：查库，交回有出处的结果。双盲复算、反方审稿、审批质询共用这一个。

三处要的是同一件事：另起一个模型，只给它几个数据源的只读查询工具，让它自己写 SQL 去核对一个数、一句结论、一个
提问，最后交回结构化的结果——而且结果里的数要能核对。

**只读。** 工具只建给定数据源的 db_schema / db_query，建的时候 force_readonly：可写源上的写语句也被守卫拒绝
（tools/datasource.py）。这一路没有审批关卡，不能指望人在半路拦下一条 DELETE。

**数从快照取。** 每次成功的查询是一份查询快照（工具自己落的 put_json），按这个 agent 自己的顺序编 Q1、Q2…。
最后交结果时，调用方结构里的数值字段一律改成格子引用 {alias, row, column}，由代码按引用从快照里取值：模型自己
写的数字不采信，取不到就是 None，不兜底成 0。理由文字里的数字要写成 [[v:Qn.rX.列]]，用本 agent 自己的台账
（evidence.build_catalog）组装成文档（evidence.compose_doc），裸数字会被标出来。

**不进写作目录。** 节点里调用时（给了 emit）每次工具调用发 tool.start / tool.end，查询的 tool.end 带
query_artifact 和 role: verify——随运行封存、证据面板能下钻。但这里绝不往 state["evidence"] 写 kind: query：
报告的写作目录按台账里的 query 编号，核查的查询混进去，写作者就会把它们当成可以引用的证据。调用方只拿到结果，
台账条目由它按自己的种类记（dual_check / challenge / inquiry）。

**上限。** 查询次数、时长、金额（这一次、全局每日）任何一项到顶就停止查询，按已经查到的结果收尾：收尾那次
调用总是要做的（时长到顶时再给 SETTLE_GRACE_S 的宽限），所以金额上限每一轮都给它留出钱。模型不在价格目录里
时估不出金额，金额上限对它无效，照裁判的说法记一条说明。每次调用都记进核查的每日账（engine/spend.py）。

永远不抛：模型没配好、调用失败、格式不对、触顶，都落在返回值里（error / limit）。模型调用都经 get_chat_model，
测试换成剧本。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core import artifact_store
from app.core.errors import describe_exception, not_configured
from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine import spend
from app.engine.evidence import build_catalog, cell_eid, compose_doc, query_entry_fields, schema_ledger_entry
from app.engine.expressions import CellError, column_kind, locate_cell
from app.engine.judge import _json_in, _tokens, _usd
from app.engine.nodes.llm import _digest, _split_structured, _typed, explain_model_error
from app.engine.state import message_text
from app.engine.toolcalls import ToolTimeout, limit_fields, limit_of, run_bounded
from app.providers import catalog as pricing
from app.providers.factory import ModelSpec, ProviderNotConfigured, bind_tools_safely, get_chat_model
from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX, build_datasource_tools
from app.tools.registry import ToolArgsError, ToolContext, Versions, args_model_of, prepare_args

logger = logging.getLogger(__name__)

#: 时长到顶之后，收尾那次调用（交结构化结果）最多再等这么久。不给宽限的话，到点时手里的查询全白查了
SETTLE_GRACE_S = 30.0
#: 估算金额时每一轮、收尾那次的输出 token 数（回复里带了用量就按实际找补）
_OUT_STEP = 512
_OUT_FINAL = 1024
#: 工具结果交给模型时最多这么多字：完整结果在查询快照里，收尾时另按 _digest 列出
_TOOL_CHARS = 12_000
#: 结果里自动加上的理由字段（调用方的结构里已经有同名的字符串字段就沿用它）
RATIONALE = "rationale"
#: 触顶原因
LIMITS = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd", "max_steps")

VERIFY_SYSTEM = (
    "你是数据核查员：只读地查库，用查询结果完成交给你的任务。\n"
    "- 先用 db_schema 工具看表结构，确认表名、字段名，再用 db_query 工具写只读 SQL。不能改数据，写语句会被拒绝\n"
    "- 每次成功的查询前面都标着编号【证据 Qn】，最后交结果时用它说明每个数取自哪次查询的哪一格\n"
    "- {limit}先想清楚要查什么，不要重复同样的查询\n"
    "- 不要编造数字，查不到就照实说"
)
VERIFY_FINAL_PROMPT = (
    "现在交结果，不能再查询。按给定结构填写：\n"
    "- 数值字段一律写格子引用，形如 {\"alias\": \"Q1\", \"row\": 0, \"column\": \"列名\"}：alias 是【证据 Qn】里的编号，"
    "row 是行号（从 0 数），column 是列名。系统按引用从查询结果里取值，你自己写的数字不会被采用；查不到就写 null，"
    "不要估算\n"
    "- rationale 写理由，简短说清依据。理由里出现的每个数字都写成 [[v:Q1.r0.列名]] 这样的引用，系统会换成查询结果"
    "里的值；不要直接写数字，提到某次查询时写 [[see:Q1]]\n"
    "- 其余字段按各自的说明填写"
)
_CELL_REF: dict[str, Any] = {
    "type": "object",
    "description": "格子引用：alias 是【证据 Qn】里的编号，row 是行号（从 0 数），column 是列名",
    "properties": {"alias": {"type": "string"}, "row": {"type": "integer"}, "column": {"type": "string"}},
    "required": ["alias", "row", "column"],
}
_RATIONALE_FIELD: dict[str, Any] = {
    "type": "string",
    "description": "理由：简短说清依据，里面的每个数字都写成 [[v:Q1.r0.列名]] 这样的引用",
}

Emit = Callable[..., Any]


@dataclass
class VerifyOutcome:
    """一次核查的结果。

    result：调用方结构的形状，数值字段是从快照取到的值（取不到为 None），另有 rationale（理由渲染后的正文）；
    没交成结果时为 None，error 说明为什么。cells：每个数值字段的出处 {path, status, ref, alias, row, column,
    artifact, eid, value}，status 是 resolved / missing（模型说查不到）/ unresolved（reason 写原因，模型直接写了
    数字的记 model_value）。queries：成功的查询 {alias, artifact, sql, row_count, truncated, source, tool, call_id}。
    rationale：理由组装成的报告文档（evidence.compose_doc 的形状，violations 里有裸数字）。usage：{input_tokens,
    output_tokens, total_tokens, cost_usd, calls, unpriced_calls}。limit：触顶原因（LIMITS 之一），没触顶为 None。
    answered：模型是不是自己停下来的（没被上限掐断）。
    """

    result: dict[str, Any] | None
    cells: list[dict[str, Any]]
    queries: list[dict[str, Any]]
    rationale: dict[str, Any] | None
    usage: dict[str, Any]
    limit: str | None = None
    error: str | None = None
    model: str | None = None
    answered: bool = False
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 结构：数值字段换成格子引用
# --------------------------------------------------------------------------


def _types(schema: Any) -> set[str]:
    declared = schema.get("type") if isinstance(schema, dict) else None
    return {declared} if isinstance(declared, str) else set(declared) if isinstance(declared, list) else set()


def _is_object(schema: Any) -> bool:
    return isinstance(schema, dict) and ("object" in _types(schema) or "properties" in schema)


def _is_number(schema: Any) -> bool:
    return bool(_types(schema) & {"number", "integer"})


def result_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """交给模型的结构：调用方结构里的数值字段（number / integer，嵌套的对象、数组里的也算）一律换成可为 null 的
    格子引用，另加一个理由字段（已有同名的字符串字段就沿用）。其余字段原样。"""

    def rewrite(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if _is_number(node):
            out: dict[str, Any] = {"anyOf": [_CELL_REF, {"type": "null"}]}
            if node.get("description"):
                out["description"] = f"{node['description']}（写格子引用，查不到写 null）"
            return out
        if _is_object(node):
            props = node.get("properties") or {}
            return {**node, "type": "object", "properties": {k: rewrite(v) for k, v in props.items()}}
        if "array" in _types(node) and isinstance(node.get("items"), dict):
            return {**node, "items": rewrite(node["items"])}
        return node

    root = rewrite(schema if _is_object(schema) else {"type": "object", "properties": {}})
    props = dict(root.get("properties") or {})
    props.setdefault(RATIONALE, _RATIONALE_FIELD)
    required = list(dict.fromkeys([*(root.get("required") or []), RATIONALE]))
    # 走工具调用的结构化输出要求函数名只含字母、数字、下划线
    return {**root, "title": "verify_result", "type": "object", "properties": props, "required": required}


class _Cells:
    """按格子引用从查询快照里取值：快照经工件库读（复验哈希），同一份只读一次。"""

    def __init__(self, queries: list[dict[str, Any]]) -> None:
        self.by_alias = {q["alias"]: q for q in queries}
        self.memo: dict[str, tuple[Any, str | None]] = {}
        self.cells: list[dict[str, Any]] = []

    def snapshot(self, artifact: str) -> tuple[Any, str | None]:
        if artifact not in self.memo:
            try:
                content = artifact_store.load(artifact)
                self.memo[artifact] = (content, None if content is not None else "查询快照无法读取")
            except ValueError:
                self.memo[artifact] = (None, "查询快照与哈希不一致，可能已被修改")
        return self.memo[artifact]

    def take(self, value: Any, path: str, schema: dict[str, Any]) -> Any:
        """一个数值字段：按引用取值，记下出处，返回值（取不到为 None）。"""
        if value is None:
            self.cells.append({"path": path, "status": "missing"})
            return None
        if not isinstance(value, dict):
            # 模型直接写了数：不采信。照记下来，界面上能看到它想填什么
            self.cells.append({"path": path, "status": "unresolved", "model_value": value,
                               "reason": "模型直接写了数值，没有给出格子引用，不采信"})
            return None
        alias = str(value.get("alias") or value.get("call") or "").strip()
        row, column = value.get("row"), value.get("column")
        if isinstance(row, str) and row.strip().isdigit():
            row = int(row)          # 有的模型把整数写成 "0"：意思没有歧义
        elif isinstance(row, float) and row.is_integer():
            row = int(row)
        query = self.by_alias.get(alias)
        if query is None:
            self.cells.append({"path": path, "status": "unresolved", "alias": alias or None,
                               "reason": f"没有编号为 {alias or '（空）'} 的查询，可用的查询："
                                         + ("、".join(self.by_alias) or "（无）")})
            return None
        content, why = self.snapshot(query["artifact"])
        if why:
            self.cells.append({"path": path, "status": "unresolved", "alias": alias, "artifact": query["artifact"],
                               "reason": f"{alias} 的{why}"})
            return None
        try:
            raw, name = locate_cell(content, row, column)
        except CellError as e:
            self.cells.append({"path": path, "status": "unresolved", "alias": alias, "artifact": query["artifact"],
                               "reason": f"{alias} {e}"})
            return None
        truth = _typed(raw, schema, column_kind(content, name))
        self.cells.append({"path": path, "status": "resolved", "ref": f"{alias}.r{row}.{name}", "alias": alias,
                           "row": row, "column": name, "artifact": query["artifact"],
                           "eid": cell_eid(query["artifact"], row, name), "value": truth})
        return truth

    def walk(self, schema: Any, value: Any, path: str = "") -> Any:
        """按调用方的结构走一遍模型交的结果：数值字段换成从快照取到的值，其余原样。"""
        if _is_number(schema):
            return self.take(value, path, schema)
        if _is_object(schema):
            given = value if isinstance(value, dict) else {}
            return {k: self.walk(sub, given.get(k), f"{path}.{k}" if path else k)
                    for k, sub in (schema.get("properties") or {}).items()}
        if "array" in _types(schema) and isinstance(schema.get("items"), dict):
            if not isinstance(value, list):
                return None
            return [self.walk(schema["items"], v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value


# --------------------------------------------------------------------------
# 花费
# --------------------------------------------------------------------------


class _Budget:
    """金额上限（这一次、全局每日）和核查的每日账。调用前按估算预留，调用后按实际用量找补。"""

    def __init__(self, model_id: str, max_cost_usd: float | None, daily_max_usd: float | None) -> None:
        self.model_id = model_id
        self.priced = pricing.find_pricing(model_id) is not None
        self.max_cost_usd = max_cost_usd
        self.daily_max_usd = daily_max_usd
        self.spent = 0.0

    def estimate(self, text: str, out: int) -> float:
        return pricing.estimate_cost(self.model_id, _tokens(text), out) if self.priced else 0.0

    async def reserve(self, estimate: float, *, keep: float = 0.0) -> tuple[str | None, str | None]:
        """(记在哪天, None) 或 (None, 拦下的上限)。keep 是要额外留出的钱（收尾那次调用）：只用来判断，不预留。"""
        if self.priced and self.max_cost_usd is not None and self.spent + estimate + keep > self.max_cost_usd + 1e-12:
            return None, "max_cost_usd"
        cap = self.daily_max_usd if self.priced else None
        if cap is not None and keep and (await spend.daily_spend())["usd"] + estimate + keep > cap + 1e-12:
            return None, "daily_max_usd"
        day = await spend.charge(estimate, limit=cap, priced=self.priced)
        return (day, None) if day is not None else (None, "daily_max_usd")

    async def settle(self, day: str, estimate: float, inp: int, out: int) -> float:
        """按实际用量找补，返回这一次的金额。回复没带用量（有的网关不给、调用超时）就按估算记：宁可多记。"""
        actual = (pricing.estimate_cost(self.model_id, inp, out) if inp or out else estimate) if self.priced else 0.0
        await spend.settle(day, actual - estimate)
        self.spent += actual
        return actual


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


async def _announce(emit: Emit | None, kind: EventType, **data: Any) -> None:
    """经 emit 发一条事件（同步、协程函数都认）；没给 emit 就不发。发不出去不影响核查本身。"""
    if emit is None:
        return
    try:
        out = emit(kind, **data)
        if inspect.isawaitable(out):
            await out
    except Exception:  # noqa: BLE001 - 事件只是留痕
        logger.warning("核查的事件没发出去：%s", kind, exc_info=True)


async def _bounded(job: Any, seconds: float | None) -> Any:
    if seconds is None:
        return await job
    return await asyncio.wait_for(job, timeout=max(seconds, 0.001))


def _usage(message: Any) -> tuple[int, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)


def _limit_words(reason: str, *, max_queries: int | None, timeout_s: float | None, max_cost_usd: float | None,
                 daily_max_usd: float | None, steps: int) -> str:
    if reason == "max_queries":
        return f"最多查询 {max_queries} 次"
    if reason == "timeout_s":
        return f"时长上限 {timeout_s:g} 秒"
    if reason == "max_cost_usd":
        return f"这次核查的金额上限 {_usd(max_cost_usd)}"
    if reason == "daily_max_usd":
        return f"全局每日核查金额上限 {_usd(daily_max_usd)}"
    return f"最多 {steps} 轮"


def _text_of(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
                     for m in messages)


def _final_messages(system: str, task: str, queries: list[dict[str, Any]], said: str, stopped: str | None
                    ) -> list[BaseMessage]:
    """收尾那次调用的消息：任务、查到的结果（每次查询列出行号）、模型最后说的话，以及交结果的规则。"""
    parts = [f"任务：\n{task.strip()}"]
    if stopped:
        parts.append(f"已达上限（{stopped}），不能再查询，按已经查到的结果交结果。")
    parts.append(f"你查到的结果：\n{_digest(queries) if queries else '（没有成功的查询）'}")
    parts.append(f"你最后的说明（只作参考，数以查询结果为准）：\n{said.strip() or '（没有）'}")
    parts.append(VERIFY_FINAL_PROMPT)
    return [SystemMessage(content=system), HumanMessage(content="\n\n".join(parts))]


def _parsed(value: Any) -> dict[str, Any] | None:
    """结构化结果：对象；有的模型把整个对象写成一段 JSON 文本，也认。"""
    if isinstance(value, str):
        value = _json_in(value)
    return value if isinstance(value, dict) else None


async def run_verify_agent(
    *,
    spec: ModelSpec,
    system: str,
    task: str,
    sources: Iterable[str],
    output_schema: dict[str, Any],
    max_queries: int | None = 6,
    timeout_s: float | None = 120,
    max_cost_usd: float | None = None,
    daily_max_usd: float | None = None,
    max_steps: int | None = None,
    emit: Emit | None = None,
    run_id: str = "",
    node_id: str = "",
    data_versions: dict[str, Any] | Versions | None = Versions.UNSET,
) -> VerifyOutcome:
    """让一个只读 agent 查库，按 output_schema 交回有出处的结果。永远不抛。

    - spec：用哪个模型（一般是 api/settings.py 的 verify_model_spec）。system / task：交给模型的角色说明和任务。
    - sources：可以查的数据源名；不存在、停用了的照实记进 notes。
    - output_schema：调用方要的结果结构（JSON Schema 对象）。数值字段会换成格子引用交给模型、由代码取值。
    - 上限：max_queries（db_query 的执行次数，被拒的语句也算）、timeout_s（整个循环的时长，收尾另给宽限）、
      max_cost_usd（这一次）、daily_max_usd（全局每日，和裁判分开记账）；None 表示不限。max_steps 是模型最多想
      几轮，缺省按查询次数推。
    - emit：节点里调用时传 NodeContext.emit，每次模型调用发 llm.start / llm.end、每次工具调用发 tool.start /
      tool.end（role: verify，查询带 query_artifact）；不在节点里（质询接口）就不传，什么都不发。
    - run_id / node_id：查询快照、表结构快照记在谁名下。data_versions：上传表格用哪一版（运行固定的那份
      data_versions；不在运行里传 Versions.CURRENT；缺省 UNSET 时上传源一律报错，不拿当前版本顶上）。
    """
    started = time.monotonic()
    deadline = started + timeout_s if timeout_s is not None else None
    usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "calls": 0,
                             "unpriced_calls": 0}
    notes: list[str] = []
    queries: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    steps = max_steps if max_steps is not None else max(4, (max_queries if max_queries is not None else 6) * 2 + 2)
    owner = node_id or "verify"

    def left() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    def outcome(**fields: Any) -> VerifyOutcome:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        usage["cost_usd"] = round(usage["cost_usd"], 6)
        shown = [{k: q[k] for k in ("alias", "artifact", "sql", "row_count", "truncated", "source", "tool", "call_id")}
                 for q in queries]
        base: dict[str, Any] = {"result": None, "cells": [], "queries": shown, "rationale": None, "usage": usage,
                                "notes": notes, "duration_ms": int((time.monotonic() - started) * 1000)}
        return VerifyOutcome(**{**base, **fields})

    # ---- 模型
    try:
        async with SessionLocal() as session:
            base_model, model_id = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 核查模型没配好：这回核不了，照实交回
        why = not_configured(e) if isinstance(e, ProviderNotConfigured) else describe_exception(e)
        return outcome(error=f"核查模型不可用（{why}）")
    budget = _Budget(model_id, max_cost_usd, daily_max_usd)
    if not budget.priced and (max_cost_usd is not None or daily_max_usd is not None):
        note = (f"模型「{model_id}」不在价格目录中，无法按 token 数估算金额：金额上限（这次核查、每日）对其无效，"
                "费用只受查询次数和时长上限约束")
        notes.append(note)
        await _announce(emit, EventType.LOG, level="warn", code="verify_unpriced", message=note)

    # ---- 工具：只读的 db_schema / db_query
    wanted = list(dict.fromkeys(s for s in sources if isinstance(s, str) and s))
    ctx = ToolContext(run_id=run_id, node_id=node_id, data_versions=data_versions, emit=emit)
    async with SessionLocal() as session:
        tools = await build_datasource_tools([f"{p}{s}" for s in wanted for p in (SCHEMA_PREFIX, QUERY_PREFIX)], ctx,
                                             session, force_readonly=True)
    tool_map = {t.name: t for t in tools}
    if lost := [s for s in wanted if f"{QUERY_PREFIX}{s}" not in tool_map]:
        notes.append("数据源" + "、".join(f"「{s}」" for s in lost) + "不存在或已停用，核查没有查它")
    model = bind_tools_safely(base_model, tools, parallel=False)
    limit_note = f"查询次数有上限，本次最多 {max_queries} 次，用完就不能再查。" if max_queries is not None else ""
    head = "\n\n".join(p for p in (system.strip(), VERIFY_SYSTEM.format(limit=limit_note)) if p)
    messages: list[BaseMessage] = [SystemMessage(content=head), HumanMessage(content=task)]
    tool_text = "\n".join(f"{t.name}：{t.description}" for t in tools)

    async def ask(payload: list[BaseMessage], runner: Any, estimate: float, day: str, purpose: str,
                  seconds: float | None) -> tuple[Any, BaseMessage | None, BaseException | None]:
        """调一次模型并记账。返回 (结果, 原始回复, 异常)；超时的异常是 asyncio.TimeoutError。"""
        await _announce(emit, EventType.LLM_START, model=model_id, purpose=purpose, role="verify",
                        message_count=len(payload))
        began = time.perf_counter()
        got: Any = None
        raw: BaseMessage | None = None
        failure: BaseException | None = None
        try:
            got = await _bounded(runner(payload), seconds)
            raw = got if isinstance(got, BaseMessage) else _split_structured(got)[0]
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - 调用失败落在返回值里
            failure = e
        inp, out = _usage(raw)
        cost = await budget.settle(day, estimate, inp, out)
        usage["input_tokens"] += inp
        usage["output_tokens"] += out
        usage["cost_usd"] += cost
        usage["calls"] += 1
        usage["unpriced_calls"] += 0 if budget.priced else 1
        end = {"model": model_id, "purpose": purpose, "role": "verify",
               "duration_ms": int((time.perf_counter() - began) * 1000), "input_tokens": inp, "output_tokens": out,
               "total_tokens": inp + out, "cost_usd": round(cost, 6), "calls": 1}
        if failure is not None:
            end["error"] = "超过核查的时长上限，已放弃这次调用" if isinstance(failure, asyncio.TimeoutError) \
                else explain_model_error(failure, model_id)
        await _announce(emit, EventType.LLM_END, **end)
        return got, raw, failure

    # ---- 查库的循环
    stopped: str | None = None
    answered = False
    said = ""
    attempts = 0
    done: dict[str, str] = {}           # 成功过的查询（工具名 + 参数）→ 编号：一模一样的不再执行
    for _ in range(steps):
        remaining = left()
        if remaining is not None and remaining <= 0:
            stopped = "timeout_s"
            break
        estimate = budget.estimate(_text_of(messages) + tool_text, _OUT_STEP)
        day, refused = await budget.reserve(estimate, keep=budget.estimate(_text_of(messages), _OUT_FINAL))
        if refused:
            stopped = refused
            break
        response, _, failure = await ask(messages, model.ainvoke, estimate, day or "", "verify", remaining)
        if isinstance(failure, asyncio.TimeoutError):
            stopped = "timeout_s"
            break
        if failure is not None or not isinstance(response, BaseMessage):
            return outcome(model=model_id, error=explain_model_error(failure, model_id) if failure
                           else "核查模型没有给出回复")
        messages.append(response)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            answered, said = True, message_text(response)
            break
        said = message_text(response) or said
        for call in calls:
            name, args = str(call.get("name") or ""), dict(call.get("args") or {})
            call_id = str(call.get("id") or f"verify-{len(messages)}-{name}")
            if stopped:
                messages.append(ToolMessage(content=f"未执行：已达上限（{stopped}），不能再调用工具。", tool_call_id=call_id))
                continue
            tool = tool_map.get(name)
            if tool is None:
                messages.append(ToolMessage(content=f"错误：没有名为 {name} 的工具，可用的工具："
                                            + ("、".join(tool_map) or "（无）"), tool_call_id=call_id))
                continue
            schema = args_model_of(tool)
            if schema is not None:
                try:
                    args, _ = prepare_args(schema, args)
                except ToolArgsError as e:
                    await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, error=str(e), role="verify")
                    messages.append(ToolMessage(content=str(e), tool_call_id=call_id))
                    continue
            is_query = name.startswith(QUERY_PREFIX)
            fingerprint = f"{name}\n{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"
            if is_query and fingerprint in done:
                alias = done[fingerprint]
                messages.append(ToolMessage(content=f"这条查询和 {alias} 完全相同，结果见 {alias}，没有再执行。",
                                            tool_call_id=call_id))
                continue
            if is_query and max_queries is not None and attempts >= max_queries:
                stopped = "max_queries"
                messages.append(ToolMessage(content=f"未执行：已达查询次数上限（{max_queries} 次），不能再查询。",
                                            tool_call_id=call_id))
                continue
            remaining = left()
            if remaining is not None and remaining <= 0:
                stopped = "timeout_s"
                messages.append(ToolMessage(content="未执行：已达核查的时长上限，不能再查询。", tool_call_id=call_id))
                continue
            content = await _run_tool(tool, name, args, call_id, remaining, emit, queries, ledger, owner)
            if content is None:
                stopped = "timeout_s"
                content = "查询因核查的时长上限被中断，没有结果。"
            if is_query:
                attempts += 1
                if queries and queries[-1]["call_id"] == call_id:
                    done[fingerprint] = queries[-1]["alias"]
            messages.append(ToolMessage(content=content, tool_call_id=call_id))
        if not stopped and max_queries is not None and attempts >= max_queries:
            stopped = "max_queries"
        if stopped:
            break
    else:
        stopped = "max_steps"

    words = None
    if stopped:
        words = _limit_words(stopped, max_queries=max_queries, timeout_s=timeout_s, max_cost_usd=max_cost_usd,
                             daily_max_usd=daily_max_usd, steps=steps)
        await _announce(emit, EventType.LOG, level="warn", code="verify_limit", limit=stopped,
                        message=f"核查已达上限（{words}），已停止查询，按已有结果收尾")

    # ---- 收尾：按结构交结果
    schema = result_schema(output_schema)
    final = _final_messages(head, task, queries, said, words)
    estimate = budget.estimate(_text_of(final), _OUT_FINAL)
    day, refused = await budget.reserve(estimate)
    if refused:
        limit_words = _limit_words(refused, max_queries=max_queries, timeout_s=timeout_s, max_cost_usd=max_cost_usd,
                                   daily_max_usd=daily_max_usd, steps=steps)
        return outcome(model=model_id, limit=stopped or refused, answered=answered,
                       error=f"已达{limit_words}，没能交结果")
    remaining = left()
    grace = None if remaining is None else max(remaining, SETTLE_GRACE_S)
    finish = None if grace is None else time.monotonic() + grace
    try:
        async with SessionLocal() as session:
            settler, _ = await get_chat_model(session, spec.model_copy(update={"thinking": "off"}))
    except Exception as e:  # noqa: BLE001
        await spend.settle(day or "", -estimate)
        return outcome(model=model_id, limit=stopped, answered=answered, error=f"核查模型不可用（{describe_exception(e)}）")

    def until_finish() -> float | None:
        return None if finish is None else finish - time.monotonic()

    got, raw, failure = await ask(final, settler.with_structured_output(schema, include_raw=True).ainvoke, estimate,
                                  day or "", "verify_result", until_finish())
    parsed = None if failure is not None else _parsed(_split_structured(got)[1])
    if parsed is None and raw is not None:
        # 有的网关把 JSON 写在正文里、或者塞进了一个没按结构解析出来的工具调用
        for candidate in [message_text(raw), *(c.get("args") for c in getattr(raw, "tool_calls", None) or [])]:
            if (parsed := _parsed(candidate)) is not None:
                break
    if parsed is None and not isinstance(failure, asyncio.TimeoutError):
        # 不支持结构化输出的模型：普通调用一次，从回复里把 JSON 抠出来
        remaining = until_finish()
        if remaining is None or remaining > 0:
            estimate = budget.estimate(_text_of(final), _OUT_FINAL)
            day, refused = await budget.reserve(estimate)
            if not refused:
                got, raw, failure = await ask(final, settler.ainvoke, estimate, day or "", "verify_result", remaining)
                parsed = None if failure is not None else _parsed(message_text(got) if got is not None else None)
    if parsed is None:
        why = "收尾时超过时长上限，没能交结果" if isinstance(failure, asyncio.TimeoutError) else \
            f"核查模型没有按结构交结果（{explain_model_error(failure, model_id)}）" if failure is not None else \
            "核查模型没有按结构交结果"
        return outcome(model=model_id, limit=stopped, answered=answered, error=why)

    # ---- 取值、组装理由
    cells = _Cells(queries)
    result = cells.walk(output_schema, parsed) if _is_object(output_schema) else {}
    rationale = parsed.get(RATIONALE)
    doc = None
    if isinstance(rationale, str) and rationale.strip():
        doc = compose_doc(rationale, build_catalog(nodes={}, ledger=ledger), node_id=owner, run_id=run_id or None)
    result[RATIONALE] = doc["markdown"] if doc else ""
    return outcome(model=model_id, result=result, cells=cells.cells, rationale=doc, limit=stopped, answered=answered)


async def _run_tool(tool: Any, name: str, args: dict[str, Any], call_id: str, remaining: float | None,
                    emit: Emit | None, queries: list[dict[str, Any]], ledger: list[dict[str, Any]],
                    owner: str) -> str | None:
    """执行一次工具调用，返回交给模型的内容；整个核查的时长到点、这次被中断时返回 None。

    成功的查询编号（Q1、Q2…）、记进本 agent 自己的台账和查询清单，内容前面加「【证据 Qn】」。
    """
    limit = limit_of(tool, name, args)
    await _announce(emit, EventType.TOOL_START, tool=name, args=args, call_id=call_id, role="verify",
                    **limit_fields(limit))
    began = time.perf_counter()
    try:
        result = await _bounded(run_bounded(tool.ainvoke(args), limit, name), remaining)
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    except asyncio.TimeoutError:
        await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, role="verify", timed_out=True,
                        duration_ms=int((time.perf_counter() - began) * 1000),
                        error="查询因核查的时长上限被中断")
        return None
    except ToolTimeout as e:
        await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, role="verify", timed_out=True,
                        duration_ms=int((time.perf_counter() - began) * 1000), error=str(e))
        return str(e)
    except Exception as e:  # noqa: BLE001 - 工具失败交回模型，让它自己改
        message = f"工具执行失败：{describe_exception(e)}"
        await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, role="verify",
                        duration_ms=int((time.perf_counter() - began) * 1000), error=message[:300])
        return message
    elapsed = int((time.perf_counter() - began) * 1000)
    fields = query_entry_fields(content) if name.startswith(QUERY_PREFIX) else None
    extra: dict[str, Any] = {}
    if fields is not None:
        alias = f"Q{len(queries) + 1}"
        data = json.loads(content)
        schema = fields.get("schema_artifact")
        seen = schema and any(e.get("kind") == "schema" and e.get("artifact") == schema for e in ledger)
        frozen = schema_ledger_entry(schema, node_id=owner, exec_no=1) if schema and not seen else None
        shaped = {"schema_artifact": schema, "tables": fields["tables"]} if seen or frozen else {}
        ledger.append({"kind": "query", "node_id": owner, "exec": 1, "artifact": fields["artifact"],
                       "call_id": call_id, "tool": name, "source": fields["source"], "columns": fields["columns"],
                       "rows": fields["rows"], "truncated": fields["truncated"], **shaped})
        if frozen:
            ledger.append(frozen)
        queries.append({"alias": alias, "call_id": call_id, "tool": name, "source": fields["source"],
                        "sql": str(data.get("sql") or args.get("sql") or ""), "artifact": fields["artifact"],
                        "columns": fields["columns"], "rows": data.get("rows") or [], "row_count": fields["rows"],
                        "truncated": fields["truncated"]})
        extra = {"query_artifact": fields["artifact"], "alias": alias,
                 **({"schema_artifact": schema} if schema else {}),
                 **({"checks": data["checks"]} if isinstance(data.get("checks"), list) and data["checks"] else {})}
        content = f"【证据 {alias}】\n{content}"
    await _announce(emit, EventType.TOOL_END, tool=name, call_id=call_id, duration_ms=elapsed, preview=content[:2000],
                    role="verify", **extra)
    if len(content) > _TOOL_CHARS:
        content = content[:_TOOL_CHARS] + "\n…（结果太长，后面的省略了；完整结果在查询快照里，交结果时会按编号列出）"
    return content


__all__ = ["LIMITS", "RATIONALE", "SETTLE_GRACE_S", "VerifyOutcome", "result_schema", "run_verify_agent"]
