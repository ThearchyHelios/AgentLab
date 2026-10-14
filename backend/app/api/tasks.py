"""待办（BE-2）：列表、计数、详情、回复、改派、催办。设计文档 §3.7、§3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
