"""运行、工件的读权限（§3.12「运行、证据、工件（只读）」那一行）。

关登录：都能读。开登录后：
- 只给拥有者的运行（PRIVATE_ORIGINS，docs/design/normal-user-apps.md N7、§3.12「看别人的个人应用内容」一行全是 ✗）：
  个人应用的刷新、个人蓝图的试运行只有拥有者能读——管理员、搭建者也不例外，运行、证据、工件、文件下载、运行列表、
  事件和实时流一个口径（private_owner / hidden_runs）；
- builder 及以上：别的运行都能读；
- user 角色只能读：
  - 自己发起的运行；
  - 自己参与的实例：派给过自己（或自己所在的组）的待办，或者自己回复过的待办所在的运行。派给「任何人」的不算：那是
    没有应用绑定的运行（画布、定时任务），user 角色不该因此读到；
  - 有授权（use / observe / manage，含 everyone 和自己所在的组）的应用里：视图运行（来源是应用视图、按新口径补算）；
    observe、manage 授权另能读全部实例；应用发布版本里作为视图固定的工作流版本的正式运行（视图读的是定时任务那次
    运行，它不带应用 id）。

user 角色读到的查询快照一律按派生遮罩处理、tool_snapshot 的原文不给（api/artifacts.py），这一层只回答「能不能读」。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

#: 视图运行的来源（engine/origin.py）
_VIEW_ORIGINS = ("app_view", "app_backfill")
#: 只给拥有者读的运行来源：个人应用的刷新（personal_app）；试运行（trial）里出自个人蓝图的那些
PRIVATE_ORIGINS = ("personal_app", "trial")


async def private_owner(session: AsyncSession, run: Any) -> tuple[bool, str | None]:
    """这次运行是不是只给拥有者读的，是的话交回拥有者：(是不是, 拥有者)。

    - 个人应用的刷新：拥有者按应用上记的认（刷新只能由拥有者发起，这里不靠发起人）。应用不在了、不是个人应用的，拥有者
      为 None——谁都读不了，不往宽处猜；
    - 试运行：出自个人蓝图的（run.origin 里记的蓝图 id，发起时由服务端写）只给蓝图的拥有者；共享蓝图的试运行照旧按
      搭建者读。"""
    from app.db.models_apps import App
    from app.db.models_blueprints import Blueprint

    kind = getattr(run, "origin_kind", None)
    if kind == "personal_app":
        owner = await session.scalar(select(App.owner).where(App.id == run.app_id, App.scope == "personal")) \
            if run.app_id else None
        return True, owner
    if kind == "trial":
        blueprint_id = (run.origin or {}).get("blueprint_id") if isinstance(run.origin, dict) else None
        row = (await session.execute(select(Blueprint.scope, Blueprint.owner).where(
            Blueprint.id == str(blueprint_id or "")))).first()
        if row is not None and row.scope == "personal":
            return True, row.owner
    return False, None


def hidden_runs(principal: Any) -> Any:
    """SQL 条件：这个人读不了的只给拥有者的运行（别人的个人应用刷新、别人的个人蓝图试运行）。运行列表、审批列表、
    探索问题聚类按 not_(hidden_runs(...)) 筛，和 run_readable 同一个口径。关登录时什么都不藏（没有可信的身份）。

    各项都按非空比较（coalesce）：来源、应用、蓝图 id 为空的老运行不能因为 NULL 被一起筛掉。"""
    from sqlalchemy import false, func

    from app.db.models import Run
    from app.db.models_apps import App
    from app.db.models_blueprints import Blueprint
    from app.db.portable import json_text

    if principal.mode == "off":
        return false()
    me = principal.username or ""
    mine = select(App.id).where(App.scope == "personal", App.owner == me)
    others = select(Blueprint.id).where(Blueprint.scope == "personal",
                                        func.coalesce(Blueprint.owner, "") != me)
    kind = func.coalesce(Run.origin_kind, "")
    return or_(
        and_(kind == "personal_app", or_(Run.app_id.is_(None), Run.app_id.not_in(mine))),
        and_(kind == "trial", func.coalesce(json_text(Run.origin, "blueprint_id"), "").in_(others)),
    )


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
    """能不能读这次运行（证据、事件、工件）。run 可以是 Run，也可以是运行 id。

    关登录：能。只给拥有者的运行（private_owner）：只有拥有者能读，管理员、搭建者也不例外。builder 及以上：别的都能读，
    不存在的运行也答「能」（接口照旧回 404，不改成 403）。user：见模块开头。"""
    if principal.mode == "off":
        return True
    builder = principal.at_least("builder")
    if builder and run is not None and not isinstance(run, str) \
            and (getattr(run, "origin_kind", None) or "") not in PRIVATE_ORIGINS:
        return True
    if not builder and not principal.username:
        return False
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        if isinstance(run, str):
            run = await session.get(Run, run)
        if run is None:
            return builder
        private, owner = await private_owner(session, run)
        if private:
            return bool(owner) and bool(principal.username) and owner == principal.username
        if builder:
            return True
        if run.started_by == principal.username:
            return True
        if await _participated(session, principal, run.id):
            return True
        apps = await app_roles(session, principal)
        if run.app_id and run.app_id in apps:
            if run.origin_kind in _VIEW_ORIGINS or apps[run.app_id] & {"observe", "manage"}:
                return True
        return await _viewed_by_granted_app(session, run, set(apps))


async def artifact_readable(principal: Any, artifact_id: str) -> bool:
    """能不能读这件工件：引用它的某次运行能读就能读。工件按内容寻址，同一件可能被好几次运行引用。不挂在任何运行上的
    工件：builder 及以上照旧能读，user 读不了。只被别人的个人应用刷新、个人蓝图试运行引用的，builder 及以上也读不了。"""
    if principal.mode == "off":
        return True
    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        run_ids = [r for r in (await session.execute(
            select(Artifact.run_id).where(Artifact.id == artifact_id))).scalars() if r]
    if not run_ids:
        return principal.at_least("builder")
    for run_id in run_ids:
        if await run_readable(principal, run_id):
            return True
    return False
