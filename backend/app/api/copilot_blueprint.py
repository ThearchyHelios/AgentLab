"""蓝图助手和填关节的流式接口（docs/design/normal-user-apps.md §2.2、§2.3、§2.11）。

- POST /api/copilot/blueprint-stream：蓝图对话。模型输出 bp_* 操作（BlueprintApplier 落地）、look、bp_ask（这一轮就此结束）；
- POST /api/copilot/fill-joints-stream：按模板填生成关节（sql、metric_expr）。TemplateApplier 只收 fill_joint、只收模板声明过的
  关节；不同 group 的关节并行填。

都复用 api/copilot_gen.Generation。包 B1 实现。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/copilot", tags=["copilot-blueprint"])


@router.post("/blueprint-stream")
async def blueprint_stream() -> Any:
    raise HTTPException(501, "尚未实现")


@router.post("/fill-joints-stream")
async def fill_joints_stream() -> Any:
    raise HTTPException(501, "尚未实现")
