"""个人应用与审核（docs/design/normal-user-apps.md §3.9）、页面编辑的候选（§2.6），以及记录的逐行查看（§3.7 records_list）。

要在 apps.router 之前登记（共用 /api/apps 前缀，路径不冲突）。包 B4 实现个人应用和候选，记录列表由包 B3 实现。

- POST /api/blueprints/{id}/save-personal：存成个人应用（拥有者；使用者要管理员开了开关）→ {app_id, blueprint}
- POST /api/blueprints/{id}/copy：审核的搭建者取共享副本 → Blueprint
- POST /api/apps/{id}/refresh：个人应用刷新一次（探索运行，受每日上限）→ {run_id, remaining}
- POST /api/apps/{id}/submit-review {to, note?}：提交审核 → {task_id}
- GET /api/apps/{id}/candidates：页面编辑「加一张数据卡片」的候选（要 manage）→ {items}
- GET /api/apps/{id}/records：记录表的逐行，按看的人实时过滤（包 B3，records/listing.py）→ RecordsPage

业务规则都在 blueprints/personal.py、apps/candidates.py，这里只把拒绝换成 HTTP 响应（带 personal.* 机器码）。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import Principal, actor_of, current_principal
from app.db.base import get_session

router = APIRouter(prefix="/api", tags=["personal"])

_TODO = "尚未实现"


def _principal(x_actor: str | None) -> Principal:
    """这次请求的身份。关登录时 username 换成自报的署名（X-Actor），同应用接口：拥有者、发起人按它认。"""
    principal = current_principal()
    if principal.mode == "off" and principal.username is None:
        return dataclasses.replace(principal, username=actor_of(x_actor))
    return principal


def _refused(e: Any) -> HTTPException:
    """blueprints/personal.PersonalRefused → HTTP（带 personal.* 机器码的回 {detail, code}）。"""
    if e.code:
        return CodedHTTPException(e.status_code, e.message, e.code)
    return HTTPException(e.status_code, e.message)


def blueprint_out(row: Any) -> dict[str, Any]:
    """蓝图的接口形状只有一份（api/blueprints.blueprint_out，带蓝图检查的白话）：存成个人应用、取副本交回的也是它。"""
    from app.api.blueprints import blueprint_out as out

    return out(row)


async def _blueprint(session: AsyncSession, blueprint_id: str) -> Any:
    from app.db.models_blueprints import Blueprint

    return await session.get(Blueprint, blueprint_id)


async def _personal_app(session: AsyncSession, app_id: str, principal: Principal) -> Any:
    """个人应用：只有拥有者看得见。不存在、不是你的，一律 404（不说它在不在）；不是个人应用的另说（409）。"""
    from app.apps.access import app_role
    from app.blueprints.personal import APP_GONE
    from app.db.models_apps import App

    app = await session.get(App, app_id)
    if app is None or await app_role(session, principal, app) is None:
        raise HTTPException(404, APP_GONE)
    return app


# --------------------------------------------------------------------------
# 存成个人应用、共享副本
# --------------------------------------------------------------------------


@router.post("/blueprints/{blueprint_id}/save-personal")
async def save_personal(blueprint_id: str, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> Any:
    from app.blueprints.personal import PersonalRefused, save_personal as save

    principal = _principal(x_actor)
    try:
        app = await save(session, await _blueprint(session, blueprint_id), principal=principal)
    except PersonalRefused as e:
        if e.issues:
            # 蓝图检查没过（缺项、模板不能做成个人应用……）：问题带白话原样交回，卡片上逐条标出来
            return JSONResponse(status_code=e.status_code,
                                content={"detail": e.message, "code": e.code, "issues": e.issues})
        raise _refused(e) from e
    return {"app_id": app.id, "blueprint": blueprint_out(await _blueprint(session, blueprint_id))}


@router.post("/blueprints/{blueprint_id}/copy", status_code=201)
async def copy_for_review(blueprint_id: str, session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.blueprints.personal import PersonalRefused, copy_for_review as copy

    try:
        row = await copy(session, await _blueprint(session, blueprint_id), principal=_principal(x_actor))
    except PersonalRefused as e:
        raise _refused(e) from e
    return blueprint_out(row)


# --------------------------------------------------------------------------
# 刷新、提交审核
# --------------------------------------------------------------------------


@router.post("/apps/{app_id}/refresh")
async def refresh(app_id: str, session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """个人应用刷新一次：交回这次运行（正在刷新的接上那一次）和今天还能刷新几次。"""
    from app.blueprints.personal import PersonalRefused, refresh as run_refresh
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app = await _personal_app(session, app_id, principal)
    try:
        run, remaining = await run_refresh(session, app, principal=principal)
    except PersonalRefused as e:
        raise _refused(e) from e
    except LaunchRefused as e:
        raise (CodedHTTPException(e.status_code, e.message, e.code) if e.code
               else HTTPException(e.status_code, e.message)) from e
    return {"run_id": run.id, "remaining": remaining}


class ReviewIn(BaseModel):
    #: 交给谁：[{kind: user|group, name}]
    to: list[dict[str, str]] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/apps/{app_id}/submit-review")
async def submit_review(app_id: str, payload: ReviewIn, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.blueprints.personal import PersonalRefused, submit_review as submit

    principal = _principal(x_actor)
    app = await _personal_app(session, app_id, principal)
    try:
        task = await submit(session, app, to=payload.to, note=payload.note, principal=principal)
    except PersonalRefused as e:
        raise _refused(e) from e
    return {"task_id": task.id}


# --------------------------------------------------------------------------
# 页面编辑的候选
# --------------------------------------------------------------------------


@router.get("/apps/{app_id}/candidates")
async def candidates(app_id: str, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """「加一张数据卡片」的候选：要这个应用的管理权限（个人应用的拥有者算管理）。按草稿列（页面编辑改的是草稿）。"""
    from app.api.apps import _app
    from app.apps.candidates import app_candidates

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    return {"items": await app_candidates(session, app.draft or {})}


# --------------------------------------------------------------------------
# 记录的逐行查看（包 B3）
# --------------------------------------------------------------------------


@router.get("/apps/{app_id}/records")
async def records(app_id: str, table: str | None = None, cursor: str | None = None, history: bool = False,
                  who: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                  session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """记录表的逐行（组件 records_list）：每次打开按看的人实时过滤，不走封存。use 只看自己提交的，observe、manage 看
    全部（who=mine 只看自己的）；history 连被更正过的行一起列。形状同前端的 RecordsPage（records/listing.py）。"""
    from app.api.apps import _app
    from app.records.listing import page

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "use")
    return await page(session, app, principal=principal, role=role, table=table, who=who, cursor=cursor,
                      history=history, limit=limit)
