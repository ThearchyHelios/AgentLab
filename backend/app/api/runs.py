from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any
from urllib.parse import unquote

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


def actor_of(header: str | None) -> str | None:
    """请求头 X-Actor 里的署名（设置页填的那个），没填就是 None。

    浏览器的请求头只能是 Latin-1：中文署名原样放进去，fetch 直接抛错、整个请求
    发不出去。所以前端按 encodeURIComponent 编码后发，这里解回来；老客户端发的
    纯 ASCII 署名解码前后一样。
    """
    value = unquote((header or "").strip()).strip()
    return value[:100] or None


class StartRunIn(BaseModel):
    workflow_id: str | None = None
    graph: dict[str, Any] | None = None  # 直接跑一张未保存的图（画布上的"试运行"）
    input: dict[str, Any] = Field(default_factory=dict)
    # 不传就取设置里的运行默认值（settings.resolve_run_scope）
    memory_scope: str | None = None
    collection: str | None = None
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
    #: 封存到了哪一条事件。为空 = 还没封存（没跑完或停在审批）
    manifest_seq: int | None = None
    started_by: str | None = None
    #: 失败时能定位到的节点
    error_node_id: str | None = None
    #: 发起时实际生效的记忆域和知识库
    memory_scope: str | None = None
    collection: str | None = None
    # 时间一律带时区（db.base.UTCDateTime 读出来就是 UTC aware）
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
    actor = actor_of(x_actor)
    name = "临时图"
    version: int | None = None
    version_hash: str | None = None

    if payload.run_class == "formal":
        # 正式运行的全部语义就这一条规则：必须引用一个不可变的已发布版本。
        # 传裸 graph、或工作流还没发布过，都进不了 formal。
        if not payload.workflow_id:
            raise HTTPException(400, "正式运行要从一个已保存的工作流发起：请求里没有指定工作流")
        if payload.graph is not None:
            raise HTTPException(400, "正式运行只跑已发布的版本，不接受画布上的临时图——先保存并发布")
        workflow = await session.get(Workflow, payload.workflow_id)
        if not workflow:
            raise HTTPException(404, "找不到这个工作流，可能已经被删除了")
        # 只看 published_version：status 反映的是当前画布（改过图就退回 draft），
        # 而 formal 跑的是已发布的那一版，两者本来就可以不一致
        if not workflow.published_version:
            raise HTTPException(409, f"「{workflow.name}」还没有发布版本，先在编排页发布")
        version = payload.version or workflow.published_version
        # "不可变"和"过过闸"是两件事。快照确实不可变，但 PATCH 保存出来的
        # 草稿版本同样有快照——只认 version 存不存在的话，工作流只要发布过
        # 任意一个版本，之后所有草稿都能以 formal 身份启动，发布期的
        # validate_graph + lint_for_publish 全部绕过。
        if version != workflow.published_version:
            raise HTTPException(
                409,
                f"v{version} 不是当前发布版本（v{workflow.published_version}）。"
                "正式运行只能引用发布过的版本——要跑这一版就先发布它。",
            )
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
                raise HTTPException(404, "找不到这个工作流，可能已经被删除了")
            graph = graph or workflow.graph
            name = workflow.name
    if not graph:
        raise HTTPException(400, "没有要运行的内容：请求里既没有工作流，也没有图")

    from app.api.settings import resolve_run_scope

    memory_scope, collection = await resolve_run_scope(
        session, payload.memory_scope, payload.collection
    )
    try:
        run = await run_manager.start(
            graph=graph,
            input_payload=payload.input,
            workflow_id=payload.workflow_id,
            workflow_name=name,
            memory_scope=memory_scope,
            collection=collection,
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
    status: str | None = Query(default=None, description="逗号分隔，如 failed,cancelled"),
    run_class: str | None = None,
    q: str | None = Query(default=None, description="按工作流名模糊匹配"),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(default=None, description="翻页游标：只要 created_at 早于它的"),
    session: AsyncSession = Depends(get_session),
) -> list[Run]:
    """运行列表，新的在前。

    以前只有 limit：待审批的运行排在第 145 位，列表只取 100 条，点了徽标也找不到它。
    筛选都走服务端，翻页用 created_at 做游标（before 传上一页最后一条的 created_at）。
    """
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if workflow_id:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    if statuses := _csv(status):
        stmt = stmt.where(Run.status.in_(statuses))
    if classes := _csv(run_class):
        stmt = stmt.where(Run.run_class.in_(classes))
    if q and q.strip():
        stmt = stmt.where(Run.workflow_name.ilike(f"%{_like(q.strip())}%", escape="\\"))
    if before is not None:
        stmt = stmt.where(Run.created_at < before)
    return list((await session.execute(stmt)).scalars())


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _like(text: str) -> str:
    """用户输入里的 % 和 _ 按字面匹配，不当通配符。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> RunOut:
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    out = RunOut.model_validate(run)
    if out.status == "failed" and not out.error_node_id:
        # 加这一列之前失败的老运行：从事件里找最后一个失败的节点
        out.error_node_id = (await session.execute(
            select(RunEvent.node_id)
            .where(RunEvent.run_id == run_id, RunEvent.type == "node.failed")
            .order_by(RunEvent.seq.desc()).limit(1)
        )).scalar_one_or_none()
    return out


@router.get("/{run_id}/graph")
async def get_run_graph(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """运行时的那张图（快照），不是工作流现在的样子。回放历史运行时画布照它画。"""
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return {"graph": run.graph or {}, "workflow_id": run.workflow_id, "version": run.version}


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
        raise HTTPException(
            409, "这次运行已经不在执行中（可能刚结束，或者服务重启过），不需要再停止。刷新看看最新状态"
        )
    return {"ok": True}


class ResumeIn(BaseModel):
    response: Any = None
    approval_id: str | None = None


def _missing(e: KeyError) -> HTTPException:
    # str(KeyError) 会带上 repr 的引号，界面上就成了 "'找不到运行 x'"
    return HTTPException(404, str(e.args[0]) if e.args else "运行记录不存在")


@router.post("/{run_id}/resume", response_model=RunOut)
async def resume_run(
    run_id: str, payload: ResumeIn, x_actor: str | None = Header(default=None)
) -> Run:
    try:
        # approval_id 以前定义了却从没传下去——一次运行里有多条待审批时，
        # 引擎无从知道这次回复的是哪一条
        return await run_manager.resume(
            run_id, payload.response,
            approval_id=payload.approval_id,
            actor=actor_of(x_actor),
        )
    except KeyError as e:
        raise _missing(e) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


class ContinueIn(BaseModel):
    """接着跑。graph 只能带改过配置的同一张图，结构必须一致。"""

    graph: dict[str, Any] | None = None


@router.post("/{run_id}/continue", response_model=RunOut)
async def continue_run(
    run_id: str, payload: ContinueIn, x_actor: str | None = Header(default=None)
) -> Run:
    """从失败的节点接着跑，前面跑过的不重来。

    和 /resume 不是一回事：那条回复人工审批，这条是真的挂了（status=failed），
    或者被服务重启打断、没有在等谁的 interrupted。挡住前者的原来只是 resume 里的
    一行状态判定，而 checkpoint 一直都在——改个模型 id 就得把前面两分钟的取数
    重跑一遍，纯属白等。
    """
    try:
        return await run_manager.continue_failed(
            run_id, graph=payload.graph, actor=actor_of(x_actor),
        )
    except KeyError as e:
        raise _missing(e) from e
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


@router.get("/{run_id}/verify")
async def verify_run(run_id: str) -> dict[str, Any]:
    """核对事件流和封存时的清单哈希是否一致：事后被改过、删过、插过都会对不上。"""
    from app.engine.runner import verify_manifest

    try:
        return await verify_manifest(run_id)
    except KeyError as e:
        raise _missing(e) from e


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
async def delete_run(
    run_id: str, force: bool = False, session: AsyncSession = Depends(get_session)
) -> None:
    """删除运行记录。运行记录是出具的追溯依据，两种情况要拦：

    - 还在跑的：删了会留下没人管的执行任务，它还会往一条不存在的记录里写事件；
    - 封存过的正式运行：删掉之后清单再也无法核对。确实要删，带 force=true。
    """
    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    if run.status in ("queued", "running") or run_manager.is_active(run_id):
        raise HTTPException(409, "这次运行还在执行，现在删除会留下没人管的任务。先停止它，再删除")
    if run.run_class == "formal" and run.manifest_hash and not force:
        raise HTTPException(
            409,
            "这是一次已封存的正式运行，是出具结果的追溯凭证。删除后它的事件和工件会一并删掉，"
            "封存清单再也无法核对。确认要删的话请再确认一次（强制删除）",
        )
    await session.delete(run)
    await session.commit()


# --------------------------------------------------------------------------
# 实时事件流
# --------------------------------------------------------------------------


#: 不在进行中的状态：没有后续事件会来（interrupted 要等人回复，恢复会新开一段
#: 执行，客户端到时候重新接）
_SETTLED = ("succeeded", "failed", "cancelled", "interrupted")


@router.websocket("/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    """把一次运行的事件推给前端。

    先补历史再接实时，所以中途打开页面、或者刷新之后，时间线都是完整的。
    运行一旦不在进行中，补完历史就发一条 stream.end（data.status 是当时的状态）
    再关掉——interrupted 也发。以前停在审批或服务重启挂起的运行什么都不发，
    客户端一直重连、一直以为它还在跑。
    """
    await websocket.accept()
    after = int(websocket.query_params.get("after", 0) or 0)

    # 先占订阅位，再读历史。反过来的话，两步之间图正好跑出来的事件既不在
    # 历史里、也不在实时流里——永远丢了。实测就是这样：一次跑图的开头
    # 两三条 node.started/node.finished 消失，界面上表现为某个节点永远
    # 在转圈、它的子步骤跑到了顶层。
    subscription = bus.attach(run_id)
    try:
        last_seq, run = await _replay(websocket, run_id, after)
        if run is not None and run.status in _SETTLED and run_manager.is_active(run_id):
            # 状态已经落了、执行任务还在收尾（终态事件、封存）：等它收完，把这期间
            # 落库的事件补上，按最终状态收尾。否则会在 run.finished 之前就宣布结束
            await run_manager.wait_idle(run_id)
            last_seq, run = await _replay(websocket, run_id, last_seq)

        if run is None or run.status in _SETTLED:
            await _end(websocket, run.status if run else None)
            return

        # 占位期间攒下的那几条历史里也有，按 seq 滤掉，否则前端同一步收两遍
        pump = asyncio.create_task(_pump(websocket, subscription.stream(after=last_seq), run_id))
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
    finally:
        subscription.close()


async def _replay(websocket: WebSocket, run_id: str, after: int) -> tuple[int, Run | None]:
    """把 seq > after 的历史事件发出去，顺带取回运行当前的状态。"""
    last_seq = after
    async with SessionLocal() as session:
        rows = await session.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after)
            .order_by(RunEvent.seq)
        )
        for event in rows.scalars():
            last_seq = max(last_seq, event.seq or 0)
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
    return last_seq, run


async def _end(websocket: WebSocket, status: str | None) -> None:
    # 顶层的 status 留给老客户端；新口径读 data.status
    await websocket.send_json({"type": "stream.end", "status": status, "data": {"status": status}})
    await websocket.close()


async def _pump(websocket: WebSocket, stream: Any, run_id: str) -> None:
    try:
        async for event in stream:
            await websocket.send_json(event.to_wire())
        # 实时流走完 = 运行到了终态，或者关停时挂起了：补一条结束标记再关，
        # 客户端不用靠猜，也不会对着一条已经结束的运行无限重连
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
        await _end(websocket, run.status if run else None)
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
    #: 谁批的：设置里的署名（请求头 X-Actor）。没署名就是 null，由前端写「未署名」
    resolved_by: str | None = None
    resolved_at: Any = None
    # 下面几项来自所属的运行。全局待办列表要拼得出一行像样的字：
    # 哪个工作流、卡在哪个节点、运行现在什么状态
    workflow_id: str | None = None
    workflow_name: str | None = None
    node_label: str | None = None
    run_status: str | None = None
    run_class: str | None = None


@approvals_router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    status: str = Query(default="pending", description="pending / resolved / all，可逗号分隔"),
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalOut]:
    stmt = (
        select(Approval, Run)
        .join(Run, Run.id == Approval.run_id, isouter=True)
        .order_by(Approval.created_at.desc())
        .limit(limit)
    )
    if status != "all" and (statuses := _csv(status)):
        stmt = stmt.where(Approval.status.in_(statuses))
    if run_id:
        stmt = stmt.where(Approval.run_id == run_id)
    out: list[ApprovalOut] = []
    for approval, run in (await session.execute(stmt)).all():
        item = ApprovalOut.model_validate(approval, from_attributes=True)
        if run is not None:
            item.workflow_id = run.workflow_id
            item.workflow_name = run.workflow_name
            item.run_status = run.status
            item.run_class = run.run_class
            item.node_label = _node_label(run.graph, approval.node_id)
        out.append(item)
    return out


def _node_label(graph: dict[str, Any] | None, node_id: str) -> str | None:
    for n in (graph or {}).get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            return ((n.get("data") or {}).get("label")) or node_id
    return None


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
        raise HTTPException(404, "找不到这条审批，所属的运行可能已经被删除了")
    if approval.status != "pending":
        raise HTTPException(409, "这条审批已经处理过了，刷新看看最新状态")

    # 责任归属先落库再恢复执行——恢复失败也要留下"谁试图批的"
    approval.resolved_by = actor_of(x_actor)
    await session.commit()

    response: dict[str, Any] = {"approved": payload.approved, "note": payload.note}
    if payload.value is not None:
        response["value"] = payload.value
    if payload.args is not None:
        response["args"] = payload.args

    try:
        return await run_manager.resume(
            approval.run_id, response,
            approval_id=approval.id,
            actor=approval.resolved_by,
        )
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
