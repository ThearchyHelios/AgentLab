"""几个进程共用的计数：访问密钥每分钟限流的令牌桶（auth/keys）、登录失败的退避（auth/sessions）。

应用库是 PostgreSQL（可以多进程）时记在库里（rate_counters），一次请求一句带条件的 upsert：令牌桶「补多少、扣一个」在一句
INSERT … ON CONFLICT DO UPDATE … WHERE 里算完，几个进程同时来也扣不过头；登录失败先把次数加一、占住那一行，再按次数算要等
多久。SQLite 只有一个进程，计数照旧在进程里（调用方自己的那份），不为每个请求多写一次库——单进程时进程就是全站。

时间用墙钟（time.time）：几台机器各自的单调时钟不能比，墙钟要对时（NTP），同租约。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.cluster.service import started
from app.core.config import settings
from app.db.base import SessionLocal, utcnow


def shared() -> bool:
    """计数要不要记在库里：PostgreSQL、而且起了协调（服务在跑）。测试、脚本里直接调用时照旧在进程里。"""
    return settings.db_dialect == "postgresql" and started()


def _expiry(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


async def take_token(key: str, *, per_minute: int, now: float | None = None) -> int:
    """令牌桶：满桶 per_minute 个，每秒补 per_minute / 60 个。放行交回 0，超了交回还要等几秒。"""
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models_cluster import RateCounter as Row

    rate = per_minute / 60.0
    cap = float(per_minute)
    moment = time.time() if now is None else now
    refill = func_least(cap, Row.value + (moment - Row.at) * rate)
    stmt = insert(Row).values(key=key, count=1, value=cap - 1.0, at=moment, expires_at=_expiry(120)) \
        .on_conflict_do_update(index_elements=[Row.key],
                               set_={"value": refill - 1.0, "at": moment, "count": Row.count + 1,
                                     "expires_at": _expiry(120)},
                               where=refill >= 1.0) \
        .returning(Row.value)
    async with SessionLocal() as session:
        got = (await session.execute(stmt)).first()
        if got is not None:
            await session.commit()
            return 0
        row = await session.get(Row, key)
        await session.rollback()
    tokens = min(cap, float(row.value) + (moment - float(row.at)) * rate) if row is not None else 0.0
    return max(1, math.ceil((1.0 - tokens) / rate))


def func_least(a: float, b: object) -> object:
    from sqlalchemy import func

    return func.least(a, b)


async def wait_seconds(*keys: str) -> int:
    """登录失败退避：这几个键里还要等多久（秒），不用等为 0。"""
    from app.db.models_cluster import RateCounter as Row

    if not keys:
        return 0
    now = time.time()
    async with SessionLocal() as session:
        rows = (await session.execute(select(Row.value).where(Row.key.in_([f"login:{k}" for k in keys])))).scalars()
        until = max((float(v) for v in rows), default=0.0)
    return int(until - now) + 1 if until > now else 0


async def failed(*keys: str, free_tries: int, max_wait_s: int) -> None:
    """记一次登录失败：次数加一，满 free_tries 次之后要等 2^(次数-free_tries) 秒（最多 max_wait_s）。"""
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models_cluster import RateCounter as Row

    now = time.time()
    async with SessionLocal() as session:
        for key in keys:
            name = f"login:{key}"
            count = (await session.execute(
                insert(Row).values(key=name, count=1, value=0.0, at=now, expires_at=_expiry(86400))
                .on_conflict_do_update(index_elements=[Row.key], set_={"count": Row.count + 1, "at": now,
                                                                       "expires_at": _expiry(86400)})
                .returning(Row.count))).scalar_one()
            wait = 0 if count < free_tries else min(2 ** (count - free_tries), max_wait_s)
            await session.execute(update(Row).where(Row.key == name).values(value=now + wait)
                                  .execution_options(synchronize_session=False))
        await session.commit()


async def succeeded(*keys: str) -> None:
    from app.db.models_cluster import RateCounter as Row

    async with SessionLocal() as session:
        await session.execute(delete(Row).where(Row.key.in_([f"login:{k}" for k in keys])))
        await session.commit()


async def prune(now: datetime | None = None) -> int:
    """到期的计数删掉（当班进程做）。"""
    from app.db.models_cluster import RateCounter as Row

    async with SessionLocal() as session:
        done = await session.execute(delete(Row).where(Row.expires_at < (now or datetime.now(timezone.utc))))
        await session.commit()
    return int(done.rowcount or 0)


__all__ = ["failed", "prune", "shared", "succeeded", "take_token", "wait_seconds"]
