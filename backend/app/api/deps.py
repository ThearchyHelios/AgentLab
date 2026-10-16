"""请求里的「人」：身份与权限的唯一入口（docs/design/app-platform-and-look.md §2.7、附录 C）。

登录方式三种：off（默认，今天的行为）、local、oidc。各接口不直接碰请求头或会话，只问这里：
- actor_of(header)：记在各「人」列上的名字。关登录时照旧解码 X-Actor（自报署名）；开了登录返回认证过的
  用户名，请求头一律不看——这样各接口的签名一个都不用改；
- current_principal()：完整身份（用户名、角色、组、是否认证过、登录方式），中间件按请求写进 contextvar；
- require_role(role)：挂在 main.py 的 include_router(..., dependencies=[...]) 上的路由级依赖；
- can_read_run / can_read_artifact：运行、工件的读权限（user 角色只能读有授权的应用视图运行、自己参与的实例）；
- ensure_role / ensure_can_publish：接口内部按请求内容判定的权限（MCP 的 stdio、受管级发布、版本退役，Q8）；
- restricted()：这次请求是不是开登录后的 user 角色（按请求内容收窄的接口用：问数据只能发起探索运行、工件按白名单给）；
- run_events_readable(principal, run)：运行的原始记录、事件、实时流能不能读（user 只读查询结果在工具边界遮过的）；
- ensure_conversation_access(session, conversation_id)：接受 conversation_id 的接口核对对话归属（对话按人隔离）；
- run_scope / evidence_scope / chat_scope：main.py 挂在运行、证据、问数据几组路由上的依赖，按 run_id 判断能不能读。
  run_scope 同样管运行的 WebSocket，握手时按运行判；
- private_run_scope：挂在只给搭建者的运行子路由（对比、可验证报告、质询、假设分析）上，别人的个人应用刷新、个人蓝图
  试运行只有拥有者能碰（auth/access.py 的 private_owner），管理员、搭建者也不例外。

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


def signer_of(given: Any, header: str | None, limit: int = 100) -> str | None:
    """请求体里自带署名（signed_by）的留痕记谁：开了登录一律是认证过的用户名，请求体里填的不看——不然随手填一个名字就
    盖掉了登录的人；关登录时请求体里填的优先（截到 limit 个字），其次 X-Actor，都只原样记下（未认证）。"""
    principal = _principal.get()
    if principal is not None and principal.mode != "off":
        return principal.username
    if isinstance(given, str) and given.strip():
        return given.strip()[:limit]
    return decode_actor(header)


def signer_verified() -> bool:
    """signer_of 记下的署名是不是认证过的身份：开了登录、这次请求的身份认证过时为真；关登录时署名是自报的，为假。

    导入清单、累积快照清单、作废记录、清除原件记录的「是否认证」记这个（docs/design/next-round/05-已知遗留.md (c)），
    和审计表、待办派单、run.origin 的 verified 同一个口径。证据要在生成那一刻定下：调用方和 signed_by 一起往下传。"""
    principal = _principal.get()
    return principal is not None and principal.mode != "off" and bool(principal.verified)


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


async def ensure_conversation_access(session: Any, conversation_id: str | None) -> Any:
    """接受 conversation_id 的接口（助手的 generate-stream、generate、app-stream、from-run……）在读这段对话的历史、记来源
    之前调：这段对话这个人能不能用（对话按人隔离，D-d）。能用交回对话（Conversation）；没给 id、对话不存在交回 None
    （和以前一样当没有历史，不报错）；存在但不能用回 403。规则见 api/conversations.py 的同名函数。"""
    from app.api.conversations import ensure_conversation_access as check

    return await check(session, conversation_id)


def _forbidden() -> HTTPException:
    return HTTPException(403, "没有权限查看这次运行")


#: user 角色在自己的问数据运行上能读的（GET /api/runs/{id} 之后的部分）：运行记录、事件、运行时的图，加上实时流
#: （WebSocket /stream，握手时另判）。这几样就是问数据页要的；状态、工件列表、checkpoint 历史不给
_CHAT_RUN_READS = ("", "/events", "/graph")
_RUNS_FOR_BUILDERS = "没有权限：运行记录只对搭建者及以上的角色开放"


def masked_at_boundary(run: Any) -> bool:
    """这次运行的查询结果是不是发起时就定下在工具边界遮（tools/datasource.py 的 MASK_RESULTS_OPTION）。开登录后 user 角色
    发起的问数据运行一定带它（api/runs.py 的 start_run），随运行记录沿用到续跑、审批恢复。"""
    from app.tools.datasource import MASK_RESULTS_OPTION

    options = getattr(run, "options", None)
    return isinstance(options, dict) and bool(options.get(MASK_RESULTS_OPTION))


async def run_events_readable(principal: Principal, run: Any) -> bool:
    """能不能读这次运行的原始记录、事件和实时流（GET /api/runs/{id}、/events、/graph，WebSocket /stream）。

    关登录：能。builder 及以上：这次运行它能读就能（can_read_run：别人的个人应用刷新、个人蓝图试运行读不了）。
    user：这次运行它能读（can_read_run），而且查询结果在工具边界就遮过（masked_at_boundary）。两个条件缺一不可：
    没在边界遮过的运行，事件里 tool.end 的预览、运行状态里是查询结果的原文，遮罩管不到——自己发起的应用实例、
    有授权的应用视图运行经应用页和证据接口读（那两处按派生遮罩给）。实际上满足两条的就是它自己开登录后发起的
    问数据运行；关登录时自报同名留下的老运行没在边界遮过，开了登录也读不到。"""
    if principal.mode == "off":
        return True
    if principal.at_least("builder"):
        return await can_read_run(principal, run)
    if run is None or not principal.username or not masked_at_boundary(run):
        return False
    return await can_read_run(principal, run)


def _own_chat_run(principal: Principal, run: Any) -> bool:
    return run is not None and bool(principal.username) and run.started_by == principal.username \
        and run.origin_kind == "chat"


async def _run_scope(conn: HTTPConnection) -> None:
    """/api/runs 下的接口和运行的实时流。builder 及以上放行，只有别人的个人应用刷新、个人蓝图试运行例外（只给拥有者，
    _refuse_private）；user 角色只在管理员开了问数据（Q2）时能碰这组接口：
    - 列表、别人的运行一律不给（记录页是给搭建者看的）；
    - 能发起（只能是问数据的探索运行，start_run 里按请求体再查）；能停止自己的问数据运行；
    - 能读运行记录、事件、运行时的图，能连 WebSocket：按运行判（run_events_readable），握手时就判，不再一刀切只给
      builder。能读的就是它自己发起、查询结果在工具边界遮过的问数据运行，事件、回答里只有遮过的值；
    - 自己发起的应用实例、有授权的应用视图运行、参与的实例，经应用页（/api/apps/{id}/instances）和证据接口
      （evidence_scope）读，这里不给原始事件和状态：tool.end 的预览、运行状态里是查询结果的原文，遮罩管不到。"""
    principal = current_principal()
    if principal.mode == "off":
        return
    if principal.at_least("builder"):
        await _refuse_private(conn, principal)
        return
    if conn.scope["type"] == "websocket":
        # 握手前拒：服务端回 1008，不建立连接。没登录的中间件已经挡掉了，这里只剩按运行判
        if not (principal.username and user_can_chat()
                and await run_events_readable(principal, await _load_run(conn.path_params.get("run_id")))):
            from fastapi import WebSocketException, status

            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        return
    ensure_role("user")
    if not user_can_chat():
        raise HTTPException(403, _RUNS_FOR_BUILDERS)
    method = conn.scope.get("method", "GET")
    run_id = conn.path_params.get("run_id")
    if run_id is None:
        if method == "POST" and conn.url.path.rstrip("/") == "/api/runs":
            return
        raise HTTPException(403, _RUNS_FOR_BUILDERS)
    run = await _load_run(run_id)
    rest = conn.url.path.rstrip("/").removeprefix(f"/api/runs/{run_id}")
    if method == "GET" and rest in _CHAT_RUN_READS:
        if await run_events_readable(principal, run):
            return
        raise _forbidden()
    if not _own_chat_run(principal, run):
        raise _forbidden()
    if method == "POST" and rest == "/cancel":
        return
    raise HTTPException(403, "没有权限：这项操作需要搭建者及以上的角色")


async def _load_run(run_id: str | None) -> Any:
    if not run_id:
        return None
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        return await session.get(Run, run_id)


#: 搭建者、管理员碰别人的个人应用刷新、个人蓝图试运行时的说法
PRIVATE_RUN = "没有权限查看这次运行：个人应用的刷新和个人蓝图的试运行只有拥有者能看"
#: 运行子路由里另带的运行 id（对比的基准 ?base=）
_RUN_QUERY_KEYS = ("base",)


async def _refuse_private(conn: HTTPConnection, principal: Principal) -> None:
    """builder 及以上：路径里的运行（和对比的基准）是别人的个人应用刷新、个人蓝图试运行时拒（403，WebSocket 握手前
    1008）。不存在的运行放过去，由接口照旧回 404。"""
    ids = [conn.path_params.get("run_id"), *(conn.query_params.get(k) for k in _RUN_QUERY_KEYS)]
    for run_id in (i for i in ids if i):
        if await can_read_run(principal, run_id):
            continue
        if conn.scope["type"] == "websocket":
            from fastapi import WebSocketException, status

            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        raise HTTPException(403, PRIVATE_RUN)


async def _private_run_scope(conn: HTTPConnection) -> None:
    """只给搭建者的运行子路由（main.py 里对比、可验证报告、质询、假设分析）：角色由 require_role 判，这里只管别人的
    个人应用刷新、个人蓝图试运行。"""
    principal = current_principal()
    if principal.mode != "off":
        await _refuse_private(conn, principal)


def restricted() -> bool:
    """这次请求的人是不是开登录后的 user 角色：对它来说遮罩就是边界，只能做问数据、用应用这几件事。"""
    principal = current_principal()
    return principal.mode != "off" and not principal.at_least("builder")


async def _evidence_scope(conn: HTTPConnection) -> None:
    """/api/runs/{run_id}/evidence… 这组证据接口：builder 及以上放行，别人的个人应用刷新、个人蓝图试运行除外（只给
    拥有者）；user 角色只读（GET），而且这次运行要能读（can_read_run）。证据接口交出的查询行本身按派生遮罩处理过。"""
    principal = current_principal()
    if principal.mode == "off" or conn.scope["type"] != "http":
        return
    if principal.at_least("builder"):
        await _refuse_private(conn, principal)
        return
    ensure_role("user")
    if conn.scope.get("method", "GET") != "GET":
        raise HTTPException(403, "没有权限：这项操作需要搭建者及以上的角色")
    run_id = conn.path_params.get("run_id")
    if not run_id or not await can_read_run(principal, run_id):
        raise _forbidden()


async def _chat_scope(conn: HTTPConnection) -> None:
    """问数据和助手（对话、生成）：builder 及以上；user 角色要管理员开了问数据（Q2），而且只给问数据用得到的那几个接口。
    对话按人隔离（只看得到、只改得动自己建的）在 api/conversations.py 里按对话判（ensure_conversation_access）。"""
    principal = current_principal()
    if principal.mode == "off" or principal.at_least("builder"):
        return
    ensure_role("user")
    path = conn.url.path.rstrip("/")
    method = conn.scope.get("method", "GET")
    if user_can_chat() and (path.startswith("/api/conversations") or (method, path) in _CHAT_ROUTES):
        return
    raise HTTPException(403, "没有权限：问数据和助手需要搭建者及以上的角色，或由管理员为使用者开启问数据")


#: user 角色开了问数据时能用的其余接口：问数据的生成流、读当前助手模型、问数据页选数据源用的数据源名单
#: （GET /api/datasources/names，只有名字、说明和种类，不含连接信息）
_CHAT_ROUTES = frozenset({("POST", "/api/copilot/generate-stream"), ("GET", "/api/copilot/model"),
                          ("GET", "/api/datasources/names")})

run_scope = Depends(_run_scope)
evidence_scope = Depends(_evidence_scope)
chat_scope = Depends(_chat_scope)
private_run_scope = Depends(_private_run_scope)


def role_by_method(*, read: str, write: str) -> Any:
    """读（GET、HEAD）要 read，改动要 write 的路由级依赖：模型接入、运行默认值这类搭建者只读的设置。"""

    async def _check(conn: HTTPConnection) -> None:
        ensure_role(read if conn.scope.get("method", "GET") in ("GET", "HEAD") else write)

    return Depends(_check)
