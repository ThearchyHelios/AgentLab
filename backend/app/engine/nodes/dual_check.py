"""双盲复算节点：另起一路、不看原结果独立算一遍，再和原结果逐项比对。

目前只是占位：图里放了这个节点也能运行，执行器原样跳过、记一条日志，不改任何状态。
真正的复算和比对由后续工作包补上；比不上的项通过节点产出里的 issuance_gaps 交给成果节点的
出具契约降档（见 nodes/io.py 的 _upstream_issuance_gaps）。
"""
from __future__ import annotations

from typing import Any

from app.core.events import EventType
from app.engine.context import NodeContext
from app.engine.state import GraphState

#: 占位期间的跳过原因：节点产出和日志里都写这一句
PENDING_REASON = "双盲复算功能开发中，本节点暂未生效，已跳过"


async def run_dual_check(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    ctx.emit(EventType.LOG, level="info", code="node_pending", message=PENDING_REASON)
    return {"nodes": {ctx.node.id: {"skipped": True, "reason": PENDING_REASON}}}
