"""可验证报告：把一次运行的报告和证据导出成可独立核对的 HTML 包。

目前只登记了路由前缀，接口由可验证报告的工作包补上。路径挂在 /api/runs/{run_id}/… 下面；
注意 runs.py 里已经有 /{run_id}/verify（封存复验），别撞名。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/runs", tags=["bundle"])
