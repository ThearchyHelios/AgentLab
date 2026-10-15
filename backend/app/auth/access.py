"""运行、工件的读权限（§3.12「运行、证据、工件（只读）」那一行）。

关登录、builder 及以上：都能读。开登录后 user 角色只能读：
- 自己发起的运行；
- 自己参与的实例：派给过自己（或自己所在的组）的待办，或者自己回复过的待办所在的运行。派给「任何人」的不算：那是没有
  应用绑定的运行（画布、定时任务），user 角色不该因此读到；
- 自己的个人应用（docs/design/normal-user-apps.md N7）的刷新：只有拥有者，别人的个人应用即使有人自报同名也不算；
- 有授权（use / observe / manage，含 everyone 和自己所在的组）的应用里：
  - 视图运行（来源是应用视图、按新口径补算）；
  - observe、manage 授权另能读全部实例；
  - 应用发布版本里作为视图固定的工作流版本的正式运行（视图读的是定时任务那次运行，它不带应用 id）。

user 角色读到的查询快照一律按派生遮罩处理、tool_snapshot 的原文不给（api/artifacts.py），这一层只回答「能不能读」。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

#: 视图运行的来源（engine/origin.py）
_VIEW_ORIGINS = ("app_view", "app_backfill")


def _unrestricted(principal: Any) -> bool:
    return principal.mode == "off" or principal.at_least("builder")


async def app_roles(session: AsyncSession, principal: Any) -> dict[str, set[str]]:
    """这个人在各应用上的授权：{app_id: {use, observe, manage}}。"""
    from app.db.models import AppGrant

    who = [AppGrant.principal_kind == "everyone",
           and_(AppGrant.principal_kind == "user", AppGrant.principal == principal.username)]
    if principal.groups:
        who.append(and_(AppGrant.principal_kind == "group", AppGrant.principal.in_(list(principal.groups))))
    out: dict[str, set[str]] = {}
    for app_id, role in (await session.execute(select(AppGrant.app_id, AppGrant.role).where(or_(*who)))).all():
        out.setdefault(app_id, set()).add(role)
    return out


async def _participated(session: AsyncSession, principal: Any, run_id: str) -> bool:
    from app.db.models import Task, TaskAssignment

    mine = [and_(TaskAssignment.principal_kind == "user", TaskAssignment.principal == principal.username),
            TaskAssignment.responded_by == principal.username]
    if principal.groups:
        mine.append(and_(TaskAssignment.principal_kind == "group",
                         TaskAssignment.principal.in_(list(principal.groups))))
    hit = (await session.execute(
        select(TaskAssignment.id).join(Task, Task.id == TaskAssignment.task_id)
        .where(Task.run_id == run_id, or_(*mine)).limit(1))).first()
    return hit is not None


async def _viewed_by_granted_app(session: AsyncSession, run: Any, apps: set[str]) -> bool:
    """这次运行的工作流版本是不是被有授权的应用（当前发布版本）作为视图固定着。"""
    from app.db.models import App, AppVersion

    if run.run_class != "formal" or not run.workflow_id or run.version is None or not apps:
        return False
    rows = (await session.execute(
        select(AppVersion.pins).join(App, and_(App.id == AppVersion.app_id, App.published_version == AppVersion.version))
        .where(App.id.in_(list(apps))))).scalars()
    for pins in rows:
        for pin in pins or []:
            if (isinstance(pin, dict) and pin.get("workflow_id") == run.workflow_id
                    and pin.get("version") == run.version and pin.get("use", "view") == "view"):
                return True
    return False


async def run_readable(principal: Any, run: Any) -> bool:
    """能不能读这次运行（证据、事件、工件）。run 可以是 Run，也可以是运行 id；不存在的运行读不了。"""
    if _unrestricted(principal):
        return True
    if not principal.username:
        return False
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        if isinstance(run, str):
            run = await session.get(Run, run)
        if run is None:
            return False
        if run.started_by == principal.username:
            return True
        if run.origin_kind == "personal_app" and run.app_id:
            return await _owns_personal_app(session, principal, run.app_id)
        if await _participated(session, principal, run.id):
            return True
        apps = await app_roles(session, principal)
        if run.app_id and run.app_id in apps:
            if run.origin_kind in _VIEW_ORIGINS or apps[run.app_id] & {"observe", "manage"}:
                return True
        return await _viewed_by_granted_app(session, run, set(apps))


async def _owns_personal_app(session: AsyncSession, principal: Any, app_id: str) -> bool:
    """个人应用的刷新只有这个应用的拥有者能读：别人参与不了（不派待办）、也没有授权可言（个人应用不能分享）。按应用上记的
    拥有者认，不按发起人——刷新只能由拥有者发起，这里不靠它。"""
    from app.db.models import App

    owner = await session.scalar(select(App.owner).where(App.id == app_id, App.scope == "personal"))
    return bool(owner) and owner == principal.username


async def artifact_readable(principal: Any, artifact_id: str) -> bool:
    """能不能读这件工件：引用它的某次运行能读就能读。工件按内容寻址，同一件可能被好几次运行引用。"""
    if _unrestricted(principal):
        return True
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        run_ids = [r for r in (await session.execute(
            select(Artifact.run_id).where(Artifact.id == artifact_id))).scalars() if r]
    for run_id in run_ids:
        if await run_readable(principal, run_id):
            return True
    return False
