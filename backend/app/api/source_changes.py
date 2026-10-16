"""数据源的变化记录（docs/design/next-round/03-数据接入与目录维护.md B3）：结构变化、取值变化，查看与处理。

挂在 /api/datasources/{id}/changes 下，要在 datasources 的通配 /{source_id} 之前登记（main.py）。第 3 波 W3-a 填。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/datasources", tags=["source-changes"])
