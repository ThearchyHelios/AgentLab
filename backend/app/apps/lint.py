"""应用的发布检查（§3.13 的 app.* 编号）。问题格式同 engine/schema.py 的 ValidationIssue：
{message, level: error|warning, code, path}。path 指向 spec 里的位置（如 pages[0].body[2].bind）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def lint_app(session: AsyncSession, spec: dict[str, Any], bindings: dict[str, Any], *,
                   previous: Any | None = None, dispositions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """检查一份待发布的应用。previous 是上一个应用版本（判断钉住项升版要不要处置）。"""
    raise NotImplementedError("BE-3")
