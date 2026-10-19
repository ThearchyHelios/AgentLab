"""本进程在协调里的那一份：启动时登记、起心跳，关停时交班、删登记。

续租约在单独的任务、单独的连接上（不走连接池，不被同一拍别的活拖住）；续晚了（离上次续上超过一个租约时长）就隔离：停掉手上的
执行、不写终态，换一个身份重新登记（cluster/fence.py）。SQLite 只有一个进程：登记只写一次、不续租、本进程直接当班、接手只在
启动时做，心跳只做本进程自己攒着的活。

心跳每一拍（AGENTLAB_WORKER_HEARTBEAT_S，缺省 5 秒；PostgreSQL）：
1. （续租约另有任务）；
2. 刷新「别的进程谁还活着」；
3. 取走发给本进程的请求照办（停止、往运行里记事件，cluster/requests.py）；
4. 本进程自己的周期活（on_beat 登记的：访问密钥的请求记录写库……——这些是每个进程攒在自己内存里的，只能自己写）；
5. 争 / 续调度当班（cluster/leader.py）；当班的进程在有进程走了（少了一个、收到 gone 通知）或者隔一个租约时长时，接手
   不在了的进程留下的行（cluster/reaper.py），顺手删掉早就过期的登记和没人取的请求。

调度循环（scheduler/loop.py）在每个进程里都起，每一拍先看本进程当不当班，不当班就什么都不做。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.cluster import guard, identity, leader, reaper, requests, state, workers
from app.cluster.signal import signals
from app.core.config import settings

logger = logging.getLogger(__name__)

_beats: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
_leader_listeners: list[Callable[[bool], Any]] = []


def on_beat(name: str, fn: Callable[[], Awaitable[Any]]) -> None:
    """登记一件本进程每拍心跳都要做的事（同名换成新的）。"""
    for i, (known, _) in enumerate(_beats):
        if known == name:
            _beats[i] = (name, fn)
            return
    _beats.append((name, fn))


def on_leadership(fn: Callable[[bool], Any]) -> None:
    """当班换了（接手 True、交出 False）时调。"""
    if fn not in _leader_listeners:
        _leader_listeners.append(fn)


def leading() -> bool:
    """本进程现在当不当调度班。没起协调（测试、脚本里直接调调度循环、直接 run_manager.setup）时算当班：那时只有这一个进程。"""
    return not service.started or leader.scheduler.held()


class _Service:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.lease_task: asyncio.Task[None] | None = None
        self.wake: asyncio.Event | None = None
        self.reap_now = False
        self.last_reap = 0.0
        self.others: frozenset[str] = frozenset()
        self.started = False
        #: SQLite：守卫已经确认只有本进程——登记只写一次、不续租、本进程直接当班、接手只在启动时做（不为协调多写库）
        self.single = False
        self._lease_conn: Any = None

    async def start(self) -> None:
        """登记、开始收发通知、起心跳。SQLite 时先取单进程的守卫（拿不到抛 guard.SingleProcessOnly）。"""
        if self.started:
            return
        guard.acquire()
        self.single = settings.db_dialect == "sqlite"
        await workers.register(exclusive=self.single)
        live = await workers.liveness()
        state.remember(live)
        self.others = live.others
        await signals.start()
        signals.on("q", self._on_request)
        signals.on("gone", self._on_gone)
        if not self.single:
            await signals.ready(timeout=5.0)
        self.wake = asyncio.Event()
        self.started = True
        from app.cluster import caches, fence

        await caches.start()
        if self.single:
            # 只有本进程：当班不用争（不写库），心跳只做本进程自己攒着的活（访问密钥的请求记录）
            leader.scheduler.single()
            self.task = asyncio.create_task(self._light_loop(), name="agentlab-cluster-beats")
        else:
            await self._lead()
            fence.activate()
            fence.on_trip(self._on_fenced)
            self.lease_task = asyncio.create_task(self._lease_loop(), name="agentlab-cluster-lease")
            self.task = asyncio.create_task(self._loop(), name="agentlab-cluster-heartbeat")
        logger.info("工作进程 %s 已登记（%s，租约 %g 秒、心跳 %g 秒；现在另有 %d 个进程）", identity.describe(identity.owner()),
                    "SQLite，单进程" if self.single else "PostgreSQL，可以多进程",
                    workers.lease_s(), workers.heartbeat_s(), len(live.others))

    def _on_request(self, parts: list[str]) -> None:
        if parts and parts[0] == identity.owner() and self.wake is not None:
            asyncio.ensure_future(requests.drain())

    def _on_gone(self, _parts: list[str]) -> None:
        self.reap_now = True
        if self.wake is not None:
            self.wake.set()

    # ---------------- 续租约：单独的任务、单独的连接 ----------------

    async def _lease_conn_open(self) -> Any:
        import asyncpg

        from app.cluster.signal import listen_dsn
        from app.db.base import APPLICATION_NAME

        if self._lease_conn is None or self._lease_conn.is_closed():
            self._lease_conn = await asyncpg.connect(listen_dsn(), server_settings={"application_name": APPLICATION_NAME},
                                                     timeout=max(1.0, workers.heartbeat_s()))
        return self._lease_conn

    async def renew(self) -> bool:
        """续一拍租约（在专用连接上，不走连接池：池子被占满时取连接要等，等得比租约还长）。按库的时钟续、按库的时钟核对（复核
        R-b7）：库里自己的租约已经过期（或者登记不见了），别的进程可能已经接手，不再续这个身份——隔离（cluster/fence.py），换身份
        重新登记。本地的两只钟说续晚了、或者两只钟差出一个租约（墙钟跳了），同样隔离。交回续没续上。"""
        from app.cluster import fence

        if fence.stale():
            fence.trip("续租约的时候发现已经续晚了")
            return False
        if fence.jumped():
            fence.trip("续租约的时候发现本机的墙钟和单调钟差出一个租约以上（墙钟跳了，或者休眠没计进单调钟）")
            return False
        try:
            conn = await self._lease_conn_open()
            renewed = await asyncio.wait_for(conn.fetchval(
                "UPDATE cluster_workers SET heartbeat_at = timezone('UTC', clock_timestamp()), "
                "lease_until = timezone('UTC', clock_timestamp()) + make_interval(secs => $2) "
                "WHERE id = $1 AND lease_until >= timezone('UTC', clock_timestamp()) RETURNING id",
                identity.owner(), float(workers.lease_s())), timeout=workers.heartbeat_s())
            if renewed is None:
                expired = await asyncio.wait_for(conn.fetchval(
                    "SELECT lease_until < timezone('UTC', clock_timestamp()) FROM cluster_workers WHERE id = $1",
                    identity.owner()), timeout=workers.heartbeat_s())
        except Exception:  # noqa: BLE001 - 库一时连不上：这一拍没续上，下一拍再试（续晚了的话下一拍隔离）
            logger.warning("续租约没续上，下一拍再试", exc_info=True)
            with contextlib.suppress(Exception):
                if self._lease_conn is not None:
                    await self._lease_conn.close()
            self._lease_conn = None
            return False
        if renewed is None:
            fence.trip("按库的时钟本进程的租约已经过期" if expired else "本进程的登记不见了（被当成不在了）")
            return False
        fence.renewed()
        return True

    async def _lease_loop(self) -> None:
        while True:
            await self.renew()
            await asyncio.sleep(workers.heartbeat_s())

    def _on_fenced(self) -> None:
        """隔离了：手上的执行由各处自己停（runner、job_runtime 登记的处理），这里换身份重新登记。"""
        asyncio.ensure_future(self._reidentify())

    async def _reidentify(self) -> None:
        from app.cluster import fence

        old = identity.owner()
        new = identity.regenerate()
        leader.scheduler.drop()
        try:
            await workers.register()
            fence.renewed()
            logger.warning("隔离之后换了身份重新登记：%s → %s（之前认领的行由当班进程按「属主不在了」接手）",
                           identity.describe(old), identity.describe(new))
        except Exception:  # noqa: BLE001
            logger.exception("隔离之后重新登记失败，下一拍再试")

    # ---------------- 当班、心跳的其余活 ----------------

    async def _lead(self) -> bool:
        was = leader.scheduler.held()
        try:
            now_leading = await leader.scheduler.claim()
        except Exception:  # noqa: BLE001 - 库一时不通：本地按没当班算
            logger.exception("争调度当班时出错")
            now_leading = False
        if now_leading != was:
            for fn in list(_leader_listeners):
                try:
                    out = fn(now_leading)
                    if asyncio.iscoroutine(out):
                        await out
                except Exception:  # noqa: BLE001
                    logger.exception("当班换了之后的处理出错")
            if now_leading:
                self.reap_now = True
        return now_leading

    async def beat(self) -> None:
        """心跳的一拍（续租约另在 _lease_loop；测试里直接调）。SQLite 只做本进程自己攒着的活。"""
        if self.single:
            await self._own_work()
            return
        live = await workers.liveness()
        state.remember(live)
        if self.others - live.others:
            self.reap_now = True
        self.others = live.others
        try:
            await requests.drain()
        except Exception:  # noqa: BLE001
            logger.exception("取发给本进程的请求时出错")
        try:
            from app.cluster import caches

            await caches.poll()
        except Exception:  # noqa: BLE001
            logger.exception("读别的进程的缓存变化时出错")
        await self._own_work()
        if await self._lead() and (self.reap_now or time.monotonic() - self.last_reap >= workers.lease_s()):
            # 隔一会儿的这一次信得过自己：属主是本进程的行（刚建、还没交给后台任务的）不当成没人管
            await self.reap(live.trusting())

    async def _own_work(self) -> None:
        for name, fn in list(_beats):
            try:
                await fn()
            except Exception:  # noqa: BLE001
                logger.exception("心跳里的「%s」出错，下一拍再试", name)

    async def reap(self, live: workers.Liveness | None = None) -> dict[str, Any]:
        self.reap_now = False
        self.last_reap = time.monotonic()
        live = live or await state.current(fresh=True)
        dead = await _dead_owners(live)
        out = await reaper.reap(live)
        if dead:
            await requests.discard_for(dead)
        with contextlib.suppress(Exception):
            await workers.prune()
            await requests.prune()
            from app.cluster import caches, counters

            # 到期的计数、额度（每日额度两种库都记在库里）
            await counters.prune()
            if settings.db_dialect == "postgresql":
                await caches.prune()
        return out

    async def _loop(self) -> None:
        assert self.wake is not None
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=workers.heartbeat_s())
            self.wake.clear()
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一拍出错（库一时不通）不该让心跳停下
                logger.exception("心跳这一拍出错，下一拍照常进行")

    async def _light_loop(self) -> None:
        while True:
            await asyncio.sleep(workers.heartbeat_s())
            await self._own_work()

    async def stop(self) -> None:
        """关停：停心跳、交班、删登记（别的进程马上接手本进程留下的行），停收发通知。"""
        if not self.started:
            return
        self.started = False
        from app.cluster import fence

        for task in (self.lease_task, self.task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.task = self.lease_task = None
        fence.deactivate()
        with contextlib.suppress(Exception):
            await leader.scheduler.release()
        with contextlib.suppress(Exception):
            await workers.unregister()
        if self._lease_conn is not None:
            with contextlib.suppress(Exception):
                await self._lease_conn.close()
            self._lease_conn = None
        signals.publish("gone", identity.owner())
        await signals.stop()
        state.forget()
        guard.release()


async def startup_reap() -> dict[str, Any]:
    """启动时的那一次接手（每个进程都做；那时本进程什么都还没在跑，属主是自己的只会是前一次起停留下的）。"""
    return await service.reap(await state.current(fresh=True))


async def _dead_owners(live: workers.Liveness) -> set[str]:
    """库里登记着、租约已过期的进程（它们的请求删掉）。"""
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models_cluster import ClusterRequest

    async with SessionLocal() as session:
        owners = set((await session.execute(select(ClusterRequest.owner).distinct())).scalars())
    return {o for o in owners if o != identity.owner() and o not in live.others}


service = _Service()


async def start() -> None:
    await service.start()


async def stop() -> None:
    await service.stop()


def started() -> bool:
    return service.started


__all__ = ["leading", "on_beat", "on_leadership", "service", "start", "started", "startup_reap", "stop"]
