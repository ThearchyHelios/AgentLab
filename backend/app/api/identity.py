"""用户、组、审计（BE-1，只给管理员：main.py 挂 require_role("admin")）；人和组的名录（只读，给搭建者及以上）。
设计文档 §2.7、§3.10。

- 用户名一经建立就不改（各「人」列开了登录就写它）；改叫法改显示名。单点登录来的用户角色每次登录按映射重算，这里改了
  下次登录会被映射盖掉；
- 至少保留一个能用的管理员：降级、停用最后一个时拒绝；
- 从单点登录同步来的组只读（成员由每次登录的 groups 声明决定）；
- 每个改动写审计（audit_events），和改动同一个事务。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.audit import record
from app.auth.passwords import hash_password, password_problem
from app.auth.sessions import revoke_user
from app.db.base import get_session
from app.db.models import AuditEvent, Group, GroupMember, User
from app.db.models_identity import ROLES

router = APIRouter(prefix="/api", tags=["identity"])
#: 服务账号的用户名开头（auth/keys.SERVICE_PREFIX）：这里建的人不能用它，免得和服务账号混在一起
_SERVICE_PREFIX = "svc-"
#: 人和组的名录（GET /api/directory）：只读、给搭建者及以上（main.py 挂 require_role("builder")），岗位绑定、应用授权的
#: 选人控件据此给候选、核对填的名字在不在。只有用户名、显示名、是否停用和组名，不含邮箱、角色、登录时间
directory_router = APIRouter(prefix="/api", tags=["identity"])
#: 使用者选人的候选（GET /api/directory/candidates，05-已知遗留 (b)）：任何登录的人都能调（main.py 挂 require_role("user")），
#: 按用途给候选池，只有用户名和显示名
candidates_router = APIRouter(prefix="/api", tags=["identity"])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def user_out(row: User) -> dict[str, Any]:
    return {"id": row.id, "username": row.username, "display_name": row.display_name or row.username,
            "email": row.email, "role": row.role, "disabled": row.disabled,
            "source": "oidc" if row.oidc_subject else "local", "last_login_at": _iso(row.last_login_at),
            "created_at": _iso(row.created_at),
            # 服务账号（next-round 04 A1）在「设置 → 访问密钥」里建和停用，这里只标出来
            "kind": row.kind or "person"}


@router.get("/users")
async def list_users(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars()
    return {"items": [user_out(r) for r in rows]}


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[^\s/]+$")
    display_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    role: str = "user"
    password: str | None = Field(default=None, max_length=200)


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise HTTPException(422, "角色只能是管理员、搭建者、使用者之一")


@router.post("/users", status_code=201)
async def create_user(payload: UserIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _check_role(payload.role)
    if payload.username.lower().startswith(_SERVICE_PREFIX):
        raise HTTPException(422, f"{_SERVICE_PREFIX} 开头的用户名留给服务账号：服务账号请在「设置 → 访问密钥」里建")
    if payload.password and (problem := password_problem(payload.password)):
        raise HTTPException(422, problem)
    if (await session.execute(select(User.id).where(User.username == payload.username))).first():
        raise HTTPException(409, f"用户名「{payload.username}」已存在")
    from app.api.deps import current_principal

    row = User(username=payload.username, display_name=payload.display_name or payload.username,
               email=payload.email or None, role=payload.role,
               password_hash=hash_password(payload.password) if payload.password else None,
               created_by=current_principal().username)
    session.add(row)
    await session.flush()
    await record(session, "user.create", "user", row.id, data={"username": row.username, "role": row.role})
    await session.commit()
    return user_out(row)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=200)


async def _other_admins(session: AsyncSession, user_id: str) -> int:
    return int((await session.execute(select(func.count()).select_from(User).where(
        User.role == "admin", User.disabled.is_(False), User.id != user_id))).scalar_one())


@router.patch("/users/{user_id}")
async def update_user(user_id: str, payload: UserPatch, session: AsyncSession = Depends(get_session)
                      ) -> dict[str, Any]:
    row = await session.get(User, user_id)
    if row is None:
        raise HTTPException(404, "用户不存在")
    if row.kind == "service" and ((payload.role is not None and payload.role != row.role) or payload.password):
        # 服务账号角色固定使用者、没有口令、不能登录（访问密钥一律按使用者算，next-round 04 A1、A3）
        raise HTTPException(409, "服务账号的角色固定是使用者，也不能设密码：它只持有访问密钥，在「设置 → 访问密钥」里管理")
    changes: dict[str, Any] = {}
    if payload.role is not None and payload.role != row.role:
        _check_role(payload.role)
        changes["role"] = [row.role, payload.role]
    if payload.disabled is not None and payload.disabled != row.disabled:
        changes["disabled"] = [row.disabled, payload.disabled]
    losing_admin = row.role == "admin" and not row.disabled and (
        ("role" in changes and payload.role != "admin") or payload.disabled is True)
    if losing_admin and not await _other_admins(session, row.id):
        raise HTTPException(409, "至少需要保留一个可用的管理员")
    if payload.password:
        if problem := password_problem(payload.password):
            raise HTTPException(422, problem)
        row.password_hash = hash_password(payload.password)
        changes["password"] = "reset"
        await revoke_user(session, row.id)
    if payload.display_name is not None:
        row.display_name = payload.display_name
    if payload.email is not None:
        row.email = payload.email or None
    if "role" in changes:
        row.role = payload.role or row.role
    if "disabled" in changes:
        row.disabled = bool(payload.disabled)
        if row.disabled:
            await revoke_user(session, row.id)
    if changes:
        await record(session, "user.update", "user", row.id, data={"username": row.username, **changes})
    await session.commit()
    return user_out(row)


# ---------------------------------------------------------------- 组


async def _group_out(session: AsyncSession, row: Group) -> dict[str, Any]:
    members = (await session.execute(select(User.username).join(GroupMember, GroupMember.user_id == User.id)
                                     .where(GroupMember.group_id == row.id).order_by(User.username))).scalars()
    return {"id": row.id, "name": row.name, "label": row.label or row.name, "source": row.source,
            "members": list(members)}


@router.get("/groups")
async def list_groups(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(Group).order_by(Group.name))).scalars())
    return {"items": [await _group_out(session, r) for r in rows]}


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[^\s/]+$")
    label: str | None = Field(default=None, max_length=100)


@router.post("/groups", status_code=201)
async def create_group(payload: GroupIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if (await session.execute(select(Group.id).where(Group.name == payload.name))).first():
        raise HTTPException(409, f"组「{payload.name}」已存在")
    row = Group(name=payload.name, label=payload.label or payload.name, source="local")
    session.add(row)
    await session.flush()
    await record(session, "group.create", "group", row.id, data={"name": row.name})
    await session.commit()
    return await _group_out(session, row)


async def _local_group(session: AsyncSession, group_id: str) -> Group:
    row = await session.get(Group, group_id)
    if row is None:
        raise HTTPException(404, "组不存在")
    if row.source != "local":
        raise HTTPException(409, "这个组从单点登录同步而来，成员由身份提供方决定，不能在这里修改")
    return row


class MembersIn(BaseModel):
    members: list[str] = Field(default_factory=list)


@router.put("/groups/{group_id}/members")
async def set_members(group_id: str, payload: MembersIn, session: AsyncSession = Depends(get_session)
                      ) -> dict[str, Any]:
    """整组成员换成给的这些用户名。人员变动只改组成员，不发新的应用版本（写审计）。"""
    from app.api.deps import current_principal

    row = await _local_group(session, group_id)
    wanted = list(dict.fromkeys(m.strip() for m in payload.members if m.strip()))
    users = {u.username: u for u in (await session.execute(select(User).where(User.username.in_(wanted)))).scalars()}
    if missing := [m for m in wanted if m not in users]:
        raise HTTPException(422, f"这些用户不存在：{'、'.join(missing)}")
    current = {m.user_id: m for m in (await session.execute(
        select(GroupMember).where(GroupMember.group_id == row.id))).scalars()}
    keep = {users[m].id for m in wanted}
    before = (await _group_out(session, row))["members"]
    for user_id, member in current.items():
        if user_id not in keep:
            await session.delete(member)
    for user_id in keep - set(current):
        session.add(GroupMember(group_id=row.id, user_id=user_id, added_by=current_principal().username))
    await record(session, "group.members", "group", row.id, data={"name": row.name, "before": before, "after": wanted})
    await session.commit()
    return await _group_out(session, row)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await _local_group(session, group_id)
    await record(session, "group.delete", "group", row.id, data={"name": row.name})
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------- 名录


@directory_router.get("/directory")
async def directory(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """选人用的名录。停用的人也列出来（标 disabled）：已经绑着他的岗位、授权要能认出「这个人停用了」，不是「没有这个人」。
    和应用检查、改授权时的核对同一处取（auth/directory.py）。"""
    from app.auth.directory import load_directory

    known = await load_directory(session)
    # 服务账号另标 service：岗位绑定、改派不能选它（不能被派待办），应用授权能选
    return {"users": [{"username": u.username, "display_name": u.display_name, "disabled": u.disabled,
                       **({"service": True} if u.service else {})}
                      for u in known.users.values()],
            "groups": [{"name": g.name, "label": g.label} for g in known.groups.values()]}


@candidates_router.get("/directory/candidates")
async def candidates(purpose: str, app_id: str | None = None, entry: str | None = None, task_id: str | None = None,
                     field: str | None = None, q: str | None = None, limit: int = 20,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """使用者选人时的候选：{users: [{username, display_name}], groups: [{name, label}], truncated, needs_query?}。

    不给是否停用（停用的直接不列）、角色、邮箱、组成员。purpose=form：表单里的选人控件，带 app_id 和 entry（入口表单）
    或 task_id（待办表单）、field（哪个控件），池按字段的 x-principal 算、缺省是能用这个应用的人，要求调用的人能用这个
    应用（否则 403）；应用授权给了所有人时不带前缀 q 不列（needs_query）。purpose=review：个人应用提交审核，启用的搭建者
    和管理员。q 按用户名或显示名的开头筛，limit 最多 50。开了登录时提交的值按同一个池核对（auth/directory.principal_errors）。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.apps.access import app_role, at_least
    from app.apps.tree import entry_of, resolve_form
    from app.auth.directory import PURPOSES, field_pool, principal_fields, review_pool
    from app.db.models_apps import App, AppVersion
    from app.db.models_process import Task

    if purpose not in PURPOSES:
        raise CodedHTTPException(422, f"候选的用途只能是 {' / '.join(PURPOSES)}", "candidates_purpose")
    if purpose == "review":
        return (await review_pool(session)).pick(q, limit) | {"groups": []}
    principal = current_principal()
    bindings: dict[str, Any] | None = None
    form: Any = None
    task = await session.get(Task, task_id) if task_id else None
    app = await session.get(App, task.app_id if task is not None else app_id) if (task or app_id) else None
    if app is None or (task_id and task is None):
        raise CodedHTTPException(404, "应用或待办不存在", "candidates_not_found")
    if not at_least(await app_role(session, principal, app), "use"):
        raise CodedHTTPException(403, "你没有这个应用的使用权限，看不到它的候选人", "candidates_forbidden")
    if task is not None:
        form = task.form
        if task.app_version is not None:
            bindings = await session.scalar(select(AppVersion.bindings).where(
                AppVersion.app_id == app.id, AppVersion.version == task.app_version))
    else:
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none() \
            if app.published_version else None
        spec = version.spec if version is not None else {}
        found = entry_of(spec or {}, entry or "") if entry else None
        form = resolve_form(spec or {}, found.get("form")) if found else None
        bindings = version.bindings if version is not None else None
    target = principal_fields(form).get(field or "")
    if target is None:
        raise CodedHTTPException(404, "表单里没有这个选人控件", "candidates_not_found")
    return (await field_pool(session, app, bindings, target)).pick(q, limit)


# ---------------------------------------------------------------- 审计


@router.get("/audit")
async def list_audit(subject_kind: str | None = None, subject_id: str | None = None, limit: int = 200,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    query = select(AuditEvent).order_by(AuditEvent.at.desc()).limit(max(1, min(limit, 1000)))
    if subject_kind:
        query = query.where(AuditEvent.subject_kind == subject_kind)
    if subject_id:
        query = query.where(AuditEvent.subject_id == subject_id)
    rows = (await session.execute(query)).scalars()
    return {"items": [{"id": r.id, "at": _iso(r.at), "actor": r.actor, "verified": r.verified, "action": r.action,
                       "subject_kind": r.subject_kind, "subject_id": r.subject_id, "data": r.data or {}}
                      for r in rows]}
