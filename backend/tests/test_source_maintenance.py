"""数据源的定时维护（app/data/source_maintenance.py，next-round 03 B4、总方案 X4）。

定时只有一套：维护就是一条 target_kind=source_maintenance 的 Schedule，认领、补跑、积压只记一条都是 scheduler 那一套。
这里守：

- 到点按顺序建探查任务 → 剖析任务（范围按选的步骤），任务上记 trigger=schedule 和 fire_at；剖析排在同一次触发的探查后面
  （同源队串行：探查做完剖析才开始）；触发记录写明建了哪几个任务；
- 照实记没执行的：剖析没开时剖析、观察两步不执行（探查照做）；数据源停用、不存在、是上传表格时整次不执行；上一次的探查
  还没做完时这次跳过；
- 调度循环的认领：错过的只补最近一次、积压只记一条；
- 维护步骤的接口：不合规 422，合规存进 options、数据源表单不显示它；核对目标（check_target）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.data import catalog, catalog_profile_jobs, introspect_jobs, source_maintenance as sm
from app.db.base import SessionLocal, utcnow
from app.db.models import CatalogProfileJob, DataSource, Schedule, ScheduleFire
from app.db.models_sources import IntrospectJob
from app.main import app
from app.scheduler import loop, service
from tests.fixtures.sources import drop_source, introspect_now


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(client, path, *, profile: bool = True) -> str:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR(10), paid_on DATE);
        INSERT INTO orders VALUES (1, 'paid', '2026-10-01'), (2, 'void', '2026-10-02');
    """)
    db.commit()
    db.close()
    r = await client.post("/api/datasources", json={"name": f"mnt_{uuid.uuid4().hex[:8]}", "kind": "sqlite",
                                                    "database": str(path), "readonly": True})
    sid = r.json()["id"]
    await introspect_now(client, sid)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        if profile:
            row.options = {**(row.options or {}), "catalog_profile": {"enabled": True}}
        await session.commit()
        await catalog.write_entry(session, sid, "orders", {
            "columns": {"status": {"codes": catalog.make_item({"paid": "已支付", "void": "作废"}, "human")}},
        }, if_version=0, actor="测试员")
    return sid


async def _schedule(sid: str, **over) -> str:
    async with SessionLocal() as session:
        fields = {"name": f"维护-{uuid.uuid4().hex[:6]}", "cron": "0 3 * * *", "timezone": "Asia/Shanghai",
                  **service.target_columns("source_maintenance", sid), **over}
        row = Schedule(**fields)
        session.add(row)
        await session.commit()
        return row.id


@pytest.fixture
async def made():
    ids: list[str] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(Schedule).where(Schedule.id.in_(ids)))
        await session.commit()


async def _wait_jobs(fire: ScheduleFire) -> None:
    for job in (fire.inputs or {}).get("jobs") or []:
        manager = introspect_jobs.manager if job["kind"] == "introspect" else catalog_profile_jobs.manager
        await manager.wait(job["id"], timeout=60)


async def test_fire_builds_introspect_then_profile_in_order(client, tmp_path, made):
    sid = await _source(client, tmp_path / "a.db")
    try:
        schedule_id = await _schedule(sid)
        made.append(schedule_id)
        at = utcnow().replace(microsecond=0)
        fire = await service.fire_once(schedule_id, at, "manual", actor="测试员")
        assert fire.status == "started" and fire.reason is None, fire.reason
        jobs = fire.inputs["jobs"]
        assert [j["kind"] for j in jobs] == ["introspect", "profile"] and jobs[1]["scope"] == "maintenance"
        assert fire.inputs["steps"] == list(sm.STEPS)
        await _wait_jobs(fire)
        async with SessionLocal() as session:
            intro = await session.get(IntrospectJob, jobs[0]["id"])
            prof = await session.get(CatalogProfileJob, jobs[1]["id"])
        assert intro.trigger == "schedule" and intro.scope["fire_at"] == at.isoformat()
        assert intro.scope["schedule_id"] == schedule_id and intro.created_by == "测试员"
        assert catalog_profile_jobs.trigger_of(prof) == {"trigger": "schedule", "fire_at": at.isoformat(),
                                                         "schedule_id": schedule_id}
        assert prof.started_at >= intro.finished_at, "同源串行：探查做完，剖析才开始"
        assert prof.status in ("succeeded", "partial"), prof.error
        # 同一次触发再来一遍：交回已有的那一条，不再建任务
        again = await service.fire_once(schedule_id, at, "manual", actor="测试员")
        assert again.id == fire.id
    finally:
        await drop_source(sid)


async def test_observing_again_within_the_window_records_why_in_the_fire(client, tmp_path, made):
    """只选取值观察：12 小时内观察过的表不再观察，任务以 0 张表结束。建任务时就判断得出（这次没有探查、也没排在起草后面，
    开始时表结构、目录不会变）：原因记进触发记录的 inputs.skipped 和 reason（下一轮验收：以前只在剖析任务的提醒里）。
    任务照建（触发照旧是 started，不当成没能发起）。"""
    sid = await _source(client, tmp_path / "c.db")
    try:
        async with SessionLocal() as session:
            row = await session.get(DataSource, sid)
            row.options = sm.with_steps(row.options, ["observe"], actor="测试员")
            await session.commit()
        schedule_id = await _schedule(sid)
        made.append(schedule_id)
        at = utcnow().replace(microsecond=0)
        first = await service.fire_once(schedule_id, at, "manual")
        assert first.status == "started" and not first.inputs.get("skipped"), first.inputs
        await _wait_jobs(first)
        second = await service.fire_once(schedule_id, at + timedelta(seconds=1), "manual")
        await _wait_jobs(second)
        assert second.status == "started" and [j["tables"] for j in second.inputs["jobs"]] == [0]
        [skipped] = second.inputs["skipped"]
        assert skipped["step"] == "observe" and "12 小时内都观察过" in skipped["reason"], skipped
        assert "取值观察没有执行" in second.reason
    finally:
        await drop_source(sid)


async def test_profile_off_records_steps_not_run_and_still_introspects(client, tmp_path, made):
    sid = await _source(client, tmp_path / "b.db", profile=False)
    try:
        schedule_id = await _schedule(sid)
        made.append(schedule_id)
        fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0), "manual")
        assert fire.status == "started"
        assert [j["kind"] for j in fire.inputs["jobs"]] == ["introspect"]
        assert {s["step"] for s in fire.inputs["skipped"]} == {"profile_needed", "observe"}
        assert "剖析需要的表没有执行" in fire.reason and "取值观察没有执行" in fire.reason and "数据剖析" in fire.reason
        await _wait_jobs(fire)
        # 只选观察一步：剖析没开就整次没执行
        async with SessionLocal() as session:
            row = await session.get(DataSource, sid)
            row.options = sm.with_steps(row.options, ["observe"], actor="测试员")
            await session.commit()
        fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0) + timedelta(seconds=1), "manual")
        assert fire.status == "failed_to_start" and "取值观察没有执行" in fire.reason
    finally:
        await drop_source(sid)


async def test_disabled_missing_and_busy_sources(client, tmp_path, made):
    sid = await _source(client, tmp_path / "c.db")
    try:
        schedule_id = await _schedule(sid)
        made.append(schedule_id)
        async with SessionLocal() as session:
            row = await session.get(DataSource, sid)
            row.enabled = False
            await session.commit()
        fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0), "manual")
        assert fire.status == "failed_to_start" and "已停用" in fire.reason
        async with SessionLocal() as session:
            row = await session.get(DataSource, sid)
            row.enabled = True
            session.add(IntrospectJob(source_id=sid, status="queued", scope={}))
            await session.commit()
        fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0) + timedelta(seconds=2), "manual")
        assert fire.status == "skipped_overlap" and "正在探查结构" in fire.reason
        async with SessionLocal() as session:
            await session.execute(delete(IntrospectJob).where(IntrospectJob.source_id == sid,
                                                              IntrospectJob.status == "queued"))
            await session.commit()
    finally:
        await drop_source(sid)
    fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0) + timedelta(seconds=3), "manual")
    assert fire.status == "failed_to_start" and fire.reason == sm.SOURCE_GONE


async def test_loop_claims_and_only_the_latest_missed_fire_runs(client, tmp_path, made):
    sid = await _source(client, tmp_path / "d.db")
    try:
        async with SessionLocal() as session:
            row = await session.get(DataSource, sid)
            row.options = sm.with_steps(row.options, ["introspect"], actor="测试员")
            await session.commit()
        now = utcnow().replace(second=0, microsecond=0)
        # 每 10 分钟一次，停机错过了好几次：只补最近一次，更早的积压只记一条
        schedule_id = await _schedule(sid, cron="*/10 * * * *", timezone="UTC",
                                      next_run_at=now - timedelta(minutes=40), misfire_grace_s=3600)
        made.append(schedule_id)
        fires = await loop.fire_due(now + timedelta(minutes=1))
        assert sorted(f.status for f in fires) == ["skipped_misfire", "started"], [(f.kind, f.status) for f in fires]
        started = next(f for f in fires if f.status == "started")
        assert [j["kind"] for j in started.inputs["jobs"]] == ["introspect"]
        await _wait_jobs(started)
        async with SessionLocal() as session:
            sched = await session.get(Schedule, schedule_id)
            count = len(list((await session.execute(select(ScheduleFire).where(
                ScheduleFire.schedule_id == schedule_id))).scalars()))
        assert sched.next_run_at > now and count == 2
    finally:
        await drop_source(sid)


async def test_steps_api_and_check_target(client, tmp_path):
    sid = await _source(client, tmp_path / "e.db")
    try:
        url = f"/api/datasources/{sid}/maintenance"
        body = (await client.get(url)).json()
        assert body["steps"] == list(sm.STEPS) and body["profile_enabled"] is True and body["schedules"] == []
        assert (await client.put(url, json={"steps": ["nope"]})).status_code == 422
        assert (await client.put(url, json={"steps": []})).status_code == 422
        r = await client.put(url, json={"steps": ["observe", "introspect"]})
        assert r.status_code == 200 and r.json()["steps"] == ["introspect", "observe"], "按固定顺序存"
        [shown] = [s["options"] for s in (await client.get("/api/datasources")).json() if s["id"] == sid]
        assert sm.MAINTENANCE_OPTION not in shown, "数据源表单不显示、不提交它"
        async with SessionLocal() as session:
            assert await sm.check_target(session, sid) is None
            assert await sm.check_target(session, "missing") == sm.SOURCE_GONE
            row = await session.get(DataSource, sid)
            row.origin = "upload"
            await session.commit()
            assert await sm.check_target(session, sid) == sm.NOT_MAINTAINED
    finally:
        await drop_source(sid)


async def test_scheduler_switched_off_never_fires_maintenance(client, tmp_path, made):
    """AGENTLAB_SCHEDULER 关着（测试环境就是关着的，见 conftest）：调度循环不起，到点的维护不会触发——拷来的数据目录不替原库
    查业务库。照实说：这时没有触发记录（定时只有一套，没起循环就没有「这一拍」）。"""
    import asyncio

    from app import scheduler

    sid = await _source(client, tmp_path / "f.db")
    try:
        schedule_id = await _schedule(sid, next_run_at=utcnow() - timedelta(minutes=1))
        made.append(schedule_id)
        assert scheduler.scheduler_enabled() is False
        await scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop()
        async with SessionLocal() as session:
            fires = list((await session.execute(select(ScheduleFire).where(
                ScheduleFire.schedule_id == schedule_id))).scalars())
            jobs = list((await session.execute(select(IntrospectJob).where(
                IntrospectJob.source_id == sid, IntrospectJob.trigger == "schedule"))).scalars())
        assert fires == [] and jobs == []
    finally:
        await drop_source(sid)
