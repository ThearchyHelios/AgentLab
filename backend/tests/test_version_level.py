"""发布级别记在版本上（WorkflowVersion.level）。

以前「这次正式运行按不按受管出具」看的是工作流当前的 status。可 status 说的是当前画布：
受管版本发布之后在画布上改一笔，status 就退回 draft，再从那个受管版本发起的正式运行
就被当成已发布级别出具（单元格不必在契约里声明 cells 就放行）。级别要跟着版本走：
发布时记在那一版上，正式运行看它钉的版本；没有记过级别的老版本照旧看 status。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, select, text

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
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
def writer(monkeypatch):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda model, messages: AIMessage(content="本周销售额 [[m:gmv]]。"))


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly() -> dict:
    nodes = [
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 300.5}", assign_to="kpi"),
        node("card", "metrics", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
        node("write", "report", instructions="写周报",
             numbers="strict", on_violation="fail", claims="require_citation"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def version_row(wf: str, version: int) -> WorkflowVersion:
    async with SessionLocal() as session:
        return (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == version))).scalar_one()


async def notes(run_id: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "log").order_by(RunEvent.seq))).scalars()
        return [e.data for e in rows if e.data.get("code") == "run_governance"]


async def settle(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


@pytest.mark.parametrize("level", ["published", "governed"])
async def test_publishing_records_the_level_on_the_version(client, level):
    wf = (await client.post("/api/workflows", json={"name": f"周报-{level}", "graph": weekly()})).json()["id"]
    body = (await client.post(f"/api/workflows/{wf}/publish", json={"level": level})).json()
    assert body["ok"] is True, body["issues"]
    assert (await version_row(wf, 1)).level == level
    versions = (await client.get(f"/api/workflows/{wf}/versions")).json()
    assert versions[0]["level"] == level
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["level"] == level and detail["published"] is True


async def test_a_blocked_publish_records_nothing(client):
    graph = weekly()
    graph["nodes"][-1]["data"]["config"]["contract"].pop("required")
    wf = (await client.post("/api/workflows", json={"name": "周报-被拦", "graph": graph})).json()["id"]
    assert (await client.post(f"/api/workflows/{wf}/publish", json={"level": "governed"})).json()["ok"] is False
    assert (await version_row(wf, 1)).level is None


async def test_republishing_the_same_version_updates_its_level(client):
    wf = (await client.post("/api/workflows", json={"name": "周报-升档", "graph": weekly()})).json()["id"]
    await client.post(f"/api/workflows/{wf}/publish", json={"level": "published"})
    await client.post(f"/api/workflows/{wf}/publish", json={"level": "governed"})
    assert (await version_row(wf, 1)).level == "governed"


@pytest.mark.parametrize("level, governed", [("governed", True), ("published", False)])
async def test_editing_the_canvas_after_publishing_does_not_change_how_the_version_issues(client, level, governed):
    from app.engine.governance import governed_formal

    wf = (await client.post("/api/workflows", json={"name": f"周报-改画布-{level}", "graph": weekly()})).json()["id"]
    assert (await client.post(f"/api/workflows/{wf}/publish", json={"level": level})).json()["ok"] is True
    edited = weekly()
    edited["nodes"][3]["data"]["label"] = "改过的报告"
    after = (await client.patch(f"/api/workflows/{wf}", json={"graph": edited})).json()
    assert after["status"] == "draft" and after["published_version"] == 1
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    row = await settle(run_id)
    [note] = await notes(run_id)
    assert note["governed"] is governed and "v1" in note["message"], note
    assert await governed_formal(run_id) is governed
    assert row.status == "succeeded", row.error


async def test_a_version_published_before_the_column_existed_follows_the_status(client):
    """老版本没有记级别：照旧看工作流现在的 status（test_contract_cells 那套旧规则）。"""
    async with SessionLocal() as session:
        w = Workflow(name="周报-老版本", graph=weekly(), status="governed", published_version=1)
        session.add(w)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=w.id, version=1, graph=weekly()))
        await session.commit()
        wf = w.id
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    run_id = r.json()["id"]
    await settle(run_id)
    [note] = await notes(run_id)
    assert note["governed"] is True


def test_the_column_is_added_to_old_databases():
    from app.db.base import _migrate

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE workflow_versions (id VARCHAR(32) PRIMARY KEY, workflow_id VARCHAR(32), "
                          "version INTEGER, graph JSON, note TEXT, graph_hash VARCHAR(64))"))
        conn.execute(text("INSERT INTO workflow_versions (id, workflow_id, version) VALUES ('a', 'w', 1)"))
        _migrate(conn)
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(workflow_versions)"))}
        assert "level" in cols
        assert conn.execute(text("SELECT level FROM workflow_versions")).scalar() is None
