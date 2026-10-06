"""核查用的只读 agent：查库，交回有出处的结果。双盲复算、反方审稿、审批质询共用这一个。

三处要的是同一件事：另起一个模型，只给它几个数据源的只读查询工具，让它自己写 SQL 去核对一个数、一句结论、一个
提问，最后交回结构化的结果——而且结果里的数要能核对。

**只读。** 工具只建给定数据源的 db_schema / db_query，建的时候 force_readonly：可写源上的写语句也被守卫拒绝
（tools/datasource.py）。这一路没有审批关卡，不能指望人在半路拦下一条 DELETE。

**数从快照取。** 每次成功的查询是一份查询快照（工具自己落的 put_json），按这个 agent 自己的顺序编 Q1、Q2…。
最后交结果时，调用方结构里的数值字段一律改成格子引用 {alias, row, column}，由代码按引用从快照里取值：模型自己
写的数字不采信，取不到就是 None，不兜底成 0。数值字段的各种写法都认（type 列表、anyOf 里的数、$ref 引用进来的、
allOf 包着的）；认不出的结构下模型写的数一律丢掉、照记下来，宁可少一个数，不让它绕过格子引用。理由文字里的数字
要写成 [[v:Qn.rX.列]]，用本 agent 自己的台账（evidence.build_catalog）组装成文档（evidence.compose_doc），裸数字
会被标出来。

**不进写作目录。** 节点里调用时（给了 emit）每次工具调用发 tool.start / tool.end，查询的 tool.end 带
query_artifact 和 role: verify——随运行封存、证据面板能下钻。但这里绝不往 state["evidence"] 写 kind: query：
报告的写作目录按台账里的 query 编号，核查的查询混进去，写作者就会把它们当成可以引用的证据。调用方只拿到结果，
台账条目由它按自己的种类记（dual_check / challenge / inquiry）。

**上限。** 查询次数、时长、金额（这一次、全局每日）任何一项到顶就停止查询，按已经查到的结果收尾：收尾那次
调用总是要做的（时长到顶时再给 SETTLE_GRACE_S 的宽限），所以每一轮都按收尾最贵的样子给它留出钱——收尾时要把
查到的结果交给模型，结果有多长事先不知道，按 _digest 的上限算。留过钱的收尾不再被上限拦下：某一轮的实际用量
超出了估算、或者并行的核查先用掉了当天的余额时，宁可超出上限一次收尾调用的钱，也不让已经查到的结果白查。
每日上限不传就按设置里的「核查每日金额上限」，明确传 None 才是不限。模型不在价格目录里时估不出金额，金额上限
对它无效，照裁判的说法记一条说明。每次调用都记进核查的每日账（engine/spend.py）。

**输出额度。** 经 OpenAI 兼容接入的推理模型收不到 thinking=off，思考也算在输出额度里：额度按 spec（缺省 judge.MAX_TOKENS），
服务方嫌大时退回 judge.FALLBACK_MAX_TOKENS 再试一次，之后工具循环、收尾都用小的（providers/budget.py）。每次调用都看
有没有到了上限被截断：工具循环里被截断（话没说完、也没发查询）不算模型自己停下，一条查询都没成功时不再收尾；收尾被截断
时不再退回普通调用多花一次额度。因为截断没交成结果的，error 照实说是输出额度（TRUNCATED_ERROR），不说成「没查到」
「没按结构交」；节点里调用时另记一条 verify_truncated。

永远不抛：模型没配好、调用失败、格式不对、触顶、库写不进去，都落在返回值里（error / limit）。模型调用都经
get_chat_model，测试换成剧本。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core import artifact_store
from app.core.errors import describe_exception, not_configured
from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine import spend
from app.engine.evidence import build_catalog, cell_eid, compose_doc, query_entry_fields, schema_ledger_entry
from app.engine.expressions import CellError, column_kind, locate_cell, numeric_text
from app.engine.judge import FALLBACK_MAX_TOKENS, MAX_TOKENS, _json_in, _tokens, _usd
from app.engine.nodes.llm import _DIGEST_BUDGET, _digest, _split_structured, _typed, explain_model_error
from app.engine.state import message_text
from app.engine.toolcalls import ToolTimeout, limit_fields, limit_of, run_bounded
from app.providers import catalog as pricing
from app.providers.budget import BudgetFallback, cut_off, cut_words, error_usage, truncated_error
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
#: 收尾时带上的模型最后那段说明最多这么多字。收尾那次调用的长度有了上限，循环里才能按它把钱留够
_SAID_CHARS = 2_000
#: 收尾那次调用里查询结果最多占多少字：_digest 的上限，加上它截断时末尾那句说明
_DIGEST_ROOM = _DIGEST_BUDGET + 64
#: 留钱时给「已达上限（…）」那句话留的字数
_LIMIT_ROOM = 40
#: 本地 $ref 最多展开这么多层
_REF_DEPTH = 8
#: 结果里自动加上的理由字段（调用方的结构里已经有同名的字符串字段就沿用它）
RATIONALE = "rationale"
#: 触顶原因
LIMITS = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd", "max_steps")
#: 核查模型的输出到了上限被截断时给人看的话。TRUNCATED_ERROR 是没交成结果的原因：双盲复算的无法复算、反方审稿的「反查
#: 未完成」、质询的报错都照它说，也是 verify_truncated 日志的原话。TRUNCATED_SETTLED 是截断前查到过东西、照常收尾了的，
#: 记进 notes 和日志。cut 是 budget.cut_words 的「核查模型的输出达到上限（N token）被截断」
TRUNCATED_ERROR = "{cut}，核查未能完成。{advice}"
TRUNCATED_SETTLED = "{cut}，已按截断前查到的结果收尾，结果可能不完整。{advice}"
TRUNCATED_ADVICE = "通常是推理占满了输出额度，可在「设置 → 偏好设置 → 核查」中换用不带推理的模型"

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


class Default(Enum):
    """run_verify_agent 的 daily_max_usd 缺省值：按设置里的「核查每日金额上限」（api/settings.py 的
    verify_settings）。节点、质询接口不必各自记得去读；明确传 None 才是不限。"""

    SETTINGS = "settings"


@dataclass
class VerifyOutcome:
    """一次核查的结果。

    result：调用方结构的形状，数值字段是从快照取到的值（取不到为 None），另有 rationale（理由渲染后的正文）；
    没交成结果时为 None，error 说明为什么。cells：每个数值字段的出处 {path, status, ref, alias, row, column,
    artifact, eid, value}，status 是 resolved（value 是那一格的值，那一格本身是 NULL 时为 None）/ missing（模型
    交了 null：查不到）/ unresolved（reason 写原因：没交这个字段、引用指不到、指到的不是数；模型直接写了数的记
    model_value，非数值字段里写的数也照此记下）。queries：成功的查询 {alias, artifact, sql, row_count, truncated,
    source, tool, call_id}。
    rationale：理由组装成的报告文档（evidence.compose_doc 的形状，violations 里有裸数字）。usage：{input_tokens,
    output_tokens, total_tokens, cost_usd, calls, unpriced_calls}。limit：触顶原因（LIMITS 之一），没触顶为 None。
    answered：模型是不是自己停下来的（没被上限掐断，也没因为输出到了上限被截断）。因为截断没交成结果的，error 是
    TRUNCATED_ERROR；截断前查到过东西、照常交了结果的，error 为 None，notes 里记一句 TRUNCATED_SETTLED。
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


def _is_array(schema: Any) -> bool:
    return isinstance(schema, dict) and ("array" in _types(schema) or "items" in schema)


def _numeric_types(schema: Any) -> set[str] | None:
    """数值字段允许的类型（number / integer，连同写在一起的 null、string）；不是数值字段返回 None。

    type 写成 "number"、["number", "null"]，或者 anyOf / oneOf 里有一支是数（可为 null 的数一般这么写，
    Pydantic 的 Optional[float] 就是）都算：有一支能放数，整个字段就得是格子引用，不然数从那一支漏过去。
    """
    if not isinstance(schema, dict):
        return None
    types = set(_types(schema))
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key) or ():
            if isinstance(branch, dict) and not _is_object(branch) and not _is_array(branch):
                types |= _numeric_types(branch) or _types(branch)
    return types if types & {"number", "integer"} else None


def _has_numbers(schema: Any, depth: int = 0) -> bool:
    """结构里有没有数值字段（嵌套的也算）。"""
    if not isinstance(schema, dict) or depth > 32:
        return False
    if _numeric_types(schema) is not None:
        return True
    subs = [*(schema.get("properties") or {}).values(), schema.get("items"), schema.get("additionalProperties"),
            *(schema.get("anyOf") or ()), *(schema.get("oneOf") or ())]
    return any(_has_numbers(sub, depth + 1) for sub in subs if isinstance(sub, dict))


#: 值是「名字 → 结构」的键、值是一个结构的键、值是一串结构的键：展开引用时往里走；其余的键（enum、description…）原样
_SCHEMA_MAPS = ("properties", "patternProperties")
_SCHEMA_ONE = ("items", "additionalProperties", "contains", "not")
_SCHEMA_LIST = ("anyOf", "oneOf", "prefixItems")


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """allOf 的两支并成一个节点：属性合在一起，必填合在一起，类型取并集（只用来认是不是数、是不是对象）。"""
    out = dict(a)
    for key, value in b.items():
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {**(out.get("properties") or {}), **value}
        elif key == "required" and isinstance(value, list):
            out["required"] = list(dict.fromkeys([*(out.get("required") or []), *value]))
        elif key == "type":
            merged = _types(out) | _types(b)
            out["type"] = next(iter(merged)) if len(merged) == 1 else sorted(merged)
        else:
            out.setdefault(key, value)
    return out


def _inline(schema: Any) -> dict[str, Any]:
    """展开调用方结构里的本地引用（$ref: #/$defs/…、#/definitions/…），allOf 并成一个节点，去掉 $defs。

    后面换格子引用、取值只认 type / properties / items / anyOf 这几种写法：引用不展开，引用进来的数值字段就绕过了
    格子引用这一关（Pydantic 生成的结构里，嵌套的对象全是 $ref）。引用不到的、绕回自己的（树形结构第二次遇到同一个
    引用）就地断开，当作认不出的结构：模型在那里写的数照样不采信。
    """
    root = schema if isinstance(schema, dict) else {}

    def target(ref: Any) -> dict[str, Any] | None:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else None

    def expand(node: Any, trail: tuple[str, ...]) -> Any:
        while isinstance(node, dict) and "$ref" in node:
            ref, rest = node["$ref"], {k: v for k, v in node.items() if k != "$ref"}
            found = target(ref)
            if found is None or ref in trail or len(trail) >= _REF_DEPTH:
                node = rest
                break
            node, trail = {**found, **rest}, (*trail, ref)
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "definitions", "allOf"):
                continue
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                out[key] = {k: expand(v, trail) for k, v in value.items()}
            elif key in _SCHEMA_ONE and isinstance(value, dict):
                out[key] = expand(value, trail)
            elif key in _SCHEMA_LIST and isinstance(value, list):
                out[key] = [expand(v, trail) for v in value]
            else:
                out[key] = value
        for part in node.get("allOf") or ():
            if isinstance(part, dict):
                out = _merge(out, expand(part, trail))
        return out

    return expand(root, ())


def result_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """交给模型的结构：调用方结构里的数值字段（见 _numeric_types；嵌套的对象、数组、anyOf 的分支里的也算，引用先
    展开）一律换成可为 null 的格子引用，另加一个理由字段（已有同名的字符串字段就沿用）。其余字段原样。"""

    def rewrite(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if _numeric_types(node) is not None:
            out: dict[str, Any] = {"anyOf": [_CELL_REF, {"type": "null"}]}
            if node.get("description"):
                out["description"] = f"{node['description']}（写格子引用，查不到写 null）"
            return out
        out = dict(node)
        if isinstance(node.get("properties"), dict):
            out["properties"] = {k: rewrite(v) for k, v in node["properties"].items()}
            out.setdefault("type", "object")
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                out[key] = rewrite(node[key])
        for key in ("anyOf", "oneOf"):
            if isinstance(node.get(key), list):
                out[key] = [rewrite(b) for b in node[key]]
        return out

    inlined = _inline(schema)
    root = rewrite(inlined if _is_object(inlined) else {"type": "object", "properties": {}})
    props = dict(root.get("properties") or {})
    props.setdefault(RATIONALE, _RATIONALE_FIELD)
    required = list(dict.fromkeys([*(root.get("required") or []), RATIONALE]))
    # 走工具调用的结构化输出要求函数名只含字母、数字、下划线
    return {**root, "title": "verify_result", "type": "object", "properties": props, "required": required}


@dataclass(frozen=True)
class _Gap:
    """模型交的结果里这个位置没有值。reason 说为什么：没交这个字段、上一层的格式不对。"""

    reason: str


_LEFT_OUT = _Gap("模型没有交这个字段")


def _fits(branch: dict[str, Any], value: Any) -> bool:
    """anyOf / oneOf 的这一支收不收这个值（按形状认）。"""
    if isinstance(value, _Gap):
        return _has_numbers(branch)
    if isinstance(value, dict):
        return _is_object(branch)
    if isinstance(value, list):
        return _is_array(branch)
    if value is None:
        return "null" in _types(branch)
    if isinstance(value, bool):
        return "boolean" in _types(branch)
    if isinstance(value, str):
        return "string" in _types(branch) or "enum" in branch or "const" in branch
    # 数：能放数的分支已经整个换成了格子引用（_numeric_types），这里只剩码值里的数。True == 1，真假不算数
    choices = [*(branch.get("enum") or ()), *([branch["const"]] if "const" in branch else [])]
    return any(c == value and not isinstance(c, bool) for c in choices)


def _decoded(value: Any, kind: type) -> Any:
    """有的模型把嵌套的对象、数组写成一段 JSON 文本（裁判见过同样的事，judge._items_of）：解得开、形状对就用解开的。"""
    if isinstance(value, str) and isinstance(found := _json_in(value), kind):
        return found
    return value


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

    def take(self, value: Any, path: str, types: set[str]) -> Any:
        """一个数值字段：按引用取值，记下出处，返回值（取不到为 None）。types 是字段允许的类型：不许文本的，
        引用的格子不是数（日期、名称、真假）就不采信——拿它和原流程的数比，比较本身就会出错。"""
        if isinstance(value, _Gap):
            self.cells.append({"path": path, "status": "unresolved", "reason": value.reason})
            return None
        if value is None:
            self.cells.append({"path": path, "status": "missing"})
            return None
        value = _decoded(value, dict)
        if not isinstance(value, dict):
            # 模型直接写了数：不采信。照记下来，界面上能看到它想填什么
            number = isinstance(value, (int, float)) or numeric_text(value, loose=True) is not None
            self.cells.append({"path": path, "status": "unresolved", "model_value": value,
                               "reason": "模型直接写了数值，没有给出格子引用，不采信" if number
                               else "格子引用的格式不对（应写成 {alias, row, column}），不采信"})
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
        truth = _typed(raw, {"type": sorted(types)}, column_kind(content, name))
        cited = {"ref": f"{alias}.r{row}.{name}", "alias": alias, "row": row, "column": name,
                 "artifact": query["artifact"], "eid": cell_eid(query["artifact"], row, name), "value": truth}
        if truth is not None and "string" not in types and (isinstance(truth, bool)
                                                              or not isinstance(truth, (int, float))):
            self.cells.append({"path": path, "status": "unresolved", **cited,
                               "reason": f"{alias} 第 {row} 行「{name}」的值是「{truth}」，不是数值，不采信"})
            return None
        self.cells.append({"path": path, "status": "resolved", **cited})
        return truth

    def walk(self, schema: Any, value: Any, path: str = "") -> Any:
        """按调用方的结构（_inline 展开过的）走一遍模型交的结果：数值字段换成从快照取到的值，其余原样。"""
        types = _numeric_types(schema)
        if types is not None:
            return self.take(value, path, types)
        if not isinstance(schema, dict):
            return self.scrub({}, value, path)
        branches = [b for key in ("anyOf", "oneOf") for b in schema.get(key) or () if isinstance(b, dict)]
        if branches:
            rest = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}
            if any(_is_object(b) for b in branches):
                value = _decoded(value, dict)
            if any(_is_array(b) for b in branches):
                value = _decoded(value, list)
            if value is None and any("null" in _types(b) for b in branches):
                return None
            chosen = next((b for b in branches if _fits(b, value)), None)
            return self.walk({**rest, **chosen} if chosen is not None else rest, value, path)
        if _is_object(schema):
            return self._object(schema, value, path)
        if _is_array(schema):
            return self._array(schema, value, path)
        return self.scrub(schema, value, path)

    def _object(self, schema: dict[str, Any], value: Any, path: str) -> dict[str, Any]:
        given = _decoded(value, dict)
        if isinstance(given, dict):
            def child(key: str) -> Any:
                return given[key] if key in given else _LEFT_OUT
        else:
            gap = given if given is None or isinstance(given, _Gap) else _Gap("模型交的格式不对：这里应是一个对象")

            def child(key: str) -> Any:
                return gap
        props = schema.get("properties") or {}
        out = {k: self.walk(sub, child(k), f"{path}.{k}" if path else k) for k, sub in props.items()}
        extra = schema.get("additionalProperties")
        for k, v in (given.items() if isinstance(given, dict) else ()):
            if k in props:
                continue
            if isinstance(extra, dict):
                out[k] = self.walk(extra, v, f"{path}.{k}" if path else k)
            elif extra is True or (not props and extra is not False):
                # 没写属性的对象（任意键）照收，里面的数照样不采信；写了属性的，没声明的键不要
                out[k] = self.scrub({}, v, f"{path}.{k}" if path else k)
        return out

    def _array(self, schema: dict[str, Any], value: Any, path: str) -> list[Any] | None:
        items = _decoded(value, list)
        if isinstance(items, list):
            sub = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            return [self.walk(sub, v, f"{path}[{i}]") for i, v in enumerate(items)]
        if _has_numbers(schema):
            if items is None:
                self.cells.append({"path": path, "status": "missing"})
            elif isinstance(items, _Gap):
                self.cells.append({"path": path, "status": "unresolved", "reason": items.reason})
            else:
                self.cells.append({"path": path, "status": "unresolved", "model_value": items,
                                   "reason": "模型交的格式不对：这里应是一个数组"})
        elif items is not None and not isinstance(items, _Gap):
            self.scrub({}, items, path)
        return None

    def scrub(self, schema: Any, value: Any, path: str) -> Any:
        """认不出的结构（没写类型、文本字段、没写 items 的数组……）下的值：文本、真假、null 原样，数一律不采信、
        照记下来（码值里的数是调用方给的选项，不是查出来的数，照收），对象、数组逐项照此处理。"""
        if isinstance(value, _Gap):
            return None
        if value is None or isinstance(value, (bool, str)):
            return value
        if isinstance(value, (int, float)):
            if isinstance(schema, dict) and _fits(schema, value):
                return value
            self.cells.append({"path": path, "status": "unresolved", "model_value": value,
                               "reason": "这里不是数值字段，模型却写了数值，不采信"})
            return None
        if isinstance(value, dict):
            return {k: self.scrub({}, v, f"{path}.{k}" if path else k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub({}, v, f"{path}[{i}]") for i, v in enumerate(value)]
        return None


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

    def estimate(self, text: str, out: int, *, room: int = 0) -> float:
        """按文本估一次调用的金额。room 是还不知道内容、按最坏情况（一个字一个 token）算的输入字数。"""
        return pricing.estimate_cost(self.model_id, _tokens(text) + room, out) if self.priced else 0.0

    async def reserve(self, estimate: float, *, keep: float = 0.0, kept: bool = False
                      ) -> tuple[str | None, str | None]:
        """(记在哪天, None) 或 (None, 拦下的上限)。

        keep：要额外留出的钱（收尾那次调用最贵的样子），只用来判断、不预留。kept：这就是循环里一直给它留着钱的
        那次收尾，不再按上限拦，照记账：每一轮都留够了，还超出只能是某一轮实际用得比估算多，或者并行的核查先用掉了
        当天的余额（每日余额只是比了一下、没有真的预留）。这时宁可超出上限一次收尾调用的钱，也不让查到的结果白查。
        """
        cap = self.daily_max_usd if self.priced and not kept else None
        if not kept:
            if self.priced and self.max_cost_usd is not None and \
                    self.spent + estimate + keep > self.max_cost_usd + 1e-12:
                return None, "max_cost_usd"
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


def _final_messages(system: str, task: str, queries: list[dict[str, Any]], said: str, limit_words: str | None
                    ) -> list[BaseMessage]:
    """收尾那次调用的消息：任务、查到的结果（每次查询列出行号）、模型最后说的话，以及交结果的规则。
    limit_words：触顶时是哪个上限（「最多查询 6 次」），告诉模型为什么不能再查。

    长度有上限（任务和规则之外，查询结果按 _digest 的上限、说明按 _SAID_CHARS）：循环里按这个上限给收尾留钱。
    """
    parts = [f"任务：\n{task.strip()}"]
    if limit_words:
        parts.append(f"已达上限（{limit_words}），不能再查询，按已经查到的结果交结果。")
    parts.append(f"你查到的结果：\n{_digest(queries) if queries else '（没有成功的查询）'}")
    parts.append(f"你最后的说明（只作参考，数以查询结果为准）：\n{said.strip()[:_SAID_CHARS] or '（没有）'}")
    parts.append(VERIFY_FINAL_PROMPT)
    return [SystemMessage(content=system), HumanMessage(content="\n\n".join(parts))]


def _plain_messages(final: list[BaseMessage], schema: dict[str, Any]) -> list[BaseMessage]:
    """普通调用（不走结构化输出）的消息：结构不在请求里了，要写进消息，不然模型不知道要交哪些字段、字段叫什么，
    交回来的字段对不上，全成了没交。"""
    *head, last = final
    text = (f"{last.content}\n\n只交一个 JSON 对象，不要写别的内容。对象的结构（JSON Schema）：\n"
            f"```json\n{json.dumps(schema, ensure_ascii=False)}\n```")
    return [*head, HumanMessage(content=text)]


def _parsed(value: Any) -> dict[str, Any] | None:
    """结构化结果：对象；有的模型把整个对象写成一段 JSON 文本，也认。"""
    if isinstance(value, str):
        value = _json_in(value)
    return value if isinstance(value, dict) else None


@dataclass
class _State:
    """一次核查过程中攒下的东西：出意外时也照这些交回，用了多少、查了什么不丢。"""

    started: float
    usage: dict[str, Any] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "calls": 0, "unpriced_calls": 0})
    notes: list[str] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None

    def outcome(self, **fields: Any) -> VerifyOutcome:
        usage = self.usage
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        usage["cost_usd"] = round(usage["cost_usd"], 6)
        shown = [{k: q[k] for k in ("alias", "artifact", "sql", "row_count", "truncated", "source", "tool", "call_id")}
                 for q in self.queries]
        base: dict[str, Any] = {"result": None, "cells": [], "queries": shown, "rationale": None, "usage": usage,
                                "notes": self.notes, "model": self.model,
                                "duration_ms": int((time.monotonic() - self.started) * 1000)}
        return VerifyOutcome(**{**base, **fields})


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
    daily_max_usd: float | None | Default = Default.SETTINGS,
    max_steps: int | None = None,
    emit: Emit | None = None,
    run_id: str = "",
    node_id: str = "",
    data_versions: dict[str, Any] | Versions | None = Versions.UNSET,
) -> VerifyOutcome:
    """让一个只读 agent 查库，按 output_schema 交回有出处的结果。永远不抛。

    - spec：用哪个模型（一般是 api/settings.py 的 verify_model_spec）。system / task：交给模型的角色说明和任务。
    - sources：可以查的数据源名；不存在、停用了的照实记进 notes。
    - output_schema：调用方要的结果结构（JSON Schema 对象，可以带 $defs / $ref）。数值字段会换成格子引用交给模型、
      由代码取值。
    - 上限：max_queries（db_query 的执行次数，被拒的语句也算）、timeout_s（整个循环的时长，收尾另给宽限）、
      max_cost_usd（这一次）、daily_max_usd（全局每日，和裁判分开记账；缺省按设置里的「核查每日金额上限」）；
      None 表示不限。max_steps 是模型最多想几轮，缺省按查询次数推。
    - emit：节点里调用时传 NodeContext.emit，每次模型调用发 llm.start / llm.end、每次工具调用发 tool.start /
      tool.end（role: verify，查询带 query_artifact）；不在节点里（质询接口）就不传，什么都不发。
    - run_id / node_id：查询快照、表结构快照记在谁名下。data_versions：上传表格用哪一版（运行固定的那份
      data_versions；不在运行里传 Versions.CURRENT；缺省 UNSET 时上传源一律报错，不拿当前版本顶上）。
    """
    state = _State(started=time.monotonic())
    try:
        return await _verify(state, spec=spec, system=system, task=task, sources=sources,
                             output_schema=output_schema, max_queries=max_queries, timeout_s=timeout_s,
                             max_cost_usd=max_cost_usd, daily_max_usd=daily_max_usd, max_steps=max_steps, emit=emit,
                             run_id=run_id, node_id=node_id, data_versions=data_versions)
    except Exception as e:  # noqa: BLE001 - 永远不抛：库写不进去、建工具出错这类意外也落在返回值里
        logger.warning("核查出错", exc_info=True)
        return state.outcome(error=f"核查出错（{describe_exception(e)}）")


async def _verify(state: _State, *, spec: ModelSpec, system: str, task: str, sources: Iterable[str],
                  output_schema: dict[str, Any], max_queries: int | None, timeout_s: float | None,
                  max_cost_usd: float | None, daily_max_usd: float | None | Default, max_steps: int | None,
                  emit: Emit | None, run_id: str, node_id: str,
                  data_versions: dict[str, Any] | Versions | None) -> VerifyOutcome:
    deadline = state.started + timeout_s if timeout_s is not None else None
    usage, notes, queries, ledger = state.usage, state.notes, state.queries, state.ledger
    outcome = state.outcome
    steps = max_steps if max_steps is not None else max(4, (max_queries if max_queries is not None else 6) * 2 + 2)
    owner = node_id or "verify"

    def left() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    # ---- 模型。额度没写的按 MAX_TOKENS：推理模型的思考算在输出额度里，4096 常常想完就没额度发查询、交结果了
    spec = spec.model_copy(update={"max_tokens": spec.max_tokens or MAX_TOKENS})
    small = FALLBACK_MAX_TOKENS if spec.max_tokens > FALLBACK_MAX_TOKENS else None
    try:
        async with SessionLocal() as session:
            first, model_id = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 核查模型没配好：这回核不了，照实交回
        why = not_configured(e) if isinstance(e, ProviderNotConfigured) else describe_exception(e)
        return outcome(error=f"核查模型不可用（{why}）")
    base_model = _with_fallback(first, spec, small)
    state.model = model_id
    if daily_max_usd is Default.SETTINGS:
        from app.api.settings import verify_settings   # 在这里导入：api.settings 导入了 engine 里的模块

        async with SessionLocal() as session:
            daily_max_usd = (await verify_settings(session))["daily_max_usd"]
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
    resolved = _inline(output_schema)
    schema = result_schema(resolved)
    # 结构也算输入：结构化输出时它是工具定义，退回普通调用时写在消息里
    schema_text = json.dumps(schema, ensure_ascii=False)
    # 收尾那次最贵的样子：查询结果、最后的说明都按上限、按一个字一个 token 算。查询结果要到查完才知道多长，
    # 按眼下的消息估，等最后一条查询的结果进来就不够了（大段中文的结果尤其如此）
    keep = budget.estimate(_text_of(_final_messages(head, task, [], "", "上" * _LIMIT_ROOM)) + schema_text,
                           _OUT_FINAL, room=_DIGEST_ROOM + _SAID_CHARS)

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
        # openai 的结构化输出被截断时抛异常，用量在异常里：额度是真花出去了
        inp, out = _usage(raw) if raw is not None else error_usage(failure)
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
        if truncated_error(failure) or cut_off(raw):
            # 到了上限被截断：不是「调用失败，请稍后重试」，重试解决不了额度问题
            limit = base_model.max_tokens
            end.update(truncated=True, max_tokens=limit, error=cut_words("核查模型", limit))
        await _announce(emit, EventType.LLM_END, **end)
        return got, raw, failure

    # ---- 查库的循环
    stopped: str | None = None
    answered = False
    kept = False                        # 有一轮按收尾最贵的样子留过钱：收尾不再按金额上限拦
    said = ""
    attempts = 0
    done: dict[str, str] = {}           # 成功过的查询（工具名 + 参数）→ 编号：一模一样的不再执行
    cut: tuple[str, int | None] | None = None   # 最近一次因为输出到了上限被截断的调用：(哪一步, 那次的额度)
    for _ in range(steps):
        remaining = left()
        if remaining is not None and remaining <= 0:
            stopped = "timeout_s"
            break
        estimate = budget.estimate(_text_of(messages) + tool_text, _OUT_STEP)
        day, refused = await budget.reserve(estimate, keep=keep)
        if refused:
            stopped = refused
            break
        kept = True
        response, _, failure = await ask(messages, model.ainvoke, estimate, day or "", "verify", remaining)
        if isinstance(failure, asyncio.TimeoutError):
            stopped = "timeout_s"
            break
        if truncated_error(failure):
            cut = ("verify", base_model.max_tokens)
            break
        if failure is not None or not isinstance(response, BaseMessage):
            return outcome(error=explain_model_error(failure, model_id) if failure else "核查模型没有给出回复")
        messages.append(response)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            if cut_off(response):
                # 话没说完、也没发查询就到了上限（推理模型想完了额度也用完了）：不是模型自己停下来的，不能当成说完了去收尾
                cut, said = ("verify", base_model.max_tokens), message_text(response) or said
                break
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
            args_schema = args_model_of(tool)
            if args_schema is not None:
                try:
                    args, _ = prepare_args(args_schema, args)
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

    def words_of(reason: str) -> str:
        return _limit_words(reason, max_queries=max_queries, timeout_s=timeout_s, max_cost_usd=max_cost_usd,
                            daily_max_usd=daily_max_usd, steps=steps)

    words = words_of(stopped) if stopped else None
    if words:
        await _announce(emit, EventType.LOG, level="warn", code="verify_limit", limit=stopped,
                        message=f"核查已达上限（{words}），已停止查询，按已有结果收尾")

    async def report_cut(delivered: bool) -> str:
        """被截断过：记一条 verify_truncated，返回给人看的话（没交成结果的原因，或者交了结果时记进 notes 的说明）。"""
        purpose, limit = cut or ("verify", None)
        text = (TRUNCATED_SETTLED if delivered else TRUNCATED_ERROR).format(cut=cut_words("核查模型", limit),
                                                                            advice=TRUNCATED_ADVICE)
        await _announce(emit, EventType.LOG, level="warn", code="verify_truncated", purpose=purpose, max_tokens=limit,
                        delivered=delivered, message=text)
        return text

    async def give_up(error: str, limit: str | None = None) -> VerifyOutcome:
        """没交成结果，原因照 error 说。工具循环里被截断过的，时间线上照记一条：收尾失败可能就是它带出来的。"""
        if cut is not None:
            await report_cut(False)
        return outcome(limit=limit or stopped, answered=answered, error=error)

    if cut is not None and not queries:
        # 一条查询都没成功就被截断：收尾只能交「查不到」，双盲复算会记成「盲算没有查到这个数」，原因却是输出额度。
        # 收尾那次多半同样被推理占满，不再为它多花一次额度，直接交回截断的原因
        return outcome(limit=stopped, answered=answered, error=await report_cut(False))

    # ---- 收尾：按结构交结果。关掉 thinking：结构化输出靠强制工具调用，和思考模式不兼容（经 OpenAI 兼容接入的推理模型
    # 关不掉，所以额度和工具循环一样，退回小额度也和工具循环共用一个开关）
    settle_spec = spec.model_copy(update={"thinking": "off"})
    try:
        async with SessionLocal() as session:
            settle_model, _ = await get_chat_model(session, settle_spec)
    except Exception as e:  # noqa: BLE001
        return await give_up(f"核查模型不可用（{describe_exception(e)}）")
    settler = _with_fallback(settle_model, settle_spec, small, shared_with=base_model)
    final = _final_messages(head, task, queries, said, words)
    try:
        structured = settler.with_structured_output(schema, include_raw=True)
    except Exception:  # noqa: BLE001 - 模型不支持结构化输出：直接走普通调用，不白算一次调用
        structured = None
    remaining = left()
    grace = None if remaining is None else max(remaining, SETTLE_GRACE_S)
    finish = None if grace is None else time.monotonic() + grace

    def until_finish() -> float | None:
        return None if finish is None else finish - time.monotonic()

    parsed: dict[str, Any] | None = None
    failure: BaseException | None = None
    settle_cut = False                  # 收尾那次到了上限被截断
    if structured is not None:
        estimate = budget.estimate(_text_of(final) + schema_text, _OUT_FINAL)
        day, refused = await budget.reserve(estimate, kept=kept)
        if refused:
            return await give_up(f"已达{words_of(refused)}，没能交结果", refused)
        got, raw, failure = await ask(final, structured.ainvoke, estimate, day or "", "verify_result", until_finish())
        parsed = None if failure is not None else _parsed(_split_structured(got)[1])
        if parsed is None and raw is not None:
            # 有的网关把 JSON 写在正文里、或者塞进了一个没按结构解析出来的工具调用
            for candidate in [message_text(raw), *(c.get("args") for c in getattr(raw, "tool_calls", None) or [])]:
                if (parsed := _parsed(candidate)) is not None:
                    break
        settle_cut = parsed is None and (truncated_error(failure) or bool(cut_off(raw)))
    if parsed is None and not isinstance(failure, asyncio.TimeoutError) and not settle_cut:
        # 不支持结构化输出的模型：普通调用一次，结构写进消息，从回复里把 JSON 抠出来。结构化那次已经做过的，
        # 这一次是补救，照常按上限拦；没做过的，这一次就是收尾，循环里留过钱。结构化那次是被截断的不补救：
        # 推理占满额度时普通调用多半一样被截断，白花一次额度
        plain = _plain_messages(final, schema)
        remaining = until_finish()
        if remaining is None or remaining > 0:
            estimate = budget.estimate(_text_of(plain), _OUT_FINAL)
            day, refused = await budget.reserve(estimate, kept=kept and structured is None)
            if refused and structured is None:
                return await give_up(f"已达{words_of(refused)}，没能交结果", refused)
            if not refused:
                got, _, failure = await ask(plain, settler.ainvoke, estimate, day or "", "verify_result", remaining)
                parsed = None if failure is not None else _parsed(message_text(got) if got is not None else None)
                settle_cut = parsed is None and (truncated_error(failure) or bool(cut_off(got)))
    if parsed is None:
        if settle_cut:
            cut = ("verify_result", settler.max_tokens)
            return outcome(limit=stopped, answered=answered, error=await report_cut(False))
        why = "收尾时超过时长上限，没能交结果" if isinstance(failure, asyncio.TimeoutError) else \
            f"核查模型没有按结构交结果（{explain_model_error(failure, model_id)}）" if failure is not None else \
            "核查模型没有按结构交结果"
        return await give_up(why)
    declared = list((resolved.get("properties") or {}) if _is_object(resolved) else {})
    if declared and not any(k in parsed for k in declared):
        # 一个要求的字段都没交：不是「查不到」，是没按结构交。不这么分，字段名对不上的回复会被当成什么都没查到
        return await give_up("核查模型没有按结构交结果（交回的内容里没有要求的字段："
                             + "、".join(declared[:8]) + ("等" if len(declared) > 8 else "") + "）")

    # ---- 取值、组装理由
    cells = _Cells(queries)
    result = cells.walk(resolved, parsed) if _is_object(resolved) else {}
    if cut is not None and any(c["status"] != "resolved" for c in cells.cells):
        # 工具循环被截断，截断前查到的里面没有要的数（或者引用指不到）：话没说完就停了，说「没查到」会让人以为数据里
        # 没有。不交这份残缺的结果，原因照截断说；查过的照记
        return outcome(cells=cells.cells, limit=stopped, answered=answered, error=await report_cut(False))
    rationale = parsed.get(RATIONALE)
    doc = None
    if isinstance(rationale, str) and rationale.strip():
        doc = compose_doc(rationale, build_catalog(nodes={}, ledger=ledger), node_id=owner, run_id=run_id or None)
    result[RATIONALE] = doc["markdown"] if doc else ""
    if cut is not None:
        # 截断前查到的够用：照常交结果，说明记进 notes，时间线上照记一条（结果可能不完整）
        notes.append(await report_cut(True))
    return outcome(result=result, cells=cells.cells, rationale=doc, limit=stopped, answered=answered)


def _with_fallback(model: Any, spec: ModelSpec, small: int | None, *, shared_with: BudgetFallback | None = None
                   ) -> BudgetFallback:
    """按 spec 建好的模型包一层：服务方嫌额度大时按 small 重建一个再调，要退回时才建，平常一个都不多建。small 为 None
    是额度本来就不比它大，没有可退的。经本模块的 get_chat_model 建，测试换成剧本时小额度的那个也是剧本。"""

    async def build() -> Any:
        async with SessionLocal() as session:
            return (await get_chat_model(session, spec.model_copy(update={"max_tokens": small})))[0]

    return BudgetFallback(model, build=build if small else None, limits=(spec.max_tokens, small or spec.max_tokens),
                          shared_with=shared_with)


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
    except ToolTimeout as e:
        # 这条查询自己的时限到了：交回原因让模型改写，核查照常往下走。要排在下一条前面：ToolTimeout 也是
        # TimeoutError（asyncio.TimeoutError 就是它），排在后面就被当成整个核查的时长到了
        await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, role="verify", timed_out=True,
                        duration_ms=int((time.perf_counter() - began) * 1000), error=str(e))
        return str(e)
    except asyncio.TimeoutError:
        await _announce(emit, EventType.TOOL_ERROR, tool=name, call_id=call_id, role="verify", timed_out=True,
                        duration_ms=int((time.perf_counter() - began) * 1000),
                        error="查询因核查的时长上限被中断")
        return None
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


__all__ = ["LIMITS", "RATIONALE", "SETTLE_GRACE_S", "Default", "VerifyOutcome", "result_schema", "run_verify_agent"]
