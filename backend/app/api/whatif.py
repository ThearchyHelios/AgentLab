"""数字推演：在一次运行的基础上改动输入，推演指标会怎样变化，不改原运行。

目前只登记了路由前缀，接口由数字推演的工作包补上。路径挂在 /api/runs/{run_id}/… 下面。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/runs", tags=["whatif"])
