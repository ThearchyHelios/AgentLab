"""数据源的定时维护：探查 → 剖析需要的表 → 取值观察（docs/design/next-round/03-数据接入与目录维护.md B4，目录-D5）。

**为什么要有它。** 以前只能手动探查、手动剖析，目录只在点按钮那一刻是对的；结构变化、取值漂移要靠定期观察才看得到。

**定时只有一套**（总方案 X4）：维护就是一条 Schedule，target_kind=source_maintenance、target_id=数据源 id。认领、错过的只补
最近一次、积压只记一条、重叠跳过、触发记录、通知都是 scheduler 那一套（scheduler/service.py 按目标种类导入本模块：fire 到点
发起一次、check_target 建任务时核对目标），不另建表、不另写 cron。按数据源开、缺省关（没有这条定时就不维护），只在设定的
时段跑（cron 自己定）。

**做哪几步**存在数据源的 options.catalog_maintenance（{steps: [...], updated_by, updated_at}，由 PUT
/api/datasources/{id}/maintenance 维护，数据源表单不显示、不提交它）：introspect 探查结构、profile_needed 剖析需要的表、
observe 取值观察，可多选，没设过是三步都做。

**到点**（fire，持着 scheduler 的 launch_lock）：按顺序建探查任务 → 剖析任务（范围 needed / observe / maintenance，按选的
步骤），任务上记 trigger=schedule 和 fire_at，靠 SourceLanes 串行：剖析排在同一次触发的探查后面（等探查进了同源队才建剖析
任务），开始时按那时的表结构和目录挑表（挑出 0 张也照建，任务以 0 张表结束、提醒里写明为什么）。建任务时就判断得出会是 0 张
的（这次没有探查、也没排在起草后面，开始时表结构和目录都不会变——比如有数据目录的表都在 12 小时内观察过），原因另记进触发
记录的 inputs.skipped 和 reason；同一次触发里有探查、或者排在起草后面时说不准（探查完、起草完可能就有了），保持现状，只在
任务的提醒里写。预算、止损照剖析设置。
照实记没执行的：数据源不存在、已停用、是上传表格或记录表时整次不执行（failed_to_start）；剖析没开时剖析、观察两步不执行、
探查照做；上一次的维护（或别的探查、剖析）还没做完时这次跳过（skipped_overlap）。每次触发记下建了哪几个任务、哪几步没执行、
为什么（触发记录的 inputs：{steps, jobs, skipped}）。

AGENTLAB_SCHEDULER 关着时调度循环不起，到点不会触发（拷来的数据目录不替原库查业务库）；「立即运行」是人点的，照常执行。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

#: options 里维护设置的键
MAINTENANCE_OPTION = "catalog_maintenance"
STEPS = ("introspect", "profile_needed", "observe")
STEP_LABEL = {"introspect": "探查结构", "profile_needed": "剖析需要的表", "observe": "取值观察"}
DEFAULT_STEPS = STEPS
#: 等同一次触发的探查任务进同源队最多等几秒（进了队，剖析任务才排得到它后面）
LANE_WAIT_S = 5.0

#: 先记下这次触发、还没建完任务时的说法：进程在这中间没了，留下的记录照实说没有执行
INTERRUPTED = "服务在建维护任务前停止，这次维护没有执行"
SOURCE_GONE = "数据源不存在，可能已被删除"
SOURCE_DISABLED = "数据源「{name}」已停用，这次维护没有执行"
NOT_MAINTAINED = "上传的表格、记录表的结构随版本或表单走，不需要定时维护"
NO_STEPS = "没有选要做的维护步骤（探查结构、剖析需要的表、取值观察至少选一步）"
BUSY = "数据源「{name}」上一次的维护（或别的探查、剖析）还没做完：{what}，这次触发已跳过"
NOT_RUN = "{step}没有执行：{why}"


def steps_of(options: Mapping[str, Any] | None) -> list[str]:
    """这个源定时维护要做哪几步（按 STEPS 的顺序）；没设过是三步都做。"""
    raw = (options or {}).get(MAINTENANCE_OPTION)
    steps = raw.get("steps") if isinstance(raw, Mapping) else None
    if not isinstance(steps, list):
        return list(DEFAULT_STEPS)
    picked = {s for s in steps if s in STEPS}
    return [s for s in STEPS if s in picked]


def steps_problem(raw: Any) -> str | None:
    """PUT /maintenance 交来的步骤合不合规：不合规返回一句中文。"""
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        return "维护步骤要是一组文字：introspect（探查结构）、profile_needed（剖析需要的表）、observe（取值观察）"
    unknown = [s for s in raw if s not in STEPS]
    if unknown:
        return f"不认识的维护步骤：{'、'.join(unknown)}。可选：{'、'.join(f'{k}（{v}）' for k, v in STEP_LABEL.items())}"
    if not raw:
        return NO_STEPS
    return None


def with_steps(options: Mapping[str, Any] | None, steps: list[str], *, actor: str | None) -> dict[str, Any]:
    """options 换上新的维护步骤（不改别的项）。"""
    ordered = [s for s in STEPS if s in set(steps)]
    return {**dict(options or {}),
            MAINTENANCE_OPTION: {"steps": ordered, "updated_by": actor, "updated_at": utcnow().isoformat()}}


def _origin_problem(row: Any) -> str | None:
    return NOT_MAINTAINED if (getattr(row, "origin", None) or "manual") in ("upload", "records") else None


async def check_target(session: Any, target_id: str) -> str | None:
    """建定时任务、改目标时核对：数据源要在、不是上传表格或记录表、选了至少一步。停用了、没开剖析不拦（到点照实记没执行）。"""
    from app.db.models import DataSource

    row = await session.get(DataSource, target_id)
    if row is None:
        return SOURCE_GONE
    if problem := _origin_problem(row):
        return problem
    if not steps_of(row.options):
        return NO_STEPS
    return None


async def _busy(session: Any, source_id: str) -> str | None:
    """同源有排队或进行中的探查、剖析任务：说明是哪一种。"""
    from app.data import catalog_profile_jobs, introspect_jobs

    if await introspect_jobs.active_job(session, source_id) is not None:
        return "正在探查结构"
    if await catalog_profile_jobs.active_job(session, source_id) is not None:
        return "正在剖析"
    return None


async def _entered(source_id: str, job_id: str) -> None:
    """等这个探查任务进了同源队（或者已经不在跑了）：剖析任务要排在它后面。最多等 LANE_WAIT_S 秒。"""
    from app.core import job_runtime as runtime
    from app.data import introspect_jobs

    loop = asyncio.get_running_loop()
    deadline = loop.time() + LANE_WAIT_S
    while loop.time() < deadline:
        if runtime.lanes.ahead(source_id, job_id) is not None or not introspect_jobs.manager.running(job_id):
            return
        await asyncio.sleep(0.01)
    logger.warning("定时维护：探查任务 %s 过了 %.0f 秒还没进同源队，剖析任务照建", job_id, LANE_WAIT_S)


async def _settle_fire(fire_id: str, status: str, reason: str | None, inputs: dict[str, Any]) -> Any:
    from app.db.models import ScheduleFire

    async with SessionLocal() as session:
        await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id)
                              .values(status=status, reason=reason, inputs=inputs))
        await session.commit()
        return await session.get(ScheduleFire, fire_id)


async def fire(*, schedule_id: str, fire_at: datetime, kind: str, actor: str | None, target_id: str, name: str,
               tz: str, cron_expr: str) -> Any:
    """到点维护一次（持着 launch_lock 调用）：建探查任务、剖析任务并在后台开始，记下触发。同一次触发已经记过就交回那一条。"""
    from app.db.models import DataSource, ScheduleFire
    from app.scheduler import service

    async with SessionLocal() as session:
        existing = (await session.execute(select(ScheduleFire).where(
            ScheduleFire.schedule_id == schedule_id, ScheduleFire.fire_at == fire_at,
            ScheduleFire.kind == kind))).scalar_one_or_none()
        if existing is not None:
            return existing
        row = await session.get(DataSource, target_id)
        if row is None:
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", SOURCE_GONE)
        if problem := _origin_problem(row):
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", problem)
        if row.enabled is False:
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start",
                                             SOURCE_DISABLED.format(name=row.name))
        steps = steps_of(row.options)
        if not steps:
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", NO_STEPS)
        if what := await _busy(session, row.id):
            return await service.record_fire(schedule_id, fire_at, kind, "skipped_overlap",
                                             BUSY.format(name=row.name, what=what), {"steps": steps})
    placeholder = await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", INTERRUPTED,
                                            {"steps": steps})
    if placeholder is None:
        return None
    try:
        jobs, skipped = await _start(target_id, steps, actor=actor or service.actor_label(name),
                                     fire_at=fire_at.isoformat(), schedule_id=schedule_id)
    except Exception:  # noqa: BLE001 - 建任务出意外不能让调度循环停下：照实记下，详细信息进日志
        logger.exception("定时维护（%s）建任务时出错", schedule_id)
        return await _settle_fire(placeholder.id, "failed_to_start", "建维护任务时出错，详细信息见服务日志",
                                  {"steps": steps})
    reason = "；".join(NOT_RUN.format(step=STEP_LABEL[s["step"]], why=s["reason"]) for s in skipped) or None
    return await _settle_fire(placeholder.id, "started" if jobs else "failed_to_start", reason,
                              {"steps": steps, "jobs": jobs, "skipped": skipped})


async def _start(source_id: str, steps: list[str], *, actor: str, fire_at: str,
                 schedule_id: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """按顺序建任务并开始：探查（introspect）→ 剖析 / 观察（一个剖析任务：范围按选的步骤）。返回 (建了的任务, 没执行的步骤
    和原因)。"""
    from app.data import catalog_profile, catalog_profile_jobs, introspect_jobs, table_versions
    from app.db.models import DataSource

    jobs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    introspect_id: str | None = None
    if "introspect" in steps:
        async with SessionLocal() as session:
            row = await session.get(DataSource, source_id)
            try:
                job, n = await introspect_jobs.prepare(session, row, actor=actor, trigger="schedule", fire_at=fire_at,
                                                       schedule_id=schedule_id)
            except (introspect_jobs.IntrospectRefused, introspect_jobs.IntrospectBusy, introspect_jobs.IntrospectFailed,
                    introspect_jobs.ScopeTooLarge) as e:
                skipped.append({"step": "introspect", "reason": str(e)})
            else:
                introspect_jobs.manager.start(job.id)
                introspect_id = job.id
                jobs.append({"step": "introspect", "kind": "introspect", "id": job.id, "tables": n})
    wanted = [s for s in steps if s in ("profile_needed", "observe")]
    if not wanted:
        return jobs, skipped
    if introspect_id is not None:
        await _entered(source_id, introspect_id)
    scope = "maintenance" if len(wanted) == 2 else ("needed" if wanted == ["profile_needed"] else "observe")
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        try:
            settings = catalog_profile.ensure_enabled(row)
        except catalog_profile.ProfileDisabled as e:
            skipped += [{"step": s, "reason": str(e)} for s in wanted]
            return jobs, skipped
        try:
            view = await table_versions.resolve_source(session, row)
            job, tasks = await catalog_profile_jobs.create_job(
                session, row, view, tables=None, scope=scope, settings=settings, actor=actor, trigger="schedule",
                fire_at=fire_at, schedule_id=schedule_id, allow_empty=True)
        except (catalog_profile_jobs.ProfileBusy, catalog_profile_jobs.ProfileNothing,
                table_versions.SnapshotMissing) as e:
            skipped += [{"step": s, "reason": str(e)} for s in wanted]
            return jobs, skipped
        if not tasks and introspect_id is None and (why := await catalog_profile_jobs.nothing_reason(
                session, row, view, scope)):
            # 建任务时就挑不出表、这次又没有探查（开始时表结构不会变）、也没排在起草后面（目录不会变）：任务会以 0 张表
            # 结束，原因照实记进触发记录，不只在剖析任务的提醒里。任务照建（触发照旧是 started）
            skipped += [{"step": s, "reason": why} for s in wanted]
        catalog_profile_jobs.manager.start(job.id)
        jobs.append({"step": "+".join(wanted), "kind": "profile", "id": job.id, "scope": scope, "tables": len(tasks)})
    return jobs, skipped


async def schedules_for(session: Any, source_id: str) -> list[dict[str, Any]]:
    """指向这个数据源的维护定时（数据源设置里「定时维护」的快捷入口读它，第 4 波）：[{id, name, cron, timezone, enabled,
    next_run_at}]。"""
    from app.db.models import Schedule

    rows = (await session.execute(select(Schedule).where(
        Schedule.target_kind == "source_maintenance", Schedule.target_id == source_id)
        .order_by(Schedule.created_at))).scalars()
    return [{"id": r.id, "name": r.name, "cron": r.cron, "timezone": r.timezone, "enabled": r.enabled,
             "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None} for r in rows]


__all__ = ["DEFAULT_STEPS", "MAINTENANCE_OPTION", "STEPS", "STEP_LABEL", "check_target", "fire", "schedules_for",
           "steps_of", "steps_problem", "with_steps"]
