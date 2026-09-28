"""停在审批上的运行要能放弃；记录页要能只看「没保存的工作流」跑出来的运行。

放弃：等审批的运行此时没有执行任务，以前 POST /cancel 只会去取消任务，于是一律
回 409「已经不在执行中」。可它明明还挂着一条待审批——界面上右栏的「去审批」旁边
给不出「放弃这次运行」，审批卡留在全局待办里，只能批了它、让它跑完。服务重启
挂起的运行（interrupted、没有待审批）同样没法收掉。

真正的取消：状态置为 cancelled，待审批一并关掉（不再出现在待办里，也不能再批），
发 run.cancelled 并封存；在线的客户端收到结束标记。
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Workflow
from app.engine.runner import run_manager
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


GATED = {
    "nodes": [
        node("start", "input", fields=[{"name": "question"}]),
        node("gate", "human", mode="approve", title="放行吗"),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.gate.approved }}"}]),
    ],
    "edges": [{"source": "start", "target": "gate"}, {"source": "gate", "target": "out"}],
}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 15.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 没有进入 {statuses}")


async def _waiting_run() -> str:
    run = await run_manager.start(graph=GATED, input_payload={"question": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    return run.id


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


async def test_a_run_waiting_for_approval_can_be_abandoned(client):
    run_id = await _waiting_run()
    r = await client.post(f"/api/runs/{run_id}/cancel", headers={"X-Actor": quote("审核员甲")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    row = (await client.get(f"/api/runs/{run_id}")).json()
    assert row["status"] == "cancelled"
    assert row["finished_at"] and row["manifest_seq"], "取消是终态：要收尾时间，也要封存"
    assert row["usage"]["wait_ms"] >= 0

    # 待审批一并关掉：不再出现在待办里，谁关的记下来
    assert (await client.get("/api/approvals", params={"status": "pending",
                                                        "run_id": run_id})).json() == []
    async with SessionLocal() as session:
        approval = (await session.execute(
            select(Approval).where(Approval.run_id == run_id))).scalar_one()
    assert approval.status == "cancelled"
    assert approval.resolved_by == "审核员甲" and approval.resolved_at is not None

    events = await _events(run_id)
    last = events[-1]
    assert last.type == "run.cancelled", [e.type for e in events[-3:]]
    assert set(last.data["timing"]) == {"wall_ms", "active_ms", "wait_ms"}
    assert last.data.get("actor") == "审核员甲"
    verified = (await client.get(f"/api/runs/{run_id}/verify")).json()
    assert verified["sealed"] and verified["ok"], verified


async def test_an_abandoned_run_cannot_be_approved_or_continued(client):
    run_id = await _waiting_run()
    assert (await client.post(f"/api/runs/{run_id}/cancel")).status_code == 200
    async with SessionLocal() as session:
        approval = (await session.execute(
            select(Approval).where(Approval.run_id == run_id))).scalar_one()

    r = await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": True})
    assert r.status_code == 409
    r = await client.post(f"/api/runs/{run_id}/resume", json={"response": True})
    assert r.status_code == 409
    r = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409 and "已取消" in r.json()["detail"]
    # 再取消一次是 409：已经是终态了
    r = await client.post(f"/api/runs/{run_id}/cancel")
    assert r.status_code == 409


async def test_a_run_suspended_by_a_restart_can_be_abandoned_too(client):
    async with SessionLocal() as session:
        run = Run(workflow_name="挂起", status="interrupted", graph=GATED, input={},
                  error="服务重启，运行已挂起，可从断点恢复")
        session.add(run)
        await session.commit()
        run_id = run.id
    r = await client.post(f"/api/runs/{run_id}/cancel")
    assert r.status_code == 200, r.text
    row = (await client.get(f"/api/runs/{run_id}")).json()
    assert row["status"] == "cancelled"
    assert (await _events(run_id))[-1].type == "run.cancelled"


async def test_finished_runs_still_refuse_to_be_cancelled(client):
    async with SessionLocal() as session:
        run = Run(workflow_name="完了", status="succeeded", graph=GATED, input={})
        session.add(run)
        await session.commit()
        run_id = run.id
    r = await client.post(f"/api/runs/{run_id}/cancel")
    assert r.status_code == 409
    assert "已完成" in r.json()["detail"]
    assert (await client.post("/api/runs/nope/cancel")).status_code == 404


class _Socket:
    """够 stream_run 用的最小 WebSocket：记下发出去的帧。"""

    def __init__(self) -> None:
        self.query_params: dict[str, str] = {}
        self.sent: list[dict] = []
        self.closed = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def receive_text(self) -> str:
        await self.closed.wait()
        from starlette.websockets import WebSocketDisconnect

        raise WebSocketDisconnect()

    async def close(self) -> None:
        self.closed.set()


async def test_a_live_stream_ends_when_the_waiting_run_is_abandoned():
    """停在审批上时在线的连接不关（它在等恢复）。放弃之后它要收到 run.cancelled 和结束标记。"""
    from app.api.runs import stream_run

    run_id = await _waiting_run()
    async with SessionLocal() as session:
        # 让连接以为运行还在进行：审批停下时在线的客户端就是这个处境
        (await session.get(Run, run_id)).status = "running"
        await session.commit()
    socket = _Socket()
    listening = asyncio.create_task(stream_run(socket, run_id))
    await asyncio.sleep(0.2)
    async with SessionLocal() as session:
        (await session.get(Run, run_id)).status = "interrupted"
        await session.commit()

    assert await run_manager.cancel(run_id) == "cancelled"
    await asyncio.wait_for(listening, 5)
    types = [f.get("type") for f in socket.sent]
    assert "run.cancelled" in types, types
    assert socket.sent[-1]["type"] == "stream.end"
    assert socket.sent[-1]["data"]["status"] == "cancelled"


# --------------------------------------------------------------------------
# 记录页：只看没保存的工作流跑出来的运行
# --------------------------------------------------------------------------


async def test_runs_can_be_filtered_to_unsaved_workflows(client):
    async with SessionLocal() as session:
        workflow = Workflow(name="已保存的", graph=GATED)
        session.add(workflow)
        await session.flush()
        saved = Run(workflow_id=workflow.id, workflow_name="已保存的", graph=GATED, input={},
                    status="succeeded")
        loose = Run(workflow_id=None, workflow_name="临时图", graph=GATED, input={},
                    status="succeeded")
        session.add_all([saved, loose])
        await session.commit()
        saved_id, loose_id = saved.id, loose.id

    ids = [r["id"] for r in (await client.get(
        "/api/runs", params={"workflow_id": "__none__", "limit": 200})).json()]
    assert loose_id in ids and saved_id not in ids
    assert all(r["workflow_id"] is None for r in (await client.get(
        "/api/runs", params={"workflow_id": "__none__"})).json())


async def test_stopping_a_running_run_records_who_stopped_it(client, monkeypatch):
    from app.engine import compiler
    from app.engine.schema import NodeType

    async def slow(state, ctx):
        await asyncio.sleep(5)
        return {"nodes": {ctx.node.id: "x"}}

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, slow)
    graph = {"nodes": [node("start", "input"), node("work", "transform", mode="template", template="x")],
             "edges": [{"source": "start", "target": "work"}]}
    run = await run_manager.start(graph=graph, input_payload={})
    await _wait(run.id, ("running",))
    await asyncio.sleep(0.2)
    r = await client.post(f"/api/runs/{run.id}/cancel", headers={"X-Actor": quote("值班员")})
    assert r.json()["status"] == "stopping"
    await _wait(run.id, ("cancelled",))
    await run_manager.wait_idle(run.id)
    last = (await _events(run.id))[-1]
    assert last.type == "run.cancelled" and last.data["actor"] == "值班员", last.data
