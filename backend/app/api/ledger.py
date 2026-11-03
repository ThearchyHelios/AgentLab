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
这几个新接口都不放给访问密钥（auth/keys.KEY_ROUTES 不登记）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, get_session
from app.db.models import Run, Workflow
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
