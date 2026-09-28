"""删除工作流时的两道口子：还在跑的运行、以及中文署名。

- 运行记录的 workflow_id 是 ON DELETE SET NULL：删掉工作流，运行记录本身留着，
  封存核对用的是运行自带的图快照，不受影响。但还在跑的那次运行会失去归属，
  界面上再也回不到它的画布——和删除运行记录一样，先停再删。
- 发布署名从 X-Actor 请求头来。浏览器请求头只能是 Latin-1，前端按
  encodeURIComponent 编码后发，发布时要解回来，否则中文署名存成 %E5...。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run, Workflow
from app.main import app

GRAPH = {
    "nodes": [
        {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
         "data": {"label": "输入", "config": {"fields": [{"name": "q"}]}}},
        {"id": "out", "type": "output", "position": {"x": 300, "y": 0},
         "data": {"label": "成果", "config": {"fields": [{"name": "r", "value": "{{ input.q }}"}]}}},
    ],
    "edges": [{"source": "start", "target": "out"}],
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _workflow_with_run(status: str) -> tuple[str, str]:
    async with SessionLocal() as session:
        workflow = Workflow(name="删除保护", graph=GRAPH)
        session.add(workflow)
        await session.flush()
        run = Run(workflow_id=workflow.id, workflow_name=workflow.name, graph=GRAPH,
                  input={}, status=status)
        session.add(run)
        await session.commit()
        return workflow.id, run.id


@pytest.mark.parametrize("status", ["running", "queued"])
async def test_a_workflow_with_a_live_run_cannot_be_deleted(client, status):
    workflow_id, _ = await _workflow_with_run(status)
    r = await client.delete(f"/api/workflows/{workflow_id}")
    assert r.status_code == 409
    assert "停止" in r.json()["detail"]
    assert (await client.get(f"/api/workflows/{workflow_id}")).status_code == 200


async def test_finished_runs_survive_their_workflow(client):
    workflow_id, run_id = await _workflow_with_run("succeeded")
    assert (await client.delete(f"/api/workflows/{workflow_id}")).status_code == 204
    r = await client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["workflow_id"] is None


async def test_a_chinese_publisher_name_is_stored_decoded(client):
    r = await client.post("/api/workflows", json={"name": "署名", "graph": GRAPH})
    workflow_id = r.json()["id"]
    r = await client.post(f"/api/workflows/{workflow_id}/publish",
                          json={"level": "published"}, headers={"X-Actor": quote("张工")})
    assert r.status_code == 200, r.text
    assert r.json()["published_by"] == "张工"
    assert (await client.get(f"/api/workflows/{workflow_id}")).json()["published_by"] == "张工"
