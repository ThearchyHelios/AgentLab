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
  POST …/{id}/attach、GET …/{id}/trial（router，搭建者）：从业务文档起草口径。主路径一次到位：模型读文档和数据目录，
  产出定义 + 取数 SQL + 入口字段 + 指标表达式（app/ledger/draftplan.py），服务端校验收成草稿图、建草稿工作流和 v1，
  后台自动只读试跑；草稿页给「定义 + 数 + 原文引用」。模型一轮只起草一组指标，一轮覆盖不完就带着 uncovered 续跑，
  一组一个取数节点挂到同一张口径卡上（见 _extract_in_background）。attach 保留为辅助入口：已经有跑对的答案时让它认
  这份定义（app/ledger/docattach.py）。
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
from app.db.models import CaliberDraft, Chunk, DataSource, Document, Run, Workflow, WorkflowVersion
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
    """还有问题 → questions_open；没有问题且有图 → ready；没有图的老草稿 → path_pending。

    questions_open 现在也有图和数，只是问题还没清完，不阻断试跑；path_pending 只留给这次改版之前起草、还没有取数路径的草稿。
    """
    if _question_count(payload):
        return "questions_open"
    if payload.get("workflow_id") and payload.get("sql"):
        return "ready"
    return "path_pending"


#: 试跑最多等多久（秒）和轮询间隔：和 trial_caliber 的 300 × 0.1 一致
TRIAL_TIMEOUT_S = 30
TRIAL_POLL_S = 0.1
#: 运行到这里就是终态了（interrupted 归到 failed，接口契约只有三种状态）
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def _run_status(status: Any) -> str:
    """运行状态 → 接口契约里的 succeeded / failed / running。"""
    if status == "succeeded":
        return "succeeded"
    if status in ("failed", "cancelled", "interrupted"):
        return "failed"
    return "running"


def _source_tables(source: DataSource) -> dict[str, Any]:
    cache = source.schema_cache if isinstance(source.schema_cache, dict) else {}
    tables = cache.get("tables")
    return tables if isinstance(tables, dict) else {}


def _focus_tables(tables: list[str], text: str, limit: int = 8) -> list[str]:
    """文档里点到的表优先展开：按它在原文里第一次出现的位置排，没点到的按目录顺序补几张。

    目录动辄十万字符，全塞给模型会把文档和别的表挤掉。先给全量列名（见 _columns_digest），再把文档点到的
    那几张表展开注释和目录——写 SQL 需要的列名一个不少，细节只给用得上的。
    """
    lowered = (text or "").lower()
    hit: list[tuple[int, str]] = []
    for table in tables:
        name = str(table).lower()
        short = name.split(".")[-1]
        at = lowered.find(name)
        if at < 0:
            at = lowered.find(short)
        if at >= 0:
            hit.append((at, table))
    hit.sort(key=lambda pair: pair[0])
    picked = [table for _, table in hit][:limit]
    for table in tables:
        if len(picked) >= min(limit, 5):
            break
        if table not in picked:
            picked.append(table)
    return picked


def _columns_digest(source: DataSource, tables: list[str]) -> str:
    """每张表的列名清单（只给名字）：要写 SQL，先得知道有哪些列。注释和目录留给下面展开的那几张。"""
    from app.data import introspect

    lines = ["各表的列（只列名字；下面展开的表另有类型、注释和数据目录）："]
    for table in tables:
        meta = introspect.find_table(source, table)
        columns = [str(c.get("name")) for c in (meta or {}).get("columns") or [] if c.get("name")]
        if columns:
            lines.append(f"{table}: {', '.join(columns)}")
    return "\n".join(lines)


async def _schema_material(session: AsyncSession, source: DataSource, focus_text: str = ""
                           ) -> tuple[str, set[str]]:
    """这个数据源给模型看的表结构 + 数据目录文本，以及 SQL 允许出现的表名集合。

    文本按「概览 → 全量表索引 → 全量列名 → 文档点到的表展开」的顺序排：预算不够时先丢的是最后那一段，
    列名不会丢。表名一律照目录原文，模型照着抄。
    """
    from app.data import catalog, schema_view

    cache = source.schema_cache if isinstance(source.schema_cache, dict) else {}
    tables = _source_tables(source)
    if not tables:
        return "", set()
    entries = await catalog.read_catalog(session, source.id)
    notes = {table: entry.notes for table, entry in entries.items()}
    lines = [schema_view.overview_line(source, catalog_tables=sum(
        1 for entry in entries.values() if catalog.visible_notes(entry.notes)))]
    lines.append(schema_view.table_index(source, notes))
    lines.append(_columns_digest(source, tables))
    for table in _focus_tables(tables, focus_text):
        lines.append(schema_view.table_detail(source, table, catalog_notes=notes))
    schema = cache.get("schema")
    allowed: set[str] = set()
    for table in tables:
        allowed.add(str(table))
        if isinstance(schema, str) and schema:
            allowed.add(f"{schema}.{table}")
    return "\n\n".join(line for line in lines if line), allowed


async def _save_trial(draft_id: str, trial: dict[str, str]) -> None:
    """把试跑状态写进 draft.payload["trial"]（用新会话：后台这一段和起草那段不是同一个事务）。"""
    async with SessionLocal() as fresh:
        row = await fresh.get(CaliberDraft, draft_id)
        if row is None or not isinstance(row.payload, dict):
            return
        body = dict(row.payload)
        body["trial"] = trial
        row.payload = body
        await fresh.commit()


async def _run_trial(*, draft_id: str, workflow_id: str, graph: dict[str, Any], values: dict[str, Any],
                     started_by: str | None,
                     entry_fields: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> dict[str, str]:
    """后台试跑草稿的取数与口径卡，返回 payload["trial"] 的形状（run_id / status / error）。

    必填入口没有值时不跑：试跑是为了「看数对不对」，拿系统编的日期跑出来的数没有意义。这时状态记 needs_input，
    页面上让人把入口字段填了再点试运行（POST 同一个路径）。

    跑的是**探针图**（取数 + 口径卡，`derive.caliber_probe`），不是整张草稿图：草稿图里带着报告撰写节点，
    试跑只关心数对不对，不该为了一次试跑多花一次模型调用、也不该因为没有可用模型就判这份口径不行。

    不复用 trial_caliber：那里要求「已发布」，草稿本来就没发布。来源记 caliber_trial；起来之后先写一次 run_id，
    页面这时就能去看运行；轮询到终态或者等满 TRIAL_TIMEOUT_S，没跑完就照实答 running。
    """
    from app.engine.runner import run_manager
    from app.ledger.derive import NotDerivable, caliber_probe, load_pinned

    missing = [str(field.get("name")) for field in entry_fields
               if field.get("required") and values.get(str(field.get("name"))) in (None, "")]
    if missing:
        return {"run_id": "", "status": "needs_input",
                "error": f"缺少必填输入：{'、'.join(missing)}"}

    card_id = next((n.get("id") for n in graph.get("nodes") or []
                    if isinstance(n, dict) and n.get("type") == "metrics"), None)
    if not card_id:
        return {"run_id": "", "status": "failed", "error": "这份草稿没有口径卡，没有可试运行的指标"}
    try:
        async with SessionLocal() as read:
            pinned = await load_pinned(read, workflow_id, 1, str(card_id))
        target = caliber_probe(pinned).graph
    except NotDerivable as e:
        return {"run_id": "", "status": "failed", "error": f"口径卡不能派生探针：{e.message}"}

    try:
        run = await run_manager.start(
            graph=target, input_payload=values, run_class="exploratory", origin_kind="caliber_trial",
            origin={"kind": "caliber_trial", "draft_id": draft_id, "workflow_id": workflow_id, "version": 1,
                    "node_id": str(card_id)},
            started_by=started_by)
    except Exception as e:  # noqa: BLE001 - 试跑发起失败不该把已经生成的草稿弄没
        logger.exception("草稿 %s 的试运行没有发起", draft_id)
        return {"run_id": "", "status": "failed", "error": f"试运行没有发起：{e}"}
    await _save_trial(draft_id, {"run_id": run.id, "status": "running", "error": ""})
    row: Run | None = None
    for _ in range(int(TRIAL_TIMEOUT_S / TRIAL_POLL_S)):
        async with SessionLocal() as poll:
            row = await poll.get(Run, run.id)
        if row is None or row.status in TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(TRIAL_POLL_S)
    status = _run_status(row.status if row is not None else "running")
    error = str(row.error or "") if row is not None and status == "failed" else ""
    return {"run_id": run.id, "status": status, "error": error}


async def _trial_metrics(run_id: str) -> list[dict[str, Any]]:
    """试跑运行里口径卡上的指标值：复用证据接口读封存的现成函数，不另写指标集解析。"""
    from app.api.evidence import _sealed, _sealed_cards

    try:
        sealed = await _sealed(run_id)
        cards = {node_id: card for node_id, _, card in _sealed_cards(sealed)}
    except Exception:  # noqa: BLE001 - 证据读不出来就只给状态，不编造指标值
        logger.exception("试跑 %s 的指标值读不出来", run_id)
        return []
    card = cards.get("card") or next(iter(cards.values()), None)
    if not isinstance(card, dict):
        return []
    return [{k: m.get(k) for k in ("id", "name", "rendered", "value") if m.get(k) is not None}
            for m in card.get("metrics") or [] if isinstance(m, dict) and m.get("id")]


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


#: 一次起草最多续跑几轮：模型一轮只给一组指标，文档里的指标分成几组由它自己切，覆盖完就停。
#: 实测一份 39 条指标的文档，每轮 3–5 条，6 轮只覆盖 22 条，所以放到 10 轮（约 5 分钟）。
MAX_ROUNDS = 10
#: 一张草稿图最多收几条指标：再多，一张口径卡和一次试跑都读不动
MAX_METRICS = 60


def _merged_fields(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各组声明的入口字段合成一份，给界面看：它和建图用的是同一套合并（draftplan.merge_entry_fields）。

    早先这里是「先出现的为准」的另一份实现，建图那边还会因为同名冲突抛错——两份逻辑说不到一起。现在只有一处。
    """
    from app.ledger import draftplan

    fields, _ = draftplan.merge_entry_fields(blocks)
    return fields


def _entry_notes(blocks: list[dict[str, Any]]) -> list[str]:
    """入口字段在各组里声明不一致的地方（必填取并集、类型取更宽），摆给起草的人看。"""
    from app.ledger import draftplan

    return draftplan.merge_entry_fields(blocks)[1]


#: 问题去重时取第一句多长：同一件事每轮换个说法，前面那句「问的是什么」通常一样，后面才是补充
_QUESTION_KEY = 30
_CLAUSE = "，,。；;：:！!？?"
_PUNCT = set(" \t\r\n，。；：、！？,.!?;:'\"（）()[]【】《》<>-_/\\|`~")


def _question_key(text: str) -> str:
    """一句话的「问的是什么」：取第一个分句（问的是同一件事，后面常跟着各轮的补充说明）。"""
    head = text
    for mark in _CLAUSE:
        at = head.find(mark)
        if at > 0:
            head = head[:at]
    return "".join(ch for ch in head if ch not in _PUNCT)[:_QUESTION_KEY]


def _merge_questions(kept: list[str], incoming: list[str]) -> list[str]:
    """跨轮合并未决问题：同一件事每轮都会重新问一遍（68 条里有一半是重复），按归一化后的前 24 个字去重，
    保留先出现的那一句。措辞不同但意思一样的仍会留下——机器分不出来，交给人决定要不要回。"""
    seen = {_question_key(q) for q in kept}
    out = list(kept)
    for item in incoming:
        text = item.strip() if isinstance(item, str) else ""
        if not text:
            continue
        key = _question_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _fresh_metrics(metrics: list[dict[str, Any]], covered: list[str]) -> list[dict[str, Any]]:
    """这一轮里还没起草过的指标，按名字认；同一轮重复交同名指标的只留第一条。

    提示词里已经写了「这些起草过、别重复」，但模型偶尔照抄一遍上一轮的指标。照单全收会出现同名两条指标，口径卡上
    读的是哪条也说不清，所以在这里挡掉。
    """
    seen = set(covered)
    fresh: list[dict[str, Any]] = []
    for metric in metrics:
        name = str(metric.get("name") or "").strip()
        if name:
            if name in seen:
                continue
            seen.add(name)
        fresh.append(metric)
    return fresh


def _draft_payload(*, entries: list[dict[str, Any]], blocks: list[dict[str, Any]], problems: list[dict[str, Any]],
                   questions: list[str], uncovered: list[str], progress: dict[str, int],
                   doc_truncated: bool, catalog_truncated: bool, model_id: str,
                   salvaged: bool) -> dict[str, Any]:
    """草稿 payload 的形状：整份指标、各组 SQL、进度。起草中途也按它写回，页面靠 progress 显示第几轮、覆盖了几条。

    blocks 是组数（不是内容）；sql 把各组的取数拼在一起（组间空一行），人看的是「这次一共要跑哪几条」。truncated 仍是
    doc_truncated 与 catalog_truncated 的「或」，老前端只认这一个键。
    """
    return {
        "documents": entries,
        "metrics": [m for block in blocks for m in block["metrics"]],
        "entry_fields": _merged_fields(blocks),
        "entry_notes": _entry_notes(blocks),
        "sql": "\n\n".join(str(block["sql"]) for block in blocks),
        "version": 1,
        "blocks": len(blocks),
        "progress": progress,
        "trial": {"run_id": "", "status": "running", "error": ""},
        "dropped": problems,
        "truncated": doc_truncated or catalog_truncated,
        "doc_truncated": doc_truncated,
        "catalog_truncated": catalog_truncated,
        "model_id": model_id,
        "open_questions": questions,
        "uncovered": uncovered,
        "salvaged": salvaged,
    }


async def _extract_in_background(draft_id: str) -> None:
    """后台起草：读文档的段落和数据目录，分组反复调模型直到覆盖完，收成一张多取数的草稿图、建工作流、再跑一次试跑。

    用新的会话：请求那一份在响应发出后就关了，后台接着用它写库是错的。没有数据源或没有表结构时起草必须失败——SQL 只能
    对照真实目录写，没有目录就不让模型猜。个别指标不合格只丢那一条、写进 dropped / open_questions；SQL 本身不合格才整份
    失败（图跑不了，留着也没有意义）。

    **为什么一轮接一轮地跑。** 模型一轮只交一组能共用同一条 SQL 的指标（提示词里限 6 条），39 条指标的文档一轮必然覆盖
    不完。以前只跑一轮，其余全进 uncovered，草稿就停在 6 条。现在把已起草的名字（covered）交回提示词、按 uncovered 续跑，
    每轮收一组；直到模型不再给出新指标、uncovered 空了、或到轮数/条数上限。每轮都先写一次 payload 的 progress 再
    commit：一轮要等一次模型调用，页面得能看见进展，而不是停在「起草中」。
    """
    from app.core.artifact_store import graph_hash
    from app.db.models import WorkflowVersion
    from app.ledger import docdraft, draftplan

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

        source = await session.get(DataSource, draft.datasource_id) if draft.datasource_id else None
        if source is None:
            draft.error = "没有数据源：生成取数 SQL 必须对照数据目录里的表和列"
            draft.status = "failed"
            await session.commit()
            return
        focus_text = "\n".join(str(c.get("content") or "") for c in chunks.values())
        tables_text, allowed_tables = await _schema_material(session, source, focus_text)
        if not allowed_tables:
            draft.error = "这个数据源还没有表结构：先探查表结构，再起草口径"
            draft.status = "failed"
            await session.commit()
            return
        datasource = {"name": source.name, "type": source.kind, "description": source.description}

        guidance = str(origin.get("guidance") or "")
        alignment: dict[str, list[Any]] = {"new": [], "duplicate": [], "conflict": []}
        # 每组一组指标：{sql, entry_fields, metrics}，循环结束时拼成一张图
        blocks: list[dict[str, Any]] = []
        problems: list[dict[str, Any]] = []
        covered: list[str] = []
        questions: list[str] = []
        uncovered: list[str] = []
        progress: dict[str, int] = {}
        diagnostics = ""
        doc_truncated = False
        catalog_truncated = False
        salvaged = False
        model_id = ""

        def state() -> dict[str, Any]:
            return _draft_payload(entries=entries, blocks=blocks, problems=problems, questions=questions,
                                  uncovered=uncovered, progress=progress, doc_truncated=doc_truncated,
                                  catalog_truncated=catalog_truncated, model_id=model_id, salvaged=salvaged)

        try:
            for rnd in range(1, MAX_ROUNDS + 1):
                result = await draftplan.plan(session, docs=entries, chunks=chunks, guidance=guidance,
                                              datasource=datasource, tables_text=tables_text, covered=covered)
                plan, round_problems = draftplan.normalize_plan(result, chunks, allowed_tables=allowed_tables)
                fresh = _fresh_metrics(plan["metrics"], covered)
                if plan["sql"] and fresh:
                    blocks.append({"sql": plan["sql"], "entry_fields": plan["entry_fields"], "metrics": fresh})
                    covered += [m["name"] for m in fresh if m.get("name")]
                problems += round_problems
                questions = _merge_questions(questions, plan["open_questions"])
                uncovered = list(result.get("uncovered") or [])
                diagnostics = result.get("diagnostics") or diagnostics
                doc_truncated = doc_truncated or bool(result.get("doc_truncated"))
                catalog_truncated = catalog_truncated or bool(result.get("catalog_truncated"))
                salvaged = salvaged or bool(result.get("salvaged"))
                model_id = result.get("model_id") or model_id
                progress = {"round": rnd, "covered": len(covered), "uncovered": len(uncovered)}
                # 每轮先落一次库：这一轮要等一次模型调用，页面这时就能显示「第几轮、起草了几条、还剩几条」
                draft.payload = state()
                await session.commit()
                if not fresh or not uncovered or len(covered) >= MAX_METRICS:
                    break

            if not blocks:
                error = _problems_text(problems) or "模型没有给出可用的取数 SQL 和指标"
                if diagnostics:
                    error = f"{error}（{diagnostics}）"
                if questions:
                    # 模型把缺什么说清楚了（目录里没有这些列、业务上还没定）：这是一份要补信息的草稿，不是失败
                    draft.payload = {**state(), "trial": {"run_id": "", "status": "", "error": ""}}
                    draft.model_id = model_id or None
                    draft.error = ""
                    draft.status = "questions_open"
                    await session.commit()
                    return
                draft.payload = {**state(), "trial": {"run_id": "", "status": "failed", "error": error}}
                draft.model_id = model_id or None
                draft.error = error
                draft.status = "failed"
                await session.commit()
                return

            try:
                graph = draftplan.build_graph(blocks, name=draft.title, question=guidance or draft.title,
                                              datasource=source.name)
            except ValueError as e:
                # build_graph 现在合并入口字段不再抛错；这条留着兜底：真出现合不了的图，照实记为失败，别留半成品
                draft.payload = {**state(), "dropped": [*problems, {"name": "入口字段", "reason": str(e)}],
                                 "trial": {"run_id": "", "status": "failed", "error": str(e)}}
                draft.model_id = model_id or None
                draft.error = str(e)
                draft.status = "failed"
                await session.commit()
                return

            # 对齐要扫所有已发布口径：某一份读不出来不该让整份草稿失败，照实不做对齐（alignment 三个空列表）
            try:
                existing = await _published_metrics(session)
            except Exception:  # noqa: BLE001 - 对齐是辅助信息，不是草稿本身
                logger.exception("读已发布口径失败，草稿 %s 不做对齐", draft_id)
                existing = []
            metrics = [m for block in blocks for m in block["metrics"]]
            alignment = docdraft.alignment(metrics, existing)

            asked = list(dict.fromkeys(questions + [
                f"{p['name']}：{p['reason']}" for p in problems
                if p.get("name") not in ("sql", "metrics")]))
            workflow = Workflow(
                name=(draft.title.strip() or "文档口径草稿")[:200],
                description=(f"从业务文档起草的口径：{len(metrics)} 个指标、{len(blocks)} 组取数，取数 SQL 由模型对照"
                             f"数据目录生成，后台自动只读试运行；人工审改后按受管级别发布"),
                graph=graph, status="draft", tags=["doc_draft"],
                origin={"kind": "doc_draft", "draft_id": draft.id, "datasource_id": source.id,
                        "documents": [doc.id for doc in docs], "guidance": guidance})
            session.add(workflow)
            await session.flush()
            session.add(WorkflowVersion(workflow_id=workflow.id, version=1, graph=graph,
                                        graph_hash=graph_hash(graph), note="从业务文档分组生成"))
            payload = {
                **state(), "workflow_id": workflow.id, "open_questions": asked,
                "alignment": alignment,
            }
        except docdraft.DraftUnavailable as e:
            draft.error = str(e) or "AI 起草不可用"
            draft.status = "failed"
            await session.commit()
            return
        except Exception as e:  # noqa: BLE001 - 后台出错要写回这一行，不然界面永远停在「起草中」
            logger.exception("从文档起草口径出错（草稿 %s）", draft_id)
            draft.error = f"起草出错：{e}"
            draft.status = "failed"
            await session.commit()
            return
        draft.payload = payload
        draft.workflow_id = workflow.id
        draft.model_id = model_id or None
        draft.error = None
        draft.status = _status_for(payload)
        await session.commit()
        values = {field["name"]: field.get("default") for field in _merged_fields(blocks)
                  if field.get("default") not in (None, "")}

    try:
        trial = await _run_trial(draft_id=draft_id, workflow_id=workflow.id, graph=graph, values=values,
                                 started_by=draft.created_by, entry_fields=_merged_fields(blocks))
    except Exception as e:  # noqa: BLE001 - 试运行出错也要写进 payload，不能让界面停在「试运行中」
        logger.exception("草稿 %s 的试运行出错", draft_id)
        trial = {"run_id": "", "status": "failed", "error": f"试运行出错：{e}"}
    await _save_trial(draft_id, trial)


def _problems_text(problems: list[dict[str, Any]]) -> str:
    """把校验问题拼成一句给人看的原因（接口的 error 只有一个字段）。"""
    return "；".join(f"{p.get('name') or '问题'}：{p.get('reason') or ''}".strip("：")
                    for p in problems if p.get("reason"))[:2000]


class DraftExtractIn(BaseModel):
    """从哪几份文档起草、起草时给模型看的附加要求。数据源必须给：它的表结构和目录是 SQL 的唯一依据。"""

    documents: list[str] = Field(min_length=1, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    datasource_id: str | None = Field(default=None, max_length=32)
    guidance: str = Field(default="", max_length=2000)


@router.post("/drafts/extract", status_code=201)
async def draft_extract(payload: DraftExtractIn, background: BackgroundTasks,
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """从业务文档起草一份口径（app/ledger/draftplan.py）：先建一行 drafting 立刻返回，模型在后台跑。

    文档必须都存在（缺的回 422，一个草稿都不建）；数据源必须给、必须存在，而且已经探查到表结构——SQL 只能对照真实目录
    写，没有目录时让模型猜表，图跑不了。模型产出定义 + SQL + 入口字段后由服务端校验收成草稿图和 v1，后台自动只读试跑。
    起草完的 payload / status / trial 用 GET 看。"""
    from app.api.deps import current_principal, ensure_role

    ensure_role("builder")
    doc_ids = list(dict.fromkeys(str(d) for d in payload.documents if str(d).strip()))
    if not doc_ids:
        raise HTTPException(422, "至少要给一份文档")
    if not payload.datasource_id:
        raise HTTPException(422, "生成取数 SQL 需要数据源：请选一个已经探查过表结构的数据源")
    found = {doc.id: doc for doc in (await session.execute(
        select(Document).where(Document.id.in_(doc_ids)))).scalars()}
    missing = [doc_id for doc_id in doc_ids if doc_id not in found]
    if missing:
        raise HTTPException(422, f"文档不存在，可能已被删除：{'、'.join(missing)}")
    source = await session.get(DataSource, payload.datasource_id)
    if source is None:
        raise HTTPException(422, "数据源不存在，可能已被删除")
    if not _source_tables(source):
        raise HTTPException(422, "这个数据源还没有表结构：先探查表结构，再起草口径")
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
async def list_drafts(status: str | None = Query(default=None, description="按状态筛：drafting / ready / "
                                                   "questions_open / path_pending / attached / rejected / failed"),
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
    """一份草稿的完整内容：指标（含引用和 span）、取数 SQL、入口字段、试跑状态、丢弃原因、未决问题、和已发布口径的对齐
    结果、关联工作流名。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    return await _draft_view(session, await _draft_or_404(session, draft_id))


@router.get("/drafts/{draft_id}/trial")
async def get_draft_trial(draft_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这份草稿的试跑：{run_id, status, metrics: [{id, name, rendered, value}], error}。

    状态只有 succeeded / failed / running 三种；指标值读试跑运行封存的口径卡（和口径试跑同一个读法）。还没试跑（老草稿、
    起草失败）回 404。只读，不发起运行。"""
    from app.api.deps import ensure_role

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    payload = draft.payload if isinstance(draft.payload, dict) else {}
    trial = payload.get("trial") if isinstance(payload.get("trial"), dict) else {}
    run_id = trial.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        if trial.get("status") == "needs_input":
            return {"run_id": "", "status": "needs_input", "metrics": [],
                    "error": str(trial.get("error") or "")}
        raise HTTPException(404, "这份草稿还没有试运行")
    run = await session.get(Run, run_id)
    if run is None:
        return {"run_id": run_id, "status": _run_status(trial.get("status")), "metrics": [],
                "error": str(trial.get("error") or "")}
    status = _run_status(run.status)
    metrics = await _trial_metrics(run.id) if status == "succeeded" else []
    return {"run_id": run.id, "status": status, "metrics": metrics, "error": str(run.error or "")}


class DraftTrialIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/drafts/{draft_id}/trial")
async def run_draft_trial(draft_id: str, payload: DraftTrialIn,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按填好的入口字段试跑一次（builder 及以上）。

    自动试跑缺必填输入时停在 needs_input（不拿系统编的日期跑，那样跑出来的数没有意义），人在这里补上再跑。
    参数按入口字段校验，和正式运行同一套（apps/forms）；跑的仍是探针图：只取数与口径卡，不写报告、不进统计。
    """
    from app.api.deps import current_principal, ensure_role
    from app.apps.forms import input_schema, validate_form

    ensure_role("builder")
    draft = await _draft_or_404(session, draft_id)
    body = draft.payload if isinstance(draft.payload, dict) else {}
    fields = [field for field in body.get("entry_fields") or [] if isinstance(field, dict)]
    workflow_id = draft.workflow_id or body.get("workflow_id")
    if not workflow_id:
        raise HTTPException(409, "这份草稿还没有取数图，不能试运行")
    version = int(body.get("version") or 1)
    row = (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one_or_none()
    if row is None or not isinstance(row.graph, dict):
        raise HTTPException(409, "找不到这份草稿的取数图，可能已被删除")
    values: dict[str, Any] = {str(field.get("name")): field.get("default") for field in fields
                              if field.get("default") not in (None, "")}
    values.update({str(key): value for key, value in payload.inputs.items()})
    if errors := validate_form(input_schema(fields), values):
        raise HTTPException(422, "；".join(str(e.get("message")) for e in errors[:5]))
    trial = await _run_trial(draft_id=draft.id, workflow_id=str(workflow_id), graph=row.graph,
                             values=values, started_by=current_principal().username, entry_fields=fields)
    await _save_trial(draft.id, trial)
    run_id = str(trial.get("run_id") or "")
    return {**trial, "metrics": await _trial_metrics(run_id) if run_id else []}


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
    据此重算状态（还有问题 → questions_open，否则有图 → ready、没有图的老草稿 → path_pending）。已经 attach 或驳回的
    草稿不改状态——那时它说的已经不是一份草稿了。"""
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
        metrics = payload_data.get("metrics")
        # 起草失败的草稿补上真指标后要能往前走；没有指标时不重算，免得空草稿被当成「待接数据路径」
        if isinstance(metrics, list) and metrics and draft.status in ("drafting", "questions_open",
                                                                     "path_pending", "ready", "failed"):
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
