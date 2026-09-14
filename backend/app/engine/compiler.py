from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from app.core.events import EventType
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.nodes import control, human, io, knowledge, llm, multi, tools
from app.engine.schema import GraphNode, GraphSpec, NodeType
from app.engine.state import GraphState

NodeRunner = Callable[[GraphState, NodeContext], Awaitable[dict[str, Any]]]

# 节点类型 -> 执行器。加一种新节点只需要在这里登记一行。
RUNNERS: dict[NodeType, NodeRunner] = {
    NodeType.INPUT: io.run_input,
    NodeType.OUTPUT: io.run_output,
    NodeType.TRANSFORM: io.run_transform,
    NodeType.LLM: llm.run_llm,
    NodeType.AGENT: llm.run_agent,
    NodeType.SUPERVISOR: multi.run_supervisor,
    NodeType.SUBGRAPH: multi.run_subgraph,
    NodeType.TOOL: tools.run_tool,
    NodeType.CODE: tools.run_code,
    NodeType.BRANCH: control.run_branch,
    NodeType.LOOP: control.run_loop,
    NodeType.HUMAN: human.run_human,
    NodeType.VALIDATE: human.run_validate,
    NodeType.MEMORY: knowledge.run_memory,
    NodeType.RETRIEVE: knowledge.run_retrieve,
}

# 这些节点的出边由"决定"路由，不能直接连死
ROUTING_TYPES = {NodeType.BRANCH, NodeType.LOOP}


def _wrap(node: GraphNode, run_ctx: RunContext) -> Callable[[GraphState], Awaitable[dict[str, Any]]]:
    """给节点执行器套上事件、计时、重试和错误处理。

    画布上看到的每一次高亮、每一条耗时，都来自这层包装；
    执行器本身只管自己那点业务逻辑。
    """
    runner = RUNNERS.get(node.type)
    ctx = NodeContext(node=node, run=run_ctx)

    async def _execute(state: GraphState) -> dict[str, Any]:
        if runner is None:
            raise NodeError(node.id, f"没有实现的节点类型：{node.type}")

        # 允许节点配置跳过条件，不用为了"有时不执行"专门加一个分支节点
        skip_if = node.config.get("skip_if")
        if skip_if:
            from app.engine.expressions import eval_condition
            from app.engine.state import template_context

            if eval_condition(skip_if, template_context(state)):
                ctx.emit(EventType.NODE_SKIPPED, reason=f"skip_if 成立：{skip_if}")
                return {"nodes": {node.id: {"skipped": True}}}

        retries = int(node.config.get("retries", 0) or 0)
        backoff = float(node.config.get("retry_backoff", 1.0) or 1.0)
        started = time.perf_counter()
        ctx.emit(EventType.NODE_STARTED, node_type=str(node.type), label=node.title)

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                updates = await runner(state, ctx)
                elapsed = int((time.perf_counter() - started) * 1000)

                # 完整输出落工件库。事件里只放截断预览是给画布看的；
                # 出具溯源、周对比、轨迹提模板需要的是这份完整证据。
                artifact_id: str | None = None
                payload = (updates.get("nodes") or {}).get(node.id)
                if payload is not None:
                    from app.core.artifact_store import put_json

                    try:
                        # 只记录、不回写：payload 和 run.output 可能是同一个对象，
                        # 往里塞 __artifact__ 会污染最终成果。事件里携带 id 就够了。
                        artifact_id = await put_json(
                            payload,
                            kind="node_output",
                            run_id=run_ctx.run_id,
                            node_id=node.id,
                            meta={"type": str(node.type), "attempt": attempt + 1},
                        )
                    except Exception:  # noqa: BLE001 - 工件写失败不该毁掉运行本身
                        artifact_id = None

                ctx.emit(
                    EventType.NODE_FINISHED,
                    duration_ms=elapsed,
                    attempt=attempt + 1,
                    preview=_preview(updates, node.id),
                    artifact=artifact_id,
                )
                trail = {
                    "node_id": node.id,
                    "type": str(node.type),
                    "duration_ms": elapsed,
                    "attempt": attempt + 1,
                    "ts": time.time(),
                }
                return {**updates, "trail": [trail]}
            except GraphBubbleUp:
                # interrupt() / Command 靠异常向上传递控制流，不是执行失败：
                # 必须原样放行，否则人工介入会被当成节点报错吞掉。
                raise
            except asyncio.CancelledError:
                raise
            except NodeError:
                raise
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < retries:
                    ctx.emit(
                        EventType.LOG,
                        level="warn",
                        message=f"第 {attempt + 1} 次失败（{type(e).__name__}: {e}），准备重试",
                    )
                    await asyncio.sleep(backoff * (2**attempt))
                    continue
                break

        elapsed = int((time.perf_counter() - started) * 1000)
        message = f"{type(last_error).__name__}: {last_error}"
        ctx.emit(EventType.NODE_FAILED, error=message, duration_ms=elapsed)

        # 容错模式：记下错误继续往下走，而不是让整张图挂掉
        if node.config.get("on_error") == "continue":
            return {
                "nodes": {node.id: {"error": message, "failed": True}},
                "trail": [{"node_id": node.id, "error": message, "ts": time.time()}],
            }
        raise NodeError(node.id, message) from last_error

    async def _entry(state: GraphState) -> dict[str, Any]:
        return await _execute(state)

    return _entry


def _preview(updates: dict[str, Any], node_id: str) -> Any:
    """给前端卡片用的输出摘要，避免把整坨数据塞进事件流。"""
    payload = (updates.get("nodes") or {}).get(node_id)
    if payload is None:
        return None
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                out[key] = value[:1200]
            elif isinstance(value, (int, float, bool, type(None))):
                out[key] = value
            elif isinstance(value, list):
                out[key] = f"[{len(value)} 项]"
            else:
                out[key] = str(value)[:300]
        return out
    return str(payload)[:1200]


def _make_router(
    node: GraphNode, spec: GraphSpec
) -> tuple[Callable[[GraphState], list[str] | str], dict[str, str]]:
    """按节点写下的 __decision__ 把执行导向对应的边。

    路由函数必须是纯函数 —— LangGraph 会调用它决定下一跳，
    所以真正的判断逻辑放在节点执行阶段，这里只查表。
    """
    mapping: dict[str, list[str]] = {}
    for edge in spec.outgoing(node.id):
        key = edge.sourceHandle or "default"
        mapping.setdefault(key, []).append(edge.target)

    default_targets = mapping.get("default") or []

    def route(state: GraphState) -> list[str] | str:
        payload = (state.get("nodes") or {}).get(node.id) or {}
        decision = payload.get("__decision__") if isinstance(payload, dict) else None
        targets = mapping.get(str(decision)) or default_targets
        return targets if targets else END

    path_map = {key: key for key in mapping}
    return route, path_map


def compile_graph(spec: GraphSpec, run_ctx: RunContext) -> StateGraph:
    """把图定义编译成未 compile 的 StateGraph。

    调用方负责挂 checkpointer 后再 .compile()，这样主图能带持久化、
    子图可以不带。
    """
    builder = StateGraph(GraphState)
    node_map = spec.node_map()

    for node in spec.nodes:
        builder.add_node(node.id, _wrap(node, run_ctx))

    # 入口
    entries = spec.entry_nodes()
    if not entries:
        raise ValueError("这张图没有入口节点")
    for node in entries:
        builder.add_edge(START, node.id)

    # 边
    for node in spec.nodes:
        outgoing = spec.outgoing(node.id)
        if not outgoing:
            builder.add_edge(node.id, END)
            continue

        handles = {e.sourceHandle for e in outgoing if e.sourceHandle}
        needs_routing = node.type in ROUTING_TYPES or (
            node.type == NodeType.HUMAN and handles & {"approved", "rejected"}
        )

        if needs_routing:
            route, path_map = _make_router(node, spec)
            targets = {t for edge in outgoing for t in [edge.target] if t in node_map}
            # path_map 用实际目标节点名，LangGraph 才知道有哪些可达分支
            builder.add_conditional_edges(
                node.id,
                route,
                sorted(targets) + [END],
            )
        else:
            for edge in outgoing:
                if edge.target in node_map:
                    builder.add_edge(node.id, edge.target)

    return builder


def initial_state(payload: dict[str, Any] | None = None) -> GraphState:
    data = dict(payload or {})
    return {
        "input": data,
        "vars": dict(data),
        "nodes": {},
        "output": {},
        "messages": [],
        "usage": {},
        "trail": [],
        "loops": {},
    }
