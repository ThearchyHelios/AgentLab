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
  MODEL_SPLIT_MIN_COLUMNS 列，最多拆 MODEL_SPLIT_DEPTH 层。同一张表按列拆时带表级项的前一半先写、后一半拿它做背景；
  超时的两半同时问（一次超时就是一个时限，串着问 40 → 20 → 10 要好几个时限）。拆不开的空壳再问一次：结构化输出给的
  换纯文本（有的网关的结构化输出只给空壳、纯文本却能写——写出来了，这次起草后面都走纯文本），已经走纯文本的原样再问；
  仍是空壳才记没写出来。
- 结构化输出回了一段说明文字（不是 JSON、也没停在额度上）：网关接受了结构化输出的参数却没照做（例如开了丢弃不认识
  的参数），提示词里又没有格式说明，模型就写成了说明文字。这次起草后面改走纯文本，这一段换纯文本问一次。有思考
  不等于额度用完：只有停在额度上、或者正文为空而有思考才算。
- 被截断的 JSON：先把已经完整的表、列对象捡回来，只补剩下的。
- 一段里缺了的表、列只补缺的，补一次。补问之后：模型列出了、字段留空的列是拿不准（补问时才列出来的也一样），不算
  没写出来（补问的那次调用把请写的都列出来了，用量和提醒里也不算没写出来）；整列没列出来的算没写出来（这张表是
  partial）；整张表还一项都没有的，照只给空壳处理（拆小、再问一次）。
- 结构化输出这个接口不支持（400 / 422 提到 response_format、json_schema、tool……，或者模型类不支持）：这次起草
  后面改走纯文本、从正文取 JSON（纯文本那条路另外说清 JSON 的格式）。
- 服务方嫌额度太大：退回 MODEL_FALLBACK_MAX_TOKENS 重来，这次起草后面都用它（catalog.DraftModel.shrink）。
- 网络错误、429、5xx：等一会儿重试（最多 MODEL_RETRIES 次，退避）。单次调用的时限是 MODEL_TIMEOUT_S。SDK
  自己不再重试（catalog.resolve_draft_model 建模型时 max_retries=0），实际重试次数和报错写的一致。
- 别的错误：这一段没写出来，原因照实写。
- 读回复时出了意料之外的错：这一块记没写出来（「起草这张表时出错」），这次调用照样记进用量、算一次出错。该是文字的
  地方写成了数组、对象的值在校验（catalog._from_model）里丢掉，不算出错。
- 封顶：每个原始段（计划里的整表段、表级那段或一个列分段，连同按缩小后的上限分开的各块、拆出来的、补问的、重试的）
  一共最多调用 MODEL_PART_MAX_CALLS 次，到了次数还没写完的记没写出来。
- 止损按比例：这次起草最近 MODEL_HALT_WINDOW 次调用里有 MODEL_HALT_ON 次以上没写出来（额度用完、超时、空壳、出错），
  之后的段不再拆小、每块只问一次（不拆小、不补问漏掉的列、截断后不再补剩下的、空壳不再换纯文本原样再问；拆出来还没问
  的块也不再问），报错建议更换起草模型，直到比例降下去。以前按「连续几段拆到底」「接连超时」算，偶尔一次写出来就清零：
  实测会思考的模型在宽表上单次 1 到 7 分钟、四分之一超时，90 分钟停不下来。几处只管通信的调整照旧：服务方嫌额度
  太大退回小额度、结构化输出不支持或回了说明文字改走纯文本、网络错误退避重试；结构化输出只给空壳的，这次起草还没换
  纯文本问过的照旧问一次（看看是不是结构化输出的问题）。止损的窗口只记每次调用的最终结局：上面这几种重来之前的那次
  不花额度、也说明不了模型写不出来，照旧报给进度回调（用量、提醒照算），不进止损的窗口——以前都记成没写出来，额度上限
  8192、不支持 json_schema 的 OpenAI 兼容网关上，并发的头几次调用全是这种重来，第一次额度用完就凑满 10 次里 7 次，
  那一段直接记没写出来、不再拆小。重试用完仍出错的那一次是最终结局，照常算。
- 同一张表连续 MODEL_TABLE_GIVE_UP_AFTER 个原始段都一项没写出来：这张表剩下还没开始的段跳过不问（宽表一段一段问下去
  只是烧额度），原因写清跳过了几段、可以换一个起草模型后只重试这张表。中间有一段写出来了就不算「接连」。
- 提醒：最近 MODEL_NOTICE_WINDOW 次调用里有 MODEL_NOTICE_ON 次以上没写出来（额度用完、超时、空壳、出错），给一句
  提醒（次数和原因，建议换起草模型、缩小起草范围或停止），经进度回调交给任务；之后少于 MODEL_NOTICE_OFF 次时清掉。
  以前看不出调用在反复失败：一个任务 55 分钟只结束 1 张表，界面上只有段进度和用量。

**原因。** 没写出来的原因只写进这张表的 model_error：几段因为同一个原因没写出来时说「这几段」，分段起草的表（只写
出一部分的、一项都没写出来的都一样）每段写上列的范围（「第 5 段（列名 到 列名，共 33 列）」，列名照表结构原样），
真拆小重试过的才补「已拆小重试仍没写出」，而且只跟在拆过的那几段后面说（说到的段只有一部分拆过时写「其中第 3 段」）。
只有一段的表，这一段就是整张表：只写出一部分的写清是哪几列，一项都没写出来的只写原因。

**留空的列。** 请模型写了、模型也列出了，中文名和含义却都没给（或者给的都没通过校验）的列是模型拿不准，不补问，
记在 ModelOutcome.blank 里（表的状态不因此变）。目录里已经有中文名或含义的列不算（不论来源，驳回的不算有：以前模型
起草的留着，照「补充起草要求再重新起草」做一遍，模型这次没再给的老列不该又说成拿不准），库注释已经说清的不算
（提示词里它们以此为准）；写入时（catalog.write_draft）再按并入目录之后的结果筛一遍。

**并发。** 全进程最多 MODEL_CONCURRENCY 个调用同时进行（model_gate：按事件循环各一个信号量，所有起草任务和同步
起草共用，先来先放）。一次起草里同时只起草 MODEL_CONCURRENCY 段（一段拆小、补缺都做完再起草下一段），同时在等
名额的调用也最多这么多个：不把几百段一下子都排上去，几次起草（不同数据源的任务、同步起草）的调用交替排队，后开始
的不会等前一个排着的段全部调完。一次起草里按表的先后排队：宽表的表级那段写完，它的列分段排到已经排着的段后面
（先来先服务），不插到窄表前面。

**进度。** 调用方给 Hooks：开始调用、每次调用的结果（用量、没写出来的原因、当前的提醒和列数上限）、一段结束、宽表的
一段写出来了（交出这张表到这时拿到的全部内容，调用方马上并入目录：停止、服务重启时已经写出来的段不丢）、块进度变了、
一张表的模型部分全部结束。后台起草任务据此落库（data/catalog_jobs.py）。块：这张表实际分成几块来问——一开始是段数，
一段被缩小的上限分开成几块、被拆成两半，块数就多几块；一块问完（写没写出来都算）算结束一块。

**防错事实。** 表级的项里模型可以提议有效行过滤（valid_filter）和去重规则（dedup），各附一句依据（写进这一项的 note），
校验见 catalog._from_model（有效行过滤按方言解析，不过就丢掉）。开了数据剖析的源（catalog_profile.profile_settings）：
目录里记下的码值交给模型做背景（遮罩的列不给），表级那段写完后经剖析的只读守卫数一数有效行过滤排除了多少行
（catalog_profile.check_valid_filter），排除 0% 或 100% 的不收，收下的 note 写上排除比例；没开剖析、连不上的照收，
note 写没有核对的原因。

交给模型的是表结构（列、类型、主键、外键、唯一约束、注释）和使用者补充的背景；开了数据剖析的源另给目录里记下的码值
（剖析取过的取值和含义，遮罩的列不给），别的数据值一概不发。写给模型的文字都在 catalog.py 名字带 _SYSTEM / _PROMPT
的常量里；这里的 REASON_* 是给人看的（拼进这张表的 model_error）。
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
#: 说到的几段里只有一部分真拆小重试过：只说那几段
REASON_SPLIT_PARTS = "；其中第 {parts} 段已拆小重试仍没写出"
REASON_HOLLOW = "模型只给了空壳，没有写出可用的内容"
REASON_TRUNCATED = "模型的输出被截断，{seg}没写完"
REASON_FORMAT = "模型的回复不是约定的格式"
REASON_MISSING = "模型的回复里没有这张表"
REASON_UNANSWERED = "模型的回复里漏了这些列，补问一次仍没有给出"
#: 止损期间每块只问一次：漏了的列不补问
REASON_OMITTED = "模型的回复里漏了这些列"
REASON_TIMEOUT = "模型 {seconds} 秒内没有写完{seg}"
REASON_CALL = "调用模型出错：{line}"
REASON_RETRIED = "（已重试 {n} 次）"
REASON_CRASH = "起草这张表时出错：{line}"
#: 一段到了调用次数的上限（MODEL_PART_MAX_CALLS）还没写完
REASON_CAPPED = "每段最多调用 {n} 次模型，{seg}到了次数仍没写完，已停止重试"
#: 止损（最近几次调用多数没写出来，之后的段不再拆小、每块只问一次）时补在原因后面
REASON_HALTED = "；最近 {window} 次调用有 {on} 次以上没写出来，已停止拆小重试，建议更换起草模型"
#: 同一张表接连几段都没写出来，剩下的段跳过不问
REASON_ABANDONED = "这张表接连 {k} 段都没写出来，剩下的 {n} 段已跳过，可以换一个起草模型后只重试这张表"
#: 跳过不问的段在「没写出来」记录里的原因（不拼进原因，见 _Run._outcome）
_SKIPPED = "\0skipped"

# ---- 提醒（最近几次调用多数没写出来），经进度回调交给任务（CatalogDraftJob.notice）
NOTICE = ("最近 {calls} 次调用有 {failed} 次没写出来（{kinds}）。建议在{place}换一个模型、缩小起草范围，"
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
    （整表段是 1，宽表是 1 + 列分段数），parts_done 已经结束了几段，parts_written 其中写出了内容的段数。blank：模型拿
    不准、留空了的列（按表结构的顺序；表的状态不因此变）。chunks_total、chunks_done：这张表实际分成的块数和已结束的
    块数（段被缩小的上限分开、拆成两半后会变多）。"""

    status: str
    notes: dict[str, Any]
    items: int
    error: str | None
    parts_total: int
    parts_done: int
    blank: list[str] = field(default_factory=list)
    parts_written: int = 0
    chunks_total: int = 0
    chunks_done: int = 0


@dataclass(frozen=True)
class PartWrite:
    """宽表写出了一段（进度回调 part_written 收到的）：notes 是这张表到这时拿到的全部模型起草（已校验、已核对，深拷贝），
    parts_written 是其中写出了内容的段数。调用方马上并入目录（这一次不删任何东西），整张表结束时另有 table_done 收尾。"""

    notes: dict[str, Any]
    parts_written: int


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
    - part_written(表名, PartWrite)：宽表写出了一段、后面还有段没结束（最后一段由 table_done 收尾；只有一段的表不调）；
    - chunks_changed(表名, 块数, 已结束的块数)：块进度变了；
    - table_done(表名, ModelOutcome)：这张表的模型部分全部结束。"""

    call_started: Callable[[list[str]], Awaitable[None]] | None = None
    call_done: Callable[[CallReport], Awaitable[None]] | None = None
    part_done: Callable[[str], Awaitable[None]] | None = None
    table_done: Callable[[str, ModelOutcome], Awaitable[None]] | None = None
    part_written: Callable[[str, PartWrite], Awaitable[None]] | None = None
    chunks_changed: Callable[[str, int, int], Awaitable[None]] | None = None


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
    #: 超时拆出来的块，轮到它时已经止损：这次没有调用（见 _Run._ask 的 optional）
    skipped: bool = False
    #: 这次调用的用量 (输入, 输出)，还没报给进度回调；没有调用（capped）的是 None
    usage: tuple[int, int] | None = None


@dataclass(frozen=True)
class _Why:
    """一部分没写出来的原因。base 给人看的原因（可以带 SEG，拼成一句时换成「这一段」「这几段」；跳过不问的段是 _SKIPPED，
    不拼进原因）；split 真拆小重试过；halted 本来还能拆小、补问或再问、因为止损才没问时止损的说明（REASON_HALTED：最近
    几次调用多数没写出来），没有是空串。"""

    base: str
    split: bool = False
    halted: str = ""


def _join(failures: Iterable[tuple[_Why, int]]) -> str:
    """几处没写出来的原因拼成一句。failures 是 (原因, 属于这张表的第几段)。

    同样的原因只说一次（最多说两种）；同一个原因落在几段上说「这几段」，同一段拆出来的几块仍是「这一段」；拆小重试过、
    止损的说明各在最后补一次（只看说出来的那几种）。不然宽表的表级那段拆不开、列分段拆过时，同一句原因会说两遍。

    「已拆小重试仍没写出」只跟在真拆过的那几段后面说：说到的段都拆过（只有一段的表就是这一段）时不点名，只有一部分拆过
    时写「其中第 3、4 段已拆小重试仍没写出」。以前一律补在最后，读起来像每一段都拆过。"""
    failures = list(failures)
    parts: dict[str, set[int]] = {}
    for why, part in failures:
        parts.setdefault(why.base, set()).add(part)
    shown = list(parts)[:2]
    told = [(w, part) for w, part in failures if w.base in shown]
    text = "；".join(base.replace(SEG, SEG_MANY if len(parts[base]) > 1 else SEG_ONE) for base in shown)
    split = sorted({part for w, part in told if w.split})
    if split:
        everywhere = {part for _, part in told}
        text += REASON_SPLIT if set(split) == everywhere else \
            REASON_SPLIT_PARTS.format(parts="、".join(str(part) for part in split))
    for note in dict.fromkeys(w.halted for w, _ in told if w.halted):
        text += note
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
    #: 写出了内容的段（第几段）
    written_parts: set[int] = field(default_factory=set)
    #: 接连几个原始段一项都没写出来（写出来就清零）；到了 MODEL_TABLE_GIVE_UP_AFTER 段，剩下的段跳过不问
    dead_parts: int = 0
    abandoned: bool = False
    #: 跳过不问的段（第几段）
    skipped_parts: list[int] = field(default_factory=list)
    #: 块进度：实际分成几块、结束了几块
    chunks_total: int = 0
    chunks_done: int = 0
    #: 有效行过滤核对过了（表级那段写完核对一次）
    vetted: bool = False

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


@dataclass
class _Chunk:
    """一块：一次拿来问的几张表（多表拼成的段每张表各是一块）。补问、截断后补剩下的、换纯文本再问都还是这一块；拆成两半
    （单张表按列拆，多一块；多表的段按表拆，各张表仍是一块）后，这一块里被拆的表交给新的块，split 记下它们。"""

    tables: tuple[str, ...]
    split: set[str] = field(default_factory=set)


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
        #: 新开始的段每段最多几列：额度用完或超时（strikes）累计 MODEL_SHRINK_AFTER 次减半一次
        self.part_columns = max(1, int(C.MODEL_BATCH_COLUMNS))
        self.strikes = 0
        #: 最近几次调用没写出来的原因种类（写出来了是 None），提醒看它
        self.recent: collections.deque[str | None] = collections.deque(maxlen=max(1, int(C.MODEL_NOTICE_WINDOW)))
        self.notice: str | None = None
        #: 最近 MODEL_HALT_WINDOW 次调用没写出来没有（止损按比例看它）
        self.window: collections.deque[bool] = collections.deque(maxlen=max(1, int(C.MODEL_HALT_WINDOW)))
        self.was_halted = False
        #: 这次起草里结构化输出只给空壳时换纯文本问过没有（止损期间只问这一次）
        self.text_probed = False
        #: 数据源：开了数据剖析的源把码值交给模型、核对有效行过滤的排除比例（遮罩的列都不碰）
        from app.data import catalog_profile
        from app.data.engine import masked_columns

        self.source = source
        self.kind = getattr(source, "kind", None)
        options = getattr(source, "options", None)
        self.profiling = catalog_profile.profile_settings(options if isinstance(options, dict) else None)
        self.masked = {c.lower() for c in masked_columns(options if isinstance(options, dict) else None)}

    def _halted(self) -> bool:
        """止损：最近 MODEL_HALT_WINDOW 次调用里有 MODEL_HALT_ON 次以上没写出来。之后的段不再拆小、每块只问一次，直到
        比例降下去（偶尔一次写出来不会清零）。"""
        return sum(self.window) >= max(1, int(C.MODEL_HALT_ON))

    def _halt_note(self) -> str:
        """止损的说明（拼在原因后面），没止损是空串。"""
        if not self._halted():
            return ""
        return REASON_HALTED.format(window=max(1, int(C.MODEL_HALT_WINDOW)), on=max(1, int(C.MODEL_HALT_ON)))

    @staticmethod
    def _floor() -> int:
        """段大小减半的下限（每段最少几列）。"""
        return min(max(1, int(C.MODEL_BATCH_COLUMNS)), max(1, int(C.MODEL_MIN_PART_COLUMNS)))

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
        for st in self.tables.values():
            st.chunks_total = len(st.parts)
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
        """起草一段；它之后的段（宽表的列分段）排到队尾；一张表的段都结束了，这张表的模型部分就结束了。

        这一段（原始段：计划里的整表段、表级那段或一个列分段）连同按缩小后的上限分开的各块、拆出来的、补问的、重试的，
        一共最多调用 MODEL_PART_MAX_CALLS 次（各块共用一个 _Budget）。表级那段写完先核对模型提议的有效行过滤（_vet），
        再交出去。接连几段没写出来的表（_tally），剩下的段跳过不问。宽表写出了一段、后面还有段没结束，就把这张表到这时
        拿到的内容交给调用方并入目录（part_written）；最后一段结束由 table_done 收尾。"""
        asked = [p for p in work.pieces if not self.tables[p.table].abandoned]
        for piece in work.pieces:
            if self.tables[piece.table].abandoned:
                await self._skip_part(piece)
        if asked:
            await self._start(asked, _Budget(max(1, int(C.MODEL_PART_MAX_CALLS))))
            for piece in asked:
                if piece.table_level:
                    await self._vet(self.tables[piece.table])
            self._tally(asked)
        for piece in work.pieces:
            await self._part_done(piece.table)
        queue.extend(work.then)
        for piece in work.pieces:
            left[piece.table] -= 1
            if not left[piece.table]:
                await self._table_done(piece.table)
            elif piece.part in self.tables[piece.table].written_parts:
                await self._part_written(piece.table)

    async def _start(self, pieces: list[_Piece], budget: _Budget) -> None:
        """开始起草一段。超过当前的列数上限（额度用完、超时多了会减半）的先按上限分开：多表的段按表重新拼，单张表的
        按列均分（表级的项跟着第一块，先写它，后面几块拿它做背景）。分开的各块同时起草，共用这一段的调用次数（budget）：
        以前每块各有 MODEL_PART_MAX_CALLS 次，上限缩到 10 列以后一段 40 列最多能调用 32 次。"""
        cap = max(1, self.part_columns)
        if sum(len(p.columns) for p in pieces) <= cap:
            await self._guarded(pieces, budget)
            return
        if len(pieces) > 1:
            await asyncio.gather(*(self._start(group, budget) for group in _pack(pieces, cap)))
            return
        piece = pieces[0]
        chunks = _even(piece.columns, -(-len(piece.columns) // cap))
        await self._more_chunks(piece.table, len(chunks) - 1)
        first = [_Piece(piece.table, chunks[0], piece.table_level, piece.part)]
        rest = [[_Piece(piece.table, cols, False, piece.part)] for cols in chunks[1:]]
        if piece.table_level:
            await self._guarded(first, budget)
            await asyncio.gather(*(self._guarded(group, budget) for group in rest))
        else:
            await asyncio.gather(*(self._guarded(group, budget) for group in [first, *rest]))

    async def _guarded(self, pieces: list[_Piece], budget: _Budget) -> None:
        """起草一块（一段，或它按上限分开的一块）；意料之外的错误只记这一块没写出来，不影响别的段和别的表。"""
        chunk = _Chunk(tuple(dict.fromkeys(p.table for p in pieces)))
        try:
            await self._segment(pieces, 0, False, budget, chunk=chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            from app.core.errors import describe_exception, first_line

            logger.exception("起草数据目录时出错：%s", "、".join(p.table for p in pieces))
            self._fail(pieces, REASON_CRASH.format(line=first_line(e) or describe_exception(e)))
        await self._chunk_done(chunk)

    def _tally(self, pieces: list[_Piece]) -> None:
        """一段（原始段，连同它分开的各块、拆小、补缺）结束：记下这一段写出内容没有；同一张表接连
        MODEL_TABLE_GIVE_UP_AFTER 段一项都没写出来，这张表剩下的段跳过不问（写出来了清零）。"""
        give_up = max(1, int(C.MODEL_TABLE_GIVE_UP_AFTER))
        for piece in pieces:
            st = self.tables[piece.table]
            if st.wrote(piece):
                st.written_parts.add(piece.part)
                st.dead_parts = 0
                continue
            st.dead_parts += 1
            if st.dead_parts >= give_up and not st.abandoned and len(st.parts) > 1:
                st.abandoned = True
                logger.warning("起草数据目录：表 %s 接连 %d 段都没写出来，剩下的段跳过", st.name, st.dead_parts)

    async def _skip_part(self, piece: _Piece) -> None:
        """这张表接连几段都没写出来：这一段跳过不问（算一块结束）。"""
        st = self.tables[piece.table]
        st.skipped_parts.append(piece.part)
        self._fail([piece], _Why(_SKIPPED))
        st.chunks_done += 1
        await self._hook(self.hooks.chunks_changed, st.name, st.chunks_total, st.chunks_done)

    # ---------------------------------------------------------------- 块进度

    async def _more_chunks(self, table: str, n: int) -> None:
        if n <= 0:
            return
        st = self.tables[table]
        st.chunks_total += n
        await self._hook(self.hooks.chunks_changed, table, st.chunks_total, st.chunks_done)

    async def _chunk_done(self, chunk: _Chunk) -> None:
        """一块问完了：这一块里没被拆给新块的表，各结束一块。"""
        for table in chunk.tables:
            if table in chunk.split:
                continue
            st = self.tables[table]
            st.chunks_done += 1
            await self._hook(self.hooks.chunks_changed, table, st.chunks_total, st.chunks_done)

    # ---------------------------------------------------------------- 一段

    async def _segment(self, pieces: list[_Piece], depth: int, followup: bool, budget: _Budget, *,
                       chunk: _Chunk, halved: bool = False, mode: str | None = None, last: bool = False,
                       optional: str | None = None) -> None:
        """问一段，拿到的并进各表；按失败原因拆开、补缺、记下没写出来的。

        depth：已经拆了几层（截断后只补剩下的也算一层）。followup：这一段是在补缺（补过就不再补，见 _after_followup）。
        budget：这一段（原始段）还能调用几次。chunk：这是哪一块（块进度）。halved：这几块是真拆小（_split）出来的——
        原因里「已拆小重试」只在这时写。mode / last：换纯文本问的那一次——拆不开的空壳、结构化输出回了说明文字（再不行
        就记没写出来）。optional：拆出来的块（值是拆它的原因），轮到它时已经止损就不问了（_ask）。

        止损期间（_halted）每块只问一次：不拆小（_split）、不补问漏掉的列、截断后不再补剩下的。"""
        reply = await self._ask(pieces, budget, mode, optional=optional)
        if reply.capped:
            self._fail(pieces, self._capped(halved))
            return
        if reply.skipped:
            note = self._halt_note()
            self._fail(pieces, lambda p: self._reason(optional or "hollow", split=True, piece=p, halted=note))
            return
        if reply.timeout:
            await self._report(reply, wrote=False)
            await self._split(pieces, depth, "timeout", followup, budget, halved, chunk)
            return
        if reply.error is not None:
            # 结构化输出回了说明文字、这一块马上改走纯文本再问：只管通信的重来，不进止损的窗口
            redo = reply.prose and not last
            await self._report(reply, wrote=False, halting=not redo)
            if redo:
                await self._prose_to_text(pieces, depth, followup, budget, halved, chunk)
                return
            self._fail(pieces, _Why(reply.error, split=halved))
            return
        try:
            gains = self._take(pieces, reply.entries)
        except Exception:
            # 读回复时出了意料之外的错（_guarded 记这一块没写出来）：这次调用照样记进用量，算一次出错
            await self._called(reply.usage or (0, 0), "error")
            raise
        anything = any(g.items for g in gains)
        complete = not (reply.exhausted or reply.truncated)
        rest = self._remainder(pieces, gains, truncated=False, followup=followup) if complete else []
        # 补问时请写的都列出来了、字段留空：是模型拿不准（记留空），这次调用不算没写出来（不进提醒的窗口）
        await self._report(reply, wrote=anything or (followup and complete and not rest))
        if last and anything and self.mode != "text":
            # 结构化输出只给空壳、纯文本写出来了：这个模型的结构化输出靠不住，这次起草后面都走纯文本
            self.mode = "text"
            logger.info("起草数据目录：结构化输出只给了空壳，纯文本写出来了，后面改走纯文本")
        if not complete:
            rest = self._remainder(pieces, gains, truncated=True, followup=followup)
            if not rest:
                return
            kind = "exhausted" if reply.exhausted else "truncated"
            if not anything:
                await self._split(rest, depth, kind, followup, budget, halved, chunk)
            elif depth < int(C.MODEL_SPLIT_DEPTH) and not self._halted():
                # 捡回了一部分：只补剩下的（不是拆小）
                await self._segment(rest, depth + 1, followup, budget, chunk=chunk, halved=halved)
            else:
                note = self._halt_note() if depth < int(C.MODEL_SPLIT_DEPTH) else ""
                self._fail(rest, self._reason(kind, split=halved, halted=note))
            return
        if not anything and not followup:
            if last:
                halted = self._stopped(pieces, depth)
                self._fail(pieces, lambda p: self._reason("hollow", split=halved, piece=p, halted=halted))
                return
            await self._split(pieces, depth, "hollow", followup, budget, halved, chunk)
            return
        if not rest:
            return
        if followup:
            await self._after_followup(rest, depth, budget, halved, chunk, last=last)
            return
        if self._halted():
            # 止损期间每块只问一次：漏了的不补问
            note = self._halt_note()
            self._fail(rest, lambda p: _Why(REASON_OMITTED, split=halved, halted=note)
                       if self.tables[p.table].has_any() else self._reason("hollow", split=halved, piece=p,
                                                                           halted=note))
            return
        await self._segment(rest, depth, True, budget, chunk=chunk, halved=halved)

    async def _after_followup(self, rest: list[_Piece], depth: int, budget: _Budget, halved: bool, chunk: _Chunk, *,
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
        await self._split(empty, depth, "hollow", False, budget, halved, chunk)

    async def _prose_to_text(self, pieces: list[_Piece], depth: int, followup: bool, budget: _Budget,
                             halved: bool, chunk: _Chunk) -> None:
        """结构化输出回了一段说明文字：网关接受了结构化输出的参数却没照做。这次起草后面都走纯文本（另带格式说明），
        这一段换纯文本问一次（止损期间也问：这是通信方式的问题，不是模型写不出来）。纯文本额度用完了还能照常拆小
        （拆出来的半段也走纯文本，不再先问一遍结构化输出）。"""
        if self.mode != "text":
            self.mode = "text"
            logger.info("起草数据目录：结构化输出回了说明文字，后面改走纯文本")
        await self._segment(pieces, depth, followup, budget, chunk=chunk, halved=halved, mode="text", last=True)

    def _stopped(self, pieces: list[_Piece], depth: int) -> str:
        """这几段本来还能拆小、因为止损才没拆时，止损的说明；不是的话是空串。"""
        if depth < int(C.MODEL_SPLIT_DEPTH) and _halves(pieces) is not None:
            return self._halt_note()
        return ""

    async def _split(self, pieces: list[_Piece], depth: int, kind: str, followup: bool, budget: _Budget,
                     halved: bool, chunk: _Chunk) -> None:
        """拆成两半再问（额度用完、超时、只给空壳、截断后一项都没捡回来）。

        - 同一张表按列拆、前一半带着表级的项：先写前一半，后一半拿它做背景。超时的不等：一次超时就是一个时限，40 → 20
          → 10 一层一层串着问要好几个时限，两半同时问。拆出来的两半是新的块（单张表多一块）；轮到它时已经止损的不再问
          （optional）。
        - 拆不开的空壳再问一次：结构化输出给的换纯文本，已经走纯文本的原样再问。
        - 止损期间（_halted）不拆小、每块只问一次：本来能拆的不拆；空壳不再原样再问，只有结构化输出给的、这次起草还没
          换纯文本问过的照旧问一次（看看是不是结构化输出的问题）。
        - 别的原因拆不开、止损了，就记没写出来。"""
        halves = _halves(pieces) if depth < int(C.MODEL_SPLIT_DEPTH) else None
        halted = self._halted()
        if halves is not None and not halted:
            first, second = halves
            await self._split_chunk(chunk, pieces)
            if (kind != "timeout" and len(first) == len(second) == 1 and first[0].table == second[0].table
                    and first[0].table_level):
                await self._half(first, depth + 1, followup, budget, kind)
                await self._half(second, depth + 1, followup, budget, kind)
                return
            results = await asyncio.gather(self._half(first, depth + 1, followup, budget, kind),
                                           self._half(second, depth + 1, followup, budget, kind),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return
        if kind == "hollow" and (not halted or (self.mode == "structured" and not self.text_probed)):
            self.text_probed = True
            await self._segment(pieces, depth, followup, budget, chunk=chunk, halved=halved, mode="text", last=True)
            return
        note = self._halt_note() if halted and (halves is not None or kind == "hollow") else ""
        self._fail(pieces, lambda p: self._reason(kind, split=halved, piece=p, halted=note))

    async def _half(self, pieces: list[_Piece], depth: int, followup: bool, budget: _Budget, kind: str) -> None:
        """拆出来的一半：新的一块，问完算结束一块。"""
        chunk = _Chunk(tuple(dict.fromkeys(p.table for p in pieces)))
        try:
            await self._segment(pieces, depth, followup, budget, chunk=chunk, halved=True, optional=kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._chunk_done(chunk)      # 出错也算问完（_guarded 记没写出来），块进度对得上
            raise
        await self._chunk_done(chunk)

    async def _split_chunk(self, chunk: _Chunk, pieces: list[_Piece]) -> None:
        """这一块里的这几张表拆给了新的块：单张表按列拆成两半，多一块；多表的段按表拆，各张表仍是一块。"""
        chunk.split.update(dict.fromkeys(p.table for p in pieces))
        if len(pieces) == 1:
            await self._more_chunks(pieces[0].table, 1)

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

    async def _ask(self, pieces: list[_Piece], budget: _Budget, mode: str | None = None, *,
                   optional: str | None = None) -> _Reply:
        """调一次模型（按原因重来：额度被拒、结构化输出不支持、网络错误……），交回读好的回复。

        这一段的调用次数用完了交回 capped（不调用）。optional（拆出来的块）：拿到名额时已经止损，交回 skipped（不调用，
        次数退回）——等名额的时候前面的调用多数没写出来，不再拆小重试。超时不原样重试：交回 timeout，由调用方当作这一段
        太大、拆小再问。重来之前的那次调用在这里就报给进度回调（记进用量和提醒，不进止损的窗口：halting=False）；交回的
        那次由调用方读完内容再报（写没写出来要看校验之后的内容）。服务方嫌额度太大、结构化输出不支持、网络错误的重来止损
        期间照旧（只管通信，不是模型写不出来）。"""
        mode = mode or self.mode
        retried = 0
        while True:
            if budget.left <= 0:
                return _Reply(capped=True)
            budget.left -= 1                 # 先占上：各块共用这一段的次数，等名额的时候别的块不能把它用掉
            used = getattr(self.model, "budget", None)
            try:
                # 先占这次起草自己的名额，再去排全进程的：一次起草排在全进程名额上的调用最多 MODEL_CONCURRENCY 个
                async with self.gate, model_gate():
                    if optional and self._halted():
                        budget.left += 1
                        return _Reply(skipped=True)
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
                    await self._called((0, 0), "error", halting=False)
                    continue
                if kind == "unsupported" and mode == "structured":
                    if self.mode == "structured":
                        logger.info("起草数据目录：结构化输出用不了（%s），后面改走纯文本", str(e)[:200])
                    self.mode = mode = "text"
                    await self._called((0, 0), "error", halting=False)
                    continue
                if kind == "transient" and retried < int(C.MODEL_RETRIES):
                    await self._called((0, 0), "error", halting=False)
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

    async def _report(self, reply: _Reply, *, wrote: bool, halting: bool = True) -> None:
        """交回的那次调用读完内容之后报给进度回调。halting：这次是不是这一块这次调用的最终结局（进不进止损的窗口，见
        _called）。"""
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
        await self._called(reply.usage, failure, strike=reply.timeout or reply.exhausted, halting=halting)

    async def _called(self, usage: tuple[int, int], failure: str | None, *, strike: bool = False,
                      halting: bool = True) -> None:
        """记下一次调用：额度用完或超时（strike）累计 MODEL_SHRINK_AFTER 次，新开始的段列数上限减半；最近几次调用多数
        没写出来时提醒，少了清掉；止损按最近 MODEL_HALT_WINDOW 次调用的比例（_halted）。报给进度回调。

        halting=False：只管通信的重来之前的那次（服务方嫌额度太大、结构化输出不支持或回了说明文字、429 / 5xx 退避重试）。
        照旧记进用量和提醒，不进止损的窗口：它不花额度，也说明不了模型写不出来；止损只看每次调用的最终结局。"""
        self.recent.append(failure)
        if halting:
            self.window.append(failure is not None)
        halted = self._halted()
        if halted != self.was_halted:
            self.was_halted = halted
            if halted:
                logger.warning("起草数据目录：最近 %d 次调用有 %d 次没写出来，之后的段不再拆小、每块只问一次",
                               len(self.window), sum(self.window))
            else:
                logger.info("起草数据目录：最近的调用多数写出来了，恢复拆小重试")
        if strike:
            self.strikes += 1
            width = max(1, int(C.MODEL_BATCH_COLUMNS))
            cap = max(self._floor(), width >> (self.strikes // max(1, int(C.MODEL_SHRINK_AFTER))))
            if cap < self.part_columns:
                self.part_columns = cap
                logger.info("起草数据目录：额度用完或超时已有 %d 次，之后新开始的段每段最多 %d 列", self.strikes, cap)
        failed = [f for f in self.recent if f is not None]
        if len(failed) >= int(C.MODEL_NOTICE_ON) or (self.notice is not None and len(failed) >= int(C.MODEL_NOTICE_OFF)):
            counts = collections.Counter(failed)
            kinds = "、".join(f"{label} {counts[kind]} 次" for kind, label in FAILURE_LABEL.items() if counts[kind])
            if self.notice is None:
                logger.warning("起草数据目录：最近 %d 次调用有 %d 次没写出来（%s）", len(self.recent), len(failed), kinds)
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
                drafted = C._from_model(entry, st.meta, system=self.system, source_kind=self.kind, table=st.name)
                notes = _restrict(drafted, piece)
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

    def _reason(self, kind: str, *, split: bool, piece: _Piece | None = None, halted: str = "") -> _Why:
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
        progress = {"parts_written": len(st.written_parts), "chunks_total": st.chunks_total,
                    "chunks_done": st.chunks_done}
        abandoned = self._abandoned_text(st)
        if not items:
            failures = [(why, part_of.get(key, 1)) for key, why in st.failed.items() if why.base != _SKIPPED]
            reason = _join(failures) if failures else (C.MODEL_EMPTY_REASON if st.named else REASON_MISSING)
            if abandoned:
                reason = f"{reason}；{abandoned}"
            if st.failed and len(st.parts) > 1:
                # 宽表一项都没写出来：同样逐段写上列的范围
                said = self._parts_said(st, failed_cols, failed_table)
                every = "都" if len(said) == len(st.parts) else ""
                reason = f"这张表 {len(st.columns)} 列，分 {len(st.parts)} 段起草，{'、'.join(said)}{every}没写出来：{reason}"
            return ModelOutcome("failed", {}, 0, reason, total, st.parts_done, **progress)
        notes = copy.deepcopy(st.notes)
        blank = self._blank(st, set(failed_cols))
        if failed_cols or failed_table:
            return ModelOutcome("partial", notes, items, self._partial_text(st, failed_cols, failed_table, abandoned),
                                total, st.parts_done, blank, **progress)
        return ModelOutcome("ok", notes, items, None, total, st.parts_done, blank, **progress)

    @staticmethod
    def _abandoned_text(st: _Table) -> str:
        """这张表接连几段没写出来、剩下的段跳过了：说清跳过了几段、怎么办。没跳过是空串。"""
        if not st.skipped_parts:
            return ""
        return REASON_ABANDONED.format(k=max(1, int(C.MODEL_TABLE_GIVE_UP_AFTER)), n=len(st.skipped_parts))

    def _blank(self, st: _Table, failed: set[str]) -> list[str]:
        """模型拿不准、留空了的列：请它写了（不在没写出来的里面），中文名和含义却都没给（或者给的都没通过校验）。

        目录里已经有中文名或含义的不算（不论来源：以前模型起草的——重新起草时模型这次没给的留着、不删；人工填写、确认的；
        库注释；驳回的不算有）：照「补充起草要求再重新起草」做一遍，模型这次没再给的老列不该又说成拿不准。库注释已经说清
        的（catalog.comment_role 记进目录的那种；导入表格的源是系统说明）同样不算：提示词里它们以此为准，模型不写也不缺。
        写入时（catalog.write_draft）再按并入目录之后的结果筛一遍。"""
        drafted = st.notes.get("columns") or {}
        known = C.visible_notes(st.existing).get("columns") or {}
        comments = {str(c.get("name")): (c.get("comment") or "").strip()
                    for c in st.meta.get("columns") or [] if isinstance(c, dict)}
        out: list[str] = []
        for col in st.columns:
            if col in failed:
                continue
            if any(f in (drafted.get(col) or {}) or f in (known.get(col) or {}) for f in ("label", "meaning")):
                continue
            text = comments.get(col)
            if text and (self.system or C.comment_role(text, col) is not None):
                continue
            out.append(col)
        return out

    @staticmethod
    def _parts_said(st: _Table, failed_cols: list[str], failed_table: bool) -> list[str]:
        """分段起草的表没写出来的是哪几段，每段写上列的范围（列名照表结构原样）：「第 1 段（表级的项）」「第 3 段（列名 到
        列名，共 32 列）」。"""
        part_of = st.part_of()
        bad: dict[int, list[str]] = {}
        for col in failed_cols:
            bad.setdefault(part_of.get(col, 1), []).append(col)
        said = ["第 1 段（表级的项）"] if failed_table else []
        return said + [f"第 {k} 段（{_columns_text(st.columns, cols, count=True)}）" for k, cols in sorted(bad.items())]

    @classmethod
    def _partial_text(cls, st: _Table, failed_cols: list[str], failed_table: bool, abandoned: str = "") -> str:
        """只写出一部分时说清哪部分没写出来（第几段、哪几列）、为什么、怎么办。剩下的段跳过了的，跳过的说明里已经说了
        怎么办（换一个起草模型后只重试这张表），不再另说「可以只重试这张表」。"""
        part_of = st.part_of()
        keys = ([_TABLE] if failed_table else []) + failed_cols
        reason = _join((st.failed[k], part_of.get(k, 1)) for k in keys if st.failed[k].base != _SKIPPED)
        if abandoned:
            reason = f"{reason}；{abandoned}" if reason else abandoned
        n = len(st.columns)
        if len(st.parts) > 1:
            said = cls._parts_said(st, failed_cols, failed_table)
            head = f"这张表 {n} 列，分 {len(st.parts)} 段起草，{'、'.join(said)}没写出来"
        elif failed_cols and failed_table:
            head = (f"这张表 {n} 列，有表级的项和 {len(failed_cols)} 列（{_columns_text(st.columns, failed_cols, count=False)}）"
                    "没写出来")
        elif failed_cols:
            head = f"这张表 {n} 列，有 {len(failed_cols)} 列（{_columns_text(st.columns, failed_cols, count=False)}）没写出来"
        else:
            head = f"这张表 {n} 列，表级的项没写出来"
        return f"{head}：{reason}" if abandoned else f"{head}：{reason}。可以只重试这张表"

    async def _part_done(self, name: str) -> None:
        self.tables[name].parts_done += 1
        await self._hook(self.hooks.part_done, name)

    async def _table_done(self, name: str) -> None:
        st = self.tables[name]
        if st.finished:
            return
        st.finished = True
        await self._hook(self.hooks.table_done, name, self._outcome(st))

    async def _part_written(self, name: str) -> None:
        """宽表写出了一段、后面还有段没结束：把这张表到这时拿到的全部内容交给调用方并入目录（停止、服务重启时不丢）。"""
        st = self.tables[name]
        await self._hook(self.hooks.part_written, name, PartWrite(copy.deepcopy(st.notes), len(st.written_parts)))

    # ---------------------------------------------------------------- 防错事实：核对有效行过滤

    async def _vet(self, st: _Table) -> None:
        """表级那段写完，核对模型提议的有效行过滤（只核对一次）。

        - 目录里人工确认、驳回过的有效行过滤：并入时不会采用这次的提议，不必再查库；
        - 没开数据剖析：照收，note 写「没有核对排除比例（数据源没有开数据剖析）」；
        - 开了：经剖析的只读守卫数排除比例（catalog_profile.check_valid_filter）。排除 0% 或 100% 的不收（日志记一条，
          不算失败）；收下的 note 在依据后面写上剖析核对的结果，或者没有核对的原因。"""
        if st.vetted:
            return
        st.vetted = True
        item = st.notes.get("valid_filter")
        if not isinstance(item, dict):
            return
        held = (st.existing or {}).get("valid_filter")
        if isinstance(held, Mapping) and held.get("status") in ("confirmed", "rejected"):
            return
        from app.data import catalog_profile

        if not self.profiling.enabled:
            found = catalog_profile.FilterCheck(True, catalog_profile.FILTER_UNCHECKED.format(
                why=catalog_profile.FILTER_NOT_ENABLED))
        else:
            try:
                found = await catalog_profile.check_valid_filter(self.source, st.name, dict(st.meta),
                                                                 str(item.get("value") or ""), settings=self.profiling)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 核对是附加的：出了意外照收，写明没有核对
                from app.core.errors import describe_exception, first_line

                logger.exception("起草数据目录：核对表 %s 的有效行过滤时出错", st.name)
                found = catalog_profile.FilterCheck(True, catalog_profile.FILTER_UNCHECKED.format(
                    why=f"核对时出错：{first_line(e) or describe_exception(e)}"))
        if not found.keep:
            logger.info("起草数据目录：表 %s 的有效行过滤 %s 没有收下：%s", st.name, item.get("value"), found.dropped)
            st.notes.pop("valid_filter", None)
            return
        item["note"] = _sentences(item.get("note"), found.note)

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
        shown = self._confirmed_for(st, piece)
        codes = self._codes_lines(st, piece, shown)
        if codes:
            lines.append(labels["codes"])
            lines += codes
        confirmed = C._notes_lines(shown, meta=st.meta, system_notes=False)
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
    def _confirmed_for(st: _Table, piece: _Piece) -> dict[str, Any]:
        """已确认、有确证的项（以此为准的背景）：整表全给；其余给表级的项和这一段的列，关系只在要写表级的项时给。"""
        confirmed = C._confirmed_only(st.existing)
        if not st.whole(piece):
            wanted = set(piece.columns)
            kept = {key: item for key, item in C._slots(confirmed).items()
                    if key[0] == "t" or (key[0] == "c" and key[1] in wanted) or (key[0] == "r" and piece.table_level)}
            confirmed = C._assemble(kept)
        return confirmed

    def _codes_lines(self, st: _Table, piece: _Piece, shown: Mapping[str, Any]) -> list[str]:
        """开了数据剖析的源：目录里记下的码值（剖析取过的取值和已知含义）交给模型，判断有效行过滤、列的含义用。要写表级
        的项的给全部列的，只写几列的给这几列的；遮罩的列、驳回的码值不给；「已确认的目录项」里已经有的不重复。"""
        if not self.profiling.enabled:
            return []
        labels = C.CATALOG_DRAFT_PROMPT_LABELS
        wanted = set(st.columns) if piece.table_level else set(piece.columns)
        known = C.visible_notes(st.existing).get("columns") or {}
        given = (shown.get("columns") or {}) if isinstance(shown.get("columns"), Mapping) else {}
        out: list[str] = []
        for col in st.columns:
            item = (known.get(col) or {}).get("codes")
            if col not in wanted or col.lower() in self.masked or not isinstance(item, Mapping):
                continue
            if "codes" in (given.get(col) or {}) or not isinstance(item.get("value"), Mapping) or not item["value"]:
                continue
            out.append(labels["code"].format(name=col, codes=C._codes_text(item["value"])))
        return out


def _sentences(*parts: Any) -> str:
    """几句话连成一段：各句去掉句末的句号，用句号隔开（「依据：……。剖析核对：……」）。"""
    texts = [str(p).strip().rstrip("。") for p in parts if isinstance(p, str) and p.strip()]
    return "。".join(texts)


__all__ = ["BUDGET_WORDS", "Background", "CallReport", "FAILURE_LABEL", "Hooks", "ModelOutcome", "NOTICE", "PartWrite",
           "REASON_ABANDONED", "REASON_CALL", "REASON_CAPPED", "REASON_EXHAUSTED", "REASON_FORMAT", "REASON_HALTED",
           "REASON_HOLLOW", "REASON_MISSING", "REASON_OMITTED", "REASON_RETRIED", "REASON_SPLIT", "REASON_SPLIT_PARTS",
           "REASON_TIMEOUT", "REASON_TRUNCATED", "REASON_UNANSWERED", "SEG", "classify_failure", "model_gate",
           "parts_for", "run_model_draft"]
