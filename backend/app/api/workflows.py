from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import graph_error
from app.api.runs import actor_of
from app.core.artifact_store import graph_hash
from app.db.base import get_session
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.governance import lint_for_publish
from app.engine.schema import GraphSpec, validate_graph

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_GONE = "这个工作流不存在，可能已经被删了"


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
        raise HTTPException(409, f"这个工作流还有 {live} 次运行在执行，现在删除它们会失去归属。先停止运行，再删除")
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
        raise HTTPException(404, f"「{workflow.name}」没有 v{version} 这个版本")

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
        published=workflow.published_version == snapshot.version,
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
        raise HTTPException(404, f"没有 v{version} 这个版本")

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
    if payload.level not in ("published", "governed"):
        raise HTTPException(400, "发布档位只能选「已发布」或「受管」")
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
        raise HTTPException(404, f"没有 v{version} 这个版本：画布上的改动还没保存，先保存一次再发布")

    try:
        spec = GraphSpec.model_validate(snapshot.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"工作流的结构读不懂：{graph_error(e)}"}]}

    issues = [i.model_dump() for i in validate_graph(spec).issues]
    issues += [i.model_dump() for i in lint_for_publish(spec, level=payload.level).issues]
    if any(i["level"] == "error" for i in issues):
        return {"ok": False, "level": payload.level, "version": version, "issues": issues}

    if not snapshot.graph_hash:
        snapshot.graph_hash = graph_hash(snapshot.graph)
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


class ValidateIn(BaseModel):
    graph: dict[str, Any]


@router.post("/validate")
async def validate(payload: ValidateIn) -> dict[str, Any]:
    """画布上的实时校验。前端每次改动都会调它来标红。"""
    try:
        spec = GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"工作流的结构读不懂：{graph_error(e)}"}]}
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
        return {"variables": [], "issues": [{"level": "error", "message": f"工作流的结构读不懂：{graph_error(e)}"}]}
    return analyze(spec).model_dump()
