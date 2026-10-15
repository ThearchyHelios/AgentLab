"""数据目录的后台剖析任务（草稿，下一步补全）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.catalog_job_runtime import ACTIVE, TASK_DONE
from app.db.models import CatalogProfileJob, CatalogProfileTask


class ProfileBusy(Exception):
    """这个数据源已经有排队或进行中的剖析任务。message 给人看。"""


async def active_job(session: AsyncSession, source_id: str) -> CatalogProfileJob | None:
    return (await session.execute(
        select(CatalogProfileJob).where(CatalogProfileJob.source_id == source_id,
                                        CatalogProfileJob.status.in_(ACTIVE))
        .order_by(CatalogProfileJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def busy_message(row: Any, progress: tuple[int, int] | None) -> str:
    name = getattr(row, "name", "") or ""
    return f"数据源「{name}」正在剖析，请等这次剖析结束（或先停止它）再开始新的剖析"


async def ensure_free(session: AsyncSession, row: Any) -> None:
    job = await active_job(session, row.id)
    if job is not None:
        rows = (await session.execute(
            select(CatalogProfileTask.status, func.count()).where(CatalogProfileTask.job_id == job.id)
            .group_by(CatalogProfileTask.status))).all()
        counts = dict(rows)
        raise ProfileBusy(busy_message(row, (sum(counts.get(s, 0) for s in TASK_DONE), sum(counts.values()))))
