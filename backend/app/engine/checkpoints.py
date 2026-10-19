"""LangGraph 的断点库：应用库是 SQLite 时用 AsyncSqliteSaver（数据目录里的 checkpoints.db，和业务表分开免得抢写锁），
是 PostgreSQL 时用官方的 AsyncPostgresSaver（psycopg 3 + 连接池），放在同一个库里，AGENTLAB_CHECKPOINT_URL 另给就放那里。

它建的四张表（checkpoints、checkpoint_blobs、checkpoint_writes、checkpoint_migrations）和应用的表不重名
（test_db_portable 守着），所以同一个库、同一个 schema 就行。

应用里读断点一律走 saver 的公开接口（aget_state / aget_state_history / aget_tuple / alist），不直接查这几张表：
两种库的表结构完全不一样（SQLite 整份断点是一个 msgpack，PostgreSQL 拆成 JSONB 加 blob）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.config import settings

#: 建断点表时拿的会话级咨询锁的号：几个进程同时启动，各自的 setup（建表、记迁移版本）一个一个来
_SETUP_LOCK = 0x4167656E74436B70  # "AgentCkp"


def _pool_size() -> int:
    # 运行里每个超步写一次断点、每个任务写一次 pending writes，都是一来一回的短操作。并发运行多时多给几条
    return max(4, min(settings.max_concurrent_runs, 20))


@contextlib.asynccontextmanager
async def open_postgres_saver(conninfo: str, *, max_size: int | None = None,
                              application_name: str = "agentlab") -> AsyncIterator[BaseCheckpointSaver]:
    """PostgreSQL 的断点库：开连接池、建表（幂等），退出时关池子。application_name 同应用库（db/base.APPLICATION_NAME）。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    # 连接参数照 AsyncPostgresSaver.from_conn_string：自动提交（它不自己管事务）、语句第一次执行就预备、行按 dict 取
    pool = AsyncConnectionPool(
        conninfo, min_size=1, max_size=max_size or _pool_size(), open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "application_name": application_name},
        check=AsyncConnectionPool.check_connection, name="agentlab-checkpoints")
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK,))
            try:
                await saver.setup()
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK,))
        yield saver
    finally:
        await pool.close()


@contextlib.asynccontextmanager
async def open_sqlite_saver(path: Path | str) -> AsyncIterator[BaseCheckpointSaver]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def open_saver() -> contextlib.AbstractAsyncContextManager[BaseCheckpointSaver]:
    """按配置开这个进程的断点库。"""
    conninfo = settings.checkpoint_conninfo
    if conninfo:
        return open_postgres_saver(conninfo)
    return open_sqlite_saver(settings.checkpoint_path)


async def count(saver: Any, thread_id: str | None = None) -> tuple[int, int]:
    """(断点条数, pending writes 条数)。thread_id 为空数全部。copy-to-postgres 核对用，走公开接口。"""
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    checkpoints = writes = 0
    async for tup in saver.alist(config):
        checkpoints += 1
        writes += len(tup.pending_writes or ())
    return checkpoints, writes
