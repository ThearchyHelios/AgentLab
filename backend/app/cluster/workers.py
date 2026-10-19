"""工作进程的登记和租约（cluster_workers），以及「这一行的属主还在不在」。

- register / renew / unregister：启动时登记、隔一拍心跳续租约、平稳关停时删掉自己那一行（别的进程马上就知道它不在了，
  不必等租约到期）。
- liveness()：现在哪些别的进程还活着（租约没过期）。各种「启动整理」「接手」都只碰属主不在了的行：
  Liveness.alive(owner, local=…) —— 属主是别的进程：看它的租约；属主是自己：看这一行在本进程里是不是真的在跑（local），
  同一个进程里前一次起停留下的（测试里一个进程起停多次服务）照样算不在了；没有属主（升级前的行）算不在了。

时间按应用的 utcnow（各台机器要对时，NTP）：租约以十秒计，几百毫秒的偏差无碍。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.cluster import identity
from app.core.config import settings
from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)


def lease_s() -> float:
    """租约多长（秒）：至少是心跳间隔的三倍——库一时慢了、一拍没续上，不算进程没了。"""
    return max(float(settings.worker_lease_s), 3.0 * float(settings.worker_heartbeat_s))


def heartbeat_s() -> float:
    return float(settings.worker_heartbeat_s)


@dataclass(frozen=True)
class Liveness:
    """某一刻还活着的别的进程。"""

    others: frozenset[str] = frozenset()
    #: 属主是自己的行一律算活着（当班进程隔一会儿接手时用：自己刚建、还没交给后台任务的行不能当成没人管）。启动时的那一次
    #: 不信：那时本进程什么都还没在跑，属主是自己的只会是同一个进程里前一次起停留下的
    trust_mine: bool = False

    def alive(self, owner: str | None, *, local: bool = False) -> bool:
        """这一行的属主还在不在。local：这一行在本进程里是不是真的在跑（属主是自己时只看它）。"""
        if not owner:
            return False
        if owner == identity.owner():
            return local or self.trust_mine
        return owner in self.others

    def trusting(self) -> Liveness:
        return Liveness(self.others, trust_mine=True)


async def liveness(now: datetime | None = None) -> Liveness:
    """现在还活着（租约没过期）的别的进程。"""
    from app.db.models_cluster import ClusterWorker

    now = now or utcnow()
    me = identity.owner()
    async with SessionLocal() as session:
        ids = (await session.execute(select(ClusterWorker.id).where(
            ClusterWorker.lease_until > now, ClusterWorker.id != me))).scalars().all()
    return Liveness(frozenset(ids))


async def alive(owner: str | None, *, local: bool = False) -> bool:
    """单独问一个属主（停止时找收件人用）。"""
    if not owner or owner == identity.owner():
        return bool(owner) and local
    return (await liveness()).alive(owner)


def _info() -> dict[str, Any]:
    return {"data_dir": str(settings.data_dir), "workers": settings.workers}


async def register(*, exclusive: bool = False) -> None:
    """登记自己（已有就续上）。exclusive：库里别的进程一律当成已经不在了，删掉它们的登记（SQLite：只有一个进程，
    守卫已经确认没有别的进程在用这个数据目录，见 cluster/guard.py）。"""
    from app.db.models_cluster import ClusterWorker

    me = identity.owner()
    now = utcnow()
    async with SessionLocal() as session:
        if exclusive:
            await session.execute(delete(ClusterWorker).where(ClusterWorker.id != me))
        row = await session.get(ClusterWorker, me)
        if row is None:
            session.add(ClusterWorker(id=me, host=identity.host(), pid=os.getpid(), started_at=now, heartbeat_at=now,
                                      lease_until=now + timedelta(seconds=lease_s()), info=_info()))
        else:
            row.heartbeat_at, row.lease_until = now, now + timedelta(seconds=lease_s())
        await session.commit()


async def renew() -> bool:
    """续一拍租约。自己那一行不见了（别的进程以为我没了、清掉了）返回 False，调用方重新登记。"""
    from app.db.models_cluster import ClusterWorker

    now = utcnow()
    async with SessionLocal() as session:
        done = await session.execute(update(ClusterWorker).where(ClusterWorker.id == identity.owner()).values(
            heartbeat_at=now, lease_until=now + timedelta(seconds=lease_s()))
            .execution_options(synchronize_session=False))
        await session.commit()
    return done.rowcount == 1


async def unregister() -> None:
    from app.db.models_cluster import ClusterWorker

    async with SessionLocal() as session:
        await session.execute(delete(ClusterWorker).where(ClusterWorker.id == identity.owner()))
        await session.commit()


async def prune(now: datetime | None = None) -> int:
    """删掉早就过期的登记（当班进程做）：租约过期超过十倍租约时长的。它们认领的行早已被接手。"""
    from app.db.models_cluster import ClusterWorker

    floor = (now or utcnow()) - timedelta(seconds=10 * lease_s())
    async with SessionLocal() as session:
        done = await session.execute(delete(ClusterWorker).where(ClusterWorker.lease_until < floor))
        await session.commit()
    return int(done.rowcount or 0)


async def listing() -> list[dict[str, Any]]:
    """现在登记着的进程（系统信息页、验收脚本看）。"""
    from app.db.models_cluster import ClusterWorker

    now = utcnow()
    async with SessionLocal() as session:
        rows = (await session.execute(select(ClusterWorker).order_by(ClusterWorker.started_at))).scalars().all()
    return [{"id": r.id, "host": r.host, "pid": r.pid, "alive": r.lease_until > now, "me": r.id == identity.owner(),
             "started_at": r.started_at.isoformat() if r.started_at else None,
             "heartbeat_at": r.heartbeat_at.isoformat() if r.heartbeat_at else None} for r in rows]


__all__ = ["Liveness", "alive", "heartbeat_s", "lease_s", "listing", "liveness", "prune", "register", "renew",
           "unregister"]
