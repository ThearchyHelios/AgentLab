"""定时任务：按周期自动发起工作流运行。

目前只登记了路由前缀，接口由定时任务的工作包补上；后台的调度循环在 app/scheduler 里。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/schedules", tags=["schedules"])
