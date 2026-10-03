"""发布时记下目录版本，正式运行时提示目录的变化（阶段 4A）。

- 发布（把某一版立为正式版本）时，记下这个模板 SQL 用到的每张表的目录版本 {源名: {表名: 版本}}，存在
  workflow_versions.catalog_versions；还没有目录的表记 0（之后补了目录也算变化）。
- 从这一版发起正式运行时和当前的目录版本比：有变化发一条 catalog.drift 事件（列出表和前后版本），**不拦运行**；
  目录没变不发。
- 版本详情给出发布时的目录版本，以及哪些表在那之后变了。
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


def daily(source: str) -> dict:
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": SQL}),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.fetch }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _publish(client, source) -> str:
    wf = (await client.post("/api/workflows", json={"name": f"入园日报-{uuid.uuid4().hex[:6]}",
                                                    "graph": daily(source.name)})).json()["id"]
    source.made.append(wf)
    body = (await client.post(f"/api/workflows/{wf}/publish", json={"level": "published"})).json()
    assert body["ok"] is True, body
    return wf


async def _events(run_id: str, kind: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == kind).order_by(RunEvent.seq))).scalars()
        return [e.data for e in rows]


async def _settle(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def _edit_visits(source_id: str) -> int:
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "visits")
        saved = await catalog.save_patch(session, source_id, "visits",
                                         [{"path": "columns.visitor_count.measure", "value": "stock"}],
                                         if_version=entry.version, actor="李雷")
        return saved.version


async def test_publish_records_catalog_versions(client, source):
    wf = await _publish(client, source)
    async with SessionLocal() as session:
        snap = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
    # 入园记录有目录（第 1 版）；景区表还没有目录，记 0
    assert snap.catalog_versions == {source.name: {"parks": 0, "visits": 1}}
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["catalog_versions"] == {source.name: {"parks": 0, "visits": 1}}
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
                     "published": 1, "current": now}
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
