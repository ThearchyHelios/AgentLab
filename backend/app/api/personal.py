"""个人应用与审核（docs/design/normal-user-apps.md §3.9）、页面编辑的候选（§2.6），以及记录的逐行查看（§3.7 records_list）。

要在 apps.router 之前登记（共用 /api/apps 前缀，路径不冲突）。包 B4 实现个人应用和候选，记录列表由包 B3 实现。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api", tags=["personal"])

_TODO = "尚未实现"


@router.post("/blueprints/{blueprint_id}/save-personal")
async def save_personal(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/blueprints/{blueprint_id}/copy", status_code=201)
async def copy_for_review(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/apps/{app_id}/refresh")
async def refresh(app_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/apps/{app_id}/submit-review")
async def submit_review(app_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.get("/apps/{app_id}/candidates")
async def candidates(app_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.get("/apps/{app_id}/records")
async def records(app_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)
