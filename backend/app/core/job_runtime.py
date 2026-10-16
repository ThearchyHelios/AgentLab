"""后台任务的共用运行时：数据目录的起草（data/catalog_jobs.py）和剖析（data/catalog_profile_jobs.py）各自建表、各自的步骤，
生命周期是同一套；之后的评测、后台探查也用它（docs/design/next-round/00-总方案.md X5）。

**为什么在 core。** 最早只服务数据目录，放在 data/catalog_job_runtime.py；评测（一次评测一个任务、每题一项）和后台探查
（同源排队）要的是同一样东西，从 data 包里导入一个「数据目录的」运行时说不通。原路径留着再导出（同一个模块对象里的
名字，lanes 也是同一条队），已有的 import 不用改，正在改起草、剖析的工作也不会因此冲突。

**为什么抽出来。** 剖析以前是一次同步长请求，和起草改成后台任务之前是同一类问题：关掉页面结果就没了、不能停、不留痕。
两边要的是同一样东西——进程内跑、能停、重启标中断、写库不被停止打断、同源排队——各写一份，迟早一边修了另一边没修。
共用的是生命周期代码，不是表：两种任务的进度完全不同（起草按段、块、模型用量，剖析按检查项、查询），合成一张
「kind + JSON」的通用任务表要丢掉起草那些强类型列，还要迁移刚稳定的起草数据。

- **状态词**（ACTIVE、FINISHED、TASK_STATUSES、TASK_DONE、RETRYABLE、OPEN）：任务 queued → running → succeeded / partial
  / failed / cancelled / interrupted；逐表 pending → running → ok / partial / failed / skipped / cancelled。两种任务、
  接口、界面都用这一套词。
- **SerialWriter**：同一个任务的库写入一条一条来，shield 起来——停止任务时不会打断写到一半的库操作；收尾前 settle 等它们
  写完。
- **JobRun / JobManager**：进程内一个 asyncio 任务一件事（照 app/scheduler 的写法）：start / stop(grace) / wait / running
  / shutdown。JobRun.run 把「进队、等轮到自己、执行、按停止的原因收尾、出队」写一次，各种任务只写执行和收尾的那几步。
- **SourceLanes**：每个数据源一条先来先到的队。起草、剖析都打业务库或模型、都写同一个源的目录：同源串行（先建的先跑），
  不同源并行。任务阻塞地占：建出来是 queued，轮到了才 running。同步起草、导入目录这类同步写操作不阻塞地试占：占不到
  说明源上正有别的事，接口回 409；占到了，执行期间新建的任务排在它后面。**队里的事是开放的**：运行时不认具体哪几种，
  每种在自己的模块里 register_lane_kind 登记一次（kind → 叫法），进队时只核对登记过——第 2 波的后台探查（introspect）
  登记一行就排进同一条队，不用改这里；拼错的 kind 当场报错，不会悄悄排成一种谁都不认识的事。
- **close_job / interrupt_on_startup**：收尾（还没结束的表记成某个状态，任务记成某个状态），和服务启动时的整理：排队、
  进行中的标 interrupted，没结束的表记 cancelled。**不自动续跑**：复制出来的数据目录（检查脚本、验收副本）启动时不能
  自己去调模型、查业务库，界面让人点「重试」接着做。

进程内登记、进程内的队：服务端是单进程部署，多进程部署时要换成库里的锁。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

#: 任务还没结束的状态（同一个源同一种任务只能有一个）、结束了的状态
ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "partial", "failed", "cancelled", "interrupted")
#: 逐表的状态，其中算「已结束」的、重试时要带上的、还没结束的
TASK_STATUSES = ("pending", "running", "ok", "partial", "failed", "skipped", "cancelled")
TASK_DONE = ("ok", "partial", "failed", "skipped", "cancelled")
RETRYABLE = ("failed", "partial", "cancelled")
OPEN = ("pending", "running")

#: 停止进行中的任务时最多等它收尾多久（秒）；关停服务时另给（shutdown 的 grace）
STOP_GRACE_S = 10.0
SHUTDOWN_GRACE_S = 3.0


class JobFailed(Exception):
    """任务整体出错（数据源没了、快照没了、模型用不了……）：message 就是任务的 error。"""


# ==========================================================================
# 库写入
# ==========================================================================


class SerialWriter:
    """一个任务的库写入：一条一条来，每条开一个会话；shield 起来，停止任务时不会打断写到一半的库操作。"""

    def __init__(self) -> None:
        self._writes: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    async def __call__(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        async def go() -> Any:
            async with self._lock:
                async with SessionLocal() as session:
                    return await fn(session, *args)

        task = asyncio.ensure_future(go())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        return await asyncio.shield(task)

    async def settle(self) -> None:
        """等手上的库写入都写完（停止、出错收尾之前）。"""
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)


# ==========================================================================
# 每个源一条队
# ==========================================================================


#: 同源队里能排的事：{kind: 叫法}。开放的，各种事在自己的模块里登记（register_lane_kind）：数据目录登记了 draft 起草任务、
#: profile 剖析任务、sync_draft 同步起草、import 导入目录（data/catalog_jobs.py）
_LANE_KINDS: dict[str, str] = {}


def register_lane_kind(kind: str, label: str) -> str:
    """登记一种同源队里的事（重复登记同一个叫法无害，换叫法报错：同一个 kind 只能有一种说法）。返回 kind。"""
    if not kind or not label:
        raise ValueError("同源队里的事要有 kind 和叫法")
    known = _LANE_KINDS.get(kind)
    if known is not None and known != label:
        raise ValueError(f"同源队里的「{kind}」已经登记成「{known}」，不能再登记成「{label}」")
    _LANE_KINDS[kind] = label
    return kind


def lane_kinds() -> dict[str, str]:
    """登记过的各种事：{kind: 叫法}。"""
    return dict(_LANE_KINDS)


def lane_label(kind: str) -> str:
    """一种事的叫法（「起草」「剖析」……）；没登记的照原样给 kind。"""
    return _LANE_KINDS.get(kind, kind)


class LaneTicket:
    """在一个源的队里的一个位置。kind 是占着它的是什么（登记过的一种，见 register_lane_kind）；job_id 是任务的（同步写
    操作没有）。轮到它时 granted 置位。"""

    def __init__(self, lanes: SourceLanes, source_id: str, kind: str, job_id: str | None,
                 loop: asyncio.AbstractEventLoop) -> None:
        self._lanes = lanes
        self.source_id = source_id
        self.kind = kind
        self.job_id = job_id
        self.loop = loop
        self.granted = asyncio.Event()
        self.released = False

    async def wait(self) -> None:
        """等轮到自己。"""
        await self.granted.wait()

    def release(self) -> None:
        """出队（轮到了、没轮到都行，重复调用无害）：排在后面的第一个接着开始。"""
        self._lanes._release(self)


class LaneBusy(Exception):
    """不阻塞地试占时这个源的队里已经有人：holder 是排在最前面（正在做）的那一个。"""

    def __init__(self, holder: LaneTicket) -> None:
        super().__init__(holder.kind)
        self.holder = holder


class SourceLanes:
    """每个数据源一条先来先到的队。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[LaneTicket]] = {}

    def _queue(self, source_id: str) -> list[LaneTicket]:
        """这个源的队。别的事件循环留下的（测试每个用例一个循环）去掉：那个循环里没人会再出队了。"""
        queue = self._queues.get(source_id) or []
        loop = asyncio.get_running_loop()
        alive = [t for t in queue if t.loop is loop and not t.released]
        if alive:
            self._queues[source_id] = alive
            alive[0].granted.set()
        else:
            self._queues.pop(source_id, None)
        return alive

    def enter(self, source_id: str, kind: str, job_id: str | None = None) -> LaneTicket:
        """排进队尾，马上返回（不等）。排在最前面的立刻 granted；调用方 await ticket.wait() 等轮到自己。kind 要登记过。"""
        if kind not in _LANE_KINDS:
            raise ValueError(f"同源队里没有登记「{kind}」这种事，先 register_lane_kind")
        queue = self._queue(source_id)
        ticket = LaneTicket(self, source_id, kind, job_id, asyncio.get_running_loop())
        queue.append(ticket)
        self._queues[source_id] = queue
        if len(queue) == 1:
            ticket.granted.set()
        return ticket

    def try_enter(self, source_id: str, kind: str) -> LaneTicket:
        """队空着才占（马上就轮到）；有人在队里抛 LaneBusy。"""
        queue = self._queue(source_id)
        if queue:
            raise LaneBusy(queue[0])
        return self.enter(source_id, kind)

    def holders(self, source_id: str) -> list[LaneTicket]:
        """队里的各位，排在最前面（正在做）的在前。"""
        return list(self._queue(source_id))

    def ahead(self, source_id: str, job_id: str) -> list[str] | None:
        """排在这个任务前面的各位是什么（kind，正在做的在前）；这个任务不在队里（刚建、后台还没排进来）返回 None。"""
        queue = self._queue(source_id)
        for i, ticket in enumerate(queue):
            if ticket.job_id == job_id:
                return [t.kind for t in queue[:i]]
        return None

    def _release(self, ticket: LaneTicket) -> None:
        if ticket.released:
            return
        ticket.released = True
        queue = self._queues.get(ticket.source_id)
        if not queue or ticket not in queue:
            return
        head = queue[0] is ticket
        queue.remove(ticket)
        if not queue:
            self._queues.pop(ticket.source_id, None)
        elif head:
            queue[0].granted.set()

    @contextlib.asynccontextmanager
    async def hold(self, source_id: str, kind: str, job_id: str | None = None) -> AsyncIterator[LaneTicket]:
        """阻塞地占：排队，轮到了再进去，出来时出队。"""
        ticket = self.enter(source_id, kind, job_id)
        try:
            await ticket.wait()
            yield ticket
        finally:
            ticket.release()

    @contextlib.contextmanager
    def try_hold(self, source_id: str, kind: str) -> Iterator[LaneTicket]:
        """不阻塞地试占（同步起草、导入目录）：占不到抛 LaneBusy；占到了，执行期间新建的任务排在后面。"""
        ticket = self.try_enter(source_id, kind)
        try:
            yield ticket
        finally:
            ticket.release()


lanes = SourceLanes()


# ==========================================================================
# 收尾、启动时的整理
# ==========================================================================


async def close_job(session: AsyncSession, job_model: Any, task_model: Any, job_id: str, job_status: str,
                    job_error: str | None, task_status: str, task_error: str | None) -> None:
    """收尾：还没结束的表记 task_status（原因 task_error），任务（还没结束的话）记 job_status。提交。"""
    now = utcnow()
    await session.execute(
        update(task_model).where(task_model.job_id == job_id, task_model.status.in_(OPEN))
        .values(status=task_status, error=task_error, finished_at=now).execution_options(synchronize_session=False))
    await session.execute(
        update(job_model).where(job_model.id == job_id, job_model.status.in_(ACTIVE))
        .values(status=job_status, error=job_error, finished_at=now).execution_options(synchronize_session=False))
    await session.commit()


async def cancel_queued(session: AsyncSession, job_model: Any, task_model: Any, job_id: str) -> bool:
    """停止一个排队中的任务：任务和没结束的表直接记 cancelled。已经被后台认领（开始了）返回 False，交给调用方按进行中
    的停。"""
    now = utcnow()
    claimed = await session.execute(
        update(job_model).where(job_model.id == job_id, job_model.status == "queued")
        .values(status="cancelled", finished_at=now).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        await session.rollback()
        return False
    await session.execute(
        update(task_model).where(task_model.job_id == job_id, task_model.status.in_(OPEN))
        .values(status="cancelled", finished_at=now).execution_options(synchronize_session=False))
    await session.commit()
    return True


async def claim(session: AsyncSession, job_model: Any, job_id: str) -> bool:
    """认领：排队中 → 进行中。开始之前就被停止了返回 False。"""
    result = await session.execute(
        update(job_model).where(job_model.id == job_id, job_model.status == "queued")
        .values(status="running", started_at=utcnow()).execution_options(synchronize_session=False))
    await session.commit()
    return result.rowcount == 1


async def interrupt_on_startup(job_model: Any, close: Callable[[AsyncSession, str], Awaitable[None]], *,
                               running: Callable[[str], bool]) -> int:
    """服务启动时：排队、进行中、而且不在这个进程里跑的任务逐个 close（各自记成 interrupted、没结束的表记 cancelled）。
    不自动续跑。返回整理了几个。"""
    async with SessionLocal() as session:
        ids = [job_id for job_id in (await session.execute(
            select(job_model.id).where(job_model.status.in_(ACTIVE)))).scalars() if not running(job_id)]
        for job_id in ids:
            await close(session, job_id)
    return len(ids)


# ==========================================================================
# 在后台执行
# ==========================================================================


class JobRun:
    """一个任务在后台的一次执行。子类给 kind（进队时的身份）、model（任务表，进队前读源和状态），写 _run（执行）和
    _stopped / _failed / _crashed（停止、整体出错、意外出错时的收尾）。"""

    kind = ""
    model: Any = None

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.task: asyncio.Task[None] | None = None
        #: 为什么停：user 使用者点了停止，shutdown 服务关停（标中断）
        self.stop_reason: str | None = None
        self.writer = SerialWriter()
        self.ticket: LaneTicket | None = None

    async def _write(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """一条库写入（SerialWriter：一条一条来，停止时不被打断）。"""
        return await self.writer(fn, *args)

    async def _settle(self) -> None:
        await self.writer.settle()

    async def run(self) -> None:
        try:
            try:
                await self._queue()
                await self._run()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await self._stopped()
            except JobFailed as e:
                await self._failed(str(e))
            except Exception as e:  # noqa: BLE001 - 任务整体出错：照实记下，不留在「进行中」
                await self._crashed(e)
        finally:
            # 收尾写完才出队：排在后面的任务开始时，这个任务已经记成结束了
            if self.ticket is not None:
                self.ticket.release()

    def lane_of(self, job: Any) -> str | None:
        """这个任务排在哪条队里：缺省是任务的数据源（job.source_id）。不占源的任务（以后的评测：开始时拍目录快照，不挡
        起草、剖析）覆盖成返回 None，不进队、直接执行。"""
        return job.source_id

    async def _queue(self) -> None:
        """进这个源的队，等轮到自己。任务不在了、已经不是排队中（开始之前就被停止了）的不进队：_run 认领不到，直接结束。"""
        async with SessionLocal() as session:
            job = await session.get(self.model, self.job_id)
            source_id = self.lane_of(job) if job is not None and job.status == "queued" else None
        if source_id is None:
            return
        self.ticket = lanes.enter(source_id, self.kind, self.job_id)
        await self.ticket.wait()

    async def _run(self) -> None:
        raise NotImplementedError

    async def _stopped(self) -> None:
        raise NotImplementedError

    async def _failed(self, reason: str) -> None:
        raise NotImplementedError

    async def _crashed(self, e: Exception) -> None:
        raise NotImplementedError


class JobManager:
    """进程内的一种后台任务：一个 asyncio 任务一件事，停的时候给宽限。factory 按任务编号造一次执行（JobRun）；label 是
    日志里的叫法（「数据目录起草任务」），name 是 asyncio 任务名的前缀。"""

    def __init__(self, factory: Callable[[str], JobRun], *, label: str, name: str) -> None:
        self._runs: dict[str, JobRun] = {}
        self._factory = factory
        self.label = label
        self.name = name

    def _alive(self, job_id: str) -> JobRun | None:
        run = self._runs.get(job_id)
        if run is None or run.task is None or run.task.done():
            return None
        try:
            if run.task.get_loop() is not asyncio.get_running_loop():
                self._runs.pop(job_id, None)   # 别的事件循环留下的（测试每个用例一个循环）
                return None
        except RuntimeError:
            return None
        return run

    def start(self, job_id: str) -> None:
        """开始在后台执行这个（排队中的）任务：先进这个源的队，轮到了才开始。"""
        run = self._factory(job_id)
        run.task = asyncio.get_running_loop().create_task(run.run(), name=f"{self.name}-{job_id}")
        self._runs[job_id] = run

        def forget(_: asyncio.Task[None]) -> None:
            if self._runs.get(job_id) is run:
                self._runs.pop(job_id, None)

        run.task.add_done_callback(forget)

    def running(self, job_id: str) -> bool:
        return self._alive(job_id) is not None

    async def stop(self, job_id: str, reason: str, *, grace: float = STOP_GRACE_S) -> bool:
        """停掉这个进程里在跑（或在排队等）的任务，等它收尾（最多 grace 秒）。不在这个进程里返回 False。"""
        run = self._alive(job_id)
        if run is None:
            return False
        if run.stop_reason is None:
            run.stop_reason = reason
            run.task.cancel()  # type: ignore[union-attr]
        await asyncio.wait([run.task], timeout=grace)  # type: ignore[list-item]
        return True

    async def wait(self, job_id: str, timeout: float | None = None) -> None:
        """等这个任务做完（测试、调试用）。不在跑直接返回。"""
        run = self._alive(job_id)
        if run is not None:
            await asyncio.wait([run.task], timeout=timeout)  # type: ignore[list-item]

    async def shutdown(self, *, grace: float = SHUTDOWN_GRACE_S) -> None:
        """关停：停掉进行中、排队等着的任务（标中断），最多等 grace 秒收尾。"""
        runs = [run for job_id in list(self._runs) if (run := self._alive(job_id)) is not None]
        for run in runs:
            if run.stop_reason is None:
                run.stop_reason = "shutdown"
                run.task.cancel()  # type: ignore[union-attr]
        if runs:
            await asyncio.wait([run.task for run in runs], timeout=grace)  # type: ignore[misc]
            logger.info("已停止 %d 个%s", len(runs), self.label)


__all__ = ["ACTIVE", "FINISHED", "JobFailed", "JobManager", "JobRun", "LaneBusy", "LaneTicket", "OPEN", "RETRYABLE",
           "SHUTDOWN_GRACE_S", "STOP_GRACE_S", "SerialWriter", "SourceLanes", "TASK_DONE", "TASK_STATUSES", "cancel_queued",
           "claim", "close_job", "interrupt_on_startup", "lane_kinds", "lane_label", "lanes", "register_lane_kind"]
