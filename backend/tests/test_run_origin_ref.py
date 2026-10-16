"""运行 ↔ 轮次的来源链（next-round 02 做法 9）：POST /api/runs 的 origin_ref、「在画布里打开」的 WorkflowIn.origin。

- origin_ref 只在来源是问数据时收：核对对话用不用得了、对话在不在、这一轮是不是这段对话的，写进 Run.origin 和封存的 run.origin；
  别的来源带它、伪造的（别的对话的轮次、不存在的对话）一律拒收；
- 新建工作流带 origin（run_id、conversation_id、turn_id）：运行读不到、轮次不属于对话的拒收，对得上的记进 workflows.origin，
  形状同从运行提取（/copilot/from-run）。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow
from app.engine.runner import run_manager
from app.main import app

GRAPH = {"nodes": [{"id": "start", "type": "input", "position": {"x": 0, "y": 0},
                    "data": {"label": "开始", "config": {"fields": [{"name": "question"}]}}},
                   {"id": "out", "type": "output", "position": {"x": 0, "y": 0},
                    "data": {"label": "成果", "config": {"fields": [{"name": "答案", "value": "{{ input.question }}"}]}}}],
         "edges": [{"source": "start", "target": "out"}]}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def conversation_with_turn(client) -> tuple[str, str]:
    res = await client.post("/api/conversations", json={"title": "问数据"})
    assert res.status_code == 201, res.text
    cid = res.json()["id"]
    res = await client.post(f"/api/conversations/{cid}/turns", json={"question": "本周销售额多少"})
    assert res.status_code == 201, res.text
    return cid, res.json()["id"]


async def wait(run_id: str) -> Run:
    for _ in range(200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError("运行没跑完")


async def test_a_chat_run_records_its_conversation_and_turn(client):
    cid, tid = await conversation_with_turn(client)
    res = await client.post("/api/runs", json={"graph": GRAPH, "input": {"question": "本周销售额多少"}, "origin": "chat",
                                                "origin_ref": {"conversation_id": cid, "turn_id": tid}})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["origin_kind"] == "chat" and body["origin"] == {"conversation_id": cid, "turn_id": tid}
    row = await wait(body["id"])
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "run.origin"))).scalars()
    # 在封存范围内：事后改不了
    assert event.seq <= row.manifest_seq
    assert event.data["kind"] == "chat" and event.data["conversation_id"] == cid and event.data["turn_id"] == tid


async def test_origin_ref_is_only_for_chat_runs(client):
    cid, tid = await conversation_with_turn(client)
    res = await client.post("/api/runs", json={"graph": GRAPH, "origin": "canvas",
                                                "origin_ref": {"conversation_id": cid, "turn_id": tid}})
    assert res.status_code == 422 and res.json()["code"] == "origin_ref_not_allowed"
    assert res.json()["field"] == "origin_ref"


async def test_forged_origin_refs_are_refused(client):
    cid, tid = await conversation_with_turn(client)
    other, other_turn = await conversation_with_turn(client)
    for ref in ({"conversation_id": "nope", "turn_id": tid}, {"conversation_id": cid, "turn_id": other_turn},
                {"conversation_id": cid, "turn_id": "nope"}):
        res = await client.post("/api/runs", json={"graph": GRAPH, "origin": "chat", "origin_ref": ref})
        assert res.status_code == 422, (ref, res.text)
        assert res.json()["code"] == "origin_ref_invalid"


async def test_someone_elses_conversation_is_refused(client, monkeypatch):
    """对话按人隔离（开登录后的使用者）：挂到别人的对话上，和读别人的对话一样回 403。"""
    from app.api import conversations

    cid, tid = await conversation_with_turn(client)
    monkeypatch.setattr(conversations, "_isolated", lambda: "someone-else")
    res = await client.post("/api/runs", json={"graph": GRAPH, "origin": "chat",
                                                "origin_ref": {"conversation_id": cid, "turn_id": tid}})
    assert res.status_code == 403


async def test_open_in_canvas_records_where_the_workflow_came_from(client):
    cid, tid = await conversation_with_turn(client)
    run = (await client.post("/api/runs", json={"graph": GRAPH, "origin": "chat",
                                                 "origin_ref": {"conversation_id": cid, "turn_id": tid}})).json()
    await wait(run["id"])
    res = await client.post("/api/workflows", json={"name": "问数据：本周销售额", "graph": GRAPH,
                                                     "origin": {"run_id": run["id"], "conversation_id": cid,
                                                                "turn_id": tid}})
    assert res.status_code == 201, res.text
    async with SessionLocal() as session:
        wf = await session.get(Workflow, res.json()["id"])
    assert wf.origin == {"run_id": run["id"], "conversation_id": cid, "turn_id": tid}

    # 不带来源的照旧（手建的为空）
    res = await client.post("/api/workflows", json={"name": "手建", "graph": GRAPH})
    async with SessionLocal() as session:
        assert (await session.get(Workflow, res.json()["id"])).origin is None


async def test_open_in_canvas_refuses_origins_that_do_not_check_out(client):
    cid, tid = await conversation_with_turn(client)
    other, other_turn = await conversation_with_turn(client)
    run = (await client.post("/api/runs", json={"graph": GRAPH, "origin": "chat"})).json()
    await wait(run["id"])
    for origin in ({"run_id": "nope"}, {"run_id": run["id"], "conversation_id": cid, "turn_id": other_turn},
                   {"run_id": run["id"], "turn_id": tid}):
        res = await client.post("/api/workflows", json={"name": "x", "graph": GRAPH, "origin": origin})
        assert res.status_code == 422, (origin, res.text)
        assert res.json()["code"] == "workflow_origin_invalid"
