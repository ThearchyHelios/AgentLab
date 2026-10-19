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
    """等运行停到确认上、确认卡出来，按卡上的内容指纹确认。待办和审批同一个事务开出来，那一刻运行可能还没收成 interrupted、
    还在跑——这时去确认会被拒（「运行正在运行，没有待处理的审批」）：先等运行停下来，再取待办。"""
    await _wait(run_id, ("interrupted",))
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

    assert rec.id not in await actions.tick(utcnow())       # 还没到第一次查询的时候
    assert rec.id in await actions.tick(utcnow() + timedelta(minutes=2))
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


# --------------------------------------------------------------------------
# 参数按能力的参数定义收好类型（typed_args）：哈希、预览、发出去的都是收好的那一份
# --------------------------------------------------------------------------

PARAMS = {"type": "object", "required": ["amount"], "properties": {
    "amount": {"type": "number"}, "count": {"type": "integer"}, "urgent": {"type": "boolean"},
    "card": {"type": "string"}, "memo": {"type": "number"},
    "payee": {"type": "object", "properties": {"id": {"type": "integer"}, "vip": {"type": "boolean"}}},
    "splits": {"type": "array", "items": {"type": "number"}}}}


def _cap(schema=PARAMS):
    from types import SimpleNamespace

    return SimpleNamespace(name=PAY, title="付款", params_schema=schema)


def test_typed_args_follow_the_declared_types():
    out = actions.typed_args(_cap(), {
        "amount": "1280.5", "count": "3", "urgent": "true", "card": "6222", "extra": "原样",
        "payee": '{"id": "42", "vip": "false", "name": "钱七"}', "splits": ["1", 2.5], "memo": ""})
    assert out == {"amount": 1280.5, "count": 3, "urgent": True, "card": "6222", "extra": "原样",
                   "payee": {"id": 42, "vip": False, "name": "钱七"}, "splits": [1, 2.5]}
    assert actions.typed_args(_cap(), {"amount": "120"})["amount"] == 120 and \
        isinstance(actions.typed_args(_cap(), {"amount": "120"})["amount"], int)
    assert actions.typed_args(_cap(), {"amount": 7, "count": "4.0"}) == {"amount": 7, "count": 4}
    # 没登记参数定义的能力：原样不动，发出去的和以前一字不差
    assert actions.typed_args(_cap({}), {"amount": "1280.5"}) == {"amount": "1280.5"}


@pytest.mark.parametrize("args, where, want", [
    ({"amount": "abc"}, "amount", "应为数"),
    ({"amount": "nan"}, "amount", "应为数"),
    ({"amount": "1", "count": "2.5"}, "count", "应为整数"),
    ({"amount": "1", "urgent": "yes"}, "urgent", "应为是或否"),
    ({"amount": "1", "payee": {"id": "x"}}, "payee.id", "应为整数"),
    ({"amount": "1", "splits": ["1", "两元"]}, "splits[1]", "应为数"),
    ({"amount": "1", "payee": "{坏的"}, "payee", "应为对象（JSON）"),
    ({"amount": "  "}, "amount", "必填参数"),
])
def test_typed_args_refuse_what_cannot_be_the_declared_type(args, where, want):
    with pytest.raises(actions.ActionFailed) as refused:
        actions.typed_args(_cap(), args)
    assert f" {where} " in str(refused.value) and want in str(refused.value) and "没有发送" in str(refused.value)


async def test_a_template_amount_is_sent_as_a_number_and_the_hash_covers_what_is_sent(erp, capabilities):
    caps = capabilities(await _endpoint(test=True), lookup=False)
    caps[PAY].params_schema = {"type": "object", "properties": {"amount": {"type": "number"},
                                                                "card": {"type": "string"}}}
    run_id = await _formal(pay_graph(), amount=1280.5)
    task = await _task(run_id, "action_confirm")
    card = (await tasks.detail(task.id, ANN))["payload"]
    assert card["preview"] == {"amount": 1280.5, "card": "••••"}
    sent = {"amount": 1280.5, "card": "6222020200112233"}
    assert card["payload_hash"] == actions.payload_hash(PAY, sent)
    assert card["payload_hash"] != actions.payload_hash(PAY, {**sent, "amount": "1280.5"})
    await _confirm(run_id)
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    [request] = erp.writes("prod")
    assert request["body"] == sent and isinstance(request["body"]["amount"], float)
    [rec] = await _records(run_id)
    assert rec.payload_hash == card["payload_hash"] and rec.preview["amount"] == 1280.5


async def test_an_untyped_value_fails_the_node_without_sending(erp, capabilities):
    caps = capabilities(await _endpoint(test=True), lookup=False)
    caps[PAY].params_schema = {"type": "object", "properties": {"amount": {"type": "number"}}}
    run_id = await _formal(pay_graph(), amount="abc")
    final = await _wait(run_id, ("succeeded", "failed", "interrupted"))
    assert final.status == "failed" and "参数 amount 应为数" in final.error and "「abc」" in final.error
    assert erp.requests == [] and await _records(run_id) == []
    async with SessionLocal() as session:   # 收不成的在确认之前就停下：不开确认卡，看过的不会是发不出去的
        assert (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().all() == []


async def test_a_changed_params_schema_does_not_fail_a_write_that_already_went_out(erp, capabilities):
    """发出之后能力的参数定义改了（升级时停在结果未知的运行）：先查记录再收类型。结果未知的照旧挂起等核实，核实为已生效
    之后节点重新执行交回存下的回执——手上的「1,280」收不成新声明的数，也不把已经生效的写入判成失败。"""
    caps = capabilities(await _endpoint(test=True), lookup=True)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph(), amount="1,280")
    await _confirm(run_id)
    assert (await _wait(run_id, ("interrupted", "failed", "succeeded"))).status == "interrupted"
    [rec] = await _records(run_id)
    assert rec.state == "unknown" and rec.preview["amount"] == "1,280"
    caps[PAY].params_schema = {"type": "object", "properties": {"amount": {"type": "number"}}}
    args = {"amount": "1,280", "card": "6222020200112233"}
    with pytest.raises(actions.ActionPending):
        await actions.prepare(run_id=run_id, node_id="pay", exec_no=1, capability=caps[PAY], args=args)

    assert rec.id in await actions.tick(utcnow() + timedelta(minutes=2))
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert len(erp.writes()) == 1
    [rec] = await _records(run_id)
    assert rec.state == "confirmed"
    # 再执行一次（接着跑、重放）：交回生效那一次的回执，指纹、预览照记录，不按这一次的参数重算
    prep = await actions.prepare(run_id=run_id, node_id="pay", exec_no=1, capability=caps[PAY], args=args)
    assert prep.receipt is not None and prep.receipt["receipt"]["id"] == final.output["单号"]
    assert (prep.payload_hash, prep.preview["amount"], prep.attempt) == (rec.payload_hash, "1,280", 1)
    # 真要发的（新的一次执行）照旧按参数定义收类型：收不成的不发
    with pytest.raises(actions.ActionFailed, match="参数 amount 应为数"):
        await actions.prepare(run_id=run_id, node_id="pay", exec_no=2, capability=caps[PAY], args=args)
    assert len(erp.writes()) == 1


# --------------------------------------------------------------------------
# 实例页怎么说：驳回动作确认、结果未知、核实之后
# --------------------------------------------------------------------------


async def _instance_view(run_id: str) -> dict:
    """按一页实例页（概况、进度、付款的动作状态）解析这次运行：{组件 id: data}。"""
    from app.apps.resolve import resolve_page
    from app.db.models import App, AppVersion

    spec = {"format": "agentlab.app/1", "name": "付款", "pages": [{"id": "instance", "title": "实例", "body": [
        {"id": "hdr", "type": "instance_header"}, {"id": "tl", "type": "instance_timeline"},
        {"id": "act", "type": "action_status", "props": {"label": "付款"}, "bind": {"action": "pay"}}]}]}
    async with SessionLocal() as session:
        app = App(name="付款", published_version=1)
        session.add(app)
        await session.flush()
        version = AppVersion(app_id=app.id, version=1, spec=spec, spec_hash="x" * 64)
        session.add(version)
        await session.commit()
        run = await session.get(Run, run_id)
        vm = await resolve_page(session, app, page_id="instance", principal=ANN, role="manage", instance=run,
                                version=version)
    return {n["id"]: n.get("data") for n in vm["tree"]}


async def test_a_rejected_confirmation_reads_as_a_decision_not_a_fault(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=False)
    run_id = await _formal(pay_graph())
    task = await _task(run_id, "action_confirm")
    await tasks.respond(task.id, ANN, decision="reject", note="金额不对")
    final = await _wait(run_id, ("succeeded", "failed", "cancelled"))
    # 驳回是人的决定，不是故障（05-已知遗留 (a)）：运行收成 cancelled + end_reason=rejected，原话照旧写未确认（被驳回），
    # run.cancelled 带上在哪一步、谁、备注；实例页按人的决定说
    assert (final.status, final.end_reason) == ("cancelled", "rejected")
    assert final.error.startswith("「pay」未确认（被驳回），没有发送：金额不对")
    [ended] = await _events(run_id, "run.cancelled")
    assert {k: ended.data[k] for k in ("reason", "node_id", "kind", "decision", "note")} == {
        "reason": "rejected", "node_id": "pay", "kind": "action_confirm", "decision": "reject", "note": "金额不对"}
    assert not await _events(run_id, "run.failed")
    view = await _instance_view(run_id)
    assert (view["hdr"]["status_code"], view["hdr"]["status_text"]) == ("cancelled", "「pay」未确认（被驳回）")
    step = next(s for s in view["tl"]["steps"] if s["node"] == "pay")
    assert (step["state"], step["text"]) == ("rejected", "未确认（被驳回）")
    assert erp.requests == []


async def test_a_rejected_confirmation_with_a_reject_exit_sends_nothing_and_goes_on(erp, capabilities):
    """动作确认接了「驳回」出口（docs/design/orchestration-round/00-总方案.md §3）：驳回时照旧不发送写入、记「未确认（被驳回）」，
    这一步正常结束、沿驳回边往下走（这里是通知申请人），运行照它的路径结束；实例页写「未确认（被驳回），转到「通知申请人」」。"""
    capabilities(await _endpoint(test=True), lookup=False)
    graph = pay_graph()
    graph["nodes"].append(node("tell", "output", fields=[{"name": "结论", "value": "{{ nodes.pay.text }}"}]))
    graph["nodes"][-1]["data"]["label"] = "通知申请人"
    graph["edges"].append({"source": "pay", "target": "tell", "sourceHandle": "rejected"})
    run_id = await _formal(graph)
    task = await _task(run_id, "action_confirm")
    await tasks.respond(task.id, ANN, decision="reject", note="金额不对")
    final = await _wait(run_id, ("succeeded", "failed", "cancelled"))
    assert (final.status, final.end_reason) == ("succeeded", None), final.error
    assert final.output == {"结论": "「pay」未确认（被驳回），没有发送：金额不对"}
    assert erp.requests == [], "驳回之后走出口也不发送"
    assert await _records(run_id) == []
    [done] = [e for e in await _events(run_id, "node.finished") if e.node_id == "pay"]
    assert (done.data["outcome"], done.data["kind"], done.data["decision"]) == ("rejected", "action_confirm", "reject")
    assert not [e for e in await _events(run_id, "tool.start") if e.node_id == "pay"]
    view = await _instance_view(run_id)
    step = next(s for s in view["tl"]["steps"] if s["node"] == "pay")
    assert (step["state"], step["text"]) == ("rejected", "未确认（被驳回），转到「通知申请人」")


async def test_unknown_then_reconciled_reads_as_waiting_then_a_footnote(erp, capabilities):
    capabilities(await _endpoint(test=True), lookup=True)
    erp.mode = "timeout_after_write"
    run_id = await _formal(pay_graph())
    await _confirm(run_id)
    await _wait(run_id, ("interrupted",))
    view = await _instance_view(run_id)
    # 结果未知：在等核实，不是「等待审批」；付款这一步不是「完成」
    assert (view["hdr"]["status_code"], view["hdr"]["status_text"]) == ("waiting", "等待核实写入结果")
    step = next(s for s in view["tl"]["steps"] if s["node"] == "pay")
    assert (step["state"], step["text"]) == ("unknown", "结果未知，等待核实")
    [row] = view["act"]["records"]
    assert row["state"] == "unknown" and "error" not in row and "ReadTimeout" in row["note"]

    assert row["id"] in await actions.tick(utcnow() + timedelta(minutes=2))
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    view = await _instance_view(run_id)
    assert view["hdr"]["status_text"] == "已完成"
    assert next(s for s in view["tl"]["steps"] if s["node"] == "pay")["state"] == "done"
    [row] = view["act"]["records"]
    # 核实为已生效之后：第一次发送超时只作历史记一笔，不再当失败原因
    assert row["state"] == "confirmed" and "error" not in row
    assert "ReadTimeout" in row["note"] and "按幂等键查询核实为已生效" in row["note"]
