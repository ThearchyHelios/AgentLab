"""只读数据源在连接层也只读。

守卫按关键字判定，漏判过一次：`WITH k AS (SELECT 1) DELETE FROM t` 首关键字是
WITH，被当成查询放行；而 pysqlite 只在 INSERT/UPDATE/DELETE 开头的语句前隐式开
事务，WITH 开头的直接自动提交——连接关闭时的回滚兜不住，数据真的删了，调用方
只看到一个驱动报错。Excel/CSV 上传生成的正是这种 SQLite 源。

所以只读不能只靠猜。这组测试绕过守卫，直接在连接上写，看它拒不拒。
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.data.engine import engine_args, engines, run_query
from app.data.guard import SqlRejected


def _db(path) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()
    return str(path)


def _source(path: str = "", *, readonly: bool = True, kind: str = "sqlite"):
    return SimpleNamespace(
        id=f"ro-{uuid.uuid4().hex[:8]}", kind=kind, database=path, readonly=readonly,
        name="测试源", options={}, password=None, username="u", host="h", port=None,
    )


def _state(path: str) -> tuple[int, int]:
    conn = sqlite3.connect(path)
    try:
        return (conn.execute("SELECT COUNT(*) FROM t").fetchone()[0],
                conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
async def ro_source(tmp_path):
    src = _source(_db(tmp_path / "shop.db"))
    yield src
    await engines.invalidate(src.id)


async def test_the_original_bypass_is_closed(ro_source):
    """当初那条：守卫放行、驱动报错、数据已经没了。现在守卫就拦下，数据原样。"""
    with pytest.raises(SqlRejected, match="只读"):
        await run_query(ro_source, "WITH k AS (SELECT 1) DELETE FROM t WHERE x < 3")
    assert _state(ro_source.database) == (5, 0)


async def test_readonly_sqlite_refuses_writes_even_past_the_guard(ro_source):
    """守卫再漏一次也没用：连接是以 mode=ro 打开的，写由 SQLite 自己拒绝。"""
    engine = await engines.get(ro_source)
    async with engine.connect() as conn:
        for sql in ("DELETE FROM t",
                    "WITH k AS (SELECT 1) DELETE FROM t",
                    "PRAGMA user_version = 7"):
            with pytest.raises(OperationalError, match="readonly"):
                await conn.execute(text(sql))
    assert _state(ro_source.database) == (5, 0)


async def test_readonly_sqlite_still_answers_queries(ro_source):
    result = await run_query(ro_source, "SELECT COUNT(*) AS n FROM t")
    assert result.rows == [[5]]
    info = await run_query(ro_source, "PRAGMA table_info(t)")
    assert info.row_count == 1


async def test_readonly_path_with_spaces_and_cjk_still_opens(tmp_path):
    """连接串走 file: URI，路径得转义——带空格、中文、# 的文件名不能打不开。"""
    src = _source(_db(tmp_path / "销售 数据#1.db"))
    try:
        assert (await run_query(src, "SELECT COUNT(*) FROM t")).rows == [[5]]
    finally:
        await engines.invalidate(src.id)


async def test_writable_sqlite_is_not_opened_readonly(tmp_path):
    """只读收紧不能波及可写源。"""
    src = _source(_db(tmp_path / "rw.db"), readonly=False)
    engine = await engines.get(src)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM t WHERE x = 0"))
    finally:
        await engines.invalidate(src.id)
    assert _state(src.database) == (4, 0)


def test_server_databases_get_session_level_readonly():
    """PG / MySQL 的只读落在会话上（真库验证见提交说明）。

    Oracle 没有会话级只读开关，只剩守卫——这里把这一点钉住，免得有人以为它也有。
    """
    _, pg = engine_args(_source("db", kind="postgresql"))
    assert pg["connect_args"]["server_settings"]["default_transaction_read_only"] == "on"
    _, my = engine_args(_source("db", kind="mysql"))
    assert my["connect_args"]["init_command"] == "SET SESSION TRANSACTION READ ONLY"
    _, ora = engine_args(_source("db", kind="oracle"))
    assert ora == {}
    _, rw = engine_args(_source("db", kind="postgresql", readonly=False))
    assert rw == {}
