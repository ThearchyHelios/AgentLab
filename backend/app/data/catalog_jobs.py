"""数据目录的后台起草任务，以及数据源级的起草要求。

以前起草是一次同步长请求（POST /catalog/draft，界面每 12 张表一个请求）：一个请求出错整个对话框就停，关掉对话框
之后没有任何记录哪些表失败、为什么。现在：

- **建任务**（create_job）：同一个数据源同时只有一个排队或进行中的任务（DraftBusy；部分唯一索引兜底）。不指定表时
  按使用次数取前 20 张；表结构里没有的表照样建进任务、状态 skipped。起草要求（数据源级）和本次说明随任务记下快照。
- **在后台起草**（manager.start）：进程内的 asyncio 任务，引擎是 data/catalog_draft.py（分段、按失败原因处理）。
  宽表每写出一段就并入目录（write_part：这一次合并不删任何东西），整张表结束（不管写全没写全）再按完整覆盖的规则
  收尾一次（catalog.write_draft，规则和同步起草一样：人工确认、驳回过的不动，模型这次没给的项不删以前起草的，只删
  已经不在表结构里的列的模型项，乐观锁冲突重读重并）；新增、更新、删除的计数是各次写入的合计。逐表的状态、到第几段、
  写进目录的段数、块进度、计数、模型留空的列，任务的用量（含超时、没写出来的调用次数）、当前的列数上限、提醒随时落库，
  接口读的是库里的状态。
- **停止**（cancel）：排队的直接 cancelled；进行中的停掉正在进行的模型调用，没开始和没写完的表记 cancelled，已经写进
  目录的（含宽表已经写出来的段）不回滚，任务最后是 cancelled。写过段的表 model_error 说清写进了几段、剩几段没起草。
- **同种任务互斥**（ensure_draft_free）：同一个源同时只有一个排队或进行中的起草任务，同步起草（POST /catalog/draft）
  进行期间也算在起草，建任务、重试都是 DraftBusy。剖析任务不挡起草：同源有剖析任务时起草任务照建，排在它后面
  （waiting_for = "profile"），剖析结束后自动开始（每个源一条队，见 data/catalog_job_runtime.py）。
- **同步写操作要求源空闲**（ensure_idle；sync_draft、import_write 占队）：同步起草、导入目录要的是这个源上没有别的事——有排队或进行中的
  起草任务（DraftBusy）、剖析任务（catalog_profile_jobs.ProfileBusy）都不开始；开始了就占着这个源的队，执行期间
  新建的任务排在它后面。进程内登记（单进程部署）。
- **重试**（retry_tables + create_job）：新任务，只含原任务里 failed、partial、cancelled 的表；用不用模型、本次说明沿用，
  起草要求取数据源当前的。
- **服务重启**：启动时（startup）把排队、进行中的任务标成 interrupted，没结束的表记 cancelled、写明「服务重启，起草
  中断」。不自动续跑：复制出来的数据目录（检查、验收）启动时不能自己去调模型；界面让人点「重试」续上。关停时
  （manager.shutdown）停掉进行中的任务，同样标成 interrupted。

任务的生命周期（进程内执行、停止、同源排队、库写入一条一条来并 shield、启动时标中断、状态词）在
data/catalog_job_runtime.py，剖析任务用的是同一套；这里只写起草自己的步骤。

**起草要求**存在数据源 options 的 catalog_draft_guidance 里（上传表格的源存在它自己的 DataSource 行上），一段文字，
去首尾空白、最多 GUIDANCE_MAX 字，空串等于没有。只经 PUT /catalog/guidance 改：数据源接口不显示它、整份回写 options
时留着（api/datasources.py），拼连接串时摘掉（data/engine.py）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_draft
from app.data import catalog_job_runtime as runtime
from app.data.catalog_job_runtime import ACTIVE, FINISHED, RETRYABLE, STOP_GRACE_S, TASK_DONE, TASK_STATUSES
from app.data.engine import CATALOG_DRAFT_GUIDANCE_OPTION
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import CatalogDraftJob, CatalogDraftTask, CatalogProfileJob, DataSource

logger = logging.getLogger(__name__)

_OPEN = runtime.OPEN

#: 起草这边占同源队的几种事（叫法登记在运行时里，见 core/job_runtime.register_lane_kind）：后台起草任务、同步起草、导入目录
runtime.register_lane_kind("draft", "起草")
runtime.register_lane_kind("sync_draft", "同步起草")
runtime.register_lane_kind("import", "导入目录")

#: 一个任务最多几张表；起草要求、本次说明最多几个字
MAX_TABLES = 500
GUIDANCE_MAX = 2000
NOTE_MAX = 1000
#: 目录清单上「这张表最近一次起草」只看最近这么多个任务
LAST_DRAFT_JOBS = 100
#: 逐表最多记几个模型留空的列名
BLANK_SAMPLE = 5
INTERRUPTED_ERROR = "服务重启，起草中断"
#: 停止、中断时写过段的表（宽表每写出一段就并入目录）：写进了几段、剩几段没起草
STOPPED_WRITTEN = "已停止：已写进目录 {n} 段，剩下 {m} 段没起草"
INTERRUPTED_WRITTEN = "已中断：已写进目录 {n} 段，剩下 {m} 段没起草"
_WRITTEN = {"cancelled": STOPPED_WRITTEN, "interrupted": INTERRUPTED_WRITTEN}
UNFINISHED_ERROR = "起草结束时这张表还没有写完，请重试这张表"
WRITE_ERROR = "写入数据目录时出错：{line}"
JOB_ERROR = "起草任务出错：{line}"
SOURCE_GONE = "数据源不存在，可能已被删除"


class DraftBusy(Exception):
    """这个数据源已经有排队或进行中的起草任务。message 给人看。"""


class DraftFinished(Exception):
    """要停止的任务已经结束了。"""


class NothingToRetry(Exception):
    """原任务里没有需要重试的表。"""


class TextTooLong(ValueError):
    """起草要求、本次说明超长。message 给人看。"""


class _JobFailed(runtime.JobFailed):
    """任务整体出错（数据源没了、快照没了、模型用不了）：message 就是任务的 error。"""


# ==========================================================================
# 起草要求、本次说明
# ==========================================================================


def clean_text(value: Any, limit: int, label: str) -> str | None:
    """去首尾空白；空串（和不是字符串的）等于没有；超过 limit 个字抛 TextTooLong。"""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) > limit:
        raise TextTooLong(f"{label}最多 {limit} 字，当前 {len(text)} 字，请删减后再保存")
    return text or None


def guidance_of(row: Any) -> str | None:
    """数据源级的起草要求（没有是 None）。"""
    raw = (getattr(row, "options", None) or {}).get(CATALOG_DRAFT_GUIDANCE_OPTION)
    return raw.strip() or None if isinstance(raw, str) else None


async def save_guidance(session: AsyncSession, row: DataSource, value: Any) -> str | None:
    """存起草要求，返回存下的（清除了是 None）。超长抛 TextTooLong，什么都不改。"""
    text = clean_text(value, GUIDANCE_MAX, "起草要求")
    options = {k: v for k, v in (row.options or {}).items() if k != CATALOG_DRAFT_GUIDANCE_OPTION}
    if text:
        options[CATALOG_DRAFT_GUIDANCE_OPTION] = text
    row.options = options
    await session.commit()
    return text


# ==========================================================================
# 读任务
# ==========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def active_job(session: AsyncSession, source_id: str) -> CatalogDraftJob | None:
    """排队或进行中的那个（同一个源最多一个）。"""
    return (await session.execute(
        select(CatalogDraftJob).where(CatalogDraftJob.source_id == source_id, CatalogDraftJob.status.in_(ACTIVE))
        .order_by(CatalogDraftJob.created_at.desc(), CatalogDraftJob.id.desc()).limit(1)
    )).scalar_one_or_none()


async def latest_job(session: AsyncSession, source_id: str) -> CatalogDraftJob | None:
    """最近建的一个（任何状态）。"""
    return (await session.execute(
        select(CatalogDraftJob).where(CatalogDraftJob.source_id == source_id)
        .order_by(CatalogDraftJob.created_at.desc(), CatalogDraftJob.id.desc()).limit(1)
    )).scalar_one_or_none()


async def load(session: AsyncSession, source_id: str, job_id: str) -> CatalogDraftJob | None:
    """这个源的这个任务；不存在、不属于这个源都是 None。"""
    job = await session.get(CatalogDraftJob, job_id)
    return job if job is not None and job.source_id == source_id else None


async def tasks_of(session: AsyncSession, job_id: str) -> list[CatalogDraftTask]:
    return list((await session.execute(
        select(CatalogDraftTask).where(CatalogDraftTask.job_id == job_id).order_by(CatalogDraftTask.seq)
    )).scalars())


def task_dict(task: CatalogDraftTask) -> dict[str, Any]:
    return {
        "table_name": task.table_name,
        "status": task.status,
        "columns": task.column_count,
        "parts_total": task.parts_total,
        "parts_done": task.parts_done,
        # 宽表每写出一段就并入目录：已经写进目录的段数（停止、服务重启时这些段留着）
        "parts_written": task.parts_written or 0,
        # 块进度：这张表实际分成的块数和已结束的块数（段被缩小、拆成两半后会变多）；不用模型时为 null
        "chunks_total": task.chunks_total,
        "chunks_done": task.chunks_done,
        "added": task.added,
        "updated": task.updated,
        "removed": task.removed,
        "version": task.version,
        "model_items": task.model_items,
        # 模型拿不准、留空了的列（中文名和含义都没给）：几列、最多 5 个列名（按表结构的顺序）。表的状态不因此变
        "blank_columns": task.blank_columns or 0,
        "blank_sample": list(task.blank_sample or []),
        # 起草时记下的几句话，不算失败（模型提议的有效记录条件没有收下的原因：没通过校验、剖析核对排除 0% 或 100%）
        "log": list(task.log or []),
        "error": task.error,
        "model_error": task.model_error,
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


def _counts(statuses: list[str]) -> dict[str, int]:
    counts = dict.fromkeys(TASK_STATUSES, 0)
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def job_dict(job: CatalogDraftJob, tasks: list[CatalogDraftTask], *, waiting_for: str | None = None) -> dict[str, Any]:
    """接口返回的任务（CatalogDraftJob）：逐表的状态按起草顺序；计数、合计都按逐表的算。waiting_for：排队中、排在同源的
    剖析任务后面时是 "profile"（见 waiting_for），否则 null。"""
    counts = _counts([t.status for t in tasks])
    return {
        "id": job.id,
        "source_id": job.source_id,
        "status": job.status,
        "waiting_for": waiting_for,
        "use_model": bool(job.use_model),
        "model": job.model,
        "guidance": job.guidance,
        "note": job.note,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "created_by": job.created_by,
        "retry_of": job.retry_of,
        "total": len(tasks),
        "done": sum(counts.get(s, 0) for s in TASK_DONE),
        "counts": counts,
        "added": sum(t.added or 0 for t in tasks),
        "updated": sum(t.updated or 0 for t in tasks),
        "removed": sum(t.removed or 0 for t in tasks),
        "blank_columns": sum(t.blank_columns or 0 for t in tasks),
        # 超时的调用拿不到用量，token 按 0 算（网关那边可能已经生成了）；failed_calls 含额度用完、只给空壳、超时、出错
        "usage": {"calls": job.calls or 0, "input_tokens": job.input_tokens or 0,
                  "output_tokens": job.output_tokens or 0, "timeouts": job.timeouts or 0,
                  "failed_calls": job.failed_calls or 0},
        # 新开始的段每段最多几列（不用模型、还没开始时为 null）；提醒：最近几次调用多数没写出来时的一句话，没有为 null
        "part_columns": job.part_columns,
        "notice": job.notice,
        "error": job.error,
        "tasks": [task_dict(t) for t in tasks],
    }


async def waiting_for(session: AsyncSession, job: CatalogDraftJob) -> str | None:
    """排队中的起草任务在等什么：排在同源的剖析任务后面时是 "profile"，否则 None（进行中、结束了的都是 None）。

    先看这个源的队（进程内，排在它前面的是谁）；任务刚建、后台还没排进队时看库：同源有比它先建的、排队或进行中的剖析
    任务。"""
    if job.status != "queued":
        return None
    ahead = await runtime.lanes.ahead(job.source_id, job.id)
    if ahead is not None:
        if "profile" in ahead:
            return "profile"
        # 排在同源的探查任务后面（next-round 03 A1：探查也进同一条队）
        return "introspect" if "introspect" in ahead else None
    earlier = (await session.execute(
        select(CatalogProfileJob.id).where(CatalogProfileJob.source_id == job.source_id,
                                           CatalogProfileJob.status.in_(ACTIVE),
                                           CatalogProfileJob.created_at <= job.created_at).limit(1)
    )).first()
    return "profile" if earlier is not None else None


async def job_out(session: AsyncSession, job: CatalogDraftJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return job_dict(job, await tasks_of(session, job.id), waiting_for=await waiting_for(session, job))


async def job_brief(session: AsyncSession, job: CatalogDraftJob | None) -> dict[str, Any] | None:
    """目录清单顶上的进度：{id, status, total, done, waiting_for}。"""
    if job is None:
        return None
    rows = (await session.execute(
        select(CatalogDraftTask.status, func.count()).where(CatalogDraftTask.job_id == job.id)
        .group_by(CatalogDraftTask.status)
    )).all()
    counts = {status: n for status, n in rows}
    return {"id": job.id, "status": job.status, "total": sum(counts.values()),
            "done": sum(counts.get(s, 0) for s in TASK_DONE), "waiting_for": await waiting_for(session, job)}


async def last_drafts(session: AsyncSession, source_id: str) -> dict[str, dict[str, Any]]:
    """每张表最近一次起草任务里的那一项：{表名: {status, job_id, finished_at, error, model_error, blank_columns,
    parts_written, blank_columns_from}}。任务还在进行，就是进行中的状态。只看最近 LAST_DRAFT_JOBS 个任务；每张表取哪一项
    在库里挑好（清单页起草时会反复取）。

    blank_columns_from：最近一次结束于 ok / partial 的用模型起草记下的留空列数（数字），没有是 null。之后的起草停止了、
    中断了、而且没有写进新内容（parts_written 为 0）时照样带着，界面据此继续提示留空的列；之后有起草写进了新内容（停止
    前已经写了几段、或者正在写）就是 null：以前的留空列数可能已经不对了。不用模型的起草不改模型留空的列，不算。"""
    recent = (select(CatalogDraftJob.id).where(CatalogDraftJob.source_id == source_id)
              .order_by(CatalogDraftJob.created_at.desc(), CatalogDraftJob.id.desc()).limit(LAST_DRAFT_JOBS)).scalar_subquery()

    def ranked() -> Any:
        return func.row_number().over(partition_by=CatalogDraftTask.table_name,
                                      order_by=(CatalogDraftJob.created_at.desc(), CatalogDraftTask.seq)).label("rank")

    inner = (select(CatalogDraftTask.table_name, CatalogDraftTask.status, CatalogDraftTask.job_id,
                    CatalogDraftTask.finished_at, CatalogDraftTask.error, CatalogDraftTask.model_error,
                    CatalogDraftTask.blank_columns, CatalogDraftTask.parts_written, ranked())
             .join(CatalogDraftJob, CatalogDraftJob.id == CatalogDraftTask.job_id)
             .where(CatalogDraftJob.id.in_(recent))).subquery()
    rows = (await session.execute(select(inner).where(inner.c.rank == 1))).mappings()
    # 每张表最近一次「写进了模型起草的内容」的用模型起草：结束于 ok / partial 的，或者写过段的（停止、中断、进行中）
    wrote = (select(CatalogDraftTask.table_name, CatalogDraftTask.status, CatalogDraftTask.blank_columns, ranked())
             .join(CatalogDraftJob, CatalogDraftJob.id == CatalogDraftTask.job_id)
             .where(CatalogDraftJob.id.in_(recent), CatalogDraftTask.parts_total.is_not(None),
                    or_(CatalogDraftTask.status.in_(("ok", "partial")),
                        func.coalesce(CatalogDraftTask.parts_written, 0) > 0))).subquery()
    blank_from = {row["table_name"]: (row["blank_columns"] or 0) if row["status"] in ("ok", "partial") else None
                  for row in (await session.execute(select(wrote).where(wrote.c.rank == 1))).mappings()}
    return {row["table_name"]: {"status": row["status"], "job_id": row["job_id"],
                                "finished_at": _iso(row["finished_at"]), "error": row["error"],
                                "model_error": row["model_error"], "blank_columns": row["blank_columns"] or 0,
                                "parts_written": row["parts_written"] or 0,
                                "blank_columns_from": blank_from.get(row["table_name"])}
            for row in rows}


# ==========================================================================
# 建任务、重试、停止
# ==========================================================================


#: 撞上起草时说法的最后一句（「请等这次起草结束{then}」）缺省是建起草任务的；导入目录、同步起草撞上起草、剖析时按这次要做
#: 的事说（_then）
THEN_DRAFT = "再开始新的起草"


def _then(kind: str, busy: str) -> str:
    """同步写操作（kind：import / sync_draft）撞上 busy（draft 起草、profile 剖析）时说法的最后一句：导入撞上什么都是
    「再导入」；同步起草撞上起草是「再开始新的起草」（和建起草任务同一句话），撞上剖析是「再起草」——那时要做的是起草，
    不是「开始新的剖析」。"""
    if kind == "import":
        return "再导入"
    return THEN_DRAFT if busy == "draft" else "再起草"


def busy_message(row: Any, then: str = THEN_DRAFT) -> str:
    return f"数据源「{getattr(row, 'name', '')}」正在起草数据目录，请等这次起草结束（或先停止它）{then}"


def sync_busy_message(row: Any, then: str = THEN_DRAFT) -> str:
    return f"数据源「{getattr(row, 'name', '')}」正在起草数据目录（同步起草），请等这次起草结束{then}"


def import_busy_message(row: Any) -> str:
    return f"数据源「{getattr(row, 'name', '')}」正在导入数据目录，请等导入完成后再试"


#: 本进程里正在同步起草（POST /catalog/draft）的数据源：先登记、再查库（见 sync_draft）。别的进程上的同步起草看同源队里
#: kind=sync_draft 的那一位（队记在库里，多进程共用）
_SYNC: set[str] = set()


async def sync_drafting(source_id: str) -> bool:
    """这个源正在同步起草（本进程里，或者别的进程占着同源队做同步起草）。"""
    if source_id in _SYNC:
        return True
    return any(h.kind == "sync_draft" for h in await runtime.lanes.holders(source_id))


async def ensure_draft_free(session: AsyncSession, row: Any, *, then: str = THEN_DRAFT) -> None:
    """同种任务互斥：这个源没有在起草——没有排队或进行中的起草任务，也没有在同步起草。有就抛 DraftBusy。建起草任务、
    重试之前调它。剖析任务不算：同源有剖析任务时起草任务照建，排在它后面。then 是说法最后一句要做的事。"""
    if await sync_drafting(row.id):
        raise DraftBusy(sync_busy_message(row, then))
    if await active_job(session, row.id) is not None:
        raise DraftBusy(busy_message(row, then))


async def ensure_idle(session: AsyncSession, row: Any, kind: str = "import") -> None:
    """同步写操作（kind：import 导入目录、sync_draft 同步起草）要求源空闲：没有在起草（DraftBusy，同 ensure_draft_free），
    也没有排队或进行中的剖析任务（catalog_profile_jobs.ProfileBusy，说法里写明做完了几张）。说法最后一句按 kind 说
    这次要做的事（_then）。"""
    from app.data import catalog_profile_jobs

    await ensure_draft_free(session, row, then=_then(kind, "draft"))
    await catalog_profile_jobs.ensure_free(session, row, then=_then(kind, "profile"))


def _lane_busy(row: Any, holder: runtime.LaneHolder, kind: str) -> Exception:
    """库里查过没有任务、这个源的队却被占着（同步起草、导入目录正在做，或者一个任务刚好在收尾）：按占着的是谁说，最后
    一句按这次要做的事（kind）说。"""
    if holder.kind == "profile":
        from app.data import catalog_profile_jobs

        return catalog_profile_jobs.ProfileBusy(catalog_profile_jobs.busy_message(row, None,
                                                                                  then=_then(kind, "profile")))
    if holder.kind == "import":
        return DraftBusy(import_busy_message(row))
    if holder.kind == "introspect":
        # 正在探查结构（next-round 03 A1）：表结构做到一半，起草、导入要等它读完
        from app.data import introspect_jobs

        return introspect_jobs.IntrospectBusy(introspect_jobs.busy_message(row, None, then=_then(kind, "profile")))
    then = _then(kind, "draft")
    return DraftBusy(sync_busy_message(row, then) if holder.kind == "sync_draft" else busy_message(row, then))


@contextlib.asynccontextmanager
async def _sync_write(row: Any, kind: str, check: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    """同步写操作期间占着这个源的队（不阻塞地试占）：先查库（check：源上有排队或进行中的任务抛 DraftBusy / ProfileBusy），
    再占队——别的同步写操作正在做时占不到，同样抛忙，什么都不做。占到了，执行期间新建的任务排在它后面，做完才开始。"""
    await check()
    try:
        ticket = await runtime.lanes.try_enter(row.id, kind)
    except runtime.LaneBusy as e:
        raise _lane_busy(row, e.holder, kind) from e
    try:
        yield
    finally:
        await ticket.release()


async def _ensure_idle_but_sync(session: AsyncSession, row: Any) -> None:
    """同 ensure_idle(…, "sync_draft")，只是不看 _SYNC：同步起草自己刚登记在里面。"""
    from app.data import catalog_profile_jobs

    if await active_job(session, row.id) is not None:
        raise DraftBusy(busy_message(row))
    await catalog_profile_jobs.ensure_free(session, row, then=_then("sync_draft", "profile"))


@contextlib.asynccontextmanager
async def sync_draft(session: AsyncSession, row: Any) -> AsyncIterator[None]:
    """同步起草期间登记这个源、占着它的队：同一个源建起草任务、重试、导入、再开一次同步起草都抛 DraftBusy，期间新建的
    剖析任务排在它后面；有排队或进行中的起草任务时这里抛 DraftBusy，有剖析任务时抛 ProfileBusy。先登记、再查库：查库要
    让出事件循环，先登记才不会和同时建的任务互相看不见（建任务那边写库之后也再看一次，见 create_job）。"""
    if await sync_drafting(row.id):
        raise DraftBusy(sync_busy_message(row))
    _SYNC.add(row.id)
    try:
        async with _sync_write(row, "sync_draft", lambda: _ensure_idle_but_sync(session, row)):
            yield
    finally:
        _SYNC.discard(row.id)


@contextlib.asynccontextmanager
async def import_write(session: AsyncSession, row: Any) -> AsyncIterator[None]:
    """导入目录期间占着这个源的队：源上有排队或进行中的起草、剖析任务（或者正在同步起草、导入）时抛 DraftBusy /
    ProfileBusy；导入期间新建的任务排在它后面。"""
    async with _sync_write(row, "import", lambda: ensure_idle(session, row, "import")):
        yield


async def check_model(session: AsyncSession) -> None:
    """起草模型能不能用：不能用抛 catalog.CatalogModelUnavailable（只建模型对象，不发请求）。"""
    await catalog.resolve_draft_model(session)


async def create_job(session: AsyncSession, row: Any, source: Any, *, tables: list[str] | None, use_model: bool,
                     note: str | None, actor: str | None,
                     retry_of: str | None = None) -> tuple[CatalogDraftJob, list[CatalogDraftTask]]:
    """建一个排队的任务（还没开始，调用方接着 manager.start）。row 是数据源记录（起草要求取它的），source 是读表结构
    用的源（上传源是绑定当前快照的视图）。有排队或进行中的起草任务（或正在同步起草）抛 DraftBusy；同源有剖析任务时照建，
    排在它后面。"""
    await ensure_draft_free(session, row)
    cache = getattr(source, "schema_cache", None) or {}
    all_tables = cache.get("tables") or {}
    wanted = list(tables) if tables else await catalog.default_tables(session, source)
    job = CatalogDraftJob(id=new_id(), source_id=row.id, status="queued", use_model=bool(use_model),
                          guidance=guidance_of(row), note=note, created_by=actor, retry_of=retry_of)
    tasks: list[CatalogDraftTask] = []
    seen: set[str] = set()
    now = utcnow()
    for name in dict.fromkeys(str(t) for t in wanted):
        key = catalog.resolve_table_name(cache, name)
        if key is None:
            tasks.append(CatalogDraftTask(job_id=job.id, seq=len(tasks), table_name=name, status="skipped",
                                          error=catalog.missing_table_error(name), finished_at=now))
            continue
        if key in seen:
            continue
        seen.add(key)
        columns = len((all_tables.get(key) or {}).get("columns") or [])
        parts = catalog_draft.parts_for(columns) if use_model else None
        tasks.append(CatalogDraftTask(job_id=job.id, seq=len(tasks), table_name=key, status="pending",
                                      column_count=columns, parts_total=parts, parts_written=0,
                                      chunks_total=parts, chunks_done=0 if use_model else None))
    session.add(job)
    try:
        await session.flush()
        session.add_all(tasks)
        await session.commit()
    except IntegrityError as e:
        # 部分唯一索引：另一个请求同时给这个源建了任务
        await session.rollback()
        raise DraftBusy(busy_message(row)) from e
    if await sync_drafting(row.id):
        # 写库的时候同一个源开始了同步起草（它查库时还没看到这个任务）：这个任务不要了
        await session.execute(delete(CatalogDraftTask).where(CatalogDraftTask.job_id == job.id))
        await session.execute(delete(CatalogDraftJob).where(CatalogDraftJob.id == job.id))
        await session.commit()
        raise DraftBusy(sync_busy_message(row))
    return job, tasks


async def retry_tables(session: AsyncSession, job: CatalogDraftJob) -> list[str]:
    """重试要带上的表：原任务里 failed、partial、cancelled 的，按原来的顺序。没有抛 NothingToRetry。"""
    names = [t.table_name for t in await tasks_of(session, job.id) if t.status in RETRYABLE]
    if not names:
        raise NothingToRetry("这次起草里没有失败、只写出一部分或停止了的表，不需要重试")
    return names


async def cancel(session: AsyncSession, job: CatalogDraftJob) -> None:
    """停止一个任务。排队的直接 cancelled（在队里等着的后台执行跟着停掉、出队）；进行中的交给 manager 停（停掉模型调用、
    没写完的表记 cancelled）。已经结束的抛 DraftFinished。"""
    # 先取出来：排队的没停成（刚被后台认领）时 cancel_queued 会回滚，回滚让 job 的属性全部过期，再读 job.id 就要在
    # 异步会话里同步地重新加载（MissingGreenlet）
    job_id, status = job.id, job.status
    if status in FINISHED:
        raise DraftFinished("这次起草已经结束了，不需要停止")
    if status == "queued":
        if await runtime.cancel_queued(session, CatalogDraftJob, CatalogDraftTask, job_id):
            await manager.stop(job_id, "user", grace=STOP_GRACE_S)
            return
        # 刚被后台认领、开始了：按进行中的停
    if await manager.stop(job_id, "user", grace=STOP_GRACE_S):
        return
    # 库里说在进行，这个进程里却没有在跑它（记录对不上）：直接记停止
    await _close(session, job_id, "cancelled", None, "cancelled", None)


async def _close(session: AsyncSession, job_id: str, job_status: str, job_error: str | None, task_status: str,
                 task_error: str | None) -> None:
    """收尾：还没结束的表记 task_status，任务（还没结束的话）记 job_status。停止、中断时写过段的表 model_error 说清写进了
    几段、剩几段没起草（已经写进去的段留在目录里）。"""
    now = utcnow()
    written = _WRITTEN.get(job_status)
    if written is not None:
        rows = (await session.execute(
            select(CatalogDraftTask.id, CatalogDraftTask.parts_written, CatalogDraftTask.parts_total)
            .where(CatalogDraftTask.job_id == job_id, CatalogDraftTask.status.in_(_OPEN),
                   func.coalesce(CatalogDraftTask.parts_written, 0) > 0))).all()
        for task_id, n, total in rows:
            await session.execute(
                update(CatalogDraftTask).where(CatalogDraftTask.id == task_id, CatalogDraftTask.status.in_(_OPEN))
                .values(status=task_status, error=task_error, finished_at=now,
                        model_error=written.format(n=n, m=max(0, int(total or n) - int(n))))
                .execution_options(synchronize_session=False))
    await runtime.close_job(session, CatalogDraftJob, CatalogDraftTask, job_id, job_status, job_error, task_status,
                            task_error)


async def startup(*, live: Any = None) -> int:
    """服务启动时：排队、进行中的任务标成 interrupted，没结束的表记 cancelled、写明服务重启。不自动续跑。返回标了几个。"""
    async def interrupt(session: AsyncSession, job_id: str) -> None:
        await _close(session, job_id, "interrupted", INTERRUPTED_ERROR, "cancelled", INTERRUPTED_ERROR)

    n = await runtime.interrupt_on_startup(CatalogDraftJob, interrupt, running=manager.running, live=live)
    if n:
        logger.warning("服务重启：%d 个数据目录起草任务没有做完，已标为中断，可在数据目录页重试", n)
    return n


# ==========================================================================
# 在后台起草
# ==========================================================================


class _JobRun(runtime.JobRun):
    """一个起草任务在后台的一次执行（进队、停止、收尾的框架在 runtime.JobRun）。"""

    kind = "draft"
    model = CatalogDraftJob

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self._started: set[str] = set()
        self.source: Any = None
        self.actor: str | None = None

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

        logger.exception("数据目录起草任务 %s 出错", self.job_id)
        reason = JOB_ERROR.format(line=first_line(e) or describe_exception(e))
        await self._end("failed", reason, "failed", reason)

    async def _end(self, job_status: str, job_error: str | None, task_status: str, task_error: str | None) -> None:
        """停止、出错时收尾：等手上的库写入写完，再把没结束的表和任务记下来。收尾本身写不进去只记日志（下次启动时
        startup 会把它标成中断）。"""
        try:
            await self._settle()
            await self._write(_close, self.job_id, job_status, job_error, task_status, task_error)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("数据目录起草任务 %s 收尾时写库出错", self.job_id)

    async def _run(self) -> None:
        if not await self._write(self._claim):
            return                          # 开始之前就被停止了
        async with SessionLocal() as session:
            job = await session.get(CatalogDraftJob, self.job_id)
            row = await session.get(DataSource, job.source_id) if job is not None else None
            if job is None or row is None:
                raise _JobFailed(SOURCE_GONE)
            from app.data import table_versions

            try:
                source = await table_versions.resolve_source(session, row)
            except table_versions.SnapshotMissing as e:
                raise _JobFailed(str(e)) from e
            pending = [t.table_name for t in await tasks_of(session, job.id) if t.status == "pending"]
            use_model, background = bool(job.use_model), catalog_draft.Background(guidance=job.guidance, note=job.note)
            self.actor = job.created_by
            model: Any = None
            existing: dict[str, Any] = {}
            if use_model and pending:
                try:
                    model, model_id = await catalog.resolve_draft_model(session)
                except catalog.CatalogModelUnavailable as e:
                    raise _JobFailed(str(e)) from e
                existing = {t: e.notes for t, e in (await catalog.read_catalog(session, source.id)).items()}
        self.source = source
        all_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
        names = [n for n in pending if isinstance(all_tables.get(n), dict)]
        gone = [n for n in pending if n not in names]
        if gone:
            # 建任务之后表结构变了（重新探查过）：这几张表跳过
            await self._write(self._skip, gone)
        if use_model and names:
            await self._write(self._set_model, model_id)
            hooks = catalog_draft.Hooks(call_started=self._call_started, call_done=self._call_done,
                                        part_done=self._part_done, table_done=self._table_done,
                                        part_written=self._part_written, chunks_changed=self._chunks_changed)
            await catalog_draft.run_model_draft(model, source, names, existing=existing, background=background,
                                                hooks=hooks)
        else:
            for name in names:
                await self._call_started([name])
                await self._table_done(name, None)
        await self._settle()
        await self._write(self._finish)

    # ---------------------------------------------------------------- 进度回调（引擎调）

    async def _call_started(self, tables: list[str]) -> None:
        fresh = [t for t in dict.fromkeys(tables) if t not in self._started]
        if fresh:
            self._started.update(fresh)
            await self._write(self._mark_running, fresh)

    async def _call_done(self, report: catalog_draft.CallReport) -> None:
        await self._write(self._add_call, report)

    async def _part_done(self, table: str) -> None:
        await self._write(self._bump_part, table)

    async def _table_done(self, table: str, outcome: catalog_draft.ModelOutcome | None) -> None:
        await self._write(self._write_table, table, outcome)

    async def _part_written(self, table: str, written: catalog_draft.PartWrite) -> None:
        await self._write(self._write_part, table, written)

    async def _chunks_changed(self, table: str, total: int, done: int) -> None:
        await self._write(self._set_chunks, table, total, done)

    # ---------------------------------------------------------------- 库操作

    def _task(self, *where: Any) -> Any:
        return update(CatalogDraftTask).where(CatalogDraftTask.job_id == self.job_id, *where) \
            .execution_options(synchronize_session=False)

    async def _claim(self, session: AsyncSession) -> bool:
        return await runtime.claim(session, CatalogDraftJob, self.job_id)

    async def _set_model(self, session: AsyncSession, model_id: str) -> None:
        await session.execute(update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id)
                              .values(model=model_id or None, part_columns=max(1, int(catalog.MODEL_BATCH_COLUMNS)))
                              .execution_options(synchronize_session=False))
        await session.commit()

    async def _skip(self, session: AsyncSession, names: list[str]) -> None:
        now = utcnow()
        for name in names:
            await session.execute(self._task(CatalogDraftTask.table_name == name, CatalogDraftTask.status == "pending")
                                  .values(status="skipped", error=catalog.missing_table_error(name), finished_at=now))
        await session.commit()

    async def _mark_running(self, session: AsyncSession, names: list[str]) -> None:
        await session.execute(self._task(CatalogDraftTask.table_name.in_(names), CatalogDraftTask.status == "pending")
                              .values(status="running", started_at=utcnow()))
        await session.commit()

    async def _add_call(self, session: AsyncSession, report: catalog_draft.CallReport) -> None:
        """一次调用：用量、超时、没写出来的次数累加，当前的列数上限和提醒照这次调用之后的写（按调用的先后写进来）。"""
        await session.execute(
            update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id)
            .values(calls=CatalogDraftJob.calls + 1,
                    input_tokens=CatalogDraftJob.input_tokens + int(report.input_tokens),
                    output_tokens=CatalogDraftJob.output_tokens + int(report.output_tokens),
                    timeouts=func.coalesce(CatalogDraftJob.timeouts, 0) + int(report.timeout),
                    failed_calls=func.coalesce(CatalogDraftJob.failed_calls, 0) + int(report.failure is not None),
                    part_columns=report.part_columns, notice=report.notice)
            .execution_options(synchronize_session=False))
        await session.commit()

    async def _bump_part(self, session: AsyncSession, table: str) -> None:
        await session.execute(self._task(CatalogDraftTask.table_name == table)
                              .values(parts_done=CatalogDraftTask.parts_done + 1))
        await session.commit()

    async def _set_chunks(self, session: AsyncSession, table: str, total: int, done: int) -> None:
        await session.execute(self._task(CatalogDraftTask.table_name == table, CatalogDraftTask.status.in_(_OPEN))
                              .values(chunks_total=int(total), chunks_done=int(done)))
        await session.commit()

    async def _write_part(self, session: AsyncSession, table: str, written: catalog_draft.PartWrite) -> None:
        """宽表写出了一段：马上并入目录（这一次不删任何东西），记下写进去的段数、累加计数。写不进去只记日志：整张表结束
        时收尾那次还会把它带上。

        新增、更新的项的修改时间是这一次写入的时间（每次写入各取一次）：以前整个任务只在开始时取一次，起草了几十分钟的宽表，
        每一段写进去的项都记成任务开始那一刻，比任务的开始时间还早。"""
        try:
            found = await write_part(session, self.source, table, written.notes, at=catalog.now_iso(), actor=self.actor)
        except Exception:  # noqa: BLE001 - 这一段写不进去不影响起草
            logger.exception("起草任务 %s 把表 %s 写出来的一段并入数据目录时出错", self.job_id, table)
            await session.rollback()
            return
        if found is None:
            logger.warning("起草任务 %s：表 %s 的数据目录正被修改，这一段等整张表写完时再并入", self.job_id, table)
            return
        stats, version = found
        await session.execute(
            self._task(CatalogDraftTask.table_name == table, CatalogDraftTask.status.in_(_OPEN))
            .values(parts_written=int(written.parts_written), version=version,
                    added=CatalogDraftTask.added + stats.added, updated=CatalogDraftTask.updated + stats.updated,
                    removed=CatalogDraftTask.removed + stats.removed))
        await session.commit()

    async def _write_table(self, session: AsyncSession, table: str,
                           outcome: catalog_draft.ModelOutcome | None) -> None:
        """一张表结束：并入目录、写入（宽表写过的段已经并入过，这里是整张表的收尾），记下这张表的结果。计数加在逐段写入
        的计数上。修改时间取这一次写入的时间（同 _write_part）：逐段写进去、内容没变的项留着那一段的时间。"""
        try:
            result = await catalog.write_draft(session, self.source, table, outcome, at=catalog.now_iso(),
                                               actor=self.actor)
        except Exception as e:  # noqa: BLE001 - 这一张写不进去不影响别的表
            from app.core.errors import describe_exception, first_line

            logger.exception("起草任务 %s 写入表 %s 的数据目录时出错", self.job_id, table)
            await session.rollback()
            result = catalog.TableDraftResult(table=table, status="failed",
                                              error=WRITE_ERROR.format(line=first_line(e) or describe_exception(e)),
                                              model_error=outcome.error if outcome is not None else None)
        values: dict[str, Any] = {
            "status": result.status, "added": CatalogDraftTask.added + result.added,
            "updated": CatalogDraftTask.updated + result.updated, "removed": CatalogDraftTask.removed + result.removed,
            "model_items": result.model_items, "error": result.error,
            "model_error": result.model_error, "finished_at": utcnow(),
            "blank_columns": len(result.blank), "blank_sample": result.blank[:BLANK_SAMPLE] or None,
        }
        if result.version:
            values["version"] = result.version
        if outcome is not None:
            values.update(parts_done=outcome.parts_done, chunks_total=outcome.chunks_total,
                          chunks_done=outcome.chunks_done, log=list(outcome.log) or None)
            if not result.error:
                # 收尾写进去了：写出内容的段都在目录里了。没写进去（一直撞上别人刚改过）的照库里逐段写进去的段数
                values["parts_written"] = outcome.parts_written
        await session.execute(self._task(CatalogDraftTask.table_name == table, CatalogDraftTask.status.in_(_OPEN))
                              .values(**values))
        await session.commit()

    async def _finish(self, session: AsyncSession) -> None:
        """起草完：万一还有没收尾的表（进度回调写库失败之类）记失败；有 failed、partial、cancelled 的表是 partial，
        否则 succeeded。"""
        now = utcnow()
        await session.execute(self._task(CatalogDraftTask.status.in_(_OPEN))
                              .values(status="failed", error=UNFINISHED_ERROR, finished_at=now))
        statuses = list((await session.execute(
            select(CatalogDraftTask.status).where(CatalogDraftTask.job_id == self.job_id))).scalars())
        status = "partial" if any(s in RETRYABLE for s in statuses) else "succeeded"
        await session.execute(
            update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id, CatalogDraftJob.status == "running")
            .values(status=status, finished_at=now).execution_options(synchronize_session=False))
        await session.commit()


async def write_part(session: AsyncSession, source: Any, table: str, notes: dict[str, Any], *, at: str,
                     actor: str | None) -> tuple[catalog.MergeStats, int] | None:
    """宽表写出来的一段并入目录：(计数, 写入后的版本)；撞上别人刚改过、重读重并三次仍冲突返回 None。

    数据库注释、外键、命名推断和这张表到这时拿到的模型起草，按 merge_notes 的规则并入（人工确认、驳回过的不动），
    **这一次不删任何东西**（covered 为空）：删掉表结构里已经没有的、这一轮没再得出的，是整张表写完时 catalog.write_draft
    收尾那一次的事。内容没变不写、不升版本。"""
    structure = catalog.draft_structure(source, table)
    for _attempt in range(3):
        entry = await catalog.read_entry(session, source.id, table)
        version = entry.version if entry else 0
        merged, stats = catalog.merge_notes(entry.notes if entry else {}, structure.notes, at=at)
        merged, more = catalog.merge_notes(merged, notes, at=at)
        stats += more
        if entry is None and not merged:
            return stats, 0
        try:
            written = await catalog.write_entry(session, source.id, table, merged, if_version=version, actor=actor)
        except catalog.CatalogConflict:
            continue
        return stats, written.version
    return None


class DraftJobs(runtime.JobManager):
    """进程内的起草任务（start / stop / wait / running / shutdown 见 runtime.JobManager）。"""

    def __init__(self) -> None:
        super().__init__(_JobRun, label="数据目录起草任务", name="catalog-draft")


manager = DraftJobs()


__all__ = ["ACTIVE", "BLANK_SAMPLE", "DraftBusy", "DraftFinished", "DraftJobs", "FINISHED", "GUIDANCE_MAX",
           "INTERRUPTED_ERROR", "INTERRUPTED_WRITTEN", "MAX_TABLES", "NOTE_MAX", "NothingToRetry", "RETRYABLE",
           "STOPPED_WRITTEN", "TASK_DONE", "TASK_STATUSES", "THEN_DRAFT", "TextTooLong",
           "active_job", "cancel", "check_model", "clean_text", "create_job", "ensure_draft_free", "ensure_idle",
           "guidance_of", "import_busy_message", "import_write", "job_brief", "job_dict", "job_out", "last_drafts",
           "latest_job", "load", "manager", "retry_tables", "save_guidance", "startup", "sync_busy_message", "sync_draft",
           "sync_drafting", "tasks_of", "waiting_for", "write_part"]
