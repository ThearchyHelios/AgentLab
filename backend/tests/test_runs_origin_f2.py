"""运行来源（X3）和 F2 回归。

- POST /api/runs 只接受客户端能声明的来源（chat / canvas / launcher / api），不传按 api 记；app_*、hook、schedule
  一律拒绝——否则谁都能把一次画布试跑说成应用发起的。来源记在运行上，run.origin 事件在 run.started 之前、封存范围内。
- F2：正式运行带图接着跑一律 409（formal_graph_locked）：它钉的是发布的不可变版本，换一张图运行就不再等于那一版。
  接着跑也不再改发起人，接着跑的人记在 run.resumed 里。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(schema: dict | None = None) -> dict:
    nodes = [node("start", "input", fields=[{"name": "q"}]),
             node("check", "validate", source="{{ input.q }}", schema=schema or {"type": "string"},
                  repair_with_llm=False, max_retries=0),
             node("out", "output", fields=[])]
    return {"nodes": nodes, "edges": [{"source": "start", "target": "check"}, {"source": "check", "target": "out"}]}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(run_id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                                           .order_by(RunEvent.seq))).scalars())


@pytest.mark.parametrize("origin", ["app_entry", "app_view", "app_backfill", "hook", "schedule", "nonsense"])
async def test_client_cannot_claim_server_only_origins(client, origin):
    resp = await client.post("/api/runs", json={"graph": graph(), "input": {"q": "x"}, "origin": origin})
    assert resp.status_code == 422 and resp.json()["code"] == "origin_not_allowed"


async def test_declared_origin_is_recorded_before_run_started(client):
    resp = await client.post("/api/runs", json={"graph": graph(), "input": {"q": "x"}, "origin": "canvas"},
                             headers={"X-Actor": "ann"})
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    assert resp.json()["origin_kind"] == "canvas"
    await _wait(run_id, ("succeeded", "failed"))
    events = await _events(run_id)
    kinds = [e.type for e in events]
    assert kinds.index("run.origin") < kinds.index("run.started")
    origin = next(e for e in events if e.type == "run.origin").data
    assert origin["kind"] == "canvas" and origin["actor"] == "ann"


async def test_no_origin_is_recorded_as_api(client):
    resp = await client.post("/api/runs", json={"graph": graph(), "input": {"q": "x"}})
    assert resp.status_code == 201 and resp.json()["origin_kind"] == "api"


async def test_formal_run_cannot_continue_with_a_graph_and_keeps_its_initiator(client):
    run = await run_manager.start(graph=graph({"type": "object", "required": ["a"]}), input_payload={"q": '"x"'},
                                  run_class="formal", started_by="alice")
    assert (await _wait(run.id, ("failed", "succeeded"))).status == "failed"

    resp = await client.post(f"/api/runs/{run.id}/continue", json={"graph": graph()}, headers={"X-Actor": "bob"})
    assert resp.status_code == 409 and resp.json()["code"] == "formal_graph_locked"
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.graph == graph({"type": "object", "required": ["a"]})

    resp = await client.post(f"/api/runs/{run.id}/continue", json={}, headers={"X-Actor": "bob"})
    assert resp.status_code == 200, resp.text
    await _wait(run.id, ("failed", "succeeded"))
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.started_by == "alice"
    resumed = [e for e in await _events(run.id) if e.type == "run.resumed"]
    assert resumed[-1].data["actor"] == "bob"


async def test_exploratory_run_can_still_continue_with_a_revised_graph(client):
    run = await run_manager.start(graph=graph({"type": "object", "required": ["a"]}), input_payload={"q": '"x"'},
                                  started_by="alice")
    assert (await _wait(run.id, ("failed", "succeeded"))).status == "failed"
    resp = await client.post(f"/api/runs/{run.id}/continue", json={"graph": graph()}, headers={"X-Actor": "bob"})
    assert resp.status_code == 200, resp.text
    final = await _wait(run.id, ("failed", "succeeded"))
    assert final.status == "succeeded" and final.started_by == "alice"
