"""跨进程的互斥锁：本进程里一把 asyncio 锁（按事件循环各一把），应用库是 PostgreSQL 时另取一把会话级咨询锁
（pg_advisory_lock，按锁名的哈希取号），持锁期间占着一条专门的连接，放锁时在同一条连接上放。SQLite 只有一个进程，
本进程的锁就是全站的锁。

用在「先看库里有什么、再改文件」这种一个库事务包不住的地方：上传表格的版本存储（发布、回收、启动清理、固定版本，
data/table_versions.store_lock）、填报记录表的追加（records/store）。

用法同 asyncio.Lock：`async with lock:`。另留 acquire() / release() / _waiters / locked()，只碰本进程那一把（测试用来在
本进程里卡住锁）。不可重入。
"""
from __future__ import annotations

import asyncio
import hashlib
import weakref
from typing import Any

from app.core.config import settings


def advisory_key(name: str) -> int:
    """锁名 → 咨询锁的号（有符号 64 位）。加前缀，不和别处直接写的号（db/base 的建表锁、断点库的建表锁）撞。"""
    digest = hashlib.blake2b(f"agentlab:{name}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class ClusterLock:
    def __init__(self, name: str) -> None:
        self.name = name
        self.key = advisory_key(name)
        self._local = asyncio.Lock()
        self._conn: Any = None

    # 只碰本进程那一把（同 asyncio.Lock）
    async def acquire(self) -> bool:
        return await self._local.acquire()

    def release(self) -> None:
        self._local.release()

    def locked(self) -> bool:
        return self._local.locked()

    @property
    def _waiters(self) -> Any:
        return self._local._waiters

    async def try_take(self) -> bool:
        """不等：本进程、别的进程都没人占着就占上，交回占没占到。占到了用 give_back 放（可以在另一个任务里放：后台任务做完才放）。"""
        if self._local.locked():
            return False
        await self._local.acquire()
        if settings.db_dialect != "postgresql":
            return True
        try:
            from sqlalchemy import text

            from app.db.base import session_engine

            conn = await session_engine().connect()
            try:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}))
            except BaseException:
                await conn.close()
                raise
            if not got:
                await conn.close()
                self._local.release()
                return False
            self._conn = conn
        except BaseException:
            if self._local.locked() and self._conn is None:
                self._local.release()
            raise
        return True

    async def give_back(self) -> None:
        """放掉 try_take 占到的。"""
        await self.__aexit__(None, None, None)

    async def __aenter__(self) -> ClusterLock:
        await self._local.acquire()
        if settings.db_dialect != "postgresql":
            return self
        try:
            from sqlalchemy import text

            from app.db.base import session_engine

            # 会话级的锁：走直连（AGENTLAB_DATABASE_DIRECT_URL，没设就是应用库）——PgBouncer 事务模式下取锁、放锁可能落在
            # 两条不同的服务端连接上
            conn = await session_engine().connect()
            try:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": self.key})
            except BaseException:
                await conn.close()
                raise
            self._conn = conn
        except BaseException:
            self._local.release()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        conn, self._conn = self._conn, None
        try:
            if conn is not None:
                from sqlalchemy import text

                try:
                    await asyncio.shield(conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key}))
                finally:
                    # 放不掉（连接断了）也要关：会话一结束，库那边的咨询锁随之释放
                    await asyncio.shield(conn.close())
        finally:
            self._local.release()


class NamedLocks:
    """按事件循环各一组的命名锁（测试每个用例一个循环：asyncio.Lock 不能换循环用）。"""

    def __init__(self) -> None:
        self._by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, ClusterLock]] = \
            weakref.WeakKeyDictionary()

    def get(self, name: str) -> ClusterLock:
        loop = asyncio.get_running_loop()
        group = self._by_loop.get(loop)
        if group is None:
            group = self._by_loop[loop] = {}
        lock = group.get(name)
        if lock is None:
            lock = group[name] = ClusterLock(name)
        return lock


locks = NamedLocks()


def lock(name: str) -> ClusterLock:
    """名为 name 的跨进程锁（本事件循环里同一个名字是同一把）。"""
    return locks.get(name)


__all__ = ["ClusterLock", "NamedLocks", "advisory_key", "lock", "locks"]
