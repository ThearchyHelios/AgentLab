from __future__ import annotations

import json
import time
from typing import Any

from langgraph.types import interrupt

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.approval import read_decision
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState
from app.sandbox.base import SandboxLimits
from app.sandbox.manager import sandbox_manager
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    build_tools,
    call_is_dangerous,
    call_tool,
    get_spec,
)


def _tool_ctx(ctx: NodeContext) -> ToolContext:
    return ToolContext(
        run_id=ctx.run.run_id,
        node_id=ctx.node.id,
        sandbox_session=ctx.run.thread_id,
        memory_scope=ctx.cfg("memory_scope") or ctx.run.memory_scope,
        collection=ctx.cfg("collection") or ctx.run.collection,
    )


async def _is_dangerous(name: str, args: dict[str, Any], ctx: NodeContext) -> bool:
    """内置工具看 ToolSpec；数据源这类动态工具得先建出来，才问得到这一次危不危险。

    审批通过后节点会整个重放，这里会再算一遍——所以它必须只取决于 name 和 args。
    """
    if get_spec(name) is not None:
        return call_is_dangerous(None, name, args)
    async with SessionLocal() as session:
        tools = await build_tools([name], _tool_ctx(ctx), session=session)
    return call_is_dangerous(tools[0] if tools else None, name, args)


async def run_tool(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """直接调用一个工具。参数里的 {{ }} 会先用当前状态渲染。"""
    name = ctx.cfg("tool", "")
    if not name:
        raise NodeError(ctx.node.id, "工具节点没有选择工具")

    args = ctx.render(ctx.cfg("args", {}) or {}, state)
    if not isinstance(args, dict):
        raise NodeError(ctx.node.id, "工具参数必须是对象")

    approval = ctx.cfg("approval", "dangerous")
    if approval == "always" or (approval == "dangerous" and await _is_dangerous(name, args, ctx)):
        ctx.emit(EventType.HUMAN_REQUESTED, mode="approve", tool=name, args=args,
                 title=f"是否允许调用 {name}？")
        decision = read_decision(interrupt(
            {"kind": "tool_approval", "node_id": ctx.node.id, "tool": name, "args": args,
             "title": f"是否允许调用 {name}？"}
        ))
        if decision.args is not None:
            args = decision.args
        ctx.emit(EventType.HUMAN_RESOLVED, tool=name, approved=decision.approved,
                 note=decision.note)
        if not decision.approved:
            raise NodeError(ctx.node.id, f"用户拒绝执行工具 {name}"
                            + (f"：{decision.note}" if decision.note else ""))

    ctx.emit(EventType.TOOL_START, tool=name, args=args)
    started = time.perf_counter()

    def _fixed(note: str) -> None:
        # 替它跑通了这一次，但节点配置里那个错的参数名原封不动，下次还会踩。
        # 所以纠正必须留一条看得见的痕迹，而不是安静地把事办了
        ctx.emit(EventType.LOG, level="warn",
                 message=f"工具 {name}：{note}。请到节点里改正。", code="tool_args_fixed")

    try:
        async with SessionLocal() as session:
            result = await call_tool(name, args, _tool_ctx(ctx), session=session, on_fix=_fixed)
    except KeyError as e:
        raise NodeError(ctx.node.id, str(e)) from e
    except ToolArgsError as e:
        # 参数对不上且没有唯一候选可纠。报错里已经写清楚该填什么，
        # 不要再套一层 "执行失败：" 把它推远
        ctx.emit(EventType.TOOL_ERROR, tool=name, error=str(e))
        if ctx.cfg("fail_fast", True):
            raise NodeError(ctx.node.id, f"工具 {name}：{e}") from e
        result = {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        ctx.emit(EventType.TOOL_ERROR, tool=name, error=f"{type(e).__name__}: {e}")
        if ctx.cfg("fail_fast", True):
            raise NodeError(ctx.node.id, f"工具 {name} 执行失败：{e}") from e
        result = {"error": f"{type(e).__name__}: {e}"}

    elapsed = int((time.perf_counter() - started) * 1000)
    # 取数快照：query（args）和结果集一起进工件库，正式出具时数字回指的就是它
    from app.core.artifact_store import put_json

    try:
        snapshot_id = await put_json(
            {"tool": name, "args": args, "result": result},
            kind="tool_snapshot", run_id=ctx.run.run_id, node_id=ctx.node.id,
        )
    except Exception:  # noqa: BLE001
        snapshot_id = None
    ctx.emit(EventType.TOOL_END, tool=name, duration_ms=elapsed,
             preview=json.dumps(result, ensure_ascii=False, default=str)[:2000],
             artifact=snapshot_id)

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
        decision = read_decision(interrupt(
            {"kind": "code_approval", "node_id": ctx.node.id, "code": code,
             "language": language, "title": "是否执行这段代码？"}
        ))
        if decision.code:
            code = decision.code  # 允许人工改完再跑
        ctx.emit(EventType.HUMAN_RESOLVED, approved=decision.approved, note=decision.note)
        if not decision.approved:
            raise NodeError(ctx.node.id, "用户拒绝执行代码"
                            + (f"：{decision.note}" if decision.note else ""))

    # 隔离档位：strict 要硬件级（microVM），fast 要低延迟（Seatbelt/bwrap），
    # 留空跟随整机默认。要的那档不可用时会安静退回默认，不阻断运行。
    isolation = ctx.cfg("isolation", "") or None

    # 把真正要跑的那份代码发出来。用户在编辑器里写的是带 {{ }} 的模板，
    # 送进沙箱的是渲染后的字符串——两者可能差很远：一个带引号或换行的值
    # 插进字符串字面量里就会把程序写坏，而报错指的是渲染后的行号，用户对着
    # 自己那份数怎么也对不上。这条事件是他们唯一能看到"实际跑了什么"的地方。
    ctx.emit(EventType.SANDBOX_START, language=language, limits=limits.model_dump(),
             isolation=isolation or "auto", code=code[:8000],
             code_truncated=len(code) > 8000,
             interpolated="{{" in str(ctx.cfg("code", "")))
    result = await sandbox_manager.run(
        code,
        language=language,
        limits=limits,
        session_id=ctx.run.thread_id,
        files=ctx.render(ctx.cfg("files", {}) or {}, state),
        backend=isolation,
    )
    if isolation == "strict" and result.backend != "microvm":
        # 要了硬件隔离却没拿到，必须说出来——否则用户以为自己在 VM 里跑
        ctx.emit(EventType.LOG, level="warn",
                 message=f"节点要求 strict 隔离，但 microVM 未就绪，实际用了 {result.backend}",
                 code="isolation_fallback")
    if result.session_reset:
        # 会话工作区被清空了。同一会话的下游节点会突然读不到自己写的文件，
        # 不说一声的话那个 FileNotFoundError 根本无从追查
        ctx.emit(EventType.LOG, level="warn",
                 message="沙箱会话已重置，此前在该会话写入的文件已丢失"
                         "（多为超时重建，或同会话中各节点的内存限额不一致）",
                 code="sandbox_reset")
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
    # 节点的"文本输出"要去掉 print() 补的那个换行。
    #
    # 代码节点只有 print 一条产出通道，而 print / echo / console.log 一定会补
    # 一个换行——它是传输的产物，不是值的一部分。以前 assign_to 直接给原始
    # stdout，于是 `print('ok')` 写进变量的是 "ok\n"：下游 `== 'ok'` 永远不
    # 成立，而 `== 'fail'` 也不成立。同一个变量、同一个表达式，循环条件和分支
    # 条件得出相反的结论——循环正常退出（看着像成功），分支掉进 default（判成
    # 失败），最后交出一份自相矛盾的结果。运行 d47bfcb0 就是这么回事。
    #
    # 只削尾不削头：首行的缩进是内容（比如逐行打印一段缩进文本），削掉就毁了。
    # 原始 stdout 仍在 payload["stdout"] 里，要查"到底跑了什么"看那个。
    text = result.stdout.rstrip()

    # 如果 stdout 是 JSON，顺手解析出来，下游就能直接用字段而不是再写解析
    parsed: Any = None
    sniff = result.stdout.strip()
    if sniff.startswith(("{", "[")):
        try:
            parsed = json.loads(sniff)
        except json.JSONDecodeError:
            parsed = None
    payload["data"] = parsed
    payload["text"] = text

    updates: dict[str, Any] = {"nodes": {ctx.node.id: payload}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        # {{ vars.x }} 和 {{ nodes.x.text }} 必须是同一个值：两种写法给不同的
        # 值比原来的毛病更难查
        updates["vars"] = {var_name: parsed if parsed is not None else text}
    return updates
