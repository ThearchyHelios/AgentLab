"""数据事件触发复核意见的修正（docs/design/orchestration-round/00-总方案.md §4；scheduler/outbox.py、triggers.py）。

- M1 途经驳回：正式运行走驳回出口跑完（succeeded，run.finished 带 rejected_via）照发 workflow.finished，事件内容带 rejected_via、
  说法接「（途经驳回）」、模板有 {{ event.rejected }}；触发条件缺省只认没途经驳回的，include_rejected 时也认；
- C1 去抖积压：一拍处理到没有到点的窗口为止；已过宽限的几个窗口合成一条跳过（合并了几条、时间跨度），「调度未运行」只在
  派发确实晚到（派给任务的时刻离事件发生超过宽限）时说，否则照实说积压；积压清完之后新来的事件照常触发；
- C3 source.changed 按任务补写：同一个任务只记一条（dedupe），任务结束后没记上的（进程死了、库被锁）在调度的一拍按 job_id 补写；
- C4 不同期次的 table.version 不合进同一个窗口：各期有各自的触发；同一期的重传照旧合并；
- C5 应用补算（app_backfill）发的 workflow.finished 缺省不认，include_backfill 或只认补算时才认；
- C6 预览、立即运行的示例事件和真派发同一套出口字段过滤（「_」开头的不给模板）。

工作流、表格、数据源都是虚构的。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Run, Schedule
from app.db.models_sources import IntrospectJob, SourceChange
from app.db.models_triggers import TriggerDelivery, TriggerEvent
from app.scheduler import outbox, triggers
from tests.test_schedule_events import (  # noqa: F401  （engine_up：每个测试清掉定时任务和事件）
    deliveries, engine_up, event_schedule, events, fires, formal_run, later, put_event, settle, workflow,
)


# --------------------------------------------------------------------------
# M1 途经驳回
# --------------------------------------------------------------------------


async def test_rejected_via_is_carried_and_excluded_by_default():
    upstream = await workflow("门店日报")
    plain = await event_schedule(await workflow("区域汇总"),
                                 {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0}, name="不认驳回")
    also = await event_schedule(await workflow("驳回复核", fields=[{"name": "rej"}]),
                                {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0,
                                 "include_rejected": True}, template={"rej": "{{ event.rejected }}"}, name="也认驳回")
    a = await formal_run(upstream)
    async with SessionLocal() as session:
        # runner 收尾时把这次运行途经的驳回交进来（W1-b 的 _rejected_via(state)）：这里直接调，覆盖那一下
        await session.execute(TriggerEvent.__table__.delete().where(TriggerEvent.subject_id == upstream))
        outbox.stage_run_finished(session, await session.get(Run, a.id),
                                  rejected_via=[{"node_id": "gate", "label": "主管审核"}])
        await session.commit()
    [ev] = await events("workflow.finished", upstream)
    assert ev.payload["rejected"] is True and ev.payload["rejected_via"] == [{"node_id": "gate", "label": "主管审核"}]
    assert triggers.event_text(ev.event, ev.subject_name, ev.payload).endswith("（途经驳回）")
    [fire] = await outbox.dispatch_due(later())
    assert fire.schedule_id == also and fire.status == "started" and fire.inputs["inputs"] == {"rej": "true"}
    assert fire.inputs["event"]["text"].endswith("（途经驳回）")
    assert await fires(plain) == [] and await deliveries(plain) == []
    await settle(fire.run_id)


async def test_plain_runs_say_not_rejected():
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总", fields=[{"name": "rej"}]),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 0},
                               template={"rej": "{{ event.rejected }}"})
    await formal_run(upstream)                                 # runner 收尾没交 rejected_via：没途经驳回
    [ev] = await events("workflow.finished", upstream)
    assert ev.payload["rejected"] is False and ev.payload["rejected_via"] is None
    [fire] = await outbox.dispatch_due(later())
    assert fire.schedule_id == sid and fire.inputs["inputs"] == {"rej": "false"}
    assert "途经驳回" not in fire.inputs["event"]["text"]
    await settle(fire.run_id)


def test_rejected_and_backfill_options_are_described():
    base = triggers.normalize({"event": "workflow.finished", "workflow_id": "wf"})
    assert "（途经驳回、应用补算的不算；去抖 1 分钟）" in triggers.describe(base, "门店日报")
    both = triggers.normalize({"event": "workflow.finished", "workflow_id": "wf", "include_rejected": True,
                               "include_backfill": True})
    assert both["include_rejected"] is True and both["include_backfill"] is True
    assert "（途经驳回、应用补算的也算；去抖 1 分钟）" in triggers.describe(both, "门店日报")
    # 只认应用补算的：补算本来就要认，不再说「补算的不算」
    only = triggers.normalize({"event": "workflow.finished", "workflow_id": "wf", "origin_kind": "app_backfill"})
    text = triggers.describe(only, "门店日报")
    assert "只认来源为「应用补算」的" in text and "应用补算的不算" not in text and "途经驳回的不算" in text
    # 别的事件不收这两项
    for key in ("include_rejected", "include_backfill"):
        try:
            triggers.normalize({"event": "table.version", "source_id": "x", key: True})
        except triggers.TriggerError as e:
            assert "上游工作流运行成功结束" in str(e)
        else:
            raise AssertionError(key)
    assert "event.rejected" in triggers.names("workflow.finished")


# --------------------------------------------------------------------------
# C5 应用补算
# --------------------------------------------------------------------------


def test_backfill_runs_are_excluded_unless_asked():
    payload = {"origin_kind": "app_backfill", "rejected": False}
    base = triggers.normalize({"event": "workflow.finished", "workflow_id": "wf"})
    assert not triggers.matches(base, "workflow.finished", "wf", payload)
    assert triggers.matches(base, "workflow.finished", "wf", {**payload, "origin_kind": "launcher"})
    assert triggers.matches({**base, "include_backfill": True}, "workflow.finished", "wf", payload)
    assert triggers.matches({**base, "origin_kind": "app_backfill"}, "workflow.finished", "wf", payload)


# --------------------------------------------------------------------------
# C6 示例事件的出口字段
# --------------------------------------------------------------------------


async def test_sample_outputs_use_the_same_filter_as_dispatch():
    upstream = await workflow("门店日报")
    run = await formal_run(upstream)
    async with SessionLocal() as session:
        await session.execute(Run.__table__.update().where(Run.id == run.id).values(
            output={"r": "日报完成", "_issuance": {"tier": "full"}}))
        await session.commit()
        got = await triggers.sample(session, {"event": "workflow.finished", "workflow_id": upstream})
    assert got.real and got.outputs == {"r": "日报完成"}
    assert triggers.outputs_of({"r": 1, "_x": 2}) == {"r": 1}


async def test_sample_skips_rejected_and_backfill_runs_by_default():
    upstream = await workflow("门店日报")
    plain = await formal_run(upstream)
    backfill = await formal_run(upstream, origin_kind="app_backfill")
    async with SessionLocal() as session:
        got = await triggers.sample(session, {"event": "workflow.finished", "workflow_id": upstream})
        wide = await triggers.sample(session, {"event": "workflow.finished", "workflow_id": upstream,
                                               "include_backfill": True})
    assert got.payload["run_id"] == plain.id and wide.payload["run_id"] == backfill.id


# --------------------------------------------------------------------------
# C1 去抖积压
# --------------------------------------------------------------------------


async def _backlog(upstream: str, sid: str, *, n: int, gap_s: int, routed_late: bool) -> tuple:
    """n 条相隔 gap_s 秒的事件，派给了任务、还没处理。routed_late：派给任务的时刻是现在（调度停过）；否则发生后 1 秒就派了
    （调度在跑，只是没处理完）。"""
    t0 = utcnow().replace(microsecond=0) - timedelta(seconds=gap_s * (n - 1) + 61)
    ids = []
    async with SessionLocal() as session:
        for i in range(n):
            at = t0 + timedelta(seconds=gap_s * i)
            ev = TriggerEvent(event="workflow.finished", subject_id=upstream, subject_name="门店日报",
                              payload={"workflow_id": upstream, "run_id": f"run-{i}", "rejected": False},
                              occurred_at=at, created_at=at, status="routed", matched=1)
            session.add(ev)
            await session.flush()
            session.add(TriggerDelivery(schedule_id=sid, event_id=ev.id, occurred_at=at,
                                        created_at=utcnow() if routed_late else at + timedelta(seconds=1)))
            ids.append(ev.id)
        await session.commit()
    return t0, ids


async def test_backlog_is_drained_in_one_tick_and_missed_windows_merge_into_one_skip():
    """12 条相隔 5 分钟、去抖 60 秒、宽限 600 秒：一拍处理完。过了宽限的 10 个窗口合成一条跳过（合并 10 次、写时间跨度），
    宽限内的两个照常发起（第二个照重叠规则可能跳过）；积压清完以后新来的事件照常触发"""
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总", fields=[{"name": "run"}]),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60}, grace=600,
                               template={"run": "{{ event.run_id }}"})
    t0, ids = await _backlog(upstream, sid, n=12, gap_s=300, routed_late=True)
    done = await outbox.dispatch_due(utcnow())
    assert len(done) == 3, [(f.status, f.reason) for f in done]
    skip, *rest = done
    assert skip.status == "skipped_misfire" and skip.kind == "event"
    assert skip.inputs["event"]["merged"] == 10 and skip.inputs["event"]["windows"] == 10
    assert skip.inputs["event"]["event_ids"] == ids[:10]
    assert "服务当时没有派发（调度未运行或已停止）" in skip.reason and " 至 " in skip.reason and "10 次事件" in skip.reason
    assert [f.inputs["event"]["event_ids"] for f in rest] == [[ids[10]], [ids[11]]]
    assert rest[0].status == "started" and rest[1].status in ("started", "skipped_overlap")
    assert {d.status for d in await deliveries(sid)} == {"fired"}
    for f in rest:
        if f.run_id:
            await settle(f.run_id)
    # 积压清完：新来的一条照常触发
    new = await put_event("workflow.finished", upstream, {"workflow_id": upstream, "run_id": "run-new"}, utcnow())
    [fresh] = await outbox.dispatch_due(later(61))
    assert fresh.status == "started" and fresh.inputs["event"]["event_ids"] == [new]
    await settle(fresh.run_id)


async def test_backlog_while_scheduler_ran_says_backlog_not_scheduler_off():
    upstream = await workflow("门店日报")
    sid = await event_schedule(await workflow("区域汇总"),
                               {"event": "workflow.finished", "workflow_id": upstream, "debounce_s": 60}, grace=600)
    await _backlog(upstream, sid, n=4, gap_s=600, routed_late=False)
    skip, *rest = await outbox.dispatch_due(utcnow())
    assert skip.status == "skipped_misfire" and skip.inputs["event"]["merged"] == 3
    assert "调度未运行" not in skip.reason and "积压" in skip.reason, skip.reason
    for f in rest:
        if f.run_id:
            await settle(f.run_id)


# --------------------------------------------------------------------------
# C4 不同期次各自一个窗口
# --------------------------------------------------------------------------


async def test_different_periods_get_their_own_windows():
    table = uuid.uuid4().hex
    sid = await event_schedule(await workflow("月报", fields=[{"name": "month"}]),
                               {"event": "table.version", "source_id": table, "debounce_s": 60},
                               template={"month": "{{ event.period }}"})
    t0 = utcnow().replace(microsecond=0)
    payload = lambda p, s, e: {"table": "门店销售", "period": p, "period_start": s, "period_end": e,  # noqa: E731
                               "version": f"snap-{p}", "replaced": False}
    sep = await put_event("table.version", table, payload("2026-09", "2026-09-01", "2026-09-30"), t0, name="门店销售")
    oct_ = await put_event("table.version", table, payload("2026-10", "2026-10-01", "2026-10-31"),
                           t0 + timedelta(seconds=30), name="门店销售")
    again = await put_event("table.version", table, {**payload("2026-09", "2026-09-01", "2026-09-30"), "replaced": True},
                            t0 + timedelta(seconds=40), name="门店销售")
    [first] = await outbox.dispatch_due(t0 + timedelta(seconds=61))
    # 9 月一个窗口：9 月和 9 月的重传合在一起（内容取最新的那一条），10 月不合进来
    assert first.inputs["event"]["event_ids"] == [sep, again] and first.inputs["inputs"] == {"month": "2026-09"}
    await settle(first.run_id)
    [second] = await outbox.dispatch_due(t0 + timedelta(seconds=91))
    assert second.inputs["event"]["event_ids"] == [oct_] and second.inputs["inputs"] == {"month": "2026-10"}
    assert second.fire_at == t0 + timedelta(seconds=90) and second.status == "started"
    await settle(second.run_id)


# --------------------------------------------------------------------------
# C3 source.changed 按任务补写
# --------------------------------------------------------------------------


async def _source_with_changes(job_status: str) -> tuple[str, str]:
    async with SessionLocal() as session:
        source = DataSource(name=f"门店库-{uuid.uuid4().hex[:6]}", kind="sqlite", database=":memory:")
        session.add(source)
        await session.flush()
        job = IntrospectJob(source_id=source.id, status=job_status,
                            finished_at=utcnow() if job_status != "running" else None)
        session.add(job)
        await session.flush()
        for i in range(2):
            session.add(SourceChange(source_id=source.id, source_name=source.name, table_name="orders",
                                     column_name=f"c{i}", kind="column_removed", severity="action",
                                     detected_by={"job_kind": "introspect", "job_id": job.id},
                                     fingerprint=uuid.uuid4().hex, status="open"))
        await session.commit()
        return source.id, job.id


async def test_source_changed_is_backfilled_once_per_job():
    """任务结束了、变化记下了，那条事件却没记上（进程在中间死了）：调度的一拍按 job_id 补写，只补一次；之后再走到
    notify_after_job 也不重复"""
    source_id, job_id = await _source_with_changes("succeeded")
    running_source, _ = await _source_with_changes("running")
    assert await events("source.changed", source_id) == []
    assert await outbox.backfill_source_changes(utcnow()) >= 1
    [ev] = await events("source.changed", source_id)
    assert ev.payload["job_id"] == job_id and ev.payload["counts"]["new"] == 2 and ev.dedupe_key == f"source.changed:{job_id}"
    # 还在进行的任务不补：它结束时自己会记
    assert await events("source.changed", running_source) == []
    await outbox.backfill_source_changes(utcnow())
    from app.data import source_changes

    await source_changes.notify_after_job(source_id, job_id)
    assert len(await events("source.changed", source_id)) == 1


async def test_dispatch_tick_runs_the_backfill():
    source_id, _ = await _source_with_changes("succeeded")
    outbox._backfilled_at = None                                # 刚起的服务：第一拍就补
    await outbox.dispatch_due(utcnow())
    assert len(await events("source.changed", source_id)) == 1
