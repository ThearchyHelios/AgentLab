from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.config import get_stream_writer

from app.core.events import EventType
from app.engine.schema import GraphNode, GraphSpec
from app.engine.state import GraphState, template_context
from app.engine.expressions import render_deep, render_template


@dataclass
class RunContext:
    """一次运行的全局上下文，编译时闭包进每个节点。"""

    run_id: str
    thread_id: str
    spec: GraphSpec
    workflow_id: str | None = None
    # 深度 > 0 表示当前在子图里，用来防止子图无限递归
    depth: int = 0
    memory_scope: str = "default"
    collection: str = "default"
    #: 工具审批策略的全局默认（设置里的「危险工具默认需要人工确认」）。节点和图级
    #: defaults 都没配 approval 时才用它；为空表示老运行，按节点各自的缺省走
    approval_default: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeContext:
    """单个节点执行时能拿到的东西。"""

    node: GraphNode
    run: RunContext

    @property
    def config(self) -> dict[str, Any]:
        return self.node.data.config

    def emit(self, event_type: EventType | str, **data: Any) -> None:
        """往 LangGraph 的 custom 流里发一条事件。

        runner 在另一端消费，转成落库的 RunEvent 并广播给前端 —— 画布上的节点高亮、
        token 流、工具调用卡片全都来自这里。
        """
        try:
            writer = get_stream_writer()
        except Exception:  # noqa: BLE001 - 不在图执行上下文里（比如单元测试）
            return
        writer(
            {
                "__agentlab__": True,
                "type": str(event_type),
                "node_id": self.node.id,
                "ts": time.time(),
                "data": data,
            }
        )

    def render(self, value: Any, state: GraphState) -> Any:
        """用当前状态渲染配置里的模板。"""
        return render_deep(value, template_context(state))

    def render_str(self, value: str | None, state: GraphState) -> str:
        return render_template(value or "", template_context(state))

    def cfg(self, key: str, default: Any = None) -> Any:
        """读节点配置，没有就回退到图级 defaults。"""
        value = self.config.get(key)
        if value in (None, ""):
            return self.run.spec.defaults.get(key, default)
        return value

    def approval_mode(self, fallback: str = "dangerous") -> str:
        """工具审批策略：节点 > 图级 defaults > 全局设置 > 节点类型自己的缺省。

        审批恢复时节点会整个重放、再算一遍要不要问人，所以这里只能取决于
        这次运行固定下来的东西——全局默认在发起时就定死在 RunContext 上了。
        """
        return self.cfg("approval") or self.run.approval_default or fallback

    def actor(self) -> str | None:
        """回复这个节点审批的人（设置里的署名）。没署名、或者不是恢复出来的，就是 None。"""
        return (self.run.extra.get("actors") or {}).get(self.node.id)


class NodeError(RuntimeError):
    """节点执行失败。带上 node_id 方便前端定位到具体的卡片。"""

    def __init__(self, node_id: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.retryable = retryable
