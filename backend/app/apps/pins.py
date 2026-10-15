"""钉住项落到库里：每个钉住项对应哪一版工作流、那一版的级别、能不能用。检查、发布、解析、入口共用。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.versions import is_published
from app.core.artifact_store import graph_hash
from app.db.models import DataSource, Workflow, WorkflowVersion


@dataclass
class PinState:
    alias: str
    pin: dict[str, Any]
    workflow: Any | None
    row: Any | None

    @property
    def name(self) -> str:
        return self.workflow.name if self.workflow is not None else str(self.pin.get("workflow_id") or "")

    @property
    def label(self) -> str:
        """「周销售 v7」。"""
        return f"{self.name} v{self.pin.get('version')}"

    @property
    def graph(self) -> dict[str, Any]:
        return (self.row.graph if self.row is not None else None) or {}

    @property
    def hash(self) -> str | None:
        if self.row is None:
            return None
        return self.row.graph_hash or graph_hash(self.row.graph or {})

    @property
    def retired(self) -> bool:
        return self.row is not None and self.row.retired_at is not None

    @property
    def published(self) -> bool:
        return self.row is not None and self.workflow is not None and is_published(
            self.row, current=self.workflow.published_version)

    @property
    def usable(self) -> bool:
        """能从它发起正式运行：发布过、没退役、指纹和应用记下的一样。"""
        return self.published and self.hash == self.pin.get("graph_hash")

    @property
    def level(self) -> str | None:
        """这一版的发布级别。记级别之前发布的老版本没有，照旧看工作流现在的 status（同 launch_run）。"""
        if self.row is None:
            return None
        if self.row.level:
            return self.row.level
        if self.published and self.workflow is not None:
            return "governed" if self.workflow.status == "governed" else "published"
        return None


async def load_pins(session: AsyncSession, spec: dict[str, Any]) -> dict[str, PinState]:
    out: dict[str, PinState] = {}
    for pin in spec.get("pins") or []:
        if not isinstance(pin, dict) or not pin.get("alias"):
            continue
        wf_id, version = pin.get("workflow_id"), pin.get("version")
        workflow = await session.get(Workflow, wf_id) if isinstance(wf_id, str) else None
        row = None
        if workflow is not None and isinstance(version, int):
            row = (await session.execute(select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == wf_id, WorkflowVersion.version == version))).scalar_one_or_none()
        out[str(pin["alias"])] = PinState(alias=str(pin["alias"]), pin=pin, workflow=workflow, row=row)
    return out


def needs_upgrade(pins: dict[str, PinState]) -> bool:
    """钉住的版本有退役的（或者已经不能用了）：入口拒绝发起新实例，视图照实写明（A1）。"""
    return any(not p.usable for p in pins.values())


#: 结果只由参数决定的内置工具：同样的输入算出同样的东西。别的（联网、读文件、知识库、记忆、当前时间、执行代码）都算活的
_FIXED_TOOLS = frozenset({"calculator", "json_query"})


async def reads_live(session: AsyncSession, graph: dict[str, Any]) -> bool:
    """这张图会不会查活库：绑了不是上传表格的数据源、MCP、自定义工具，或者工具名是模板拼的（发起时说不准）。

    上传表格按数据版本固定（runner._data_versions），同一版本查到的就是同一份；其余的源每次查都可能不一样，按需视图的
    缓存必须有时效（app.on_demand_ttl）。"""
    from app.engine.launch import tool_bindings
    from app.engine.schema import GraphSpec
    from app.tools.datasource import source_of_tool

    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001 - 图不成形交给发起时报
        return False
    sources: set[str] = set()
    for _, name in tool_bindings(spec):
        if "{{" in name:
            return True
        if (source := source_of_tool(name)) is not None:
            sources.add(source)
        elif name not in _FIXED_TOOLS:
            return True
    if not sources:
        return False
    origins = dict((await session.execute(select(DataSource.name, DataSource.origin)
                                          .where(DataSource.name.in_(sorted(sources))))).all())
    return any(origins.get(s) != "upload" for s in sources)


async def write_nodes(session: AsyncSession, graph: dict[str, Any]) -> dict[str, Any]:
    """图里写外部系统的调用工具节点：{节点 id: 能力登记}，按节点出现的顺序。认法和运行时同一个（调用工具节点
    engine/nodes/tools._write_capability → capabilities.registry.writes_externally：治理后效果为 write，或绑定端点、视参数
    而定）：只有它们走动作流程——开发送确认卡、留生效记录、结果未知时要人核实。派生（动作状态、核实岗位）和检查（发送
    确认派给的岗位、核实岗位）共用这一份。只读的调用工具节点从不开确认卡，它写的（或取图级默认的）「派给谁」用不到岗位。
    登记里没有的不算：运行时同样不按写入走。"""
    from app.apps.tree import node_map
    from app.capabilities.registry import effective_effect, get_capability, writes_externally

    out: dict[str, Any] = {}
    for node_id, node in node_map(graph).items():
        name = node["config"].get("tool") if node["type"] == "tool" else None
        if not isinstance(name, str) or not name:
            continue
        cap = await get_capability(session, name)
        if cap is not None and writes_externally(name, effective_effect(cap), getattr(cap, "endpoint_id", None)):
            out[node_id] = cap
    return out
