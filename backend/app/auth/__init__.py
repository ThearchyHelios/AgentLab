"""身份的实现（docs/design/app-platform-and-look.md §2.7）：登录方式、会话、中间件、OIDC、引导与找回。

api/deps.py 只定接口，这里给实现。契约骨架阶段只有关登录（off）的行为：全部放行、等同 admin。
开登录（local / oidc）由 BE-1 补齐：会话 cookie、中间件写 contextvar、角色依赖、运行与工件的读权限。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends


def current_mode() -> str:
    """当前的登录方式：环境变量 AGENTLAB_AUTH_MODE 优先；没设时读设置（BE-1 实现），默认 off。"""
    mode = (os.environ.get("AGENTLAB_AUTH_MODE") or "").strip().lower()
    return mode if mode in ("off", "local", "oidc") else "off"


def role_dependency(role: str) -> Any:
    """至少是 role 的路由级依赖。关登录时放行。"""

    async def _check() -> None:
        if current_mode() == "off":
            return None
        raise NotImplementedError("开登录时的角色检查由 BE-1 实现")

    return Depends(_check)


async def run_readable(principal: Any, run: Any) -> bool:
    if current_mode() == "off":
        return True
    raise NotImplementedError("开登录时的运行读权限由 BE-1 实现")


async def artifact_readable(principal: Any, artifact_id: str) -> bool:
    if current_mode() == "off":
        return True
    raise NotImplementedError("开登录时的工件读权限由 BE-1 实现")
