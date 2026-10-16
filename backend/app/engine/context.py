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
    #: MCP / 自定义工具信任三档的快照（tools/trust.py）。None 是升级前发起的运行，
    #: 这两类工具照旧不问人
    tool_trust: dict[str, str] | None = None
    #: agent 护栏的上限快照（engine/guards.py）。None 是升级前发起的运行，走旧逻辑
    agent_limits: dict[str, Any] | None = None
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
        if str(event_type) == EventType.LLM_END and "unpriced" not in data:
            from app.providers.catalog import unpriced_call

            if unpriced_call(data):
                # 金额估不出来（模型不在价格目录里）：标出来，界面不把它的 0 当成「没花钱」
                data = {**data, "unpriced": True}
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


#: 驳回发生在哪种关卡上（NodeRejected.kind）：人工节点的三种模式、写入前的动作确认、代码执行审批、工具调用审批、双盲裁决
REJECTION_KINDS = ("approve", "input", "edit", "action_confirm", "code", "tool", "dual_check")


class NodeRejected(NodeError):
    """人的决定让运行到这里结束：驳回、超时驳回（docs/design/next-round/05-已知遗留.md (a)）。

    这不是故障。以前六处驳回都抛普通的 NodeError，运行记成 failed：通知推「实例失败」，定时推「运行失败」，发布单把
    驳回过的试运行说成「试运行失败了」，记录页还给「继续运行」——接着跑只会读到同一个驳回答复、再停一次。现在 runner
    单独接住它，收成 cancelled + end_reason=rejected，run.cancelled 带上在哪一步、谁、结论和备注，消费方都读这一个字段。

    是 NodeError 的子类：节点这一步照旧记 node.failed（时间线、实例进度按它收尾）；但不重试、不被 on_error=continue
    接住（engine/compiler._execute：那两项是给故障准备的，驳回被吞掉运行就以 succeeded 结束了）。原话（str）照旧写给人看，
    Run.error 和以前一字不差。kind 见 REJECTION_KINDS；decision 是 reject 或 expired_reject（同待办的结论）。"""

    def __init__(self, node_id: str, message: str, *, kind: str, decision: str = "reject",
                 actor: str | None = None, note: str = "") -> None:
        super().__init__(node_id, message)
        self.kind = kind
        self.decision = decision
        self.actor = actor
        self.note = note

    @classmethod
    def of(cls, ctx: Any, message: str, *, kind: str, decision: Any) -> NodeRejected:
        """按节点上下文和读出来的审批结论（engine/approval.Decision）造一个：谁驳回的取 ctx.actor()。"""
        return cls(ctx.node.id, message, kind=kind, decision="expired_reject" if decision.expired else "reject",
                   actor=ctx.actor(), note=decision.note or "")
