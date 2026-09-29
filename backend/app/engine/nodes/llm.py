from __future__ import annotations

import json
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.func import task
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.core.errors import describe_exception, raw_detail
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Skill
from app.engine.approval import read_decision
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import (
    cell_eid,
    ledger_enabled,
    make_eid,
    next_exec,
    query_entry_fields,
    schema_ledger_entry,
)
from app.engine.expressions import CellError, cell_value, column_kind, locate_cell, numeric_text, same_value
from app.engine.guards import FALLBACK_CONTEXT, Guard, legacy_hint, node_limits
from app.engine.replay import ask, once
from app.engine.state import GraphState, message_text, template_context, thinking_text
from app.engine.toolcalls import (
    TOOL_MARKUP_ERROR,
    TOOL_MARKUP_NUDGE,
    ToolTimeout,
    leaked_markup,
    limit_fields,
    limit_of,
    markup_warning,
    run_bounded,
)
from app.providers import catalog
from app.providers.factory import (
    ModelSpec,
    ProviderNotConfigured,
    bind_tools_safely,
    get_chat_model,
)
from app.tools.datasource import QUERY_PREFIX
from app.tools.registry import (
    ToolArgsError,
    ToolBuildError,
    ToolContext,
    args_model_of,
    build_tools,
    describe_args,
    prepare_args,
)
from app.tools.trust import asks_trustable, ask_gate, call_policy, node_task, trust_key_of

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


def _report_call(ctx: NodeContext, response: BaseMessage, model_id: str, started: float) -> dict[str, Any]:
    """一次模型调用一条 llm.end。返回这一次的用量。"""
    usage = _usage_of(response, model_id)
    ctx.emit(EventType.LLM_END, model=model_id,
             duration_ms=int((time.perf_counter() - started) * 1000), **usage)
    return usage


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
    return (f"调用模型「{model_id}」失败：{describe_exception(exc)}。"
            "稍后重试；反复失败就去「设置 → 模型接入」测一下这个模型")


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
    #: 这个节点里每次模型调用的用量。正常只有一次；模型把工具调用写成文字时会多一次纠正
    spent: list[dict[str, Any]] = []

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
            raise NodeError(
                ctx.node.id,
                f"模型没能按「结构化输出 Schema」给出结果：{describe_exception(e)}。"
                "检查 Schema 是否写对，或者换一个支持结构化输出的模型",
            ) from e
        payload = value if isinstance(value, (dict, list)) else getattr(value, "model_dump", lambda: value)()
        output = {"data": payload, "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        response: BaseMessage = AIMessage(content=output["text"])
        spent.append(_report_call(ctx, response, model_id, started))
    else:
        response = await _invoke_streaming(model, messages, ctx, model_id)
        spent.append(_report_call(ctx, response, model_id, started))
        text = message_text(response)
        if snippet := leaked_markup(text):
            # 模型把工具调用当正文写了出来：这个节点压根没有工具，那段"调用"什么都没查到。
            # 带一句纠正再问一次；还这样就判失败，别让一堆标记当答案往下游走
            ctx.emit(EventType.LOG, level="warn", code="tool_markup_leak",
                     message=f"{markup_warning(snippet)}，已提醒它重试一次")
            retried = time.perf_counter()
            response = await _invoke_streaming(
                model, [*messages, response, HumanMessage(content=TOOL_MARKUP_NUDGE)], ctx, model_id)
            spent.append(_report_call(ctx, response, model_id, retried))
            text = message_text(response)
            if leaked_markup(text):
                raise NodeError(ctx.node.id, TOOL_MARKUP_ERROR)
        reasoning = thinking_text(response)
        if not text and reasoning:
            # 思考型模型把额度全花在推理上了，正文是空的 —— 说清楚而不是抛个空结果给下游
            ctx.emit(EventType.LOG, level="warn",
                     message="模型只输出了思考内容，正文为空。把 max_tokens 调大，或把 thinking 设为 off。",
                     code="empty_completion")
        output = {"text": text, "thinking": reasoning}

    elapsed = int((time.perf_counter() - started) * 1000)
    usage = {key: sum(one[key] for one in spent) for key in spent[0]}
    usage["cost_usd"] = round(usage["cost_usd"], 6)

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
        try:
            return await build_tools(names, tool_ctx, session=session)
        except ToolBuildError as e:
            # 绑的工具自己坏了（参数定义写坏的自定义工具）：说清是哪个、去哪里改
            raise NodeError(ctx.node.id, str(e)) from e


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


def _guard(ctx: NodeContext, model_id: str) -> Guard | None:
    """这个节点的护栏。升级前发起的运行（没有快照）返回 None，走旧逻辑。"""
    if ctx.run.agent_limits is None:
        return None
    limits = node_limits(
        {k: ctx.cfg(k) for k in ("max_steps", "budget_tokens", "budget_usd")},
        ctx.run.agent_limits, settings.max_agent_steps,
    )
    return Guard(limits=limits, window=catalog.find_context(model_id) or FALLBACK_CONTEXT)


def _policy(ctx: NodeContext, tool: BaseTool, args: dict[str, Any], granted: set[str]) -> str:
    """这一次调用怎么放：safe 直接跑 / gate 先问门控模型 / ask 问人。"""
    mode = ctx.approval_mode()  # never | dangerous | always
    if mode == "always":
        return "ask"
    if mode == "never":
        return "safe"
    policy = call_policy(tool, tool.name, args, ctx.run.tool_trust)
    # 本节点里点过「始终允许」的工具，后面的调用改由门控把关。这个集合只从本节点
    # 重放出来的审批答复里长出来，重放时照样长到同一个位置，不会让 interrupt 错号
    if policy == "ask" and trust_key_of(tool) in granted:
        return "gate"
    return policy


async def run_agent(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """ReAct 循环：模型想调工具就调，拿到结果继续想，直到给出最终答案。

    这里没有用 prebuilt 的 create_react_agent，因为需要在循环内部做三件它不做的事：
    每步工具调用都往画布推事件、对危险工具逐个弹人工审批、以及步数护栏。

    人工审批恢复时整个节点从头重放。模型调用和工具调用都包在 @task 里，重放时结果
    取自 checkpoint，不重复计费、不重复执行；它们的事件也发在 task 里面，于是轨迹里
    同一次调用只出现一回。写在 task 外面的事件一律经 replay.once 发，道理相同。
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
    # 护栏（engine/guards.py）。None 是升级前发起的运行：默认 12 步、没有别的护栏，和以前一样
    guard = _guard(ctx, model_id)
    # 证据台账、工具结果前的编号、cite_fields：升级前发起的运行一样都不加（见 evidence.ledger_enabled）
    evidence_on = ledger_enabled(ctx.run)
    exec_no = next_exec(state, ctx.node.id)
    #: 本节点查成功的库：编号 Q1… 只在本节点内有效，cite_fields 抽取靠它找快照
    queries: list[dict[str, Any]] = []
    #: 要记进证据台账的条目，按调用顺序
    entries: list[dict[str, Any]] = []
    max_steps = (guard.limits.max_steps if guard
                 else min(int(ctx.cfg("max_steps", 12) or 12), settings.max_agent_steps))
    total_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    transcript: list[dict[str, Any]] = []

    def _report(response: BaseMessage, started: float) -> None:
        # 每次模型调用一条 llm.end：用量在节点跑的过程中就看得到
        ctx.emit(EventType.LLM_END, agent=ctx.node.title, model=model_id,
                 duration_ms=int((time.perf_counter() - started) * 1000),
                 **_usage_of(response, model_id))

    def _count(response: BaseMessage) -> None:
        usage = _usage_of(response, model_id)
        for key in ("input_tokens", "output_tokens", "calls"):
            total_usage[key] += usage[key]
        total_usage["cost_usd"] = round(total_usage["cost_usd"] + usage["cost_usd"], 6)

    # @task 的返回值会进 checkpoint：人工审批导致节点重放时，
    # 之前已经完成的模型调用和工具执行不会重跑，也就不会重复计费。
    @task
    async def llm_step(step: int, payload: list[BaseMessage]) -> BaseMessage:
        started = time.perf_counter()
        response = await _invoke_streaming(model, payload, ctx, model_id)
        _report(response, started)
        return response

    @task
    async def settle_step(payload: list[BaseMessage]) -> BaseMessage:
        # 用 base_model 而不是上面那个绑过工具的 model：收尾轮必须在**结构上**
        # 发不出工具调用，靠提示词说"别调工具"是约束不住的
        started = time.perf_counter()
        response = await _invoke_streaming(base_model, payload, ctx, model_id)
        _report(response, started)
        return response

    @task
    async def tool_step(step: int, name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
        # 失败也在这里收住、作为结果返回：失败的那次同样只执行一回，重放时不再重试
        tool = tool_map[name]
        limit = limit_of(tool, name, args)
        ctx.emit(EventType.TOOL_START, tool=name, args=args, call_id=call_id, **limit_fields(limit))
        started = time.perf_counter()
        extra: dict[str, Any] = {}
        try:
            result = await run_bounded(tool.ainvoke(args), limit, name)
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            ok = True
        except ToolTimeout as e:
            # 到点就收回控制权，驱动的收尾在后台做完。模型拿到的是"等了多久、为什么放弃"
            content, ok = str(e), False
            extra = {"error": str(e), "timed_out": True}
        except Exception as e:  # noqa: BLE001 - 工具失败要喂回模型，让它自己纠错
            content = f"工具执行失败：{describe_exception(e)}"
            schema = args_model_of(tool)
            if isinstance(e, TypeError) and schema is not None:
                # 签名对不上还能走到这儿，说明 schema 和函数本身不一致（工具的 bug）。
                # 模型改不了这个，但把参数表摊开至少让它别在同一个地方反复试
                content += f"\n{describe_args(schema, args)}"
            ok = False
            extra = {"error": content.splitlines()[0][:300], "detail": raw_detail(e)}
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
        # 查库成功时工具交回的 JSON 里带着查询快照的工件 id：记进返回值（进 checkpoint，重放拿到的
        # 是同一份）和 tool.end（在封存范围内，证据接口从这里出发找快照）。只认数据源工具：那个 id 是
        # 我们自己落的；MCP、自定义工具的文字来自外面，拼一个同样形状的 JSON 就能把任意快照塞进封存范围
        query = query_entry_fields(content) if evidence_on and ok and name.startswith(QUERY_PREFIX) else None
        ctx.emit(
            EventType.TOOL_END if ok else EventType.TOOL_ERROR,
            tool=name,
            call_id=call_id,
            duration_ms=elapsed,
            preview=content[:2000],
            artifact=snapshot_id,
            **({"query_artifact": query["artifact"]} if query else {}),
            # 查询当时数据源的表结构快照：和查询快照一样落在封存范围内，证据接口从这里认它
            **({"schema_artifact": query["schema_artifact"]} if query and query.get("schema_artifact") else {}),
            **extra,
        )
        outcome: dict[str, Any] = {"content": content, "ok": ok, "duration_ms": elapsed}
        if evidence_on:
            outcome.update(snapshot=snapshot_id, query_artifact=query["artifact"] if query else None)
        return outcome

    @task
    async def gate_step(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
        # 门控的判定进 checkpoint：审批恢复重放时不再问一遍。再问一遍可能判得不一样，
        # 后面的 interrupt 就对错号了
        gate = await ask_gate(tool=tool_map[name], args=args, node_title=ctx.node.title,
                              task=node_task(ctx.config))
        ctx.emit(EventType.TOOL_GATED, tool=name, call_id=call_id, **gate.event())
        return {"allowed": gate.allowed, **gate.usage}

    @task
    async def extract_step(payload: list[BaseMessage], schema: dict[str, Any]) -> dict[str, Any]:
        # cite_fields 的结构化抽取：进 checkpoint，节点重放时不再调用、不再计费。失败也在这里收住、
        # 作为结果返回——agent 的结论还在，字段记空值、发警告，而不是把整个节点赔进去。
        # thinking 关掉：结构化输出靠强制工具调用，和思考模式不兼容，抽取也用不着思考
        spec = model_spec.model_copy(update={
            "thinking": "off", "max_tokens": max(int(ctx.cfg("max_tokens") or 0), 4096)})
        started = time.perf_counter()
        ctx.emit(EventType.LLM_START, model=model_id, structured=True, purpose="cite_fields",
                 message_count=len(payload))
        try:
            async with SessionLocal() as session:
                extractor, _ = await get_chat_model(session, spec)
            got = await extractor.with_structured_output(schema, include_raw=True).ainvoke(payload)
        except Exception as e:  # noqa: BLE001
            ctx.emit(EventType.LLM_END, agent=ctx.node.title, model=model_id, purpose="cite_fields",
                     duration_ms=int((time.perf_counter() - started) * 1000), error=describe_exception(e))
            return {"error": f"结构化抽取没跑成：{explain_model_error(e, model_id)}"}
        raw, parsed = _split_structured(got)
        usage = _usage_of(raw if raw is not None else AIMessage(content=""), model_id)
        ctx.emit(EventType.LLM_END, agent=ctx.node.title, model=model_id, purpose="cite_fields",
                 duration_ms=int((time.perf_counter() - started) * 1000), **usage)
        return {"parsed": parsed, **usage}

    final_text = ""
    #: 模型把工具调用写成了文字：纠正过一次了没有
    nudged = False
    #: 本节点里审批时点了「始终允许」的工具（trust_key）
    granted: set[str] = set()
    #: 为什么没等到模型自己给结论：steps / stall / budget_tokens / budget_usd / context
    settle_reason: str | None = None
    answered = False
    for step in range(max_steps):
        response = await llm_step(step, messages)
        if guard:
            guard.observe(response, messages)
        messages.append(response)
        _count(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = message_text(response)
            if snippet := leaked_markup(final_text):
                # 没有真实的调用，正文里却是一段调用标记：这一步什么都没查到。
                # 纠正一次；再来一次就是这个节点/模型根本调不了工具，判失败
                if nudged:
                    raise NodeError(ctx.node.id, TOOL_MARKUP_ERROR)
                nudged = True
                await once(ctx, EventType.LOG, level="warn", code="tool_markup_leak",
                           message=f"{markup_warning(snippet)}，已提醒它重试一次")
                messages.append(HumanMessage(content=TOOL_MARKUP_NUDGE))
                final_text = ""
                continue
            answered = True
            break

        run_calls, deferred = split_tool_calls(tool_calls, parallel=parallel_tools)
        #: 这一步有没有拿到新信息：至少一个调用真的执行成功了（复用上次结果的不算）
        progressed = False

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
                    await once(ctx, EventType.TOOL_ERROR, tool=name, call_id=call_id, error=str(e))
                    messages.append(ToolMessage(content=str(e), tool_call_id=call_id))
                    transcript.append({"tool": name, "args": args, "ok": False, "result": str(e)})
                    continue
                if fix_note:
                    await once(ctx, EventType.LOG, level="warn",
                               message=f"工具 {name}：{fix_note}", code="tool_args_fixed")

            if guard and (again := guard.repeat(name, args)) is not None:
                # 同样的调用已经成功跑过：不再执行（也就不用再审批），把上次的结果交还给它
                await once(ctx, EventType.LOG, level="info", code="tool_repeat",
                           message=f"{name} 用同样的参数又调了一次，没有再执行，把上次的结果交还给它")
                messages.append(ToolMessage(content=again, tool_call_id=call_id))
                transcript.append({"tool": name, "args": args, "repeat": True})
                continue

            policy = _policy(ctx, tool_map[name], args, granted)
            if policy == "gate":
                gated = await gate_step(name, args, call_id)
                for key in ("input_tokens", "output_tokens"):
                    total_usage[key] += int(gated.get(key) or 0)
                total_usage["cost_usd"] = round(total_usage["cost_usd"] + float(gated.get("cost_usd") or 0), 6)
                policy = "safe" if gated["allowed"] else "ask"
            if policy == "ask":
                trust_key = (asks_trustable(tool_map[name], ctx.run.tool_trust)
                             if ctx.approval_mode() == "dangerous" else None)
                trustable = {"trust_key": trust_key} if trust_key else {}
                decision = await ask(
                    ctx,
                    {"kind": "tool_approval", "node_id": ctx.node.id, "tool": name, "args": args,
                     "title": f"是否允许调用 {name}？", **trustable},
                    mode="approve", tool=name, args=args, title=f"Agent 想调用工具 {name}", **trustable,
                )
                verdict = read_decision(decision)
                note = verdict.note
                always = verdict.always and bool(trust_key)
                if always:
                    granted.add(trust_key)
                await once(ctx, EventType.HUMAN_RESOLVED, tool=name, approved=verdict.approved,
                           note=note, actor=ctx.actor(), **({"always": True} if always else {}))
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

            outcome = await tool_step(step, name, args, call_id)
            content = outcome["content"]
            record = {"tool": name, "args": args, "ok": outcome["ok"],
                      "duration_ms": outcome["duration_ms"], "result": content[:4000]}
            if evidence_on:
                # 编号只取决于 tool_step 的返回值（重放时取自 checkpoint），重放出来的还是这一套
                content = _record_call(ctx.node.id, exec_no, call_id, name, args, outcome, record,
                                       queries, entries)
            transcript.append(record)
            messages.append(ToolMessage(content=content, tool_call_id=call_id))
            if guard:
                guard.record(step, name, args, outcome["ok"], content)
            progressed = progressed or outcome["ok"]

        # 没跑的那些排在执行过的后面，保持模型原本的调用顺序
        for call, message in deferred:
            messages.append(message)
            transcript.append(
                {"tool": call.get("name", ""), "args": call.get("args", {}) or {}, "skipped": True}
            )

        if guard:
            guard.step_done(progressed)
            squeezed = guard.compress(messages)
            if squeezed:
                await once(ctx, EventType.LOG, level="info", code="context_compressed",
                           message=f"对话用到上下文窗口的 {guard.last_input * 100 // guard.window}%，"
                                   f"已压缩 {squeezed} 条早期的工具结果")
            # 刚压缩过的这一步不按上下文收尾：压缩的效果要到下一次调用才看得出来
            if settle_reason := guard.stop_reason(total_usage, context=not squeezed):
                break
            if note := guard.reminder(step, total_usage):
                messages.append(HumanMessage(content=note))

    if not answered and settle_reason is None:
        # 步数用完了：最后那步要么要求了工具，要么是纠正之后还没来得及答
        settle_reason = "steps"

    if settle_reason:
        # 模型从来没拿到过"说结论"的那一轮——它只是被掐断在半路。
        #
        # 所以先补上那一轮：不给工具，让它基于已经查到的东西收口。这比把中间
        # 过程当答案交出去强得多，也比只留一句抱怨强——用户要的是"已知什么、
        # 还缺什么"，不是"系统哪里不够用"。
        to_model, to_user = (guard.describe(settle_reason, total_usage) if guard
                             else (f"步数预算用完了（{max_steps} 步）", ""))
        settled = ""
        messages.append(HumanMessage(content=(
            f"{to_model}，现在起不能再调用任何工具。"
            "基于已经查到的信息给出结论；明确说清哪些部分没有查到、"
            "结论因此有什么局限。不要编造没查到的数据。"
        )))
        try:
            response = await settle_step(messages)
            _count(response)
            settled = message_text(response).strip()
        except Exception as e:  # noqa: BLE001 - 收尾失败不能把已有成果一起赔进去
            await once(ctx, EventType.LOG, level="warn",
                       message=f"收尾轮没跑成：{describe_exception(e)}", code="settle_failed")

        if snippet := leaked_markup(settled):
            if not any(isinstance(m, AIMessage) and getattr(m, "tool_calls", None) for m in messages):
                # 从头到尾一次真调用都没有，步数全耗在写标记上：还是那个「调不了工具」
                raise NodeError(ctx.node.id, TOOL_MARKUP_ERROR)
            # 收尾轮拿的是没绑工具的模型，它还是写了一段调用标记：想接着查、没能收口。
            # 这不是「节点没绑工具」（前面的步数里工具是真调过的），不能套 TOOL_MARKUP_ERROR
            # 判失败、把已经查到的一起丢掉——按收尾轮没说出话处理，取它之前说过的话
            await once(ctx, EventType.LOG, level="warn", code="tool_markup_leak",
                       message=f"{markup_warning(snippet)}：步数用完后的收尾轮仍想调用工具，"
                               "没能给出结论")
            settled = ""

        limited = {"reason": settle_reason} if guard else {}
        if settled:
            final_text = settled
            hint = f"{to_user}这个结论是基于已经查到的部分给出的。" if guard else legacy_hint(max_steps, True)
            # 和 step_limit 分开发：库里那些老事件的含义确实是"硬截断"，
            # 复用同一个 code 会把历史运行重新解释成另一回事
            ctx.emit(EventType.LOG, level="warn", message=hint, code="step_limit_settled", **limited)
        else:
            # 收尾轮也没说出话。成果只能取模型自己说过的话——messages[-1] 很可能
            # 是一条 ToolMessage：工具的原始返回，或者串行模式下那句"本轮只执行了
            # 第一个工具"。后者真的漏到用户面前当过答案（run dd9927e6），一句内部
            # 管道文案冒充结论，比明说"没跑完"糟糕得多。
            final_text = next(
                (text for m in reversed(messages)
                 if isinstance(m, AIMessage) and (text := message_text(m).strip())
                 and not leaked_markup(text)),
                "",
            )
            hint = f"{to_user}收尾轮也没有给出结论。" if guard else legacy_hint(max_steps, False)
            # 一步一工具时步数消耗得比并行快得多，这里不说清楚，用户只会看到
            # 一个没头没尾的答案，而不知道是被步数掐断的
            final_text = f"{final_text}\n\n（{hint}）".strip() if final_text else f"（{hint}）"
            ctx.emit(EventType.LOG, level="warn", message=hint, code="step_limit", **limited)

    cited: tuple[dict[str, Any], dict[str, Any]] | None = None
    schema = _output_schema(ctx) if evidence_on and ctx.cfg("cite_fields") is True else None
    if schema is not None and (fields := cited_fields(schema)):
        # 结构化抽取只能追加在循环之后：循环里的 task 按调用位置从 checkpoint 取回，插一个进去，
        # 停在审批上的运行恢复时位置就错开了
        extracted: dict[str, Any] = {"error": "这个节点没有查成功的库，没有可以核对出处的数据"}
        if queries:
            extracted = await extract_step(_extract_messages(final_text, queries, fields), cited_schema(schema))
            for key in ("input_tokens", "output_tokens", "calls"):
                total_usage[key] += int(extracted.get(key) or 0)
            total_usage["cost_usd"] = round(total_usage["cost_usd"] + float(extracted.get("cost_usd") or 0), 6)
        cited = verify_cited_fields(extracted, fields, queries)
        for code, message, names in _field_warnings(*cited, extracted.get("error")):
            await once(ctx, EventType.LOG, level="warn", code=code, message=message, fields=names)

    total_usage["total_tokens"] = total_usage["input_tokens"] + total_usage["output_tokens"]
    result = {
        "text": final_text,
        "steps": len(transcript),
        "tool_calls": transcript,
        "model": model_id,
        # 没等到模型自己给结论就收了尾：下游校验失败时拿它说明根源（human.py）
        **({"limited": settle_reason} if settle_reason else {}),
    }
    if cited is not None:
        result["data"], result["data_evidence"] = cited
    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": total_usage,
    }
    if entries:
        updates["evidence"] = entries
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=final_text or "(空)")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        # 开了 cite_fields 的，变量里是核对过的 data（值以快照为准），下游口径卡按字段取
        updates["vars"] = {var_name: cited[0] if cited is not None else final_text}
    return updates


# --------------------------------------------------------------------------
# 证据台账与 cite_fields：agent 查到的数怎么变成能核对的证据
# --------------------------------------------------------------------------


def _record_call(node_id: str, exec_no: int, call_id: str, name: str, args: dict[str, Any],
                 outcome: dict[str, Any], record: dict[str, Any], queries: list[dict[str, Any]],
                 entries: list[dict[str, Any]]) -> str:
    """把一次执行过的工具调用记进台账、调用记录和本节点的查询清单，返回给模型看的内容。

    查库成功的结果前面加一行「【证据 Q1】」：模型在循环里就知道每次查询的编号，cite_fields
    抽取时照这个编号写出处。编号只在本节点内有效；报告目录里的 Q1… 按全局台账另编，两边
    靠工件 id 对应，不靠编号。
    """
    content = outcome["content"]
    record["call_id"] = call_id
    snapshot, artifact = outcome.get("snapshot"), outcome.get("query_artifact")
    if snapshot:
        record["via"] = snapshot
        entries.append({"kind": "tool", "node_id": node_id, "exec": exec_no, "artifact": snapshot,
                        "call_id": call_id, "tool": name})
    if not artifact or not name.startswith(QUERY_PREFIX):
        return content
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    fields = query_entry_fields(data)
    if fields is None:
        return content
    alias = f"Q{len(queries) + 1}"
    record.update(alias=alias, artifact=artifact)
    # 表结构快照：同一件在本节点这次执行里只记一条 schema 条目。快照是内容寻址的，重放时读到的是同一份
    schema = fields.get("schema_artifact")
    seen = schema and any(e.get("kind") == "schema" and e.get("artifact") == schema for e in entries)
    frozen = None if not schema or seen else schema_ledger_entry(schema, node_id=node_id, exec_no=exec_no)
    shaped = {"schema_artifact": schema, "tables": fields["tables"]} if seen or frozen else {}
    entries.append({"kind": "query", "node_id": node_id, "exec": exec_no, "artifact": artifact,
                    **({"via": snapshot} if snapshot else {}), "call_id": call_id, "tool": name,
                    "source": fields["source"], "columns": fields["columns"], "rows": fields["rows"],
                    "truncated": fields["truncated"], **shaped})
    if frozen:
        entries.append(frozen)
    queries.append({"alias": alias, "call_id": call_id, "tool": name, "sql": str(args.get("sql") or ""),
                    "artifact": artifact, "via": snapshot, "columns": fields["columns"],
                    "rows": data.get("rows") or [], "truncated": fields["truncated"]})
    return f"【证据 {alias}】\n{content}"


def _output_schema(ctx: NodeContext) -> dict[str, Any] | None:
    """节点的 output_schema（对象形状）。画布里存成 JSON 文本的也认。"""
    schema = ctx.cfg("output_schema")
    if isinstance(schema, str) and schema.strip():
        try:
            schema = json.loads(schema)
        except ValueError:
            return None
    if isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema):
        return schema
    return None


def _is_object(schema: Any) -> bool:
    return isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema)


def cited_fields(schema: dict[str, Any], prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    """output_schema 里要标出处的字段：(点号路径, 字段的 schema)，按声明顺序，嵌套对象展开。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for key, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}{key}"
        if _is_object(sub):
            out.extend(cited_fields(sub, f"{path}."))
        else:
            out.append((path, sub if isinstance(sub, dict) else {}))
    return out


#: 一个值在哪次查询的哪一格
_FROM_CELL: dict[str, Any] = {
    "type": "object",
    "description": "这个值在哪次查询的哪一格：call 是【证据 Qn】里的编号，row 是行号（从 0 数），column 是列名",
    "properties": {"call": {"type": "string"}, "row": {"type": "integer"}, "column": {"type": "string"}},
    "required": ["call", "row", "column"],
}


def _is_array(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "array"


def _row_columns(schema: dict[str, Any]) -> list[str] | None:
    """数组里是对象时，对象的字段（都得是标量）；不是对象返回 None。"""
    items = schema.get("items")
    if not _is_object(items):
        return None
    return list((items.get("properties") or {}).keys())


def _from_rows(schema: dict[str, Any]) -> dict[str, Any]:
    """数组字段按行映射：一段连续的行，标量数组对应一列，对象数组的每个字段各对应一列。"""
    fields = _row_columns(schema)
    where = ({"columns": {"type": "object", "description": "对象的每个字段取自哪一列：{字段: 列名}",
                          "properties": {f: {"type": "string"} for f in fields}, "required": fields}}
             if fields is not None else {"column": {"type": "string"}})
    return {"type": "object",
            "description": "这组值在哪次查询的哪几行：call 是【证据 Qn】里的编号，rows 写行号范围（从 0 数，"
                           "如 0-4，数组第 k 个元素就是第 起始+k 行）",
            "properties": {"call": {"type": "string"}, "rows": {"type": "string"}, **where},
            "required": ["call", "rows", *where]}


def cited_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """抽取用的 schema：每个标量字段改写成 {value, from}，查不到时两个都是 null。

    数组字段整个是一个 {value, from}：from 写一段连续的行（rows: "0-4"）和对应的列。
    """

    def rewrite(node: dict[str, Any]) -> dict[str, Any]:
        if _is_object(node):
            props = node.get("properties") or {}
            return {"type": "object", **({"description": node["description"]} if node.get("description") else {}),
                    "properties": {k: rewrite(v if isinstance(v, dict) else {}) for k, v in props.items()},
                    "required": list(props)}
        source = _from_rows(node) if _is_array(node) else _FROM_CELL
        return {"type": "object",
                "properties": {"value": {"anyOf": [node, {"type": "null"}]},
                               "from": {"anyOf": [source, {"type": "null"}]}},
                "required": ["value", "from"]}

    # 走工具调用的结构化输出要求函数名只含字母、数字、下划线
    return {"title": "cited_fields", **rewrite(schema)}


EXTRACT_SYSTEM = (
    "你是数据抽取员：把智能体查到的数据按给定结构填好，每个字段都写出处。\n"
    "- value 是字段的值；from 写它在哪次查询的哪一格：call 是【证据 Qn】里的编号，row 是行号（从 0 数），"
    "column 是列名。\n"
    "- 只能用下面查询结果里真实出现的值。查不到就把 value 和 from 都填 null，禁止估算，"
    "禁止用 0 或空字符串代替没查到的值。\n"
    "- 不要做计算：比率、增幅、合计这类要算出来的量填 null，交给口径卡去算。"
)
#: 抽取提示里每次查询最多列出几行、整段最多多少字
_DIGEST_ROWS, _DIGEST_BUDGET = 50, 16000


def _digest(queries: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for q in queries:
        lines = [f"【证据 {q['alias']}】{q['tool']}"]
        if q["sql"]:
            lines.append(f"SQL：{q['sql'][:300]}")
        columns = q["columns"]
        lines.append(f"列：{', '.join(columns)}")
        for r, record in enumerate(q["rows"][:_DIGEST_ROWS]):
            values = record if isinstance(record, list) else [record.get(c) for c in columns] \
                if isinstance(record, dict) else [record]
            lines.append(f"r{r}：" + "，".join(f"{c}={json.dumps(v, ensure_ascii=False, default=str)}"
                                              for c, v in zip(columns, values)))
        if len(q["rows"]) > _DIGEST_ROWS:
            lines.append(f"…共 {len(q['rows'])} 行，只列出前 {_DIGEST_ROWS} 行")
        elif not q["rows"]:
            lines.append("（0 行）")
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    return text if len(text) <= _DIGEST_BUDGET else text[:_DIGEST_BUDGET] + "\n…（查询结果太长，后面的省略了）"


def _extract_messages(final_text: str, queries: list[dict[str, Any]],
                      fields: list[tuple[str, dict[str, Any]]]) -> list[BaseMessage]:
    wanted = "\n".join(
        f"- {path}（{sub.get('type') or '任意'}）" + (f"：{sub['description']}" if sub.get("description") else "")
        for path, sub in fields)
    return [
        SystemMessage(content=EXTRACT_SYSTEM),
        HumanMessage(content=(
            f"智能体的结论（只作参考，数以查询结果为准）：\n{final_text or '（没有结论）'}\n\n"
            f"查询结果：\n{_digest(queries)}\n\n要填的字段：\n{wanted}"
        )),
    ]


def _split_structured(got: Any) -> tuple[BaseMessage | None, Any]:
    """with_structured_output(include_raw=True) 的结果拆成 (原始回复, 解析结果)。

    不认 include_raw 的模型（比如 mock）直接给解析结果。
    """
    if isinstance(got, dict) and isinstance(got.get("raw"), BaseMessage) and "parsed" in got:
        raw, parsed = got["raw"], got["parsed"]
    else:
        raw, parsed = None, got
    if parsed is not None and not isinstance(parsed, (dict, list)) and hasattr(parsed, "model_dump"):
        parsed = parsed.model_dump()
    return raw, parsed


def _typed(raw: Any, schema: Any, kind: str | None = None) -> Any:
    """快照里的一格 → 按字段声明的类型给出的值（data 里放的就是它）。

    数据源把 DECIMAL 落成文本，小数位为 0 的是 "45678"：声明成 number / integer 的换成数，下游口径卡
    才能拿它做除法；声明成 string 的原样是文本，"2026" 不会变成 2026。没声明类型的按 cell_value，
    kind（查询时记下的列类型）是 text 的原样是文本；老快照没记类型就按值猜。
    声明成 integer 而快照里是 3.0 这样的整值，给 3；是 45678.5 就照实给 45678.5，不替它取整。
    """
    declared = schema.get("type") if isinstance(schema, dict) else None
    types = {declared} if isinstance(declared, str) else set(declared) if isinstance(declared, list) else set()
    value = cell_value(raw, kind)
    if types & {"number", "integer"}:
        loose = numeric_text(raw, loose=True)
        value = value if loose is None else loose
        if "integer" in types and isinstance(value, float) and value.is_integer():
            value = int(value)
    elif "string" in types and isinstance(raw, str):
        value = raw
    return value


def _dig(data: Any, path: str) -> Any:
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _plant(data: dict[str, Any], path: str, value: Any) -> None:
    *heads, last = path.split(".")
    for key in heads:
        data = data.setdefault(key, {})
    data[last] = value


def verify_cited_fields(extracted: dict[str, Any], fields: list[tuple[str, dict[str, Any]]],
                        queries: list[dict[str, Any]], loader: Any = None
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """确定性核对：按 from 取出快照里的那一格，以快照为准。返回 (data, data_evidence)。

    每个字段的结果是四种之一：
    - verified：和模型报的值一致，取快照的值
    - mismatch：不一致，取快照的值，model_value 留着模型报的
    - unresolved：出处取不到（没有这次查询、行列不存在、快照取不回来），值为 None，reason 写原因
    - missing：模型说查不到（from 为 null），值为 None
    值为 None 就是 None：永远不兜底成 0，也不采信没有出处的模型值。
    """
    load = loader or artifact_store.load
    by_alias = {q["alias"]: q for q in queries}
    snapshots: dict[str, tuple[Any, str | None]] = {}

    def snapshot_of(q: dict[str, Any]) -> tuple[Any, str | None]:
        if q["artifact"] not in snapshots:
            try:
                content = load(q["artifact"])
                snapshots[q["artifact"]] = (content, None if content is not None else "查询快照取不回来")
            except ValueError:
                snapshots[q["artifact"]] = (None, "查询快照和哈希对不上，疑似被改过")
        return snapshots[q["artifact"]]

    parsed = extracted.get("parsed")
    failed = extracted.get("error") or (None if isinstance(parsed, dict) else "抽取结果不是一个对象")
    data: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for path, sub in fields:
        _plant(data, path, None)
        if failed:
            evidence[path] = {"status": "unresolved", "reason": str(failed)}
            continue
        node = _dig(parsed, path)
        if not isinstance(node, dict) or "from" not in node:
            evidence[path] = {"status": "unresolved", "reason": "抽取结果里没有按 {value, from} 写出这个字段"}
            continue
        told, source = node.get("value"), node.get("from")
        if source is None:
            evidence[path] = {"ref": None, "status": "missing",
                              **({"model_value": told} if told is not None else {})}
            continue
        if _is_array(sub):
            value, evidence[path] = _verify_rows(sub, told, source, by_alias, snapshot_of)
            _plant(data, path, value)
            continue
        call = str(source.get("call") or "").strip() if isinstance(source, dict) else ""
        row, column = (source.get("row"), source.get("column")) if isinstance(source, dict) else (None, None)
        if isinstance(row, str) and row.strip().isdigit():
            row = int(row)      # 有的模型把整数写成 "0"：意思没有歧义
        elif isinstance(row, float) and row.is_integer():
            row = int(row)
        ref = f"{call}.r{row}.{column}" if call and isinstance(row, int) and column not in (None, "") else None
        q = by_alias.get(call)
        if q is None:
            evidence[path] = {"ref": ref, "call": call, "status": "unresolved",
                              "reason": f"本节点没有 {call or '（空）'} 这次查询，能用的是 "
                                        + ("、".join(by_alias) or "（没有）")}
            continue
        base = {"ref": ref, "call": call, "artifact": q["artifact"], "via": q["via"]}
        content, why = snapshot_of(q)
        if why:
            evidence[path] = {**base, "status": "unresolved", "reason": f"{call} 的{why}"}
            continue
        try:
            raw, name = locate_cell(content, row, column)
        except CellError as e:
            evidence[path] = {**base, "status": "unresolved", "reason": f"{call} {e}"}
            continue
        truth = _typed(raw, sub, column_kind(content, name))
        locator = {"row": row, "column": name}
        cited = {**base, "ref": f"{call}.r{row}.{name}", "locator": locator,
                 "eid": cell_eid(q["artifact"], row, name)}
        if same_value(told, truth):
            cited["status"] = "verified"
        else:
            cited.update(status="mismatch", model_value=told)
        _plant(data, path, truth)
        evidence[path] = cited
    return data, evidence


_ROWS = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def _verify_rows(schema: dict[str, Any], told: Any, source: Any, by_alias: dict[str, dict[str, Any]],
                 snapshot_of: Any) -> tuple[Any, dict[str, Any]]:
    """数组字段：按 from 的行范围和列从快照里取出整组值，以快照为准。返回 (值, 出处)。"""
    if not isinstance(source, dict):
        return None, {"status": "unresolved", "reason": "数组的 from 要写成 {call, rows, column（或 columns）}"}
    call = str(source.get("call") or "").strip()
    span = _ROWS.match(str(source.get("rows") or ""))
    fields = _row_columns(schema)
    wanted = source.get("columns") if fields is not None else source.get("column")
    if fields is not None and not (isinstance(wanted, dict) and all(isinstance(wanted.get(f), str) for f in fields)):
        return None, {"call": call, "status": "unresolved", "reason": "对象数组要写 columns：每个字段取自哪一列"}
    if fields is None and not isinstance(wanted, str):
        return None, {"call": call, "status": "unresolved", "reason": "数组要写 column：这组值取自哪一列"}
    if span is None:
        return None, {"call": call, "status": "unresolved", "reason": "rows 要写成 0-4 这样的行号范围（从 0 数）"}
    first, last = int(span.group(1)), int(span.group(2) or span.group(1))
    q = by_alias.get(call)
    if q is None or last < first:
        reason = (f"rows 要从小到大写，写的是 {source.get('rows')}" if q is not None else
                  f"本节点没有 {call or '（空）'} 这次查询，能用的是 " + ("、".join(by_alias) or "（没有）"))
        return None, {"call": call, "status": "unresolved", "reason": reason}
    base = {"call": call, "artifact": q["artifact"], "via": q["via"]}
    content, why = snapshot_of(q)
    if why:
        return None, {**base, "status": "unresolved", "reason": f"{call} 的{why}"}
    columns = {f: wanted[f] for f in fields} if fields is not None else {"": wanted}
    items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
    kinds = ({f: (items.get("properties") or {}).get(f) for f in fields} if fields is not None else {"": items})
    truth: list[Any] = []
    names: dict[str, str] = {}
    try:
        for r in range(first, last + 1):
            one = {}
            for f, col in columns.items():
                raw, names[f] = locate_cell(content, r, col)
                one[f] = _typed(raw, kinds[f], column_kind(content, names[f]))
            truth.append(one if fields is not None else one[""])
    except CellError as e:
        return None, {**base, "status": "unresolved", "reason": f"{call} {e}"}
    locator: dict[str, Any] = {"rows": [first, last],
                               **({"columns": names} if fields is not None else {"column": names[""]})}
    cited = {"ref": f"{call}.r{first}-{last}" + ("" if fields is not None else f".{names['']}"), **base,
             "locator": locator, "eid": make_eid("rows", q["artifact"], locator)}
    same = isinstance(told, list) and len(told) == len(truth) and all(
        (isinstance(m, dict) and all(same_value(m.get(f), t[f]) for f in t)) if isinstance(t, dict)
        else same_value(m, t)
        for m, t in zip(told, truth))
    cited.update({"status": "verified"} if same else {"status": "mismatch", "model_value": told})
    return truth, cited


def _shown(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text if len(text) <= 40 else text[:40] + "…"


def _field_warnings(data: dict[str, Any], evidence: dict[str, Any],
                    failed: str | None) -> list[tuple[str, str, list[str]]]:
    """核对结果里要让人看见的：(code, message, 字段)。mismatch 和 unresolved 各一条；missing 是模型
    照实说没查到，值已经是空的，不另发警告。"""
    out: list[tuple[str, str, list[str]]] = []
    mismatched = [(p, e) for p, e in evidence.items() if e.get("status") == "mismatch"]
    if mismatched:
        detail = "；".join(f"{p} 模型报 {_shown(e.get('model_value'))}，快照是 {_shown(_dig(data, p))}"
                          for p, e in mismatched[:6])
        out.append(("agent_field_mismatch",
                    f"有 {len(mismatched)} 个字段和查询快照对不上，已按快照取值：{detail}", [p for p, _ in mismatched]))
    unresolved = [(p, e) for p, e in evidence.items() if e.get("status") == "unresolved"]
    if unresolved:
        if failed:
            message = f"{failed}。{len(unresolved)} 个字段都记为空值（没有兜底成 0）"
        else:
            detail = "；".join(f"{p}（{e.get('reason')}）" for p, e in unresolved[:6])
            message = f"有 {len(unresolved)} 个字段核对不了出处，记为空值（没有兜底成 0）：{detail}"
        out.append(("agent_field_unverified", message, [p for p, _ in unresolved]))
    return out
