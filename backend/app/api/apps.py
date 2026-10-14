"""应用（BE-3）：增删改查、派生、检查、发布、版本、授权、页面解析、入口、实例、视图刷新、组件目录。设计文档 §3.10。

应用级权限按 §3.12（app/apps/access.py）：use 能用（入口、视图、自己的实例）；observe 另能看全部实例；manage 能搭建、
发布、授权、管钩子。关登录时全部放行、等同 manage。

改 spec、改岗位绑定只改草稿，发布才生成不可变的应用版本；改授权、管钩子不发新版本，写审计（A1）。
"""
from __future__ import annotations

import dataclasses
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import Principal, actor_of, current_principal
from app.apps.access import app_role, at_least, grants_of
from app.db.base import get_session
from app.db.models import Run
from app.db.models_apps import App, AppGrant, AppHook, AppVersion

router = APIRouter(prefix="/api/apps", tags=["apps"])

APP_GONE = "应用不存在，可能已被删除"
NO_ACCESS = "app_forbidden"
NOT_PUBLISHED = "app_unpublished"
PUBLISH_CONFLICT = "app_publish_conflict"
_ROLE_TEXT = {"use": "使用", "observe": "查看全部实例", "manage": "管理"}


@router.get("/catalog")
async def get_catalog() -> dict:
    """受信组件目录 v1。前端按名字找渲染器；助手生成时给模型看同一份。"""
    from app.apps.catalog import catalog_payload

    return catalog_payload()


# --------------------------------------------------------------------------
# 取应用、判权限
# --------------------------------------------------------------------------


def _principal(x_actor: str | None) -> Principal:
    """这次请求的身份。关登录时 username 换成自报的署名（X-Actor）：发起人、我发起的都按它认。"""
    principal = current_principal()
    if principal.mode == "off" and principal.username is None:
        return dataclasses.replace(principal, username=actor_of(x_actor))
    return principal


async def _app(session: AsyncSession, app_id: str, principal: Principal, need: str) -> tuple[App, str]:
    app = await session.get(App, app_id)
    if app is None:
        raise HTTPException(404, APP_GONE)
    role = await app_role(session, principal, app)
    if role is None:
        # 没有任何授权的人不该知道这个应用在不在
        raise HTTPException(404, APP_GONE)
    if not at_least(role, need):
        raise CodedHTTPException(403, f"需要这个应用的「{_ROLE_TEXT[need]}」权限", NO_ACCESS)
    return app, role


def _require_builder(principal: Principal) -> None:
    if not principal.at_least("builder"):
        raise CodedHTTPException(403, "搭建应用需要搭建者权限", NO_ACCESS)


async def _current_version(session: AsyncSession, app: App) -> AppVersion:
    if not app.published_version:
        raise CodedHTTPException(409, f"「{app.name}」还没有发布", NOT_PUBLISHED)
    return (await session.execute(select(AppVersion).where(
        AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one()


async def _needs_upgrade(session: AsyncSession, version: AppVersion | None) -> bool:
    from app.apps.pins import load_pins, needs_upgrade

    if version is None:
        return False
    return needs_upgrade(await load_pins(session, version.spec or {}))


async def _pending(session: AsyncSession, app_id: str, principal: Principal) -> int:
    """这个人在这个应用里的待办数。关登录时所有待办都算「我的」。"""
    from app.db.models_process import Task, TaskAssignment

    query = select(func.count(Task.id)).where(Task.app_id == app_id, Task.status == "pending")
    if principal.mode != "off":
        mine = select(TaskAssignment.task_id).where(TaskAssignment.status == "pending").where(
            (TaskAssignment.principal_kind == "anyone")
            | ((TaskAssignment.principal_kind == "user") & (TaskAssignment.principal == (principal.username or "")))
            | ((TaskAssignment.principal_kind == "group") & TaskAssignment.principal.in_(list(principal.groups) or [""])))
        query = query.where(Task.id.in_(mine))
    return int(await session.scalar(query) or 0)


async def _summary(session: AsyncSession, app: App, principal: Principal, role: str | None) -> dict[str, Any]:
    version = None
    if app.published_version:
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none()
    return {"id": app.id, "name": app.name, "description": app.description, "icon": app.icon, "status": app.status,
            "published_version": app.published_version, "level": version.level if version else None,
            "needs_upgrade": await _needs_upgrade(session, version),
            "pending_tasks": await _pending(session, app.id, principal), "my_role": role,
            "updated_at": app.updated_at.isoformat() if app.updated_at else None}


async def _detail(session: AsyncSession, app: App, principal: Principal, role: str | None) -> dict[str, Any]:
    from app.apps.spec import empty_spec

    return {**await _summary(session, app, principal, role), "draft": app.draft or empty_spec(app.name),
            "draft_bindings": {"slots": {}, **(app.draft_bindings or {})}, "created_by": app.created_by}


# --------------------------------------------------------------------------
# 增删改查
# --------------------------------------------------------------------------


@router.get("")
async def list_apps(include_archived: bool = False, session: AsyncSession = Depends(get_session),
                    x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    principal = _principal(x_actor)
    query = select(App).order_by(App.updated_at.desc())
    if not include_archived:
        query = query.where(App.status != "archived")
    items = []
    for app in (await session.execute(query)).scalars():
        role = await app_role(session, principal, app)
        if role is None:
            continue
        items.append(await _summary(session, app, principal, role))
    return {"items": items}


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    icon: str = Field(default="app", max_length=40)
    spec: dict[str, Any] | None = None


@router.post("", status_code=201)
async def create_app(payload: AppIn, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.apps.spec import empty_spec

    principal = _principal(x_actor)
    _require_builder(principal)
    draft = dict(payload.spec) if payload.spec else empty_spec(payload.name)
    draft["name"] = payload.name
    app = App(name=payload.name, description=payload.description, icon=payload.icon, draft=draft,
              draft_bindings={"slots": {}}, created_by=actor_of(x_actor), updated_by=actor_of(x_actor))
    session.add(app)
    await session.commit()
    await session.refresh(app)
    return await _detail(session, app, principal, "manage")


class DeriveIn(BaseModel):
    workflow_id: str
    version: int
    name: str | None = None


@router.post("/derive")
async def derive(payload: DeriveIn, session: AsyncSession = Depends(get_session),
                 x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """从工作流版本确定性派生一份应用草稿（不落库）。岗位留空，等人绑定。"""
    from app.apps.derive import DeriveRefused, derive_app

    _require_builder(_principal(x_actor))
    try:
        return {"spec": await derive_app(session, payload.workflow_id, payload.version, name=payload.name)}
    except DeriveRefused as e:
        raise HTTPException(e.status_code, e.message) from e


@router.get("/{app_id}")
async def get_app(app_id: str, session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "use")
    return await _detail(session, app, principal, role)


class AppPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=40)
    draft: dict[str, Any] | None = None
    draft_bindings: dict[str, Any] | None = None


def _bindings_problem(bindings: dict[str, Any]) -> str | None:
    """岗位绑定的形状：{"slots": {岗位: [{kind: user|group, name}]}, "hooks"?, "channels"?}。"""
    slots = bindings.get("slots", {})
    if not isinstance(slots, dict):
        return "岗位绑定需要按岗位列出人或组"
    for slot, items in slots.items():
        if not isinstance(items, list) or not all(
                isinstance(i, dict) and i.get("kind") in ("user", "group") and isinstance(i.get("name"), str)
                and i["name"].strip() for i in items):
            return f"岗位「{slot}」的绑定需要是人或组的列表"
    return None


@router.patch("/{app_id}")
async def update_app(app_id: str, payload: AppPatch, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """改草稿：spec、岗位绑定、名称等。草稿随便改，发布时才过检查（形状不对的检查里报 app.format）。"""
    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "manage")
    if payload.draft_bindings is not None and (problem := _bindings_problem(payload.draft_bindings)):
        raise HTTPException(422, problem)
    for key in ("name", "description", "icon"):
        if (value := getattr(payload, key)) is not None:
            setattr(app, key, value)
    if payload.draft is not None:
        app.draft = payload.draft
    if payload.draft_bindings is not None:
        app.draft_bindings = payload.draft_bindings
    app.updated_by = actor_of(x_actor)
    await session.commit()
    await session.refresh(app)
    return await _detail(session, app, principal, role)


@router.post("/{app_id}/archive")
async def archive_app(app_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """归档：不再出现在应用列表里，入口拒绝发起；版本、实例、生效记录都留着。"""
    from app.apps.audit import record

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "manage")
    app.status = "archived"
    app.updated_by = actor_of(x_actor)
    await record(session, action="app.archive", subject_kind="app", subject_id=app.id, actor=actor_of(x_actor),
                 data={"name": app.name})
    await session.commit()
    await session.refresh(app)
    return await _detail(session, app, principal, role)


# --------------------------------------------------------------------------
# 检查与发布
# --------------------------------------------------------------------------


async def _previous(session: AsyncSession, app: App) -> AppVersion | None:
    if not app.published_version:
        return None
    return (await session.execute(select(AppVersion).where(
        AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none()


@router.post("/{app_id}/lint")
async def lint(app_id: str, session: AsyncSession = Depends(get_session),
               x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.apps.lint import acl_issues, lint_app
    from app.apps.pins import load_pins
    from app.apps.tree import app_level

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "manage")
    issues = await lint_app(session, app.draft or {}, app.draft_bindings or {}, previous=await _previous(session, app))
    issues += await acl_issues(session, app.id)
    pins = await load_pins(session, app.draft or {})
    return {"issues": issues, "level": app_level([{"level": p.level} for p in pins.values()]) if pins else None}


class PublishIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    dispositions: dict[str, str] = Field(default_factory=dict)


@router.post("/{app_id}/publish")
async def publish(app_id: str, payload: PublishIn, session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """把草稿冻结成一个不可变的应用版本。钉住项相对上一版升了版而没选处置的不发布（ok=false，needs_disposition 列出）。

    返回 200 + ok/issues 而不是抛错，前端把问题列表渲染出来（同工作流发布）。"""
    from app.apps.audit import record
    from app.apps.lint import acl_issues, lint_app, needs_disposition, pins_with_state
    from app.apps.pins import load_pins
    from app.apps.resolve import clear_cache
    from app.apps.spec import parse_spec
    from app.apps.tree import app_level, spec_hash
    from app.db.base import utcnow

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "manage")
    previous = await _previous(session, app)
    draft = app.draft or {}
    issues = await lint_app(session, draft, app.draft_bindings or {}, previous=previous,
                            dispositions=payload.dispositions)
    issues += await acl_issues(session, app.id)
    upgraded = needs_disposition(previous, draft.get("pins") or []) if isinstance(draft.get("pins"), list) else []
    if any(i["level"] == "error" for i in issues):
        return {"ok": False, "issues": issues,
                "needs_disposition": [u for u in upgraded if payload.dispositions.get(u["alias"]) is None]}
    pins = await load_pins(session, draft)
    spec = parse_spec(draft).model_dump(mode="json", by_alias=True)
    spec["pins"] = pins_with_state(spec, pins)
    number = int(await session.scalar(select(func.max(AppVersion.version)).where(AppVersion.app_id == app.id)) or 0) + 1
    level = app_level(spec["pins"])
    row = AppVersion(app_id=app.id, version=number, spec=spec, spec_hash=spec_hash(spec), pins=spec["pins"],
                     bindings={"slots": {}, **(app.draft_bindings or {})}, level=level,
                     dispositions={u["alias"]: payload.dispositions[u["alias"]] for u in upgraded},
                     note=payload.note, published_by=actor_of(x_actor), published_at=utcnow())
    session.add(row)
    app.published_version = number
    app.updated_by = actor_of(x_actor)
    await record(session, action="app.publish", subject_kind="app", subject_id=app.id, actor=actor_of(x_actor),
                 data={"version": number, "spec_hash": row.spec_hash, "level": level, "pins": spec["pins"],
                       "dispositions": row.dispositions, "note": payload.note})
    try:
        await session.commit()
    except IntegrityError as e:
        # 两个人同时发布：(app_id, version) 唯一，后到的那个撞上，不另加锁
        await session.rollback()
        raise CodedHTTPException(409, "有人刚刚发布了这个应用，请刷新后再试", PUBLISH_CONFLICT) from e
    clear_cache()
    return {"ok": True, "version": number, "level": level, "spec_hash": row.spec_hash, "issues": issues}


def _version_row(row: AppVersion) -> dict[str, Any]:
    return {"version": row.version, "spec_hash": row.spec_hash, "level": row.level, "pins": row.pins,
            "dispositions": row.dispositions or {}, "note": row.note, "published_by": row.published_by,
            "published_at": row.published_at.isoformat() if row.published_at else None}


@router.get("/{app_id}/versions")
async def list_versions(app_id: str, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    app, _ = await _app(session, app_id, _principal(x_actor), "observe")
    rows = (await session.execute(select(AppVersion).where(AppVersion.app_id == app.id)
                                  .order_by(AppVersion.version.desc()))).scalars()
    return {"items": [_version_row(r) for r in rows]}


@router.get("/{app_id}/versions/{version}")
async def get_version(app_id: str, version: int, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    app, role = await _app(session, app_id, _principal(x_actor), "observe")
    row = (await session.execute(select(AppVersion).where(
        AppVersion.app_id == app.id, AppVersion.version == version))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"「{app.name}」没有 v{version}")
    # 岗位绑定是这一个部署里的人和组：管理者才看得到
    return {**_version_row(row), "spec": row.spec, "bindings": row.bindings if role == "manage" else {"slots": {}}}


# --------------------------------------------------------------------------
# 授权
# --------------------------------------------------------------------------


class GrantItem(BaseModel):
    principal_kind: str
    principal: str = ""
    role: str = "use"


class GrantsIn(BaseModel):
    items: list[GrantItem]


def _grant_out(g: AppGrant) -> dict[str, Any]:
    return {"principal_kind": g.principal_kind, "principal": g.principal, "role": g.role}


@router.get("/{app_id}/grants")
async def get_grants(app_id: str, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    return {"items": [_grant_out(g) for g in await grants_of(session, app.id)]}


@router.put("/{app_id}/grants")
async def put_grants(app_id: str, payload: GrantsIn, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """整份替换授权。授权不进应用版本（改了不用重新发布），写审计。"""
    from app.apps.audit import record
    from app.db.models_apps import GRANT_ROLES

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    wanted: list[tuple[str, str, str]] = []
    for item in payload.items:
        if item.principal_kind not in ("user", "group", "everyone") or item.role not in GRANT_ROLES:
            raise HTTPException(422, "授权只能给人、组或所有人，权限只能是使用、查看全部实例或管理")
        principal = "" if item.principal_kind == "everyone" else item.principal.strip()
        if item.principal_kind != "everyone" and not principal:
            raise HTTPException(422, "授权给人或组时需要写明是谁")
        key = (item.principal_kind, principal, item.role)
        if key not in wanted:
            wanted.append(key)
    before = [_grant_out(g) for g in await grants_of(session, app.id)]
    for g in await grants_of(session, app.id):
        await session.delete(g)
    await session.flush()
    for kind, who, role in wanted:
        session.add(AppGrant(app_id=app.id, principal_kind=kind, principal=who, role=role, granted_by=actor_of(x_actor)))
    after = [{"principal_kind": k, "principal": p, "role": r} for k, p, r in wanted]
    await record(session, action="app.grants", subject_kind="app", subject_id=app.id, actor=actor_of(x_actor),
                 data={"before": before, "after": after})
    await session.commit()
    return {"items": after}


# --------------------------------------------------------------------------
# 入站钩子的管理（收请求在 /api/hooks/{hook_id}，BE-2）
# --------------------------------------------------------------------------


def _hook_out(h: AppHook, secret: str | None = None) -> dict[str, Any]:
    return {"id": h.id, "name": h.name, "kind": h.kind, "entry_id": h.entry_id, "enabled": h.enabled,
            "created_at": h.created_at.isoformat() if h.created_at else None, **({"secret": secret} if secret else {})}


class HookIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    entry_id: str | None = None


class HookPatch(BaseModel):
    enabled: bool | None = None
    entry_id: str | None = None


@router.get("/{app_id}/hooks")
async def list_hooks(app_id: str, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    rows = (await session.execute(select(AppHook).where(AppHook.app_id == app.id).order_by(AppHook.created_at))).scalars()
    return {"items": [_hook_out(h) for h in rows]}


@router.post("/{app_id}/hooks", status_code=201)
async def create_hook(app_id: str, payload: HookIn, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """建一个入站钩子。签名密钥只在这里交回一次（库里存密文），之后要换就轮换。"""
    from app.apps.audit import record
    from app.core.crypto import encrypt

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    if payload.kind not in ("start", "signal"):
        raise HTTPException(422, "钩子只能是发起实例或回传信号两种")
    if payload.kind == "start" and not payload.entry_id:
        raise HTTPException(422, "发起实例的钩子需要指定入口")
    secret = secrets.token_urlsafe(32)
    hook = AppHook(app_id=app.id, name=payload.name, kind=payload.kind, entry_id=payload.entry_id,
                   secret=encrypt(secret) or "")
    session.add(hook)
    await session.flush()
    await record(session, action="app.hook_create", subject_kind="app_hook", subject_id=hook.id,
                 actor=actor_of(x_actor), data={"app_id": app.id, "name": hook.name, "kind": hook.kind,
                                                "entry_id": hook.entry_id})
    await session.commit()
    await session.refresh(hook)
    return _hook_out(hook, secret)


async def _hook(session: AsyncSession, app: App, hook_id: str) -> AppHook:
    hook = await session.get(AppHook, hook_id)
    if hook is None or hook.app_id != app.id:
        raise HTTPException(404, "钩子不存在")
    return hook


@router.patch("/{app_id}/hooks/{hook_id}")
async def update_hook(app_id: str, hook_id: str, payload: HookPatch, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.apps.audit import record

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    hook = await _hook(session, app, hook_id)
    if payload.enabled is not None:
        hook.enabled = payload.enabled
    if payload.entry_id is not None:
        hook.entry_id = payload.entry_id or None
    await record(session, action="app.hook_update", subject_kind="app_hook", subject_id=hook.id,
                 actor=actor_of(x_actor), data=payload.model_dump(exclude_none=True))
    await session.commit()
    await session.refresh(hook)
    return _hook_out(hook)


@router.post("/{app_id}/hooks/{hook_id}/rotate")
async def rotate_hook(app_id: str, hook_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.apps.audit import record
    from app.core.crypto import encrypt

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    hook = await _hook(session, app, hook_id)
    secret = secrets.token_urlsafe(32)
    hook.secret = encrypt(secret) or ""
    await record(session, action="app.hook_rotate", subject_kind="app_hook", subject_id=hook.id,
                 actor=actor_of(x_actor), data={"app_id": app.id})
    await session.commit()
    await session.refresh(hook)
    return _hook_out(hook, secret)


@router.delete("/{app_id}/hooks/{hook_id}", status_code=204)
async def delete_hook(app_id: str, hook_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> None:
    from app.apps.audit import record

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    hook = await _hook(session, app, hook_id)
    await record(session, action="app.hook_delete", subject_kind="app_hook", subject_id=hook.id,
                 actor=actor_of(x_actor), data={"app_id": app.id, "name": hook.name})
    await session.delete(hook)
    await session.commit()


# --------------------------------------------------------------------------
# 使用：页面、入口、实例、视图刷新
# --------------------------------------------------------------------------


def _params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise HTTPException(422, "页面参数需要是 JSON 对象") from e
    if not isinstance(value, dict):
        raise HTTPException(422, "页面参数需要是 JSON 对象")
    return value


async def _instance(session: AsyncSession, app: App, run_id: str, principal: Principal, role: str) -> Run:
    """实例要属于这个应用；只有 use 的人只能看自己发起的（关登录时都能看）。"""
    run = await session.get(Run, run_id)
    if run is None or run.app_id != app.id or run.origin_kind not in ("app_entry", "hook"):
        raise HTTPException(404, "实例不存在")
    if principal.mode != "off" and not at_least(role, "observe") and run.started_by != principal.username:
        raise CodedHTTPException(403, "只能查看自己发起的实例", NO_ACCESS)
    return run


def _view_refused(e: Any) -> HTTPException:
    return CodedHTTPException(e.status_code, e.message, e.code) if e.code else HTTPException(e.status_code, e.message)


@router.get("/{app_id}/pages/{page_id}")
async def get_page(app_id: str, page_id: str, instance: str | None = None, params: str | None = None,
                   draft: int = Query(default=0), session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """页面解析：服务端解析好的视图模型（agentlab.view/1）。draft=1 按草稿解析（编辑器预览，要管理权限），不发起运行。"""
    from app.apps.resolve import ViewRefused, resolve_page

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "manage" if draft else "use")
    version = None if draft else await _current_version(session, app)
    run = await _instance(session, app, instance, principal, role) if instance else None
    try:
        return await resolve_page(session, app, page_id=page_id, principal=principal, role=role, instance=run,
                                  params=_params(params), draft=bool(draft), version=version, actor=actor_of(x_actor))
    except ViewRefused as e:
        raise _view_refused(e) from e


class StartIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.post("/{app_id}/entries/{entry_id}", status_code=201)
async def start(app_id: str, entry_id: str, payload: StartIn, session: AsyncSession = Depends(get_session),
                x_actor: str | None = Header(default=None)) -> Any:
    """发起一个实例。表单没通过校验回 422，带 {errors: [{path, code, message}]}。"""
    from app.apps.entries import EntryRefused, FormRejected, start_entry
    from app.apps.resolve import instance_title
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "use")
    try:
        run = await start_entry(app.id, entry_id, payload.values, principal)
    except FormRejected as e:
        return JSONResponse(status_code=422, content={"detail": "表单没有通过校验", "errors": e.errors})
    except (EntryRefused, LaunchRefused) as e:
        raise _view_refused(e) from e
    return {"run_id": run.id, "title": instance_title(run)}


@router.get("/{app_id}/instances")
async def list_instances(app_id: str, who: str = "mine", entry: str | None = None, status: str | None = None,
                         limit: int = Query(default=50, ge=1, le=200), session: AsyncSession = Depends(get_session),
                         x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """实例列表。看全部要 observe；只有 use 的只看自己发起的。"""
    from app.apps.resolve import list_instances as instances

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "use")
    if who == "all" and not at_least(role, "observe"):
        raise CodedHTTPException(403, "查看全部实例需要这个应用的「查看全部实例」权限", NO_ACCESS)
    mine = principal.username if who != "all" else None
    return {"items": await instances(session, app.id, mine=mine, entry=entry, status=status, limit=limit)}


@router.get("/{app_id}/instances/{run_id}")
async def get_instance(app_id: str, run_id: str, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """一个实例：列表里的那一行，加上实例页的视图模型（应用里叫 instance 的页；没有就给一页默认的）。"""
    from app.apps.resolve import ViewRefused, instance_page, instance_row, pending_counts, resolve_page

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "use")
    run = await _instance(session, app, run_id, principal, role)
    version = (await session.execute(select(AppVersion).where(
        AppVersion.app_id == app.id, AppVersion.version == (run.app_version or app.published_version))))\
        .scalar_one_or_none() or await _current_version(session, app)
    spec = version.spec or {}
    page_id = instance_page(spec)
    if page_id is None:
        # 应用里没有实例页：给一页默认的（概况、进度、待办），不改应用版本本身
        spec = {**spec, "pages": [*(spec.get("pages") or []), {"id": "__instance", "title": "实例", "body": [
            {"id": "__header", "type": "instance_header"}, {"id": "__timeline", "type": "instance_timeline"},
            {"id": "__tasks", "type": "task_list", "props": {"scope": "instance"}}]}]}
        page_id = "__instance"
        version = AppVersion(app_id=version.app_id, version=version.version, spec=spec, spec_hash=version.spec_hash,
                             pins=version.pins, bindings=version.bindings, level=version.level,
                             dispositions=version.dispositions)
    try:
        view = await resolve_page(session, app, page_id=page_id, principal=principal, role=role, instance=run,
                                  version=version, actor=actor_of(x_actor))
    except ViewRefused as e:
        raise _view_refused(e) from e
    counts = await pending_counts(session, [run.id])
    return {**instance_row(run, counts.get(run.id, 0)), "view": view}


class RefreshIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/{app_id}/views/{view_id}/refresh")
async def refresh_view(app_id: str, view_id: str, payload: RefreshIn, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> Any:
    """按需视图：发起一次运行，或者命中缓存、接上同一个参数正在运行的那次。"""
    from app.apps.entries import FormRejected
    from app.apps.resolve import ViewRefused, run_view
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "use")
    version = await _current_version(session, app)
    try:
        run, cached = await run_view(session, app=app, version=version, view_id=view_id, params=payload.params,
                                     principal=principal, actor=actor_of(x_actor))
    except FormRejected as e:
        return JSONResponse(status_code=422, content={"detail": "参数没有通过校验", "errors": e.errors})
    except (ViewRefused, LaunchRefused) as e:
        raise _view_refused(e) from e
    return {"run_id": run.id, "cached": cached}
