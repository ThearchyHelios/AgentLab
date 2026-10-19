"""评测执行：一次评测一个后台任务，每题 × 重复 × 臂一项（细案 01 做法 9；评测-D9、D10；总方案 X5）。

**后台任务。** 用 core/job_runtime（和数据目录的起草、剖析，影子起草同一套生命周期）：进程内一个 asyncio 任务；能停；
写库经 SerialWriter 一条一条来、停止时不被打断；服务重启时排队、进行中的标中断，**不自动续跑**——复制出来的数据目录
（检查脚本、验收副本）启动时不能自己去调模型、查业务库，界面上点「重试」只把没做完的题接着做。

**一次评测**是 EvalRun 一行；A/B（几臂一次发起）是几行，共用 pair_id（就是第一臂的 id，它也是后台任务的编号）：两臂共用
期望答案、同一题同时开跑（gold.bracket 在作答那一步里同时跑几臂），所以一个任务跑全部几臂。每题 × 重复 × 臂是一项
EvalResult：建评测时就全部建好（pending），进度、「正在做什么」（current）随时落库。

**不占数据源的队。** 开评时拍一份当前目录的快照（目录选「当前目录」「只用已核实的项」的都按它读，data/catalog_view 的
snapshot 视图）：起草、剖析照常进行，也不影响这次评测的可比性。各数据源的上传表格版本、表结构哈希开始和结束各记一次，
变了在指纹里标出（sources_changed）。

**并发。** 同时最多 max_parallel 题，且所有臂合起来不超过全局运行并发的四分之一（全局只有一个信号量、没有优先级，评测不能
挤占真实用户的运行）。A/B 的同一题几臂同时开跑。每题时限 per_case_timeout_s：作答一臂超时就取消它正在跑的运行、记超时；
期望探针每次也按这个时限。

**预算**（评测-D9；单独一本账 engine/spend.eval_spend，不挤占裁判、核查的额度）：开跑前按上一次的每题平均花费估算（estimate）。
每题开评前按估算预留——加上会超过这次评测的金额上限、或评测每日上限就不再开新题，剩下的记「已到上限」；评完按实际找补。
结论裁判、文字要点的花费在判的时候直接记在这本账上（judging.py），不再找补一次。估不出金额的模型（不在价格目录里）只计
次数，金额上限对它不起作用，照实记在用量里。

**止损**：连续 STOP_COPILOT 题助手调用失败（模型没配好、助手本轮未完成），或最近 STOP_WINDOW 题里有 STOP_TIMEOUTS 题超时，
就不再开新题、原因照实写。

**收尾。** 每一臂按逐题结果算总表（stats.summarize，永久保留）、挑基准、和上一次比（compare.py）；定时发起的，有一道题从通过
变成不通过就推「定时评测有退步」，没评完（失败、到了上限、止损）推「定时评测没有评完」——消息里只有题集名、通过数、退步题数
和链接，不放期望值也不放答案里的数（notify/service.notify_eval）。手动发起的不推：那一次人自己在看，事件的叫法也照实带
「定时」。最后按保留规则清理更早的评测明细（cleanup.py，在后台做）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import job_runtime as runtime
from app.db.base import current_owner, SessionLocal, utcnow
from app.db.models_eval import EvalCase, EvalResult, EvalRun, EvalSuite
from app.eval.cases import CaseInvalid

logger = logging.getLogger(__name__)

KIND = "eval_run"
#: 机器码
EVAL_INVALID = "eval_run_invalid"
EVAL_BUSY = "eval_run_busy"
EVAL_FINISHED = "eval_run_finished"
NOTHING_TO_RETRY = "eval_nothing_to_retry"
#: 止损（细案 01 做法 9）
STOP_COPILOT = 3
STOP_WINDOW = 10
STOP_TIMEOUTS = 7
#: 说法
INTERRUPTED = "服务重启时评测还没做完，没有自动继续。可以点「重试」只把没做完的题接着做"
STOPPED = "评测已停止"
NOT_STARTED = "评测停下时这道题还没开评"
LIMIT_RUN = "已到这次评测的金额上限（${limit:g}），没有再开新题"
LIMIT_DAILY = "评测今天的金额已到上限（${limit:g}），没有再开新题"
STOP_COPILOT_TEXT = "连续 {n} 题助手调用失败，评测停下了：{reason}"
STOP_TIMEOUT_TEXT = "最近 {window} 题里有 {n} 题超时，评测停下了"
CASE_GONE = "这道题已被删除"
TIMED_OUT = "超过每题时限（{s:g} 秒），已停止这一题正在执行的运行"
#: 结束后多久做清理（在后台、不挡收尾）
DETAILS_KIND = "eval_result"
DETAILS_FORMAT = 1


class EvalRefused(CaseInvalid):
    """发起、停止、重试不了：message 给人看，code 给程序认，status 是 HTTP 状态码。"""

    def __init__(self, message: str, code: str = EVAL_INVALID, status: int = 422, field_name: str | None = None) -> None:
        super().__init__(message, field_name, code)
        self.status = status


def leader_of(run: Any) -> str:
    """跑这一次评测的后台任务的编号：A/B 是第一臂的 id（pair_id），单臂就是自己。"""
    return run.pair_id or run.id


# ==========================================================================
# 指纹、估算
# ==========================================================================


def prompt_fingerprint() -> str:
    """助手提示词的指纹：节点手册、输出协议、问数据的请求包法、重来的两种措辞、复核的规则。改了任何一段，和上一次比时
    就标「提示词变了」。"""
    from app.api import copilot
    from app.chat import turn
    from app.core.artifact_store import canonical_json, content_hash
    from app.engine import review

    parts = [copilot.NODE_REFERENCE, copilot._STREAM_PROTOCOL,
             copilot._user_message(copilot.GenerateIn(instruction="{question}", intent="answer"), patch=False),
             turn.RETRY_WITH_BASE, turn.RETRY_FROM_SCRATCH,
             str(getattr(review, "REVIEW_SYSTEM", "") or getattr(review, "SYSTEM_PROMPT", ""))]
    return content_hash(canonical_json(parts))[:16]


async def source_versions(session: AsyncSession, source_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """各数据源此刻的上传表格版本（当前快照）和表结构哈希：{源 id: {name, snapshot, schema}}。"""
    from app.core.artifact_store import canonical_json, content_hash
    from app.db.models import DataSource

    out: dict[str, dict[str, Any]] = {}
    for sid in dict.fromkeys(source_ids):
        row = await session.get(DataSource, sid)
        if row is None:
            out[sid] = {"name": None, "snapshot": None, "schema": None, "gone": True}
            continue
        out[sid] = {"name": row.name, "snapshot": getattr(row, "current_snapshot_id", None),
                    "schema": content_hash(canonical_json(row.schema_cache or {}))[:16]}
    return out


async def estimate(session: AsyncSession, suite_id: str, items: int) -> dict[str, Any]:
    """开跑前的估算：按这个题集上一次结束的评测的每题平均花费 × 这次的项数。第一次评测估不出，照实写。

    上一次有估不出金额的调用（模型不在价格目录里、用量没报全）时，平均花费只是估得出的那部分：一分都没估出来的照实写估不出
    （以前写「约 $0」，像不花钱），估出了一部分的写「至少」（at_least）、带上另有几次估不出（unpriced_calls）。老总表里每题
    平均记成 0、却有估不出的调用的，也按估不出算。"""
    row = (await session.execute(select(EvalRun).where(
        EvalRun.suite_id == suite_id, EvalRun.status.in_(("succeeded", "partial")))
        .order_by(EvalRun.created_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return {"items": items, "per_item_usd": None, "total_usd": None, "basis": None,
                "note": "这个题集还没有评完过，估不出花费：按每题开评前的实际花费累计，到了上限就停"}
    summary = row.summary or {}
    per_item = summary.get("per_case_cost_usd")
    unpriced = int(summary.get("unpriced_calls") or 0)
    if per_item is None or (unpriced and not float(per_item)):
        if not unpriced:
            return {"items": items, "per_item_usd": None, "total_usd": None, "basis": None,
                    "note": "这个题集上一次评测没有评完一道题，估不出花费：按每题开评前的实际花费累计，到了上限就停"}
        return {"items": items, "per_item_usd": None, "total_usd": None, "basis": row.id, "unpriced_calls": unpriced,
                "note": f"上一次评测的 {unpriced} 次模型调用金额都估不出（模型不在价格目录中，或用量没有报全）："
                        "金额上限只管得住估得出金额的调用"}
    out = {"items": items, "per_item_usd": round(float(per_item), 6), "total_usd": round(float(per_item) * items, 4),
           "basis": row.id, "note": "按这个题集上一次评测的每题平均花费估算"}
    if unpriced:
        out.update(at_least=True, unpriced_calls=unpriced,
                   note=f"按这个题集上一次评测的每题平均花费估算，实际至少这么多：上一次另有 {unpriced} 次调用金额估不出，没有算进去")
    return out


def judge_usage(judge: Mapping[str, Any], cost_usd: float) -> dict[str, Any]:
    """按角色的用量里裁判那一份：金额、次数，和估不出金额的次数（结论裁判的模型不在价格目录里时它的每次调用都估不出；
    文字要点按计量汇总）。以前只有金额和次数，裁判模型不在价格目录里时写成 $0。"""
    claims = judge.get("claims") or {}
    text = judge.get("text") or {}
    calls = int(claims.get("calls") or 0) + int(text.get("calls") or 0)
    unpriced = (int(claims.get("calls") or 0) if claims.get("priced") is False else 0) \
        + int((text.get("usage") or {}).get("unpriced_calls") or 0)
    return {"cost_usd": round(cost_usd, 6), "calls": calls, "unpriced_calls": unpriced}


# ==========================================================================
# 发起
# ==========================================================================


async def _cases(session: AsyncSession, suite_id: str) -> list[EvalCase]:
    return list((await session.execute(select(EvalCase).where(
        EvalCase.suite_id == suite_id, EvalCase.enabled.is_(True), EvalCase.archived.is_(False))
        .order_by(EvalCase.seq))).scalars())


async def active_run(session: AsyncSession, suite_id: str) -> EvalRun | None:
    """这个题集还没结束的评测（排队、进行中，而且真在这个进程里跑着的）。记着进行中、却没在跑的（服务重启过）先照实标中断。"""
    rows = list((await session.execute(select(EvalRun).where(
        EvalRun.suite_id == suite_id, EvalRun.status.in_(runtime.ACTIVE)))).scalars())
    await reconcile_stale(session, rows)
    return next((r for r in rows if r.status in runtime.ACTIVE), None)


async def start(session: AsyncSession, suite: EvalSuite, variants: Sequence[Mapping[str, Any]], *, actor: str | None,
                trigger: str = "manual", baseline_id: str | None = None, schedule_id: str | None = None,
                as_of: datetime | None = None, cron: str | None = None) -> list[EvalRun]:
    """建一次评测（几臂就几行）、逐项建好、交给后台。variants 是规范化过的变体（variants.normalize_variant）。不发起抛
    EvalRefused / CaseInvalid。"""
    from app.api.settings import eval_settings
    from app.eval import variants as variant_rules
    from app.eval.suites import effective_budget

    if suite.archived:
        raise EvalRefused("题集已归档，不能发起评测", status=409)
    if not variants or len(variants) > variant_rules.ARMS_MAX:
        raise EvalRefused(f"一次评测要有 1 到 {variant_rules.ARMS_MAX} 臂", field_name="arms")
    busy = await active_run(session, suite.id)
    if busy is not None:
        raise EvalRefused(f"这个题集上一次评测 #{busy.id[:6]} 还没结束，结束后再发起", EVAL_BUSY, 409)
    cases = await _cases(session, suite.id)
    if not cases:
        raise EvalRefused("题集里没有启用的题目", status=409)
    repeats = max(int(v.get("repeats") or 1) for v in variants)
    resolved: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
    for variant in variants:
        models = await variant_rules.resolve_models(session, variant)
        snapshot = await variant_rules.snapshot_artifact(session, variant)
        resolved.append((dict(variant), models, snapshot))
    site = await eval_settings(session)
    budget = effective_budget(suite.budget, site)
    items = len(cases) * repeats * len(variants)
    budget["estimate"] = await estimate(session, suite.id, items)
    if baseline_id is not None:
        base = await session.get(EvalRun, baseline_id)
        if base is None or base.suite_id != suite.id:
            raise EvalRefused("点名的基线不是这个题集的评测", field_name="baseline_id")
    now = utcnow()
    moment = as_of or now
    runs: list[EvalRun] = []
    leader_id = None
    for index, (variant, models, snapshot) in enumerate(resolved):
        run = EvalRun(suite_id=suite.id, status="queued",
                      trigger="pair" if len(resolved) > 1 and trigger == "manual" else trigger,
                      schedule_id=schedule_id, variant={**variant, "repeats": repeats}, resolved_models=models,
                      as_of=moment, repeats=repeats, baseline_id=baseline_id, budget=dict(budget), created_by=actor,
                      arm=variant_rules.arm_name(variant, index) if len(resolved) > 1 else None,
                      fingerprint={"variant_key": variant_rules.variant_key(variant, models),
                                   "assistant_model": models.get("assistant"), "writer_model": models.get("writer"),
                                   "catalog_mode": (variant.get("catalog") or {}).get("mode"),
                                   "snapshot_artifact": snapshot, "cron": cron},
                      counts={"total": len(cases) * repeats, "done": 0})
        session.add(run)
        await session.flush()
        if leader_id is None:
            leader_id = run.id
        if len(resolved) > 1:
            run.pair_id = leader_id
        seq = 0
        for case in cases:
            for repeat in range(repeats):
                seq += 1
                session.add(EvalResult(eval_run_id=run.id, seq=seq, case_id=case.id, case_seq=case.seq,
                                       case_hash=case.case_hash, repeat=repeat, arm=run.arm, status="pending"))
        runs.append(run)
    await session.commit()
    # 提交之后立刻交给后台（中间不 await）：别的请求读到「排队中」时它已经在这个进程里跑着，不会被当成服务重启留下的
    # 而标中断（reconcile_stale）
    manager.start(leader_id)
    for run in runs:
        await session.refresh(run)
    return runs


# ==========================================================================
# 执行
# ==========================================================================


@dataclass
class _Arm:
    run_id: str
    variant: dict[str, Any]
    models: dict[str, Any]
    view: dict[str, Any] | None = None
    #: 这一臂的名字（A/B 时）
    name: str | None = None


@dataclass
class _Unit:
    case_id: str
    repeat: int
    #: {臂的运行 id: 结果 id}
    results: dict[str, str]


class EvalExecution(runtime.JobRun):
    """一次评测（A/B 是几臂一起）在后台的执行。不占数据源的队。"""

    kind = KIND
    model = EvalRun

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.arms: list[_Arm] = []
        self.suite: dict[str, Any] = {}
        self.cases: dict[str, dict[str, Any]] = {}
        self.budget: dict[str, Any] = {}
        self.as_of: datetime | None = None
        self.cron: str | None = None
        self.actor: str | None = None
        self.spent = 0.0
        self.reserved = 0.0
        self.halt: str | None = None
        self.copilot_streak = 0
        self.copilot_reason = ""
        self.timeouts: list[bool] = []
        self.tasks: set[asyncio.Task[Any]] = set()

    def lane_of(self, job: Any) -> str | None:
        return None

    # ---------------------------------------------------------------- 认领、准备

    async def _claim(self, session: AsyncSession) -> bool:
        claimed = await runtime.claim(session, EvalRun, self.job_id)
        if claimed:
            await session.execute(update(EvalRun).where(
                EvalRun.pair_id == self.job_id, EvalRun.id != self.job_id, EvalRun.status == "queued")
                .values(status="running", started_at=utcnow()).execution_options(synchronize_session=False))
            await session.commit()
        return claimed

    async def _prepare(self) -> list[_Unit]:
        from app.eval import variants as variant_rules
        from app.eval.cases import case_fields
        from app.eval.snapshots import take_live

        async with SessionLocal() as session:
            leader = await session.get(EvalRun, self.job_id)
            runs = [leader] if not leader.pair_id else list((await session.execute(
                select(EvalRun).where(EvalRun.pair_id == leader.pair_id).order_by(EvalRun.created_at))).scalars())
            suite = await session.get(EvalSuite, leader.suite_id)
            if suite is None:
                raise runtime.JobFailed("题集不存在，可能已被删除")
            self.suite = {"id": suite.id, "name": suite.name, "timezone": suite.timezone}
            self.budget = dict(leader.budget or {})
            self.as_of = leader.as_of or utcnow()
            self.cron = (leader.fingerprint or {}).get("cron")
            self.actor = leader.created_by
            results = list((await session.execute(select(EvalResult).where(
                EvalResult.eval_run_id.in_([r.id for r in runs])).order_by(EvalResult.seq))).scalars())
            case_ids = {r.case_id for r in results if r.case_id}
            for row in (await session.execute(select(EvalCase).where(EvalCase.id.in_(case_ids)))).scalars():
                self.cases[row.id] = case_fields(row)
            # 拍一份当前目录（目录选当前目录、只用已核实的项的都按它读）。已经拍过的（重试）沿用那一份：同一次评测
            # 前后读的是同一份目录
            snapshot = leader.catalog_snapshot
            wants_live = any((r.variant.get("catalog") or {}).get("mode") in ("live", "reviewed") for r in runs)
            sources = await self._sources(session)
            if wants_live and not snapshot:
                from app.eval.snapshots import MAX_SOURCES, SnapshotInvalid

                if len(sources) > MAX_SOURCES:
                    raise runtime.JobFailed(f"这次评测用到的数据源超过 {MAX_SOURCES} 个，拍不了目录快照：请在题目里限定数据源")
                if sources:
                    try:
                        snap = await take_live(session, sources, actor=self.actor)
                    except SnapshotInvalid as e:
                        raise runtime.JobFailed(f"拍目录快照没有成功：{e.message}") from e
                    snapshot = snap.artifact_id
            versions = await source_versions(session, sources)
            for run in runs:
                fp = dict(run.fingerprint or {})
                if "sources_before" not in fp:
                    from app.eval.score import SCORER_VERSION

                    mode = (run.variant.get("catalog") or {}).get("mode") or "live"
                    used = snapshot if mode in ("live", "reviewed") else fp.get("snapshot_artifact")
                    fp.update(prompt=prompt_fingerprint(), scorer_version=SCORER_VERSION,
                              catalog="off" if mode == "off" else f"{mode}:{(used or '')[:16]}",
                              sources_before=versions,
                              schemas={sid: v.get("schema") for sid, v in versions.items()},
                              cases=_cases_key(results, run.id))
                run.fingerprint = fp
                run.catalog_snapshot = snapshot if (run.variant.get("catalog") or {}).get("mode") in (
                    "live", "reviewed") else fp.get("snapshot_artifact")
            await session.commit()
            for run in runs:
                mode = (run.variant.get("catalog") or {}).get("mode") or "live"
                art = snapshot if mode in ("live", "reviewed") else (run.fingerprint or {}).get("snapshot_artifact")
                view = variant_rules.view_of(run.variant, art) if (mode == "off" or art) else None
                arm = _Arm(run.id, dict(run.variant or {}), dict(run.resolved_models or {}), view, run.arm)
                self.arms.append(arm)
            units: dict[tuple[str, int], _Unit] = {}
            for r in results:
                if r.status != "pending":
                    continue
                key = (str(r.case_id), int(r.repeat or 0))
                units.setdefault(key, _Unit(key[0], key[1], {})).results[r.eval_run_id] = r.id
        return list(units.values())

    async def _sources(self, session: AsyncSession) -> list[str]:
        """这次评测用得到的数据源：题目限定的、期望查的；有不限数据源的问数据题时，全部启用的源（助手会在它们里面挑）。"""
        from app.db.models import DataSource

        ids: list[str] = []
        unrestricted = False
        for case in self.cases.values():
            ids += list(case.get("datasource_ids") or [])
            gold = case.get("gold") or {}
            if gold.get("datasource_id"):
                ids.append(gold["datasource_id"])
            if case.get("kind") == "ask" and not case.get("datasource_ids"):
                unrestricted = True
        if unrestricted:
            ids += list((await session.execute(select(DataSource.id).where(DataSource.enabled.is_(True))
                                               .order_by(DataSource.created_at))).scalars())
        return list(dict.fromkeys(ids))

    # ---------------------------------------------------------------- 主循环

    async def _run(self) -> None:
        if not await self._write(self._claim):
            return
        units = await self._prepare()
        parallel = self._parallel()
        gate = asyncio.Semaphore(parallel)
        for unit in units:
            await gate.acquire()
            charge = None
            if self.halt is None:
                self.halt, charge = await self._reserve()
            if self.halt is not None:
                gate.release()
                break
            task = asyncio.create_task(self._guarded(unit, gate, charge))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self._settle()
        await self._finish()

    def _parallel(self) -> int:
        from app.core.config import settings

        arms = max(1, len(self.arms))
        quarter = max(1, int(settings.max_concurrent_runs) // 4 // arms)
        return max(1, min(int(self.budget.get("max_parallel") or 2), quarter))

    async def _reserve(self) -> tuple[str | None, tuple[str, float] | None]:
        """开一题之前在评测那本账上预留估算的花费（几臂一起）。触顶交回 (原因, None)，不再开新题；否则 (None, (记在哪天,
        预留了多少))，评完按实际找补。"""
        from app.engine.spend import eval_spend

        per_item = float(((self.budget.get("estimate") or {}).get("per_item_usd")) or 0.0) * max(1, len(self.arms))
        cap = self.budget.get("per_run_max_usd")
        if cap is not None and self.spent + self.reserved + per_item > float(cap) + 1e-9:
            return LIMIT_RUN.format(limit=float(cap)), None
        daily = self.budget.get("daily_max_usd")
        day = await eval_spend.charge(per_item, limit=float(daily) if daily is not None else None, priced=True)
        if day is None:
            return LIMIT_DAILY.format(limit=float(daily)), None
        self.reserved += per_item
        return None, (day, per_item)

    async def _guarded(self, unit: _Unit, gate: asyncio.Semaphore, charge: tuple[str, float] | None) -> None:
        actual = 0.0
        try:
            actual = await self._unit(unit)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 一题出错不让整次评测停下：这一题照实记失败
            from app.core.errors import describe_exception

            logger.exception("评测 %s：第 %s 题出错", self.job_id, unit.case_id)
            await self._write(self._fail_unit, unit, f"评这道题时出错：{describe_exception(e)}")
        finally:
            gate.release()
            if charge is not None:
                from app.engine.spend import eval_spend

                day, reserved = charge
                self.reserved -= reserved
                try:
                    await eval_spend.settle(day, actual - reserved)
                except Exception:  # noqa: BLE001
                    logger.exception("评测：找补花费出错")

    # ---------------------------------------------------------------- 一题（几臂一起）

    async def _unit(self, unit: _Unit) -> float:
        """评一题（几臂同时）。返回这一题按预留找补的那部分实际花费（助手 + 写作；裁判在判的时候已经记过账）。"""
        from app.eval import gold as gold_rules
        from app.eval.cases import render, variable_values

        case = self.cases.get(unit.case_id)
        if case is None:
            await self._write(self._fail_unit, unit, CASE_GONE)
            return 0.0
        arms = [a for a in self.arms if a.run_id in unit.results]
        await self._write(self._mark, unit, "running", "gold")
        values = variable_values(self.as_of, self.suite["timezone"], self.cron)
        question = render(case.get("question") or "", values)
        timeout = float(self.budget.get("per_case_timeout_s") or 900)
        ctx = gold_rules.Context(as_of=self.as_of, tz=self.suite["timezone"], cron=self.cron, actor=self.actor,
                                 suite_id=self.suite["id"], suite_name=self.suite["name"], eval_run_id=self.job_id,
                                 role="gold", extra={"repeat": unit.repeat}, timeout_s=timeout)

        async def answer(_before: Any) -> list[Any]:
            return await asyncio.gather(*(self._answer(arm, unit, case, question, values, timeout) for arm in arms))

        turns, settled = await gold_rules.bracket(case, ctx, answer)
        actual = 0.0
        for index, arm in enumerate(arms):
            turn = turns[index] if turns is not None else None
            actual += await self._score(arm, unit, case, question, turn, settled)
        return actual

    async def _answer(self, arm: _Arm, unit: _Unit, case: Mapping[str, Any], question: str,
                      values: Mapping[str, str], timeout: float) -> Any:
        from app.eval.answer import Driver, TurnContext, stop_run

        result_id = unit.results[arm.run_id]

        async def on_phase(phase: str) -> None:
            await self._write(self._current, result_id, phase)

        ctx = TurnContext(suite_id=self.suite["id"], suite_name=self.suite["name"], eval_run_id=arm.run_id,
                          case_id=unit.case_id, case_seq=case.get("seq"), actor=self.actor,
                          arm=arm.name, repeat=unit.repeat,
                          assistant=arm.variant.get("assistant") or {}, writer=arm.variant.get("writer") or {},
                          view=arm.view)
        driver = Driver(question, datasource_ids=list(case.get("datasource_ids") or []), ctx=ctx, on_phase=on_phase)
        try:
            if case.get("kind") == "workflow":
                graph, inputs = await self._workflow_graph(case, values)
                await asyncio.wait_for(driver.drive_workflow(graph, inputs), timeout=timeout)
            else:
                await asyncio.wait_for(driver.drive(), timeout=timeout)
        except asyncio.TimeoutError:
            turn = driver.turn
            turn.timed_out = True
            await stop_run(turn.current_run, self.actor)
            turn.current_run = None
            if turn.path is None:
                turn.path = {"build": "build_failed", "run": "run_failed", "review": "ran"}.get(turn.phase or "",
                                                                                                 "build_failed")
            turn.reason = TIMED_OUT.format(s=timeout)
        except _WorkflowRefused as e:
            driver.turn.path, driver.turn.reason = "run_failed", str(e)
        return driver.turn

    async def _workflow_graph(self, case: Mapping[str, Any], values: Mapping[str, str]) -> tuple[dict[str, Any], dict]:
        from app.apps import versions as app_versions
        from app.scheduler import service

        async with SessionLocal() as session:
            row = await app_versions.version_row(session, str(case.get("workflow_id")), int(case.get("workflow_version")))
        if row is None:
            raise _WorkflowRefused(f"工作流 v{case.get('workflow_version')} 不在了")
        return copy.deepcopy(row.graph), service.render_inputs(dict(case.get("input_template") or {}), dict(values))

    async def _score(self, arm: _Arm, unit: _Unit, case: Mapping[str, Any], question: str, turn: Any,
                     settled: Any) -> float:
        """评一臂：读答案、（打开了的话）请裁判判断、评分、比 SQL、写结果。返回要找补的实际花费。"""
        from app.core.artifact_store import put_json
        from app.eval import judging, score
        from app.eval.answer import add_usage, writer_usage

        result_id = unit.results[arm.run_id]
        await self._write(self._current, result_id, "score")
        gold_dict = settled.to_dict()
        if turn is None:
            # 作答前就算不出期望（升版待处置、拒收、失败）：不作答，结论照搬原因
            graded = score.score_case(case, gold_dict, score.Answer(run_id=None), path=None)
            await self._write(self._save, result_id, arm, {
                "gold": gold_dict, "gold_run_ids": list(settled.before.run_ids), "verdict": graded.verdict,
                "scores": graded.scores, "error": graded.reason}, None, unit)
            return 0.0
        answer = await score.read_answer(turn.run_id if turn.path == "ran" else None, reply=turn.reply)
        judge: dict[str, Any] = {}
        judge_cost = 0.0
        if arm.variant.get("judge") and turn.path == "ran" and turn.run_id:
            await self._write(self._current, result_id, "judge")
            daily = self.budget.get("daily_max_usd")
            judge["claims"] = await judging.judge_claims(turn.run_id, daily_max_usd=daily)
            judge_cost += float(judge["claims"].get("cost_usd") or 0)
            cap = self.budget.get("per_run_max_usd")
            left = None if cap is None else float(cap) - self.spent - judge_cost
            judge["text"] = await judging.judge_points(question, answer.text, case.get("expectations") or [],
                                                       daily_max_usd=daily, per_run_left=left)
            judge_cost += float(judge["text"].get("cost_usd") or 0)
        graded = score.score_case(case, gold_dict, answer, path=turn.path, path_reason=turn.reason,
                                  judgement=judge.get("text"))
        match = await score.sql_match(case, gold_dict, answer, cells=score.matched_cells(graded))
        writer = await writer_usage(turn.runs)
        assistant = add_usage(turn.usage.get("assistant"), turn.usage.get("review"))
        usage = {"assistant": assistant, "writer": writer, "review": turn.usage.get("review"),
                 "judge": judge_usage(judge, judge_cost)}
        cost = float(assistant.get("cost_usd") or 0) + float(writer.get("cost_usd") or 0)
        self.spent += cost + judge_cost
        looks = {"model": sum(1 for x in turn.looks if x.get("by") == "model"),
                 "system": sum(1 for x in turn.looks if x.get("by") == "system")}
        from app.look.base import LOOK_LIMIT

        looks["limit_hit"] = looks["model"] >= LOOK_LIMIT
        shapes = _sql_shapes(answer)
        details = {"format": DETAILS_FORMAT, "eval_run_id": arm.run_id, "result_id": result_id,
                   "case_id": unit.case_id, "repeat": unit.repeat, "turn": turn.to_dict(), "answer": answer.to_dict(),
                   "judge": judge, "sql_shapes": shapes, "gold_key": gold_key(gold_dict),
                   "scored": graded.to_dict()}
        artifact = await put_json(details, kind=DETAILS_KIND, meta={"eval_run_id": arm.run_id, "result_id": result_id})
        columns = {"run_id": turn.run_id, "gold": gold_dict,
                   "gold_run_ids": list(settled.before.run_ids) + list((settled.after.run_ids if settled.after else [])),
                   "path": turn.path, "verdict": graded.verdict, "scores": graded.scores, "review": turn.review,
                   "tier": answer.tier, "judge": judge or None, "looks": looks,
                   "calls": int(assistant.get("calls") or 0), "timings": dict(turn.timings), "usage": usage,
                   "sql_match": match, "details_artifact": artifact,
                   "error": graded.reason if graded.verdict != "pass" else None}
        await self._write(self._save, result_id, arm, columns, turn, unit)
        self._watch(turn)
        return cost

    def _watch(self, turn: Any) -> None:
        """止损：连续几题助手调用失败、最近几题超时太多，就不再开新题。"""
        if turn.copilot_failed:
            self.copilot_streak += 1
            self.copilot_reason = turn.reason or ""
        else:
            self.copilot_streak = 0
        self.timeouts = [*self.timeouts, bool(turn.timed_out)][-STOP_WINDOW:]
        if self.halt is None and self.copilot_streak >= STOP_COPILOT:
            self.halt = STOP_COPILOT_TEXT.format(n=STOP_COPILOT, reason=self.copilot_reason)
        elif self.halt is None and sum(self.timeouts) >= STOP_TIMEOUTS:
            self.halt = STOP_TIMEOUT_TEXT.format(window=STOP_WINDOW, n=STOP_TIMEOUTS)

    # ---------------------------------------------------------------- 库操作（经 SerialWriter）

    async def _mark(self, session: AsyncSession, unit: _Unit, status: str, phase: str) -> None:
        from app.eval.answer import PHASE_TEXT

        await session.execute(update(EvalResult).where(EvalResult.id.in_(list(unit.results.values())))
                              .values(status=status, started_at=utcnow(), current=PHASE_TEXT.get(phase, phase))
                              .execution_options(synchronize_session=False))
        await session.commit()

    async def _current(self, session: AsyncSession, result_id: str, phase: str) -> None:
        from app.eval.answer import PHASE_TEXT

        await session.execute(update(EvalResult).where(EvalResult.id == result_id, EvalResult.status == "running")
                              .values(current=PHASE_TEXT.get(phase, phase)).execution_options(synchronize_session=False))
        await session.commit()

    async def _save(self, session: AsyncSession, result_id: str, arm: _Arm, columns: dict[str, Any], turn: Any,
                    unit: _Unit) -> None:
        row = await session.get(EvalResult, result_id)
        if row is None:
            return
        for key, value in columns.items():
            setattr(row, key, value)
        row.status, row.current, row.finished_at = "ok", None, utcnow()
        await session.flush()
        await _recount(session, arm.run_id)
        await session.commit()

    async def _fail_unit(self, session: AsyncSession, unit: _Unit, reason: str) -> None:
        await session.execute(update(EvalResult).where(EvalResult.id.in_(list(unit.results.values())),
                                                       EvalResult.status.in_(runtime.OPEN))
                              .values(status="failed", current=None, error=reason, finished_at=utcnow())
                              .execution_options(synchronize_session=False))
        for run_id in unit.results:
            await _recount(session, run_id)
        await session.commit()

    # ---------------------------------------------------------------- 收尾

    async def _finish(self) -> None:
        reason = self.halt
        skipped = reason or NOT_STARTED

        async def close(session: AsyncSession) -> None:
            ids = [a.run_id for a in self.arms]
            await session.execute(update(EvalResult).where(EvalResult.eval_run_id.in_(ids),
                                                           EvalResult.status.in_(runtime.OPEN))
                                  .values(status="skipped", current=None, error=skipped, finished_at=utcnow())
                                  .execution_options(synchronize_session=False))
            await session.commit()

        await self._write(close)
        await self._conclude(None, reason)

    async def _conclude(self, forced: str | None, reason: str | None, error: str | None = None) -> None:
        """每一臂：算总表、定状态、挑基准、比一次；收尾之后推通知、在后台清理。forced 是停止、出错时的状态。"""
        notices: list[tuple[str, str, dict[str, Any]]] = []

        async def write(session: AsyncSession) -> None:
            sources_after = None
            for arm in self.arms:
                run = await session.get(EvalRun, arm.run_id)
                if run is None:
                    continue
                rows = await result_rows(session, run.id)
                ok = [r for r in rows if r["status"] == "ok"]
                status = forced or ("succeeded" if len(ok) == len(rows) else "partial" if ok else "failed")
                fp = dict(run.fingerprint or {})
                if sources_after is None and fp.get("sources_before"):
                    sources_after = await source_versions(session, list(fp["sources_before"]))
                if sources_after is not None and fp.get("sources_before"):
                    fp["sources_after"] = sources_after
                    fp["sources_changed"] = [v.get("name") or sid for sid, v in sources_after.items()
                                             if fp["sources_before"].get(sid) != v]
                run.fingerprint = fp
                summary = await refresh_summary(session, run, rows)
                run.status, run.finished_at = status, utcnow()
                run.stop_reason = reason
                if error:
                    run.error = error
                if run.trigger == "schedule":
                    regressed = ((summary.get("baseline") or {}).get("counts") or {}).get("regressed", 0)
                    counts = {"passed": summary["verdicts"]["pass"], "graded": summary["accuracy"]["n"],
                              "regressed": int(regressed or 0)}
                    if regressed:
                        notices.append(("eval.regressed", run.id, counts))
                    if status in ("failed", "partial"):
                        notices.append(("eval.failed", run.id, counts))
            await session.commit()

        await self._write(write)
        if forced in (None, "failed"):
            from app.notify import service as notify

            for event, run_id, counts in notices:
                notify.spawn(notify.notify_eval(run_id, event, **counts))
        from app.eval import cleanup

        cleanup.spawn(self.suite.get("id") or "")

    async def _close(self, status: str, error: str) -> None:
        """停止、服务关停、整体出错：没结束的项记下原因，每一臂收尾（总表照算已经评完的那些）。"""
        await self._settle()

        async def write(session: AsyncSession) -> None:
            # 还没准备好就停下、出错的（arms 还是空的）：按 pair_id 找全几臂
            ids = [a.run_id for a in self.arms] or list((await session.execute(select(EvalRun.id).where(
                (EvalRun.id == self.job_id) | (EvalRun.pair_id == self.job_id)))).scalars())
            item_status = "cancelled" if status in ("cancelled", "interrupted") else "failed"
            await session.execute(update(EvalResult).where(EvalResult.eval_run_id.in_(ids),
                                                           EvalResult.status.in_(runtime.OPEN))
                                  .values(status=item_status, current=None, error=error, finished_at=utcnow())
                                  .execution_options(synchronize_session=False))
            await session.commit()

        # 停止、中断是「停下」不是「出错」：原因记 stop_reason（error 留空）；整体出错记 error
        stopped = status in ("cancelled", "interrupted")
        try:
            await self._write(write)
            if not self.arms:
                async def bare(session: AsyncSession) -> None:
                    ids = list((await session.execute(select(EvalRun.id).where(
                        (EvalRun.id == self.job_id) | (EvalRun.pair_id == self.job_id),
                        EvalRun.status.in_(runtime.ACTIVE)))).scalars())
                    await session.execute(update(EvalRun).where(EvalRun.id.in_(ids))
                                          .values(status=status, error=None if stopped else error,
                                                  stop_reason=error if stopped else None, finished_at=utcnow())
                                          .execution_options(synchronize_session=False))
                    for run_id in ids:
                        await _recount(session, run_id)
                    await session.commit()

                await self._write(bare)
                return
            await self._conclude(status, error if stopped else None, None if stopped else error)
        except Exception:  # noqa: BLE001
            logger.exception("评测 %s 收尾时写库出错", self.job_id)

    async def _stopped(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        for arm_run in await self._running_runs():
            from app.eval.answer import stop_run

            await stop_run(arm_run, self.actor)
        if self.stop_reason == "shutdown":
            await self._close("interrupted", INTERRUPTED)
        else:
            await self._close("cancelled", STOPPED)

    async def _running_runs(self) -> list[str]:
        """这次评测发起的、还没结束的运行（停止时一并取消：作答、算期望的都算）。"""
        from app.db.models import Run

        ids = [a.run_id for a in self.arms] or [self.job_id]
        async with SessionLocal() as session:
            rows = (await session.execute(select(Run.id, Run.origin).where(
                Run.origin_kind == "eval", Run.status.in_(("queued", "running", "interrupted"))))).all()
        return [rid for rid, origin in rows if (origin or {}).get("eval_run_id") in ids]

    async def _failed(self, reason: str) -> None:
        await self._close("failed", reason)

    async def _crashed(self, e: Exception) -> None:
        from app.core.errors import describe_exception

        logger.exception("评测 %s 出错", self.job_id)
        await self._close("failed", f"评测出错：{describe_exception(e)}")


class _WorkflowRefused(Exception):
    pass


def _cases_key(results: Sequence[EvalResult], run_id: str) -> str:
    """这一臂评的题目：题目 id 和指纹（改过一道题指纹就变）。"""
    from app.core.artifact_store import canonical_json, content_hash

    pairs = sorted({(str(r.case_id), r.case_hash) for r in results if r.eval_run_id == run_id})
    return content_hash(canonical_json(pairs))[:16]


def gold_key(gold: Mapping[str, Any] | None) -> str | None:
    """期望值的指纹（作答前那一份）：和上一次比时认「数据变了」。"""
    from app.core.artifact_store import canonical_json, content_hash

    before = (gold or {}).get("before") or {}
    if (gold or {}).get("status") not in ("ok", "drifted"):
        return None
    return content_hash(canonical_json([[s.get("version"), s.get("values")] for s in before.get("sets") or []]))[:16]


def _sql_shapes(answer: Any) -> list[dict[str, Any]]:
    """答案执行的查询的规范化（核查类的不算）：和上一次比时认「做法变」「只参数变」。"""
    from app.data.sql_semantics import normalize_sql

    out = []
    for q in answer.queries:
        if q.get("role") == "verify" or not q.get("sql"):
            continue
        out.append(normalize_sql(str(q["sql"])).as_dict())
    return out


#: 逐项结束了、却没有跑出结果的状态：cancelled 停止、中断时还没评完的，skipped 到了金额上限、止损之后没开评的
NOT_RUN_STATUSES = ("cancelled", "skipped")


def _counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """一臂的计数（EvalRun.counts）。几样各管各的，加起来不重：

    - done：评完了的（ok、partial）和评的时候出错的（failed）——真的跑过的；running、pending 是还在评、还没轮到的；
    - not_run：停止、中断时还没评完，到了上限、止损之后没开评的（NOT_RUN_STATUSES）。以前这些算进 done 和 errors，停下的
      评测就成了 done = total、一堆「出错」，读起来像全评完了、出了很多错；
    - errors：出错的（failed：题目没了、评的时候出了意外），不含没跑的；
    - passed / failed / ungradable / skipped：评完了的按结论分（skipped 是评了、但期望算不出、不评分，不含没开评的）。

    done + not_run + running + pending = total。"""
    out = {"total": len(rows), "done": 0, "running": 0, "pending": 0, "not_run": 0, "passed": 0, "failed": 0,
           "ungradable": 0, "skipped": 0, "errors": 0}
    for r in rows:
        status = r.get("status")
        if status in NOT_RUN_STATUSES:
            out["not_run"] += 1
        elif status in runtime.TASK_DONE:
            out["done"] += 1
        elif status in ("running", "pending"):
            out[status] += 1
        if status == "failed":
            out["errors"] += 1
        verdict = r.get("verdict")
        if verdict == "pass":
            out["passed"] += 1
        elif verdict == "fail":
            out["failed"] += 1
        elif verdict == "ungradable":
            out["ungradable"] += 1
        elif verdict == "skipped":
            out["skipped"] += 1
    return out


def _closing(run_id: str, reason: str) -> runtime.Finish:
    """runtime.close_job / cancel_queued 的 finish：同一个事务里记停下的原因（停止、中断），按改好的逐项状态重算计数。"""
    async def finish(session: AsyncSession) -> None:
        await session.execute(update(EvalRun).where(EvalRun.id == run_id).values(stop_reason=reason)
                              .execution_options(synchronize_session=False))
        await _recount(session, run_id)

    return finish


async def _recount(session: AsyncSession, run_id: str) -> None:
    rows = (await session.execute(select(EvalResult.status, EvalResult.verdict).where(
        EvalResult.eval_run_id == run_id))).all()
    run = await session.get(EvalRun, run_id)
    if run is not None:
        run.counts = _counts([{"status": s, "verdict": v} for s, v in rows])


def result_dict(row: EvalResult) -> dict[str, Any]:
    """逐题结果的列（统计、对比的输入；接口也按它给）。traced 从各期望的结论算。"""
    scores = list(row.scores or [])
    counted = [s for s in scores if s.get("required") and s.get("kind") != "text"]
    return {"id": row.id, "eval_run_id": row.eval_run_id, "seq": row.seq, "case_id": row.case_id,
            "case_seq": row.case_seq, "case_hash": row.case_hash, "repeat": row.repeat, "arm": row.arm,
            "status": row.status, "run_id": row.run_id, "gold_run_ids": list(row.gold_run_ids or []),
            "gold": row.gold, "path": row.path, "verdict": row.verdict, "scores": scores,
            "traced": row.verdict == "pass" and bool(counted) and all(s.get("outcome") == "traced" for s in counted),
            "review": row.review, "tier": row.tier, "judge": row.judge, "looks": row.looks, "calls": row.calls,
            "timings": dict(row.timings or {}), "usage": dict(row.usage or {}), "sql_match": row.sql_match,
            "details_artifact": row.details_artifact, "current": row.current, "error": row.error,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


async def result_rows(session: AsyncSession, run_id: str) -> list[dict[str, Any]]:
    """一次评测的逐题结果，加上明细工件里统计、对比要用的几样（期望值的指纹、答案查询的规范化、报告的引用统计、重来了几次）。
    明细清理掉了的，这几样为空。"""
    from app.core import artifact_store

    rows = list((await session.execute(select(EvalResult).where(EvalResult.eval_run_id == run_id)
                                       .order_by(EvalResult.seq))).scalars())
    out = []
    for row in rows:
        item = result_dict(row)
        details = None
        if row.details_artifact:
            try:
                details = artifact_store.load(row.details_artifact)
            except ValueError:
                details = None
        details = details if isinstance(details, dict) else {}
        item["gold_key"] = details.get("gold_key") or gold_key(row.gold)
        item["sql_shapes"] = details.get("sql_shapes") or []
        item["answer_stats"] = ((details.get("answer") or {}).get("stats")) or {}
        item["attempts"] = int(((details.get("turn") or {}).get("attempts")) or 0)
        out.append(item)
    return out


async def refresh_summary(session: AsyncSession, run: EvalRun, rows: list[dict[str, Any]] | None = None
                          ) -> dict[str, Any]:
    """按逐题结果（重）算一臂的总表、计数、按角色的用量，挑基准、和它比一次（计数、指纹差异记进 summary.baseline）。
    收尾、重新评分都调它。不提交。"""
    from app.eval import compare, stats

    rows = rows if rows is not None else await result_rows(session, run.id)
    summary = stats.summarize(rows, judge_on=bool((run.variant or {}).get("judge")))
    base = await compare.pick_baseline(session, run, explicit=run.baseline_id)
    if base is not None:
        base_rows = await result_rows(session, base.id)
        moved = compare.transitions(base_rows, rows) if base_rows else None
        summary["baseline"] = {"id": base.id, "pinned": bool(base.pinned),
                               "created_at": base.created_at.isoformat() if base.created_at else None,
                               "fingerprint_diff": compare.fingerprint_diff(base.fingerprint or {}, run.fingerprint or {}),
                               **({"counts": moved["counts"], "causes": moved["causes"]} if moved else {"cleaned": True})}
        run.baseline_id = base.id
    run.summary = summary
    run.usage = {k: summary["usage"].get(k) for k in ("assistant", "writer", "judge")} | {
        "cost_usd": summary["cost_usd"], "unpriced_calls": summary["unpriced_calls"]}
    run.counts = _counts(rows)
    return summary


class EvalManager(runtime.JobManager):
    def __init__(self) -> None:
        super().__init__(EvalExecution, label="评测任务", name="eval-run")


manager = EvalManager()


# ==========================================================================
# 停止、重试、启动整理
# ==========================================================================


def running(run: Any) -> bool:
    return manager.running(leader_of(run))


def leader_of_id(rows: Sequence[EvalRun], job_id: str) -> str:
    """rows 里 id 是 job_id 的那一臂由哪一臂的任务在跑（A/B 几臂共用领头那一臂的后台任务）。"""
    row = next((r for r in rows if r.id == job_id), None)
    return leader_of(row) if row is not None else job_id


async def reconcile_stale(session: AsyncSession, rows: Sequence[EvalRun]) -> None:
    """读接口当场整理：记着排队、进行中，这个进程里却没在跑（服务重启过）的，照实标中断。"""
    # 属主不在了才算：多进程时别的进程正在跑的评测，这里看不到它的任务，却不能标中断（runtime.stale 按租约判断）
    stale = await runtime.stale(list(rows), running=lambda job_id: manager.running(leader_of_id(rows, job_id)))
    for row in stale:
        await runtime.close_job(session, EvalRun, EvalResult, row.id, "interrupted", None, "cancelled",
                                INTERRUPTED, finish=_closing(row.id, INTERRUPTED))
        await session.refresh(row)


async def startup(*, live: Any = None) -> int:
    """服务启动时（多进程时当班进程隔一会儿再来一次）：属主不在了的评测标中断（没结束的项记停止），不自动续跑。"""
    async def close(session: AsyncSession, job_id: str) -> None:
        await runtime.close_job(session, EvalRun, EvalResult, job_id, "interrupted", None, "cancelled",
                                INTERRUPTED, finish=_closing(job_id, INTERRUPTED))

    # 几臂共用领头那一臂的后台任务，各臂的属主是同一个进程：按属主判断就够了（running 只在属主是本进程时看）
    return await runtime.interrupt_on_startup(EvalRun, close, running=manager.running, live=live)


async def stop(run_id: str) -> bool:
    """停止一次评测（A/B 的话几臂一起停）。排队中的直接记 cancelled；进行中的停掉、等它收尾。已经结束的返回 False。"""
    async with SessionLocal() as session:
        row = await session.get(EvalRun, run_id)
        if row is None or row.status not in runtime.ACTIVE:
            return False
        leader = leader_of(row)
    if await manager.stop(leader, "user"):
        return True
    async with SessionLocal() as session:
        arms = list((await session.execute(select(EvalRun.id).where(
            (EvalRun.id == leader) | (EvalRun.pair_id == leader)))).scalars())
        done = False
        for arm in arms:
            done = await runtime.cancel_queued(session, EvalRun, EvalResult, arm,
                                               finish=_closing(arm, STOPPED)) or done
        return done


async def retry(run_id: str) -> list[str]:
    """只把没做完的题接着做（停止、中断、出错、到了上限的那几题）：这几项改回待评、整次评测重新排队。A/B 的话同一题几臂一起
    重做（几臂共用期望，要在同一份数据上一起答）。交回重排的几臂的 id。没有可重试的抛 EvalRefused。"""
    async with SessionLocal() as session:
        row = await session.get(EvalRun, run_id)
        if row is None:
            raise EvalRefused("评测不存在", status=404)
        if row.status in runtime.ACTIVE:
            raise EvalRefused("这次评测还没结束", EVAL_BUSY, 409)
        if row.cleaned_at is not None:
            raise EvalRefused("这次评测的明细已清理，不能重试", NOTHING_TO_RETRY, 409)
        leader = leader_of(row)
        arms = list((await session.execute(select(EvalRun).where(
            (EvalRun.id == leader) | (EvalRun.pair_id == leader)))).scalars())
        suite = await session.get(EvalSuite, row.suite_id)
        if suite is None or suite.archived:
            raise EvalRefused("题集已归档或不存在，不能重试", status=409)
        busy = await active_run(session, row.suite_id)
        if busy is not None and leader_of(busy) != leader:
            raise EvalRefused(f"这个题集另一次评测 #{busy.id[:6]} 还没结束", EVAL_BUSY, 409)
        results = list((await session.execute(select(EvalResult).where(
            EvalResult.eval_run_id.in_([a.id for a in arms])))).scalars())
        # 评完了的（status=ok，含升版待处置、结论是跳过的）不重做；停止、中断、出错、到了上限没开评的重做
        open_units = {(r.case_id, r.repeat) for r in results if r.status != "ok"}
        if not open_units:
            raise EvalRefused("这次评测没有没做完的题", NOTHING_TO_RETRY, 409)
        blank = dict(status="pending", run_id=None, gold_run_ids=[], gold=None, path=None, verdict=None, scores=[],
                     review=None, tier=None, judge=None, looks=None, calls=0, timings={}, usage={}, sql_match=None,
                     details_artifact=None, current=None, error=None, started_at=None, finished_at=None)
        for r in results:
            if (r.case_id, r.repeat) in open_units:
                for key, value in blank.items():
                    setattr(r, key, value)
        for arm in arms:
            arm.status, arm.error, arm.stop_reason, arm.finished_at = "queued", None, None, None
            arm.owner = current_owner()   # 重新排队的这一次由本进程认领（多进程：别把它当成原来那个进程的）
        await session.flush()
        for arm in arms:
            await _recount(session, arm.id)
        await session.commit()
    manager.start(leader)
    return [a.id for a in arms]


async def shutdown() -> None:
    await manager.shutdown()


__all__ = ["EVAL_BUSY", "EVAL_FINISHED", "EVAL_INVALID", "EvalExecution", "EvalRefused", "INTERRUPTED",
           "NOTHING_TO_RETRY", "STOPPED", "active_run", "estimate", "gold_key", "judge_usage", "leader_of", "manager",
           "prompt_fingerprint", "reconcile_stale", "refresh_summary", "result_dict", "result_rows", "retry", "running",
           "shutdown", "source_versions", "start", "startup", "stop"]
