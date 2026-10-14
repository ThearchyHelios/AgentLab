"""账本（BE-3，只读）：工作流版本被哪些应用钉住、生效计数；生效记录查询。设计文档 §2.9 L4。

不另建存储：ActionRecord 本身带着工作流、版本、graph_hash、能力版本、payload_hash、回执；「口径 → 应用」就是固定这一版的
应用版本。用在发布弹窗、退役确认、版本列表三处。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Workflow
from app.db.models_process import ActionRecord

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.get("/workflow-versions/{workflow_id}/{version}")
async def workflow_version(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                           ) -> dict[str, Any]:
    """一个工作流版本被哪些应用固定、生效记录数、正式运行数。"""
    from app.apps.versions import usage

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    return await usage(session, workflow_id, version)


def effect_row(r: ActionRecord, *, receipt: bool = True) -> dict[str, Any]:
    return {"id": r.id, "run_id": r.run_id, "node_id": r.node_id, "app_id": r.app_id, "workflow_id": r.workflow_id,
            "workflow_version": r.workflow_version, "graph_hash": r.graph_hash, "capability": r.capability,
            "capability_version": r.capability_version, "environment": r.environment, "state": r.state,
            "attempt": r.attempt, "payload_hash": r.payload_hash, "preview": r.preview,
            "receipt": r.receipt if receipt else None, "error": r.error, "resolution": r.resolution,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@router.get("/effects")
async def effects(workflow_id: str | None = None, version: int | None = None, app_id: str | None = None,
                  state: str | None = None, capability: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500), session: AsyncSession = Depends(get_session)
                  ) -> dict[str, Any]:
    """生效记录，新的在前。演练（simulated）照样列出，状态写明是演练。"""
    query = select(ActionRecord)
    if workflow_id:
        query = query.where(ActionRecord.workflow_id == workflow_id)
    if version is not None:
        query = query.where(ActionRecord.workflow_version == version)
    if app_id:
        query = query.where(ActionRecord.app_id == app_id)
    if state:
        query = query.where(ActionRecord.state == state)
    if capability:
        query = query.where(ActionRecord.capability == capability)
    rows = (await session.execute(query.order_by(ActionRecord.created_at.desc()).limit(limit))).scalars()
    return {"items": [effect_row(r) for r in rows]}
