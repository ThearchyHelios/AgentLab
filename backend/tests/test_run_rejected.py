"""驳回 ≠ 失败（docs/design/next-round/05-已知遗留.md (a)、(h)；总方案 X9）。

人的驳回（工具调用审批、代码执行审批、审批设了驳回即终止、填写 / 编辑被驳回、动作确认、双盲裁决）以前在引擎里抛普通的
NodeError，运行记成 failed：通知推「实例失败」，定时推「运行失败」，记录页给「继续运行」——接着跑只会读到同一个驳回答复、
再停一次。现在统一抛 NodeRejected，runner 收成 cancelled + end_reason=rejected，run.cancelled 带上在哪一步、哪种关卡、
结论、谁、备注；原话（Run.error）一字不变。手动停止记 end_reason=stopped。被驳回的不能接着跑，存量的 failed+驳回也不能。

动作确认、双盲裁决的驳回在 test_process_actions.py、test_dual_check_node.py 里（要假的外部系统、口径卡）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Task
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


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


START = node("start", "input", fields=[{"name": "q"}])
OUT = node("out", "output", fields=[{"name": "结果", "value": "到了"}])

#: (关卡, 那一步, 驳回之后 Run.error 的开头)
GATES = {
    "tool": (node("gate", "tool", label="算一下", tool="calculator", args={"expression": "1+1"}, approval="always"),
             "工具 calculator 的调用已被驳回：不对"),
    "code": (node("gate", "code", label="跑代码", code="print(1)", language="python", approval="always"),
             "代码执行已被驳回：不对"),
    "approve": (node("gate", "human", label="把关", mode="approve", title="可以吗", stop_on_reject=True),
                "人工驳回：不对"),
    "edit": (node("gate", "human", label="改稿", mode="edit", title="改一下", draft="初稿"), "人工驳回：不对"),
    "input": (node("gate", "human", label="补充", mode="input", title="补一下"), "人工驳回：不对"),
}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _events(run_id: str, kind: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        query = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        if kind:
            query = query.where(RunEvent.type == kind)
        return list((await session.execute(query)).scalars())


async def _rejected_run(kind: str, *, note: str = "不对", **reply) -> str:
    gate, _ = GATES[kind]
    run = await run_manager.start(graph=chain(START, gate, OUT), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, {"approved": False, "note": note, **reply}, actor="张工")
    return run.id


@pytest.mark.parametrize("kind", list(GATES))
async def test_a_rejection_ends_the_run_as_cancelled_with_who_and_why(kind):
    run_id = await _rejected_run(kind)
    final = await _wait(run_id, ("cancelled", "failed", "succeeded"))
    assert (final.status, final.end_reason) == ("cancelled", "rejected"), final.error
    # 原话不变：给人看的那一句照旧
    assert final.error == GATES[kind][1] and final.error_node_id == "gate" and final.finished_at is not None
    assert not await _events(run_id, "run.failed"), "驳回不是失败"
    [cancelled] = await _events(run_id, "run.cancelled")
    data = {k: v for k, v in cancelled.data.items() if k != "timing"}
    assert data == {"reason": "rejected", "node_id": "gate", "label": GATES[kind][0]["data"]["label"], "kind": kind,
                    "decision": "reject", "actor": "张工", "note": "不对"}
    # 收尾和取消一样：封存了，在线的流收到终态
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
    assert row.manifest_hash and row.manifest_seq == cancelled.seq


async def test_a_timeout_rejection_is_recorded_as_expired_reject():
    from app.process import tasks

    gate = node("gate", "human", label="把关", mode="approve", title="可以吗", stop_on_reject=True, due="PT30M",
                on_timeout={"action": "reject"})
    run = await run_manager.start(graph=chain(START, gate, OUT), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    ticked = await tasks.tick(utcnow() + timedelta(hours=1))
    async with SessionLocal() as session:
        mine = (await session.execute(select(Task.id).where(Task.run_id == run.id))).scalar_one()
    assert mine in ticked
    final = await _wait(run.id, ("cancelled", "failed", "succeeded"))
    assert (final.status, final.end_reason) == ("cancelled", "rejected")
    [cancelled] = await _events(run.id, "run.cancelled")
    assert cancelled.data["decision"] == "expired_reject" and cancelled.data["actor"] is None


async def test_the_api_carries_end_reason_and_refuses_to_continue_a_rejected_run(client):
    run_id = await _rejected_run("approve")
    await _wait(run_id, ("cancelled",))
    body = (await client.get(f"/api/runs/{run_id}")).json()
    assert (body["status"], body["end_reason"]) == ("cancelled", "rejected")
    r = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409 and r.json()["code"] == "run_rejected"
    assert r.json()["detail"] == "被驳回的运行不能继续运行：改好内容后重新发起"
    stop = await client.post(f"/api/runs/{run_id}/cancel")
    assert stop.status_code == 409 and "被驳回" in stop.json()["detail"]


async def test_a_legacy_failed_rejection_cannot_be_continued_either(client):
    """加 end_reason 之前被驳回的运行记的是 failed（封存不改写）：失败的那一步上最近一条处理完的待办是驳回，也不许接着跑。
    别的失败照旧能接着跑（这里断点不在，说的是没有断点，不是被驳回）。"""
    async with SessionLocal() as session:
        old = Run(workflow_name="老的驳回", status="failed", graph=chain(START, GATES["approve"][0], OUT), input={},
                  error="人工驳回：不对", error_node_id="gate")
        plain = Run(workflow_name="老的失败", status="failed", graph=chain(START, OUT), input={}, error="出错了")
        session.add_all([old, plain])
        await session.flush()
        session.add_all([
            RunEvent(run_id=old.id, seq=1, type="node.failed", node_id="gate", ts=1.0, data={"error": "人工驳回：不对"}),
            Task(run_id=old.id, node_id="gate", kind="approve", title="可以吗", status="resolved",
                 outcome={"decision": "reject"}),
            RunEvent(run_id=plain.id, seq=1, type="node.failed", node_id="out", ts=1.0, data={"error": "出错了"}),
        ])
        await session.commit()
        old_id, plain_id = old.id, plain.id
    r = await client.post(f"/api/runs/{old_id}/continue", json={})
    assert r.status_code == 409 and r.json()["code"] == "run_rejected", r.text
    other = await client.post(f"/api/runs/{plain_id}/continue", json={})
    assert other.status_code == 409 and other.json().get("code") != "run_rejected"


async def test_instance_state_says_the_same_for_the_new_shape_and_the_legacy_one():
    from app.apps.resolve import _trails, instance_state

    run_id = await _rejected_run("approve")
    await _wait(run_id, ("cancelled",))
    async with SessionLocal() as session:
        new = await session.get(Run, run_id)
        legacy = Run(workflow_name="老的驳回", status="failed", graph=new.graph, input={}, error=new.error,
                     error_node_id="gate")
        session.add(legacy)
        await session.flush()
        session.add_all([
            RunEvent(run_id=legacy.id, seq=1, type="node.failed", node_id="gate", ts=1.0, data={}),
            Task(run_id=legacy.id, node_id="gate", kind="approve", title="可以吗", status="resolved",
                 outcome={"decision": "reject"}),
        ])
        await session.commit()
        trails = await _trails(session, [new, legacy])
    said = {"code": "cancelled", "text": "「把关」被驳回"}
    assert instance_state(new, trails[new.id]) == said
    assert instance_state(legacy, trails[legacy.id]) == said


async def test_stopping_is_recorded_as_stopped():
    gate, _ = GATES["approve"]
    run = await run_manager.start(graph=chain(START, gate, OUT), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    assert await run_manager.cancel(run.id, actor="李工") == "cancelled"
    final = await _wait(run.id, ("cancelled",))
    assert final.end_reason == "stopped"
    [cancelled] = await _events(run.id, "run.cancelled")
    assert cancelled.data["reason"] == "stopped" and cancelled.data["actor"] == "李工"


async def test_a_run_still_queued_at_restart_gets_its_run_failed_event():
    """(h)：重启时排队中的运行改成 failed，以前只改状态，时间线里没有失败原因这一行。"""
    from app.core.bus import bus

    async with SessionLocal() as session:
        run = Run(workflow_name=f"排队-{uuid.uuid4().hex[:6]}", status="queued", graph={}, input={}, last_seq=2)
        session.add(run)
        await session.commit()
        run_id = run.id
    await run_manager.shutdown()
    bus._seq.pop(run_id, None)
    await run_manager.setup()
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
    assert row.status == "failed" and row.started_at is None and row.finished_at is None
    [failed] = await _events(run_id, "run.failed")
    assert failed.seq == 3 and failed.data == {"error": row.error}
