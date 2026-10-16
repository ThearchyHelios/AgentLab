"""待办（BE-2）：列表、计数、详情、回复、改派、催办。设计文档 §3.7、§3.10。

身份经 api/deps.current_principal 取；关登录时它没有名字，用自报的署名（X-Actor）补上，回复人一栏才写得出人。
回复被拒回 403 / 404 / 409 / 422，{detail, code}，一律带 code（外部系统按它认）；表单不通过时另带 errors（同应用入口的
422）。服务层没带码的拒绝按状态补上同一类的码（api/coded.py 的 TASK_*）。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.coded import (
    ROLE_REQUIRED, TASK_CLOSED, TASK_FORBIDDEN, TASK_INVALID, TASK_NOT_FOUND, CodedHTTPException,
)
from app.api.deps import current_principal, decode_actor
from app.process import tasks as task_service
from app.process.tasks import FormInvalid, TaskRefused

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def principal_of(x_actor: str | None) -> Any:
    return task_service.principal_with_actor(current_principal(), decode_actor(x_actor))


#: 服务层（process/tasks.py、process/actions.py、blueprints/personal.py）没带码的拒绝按状态归到哪一类
_CODE_BY_STATUS = {404: TASK_NOT_FOUND, 403: TASK_FORBIDDEN, 409: TASK_CLOSED, 422: TASK_INVALID}


def refused(e: TaskRefused) -> CodedHTTPException | JSONResponse:
    if isinstance(e, FormInvalid):
        return JSONResponse({"detail": str(e), "code": e.code, "errors": e.errors}, status_code=422)
    return CodedHTTPException(e.status_code, str(e), e.code or _CODE_BY_STATUS.get(e.status_code, TASK_CLOSED))


def _raise(e: TaskRefused) -> Any:
    out = refused(e)
    if isinstance(out, JSONResponse):
        return out
    raise out from e


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@router.get("")
async def list_tasks(
    assigned: Literal["me", "all"] = "me",
    status: str = Query(default="pending", description="pending / resolved / all，可逗号分隔"),
    app_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    principal = principal_of(x_actor)
    if assigned == "all" and principal.mode != "off" and not principal.at_least("builder"):
        raise CodedHTTPException(403, "只能查看派给自己的待办", ROLE_REQUIRED)
    statuses = None if status == "all" else _csv(status) or ["pending"]
    items = await task_service.list_tasks(principal, assigned=assigned, statuses=statuses, app_id=app_id,
                                          run_id=run_id, limit=limit)
    return {"items": items}


@router.get("/count")
async def count_tasks(mine: int = 1, x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """导航徽标：派给我的、待处理的待办数，按来源分开（apps：应用里的；other：画布、定时任务的审批）；latest 是最新一条的开出时间。"""
    return await task_service.count(principal_of(x_actor))


@router.get("/{task_id}")
async def get_task(task_id: str, x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    principal = principal_of(x_actor)
    out = await task_service.detail(task_id, principal)
    if out is None or not task_service.visible(out, principal):
        raise CodedHTTPException(404, "待办不存在，所属的运行可能已被删除", TASK_NOT_FOUND)
    return out


class RespondIn(BaseModel):
    decision: str
    values: dict[str, Any] | None = None
    note: str | None = None
    #: 动作确认：确认卡上显示的内容指纹，原样带回
    payload_hash: str | None = None
    #: 核实「已生效」时填的外部单号
    external_id: str | None = None


@router.post("/{task_id}/respond", response_model=None)
async def respond(task_id: str, payload: RespondIn, x_actor: str | None = Header(default=None)) -> Any:
    values = dict(payload.values or {})
    if payload.external_id:
        values["external_id"] = payload.external_id
    try:
        out = await task_service.respond(task_id, principal_of(x_actor), decision=payload.decision, values=values,
                                         note=payload.note, payload_hash=payload.payload_hash)
    except TaskRefused as e:
        return _raise(e)
    return {"task": out["task"], "resolved": out["resolved"]}


class Target(BaseModel):
    kind: Literal["user", "group"]
    name: str


class ReassignIn(BaseModel):
    to: list[Target] = Field(default_factory=list)
    note: str | None = None


@router.post("/{task_id}/reassign", response_model=None)
async def reassign(task_id: str, payload: ReassignIn, x_actor: str | None = Header(default=None)) -> Any:
    try:
        return await task_service.reassign(task_id, principal_of(x_actor),
                                           to=[t.model_dump() for t in payload.to], note=payload.note)
    except TaskRefused as e:
        return _raise(e)


@router.post("/{task_id}/remind", response_model=None)
async def remind(task_id: str, x_actor: str | None = Header(default=None)) -> Any:
    try:
        return await task_service.remind(task_id, principal_of(x_actor))
    except TaskRefused as e:
        return _raise(e)
