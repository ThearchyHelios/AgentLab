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
- POST /api/ledger/answers/{run_id}/reconcile/{reconcile_id}/feedback（同上，第 4 波）：对账结果上的「说明有用 / 看不懂」
  （app/ledger/feedback.py，统计 E2）；
- GET /api/ledger/certify-queue、POST /api/ledger/certify-queue/dismiss（router，搭建者，第 3 波）：可沉淀的问题、「不是口径」
  （app/ledger/certify.py）。
- GET /api/ledger/recall/{workflow_id}/{version}、POST …/continue（router，搭建者，第 4 波）：口径升到这一版之后的召回批次——
  每组的结论、「另有 M 组未重算」、手动接着算（app/ledger/recall.py）。版本用量（workflow-versions）另加问数回答、导出、召回。
- GET /api/ledger/calibers、GET /api/ledger/calibers/{workflow_id}/{version}/{node_id}（router，搭建者）：口径可调用描述
  （app/ledger/descriptor.py）：入口字段的 JSON Schema、依赖、能不能组合和出具；不返回 SQL 和参数值。
- POST /api/ledger/drafts/extract、GET /api/ledger/drafts、GET/PATCH /api/ledger/drafts/{id}、POST …/{id}/dismiss、
  POST …/{id}/attach（router，搭建者）：从业务文档起草口径（app/ledger/docdraft.py），草稿独立存在、不写进
  workflows.graph；attach 才按一次真实运行补上取数路径（app/ledger/docattach.py）。
这几个新接口都不放给访问密钥（auth/keys.KEY_ROUTES 不登记）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, get_session
from app.db.models import CaliberDraft, Chunk, DataSource, Document, Run, Workflow
from app.db.models_process import ActionRecord

logger = logging.getLogger(__name__)

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
    """一个工作流版本被哪些应用固定、生效记录数、正式运行数；另加口径账本的用量（next-round 02 做法 8）：被几次问数回答用过
    （answers：比对里等价已证明的探索运行）、导出过几次（exports：这些答案和这一版的正式运行上的报告导出审计）、定时任务几个、
    召回（recall：以这一版为新版本召回过几组、其中数变了几组，作为旧版本被召回过几组）。"""
    from app.apps.versions import usage
    from app.ledger.uses import version_usage

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    base = await usage(session, workflow_id, version)
    extra = await version_usage(session, workflow_id, version)
    return {**base, "answers": extra["answers"], "exports": extra["exports"], "schedules": extra["schedules"],
            "recall": extra["recall"]}


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
    rows = (await session.execute(query.order_by(ActionRecord.created_at.desc(), ActionRecord.id.desc()).limit(limit))).scalars()
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


class FeedbackIn(BaseModel):
    """对账结果上的反馈：说明有用（useful）还是看不懂（unclear）。"""

    verdict: str = Field(min_length=1, max_length=20)


@answers_router.post("/{run_id}/reconcile/{reconcile_id}/feedback")
async def reconcile_feedback(run_id: str, reconcile_id: str, payload: FeedbackIn) -> dict[str, Any]:
    """对账结果上的「说明有用 / 看不懂」（app/ledger/feedback.py，统计 E2）：记成对账运行上的封存后事件，同一个人改主意只算
    最后一次。交回这次对账现在的反馈 {mine, useful, unclear}。不认的反馈 422；不是这个答案的对账 404；读不了这个答案 403；
    对账还没跑完 409。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import feedback

    try:
        out = await feedback.give(run_id, reconcile_id, payload.verdict, principal=current_principal())
    except feedback.FeedbackRefused as e:
        raise CodedHTTPException(e.status_code, e.message, e.code) from e
    return {"run_id": reconcile_id, "feedback": out}


@router.get("/recall/{workflow_id}/{version}")
async def recall_batches(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                         ) -> dict[str, Any]:
    """口径升到这一版之后的召回（app/ledger/recall.batches）：每组（旧版本、参数）的结论——数变了 / 没变 / 无法重算：原因 /
    重算失败 / 还在算 / 未重算——和「另有 M 组未重算」、能不能接着算。计划从已有的召回运行现算，不另存。"""
    from app.ledger import recall

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    return await recall.batches(session, workflow_id, version)


@router.post("/recall/{workflow_id}/{version}/continue")
async def recall_continue(workflow_id: str, version: int, session: AsyncSession = Depends(get_session)
                          ) -> dict[str, Any]:
    """接着算：把还没重算的组再发起一批（每批 recall_limit 组，上限是 0 时按缺省的组数），交回发起了的召回运行和新的批次
    情况。都算过了的、这一版已经不是当前发布版的（之后又发布了更新的版本）回 409，detail 写为什么。"""
    from app.api.coded import CodedHTTPException
    from app.api.deps import current_principal
    from app.ledger import recall

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    current = await recall.batches(session, workflow_id, version)
    if not current["can_continue"]:
        raise CodedHTTPException(409, current.get("continue_reason") or "没有还没重算的组", recall.REFUSED)
    launched = await recall.launch(workflow_id, version, batch="manual", actor=current_principal().username)
    async with SessionLocal() as fresh:
        view = await recall.batches(fresh, workflow_id, version)
    return {"launched": launched, **view}


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

    from app.api.deps import current_principal

    if payload.kind is not None and payload.kind not in certify.KINDS:
        raise HTTPException(422, "可沉淀的问题只有三种：确定性重复、问题聚类、被反复用的答案")
    await certify.dismiss(session, payload.item_id, kind=payload.kind, note=payload.note,
                          principal=current_principal())
    return {"ok": True, "item_id": payload.item_id}


@router.get("/calibers")
async def calibers(workflow_id: str | None = None, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """口径可调用描述（app/ledger/descriptor.py）：每个工作流最新「已发布且未退役」版本里的源头口径卡一张一条，含入口字段的
    JSON Schema、指标、依赖的表和发布时钉住的数据目录/结构版本、能不能组合、出具策略。workflow_id 可只列一个工作流。

    不返回取数 SQL、不返回参数值（SQL 里可能写着遮罩值）。issuance 只描述这一版的策略（级别、有没有人工节点或写外部系统），
    不代表这次运行一定能出具：全局自动出具开关、每日上限这类运行时条件不在这里判。"""
    from app.ledger.descriptor import list_calibers as list_descriptors

    return {"calibers": await list_descriptors(session, workflow_id=workflow_id)}


@router.get("/calibers/{workflow_id}/{version}/{node_id}")
async def caliber_descriptor(workflow_id: str, version: int, node_id: str,
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一张源头口径卡的描述（形状同列表里的一条）。工作流不存在、这一版没发布过或已退役、节点不是源头口径卡（转抄卡也算）
    回 404。同样不返回 SQL、参数值；issuance 只描述版本策略，不代表这次运行一定能出具。"""
    from app.ledger.descriptor import caliber_descriptor as descriptor_of

    made = await descriptor_of(session, workflow_id, version, node_id)
    if made is None:
        raise HTTPException(404, "没有这份口径：工作流不存在、这一版没发布过或已退役，或者节点不是源头口径卡")
    return made


class RuleCompileIn(BaseModel):
    """规则 DSL 编译：表 + 结构化条件 → 只读 SQL；fixtures 可选，用内存 SQLite 跑一遍。"""

    rule: dict[str, Any]
    table: str
    fixtures: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/rules/compile")
async def compile_rule_endpoint(payload: RuleCompileIn) -> dict[str, Any]:
    """把结构化规则编译成 SQL，并（给了 fixtures 时）用样例行验证。只生成 SELECT，不碰业务库。"""
    from app.data.rule_compile import RuleInvalid, compile_rule, run_fixtures

    try:
        sql = compile_rule(payload.rule, payload.table)
        checks = run_fixtures(payload.rule, payload.table, payload.fixtures)
    except RuleInvalid as e:
        raise HTTPException(422, str(e)) from None
    return {"sql": sql, "fixtures": checks}


class TrialIn(BaseModel):
    """试跑只收入口字段的值；没写的按口径里的缺省值补。"""

    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/calibers/{workflow_id}/{version}/{node_id}/trial")
async def trial_caliber(workflow_id: str, version: int, node_id: str, payload: TrialIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按固定版本试跑一份口径：派生只读小图（取数 + 口径卡），跑一次探索运行，返回指标值和运行 id。

    试跑不是正式出具：来源记 trial，不进使用统计、不自动出具；参数按入口 Schema 校验，缺的补缺省值。不能派生的口径
    （输入不是查库结果里的格、取数模板引用了运行时变量……）照实回 409。最多等 30 秒；还没跑完就回 running，前端按
    run_id 去看运行。"""
    from app.api.deps import current_principal
    from app.apps.forms import input_schema, validate_form
    from app.engine.runner import run_manager
    from app.ledger.derive import NotDerivable, caliber_probe, load_pinned

    try:
        pinned = await load_pinned(session, workflow_id, int(version), node_id)
    except NotDerivable as e:
        raise HTTPException(409, e.message) from None
    if not pinned.published or pinned.retired:
        raise HTTPException(409, "这一版没有发布或已退役，不能试运行")
    fields = [dict(f) for f in pinned.entry_fields]
    known = {str(f.get("name")) for f in fields}
    unknown = sorted(str(k) for k in payload.inputs if str(k) not in known)
    if unknown:
        raise HTTPException(422, f"不认识的入口字段：{'、'.join(unknown)}")
    values = {str(f["name"]): f.get("default") for f in fields if f.get("default") not in (None, "")}
    values.update({str(k): v for k, v in payload.inputs.items()})
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))

    graph = caliber_probe(pinned).graph
    principal = current_principal()
    run = await run_manager.start(
        graph=graph, input_payload=values, run_class="exploratory", origin_kind="caliber_trial",
        origin={"kind": "caliber_trial", "workflow_id": workflow_id, "version": int(version),
                "node_id": node_id, "card": pinned.node_id},
        started_by=principal.username)
    row = None
    for _ in range(300):
        async with SessionLocal() as poll:
            row = await poll.get(Run, run.id)
        if row is None or row.status in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)

    out: dict[str, Any] = {"run_id": run.id, "status": row.status if row is not None else "unknown",
                           "output": row.output if row is not None else None,
                           "error": row.error if row is not None else None, "metrics": []}
    if row is not None and row.status == "succeeded":
        from app.api.evidence import _sealed, _sealed_cards

        sealed = await _sealed(run.id)
        cards = {nid: card for nid, _, card in _sealed_cards(sealed)}
        card = cards.get(pinned.node_id) or next(iter(cards.values()), None)
        if card is not None:
            out["metrics"] = [
                {k: m.get(k) for k in ("id", "name", "unit", "value", "rendered", "decimals", "format")
                 if m.get(k) is not None}
                for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id")
            ]
    return out


# --------------------------------------------------------------------------
# 从业务文档起草口径（app/ledger/docdraft.py；attach 是 app/ledger/docattach.py）
# --------------------------------------------------------------------------

#: 草稿被驳回的审计（和 certify.dismiss 同一个写法：subject 是草稿 id）
DRAFT_DISMISS_ACTION = "ledger.docdraft.dismiss"
DRAFT_SUBJECT_KIND = "ledger_draft"


def _question_count(payload: dict[str, Any]) -> int:
    """未决问题数：跨指标的 open_questions + 各指标自己的。status 就按它算，PATCH 之后重算也用它。"""
    def count(items: Any) -> int:
        return len([q for q in items if isinstance(q, str) and q.strip()]) if isinstance(items, list) else 0

    total = count(payload.get("open_questions"))
    metrics = payload.get("metrics")
    for metric in metrics if isinstance(metrics, list) else []:
        if isinstance(metric, dict):
            total += count(metric.get("open_questions"))
    return total


def _status_for(payload: dict[str, Any]) -> str:
    """还有问题 → questions_open，否则 path_pending（等一次真实运行补取数路径）。"""
    return "questions_open" if _question_count(payload) else "path_pending"


def _draft_summary(draft: CaliberDraft) -> dict[str, Any]:
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    metrics = payload.get("metrics")
    return {"id": draft.id, "title": draft.title, "status": draft.status,
            "metric_count": len(metrics) if isinstance(metrics, list) else 0,
            "question_count": _question_count(payload), "workflow_id": draft.workflow_id,
            "created_at": draft.created_at.isoformat() if draft.created_at else None, "error": draft.error}


async def _draft_view(session: AsyncSession, draft: CaliberDraft) -> dict[str, Any]:
    """一份草稿的完整样子：payload 摊平在上层（界面直接读 metrics / open_questions），另加状态和关联工作流名。"""
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    workflow = await session.get(Workflow, draft.workflow_id) if draft.workflow_id else None
    return {**payload, **_draft_summary(draft),
            "collection": draft.collection, "datasource_id": draft.datasource_id,
            "origin": draft.origin if isinstance(draft.origin, dict) else {},
            "model_id": draft.model_id or payload.get("model_id"), "created_by": draft.created_by,
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "workflow_name": workflow.name if workflow is not None else None}


async def _draft_or_404(session: AsyncSession, draft_id: str) -> CaliberDraft:
    draft = await session.get(CaliberDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "口径草稿不存在，可能已被删除")
    return draft


async def _published_metrics(session: AsyncSession) -> list[dict[str, Any]]:
    """已发布口径里的每一项指标，给 docdraft.alignment 用：{workflow_id, version, node_id, name, synonyms, unit,
    definition_text}。直接复用 descriptor.list_calibers 的认法（只看最新已发布且未退役版本的源头口径卡）。"""
    from app.ledger.descriptor import list_calibers

    out: list[dict[str, Any]] = []
    for described in await list_calibers(session):
        for metric in described.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            definition = metric.get("definition")
            text = definition.get("text") if isinstance(definition, dict) else None
            name = metric.get("name") or metric.get("id")
            if not name:
                continue
            out.append({"workflow_id": described["workflow_id"], "version": described["version"],
                        "node_id": described["card"]["node_id"], "name": str(name),
                        "synonyms": metric.get("synonyms") if isinstance(metric.get("synonyms"), list) else [],
                        "unit": metric.get("unit") or "", "definition_text": text or ""})
    return out


def _doc_title(docs: list[Document]) -> str:
    """草稿标题取第一份文档的名字；多份文档写明还有几份。"""
    first = docs[0]
    title = (first.title or first.source or first.id).strip() or first.id
    return (f"{title}等 {len(docs)} 份文档" if len(docs) > 1 else title)[:200]


async def _extract_in_background(draft_id: str) -> None:
    """后台起草：读文档的段落、调一次模型、和已发布口径对齐，结果写回这一行。

    用新的会话：请求那一份在响应发出后就关了，后台接着用它写库是错的。失败写进 error、状态留在 drafting——
    草稿不是「被驳回」，人改完文档还能再来一次。
    """
    from app.ledger import docdraft

    async with SessionLocal() as session:
        draft = await session.get(CaliberDraft, draft_id)
        if draft is None or draft.status != "drafting":
            return
        origin = draft.origin if isinstance(draft.origin, dict) else {}
        wanted = [str(d) for d in origin.get("documents") or []]
        docs: list[Document] = []
        for doc_id in wanted:
            doc = await session.get(Document, doc_id)
            if doc is not None:
                docs.append(doc)
        chunks: dict[str, dict[str, Any]] = {}
        entries: list[dict[str, Any]] = []
        for doc in docs:
            rows = (await session.execute(select(Chunk).where(Chunk.document_id == doc.id)
                                          .order_by(Chunk.ordinal, Chunk.id))).scalars()
            chunk_ids: list[str] = []
            for chunk in rows:
                chunks[chunk.id] = {"ordinal": chunk.ordinal, "content": chunk.content or "",
                                    "document_id": doc.id}
                chunk_ids.append(chunk.id)
            entries.append({"document_id": doc.id, "title": doc.title or doc.source or doc.id,
                            "chunk_ids": chunk_ids})
        datasource = None
        if draft.datasource_id:
            source = await session.get(DataSource, draft.datasource_id)
            if source is not None:
                datasource = {"name": source.name, "type": source.kind, "description": source.description}
        try:
            result = await docdraft.extract(session, docs=entries, chunks=chunks,
                                            guidance=str(origin.get("guidance") or ""), datasource=datasource)
            # 对齐要扫所有已发布口径：某一份读不出来不该让整份草稿失败，照实不做对齐（alignment 三个空列表）
            try:
                existing = await _published_metrics(session)
            except Exception:  # noqa: BLE001 - 对齐是辅助信息，不是草稿本身
                logger.exception("读已发布口径失败，草稿 %s 不做对齐", draft_id)
                existing = []
            payload = {
                "documents": entries, "metrics": result["metrics"], "dropped": result["dropped"],
                "open_questions": result["open_questions"],
                "alignment": docdraft.alignment(result["metrics"], existing),
                "truncated": result["truncated"], "model_id": result["model_id"],
            }
        except docdraft.DraftUnavailable as e:
            draft.error = str(e) or "AI 起草不可用"
            await session.commit()
            return
        except Exception as e:  # noqa: BLE001 - 后台出错要写回这一行，不然界面永远停在「起草中」
            logger.exception("从文档起草口径出错（草稿 %s）", draft_id)
            draft.error = f"起草出错：{e}"
            await session.commit()
            return
        draft.payload = payload
        draft.model_id = result["model_id"] or None
        draft.error = None
        draft.status = _status_for(payload)
        await session.commit()


class DraftExtractIn(BaseModel):
    """从哪几份文档起草、起草时给模型看的附加要求。数据源只用来写清适用范围，不给表结构。"""

    documents: list[str] = Field(min_length=1, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    datasource_id: str | None = Field(default=None, max_length=32)
    guidance: str = Field(default="", max_length=2000)


@router.post("/drafts/extract", status_code=201)
async def draft_extract(payload: DraftExtractIn, background: BackgroundTasks,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """从业务文档起草一份口径（app/ledger/docdraft.py）：先建一行 drafting 立刻返回，模型在后台跑。

    文档必须都存在（缺的回 422，一个草稿都不建）；数据源给了也必须存在。草稿只产出定义，不产出取数，也不写进
    workflows.graph——path_pending 不是合法的图。抽取完的 payload / status 用 GET 看。"""
    from app.api.deps import current_principal, ensure_role

    ensure_role("builder")
    doc_ids = list(dict.fromkeys(str(d) for d in payload.documents if str(d).strip()))
    if not doc_ids:
        raise HTTPException(422, "至少要给一份文档")
    found = {doc.id: doc for doc in (await session.execute(
        select(Document).where(Document.id.in_(doc_ids)))).scalars()}
    missing = [doc_id for doc_id in doc_ids if doc_id not in found]
    if missing:
        raise HTTPException(422, f"文档不存在，可能已被删除：{'、'.join(missing)}")
    if payload.datasource_id and await session.get(DataSource, payload.datasource_id) is None:
        raise HTTPException(422, "数据源不存在，可能已被删除")
    docs = [found[doc_id] for doc_id in doc_ids]
    draft = CaliberDraft(title=_doc_title(docs),
                         collection=payload.collection or (docs[0].collection or "default"),
                         datasource_id=payload.datasource_id or None, status="drafting", payload={},
                         origin={"documents": doc_ids, "guidance": payload.guidance.strip()},
                         created_by=current_principal().username)
    session.add(draft)
    await session.commit()
    background.add_task(_extract_in_background, draft.id)
    return {"draft_id": draft.id, "status": draft.status}


@router.get("/drafts")
async def list_drafts(status: str | None = Query(default=None, description="按状态筛：drafting / questions_open / "
                                                   "path_pending / attached / rejected"),
                      session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """口径草稿列表，最近改过的在前。只给计数和状态，正文用 GET /api/ledger/drafts/{id} 看。"""
    from app.api.deps import ensure_role
    from app.ledger import docdraft

    ensure_role("builder")
    query = select(CaliberDraft).order_by(CaliberDraft.updated_at.desc(), CaliberDraft.id.desc())
    if status:
        if status not in docdraft.STATUSES:
            raise HTTPException(422, f"不认识的状态：{status}")
        query = query.where(CaliberDraft.status == status)
    rows = (await session.execute(query)).scalars()
    return {"drafts": [_draft_summary(draft) for draft in rows]}


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一份草稿的完整内容：指标（含引用和 span）、丢弃原因、未决问题、和已发布口径的对齐结果、关联工作流名。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    return await _draft_view(session, await _draft_or_404(session, draft_id))


def _check_metrics(metrics: Any) -> list[dict[str, Any]]:
    """PATCH 换指标时只做结构校验（不重跑引用核对：引用是模型交的，人改过的段落没法再逐字对一遍）。"""
    if not isinstance(metrics, list):
        raise HTTPException(422, "metrics 要是数组")
    for position, metric in enumerate(metrics, start=1):
        if not isinstance(metric, dict):
            raise HTTPException(422, f"第 {position} 条指标不是一个对象")
        name = metric.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(422, f"第 {position} 条指标缺少名称")
        for key in ("synonyms", "open_questions"):
            value = metric.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                raise HTTPException(422, f"第 {position} 条指标的 {key} 要是字符串数组")
        if metric.get("definition") is not None and not isinstance(metric.get("definition"), dict):
            raise HTTPException(422, f"第 {position} 条指标的 definition 要是一个对象")
    return [dict(metric) for metric in metrics]


class DraftPatchIn(BaseModel):
    """改标题、整体换掉指标、或把已经答清的问题消掉。"""

    title: str | None = Field(default=None, max_length=200)
    metrics: list[dict[str, Any]] | None = None
    resolved: list[str] | None = None


@router.patch("/drafts/{draft_id}")
async def patch_draft(draft_id: str, payload: DraftPatchIn, session: AsyncSession = Depends(get_session)
                      ) -> dict[str, Any]:
    """改一份草稿：metrics 整体替换（只做结构校验，不重跑引用核对）；resolved 里的问题从跨指标和各指标的问题里删掉。
    据此重算状态（还有问题 → questions_open，否则 path_pending）。已经 attach 或驳回的草稿不改状态——那时它说的
    已经不是一份草稿了。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    body = draft.payload if isinstance(draft.payload, dict) else {}
    payload_data = dict(body)
    if payload.title is not None:
        if not payload.title.strip():
            raise HTTPException(422, "标题不能为空")
        draft.title = payload.title.strip()
    if payload.metrics is not None:
        payload_data["metrics"] = _check_metrics(payload.metrics)
    if payload.resolved is not None:
        drop = {q.strip() for q in payload.resolved if isinstance(q, str) and q.strip()}
        questions = payload_data.get("open_questions")
        payload_data["open_questions"] = [q for q in questions if isinstance(q, str) and q.strip() not in drop] \
            if isinstance(questions, list) else []
        metrics = payload_data.get("metrics")
        for metric in metrics if isinstance(metrics, list) else []:
            if not isinstance(metric, dict) or not isinstance(metric.get("open_questions"), list):
                continue
            metric["open_questions"] = [q for q in metric["open_questions"]
                                        if not (isinstance(q, str) and q.strip() in drop)]
    if payload.metrics is not None or payload.resolved is not None:
        draft.payload = payload_data
        if draft.status in ("drafting", "questions_open", "path_pending"):
            draft.status = _status_for(payload_data)
    await session.commit()
    return await _draft_view(session, draft)


class DraftDismissIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/drafts/{draft_id}/dismiss")
async def dismiss_draft(draft_id: str, payload: DraftDismissIn,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿不是口径：置 rejected，写一条审计（ledger.docdraft.dismiss），以后不再出现在待办里。"""
    from app.api.deps import ensure_role
    from app.auth.audit import record

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    draft.status = "rejected"
    await record(session, DRAFT_DISMISS_ACTION, DRAFT_SUBJECT_KIND, draft.id,
                 data={k: v for k, v in {"title": draft.title, "note": payload.note}.items() if v})
    await session.commit()
    return {"ok": True, "draft_id": draft.id, "status": draft.status}


class DraftAttachIn(BaseModel):
    """按哪次运行补取数路径：运行里执行过的查询就是这份口径的取数，不另写 SQL。"""

    run_id: str = Field(min_length=1, max_length=64)


@router.post("/drafts/{draft_id}/attach")
async def attach_draft(draft_id: str, payload: DraftAttachIn,
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """把定义接到一次真实运行上（app/ledger/docattach.py）：由它建工作流和版本，交回 {workflow_id, version, metrics,
    warnings}。成功后草稿记下工作流 id、置 attached。接不上时照 docattach 自己的状态码和说法回。"""
    from app.api.deps import ensure_role
    from app.ledger.docattach import attach

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    try:
        out = await attach(session, payload=draft.payload, name=draft.title, run_id=payload.run_id,
                           draft_id=draft.id)
    except Exception as e:  # noqa: BLE001 - docattach 的拒绝带着 status_code / message，照原样交回
        status = getattr(e, "status_code", None)
        if isinstance(status, int) and getattr(e, "message", None):
            raise HTTPException(status, e.message) from e
        raise
    draft.workflow_id = out.get("workflow_id") or draft.workflow_id
    draft.status = "attached"
    draft.error = None
    await session.commit()
    return out
