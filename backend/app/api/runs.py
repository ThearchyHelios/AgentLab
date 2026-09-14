from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select as _select

from app.core.bus import bus
from app.core import artifact_store
from app.db.base import SessionLocal, get_session
from app.db.models import Approval, Artifact, Run, RunEvent, Workflow, WorkflowVersion
from app.engine.governance import unresolved_caliber_upgrades
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec

router = APIRouter(prefix="/api/runs", tags=["runs"])


class StartRunIn(BaseModel):
    workflow_id: str | None = None
    graph: dict[str, Any] | None = None  # 直接跑一张未保存的图（画布上的"试运行"）
    input: dict[str, Any] = Field(default_factory=dict)
    memory_scope: str = "default"
    collection: str = "default"
    # formal：从已发布的不可变版本发起，可复现可出具；exploratory：随便玩
    run_class: str = "exploratory"
    version: int | None = None  # formal 时可指定版本，默认用已发布版本


class RunOut(BaseModel):
    id: str
    workflow_id: str | None
    workflow_name: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None
    usage: dict[str, Any]
    run_class: str | None = "exploratory"
    version: int | None = None
    version_hash: str | None = None
    manifest_hash: str | None = None
    started_by: str | None = None
    created_at: Any = None
    started_at: Any = None
    finished_at: Any = None

    model_config = {"from_attributes": True}


@router.post("", response_model=RunOut, status_code=201)
async def start_run(
    payload: StartRunIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> Run:
    actor = (x_actor or "").strip() or None
    name = "临时图"
    version: int | None = None
    version_hash: str | None = None

    if payload.run_class == "formal":
        # 正式运行的全部语义就这一条规则：必须引用一个不可变的已发布版本。
        # 传裸 graph、或工作流还没发布过，都进不了 formal。
        if not payload.workflow_id:
            raise HTTPException(400, "正式运行必须指定 workflow_id")
        if payload.graph is not None:
            raise HTTPException(400, "正式运行不接受临时 graph——先保存并发布版本")
        workflow = await session.get(Workflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(404, "工作流不存在")
        if workflow.status not in ("published", "governed") or not workflow.published_version:
            raise HTTPException(409, f"「{workflow.name}」还没有发布版本，先在编排页发布")
        version = payload.version or workflow.published_version
        snapshot = (
            await session.execute(
                _select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow.id,
                    WorkflowVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if not snapshot:
            raise HTTPException(404, f"版本 v{version} 不存在")
        graph = snapshot.graph
        version_hash = snapshot.graph_hash or artifact_store.graph_hash(graph)
        name = workflow.name

        # 口径升版是被迫处置的事件：钉住的方法卡有新版本而未声明策略，拒绝启动
        spec = GraphSpec.model_validate(graph)
        errors, upgrade_events = await unresolved_caliber_upgrades(session, spec)
        if errors:
            raise HTTPException(409, "；".join(errors))
    else:
        graph = payload.graph
        upgrade_events = []
        if payload.workflow_id:
            workflow = await session.get(Workflow, payload.workflow_id)
            if not workflow:
                raise HTTPException(404, "工作流不存在")
            graph = graph or workflow.graph
            name = workflow.name
    if not graph:
        raise HTTPException(400, "需要提供 workflow_id 或 graph")

    try:
        run = await run_manager.start(
            graph=graph,
            input_payload=payload.input,
            workflow_id=payload.workflow_id,
            workflow_name=name,
            memory_scope=payload.memory_scope,
            collection=payload.collection,
            run_class="formal" if payload.run_class == "formal" else "exploratory",
            version=version,
            version_hash=version_hash,
            started_by=actor,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # 已声明策略的升版记入事件流，出具物上能看到"这期换口径了、怎么处置的"
    for ev in upgrade_events:
        payload_ev = dict(ev)
        node = payload_ev.pop("node_id", None)
        await run_manager.note(run.id, "caliber.upgrade", node_id=node, **payload_ev)
    return run


@router.get("", response_model=list[RunOut])
async def list_runs(
    workflow_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[Run]:
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if workflow_id:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    if status:
        stmt = stmt.where(Run.status == status)
    return list((await session.execute(stmt)).scalars())


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Run:
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return run


@router.get("/{run_id}/events")
async def get_events(
    run_id: str,
    after: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """拉历史事件。刷新页面后重建时间线用这个，再接 WebSocket 续上实时流。"""
    rows = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after)
        .order_by(RunEvent.seq)
    )
    return [
        {
            "id": e.id,
            "run_id": e.run_id,
            "seq": e.seq,
            "type": e.type,
            "node_id": e.node_id,
            "ts": e.ts,
            "data": e.data,
        }
        for e in rows.scalars()
    ]


@router.get("/{run_id}/artifacts")
async def list_artifacts(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    """这次运行产出的全部工件引用。审阅出具物时从这里下钻到完整证据。"""
    rows = await session.execute(
        select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
    )
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "node_id": a.node_id,
            "size": a.size,
            "meta": a.meta,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows.scalars()
    ]


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    stopped = await run_manager.cancel(run_id)
    if not stopped:
        raise HTTPException(409, "该运行当前不在执行中")
    return {"ok": True}


class ResumeIn(BaseModel):
    response: Any = None
    approval_id: str | None = None


@router.post("/{run_id}/resume", response_model=RunOut)
async def resume_run(run_id: str, payload: ResumeIn) -> Run:
    try:
        return await run_manager.resume(run_id, payload.response)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/{run_id}/state")
async def get_state(run_id: str) -> dict[str, Any]:
    """读取 checkpoint 里的当前状态。调试中断中的 run 时很有用。"""
    from app.engine.compiler import compile_graph
    from app.engine.context import RunContext
    from app.engine.schema import GraphSpec

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "运行记录不存在")

    spec = GraphSpec.model_validate(run.graph)
    app = compile_graph(spec, RunContext(run_id=run_id, thread_id=run_id, spec=spec)).compile(
        checkpointer=run_manager.checkpointer
    )
    snapshot = await app.aget_state({"configurable": {"thread_id": run_id}})
    values = dict(snapshot.values or {})
    values.pop("messages", None)  # 消息对象不好直接序列化，前端也用不到
    return {
        "values": _jsonable(values),
        "next": list(snapshot.next or ()),
        "interrupts": [
            {"id": getattr(i, "id", None), "value": _jsonable(getattr(i, "value", None))}
            for i in (snapshot.interrupts or ())
        ],
    }


@router.get("/{run_id}/history")
async def get_history(run_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """checkpoint 历史 —— 可以看到每一步之后的状态快照，用来做时间旅行式调试。"""
    from app.engine.compiler import compile_graph
    from app.engine.context import RunContext
    from app.engine.schema import GraphSpec

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "运行记录不存在")

    spec = GraphSpec.model_validate(run.graph)
    app = compile_graph(spec, RunContext(run_id=run_id, thread_id=run_id, spec=spec)).compile(
        checkpointer=run_manager.checkpointer
    )
    out: list[dict[str, Any]] = []
    async for snapshot in app.aget_state_history({"configurable": {"thread_id": run_id}}):
        values = dict(snapshot.values or {})
        values.pop("messages", None)
        out.append(
            {
                "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
                "next": list(snapshot.next or ()),
                "created_at": snapshot.created_at,
                "trail": _jsonable(values.get("trail") or [])[-1:],
                "nodes_done": list((values.get("nodes") or {}).keys()),
            }
        )
        if len(out) >= limit:
            break
    return out


@router.delete("/{run_id}", status_code=204)
async def delete_run(run_id: str, session: AsyncSession = Depends(get_session)) -> None:
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    await session.delete(run)
    await session.commit()


# --------------------------------------------------------------------------
# 实时事件流
# --------------------------------------------------------------------------


@router.websocket("/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    """把一次运行的事件推给前端。

    先补历史再接实时，所以中途打开页面、或者刷新之后，时间线都是完整的。
    """
    await websocket.accept()
    after = int(websocket.query_params.get("after", 0) or 0)

    try:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after)
                .order_by(RunEvent.seq)
            )
            for event in rows.scalars():
                await websocket.send_json(
                    {
                        "seq": event.seq,
                        "type": event.type,
                        "node_id": event.node_id,
                        "ts": event.ts,
                        "data": event.data,
                        "replay": True,
                    }
                )
            run = await session.get(Run, run_id)

        # 已经结束的 run 没有后续事件，补完历史就关掉
        if run and run.status in ("succeeded", "failed", "cancelled"):
            await websocket.send_json({"type": "stream.end", "status": run.status})
            await websocket.close()
            return

        stream = bus.subscribe(run_id)
        pump = asyncio.create_task(_pump(websocket, stream))
        try:
            # 客户端断开时读操作会抛异常，用它来结束这条连接
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await websocket.close()


async def _pump(websocket: WebSocket, stream: Any) -> None:
    try:
        async for event in stream:
            await websocket.send_json(event.to_wire())
    except (WebSocketDisconnect, RuntimeError):
        return


# --------------------------------------------------------------------------
# 审批
# --------------------------------------------------------------------------

approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    node_id: str
    mode: str
    title: str
    payload: dict[str, Any]
    status: str
    response: dict[str, Any]
    created_at: Any = None

    model_config = {"from_attributes": True}


@approvals_router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    status: str = "pending",
    run_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[Approval]:
    stmt = select(Approval).order_by(Approval.created_at.desc()).limit(100)
    if status != "all":
        stmt = stmt.where(Approval.status == status)
    if run_id:
        stmt = stmt.where(Approval.run_id == run_id)
    return list((await session.execute(stmt)).scalars())


class DecideIn(BaseModel):
    approved: bool = True
    note: str = ""
    value: Any = None
    args: dict[str, Any] | None = None


@approvals_router.post("/{approval_id}/decide", response_model=RunOut)
async def decide(
    approval_id: str,
    payload: DecideIn,
    session: AsyncSession = Depends(get_session),
    x_actor: str | None = Header(default=None),
) -> Run:
    approval = await session.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404, "审批请求不存在")
    if approval.status != "pending":
        raise HTTPException(409, "该请求已经处理过了")

    # 责任归属先落库再恢复执行——恢复失败也要留下"谁试图批的"
    approval.resolved_by = (x_actor or "").strip() or None
    await session.commit()

    response: dict[str, Any] = {"approved": payload.approved, "note": payload.note}
    if payload.value is not None:
        response["value"] = payload.value
    if payload.args is not None:
        response["args"] = payload.args

    try:
        return await run_manager.resume(approval.run_id, response)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
