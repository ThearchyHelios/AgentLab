from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.func import task

from app.core.config import settings
from app.core.errors import describe_exception, raw_detail
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Workflow
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, message_text, template_context
from app.engine.toolcalls import (
    TOOL_MARKUP_NUDGE,
    ToolTimeout,
    leaked_markup,
    limit_fields,
    limit_of,
    markup_warning,
    run_bounded,
)
from app.providers import catalog
from app.providers.factory import ModelSpec, bind_tools_safely, get_chat_model
from app.engine.sql_problems import event_checks
from app.tools.datasource import QUERY_PREFIX, run_versions
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    args_model_of,
    build_tools,
    prepare_args,
)
from app.tools.trust import ask_gate, call_policy

_MAX_DEPTH = 3

#: 调度者在 llm.end 里的 agent 名。成员用各自的名字
COORDINATOR = "调度者"

#: 成员把工具调用写成文字、纠正之后还是这样时，交给调度者的那句话
MEMBER_MARKUP_FAILED = ("模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据"
                        "（常见原因：该成员未绑定工具，或模型、服务不支持工具调用）")


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
        raise NodeError(ctx.node.id, "「多 Agent 协作」节点未配置团队成员，至少需要一个")

    names = [a.get("name") or f"agent{i}" for i, a in enumerate(agents)]
    agent_map = {n: a for n, a in zip(names, agents)}
    max_rounds = min(int(ctx.cfg("max_rounds", 6) or 6), settings.max_agent_steps)
    #: 轮数用完、调度者始终没判定完成时怎么办。fail：判失败；degrade：照样交付但标明降档。
    #: 缺省 fail——成员的原话不是结论，把它当成功交出去比失败更糟
    on_exhausted = "degrade" if ctx.cfg("on_exhausted") == "degrade" else "fail"

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

    def _accumulate(one: dict[str, Any], into: dict[str, Any] | None = None) -> None:
        into = usage_total if into is None else into
        into["input_tokens"] += one["input_tokens"]
        into["output_tokens"] += one["output_tokens"]
        into["calls"] += one.get("calls", 1)
        into["cost_usd"] = round(into["cost_usd"] + one["cost_usd"], 6)

    def _report(who: str, model_id: str, one: dict[str, Any], t0: float) -> None:
        # 每次模型调用各发一条 llm.end，界面上的用量才能实时涨，而且分得清是谁花的。
        # 它只是把 usage_total 这笔账逐次摊开：run.finished 的 usage 仍以累计为准
        ctx.emit(EventType.LLM_END, agent=who, model=model_id,
                 duration_ms=int((time.perf_counter() - t0) * 1000), **one)

    async def _decide(prompt: str, *, closing: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """问调度者一次。返回 (决定, 这次调用的用量)。"""
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
            # 不支持结构化输出的模型退回文本：从回复里认出一个成员名，按单人派。
            # 收尾判定时同理：回复里还点着哪个成员，就是还没完
            reply = await supervisor_model.ainvoke(prompt)
            spent = _usage(reply, sup_model_id)
            raw = message_text(reply)
            picked = next((n for n in names if n in raw), None)
            if closing:
                value = {"assignments": [], "done": picked is None, "reason": raw[:200]}
            else:
                value = ({"assignments": [{"agent": picked, "instruction": raw[:500]}]}
                         if picked else {"assignments": [], "done": True, "reason": raw[:200]})
        return (value if isinstance(value, dict) else {}), spent

    def _batch_of(decision: dict[str, Any]) -> list[tuple[str, str]]:
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
        return batch

    def _finished(round_no: int, reason: str, **extra: Any) -> None:
        # agents / done 是结构化字段，界面照着它渲染。以前只有一句中文
        # 消息串，解码层得拿正则去拆「调度 → X（理由）」——文案一改就散架
        ctx.emit(EventType.LOG, level="info", round=round_no, agents=[], done=True,
                 message="调度 → 结束协作" + (f"（{reason}）" if reason else ""), **extra)

    # 调度者决策前后各一条事件（它常常占掉团队节点大半的时间，以前是黑盒），和派活的
    # 那条日志一起都在 task 里发：「接着跑」一个失败的团队节点时整个节点重放，前几轮
    # 的决定取自 checkpoint，这些事件不该在时间线上再出现一遍
    @task
    async def route(round_no: int, progress: str) -> dict[str, Any]:
        ctx.emit(EventType.AGENT_ROUTE_START, round=round_no)
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
        value, spent = await _decide(prompt)
        _report(COORDINATOR, sup_model_id, spent, t0)

        batch = _batch_of(value)
        finishing = bool(value.get("done")) or not batch
        reason = str(value.get("reason", ""))
        ctx.emit(EventType.AGENT_ROUTE_END, round=round_no,
                 duration_ms=int((time.perf_counter() - t0) * 1000),
                 agents=[] if finishing else [n for n, _ in batch],
                 parallel=0 if finishing else len(batch), done=finishing, reason=reason)
        if finishing:
            _finished(round_no, reason)
        else:
            who = "、".join(n for n, _ in batch)
            ctx.emit(
                EventType.LOG, level="info", round=round_no, parallel=len(batch),
                agents=[n for n, _ in batch], done=False, reason=reason,
                message=(f"调度 → {who}" + (f"（{reason}）" if reason else "")
                         + (f" · {len(batch)} 名成员同时执行" if len(batch) > 1 else "")),
            )
        return {"decision": value, "usage": spent}

    # 最后一轮派出去的成员（常常就是定稿的那个）交回来之后，调度者还没看过它的产出。
    # 以前轮数一到就算用完：步数恰好等于最多轮数的正常流水线（查数 → 撰稿，2 轮）也会
    # 被判成没完成。补这一次只判定、不派活的决定，它说完成了才算完成
    @task
    async def verdict(round_no: int, progress: str) -> dict[str, Any]:
        ctx.emit(EventType.AGENT_ROUTE_START, round=round_no, closing=True)
        prompt = (
            f"你是团队调度者。目标：\n{goal}\n\n"
            f"当前进展：\n{progress or '(还没有任何进展)'}\n\n"
            f"轮数已经用完（{max_rounds} 轮），不能再派任何任务。只判断一件事：按上面的进展，"
            "目标是不是已经达成、能不能把最后一个成员的产出作为结论交出去。\n"
            "达成了 done 填 true；没达成 done 填 false，并在 reason 里说清还缺什么。"
            "assignments 给空数组。"
        )
        t0 = time.perf_counter()
        value, spent = await _decide(prompt, closing=True)
        _report(COORDINATOR, sup_model_id, spent, t0)
        done = value.get("done") is True
        reason = str(value.get("reason", ""))
        ctx.emit(EventType.AGENT_ROUTE_END, round=round_no,
                 duration_ms=int((time.perf_counter() - t0) * 1000),
                 agents=[], parallel=0, done=done, reason=reason, closing=True)
        if done:
            _finished(round_no, reason, closing=True)
        return {"done": done, "reason": reason, "usage": spent}

    # 成员的开始、结束两条事件也在 task 里发，理由同 route。失败也在 task 里收住、作为
    # 结果缓存：重放时这一步照旧是失败，不会换一个结果。用量随结果带出来再记账——在
    # task 里直接记进 usage_total 的话，重放取了缓存，这一笔就没了
    @task
    async def work(round_no: int, name: str, instruction: str, progress: str,
                   parallel: int = 1) -> dict[str, Any]:
        ctx.emit(EventType.AGENT_STEP_START, agent=name, instruction=instruction[:300],
                 round=round_no, parallel=parallel)
        tally = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
        t0 = time.perf_counter()
        try:
            out: dict[str, Any] = await _member(name, instruction, progress, tally)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 一个专家挂了不该拖垮整轮
            # 把失败当成它的产出交回调度者，让它决定改派还是收工
            why = describe_exception(exc)
            text = f"（{name} 执行失败：{why}）"
            out = {"text": text, "tool_calls": [], "failed": True, "error": why}
            ctx.emit(EventType.LOG, level="warn", round=round_no,
                     message=f"{name} 本轮执行失败：{text}")
        each_ms = int((time.perf_counter() - t0) * 1000)
        # failed / error 让矩阵把这一格画成失败，而不是一个"完成"的格子里写着失败原因
        failure = {"failed": True, "error": out.get("error") or ""} if out.get("failed") else {}
        ctx.emit(EventType.AGENT_STEP_END, agent=name, duration_ms=each_ms,
                 round=round_no, parallel=parallel, preview=out["text"][:500], **failure)
        return {**out, "duration_ms": each_ms, "usage": tally}

    async def _member(name: str, instruction: str, progress: str,
                      tally: dict[str, Any]) -> dict[str, Any]:
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
                data_versions=run_versions(ctx.run),
                # 数据源工具运行中途补固定版本时经它留一条事件（tools/datasource.py 的 _pin_late）
                emit=ctx.emit,
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
        nudged = False
        for _ in range(max_steps):
            t0 = time.perf_counter()
            reply = await bound.ainvoke(messages)
            spent = _usage(reply, model_id)
            _accumulate(spent, tally)
            _report(name, model_id, spent, t0)
            messages.append(reply)
            tool_calls = getattr(reply, "tool_calls", None) or []
            if not tool_calls:
                text = message_text(reply)
                snippet = leaked_markup(text)
                if not snippet:
                    return {"text": text, "tool_calls": calls}
                # 把工具调用写成了文字：这一步什么都没查到。纠正一次，还这样就记为失败，
                # 原因交给调度者——以前这段标记会被当成这个成员的结论交上去
                if nudged:
                    return {"text": f"（{name} 这一步执行失败：{MEMBER_MARKUP_FAILED}）",
                            "tool_calls": calls, "failed": True, "error": MEMBER_MARKUP_FAILED}
                nudged = True
                ctx.emit(EventType.LOG, level="warn", code="tool_markup_leak",
                         message=f"{markup_warning(snippet, name)}，已要求模型重试一次")
                messages.append(HumanMessage(content=TOOL_MARKUP_NUDGE))
                continue
            for call in tool_calls:
                tname = call.get("name", "")
                targs = call.get("args", {}) or {}
                cid = call.get("id") or f"{name}-{tname}"
                limit = None
                failure: dict[str, Any] = {}
                started = time.perf_counter()
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
                    # 要不要人工确认、要不要先问门控：按纠正后的参数判
                    policy = "safe"
                    gate_reason = ""
                    if not args_error and approval != "never":
                        policy = call_policy(tool_map[tname], tname, targs, ctx.run.tool_trust)
                        if policy == "gate":
                            gate = await ask_gate(
                                tool=tool_map[tname], args=targs, node_title=f"{ctx.node.title} · {name}",
                                task=f"团队目标：{goal}\n\n{name} 的任务：{instruction}")
                            ctx.emit(EventType.TOOL_GATED, tool=tname, agent=name, call_id=cid, **gate.event())
                            _accumulate({"input_tokens": gate.usage.get("input_tokens", 0),
                                         "output_tokens": gate.usage.get("output_tokens", 0),
                                         "cost_usd": gate.usage.get("cost_usd", 0.0), "calls": 1}, tally)
                            policy = "safe" if gate.allowed else "ask"
                            gate_reason = gate.reason
                    # 纠正后的参数才是真正要执行的那份，tool.start 要发它
                    limit = limit_of(tool_map[tname], tname, targs)
                    ctx.emit(EventType.TOOL_START, tool=tname, args=targs, agent=name, call_id=cid,
                             **limit_fields(limit))
                    if fix_note:
                        ctx.emit(EventType.LOG, level="warn",
                                 message=f"工具 {tname}：{fix_note}", code="tool_args_fixed")
                    if args_error:
                        # 参数就不对，没必要真调一次。把"它接受什么"喂回去让它改
                        content = args_error
                    elif policy == "ask":
                        # 专家们并行跑在同一个节点里，停不下来等人：审批恢复时节点整个
                        # 重放，几个人的 interrupt 谁先谁后对不上号。以前的做法是不审，
                        # shell_exec / file_write / 可写库上的 DELETE 照跑不误——
                        # agent 节点守着的那道门，在这里是敞开的。现在是不跑，并说清原因
                        content = (
                            (f"门控模型未批准（{gate_reason}）。" if gate_reason else "")
                            + f"没有执行：{tname} 这次调用需要人工审批，而协作团队里的成员"
                            "不能停下来等人。换一种不需要它的做法；实在需要，交给团队外"
                            "的 Agent 节点去做（那里可以逐次审批）。"
                        )
                        ctx.emit(EventType.LOG, level="warn", code="tool_needs_approval", tool=tname,
                                 agent=name, call_id=cid,
                                 message=f"{name} 请求调用 {tname}，该调用需要人工审批，协作节点内不执行")
                    else:
                        try:
                            raw = await run_bounded(tool_map[tname].ainvoke(targs), limit, tname)
                            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                        except ToolTimeout as e:
                            content = str(e)
                            failure = {"error": content, "timed_out": True}
                        except Exception as e:  # noqa: BLE001
                            content = f"工具失败：{describe_exception(e)}"
                            failure = {"error": content, "detail": raw_detail(e)}
                # 查库的 SQL 检查结果单独带上：preview 截到前 1500 个字符，checks 通常在截掉的那段里
                checks = event_checks(content) if not failure and tname.startswith(QUERY_PREFIX) else []
                ctx.emit(EventType.TOOL_ERROR if failure else EventType.TOOL_END, tool=tname,
                         agent=name, call_id=cid, preview=content[:1500],
                         duration_ms=int((time.perf_counter() - started) * 1000), **failure,
                         **({"checks": checks} if checks else {}))
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
            _accumulate(spent, tally)
            _report(name, model_id, spent, t0)
            text = message_text(settled).strip()
        except Exception as e:  # noqa: BLE001 - 收尾失败不能把已有的过程一起赔进去
            ctx.emit(EventType.LOG, level="warn", code="settle_failed",
                     message=f"{name} 收尾失败：{describe_exception(e)}")
            text = next((t for m in reversed(messages)
                         if isinstance(m, AIMessage) and (t := message_text(m).strip())
                         and not leaked_markup(t)), "")
        if snippet := leaked_markup(text):
            if not calls:
                return {"text": f"（{name} 这一步执行失败：{MEMBER_MARKUP_FAILED}）",
                        "tool_calls": calls, "failed": True, "error": MEMBER_MARKUP_FAILED}
            # 真调过工具，只是收尾轮还想接着查：取它之前说过的话，和 agent 节点一样
            ctx.emit(EventType.LOG, level="warn", code="tool_markup_leak",
                     message=f"{markup_warning(snippet, name)}：已达步数上限，收尾时仍试图调用工具")
            text = next((t for m in reversed(messages)
                         if isinstance(m, AIMessage) and (t := message_text(m).strip())
                         and not leaked_markup(t)), "")
        ctx.emit(EventType.LOG, level="warn", code="step_limit_settled",
                 message=f"{name} 已用完 {max_steps} 步上限，结论基于已查到的部分")
        return {"text": text or f"（{name} 已用完 {max_steps} 步上限，未给出结论）", "tool_calls": calls}

    progress_lines: list[str] = []
    final_text = ""
    #: 成功交回来的产出（成员, 原话）。调度者说完成时，成果取最后一份成功的：
    #: 失败说明是写给调度者看的，不是结论
    delivered: list[tuple[str, str]] = []
    rounds_meta: list[dict[str, Any]] = []
    #: 调度者最后一轮给的理由，以及派过活的成员。轮数用完时报错要说清卡在哪、谁从没上过场
    last_reason = ""
    dispatched: set[str] = set()
    finished = False
    for round_no in range(max_rounds):
        progress = "\n\n".join(progress_lines)
        routed = await route(round_no, progress)
        decision = routed.get("decision", routed) if isinstance(routed, dict) else {}
        decision = decision if isinstance(decision, dict) else {}
        _accumulate(routed.get("usage") or _usage(None, sup_model_id))
        reason = str(decision.get("reason", ""))
        last_reason = reason or last_reason

        batch = _batch_of(decision)
        if bool(decision.get("done")) or not batch:
            finished = True
            break

        dispatched.update(n for n, _ in batch)

        # 并发执行。它们拿到的是**同一份** progress 快照——这正是"任务必须
        # 互不依赖"的技术含义，也是上面提示词里那几条反例的由来。
        #
        # 每个人单独计时，而不是事后拿整轮墙钟去摊：界面要显示"并行省了多少
        # 时间"，那个数必须是量出来的。按 N×墙钟 推算等于假设每个人都跑满了
        # 最慢那条，而实际上快的那个可能只花了三分之一。
        #
        # 结束事件也在各自的 task 里一完成就发，不等整轮 gather：以前先做完的成员要
        # 一直显示"进行中"，直到最慢的那个做完——矩阵答不了"谁还在跑"
        async def _one(name: str, instruction: str, parallel: int) -> dict[str, Any]:
            return await work(round_no, name, instruction, progress, parallel)

        started = time.perf_counter()
        results = await asyncio.gather(*(_one(n, i, len(batch)) for n, i in batch))
        wall_ms = int((time.perf_counter() - started) * 1000)

        # 进展仍按派活顺序拼：它是下一轮调度者读的输入，语义不随谁先回来而变
        sum_ms = 0
        for (name, instruction), result in zip(batch, results):
            outcome = dict(result)
            spent = outcome.pop("usage", None)
            if spent:
                _accumulate(spent)
            each_ms = int(outcome.pop("duration_ms", 0) or 0)
            sum_ms += each_ms
            progress_lines.append(f"【{name}】{outcome['text']}")
            transcript.append(
                {"round": round_no, "agent": name, "instruction": instruction, **outcome,
                 "duration_ms": each_ms}
            )
            final_text = outcome["text"]
            if not outcome.get("failed"):
                delivered.append((name, outcome["text"]))

        rounds_meta.append({
            "round": round_no, "agents": [n for n, _ in batch],
            "parallel": len(batch), "wall_ms": wall_ms, "sum_ms": sum_ms,
        })

    if not finished and progress_lines:
        judged = await verdict(max_rounds, "\n\n".join(progress_lines))
        _accumulate(judged.get("usage") or _usage(None, sup_model_id))
        last_reason = str(judged.get("reason") or "") or last_reason
        if judged.get("done"):
            finished = True

    exhausted: dict[str, Any] = {}
    failed = [t for t in transcript if t.get("failed")]
    never = [n for n in names if n not in dispatched]
    if finished and delivered:
        final_text = delivered[-1][1]
        if transcript[-1].get("failed"):
            # 调度者认可了收工，可最后派出去的人没交回东西：成果取最后一份成功的产出，
            # 并说清楚——下游拿到的是它，不是本该定稿的那个人的
            late = "、".join(dict.fromkeys(t["agent"] for t in failed
                                           if t["round"] == transcript[-1]["round"]))
            ctx.emit(EventType.LOG, level="warn", code="team_last_failed",
                     message=f"{late} 最后一轮未交回结果，团队成果取自{delivered[-1][0]}的产出")
    elif finished and transcript:
        # 调度者说完成了，派出去的成员却一个都没交回结果，手上只有失败说明。以前最后
        # 那段失败说明就被当成团队的结论交出去：一条数据都没查到，运行照样是已完成。
        # 不看 on_exhausted：那是「用完轮数时」的收场方式，降档交的是成员原话；这里
        # 连原话都没有，交下去的只能是失败说明，界面还会把它说成「用完 N 轮」
        why = "；".join(f"{who}：{err[:200]}" for who, err in dict.fromkeys(
            (t["agent"], t.get("error") or "未说明原因") for t in failed))
        summary = (f"协作团队未交出结论：所有被分派的成员都未能交回结果，调度者仍判定完成"
                   f"（{last_reason or '未给出理由'}）。{why}")
        if never:
            summary += f"。一次都没被派到的成员：{'、'.join(never)}"
        raise NodeError(ctx.node.id, f"{summary}。请根据各成员的失败原因修改配置后重新运行")
    elif not finished:
        # 轮数用完，调度者一次都没说「完成」。这时手上只有最后一个成员的原话——
        # 以前它就被当作团队的结论交了出去：真实运行里是一段没执行的工具调用标记，
        # 而负责定稿的成员一次都没被派到
        summary = (f"协作团队用完 {max_rounds} 轮仍未完成："
                   f"{last_reason or '调度者未给出理由'}")
        if never:
            summary += f"。一次都没被派到的成员：{'、'.join(never)}"
        if on_exhausted == "fail":
            # 「。先看成员」是前端切分这句话的锚点（decode.ts exhaustedOf、NodeCard、explain.ts），不要改
            raise NodeError(ctx.node.id, f"{summary}。先看成员是否绑定了所需工具，再调大「最多"
                                         "轮数」；也可将「用完轮数时」改为「降档交付」")
        exhausted = {"exhausted": True, "exhausted_reason": last_reason,
                     "never_dispatched": never}
        ctx.emit(EventType.LOG, level="warn", code="team_exhausted",
                 message=f"{summary}。按降档交付：成果取自成员最后的回复，并非调度者认可的结论")

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
        **exhausted,
    }
    updates: dict[str, Any] = {
        "nodes": {ctx.node.id: result},
        "usage": usage_total,
    }
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=final_text or "（空）")]
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
                                     "可能是多个工作流相互引用，请检查「子工作流」节点选择的工作流")

    workflow_id = ctx.cfg("workflow_id")
    if not workflow_id:
        raise NodeError(ctx.node.id, "「子工作流」节点尚未选择要嵌套的工作流")

    # 版本钉死：这就是"方法卡"的机制核心。钉了 workflow_version 就永远
    # 执行那个不可变快照，上游改了方法卡也不会让这里的口径悄悄漂移；
    # 没钉则跟随最新（画布试跑方便，但治理 lint 会在受管模板里拦下它）。
    pinned = ctx.cfg("workflow_version")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        if not workflow:
            raise NodeError(ctx.node.id, f"「子工作流」引用的工作流（{workflow_id}）已不存在，"
                                         "可能已被删除，请在节点中重新选择")
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
                    ctx.node.id, f"工作流「{workflow.name}」不存在 v{pinned} 版本"
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
        tool_trust=ctx.run.tool_trust,
        agent_limits=ctx.run.agent_limits,
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
            f"子工作流「{workflow.name}」已执行 {limit} 步仍未结束，已中止。可能存在没有轮数上限的环路，"
            "请在子工作流中用「循环」节点限制轮数",
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
