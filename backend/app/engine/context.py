from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.config import get_stream_writer

from app.core.events import EventType
from app.engine.schema import REJECT_HANDLE, GraphNode, GraphSpec
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

    @property
    def prefix(self) -> str:
        """子工作流里的节点在这次运行里的 id 前缀（「子工作流节点 id/」，嵌套的一层层接上）；最外层是空串。"""
        return str(self.extra.get(PREFIX_KEY) or "")

    def global_id(self, node_id: str) -> str:
        """这张图里的节点 id → 这次运行里的 id（事件、工件上记的那个）：最外层原样，子工作流里加前缀。"""
        return f"{self.prefix}{node_id}"


#: 子工作流里的节点 id 前缀（RunContext.extra 里的键，engine/nodes/multi.run_subgraph 放）
PREFIX_KEY = "node_prefix"
#: 父运行的事件流（RunContext.extra 里的键，同上）。子工作流在父节点里用 ainvoke 跑，LangGraph 不把它的 custom 事件交给
#: 父图的流（ainvoke 不开 custom 模式，get_stream_writer 拿到的是空的那个）：子工作流里每一步以前在运行的事件里一条都
#: 没有，时间线看不到、封存也不含它查过的数。所以 run_subgraph 把父图那一刻的写入口交下来，子工作流的节点经它发
STREAM_KEY = "stream_writer"


def stream_writer(run: RunContext) -> Any:
    """往这次运行的事件流里写事件的入口：子工作流里是父运行交下来的那个，否则是 LangGraph 这一刻的。不在图执行上下文里
    （单元测试直接调执行器）时为 None。"""
    writer = run.extra.get(STREAM_KEY)
    if writer is not None:
        return writer
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 - 不在图执行上下文里
        return None


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

        子工作流里的节点经父运行交下来的写入口发（stream_writer），节点 id 带上前缀（RunContext.global_id）：
        「子工作流节点 id/节点 id」，嵌套的一层层接上。事件里别的字段（台账条目、口径卡上记的输入）照旧写这张图里的 id，
        读的一方按事件的节点 id 认它在哪一层（api/evidence 的 _scoped）。
        """
        writer = stream_writer(self.run)
        if writer is None:
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
                "node_id": self.run.global_id(self.node.id),
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
    Run.error 和以前一字不差。kind 见 REJECTION_KINDS；decision 是 reject 或 expired_reject（同待办的结论）。

    节点在画布上接了「驳回」出口（REJECT_HANDLE，engine/schema.reject_targets）时，驳回不让运行结束：compiler 接住它，
    这一步正常结束、产出 exit_payload()、沿驳回那条边往下走（docs/design/orchestration-round/00-总方案.md §3）。stop 为真
    的除外：审批节点明说了「驳回即终止运行」，那条驳回边照旧走不到（以前就是这样），运行照旧收成被驳回。"""

    def __init__(self, node_id: str, message: str, *, kind: str, decision: str = "reject",
                 actor: str | None = None, note: str = "", stop: bool = False) -> None:
        super().__init__(node_id, message)
        self.kind = kind
        self.decision = decision
        self.actor = actor
        self.note = note
        self.stop = stop
        #: 走驳回出口时这一步另外交出的：{payload: 产出里的其余字段, evidence: 台账条目, usage: 用量}。节点在抛之前按需填：
        #: 驳回之前已经算出、下游要据此判档的东西不能跟着驳回丢掉（双盲复算的出具缺口和台账条目）。没接出口时用不上
        self.exit_updates: dict[str, Any] | None = None

    @classmethod
    def of(cls, ctx: Any, message: str, *, kind: str, decision: Any, stop: bool = False) -> NodeRejected:
        """按节点上下文和读出来的审批结论（engine/approval.Decision）造一个：谁驳回的取 ctx.actor()。"""
        return cls(ctx.node.id, message, kind=kind, decision="expired_reject" if decision.expired else "reject",
                   actor=ctx.actor(), note=decision.note or "", stop=stop)

    def exit_payload(self) -> dict[str, Any]:
        """走驳回出口时这一步的产出：{rejected, decision, actor, note, kind, text}，加路由用的 __decision__。

        text 是原话（「「付款」未确认（被驳回），没有发送」），下游的「通知」「退回修改」写 {{ nodes.<id>.text }} 就有一句
        说得清的话；note 是驳回人写的备注。审批模式另带 approved=false：以前接了驳回边的审批节点产出就是
        {approved: false, note}，下游模板读 approved 的照旧读得到。"""
        out: dict[str, Any] = {"rejected": True, "decision": self.decision, "actor": self.actor, "note": self.note,
                               "kind": self.kind, "text": str(self), "__decision__": REJECT_HANDLE}
        if self.kind == "approve":
            out["approved"] = False
        return out

