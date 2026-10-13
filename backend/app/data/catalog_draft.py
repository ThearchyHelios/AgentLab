"""数据目录的模型起草：分段、按失败原因处理、捡回被截断的部分、记用量。

对外的名字留在 data/catalog.py（draft_with_model、draft_catalog、write_draft、CATALOG_DRAFT_SYSTEM、各个 MODEL_*
常量……），这里是实现。常量一律在调用时按 catalog.X 读：测试改 catalog 上的常量就能生效。

**为什么要分段、按原因处理。** 实测（经 OpenAI 兼容网关的会思考的模型）：思考算在输出额度里，一段上百列时思考就把
额度全用掉，正文 0 字或只给一个空壳；同一批表有时成功有时失败，看模型这次想多久。以前失败了退回纯文本、发同一个提示词，
同样失败、耗时翻倍；超时把第一次拿到的部分也丢掉；一批里有一张表是空的就整批重问。

**分段。** 每次调用最多 MODEL_BATCH_TABLES 张表、合计 MODEL_BATCH_COLUMNS 列：列数不超过它的表按顺序拼在一起
（整表段）；超过它的宽表拆成「表级一段 + 列分段」——表级那段给全部列名和类型做背景、只要表级的项（中文名、粒度、
表类型、业务主键、业务日期）；列分段每段 MODEL_BATCH_COLUMNS 列，带着表名、表注释和表级起草结果做背景，只要这几列。
最后合并成这张表的一份，照旧经 catalog._from_model 逐项校验（起草要求不能让模型绕过校验）。

**按失败原因处理，不原样重发。**

- 思考加输出用完额度（finish_reason=length、LengthFinishReasonError、正文为空而有思考）、只给空壳：把这一段拆成
  两半再问（多表的段按表拆，单表的按列拆），最小 MODEL_SPLIT_MIN_COLUMNS 列，最多拆 MODEL_SPLIT_DEPTH 层。
  拆不开的空壳（结构化输出给的）换纯文本问一次：有的网关的结构化输出只给空壳、纯文本却能写——写出来了，这次起草
  后面都走纯文本。
- 被截断的 JSON：先把已经完整的表、列对象捡回来，只补剩下的。
- 一段里缺了的表、列只补缺的，补一次。补过仍然没给的列算模型拿不准、留空，不算没写出来；整张表一项都没有才算。
- 结构化输出这个接口不支持（400 / 422 提到 response_format、json_schema、tool……，或者模型类不支持）：这次起草
  后面改走纯文本、从正文取 JSON（纯文本那条路另外说清 JSON 的格式）。
- 服务方嫌额度太大：退回 MODEL_FALLBACK_MAX_TOKENS 重来，这次起草后面都用它（catalog.DraftModel.shrink）。
- 网络错误、429、5xx、超时：等一会儿重试（最多 MODEL_RETRIES 次，退避）。单次调用的时限是 MODEL_TIMEOUT_S。SDK
  自己不再重试（catalog.resolve_draft_model 建模型时 max_retries=0），实际重试次数和报错写的一致。
- 别的错误：这一段没写出来，原因照实写。

**并发。** 全进程最多 MODEL_CONCURRENCY 个调用同时进行（model_gate：按事件循环各一个信号量，所有起草任务和同步
起草共用）。

**进度。** 调用方给 Hooks：开始调用、每次调用的用量、一段结束、一张表的模型部分全部结束。后台起草任务据此落库，
每张表一结束就并入目录（data/catalog_jobs.py）。

交给模型的只有表结构（列、类型、主键、外键、唯一约束、注释）和使用者补充的背景，不发任何数据值。写给模型的文字都在
catalog.py 名字带 _SYSTEM / _PROMPT 的常量里；这里的 REASON_* 是给人看的（拼进这张表的 model_error）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.data import catalog as C
from app.data.names import name_key

logger = logging.getLogger(__name__)

#: 退避等待用的 sleep（测试里换掉，不真等）
_sleep = asyncio.sleep

#: 服务方嫌额度太大时报错里会出现的字样（同 copilot._BUDGET_WORDS、recipe_ai._BUDGET_WORDS，有测试核对）
BUDGET_WORDS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maximum context length")
#: 结构化输出这个接口不支持时报错里会出现的字样：json_schema 的 response_format（有的服务只认 json_object）、
#: 工具调用（有的网关不支持）、Anthropic 的 output_config
STRUCTURED_WORDS = ("response_format", "json_schema", "json_object", "tool", "function", "output_config",
                    "output_format", "structured output")
#: 一张表的表级的项在「没写出来」记录里的键（不会和列名撞上）
_TABLE = "\0table"

# ---- 一段没写出来的原因（给人看，拼进这张表的 model_error）
REASON_EXHAUSTED = "模型的思考和输出用完了额度（{budget} 个 token），这一段没写出来"
REASON_SPLIT = "；已拆小重试仍没写出"
REASON_HOLLOW = "模型只给了空壳，没有写出可用的内容"
REASON_TRUNCATED = "模型的输出被截断，这一段没写完"
REASON_FORMAT = "模型的回复不是约定的格式"
REASON_MISSING = "模型的回复里没有这张表"
REASON_TIMEOUT = "模型 {seconds} 秒内没有写完这一段"
REASON_CALL = "调用模型出错：{line}"
REASON_RETRIED = "（已重试 {n} 次）"
REASON_CRASH = "起草这张表时出错：{line}"


# ==========================================================================
# 对外的形状
# ==========================================================================


@dataclass(frozen=True)
class Background:
    """使用者补充的背景和要求：guidance 是数据源级的起草要求，note 是这一次的补充说明。数据源说明（description）
    从源上读。都没有就不出现那一段。"""

    guidance: str | None = None
    note: str | None = None


@dataclass
class ModelOutcome:
    """一张表的模型起草结果。

    status：ok 各段都写出来了；partial 有的段写出来、有的没有（error 写清哪部分、为什么）；failed 一项都没写出来
    （error 写原因）。notes 是写出来的那些（已逐项校验，llm / proposed）；items 是项数。parts_total 分几段起草
    （整表段是 1，宽表是 1 + 列分段数），parts_done 已经结束了几段。"""

    status: str
    notes: dict[str, Any]
    items: int
    error: str | None
    parts_total: int
    parts_done: int


@dataclass
class Hooks:
    """进度回调，都是 async，出错只记日志、不影响起草。

    - call_started(表名列表)：一次调用拿到并发名额、马上要发出去了；
    - usage(输入 token, 输出 token)：每次调用一回（拿不到用量的是 0），条数就是调用次数；
    - part_done(表名)：这张表的一段结束了（写没写出来都算）；
    - table_done(表名, ModelOutcome)：这张表的模型部分全部结束。"""

    call_started: Callable[[list[str]], Awaitable[None]] | None = None
    usage: Callable[[int, int], Awaitable[None]] | None = None
    part_done: Callable[[str], Awaitable[None]] | None = None
    table_done: Callable[[str, ModelOutcome], Awaitable[None]] | None = None


def parts_for(columns: int) -> int:
    """一张有这么多列的表分几段起草：不超过 MODEL_BATCH_COLUMNS 列是 1 段，否则是表级一段加列分段。"""
    width = max(1, int(C.MODEL_BATCH_COLUMNS))
    return 1 if columns <= width else 1 + -(-columns // width)


async def run_model_draft(model: Any, source: Any, tables: Sequence[str], *,
                          existing: Mapping[str, Mapping[str, Any]] | None = None,
                          background: Background | None = None,
                          hooks: Hooks | None = None) -> dict[str, ModelOutcome]:
    """用模型起草这些表（schema_cache["tables"] 的键；表结构里没有的跳过，不在结果里）。

    model 要有 with_structured_output(schema, include_raw=…) 和 ainvoke(messages)；有 shrink() 的（catalog.DraftModel）
    在服务方嫌额度太大时换小额度。existing 是 {表名: 目录}：已确认、有确证的项作为「以此为准」的背景交给模型。
    """
    run = _Run(model, source, tables, existing or {}, background or Background(), hooks or Hooks())
    return await run.run()


# ==========================================================================
# 并发名额
# ==========================================================================

_GATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]]" = \
    weakref.WeakKeyDictionary()


def model_gate() -> asyncio.Semaphore:
    """全进程的模型调用名额（MODEL_CONCURRENCY 个）。按事件循环各一个：asyncio 的信号量一旦在某个循环里等过，
    就不能换循环用（测试每个用例一个循环）。"""
    loop = asyncio.get_running_loop()
    limit = max(1, int(C.MODEL_CONCURRENCY))
    held = _GATES.get(loop)
    if held is None or held[0] != limit:
        held = (limit, asyncio.Semaphore(limit))
        _GATES[loop] = held
    return held[1]


# ==========================================================================
# 失败的种类
# ==========================================================================


def _status_of(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def classify_failure(e: BaseException) -> str:
    """调用模型抛出来的异常属于哪一种：length 额度用完（LengthFinishReasonError）、budget 服务方嫌额度太大、
    unsupported 结构化输出不支持、timeout 超时、transient 网络错误 / 429 / 5xx、error 别的。"""
    from app.core.errors import NETWORK, RATE_LIMIT, SERVER, TIMEOUT, classify

    names = {c.__name__ for c in type(e).__mro__}
    if "LengthFinishReasonError" in names:
        return "length"
    status = _status_of(e)
    text = str(e).lower()
    if status in (400, 422) and any(w in text for w in BUDGET_WORDS):
        return "budget"
    # thinking 打开时 Anthropic 不让强制调用工具，langchain 改成不强制，模型没调用工具就抛 OutputParserException
    if isinstance(e, NotImplementedError) or "OutputParserException" in names:
        return "unsupported"
    if status in (400, 422) and any(w in text for w in STRUCTURED_WORDS):
        return "unsupported"
    if isinstance(e, TimeoutError):
        return "timeout"
    kind = classify(e)
    if kind == TIMEOUT:
        return "timeout"
    if kind in (RATE_LIMIT, SERVER, NETWORK):
        return "transient"
    return "error"


def _seconds(value: Any) -> Any:
    number = float(value)
    return int(number) if number.is_integer() else number


# ==========================================================================
# 读回复：结构化输出、正文里的 JSON、被截断的 JSON
# ==========================================================================


@dataclass
class _Reply:
    #: 回复里的表对象（截断的只含已经完整的字段和列）
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: JSON 没写完（捡回了一部分）
    truncated: bool = False
    #: 思考加输出用完了额度
    exhausted: bool = False
    #: 这一段最终没写出来的原因（重试用完、别的错误、不是约定的格式）
    error: str | None = None


class _Cut:
    """被截断的容器（对象或数组）：value 里只有已经完整的成员，最后一个没写完的容器成员也包成 _Cut 留着，好往里捡。"""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class _Bad(ValueError):
    """不是 JSON（不是写到一半，是写错了）。"""


_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_LITERALS = (("true", True), ("false", False), ("null", None))
_MAX_DEPTH = 64


def _skip(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _string(text: str, i: int) -> tuple[Any, int, bool]:
    n = len(text)
    j = i + 1
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            try:
                return json.loads(text[i:j + 1]), j + 1, True
            except ValueError as e:
                raise _Bad from e
        j += 1
    return None, n, False


def _value(text: str, i: int, depth: int) -> tuple[Any, int, bool]:
    """从 text[i] 起读一个 JSON 值：(值, 读到哪, 写完了没有)。读到结尾还没写完的容器返回 _Cut。"""
    if depth > _MAX_DEPTH:
        raise _Bad
    n = len(text)
    i = _skip(text, i)
    if i >= n:
        return None, i, False
    ch = text[i]
    if ch == "{":
        return _object(text, i + 1, depth + 1)
    if ch == "[":
        return _array(text, i + 1, depth + 1)
    if ch == '"':
        return _string(text, i)
    for word, value in _LITERALS:
        if text.startswith(word, i):
            return value, i + len(word), True
        if word.startswith(text[i:]):
            return None, n, False
    m = _NUMBER.match(text, i)
    if not m:
        raise _Bad
    j = m.end()
    if j >= n:
        return None, j, False              # 数字写到了结尾，可能还没写完
    number = m.group()
    return (float(number) if any(c in number for c in ".eE") else int(number)), j, True


def _object(text: str, i: int, depth: int) -> tuple[Any, int, bool]:
    out: dict[str, Any] = {}
    n = len(text)
    while True:
        i = _skip(text, i)
        if i >= n:
            return _Cut(out), i, False
        ch = text[i]
        if ch == "}":
            return out, i + 1, True
        if ch == ",":
            i += 1
            continue
        if ch != '"':
            raise _Bad
        key, i, ok = _string(text, i)
        if not ok:
            return _Cut(out), i, False
        i = _skip(text, i)
        if i >= n:
            return _Cut(out), i, False
        if text[i] != ":":
            raise _Bad
        value, i, ok = _value(text, i + 1, depth)
        if not ok:
            if isinstance(value, _Cut):
                out[key] = value
            return _Cut(out), i, False
        out[key] = value


def _array(text: str, i: int, depth: int) -> tuple[Any, int, bool]:
    out: list[Any] = []
    n = len(text)
    while True:
        i = _skip(text, i)
        if i >= n:
            return _Cut(out), i, False
        ch = text[i]
        if ch == "]":
            return out, i + 1, True
        if ch == ",":
            i += 1
            continue
        value, i, ok = _value(text, i, depth)
        if not ok:
            if isinstance(value, _Cut):
                out.append(value)
            return _Cut(out), i, False
        out.append(value)


#: 正文里最多试几个 { 或 [ 作为 JSON 的开头（前面的说明文字里也可能有括号）
_MAX_STARTS = 16


def _starts(text: str) -> list[int]:
    out: list[int] = []
    for i, ch in enumerate(text):
        if ch in "{[":
            out.append(i)
            if len(out) >= _MAX_STARTS:
                break
    return out


def _entry(item: Any) -> dict[str, Any] | None:
    """一个表对象：只留写完了的字段；columns 留写完了的列对象。没有表名的不要。"""
    if isinstance(item, _Cut):
        item = item.value
    if not isinstance(item, dict) or not isinstance(item.get("table"), str):
        return None
    out: dict[str, Any] = {}
    for key, value in item.items():
        if key == "columns":
            cols = value.value if isinstance(value, _Cut) else value
            if isinstance(cols, list):
                out["columns"] = [c for c in cols if isinstance(c, dict)]
        elif not isinstance(value, _Cut):
            out[key] = value
    return out


def _tables_of(root: Any) -> list[dict[str, Any]] | None:
    """解析出来的根 → 表对象列表。{"tables": […]}、[…]、单个表对象（实测见过不带 tables 外壳的）都认。"""
    if isinstance(root, _Cut):
        root = root.value
    if isinstance(root, dict):
        if "tables" in root:
            items = root["tables"]
        elif isinstance(root.get("table"), str):
            items = [root]
        elif not root:
            return []
        else:
            return None
    else:
        items = root
    if isinstance(items, _Cut):
        items = items.value
    if not isinstance(items, list):
        return None
    return [e for e in (_entry(item) for item in items) if e is not None]


def _parse_text(text: str, cut: bool) -> _Reply | None:
    """正文里的表对象。前面可以有一句话、围栏，后面可以有别的字；JSON 没写完的（截断）只收已经写完的部分。
    认不出约定的格式返回 None。"""
    empty: _Reply | None = None
    for start in _starts(text):
        try:
            root, _, complete = _value(text, start, 0)
        except (_Bad, RecursionError):
            continue
        entries = _tables_of(root)
        if entries is None:
            continue
        reply = _Reply(entries=entries) if complete else _Reply(entries=entries, truncated=True, exhausted=cut)
        if entries:
            return reply
        empty = empty or reply           # {} 或 {"tables": []}：后面还可能有真的那一份
    return empty


def _cut_off(raw: Any) -> bool:
    """额度用完停下的：OpenAI 系是 finish_reason=length，Anthropic 是 stop_reason=max_tokens。"""
    meta = getattr(raw, "response_metadata", None) or {}
    return (meta.get("finish_reason") or meta.get("stop_reason")) in ("length", "max_tokens")


def _thought(raw: Any) -> bool:
    """这次回复有没有思考：思考内容（Anthropic 的 thinking 块、OpenAI 兼容系的 reasoning_content），或者用量里有推理 token。"""
    from app.engine.state import thinking_text

    if raw is None:
        return False
    if thinking_text(raw):
        return True
    details = (getattr(raw, "usage_metadata", None) or {}).get("output_token_details") or {}
    return bool(details.get("reasoning"))


def _text_of(raw: Any) -> str:
    from app.engine.state import message_text

    return message_text(raw) if raw is not None else ""


def _raw_texts(raw: Any) -> list[str]:
    """结构化输出没解析出来时，原始回复里可能还有 JSON 的地方：工具调用的参数（截断的在 invalid_tool_calls 里，是一段
    字符串）、正文。"""
    out: list[str] = []
    for call in getattr(raw, "tool_calls", None) or []:
        args = call.get("args") if isinstance(call, dict) else None
        if isinstance(args, dict) and args:
            out.append(json.dumps(args, ensure_ascii=False))
    for call in getattr(raw, "invalid_tool_calls", None) or []:
        args = call.get("args") if isinstance(call, dict) else None
        if isinstance(args, str) and args.strip():
            out.append(args)
    text = _text_of(raw)
    if text.strip():
        out.append(text)
    return out


def _entries_of(parsed: Any) -> list[dict[str, Any]] | None:
    if parsed is None:
        return None
    if isinstance(parsed, dict) and not parsed:
        return []
    return C._model_tables(parsed)


def _structured_reply(raw: Any, parsed: Any) -> _Reply:
    entries = _entries_of(parsed)
    if entries is not None:
        return _Reply(entries=entries)
    cut = _cut_off(raw)
    texts = _raw_texts(raw)
    for text in texts:
        reply = _parse_text(text, cut)
        if reply is not None:
            return reply
    if cut or _thought(raw):
        return _Reply(exhausted=True)
    if not texts:
        return _Reply()                    # 什么都没写：按空壳处理
    return _Reply(error=REASON_FORMAT)


def _text_reply(raw: Any) -> _Reply:
    text = _text_of(raw)
    cut = _cut_off(raw)
    if text.strip():
        reply = _parse_text(text, cut)
        if reply is not None:
            return reply
        if cut:
            return _Reply(exhausted=True)  # 写了字、额度就用完了，一个 JSON 对象都没捡回来
        return _Reply(error=REASON_FORMAT)
    return _Reply(exhausted=cut or _thought(raw))


def _unpack(out: Any) -> tuple[Any, Any]:
    """with_structured_output(include_raw=True) 交回 {raw, parsed, parsing_error}；测试的假模型直接交回解析好的对象。"""
    if isinstance(out, dict) and "raw" in out and ("parsed" in out or "parsing_error" in out):
        return out.get("raw"), out.get("parsed")
    return None, out


# ==========================================================================
# 一次起草
# ==========================================================================


@dataclass(frozen=True)
class _Piece:
    """一段里的一张表：要写哪些列、要不要表级的项、属于这张表的第几段（从 1 起）。"""

    table: str
    columns: tuple[str, ...]
    table_level: bool
    part: int


@dataclass
class _Gain:
    """一次回复里某张表拿到了什么。"""

    named: bool = False
    items: int = 0
    table_items: int = 0
    answered: set[str] = field(default_factory=set)


@dataclass
class _Table:
    name: str
    meta: Mapping[str, Any]
    columns: list[str]
    existing: Mapping[str, Any] | None
    parts: list[_Piece] = field(default_factory=list)
    #: 已经拿到的模型起草（逐段合并）
    notes: dict[str, Any] = field(default_factory=dict)
    #: 模型的回复里出现过这张表
    named: bool = False
    got_table: bool = False
    got_columns: set[str] = field(default_factory=set)
    #: 没写出来的列（表级的项记在 _TABLE）→ 原因
    failed: dict[str, str] = field(default_factory=dict)
    parts_done: int = 0
    finished: bool = False

    def whole(self, piece: _Piece) -> bool:
        return piece.table_level and len(piece.columns) == len(self.columns)


def _restrict(notes: Mapping[str, Any], piece: _Piece) -> dict[str, Any]:
    """只留这一段请模型写的部分：没请写表级的项就不要表级的项，列只要请写的那几列。"""
    out: dict[str, Any] = {}
    if piece.table_level:
        out.update({k: v for k, v in notes.items() if k != "columns"})
    wanted = set(piece.columns)
    cols = {c: items for c, items in (notes.get("columns") or {}).items() if c in wanted}
    if cols:
        out["columns"] = cols
    return out


def _halves(pieces: list[_Piece]) -> tuple[list[_Piece], list[_Piece]] | None:
    """拆成两半：多表的段按表拆；单表的按列拆，两半都至少 MODEL_SPLIT_MIN_COLUMNS 列（表级的项跟着前一半）。拆不开返回 None。"""
    if len(pieces) > 1:
        k = (len(pieces) + 1) // 2
        return pieces[:k], pieces[k:]
    piece = pieces[0]
    n = len(piece.columns)
    if n < 2 * max(1, int(C.MODEL_SPLIT_MIN_COLUMNS)):
        return None
    h = (n + 1) // 2
    return ([_Piece(piece.table, piece.columns[:h], piece.table_level, piece.part)],
            [_Piece(piece.table, piece.columns[h:], False, piece.part)])


class _Run:
    def __init__(self, model: Any, source: Any, tables: Sequence[str], existing: Mapping[str, Mapping[str, Any]],
                 background: Background, hooks: Hooks) -> None:
        self.model = model
        self.hooks = hooks
        #: structured 结构化输出；text 纯文本、从正文取 JSON。结构化输出用不了时这次起草后面都改走纯文本
        self.mode = "structured"
        self.system = C.system_notes_source(source)
        all_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
        self.order = [t for t in dict.fromkeys(str(t) for t in tables) if isinstance(all_tables.get(t), dict)]
        self.tables = {
            name: _Table(name=name, meta=all_tables[name], existing=existing.get(name),
                         columns=[str(c["name"]) for c in all_tables[name].get("columns") or []
                                  if isinstance(c, dict) and c.get("name") is not None])
            for name in self.order
        }
        self.background = self._background(getattr(source, "description", None), background)

    # ---------------------------------------------------------------- 总流程

    async def run(self) -> dict[str, ModelOutcome]:
        await asyncio.gather(*(self._unit(unit) for unit in self._plan()))
        return {name: self._outcome(t) for name, t in self.tables.items()}

    def _plan(self) -> list[list[str] | str]:
        """分段：窄表按顺序拼成整表段（列表），宽表一个一项（表名）。各项按第一张表在 tables 里的位置排。"""
        width = max(1, int(C.MODEL_BATCH_COLUMNS))
        most = max(1, int(C.MODEL_BATCH_TABLES))
        units: list[list[str] | str] = []
        pack: list[str] | None = None
        used = 0
        for name in self.order:
            st = self.tables[name]
            n = len(st.columns)
            if n > width:
                st.parts = [_Piece(name, (), True, 1)] + [
                    _Piece(name, tuple(st.columns[i:i + width]), False, k + 2)
                    for k, i in enumerate(range(0, n, width))]
                units.append(name)
                continue
            st.parts = [_Piece(name, tuple(st.columns), True, 1)]
            if pack is None or len(pack) >= most or used + n > width:
                pack, used = [], 0
                units.append(pack)
            pack.append(name)
            used += n
        return units

    async def _unit(self, unit: list[str] | str) -> None:
        names = unit if isinstance(unit, list) else [unit]
        if isinstance(unit, list):
            await self._guarded([self.tables[n].parts[0] for n in unit])
            for name in unit:
                await self._part_done(name)
        else:
            head, *rest = self.tables[unit].parts
            await self._guarded([head])        # 表级那段先写：列分段拿它做背景
            await self._part_done(unit)

            async def column_part(piece: _Piece) -> None:
                await self._guarded([piece])
                await self._part_done(unit)

            await asyncio.gather(*(column_part(p) for p in rest))
        for name in names:
            await self._table_done(name)

    async def _guarded(self, pieces: list[_Piece]) -> None:
        """起草一段；意料之外的错误只记这一段没写出来，不影响别的段和别的表。"""
        try:
            await self._segment(pieces, 0, False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            from app.core.errors import describe_exception, first_line

            logger.exception("起草数据目录时出错：%s", "、".join(p.table for p in pieces))
            self._fail(pieces, REASON_CRASH.format(line=first_line(e) or describe_exception(e)))

    # ---------------------------------------------------------------- 一段

    async def _segment(self, pieces: list[_Piece], depth: int, followup: bool, *, mode: str | None = None,
                       last: bool = False) -> None:
        """问一段，拿到的并进各表；按失败原因拆开、补缺、记下没写出来的。

        depth：已经拆了几层（截断后只补剩下的也算一层）。followup：这一段是在补缺（补过就不再补，空着的算模型拿不准）。
        mode / last：拆不开的空壳换纯文本问的那一次（再空就记没写出来）。"""
        reply = await self._ask(pieces, mode)
        if reply.error is not None:
            self._fail(pieces, reply.error)
            return
        gains = self._take(pieces, reply.entries)
        anything = any(g.items for g in gains)
        if last and anything and self.mode != "text":
            # 结构化输出只给空壳、纯文本写出来了：这个模型的结构化输出靠不住，这次起草后面都走纯文本
            self.mode = "text"
            logger.info("起草数据目录：结构化输出只给了空壳，纯文本写出来了，后面改走纯文本")
        if reply.exhausted or reply.truncated:
            rest = self._remainder(pieces, gains, truncated=True)
            if not rest:
                return
            kind = "exhausted" if reply.exhausted else "truncated"
            if not anything:
                await self._split(rest, depth, kind, followup)
            elif depth < int(C.MODEL_SPLIT_DEPTH):
                await self._segment(rest, depth + 1, followup)     # 捡回了一部分：只补剩下的
            else:
                self._fail(rest, self._reason(kind, split=True))
            return
        if not anything:
            if followup:
                return
            if last:
                self._fail(pieces, lambda p: self._reason("hollow", split=depth > 0, piece=p))
                return
            await self._split(pieces, depth, "hollow", followup)
            return
        if followup:
            return
        rest = self._remainder(pieces, gains, truncated=False)
        if rest:
            await self._segment(rest, depth, True)

    async def _split(self, pieces: list[_Piece], depth: int, kind: str, followup: bool) -> None:
        halves = _halves(pieces) if depth < int(C.MODEL_SPLIT_DEPTH) else None
        if halves is not None:
            first, second = halves
            if (len(first) == len(second) == 1 and first[0].table == second[0].table and first[0].table_level):
                # 同一张表按列拆、前一半带着表级的项：先写前一半，后一半拿它做背景
                await self._segment(first, depth + 1, followup)
                await self._segment(second, depth + 1, followup)
                return
            results = await asyncio.gather(self._segment(first, depth + 1, followup),
                                           self._segment(second, depth + 1, followup), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return
        if kind == "hollow" and self.mode == "structured":
            await self._segment(pieces, depth, followup, mode="text", last=True)
            return
        self._fail(pieces, lambda p: self._reason(kind, split=depth > 0, piece=p))

    def _remainder(self, pieces: list[_Piece], gains: list[_Gain], *, truncated: bool) -> list[_Piece]:
        """还没写出来的部分：回复里没有这张表、或者这张表一项可用内容都没有的整段重问；有内容的只问没给的列。
        截断的回复里表级的项也没拿到的，表级的项一起重问（没截断时表级空着算模型拿不准）。"""
        out: list[_Piece] = []
        for piece, gain in zip(pieces, gains):
            if not gain.named or not gain.items:
                out.append(piece)
                continue
            cols = tuple(c for c in piece.columns if c not in gain.answered)
            table_level = piece.table_level and truncated and not gain.table_items
            if cols or table_level:
                out.append(_Piece(piece.table, cols, table_level, piece.part))
        return out

    # ---------------------------------------------------------------- 调用

    async def _ask(self, pieces: list[_Piece], mode: str | None = None) -> _Reply:
        """调一次模型（按原因重来：额度被拒、结构化输出不支持、网络错误……），交回读好的回复。"""
        mode = mode or self.mode
        retried = 0
        while True:
            used = getattr(self.model, "budget", None)
            try:
                async with model_gate():
                    await self._hook(self.hooks.call_started, [p.table for p in pieces])
                    reply, raw = await asyncio.wait_for(self._call(pieces, mode), timeout=C.MODEL_TIMEOUT_S)
                # 用量记在时限之外：落库慢了不能把已经回来的这次调用算成超时
                await self._usage(raw)
                return reply
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 按原因分别处理
                kind = classify_failure(e)
                if kind == "length":
                    return await self._length_reply(e)
                await self._hook(self.hooks.usage, 0, 0)
                if kind == "budget" and (getattr(self.model, "budget", None) != used or self._shrink()):
                    continue
                if kind == "unsupported" and mode == "structured":
                    if self.mode == "structured":
                        logger.info("起草数据目录：结构化输出用不了（%s），后面改走纯文本", str(e)[:200])
                    self.mode = mode = "text"
                    continue
                if kind in ("transient", "timeout") and retried < int(C.MODEL_RETRIES):
                    delays = tuple(C.MODEL_RETRY_DELAYS_S) or (0.0,)
                    await _sleep(delays[min(retried, len(delays) - 1)])
                    retried += 1
                    continue
                return _Reply(error=self._call_error(e, kind, retried))

    async def _call(self, pieces: list[_Piece], mode: str) -> tuple[_Reply, Any]:
        """一次调用：(读好的回复, 原始回复)。原始回复给用量用（测试的假模型没有，是 None）。"""
        messages = self._messages(pieces, mode)
        if mode == "structured":
            runnable = self.model.with_structured_output(C.CATALOG_DRAFT_PROMPT_SCHEMA, include_raw=True)
            raw, parsed = _unpack(await runnable.ainvoke(messages))
            return _structured_reply(raw, parsed), raw
        raw = await self.model.ainvoke(messages)
        return _text_reply(raw), raw

    async def _length_reply(self, e: BaseException) -> _Reply:
        """LengthFinishReasonError：额度用完了。异常里带着那次的回复，能捡回来的照捡。"""
        completion = getattr(e, "completion", None)
        usage = getattr(completion, "usage", None)
        await self._hook(self.hooks.usage, int(getattr(usage, "prompt_tokens", 0) or 0),
                         int(getattr(usage, "completion_tokens", 0) or 0))
        try:
            text = completion.choices[0].message.content or ""  # type: ignore[union-attr]
        except (AttributeError, IndexError, TypeError):
            text = ""
        if text.strip():
            reply = _parse_text(text, True)
            if reply is not None and reply.entries:
                return _Reply(entries=reply.entries, truncated=True, exhausted=True)
        return _Reply(exhausted=True)

    def _shrink(self) -> bool:
        shrink = getattr(self.model, "shrink", None)
        if callable(shrink) and shrink():
            logger.info("起草数据目录：服务方不接受这么大的输出额度，这次起草改用 %s", getattr(self.model, "budget", ""))
            return True
        return False

    def _budget(self) -> int:
        return int(getattr(self.model, "budget", None) or C.MODEL_MAX_TOKENS)

    def _call_error(self, e: BaseException, kind: str, retried: int) -> str:
        from app.core.errors import describe_exception, first_line

        tail = REASON_RETRIED.format(n=retried) if retried else ""
        if kind == "timeout":
            return REASON_TIMEOUT.format(seconds=_seconds(C.MODEL_TIMEOUT_S)) + tail
        return REASON_CALL.format(line=first_line(e) or describe_exception(e)) + tail

    async def _usage(self, raw: Any) -> None:
        meta = getattr(raw, "usage_metadata", None) or {}
        await self._hook(self.hooks.usage, int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0))

    # ---------------------------------------------------------------- 拿到的内容

    def _match(self, pieces: list[_Piece], entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """回复里的表对象按表名对到这一段的表上。表名照原样写的直接对上；带了 schema 前缀、大小写不一致的，只在恰好
        对上一张时认。"""
        names = [p.table for p in pieces]
        out: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            raw = entry.get("table")
            if not isinstance(raw, str):
                continue
            name = raw if raw in names else None
            if name is None:
                short = raw.strip().rsplit(".", 1)[-1].strip('"`[] ')
                found = [n for n in names if n == short or name_key(n) == name_key(short)]
                name = found[0] if len(found) == 1 else None
            if name is not None:
                out.setdefault(name, []).append(entry)
        return out

    def _take(self, pieces: list[_Piece], entries: list[dict[str, Any]]) -> list[_Gain]:
        """回复里能用的并进各表（逐项校验，只留这一段请写的部分），返回每张表这次拿到了什么。"""
        by_table = self._match(pieces, entries)
        gains: list[_Gain] = []
        for piece in pieces:
            st = self.tables[piece.table]
            gain = _Gain()
            for entry in by_table.get(piece.table, []):
                gain.named = st.named = True
                notes = _restrict(C._from_model(entry, st.meta, system=self.system), piece)
                cols = entry.get("columns") if isinstance(entry.get("columns"), list) else []
                gain.answered |= {c.get("name") for c in cols if isinstance(c, dict) and c.get("name") in piece.columns}
                slots = C._slots(notes)
                gain.items += len(slots)
                gain.table_items += sum(1 for key in slots if key[0] == "t")
                self._merge(st, notes)
            gains.append(gain)
        return gains

    @staticmethod
    def _merge(st: _Table, notes: Mapping[str, Any]) -> None:
        for key, value in notes.items():
            if key == "columns":
                cols = st.notes.setdefault("columns", {})
                for col, items in value.items():
                    target = cols.setdefault(col, {})
                    for name, item in items.items():
                        target.setdefault(name, item)
                    st.got_columns.add(col)
            else:
                st.notes.setdefault(key, value)
                st.got_table = True

    def _fail(self, pieces: list[_Piece], reason: str | Callable[[_Piece], str]) -> None:
        """这几段没写出来：还没拿到内容的列（和表级的项）记下原因。"""
        for piece in pieces:
            st = self.tables[piece.table]
            why = reason(piece) if callable(reason) else reason
            for col in piece.columns:
                if col not in st.got_columns:
                    st.failed.setdefault(col, why)
            if piece.table_level and not st.got_table:
                st.failed.setdefault(_TABLE, why)

    def _reason(self, kind: str, *, split: bool, piece: _Piece | None = None) -> str:
        if kind == "exhausted":
            base = REASON_EXHAUSTED.format(budget=self._budget())
        elif kind == "truncated":
            base = REASON_TRUNCATED
        else:
            st = self.tables[piece.table] if piece is not None else None
            if st is not None and st.whole(piece):
                base = C.MODEL_EMPTY_REASON if st.named else REASON_MISSING
            else:
                base = REASON_HOLLOW
        return base + (REASON_SPLIT if split else "")

    # ---------------------------------------------------------------- 收尾

    def _outcome(self, st: _Table) -> ModelOutcome:
        items = len(C._slots(st.notes))
        failed_cols = [c for c in st.columns if c in st.failed and c not in st.got_columns]
        failed_table = _TABLE in st.failed and not st.got_table
        total = len(st.parts) or 1
        if not items:
            reasons = list(dict.fromkeys(st.failed.values()))
            reason = "；".join(reasons[:2]) if reasons else (C.MODEL_EMPTY_REASON if st.named else REASON_MISSING)
            return ModelOutcome("failed", {}, 0, reason, total, st.parts_done)
        notes = copy.deepcopy(st.notes)
        if failed_cols or failed_table:
            return ModelOutcome("partial", notes, items, self._partial_text(st, failed_cols, failed_table), total,
                                st.parts_done)
        return ModelOutcome("ok", notes, items, None, total, st.parts_done)

    @staticmethod
    def _partial_text(st: _Table, failed_cols: list[str], failed_table: bool) -> str:
        """只写出一部分时说清哪部分没写出来、为什么、怎么办。"""
        keys = ([_TABLE] if failed_table else []) + failed_cols
        reason = "；".join(list(dict.fromkeys(st.failed[k] for k in keys))[:2])
        n = len(st.columns)
        if len(st.parts) > 1:
            part_of = {c: p.part for p in st.parts for c in p.columns}
            bad = sorted({part_of[c] for c in failed_cols if c in part_of} | ({1} if failed_table else set()))
            head = f"这张表 {n} 列，分 {len(st.parts)} 段起草，第 {'、'.join(str(b) for b in bad)} 段没写出来"
            head += f"（共 {len(failed_cols)} 列）" if failed_cols else "（表级的项）"
        elif failed_cols and failed_table:
            head = f"这张表 {n} 列，有表级的项和 {len(failed_cols)} 列没写出来"
        elif failed_cols:
            head = f"这张表 {n} 列，有 {len(failed_cols)} 列没写出来"
        else:
            head = f"这张表 {n} 列，表级的项没写出来"
        return f"{head}：{reason}。可以只重试这张表"

    async def _part_done(self, name: str) -> None:
        self.tables[name].parts_done += 1
        await self._hook(self.hooks.part_done, name)

    async def _table_done(self, name: str) -> None:
        st = self.tables[name]
        if st.finished:
            return
        st.finished = True
        await self._hook(self.hooks.table_done, name, self._outcome(st))

    @staticmethod
    async def _hook(fn: Callable[..., Awaitable[None]] | None, *args: Any) -> None:
        if fn is None:
            return
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 进度回调出错不影响起草
            logger.exception("起草数据目录的进度回调出错")

    # ---------------------------------------------------------------- 提示词

    @staticmethod
    def _background(description: Any, background: Background) -> str:
        labels = C.CATALOG_DRAFT_PROMPT_LABELS
        lines = []
        for key, text in (("description", description), ("guidance", background.guidance), ("note", background.note)):
            text = text.strip() if isinstance(text, str) else ""
            if text:
                lines.append(labels[key].format(text=text))
        return C.CATALOG_DRAFT_BACKGROUND_PROMPT.format(lines="\n".join(lines)) if lines else ""

    def _messages(self, pieces: list[_Piece], mode: str) -> list[Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        text = C.CATALOG_DRAFT_PROMPT.format(n=len(pieces), tables="\n\n".join(self._describe(p) for p in pieces))
        if self.background:
            text = f"{self.background}\n\n{text}"
        if mode == "text":
            text = f"{text}\n\n{C.CATALOG_DRAFT_TEXT_PROMPT}"
        return [SystemMessage(content=C.CATALOG_DRAFT_SYSTEM), HumanMessage(content=text)]

    def _describe(self, piece: _Piece) -> str:
        """一段里一张表的结构描述：列、类型、主键、外键、唯一约束、注释；不含任何数据值。

        整表：照旧的完整描述。只写表级的项：全部列只给名字和类型。表级的项加几列：完整描述，表名下一行点名这几列。
        只写几列：表注释、已起草的表级内容做背景，只列这几列。"""
        labels = C.CATALOG_DRAFT_PROMPT_LABELS
        st = self.tables[piece.table]
        meta = st.meta
        lines = [(labels["view"] if meta.get("is_view") else labels["table"]).format(name=st.name)]
        brief = piece.table_level and not piece.columns
        if not st.whole(piece):
            if brief:
                lines.append(labels["only_table"])
            elif piece.table_level:
                lines.append(labels["table_and_columns"].format(n=len(piece.columns), cols="、".join(piece.columns)))
            else:
                lines.append(labels["only_columns"].format(n=len(piece.columns), total=len(st.columns)))
        comment = (meta.get("comment") or "").strip()
        if comment:
            lines.append((labels["system"] if self.system else labels["comment"]).format(text=comment))
        pk = set(meta.get("primary_key") or [])
        fk_of: dict[str, str] = {}
        for fk in meta.get("foreign_keys") or []:
            if isinstance(fk, dict) and len(fk.get("columns") or []) == 1:
                to_cols = fk.get("to_columns") or []
                fk_of[fk["columns"][0]] = f"{fk.get('to_table')}.{to_cols[0]}" if to_cols else str(fk.get("to_table"))
        if brief:
            lines.append(labels["brief_columns"].format(n=len(st.columns)))
            wanted = set(st.columns)
        else:
            if not piece.table_level:
                drafted = C._notes_lines({k: v for k, v in st.notes.items() if k in C.TABLE_FIELDS}, meta=meta,
                                         system_notes=False)
                if drafted:
                    lines.append(labels["drafted"])
                    lines += drafted
            lines.append(labels["columns"])
            wanted = set(st.columns) if piece.table_level else set(piece.columns)
        for col in meta.get("columns") or []:
            if isinstance(col, dict) and col.get("name") in wanted:
                lines.append(self._column_line(col, pk, fk_of, brief=brief))
        if piece.table_level:
            for group in meta.get("unique") or []:
                if isinstance(group, list) and group:
                    lines.append(labels["unique"].format(cols="、".join(group)))
        confirmed = self._confirmed_lines(st, piece)
        if confirmed:
            lines.append(labels["confirmed"])
            lines += confirmed
        return "\n".join(lines)

    def _column_line(self, col: Mapping[str, Any], pk: set[str], fk_of: Mapping[str, str], *, brief: bool) -> str:
        labels = C.CATALOG_DRAFT_PROMPT_LABELS
        marks = []
        if col["name"] in pk:
            marks.append(labels["pk"])
        if not brief and not col.get("nullable", True):
            marks.append(labels["not_null"])
        if col["name"] in fk_of:
            marks.append(labels["fk"].format(target=fk_of[col["name"]]))
        if not brief and (text := (col.get("comment") or "").strip()):
            marks.append((labels["col_system"] if self.system else labels["col_comment"]).format(text=text))
        line = labels["column"].format(name=col["name"], type=col.get("type") or "")
        return line + ("，" + "，".join(marks) if marks else "")

    @staticmethod
    def _confirmed_lines(st: _Table, piece: _Piece) -> list[str]:
        """已确认、有确证的项（以此为准的背景）：整表全给；其余给表级的项和这一段的列，关系只在要写表级的项时给。"""
        confirmed = C._confirmed_only(st.existing)
        if not st.whole(piece):
            wanted = set(piece.columns)
            kept = {key: item for key, item in C._slots(confirmed).items()
                    if key[0] == "t" or (key[0] == "c" and key[1] in wanted) or (key[0] == "r" and piece.table_level)}
            confirmed = C._assemble(kept)
        return C._notes_lines(confirmed, meta=st.meta, system_notes=False)


__all__ = ["BUDGET_WORDS", "Background", "Hooks", "ModelOutcome", "REASON_CALL", "REASON_EXHAUSTED", "REASON_FORMAT",
           "REASON_HOLLOW", "REASON_MISSING", "REASON_RETRIED", "REASON_SPLIT", "REASON_TIMEOUT", "REASON_TRUNCATED",
           "classify_failure", "model_gate", "parts_for", "run_model_draft"]
