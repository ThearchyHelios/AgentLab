"""发起运行前检查绑定的工具：本机不存在的，发起就拒绝（422），不要等跑到那一步才失败。

用户手上真实踩到的：图里的 agent 绑着一个已经删掉的数据源工具，运行跑了几分钟、花了几轮
模型调用才在半路报错。查的方式和运行时 build_tools 的解析一致：内置、mcp:、
db_query__ / db_schema__、自定义。MCP 连不上时不拦，只在日志里提示。
"""
from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import CustomTool, DataSource, McpServer, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def catalog(tmp_path):
    """本机有：数据源 shop（启用）、archive（停用）；自定义工具 lookup；MCP 服务 files（连得上）、
    flaky（上次没连上）、off（停用）。别的测试也会建同名的行，这里按名字补齐状态，不删别人的。"""
    rows = [
        (DataSource, "shop", {"kind": "sqlite", "database": str(tmp_path / "shop.db"), "readonly": True,
                              "enabled": True}),
        (DataSource, "archive", {"kind": "sqlite", "database": str(tmp_path / "a.db"), "readonly": True,
                                 "enabled": False}),
        (CustomTool, "lookup", {"kind": "http", "description": "查编码", "parameters": {}, "enabled": True}),
        (McpServer, "files", {"transport": "stdio", "command": "true", "enabled": True, "status": "ok",
                              "tools_cache": ["read_file"]}),
        (McpServer, "flaky", {"transport": "stdio", "command": "true", "enabled": True, "status": "error"}),
        (McpServer, "off", {"transport": "stdio", "command": "true", "enabled": False}),
    ]
    async with SessionLocal() as session:
        for model, name, fields in rows:
            row = (await session.execute(select(model).where(model.name == name))).scalar_one_or_none()
            if row is None:
                row = model(name=name)
                session.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
        for model, name in ((DataSource, "nope"), (CustomTool, "ghost_tool"), (McpServer, "gone")):
            await session.execute(delete(model).where(model.name == name))
        await session.commit()
    yield


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def graph(*middle):
    nodes = [node("start", "input"), *middle, node("out", "output", fields=[{"name": "r", "value": "{{ last_message }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def start(client, g):
    return await client.post("/api/runs", json={"graph": g})


async def test_a_missing_datasource_tool_is_refused_and_named(client):
    res = await start(client, graph(node("fetch", "tool", "取数", tool="db_query__nope", args={"sql": "SELECT 1"})))
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "run_tool_missing"
    assert "「取数」" in body["detail"] and "db_query__nope" in body["detail"]
    assert "去数据页接入，或在节点里重新选" in body["detail"]


async def test_every_missing_binding_is_listed(client):
    res = await start(client, graph(
        node("bot", "agent", "分析", prompt="查", approval="never",
             tools=["web_search", "db_schema__archive", "db_query__shop", "ghost_tool", "mcp:off/x"]),
        node("team", "supervisor", "协作", goal="g", agents=[
            {"name": "研究员", "tools": ["mcp:gone/read"]}, {"name": "写手", "tools": ["lookup"]}]),
    ))
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    # 停用的数据源、库里没有的自定义工具、停用的 MCP 服务、没登记的 MCP 服务都算不存在
    for name in ("db_schema__archive", "ghost_tool", "mcp:off/x", "mcp:gone/read"):
        assert name in detail, (name, detail)
    assert "「分析」" in detail and "「协作」的成员「研究员」" in detail
    # 在的不点名
    for name in ("web_search", "db_query__shop", "lookup"):
        assert name not in detail, (name, detail)


async def test_existing_tools_pass(client):
    res = await start(client, graph(
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": "SELECT 1"}),
        node("bot", "agent", "分析", prompt="查", approval="never",
             tools=["web_search", "db_schema__shop", "lookup", "mcp:files/read_file", "mcp:files/*"]),
    ))
    assert res.status_code == 201, res.text


async def test_an_unreachable_mcp_server_is_not_blocked(client, caplog):
    """服务登记着、只是上次没连上：可能只是暂时的，发起时不拦，日志里提一句。"""
    with caplog.at_level(logging.WARNING, logger="app.api.runs"):
        res = await start(client, graph(node("bot", "agent", "分析", prompt="查", approval="never",
                                             tools=["mcp:flaky/search", "mcp:files/not_listed"])))
    assert res.status_code == 201, res.text
    assert "flaky" in caplog.text and "not_listed" in caplog.text


async def test_formal_runs_are_checked_too(client):
    g = graph(node("fetch", "tool", "取数", tool="db_query__nope", args={"sql": "SELECT 1"}))
    async with SessionLocal() as session:
        wf = Workflow(name="周报", graph=g, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=g))
        await session.commit()
        wf_id = wf.id
    res = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert res.status_code == 422 and "db_query__nope" in res.json()["detail"]
    # 保存过的工作流发起探索运行也查
    res = await client.post("/api/runs", json={"workflow_id": wf_id})
    assert res.status_code == 422


async def test_graphs_without_tools_are_untouched(client):
    res = await start(client, graph(node("fmt", "transform", mode="expression", expression="1")))
    assert res.status_code == 201, res.text
