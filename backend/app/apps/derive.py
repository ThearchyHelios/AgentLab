"""从工作流版本确定性派生应用（A8）：不靠模型，助手失败了也有可用的应用（P3）。

入口字段 → 入口表单；人工节点 → 待办（岗位留空等人绑定）；口径卡 → 指标块；报告 → 报告块；取数节点 → 表格；
写入的调用工具节点 → 动作确认待办 + 动作状态。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def derive_app(session: AsyncSession, workflow_id: str, version: int, *, name: str | None = None) -> dict[str, Any]:
    """返回一份 agentlab.app/1 spec（未发布的草稿）。"""
    raise NotImplementedError("BE-3")
