"""动作：调用工具节点写外部系统（app/process/actions.py，设计文档 §3.8、§7.2）。

用假外部系统（tests/fakes/fake_erp.py）数「它收到了几次写请求」：
- 确认卡是遮罩后的预览 + 内容指纹；确认之后上游变了，指纹对不上，拒发；
- 写成功之后超时 → 结果未知，运行挂起（不封存）；登记了查询能力的自动核实为已生效、接着跑，外部系统只收到 1 次；
- 没有查询能力的开核实待办，选「未生效，重新发送」，第 2 次的幂等键带 :2；
- 探索运行、端点没有测试环境 → 演练，外部系统收到 0 次；
- 发出之后进程没了 → 重启时收成结果未知；
- 4xx 确定失败，接着跑也不重发。
能力登记和平台执行器是治理底座包的，这里用替身（get_capability / endpoint_request）。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import Principal
from app.db.base import SessionLocal, utcnow
from app.db.models import ActionRecord, Run, RunEvent, Task
from app.engine.runner import ContinueRefused, run_manager
from app.process import actions, tasks
from tests.fakes.fake_erp import PAY, FakeErp, endpoint_row, install


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


@pytest.fixture
def capabilities(monkeypatch, erp):
    """登记付款（写入，遮罩卡号）和按幂等键查付款两个能力；参数是端点、要不要查询能力。"""
    return lambda endpoint_id, *, lookup: install(monkeypatch, erp, endpoint_id, lookup=lookup)


async def _endpoint(*, test: bool) -> str:
    return await endpoint_row(test=test)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def pay_graph(amount: str = "{{ input.amount }}") -> dict:
    nodes = [node("start", "input", fields=[{"name": "amount"}]),
             node("pay", "tool", tool=PAY, args={"amount": amount, "card": "6222020200112233"}),
             node("out", "output", fields=[{"name": "单号", "value": "{{ nodes.pay.receipt.id }}"}])]
    return {"nodes": nodes, "edges": [{"source": "start", "target": "pay"}, {"source": "pay", "target": "out"}]}


ANN = Principal(username="ann", role="admin", mode="off")


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(run_id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _task(run_id: str, kind: str) -> Task:
    for _ in range(100):
        async with SessionLocal() as session:
            row = (await session.execute(select(Task).where(Task.run_id == run_id, Task.kind == kind,
                                                            Task.status == "pending"))).scalars().first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"没有开出 {kind} 待办")


async def _records(run_id: str) -> list[ActionRecord]:
    async with SessionLocal() as session:
        return list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == run_id)
                                           .order_by(ActionRecord.attempt))).scalars())


async def _events(run_id: str, kind: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == kind)
                                           .order_by(RunEvent.seq))).scalars())


async def _confirm(run_id: str) -> Task:
    """等确认卡出来，按卡上的内容指纹确认。"""
    task = await _task(run_id, "action_confirm")
    card = (await tasks.detail(task.id, ANN))["payload"]
    out = await tasks.respond(task.id, ANN, decision="approve", payload_hash=card["payload_hash"])
    assert out["resolved"] is True
    return task


async def _formal(graph: dict, **inputs) -> str:
    run = await run_manager.start(graph=graph, input_payload=inputs or {"amount": 120}, run_class="formal")
    await _wait(run.id, ("interrupted", "failed", "succeeded"))
    return run.id


async def test_confirm_card_is_masked_and_sends_exactly_once(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    run_id = await _formal(pay_graph())
    task = await _task(run_id, "action_confirm")
    card = (await tasks.detail(task.id, ANN))["payload"]
    assert card["kind"] == "action_confirm" and card["environment"] == "prod"
    assert card["preview"] == {"amount": "120", "card": "••••"}
    assert card["payload_hash"] == actions.payload_hash(PAY, {"amount": "120", "card": "6222020200112233"})
    assert erp.requests == []                      # 确认之前一次都不发

    with pytest.raises(tasks.TaskRefused) as wrong:
        await tasks.respond(task.id, ANN, decision="approve", payload_hash="0" * 64)
    assert wrong.value.status_code == 422 and wrong.value.code == "payload_hash_mismatch"

    await _confirm(run_id)
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert final.output["单号"].startswith("PAY-")
    assert len(erp.writes("prod")) == 1
    [rec] = await _records(run_id)
    assert rec.state == "confirmed" and rec.attempt == 1 and rec.idem_key == f"{rec.action_key}:1"
    assert rec.preview["card"] == "••••" and rec.confirm_task_id == task.id and rec.receipt["artifact"]
    assert erp.requests[0]["idem_key"] == rec.idem_key
    kinds = [e.type for e in await _events(run_id, "tool.start")]
    assert len(kinds) == 1
    started = (await _events(run_id, "tool.start"))[0].data
    assert started["action_key"] == rec.action_key and started["args"]["card"] == "••••"
    for kind in ("action.prepared", "action.sent", "action.confirmed"):
        assert len(await _events(run_id, kind)) == 1, kind


async def test_changed_upstream_after_confirm_refuses_to_send(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    run_id = await _formal(pay_graph())
    await _task(run_id, "action_confirm")
    async with SessionLocal() as session:          # 确认之后上游变了：节点恢复时重新渲染出来的参数不一样
        run = await session.get(Run, run_id)
        graph = dict(run.graph)
        graph["nodes"] = [dict(n) for n in graph["nodes"]]
        pay = next(n for n in graph["nodes"] if n["id"] == "pay")
        pay["data"] = {**pay["data"], "config": {**pay["data"]["config"], "args": {"amount": "999", "card": "x"}}}
        run.graph = graph
        await session.commit()
    await _confirm(run_id)
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "failed" and "内容指纹对不上" in final.error
    assert erp.requests == [] and await _records(run_id) == []


async def test_unknown_result_with_lookup_is_reconciled_automatically_and_sent_once(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=True)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    held = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert held.status == "interrupted" and held.manifest_hash is None
    [rec] = await _records(run_id)
    assert rec.state == "unknown" and rec.next_lookup_at is not None
    assert len(await _events(run_id, "action.unknown")) == 1
    with pytest.raises(ContinueRefused) as refused:
        await run_manager.continue_failed(run_id)
    assert refused.value.code == "action_unresolved"

    assert await actions.tick(utcnow()) == 0       # 还没到第一次查询的时候
    assert await actions.tick(utcnow() + timedelta(minutes=2)) == 1
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert len(erp.writes()) == 1 and len(erp.lookups) == 1
    [rec] = await _records(run_id)
    assert rec.state == "confirmed" and rec.resolution["how"] == "lookup"
    assert final.output["单号"] == erp.payments[("prod", rec.idem_key)]["id"]
    assert len(await _events(run_id, "action.reconciled")) == 1


async def test_without_lookup_manual_resend_uses_attempt_two(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    assert (await _wait(run_id, ("interrupted", "failed", "succeeded"))).status == "interrupted"
    task = await _task(run_id, "reconcile")
    detail = await tasks.detail(task.id, ANN)
    assert detail["payload"]["options"] == ["confirmed", "not_applied_resend", "not_applied_abandon"]
    assert detail["payload"]["preview"]["card"] == "••••"

    erp.mode = "ok"
    out = await tasks.respond(task.id, ANN, decision="not_applied_resend", note="对方说没收到")
    assert out["resolved"] is True
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    first, second = await _records(run_id)
    assert (first.state, first.resolution["outcome"]) == ("failed", "resend")
    assert second.state == "confirmed" and second.idem_key.endswith(":2")
    assert [r["idem_key"].rsplit(":", 1)[1] for r in erp.writes()] == ["1", "2"]
    assert (await tasks.detail(task.id, ANN))["status"] == "resolved"
    async with SessionLocal() as session:   # 接着跑时确认的答复取自断点：内容指纹没变，不再要一次确认
        confirms = (await session.execute(select(Task).where(Task.run_id == run_id,
                                                             Task.kind == "action_confirm"))).scalars().all()
    assert len(confirms) == 1


async def test_manual_confirm_hands_back_the_external_id_without_sending(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    await _wait(run_id, ("interrupted",))
    task = await _task(run_id, "reconcile")
    await tasks.respond(task.id, ANN, decision="confirmed", values={"external_id": "PAY-777"})
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert len(erp.writes()) == 1


async def test_exploratory_run_without_test_environment_is_simulated(erp, capabilities):
    capabilities(await _endpoint(test=False), lookup=False)
    run = await run_manager.start(graph=pay_graph(), input_payload={"amount": 5})
    task = await _task(run.id, "action_confirm")
    assert (await tasks.detail(task.id, ANN))["payload"]["environment"] == "simulated"
    await _confirm(run.id)
    final = await _wait(run.id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert erp.requests == []
    [rec] = await _records(run.id)
    assert rec.state == "simulated" and rec.receipt["simulated"] is True
    assert len(await _events(run.id, "action.simulated")) == 1


async def test_exploratory_run_with_test_environment_writes_to_test(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    run = await run_manager.start(graph=pay_graph(), input_payload={"amount": 5})
    await _confirm(run.id)
    assert (await _wait(run.id, ("succeeded", "failed"))).status == "succeeded"
    assert len(erp.writes("test")) == 1 and erp.writes("prod") == []


async def test_a_4xx_fails_and_continuing_does_not_resend(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    erp.mode = "error_4xx"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    final = await _wait(run_id, ("succeeded", "failed", "interrupted"))
    assert final.status == "failed" and "HTTP 422" in final.error
    erp.mode = "ok"
    await run_manager.continue_failed(run_id)
    again = await _wait(run_id, ("succeeded", "failed"))
    assert again.status == "failed" and "不会自动重发" in again.error
    assert len(erp.writes()) == 1


async def test_sent_then_restart_becomes_unknown(capabilities, monkeypatch):
    """请求发出去、还没回话时进程没了：重启后记录是 sent，收成结果未知、开核实待办，绝不当成没发过。"""
    from app.capabilities import registry

    capabilities(await _endpoint(test=True), lookup=False)
    gate = asyncio.Event()
    sent = asyncio.Event()

    async def hang(*args, **kwargs):
        sent.set()
        await gate.wait()

    monkeypatch.setattr(registry, "endpoint_request", hang)
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    await asyncio.wait_for(sent.wait(), 10)
    await run_manager.shutdown()                   # 关停：执行任务被取消，运行挂起
    [rec] = await _records(run_id)
    assert rec.state == "sent"
    await run_manager.setup()
    # 启动整理按全库数：同一个测试进程里别的测试留下的结果未知（还没核实）也在里面，这里只看这一次运行的那条
    assert await actions.startup() >= 1
    [rec] = await _records(run_id)
    assert rec.state == "unknown"
    await _task(run_id, "reconcile")
    assert len(await _events(run_id, "action.unknown")) == 1
    assert await actions.startup() >= 1 and len(await _events(run_id, "action.unknown")) == 1   # 不重复安排


async def test_continue_api_refuses_while_unresolved(erp, capabilities):
    from app.main import app

    capabilities(await _endpoint(test=True), lookup=False)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    await _wait(run_id, ("interrupted",))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/runs/{run_id}/continue", json={})
        assert resp.status_code == 409 and resp.json()["code"] == "action_unresolved"
        listed = (await client.get(f"/api/actions?run_id={run_id}")).json()["items"]
    assert [r["state"] for r in listed] == ["unknown"] and listed[0]["preview"]["card"] == "••••"


def test_schedule_overlap_says_the_run_is_waiting_for_reconciliation():
    from app.scheduler.service import overlap_reason

    assert "核实" in overlap_reason("abcdef123", "interrupted", False, True)
    assert "服务重启" in overlap_reason("abcdef123", "interrupted", False)


def test_masking_by_json_pointer():
    doc = {"a": {"b": "secret", "c": [1, "x"]}, "d~e": 1, "f/g": 2}
    out = actions.masked(doc, ["/a/b", "/a/c/1", "/d~0e", "/f~1g", "/missing/x"])
    assert out == {"a": {"b": "••••", "c": [1, "••••"]}, "d~e": "••••", "f/g": "••••"}
    assert doc["a"]["b"] == "secret"
