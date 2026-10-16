"""身份的实现（docs/design/app-platform-and-look.md §2.7）：登录方式、会话、中间件、OIDC、引导与找回。

api/deps.py 只定接口，这里给实现：
- 登录方式：环境变量 AGENTLAB_AUTH_MODE 优先，没设时读设置（settings 表的 auth 一行，进程内缓存，改设置时刷新）；
- middleware.py：按请求认出是谁（会话 cookie），写进 api/deps 的 contextvar；非 GET 请求核对 X-AgentLab-Request 头和 Origin；
- sessions.py / passwords.py：会话（库里只存 token 的 sha256）、口令（hashlib.scrypt）、登录失败退避；
- oidc.py：授权码 + PKCE；access.py：运行、工件的读权限；audit.py：管理类操作的留痕；signing.py：身份断言；
- chat_policy.py：开了问数据的使用者能运行什么样的图（节点、工具的白名单）。

关登录（off，默认）时：中间件什么都不做，所有依赖放行，行为和加这一层之前一模一样。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import Depends

from app.api.coded import AUTH_REQUIRED, ROLE_REQUIRED, CodedHTTPException

logger = logging.getLogger(__name__)

MODES = ("off", "local", "oidc")
#: settings 表里存登录设置的那一行
SETTING_KEY = "auth"
ROLE_LABEL = {"admin": "管理员", "builder": "搭建者", "user": "使用者"}

#: 设置的进程内缓存。单进程部署：改设置的接口改完就刷新它；启动时、第一个请求到来时读一次
_stored: dict[str, Any] = {}
_loaded = False


def env_mode() -> str | None:
    """环境变量指定的登录方式；没设、或者写的不是三种之一为 None。"""
    raw = (os.environ.get("AGENTLAB_AUTH_MODE") or "").strip().lower()
    if raw and raw not in MODES:
        logger.warning("AGENTLAB_AUTH_MODE=%s 不是 off / local / oidc 之一，按设置页的登录方式处理", raw)
        return None
    return raw or None


def current_mode() -> str:
    """当前的登录方式：环境变量 AGENTLAB_AUTH_MODE 优先；没设时读设置，默认 off。"""
    mode = env_mode() or _stored.get("mode") or "off"
    return mode if mode in MODES else "off"


def stored_settings() -> dict[str, Any]:
    return dict(_stored)


def user_can_chat() -> bool:
    """开登录后 user 角色能不能问数据（Q2，默认不能，管理员开关）。"""
    return bool(_stored.get("user_can_chat"))


async def load_settings(force: bool = False) -> dict[str, Any]:
    """从库里读登录设置进缓存。"""
    global _loaded
    if _loaded and not force:
        return dict(_stored)
    from app.db.base import SessionLocal
    from app.db.models import Setting

    async with SessionLocal() as session:
        row = await session.get(Setting, SETTING_KEY)
    _stored.clear()
    _stored.update((row.value if row else None) or {})
    _loaded = True
    return dict(_stored)


def remember_settings(value: dict[str, Any]) -> None:
    """改设置的接口写库之后调：缓存换成新值。"""
    global _loaded
    _stored.clear()
    _stored.update(value)
    _loaded = True


def refusal(principal: Any, role: str) -> CodedHTTPException | None:
    """principal 够不够 role：够（或关登录）是 None；不够交回该回的错——没登录 401（auth_required），角色不够 403
    （role_required）。路由级依赖、接口内部的 ensure_role、中间件拦 /docs 都用它，同一个判断、同一句话。"""
    if principal.mode == "off":
        return None
    if not principal.username:
        return CodedHTTPException(401, "请先登录", AUTH_REQUIRED)
    if not principal.at_least(role):
        return CodedHTTPException(403, f"没有权限：这项操作需要{ROLE_LABEL.get(role, role)}及以上的角色", ROLE_REQUIRED)
    return None


def require(role: str) -> None:
    """当前请求至少是 role，否则 401（没登录）/ 403（角色不够），都带 code。关登录时放行。"""
    from app.api.deps import current_principal

    if (refused := refusal(current_principal(), role)) is not None:
        raise refused


def role_dependency(role: str) -> Any:
    """至少是 role 的路由级依赖。关登录时放行。"""

    async def _check() -> None:
        require(role)

    return Depends(_check)


async def run_readable(principal: Any, run: Any) -> bool:
    from app.auth.access import run_readable as check

    return await check(principal, run)


async def artifact_readable(principal: Any, artifact_id: str) -> bool:
    from app.auth.access import artifact_readable as check

    return await check(principal, artifact_id)
