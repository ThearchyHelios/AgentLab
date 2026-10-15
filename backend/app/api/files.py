"""文件的上传、下载、删除（docs/design/normal-user-apps.md §3.8）。包 B3 实现。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("", status_code=201)
async def upload() -> dict[str, Any]:
    """multipart：file + context（{app_id, entry} | {task_id} | {blueprint_id}），交回引用 {file_id, name, size, sha256, type}。"""
    raise HTTPException(501, "尚未实现")


@router.get("/{file_id}")
async def download(file_id: str) -> Any:
    raise HTTPException(501, "尚未实现")


@router.delete("/{file_id}", status_code=204)
async def delete(file_id: str) -> None:
    raise HTTPException(501, "尚未实现")
