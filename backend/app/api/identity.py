"""用户、组、审计（BE-1，只给管理员）。设计文档 §3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["identity"])
