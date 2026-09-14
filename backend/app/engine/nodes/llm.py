from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.func import task
from langgraph.types import interrupt
from sqlalchemy import select

from app.core.config import settings
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Skill
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, message_text, template_context, thinking_text
from app.providers import catalog
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model
from app.tools.registry import ToolContext, build_tools, get_spec

# --------------------------------------------------------------------------
# 公共部分
# --------------------------------------------------------------------------


def _model_spec(ctx: NodeContext) -> ModelSpec:
    return ModelSpec(
        provider=ctx.cfg("provider"),
        model=ctx.cfg("model"),
        temperature=ctx.cfg("temperature"),
        max_tokens=ctx.cfg("max_tokens"),
        thinking=ctx.cfg("thinking"),
        effort=ctx.cfg("effort"),
    )


async def _load_skills(names: list[str]) -> str:
    """把挂载的 Skill 拼成一段 system 指令。

    Skill 把"怎么做事"从图结构里抽出来单独管理 —— 改方法论不用动编排。
    """
    if not names:
        return ""
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Skill).where(Skill.name.in_(names), Skill.enabled.is_(True))
                )
            ).scalars()
        )
    if not rows:
        return ""
    blocks: list[str] = []
    for skill in rows:
        block = f"## {skill.name}\n{skill.instructions.strip()}"
        if skill.examples:
            examples = "\n".join(
                f"- 输入：{e.get('input', '')}\n  输出：{e.get('output', '')}"
                for e in skill.examples
                if isinstance(e, dict)
            )
            if examples:
                block += f"\n\n示例：\n{examples}"
        blocks.append(block)
    return "# 适用的方法论\n\n" + "\n\n".join(blocks)


async def _build_messages(state: GraphState, ctx: NodeContext) -> list[BaseMessage]:
    tctx = template_context(state)
    messages: list[BaseMessage] = []

    system_parts: list[str] = []
    system = ctx.render_str(ctx.cfg("system", ""), state)
    if system:
        system_parts.append(system)
    skill_text = await _load_skills(ctx.cfg("skills", []) or [])
    if skill_text:
        system_parts.append(skill_text)
    if system_parts:
        messages.append(SystemMessage(content="\n\n".join(system_parts)))

    # 是否把之前节点的对话历史带上
    if ctx.cfg("use_history", False):
        messages.extend(state.get("messages") or [])

    prompt = ctx.render_str(ctx.cfg("prompt", ""), state)
    if not prompt and not ctx.cfg("use_history", False):
        prompt = str(tctx.get("last_message") or json.dumps(tctx.get("input"), ensure_ascii=False))
    if prompt:
        messages.append(HumanMessage(content=prompt))
    if not messages:
        raise NodeError(ctx.node.id, "没有可发送的内容：system 和 prompt 都是空的")
    return messages


def _usage_of(message: BaseMessage, model_id: str) -> dict[str, Any]:
    meta = getattr(message, "usage_metadata", None) or {}
    inp = int(meta.get("input_tokens") or 0)
    out = int(meta.get("output_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cost_usd": round(catalog.estimate_cost(model_id, inp, out), 6),
        "calls": 1,
    }


async def _invoke_streaming(model: Any, messages: list[BaseMessage], ctx: NodeContext) -> AIMessage:
    """流式调用并把 token 实时推给前端。

    失败时回退到非流式 —— 有些兼容网关不支持 SSE，不该因此让整个节点挂掉。
    """
    ctx.emit(EventType.LLM_START, message_count=len(messages))
    chunks: list[Any] = []
    response: AIMessage | None = None
    try:
        async for chunk in model.astream(messages):
            chunks.append(chunk)
            text = message_text(chunk)
            if text:
                ctx.emit(EventType.LLM_TOKEN, delta=text)
            reasoning = thinking_text(chunk)
            if reasoning:
                # 增量只服务画布的实时滚动，不进轨迹（runner 标记为 ephemeral）
                ctx.emit(EventType.LLM_THINKING_DELTA, delta=reasoning)
    except NotImplementedError:
        response = await model.ainvoke(messages)
    except Exception as e:  # noqa: BLE001
        ctx.emit(EventType.LOG, level="warn", message=f"流式失败，回退非流式：{e}")
        response = await model.ainvoke(messages)

    if response is None:
        if not chunks:
            response = await model.ainvoke(messages)
        else:
            merged = chunks[0]
            for chunk in chunks[1:]:
                merged = merged + chunk
            response = merged

    # 一次思考在轨迹里只占一条事件：调用收尾时落完整内容，
    # 刷新页面回放时也靠它一次性恢复，而不是重放几十条增量
    reasoning = thinking_text(response)
    if reasoning:
        ctx.emit(
            EventType.LLM_THINKING,
            text=reasoning[:6000],
            chars=len(reasoning),
            truncated=len(reasoning) > 6000,
        )
    return response


# --------------------------------------------------------------------------
# LLM 节点：单次调用
# --------------------------------------------------------------------------


async def run_llm(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    spec = _model_spec(ctx)
    async with SessionLocal() as session:
        try:
            model, model_id = await get_chat_model(session, spec)
        except ProviderNotConfigured as e:
            raise NodeError(ctx.node.id, str(e)) from e

    messages = await _build_messages(state, ctx)
    schema = ctx.cfg("output_schema")
    started = time.perf_counter()

    if schema:
        # 要结构化结果就走原生 structured output，这条路径拿不到 token 流
        ctx.emit(EventType.LLM_START, structured=True, message_count=len(messages))
        try:
            structured = model.with_structured_output(schema)
            value = await structured.ainvoke(messages)
        except Exception as e:  # noqa: BLE001
            raise NodeError(ctx.node.id, f"结构化输出失败：{type(e).__name__}: {e}") from e
        payload = value if isinstance(value, (dict, list)) else getattr(value, "model_dump", lambda: value)()
        output = {"data": payload, "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        response: BaseMessage = AIMessage(content=output["text"])
    else:
        response = await _invoke_streaming(model, messages, ctx)
        text = message_text(response)
        reasoning = thinking_text(response)
        if not text and reasoning:
            # 思考型模型把额度全花在推理上了，正文是空的 —— 说清楚而不是抛个空结果给下游
            ctx.emit(EventType.LOG, level="warn",
                     message="模型只输出了思考内容，正文为空。把 max_tokens 调大，或把 thinking 设为 off。")
        output = {"text": text, "thinking": reasoning}

    usage = _usage_of(response, model_id)
    elapsed = int((time.perf_counter() - started) * 1000)
    ctx.emit(
        EventType.LLM_END,
        model=model_id,
        duration_ms=elapsed,
        **usage,
    )

    result = {**output, "model": model_id, "duration_ms": elapsed}
    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": usage,
    }
    if ctx.cfg("emit_message", True):
        updates["messages"] = [response]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output.get("data", output.get("text"))}
    return updates


# --------------------------------------------------------------------------
# Agent 节点：带工具循环
# --------------------------------------------------------------------------


async def _resolve_tools(ctx: NodeContext, state: GraphState) -> list[BaseTool]:
    names = ctx.cfg("tools", []) or []
    tool_ctx = ToolContext(
        run_id=ctx.run.run_id,
        node_id=ctx.node.id,
        sandbox_session=ctx.run.thread_id,
        memory_scope=ctx.cfg("memory_scope") or ctx.run.memory_scope,
        collection=ctx.cfg("collection") or ctx.run.collection,
    )
    async with SessionLocal() as session:
        return await build_tools(names, tool_ctx, session=session)


def _needs_approval(ctx: NodeContext, tool_name: str) -> bool:
    mode = ctx.cfg("approval", "dangerous")  # never | dangerous | always
    if mode == "always":
        return True
    if mode == "never":
        return False
    spec = get_spec(tool_name)
    return bool(spec and spec.dangerous)


async def run_agent(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """ReAct 循环：模型想调工具就调，拿到结果继续想，直到给出最终答案。

    这里没有用 prebuilt 的 create_react_agent，因为需要在循环内部做三件它不做的事：
    每步工具调用都往画布推事件、对危险工具逐个弹人工审批、以及步数护栏。
    """
    model_spec = _model_spec(ctx)
    async with SessionLocal() as session:
        try:
            base_model, model_id = await get_chat_model(session, model_spec)
        except ProviderNotConfigured as e:
            raise NodeError(ctx.node.id, str(e)) from e

    tools = await _resolve_tools(ctx, state)
    tool_map = {t.name: t for t in tools}
    model = base_model.bind_tools(tools) if tools else base_model

    messages = await _build_messages(state, ctx)
    max_steps = min(int(ctx.cfg("max_steps", 8) or 8), settings.max_agent_steps)
    total_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    transcript: list[dict[str, Any]] = []

    # @task 的返回值会进 checkpoint：人工审批导致节点重放时，
    # 之前已经完成的模型调用和工具执行不会重跑，也就不会重复计费。
    @task
    async def llm_step(step: int, payload: list[BaseMessage]) -> BaseMessage:
        return await _invoke_streaming(model, payload, ctx)

    @task
    async def tool_step(step: int, name: str, args: dict[str, Any]) -> str:
        tool = tool_map[name]
        result = await tool.ainvoke(args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    final_text = ""
    for step in range(max_steps):
        response = await llm_step(step, messages)
        messages.append(response)
        usage = _usage_of(response, model_id)
        for key in ("input_tokens", "output_tokens", "calls"):
            total_usage[key] += usage[key]
        total_usage["cost_usd"] = round(total_usage["cost_usd"] + usage["cost_usd"], 6)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = message_text(response)
            break

        for call in tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            call_id = call.get("id") or f"{ctx.node.id}-{step}-{name}"

            if name not in tool_map:
                messages.append(
                    ToolMessage(content=f"错误：没有名为 {name} 的工具", tool_call_id=call_id)
                )
                continue

            if _needs_approval(ctx, name):
                ctx.emit(
                    EventType.HUMAN_REQUESTED,
                    mode="approve",
                    tool=name,
                    args=args,
                    title=f"Agent 想调用工具 {name}",
                )
                decision = interrupt(
                    {
                        "kind": "tool_approval",
                        "node_id": ctx.node.id,
                        "tool": name,
                        "args": args,
                        "title": f"是否允许调用 {name}？",
                    }
                )
                approved, note, override = _parse_decision(decision)
                ctx.emit(EventType.HUMAN_RESOLVED, tool=name, approved=approved, note=note)
                if not approved:
                    messages.append(
                        ToolMessage(
                            content=f"用户拒绝了这次调用。原因：{note or '未说明'}。请换一种方式。",
                            tool_call_id=call_id,
                        )
                    )
                    transcript.append({"tool": name, "args": args, "denied": True, "note": note})
                    continue
                if isinstance(override, dict) and override:
                    args = override

            ctx.emit(EventType.TOOL_START, tool=name, args=args, call_id=call_id)
            started = time.perf_counter()
            try:
                content = await tool_step(step, name, args)
                ok = True
            except Exception as e:  # noqa: BLE001 - 工具失败要喂回模型，让它自己纠错
                content = f"工具执行失败：{type(e).__name__}: {e}"
                ok = False
            elapsed = int((time.perf_counter() - started) * 1000)
            snapshot_id: str | None = None
            if ok:
                from app.core.artifact_store import put_json

                try:
                    snapshot_id = await put_json(
                        {"tool": name, "args": args, "result": content},
                        kind="tool_snapshot", run_id=ctx.run.run_id, node_id=ctx.node.id,
                    )
                except Exception:  # noqa: BLE001
                    snapshot_id = None
            ctx.emit(
                EventType.TOOL_END if ok else EventType.TOOL_ERROR,
                tool=name,
                call_id=call_id,
                duration_ms=elapsed,
                preview=content[:2000],
                artifact=snapshot_id,
            )
            transcript.append(
                {"tool": name, "args": args, "ok": ok, "duration_ms": elapsed, "result": content[:4000]}
            )
            messages.append(ToolMessage(content=content, tool_call_id=call_id))
    else:
        final_text = message_text(messages[-1]) if messages else ""
        ctx.emit(
            EventType.LOG,
            level="warn",
            message=f"agent 达到最大步数 {max_steps}，提前收尾",
        )

    total_usage["total_tokens"] = total_usage["input_tokens"] + total_usage["output_tokens"]
    result = {
        "text": final_text,
        "steps": len(transcript),
        "tool_calls": transcript,
        "model": model_id,
    }
    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": total_usage,
    }
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=final_text or "(空)")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: final_text}
    return updates


def _parse_decision(decision: Any) -> tuple[bool, str, dict[str, Any] | None]:
    """人工审批的返回值兼容几种写法：布尔、字符串、或带备注的结构体。"""
    if isinstance(decision, bool):
        return decision, "", None
    if isinstance(decision, str):
        return decision.lower() in ("yes", "y", "true", "approve", "ok", "同意"), decision, None
    if isinstance(decision, dict):
        approved = decision.get("approved")
        if approved is None:
            approved = decision.get("decision") in ("approve", "yes", True)
        return bool(approved), str(decision.get("note", "")), decision.get("args")
    return bool(decision), "", None
