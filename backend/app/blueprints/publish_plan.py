"""一次确认发布：发布单（N5、§3.6）。先算出每一步和白话摘要（只读），确认后按序执行、每步留痕，停下后从那一步接着做。

步骤的顺序让中途失败的状态无害：定时任务先停用、应用发布后才启用；授权放最后。

包 B2 实现。
"""
from __future__ import annotations

from typing import Any

#: 发布单的步骤（顺序即执行顺序）
STEP_KINDS = (
    "save_version",       # 保存版本（按图哈希复用已有的）
    "preflight",          # 全量预检（只读）：工作流发布前检查 + 应用检查 + 定时任务预览 + 名录核对
    "publish_workflow",   # 发布工作流（受管级只有管理员）
    "create_schedule",    # 建定时任务（停用）
    "create_app",         # 建应用、派生、写岗位绑定
    "publish_app",        # 发布应用（钉住项升版时按蓝图里的处置）
    "enable_schedule",    # 启用定时任务
    "grant",              # 授权
    "first_run",          # 跑第一次（看板类默认勾选）
)
#: 问题的三类（N10 的 who）
PROBLEM_WHO = ("you", "assistant", "admin")


async def plan(session: Any, blueprint: Any, *, level: str, principal: Any, options: dict[str, Any] | None = None) -> Any:
    """出发布单（只读预检），交回 PublishPlan（已落库，status=planned）。"""
    raise NotImplementedError


async def run(session: Any, plan_id: str, *, principal: Any) -> Any:
    """按序执行；某步失败时 status=stopped 并写明原因。"""
    raise NotImplementedError


async def resume(session: Any, plan_id: str, *, principal: Any) -> Any:
    """从停下的那一步接着做；已完成的步骤按结果里的 id 核对，不重做。"""
    raise NotImplementedError
