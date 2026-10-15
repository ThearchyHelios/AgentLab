"""场景模板列表（docs/design/normal-user-apps.md §3.3）：GET /api/templates。包 B1 实现。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("")
async def list_templates() -> dict[str, Any]:
    raise HTTPException(501, "尚未实现")
