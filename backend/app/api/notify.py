"""通知渠道与投递记录（BE-2）。设计文档 §2.4、§3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/notify", tags=["notify"])
