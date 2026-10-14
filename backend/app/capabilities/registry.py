"""能力登记的对外接口（附录 C，BE-1 提供）。

契约骨架阶段的默认行为和今天一致：is_platform_query 仍按数据源查询工具的名字前缀认，其余接口未实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class EndpointResult:
    status: int | None
    #: 响应体（解析后的 JSON 或文本）
    body: Any
    #: 按 tabular 落成的查询快照形状的工件 id（声明了 tabular 的读取才有）
    snapshot_artifact: str | None = None
    #: 网络层没拿到确定结果（超时、连接中断）
    unknown: bool = False
    error: str | None = None
    environment: str = "prod"


async def sync_all(session: AsyncSession) -> int:
    """把内置工具、数据源、自定义工具、MCP 同步进 capabilities。返回变动的行数。启动时和各对象增删改时调。"""
    raise NotImplementedError("BE-1")


# 运行路径上会被别的包调到的几个函数，契约骨架里给的是「和今天行为一致」的默认实现（不查登记、不改变任何判定），
# 并行开发时一个包没写完不会弄挂别的包的测试。BE-1 换成真实现，签名不变。


async def get_capability(session: AsyncSession, name: str) -> Any | None:
    """一行能力登记（db.models_capabilities.Capability），没有就是 None。"""
    return None


def effective_effect(capability: Any) -> str:
    """治理后的效果：effect 为空时按 effect_system。"""
    return getattr(capability, "effect", None) or getattr(capability, "effect_system", None) or "dynamic"


async def capability_snapshot(session: AsyncSession, tool_names: list[str]) -> dict[str, Any]:
    """运行发起时用到的能力的治理快照：{名字: {effect, approval, unattended_write, version, endpoint_id, masked}}。
    写进 Run.options["capabilities"]，恢复和重放用同一份。"""
    return {}


async def capability_versions_for(session: AsyncSession, tool_names: list[str]) -> dict[str, int]:
    """发布时记下的能力版本：{名字: version}。"""
    return {}


def is_platform_query(name: str) -> bool:
    """这个工具的结果能不能当查询快照作证（替换 nodes/tools.py、nodes/llm.py 里按名字前缀的判断，F11）。
    认的是「平台执行」这个身份：数据源查询工具，和登记里声明了 tabular、绑定端点、由平台执行的读取。"""
    from app.tools.datasource import QUERY_PREFIX

    return name.startswith(QUERY_PREFIX)


async def endpoint_request(session: AsyncSession, capability: Any, args: dict[str, Any], *, environment: str,
                           idem_key: str | None = None, on_behalf: str | None = None,
                           run_id: str | None = None, node_id: str | None = None) -> EndpointResult:
    """平台执行器：按登记的端点发请求（只认登记的 base_url，参数按路径段编码代入），声明了 tabular 的读取落快照。"""
    raise NotImplementedError("BE-1")


def masked_paths(capability: Any) -> list[str]:
    """能力的遮罩字段（JSON 指针）。"""
    return list(getattr(capability, "masked", None) or [])
