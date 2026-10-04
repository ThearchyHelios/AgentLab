"""发布时记下目录版本，正式运行时提示目录的变化（阶段 4A）。

- 发布（把某一版立为正式版本）时，记下这个模板用到的表的目录版本，存在 workflow_versions.catalog_versions，
  分两组：{"direct": {源名: {表名: 版本}}, "possible": {源名: {表名: 版本}}}。
  - direct：调用工具节点写死的 SQL 用到的表；还没有目录的表记 0（之后补了目录也算变化）。
  - possible：Agent 节点、协作成员绑定了某个源的查询工具——SQL 运行时才写，发布时记下这个源里所有有目录的表
    （直接引用的不重复记）。之后新建了目录的表同样算变化。
- 从这一版发起正式运行时和当前的目录版本比：有变化发一条 catalog.drift 事件（列出表、前后版本、属于哪一组），
  **不拦运行**；目录没变不发。
- 版本详情给出发布时的目录版本，以及哪些表在那之后变了。
- 没有记录的老版本不提醒；只有直接引用那一种写法（{源名: {表名: 版本}}）的记录照旧按直接引用比，不提醒可能涉及。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.catalog import scenic_notes
from tests.fixtures.sources import drop_source

_CACHE: dict[str, dict] = {}
SQL = "SELECT COUNT(*) AS n FROM visits v JOIN parks p ON p.id = v.park_id WHERE v.status = 1"


async def _schema(path: str) -> dict:
    if path not in _CACHE:
        probe = SimpleNamespace(id=f"probe-{uuid.uuid4().hex[:6]}", name="scenic", kind="sqlite", database=path,
                                host=None, port=None, username=None, password=None, options={}, readonly=True,
                                description="", schema_cache={}, origin="manual")
        try:
            _CACHE[path] = await introspect(probe)
        finally:
            await engines.invalidate(probe.id)
    return _CACHE[path]


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(scenic_db):
    """带目录的景区数据源；用完连同模板一起删掉（删源会级联删掉目录）。"""
    name = f"scenic_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=scenic_db, readonly=True)
        row.schema_cache = await _schema(scenic_db)
        session.add(row)
        await session.commit()
        for table, notes in scenic_notes.notes().items():
            await catalog.write_entry(session, row.id, table, notes, if_version=0, actor="王敏")
        sid = row.id
    made: list[str] = []
    yield SimpleNamespace(id=sid, name=name, made=made)
    async with SessionLocal() as session:
        for wid in made:
            if (wf := await session.get(Workflow, wid)) is not None:
                await session.delete(wf)
        await session.commit()
    await drop_source(sid)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _chain(*nodes) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def daily(source: str) -> dict:
    return _chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": SQL}),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.fetch }}"}]),
    )


def agent_only(source: str) -> dict:
    """SQL 只写在 Agent 里：图上没有一条写死的查询。"""
    return _chain(
        node("start", "input"),
        node("ask", "agent", prompt="查一下昨天各景区的入园人数", tools=[f"db_query__{source}"]),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.ask.text }}"}]),
    )


def team(source: str) -> dict:
    """协作成员里有一个绑定了查询工具。"""
    return _chain(
        node("start", "input"),
        node("crew", "supervisor", agents=[
            {"name": "取数员", "prompt": "查入园人数", "tools": [f"db_query__{source}"]},
            {"name": "撰稿人", "prompt": "写一句总结", "tools": []},
        ]),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.crew.text }}"}]),
    )


def mixed(source: str) -> dict:
    """写死的查询用到入园记录和景区，Agent 还绑着同一个源。"""
    return _chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": SQL}),
        node("ask", "agent", prompt="再看看别的表", tools=[f"db_query__{source}"]),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.ask.text }}"}]),
    )


async def _publish(client, source, graph: dict | None = None) -> str:
    wf = (await client.post("/api/workflows", json={"name": f"入园日报-{uuid.uuid4().hex[:6]}",
                                                    "graph": graph or daily(source.name)})).json()["id"]
    source.made.append(wf)
    body = (await client.post(f"/api/workflows/{wf}/publish", json={"level": "published"})).json()
    assert body["ok"] is True, body
    return wf


async def _events(run_id: str, kind: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == kind).order_by(RunEvent.seq))).scalars()
        return [e.data for e in rows]


async def _settle(run_id: str, *, sealed: bool = True) -> Run:
    """等运行结束。sealed=False 时不等封存：Agent 没有可用的模型，跑到它那一步就失败，只看开头的事件。"""
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and (row.manifest_seq is not None or not sealed):
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def _edit_visits(source_id: str) -> int:
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "visits")
        saved = await catalog.save_patch(session, source_id, "visits",
                                         [{"path": "columns.visitor_count.measure", "value": "stock"}],
                                         if_version=entry.version, actor="李雷")
        return saved.version


async def _recorded(wf: str) -> dict | None:
    async with SessionLocal() as session:
        return (await session.execute(select(WorkflowVersion.catalog_versions).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()


async def _catalogued(source_id: str) -> dict[str, int]:
    """这个源现在所有有目录的表：{表名: 版本}。"""
    async with SessionLocal() as session:
        return {t: e.version for t, e in (await catalog.read_catalog(session, source_id)).items()}


async def _formal(client, wf: str, *, sealed: bool = True) -> str:
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    assert r.status_code == 201, r.text                 # 不拦
    run_id = r.json()["id"]
    await _settle(run_id, sealed=sealed)
    return run_id


async def test_publish_records_catalog_versions(client, source):
    wf = await _publish(client, source)
    async with SessionLocal() as session:
        snap = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
    # 入园记录有目录（第 1 版）；景区表还没有目录，记 0。没有 Agent 查库，可能涉及的一组是空的
    assert snap.catalog_versions == {"direct": {source.name: {"parks": 0, "visits": 1}}, "possible": {}}
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["catalog_versions"] == {"direct": {source.name: {"parks": 0, "visits": 1}}, "possible": {}}
    assert detail["catalog_changes"] == []


async def test_formal_run_warns_on_catalog_change_but_runs(client, source):
    wf = await _publish(client, source)
    now = await _edit_visits(source.id)
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    assert r.status_code == 201, r.text                 # 不拦
    run_id = r.json()["id"]
    row = await _settle(run_id)
    assert row.status == "succeeded", row.error
    [drift] = await _events(run_id, "catalog.drift")
    assert drift["count"] == 1 and drift["version"] == 1
    [table] = drift["tables"]
    assert table == {"source": source.name, "source_id": source.id, "table": "visits", "label": "入园记录",
                     "published": 1, "current": now, "impact": "direct"}
    # 版本详情也写明哪些表在发布之后变了
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert [(c["table"], c["published"], c["current"]) for c in detail["catalog_changes"]] == [("visits", 1, now)]


async def test_catalog_added_after_publish_counts_as_change(client, source):
    wf = await _publish(client, source)
    async with SessionLocal() as session:
        await catalog.write_entry(session, source.id, "parks", {"label": catalog.make_item("景区", "human")},
                                  if_version=0, actor="王敏")
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    run_id = r.json()["id"]
    await _settle(run_id)
    [drift] = await _events(run_id, "catalog.drift")
    assert [(t["table"], t["published"], t["current"], t["label"]) for t in drift["tables"]] == [("parks", 0, 1, "景区")]


# --------------------------------------------------------------------------
# SQL 只写在 Agent 里：可能涉及的表
# --------------------------------------------------------------------------


async def test_agent_only_template_records_every_catalogued_table_as_possible(client, source):
    """影响面把绑定了查询工具的 Agent 算作「可能涉及」，发布记录得是同一个说法：记下这个源所有有目录的表。"""
    wf = await _publish(client, source, agent_only(source.name))
    recorded = await _recorded(wf)
    assert recorded == {"direct": {}, "possible": {source.name: await _catalogued(source.id)}}
    assert "visits" in recorded["possible"][source.name] and "parks" not in recorded["possible"][source.name]
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["catalog_versions"] == recorded and detail["catalog_changes"] == []


async def test_agent_only_template_warns_after_a_catalog_change(client, source):
    wf = await _publish(client, source, agent_only(source.name))
    now = await _edit_visits(source.id)
    run_id = await _formal(client, wf, sealed=False)
    [drift] = await _events(run_id, "catalog.drift")
    assert drift["count"] == 1
    assert drift["tables"] == [{"source": source.name, "source_id": source.id, "table": "visits", "label": "入园记录",
                                "published": 1, "current": now, "impact": "possible"}]
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert [(c["table"], c["impact"]) for c in detail["catalog_changes"]] == [("visits", "possible")]


async def test_catalog_created_after_publish_counts_for_possible_tables(client, source):
    """发布时没有目录的表不逐张记 0（一个库几百张表），之后有人给它建了目录同样算变化。"""
    wf = await _publish(client, source, agent_only(source.name))
    async with SessionLocal() as session:
        await catalog.write_entry(session, source.id, "parks", {"label": catalog.make_item("景区", "human")},
                                  if_version=0, actor="王敏")
    run_id = await _formal(client, wf, sealed=False)
    [drift] = await _events(run_id, "catalog.drift")
    assert [(t["table"], t["published"], t["current"], t["impact"]) for t in drift["tables"]] == [
        ("parks", 0, 1, "possible")]


async def test_team_members_with_the_query_tool_count_as_possible(client, source):
    wf = await _publish(client, source, team(source.name))
    assert await _recorded(wf) == {"direct": {}, "possible": {source.name: await _catalogued(source.id)}}


async def test_direct_tables_are_not_repeated_as_possible(client, source):
    """同一个源既有写死的查询、又有 Agent：写死的 SQL 用到的表按直接引用记，可能涉及里不再记一遍。"""
    wf = await _publish(client, source, mixed(source.name))
    recorded = await _recorded(wf)
    assert recorded["direct"] == {source.name: {"parks": 0, "visits": 1}}
    rest = {t: v for t, v in (await _catalogued(source.id)).items() if t not in ("parks", "visits")}
    assert recorded["possible"] == {source.name: rest} and rest
    now = await _edit_visits(source.id)
    async with SessionLocal() as session:
        await catalog.write_entry(session, source.id, "parks", {"label": catalog.make_item("景区", "human")},
                                  if_version=0, actor="王敏")
    run_id = await _formal(client, wf, sealed=False)
    [drift] = await _events(run_id, "catalog.drift")
    # 两张都是直接引用：parks 新建了目录也不会再按可能涉及报一遍
    assert [(t["table"], t["published"], t["current"], t["impact"]) for t in drift["tables"]] == [
        ("parks", 0, 1, "direct"), ("visits", 1, now, "direct")]


async def test_direct_only_records_from_before_still_warn_as_direct(client, source):
    """可能涉及上线之前发布的版本只记了直接引用（{源名: {表名: 版本}}）：照旧按直接引用比，不补可能涉及。"""
    wf = await _publish(client, source, mixed(source.name))
    async with SessionLocal() as session:
        snap = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
        snap.catalog_versions = {source.name: {"parks": 0, "visits": 1}}
        await session.commit()
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["catalog_versions"] == {"direct": {source.name: {"parks": 0, "visits": 1}}, "possible": {}}
    now = await _edit_visits(source.id)
    async with SessionLocal() as session:
        await catalog.write_entry(session, source.id, "gates", {"label": catalog.make_item("闸机", "human")},
                                  if_version=0, actor="王敏")
    run_id = await _formal(client, wf, sealed=False)
    [drift] = await _events(run_id, "catalog.drift")
    assert [(t["table"], t["impact"]) for t in drift["tables"]] == [("visits", "direct")]
    assert drift["tables"][0]["current"] == now


async def test_no_change_no_warning(client, source):
    wf = await _publish(client, source)
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    run_id = r.json()["id"]
    row = await _settle(run_id)
    assert row.status == "succeeded", row.error
    assert await _events(run_id, "catalog.drift") == []
    # 探索运行不比对（不是从发布版本发起的）
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "exploratory"})
    await _edit_visits(source.id)
    run_id = r.json()["id"]
    await _settle(run_id)
    assert await _events(run_id, "catalog.drift") == []


async def test_versions_published_before_the_record_existed_get_no_warning(client, source):
    """这个功能之前发布的版本没有记录：不知道当时是哪一版，不提醒。"""
    wf = await _publish(client, source)
    async with SessionLocal() as session:
        snap = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
        snap.catalog_versions = None
        await session.commit()
    await _edit_visits(source.id)
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    run_id = r.json()["id"]
    await _settle(run_id)
    assert await _events(run_id, "catalog.drift") == []
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["catalog_versions"] is None and detail["catalog_changes"] == []
