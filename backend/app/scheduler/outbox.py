"""待派发事件（docs/design/orchestration-round/00-总方案.md §4）：在发生处写、调度循环一拍取走、按触发条件派给定时任务、去抖
合并、发起。触发条件的写法和说法在 triggers.py，发起在 service.fire_once。

**写（发生处）。** 三处，各自只加一两行调用这里：
- 上传表格按期累积的新一期启用（data/table_versions 的 publish_build / publish_snapshot，切指针的同一个事务）：stage_table_version；
- 正式运行成功收尾（engine/runner 的 _finalize，写终态的同一个事务）：stage_run_finished；途经驳回的（W1-b 的驳回出口）带上
  rejected_via，缺省不认它的任务不派；
- 探查、剖析任务结束、这一批新记了「要处理」的变化（data/source_changes.notify_after_job）：emit_source_changed，一个任务一条。
  这一处不和变化记录同一个事务（一批只触发一次要等任务结束才知道），中间丢了的由每一拍的 backfill_source_changes 按任务号补写，
  去重键唯一、不会记成两条。
发生处只写一行 TriggerEvent，不查有没有任务在等、不发起运行：事务边界、重复与重启都在这边说清。

**取（调度循环的一拍，dispatch_due）。**
1. 派：pending 的事件逐条按条件 UPDATE 认领成 routed（多个进程也只有一个认领得到），同一个事务里给每个合条件、启用着的
   事件触发任务写一行 TriggerDelivery。没人合条件的也认领掉（matched=0）。
2. 发：每个有等待中的 delivery 的任务，按发生时刻把它们切成去抖窗口：窗口从第一条的发生时刻起算 debounce_s 秒，落在里面的
   几条合成一次触发，内容取最新的那一条，fire_at 是窗口的结束时刻（同一个窗口算出来的永远是同一个时刻，触发记录
   (任务, fire_at, kind) 唯一，重复派发、重启后补派都不会发起两次）。上传表格按期次分窗口（triggers.window_key）：9 月、10 月
   连着启用各有各的触发，同一期的重传照旧合并。一拍里把到点的窗口处理完（最多 MAX_WINDOWS 个），不是一拍一个：以前积压时
   一拍只走一个窗口，后面的越等越久、最后全过了宽限。到点的窗口里已经过了补发宽限的（排在前面）合成**一条** skipped_misfire，
   写合并了几次事件、几个窗口、时间跨度——派给任务就已经晚了（调度停过）才说「调度未运行」，否则照实说积压；之后宽限内的
   照常发起：上一次的运行还没结束照 overlap=skip 记 skipped_overlap，触发链超过上限记未能发起。处理过的标 fired。任务停用了、
   改了触发条件，等着的标 dropped，不再派发。
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
#: 一个任务一拍最多处理几个到点的去抖窗口、读几条等着的事件（积压特别多时分几拍处理完，不让一拍拖太久）
MAX_WINDOWS = 100
DRAIN_BATCH = 5000
#: source.changed 往回补写几天内结束的任务；补写最多多久一次（刚起的服务第一拍就补）
BACKFILL_DAYS = 2
BACKFILL_EVERY = timedelta(minutes=10)


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


def stage_run_finished(session: Any, run: Run, rejected_via: Any = None) -> None:
    """正式运行成功结束：往收尾的会话里加一条 workflow.finished（不提交，随终态一起提交）。探索运行、没挂在工作流上的不算。
    这次运行自己是事件触发的，事件带上它的触发深度和经过的运行，下游据此防环。

    rejected_via：这次运行途经了哪几个关卡的驳回（W1-b 的驳回出口：按驳回那条路走完，状态仍是 succeeded；runner 收尾时把
    _rejected_via(state) 交进来），缺省 None 是没途经驳回。照发，事件里带上它：缺省不认途经驳回的任务不派（triggers.matches）。"""
    if run.status != "succeeded" or run.run_class != "formal" or not run.workflow_id:
        return
    upstream = (run.origin or {}).get("trigger") if run.origin_kind == "schedule" and isinstance(run.origin, dict) else None
    upstream = upstream if isinstance(upstream, dict) else {}
    depth = upstream.get("depth") if isinstance(upstream.get("depth"), int) else 0
    chain = [str(x) for x in (upstream.get("chain") or []) if x][-(CHAIN_KEEP - 1):] + [run.id]
    session.add(TriggerEvent(
        event="workflow.finished", subject_id=run.workflow_id, subject_name=(run.workflow_name or "")[:200],
        payload=triggers.run_payload(run, rejected_via), depth=depth, chain=chain,
        occurred_at=run.finished_at or utcnow()))


def source_key(job_id: str) -> str:
    """source.changed 的去重键：一个任务一条。"""
    return f"source.changed:{job_id}"


async def emit_source_changed(source_id: str, job_id: str, new: int, *, occurred_at: datetime | None = None) -> bool:
    """一次探查、剖析任务结束、新记了 new 条「要处理」的变化：记一条 source.changed（自己开会话提交），返回记没记下。同一个任务
    只记一条（去重键唯一）：「同一次探查 / 剖析产生的一批只触发一次」，补写（backfill_source_changes）和这里撞上也只有一条。
    出错只记日志：变化记录本身已经落库，没记上的由补写接着记。"""
    from sqlalchemy.exc import IntegrityError

    from app.data import source_changes
    from app.db.models import DataSource

    try:
        async with SessionLocal() as session:
            if await session.scalar(select(TriggerEvent.id).where(TriggerEvent.dedupe_key == source_key(job_id))):
                return False
            source = await session.get(DataSource, source_id)
            got = await source_changes.counts(session, source_id)
            by = got.get("by_severity") or {}
            name = source.name if source is not None else ""
            session.add(TriggerEvent(
                event="source.changed", subject_id=source_id, subject_name=name[:200], dedupe_key=source_key(job_id),
                occurred_at=occurred_at or utcnow(),
                payload={"source": name, "source_id": source_id, "job_id": job_id, "counts": {
                    "new": int(new), "action": int(by.get("action") or 0), "warning": int(by.get("warning") or 0),
                    "info": int(by.get("info") or 0), "pending": int(got.get("pending") or 0)}}))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()          # 同一个任务刚被别处记上（补写和收尾撞在一起）
                return False
            return True
    except Exception:  # noqa: BLE001
        logger.exception("任务 %s 结束后记数据源变化的待派发事件时出错", job_id)
        return False


_backfilled_at: datetime | None = None


async def backfill_source_changes(now: datetime | None = None) -> int:
    """补写 source.changed：BACKFILL_DAYS 天内测到、还开着的「要处理」级变化，按任务号分组；任务已经结束（探查任务；剖析任务
    只算取值观察那几种，同 notify_after_job）、却没有那个任务的事件的，补一条（发生时刻取任务结束的时刻）。返回补了几条。
    在 Python 里按任务号分组，不用各库写法不同的 JSON 取值。"""
    from app.data.catalog_profile_jobs import OBSERVING_SCOPES
    from app.db.models import CatalogProfileJob
    from app.db.models_sources import IntrospectJob, SourceChange

    now = now or utcnow()
    async with SessionLocal() as session:
        rows = (await session.execute(select(SourceChange.source_id, SourceChange.detected_by).where(
            SourceChange.severity == "action", SourceChange.status == "open",
            SourceChange.detected_at >= now - timedelta(days=BACKFILL_DAYS)))).all()
        jobs: dict[tuple[str, str, str], int] = {}
        for source_id, detected in rows:
            detected = detected if isinstance(detected, dict) else {}
            job_id, kind = detected.get("job_id"), detected.get("job_kind")
            if job_id and kind in ("introspect", "profile"):
                key = (str(kind), str(job_id), source_id)
                jobs[key] = jobs.get(key, 0) + 1
        if not jobs:
            return 0
        have = set((await session.execute(select(TriggerEvent.dedupe_key).where(
            TriggerEvent.dedupe_key.in_([source_key(j) for _, j, _ in jobs])))).scalars())
        todo: list[tuple[str, str, int, datetime | None]] = []
        for (kind, job_id, source_id), n in jobs.items():
            if source_key(job_id) in have:
                continue
            job = await session.get(IntrospectJob if kind == "introspect" else CatalogProfileJob, job_id)
            if job is None or job.status in ("queued", "running"):
                continue                          # 还在进行：结束时自己会记
            if kind == "profile" and job.scope not in OBSERVING_SCOPES:
                continue
            todo.append((source_id, job_id, n, job.finished_at))
    made = 0
    for source_id, job_id, n, finished in todo:
        if await emit_source_changed(source_id, job_id, n, occurred_at=finished):
            made += 1
            logger.info("补写了任务 %s 的数据源变化事件（它结束时没记上）", job_id)
    return made


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
                "text": self.text, "sample": self.sample, "real": self.real}


def sample_fire(sample: triggers.Sample, tz: str) -> EventFire:
    """立即运行：按示例事件发起（触发链从这里起算，第 1 层）。是不是示例值记在 real 上，说法不另加后缀：界面、印章按
    sample / real 自己说「立即运行，按最近一次事件」「事件为示例值」。"""
    text = triggers.event_text(sample.event, sample.subject_name, sample.payload)
    return EventFire(event=sample.event, subject_id=sample.subject_id, subject_name=sample.subject_name,
                     values=triggers.values(sample.event, sample.payload, tz, outputs=sample.outputs),
                     text=text, sample=True, real=sample.real,
                     chain=[sample.payload["run_id"]] if sample.event == "workflow.finished" and sample.payload.get(
                         "run_id") else [])


# --------------------------------------------------------------------------
# 取（调度循环的一拍）
# --------------------------------------------------------------------------

_cleaned_at: datetime | None = None


async def dispatch_due(now: datetime | None = None) -> list[ScheduleFire]:
    """完整的一拍：补写没记上的 source.changed（每 BACKFILL_EVERY 一次，刚起的服务第一拍就补）、派新来的事件、发去抖窗口到了的
    触发、顺手清理。返回这一拍记下的触发。一个任务出错不影响别的。"""
    global _backfilled_at
    now = now or utcnow()
    if _backfilled_at is None or now - _backfilled_at >= BACKFILL_EVERY:
        _backfilled_at = now
        try:
            await backfill_source_changes(now)
        except Exception:  # noqa: BLE001 - 补写是兜底，出错不挡派发
            logger.exception("补写数据源变化的待派发事件时出错")
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
        listening = await listeners(session, events)
    routed = 0
    for ev in events:
        matched = [sid for sid, trigger in listening if triggers.matches(trigger, ev.event, ev.subject_id, ev.payload)]
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


def _listening_to(pairs: Any) -> Any:
    """触发条件监听着这几对（事件种类, 对象）的事件触发任务的 WHERE 条件：按触发条件里的事件种类和对象筛（SQLAlchemy 的通用
    JSON 访问，各库都能用），不把全部事件触发任务读出来。pairs 是 {事件种类: {对象 id…}}。"""
    from sqlalchemy import and_, false, or_

    conds = [and_(Schedule.trigger["event"].as_string() == kind,
                  Schedule.trigger[triggers.SUBJECT_KEY[kind]].as_string().in_(sorted(subjects)))
             for kind, subjects in pairs.items() if kind in triggers.SUBJECT_KEY and subjects]
    return or_(*conds) if conds else false()


async def listeners(session: Any, events: list[TriggerEvent]) -> list[tuple[str, dict[str, Any]]]:
    """这几条事件可能派给的任务：启用着的、指向工作流的事件触发任务里，触发条件的事件种类和对象对得上的 [(任务 id, 触发条件)]。
    过滤条件（应用、来源、期次……）由 triggers.matches 逐条再判。"""
    pairs: dict[str, set[str]] = {}
    for ev in events:
        pairs.setdefault(ev.event, set()).add(ev.subject_id)
    rows = (await session.execute(select(Schedule.id, Schedule.trigger, Schedule.target_kind).where(
        Schedule.trigger_kind == "event", Schedule.enabled.is_(True), _listening_to(pairs)))).all()
    return [(sid, trigger) for sid, trigger, kind in rows if (kind or "workflow") == "workflow"]


async def fire_waiting(now: datetime | None = None) -> list[ScheduleFire]:
    """有等着的事件的任务，各把到点的去抖窗口处理完。"""
    now = now or utcnow()
    async with SessionLocal() as session:
        waiting = list((await session.execute(
            select(TriggerDelivery.schedule_id).where(TriggerDelivery.status == "pending").distinct())).scalars())
    out: list[ScheduleFire] = []
    for schedule_id in waiting:
        try:
            out.extend(await _drain(schedule_id, now))
        except Exception:  # noqa: BLE001 - 一个任务出错不能拖住这一拍的其他任务
            logger.exception("事件触发的定时任务 %s 这一拍处理失败", schedule_id)
    return out


@dataclass
class _Window:
    """一个去抖窗口：同一个分窗口的键（上传表格是期次）、落在里面的几条（delivery id, 事件, 派给任务的时刻）、结束的时刻。"""

    key: str
    start: datetime
    due: datetime
    items: list[tuple[str, TriggerEvent, datetime]] = field(default_factory=list)

    @property
    def events(self) -> list[TriggerEvent]:
        return [ev for _, ev, _ in self.items]


def windows(items: list[tuple[str, TriggerEvent, datetime]], debounce_s: int) -> list[_Window]:
    """按发生时刻切成去抖窗口（items 已按发生时刻排好）：每个分窗口的键各自一串，窗口从第一条起算 debounce_s 秒；按结束时刻
    排。两个窗口结束在同一刻（不同期次同一刻启用）时后一个往后挪 1 微秒：fire_at 不撞触发记录的唯一约束，同样的输入永远切出
    同样的窗口。"""
    from app.scheduler import service

    span = timedelta(seconds=debounce_s)
    current: dict[str, _Window] = {}
    out: list[_Window] = []
    for did, ev, routed in items:
        at = service.as_utc(ev.occurred_at)
        key = triggers.window_key(ev.event, ev.payload)
        win = current.get(key)
        if win is None or at > win.due:
            win = current[key] = _Window(key, at, at + span)
            out.append(win)
        win.items.append((did, ev, routed))
    out.sort(key=lambda w: (w.due, w.key))
    for prev, cur in zip(out, out[1:]):
        if cur.due <= prev.due:
            cur.due = prev.due + timedelta(microseconds=1)
    return out


async def _drain(schedule_id: str, now: datetime) -> list[ScheduleFire]:
    """一个任务这一拍到点的窗口：已过补发宽限的合成一条跳过，其余照常发起。"""
    from app.scheduler import loop, service

    async with SessionLocal() as session:
        schedule = await session.get(Schedule, schedule_id)
        if schedule is None:
            return []
        trigger, tz = schedule.trigger if isinstance(schedule.trigger, dict) else {}, schedule.timezone
        if not schedule.enabled or schedule.trigger_kind != "event":
            # 等着的时候被停用、改成了别的触发方式：停用是「别再触发了」，等着的不再派发
            await _close(session, _pending(schedule_id), "dropped")
            return []
        rows = (await session.execute(
            select(TriggerDelivery.id, TriggerDelivery.created_at, TriggerEvent)
            .join(TriggerEvent, TriggerEvent.id == TriggerDelivery.event_id)
            .where(TriggerDelivery.schedule_id == schedule_id, TriggerDelivery.status == "pending")
            .order_by(TriggerDelivery.occurred_at, TriggerEvent.created_at).limit(DRAIN_BATCH))).all()
        # 等着的时候改了触发条件（换了对象、加了过滤）：已经不合条件的不再派发
        stale = [did for did, _, ev in rows if not triggers.matches(trigger, ev.event, ev.subject_id, ev.payload)]
        if stale:
            await _close(session, TriggerDelivery.id.in_(stale), "dropped")
        live = [(did, ev, service.as_utc(routed)) for did, routed, ev in rows if did not in set(stale)]
        due = [w for w in windows(live, triggers.debounce_of(trigger)) if w.due <= now][:MAX_WINDOWS]
        if not due:
            return []
        grace = max(int(schedule.misfire_grace_s or 0), 0)
        limit = max(loop.ON_TIME_S, grace)
        missed = [w for w in due if (now - w.due).total_seconds() > limit]
        timely = [w for w in due if w not in missed]
        outputs = {id(w): await _outputs_of(session, w.events[-1]) for w in timely}
    await _clear_of_earlier_fires(schedule_id, timely)
    out: list[ScheduleFire] = []
    if missed:
        fire = await _skip_missed(schedule_id, missed, tz, grace)
        if fire is not None:
            out.append(fire)
            await _close_window(missed, fire.id)
    for w in timely:
        fire = await service.fire_once(schedule_id, w.due, "event", event=_event_fire(w.events, tz, outputs[id(w)]))
        if fire is not None:
            out.append(fire)
            await _close_window([w], fire.id)
    return out


async def _clear_of_earlier_fires(schedule_id: str, wins: list[_Window]) -> None:
    """要发起的窗口的结束时刻（fire_at）和这个任务已经有的另一次事件触发撞在同一刻（别的期次的窗口早一拍发了，结束时刻相同）
    时往后挪 1 微秒，直到不撞：service.fire_once 按 (任务, fire_at, kind) 认「已经处理过」，撞上了就交回已有的那一条，这个
    窗口的几条事件会被记到别人的触发上、不再发起。已有的那次触发里有这个窗口的事件（两拍撞在一起处理了同一个窗口）的不挪：
    那就是同一次，交回已有的那一条是对的。"""
    from app.scheduler import service

    if not wins:
        return
    lo, hi = min(w.due for w in wins), max(w.due for w in wins) + timedelta(seconds=1)
    async with SessionLocal() as session:
        rows = (await session.execute(select(ScheduleFire.fire_at, ScheduleFire.inputs).where(
            ScheduleFire.schedule_id == schedule_id, ScheduleFire.kind == "event",
            ScheduleFire.fire_at >= lo, ScheduleFire.fire_at <= hi))).all()
    taken: dict[datetime, set[str]] = {}
    for at, inputs in rows:
        brief = (inputs or {}).get("event") if isinstance(inputs, dict) else None
        ids = brief.get("event_ids") if isinstance(brief, dict) else None
        taken.setdefault(service.as_utc(at), set()).update(str(i) for i in ids or [])
    for w in sorted(wins, key=lambda w: w.due):
        mine = {ev.id for ev in w.events}
        while w.due in taken and not taken[w.due] & mine:
            w.due += timedelta(microseconds=1)
        taken.setdefault(w.due, set()).update(mine)


def _event_fire(events: list[TriggerEvent], tz: str, outputs: dict[str, Any]) -> EventFire:
    """几条合成一次的事件：内容取最新的那一条，深度取最深的那一条加 1。"""
    latest = events[-1]
    return EventFire(
        event=latest.event, subject_id=latest.subject_id, subject_name=latest.subject_name,
        values=triggers.values(latest.event, latest.payload, tz, outputs=outputs),
        text=triggers.event_text(latest.event, latest.subject_name, latest.payload),
        depth=max(int(ev.depth or 0) for ev in events) + 1, chain=[str(x) for x in latest.chain or []],
        event_ids=[ev.id for ev in events], first_at=_iso(events[0].occurred_at), last_at=_iso(latest.occurred_at))


async def _skip_missed(schedule_id: str, missed: list[_Window], tz: str, grace: int) -> ScheduleFire | None:
    """已过补发宽限的几个窗口合成一条 skipped_misfire（fire_at 取最后一个的结束时刻）：合并了几次事件、几个窗口、时间跨度、
    合掉的期次。派给任务的时刻离事件发生已经超过宽限的（调度停过、没在派）才说「调度未运行」，否则照实说积压。"""
    from app.scheduler import service

    events = [ev for w in missed for ev in w.events]
    events.sort(key=lambda ev: service.as_utc(ev.occurred_at))
    late_routed = any((routed - service.as_utc(ev.occurred_at)).total_seconds() > grace
                      for w in missed for _, ev, routed in w.items)
    first, last = service.local_text(events[0].occurred_at, tz), service.local_text(events[-1].occurred_at, tz)
    span = first if first == last else f"{first} 至 {last}"
    count = f"{len(events)} 次事件" + (f"（{len(missed)} 个去抖窗口）" if len(missed) > 1 else "")
    if late_routed:
        why = "服务当时没有派发（调度未运行或已停止）"
    else:
        why = "事件积压，轮到派发时"
    reason = f"{span} 的 {count}：{why}，已超出补发宽限（{service.duration_text(grace)}），没有发起；积压的事件不补"
    brief = _event_fire(events, tz, {}).brief()
    brief["windows"] = len(missed)
    periods = [p for p in dict.fromkeys(str((ev.payload or {}).get("period") or "") for ev in events) if p]
    if events[-1].event == "table.version" and periods:
        brief["periods"] = periods
    return await service.record_fire(schedule_id, max(w.due for w in missed), "event", "skipped_misfire", reason,
                                     {"event": brief})


async def _close_window(wins: list[_Window], fire_id: str) -> None:
    async with SessionLocal() as session:
        await _close(session, TriggerDelivery.id.in_([did for w in wins for did, _, _ in w.items]), "fired",
                     fire_id=fire_id)


def _pending(schedule_id: str) -> Any:
    return (TriggerDelivery.schedule_id == schedule_id) & (TriggerDelivery.status == "pending")


async def _close(session: Any, cond: Any, status: str, *, fire_id: str | None = None) -> None:
    await session.execute(update(TriggerDelivery).where(cond, TriggerDelivery.status == "pending")
                          .values(status=status, fire_id=fire_id).execution_options(synchronize_session=False))
    await session.commit()


async def _outputs_of(session: Any, ev: TriggerEvent) -> dict[str, Any]:
    """workflow.finished 的出口字段：读那次运行的成果（运行已经结束、不会再变），过滤同预览、立即运行（triggers.outputs_of）。
    读不到为空。"""
    if ev.event != "workflow.finished":
        return {}
    run_id = (ev.payload or {}).get("run_id")
    run = await session.get(Run, run_id) if run_id else None
    return triggers.outputs_of(run.output if run is not None else None)


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
    """一个事件触发任务现在有几条事件在等（见 pending_for_many）。"""
    return (await pending_for_many(session, [schedule]))[schedule.id]


async def pending_for_many(session: Any, schedules: list[Schedule]) -> dict[str, dict[str, Any]]:
    """几个事件触发任务各有几条事件在等，一次查齐（定时任务列表每行都要，不再每行各查一次）：任务 id → {events, waiting, due_at}。

    events 是还没派发的（调度关着时一直留在这里）；waiting 是已经派给它、在去抖窗口里的；due_at 是最早那个窗口结束、会合成
    一次触发的时刻（没有在等的为空）。等着的时候任务改了条件、已经不合条件的（下一拍会标 dropped、不再派发）不算：窗口按剩下的
    最早一条起算，和派发时（_drain）切的窗口一样。待派发数按任务分组一条 SQL 算；窗口要看事件内容，等着的那几条一次读出来。"""
    from app.scheduler import service

    rows = [s for s in schedules if s is not None]
    out = {s.id: {"events": 0, "waiting": 0, "due_at": None} for s in rows}
    if not rows:
        return out
    trig = {s.id: s.trigger if isinstance(s.trigger, dict) else {} for s in rows}
    ids = list(out)
    counts = dict((await session.execute(select(TriggerDelivery.schedule_id, func.count()).where(
        TriggerDelivery.schedule_id.in_(ids), TriggerDelivery.status == "pending")
        .group_by(TriggerDelivery.schedule_id))).all())
    # 每个任务各读最早的 DRAIN_BATCH 条（同 _drain 一拍读的）：合起来一个上限的话，积压多的任务会把别的挤掉，它们的 due_at 为空
    ranked = (select(TriggerDelivery.id.label("did"), func.row_number().over(
        partition_by=TriggerDelivery.schedule_id,
        order_by=(TriggerDelivery.occurred_at, TriggerDelivery.created_at, TriggerDelivery.id)).label("rn"))
        .where(TriggerDelivery.schedule_id.in_([i for i in ids if counts.get(i)]), TriggerDelivery.status == "pending")
        .subquery())
    waiting = (await session.execute(
        select(TriggerDelivery.schedule_id, TriggerDelivery.id, TriggerDelivery.created_at, TriggerEvent)
        .join(TriggerEvent, TriggerEvent.id == TriggerDelivery.event_id)
        .join(ranked, ranked.c.did == TriggerDelivery.id).where(ranked.c.rn <= DRAIN_BATCH)
        .order_by(TriggerDelivery.occurred_at, TriggerEvent.created_at))).all() if counts else []
    by: dict[str, list[tuple[str, TriggerEvent, datetime]]] = {}
    stale: dict[str, int] = {}
    for sid, did, routed, ev in waiting:
        if triggers.matches(trig[sid], ev.event, ev.subject_id, ev.payload):
            by.setdefault(sid, []).append((did, ev, service.as_utc(routed)))
        else:
            stale[sid] = stale.get(sid, 0) + 1
    for sid in ids:
        out[sid]["waiting"] = max(int(counts.get(sid) or 0) - stale.get(sid, 0), 0)
        wins = windows(by.get(sid, []), triggers.debounce_of(trig[sid]))
        if wins:
            out[sid]["due_at"] = min(w.due for w in wins).isoformat()
    pairs: dict[str, set[str]] = {}
    for sid in ids:
        if subject := triggers.subject_of(trig[sid]):
            pairs.setdefault(str(trig[sid].get("event") or ""), set()).add(subject)
    if pairs:
        from sqlalchemy import and_, or_

        cond = or_(*[and_(TriggerEvent.event == kind, TriggerEvent.subject_id.in_(sorted(subs)))
                     for kind, subs in pairs.items()])
        # 每个（事件种类, 对象）各读前 ROUTE_BATCH 条（同改前每个任务各读的那么多）：合起来一个上限会把别的对象挤掉、少算
        ranked = (select(TriggerEvent.id.label("eid"), func.row_number().over(
            partition_by=(TriggerEvent.event, TriggerEvent.subject_id),
            order_by=(TriggerEvent.created_at, TriggerEvent.id)).label("rn"))
            .where(TriggerEvent.status == "pending", cond).subquery())
        unrouted = (await session.execute(select(TriggerEvent.event, TriggerEvent.subject_id, TriggerEvent.payload)
                                          .join(ranked, ranked.c.eid == TriggerEvent.id)
                                          .where(ranked.c.rn <= ROUTE_BATCH))).all()
        for kind, subject, payload in unrouted:
            for sid in ids:
                if triggers.matches(trig[sid], kind, subject, payload):
                    out[sid]["events"] += 1
    return out
