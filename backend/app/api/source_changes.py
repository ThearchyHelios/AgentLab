"""数据源的变化记录（docs/design/next-round/03-数据接入与目录维护.md B3）：结构变化、取值变化，查看与处理；以及同一前缀下
数据源定时维护要做哪几步（B4）。

挂在 /api/datasources/{id}/… 下，要在 datasources 的通配 /{source_id} 之前登记（main.py）：

- GET  /{id}/changes                 变化列表（缺省开着的和知道了的；status=all 全部）：按「使用次数权重 × 严重程度」排，
                                     同分新的在前；带各级别的计数（数据源卡片的角标「N 个变化要处理」按它，第 4 波）
- GET  /{id}/changes/{change_id}     一条变化
- POST /{id}/changes/{change_id}/resolve  处理：{action, note?, codes?}。动作落到目录已有的写入接口并记目录版本
                                     （data/source_changes.handle）；误报必须写理由（422）；已经处理过的 409
- GET  /{id}/maintenance             定时维护要做哪几步、剖析开没开、指向这个源的维护定时（定时任务页同一个形状，最近一次触发
                                     带 inputs：建了哪些任务、哪几步没执行和原因）、调度开没开、这个源要不要维护
- PUT  /{id}/maintenance             改要做哪几步：{steps: [introspect | profile_needed | observe]}。定时本身（cron、时区、开关）
                                     在 /api/schedules 里建（target_kind=source_maintenance），这里只管步骤

第 4 波（W4-d）给界面加的：

- GET  /upkeep                       数据源列表一次取齐：各源要处理的变化条数（卡片角标）、指向它的维护定时（频率、开没开、
                                     下一次）
- GET  /{id}/observations?table=     表详情「结构与取值变化」一节：这张表最近几次观察（行数、码值分布、最新业务日期、按日
                                     行数），各带口径；遮罩在读的那一刻再核
- GET  /{id}/background-jobs         这个源进行中和排队的后台任务（探查、起草、剖析），按同源队的先后排好（目录页的后台任务条，
                                     界面不自己排）

变化列表、单条、处理的回答里，每条变化在 agentlab-change/1 之外另带几项只给界面看的（_views；召回拉的 list_changes 不带）：
category（struct 结构 / value 取值）、basis_text（依据的口径）、actions（现在能做哪几个处理动作，做不了的写原因）、job（处理时
建的任务，能点进进度）、影响面里每个目录项现在的状态（affects.catalog[*].now：清理过、改过的照实）。列表另能按 category 筛、
按 status=handled 看处理过的。

访问密钥默认不放（auth/keys.py 的 KEY_ROUTES 没有登记这几条）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import actor_of
from app.data import catalog, schema_diff as sd, source_changes as sc, source_maintenance
from app.db.base import get_session
from app.db.models import DataSource

router = APIRouter(prefix="/api/datasources", tags=["source-changes"])

#: 一页最多几条
LIMIT_MAX = 200
#: 列表的状态筛选：pending 开着的和知道了的（缺省）、handled 处理过的（解决了的、标为误报的）、all、或者某一个状态
STATUS_FILTERS = ("pending", "handled", "all", *sc.STATUSES)
#: 变化的两类：结构变化、取值变化（叫法分开，和已有的「目录有变化」也分开，总方案 X10）
CATEGORIES = {"struct": tuple(sd.STRUCT_KINDS), "value": tuple(sc.VALUE_KINDS)}
#: 处理动作建的任务是哪一种
_JOB_OF = {"redraft": "draft", "reprofile": "profile"}
#: 关联覆盖率下降的依据（剖析核对关系时抽样）
RELATION_BASIS = "关联关系抽样核对"


async def _source(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if row is None:
        raise HTTPException(404, "数据源不存在，可能已被删除")
    return row


# ---- 给界面的几样（要登记在 /{source_id}/… 前面：/upkeep 只有一段）


@router.get("/upkeep")
async def upkeep(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """数据源列表一次取齐：{items: [{source_id, changes: {pending, open, by_severity}, maintenance: [{id, enabled, cron,
    timezone, description_text, next_run_at}]}]}。卡片角标「N 个变化要处理」按 changes.by_severity.action；没有变化、没有
    维护定时的源也列出来（计数为 0、maintenance 为空）。"""
    from app.db.models import Schedule
    from app.scheduler import service

    ids = list((await session.execute(select(DataSource.id))).scalars())
    counts = await sc.counts_by_source(session, ids)
    schedules: dict[str, list[dict[str, Any]]] = {}
    for r in (await session.execute(select(Schedule).where(Schedule.target_kind == "source_maintenance")
                                    .order_by(Schedule.created_at))).scalars():
        schedules.setdefault(r.target_id or "", []).append({
            "id": r.id, "enabled": r.enabled, "cron": r.cron, "timezone": r.timezone,
            "description_text": _describe(r.cron, r.timezone),
            "next_run_at": service.as_utc(r.next_run_at).isoformat() if r.next_run_at else None})
    empty = {"pending": 0, "open": 0, "by_severity": dict.fromkeys(sc.SEVERITIES, 0)}
    return {"items": [{"source_id": i, "changes": counts.get(i, empty), "maintenance": schedules.get(i, [])}
                      for i in ids]}


def _describe(cron_expr: str, tz: str) -> str:
    from app.scheduler import cron

    try:
        return cron.describe(cron_expr, tz)
    except cron.CronError:
        return f"按 cron 表达式 {cron_expr}（{tz}）"


def _basis_text(row: Any) -> str | None:
    """依据的口径（取值变化）：整表、业务日期最近 N 天的窗口、块采样、按存储顺序的前 N 行、统计估算……照观察时记下的写。
    结构变化的依据是探查读到的结构版本（前后各一版），界面照 struct_version_before / after 写，这里为 None。"""
    from app.data import catalog_profile_observe as observe

    if row.kind in sd.STRUCT_KINDS:
        return None
    basis = row.basis if isinstance(row.basis, dict) else {}
    if basis.get("method") == "relation_sample":
        return RELATION_BASIS
    return observe.key_text(basis.get("key")) or (observe.basis_text(basis) if basis.get("method") else None)


async def _views(session: AsyncSession, source: DataSource, rows: list[Any]) -> list[dict[str, Any]]:
    """接口给界面的变化：change_out 之外另带 category、basis_text、actions、job、影响面里每个目录项现在的状态（见模块说明）。"""
    from app.data import catalog_profile, table_versions

    if not rows:
        return []
    try:
        view = await table_versions.resolve_source(session, source)
    except table_versions.SnapshotMissing:
        view = source
    tables = set(((getattr(view, "schema_cache", None) or {}).get("tables") or {}))
    settings = catalog_profile.profile_settings(source.options)
    profile_reason = None
    if not settings.enabled:
        try:
            catalog_profile.ensure_enabled(source)
        except catalog_profile.ProfileDisabled as e:
            profile_reason = str(e)
    entries = await catalog.read_catalog(session, source.id) if any(
        (r.affects or {}).get("catalog") for r in rows) else {}
    slots = {t: catalog._slots(e.notes) for t, e in entries.items()}

    def now_of(ref: dict[str, Any]) -> dict[str, Any] | None:
        try:
            item = slots.get(str(ref.get("table") or ""), {}).get(catalog.parse_path(str(ref.get("path") or "")))
        except catalog.CatalogPathError:
            return None
        return {"status": item.get("status"), "source": item.get("source")} if item else None

    out: list[dict[str, Any]] = []
    for r in rows:
        v = sc.change_out(r)
        v["category"] = "struct" if r.kind in sd.STRUCT_KINDS else "value"
        v["basis_text"] = _basis_text(r)
        v["actions"] = sc.available_actions(r, in_schema=bool(r.table_name) and r.table_name in tables,
                                            profile_enabled=settings.enabled, profile_reason=profile_reason)
        resolution = r.resolution or {}
        kind = _JOB_OF.get(str(resolution.get("action") or ""))
        v["job"] = {"kind": kind, "id": resolution["job_id"]} if kind and resolution.get("job_id") else None
        affects = dict(v["affects"])
        affects["catalog"] = [{**ref, "now": now_of(ref)} for ref in affects.get("catalog") or []
                              if isinstance(ref, dict)]
        v["affects"] = affects
        out.append(v)
    return out


@router.get("/{source_id}/changes")
async def list_changes(source_id: str, status: str = Query("pending"), kind: list[str] | None = Query(None),
                       category: str | None = Query(None), table: str | None = Query(None),
                       limit: int = Query(100, ge=1, le=LIMIT_MAX), offset: int = Query(0, ge=0),
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{items: [变化（agentlab-change/1，另带给界面的几项）], total, counts: {pending, open, by_severity}}。

    status=pending（缺省）按「使用次数权重 × 严重程度」排，同分新的在前；status=handled 是处理过的，最近测到的在前。
    category=struct / value 只看结构变化 / 取值变化（和 kind 一起给时取两者都对得上的；一种都对不上就是空的）。"""
    if status not in STATUS_FILTERS:
        raise HTTPException(422, f"不认识的状态：{status}。可选：{'、'.join(STATUS_FILTERS)}")
    if kind and (bad := [k for k in kind if k not in sc.KINDS]):
        raise HTTPException(422, f"不认识的变化种类：{'、'.join(bad)}")
    if category is not None and category not in CATEGORIES:
        raise HTTPException(422, f"不认识的变化类别：{category}。可选：struct（结构变化）、value（取值变化）")
    row = await _source(session, source_id)
    kinds = list(kind) if kind else None
    if category is not None:
        kinds = [k for k in (kinds or CATEGORIES[category]) if k in CATEGORIES[category]]
    counts = await sc.counts(session, row.id)
    if kinds == []:
        return {"items": [], "total": 0, "counts": counts}
    if status == "handled":
        rows, total = await sc.list_handled(session, row.id, kinds=kinds, table=table, limit=limit, offset=offset)
    else:
        rows, total = await sc.list_for_source(session, row, status=status, kinds=kinds, table=table, limit=limit,
                                               offset=offset)
    return {"items": await _views(session, row, rows), "total": total, "counts": counts}


@router.get("/{source_id}/changes/{change_id}")
async def get_change(source_id: str, change_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _source(session, source_id)
    found = await sc.load(session, row.id, change_id)
    if found is None:
        raise HTTPException(404, "这条变化不存在，可能已随数据源删除")
    return {"change": (await _views(session, row, [found]))[0]}


class ResolveIn(BaseModel):
    action: str
    #: 理由：误报必须写
    note: str | None = Field(default=None, max_length=sc.NOTE_MAX)
    #: 加入码值时的 {码值: 含义}（含义可以空）；不给就加这条变化测到的新码值
    codes: dict[str, str] | None = None


@router.post("/{source_id}/changes/{change_id}/resolve")
async def resolve_change(source_id: str, change_id: str, payload: ResolveIn, x_actor: str | None = Header(default=None),
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """处理一条变化：clean_catalog 清理目录里说到这些列的项、add_codes 加入码值、redraft 重新起草这张表、reprofile 重新
    剖析、acknowledge 知道了、dismiss 误报（必须写理由）。返回 {change, catalog_version?, job?}。目录被别人刚改过 409。"""
    row = await _source(session, source_id)
    found = await sc.load(session, row.id, change_id)
    if found is None:
        raise HTTPException(404, "这条变化不存在，可能已随数据源删除")
    try:
        out = await sc.handle(session, row, found, payload.action, actor=actor_of(x_actor), note=payload.note,
                              codes=payload.codes)
    except sc.ChangeInvalid as e:
        await session.rollback()
        raise HTTPException(422, str(e)) from e
    except sc.ChangeFinished as e:
        await session.rollback()
        raise HTTPException(409, str(e)) from e
    except catalog.CatalogConflict as e:
        await session.rollback()
        raise HTTPException(409, "这张表的数据目录刚被别人改过，请刷新后再处理") from e
    # 处理完照实给这条变化现在的样子（另带给界面的几项）：目录项清理过的、动作做完的都跟着变
    await session.refresh(row)
    fresh = await sc.load(session, row.id, change_id)
    if fresh is not None:
        out["change"] = (await _views(session, row, [fresh]))[0]
    return out


@router.get("/{source_id}/maintenance")
async def get_maintenance(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{steps, profile_enabled, maintained, scheduler_on, schedules}：要做哪几步、剖析开没开（没开时剖析、观察两步到点照实记
    没执行）、这个源要不要维护（上传表格、记录表不要）、调度开没开（关着时到点不会触发）、指向这个源的维护定时。

    定时是定时任务页同一个形状（api/schedules 的 Schedule：改频率、开关照样带 version 走 PATCH /api/schedules/{id}），最近
    一次触发另带 inputs（{steps, jobs: [{step, kind, id, tables}], skipped: [{step, reason}]}：建了哪些任务、哪几步没执行和原因）。"""
    from app import scheduler
    from app.api.schedules import _schedule_outs
    from app.data import catalog_profile
    from app.db.models import Schedule, ScheduleFire

    row = await _source(session, source_id)
    rows = list((await session.execute(select(Schedule).where(
        Schedule.target_kind == "source_maintenance", Schedule.target_id == row.id)
        .order_by(Schedule.created_at))).scalars())
    outs = await _schedule_outs(session, rows)
    fire_ids = [o["last_fire"]["id"] for o in outs if o.get("last_fire")]
    inputs = dict((await session.execute(select(ScheduleFire.id, ScheduleFire.inputs).where(
        ScheduleFire.id.in_(fire_ids)))).all()) if fire_ids else {}
    for o in outs:
        if o.get("last_fire"):
            o["last_fire"]["inputs"] = dict(inputs.get(o["last_fire"]["id"]) or {})
    return {"steps": source_maintenance.steps_of(row.options),
            "profile_enabled": catalog_profile.profile_settings(row.options).enabled,
            "maintained": (row.origin or "manual") not in ("upload", "records"),
            "scheduler_on": scheduler.scheduler_enabled(),
            "schedules": outs}


class MaintenanceIn(BaseModel):
    steps: list[Any]


@router.put("/{source_id}/maintenance")
async def put_maintenance(source_id: str, payload: MaintenanceIn, x_actor: str | None = Header(default=None),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _source(session, source_id)
    if (row.origin or "manual") in ("upload", "records"):
        raise HTTPException(409, source_maintenance.NOT_MAINTAINED)
    if problem := source_maintenance.steps_problem(payload.steps):
        raise HTTPException(422, problem)
    row.options = source_maintenance.with_steps(row.options, [str(s) for s in payload.steps], actor=actor_of(x_actor))
    await session.commit()
    return await get_maintenance(source_id, session)


@router.get("/{source_id}/observations")
async def table_observations(source_id: str, table: str = Query(..., min_length=1, max_length=500),
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """表详情「结构与取值变化」一节：{table, series: [{kind, column, points: [{at, basis_key, basis_text, data}],
    basis_changed}]}（catalog_profile_observe.recent）。现在被遮罩的列不给；没观察过是空的。"""
    from app.data import catalog_profile, catalog_profile_observe as observe

    row = await _source(session, source_id)
    masked = catalog_profile.masked_of(row.options)
    return {"table": table, "series": await observe.recent(session, row.id, table, masked=masked)}


@router.get("/{source_id}/background-jobs")
async def background_jobs(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这个源进行中和排队的后台任务（目录页的后台任务条，05 (g)）：{items: [{kind: introspect | draft | profile, id, status,
    total, done, waiting_for, position}]}。

    按同源队（core/job_runtime.lanes）的先后排：position 是在队里的位置（0 是正在做的）；刚建、后台还没排进队的（position 为
    null）排在队里的后面、按建的先后。waiting_for 是各任务自己的说法（排在谁后面），界面照写，不自己排。"""
    from app.core import job_runtime as runtime
    from app.data import catalog_jobs, catalog_profile_jobs, introspect_jobs

    row = await _source(session, source_id)
    found: list[tuple[int | None, Any, dict[str, Any]]] = []
    for kind, module in (("introspect", introspect_jobs), ("draft", catalog_jobs), ("profile", catalog_profile_jobs)):
        job = await module.active_job(session, row.id)
        brief = await module.job_brief(session, job) if job is not None else None
        if job is None or brief is None:
            continue
        ahead = await runtime.lanes.ahead(row.id, job.id)
        position = len(ahead) if ahead is not None else None
        found.append((position, job.created_at, {
            "kind": kind, "id": job.id, "status": brief["status"], "total": brief.get("total", 0),
            "done": brief.get("done", 0), "waiting_for": brief.get("waiting_for"), "position": position}))
    found.sort(key=lambda f: (f[0] is None, f[0] if f[0] is not None else 0, f[1]))
    return {"items": [f[2] for f in found]}
