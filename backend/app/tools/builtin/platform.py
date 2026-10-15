"""平台内置能力的工具定义（docs/design/normal-user-apps.md N8、N9；执行在 capabilities/platform.py）。

这里只登记名字、说明和参数，让它们进能力登记（效果、治理、版本）和画布的工具清单。写入的两项（records_append、
table_import_period）效果记 write：只能放在调用工具节点里，节点按动作流程执行（先留生效记录、按幂等键写、需要时等人
确认），不会走到这里的函数——走到了说明绕过了动作流程，照实拒绝。records_lookup 是只读的查询能力。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.files import TABLE_IMPORT_PERIOD
from app.records import RECORDS_APPEND, RECORDS_LOOKUP
from app.tools.registry import ToolContext, register

#: 写入能力被直接调用（没经动作流程）时交回的话
_ONLY_VIA_ACTION = "「{name}」是写入能力，只能放在「调用工具」节点里，由动作流程写入（留生效记录、按动作键只写一次）"


class RecordsAppendArgs(BaseModel):
    table: str = Field(description="写哪张记录表：记录表的数据源名（如 rec_ab12cd），或 records_tables 的编号")
    values: dict[str, Any] | str = Field(description="这一行各字段的值，键是表单字段名；也可以是一段 JSON 文字")
    supersedes: str | None = Field(default=None, description="更正时填被更正那一行的编号（_id）；新增一行时不填")


@register(
    name=RECORDS_APPEND,
    category="记录表",
    description="往平台托管的记录表追加一行（填报登记）。一次动作只落一行，重放不会重复写；更正是追加一行并指向被更正的行，"
                "原行不改。只能放在「调用工具」节点里",
    args_schema=RecordsAppendArgs,
    effect="write",
)
async def records_append(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return {"error": _ONLY_VIA_ACTION.format(name=RECORDS_APPEND)}


class RecordsLookupArgs(BaseModel):
    idempotency_key: str = Field(description="写入时用的幂等键：动作键加上第几次发送")


@register(
    name=RECORDS_LOOKUP,
    category="记录表",
    description="按幂等键查一次记录表写入有没有落下（写入结果未知时，平台用它自动核实）",
    args_schema=RecordsLookupArgs,
)
async def records_lookup(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    from app.records.actions import lookup_action

    args = RecordsLookupArgs(**kwargs)
    result = await lookup_action({"idempotency_key": args.idempotency_key}, environment="prod")
    return {"status": result.status, **(result.body if isinstance(result.body, dict) else {})}


class TableImportPeriodArgs(BaseModel):
    source: str = Field(description="导入到哪个数据源（按配方导入的上传表格，写数据源名）")
    file: dict[str, Any] | str = Field(description="要导入的文件：上传后拿到的引用（来自表单的文件字段）")


@register(
    name=TABLE_IMPORT_PERIOD,
    category="数据",
    description="把一个上传的文件按数据源已有的配方导入成新的一期（同数据页的「上传新一期」）。会换掉数据源的当前数据，"
                "只能放在「调用工具」节点里，由人确认后执行",
    args_schema=TableImportPeriodArgs,
    effect="write",
)
async def table_import_period(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    return {"error": _ONLY_VIA_ACTION.format(name=TABLE_IMPORT_PERIOD)}
