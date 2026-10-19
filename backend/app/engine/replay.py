"""节点重放时，已经发生过的事不在轨迹里再发生一次。

LangGraph 恢复一个停在 interrupt 上的节点，是把节点函数从头再跑一遍：前面几次
interrupt() 按顺序直接返回已经给过的答复，@task 包住的调用直接取 checkpoint 里的
结果。执行层面不会重复——agent 节点的模型调用、工具调用都在 task 里，副作用只发生
一次。可写在 task 外面的 ctx.emit 每重放一次就再发一遍：运行 fac480ff 的轨迹里，
同一个 call_id 的 python_exec 有两对 tool.start / tool.end，审批请求也问了两回，
签批人还按最后一次恢复的人记。看轨迹的人只能照字面理解成「执行了两次」。
「接着跑」一个失败的节点是同一回事。

所以要落进轨迹、又写在 task 外面的事，经这里发：它本身就是一次 task，第一次真正
走到这里时发出，重放时结果取自 checkpoint、不再发。
"""
from __future__ import annotations

from typing import Any

from langgraph.config import get_config
from langgraph.func import task
from langgraph.types import interrupt

from app.core.events import EventType
from app.engine.context import NodeContext

#: 重放协议的版本，记在每个 checkpoint 的 metadata 里。
#:
#: LangGraph 认 task 的缓存靠「函数名 + 节点里第几次调用」。这一版多了 once / ask 的
#: 记录 task，agent 的 tool_step 也换了位置和返回值：升级前停下的断点照新代码重放，
#: 节点里已经执行过的工具、模型调用对不上旧的位置，会再执行一次（副作用翻倍），缓存
#: 下来的旧返回值还可能直接对不上形状。恢复前先看版本，对不上又有这种风险就拒绝
#: （见 runner 的 _stale_replay）。
#:
#: 规矩：以后改了任何节点里 task 的调用顺序（多一个、少一个、换位置，含 once / ask 的记录 task），把它加一，
#: 并跑一遍升级安全测试（tests/test_replay_side_effects.py 里「跨重放协议」那几条：monkeypatch 成新版本号，
#: 停在人工节点的能恢复、停在 Agent 半路审批的拒绝、写入节点开头等确认的能恢复且只发一次、结果未知的核实后
#: 接着跑且不重发）。应用的流程实例一等就是几天，中间升级是常态：不加一，旧断点会按新顺序重放、重复执行；
#: 加了一，_stale_replay 才认得出哪些断点不能续。写入动作（调用工具节点 tool.start 带 action_key）是唯一的豁免，
#: 理由见 _stale_replay
PROTOCOL = 2
PROTOCOL_KEY = "agentlab_replay"


def protocol_of(metadata: Any) -> int:
    """checkpoint 是按哪一版重放协议写的。加这个标记之前的都算 1。"""
    value = (metadata or {}).get(PROTOCOL_KEY) if isinstance(metadata, dict) else None
    return int(value) if isinstance(value, int) else 1


def _in_graph() -> bool:
    try:
        get_config()
    except RuntimeError:        # 不在图执行上下文里（单独调用执行器的测试）
        return False
    return True


async def once(ctx: NodeContext, event_type: EventType | str, **data: Any) -> None:
    """发一条事件，节点重放时不再发。"""
    if not _in_graph():
        ctx.emit(event_type, **data)
        return

    @task
    async def record(kind: str) -> bool:
        ctx.emit(kind, **data)
        return True

    await record(str(event_type))


async def ask(ctx: NodeContext, payload: dict[str, Any], **request: Any) -> Any:
    """请人审批：human.requested 只在第一次走到这里时发，然后停下等答复。

    返回答复。答复读完之后由调用方用 once() 发 human.resolved——那一条也只该在
    答复真正到达的那次执行里出现。
    """
    await once(ctx, EventType.HUMAN_REQUESTED, **request)
    route = reject_route(ctx) if isinstance(payload, dict) else None
    return interrupt({**payload, "reject_route": route} if route else payload)


def reject_route(ctx: NodeContext) -> dict[str, Any] | None:
    """驳回之后去哪（总方案 §3 的驳回出口），放进中断内容：待办卡的提示、驳回之后的那句、处理过的结果照它说，不再一律写
    「转入驳回分支」或「本次运行终止」。

    - {"to": [{node_id, label}]}：接了驳回出口，驳回后转到这几步（节点 id、名字按这次运行的说法：子工作流里的写
      「子工作流 › x」，同 compiler._rejected_exit 记在 node.finished 上的 to）；
    - {"ends": "run"}：运行就此结束、记为被驳回（没接驳回出口的关卡；审批设了驳回即终止时驳回出口走不到，也是这样）；
    - {"ends": "path"}：审批模式没接驳回出口、没设驳回即终止：这一路到审批为止，不再往下走（engine/nodes/human）。

    不是关卡的节点（agent 自己调工具时的审批：驳回了 agent 照常往下想）为 None，中断内容和以前一字不差。"""
    from app.engine.schema import GATE_TYPES, approve_mode, reject_targets

    node, spec = ctx.node, ctx.run.spec
    if node.type not in GATE_TYPES:
        return None
    approving = approve_mode(spec, node)
    if approving and bool(ctx.cfg("stop_on_reject", False)):
        return {"ends": "run"}
    targets = reject_targets(spec, node)
    if targets:
        nodes = spec.node_map()
        return {"to": [{"node_id": ctx.run.global_id(t),
                        "label": ctx.run.global_label(nodes[t].title if t in nodes else t)}
                       for t in dict.fromkeys(targets)]}
    return {"ends": "path" if approving else "run"}
