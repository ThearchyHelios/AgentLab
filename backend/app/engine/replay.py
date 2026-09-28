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
#: （见 runner 的 _stale_replay）。以后再改节点里 task 的调用顺序，把它加一
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
    return interrupt(payload)
