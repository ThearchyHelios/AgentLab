"""数据源的后台探查任务（POST /api/datasources/{id}/introspect，docs/design/next-round/03-数据接入与目录维护.md A1）。

以前探查是一次同步请求、结果整份替换 schema_cache、不排队：MySQL 一张表一次往返，范围一大同步请求就超时；和起草、剖析同时
进行时，目录和结构会对不上。现在：

- **建任务**（prepare，在请求里做完）：先刷新对象索引（data/source_objects：一条字典查询，查不了退回 Inspector；连接本身
  失败抛 IntrospectFailed，失败照实记进 schema_cache，表结构照旧留着；升级前探查过的源第一次建索引时接上老缓存里的表结构，
  见 source_objects.legacy_seed；一个对象都没有时记下这台服务器上能选的 schema），再按探查范围的规则圈表；规则命中超过
  上限抛 ScopeTooLarge（说出命中多少张），不建任务、不截断。任务逐表一行（按名字排）。同一个源同时只有一个排队或进行中的
  探查任务（IntrospectBusy；部分唯一索引兜底）。
- **在后台反射**（manager.start）：照 core/job_runtime 的生命周期，和同源的起草、剖析排同一条队（introspect），轮到了才开始。
  开始时才把圈好的范围写进对象索引（建任务之后才加进范围的表照旧留着）。按批（source_objects.REFLECT_BATCH 张一批）只读
  反射，每批开始前重读数据源（停用了就停下），读完的表逐表记 ok / failed、写进对象索引（指纹变了结构版本加 1）。全部读完
  由 rebuild_schema_cache 物化 schema_cache、盖上同步时间。
- **停止**（cancel）：排队的直接 cancelled；进行中的停在当前这一批之后（库那边按单条时限截止），没读的表 cancelled；读完的
  照常物化进 schema_cache（范围内已反射的行），任务 cancelled。
- **服务重启**（startup）：排队、进行中的标 interrupted，没读的表 cancelled、error「服务重启，探查中断」，同样把已读完的物化
  进去；不自动续跑：复制出来的数据目录启动时不能自己去查业务库，界面上点「探查结构」重来。
- **保留**（prune）：每个源保留最近 KEEP_JOBS 个结束了的探查任务。

任务最后的状态：有表读不到是 partial，否则 succeeded；整体出错（数据源没了、连不上）是 failed。

**结构变化**（第 3 波，next-round 03 B1）：探查顺带把前后差异记进 source_changes（data/source_changes.py），每条带影响面
（data/catalog_impact.structure_impact）。刷新对象索引时记新出现的表、真删除的表（第一次建索引只建基线）；开始执行时记
离开范围的、有数据目录的表，以及范围规则和上一次探查不同（scope_changed）；逐批读完时拿上一次的结构和这一次的比
（data/schema_diff.diff_table），在范围里却读不到结构的记 table_reflect_failed（不算删除）。升级后第一次读到（结构版本 0）
只建基线。记变化出错只记日志，不挡探查。任务上记 trigger（manual 人点的 / schedule 定时维护）和定时的 fire_at。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import job_runtime as runtime
from app.core.errors import describe_exception, first_line
from app.core.job_runtime import ACTIVE, FINISHED, OPEN, STOP_GRACE_S
from app.data import source_objects as so
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import DataSource
from app.db.models_sources import IntrospectJob, IntrospectTask, SourceObject

logger = logging.getLogger(__name__)

#: 探查任务占同源队（叫法登记在运行时里）：和起草、剖析、导入、同步起草串行
runtime.register_lane_kind("introspect", "探查")

#: 每个源保留几个结束了的探查任务
KEEP_JOBS = 20
#: 任务里列出的读不到的表最多几张（全部的在逐表的行里）
FAILURES_SHOWN = 50

INTERRUPTED_ERROR = "服务重启，探查中断"
STOPPED_ERROR = "探查已停止"
SOURCE_GONE = "数据源不存在，可能已被删除"
SOURCE_DISABLED = "数据源「{name}」已停用，探查已停止"
JOB_ERROR = "探查任务出错：{line}"
CONNECT_ERROR = "读表结构时无法连接数据库：{line}"
UPLOAD_REFUSED = "上传的表格无需重新探查，重新上传即可更新"
RECORDS_REFUSED = "记录表的结构由应用的表单维护，无需重新探查"


class IntrospectBusy(Exception):
    """这个数据源已经有排队或进行中的探查任务（message 写明进行到第几张）。"""


class IntrospectFinished(Exception):
    """要停止的探查任务已经结束了。"""


class ScopeTooLarge(Exception):
    """规则命中超过上限：不建任务。plan 是圈出的范围（接口回给界面命中数）。"""

    def __init__(self, plan: so.ScopePlan) -> None:
        super().__init__(so.too_large_message(plan))
        self.plan = plan


class IntrospectFailed(Exception):
    """对象清单都拿不到（连不上、schema 不存在）。cause 是原来的异常，接口按它翻译成一句话和下一步。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(first_line(cause) or describe_exception(cause))
        self.cause = cause


class IntrospectRefused(Exception):
    """这种源不探查（上传表格、记录表）。"""


# ==========================================================================
# 读任务
# ==========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def active_job(session: AsyncSession, source_id: str) -> IntrospectJob | None:
    return (await session.execute(
        select(IntrospectJob).where(IntrospectJob.source_id == source_id, IntrospectJob.status.in_(ACTIVE))
        .order_by(IntrospectJob.created_at.desc()).limit(1))).scalar_one_or_none()


async def latest_job(session: AsyncSession, source_id: str) -> IntrospectJob | None:
    return (await session.execute(
        select(IntrospectJob).where(IntrospectJob.source_id == source_id)
        .order_by(IntrospectJob.created_at.desc()).limit(1))).scalar_one_or_none()


async def load(session: AsyncSession, source_id: str, job_id: str) -> IntrospectJob | None:
    job = await session.get(IntrospectJob, job_id)
    return job if job is not None and job.source_id == source_id else None


async def _counts(session: AsyncSession, job_id: str) -> dict[str, int]:
    counts = dict.fromkeys(("pending", "running", "ok", "failed", "cancelled"), 0)
    for status, n in (await session.execute(
            select(IntrospectTask.status, func.count()).where(IntrospectTask.job_id == job_id)
            .group_by(IntrospectTask.status))).all():
        counts[status] = counts.get(status, 0) + n
    return counts


async def waiting_for(session: AsyncSession, job: IntrospectJob) -> str | None:
    """排队中的探查任务在等什么：同源队里排在它前面的第一件事（draft 起草、profile 剖析、import 导入目录、sync_draft 同步
    起草）；任务刚建、后台还没排进队时看库：同源有比它先建的、排队或进行中的起草、剖析任务。"""
    if job.status != "queued":
        return None
    ahead = runtime.lanes.ahead(job.source_id, job.id)
    if ahead is not None:
        return ahead[0] if ahead else None
    from app.db.models import CatalogDraftJob, CatalogProfileJob

    for kind, model in (("draft", CatalogDraftJob), ("profile", CatalogProfileJob)):
        earlier = (await session.execute(
            select(model.id).where(model.source_id == job.source_id, model.status.in_(ACTIVE),
                                   model.created_at <= job.created_at).limit(1))).first()
        if earlier is not None:
            return kind
    return None


async def job_out(session: AsyncSession, job: IntrospectJob | None) -> dict[str, Any] | None:
    """接口返回的任务（IntrospectJob）：计数按逐表的算，读不到的表列前 FAILURES_SHOWN 张和原因，正在读的那张。"""
    if job is None:
        return None
    counts = await _counts(session, job.id)
    failures = [{"name": r.table_name, "error": r.error} for r in (await session.execute(
        select(IntrospectTask.table_name, IntrospectTask.error).where(
            IntrospectTask.job_id == job.id, IntrospectTask.status == "failed")
        .order_by(IntrospectTask.seq).limit(FAILURES_SHOWN))).all()]
    current = (await session.execute(
        select(IntrospectTask.table_name).where(IntrospectTask.job_id == job.id, IntrospectTask.status == "running")
        .order_by(IntrospectTask.seq).limit(1))).scalar_one_or_none()
    total = sum(counts.values())
    return {
        "id": job.id, "source_id": job.source_id, "status": job.status, "trigger": job.trigger,
        "schema": job.schema_name, "created_by": job.created_by, "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at), "finished_at": _iso(job.finished_at),
        "total": total, "done": counts["ok"] + counts["failed"] + counts["cancelled"], "counts": counts,
        "current": current, "failures": failures, "error": job.error, "scope": dict(job.scope or {}),
        "waiting_for": await waiting_for(session, job),
    }


async def job_brief(session: AsyncSession, job: IntrospectJob | None) -> dict[str, Any] | None:
    """数据源卡片上的那一行（DataSourceOut.introspect_job）：{id, status, total, done, failed, waiting_for, finished_at,
    error}。"""
    if job is None:
        return None
    counts = await _counts(session, job.id)
    return {"id": job.id, "status": job.status, "total": sum(counts.values()),
            "done": counts["ok"] + counts["failed"] + counts["cancelled"], "failed": counts["failed"],
            "cancelled": counts["cancelled"], "waiting_for": await waiting_for(session, job),
            "finished_at": _iso(job.finished_at), "error": job.error}


async def latest_briefs(session: AsyncSession, source_ids: list[str]) -> dict[str, dict[str, Any]]:
    """各源最近一个探查任务的那一行（数据源列表一次取齐：每个源一条查询，源不多）。"""
    out: dict[str, dict[str, Any]] = {}
    for sid in source_ids:
        brief = await job_brief(session, await latest_job(session, sid))
        if brief is not None:
            out[sid] = brief
    return out


#: 撞上探查任务时说法的最后一句缺省是开始新的探查；导入目录、同步起草撞上时各说各的（catalog_jobs._lane_busy）
THEN_INTROSPECT = "再开始新的探查"


def busy_message(row: Any, brief: dict[str, Any] | None, *, then: str = THEN_INTROSPECT) -> str:
    name = getattr(row, "name", "") or ""
    if brief is None:
        return f"数据源「{name}」正在探查结构，请等它结束（或先停止它）{then}"
    if brief["status"] == "queued":
        return f"数据源「{name}」已有一个排队中的探查任务（共 {brief['total']:,} 张表），请等它结束（或先停止它）{then}"
    return (f"数据源「{name}」正在探查结构：第 {min(brief['done'] + 1, brief['total']):,} / {brief['total']:,} 张表，"
            f"请等它结束（或先停止它）{then}")


async def ensure_free(session: AsyncSession, row: Any) -> None:
    job = await active_job(session, row.id)
    if job is not None:
        raise IntrospectBusy(busy_message(row, await job_brief(session, job)))


# ==========================================================================
# 预览、建任务
# ==========================================================================


def _refuse(row: Any) -> None:
    origin = getattr(row, "origin", None) or "manual"
    if origin == "upload":
        raise IntrospectRefused(UPLOAD_REFUSED)
    if origin == "records":
        raise IntrospectRefused(RECORDS_REFUSED)


async def _other_schemas(row: Any) -> list[str]:
    """这台服务器上能选的 schema（introspect.list_schemas：真有对象的在前）；拿不到就不给。"""
    from app.data import introspect

    try:
        return list(await introspect.list_schemas(row))
    except Exception:  # noqa: BLE001 - 候选也拿不到就不给
        return []


async def _failure_marker(row: Any, cause: BaseException) -> dict[str, Any]:
    """对象清单拿不到时记进 schema_cache 的失败标记：原因（驱动的原话第一行）和这台服务器上能选的 schema。"""
    return {"failed": True, "error": first_line(cause) or describe_exception(cause),
            "available_schemas": await _other_schemas(row)}


async def _record_failure(session: AsyncSession, row: Any, cause: BaseException) -> None:
    """失败照实记进 schema_cache：有对象索引的按行物化、带上失败标记；没有的（第一次探查就连不上）只在原来的缓存上加
    标记。表结构照旧留着。不盖同步时间。"""
    marker = await _failure_marker(row, cause)
    if await so.has_index(session, row.id, so.index_schema(row)):
        await so.rebuild_schema_cache(session, row, failure=marker)
    else:
        cache = dict(row.schema_cache or {})
        cache.setdefault("tables", {})
        cache.update(marker)
        cache["schema"] = so.target_schema(row)
        row.schema_cache = cache
    await session.commit()


async def preview(session: AsyncSession, row: Any, *, schema: str | None = None,
                  rules: so.ScopeRules | None = None) -> dict[str, Any]:
    """只看不存的探查（dry_run）：拿对象清单、按规则数命中。schema 是数据源配置的那个（或者没给）时刷新存着的对象索引
    （只刷新索引，不反射、不动 schema_cache）；是别的 schema 时只看不存。连不上抛 IntrospectFailed。"""
    _refuse(row)
    configured = so.index_schema(row)
    other = bool(schema) and schema != configured
    target = schema if other else so.target_schema(row)
    try:
        result = await so.list_objects(row, target)
    except Exception as e:  # noqa: BLE001
        raise IntrospectFailed(e) from e
    rules = rules or so.ScopeRules.from_options(row.options)
    out: dict[str, Any] = {"schema": target or "", "index_total": result.total, "index_method": result.method,
                           "index_capped": result.capped, "index_note": result.note}
    if other:
        live = [(o.name, o.kind) for o in result.objects]
        plan = so.plan_scope(live, rules, pinned=set(), on_demand=set())
        out.update(stored=False, objects=[o.name for o in result.objects], scope=plan.summary())
        return out
    async with so.source_lock(row.id):
        stats = await so.refresh_index(session, row.id, configured, result, legacy=row.schema_cache)
        await _record_index_changes(session, row, stats, _detected(None, "manual", None))
        await session.commit()
    plan = await so.plan_for(session, row, rules)
    out.update(stored=True, objects=[o.name for o in result.objects], scope=plan.summary(), added=stats.added,
               removed=stats.removed)
    return out


async def prepare(session: AsyncSession, row: Any, *, actor: str | None, trigger: str = "manual",
                  fire_at: str | None = None, schedule_id: str | None = None) -> tuple[IntrospectJob, int]:
    """建一个排队的探查任务（还没开始，调用方接着 manager.start）。返回 (任务, 几张表)。trigger 是 manual（人点的）或
    schedule（定时维护：fire_at、schedule_id 记进任务的 scope，留痕这一次是哪一次触发建的）。

    上传表格、记录表抛 IntrospectRefused；同源已有排队或进行中的探查任务抛 IntrospectBusy；对象清单拿不到抛
    IntrospectFailed（失败照实记进 schema_cache）；规则命中超过上限抛 ScopeTooLarge（对象索引照样刷新了）。

    这里只刷新对象索引、圈表、核上限、建任务；范围写进对象索引（离开范围的表从 schema_cache 里拿掉）等轮到这个任务、
    开始执行时再做（_IntrospectRun._apply）：排在同源的剖析后面时，剖析看到的表结构不该在它做到一半时变。"""
    _refuse(row)
    name, source_id = row.name, row.id
    await ensure_free(session, row)
    schema = so.index_schema(row)
    try:
        result = await so.list_objects(row, so.target_schema(row))
    except Exception as e:  # noqa: BLE001
        await _record_failure(session, row, e)
        raise IntrospectFailed(e) from e
    # 一个对象都没有：PostgreSQL、Oracle 的 schema 名写错（大小写、拼写）时字典查询不报错、只回 0 行，最常见的就是这种。
    # 和以前同步探查一样把这台服务器上能选的 schema 记下来（物化时写进 schema_cache 的 available_schemas）：卡片上「该
    # 服务器上还有……」、助手摘要里「数据可能在这些 schema」才给得出，不显示成「已同步、0 张表」了事
    candidates = await _other_schemas(row) if not result.objects else []
    async with so.source_lock(source_id):
        stats = await so.refresh_index(session, source_id, schema, result, legacy=row.schema_cache)
        await session.flush()
        rules = so.ScopeRules.from_options(row.options)
        plan = await so.plan_for(session, row, rules)
        if plan.over:
            await _record_index_changes(session, row, stats, _detected(None, trigger, actor, fire_at))
            await session.commit()
            raise ScopeTooLarge(plan)
        summary = {**plan.summary(rules), "index_method": stats.method, "index_capped": stats.capped,
                   "index_note": stats.note, "added": stats.added, "removed": stats.removed,
                   "adopted": stats.adopted}
        if candidates:
            summary["available_schemas"] = candidates
        if fire_at:
            summary["fire_at"] = fire_at
        if schedule_id:
            summary["schedule_id"] = schedule_id
        job = IntrospectJob(id=new_id(), source_id=source_id, status="queued", trigger=trigger, schema_name=schema,
                            scope=summary, created_by=actor)
        session.add(job)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            raise IntrospectBusy(busy_message(SimpleNamespace(name=name), None)) from e
        session.add_all([IntrospectTask(job_id=job.id, seq=i, table_name=table, reason=reason)
                         for i, (table, reason) in enumerate(plan.in_scope.items())])
        await _record_index_changes(session, row, stats, _detected(job.id, trigger, actor, fire_at))
        await session.commit()
    try:
        await prune(session, source_id)
    except Exception:  # noqa: BLE001 - 清旧任务失败不影响这次探查
        logger.exception("清理数据源 %s 的旧探查任务时出错", source_id)
        await session.rollback()
    return job, len(plan.in_scope)


async def prune(session: AsyncSession, source_id: str, *, keep: int = KEEP_JOBS) -> int:
    finished = list((await session.execute(
        select(IntrospectJob.id).where(IntrospectJob.source_id == source_id, IntrospectJob.status.in_(FINISHED))
        .order_by(IntrospectJob.created_at.desc(), IntrospectJob.id.desc()))).scalars())
    victims = finished[keep:]
    if not victims:
        return 0
    await session.execute(delete(IntrospectTask).where(IntrospectTask.job_id.in_(victims)))
    await session.execute(delete(IntrospectJob).where(IntrospectJob.id.in_(victims)))
    await session.commit()
    return len(victims)


# ==========================================================================
# 结构变化（next-round 03 B1）
# ==========================================================================

#: 一次刷新对象索引最多记几条「新出现的表」（几千个按月新建的分区表这类：照实记前面的，其余只进日志）
ADDED_RECORDED = 200


def _detected(job_id: str | None, trigger: str, actor: str | None, fire_at: str | None = None,
              task_seq: int | None = None) -> Any:
    from app.data.source_changes import Detected

    return Detected(job_kind="introspect", job_id=job_id, task_seq=task_seq, trigger=trigger or "manual",
                    actor=actor, fire_at=fire_at)


async def _record_index_changes(session: AsyncSession, row: Any, stats: so.RefreshStats, detected: Any) -> None:
    """刷新对象索引得出的表级变化（不提交）：新出现的表（table_added；这个 schema 第一次建索引时全是新的，只建基线）、
    真删除的表（table_removed，带影响面：目录里这张表的项、别的表指向它的关联关系、写死它的已发布模板）。出错只记日志。"""
    if not stats.removed_names and not (stats.added_names and not stats.first_build):
        return
    from app.data import catalog_impact
    from app.data import schema_diff as sd
    from app.data import source_changes as sc

    try:
        now = utcnow().isoformat()
        changes: list[sc.NewChange] = []
        added = [] if stats.first_build else list(stats.added_names)
        if len(added) > ADDED_RECORDED:
            logger.warning("数据源 %s 这次新出现 %d 个对象，变化记录只记前 %d 个", row.name, len(added), ADDED_RECORDED)
        for name in added[:ADDED_RECORDED]:
            changes.append(sc.NewChange(kind=sd.TABLE_ADDED, table=name, severity="info", key=now))
        if stats.removed_names:
            schema = so.index_schema(row)
            gone = {r.name: r for r in (await session.execute(select(SourceObject).where(
                SourceObject.source_id == row.id, SourceObject.schema_name == schema,
                SourceObject.name.in_(stats.removed_names)))).scalars()}
            tables_after = dict((row.schema_cache or {}).get("tables") or {})
            specs = await catalog_impact.published_specs(session)
            for name in stats.removed_names:
                obj = gone.get(name)
                version = int(obj.struct_version or 0) if obj is not None else 0
                meta = obj.meta if obj is not None and isinstance(obj.meta, dict) else None
                [(affects, severity)] = await catalog_impact.structure_impact(
                    session, row, name, [sd.Diff(sd.TABLE_REMOVED)], before_meta=meta,
                    tables_after={k: v for k, v in tables_after.items() if k != name}, specs=specs)
                changes.append(sc.NewChange(
                    kind=sd.TABLE_REMOVED, table=name, severity=severity, affects=affects,
                    before={"kind": obj.kind if obj is not None else "table"}, struct_version_before=version or None,
                    key=(obj.removed_at.isoformat() if obj is not None and obj.removed_at else now)))
        await sc.record_all(session, row, changes, detected)
    except Exception:  # noqa: BLE001 - 记变化出错不挡探查
        logger.exception("数据源 %s 记录表级的结构变化时出错", getattr(row, "name", "?"))


async def _record_scope_changes(session: AsyncSession, row: Any, job: IntrospectJob, leaving: list[str],
                                entering: int) -> None:
    """开始执行、把范围写进对象索引时（不提交）：有数据目录的表离开了范围（table_left_scope，不算删除），和范围规则跟上一次
    探查不同（scope_changed：前后的规则、进出各几张）。出错只记日志。"""
    from app.data import catalog
    from app.data import schema_diff as sd
    from app.data import source_changes as sc

    try:
        detected = _detected(job.id, job.trigger, job.created_by, (job.scope or {}).get("fire_at"))
        changes: list[sc.NewChange] = []
        if leaving:
            noted = set((await catalog.read_catalog(session, row.id)).keys())
            for name in leaving:
                if name in noted:
                    changes.append(sc.NewChange(kind=sd.TABLE_LEFT_SCOPE, table=name, severity="info", key=job.id))
        previous = (await session.execute(
            select(IntrospectJob).where(IntrospectJob.source_id == job.source_id, IntrospectJob.id != job.id,
                                        IntrospectJob.started_at.is_not(None),
                                        IntrospectJob.created_at <= job.created_at)
            .order_by(IntrospectJob.created_at.desc()).limit(1))).scalar_one_or_none()
        before = (previous.scope or {}).get("rules") if previous is not None else None
        after = (job.scope or {}).get("rules")
        if isinstance(before, dict) and isinstance(after, dict) and before != after:
            changes.append(sc.NewChange(kind=sd.SCOPE, table="", severity="info", before={"rules": before},
                                        after={"rules": after, "entered": entering, "left": len(leaving)}, key=job.id))
        await sc.record_all(session, row, changes, detected)
    except Exception:  # noqa: BLE001
        logger.exception("数据源 %s 记录探查范围的变化时出错", getattr(row, "name", "?"))


# ==========================================================================
# 收尾、停止、启动时的整理
# ==========================================================================


def _index_stats(job: IntrospectJob | None) -> so.RefreshStats | None:
    """建任务时刷新对象索引的结果（记在任务的 scope 里），物化时写进 schema_cache 的 scope 摘要。"""
    scope = (job.scope or {}) if job is not None else {}
    if "index_method" not in scope:
        return None
    return so.RefreshStats(total=int(scope.get("index_total") or 0), added=int(scope.get("added") or 0),
                           removed=int(scope.get("removed") or 0), method=str(scope.get("index_method")),
                           capped=bool(scope.get("index_capped")), note=scope.get("index_note"))


async def _materialize(session: AsyncSession, source_id: str, *, synced: bool,
                       job: IntrospectJob | None = None) -> None:
    """把范围内已反射的行物化进 schema_cache（停止、中断、出错收尾时也做：读完的不白读）。synced：探查做完了，盖同步时间。"""
    row = await session.get(DataSource, source_id)
    if row is None:
        return
    scope = (job.scope or {}) if job is not None else {}
    async with so.source_lock(source_id):
        await so.rebuild_schema_cache(session, row, synced=synced, index=_index_stats(job),
                                      candidates=list(scope.get("available_schemas") or []))
        if synced:
            row.schema_synced_at = utcnow()
        await session.commit()


async def _close(session: AsyncSession, job_id: str, job_status: str, job_error: str | None,
                 task_error: str | None) -> None:
    """收尾：还没读的表记 cancelled（原因 task_error），任务（还没结束的话）记 job_status；读完的物化进 schema_cache。"""
    now = utcnow()
    job = await session.get(IntrospectJob, job_id)
    await session.execute(update(IntrospectTask).where(IntrospectTask.job_id == job_id,
                                                       IntrospectTask.status.in_(OPEN))
                          .values(status="cancelled", error=task_error, finished_at=now)
                          .execution_options(synchronize_session=False))
    await session.execute(update(IntrospectJob).where(IntrospectJob.id == job_id, IntrospectJob.status.in_(ACTIVE))
                          .values(status=job_status, error=job_error, finished_at=now)
                          .execution_options(synchronize_session=False))
    await session.commit()
    if job is not None and job.started_at is not None:
        # 开始过的才物化：没轮到就结束了的，范围还没写进对象索引，schema_cache 照旧
        await _materialize(session, job.source_id, synced=False, job=job)


async def cancel(session: AsyncSession, job: IntrospectJob) -> None:
    """停止一个探查任务。排队的直接 cancelled；进行中的交给 manager 停（停在当前这一批之后）。已经结束的抛
    IntrospectFinished。"""
    job_id, status = job.id, job.status
    if status in FINISHED:
        raise IntrospectFinished("这次探查已经结束了，不需要停止")
    if status == "queued" and await runtime.cancel_queued(session, IntrospectJob, IntrospectTask, job_id):
        # 还没开始：范围没写进对象索引、schema_cache 没动，停了就是什么都没发生
        await manager.stop(job_id, "user", grace=STOP_GRACE_S)
        return
    if await manager.stop(job_id, "user", grace=STOP_GRACE_S):
        return
    # 库里说在进行，这个进程里却没有在跑它（记录对不上）：直接记停止
    await _close(session, job_id, "cancelled", None, STOPPED_ERROR)


async def startup() -> int:
    """服务启动时：排队、进行中的探查任务标成 interrupted，没读的表 cancelled，读完的物化进去；不自动续跑。"""
    async def interrupt(session: AsyncSession, job_id: str) -> None:
        await _close(session, job_id, "interrupted", INTERRUPTED_ERROR, INTERRUPTED_ERROR)

    n = await runtime.interrupt_on_startup(IntrospectJob, interrupt, running=manager.running)
    if n:
        logger.warning("服务重启：%d 个探查任务没有做完，已标为中断，可在数据源卡片上重新探查", n)
    return n


# ==========================================================================
# 在后台反射
# ==========================================================================


def _side(diff: Any, which: str) -> Any:
    """一条差异的前（before）或后（after），疑似改名的另一半记在这一侧（{…, rename}）。"""
    value = getattr(diff, which)
    if diff.rename:
        base = dict(value) if isinstance(value, dict) else ({"value": value} if value is not None else {})
        return {**base, "rename": dict(diff.rename)}
    return value


def json_key(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class _IntrospectRun(runtime.JobRun):
    """一个探查任务在后台的一次执行（进队、停止、收尾的框架在 runtime.JobRun）。"""

    kind = "introspect"
    model = IntrospectJob

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        #: 已发布模板和解析好的图（catalog_impact.published_specs）：这次探查里各批的影响面共用，第一次用到时读
        self._specs: list[Any] | None = None

    async def _stopped(self) -> None:
        if self.stop_reason == "user":
            await self._end("cancelled", None, STOPPED_ERROR)
        else:
            await self._end("interrupted", INTERRUPTED_ERROR, INTERRUPTED_ERROR)

    async def _failed(self, reason: str) -> None:
        await self._end("failed", reason, reason)

    async def _crashed(self, e: Exception) -> None:
        logger.exception("探查任务 %s 出错", self.job_id)
        reason = JOB_ERROR.format(line=first_line(e) or describe_exception(e))
        await self._end("failed", reason, reason)

    async def _end(self, job_status: str, job_error: str | None, task_error: str | None) -> None:
        try:
            await self._settle()
            await self._write(_close, self.job_id, job_status, job_error, task_error)
        except Exception:  # noqa: BLE001
            logger.exception("探查任务 %s 收尾时写库出错", self.job_id)

    async def _run(self) -> None:
        if not await self._write(self._claim):
            return                          # 开始之前就被停止了
        async with SessionLocal() as session:
            job = await session.get(IntrospectJob, self.job_id)
            if job is None:
                raise runtime.JobFailed(SOURCE_GONE)
            source_id, schema = job.source_id, job.schema_name
        await self._write(self._apply, source_id, schema)
        async with SessionLocal() as session:
            pending = [(t.seq, t.table_name) for t in (await session.execute(
                select(IntrospectTask.seq, IntrospectTask.table_name).where(
                    IntrospectTask.job_id == self.job_id, IntrospectTask.status == "pending")
                .order_by(IntrospectTask.seq))).all()]
        for start in range(0, len(pending), so.REFLECT_BATCH):
            batch = pending[start:start + so.REFLECT_BATCH]
            row = await self._write(self._recheck, source_id)
            await self._write(self._mark_running, [seq for seq, _ in batch])
            objs = await self._write(self._objects, source_id, schema, [name for _, name in batch])
            try:
                got = await so.reflect(row, [(name, objs.get(name, ("table", False))[1]) for _, name in batch],
                                       views={n for n, (kind, _q) in objs.items() if kind == "view"})
            except Exception as e:  # noqa: BLE001 - 连接本身出错：这一批和后面的都读不了
                raise runtime.JobFailed(CONNECT_ERROR.format(line=first_line(e) or describe_exception(e))) from e
            await self._write(self._save_batch, source_id, schema, batch, got)
        await self._settle()
        await self._write(self._finish, source_id)

    # ---------------------------------------------------------------- 库操作

    async def _claim(self, session: AsyncSession) -> bool:
        return await runtime.claim(session, IntrospectJob, self.job_id)

    async def _apply(self, session: AsyncSession, source_id: str, schema: str) -> None:
        """开始执行时把建任务时圈好的范围写进对象索引：离开范围的清掉结构，范围内的按原因记。

        建任务之后才加进范围的表（added_at 不早于建任务的时刻：look、db_schema__ 按需加入的，目录页「加入范围」显式加的）
        照旧留在范围里：它们是加的那一刻当场读过结构的，不在这次的表单里；任务排在剖析后面时，用户刚看到「已加入」，任务
        一开始就把它移出去、清掉结构，要等再探查一次才回来。原因按圈范围的先后取高的那个（pinned > 显式 > 规则 > 按需）。
        建任务之前就按需加入的不在此列：圈范围时已经算过（plan_scope），被显式移出的照规则移出。"""
        rank = {r: i for i, r in enumerate(so.SCOPE_REASONS)}
        reasons = {r.table_name: r.reason or "rule" for r in (await session.execute(
            select(IntrospectTask.table_name, IntrospectTask.reason).where(IntrospectTask.job_id == self.job_id)
        )).all()}
        job = await session.get(IntrospectJob, self.job_id)
        async with so.source_lock(source_id):
            if job is not None:
                for name, reason in (await session.execute(select(SourceObject.name, SourceObject.scope_reason).where(
                        SourceObject.source_id == source_id, SourceObject.schema_name == schema,
                        SourceObject.in_scope.is_(True), SourceObject.removed_at.is_(None),
                        SourceObject.added_at >= job.created_at))).all():
                    late = reason or "on_demand"
                    held = reasons.get(name)
                    if held is None or rank.get(late, len(rank)) < rank.get(held, len(rank)):
                        reasons[name] = late
            counts = {r: 0 for r in so.SCOPE_REASONS}
            for reason in reasons.values():
                counts[reason] = counts.get(reason, 0) + 1
            before = set((await session.execute(select(SourceObject.name).where(
                SourceObject.source_id == source_id, SourceObject.schema_name == schema,
                SourceObject.in_scope.is_(True)))).scalars())
            plan = so.ScopePlan(in_scope=reasons, counts=counts, cap=0, index_total=0, out_of_scope=0)
            await so.apply_scope(session, source_id, schema, plan)
            row = await session.get(DataSource, source_id)
            if job is not None and row is not None:
                await _record_scope_changes(session, row, job, sorted(before - set(reasons)),
                                            len(set(reasons) - before))
            await session.commit()

    async def _recheck(self, session: AsyncSession, source_id: str) -> Any:
        """每批开始前重读数据源：没了抛 JobFailed，停用了也停下（JobFailed，原因写明）。返回会话外可用的数据源对象。"""
        row = await session.get(DataSource, source_id)
        if row is None:
            raise runtime.JobFailed(SOURCE_GONE)
        if row.enabled is False:
            raise runtime.JobFailed(SOURCE_DISABLED.format(name=row.name))
        session.expunge(row)
        return row

    async def _mark_running(self, session: AsyncSession, seqs: list[int]) -> None:
        await session.execute(update(IntrospectTask).where(IntrospectTask.job_id == self.job_id,
                                                           IntrospectTask.seq.in_(seqs),
                                                           IntrospectTask.status == "pending")
                              .values(status="running").execution_options(synchronize_session=False))
        await session.commit()

    async def _objects(self, session: AsyncSession, source_id: str, schema: str,
                       names: list[str]) -> dict[str, tuple[str, bool]]:
        return {r.name: (r.kind, bool(r.quote_name)) for r in (await session.execute(
            select(SourceObject.name, SourceObject.kind, SourceObject.quote_name).where(
                SourceObject.source_id == source_id, SourceObject.schema_name == schema,
                SourceObject.name.in_(names)))).all()}

    async def _save_batch(self, session: AsyncSession, source_id: str, schema: str, batch: list[tuple[int, str]],
                          got: so.Reflected) -> None:
        """一批读完：读到的结构写进对象索引，逐表记 ok / failed 和结构版本；拿上一次的结构和这一次的比，变化记进
        source_changes（同一个事务）。"""
        old = {r.name: r for r in (await session.execute(
            select(SourceObject.name, SourceObject.meta, SourceObject.struct_version, SourceObject.fingerprint,
                   SourceObject.reflect_error).where(
                SourceObject.source_id == source_id, SourceObject.schema_name == schema,
                SourceObject.name.in_([name for _, name in batch])))).all()}
        versions = await so.save_reflected(session, source_id, schema, got)
        await self._record_diffs(session, source_id, batch, got, old, versions)
        now = utcnow()
        for seq, name in batch:
            if name in got.tables:
                values: dict[str, Any] = {"status": "ok", "error": None}
            else:
                values = {"status": "failed", "error": got.skipped.get(name) or "读不到列"}
            await session.execute(update(IntrospectTask).where(IntrospectTask.job_id == self.job_id,
                                                               IntrospectTask.seq == seq)
                                  .values(**values, struct_version=versions.get(name, 0), finished_at=now)
                                  .execution_options(synchronize_session=False))
        await session.commit()

    async def _record_diffs(self, session: AsyncSession, source_id: str, batch: list[tuple[int, str]],
                            got: so.Reflected, old: dict[str, Any], versions: dict[str, int]) -> None:
        """这一批的结构差异（不提交）：读到的表指纹变了、又不是基线（schema_diff.is_baseline）的，逐条比出变化、算影响面；
        以前读得到、这次读不到的记 table_reflect_failed。出错只记日志，不挡探查。"""
        from app.data import catalog_impact
        from app.data import schema_diff as sd
        from app.data import source_changes as sc

        try:
            todo: list[tuple[int, str, list[Any], dict[str, Any] | None]] = []
            failed: list[tuple[int, str]] = []
            for seq, name in batch:
                prev = old.get(name)
                if prev is None:
                    continue
                if name in got.tables:
                    meta = got.tables[name]
                    if sd.is_baseline(prev.struct_version, prev.meta) or so.fingerprint(meta) == prev.fingerprint:
                        continue
                    diffs = sd.diff_table(prev.meta, meta)
                    if diffs:
                        todo.append((seq, name, diffs, prev.meta))
                elif name in got.skipped and isinstance(prev.meta, dict) and prev.meta and not prev.reflect_error:
                    failed.append((seq, name))
            if not todo and not failed:
                return
            job = await session.get(IntrospectJob, self.job_id)
            row = await session.get(DataSource, source_id)
            if job is None or row is None:
                return
            fire_at = (job.scope or {}).get("fire_at")
            if self._specs is None:
                self._specs = await catalog_impact.published_specs(session)
            tables_after = {**dict((row.schema_cache or {}).get("tables") or {}), **got.tables}
            for seq, name, diffs, before in todo:
                impacts = await catalog_impact.structure_impact(session, row, name, diffs, before_meta=before,
                                                                tables_after=tables_after, specs=self._specs)
                after_v, before_v = versions.get(name), old[name].struct_version
                changes = [sc.NewChange(kind=d.kind, table=name, column=d.column, severity=severity,
                                        before=_side(d, "before"), after=_side(d, "after"), affects=affects,
                                        struct_version_before=before_v, struct_version_after=after_v,
                                        key=(after_v, d.key, json_key(d.before), json_key(d.after)))
                           for d, (affects, severity) in zip(diffs, impacts)]
                await sc.record_all(session, row, changes,
                                    _detected(self.job_id, job.trigger, job.created_by, fire_at, seq))
            for seq, name in failed:
                await sc.record(session, row, sc.NewChange(
                    kind=sd.TABLE_REFLECT_FAILED, table=name, severity="warning",
                    after={"error": got.skipped.get(name)}, struct_version_before=old[name].struct_version,
                    key=(old[name].struct_version, got.skipped.get(name))),
                    _detected(self.job_id, job.trigger, job.created_by, fire_at, seq))
        except Exception:  # noqa: BLE001
            logger.exception("探查任务 %s 记录结构变化时出错", self.job_id)

    async def _finish(self, session: AsyncSession, source_id: str) -> None:
        """读完：物化 schema_cache、盖同步时间；有表读不到是 partial，否则 succeeded。"""
        failed = int((await session.execute(select(func.count()).select_from(IntrospectTask).where(
            IntrospectTask.job_id == self.job_id, IntrospectTask.status == "failed"))).scalar() or 0)
        await _materialize(session, source_id, synced=True, job=await session.get(IntrospectJob, self.job_id))
        await session.execute(update(IntrospectJob).where(IntrospectJob.id == self.job_id,
                                                          IntrospectJob.status == "running")
                              .values(status="partial" if failed else "succeeded", finished_at=utcnow())
                              .execution_options(synchronize_session=False))
        await session.commit()


class IntrospectJobs(runtime.JobManager):
    """进程内的探查任务（start / stop / wait / running / shutdown 见 runtime.JobManager）。"""

    def __init__(self) -> None:
        super().__init__(_IntrospectRun, label="探查任务", name="introspect")


manager = IntrospectJobs()


__all__ = ["INTERRUPTED_ERROR", "IntrospectBusy", "IntrospectFailed", "IntrospectFinished", "IntrospectJobs",
           "IntrospectRefused", "KEEP_JOBS", "STOPPED_ERROR", "ScopeTooLarge", "active_job", "busy_message", "cancel",
           "ensure_free", "job_brief", "job_out", "latest_briefs", "latest_job", "load", "manager", "prepare",
           "preview", "prune", "startup", "waiting_for"]
