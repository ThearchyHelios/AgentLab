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
from app.engine.approval import read_decision
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, message_text, template_context, thinking_text
from app.providers import catalog
from app.providers.factory import (
    ModelSpec,
    ProviderNotConfigured,
    bind_tools_safely,
    get_chat_model,
)
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    args_model_of,
    build_tools,
    call_is_dangerous,
    describe_args,
    prepare_args,
)

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


def explain_model_error(exc: Exception, model_id: str) -> str:
    """把供应商的原始报错翻成能照着做的一句话。

    最典型的是 `401 invalid_model`：网关用"认证失败"的状态码报"模型不存在"，
    原文长得像 key 过期，实际上 key 好好的、只是这个 model id 在这个端点上
    不存在（供应商配置里列了几个它根本不提供的模型，就会这样）。照着原文去
    换 key 是白费功夫，所以这里必须把话说对。
    """
    text = str(exc)
    if "invalid_model" in text or "model does not exist" in text.lower():
        return (
            f"模型「{model_id}」在这个供应商上不存在，或者当前 key 没有它的权限。"
            f"去「设置 → 供应商」把模型列表改成它真正提供的 id，"
            f"或者在节点上换一个模型。"
        )
    if "insufficient" in text.lower() or "quota" in text.lower():
        return f"模型「{model_id}」的额度用完了：{text[:200]}"
    return f"调用模型「{model_id}」失败：{type(exc).__name__}: {text[:300]}"


async def _invoke_streaming(
    model: Any, messages: list[BaseMessage], ctx: NodeContext, model_id: str = ""
) -> AIMessage:
    """流式调用并把 token 实时推给前端。

    失败时回退到非流式 —— 有些兼容网关不支持 SSE，不该因此让整个节点挂掉。
    但回退也失败的话，两次都是同一个原因，不能只把原始异常往上抛：用户看到的
    会是两段一模一样的供应商报错，而里面既没有模型名也没有下一步该干什么。
    """
    # 模型 id 要跟着 llm.start 走。以前这条事件只有 message_count，于是调用失败时
    # 整条轨迹里**没有任何地方**记着试的是哪个模型——排查只能靠翻配置猜。
    ctx.emit(EventType.LLM_START, model=model_id or None, message_count=len(messages))
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
        ctx.emit(EventType.LOG, level="warn",
                 message=f"流式失败，回退非流式：{explain_model_error(e, model_id)}",
                 code="stream_fallback")
        try:
            response = await model.ainvoke(messages)
        except Exception as retry_error:  # noqa: BLE001
            raise NodeError(ctx.node.id, explain_model_error(retry_error, model_id)) from retry_error

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
        ctx.emit(EventType.LLM_START, model=model_id, structured=True,
                 message_count=len(messages))
        try:
            # langchain 靠 "title" 识别裸 JSON Schema dict，缺了会抛 Unsupported function
            if isinstance(schema, dict) and "title" not in schema:
                schema = {"title": "output", **schema}
            structured = model.with_structured_output(schema)
            value = await structured.ainvoke(messages)
        except Exception as e:  # noqa: BLE001
            # 模型压根不存在也会走到这里，那不是"结构化输出失败"
            if "invalid_model" in str(e) or "model does not exist" in str(e).lower():
                raise NodeError(ctx.node.id, explain_model_error(e, model_id)) from e
            raise NodeError(ctx.node.id, f"结构化输出失败：{type(e).__name__}: {e}") from e
        payload = value if isinstance(value, (dict, list)) else getattr(value, "model_dump", lambda: value)()
        output = {"data": payload, "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        response: BaseMessage = AIMessage(content=output["text"])
    else:
        response = await _invoke_streaming(model, messages, ctx, model_id)
        text = message_text(response)
        reasoning = thinking_text(response)
        if not text and reasoning:
            # 思考型模型把额度全花在推理上了，正文是空的 —— 说清楚而不是抛个空结果给下游
            ctx.emit(EventType.LOG, level="warn",
                     message="模型只输出了思考内容，正文为空。把 max_tokens 调大，或把 thinking 设为 off。",
                     code="empty_completion")
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


SKIPPED_NOTE = (
    "本轮只执行了第一个工具。看到它的结果之后，再决定这一个还要不要调、要用什么参数调。"
)


def split_tool_calls(
    tool_calls: list[dict[str, Any]], *, parallel: bool
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], ToolMessage]]]:
    """把一轮里的工具调用分成"这次跑"和"这次不跑"。

    串行时只跑第一个。剩下的不能就这么丢掉：两家 API 都要求每个 tool_use 在
    下一轮有一条配对的 tool_result，少一条下次调用直接 400。所以这里给每个
    未执行的调用配一条说明性的 ToolMessage 一起交出去。

    它们也**绝不能**发 tool.start —— 前端靠 call_id 配对 start/end
    （frontend/src/run/decode.ts），发了一条没有结尾的 start，那个工具卡片就会
    在界面上永远转圈。所以这个函数不碰事件，只产消息。
    """
    if parallel or len(tool_calls) <= 1:
        return list(tool_calls), []
    deferred = [
        (call, ToolMessage(content=SKIPPED_NOTE, tool_call_id=call.get("id") or f"skipped-{i}"))
        for i, call in enumerate(tool_calls[1:])
    ]
    return tool_calls[:1], deferred


def _needs_approval(ctx: NodeContext, tool: BaseTool, args: dict[str, Any]) -> bool:
    mode = ctx.cfg("approval", "dangerous")  # never | dangerous | always
    if mode == "always":
        return True
    if mode == "never":
        return False
    return call_is_dangerous(tool, tool.name, args)


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
    # 默认串行：一轮只调一个工具，拿到结果再想下一步。并行更快，但一批工具
    # 中间没有任何新的思考——第二个调用是照着"还没看到第一个结果"时的判断发出来的
    parallel_tools = bool(ctx.cfg("parallel_tools", False))
    model = bind_tools_safely(base_model, tools, parallel=parallel_tools)

    messages = await _build_messages(state, ctx)
    max_steps = min(int(ctx.cfg("max_steps", 12) or 12), settings.max_agent_steps)
    total_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    transcript: list[dict[str, Any]] = []

    # @task 的返回值会进 checkpoint：人工审批导致节点重放时，
    # 之前已经完成的模型调用和工具执行不会重跑，也就不会重复计费。
    @task
    async def llm_step(step: int, payload: list[BaseMessage]) -> BaseMessage:
        return await _invoke_streaming(model, payload, ctx, model_id)

    @task
    async def settle_step(payload: list[BaseMessage]) -> BaseMessage:
        # 用 base_model 而不是上面那个绑过工具的 model：收尾轮必须在**结构上**
        # 发不出工具调用，靠提示词说"别调工具"是约束不住的
        return await _invoke_streaming(base_model, payload, ctx, model_id)

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

        run_calls, deferred = split_tool_calls(tool_calls, parallel=parallel_tools)

        for call in run_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            call_id = call.get("id") or f"{ctx.node.id}-{step}-{name}"

            if name not in tool_map:
                messages.append(
                    ToolMessage(content=f"错误：没有名为 {name} 的工具", tool_call_id=call_id)
                )
                continue

            # 参数先过 schema 再决定要不要打扰人审批：参数就不对的调用没必要问人，
            # 而纠正过的参数必须是人在审批框里看到的那一份
            schema = args_model_of(tool_map[name])
            if schema is not None:
                try:
                    args, fix_note = prepare_args(schema, args)
                except ToolArgsError as e:
                    # 不执行，把"它接受什么"原样喂回去。模型下一步照着改就行，
                    # 这才是失败之后真正能自愈的重试——以前喂回去的是一句
                    # TypeError，里面既没有正确参数名也没有字段说明
                    ctx.emit(EventType.TOOL_ERROR, tool=name, call_id=call_id, error=str(e))
                    messages.append(ToolMessage(content=str(e), tool_call_id=call_id))
                    transcript.append({"tool": name, "args": args, "ok": False, "result": str(e)})
                    continue
                if fix_note:
                    ctx.emit(EventType.LOG, level="warn",
                             message=f"工具 {name}：{fix_note}", code="tool_args_fixed")

            if _needs_approval(ctx, tool_map[name], args):
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
                verdict = read_decision(decision)
                note = verdict.note
                ctx.emit(EventType.HUMAN_RESOLVED, tool=name, approved=verdict.approved, note=note)
                if not verdict.approved:
                    messages.append(
                        ToolMessage(
                            content=f"用户拒绝了这次调用。原因：{note or '未说明'}。请换一种方式。",
                            tool_call_id=call_id,
                        )
                    )
                    transcript.append({"tool": name, "args": args, "denied": True, "note": note})
                    continue
                if verdict.args:
                    args = verdict.args

            ctx.emit(EventType.TOOL_START, tool=name, args=args, call_id=call_id)
            started = time.perf_counter()
            try:
                content = await tool_step(step, name, args)
                ok = True
            except Exception as e:  # noqa: BLE001 - 工具失败要喂回模型，让它自己纠错
                content = f"工具执行失败：{type(e).__name__}: {e}"
                if isinstance(e, TypeError) and schema is not None:
                    # 签名对不上还能走到这儿，说明 schema 和函数本身不一致（工具的 bug）。
                    # 模型改不了这个，但把参数表摊开至少让它别在同一个地方反复试
                    content += f"\n{describe_args(schema, args)}"
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

        # 没跑的那些排在执行过的后面，保持模型原本的调用顺序
        for call, message in deferred:
            messages.append(message)
            transcript.append(
                {"tool": call.get("name", ""), "args": call.get("args", {}) or {}, "skipped": True}
            )
    else:
        # 步数用完了。走到这儿一定意味着最后那步**要求了工具**（不要工具会 break），
        # 也就是说模型从来没拿到过"说结论"的那一轮——它只是被掐断在半路。
        #
        # 所以先补上那一轮：不给工具，让它基于已经查到的东西收口。这比把中间
        # 过程当答案交出去强得多，也比只留一句抱怨强——用户要的是"已知什么、
        # 还缺什么"，不是"系统哪里不够用"。
        settled = ""
        messages.append(HumanMessage(content=(
            f"步数预算用完了（{max_steps} 步），现在起不能再调用任何工具。"
            "基于已经查到的信息给出结论；明确说清哪些部分没有查到、"
            "结论因此有什么局限。不要编造没查到的数据。"
        )))
        try:
            response = await settle_step(messages)
            usage = _usage_of(response, model_id)
            for key in ("input_tokens", "output_tokens", "calls"):
                total_usage[key] += usage[key]
            total_usage["cost_usd"] = round(total_usage["cost_usd"] + usage["cost_usd"], 6)
            settled = message_text(response).strip()
        except Exception as e:  # noqa: BLE001 - 收尾失败不能把已有成果一起赔进去
            ctx.emit(EventType.LOG, level="warn",
                     message=f"收尾轮没跑成：{type(e).__name__}: {e}", code="settle_failed")

        if settled:
            final_text = settled
            hint = (
                f"agent 用满了 {max_steps} 步，这个结论是基于已经查到的部分给出的。"
                "要跑全请把节点上的「最大步数」调大；如果它大部分步数花在逐张表"
                "查结构上，也可以在提示词里点明该查哪几张表。"
            )
            # 和 step_limit 分开发：库里那些老事件的含义确实是"硬截断"，
            # 复用同一个 code 会把历史运行重新解释成另一回事
            ctx.emit(EventType.LOG, level="warn", message=hint, code="step_limit_settled")
        else:
            # 收尾轮也没说出话。成果只能取模型自己说过的话——messages[-1] 很可能
            # 是一条 ToolMessage：工具的原始返回，或者串行模式下那句"本轮只执行了
            # 第一个工具"。后者真的漏到用户面前当过答案（run dd9927e6），一句内部
            # 管道文案冒充结论，比明说"没跑完"糟糕得多。
            final_text = next(
                (text for m in reversed(messages)
                 if isinstance(m, AIMessage) and (text := message_text(m).strip())),
                "",
            )
            hint = (
                f"agent 用满了 {max_steps} 步还没给出结论。"
                "把节点上的「最大步数」调大；如果它大部分步数花在逐张表查结构上，"
                "也可以在提示词里点明该查哪几张表。"
            )
            # 一步一工具时步数消耗得比并行快得多，这里不说清楚，用户只会看到
            # 一个没头没尾的答案，而不知道是被步数掐断的
            final_text = f"{final_text}\n\n（{hint}）".strip() if final_text else f"（{hint}）"
            ctx.emit(EventType.LOG, level="warn", message=hint, code="step_limit")

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
