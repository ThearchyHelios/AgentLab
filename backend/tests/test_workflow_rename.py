"""工作流改名（PATCH /api/workflows/{id} 的 name）：去掉首尾空白，不许空、超长、和别的工作流同名；内置模板不许改名。

以前名字原样写进去：空名、重名都能存，界面上也没有改名的入口。改名只动名字，不加版本、不碰图。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Workflow
from app.main import app
from app.seed import TEMPLATES

GRAPH = {"nodes": [{"id": "start", "type": "input", "position": {"x": 0, "y": 0},
                    "data": {"label": "开始", "config": {"fields": [{"name": "question"}]}}}], "edges": []}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make(client: AsyncClient, name: str) -> dict:
    r = await client.post("/api/workflows", json={"name": name, "graph": GRAPH})
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_a_workflow_can_be_renamed_without_a_new_version(client):
    w = await _make(client, "周报草稿-改名测试")
    r = await client.patch(f"/api/workflows/{w['id']}", json={"name": "  经营周报-改名测试  "})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["name"] == "经营周报-改名测试", "首尾空白去掉"
    assert got["version"] == w["version"], "只改名不加版本"
    assert got["graph"] == w["graph"]


async def test_empty_too_long_and_taken_names_are_refused(client):
    a = await _make(client, "甲-改名测试")
    await _make(client, "乙-改名测试")
    r = await client.patch(f"/api/workflows/{a['id']}", json={"name": "   "})
    assert r.status_code == 422 and "不能为空" in r.json()["detail"]
    r = await client.patch(f"/api/workflows/{a['id']}", json={"name": "长" * 201})
    assert r.status_code == 422 and "最多" in r.json()["detail"]
    r = await client.patch(f"/api/workflows/{a['id']}", json={"name": "乙-改名测试"})
    assert r.status_code == 409 and "同名" in r.json()["detail"]
    r = await client.patch(f"/api/workflows/{a['id']}", json={"name": "甲-改名测试 "})
    assert r.status_code == 200, "名字没变（只多了空白）照常放行，不算和自己重名"
    async with SessionLocal() as s:
        assert (await s.get(Workflow, a["id"])).name == "甲-改名测试"


async def test_a_built_in_template_keeps_its_name(client):
    builtin = TEMPLATES[0]["name"]
    async with SessionLocal() as s:
        row = Workflow(name=builtin, description="", graph=GRAPH, tags=["template"], is_template=True)
        s.add(row)
        await s.commit()
        wid = row.id
    r = await client.patch(f"/api/workflows/{wid}", json={"name": "我的模板-改名测试"})
    assert r.status_code == 409 and "内置模板" in r.json()["detail"], "启动时按名字补内置模板，改了名会再导入一份"
    r = await client.patch(f"/api/workflows/{wid}", json={"description": "只改说明可以"})
    assert r.status_code == 200 and r.json()["name"] == builtin
