"""评测接口（docs/design/next-round/01-效果度量与回归评测.md）：题集、题目、期望答案试算、评测、结果、对比。

整组只给搭建者及以上（main.py 挂 require_role("builder")）。第 2 波 W2-c 起填。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/evals", tags=["evals"])
