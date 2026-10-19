"""调度当班：全站只许一个进程做的事（定时认领、事件派发、Webhook 投递、流程的时限和核实、各类周期清理、接手不在了的
进程留下的活）由库里的一份租约定下谁做（cluster_leases 的 name=scheduler 那一行）。

一句条件 UPDATE 同时管「接班」和「续租」：这一行的 owner 是自己，或者租约已经过期（当班的进程没了）、没人当班，就改成自己、
租约往后推。改到了一行就是当班。当班的进程平稳关停时交班（owner 清空），待命的下一拍心跳就接上；强杀的等租约过期。

两个进程短暂地都以为自己当班（续租那一拍库慢了、本地还没过期）时，重复不了：定时认领本来就是条件 UPDATE、触发记录
(任务, 时刻, 种类) 唯一，事件派发按投递记录的唯一约束，待办的升级、核实各自也是条件更新。当班只是让它们平常不撞。
本地另留一截余量（MARGIN）：租约快到期还没续上时，自己先当作不当班了。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import case, or_, update

from app.cluster import identity
from app.cluster.workers import lease_s
from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

SCHEDULER = "scheduler"
#: 本地判断「还在当班」时留的余量（秒）
MARGIN_S = 2.0


class Leadership:
    def __init__(self, name: str) -> None:
        self.name = name
        self.until: datetime | None = None
        self.epoch = 0

    def held(self, now: datetime | None = None) -> bool:
        """本地看：还在当班（租约离到期还有余量）。"""
        if self.until is None:
            return False
        return (now or utcnow()) + timedelta(seconds=MARGIN_S) < self.until

    async def claim(self, now: datetime | None = None) -> bool:
        """接班或续租。交回这一拍之后是不是当班。"""
        from app.db.models_cluster import ClusterLease
        from app.db.portable import insert_ignore

        now = now or utcnow()
        until = now + timedelta(seconds=lease_s())
        me = identity.owner()
        mine = ClusterLease.owner == me
        async with SessionLocal() as session:
            done = await session.execute(
                update(ClusterLease).where(
                    ClusterLease.name == self.name,
                    or_(mine, ClusterLease.owner.is_(None), ClusterLease.lease_until.is_(None),
                        ClusterLease.lease_until < now))
                .values(owner=me, lease_until=until,
                        acquired_at=case((mine, ClusterLease.acquired_at), else_=now),
                        epoch=case((mine, ClusterLease.epoch), else_=ClusterLease.epoch + 1))
                .returning(ClusterLease.epoch)
                .execution_options(synchronize_session=False))
            epoch = done.scalar_one_or_none()
            if epoch is None:
                # 还没有这一行（新库）：插一行，撞了说明别人刚插上，这一拍不当班
                inserted = await session.execute(insert_ignore(session, ClusterLease.__table__).values(
                    name=self.name, owner=me, lease_until=until, acquired_at=now, epoch=1)
                    .on_conflict_do_nothing(index_elements=["name"]))
                epoch = 1 if inserted.rowcount == 1 else None
            await session.commit()
        was = self.held(now)
        if epoch is None:
            if was or self.until is not None:
                logger.warning("调度当班交给了别的进程（本进程 %s 不再当班）", identity.describe(me))
            self.until = None
            return False
        if not was and self.epoch != epoch:
            logger.info("本进程（%s）接手调度当班（第 %d 班）", identity.describe(me), epoch)
        self.until, self.epoch = until, int(epoch)
        return True

    async def release(self) -> None:
        """交班（平稳关停时）。"""
        from app.db.models_cluster import ClusterLease

        if self.until is None:
            return
        self.until = None
        async with SessionLocal() as session:
            await session.execute(update(ClusterLease).where(
                ClusterLease.name == self.name, ClusterLease.owner == identity.owner())
                .values(owner=None, lease_until=None).execution_options(synchronize_session=False))
            await session.commit()


scheduler = Leadership(SCHEDULER)


async def current(name: str = SCHEDULER) -> dict[str, object] | None:
    """库里记着谁当班（系统信息、验收脚本看）。"""
    from app.db.models_cluster import ClusterLease

    async with SessionLocal() as session:
        row = await session.get(ClusterLease, name)
    if row is None:
        return None
    return {"owner": row.owner, "until": row.lease_until.isoformat() if row.lease_until else None, "epoch": row.epoch,
            "me": row.owner == identity.owner()}


__all__ = ["Leadership", "MARGIN_S", "SCHEDULER", "current", "scheduler"]
