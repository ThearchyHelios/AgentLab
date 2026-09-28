from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.core import artifact_store

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

_MAX_INLINE = 2 * 1024 * 1024


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict[str, Any]:
    """按内容哈希取回一个工件。取回时会复验哈希——内容不可变是这层的全部承诺。"""
    try:
        content = artifact_store.load(artifact_id)
    except ValueError as e:
        # 哈希对不上：明确报给调用方，这正是工件库存在的意义
        raise HTTPException(409, str(e)) from e
    if content is None:
        raise HTTPException(404, "这个工件不存在，可能已经被删了")
    return {"id": artifact_id, "content": content}
