"""换个 schema 看看，不擅自改缓存；表结构给结构化的列；卡片知道缓存是按哪个 schema 探的。

「换个 schema 看看」以前一探就把缓存换成那个 schema 的结构——哪怕用户随后选了
「只看看，不改」，助手此刻看到的已经是另一套表了。前端只好再探一次配置里的
schema 把缓存换回来，而那一次要是失败，缓存和配置就对不上，只剩一条会消失的 toast。

表结构以前只有一段给模型看的文本，前端照着文本格式去解析列，格式一改就全乱。
"""
from __future__ import annotations

import re

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fixtures.sources import introspect_now


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(client, tmp_path):
    path = tmp_path / "preview.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, store TEXT NOT NULL, amount REAL)")
    db.execute("CREATE TABLE sales_daily (day TEXT, total REAL)")
    db.execute("CREATE VIEW v_big_orders AS SELECT * FROM orders WHERE amount > 100")
    db.commit()
    db.close()
    r = await client.post("/api/datasources", json={"name": "preview_src", "kind": "sqlite",
                                                   "database": str(path)})
    assert r.status_code == 201, r.text
    yield r.json()
    await client.delete(f"/api/datasources/{r.json()['id']}")


async def _cache(source_id: str) -> dict:
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        return {"cache": dict(row.schema_cache or {}), "synced": row.schema_synced_at}


# --------------------------------------------------------------------------
# REQ-7：缓存是按哪个 schema 探的
# --------------------------------------------------------------------------


async def test_cached_schema_says_which_schema_the_cache_came_from(client, source):
    assert source["cached_schema"] is None, "没探过就是没有缓存"
    out = await introspect_now(client, source["id"])
    assert out["cached_schema"] == "", "按默认 schema 探的写空串，和「没探过」分开"
    # 一个数据源一个 schema（目录-D2）：真探查只按配置里的 schema，要换先改配置
    r = await client.patch(f"/api/datasources/{source['id']}", json={"options": {"schema": "main"}})
    assert r.status_code == 200, r.text
    out = await introspect_now(client, source["id"])
    assert out["cached_schema"] == "main"
    listed = next(s for s in (await client.get("/api/datasources")).json() if s["id"] == source["id"])
    assert listed["cached_schema"] == "main"


# --------------------------------------------------------------------------
# REQ-6 ①：只看看，不改缓存
# --------------------------------------------------------------------------


async def test_a_dry_run_shows_what_is_there_without_touching_the_cache(client, source):
    await introspect_now(client, source["id"])
    before = await _cache(source["id"])

    r = await client.post(f"/api/datasources/{source['id']}/introspect",
                          params={"schema": "main", "dry_run": "true"})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["dry_run"] is True
    assert preview["schema"] == "main"
    assert preview["table_count"] == 3
    assert set(preview["tables"]) == {"main.orders", "main.sales_daily", "main.v_big_orders"}
    assert preview["schema_error"] == ""

    after = await _cache(source["id"])
    assert after == before, "只看看也把缓存换掉了——助手此刻看到的就不是配置里那个 schema 了"


async def test_a_dry_run_that_finds_nothing_says_why_and_still_leaves_the_cache(client, source):
    await introspect_now(client, source["id"])
    before = await _cache(source["id"])

    r = await client.post(f"/api/datasources/{source['id']}/introspect",
                          params={"schema": "no_such_schema", "dry_run": "true"})
    preview = r.json()
    assert preview["dry_run"] is True and preview["table_count"] == 0
    assert preview["schema_error"], "探失败了要说出来，不能看起来像「这个 schema 是空的」"
    # 这句话原样画在数据源卡片上：不带驱动的异常类名（OperationalError: …）
    assert not re.search(r"\b[A-Z]\w*(Error|Exception)\b", preview["schema_error"]), preview["schema_error"]
    assert "main" in preview["available_schemas"]
    assert await _cache(source["id"]) == before


async def test_without_dry_run_the_cache_is_still_replaced(client, source):
    r = await client.patch(f"/api/datasources/{source['id']}", json={"options": {"schema": "main"}})
    assert r.status_code == 200, r.text
    out = await introspect_now(client, source["id"])
    assert out["table_count"] == 3
    assert (await _cache(source["id"]))["cache"]["schema"] == "main"


async def test_a_real_introspect_with_another_schema_is_refused(client, source):
    """真探查不接受临时换 schema：一个数据源一个 schema，对象索引、探查范围都按配置里的那个记。"""
    r = await client.post(f"/api/datasources/{source['id']}/introspect", params={"schema": "other"})
    assert r.status_code == 422 and "schema" in r.json()["detail"]


# --------------------------------------------------------------------------
# REQ-6 ②：表结构的结构化列
# --------------------------------------------------------------------------


async def test_table_schema_comes_with_structured_columns(client, source):
    await introspect_now(client, source["id"])
    body = (await client.get(f"/api/datasources/{source['id']}/schema",
                             params={"table": "orders"})).json()
    assert body["found"] is True
    assert body["kind"] == "table" and body["qualified"] == "orders"
    assert body["detail"].startswith("表 orders"), "给模型看的那段文本照旧"
    assert body["columns"] == [
        {"name": "id", "type": "INTEGER", "pk": True, "not_null": False, "comment": None},
        {"name": "store", "type": "TEXT", "pk": False, "not_null": True, "comment": None},
        {"name": "amount", "type": "REAL", "pk": False, "not_null": False, "comment": None},
    ]

    view = (await client.get(f"/api/datasources/{source['id']}/schema",
                             params={"table": "V_BIG_ORDERS"})).json()
    assert view["found"] is True and view["kind"] == "view", "和给模型的那份一样，大小写不敏感"


async def test_an_unknown_table_has_no_columns_but_keeps_the_explanation(client, source):
    await introspect_now(client, source["id"])
    body = (await client.get(f"/api/datasources/{source['id']}/schema",
                             params={"table": "nope"})).json()
    assert body["found"] is False and body["columns"] == []
    assert "没有 nope" in body["detail"]
