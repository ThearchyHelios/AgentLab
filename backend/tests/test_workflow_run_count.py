"""单个工作流接口的 run_count 和列表接口按同一个函数算。

2026-10 验收：GET /api/workflows/{id} 的 run_count 恒为 0，同一时刻列表接口（GET /api/workflows）里这个工作流是
2 次、运行列表按 workflow_id 筛出来也是 2 条——只有列表接口按 Run.workflow_id 计数，详情、保存（PATCH）、恢复旧版
直接返回数据库对象，WorkflowOut.run_count 落在默认值 0 上。现在凡是返回工作流的接口都按同一个函数算，次数和列表一致。

工作流名、图、运行都是虚构的。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run
from app.main import app

GRAPH = {
    "nodes": [
        {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
         "data": {"label": "输入", "config": {"fields": [{"name": "week"}]}}},
        {"id": "out", "type": "output", "position": {"x": 300, "y": 0},
         "data": {"label": "成果", "config": {"fields": [{"name": "r", "value": "{{ input.week }}"}]}}},
    ],
    "edges": [{"source": "start", "target": "out"}],
}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _add_runs(workflow_id: str | None, n: int, status: str = "succeeded") -> None:
    async with SessionLocal() as session:
        for _ in range(n):
            session.add(Run(workflow_id=workflow_id, workflow_name="灯塔周报", graph=GRAPH, input={}, status=status))
        await session.commit()


async def _listed(client, workflow_id: str) -> int:
    [row] = [w for w in (await client.get("/api/workflows")).json() if w["id"] == workflow_id]
    return row["run_count"]


async def test_the_single_workflow_counts_its_runs_like_the_list(client):
    made = (await client.post("/api/workflows", json={"name": "灯塔周报", "graph": GRAPH})).json()
    other = (await client.post("/api/workflows", json={"name": "潮汐日报", "graph": GRAPH})).json()
    await _add_runs(made["id"], 3)
    await _add_runs(made["id"], 1, status="failed")
    # 别的工作流、没挂在工作流上的运行不算进来
    await _add_runs(other["id"], 2)
    await _add_runs(None, 1)

    assert await _listed(client, made["id"]) == 4
    got = await client.get(f"/api/workflows/{made['id']}")
    assert got.status_code == 200 and got.json()["run_count"] == 4
    assert (await client.get(f"/api/workflows/{other['id']}")).json()["run_count"] == await _listed(client, other["id"]) == 2


async def test_save_and_restore_answer_with_the_same_count(client):
    """保存、恢复旧版的返回也是这个工作流：界面拿它替换手里的那一份，次数不能变回 0。"""
    made = (await client.post("/api/workflows", json={"name": "灯塔周报", "graph": GRAPH})).json()
    await _add_runs(made["id"], 2)

    renamed = await client.patch(f"/api/workflows/{made['id']}", json={"name": "灯塔周报（新）"})
    assert renamed.status_code == 200 and renamed.json()["run_count"] == 2
    changed = {**GRAPH, "nodes": [*GRAPH["nodes"][:1], {**GRAPH["nodes"][1], "position": {"x": 320, "y": 0}}]}
    saved = await client.patch(f"/api/workflows/{made['id']}", json={"graph": changed, "note": "挪了一下"})
    assert saved.status_code == 200 and saved.json()["version"] == 2 and saved.json()["run_count"] == 2
    restored = await client.post(f"/api/workflows/{made['id']}/versions/1/restore")
    assert restored.status_code == 200 and restored.json()["run_count"] == 2
    assert await _listed(client, made["id"]) == 2


async def test_new_and_copied_workflows_have_no_runs_yet(client):
    """新建、复制出来的是新工作流，还没有运行；复制不带上原来那个的次数。"""
    made = (await client.post("/api/workflows", json={"name": "灯塔周报", "graph": GRAPH})).json()
    assert made["run_count"] == 0
    await _add_runs(made["id"], 3)
    copy = await client.post(f"/api/workflows/{made['id']}/duplicate")
    assert copy.status_code == 201 and copy.json()["run_count"] == 0
    assert (await client.get(f"/api/workflows/{made['id']}")).json()["run_count"] == 3
