"""个人应用（N7、§3.9）：使用者自己的应用。图存在蓝图上（不进工作流表）、只有拥有者看得见、永远跑探索运行
（origin_kind=personal_app，强加 MASK_RESULTS / READ_ONLY）、受每日上限约束；想给别人用就提交审核，搭建者在共享副本上发布。

- 开关：设置里 apps 这一组的「使用者可以建个人应用」（默认关，只有管理员能改，api/settings.py）。开了登录时搭建者及以上
  随时能用（§3.12）；使用者要开关开着；关登录时全部放行，开关照样生效（关着就没有入口）。
- 约束：和使用者问数据同一份白名单（auth/chat_policy.py）——节点只许读数据、整理、撰写这一类，工具只许数据源查询和只读的
  内置工具；没有人工节点、不写任何东西。存的时候查（save_personal），刷新时再查一次（白名单以后可能收紧），应用检查也按它
  报（personal.policy）。
- 应用：scope=personal、owner=蓝图的拥有者、blueprint_id=蓝图；没有应用版本，草稿就是在用的那一份（改了就生效）。固定版本
  写 blueprint（apps/spec.Pin），视图来源一律 exploratory：取这个应用、这个拥有者、这张图下最近一次成功封存的 personal_app
  运行（exploratory_runs，apps/resolve.py 解析时调）。
- 刷新：以拥有者的身份发起探索运行，和按需视图共用每日上限（apps.on_demand_daily_limit），按人计。同一个应用正在刷新时
  再点，接上那一次，不另花一次。
- 提交审核：开一条 review 待办（process/tasks.open_review），派给选定的搭建者或组。待办的 run_id 不能为空（带外键，开待办、
  记事件、通知都按运行走），挂在最近一次成功的刷新上——审核的人要看的也正是这次用真数出的结果；所以提交前要至少成功刷新
  一次。搭建者取共享副本（copy_for_review，原个人应用不动），在副本上试运行、走发布单；发布单做完调 review_published，
  待办记为已发布、通知提交人；不发布就在待办里退回（reject_review）。
"""
from __future__ import annotations

import asyncio
import copy as copy_module
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: 管理员开关：使用者能不能建个人应用（apps 设置组里，默认关）
SETTING_KEY = "user_personal_apps"
#: 个人应用刷新的运行来源（engine/origin.py）
ORIGIN = "personal_app"
#: 提交审核开的待办种类（§3.1 tasks.kind 新增 review）
REVIEW = "review"
#: 审核待办按它找回蓝图（Task.correlation_key）
REVIEW_KEY = "blueprint:"
#: 个人应用的那一个固定版本的别名（as_personal 把固定版本换成它）
PIN = "main"

# 机器码（接口回 {detail, code}；白话在 app/plain.py）
PERSONAL_OFF = "personal.off"
POLICY = "personal.policy"
DAILY_LIMIT = "personal.daily_limit"
GRAPH_CHANGED = "personal.graph_changed"
NOT_TRIALED = "personal.not_trialed"
NO_RUN = "personal.no_run"
IN_REVIEW = "personal.in_review"
NOT_PERSONAL = "personal.not_personal"
REVIEW_TARGET = "personal.review_target"

BLUEPRINT_GONE = "蓝图不存在，可能已被删除"
APP_GONE = "应用不存在，可能已被删除"


class PersonalRefused(Exception):
    """不做这件事：status_code 照 HTTP 语义，message 是给人看的一句话，code 是 personal.* 机器码。issues 是蓝图检查交回的
    问题（存个人应用时缺项、待填），接口原样带给前端。"""

    def __init__(self, status_code: int, message: str, code: str | None = None,
                 issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.issues = issues or []


# --------------------------------------------------------------------------
# 开关、拥有者、约束
# --------------------------------------------------------------------------


async def enabled(session: AsyncSession) -> bool:
    from app.api.settings import apps_settings

    return bool((await apps_settings(session))[SETTING_KEY])


async def allowed(session: AsyncSession, principal: Any) -> bool:
    """这个人能不能建个人应用、刷新、提交审核：开了登录的搭建者及以上随时能；使用者和关登录时看开关。"""
    if principal.mode != "off" and principal.at_least("builder"):
        return True
    return await enabled(session)


async def ensure_allowed(session: AsyncSession, principal: Any) -> None:
    from app.api.settings import APPS_SETTING_LABEL

    if not await allowed(session, principal):
        label = APPS_SETTING_LABEL[SETTING_KEY]
        raise PersonalRefused(403, f"没有权限：管理员还没有开放个人应用（「设置 → 应用」里的「{label}」）。"
                                   "想要一个给自己看的看板，可以请管理员打开，或请搭建者做成应用", PERSONAL_OFF)


def owns(principal: Any, owner: str | None) -> bool:
    """是不是拥有者。关登录时全部放行（没有可信的身份，同应用级权限）。"""
    return principal.mode == "off" or (bool(principal.username) and principal.username == owner)


def policy_problem(graph: Any) -> str | None:
    """个人应用的图能不能用：和使用者问数据同一份白名单（auth/chat_policy.chat_graph_problem），说法只换「在哪」。
    能用返回 None。没有人工节点、不写入都在白名单里：人工、代码、记忆写入这几类节点不在名单上；调用工具节点可以用，
    但和 Agent 一样只能绑数据源查询和只读的内置工具（模板的取数节点就是它），写入能力和自定义工具不是只读工具；数据源
    查询在运行时另按 READ_ONLY 只读、按 MASK_RESULTS 在工具边界遮，调用工具节点和 Agent 走同一道。"""
    from app.auth.chat_policy import chat_graph_problem

    return chat_graph_problem(graph, where="个人应用")


def _pin(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """个人应用的那一个固定版本（写 blueprint 的那一项）。"""
    return next((p for p in (spec or {}).get("pins") or [] if isinstance(p, dict) and p.get("blueprint")), None)


def graph_hash_of(blueprint: Any) -> str:
    """蓝图现在这张图的指纹：按图算，不信库里记的（实例化之后记没记上由别处保证，这里不赌）。"""
    from app.core.artifact_store import graph_hash

    return graph_hash(blueprint.graph or {})


async def _blueprint_of(session: AsyncSession, app: Any) -> Any:
    from app.db.models_blueprints import Blueprint

    blueprint = await session.get(Blueprint, app.blueprint_id) if app.blueprint_id else None
    if blueprint is None:
        raise PersonalRefused(409, "这个个人应用出自的蓝图已经不在了，没法再刷新；可以归档它，再从新的蓝图存一个", NOT_PERSONAL)
    return blueprint


def _personal_app(app: Any, principal: Any) -> None:
    """个人应用的接口只认个人应用、只给拥有者。别人一律当没有这个应用（不说它在不在，同应用级权限）。共享应用能不能看
    由接口先按应用级权限判过，这里只说它不是个人应用。"""
    if app is None:
        raise PersonalRefused(404, APP_GONE)
    if getattr(app, "scope", "shared") != "personal":
        raise PersonalRefused(409, "这是共享应用，不是个人应用：刷新、提交审核只对个人应用", NOT_PERSONAL)
    if not owns(principal, app.owner):
        raise PersonalRefused(404, APP_GONE)
    if app.status == "archived":
        raise PersonalRefused(409, f"「{app.name}」已归档，不能再刷新或提交审核", "app_archived")


# --------------------------------------------------------------------------
# 个人应用的描述
# --------------------------------------------------------------------------


def as_personal(spec: dict[str, Any], *, blueprint_id: str, graph_hash: str) -> dict[str, Any]:
    """任一份应用描述（模板的应用骨架、派生出来的草稿）改成个人应用的样子：固定版本换成这个蓝图；看结果的视图一律改取
    探索运行（exploratory）；入口、表单、岗位、待办外观、钩子、通知订阅都去掉（个人应用不发起实例、不派待办）；实例视图、
    流程组件、表单组件、绑定了去掉的视图或待办、动作的组件一并去掉，去空了的页面不留。"""
    from app.apps.catalog import BY_NAME
    from app.apps.spec import parse_spec
    from app.apps.tree import bind_kind

    out = copy_module.deepcopy(spec)
    out["pins"] = [{"alias": PIN, "blueprint": blueprint_id, "graph_hash": graph_hash, "use": "view"}]
    views: dict[str, Any] = {}
    for vid, view in (out.get("views") or {}).items():
        if ((view or {}).get("source") or {}).get("kind") != "instance":
            views[vid] = {"source": {"kind": "exploratory", "pin": PIN}}
    out["views"] = views
    for key in ("entries", "hooks", "notify"):
        out[key] = []
    for key in ("forms", "slots", "task_ui"):
        out[key] = {}

    def keep(comp: Any) -> bool:
        if not isinstance(comp, dict):
            return False
        entry = BY_NAME.get(str(comp.get("type"))) or {}
        if entry.get("category") in ("process", "input"):
            return False
        bind = comp.get("bind") if isinstance(comp.get("bind"), dict) else None
        if bind is not None and (bind_kind(bind) in ("task", "action") or bind.get("view") not in views):
            return False
        return comp.get("scope") is None or comp.get("scope") in views

    def prune(items: list[Any]) -> list[Any]:
        kept = []
        for comp in items:
            if keep(comp):
                comp["children"] = prune(comp.get("children") or [])
                kept.append(comp)
        return kept

    pages = []
    for page in out.get("pages") or []:
        if isinstance(page, dict) and (body := prune(page.get("body") or [])):
            pages.append({**page, "body": body})
    out["pages"] = pages
    return parse_spec(out).model_dump(mode="json", by_alias=True)


async def _base_spec(session: AsyncSession, blueprint: Any, graph: dict[str, Any], name: str, description: str,
                     graph_hash: str) -> dict[str, Any]:
    """新存的个人应用用哪份描述：这张图实例化时模板排布的应用草稿（blueprints/trial.app_spec_for，P6）；没有的（自由形）
    按派生规则从图出（trial.draft_app_spec，和共享应用同一份规则，P3）；再改成个人应用的样子（as_personal）。"""
    from app.blueprints.trial import app_spec_for, draft_app_spec

    base, _ = await app_spec_for(session, blueprint, graph_hash)
    if base is None:
        base = await draft_app_spec(session, blueprint, graph)
    return as_personal({**base, "name": name, "description": description}, blueprint_id=blueprint.id,
                       graph_hash=graph_hash)


#: 个人应用不能有的几样：它不发起实例、不派待办（spec 里的键 → 说法；应用检查 apps/lint._lint_personal 用同一份）
NOT_IN_PERSONAL = (("entries", "入口"), ("hooks", "钩子"), ("notify", "通知订阅"), ("slots", "岗位"))


def draft_problem(current: dict[str, Any], draft: dict[str, Any]) -> str | None:
    """个人应用的草稿就是在用的那一份，改了就生效（PATCH /api/apps/{id} 调）：只许改呈现——页面、组件、文字、隐藏、名称。
    固定的蓝图只由「存成我的应用」改（改取数要回到蓝图、重新试运行）；视图只能取探索运行；入口、钩子、通知订阅、岗位
    个人应用不能有。合规返回 None，不合规交回一句话（422）。绑定找不到数这类问题照旧在页面和检查里照实报，不挡保存。"""
    def pins(spec: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
        return [(p.get("alias"), p.get("blueprint"), p.get("graph_hash")) for p in spec.get("pins") or []
                if isinstance(p, dict)]

    if not isinstance(draft, dict):
        return "应用描述需要是一个对象"
    if pins(draft) != pins(current):
        return "个人应用固定的是它自己的蓝图，不能在这里改：要改取数，请回到蓝图修改、用真数试一次，再存成你的应用"
    aliases = {alias for alias, _, _ in pins(current)}
    for vid, view in (draft.get("views") or {}).items():
        source = (view or {}).get("source") if isinstance(view, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "exploratory" or source.get("pin") not in aliases:
            return f"视图「{vid}」：个人应用的视图只能取它自己的探索运行（每次刷新的结果）"
    if declared := [label for key, label in NOT_IN_PERSONAL if draft.get(key)]:
        return f"个人应用不发起实例、不派待办，不能有{'、'.join(declared)}"
    return None


def _purpose(blueprint: Any) -> str:
    value = ((blueprint.spec or {}).get("purpose") or {}).get("value")
    return value.strip()[:2000] if isinstance(value, str) else ""


# --------------------------------------------------------------------------
# 存成个人应用
# --------------------------------------------------------------------------


async def save_personal(session: AsyncSession, blueprint: Any, *, principal: Any) -> Any:
    """校验图满足使用者的约束，建或更新 scope=personal 的 App（提交），交回 App。

    要先用真数试过（蓝图上有试运行）——界面上也是试过之后才有「存成我的应用」，服务端一样拦。蓝图检查（bp.*，按个人应用
    的要求）有错的不存，问题原样交回。新建时名字、说明取蓝图的；再存一次时应用上的名字、页面都是拥有者的，图没变的什么都
    不动；图变了的，原来的页面绑定在新图上都还找得到的留着（改过的文字、隐藏、顺序不丢），有找不到的按新图重新派生——
    页面上不会挂着找不到数的块。"""
    from app.apps.audit import record
    from app.db.models_apps import App

    await ensure_allowed(session, principal)
    if blueprint is None or blueprint.scope != "personal" or not owns(principal, blueprint.owner):
        raise PersonalRefused(404, BLUEPRINT_GONE)
    graph = blueprint.graph or {}
    if not graph.get("nodes"):
        raise PersonalRefused(409, "蓝图还没有生成图：请先把卡片补齐、用真数试一次", NOT_TRIALED)
    if not blueprint.last_trial_run_id:
        raise PersonalRefused(409, "还没有用真数试过：先试一次、看过结果，再存成你的应用", NOT_TRIALED)
    # 卡片上的问题先说（「个人应用不能有审批步骤」比「不能使用人工节点」好懂），图本身的约束再核一遍
    if issues := _blocking_issues(blueprint):
        raise PersonalRefused(422, f"蓝图还有要改的地方：{issues[0]['message']}", issues[0]["code"], issues=issues)
    if problem := policy_problem(graph):
        raise PersonalRefused(403, problem, POLICY)
    hash_ = graph_hash_of(blueprint)
    name = (blueprint.name or "").strip()[:200] or _purpose(blueprint)[:200] or "我的应用"
    description = _purpose(blueprint)
    actor = principal.username
    app = await session.get(App, blueprint.app_id) if blueprint.app_id else None
    if app is not None and (app.scope != "personal" or app.status == "archived"):
        # 原来的个人应用归档了（拥有者自己、或管理员归的）：不复活它，另建一个
        app = None
    created = app is None
    if app is None:
        app = App(name=name, description=description, icon="app",
                  draft=await _base_spec(session, blueprint, graph, name, description, hash_),
                  draft_bindings={"slots": {}},
                  created_by=actor, updated_by=actor, scope="personal", owner=blueprint.owner,
                  blueprint_id=blueprint.id)
        session.add(app)
        await session.flush()
    else:
        app.draft = await _kept_or_fresh(session, app, blueprint, graph, app.name, app.description or "", hash_)
        app.updated_by = actor
    blueprint.app_id = app.id
    blueprint.graph_hash = hash_
    if blueprint.status != "in_review":
        blueprint.status = "personal"
    await event(session, blueprint, "save_personal", principal, {"app_id": app.id, "graph_hash": hash_,
                                                                  "created": created})
    await record(session, action="app.personal_save", subject_kind="app", subject_id=app.id, actor=actor,
                 data={"blueprint_id": blueprint.id, "graph_hash": hash_, "created": created})
    await session.commit()
    await session.refresh(app)
    return app


def _blocking_issues(blueprint: Any) -> list[dict[str, Any]]:
    """蓝图检查（blueprints/ops.blueprint_issues，按个人应用的要求：缺项、模板不能做成个人应用、有审批步骤、要写回）里
    挡住存的：error 一级，带白话。"""
    from app.blueprints.ops import blueprint_issues
    from app.blueprints.trial import FREEFORM, template_of
    from app.plain import with_plain

    # 自由形（从运行、工作流版本导入的）卡片上可以没选模板，「还没有选场景」不算（同试运行、蓝图接口）
    freeform = template_of(blueprint) == FREEFORM
    return [with_plain(i) for i in blueprint_issues(blueprint.spec or {}, for_="personal")
            if i.get("level") == "error" and not (freeform and i.get("path") == "/template")]


async def _kept_or_fresh(session: AsyncSession, app: Any, blueprint: Any, graph: dict[str, Any], name: str,
                         description: str, hash_: str) -> dict[str, Any]:
    from app.apps.lint import lint_app

    current = copy_module.deepcopy(app.draft or {})
    pin = _pin(current)
    if pin is not None and pin.get("graph_hash") == hash_:
        return {**current, "name": name, "description": description}
    kept = {**current, "name": name, "description": description,
            "pins": [{"alias": PIN, "blueprint": blueprint.id, "graph_hash": hash_, "use": "view"}]}
    issues = await lint_app(session, kept, {}, scope="personal")
    if any(i["level"] == "error" for i in issues):
        return await _base_spec(session, blueprint, graph, name, description, hash_)
    return kept


async def event(session: AsyncSession, blueprint: Any, op: str, principal: Any, data: dict[str, Any]) -> None:
    """记一条蓝图留痕（blueprint_events，blueprints/trial.record_event），和改动同一个事务，调用方提交。"""
    from app.blueprints.trial import record_event

    await record_event(session, blueprint, op, data, principal=principal)


# --------------------------------------------------------------------------
# 刷新
# --------------------------------------------------------------------------

#: 每人一把锁：查上限和发起在同一把锁里，同一个人连点两下不会多跑一次
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(who: str | None) -> Any:
    """跨进程的锁（app/cluster/mutex）：查上限和发起在一起，多进程时同一个人连点两下也不会多跑一次。"""
    from app.cluster import mutex

    return mutex.lock(f"personal-refresh:{who or ''}")


def _today_start() -> datetime:
    """「每天」按服务端所在时区的自然日算（同按需视图，apps/resolve.run_view）。"""
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def used_today(session: AsyncSession, who: str | None) -> int:
    from app.db.models import Run

    query = select(func.count(Run.id)).where(Run.origin_kind == ORIGIN, Run.created_at >= _today_start())
    query = query.where(Run.started_by == who) if who else query.where(Run.started_by.is_(None))
    return int(await session.scalar(query) or 0)


async def remaining_today(session: AsyncSession, who: str | None) -> tuple[int, int]:
    """(今天还能刷新几次, 每天的上限)。上限和按需视图共用设置项 apps.on_demand_daily_limit，按人计。"""
    from app.apps.resolve import daily_limit

    limit = await daily_limit(session)
    return max(limit - await used_today(session, who), 0), limit


def refresh_inputs(blueprint: Any) -> dict[str, Any]:
    """刷新时交给入口的输入：和试运行同一个规则（blueprints/trial.trial_inputs）——看板类按「多久更新」卡片、按现在这一刻
    算出最近一期（和定时任务到点渲染的是同一份），统计期跟着日子走；算不出的（自由形、没有统计期的）交空的，入口节点按
    字段的默认值补。"""
    from app.blueprints.trial import trial_inputs

    return trial_inputs(blueprint, None)


async def refresh(session: AsyncSession, app: Any, *, principal: Any) -> tuple[Any, int]:
    """发起一次个人应用的刷新（探索运行），交回 (运行, 今天还剩几次)。

    以拥有者的身份发起（记忆固定为他自己的范围，知识库取设置里的默认值，同问数据），强加 MASK_RESULTS、READ_ONLY：查询
    结果在工具边界就遮，数据源只读。跑的是蓝图上的图，和应用记的指纹对不上（蓝图改过、还没重新存）就不跑——跑了页面也
    取不到它。同一个应用这张图正在刷新的，接上那一次，不另花一次。"""
    from app.auth.chat_policy import chat_memory_scope
    from app.engine.launch import launch_run
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    await ensure_allowed(session, principal)
    _personal_app(app, principal)
    blueprint = await _blueprint_of(session, app)
    pin = _pin(app.draft)
    hash_ = graph_hash_of(blueprint)
    if pin is None or pin.get("graph_hash") != hash_:
        raise PersonalRefused(409, "蓝图在存成个人应用之后又改过：请先回到蓝图「存成我的应用」，再刷新", GRAPH_CHANGED)
    if problem := policy_problem(blueprint.graph):
        raise PersonalRefused(403, problem, POLICY)
    who = principal.username
    async with _lock(who):
        active = await latest_refresh(session, app.id, hash_, active_only=True)
        if active is not None:
            return active, (await remaining_today(session, who))[0]
        remaining, limit = await remaining_today(session, who)
        if remaining <= 0:
            raise PersonalRefused(429, f"今天的刷新次数已经用完（每人每天 {limit} 次），请明天再试，或请管理员调整上限",
                                  DAILY_LIMIT)
        run = await launch_run(
            session, graph=blueprint.graph, input_payload=refresh_inputs(blueprint),
            run_class="exploratory", workflow_name=app.name,
            actor=who, origin_kind=ORIGIN, app_id=app.id, memory_scope=chat_memory_scope(app.owner),
            origin={"blueprint_id": blueprint.id, "graph_hash": hash_, "actor_verified": principal.verified,
                    "auth_mode": principal.mode},
            options={MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True})
    return run, remaining - 1


def _runs_of(app_id: str, graph_hash: str) -> Any:
    from app.db.models import Run
    from app.db.portable import json_eq

    return select(Run).where(Run.app_id == app_id, Run.origin_kind == ORIGIN,
                             json_eq(Run.origin, "graph_hash", graph_hash))


async def latest_refresh(session: AsyncSession, app_id: str, graph_hash: str, *, active_only: bool = False) -> Any:
    """这个应用这张图最近一次刷新（不论成败）；active_only 只要还没结束的。"""
    from app.db.models import Run

    query = _runs_of(app_id, graph_hash)
    if active_only:
        query = query.where(Run.status.in_(("queued", "running", "interrupted")))
    return (await session.execute(query.order_by(Run.created_at.desc()).limit(1))).scalars().first()


async def exploratory_runs(session: AsyncSession, app_id: str, graph_hash: str | None, n: int,
                           owner: str | None = None) -> list[Any]:
    """视图来源 exploratory：这个应用、这个拥有者、这张图（run.origin 里记的指纹，发起时由服务端写）下最近 n 次成功且
    已封存的刷新，新的在前。sealed_now 认的是「这一次结束时封的」（同 apps/resolve.sealed_runs）。owner 给了只认他发起的
    （开了登录时刷新只能由拥有者发起；关登录没有可信的身份，不按人筛）。"""
    from app.db.models import Run
    from app.scheduler.auto_compare import sealed_now

    if not graph_hash:
        return []
    query = _runs_of(app_id, graph_hash).where(Run.status == "succeeded")
    if owner:
        query = query.where(Run.started_by == owner)
    rows = list((await session.execute(query.order_by(Run.finished_at.desc(), Run.created_at.desc())
                                       .limit(max(n, 1) * 3 + 5))).scalars())
    out = []
    for run in rows:
        if await sealed_now(session, run):
            out.append(run)
            if len(out) >= n:
                break
    return out


# --------------------------------------------------------------------------
# 提交审核、共享副本
# --------------------------------------------------------------------------


async def review_task(session: AsyncSession, blueprint_id: str, *, pending: bool = True) -> Any:
    """这个蓝图最近一条审核待办；pending 只要还在等的。"""
    from app.db.models_process import Task

    query = select(Task).where(Task.kind == REVIEW, Task.correlation_key == f"{REVIEW_KEY}{blueprint_id}")
    if pending:
        query = query.where(Task.status == "pending")
    return (await session.execute(query.order_by(Task.created_at.desc()).limit(1))).scalars().first()


async def _review_targets(session: AsyncSession, to: Any) -> list[tuple[str, str]]:
    """提交给谁：一位或几位搭建者（用户），或者一个组。开了登录时按名录核对（同岗位绑定、授权）：没有的、停用的拒收；
    点名的人要是搭建者或管理员——使用者拿不到共享副本，派给他等于没人能审。组里的人在打开待办那一刻展开成成员，取副本时
    再按角色判。"""
    from app.api.deps import auth_mode
    from app.auth.directory import load_directory, mismatch
    from app.db.models import User

    targets = list(dict.fromkeys((str((t or {}).get("kind") or ""), str((t or {}).get("name") or "").strip())
                                 for t in to or [] if isinstance(t, dict)))
    if not targets or any(kind not in ("user", "group") or not name for kind, name in targets):
        raise PersonalRefused(422, "提交审核要选一位搭建者，或者交给一个组", REVIEW_TARGET)
    if (mode := auth_mode()) == "off":
        return targets
    known = await load_directory(session)
    for kind, name in targets:
        if (m := mismatch(known, mode, kind, name)) is not None and m.blocking:
            raise PersonalRefused(422, f"{m.text}：请从候选中重新选择审核的人", REVIEW_TARGET)
        if kind == "user":
            role = await session.scalar(select(User.role).where(User.username == name))
            if role is not None and role not in ("builder", "admin"):
                raise PersonalRefused(422, f"「{name}」不是搭建者：审核个人应用要交给搭建者或管理员，他们才能把它做成正式应用",
                                      REVIEW_TARGET)
    return targets


async def submit_review(session: AsyncSession, app: Any, *, to: list[dict[str, str]], principal: Any,
                        note: str | None = None) -> Any:
    """开一条 review 待办派给搭建者（提交），交回 Task。蓝图记为审核中；同一个蓝图已经有一条在等的，不另开。"""
    from app.apps.audit import record
    from app.process.tasks import announce, open_review

    await ensure_allowed(session, principal)
    _personal_app(app, principal)
    blueprint = await _blueprint_of(session, app)
    targets = await _review_targets(session, to)
    if await review_task(session, blueprint.id) is not None:
        raise PersonalRefused(409, "已经提交过审核，正在等搭建者处理；处理完（发布或退回）之后才能再提交", IN_REVIEW)
    pin = _pin(app.draft)
    runs = await exploratory_runs(session, app.id, (pin or {}).get("graph_hash"), 1,
                                  app.owner if principal.mode != "off" else None)
    if not runs:
        raise PersonalRefused(409, "提交审核前请先成功刷新一次：审核的人要看到这个应用用真数出的结果", NO_RUN)
    note = (note or "").strip()[:2000]
    task = await open_review(session, run=runs[0], app_id=app.id, blueprint_id=blueprint.id,
                             title=f"审核个人应用「{app.name}」", to=targets)
    blueprint.status = "in_review"
    await event(session, blueprint, "submit_review", principal,
                {"task_id": task.id, "run_id": runs[0].id, "to": [{"kind": k, "name": n} for k, n in targets],
                 "note": note})
    await record(session, action="personal.submit_review", subject_kind="app", subject_id=app.id,
                 actor=principal.username, data={"blueprint_id": blueprint.id, "task_id": task.id,
                                                 "to": [{"kind": k, "name": n} for k, n in targets]})
    await session.commit()
    await announce(task.id)
    return task


async def copy_for_review(session: AsyncSession, blueprint: Any, *, principal: Any) -> Any:
    """搭建者处理审核时得到这个蓝图的共享副本（copied_from 指向原蓝图，提交），原个人应用和原蓝图都不动。

    只有这条审核派给的人能取（同待办的处理人，process/tasks.handles）：个人蓝图只有拥有者看得见，管理员也不例外，审核是
    拥有者主动交出去的那一次。同一个人再取，交回他上次取的那一份。审核已经结束（发布、退回）的，取过的照样交回，没取过的
    不再给。"""
    from app.db.models_blueprints import Blueprint
    from app.process.tasks import review_handler

    if principal.mode != "off" and not principal.at_least("builder"):
        raise PersonalRefused(403, "没有权限：审核个人应用需要搭建者及以上的角色", "personal.review_forbidden")
    if blueprint is None or blueprint.scope != "personal":
        raise PersonalRefused(404, BLUEPRINT_GONE)
    mine = await _copy_by(session, blueprint.id, principal.username)
    if mine is not None:
        return mine
    task = await review_task(session, blueprint.id)
    if task is None or not await review_handler(session, task, principal):
        raise PersonalRefused(404, BLUEPRINT_GONE)
    copied = Blueprint(name=blueprint.name, owner=principal.username, scope="shared", status="drafting",
                       spec=copy_module.deepcopy(blueprint.spec or {}), template_id=blueprint.template_id,
                       template_version=blueprint.template_version, source=dict(blueprint.source or {}),
                       graph=copy_module.deepcopy(blueprint.graph or {}), graph_hash=graph_hash_of(blueprint),
                       joints=copy_module.deepcopy(blueprint.joints or {}), copied_from=blueprint.id,
                       updated_by=principal.username)
    session.add(copied)
    await session.flush()
    await event(session, copied, "copy", principal, {"from": blueprint.id, "task_id": task.id})
    await event(session, blueprint, "copy", principal, {"to": copied.id, "task_id": task.id})
    await session.commit()
    await session.refresh(copied)
    return copied


async def _copy_by(session: AsyncSession, blueprint_id: str, who: str | None) -> Any:
    from app.db.models_blueprints import Blueprint

    query = select(Blueprint).where(Blueprint.copied_from == blueprint_id)
    query = query.where(Blueprint.owner == who) if who else query.where(Blueprint.owner.is_(None))
    return (await session.execute(query.order_by(Blueprint.created_at.desc()).limit(1))).scalars().first()


async def review_published(session: AsyncSession, blueprint: Any, *, app_id: str, principal: Any) -> str | None:
    """发布单把审核副本发布完之后调（包 B2 的 publish_plan 在最后一步提交之后调，这里自己提交）：原蓝图的审核待办记为
    已发布（结论里记副本和发布出来的应用），原蓝图回到「个人应用」，通知提交人。不是审核副本的、原蓝图的待办已经结束的，
    什么都不做，交回 None；做了交回待办 id。提交人自己决定个人应用留不留，这里不动它。"""
    from app.db.models_blueprints import Blueprint
    from app.process.tasks import close_review

    if blueprint is None or not blueprint.copied_from:
        return None
    original = await session.get(Blueprint, blueprint.copied_from)
    if original is None:
        return None
    task_id = await close_review(session, original.id, principal,
                                 {"decision": "published", "copy": blueprint.id, "app_id": app_id})
    if task_id is None:
        return None
    if original.status == "in_review":
        original.status = "personal"
    await event(session, original, "review_published", principal, {"task_id": task_id, "copy": blueprint.id,
                                                                    "app_id": app_id})
    await session.commit()
    await _announce_review(session, original, task_id, "published", app_id)
    return task_id


async def settle_review(session: AsyncSession, original: Any) -> str | None:
    """审核副本已经发布、原蓝图的审核待办却还挂着（发布单做完时没能结束它：通知、写库出了错只记了日志）：补上。拥有者
    打开个人应用、处理人打开待办时顺带看一眼（同试运行的 settle），不让原蓝图一直停在「审核中」。记的处理人是发布那份
    副本的人。做了交回待办 id，不用做为 None。"""
    from types import SimpleNamespace

    from app.db.models_blueprints import Blueprint

    if original is None or original.scope != "personal" or original.status != "in_review":
        return None
    published = (await session.execute(select(Blueprint).where(
        Blueprint.copied_from == original.id, Blueprint.status == "published", Blueprint.app_id.is_not(None))
        .order_by(Blueprint.updated_at.desc()).limit(1))).scalars().first()
    if published is None or await review_task(session, original.id) is None:
        return None
    publisher = SimpleNamespace(username=published.owner, verified=False, groups=(), mode="local",
                                at_least=lambda _role: False)
    return await review_published(session, published, app_id=published.app_id, principal=publisher)


async def reject_review(task_id: str, principal: Any, *, note: str | None) -> None:
    """审核的人在待办里退回（process/tasks.respond 转过来）：待办记为退回，原蓝图回到「个人应用」，通知提交人。"""
    from app.db.base import SessionLocal
    from app.db.models_blueprints import Blueprint
    from app.db.models_process import Task
    from app.process.tasks import TaskRefused, close_review

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        blueprint_id = (task.correlation_key or "").removeprefix(REVIEW_KEY) if task is not None else ""
        original = await session.get(Blueprint, blueprint_id) if blueprint_id else None
        if task is None or original is None:
            raise TaskRefused("待办不存在，提交审核的蓝图可能已被删除", status_code=404)
        closed = await close_review(session, original.id, principal,
                                    {"decision": "reject", "note": (note or "").strip()[:2000]}, require_handler=True)
        if closed is None:
            raise TaskRefused("这条待办已经处理过了，请刷新页面查看最新状态")
        if original.status == "in_review":
            original.status = "personal"
        await event(session, original, "review_rejected", principal, {"task_id": closed, "note": note or ""})
        await session.commit()
        await _announce_review(session, original, closed, "rejected", original.app_id)


async def _announce_review(session: AsyncSession, blueprint: Any, task_id: str, decision: str,
                           app_id: str | None) -> None:
    """通知提交人（蓝图的拥有者）审核有了结果：订阅了「我提交审核的应用有结果」的个人渠道和全局渠道收到。app_id 是链接
    过去的应用（发布了的是正式应用，退回的是他的个人应用）。"""
    from app.notify.service import notify_review, spawn

    if blueprint.owner:
        spawn(notify_review(task_id, decision, owner=blueprint.owner, name=blueprint.name or "", app_id=app_id))


async def review_payload(session: AsyncSession, task: Any) -> dict[str, Any]:
    """审核待办给处理人看的内容（待办箱、待办详情的 payload）：提交的是哪个蓝图、哪个个人应用、谁提交的、留了什么话、
    这个人取过副本没有。不含个人应用页面上的数——要看数，取副本后在副本上用真数试一次。"""
    from app.db.models_blueprints import Blueprint, BlueprintEvent

    blueprint_id = (task.correlation_key or "").removeprefix(REVIEW_KEY)
    blueprint = await session.get(Blueprint, blueprint_id) if blueprint_id else None
    if blueprint is None:
        # 蓝图已经不在了（被删）：键照样都给，界面按同一个形状读，不另判缺键
        return {"kind": REVIEW, "blueprint_id": blueprint_id or None, "app_id": task.app_id, "name": None,
                "purpose": "", "template_id": None, "submitted_by": None, "note": "", "copies": []}
    await settle_review(session, blueprint)
    submitted = (await session.execute(select(BlueprintEvent).where(
        BlueprintEvent.blueprint_id == blueprint.id, BlueprintEvent.op == "submit_review")
        .order_by(BlueprintEvent.seq.desc()).limit(1))).scalars().first()
    copies = (await session.execute(select(Blueprint.id, Blueprint.owner, Blueprint.status, Blueprint.app_id)
                                    .where(Blueprint.copied_from == blueprint.id)
                                    .order_by(Blueprint.created_at))).all()
    return {"kind": REVIEW, "blueprint_id": blueprint.id, "app_id": task.app_id, "name": blueprint.name,
            "purpose": _purpose(blueprint), "template_id": blueprint.template_id,
            "submitted_by": submitted.actor if submitted is not None else blueprint.owner,
            "note": (submitted.data or {}).get("note") or "" if submitted is not None else "",
            "copies": [{"id": c.id, "owner": c.owner, "status": c.status, "app_id": c.app_id} for c in copies]}


# --------------------------------------------------------------------------
# 给拥有者看的状态
# --------------------------------------------------------------------------


async def personal_state(session: AsyncSession, app: Any, principal: Any) -> dict[str, Any]:
    """个人应用详情里给拥有者的那一块：能不能刷新、提交审核（allowed，同 ensure_allowed 那一条规则：管理员关了开关之后
    使用者的个人应用还看得到，只是不能再刷新、提交审核；界面照它置灰，不自己再推一遍）、今天还能刷新几次、蓝图的状态
    （是不是改过还没重新存）、审核到哪了（在等谁、退回了、已发布成哪个正式应用）。"""
    from app.db.models_blueprints import Blueprint

    remaining, limit = await remaining_today(session, principal.username)
    blueprint = await session.get(Blueprint, app.blueprint_id) if app.blueprint_id else None
    pin = _pin(app.draft)
    out: dict[str, Any] = {"allowed": await allowed(session, principal), "remaining_today": remaining,
                           "daily_limit": limit,
                           "blueprint_status": blueprint.status if blueprint is not None else None,
                           "graph_changed": blueprint is not None and pin is not None
                           and pin.get("graph_hash") != graph_hash_of(blueprint),
                           "review": None}
    if blueprint is None:
        return out
    if await settle_review(session, blueprint) is not None:
        out["blueprint_status"] = blueprint.status
    task = await review_task(session, blueprint.id, pending=False)
    if task is not None:
        from app.process.tasks import _assignments

        rows = (await _assignments(session, [task.id])).get(task.id, [])
        outcome = task.outcome or {}
        out["review"] = {"task_id": task.id, "status": task.status, "decision": outcome.get("decision"),
                         "note": outcome.get("note") or "", "app_id": outcome.get("app_id"),
                         "to": [{"kind": a.principal_kind, "name": a.principal} for a in rows
                                if a.status != "withdrawn"],
                         "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None}
    return out
