"""生效记录（BE-2，只读；核实走待办）。设计文档 §3.8、§3.10。预览、回执摘要都是遮罩后的，原文在回执工件里。

开登录后 user 角色：列表不给（那是所有应用、所有运行的生效记录，记录页是给搭建者看的），使用者看动作状态经应用页的
动作状态组件；单条只给它能读的运行（can_read_run）上的，核实待办的卡片要用。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import can_read_run, current_principal, ensure_role, restricted
from app.db.base import SessionLocal
from app.db.models import ActionRecord
from app.process.actions import describe, row_out

router = APIRouter(prefix="/api/actions", tags=["actions"])


@router.get("")
async def list_actions(
    run_id: str | None = None,
    app_id: str | None = None,
    state: str | None = Query(default=None, description="逗号分隔，如 unknown,failed"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    ensure_role("builder")
    stmt = select(ActionRecord).order_by(ActionRecord.created_at.desc()).limit(limit)
    if run_id:
        stmt = stmt.where(ActionRecord.run_id == run_id)
    if app_id:
        stmt = stmt.where(ActionRecord.app_id == app_id)
    if states := [s.strip() for s in (state or "").split(",") if s.strip()]:
        stmt = stmt.where(ActionRecord.state.in_(states))
    async with SessionLocal() as session:
        return {"items": [row_out(r) for r in (await session.execute(stmt)).scalars()]}


@router.get("/{action_id}")
async def get_action(action_id: str) -> dict[str, Any]:
    out = await describe(action_id)
    if out is None:
        raise HTTPException(404, "生效记录不存在，所属的运行可能已被删除")
    if restricted() and not await can_read_run(current_principal(), out["run_id"]):
        raise HTTPException(403, "没有权限查看这条生效记录")
    return out
