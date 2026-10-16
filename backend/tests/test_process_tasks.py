"""待办服务（app/process/tasks.py）：一次中断 = 一条 Approval（给引擎）+ 一条 Task（给人）。

会签按策略在待办服务这一层汇总，凑齐了才恢复一次运行（引擎、重放都不改）；时限到了提醒、升级、驳回，永不自动批准；
老接口（/api/approvals/{id}/decide、/api/runs/{id}/resume）改成垫片，请求体照旧；存量的待审批启动时补建待办。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api.deps import Principal
from app.db.base import SessionLocal, utcnow
from app.db.models import (
    App, Approval, AppVersion, Group, GroupMember, Run, RunEvent, Task, TaskAssignment, User,
)
from app.engine.runner import run_manager
from app.process import tasks
from app.process.tasks import TaskRefused


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


def approval_flow(**human):
    nodes = [node("start", "input", fields=[{"name": "q"}, {"name": "manager"}]),
             node("review", "human", mode=human.pop("mode", "approve"), title="审一下", **human),
             node("ok", "output", fields=[{"name": "结论", "value": "通过"}]),
             node("no", "output", fields=[{"name": "结论", "value": "退回"}])]
    edges = [{"source": "start", "target": "review"},
             {"source": "review", "target": "ok", "sourceHandle": "approved"},
             {"source": "review", "target": "no", "sourceHandle": "rejected"}]
    return {"nodes": nodes, "edges": edges}


def off(name: str | None) -> Principal:
    return Principal(username=name, role="admin", mode="off")


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _task(run_id: str) -> Task:
    for _ in range(100):
        async with SessionLocal() as session:
            row = (await session.execute(select(Task).where(Task.run_id == run_id, Task.status == "pending")
                                         .order_by(Task.created_at.desc()))).scalars().first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("没有开出待办")


async def _events(run_id: str, kind: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == kind)
                                           .order_by(RunEvent.seq))).scalars())


async def _people(*names: str, group: str | None = None) -> None:
    async with SessionLocal() as session:
        users = []
        for name in names:
            user = User(username=name, role="user")
            session.add(user)
            users.append(user)
        await session.flush()
        if group:
            g = Group(name=group)
            session.add(g)
            await session.flush()
            for user in users:
                session.add(GroupMember(group_id=g.id, user_id=user.id))
        await session.commit()


async def _app(slots: dict) -> tuple[str, int]:
    async with SessionLocal() as session:
        app = App(name=f"报销-{uuid.uuid4().hex[:6]}", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec={"format": "agentlab.app/1"}, spec_hash="x" * 64,
                               bindings={"slots": slots}))
        await session.commit()
        return app.id, 1


async def _app_run(graph: dict, slots: dict, **inputs) -> str:
    app_id, version = await _app(slots)
    run = await run_manager.start(graph=graph, input_payload={"q": "x", **inputs}, app_id=app_id,
                                  app_version=version, started_by="dave")
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    return run.id


def _u() -> str:
    return uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------
# 打开
# --------------------------------------------------------------------------


async def test_an_interrupt_opens_one_task_assigned_to_anyone_without_an_app():
    run = await run_manager.start(graph=approval_flow(), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    assert task.kind == "approve" and task.policy == {"kind": "any"} and task.app_id is None
    async with SessionLocal() as session:
        approval = await session.get(Approval, task.approval_id)
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id)))
                    .scalars())
    assert approval.run_id == run.id
    assert [(a.principal_kind, a.principal) for a in rows] == [("anyone", "")]
    opened = await _events(run.id, "task.opened")
    assert len(opened) == 1 and opened[0].data["task_id"] == task.id


async def test_task_kind_follows_the_interrupt_kind_not_approval_mode():
    graph = {"nodes": [node("start", "input", fields=[{"name": "q"}]),
                       node("t", "tool", tool="current_time", args={}, approval="always"),
                       node("out", "output", fields=[])],
             "edges": [{"source": "start", "target": "t"}, {"source": "t", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    assert task.kind == "tool"
    out = await tasks.respond(task.id, off("ann"), decision="approve")
    assert out["resolved"] is True
    assert (await _wait(run.id, ("succeeded", "failed"))).status == "succeeded"


async def test_slot_expands_group_members_at_open_time():
    a, b, g = f"a{_u()}", f"b{_u()}", f"finance{_u()}"
    await _people(a, b, group=g)
    run_id = await _app_run(approval_flow(assignee={"kind": "slot", "slot": "finance"}, policy="all"),
                            {"finance": [{"kind": "group", "name": g}]})
    task = await _task(run_id)
    async with SessionLocal() as session:
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id)))
                    .scalars())
    assert sorted((r.principal_kind, r.principal, r.source) for r in rows) == [
        ("user", a, "slot:finance"), ("user", b, "slot:finance")]
    assert task.policy == {"kind": "all"} and task.app_id is not None


async def test_initiator_and_expression_assignees():
    run_id = await _app_run(approval_flow(assignee={"kind": "initiator"}), {})
    task = await _task(run_id)
    detail = await tasks.detail(task.id, off(None))
    assert [(x["principal_kind"], x["principal"]) for x in detail["assignments"]] == [("user", "dave")]

    who = f"m{_u()}"
    run_id = await _app_run(approval_flow(assignee={"kind": "expression", "expr": "input.manager"}), {}, manager=who)
    detail = await tasks.detail((await _task(run_id)).id, off(None))
    assert [(x["principal_kind"], x["principal"], x["source"]) for x in detail["assignments"]] == [
        ("user", who, "expression")]


# --------------------------------------------------------------------------
# 会签
# --------------------------------------------------------------------------


async def test_all_policy_waits_for_everyone_then_resumes_once_with_signoffs():
    a, b, g = f"a{_u()}", f"b{_u()}", f"finance{_u()}"
    await _people(a, b, group=g)
    run_id = await _app_run(approval_flow(assignee={"kind": "slot", "slot": "finance"}, policy="all"),
                            {"finance": [{"kind": "group", "name": g}]})
    task = await _task(run_id)

    first = await tasks.respond(task.id, off(a), decision="approve", note="票据齐全")
    assert first["resolved"] is False
    assert (await _wait(run_id, ("interrupted",))).status == "interrupted"
    with pytest.raises(TaskRefused) as again:
        await tasks.respond(task.id, off(a), decision="approve")
    assert again.value.status_code == 409

    second = await tasks.respond(task.id, off(b), decision="approve")
    assert second["resolved"] is True
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.status == "succeeded" and final.output.get("结论") == "通过"
    resolved = [e for e in await _events(run_id, "human.resolved") if e.node_id == "review"]
    assert len(resolved) == 1
    signoffs = resolved[0].data["response"]["signoffs"]
    assert [(s["by"], s["decision"]) for s in signoffs] == [(a, "approve"), (b, "approve")]
    responded = await _events(run_id, "task.responded")
    assert [(e.data["index"], e.data["of"]) for e in responded] == [(1, 2), (2, 2)]
    assert len(await _events(run_id, "run.resumed")) == 1


async def test_all_policy_one_veto_rejects_immediately():
    a, b, g = f"a{_u()}", f"b{_u()}", f"finance{_u()}"
    await _people(a, b, group=g)
    run_id = await _app_run(approval_flow(assignee={"kind": "slot", "slot": "finance"}, policy="all"),
                            {"finance": [{"kind": "group", "name": g}]})
    task = await _task(run_id)
    out = await tasks.respond(task.id, off(b), decision="reject", note="金额超标")
    assert out["resolved"] is True
    final = await _wait(run_id, ("succeeded", "failed"))
    assert final.output.get("结论") == "退回"
    detail = await tasks.detail(task.id, off(None))
    assert detail["status"] == "resolved"
    assert sorted(x["status"] for x in detail["assignments"]) == ["responded", "withdrawn"]


async def test_quorum_two_of_three():
    a, b, c, g = f"a{_u()}", f"b{_u()}", f"c{_u()}", f"finance{_u()}"
    await _people(a, b, c, group=g)
    run_id = await _app_run(approval_flow(assignee={"kind": "slot", "slot": "finance"}, policy={"quorum": 2}),
                            {"finance": [{"kind": "group", "name": g}]})
    task = await _task(run_id)
    assert (await tasks.respond(task.id, off(a), decision="reject"))["resolved"] is False
    assert (await tasks.respond(task.id, off(b), decision="approve"))["resolved"] is False
    assert (await tasks.respond(task.id, off(c), decision="approve"))["resolved"] is True
    assert (await _wait(run_id, ("succeeded", "failed"))).output.get("结论") == "通过"


async def test_login_on_only_assignees_can_respond():
    a, g = f"a{_u()}", f"finance{_u()}"
    await _people(a, group=g)
    run_id = await _app_run(approval_flow(assignee={"kind": "slot", "slot": "finance"}),
                            {"finance": [{"kind": "group", "name": g}]})
    task = await _task(run_id)
    stranger = Principal(username="eve", role="user", mode="local", verified=True)
    me = Principal(username=a, role="user", mode="local", verified=True)
    assert (await tasks.detail(task.id, stranger))["can_respond"] is False
    assert task.id not in [t["id"] for t in await tasks.list_tasks(stranger)]
    assert task.id in [t["id"] for t in await tasks.list_tasks(me)]
    with pytest.raises(TaskRefused) as refused:
        await tasks.respond(task.id, stranger, decision="approve")
    assert refused.value.status_code == 403
    out = await tasks.respond(task.id, me, decision="approve")
    assert out["resolved"] is True
    rows = out["task"]["assignments"]
    assert rows[0]["responded_by"] == a and rows[0]["verified"] is True


# --------------------------------------------------------------------------
# 时限：提醒、升级、超时驳回
# --------------------------------------------------------------------------


@pytest.fixture
async def stray_due_task():
    """别的测试留下的、早就到期、可升级的待办（-n 8 下同一个库里常有）。tick 扫全库，按条数断言的测试会被它带偏：
    这里先放一条，测试只断言自己造的那条在不在 tick 交回的 id 里（05-已知遗留 (f)）。"""
    async with SessionLocal() as session:
        run = Run(workflow_name="别的测试留下的", status="interrupted", graph={}, input={})
        session.add(run)
        await session.flush()
        task = Task(run_id=run.id, node_id="x", kind="approve", title="残留", due_at=utcnow() - timedelta(days=1),
                    on_timeout={"action": "escalate", "to_slot": "nobody"})
        session.add(task)
        await session.commit()
        yield task.id
        await session.execute(delete(Run).where(Run.id == run.id))
        await session.commit()


async def test_escalation_adds_the_director_whose_answer_decides(stray_due_task):
    a, b, g, boss = f"a{_u()}", f"b{_u()}", f"finance{_u()}", f"boss{_u()}"
    await _people(a, b, group=g)
    run_id = await _app_run(
        approval_flow(assignee={"kind": "slot", "slot": "finance"}, policy="all", due="PT1H",
                      on_timeout={"action": "escalate", "to_slot": "director"}),
        {"finance": [{"kind": "group", "name": g}], "director": [{"kind": "user", "name": boss}]})
    task = await _task(run_id)
    assert task.due_at is not None
    assert task.id not in await tasks.tick(utcnow())          # 还没到时限
    assert task.id in await tasks.tick(utcnow() + timedelta(hours=2))
    assert task.id not in await tasks.tick(utcnow() + timedelta(hours=3))   # 只升一次
    detail = await tasks.detail(task.id, off(None))
    assert detail["escalation_level"] == 1 and detail["status"] == "pending"
    assert ("user", boss, "escalation") in [(x["principal_kind"], x["principal"], x["source"])
                                            for x in detail["assignments"]]
    assert len(await _events(run_id, "task.escalated")) == 1
    out = await tasks.respond(task.id, off(boss), decision="approve")
    assert out["resolved"] is True
    assert (await _wait(run_id, ("succeeded", "failed"))).output.get("结论") == "通过"


async def test_timeout_reject_never_approves(stray_due_task):
    run = await run_manager.start(
        graph=approval_flow(due="PT30M", on_timeout={"action": "reject"}), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    task = await _task(run.id)
    assert task.id in await tasks.tick(utcnow() + timedelta(hours=1))
    final = await _wait(run.id, ("succeeded", "failed"))
    assert final.output.get("结论") == "退回"
    assert len(await _events(run.id, "task.expired")) == 1
    async with SessionLocal() as session:
        row = await session.get(Task, task.id)
    assert row.status == "resolved" and row.outcome["decision"] == "expired_reject"


async def test_remind_repeats_every_interval(stray_due_task):
    run = await run_manager.start(
        graph=approval_flow(due="PT1H", on_timeout={"action": "remind", "every": "PT1H"}), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    task = await _task(run.id)
    later = utcnow() + timedelta(hours=1, minutes=1)
    assert task.id in await tasks.tick(later)
    assert task.id not in await tasks.tick(later)
    assert task.id in await tasks.tick(later + timedelta(hours=1))
    assert len(await _events(run.id, "task.reminded")) == 2
    async with SessionLocal() as session:
        assert (await session.get(Task, task.id)).status == "pending"


# --------------------------------------------------------------------------
# 表单
# --------------------------------------------------------------------------


async def test_form_values_are_validated_and_reach_the_node(monkeypatch, client):
    from app.apps import forms

    monkeypatch.setattr(forms, "validate_form_schema", lambda form: [])
    monkeypatch.setattr(forms, "validate_form", lambda form, values: (
        [] if isinstance(values.get("amount"), (int, float)) else
        [{"path": "amount", "code": "type", "message": "金额需要是数字"}]))
    form = {"format": "agentlab.form/1", "type": "object", "required": ["amount"],
            "properties": {"amount": {"type": "number", "title": "金额"}}}
    graph = {"nodes": [node("start", "input", fields=[{"name": "q"}]),
                       node("fill", "human", mode="input", title="补金额", form=form),
                       node("out", "output", fields=[{"name": "金额", "value": "{{ nodes.fill.value.amount }}"}])],
             "edges": [{"source": "start", "target": "fill"}, {"source": "fill", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    assert task.kind == "input" and task.form == form

    bad = await client.post(f"/api/tasks/{task.id}/respond", json={"decision": "submit", "values": {"amount": "多"}})
    assert bad.status_code == 422 and bad.json()["code"] == "form_invalid"
    assert bad.json()["errors"] == [{"path": "amount", "code": "type", "message": "金额需要是数字"}]
    good = await client.post(f"/api/tasks/{task.id}/respond", json={"decision": "submit", "values": {"amount": 12}})
    assert good.status_code == 200 and good.json()["resolved"] is True
    assert (await _wait(run.id, ("succeeded", "failed"))).output.get("金额") == "12"


async def test_form_from_falls_back_to_the_static_form_with_a_warning(monkeypatch):
    from app.apps import forms

    static = {"format": "agentlab.form/1", "type": "object", "properties": {"note": {"type": "string"}}}
    monkeypatch.setattr(forms, "validate_form_schema",
                        lambda form: [] if form is static or form == static else ["用了不支持的 pattern"])
    graph = {"nodes": [node("start", "input", fields=[{"name": "q"}]),
                       node("ask", "transform", mode="expression",
                            expression="{'type': 'object', 'properties': {'x': {'type': 'string', 'pattern': 'a+'}}}",
                            assign_to="asked"),
                       node("fill", "human", mode="input", title="补充", form=static, form_from="vars.asked"),
                       node("out", "output", fields=[])],
             "edges": [{"source": "start", "target": "ask"}, {"source": "ask", "target": "fill"},
                       {"source": "fill", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    assert task.form == static
    warned = [e for e in await _events(run.id, "log") if e.data.get("code") == "form_from_invalid"]
    assert len(warned) == 1 and "pattern" in warned[0].data["message"]


# --------------------------------------------------------------------------
# 老接口：垫片
# --------------------------------------------------------------------------


async def test_decide_shim_still_works_and_goes_through_the_task(client):
    run = await run_manager.start(graph=approval_flow(), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    resp = await client.post(f"/api/approvals/{task.approval_id}/decide", json={"approved": False, "note": "不行"},
                             headers={"X-Actor": "zhang"})
    assert resp.status_code == 200, resp.text
    assert (await _wait(run.id, ("succeeded", "failed"))).output.get("结论") == "退回"
    detail = await tasks.detail(task.id, off(None))
    assert detail["status"] == "resolved" and detail["assignments"][0]["responded_by"] == "zhang"
    async with SessionLocal() as session:
        assert (await session.get(Approval, task.approval_id)).resolved_by == "zhang"


async def test_resume_shim_passes_the_raw_response_through(client):
    graph = {"nodes": [node("start", "input", fields=[{"name": "q"}]),
                       node("fill", "human", mode="input", title="写一句"),
                       node("out", "output", fields=[{"name": "回复", "value": "{{ nodes.fill.value.choices.m1 }}"}])],
             "edges": [{"source": "start", "target": "fill"}, {"source": "fill", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    # 双盲裁决那种回复：value 里是 {choices, note}，原样交给节点
    resp = await client.post(f"/api/runs/{run.id}/resume", json={
        "response": {"approved": True, "note": "用 B", "value": {"choices": {"m1": "b"}, "note": "用 B"}},
        "approval_id": task.approval_id})
    assert resp.status_code == 200, resp.text
    assert (await _wait(run.id, ("succeeded", "failed"))).output.get("回复") == "b"
    assert (await tasks.detail(task.id, off(None)))["status"] == "resolved"


async def test_startup_backfills_tasks_for_old_pending_approvals():
    run = await run_manager.start(graph=approval_flow(assignee={"kind": "initiator"}), input_payload={"q": "x"},
                                  started_by="dave")
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    async with SessionLocal() as session:   # 模拟升级前的存量：只有审批，没有待办
        await session.execute(delete(Task).where(Task.id == task.id))
        await session.commit()
    assert await tasks.startup() >= 1
    again = await _task(run.id)
    assert again.approval_id == task.approval_id
    assert await tasks.startup() == 0          # 幂等
    out = await tasks.respond(again.id, off("z"), decision="approve")
    assert out["resolved"] is True


async def test_cancelling_the_run_closes_its_tasks():
    run = await run_manager.start(graph=approval_flow(), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    task = await _task(run.id)
    assert await run_manager.cancel(run.id) == "cancelled"
    detail = await tasks.detail(task.id, off(None))
    assert detail["status"] == "cancelled" and detail["can_respond"] is False


async def test_count_and_list_api(client):
    run = await run_manager.start(graph=approval_flow(), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _task(run.id)
    count = (await client.get("/api/tasks/count?mine=1")).json()
    assert set(count) == {"mine", "apps", "other", "latest"} and count["other"] >= 1 and count["latest"]
    listed = (await client.get(f"/api/tasks?assigned=me&run_id={run.id}")).json()["items"]
    assert [t["id"] for t in listed] == [task.id]
    item = listed[0]
    assert item["payload"]["kind"] == "human_node" and item["can_respond"] is True
    assert item["node_label"] == "review"
    missing = await client.post(f"/api/tasks/{task.id}/respond", json={"decision": "maybe"})
    assert missing.status_code == 422


async def test_reassign_and_remind(client):
    run_id = await _app_run(approval_flow(assignee={"kind": "initiator"}), {})
    task = await _task(run_id)
    resp = await client.post(f"/api/tasks/{task.id}/reassign", json={"to": [{"kind": "user", "name": "frank"}],
                                                                      "note": "dave 休假"})
    assert resp.status_code == 200, resp.text
    rows = {(a["principal"], a["status"], a["source"]) for a in resp.json()["assignments"]}
    assert rows == {("dave", "withdrawn", "initiator"), ("frank", "pending", "reassign")}
    assert len(await _events(run_id, "task.reassigned")) == 1
    frank = Principal(username="frank", role="user", mode="local", verified=True)
    dave = Principal(username="dave", role="user", mode="local", verified=True)
    assert (await tasks.detail(task.id, frank))["can_respond"] is True
    assert (await tasks.detail(task.id, dave))["can_respond"] is False
    with pytest.raises(TaskRefused) as refused:
        await tasks.reassign(task.id, dave, to=[{"kind": "user", "name": "dave"}])
    assert refused.value.status_code == 403            # 开了登录，user 角色不能改派
    resp = await client.post(f"/api/tasks/{task.id}/remind")
    assert resp.status_code == 200 and resp.json() == {"sent": 0}       # 没有订阅的渠道
    assert len(await _events(run_id, "task.reminded")) == 1
    empty = await client.post(f"/api/tasks/{task.id}/reassign", json={"to": []})
    assert empty.status_code == 422


async def test_user_role_only_sees_tasks_assigned_to_them():
    run_id = await _app_run(approval_flow(assignee={"kind": "initiator"}), {})
    task = await _task(run_id)
    item = await tasks.detail(task.id, off(None))
    assert tasks.visible(item, Principal(username="dave", role="user", mode="local"))
    assert not tasks.visible(item, Principal(username="eve", role="user", mode="local"))
    assert tasks.visible(item, Principal(username="eve", role="builder", mode="local"))
    assert tasks.visible(item, off(None))
