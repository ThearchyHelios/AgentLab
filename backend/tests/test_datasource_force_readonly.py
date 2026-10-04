"""数据源查询工具的 force_readonly：核查（双盲复算、反方审稿、审批质询）只许读数据。

核查的 agent 是另起的一路，没有审批关卡兜底；它要查的数据源却可能是可写的。所以建工具时传 force_readonly=True：
不管源本身可不可写，一律按只读守卫检查——可写源上的写语句也拒绝，原因交回模型（它据此改写），数据原样不动；
查询照常。工具描述跟着写「只读」，审批判定也不再把写语句当成要人批的危险调用（反正执行不了）。
不传的时候一切照旧：可写源上的写语句仍是要审批的危险调用。

夹具是一个现造的可写 SQLite 文件，表和数字都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.tools.datasource import build_datasource_tools
from app.tools.registry import ToolContext, Versions


def rows_in(path: str) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT id, amount FROM orders ORDER BY id").fetchall()
    finally:
        db.close()


@pytest.fixture
async def writable(tmp_path):
    """一个可写的手工源：orders 两行，金额 120 和 80。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER);"
                     "INSERT INTO orders VALUES (1, 120), (2, 80);")
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=False, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name, str(path)
    await engines.invalidate(source_id)


async def tools(name: str, **kwargs) -> dict:
    async with SessionLocal() as session:
        built = await build_datasource_tools([f"db_query__{name}", f"db_schema__{name}"],
                                             ToolContext(data_versions=Versions.CURRENT), session, **kwargs)
    return {t.name: t for t in built}


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET amount = 0",
    "INSERT INTO orders VALUES (3, 1)",
    "WITH k AS (SELECT 1) DELETE FROM orders WHERE id = 1",
])
async def test_writes_are_refused_on_a_writable_source(writable, sql):
    name, path = writable
    query = (await tools(name, force_readonly=True))[f"db_query__{name}"]
    got = await query.ainvoke({"sql": sql})
    assert got.startswith("SQL 被拒绝：") and "只允许读取数据" in got, got
    assert rows_in(path) == [(1, 120), (2, 80)], "数据原样不动"


async def test_structural_changes_are_refused_with_the_usual_reason(writable):
    name, path = writable
    query = (await tools(name, force_readonly=True))[f"db_query__{name}"]
    got = await query.ainvoke({"sql": "DROP TABLE orders"})
    assert got.startswith("SQL 被拒绝：") and "结构变更" in got
    assert rows_in(path) == [(1, 120), (2, 80)]


async def test_queries_still_work_and_land_as_snapshots(writable):
    name, _ = writable
    built = await tools(name, force_readonly=True)
    got = json.loads(await built[f"db_query__{name}"].ainvoke({"sql": "SELECT SUM(amount) AS total FROM orders"}))
    assert got["rows"] == [[200]] and got["artifact"], "查询照常，结果照常落成查询快照"
    assert "orders" in await built[f"db_schema__{name}"].ainvoke({})


async def test_the_tool_says_read_only_and_needs_no_approval(writable):
    name, _ = writable
    forced = (await tools(name, force_readonly=True))[f"db_query__{name}"]
    assert "只读。" in forced.description and "可写" not in forced.description
    assert forced.metadata["dangerous_if"]({"sql": "DELETE FROM orders"}) is False, "写语句反正执行不了，不用找人批"


async def test_without_the_flag_nothing_changes(writable):
    name, path = writable
    normal = (await tools(name))[f"db_query__{name}"]
    assert "可写（写操作需要人工审批）" in normal.description
    assert normal.metadata["dangerous_if"]({"sql": "DELETE FROM orders"}) is True
    got = json.loads(await normal.ainvoke({"sql": "UPDATE orders SET amount = 90 WHERE id = 2"}))
    assert got["rows"] == [[1]], "不开 force_readonly 时可写源照常能写（审批由节点把关）"
    assert rows_in(path) == [(1, 120), (2, 90)]
