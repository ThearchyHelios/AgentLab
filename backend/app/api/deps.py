"""请求里的「人」：身份与权限的唯一入口（docs/design/app-platform-and-look.md §2.7、附录 C）。

登录方式三种：off（默认，今天的行为）、local、oidc。各接口不直接碰请求头或会话，只问这里：
- actor_of(header)：记在各「人」列上的名字。关登录时照旧解码 X-Actor（自报署名）；开了登录返回认证过的
  用户名，请求头一律不看——这样各接口的签名一个都不用改；
- current_principal()：完整身份（用户名、角色、组、是否认证过、登录方式），中间件按请求写进 contextvar；
- require_role(role)：挂在 main.py 的 include_router(..., dependencies=[...]) 上的路由级依赖；
- can_read_run / can_read_artifact：运行、工件的读权限（user 角色只能读有授权的应用视图运行、自己参与的实例）；
- ensure_role / ensure_can_publish：接口内部按请求内容判定的权限（MCP 的 stdio、受管级发布、版本退役，Q8）；
- restricted()：这次请求是不是开登录后的 user 角色（按请求内容收窄的接口用：问数据只能发起探索运行、工件按白名单给）；
- run_scope / evidence_scope / chat_scope：main.py 挂在运行、证据、问数据几组路由上的依赖，按 run_id 判断能不能读。

关登录时：所有人等同 admin、全部放行，行为和加这一层之前一模一样（界面上挂横幅说明）。

开登录（local / oidc）的实现由 app/auth/ 提供：会话、中间件、OIDC、引导第一个管理员。这个模块只定接口。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

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
    """这次请求的完整身份。关登录时：等同 admin、未认证，username 是中间件按 X-Actor 解出的自报署名；中间件没写
    （不在请求里，比如测试直接调函数、启动时的整理、调度循环）时同样等同 admin、未认证，username 为空。"""
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


def ensure_role(role: str) -> None:
    """接口内部用：至少是 role，否则 401（没登录）/ 403（角色不够）。关登录时放行。
    用在要看请求内容才知道要什么角色的地方（比如 MCP 的 stdio 只有管理员能建）。"""
    from app.auth import require

    require(role)


def ensure_can_publish(level: str | None) -> None:
    """发布工作流（Q8）：受管级只给管理员，已发布级给搭建者及以上。版本退役直接 ensure_role("admin")。
    BE-3 在 api/workflows.py 的发布、退役接口开头调。"""
    ensure_role("admin" if level == "governed" else "builder")


def user_can_chat() -> bool:
    """开登录后 user 角色能不能用问数据（Q2，管理员在「设置 → 身份」里开）。"""
    from app.auth import user_can_chat as allowed

    return allowed()


def _forbidden() -> HTTPException:
    return HTTPException(403, "没有权限查看这次运行")


#: user 角色在自己的问数据运行上能读的（GET /api/runs/{id} 之后的部分）：运行记录、事件、运行时的图。这几样就是问数据页
#: 要的；状态、工件列表、checkpoint 历史不给
_CHAT_RUN_READS = ("", "/events", "/graph")


async def _run_scope(conn: HTTPConnection) -> None:
    """/api/runs 下的接口。builder 及以上全部放行；user 角色只在管理员开了问数据（Q2）时能碰这组接口，而且只碰自己的
    问数据运行（origin_kind=chat）：
    - 列表、别人的运行一律不给（记录页是给搭建者看的）；
    - 能发起（只能是问数据的探索运行，start_run 里按请求体再查）、能停止、能读运行记录和事件。这种运行发起时就把查询
      结果放在工具边界遮（tools/datasource.py 的 MASK_RESULTS_OPTION），事件、回答里只有遮过的值；
    - 自己发起的应用实例、有授权的应用视图运行、参与的实例，经应用页（/api/apps/{id}/instances）和证据接口
      （evidence_scope）读，这里不给原始事件和状态：tool.end 的预览、运行状态里是查询结果的原文，遮罩管不到。"""
    principal = current_principal()
    if principal.mode == "off" or principal.at_least("builder") or conn.scope["type"] != "http":
        return
    ensure_role("user")
    if not user_can_chat():
        raise HTTPException(403, "没有权限：运行记录只对搭建者及以上的角色开放")
    method = conn.scope.get("method", "GET")
    run_id = conn.path_params.get("run_id")
    if run_id is None:
        if method == "POST" and conn.url.path.rstrip("/") == "/api/runs":
            return
        raise HTTPException(403, "没有权限：运行记录只对搭建者及以上的角色开放")
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    if run is None or run.started_by != principal.username or run.origin_kind != "chat":
        raise _forbidden()
    rest = conn.url.path.rstrip("/").removeprefix(f"/api/runs/{run_id}")
    if (method == "GET" and rest in _CHAT_RUN_READS) or (method == "POST" and rest == "/cancel"):
        return
    raise HTTPException(403, "没有权限：这项操作需要搭建者及以上的角色")


def restricted() -> bool:
    """这次请求的人是不是开登录后的 user 角色：对它来说遮罩就是边界，只能做问数据、用应用这几件事。"""
    principal = current_principal()
    return principal.mode != "off" and not principal.at_least("builder")


async def _evidence_scope(conn: HTTPConnection) -> None:
    """/api/runs/{run_id}/evidence… 这组证据接口：builder 及以上全部放行；user 角色只读（GET），而且这次运行要能读
    （can_read_run）。证据接口交出的查询行本身按派生遮罩处理过。"""
    principal = current_principal()
    if principal.mode == "off" or principal.at_least("builder") or conn.scope["type"] != "http":
        return
    ensure_role("user")
    if conn.scope.get("method", "GET") != "GET":
        raise HTTPException(403, "没有权限：这项操作需要搭建者及以上的角色")
    run_id = conn.path_params.get("run_id")
    if not run_id or not await can_read_run(principal, run_id):
        raise _forbidden()


async def _chat_scope(conn: HTTPConnection) -> None:
    """问数据和助手（对话、生成）：builder 及以上；user 角色要管理员开了问数据（Q2），而且只给问数据用得到的那几个接口。"""
    principal = current_principal()
    if principal.mode == "off" or principal.at_least("builder"):
        return
    ensure_role("user")
    path = conn.url.path
    method = conn.scope.get("method", "GET")
    if user_can_chat() and (path.startswith("/api/conversations") or (method, path) in _CHAT_COPILOT):
        return
    raise HTTPException(403, "没有权限：问数据和助手需要搭建者及以上的角色，或由管理员为使用者开启问数据")


#: user 角色开了问数据时能用的助手接口：问数据的生成流和读当前助手模型
_CHAT_COPILOT = frozenset({("POST", "/api/copilot/generate-stream"), ("GET", "/api/copilot/model")})

run_scope = Depends(_run_scope)
evidence_scope = Depends(_evidence_scope)
chat_scope = Depends(_chat_scope)


def role_by_method(*, read: str, write: str) -> Any:
    """读（GET、HEAD）要 read，改动要 write 的路由级依赖：模型接入、运行默认值这类搭建者只读的设置。"""

    async def _check(conn: HTTPConnection) -> None:
        ensure_role(read if conn.scope.get("method", "GET") in ("GET", "HEAD") else write)

    return Depends(_check)
