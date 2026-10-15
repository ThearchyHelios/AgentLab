"""记录表的行级可见（docs/design/normal-user-apps.md N8、§3.7、§3.12）。

谁看得到哪些行，只有三种：all（全部）、mine（只看自己提交的，_submitted_by = 看的人）、none（一行都不给）。
- 正式运行以服务身份跑，看全部：看板读的是正式运行的封存结果，汇总是全量的（作者的选择，发布摘要里写明）；
- 别的运行（问数据、试运行、个人应用的刷新）按发起人和这张表所属应用的授权：use 只看自己的，observe、manage 看全部；
- 记录列表（records_list、GET /api/apps/{id}/records）按看的人，同一套规则；
- 关登录时所有人等同管理员，看全部；管理员看全部。
过滤在查询连接的临时视图上做（records/store.view_prelude），模型和 SQL 都绕不过去。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SCOPES = ("all", "mine", "none")


def scope_of_role(principal: Any, role: str | None) -> str:
    """看的人在应用上拿到的那一档 → 看哪些行。"""
    from app.apps.access import at_least

    if principal.mode == "off" or principal.role == "admin":
        return "all"
    if at_least(role, "observe"):
        return "all"
    return "mine" if role == "use" else "none"


async def scope_of(session: AsyncSession, principal: Any, app_id: str | None) -> str:
    """看的人对这张表（所属应用 app_id）看哪些行。还没归到应用的（试运行时按蓝图建的）：搭建者看全部，使用者看自己的。"""
    from app.apps.access import app_role
    from app.db.models_apps import App

    if principal.mode == "off" or principal.role == "admin":
        return "all"
    if not app_id:
        return "all" if principal.at_least("builder") else "mine"
    app = await session.get(App, app_id)
    if app is None:
        return "all" if principal.at_least("builder") else "none"
    return scope_of_role(principal, await app_role(session, principal, app))


async def scope_of_run(session: AsyncSession, app_id: str | None, *, run_class: str | None,
                       started_by: str | None) -> dict[str, Any]:
    """一次运行看哪些行，记进 data_versions：{"rows": all|mine|none, "viewer"?: 发起人}。发起时定下，续跑、恢复沿用。"""
    from app.api.deps import auth_mode

    if run_class == "formal":
        return {"rows": "all"}
    mode = auth_mode()
    if mode == "off":
        return {"rows": "all"}
    principal = await _principal(session, started_by, mode)
    if principal is None:
        return {"rows": "none"}
    scope = await scope_of(session, principal, app_id)
    return {"rows": scope, **({"viewer": principal.username} if scope == "mine" else {})}


async def _principal(session: AsyncSession, username: str | None, mode: str) -> Any | None:
    from app.auth.sessions import principal_of
    from app.db.models import User

    if not username:
        return None
    user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or user.disabled:
        return None
    return await principal_of(session, user, mode)
