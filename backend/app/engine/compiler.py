from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from app.core.events import EventType
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.nodes import control, human, io, knowledge, llm, metrics, multi, tools
from app.engine.schema import GraphNode, GraphSpec, NodeType, back_edges
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
    NodeType.METRICS: metrics.run_metrics,
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
                    # assign_to 写进 vars 的值原先不在任何落库事件里，界面上
                    # 的"变量当前值"只能从 preview 反推（data ?? text ?? 整个
                    # output）。那个推断大多数时候对——而一个"大多数时候对"
                    # 的调试工具比没有更糟：它会在你最需要它的那次骗你。
                    # 如实发出来，只多一个已截断的值，且只有真的赋了值的节点才有。
                    vars=_vars_preview(updates),
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
            except Exception as e:  # noqa: BLE001
                # NodeError 以前在这里直接 raise，于是节点自己抛的错既不重试、
                # 也不发 node.failed、更不被 on_error=continue 接住。而 NodeError
                # 恰恰是所有执行器表达失败的标准方式（缺必填输入、模型没配、
                # 口径卡算不出…），等于这两个配置对最常见的报错全都不生效。
                last_error = e
                if attempt < retries:
                    ctx.emit(
                        EventType.LOG,
                        level="warn",
                        message=f"第 {attempt + 1} 次失败（{_describe(e)}），准备重试",
                        code="node_retry",
                    )
                    await asyncio.sleep(backoff * (2**attempt))
                    continue
                break

        elapsed = int((time.perf_counter() - started) * 1000)
        message = _describe(last_error)
        # node_id 要带上：前端靠它把出错的节点从"转圈"收敛成"失败"
        ctx.emit(EventType.NODE_FAILED, error=message, duration_ms=elapsed, node_id=node.id)

        # 容错模式：记下错误继续往下走，而不是让整张图挂掉
        if node.config.get("on_error") == "continue":
            return {
                "nodes": {node.id: {"error": message, "failed": True}},
                "trail": [{"node_id": node.id, "error": message, "ts": time.time()}],
            }
        # 已经是 NodeError 就别再套一层，否则消息会变成
        # "NodeError: NodeError: 真正的原因"
        if isinstance(last_error, NodeError):
            raise last_error
        raise NodeError(node.id, message) from last_error

    async def _entry(state: GraphState) -> dict[str, Any]:
        return await _execute(state)

    return _entry


def _describe(error: BaseException | None) -> str:
    """错误消息。NodeError 的 message 本来就是写给人看的，不用再前缀类型名。"""
    if error is None:
        return "未知错误"
    if isinstance(error, NodeError):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _vars_preview(updates: dict[str, Any]) -> dict[str, Any] | None:
    """这一步往 vars 里写了什么。没写就不带这个字段。

    截断比 preview 更狠：这是给变量表当"当前值"用的，看个大概就够，
    要全文有工件库。事件是审计凭证，能省则省。
    """
    written = updates.get("vars")
    if not isinstance(written, dict) or not written:
        return None
    out: dict[str, Any] = {}
    for key, value in list(written.items())[:20]:
        if isinstance(value, str):
            out[key] = value[:600]
        elif isinstance(value, (int, float, bool, type(None))):
            out[key] = value
        elif isinstance(value, list):
            out[key] = f"[{len(value)} 项]"
        elif isinstance(value, dict):
            out[key] = {k: str(v)[:120] for k, v in list(value.items())[:12]}
        else:
            out[key] = str(value)[:300]
    return out


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


def _join_nodes(spec: GraphSpec, node_map: dict[str, GraphNode]) -> set[str]:
    """有两个以上上游、要等汇齐了才能跑的节点。

    每条入边各 add_edge 一次时，LangGraph 的语义是"任何一个上游跑完就触发
    一次"。两条支路一样长，两次触发落在同一步里被合并，看不出问题；一长一短，
    汇合节点就先拿着短的那边跑一次、长的到了再跑一次——模型调用和工具副作用
    都翻倍，下游跟着重跑；循环体里的汇合还会让循环节点多推进一格，把 done
    出口提前放行。

    不用屏障（add_edge([a, b], j)）：分支之后的二选一汇合，没走的那条永远
    不会到，屏障会一直等，而 LangGraph 没有可执行任务时会安静地结束——汇合
    节点和它下游的一切就这么被跳过了，连报错都没有。defer 的语义是"等图里
    其余待执行的任务都跑完，再跑这一次"：该到的都到齐了，没走的也不会被等。
    代价是它也会等图里与它无关、恰好还在跑的支路。

    循环回边不算：循环节点的入边是"入口 + 回边"，那不是汇合。
    """
    entries = [n.id for n in spec.entry_nodes()]
    back = back_edges(node_map, spec.edges, roots=entries)
    upstream: dict[str, set[str]] = defaultdict(set)
    for e in spec.edges:
        if e.source not in node_map or e.target not in node_map:
            continue
        if (e.source, e.target, e.sourceHandle or "") in back:
            continue
        upstream[e.target].add(e.source)
    return {node_id for node_id, sources in upstream.items() if len(sources) >= 2}


def compile_graph(spec: GraphSpec, run_ctx: RunContext) -> StateGraph:
    """把图定义编译成未 compile 的 StateGraph。

    调用方负责挂 checkpointer 后再 .compile()，这样主图能带持久化、
    子图可以不带。
    """
    builder = StateGraph(GraphState)
    node_map = spec.node_map()
    joins = _join_nodes(spec, node_map)

    for node in spec.nodes:
        builder.add_node(node.id, _wrap(node, run_ctx), defer=node.id in joins)

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
