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
- **close_job / interrupt_on_startup**：收尾（还没结束的表记成某个状态，任务记成某个状态；可选的 finish 在同一个事务里
  补一步，评测拿它按改好的逐项状态重算计数、记停下的原因），和服务启动时的整理：排队、进行中的标 interrupted，没结束的
  表记 cancelled。**不自动续跑**：复制出来的数据目录（检查脚本、验收副本）启动时不能
  自己去调模型、查业务库，界面让人点「重试」接着做。

**多进程**（docs/design/orchestration-round §5 W2-b，app/cluster）：任务表的 owner 是认领它的进程（建任务时缺省就是建它的
进程），执行在那个进程里；同源队记在库里（lane_tickets），几个进程共用一条；启动整理只接手属主不在了的任务
（interrupt_on_startup，多进程时当班进程隔一会儿再来一次）；停止落在别的进程上时给属主发停止请求（JobManager.stop）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
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
        from app.cluster import fence

        # 本进程续不上租约了（cluster/fence.py）：这个任务可能已经被别的进程接手，不再写
        fence.check()

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
    """在一个源的队里的一个位置（库里 lane_tickets 的一行）。kind 是占着它的是什么（登记过的一种，见 register_lane_kind）；
    job_id 是任务的（同步写操作没有）。轮到它时 granted 置位。"""

    def __init__(self, lanes: SourceLanes, row_id: int, source_id: str, kind: str, job_id: str | None,
                 loop: asyncio.AbstractEventLoop) -> None:
        self._lanes = lanes
        self.id = row_id
        self.source_id = source_id
        self.kind = kind
        self.job_id = job_id
        self.loop = loop
        self.granted = asyncio.Event()
        self.woke = asyncio.Event()
        self.released = False

    async def wait(self) -> None:
        """等轮到自己：本进程里有人出队时叫醒，PostgreSQL 上别的进程出队时 NOTIFY 叫醒，另隔一会儿自己回库里看一次。"""
        from app.core.config import settings

        poll = LANE_POLL_S.get(settings.db_dialect, 5.0)
        while not self.granted.is_set():
            self.woke.clear()
            got = await self._lanes._advance(self.source_id, me=self)
            if got or self.granted.is_set():
                self.granted.set()
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.woke.wait(), timeout=poll)

    async def release(self) -> None:
        """出队（轮到了、没轮到都行，重复调用无害）：排在后面的第一个接着开始。"""
        await self._lanes._release(self)


class LaneHolder:
    """队里的一位（holders / LaneBusy 里给出的样子）：kind、job_id、属主。"""

    def __init__(self, kind: str, job_id: str | None, owner: str | None) -> None:
        self.kind = kind
        self.job_id = job_id
        self.owner = owner

    def __repr__(self) -> str:
        return f"LaneHolder({self.kind!r}, {self.job_id!r})"


class LaneBusy(Exception):
    """不阻塞地试占时这个源的队里已经有人：holder 是排在最前面（正在做）的那一位。"""

    def __init__(self, holder: LaneHolder) -> None:
        super().__init__(holder.kind)
        self.holder = holder


#: 等轮到自己时隔多久回库里看一次（秒）：PostgreSQL 上别的进程出队靠 NOTIFY 叫醒，这是兜底；SQLite 只有本进程
LANE_POLL_S = {"postgresql": 1.0, "sqlite": 5.0}


class SourceLanes:
    """每个数据源一条先来先到的队，记在库里（lane_tickets），几个进程共用一条（docs/design/orchestration-round §5 W2-b）。

    排队 = 插一行（id 自增，先来的号小）；轮到谁 = 这个源上没有已经轮到的、活着的那一行时，号最小的那一行改成「轮到了」。改的那
    一下按源串行：PostgreSQL 上在事务里取这个源的咨询锁，SQLite 只有一个进程、本进程的锁就够。属主不在了的行（进程没了；本进程
    里登记已经没了——测试里事件循环关了留下的）改的时候顺手删掉，后面的接着轮到。"""

    def __init__(self) -> None:
        self._tickets: dict[int, LaneTicket] = {}
        #: 本进程里正在排进某个源的队（插了行、还没登记好 LaneTicket）的个数：这期间属主是本进程、登记里还没有的那一行不能
        #: 当成没人占着删掉
        self._entering: dict[str, int] = {}
        self._locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
        self._listening = False

    def _local_alive(self, row_id: int) -> bool:
        ticket = self._tickets.get(row_id)
        if ticket is None or ticket.released:
            return False
        try:
            return ticket.loop is asyncio.get_running_loop() and not ticket.loop.is_closed()
        except RuntimeError:
            return False

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def _listen(self) -> None:
        if self._listening:
            return
        from app.cluster.signal import signals

        signals.on("lane", lambda parts: self._wake(parts[0] if parts else None))
        self._listening = True

    def _wake(self, source_id: str | None) -> None:
        for ticket in list(self._tickets.values()):
            if (source_id is None or ticket.source_id == source_id) and not ticket.loop.is_closed():
                ticket.loop.call_soon_threadsafe(ticket.woke.set)

    async def _locked(self, session: AsyncSession, source_id: str) -> None:
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text

            from app.cluster.mutex import advisory_key

            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(f"lane:{source_id}")})

    async def _dead_owners(self, source_id: str) -> frozenset[str]:
        """拿这个源的锁之前备好：队里别的进程的属主哪些确实不在了（手上那份不认识的当场再查一次登记）。持着咨询锁时不另取连接
        查库（复核：连接池用尽时会卡到取连接超时）；拿锁之后才出现的属主不在这里面，这一次当活着，下一次再说。"""
        from app.cluster import identity, state
        from app.db.models_cluster import LaneTicket as Row

        async with SessionLocal() as session:
            owners = set((await session.execute(select(Row.owner).where(Row.source_id == source_id).distinct()))
                         .scalars())
        me = identity.owner()
        others = {o for o in owners if o and o != me}
        if not others:
            return frozenset()
        live = await state.live_for(others)
        return frozenset(o for o in others if o not in live.others)

    async def _rows(self, session: AsyncSession, source_id: str, dead_owners: frozenset[str]) -> list[Any]:
        """这个源队里活着的各行（号小的在前）；属主不在了的删掉（不提交）。dead_owners 是拿锁之前备好的那一份：属主是别的进程的
        只认它；属主是自己的看本进程里登记着没有（正有人排进来时一律算活着）。"""
        from app.cluster import identity
        from app.db.models_cluster import LaneTicket as Row

        rows = list((await session.execute(select(Row).where(Row.source_id == source_id).order_by(Row.id))).scalars())
        me = identity.owner()
        entering = bool(self._entering.get(source_id))

        def alive(row: Any) -> bool:
            if not row.owner:
                return False
            if row.owner == me:
                return entering or self._local_alive(row.id)
            return row.owner not in dead_owners

        dead = [r.id for r in rows if not alive(r)]
        if dead:
            from sqlalchemy import delete

            await session.execute(delete(Row).where(Row.id.in_(dead)))
        return [r for r in rows if r.id not in dead]

    async def _advance(self, source_id: str, *, me: LaneTicket | None = None) -> bool:
        """这个源上没有轮到的就让号最小的轮到。交回 me 是不是轮到了。"""
        from app.db.models_cluster import LaneTicket as Row

        granted_now: int | None = None
        dead_owners = await self._dead_owners(source_id)
        async with self._lock(), SessionLocal() as session:
            await self._locked(session, source_id)
            rows = await self._rows(session, source_id, dead_owners)
            head = next((r for r in rows if r.granted), None)
            if head is None and rows:
                head = rows[0]
                await session.execute(update(Row).where(Row.id == head.id).values(granted=True)
                                      .execution_options(synchronize_session=False))
                granted_now = head.id
            await session.commit()
        if granted_now is not None:
            ticket = self._tickets.get(granted_now)
            if ticket is not None:
                ticket.granted.set()
                ticket.woke.set()
            from app.cluster.signal import signals

            signals.publish("lane", source_id)
        return me is not None and head is not None and head.id == me.id

    async def enter(self, source_id: str, kind: str, job_id: str | None = None) -> LaneTicket:
        """排进队尾，马上返回（不等）。排在最前面的立刻 granted；调用方 await ticket.wait() 等轮到自己。kind 要登记过。"""
        from app.cluster import identity
        from app.db.models_cluster import LaneTicket as Row

        if kind not in _LANE_KINDS:
            raise ValueError(f"同源队里没有登记「{kind}」这种事，先 register_lane_kind")
        self._listen()
        self._entering[source_id] = self._entering.get(source_id, 0) + 1
        try:
            async with SessionLocal() as session:
                row = Row(source_id=source_id, kind=kind, job_id=job_id, owner=identity.owner(), granted=False,
                          created_at=utcnow())
                session.add(row)
                await session.commit()
                row_id = int(row.id)
            ticket = LaneTicket(self, row_id, source_id, kind, job_id, asyncio.get_running_loop())
            self._tickets[row_id] = ticket
        finally:
            left = self._entering.get(source_id, 1) - 1
            if left > 0:
                self._entering[source_id] = left
            else:
                self._entering.pop(source_id, None)
        if await self._advance(source_id, me=ticket):
            ticket.granted.set()
        return ticket

    async def try_enter(self, source_id: str, kind: str) -> LaneTicket:
        """队空着才占（马上就轮到）；有人在队里抛 LaneBusy。查和占在同一个事务里、按源串行。"""
        from app.cluster import identity
        from app.db.models_cluster import LaneTicket as Row

        if kind not in _LANE_KINDS:
            raise ValueError(f"同源队里没有登记「{kind}」这种事，先 register_lane_kind")
        self._listen()
        dead_owners = await self._dead_owners(source_id)
        async with self._lock(), SessionLocal() as session:
            await self._locked(session, source_id)
            rows = await self._rows(session, source_id, dead_owners)
            if rows:
                head = next((r for r in rows if r.granted), rows[0])
                await session.commit()
                raise LaneBusy(LaneHolder(head.kind, head.job_id, head.owner))
            row = Row(source_id=source_id, kind=kind, job_id=None, owner=identity.owner(), granted=True,
                      created_at=utcnow())
            session.add(row)
            await session.commit()
            row_id = int(row.id)
        ticket = LaneTicket(self, row_id, source_id, kind, None, asyncio.get_running_loop())
        ticket.granted.set()
        self._tickets[row_id] = ticket
        return ticket

    async def holders(self, source_id: str) -> list[LaneHolder]:
        """队里的各位，排在最前面（正在做）的在前。"""
        dead_owners = await self._dead_owners(source_id)
        async with SessionLocal() as session:
            rows = await self._rows(session, source_id, dead_owners)
            await session.commit()
        rows.sort(key=lambda r: (not r.granted, r.id))
        return [LaneHolder(r.kind, r.job_id, r.owner) for r in rows]

    async def ahead(self, source_id: str, job_id: str) -> list[str] | None:
        """排在这个任务前面的各位是什么（kind，正在做的在前）；这个任务不在队里（刚建、后台还没排进来）返回 None。"""
        holders = await self.holders(source_id)
        for i, holder in enumerate(holders):
            if holder.job_id == job_id:
                return [h.kind for h in holders[:i]]
        return None

    async def _release(self, ticket: LaneTicket) -> None:
        from sqlalchemy import delete

        from app.db.models_cluster import LaneTicket as Row

        if ticket.released:
            return
        ticket.released = True
        self._tickets.pop(ticket.id, None)

        async def go() -> None:
            async with SessionLocal() as session:
                await session.execute(delete(Row).where(Row.id == ticket.id))
                await session.commit()
            await self._advance(ticket.source_id)

        try:
            await asyncio.shield(go())
        except Exception:  # noqa: BLE001 - 删不掉的那一行下次排队时按「本进程里已经没人占着」收回
            logger.exception("同源队里的一位没能出队（源 %s），下次排队时收回", ticket.source_id)
        self._wake(ticket.source_id)

    @contextlib.asynccontextmanager
    async def hold(self, source_id: str, kind: str, job_id: str | None = None) -> AsyncIterator[LaneTicket]:
        """阻塞地占：排队，轮到了再进去，出来时出队。"""
        ticket = await self.enter(source_id, kind, job_id)
        try:
            await ticket.wait()
            yield ticket
        finally:
            await ticket.release()

    @contextlib.asynccontextmanager
    async def try_hold(self, source_id: str, kind: str) -> AsyncIterator[LaneTicket]:
        """不阻塞地试占（同步起草、导入目录）：占不到抛 LaneBusy；占到了，执行期间新建的任务排在后面。"""
        ticket = await self.try_enter(source_id, kind)
        try:
            yield ticket
        finally:
            await ticket.release()


lanes = SourceLanes()


# ==========================================================================
# 收尾、启动时的整理
# ==========================================================================


#: 收尾时在同一个事务里补写的那一步（close_job、cancel_queued 的 finish）：表和任务的状态已经改好、还没提交
Finish = Callable[[AsyncSession], Awaitable[None]]


async def close_job(session: AsyncSession, job_model: Any, task_model: Any, job_id: str, job_status: str,
                    job_error: str | None, task_status: str, task_error: str | None, *,
                    finish: Finish | None = None) -> None:
    """收尾：还没结束的表记 task_status（原因 task_error），任务（还没结束的话）记 job_status。提交。

    finish：提交前在同一个事务里再做的一步（评测按改好的逐项状态重算计数、记停下的原因）。起草、剖析、探查不传，照旧。"""
    now = utcnow()
    await session.execute(
        update(task_model).where(task_model.job_id == job_id, task_model.status.in_(OPEN))
        .values(status=task_status, error=task_error, finished_at=now).execution_options(synchronize_session=False))
    await session.execute(
        update(job_model).where(job_model.id == job_id, job_model.status.in_(ACTIVE))
        .values(status=job_status, error=job_error, finished_at=now).execution_options(synchronize_session=False))
    if finish is not None:
        await finish(session)
    await session.commit()


async def cancel_queued(session: AsyncSession, job_model: Any, task_model: Any, job_id: str, *,
                        finish: Finish | None = None) -> bool:
    """停止一个排队中的任务：任务和没结束的表直接记 cancelled。已经被后台认领（开始了）返回 False，交给调用方按进行中
    的停。finish 同 close_job（只在真停了时做）。"""
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
    if finish is not None:
        await finish(session)
    await session.commit()
    return True


async def claim(session: AsyncSession, job_model: Any, job_id: str) -> bool:
    """认领：排队中 → 进行中，属主记成本进程（多进程：执行就在这里）。开始之前就被停止了返回 False。"""
    from app.db.base import current_owner

    values: dict[str, Any] = {"status": "running", "started_at": utcnow()}
    if hasattr(job_model, "owner"):
        values["owner"] = current_owner()
    result = await session.execute(
        update(job_model).where(job_model.id == job_id, job_model.status == "queued")
        .values(**values).execution_options(synchronize_session=False))
    await session.commit()
    return result.rowcount == 1


async def interrupt_on_startup(job_model: Any, close: Callable[[AsyncSession, str], Awaitable[None]], *,
                               running: Callable[[str], bool], live: Any = None) -> int:
    """接手属主不在了的任务（服务启动时；多进程时当班进程隔一会儿再来一次，见 app/cluster/reaper.py）：排队、进行中、属主
    不在了（进程没了，或者就是本进程、却不在本进程里跑）的任务认领过来、逐个 close（各自记成 interrupted、没结束的表记
    cancelled）。别的活着的进程在跑的一概不碰。不自动续跑。返回整理了几个。

    live：判断属主在不在的那一份（cluster.workers.Liveness），不给就当场查库（启动时的口径：属主是自己的只看本进程）。"""
    from app.cluster import claims, state

    live = live if live is not None else await state.current(fresh=True)
    ids = [job_id for job_id, _ in await claims.orphans(job_model, live, running=running, statuses=ACTIVE)]
    async with SessionLocal() as session:
        for job_id in ids:
            await close(session, job_id)
    return len(ids)


async def stale(rows: Any, *, running: Callable[[str], bool]) -> list[Any]:
    """读接口当场整理用：rows 里记着排队、进行中，属主却不在了的（属主是本进程的看 running——同以前「这个进程里却没在跑」，
    别的进程看租约）。"""
    from app.cluster import state

    rows = [r for r in rows if r.status in ACTIVE]
    if not rows:
        return []
    live = await state.live_for([getattr(r, "owner", None) for r in rows])
    return [r for r in rows if not live.alive(getattr(r, "owner", None), local=running(r.id))]


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
        from app.cluster import fence

        fence.bind()
        try:
            try:
                await self._queue()
                await self._run()
            except fence.Fenced:
                # 本进程续不上租约了：停手、不写收尾（这个任务由接手的进程按「属主不在了」处理）
                logger.error("%s %s：本进程续不上租约，停手、不写收尾", self.kind, self.job_id)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if fence.fenced_here():
                    return
                await self._stopped()
            except JobFailed as e:
                await self._failed(str(e))
            except Exception as e:  # noqa: BLE001 - 任务整体出错：照实记下，不留在「进行中」
                await self._crashed(e)
        finally:
            # 收尾写完才出队：排在后面的任务开始时，这个任务已经记成结束了
            if self.ticket is not None:
                await self.ticket.release()

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
        self.ticket = await lanes.enter(source_id, self.kind, self.job_id)
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
    """一种后台任务：一个 asyncio 任务一件事，停的时候给宽限。factory 按任务编号造一次执行（JobRun）；label 是日志里的叫法
    （「数据目录起草任务」），name 是 asyncio 任务名的前缀，也是跨进程停止请求里的 target。

    执行在认领它的那个进程里（任务表的 owner 列，建任务时缺省就是建它的进程）。停止落在别的进程上时（多进程，见
    app/cluster）：stop 给属主发一条停止请求，属主照本进程的规矩停、收尾，这里等它收完（最多 grace 秒）；属主不在了的交给
    调用方按「不在这个进程里跑」处理（同以前「记录对不上：直接记停止」）。"""

    def __init__(self, factory: Callable[[str], JobRun], *, label: str, name: str) -> None:
        self._runs: dict[str, JobRun] = {}
        self._factory = factory
        self.label = label
        self.name = name
        from app.cluster import fence, requests

        requests.handle("stop", name)(self._stop_requested)
        fence.on_trip(self._stop_fenced)

    def _stop_fenced(self) -> None:
        """本进程隔离了（cluster/fence.py）：停掉手上的任务，停的时候不写收尾。"""
        for job_id in list(self._runs):
            run = self._runs.get(job_id)
            if run is not None and run.task is not None and not run.task.done():
                run.stop_reason = run.stop_reason or "fenced"
                run.task.cancel()

    def model(self) -> Any:
        """任务表（停止时读属主用）：缺省取 factory 上的 model。"""
        return getattr(self._factory, "model", None)

    async def _stop_requested(self, job_id: str, actor: str | None, payload: dict[str, Any]) -> None:
        await self.stop(job_id, str(payload.get("reason") or "user"), grace=float(payload.get("grace") or STOP_GRACE_S),
                        remote=False)

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

    async def stop(self, job_id: str, reason: str, *, grace: float = STOP_GRACE_S, remote: bool = True) -> bool:
        """停掉这个任务，等它收尾（最多 grace 秒）。在本进程里跑的直接停；remote 时在别的活着的进程里跑的给它发停止请求、
        等它收完。都不是（不在跑、属主不在了）返回 False。"""
        run = self._alive(job_id)
        if run is None:
            return await self._stop_remote(job_id, reason, grace) if remote else False
        if run.stop_reason is None:
            run.stop_reason = reason
            run.task.cancel()  # type: ignore[union-attr]
        await asyncio.wait([run.task], timeout=grace)  # type: ignore[list-item]
        return True

    async def _stop_remote(self, job_id: str, reason: str, grace: float) -> bool:
        from app.cluster import identity, requests, workers

        model = self.model()
        if model is None or not hasattr(model, "owner"):
            return False
        async with SessionLocal() as session:
            row = await session.get(model, job_id)
            owner, status = (row.owner, row.status) if row is not None else (None, None)
        if status not in ACTIVE or not owner or owner == identity.owner() or not await workers.alive(owner):
            return False
        await requests.send(owner, "stop", self.name, job_id, payload={"reason": reason, "grace": grace})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace + workers.heartbeat_s()
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            async with SessionLocal() as session:
                status = await session.scalar(select(model.status).where(model.id == job_id))
            if status not in ACTIVE:
                break
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
           "claim", "close_job", "interrupt_on_startup", "lane_kinds", "lane_label", "lanes", "register_lane_kind", "stale"]
