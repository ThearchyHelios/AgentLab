"""审批质询：审批人在批准前就待审的报告提出质询。

目前只登记了路由前缀，接口由审批质询的工作包补上。路径挂在 /api/runs/{run_id}/… 下面。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/runs", tags=["inquiry"])
