"""试运行（N4、§3.5）：跑一次还没发布的图（探索运行，origin_kind=trial），草稿应用对着这次运行解析
（apps/resolve.resolve_page 的 trial 旁路），印章写「试运行」。流程类的人工步骤派给试运行的人自己，写入走测试环境或演练，
调度不对 trial 的待办做超时升级（process/tasks.py）。

- 实例化（materialize）：模板按卡片出图、关节、应用草稿、定时任务参数（blueprints/instantiate，纯函数），图和关节存到
  蓝图上，应用草稿和定时任务参数记进这一次的留痕（blueprint_events 的 instantiate）——发布单建应用时用的就是试运行时看到的
  那一份（P1：看到的就是发布的）。已经填好的生成关节（蓝图 joints 上存的值，依据没变的才算，instantiate.filled_from）交回
  模板，改了一张卡片不会把助手填的 SQL、指标式丢掉。「自由形」（从运行、工作流版本导入）没有关节，图原样存。
- 试运行（start_trial）：先实例化，蓝图检查（缺项）、实例化交回的问题有 error 的不试；以发起人的身份发起探索运行，看板类的
  输入按「多久更新」算出最近一期（同定时任务到点时的取值），用户给的覆盖它；流程类填了示例表单的，和应用入口提交同一条路
  （表单校验、文件核对后绑到这次运行、按入口映射出输入，entry_inputs）。开登录后的使用者（个人蓝图）照问数据的约束：
  图只能用放行的节点和工具，结果遮罩、只读。
- 预览（preview_page）：按这次试运行解析草稿应用的一页。只认这个蓝图自己的试运行。
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.db.models import Run
from app.db.models_blueprints import Blueprint, BlueprintEvent

logger = logging.getLogger(__name__)

#: 试运行的运行来源（engine/origin.py）
ORIGIN = "trial"
#: 没有关节、图原样导入的模板
FREEFORM = "freeform"
#: 机器码（接口回 {detail, code}；白话在 app/plain.py）
NOT_READY = "trial_not_ready"
NO_GRAPH = "trial_no_graph"
NO_RUN = "trial_no_run"
POLICY = "trial_policy"
FORM = "trial_form"

NO_GRAPH_TEXT = "蓝图还没有可以运行的图：请先选场景、把卡片补齐"
NOT_READY_TEXT = "蓝图还有没补齐的地方，补齐之后才能用真数试运行"
NO_RUN_TEXT = "这个蓝图还没有试运行过，或者这次运行不是它的试运行"


class TrialRefused(Exception):
    """不试运行 / 不预览：status_code 照 HTTP 语义，message 是给人看的一句话，code 是机器码，issues 是挡住它的问题
    （蓝图检查、实例化交回的，带白话）。"""

    def __init__(self, status_code: int, message: str, code: str | None = None,
                 issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.issues = issues or []


@dataclass
class Materialized:
    """实例化的结果（已存到蓝图上）：图和哈希、关节状态、应用草稿（钉住项没有版本号）、定时任务参数、问题。"""

    graph: dict[str, Any]
    graph_hash: str
    joints: dict[str, Any]
    app_spec: dict[str, Any]
    schedule: dict[str, Any] | None
    problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(p.get("level") == "error" for p in self.problems)


# --------------------------------------------------------------------------
# 留痕
# --------------------------------------------------------------------------


async def record_event(session: AsyncSession, bp: Blueprint, op: str, data: dict[str, Any], *,
                       principal: Principal | None) -> None:
    """蓝图的一条留痕（blueprints/store.record_event，同一个蓝图里 seq 递增），记谁、核实过没有。调用方提交。"""
    from app.blueprints.store import record_event as record

    await record(session, bp, op=op, data=data, actor=principal.username if principal else None,
                 verified=bool(principal and principal.verified))


async def last_event(session: AsyncSession, bp: Blueprint, op: str, **match: Any) -> BlueprintEvent | None:
    """这个蓝图最近一条某种留痕；match 给了只要 data 里这几项对得上的。"""
    rows = (await session.execute(select(BlueprintEvent).where(
        BlueprintEvent.blueprint_id == bp.id, BlueprintEvent.op == op)
        .order_by(BlueprintEvent.seq.desc()).limit(50))).scalars()
    for row in rows:
        data = row.data or {}
        if all(data.get(k) == v for k, v in match.items()):
            return row
    return None


# --------------------------------------------------------------------------
# 实例化
# --------------------------------------------------------------------------


def template_of(bp: Blueprint) -> str:
    """蓝图用的模板：卡片上选的那个（从运行、工作流版本导入的记「自由形」）；还没选的是空串。"""
    ref = (bp.spec or {}).get("template")
    if isinstance(ref, dict) and ref.get("id"):
        return str(ref["id"])
    return bp.template_id or ""


#: 填报登记的模板
RECORDS_TEMPLATE = "records"


async def materialize(session: AsyncSession, bp: Blueprint, *, principal: Principal | None) -> Materialized:
    """按模板实例化，把图、图哈希、关节存到蓝图上，记一条 instantiate 留痕（带应用草稿、定时任务参数），提交。
    交回的问题是实例化交回的（缺项、待填的关节、骨架改动、画布校验的 error）加上蓝图检查的提醒，带白话；挡不挡由调用方定。

    自由形导入的图原样存（不重新排版）：它的哈希要和导入的那一版对得上，发布单才按哈希复用那一版，不另存一版。"""
    from app.blueprints.instantiate import filled_from, instantiate
    from app.blueprints.ops import blueprint_issues
    from app.core.artifact_store import graph_hash
    from app.plain import with_plain

    from app.blueprints.store import instantiate_context
    from app.records.store import RecordsRefused

    template = template_of(bp)
    if template == FREEFORM and not (bp.graph or {}).get("nodes"):
        raise TrialRefused(409, NO_GRAPH_TEXT, NO_GRAPH)
    # 上下文和卡片修改、填关节、发布单同一份（blueprints/store.instantiate_context）：填报登记的记录表按「填什么」取或建
    # 起草这边的那一版（草稿不影响线上），图里写「键.v版本」
    try:
        ctx = await instantiate_context(session, bp, create_records=True)
    except RecordsRefused as e:
        await session.rollback()
        raise TrialRefused(422, e.message, e.code) from e
    filled = filled_from(bp.spec or {}, bp.joints or {}, graph=ctx.get("graph"), records=ctx.get("records"))
    result = instantiate(bp.spec or {}, filled=filled, **ctx)
    graph = bp.graph if template == FREEFORM else result.graph
    if not (graph or {}).get("nodes") and not any(p.get("level") == "error" for p in result.problems):
        raise TrialRefused(409, NO_GRAPH_TEXT, NO_GRAPH)
    digest = graph_hash(graph or {})
    app_spec = _draft_pins(result.app_spec, bp, digest)
    # 实例化交回的是挡路的 error；蓝图检查里的提醒（模板版本旧了之类）另外带上
    warnings = [i for i in blueprint_issues(bp.spec or {}, for_="trial") if i.get("level") != "error"]
    problems = [with_plain(p) for p in [*result.problems, *warnings]]
    bp.graph, bp.graph_hash, bp.joints = graph, digest, result.joints
    bp.updated_by = principal.username if principal else bp.updated_by
    await record_event(session, bp, "instantiate", {
        "graph_hash": digest, "template": template, "app_spec": app_spec, "schedule": result.schedule,
        "joints": result.joints, "problems": problems}, principal=principal)
    await session.commit()
    await session.refresh(bp)
    return Materialized(graph=graph, graph_hash=digest, joints=result.joints, app_spec=app_spec,
                        schedule=result.schedule, problems=problems)


def _draft_pins(app_spec: dict[str, Any], bp: Blueprint, digest: str) -> dict[str, Any]:
    """草稿的钉住项：没有版本号、哈希是这张图的；有工作流的写工作流，还没有的写蓝图（apps/spec.Pin 二者写一个）。"""
    spec = copy.deepcopy(app_spec)
    pins = []
    for pin in spec.get("pins") or []:
        pin = {k: v for k, v in pin.items() if k not in ("workflow_id", "blueprint")}
        pin.update(version=None, graph_hash=digest, level=None)
        pin.update({"workflow_id": bp.workflow_id} if bp.workflow_id else {"blueprint": bp.id})
        pins.append(pin)
    spec["pins"] = pins
    return spec


async def draft_app_spec(session: AsyncSession, bp: Blueprint, graph: dict[str, Any]) -> dict[str, Any]:
    """按派生规则从这张图出应用草稿：预览找不到试运行时记下的那一份时用。"""
    from app.apps.derive import derive_from_graph

    purpose = ((bp.spec or {}).get("purpose") or {}).get("value")
    return await derive_from_graph(session, graph, name=bp.name or "未命名应用",
                                   description=str(purpose) if isinstance(purpose, str) else "",
                                   workflow_id=bp.workflow_id, blueprint_id=None if bp.workflow_id else bp.id)


async def app_spec_for(session: AsyncSession, bp: Blueprint, digest: str | None) -> tuple[dict[str, Any] | None,
                                                                                            dict[str, Any] | None]:
    """这张图（按哈希认）实例化时记下的 (应用草稿, 定时任务参数)；没有实例化过这张图为 (None, None)。"""
    event = await last_event(session, bp, "instantiate", graph_hash=digest) if digest else None
    if event is None:
        return None, None
    data = event.data or {}
    return data.get("app_spec"), data.get("schedule")


# --------------------------------------------------------------------------
# 试运行
# --------------------------------------------------------------------------


def trial_inputs(blueprint: Blueprint, inputs: dict[str, Any] | None) -> dict[str, Any]:
    """试运行（和个人应用的刷新，blueprints/personal.refresh_inputs）的输入：看板类按「多久更新」算出最近一期
    （blueprints/instantiate.trial_inputs，和定时任务到点时渲染的是同一份），用户给的覆盖它。流程类填了示例表单的
    不走这里，走 entry_inputs（和应用入口提交同一条路）。"""
    from app.blueprints import instantiate

    try:
        base = instantiate.trial_inputs(blueprint.spec or {})
    except Exception:  # noqa: BLE001 - 卡片还不全、设置不成形（发布单的预检会报）时只用用户给的，缺的输入运行时照实报
        logger.warning("蓝图 %s 算不出试运行的统计期，只用用户给的输入", blueprint.id, exc_info=True)
        base = {}
    return {**base, **{k: v for k, v in (inputs or {}).items() if v not in (None, "")}}


def retry_form(app_spec: dict[str, Any]) -> dict[str, Any] | None:
    """「换一期再试」填的表单（预览的 trial.form，界面照它画，不自己挑）：流程类、填报登记是第一个带表单的入口的示例表单
    （entry_of_draft，填了就走 entry_inputs，和应用入口提交同一条路）；看板类没有入口，是按条件查看的条件表单（视图来源的
    params，就是图的输入，trial_inputs 拿用户给的覆盖按「多久更新」算出的那一期）。都没有为 None（只能原样再试一次）。"""
    from app.apps.tree import resolve_form

    if (found := entry_of_draft(app_spec)) is not None:
        return found[1]
    for view in (app_spec.get("views") or {}).values():
        source = view.get("source") if isinstance(view, dict) else None
        form = resolve_form(app_spec, source.get("params")) if isinstance(source, dict) else None
        if isinstance(form, dict) and (form.get("properties") or {}):
            return form
    return None


def entry_of_draft(app_spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """应用草稿里第一个带表单的入口和它的表单（流程类：「填什么」就是它）；看板类没有入口，为 None。"""
    from app.apps.tree import resolve_form

    for entry in app_spec.get("entries") or []:
        form = resolve_form(app_spec, (entry or {}).get("form")) if isinstance(entry, dict) else None
        if isinstance(form, dict) and (form.get("properties") or {}):
            return entry, form
    return None


async def entry_inputs(blueprint: Blueprint, app_spec: dict[str, Any], inputs: dict[str, Any] | None, *,
                       principal: Principal) -> tuple[dict[str, Any], Any] | None:
    """流程类的示例表单：用户填了入口表单里的字段，就按应用入口提交同一条路换成运行输入（apps/entries）——先按表单校验，
    表单里的文件核对是这个人在这张蓝图上传的、还没用过，占住（发起成功绑到这次运行，file_text 读得到），再按入口映射
    出输入（常量也带上：填报登记的 mode=submit，试运行走的就是填报那条路）。没填表单字段的（看板类、只改统计期的）为
    None，按 trial_inputs。交回 (运行输入, 文件的占位)。"""
    from app.apps.entries import map_inputs
    from app.apps.forms import validate_form
    from app.files import store as files_store

    found = entry_of_draft(app_spec)
    if found is None or not inputs:
        return None
    entry, form = found
    if not any(k in (form.get("properties") or {}) and v not in (None, "") for k, v in inputs.items()):
        return None
    if errors := validate_form(form, inputs):
        first = errors[0]
        label = ((form.get("properties") or {}).get(first.get("path")) or {}).get("title") or first.get("path") or ""
        raise TrialRefused(422, f"示例表单没有通过校验：{label}{'：' if label else ''}{first.get('message')}", FORM)
    try:
        values, claimed = await files_store.claim(form, inputs, principal=principal,
                                                  context={"blueprint_id": blueprint.id})
    except files_store.FileRefused as e:
        raise TrialRefused(e.status, e.message, e.code) from e
    return map_inputs(entry, values, principal), claimed


async def start_trial(session: AsyncSession, blueprint: Blueprint, *, inputs: dict[str, Any] | None,
                      principal: Principal, actor: str | None) -> tuple[Run, Materialized]:
    """实例化 → 保存图到蓝图 → 发起探索运行（origin_kind=trial、origin={blueprint_id}），交回 (运行, 实例化结果)。
    蓝图记 last_trial_run_id，留痕记 trial（带这次用的应用草稿）；运行成功结束后蓝图记「试运行过」（settle）。"""
    from app.api.deps import restricted
    from app.engine.launch import launch_run

    result = await materialize(session, blueprint, principal=principal)
    if result.blocked:
        raise TrialRefused(409, NOT_READY_TEXT, NOT_READY, [p for p in result.problems if p.get("level") == "error"])
    options = None
    memory_scope = None
    if restricted() or blueprint.scope == "personal":
        # 开登录后的使用者，和任何人的个人蓝图：和问数据、个人应用的刷新同一份约束（auth/chat_policy.py，
        # blueprints/personal.policy_problem）。个人蓝图的试运行就是刷新的预演，搭建者自己的个人蓝图也照样遮罩、只读，
        # 试出来的才是存成个人应用以后刷新出来的样子
        from app.auth.chat_policy import chat_memory_scope
        from app.blueprints.personal import policy_problem
        from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

        if problem := policy_problem(result.graph):
            raise TrialRefused(403, problem, POLICY)
        memory_scope = chat_memory_scope(actor)
        options = {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}
    from app.files import store as files_store

    submitted = await entry_inputs(blueprint, result.app_spec, inputs, principal=principal)
    payload, claimed = submitted if submitted is not None else (trial_inputs(blueprint, inputs), None)
    try:
        run = await launch_run(
            session, graph=result.graph, input_payload=payload, run_class="exploratory",
            actor=actor, origin_kind=ORIGIN, origin={"blueprint_id": blueprint.id, "graph_hash": result.graph_hash,
                                                      "actor_verified": principal.verified,
                                                      "auth_mode": principal.mode},
            workflow_name=blueprint.name or None, options=options, memory_scope=memory_scope)
    except BaseException:
        if claimed is not None:
            await files_store.release(claimed)
        raise
    if claimed is not None:
        await files_store.finish(claimed, run.id)
    blueprint.last_trial_run_id = run.id
    await record_event(session, blueprint, "trial", {"run_id": run.id, "graph_hash": result.graph_hash,
                                                      "app_spec": result.app_spec}, principal=principal)
    await session.commit()
    await session.refresh(blueprint)
    return run, result


def is_trial_of(run: Run | None, bp: Blueprint) -> bool:
    return run is not None and run.origin_kind == ORIGIN and (run.origin or {}).get("blueprint_id") == bp.id


async def settle(session: AsyncSession, bp: Blueprint) -> Blueprint:
    """最近一次试运行成功结束了、蓝图还记着「起草中」：改记「试运行过」（读蓝图、预览、出发布单时顺带看一眼，
    不在运行收尾时回调蓝图——引擎不认识蓝图）。"""
    if bp.status != "drafting" or not bp.last_trial_run_id:
        return bp
    status = await session.scalar(select(Run.status).where(Run.id == bp.last_trial_run_id))
    if status == "succeeded":
        bp.status = "trialed"
        await session.commit()
        await session.refresh(bp)
    return bp


# --------------------------------------------------------------------------
# 预览
# --------------------------------------------------------------------------


@dataclass
class PreviewApp:
    """预览用的「应用」：还没有应用（或者有，但预览的是蓝图上的草稿）。resolve_page 要的几样：id、名字、草稿。"""

    id: str
    name: str
    draft: dict[str, Any]
    published_version: int | None = None
    status: str = "active"


async def preview_page(session: AsyncSession, blueprint: Blueprint, *, page_id: str | None, run_id: str | None,
                       principal: Principal) -> dict[str, Any]:
    """按这次试运行解析应用草稿的一页（agentlab.view/1），印章 from=试运行、trial=true。run_id 不给就是最近一次。
    草稿用这次试运行时记下的那一份（之后改了卡片，预览的仍是这次运行对应的样子）。trial 里另给「换一期再试」的表单
    （retry_form）。"""
    from app.apps.resolve import resolve_page
    from app.apps.tree import page_of

    run = await session.get(Run, run_id or blueprint.last_trial_run_id or "")
    if not is_trial_of(run, blueprint):
        raise TrialRefused(404, NO_RUN_TEXT, NO_RUN)
    assert run is not None
    await settle(session, blueprint)
    event = await last_event(session, blueprint, "trial", run_id=run.id)
    spec = (event.data or {}).get("app_spec") if event is not None else None
    if not isinstance(spec, dict):
        spec = await draft_app_spec(session, blueprint, run.graph or {})
    pages = [p for p in spec.get("pages") or [] if isinstance(p, dict) and p.get("id")]
    wanted = page_id or (pages[0]["id"] if pages else "home")
    if page_of(spec, wanted) is None:
        from app.apps.resolve import ViewRefused

        raise ViewRefused(404, f"应用里没有页面「{wanted}」", "app_page_not_found")
    app = PreviewApp(id=blueprint.app_id or blueprint.id, name=blueprint.name, draft=spec)
    page = await resolve_page(session, app, page_id=wanted, principal=principal, role="manage", draft=True,
                              trial=run, actor=principal.username)
    # 「换一期再试」的表单按这次试运行对应的那份草稿给（retry_form）：界面照它画，不自己从骨架里挑
    return {**page, "trial": {**(page.get("trial") or {}), "form": retry_form(spec)}}
