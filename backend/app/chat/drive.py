"""一轮问数据的流程：问数据（服务端跑整轮，app/chat/job.py）和评测（app/eval/answer.py）共用这一份。

**为什么只有一份。** 评测量的必须是用户拿到的那一轮（细案 01 做法 1），以前问数据的一轮是浏览器在推（frontend/src/store/chat.ts
的 plan / launch / settle），评测照着在服务端另串了一遍（eval/answer.Driver）。现在问数据也整轮放到服务端（总方案 §1），两边各写
一份流程迟早走样，所以把流程抽到这里，差异写成子类给的几项（总方案上一轮 X「一种说法」）：

    answer_ops → final.next（chat/turn.next_step）→ launch_run → 等终态 → review_of_run → 要的话带原图重来一次

- 生成：generate_in 给这一次生成的请求（问数据带对话、限定的数据源；评测带变体的助手模型），catalog_view 给读哪一份数据目录
  （评测按变体，问数据是当前目录）。每一条操作都先交给 on_op（问数据写进这一轮的事件日志，评测不管），思考和心跳不进结局判断。
- 结局：reply 直接回答、asked 反问、ready 只建不跑、build_failed 建图失败、retry 带着原因重来（次数照 MAX_ATTEMPTS）、run 运行。
  每次重来前调 on_retry（问数据记一条「重来」，把换下来的运行的用量并进这一轮）。
- 运行：prepare_graph 改图（评测按变体写进写作模型）、run_input 填入口（问数据带前几轮的问答，评测单轮）、launch_args 给来源、
  选项、发起人（遮罩、只读、目录视图照各自现有的规则），发起之后调 on_launched。
- 终态：停在审批上交给 on_interrupted——评测取消（不代批），问数据照旧等人（这是两边唯一的流程差别）；被驳回的记 rejected，
  失败、被停止记 run_failed。
- 复核：_review（评测一直等、复核自己出错照实记一条失败的复核；问数据最多等 25 秒，见 chat/job.py）。给了
  retry_instruction 且 retry 打开时带着这次运行的图重来一次。

drive 的几个参数只有问数据用：graph（不建图、直接运行这张图：重新运行本轮、放宽步数、跑一下）、run_id（不发起、接着等这次
运行：继续运行、服务重启后接着收尾）、retry（复核之后要不要自动重来：用户点的「重新运行」照以前不自动重来）、instruction /
base_graph（用户点「重试本轮」时带着原因和上一次的图重建）。不给就是评测那一轮，和抽出来之前逐条一样。

时限、停止由调用方管：这里随时把正在跑的那次运行记在 turn.current_run，调用方超时、停止时据此取消它。
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

#: 取消一次运行之后最多再等它收尾多久
CANCEL_GRACE_S = 15.0


@dataclass
class Turn:
    """一轮作答的结果（也是进行中的状态：current_run、phase 随时更新）。"""

    path: str | None = None
    run_id: str | None = None
    #: 这一轮发起过的全部运行（重来的话两次），失败的也在
    runs: list[str] = field(default_factory=list)
    attempts: int = 0
    #: 每次自动重来的原因（和 attempts 一一对应）：没过检查、复核说不可信、生成过程一个操作都没给
    retries: list[str] = field(default_factory=list)
    reason: str | None = None
    #: 助手调用失败（生成过程交出 error、开流前就被拒）：评测据此止损
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
    #: 写作模型按变体换了，但节点自己点了模型、没被覆盖的节点 id（评测）
    own_models: list[str] = field(default_factory=list)
    #: 正在跑的那次运行（调用方超时、停止时取消它）
    current_run: str | None = None
    phase: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "run_id": self.run_id, "runs": list(self.runs), "attempts": self.attempts,
                "retries": list(self.retries), "reason": self.reason, "copilot_failed": self.copilot_failed, "timed_out": self.timed_out,
                "reply": self.reply, "asked": self.asked, "review": self.review, "looks": list(self.looks),
                "assistant_model": self.assistant_model, "usage": copy.deepcopy(self.usage),
                "timings": dict(self.timings), "own_models": list(self.own_models)}


#: 用量汇总里相加的计数（providers/usage 的 summary）：unfinished_calls 是没做完的调用（被取消、超时，token 只计了已报的）
_COUNTED = ("input_tokens", "output_tokens", "calls", "unpriced_calls", "unfinished_calls")


def add_usage(into: dict[str, Any] | None, more: Mapping[str, Any] | None) -> dict[str, Any]:
    """两份 providers/usage 汇总相加（by_model 逐个模型加）。"""
    out = copy.deepcopy(into) if into else {"by_model": {}, **dict.fromkeys(_COUNTED, 0), "cost_usd": 0.0}
    if not more:
        return out
    for key in _COUNTED:
        out[key] = int(out.get(key) or 0) + int(more.get(key) or 0)
    out["cost_usd"] = round(float(out.get("cost_usd") or 0) + float(more.get("cost_usd") or 0), 6)
    for model, row in (more.get("by_model") or {}).items():
        mine = out["by_model"].setdefault(model, {**dict.fromkeys(_COUNTED, 0), "cost_usd": 0.0})
        for key in _COUNTED:
            mine[key] = int(mine.get(key) or 0) + int(row.get(key) or 0)
        mine["cost_usd"] = round(float(mine.get("cost_usd") or 0) + float(row.get("cost_usd") or 0), 6)
    return out


async def _sealed(run_id: str, timeout: float) -> None:
    """等终态的运行封存好（manifest_hash 写上），最多 timeout 秒。"""
    from app.db.base import SessionLocal
    from app.db.models import Run

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with SessionLocal() as session:
            if await session.scalar(select(Run.manifest_hash).where(Run.id == run_id)):
                return
        await asyncio.sleep(0.1)


class TurnDriver:
    """一轮问数据。drive() 跑完一轮，结果在 turn 上；on_phase(阶段) 在每一步开始时调。子类给差异（见模块说明）。

    跑完之后这几项给调用方收尾用（问数据据此写 conversation_turns）：terminal 最后一次生成的结局操作；refusal 开流前的拒绝
    （HTTPException，带 detail / code）；launch_refused 发起被拒（LaunchRefused）；graph 最后一次建出的图；self_checked 服务端
    这一次自查过；last_run 最后一次运行的库里记录；last_review 最后一次复核的完整结果。"""

    def __init__(self, question: str, *, datasource_ids: list[str],
                 on_phase: Callable[[str], Awaitable[None]] | None = None,
                 provider: str | None = None, model: str | None = None, analysis_tools: bool = True) -> None:
        self.question = question
        self.datasource_ids = list(datasource_ids)
        self.on_phase = on_phase
        #: 助手模型（生成、复核都用它）：评测按变体覆盖，问数据用设置里的
        self.provider = provider
        self.model = model
        #: 这一轮给不给助手分析工具：透传给 api/copilot.answer_ops 的 analysis_tools。只给评测的「分析工具开、关」对照用
        #: （eval/answer.Driver 按变体设），问数据页不暴露这个开关，一直是真
        self.analysis_tools = analysis_tools
        self.turn = Turn()
        self.terminal: dict[str, Any] | None = None
        self.refusal: Exception | None = None
        self.launch_refused: Exception | None = None
        self.graph: dict[str, Any] | None = None
        self.self_checked = False
        self.last_run: Any = None
        self.last_review: dict[str, Any] | None = None

    async def _phase(self, phase: str) -> None:
        self.turn.phase = phase
        if self.on_phase is not None:
            try:
                await self.on_phase(phase)
            except Exception:  # noqa: BLE001 - 写进度失败不挡作答
                logger.exception("一轮作答：记进度出错")

    # ------------------------------------------------------------------ 子类给的差异

    def generate_in(self, instruction: str, base_graph: dict[str, Any] | None) -> Any:
        """这一次生成的请求（api/copilot.GenerateIn）。question 是这一轮原本的问题：重来时 instruction 是带着原因的那段话，
        自查按原本的问题判分析意图。"""
        from app.api.copilot import GenerateIn

        return GenerateIn(instruction=instruction, base_graph=base_graph, intent="answer", provider=self.provider,
                          model=self.model, datasource_ids=self.datasource_ids or None, question=self.question)

    def catalog_view(self) -> Any:
        """这一轮读哪一份数据目录（data/catalog_view.py），None 是当前目录。"""
        return None

    async def on_op(self, op: dict[str, Any]) -> None:
        """生成过程的每一条操作（含思考、心跳），收下之前先交给这里。"""

    async def on_retry(self, reason: str, *, kind: str, base_graph: dict[str, Any] | None,
                       detail: str | None = None) -> None:
        """要自动重来一次了（kind：empty 生成过程一个操作都没给、blocked 没过检查、review 复核判不可用）。"""

    def prepare_graph(self, graph: dict[str, Any]) -> dict[str, Any]:
        """发起之前改一下图（评测按变体写进写作模型）。"""
        return graph

    async def run_input(self, session: Any, graph: dict[str, Any]) -> dict[str, Any]:
        """入口字段怎么填（chat/turn.fill_input）。缺省单轮：前几轮的问答为空。"""
        from app.chat import turn as policy

        return policy.fill_input(graph, self.question, "")

    async def launch_args(self, session: Any, graph: dict[str, Any]) -> dict[str, Any]:
        """launch_run 的其余参数：来源、选项、发起人……可以抛 LaunchRefused（照发起被拒记）。"""
        raise NotImplementedError

    async def on_launched(self, run: Any) -> None:
        """运行发起了。"""

    async def on_interrupted(self, run_id: str, row: Any) -> Any | None:
        """运行停在断点上（审批、服务重启挂起）：交回接着等到的运行记录，或者记好路径、交回 None。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 生成

    async def _generate(self, instruction: str, base_graph: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str | None]:
        """跑一次生成过程，收下全部操作。开流前就被拒（HTTPException）交回原因。"""
        from fastapi import HTTPException

        from app.api.copilot import answer_ops

        payload = self.generate_in(instruction, base_graph)
        ops: list[dict[str, Any]] = []
        try:
            async for op in answer_ops(payload, catalog_view=self.catalog_view(), analysis_tools=self.analysis_tools):
                await self.on_op(op)
                if op.get("op") in ("thinking", "heartbeat"):
                    continue
                ops.append(op)
        except HTTPException as e:
            self.refusal = e
            return ops, str(e.detail)
        return ops, None

    def _take(self, ops: list[dict[str, Any]]) -> dict[str, Any] | None:
        """收下这一次生成的用量、查看记录、模型；交回结局（final / reply / error），只有反问的为 None。"""
        terminal = None
        for op in ops:
            kind = op.get("op")
            if kind == "usage":
                self.turn.usage["assistant"] = add_usage(self.turn.usage.get("assistant"),
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
        from app.db.base import SessionLocal
        from app.engine.launch import LaunchRefused, launch_run

        graph = self.prepare_graph(graph)
        try:
            async with SessionLocal() as session:
                inputs = payload if payload is not None else await self.run_input(session, graph)
                run = await launch_run(session, graph=graph, input_payload=inputs,
                                       **(await self.launch_args(session, graph)))
        except LaunchRefused as e:
            self.launch_refused = e
            self.turn.path, self.turn.reason = "run_failed", f"运行没有发起：{e.message}"
            return None
        self.turn.runs.append(run.id)
        self.turn.run_id = self.turn.current_run = run.id
        await self.on_launched(run)
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
                if row is not None and row.status != "interrupted" and not run_manager.is_active(run_id):
                    # 多进程：运行在别的进程里执行（审批之后被别的进程接着跑了），本进程等不到它的执行任务，回库里等封存
                    await _sealed(run_id, CANCEL_GRACE_S)
                async with SessionLocal() as session:
                    return await session.get(Run, run_id)
            await asyncio.sleep(0.2)

    async def _pending_approvals(self, run_id: str) -> int:
        from app.db.base import SessionLocal
        from app.db.models import Approval

        async with SessionLocal() as session:
            return int(await session.scalar(select(func.count()).select_from(Approval).where(
                Approval.run_id == run_id, Approval.status.in_(("pending", "answered")))) or 0)

    async def _follow(self, run_id: str) -> Any | None:
        """等这次运行到终态。成功结束的交回运行；别的结局记好路径和原因，交回 None。"""
        from app.engine.runner import END_REJECTED

        t = self.turn
        row = await self._wait(run_id)
        t.current_run = None
        self.last_run = row
        usage = dict((row.usage or {}) if row is not None else {})
        t.timings["run_wall_ms"] = t.timings.get("run_wall_ms", 0) + int(usage.get("wall_ms") or 0)
        t.timings["run_active_ms"] = t.timings.get("run_active_ms", 0) + int(
            usage.get("active_ms") or usage.get("duration_ms") or 0)
        if row is None:
            t.path, t.reason = "run_failed", "运行记录不见了"
            return None
        if row.status == "interrupted":
            return await self.on_interrupted(run_id, row)
        if row.status == "cancelled" and row.end_reason == END_REJECTED:
            t.path, t.reason = "rejected", "运行被驳回（人的决定，不算运行失败）"
            return None
        if row.status != "succeeded":
            t.path = "run_failed"
            t.reason = f"运行{'失败' if row.status == 'failed' else '被停止'}：{row.error or '没有记下原因'}"
            return None
        return row

    async def _run(self, graph: dict[str, Any], payload: dict[str, Any] | None = None) -> Any | None:
        """发起、等到终态。成功结束的交回运行；别的结局记好路径和原因，交回 None。"""
        run_id = await self._launch(graph, payload)
        if run_id is None:
            return None
        return await self._follow(run_id)

    # ------------------------------------------------------------------ 一轮

    async def drive(self, *, graph: dict[str, Any] | None = None, run_id: str | None = None, retry: bool = True,
                    instruction: str | None = None, base_graph: dict[str, Any] | None = None) -> Turn:
        from app.chat import turn as policy

        t = self.turn
        started = time.monotonic()
        instruction = instruction or self.question
        # 不建图、直接运行这张图（graph），或者不发起、接着等这次运行（run_id）：只有第一圈这样，重来还是照常建图
        direct, follow = graph, run_id
        try:
            while True:
                if direct is None and follow is None:
                    await self._phase("build")
                    building = time.monotonic()
                    ops, refused = await self._generate(instruction, base_graph)
                    t.timings["build_ms"] = t.timings.get("build_ms", 0) + int((time.monotonic() - building) * 1000)
                    terminal = self._take(ops)
                    self.terminal = terminal
                    if refused is not None:
                        t.path, t.reason, t.copilot_failed = "build_failed", f"助手没有开始：{refused}", True
                        return t
                    if terminal is None:
                        asked = next((o for o in reversed(ops) if o.get("op") == "ask"), None)
                        t.path, t.asked = "asked", {k: asked.get(k) for k in ("question", "options")} if asked else None
                        return t
                    kind = terminal.get("op")
                    if kind == "error":
                        # 生成过程一个操作都没给（只写了思考）：按重来一次的规矩带着原因重来（chat/turn.next_step）
                        step = terminal.get("next") or policy.next_step(terminal)
                        if step == policy.NEXT_RETRY and t.attempts + 1 < policy.MAX_ATTEMPTS:
                            # 原因是生成过程给的那句纠正（只写了思考、格式跑偏、用了工具调用……），附在问题后面重来，
                            # 不原样重发；诊断（error 的 detail）随重来的事件记下
                            reason = policy.error_reason(terminal)
                            t.attempts += 1
                            t.retries.append(reason)
                            instruction = policy.empty_retry_instruction(self.question, reason)
                            await self.on_retry(reason, kind="empty", base_graph=base_graph,
                                                detail=str(terminal.get("detail") or "") or None)
                            continue
                        t.path, t.reason, t.copilot_failed = "build_failed", str(terminal.get("message") or "助手本轮未完成"), True
                        return t
                    if kind == "reply":
                        t.path, t.reply = "reply", str(terminal.get("text") or "")
                        return t
                    if terminal.get("asked"):
                        self.graph = terminal.get("graph") or None
                        t.path, t.asked = "asked", dict(terminal["asked"])
                        return t
                    self.self_checked = any(o.get("op") == "check" for o in ops)
                    step = terminal.get("next") or policy.next_step(terminal, self_checked=self.self_checked)
                    built = terminal.get("graph") or {}
                    self.graph = built
                    if step == policy.NEXT_RETRY:
                        if t.attempts + 1 < policy.MAX_ATTEMPTS:
                            t.attempts += 1
                            t.retries.append(policy.blocked_reason(terminal))
                            instruction = policy.retry_instruction(self.question, policy.blocked_reason(terminal),
                                                                   policy.has_nodes(built))
                            base_graph = built if policy.has_nodes(built) else None
                            await self.on_retry(t.retries[-1], kind="blocked", base_graph=base_graph)
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
                    direct = built

                await self._phase("run")
                if follow is not None:
                    t.runs.append(follow)
                    t.run_id = t.current_run = follow
                    row = await self._follow(follow)
                else:
                    row = await self._run(direct)
                direct = follow = None
                if row is None:
                    return t

                await self._phase("review")
                reviewing = time.monotonic()
                self.last_review = None
                review = await self._review(row)
                spent = int((time.monotonic() - reviewing) * 1000)
                t.timings["review_ms"] = t.timings.get("review_ms", 0) + spent
                if review is not None:
                    self.last_review = review
                    t.review = {k: v for k, v in review.items() if k != "usage"}
                    t.usage["review"] = add_usage(t.usage.get("review"), review.get("usage"))
                    if retry and review.get("retry_instruction"):
                        t.attempts += 1
                        t.retries.append(policy.review_reason(review) or "复核判答案不可用")
                        instruction = str(review["retry_instruction"])
                        base_graph = row.graph if policy.has_nodes(row.graph) else None
                        await self.on_retry(t.retries[-1], kind="review", base_graph=base_graph)
                        continue
                t.path = "ran"
                return t
        finally:
            t.timings["total_ms"] = int((time.monotonic() - started) * 1000)

    async def _review(self, run: Any) -> dict[str, Any] | None:
        """复核（review_of_run，问数据和评测同一个函数）。复核自己出错不挡收尾：照实记一条失败的复核。交回 None 表示没有
        复核结论（问数据等不及的那种，见 chat/job.py）。"""
        from app.api.copilot import review_of_run
        from app.core.errors import describe_exception
        from app.db.base import SessionLocal

        try:
            async with SessionLocal() as session:
                return await review_of_run(session, run, question=self.question, provider=self.provider,
                                           model=self.model, attempt=self.turn.attempts)
        except Exception as e:  # noqa: BLE001
            logger.exception("一轮作答：复核运行 %s 出错", run.id)
            return {"verdict": "failed", "note": f"复核出错：{describe_exception(e)}", "signals": [],
                    "retry_instruction": None}


__all__ = ["CANCEL_GRACE_S", "Turn", "TurnDriver", "add_usage"]
