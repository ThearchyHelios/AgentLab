"""平台内置能力（docs/design/normal-user-apps.md N8、N9）：平台自己执行的写入，接进动作的同一条路径。

写外部系统的能力由平台执行器按登记的端点发请求（capabilities/endpoints.py）；这里的几项不发请求，由平台自己执行，
但生效记录、幂等键、发送确认、结果未知转核实全部沿用 process/actions.py——调用工具节点认得出它们是写入（能力登记里
效果为 write），走的就是 prepare → （确认）→ send，send 调 registry.endpoint_request，那里按名字转到 execute。

| 能力 | 效果 | 治理默认 | 做什么 |
| records_append | 写入 | 允许无人值守、查询能力 records_lookup | 往记录表追加一行（records/actions.py），幂等键就是动作键 |
| records_lookup | 只读 | | 按幂等键查那一行写没写下（结果未知时 actions._lookup 调） |
| table_import_period | 写入 | 要人确认 | 按上传源已有的配方把一个文件导入成一期（files/imports.py） |

环境：正式运行写真的；探索运行（试运行、个人应用）在 actions.prepare 里就定成演练——这几项没有测试环境，一行都不写。
治理默认只在登记里第一次出现这一行时写进去，之后管理员改了算管理员的。
"""
from __future__ import annotations

from typing import Any

from app.files import TABLE_IMPORT_PERIOD
from app.records import RECORDS_APPEND, RECORDS_LOOKUP

#: 平台执行的能力
PLATFORM_CAPABILITIES = (RECORDS_APPEND, RECORDS_LOOKUP, TABLE_IMPORT_PERIOD)
#: 登记里新出现这一行时写进去的治理字段
GOVERNANCE_DEFAULTS: dict[str, dict[str, Any]] = {
    # 平台自己的表、一次提交只落一行、重放不会重复写：允许无人值守（填报登记不该每条都等人确认）
    RECORDS_APPEND: {"unattended_write": True, "lookup": RECORDS_LOOKUP},
    # 换掉一个数据源的数据：要人确认
    TABLE_IMPORT_PERIOD: {"unattended_write": False},
}
#: 写入能力 → 它的查询能力（平台按幂等键替它查）。治理里只许选这一对，同端点上的只读能力那条规矩管不到平台能力
LOOKUPS = {RECORDS_APPEND: RECORDS_LOOKUP}


def is_platform(name: str | None) -> bool:
    return bool(name) and name in PLATFORM_CAPABILITIES


def lookup_allowed(name: str, lookup: str) -> bool:
    """治理时给平台能力选的查询能力对不对。"""
    return LOOKUPS.get(name) == lookup


async def execute(session: Any, capability: Any, args: dict[str, Any], *, environment: str,
                  idem_key: str | None = None, on_behalf: Any = None, run_id: str | None = None,
                  node_id: str | None = None) -> Any:
    """平台能力的执行器，交回和端点请求一样的 EndpointResult：2xx 生效、status 为空且不是 unknown 为确定没有生效
    （error 写原因，actions.send 照实说「没有写入」），404 是查询能力没查到。"""
    from app.capabilities.registry import EndpointResult

    name = getattr(capability, "name", None) or str(capability)
    if name == RECORDS_APPEND:
        from app.records.actions import append_action

        return await append_action(args, idem_key=idem_key, run_id=run_id, on_behalf=on_behalf,
                                   environment=environment)
    if name == RECORDS_LOOKUP:
        from app.records.actions import lookup_action

        return await lookup_action(args, environment=environment)
    if name == TABLE_IMPORT_PERIOD:
        from app.files.imports import import_period_action

        return await import_period_action(args, run_id=run_id, on_behalf=on_behalf, environment=environment)
    return EndpointResult(status=None, body=None, error=f"「{name}」不是平台能力", environment=environment)


def actor_of(on_behalf: Any) -> str | None:
    """写记录、签导入记哪个人：身份断言里的 sub（运行的发起人：这件事是替他办的）。"""
    if isinstance(on_behalf, dict):
        sub = on_behalf.get("sub")
        return str(sub) if sub else None
    return str(on_behalf) if on_behalf else None
