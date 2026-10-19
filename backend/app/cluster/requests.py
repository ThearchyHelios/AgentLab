"""发给别的进程的请求（cluster_requests）：停止它在跑的东西、往它在执行的运行里记一条事件。

**为什么不直接改库。** 一次运行、一轮问数据、一个后台任务的执行在认领它的那个进程里（一个 asyncio 任务），手上拿着模型调用、
查询、写到一半的库操作。别的进程收到「停止」时能做的只是告诉它：写一条请求（收件人是这一行的 owner），PostgreSQL 上 NOTIFY
叫醒它；它隔一拍心跳（最迟 worker_heartbeat_s 秒）也会来取，照本进程里的停止办（停掉执行、按停止的原因收尾、发 run.cancelled
带上是谁停的），界面上的「停止」和单进程时一样。收件人不在了（租约过期）的请求不办：那一行按「属主不在了」的规矩接手
（cluster/reaper.py），请求跟着删掉。

往运行里记事件同理：执行中的运行，序号由执行它的进程发（core/bus 的进程内计数），别的进程直接写会撞号；所以交给它写。
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.cluster import identity
from app.cluster.signal import signals
from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

#: (kind, target) → 办这件事的函数 (target_id, actor, payload)
_HANDLERS: dict[tuple[str, str], Callable[[str, str | None, dict[str, Any]], Awaitable[Any]]] = {}
#: 取请求时本进程里一次只取一份（心跳和 NOTIFY 同时来时不重复办）
_draining: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def handle(kind: str, target: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """登记一种请求怎么办（在各自的模块里，导入时登记）。"""
    def wrap(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        _HANDLERS[(kind, target)] = fn
        return fn
    return wrap


async def send(owner: str, kind: str, target: str, target_id: str, *, actor: str | None = None,
               payload: dict[str, Any] | None = None) -> None:
    """发一条请求给 owner 那个进程（提交之后 NOTIFY 叫醒它）。"""
    from app.db.models_cluster import ClusterRequest

    async with SessionLocal() as session:
        session.add(ClusterRequest(owner=owner, kind=kind, target=target, target_id=target_id, actor=actor,
                                   payload=dict(payload or {})))
        await session.commit()
    signals.publish("q", owner)


async def drain() -> int:
    """取走发给本进程的请求、照办（心跳每一拍、收到 NOTIFY 时）。交回办了几件。一件出错不影响别的。"""
    from app.db.models_cluster import ClusterRequest

    loop = asyncio.get_running_loop()
    lock = _draining.get(loop)
    if lock is None:
        lock = _draining[loop] = asyncio.Lock()
    async with lock:
        return await _drain()


async def _drain() -> int:
    from app.db.models_cluster import ClusterRequest

    me = identity.owner()
    async with SessionLocal() as session:
        rows = list((await session.execute(select(ClusterRequest).where(ClusterRequest.owner == me)
                                           .order_by(ClusterRequest.created_at, ClusterRequest.id))).scalars())
        if not rows:
            return 0
        await session.execute(delete(ClusterRequest).where(ClusterRequest.id.in_([r.id for r in rows])))
        await session.commit()
    done = 0
    for row in rows:
        fn = _HANDLERS.get((row.kind, row.target))
        if fn is None:
            logger.warning("收到不认识的进程间请求 %s/%s，不办", row.kind, row.target)
            continue
        # 各办各的、另起任务：停止要等执行收尾（最多十来秒），不能拖住心跳和后面几件
        task = asyncio.ensure_future(_handle(fn, row.kind, row.target, row.target_id, row.actor, dict(row.payload or {})))
        _running.add(task)
        task.add_done_callback(_running.discard)
        done += 1
    return done


#: 正在办的请求（另起的任务要有人拿着引用）
_running: set[asyncio.Future[Any]] = set()


async def _handle(fn: Callable[..., Awaitable[Any]], kind: str, target: str, target_id: str, actor: str | None,
                  payload: dict[str, Any]) -> None:
    try:
        await fn(target_id, actor, payload)
    except Exception:  # noqa: BLE001
        logger.exception("办进程间请求 %s/%s %s 出错", kind, target, target_id)


async def settle(timeout: float = 5.0) -> None:
    """等手上正在办的请求办完（测试、关停用）。"""
    if _running:
        await asyncio.wait(list(_running), timeout=timeout)


async def discard_for(owners: set[str]) -> int:
    """收件人已经不在了的请求删掉（接手的进程做：那些行按属主不在了的规矩处理）。"""
    from app.db.models_cluster import ClusterRequest

    if not owners:
        return 0
    async with SessionLocal() as session:
        done = await session.execute(delete(ClusterRequest).where(ClusterRequest.owner.in_(sorted(owners))))
        await session.commit()
    return int(done.rowcount or 0)


async def prune(now: datetime | None = None, *, older_than_s: float = 3600.0) -> int:
    """很久没人取的请求删掉（当班进程做）。"""
    from app.db.models_cluster import ClusterRequest

    floor = (now or utcnow()) - timedelta(seconds=older_than_s)
    async with SessionLocal() as session:
        done = await session.execute(delete(ClusterRequest).where(ClusterRequest.created_at < floor))
        await session.commit()
    return int(done.rowcount or 0)


__all__ = ["discard_for", "drain", "handle", "prune", "send"]
