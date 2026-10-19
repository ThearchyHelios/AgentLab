"""进程里缓存着的东西跨进程失效（docs/design/orchestration-round/00-总方案.md §5 W2-b「进程内的账和缓存」）。

有几样东西每个进程读进内存、改的时候只刷新改它的那个进程：登录设置（开没开登录！）、对外访问地址、向量模型、能力登记的
治理快照、数据源的连接池、应用页面的渲染缓存。多进程时别的进程会一直用旧的——登录方式那一样是安全问题。

做法：改的进程照旧刷新自己，再 broadcast(名字, 键) 写一条 cache_events、发 NOTIFY cache；别的进程收到通知（或者隔一拍心跳，
NOTIFY 丢了的兜底）按 id 往后读，调登记过的处理（on）清掉、重读自己那一份。处理按名字登记在各自的模块里。SQLite 只有
一个进程，broadcast 什么都不做（改的进程已经刷新过自己）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select

from app.cluster import identity
from app.cluster.signal import signals
from app.core.config import settings
from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

_handlers: dict[str, Callable[[str | None], Any]] = {}
_last_seen: int | None = None
#: 往回多读几条（复核 R-b5）：id 是先取后提交的，id 大的先提交时游标会越过 id 小的那一条——每次从游标往回留这么多条重读，按处理过
#: 的 id 去重
LOOKBACK = 200
_done: set[int] = set()
_lock: asyncio.Lock | None = None


def on(name: str, handler: Callable[[str | None], Any]) -> None:
    """登记名为 name 的缓存怎么失效：handler(键)，键为 None 是整个清掉。可以是协程函数。"""
    _handlers[name] = handler


def _enabled() -> bool:
    from app.cluster.service import started

    return settings.db_dialect == "postgresql" and started()


async def broadcast(name: str, key: str | None = None) -> None:
    """name（某个键）变了：告诉别的进程。改的进程自己要先刷新好自己那一份（这里不调本进程的处理）。"""
    from app.db.models_cluster import CacheEvent

    if not _enabled():
        return
    try:
        async with SessionLocal() as session:
            session.add(CacheEvent(name=name, key=key, owner=identity.owner()))
            await session.commit()
    except Exception:  # noqa: BLE001 - 写不进去只是别的进程晚一点（它们的缓存各有自己的兜底）
        logger.exception("通知别的进程缓存「%s」变了，没写进库", name)
        return
    signals.publish("cache", name)


async def start() -> None:
    """起服务时：从现在的最后一条往后读（之前的变化，自己启动时读进来的已经是新的）。"""
    global _last_seen, _floor
    from app.db.models_cluster import CacheEvent

    signals.on("cache", lambda _parts: asyncio.ensure_future(poll()))
    async with SessionLocal() as session:
        _last_seen = int(await session.scalar(select(func.coalesce(func.max(CacheEvent.id), 0))) or 0)
    _floor = _last_seen
    _done.clear()


#: 起服务时已有的最后一条：往回重读不越过它（那之前的变化，自己启动时读进来的已经是新的）
_floor = 0


async def poll() -> int:
    """读别的进程写的、还没处理过的几条，照着失效。交回处理了几条。"""
    global _last_seen, _lock
    from app.db.models_cluster import CacheEvent

    if not _enabled() or _last_seen is None:
        return 0
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        async with SessionLocal() as session:
            rows = (await session.execute(select(CacheEvent).where(CacheEvent.id > max(_floor, _last_seen - LOOKBACK))
                                          .order_by(CacheEvent.id).limit(500))).scalars().all()
        done = 0
        for row in rows:
            if int(row.id) in _done:
                continue
            _done.add(int(row.id))
            _last_seen = max(_last_seen, int(row.id))
            if row.owner == identity.owner():
                continue
            handler = _handlers.get(row.name)
            if handler is None:
                continue
            try:
                out = handler(row.key)
                if asyncio.iscoroutine(out):
                    await out
                done += 1
            except Exception:  # noqa: BLE001
                logger.exception("按别的进程的通知重读缓存「%s」出错", row.name)
        if len(_done) > 4 * LOOKBACK:
            low = _last_seen - 2 * LOOKBACK
            _done.difference_update({i for i in _done if i <= low})
        return done


async def prune() -> int:
    from app.db.models_cluster import CacheEvent

    async with SessionLocal() as session:
        done = await session.execute(delete(CacheEvent).where(CacheEvent.created_at < utcnow() - timedelta(hours=1)))
        await session.commit()
    return int(done.rowcount or 0)


__all__ = ["broadcast", "on", "poll", "prune", "start"]
