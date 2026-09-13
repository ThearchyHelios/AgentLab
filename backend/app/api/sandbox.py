from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.sandbox.base import SandboxLimits
from app.sandbox.manager import sandbox_manager

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


@router.get("/health")
async def health() -> dict[str, Any]:
    return await sandbox_manager.health()


class ExecIn(BaseModel):
    code: str = Field(min_length=1)
    language: str = "python"
    timeout: int = Field(default=30, ge=1, le=300)
    memory_mb: int = Field(default=512, ge=64, le=4096)
    network: bool = False
    session_id: str = "playground"
    files: dict[str, str] = Field(default_factory=dict)


@router.post("/exec")
async def execute(payload: ExecIn) -> dict[str, Any]:
    """沙箱试验台。在画布外面直接跑一段代码，用来试限额、试依赖。"""
    result = await sandbox_manager.run(
        payload.code,
        language=payload.language,
        limits=SandboxLimits(
            timeout=payload.timeout,
            memory_mb=payload.memory_mb,
            network=payload.network,
        ),
        session_id=payload.session_id,
        files=payload.files,
    )
    return result.model_dump()


@router.post("/cleanup/{session_id}")
async def cleanup(session_id: str) -> dict[str, bool]:
    await sandbox_manager.cleanup(session_id)
    return {"ok": True}
