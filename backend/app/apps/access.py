"""应用级权限（§3.12）：use 能用（入口、视图、自己的实例）；observe 另能看全部实例；manage 能搭建、发布、授权、管钩子。

- 关登录：全部放行，等同 manage（界面上挂横幅说明，签批人是自报的署名）；
- admin：所有应用 manage；
- builder：所有应用至少 observe（能用、能看全部实例）；自己建的、或者有 manage 授权的才 manage；
- user：按授权（给本人、给所在的组、给所有人）取最高的那一档，最高到 observe：搭建、发布、绑定岗位、授权、钩子、按草稿
  预览、改派催办都不给 user 角色（§3.12），授给它的 manage 按 observe 算。没有授权就看不到这个应用。
- 个人应用（scope=personal，docs/design/normal-user-apps.md N7）：只有拥有者能用，按 manage 算（改页面、刷新、提交审核）；
  别人一律没有权限、看不到，管理员也一样——管理员只在应用列表里看得到它存在、能归档（can_archive），不看内容（P4）。
  个人应用没有授权（不能分享），授权表里不会有它。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.db.models_apps import AppGrant

RANK = {"use": 1, "observe": 2, "manage": 3}
#: 三档授权的叫法（同前端 GRANT_ROLE_LABEL）：权限不够的报错、授权核对的说明里写这个
ROLE_TEXT = {"use": "使用", "observe": "查看全部实例", "manage": "管理"}


def at_least(role: str | None, need: str) -> bool:
    return RANK.get(role or "", 0) >= RANK[need]


def granted_role(principal: Principal, grants: list[Any]) -> str | None:
    """授权里这个人能拿到的最高一档。"""
    best: str | None = None
    groups = set(principal.groups)
    for g in grants:
        hit = (g.principal_kind == "everyone"
               or (g.principal_kind == "user" and principal.username and g.principal == principal.username)
               or (g.principal_kind == "group" and g.principal in groups))
        if hit and RANK.get(g.role, 0) > RANK.get(best or "", 0):
            best = g.role
    return best


def personal_role(principal: Principal, app: Any) -> str | None:
    """个人应用上这个人的权限：拥有者 manage，别人 None。关登录时全部放行（没有可信的身份，同共享应用）。"""
    if principal.mode == "off":
        return "manage"
    return "manage" if app.owner and principal.username == app.owner else None


def is_personal(app: Any) -> bool:
    return getattr(app, "scope", "shared") == "personal"


def can_archive(principal: Principal, app: Any, role: str | None) -> bool:
    """能不能归档：管理权限；个人应用另外管理员也能（看不到内容，但能把它收起来）。"""
    return at_least(role, "manage") or (is_personal(app) and principal.role == "admin")


def role_of(principal: Principal, app: Any, grants: list[Any]) -> str | None:
    if is_personal(app):
        return personal_role(principal, app)
    if principal.mode == "off" or principal.role == "admin":
        return "manage"
    granted = granted_role(principal, grants)
    if principal.role == "builder":
        if app.created_by and principal.username and app.created_by == principal.username:
            return "manage"
        return granted if at_least(granted, "manage") else "observe"
    return "observe" if at_least(granted, "manage") else granted


async def grants_of(session: AsyncSession, app_id: str) -> list[AppGrant]:
    return list((await session.execute(select(AppGrant).where(AppGrant.app_id == app_id)
                                       .order_by(AppGrant.created_at))).scalars())


async def app_role(session: AsyncSession, principal: Principal, app: Any) -> str | None:
    if is_personal(app):
        return personal_role(principal, app)
    if principal.mode == "off" or principal.role == "admin":
        return "manage"
    return role_of(principal, app, await grants_of(session, app.id))
