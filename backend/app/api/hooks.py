"""入站钩子（BE-2）：POST /api/hooks/{hook_id}，只认签名、不走会话。设计文档 §2.4。

签名、时间窗、幂等键、start / signal 的规则见 app/process/hooks.py。钩子的增删在 /api/apps/{id}/hooks（应用包）。
开了登录时，身份中间件要放过这条路径（外部系统没有会话，也不带 X-AgentLab-Request）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.process import hooks
from app.process.tasks import TaskRefused

router = APIRouter(prefix="/api/hooks", tags=["hooks"])


@router.post("/{hook_id}", response_model=None)
async def receive(hook_id: str, request: Request) -> Any:
    from app.apps.entries import FormRejected

    body = await request.body()
    try:
        return await hooks.receive(hook_id, request.headers, body)
    except hooks.HookRejected as e:
        raise HTTPException(e.status_code, str(e)) from e
    except FormRejected as e:
        return JSONResponse({"detail": str(e), "code": "form_invalid", "errors": e.errors}, status_code=422)
    except TaskRefused as e:
        raise HTTPException(e.status_code, str(e)) from e
