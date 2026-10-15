"""蓝图、试运行、发布单的接口（docs/design/normal-user-apps.md §3.5、§3.6、§3.10）。

权限在接口里判：共享蓝图要 builder（受管级发布单再要 admin），个人蓝图要拥有者且管理员开了 apps.user_personal_apps。
骨架阶段全部 501，包 B2 实现（CRUD 调 blueprints/ops，试运行调 blueprints/trial，发布单调 blueprints/publish_plan）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api", tags=["blueprints"])

_TODO = "尚未实现"


@router.post("/blueprints", status_code=201)
async def create_blueprint() -> dict[str, Any]:
    """起点：{source: {kind: conversation|template|run|workflow_version, id?, version?}, scope: shared|personal, name?}"""
    raise HTTPException(501, _TODO)


@router.get("/blueprints")
async def list_blueprints() -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.get("/blueprints/{blueprint_id}")
async def get_blueprint(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.patch("/blueprints/{blueprint_id}")
async def patch_blueprint(blueprint_id: str) -> dict[str, Any]:
    """{ops: [bp_* 操作], name?}：用户在卡片上的修改，来源记成 user。"""
    raise HTTPException(501, _TODO)


@router.post("/blueprints/{blueprint_id}/instantiate")
async def instantiate_blueprint(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/blueprints/{blueprint_id}/trial")
async def trial(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.get("/blueprints/{blueprint_id}/preview")
async def preview(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/blueprints/{blueprint_id}/publish-plan")
async def publish_plan(blueprint_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.get("/publish-plans/{plan_id}")
async def get_plan(plan_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/publish-plans/{plan_id}/run")
async def run_plan(plan_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)


@router.post("/publish-plans/{plan_id}/resume")
async def resume_plan(plan_id: str) -> dict[str, Any]:
    raise HTTPException(501, _TODO)
