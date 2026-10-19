"""事件触发的规模（scheduler/outbox.py、scheduler/triggers.py、api/schedules.py）：

- 定时任务列表里每个事件触发任务「有几条在等」一次查齐（outbox.pending_for_many：一条 SQL 按任务分组算待派发数），不再
  每行各查一次；
- 每一拍派发只读监听着这几种事件、这几个对象的任务（按触发条件里的事件种类、对象筛，用 SQLAlchemy 的通用 JSON 访问），
  不再把全部事件触发任务读出来；保存时查环只顺着「上游工作流运行成功结束」那几条边往下找；
- 选对象（trigger-options）按事件、关键字查、分页：返回 more 说还有没有，当前选中的那一个总在里面；
- 去抖窗口最早那一条因为任务改了条件被标 dropped 时，「什么时候合成触发」按剩下的最早一条算；一个窗口的结束时刻和这个任务
  已经有的另一次触发撞在同一刻（别的期次、早一拍发的）时往后挪，那几条事件照常发起，不并进已有的触发。
工作流、表格、数据源都是虚构的。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import select

from app.db.base import SessionLocal, engine, utcnow
from app.db.models import Schedule, ScheduleFire, Workflow
from app.db.models_triggers import TriggerDelivery
from app.scheduler import outbox
from tests.test_schedule_events import (  # noqa: F401 - engine_up 是本模块也要的 autouse 夹具
    deliveries, engine_up, event_schedule, fires, put_event, settle, workflow,
)


@contextmanager
def counted():
    """数这一段里发了几条 SQL。"""
    seen: list[str] = []

    def hook(conn, cursor, statement, *_a):  # noqa: ANN001
        seen.append(statement)

    target = engine.sync_engine
    sa_event.listen(target, "before_cursor_execute", hook)
    try:
        yield seen
    finally:
        sa_event.remove(target, "before_cursor_execute", hook)


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _many(n: int) -> tuple[str, list[str]]:
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总")
    ids = [await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 600},
                                name=f"汇总{i}") for i in range(n)]
    return upstream, ids


async def test_waiting_counts_for_many_schedules_in_one_go():
    upstream, ids = await _many(3)
    t0 = utcnow().replace(microsecond=0)
    for i in range(2):
        await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": f"r{i}"}, t0 + timedelta(seconds=i))
    await outbox.route(t0 + timedelta(seconds=5))
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r9"}, t0 + timedelta(seconds=9))
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Schedule).where(Schedule.id.in_(ids)))).scalars())
        one = {r.id: await outbox.pending_for(session, r) for r in rows}
        with counted() as sql:
            many = await outbox.pending_for_many(session, rows)
    assert many == one
    assert all(v == {"events": 1, "waiting": 2, "due_at": (t0 + timedelta(seconds=600)).isoformat()} for v in many.values())
    assert len(sql) <= 3, sql


async def test_the_schedule_list_does_not_query_once_per_row(client):
    await _many(2)
    with counted() as few:
        assert (await client.get("/api/schedules")).status_code == 200
    await _many(5)
    with counted() as more:
        assert (await client.get("/api/schedules")).status_code == 200
    assert len(more) <= len(few), (len(few), len(more))


async def test_a_tick_reads_only_the_schedules_listening_to_the_pending_events():
    upstream, ids = await _many(2)
    other = await workflow("别的日报")
    elsewhere = await event_schedule(await workflow("别的汇总"), {"event": "workflow.finished", "workflow_id": other})
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r1"}, utcnow())
    async with SessionLocal() as session:
        from app.db.models_triggers import TriggerEvent

        pending = list((await session.execute(select(TriggerEvent).where(TriggerEvent.status == "pending"))).scalars())
        listeners = await outbox.listeners(session, pending)
    assert sorted(sid for sid, _ in listeners) == sorted(ids) and elsewhere not in {sid for sid, _ in listeners}


async def test_trigger_options_search_and_page(client):
    names = [f"检索样例{c}-{i}" for c in "甲乙" for i in range(3)]
    async with SessionLocal() as session:
        rows = [Workflow(name=n, graph={"nodes": [], "edges": []}) for n in names]
        session.add_all(rows)
        await session.commit()
        chosen = next(r.id for r in rows if r.name == "检索样例乙-2")
    r = await client.get("/api/schedules/trigger-options", params={"event": "workflow.finished", "q": "检索样例甲",
                                                                   "limit": 2})
    assert r.status_code == 200, r.text
    [entry] = r.json()["events"]
    assert entry["event"] == "workflow.finished" and entry["more"] is True
    assert [o["name"] for o in entry["objects"]] == ["检索样例甲-0", "检索样例甲-1"]
    r = await client.get("/api/schedules/trigger-options", params={"event": "workflow.finished", "q": "检索样例甲",
                                                                   "limit": 2, "selected": chosen})
    [entry] = r.json()["events"]
    assert chosen in [o["id"] for o in entry["objects"]], "当前选中的那一个总在里面（不被分页挤掉、不显示成已不存在）"
    # 不带参数：照旧给全部种类，每种最多一页
    body = (await client.get("/api/schedules/trigger-options")).json()
    assert {e["event"] for e in body["events"]} == {"table.version", "workflow.finished", "source.changed"}
    assert all("more" in e for e in body["events"])


async def test_a_dropped_earliest_event_no_longer_sets_the_window_end():
    """窗口最早那一条因为任务改了条件被标 dropped：什么时候合成触发按剩下的最早一条算（列表上的 due_at、真发起的 fire_at 一致）。"""
    upstream = await workflow("门店日报")
    downstream = await workflow("区域汇总")
    sid = await event_schedule(downstream, {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60})
    t0 = utcnow().replace(microsecond=0)
    await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r1", "origin_kind": "launcher"}, t0)
    e2 = await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "r2",
                                                         "origin_kind": "app_entry"}, t0 + timedelta(seconds=30))
    await outbox.route(t0 + timedelta(seconds=31))
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == sid).values(
            trigger={"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60, "origin_kind": "app_entry"}))
        await session.commit()
        state = await outbox.pending_for(session, await session.get(Schedule, sid))
    assert state["due_at"] == (t0 + timedelta(seconds=90)).isoformat(), state
    assert await outbox.dispatch_due(t0 + timedelta(seconds=61)) == []
    [fire] = await outbox.dispatch_due(t0 + timedelta(seconds=91))
    assert fire.fire_at == t0 + timedelta(seconds=90) and fire.inputs["event"]["event_ids"] == [e2]
    await settle(fire.run_id)


async def test_a_window_ending_where_another_fire_already_is_still_fires():
    """一个窗口的结束时刻和这个任务已有的另一次触发撞在同一刻（别的期次早一拍发了）：往后挪，这几条照常发起，
    不并进已有的那次触发、不被它吞掉。"""
    source = "src-虚构"
    downstream = await workflow("月报")
    sid = await event_schedule(downstream, {"event": "table.version", "source_id": source, "debounce_s": 60})
    t0 = utcnow().replace(microsecond=0)
    aug = await put_event("table.version", source, {"period": "2026-08", "table": "日报"}, t0)
    [first] = await outbox.dispatch_due(t0 + timedelta(seconds=61))
    assert first.fire_at == t0 + timedelta(seconds=60) and first.inputs["event"]["event_ids"] == [aug]
    await settle(first.run_id)
    # 另一期的事件发生在同一刻，晚到了一拍（比如补写的）：窗口结束同样是 t0+60
    sep = await put_event("table.version", source, {"period": "2026-09", "table": "日报"}, t0)
    [second] = await outbox.dispatch_due(t0 + timedelta(seconds=62))
    assert second.id != first.id and second.inputs["event"]["event_ids"] == [sep]
    assert second.fire_at > first.fire_at
    states = {d.event_id: (d.status, d.fire_id) for d in await deliveries(sid)}
    assert states[sep] == ("fired", second.id)
    await settle(second.run_id)
    async with SessionLocal() as session:
        assert len(list((await session.execute(select(ScheduleFire).where(ScheduleFire.schedule_id == sid))).scalars())) == 2
    assert len(await fires(sid)) == 2
    assert [d.status for d in await deliveries(sid)] == ["fired", "fired"]
    assert TriggerDelivery is not None
