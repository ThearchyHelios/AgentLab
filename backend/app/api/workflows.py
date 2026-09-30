from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import graph_error
from app.api.runs import actor_of
from app.core.artifact_store import graph_hash
from app.db.base import get_session
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec, NodeType, validate_graph

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_GONE = "工作流不存在，可能已被删除"
_LEVELS = ("published", "governed")
_BAD_LEVEL = "发布级别只能是「已发布」或「受管」"


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    graph: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


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
    created_at: Any = None
    updated_at: Any = None
    run_count: int = 0

    model_config = {"from_attributes": True}


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[WorkflowOut]:
    rows = list(
        (await session.execute(select(Workflow).order_by(Workflow.updated_at.desc()))).scalars()
    )
    counts = dict(
        (await session.execute(select(Run.workflow_id, func.count(Run.id)).group_by(Run.workflow_id))).all()
    )
    out: list[WorkflowOut] = []
    for row in rows:
        item = WorkflowOut.model_validate(row)
        item.run_count = counts.get(row.id, 0)
        out.append(item)
    return out


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowIn, session: AsyncSession = Depends(get_session)
) -> Workflow:
    workflow = Workflow(
        name=payload.name,
        description=payload.description,
        graph=payload.graph,
        tags=payload.tags,
    )
    session.add(workflow)
    await session.flush()
    session.add(
        WorkflowVersion(workflow_id=workflow.id, version=1, graph=payload.graph,
                        graph_hash=graph_hash(payload.graph), note="初始版本")
    )
    await session.commit()
    await session.refresh(workflow)
    return workflow


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)
    return workflow


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str, payload: WorkflowPatch, session: AsyncSession = Depends(get_session)
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)

    if payload.name is not None:
        workflow.name = payload.name
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
    return workflow


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
    await session.delete(workflow)
    await session.commit()


@router.post("/{workflow_id}/duplicate", response_model=WorkflowOut, status_code=201)
async def duplicate_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> Workflow:
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
    return copy


class VersionOut(BaseModel):
    id: str
    version: int
    note: str
    created_at: Any = None
    #: 这一版按哪一档发布的（published / governed）。没发布过、或者是记级别之前发布的为 null
    level: str | None = None

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
    return VersionDetailOut(
        id=snapshot.id, version=snapshot.version, note=snapshot.note,
        created_at=snapshot.created_at, workflow_id=workflow_id, graph=graph,
        graph_hash=snapshot.graph_hash or graph_hash(graph), input_fields=fields,
        published=workflow.published_version == snapshot.version, level=snapshot.level,
    )


@router.post("/{workflow_id}/versions/{version}/restore", response_model=WorkflowOut)
async def restore_version(
    workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
) -> Workflow:
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
    return workflow


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
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, _GONE)

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

    try:
        spec = GraphSpec.model_validate(snapshot.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"无法解析工作流结构：{graph_error(e)}"}]}

    issues = [i.model_dump() for i in publish_issues(spec, level=payload.level)]
    if any(i["level"] == "error" for i in issues):
        return {"ok": False, "level": payload.level, "version": version, "issues": issues}

    if not snapshot.graph_hash:
        snapshot.graph_hash = graph_hash(snapshot.graph)
    # 级别记在这一版上：之后改画布（status 退回 draft），从这一版发起的正式运行照样按发布时的级别出具
    snapshot.level = payload.level
    workflow.status = payload.level
    workflow.published_version = version
    workflow.published_by = actor_of(x_actor)
    await session.commit()
    return {
        "ok": True,
        "level": payload.level,
        "version": version,
        "graph_hash": snapshot.graph_hash,
        "published_by": workflow.published_by,
        "issues": issues,  # 剩下的都是警告
    }


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
    from app.engine.autofix import check

    graph = await _draft(session, workflow_id, payload)
    try:
        spec = GraphSpec.model_validate(graph)
    except Exception as e:  # noqa: BLE001
        return _unreadable(payload.level, e)
    versions, cards = await _gate_context(session, spec)
    return check(spec, level=payload.level, versions=versions, cards=cards)


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
    out = apply_fixes(graph, payload.apply, payload.choices, level=payload.level, versions=versions, cards=cards)
    out["assist"] = None
    # 人在选项里选了「交给 Copilot」（handoff），和点「交给 Copilot」是一回事
    if payload.assist or out["handoff"]:
        if out["ok"]:
            out["assist"] = {"ok": True, "summary": "修复后已没有阻止发布的错误，未再交给助手",
                             "questions": []}
            return out
        from app.api.copilot import assist_publish_fix

        helped = await assist_publish_fix(session, out["graph"], level=payload.level,
                                          provider=payload.provider, model=payload.model)
        out["assist"] = {"ok": helped["accepted"], "summary": helped["summary"], "questions": helped["questions"]}
        if helped["accepted"]:
            fixed = helped["graph"]
            out["changes"] += diff_changes(out["graph"], fixed, fix_id="assist", label="助手的修改")
            out["ops"] += helped["ops"]
            out["applied"].append("assist")
            after = apply_fixes(fixed, [], level=payload.level, versions=versions, cards=cards)
            out.update(graph=fixed, remaining=after["remaining"], fixes=after["fixes"], ok=after["ok"])
        elif helped["reason"]:
            out["rejected"].append({"fix_id": "assist", "reason": helped["reason"]})
    return out


class ValidateIn(BaseModel):
    graph: dict[str, Any]


@router.post("/validate")
async def validate(payload: ValidateIn) -> dict[str, Any]:
    """画布上的实时校验。前端每次改动都会调它来标红。"""
    try:
        spec = GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"无法解析工作流结构：{graph_error(e)}"}]}
    result = validate_graph(spec)
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
