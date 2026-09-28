"""MCP 服务改了、删了、新加了，运行时拿到的工具要跟着变。

mcp_manager 按服务名缓存工具（每次都拉起一个 stdio 子进程太贵），而以前缓存只在
「一个都没加载过」时才建：改了启动命令，运行照样用旧命令建出来的工具；删掉的服务
照样能被节点调到；缓存建好之后新加的服务，工具列表里永远没有它，除非有人去点刷新。

这里用一个假的 MCP 客户端：它建出来的工具在 description 里写着自己是按哪个命令
（或地址）连的，于是一眼看得出拿到的是新配置还是旧配置。
"""
from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.tools import StructuredTool

from app.main import app


class _FakeClient:
    """一次连接 = 一次 get_tools。connects 记下每次连的是哪个服务、按什么配置。"""

    connects: list[tuple[str, str]] = []

    def __init__(self, servers: dict[str, dict[str, Any]]) -> None:
        self.servers = servers

    async def get_tools(self) -> list[StructuredTool]:
        (name, cfg), = self.servers.items()
        via = cfg.get("command") or cfg.get("url") or ""
        _FakeClient.connects.append((name, via))

        async def _echo(text: str = "") -> str:
            return f"{via}:{text}"

        return [StructuredTool.from_function(coroutine=_echo, name="echo",
                                             description=f"via {via}")]


@pytest.fixture
def manager(monkeypatch):
    """每个测试一个干净的 McpManager，接口层和运行时拿到的是同一个。"""
    import langchain_mcp_adapters.client as mcp_client

    import app.api.tools as tools_api
    import app.tools.mcp_manager as mcp_module

    _FakeClient.connects = []
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", _FakeClient)
    fresh = mcp_module.McpManager()
    monkeypatch.setattr(mcp_module, "mcp_manager", fresh)
    monkeypatch.setattr(tools_api, "mcp_manager", fresh)
    return fresh


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def server(client, manager):
    """一个叫 files 的 stdio 服务，测完删掉（测试里已经删了的就不用再删）。"""
    r = await client.post("/api/mcp/servers", json={
        "name": "files", "transport": "stdio", "command": "fs-v1"})
    assert r.status_code == 201, r.text
    row = r.json()
    yield row
    await client.delete(f"/api/mcp/servers/{row['id']}")


def _body(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    keys = ("name", "transport", "command", "args", "env", "url", "enabled")
    return {**{k: row[k] for k in keys}, **changes}


async def _via(manager, ref: str) -> list[str]:
    """运行时按引用取到的工具，各自是按什么配置连的。"""
    return [t.description for t in await manager.get_tools([ref])]


async def test_changing_the_command_drops_the_tools_built_from_the_old_one(client, manager, server):
    assert await _via(manager, "mcp:files/echo") == ["via fs-v1"]

    r = await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server, command="fs-v2"))
    assert r.status_code == 200, r.text
    assert "files" not in manager._tools, "保存之后旧工具就该丢掉，而不是等下次有人用"

    assert await _via(manager, "mcp:files/echo") == ["via fs-v2"], "改了启动命令，运行还在用旧命令建的工具"
    listed = [t for t in await manager.list_tools() if t["server"] == "files"]
    assert [t["description"] for t in listed] == ["via fs-v2"]


async def test_changing_the_address_of_an_http_server_reconnects(client, manager):
    r = await client.post("/api/mcp/servers", json={
        "name": "remote", "transport": "http", "url": "https://mcp-a.example/mcp"})
    row = r.json()
    try:
        assert await _via(manager, "mcp:remote/*") == ["via https://mcp-a.example/mcp"]
        await client.patch(f"/api/mcp/servers/{row['id']}",
                           json=_body(row, url="https://mcp-b.example/mcp"))
        assert await _via(manager, "mcp:remote/*") == ["via https://mcp-b.example/mcp"]
    finally:
        await client.delete(f"/api/mcp/servers/{row['id']}")


async def test_a_deleted_server_is_gone_from_runs_and_from_the_list(client, manager, server):
    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]

    r = await client.delete(f"/api/mcp/servers/{server['id']}")
    assert r.status_code == 204
    assert "files" not in manager._tools

    assert await manager.get_tools(["mcp:files/*"]) == [], "删掉的服务还能被节点调到"
    assert not [t for t in await manager.list_tools() if t["server"] == "files"]
    tools = (await client.get("/api/tools")).json()
    assert not [t for t in tools if t["id"].startswith("mcp:files/")]


async def test_a_disabled_server_is_not_handed_out(client, manager, server):
    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]
    await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server, enabled=False))
    assert await manager.get_tools(["mcp:files/*"]) == []

    # 再打开：下次用时照常连上
    await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server, enabled=True))
    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]


async def test_renaming_moves_the_tools_to_the_new_name(client, manager, server):
    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]
    await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server, name="documents"))
    assert await manager.get_tools(["mcp:files/*"]) == []
    assert await _via(manager, "mcp:documents/echo") == ["via fs-v1"]


async def test_a_server_added_after_the_cache_is_warm_shows_up(client, manager, server):
    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]
    r = await client.post("/api/mcp/servers", json={
        "name": "later", "transport": "stdio", "command": "later-v1"})
    row = r.json()
    try:
        servers = {t["server"] for t in await manager.list_tools()}
        assert {"files", "later"} <= servers, "缓存建好之后新加的服务，工具列表里没有它"
        assert await _via(manager, "mcp:later/echo") == ["via later-v1"]
    finally:
        await client.delete(f"/api/mcp/servers/{row['id']}")


async def test_unchanged_servers_are_not_reconnected_on_every_use(client, manager, server):
    for _ in range(3):
        await manager.get_tools(["mcp:files/*"])
        await manager.list_tools()
    assert _FakeClient.connects.count(("files", "fs-v1")) == 1

    # 只改名字以外无关连接的保存（原样再存一次）也不该重连
    await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server))
    await manager.get_tools(["mcp:files/*"])
    assert _FakeClient.connects.count(("files", "fs-v1")) == 1


async def test_a_change_made_behind_the_api_is_still_noticed(client, manager, server):
    """缓存认的是连接配置，不只是接口层的通知：库里的配置变了，下次用时就重连。"""
    from app.db.base import SessionLocal
    from app.db.models import McpServer

    assert await _via(manager, "mcp:files/*") == ["via fs-v1"]
    async with SessionLocal() as session:
        row = await session.get(McpServer, server["id"])
        row.command = "fs-v3"
        await session.commit()
    assert await _via(manager, "mcp:files/*") == ["via fs-v3"]


async def test_a_load_that_raced_with_an_edit_is_not_cached(client, manager, server, monkeypatch):
    """连接要花时间（stdio 子进程、最长 30 秒）。连到一半服务被改了，这次连的是旧配置，
    结果不能进缓存，也不能把旧配置的「连得上」写回库里。"""
    import asyncio

    gate = asyncio.Event()
    started = asyncio.Event()
    original = _FakeClient.get_tools

    async def _slow(self):
        started.set()
        await gate.wait()
        return await original(self)

    monkeypatch.setattr(_FakeClient, "get_tools", _slow)
    loading = asyncio.create_task(manager.get_tools(["mcp:files/*"]))
    await started.wait()
    await client.patch(f"/api/mcp/servers/{server['id']}", json=_body(server, command="fs-v2"))
    gate.set()
    await loading   # 这一次拿到什么都行：它开始时配置还是旧的

    async def _row() -> dict[str, Any]:
        return next(s for s in (await client.get("/api/mcp/servers")).json() if s["id"] == server["id"])

    stale = await _row()
    assert stale["status"] == "unknown" and stale["last_check_ok"] is None, \
        f"旧配置那次连接的结论写回了库里：{stale}"

    monkeypatch.setattr(_FakeClient, "get_tools", original)
    assert await _via(manager, "mcp:files/*") == ["via fs-v2"]
    fresh = await _row()
    assert fresh["status"] == "ok" and fresh["last_check_ok"] is True


async def test_a_missing_command_is_named_in_the_error(client, manager, monkeypatch):
    """stdio 客户端抛的 FileNotFoundError 常常不带文件名，报错里得补上配置的那条命令，
    不能只剩一句「找不到命令 」。"""
    async def _missing(self):
        raise FileNotFoundError()

    monkeypatch.setattr(_FakeClient, "get_tools", _missing)
    r = await client.post("/api/mcp/servers", json={
        "name": "ghost", "transport": "stdio", "command": "ghost-mcp-server", "args": ["--stdio"]})
    row = r.json()
    try:
        assert await manager.get_tools(["mcp:ghost/*"]) == []
        listed = next(s for s in (await client.get("/api/mcp/servers")).json() if s["id"] == row["id"])
        assert listed["status"] == "error"
        assert "ghost-mcp-server" in listed["last_error"], listed["last_error"]
        probe = (await client.post(f"/api/mcp/servers/{row['id']}/probe")).json()
        assert probe["ok"] is False and "ghost-mcp-server" in probe["error"], probe
    finally:
        await client.delete(f"/api/mcp/servers/{row['id']}")
