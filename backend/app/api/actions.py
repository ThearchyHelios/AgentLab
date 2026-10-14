"""生效记录（BE-2，只读；核实走待办）。设计文档 §3.8、§3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/actions", tags=["actions"])
