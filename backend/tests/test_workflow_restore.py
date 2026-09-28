"""恢复旧版本也是改图：受管 / 已发布得退回草稿。

status 说的是**当前画布**的状态。PATCH 改图时早就这么退了，restore 接口却只换图、
升版本、留快照，状态原样挂着——一张受管工作流恢复成从没过闸的旧图，工具栏照样
写「受管」。前端的版本历史为此绕开了它，但接口还对外开着，谁直接调都会中招。
已发布的那一版仍由 published_version 指着，正式运行不受影响。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Workflow
from app.main import app


def _graph(label: str) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
             "data": {"label": "输入", "config": {"fields": [{"name": "q"}]}}},
            {"id": "out", "type": "output", "position": {"x": 300, "y": 0},
             "data": {"label": label, "config": {"fields": [{"name": "r", "value": "{{ input.q }}"}]}}},
        ],
        "edges": [{"source": "start", "target": "out"}],
    }


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("level", ["published", "governed"])
async def test_restoring_an_old_version_drops_back_to_draft(client, level):
    old, new = _graph("旧出口"), _graph("新出口")
    workflow_id = (await client.post("/api/workflows", json={"name": "恢复", "graph": old})).json()["id"]
    assert (await client.patch(f"/api/workflows/{workflow_id}", json={"graph": new})).status_code == 200
    # 受管要过治理 lint，这里只关心恢复之后的状态，直接把 v2 立成发布版
    async with SessionLocal() as session:
        row = await session.get(Workflow, workflow_id)
        row.status, row.published_version = level, 2
        await session.commit()

    r = await client.post(f"/api/workflows/{workflow_id}/versions/1/restore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graph"] == old and body["version"] == 3
    assert body["status"] == "draft", f"恢复成 v1 之后仍显示 {body['status']}"
    assert body["published_version"] == 2, "发布的那一版不受影响"
    assert (await client.get(f"/api/workflows/{workflow_id}")).json()["status"] == "draft"


async def test_restoring_a_draft_stays_a_draft(client):
    workflow_id = (await client.post("/api/workflows", json={"name": "恢复", "graph": _graph("a")})).json()["id"]
    await client.patch(f"/api/workflows/{workflow_id}", json={"graph": _graph("b")})
    body = (await client.post(f"/api/workflows/{workflow_id}/versions/1/restore")).json()
    assert body["status"] == "draft" and body["published_version"] is None
