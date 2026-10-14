"""应用（BE-3）：增删改查、派生、检查、发布、版本、授权、页面解析、入口、实例、视图刷新、组件目录。设计文档 §3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/apps", tags=["apps"])


@router.get("/catalog")
async def get_catalog() -> dict:
    """受信组件目录 v1。前端按名字找渲染器；助手生成时给模型看同一份。"""
    from app.apps.catalog import catalog_payload

    return catalog_payload()
