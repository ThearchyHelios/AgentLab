"""评测接口（docs/design/next-round/01-效果度量与回归评测.md）：题集、题目、期望答案试算、目录快照，评测的发起、进度、结果、
对比、A/B、清理。

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
- 评测（app/eval/runner.py，后台任务）：POST /suites/{id}/evaluations/estimate（发起前的估算：几项、按上一次每题平均花费估的
  金额、生效的预算、同时评几题）；POST /suites/{id}/evaluations（发起，202；variant 一臂，或 arms 几臂的 A/B，或
  preset=catalog / catalog_reviewed「目录贡献」预设对照）；GET /suites/{id}/evaluations（历次）；GET /evaluations/{id}（进度、
  总表、和上一次比的计数）；GET /evaluations/{id}/results（逐题）、GET /evaluations/{id}/results/{rid}（一题的明细）；
  POST /evaluations/{id}/stop、/retry（只重做没做完的）、/rescore（按新评分规则重评，只读封存、不调模型）、/pin（钉住当基线）；
  GET /evaluations/{id}/compare?base=（和上一次或点名的那一次比：指纹差异、逐题转移和归因）；GET /pairs/{pair_id}（A/B 配对表和
  精确 McNemar）；POST /suites/{id}/cleanup（按保留规则清理更早的明细）。「口径命中与重复度」的 E1–E4 直接用口径账本的
  GET /api/ledger/stats（总方案 X2：只有一份实现）。

第 4 波评测页（细案 01 第 18 项）缺的几样，补在这里（统计、能不能做都由这里给，界面只显示）：
- GET /suites 每个题集另带 latest（最近一次评测的状态和准确率）、trend（同一变体最近几次评完的准确率，旧的在前）、schedules
  （目标是这个题集的定时任务）——列表页不再为每个题集再取一遍历次评测；
- GET /evaluations/{id} 另带 actions：停止、只重试没做完的、重新评分、钉住 / 取消基线现在能不能做（和那几个接口拒收的条件
  同一份），以及 open（没做完的项数）；
- POST /suites/{id}/cases/preview：写 SQL 定期望时的第一步——按此刻渲染、过只读守卫、跑一次和期望同一条路的 SQL 探针，交回
  结果的列和前几行（界面上点选格或列）；用到遮罩列的照保存时那一道拒收、照实写原因（不回 422）；
- GET /signals：「口径命中与重复度」页签的判据（细案 copilot-look-and-caliber-ledger §3.8 的参考线：E1 ≥30% / <10%，
  E3 ≥20%）、当前窗口和往前几个窗口各自落在哪一档、成员不少于 3 的问题类（可以加入题集或提取模板）。数都出自
  ledger/stats.compute_stats 和 chat/questions（全站各一份），这里只按窗口调它们、按判据分档。每个窗口都要聚一次问题，页面
  另调 /api/ledger/stats：问题的向量由 chat/questions 在进程里留一份，同一句话只取一次（配了远程向量模型时不按窗口重发）。
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
from app.db.base import SessionLocal, get_session, new_id, utcnow
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


async def _site(session: AsyncSession) -> dict[str, Any]:
    """设置里 eval 一组（全站的预算缺省、保留几次、裁判的缺省开关）。"""
    from app.api.settings import eval_settings

    return await eval_settings(session)


# ==========================================================================
# 形状
# ==========================================================================


def _suite_out(row: EvalSuite, *, counts: dict[str, int] | None = None, site: dict[str, Any] | None = None
               ) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "description": row.description, "timezone": row.timezone,
            "default_variant": dict(row.default_variant or {}), "budget": dict(row.budget or {}),
            "effective_budget": effective_budget(row.budget, site), "archived": bool(row.archived), "version": row.version,
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


async def _trial(session: AsyncSession, suite: EvalSuite, fields: dict[str, Any], actor: str | None, *,
                 case_id: str | None = None, seq: int | None = None) -> gold_rules.GoldOutcome:
    """按此刻试算一次期望。试算要等一次运行跑完（最多 TRIAL_TIMEOUT_S 秒）：先结束这个请求的读事务，不一直开着它等
    （会话的 expire_on_commit 是关的，读好的对象照用）。"""
    await session.commit()
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


#: 列表里每个题集最多看最近几次评测（最近一次、趋势都从这几次里取）；趋势最多几个点
OVERVIEW_RUNS = 30
TREND_POINTS = 8


def _rate_of(run: EvalRun) -> dict[str, Any] | None:
    """一次评测总表里的准确率（stats.rate 的形状：passed、n、rate、ci）；还没有总表的为 None。"""
    acc = (run.summary or {}).get("accuracy")
    return dict(acc) if isinstance(acc, dict) else None


async def _overview(session: AsyncSession, suite_ids: list[str]) -> dict[str, dict[str, Any]]:
    """每个题集：最近一次评测（不论结束没有）、同一变体最近几次评完的准确率（趋势，旧的在前）、目标是它的定时任务。

    趋势只连同一变体（fingerprint.variant_key，和上一次比挑基准同一个认法）的几次：换了模型、换了目录的那几次不连成一条线，
    免得把「换了配置」读成「变好了 / 变差了」。A/B 的几臂各是一次评测，同一变体的那一臂才进趋势。"""
    from app.db.models import Schedule
    from app.eval import runner
    from app.scheduler import cron

    out: dict[str, dict[str, Any]] = {sid: {"latest": None, "trend": [], "schedules": []} for sid in suite_ids}
    if not suite_ids:
        return out
    ranked = select(EvalRun, func.row_number().over(partition_by=EvalRun.suite_id,
                                                    order_by=EvalRun.created_at.desc()).label("n")) \
        .where(EvalRun.suite_id.in_(suite_ids)).subquery()
    from sqlalchemy.orm import aliased

    run_alias = aliased(EvalRun, ranked)
    rows = list((await session.execute(select(run_alias).where(ranked.c.n <= OVERVIEW_RUNS)
                                       .order_by(run_alias.created_at.desc()))).scalars())
    await runner.reconcile_stale(session, rows)
    by_suite: dict[str, list[EvalRun]] = {}
    for row in rows:
        by_suite.setdefault(row.suite_id, []).append(row)
    for sid, runs in by_suite.items():
        latest = runs[0]
        out[sid]["latest"] = {"id": latest.id, "status": latest.status, "running": runner.running(latest),
                              "arm": latest.arm, "pair_id": latest.pair_id, "trigger": latest.trigger,
                              "created_at": _iso(latest.created_at), "finished_at": _iso(latest.finished_at),
                              "counts": dict(latest.counts or {}), "accuracy": _rate_of(latest),
                              "traced_accuracy": dict((latest.summary or {}).get("traced_accuracy") or {}) or None}
        finished = [r for r in runs if r.status in ("succeeded", "partial") and _rate_of(r) is not None]
        if finished:
            key = (finished[0].fingerprint or {}).get("variant_key")
            same = [r for r in finished if (r.fingerprint or {}).get("variant_key") == key][:TREND_POINTS]
            out[sid]["trend"] = [{"id": r.id, "created_at": _iso(r.created_at), "arm": r.arm, **(_rate_of(r) or {})}
                                 for r in reversed(same)]
    schedules = (await session.execute(select(Schedule).where(
        Schedule.target_kind == "eval_suite", Schedule.target_id.in_(suite_ids))
        .order_by(Schedule.created_at))).scalars()
    for row in schedules:
        try:
            text = cron.describe(row.cron, row.timezone)
        except cron.CronError:
            text = f"按 cron 表达式 {row.cron}（{row.timezone}）"
        out[row.target_id]["schedules"].append({"id": row.id, "name": row.name, "enabled": row.enabled,
                                                "description_text": text, "next_run_at": _iso(row.next_run_at)})
    return out


@router.get("/suites")
async def list_suites(archived: bool = False, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """题集（归档的不列，archived=true 时一起列）。每个题集另带 latest、trend、schedules（_overview）。"""
    query = select(EvalSuite).order_by(EvalSuite.updated_at.desc())
    if not archived:
        query = query.where(EvalSuite.archived.is_(False))
    rows = list((await session.execute(query)).scalars())
    site = await _site(session)
    counts: dict[str, dict[str, int]] = {r.id: {"cases": 0, "enabled": 0} for r in rows}
    if rows:
        for suite_id, enabled, n in (await session.execute(
                select(EvalCase.suite_id, EvalCase.enabled, func.count()).where(
                    EvalCase.suite_id.in_(list(counts)), EvalCase.archived.is_(False))
                .group_by(EvalCase.suite_id, EvalCase.enabled))).all():
            counts[suite_id]["cases"] += n
            if enabled:
                counts[suite_id]["enabled"] += n
    overview = await _overview(session, [r.id for r in rows])
    return {"suites": [{**_suite_out(r, counts=counts[r.id], site=site), **overview[r.id]} for r in rows]}


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
    return _suite_out(row, counts={"cases": 0, "enabled": 0}, site=await _site(session))


@router.get("/suites/{suite_id}")
async def get_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """题集和它的题目（归档的题也在，archived 标着）。口径题带上升版状态：pending_upgrades 是待处置的题数（界面顶部提示）。"""
    suite = await _suite(session, suite_id)
    rows = list((await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id)
                                       .order_by(EvalCase.seq))).scalars())
    out = await _cases_out(session, rows)
    active = [c for c in out if not c["archived"]]
    return {**_suite_out(suite, counts={"cases": len(active), "enabled": sum(1 for c in active if c["enabled"])},
                         site=await _site(session)),
            **(await _overview(session, [suite_id]))[suite_id],
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
    outcome = await _trial(session, suite, fields, actor)
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
    return {"trial": (await _trial(session, suite, fields, actor_of(x_actor))).to_dict()}


#: 预览最多交回几行（界面上点选格或列用；多行期望的取值仍由试算按整份结果算）
PREVIEW_ROWS = 50


@router.post("/suites/{suite_id}/cases/preview")
async def preview_sql(suite_id: str, request: Request, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """写 SQL 定期望的第一步：{datasource_id, sql}（SQL 可以带 {{ schedule.* }}）。按此刻渲染、过只读守卫、跑一次和期望同一条路
    的 SQL 探针（裸图探索运行，来源 eval），交回结果的列和前 PREVIEW_ROWS 行，界面上据此点选格或列、再去试算。

    结果里有遮罩列、取不到结果、运行失败、超时的，照保存时那一道拒收、照实写原因（status 不是 ok，reason、code 同试算）——不回
    422：这是在写题的途中，界面把原因落在 SQL 那一格下面。{status, reason, code, columns, rows, row_count, truncated, run_id,
    rendered}。"""
    from app.core import artifact_store
    from app.db.models import Artifact

    suite = await _suite(session, suite_id)
    body = await _body(request)
    probe = {"question": "（预览）", "gold": {"kind": "sql", "datasource_id": body.get("datasource_id"),
                                            "sql": body.get("sql")},
             # 只为跑一次探针：「没有数据」这一种期望不取任何格，取不到值时下面照样交回结果
             "expectations": [{"id": "preview", "kind": "none"}]}
    try:
        fields = normalize_case(probe)
        await resolve_case(session, fields, tz=suite.timezone)
    except CaseInvalid as e:
        # 没选数据源、SQL 空着、变量写错、没过只读守卫：照实写原因、指明是哪一格
        return {"status": "invalid", "reason": e.message, "code": e.code, "field": e.field, "columns": [], "rows": [],
                "row_count": None, "truncated": False, "run_id": None, "rendered": {}}
    outcome = await _trial(session, suite, fields, actor_of(x_actor))
    out: dict[str, Any] = {"status": outcome.status, "reason": outcome.reason, "code": outcome.code, "columns": [],
                           "rows": [], "row_count": None, "truncated": False,
                           "run_id": outcome.run_ids[0] if outcome.run_ids else None,
                           "rendered": dict(outcome.rendered or {})}
    if outcome.status not in ("ok", "no_value") or not outcome.run_ids:
        return out
    async with SessionLocal() as read:
        refs = (await read.execute(select(Artifact.id).where(
            Artifact.run_id == outcome.run_ids[0], Artifact.kind == "query_snapshot",
            Artifact.node_id == gold_rules.QUERY_NODE).order_by(Artifact.created_at.desc()))).scalars().all()
    snapshot = None
    for ref in refs:
        try:
            snapshot = artifact_store.load(ref)
        except ValueError:
            snapshot = None
        if isinstance(snapshot, dict):
            break
    if not isinstance(snapshot, dict):
        return {**out, "status": "failed", "reason": "预览的运行没有留下查询结果", "code": gold_rules.GOLD_FAILED}
    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in snapshot.get("rows") or []]
    return {**out, "status": "ok", "reason": None, "code": None, "columns": [str(c) for c in snapshot.get("columns") or []],
            "rows": rows[:PREVIEW_ROWS], "row_count": snapshot.get("row_count", len(rows)),
            "truncated": bool(snapshot.get("truncated")) or len(rows) > PREVIEW_ROWS, "shown": min(len(rows), PREVIEW_ROWS)}


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
        outcome = await _trial(session, suite, fields, actor, case_id=row.id, seq=row.seq)
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
    outcome = await _trial(session, suite, case_fields(row), actor, case_id=row.id, seq=row.seq)
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
    outcome = await _trial(session, suite, fields, actor)
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
    if policy != "dual":
        # 改钉到新版本：按新版本再核一遍（入口字段、指标），说得清是哪一项不行；dual 的新版由试算一起算
        try:
            await resolve_case(session, fields, tz=suite.timezone)
        except CaseInvalid as e:
            raise _invalid(e) from e
    outcome = await _trial(session, suite, fields, actor, case_id=row.id if policy != "incomparable" else None,
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
    await session.commit()          # 试算要等运行跑完：不一直开着读事务等（同 _trial）
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


# ==========================================================================
# 评测：发起、进度、结果
# ==========================================================================

EVALUATION_NOT_FOUND = "eval_evaluation_not_found"
PAIR_NOT_FOUND = "eval_pair_not_found"
RESULT_NOT_FOUND = "eval_result_not_found"
CLEANED = "eval_details_cleaned"
#: 预设对照
PRESETS = ("catalog", "catalog_reviewed")


def _refused(e: CaseInvalid) -> CodedHTTPException:
    return CodedHTTPException(getattr(e, "status", 422), e.message, e.code, field=e.field)


async def _arms(session: AsyncSession, suite: EvalSuite, body: dict[str, Any]) -> list[dict[str, Any]]:
    """请求体 → 规范化的几臂变体：{variant}（一臂，不写用题集的缺省变体）、{arms: [变体…]}（A/B）或 {preset: catalog |
    catalog_reviewed}（目录贡献对照：当前目录 vs 关，可加只用已核实的项；模型照 variant 或题集的缺省）。A/B 缺省每题 3 次。"""
    from app.eval import variants as variant_rules

    judge = bool((await _site(session))["judge"])
    preset = body.get("preset")
    raw_arms = body.get("arms")
    try:
        if preset is not None:
            if preset not in PRESETS:
                raise CaseInvalid(f"预设对照只能是 {'、'.join(PRESETS)}", "preset", variant_rules.VARIANT_INVALID)
            base = variant_rules.normalize_variant({**dict(suite.default_variant or {}), **dict(body.get("variant") or {})},
                                                   judge_default=judge, repeats_default=variant_rules.PAIR_REPEATS)
            return variant_rules.catalog_preset(base, reviewed=preset == "catalog_reviewed")
        if raw_arms is not None:
            if not isinstance(raw_arms, list) or not 2 <= len(raw_arms) <= variant_rules.ARMS_MAX:
                raise CaseInvalid(f"A/B 要 2 到 {variant_rules.ARMS_MAX} 臂", "arms", variant_rules.VARIANT_INVALID)
            arms = [variant_rules.normalize_variant(a, judge_default=judge, repeats_default=variant_rules.PAIR_REPEATS)
                    for a in raw_arms]
            names = [variant_rules.arm_name(a, i) for i, a in enumerate(arms)]
            if len(set(names)) != len(names):
                raise CaseInvalid("几臂的名字（label）不能重复", "arms", variant_rules.VARIANT_INVALID)
            return arms
        return [variant_rules.normalize_variant({**dict(suite.default_variant or {}), **dict(body.get("variant") or {})},
                                                judge_default=judge)]
    except CaseInvalid as e:
        raise _invalid(e) from e


def _evaluation_out(row: EvalRun) -> dict[str, Any]:
    from app.eval import runner, variants as variant_rules

    return {"id": row.id, "suite_id": row.suite_id, "status": row.status, "trigger": row.trigger,
            "schedule_id": row.schedule_id, "variant": dict(row.variant or {}),
            "resolved_models": dict(row.resolved_models or {}),
            "variant_text": variant_rules.describe(row.variant or {}, row.resolved_models or {}),
            "catalog_snapshot": row.catalog_snapshot, "as_of": _iso(row.as_of), "repeats": row.repeats,
            "fingerprint": dict(row.fingerprint or {}), "pair_id": row.pair_id, "arm": row.arm,
            "baseline_id": row.baseline_id, "pinned": bool(row.pinned), "budget": dict(row.budget or {}),
            "counts": dict(row.counts or {}), "usage": dict(row.usage or {}), "summary": dict(row.summary or {}),
            "stop_reason": row.stop_reason, "error": row.error, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "started_at": _iso(row.started_at), "finished_at": _iso(row.finished_at),
            "cleaned_at": _iso(row.cleaned_at), "running": runner.running(row)}


async def _evaluation(session: AsyncSession, evaluation_id: str) -> EvalRun:
    from app.eval import runner

    row = await session.get(EvalRun, evaluation_id)
    if row is None:
        raise CodedHTTPException(404, "评测不存在", EVALUATION_NOT_FOUND)
    await runner.reconcile_stale(session, [row])
    return row


@router.post("/suites/{suite_id}/evaluations/estimate")
async def estimate_evaluation(suite_id: str, request: Request, session: AsyncSession = Depends(get_session)
                              ) -> dict[str, Any]:
    """发起之前给人看：几臂、各臂实际解析出的模型、几项、估算的花费（按这个题集上一次评测的每题平均花费）、生效的预算、
    同时评几题、今天评测已经花了多少。不调模型、不查库。"""
    from app.core.config import settings
    from app.engine.spend import eval_spend
    from app.eval import runner, variants as variant_rules
    from app.eval.suites import effective_budget

    suite = await _suite(session, suite_id)
    arms = await _arms(session, suite, await _body(request))
    out = []
    try:
        for i, arm in enumerate(arms):
            models = await variant_rules.resolve_models(session, arm)
            await variant_rules.snapshot_artifact(session, arm)
            out.append({"label": variant_rules.arm_name(arm, i) if len(arms) > 1 else None, "variant": arm,
                        "models": models, "text": variant_rules.describe(arm, models)})
    except CaseInvalid as e:
        raise _refused(e) from e
    cases = len(await runner._cases(session, suite_id))
    repeats = max(int(a.get("repeats") or 1) for a in arms)
    budget = effective_budget(suite.budget, await _site(session))
    parallel = max(1, min(int(budget["max_parallel"]), int(settings.max_concurrent_runs) // 4 // len(arms) or 1))
    return {"arms": out, "cases": cases, "repeats": repeats,
            "estimate": await runner.estimate(session, suite_id, cases * repeats * len(arms)), "budget": budget,
            "parallel": parallel, "spend": await eval_spend.daily()}


@router.post("/suites/{suite_id}/evaluations", status_code=202)
async def start_evaluation(suite_id: str, request: Request, session: AsyncSession = Depends(get_session),
                           x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """发起一次评测（后台）：{variant?} 一臂，或 {arms: [变体…]} / {preset} 几臂的 A/B（共用期望、同一题同时开跑），可带
    baseline_id 点名和哪一次比。交回 {evaluations: [每一臂], pair_id}；进度按 GET /evaluations/{id} 读。"""
    from app.eval import runner

    suite = await _suite(session, suite_id)
    body = await _body(request)
    arms = await _arms(session, suite, body)
    baseline = body.get("baseline_id")
    if baseline is not None and not isinstance(baseline, str):
        raise CodedHTTPException(422, "基线需要是一次评测的 id", runner.EVAL_INVALID, field="baseline_id")
    try:
        runs = await runner.start(session, suite, arms, actor=actor_of(x_actor), baseline_id=baseline or None)
    except CaseInvalid as e:
        raise _refused(e) from e
    return {"evaluations": [_evaluation_out(r) for r in runs], "pair_id": runs[0].pair_id}


@router.get("/suites/{suite_id}/evaluations")
async def list_evaluations(suite_id: str, limit: int = 50, session: AsyncSession = Depends(get_session)
                           ) -> dict[str, Any]:
    """这个题集的历次评测（新的在前；明细清理了的总表还在）。"""
    from app.eval import runner

    await _suite(session, suite_id)
    rows = list((await session.execute(select(EvalRun).where(EvalRun.suite_id == suite_id)
                                       .order_by(EvalRun.created_at.desc()).limit(max(1, min(limit, 200))))).scalars())
    await runner.reconcile_stale(session, rows)
    return {"evaluations": [_evaluation_out(r) for r in rows]}


@router.get("/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一次评测：进度（counts、正在评的几题在做什么）、总表（summary，结束后才有）、和上一次比的计数（summary.baseline）。"""
    from app.db.models_eval import EvalResult

    row = await _evaluation(session, evaluation_id)
    running = (await session.execute(select(EvalResult.case_seq, EvalResult.repeat, EvalResult.arm, EvalResult.current)
                                     .where(EvalResult.eval_run_id == row.id, EvalResult.status == "running")
                                     .order_by(EvalResult.seq))).all()
    return {**_evaluation_out(row),
            "current": [{"case_seq": seq, "repeat": rep, "arm": arm, "doing": doing} for seq, rep, arm, doing in running],
            "actions": await _actions(session, row)}


async def _actions(session: AsyncSession, row: EvalRun) -> dict[str, Any]:
    """这次评测现在能做哪几件事（界面照它摆按钮，不自己判）：和 stop / retry / rescore / pin 几个接口拒收的条件同一份。
    open 是这次评测（A/B 的话几臂合起来）还没做完的项数：停止、中断、出错、到了上限没开评的，重试就是把它们接着做。"""
    from app.core import job_runtime as runtime
    from app.db.models_eval import EvalResult
    from app.eval import runner

    active = row.status in runtime.ACTIVE
    leader = runner.leader_of(row)
    arms = list((await session.execute(select(EvalRun.id).where(
        (EvalRun.id == leader) | (EvalRun.pair_id == leader)))).scalars())
    open_items = int(await session.scalar(select(func.count()).select_from(EvalResult).where(
        EvalResult.eval_run_id.in_(arms), EvalResult.status != "ok")) or 0)
    suite = await session.get(EvalSuite, row.suite_id)
    cleaned = row.cleaned_at is not None
    finished = row.status in ("succeeded", "partial")
    return {"stop": active, "retry": not active and not cleaned and open_items > 0 and suite is not None
            and not suite.archived, "rescore": not active and not cleaned,
            "pin": finished and not row.pinned, "unpin": bool(row.pinned), "open": open_items, "cleaned": cleaned}


@router.get("/evaluations/{evaluation_id}/results")
async def evaluation_results(evaluation_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """逐题结果：结论、各期望的结论（期望值和答案里对上的数、有没有引用、维度）、作答路径、复核、出具档位、SQL 差在哪、look、
    耗时、按角色的用量，点 run_id 去运行详情。明细清理了的照实写 cleaned。"""
    from app.eval import runner

    row = await _evaluation(session, evaluation_id)
    results = [runner.result_dict(r) for r in (await session.execute(
        select(runner.EvalResult).where(runner.EvalResult.eval_run_id == row.id)
        .order_by(runner.EvalResult.seq))).scalars()]
    questions = dict((await session.execute(select(EvalCase.id, EvalCase.question).where(
        EvalCase.id.in_({r["case_id"] for r in results if r["case_id"]})))).all()) if results else {}
    for r in results:
        r["question"] = questions.get(r["case_id"])
        r["cost"] = _result_cost(r.get("usage") or {})
    return {"evaluation_id": row.id, "cleaned": row.cleaned_at is not None, "results": results}


def _result_cost(usage: dict[str, Any]) -> dict[str, Any]:
    """一题的花费：助手（runner 记的 assistant 已含复核）+ 写作 + 结论裁判，和这次评测记在账上的同一份；估不出金额的调用另计
    次数（界面写「≥」或「金额无法估算」）。加在服务端：界面不算数。"""
    roles = [usage.get(k) or {} for k in ("assistant", "writer", "judge")]
    return {"cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in roles), 6),
            "unpriced_calls": sum(int(r.get("unpriced_calls") or 0) for r in roles)}


@router.get("/evaluations/{evaluation_id}/results/{result_id}")
async def evaluation_result(evaluation_id: str, result_id: str, session: AsyncSession = Depends(get_session)
                            ) -> dict[str, Any]:
    """一题的明细：结果的列，加上明细工件（这一轮作答的经过、读出来的答案、裁判的判断、评分的原样）。"""
    from app.core import artifact_store
    from app.eval import runner

    row = await _evaluation(session, evaluation_id)
    result = await session.get(runner.EvalResult, result_id)
    if result is None or result.eval_run_id != row.id:
        raise CodedHTTPException(404, "这一题的结果不存在（明细可能已清理）", RESULT_NOT_FOUND)
    details = None
    if result.details_artifact:
        try:
            details = artifact_store.load(result.details_artifact)
        except ValueError:
            details = None
    return {**runner.result_dict(result), "details": details}


@router.post("/evaluations/{evaluation_id}/stop")
async def stop_evaluation(evaluation_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """停止（A/B 几臂一起停）。已经结束的回 409。正在跑的作答、算期望的运行一并取消。"""
    from app.eval import runner

    await _evaluation(session, evaluation_id)
    await session.commit()
    if not await runner.stop(evaluation_id):
        raise CodedHTTPException(409, "这次评测已经结束了", runner.EVAL_FINISHED)
    session.expire_all()
    return _evaluation_out(await _evaluation(session, evaluation_id))


@router.post("/evaluations/{evaluation_id}/retry", status_code=202)
async def retry_evaluation(evaluation_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """只把没做完的题接着做（停止、中断、出错、到了上限的那几题；A/B 时同一题几臂一起重做）。"""
    from app.eval import runner

    await _evaluation(session, evaluation_id)
    await session.commit()
    try:
        ids = await runner.retry(evaluation_id)
    except CaseInvalid as e:
        raise _refused(e) from e
    session.expire_all()
    return {"evaluations": [_evaluation_out(await _evaluation(session, i)) for i in ids]}


@router.post("/evaluations/{evaluation_id}/rescore")
async def rescore_evaluation(evaluation_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按现在的评分规则（score.SCORER_VERSION）重新评分：只读封存的作答运行和存下的期望、裁判判断，不调模型、不查业务库。
    总表跟着重算。还在跑、明细已清理的不能重评。"""
    from app.eval import rescore, runner

    row = await _evaluation(session, evaluation_id)
    if row.status in ("queued", "running"):
        raise CodedHTTPException(409, "这次评测还没结束，结束后再重新评分", runner.EVAL_BUSY)
    if row.cleaned_at is not None:
        raise CodedHTTPException(409, "这次评测的明细已清理，不能重新评分", CLEANED)
    await session.commit()
    changed = await rescore.rescore(evaluation_id)
    session.expire_all()
    return {**_evaluation_out(await _evaluation(session, evaluation_id)), "changed": changed}


@router.post("/evaluations/{evaluation_id}/pin")
async def pin_evaluation(evaluation_id: str, request: Request, session: AsyncSession = Depends(get_session)
                         ) -> dict[str, Any]:
    """钉住当基线（{pinned: true}）或取消（false）：和上一次比时优先用同一变体里钉住的那一次，清理时也留着它。"""
    row = await _evaluation(session, evaluation_id)
    body = await _body(request)
    pinned = body.get("pinned")
    if not isinstance(pinned, bool):
        raise CodedHTTPException(422, "pinned 需要是 true 或 false", case_rules.CASE_INVALID, field="pinned")
    if pinned and row.status not in ("succeeded", "partial"):
        raise CodedHTTPException(409, "只有评完了（或部分评完）的评测能设为基线", case_rules.CASE_INVALID, field="pinned")
    row.pinned = pinned
    await session.commit()
    await session.refresh(row)
    return _evaluation_out(row)


@router.get("/evaluations/{evaluation_id}/compare")
async def compare_evaluation(evaluation_id: str, base: str | None = None, session: AsyncSession = Depends(get_session)
                             ) -> dict[str, Any]:
    """和上一次比：base 不给就按规则挑（同一变体钉住的基线，或比这次先发起、已经结束的最近一次）。{base, fingerprint_diff,
    transitions: {counts, causes, items}}；没有可比的 base 为 null。任何一边的明细已清理，逐题部分为空、照实写 cleaned。"""
    from app.eval import compare, runner

    row = await _evaluation(session, evaluation_id)
    picked = await compare.pick_baseline(session, row, explicit=base or row.baseline_id)
    if picked is None:
        return {"base": None, "fingerprint_diff": [], "transitions": None, "cleaned": False}
    cleaned = row.cleaned_at is not None or picked.cleaned_at is not None
    moved = None if cleaned else compare.transitions(await runner.result_rows(session, picked.id),
                                                     await runner.result_rows(session, row.id))
    return {"base": _evaluation_out(picked),
            "fingerprint_diff": compare.fingerprint_diff(picked.fingerprint or {}, row.fingerprint or {}),
            "transitions": moved, "cleaned": cleaned}


@router.get("/pairs/{pair_id}")
async def get_pair(pair_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一次 A/B：几臂各自的准确率、两两配对表（都对 / 都错 / 只 A 对 / 只 B 对）和精确 McNemar 双侧 p 值；p ≥ 0.05 写
    「这批题上看不出差别」。几臂共用期望答案。"""
    from app.eval import compare, runner

    rows = list((await session.execute(select(EvalRun).where(EvalRun.pair_id == pair_id)
                                       .order_by(EvalRun.created_at))).scalars())
    if not rows:
        raise CodedHTTPException(404, "这次 A/B 不存在", PAIR_NOT_FOUND)
    await runner.reconcile_stale(session, rows)
    cleaned = any(r.cleaned_at is not None for r in rows)
    table = None if cleaned else compare.pair_table(
        [(r.arm or r.id, await runner.result_rows(session, r.id)) for r in rows])
    return {"pair_id": pair_id, "evaluations": [_evaluation_out(r) for r in rows], "table": table, "cleaned": cleaned}


# ==========================================================================
# 口径命中与重复度（E1、E3）：判据、分档、窗口趋势、成员不少于 3 的问题类
# ==========================================================================

#: 判据（docs/design/copilot-look-and-caliber-ledger.md §3.8，已修订为持续统计的参考线）：E1 等价命中率 ≥30% 值得按口径走、
#: <10% 先只做对账、介于两者之间先加强规范化；E3 问题重复度 ≥20% 沉淀有价值。只作参考，不卡门
E1_ROUTE, E1_LOW, E3_WORTH = 0.30, 0.10, 0.20
SIGNAL_CRITERIA = {
    "e1": [{"band": "route", "at_least": E1_ROUTE, "text": "等价命中率 ≥ 30%：值得按已治理的口径走"},
           {"band": "normalize", "at_least": E1_LOW, "text": "10%–30%：先加强规范化，让写法不同的等价 SQL 认得出"},
           {"band": "reconcile", "at_least": 0.0, "text": "< 10%：先只做口径对账"}],
    "e3": [{"band": "worth", "at_least": E3_WORTH, "text": "问题重复度 ≥ 20%：把反复问的问题沉淀成口径有价值"},
           {"band": "not_yet", "at_least": 0.0, "text": "< 20%：反复问的问题还不多"}],
    "note": "参考线出自口径账本的设计文档，只作参考，不卡门",
}
SIGNAL_DAYS_MAX = 365
SIGNAL_POINTS_MAX = 12
#: 每类最多列几个问题
CLUSTER_MEMBERS = 10


def _e1_band(rate: float | None) -> str | None:
    if rate is None:
        return None
    return "route" if rate >= E1_ROUTE else "normalize" if rate >= E1_LOW else "reconcile"


def _e3_band(share: float | None) -> str | None:
    if share is None:
        return None
    return "worth" if share >= E3_WORTH else "not_yet"


def _point(stats: dict[str, Any], since: Any, until: Any) -> dict[str, Any]:
    """一个窗口的 E1、E3（取自 ledger/stats.compute_stats 的结果，不另算）和各自落在哪一档。"""
    e1, e3 = stats.get("e1") or {}, stats.get("e3") or {}
    rate = (e1.get("rates") or {}).get("proven")
    share = e3.get("cluster_share")
    return {"since": _iso(since), "until": _iso(until),
            "e1": {"rate": rate, "n": int(e1.get("with_candidates") or 0), "queries": int(e1.get("queries") or 0),
                   "band": _e1_band(rate)},
            "e3": {"share": share, "n": int(e3.get("questions") or 0), "lower_bound": e3.get("repeated_share"),
                   "band": _e3_band(share)}}


@router.get("/signals")
async def signals(days: int = 30, points: int = 6, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """「口径命中与重复度」页签：判据、最近 days 天和往前 points - 1 个同样长的窗口各自的 E1、E3（趋势，旧的在前），以及最近
    days 天里成员不少于 3 的问题类。

    E1、E3 的数只出自口径账本的 compute_stats（总方案 X2，全站一份；评测运行不计入，见 engine/origin.counted_runs），问题类只出自
    chat/questions（和 E3、认证队列同一份：只取问题原文、不含 history、排除系统派生的运行）。界面上的详细分布另调
    GET /api/ledger/stats。每一类给代表问题、最近几个成员和最近一个挂着运行的成员（「提取模板」就从那次运行提取）。"""
    from datetime import timedelta

    from app.api.deps import current_principal
    from app.chat.questions import cluster_questions, embedder, recent_questions
    from app.ledger.stats import QUESTION_LIMIT, REPEAT_RUNS, compute_stats

    if not 1 <= days <= SIGNAL_DAYS_MAX:
        raise CodedHTTPException(422, f"窗口需要是 1 到 {SIGNAL_DAYS_MAX} 天", case_rules.CASE_INVALID, field="days")
    if not 1 <= points <= SIGNAL_POINTS_MAX:
        raise CodedHTTPException(422, f"趋势需要是 1 到 {SIGNAL_POINTS_MAX} 个窗口", case_rules.CASE_INVALID,
                                 field="points")
    principal = current_principal()
    now = utcnow()
    span = timedelta(days=days)
    trend = []
    for i in reversed(range(points)):
        until = now - span * i
        since = until - span
        trend.append(_point(await compute_stats(session, since=since, until=until, principal=principal), since, until))
    since = now - span
    questions = [q for q in await recent_questions(session, principal, limit=QUESTION_LIMIT)
                 if q.at is None or q.at >= since]
    clusters = []
    for c in await cluster_questions(questions) if questions else []:
        if c.count < REPEAT_RUNS:
            continue
        with_run = next((m for m in c.members if m.run_id), None)
        clusters.append({"count": c.count, "representative": c.members[0].text[:500],
                         "first_at": _iso(c.members[-1].at), "last_at": _iso(c.members[0].at),
                         "members": [{"text": m.text[:500], "at": _iso(m.at), "run_id": m.run_id, "turn_id": m.turn_id}
                                     for m in c.members[:CLUSTER_MEMBERS]],
                         "run_id": with_run.run_id if with_run else None})
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return {"window": {"since": _iso(since), "until": _iso(now), "days": days}, "criteria": SIGNAL_CRITERIA,
            "current": trend[-1], "trend": trend, "clusters": clusters, "questions": len(questions),
            "threshold": REPEAT_RUNS, "embedder": embedder()}


@router.post("/suites/{suite_id}/cleanup")
async def cleanup_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按保留规则清理这个题集更早的评测明细（最近 keep_runs 次、钉住的基线、A/B 的另几臂留着；总表永久保留）。"""
    from app.eval import cleanup

    await _suite(session, suite_id)
    await session.commit()
    return await cleanup.clean_suite(suite_id)


__all__ = ["router"]

