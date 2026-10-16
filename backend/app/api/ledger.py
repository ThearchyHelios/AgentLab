"""账本（BE-3，只读）：工作流版本被哪些应用钉住、生效计数；生效记录查询。设计文档 §2.9 L4。

不另建存储：ActionRecord 本身带着工作流、版本、graph_hash、能力版本、payload_hash、回执；「口径 → 应用」就是固定这一版的
应用版本。用在发布弹窗、退役确认、版本列表三处。

口径账本（docs/design/next-round/02-口径账本.md）在这里加的接口：
- GET /api/runs/{run_id}/ledger（runs_router，和证据同一道门）：一次运行里每个数的口径标签、整题判断、每条查询的比对、能做的
  动作、出具过的正式运行、对过的账（app/ledger/view.py）；
- GET /api/ledger/stats（router，搭建者）：账本自身给出的统计 E1–E4，从比对工件、运行、审计现算（app/ledger/stats.py）；
- POST /api/ledger/answers/{run_id}/issue（answers_router，登录即可，第 3 波）：按口径正式出具（app/ledger/issue.py，拍板
  L1、L5：参数只从比对工件取，使用者只能按他有权使用的应用固定着的版本出具）；
- POST /api/ledger/answers/{run_id}/reconcile（同上）：口径对账，点开才跑一次确定性派生的只读图（app/ledger/reconcile.py）；
- GET /api/ledger/certify-queue、POST /api/ledger/certify-queue/dismiss（router，搭建者，第 3 波）：可沉淀的问题、「不是口径」
  （app/ledger/certify.py）。这几个新接口都不放给访问密钥（auth/keys.KEY_ROUTES 不登记）。
"""
from __future__ import annotations

from typing import Any

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Workflow
from app.db.models_process import ActionRecord

router = APIRouter(prefix="/api/ledger", tags=["ledger"])
#: 口径账本里按运行读的那几个（一个答案里每个数的口径标签等，docs/design/next-round/02-口径账本.md）：挂在
#: /api/runs/{run_id}/ledger… 下，和证据接口同一道门（main.py 挂 evidence_scope：使用者只读、而且要能读这次运行）
runs_router = APIRouter(prefix="/api/runs", tags=["ledger"])
#: 按口径正式出具、口径对账这类要写的（docs/design/next-round/02-口径账本.md 做法 5、6）：使用者也能发起（总方案拍板
#: 账本-L5：对账都可以，正式出具只限有权使用的应用固定着的口径版本），所以 main.py 只挂登录即可，接口里按运行和应用授权再判
answers_router = APIRouter(prefix="/api/ledger/answers", tags=["ledger"])


@router.get("/workflow-versions/{workflow_id}/{version}")
async def workflow_version(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                           ) -> dict[str, Any]:
    """一个工作流版本被哪些应用固定、生效记录数、正式运行数。"""
    from app.apps.versions import usage

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    return await usage(session, workflow_id, version)


def effect_row(r: ActionRecord, *, receipt: bool = True) -> dict[str, Any]:
    return {"id": r.id, "run_id": r.run_id, "node_id": r.node_id, "app_id": r.app_id, "workflow_id": r.workflow_id,
            "workflow_version": r.workflow_version, "graph_hash": r.graph_hash, "capability": r.capability,
            "capability_version": r.capability_version, "environment": r.environment, "state": r.state,
            "attempt": r.attempt, "payload_hash": r.payload_hash, "preview": r.preview,
            "receipt": r.receipt if receipt else None, "error": r.error, "resolution": r.resolution,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@router.get("/effects")
async def effects(workflow_id: str | None = None, version: int | None = None, app_id: str | None = None,
                  state: str | None = None, capability: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500), session: AsyncSession = Depends(get_session)
                  ) -> dict[str, Any]:
    """生效记录，新的在前。演练（simulated）照样列出，状态写明是演练。"""
    query = select(ActionRecord)
    if workflow_id:
        query = query.where(ActionRecord.workflow_id == workflow_id)
    if version is not None:
        query = query.where(ActionRecord.workflow_version == version)
    if app_id:
        query = query.where(ActionRecord.app_id == app_id)
    if state:
        query = query.where(ActionRecord.state == state)
    if capability:
        query = query.where(ActionRecord.capability == capability)
    rows = (await session.execute(query.order_by(ActionRecord.created_at.desc()).limit(limit))).scalars()
    return {"items": [effect_row(r) for r in rows]}


# --------------------------------------------------------------------------
# 口径账本（next-round 02）
# --------------------------------------------------------------------------


@runs_router.get("/{run_id}/ledger")
async def run_ledger(run_id: str) -> dict[str, Any]:
    """一次运行的口径账本：每个数来自哪一份口径（等价已证明 / 助手选用）、相关口径差在哪、这次的推理、没比对的原因；整题判断；
    每条查询和哪几份口径比过。比对在封存之后做（note 写明不在封存范围内）；没比对过、能比的运行这时排上按需比对，答复
    state=pending。绑定的值是这次运行查到过的遮罩值时不给值，差异条目里的 SQL 片段按同一套遮罩换掉。"""
    from app.ledger.view import run_view

    return await run_view(run_id)


@router.get("/stats")
async def ledger_stats(since: datetime | None = Query(default=None, description="只算这之后发起的运行（ISO 时间）"),
                       until: datetime | None = Query(default=None, description="只算这之前发起的运行"),
                       origin: str | None = Query(default=None, description="逗号分隔的运行来源，缺省是探索运行的几种"),
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """账本自身给出的统计 E1–E4（app/ledger/stats.compute_stats）：口径命中、对账、重复度、召回。全部现算，不另存；评测页
    「口径命中与重复度」直接调同一个函数。"""
    from app.api.deps import current_principal
    from app.ledger.stats import compute_stats

    origins = [o.strip() for o in (origin or "").split(",") if o.strip()] or None
    return await compute_stats(session, since=since, until=until, origins=origins, principal=current_principal())


class IssueIn(BaseModel):
    """按哪份口径出具：只能选口径，参数由服务端从比对工件里取（不收客户端传的参数）。"""

    workflow_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)


@answers_router.post("/{run_id}/issue")
async def issue_answer(run_id: str, payload: IssueIn) -> dict[str, Any]:
    """按口径正式出具这次问数据的答案（app/ledger/issue.py）：经 launch_run 发起正式运行，来源 ledger_issue。同一运行、同一
    口径、同一组参数只发起一次，再点交回已经发起的那一次（created 为假）。不能出具时回 409（整题不等价、版本退役、有人工
    节点或写入、缺入口字段……）、403（权限不够：使用者只能按他有权使用的应用固定着的版本出具）、429（超过每日上限），
    detail 写明原因。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import issue as ledger_issue

    try:
        run, created = await ledger_issue.issue(run_id, payload.workflow_id, payload.version,
                                                principal=current_principal())
    except ledger_issue.IssueRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code) from e
    return {"run_id": run.id, "created": created, "issued": ledger_issue.issued_view(run)}


class ReconcileIn(BaseModel):
    """对哪条查询、和哪份相关口径对账。inputs + confirm：参数要核对时（动作里 needs_check）人核对、改过的参数；pairs：认不出
    哪一列对哪个指标时人选的 [{ours: 这次的列, metric: 口径卡上的指标 id}]（动作里写明要不要选）。"""

    query: str = Field(min_length=1, max_length=64)
    workflow_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)
    inputs: dict[str, Any] | None = None
    confirm: bool = False
    pairs: list[dict[str, Any]] | None = None


@answers_router.post("/{run_id}/reconcile", status_code=201)
async def reconcile_answer(run_id: str, payload: ReconcileIn) -> dict[str, Any]:
    """口径对账（app/ledger/reconcile.py）：按那一版口径确定性派生只读图，发起一次探索运行（来源 reconcile），差值在对账卡
    里算。参数是推断出来的、不全的要先核对（409，code ledger_reconcile_check_inputs）；认不出列怎么配的要人选（409，
    ledger_reconcile_needs_pairs）；多行时维度是遮罩列、键不唯一照实拒绝（409）。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import reconcile as ledger_reconcile

    try:
        run = await ledger_reconcile.reconcile(run_id, ledger_reconcile.Request(
            query=payload.query, workflow_id=payload.workflow_id, version=payload.version, inputs=payload.inputs,
            confirm=payload.confirm, pairs=payload.pairs), principal=current_principal())
    except ledger_reconcile.ReconcileRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code, extra=e.extra) from e
    return {"run_id": run.id, "reconciled": ledger_reconcile.reconciled_view(run)}


@router.get("/certify-queue")
async def certify_queue(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """可沉淀的问题（认证队列，app/ledger/certify.py）：确定性重复的 SQL 形状、问题聚类、被反复用的答案。每项给代表问题、
    轮次、（形状的）参数候选和相关口径；点过「不是口径」的不再列。固化走 POST /api/copilot/from-run（freeze: true）。"""
    from app.api.deps import current_principal
    from app.ledger import certify

    return await certify.queue(session, principal=current_principal())


class DismissIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    kind: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


@router.post("/certify-queue/dismiss")
async def certify_dismiss(payload: DismissIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """「不是口径」：记一条审计（ledger.certify.dismiss），这一项之后不再进队。"""
    from app.ledger import certify

    if payload.kind is not None and payload.kind not in certify.KINDS:
        raise HTTPException(422, "可沉淀的问题只有三种：确定性重复、问题聚类、被反复用的答案")
    await certify.dismiss(session, payload.item_id, kind=payload.kind, note=payload.note)
    return {"ok": True, "item_id": payload.item_id}
