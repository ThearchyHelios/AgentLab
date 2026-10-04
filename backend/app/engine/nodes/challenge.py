"""反方审稿节点：让另一个模型站在反方，专挑上游报告的毛病，逐条提出反驳意见。

目前只是占位：图里放了这个节点也能运行，执行器原样跳过、记一条日志，不改任何状态。
真正的审稿由后续工作包补上；没能回应的反驳通过节点产出里的 issuance_gaps 交给成果节点的
出具契约降档（见 nodes/io.py 的 _upstream_issuance_gaps）。
"""
from __future__ import annotations

from typing import Any

from app.core.events import EventType
from app.engine.context import NodeContext
from app.engine.state import GraphState

#: 占位期间的跳过原因：节点产出和日志里都写这一句
PENDING_REASON = "反方审稿功能开发中，本节点暂未生效，已跳过"


async def run_challenge(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    ctx.emit(EventType.LOG, level="info", code="node_pending", message=PENDING_REASON)
    return {"nodes": {ctx.node.id: {"skipped": True, "reason": PENDING_REASON}}}
