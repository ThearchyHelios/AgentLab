"""认领属主不在了的行（cluster/reaper 的各种规矩共用）：先看库里还没结束的那些行和它们的属主，属主不在了的逐行用带条件的
UPDATE 改成自己（状态还是刚才看到的、属主还是刚才看到的那个）——几个进程同时接手，同一行只有一个改得动。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import select, update

from app.cluster import identity
from app.cluster.workers import Liveness
from app.db.base import SessionLocal


def same(column: Any, value: Any) -> Any:
    """column 还是 value（None 用 IS NULL 比）。"""
    return column.is_(None) if value is None else column == value


async def orphans(model: Any, live: Liveness, *, running: Callable[[str], bool], statuses: Iterable[str],
                  extra: Any = None) -> list[tuple[str, str]]:
    """model 里状态在 statuses、属主不在了的行认领过来（owner 改成本进程），交回 [(id, 原来的状态)]。running：这一行在本进程里
    是不是真的在跑。extra：另加的筛选条件。"""
    wanted = tuple(statuses)
    stmt = select(model.id, model.owner, model.status).where(model.status.in_(wanted))
    if extra is not None:
        stmt = stmt.where(extra)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
    me = identity.owner()
    out: list[tuple[str, str]] = []
    for row_id, owner, status in rows:
        if live.alive(owner, local=running(row_id)):
            continue
        async with SessionLocal() as session:
            done = await session.execute(update(model).where(model.id == row_id, model.status == status,
                                                             same(model.owner, owner))
                                         .values(owner=me).execution_options(synchronize_session=False))
            await session.commit()
        if done.rowcount == 1:
            out.append((row_id, status))
    return out


__all__ = ["orphans", "same"]
