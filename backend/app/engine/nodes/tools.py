from __future__ import annotations

import json
import time
from typing import Any

from langgraph.types import interrupt

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState
from app.sandbox.base import SandboxLimits
from app.sandbox.manager import sandbox_manager
from app.tools.registry import ToolContext, call_tool, get_spec


def _tool_ctx(ctx: NodeContext) -> ToolContext:
    return ToolContext(
        run_id=ctx.run.run_id,
        node_id=ctx.node.id,
        sandbox_session=ctx.run.thread_id,
        memory_scope=ctx.cfg("memory_scope") or ctx.run.memory_scope,
        collection=ctx.cfg("collection") or ctx.run.collection,
    )


async def run_tool(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """直接调用一个工具。参数里的 {{ }} 会先用当前状态渲染。"""
    name = ctx.cfg("tool", "")
    if not name:
        raise NodeError(ctx.node.id, "工具节点没有选择工具")

    args = ctx.render(ctx.cfg("args", {}) or {}, state)
    if not isinstance(args, dict):
        raise NodeError(ctx.node.id, "工具参数必须是对象")

    spec = get_spec(name)
    approval = ctx.cfg("approval", "dangerous")
    if approval == "always" or (approval == "dangerous" and spec and spec.dangerous):
        ctx.emit(EventType.HUMAN_REQUESTED, mode="approve", tool=name, args=args,
                 title=f"是否允许调用 {name}？")
        decision = interrupt(
            {"kind": "tool_approval", "node_id": ctx.node.id, "tool": name, "args": args,
             "title": f"是否允许调用 {name}？"}
        )
        approved = decision if isinstance(decision, bool) else bool(
            (decision or {}).get("approved") if isinstance(decision, dict) else decision
        )
        if isinstance(decision, dict) and isinstance(decision.get("args"), dict):
            args = decision["args"]
        ctx.emit(EventType.HUMAN_RESOLVED, tool=name, approved=approved)
        if not approved:
            raise NodeError(ctx.node.id, f"用户拒绝执行工具 {name}")

    ctx.emit(EventType.TOOL_START, tool=name, args=args)
    started = time.perf_counter()
    try:
        async with SessionLocal() as session:
            result = await call_tool(name, args, _tool_ctx(ctx), session=session)
    except KeyError as e:
        raise NodeError(ctx.node.id, str(e)) from e
    except Exception as e:  # noqa: BLE001
        ctx.emit(EventType.TOOL_ERROR, tool=name, error=f"{type(e).__name__}: {e}")
        if ctx.cfg("fail_fast", True):
            raise NodeError(ctx.node.id, f"工具 {name} 执行失败：{e}") from e
        result = {"error": f"{type(e).__name__}: {e}"}

    elapsed = int((time.perf_counter() - started) * 1000)
    ctx.emit(EventType.TOOL_END, tool=name, duration_ms=elapsed,
             preview=json.dumps(result, ensure_ascii=False, default=str)[:2000])

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result}
    return updates


async def run_code(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """沙箱代码节点。代码本身支持模板插值，可以把上游结果直接嵌进去。"""
    code = ctx.render_str(ctx.cfg("code", ""), state)
    if not code.strip():
        raise NodeError(ctx.node.id, "代码节点是空的")

    language = ctx.cfg("language", "python")
    limits = SandboxLimits(
        timeout=int(ctx.cfg("timeout", 30) or 30),
        memory_mb=int(ctx.cfg("memory_mb", 512) or 512),
        network=bool(ctx.cfg("network", False)),
    )

    if ctx.cfg("approval", "never") == "always":
        ctx.emit(EventType.HUMAN_REQUESTED, mode="approve", title="是否执行这段代码？",
                 code=code[:4000], language=language)
        decision = interrupt(
            {"kind": "code_approval", "node_id": ctx.node.id, "code": code,
             "language": language, "title": "是否执行这段代码？"}
        )
        approved = decision if isinstance(decision, bool) else bool(
            (decision or {}).get("approved") if isinstance(decision, dict) else decision
        )
        if isinstance(decision, dict) and decision.get("code"):
            code = decision["code"]  # 允许人工改完再跑
        ctx.emit(EventType.HUMAN_RESOLVED, approved=approved)
        if not approved:
            raise NodeError(ctx.node.id, "用户拒绝执行代码")

    ctx.emit(EventType.SANDBOX_START, language=language, limits=limits.model_dump())
    result = await sandbox_manager.run(
        code,
        language=language,
        limits=limits,
        session_id=ctx.run.thread_id,
        files=ctx.render(ctx.cfg("files", {}) or {}, state),
    )
    ctx.emit(
        EventType.SANDBOX_END,
        ok=result.ok,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        backend=result.backend,
        stdout=result.stdout[:4000],
        stderr=result.stderr[:2000],
    )

    if not result.ok and ctx.cfg("fail_fast", True):
        raise NodeError(
            ctx.node.id,
            f"代码执行失败（exit={result.exit_code}）：{result.error or result.stderr[:500]}",
        )

    payload = result.model_dump()
    # 如果 stdout 是 JSON，顺手解析出来，下游就能直接用字段而不是再写解析
    parsed: Any = None
    text = result.stdout.strip()
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    payload["data"] = parsed
    payload["text"] = result.stdout

    updates: dict[str, Any] = {"nodes": {ctx.node.id: payload}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: parsed if parsed is not None else result.stdout}
    return updates
