"""应用（BE-3）：增删改查、派生、检查、发布、版本、授权、页面解析、入口、实例、视图刷新、组件目录。设计文档 §3.10。

应用级权限按 §3.12（app/apps/access.py）：use 能用（入口、视图、自己的实例）；observe 另能看全部实例；manage 能搭建、
发布、授权、管钩子。关登录时全部放行、等同 manage。

改 spec、改岗位绑定只改草稿，发布才生成不可变的应用版本；改授权、管钩子不发新版本，写审计（A1）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import (
    APP_BINDINGS_INVALID, APP_DRAFT_INVALID, APP_GRANTS_INVALID, APP_HOOK_NOT_DECLARED, APP_HOOK_NOT_FOUND,
    APP_INSTANCE_NOT_FOUND, APP_NOT_FOUND, APP_PARAMS_INVALID, APP_VERSION_NOT_FOUND, FORM_INVALID, RUN_LAUNCH_REFUSED,
    CodedHTTPException,
)
from app.api.deps import Principal, actor_of, current_principal
from app.apps.access import ROLE_TEXT, app_role, at_least, grants_of
from app.db.base import get_session
from app.db.models import Run
from app.db.models_apps import App, AppGrant, AppHook, AppVersion

router = APIRouter(prefix="/api/apps", tags=["apps"])

APP_GONE = "应用不存在，可能已被删除"
NO_ACCESS = "app_forbidden"
NOT_PUBLISHED = "app_unpublished"
PUBLISH_CONFLICT = "app_publish_conflict"
#: 归档了的应用不再改草稿、不再发布（没有恢复的接口：归档后这个应用只留着给进行中的实例走完、给人查）
APP_ARCHIVED = "app_archived"


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
        raise CodedHTTPException(404, APP_GONE, APP_NOT_FOUND)
    role = await app_role(session, principal, app)
    if role is None:
        # 没有任何授权的人不该知道这个应用在不在：码和不存在的一样
        raise CodedHTTPException(404, APP_GONE, APP_NOT_FOUND)
    if not at_least(role, need):
        raise CodedHTTPException(403, f"需要这个应用的「{ROLE_TEXT[need]}」权限", NO_ACCESS)
    return app, role


def _refuse_archived(app: App, what: str) -> None:
    if app.status == "archived":
        raise CodedHTTPException(409, f"「{app.name}」已归档，不能再{what}", APP_ARCHIVED)


#: 个人应用（docs/design/normal-user-apps.md N7）没有的那几样：发布、授权
PERSONAL_ONLY_MINE = "personal.not_shareable"


def _refuse_personal(app: App, what: str) -> None:
    """个人应用没有应用版本（草稿就是在用的那一份）、不能分享：发布、授权一律不收，说清想给别人用怎么办。"""
    if getattr(app, "scope", "shared") == "personal":
        raise CodedHTTPException(409, f"「{app.name}」是个人应用，只给你自己用，不能{what}。想给别人用，请提交审核，由搭建者"
                                      "发布成正式应用", PERSONAL_ONLY_MINE)


def _require_builder(principal: Principal) -> None:
    if not principal.at_least("builder"):
        raise CodedHTTPException(403, "搭建应用需要搭建者权限", NO_ACCESS)


async def _current_version(session: AsyncSession, app: App) -> AppVersion:
    if getattr(app, "scope", "shared") == "personal":
        # 个人应用没有发布版本：不发起实例、没有按需视图，页面直接按存的样子显示，要新数点「刷新」
        raise CodedHTTPException(409, f"「{app.name}」是个人应用，不发起实例、没有按需视图：要看最新的数请点「刷新」",
                                 NOT_PUBLISHED)
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
            "created_at": app.created_at.isoformat() if app.created_at else None,
            "updated_at": app.updated_at.isoformat() if app.updated_at else None,
            # 个人应用（N7）：scope=personal、拥有者；出自哪个蓝图（共享应用经发布单生成的也有）
            "scope": app.scope or "shared", "owner": app.owner, "blueprint_id": app.blueprint_id}


def _personal_stub(app: App) -> dict[str, Any]:
    """管理员看到的别人的个人应用（列表、详情、归档的回包同一份）：名称、拥有者、状态、创建和更新时间，能归档；说明、
    页面、数、运行、蓝图一概不给（P4，N7「管理员看得到谁有几个个人应用，可以归档，但不看内容」）。"""
    return {"id": app.id, "name": app.name, "description": "", "icon": "app", "status": app.status,
            "published_version": None, "level": None, "needs_upgrade": False, "pending_tasks": 0, "my_role": None,
            "created_at": app.created_at.isoformat() if app.created_at else None,
            "updated_at": app.updated_at.isoformat() if app.updated_at else None,
            "scope": "personal", "owner": app.owner, "blueprint_id": None}


def _sees_stub(principal: Principal, app: App) -> bool:
    """这个人对这个应用只看得到元数据（_personal_stub）：开了登录的管理员看别人的个人应用。"""
    from app.apps.access import is_personal

    return is_personal(app) and principal.mode != "off" and principal.role == "admin"


def _published_outline(version: AppVersion | None) -> dict[str, Any] | None:
    """当前发布版里使用面要的那一点：有哪些页面、哪些入口（名字和标题）。页面内容走页面解析，表单走页面的 forms。"""
    if version is None:
        return None
    spec = version.spec or {}
    return {"version": version.version,
            "pages": [{"id": p.get("id"), "title": p.get("title") or ""} for p in spec.get("pages") or []
                      if isinstance(p, dict) and p.get("id")],
            "entries": [{"id": e.get("id"), "label": e.get("label") or ""} for e in spec.get("entries") or []
                        if isinstance(e, dict) and e.get("id")]}


def _draft_changed(app: App, previous: AppVersion | None) -> bool | None:
    """草稿和当前发布版是否一致（N6，页面上直接改时说「有未发布的改动」还是「和发布版相同」）。按发布时的规整方式比
    （publish_draft：parse_spec 规整成同一个形状），两边都过一遍，老版本发布时还没有的字段按缺省补齐、不算改动；钉住项的
    级别发布时按库里的那一版补、不出自草稿，不比。岗位绑定一并比。没发布过、发布版规整不了的为 None（不知道，界面不猜）；
    草稿规整不了的算改过（发布前得先改好）。"""
    from pydantic import ValidationError

    from app.apps.lint import pins_with_state
    from app.apps.spec import parse_spec
    from app.core.artifact_store import canonical_json

    def normal(spec: Any) -> str:
        out = parse_spec(spec or {}).model_dump(mode="json", by_alias=True)
        out["pins"] = pins_with_state(out, {})
        return canonical_json(out)

    if previous is None:
        return None
    try:
        published = normal(previous.spec)
    except ValidationError:
        return None
    try:
        draft = normal(app.draft)
    except ValidationError:
        return True
    bindings = {"slots": {}, **(app.draft_bindings or {})}
    return draft != published or canonical_json(bindings) != canonical_json(previous.bindings or {"slots": {}})


async def _detail(session: AsyncSession, app: App, principal: Principal, role: str | None, *,
                  with_backfill: bool = False) -> dict[str, Any]:
    """应用详情。草稿和草稿里的岗位绑定只给 manage（D-m）：草稿是还没发布、没过检查的东西，岗位绑定是这个部署里具体
    的人和组；use / observe 只拿概况和当前发布版的页面、入口目录（published）。

    with_backfill：另给管理者列出能按新口径补算的视图、各会发起几次（D-f）。要逐期查封存，只在打开详情时算；保存草稿、
    归档这些改动的回包里不算（编辑器存一次草稿就算一遍太浪费，补算只看发布版，草稿改了也不变）。"""
    from app.apps.spec import empty_spec

    previous = await _previous(session, app)
    out = {**await _summary(session, app, principal, role), "published": _published_outline(previous)}
    if not at_least(role, "manage"):
        return out
    out.update(draft=app.draft or empty_spec(app.name), draft_bindings={"slots": {}, **(app.draft_bindings or {})},
               created_by=app.created_by)
    if app.scope == "personal":
        # 个人应用给拥有者的那一块：今天还能刷新几次、蓝图改过没有、审核到哪了（blueprints/personal.personal_state）
        from app.blueprints.personal import personal_state

        out["personal"] = await personal_state(session, app, principal)
        return out
    if (changed := _draft_changed(app, previous)) is not None:
        out["draft_changed"] = changed
    if with_backfill:
        from app.apps.backfill import backfillable

        out["backfill"] = await backfillable(session, app)
    return out


# --------------------------------------------------------------------------
# 增删改查
# --------------------------------------------------------------------------


@router.get("")
async def list_apps(include_archived: bool = False, scope: str | None = None,
                    session: AsyncSession = Depends(get_session),
                    x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """应用列表。个人应用只列给拥有者；管理员另外看得到别人的个人应用在不在（_personal_stub，不含内容），能归档。
    scope 给了只列那一种（shared / personal）。can_create 是这个人能建哪种应用：新建应用的入口按它给（共享要搭建者及以上，
    个人看管理员的开关，blueprints/personal.allowed）。"""
    from sqlalchemy import or_

    from app.blueprints.personal import allowed

    principal = _principal(x_actor)
    query = select(App).order_by(App.updated_at.desc())
    if not include_archived:
        query = query.where(App.status != "archived")
    if scope == "personal":
        query = query.where(App.scope == "personal")
    elif scope == "shared":
        query = query.where(or_(App.scope == "shared", App.scope.is_(None)))
    from app.auth.keys import app_allowed

    items = []
    for app in (await session.execute(query)).scalars():
        if not app_allowed(principal, app):
            # 访问密钥（04 细案 A2）：个人应用不列，限定了应用的只列那几个
            continue
        role = await app_role(session, principal, app)
        if role is None:
            if _sees_stub(principal, app):
                items.append(_personal_stub(app))
            continue
        items.append(await _summary(session, app, principal, role))
    return {"items": items, "can_create": {"shared": principal.mode == "off" or principal.at_least("builder"),
                                           "personal": await allowed(session, principal)}}


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    icon: str = Field(default="app", max_length=40)
    spec: dict[str, Any] | None = None


@router.post("", status_code=201)
async def create_app(payload: AppIn, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    principal = _principal(x_actor)
    _require_builder(principal)
    app = await new_app(session, name=payload.name, description=payload.description, icon=payload.icon,
                        spec=payload.spec, actor=actor_of(x_actor))
    return await _detail(session, app, principal, "manage")


async def new_app(session: AsyncSession, *, name: str, description: str = "", icon: str = "app",
                  spec: dict[str, Any] | None = None, bindings: dict[str, Any] | None = None,
                  actor: str | None, blueprint_id: str | None = None) -> App:
    """建一个应用（只有草稿，提交）。建应用的接口和发布单共用；发布单另带上岗位绑定和出自哪个蓝图。"""
    from app.apps.spec import empty_spec

    draft = dict(spec) if spec else empty_spec(name)
    draft["name"] = name
    app = App(name=name, description=description, icon=icon, draft=draft,
              draft_bindings={"slots": {}, **(bindings or {})}, created_by=actor, updated_by=actor,
              blueprint_id=blueprint_id)
    session.add(app)
    await session.commit()
    await session.refresh(app)
    return app


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
        raise CodedHTTPException(e.status_code, e.message, e.code) from e


@router.get("/{app_id}")
async def get_app(app_id: str, session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """应用详情（_detail）。管理员打开别人的个人应用只拿到元数据（_personal_stub，同列表里那一条），页面、草稿、刷新、
    蓝图的接口照旧当它不存在。"""
    principal = _principal(x_actor)
    stub = await session.get(App, app_id)
    if stub is not None and _sees_stub(principal, stub) and await app_role(session, principal, stub) is None:
        return {**_personal_stub(stub), "published": None}
    app, role = await _app(session, app_id, principal, "use")
    return await _detail(session, app, principal, role, with_backfill=True)


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
    _refuse_archived(app, "修改")
    if payload.draft_bindings is not None and (problem := _bindings_problem(payload.draft_bindings)):
        raise CodedHTTPException(422, problem, APP_BINDINGS_INVALID)
    if app.scope == "personal" and payload.draft is not None:
        # 个人应用的草稿就是在用的那一份：只许改呈现，固定的蓝图、视图来源只由「存成我的应用」改
        from app.blueprints.personal import draft_problem

        if problem := draft_problem(app.draft or {}, payload.draft):
            raise CodedHTTPException(422, problem, APP_DRAFT_INVALID)
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
    """归档：不再出现在应用列表里，入口拒绝发起、入站钩子一律拒收（apps/entries.py、process/hooks.py），不再改草稿、
    不再发布；进行中的实例照常走完，版本、实例、生效记录都留着。没有恢复的接口。"""
    from app.apps.access import can_archive
    from app.apps.audit import record

    principal = _principal(x_actor)
    app = await session.get(App, app_id)
    role = await app_role(session, principal, app) if app is not None else None
    if app is None or not can_archive(principal, app, role):
        # 管理员能归档别人的个人应用（看不到内容，能收起来）；其余照旧要管理权限
        app, role = await _app(session, app_id, principal, "manage")
    app.status = "archived"
    app.updated_by = actor_of(x_actor)
    await record(session, action="app.archive", subject_kind="app", subject_id=app.id, actor=actor_of(x_actor),
                 data={"name": app.name, **({"owner": app.owner} if app.scope == "personal" else {})})
    await session.commit()
    await session.refresh(app)
    if role is None:
        return {**_personal_stub(app), "published": None}
    return await _detail(session, app, principal, role)


#: (应用 id, 人) → 取或建助手对话的锁。只增不减：编辑器打开过的应用 × 人
_ASSISTANT_LOCKS: dict[tuple[str, str | None], asyncio.Lock] = {}


def _assistant_lock(app_id: str, who: str | None) -> asyncio.Lock:
    return _ASSISTANT_LOCKS.setdefault((app_id, who), asyncio.Lock())


@router.get("/{app_id}/assistant-conversation")
async def assistant_conversation(app_id: str, session: AsyncSession = Depends(get_session),
                                 x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """应用编辑器里助手的那段对话：没有就建（D-e）。kind=app、归这个应用、建立人是现在这个人——对话按人隔离，同一个应用
    几个人轮流改，各自的历史不串。编辑器每轮落一条 turn（问题、说明、meta.looks），润色接口按 conversation_id 带历史。"""
    from app.api.conversations import _detail
    from app.db.models import Conversation, ConversationTurn

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    who = actor_of(x_actor)
    mine = Conversation.created_by == who if who else Conversation.created_by.is_(None)
    lookup = (select(Conversation).where(Conversation.kind == "app", Conversation.app_id == app.id,
                                         Conversation.archived.is_(False), mine)
              .order_by(Conversation.last_active_at.desc()).limit(1))
    # 取或建在进程里按 (应用, 人) 串行：编辑器开发模式下 effect 执行两次、同一个人开两个标签页，都会同时来取。库里有
    # (app_id, created_by) 的部分唯一索引（ux_conversations_app_owner）：别处（另一个进程）抢先建了的，这次插入在保存点里
    # 被拒，改用那一段——历史才不会被拆开。没署名（关登录又没填）的不受索引约束，靠这把进程内的锁
    async with _assistant_lock(app.id, who):
        row = await session.scalar(lookup)
        if row is None:
            fresh = Conversation(kind="app", app_id=app.id, created_by=who, title=f"「{app.name}」的助手")
            try:
                async with session.begin_nested():
                    session.add(fresh)
                    await session.flush()
                row = fresh
            except IntegrityError:
                row = await session.scalar(lookup)
                if row is None:
                    raise
            await session.commit()
            await session.refresh(row)
    turns = list((await session.execute(select(ConversationTurn).where(
        ConversationTurn.conversation_id == row.id).order_by(ConversationTurn.seq))).scalars())
    # 形状同 GET /api/conversations/{id}（ConversationDetail）：编辑器拿它渲染历史、接着落轮次
    return {**_detail(row, turns).model_dump(mode="json"), "turn_count": len(turns),
            "last_question": turns[-1].question if turns else ""}


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
    scope = app.scope or "shared"
    issues = await lint_app(session, app.draft or {}, app.draft_bindings or {}, previous=await _previous(session, app),
                            scope=scope)
    if scope != "personal":
        # 个人应用不能授权给别人，「还没有授权」不是问题
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
    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "manage")
    _refuse_archived(app, "发布")
    _refuse_personal(app, "发布")
    return await publish_draft(session, app, note=payload.note, dispositions=payload.dispositions,
                               actor=actor_of(x_actor))


async def publish_draft(session: AsyncSession, app: App, *, note: str, dispositions: dict[str, str],
                        actor: str | None) -> dict[str, Any]:
    """检查草稿，过了就冻结成新的应用版本并提交；没过交回 ok=false 和问题（不写库）。发布接口和发布单共用。
    两个人同时发布时后到的那个抛 409（app_publish_conflict）。"""
    from app.apps.audit import record
    from app.apps.lint import acl_issues, lint_app, needs_disposition, pins_with_state
    from app.apps.pins import load_pins
    from app.apps.resolve import clear_cache
    from app.apps.spec import parse_spec
    from app.apps.tree import app_level, spec_hash
    from app.db.base import utcnow

    previous = await _previous(session, app)
    draft = app.draft or {}
    issues = await lint_app(session, draft, app.draft_bindings or {}, previous=previous, dispositions=dispositions)
    issues += await acl_issues(session, app.id)
    upgraded = needs_disposition(previous, draft.get("pins") or []) if isinstance(draft.get("pins"), list) else []
    if any(i["level"] == "error" for i in issues):
        return {"ok": False, "issues": issues,
                "needs_disposition": [u for u in upgraded if dispositions.get(u["alias"]) is None]}
    pins = await load_pins(session, draft)
    spec = parse_spec(draft).model_dump(mode="json", by_alias=True)
    spec["pins"] = pins_with_state(spec, pins)
    number = int(await session.scalar(select(func.max(AppVersion.version)).where(AppVersion.app_id == app.id)) or 0) + 1
    level = app_level(spec["pins"])
    row = AppVersion(app_id=app.id, version=number, spec=spec, spec_hash=spec_hash(spec), pins=spec["pins"],
                     bindings={"slots": {}, **(app.draft_bindings or {})}, level=level,
                     dispositions={u["alias"]: dispositions[u["alias"]] for u in upgraded},
                     note=note, published_by=actor, published_at=utcnow())
    session.add(row)
    app.published_version = number
    app.updated_by = actor
    await record(session, action="app.publish", subject_kind="app", subject_id=app.id, actor=actor,
                 data={"version": number, "spec_hash": row.spec_hash, "level": level, "pins": spec["pins"],
                       "dispositions": row.dispositions, "note": note})
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
        raise CodedHTTPException(404, f"「{app.name}」没有 v{version}", APP_VERSION_NOT_FOUND)
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
    """整份替换授权。授权不进应用版本（改了不用重新发布），写审计。

    给的人和组按名录核对（auth/directory.py，同选人控件、同应用检查）：账号密码登录时名录里没有的、任何登录方式下停用了
    的拒收，说清是哪一档、哪个名字——界面上选人时已经拦着，这里拦旧客户端和直接调接口的。单点登录的人第一次登录后才进
    名录，名录里还没有的照收（检查里提醒）；关登录不核对。"""
    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    _refuse_personal(app, "授权给别人")
    wanted, problem = await grants_problem(session, [(i.principal_kind, i.principal, i.role) for i in payload.items])
    if problem:
        raise CodedHTTPException(422, problem, APP_GRANTS_INVALID)
    return {"items": await replace_grants(session, app, wanted, actor=actor_of(x_actor))}


async def grants_problem(session: AsyncSession, items: list[tuple[str, str, str]]
                         ) -> tuple[list[tuple[str, str, str]], str | None]:
    """要给的授权 [(principal_kind, principal, role)] → (去重规范化之后的, 收不下的说明)，只读。形状不对、名录核对不过
    （同上面的规则）时说明不为空。授权接口和发布单（预检只检查、最后一步照它授权）共用。"""
    from app.api.deps import auth_mode
    from app.auth.directory import load_directory, mismatch
    from app.db.models_apps import GRANT_ROLES

    wanted: list[tuple[str, str, str]] = []
    for kind, name, role in items:
        if kind not in ("user", "group", "everyone") or role not in GRANT_ROLES:
            return wanted, "授权只能给人、组或所有人，权限只能是使用、查看全部实例或管理"
        principal = "" if kind == "everyone" else (name or "").strip()
        if kind != "everyone" and not principal:
            return wanted, "授权给人或组时需要写明是谁"
        key = (kind, principal, role)
        if key not in wanted:
            wanted.append(key)
    if (mode := auth_mode()) != "off":
        known = await load_directory(session)
        # 服务账号能拿授权（外部系统按授权读应用，docs/design/next-round/04-对外集成与触达.md A1），这里不算对不上
        refused = [f"「{ROLE_TEXT[role]}」里的{m.text}" for kind, who, role in wanted
                   if (m := mismatch(known, mode, kind, who, allow_service=True)) is not None and m.blocking]
        if refused:
            return wanted, f"{'；'.join(refused)}：授权只能给名录里在用的人和组，请移除或从候选中重新选择"
    return wanted, None


async def replace_grants(session: AsyncSession, app: App, wanted: list[tuple[str, str, str]], *,
                         actor: str | None) -> list[dict[str, str]]:
    """整份替换授权（调用方先过了 grants_problem），写审计并提交。"""
    from app.apps.audit import record

    before = [_grant_out(g) for g in await grants_of(session, app.id)]
    for g in await grants_of(session, app.id):
        await session.delete(g)
    await session.flush()
    for kind, who, role in wanted:
        session.add(AppGrant(app_id=app.id, principal_kind=kind, principal=who, role=role, granted_by=actor))
    after = [{"principal_kind": k, "principal": p, "role": r} for k, p, r in wanted]
    await record(session, action="app.grants", subject_kind="app", subject_id=app.id, actor=actor,
                 data={"before": before, "after": after})
    await session.commit()
    return after


# --------------------------------------------------------------------------
# 入站钩子的管理（收请求在 /api/hooks/{hook_id}，process/hooks.py）
#
# 按名字认（D-g）：spec.hooks 声明 {name, kind, entry}，app_hooks 按 (应用, 名字) 一行，只存密钥和启用与否。钩子做
# 什么以当前发布版的声明为准：改草稿不改正在用的钩子，发布版里没声明的拒收。密钥只在生成、轮换时交回一次
# --------------------------------------------------------------------------


class HookPatch(BaseModel):
    enabled: bool


async def _hook_state(session: AsyncSession, app: App) -> tuple[dict[str, Any], dict[str, Any], dict[str, AppHook]]:
    """(草稿里的声明, 当前发布版里的声明, 已有的行)，都按名字。"""
    from app.process.hooks import declarations

    version = await _previous(session, app)
    rows: dict[str, AppHook] = {}
    for row in (await session.execute(select(AppHook).where(AppHook.app_id == app.id)
                                      .order_by(AppHook.created_at))).scalars():
        rows.setdefault(row.name, row)
    return declarations(app.draft or {}), declarations(version.spec if version is not None else {}), rows


def _hook_item(name: str, draft: dict[str, Any], published: dict[str, Any], rows: dict[str, AppHook],
               secret: str | None = None) -> dict[str, Any]:
    from app.process.hooks import hook_out

    return hook_out(rows.get(name), name=name, declared=draft.get(name) or published.get(name),
                    in_draft=name in draft, in_published=name in published, secret=secret)


@router.get("/{app_id}/hooks")
async def list_hooks(app_id: str, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """草稿里声明的 ∪ 已经有行的：每个名字一行，标出密钥生成过没有、当前发布版里有没有（没有的收不到请求）。"""
    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    draft, published, rows = await _hook_state(session, app)
    names = list(dict.fromkeys([*draft, *published, *rows]))
    return {"items": [_hook_item(n, draft, published, rows) for n in names]}


@router.post("/{app_id}/hooks/{name}/rotate")
async def rotate_hook(app_id: str, name: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """生成或轮换这个名字的签名密钥，密钥只在这里交回一次（库里存密文）。名字要在草稿或当前发布版里声明过。"""
    from app.apps.audit import record
    from app.process import hooks

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    draft, published, rows = await _hook_state(session, app)
    declared = draft.get(name) or published.get(name)
    if declared is None:
        raise CodedHTTPException(404, f"草稿和当前发布版里都没有声明钩子「{name}」，请先在「钩子」一节里声明它",
                                 APP_HOOK_NOT_DECLARED)
    # 从读行锁到提交：两位管理者同时点「生成密钥」，后到的那次读得到前一次插入的行，只轮换它（见 process/hooks.py）
    async with hooks.hook_lock(app.id, name):
        row, secret, created = await hooks.rotate_hook(session, app.id, name, kind=declared["kind"],
                                                        entry=declared.get("entry"))
        await record(session, action="app.hook_create" if created else "app.hook_rotate", subject_kind="app_hook",
                     subject_id=row.id, actor=actor_of(x_actor),
                     data={"app_id": app.id, "name": name, "kind": declared["kind"], "entry": declared.get("entry")})
        await session.commit()
    await session.refresh(row)
    return _hook_item(name, draft, published, {**rows, name: row}, secret)


async def _hook_named(session: AsyncSession, app: App, name: str) -> AppHook:
    from app.process.hooks import hook_row

    row = await hook_row(session, app.id, name)
    if row is None:
        raise CodedHTTPException(404, f"钩子「{name}」还没有生成密钥", APP_HOOK_NOT_FOUND)
    return row


@router.patch("/{app_id}/hooks/{name}")
async def update_hook(app_id: str, name: str, payload: HookPatch, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """启用、停用。停用的钩子收到请求一律拒收，密钥留着，重新启用就能接着用。"""
    from app.apps.audit import record
    from app.process.hooks import set_enabled

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    row = await _hook_named(session, app, name)
    await set_enabled(session, app.id, name, payload.enabled)
    await record(session, action="app.hook_update", subject_kind="app_hook", subject_id=row.id,
                 actor=actor_of(x_actor), data={"app_id": app.id, "name": name, "enabled": payload.enabled})
    await session.commit()
    draft, published, rows = await _hook_state(session, app)
    return _hook_item(name, draft, published, rows)


@router.delete("/{app_id}/hooks/{name}", status_code=204)
async def delete_hook(app_id: str, name: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> None:
    """删掉这个名字的密钥和去重记录。声明留在草稿里（要去掉在编辑器里删）；再生成就是一把新密钥、一个新地址。"""
    from app.apps.audit import record
    from app.process import hooks

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    row = await _hook_named(session, app, name)
    await record(session, action="app.hook_delete", subject_kind="app_hook", subject_id=row.id,
                 actor=actor_of(x_actor), data={"app_id": app.id, "name": name})
    await hooks.delete_hook(session, app.id, name)
    await session.commit()


@router.get("/{app_id}/hooks/{name}/receipts")
async def hook_receipts(app_id: str, name: str, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """这个钩子最近收到的请求（04 细案 A6，要管理权限）：最近 100 条的幂等键、状态、时间、发起的实例或解决的待办、被拒的
    原因，另给近 1 小时验签失败几次（不落库，只在进程里按钩子数）。还没生成密钥的是空的。"""
    from app.process import hooks

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    return await hooks.recent_receipts(session, app.id, name)


# --------------------------------------------------------------------------
# 使用：页面、入口、实例、视图刷新
# --------------------------------------------------------------------------


def _params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise CodedHTTPException(422, "页面参数需要是 JSON 对象", APP_PARAMS_INVALID) from e
    if not isinstance(value, dict):
        raise CodedHTTPException(422, "页面参数需要是 JSON 对象", APP_PARAMS_INVALID)
    return value


async def _instance(session: AsyncSession, app: App, run_id: str, principal: Principal, role: str) -> Run:
    """实例要属于这个应用；只有 use 的人只能看自己发起的（关登录时都能看）。"""
    run = await session.get(Run, run_id)
    if run is None or run.app_id != app.id or run.origin_kind not in ("app_entry", "hook"):
        raise CodedHTTPException(404, "实例不存在", APP_INSTANCE_NOT_FOUND)
    if principal.mode != "off" and not at_least(role, "observe") and run.started_by != principal.username:
        raise CodedHTTPException(403, "只能查看自己发起的实例", NO_ACCESS)
    return run


def _view_refused(e: Any) -> CodedHTTPException:
    """视图、入口、补算、发起运行的拒绝 → 带 code 的 HTTP 错误。只有引擎不发起运行（LaunchRefused）有不带码的几种（工作流
    没发布、版本不存在、图没通过校验……），补上同一类的 run_launch_refused，detail 照旧说清是哪一种。"""
    return CodedHTTPException(e.status_code, e.message, e.code or RUN_LAUNCH_REFUSED)


@router.get("/{app_id}/pages/{page_id}")
async def get_page(app_id: str, page_id: str, instance: str | None = None, params: str | None = None,
                   draft: int = Query(default=0), session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """页面解析：服务端解析好的视图模型（agentlab.view/1）。draft=1 按草稿解析（编辑器预览，要管理权限），不发起运行。"""
    from app.apps.resolve import ViewRefused, resolve_page

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "manage" if draft else "use")
    # 个人应用没有应用版本：草稿就是在用的那一份（resolve_page 按 scope 认）
    version = None if draft or app.scope == "personal" else await _current_version(session, app)
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
                x_actor: str | None = Header(default=None),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Any:
    """发起一个实例。表单没通过校验回 422，带 {errors: [{path, code, message}]}。

    用访问密钥发起（docs/design/next-round/04-对外集成与触达.md A4）：必须带 Idempotency-Key，同一个键重发交回同一个实例
    （duplicate: true），处理到一半服务停了的同一个键能认领回已发起的实例；入口表单里有文件控件的 422
    （key_files_unsupported：文件要先在应用里上传，密钥不开上传接口）。"""
    from app.apps.entries import EntryRefused, FormRejected, start_entry
    from app.apps.resolve import instance_title
    from app.auth import keys
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "use")
    idem = keys.idempotency_of(idempotency_key) if principal.via == "key" else None

    async def launch(reclaimed: bool = False) -> dict[str, Any]:
        if reclaimed and idem and (run_id := await keys.started_before(principal, idem)):
            # 上一次处理到一半服务停了：实例其实已经发起，交回它，不再发起一个
            return await replay({"run_id": run_id})
        run = await start_entry(app.id, entry_id, payload.values, principal, idempotency_key=idem)
        return {"run_id": run.id, "title": instance_title(run)}

    async def replay(previous: dict[str, Any]) -> dict[str, Any]:
        run = await session.get(Run, previous.get("run_id")) if previous.get("run_id") else None
        return {"run_id": previous.get("run_id"), "title": instance_title(run) if run is not None else "",
                **({"duplicate": True} if previous.get("duplicate") else {})}

    try:
        if principal.via != "key":
            return await launch()
        await _refuse_key_files(session, app, entry_id)
        return await keys.once(principal, idem, route=f"POST /api/apps/{app.id}/entries/{entry_id}",
                               request=payload.values, run=launch, replay=replay)
    except FormRejected as e:
        return JSONResponse(status_code=422, content={"detail": "表单没有通过校验", "code": FORM_INVALID, "errors": e.errors})
    except (EntryRefused, LaunchRefused) as e:
        raise _view_refused(e) from e


async def _refuse_key_files(session: AsyncSession, app: App, entry_id: str) -> None:
    """用访问密钥发起时，入口表单（当前发布版）里有文件控件的拒（422 key_files_unsupported）。发布版、入口不存在的放过去，
    由发起照旧报。"""
    from app.apps.tree import entry_of, resolve_form
    from app.auth import keys
    from app.files.store import file_fields

    version = await _previous(session, app)
    spec = (version.spec or {}) if version is not None else {}
    entry = entry_of(spec, entry_id)
    if entry is not None and file_fields(resolve_form(spec, entry.get("form"))):
        raise CodedHTTPException(422, "这个入口的表单里要传文件，不能用访问密钥发起：请在应用里发起",
                                 keys.KEY_FILES_UNSUPPORTED)


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
    from app.apps.resolve import ViewRefused, instance_page, instance_rows, resolve_page

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
    return {**(await instance_rows(session, [run]))[0], "view": view}


class RefreshIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/{app_id}/views/{view_id}/refresh")
async def refresh_view(app_id: str, view_id: str, payload: RefreshIn, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Any:
    """按需视图：发起一次运行，或者命中缓存、接上同一个参数正在运行的那次。

    用访问密钥刷新（04 细案 A4，集成-I3）：同发起实例，必须带 Idempotency-Key，同一个键重发交回同一次运行。刷新本来就按
    参数去重（缓存、同一组参数只跑一份），幂等键再管住「上一次失败、缓存被清掉之后重发又发起一次」这种情形。"""
    from app.apps.entries import FormRejected
    from app.apps.resolve import ViewRefused, run_view
    from app.auth import keys
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "use")
    version = await _current_version(session, app)

    async def refresh(_reclaimed: bool = False) -> dict[str, Any]:
        # 重新认领时照样走 run_view：上一次已经发起的那次在缓存里，命中它或者接上它，不会再发起一次
        run, cached = await run_view(session, app=app, version=version, view_id=view_id, params=payload.params,
                                     principal=principal, actor=actor_of(x_actor))
        return {"run_id": run.id, "cached": cached}

    async def replay(previous: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": previous.get("run_id"), "cached": False, "duplicate": True}

    try:
        if principal.via != "key":
            return await refresh()
        return await keys.once(principal, idempotency_key, route=f"POST /api/apps/{app.id}/views/{view_id}/refresh",
                               request=payload.params, run=refresh, replay=replay)
    except FormRejected as e:
        return JSONResponse(status_code=422, content={"detail": "参数没有通过校验", "code": FORM_INVALID, "errors": e.errors})
    except (ViewRefused, LaunchRefused) as e:
        raise _view_refused(e) from e


@router.get("/{app_id}/views/{view_id}/data")
async def view_data(app_id: str, view_id: str, params: str | None = None, session: AsyncSession = Depends(get_session),
                    x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """视图数据（agentlab.viewdata/1，04 细案 A5）：按当前发布版解析这个视图取的是哪一次封存的正式运行，交出印章、清单哈希、
    口径卡指标的原值和渲染文字（遮罩的 value 为 null）、证据指针。按需视图读 ?params= 这组参数上次算的结果，不发起运行，
    没算过的 state=empty。界面和访问密钥都能用；个人应用没有发布版（409）。"""
    from app.apps.resolve import ViewRefused, view_data as data_of

    principal = _principal(x_actor)
    app, role = await _app(session, app_id, principal, "use")
    version = await _current_version(session, app)
    try:
        return await data_of(session, app, version=version, view_id=view_id, params=_params(params),
                             principal=principal, role=role)
    except ViewRefused as e:
        raise _view_refused(e) from e


# --------------------------------------------------------------------------
# 按新口径补算（D-f）
# --------------------------------------------------------------------------


@router.get("/{app_id}/views/{view_id}/backfill")
async def backfill_plan(app_id: str, view_id: str, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """补算会发起几次、跳过几次（不发起运行）：按钮上写明会发起几次运行用。"""
    from app.apps.backfill import BackfillRefused, plan

    app, _ = await _app(session, app_id, _principal(x_actor), "manage")
    version = await _current_version(session, app)
    try:
        return (await plan(session, app, version, view_id)).out()
    except BackfillRefused as e:
        raise _view_refused(e) from e


@router.post("/{app_id}/views/{view_id}/backfill")
async def backfill(app_id: str, view_id: str, session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """按新口径补算：这个视图的钉住项最近一次升版选了 recompute 时，取旧版本在同一来源上最近几期的输入，用新版本逐个
    发起正式运行（来源 app_backfill）；新版本已经算过同一个输入的跳过。返回发起了几次、跳过几次。"""
    from app.apps.backfill import BackfillRefused, run_backfill
    from app.apps.resolve import clear_cache
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "manage")
    version = await _current_version(session, app)
    try:
        out = await run_backfill(session, app, version, view_id, principal=principal, actor=actor_of(x_actor))
    except BackfillRefused as e:
        raise _view_refused(e) from e
    except LaunchRefused as e:
        raise _view_refused(e) from e
    clear_cache()
    return out
