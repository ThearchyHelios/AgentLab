"""评测的清理与工件回收（细案 01 做法 16）。

每次评测有几十次运行（作答、作答前后算期望）和它们的查询快照、报告文档，只增不减，库和工件目录会一直涨。按保留规则清掉更早
那几次的**明细**，总表（EvalRun.summary：准确率、区间、分布、和上一次比的计数）永久保留，界面上写「明细已清理」（cleaned_at）。

**保留哪些**（每个题集）：最近 keep_runs 次（设置 eval.keep_runs，缺省 20）；钉住的基线；以及这些里面 A/B 的另几臂（同一个
pair_id，配对表要两边都在）。还没结束的不动。按时间一刀切会把还在当基线的那次删掉，所以钉住的另算。

**删什么**：逐题结果行、它们的明细工件、底下的运行（作答、算期望的探索运行，来源都是 eval；事件随运行级联删掉）和这些运行
在工件引用表里的引用行。**工件文件**要等引用行删完之后一条引用都不剩才删（core/artifact_store.forget）：同样的内容可能被别的
运行引用（同一份表结构快照、同一份查询结果），不能跟着删。评测运行全是探索运行，删正式运行的那道拦截（api/runs.delete_run）
不受影响；这里也只删来源是 eval、已经结束的运行。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, utcnow
from app.db.models_eval import EvalResult, EvalRun

logger = logging.getLogger(__name__)

#: 删运行时还在这几种状态的不删（运行在跑、停在审批上）
_LIVE = ("queued", "running", "interrupted")
_background: set[asyncio.Task[Any]] = set()


def keep_set(runs: list[EvalRun], keep: int) -> set[str]:
    """要留明细的那几次（见模块说明）。runs 是一个题集的全部评测，新的在前。"""
    from app.core.job_runtime import ACTIVE

    kept = {r.id for r in runs[:max(1, keep)]} | {r.id for r in runs if r.pinned or r.status in ACTIVE}
    pairs = {r.pair_id for r in runs if r.id in kept and r.pair_id}
    kept |= {r.id for r in runs if r.pair_id and r.pair_id in pairs}
    return kept


async def _clean_one(session: AsyncSession, run: EvalRun) -> tuple[int, int, set[str]]:
    """清一次评测的明细：(删了几项结果, 删了几次运行, 要回收的工件 id)。不提交。"""
    from app.db.models import Artifact, Run

    results = list((await session.execute(select(EvalResult).where(EvalResult.eval_run_id == run.id))).scalars())
    run_ids: set[str] = set()
    artifacts: set[str] = set()
    for r in results:
        if r.run_id:
            run_ids.add(r.run_id)
        run_ids |= {x for x in r.gold_run_ids or [] if isinstance(x, str)}
        if r.details_artifact:
            artifacts.add(r.details_artifact)
            await session.execute(delete(Artifact).where(Artifact.id == r.details_artifact, Artifact.run_id == "")
                                  .execution_options(synchronize_session=False))
    # 这次评测发起的、结果里没记下的运行（重来的前一次、停止时没写进结果的）：按来源找
    for rid, origin in (await session.execute(select(Run.id, Run.origin).where(Run.origin_kind == "eval"))).all():
        if (origin or {}).get("eval_run_id") == run.id:
            run_ids.add(rid)
    removed = 0
    for rid in sorted(run_ids):
        row = await session.get(Run, rid)
        if row is None or row.origin_kind != "eval" or row.status in _LIVE:
            continue
        artifacts |= set((await session.execute(select(Artifact.id).where(Artifact.run_id == rid))).scalars())
        await session.execute(delete(Artifact).where(Artifact.run_id == rid).execution_options(synchronize_session=False))
        await session.delete(row)
        removed += 1
    for r in results:
        await session.delete(r)
    run.cleaned_at = utcnow()
    return len(results), removed, artifacts


async def clean_suite(suite_id: str, *, keep: int | None = None) -> dict[str, Any]:
    """按保留规则清一个题集：{runs: 清了几次评测, results, deleted_runs, files: 回收了几个工件文件}。"""
    from app.api.settings import eval_settings
    from app.core.artifact_store import forget

    async with SessionLocal() as session:
        if keep is None:
            keep = int((await eval_settings(session))["keep_runs"])
        runs = list((await session.execute(select(EvalRun).where(EvalRun.suite_id == suite_id)
                                           .order_by(EvalRun.created_at.desc(), EvalRun.id.desc()))).scalars())
        kept = keep_set(runs, keep)
        targets = [r for r in runs if r.id not in kept and r.cleaned_at is None]
        results = removed = 0
        artifacts: set[str] = set()
        for run in targets:
            n, m, found = await _clean_one(session, run)
            results += n
            removed += m
            artifacts |= found
        await session.commit()
    files = await forget(artifacts) if artifacts else []
    if targets:
        logger.info("评测清理：题集 %s 清了 %d 次评测的明细（%d 项结果、%d 次运行、%d 个工件文件）", suite_id, len(targets),
                    results, removed, len(files))
    return {"runs": len(targets), "results": results, "deleted_runs": removed, "files": len(files)}


def spawn(suite_id: str) -> None:
    """在后台清理（评测收尾时调，不挡收尾）。出错只记日志。"""
    if not suite_id:
        return

    async def go() -> None:
        try:
            await clean_suite(suite_id)
        except Exception:  # noqa: BLE001
            logger.exception("评测清理出错（题集 %s）", suite_id)

    task = asyncio.get_running_loop().create_task(go())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def drain() -> None:
    """等后台的清理做完（测试、关停用）。"""
    from app.core.background import drain as drain_tasks

    # 不写成 while _background: await gather(...)：别的事件循环上做完、还没摘掉的任务会让那样的循环空转（core/background）
    await drain_tasks(_background)


__all__ = ["clean_suite", "drain", "keep_set", "spawn"]
