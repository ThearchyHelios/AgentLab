"""用户、组、审计（BE-1，只给管理员：main.py 挂 require_role("admin")）。设计文档 §2.7、§3.10。

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


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def user_out(row: User) -> dict[str, Any]:
    return {"id": row.id, "username": row.username, "display_name": row.display_name or row.username,
            "email": row.email, "role": row.role, "disabled": row.disabled,
            "source": "oidc" if row.oidc_subject else "local", "last_login_at": _iso(row.last_login_at),
            "created_at": _iso(row.created_at)}


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
