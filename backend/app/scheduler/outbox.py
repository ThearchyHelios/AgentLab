"""待派发事件（docs/design/orchestration-round/00-总方案.md §4）：在发生处写、调度循环一拍取走、按触发条件派给定时任务、去抖
合并、发起。触发条件的写法和说法在 triggers.py，发起在 service.fire_once。

**写（发生处）。** 三处，各自只加一两行调用这里：
- 上传表格按期累积的新一期启用（data/table_versions 的 publish_build / publish_snapshot，切指针的同一个事务）：stage_table_version；
- 正式运行成功收尾（engine/runner 的 _finalize，写终态的同一个事务）：stage_run_finished；
- 探查、剖析任务结束、这一批新记了「要处理」的变化（data/source_changes.notify_after_job）：emit_source_changed，一批一条。
发生处只写一行 TriggerEvent，不查有没有任务在等、不发起运行：事务边界、重复与重启都在这边说清。

**取（调度循环的一拍，dispatch_due）。**
1. 派：pending 的事件逐条按条件 UPDATE 认领成 routed（多个进程也只有一个认领得到），同一个事务里给每个合条件、启用着的
   事件触发任务写一行 TriggerDelivery。没人合条件的也认领掉（matched=0）。
2. 发：每个有等待中的 delivery 的任务，窗口从最早那一条的发生时刻起算 debounce_s 秒；窗口过了就把落在窗口里的几条合成一次
   触发：内容取最新的那一条，fire_at 就是窗口的结束时刻（同一个窗口算出来的永远是同一个时刻，触发记录 (任务, fire_at, kind)
   唯一，重复派发、重启后补派都不会发起两次）。上一次的运行还没结束照 overlap=skip 记 skipped_overlap；触发链超过上限记未能
   发起；派发得太晚（调度停过、超出补发宽限）记 skipped_misfire。之后把这几条标 fired。任务停用了、改了触发条件，等着的标
   dropped，不再派发。
调度关着（AGENTLAB_SCHEDULER）时没有这一拍：事件留在表里，接口照实说有几条在等（pending_for）。

**留多久。** 派完的事件、合进触发的 delivery 留 RETAIN_DAYS 天给排查，之后由这一拍顺手清掉（每小时最多一次）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Schedule, ScheduleFire
from app.db.models_triggers import TriggerDelivery, TriggerEvent
from app.scheduler import triggers

logger = logging.getLogger(__name__)

#: 一拍最多派多少条事件（积压时分几拍派完，不让一拍拖太久）
ROUTE_BATCH = 500
#: 派完的事件、合进触发的 delivery 留多少天
RETAIN_DAYS = 30
#: 清理最多多久一次
CLEAN_EVERY = timedelta(hours=1)
#: 触发链里记几条运行 id（从最近的往前）
CHAIN_KEEP = 10
#: 一次触发里记几条合并进来的事件 id
MERGED_KEEP = 20


# --------------------------------------------------------------------------
# 写（发生处）
# --------------------------------------------------------------------------


async def stage_table_version(session: Any, *, source_id: str, source_name: str, snapshot_id: str,
                              previous_snapshot_id: str | None, period_start: str | None, period_end: str | None,
                              seq: int | None) -> None:
    """按期累积的上传表格启用了新的一期：往调用方的会话里加一条 table.version（不提交，随切指针一起提交）。这一期在上一个
    当前版本里已经有了（统计期相同）就是「替换了已有的一期」（replaced），只认新增一期的任务不认它。读上一个版本时不自动刷写
    会话：调用方（table_versions）挂着还没刷进库的导入记录、快照。"""
    replaced = False
    if previous_snapshot_id and period_start:
        from app.db.models import SourceSnapshot, TableImport

        with session.no_autoflush:
            prev = await session.get(SourceSnapshot, previous_snapshot_id)
            ids = [str(x) for x in (prev.imports or [])] if prev is not None else []
            if ids:
                replaced = bool((await session.execute(select(func.count()).select_from(TableImport).where(
                    TableImport.id.in_(ids), TableImport.period_start == period_start,
                    TableImport.period_end == period_end))).scalar())
    now = utcnow()
    session.add(TriggerEvent(
        event="table.version", subject_id=source_id, subject_name=(source_name or "")[:200], occurred_at=now,
        payload=triggers.table_payload(source_name, snapshot_id=snapshot_id, period_start=period_start,
                                       period_end=period_end, activated_at=now, replaced=replaced, seq=seq)))


def stage_run_finished(session: Any, run: Run) -> None:
    """正式运行成功结束：往收尾的会话里加一条 workflow.finished（不提交，随终态一起提交）。探索运行、没挂在工作流上的不算。
    这次运行自己是事件触发的，事件带上它的触发深度和经过的运行，下游据此防环。"""
    if run.status != "succeeded" or run.run_class != "formal" or not run.workflow_id:
        return
    upstream = (run.origin or {}).get("trigger") if run.origin_kind == "schedule" and isinstance(run.origin, dict) else None
    upstream = upstream if isinstance(upstream, dict) else {}
    depth = upstream.get("depth") if isinstance(upstream.get("depth"), int) else 0
    chain = [str(x) for x in (upstream.get("chain") or []) if x][-(CHAIN_KEEP - 1):] + [run.id]
    session.add(TriggerEvent(
        event="workflow.finished", subject_id=run.workflow_id, subject_name=(run.workflow_name or "")[:200],
        payload=triggers.run_payload(run), depth=depth, chain=chain, occurred_at=run.finished_at or utcnow()))


async def emit_source_changed(source_id: str, job_id: str, new: int) -> None:
    """一次探查、剖析任务结束、新记了 new 条「要处理」的变化：记一条 source.changed（自己开会话提交）。同一个任务只记一条：
    「同一次探查 / 剖析产生的一批只触发一次」。出错只记日志：变化记录本身已经落库。"""
    from app.data import source_changes
    from app.db.models import DataSource

    try:
        async with SessionLocal() as session:
            source = await session.get(DataSource, source_id)
            got = await source_changes.counts(session, source_id)
            by = got.get("by_severity") or {}
            name = source.name if source is not None else ""
            session.add(TriggerEvent(
                event="source.changed", subject_id=source_id, subject_name=name[:200],
                payload={"source": name, "source_id": source_id, "job_id": job_id, "counts": {
                    "new": int(new), "action": int(by.get("action") or 0), "warning": int(by.get("warning") or 0),
                    "info": int(by.get("info") or 0), "pending": int(got.get("pending") or 0)}}))
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("任务 %s 结束后记数据源变化的待派发事件时出错", job_id)


# --------------------------------------------------------------------------
# 一次触发要带的事件
# --------------------------------------------------------------------------


@dataclass
class EventFire:
    """交给 service.fire_once 的事件：模板里 {{ event.* }} 的值、写进触发记录和运行来源的说明、新运行的触发深度。

    sample：立即运行时按对象现在的情况凑的（不是真来了一次事件）；real 为假时是示例值，用到 event.* 的模板不拿它发起。"""

    event: str
    subject_id: str
    subject_name: str
    values: dict[str, Any]
    text: str
    depth: int = 1
    chain: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    first_at: str | None = None
    last_at: str | None = None
    sample: bool = False
    real: bool = True

    def brief(self) -> dict[str, Any]:
        """写进触发记录 inputs.event 的那一份（触发历史、通知读它）：不含出口字段的值。"""
        return {"event": self.event, "subject_id": self.subject_id, "subject_name": self.subject_name,
                "text": self.text, "event_ids": self.event_ids[-MERGED_KEEP:], "merged": len(self.event_ids),
                "first_at": self.first_at, "last_at": self.last_at, "depth": self.depth, "sample": self.sample,
                "real": self.real}

    def origin(self) -> dict[str, Any]:
        """运行来源里的 trigger：{kind, event, 对象, 事件 id……}，在封存范围内（engine/origin）。记录页据 text 写「由事件触发：…」。"""
        return {"kind": "event", "event": self.event, "subject_id": self.subject_id, "subject_name": self.subject_name,
                "event_id": self.event_ids[-1] if self.event_ids else None, "event_ids": self.event_ids[-MERGED_KEEP:],
                "merged": len(self.event_ids), "depth": self.depth, "chain": self.chain[-CHAIN_KEEP:],
                "text": self.text, "sample": self.sample}


def sample_fire(sample: triggers.Sample, tz: str) -> EventFire:
    """立即运行：按示例事件发起（触发链从这里起算，第 1 层）。"""
    text = triggers.event_text(sample.event, sample.subject_name, sample.payload)
    return EventFire(event=sample.event, subject_id=sample.subject_id, subject_name=sample.subject_name,
                     values=triggers.values(sample.event, sample.payload, tz, outputs=sample.outputs),
                     text=text if sample.real else f"{text}（示例值）", sample=True, real=sample.real,
                     chain=[sample.payload["run_id"]] if sample.event == "workflow.finished" and sample.payload.get(
                         "run_id") else [])


# --------------------------------------------------------------------------
# 取（调度循环的一拍）
# --------------------------------------------------------------------------

_cleaned_at: datetime | None = None


async def dispatch_due(now: datetime | None = None) -> list[ScheduleFire]:
    """完整的一拍：派新来的事件、发去抖窗口到了的触发、顺手清理。返回这一拍记下的触发。一个任务出错不影响别的。"""
    now = now or utcnow()
    await route(now)
    out = await fire_waiting(now)
    await _clean(now)
    return out


async def route(now: datetime | None = None) -> int:
    """把 pending 的事件派给合条件、启用着的事件触发任务。返回这一拍派了几条事件。"""
    now = now or utcnow()
    async with SessionLocal() as session:
        events = list((await session.execute(select(TriggerEvent).where(TriggerEvent.status == "pending")
                                             .order_by(TriggerEvent.created_at, TriggerEvent.id)
                                             .limit(ROUTE_BATCH))).scalars())
        if not events:
            return 0
        rows = (await session.execute(select(Schedule.id, Schedule.trigger, Schedule.target_kind).where(
            Schedule.trigger_kind == "event", Schedule.enabled.is_(True)))).all()
    listeners = [(sid, trigger) for sid, trigger, kind in rows if (kind or "workflow") == "workflow"]
    routed = 0
    for ev in events:
        matched = [sid for sid, trigger in listeners if triggers.matches(trigger, ev.event, ev.subject_id, ev.payload)]
        try:
            async with SessionLocal() as session:
                # 条件 UPDATE 认领：两拍撞在一起（以后多开了进程）只有一拍改得动这一行
                claimed = await session.execute(
                    update(TriggerEvent).where(TriggerEvent.id == ev.id, TriggerEvent.status == "pending")
                    .values(status="routed", routed_at=now, matched=len(matched))
                    .execution_options(synchronize_session=False))
                if claimed.rowcount != 1:
                    await session.rollback()
                    continue
                for sid in matched:
                    session.add(TriggerDelivery(schedule_id=sid, event_id=ev.id, occurred_at=ev.occurred_at))
                await session.commit()
                routed += 1
        except Exception:  # noqa: BLE001 - 一条出错（任务刚被删掉、外键）不拖住别的，下一拍再派
            logger.exception("派发事件 %s 时出错", ev.id)
    return routed


async def fire_waiting(now: datetime | None = None) -> list[ScheduleFire]:
    """去抖窗口到了的任务各合成一次触发。"""
    now = now or utcnow()
    async with SessionLocal() as session:
        waiting = (await session.execute(
            select(TriggerDelivery.schedule_id, func.min(TriggerDelivery.occurred_at))
            .where(TriggerDelivery.status == "pending").group_by(TriggerDelivery.schedule_id))).all()
    out: list[ScheduleFire] = []
    for schedule_id, first in waiting:
        try:
            fire = await _fire_one(schedule_id, first, now)
            if fire is not None:
                out.append(fire)
        except Exception:  # noqa: BLE001 - 一个任务出错不能拖住这一拍的其他任务
            logger.exception("事件触发的定时任务 %s 这一拍处理失败", schedule_id)
    return out


async def _fire_one(schedule_id: str, first: datetime, now: datetime) -> ScheduleFire | None:
    from app.scheduler import loop, service

    async with SessionLocal() as session:
        schedule = await session.get(Schedule, schedule_id)
        if schedule is None:
            return None
        trigger, tz = schedule.trigger if isinstance(schedule.trigger, dict) else {}, schedule.timezone
        if not schedule.enabled or schedule.trigger_kind != "event":
            # 等着的时候被停用、改成了别的触发方式：停用是「别再触发了」，等着的不再派发
            await _close(session, _pending(schedule_id), "dropped")
            return None
        due = service.as_utc(first) + timedelta(seconds=triggers.debounce_of(trigger))
        if now < due:
            return None
        window = (await session.execute(
            select(TriggerDelivery.id, TriggerEvent).join(TriggerEvent, TriggerEvent.id == TriggerDelivery.event_id)
            .where(TriggerDelivery.schedule_id == schedule_id, TriggerDelivery.status == "pending",
                   TriggerDelivery.occurred_at <= due)
            .order_by(TriggerDelivery.occurred_at, TriggerEvent.created_at))).all()
        # 等着的时候改了触发条件（换了对象、加了过滤）：已经不合条件的不再派发
        stale = [did for did, ev in window if not triggers.matches(trigger, ev.event, ev.subject_id, ev.payload)]
        if stale:
            await _close(session, TriggerDelivery.id.in_(stale), "dropped")
        window = [(did, ev) for did, ev in window if did not in set(stale)]
        if not window:
            return None
        grace = max(int(schedule.misfire_grace_s or 0), 0)
        events = [ev for _, ev in window]
        latest = events[-1]
        outputs = await _outputs_of(session, latest)
    fire_event = EventFire(
        event=latest.event, subject_id=latest.subject_id, subject_name=latest.subject_name,
        values=triggers.values(latest.event, latest.payload, tz, outputs=outputs),
        text=triggers.event_text(latest.event, latest.subject_name, latest.payload),
        depth=max(int(ev.depth or 0) for ev in events) + 1, chain=[str(x) for x in latest.chain or []],
        event_ids=[ev.id for ev in events], first_at=_iso(events[0].occurred_at), last_at=_iso(latest.occurred_at))
    lateness = (now - due).total_seconds()
    if lateness > max(loop.ON_TIME_S, grace):
        reason = (f"事件发生在 {service.local_text(events[0].occurred_at, tz)}，服务当时没有派发（调度未运行或已停止），"
                  f"超出补发宽限（{service.duration_text(grace)}），没有发起")
        fire = await service.record_fire(schedule_id, due, "event", "skipped_misfire", reason,
                                         {"event": fire_event.brief()})
    else:
        fire = await service.fire_once(schedule_id, due, "event", event=fire_event)
    if fire is not None:
        async with SessionLocal() as session:
            await _close(session, TriggerDelivery.id.in_([did for did, _ in window]), "fired", fire_id=fire.id)
    return fire


def _pending(schedule_id: str) -> Any:
    return (TriggerDelivery.schedule_id == schedule_id) & (TriggerDelivery.status == "pending")


async def _close(session: Any, cond: Any, status: str, *, fire_id: str | None = None) -> None:
    await session.execute(update(TriggerDelivery).where(cond, TriggerDelivery.status == "pending")
                          .values(status=status, fire_id=fire_id).execution_options(synchronize_session=False))
    await session.commit()


async def _outputs_of(session: Any, ev: TriggerEvent) -> dict[str, Any]:
    """workflow.finished 的出口字段：读那次运行的成果（运行已经结束、不会再变）。读不到为空。"""
    if ev.event != "workflow.finished":
        return {}
    run_id = (ev.payload or {}).get("run_id")
    run = await session.get(Run, run_id) if run_id else None
    output = run.output if run is not None and isinstance(run.output, dict) else {}
    return {k: v for k, v in output.items() if not str(k).startswith("_")}


def _iso(moment: datetime | None) -> str | None:
    from app.scheduler import service

    return service.as_utc(moment).isoformat() if moment is not None else None


async def _clean(now: datetime) -> None:
    """派完超过 RETAIN_DAYS 天的事件、合进触发或不再派发的 delivery 清掉。每小时最多一次；出错只记日志。"""
    global _cleaned_at
    if _cleaned_at is not None and now - _cleaned_at < CLEAN_EVERY:
        return
    _cleaned_at = now
    cutoff = now - timedelta(days=RETAIN_DAYS)
    try:
        async with SessionLocal() as session:
            await session.execute(delete(TriggerDelivery).where(TriggerDelivery.status != "pending",
                                                                TriggerDelivery.created_at < cutoff))
            waiting = select(TriggerDelivery.id).where(TriggerDelivery.event_id == TriggerEvent.id).exists()
            await session.execute(delete(TriggerEvent).where(TriggerEvent.status == "routed",
                                                             TriggerEvent.created_at < cutoff, ~waiting)
                                  .execution_options(synchronize_session=False))
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("清理派完的待派发事件时出错")


# --------------------------------------------------------------------------
# 给接口看的
# --------------------------------------------------------------------------


async def pending_for(session: Any, schedule: Schedule) -> dict[str, Any]:
    """一个事件触发任务现在有几条事件在等：events 还没派发的（调度关着时一直留在这里）、waiting 已经派给它、在去抖窗口里的；
    due_at 是窗口结束、会合成一次触发的时刻（没有在等的为空）。"""
    trigger = schedule.trigger if isinstance(schedule.trigger, dict) else {}
    subject, event = triggers.subject_of(trigger), trigger.get("event") or ""
    rows = (await session.execute(select(TriggerEvent.payload).where(
        TriggerEvent.status == "pending", TriggerEvent.event == event, TriggerEvent.subject_id == subject)
        .limit(ROUTE_BATCH))).scalars() if subject else []
    unrouted = sum(1 for payload in rows if triggers.matches(trigger, event, subject, payload))
    waiting, first = (await session.execute(select(func.count(), func.min(TriggerDelivery.occurred_at)).where(
        TriggerDelivery.schedule_id == schedule.id, TriggerDelivery.status == "pending"))).one()
    from app.scheduler import service

    due = service.as_utc(first) + timedelta(seconds=triggers.debounce_of(trigger)) if first is not None else None
    return {"events": unrouted, "waiting": int(waiting or 0), "due_at": due.isoformat() if due else None}
