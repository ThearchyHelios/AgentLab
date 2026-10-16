"""评测题集的定时（docs/design/next-round/01-效果度量与回归评测.md 做法 15）。

scheduler/service.py 按目标种类导入本模块（_TARGET_MODULES）：fire 到点发起一次，check_target 建任务时核对目标。
第 3 波 W3-c 填；填之前照实记「当前版本还不能按时发起这类目标」，不发起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


async def fire(*, schedule_id: str, fire_at: datetime, kind: str, actor: str | None, target_id: str, name: str,
               tz: str, cron_expr: str) -> Any:
    from app.scheduler import service

    reason = service.TARGET_UNSUPPORTED.format(kind=service.TARGET_LABEL["eval_suite"])
    return await service.record_fire(schedule_id, fire_at, kind, "failed_to_start", reason)


async def check_target(session: Any, target_id: str) -> str | None:
    return "当前版本还不能按时发起「评测题集」这类目标"
