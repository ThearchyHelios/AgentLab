from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.schema import GraphSpec, validate_graph

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


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
        WorkflowVersion(workflow_id=workflow.id, version=1, graph=payload.graph, note="初始版本")
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
        raise HTTPException(404, "工作流不存在")
    return workflow


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str, payload: WorkflowPatch, session: AsyncSession = Depends(get_session)
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, "工作流不存在")

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
                note=payload.note,
            )
        )
    await session.commit()
    await session.refresh(workflow)
    return workflow


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, "工作流不存在")
    await session.delete(workflow)
    await session.commit()


@router.post("/{workflow_id}/duplicate", response_model=WorkflowOut, status_code=201)
async def duplicate_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> Workflow:
    source = await session.get(Workflow, workflow_id)
    if not source:
        raise HTTPException(404, "工作流不存在")
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


@router.post("/{workflow_id}/versions/{version}/restore", response_model=WorkflowOut)
async def restore_version(
    workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, "工作流不存在")
    snapshot = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not snapshot:
        raise HTTPException(404, "版本不存在")

    workflow.graph = snapshot.graph
    workflow.version += 1
    session.add(
        WorkflowVersion(
            workflow_id=workflow.id,
            version=workflow.version,
            graph=snapshot.graph,
            note=f"回滚到 v{version}",
        )
    )
    await session.commit()
    await session.refresh(workflow)
    return workflow


class ValidateIn(BaseModel):
    graph: dict[str, Any]


@router.post("/validate")
async def validate(payload: ValidateIn) -> dict[str, Any]:
    """画布上的实时校验。前端每次改动都会调它来标红。"""
    try:
        spec = GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "message": f"图结构非法：{e}"}]}
    result = validate_graph(spec)
    return result.model_dump()
