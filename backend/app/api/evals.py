"""评测接口（docs/design/next-round/01-效果度量与回归评测.md）：题集、题目、期望答案试算、目录快照；第 3 波加评测、结果、对比。

整组只给搭建者及以上（main.py 挂 require_role("builder")）：期望值和查询原文只给搭建者。这一波没有界面（第 4 波），
接口的形状就是给界面的契约。请求体自己解析、自己校验，错误一律回中文 detail + 机器码（422 {detail, code, field}，
同 api/coded.py），表单据 field 把报错落到那一格。

- 题集：GET/POST /suites，GET/PATCH/DELETE /suites/{id}（改要带读到的 version，对不上 409；有评测记录的不能删，只能归档）；
  GET /suites/{id}/export、POST /suites/import/preview、POST /suites/import（app/eval/transfer.py）。
- 题目：POST /suites/{id}/cases、PATCH/DELETE /suites/{id}/cases/{cid}（改要带 version）。保存时试算一次（app/eval/
  gold.py）：没过只读守卫、用到遮罩列、取不到值、运行失败、超时、口径升版待处置的，照实回 422、不保存。
  POST /suites/{id}/cases/trial 只试算不保存（界面上「试算」按钮），POST /suites/{id}/cases/{cid}/trial 试算已有的题并记下。
- 从运行认定：GET /runs/{run_id}/queries 列出那次运行里的查询，POST /suites/{id}/cases/from-run 拷出一条查询的 SQL 建题
  （记确认人、时间、出自哪次运行），之后每次评测按这条 SQL 重算。
- 口径：GET /calibers 列出能选的口径（和蓝图助手同一份清单：每个工作流最新的已发布未退役版本里的源头口径卡，能不能复用、
  为什么不能）；GET/POST /suites/{id}/cases/{cid}/upgrade 看、处置口径升版（recompute / dual / incomparable 三选一）。
- 目录快照：GET/POST /catalog-snapshots（当前目录）、POST /catalog-snapshots/shadow/estimate（影子起草的计划和估算）、
  POST /catalog-snapshots/shadow（后台起草，202）、GET /catalog-snapshots/{id}、POST /catalog-snapshots/{id}/stop。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import actor_of
from app.db.base import get_session, new_id, utcnow
from app.db.models_eval import EvalCase, EvalCatalogSnapshot, EvalRun, EvalSuite
from app.eval import cases as case_rules
from app.eval import gold as gold_rules
from app.eval import snapshots, transfer
from app.eval.cases import CaseInvalid, case_fields, case_hash, normalize_case, resolve_case
from app.eval.suites import effective_budget, normalize_suite

router = APIRouter(prefix="/api/evals", tags=["evals"])

#: 机器码
VERSION_CONFLICT = "eval_version_conflict"
SUITE_NOT_FOUND = "eval_suite_not_found"
CASE_NOT_FOUND = "eval_case_not_found"
SUITE_HAS_RUNS = "eval_suite_has_runs"
RUN_NOT_FOUND = "eval_run_query_not_found"
SNAPSHOT_NOT_FOUND = "eval_snapshot_not_found"
IMPORT_INVALID = "eval_import_invalid"
#: 导入时同时试算几道题
IMPORT_TRIALS = 4


def _invalid(e: CaseInvalid) -> CodedHTTPException:
    return CodedHTTPException(422, e.message, e.code, field=e.field)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CodedHTTPException(422, "请求内容需要是 JSON 对象", case_rules.CASE_INVALID)
    return data


def _version(body: dict[str, Any], current: int, what: str) -> int:
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CodedHTTPException(422, "保存时需要带上读到的版本号 version，请刷新后重试", case_rules.CASE_INVALID,
                                 field="version")
    if version != current:
        raise CodedHTTPException(409, f"这个{what}已被修改（当前是第 {current} 版，提交的是第 {version} 版），"
                                      "请刷新后再保存", VERSION_CONFLICT)
    return version


def _iso(moment: Any) -> str | None:
    return moment.isoformat() if moment is not None else None


# ==========================================================================
# 形状
# ==========================================================================


def _suite_out(row: EvalSuite, *, counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "description": row.description, "timezone": row.timezone,
            "default_variant": dict(row.default_variant or {}), "budget": dict(row.budget or {}),
            "effective_budget": effective_budget(row.budget), "archived": bool(row.archived), "version": row.version,
            "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
            **({"counts": counts} if counts is not None else {})}


async def _source_names(session: AsyncSession, rows: list[EvalCase]) -> dict[str, str]:
    from app.db.models import DataSource

    ids = {s for r in rows for s in (r.datasource_ids or [])} | {
        (r.gold or {}).get("datasource_id") for r in rows if (r.gold or {}).get("datasource_id")}
    if not ids:
        return {}
    return dict((await session.execute(select(DataSource.id, DataSource.name).where(DataSource.id.in_(ids)))).all())


async def _upgrade_of(session: AsyncSession, row: EvalCase) -> dict[str, Any] | None:
    """口径题钉的那一版现在怎么样（gold.upgrade_state）；读不出那一版的照实写原因。别的题为 None。"""
    from app.ledger import derive

    gold = row.gold or {}
    if gold.get("kind") != "caliber" or row.archived:
        return None
    try:
        pinned = await derive.load_pinned(session, gold["workflow_id"], gold["version"], gold["node_id"])
    except derive.NotDerivable as e:
        return {"pinned": gold.get("version"), "pending": True, "message": e.message, "code": e.code}
    return (await gold_rules.upgrade_state(session, gold, pinned)).to_dict()


def _case_out(row: EvalCase, names: dict[str, str], upgrade: dict[str, Any] | None = None) -> dict[str, Any]:
    """gold 原样给（改题时整份带回来就行）；它查的数据源的名字另放在 gold_source。"""
    gold_source = (row.gold or {}).get("datasource_id")
    return {**case_fields(row),
            "gold_source": {"id": gold_source, "name": names.get(gold_source)} if gold_source else None,
            "datasources": [{"id": s, "name": names.get(s)} for s in row.datasource_ids or []],
            "archived": bool(row.archived), "superseded_by": row.superseded_by, "version": row.version,
            "case_hash": row.case_hash, "trial": row.trial, "upgrade": upgrade, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at)}


async def _cases_out(session: AsyncSession, rows: list[EvalCase]) -> list[dict[str, Any]]:
    names = await _source_names(session, rows)
    return [_case_out(r, names, await _upgrade_of(session, r)) for r in rows]


# ==========================================================================
# 读写的小件
# ==========================================================================


async def _suite(session: AsyncSession, suite_id: str) -> EvalSuite:
    row = await session.get(EvalSuite, suite_id)
    if row is None:
        raise CodedHTTPException(404, "题集不存在，可能已被删除", SUITE_NOT_FOUND)
    return row


async def _case(session: AsyncSession, suite_id: str, case_id: str) -> EvalCase:
    row = await session.get(EvalCase, case_id)
    if row is None or row.suite_id != suite_id:
        raise CodedHTTPException(404, "题目不存在，可能已被删除", CASE_NOT_FOUND)
    return row


def _context(suite: EvalSuite, actor: str | None, **extra: Any) -> gold_rules.Context:
    return gold_rules.Context(as_of=utcnow(), tz=suite.timezone, actor=actor, suite_id=suite.id, suite_name=suite.name,
                              role="gold", extra={"trial": True, **extra})


async def _trial(suite: EvalSuite, fields: dict[str, Any], actor: str | None, *, case_id: str | None = None,
                 seq: int | None = None) -> gold_rules.GoldOutcome:
    """按此刻试算一次期望。"""
    return await gold_rules.compute_gold({**fields, "id": case_id, "seq": seq}, _context(suite, actor))


def _trial_record(outcome: gold_rules.GoldOutcome, actor: str | None) -> dict[str, Any]:
    return {**outcome.summary(), "at": utcnow().isoformat(), "by": actor}


def _gate(outcome: gold_rules.GoldOutcome) -> None:
    """保存题目的那一道关：试算没过就照实回 422、不保存。"""
    if outcome.ok:
        return
    reason = outcome.reason or "试算没有通过"
    if outcome.status == "skipped":
        reason += "：新建、修改的口径题请固定最新的已发布版本，或在这道题上选择升版的处置"
    raise CodedHTTPException(422, f"试算没有通过，题目没有保存：{reason}", outcome.code or gold_rules.GOLD_FAILED,
                             field="gold")


async def _next_seq(session: AsyncSession, suite_id: str) -> int:
    top = await session.scalar(select(func.max(EvalCase.seq)).where(EvalCase.suite_id == suite_id))
    return int(top or 0) + 1


async def _insert_case(session: AsyncSession, suite: EvalSuite, fields: dict[str, Any], actor: str | None,
                       trial: dict[str, Any] | None) -> EvalCase:
    """建一道题（提交）。题号撞了（两个请求同时建）重取一次。"""
    for _attempt in range(3):
        row = EvalCase(suite_id=suite.id, seq=await _next_seq(session, suite.id), case_hash=case_hash(fields),
                       trial=trial, created_by=actor, **fields)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            continue
        await session.refresh(row)
        return row
    raise HTTPException(409, "题集刚被别人加了题，请重试")


# ==========================================================================
# 题集
# ==========================================================================


@router.get("/suites")
async def list_suites(archived: bool = False, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    query = select(EvalSuite).order_by(EvalSuite.updated_at.desc())
    if not archived:
        query = query.where(EvalSuite.archived.is_(False))
    rows = list((await session.execute(query)).scalars())
    counts: dict[str, dict[str, int]] = {r.id: {"cases": 0, "enabled": 0} for r in rows}
    if rows:
        for suite_id, enabled, n in (await session.execute(
                select(EvalCase.suite_id, EvalCase.enabled, func.count()).where(
                    EvalCase.suite_id.in_(list(counts)), EvalCase.archived.is_(False))
                .group_by(EvalCase.suite_id, EvalCase.enabled))).all():
            counts[suite_id]["cases"] += n
            if enabled:
                counts[suite_id]["enabled"] += n
    return {"suites": [_suite_out(r, counts=counts[r.id]) for r in rows]}


@router.post("/suites", status_code=201)
async def create_suite(request: Request, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    try:
        values = normalize_suite(await _body(request))
    except CaseInvalid as e:
        raise _invalid(e) from e
    row = EvalSuite(**values, created_by=actor_of(x_actor))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _suite_out(row, counts={"cases": 0, "enabled": 0})


@router.get("/suites/{suite_id}")
async def get_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """题集和它的题目（归档的题也在，archived 标着）。口径题带上升版状态：pending_upgrades 是待处置的题数（界面顶部提示）。"""
    suite = await _suite(session, suite_id)
    rows = list((await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id)
                                       .order_by(EvalCase.seq))).scalars())
    out = await _cases_out(session, rows)
    active = [c for c in out if not c["archived"]]
    return {**_suite_out(suite, counts={"cases": len(active), "enabled": sum(1 for c in active if c["enabled"])}),
            "cases": out, "pending_upgrades": sum(1 for c in active if (c["upgrade"] or {}).get("pending"))}


@router.patch("/suites/{suite_id}")
async def update_suite(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    suite = await _suite(session, suite_id)
    body = await _body(request)
    version = _version(body, suite.version, "题集")
    current = {k: getattr(suite, k) for k in ("name", "description", "timezone", "default_variant", "budget",
                                              "archived")}
    try:
        values = normalize_suite(body, current=current)
    except CaseInvalid as e:
        raise _invalid(e) from e
    result = await session.execute(update(EvalSuite).where(EvalSuite.id == suite_id, EvalSuite.version == version)
                                   .values(**values, version=version + 1, updated_at=utcnow())
                                   .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise CodedHTTPException(409, "这个题集刚被别人修改过，请刷新后再保存", VERSION_CONFLICT)
    await session.commit()
    await session.refresh(suite)
    return await get_suite(suite_id, session)


@router.delete("/suites/{suite_id}", status_code=204)
async def delete_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删题集和它的题目。有评测记录的不能删：每次评测的总表永久保留，请改为归档。"""
    suite = await _suite(session, suite_id)
    runs = await session.scalar(select(func.count()).select_from(EvalRun).where(EvalRun.suite_id == suite_id))
    if runs:
        raise CodedHTTPException(409, f"这个题集有 {runs} 次评测记录，不能删除（评测的总表要留着）。请改为归档",
                                 SUITE_HAS_RUNS)
    await session.delete(suite)
    await session.commit()
    return Response(status_code=204)


# ==========================================================================
# 题目
# ==========================================================================


async def _validated(session: AsyncSession, suite: EvalSuite, body: dict[str, Any], *,
                     current: dict[str, Any] | None = None, trusted_gold: bool = False) -> dict[str, Any]:
    try:
        fields = normalize_case(body, current=current, trusted_gold=trusted_gold)
        await resolve_case(session, fields, tz=suite.timezone)
    except CaseInvalid as e:
        raise _invalid(e) from e
    return fields


@router.post("/suites/{suite_id}/cases", status_code=201)
async def create_case(suite_id: str, request: Request, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """建一道题：校验、试算一次（没过不保存），交回题目和这次试算的结果（含期望值）。"""
    suite = await _suite(session, suite_id)
    actor = actor_of(x_actor)
    fields = await _validated(session, suite, await _body(request))
    outcome = await _trial(suite, fields, actor)
    _gate(outcome)
    row = await _insert_case(session, suite, fields, actor, _trial_record(outcome, actor))
    return {"case": _case_out(row, await _source_names(session, [row]), await _upgrade_of(session, row)),
            "trial": outcome.to_dict()}


@router.post("/suites/{suite_id}/cases/trial")
async def trial_unsaved(suite_id: str, request: Request, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """只试算、不保存（界面上写 SQL、点选格或列时的「试算」）：交回试算结果，没过的照实写原因（不回 422）。"""
    suite = await _suite(session, suite_id)
    fields = await _validated(session, suite, await _body(request))
    return {"trial": (await _trial(suite, fields, actor_of(x_actor))).to_dict()}


@router.patch("/suites/{suite_id}/cases/{case_id}")
async def update_case(suite_id: str, case_id: str, request: Request, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """改一道题（带上读到的 version）。题目内容（指纹）变了要重新试算、没过不保存；只改标签、启用的不试算。"""
    suite = await _suite(session, suite_id)
    row = await _case(session, suite_id, case_id)
    body = await _body(request)
    version = _version(body, row.version, "题目")
    actor = actor_of(x_actor)
    fields = await _validated(session, suite, body, current=case_fields(row))
    new_hash = case_hash(fields)
    outcome = None
    values: dict[str, Any] = {**fields, "case_hash": new_hash}
    if new_hash != row.case_hash:
        outcome = await _trial(suite, fields, actor, case_id=row.id, seq=row.seq)
        _gate(outcome)
        values["trial"] = _trial_record(outcome, actor)
    result = await session.execute(update(EvalCase).where(EvalCase.id == case_id, EvalCase.version == version)
                                   .values(**values, version=version + 1, updated_at=utcnow())
                                   .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise CodedHTTPException(409, "这道题刚被别人修改过，请刷新后再保存", VERSION_CONFLICT)
    await session.commit()
    await session.refresh(row)
    return {"case": _case_out(row, await _source_names(session, [row]), await _upgrade_of(session, row)),
            "trial": outcome.to_dict() if outcome is not None else None}


@router.delete("/suites/{suite_id}/cases/{case_id}", status_code=204)
async def delete_case(suite_id: str, case_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删一道题。已有的评测结果照留（按题目 id 和指纹记着，不随题目删）。"""
    row = await _case(session, suite_id, case_id)
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


@router.post("/suites/{suite_id}/cases/{case_id}/trial")
async def trial_case(suite_id: str, case_id: str, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """按此刻试算一道已有的题，记进题目的 trial（不带期望值），交回试算结果（含期望值）。不改题目的 version。"""
    suite = await _suite(session, suite_id)
    row = await _case(session, suite_id, case_id)
    actor = actor_of(x_actor)
    outcome = await _trial(suite, case_fields(row), actor, case_id=row.id, seq=row.seq)
    await session.execute(update(EvalCase).where(EvalCase.id == case_id).values(trial=_trial_record(outcome, actor))
                          .execution_options(synchronize_session=False))
    await session.commit()
    return {"trial": outcome.to_dict()}


# ==========================================================================
# 从运行认定
# ==========================================================================


async def _run_queries(session: AsyncSession, run_id: str) -> list[dict[str, Any]]:
    from app.core import artifact_store
    from app.data.catalog import QUERY_SNAPSHOT_KIND
    from app.db.models import Artifact

    rows = (await session.execute(select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == QUERY_SNAPSHOT_KIND)
                                  .order_by(Artifact.created_at))).scalars().all()
    out = []
    for ref in rows:
        try:
            snap = artifact_store.load(ref.id) or {}
        except ValueError:
            continue
        out.append({"artifact": ref.id, "node_id": ref.node_id, "source": snap.get("source"),
                    "source_id": (ref.meta or {}).get("source_id"), "sql": snap.get("sql"),
                    "columns": list(snap.get("columns") or []), "row_count": snap.get("row_count"),
                    "truncated": bool(snap.get("truncated")),
                    "masked": bool(snap.get("masked_at_query") or snap.get("masked_cells") or snap.get("masked_whole"))})
    return out


async def _readable_run(session: AsyncSession, run_id: str) -> Any:
    from app.api.deps import can_read_run, current_principal
    from app.db.models import Run

    run = await session.get(Run, run_id)
    if run is None or not await can_read_run(current_principal(), run):
        raise CodedHTTPException(404, "运行不存在，可能已被删除", RUN_NOT_FOUND)
    return run


@router.get("/runs/{run_id}/queries")
async def run_queries(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一次运行里执行过的查询（查询快照）：选一条认定成期望。只给 SQL、列名、行数，不给结果的格。"""
    await _readable_run(session, run_id)
    return {"queries": await _run_queries(session, run_id)}


@router.post("/suites/{suite_id}/cases/from-run", status_code=201)
async def create_from_run(suite_id: str, request: Request, session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """从一次运行里认定一条查询：{run_id, query（查询快照的工件 id）, expectations, question?, tags?, datasource_ids?}。
    拷出那条查询的 SQL 建 from_run 题，记确认人（就是这次请求的人）、时间、出自哪次运行；之后每次评测按这条 SQL 重算。
    问法没给的取那次运行的问题。同样要试算通过才保存。"""
    from app.db.models import DataSource

    suite = await _suite(session, suite_id)
    body = await _body(request)
    actor = actor_of(x_actor)
    run_id, query = body.get("run_id"), body.get("query")
    if not isinstance(run_id, str) or not isinstance(query, str):
        raise CodedHTTPException(422, "请指定运行（run_id）和其中的一条查询（query）", case_rules.CASE_INVALID,
                                 field="query")
    run = await _readable_run(session, run_id)
    picked = next((q for q in await _run_queries(session, run_id) if q["artifact"] == query), None)
    if picked is None or not picked.get("sql"):
        raise CodedHTTPException(404, "这次运行里没有这条查询", RUN_NOT_FOUND)
    source_id = picked.get("source_id")
    if not source_id and picked.get("source"):
        source_id = await session.scalar(select(DataSource.id).where(DataSource.name == picked["source"]))
    if not source_id or await session.get(DataSource, source_id) is None:
        raise CodedHTTPException(422, f"这条查询查的数据源「{picked.get('source') or '?'}」已不在，没法按它重算",
                                 case_rules.CASE_INVALID, field="query")
    question = body.get("question")
    if question in (None, "") and isinstance((run.input or {}).get("question"), str):
        question = run.input["question"]
    gold = {"kind": "from_run", "datasource_id": source_id, "sql": picked["sql"], "run_id": run_id, "query": query,
            "node_id": picked.get("node_id"), "confirmed_by": actor, "confirmed_at": utcnow().isoformat()}
    case_body = {k: body[k] for k in ("expectations", "tags", "datasource_ids", "enabled") if k in body}
    fields = await _validated(session, suite, {**case_body, "question": question, "gold": gold}, trusted_gold=True)
    outcome = await _trial(suite, fields, actor)
    _gate(outcome)
    row = await _insert_case(session, suite, fields, actor, _trial_record(outcome, actor))
    return {"case": _case_out(row, await _source_names(session, [row])), "trial": outcome.to_dict()}


# ==========================================================================
# 口径
# ==========================================================================


@router.get("/calibers")
async def list_calibers(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """能选的口径：每个工作流最新的已发布未退役版本里的源头口径卡的每个指标（和蓝图助手同一份，look/calibers.CaliberKind）。
    不能复用的照列，写明原因（选了也存不了）。"""
    from app.look.calibers import CaliberKind

    kind = await CaliberKind.load(session)
    return {"calibers": [{"workflow_id": i.workflow_id, "workflow_name": i.workflow_name, "version": i.version,
                          "level": i.level, "node_id": i.node_id, "card": i.card, "caliber": i.caliber,
                          "metric_id": i.metric_id, "name": i.name, "unit": i.unit, "reusable": i.reason is None,
                          "reason": i.reason} for i in kind.items]}


@router.get("/suites/{suite_id}/cases/{case_id}/upgrade")
async def case_upgrade(suite_id: str, case_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _case(session, suite_id, case_id)
    return {"upgrade": await _upgrade_of(session, row), "choices": gold_rules.UPGRADE_LABEL}


@router.post("/suites/{suite_id}/cases/{case_id}/upgrade")
async def dispose_upgrade(suite_id: str, case_id: str, request: Request, session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """处置口径升版：{policy: recompute|dual|incomparable, version}，没有缺省。

    - recompute：改钉新版本（题目指纹变，和上一次比时标不可比）；
    - dual：新旧两版各算一份期望（旧的那一份照旧可比）；
    - incomparable：旧题归档（不再评），按新版本另建一道新题、重开基线。
    处置后的题同样要试算通过才生效。"""
    suite = await _suite(session, suite_id)
    row = await _case(session, suite_id, case_id)
    body = await _body(request)
    version = _version(body, row.version, "题目")
    actor = actor_of(x_actor)
    policy = body.get("policy")
    current = case_fields(row)
    try:
        planned, _latest = await gold_rules.plan_dispose(session, current, policy if isinstance(policy, str) else "")
    except gold_rules.DisposeRefused as e:
        raise CodedHTTPException(e.status, e.message, e.code, field="policy") from e
    if isinstance(planned.get("upgrade"), dict):
        planned["upgrade"]["by"] = actor
    fields = {**current, "gold": planned}
    for key in ("id", "suite_id", "seq"):
        fields.pop(key, None)
    outcome = await _trial(suite, fields, actor, case_id=row.id if policy != "incomparable" else None,
                           seq=row.seq if policy != "incomparable" else None)
    _gate(outcome)
    if policy == "incomparable":
        new = await _insert_case(session, suite, fields, actor, _trial_record(outcome, actor))
        result = await session.execute(update(EvalCase).where(EvalCase.id == case_id, EvalCase.version == version)
                                       .values(archived=True, enabled=False, superseded_by=new.id,
                                               version=version + 1, updated_at=utcnow())
                                       .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await session.delete(new)
            await session.commit()
            raise CodedHTTPException(409, "这道题刚被别人修改过，请刷新后再处置", VERSION_CONFLICT)
        await session.commit()
        await session.refresh(row)
        names = await _source_names(session, [row, new])
        return {"case": _case_out(row, names), "new_case": _case_out(new, names, await _upgrade_of(session, new)),
                "trial": outcome.to_dict()}
    result = await session.execute(update(EvalCase).where(EvalCase.id == case_id, EvalCase.version == version)
                                   .values(gold=planned, case_hash=case_hash(fields), trial=_trial_record(outcome, actor),
                                           version=version + 1, updated_at=utcnow())
                                   .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise CodedHTTPException(409, "这道题刚被别人修改过，请刷新后再处置", VERSION_CONFLICT)
    await session.commit()
    await session.refresh(row)
    return {"case": _case_out(row, await _source_names(session, [row]), await _upgrade_of(session, row)),
            "new_case": None, "trial": outcome.to_dict()}


# ==========================================================================
# 导出导入
# ==========================================================================


@router.get("/suites/{suite_id}/export")
async def export_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """题集导出成 JSON 文件（按数据源名、工作流名对上；不含期望值、答案、运行）。"""
    import json
    from urllib.parse import quote

    suite = await _suite(session, suite_id)
    rows = list((await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id))).scalars())
    content = await transfer.export_suite(session, suite, rows)
    filename = transfer.attachment_name(suite.name)
    return Response(content=json.dumps(content, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename=\"eval-suite-{suite.id}.json\"; "
                                                    f"filename*=UTF-8''{quote(filename)}"})


async def _plan(session: AsyncSession, request: Request) -> tuple[transfer.ImportPlan, dict[str, Any]]:
    body = await _body(request)
    try:
        return await transfer.plan_import(session, body.get("content")), body
    except transfer.TransferInvalid as e:
        raise CodedHTTPException(422, str(e), IMPORT_INVALID, field="content") from e


@router.post("/suites/import/preview")
async def import_preview(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{content: 题集文件}：哪些题对得上、哪些跳过和原因。不写库、不查业务库（试算在导入时做）。"""
    plan, _ = await _plan(session, request)
    return plan.to_dict()


@router.post("/suites/import", status_code=201)
async def import_suite(request: Request, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """{content: 题集文件, name?}：建一个新题集，对得上的题逐道试算（和保存题目同一道关），过了的导进来，没过的跳过、写原因。
    一道都导不进来就不建题集（422，带每道题的原因）。"""
    plan, body = await _plan(session, request)
    actor = actor_of(x_actor)
    if isinstance(body.get("name"), str) and body["name"].strip():
        plan.suite["name"] = body["name"].strip()[:200]
    # 先定好 id：试算的运行记着是哪个题集的（一道都导不进来时题集不建，那几次试算的运行照样留着、点得开）
    suite = EvalSuite(id=new_id(), **plan.suite, created_by=actor)
    ready = [c for c in plan.cases if c.fields is not None]
    gate = asyncio.Semaphore(IMPORT_TRIALS)

    async def trial(item: transfer.PlannedCase) -> gold_rules.GoldOutcome:
        async with gate:
            return await gold_rules.compute_gold({**item.fields, "id": None, "seq": item.seq},
                                                 _context(suite, actor, imported=True))

    passed: list[tuple[transfer.PlannedCase, gold_rules.GoldOutcome]] = []
    for item, outcome in zip(ready, await asyncio.gather(*(trial(c) for c in ready))):
        if outcome.ok:
            passed.append((item, outcome))
        else:
            item.fields, item.reason, item.code = None, f"试算没有通过：{outcome.reason}", outcome.code
    if not passed:
        raise CodedHTTPException(422, "题集里没有一道题能导进来：" + "；".join(
            f"第 {c.seq or '?'} 题：{c.reason}" for c in plan.cases[:10]), IMPORT_INVALID, field="content")
    session.add(suite)
    await session.flush()
    passed.sort(key=lambda pair: (pair[0].seq is None, pair[0].seq or 0))
    for seq, (item, outcome) in enumerate(passed, start=1):
        assert item.fields is not None
        session.add(EvalCase(suite_id=suite.id, seq=seq, case_hash=case_hash(item.fields),
                             trial=_trial_record(outcome, actor), created_by=actor, **item.fields))
    await session.commit()
    detail = await get_suite(suite.id, session)
    return {"suite": detail, "imported": len(passed), "skipped": [c.to_dict() for c in plan.cases if c.fields is None]}


# ==========================================================================
# 目录快照
# ==========================================================================


def _snapshot_error(e: snapshots.SnapshotInvalid) -> CodedHTTPException:
    return CodedHTTPException(e.status, e.message, e.code, field=e.field)


@router.get("/catalog-snapshots")
async def list_snapshots(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(EvalCatalogSnapshot).order_by(EvalCatalogSnapshot.created_at.desc())
                                       .limit(200))).scalars())
    await snapshots.reconcile_stale(session, rows)
    return {"snapshots": [snapshots.snapshot_out(r) for r in rows]}


@router.post("/catalog-snapshots", status_code=201)
async def take_snapshot(request: Request, session: AsyncSession = Depends(get_session),
                        x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """{source_ids}：拍这几个数据源此刻的目录（当场写好、不可变）。"""
    body = await _body(request)
    try:
        row = await snapshots.take_live(session, body.get("source_ids") or [], actor=actor_of(x_actor))
    except snapshots.SnapshotInvalid as e:
        raise _snapshot_error(e) from e
    return snapshots.snapshot_out(row)


async def _shadow_plan(session: AsyncSession, body: dict[str, Any]) -> snapshots.ShadowPlan:
    try:
        _model, model_id = await snapshots.resolve_model(session, body.get("provider"), body.get("model"))
        return await snapshots.plan_shadow(session, body, model_id=model_id)
    except snapshots.SnapshotInvalid as e:
        raise _snapshot_error(e) from e


@router.post("/catalog-snapshots/shadow/estimate")
async def shadow_estimate(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """影子起草的计划和估算（起草哪些表、调几次模型、多少 token、多少钱）：开始之前给人看。不调模型。"""
    return (await _shadow_plan(session, await _body(request))).to_dict()


@router.post("/catalog-snapshots/shadow", status_code=202)
async def start_shadow(request: Request, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """开始影子起草（后台）：{sources: [{id, tables?}], suite_id?, provider?, model?, guidance?, note?, strip_llm?}。
    交回登记的那一行（queued）；进度、用量、结果按 GET /catalog-snapshots/{id} 读。"""
    plan = await _shadow_plan(session, await _body(request))
    row = await snapshots.start_shadow(session, plan, actor=actor_of(x_actor))
    return snapshots.snapshot_out(row)


async def _snapshot(session: AsyncSession, snapshot_id: str) -> EvalCatalogSnapshot:
    row = await session.get(EvalCatalogSnapshot, snapshot_id)
    if row is None:
        raise CodedHTTPException(404, "目录快照不存在", SNAPSHOT_NOT_FOUND)
    return row


@router.get("/catalog-snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _snapshot(session, snapshot_id)
    await snapshots.reconcile_stale(session, [row])
    return snapshots.snapshot_out(row)


@router.post("/catalog-snapshots/{snapshot_id}/stop")
async def stop_snapshot(snapshot_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """停止一次影子起草：不出快照。已经结束的回 409。"""
    await _snapshot(session, snapshot_id)
    if not await snapshots.stop(snapshot_id):
        raise CodedHTTPException(409, "这次影子起草已经结束了", snapshots.SNAPSHOT_FINISHED)
    session.expire_all()
    return snapshots.snapshot_out(await _snapshot(session, snapshot_id))


__all__ = ["router"]
