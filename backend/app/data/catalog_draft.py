"""数据目录的模型起草：分段、按失败原因处理、捡回被截断的部分、记用量。

对外的名字留在 data/catalog.py（draft_with_model、draft_catalog、write_draft、CATALOG_DRAFT_SYSTEM、各个 MODEL_*
常量……），这里是实现。常量一律在调用时按 catalog.X 读：测试改 catalog 上的常量就能生效。

**为什么要分段、按原因处理。** 实测（经 OpenAI 兼容网关的会思考的模型）：思考算在输出额度里，一段上百列时思考就把
额度全用掉，正文 0 字或只给一个空壳；同一批表有时成功有时失败，看模型这次想多久。以前失败了退回纯文本、发同一个提示词，
同样失败、耗时翻倍；超时把第一次拿到的部分也丢掉；一批里有一张表是空的就整批重问。

**分段。** 每次调用最多 MODEL_BATCH_TABLES 张表、合计 MODEL_BATCH_COLUMNS 列：列数不超过它的表按顺序拼在一起
（整表段）；超过它的宽表拆成「表级一段 + 列分段」——表级那段给全部列名和类型做背景、只要表级的项（中文名、粒度、
表类型、业务主键、业务日期）；列分段数是列数除以 MODEL_BATCH_COLUMNS 向上取整、各段列数平均（相差不超过 1，以前
按 40 列定长切，留下几列的尾段，实测会思考的模型常对这种小段只给空壳），带着表名、表注释和表级起草结果做背景，只要
这几列。最后合并成这张表的一份，照旧经 catalog._from_model 逐项校验（起草要求不能让模型绕过校验）。

**段大小随模型调整。** 一次起草里额度用完或超时累计 MODEL_SHRINK_AFTER 次，之后新开始的段列数上限减半（40 → 20
→ 10，最低 MODEL_MIN_PART_COLUMNS）：新开始的段（整表段、列分段）超过上限就先按上限均分开再问。已经开始的段照旧
按失败原因拆小。

**按失败原因处理，不原样重发。**

- 思考加输出用完额度（finish_reason=length、LengthFinishReasonError、正文为空而有思考）、只给空壳、超时（当作这一段
  太大，不原样重试同样大小的段）：把这一段拆成两半再问（多表的段按表拆，单表的按列拆），最小
  MODEL_SPLIT_MIN_COLUMNS 列，最多拆 MODEL_SPLIT_DEPTH 层。拆不开的空壳再问一次：结构化输出给的换纯文本（有的网关的
  结构化输出只给空壳、纯文本却能写——写出来了，这次起草后面都走纯文本），已经走纯文本的原样再问；仍是空壳才记没写
  出来。
- 结构化输出回了一段说明文字（不是 JSON、也没停在额度上）：网关接受了结构化输出的参数却没照做（例如开了丢弃不认识
  的参数），提示词里又没有格式说明，模型就写成了说明文字。这次起草后面改走纯文本，这一段换纯文本问一次。有思考
  不等于额度用完：只有停在额度上、或者正文为空而有思考才算。
- 被截断的 JSON：先把已经完整的表、列对象捡回来，只补剩下的。
- 一段里缺了的表、列只补缺的，补一次。补问之后：模型列出了、字段留空的列是拿不准，不算没写出来；整列没列出来的
  算没写出来（这张表是 partial，旧的模型起草项不删）；整张表还一项都没有的，照只给空壳处理（拆小、换纯文本）。
- 结构化输出这个接口不支持（400 / 422 提到 response_format、json_schema、tool……，或者模型类不支持）：这次起草
  后面改走纯文本、从正文取 JSON（纯文本那条路另外说清 JSON 的格式）。
- 服务方嫌额度太大：退回 MODEL_FALLBACK_MAX_TOKENS 重来，这次起草后面都用它（catalog.DraftModel.shrink）。
- 网络错误、429、5xx：等一会儿重试（最多 MODEL_RETRIES 次，退避）。单次调用的时限是 MODEL_TIMEOUT_S。SDK
  自己不再重试（catalog.resolve_draft_model 建模型时 max_retries=0），实际重试次数和报错写的一致。
- 别的错误：这一段没写出来，原因照实写。
- 封顶：一段（连同它拆出来的、补问的、重试的）最多调用 MODEL_PART_MAX_CALLS 次，到了次数还没写完的记没写出来。
- 止损：连续 MODEL_GIVE_UP_AFTER 段拆到底仍一项没写出来（模型明显用不了，例如思考总是吃满额度），这次起草后面的
  段不再拆小重试、没写出来就直接记下，报错建议更换起草模型；有一段写出来了就恢复。
- 提醒：最近 MODEL_NOTICE_WINDOW 次调用里有 MODEL_NOTICE_ON 次以上没写出来（额度用完、超时、空壳、出错），给一句
  提醒（次数和原因，建议换起草模型、减少表或停止），经进度回调交给任务；之后少于 MODEL_NOTICE_OFF 次时清掉。以前
  看不出调用在反复失败：一个任务 55 分钟只结束 1 张表，界面上只有段进度和用量。

**原因。** 没写出来的原因只写进这张表的 model_error：几段因为同一个原因没写出来时说「这几段」，每段写上列的范围
（「第 5 段（列名 到 列名，共 33 列）」，列名照表结构原样），真拆小重试过的才补「已拆小重试仍没写出」。

**留空的列。** 请模型写了、模型也列出了，中文名和含义却都没给（或者给的都没通过校验）的列是模型拿不准，不补问，
记在 ModelOutcome.blank 里（表的状态不因此变）。已确认的、库注释已经说清的列不算（提示词里它们以此为准）。

**并发。** 全进程最多 MODEL_CONCURRENCY 个调用同时进行（model_gate：按事件循环各一个信号量，所有起草任务和同步
起草共用，先来先放）。一次起草里同时只起草 MODEL_CONCURRENCY 段（一段拆小、补缺都做完再起草下一段），同时在等
名额的调用也最多这么多个：不把几百段一下子都排上去，几次起草（不同数据源的任务、同步起草）的调用交替排队，后开始
的不会等前一个排着的段全部调完。一次起草里按表的先后排队：宽表的表级那段写完，它的列分段排到已经排着的段后面
（先来先服务），不插到窄表前面。

**进度。** 调用方给 Hooks：开始调用、每次调用的结果（用量、没写出来的原因、当前的提醒和列数上限）、一段结束、一张表
的模型部分全部结束。后台起草任务据此落库，每张表一结束就并入目录（data/catalog_jobs.py）。

交给模型的只有表结构（列、类型、主键、外键、唯一约束、注释）和使用者补充的背景，不发任何数据值。写给模型的文字都在
catalog.py 名字带 _SYSTEM / _PROMPT 的常量里；这里的 REASON_* 是给人看的（拼进这张表的 model_error）。
"""
from __future__ import annotations

import asyncio
import collections
import copy
import json
import logging
import re
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.data import catalog as C
from app.data.draft_model import DRAFT_MODEL_PLACE
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

# ---- 一段没写出来的原因（给人看，拼进这张表的 model_error）。{seg} 在拼成一句时换成「这一段」或「这几段」：同一个
#: 原因落在几段上（宽表的几个列分段）说「这几段」，同一段拆出来的几块仍是「这一段」
SEG = "{seg}"
SEG_ONE = "这一段"
SEG_MANY = "这几段"
REASON_EXHAUSTED = "模型的思考和输出用完了额度（{budget} 个 token），{seg}没写出来"
REASON_SPLIT = "；已拆小重试仍没写出"
REASON_HOLLOW = "模型只给了空壳，没有写出可用的内容"
REASON_TRUNCATED = "模型的输出被截断，{seg}没写完"
REASON_FORMAT = "模型的回复不是约定的格式"
REASON_MISSING = "模型的回复里没有这张表"
REASON_UNANSWERED = "模型的回复里漏了这些列，补问一次仍没有给出"
REASON_TIMEOUT = "模型 {seconds} 秒内没有写完{seg}"
REASON_CALL = "调用模型出错：{line}"
REASON_RETRIED = "（已重试 {n} 次）"
REASON_CRASH = "起草这张表时出错：{line}"
#: 一段到了调用次数的上限（MODEL_PART_MAX_CALLS）还没写完
REASON_CAPPED = "每段最多调用 {n} 次模型，{seg}到了次数仍没写完，已停止重试"
#: 止损（连续几段拆到底仍一项没写出来，后面的段不再拆小重试）时补在原因后面
REASON_HALTED = "；模型连续 {n} 段都没写出来，已停止拆小重试，建议更换起草模型"

# ---- 提醒（最近几次调用多数没写出来），经进度回调交给任务（CatalogDraftJob.notice）
NOTICE = ("最近 {calls} 次调用有 {failed} 次没写出来（{kinds}）。可以在{place}换一个模型、减少这次起草的表，"
          "或者停止这次起草")
#: 一次调用没写出来的原因种类 → 提醒里的说法（按这个顺序列）
FAILURE_LABEL = {"exhausted": "额度用完", "timeout": "超时", "hollow": "只给空壳", "error": "出错"}


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
    （整表段是 1，宽表是 1 + 列分段数），parts_done 已经结束了几段。blank：模型拿不准、留空了的列（按表结构的顺序；
    表的状态不因此变）。"""

    status: str
    notes: dict[str, Any]
    items: int
    error: str | None
    parts_total: int
    parts_done: int
    blank: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CallReport:
    """一次调用的结果（进度回调 call_done 收到的）。

    failure：没写出来的原因种类，写出来了是 None——exhausted 额度用完（一项都没捡回来）、timeout 超时、hollow 只给空壳
    （回复里没有可用的内容）、error 出错（会重试的那次也算一次）。超时的调用拿不到用量，token 记 0（网关那边可能已经
    生成了）。notice、part_columns 是这次调用之后的：提醒（没有为 None）、新开始的段每段最多几列。"""

    input_tokens: int
    output_tokens: int
    failure: str | None
    notice: str | None
    part_columns: int

    @property
    def timeout(self) -> bool:
        return self.failure == "timeout"


@dataclass
class Hooks:
    """进度回调，都是 async，出错只记日志、不影响起草。

    - call_started(表名列表)：一次调用拿到并发名额、马上要发出去了；
    - call_done(CallReport)：每次调用一回（条数就是调用次数），带着用量、没写出来的原因、当前的提醒和列数上限；
    - part_done(表名)：这张表的一段结束了（写没写出来都算）；
    - table_done(表名, ModelOutcome)：这张表的模型部分全部结束。"""

    call_started: Callable[[list[str]], Awaitable[None]] | None = None
    call_done: Callable[[CallReport], Awaitable[None]] | None = None
    part_done: Callable[[str], Awaitable[None]] | None = None
    table_done: Callable[[str, ModelOutcome], Awaitable[None]] | None = None


def parts_for(columns: int) -> int:
    """一张有这么多列的表分几段起草：不超过 MODEL_BATCH_COLUMNS 列是 1 段，否则是表级一段加列分段（列数除以
    MODEL_BATCH_COLUMNS 向上取整）。"""
    width = max(1, int(C.MODEL_BATCH_COLUMNS))
    return 1 if columns <= width else 1 + -(-columns // width)


def _even(columns: Sequence[str], parts: int) -> list[tuple[str, ...]]:
    """按顺序切成 parts 段，各段列数平均（相差不超过 1，多出来的放在前面几段）。"""
    columns = tuple(columns)
    parts = max(1, min(int(parts), len(columns) or 1))
    size, extra = divmod(len(columns), parts)
    out: list[tuple[str, ...]] = []
    start = 0
    for i in range(parts):
        n = size + (1 if i < extra else 0)
        out.append(columns[start:start + n])
        start += n
    return out


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
    #: 结构化输出回了一段说明文字（error 是「不是约定的格式」）：换纯文本还可能写出来
    prose: bool = False
    #: 这次调用超时了（当作这一段太大：拆小再问，不原样重试）
    timeout: bool = False
    #: 这一段的调用次数用完了，这次没有调用
    capped: bool = False
    #: 这次调用的用量 (输入, 输出)，还没报给进度回调；没有调用（capped）的是 None
    usage: tuple[int, int] | None = None


@dataclass(frozen=True)
class _Why:
    """一部分没写出来的原因。base 给人看的原因（可以带 SEG，拼成一句时换成「这一段」「这几段」）；split 真拆小重试过；
    halted 本来还能拆小，因为前面连续几段都没写出来（止损）才没拆。"""

    base: str
    split: bool = False
    halted: bool = False


def _join(failures: Iterable[tuple[_Why, int]]) -> str:
    """几处没写出来的原因拼成一句。failures 是 (原因, 属于这张表的第几段)。

    同样的原因只说一次（最多说两种）；同一个原因落在几段上说「这几段」，同一段拆出来的几块仍是「这一段」；拆小重试过、
    止损的说明各在最后补一次（只看说出来的那几种）。不然宽表的表级那段拆不开、列分段拆过时，同一句原因会说两遍。"""
    failures = list(failures)
    parts: dict[str, set[int]] = {}
    for why, part in failures:
        parts.setdefault(why.base, set()).add(part)
    shown = list(parts)[:2]
    told = [w for w, _ in failures if w.base in shown]
    text = "；".join(base.replace(SEG, SEG_MANY if len(parts[base]) > 1 else SEG_ONE) for base in shown)
    if any(w.split for w in told):
        text += REASON_SPLIT
    if any(w.halted for w in told):
        text += REASON_HALTED.format(n=max(1, int(C.MODEL_GIVE_UP_AFTER)))
    return text


def _columns_text(order: Sequence[str], columns: Iterable[str], *, count: bool) -> str:
    """说清是哪几列（列名照表结构原样，按表结构的顺序）：一到三列逐个写；更多的连着就写「头 到 尾」，不连着写前三个加
    「等」。count：后面带上列数（「，共 33 列」）。"""
    pos = {name: i for i, name in enumerate(order)}
    cols = sorted(dict.fromkeys(columns), key=lambda c: pos.get(c, len(pos)))
    n = len(cols)
    if n <= 3:
        text = "、".join(cols)
    elif pos.get(cols[-1], -1) - pos.get(cols[0], 0) + 1 == n:
        text = f"{cols[0]} 到 {cols[-1]}"
    else:
        head = "、".join(cols[:3])
        return f"{head} 等 {n} 列" if count else f"{head} 等"
    return f"{text}，共 {n} 列" if count else text


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
    if cut or (not texts and _thought(raw)):
        return _Reply(exhausted=True)      # 停在额度上，或者正文为空而有思考（同 _text_reply）
    if not texts:
        return _Reply()                    # 什么都没写：按空壳处理
    # 写了字却不是约定的格式、也没停在额度上：网关接受了结构化输出的参数却没照做，模型写成了说明文字。有没有思考都
    # 一样，不是额度的问题；换纯文本（另带格式说明）还能写出来
    return _Reply(error=REASON_FORMAT, prose=True)


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


def _usage_of(raw: Any) -> tuple[int, int]:
    """一次调用的用量 (输入, 输出)：拿不到的是 0（测试的假模型没有原始回复）。"""
    meta = getattr(raw, "usage_metadata", None) or {}
    return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)


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
    failed: dict[str, _Why] = field(default_factory=dict)
    parts_done: int = 0
    finished: bool = False

    def whole(self, piece: _Piece) -> bool:
        return piece.table_level and len(piece.columns) == len(self.columns)

    def has_any(self) -> bool:
        """这张表已经拿到了模型起草的内容（表级的项或任何一列）。"""
        return self.got_table or bool(self.got_columns)

    def wrote(self, piece: _Piece) -> bool:
        """这一段请写的部分拿到了内容。"""
        return (piece.table_level and self.got_table) or any(c in self.got_columns for c in piece.columns)

    def part_of(self) -> dict[str, int]:
        """每一列（表级的项是 _TABLE）属于这张表的第几段。"""
        out = {col: piece.part for piece in self.parts for col in piece.columns}
        out[_TABLE] = 1
        return out


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


@dataclass
class _Work:
    """排队起草的一段（顶层的段：窄表拼成的整表段、宽表的表级那段或一个列分段）。then：这一段写完才能起草的段（宽表
    的表级那段写完，才轮到它的列分段）。"""

    pieces: list[_Piece]
    then: list[_Work] = field(default_factory=list)


@dataclass
class _Budget:
    """一段（新开始的一段，连同它拆出来的、补问的、重试的）还能调用几次模型。"""

    left: int


def _pack(pieces: list[_Piece], cap: int) -> list[list[_Piece]]:
    """多表的段按顺序重新拼成每段不超过 cap 列（超过 cap 的表自成一段）。"""
    groups: list[list[_Piece]] = []
    used = 0
    for piece in pieces:
        n = len(piece.columns)
        if not groups or used + n > cap:
            groups.append([])
            used = 0
        groups[-1].append(piece)
        used += n
    return groups


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
        #: 这次起草同时在等名额或正在进行的调用最多几个（全进程的名额另算，见 model_gate）
        self.gate = asyncio.Semaphore(max(1, int(C.MODEL_CONCURRENCY)))
        #: 连续几段拆到底仍一项没写出来（止损用，有一段写出来就清零）
        self.dead_streak = 0
        #: 新开始的段每段最多几列：额度用完或超时（strikes）累计 MODEL_SHRINK_AFTER 次减半一次
        self.part_columns = max(1, int(C.MODEL_BATCH_COLUMNS))
        self.strikes = 0
        #: 最近几次调用没写出来的原因种类（写出来了是 None），提醒看它
        self.recent: collections.deque[str | None] = collections.deque(maxlen=max(1, int(C.MODEL_NOTICE_WINDOW)))
        self.notice: str | None = None

    @property
    def halted(self) -> bool:
        """止损：连续 MODEL_GIVE_UP_AFTER 段一项都没写出来，后面的段不再拆小重试。"""
        return self.dead_streak >= max(1, int(C.MODEL_GIVE_UP_AFTER))

    # ---------------------------------------------------------------- 总流程

    async def run(self) -> dict[str, ModelOutcome]:
        """按计划的先后起草：同时只起草 MODEL_CONCURRENCY 段，一段（连同拆小、补缺）做完再取下一段；宽表的表级那段
        写完，它的列分段排到队尾（先来先服务：排在已经排着的窄表段后面，不插队）。不一下子把所有段（包括宽表的全部列
        分段）都排上去：那样后开始的另一次起草要等这次排着的段全部调完；拆小重试也排在所有段的第一次调用后面，一段要
        等几乎所有调用都做完才结束，止损形同虚设。"""
        queue: collections.deque[_Work] = collections.deque()
        for unit in self._plan():
            if isinstance(unit, list):
                queue.append(_Work([self.tables[n].parts[0] for n in unit]))
            else:
                head, *rest = self.tables[unit].parts
                # 表级那段先写：列分段拿它做背景
                queue.append(_Work([head], [_Work([p]) for p in rest]))
        left = {name: len(st.parts) for name, st in self.tables.items()}
        busy = 0
        changed = asyncio.Event()

        async def worker() -> None:
            nonlocal busy
            while True:
                if queue:
                    work = queue.popleft()
                    busy += 1
                    try:
                        await self._work(work, queue, left)
                    finally:
                        busy -= 1
                        changed.set()
                    continue
                if not busy:
                    return
                changed.clear()       # 队列空着、别的段还在起草（宽表的表级那段写完会排上列分段）：等它们
                await changed.wait()

        await asyncio.gather(*(worker() for _ in range(max(1, int(C.MODEL_CONCURRENCY)))))
        return {name: self._outcome(t) for name, t in self.tables.items()}

    def _plan(self) -> list[list[str] | str]:
        """分段：窄表按顺序拼成整表段（列表），宽表一个一项（表名），列分段均分（_even）。各项按第一张表在 tables 里的
        位置排。"""
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
                    _Piece(name, cols, False, k + 2) for k, cols in enumerate(_even(st.columns, -(-n // width)))]
                units.append(name)
                continue
            st.parts = [_Piece(name, tuple(st.columns), True, 1)]
            if pack is None or len(pack) >= most or used + n > width:
                pack, used = [], 0
                units.append(pack)
            pack.append(name)
            used += n
        return units

    async def _work(self, work: _Work, queue: collections.deque[_Work], left: dict[str, int]) -> None:
        """起草一段；它之后的段（宽表的列分段）排到队尾；一张表的段都结束了，这张表的模型部分就结束了。"""
        await self._start(work.pieces)
        for piece in work.pieces:
            await self._part_done(piece.table)
        queue.extend(work.then)
        for piece in work.pieces:
            left[piece.table] -= 1
            if not left[piece.table]:
                await self._table_done(piece.table)

    async def _start(self, pieces: list[_Piece]) -> None:
        """开始起草一段。超过当前的列数上限（额度用完、超时多了会减半）的先按上限分开：多表的段按表重新拼，单张表的
        按列均分（表级的项跟着第一块，先写它，后面几块拿它做背景）。分开的每一块各算一段（各有调用次数的上限）。"""
        cap = max(1, self.part_columns)
        if sum(len(p.columns) for p in pieces) <= cap:
            await self._guarded(pieces)
            return
        if len(pieces) > 1:
            await asyncio.gather(*(self._start(group) for group in _pack(pieces, cap)))
            return
        piece = pieces[0]
        chunks = _even(piece.columns, -(-len(piece.columns) // cap))
        first = [_Piece(piece.table, chunks[0], piece.table_level, piece.part)]
        rest = [[_Piece(piece.table, cols, False, piece.part)] for cols in chunks[1:]]
        if piece.table_level:
            await self._guarded(first)
            await asyncio.gather(*(self._guarded(group) for group in rest))
        else:
            await asyncio.gather(*(self._guarded(group) for group in [first, *rest]))

    async def _guarded(self, pieces: list[_Piece]) -> None:
        """起草一段；意料之外的错误只记这一段没写出来，不影响别的段和别的表。"""
        try:
            await self._segment(pieces, 0, False, _Budget(max(1, int(C.MODEL_PART_MAX_CALLS))))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            from app.core.errors import describe_exception, first_line

            logger.exception("起草数据目录时出错：%s", "、".join(p.table for p in pieces))
            self._fail(pieces, REASON_CRASH.format(line=first_line(e) or describe_exception(e)))
        self._tally(pieces)

    def _tally(self, pieces: list[_Piece]) -> None:
        """一段（连同拆小、补缺）结束：一项都没写出来的接着数，写出来了清零。"""
        if any(self.tables[p.table].wrote(p) for p in pieces):
            self.dead_streak = 0
            return
        self.dead_streak += 1
        if self.dead_streak == max(1, int(C.MODEL_GIVE_UP_AFTER)):
            logger.warning("起草数据目录：模型连续 %d 段都没写出来，这次起草后面的段不再拆小重试", self.dead_streak)

    # ---------------------------------------------------------------- 一段

    async def _segment(self, pieces: list[_Piece], depth: int, followup: bool, budget: _Budget, *,
                       halved: bool = False, mode: str | None = None, last: bool = False) -> None:
        """问一段，拿到的并进各表；按失败原因拆开、补缺、记下没写出来的。

        depth：已经拆了几层（截断后只补剩下的也算一层）。followup：这一段是在补缺（补过就不再补，见 _after_followup）。
        budget：这一段还能调用几次。halved：这几块是真拆小（_split）出来的——原因里「已拆小重试」只在这时写。
        mode / last：换纯文本问的那一次——拆不开的空壳、结构化输出回了说明文字（再不行就记没写出来）。"""
        reply = await self._ask(pieces, budget, mode)
        if reply.capped:
            self._fail(pieces, self._capped(halved))
            return
        if reply.timeout:
            await self._report(reply, wrote=False)
            await self._split(pieces, depth, "timeout", followup, budget, halved)
            return
        if reply.error is not None:
            await self._report(reply, wrote=False)
            if reply.prose and not last:
                await self._prose_to_text(pieces, depth, followup, budget, halved)
                return
            self._fail(pieces, _Why(reply.error, split=halved))
            return
        gains = self._take(pieces, reply.entries)
        anything = any(g.items for g in gains)
        await self._report(reply, wrote=anything)
        if last and anything and self.mode != "text":
            # 结构化输出只给空壳、纯文本写出来了：这个模型的结构化输出靠不住，这次起草后面都走纯文本
            self.mode = "text"
            logger.info("起草数据目录：结构化输出只给了空壳，纯文本写出来了，后面改走纯文本")
        if reply.exhausted or reply.truncated:
            rest = self._remainder(pieces, gains, truncated=True, followup=followup)
            if not rest:
                return
            kind = "exhausted" if reply.exhausted else "truncated"
            if not anything:
                await self._split(rest, depth, kind, followup, budget, halved)
            elif depth < int(C.MODEL_SPLIT_DEPTH):
                # 捡回了一部分：只补剩下的（不是拆小）
                await self._segment(rest, depth + 1, followup, budget, halved=halved)
            else:
                self._fail(rest, self._reason(kind, split=halved))
            return
        if not anything and not followup:
            if last:
                halted = self._stopped(pieces, depth)
                self._fail(pieces, lambda p: self._reason("hollow", split=halved, piece=p, halted=halted))
                return
            await self._split(pieces, depth, "hollow", followup, budget, halved)
            return
        rest = self._remainder(pieces, gains, truncated=False, followup=followup)
        if not rest:
            return
        if followup:
            await self._after_followup(rest, depth, budget, halved, last=last)
            return
        await self._segment(rest, depth, True, budget, halved=halved)

    async def _after_followup(self, rest: list[_Piece], depth: int, budget: _Budget, halved: bool, *,
                              last: bool) -> None:
        """补问过仍然没给的部分（补过就不再补）。

        - 这张表已经拿到了内容：补问时仍没列出来的列记没写出来（这张表是 partial）。补问时列出了、字段留空的列是拿不准
          （记留空），不在这里（_remainder 只把没列出来的列交过来）；
        - 这张表到现在还一项都没有（回复里没有这张表、只给了空壳）：照第一次只给空壳的规矩，拆小再问，拆不开再问一次。
          拆出来的半段是新的一段（还能补缺一次），层数照算。"""
        given = [p for p in rest if self.tables[p.table].has_any()]
        empty = [p for p in rest if not self.tables[p.table].has_any()]
        if given:
            self._fail(given, _Why(REASON_UNANSWERED, split=halved))
        if not empty:
            return
        if last:
            halted = self._stopped(empty, depth)
            self._fail(empty, lambda p: self._reason("hollow", split=halved, piece=p, halted=halted))
            return
        await self._split(empty, depth, "hollow", False, budget, halved)

    async def _prose_to_text(self, pieces: list[_Piece], depth: int, followup: bool, budget: _Budget,
                             halved: bool) -> None:
        """结构化输出回了一段说明文字：网关接受了结构化输出的参数却没照做。这次起草后面都走纯文本（另带格式说明），
        这一段换纯文本问一次。纯文本额度用完了还能照常拆小（拆出来的半段也走纯文本，不再先问一遍结构化输出）。"""
        if self.mode != "text":
            self.mode = "text"
            logger.info("起草数据目录：结构化输出回了说明文字，后面改走纯文本")
        await self._segment(pieces, depth, followup, budget, halved=halved, mode="text", last=True)

    def _stopped(self, pieces: list[_Piece], depth: int) -> bool:
        """这几段本来还能拆小，因为前面连续几段都没写出来（止损）才没拆。"""
        return self.halted and depth < int(C.MODEL_SPLIT_DEPTH) and _halves(pieces) is not None

    async def _split(self, pieces: list[_Piece], depth: int, kind: str, followup: bool, budget: _Budget,
                     halved: bool) -> None:
        """拆成两半再问（额度用完、超时、只给空壳、截断后一项都没捡回来）。拆不开的空壳再问一次：结构化输出给的换纯文本，
        已经走纯文本的原样再问；别的原因拆不开就记没写出来。"""
        halves = _halves(pieces) if depth < int(C.MODEL_SPLIT_DEPTH) else None
        halted = halves is not None and self.halted
        if halves is not None and not halted:
            first, second = halves
            if (len(first) == len(second) == 1 and first[0].table == second[0].table and first[0].table_level):
                # 同一张表按列拆、前一半带着表级的项：先写前一半，后一半拿它做背景
                await self._segment(first, depth + 1, followup, budget, halved=True)
                await self._segment(second, depth + 1, followup, budget, halved=True)
                return
            results = await asyncio.gather(self._segment(first, depth + 1, followup, budget, halved=True),
                                           self._segment(second, depth + 1, followup, budget, halved=True),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return
        if kind == "hollow":
            await self._segment(pieces, depth, followup, budget, halved=halved, mode="text", last=True)
            return
        self._fail(pieces, lambda p: self._reason(kind, split=halved, piece=p, halted=halted))

    def _remainder(self, pieces: list[_Piece], gains: list[_Gain], *, truncated: bool,
                   followup: bool) -> list[_Piece]:
        """还没写出来的部分：回复里没有这张表、或者这张表一项可用内容都没有的整段重问；有内容的只问没列出来的列
        （列出了、字段留空的算模型拿不准，不再问）。截断的回复里表级的项也没拿到的，表级的项一起重问（没截断时表级
        空着算模型拿不准：这张表在回复里，表级的字段留空和列的字段留空一样）。

        补问（followup）时这张表已经有内容了：补问的列列出来了就算答过（字段留空是拿不准），只交回没列出来的列。"""
        out: list[_Piece] = []
        for piece, gain in zip(pieces, gains):
            answered_blank = followup and gain.named and self.tables[piece.table].has_any()
            if not gain.named or (not gain.items and not answered_blank):
                out.append(piece)
                continue
            cols = tuple(c for c in piece.columns if c not in gain.answered)
            table_level = piece.table_level and truncated and not gain.table_items
            if cols or table_level:
                out.append(_Piece(piece.table, cols, table_level, piece.part))
        return out

    # ---------------------------------------------------------------- 调用

    async def _ask(self, pieces: list[_Piece], budget: _Budget, mode: str | None = None) -> _Reply:
        """调一次模型（按原因重来：额度被拒、结构化输出不支持、网络错误……），交回读好的回复。

        这一段的调用次数用完了交回 capped（不调用）。超时不原样重试：交回 timeout，由调用方当作这一段太大、拆小再问。
        重来之前的那次调用在这里就报给进度回调；交回的那次由调用方读完内容再报（写没写出来要看校验之后的内容）。"""
        mode = mode or self.mode
        retried = 0
        while True:
            if budget.left <= 0:
                return _Reply(capped=True)
            budget.left -= 1
            used = getattr(self.model, "budget", None)
            try:
                # 先占这次起草自己的名额，再去排全进程的：一次起草排在全进程名额上的调用最多 MODEL_CONCURRENCY 个
                async with self.gate, model_gate():
                    await self._hook(self.hooks.call_started, [p.table for p in pieces])
                    reply, raw = await asyncio.wait_for(self._call(pieces, mode), timeout=C.MODEL_TIMEOUT_S)
                reply.usage = _usage_of(raw)
                return reply
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 按原因分别处理
                kind = classify_failure(e)
                if kind == "length":
                    return self._length_reply(e)
                if kind == "timeout":
                    return _Reply(timeout=True, usage=(0, 0))
                if kind == "budget" and (getattr(self.model, "budget", None) != used or self._shrink()):
                    await self._called((0, 0), "error")
                    continue
                if kind == "unsupported" and mode == "structured":
                    if self.mode == "structured":
                        logger.info("起草数据目录：结构化输出用不了（%s），后面改走纯文本", str(e)[:200])
                    self.mode = mode = "text"
                    await self._called((0, 0), "error")
                    continue
                if kind == "transient" and retried < int(C.MODEL_RETRIES):
                    await self._called((0, 0), "error")
                    delays = tuple(C.MODEL_RETRY_DELAYS_S) or (0.0,)
                    await _sleep(delays[min(retried, len(delays) - 1)])
                    retried += 1
                    continue
                return _Reply(error=self._call_error(e, retried), usage=(0, 0))

    async def _call(self, pieces: list[_Piece], mode: str) -> tuple[_Reply, Any]:
        """一次调用：(读好的回复, 原始回复)。原始回复给用量用（测试的假模型没有，是 None）。"""
        messages = self._messages(pieces, mode)
        if mode == "structured":
            runnable = self.model.with_structured_output(C.CATALOG_DRAFT_PROMPT_SCHEMA, include_raw=True)
            raw, parsed = _unpack(await runnable.ainvoke(messages))
            return _structured_reply(raw, parsed), raw
        raw = await self.model.ainvoke(messages)
        return _text_reply(raw), raw

    @staticmethod
    def _length_reply(e: BaseException) -> _Reply:
        """LengthFinishReasonError：额度用完了。异常里带着那次的回复（和用量），能捡回来的照捡。"""
        completion = getattr(e, "completion", None)
        usage = getattr(completion, "usage", None)
        tokens = (int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0))
        try:
            text = completion.choices[0].message.content or ""  # type: ignore[union-attr]
        except (AttributeError, IndexError, TypeError):
            text = ""
        if text.strip():
            reply = _parse_text(text, True)
            if reply is not None and reply.entries:
                return _Reply(entries=reply.entries, truncated=True, exhausted=True, usage=tokens)
        return _Reply(exhausted=True, usage=tokens)

    def _shrink(self) -> bool:
        shrink = getattr(self.model, "shrink", None)
        if callable(shrink) and shrink():
            logger.info("起草数据目录：服务方不接受这么大的输出额度，这次起草改用 %s", getattr(self.model, "budget", ""))
            return True
        return False

    def _budget(self) -> int:
        return int(getattr(self.model, "budget", None) or C.MODEL_MAX_TOKENS)

    @staticmethod
    def _call_error(e: BaseException, retried: int) -> str:
        from app.core.errors import describe_exception, first_line

        tail = REASON_RETRIED.format(n=retried) if retried else ""
        return REASON_CALL.format(line=first_line(e) or describe_exception(e)) + tail

    # ---------------------------------------------------------------- 每次调用之后：用量、段大小、提醒

    async def _report(self, reply: _Reply, *, wrote: bool) -> None:
        """交回的那次调用读完内容之后报给进度回调。"""
        if reply.usage is None:
            return
        if reply.timeout:
            failure: str | None = "timeout"
        elif reply.error is not None:
            failure = "error"
        elif wrote:
            failure = None
        else:
            failure = "exhausted" if reply.exhausted else "hollow"
        await self._called(reply.usage, failure, strike=reply.timeout or reply.exhausted)

    async def _called(self, usage: tuple[int, int], failure: str | None, *, strike: bool = False) -> None:
        """记下一次调用：额度用完或超时（strike）累计 MODEL_SHRINK_AFTER 次，新开始的段列数上限减半；最近几次调用多数
        没写出来时提醒，少了清掉。报给进度回调。"""
        self.recent.append(failure)
        if strike:
            self.strikes += 1
            width = max(1, int(C.MODEL_BATCH_COLUMNS))
            floor = min(width, max(1, int(C.MODEL_MIN_PART_COLUMNS)))
            cap = max(floor, width >> (self.strikes // max(1, int(C.MODEL_SHRINK_AFTER))))
            if cap < self.part_columns:
                self.part_columns = cap
                logger.info("起草数据目录：额度用完或超时已有 %d 次，之后新开始的段每段最多 %d 列", self.strikes, cap)
        failed = [f for f in self.recent if f is not None]
        if len(failed) >= int(C.MODEL_NOTICE_ON) or (self.notice is not None and len(failed) >= int(C.MODEL_NOTICE_OFF)):
            counts = collections.Counter(failed)
            kinds = "、".join(f"{label} {counts[kind]} 次" for kind, label in FAILURE_LABEL.items() if counts[kind])
            self.notice = NOTICE.format(calls=len(self.recent), failed=len(failed), kinds=kinds, place=DRAFT_MODEL_PLACE)
        else:
            self.notice = None
        await self._hook(self.hooks.call_done, CallReport(int(usage[0]), int(usage[1]), failure, self.notice,
                                                          self.part_columns))

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

    def _fail(self, pieces: list[_Piece], reason: _Why | str | Callable[[_Piece], _Why]) -> None:
        """这几段没写出来：还没拿到内容的列（和表级的项）记下原因。"""
        for piece in pieces:
            st = self.tables[piece.table]
            why = reason(piece) if callable(reason) else reason
            if isinstance(why, str):
                why = _Why(why)
            for col in piece.columns:
                if col not in st.got_columns:
                    st.failed.setdefault(col, why)
            if piece.table_level and not st.got_table:
                st.failed.setdefault(_TABLE, why)

    def _reason(self, kind: str, *, split: bool, piece: _Piece | None = None, halted: bool = False) -> _Why:
        if kind == "exhausted":
            base = REASON_EXHAUSTED.format(budget=self._budget(), seg=SEG)
        elif kind == "truncated":
            base = REASON_TRUNCATED.format(seg=SEG)
        elif kind == "timeout":
            base = REASON_TIMEOUT.format(seconds=_seconds(C.MODEL_TIMEOUT_S), seg=SEG)
        else:
            st = self.tables[piece.table] if piece is not None else None
            if st is not None and st.whole(piece):
                base = C.MODEL_EMPTY_REASON if st.named else REASON_MISSING
            else:
                base = REASON_HOLLOW
        return _Why(base, split=split, halted=halted)

    @staticmethod
    def _capped(halved: bool) -> _Why:
        return _Why(REASON_CAPPED.format(n=max(1, int(C.MODEL_PART_MAX_CALLS)), seg=SEG), split=halved)

    # ---------------------------------------------------------------- 收尾

    def _outcome(self, st: _Table) -> ModelOutcome:
        items = len(C._slots(st.notes))
        failed_cols = [c for c in st.columns if c in st.failed and c not in st.got_columns]
        failed_table = _TABLE in st.failed and not st.got_table
        total = len(st.parts) or 1
        part_of = st.part_of()
        if not items:
            failures = [(why, part_of.get(key, 1)) for key, why in st.failed.items()]
            reason = _join(failures) if failures else (C.MODEL_EMPTY_REASON if st.named else REASON_MISSING)
            return ModelOutcome("failed", {}, 0, reason, total, st.parts_done)
        notes = copy.deepcopy(st.notes)
        blank = self._blank(st, set(failed_cols))
        if failed_cols or failed_table:
            return ModelOutcome("partial", notes, items, self._partial_text(st, failed_cols, failed_table), total,
                                st.parts_done, blank)
        return ModelOutcome("ok", notes, items, None, total, st.parts_done, blank)

    def _blank(self, st: _Table, failed: set[str]) -> list[str]:
        """模型拿不准、留空了的列：请它写了（不在没写出来的里面），中文名和含义却都没给（或者给的都没通过校验）。已确认
        的、库注释已经说清的不算——提示词里它们以此为准，模型不写也不缺：已确认（有确证）的中文名或含义、库注释（按
        catalog.comment_role 记进目录的那种；导入表格的源是系统说明）。"""
        drafted = st.notes.get("columns") or {}
        confirmed = C._confirmed_only(st.existing).get("columns") or {}
        comments = {str(c.get("name")): (c.get("comment") or "").strip()
                    for c in st.meta.get("columns") or [] if isinstance(c, dict)}
        out: list[str] = []
        for col in st.columns:
            if col in failed:
                continue
            if any(f in (drafted.get(col) or {}) or f in (confirmed.get(col) or {}) for f in ("label", "meaning")):
                continue
            text = comments.get(col)
            if text and (self.system or C.comment_role(text, col) is not None):
                continue
            out.append(col)
        return out

    @staticmethod
    def _partial_text(st: _Table, failed_cols: list[str], failed_table: bool) -> str:
        """只写出一部分时说清哪部分没写出来（第几段、哪几列）、为什么、怎么办。"""
        part_of = st.part_of()
        keys = ([_TABLE] if failed_table else []) + failed_cols
        reason = _join((st.failed[k], part_of.get(k, 1)) for k in keys)
        n = len(st.columns)
        if len(st.parts) > 1:
            bad: dict[int, list[str]] = {}
            for col in failed_cols:
                bad.setdefault(part_of.get(col, 1), []).append(col)
            said = ["第 1 段（表级的项）"] if failed_table else []
            said += [f"第 {k} 段（{_columns_text(st.columns, cols, count=True)}）" for k, cols in sorted(bad.items())]
            head = f"这张表 {n} 列，分 {len(st.parts)} 段起草，{'、'.join(said)}没写出来"
        elif failed_cols and failed_table:
            head = (f"这张表 {n} 列，有表级的项和 {len(failed_cols)} 列（{_columns_text(st.columns, failed_cols, count=False)}）"
                    "没写出来")
        elif failed_cols:
            head = f"这张表 {n} 列，有 {len(failed_cols)} 列（{_columns_text(st.columns, failed_cols, count=False)}）没写出来"
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


__all__ = ["BUDGET_WORDS", "Background", "CallReport", "FAILURE_LABEL", "Hooks", "ModelOutcome", "NOTICE", "REASON_CALL",
           "REASON_CAPPED", "REASON_EXHAUSTED", "REASON_FORMAT", "REASON_HALTED", "REASON_HOLLOW", "REASON_MISSING",
           "REASON_RETRIED", "REASON_SPLIT", "REASON_TIMEOUT", "REASON_TRUNCATED", "REASON_UNANSWERED", "SEG",
           "classify_failure", "model_gate", "parts_for", "run_model_draft"]
