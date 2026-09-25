"""aiosqlite 连接的关闭不能把调用方永远挂住。

aiosqlite 0.22 的 stop() 每调一次都往工作线程队列里放一个"关闭并退出线程"
的请求：线程处理完第一个就退出，之后的请求没人处理，等它的一方永远挂着。
close() 的 finally 会调 stop()，SQLAlchemy 的强制 terminate 也会直接调——
不打补丁时同一连接并发 close 两次，10 次卡 10 次。

它在这个项目里的真实后果是运行卡死：并行支路一条失败，LangGraph 取消另一条，
那一条正在写工件，SQLAlchemy 就在两条路径上各 terminate 一次同一条连接——
节点任务取消不完，运行永远停在 running（test_approval 里并行驳回那例约五次
卡一次，就是这么被发现的）。补丁在 app.db.base。
"""
from __future__ import annotations

import asyncio

import aiosqlite

import app.db.base  # noqa: F401  补丁在这里打上


async def test_concurrent_close_of_one_connection_finishes(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "x.db")
    await conn.execute("CREATE TABLE t (x INTEGER)")
    await asyncio.wait_for(asyncio.gather(conn.close(), conn.close(), conn.close()), timeout=5)


async def test_close_racing_a_forced_stop_finishes(tmp_path):
    """SQLAlchemy 的优雅关闭被取消时会退回强制 terminate，直接调 stop()。"""
    conn = await aiosqlite.connect(tmp_path / "w.db")
    closing = asyncio.ensure_future(conn.close())
    await asyncio.sleep(0)
    conn.stop()
    await asyncio.wait_for(closing, timeout=5)


async def test_close_after_a_cancelled_close_finishes(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "y.db")
    first = asyncio.ensure_future(conn.close())
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.wait_for(conn.close(), timeout=5)


async def test_closing_an_already_closed_connection_is_a_no_op(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "z.db")
    await conn.close()
    await asyncio.wait_for(conn.close(), timeout=5)
