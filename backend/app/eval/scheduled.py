"""评测题集的定时（细案 01 做法 15；总方案 X4：定时只有一套）。

scheduler/service.py 按目标种类导入本模块（_TARGET_MODULES）：认领、补跑（错过的按任务的宽限补一次）、重叠跳过、触发记录都是
定时那一套，这里只管两件事：

- fire：到点（或立即运行）发起一次评测。变体用题集的缺省变体，定时跑 1 次（评测-D10：对照才跑 3 次），截至时间就是这次
  计划触发的时间（题目里的 {{ schedule.* }} 按它、按这条定时的 cron 渲染，和工作流的定时同一份变量）；基准是同一变体的
  上一次（runner 收尾时按 compare.pick_baseline 挑）。这个题集上一次评测还没结束就记 skipped_overlap、写清是哪一次；题集
  不在、归档了、没有启用的题、变体里的模型用不了，记 failed_to_start 和原因。发起了记 started，ScheduleFire.eval_run_id 指向
  这一次评测（用 service.record_fire 写，和工作流同一套去重：同一次触发撞上唯一约束就交回已有的那一条）。
- check_target：建任务、改目标时核对题集在、没归档、有启用的题。

有退步、没评完的通知在评测收尾时推（runner.py → notify/service.notify_eval），只推定时发起的。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


async def fire(*, schedule_id: str, fire_at: datetime, kind: str, actor: str | None, target_id: str, name: str,
               tz: str, cron_expr: str) -> Any:
    from app.db.base import SessionLocal
    from app.db.models_eval import EvalSuite
    from app.eval import runner, variants
    from app.eval.cases import CaseInvalid
    from app.scheduler import service

    async with SessionLocal() as session:
        suite = await session.get(EvalSuite, target_id)
        if suite is None:
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", "评测题集不存在，可能已被删除")
        busy = await runner.active_run(session, suite.id)
        if busy is not None:
            reason = f"题集「{suite.name}」上一次评测 #{busy.id[:6]} 还没结束，本次触发已跳过"
            return await service.record_fire(schedule_id, fire_at, kind, "skipped_overlap", reason)
        existing = await service._existing(schedule_id, fire_at, kind)
        if existing is not None:
            return existing
        try:
            from app.api.settings import eval_settings

            judge = bool((await eval_settings(session))["judge"])
            variant = variants.normalize_variant(dict(suite.default_variant or {}) | {"repeats": variants.SCHEDULE_REPEATS},
                                                 judge_default=judge)
            [run] = await runner.start(session, suite, [variant], actor=actor or service.actor_label(name),
                                       trigger="schedule", schedule_id=schedule_id, as_of=fire_at, cron=cron_expr)
        except CaseInvalid as e:
            return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", f"没有发起评测：{e.message}")
        return await service.record_fire(schedule_id, fire_at, kind, "started", None, eval_run_id=run.id)


async def check_target(session: Any, target_id: str) -> str | None:
    """题集在、没归档、有启用的题。不合规返回给人看的一句。"""
    from sqlalchemy import func, select

    from app.db.models_eval import EvalCase, EvalSuite

    suite = await session.get(EvalSuite, target_id)
    if suite is None:
        return "评测题集不存在，可能已被删除"
    if suite.archived:
        return f"题集「{suite.name}」已归档，不能定时评测"
    enabled = await session.scalar(select(func.count()).select_from(EvalCase).where(
        EvalCase.suite_id == target_id, EvalCase.enabled.is_(True), EvalCase.archived.is_(False)))
    if not enabled:
        return f"题集「{suite.name}」里没有启用的题目"
    return None


async def target_name(session: Any, target_id: str) -> str | None:
    """接口里给人看的目标名（题集名）；不在了为 None。"""
    from app.db.models_eval import EvalSuite

    suite = await session.get(EvalSuite, target_id)
    return suite.name if suite is not None else None
