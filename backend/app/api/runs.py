from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
# 「人」的唯一入口搬到了 api/deps.py（开登录时返回认证过的用户名）。别处照旧从这里导入 actor_of
from app.api.deps import actor_of  # noqa: F401
from app.core.bus import bus
from app.db.base import SessionLocal, get_session
from app.db.models import Approval, Artifact, Run, RunEvent
from app.engine.inquiry import BUSY_CONTINUE, BUSY_DECIDE, INQUIRY_BUSY, inquiry_busy
# 发起规则和发起前检查绑定的工具搬到了 engine/launch.py。这几个名字以前从这里导入，照旧可用
from app.engine.launch import (  # noqa: F401
    TOOL_MISSING, LaunchRefused, launch_run, missing_tools, node_setting, tool_bindings,
)
from app.engine.runner import run_manager

router = APIRouter(prefix="/api/runs", tags=["runs"])

#: POST /api/runs 声明了客户端不能声明的来源（422）
ORIGIN_NOT_ALLOWED = "origin_not_allowed"
#: origin_ref（问数据的运行属于哪段对话、哪一轮）只在来源是问数据时收；对话不存在、这一轮不属于这段对话（422）
ORIGIN_REF_NOT_ALLOWED = "origin_ref_not_allowed"
ORIGIN_REF_INVALID = "origin_ref_invalid"
#: 删除被拒：这次运行写过外部系统（有演练以外的生效记录，409）
RUN_HAS_EFFECTS = "run_has_effects"




class OriginRef(BaseModel):
    """问数据的运行属于哪段对话、哪一轮（next-round 02 做法 9）。服务端核对对话这个人用不用得了、这一轮是不是这段对话的，
    写进 Run.origin 和封存的 run.origin 事件：运行和轮次之间从此有一条封存的链接（召回找答案的主人、统计按对话数重复都靠它），
    不再只靠前端写的 meta.runId。"""

    conversation_id: str = Field(min_length=1, max_length=32)
    turn_id: str = Field(min_length=1, max_length=32)


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
    #: 运行来源（engine/origin.py）：客户端只能声明 chat / canvas / launcher / api，不传按 api 记。
    #: 应用、钩子、定时任务发起的来源只能由服务端写
    origin: str | None = None
    #: 来源是问数据时，这次运行属于哪段对话、哪一轮（OriginRef）。别的来源带它回 422
    origin_ref: OriginRef | None = None


class RunOut(BaseModel):
    id: str
    workflow_id: str | None
    workflow_name: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None
    #: 结束在 cancelled 的原因（docs/design/next-round/00-总方案.md X9）：rejected 人工驳回、stopped 手动停止。
    #: 其余终态、加这一列之前的运行为空。界面、通知、定时推送都读这一个字段，不再解析报错原文
    end_reason: str | None = None
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
    #: 发起它的定时任务。为空 = 手动发起
    schedule_id: str | None = None
    #: 运行来源（engine/origin.py 的 ORIGIN_KINDS）。为空是加这一列之前的运行：来源未记录
    origin_kind: str | None = None
    #: run.origin 事件的数据（kind、app_id、entry / view、hook、actor、actor_verified……），和封存范围里那一条是同一份
    origin: dict[str, Any] | None = None
    #: 应用发起的运行属于哪个应用版本
    app_id: str | None = None
    app_version: int | None = None
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
    """发起规则全在 engine/launch.py（定时任务到点发起也用它），这里只把拒绝换回 HTTP 响应。"""
    from app.api.deps import restricted
    from app.engine.origin import CLIENT_ORIGINS

    origin = payload.origin or "api"
    if origin not in CLIENT_ORIGINS:
        # 应用入口、钩子、定时任务的来源由服务端写：客户端能声明的话，谁都能把一次画布试跑说成应用发起的
        raise CodedHTTPException(422, "运行来源只能是问数据、画布上的试运行、正式运行（手动）或接口，应用、外部系统和定时任务"
                                      "发起的运行由服务端记录来源", ORIGIN_NOT_ALLOWED, field="origin")
    if payload.origin_ref is not None and origin != "chat":
        raise CodedHTTPException(422, "只有问数据的运行能写明属于哪段对话、哪一轮", ORIGIN_REF_NOT_ALLOWED,
                                 field="origin_ref")
    origin_data = await _origin_ref(session, payload.origin_ref) if payload.origin_ref is not None else None
    options = None
    actor = actor_of(x_actor)
    memory_scope = payload.memory_scope
    if restricted():
        # user 角色（管理员开了问数据）只能发起问数据当场搭的探索运行：已发布工作流的正式运行只经应用入口发起，别的来源
        # 都是搭建者的事。图里只许读数据、整理、撰写这一类节点和只读工具（app/auth/chat_policy.py）。
        # 查询结果在工具边界就遮，事件、回答里只有遮过的值——它读得到自己这次运行的事件（api/deps.py 的 _run_scope）；
        # 数据源查询一律只读，写语句不交给审批（Agent 的 approval 是它自己写在图里的）
        if origin != "chat" or payload.run_class != "exploratory" or payload.workflow_id or payload.version is not None:
            raise HTTPException(403, "没有权限：使用者只能发起问数据的运行，工作流的运行需要搭建者及以上的角色")
        from app.auth.chat_policy import SCOPE_REFUSED
        from app.chat.launch import ChatRunRefused, user_chat_run

        # 白名单、记忆范围、遮罩与只读这几条和服务端跑的一轮问数据（chat/job.py）共用 chat/launch.user_chat_run
        try:
            memory_scope, options = user_chat_run(payload.graph, actor)
        except ChatRunRefused as e:
            raise HTTPException(403, str(e)) from e
        if payload.memory_scope or payload.collection:
            raise HTTPException(403, SCOPE_REFUSED)
    try:
        return await launch_run(
            session,
            workflow_id=payload.workflow_id,
            graph=payload.graph,
            input_payload=payload.input,
            memory_scope=memory_scope,
            collection=payload.collection,
            run_class=payload.run_class,
            version=payload.version,
            actor=actor,
            origin_kind=origin,
            origin=origin_data,
            options=options,
        )
    except LaunchRefused as e:
        raise _refused(e) from e


async def _origin_ref(session: AsyncSession, ref: OriginRef) -> dict[str, str]:
    """核对 origin_ref：对话这个人用不用得了（不能用的 403，规则同助手接口的 ensure_conversation_access）、对话在不在、这一轮
    是不是这段对话的。核对过的才写进运行来源：客户端不能把一次运行挂到别人的对话、别的轮次上。"""
    from app.api.deps import ensure_conversation_access
    from app.db.models import ConversationTurn

    conversation = await ensure_conversation_access(session, ref.conversation_id)
    if conversation is None:
        raise CodedHTTPException(422, "运行要挂的对话不存在，可能已被删除", ORIGIN_REF_INVALID, field="origin_ref")
    turn = await session.get(ConversationTurn, ref.turn_id)
    if turn is None or turn.conversation_id != conversation.id:
        raise CodedHTTPException(422, "运行要挂的这一轮不属于这段对话", ORIGIN_REF_INVALID, field="origin_ref")
    return {"conversation_id": conversation.id, "turn_id": turn.id}


def _refused(e: LaunchRefused) -> HTTPException:
    """发起被拒的 HTTP 响应，和规则搬去 engine/launch.py 之前逐字相同：带机器码的回 {detail, code}，
    不带的回 {detail}。"""
    if e.code:
        return CodedHTTPException(e.status_code, e.message, e.code)
    return HTTPException(e.status_code, e.message)


#: workflow_id 取这个值时只要没挂在任何工作流上的运行：画布上没保存就跑的临时图，
#: 以及工作流被删掉之后留下来的运行（外键是 SET NULL）
UNSAVED = "__none__"
#: origin_kind 取这个值时只要来源未记录的运行（加来源这一列之前的老运行）
NO_ORIGIN = "none"


@router.get("", response_model=list[RunOut])
async def list_runs(
    workflow_id: str | None = Query(default=None, description=f"{UNSAVED} 表示没保存的工作流"),
    status: str | None = Query(default=None, description="逗号分隔，如 failed,cancelled"),
    run_class: str | None = None,
    q: str | None = Query(default=None, description="按工作流名模糊匹配"),
    schedule_id: str | None = Query(default=None, description="只要这个定时任务发起的运行"),
    origin_kind: str | None = Query(default=None, description=f"逗号分隔的运行来源；{NO_ORIGIN} 表示来源未记录的老运行"),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(default=None, description="翻页游标：只要 created_at 早于它的"),
    session: AsyncSession = Depends(get_session),
) -> list[Run]:
    """运行列表，新的在前。

    以前只有 limit：待审批的运行排在第 145 位，列表只取 100 条，点了徽标也找不到它。
    筛选都走服务端，翻页用 created_at 做游标（before 传上一页最后一条的 created_at）。
    别人的个人应用刷新、个人蓝图试运行不列（只给拥有者，auth/access.hidden_runs），管理员也一样。
    没按来源筛时，系统派生的运行（评测、口径对账、口径召回：engine/origin.SYSTEM_ORIGINS）默认不列——几十道评测题的运行
    会把真实用户的运行挤出第一页；按来源点名就列。没列出几次由 GET /api/runs/unlisted 照实说，界面写在列表底下。
    """
    stmt = _filtered(select(Run), workflow_id=workflow_id, status=status, run_class=run_class, q=q,
                     schedule_id=schedule_id, origin_kind=origin_kind)
    stmt = stmt.order_by(Run.created_at.desc()).limit(limit)
    if before is not None:
        stmt = stmt.where(Run.created_at < before)
    return list((await session.execute(stmt)).scalars())


@router.get("/unlisted")
async def unlisted_runs(
    workflow_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    run_class: str | None = None,
    q: str | None = Query(default=None),
    schedule_id: str | None = Query(default=None),
    origin_kind: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """同样的筛选下，运行列表因为「系统派生的运行默认不列」而没列出的有几次：{total, by_origin: {来源: 次数}}。

    按来源筛了的（origin_kind 给了）什么都不藏，total 为 0。别人的个人应用刷新、个人蓝图试运行不算在内：它们不是「没列出」，
    是读不了。"""
    from sqlalchemy import func

    from app.engine.origin import SYSTEM_ORIGINS

    if _csv(origin_kind):
        return {"total": 0, "by_origin": {}}
    stmt = _filtered(select(Run.origin_kind, func.count()), workflow_id=workflow_id, status=status, run_class=run_class,
                     q=q, schedule_id=schedule_id, origin_kind=",".join(SYSTEM_ORIGINS))
    rows = (await session.execute(stmt.group_by(Run.origin_kind))).all()
    by_origin = {kind: int(n) for kind, n in rows if kind and n}
    return {"total": sum(by_origin.values()), "by_origin": by_origin}


def _filtered(stmt: Any, *, workflow_id: str | None, status: str | None, run_class: str | None, q: str | None,
              schedule_id: str | None, origin_kind: str | None) -> Any:
    """运行列表的筛选条件（列表和「没列出几次」共用一份）。"""
    from sqlalchemy import not_

    from app.api.deps import current_principal
    from app.auth.access import hidden_runs
    from app.engine.origin import counted_runs

    stmt = stmt.where(not_(hidden_runs(current_principal())))
    if workflow_id == UNSAVED:
        stmt = stmt.where(Run.workflow_id.is_(None))
    elif workflow_id:
        stmt = stmt.where(Run.workflow_id == workflow_id)
    if statuses := _csv(status):
        stmt = stmt.where(Run.status.in_(statuses))
    if classes := _csv(run_class):
        stmt = stmt.where(Run.run_class.in_(classes))
    if q and q.strip():
        stmt = stmt.where(Run.workflow_name.ilike(f"%{_like(q.strip())}%", escape="\\"))
    if schedule_id:
        stmt = stmt.where(Run.schedule_id == schedule_id)
    if origins := _csv(origin_kind):
        kinds = [o for o in origins if o != NO_ORIGIN]
        stmt = stmt.where(or_(Run.origin_kind.in_(kinds), *([Run.origin_kind.is_(None)] if NO_ORIGIN in origins else [])))
    else:
        # 没点名来源：只列计入统计的运行，系统派生的（评测、口径对账、召回）不列；来源为空的老运行照常列出
        stmt = stmt.where(counted_runs())
    return stmt


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
async def cancel_run(run_id: str, x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """停止一次运行。status 是 stopping（正在执行的，取消已发出，稍后收到 run.cancelled）
    或 cancelled（停在审批上、或服务重启挂起的，已经收成终态，待审批一并关闭）。"""
    outcome = await run_manager.cancel(run_id, actor=actor_of(x_actor))
    if outcome is None:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "运行记录不存在")
        raise HTTPException(409, _cannot_cancel(run.status, run.end_reason))
    return {"ok": True, "status": outcome}


def _cannot_cancel(status: str, end_reason: str | None = None) -> str:
    if status == "cancelled" and end_reason == "rejected":
        return "这次运行已被驳回结束，没有要停止的。"
    return {
        "succeeded": "这次运行已完成，没有要停止的。",
        "failed": "这次运行已失败结束，没有要停止的；如需继续，请点「继续运行」。",
        "cancelled": "这次运行已取消。",
    }.get(status, "这次运行已不在执行中（可能刚结束，或服务重启过），无需停止。请刷新页面查看最新状态")


class ResumeIn(BaseModel):
    response: Any = None
    approval_id: str | None = None


def _missing(e: KeyError) -> HTTPException:
    # str(KeyError) 会带上 repr 的引号，界面上就成了 "'运行记录不存在'"
    return HTTPException(404, str(e.args[0]) if e.args else "运行记录不存在")


def _not_while_inquiring(run_id: str, detail: str) -> None:
    """这次运行正在回答审批质询（engine/inquiry.py 的进程内登记）时，审批回复和继续运行一律 409：问答要写进
    运行停着的这一段，运行先往下走了，审批人看着回答再批的那份依据就落了空。"""
    if inquiry_busy(run_id):
        raise CodedHTTPException(409, detail, INQUIRY_BUSY)


@router.post("/{run_id}/resume", response_model=RunOut)
async def resume_run(
    run_id: str, payload: ResumeIn, x_actor: str | None = Header(default=None)
) -> Any:
    """兼容垫片：回复审批改走待办服务（app/process/tasks.py 的 respond，按派给谁、会签汇总）。「始终允许」、改过的代码、
    双盲裁决的选择原样交给节点。没有待审批的（服务重启挂起的运行从断点接着跑）照旧直接恢复。"""
    _not_while_inquiring(run_id, BUSY_DECIDE)
    target = payload.approval_id
    if target is None:
        async with SessionLocal() as session:
            pending = list((await session.execute(select(Approval.id).where(
                Approval.run_id == run_id, Approval.status == "pending"))).scalars())
        # 只有一条时才敢认；多条时交给引擎说清楚「无法确定回复的是哪一条」
        target = pending[0] if len(pending) == 1 else None
    if target is not None:
        async with SessionLocal() as session:
            approval = await session.get(Approval, target)
        if approval is not None and approval.run_id == run_id and approval.status == "pending":
            return await _respond_to(target, payload.response, x_actor)
    from app.engine.runner import ContinueRefused

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
    except ContinueRefused as e:
        # 被驳回的（run_rejected）：和 /continue 同一个机器码
        raise CodedHTTPException(409, str(e), e.code) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


async def _respond_to(approval_id: str, response: Any, x_actor: str | None) -> Any:
    """老接口的回复交给待办服务：只当一票、原样交给节点。待办还没有的（开待办时出过错）现在补一条。"""
    from app.api.tasks import principal_of, refused
    from app.process.tasks import RawResponse, TaskRefused, respond, task_for_approval

    task_id = await task_for_approval(approval_id)
    if task_id is None:
        raise HTTPException(404, "审批记录不存在，所属的运行可能已被删除")
    try:
        out = await respond(task_id, principal_of(x_actor), decision=RawResponse(response))
    except TaskRefused as e:
        error = refused(e)
        if isinstance(error, HTTPException):
            raise error from e
        return error
    return out["run"]


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
    _not_while_inquiring(run_id, BUSY_CONTINUE)
    from app.engine.runner import ContinueRefused

    try:
        return await run_manager.continue_failed(
            run_id, graph=payload.graph, actor=actor_of(x_actor),
        )
    except KeyError as e:
        raise _missing(e) from e
    except ContinueRefused as e:
        # 正式运行带图（formal_graph_locked）、写入结果未知还没核实（action_unresolved）、被驳回了的（run_rejected）：带机器码
        raise CodedHTTPException(409, str(e), e.code) from e
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
    """删除运行记录。运行记录是出具的追溯依据，三种情况要拦：

    - 还在跑的：删了会留下没人管的执行任务，它还会往一条不存在的记录里写事件；
    - 写过外部系统的（有演练以外的生效记录）：生效记录随运行级联删除，删了就对不上外部系统里已经发生的事（P8），
      停在「结果未知、等核实」上的连核实待办一起没了。force 也不放行；工作流的删除检查（F4）也就绕不过去；
    - 封存过的正式运行：删掉之后清单再也无法核对。确实要删，带 force=true。
    """
    from sqlalchemy import func

    from app.db.models import ActionRecord

    run = await session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    if run.status in ("queued", "running") or run_manager.is_active(run_id):
        raise HTTPException(409, "这次运行仍在进行，现在删除会留下无人管理的后台任务。请先停止运行，再删除")
    effects = await session.scalar(select(func.count(ActionRecord.id)).where(
        ActionRecord.run_id == run_id, ActionRecord.state != "simulated"))
    if effects:
        raise CodedHTTPException(
            409, f"这次运行有 {effects} 条写入外部系统的生效记录，是和外部系统对账的依据，不能删除（强制删除也不行）",
            RUN_HAS_EFFECTS)
    if run.run_class == "formal" and run.manifest_hash and not force:
        raise HTTPException(
            409,
            "这是一次已封存的正式运行，是出具结果的追溯凭证。删除后其事件和工件将一并删除，"
            "封存清单无法再核对。如仍要删除，请选择「强制删除」",
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
    #: 这条审批对应的待办（app/process/tasks.py）。回复走 POST /api/tasks/{task_id}/respond；开待办时出过错、还没补上的
    #: 为空，老接口 /decide 照旧能用（它会现补一条）
    task_id: str | None = None


@approvals_router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    status: str = Query(default="pending", description="pending / resolved / all，可逗号分隔"),
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalOut]:
    from sqlalchemy import not_

    from app.api.deps import current_principal
    from app.auth.access import hidden_runs
    from app.db.models import Task

    stmt = (
        select(Approval, Run, Task.id)
        .join(Run, Run.id == Approval.run_id, isouter=True)
        .join(Task, Task.approval_id == Approval.id, isouter=True)
        # 别人的个人应用刷新、个人蓝图试运行上的审批不列（只给拥有者，同运行列表）
        .where(or_(Run.id.is_(None), not_(hidden_runs(current_principal()))))
        .order_by(Approval.created_at.desc())
        .limit(limit)
    )
    if status != "all" and (statuses := _csv(status)):
        stmt = stmt.where(Approval.status.in_(statuses))
    if run_id:
        stmt = stmt.where(Approval.run_id == run_id)
    out: list[ApprovalOut] = []
    for approval, run, task_id in (await session.execute(stmt)).all():
        item = ApprovalOut.model_validate(approval, from_attributes=True)
        item.task_id = task_id
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
) -> Any:
    """兼容垫片：改走待办服务（app/process/tasks.py 的 respond）。请求体照旧，只当一票、原样交给节点。"""
    from app.api.deps import PRIVATE_RUN, can_read_run, current_principal

    approval = await session.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404, "审批记录不存在，所属的运行可能已被删除")
    if not await can_read_run(current_principal(), approval.run_id):
        raise HTTPException(403, PRIVATE_RUN)
    if approval.status != "pending":
        raise HTTPException(409, "这条审批已处理过，请刷新页面查看最新状态")
    # 在落签批人之前拦：拒掉的批准不该留下「谁试图批的」
    _not_while_inquiring(approval.run_id, BUSY_DECIDE)

    # 责任归属先落库再恢复执行——恢复失败也要留下"谁试图批的"
    approval.resolved_by = actor_of(x_actor)
    await session.commit()

    response: dict[str, Any] = {"approved": payload.approved, "note": payload.note}
    if payload.value is not None:
        response["value"] = payload.value
    if payload.args is not None:
        response["args"] = payload.args
    return await _respond_to(approval.id, response, x_actor)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
