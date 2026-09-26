from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.func import task

from app.core.config import settings
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Workflow
from app.engine.context import NodeContext, NodeError
from app.engine.errors import describe_exception
from app.engine.state import GraphState, message_text, template_context
from app.providers import catalog
from app.providers.factory import ModelSpec, bind_tools_safely, get_chat_model
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    args_model_of,
    build_tools,
    call_is_dangerous,
    prepare_args,
)

_MAX_DEPTH = 3

#: 调度者在 llm.end 里的 agent 名。成员用各自的名字
COORDINATOR = "调度者"


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
    # 一轮可以派给多个专家。协议用 assignments 数组而不是单个 next：
    # 后者把"一次只能派一个"编进了协议本身，模型再想并行也表达不出来。
    #
    # 但并行只在任务**互不依赖**时才成立——并发的几个专家看到的是同一份进展
    # 快照，谁也看不见谁。派错了不会报错，只会让两个人基于同样的旧信息
    # 重复劳动，而这件事在结果里很难看出来。所以判断责任明确压在调度者身上，
    # 提示词里把反例写出来。
    route_schema = {
        "title": "route",  # langchain 靠 title 识别 JSON Schema dict
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "description": "这一轮要派出去的任务。互不依赖时可以给多个，它们会同时执行",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "enum": names},
                        "instruction": {"type": "string"},
                    },
                    "required": ["agent", "instruction"],
                },
            },
            "done": {"type": "boolean", "description": "目标已达成，结束协作"},
            "reason": {"type": "string"},
        },
        "required": ["assignments"],
    }

    #: 一轮最多同时派几个。再多的话调度者多半只是在把任务拆碎，
    #: 而每多一路就多一份"基于陈旧进展"的风险
    max_parallel = max(1, min(int(ctx.cfg("max_parallel", 3) or 3), len(names)))
    #: dangerous：需要人工确认的调用不执行（成员停不下来等人）；never：全部放行
    approval = ctx.approval_mode()

    transcript: list[dict[str, Any]] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}

    def _usage(msg: Any, model_id: str) -> dict[str, Any]:
        meta = getattr(msg, "usage_metadata", None) or {}
        i, o = int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
        return {"input_tokens": i, "output_tokens": o,
                "cost_usd": round(catalog.estimate_cost(model_id, i, o), 6)}

    def _accumulate(one: dict[str, Any]) -> None:
        usage_total["input_tokens"] += one["input_tokens"]
        usage_total["output_tokens"] += one["output_tokens"]
        usage_total["calls"] += 1
        usage_total["cost_usd"] = round(usage_total["cost_usd"] + one["cost_usd"], 6)

    def _report(who: str, model_id: str, one: dict[str, Any], t0: float) -> None:
        # 每次模型调用各发一条 llm.end，界面上的用量才能实时涨，而且分得清是谁花的。
        # 它只是把 usage_total 这笔账逐次摊开：run.finished 的 usage 仍以累计为准
        ctx.emit(EventType.LLM_END, agent=who, model=model_id,
                 duration_ms=int((time.perf_counter() - t0) * 1000), **one)

    @task
    async def route(round_no: int, progress: str) -> dict[str, Any]:
        prompt = (
            f"你是团队调度者。目标：\n{goal}\n\n"
            f"可用成员：\n{roster}\n\n"
            f"当前进展：\n{progress or '(还没有任何进展)'}\n\n"
            "决定这一轮把什么任务交给谁，每个任务给一句明确的指令。\n\n"
            f"**互不依赖的任务可以放在同一轮**（最多 {max_parallel} 个），它们会同时执行，"
            "总耗时按最慢的那个算。判断依据只有一条：**后一个任务需不需要看到前一个的结果**。\n"
            "  可以同时派：「查 A 产品的资料」和「查 B 产品的资料」——两边互不相干\n"
            "  不能同时派：「查资料」和「根据资料写报告」——后者要等前者\n"
            "  不能同时派：「写初稿」和「校对初稿」——同上\n\n"
            "同时派出去的成员看到的是**同一份进展快照**，谁也看不见谁这一轮干了什么。"
            "拿不准是否独立时就分两轮派，代价只是慢一点；派错了则是两个人基于同样的"
            "旧信息重复劳动，而这在结果里很难看出来。\n\n"
            "目标已经达成时，done 填 true、assignments 给空数组。"
        )
        t0 = time.perf_counter()
        spent = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        try:
            # include_raw：结构化结果之外把原始消息也要回来，用量在那条消息上。
            # 以前只拿解析结果，调度者的 token 一笔都没记进 usage
            out = await supervisor_model.with_structured_output(
                route_schema, include_raw=True).ainvoke(prompt)
            if isinstance(out, dict) and "raw" in out and "parsed" in out:
                spent = _usage(out["raw"], sup_model_id)
                value = out["parsed"]
                if value is None:
                    value = json.loads(message_text(out["raw"]))
            else:
                value = out
            if not isinstance(value, dict):
                value = json.loads(message_text(value))
        except Exception:  # noqa: BLE001
            # 不支持结构化输出的模型退回文本：从回复里认出一个成员名，按单人派
            reply = await supervisor_model.ainvoke(prompt)
            spent = _usage(reply, sup_model_id)
            raw = message_text(reply)
            picked = next((n for n in names if n in raw), None)
            value = ({"assignments": [{"agent": picked, "instruction": raw[:500]}]}
                     if picked else {"assignments": [], "done": True, "reason": raw[:200]})
        # 在 task 里发：节点重放时 task 结果取自 checkpoint，这条不会重复
        _report(COORDINATOR, sup_model_id, spent, t0)
        return {"decision": value, "usage": spent}

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
        # 和 agent 节点同一个硬顶：成员上配多少步都过不去 max_agent_steps
        max_steps = min(int(cfg.get("max_steps") or 4), settings.max_agent_steps)
        for _ in range(max_steps):
            t0 = time.perf_counter()
            reply = await bound.ainvoke(messages)
            spent = _usage(reply, model_id)
            _accumulate(spent)
            _report(name, model_id, spent, t0)
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
                                 message=f"工具 {tname}：{fix_note}", code="tool_args_fixed")
                    if args_error:
                        # 参数就不对，没必要真调一次。把"它接受什么"喂回去让它改
                        content = args_error
                    elif approval != "never" and call_is_dangerous(tool_map[tname], tname, targs):
                        # 专家们并行跑在同一个节点里，停不下来等人：审批恢复时节点整个
                        # 重放，几个人的 interrupt 谁先谁后对不上号。以前的做法是不审，
                        # shell_exec / file_write / 可写库上的 DELETE 照跑不误——
                        # agent 节点守着的那道门，在这里是敞开的。现在是不跑，并说清原因
                        content = (
                            f"没有执行：{tname} 这次调用需要人工确认，而协作团队里的成员"
                            "不能停下来等人。换一种不需要它的做法；实在需要，交给团队外"
                            "的 agent 节点去做（那里可以逐次审批）。"
                        )
                        ctx.emit(EventType.LOG, level="warn", code="tool_needs_approval",
                                 message=f"{name} 想调用 {tname}，需要人工确认，协作节点里不执行")
                    else:
                        try:
                            raw = await tool_map[tname].ainvoke(targs)
                            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                        except Exception as e:  # noqa: BLE001
                            content = f"工具失败：{describe_exception(e)}"
                ctx.emit(EventType.TOOL_END, tool=tname, agent=name, call_id=cid, preview=content[:1500])
                calls.append({"tool": tname, "args": targs, "result": content[:2000]})
                messages.append(ToolMessage(content=content, tool_call_id=cid))

        # 步数用完了：最后一步一定是要了工具的，模型从没拿到过"说结论"的那一轮。
        # messages[-1] 是一条 ToolMessage——以前它就被当成这个成员的产出交给调度者，
        # 工具的原始返回冒充了结论。agent 节点修过同一个问题，这里照做：不给工具
        # （用没绑工具的 model，结构上就发不出调用）补一轮收尾。
        messages.append(HumanMessage(content=(
            f"步数预算用完了（{max_steps} 步），现在起不能再调用任何工具。基于已经查到的"
            "信息给出你这部分的结论，说清哪些没查到、结论因此有什么局限。不要编造数据。"
        )))
        try:
            t0 = time.perf_counter()
            settled = await model.ainvoke(messages)
            spent = _usage(settled, model_id)
            _accumulate(spent)
            _report(name, model_id, spent, t0)
            text = message_text(settled).strip()
        except Exception as e:  # noqa: BLE001 - 收尾失败不能把已有的过程一起赔进去
            ctx.emit(EventType.LOG, level="warn", code="settle_failed",
                     message=f"{name} 的收尾轮没跑成：{describe_exception(e)}")
            text = next((t for m in reversed(messages)
                         if isinstance(m, AIMessage) and (t := message_text(m).strip())), "")
        ctx.emit(EventType.LOG, level="warn", code="step_limit_settled",
                 message=f"{name} 用满了 {max_steps} 步，结论基于已经查到的部分")
        return {"text": text or f"（{name} 用满了 {max_steps} 步，没有给出结论）", "tool_calls": calls}

    progress_lines: list[str] = []
    final_text = ""
    rounds_meta: list[dict[str, Any]] = []
    for round_no in range(max_rounds):
        progress = "\n\n".join(progress_lines)
        # 调度者决策前后各一条事件。它常常占掉团队节点大半的时间，以前是黑盒
        ctx.emit(EventType.AGENT_ROUTE_START, round=round_no)
        route_t0 = time.perf_counter()
        routed = await route(round_no, progress)
        route_ms = int((time.perf_counter() - route_t0) * 1000)
        decision = routed.get("decision", routed) if isinstance(routed, dict) else {}
        _accumulate(routed.get("usage") or _usage(None, sup_model_id))
        reason = str(decision.get("reason", ""))

        # 只留认得出的成员，并按上限截断。模型偶尔会重复派同一个人或者编一个
        # 不存在的名字——这两种在并发里都是纯浪费
        batch: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in decision.get("assignments") or []:
            name = str((item or {}).get("agent") or "")
            if name in agent_map and name not in seen:
                seen.add(name)
                batch.append((name, str(item.get("instruction") or goal)))
            if len(batch) >= max_parallel:
                break

        finishing = bool(decision.get("done")) or not batch
        ctx.emit(EventType.AGENT_ROUTE_END, round=round_no, duration_ms=route_ms,
                 agents=[] if finishing else [n for n, _ in batch],
                 parallel=0 if finishing else len(batch), done=finishing, reason=reason)

        if finishing:
            # agents / done 是结构化字段，界面照着它渲染。以前只有一句中文
            # 消息串，解码层得拿正则去拆「调度 → X（理由）」——文案一改就散架
            ctx.emit(EventType.LOG, level="info", round=round_no,
                     agents=[], done=True,
                     message="调度 → 结束协作" + (f"（{reason}）" if reason else ""))
            final_text = progress_lines[-1].split("】", 1)[-1] if progress_lines else ""
            break

        who = "、".join(n for n, _ in batch)
        ctx.emit(
            EventType.LOG, level="info", round=round_no, parallel=len(batch),
            agents=[n for n, _ in batch], done=False, reason=reason,
            message=(f"调度 → {who}" + (f"（{reason}）" if reason else "")
                     + (f" · {len(batch)} 人同时进行" if len(batch) > 1 else "")),
        )

        for name, instruction in batch:
            ctx.emit(EventType.AGENT_STEP_START, agent=name, instruction=instruction[:300],
                     round=round_no, parallel=len(batch))

        # 并发执行。它们拿到的是**同一份** progress 快照——这正是"任务必须
        # 互不依赖"的技术含义，也是上面提示词里那几条反例的由来。
        #
        # 每个人单独计时，而不是事后拿整轮墙钟去摊：界面要显示"并行省了多少
        # 时间"，那个数必须是量出来的。按 N×墙钟 推算等于假设每个人都跑满了
        # 最慢那条，而实际上快的那个可能只花了三分之一。
        #
        # 结束事件也在这里一完成就发，不等整轮 gather：以前先做完的成员要一直
        # 显示"进行中"，直到最慢的那个做完——矩阵答不了"谁还在跑"
        async def _timed(name: str, instruction: str, parallel: int) -> tuple[str, Any, int]:
            t0 = time.perf_counter()
            try:
                out: Any = await work(round_no, name, instruction, progress)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 一个专家挂了不该拖垮整轮
                # 把失败当成它的产出交回调度者，让它决定改派还是收工
                text = f"（{name} 执行失败：{describe_exception(exc)}）"
                out = {"text": text, "tool_calls": []}
                ctx.emit(EventType.LOG, level="warn", round=round_no,
                         message=f"{name} 这一轮失败了：{text}")
            each_ms = int((time.perf_counter() - t0) * 1000)
            ctx.emit(EventType.AGENT_STEP_END, agent=name, duration_ms=each_ms,
                     round=round_no, parallel=parallel, preview=out["text"][:500])
            return name, out, each_ms

        started = time.perf_counter()
        results = await asyncio.gather(*(_timed(n, i, len(batch)) for n, i in batch))
        wall_ms = int((time.perf_counter() - started) * 1000)

        # 进展仍按派活顺序拼：它是下一轮调度者读的输入，语义不随谁先回来而变
        sum_ms = 0
        for (name, instruction), (_, outcome, each_ms) in zip(batch, results):
            sum_ms += each_ms
            progress_lines.append(f"【{name}】{outcome['text']}")
            transcript.append(
                {"round": round_no, "agent": name, "instruction": instruction, **outcome,
                 "duration_ms": each_ms}
            )
            final_text = outcome["text"]

        rounds_meta.append({
            "round": round_no, "agents": [n for n, _ in batch],
            "parallel": len(batch), "wall_ms": wall_ms, "sum_ms": sum_ms,
        })

    usage_total["total_tokens"] = usage_total["input_tokens"] + usage_total["output_tokens"]
    # 并行省下的时间：各轮"串行本该花的"减去"实际花的"。只有并发过才有差值
    saved_ms = sum(r["sum_ms"] - r["wall_ms"] for r in rounds_meta)
    result = {
        "text": final_text,
        "rounds": len(rounds_meta),
        "steps": len(transcript),
        "transcript": transcript,
        "agents": names,
        "schedule": rounds_meta,
        "parallel_saved_ms": saved_ms,
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
    from app.engine.schema import GraphSpec, loop_steps

    if ctx.run.depth >= _MAX_DEPTH:
        raise NodeError(ctx.node.id, f"子工作流嵌套超过 {_MAX_DEPTH} 层，已阻止。"
                                     "多半是几张工作流互相引用了，检查「子工作流」节点选的是哪一张")

    workflow_id = ctx.cfg("workflow_id")
    if not workflow_id:
        raise NodeError(ctx.node.id, "「子工作流」节点还没选要嵌套的工作流")

    # 版本钉死：这就是"方法卡"的机制核心。钉了 workflow_version 就永远
    # 执行那个不可变快照，上游改了方法卡也不会让这里的口径悄悄漂移；
    # 没钉则跟随最新（画布试跑方便，但治理 lint 会在受管模板里拦下它）。
    pinned = ctx.cfg("workflow_version")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        if not workflow:
            raise NodeError(ctx.node.id, f"「子工作流」引用的工作流（{workflow_id}）已经不在了，"
                                         "可能被删除了。在节点里重新选一张")
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
        approval_default=ctx.run.approval_default,
        # 恢复重放的标记和签批人只属于父图的节点，不能漏进子图（节点 id 可能重名）
        extra={**{k: v for k, v in ctx.run.extra.items() if k not in ("resumed", "actors")},
               "node_prefix": f"{ctx.node.id}/"},
    )

    ctx.emit(EventType.LOG, level="info",
             message=f"进入子工作流「{workflow.name}」（{len(sub_spec.nodes)} 个节点）")

    compiled = compile_graph(sub_spec, sub_run)
    # 子图不带 checkpointer：它的断点由父图的 checkpoint 统一承载
    app = compiled.compile()
    limit = settings.max_graph_steps + loop_steps(sub_spec)
    try:
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
            {"recursion_limit": limit},
        )
    except GraphRecursionError as e:
        # 不接住的话，外层包装成 NodeError 时带的是 LangGraph 的英文原文
        raise NodeError(
            ctx.node.id,
            f"子图「{workflow.name}」走满了 {limit} 步还没跑完，已中止。多半是子图里有个环"
            "在空转：分支连回了上游、却没有 loop 节点给它定轮数上限",
        ) from e

    output = result_state.get("output") or {}
    result = {
        "output": output,
        "workflow": workflow.name,
        # 溯源信息：这次到底跑的是哪个版本、是不是钉死的
        "workflow_version": used_version,
        "pinned": bool(pinned),
        "text": json.dumps(output, ensure_ascii=False) if output else "",
    }
    ctx.emit(EventType.LOG, level="info", message=f"子工作流「{workflow.name}」完成")

    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": result_state.get("usage") or {},
    }
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output}
    return updates
