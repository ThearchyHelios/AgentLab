"""评测里的一轮作答：无头地走一遍问数据页走的那一轮（细案 01 做法 1、9；总方案 2.1）。

**量的就是用户拿到的那一轮。** 不另写一份 Python 版编排：生成过程是 api/copilot.answer_ops（问数据页经 SSE 收的同一串
操作），几个「决定」是 chat/turn 的纯函数（问数据页照服务端给的 next、retry_instruction 走），复核是 api/copilot.review_of_run
（问数据页经 /copilot/review 调的同一个函数）。这里只把它们串起来：

    answer_ops → final.next（chat/turn.next_step）→ launch_run(origin=eval) → 等终态 → review_of_run → 要的话带原图重来一次

- 生成：问法就是 instruction，intent=answer，限定的数据源同题目；助手模型按变体覆盖（GenerateIn.provider / model，挑表、
  复核跟着换）；读哪份目录按变体（answer_ops 的 catalog_view，查看的表详情、自查的 SQL 检查器都按它）。
- 结局：reply 直接回答、asked 反问、fail 判失败（建图失败）、ready 只建不跑、retry（老服务端那条路：没自查过的图带着原因
  重来一次）、run 运行。生成过程交出 error（助手本轮未完成）、开流前就拒绝（模型没配好、数据源不在）都是建图失败，另记
  「助手调用失败」——连续几题都这样就止损（runner.py）。
- 运行：fill_input 填入口字段（单轮，history 为空），来源 eval，名字「评测：题集名·第 n 题」，选项带目录视图（Run.options
  的 catalog_view：运行时的 db_schema、冻结快照、自查都按它）、只读和遮罩（同使用者的问数据运行：评测页会显示答案里的数，
  遮罩值不能从这里露出来；评测不写业务库）。写作模型按变体写进图的 defaults：节点自己点了模型的不覆盖，照实记在
  own_models。
- 终态：停在审批上（危险工具、人工节点）的直接取消、记「评测不代批」；被驳回的（cancelled + end_reason=rejected，总方案
  X9）记路径「被驳回」，不算运行失败；失败、被停止记运行失败。
- 复核：总是等完（问数据页超过 25 秒不等，评测里照等，复核用时另记，超过 25 秒的单列，见 stats.py）。复核给了
  retry_instruction 就带着这次运行的图重来一次（chat/turn 的规则，最多试 MAX_ATTEMPTS 次）。

用量按角色记（做法 3、11）：助手 = 生成（answer_ops 末尾的 usage）+ 复核（review_of_run 的 usage）；写作 = 答案运行里的
llm.end（按模型分）。重来的话两次都算——那都是这一题花的钱。

时限、停止由调用方管（runner.py 用 asyncio.wait_for 包住 drive）：这里随时把正在跑的那次运行记在 current_run，调用方
超时、停止时据此取消它（stop_run）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

#: 问数据页最多等复核这么久（chat.ts 的 REVIEW_TIMEOUT_MS）：评测照等，超过的单列
REVIEW_PAGE_WAIT_S = 25.0
#: 取消一次运行之后最多再等它收尾多久
CANCEL_GRACE_S = 15.0
#: 运行停在审批上：评测不代批
APPROVAL_REASON = "运行停在审批上（危险工具、人工节点）：评测不代批，已取消这次运行"
#: 每一步在做什么（EvalResult.current）
PHASE_TEXT = {"gold": "正在算期望", "build": "正在建图", "run": "运行中", "review": "正在复核", "score": "正在评分",
              "gold_after": "作答后复算期望", "judge": "正在请裁判判断"}


@dataclass
class TurnContext:
    """这一轮作答的来历（记进运行 origin）和要用的东西。"""

    suite_id: str
    suite_name: str
    eval_run_id: str
    case_id: str | None
    case_seq: int | None
    actor: str | None
    arm: str | None = None
    repeat: int = 0
    #: 变体：助手模型 {provider, model}、写作模型 {provider, model}
    assistant: Mapping[str, Any] = field(default_factory=dict)
    writer: Mapping[str, Any] = field(default_factory=dict)
    #: 目录视图（Run.options['catalog_view'] 的形状）
    view: Mapping[str, Any] | None = None


@dataclass
class Turn:
    """一轮作答的结果（也是进行中的状态：current_run、phase 随时更新）。"""

    path: str | None = None
    run_id: str | None = None
    #: 这一题发起过的全部作答运行（重来的话两次），失败的也在
    runs: list[str] = field(default_factory=list)
    attempts: int = 0
    reason: str | None = None
    #: 助手调用失败（生成过程交出 error、开流前就被拒）：runner 据此止损
    copilot_failed: bool = False
    timed_out: bool = False
    #: 直接回答的原文、反问的问题
    reply: str | None = None
    asked: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    looks: list[dict[str, Any]] = field(default_factory=list)
    #: 助手这一轮用的模型 id（model 操作）
    assistant_model: str | None = None
    #: {assistant: usage 汇总（生成，含重来）, review: usage 汇总}
    usage: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, int] = field(default_factory=dict)
    #: 写作模型按变体换了，但节点自己点了模型、没被覆盖的节点 id
    own_models: list[str] = field(default_factory=list)
    #: 正在跑的那次运行（调用方超时、停止时取消它）
    current_run: str | None = None
    phase: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "run_id": self.run_id, "runs": list(self.runs), "attempts": self.attempts,
                "reason": self.reason, "copilot_failed": self.copilot_failed, "timed_out": self.timed_out,
                "reply": self.reply, "asked": self.asked, "review": self.review, "looks": list(self.looks),
                "assistant_model": self.assistant_model, "usage": copy.deepcopy(self.usage),
                "timings": dict(self.timings), "own_models": list(self.own_models)}


def _add_usage(into: dict[str, Any] | None, more: Mapping[str, Any] | None) -> dict[str, Any]:
    """两份 providers/usage 汇总相加（by_model 逐个模型加）。"""
    out = copy.deepcopy(into) if into else {"by_model": {}, "input_tokens": 0, "output_tokens": 0, "calls": 0,
                                             "cost_usd": 0.0, "unpriced_calls": 0}
    if not more:
        return out
    for key in ("input_tokens", "output_tokens", "calls", "unpriced_calls"):
        out[key] = int(out.get(key) or 0) + int(more.get(key) or 0)
    out["cost_usd"] = round(float(out.get("cost_usd") or 0) + float(more.get("cost_usd") or 0), 6)
    for model, row in (more.get("by_model") or {}).items():
        mine = out["by_model"].setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost_usd": 0.0,
                                                  "unpriced_calls": 0})
        for key in ("input_tokens", "output_tokens", "calls", "unpriced_calls"):
            mine[key] = int(mine.get(key) or 0) + int(row.get(key) or 0)
        mine["cost_usd"] = round(float(mine.get("cost_usd") or 0) + float(row.get("cost_usd") or 0), 6)
    return out


def add_usage(into: dict[str, Any] | None, more: Mapping[str, Any] | None) -> dict[str, Any]:
    return _add_usage(into, more)


async def writer_usage(run_ids: list[str]) -> dict[str, Any]:
    """答案运行里的模型调用（llm.end，按模型分）：写作这一角色的用量，形状同 providers/usage 的汇总。封存之后追加的
    （按需裁判、质询）不算：那是事后另记的账（裁判记在评测的账上另算，见 judging.py）。"""
    from app.core.events import EventType
    from app.db.base import SessionLocal
    from app.db.models import RunEvent
    from app.providers.catalog import unpriced_call

    out = _add_usage(None, None)
    if not run_ids:
        return out
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id.in_(run_ids), RunEvent.type == EventType.LLM_END))).scalars().all()
    for data in rows:
        data = data if isinstance(data, dict) else {}
        if data.get("post_seal"):
            continue
        model = str(data.get("model") or "unknown")
        piece = {"input_tokens": int(data.get("input_tokens") or 0), "output_tokens": int(data.get("output_tokens") or 0),
                 "calls": int(data.get("calls") or 1), "cost_usd": float(data.get("cost_usd") or 0.0),
                 "unpriced_calls": 1 if unpriced_call(data) else 0}
        out = _add_usage(out, {**piece, "by_model": {model: piece}})
    return out


def with_writer(graph: dict[str, Any], writer: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """写作模型按变体写进图的 defaults（只写给了的那几项）。返回 (图, 自己点了模型、不会被覆盖的节点 id)。"""
    provider, model = writer.get("provider"), writer.get("model")
    if not (provider or model):
        return graph, []
    out = copy.deepcopy(graph)
    defaults = dict(out.get("defaults") or {})
    if provider:
        defaults["provider"] = provider
    if model:
        defaults["model"] = model
    out["defaults"] = defaults
    own = []
    for node in out.get("nodes") or []:
        config = ((node or {}).get("data") or {}).get("config") or {}
        if isinstance(config, Mapping) and (config.get("provider") or config.get("model")):
            own.append(str(node.get("id")))
    return out, own


def _options(view: Mapping[str, Any] | None) -> dict[str, Any]:
    """作答运行带的选项：目录视图、只读、遮罩（见模块说明）。"""
    from app.data.catalog_view import OPTION_KEY
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    out: dict[str, Any] = {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}
    if view:
        out[OPTION_KEY] = dict(view)
    return out


def run_name(suite_name: str, case_seq: int | None) -> str:
    return f"评测：{suite_name or '题集'}·第 {case_seq or '?'} 题"


async def stop_run(run_id: str | None, actor: str | None = None) -> None:
    """取消一次还没结束的运行、等它收尾（超时、停止评测、停在审批上时）。已经结束的什么都不做。"""
    from app.engine.runner import run_manager

    if not run_id:
        return
    try:
        await run_manager.cancel(run_id, actor=actor)
        await run_manager.wait_idle(run_id, timeout=CANCEL_GRACE_S)
    except Exception:  # noqa: BLE001 - 取消是收尾，出错只记日志
        logger.exception("评测：取消运行 %s 时出错", run_id)


class Driver:
    """一轮作答。drive() 跑完一轮，结果在 turn 上；on_phase(阶段) 在每一步开始时调（runner 据此写「正在做什么」）。"""

    def __init__(self, question: str, *, datasource_ids: list[str], ctx: TurnContext,
                 on_phase: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.question = question
        self.datasource_ids = list(datasource_ids)
        self.ctx = ctx
        self.on_phase = on_phase
        self.turn = Turn()

    async def _phase(self, phase: str) -> None:
        self.turn.phase = phase
        if self.on_phase is not None:
            try:
                await self.on_phase(phase)
            except Exception:  # noqa: BLE001 - 写进度失败不挡作答
                logger.exception("评测：记进度出错")

    # ------------------------------------------------------------------ 生成

    async def _generate(self, instruction: str, base_graph: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str | None]:
        """跑一次生成过程，收下全部操作。开流前就被拒（HTTPException）交回原因。"""
        from fastapi import HTTPException

        from app.api.copilot import GenerateIn, answer_ops

        payload = GenerateIn(instruction=instruction, base_graph=base_graph, intent="answer",
                             provider=self.ctx.assistant.get("provider"), model=self.ctx.assistant.get("model"),
                             datasource_ids=self.datasource_ids or None)
        ops: list[dict[str, Any]] = []
        try:
            async for op in answer_ops(payload, catalog_view=self.ctx.view):
                if op.get("op") in ("thinking", "heartbeat"):
                    continue
                ops.append(op)
        except HTTPException as e:
            return ops, str(e.detail)
        return ops, None

    def _take(self, ops: list[dict[str, Any]]) -> dict[str, Any] | None:
        """收下这一次生成的用量、查看记录、模型；交回结局（final / reply / error），只有反问的为 None。"""
        terminal = None
        for op in ops:
            kind = op.get("op")
            if kind == "usage":
                self.turn.usage["assistant"] = _add_usage(self.turn.usage.get("assistant"),
                                                          {k: v for k, v in op.items() if k != "op"})
            elif kind == "look":
                self.turn.looks.append({k: op.get(k) for k in ("id", "by", "need", "status") if k in op})
            elif kind == "model":
                self.turn.assistant_model = str(op.get("model") or "") or None
            elif kind in ("final", "reply", "error"):
                terminal = op
        return terminal

    # ------------------------------------------------------------------ 运行

    async def _launch(self, graph: dict[str, Any], payload: dict[str, Any] | None = None) -> str | None:
        from app.chat import turn as policy
        from app.db.base import SessionLocal
        from app.engine.launch import LaunchRefused, launch_run

        graph, own = with_writer(graph, self.ctx.writer)
        self.turn.own_models = sorted(set(self.turn.own_models) | set(own))
        origin = {k: v for k, v in {
            "suite_id": self.ctx.suite_id, "eval_run_id": self.ctx.eval_run_id, "case_id": self.ctx.case_id,
            "role": "answer", "arm": self.ctx.arm, "repeat": self.ctx.repeat, "attempt": self.turn.attempts}.items()
            if v is not None}
        try:
            async with SessionLocal() as session:
                inputs = payload if payload is not None else policy.fill_input(graph, self.question, "")
                run = await launch_run(session, graph=graph, input_payload=inputs,
                                       actor=self.ctx.actor, options=_options(self.ctx.view), origin_kind="eval",
                                       origin=origin, workflow_name=run_name(self.ctx.suite_name, self.ctx.case_seq))
        except LaunchRefused as e:
            self.turn.path, self.turn.reason = "run_failed", f"运行没有发起：{e.message}"
            return None
        self.turn.runs.append(run.id)
        self.turn.run_id = self.turn.current_run = run.id
        return run.id

    async def _wait(self, run_id: str) -> Any:
        """等运行到终态（或停在审批上）。交回库里的运行。"""
        from app.db.base import SessionLocal
        from app.db.models import Run
        from app.engine.runner import run_manager

        while True:
            await run_manager.wait_idle(run_id, timeout=5.0)
            async with SessionLocal() as session:
                row = await session.get(Run, run_id)
            if row is None or row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                # 封存在终态之后：等执行任务收完尾（含封存），评分只读封存范围
                await run_manager.wait_idle(run_id, timeout=CANCEL_GRACE_S)
                async with SessionLocal() as session:
                    return await session.get(Run, run_id)
            await asyncio.sleep(0.2)

    async def _pending_approvals(self, run_id: str) -> int:
        from app.db.base import SessionLocal
        from app.db.models import Approval

        async with SessionLocal() as session:
            return int(await session.scalar(select(func.count()).select_from(Approval).where(
                Approval.run_id == run_id, Approval.status.in_(("pending", "answered")))) or 0)

    # ------------------------------------------------------------------ 一轮

    async def _run(self, graph: dict[str, Any], payload: dict[str, Any] | None = None) -> Any | None:
        """发起、等到终态。成功结束的交回运行；别的结局记好路径和原因，交回 None。"""
        from app.engine.runner import END_REJECTED

        t = self.turn
        run_id = await self._launch(graph, payload)
        if run_id is None:
            return None
        row = await self._wait(run_id)
        t.current_run = None
        usage = dict((row.usage or {}) if row is not None else {})
        t.timings["run_wall_ms"] = t.timings.get("run_wall_ms", 0) + int(usage.get("wall_ms") or 0)
        t.timings["run_active_ms"] = t.timings.get("run_active_ms", 0) + int(
            usage.get("active_ms") or usage.get("duration_ms") or 0)
        if row is None:
            t.path, t.reason = "run_failed", "运行记录不见了"
            return None
        if row.status == "interrupted":
            waiting = await self._pending_approvals(run_id)
            await stop_run(run_id, self.ctx.actor)
            t.path = "run_failed"
            t.reason = APPROVAL_REASON if waiting else "运行停在断点上（服务可能重启过）：评测不继续这次运行，已取消"
            return None
        if row.status == "cancelled" and row.end_reason == END_REJECTED:
            t.path, t.reason = "rejected", "运行被驳回（人的决定，不算运行失败）"
            return None
        if row.status != "succeeded":
            t.path = "run_failed"
            t.reason = f"运行{'失败' if row.status == 'failed' else '被停止'}：{row.error or '没有记下原因'}"
            return None
        return row

    async def drive_workflow(self, graph: dict[str, Any], inputs: dict[str, Any]) -> Turn:
        """工作流题：钉住那一版的图按输入跑一次探索运行（来源 eval）。不建图、不复核：考的是这一版本身。不发正式运行——
        正式运行会被 pick_base 和应用视图的「最近一次封存」取到（同期望探针不发正式运行的理由）。"""
        t = self.turn
        started = time.monotonic()
        try:
            await self._phase("run")
            if await self._run(graph, inputs) is not None:
                t.path = "ran"
            return t
        finally:
            t.timings["total_ms"] = int((time.monotonic() - started) * 1000)

    async def drive(self) -> Turn:
        from app.chat import turn as policy

        t = self.turn
        started = time.monotonic()
        instruction, base_graph = self.question, None
        try:
            while True:
                await self._phase("build")
                building = time.monotonic()
                ops, refused = await self._generate(instruction, base_graph)
                t.timings["build_ms"] = t.timings.get("build_ms", 0) + int((time.monotonic() - building) * 1000)
                terminal = self._take(ops)
                if refused is not None:
                    t.path, t.reason, t.copilot_failed = "build_failed", f"助手没有开始：{refused}", True
                    return t
                if terminal is None:
                    asked = next((o for o in reversed(ops) if o.get("op") == "ask"), None)
                    t.path, t.asked = "asked", {k: asked.get(k) for k in ("question", "options")} if asked else None
                    return t
                kind = terminal.get("op")
                if kind == "error":
                    t.path, t.reason, t.copilot_failed = "build_failed", str(terminal.get("message") or "助手本轮未完成"), True
                    return t
                if kind == "reply":
                    t.path, t.reply = "reply", str(terminal.get("text") or "")
                    return t
                if terminal.get("asked"):
                    t.path, t.asked = "asked", dict(terminal["asked"])
                    return t
                self_checked = any(o.get("op") == "check" for o in ops)
                step = terminal.get("next") or policy.next_step(terminal, self_checked=self_checked)
                graph = terminal.get("graph") or {}
                if step == policy.NEXT_RETRY:
                    if t.attempts + 1 < policy.MAX_ATTEMPTS:
                        t.attempts += 1
                        instruction = policy.retry_instruction(self.question, policy.blocked_reason(terminal),
                                                               policy.has_nodes(graph))
                        base_graph = graph if policy.has_nodes(graph) else None
                        continue
                    t.path, t.reason = "build_failed", policy.blocked_reason(terminal)
                    return t
                if step == policy.NEXT_FAIL:
                    errors = [str(i.get("message") or "") for i in terminal.get("issues") or []
                              if isinstance(i, Mapping) and i.get("level") == "error"]
                    t.path, t.reason = "build_failed", "生成的工作流没有通过校验：" + "；".join(errors)
                    return t
                if step == policy.NEXT_READY:
                    t.path, t.reason = "ready", "助手只建了图、没有运行（它判断用户要的是流程本身）"
                    return t

                await self._phase("run")
                row = await self._run(graph)
                if row is None:
                    return t

                await self._phase("review")
                reviewing = time.monotonic()
                review = await self._review(row)
                spent = int((time.monotonic() - reviewing) * 1000)
                t.timings["review_ms"] = t.timings.get("review_ms", 0) + spent
                t.review = {k: v for k, v in review.items() if k != "usage"}
                t.usage["review"] = _add_usage(t.usage.get("review"), review.get("usage"))
                if review.get("retry_instruction"):
                    t.attempts += 1
                    instruction = str(review["retry_instruction"])
                    base_graph = row.graph if policy.has_nodes(row.graph) else None
                    continue
                t.path = "ran"
                return t
        finally:
            t.timings["total_ms"] = int((time.monotonic() - started) * 1000)

    async def _review(self, run: Any) -> dict[str, Any]:
        """复核（review_of_run，问数据页同一个函数）。复核自己出错不挡评分：照实记一条失败的复核。"""
        from app.api.copilot import review_of_run
        from app.core.errors import describe_exception
        from app.db.base import SessionLocal

        try:
            async with SessionLocal() as session:
                return await review_of_run(session, run, question=self.question,
                                           provider=self.ctx.assistant.get("provider"),
                                           model=self.ctx.assistant.get("model"), attempt=self.turn.attempts)
        except Exception as e:  # noqa: BLE001
            logger.exception("评测：复核运行 %s 出错", run.id)
            return {"verdict": "failed", "note": f"复核出错：{describe_exception(e)}", "signals": [],
                    "retry_instruction": None}


__all__ = ["APPROVAL_REASON", "Driver", "PHASE_TEXT", "REVIEW_PAGE_WAIT_S", "Turn", "TurnContext", "add_usage",
           "run_name", "stop_run", "with_writer", "writer_usage"]
