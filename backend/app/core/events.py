from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """前端与后端之间的实时协议。画布高亮、时间线、token 流全部基于这套事件。"""

    # run 级
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"  # 等待人工介入
    RUN_RESUMED = "run.resumed"

    # 节点级
    NODE_STARTED = "node.started"
    NODE_FINISHED = "node.finished"
    NODE_FAILED = "node.failed"
    NODE_SKIPPED = "node.skipped"
    EDGE_TAKEN = "edge.taken"  # 条件分支实际走了哪条边

    # 模型级
    LLM_START = "llm.start"
    LLM_TOKEN = "llm.token"
    LLM_THINKING = "llm.thinking"
    LLM_END = "llm.end"

    # 工具 / 沙箱
    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    TOOL_ERROR = "tool.error"
    SANDBOX_START = "sandbox.start"
    SANDBOX_END = "sandbox.end"

    # 多 agent 协作中的单个 agent 步骤
    AGENT_STEP_START = "agent.step.start"
    AGENT_STEP_END = "agent.step.end"

    # 人工介入
    HUMAN_REQUESTED = "human.requested"
    HUMAN_RESOLVED = "human.resolved"

    # 其他
    LOG = "log"
    USAGE = "usage"  # token / 成本累计


class RunEventModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    seq: int = 0
    type: EventType
    node_id: str | None = None
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
