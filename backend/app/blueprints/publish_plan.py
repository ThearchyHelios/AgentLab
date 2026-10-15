"""一次确认发布：发布单（N5、§3.6）。先算出每一步和白话摘要（只读），确认后按序执行、每步留痕，停下后从那一步接着做。

步骤的顺序让中途失败的状态无害：定时任务先停用、应用发布后才启用；授权放最后。

- 出单（plan）：先按卡片现在的样子重新实例化（blueprints/trial.materialize：试运行之后改了「谁来做」「多久更新」
  「填什么」这类卡片，图、定时任务参数、应用草稿都跟着变，发布的就是卡片上写的），再按这张图（graph_hash）算出要做的
  九步、一页白话摘要和全量预检的问题（带白话、分给「你来定」「助手可以修」「要找管理员」），落成一行 publish_plans
  （status=planned），记下卡片内容的指纹（options.spec_hash）。除了蓝图自己的图，什么都不写。
- 预检（_preflight）只读：工作流发布前检查（api/workflows.check_graph，同发布前检查接口）、按将要生成的那一版跑应用检查
  （apps/lint.lint_app，把那一版当作已发布交进去）、定时任务的设置（api/schedules.schedule_values、template_problem）、
  授权名单核对名录（api/apps.grants_problem）、蓝图检查（待确认项挡发布）、发布的级别要不要管理员。执行时在保存版本之前
  再预检一次：任何 error 都停在预检，什么都没写（连版本都不存）。
- 执行（run）、接着做（resume）：每一步直接调现有的服务函数（存版本、发布、建定时任务、建应用、发布应用、启用、授权、
  发起第一次），不走 HTTP；每步结束写 steps[i]、写审计（publish_plan.*）。某步失败 status=stopped，写明哪一步、白话原因；
  接着做从那一步开始，已完成的按结果里的 id 核对（还在、还是那个状态），不重做。
- 发布单针对的那张图（graph_hash）、卡片内容（spec_hash）变了就拒绝执行，要求重新出单；发布的图和最近一次试运行的
  不同，摘要里照实说（不拦）。
- 应用的管理权限：蓝图已经发布成应用的，出单、执行都要对那个应用有管理权限（发布单会改它的草稿、发布它、整份替换
  它的授权），只有查看权限的搭建者出单时预检报 plan.app_forbidden，执行 403。
- 填报登记：发布应用那一步把图里写的记录表版本切成往里写的那一版（records/tables.publish_table），线上原来那一版
  这时才停用，和应用的新一版一起提交。
- 补偿：工作流发布之后的失败不撤回发布（本来就撤不回），摘要照实说「工作流已发布，应用还没有」；停用的定时任务在应用发布前
  不会运行；应用停在草稿时不授权。
- 中断：执行是在请求里按步做的，进程没了（重启、被杀）时单子停在「正在执行」。启动时（startup）把这样的单改成停下，
  停在还没做完的那一步、写明「执行中断」，就能接着做。
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.db.base import utcnow
from app.db.models import Run, Schedule, Workflow, WorkflowVersion
from app.db.models_apps import App, AppVersion
from app.db.models_blueprints import Blueprint, PublishPlan

logger = logging.getLogger(__name__)

#: 发布单的步骤（顺序即执行顺序）
STEP_KINDS = (
    "save_version",       # 保存版本（按图哈希复用已有的）
    "preflight",          # 全量预检（只读）：工作流发布前检查 + 应用检查 + 定时任务预览 + 名录核对
    "publish_workflow",   # 发布工作流（受管级只有管理员）
    "create_schedule",    # 建定时任务（停用）
    "create_app",         # 建应用、派生、写岗位绑定
    "publish_app",        # 发布应用（钉住项升版时按蓝图里的处置）
    "enable_schedule",    # 启用定时任务
    "grant",              # 授权
    "first_run",          # 跑第一次（看板类默认勾选）
)
#: 问题的三类（N10 的 who）
PROBLEM_WHO = ("you", "assistant", "admin")
LEVELS = ("published", "governed")
LEVEL_TEXT = {"published": "已发布", "governed": "受管"}
#: 口径升版的处置在摘要里怎么说（apps/spec.DISPOSITIONS）
DISPOSITION_TEXT = {"recompute": "以前的数按新口径重算，旧数据留底", "dual": "新旧口径的数并列",
                    "incomparable": "注明前后不可比"}
GRANT_ROLES = ("use", "observe", "manage")

# 问题和步骤失败的编号（白话在 app/plain.py）
NOT_INSTANTIATED = "plan.not_instantiated"
GRAPH_CHANGED = "plan.graph_changed"
UNTRIALED = "plan.untrialed"
TRIAL_DIFFERS = "plan.trial_differs"
ADMIN_REQUIRED = "plan.admin_required"
DOWNGRADE_ADMIN = "plan.downgrade_admin"
PREFLIGHT = "plan.preflight"
WORKFLOW_PUBLISH = "plan.workflow_publish"
SCHEDULE_INVALID = "plan.schedule_invalid"
APP_PUBLISH = "plan.app_publish"
APP_ARCHIVED = "plan.app_archived"
APP_FORBIDDEN = "plan.app_forbidden"
GRANT_REFUSED = "plan.grant_refused"
FIRST_RUN = "plan.first_run"
OBJECT_MISSING = "plan.object_missing"
STEP_FAILED = "plan.step_failed"
INTERRUPTED = "plan.interrupted"
#: 接口拒绝执行时的机器码：发布单不在能执行的状态（已在执行、已做完、没停下）
PLAN_STATE = "plan_state"
PLAN_GONE = "发布单不存在，可能已被删除"

#: 预检时还没有工作流（新蓝图第一次发布）：应用检查里钉住项先写这个占位，执行时换成真的工作流 id
_PENDING_WORKFLOW = "__pending__"


class PlanRefused(Exception):
    """不出单 / 不执行：status_code 照 HTTP 语义，message 是给人看的一句话，code 是机器码。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class StepFailed(Exception):
    """一步没做成：code 是 plan.* 编号，message 是白话原因，problems 是挡住它的检查问题（带白话）。"""

    def __init__(self, code: str, message: str, problems: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = problems or []


@dataclass
class _Target:
    """这份发布单要发布的东西：图、哈希、应用草稿、定时任务参数，以及「将要生成的那一版」（已有相同哈希的复用）。"""

    graph: dict[str, Any]
    digest: str
    app_spec: dict[str, Any]
    schedule: dict[str, Any] | None
    workflow: Workflow | None
    version: int
    reused: bool
    name: str
    app: App | None = None
    previous: AppVersion | None = None
    #: 蓝图已经发布过（再发一版）：应用视图正在用的那个定时任务，沿用它、按新设置改，不另建一个
    schedule_row: Schedule | None = None
    #: 复用的那一版已经发布过的话，发布时的级别（同一级别就不重复发布）
    published_as: str | None = None

    @property
    def process(self) -> bool:
        return any((p or {}).get("use") == "process" for p in self.app_spec.get("pins") or [])


# --------------------------------------------------------------------------
# 要发布的东西
# --------------------------------------------------------------------------


async def _target(session: AsyncSession, bp: Blueprint) -> _Target:
    from app.blueprints.trial import app_spec_for
    from app.core.artifact_store import graph_hash

    graph = bp.graph or {}
    if not graph.get("nodes"):
        raise PlanRefused(409, "蓝图还没有生成工作流：请先把卡片补齐，用真数试运行一次", NOT_INSTANTIATED)
    digest = bp.graph_hash or graph_hash(graph)
    # 应用草稿和定时任务参数用实例化这张图时记下的那一份（试运行看到的就是它）
    app_spec, schedule = await app_spec_for(session, bp, digest)
    if app_spec is None:
        raise PlanRefused(409, "蓝图现在的图还没有实例化过：请先用真数试运行一次", NOT_INSTANTIATED)
    workflow = await session.get(Workflow, bp.workflow_id) if bp.workflow_id else None
    same = None
    if workflow is not None:
        same = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.graph_hash == digest)
            .order_by(WorkflowVersion.version.desc()).limit(1))).scalar_one_or_none()
    app = await session.get(App, bp.app_id) if bp.app_id else None
    previous = None
    if app is not None and app.published_version:
        previous = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none()
    from app.apps.versions import is_published

    return _Target(graph=graph, digest=digest, app_spec=app_spec, schedule=schedule, workflow=workflow,
                   version=same.version if same is not None else (workflow.version + 1 if workflow else 1),
                   reused=same is not None, name=workflow.name if workflow is not None else (bp.name or "未命名应用"),
                   app=app, previous=previous, schedule_row=await _app_schedule(session, app, workflow),
                   published_as=same.level if same is not None and workflow is not None
                   and is_published(same, current=workflow.published_version) else None)


async def _app_schedule(session: AsyncSession, app: App | None, workflow: Workflow | None) -> Schedule | None:
    """已有应用的「最近封存」视图正在看的那个定时任务（跑的是这个工作流）：再发一版时沿用它。"""
    if app is None or workflow is None:
        return None
    for view in ((app.draft or {}).get("views") or {}).values():
        origin = ((view or {}).get("source") or {}).get("from")
        if isinstance(origin, str) and origin.startswith("schedule:"):
            row = await session.get(Schedule, origin[len("schedule:"):])
            if row is not None and row.workflow_id == workflow.id:
                return row
    return None


def _ours(pin: dict[str, Any], workflow_id: str) -> bool:
    """这个钉住项是不是蓝图的那张图：还没发布的草稿（固定到蓝图上、还没有工作流）或者就是这个工作流。应用里另加的
    别的工作流不动。"""
    return bool(pin.get("blueprint")) or pin.get("workflow_id") in (None, workflow_id, _PENDING_WORKFLOW)


def formal_spec(draft: dict[str, Any], *, workflow_id: str, version: int, digest: str, level: str,
                schedule_id: str | None) -> dict[str, Any]:
    """应用草稿 → 引用发布出来的那一版：蓝图那张图的钉住项写工作流 id、版本号、哈希、级别；绑着它的「最近封存」视图有
    定时任务的只看它出的数。"""
    spec = copy.deepcopy(draft)
    pins, ours = [], set()
    for pin in spec.get("pins") or []:
        if not isinstance(pin, dict):
            continue
        if _ours(pin, workflow_id):
            ours.add(pin.get("alias"))
            pin = {"alias": pin.get("alias"), "workflow_id": workflow_id, "version": version, "graph_hash": digest,
                   "level": level, "use": pin.get("use") or "view"}
        pins.append(pin)
    spec["pins"] = pins
    for view in (spec.get("views") or {}).values():
        source = (view or {}).get("source") or {}
        if source.get("kind") == "latest_sealed" and source.get("pin") in ours:
            source["from"] = f"schedule:{schedule_id}" if schedule_id else "formal"
    return spec


async def _simulated_pins(session: AsyncSession, spec: dict[str, Any], target: _Target, *, workflow_id: str,
                          level: str) -> dict[str, Any]:
    """应用检查认的钉住项（apps/pins.PinState）：蓝图那张图的钉住项当作将要发布的那一版已经发布的样子（不读库），
    应用里另加的别的工作流照常从库里读。"""
    from app.apps.pins import PinState, load_pins

    pins = await load_pins(session, spec)
    workflow = SimpleNamespace(id=workflow_id, name=target.name, published_version=target.version, status=level)
    row = SimpleNamespace(workflow_id=workflow_id, version=target.version, graph=target.graph,
                          graph_hash=target.digest, level=level, retired_at=None)
    for pin in spec.get("pins") or []:
        if isinstance(pin, dict) and pin.get("alias") and pin.get("workflow_id") == workflow_id \
                and pin.get("version") == target.version:
            pins[str(pin["alias"])] = PinState(alias=str(pin["alias"]), pin=pin, workflow=workflow, row=row)
    return pins


def bindings_of(bp: Blueprint) -> dict[str, Any]:
    """岗位绑定：「谁来做」卡片上写了具体的人或组的，按模板定的岗位名绑上（templates/_common.bindings_of）。"""
    from app.blueprints.templates._common import bindings_of as from_cards

    return from_cards(bp.spec or {})


def grant_items(bp: Blueprint) -> list[tuple[str, str, str]]:
    """「谁能看」卡片 → 授权 [(kind, name, role)]。"""
    access = (bp.spec or {}).get("access") or {}
    return [(str(item.get("kind") or ""), str(item.get("name") or ""), role)
            for role in GRANT_ROLES for item in access.get(role) or [] if isinstance(item, dict)]


def _app_draft(target: _Target) -> dict[str, Any]:
    """要发布的应用草稿：新应用用实例化时的那一份；已有应用的沿用它现在的草稿（页面上改过的呈现都留着），只把跟着卡片
    走的结构换成这一版的——入口表单和入口映射（「填什么」）、新加的岗位和待办外观（「谁来做」）。不换的话，表单改过
    之后应用还按旧表单收提交，和新的图、新的记录表版本对不上。"""
    if not (target.app is not None and isinstance(target.app.draft, dict) and target.app.draft.get("pins")):
        return target.app_spec
    draft = copy.deepcopy(target.app.draft)
    fresh = target.app_spec
    forms = draft.setdefault("forms", {})
    for name, form in (fresh.get("forms") or {}).items():
        forms[name] = copy.deepcopy(form)
    for key in ("slots", "task_ui"):
        have = draft.setdefault(key, {})
        for name, value in (fresh.get(key) or {}).items():
            have.setdefault(name, copy.deepcopy(value))
    wanted = {e["id"]: e for e in fresh.get("entries") or [] if isinstance(e, dict) and e.get("id")}
    entries = []
    for entry in draft.get("entries") or []:
        new = wanted.pop(entry.get("id"), None) if isinstance(entry, dict) else None
        entries.append({**entry, **{k: copy.deepcopy(new[k]) for k in ("form", "map") if k in new}} if new else entry)
    draft["entries"] = entries + [copy.deepcopy(e) for e in wanted.values()]
    return draft


def _draft_bindings(bp: Blueprint, target: _Target) -> dict[str, Any]:
    """应用的岗位绑定：已有应用里绑过的留着，蓝图上写了而应用里还没绑的补上。"""
    have = copy.deepcopy((target.app.draft_bindings if target.app is not None else None) or {"slots": {}})
    slots = have.setdefault("slots", {})
    for slot, people in (bindings_of(bp).get("slots") or {}).items():
        if not slots.get(slot):
            slots[slot] = people
    return have


def _schedule_name(target: _Target) -> str:
    return str((target.schedule or {}).get("name") or f"{target.name}（定时）")


# --------------------------------------------------------------------------
# 预检（只读）
# --------------------------------------------------------------------------


def _problem(code: str, message: str, level: str = "error", **extra: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, **extra}


async def _preflight(session: AsyncSession, bp: Blueprint, target: _Target, *, level: str, principal: Principal,
                     options: dict[str, Any]) -> list[dict[str, Any]]:
    """全量预检，只读：交回问题（带白话、谁来改）。任何 error 都挡住发布单。"""
    from app.api.apps import grants_problem
    from app.api.schedules import schedule_values, template_problem
    from app.api.workflows import check_graph, downgrades_governed
    from app.apps.lint import lint_app
    from app.blueprints.store import blueprint_issues_of
    from app.blueprints.trial import is_trial_of
    from app.core.artifact_store import graph_hash

    # 蓝图：缺的项、待确认的项（发布前要人看过，bp.unconfirmed）、还没填的生成关节（同蓝图卡片上显示的那一份）
    out: list[dict[str, Any]] = [{**i, "source": "blueprint"} for i in blueprint_issues_of(bp)]
    # 发布的级别：受管级、把受管降为已发布都只给管理员（Q8，同发布接口）
    if level == "governed" and not principal.at_least("admin"):
        out.append(_problem(ADMIN_REQUIRED, "以受管级别发布需要管理员", source="plan"))
    elif target.workflow is not None and not principal.at_least("admin") \
            and await downgrades_governed(session, target.workflow, level):
        out.append(_problem(DOWNGRADE_ADMIN, f"「{target.workflow.name}」现在是受管级别，按已发布级别发布会把它降级，"
                                             "需要管理员", source="plan"))
    # 工作流发布前检查（和发布前检查接口同一套口径，带修复的种类）
    checked = await check_graph(session, target.graph, level)
    kinds = {f.get("id"): f.get("kind") for f in checked.get("fixes") or []}
    for issue in checked.get("issues") or []:
        out.append({**issue, "fix_kind": kinds.get(issue.get("fix")), "source": "workflow"})
    workflow_id = target.workflow.id if target.workflow is not None else _PENDING_WORKFLOW
    # 定时任务：设置本身、输入模板对不对得上这张图的入口字段
    if target.schedule:
        body = {**target.schedule, "name": _schedule_name(target), "workflow_id": workflow_id,
                "run_class": "formal", "enabled": False}
        try:
            values = schedule_values(body)
        except HTTPException as e:
            out.append(_problem(SCHEDULE_INVALID, str(e.detail), source="schedule"))
        else:
            if problem := template_problem(values["input_template"], target.graph, target.name):
                out.append(_problem(SCHEDULE_INVALID, problem, source="schedule"))
    # 应用：按将要生成的那一版（当作已发布）跑应用检查；已有应用的另核对能不能再发布、要不要处置、出单的人能不能管它
    if target.app is not None and target.app.status == "archived":
        out.append(_problem(APP_ARCHIVED, f"应用「{target.app.name}」已归档，不能再发布", source="app"))
    if (refused := await app_forbidden(session, target.app, principal)) is not None:
        out.append(_problem(APP_FORBIDDEN, refused, source="app"))
    spec = formal_spec(_app_draft(target), workflow_id=workflow_id, version=target.version, digest=target.digest,
                       level=level, schedule_id=None)
    issues = await lint_app(session, spec, _draft_bindings(bp, target), previous=target.previous,
                            dispositions=options.get("dispositions") or {},
                            pins=await _simulated_pins(session, spec, target, workflow_id=workflow_id, level=level))
    out += [{**i, "source": "app"} for i in issues]
    # 授权：给的人和组要在名录里（同授权接口）
    _, problem = await grants_problem(session, grant_items(bp))
    if problem:
        out.append(_problem(GRANT_REFUSED, problem, source="grants"))
    # 试运行：没用真数试过、试过之后又改了，照实说（不拦）
    trial = await session.get(Run, bp.last_trial_run_id) if bp.last_trial_run_id else None
    if not is_trial_of(trial, bp) or trial.status != "succeeded":
        out.append(_problem(UNTRIALED, "还没有成功的试运行：发布之前没用真数看过结果", "warning", source="plan"))
    elif graph_hash(trial.graph or {}) != target.digest:
        out.append(_problem(TRIAL_DIFFERS, "发布的内容和最近一次试运行不同：试运行之后又改过", "warning", source="plan"))
    return [_with_who(p) for p in out]


def _with_who(issue: dict[str, Any]) -> dict[str, Any]:
    """问题带上白话（没登记白话的照旧只有技术说法）和归谁处理（you / assistant / admin，摘要按它分三栏）。"""
    from app.plain import who_of, with_plain

    out = with_plain(issue)
    return {**out, "who": (out.get("plain") or {}).get("who") or who_of(issue)}


def _errors(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in problems if p.get("level") == "error"]


async def app_forbidden(session: AsyncSession, app: App | None, principal: Principal) -> str | None:
    """蓝图已经发布成的那个应用，这个人能不能管：发布单要改它的草稿、发布它、整份替换它的授权，和应用编辑器、授权接口
    一样要管理权限（apps/access.app_role）。能管或还没有应用时为 None，不能管交回原因。"""
    from app.apps.access import app_role, at_least

    if app is None or at_least(await app_role(session, principal, app), "manage"):
        return None
    return (f"蓝图已经发布成应用「{app.name}」，你对它没有管理权限：发布单会改它的页面、发布新的一版、替换它的授权，"
            "请应用的管理者或管理员来发布")


# --------------------------------------------------------------------------
# 白话摘要
# --------------------------------------------------------------------------


def _names(items: list[tuple[str, str, str]], role: str) -> str:
    return "、".join("所有人" if kind == "everyone" else f"{name}（组）" if kind == "group" else name
                    for kind, name, r in items if r == role)


def _summary(bp: Blueprint, target: _Target, *, level: str, first_run: bool, options: dict[str, Any],
             problems: list[dict[str, Any]]) -> list[dict[str, str]]:
    """每一步一句「会发生什么」。"""
    from app.apps.access import ROLE_TEXT
    from app.blueprints.trial import RECORDS_TEMPLATE, template_of
    from app.scheduler import cron

    out: list[dict[str, str]] = []

    def say(step: str, text: str) -> None:
        out.append({"step": step, "text": text})

    name = target.name
    if target.reused:
        say("save_version", f"沿用「{name}」已有的 v{target.version}（内容和蓝图现在的图相同），不另存新版本")
    elif target.workflow is None:
        say("save_version", f"把蓝图的图存成工作流「{name}」的 v{target.version}")
    else:
        say("save_version", f"把蓝图的图存成「{name}」的 v{target.version}")
    say("preflight", "发布前把工作流、应用、定时任务、授权名单都检查一遍：有问题就停在这里，什么都不写")
    current = target.workflow.published_version if target.workflow is not None else None
    again = f"；「{name}」现在发布的是 v{current}，这次会把 v{target.version} 立为当前版本" \
        if current and current > target.version else ""
    codes = {p.get("code") for p in problems}
    tried = "；发布的内容和最近一次试运行不同（试运行之后又改过），建议再试一次" if TRIAL_DIFFERS in codes else \
        "；还没有用真数试运行过" if UNTRIALED in codes else ""
    if target.published_as == level:
        say("publish_workflow", f"「{name}」v{target.version} 已经按{LEVEL_TEXT[level]}级别发布过，不重复发布{tried}")
    else:
        say("publish_workflow",
            f"按{LEVEL_TEXT[level]}级别发布「{name}」v{target.version}：此后正式运行只从这一版发起{again}{tried}")
    when = ""
    if target.schedule:
        try:
            when = cron.describe(str(target.schedule.get("cron")), str(target.schedule.get("timezone")))
        except Exception:  # noqa: BLE001 - 说不清的设置预检会报
            when = "按设定的时间"
        if target.schedule_row is not None:
            say("create_schedule", f"沿用应用正在用的定时任务「{target.schedule_row.name}」，时间按蓝图更新：{when}")
        else:
            say("create_schedule", f"建定时任务（先停用，应用发布后再启用）：{when}")
    else:
        say("create_schedule", "不建定时任务：这个应用不按时间自动出数")
    if target.app is not None:
        say("create_app", f"更新应用「{target.app.name}」：沿用它现在的页面（页面上改过的都留着），换成新发布的这一版，"
                          "表单按「填什么」更新；新加的数请在页面上加数据卡片")
    else:
        say("create_app", f"建应用「{bp.name or name}」，页面就是试运行时看到的样子")
    chosen = list((options.get("dispositions") or {}).values())
    if target.previous is not None and chosen:
        say("publish_app", "发布应用。口径变了：" + "；".join(DISPOSITION_TEXT.get(v, v) for v in chosen))
    else:
        say("publish_app", "发布应用：使用者打开的就是这一版")
    if target.schedule:
        say("enable_schedule", f"启用定时任务：此后{when}按这一版自动出数")
    else:
        say("enable_schedule", "没有定时任务要启用")
    items = grant_items(bp)
    parts = [f"{who}可以{ROLE_TEXT[role]}" for role in GRANT_ROLES if (who := _names(items, role))]
    # 填报登记：看板读的是正式运行的封存结果（看得到全部行），汇总是全量的，这是作者的选择，照实写（N8）
    records = "；使用者能看到全部提交的汇总数，逐条记录只看得到自己提交的" if template_of(bp) == RECORDS_TEMPLATE else ""
    if parts:
        say("grant", "；".join(parts) + f"（应用的授权按「谁能看」整份替换）{records}")
    else:
        say("grant", f"「谁能看」里没有人：不改授权，只有管理员和搭建者能打开{records}")
    if first_run:
        say("first_run", "立即运行一次，应用一打开就有数")
    else:
        say("first_run", "不立即运行：流程类应用等有人发起" if target.process else "不立即运行：等定时任务出第一期数")
    return out


# --------------------------------------------------------------------------
# 出单
# --------------------------------------------------------------------------


def _steps(target: _Target, *, first_run: bool) -> list[dict[str, Any]]:
    skipped = set()
    if not target.schedule:
        skipped |= {"create_schedule", "enable_schedule"}
    if not first_run:
        skipped.add("first_run")
    return [{"kind": k, "status": "skipped" if k in skipped else "pending", "result": None, "error_code": None,
             "message": None, "at": None} for k in STEP_KINDS]


async def _instantiate(session: AsyncSession, blueprint: Blueprint, principal: Principal) -> None:
    """出单前按卡片现在的样子重新实例化（试运行时也是这一步）：图、关节存到蓝图上，应用草稿和定时任务参数记进留痕，
    _target 取的就是这一份。卡片没改过的，图和哈希都不变（实例化是确定的）。"""
    from app.blueprints.trial import TrialRefused, materialize

    try:
        await materialize(session, blueprint, principal=principal)
    except TrialRefused as e:
        raise PlanRefused(e.status_code, e.message, NOT_INSTANTIATED if e.status_code == 409 else e.code) from e


async def plan(session: AsyncSession, blueprint: Blueprint, *, level: str, principal: Principal,
               options: dict[str, Any] | None = None) -> PublishPlan:
    """出发布单（按卡片重新实例化，再只读预检），交回 PublishPlan（已落库，status=planned）。options：first_run（不给时
    看板类为真、流程类为假）、dispositions（钉住项升版时的处置，{别名: recompute|dual|incomparable}）；另记 spec_hash
    （卡片内容的指纹，执行前核对）。"""
    from app.apps.audit import record
    from app.blueprints.store import spec_digest

    if level not in LEVELS:
        raise PlanRefused(422, "发布级别只能是「已发布」或「受管」")
    if blueprint.scope != "shared":
        raise PlanRefused(409, "个人蓝图不走发布单：存成你的应用自己用，想给别人用请提交审核", PLAN_STATE)
    # 实例化挡路的问题（缺项、没填的关节、画布校验）预检里都有（蓝图检查、发布前检查），这里不另列：执行时的预检同一份
    await _instantiate(session, blueprint, principal)
    target = await _target(session, blueprint)
    options = dict(options or {})
    first_run = bool(options["first_run"]) if isinstance(options.get("first_run"), bool) else not target.process
    options["first_run"] = first_run
    options["spec_hash"] = spec_digest(blueprint)
    problems = await _preflight(session, blueprint, target, level=level, principal=principal, options=options)
    row = PublishPlan(blueprint_id=blueprint.id, actor=principal.username, level=level, graph_hash=target.digest,
                      options=options, steps=_steps(target, first_run=first_run),
                      summary=_summary(blueprint, target, level=level, first_run=first_run, options=options,
                                       problems=problems),
                      problems=problems, status="planned")
    session.add(row)
    await session.flush()
    await record(session, action="publish_plan.plan", subject_kind="publish_plan", subject_id=row.id,
                 actor=principal.username, data={"blueprint_id": blueprint.id, "level": level,
                                                 "graph_hash": target.digest, "errors": len(_errors(problems))})
    await session.commit()
    await session.refresh(row)
    return row


def plan_out(row: PublishPlan) -> dict[str, Any]:
    """接口形状（frontend/src/typesBlueprint.ts 的 PublishPlanRow），另带 ok（没有挡路的问题）。"""
    return {"id": row.id, "blueprint_id": row.blueprint_id, "level": row.level, "graph_hash": row.graph_hash,
            "status": row.status, "steps": row.steps or [], "summary": row.summary or [],
            "problems": row.problems or [], "options": row.options or {},
            "ok": not _errors(row.problems or []) and row.status != "stopped",
            "actor": row.actor,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


async def get_plan(session: AsyncSession, plan_id: str) -> PublishPlan:
    row = await session.get(PublishPlan, plan_id)
    if row is None:
        raise PlanRefused(404, PLAN_GONE)
    return row


async def run(session: AsyncSession, plan_id: str, *, principal: Principal) -> PublishPlan:
    """按序执行；某步失败时 status=stopped 并写明原因。只有刚出的单（planned）能执行。"""
    return await _start(session, plan_id, principal=principal, want="planned", action="publish_plan.start")


async def resume(session: AsyncSession, plan_id: str, *, principal: Principal) -> PublishPlan:
    """从停下的那一步接着做；已完成的步骤按结果里的 id 核对，不重做。只有停下的单（stopped）能接着做。"""
    return await _start(session, plan_id, principal=principal, want="stopped", action="publish_plan.resume")


async def _start(session: AsyncSession, plan_id: str, *, principal: Principal, want: str, action: str) -> PublishPlan:
    from app.apps.audit import record

    row = await get_plan(session, plan_id)
    bp = await session.get(Blueprint, row.blueprint_id)
    if bp is None:
        raise PlanRefused(404, "发布单对应的蓝图不存在，可能已被删除")
    from app.blueprints.store import spec_digest

    if (bp.graph_hash or "") != (row.graph_hash or ""):
        raise PlanRefused(409, "出单之后蓝图又改过：要发布的已经不是这份发布单看过的那张图，请重新出一份发布单",
                          GRAPH_CHANGED)
    if (row.options or {}).get("spec_hash") and row.options["spec_hash"] != spec_digest(bp):
        raise PlanRefused(409, "出单之后卡片又改过：摘要里写的已经不是现在要发布的，请重新出一份发布单", GRAPH_CHANGED)
    app = await session.get(App, bp.app_id) if bp.app_id else None
    if (refused := await app_forbidden(session, app, principal)) is not None:
        raise PlanRefused(403, refused, APP_FORBIDDEN)
    if row.status != want:
        state = {"planned": "还没开始执行", "running": "正在执行", "stopped": "停在了某一步", "done": "已经做完"}
        hint = "，请用「接着做」" if row.status == "stopped" else ""
        raise PlanRefused(409, f"这份发布单{state.get(row.status, row.status)}{hint}", PLAN_STATE)
    if any(s.get("error_code") == OBJECT_MISSING for s in row.steps or []):
        # 前面建好的东西不见了：接着做会把后面几步接到一个对不上的状态上，只能重新出单（新单按现在的样子重新算）
        raise PlanRefused(409, "这份发布单前面建好的东西不见了，不能接着做：请重新出一份发布单", OBJECT_MISSING)
    # 条件更新抢到执行权：两个人同时点「确认发布」，只有先到的那个做
    claimed = await session.execute(update(PublishPlan).where(PublishPlan.id == row.id, PublishPlan.status == want)
                                    .values(status="running").execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        await session.rollback()
        raise PlanRefused(409, "这份发布单刚刚被别人开始执行了，请刷新后查看进度", PLAN_STATE)
    await record(session, action=action, subject_kind="publish_plan", subject_id=row.id, actor=principal.username,
                 data={"blueprint_id": bp.id, "level": row.level, "graph_hash": row.graph_hash})
    await session.commit()
    await session.refresh(row)
    await _drive(session, row, bp, principal)
    await session.refresh(row)
    return row


def _set_step(row: PublishPlan, kind: str, **values: Any) -> None:
    steps = copy.deepcopy(row.steps or [])
    for step in steps:
        if step.get("kind") == kind:
            step.update(values, at=utcnow().isoformat())
    row.steps = steps


def _step(row: PublishPlan, kind: str) -> dict[str, Any]:
    return next((s for s in row.steps or [] if s.get("kind") == kind), {"kind": kind, "status": "pending"})


async def _drive(session: AsyncSession, row: PublishPlan, bp: Blueprint, principal: Principal) -> None:
    from app.apps.audit import record

    ctx = _Run(row=row, bp=bp, principal=principal, actor=principal.username,
               options=dict(row.options or {}), level=row.level)
    for kind in STEP_KINDS:
        step = _step(row, kind)
        if step.get("status") == "skipped":
            continue
        try:
            if step.get("status") == "done":
                await _verify(session, ctx, kind, step.get("result") or {})
                ctx.results[kind] = step.get("result") or {}
                continue
            if kind == "save_version" and _step(row, "preflight").get("status") != "done":
                # 预检在保存版本之前：挡住了就什么都不写（连版本都不存）
                await _execute(session, ctx, "preflight")
                ctx.prechecked = True
            result = await _execute(session, ctx, kind)
        except StepFailed as e:
            await _reset(session, row, bp)
            failed = "preflight" if kind == "save_version" and not ctx.prechecked else kind
            _set_step(row, failed, status="failed", error_code=e.code, message=_where_we_stand(ctx, failed, e.message))
            if e.problems:
                row.problems = e.problems
            row.status = "stopped"
            await record(session, action="publish_plan.stop", subject_kind="publish_plan", subject_id=row.id,
                         actor=ctx.actor, data={"step": failed, "code": e.code, "message": e.message})
            await session.commit()
            return
        except Exception as e:  # noqa: BLE001 - 意外的错照实停在这一步，接着做时从这里开始
            logger.exception("发布单 %s 的「%s」这一步出错", row.id, kind)
            await _reset(session, row, bp)
            _set_step(row, kind, status="failed", error_code=STEP_FAILED, message=_where_we_stand(
                ctx, kind, f"这一步出了意外的错（{type(e).__name__}），后面的步骤都没有做"))
            row.status = "stopped"
            await record(session, action="publish_plan.stop", subject_kind="publish_plan", subject_id=row.id,
                         actor=ctx.actor, data={"step": kind, "code": STEP_FAILED, "error": type(e).__name__})
            await session.commit()
            return
        ctx.results[kind] = result
        _set_step(row, kind, status="done", result=result, error_code=None, message=None)
        await record(session, action="publish_plan.step", subject_kind="publish_plan", subject_id=row.id,
                     actor=ctx.actor, data={"step": kind, "result": result})
        await session.commit()
    row.status = "done"
    bp.status = "published"
    app_id = (ctx.results.get("create_app") or {}).get("app_id")
    await record(session, action="publish_plan.done", subject_kind="publish_plan", subject_id=row.id, actor=ctx.actor,
                 data={"blueprint_id": bp.id, "results": ctx.results})
    await session.commit()
    if bp.copied_from and app_id:
        # 个人应用的审核副本：原蓝图的审核待办记为已发布、通知提交人（blueprints/personal.py）
        from app.blueprints.personal import review_published

        try:
            await review_published(session, bp, app_id=app_id, principal=principal)
        except Exception:  # noqa: BLE001 - 发布已经做完，不因此回滚；拥有者打开个人应用、处理人打开待办时会补上
            # （blueprints/personal.settle_review），审核待办不会一直挂着
            logger.exception("发布单 %s 做完后没能结束审核待办，等下次有人打开时补上", row.id)
            await session.rollback()


#: 停在这几步时工作流已经发布、应用还没有（发布撤不回，照实说）；停在这几步时应用已经发布好了
_PUBLISHED_NO_APP = ("create_schedule", "create_app", "publish_app")
_APP_PUBLISHED = ("enable_schedule", "grant", "first_run")


def _where_we_stand(ctx: _Run, kind: str, message: str) -> str:
    """停下时的白话原因前面说清现在的状态：工作流已发布、应用还没有；或者应用已经发布好了（补偿：发布撤不回，P1）。"""
    if kind in _PUBLISHED_NO_APP and "publish_workflow" in ctx.results and not message.startswith("工作流已经发布"):
        return f"工作流已经发布，应用还没有：{message}"
    if kind in _APP_PUBLISHED and not message.startswith("应用已经发布"):
        return f"应用已经发布好了，这一步没做完：{message}"
    return message


async def _reset(session: AsyncSession, row: PublishPlan, bp: Blueprint) -> None:
    """一步没做成：丢掉这一步没提交的改动，重新读发布单和蓝图（回滚之后它们的属性过期了，异步会话里不能隐式重读）。"""
    await session.rollback()
    await session.refresh(row)
    await session.refresh(bp)


@dataclass
class _Run:
    row: PublishPlan
    bp: Blueprint
    principal: Principal
    actor: str | None
    options: dict[str, Any]
    level: str
    results: dict[str, Any] = field(default_factory=dict)
    prechecked: bool = False
    target: _Target | None = None


async def _execute(session: AsyncSession, ctx: _Run, kind: str) -> dict[str, Any]:
    return await _STEPS[kind](session, ctx)


async def _fresh_target(session: AsyncSession, ctx: _Run) -> _Target:
    ctx.target = await _target(session, ctx.bp)
    return ctx.target


async def _do_preflight(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    if ctx.prechecked and ctx.target is not None:
        return {"errors": 0, "warnings": len([p for p in ctx.row.problems or [] if p.get("level") == "warning"])}
    target = await _fresh_target(session, ctx)
    problems = await _preflight(session, ctx.bp, target, level=ctx.level, principal=ctx.principal, options=ctx.options)
    ctx.row.problems = problems
    if errors := _errors(problems):
        raise StepFailed(PREFLIGHT, f"发布前检查有 {len(errors)} 个问题没解决，什么都没写：请按问题处理，再接着做", problems)
    return {"errors": 0, "warnings": len(problems)}


async def _do_save_version(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    """按图哈希判断：已有相同的版本就复用；没有就存一版（同画布保存：版本号加一、画布状态退回草稿）。"""
    from app.blueprints.trial import template_of

    bp = ctx.bp
    target = ctx.target or await _fresh_target(session, ctx)
    if target.reused and target.workflow is not None:
        return {"workflow_id": target.workflow.id, "version": target.version, "graph_hash": target.digest,
                "reused": True}
    workflow = target.workflow
    purpose = ((bp.spec or {}).get("purpose") or {}).get("value")
    ref = (bp.spec or {}).get("template") or {}
    note = f"出自蓝图「{bp.name}」"
    if workflow is None:
        source = bp.source or {}
        workflow = Workflow(name=bp.name or "未命名应用", description=str(purpose) if isinstance(purpose, str) else "",
                            graph=target.graph, tags=["blueprint"], status="draft",
                            origin={"blueprint_id": bp.id, "template": f"{template_of(bp)}@{ref.get('version') or 1}",
                                    **({"run_id": source.get("id")} if source.get("kind") == "run" else {})})
        session.add(workflow)
        await session.flush()
        version = 1
    else:
        workflow.graph = target.graph
        workflow.version += 1
        version = workflow.version
        if workflow.status in ("published", "governed"):
            workflow.status = "draft"
    session.add(WorkflowVersion(workflow_id=workflow.id, version=version, graph=target.graph,
                                graph_hash=target.digest, note=note))
    bp.workflow_id = workflow.id
    await session.commit()
    await session.refresh(workflow)
    target.workflow, target.version, target.reused = workflow, version, True
    return {"workflow_id": workflow.id, "version": version, "graph_hash": target.digest, "reused": False}


async def _version_row(session: AsyncSession, ctx: _Run) -> tuple[Workflow, WorkflowVersion]:
    saved = ctx.results.get("save_version") or {}
    workflow = await session.get(Workflow, saved.get("workflow_id") or "")
    row = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == saved.get("workflow_id"),
        WorkflowVersion.version == saved.get("version")))).scalar_one_or_none() if workflow else None
    if workflow is None or row is None:
        raise StepFailed(OBJECT_MISSING, "保存过的工作流版本不见了（可能被删除），请重新出一份发布单")
    return workflow, row


async def _do_publish_workflow(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    from app.api.workflows import commit_publish, downgrades_governed, gate_issues
    from app.apps.versions import is_published

    workflow, snapshot = await _version_row(session, ctx)
    if ctx.level == "governed" and not ctx.principal.at_least("admin"):
        raise StepFailed(ADMIN_REQUIRED, "以受管级别发布需要管理员")
    if is_published(snapshot, current=workflow.published_version) and snapshot.level == ctx.level:
        return {"workflow_id": workflow.id, "version": snapshot.version, "level": ctx.level, "already": True}
    if not ctx.principal.at_least("admin") and await downgrades_governed(session, workflow, ctx.level):
        raise StepFailed(DOWNGRADE_ADMIN, f"「{workflow.name}」现在是受管级别，降为已发布需要管理员")
    issues = await gate_issues(session, snapshot.graph, ctx.level)
    if issues is None or any(i["level"] == "error" for i in issues):
        problems = [_with_who({**i, "source": "workflow"}) for i in issues or []]
        raise StepFailed(WORKFLOW_PUBLISH, "工作流没有通过发布检查，还没有发布", problems)
    out = await commit_publish(session, workflow, snapshot, ctx.level, ctx.actor)
    return {"workflow_id": workflow.id, "version": out["version"], "level": out["level"]}


async def _do_create_schedule(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    """建定时任务，先停用：应用发布之后才启用，不会在没有应用的时候产生无主的正式运行。"""
    from app.api.schedules import create_schedule_row, retime

    target = ctx.target or await _fresh_target(session, ctx)
    workflow, _ = await _version_row(session, ctx)
    body = {**(target.schedule or {}), "name": _schedule_name(target), "workflow_id": workflow.id,
            "run_class": "formal", "enabled": False}
    try:
        if target.schedule_row is not None:
            row = await retime(session, target.schedule_row, body)
            return {"schedule_id": row.id, "enabled": row.enabled, "reused": True}
        row = await create_schedule_row(session, body, actor=ctx.actor)
    except HTTPException as e:
        await session.rollback()
        raise StepFailed(SCHEDULE_INVALID, str(e.detail)) from e
    return {"schedule_id": row.id, "enabled": False}


async def _do_create_app(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    """建应用（或更新蓝图已有的那个应用的草稿）：钉住项换成刚发布的那一版，看板视图绑上定时任务，岗位按「谁来做」绑上。"""
    from app.api.apps import new_app

    bp = ctx.bp
    target = ctx.target or await _fresh_target(session, ctx)
    _, snapshot = await _version_row(session, ctx)
    spec = formal_spec(_app_draft(target), workflow_id=snapshot.workflow_id, version=snapshot.version,
                       digest=target.digest, level=ctx.level,
                       schedule_id=(ctx.results.get("create_schedule") or {}).get("schedule_id"))
    bindings = _draft_bindings(bp, target)
    if target.app is not None:
        if target.app.status == "archived":
            raise StepFailed(APP_ARCHIVED, f"应用「{target.app.name}」已归档，不能再发布")
        target.app.draft, target.app.draft_bindings = spec, bindings
        target.app.blueprint_id = bp.id
        target.app.updated_by = ctx.actor
        await session.commit()
        return {"app_id": target.app.id, "created": False}
    purpose = ((bp.spec or {}).get("purpose") or {}).get("value")
    app = await new_app(session, name=bp.name or target.name,
                        description=str(purpose) if isinstance(purpose, str) else "",
                        spec=spec, bindings=bindings, actor=ctx.actor, blueprint_id=bp.id)
    bp.app_id = app.id
    target.app = app
    await session.commit()
    return {"app_id": app.id, "created": True}


def records_refs(graph: dict[str, Any]) -> list[str]:
    """图里写记录表的那几个调用工具节点写的表（args.table，「键.v版本」）。"""
    from app.records import RECORDS_APPEND

    out: list[str] = []
    for node in graph.get("nodes") or []:
        config = ((node or {}).get("data") or {}).get("config") or {}
        if (node or {}).get("type") == "tool" and config.get("tool") == RECORDS_APPEND:
            ref = str((config.get("args") or {}).get("table") or "").strip()
            if ref and ref not in out:
                out.append(ref)
    return out


async def _switch_records(session: AsyncSession, bp: Blueprint, app: App, graph: dict[str, Any]) -> list[str]:
    """填报登记：图里写的那一版记录表成为往里写的那一版（records/tables.publish_table），线上原来那一版这时才停用。
    只 flush：和应用的新一版一起提交（publish_draft），应用没发布成就一起回滚，线上照常写旧的那一版。"""
    from app.blueprints.trial import RECORDS_TEMPLATE, template_of
    from app.records.store import RecordsRefused
    from app.records.tables import columns_from_fields, publish_table

    if template_of(bp) != RECORDS_TEMPLATE:
        return []
    out = []
    for ref in records_refs(graph):
        try:
            row = await publish_table(session, app, ref, columns_from_fields((bp.spec or {}).get("fields") or []))
        except RecordsRefused as e:
            raise StepFailed(e.code, f"工作流已经发布，应用还没有：{e.message}") from e
        out.append(f"{row.table_key}.v{row.version}")
    return out


async def _the_app(session: AsyncSession, ctx: _Run) -> App:
    app = await session.get(App, (ctx.results.get("create_app") or {}).get("app_id") or "")
    if app is None:
        raise StepFailed(OBJECT_MISSING, "建好的应用不见了（可能被删除），请重新出一份发布单")
    return app


async def _do_publish_app(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    from app.api.apps import publish_draft

    app = await _the_app(session, ctx)
    if app.status == "archived":
        raise StepFailed(APP_ARCHIVED, f"应用「{app.name}」已归档，不能再发布")
    target = ctx.target or await _fresh_target(session, ctx)
    tables = await _switch_records(session, ctx.bp, app, target.graph)
    try:
        out = await publish_draft(session, app, note=f"发布单 {ctx.row.id[:8]}",
                                  dispositions=ctx.options.get("dispositions") or {}, actor=ctx.actor)
    except HTTPException as e:
        await session.rollback()
        raise StepFailed(APP_PUBLISH, f"工作流已经发布，应用还没有：{e.detail}") from e
    if not out.get("ok"):
        problems = [_with_who({**i, "source": "app"}) for i in out.get("issues") or []]
        raise StepFailed(APP_PUBLISH, "工作流已经发布，应用没有通过检查、还没有发布；使用者暂时看不到它", problems)
    return {"app_id": app.id, "version": out["version"], **({"records": tables} if tables else {})}


async def _the_schedule(session: AsyncSession, ctx: _Run) -> Schedule:
    row = await session.get(Schedule, (ctx.results.get("create_schedule") or {}).get("schedule_id") or "")
    if row is None:
        raise StepFailed(OBJECT_MISSING, "建好的定时任务不见了（可能被删除），请重新出一份发布单")
    return row


async def _do_enable_schedule(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    from app.api.schedules import set_enabled

    row = await set_enabled(session, await _the_schedule(session, ctx), True)
    return {"schedule_id": row.id, "enabled": True,
            "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None}


async def _do_grant(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    """按「谁能看」整份替换应用的授权（名录核对同授权接口）。卡片上没有人的不改授权（已有应用的授权留着）。"""
    from app.api.apps import grants_problem, replace_grants

    app = await _the_app(session, ctx)
    wanted, problem = await grants_problem(session, grant_items(ctx.bp))
    if problem:
        raise StepFailed(GRANT_REFUSED, f"应用已经发布，授权没有改：{problem}")
    if not wanted:
        return {"app_id": app.id, "grants": None}
    after = await replace_grants(session, app, wanted, actor=ctx.actor)
    return {"app_id": app.id, "grants": len(after)}


async def _do_first_run(session: AsyncSession, ctx: _Run) -> dict[str, Any]:
    """立即运行一次：有定时任务的按它触发一次（应用的视图只看它出的数），没有的从这一版发起一次正式运行。"""
    from app.engine.launch import LaunchRefused, launch_run
    from app.scheduler import service

    schedule_id = (ctx.results.get("create_schedule") or {}).get("schedule_id")
    if schedule_id:
        fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0), "manual", actor=ctx.actor)
        if fire is None or not fire.run_id:
            reason = (fire.reason if fire is not None else None) or "定时任务没能发起这次运行"
            raise StepFailed(FIRST_RUN, f"应用已经发布好了，第一次运行没能发起：{reason}")
        return {"run_id": fire.run_id, "schedule_id": schedule_id}
    _, snapshot = await _version_row(session, ctx)
    try:
        run = await launch_run(session, workflow_id=snapshot.workflow_id, run_class="formal", version=snapshot.version,
                               expect_hash=snapshot.graph_hash, actor=ctx.actor, origin_kind="launcher",
                               origin={"publish_plan": ctx.row.id})
    except LaunchRefused as e:
        raise StepFailed(FIRST_RUN, f"应用已经发布好了，第一次运行没能发起：{e.message}") from e
    return {"run_id": run.id}


_STEPS = {
    "preflight": _do_preflight, "save_version": _do_save_version, "publish_workflow": _do_publish_workflow,
    "create_schedule": _do_create_schedule, "create_app": _do_create_app, "publish_app": _do_publish_app,
    "enable_schedule": _do_enable_schedule, "grant": _do_grant, "first_run": _do_first_run,
}


# --------------------------------------------------------------------------
# 启动整理：上次进程没了时还在执行的单
# --------------------------------------------------------------------------


async def startup() -> int:
    """上次进程没了时还在执行的发布单（status=running，执行是在请求里做的，进程一没就再没有人接着推）：改成停下，停在第一个
    还没做完的步骤上、写明执行中断，这样就能「接着做」。那一步可能已经做了一半：接着做时按图哈希复用版本、按应用 id 更新
    同一个应用，不会另建一份工作流版本或应用。交回整理了几份。"""
    from app.apps.audit import record
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        rows = list((await session.execute(select(PublishPlan).where(PublishPlan.status == "running"))).scalars())
        for row in rows:
            kind = next((st.get("kind") for st in row.steps or [] if st.get("status") not in ("done", "skipped")), None)
            if kind:
                _set_step(row, kind, status="failed", error_code=INTERRUPTED,
                          message="执行到这一步时服务重启了，这一步可能只做了一半：请核对后接着做")
            row.status = "stopped"
            await record(session, action="publish_plan.stop", subject_kind="publish_plan", subject_id=row.id,
                         actor=None, data={"step": kind, "code": INTERRUPTED})
        await session.commit()
    return len(rows)


# --------------------------------------------------------------------------
# 接着做：已完成的步骤按结果里的 id 核对
# --------------------------------------------------------------------------


async def _verify(session: AsyncSession, ctx: _Run, kind: str, result: dict[str, Any]) -> None:
    """已完成的这一步做出来的东西还在、还是那个状态；不在了就停下（plan.object_missing），不重做、不另建一份。"""
    from app.apps.versions import is_published

    ctx.results[kind] = result
    if kind == "save_version":
        workflow, row = await _version_row(session, ctx)
        if row.graph_hash != ctx.row.graph_hash:
            raise StepFailed(OBJECT_MISSING, f"「{workflow.name}」v{row.version} 的内容和发布单针对的图不一致，请重新出单")
        ctx.target = await _fresh_target(session, ctx)
    elif kind == "preflight":
        ctx.prechecked = True
    elif kind == "publish_workflow":
        workflow, row = await _version_row(session, ctx)
        if not is_published(row, current=workflow.published_version):
            raise StepFailed(OBJECT_MISSING, f"「{workflow.name}」v{row.version} 已经不是发布状态（可能已退役），请重新出单")
    elif kind in ("create_schedule", "enable_schedule"):
        await _the_schedule(session, ctx)
    elif kind == "create_app":
        app = await _the_app(session, ctx)
        if ctx.target is not None:
            ctx.target.app = app
    elif kind == "publish_app":
        app = await _the_app(session, ctx)
        if (app.published_version or 0) < int(result.get("version") or 0):
            raise StepFailed(OBJECT_MISSING, f"应用「{app.name}」的发布版本对不上发布单记下的，请重新出单")
