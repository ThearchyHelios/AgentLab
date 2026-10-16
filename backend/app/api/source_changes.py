"""数据源的变化记录（docs/design/next-round/03-数据接入与目录维护.md B3）：结构变化、取值变化，查看与处理；以及同一前缀下
数据源定时维护要做哪几步（B4）。

挂在 /api/datasources/{id}/… 下，要在 datasources 的通配 /{source_id} 之前登记（main.py）：

- GET  /{id}/changes                 变化列表（缺省开着的和知道了的；status=all 全部）：按「使用次数权重 × 严重程度」排，
                                     同分新的在前；带各级别的计数（数据源卡片的角标「N 个变化要处理」按它，第 4 波）
- GET  /{id}/changes/{change_id}     一条变化
- POST /{id}/changes/{change_id}/resolve  处理：{action, note?, codes?}。动作落到目录已有的写入接口并记目录版本
                                     （data/source_changes.handle）；误报必须写理由（422）；已经处理过的 409
- GET  /{id}/maintenance             定时维护要做哪几步、剖析开没开、指向这个源的维护定时
- PUT  /{id}/maintenance             改要做哪几步：{steps: [introspect | profile_needed | observe]}。定时本身（cron、时区、开关）
                                     在 /api/schedules 里建（target_kind=source_maintenance），这里只管步骤

访问密钥默认不放（auth/keys.py 的 KEY_ROUTES 没有登记这几条）。界面（审阅页「变化」标签页、数据源角标、定时维护设置）在
第 4 波。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import actor_of
from app.data import catalog, source_changes as sc, source_maintenance
from app.db.base import get_session
from app.db.models import DataSource

router = APIRouter(prefix="/api/datasources", tags=["source-changes"])

#: 一页最多几条
LIMIT_MAX = 200


async def _source(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if row is None:
        raise HTTPException(404, "数据源不存在，可能已被删除")
    return row


@router.get("/{source_id}/changes")
async def list_changes(source_id: str, status: str = Query("pending"), kind: list[str] | None = Query(None),
                       table: str | None = Query(None), limit: int = Query(100, ge=1, le=LIMIT_MAX),
                       offset: int = Query(0, ge=0), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{items: [变化（agentlab-change/1）], total, counts: {pending, open, by_severity}}。"""
    if status not in ("pending", "all", *sc.STATUSES):
        raise HTTPException(422, f"不认识的状态：{status}。可选：pending、all、{'、'.join(sc.STATUSES)}")
    if kind and (bad := [k for k in kind if k not in sc.KINDS]):
        raise HTTPException(422, f"不认识的变化种类：{'、'.join(bad)}")
    row = await _source(session, source_id)
    rows, total = await sc.list_for_source(session, row, status=status, kinds=kind, table=table, limit=limit,
                                           offset=offset)
    return {"items": [sc.change_out(r) for r in rows], "total": total, "counts": await sc.counts(session, row.id)}


@router.get("/{source_id}/changes/{change_id}")
async def get_change(source_id: str, change_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _source(session, source_id)
    found = await sc.load(session, row.id, change_id)
    if found is None:
        raise HTTPException(404, "这条变化不存在，可能已随数据源删除")
    return {"change": sc.change_out(found)}


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
        return await sc.handle(session, row, found, payload.action, actor=actor_of(x_actor), note=payload.note,
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


@router.get("/{source_id}/maintenance")
async def get_maintenance(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{steps, profile_enabled, schedules}：要做哪几步、剖析开没开（没开时剖析、观察两步到点照实记没执行）、指向这个源的
    维护定时。"""
    from app.data import catalog_profile

    row = await _source(session, source_id)
    return {"steps": source_maintenance.steps_of(row.options),
            "profile_enabled": catalog_profile.profile_settings(row.options).enabled,
            "schedules": await source_maintenance.schedules_for(session, row.id)}


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
