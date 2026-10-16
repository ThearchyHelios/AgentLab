"""按请求认出是谁，写进 api/deps 的 contextvar（A7 第 2、4 条）。

纯 ASGI 中间件：HTTP 和 WebSocket 都经过它（Starlette 的 BaseHTTPMiddleware 不管 WebSocket），SSE 流式响应在它的
调用里发完，contextvar 一直有效。

关登录：不拦任何请求，写进 contextvar 的是等同管理员、未认证、署名取自 X-Actor 的身份；actor_of 照旧自己解码请求头，
和加这一层之前一模一样。
开登录：
- 会话 cookie → Principal（认证过的用户名、角色、组）；没有会话是匿名（username=None），X-Actor 一律不看；
- /api 下除了登录本身、健康检查、入站钩子，匿名一律 401。各路由的角色要求另由 main.py 的路由级依赖管，这里兜底：
  哪个路由忘了挂依赖，也不会对没登录的人敞开；
- 接口说明（FastAPI 自带的 /docs、/redoc、/openapi.json）和 /api 一样拦：匿名 401，不到搭建者 403。它们是 FastAPI 自己
  挂的路由，挂不上路由级依赖，所以角色也在这里判。以前只拦 /api/ 开头的，开了登录接口全貌也对匿名的人敞开；
- 非 GET 的请求必须带 X-AgentLab-Request 头，带了 Origin 的必须是本站或登记的前端地址（CSRF）。自定义请求头跨站发不出来
  （要过 CORS 预检，只放行配置里的前端地址），SameSite=Lax 的 cookie 也不随跨站的表单提交带过去；
- WebSocket：没登录的握手前就关掉；登录了的交给路由，按运行判（api/deps.py 的 run_scope：user 角色只能连自己能读、
  查询结果在工具边界遮过的运行，也就是自己的问数据运行；应用实例页照旧轮询）。

访问密钥（Authorization: Bearer agl_…，auth/keys.py，docs/design/next-round/04-对外集成与触达.md A2）：
- 带了密钥的请求只按密钥认：不看 cookie、不看 X-Actor，也不查 CSRF——Authorization 头跨站发不出来（要过 CORS 预检，只放行
  登记的前端地址），同一个请求带了会话 cookie 也不起作用；
- 认不出（无效、到期、撤销、拥有者停用、个人密钥开关关了）回 401，各带各的 code，绝不退回匿名或会话；
- 只能调 /api 下的接口（哪些能调由路由级的门禁 keys.key_gate 按 KEY_ROUTES 判），WebSocket 一律拒；
- 关登录时回 409（keys_need_login）：关登录时所有请求都等同管理员，不能让调用方以为自己的权限被收窄了；
- 每把密钥每分钟 120 次，超了 429 带 Retry-After；每次请求（含被拒的）进请求记录。
"""
from __future__ import annotations

import json
from http.cookies import SimpleCookie
from typing import Any

from app.api.coded import AUTH_REQUIRED
from app.core.config import settings

#: 开登录时匿名也能访问的接口
_PUBLIC = frozenset({"/api/health", "/api/auth/me", "/api/auth/login", "/api/auth/logout", "/api/auth/oidc/start",
                     "/api/auth/oidc/callback", "/api/auth/jwks", "/api/auth/bootstrap"})
#: 只认签名、不走会话的入站钩子（也不查 CSRF：调用方是外部系统，没有浏览器的 Origin）
_HOOKS = "/api/hooks/"
#: FastAPI 自带的接口说明（main.py 里 FastAPI() 的缺省地址）：开登录时要搭建者
_DOCS = ("/docs", "/redoc", "/openapi.json")
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


def _is_docs(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _DOCS)


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


async def _reply(send: Any, status: int, detail: str, code: str,
                 headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *(headers or [])]})
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
        from app.auth import current_mode, refusal

        # 登录设置在启动时读进缓存（main.py 的 _platform_startup），改设置的接口改完就刷新；这里不查库
        mode = current_mode()
        headers = _headers(scope)
        from app.auth import keys

        if (secret := keys.bearer(headers)) is not None:
            await self._with_key(scope, receive, send, secret, mode)
            return
        if mode == "off":
            # 关登录：所有人等同管理员，署名是自报的 X-Actor。actor_of 照旧自己解码请求头，这里写进来只为让审计、
            # current_principal() 也拿得到这个署名
            from app.api.deps import Principal, decode_actor

            actor = decode_actor(headers.get("x-actor"))
            token = set_principal(Principal(username=actor, display_name=actor, role="admin", mode="off", via="off"))
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
            await _reply(send, 401, "请先登录", AUTH_REQUIRED)
            return
        if scope["type"] == "http" and _is_docs(path) and (refused := refusal(principal, "builder")) is not None:
            # 和路由级依赖（auth.require）同一个判断、同一句话：没登录 401，角色不够 403
            await _reply(send, refused.status_code, refused.detail, refused.code)
            return

        token = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(token)

    async def _with_key(self, scope: dict[str, Any], receive: Any, send: Any, secret: str, mode: str) -> None:
        """带访问密钥的请求（见模块说明）：只按密钥认，认出来的身份交给路由，门禁（keys.key_gate）按路由模板再判。"""
        from app.api.deps import reset_principal, set_principal
        from app.auth import keys

        if scope["type"] == "websocket":
            # 实时流只给界面：带密钥的握手前就关
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        if mode == "off":
            await _reply(send, 409, keys.KEYS_NEED_LOGIN_TEXT, keys.KEYS_NEED_LOGIN)
            return
        path = scope.get("path") or ""
        method = str(scope.get("method", "GET")).upper()
        if not path.startswith("/api/"):
            # 页面、FastAPI 自带的接口说明都不对密钥开放
            await _reply(send, 403, keys.ROUTE_FORBIDDEN_TEXT, keys.KEY_ROUTE_FORBIDDEN)
            return
        client = scope.get("client")
        ip = str(client[0]) if client else None
        principal = await keys.authenticate(secret, mode, ip)
        if isinstance(principal, keys.Refusal):
            if principal.key_id:
                keys.note(principal.key_id, method=method, route=path, status=principal.status, run_id=None, ip=ip)
            await _reply(send, principal.status, principal.detail, principal.code)
            return
        if wait := keys.take(principal.key_id):
            keys.note(principal.key_id, method=method, route=path, status=429, run_id=None, ip=ip)
            await _reply(send, 429, f"这把访问密钥的请求太频繁（每分钟最多 {keys.RATE_PER_MINUTE} 次），请 {wait} 秒后再试",
                         keys.KEY_RATE_LIMITED, [(b"retry-after", str(wait).encode())])
            return

        status = {"code": 0}

        async def watch(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status") or 0)
            await send(message)

        record, record_token = keys.begin_request()
        token = set_principal(principal)
        try:
            await self.app(scope, receive, watch)
        finally:
            reset_principal(token)
            keys.end_request(record_token)
            # 路由模板（请求走完时 FastAPI 记在 scope['route'] 上）；没匹配上任何路由的记原样的路径
            route = getattr(scope.get("route"), "path", None) or path
            run_id = record.get("run_id") or (scope.get("path_params") or {}).get("run_id")
            keys.note(principal.key_id, method=method, route=route, status=status["code"] or 500, run_id=run_id, ip=ip)
