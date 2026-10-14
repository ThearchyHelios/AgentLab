"""请求里的「人」：身份与权限的唯一入口（docs/design/app-platform-and-look.md §2.7、附录 C）。

登录方式三种：off（默认，今天的行为）、local、oidc。各接口不直接碰请求头或会话，只问这里：
- actor_of(header)：记在各「人」列上的名字。关登录时照旧解码 X-Actor（自报署名）；开了登录返回认证过的
  用户名，请求头一律不看——这样各接口的签名一个都不用改；
- current_principal()：完整身份（用户名、角色、组、是否认证过、登录方式），中间件按请求写进 contextvar；
- require_role(role)：挂在 main.py 的 include_router(..., dependencies=[...]) 上的路由级依赖；
- can_read_run / can_read_artifact：运行、工件的读权限（user 角色只能读有授权的应用视图运行、自己参与的实例）。

关登录时：所有人等同 admin、全部放行，行为和加这一层之前一模一样（界面上挂横幅说明）。

开登录（local / oidc）的实现由 app/auth/ 提供：会话、中间件、OIDC、引导第一个管理员。这个模块只定接口。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

#: 登录方式
AUTH_MODES = ("off", "local", "oidc")
#: 角色由高到低
ROLE_RANK = {"user": 1, "builder": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    """这次请求是谁。

    username：开登录时是认证过的用户名（不可改）；关登录时是自报的署名，可能为空。
    verified：是不是认证过的身份。关登录时恒为 False——审计、派给人的记录、run.origin 照实记。
    """

    username: str | None
    role: str = "admin"
    display_name: str | None = None
    user_id: str | None = None
    groups: tuple[str, ...] = field(default_factory=tuple)
    verified: bool = False
    mode: str = "off"

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK.get(role, 99)


#: 中间件按请求写入（app/auth/）。没写（关登录、测试直接调函数）时为 None
_principal: ContextVar[Principal | None] = ContextVar("agentlab_principal", default=None)


def set_principal(principal: Principal | None) -> Any:
    """中间件用：写入这次请求的身份，返回 token 供 reset。"""
    return _principal.set(principal)


def reset_principal(token: Any) -> None:
    _principal.reset(token)


def auth_mode() -> str:
    """当前的登录方式。环境变量 AGENTLAB_AUTH_MODE 优先于设置页（app/auth/ 实现读取与缓存）。"""
    from app.auth import current_mode

    return current_mode()


def decode_actor(header: str | None) -> str | None:
    """请求头 X-Actor 里的署名（设置页填的那个），没填就是 None。

    浏览器的请求头只能是 Latin-1：中文署名原样放进去，fetch 直接抛错、整个请求
    发不出去。所以前端按 encodeURIComponent 编码后发，这里解回来；老客户端发的
    纯 ASCII 署名解码前后一样。
    """
    value = unquote((header or "").strip()).strip()
    return value[:100] or None


def actor_of(header: str | None) -> str | None:
    """记在各「人」列上的名字。开了登录：认证过的用户名，请求头不看；关登录：解码 X-Actor。"""
    principal = _principal.get()
    if principal is not None and principal.mode != "off":
        return principal.username
    return decode_actor(header)


def current_principal() -> Principal:
    """这次请求的完整身份。关登录（或中间件没写）时：等同 admin、未认证。"""
    principal = _principal.get()
    if principal is not None:
        return principal
    return Principal(username=None, role="admin", verified=False, mode="off")


def actor_verified() -> bool:
    return current_principal().verified


def require_role(role: str) -> Any:
    """路由级依赖：至少是 role。关登录时放行。开登录时：没登录 401，角色不够 403（app/auth/ 实现）。"""
    from app.auth import role_dependency

    return role_dependency(role)


async def can_read_run(principal: Principal, run: Any) -> bool:
    """能不能读这次运行（证据、事件、工件）。关登录或 builder 以上：能。user：有授权的应用视图运行、自己参与的实例。"""
    from app.auth import run_readable

    return await run_readable(principal, run)


async def can_read_artifact(principal: Principal, artifact_id: str) -> bool:
    """能不能读这件工件：它属于的某次运行能读就能读。"""
    from app.auth import artifact_readable

    return await artifact_readable(principal, artifact_id)
