from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.func import task

from app.core.config import settings
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Workflow
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, message_text, template_context
from app.providers import catalog
from app.providers.factory import ModelSpec, bind_tools_safely, get_chat_model
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    args_model_of,
    build_tools,
    prepare_args,
)

_MAX_DEPTH = 3


# --------------------------------------------------------------------------
# Supervisor：一个调度者 + 若干专家
# --------------------------------------------------------------------------


async def run_supervisor(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """多 agent 协作。

    调度者每轮只做一件事：看当前进展，决定交给哪个专家、并给它一句具体指令，
    或者宣布收工。专家各自带自己的 system prompt 和工具集。
    把整个协作塞进一个节点，是为了让画布保持可读 —— 展开细节看运行时间线就够了。
    """
    agents = ctx.cfg("agents", []) or []
    if not agents:
        raise NodeError(ctx.node.id, "supervisor 节点至少要配一个 agent")

    names = [a.get("name") or f"agent{i}" for i, a in enumerate(agents)]
    agent_map = {n: a for n, a in zip(names, agents)}
    max_rounds = min(int(ctx.cfg("max_rounds", 6) or 6), settings.max_agent_steps)

    goal = ctx.render_str(ctx.cfg("goal", "{{ last_message }}"), state)
    if not goal.strip():
        goal = json.dumps(template_context(state).get("input"), ensure_ascii=False)

    async with SessionLocal() as session:
        supervisor_model, sup_model_id = await get_chat_model(
            session,
            ModelSpec(
                provider=ctx.cfg("provider"),
                model=ctx.cfg("model"),
                max_tokens=int(ctx.cfg("max_tokens", 2048) or 2048),
            ),
        )

    roster = "\n".join(
        f"- {n}：{agent_map[n].get('description') or agent_map[n].get('system', '')[:100]}"
        for n in names
    )
    route_schema = {
        "title": "route",  # langchain 靠 title 识别 JSON Schema dict
        "type": "object",
        "properties": {
            "next": {"type": "string", "enum": names + ["FINISH"]},
            "instruction": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["next"],
    }

    transcript: list[dict[str, Any]] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}

    def _accumulate(msg: Any, model_id: str) -> None:
        meta = getattr(msg, "usage_metadata", None) or {}
        i, o = int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
        usage_total["input_tokens"] += i
        usage_total["output_tokens"] += o
        usage_total["calls"] += 1
        usage_total["cost_usd"] = round(
            usage_total["cost_usd"] + catalog.estimate_cost(model_id, i, o), 6
        )

    @task
    async def route(round_no: int, progress: str) -> dict[str, Any]:
        prompt = (
            f"你是团队调度者。目标：\n{goal}\n\n"
            f"可用成员：\n{roster}\n\n"
            f"当前进展：\n{progress or '(还没有任何进展)'}\n\n"
            "决定下一步交给谁处理，并给出一句明确的指令。"
            "如果目标已经达成，next 返回 FINISH。"
        )
        try:
            value = await supervisor_model.with_structured_output(route_schema).ainvoke(prompt)
            if not isinstance(value, dict):
                value = json.loads(message_text(value))
        except Exception:  # noqa: BLE001
            raw = message_text(await supervisor_model.ainvoke(prompt))
            picked = next((n for n in names if n in raw), "FINISH")
            value = {"next": picked, "instruction": raw[:500], "reason": ""}
        return value

    @task
    async def work(round_no: int, name: str, instruction: str, progress: str) -> dict[str, Any]:
        cfg = agent_map[name]
        async with SessionLocal() as session:
            model, model_id = await get_chat_model(
                session,
                ModelSpec(
                    provider=cfg.get("provider") or ctx.cfg("provider"),
                    model=cfg.get("model") or ctx.cfg("model"),
                    max_tokens=int(cfg.get("max_tokens") or 4096),
                ),
            )
            tool_ctx = ToolContext(
                run_id=ctx.run.run_id,
                node_id=f"{ctx.node.id}:{name}",
                sandbox_session=ctx.run.thread_id,
                memory_scope=ctx.run.memory_scope,
                collection=ctx.run.collection,
            )
            tools = await build_tools(cfg.get("tools", []) or [], tool_ctx, session=session)

        bound = bind_tools_safely(model, tools, parallel=False)
        tool_map = {t.name: t for t in tools}
        messages: list[Any] = [
            SystemMessage(content=cfg.get("system") or f"你是 {name}。"),
            HumanMessage(content=f"团队目标：{goal}\n\n已有进展：\n{progress or '(无)'}\n\n你的任务：{instruction}"),
        ]
        calls: list[dict[str, Any]] = []
        for _ in range(int(cfg.get("max_steps") or 4)):
            reply = await bound.ainvoke(messages)
            _accumulate(reply, model_id)
            messages.append(reply)
            tool_calls = getattr(reply, "tool_calls", None) or []
            if not tool_calls:
                return {"text": message_text(reply), "tool_calls": calls}
            for call in tool_calls:
                tname = call.get("name", "")
                targs = call.get("args", {}) or {}
                cid = call.get("id") or f"{name}-{tname}"
                if tname not in tool_map:
                    ctx.emit(EventType.TOOL_START, tool=tname, args=targs, agent=name, call_id=cid)
                    content = f"错误：没有名为 {tname} 的工具"
                else:
                    schema = args_model_of(tool_map[tname])
                    fix_note = None
                    args_error = None
                    if schema is not None:
                        try:
                            targs, fix_note = prepare_args(schema, targs)
                        except ToolArgsError as e:
                            args_error = str(e)
                    # 纠正后的参数才是真正要执行的那份，tool.start 要发它
                    ctx.emit(EventType.TOOL_START, tool=tname, args=targs, agent=name, call_id=cid)
                    if fix_note:
                        ctx.emit(EventType.LOG, level="warn",
                                 message=f"工具 {tname}：{fix_note}")
                    if args_error:
                        # 参数就不对，没必要真调一次。把"它接受什么"喂回去让它改
                        content = args_error
                    else:
                        try:
                            raw = await tool_map[tname].ainvoke(targs)
                            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                        except Exception as e:  # noqa: BLE001
                            content = f"工具失败：{type(e).__name__}: {e}"
                ctx.emit(EventType.TOOL_END, tool=tname, agent=name, call_id=cid, preview=content[:1500])
                calls.append({"tool": tname, "args": targs, "result": content[:2000]})
                messages.append(ToolMessage(content=content, tool_call_id=cid))
        return {"text": message_text(messages[-1]), "tool_calls": calls}

    progress_lines: list[str] = []
    final_text = ""
    for round_no in range(max_rounds):
        progress = "\n\n".join(progress_lines)
        decision = await route(round_no, progress)
        _accumulate(AIMessage(content=""), sup_model_id)  # route 用了一次调用
        nxt = str(decision.get("next") or "FINISH")
        reason = str(decision.get("reason", ""))
        ctx.emit(
            EventType.LOG, level="info", round=round_no,
            message=f"调度 → {nxt}" + (f"（{reason}）" if reason else ""),
        )
        if nxt == "FINISH" or nxt not in agent_map:
            final_text = progress_lines[-1].split("：", 1)[-1] if progress_lines else ""
            break

        instruction = str(decision.get("instruction") or goal)
        ctx.emit(EventType.AGENT_STEP_START, agent=nxt, instruction=instruction[:300],
                 round=round_no)
        started = time.perf_counter()
        outcome = await work(round_no, nxt, instruction, progress)
        elapsed = int((time.perf_counter() - started) * 1000)
        ctx.emit(EventType.AGENT_STEP_END, agent=nxt, duration_ms=elapsed,
                 round=round_no, preview=outcome["text"][:500])

        progress_lines.append(f"【{nxt}】{outcome['text']}")
        transcript.append(
            {"round": round_no, "agent": nxt, "instruction": instruction, **outcome,
             "duration_ms": elapsed}
        )
        final_text = outcome["text"]

    usage_total["total_tokens"] = usage_total["input_tokens"] + usage_total["output_tokens"]
    result = {
        "text": final_text,
        "rounds": len(transcript),
        "transcript": transcript,
        "agents": names,
    }
    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": usage_total,
    }
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=final_text or "(空)")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: final_text}
    return updates


# --------------------------------------------------------------------------
# 子图：把另一张工作流当成一个节点
# --------------------------------------------------------------------------


async def run_subgraph(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """嵌套执行另一个工作流。

    子图共享同一个 run 的事件流（节点 id 会加前缀），所以在时间线上能看到它内部
    每一步；但它有独立的状态，不会污染父图的变量池。
    """
    from app.engine.compiler import compile_graph  # 延迟导入，避免循环依赖
    from app.engine.schema import GraphSpec

    if ctx.run.depth >= _MAX_DEPTH:
        raise NodeError(ctx.node.id, f"子图嵌套超过 {_MAX_DEPTH} 层，已阻止")

    workflow_id = ctx.cfg("workflow_id")
    if not workflow_id:
        raise NodeError(ctx.node.id, "子图节点没有选择工作流")

    # 版本钉死：这就是"方法卡"的机制核心。钉了 workflow_version 就永远
    # 执行那个不可变快照，上游改了方法卡也不会让这里的口径悄悄漂移；
    # 没钉则跟随最新（画布试跑方便，但治理 lint 会在受管模板里拦下它）。
    pinned = ctx.cfg("workflow_version")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        if not workflow:
            raise NodeError(ctx.node.id, f"找不到工作流 {workflow_id}")
        if pinned:
            from sqlalchemy import select

            from app.db.models import WorkflowVersion

            snapshot = (
                await session.execute(
                    select(WorkflowVersion).where(
                        WorkflowVersion.workflow_id == workflow_id,
                        WorkflowVersion.version == int(pinned),
                    )
                )
            ).scalar_one_or_none()
            if not snapshot:
                raise NodeError(
                    ctx.node.id, f"工作流「{workflow.name}」没有 v{pinned} 这个版本"
                )
            sub_graph = snapshot.graph
            used_version = int(pinned)
        else:
            sub_graph = workflow.graph
            used_version = workflow.version

    sub_input = ctx.render(ctx.cfg("input", {}) or {}, state)
    if not isinstance(sub_input, dict):
        sub_input = {"input": sub_input}

    sub_spec = GraphSpec.model_validate(sub_graph)
    sub_run = type(ctx.run)(
        run_id=ctx.run.run_id,
        thread_id=f"{ctx.run.thread_id}:{ctx.node.id}",
        spec=sub_spec,
        workflow_id=workflow.id,
        depth=ctx.run.depth + 1,
        memory_scope=ctx.run.memory_scope,
        collection=ctx.run.collection,
        extra={**ctx.run.extra, "node_prefix": f"{ctx.node.id}/"},
    )

    ctx.emit(EventType.LOG, level="info",
             message=f"进入子图「{workflow.name}」（{len(sub_spec.nodes)} 个节点）")

    compiled = compile_graph(sub_spec, sub_run)
    # 子图不带 checkpointer：它的断点由父图的 checkpoint 统一承载
    app = compiled.compile()
    result_state = await app.ainvoke(
        {
            "input": sub_input,
            "vars": sub_input,
            "nodes": {},
            "output": {},
            "messages": [],
            "usage": {},
            "trail": [],
            "loops": {},
        },
        {"recursion_limit": settings.max_graph_steps},
    )

    output = result_state.get("output") or {}
    result = {
        "output": output,
        "workflow": workflow.name,
        # 溯源信息：这次到底跑的是哪个版本、是不是钉死的
        "workflow_version": used_version,
        "pinned": bool(pinned),
        "text": json.dumps(output, ensure_ascii=False) if output else "",
    }
    ctx.emit(EventType.LOG, level="info", message=f"子图「{workflow.name}」完成")

    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": result_state.get("usage") or {},
    }
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output}
    return updates
