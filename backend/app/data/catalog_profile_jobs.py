"""数据目录的后台剖析任务（/api/datasources/{id}/catalog/profile-jobs）。

以前剖析是一次同步长请求（POST /catalog/profile，已去掉）：预算按整次剖析算，选 50 张表只做得完前两三张；进行中只有一条
按时长走的横条、不能停；关掉页面报告就没了，对业务库查了什么无从查。现在和起草一样是后台任务（生命周期在
data/catalog_job_runtime.py），逐表剖析（引擎在 data/catalog_profile.py）：

- **建任务**（create_job）：范围三种——指定的表（selected：按给的顺序去重；表结构里没有、视图、没有可剖析的项照样建进
  任务、状态 skipped）、需要剖析的表（needed：从没剖析过、上次剖析没做完、剖析之后冒出了新的待核实关系，见 due_reason）、
  全部表（all）。后两种只挑表结构里、不是视图、有可剖析的项的表，有待核实关系的在前，再按使用次数、表结构顺序；挑出 0 张
  抛 ProfileNothing。剖析设置随任务记下快照，每张表的检查项按当时的目录先列一遍。同一个源同时只有一个排队或进行中的
  剖析任务（ProfileBusy，说法里写明进行到第几张；部分唯一索引兜底）；和同源的起草任务按建立的先后排队（每个源一条队），
  起草在前时 waiting_for 是 "draft"。建任务时顺手清掉超出保留数的旧任务（prune）。
- **在后台剖析**（manager.start）：轮到了才开始。每张表开始前重读数据源——停用了、关掉了剖析就停下，剩下的表 cancelled、
  error 写明；遮罩按最新的设置——再逐表调 catalog_profile.profile_table（它读的是那时目录里的最新内容：起草刚提的关系也
  核对）。检查项、正在做哪一项、查询数、发过的查询随时落库；整张表的结果和写目录在同一个事务里，停止时也不会被打断。
- **任务级止损**（替代以前的整次总量上限）：连续 3 条查询失败、最近 10 条里 7 条超时，整个任务停下（stop_reason、error），
  正在做的那张表写入已做完的项，后面的表 cancelled、error 带同一句原因；最近 6 条里 4 条超时时 notice 有一句提醒，好转后
  清掉。任务最后的状态按逐表算：有 failed、partial、cancelled 的表是 partial，否则 succeeded；任务整体出错（数据源没了、
  快照被改动、意外异常）是 failed。
- **停止**（cancel）：排队的直接 cancelled；进行中的停掉正在等的那条查询（库那边按单条时限截止），那张表做完的项照常写进
  目录、没做的保留旧结论，后面的表 cancelled，任务 cancelled。
- **重试**（retry_tables + create_job）：新任务，只含原任务里 failed、partial、cancelled 的表（按原来的顺序），范围 selected，
  retry_of 记原任务；设置取数据源当前的。
- **服务重启**（startup）：排队、进行中的标 interrupted，没结束的表 cancelled、error「服务重启，剖析中断」（进程没了时正在
  剖析的那张表，查完的项也没写进目录，照实写明，见文末）。不自动续跑：
  复制出来的数据目录启动时不能自己去查业务库，界面让人点「重试」。关停时（manager.shutdown）停掉进行中的，同样标中断。
  启动时还一次性迁移剖析设置的旧键（catalog_profile.migrate_settings）。
- **保留**（prune）：每个源保留最近 KEEP_JOBS 个结束了的剖析任务，更早的删掉；某张表「上次剖析」所在的任务不删，表详情的
  「上次剖析」一直在。
- **读**：job_dict（任务和逐表一行）、task_detail（一张表的检查项、发现、跳过的项、发过的查询）、last_profiles / last_detail
  （表清单每行、表详情的「上次剖析」）、due_reason（这张表要不要剖析、为什么）。「上次剖析」是真正开始过的最近一次（见
  _ranked）：没开始就停了的、还排着的那一项不挡掉上一次真正剖析的行数、发过的查询。

没结束的表收尾时（停止、中断、整体出错）检查项里还排着的、正在做的记为跳过（原因 cancelled 或 error），不留「排队中」的
检查项在一张已经结束的表上。目录只在一张表结束时和它的结果一起写（同一个事务）：收尾时还没结束的表，查完的项结论也没
写进目录（进程被杀、快照被改动），同样记为跳过、写明「查完了但结论没有写进数据目录」，算没做完，重试时带上这张表。
停止、关停这类能收尾的停下不走这条路：正在剖析的那张表做完的项照常写进目录（catalog_profile.profile_table）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_profile
from app.data import catalog_job_runtime as runtime
from app.data.catalog_job_runtime import ACTIVE, FINISHED, OPEN, RETRYABLE, STOP_GRACE_S, TASK_DONE, TASK_STATUSES
from app.data.catalog_profile import (
    NOTHING_ERROR, UNFINISHED_REASONS, VIEW_ERROR, ProfileSettings, ProfileSkip, TableHooks, TableResult,
)
from app.data.engine import SnapshotTampered
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import CatalogDraftJob, CatalogProfileJob, CatalogProfileTask, DataSource

logger = logging.getLogger(__name__)

#: 一个任务最多几张表
MAX_TABLES = 500
#: 每个源保留几个结束了的剖析任务（某张表最近一次剖析所在的任务另外保留）
KEEP_JOBS = 50

INTERRUPTED_ERROR = "服务重启，剖析中断"
STOPPED_ERROR = catalog_profile.STOPPED
UNFINISHED_ERROR = "剖析结束时这张表还没有收尾，请重试这张表"
JOB_ERROR = "剖析任务出错：{line}"
SOURCE_GONE = "数据源不存在，可能已被删除"
SOURCE_DISABLED = "数据源「{name}」已停用，剖析已停止"
PROFILE_TURNED_OFF = "数据源「{name}」已关闭数据剖析，剖析已停止"
#: 没结束的检查项在收尾时的说明（{why} 是停止、中断、出错的原因）
_UNCHECKED = "{why}，未检查"
#: 查完了、结论却没写进目录的检查项：这张表没走到整张表结束、写目录那一步（进程没了、任务整体出错）
_UNSAVED = "{why}，查完了但结论没有写进数据目录"
_CHECK_OPEN = ("pending", "running")


def unsaved_error(why: str, done: int, rest: int) -> str:
    """没走到写目录那一步的表的整句原因：查完的几项没写进目录，剩下几项没查。"""
    text = f"{why}：已查完的 {done} 项没有写进数据目录"
    return text + f"，剩下 {rest} 项没查" if rest else text


class ProfileBusy(Exception):
    """这个数据源已经有排队或进行中的剖析任务。message 给人看（写明进行到第几张）。"""


class ProfileFinished(Exception):
    """要停止的剖析任务已经结束了。"""


class ProfileNothing(Exception):
    """按范围挑出 0 张表。message 说明原因、建议怎么办。"""


class NothingToRetry(Exception):
    """原任务里没有需要重试的表。"""


# ==========================================================================
# 读任务
# ==========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _empty_summary() -> dict[str, int]:
    return catalog_profile.summarize([])


async def active_job(session: AsyncSession, source_id: str) -> CatalogProfileJob | None:
    """排队或进行中的那个（同一个源最多一个）。"""
    return (await session.execute(
        select(CatalogProfileJob).where(CatalogProfileJob.source_id == source_id,
                                        CatalogProfileJob.status.in_(ACTIVE))
        .order_by(CatalogProfileJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def latest_job(session: AsyncSession, source_id: str) -> CatalogProfileJob | None:
    """最近建的一个（任何状态）。"""
    return (await session.execute(
        select(CatalogProfileJob).where(CatalogProfileJob.source_id == source_id)
        .order_by(CatalogProfileJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def load(session: AsyncSession, source_id: str, job_id: str) -> CatalogProfileJob | None:
    """这个源的这个任务；不存在、不属于这个源都是 None。"""
    job = await session.get(CatalogProfileJob, job_id)
    return job if job is not None and job.source_id == source_id else None


#: 逐表一行要的列：不读发现、跳过的项、发过的查询（任务进行中界面每秒都要取一次）
_ROW_COLUMNS = (CatalogProfileTask.seq, CatalogProfileTask.table_name, CatalogProfileTask.status,
                CatalogProfileTask.checks, CatalogProfileTask.current, CatalogProfileTask.queries,
                CatalogProfileTask.elapsed_ms, CatalogProfileTask.row_estimate, CatalogProfileTask.summary,
                CatalogProfileTask.skipped_count, CatalogProfileTask.unfinished, CatalogProfileTask.added,
                CatalogProfileTask.updated, CatalogProfileTask.removed, CatalogProfileTask.version,
                CatalogProfileTask.error, CatalogProfileTask.started_at, CatalogProfileTask.finished_at)


async def task_rows(session: AsyncSession, job_id: str) -> list[Any]:
    return list((await session.execute(
        select(*_ROW_COLUMNS).where(CatalogProfileTask.job_id == job_id).order_by(CatalogProfileTask.seq)
    )).all())


def _by_kind(checks: list[Any]) -> dict[str, list[int]]:
    """检查项按种类的进度：{relation: [结束了的, 总数], codes: […], date: […]}。结束了的含跳过的。"""
    out = {"relation": [0, 0], "codes": [0, 0], "date": [0, 0]}
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in out:
            continue
        pair = out[check["kind"]]
        pair[1] += 1
        if check.get("state") in ("done", "skipped"):
            pair[0] += 1
    return out


def task_row(task: Any) -> dict[str, Any]:
    """逐表一行（CatalogProfileTaskRow）。task 是 CatalogProfileTask，或者 task_rows 取的那几列。"""
    checks = [c for c in (task.checks or []) if isinstance(c, dict)]
    return {
        "seq": task.seq,
        "table_name": task.table_name,
        "status": task.status,
        "checks_total": len(checks),
        "checks_done": sum(1 for c in checks if c.get("state") in ("done", "skipped")),
        "checks_by_kind": _by_kind(checks),
        "current": task.current if task.status == "running" else None,
        "queries": task.queries or 0,
        "elapsed_ms": task.elapsed_ms or 0,
        "row_estimate": task.row_estimate,
        "summary": {**_empty_summary(), **(task.summary or {})},
        "skipped_count": task.skipped_count or 0,
        "unfinished": task.unfinished or 0,
        "added": task.added or 0,
        "updated": task.updated or 0,
        "removed": task.removed or 0,
        "version": task.version or 0,
        "error": task.error,
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


def _counts(statuses: list[str]) -> dict[str, int]:
    counts = dict.fromkeys(TASK_STATUSES, 0)
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def job_dict(job: CatalogProfileJob, tasks: list[Any], *, waiting_for: str | None = None) -> dict[str, Any]:
    """接口返回的任务（CatalogProfileJob）：逐表按剖析顺序；计数、合计都按逐表的算。"""
    rows = [task_row(t) for t in tasks]
    counts = _counts([r["status"] for r in rows])
    summary = _empty_summary()
    for r in rows:
        for key in summary:
            summary[key] += int(r["summary"].get(key) or 0)
    return {
        "id": job.id,
        "source_id": job.source_id,
        "status": job.status,
        "scope": job.scope,
        "settings": ProfileSettings.from_dict(job.settings).to_dict(),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "created_by": job.created_by,
        "retry_of": job.retry_of,
        "waiting_for": waiting_for,
        "total": len(rows),
        "done": sum(counts.get(s, 0) for s in TASK_DONE),
        "counts": counts,
        "queries": job.queries or 0,
        "timeouts": job.timeouts or 0,
        "errors": job.errors or 0,
        "stop_reason": job.stop_reason,
        "notice": job.notice,
        "error": job.error,
        "added": sum(r["added"] for r in rows),
        "updated": sum(r["updated"] for r in rows),
        "removed": sum(r["removed"] for r in rows),
        "summary": summary,
        "tasks": rows,
    }


async def waiting_for(session: AsyncSession, job: CatalogProfileJob) -> str | None:
    """排队中的剖析任务在等什么：排在同源的起草任务（或同步起草）后面时是 "draft"，否则 None。

    先看这个源的队（进程内，排在它前面的是谁）；任务刚建、后台还没排进队时看库：同源有比它先建的、排队或进行中的起草
    任务，或者正在同步起草。"""
    if job.status != "queued":
        return None
    ahead = runtime.lanes.ahead(job.source_id, job.id)
    if ahead is not None:
        return "draft" if any(kind in ("draft", "sync_draft") for kind in ahead) else None
    from app.data import catalog_jobs

    if catalog_jobs.sync_drafting(job.source_id):
        return "draft"
    earlier = (await session.execute(
        select(CatalogDraftJob.id).where(CatalogDraftJob.source_id == job.source_id,
                                         CatalogDraftJob.status.in_(ACTIVE),
                                         CatalogDraftJob.created_at <= job.created_at).limit(1)
    )).first()
    return "draft" if earlier is not None else None


async def job_out(session: AsyncSession, job: CatalogProfileJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return job_dict(job, await task_rows(session, job.id), waiting_for=await waiting_for(session, job))


async def job_brief(session: AsyncSession, job: CatalogProfileJob | None) -> dict[str, Any] | None:
    """目录清单顶上的进度（CatalogActiveProfileJob）：{id, status, total, done, waiting_for}。"""
    if job is None:
        return None
    rows = (await session.execute(
        select(CatalogProfileTask.status, func.count()).where(CatalogProfileTask.job_id == job.id)
        .group_by(CatalogProfileTask.status)
    )).all()
    counts = {status: n for status, n in rows}
    return {"id": job.id, "status": job.status, "total": sum(counts.values()),
            "done": sum(counts.get(s, 0) for s in TASK_DONE), "waiting_for": await waiting_for(session, job)}


def task_detail(job: CatalogProfileJob, task: CatalogProfileTask) -> dict[str, Any]:
    """一张表的明细（CatalogProfileTaskDetail）：逐表一行，加上检查项、发现、跳过的项、日期范围、发过的查询。"""
    return {
        **task_row(task),
        "job_id": job.id,
        "job_status": job.status,
        "created_by": job.created_by,
        "checks": [c for c in task.checks or [] if isinstance(c, dict)],
        "findings": list(task.findings or []),
        "skipped": list(task.skipped or []),
        "date_ranges": list(task.date_ranges or []),
        "query_log": list(task.query_log or []),
    }


async def load_task(session: AsyncSession, job: CatalogProfileJob, seq: int) -> CatalogProfileTask | None:
    return (await session.execute(
        select(CatalogProfileTask).where(CatalogProfileTask.job_id == job.id, CatalogProfileTask.seq == seq)
    )).scalar_one_or_none()


#: 同一张表的各项谁算「最近」：任务建得晚的在前；同一个任务里同名的取 seq 小的
_NEWEST = (CatalogProfileJob.created_at.desc(), CatalogProfileJob.id.desc(), CatalogProfileTask.seq)
#: 表清单、表详情显示的「上次剖析」：真正开始过（started_at 有值）的排在没开始过的前面，各自再按 _NEWEST
_SHOWN = (case((CatalogProfileTask.started_at.is_(None), 1), else_=0), *_NEWEST)


def _ranked(source_id: str, *columns: Any) -> Any:
    """每张表剖析过的各项：子查询，带两个排名。

    - shown：这张表的「上次剖析」（表清单每行、表详情、要不要剖析都按它；保留也保护它所在的任务）——真正开始过的最近一项，
      一次都没开始过才退回最近一项。没开始就结束了的那一项（停在前面的表上、任务止损或整体出错后没轮到、建任务时就判为
      跳过）、排队中还没轮到的那一项都不算：它没有行数估算、发过的查询，要是挡掉上一次真正剖析的那一项，表详情就只剩
      一句「已停止」，那次剖析所在的任务还会被当成不是谁的最近一次给清掉——行数、日期范围、发过的查询只存在剖析任务里。
    - latest：最近一项，不管开始没有。它还排着、在做（pending / running）说明这张表已经在进行中的任务里了。
    """
    shown = func.row_number().over(partition_by=CatalogProfileTask.table_name, order_by=_SHOWN).label("shown")
    latest = func.row_number().over(partition_by=CatalogProfileTask.table_name, order_by=_NEWEST).label("latest")
    return (select(*columns, shown, latest).join(CatalogProfileJob, CatalogProfileJob.id == CatalogProfileTask.job_id)
            .where(CatalogProfileJob.source_id == source_id)).subquery()


async def last_profiles(session: AsyncSession, source_id: str) -> dict[str, dict[str, Any]]:
    """每张表的「上次剖析」（见 _ranked 的 shown）：{表名: CatalogLastProfile 加上内部用的 paths（那一项检查项的路径，判断
    有没有新的待核实关系用）、keys（那一项检查项的 (kind, target)，判断有没有新的码值、日期检查用）、queued（最近一项还排着
    或在做：这张表已经在进行中的任务里了）}。在做的就是进行中的状态。
    每张表取哪一项在库里挑好。"""
    inner = _ranked(source_id, CatalogProfileTask.table_name, CatalogProfileTask.job_id,
                    CatalogProfileTask.seq, CatalogProfileTask.status, CatalogProfileTask.started_at,
                    CatalogProfileTask.finished_at, CatalogProfileTask.summary,
                    CatalogProfileTask.skipped_count, CatalogProfileTask.unfinished,
                    CatalogProfileTask.error, CatalogProfileTask.row_estimate, CatalogProfileTask.checks)
    rows = list((await session.execute(
        select(inner).where(or_(inner.c.shown == 1, inner.c.latest == 1)))).mappings())
    queued = {row["table_name"] for row in rows if row["latest"] == 1 and row["status"] in OPEN}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["shown"] != 1:
            continue
        out[row["table_name"]] = {
            "job_id": row["job_id"], "seq": row["seq"], "status": row["status"],
            "started_at": _iso(row["started_at"]), "finished_at": _iso(row["finished_at"]),
            "summary": {**_empty_summary(), **(row["summary"] or {})}, "skipped_count": row["skipped_count"] or 0,
            "unfinished": row["unfinished"] or 0, "error": row["error"], "row_estimate": row["row_estimate"],
            "paths": {c.get("path") for c in row["checks"] or [] if isinstance(c, dict) and c.get("path")},
            "keys": {(c.get("kind"), c.get("target")) for c in row["checks"] or [] if isinstance(c, dict)},
            "queued": row["table_name"] in queued,
        }
    return out


#: last_profiles 里只给内部用的键
_INTERNAL = ("paths", "keys", "queued")


def public_last(last: dict[str, Any] | None) -> dict[str, Any] | None:
    """接口里的 CatalogLastProfile：去掉内部用的 paths、keys、queued。"""
    return None if last is None else {k: v for k, v in last.items() if k not in _INTERNAL}


async def last_detail(session: AsyncSession, source_id: str, table: str) -> dict[str, Any] | None:
    """这张表「上次剖析」（见 _ranked 的 shown）的明细（表详情的「数据剖析」一节）；没剖析过是 None。"""
    found = (await session.execute(
        select(CatalogProfileTask, CatalogProfileJob)
        .join(CatalogProfileJob, CatalogProfileJob.id == CatalogProfileTask.job_id)
        .where(CatalogProfileJob.source_id == source_id, CatalogProfileTask.table_name == table)
        .order_by(*_SHOWN).limit(1)
    )).first()
    return None if found is None else task_detail(found[1], found[0])


def due_reason(meta: dict[str, Any] | None, notes: dict[str, Any] | None, last: dict[str, Any] | None,
               planned: list[catalog_profile.PlannedCheck]) -> str | None:
    """这张表要不要剖析、为什么：never 从没剖析过；unfinished 上次剖析是 partial / failed / cancelled；new_relations
    目录里有待核实的关系不在上次剖析的检查项里；new_checks 上次剖析之后多出了要做的码值、日期检查（上次剖析时没有可剖析的
    项、后来起草或人工把某一列记成了状态类，或者日期列变了）。表结构里没有、视图、没有可剖析的项、已经在进行中的任务里
    （排着或在做）的不要。planned 是按当前目录和表结构列出的检查（catalog_profile.plan_checks）。

    「上次剖析」和表清单显示的是同一项（last_profiles，真正开始过的最近一项）：上次真正剖析完整了，后来某个任务没轮到它
    就停了，不算没做完——不然表清单一边显示「上次剖析：完成」，一边说「上次剖析没做完」。一次都没开始过的，按最近一项算。

    上次是空表而跳过的不算要剖析：表里有没有数据，不发查询就不知道；空表等有了数据，选「全部表」或单独剖析它。"""
    runnable = [c for c in planned if c.skip is None]
    if not isinstance(meta, dict) or meta.get("is_view") or not runnable:
        return None
    if last is None:
        return "never"
    if last.get("queued") or last["status"] in OPEN:
        return None
    if last["status"] in RETRYABLE:
        return "unfinished"
    if catalog_profile.pending_relation_paths(notes) - set(last.get("paths") or ()):
        return "new_relations"
    seen = last.get("keys") or set()
    if any(c.kind != "relation" and (c.kind, c.target) not in seen for c in runnable):
        return "new_checks"
    return None


# ==========================================================================
# 建任务、重试、停止
# ==========================================================================


#: 撞上剖析任务时说法的最后一句（「请等这次剖析结束（或先停止它）{then}」）缺省是建剖析任务的；导入目录、同步起草撞上
#: 时各说各的（catalog_jobs）：那时要做的是导入、起草，不是开始新的剖析
THEN_PROFILE = "再开始新的剖析"


def busy_message(row: Any, brief: dict[str, Any] | None, *, table: str | None = None,
                 position: int | None = None, then: str = THEN_PROFILE) -> str:
    """同源已有剖析任务时的说法：写明进行到第几张；从表详情点「剖析这张表」时再说这张表在不在那个任务里、排第几；最后
    一句按调用方要做的事说（then）。"""
    name = getattr(row, "name", "") or ""
    if brief is None:
        text = f"数据源「{name}」正在剖析"
    elif brief["status"] == "queued":
        text = f"数据源「{name}」已有一个排队中的剖析任务（共 {brief['total']} 张表）"
    else:
        text = f"数据源「{name}」正在剖析：第 {min(brief['done'] + 1, brief['total'])} / {brief['total']} 张表"
    if table is not None:
        text += f"，{table} 在这次剖析里，排第 {position} 张" if position else f"，{table} 不在这次剖析里"
    return text + f"。请等这次剖析结束（或先停止它）{then}"


async def ensure_free(session: AsyncSession, row: Any, tables: list[str] | None = None, *,
                      then: str = THEN_PROFILE) -> None:
    """这个源没有排队或进行中的剖析任务；有就抛 ProfileBusy。只指定了一张表时，说法里带上这张表在不在那个任务里；then 是
    说法的最后一句要做的事（见 busy_message）。"""
    job = await active_job(session, row.id)
    if job is None:
        return
    brief = await job_brief(session, job)
    table = position = None
    if tables and len(tables) == 1:
        key = catalog.resolve_table_name(getattr(row, "schema_cache", None), str(tables[0])) or str(tables[0])
        table = key
        names = [r.table_name for r in await task_rows(session, job.id)]
        position = names.index(key) + 1 if key in names else None
    raise ProfileBusy(busy_message(row, brief, table=table, position=position, then=then))


#: 挑出 0 张表时的说法
NOTHING_NEEDED = ("没有需要剖析的表：能剖析的表都剖析过了，上次剖析也都做完了，之后没有新的待核实关系，也没有新的码值、"
                  "日期列（上次是空表的不算）。要重新剖析，请选「全部表」或指定要剖析的表")
NOTHING_AT_ALL = ("没有可以剖析的表：表结构里的表都是视图，或者没有可剖析的项（要核对的关联关系、像码值的列、日期列都没有，"
                  "或者都被遮罩了）。请先起草数据目录，或指定要剖析的表")


def _skipped_task(job_id: str, seq: int, name: str, reason: str, why: str, checks: list[dict[str, Any]],
                  skipped: list[ProfileSkip], now: Any) -> CatalogProfileTask:
    entries = [s.to_dict() for s in skipped] + [ProfileSkip("table", name, reason, why).to_dict()]
    return CatalogProfileTask(job_id=job_id, seq=seq, table_name=name, status="skipped", checks=checks or None,
                              skipped=entries, skipped_count=len(entries),
                              unfinished=sum(1 for e in entries if e["reason"] in UNFINISHED_REASONS),
                              summary=_empty_summary(), error=why, finished_at=now)


async def _pick(session: AsyncSession, row: Any, source: Any, *, tables: list[str] | None,
                scope: str) -> list[tuple[str, str | None, str | None, list[Any]]]:
    """要剖析的表：[(表名, 跳过的原因 / None, 整句, 检查项)]，按剖析顺序。"""
    cache = getattr(source, "schema_cache", None) or {}
    all_tables = cache.get("tables") or {}
    masked = catalog_profile.masked_of(getattr(row, "options", None))
    entries = await catalog.read_catalog(session, source.id)

    def notes(name: str) -> dict[str, Any]:
        entry = entries.get(name)
        return entry.notes if entry else {}

    def plan(name: str) -> list[Any]:
        return catalog_profile.plan_checks(name, all_tables.get(name), notes(name), tables=all_tables, masked=masked)

    if scope == "selected":
        picked: list[tuple[str, str | None, str | None, list[Any]]] = []
        seen: set[str] = set()
        for name in dict.fromkeys(str(t) for t in tables or []):
            key = catalog.resolve_table_name(cache, name)
            if key is None:
                picked.append((name, "missing", catalog.missing_table_error(name), []))
                continue
            if key in seen:
                continue
            seen.add(key)
            if (all_tables.get(key) or {}).get("is_view"):
                picked.append((key, "view", VIEW_ERROR, []))
                continue
            checks = plan(key)
            if not any(c.skip is None for c in checks):
                picked.append((key, "nothing", NOTHING_ERROR, checks))
            else:
                picked.append((key, None, None, checks))
        return picked
    usage = await catalog.table_usage(session, source)
    last = await last_profiles(session, source.id) if scope == "needed" else {}
    order = {name: i for i, name in enumerate(all_tables)}
    candidates: list[tuple[str, list[Any]]] = []
    for name, meta in all_tables.items():
        if not isinstance(meta, dict) or meta.get("is_view"):
            continue
        checks = plan(name)
        if not any(c.skip is None for c in checks):
            continue
        if scope == "needed" and due_reason(meta, notes(name), last.get(name), checks) is None:
            continue
        candidates.append((name, checks))
    if not candidates:
        raise ProfileNothing(NOTHING_NEEDED if scope == "needed" else NOTHING_AT_ALL)
    # 还有待核实（推断）关系的表排在前面：剖析首先是来核实它们的；再按使用次数、表结构顺序
    candidates.sort(key=lambda c: (not catalog_profile.has_pending_relations(notes(c[0])), -usage.get(c[0], 0),
                                   order[c[0]]))
    return [(name, None, None, checks) for name, checks in candidates]


async def create_job(session: AsyncSession, row: Any, source: Any, *, tables: list[str] | None, scope: str,
                     settings: ProfileSettings, actor: str | None,
                     retry_of: str | None = None) -> tuple[CatalogProfileJob, list[CatalogProfileTask]]:
    """建一个排队的剖析任务（还没开始，调用方接着 manager.start）。row 是数据源记录（遮罩按它的设置），source 是读表结构
    用的源（上传源是绑定当前快照的视图）。同源已有排队或进行中的剖析任务抛 ProfileBusy；挑出 0 张表抛 ProfileNothing。
    同源有起草任务时照建，排在它后面。建完顺手清掉超出保留数的旧任务。"""
    await ensure_free(session, row, tables)
    picked = await _pick(session, row, source, tables=tables, scope=scope)
    job = CatalogProfileJob(id=new_id(), source_id=row.id, status="queued", scope=scope,
                            settings=settings.to_dict(), created_by=actor, retry_of=retry_of)
    now = utcnow()
    tasks: list[CatalogProfileTask] = []
    for seq, (name, reason, why, checks) in enumerate(picked):
        shown = [c.as_check() for c in checks]
        if reason is not None:
            rule = [ProfileSkip(c.kind, c.target, c.skip[0], c.skip[1], c.path) for c in checks if c.skip]
            tasks.append(_skipped_task(job.id, seq, name, reason, str(why), shown, rule, now))
            continue
        tasks.append(CatalogProfileTask(job_id=job.id, seq=seq, table_name=name, status="pending", checks=shown,
                                        summary=_empty_summary()))
    session.add(job)
    try:
        await session.flush()
        session.add_all(tasks)
        await session.commit()
    except IntegrityError as e:
        # 部分唯一索引：另一个请求同时给这个源建了剖析任务
        await session.rollback()
        raise ProfileBusy(busy_message(row, None)) from e
    try:
        await prune(session, row.id)
    except Exception:  # noqa: BLE001 - 清旧任务失败不影响这次剖析
        logger.exception("清理数据源 %s 的旧剖析任务时出错", row.id)
        await session.rollback()
    return job, tasks


async def retry_tables(session: AsyncSession, job: CatalogProfileJob) -> list[str]:
    """重试要带上的表：原任务里 failed、partial、cancelled 的，按原来的顺序。没有抛 NothingToRetry。"""
    names = [t.table_name for t in await task_rows(session, job.id) if t.status in RETRYABLE]
    if not names:
        raise NothingToRetry("这次剖析里没有失败、部分完成或停止了的表，不需要重试")
    return names


async def prune(session: AsyncSession, source_id: str, *, keep: int | None = None) -> int:
    """保留：这个源最近 keep（缺省 KEEP_JOBS）个结束了的剖析任务留着，更早的删掉；某张表「上次剖析」（真正开始过的最近
    一次，见 _ranked）所在的任务不删——排队中还没轮到、没开始就结束了的那一项不算，不然新建一个任务就能把上一次真正剖析
    清掉。返回删了几个。"""
    keep = KEEP_JOBS if keep is None else keep
    finished = list((await session.execute(
        select(CatalogProfileJob.id).where(CatalogProfileJob.source_id == source_id,
                                           CatalogProfileJob.status.in_(FINISHED))
        .order_by(CatalogProfileJob.created_at.desc(), CatalogProfileJob.id.desc())
    )).scalars())
    old = finished[keep:]
    if not old:
        return 0
    inner = _ranked(source_id, CatalogProfileTask.job_id)
    protected = set((await session.execute(select(inner.c.job_id).where(inner.c.shown == 1))).scalars())
    victims = [job_id for job_id in old if job_id not in protected]
    if not victims:
        return 0
    await session.execute(delete(CatalogProfileTask).where(CatalogProfileTask.job_id.in_(victims)))
    await session.execute(delete(CatalogProfileJob).where(CatalogProfileJob.id.in_(victims)))
    await session.commit()
    return len(victims)


async def _close_tasks(session: AsyncSession, job_id: str, task_status: str, task_error: str | None,
                       *, why: str) -> None:
    """还没结束的表记 task_status（原因 task_error）；它们的检查项里还排着的、正在做的记为跳过（停止、中断是 cancelled，
    出错是 error，说明是「{why}，未检查」），计进跳过的项和没做完的项。

    还没结束的表没走到整张表结束那一步：目录只在那一步和这张表的结果一起写（catalog_profile._record，同一个事务），
    所以这里已经查完（done）的项结论也没写进目录——进程没了、任务整体出错（快照被改动……）都是这样。它们同样记为跳过、
    原因同上，说明是「{why}，查完了但结论没有写进数据目录」，算没做完；这张表的原因写明查完的几项没写进目录。不能留着
    「完成」：检查清单上标着完成、目录里却没有，看的人会以为结论已经在目录里了。"""
    now = utcnow()
    reason = "cancelled" if task_status == "cancelled" else "error"
    detail, unsaved = _UNCHECKED.format(why=why), _UNSAVED.format(why=why)
    tasks = (await session.execute(
        select(CatalogProfileTask).where(CatalogProfileTask.job_id == job_id, CatalogProfileTask.status.in_(OPEN))
    )).scalars()
    for task in tasks:
        checks = [dict(c) for c in task.checks or [] if isinstance(c, dict)]
        skipped = list(task.skipped or [])
        listed = {(s.get("kind"), s.get("target"), s.get("path")) for s in skipped if isinstance(s, dict)}
        done = rest = 0
        for check in checks:
            state = check.get("state")
            if state == "done":
                done += 1
                check.update(state="skipped", reason=reason, detail=unsaved)
            elif state in _CHECK_OPEN:
                rest += 1
                check.update(state="skipped", reason=reason, detail=detail)
            elif state != "skipped":
                continue
            # 跳过的项按检查项列全：进行中的表只随时落了检查项，按规则不查的（遮罩……）也要进跳过的项
            key = (check.get("kind"), check.get("target"), check.get("path"))
            if key not in listed:
                listed.add(key)
                skipped.append(ProfileSkip(check.get("kind") or "", check.get("target") or "",
                                           check.get("reason") or "", check.get("detail") or "",
                                           check.get("path")).to_dict())
        error = unsaved_error(task_error or why, done, rest) if done else task_error
        task.status, task.error, task.finished_at, task.current = task_status, error, now, None
        task.checks, task.skipped = checks or None, skipped or None
        task.skipped_count = len(skipped)
        task.unfinished = sum(1 for s in skipped if s.get("reason") in UNFINISHED_REASONS)


async def _close(session: AsyncSession, job_id: str, job_status: str, job_error: str | None, task_status: str,
                 task_error: str | None, *, why: str | None = None) -> None:
    """收尾：还没结束的表记 task_status（见 _close_tasks），任务（还没结束的话）记 job_status。提交。"""
    await _close_tasks(session, job_id, task_status, task_error, why=why or task_error or STOPPED_ERROR)
    await session.execute(
        update(CatalogProfileJob).where(CatalogProfileJob.id == job_id, CatalogProfileJob.status.in_(ACTIVE))
        .values(status=job_status, error=job_error, finished_at=utcnow()).execution_options(synchronize_session=False))
    await session.commit()


async def cancel(session: AsyncSession, job: CatalogProfileJob) -> None:
    """停止一个剖析任务。排队的直接 cancelled（在队里等着的后台执行跟着停掉、出队）；进行中的交给 manager 停：正在等的那条
    查询不再等（库那边按单条时限截止），那张表做完的项照常写进目录，后面的表 cancelled。已经结束的抛 ProfileFinished。"""
    # 先取出来：排队的没停成（刚被后台认领）时要回滚，回滚让 job 的属性全部过期，再读 job.id 就要在异步会话里同步地
    # 重新加载（MissingGreenlet）
    job_id, status = job.id, job.status
    if status in FINISHED:
        raise ProfileFinished("这次剖析已经结束了，不需要停止")
    if status == "queued":
        claimed = await session.execute(
            update(CatalogProfileJob).where(CatalogProfileJob.id == job_id, CatalogProfileJob.status == "queued")
            .values(status="cancelled", finished_at=utcnow()).execution_options(synchronize_session=False))
        if claimed.rowcount == 1:
            await _close_tasks(session, job_id, "cancelled", None, why=STOPPED_ERROR)
            await session.commit()
            await manager.stop(job_id, "user", grace=STOP_GRACE_S)
            return
        await session.rollback()         # 刚被后台认领、开始了：按进行中的停
    if await manager.stop(job_id, "user", grace=STOP_GRACE_S):
        return
    # 库里说在进行，这个进程里却没有在跑它（记录对不上）：直接记停止
    await _close(session, job_id, "cancelled", None, "cancelled", None)


async def startup() -> int:
    """服务启动时：排队、进行中的剖析任务标成 interrupted，没结束的表记 cancelled、写明服务重启，不自动续跑；再把剖析设置的
    旧键迁移掉。返回标了几个任务。"""
    async def interrupt(session: AsyncSession, job_id: str) -> None:
        await _close(session, job_id, "interrupted", INTERRUPTED_ERROR, "cancelled", INTERRUPTED_ERROR)

    n = await runtime.interrupt_on_startup(CatalogProfileJob, interrupt, running=manager.running)
    if n:
        logger.warning("服务重启：%d 个数据剖析任务没有做完，已标为中断，可在数据目录页重试", n)
    async with SessionLocal() as session:
        migrated = await catalog_profile.migrate_settings(session)
    if migrated:
        logger.info("数据剖析设置：%d 个数据源去掉了按整次剖析算的旧预算", migrated)
    return n


# ==========================================================================
# 在后台剖析
# ==========================================================================


class _ProfileRun(runtime.JobRun):
    """一个剖析任务在后台的一次执行（进队、停止、收尾的框架在 runtime.JobRun）。"""

    kind = "profile"
    model = CatalogProfileJob

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.actor: str | None = None
        self.cx: catalog_profile.ProfileContext | None = None
        #: 正在剖析的那张表：seq 和开始的时刻（进度回调据此落库）
        self._seq = -1
        self._table_started = 0.0

    # ---------------------------------------------------------------- 停止、出错时收尾

    async def _stopped(self) -> None:
        if self.stop_reason == "user":
            await self._end("cancelled", None, "cancelled", None)
        else:
            await self._end("interrupted", INTERRUPTED_ERROR, "cancelled", INTERRUPTED_ERROR)

    async def _failed(self, reason: str) -> None:
        await self._end("failed", reason, "failed", reason)

    async def _crashed(self, e: Exception) -> None:
        from app.core.errors import describe_exception, first_line

        logger.exception("数据剖析任务 %s 出错", self.job_id)
        reason = JOB_ERROR.format(line=first_line(e) or describe_exception(e))
        await self._end("failed", reason, "failed", reason)

    async def _end(self, job_status: str, job_error: str | None, task_status: str, task_error: str | None) -> None:
        """停止、出错时收尾：等手上的库写入写完，记下查询计数，再把没结束的表和任务记下来。收尾本身写不进去只记日志（下次
        启动时 startup 会把它标成中断）。"""
        try:
            await self._settle()
            await self._write(self._save_counters)
            await self._write(_close, self.job_id, job_status, job_error, task_status, task_error)
        except Exception:  # noqa: BLE001
            logger.exception("数据剖析任务 %s 收尾时写库出错", self.job_id)

    def stop_text(self) -> str:
        return STOPPED_ERROR if self.stop_reason == "user" else INTERRUPTED_ERROR

    # ---------------------------------------------------------------- 主流程

    async def _run(self) -> None:
        if not await self._write(self._claim):
            return                          # 开始之前就被停止了
        async with SessionLocal() as session:
            job = await session.get(CatalogProfileJob, self.job_id)
            row = await session.get(DataSource, job.source_id) if job is not None else None
            if job is None or row is None:
                raise runtime.JobFailed(SOURCE_GONE)
            from app.data import table_versions

            try:
                source = await table_versions.resolve_source(session, row)
            except table_versions.SnapshotMissing as e:
                raise runtime.JobFailed(str(e)) from e
            pending = [(t.seq, t.table_name) for t in await task_rows(session, job.id) if t.status == "pending"]
            settings = ProfileSettings.from_dict(job.settings)
            self.actor = job.created_by
            options = dict(row.options or {})
        try:
            self.cx = catalog_profile.make_context(source, settings, options=options)
        except ValueError as e:              # 不支持的数据库类型
            raise runtime.JobFailed(str(e)) from e
        hooks = TableHooks(progress=self._progress, query=self._queried, stop_text=self.stop_text)
        for seq, name in pending:
            # 每张表开始前重读数据源：停用了、关掉了剖析就停下；遮罩按最新的设置
            stop = await self._write(self._recheck)
            if stop is not None:
                await self._write(self._halt, stop, None)
                break
            self._table_started = time.monotonic()
            await self._write(self._start_table, seq)

            async def recorded(session: AsyncSession, result: TableResult, seq: int = seq) -> None:
                await self._save_table(session, seq, result)

            hooks.recorded = recorded
            self._seq = seq
            try:
                await catalog_profile.profile_table(self.cx, name, db=self._write, hooks=hooks, actor=self.actor)
            except SnapshotTampered as e:
                raise runtime.JobFailed(str(e)) from e
            if self.cx.runner.stopped:
                # 任务级止损：后面的表 cancelled，带同一句原因
                await self._write(self._halt, self.cx.runner.error, self.cx.runner.stopped)
                break
        await self._settle()
        await self._write(self._finish)

    # ---------------------------------------------------------------- 进度回调（引擎调）

    async def _progress(self, result: TableResult) -> None:
        await self._write(self._save_progress, self._seq, result)

    async def _queried(self, result: TableResult) -> None:
        await self._write(self._save_queries, self._seq, result)

    # ---------------------------------------------------------------- 库操作

    def _task(self, *where: Any) -> Any:
        return update(CatalogProfileTask).where(CatalogProfileTask.job_id == self.job_id, *where) \
            .execution_options(synchronize_session=False)

    def _counters(self) -> dict[str, Any]:
        runner = self.cx.runner if self.cx is not None else None
        if runner is None:
            return {}
        return {"queries": runner.queries, "timeouts": runner.timeouts, "errors": runner.errors,
                "notice": runner.notice}

    async def _save_counters(self, session: AsyncSession) -> None:
        if values := self._counters():
            await session.execute(update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id).values(**values)
                                  .execution_options(synchronize_session=False))
            await session.commit()

    async def _claim(self, session: AsyncSession) -> bool:
        return await runtime.claim(session, CatalogProfileJob, self.job_id)

    async def _recheck(self, session: AsyncSession) -> str | None:
        """重读数据源：停用了、关掉了剖析返回停下的整句；遮罩换成最新的设置。数据源没了抛 JobFailed。"""
        job = await session.get(CatalogProfileJob, self.job_id)
        row = await session.get(DataSource, job.source_id) if job is not None else None
        if row is None:
            raise runtime.JobFailed(SOURCE_GONE)
        if row.enabled is False:
            return SOURCE_DISABLED.format(name=row.name)
        if not catalog_profile.profile_settings(row.options).enabled:
            return PROFILE_TURNED_OFF.format(name=row.name)
        assert self.cx is not None
        self.cx.masked = catalog_profile.masked_of(row.options)
        return None

    async def _halt(self, session: AsyncSession, error: str | None, stop_reason: str | None) -> None:
        """整个任务停下（止损、数据源停用或关掉了剖析）：后面的表 cancelled、error 是同一句原因；任务记下原因，状态到收尾时
        按逐表算。"""
        await _close_tasks(session, self.job_id, "cancelled", error, why=error or STOPPED_ERROR)
        await session.execute(update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id)
                              .values(error=error, stop_reason=stop_reason, **self._counters())
                              .execution_options(synchronize_session=False))
        await session.commit()

    async def _start_table(self, session: AsyncSession, seq: int) -> None:
        await session.execute(self._task(CatalogProfileTask.seq == seq, CatalogProfileTask.status == "pending")
                              .values(status="running", started_at=utcnow()))
        await session.commit()

    def _elapsed(self) -> int:
        return int((time.monotonic() - self._table_started) * 1000)

    async def _save_progress(self, session: AsyncSession, seq: int, result: TableResult) -> None:
        """一项检查开始或结束：这张表的检查项、正在做哪一项、查询数、发过的查询、行数估算，任务的查询计数。"""
        await session.execute(self._task(CatalogProfileTask.seq == seq, CatalogProfileTask.status == "running")
                              .values(checks=[dict(c) for c in result.checks] or None, current=result.current,
                                      queries=result.queries, elapsed_ms=self._elapsed(),
                                      query_log=list(result.query_log) or None,
                                      row_estimate=result.row_estimate.to_dict() if result.row_estimate else None))
        if values := self._counters():
            await session.execute(update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id).values(**values)
                                  .execution_options(synchronize_session=False))
        await session.commit()

    async def _save_queries(self, session: AsyncSession, seq: int, result: TableResult) -> None:
        """发出一条查询之后：这张表和任务的查询计数、提醒（发过的查询本身在检查项结束时一起写）。"""
        await session.execute(self._task(CatalogProfileTask.seq == seq, CatalogProfileTask.status == "running")
                              .values(queries=result.queries, elapsed_ms=self._elapsed()))
        if values := self._counters():
            await session.execute(update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id).values(**values)
                                  .execution_options(synchronize_session=False))
        await session.commit()

    async def _save_table(self, session: AsyncSession, seq: int, result: TableResult) -> None:
        """一张表结束：整张表的结果。和写目录在同一个事务里，由 catalog_profile._record 一起提交，这里不提交。"""
        await session.execute(
            self._task(CatalogProfileTask.seq == seq, CatalogProfileTask.status.in_(OPEN))
            .values(status=result.status, checks=[dict(c) for c in result.checks] or None, current=None,
                    queries=result.queries, elapsed_ms=result.elapsed_ms,
                    row_estimate=result.row_estimate.to_dict() if result.row_estimate else None,
                    findings=list(result.findings) or None, skipped=[s.to_dict() for s in result.skipped] or None,
                    date_ranges=list(result.date_ranges) or None, query_log=list(result.query_log) or None,
                    summary=result.summary(), skipped_count=result.skipped_count, unfinished=result.unfinished,
                    added=result.added, updated=result.updated, removed=result.removed, version=result.version,
                    error=result.error, finished_at=utcnow()))
        if values := self._counters():
            await session.execute(update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id).values(**values)
                                  .execution_options(synchronize_session=False))

    async def _finish(self, session: AsyncSession) -> None:
        """剖析完：万一还有没收尾的表（进度回调写库失败之类）记失败；有 failed、partial、cancelled 的表是 partial，否则
        succeeded。"""
        await _close_tasks(session, self.job_id, "failed", UNFINISHED_ERROR, why=UNFINISHED_ERROR)
        statuses = list((await session.execute(
            select(CatalogProfileTask.status).where(CatalogProfileTask.job_id == self.job_id))).scalars())
        status = "partial" if any(s in RETRYABLE for s in statuses) else "succeeded"
        await session.execute(
            update(CatalogProfileJob).where(CatalogProfileJob.id == self.job_id, CatalogProfileJob.status == "running")
            .values(status=status, finished_at=utcnow(), **self._counters())
            .execution_options(synchronize_session=False))
        await session.commit()


class ProfileJobs(runtime.JobManager):
    """进程内的剖析任务（start / stop / wait / running / shutdown 见 runtime.JobManager）。"""

    def __init__(self) -> None:
        super().__init__(_ProfileRun, label="数据剖析任务", name="catalog-profile")


manager = ProfileJobs()


__all__ = ["INTERRUPTED_ERROR", "KEEP_JOBS", "MAX_TABLES", "NOTHING_AT_ALL", "NOTHING_NEEDED", "NothingToRetry",
           "ProfileBusy", "ProfileFinished", "ProfileJobs", "ProfileNothing", "THEN_PROFILE", "active_job",
           "busy_message", "cancel", "create_job", "due_reason", "ensure_free", "job_brief", "job_dict", "job_out",
           "last_detail", "last_profiles", "latest_job", "load", "load_task", "manager", "prune", "public_last",
           "retry_tables", "startup", "task_detail", "task_row", "task_rows", "unsaved_error", "waiting_for"]
