"""应用库是 PostgreSQL 时测试用的连接池：每个事件循环一个池子，连接只给建它的那个循环用。

pytest-asyncio 每个测试一个事件循环，TestClient 在自己的线程里另起一个；asyncpg 的连接绑在建它的循环上，换个循环用
就报「attached to a different loop」。不用连接池（每次查库新建连接）能跑，但一次运行几十次查库，每次多几毫秒，
合起来让计时类的测试（审批收尾不计时长、反查的时限）在满载时越界——生产上有连接池，没有这笔开销。

这里：连接按建它的循环归池子，取的时候只从当前循环的池子里拿，还回来放回它自己的池子；循环关掉之前（测试结束、
TestClient 退出）把它池子里的连接在它自己的循环里关掉。只在 conftest 里、AGENTLAB_TEST_DATABASE_URL 时装上，
生产代码不用它。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from typing import Any

from sqlalchemy.pool import Pool

_LOOP = "agentlab_loop"


class LoopLocalPool(Pool):
    _is_asyncio = True

    def __init__(self, creator: Any, **kw: Any) -> None:
        super().__init__(creator, **kw)
        self._idle: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, list[Any]] = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    @classmethod
    def replacing(cls, pool: Pool) -> LoopLocalPool:
        """换掉引擎原来的池子：同一个建连接的函数、同一套连接事件（方言的 on_connect 挂在上面）。"""
        return cls(pool._creator, recycle=pool._recycle, echo=pool.echo, logging_name=pool._orig_logging_name,
                   reset_on_return=pool._reset_on_return, pre_ping=False, _dispatch=pool.dispatch,
                   dialect=pool._dialect)

    def recreate(self) -> LoopLocalPool:
        return LoopLocalPool.replacing(self)

    def _bucket(self, loop: asyncio.AbstractEventLoop) -> list[Any]:
        with self._lock:
            return self._idle.setdefault(loop, [])

    def _do_get(self) -> Any:
        bucket = self._bucket(asyncio.get_running_loop())
        with self._lock:
            if bucket:
                return bucket.pop()
        record = self._create_connection()
        record.info[_LOOP] = weakref.ref(asyncio.get_running_loop())
        return record

    def _do_return_conn(self, record: Any) -> None:
        ref = record.info.get(_LOOP)
        loop = ref() if ref is not None else None
        if loop is None or loop.is_closed():
            with contextlib.suppress(Exception):
                record.close()
            return
        bucket = self._bucket(loop)
        with self._lock:
            bucket.append(record)

    def take(self, loop: asyncio.AbstractEventLoop) -> list[Any]:
        """这个循环池子里空闲的连接，取出来（交给调用方在这个循环里关掉）。"""
        with self._lock:
            return self._idle.pop(loop, [])

    def dispose(self) -> None:
        """只关当前循环的：别的循环的连接不能在这里关。没有正在跑的循环时什么都不做（各循环关掉时自己收拾）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for record in self.take(loop):
            with contextlib.suppress(Exception):
                record.close()

    def status(self) -> str:
        with self._lock:
            idle = sum(len(v) for v in self._idle.values())
        return f"LoopLocalPool id:{id(self)} idle:{idle} loops:{len(self._idle)}"


def install(engine: Any) -> LoopLocalPool:
    """把应用库引擎的池子换成 LoopLocalPool，并让每个事件循环关掉之前先关它自己的连接。"""
    from sqlalchemy.util import greenlet_spawn

    pool = LoopLocalPool.replacing(engine.sync_engine.pool)
    engine.sync_engine.pool = pool

    async def close_mine(records: list[Any]) -> None:
        def run() -> None:
            for record in records:
                with contextlib.suppress(Exception):
                    record.close()

        await greenlet_spawn(run)

    base = asyncio.get_event_loop_policy()

    class Policy(type(base)):  # type: ignore[misc]
        def new_event_loop(self) -> asyncio.AbstractEventLoop:
            loop = super().new_event_loop()
            close = loop.close

            def closing() -> None:
                current = engine.sync_engine.pool
                if isinstance(current, LoopLocalPool) and not loop.is_closed() and not loop.is_running():
                    records = current.take(loop)
                    if records:
                        with contextlib.suppress(Exception):
                            loop.run_until_complete(close_mine(records))
                close()

            loop.close = closing  # type: ignore[method-assign]
            return loop

    asyncio.set_event_loop_policy(Policy())
    return pool
