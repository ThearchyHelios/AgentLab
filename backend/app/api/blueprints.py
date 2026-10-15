"""蓝图、试运行、发布单的接口（docs/design/normal-user-apps.md §3.5、§3.6、§3.10）。

权限在接口里判：共享蓝图要 builder（受管级发布单再要 admin），个人蓝图要拥有者且能用个人应用（blueprints/personal.allowed：
开了登录的搭建者及以上随时能，使用者和关登录时看管理员开没开 apps.user_personal_apps）。别人的个人蓝图一律当不存在（404）。

- 起点四个，都落到同一个蓝图（N1）：conversation（空蓝图，另建一段蓝图对话）、template（按模板预置卡片）、run（问数据
  的一轮回答：按 /copilot/from-run 同一个提取规则取出执行路径，模板记「自由形」）、workflow_version（已发布的版本：
  图原样导入，模板记「自由形」，蓝图就挂在那个工作流上）。
- 卡片上的修改（PATCH）走 bp_* 操作（blueprints/store.apply_ops_and_save），来源记成 user。
- 实例化、试运行、预览调 blueprints/trial；发布单调 blueprints/publish_plan。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import Principal, actor_of, current_principal
from app.db.base import get_session
from app.db.models_blueprints import Blueprint

router = APIRouter(prefix="/api", tags=["blueprints"])

#: 机器码
NO_ACCESS = "blueprint_forbidden"
BAD_SOURCE = "blueprint_source_invalid"
ADMIN_ONLY = "admin_only"
#: 没选模板、从已有的东西导入时记的模板
FREEFORM = "freeform"
#: 蓝图对话（Conversation.kind）
CONVERSATION_KIND = "blueprint"
DEFAULT_NAME = "新应用"


# --------------------------------------------------------------------------
# 身份、取蓝图、判权限
# --------------------------------------------------------------------------


def _principal(x_actor: str | None) -> Principal:
    """这次请求的身份。关登录时 username 换成自报的署名（X-Actor），蓝图的拥有者、留痕都按它记（同 api/apps.py）。"""
    principal = current_principal()
    if principal.mode == "off" and principal.username is None:
        return dataclasses.replace(principal, username=actor_of(x_actor))
    return principal


def _require_builder(principal: Principal, what: str = "共享蓝图") -> None:
    if not principal.at_least("builder"):
        raise CodedHTTPException(403, f"{what}需要搭建者权限", NO_ACCESS)


async def _personal_allowed(session: AsyncSession, principal: Principal) -> None:
    from app.blueprints.personal import PersonalRefused, ensure_allowed

    try:
        await ensure_allowed(session, principal)
    except PersonalRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code or NO_ACCESS) from e


async def _blueprint(session: AsyncSession, blueprint_id: str) -> Blueprint:
    """取蓝图并判权限（blueprints/store.load，蓝图接口和蓝图助手同一条规则）：共享的要搭建者；个人的要拥有者（别人的
    个人蓝图当不存在）且能用个人应用。"""
    from app.blueprints.store import load

    return await load(session, blueprint_id)


def blueprint_out(bp: Blueprint) -> dict[str, Any]:
    """接口形状（frontend/src/typesBlueprint.ts 的 Blueprint，blueprints/store.blueprint_out）：图本身不下发（只给哈希），
    关节只给状态，问题带白话。"""
    from app.blueprints.store import blueprint_out as shaped

    return shaped(bp)


def _refused(e: Any) -> HTTPException:
    """各服务的拒绝（status_code / message / code）→ HTTP（带 code 的回 {detail, code}）。"""
    if getattr(e, "code", None):
        return CodedHTTPException(e.status_code, e.message, e.code)
    return HTTPException(e.status_code, e.message)


def _blocked(e: Any) -> JSONResponse:
    """被检查问题挡住的拒绝（试运行遇到缺项、待填的关节）：{detail, code, issues}，问题带白话，界面逐条列出来。"""
    return JSONResponse(status_code=e.status_code, content={
        "detail": e.message, **({"code": e.code} if getattr(e, "code", None) else {}),
        "issues": getattr(e, "issues", None) or []})


# --------------------------------------------------------------------------
# 起点
# --------------------------------------------------------------------------


class SourceIn(BaseModel):
    kind: Literal["conversation", "template", "run", "workflow_version"]
    id: str | None = None
    version: int | None = None


class BlueprintIn(BaseModel):
    source: SourceIn
    scope: Literal["shared", "personal"] = "shared"
    name: str | None = Field(default=None, max_length=200)
    #: 从模板开始时的模板 id（也可以写在 source.id）
    template: str | None = None


def _freeform_ref() -> dict[str, Any]:
    from app.blueprints.templates import get_template

    template = get_template(FREEFORM)
    return {"id": FREEFORM, "version": template.version if template is not None else 1}


def _proposed(value: Any) -> dict[str, Any]:
    """起点带来的一项（运行时的问题、工作流的说明）：是从已有的东西里取的，没经用户确认，记「待确认」。"""
    return {"value": value, "source": "assistant", "status": "proposed", "why": None}


@router.post("/blueprints", status_code=201)
async def create_blueprint(payload: BlueprintIn, session: AsyncSession = Depends(get_session),
                           x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """起点：{source: {kind: conversation|template|run|workflow_version, id?, version?}, scope: shared|personal, name?}"""
    from app.blueprints.spec import empty_blueprint
    from app.blueprints.trial import record_event
    from app.core.artifact_store import graph_hash

    principal = _principal(x_actor)
    if payload.scope == "personal":
        await _personal_allowed(session, principal)
    else:
        _require_builder(principal)
    spec = empty_blueprint()
    bp = Blueprint(name=(payload.name or "").strip(), owner=principal.username, scope=payload.scope,
                   status="drafting", source={"kind": payload.source.kind}, updated_by=principal.username)
    kind = payload.source.kind
    if kind == "conversation":
        from app.db.models import Conversation

        bp.name = bp.name or DEFAULT_NAME
        conversation = Conversation(kind=CONVERSATION_KIND, created_by=principal.username, title=f"「{bp.name}」的蓝图")
        session.add(conversation)
        await session.flush()
        bp.conversation_id = conversation.id
    elif kind == "template":
        spec = _template_spec(spec, payload.template or payload.source.id)
        bp.name = bp.name or DEFAULT_NAME
        bp.source = {"kind": kind, "id": spec["template"]["id"]}
    elif kind == "run":
        spec, graph = await _from_run(session, spec, payload.source.id, principal)
        bp.graph, bp.graph_hash = graph, graph_hash(graph)
        bp.name = bp.name or str((spec.get("purpose") or {}).get("value") or DEFAULT_NAME)[:40]
        bp.source = {"kind": kind, "id": payload.source.id}
    else:
        _require_builder(principal, "从已发布的工作流开始")
        spec, workflow, row = await _from_version(session, spec, payload.source.id, payload.source.version)
        bp.graph, bp.graph_hash = row.graph or {}, row.graph_hash or graph_hash(row.graph or {})
        bp.workflow_id = workflow.id
        bp.name = bp.name or workflow.name
        bp.source = {"kind": kind, "id": workflow.id, "version": row.version}
    ref = spec.get("template")
    if isinstance(ref, dict):
        bp.template_id, bp.template_version = ref.get("id"), ref.get("version")
    bp.spec = spec
    session.add(bp)
    await session.flush()
    await record_event(session, bp, "create", {"source": bp.source, "scope": bp.scope}, principal=principal)
    await session.commit()
    await session.refresh(bp)
    return blueprint_out(bp)


def _template_spec(spec: dict[str, Any], template_id: str | None) -> dict[str, Any]:
    """从模板开始：按模板预置卡片（选模板走 bp_template，来源记成用户：是用户挑的模板）。"""
    from app.blueprints.ops import BpRefused, apply_bp_op
    from app.blueprints.templates import get_template

    template = get_template(template_id or "")
    if template is None:
        raise CodedHTTPException(422, f"没有叫「{template_id or ''}」的场景模板", BAD_SOURCE, field="template")
    try:
        return apply_bp_op(spec, {"op": "bp_template", "template": {"id": template.id, "version": template.version}},
                           source="user")
    except BpRefused as e:
        raise CodedHTTPException(422, e.message, e.code) from e


async def _from_run(session: AsyncSession, spec: dict[str, Any], run_id: str | None,
                    principal: Principal) -> tuple[dict[str, Any], dict[str, Any]]:
    """从问数据的一轮回答开始：按「提取成工作流」同一个规则取出这次运行实际走过的路径（api/copilot.extract_run_path），
    不建工作流——图存在蓝图上，发布单保存版本时才建。读不到这次运行的人当它不存在。"""
    from app.api.copilot import PathRefused, extract_run_path
    from app.api.deps import can_read_run
    from app.db.models import Run

    if not run_id:
        raise CodedHTTPException(422, "从问数据的回答开始，需要说明是哪一次运行", BAD_SOURCE, field="source")
    run = await session.get(Run, run_id)
    if run is None or not await can_read_run(principal, run):
        raise HTTPException(404, "运行记录不存在，可能已被删除")
    try:
        path = await extract_run_path(session, run_id)
    except PathRefused as e:
        raise HTTPException(e.status_code, e.message) from e
    question = (run.input or {}).get("question")
    spec = {**spec, "template": _freeform_ref()}
    if isinstance(question, str) and question.strip():
        spec["purpose"] = _proposed(question.strip()[:500])
    return spec, path.graph


async def _from_version(session: AsyncSession, spec: dict[str, Any], workflow_id: str | None,
                        version: int | None) -> tuple[dict[str, Any], Any, Any]:
    """从已发布的工作流版本开始：图原样导入，模板记「自由形」，蓝图挂在这个工作流上（发布单按图哈希复用这一版）。"""
    from app.apps.versions import is_published, version_row
    from app.db.models import Workflow

    workflow = await session.get(Workflow, workflow_id or "")
    if workflow is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    wanted = version or workflow.published_version
    row = await version_row(session, workflow.id, wanted) if wanted else None
    if row is None or not is_published(row, current=workflow.published_version):
        raise CodedHTTPException(409, f"「{workflow.name}」{f'v{wanted} ' if wanted else ''}不是在用的发布版本："
                                      "只能从已发布、没退役的版本开始", BAD_SOURCE, field="source")
    spec = {**spec, "template": _freeform_ref()}
    if (workflow.description or "").strip():
        spec["purpose"] = _proposed(workflow.description.strip()[:500])
    return spec, workflow, row


# --------------------------------------------------------------------------
# 列表、详情、卡片上的修改
# --------------------------------------------------------------------------


@router.get("/blueprints")
async def list_blueprints(scope: Literal["shared", "personal"] | None = None,
                          session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """共享蓝图（搭建者看得到）和我的个人蓝图（能用个人应用时），按最近改动排。"""
    from app.blueprints.personal import allowed

    principal = _principal(x_actor)
    wanted = []
    if scope in (None, "shared") and principal.at_least("builder"):
        wanted.append(Blueprint.scope == "shared")
    if scope in (None, "personal") and await allowed(session, principal):
        mine = Blueprint.scope == "personal"
        if principal.mode != "off":
            mine = mine & (Blueprint.owner == (principal.username or ""))
        wanted.append(mine)
    if not wanted:
        return {"items": []}
    rows = (await session.execute(select(Blueprint).where(or_(*wanted)).order_by(Blueprint.updated_at.desc())
                                  .limit(200))).scalars()
    return {"items": [blueprint_out(bp) for bp in rows]}


@router.get("/blueprints/{blueprint_id}")
async def get_blueprint(blueprint_id: str, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.blueprints.trial import settle

    bp = await _blueprint(session, blueprint_id)
    return blueprint_out(await settle(session, bp))


@router.get("/blueprints/{blueprint_id}/conversation")
async def blueprint_conversation(blueprint_id: str, session: AsyncSession = Depends(get_session),
                                 x_actor: str | None = Header(default=None)) -> Any:
    """蓝图对话的历史（ConversationDetail）：权限按蓝图判（同读蓝图），不经问数据的路由——没开问数据的使用者也读得到
    自己个人蓝图的对话。蓝图没有对话（从模板、运行开始的）回 404。"""
    from app.api.conversations import conversation_detail
    from app.db.models import Conversation

    bp = await _blueprint(session, blueprint_id)
    row = await session.get(Conversation, bp.conversation_id) if bp.conversation_id else None
    if row is None:
        raise HTTPException(404, "这张蓝图没有对话")
    return await conversation_detail(session, row)


class BlueprintPatch(BaseModel):
    ops: list[dict[str, Any]] = Field(default_factory=list)
    name: str | None = Field(default=None, min_length=1, max_length=200)


@router.patch("/blueprints/{blueprint_id}")
async def patch_blueprint(blueprint_id: str, payload: BlueprintPatch, session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """{ops: [bp_* 操作], name?}：用户在卡片上的修改，来源记成 user。一个操作被拒整批不落（422，带 code）。"""
    from app.blueprints.ops import BpRefused

    principal = _principal(x_actor)
    bp = await _blueprint(session, blueprint_id)
    if payload.name is not None and payload.name.strip() != (bp.name or ""):
        # 改名也留痕（P2）：名字进工作流和应用的名字、口径卡的标题
        from app.blueprints.trial import record_event

        before, bp.name = bp.name, payload.name.strip()
        bp.updated_by = principal.username
        await record_event(session, bp, "rename", {"source": "user", "before": before, "after": bp.name},
                           principal=principal)
    if payload.ops:
        from app.blueprints.store import apply_ops_and_save

        try:
            await apply_ops_and_save(session, bp, payload.ops, principal.username, principal.verified, "user")
        except BpRefused as e:
            await session.rollback()
            raise CodedHTTPException(422, e.message, e.code) from e
    await session.commit()
    await session.refresh(bp)
    return blueprint_out(bp)


# --------------------------------------------------------------------------
# 实例化、试运行、预览
# --------------------------------------------------------------------------


@router.post("/blueprints/{blueprint_id}/instantiate")
async def instantiate_blueprint(blueprint_id: str, session: AsyncSession = Depends(get_session),
                                x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """按模板实例化：图、关节存到蓝图上，交回应用草稿和问题（缺项、待填的关节，带白话）。不发起运行。"""
    from app.blueprints.trial import TrialRefused, materialize

    principal = _principal(x_actor)
    bp = await _blueprint(session, blueprint_id)
    try:
        result = await materialize(session, bp, principal=principal)
    except TrialRefused as e:
        raise _refused(e) from e
    return {"blueprint": blueprint_out(bp), "app_spec": result.app_spec, "problems": result.problems}


class TrialIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/blueprints/{blueprint_id}/trial")
async def trial(blueprint_id: str, payload: TrialIn, session: AsyncSession = Depends(get_session),
                x_actor: str | None = Header(default=None)) -> Any:
    """用真数试运行：实例化 → 保存图 → 发起探索运行，交回 {run_id, blueprint}。缺项、待填的关节挡住时回 409 和问题。"""
    from app.blueprints.trial import TrialRefused, start_trial
    from app.engine.launch import LaunchRefused

    principal = _principal(x_actor)
    bp = await _blueprint(session, blueprint_id)
    try:
        run, _ = await start_trial(session, bp, inputs=payload.inputs, principal=principal, actor=principal.username)
    except TrialRefused as e:
        if e.issues:
            return _blocked(e)
        raise _refused(e) from e
    except LaunchRefused as e:
        raise _refused(e) from e
    return {"run_id": run.id, "blueprint": blueprint_out(bp)}


@router.get("/blueprints/{blueprint_id}/preview")
async def preview(blueprint_id: str, page: str | None = None, run: str | None = None,
                  session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """按这次试运行解析应用草稿的一页（agentlab.view/1）：印章写「试运行」，流程组件对着这次运行。"""
    from app.apps.resolve import ViewRefused
    from app.blueprints.trial import TrialRefused, preview_page

    principal = _principal(x_actor)
    bp = await _blueprint(session, blueprint_id)
    try:
        return await preview_page(session, bp, page_id=page, run_id=run, principal=principal)
    except (TrialRefused, ViewRefused) as e:
        raise _refused(e) from e


# --------------------------------------------------------------------------
# 发布单
# --------------------------------------------------------------------------


class PlanIn(BaseModel):
    level: Literal["published", "governed"] = "published"
    #: 跑第一次（不给时看板类为真、流程类为假）
    first_run: bool | None = None
    #: 钉住项升版时的处置 {别名: recompute|dual|incomparable}
    dispositions: dict[str, Literal["recompute", "dual", "incomparable"]] = Field(default_factory=dict)


async def _shared_for_plan(session: AsyncSession, blueprint_id: str, principal: Principal) -> Blueprint:
    bp = await _blueprint(session, blueprint_id)
    if bp.scope != "shared":
        raise CodedHTTPException(409, "个人蓝图不走发布单：存成你的应用自己用，想给别人用请提交审核", "plan_state")
    _require_builder(principal, "发布")
    return bp


@router.post("/blueprints/{blueprint_id}/publish-plan")
async def publish_plan(blueprint_id: str, payload: PlanIn, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """出发布单（只读预检）：{id, steps, summary, problems（带白话、谁来改）, ok}。受管级由搭建者出单时，问题里写明要管理员。"""
    from app.blueprints import publish_plan as plans
    from app.blueprints.trial import settle

    principal = _principal(x_actor)
    bp = await settle(session, await _shared_for_plan(session, blueprint_id, principal))
    try:
        row = await plans.plan(session, bp, level=payload.level, principal=principal,
                               options={"dispositions": payload.dispositions,
                                        **({"first_run": payload.first_run} if payload.first_run is not None else {})})
    except plans.PlanRefused as e:
        raise _refused(e) from e
    return plans.plan_out(row)


async def _plan_for(session: AsyncSession, plan_id: str, principal: Principal) -> Any:
    from app.blueprints import publish_plan as plans

    try:
        row = await plans.get_plan(session, plan_id)
    except plans.PlanRefused as e:
        raise _refused(e) from e
    await _shared_for_plan(session, row.blueprint_id, principal)
    return row


@router.get("/publish-plans/{plan_id}")
async def get_plan(plan_id: str, session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.blueprints.publish_plan import plan_out

    return plan_out(await _plan_for(session, plan_id, _principal(x_actor)))


async def _drive(plan_id: str, x_actor: str | None, session: AsyncSession, how: str) -> dict[str, Any]:
    from app.blueprints import publish_plan as plans

    principal = _principal(x_actor)
    row = await _plan_for(session, plan_id, principal)
    if row.level == "governed" and not principal.at_least("admin"):
        # 受管级只有管理员能发（Q8，同工作流发布接口）：搭建者出的单可以看，执行要管理员
        raise CodedHTTPException(403, "以受管级别发布需要管理员权限", ADMIN_ONLY)
    try:
        done = await (plans.run if how == "run" else plans.resume)(session, plan_id, principal=principal)
    except plans.PlanRefused as e:
        raise _refused(e) from e
    return plans.plan_out(done)


@router.post("/publish-plans/{plan_id}/run")
async def run_plan(plan_id: str, session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """确认后按序执行。某步停下时 status=stopped，那一步写明原因（白话）；修好后用「接着做」。"""
    return await _drive(plan_id, x_actor, session, "run")


@router.post("/publish-plans/{plan_id}/resume")
async def resume_plan(plan_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """从停下的那一步接着做：已完成的按结果里的 id 核对，不重做。"""
    return await _drive(plan_id, x_actor, session, "resume")

