"""「已发布且未退役」的工作流版本：发起正式运行、口径升版判定、子图钉版本、应用钉版本都用这一个说法（P5，A1）。

以前三处各有各的判断：发起只认当前发布版（launch.py:92，F1），口径升版取 max(version) 把草稿也算进去（F3）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def is_published(version: Any, *, current: int | None = None) -> bool:
    """这一版能不能被使用：level 不为空且没退役。level 为空的老数据，只认它就是 current（当前发布版）的情形。"""
    raise NotImplementedError("BE-3")


async def published_versions(session: AsyncSession, workflow_id: str) -> list[Any]:
    """这个工作流全部「已发布且未退役」的版本（WorkflowVersion），按版本号升序。"""
    raise NotImplementedError("BE-3")


async def latest_published(session: AsyncSession, workflow_id: str) -> Any | None:
    """最新的「已发布且未退役」的版本；没有就是 None。口径升版的判定用它（替换 governance.py 的 func.max）。"""
    raise NotImplementedError("BE-3")


async def retire_version(session: AsyncSession, workflow_id: str, version: int, *, actor: str | None,
                         note: str | None) -> dict[str, Any]:
    """退役一个版本。当前发布版不能退（409）。返回 {retired, affected_apps: [...]}；写审计。"""
    raise NotImplementedError("BE-3")
