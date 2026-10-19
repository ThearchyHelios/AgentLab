"""每日额度：先占一份额度再做事，做不成退回（ledger/issue 的出具上限、apps/resolve 的按需视图上限）。

**为什么不靠锁。** 以前是「数一数今天做了几次，没到上限就发起」，数和发起之间靠一把锁串行；多进程时那把锁要么只管本进程（几个
进程一起越过上限），要么全站一把（几份出具只能一个接一个发起）。现在额度记在库里（rate_counters 里按「名字 + 本地日期」一行），
占额度是一句带条件的 UPDATE：还没到上限才加一，否则什么都不改——两种库上这一句都是原子的，几个进程、几个协程同时来也正好
占到上限为止，发起本身可以同时做。发起失败（被拒、出错）把那一份退回。

这一天第一次占的时候按库里已有的记录起算（seed：比如今天已经出具过几次），之后只看这一行。进程在占了额度、发起之前没了的，
那一份不退：宁可这一天少出一次，不会多出。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.db.base import SessionLocal, utcnow


def today() -> str:
    """本地日期（每日上限按服务端所在时区的自然日算，同以前按本地零点数）。"""
    return datetime.now().astimezone().date().isoformat()


def _key(name: str, day: str) -> str:
    return f"quota:{name}:{day}"


async def take(name: str, limit: int, *, seed: Callable[[Any], Awaitable[int]] | None = None,
               day: str | None = None) -> str | None:
    """占一份额度：这一天占过的还不到 limit 就加一、交回占在了哪一天（本地日期），到了交回 None（什么都不改）。seed(session)：
    这一天第一次占时已经用掉了几份（按库里的记录数）。退回时把交回的日期给 give_back（复核 R-b11：23:59:59 占、00:00:01 发起
    失败，要退回前一天那一份，不是第二天的）。"""
    from app.db.models_cluster import RateCounter as Row
    from app.db.portable import insert_ignore

    day = day or today()
    key = _key(name, day)
    async with SessionLocal() as session:
        if await session.scalar(select(Row.key).where(Row.key == key)) is None:
            used = int(await seed(session)) if seed is not None else 0
            await session.execute(insert_ignore(session, Row.__table__).values(
                key=key, count=used, value=0.0, at=0.0, expires_at=utcnow() + timedelta(days=3))
                .on_conflict_do_nothing(index_elements=["key"]))
        done = await session.execute(update(Row).where(Row.key == key, Row.count < int(limit))
                                     .values(count=Row.count + 1).execution_options(synchronize_session=False))
        await session.commit()
    return day if done.rowcount == 1 else None


async def give_back(name: str, *, day: str | None = None) -> None:
    """退回一份（占了额度、却没发起成）。day 给 take 交回的那个日期（不给按今天，只有测试这么用）。"""
    from app.db.models_cluster import RateCounter as Row

    key = _key(name, day or today())
    async with SessionLocal() as session:
        await session.execute(update(Row).where(Row.key == key, Row.count > 0).values(count=Row.count - 1)
                              .execution_options(synchronize_session=False))
        await session.commit()


async def used(name: str, *, day: str | None = None) -> int | None:
    """这一天占了几份（没占过为 None）。"""
    from app.db.models_cluster import RateCounter as Row

    async with SessionLocal() as session:
        return await session.scalar(select(Row.count).where(Row.key == _key(name, day or today())))


__all__ = ["give_back", "take", "today", "used"]
