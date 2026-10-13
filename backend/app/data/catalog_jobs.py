"""数据目录的后台起草任务，以及数据源级的起草要求。

以前起草是一次同步长请求（POST /catalog/draft，界面每 12 张表一个请求）：一个请求出错整个对话框就停，关掉对话框
之后没有任何记录哪些表失败、为什么。现在：

- **建任务**（create_job）：同一个数据源同时只有一个排队或进行中的任务（DraftBusy；部分唯一索引兜底）。不指定表时
  按使用次数取前 20 张；表结构里没有的表照样建进任务、状态 skipped。起草要求（数据源级）和本次说明随任务记下快照。
- **在后台起草**（manager.start）：进程内的 asyncio 任务，引擎是 data/catalog_draft.py（分段、按失败原因处理）。
  每张表一结束就并入目录（catalog.write_draft，规则和同步起草一样：人工确认、驳回过的不动，乐观锁冲突重读重并）；
  逐表的状态、到第几段、计数、用量随时落库，接口读的是库里的状态。
- **停止**（cancel）：排队的直接 cancelled；进行中的停掉正在进行的模型调用，没开始和没写完的表记 cancelled，已经写进
  目录的不回滚，任务最后是 cancelled。
- **重试**（retry_tables + create_job）：新任务，只含原任务里 failed、partial、cancelled 的表；用不用模型、本次说明沿用，
  起草要求取数据源当前的。
- **服务重启**：启动时（startup）把排队、进行中的任务标成 interrupted，没结束的表记 cancelled、写明「服务重启，起草
  中断」。不自动续跑：复制出来的数据目录（检查、验收）启动时不能自己去调模型；界面让人点「重试」续上。关停时
  （manager.shutdown）停掉进行中的任务，同样标成 interrupted。

任务的库写入一律经 _JobRun._write：一条一条来，shield 起来——停止任务时不会打断写到一半的库操作，收尾前等它们写完。

**起草要求**存在数据源 options 的 catalog_draft_guidance 里（上传表格的源存在它自己的 DataSource 行上），一段文字，
去首尾空白、最多 GUIDANCE_MAX 字，空串等于没有。只经 PUT /catalog/guidance 改：数据源接口不显示它、整份回写 options
时留着（api/datasources.py），拼连接串时摘掉（data/engine.py）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, catalog_draft
from app.data.engine import CATALOG_DRAFT_GUIDANCE_OPTION
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import CatalogDraftJob, CatalogDraftTask, DataSource

logger = logging.getLogger(__name__)

#: 任务还没结束的状态（同一个源只能有一个）、结束了的状态
ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "partial", "failed", "cancelled", "interrupted")
#: 逐表的状态，以及其中算「已结束」的、重试时要带上的
TASK_STATUSES = ("pending", "running", "ok", "partial", "failed", "skipped", "cancelled")
TASK_DONE = ("ok", "partial", "failed", "skipped", "cancelled")
RETRYABLE = ("failed", "partial", "cancelled")
_OPEN = ("pending", "running")

#: 一个任务最多几张表；起草要求、本次说明最多几个字
MAX_TABLES = 500
GUIDANCE_MAX = 2000
NOTE_MAX = 1000
#: 目录清单上「这张表最近一次起草」只看最近这么多个任务
LAST_DRAFT_JOBS = 100
#: 停止进行中的任务时最多等它收尾多久（秒）；关停服务时另给（shutdown 的 grace）
STOP_GRACE_S = 10.0

INTERRUPTED_ERROR = "服务重启，起草中断"
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


class _JobFailed(Exception):
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
        .order_by(CatalogDraftJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def latest_job(session: AsyncSession, source_id: str) -> CatalogDraftJob | None:
    """最近建的一个（任何状态）。"""
    return (await session.execute(
        select(CatalogDraftJob).where(CatalogDraftJob.source_id == source_id)
        .order_by(CatalogDraftJob.created_at.desc()).limit(1)
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
        "added": task.added,
        "updated": task.updated,
        "removed": task.removed,
        "version": task.version,
        "model_items": task.model_items,
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


def job_dict(job: CatalogDraftJob, tasks: list[CatalogDraftTask]) -> dict[str, Any]:
    """接口返回的任务（CatalogDraftJob）：逐表的状态按起草顺序；计数、合计都按逐表的算。"""
    counts = _counts([t.status for t in tasks])
    return {
        "id": job.id,
        "source_id": job.source_id,
        "status": job.status,
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
        "usage": {"calls": job.calls or 0, "input_tokens": job.input_tokens or 0,
                  "output_tokens": job.output_tokens or 0},
        "error": job.error,
        "tasks": [task_dict(t) for t in tasks],
    }


async def job_out(session: AsyncSession, job: CatalogDraftJob | None) -> dict[str, Any] | None:
    return None if job is None else job_dict(job, await tasks_of(session, job.id))


async def job_brief(session: AsyncSession, job: CatalogDraftJob | None) -> dict[str, Any] | None:
    """目录清单顶上的进度：{id, status, total, done}。"""
    if job is None:
        return None
    rows = (await session.execute(
        select(CatalogDraftTask.status, func.count()).where(CatalogDraftTask.job_id == job.id)
        .group_by(CatalogDraftTask.status)
    )).all()
    counts = {status: n for status, n in rows}
    return {"id": job.id, "status": job.status, "total": sum(counts.values()),
            "done": sum(counts.get(s, 0) for s in TASK_DONE)}


async def last_drafts(session: AsyncSession, source_id: str) -> dict[str, dict[str, Any]]:
    """每张表最近一次起草任务里的那一项：{表名: {status, job_id, finished_at, error, model_error}}。任务还在进行，
    就是进行中的状态。只看最近 LAST_DRAFT_JOBS 个任务；每张表取哪一项在库里挑好（清单页起草时会反复取）。"""
    recent = (select(CatalogDraftJob.id).where(CatalogDraftJob.source_id == source_id)
              .order_by(CatalogDraftJob.created_at.desc()).limit(LAST_DRAFT_JOBS)).scalar_subquery()
    rank = func.row_number().over(partition_by=CatalogDraftTask.table_name,
                                  order_by=(CatalogDraftJob.created_at.desc(), CatalogDraftTask.seq)).label("rank")
    inner = (select(CatalogDraftTask.table_name, CatalogDraftTask.status, CatalogDraftTask.job_id,
                    CatalogDraftTask.finished_at, CatalogDraftTask.error, CatalogDraftTask.model_error, rank)
             .join(CatalogDraftJob, CatalogDraftJob.id == CatalogDraftTask.job_id)
             .where(CatalogDraftJob.id.in_(recent))).subquery()
    rows = (await session.execute(select(inner).where(inner.c.rank == 1))).mappings()
    return {row["table_name"]: {"status": row["status"], "job_id": row["job_id"],
                                "finished_at": _iso(row["finished_at"]), "error": row["error"],
                                "model_error": row["model_error"]} for row in rows}


# ==========================================================================
# 建任务、重试、停止
# ==========================================================================


def busy_message(row: Any) -> str:
    return f"数据源「{getattr(row, 'name', '')}」正在起草数据目录，请等这次起草结束（或先停止它）再开始新的起草"


async def ensure_idle(session: AsyncSession, row: Any) -> None:
    """这个源没有排队或进行中的任务；有就抛 DraftBusy。"""
    if await active_job(session, row.id) is not None:
        raise DraftBusy(busy_message(row))


async def check_model(session: AsyncSession) -> None:
    """起草模型能不能用：不能用抛 catalog.CatalogModelUnavailable（只建模型对象，不发请求）。"""
    await catalog.resolve_draft_model(session)


async def create_job(session: AsyncSession, row: Any, source: Any, *, tables: list[str] | None, use_model: bool,
                     note: str | None, actor: str | None,
                     retry_of: str | None = None) -> tuple[CatalogDraftJob, list[CatalogDraftTask]]:
    """建一个排队的任务（还没开始，调用方接着 manager.start）。row 是数据源记录（起草要求取它的），source 是读表结构
    用的源（上传源是绑定当前快照的视图）。有排队或进行中的任务抛 DraftBusy。"""
    await ensure_idle(session, row)
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
        tasks.append(CatalogDraftTask(job_id=job.id, seq=len(tasks), table_name=key, status="pending",
                                      column_count=columns,
                                      parts_total=catalog_draft.parts_for(columns) if use_model else None))
    session.add(job)
    try:
        await session.flush()
        session.add_all(tasks)
        await session.commit()
    except IntegrityError as e:
        # 部分唯一索引：另一个请求同时给这个源建了任务
        await session.rollback()
        raise DraftBusy(busy_message(row)) from e
    return job, tasks


async def retry_tables(session: AsyncSession, job: CatalogDraftJob) -> list[str]:
    """重试要带上的表：原任务里 failed、partial、cancelled 的，按原来的顺序。没有抛 NothingToRetry。"""
    names = [t.table_name for t in await tasks_of(session, job.id) if t.status in RETRYABLE]
    if not names:
        raise NothingToRetry("这次起草里没有失败、只写出一部分或停止了的表，不需要重试")
    return names


async def cancel(session: AsyncSession, job: CatalogDraftJob) -> None:
    """停止一个任务。排队的直接 cancelled；进行中的交给 manager 停（停掉模型调用、没写完的表记 cancelled）。
    已经结束的抛 DraftFinished。"""
    if job.status in FINISHED:
        raise DraftFinished("这次起草已经结束了，不需要停止")
    if job.status == "queued":
        now = utcnow()
        claimed = await session.execute(
            update(CatalogDraftJob).where(CatalogDraftJob.id == job.id, CatalogDraftJob.status == "queued")
            .values(status="cancelled", finished_at=now).execution_options(synchronize_session=False))
        if claimed.rowcount == 1:
            await session.execute(
                update(CatalogDraftTask).where(CatalogDraftTask.job_id == job.id, CatalogDraftTask.status.in_(_OPEN))
                .values(status="cancelled", finished_at=now).execution_options(synchronize_session=False))
            await session.commit()
            return
        await session.rollback()         # 刚被后台认领、开始了：按进行中的停
    if await manager.stop(job.id, "user", grace=STOP_GRACE_S):
        return
    # 库里说在进行，这个进程里却没有在跑它（记录对不上）：直接记停止
    await _close(session, job.id, "cancelled", None, "cancelled", None)


async def _close(session: AsyncSession, job_id: str, job_status: str, job_error: str | None, task_status: str,
                 task_error: str | None) -> None:
    """收尾：还没结束的表记 task_status，任务（还没结束的话）记 job_status。"""
    now = utcnow()
    await session.execute(
        update(CatalogDraftTask).where(CatalogDraftTask.job_id == job_id, CatalogDraftTask.status.in_(_OPEN))
        .values(status=task_status, error=task_error, finished_at=now).execution_options(synchronize_session=False))
    await session.execute(
        update(CatalogDraftJob).where(CatalogDraftJob.id == job_id, CatalogDraftJob.status.in_(ACTIVE))
        .values(status=job_status, error=job_error, finished_at=now).execution_options(synchronize_session=False))
    await session.commit()


async def startup() -> int:
    """服务启动时：排队、进行中的任务标成 interrupted，没结束的表记 cancelled、写明服务重启。不自动续跑。返回标了几个。"""
    async with SessionLocal() as session:
        ids = [job_id for job_id in (await session.execute(
            select(CatalogDraftJob.id).where(CatalogDraftJob.status.in_(ACTIVE)))).scalars()
            if not manager.running(job_id)]
        for job_id in ids:
            await _close(session, job_id, "interrupted", INTERRUPTED_ERROR, "cancelled", INTERRUPTED_ERROR)
    if ids:
        logger.warning("服务重启：%d 个数据目录起草任务没有做完，已标为中断，可在数据目录页重试", len(ids))
    return len(ids)


# ==========================================================================
# 在后台起草
# ==========================================================================


class _JobRun:
    """一个任务在后台的一次执行。"""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.task: asyncio.Task[None] | None = None
        #: 为什么停：user 使用者点了停止，shutdown 服务关停（标中断）
        self.stop_reason: str | None = None
        self._writes: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._started: set[str] = set()
        self.source: Any = None
        self.actor: str | None = None
        self.at = ""

    # ---------------------------------------------------------------- 库写入

    async def _write(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """一条库写入：同一个任务的写入一条一条来；shield 起来，停止任务时不会打断写到一半的库操作。"""
        async def go() -> Any:
            async with self._lock:
                async with SessionLocal() as session:
                    return await fn(session, *args)

        task = asyncio.ensure_future(go())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        return await asyncio.shield(task)

    async def _settle(self) -> None:
        """等手上的库写入都写完（停止、出错收尾之前）。"""
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)

    # ---------------------------------------------------------------- 主流程

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if self.stop_reason == "user":
                await self._end("cancelled", None, "cancelled", None)
            else:
                await self._end("interrupted", INTERRUPTED_ERROR, "cancelled", INTERRUPTED_ERROR)
        except _JobFailed as e:
            await self._end("failed", str(e), "failed", str(e))
        except Exception as e:  # noqa: BLE001 - 任务整体出错：照实记下，不留在「起草中」
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
        self.source, self.at = source, catalog.now_iso()
        all_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
        names = [n for n in pending if isinstance(all_tables.get(n), dict)]
        gone = [n for n in pending if n not in names]
        if gone:
            # 建任务之后表结构变了（重新探查过）：这几张表跳过
            await self._write(self._skip, gone)
        if use_model and names:
            await self._write(self._set_model, model_id)
            hooks = catalog_draft.Hooks(call_started=self._call_started, usage=self._usage,
                                        part_done=self._part_done, table_done=self._table_done)
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

    async def _usage(self, input_tokens: int, output_tokens: int) -> None:
        await self._write(self._add_usage, input_tokens, output_tokens)

    async def _part_done(self, table: str) -> None:
        await self._write(self._bump_part, table)

    async def _table_done(self, table: str, outcome: catalog_draft.ModelOutcome | None) -> None:
        await self._write(self._write_table, table, outcome)

    # ---------------------------------------------------------------- 库操作

    def _task(self, *where: Any) -> Any:
        return update(CatalogDraftTask).where(CatalogDraftTask.job_id == self.job_id, *where) \
            .execution_options(synchronize_session=False)

    async def _claim(self, session: AsyncSession) -> bool:
        result = await session.execute(
            update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id, CatalogDraftJob.status == "queued")
            .values(status="running", started_at=utcnow()).execution_options(synchronize_session=False))
        await session.commit()
        return result.rowcount == 1

    async def _set_model(self, session: AsyncSession, model_id: str) -> None:
        await session.execute(update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id)
                              .values(model=model_id or None).execution_options(synchronize_session=False))
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

    async def _add_usage(self, session: AsyncSession, input_tokens: int, output_tokens: int) -> None:
        await session.execute(
            update(CatalogDraftJob).where(CatalogDraftJob.id == self.job_id)
            .values(calls=CatalogDraftJob.calls + 1, input_tokens=CatalogDraftJob.input_tokens + int(input_tokens),
                    output_tokens=CatalogDraftJob.output_tokens + int(output_tokens))
            .execution_options(synchronize_session=False))
        await session.commit()

    async def _bump_part(self, session: AsyncSession, table: str) -> None:
        await session.execute(self._task(CatalogDraftTask.table_name == table)
                              .values(parts_done=CatalogDraftTask.parts_done + 1))
        await session.commit()

    async def _write_table(self, session: AsyncSession, table: str,
                           outcome: catalog_draft.ModelOutcome | None) -> None:
        """一张表结束：并入目录、写入，记下这张表的结果。"""
        try:
            result = await catalog.write_draft(session, self.source, table, outcome, at=self.at, actor=self.actor)
        except Exception as e:  # noqa: BLE001 - 这一张写不进去不影响别的表
            from app.core.errors import describe_exception, first_line

            logger.exception("起草任务 %s 写入表 %s 的数据目录时出错", self.job_id, table)
            await session.rollback()
            result = catalog.TableDraftResult(table=table, status="failed",
                                              error=WRITE_ERROR.format(line=first_line(e) or describe_exception(e)),
                                              model_error=outcome.error if outcome is not None else None)
        values: dict[str, Any] = {
            "status": result.status, "added": result.added, "updated": result.updated, "removed": result.removed,
            "version": result.version, "model_items": result.model_items, "error": result.error,
            "model_error": result.model_error, "finished_at": utcnow(),
        }
        if outcome is not None:
            values["parts_done"] = outcome.parts_done
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


class DraftJobs:
    """进程内的起草任务（照 app/scheduler 的写法：一个 asyncio 任务一件事，停的时候给宽限）。"""

    def __init__(self) -> None:
        self._runs: dict[str, _JobRun] = {}

    def _alive(self, job_id: str) -> _JobRun | None:
        run = self._runs.get(job_id)
        if run is None or run.task is None or run.task.done():
            return None
        try:
            if run.task.get_loop() is not asyncio.get_running_loop():
                self._runs.pop(job_id, None)   # 别的事件循环留下的（测试每个用例一个循环）
                return None
        except RuntimeError:
            return None
        return run

    def start(self, job_id: str) -> None:
        """开始在后台起草这个（排队中的）任务。"""
        run = _JobRun(job_id)
        run.task = asyncio.get_running_loop().create_task(run.run(), name=f"catalog-draft-{job_id}")
        self._runs[job_id] = run

        def forget(_: asyncio.Task[None]) -> None:
            if self._runs.get(job_id) is run:
                self._runs.pop(job_id, None)

        run.task.add_done_callback(forget)

    def running(self, job_id: str) -> bool:
        return self._alive(job_id) is not None

    async def stop(self, job_id: str, reason: str, *, grace: float = STOP_GRACE_S) -> bool:
        """停掉这个进程里在跑的任务，等它收尾（最多 grace 秒）。不在这个进程里跑返回 False。"""
        run = self._alive(job_id)
        if run is None:
            return False
        if run.stop_reason is None:
            run.stop_reason = reason
            run.task.cancel()  # type: ignore[union-attr]
        await asyncio.wait([run.task], timeout=grace)  # type: ignore[list-item]
        return True

    async def wait(self, job_id: str, timeout: float | None = None) -> None:
        """等这个任务做完（测试、调试用）。不在跑直接返回。"""
        run = self._alive(job_id)
        if run is not None:
            await asyncio.wait([run.task], timeout=timeout)  # type: ignore[list-item]

    async def shutdown(self, *, grace: float = 3.0) -> None:
        """关停：停掉进行中的任务（标中断），最多等 grace 秒收尾。"""
        runs = [run for job_id in list(self._runs) if (run := self._alive(job_id)) is not None]
        for run in runs:
            if run.stop_reason is None:
                run.stop_reason = "shutdown"
                run.task.cancel()  # type: ignore[union-attr]
        if runs:
            await asyncio.wait([run.task for run in runs], timeout=grace)  # type: ignore[misc]
            logger.info("已停止 %d 个数据目录起草任务", len(runs))


manager = DraftJobs()


__all__ = ["ACTIVE", "DraftBusy", "DraftFinished", "DraftJobs", "FINISHED", "GUIDANCE_MAX", "INTERRUPTED_ERROR",
           "MAX_TABLES", "NOTE_MAX", "NothingToRetry", "RETRYABLE", "TASK_DONE", "TASK_STATUSES", "TextTooLong",
           "active_job", "cancel", "check_model", "clean_text", "create_job", "ensure_idle", "guidance_of", "job_brief",
           "job_dict", "job_out", "last_drafts", "latest_job", "load", "manager", "retry_tables", "save_guidance",
           "startup", "tasks_of"]
