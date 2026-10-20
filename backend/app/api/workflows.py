from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import graph_error
from app.api.coded import CodedHTTPException
from app.api.runs import actor_of
from app.core.artifact_store import graph_hash
from app.data.catalog_impact import (
    catalog_drift, catalog_versions_for, open_structure_issues, recorded_versions, schema_drift, schema_versions_for,
)
from app.db.base import get_session
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.governance import publish_issues
from app.engine.lint_process import load_subgraphs, with_subgraphs
from app.engine.provider_refs import CODES as PROVIDER_CODES, load_issues, load_providers, quiet_unset_model
from app.engine.schema import GraphSpec, NodeType, validate_graph

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_GONE = "工作流不存在，可能已被删除"
#: 删除被拒：还有应用固定着、或者发布过的版本有生效记录（F4）
WORKFLOW_IN_USE = "workflow_in_use"
#: 受管级发布、版本退役只给管理员（Q8）
ADMIN_ONLY = "admin_only"
_LEVELS = ("published", "governed")
_BAD_LEVEL = "发布级别只能是「已发布」或「受管」"
#: 剩下的挡住发布的只有模型接入的问题、人又要交给助手时的说明：换成哪个接入要人拿主意，助手处理不了。发布弹窗里
#: 渲染成「助手：……」，说明里不再带冒号；可能是全图默认、也可能只写了模型而提供它的接入停用了，不说「节点」「点名」。
#: 人刚点了「交给助手」，写「助手无法处理」，不写「无法交给助手处理」（读来像没交出去）
PROVIDER_LEFT = ("剩余的问题是所用的模型接入不存在或已停用，助手无法处理。"
                 "请在「设置 → 模型接入」中核对名称或启用相应的接入")
#: 剩下的问题里有一条带着修复（provider_refs 按候选算得出来）才补这半句：一个候选都落不稳时那里什么都不给。弹窗里那一组
#: 选项在每条问题下方，标题是「选择……改用的模型接入」；另起半句，不和前面的「或」连用
PROVIDER_LEFT_FIX = "；也可在各条问题下方选择改用的模型接入"


class WorkflowOrigin(BaseModel):
    """新工作流出自哪次运行、哪段对话、哪一轮（next-round 02 做法 9）：问数据的「在画布里打开」带上它，和从运行提取（/copilot/
    from-run）记成同一个形状 {run_id, conversation_id?, turn_id?}。「口径 → 来源问题」、认证队列的「在画布里打开过」都读它。"""

    run_id: str = Field(min_length=1, max_length=32)
    conversation_id: str | None = Field(default=None, max_length=32)
    turn_id: str | None = Field(default=None, max_length=32)


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    graph: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    #: 来源（WorkflowOrigin）：服务端核对运行读得到、对话用得了、这一轮属于这段对话，才记进 workflows.origin
    origin: WorkflowOrigin | None = None


class WorkflowPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    graph: dict[str, Any] | None = None
    tags: list[str] | None = None
    note: str = ""


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    graph: dict[str, Any]
    tags: list[str]
    version: int
    is_template: bool
    status: str = "draft"
    published_version: int | None = None
    published_by: str | None = None
    #: 当前发布版本的级别（published / governed）。status 在画布上改过之后会回到 draft，「这是不是受管工作流」看这一项
    published_level: str | None = None
    #: 画布这一版的图和当前发布版的图内容相同（按 graph_hash 比）：撤回改动后保存出来的新版本号更大，内容却和发布版
    #: 一样，页头照实写「与发布版内容相同」，不写「领先 N 版」。没发布过、老数据没记哈希的为 false
    same_as_published: bool = False
    created_at: Any = None
    updated_at: Any = None
    run_count: int = 0

    model_config = {"from_attributes": True}


async def _published_levels(session: AsyncSession, ids: list[str] | None = None) -> dict[str, str]:
    """各工作流当前发布版本的级别 {工作流 id: level}。没发布过、或老数据没记级别的不在里面。"""
    stmt = select(WorkflowVersion.workflow_id, WorkflowVersion.level).join(
        Workflow, (Workflow.id == WorkflowVersion.workflow_id) & (Workflow.published_version == WorkflowVersion.version))
    if ids is not None:
        stmt = stmt.where(WorkflowVersion.workflow_id.in_(ids))
    return {wid: level for wid, level in (await session.execute(stmt)).all() if level}


async def _same_as_published(session: AsyncSession, ids: list[str] | None = None) -> set[str]:
    """画布这一版（Workflow.version）的图哈希和当前发布版相同的工作流 id。两边哈希都记着才比，不在前端比图。"""
    current, published = aliased(WorkflowVersion), aliased(WorkflowVersion)
    stmt = select(Workflow.id, current.graph_hash, published.graph_hash).join(
        current, (current.workflow_id == Workflow.id) & (current.version == Workflow.version)).join(
        published, (published.workflow_id == Workflow.id) & (published.version == Workflow.published_version))
    if ids is not None:
        stmt = stmt.where(Workflow.id.in_(ids))
    return {wid for wid, mine, theirs in (await session.execute(stmt)).all() if mine and mine == theirs}


async def _run_counts(session: AsyncSession, ids: list[str] | None = None) -> dict[str, int]:
    """各工作流的运行次数 {工作流 id: 次数}，ids 为 None 时算全部。列表和单个工作流的接口都按它算：以前只有列表
    计数，详情、保存、恢复的返回落在 WorkflowOut 的默认值 0 上，同一个工作流两处说的次数不一样。"""
    stmt = select(Run.workflow_id, func.count(Run.id)).group_by(Run.workflow_id)
    if ids is not None:
        stmt = stmt.where(Run.workflow_id.in_(ids))
    return dict((await session.execute(stmt)).all())


async def _workflow_out(session: AsyncSession, workflow: Workflow) -> WorkflowOut:
    """返回单个工作流的接口都走这里，run_count 和列表一致。"""
    item = WorkflowOut.model_validate(workflow)
    item.run_count = (await _run_counts(session, [workflow.id])).get(workflow.id, 0)
    item.published_level = (await _published_levels(session, [workflow.id])).get(workflow.id)
    item.same_as_published = workflow.id in await _same_as_published(session, [workflow.id])
    return item


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[WorkflowOut]:
    rows = list(
        (await session.execute(select(Workflow).order_by(Workflow.updated_at.desc(), Workflow.id.desc()))).scalars()
    )
    counts = await _run_counts(session)
    levels = await _published_levels(session)
    same = await _same_as_published(session)
    out: list[WorkflowOut] = []
    for row in rows:
        item = WorkflowOut.model_validate(row)
        item.run_count = counts.get(row.id, 0)
        item.published_level = levels.get(row.id)
        item.same_as_published = row.id in same
        out.append(item)
    return out


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowIn, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    origin = await _checked_origin(session, payload.origin) if payload.origin is not None else None
    workflow = Workflow(
        name=payload.name,
        description=payload.description,
        graph=payload.graph,
        tags=payload.tags,
        origin=origin,
    )
    session.add(workflow)
    await session.flush()
    session.add(
        WorkflowVersion(workflow_id=workflow.id, version=1, graph=payload.graph,
                        graph_hash=graph_hash(payload.graph), note="初始版本")
    )
    await session.commit()
    await session.refresh(workflow)
    return await _workflow_out(session, workflow)


#: 新工作流的来源核对不过（422）：运行读不到、这一轮不属于这段对话
WORKFLOW_ORIGIN_INVALID = "workflow_origin_invalid"


async def _checked_origin(session: AsyncSession, origin: WorkflowOrigin) -> dict[str, str]:
    """核对新工作流的来源，规则同从运行提取（/copilot/from-run）：运行这个人读得到（读不到的当它不存在）；对话这个人用得了
    （不能用的 403）；给了轮次的，这一轮要是这段对话的。对话不存在的照 from-run 记下 id（对话删了，来源仍是那次运行）。"""
    from app.api.deps import can_read_run, current_principal, ensure_conversation_access
    from app.db.models import ConversationTurn

    run = await session.get(Run, origin.run_id)
    if run is None or not await can_read_run(current_principal(), run):
        raise CodedHTTPException(422, "来源的运行不存在，可能已被删除", WORKFLOW_ORIGIN_INVALID, field="origin")
    conversation = await ensure_conversation_access(session, origin.conversation_id)
    if origin.turn_id:
        turn = await session.get(ConversationTurn, origin.turn_id)
        if turn is None or not origin.conversation_id or turn.conversation_id != origin.conversation_id:
            raise CodedHTTPException(422, "来源的这一轮不属于这段对话", WORKFLOW_ORIGIN_INVALID, field="origin")
    return {"run_id": run.id,
            **({"conversation_id": conversation.id if conversation else origin.conversation_id}
               if origin.conversation_id else {}),
            **({"turn_id": origin.turn_id} if origin.turn_id else {})}


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    return await _workflow_out(session, workflow)


#: 工作流名称最长多少字（workflows.name 是 String(200)）
NAME_MAX = 200


async def _checked_name(session: AsyncSession, workflow: Workflow, given: str) -> str:
    """改名：去掉首尾空白，不许空、不许超长、不许和别的工作流同名。

    内置模板不许改名：启动时补内置模板是按名字认的（seed.seed_defaults），改了名下次启动会再导入一份原名的。要改名就
    「以此模板新建」，改新建的那一份。名字没变（只是首尾多了空白）的照常放行。
    """
    name = given.strip()
    if not name:
        raise HTTPException(422, "工作流名称不能为空")
    if len(name) > NAME_MAX:
        raise HTTPException(422, f"工作流名称最多 {NAME_MAX} 个字")
    if name == workflow.name:
        return name
    from app.seed import TEMPLATES

    if workflow.is_template and workflow.name in {t["name"] for t in TEMPLATES}:
        raise HTTPException(409, "内置模板的名称由系统维护，不能修改：可以「以此模板新建」，再给新建的工作流改名")
    taken = await session.scalar(select(Workflow.id).where(Workflow.name == name, Workflow.id != workflow.id).limit(1))
    if taken:
        raise HTTPException(409, f"已存在同名工作流「{name}」，请使用其他名称")
    return name


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str, payload: WorkflowPatch, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)

    if payload.name is not None:
        workflow.name = await _checked_name(session, workflow, payload.name)
    if payload.description is not None:
        workflow.description = payload.description
    if payload.tags is not None:
        workflow.tags = payload.tags
    if payload.graph is not None and payload.graph != workflow.graph:
        # 图有实际变化才留快照，避免改个名字就堆一堆版本
        workflow.graph = payload.graph
        workflow.version += 1
        session.add(
            WorkflowVersion(
                workflow_id=workflow.id,
                version=workflow.version,
                graph=payload.graph,
                graph_hash=graph_hash(payload.graph),
                note=payload.note,
            )
        )
        # status 说的是**当前画布**的状态，不是"这个工作流曾经发布过"。
        # 改完图还挂着 governed，界面上就会显示一张从未过闸的图是受管模板——
        # 已发布的那一版仍由 published_version 指着，formal 运行不受影响。
        if workflow.status in ("published", "governed"):
            workflow.status = "draft"
    await session.commit()
    await session.refresh(workflow)
    return await _workflow_out(session, workflow)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    # 运行记录是 ON DELETE SET NULL：删掉工作流，已结束的运行留着、照样能核对封存；
    # 还在跑的那次会失去归属，再也回不到它的画布，所以和删除运行记录一样先停再删
    live = await session.scalar(
        select(func.count()).select_from(Run).where(
            Run.workflow_id == workflow_id, Run.status.in_(("queued", "running"))
        )
    )
    if live:
        raise HTTPException(409, f"该工作流还有 {live} 次运行正在进行，现在删除会使这些运行失去所属工作流。请先停止运行，再删除")
    # 版本随工作流级联删除（F4）：应用固定着的版本、带生效记录的版本删掉，应用发起不了、生效记录对不上承诺。这两种拒绝
    if reasons := await _delete_blockers(session, workflow_id):
        raise CodedHTTPException(409, "该工作流还在使用中，不能删除：" + "；".join(reasons), WORKFLOW_IN_USE)
    await session.delete(workflow)
    await session.commit()


async def _delete_blockers(session: AsyncSession, workflow_id: str) -> list[str]:
    """拒绝删除的原因：应用固定着；某个版本有生效记录（演练不算）。

    生效记录按全部版本数，不只看在用的发布版：退役只是不再接受新的运行，退役之前写进外部系统的那些事照样发生过，
    版本删掉了，生效记录就对不上它当时的承诺（D-n）。"""
    from app.apps.versions import pinning_apps
    from app.db.models import ActionRecord

    reasons = [f"应用「{a['name']}」v{a['app_version']} 固定了 v{a['version']}"
               for a in await pinning_apps(session, workflow_id)]
    rows = (await session.execute(
        select(ActionRecord.workflow_version, func.count(ActionRecord.id))
        .where(ActionRecord.workflow_id == workflow_id, ActionRecord.state != "simulated")
        .group_by(ActionRecord.workflow_version).order_by(ActionRecord.workflow_version))).all()
    retired = set((await session.execute(select(WorkflowVersion.version).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.retired_at.is_not(None)))).scalars())
    for version, count in rows:
        which = f"v{version}" if version is not None else "未记版本的运行"
        reasons.append(f"{which}{'（已退役）' if version in retired else ''} 有 {count} 条生效记录")
    return reasons


@router.post("/{workflow_id}/duplicate", response_model=WorkflowOut, status_code=201)
async def duplicate_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    source = await session.get(Workflow, workflow_id)
    if not source:
        raise HTTPException(404, _GONE)
    copy = Workflow(
        name=f"{source.name} 副本",
        description=source.description,
        graph=source.graph,
        tags=list(source.tags or []),
    )
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return await _workflow_out(session, copy)


class VersionOut(BaseModel):
    id: str
    version: int
    note: str
    created_at: Any = None
    #: 这一版按哪一档发布的（published / governed）。没发布过、或者是记级别之前发布的为 null
    level: str | None = None
    #: 这一版是谁、什么时候发布的；退役了的是什么时候、谁退的、退役时写的说明（没写为 null）。存量为 null
    published_at: Any = None
    published_by: str | None = None
    retired_at: Any = None
    retired_by: str | None = None
    retire_note: str | None = None

    model_config = {"from_attributes": True}


@router.get("/{workflow_id}/versions", response_model=list[VersionOut])
async def list_versions(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> list[WorkflowVersion]:
    rows = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.version.desc())
    )
    return list(rows.scalars())


class VersionDetailOut(VersionOut):
    workflow_id: str
    graph: dict[str, Any]
    graph_hash: str | None = None
    #: 这一版入口节点声明的字段。正式运行跑的是已发布的那一版，表单得照它来填——
    #: 画布上改过入口字段时，拿当前画布的字段去跑已发布版本会传错参数
    input_fields: list[dict[str, Any]] = Field(default_factory=list)
    published: bool = False
    #: 发布时记下的数据目录版本 {"direct": {源名: {表名: 版本}}, "possible": {源名: {表名: 版本}}}：direct 写死的
    #: SQL 用到的表，possible Agent 可能查询的表（catalog_impact.recorded_versions，只记了直接引用的旧记录 possible
    #: 为空）；没发布过、或者记录之前发布的为 null
    catalog_versions: dict[str, Any] | None = None
    #: 发布之后目录有变化的表：[{source, source_id, table, label, published, current, impact}]
    catalog_changes: list[dict[str, Any]] = Field(default_factory=list)
    #: 发布时记下的表结构版本 {源名: {表名: 结构版本}}（只记写死的 SQL 直接引用的表，next-round 03 B1）；记录之前发布的为 null
    schema_versions: dict[str, Any] | None = None
    #: 发布之后结构版本变了的表：[{source, source_id, table, label, published, current, removed, changes, more}]
    schema_changes: list[dict[str, Any]] = Field(default_factory=list)
    #: 入口字段换成表单（agentlab.form/1）：发起表单、应用入口用同一个渲染器（X2）
    input_schema: dict[str, Any] = Field(default_factory=dict)
    #: 账本（L4）：固定这一版的应用、生效记录数、正式运行数
    usage: dict[str, Any] = Field(default_factory=dict)


@router.get("/{workflow_id}/versions/{version}", response_model=VersionDetailOut)
async def get_version(
    workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
) -> VersionDetailOut:
    """取某一版的完整图。"""
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    snapshot = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise HTTPException(404, f"「{workflow.name}」不存在 v{version} 版本")

    graph = snapshot.graph or {}
    fields: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        if node.get("type") == "input":
            fields.extend(
                f for f in ((node.get("data") or {}).get("config") or {}).get("fields") or []
                if isinstance(f, dict)
            )
    from app.apps.forms import input_schema
    from app.apps.versions import usage

    return VersionDetailOut(
        id=snapshot.id, version=snapshot.version, note=snapshot.note,
        created_at=snapshot.created_at, workflow_id=workflow_id, graph=graph,
        graph_hash=snapshot.graph_hash or graph_hash(graph), input_fields=fields,
        published=workflow.published_version == snapshot.version, level=snapshot.level,
        catalog_versions=recorded_versions(snapshot.catalog_versions),
        catalog_changes=await catalog_drift(session, snapshot.catalog_versions),
        schema_versions=snapshot.schema_versions,
        schema_changes=await schema_drift(session, snapshot.schema_versions),
        input_schema=input_schema(fields), published_at=snapshot.published_at, published_by=snapshot.published_by,
        retired_at=snapshot.retired_at, retired_by=snapshot.retired_by, retire_note=snapshot.retire_note,
        usage=await usage(session, workflow_id, snapshot.version),
    )


class RetireIn(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.post("/{workflow_id}/versions/{version}/retire")
async def retire_version(
    workflow_id: str, version: int, payload: RetireIn, session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    """退役一个发布过的版本：不再接受新的正式运行，进行中的实例照常走完。当前发布版不能退役；返回受影响的应用
    （它们标「需升级」）。只有管理员能做（Q8），关登录时放行。"""
    from app.apps.versions import RetireRefused, retire_version as retire

    _require_admin("退役版本")
    try:
        return await retire(session, workflow_id, version, actor=actor_of(x_actor), note=payload.note)
    except RetireRefused as e:
        if e.code:
            raise CodedHTTPException(e.status_code, e.message, e.code) from e
        raise HTTPException(e.status_code, e.message) from e


def _require_admin(what: str) -> None:
    """受管级发布、版本退役只给管理员（Q8）。关登录时所有人等同管理员。"""
    from app.api.deps import current_principal

    if not current_principal().at_least("admin"):
        raise CodedHTTPException(403, f"{what}需要管理员权限", ADMIN_ONLY)


@router.post("/{workflow_id}/versions/{version}/restore", response_model=WorkflowOut)
async def restore_version(
    workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    snapshot = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise HTTPException(404, f"不存在 v{version} 版本")

    workflow.graph = snapshot.graph
    workflow.version += 1
    session.add(
        WorkflowVersion(
            workflow_id=workflow.id,
            version=workflow.version,
            graph=snapshot.graph,
            graph_hash=graph_hash(snapshot.graph),
            note=f"回滚到 v{version}",
        )
    )
    # 恢复旧版也是改图，和 PATCH 同一条规矩：旧图没过这次的闸，不能挂着受管 / 已发布
    if workflow.status in ("published", "governed"):
        workflow.status = "draft"
    await session.commit()
    await session.refresh(workflow)
    return await _workflow_out(session, workflow)


class PublishIn(BaseModel):
    level: str = "published"  # published | governed
    version: int | None = None  # 默认发布当前最新版本


@router.post("/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: str,
    payload: PublishIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> dict[str, Any]:
    """把某个版本立为正式版本。

    formal 运行只能从这里发布过的版本发起。governed 级别额外过治理 lint：
    错误会挡住发布——这就是"必经检查点"落地的地方。
    返回 200 + ok/issues 而不是抛错，让前端能把问题列表渲染出来。
    """
    if payload.level not in _LEVELS:
        raise HTTPException(400, _BAD_LEVEL)
    if payload.level == "governed":
        _require_admin("以受管级别发布")
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    if await downgrades_governed(session, workflow, payload.level):
        # 把受管工作流降成已发布，之后的正式运行就不再过治理门禁：和以受管级别发布是同一级的决定（Q8）
        _require_admin("把受管工作流降为已发布")

    version = payload.version or workflow.version
    snapshot = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise HTTPException(404, f"不存在 v{version} 版本：画布上的修改还没有保存，请先保存再发布")

    issues = await gate_issues(session, snapshot.graph, payload.level)
    if issues is None:
        return {"ok": False, "issues": [{"level": "error", "message": _unparsed(snapshot.graph)}]}
    if any(i["level"] == "error" for i in issues):
        return {"ok": False, "level": payload.level, "version": version, "issues": issues}
    return {**await commit_publish(session, workflow, snapshot, payload.level, actor_of(x_actor)),
            "issues": issues}  # 剩下的都是警告


async def downgrades_governed(session: AsyncSession, workflow: Workflow, level: str) -> bool:
    """这次发布会不会把受管工作流降成已发布（Q8：只给管理员）。发布接口和发布单的预检共用。"""
    return level != "governed" and bool(workflow.published_version) and \
        (await _published_levels(session, [workflow.id])).get(workflow.id) == "governed"


def _unparsed(graph: Any) -> str:
    try:
        GraphSpec.model_validate(graph)
    except Exception as e:  # noqa: BLE001
        return f"无法解析工作流结构：{graph_error(e)}"
    return "无法解析工作流结构"


async def gate_issues(session: AsyncSession, graph: Any, level: str) -> list[dict[str, Any]] | None:
    """发布这张图会遇到的问题（validate + 门禁），只读：不改库、不发布。图不成形时为 None。
    发布接口和发布单的预检（blueprints/publish_plan.py）共用，口径只有这一份。"""
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001
        return None
    checkers = await _sql_checkers(session, spec)
    providers = await _providers(session, spec)
    with with_subgraphs(await load_subgraphs(session, spec)):
        issues = [i.model_dump() for i in publish_issues(spec, level=level, checkers=checkers, providers=providers)]
    # 这一版直接引用的表上还没处理的结构变化（next-round 03 B1）：只是警告，不拦发布
    return issues + await open_structure_issues(session, spec)


async def commit_publish(session: AsyncSession, workflow: Workflow, snapshot: WorkflowVersion, level: str,
                         actor: str | None) -> dict[str, Any]:
    """把这一版立为正式版本并提交（调用方先过了 gate_issues、权限）。发布接口和发布单共用。"""
    # 发布时用到的能力的治理版本（F5）。在改这一版之前取：能力登记缺了的要另开会话同步、提交，这个会话手里不能先有写
    from app.capabilities.registry import capability_versions_for
    from app.db.base import utcnow
    from app.engine.launch import graph_tool_names

    spec = GraphSpec.model_validate(snapshot.graph)
    capabilities = await capability_versions_for(session, graph_tool_names(snapshot.graph))
    if not snapshot.graph_hash:
        snapshot.graph_hash = graph_hash(snapshot.graph)
    # 级别记在这一版上：之后改画布（status 退回 draft），从这一版发起的正式运行照样按发布时的级别出具
    snapshot.level = level
    # 发布时的目录版本也记在这一版上：重新发布同一版等于按现在的目录重新认可它，跟着更新
    snapshot.catalog_versions = await catalog_versions_for(session, spec)
    # 表的结构版本同理（next-round 03 B1）：正式运行发起时和现在的比，变了写 schema.drift，只提醒、不拦
    snapshot.schema_versions = await schema_versions_for(session, spec)
    # 发布人、发布时间、用到的能力版本也记在这一版上（F5）：工作流上的 published_by 只记最近一次。退役过的版本重新
    # 发布等于重新认可它，退役信息清掉
    snapshot.published_at = utcnow()
    snapshot.published_by = actor
    snapshot.capability_versions = capabilities
    snapshot.retired_at = snapshot.retired_by = snapshot.retire_note = None
    workflow.status = level
    workflow.published_version = snapshot.version
    workflow.published_by = actor
    await session.commit()
    # 口径账本（next-round 02 做法 8）：从问数据固化出的口径发布了，来源运行在后台按它补比一次（不挡发布）
    from app.ledger import recall as ledger_recall
    from app.ledger.certify import after_publish

    after_publish(workflow.id, workflow.origin)
    # 口径召回（next-round 02 做法 7、拍板 L6）：用过旧版本的答案在后台按计划排队重算（每次最多 recall_limit 组），失败不影响发布
    ledger_recall.after_publish(workflow.id, snapshot.version, actor)
    return {
        "ok": True,
        "level": level,
        "version": snapshot.version,
        "graph_hash": snapshot.graph_hash,
        "published_by": workflow.published_by,
        "catalog_versions": recorded_versions(snapshot.catalog_versions),
    }


@router.get("/{workflow_id}/recall-preview")
async def recall_preview(workflow_id: str, version: int | None = None,
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """发布弹窗的召回预告（next-round 02 做法 7、8）：把 version（缺省是最新保存的那一版）发布成新版本之后，旧版本被多少问数
    回答、导出、定时任务、应用用过，预计自动重算几组，超出上限的几组（「另有 M 组未重算」），不能重算的原因各几组。和发布之后
    真的召回走同一份计划（ledger/recall.build_plan），规模一致。只读，不发起。"""
    from app.ledger import recall as ledger_recall

    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    return await ledger_recall.preview(session, workflow_id, int(version or workflow.version))


# --------------------------------------------------------------------------
# 发布前检查与自动修复：两个接口都只读，不改库、不发布
# --------------------------------------------------------------------------

class PublishCheckIn(BaseModel):
    level: str = "published"
    #: 画布上还没保存的图。不带就用库里当前的草稿
    graph: dict[str, Any] | None = None


class AutofixIn(PublishCheckIn):
    #: 要应用的修复 id（publish-check 给的 fixes[].id）
    apply: list[str] = Field(default_factory=list)
    #: choice 类修复的选择：{fix_id: 值}，可以多选的给列表
    choices: dict[str, Any] = Field(default_factory=dict)
    #: 剩下的 error 交给 Copilot 试着修（结果同样只是预览）
    assist: bool = False
    provider: str | None = None
    model: str | None = None


async def _draft(session: AsyncSession, workflow_id: str, payload: PublishCheckIn) -> dict[str, Any]:
    if payload.level not in _LEVELS:
        raise HTTPException(400, _BAD_LEVEL)
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    return payload.graph if payload.graph is not None else (workflow.graph or {})


async def _gate_context(session: AsyncSession, spec: GraphSpec) -> tuple[dict[str, Any], dict[str, Any]]:
    """修复要用、但门禁本身不查库的东西：子工作流当前的发布版本，钉在别处的口径卡有哪些指标。"""
    versions: dict[str, Any] = {}
    cards: dict[str, Any] = {}
    for node in spec.nodes:
        cfg = node.config
        if node.type == NodeType.SUBGRAPH and cfg.get("workflow_id") and str(cfg["workflow_id"]) not in versions:
            upstream = await session.get(Workflow, str(cfg["workflow_id"]))
            versions[str(cfg["workflow_id"])] = upstream.published_version if upstream else None
        elif node.type == NodeType.METRICS and isinstance(cfg.get("caliber_from"), dict):
            ref = cfg["caliber_from"]
            version = ref.get("workflow_version")
            if not ref.get("workflow_id") or not str(version or "").isdigit():
                continue
            snapshot = (await session.execute(select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == str(ref["workflow_id"]),
                WorkflowVersion.version == int(version)))).scalar_one_or_none()
            card = next((n for n in ((snapshot.graph if snapshot else None) or {}).get("nodes") or []
                         if isinstance(n, dict) and n.get("id") == ref.get("node_id")), None)
            if card is not None:
                cards[node.id] = ((card.get("data") or {}).get("config") or {}).get("metrics") or []
    return versions, cards


async def _sql_checkers(session: AsyncSession, spec: GraphSpec) -> dict[str, Any]:
    """图里调用工具节点写死的 SQL 用到的数据源，各建一个基于数据目录的 SQL 检查器。发布、发布前检查、
    自动修复都传这一份给门禁（governance.lint_for_publish）：门禁本身不查库，三处口径才一致。"""
    from app.data.sqlcheck import graph_sources, load_checkers_by_name

    return await load_checkers_by_name(session, graph_sources(spec.nodes))


async def _providers(session: AsyncSession, spec: GraphSpec) -> list[Any]:
    """模型接入表：节点点名的接入不存在或已停用、只写了模型而提供它的接入已停用时，发布、发布前检查、自动修复的复核
    给 error、挡住发布，修复从启用的接入里列候选（engine/provider_refs.py、autofix 的 provider.*）。和 _sql_checkers
    一样由接口这一层读好交给门禁，门禁本身不查库；三处传同一份。图里取模型的几处都没写接入和模型时不读库。"""
    return await load_providers(session, spec)


def _unreadable(level: str, e: Exception) -> dict[str, Any]:
    return {"level": level, "ok": False, "fixes": [], "issues": [
        {"level": "error", "node_id": None, "edge_id": None, "field": None, "code": None, "fix": None,
         "message": f"无法解析工作流结构：{graph_error(e)}"}]}


@router.post("/{workflow_id}/publish-check")
async def publish_check(
    workflow_id: str, payload: PublishCheckIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """发布前检查：和真正发布同一套口径（validate + 门禁）列出问题，以及能怎么修。

    只读：不改库、不发布。发布弹窗一打开就调它，不用等点了发布才知道被拦。
    """
    return await check_graph(session, await _draft(session, workflow_id, payload), payload.level)


async def check_graph(session: AsyncSession, graph: Any, level: str) -> dict[str, Any]:
    """发布前检查的本体：问题加上能怎么修（每条问题带 fix 的种类 auto / choice / assist）。只读。
    发布前检查接口和发布单的预检共用（发布单按修复的种类把问题分给「你来定」「助手可以修」）。"""
    from app.engine.autofix import check

    try:
        spec = GraphSpec.model_validate(graph)
    except Exception as e:  # noqa: BLE001
        return _unreadable(level, e)
    versions, cards = await _gate_context(session, spec)
    checkers = await _sql_checkers(session, spec)
    providers = await _providers(session, spec)
    with with_subgraphs(await load_subgraphs(session, spec)):
        out = check(spec, level=level, versions=versions, cards=cards, checkers=checkers, providers=providers)
    # 这一版直接引用的表上还没处理的结构变化（next-round 03 B1）：发布弹窗照别的警告一样列出来，不拦发布
    out["issues"] = [*out["issues"], *await open_structure_issues(session, spec)]
    return out


@router.post("/{workflow_id}/autofix")
async def autofix(
    workflow_id: str, payload: AutofixIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """应用选中的修复，返回修复后的整张图——只是预览。

    不自动保存、不自动发布：人看过逐项变更再确认，前端走现有的保存接口存草稿，再检查、再发布。
    每条修复都复核过（降低要求的、错误没变少或者冒出新错误的丢弃并写明原因）。assist 为真时，
    剩下的 error 交给 Copilot，同一道复核通过才采纳。
    """
    from app.engine.autofix import apply_fixes, diff_changes

    graph = await _draft(session, workflow_id, payload)
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception as e:  # noqa: BLE001
        bad = _unreadable(payload.level, e)
        return {"graph": graph, "changes": [], "ops": [], "applied": [], "rejected": [
            {"fix_id": fid, "reason": "无法解析工作流结构，不能应用修复"} for fid in payload.apply],
            "remaining": bad["issues"], "fixes": [], "handoff": [], "assist": None, "ok": False}
    versions, cards = await _gate_context(session, spec)
    checkers = await _sql_checkers(session, spec)
    providers = await _providers(session, spec)
    # 和发布、发布前检查同一套口径：子工作流先读好，process.human_in_subgraph 才查得到
    subgraphs = await load_subgraphs(session, spec)
    with with_subgraphs(subgraphs):
        out = apply_fixes(graph, payload.apply, payload.choices, level=payload.level, versions=versions, cards=cards,
                          checkers=checkers, providers=providers)
    out["assist"] = None
    # 人在选项里选了「交给 Copilot」（handoff），和点「交给 Copilot」是一回事
    if payload.assist or out["handoff"]:
        if out["ok"]:
            out["assist"] = {"ok": True, "summary": "修复后已没有阻止发布的错误，未再交给助手", "questions": []}
            return out
        left = [i for i in out["remaining"] if i["level"] == "error"]
        if all(i.get("code") in PROVIDER_CODES for i in left):
            # 剩下的只有点名接入的问题：换成哪个接入要人拿主意（助手的自查也不看接入表），不交给助手
            fixable = any(i.get("fix") for i in left)
            out["assist"] = {"ok": False, "summary": PROVIDER_LEFT + (PROVIDER_LEFT_FIX if fixable else ""),
                             "questions": []}
            return out
        from app.api.copilot import assist_publish_fix

        helped = await assist_publish_fix(session, out["graph"], level=payload.level,
                                          provider=payload.provider, model=payload.model)
        out["assist"] = {"ok": helped["accepted"], "summary": helped["summary"], "questions": helped["questions"],
                         "looks": helped["looks"]}
        if helped["accepted"]:
            fixed = helped["graph"]
            out["changes"] += diff_changes(out["graph"], fixed, fix_id="assist", label="助手的修改")
            out["ops"] += helped["ops"]
            out["applied"].append("assist")
            fixed_spec = GraphSpec.model_validate(fixed)
            fixed_checkers = await _sql_checkers(session, fixed_spec)
            fixed_providers = await _providers(session, fixed_spec)
            with with_subgraphs(await load_subgraphs(session, fixed_spec)):
                after = apply_fixes(fixed, [], level=payload.level, versions=versions, cards=cards,
                                    checkers=fixed_checkers, providers=fixed_providers)
            out.update(graph=fixed, remaining=after["remaining"], fixes=after["fixes"], ok=after["ok"])
        elif helped["reason"]:
            out["rejected"].append({"fix_id": "assist", "reason": helped["reason"]})
    return out


class ValidateIn(BaseModel):
    graph: dict[str, Any]


@router.post("/validate")
async def validate(payload: ValidateIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """画布上的实时校验。前端每次改动都会调它来标红。

    点名的模型接入不存在或已停用（只写了模型、提供它的接入已停用也算）只给警告：探索运行仍可发起，执行到那个节点时
    照实报错；发布时才挡住。这时同一节点上「没有指定模型，将使用接入「X」的默认模型」和它矛盾，不再列
    （quiet_unset_model，发布一类在 governance._not_repeated 里同样处理）。"""
    try:
        spec = GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"无法解析工作流结构：{graph_error(e)}"}]}
    result = validate_graph(spec)
    found = await load_issues(session, spec, level="warning")
    result.issues = [*quiet_unset_model(spec, result.issues, found), *found]
    return result.model_dump()


@router.post("/variables")
async def variables(payload: ValidateIn) -> dict[str, Any]:
    """这张图里有哪些变量、谁产出、谁引用。

    纯静态分析，不用跑图也不用有运行记录——用户在编排到一半时最需要它，
    而那时候还没有任何一次运行可看。运行期的实际取值由前端从事件里补上。
    """
    from app.engine.variables import analyze

    try:
        spec = GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"variables": [], "issues": [{"level": "error", "message": f"无法解析工作流结构：{graph_error(e)}"}]}
    return analyze(spec).model_dump()
