"""定时任务的调度循环（app/scheduler/loop.py、service.fire_once）。

- 到点只发起一次：条件 UPDATE 认领（WHERE next_run_at = 旧值）并推进到下一次，两拍并发也只有一拍认领得到；
  触发记录 (任务, fire_at, kind) 唯一，撞上约束的不再发起
- 发起走 engine/launch.py 的 launch_run：记在「定时任务：<名字>」名下、带 schedule_id，输入按 fire_at 渲染；
  正式运行用当前发布版本，没有发布版本被拒，拒绝的原话记成 failed_to_start；别的异常记通用说明、详情进日志
- 上一次的运行还没结束（排队、运行中、停在审批 / 挂起都算）记 skipped_overlap
- 错过的触发：最近一次在宽限内补一次（kind=misfire），更早的积压只记一条跳过；最近一次也超出宽限就记一条
  skipped_misfire，推进到未来的下一次，不补积压
- start / stop：起一个 asyncio 任务按拍执行，stop 干净地停下；AGENTLAB_SCHEDULER 关着时 start 不起循环

工作流、字段、任务名都是虚构的。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app import scheduler
from app.core.config import Settings
from app.db.base import SessionLocal
from app.db.models import Run, Schedule, ScheduleFire, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.scheduler import loop, service

UTC = timezone.utc
#: 上海 2026-10-05（周一）09:00
NINE = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(text: str) -> dict:
    return {
        "nodes": [node("start", "input", fields=[{"name": "stat_date", "required": True}]),
                  node("out", "output", fields=[{"name": "r", "value": f"{text} {{{{ input.stat_date }}}}"}])],
        "edges": [{"source": "start", "target": "out"}],
    }


async def workflow(*, published: bool = True, canvas: str = "画布", snapshot: str = "发布版") -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"门店周报-{uuid.uuid4().hex[:6]}", graph=graph(canvas),
                      status="published" if published else "draft", published_version=1 if published else None)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph(snapshot)))
        await session.commit()
        return wf.id


async def schedule(workflow_id: str, *, cron: str = "0 9 * * *", next_run_at: datetime | None = NINE,
                   run_class: str = "formal", grace: int = 3600, enabled: bool = True, name: str = "周一早报") -> str:
    async with SessionLocal() as session:
        row = Schedule(name=name, workflow_id=workflow_id, cron=cron, timezone="Asia/Shanghai", run_class=run_class,
                       input_template={"stat_date": "{{ schedule.yesterday }}"}, enabled=enabled,
                       misfire_grace_s=grace, next_run_at=next_run_at)
        session.add(row)
        await session.commit()
        return row.id


async def fires(schedule_id: str) -> list[ScheduleFire]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(ScheduleFire).where(ScheduleFire.schedule_id == schedule_id).order_by(ScheduleFire.created_at)
        )).scalars())


async def runs_of(schedule_id: str) -> list[Run]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Run).where(Run.schedule_id == schedule_id))).scalars())


async def next_of(schedule_id: str) -> datetime | None:
    async with SessionLocal() as session:
        return (await session.get(Schedule, schedule_id)).next_run_at


async def settle(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


# --------------------------------------------------------------------------
# 到点发起
# --------------------------------------------------------------------------


async def test_due_schedule_launches_once_and_moves_on():
    sid = await schedule(await workflow())
    await loop.fire_due(NINE + timedelta(seconds=10))
    [fire] = await fires(sid)
    assert (fire.kind, fire.status, fire.fire_at, fire.reason) == ("scheduled", "started", NINE, None)
    assert fire.inputs == {"stat_date": "2026-10-04"}
    run = await settle(fire.run_id)
    assert run.schedule_id == sid and run.run_class == "formal" and run.status == "succeeded"
    assert run.started_by == "定时任务：周一早报"
    assert run.input == {"stat_date": "2026-10-04"}
    # 正式运行用的是发布版本，不是画布
    assert run.version == 1 and run.output == {"r": "发布版 2026-10-04"}
    assert await next_of(sid) == NINE + timedelta(days=1)
    # 同一拍再来一次：已经推进到明天，不再发起
    await loop.fire_due(NINE + timedelta(seconds=20))
    assert len(await fires(sid)) == 1 and len(await runs_of(sid)) == 1


async def test_exploratory_schedule_uses_the_canvas():
    sid = await schedule(await workflow(published=False), run_class="exploratory")
    await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    run = await settle(fire.run_id)
    assert run.run_class == "exploratory" and run.output == {"r": "画布 2026-10-04"}


async def test_two_concurrent_ticks_launch_only_once():
    sid = await schedule(await workflow())
    now = NINE + timedelta(seconds=3)
    await asyncio.gather(loop.fire_due(now), loop.fire_due(now), loop.fire_due(now))
    assert len(await fires(sid)) == 1
    [run] = await runs_of(sid)
    await settle(run.id)


async def test_concurrent_ticks_with_different_clocks_claim_once():
    """一拍准点、一拍晚了两分钟（会被当成补发、kind 不同、撞不上唯一约束）：只有认领到的那一拍处理"""
    sid = await schedule(await workflow())
    await asyncio.gather(loop.fire_due(NINE + timedelta(seconds=3)), loop.fire_due(NINE + timedelta(minutes=2)))
    [fire] = await fires(sid)
    [run] = await runs_of(sid)
    assert fire.run_id == run.id
    await settle(run.id)


async def test_a_fire_already_recorded_is_not_launched_again():
    """认领之外的第二道保险：同一 (任务, fire_at, kind) 已有记录，fire_once 交回那一条，不再发起"""
    sid = await schedule(await workflow())
    first = await service.fire_once(sid, NINE, "scheduled")
    again = await service.fire_once(sid, NINE, "scheduled")
    assert again.id == first.id and len(await fires(sid)) == 1
    [run] = await runs_of(sid)
    await settle(run.id)


async def test_disabled_schedule_is_not_fired():
    sid = await schedule(await workflow(), enabled=False)
    await loop.fire_due(NINE + timedelta(seconds=5))
    assert await fires(sid) == [] and await runs_of(sid) == []


@pytest.mark.parametrize("status", ["queued", "running", "interrupted"])
async def test_previous_run_still_going_means_skip(status):
    wf = await workflow()
    sid = await schedule(wf)
    async with SessionLocal() as session:
        busy = Run(workflow_id=wf, workflow_name="门店周报", status=status, schedule_id=sid, run_class="formal")
        session.add(busy)
        await session.commit()
    await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    assert (fire.status, fire.run_id) == ("skipped_overlap", None)
    assert busy.id[:8] in fire.reason and "跳过" in fire.reason
    assert [r.id for r in await runs_of(sid)] == [busy.id]
    assert await next_of(sid) == NINE + timedelta(days=1)


# --------------------------------------------------------------------------
# 错过的触发
# --------------------------------------------------------------------------


async def test_missed_within_grace_is_caught_up_once_with_inputs_of_its_own_day():
    """服务停到 00:30（上海 10 月 6 日）才起来：补一次 9 点那一次，昨天还是 10 月 4 日"""
    sid = await schedule(await workflow(), grace=86400)
    late = NINE + timedelta(hours=15, minutes=30)
    await loop.fire_due(late)
    [fire] = await fires(sid)
    assert (fire.kind, fire.status, fire.fire_at) == ("misfire", "started", NINE)
    assert fire.inputs == {"stat_date": "2026-10-04"}
    await settle(fire.run_id)
    assert await next_of(sid) == NINE + timedelta(days=1)


async def test_backlog_is_skipped_and_only_the_latest_is_caught_up():
    """每小时一次，停了三个多小时：01–03 点的积压记一条跳过，04 点那次在宽限内补发"""
    sid = await schedule(await workflow(), cron="0 * * * *", grace=3600)
    await loop.fire_due(NINE + timedelta(hours=3, minutes=20))
    skipped, caught_up = await fires(sid)
    assert (skipped.kind, skipped.status, skipped.fire_at, skipped.run_id) == ("misfire", "skipped_misfire", NINE, None)
    assert "2026-10-05 09:00" in skipped.reason and "2026-10-05 11:00" in skipped.reason
    assert (caught_up.kind, caught_up.status, caught_up.fire_at) == ("misfire", "started", NINE + timedelta(hours=3))
    assert len(await runs_of(sid)) == 1
    await settle(caught_up.run_id)
    assert await next_of(sid) == NINE + timedelta(hours=4)


async def test_missed_beyond_grace_is_skipped_without_launching():
    sid = await schedule(await workflow(), grace=600)
    await loop.fire_due(NINE + timedelta(hours=2))
    [fire] = await fires(sid)
    assert (fire.kind, fire.status, fire.fire_at, fire.run_id) == ("misfire", "skipped_misfire", NINE, None)
    assert "10 分钟" in fire.reason and "2026-10-05 09:00" in fire.reason
    assert await runs_of(sid) == []
    assert await next_of(sid) == NINE + timedelta(days=1)


async def test_days_of_downtime_leave_one_skip_record():
    sid = await schedule(await workflow(), grace=600, next_run_at=NINE - timedelta(days=4))
    await loop.fire_due(NINE + timedelta(hours=2))
    [fire] = await fires(sid)
    assert (fire.status, fire.fire_at) == ("skipped_misfire", NINE)
    assert "2026-10-01 09:00" in fire.reason and "2026-10-05 09:00" in fire.reason
    assert await runs_of(sid) == [] and await next_of(sid) == NINE + timedelta(days=1)


async def test_on_time_fire_ignores_a_small_grace():
    """宽限写 0 也不影响准点的触发：拍与拍之间的十几秒不算错过"""
    sid = await schedule(await workflow(), grace=0)
    await loop.fire_due(NINE + timedelta(seconds=14))
    [fire] = await fires(sid)
    assert (fire.kind, fire.status) == ("scheduled", "started")
    await settle(fire.run_id)


# --------------------------------------------------------------------------
# 发起被拒、出错
# --------------------------------------------------------------------------


async def test_formal_schedule_without_published_version_records_the_refusal():
    wf = await workflow(published=False)
    sid = await schedule(wf)
    await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    async with SessionLocal() as session:
        name = (await session.get(Workflow, wf)).name
    assert (fire.status, fire.run_id) == ("failed_to_start", None)
    assert fire.reason == f"「{name}」还没有发布版本，请先在编排页发布"
    assert await runs_of(sid) == []
    assert await next_of(sid) == NINE + timedelta(days=1)


async def test_deleted_workflow_records_the_refusal():
    sid = await schedule(f"gone{uuid.uuid4().hex[:8]}")
    await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    assert (fire.status, fire.reason) == ("failed_to_start", "工作流不存在，可能已被删除")


async def test_unexpected_errors_record_a_generic_reason(monkeypatch, caplog):
    from app.engine import launch

    async def explode(*args, **kwargs):
        raise RuntimeError("库连接断开：sqlite busy")

    monkeypatch.setattr(launch, "launch_run", explode)
    sid = await schedule(await workflow())
    with caplog.at_level(logging.ERROR):
        await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    assert (fire.status, fire.reason) == ("failed_to_start", service.GENERIC_REASON)
    assert "库连接断开" in caplog.text
    assert await next_of(sid) == NINE + timedelta(days=1)


async def test_broken_cron_in_the_database_stops_the_schedule():
    sid = await schedule(await workflow(), cron="0 25 * * *")
    await loop.fire_due(NINE + timedelta(seconds=5))
    [fire] = await fires(sid)
    assert fire.status == "failed_to_start" and "「时」" in fire.reason
    assert await next_of(sid) is None


# --------------------------------------------------------------------------
# start / stop
# --------------------------------------------------------------------------


async def test_start_does_nothing_when_switched_off():
    """测试环境 AGENTLAB_SCHEDULER=0（conftest）：start 不起循环，stop 照样能调"""
    assert await scheduler.start() is False
    assert scheduler.running() is False
    await scheduler.stop()


async def test_loop_ticks_until_stopped():
    sid = await schedule(await workflow(), next_run_at=datetime.now(UTC) - timedelta(seconds=1), cron="* * * * *")
    on = Settings(_env_file=None, scheduler=True)
    assert await scheduler.start(interval=0.05, config=on) is True
    assert scheduler.running()
    for _ in range(200):
        if await fires(sid):
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    assert scheduler.running() is False
    [fire] = await fires(sid)
    assert fire.status == "started"
    await settle(fire.run_id)
    # 停下之后不再有拍：把下一次拨回过去也不会再发起
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == sid)
                              .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    await asyncio.sleep(0.2)
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduleFire)
                                    .where(ScheduleFire.schedule_id == sid)) == 1


async def test_start_twice_keeps_one_loop():
    on = Settings(_env_file=None, scheduler=True)
    await scheduler.start(interval=0.05, config=on)
    first = loop.runner.task
    await scheduler.start(interval=0.05, config=on)
    assert loop.runner.task is first
    await scheduler.stop()
    assert first.done()
