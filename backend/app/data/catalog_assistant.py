"""表助手：在表详情页和使用者对话，一起完善一张表的数据目录（POST /catalog/{table}/assistant，SSE）。

**为什么要有它。** 模型起草只看表结构，缩写列名、状态码的意思常常拿不准而留空；审阅时人知道答案，却要一项一项填。表助手
把这张表的结构、目录现状（每项的来源和状态）、关联关系、剖析结果、起草要求、使用次数、上次起草留空的列交给模型，让它主动
问拿不准的地方；使用者说出一条业务事实，它整理成成批的修改提案（形状同助手流里的 catalog_patch），使用者在界面上逐项看过、
一次保存（PATCH /catalog/{table}/patch，记为人工确认）。

**只改目录、不改业务库。** 模型只有两个工具：
- propose_changes：一批修改提案。按 catalog.plan_patch 核对（取值格式、列名必须在表结构里、公式不收），遮罩列不给码值，已经
  是这个值又确认过的不提；有效记录条件是模型写的 SQL，另按起草的规矩核对（_Tools.filter_problem：一个条件、只用这张表的
  列、不能有子查询……）。核对过的作为 proposal 事件交给界面，**不写目录**。没收下的项和原因交回模型（不带任何取值）。
  界面上还没保存的修改（pending）里同一列的码值并进新提的码值：界面上同一路径以后一次为准，不能把先提的码值丢了。
- run_query：只读查询，只有数据源开了数据剖析才给。经剖析的只读守卫和单条时限（catalog_profile._Runner：
  guard.check(readonly=True)，单条时限取剖析设置和数据源查询时限中较小的一个），每次请求最多 MAX_QUERIES 条（守卫拒掉的
  也算），最多 QUERY_ROWS 行交给模型、SHOWN_ROWS 行交给界面。结果交给模型、界面之前先遮（engine/masking.mask_query_result：
  取自遮罩列的结果列整列换成「已遮罩」，改了名字的也认；这一次请求里前面遮掉的值按值换），报错原话里的值也换。没开剖析时
  提示词里写明只能根据表结构和目录推断。

**遮罩列只给列名。** 表结构里遮罩的列只写列名和类型（不给注释），目录里这些列的码值、带着取值的备注一律不给（剖析的备注里
有取值范围）；界面上还没保存的修改里这些列的码值也不给。

**模型。** 取模型的顺序同起草（data/draft_model.py：起草模型 → 助手的模型 → 默认接入），用不了时的说法也同起草
（先过 catalog.resolve_draft_model，接口回 409 catalog_draft_model_unavailable）。保留思考、温度 0，额度
catalog.MODEL_MAX_TOKENS，服务方嫌大时退回 catalog.MODEL_FALLBACK_MAX_TOKENS（providers/budget.BudgetFallback）；单次调用
最多等 catalog.MODEL_TIMEOUT_S（同起草）。网络错误、429、5xx 在还没输出任何东西时重试（最多 len(RETRY_DELAYS_S) 次）。
一次请求最多调用 MAX_ROUNDS 次模型（查询、提案之后接着答）；倒数第二轮的工具结果里提醒它直接回答。

**事件**（每个事件一行 data: <JSON>）：
- {"type": "text", "delta"}：回答正文的增量。几轮之间补一个空行；
- {"type": "query", "sql", "columns", "rows"（前 SHOWN_ROWS 行）, "row_count", "truncated", "error"}：跑了一条只读查询；
- {"type": "proposal", "source", "source_id", "table", "table_label", "version", "changes": [{path, label, before,
  before_status, after, value, reason, state, note?}]}：一批核对过的修改提案；
- {"type": "done", "usage": {input_tokens, output_tokens}, "queries"}；
- {"type": "error", "message", "code"}：中途出错，流到此为止（之后没有 done）。code：assistant_model_error 调用模型出错、
  assistant_truncated 输出到了额度上限被截断、assistant_timeout 单次调用超过时限、assistant_rounds 调用次数用完还没答完、
  assistant_failed 意料之外的错误。
等模型的时候每 HEARTBEAT_S 秒发一行 SSE 注释（「: keep-alive」，不是事件），免得中间的代理断开。

**对话不存服务端**：每次请求带上整段对话（最多 MAX_MESSAGES 条、每条最多 MESSAGE_MAX 字）。查询结果只在这一次请求里
看得到，提示词里让模型把要用的结论写进回答。界面上待保存的修改不设上限（宽表上逐列补中文名、含义，几轮下来常常超过一次
保存的 200 项，不能因此不让问）：背景里只列最近的 PENDING_SHOWN 项（界面上新的提案接在后面），写明另有几项；码值照全部
待保存的修改合并。

写给模型的文字都在名字带 _SYSTEM / _PROMPT 的常量里；这里的 ERROR_* 是给人看的。
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_jobs, catalog_profile, sqlcheck
from app.data.catalog_review import SOURCE_LABEL, item_label, slot_path, table_usage_counts
from app.data.catalog_transfer import filter_columns_problem
from app.data.draft_model import DRAFT_MODEL_PLACE
from app.data.engine import SnapshotTampered, masked_columns

logger = logging.getLogger(__name__)

#: 对话最多几条、每条最多几个字；界面上待保存的修改在背景里最多列几项（最近的；请求里交多少都收）
MAX_MESSAGES = 40
MESSAGE_MAX = 4000
PENDING_SHOWN = 200
#: 每次请求最多调用几次模型、最多几条只读查询
MAX_ROUNDS = 8
MAX_QUERIES = 5
#: 一条查询最多几行交给模型、几行交给界面；交给模型时每格最多几个字、一条结果最多几个字
QUERY_ROWS = 50
SHOWN_ROWS = 10
CELL_MAX = 300
TOOL_CHARS = 12_000
#: 网络错误、429、5xx 重试前等几秒（依次取用，个数就是最多重试几次）。在对话里等，比起草（10、30 秒）短
RETRY_DELAYS_S = (3.0, 10.0)
#: 等模型的时候每隔几秒发一行 SSE 注释
HEARTBEAT_S = 10.0
#: 背景里「目录里还没有中文名和含义的列」最多列几个；别的表指过来的关联关系最多列几条；这张表的目录最多列几项
#: （几百列的宽表全部起草过有上千项，全列出来提示词就太长了：表级的项、关联关系在前，列的项在后）
UNNAMED_SHOWN = 30
INCOMING_SHOWN = 30
CATALOG_ITEMS_SHOWN = 600

QUERY_TOOL = "run_query"
PROPOSE_TOOL = "propose_changes"

#: 错误事件的机读码
CODE_MODEL = "assistant_model_error"
CODE_TRUNCATED = "assistant_truncated"
CODE_TIMEOUT = "assistant_timeout"
CODE_ROUNDS = "assistant_rounds"
CODE_FAILED = "assistant_failed"

# ---- 给人看的
ERROR_MODEL = ("调用模型「{model}」失败：{reason}。请稍后重试；如持续失败，请在{place}中换一个模型，"
               "或到「设置 → 模型接入」测试该模型")
ERROR_TRUNCATED = "{cut}，回答没有写完。通常是思考占满了输出额度，可以把问题拆小一点再问，或在{place}中换一个模型"
ERROR_TIMEOUT = "模型 {seconds} 秒内没有回答完，已停止这一轮。可以把问题拆小一点再问，或在{place}中换一个模型"
ERROR_ROUNDS = "这一轮已经调用了 {n} 次模型仍没有答完，已停止。可以换个问法，或者分几次问"
ERROR_FAILED = "表助手出错：{line}"
#: 对话的最后一条是助手说的（接口 422）
LAST_NOT_USER = "对话的最后一条应当是使用者的话，请重新发送"
#: 起草模型刚才还能用、建流式模型时却用不了（期间设置被改了）时的说法
ERROR_NO_MODEL = "起草模型暂时无法使用，请稍后重试，或到「设置 → 模型接入」检查"
#: 查询事件里的报错
QUERY_TAMPERED = "数据源「{name}」的{error}。请联系管理员检查数据目录"

# ---- 写给模型的
ASSISTANT_SYSTEM = """你是 AgentLab 的「表助手」，和使用者一起完善一张表的数据目录：表和列的中文名、含义、单位、度量类型、码值、\
粒度、表类型、业务主键、业务日期、有效记录条件、去重规则、关联关系。

规矩：
- 你只改数据目录，不改业务库：不能写数据、建表、改结构。
- 只记数据事实，不写计算公式：「转化率=下单数/访问数」这类指标怎么算的说法属于口径卡，不进数据目录。
- 拿不准就问，不要猜。一次只问一两件最要紧的事，问得具体（例如「status 为 9 的行是不是作废的单子？」）。目录里留空的列、\
标着「推断」的项是最值得问的。
- 使用者说出一条业务事实时，用 propose_changes 把它整理成修改提案，reason 里引用使用者的原话（例如「使用者说：9 是作废」）。\
一次可以提多项。提案只是建议：使用者在界面上看过、保存之后才写进数据目录，记为人工确认。
- 表名、列名照原样写，只能用下面表结构里有的列。
- 标着「已确认」的项由人工确认过，使用者没有明确要改就不要提修改；标着「已驳回」的项使用者驳回过，不要再提。
- 「界面上还没保存的修改」是使用者已经接受、还没保存的提案：不要重复提；要改其中一项就再提一次，写完整的新值。
- 遮罩的列只给了列名：不要猜它的取值，也不要给它写码值。
- 回答用中文，简短直接。查询结果只在这一轮看得到，后面要用的结论请写进回答里。

修改提案的写法（propose_changes 的 changes，每项 path、value、reason）：
- path：表级项直接写字段名（label 中文名、description 说明、grain 粒度、keys 业务主键的列名列表、kind 表类型 \
fact/dimension/snapshot/log/config、business_date {"column","rule","timezone"}、valid_filter 有效记录的 SQL 条件、dedup 去重\
规则）；列级项写 columns.<列名>.<字段>（label、meaning 含义、unit 单位、measure 度量类型 \
flow/stock/ratio/identifier/status/attribute、codes 码值 {"码":"含义"}）；已有的关联关系写 relations.<编号>；新增关联关系\
写 relations.new，value 写 {"columns":[本表列],"to_table":"目标表","to_columns":[目标列],\
"cardinality":"many_to_one/one_to_one/one_to_many"}。
- codes 只写要补充或更正的码，原有的码会保留；valid_filter 要写成完整的条件（原有条件也要带上）。
- valid_filter 只能用这张表的列（不带表名），由比较、IN、BETWEEN、LIKE、IS NULL 用 AND / OR / NOT 连起来，例如 \
status NOT IN ('C', 'X')；不能有子查询、聚合、窗口函数、分号，函数只能用 UPPER、LOWER、TRIM、COALESCE。"""

#: 查询工具的说明（开了剖析时接在系统提示后面）、没开剖析时的那句
ASSISTANT_QUERY_PROMPT = """只读查询：run_query 在这个数据源上执行一条只读 SQL（{dialect} 的写法），最多返回 {rows} 行，\
这一轮最多 {limit} 条。用它核对码值的取值和分布、日期范围、关联对不对得上；不要取明细的个人信息。遮罩列的值显示为「已遮罩」。"""
ASSISTANT_NO_QUERY_PROMPT = "这个数据源没开剖析，不能查数据，只能根据表结构和目录推断。"
ASSISTANT_DISABLED_PROMPT = "这个数据源已停用，不能查数据，只能根据表结构和目录推断。"
#: 使用者还没说话（刚打开表助手）时补的那句
ASSISTANT_OPENING_PROMPT = ("（使用者刚打开表助手，还没有说话。）请用一两句话说明这张表的目录现在最缺什么，然后就最要紧的一两项"
                            "向使用者提问；先不要提修改提案。")
#: 对话从助手的开场白开始时，补在最前面的一句（各家的接口都要求从使用者这一边开始）
ASSISTANT_OPENED_PROMPT = "（使用者打开了表助手。）"

#: 背景各段的写法
ASSISTANT_PROMPT_LABELS = {
    "head": "下面是要完善的表。",
    "table": "数据源「{source}」（{kind}），表 {table}{view}",
    "view": "（视图）",
    "comment": "数据库注释：{text}",
    "system": "系统说明：{text}",
    "usage": "这张表被 {runs} 次运行用到（查询 {n} 次）。",
    "usage_none": "这张表还没有被运行用到过。",
    "description": "数据源说明：{text}",
    "guidance": "数据源的起草要求（使用者补充的背景和要求）：{text}",
    "structure": "表结构：",
    "column": "- {name} {type}{marks}",
    "pk": "主键",
    "not_null": "非空",
    "col_comment": "注释：{text}",
    "masked": "遮罩列：只给列名，不给任何取值",
    "unique": "唯一约束：{cols}",
    "fk": "外键：{cols} → {target}",
    "catalog": "这张表现在的数据目录（每项写明来源和状态；路径就是修改提案里的 path）：",
    "catalog_empty": "这张表还没有数据目录。",
    "item": "- {path}（{label}）：{value}〔{source}，{status}〕",
    "item_note": "  备注：{text}",
    "catalog_more": "（还有 {n} 项没有列出：这张表的目录太长。要改的项照上面的路径写法提，核对时按当前目录算）",
    "relation": "{columns} → {to_table}.{to_columns}{extra}",
    "relation_extra": "（{text}）",
    "cardinality": "基数 {value}",
    "coverage": "覆盖率 {value}",
    "incoming": "别的表指向这张表的关联关系：",
    "incoming_item": "- {table}.{columns} → {target}.{to_columns}〔{source}，{status}〕",
    "blank": "最近一次起草时模型拿不准、留空的列：{cols}（共 {n} 列）",
    "unnamed": "目录里还没有中文名和含义的列：{cols}（共 {n} 列{more}）",
    "unnamed_more": "，这里列出前 {n} 个",
    "pending": "界面上还没保存的修改（使用者已经接受、还没保存）：",
    "pending_more": "（另有 {n} 项更早的待保存修改没有列出：太多了。它们同样已经接受，不要重复提）",
    "pending_item": "- {path}：{value}（依据：{reason}）",
    "status_proposed": "推断",
    "status_verified": "有确证",
    "status_confirmed": "已确认",
    "status_rejected": "已驳回",
    "field_relation": "关联关系",
}

#: 工具结果的写法（交回模型）
ASSISTANT_TOOL_PROMPT = {
    "proposed": "已把 {n} 项修改交给使用者确认（使用者保存后才写进数据目录）：{paths}",
    "nothing": "没有可以交给使用者的修改。",
    "dropped": "没有收下的项：{reasons}",
    "masked_codes": "列 {column} 在数据源设置中被遮罩，不能给它写码值",
    "same": "{where}已经是这个值、也确认过了，不用再提",
    "no_changes": "提案里没有要修改的项",
    "query_failed": "查询失败：{error}",
    "query_capped": "未执行：这一轮最多查 {n} 条，不能再查了。请根据已经查到的结果回答",
    "query_disabled": "未执行：这个数据源没开数据剖析，不能查数据",
    "unknown_tool": "没有名为 {name} 的工具，可用的工具：{tools}",
    "last_round": "（这一轮只剩最后一步了：请直接回答使用者，不要再调用工具。）",
    "empty_sql": "未执行：没有给出 SQL",
}

QUERY_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": QUERY_TOOL,
        "description": "在这个数据源上执行一条只读 SQL 查询，返回列名和前几行（遮罩列的值显示为「已遮罩」）。",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "一条只读的 SELECT 语句"}},
            "required": ["sql"],
        },
    },
}
PROPOSE_TOOL_PROMPT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROPOSE_TOOL,
        "description": "把使用者说出的业务事实整理成一批数据目录修改提案，交给使用者确认后保存。不会直接写进数据目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "要改的项，写法见系统提示"},
                            "value": {"description": "新的值：文字、列名列表、码值对照或关联关系"},
                            "reason": {"type": "string", "description": "一句话依据，引用使用者的原话"},
                        },
                        "required": ["path", "value", "reason"],
                    },
                },
            },
            "required": ["changes"],
        },
    },
}


# ==========================================================================
# 模型
# ==========================================================================


async def resolve_model(session: AsyncSession) -> tuple[Any, str]:
    """(模型, 模型 id)。能不能用、用不了时的说法和起草完全一致：先过 catalog.resolve_draft_model（起草任务建任务时也是这样
    查的），用不了抛 catalog.CatalogModelUnavailable。再按同一个接入、模型建流式对话用的模型：保留思考、温度 0，大小两份额度
    （BudgetFallback），单次时限同起草。只建模型对象，不发请求。"""
    from app.data.draft_model import DraftModelUnavailable, draft_unavailable, resolve_draft_provider
    from app.providers.budget import BudgetFallback
    from app.providers.factory import ModelSpec, ProviderNotConfigured, build_chat_model

    await catalog.resolve_draft_model(session)
    try:
        provider, choice = await resolve_draft_provider(session, none_reason=ERROR_NO_MODEL, mock_reason=ERROR_NO_MODEL)
    except DraftModelUnavailable as e:
        raise catalog.CatalogModelUnavailable(str(e)) from e
    # SDK 自己不重试（同起草）：网络错误、429、5xx 由这里在还没输出时重试，两层叠起来实际次数就对不上了
    spec = ModelSpec(provider=choice.provider, model=choice.model, max_tokens=catalog.MODEL_MAX_TOKENS, temperature=0,
                     timeout=int(catalog.MODEL_TIMEOUT_S), max_retries=0)
    try:
        big = build_chat_model(provider, spec)
        small = build_chat_model(provider, spec.model_copy(update={"max_tokens": catalog.MODEL_FALLBACK_MAX_TOKENS}))
    except ProviderNotConfigured as e:
        raise catalog.CatalogModelUnavailable(draft_unavailable(e, choice.origin)) from e
    model_id = spec.model or provider.default_model or ""
    return BudgetFallback(big, small, limits=(catalog.MODEL_MAX_TOKENS, catalog.MODEL_FALLBACK_MAX_TOKENS)), model_id


# ==========================================================================
# 对话
# ==========================================================================


def conversation_problem(messages: Iterable[Mapping[str, Any]]) -> str | None:
    """对话本身的问题（接口 422）：使用者说过话、最后一条却是助手说的。没有使用者的话是「刚打开表助手」，不算问题。"""
    said = [m for m in messages if str(m.get("content") or "").strip()]
    if any(m.get("role") == "user" for m in said) and said[-1].get("role") != "user":
        return LAST_NOT_USER
    return None


def _history(messages: Iterable[Mapping[str, Any]]) -> list[Any]:
    """对话 → 模型的消息：空的不要，同一方接连说的并成一条；从助手的开场白开始的，前面补一句使用者打开了表助手；使用者还没
    说话的，补一句开场的请求。"""
    from langchain_core.messages import AIMessage, HumanMessage

    merged: list[tuple[str, str]] = []
    for m in messages:
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        role = "user" if m.get("role") == "user" else "assistant"
        if merged and merged[-1][0] == role:
            merged[-1] = (role, f"{merged[-1][1]}\n\n{text}")
        else:
            merged.append((role, text))
    out: list[Any] = []
    if merged and merged[0][0] == "assistant":
        out.append(HumanMessage(content=ASSISTANT_OPENED_PROMPT))
    for role, text in merged:
        out.append(HumanMessage(content=text) if role == "user" else AIMessage(content=text))
    if not any(role == "user" for role, _ in merged):
        out.append(HumanMessage(content=ASSISTANT_OPENING_PROMPT))
    return out


# ==========================================================================
# 背景
# ==========================================================================


def _value_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _status_text(status: Any) -> str:
    return ASSISTANT_PROMPT_LABELS.get(f"status_{status}", str(status))


def _source_text(source: Any) -> str:
    return SOURCE_LABEL.get(str(source), str(source))


def _relation_line(rel: Mapping[str, Any]) -> str:
    labels = ASSISTANT_PROMPT_LABELS
    extra = []
    if rel.get("cardinality"):
        extra.append(labels["cardinality"].format(value=rel["cardinality"]))
    coverage = rel.get("coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        extra.append(labels["coverage"].format(value=f"{coverage:.0%}"))
    return labels["relation"].format(
        columns=", ".join(str(c) for c in rel.get("columns") or []), to_table=rel.get("to_table"),
        to_columns=", ".join(str(c) for c in rel.get("to_columns") or []),
        extra=labels["relation_extra"].format(text="，".join(extra)) if extra else "")


def _touches_masked(key: catalog.Slot, item: Mapping[str, Any], masked: set[str]) -> bool:
    """这一项说的是不是遮罩列：列级的项看列名，业务主键、业务日期看指着的列，关联关系看本表这一端。"""
    if key[0] == "c":
        return key[1].lower() in masked
    if key[0] == "r":
        return any(str(c).lower() in masked for c in item.get("columns") or [])
    value = item.get("value")
    if key == ("t", "business_date") and isinstance(value, Mapping):
        return str(value.get("column") or "").lower() in masked
    if key == ("t", "keys") and isinstance(value, list):
        return any(str(c).lower() in masked for c in value)
    return False


def _catalog_lines(notes: Mapping[str, Any] | None, masked: set[str]) -> list[str]:
    """这张表的目录，每项一行：路径、字段、值、来源和状态，有备注的另起一行。遮罩列上的码值不给，说到遮罩列的项不给备注
    （剖析的备注里有取值范围）。"""
    labels = ASSISTANT_PROMPT_LABELS
    lines: list[str] = []
    # 表级的项、关联关系在前，列的项在后（稳定排序，各自照目录里的顺序）；遮罩列上的码值不算
    slots = [(key, item) for key, item in sorted(catalog._slots(notes).items(), key=lambda kv: kv[0][0] == "c")
             if not (key[0] == "c" and key[2] == "codes" and key[1].lower() in masked)]
    for shown, (key, item) in enumerate(slots):
        if shown >= CATALOG_ITEMS_SHOWN:
            lines.append(labels["catalog_more"].format(n=len(slots) - shown))
            break
        hidden = _touches_masked(key, item, masked)
        if key[0] == "r":
            label, value = labels["field_relation"], _relation_line(item)
        else:
            label = (catalog.TABLE_FIELD_LABEL if key[0] == "t" else catalog.COLUMN_FIELD_LABEL).get(key[-1], key[-1])
            value = _value_text(item.get("value"))
        lines.append(labels["item"].format(path=slot_path(key), label=label, value=value,
                                           source=_source_text(item.get("source")),
                                           status=_status_text(item.get("status"))))
        note = item.get("note")
        if isinstance(note, str) and note.strip() and not hidden:
            lines.append(labels["item_note"].format(text=note.strip()))
    return lines


def _structure_lines(meta: Mapping[str, Any], masked: set[str], *, system: bool) -> list[str]:
    labels = ASSISTANT_PROMPT_LABELS
    pk = {str(c) for c in meta.get("primary_key") or []}
    lines = [labels["structure"]]
    for col in meta.get("columns") or []:
        if not isinstance(col, Mapping) or col.get("name") is None:
            continue
        name = str(col["name"])
        marks: list[str] = []
        if name.lower() in masked:
            marks.append(labels["masked"])
        else:
            if name in pk:
                marks.append(labels["pk"])
            if not col.get("nullable", True):
                marks.append(labels["not_null"])
            text = str(col.get("comment") or "").strip()
            if text:
                marks.append((labels["system"] if system else labels["col_comment"]).format(text=text))
        lines.append(labels["column"].format(name=name, type=col.get("type") or "",
                                             marks=("，" + "，".join(marks)) if marks else ""))
    for group in meta.get("unique") or []:
        if isinstance(group, list) and group:
            lines.append(labels["unique"].format(cols="、".join(str(c) for c in group)))
    for fk in meta.get("foreign_keys") or []:
        if isinstance(fk, Mapping) and fk.get("columns") and fk.get("to_table"):
            target = ", ".join(f"{fk['to_table']}.{c}" for c in fk.get("to_columns") or []) or str(fk["to_table"])
            lines.append(labels["fk"].format(cols=", ".join(str(c) for c in fk["columns"]), target=target))
    return lines


async def _last_blank(session: AsyncSession, source_id: str, table: str) -> tuple[int, list[str]]:
    """最近一次结束于 ok / partial 的起草里这张表模型留空的列：(几列, 最多 5 个列名)。停止、中断的那次不算（那次没起草完，
    留空数是 0）。没有是 (0, [])。"""
    from app.db.models import CatalogDraftJob, CatalogDraftTask

    row = (await session.execute(
        select(CatalogDraftTask.blank_columns, CatalogDraftTask.blank_sample)
        .join(CatalogDraftJob, CatalogDraftJob.id == CatalogDraftTask.job_id)
        .where(CatalogDraftJob.source_id == source_id, CatalogDraftTask.table_name == table,
               CatalogDraftTask.status.in_(("ok", "partial")))
        .order_by(CatalogDraftJob.created_at.desc(), CatalogDraftTask.seq).limit(1)
    )).first()
    if row is None:
        return 0, []
    return int(row[0] or 0), [str(c) for c in row[1] or []]


@dataclass
class AssistantContext:
    """一次请求要用到的东西，开流之前读好（流里不再用请求的会话）。"""

    source_id: str
    source_name: str
    kind: str
    table: str
    tables: dict[str, Any]
    #: 读表结构、发查询用的源（上传源是绑定当前快照的视图）
    query_source: Any
    options: dict[str, Any]
    masked: set[str]
    can_query: bool
    settings: catalog_profile.ProfileSettings
    system: str
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def tools(self) -> list[dict[str, Any]]:
        return [QUERY_TOOL_PROMPT, PROPOSE_TOOL_PROMPT] if self.can_query else [PROPOSE_TOOL_PROMPT]


async def prepare(session: AsyncSession, row: Any, source: Any, table: str,
                  pending: Iterable[Mapping[str, Any]] = ()) -> AssistantContext:
    """读好这次请求要用的背景：表结构、目录、别的表指过来的关联关系、使用次数、起草要求、留空的列、待保存的修改，拼成系统
    提示。row 是数据源记录（遮罩、剖析开关、起草要求按它），source 是读表结构和发查询用的源。table 是表结构里的键。"""
    labels = ASSISTANT_PROMPT_LABELS
    cache = getattr(source, "schema_cache", None) or {}
    tables = cache.get("tables") or {}
    meta = tables.get(table) or {}
    options = dict(getattr(row, "options", None) or {})
    masked = {c.lower() for c in masked_columns(options)}
    settings = catalog_profile.profile_settings(options)
    enabled = getattr(row, "enabled", True) is not False
    can_query = settings.enabled and enabled
    system_notes = catalog.system_notes_source(source)
    entries = await catalog.read_catalog(session, source.id)
    entry = entries.get(table)
    notes = entry.notes if entry else {}
    usage = await table_usage_counts(session, source)
    used, runs = usage.queries.get(table, 0), usage.runs.get(table, 0)
    blank_n, blank_cols = await _last_blank(session, source.id, table)

    lines = [labels["head"], labels["table"].format(source=row.name, kind=row.kind, table=table,
                                                    view=labels["view"] if meta.get("is_view") else "")]
    comment = str(meta.get("comment") or "").strip()
    if comment:
        lines.append((labels["system"] if system_notes else labels["comment"]).format(text=comment))
    lines.append(labels["usage"].format(runs=runs, n=used) if used else labels["usage_none"])
    description = str(getattr(row, "description", "") or "").strip()
    if description:
        lines.append(labels["description"].format(text=description))
    if guidance := catalog_jobs.guidance_of(row):
        lines.append(labels["guidance"].format(text=guidance))
    lines += ["", *_structure_lines(meta, masked, system=system_notes), ""]
    current = _catalog_lines(notes, masked)
    lines += [labels["catalog"], *current] if current else [labels["catalog_empty"]]
    incoming = []
    for other, other_entry in sorted(entries.items()):
        if other == table:
            continue
        for key, rel in catalog._slots(other_entry.notes).items():
            if key[0] == "r" and rel.get("to_table") == table:
                incoming.append(labels["incoming_item"].format(
                    table=other, columns=", ".join(str(c) for c in rel.get("columns") or []), target=table,
                    to_columns=", ".join(str(c) for c in rel.get("to_columns") or []),
                    source=_source_text(rel.get("source")), status=_status_text(rel.get("status"))))
    if incoming:
        lines += ["", labels["incoming"], *incoming[:INCOMING_SHOWN]]
    named = catalog.visible_notes(notes).get("columns") or {}
    columns = [str(c["name"]) for c in meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name") is not None]
    unnamed = [c for c in columns if not any(f in (named.get(c) or {}) for f in ("label", "meaning"))]
    blank = [c for c in blank_cols if c in unnamed]
    if blank_n and blank:
        lines += ["", labels["blank"].format(cols="、".join(blank), n=blank_n)]
    if unnamed:
        more = labels["unnamed_more"].format(n=UNNAMED_SHOWN) if len(unnamed) > UNNAMED_SHOWN else ""
        lines.append(labels["unnamed"].format(cols="、".join(unnamed[:UNNAMED_SHOWN]), n=len(unnamed), more=more))
    pending_codes: dict[str, dict[str, Any]] = {}
    shown_pending: list[str] = []
    for change in pending:
        path = str(change.get("path") or "").strip()
        value = change.get("value")
        try:
            key = catalog.parse_path(path)
        except catalog.CatalogPathError:
            continue
        if key[0] == "c" and key[2] == "codes":
            if key[1].lower() in masked:
                continue
            if isinstance(value, Mapping):
                pending_codes[path] = dict(value)
        reason = str(change.get("reason") or "").strip()
        shown_pending.append(labels["pending_item"].format(path=path, value=_value_text(value), reason=reason or "—"))
    if shown_pending:
        lines += ["", labels["pending"]]
        if len(shown_pending) > PENDING_SHOWN:
            lines.append(labels["pending_more"].format(n=len(shown_pending) - PENDING_SHOWN))
        lines += shown_pending[-PENDING_SHOWN:]
    if can_query:
        tools = ASSISTANT_QUERY_PROMPT.format(dialect=row.kind, rows=QUERY_ROWS, limit=MAX_QUERIES)
    else:
        tools = ASSISTANT_NO_QUERY_PROMPT if enabled or not settings.enabled else ASSISTANT_DISABLED_PROMPT
    system = "\n\n".join([ASSISTANT_SYSTEM, tools, "\n".join(lines)])
    return AssistantContext(source_id=source.id, source_name=row.name, kind=str(row.kind or ""), table=table,
                            tables=tables, query_source=source, options=options, masked=masked, can_query=can_query,
                            settings=settings, system=system, pending_codes=pending_codes)


# ==========================================================================
# 工具
# ==========================================================================


def _cell(value: Any, limit: int | None = None) -> Any:
    """一格交出去的样子：数、布尔、空照原样，别的写成文字（日期、Decimal……），长的截断。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = str(value)
    elif isinstance(value, (bytes, bytearray)):
        text = f"<{len(value)} 字节>"
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


def _rows(rows: Any, n: int, limit: int | None) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in (rows or [])[:n]:
        cells = row if isinstance(row, list) else list(row.values()) if isinstance(row, dict) else [row]
        out.append([_cell(v, limit) for v in cells])
    return out


class _Tools:
    """一次请求里的工具：查询次数、这一次遮掉的值（后面的查询按值换）。"""

    def __init__(self, ctx: AssistantContext) -> None:
        from app.engine.masking import SeenValues

        self.ctx = ctx
        self.queries = 0
        self.seen = SeenValues()

    # ---------------------------------------------------------------- 查询

    async def query(self, sql: Any) -> tuple[dict[str, Any] | None, str]:
        """(给界面的 query 事件, 交回模型的结果)。"""
        prompts = ASSISTANT_TOOL_PROMPT
        if not self.ctx.can_query:
            return None, prompts["query_disabled"]
        if self.queries >= MAX_QUERIES:
            return None, prompts["query_capped"].format(n=MAX_QUERIES)
        sql = sql.strip() if isinstance(sql, str) else ""
        if not sql:
            return None, prompts["empty_sql"]
        self.queries += 1
        mask = masked_columns(self.ctx.options)
        known = await asyncio.to_thread(self.seen.pooled)
        known = known if known.strong else None
        error: str | None = None
        result: Any = None
        settings = dataclasses.replace(self.ctx.settings, max_queries=1)
        try:
            runner = catalog_profile._Runner(self.ctx.query_source, settings,
                                            catalog_profile.SqlDialect(self.ctx.kind))
            result = await runner(sql, max_rows=QUERY_ROWS)
        except catalog_profile._Skip as e:
            error = e.detail
        except catalog_profile._Stop as e:
            error = str(e)
        except SnapshotTampered as e:
            error = QUERY_TAMPERED.format(name=self.ctx.source_name, error=e)
        except ValueError as e:                  # 不支持的数据库类型
            error = str(e)
        if error is not None:
            if mask or known is not None:
                from app.engine.masking import mask_error_text

                error = mask_error_text(error, sql, known)
            event = {"type": "query", "sql": sql, "columns": [], "rows": [], "row_count": 0, "truncated": False,
                     "error": error}
            return event, prompts["query_failed"].format(error=error)
        payload = result.to_payload()
        if mask or known is not None:
            from app.engine.masking import mask_query_result

            schema = getattr(self.ctx.query_source, "schema_cache", None)
            done = await asyncio.to_thread(mask_query_result, payload, mask, self.ctx.kind, known, schema=schema)
            payload = done.payload
            await asyncio.to_thread(self.seen.add, done.secrets)
        columns = [str(c) for c in payload.get("columns") or []]
        rows = payload.get("rows") or []
        event = {"type": "query", "sql": str(payload.get("sql") or sql), "columns": columns,
                 "rows": _rows(rows, SHOWN_ROWS, None), "row_count": int(payload.get("row_count") or len(rows)),
                 "truncated": bool(payload.get("truncated")), "error": None}
        given = {"columns": columns, "rows": _rows(rows, QUERY_ROWS, CELL_MAX), "row_count": event["row_count"],
                 "truncated": event["truncated"]}
        text = json.dumps(given, ensure_ascii=False, default=str)
        while len(text) > TOOL_CHARS and given["rows"]:
            given["rows"] = given["rows"][:max(1, len(given["rows"]) // 2)] if len(given["rows"]) > 1 else []
            given["rows_shown"] = len(given["rows"])
            text = json.dumps(given, ensure_ascii=False, default=str)
        return event, text

    # ---------------------------------------------------------------- 提案

    def _with_pending(self, change: Any) -> Any:
        """界面上还没保存的修改里同一列的码值，并进新提的码值（新提的同一个码为准）。"""
        if not isinstance(change, Mapping):
            return change
        path = str(change.get("path") or "").strip()
        before = self.ctx.pending_codes.get(path)
        value = change.get("value")
        if before is None or not isinstance(value, Mapping):
            return change
        return {**change, "value": {**before, **value}}

    def filter_problem(self, condition: Any) -> str | None:
        """模型提的有效记录条件哪里不合规（交回模型的一句话），合规是 None。

        按模型起草时的规矩核对（sqlcheck.valid_filter_problem：一个条件、只用这张表的列、不能有子查询、聚合、窗口、分号，
        只能用比较、IN、BETWEEN、LIKE、IS NULL 和几个函数）；sqlcheck 里还没有这条规矩时，至少核对用到的列都是这张表的、
        都在表结构里、没有子查询（catalog_transfer.filter_columns_problem，导入时核对人工写的条件也用它）。"""
        meta = self.ctx.tables.get(self.ctx.table) or {}
        strict = getattr(sqlcheck, "valid_filter_problem", None)
        if strict is not None:
            return strict(condition, meta, kind=self.ctx.kind, table=self.ctx.table)
        return filter_columns_problem(condition, meta, kind=self.ctx.kind, table=self.ctx.table)

    async def propose(self, changes: Any) -> tuple[dict[str, Any] | None, str]:
        """(给界面的 proposal 事件, 交回模型的结果)。提案不写目录。"""
        from app.db.base import SessionLocal

        prompts = ASSISTANT_TOOL_PROMPT
        if not isinstance(changes, list) or not changes:
            return None, prompts["no_changes"]
        changes = [self._with_pending(c) for c in changes]
        async with SessionLocal() as session:
            entry = await catalog.read_entry(session, self.ctx.source_id, self.ctx.table)
        notes = entry.notes if entry else {}
        plan = catalog.plan_patch(notes, changes, table=self.ctx.table, tables=self.ctx.tables)
        dropped = list(plan.problems)
        kept: list[catalog.PatchChange] = []
        for change in plan.changes:
            key = catalog.parse_path(change.path)
            if key[0] == "c" and key[2] == "codes" and key[1].lower() in self.ctx.masked:
                dropped.append(prompts["masked_codes"].format(column=key[1]))
                continue
            if change.state == "same":
                dropped.append(prompts["same"].format(where=_where(key, change)))
                continue
            if key == ("t", "valid_filter") and (problem := self.filter_problem(change.after)) is not None:
                dropped.append(problem)
                continue
            kept.append(change)
        said: list[str] = []
        event: dict[str, Any] | None = None
        if kept:
            label = notes.get("label") if isinstance(notes.get("label"), Mapping) else {}
            event = {
                "type": "proposal", "source": self.ctx.source_name, "source_id": self.ctx.source_id,
                "table": self.ctx.table,
                "table_label": label.get("value") if label and label.get("status") != "rejected" else None,
                "version": entry.version if entry else 0,
                "changes": [{**c.as_dict(), "label": _where(catalog.parse_path(c.path), c)} for c in kept],
            }
            said.append(prompts["proposed"].format(n=len(kept), paths="、".join(c.path for c in kept)))
        else:
            said.append(prompts["nothing"])
        if dropped:
            said.append(prompts["dropped"].format(reasons="；".join(dropped)))
        return event, "\n".join(said)


def _where(key: catalog.Slot, change: catalog.PatchChange) -> str:
    """提案里一项的中文说法：「列 status 的码值」「表的有效记录条件」「指向 members 的关联关系」（同审阅队列的 field_label）。"""
    value = change.after if isinstance(change.after, Mapping) else change.before
    return item_label(key, value if isinstance(value, Mapping) else None)


# ==========================================================================
# 流
# ==========================================================================


#: 上一轮的正文以这些结尾（说完了一句话）时，下一轮另起一段
_SENTENCE_END = ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":")


def _sse(obj: Mapping[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


class _CallFailed(Exception):
    """一次模型调用没成：code 是错误事件的机读码，message 给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _seconds(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _transient(e: BaseException) -> bool:
    from app.core.errors import NETWORK, RATE_LIMIT, SERVER, classify

    return classify(e) in (RATE_LIMIT, SERVER, NETWORK)


async def _pump(runner: Any, history: list[Any], queue: asyncio.Queue[tuple[str, Any]]) -> None:
    """在单独的任务里读模型的流，一块一块放进队列；读完放 end，出错放 error。"""
    try:
        async for chunk in runner.astream(history):
            await queue.put(("chunk", chunk))
        await queue.put(("end", None))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - 交给读队列的一方按原因处理
        await queue.put(("error", e))


async def _round(runner: Any, history: list[Any], model_id: str) -> AsyncIterator[tuple[str, Any]]:
    """调一次模型：交出 ("text", 增量)、("beat", None)（等得久了，发心跳）、最后 ("message", (合起来的消息, 截断了没有))。
    网络错误、429、5xx 在还没输出任何正文时重试；超时、别的错误抛 _CallFailed。"""
    from app.core.errors import TIMEOUT, classify, describe_exception
    from app.engine.state import message_text
    from app.providers.budget import cut_off

    loop = asyncio.get_running_loop()
    attempt = 0
    while True:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        task = asyncio.ensure_future(_pump(runner, history, queue))
        deadline = loop.time() + float(catalog.MODEL_TIMEOUT_S)
        message: Any = None
        cut = False
        started = False
        failure: BaseException | None = None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise _CallFailed(CODE_TIMEOUT, ERROR_TIMEOUT.format(seconds=_seconds(catalog.MODEL_TIMEOUT_S),
                                                                         place=DRAFT_MODEL_PLACE))
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=min(remaining, HEARTBEAT_S))
                except asyncio.TimeoutError:
                    yield "beat", None
                    continue
                if kind == "end":
                    break
                if kind == "error":
                    failure = value
                    break
                message = value if message is None else message + value
                cut = cut or bool(cut_off(value))
                text = message_text(value)
                if text:
                    started = True
                    yield "text", text
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if failure is None:
            yield "message", (message, cut)
            return
        if not started and attempt < len(RETRY_DELAYS_S) and _transient(failure):
            logger.info("表助手：调用模型出错（%s），%s 秒后重试", type(failure).__name__, RETRY_DELAYS_S[attempt])
            await asyncio.sleep(RETRY_DELAYS_S[attempt])
            attempt += 1
            continue
        if isinstance(failure, TimeoutError) or classify(failure) == TIMEOUT:
            raise _CallFailed(CODE_TIMEOUT, ERROR_TIMEOUT.format(seconds=_seconds(catalog.MODEL_TIMEOUT_S),
                                                                 place=DRAFT_MODEL_PLACE)) from failure
        raise _CallFailed(CODE_MODEL, ERROR_MODEL.format(model=model_id or "", reason=describe_exception(failure),
                                                         place=DRAFT_MODEL_PLACE)) from failure


async def stream(model: Any, model_id: str, ctx: AssistantContext,
                 messages: Iterable[Mapping[str, Any]]) -> AsyncIterator[str]:
    """表助手的一次请求：交出 SSE 的每一行（事件、心跳注释）。"""
    from langchain_core.messages import SystemMessage, ToolMessage, message_chunk_to_message

    from app.providers.budget import cut_words

    history: list[Any] = [SystemMessage(content=ctx.system), *_history(messages)]
    tools = _Tools(ctx)
    usage = {"input_tokens": 0, "output_tokens": 0}
    wrote = ""
    names = [t["function"]["name"] for t in ctx.tools()]
    try:
        # 绑工具也放在这里面：响应头已经发出去了，出了错要照实交一条错误事件，不能让流半路断掉
        runner = model.bind_tools(ctx.tools())
        for round_no in range(MAX_ROUNDS):
            fresh = True
            message: Any = None
            cut = False
            # 流在半路被关掉（使用者点了停止、断开）时，正在读的那次调用跟着停：aclosing 让 _round 的收尾马上执行
            async with contextlib.aclosing(_round(runner, history, model_id)) as rounds:
                async for kind, value in rounds:
                    if kind == "beat":
                        yield ": keep-alive\n\n"
                    elif kind == "text":
                        if fresh and wrote.endswith(_SENTENCE_END):
                            # 上一轮（查询、提案之前）说完了一句话：这一轮另起一段
                            yield _sse({"type": "text", "delta": "\n\n"})
                        fresh = False
                        wrote = value
                        yield _sse({"type": "text", "delta": value})
                    else:
                        message, cut = value
            meta = getattr(message, "usage_metadata", None) or {}
            usage["input_tokens"] += int(meta.get("input_tokens") or 0)
            usage["output_tokens"] += int(meta.get("output_tokens") or 0)
            if cut:
                limit = getattr(model, "max_tokens", None)
                yield _sse({"type": "error", "code": CODE_TRUNCATED,
                            "message": ERROR_TRUNCATED.format(cut=cut_words("表助手", limit), place=DRAFT_MODEL_PLACE)})
                return
            calls = list(getattr(message, "tool_calls", None) or []) if message is not None else []
            if not calls:
                yield _sse({"type": "done", "usage": usage, "queries": tools.queries})
                return
            if round_no == MAX_ROUNDS - 1:
                yield _sse({"type": "error", "code": CODE_ROUNDS, "message": ERROR_ROUNDS.format(n=MAX_ROUNDS)})
                return
            # 合起来的流式块换成普通消息再交回模型（不带半截的工具调用块）
            history.append(message_chunk_to_message(message))
            for call in calls:
                name = str(call.get("name") or "")
                args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
                call_id = str(call.get("id") or f"{name}-{round_no}-{len(history)}")
                event: dict[str, Any] | None = None
                if name == QUERY_TOOL:
                    event, content = await tools.query(args.get("sql"))
                elif name == PROPOSE_TOOL:
                    event, content = await tools.propose(args.get("changes"))
                else:
                    content = ASSISTANT_TOOL_PROMPT["unknown_tool"].format(name=name, tools="、".join(names))
                if event is not None:
                    yield _sse(event)
                if round_no == MAX_ROUNDS - 2:
                    content = f"{content}\n{ASSISTANT_TOOL_PROMPT['last_round']}"
                history.append(ToolMessage(content=content, tool_call_id=call_id))
    except _CallFailed as e:
        yield _sse({"type": "error", "code": e.code, "message": e.message})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - 流已经开了，照实交一条错误事件
        from app.core.errors import describe_exception, first_line

        logger.exception("表助手出错：数据源 %s 表 %s", ctx.source_id, ctx.table)
        yield _sse({"type": "error", "code": CODE_FAILED,
                    "message": ERROR_FAILED.format(line=first_line(e) or describe_exception(e))})


__all__ = ["AssistantContext", "CODE_FAILED", "CODE_MODEL", "CODE_ROUNDS", "CODE_TIMEOUT", "CODE_TRUNCATED",
           "HEARTBEAT_S", "MAX_MESSAGES", "MAX_QUERIES", "MAX_ROUNDS", "MESSAGE_MAX", "PENDING_SHOWN", "PROPOSE_TOOL",
           "QUERY_ROWS", "QUERY_TOOL", "SHOWN_ROWS", "conversation_problem", "prepare", "resolve_model", "stream"]
