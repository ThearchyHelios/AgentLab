"""问数据的一轮在服务端跑（docs/design/orchestration-round/00-总方案.md §1）。

**为什么。** 一轮问数据（建图 → 发起运行 → 复核 → 按复核重来、落库）以前是浏览器在推（frontend/src/store/chat.ts）：网络一抖，
建图那条流断了、建了一半的图丢掉；发起运行、复核、重来、落库都要页面在，页面一关这一轮就停在 running，15 分钟后才被判成断了。
现在一轮是一个后台任务：页面只发起、只看（GET /api/chat/turns/{id}/stream 先回放、再接实时，断了按 seq 补齐），这一轮在不在
页面上都会跑完、复核完、落好库。

**流程和评测是同一份**（chat/drive.TurnDriver）。问数据这边的差异写在 ChatDriver：
- 生成带着对话（历史、上一轮的图）、限定的数据源；每一条操作写进事件日志（思考按 THINKING_FLUSH_S 合并成一条，几百条增量
  不逐条落库）；
- 发起：来源 chat（origin 记对话和这一轮），入口填问题和这一轮之前的几轮问答（conversations.run_history）；开登录后 user 角色照
  chat/launch.user_chat_run（白名单、自己的记忆范围、遮罩、只读）——和以前浏览器发 POST /api/runs 时一字不差；
- 停在审批上：照旧等人（评测是取消），审批过了接着等这次运行；没有待审批的断点（服务重启挂起）这一轮收成「服务重启，本轮已
  中断」，可以「继续运行」；
- 复核最多等 REVIEW_WAIT_S（同以前页面上的 25 秒）：等不到就先交原答案，复核那一次晚到了照样把用量记进这一轮（review.late）。

**事件日志**（chat/log.py；每条 {seq, type, data, ts}）：
  turn.attempt {mode, attempts, scope, max_steps?}  这一次尝试开始了（发起、重试本轮、重新运行、跑一下、继续运行、接着收尾）
  op {...}                       助手的一条操作原样（含查看、final、usage；心跳只落换了阶段的那一拍，同一阶段接着的只在实时流上发，
                                 见 chat/log.beat）
  stage {stage}                  到了哪一步：build 建图、run 运行、waiting 停在审批上等人、review 复核
  run {run_id, status, run_class} 发起了一次运行（页面据此接上运行自己的实时流看过程）
  review {result}                复核结论原样（含 usage、retry_instruction）
  turn.retry {kind, reason, base, attempt} 自动重来一次（kind：empty 一个操作都没给、blocked 没过检查、review 复核判不可用）
  end {outcome, turn, output, run} 这一轮到此为止：outcome 是 done | reply | asked | ready | error | cancelled | suspended，
                                 turn 是落库之后的那一行（同 GET /api/conversations/{id} 里的一轮），output 是完整成果
  review.late {usage}            等不及的那次复核晚到了：它的用量（已经并进那一行的 meta.usage.review）

**重启。** 还在建图的标中断（照实说「服务重启，本轮在生成工作流时中断」，可以重试本轮）；已经发起了运行的，重启后接着等那次
运行、照常复核收尾（运行本身的中断规则不变：服务重启时在跑的运行挂起，这一轮就收成「服务重启，本轮已中断」）。接着收尾可能要
调模型（复核、按复核重来），所以和调度循环同一个开关（AGENTLAB_SCHEDULER）：拷来的数据目录（检查脚本的 --stacks）启动时不替人
做，这几轮停着，等人打开这段对话时页面再请服务端接着收尾（mode=resume）。

**并发。** 归「交互」类（总方案 §5 W2-b）：这一波还是进程内的 core/job_runtime（一个 asyncio 任务一轮，不占数据源的队），
和以前浏览器各推各的一样不设上限；第 2 波按库里的计数认领、按类别给并发上限和优先级（问数据优先于评测），租约到期别的进程接手。
同时来的几次发起（两个标签页、连点几下）在库里排队：发起的核对和写入在一个事务里、第一句先占住这段对话（hold_conversation），
同一段对话只起得来一轮、同一轮只排得上一次，第一条事件也在这个事务里写；事件的 seq 由任务行上的计数器发（chat/log.add）；
同一行的几处写入（任务、晚到的复核、下一次尝试）先占住那一行再读改写（hold_turn），用量按增量记在库里那一份上，谁也不盖掉谁；
同一轮在这个进程里不起第二个后台任务（ChatTurnManager.start）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import log as turn_log
from app.chat import record
from app.chat.drive import TurnDriver
from app.core import job_runtime as runtime
from app.db.base import SessionLocal, current_owner, utcnow

logger = logging.getLogger(__name__)

#: 发起一轮的几种方式：ask 新问；retry 重试本轮（带着上一次的原因重建）；rerun 原图重新运行；max_steps 放宽步数重新运行；
#: run 只建了图、用户点「运行」；continue 从断点接着跑（失败的、服务重启挂起的）；resume 接着等这一轮的那次运行、复核收尾
MODES = ("ask", "retry", "rerun", "max_steps", "run", "continue", "resume")
#: 复核之后会自动重来的方式（用户点的「重新运行」「跑一下」「继续运行」照以前不自动重来）
AUTO_RETRY_MODES = ("ask", "retry")
#: 并发类别（第 2 波按它给上限和优先级）
CONCURRENCY_CLASS = "interactive"
#: 最多等复核多久（秒）：等不到先交原答案（同以前页面上的 REVIEW_TIMEOUT_MS）
REVIEW_WAIT_S = 25.0
#: 思考增量攒多久写一条
THINKING_FLUSH_S = 1.0
#: 停在审批上时多久回库里看一次（审批过了运行事件总线也会叫醒它）
APPROVAL_POLL_S = 5.0
#: 停止时最多等后台任务收尾多久
STOP_GRACE_S = 10.0


# --------------------------------------------------------------------------
# 发起人：服务端接着做时按这个人的身份（遮罩、只读、对话归属都看它）
# --------------------------------------------------------------------------


def principal_snapshot() -> dict[str, Any]:
    """这次请求的身份快照（api/deps.Principal 里和问数据有关的几项）。访问密钥调不到这些接口，不记密钥的那几项。"""
    from app.api.deps import current_principal

    p = current_principal()
    return {"username": p.username, "role": p.role, "display_name": p.display_name, "user_id": p.user_id,
            "groups": list(p.groups), "verified": p.verified, "mode": p.mode, "via": p.via}


def principal_of(snapshot: Mapping[str, Any] | None) -> Any:
    from app.api.deps import Principal

    s = dict(snapshot or {})
    if not s:
        return Principal(username=None, role="admin", verified=False, mode="off", via="off")
    return Principal(username=s.get("username"), role=str(s.get("role") or "user"), display_name=s.get("display_name"),
                     user_id=s.get("user_id"), groups=tuple(s.get("groups") or ()), verified=bool(s.get("verified")),
                     mode=str(s.get("mode") or "off"), via=str(s.get("via") or "session"))


async def current_principal(snapshot: Mapping[str, Any] | None) -> tuple[Any, str | None]:
    """发起时的身份快照 → 接手这一刻的身份：(身份, 不能接着做的原因)。

    关着登录时发起的（快照里 mode 是 off、没有用户名）照快照：没有账号可核对。开着登录的按用户名读现在的账号：不在了、
    停用了交回原因；角色、组、显示名换成现在的（降了角色就按降了之后的做）；按现在的身份已经用不了问数据（使用者、管理员
    没开问数据，同 api/deps._chat_scope）也交回原因。认证方式、是否核实过照快照。"""
    from app.api.deps import Principal
    from app.auth import user_can_chat
    from app.auth.sessions import groups_of
    from app.db.models_identity import User

    s = dict(snapshot or {})
    name = s.get("username")
    if not s or str(s.get("mode") or "off") == "off" or not name:
        return principal_of(snapshot), None
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == name))).scalar_one_or_none()
        if user is None or user.disabled:
            return None, f"发起这一轮的账号「{name}」已{'停用' if user is not None else '不存在'}，本轮没有接着收尾"
        groups = await groups_of(session, user.id)
    principal = Principal(username=user.username, role=user.role, display_name=user.display_name or user.username,
                          user_id=user.id, groups=groups, verified=bool(s.get("verified")),
                          mode=str(s.get("mode") or "off"), via=str(s.get("via") or "session"))
    if not principal.at_least("builder") and not (principal.at_least("user") and user_can_chat()):
        return None, f"发起这一轮的账号「{name}」已经没有问数据的权限，本轮没有接着收尾"
    return principal, None


# --------------------------------------------------------------------------
# 驱动：问数据和评测不一样的那几项
# --------------------------------------------------------------------------


class ChatDriver(TurnDriver):
    """问数据的一轮（流程在 TurnDriver，见模块说明）。"""

    def __init__(self, execution: ChatTurnExecution, *, question: str, datasource_ids: list[str]) -> None:
        super().__init__(question, datasource_ids=datasource_ids, on_phase=execution.on_phase)
        self.execution = execution
        self._thinking = ""
        self._thinking_at = time.monotonic()
        #: 最近一次落库的心跳的阶段：同一阶段接着的心跳不落库（on_op）
        self._beat_phase: Any = None
        #: 这一轮的查看记录（存进 meta.looks）
        self.looks: list[dict[str, Any]] = []
        #: 等不及的那次复核（还在跑）：收尾之后由它把用量记进这一轮
        self.late_review: asyncio.Task[Any] | None = None

    def generate_in(self, instruction: str, base_graph: dict[str, Any] | None) -> Any:
        from app.api.copilot import GenerateIn

        return GenerateIn(instruction=instruction, base_graph=base_graph, intent="answer",
                          conversation_id=self.execution.conversation_id, datasource_ids=self.datasource_ids or None,
                          question=self.question)

    async def on_op(self, op: dict[str, Any]) -> None:
        kind = op.get("op")
        if kind == "thinking":
            self._thinking += str(op.get("delta") or "")
            if time.monotonic() - self._thinking_at >= THINKING_FLUSH_S:
                await self._flush_thinking()
            return
        await self._flush_thinking()
        if kind == "heartbeat":
            if op.get("phase") == self._beat_phase:
                # 同一阶段接着的心跳是纯保活：只在实时流上发、不落库（chat/log.beat；以前每 3 秒落一条）
                turn_log.beat(self.execution.turn_id, op)
                return
            # 换了阶段的那一拍（规划 → 查看 → 搭建……）说的是到了哪一步，照落：刷新、重开时过程里那一行阶段要回放得出来
            self._beat_phase = op.get("phase")
        await self.execution.emit("op", op)
        if kind == "look" and (look := record.look_event(op)) is not None:
            self.looks.append(look)
        elif kind == "usage":
            await self.execution.add_usage("copilot", op)
        elif kind == "final" and not op.get("asked"):
            await self.execution.save(graph=op.get("graph"), explanation=str(op.get("explanation") or ""))

    async def _flush_thinking(self) -> None:
        if self._thinking:
            text, self._thinking = self._thinking, ""
            await self.execution.emit("op", {"op": "thinking", "delta": text})
        self._thinking_at = time.monotonic()

    async def _generate(self, instruction: str, base_graph: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str | None]:
        self._beat_phase = None   # 每一次生成从头算：第一拍心跳照落（页面按它画「规划」那一行）
        try:
            return await super()._generate(instruction, base_graph)
        finally:
            await self._flush_thinking()

    async def on_retry(self, reason: str, *, kind: str, base_graph: dict[str, Any] | None,
                       detail: str | None = None) -> None:
        from app.chat import turn as policy

        # 换下来的那次运行交出去并进 earlier；这一次从「还没有运行」起，收尾时不再把它当这一次的运行记一遍
        run, self.last_run = (self.last_run if kind == "review" else None), None
        await self.execution.retrying(reason, kind=kind, base=policy.has_nodes(base_graph), attempt=self.turn.attempts,
                                      run=run, detail=detail)

    async def run_input(self, session: AsyncSession, graph: dict[str, Any]) -> dict[str, Any]:
        """入口填问题和这一轮之前的几轮问答（conversations.run_history，同以前页面上的 historyOf）：重试、重新运行较早的
        一轮时，后面接着问过的几轮不算进来。"""
        from app.api.conversations import run_history
        from app.chat import turn as policy

        history = await run_history(session, self.execution.conversation_id, before_seq=self.execution.turn_seq)
        return policy.fill_input(graph, self.question, history)

    async def launch_args(self, session: AsyncSession, graph: dict[str, Any]) -> dict[str, Any]:
        from app.api.deps import restricted
        from app.chat.launch import ChatRunRefused, user_chat_run
        from app.engine.launch import LaunchRefused

        args: dict[str, Any] = {"actor": self.execution.actor, "origin_kind": "chat",
                                "origin": {"conversation_id": self.execution.conversation_id,
                                           "turn_id": self.execution.turn_id}}
        if restricted():
            try:
                args["memory_scope"], args["options"] = user_chat_run(graph, self.execution.actor)
            except ChatRunRefused as e:
                raise LaunchRefused(403, str(e)) from e
        return args

    async def _launch(self, graph: dict[str, Any], payload: dict[str, Any] | None = None) -> str | None:
        """发起那一下不让停止打断到一半：运行建好了、这一轮却不知道它，就成了没人管、照样计费的运行。停止正好落在这时，
        等它发起完、记下运行（on_launched），再照常去取消它（_stopped）。"""
        task = asyncio.ensure_future(super()._launch(graph, payload))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await task
            raise

    async def on_launched(self, run: Any) -> None:
        await self.execution.launched(run)

    async def on_interrupted(self, run_id: str, row: Any) -> Any | None:
        """停在审批上：照旧等人，审批过了接着等这次运行。没有待审批（服务重启挂起的）：这一轮收成中断，可以继续运行。"""
        t = self.turn
        if not await self._pending_approvals(run_id):
            t.path, t.reason = "suspended", record.SUSPENDED
            return None
        await self._phase("waiting")
        await self._await_resume(run_id)
        await self._phase("run")
        t.current_run = run_id
        return await self._follow(run_id)

    async def _await_resume(self, run_id: str) -> None:
        """等到这次运行不再停在审批上（审批过了、被驳回、被放弃）。运行的事件总线叫醒，另外隔一拍回库里看一次。"""
        from app.core.bus import bus
        from app.db.models import Run

        with bus.attach(run_id) as sub:
            while True:
                async with SessionLocal() as session:
                    row = await session.get(Run, run_id)
                if row is None or row.status != "interrupted" or not await self._pending_approvals(run_id):
                    return
                try:
                    await asyncio.wait_for(anext(aiter(sub.stream())), timeout=APPROVAL_POLL_S)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass

    async def _review(self, run: Any) -> dict[str, Any] | None:
        """复核最多等 REVIEW_WAIT_S：等到了交回结论；等不到交回 None（先交原答案），那一次接着在后台跑完、记用量。复核
        自己出错也交回 None：复核是补一层说明，它失败了不能把已经到手的结果一起带走（同以前页面上的做法）。"""
        from app.api.copilot import review_of_run

        async def review() -> dict[str, Any] | None:
            try:
                async with SessionLocal() as session:
                    return await review_of_run(session, run, question=self.question, attempt=self.turn.attempts)
            except Exception:  # noqa: BLE001
                logger.exception("问数据：复核运行 %s 出错", run.id)
                return None

        task = asyncio.ensure_future(review())
        try:
            done, _ = await asyncio.wait({task}, timeout=REVIEW_WAIT_S)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task in done:
            result = task.result()
            if result is not None:
                await self.execution.emit("review", {"result": result})
                await self.execution.add_usage("review", result.get("usage"))
            return result
        self.late_review = task
        return None


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------


class ChatTurnExecution(runtime.JobRun):
    """一轮问数据在后台的一次执行。"""

    kind = "chat_turn"

    def __init__(self, job_id: str) -> None:
        from app.db.models_chat import ChatTurnJob

        super().__init__(job_id)
        self.model = ChatTurnJob
        self.turn_id = job_id
        self.conversation_id = ""
        self.actor: str | None = None
        self.mode = "ask"
        self.request: dict[str, Any] = {}
        #: 这一轮在对话里排第几（运行入口的 history 只取它之前的几轮）
        self.turn_seq = 0
        self.run_id: str | None = None
        self.run_class: str | None = None
        self.driver: ChatDriver | None = None
        #: end 已经写了：之后的停止只改任务状态，不再改那一行
        self.ended = False

    def lane_of(self, job: Any) -> str | None:
        return None   # 不占数据源的队：问数据的一轮不挡起草、剖析，起草也不挡问数据（同以前浏览器发起时）

    # ---------------- 写库 ----------------

    async def emit(self, type_: str, data: dict[str, Any] | None = None) -> int:
        """追加一条事件（和这一轮的库写入排在同一条队里，停止时不会写到一半）。"""
        return await self._write(turn_log.append, self.turn_id, type_, data or {})

    async def save(self, *, meta: Mapping[str, Any] | None = None, usage: list[tuple[Any, ...]] | None = None,
                   **fields: Any) -> dict[str, Any]:
        """改 conversation_turns 那一行：fields 照写，meta 并进去（值为 None 的键删掉），顺手记 meta.at；usage 是对 meta.usage 的
        几步改动（见 _apply_usage），在库里现有的那一份上做。交回改完之后的 meta.usage。"""
        return await self._write(_save_turn, self.turn_id, fields, dict(meta or {}), list(usage or []))

    async def _job(self, **values: Any) -> None:
        async def go(session: AsyncSession) -> None:
            from app.db.models_chat import ChatTurnJob

            await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == self.turn_id).values(**values)
                                  .execution_options(synchronize_session=False))
            await session.commit()
        await self._write(go)
        turn_log.notify(self.turn_id)

    async def add_usage(self, part: str, usage: Mapping[str, Any] | None) -> None:
        """这一轮又花了一笔（助手建图、复核）：在库里那一份上加，不拿内存里的拷贝盖回去——晚到的复核也往同一份上记"""
        if not isinstance(usage, Mapping):
            return
        await self.save(usage=[("add", part, dict(usage))])

    async def on_phase(self, phase: str) -> None:
        await self._job(stage=phase)
        await self.emit("stage", {"stage": phase})

    async def launched(self, run: Any) -> None:
        self.run_id, self.run_class = run.id, run.run_class
        await self._job(run_id=run.id)
        await self.save(run_id=run.id, status="running", usage=[("drop", "run")],
                        meta={"runId": run.id, "runClass": run.run_class, "runStatus": run.status})
        await self.emit("run", {"run_id": run.id, "status": run.status, "run_class": run.run_class})

    async def retrying(self, reason: str, *, kind: str, base: bool, attempt: int, run: Any = None,
                       detail: str | None = None) -> None:
        """自动重来一次：换下来的那次运行的用量并进 earlier；这一行回到「还没有运行」。detail：生成过程一个操作都没给时的
        诊断（结束原因、正文和思考的长度、跳过的行……，不含原文），随 turn.retry 事件记下。"""
        ops: list[tuple[Any, ...]] = [("set", "run", record.usage_total(run.usage) or {})] if run is not None else []
        self.run_id = None
        await self._job(attempt=attempt, run_id=None)
        usage = await self.save(run_id=None, meta={"runId": None, "runStatus": None}, usage=[*ops, ("retire",)])
        await self.emit("turn.retry", {"kind": kind, "reason": reason, "base": base, "attempt": attempt, "usage": usage,
                                       **({"detail": detail} if detail else {})})

    # ---------------- 执行 ----------------

    async def _run(self) -> None:
        try:
            await self._execute()
        finally:
            turn_log.forget(self.turn_id)   # 进程里记的最近一拍心跳（chat/log.beat）跟着这一次执行收掉

    async def _execute(self) -> None:
        from app.api.deps import set_principal
        from app.chat import turn as policy
        from app.db.models import ConversationTurn
        from app.db.models_chat import ChatTurnJob

        async with SessionLocal() as session:
            if not await runtime.claim(session, ChatTurnJob, self.turn_id):
                return   # 开始之前就被停止了
            job = await session.get(ChatTurnJob, self.turn_id)
            row = await session.get(ConversationTurn, self.turn_id)
        if job is None or row is None:
            return
        self.conversation_id, self.actor, self.mode = job.conversation_id, job.actor, job.mode
        self.request = dict(job.request or {})
        self.turn_seq = row.seq
        self.run_id = job.run_id
        # 这一轮按发起的人的身份跑：answer_ops 里的 restricted()、对话归属，发起运行时的遮罩、只读都看它。按这一刻的账号重新核对
        # （current_principal）：服务重启后接着收尾时，快照是发起那一刻的，之后停用了、降了角色的不能照快照接着做
        principal, refused = await current_principal(job.principal)
        if refused is not None:
            await self._identity_refused(refused)
            return
        set_principal(principal)
        scope = [s.get("id") for s in self.request.get("scope") or [] if isinstance(s, Mapping) and s.get("id")]
        driver = self.driver = ChatDriver(self, question=row.question, datasource_ids=scope)
        driver.turn.attempts = int(job.attempt or 0)
        retry = self.mode in AUTO_RETRY_MODES
        graph = row.graph if policy.has_nodes(row.graph) else None
        if self.request.get("follow") and job.run_id:
            # 接着等这次运行（继续运行、接着收尾、服务重启后接手）：自动重来的规矩照这一轮原来的方式
            await driver.drive(run_id=job.run_id, retry=retry)
        elif self.mode in ("rerun", "max_steps", "run") and graph is not None:
            if self.mode == "max_steps":
                graph = record.with_max_steps(graph, int(self.request.get("max_steps") or 0))
                await self.save(graph=graph)
            await driver.drive(graph=graph, retry=False)
        elif self.mode == "retry":
            await driver.drive(instruction=str(self.request.get("instruction") or row.question),
                               base_graph=graph if self.request.get("base") else None)
        else:
            await driver.drive()
        await self._conclude()

    async def _conclude(self) -> None:
        """按这一轮的结局写那一行、发 end、任务记成功。"""
        driver = self.driver
        assert driver is not None
        fields, meta, outcome, output, usage = await _settled(driver, self)
        await self.save(meta=meta, usage=usage, **fields)
        await self._end(outcome, output)
        # 晚到的复核先登记、再改任务状态：实时流看到任务结束时，要知道还得等这一次复核回来（库里也记一笔：别的进程上的流）
        if driver.late_review is not None:
            _track_late(self.turn_id, driver.late_review)
            await _mark_late(self.turn_id, True)
        await self._job(status="succeeded", stage=None, finished_at=utcnow())

    async def _end(self, outcome: str, output: Any = None) -> None:
        turn, run = await self._write(_snapshot, self.turn_id, self.run_id)
        await self.emit("end", {"outcome": outcome, "turn": turn, "output": output, "run": run})
        self.ended = True

    async def _stopped(self) -> None:
        reason = self.stop_reason
        if reason == "shutdown":
            # 服务关停：不改这一轮，启动时照「重启」的规矩整理（见模块说明）
            return
        driver = self.driver
        run_id = self.run_id or (driver.turn.current_run if driver else None)
        if run_id:
            from app.engine.runner import run_manager

            try:
                await run_manager.cancel(run_id, actor=self.actor)
            except Exception:  # noqa: BLE001 - 取消运行是收尾，出错只记日志
                logger.exception("问数据：停止这一轮时取消运行 %s 出错", run_id)
        if reason == "deleted":
            return   # 对话删了：那一行已经不在
        if driver is not None and driver.late_review is not None:
            driver.late_review.cancel()
        await self._settle()
        if not self.ended:
            await self.save(status="error", error=record.CANCELLED,
                            meta={"outcome": "cancelled", "runStatus": "cancelled" if run_id else None,
                                  **_timing(self.request)})
            await self._end("cancelled")
        await self._job(status="cancelled", stage=None, finished_at=utcnow())

    async def _identity_refused(self, reason: str) -> None:
        """发起这一轮的账号接手时已经停用、或者已经没有问数据的权限：照实标中断、写清原因，不再往下做（不建图、不等运行、
        不复核）。已经发起的那次运行不动——停在审批上的照旧等人，在记录页照常看得到；这一轮不替一个已经无权的人收它的尾。"""
        await self._settle()
        if not self.ended:
            await self.save(status="error", error=reason,
                            meta={"cause": {"kind": "identity", "message": reason}, **_timing(self.request)})
            await self._end("error")
        await self._job(status="interrupted", stage=None, error=reason, finished_at=utcnow())

    async def _failed(self, reason: str) -> None:
        await self._crash_out(reason)

    async def _crashed(self, e: Exception) -> None:
        from app.core.errors import describe_exception

        logger.exception("问数据：这一轮的后台任务出错", exc_info=e)
        await self._crash_out(describe_exception(e))

    async def _crash_out(self, message: str) -> None:
        await self._settle()
        if not self.ended:
            await self.save(status="error", error=message,
                            meta={"cause": {"kind": "crash", "message": message}, **_timing(self.request)})
            await self._end("error")
        await self._job(status="failed", stage=None, error=message, finished_at=utcnow())


class ChatTurnManager(runtime.JobManager):
    def __init__(self) -> None:
        super().__init__(ChatTurnExecution, label="问数据的一轮", name="chat-turn")

    def model(self) -> Any:
        from app.db.models_chat import ChatTurnJob

        return ChatTurnJob

    def start(self, job_id: str) -> bool:  # type: ignore[override]
        """同一轮在这个进程里还有一个后台任务活着就不起第二个、也不把登记换成新的（换了的话，停止只停得到后起的那个，
        先起的那个跑完把「已取消」盖成「已完成」）。起了交回 True。"""
        if self.running(job_id):
            return False
        super().start(job_id)
        return True


manager = ChatTurnManager()

#: 等不及的复核还在后台跑的几轮：{turn_id: task}。它们的实时流在复核回来（记上用量）之前不关
_late: dict[str, asyncio.Task[Any]] = {}


def _track_late(turn_id: str, task: asyncio.Task[Any]) -> None:
    async def finish() -> None:
        # 先把用量记进那一行、发 review.late，再出登记、叫醒订阅方：反过来的话，开着的实时流醒来看到「不在进行中了」，
        # 先发了 stream.end 走掉，review.late 就到不了它
        try:
            try:
                result = await task
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("问数据：晚到的复核出错")
                return
            usage = (result or {}).get("usage") if isinstance(result, Mapping) else None
            if not usage:
                return
            async with SessionLocal() as session:
                # 加在库里现有的那一份上（这一轮可能已经在重试，那一次的用量也在往同一份上记）
                await _save_turn(session, turn_id, {}, {}, [("add", "review", dict(usage))], touch_meta=False)
            async with SessionLocal() as session:
                await turn_log.append(session, turn_id, "review.late", {"usage": usage})
        except Exception:  # noqa: BLE001 - 记账失败只记日志
            logger.exception("问数据：记晚到的复核用量出错")
        finally:
            if _late.get(turn_id) is watcher:
                _late.pop(turn_id, None)
            turn_log.notify(turn_id)

    watcher = asyncio.ensure_future(finish())
    _late[turn_id] = watcher
    watcher.add_done_callback(lambda _t: _later(_mark_late(turn_id, False)))


async def _mark_late(turn_id: str, pending: bool) -> None:
    """晚到的复核记在任务行上（late_review_at）：多进程时别的进程上的流也知道还要等它（stream_status）。"""
    from app.db.models_chat import ChatTurnJob

    try:
        async with SessionLocal() as session:
            await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == turn_id).values(
                late_review_at=utcnow() if pending else None).execution_options(synchronize_session=False))
            await session.commit()
    except Exception:  # noqa: BLE001 - 记不上只是别的进程上的流可能早一点结束
        logger.exception("问数据：记晚到的复核状态出错")
    if not pending:
        turn_log.notify(turn_id)


def _later(coro: Any) -> None:
    with contextlib.suppress(RuntimeError):
        asyncio.ensure_future(coro)


def late_pending(turn_id: str) -> bool:
    """本进程里晚到的复核还在不在跑。"""
    task = _late.get(turn_id)
    return task is not None and not task.done()


async def late_waiting(job: Any) -> bool:
    """晚到的复核还要不要等：本进程里在跑，或者库里记着、记它的进程（这一轮的属主）还活着。"""
    if late_pending(job.id):
        return True
    if getattr(job, "late_review_at", None) is None:
        return False
    from app.cluster import state

    return (await state.live_for([job.owner])).alive(job.owner)


# --------------------------------------------------------------------------
# 写那一行
# --------------------------------------------------------------------------


def _apply_usage(usage: Mapping[str, Any] | None, ops: list[tuple[Any, ...]]) -> dict[str, Any]:
    """在一份 meta.usage 上做几步改动：("add", 哪一份, 用量) 加上；("set", 哪一份, 用量) 换成；("drop", 哪一份) 去掉；
    ("retire",) 当前这次运行换下来了（run 并进 earlier，同 record.retire_run）。"""
    out = dict(usage or {})
    for op in ops:
        kind = op[0]
        if kind == "add":
            out[op[1]] = record.add_total(out.get(op[1]), op[2])
        elif kind == "set":
            out[op[1]] = dict(op[2] or {})
        elif kind == "drop":
            out.pop(op[1], None)
        elif kind == "retire":
            out = record.retire_run(out)
    return out


async def hold_turn(session: AsyncSession, turn_id: str) -> None:
    """占住这一行再读改写（同一行的几处写入——这一轮的任务、晚到的复核、发起下一次尝试——一个接一个来，谁也不盖掉谁）：
    第一句就写它，SQLite 上拿到写锁、PostgreSQL 上锁住这一行，别的写入等它提交。"""
    from app.db.models import ConversationTurn

    await session.execute(update(ConversationTurn).where(ConversationTurn.id == turn_id).values(updated_at=utcnow())
                          .execution_options(synchronize_session=False))


async def _save_turn(session: AsyncSession, turn_id: str, fields: Mapping[str, Any], meta: Mapping[str, Any],
                     usage: list[tuple[Any, ...]] | None = None, *, touch_meta: bool = True) -> dict[str, Any]:
    from app.db.models import ConversationTurn

    await hold_turn(session, turn_id)
    row = await session.get(ConversationTurn, turn_id, populate_existing=True)
    if row is None:
        await session.rollback()
        return {}
    for key, value in fields.items():
        setattr(row, key, value)
    merged = dict(row.meta or {}) if isinstance(row.meta, dict) else {}
    for key, value in meta.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    if usage:
        merged["usage"] = _apply_usage(merged.get("usage"), usage)
    merged["v"] = 1
    if touch_meta:
        merged["at"] = int(time.time() * 1000)
    row.meta = merged
    await session.commit()
    return dict(merged.get("usage") or {})


async def _snapshot(session: AsyncSession, turn_id: str, run_id: str | None) -> tuple[dict[str, Any] | None,
                                                                                         dict[str, Any] | None]:
    """end 里带的那一行（同 GET /api/conversations/{id} 里的一轮）和这次运行的摘要。"""
    from app.api.conversations import turn_out
    from app.db.models import ConversationTurn, Run

    row = await session.get(ConversationTurn, turn_id)
    turn = (await turn_out(session, row)).model_dump(mode="json") if row is not None else None
    run = await session.get(Run, run_id) if run_id else None
    summary = None if run is None else {
        "id": run.id, "status": run.status, "run_class": run.run_class, "usage": run.usage or {},
        "error": run.error, "end_reason": run.end_reason,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    return turn, summary


def _timing(request: Mapping[str, Any]) -> dict[str, Any]:
    """这一次尝试的墙钟毫秒（meta.ms）：从发起这一次算（继续运行接着上次的算），到现在为止；补交付的到运行结束为止。"""
    started, ended = request.get("started_ms"), request.get("ended_ms")
    if not isinstance(started, (int, float)):
        return {}
    end = int(ended) if isinstance(ended, (int, float)) else int(time.time() * 1000)
    return {"ms": max(0, end - int(started))}


async def _queries(run_id: str | None) -> int | None:
    """这次运行查了几次库（db_query 的每一次调用，同页面的 countQueries）。"""
    from app.core.events import EventType
    from app.db.models import RunEvent

    if not run_id:
        return None
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.TOOL_START))).scalars().all()
    return sum(1 for d in rows if isinstance(d, dict) and str(d.get("tool") or "").startswith("db_query"))


async def _rejected_by(run_id: str) -> dict[str, Any]:
    """被驳回结束的运行：run.cancelled 带的谁、哪一步、备注、怎么驳回的（页面据此写「张工 驳回了「查数」：…」）。"""
    from app.core.events import EventType
    from app.db.models import RunEvent

    async with SessionLocal() as session:
        data = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.RUN_CANCELLED).order_by(RunEvent.seq.desc())
            .limit(1))).scalar_one_or_none()
    data = data if isinstance(data, dict) else {}
    return {k: data[k] for k in ("actor", "label", "note", "decision", "node_id", "kind") if data.get(k) is not None}


async def _run_failed_detail(run_id: str) -> str | None:
    from app.core.events import EventType
    from app.db.models import RunEvent

    async with SessionLocal() as session:
        data = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.RUN_FAILED).order_by(RunEvent.seq.desc())
            .limit(1))).scalar_one_or_none()
    return str(data.get("detail")) if isinstance(data, dict) and data.get("detail") else None


async def _settled(driver: ChatDriver, execution: ChatTurnExecution
                   ) -> tuple[dict[str, Any], dict[str, Any], str, Any, list[tuple[Any, ...]]]:
    """这一轮的结局 → (那一行要写的列, 并进 meta 的, outcome, 完整成果, 对 meta.usage 的改动)。和以前页面在各个结局处落库写的
    一一对应。用量里助手、复核那几份是随做随记的（add_usage），这里只记这次运行的那一份。"""
    from app.engine.runner import END_REJECTED

    t = driver.turn
    term = driver.terminal or {}
    run = driver.last_run
    meta: dict[str, Any] = {"looks": driver.looks or None, **_timing(execution.request)}
    fields: dict[str, Any] = {}
    output: Any = None
    path = t.path
    if path == "reply":
        text = t.reply or ""
        fields.update(status="done", answer=text, review={})
        meta.update(noQuery=True, runId=None)
        return fields, meta, "reply", {"answer": text}, []
    if path == "asked":
        asked = t.asked or {}
        question = str(asked.get("question") or "").strip()
        fields.update(status="done", answer=question, review={})
        meta.update(asked=dict(asked) or True, runId=None)
        outcome = "asked"
        if term.get("op") == "final":
            # 改了图又问了用户、或者只问不改（图原样交回）：图摆着等「运行」，问题作为这一轮的内容
            fields.update(graph=driver.graph or term.get("graph"), explanation=str(term.get("explanation") or ""))
            outcome = "ready"
        return fields, meta, outcome, {"answer": question} if question else None, []
    if path == "ready":
        fields.update(status="done", graph=driver.graph, explanation=str(term.get("explanation") or ""), review={})
        meta["runId"] = None
        return fields, meta, "ready", None, []
    if path == "build_failed":
        if driver.refusal is not None:
            message = str(getattr(driver.refusal, "detail", "") or driver.refusal)
            cause = {"kind": "refused", "message": message, "code": getattr(driver.refusal, "code", None)}
            error = message
        elif term.get("op") == "error":
            error = str(term.get("message") or "助手本轮未完成")
            cause = {"kind": "copilot", "message": error, "hint": term.get("hint"), "detail": term.get("detail"),
                     "code": term.get("code"), "diagnostics": term.get("diagnostics")}
        else:
            issues = [str(i.get("message") or "") for i in term.get("issues") or []
                      if isinstance(i, Mapping) and i.get("level") == "error"]
            cause = {"kind": "blocked", "issues": issues, "self_checked": driver.self_checked}
            error = t.reason or "生成的工作流没有通过校验"
            if driver.graph:
                fields.update(graph=driver.graph, explanation=str(term.get("explanation") or ""))
        fields.update(status="error", error=error, review={})
        meta.update(cause={k: v for k, v in cause.items() if v is not None}, runId=None)
        return fields, meta, "error", None, []

    # 发起过运行的结局：这次运行的用量记成 run（换下来的早在重来时并进了 earlier）
    usage: list[tuple[Any, ...]] = [("set", "run", record.usage_total(run.usage) or {})] if run is not None else []
    run_id = run.id if run is not None else execution.run_id
    meta.update(runId=run_id, runClass=getattr(run, "run_class", None) or execution.run_class,
                runStatus=getattr(run, "status", None), queries=await _queries(run_id))
    if path == "run_failed":
        refused = driver.launch_refused
        if refused is not None:
            message = getattr(refused, "message", str(refused))
            fields.update(status="error", error=message, review={})
            meta.update(cause={"kind": "start", "message": message, "code": getattr(refused, "code", None),
                               "status": getattr(refused, "status_code", None)}, runId=None, runStatus=None, queries=None)
            return fields, meta, "error", None, usage
        if run is None:
            fields.update(status="error", error=record.GONE, review={})
            meta.update(cause={"kind": "gone"}, runStatus="gone")
            return fields, meta, "error", None, usage
        if run.status == "cancelled" and run.end_reason != END_REJECTED:
            fields.update(status="error", error=record.CANCELLED, review={})
            meta["outcome"] = "cancelled"
            return fields, meta, "cancelled", None, usage
        fields.update(status="error", error=run.error or "运行失败", review={})
        meta["cause"] = {k: v for k, v in {"kind": "run", "error": run.error or "",
                                           "detail": await _run_failed_detail(run.id)}.items() if v is not None}
        return fields, meta, "error", None, usage
    if path == "rejected":
        fields.update(status="error", error=record.CANCELLED, review={})
        meta.update(outcome="cancelled", rejected=await _rejected_by(run.id) or {"decision": "reject"})
        return fields, meta, "cancelled", None, usage
    if path == "suspended":
        fields.update(status="error", error=record.SUSPENDED, review={})
        meta.update(outcome="suspended", runStatus="interrupted")
        return fields, meta, "suspended", None, usage

    # 跑完了：成果以库里那份为准（事件里那份为了推送体积被切过）
    output = run.output if isinstance(run.output, dict) else {}
    review = driver.last_review if driver.late_review is None else None
    rewritten = (review or {}).get("answer") if (review or {}).get("verdict") != "ok" else None
    fields.update(status="done", answer=rewritten or record.answer_text(output), review=review or {})
    meta.update(issuance=record.slim_issuance(output.get("_issuance")),
                evidence=True if record.has_evidence(output) else None)
    return fields, meta, "done", output, usage


# --------------------------------------------------------------------------
# 发起、停止、查询（接口层 api/chat_turns.py 调）
# --------------------------------------------------------------------------


class TurnRefused(Exception):
    """不发起：status_code、message 就是 HTTP 的状态码和 detail，code 是机器码。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


#: 机器码
TURN_BUSY = "chat_turn_busy"
TURN_INVALID = "chat_turn_invalid"
TURN_FINISHED = "chat_turn_finished"
BUSY = "这段对话正有一轮在进行：等它结束或先停止，再提问"


async def _busy(session: AsyncSession, conversation_id: str, *, besides: str | None = None) -> bool:
    """这段对话里有没有正在跑的一轮（停在审批上等人的不算：以前页面上也照样能接着问）。"""
    from app.db.models_chat import ChatTurnJob

    rows = (await session.execute(select(ChatTurnJob.id, ChatTurnJob.stage).where(
        ChatTurnJob.conversation_id == conversation_id, ChatTurnJob.status.in_(runtime.ACTIVE)))).all()
    return any(stage != "waiting" for job_id, stage in rows if job_id != besides)


async def _scope(session: AsyncSession, ids: list[str] | None) -> list[dict[str, str]]:
    """限定的数据源 → [{id, name}]（名字跟着存：库删了之后，这一轮查的是谁还得说得出来）。认 id 和名字。"""
    from app.db.models import DataSource

    wanted = [s.strip() for s in ids or [] if isinstance(s, str) and s.strip()]
    if not wanted:
        return []
    rows = {r.id: r for r in (await session.execute(select(DataSource).where(
        DataSource.id.in_(wanted) | DataSource.name.in_(wanted)))).scalars()}
    by_name = {r.name: r for r in rows.values()}
    out = []
    for item in wanted:
        hit = rows.get(item) or by_name.get(item)
        out.append({"id": hit.id, "name": hit.name} if hit else {"id": item, "name": item})
    return out


#: 只有问数据的对话能发起一轮（画布助手、应用润色、蓝图的对话各有各的助手；启动整理也只认问数据的对话）
NOT_CHAT = "这段对话不是问数据的对话，不能在这里发起问数据"
#: 使用者（user 角色）不能继续运行：以前 POST /api/runs/{id}/continue 对它就是 403（api/deps._run_scope 只放行停止），说法同那边
ROLE_ONLY = "没有权限：这项操作需要搭建者及以上的角色"


async def hold_conversation(session: AsyncSession, conversation_id: str, *, touch: bool = True) -> None:
    """占住这段对话：发起一轮的核对和写入（这段对话忙不忙、下一轮排第几、任务、第一条事件）在同一个事务里，第一句就写它——
    SQLite 上拿到写锁、PostgreSQL 上锁住这一行，同一段对话同时来的几次发起一个接一个，后来的看得见先来的写下的。

    touch：顺手记活跃时间（人在这里开始做事）。页面打开时自动发的「接着收尾」不算，原样写回，不然旧对话一打开就顶到列表最上面。"""
    from app.db.models import Conversation

    stamp = utcnow() if touch else Conversation.last_active_at
    await session.execute(update(Conversation).where(Conversation.id == conversation_id)
                          .values(last_active_at=stamp).execution_options(synchronize_session=False))


async def start(session: AsyncSession, *, conversation: Any, question: str, datasource_ids: list[str] | None,
                actor: str | None, mode: str | None = None, retry_of: Any = None,
                max_steps: int | None = None) -> str:
    """发起一轮（新问，或者对已有的一轮重试、重新运行、继续运行、接着收尾）。交回这一轮的 id（conversation_turns.id）。

    新问：建 conversation_turns 那一行（同 POST /api/conversations/{id}/turns）和任务。已有的一轮：上一次的结局归档进
    meta.attempts（重试、重新运行、放宽步数），那一行回到 running，任务重新排队。不发起抛 TurnRefused。

    **同时来的几次发起只起得来一次。** 核对和写入在同一个事务里、先占住这段对话（hold_conversation）：同一段对话同时两次新问
    只建一轮（另一次 409），同一轮同时几次重试、两个标签页同时请服务端接着收尾只有一次排得上队（任务按状态条件更新）；
    这一次尝试的第一条事件（turn.attempt）也在这个事务里写，写不进去就什么都不留下。继续运行要先续上那次运行（它自己按运行的
    状态认领，同一次运行同时只续得上一次），它用自己的会话写库，不能放进占着对话的事务里；续上了、这里却没排上队的，把续上的
    那次运行取消掉。服务重启挂起的旧运行在提交之后才取消（只有排上队的那一次去取消）。"""
    from app.api.coded import ROLE_REQUIRED
    from app.api.conversations import turn_run_id
    from app.api.deps import restricted
    from app.db.models import Run

    mode = mode or ("retry" if retry_of is not None else "ask")
    if mode not in MODES or (mode == "ask") != (retry_of is None):
        raise TurnRefused(422, "发起方式不对：新问不带 retry_of，重试、重新运行、继续运行要带上是哪一轮", TURN_INVALID)
    if getattr(conversation, "kind", "chat") != "chat":
        raise TurnRefused(422, NOT_CHAT, TURN_INVALID)
    if mode == "continue" and restricted():
        raise TurnRefused(403, ROLE_ONLY, ROLE_REQUIRED)
    if retry_of is None and not question.strip():
        raise TurnRefused(422, "问题不能为空", TURN_INVALID)
    if mode == "max_steps" and (not max_steps or max_steps < 1):
        raise TurnRefused(422, "放宽到几步要写清楚", TURN_INVALID)

    continued: str | None = None
    if mode == "continue":
        run_id = turn_run_id(retry_of)
        run = await session.get(Run, run_id) if run_id else None
        if run is None:
            raise TurnRefused(409, "这一轮没有可接着的运行，请重试本轮", TURN_INVALID)
        await _continue_run(run.id, actor)
        continued = run.id
    try:
        turn_id, stale_run = await _claim(session, conversation=conversation, question=question,
                                          datasource_ids=datasource_ids, actor=actor, mode=mode, retry_of=retry_of,
                                          max_steps=max_steps)
    except BaseException:
        await session.rollback()
        if continued:
            from app.engine.runner import run_manager

            with contextlib.suppress(Exception):
                await run_manager.cancel(continued, actor=actor)
        raise
    if stale_run:
        # 服务重启挂起的旧运行没人会再去续它，顺手取消（停在审批上的不算：重试不会从那里发起）
        from app.engine.runner import run_manager

        try:
            await run_manager.cancel(stale_run, actor=actor)
        except Exception:  # noqa: BLE001
            logger.exception("问数据：重试时取消挂起的旧运行 %s 出错", stale_run)
    if not manager.start(turn_id):
        # 上一次的后台任务刚记完结局、还没退出：等它退出再起
        await manager.wait(turn_id, timeout=STOP_GRACE_S)
        if not manager.start(turn_id):
            async with SessionLocal() as s2:
                from app.db.models_chat import ChatTurnJob

                await s2.execute(update(ChatTurnJob).where(ChatTurnJob.id == turn_id).values(
                    status="failed", error="上一次的后台任务还没有退出", finished_at=utcnow())
                    .execution_options(synchronize_session=False))
                await s2.commit()
            raise TurnRefused(409, BUSY, TURN_BUSY)
    return turn_id


async def _claim(session: AsyncSession, *, conversation: Any, question: str, datasource_ids: list[str] | None,
                 actor: str | None, mode: str, retry_of: Any, max_steps: int | None) -> tuple[str, str | None]:
    """在一个事务里核对、写轮次、排队、写这一次尝试的第一条事件，提交。交回 (这一轮的 id, 提交之后要取消的挂起旧运行)。"""
    from app.api.conversations import new_turn, turn_run_id
    from app.chat import turn as policy
    from app.db.models import Approval, ConversationTurn, Run
    from app.db.models_chat import ChatTurnJob

    await hold_conversation(session, conversation.id, touch=mode != "resume")
    if retry_of is not None:
        await hold_turn(session, retry_of.id)
    # 接着收尾（resume）不建图、不发起运行，不算占着这段对话：一段对话里停在半路的几轮可以一起交给服务端收尾
    besides = retry_of.id if retry_of is not None else None
    if mode != "resume" and await _busy(session, conversation.id, besides=besides):
        raise TurnRefused(409, BUSY, TURN_BUSY)
    now_ms = int(time.time() * 1000)
    request: dict[str, Any] = {"started_ms": now_ms}
    stale_run: str | None = None

    if retry_of is None:
        scope = await _scope(session, datasource_ids)
        row = await new_turn(session, conversation, question)
        row.meta = {"v": 1, **({"scope": scope} if scope else {}), "at": now_ms}
        request["scope"] = scope
        job = ChatTurnJob(id=row.id, conversation_id=conversation.id, status="queued", mode=mode, request=request,
                          principal=principal_snapshot(), actor=actor, started_at=None)
        session.add(job)
        await session.flush()
        attempts: list[Any] = []
    else:
        # 占住之后重读：发起之前读到的那一份可能已经被同时来的另一次发起改过了
        row = await session.get(ConversationTurn, retry_of.id, populate_existing=True)
        if row is None:
            raise TurnRefused(404, "该轮对话不存在，可能已被删除")
        job = await session.get(ChatTurnJob, row.id, populate_existing=True)
        if job is not None and job.status in runtime.ACTIVE:
            raise TurnRefused(409, BUSY, TURN_BUSY)
        meta = dict(row.meta or {}) if isinstance(row.meta, dict) else {}
        scope = await _scope(session, datasource_ids) if datasource_ids is not None else list(meta.get("scope") or [])
        request["scope"] = scope
        run_id = turn_run_id(row)
        run = await session.get(Run, run_id) if run_id else None
        attempts = list(meta.get("attempts") or [])
        graphful = policy.has_nodes(row.graph)
        if mode in ("rerun", "max_steps", "run") and not graphful:
            raise TurnRefused(409, "这一轮没有可运行的工作流，请重试本轮", TURN_INVALID)
        if mode == "max_steps":
            request["max_steps"] = int(max_steps or 0)
        if mode in ("continue", "resume"):
            # 接着收尾不要求运行还在：运行记录删了的，服务端照实收成「运行记录不见了」
            if not run_id:
                raise TurnRefused(409, "这一轮没有可接着的运行，请重试本轮", TURN_INVALID)
            request["follow"] = True
            # 计时：收过尾的（失败、中断之后点「继续运行」）接着上次停下的地方走——隔了一夜再点，头部不该写「已运行 14 小时」；
            # 停在半路的（页面以前没来得及收尾的）从运行开始算；运行早就结束了的（补交付）按它真正结束的时刻算，不按现在
            spent = meta.get("ms")
            if row.status != "running" and isinstance(spent, (int, float)):
                request["started_ms"] = now_ms - int(spent)
            elif run is not None and run.started_at is not None:
                request["started_ms"] = int(run.started_at.timestamp() * 1000)
            if mode == "resume" and run is not None and run.status in ("succeeded", "failed", "cancelled") \
                    and run.finished_at is not None:
                request["ended_ms"] = int(run.finished_at.timestamp() * 1000)
            row.status, row.error = "running", ""
            row.meta = {**meta, "at": now_ms}
        else:
            if run is not None and run.status == "interrupted" and mode != "run":
                pending = (await session.execute(select(Approval.id).where(
                    Approval.run_id == run.id, Approval.status.in_(("pending", "answered"))).limit(1))).first()
                if pending is None:
                    stale_run = run.id
            if mode != "run":
                archived = record.attempt_record(row, meta, n=len(attempts) + 1,
                                                 at_ms=int(row.updated_at.timestamp() * 1000) if row.updated_at else now_ms)
                attempts.append(archived)
                if mode == "retry":
                    reason = record.attempt_reason(archived)
                    request["base"] = graphful
                    request["instruction"] = policy.user_retry_instruction(row.question, reason, graphful)
            usage = record.retire_run(meta.get("usage"))
            new_meta = {"v": 1, "attempts": attempts or None, "scope": scope or None, "usage": usage or None,
                        "runId": None, "at": now_ms}
            if mode == "run":
                # 「跑一下」不是重来：这一轮还没有答过，别的照旧
                new_meta = {**meta, "runId": None, "at": now_ms, "asked": meta.get("asked")}
            row.meta = {k: v for k, v in new_meta.items() if v is not None}
            row.status, row.error, row.answer, row.review = "running", "", "", {}
            row.run_id = None
        values = {"status": "queued", "stage": None, "mode": mode, "request": request, "owner": current_owner(),
                  "run_id": run_id if mode in ("continue", "resume") else None, "attempt": 0,
                  "principal": principal_snapshot(), "actor": actor, "error": None, "started_at": None,
                  "finished_at": None}
        if job is None:
            session.add(ChatTurnJob(id=row.id, conversation_id=conversation.id, **values))
        else:
            # 只在它不在排队、不在跑的时候排上：同时来的另一次已经排上了的，这一次不算
            claimed = await session.execute(update(ChatTurnJob).where(
                ChatTurnJob.id == row.id, ChatTurnJob.status.not_in(runtime.ACTIVE)).values(**values)
                .execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                raise TurnRefused(409, BUSY, TURN_BUSY)
        await session.flush()
    turn_id = row.id
    usage_now = (row.meta or {}).get("usage") if isinstance(row.meta, dict) else None
    # 这一次尝试从这一条开始：刷新、重开页面时从这里回放。页面据此把这一轮重置成「这一次」的样子：归档、范围、到目前为止的
    # 用量、从什么时候算起（补交付的到运行结束为止）
    seq = await turn_log.add(session, turn_id, "turn.attempt", {
        "mode": mode, "attempts": attempts, "scope": request.get("scope") or [], "usage": usage_now or {},
        "started_ms": request.get("started_ms"),
        **({"ended_ms": request["ended_ms"]} if request.get("ended_ms") else {}),
        **({"max_steps": request["max_steps"]} if request.get("max_steps") else {})})
    await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == turn_id).values(attempt_seq=seq)
                          .execution_options(synchronize_session=False))
    try:
        await session.commit()
    except IntegrityError as e:
        # 不占锁的写入（老页面开轮）正好同时：照「这段对话正忙」答
        raise TurnRefused(409, BUSY, TURN_BUSY) from e
    turn_log.notify(turn_id)
    return turn_id, stale_run


async def _continue_run(run_id: str, actor: str | None) -> None:
    """继续运行：规则同 POST /api/runs/{id}/continue（失败的、服务重启挂起的才续得上），被拒照那边的说法。"""
    from app.engine.runner import ContinueRefused, run_manager

    try:
        await run_manager.continue_failed(run_id, graph=None, actor=actor)
    except KeyError as e:
        raise TurnRefused(404, str(e).strip("'")) from e
    except ContinueRefused as e:
        raise TurnRefused(409, str(e), e.code) from e
    except ValueError as e:
        raise TurnRefused(409, str(e)) from e


async def cancel(turn_id: str, actor: str | None) -> bool:
    """停止这一轮（取消生成、取消运行，照现有的取消规则）。这一轮已经结束了交回 False。"""
    from app.db.models_chat import ChatTurnJob

    if await manager.stop(turn_id, "user", grace=STOP_GRACE_S):
        return True
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
        if job is None or job.status not in (*runtime.ACTIVE, "interrupted"):
            return False
        # 不在这个进程里跑（排队还没开始、服务重启后停着等人来接着收尾）：这里收成已取消
        claimed = await session.execute(update(ChatTurnJob).where(
            ChatTurnJob.id == turn_id, ChatTurnJob.status == job.status).values(
            status="cancelled", stage=None, finished_at=utcnow()).execution_options(synchronize_session=False))
        await session.commit()
        if claimed.rowcount != 1:
            return False
        run_id = job.run_id
    execution = ChatTurnExecution(turn_id)
    execution.actor, execution.run_id, execution.request = actor, run_id, dict(job.request or {})
    if run_id:
        from app.engine.runner import run_manager

        try:
            await run_manager.cancel(run_id, actor=actor)
        except Exception:  # noqa: BLE001
            logger.exception("问数据：停止这一轮时取消运行 %s 出错", run_id)
    await execution.save(status="error", error=record.CANCELLED,
                         meta={"outcome": "cancelled", "runStatus": "cancelled" if run_id else None})
    await execution._end("cancelled")
    await execution._settle()
    turn_log.notify(turn_id)
    return True


async def state(session: AsyncSession, turn_id: str) -> dict[str, Any] | None:
    """这一轮的任务现在怎样（GET /api/chat/turns/{id}）。没有任务（老轮次）交回 None。"""
    from app.db.models_chat import ChatTurnJob

    job = await session.get(ChatTurnJob, turn_id)
    return None if job is None else job_view(job, late=await late_waiting(job))


def job_view(job: Any, *, late: bool | None = None) -> dict[str, Any]:
    return {"status": job.status, "stage": job.stage, "mode": job.mode, "run_id": job.run_id, "attempt": job.attempt,
            "last_seq": job.last_seq, "attempt_seq": job.attempt_seq, "error": job.error,
            "late_review": (late_pending(job.id) or getattr(job, "late_review_at", None) is not None) if late is None
            else late}


async def stream_status(turn_id: str) -> str | None:
    """实时流还要不要等：这一轮还在进行中（或晚到的复核还没回来）交回 None，否则交回任务的状态。"""
    from app.db.models_chat import ChatTurnJob

    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
    if job is None:
        return "gone"
    if job.status in runtime.ACTIVE or await late_waiting(job):
        return None
    return job.status


async def stop_conversation(conversation_id: str) -> None:
    """对话要删了：停掉它正在跑的几轮（取消各自的运行），不再写那几行。"""
    from app.db.models_chat import ChatTurnJob

    async with SessionLocal() as session:
        ids = list((await session.execute(select(ChatTurnJob.id).where(
            ChatTurnJob.conversation_id == conversation_id, ChatTurnJob.status.in_(runtime.ACTIVE)))).scalars())
    for turn_id in ids:
        await manager.stop(turn_id, "deleted", grace=STOP_GRACE_S)


async def forget(session: AsyncSession, conversation_id: str) -> None:
    """删掉一段对话的任务和事件日志（外键是级联的，这里照样显式删：理由同 conversations.delete_conversation）。"""
    from app.db.models import ConversationTurn
    from app.db.models_chat import ChatTurnEvent, ChatTurnJob

    turns = select(ConversationTurn.id).where(ConversationTurn.conversation_id == conversation_id)
    await session.execute(delete(ChatTurnEvent).where(ChatTurnEvent.turn_id.in_(turns)))
    await session.execute(delete(ChatTurnJob).where(ChatTurnJob.conversation_id == conversation_id))


# --------------------------------------------------------------------------
# 启动、关停
# --------------------------------------------------------------------------


async def startup(*, resume: bool | None = None, live: Any = None) -> dict[str, int]:
    """服务启动时（run_manager.setup 之后：服务重启时在跑的运行这时已经标成挂起）；多进程时当班进程隔一会儿再来一次
    （app/cluster/reaper.py），只碰属主不在了的轮次（进程没了；或者就是本进程、却不在本进程里跑），别的活着的进程在跑的一概
    不碰。认领那一下是带条件的 UPDATE，几个进程同时接手同一轮只有一个改得动；接手的进程照下面的规矩办：

    - 还在建图（没有运行）的这一轮标中断：那一行记 error 和原因（build_interrupted），可以重试本轮；
    - 已经发起了运行的：resume 为真（缺省看 AGENTLAB_SCHEDULER）就接着等那次运行、照常复核收尾；为假就停着（任务记
      interrupted、那一行不动），等人打开这段对话时页面再请服务端接着收尾；
    - 老轮次（这一波之前浏览器推的，没有任务）停在 running、又没有运行的：同样标中断——它的建图那条流随上次的进程一起没了。

    交回整理了几轮：{interrupted, resumed, parked, legacy}。"""
    from app.api.conversations import turn_run_id
    from app.core.config import settings
    from app.db.models import Conversation, ConversationTurn
    from app.db.models_chat import ChatTurnJob

    from app.cluster import claims, state

    resume = bool(settings.scheduler) if resume is None else resume
    counts = {"interrupted": 0, "resumed": 0, "parked": 0, "legacy": 0}
    live = live if live is not None else await state.current(fresh=True)
    claimed = [job_id for job_id, _ in await claims.orphans(ChatTurnJob, live, running=manager.running,
                                                            statuses=runtime.ACTIVE)]
    async with SessionLocal() as session:
        jobs = list((await session.execute(select(ChatTurnJob).where(ChatTurnJob.id.in_(claimed),
                                                                     ChatTurnJob.status.in_(runtime.ACTIVE)))).scalars()) \
            if claimed else []
        legacy = list((await session.execute(
            select(ConversationTurn).join(Conversation, Conversation.id == ConversationTurn.conversation_id)
            .outerjoin(ChatTurnJob, ChatTurnJob.id == ConversationTurn.id)
            .where(ConversationTurn.status == "running", Conversation.kind == "chat", ChatTurnJob.id.is_(None))
        )).scalars())
    for job in jobs:
        if not job.run_id:
            await _interrupt_build(job.id)
            counts["interrupted"] += 1
        elif resume:
            async with SessionLocal() as session:
                await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == job.id).values(
                    status="queued", request={**dict(job.request or {}), "follow": True})
                    .execution_options(synchronize_session=False))
                await session.commit()
            manager.start(job.id)
            counts["resumed"] += 1
        else:
            async with SessionLocal() as session:
                await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == job.id).values(
                    status="interrupted", finished_at=utcnow()).execution_options(synchronize_session=False))
                await session.commit()
            turn_log.notify(job.id)
            counts["parked"] += 1
    # 晚到的复核记在库里、记它的进程却不在了：不会再回来，别让别的进程上的流一直等
    async with SessionLocal() as session:
        late = (await session.execute(select(ChatTurnJob.id, ChatTurnJob.owner).where(
            ChatTurnJob.late_review_at.is_not(None)))).all()
    gone = [turn_id for turn_id, owner in late if not live.alive(owner, local=late_pending(turn_id))]
    if gone:
        async with SessionLocal() as session:
            await session.execute(update(ChatTurnJob).where(ChatTurnJob.id.in_(gone)).values(late_review_at=None)
                                  .execution_options(synchronize_session=False))
            await session.commit()
        for turn_id in gone:
            turn_log.notify(turn_id)
    # 老轮次（没有任务行）只在启动时整理：当班进程隔一会儿来的那一次（live.trust_mine）不碰——老页面开的轮次（不占锁的写入）
    # 正在建图时也是这个样子
    for row in [] if live.trust_mine else legacy:
        if turn_run_id(row):
            continue   # 有运行的老轮次：页面打开时请服务端接着收尾
        async with SessionLocal() as session:
            fresh = await session.get(ConversationTurn, row.id)
            if fresh is None or fresh.status != "running":
                continue
            meta = dict(fresh.meta or {}) if isinstance(fresh.meta, dict) else {}
            fresh.status, fresh.error = "error", record.BUILD_INTERRUPTED
            fresh.meta = {**meta, "v": 1, "cause": {"kind": "build_interrupted"}, "at": int(time.time() * 1000)}
            await session.commit()
        counts["legacy"] += 1
    if any(counts.values()):
        logger.info("问数据：启动时整理了上次没跑完的几轮 %s", counts)
    return counts


async def _interrupt_build(turn_id: str) -> None:
    execution = ChatTurnExecution(turn_id)
    await execution.save(status="error", error=record.BUILD_INTERRUPTED,
                         meta={"cause": {"kind": "build_interrupted"}, "runId": None})
    await execution._end("error")
    await execution._settle()
    async with SessionLocal() as session:
        from app.db.models_chat import ChatTurnJob

        await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == turn_id).values(
            status="interrupted", stage=None, finished_at=utcnow()).execution_options(synchronize_session=False))
        await session.commit()
    turn_log.notify(turn_id)


async def shutdown() -> None:
    """关停：停掉在跑的几轮（不改那几行，下次启动照「重启」的规矩整理）；晚到的复核不等。"""
    await manager.shutdown()
    for task in list(_late.values()):
        task.cancel()
    _late.clear()


__all__ = ["APPROVAL_POLL_S", "AUTO_RETRY_MODES", "BUSY", "CONCURRENCY_CLASS", "ChatDriver", "ChatTurnExecution",
           "MODES", "REVIEW_WAIT_S", "TURN_BUSY", "TURN_FINISHED", "TURN_INVALID", "TurnRefused", "cancel", "forget",
           "current_principal", "job_view", "late_pending", "manager", "principal_of", "principal_snapshot", "shutdown",
           "start", "startup", "state", "stop_conversation", "stream_status"]
