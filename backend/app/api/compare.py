"""运行对比：两次运行按「变化三分法」对照。

目前只登记了路由前缀，接口由运行对比的工作包补上。路径挂在 /api/runs/{run_id}/… 下面；
这个路由登记在 runs.router 之前，以后加 /api/runs/compare 这类固定路径也不会被 /{run_id} 吃掉。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/runs", tags=["compare"])
