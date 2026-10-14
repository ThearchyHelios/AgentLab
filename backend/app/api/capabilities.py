"""能力登记与端点（BE-1）：/api/capabilities、/api/endpoints。设计文档 §3.9、§3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["capabilities"])
