"""数据事件触发（docs/design/orchestration-round/00-总方案.md §4；scheduler/triggers.py、scheduler/outbox.py）。

- 三种事件在发生处各记一条待派发事件，调度的一拍取走、派给合条件的事件触发任务、发起一次正式运行：
  上传表格按期累积的新一期启用（data/table_versions）、正式运行成功结束（engine/runner 收尾）、探查 / 剖析新记了要处理的变化
  （data/source_changes.notify_after_job，一个任务一条）；探索运行、失败的运行不算；
- 输入模板里的 {{ event.* }} 按事件渲染（上游的出口字段、这一期、变化的条数）；触发记录 kind=event、inputs 带事件的说明；
  运行来源 origin_kind=schedule，origin.trigger 带事件种类、对象、事件 id、触发深度；
- 去抖：同一个任务 debounce_s 内的几次事件合成一次，内容取最新的那一条，fire_at 是窗口结束的时刻；窗口外的另起一次；
- 上一次还没结束照 overlap=skip 记 skipped_overlap；触发链超过 5 层不发起、照实记；派发得太晚（超出补发宽限）记 skipped_misfire；
- 任务停用、改了触发条件时等着的不再派发；调度关着（没有这一拍）时事件留在表里；
- 存量的 cron 任务一字不变（老测试照过，见 test_scheduler_loop.py 等）。

工作流、表格、数据源、字段都是虚构的。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Run, Schedule, ScheduleFire, Workflow, WorkflowVersion
from app.db.models_sources import SourceChange
from app.db.models_triggers import TriggerDelivery, TriggerEvent
from app.engine.launch import launch_run
from app.engine.runner import run_manager
from app.scheduler import outbox, service, triggers

UTC = timezone.utc


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(TriggerDelivery))
        await session.execute(delete(TriggerEvent))
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(fields: list[dict], value: str) -> dict:
    return {"nodes": [node("start", "input", fields=fields),
                      node("out", "output", fields=[{"name": "r", "value": value}, {"name": "rows", "value": "12"}])],
            "edges": [{"source": "start", "target": "out"}]}


async def workflow(name: str, *, fields: list[dict] | None = None, value: str = "完成") -> str:
    fields = fields if fields is not None else []
    async with SessionLocal() as session:
        wf = Workflow(name=f"{name}-{uuid.uuid4().hex[:6]}", graph=graph(fields, value), status="published",
                      published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph(fields, value)))
        await session.commit()
        return wf.id


async def event_schedule(workflow_id: str, trigger: dict, *, template: dict | None = None, enabled: bool = True,
                         grace: int = 3600, name: str = "下游汇总") -> str:
    async with SessionLocal() as session:
        row = Schedule(name=name, workflow_id=workflow_id, target_kind="workflow", target_id=workflow_id, cron="",
                       timezone="Asia/Shanghai", run_class="formal", input_template=template or {}, enabled=enabled,
                       misfire_grace_s=grace, next_run_at=None, trigger_kind="event",
                       trigger=triggers.normalize(trigger))
        session.add(row)
        await session.commit()
        return row.id


async def formal_run(workflow_id: str, inputs: dict | None = None, **origin) -> Run:
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=workflow_id, run_class="formal", input_payload=inputs or {},
                               actor="检查员", origin_kind=origin.pop("origin_kind", "launcher"), **origin)
    return await settle(run.id)


async def settle(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def fires(schedule_id: str) -> list[ScheduleFire]:
    async with SessionLocal() as session:
        return list((await session.execute(select(ScheduleFire).where(ScheduleFire.schedule_id == schedule_id)
                                           .order_by(ScheduleFire.created_at))).scalars())


async def events(kind: str | None = None, subject: str | None = None) -> list[TriggerEvent]:
    async with SessionLocal() as session:
        q = select(TriggerEvent).order_by(TriggerEvent.created_at)
        if kind:
            q = q.where(TriggerEvent.event == kind)
        if subject:
            q = q.where(TriggerEvent.subject_id == subject)
        return list((await session.execute(q)).scalars())


async def deliveries(schedule_id: str) -> list[TriggerDelivery]:
    async with SessionLocal() as session:
        return list((await session.execute(select(TriggerDelivery).where(TriggerDelivery.schedule_id == schedule_id)
                                           .order_by(TriggerDelivery.occurred_at))).scalars())


def later(seconds: float = 1) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


async def put_event(kind: str, subject: str, payload: dict, at: datetime, *, depth: int = 0,
                    name: str = "门店日报") -> str:
    async with SessionLocal() as session:
        ev = TriggerEvent(event=kind, subject_id=subject, subject_name=name, payload=payload, occurred_at=at,
                          depth=depth, created_at=at)
        session.add(ev)
        await session.commit()
        return ev.id


# --------------------------------------------------------------------------
# 上游工作流运行成功结束
# --------------------------------------------------------------------------


async def test_workflow_finished_triggers_downstream_with_outputs():
    upstream = await workflow("门店日报", value="日报完成")
    downstream = await workflow("区域汇总", fields=[{"name": "basis", "required": True}, {"name": "run"}])
    sid = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0},
                               template={"basis": "{{ event.outputs.r }}（{{ event.outputs.rows }} 行）",
                                         "run": "{{ event.run_id }}"})
    a = await formal_run(upstream)
    [ev] = await events("workflow.finished", upstream)
    assert ev.status == "pending" and ev.payload["run_id"] == a.id and ev.depth == 0 and ev.chain == [a.id]
    # 发生处只记事件、不发起：调度的一拍之前下游什么都没有
    assert await fires(sid) == []

    [fire] = await outbox.dispatch_due(later())
    assert (fire.kind, fire.status) == ("event", "started")
    assert fire.fire_at == ev.occurred_at                     # debounce 0：窗口结束就是事件发生的那一刻
    assert fire.inputs["inputs"] == {"basis": "日报完成（12 行）", "run": a.id}
    brief = fire.inputs["event"]
    assert (brief["event"], brief["subject_id"], brief["merged"], brief["depth"]) == (
        "workflow.finished", upstream, 1, 1)
    assert brief["text"].startswith("工作流「门店日报-") and f"#{a.id[:6]}" in brief["text"]
    b = await settle(fire.run_id)
    assert b.status == "succeeded" and b.schedule_id == sid and b.input == {"basis": "日报完成（12 行）", "run": a.id}
    assert b.origin_kind == "schedule"
    trigger = b.origin["trigger"]
    assert (trigger["kind"], trigger["event"], trigger["subject_id"], trigger["event_id"], trigger["depth"]) == (
        "event", "workflow.finished", upstream, ev.id, 1)
    assert trigger["chain"] == [a.id] and trigger["text"] == brief["text"]
    [delivery] = await deliveries(sid)
    assert (delivery.status, delivery.fire_id) == ("fired", fire.id)
    # 再来一拍：已经派完，不再发起
    assert await outbox.dispatch_due(later(2)) == [] and len(await fires(sid)) == 1
    # 应用页的印章「来源」照实写由什么事件触发
    from app.apps import resolve

    async with SessionLocal() as session:
        said = await resolve._from_text(session, resolve.Source(id="v", kind="latest_sealed", state="ok", run=b))
    assert said == f"由事件触发：{brief['text']}，定时任务「下游汇总」第 1 次"


async def test_webhook_says_the_event(monkeypatch):
    """事件触发的那一次推的通知：写「事件触发」和事件的说法，不写「计划触发」"""
    import json

    import httpx

    from app.core.crypto import encrypt
    from app.scheduler import webhook

    sent: list[dict] = []
    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(
        lambda request: sent.append(json.loads(request.content)) or httpx.Response(200, json={})))
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总"),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0})
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == sid).values(
            webhook_format="generic", webhook_url=encrypt("https://hooks.example.com/agentlab/inbox-9a1b"),
            webhook_events=["finished"]))
        await session.commit()
    a = await formal_run(upstream)
    [fire] = await outbox.dispatch_due(later())
    await settle(fire.run_id)
    for _ in range(3):
        await webhook.notify_due(later(120))
    [body] = sent
    assert body["fire_kind"] == "event" and body["event"] == "finished"
    assert f"事件触发：工作流「门店日报-" in body["text"] and f"（#{a.id[:6]}）" in body["text"]
    assert "计划触发" not in body["text"]


async def test_only_successful_formal_runs_are_events():
    wf = await workflow("门店日报")
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf, run_class="exploratory", input_payload={}, actor="检查员",
                               origin_kind="canvas")
    await settle(run.id)
    failing = await workflow("坏掉的日报", fields=[{"name": "d", "required": True}])
    async with SessionLocal() as session:
        bad = await launch_run(session, workflow_id=failing, run_class="formal", input_payload={}, actor="检查员",
                               origin_kind="launcher")
    assert (await settle(bad.id)).status == "failed"
    assert await events("workflow.finished", wf) == [] and await events("workflow.finished", failing) == []


async def test_workflow_filters_by_app_and_origin():
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总")
    only_hook = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream,
                                                  "origin_kind": "hook", "debounce_s": 0})
    await formal_run(upstream)                                 # 手动正式运行：不是钩子来的
    assert await outbox.dispatch_due(later()) == [] and await fires(only_hook) == []
    [ev] = await events("workflow.finished", upstream)
    assert (ev.status, ev.matched) == ("routed", 0)


async def test_chain_deeper_than_limit_is_refused():
    """防环的兜底：上游自己已经是事件触发链上的第 5 层，下游会是第 6 层，不发起、照实记"""
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总")
    sid = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0})
    await put_event("workflow.finished", upstream, {"workflow": "门店日报", "workflow_id": upstream, "run_id": "r5"},
                    utcnow(), depth=triggers.MAX_DEPTH)
    [fire] = await outbox.dispatch_due(later())
    assert (fire.status, fire.run_id) == ("failed_to_start", None)
    assert "第 6 层（上限 5 层）" in fire.reason and "循环触发" in fire.reason
    assert fire.inputs["event"]["depth"] == 6


async def test_chain_depth_follows_the_runs():
    """A 跑完触发 B：B 的运行来源记第 1 层；B 跑完又是一个事件，深度跟着走、经过的运行接上"""
    a = await workflow("门店日报")
    b = await workflow("区域汇总")
    c = await workflow("全国汇总")
    await event_schedule(b, {"event": "workflow.finished", "workflow_id": a, "debounce_s": 0}, name="B")
    sc = await event_schedule(c, {"event": "workflow.finished", "workflow_id": b, "debounce_s": 0}, name="C")
    first = await formal_run(a)
    [fb] = await outbox.dispatch_due(later())
    run_b = await settle(fb.run_id)
    [ev_b] = await events("workflow.finished", b)
    assert ev_b.depth == 1 and ev_b.chain == [first.id, run_b.id]
    [fc] = [f for f in await outbox.dispatch_due(later(2)) if f.schedule_id == sc]
    run_c = await settle(fc.run_id)
    assert run_c.origin["trigger"]["depth"] == 2 and run_c.origin["trigger"]["chain"] == [first.id, run_b.id]


# --------------------------------------------------------------------------
# 去抖、重叠、停用、太晚
# --------------------------------------------------------------------------


async def test_debounce_merges_events_in_the_window():
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总", fields=[{"name": "run"}])
    sid = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60},
                               template={"run": "{{ event.run_id }}"})
    t0 = utcnow().replace(microsecond=0)
    payload = lambda rid: {"workflow": "门店日报", "workflow_id": upstream, "run_id": rid}  # noqa: E731
    e1 = await put_event("workflow.finished", upstream, payload("run-1"), t0)
    e2 = await put_event("workflow.finished", upstream, payload("run-2"), t0 + timedelta(seconds=30))
    # 窗口还没过：只派、不发
    assert await outbox.dispatch_due(t0 + timedelta(seconds=40)) == []
    assert [d.status for d in await deliveries(sid)] == ["pending", "pending"]
    async with SessionLocal() as session:
        state = await outbox.pending_for(session, await session.get(Schedule, sid))
    assert state == {"events": 0, "waiting": 2, "due_at": (t0 + timedelta(seconds=60)).isoformat()}
    e3 = await put_event("workflow.finished", upstream, payload("run-3"), t0 + timedelta(seconds=70))
    [fire] = await outbox.dispatch_due(t0 + timedelta(seconds=61))
    assert fire.fire_at == t0 + timedelta(seconds=60) and fire.kind == "event"
    assert fire.inputs["event"]["merged"] == 2 and fire.inputs["event"]["event_ids"] == [e1, e2]
    # 内容取窗口里最新的那一条
    assert fire.inputs["inputs"] == {"run": "run-2"}
    await settle(fire.run_id)
    states = {d.event_id: (d.status, d.fire_id) for d in await deliveries(sid)}
    assert states[e1] == states[e2] == ("fired", fire.id) and states[e3] == ("pending", None)
    # 窗口外的那一条另起一次：从它发生起再等 60 秒
    assert await outbox.dispatch_due(t0 + timedelta(seconds=100)) == []
    [second] = await outbox.dispatch_due(t0 + timedelta(seconds=131))
    assert second.fire_at == t0 + timedelta(seconds=130) and second.inputs["event"]["event_ids"] == [e3]
    await settle(second.run_id)


async def test_previous_run_still_going_skips_and_consumes_the_events():
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总")
    sid = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0})
    async with SessionLocal() as session:
        busy = Run(workflow_id=downstream, workflow_name="区域汇总", status="running", schedule_id=sid,
                   run_class="formal")
        session.add(busy)
        await session.commit()
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r1"}, utcnow())
    [fire] = await outbox.dispatch_due(later())
    assert (fire.status, fire.run_id, fire.kind) == ("skipped_overlap", None, "event")
    assert f"上一次运行 #{busy.id[:6]} 仍在进行中" in fire.reason
    assert [d.status for d in await deliveries(sid)] == ["fired"]
    assert await outbox.dispatch_due(later(2)) == []


async def test_disabled_or_changed_schedule_drops_waiting_events():
    upstream = await workflow("门店日报")
    other = await workflow("别的日报")
    downstream = await workflow("区域汇总")
    off = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60},
                               name="停用的")
    moved = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60},
                                 name="改了对象的")
    t0 = utcnow()
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r1"}, t0)
    await outbox.route(t0 + timedelta(seconds=1))
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == off).values(enabled=False))
        await session.execute(Schedule.__table__.update().where(Schedule.id == moved).values(
            trigger={"event": "workflow.finished", "workflow_id": other, "debounce_s": 60}))
        await session.commit()
    assert await outbox.dispatch_due(t0 + timedelta(seconds=61)) == []
    assert [d.status for d in await deliveries(off)] == ["dropped"]
    assert [d.status for d in await deliveries(moved)] == ["dropped"]
    assert await fires(off) == [] and await fires(moved) == []


async def test_disabled_schedule_does_not_receive_events():
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总"),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0}, enabled=False)
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r1"}, utcnow())
    assert await outbox.dispatch_due(later()) == [] and await deliveries(sid) == []


async def test_events_left_while_scheduler_was_off_are_skipped_past_grace():
    """调度关着的那几天没人派：事件留在表里；开起来以后超出补发宽限的照实记一次错过、不补发"""
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总"),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60}, grace=600)
    old = utcnow() - timedelta(days=2)
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r-old"}, old)
    [ev] = await events("workflow.finished", upstream)
    assert ev.status == "pending"                              # 没有调度的一拍：一直等着
    async with SessionLocal() as session:
        state = await outbox.pending_for(session, await session.get(Schedule, sid))
    assert state["events"] == 1 and state["waiting"] == 0
    [fire] = await outbox.dispatch_due(later())
    assert (fire.status, fire.kind, fire.run_id) == ("skipped_misfire", "event", None)
    assert "服务当时没有派发" in fire.reason and "超出补发宽限（10 分钟）" in fire.reason


# --------------------------------------------------------------------------
# 数据源有要处理的变化
# --------------------------------------------------------------------------


async def test_source_changed_once_per_job_with_counts():
    async with SessionLocal() as session:
        source = DataSource(name=f"门店库-{uuid.uuid4().hex[:6]}", kind="sqlite", database=":memory:")
        session.add(source)
        await session.flush()
        for i, severity in enumerate(("action", "action", "warning")):
            session.add(SourceChange(source_id=source.id, source_name=source.name, table_name="orders",
                                     column_name=f"c{i}", kind="column_removed", severity=severity,
                                     detected_by={"job_kind": "introspect", "job_id": "job-7"},
                                     fingerprint=uuid.uuid4().hex, status="open"))
        await session.commit()
        source_id, source_name = source.id, source.name
    downstream = await workflow("变化处理", fields=[{"name": "n"}, {"name": "who"}])
    sid = await event_schedule(downstream, {"event": "source.changed", "source_id": source_id, "debounce_s": 0},
                               template={"n": "{{ event.counts.new }}", "who": "{{ event.source }}"})
    from app.data import source_changes

    assert await source_changes.notify_after_job(source_id, "job-7") == 2
    # 同一个任务没有新记要处理的变化（别的任务号）：不记事件
    assert await source_changes.notify_after_job(source_id, "job-8") == 0
    [ev] = await events("source.changed", source_id)
    assert ev.payload["counts"]["new"] == 2 and ev.payload["counts"]["warning"] == 1 and ev.payload["job_id"] == "job-7"
    [fire] = await outbox.dispatch_due(later())
    assert fire.status == "started" and fire.inputs["inputs"] == {"n": "2", "who": source_name}
    assert fire.inputs["event"]["text"] == f"数据源「{source_name}」出现要处理的变化（新记 2 条）"
    await settle(fire.run_id)


# --------------------------------------------------------------------------
# 上传表格出了新的一期
# --------------------------------------------------------------------------


from tests.test_table_versions_p3 import Chain, store  # noqa: E402,F401  （store：每个测试一个独立的数据目录）


async def test_table_version_on_each_new_period():
    chain = Chain()
    await chain.first()
    [aug] = await events("table.version", chain.sid)
    assert aug.payload["period"] == "2026-08" and aug.payload["replaced"] is False
    assert aug.payload["version"] == chain.s1 and aug.payload["table"] == chain.name
    downstream = await workflow("月报", fields=[{"name": "month", "required": True}, {"name": "table"}])
    sid = await event_schedule(downstream, {"event": "table.version", "source_id": chain.sid, "debounce_s": 0},
                               template={"month": "{{ event.period }}", "table": "{{ event.table }}"})
    async with SessionLocal() as session:
        await session.execute(TriggerEvent.__table__.update().where(TriggerEvent.id == aug.id)
                              .values(status="routed"))        # 8 月是建任务之前的事：不算
        await session.commit()
    await chain.second()
    sep = [e for e in await events("table.version", chain.sid) if e.id != aug.id]
    assert [e.payload["period"] for e in sep] == ["2026-09"] and sep[0].payload["version"] == chain.s2
    [fire] = await outbox.dispatch_due(later())
    assert fire.status == "started" and fire.inputs["inputs"] == {"month": "2026-09", "table": chain.name}
    assert fire.inputs["event"]["text"] == f"上传表格「{chain.name}」出了新的一期（2026-09）"
    run = await settle(fire.run_id)
    assert run.origin["trigger"]["event"] == "table.version" and run.origin["trigger"]["subject_id"] == chain.sid


async def test_table_version_marks_a_replaced_period():
    chain = Chain()
    await chain.first()
    await chain.second()
    async with SessionLocal() as session:
        await outbox.stage_table_version(session, source_id=chain.sid, source_name=chain.name, snapshot_id="s3",
                                         previous_snapshot_id=chain.s2, period_start=chain.sep_start,
                                         period_end=chain.sep_end, seq=3)
        await session.commit()
    replaced = [e for e in await events("table.version", chain.sid) if e.payload["version"] == "s3"]
    assert replaced[0].payload["replaced"] is True
    trigger = triggers.normalize({"event": "table.version", "source_id": chain.sid, "period": "new"})
    assert not triggers.matches(trigger, "table.version", chain.sid, replaced[0].payload)
    assert triggers.matches({**trigger, "period": "any"}, "table.version", chain.sid, replaced[0].payload)
    assert triggers.event_text("table.version", "门店销售", replaced[0].payload) == "上传表格「门店销售」替换了已有的一期（2026-09）"


# --------------------------------------------------------------------------
# 写法与模板
# --------------------------------------------------------------------------


def test_period_labels():
    cases = {("2026-09-01", "2026-09-30"): "2026-09", ("2026-07-01", "2026-09-30"): "2026-Q3",
             ("2026-01-01", "2026-12-31"): "2026", ("2026-09-28", "2026-10-04"): "2026-W40",
             ("2026-09-30", "2026-09-30"): "2026-09-30", ("2026-09-03", "2026-09-17"): "2026-09-03~2026-09-17",
             (None, None): ""}
    assert {k: triggers.period_label(*k) for k in cases} == cases


def test_event_templates_check_and_render():
    tpl = {"month": "{{ event.period }}", "basis": "{{ event.outputs.r }}"}
    with pytest.raises(service.TemplateError, match="「event.outputs.r」不是可用的变量"):
        service.check_template(tpl, event="table.version")
    assert service.check_template({"basis": "{{ event.outputs.r | trim }}"}, event="workflow.finished")
    with pytest.raises(service.TemplateError, match="在事件触发的任务里不可用"):
        service.check_template({"d": "{{ schedule.last_fire_time }}"}, event="source.changed")
    with pytest.raises(service.TemplateError, match="event.counts.new"):
        service.check_template({"n": "{{ event.counts.total }}"}, event="source.changed")
    # 按 cron 触发的任务：event.* 不认，说法一字不变
    with pytest.raises(service.TemplateError, match="「event.period」不是可用的变量，可用的变量：schedule.today"):
        service.check_template({"m": "{{ event.period }}"})
    fire_at = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    values = triggers.values("table.version", triggers.table_payload(
        "门店销售", snapshot_id="snap-9", period_start="2026-09-01", period_end="2026-09-30",
        activated_at=fire_at, replaced=False, seq=2), "Asia/Shanghai")
    out = service.render_inputs({"m": "{{ event.period }}", "at": "{{ event.activated_at }}",
                                 "d": "{{ schedule.yesterday }}"},
                                service.template_values(fire_at, "Asia/Shanghai", ""), event=values)
    assert out == {"m": "2026-09", "at": "2026-10-08T09:30:00+08:00", "d": "2026-10-07"}


def test_trigger_shape_is_checked():
    with pytest.raises(triggers.TriggerError, match="事件种类"):
        triggers.normalize({"event": "caliber.published", "source_id": "x"})
    with pytest.raises(triggers.TriggerError, match="请选择上传表格"):
        triggers.normalize({"event": "table.version"})
    with pytest.raises(triggers.TriggerError, match="不认识的项：perid"):
        triggers.normalize({"event": "table.version", "source_id": "x", "perid": "new"})
    with pytest.raises(triggers.TriggerError, match="按应用、来源过滤"):
        triggers.normalize({"event": "table.version", "source_id": "x", "app_id": "a"})
    with pytest.raises(triggers.TriggerError) as e:
        triggers.normalize({"event": "source.changed", "source_id": "x", "debounce_s": -1})
    assert e.value.field == "debounce_s"
    assert triggers.normalize({"event": "workflow.finished", "workflow_id": " wf ", "origin_kind": "hook"}) == {
        "event": "workflow.finished", "workflow_id": "wf", "origin_kind": "hook", "debounce_s": 60}
