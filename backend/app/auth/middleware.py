"""按请求认出是谁，写进 api/deps 的 contextvar（A7 第 2、4 条）。

纯 ASGI 中间件：HTTP 和 WebSocket 都经过它（Starlette 的 BaseHTTPMiddleware 不管 WebSocket），SSE 流式响应在它的
调用里发完，contextvar 一直有效。

关登录：不拦任何请求，写进 contextvar 的是等同管理员、未认证、署名取自 X-Actor 的身份；actor_of 照旧自己解码请求头，
和加这一层之前一模一样。
开登录：
- 会话 cookie → Principal（认证过的用户名、角色、组）；没有会话是匿名（username=None），X-Actor 一律不看；
- /api 下除了登录本身、健康检查、入站钩子，匿名一律 401。各路由的角色要求另由 main.py 的路由级依赖管，这里兜底：
  哪个路由忘了挂依赖，也不会对没登录的人敞开；
- 非 GET 的请求必须带 X-AgentLab-Request 头，带了 Origin 的必须是本站或登记的前端地址（CSRF）。自定义请求头跨站发不出来
  （要过 CORS 预检，只放行配置里的前端地址），SameSite=Lax 的 cookie 也不随跨站的表单提交带过去；
- WebSocket：没登录的握手前就关掉；登录了的交给路由，按运行判（api/deps.py 的 run_scope：user 角色只能连自己能读、
  查询结果在工具边界遮过的运行，也就是自己的问数据运行；应用实例页照旧轮询）。
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie
from typing import Any

from app.core.config import settings

#: 开登录时匿名也能访问的接口
_PUBLIC = frozenset({"/api/health", "/api/auth/me", "/api/auth/login", "/api/auth/logout", "/api/auth/oidc/start",
                     "/api/auth/oidc/callback", "/api/auth/jwks", "/api/auth/bootstrap"})
#: 只认签名、不走会话的入站钩子（也不查 CSRF：调用方是外部系统，没有浏览器的 Origin）
_HOOKS = "/api/hooks/"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-agentlab-request"


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - 坏掉的 cookie 头当没有
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _origin_ok(origin: str, headers: dict[str, str], scope: dict[str, Any]) -> bool:
    from app.core.public_url import origin_of, public_base_url

    allowed = {o.rstrip("/") for o in settings.cors_origins}
    # 对外访问地址（环境变量或设置里的那一项，core/public_url.py）：反向代理后面请求的 Host 不一定是它
    if address := public_base_url():
        allowed.add(origin_of(address))
    host = headers.get("x-forwarded-host") or headers.get("host")
    if host:
        scheme = headers.get("x-forwarded-proto") or scope.get("scheme") or "http"
        allowed.add(f"{scheme}://{host}")
    return origin.rstrip("/") in allowed


async def _reply(send: Any, status: int, detail: str, code: str) -> None:
    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def resolve(headers: dict[str, str], mode: str) -> Any:
    """cookie → Principal。没有有效会话是匿名（username=None、role 为空）。"""
    from app.api.deps import Principal
    from app.auth.sessions import COOKIE, principal_of, user_of
    from app.db.base import SessionLocal

    token = _cookie(headers, COOKIE)
    if token:
        async with SessionLocal() as session:
            user = await user_of(session, token)
            if user is not None:
                return await principal_of(session, user, mode)
    return Principal(username=None, role="", verified=False, mode=mode)


class AuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from app.api.deps import reset_principal, set_principal
        from app.auth import current_mode

        # 登录设置在启动时读进缓存（main.py 的 _platform_startup），改设置的接口改完就刷新；这里不查库
        mode = current_mode()
        headers = _headers(scope)
        if mode == "off":
            # 关登录：所有人等同管理员，署名是自报的 X-Actor。actor_of 照旧自己解码请求头，这里写进来只为让审计、
            # current_principal() 也拿得到这个署名
            from app.api.deps import Principal, decode_actor

            actor = decode_actor(headers.get("x-actor"))
            token = set_principal(Principal(username=actor, display_name=actor, role="admin", mode="off"))
            try:
                await self.app(scope, receive, send)
            finally:
                reset_principal(token)
            return

        path = scope.get("path") or ""
        is_api = path.startswith("/api/")
        if scope["type"] == "http" and is_api and not path.startswith(_HOOKS) \
                and scope.get("method", "GET").upper() not in _SAFE_METHODS:
            if CSRF_HEADER not in headers:
                await _reply(send, 403, "请求缺少来源标记，已拒绝。请从 AgentLab 页面操作", "csrf_header")
                return
            origin = headers.get("origin")
            if origin and not _origin_ok(origin, headers, scope):
                await _reply(send, 403, "请求来自其他站点，已拒绝", "csrf_origin")
                return

        principal = await resolve(headers, mode)
        if scope["type"] == "websocket" and not principal.username:
            # 握手前关闭：服务端回 403，不建立连接。登录了的不在这里一刀切：能不能连哪次运行要看运行（run_scope）
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        if (scope["type"] == "http" and is_api and not principal.username and path not in _PUBLIC
                and not path.startswith(_HOOKS)):
            await _reply(send, 401, "请先登录", "auth_required")
            return

        token = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(token)
