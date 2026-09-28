"""最近一次测连接的结果记在对象上，换台浏览器也看得到。

以前这份结果只存在当前浏览器的 localStorage 里：换台电脑、清个缓存，所有数据源、
模型接入、MCP 服务都回到「未测试」，而它们上周是不是连得通，谁也说不清。

记在对象上还有两条规矩：
- 在表单里测过、紧接着保存的，保存下来的对象直接带上那次结果——后端自己核对
  过测的就是这份配置，不用前端转述。
- 连接配置改过了，旧结果就不再代表它，清掉；只改说明之类的不清。
"""
from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Provider
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def orders_db(tmp_path):
    path = tmp_path / "orders.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
    db.commit()
    db.close()
    return str(path)


def _health(item: dict) -> tuple:
    return (item["last_check_ok"], item["last_error"],
            item["last_checked_at"] is not None, item["last_latency_ms"] is not None)


async def _source(client, name: str) -> dict:
    return next(s for s in (await client.get("/api/datasources")).json() if s["name"] == name)


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------


async def test_testing_a_saved_source_is_remembered_on_it(client, orders_db):
    r = await client.post("/api/datasources", json={"name": "health_src", "kind": "sqlite",
                                                   "database": orders_db})
    src = r.json()
    try:
        assert _health(src) == (None, None, False, False), "没测过就是没测过"
        body = (await client.post(f"/api/datasources/{src['id']}/test")).json()
        assert body["ok"] is True
        assert _health(await _source(client, "health_src")) == (True, None, True, True)

        # 连接配置变了：旧结果不再代表它
        r = await client.patch(f"/api/datasources/{src['id']}", json={"database": orders_db + ".gone"})
        assert _health(r.json()) == (None, None, False, False)
        body = (await client.post(f"/api/datasources/{src['id']}/test")).json()
        assert body["ok"] is False
        item = await _source(client, "health_src")
        assert item["last_check_ok"] is False and item["last_error"] == body["error"]
        assert item["last_checked_at"]

        # 只改说明：连接没变，结果留着
        r = await client.patch(f"/api/datasources/{src['id']}", json={"description": "订单库"})
        assert r.json()["last_check_ok"] is False
    finally:
        await client.delete(f"/api/datasources/{src['id']}")


async def test_a_draft_tested_then_saved_keeps_its_result(client, orders_db):
    body = (await client.post("/api/datasources/test", json={"kind": "sqlite",
                                                            "database": orders_db})).json()
    assert body["ok"] is True
    r = await client.post("/api/datasources", json={"name": "health_draft", "kind": "sqlite",
                                                   "database": orders_db})
    src = r.json()
    try:
        assert _health(src) == (True, None, True, True), "刚在表单里测通的，保存之后不该变回未测试"
        assert src["last_latency_ms"] == body["elapsed_ms"]
    finally:
        await client.delete(f"/api/datasources/{src['id']}")


async def test_a_draft_result_does_not_stick_to_a_different_config(client, orders_db, tmp_path):
    await client.post("/api/datasources/test", json={"kind": "sqlite", "database": orders_db})
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    r = await client.post("/api/datasources", json={"name": "health_other", "kind": "sqlite",
                                                   "database": str(other)})
    src = r.json()
    try:
        assert _health(src) == (None, None, False, False)
    finally:
        await client.delete(f"/api/datasources/{src['id']}")


async def test_a_draft_test_of_the_unchanged_saved_config_updates_it(client, orders_db):
    r = await client.post("/api/datasources", json={"name": "health_edit", "kind": "sqlite",
                                                   "database": orders_db})
    src = r.json()
    try:
        await client.post("/api/datasources/test", json={"id": src["id"], "kind": "sqlite",
                                                        "database": orders_db})
        assert (await _source(client, "health_edit"))["last_check_ok"] is True
        # 编辑框里改了路径再测：测的是另一份配置，不能记到已保存的那个头上
        await client.post("/api/datasources/test", json={"id": src["id"], "kind": "sqlite",
                                                        "database": orders_db + ".typo"})
        assert (await _source(client, "health_edit"))["last_check_ok"] is True
    finally:
        await client.delete(f"/api/datasources/{src['id']}")


# --------------------------------------------------------------------------
# 模型接入
# --------------------------------------------------------------------------


async def _provider(client, name: str) -> dict:
    return next(p for p in (await client.get("/api/providers")).json() if p["name"] == name)


async def test_testing_a_saved_provider_is_remembered_on_it(client):
    r = await client.post("/api/providers", json={"name": "health-mock", "kind": "mock",
                                                 "default_model": "mock-fast"})
    prov = r.json()
    try:
        assert _health(prov) == (None, None, False, False)
        assert (await client.post(f"/api/providers/{prov['id']}/test", json={})).json()["ok"] is True
        item = await _provider(client, "health-mock")
        assert _health(item) == (True, None, True, True)
        assert "last_check" not in item["extra"], "结果是后端记的，不该混进用户填的 extra 里回显"

        # 前端保存时会把 extra 原样带回来：不能因此把结果抹掉
        r = await client.patch(f"/api/providers/{prov['id']}", json={"extra": item["extra"],
                                                                    "enabled": True})
        assert r.json()["last_check_ok"] is True
        # 换了默认模型：测的那个不是它了
        r = await client.patch(f"/api/providers/{prov['id']}", json={"default_model": "mock-slow"})
        assert _health(r.json()) == (None, None, False, False)
    finally:
        await client.delete(f"/api/providers/{prov['id']}")


async def test_a_provider_draft_tested_then_saved_keeps_its_result(client):
    body = (await client.post("/api/providers/test", json={"kind": "mock",
                                                          "default_model": "mock-fast"})).json()
    assert body["ok"] is True
    r = await client.post("/api/providers", json={"name": "health-draft", "kind": "mock",
                                                 "default_model": "mock-fast"})
    prov = r.json()
    try:
        assert _health(prov) == (True, None, True, True)
    finally:
        await client.delete(f"/api/providers/{prov['id']}")


async def test_saving_a_provider_does_not_replace_its_headers_with_the_mask(client):
    """列表里请求头的值回显成 ***；设置页原样带回来保存，真值就被 *** 盖掉了。"""
    r = await client.post("/api/providers", json={
        "name": "health-headers", "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "k", "default_model": "m", "extra": {"headers": {"X-Token": "real-value"}},
    })
    prov = r.json()
    try:
        shown = (await _provider(client, "health-headers"))["extra"]
        assert shown["headers"] == {"X-Token": "***"}
        await client.patch(f"/api/providers/{prov['id']}", json={
            "extra": {**shown, "headers": {**shown["headers"], "X-Other": "new"}}})
        async with SessionLocal() as session:
            row = await session.get(Provider, prov["id"])
            assert row.extra["headers"] == {"X-Token": "real-value", "X-Other": "new"}
    finally:
        await client.delete(f"/api/providers/{prov['id']}")


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------


async def test_probing_an_mcp_server_is_remembered_with_when_and_how_long(client):
    r = await client.post("/api/mcp/servers", json={
        "name": "health-mcp", "transport": "stdio", "command": "agentlab-no-such-command-xyz"})
    server = r.json()
    try:
        assert _health(server) == (None, None, False, False)
        body = (await client.post(f"/api/mcp/servers/{server['id']}/probe")).json()
        assert body["ok"] is False
        item = next(s for s in (await client.get("/api/mcp/servers")).json() if s["id"] == server["id"])
        assert item["last_check_ok"] is False and item["last_error"] == body["error"]
        assert item["last_checked_at"] and item["last_latency_ms"] is not None

        # 改了启动命令：旧结论不再算数
        r = await client.patch(f"/api/mcp/servers/{server['id']}", json={
            "name": "health-mcp", "transport": "stdio", "command": "another-command"})
        assert _health(r.json()) == (None, None, False, False)
        assert r.json()["status"] == "unknown"
    finally:
        await client.delete(f"/api/mcp/servers/{server['id']}")
