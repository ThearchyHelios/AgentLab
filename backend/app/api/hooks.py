"""入站钩子（BE-2）：POST /api/hooks/{hook_id}，只认签名、不走会话。设计文档 §2.4。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/hooks", tags=["hooks"])
