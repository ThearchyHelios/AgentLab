"""账本（BE-3，只读）：工作流版本被哪些应用钉住、生效计数；生效记录查询。设计文档 §2.9 L4。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/ledger", tags=["ledger"])
