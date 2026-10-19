"""访问密钥（docs/design/next-round/04-对外集成与触达.md A1–A7）：签发、认人、门禁、限流、幂等、请求记录、接口说明。

外部系统（门户、OA、BI）和个人脚本用一把访问密钥调接口：Authorization: Bearer agl_…。密钥是不记名的，泄露了就能被冒用，
所以这一层的每个决定都往窄处取：
- 签发（A1）：密钥格式 agl_ 加 32 字节随机串，只在创建、轮换时交回一次；库里只存 sha256 和开头一截（prefix，认是哪一把）。
  到期必填（最长 365 天，缺省 90 天）；轮换是签一把新的、旧的 24 小时后到期，换钥匙不用停机；撤销立刻失效。拥有者是人
  （个人访问密钥，管理员开关 auth.personal_keys 缺省关）或服务账号（users.kind=service，svc- 开头、没有口令、不能登录、
  角色固定使用者，管理员建）。集成依附在某个员工身上，人一离职集成就断，所以有服务账号；开发自己写脚本拉数时不用找管理员，
  所以有个人密钥——它暴露面更大，缺省关，管理员关掉开关时已有的个人密钥一并不认。
- 认人（A2，authenticate）：中间件按哈希找密钥。找不到、到期、撤销、拥有者停用（或被删）、个人密钥开关关了，各回 401 带
  各自的 code，绝不退回匿名、也不退回会话 cookie。认出来的身份一律封顶在使用者（A3，principal_for_key）：role=user、组是
  拥有者现在的组、verified；不论拥有者是管理员还是搭建者——现有的使用者规则（只看得到授权的应用、最高到查看全部实例、工件
  按白名单遮过、运行事件和实时流碰不到）全部自动生效，不另写一套「密钥能读什么」。
- 门禁（A2，key_gate）：KEY_ROUTES 一张（方法, 路由模板）→ scope 的表，在 FastAPI(dependencies=[Depends(key_gate)]) 上挂一次，
  按 request.scope['route'].path 比对路由模板（不在中间件里写正则：会和路由定义重复一遍，改路由时漏改）。不在表里的 403
  key_route_forbidden；缺 scope 的 403 key_scope_missing；路径里的应用不在密钥限定的应用里 403 key_app_forbidden；个人应用、
  只给拥有者的运行（个人应用的刷新、个人蓝图的试运行）一律 403 key_app_personal。同一张表生成接口说明（A7，reference），
  说明和真实放行的接口出自一处，不会对不上。
- 限流（take）：每把密钥每分钟 120 次，进程内令牌桶，超了 429 带 Retry-After。
- 幂等（A4，once）：用密钥发起实例、刷新视图必须带 Idempotency-Key（外部系统超时重试一次就多出一个实例），按（拥有者, 键）
  去重——轮换了密钥同一个键照样认得出；状态机和钩子共用（process/receipts.py）。
- 请求记录（A6，note / flush）：哪一把、什么时候、哪个接口、回了什么、涉及哪次运行、从哪来。先在进程内攒着，调度循环每一拍
  （或攒满 FLUSH_AT 条）批量写一次，保留 RETAIN 天；last_used_at 至少隔 1 分钟写一次——每个请求同步写一行会和运行抢 SQLite 的
  写锁（同 sessions 的节流）。

不限定来源 IP：服务端认的客户端地址是 request.client.host，在反向代理后面认不准，靠到期、撤销和请求记录兜底。限流、请求缓冲
都在进程内，以单进程部署为前提（同登录失败退避）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select
from starlette.requests import HTTPConnection

from app.api.coded import CodedHTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量与错误码

#: 密钥的固定开头：密钥扫描工具据此认出泄露的 AgentLab 密钥
KEY_PREFIX = "agl_"
#: 随机部分的字节数
KEY_BYTES = 32
#: 库里存的明文开头：agl_ 加随机串的前 8 位
PREFIX_LEN = len(KEY_PREFIX) + 8
#: 服务账号的用户名开头
SERVICE_PREFIX = "svc-"
SERVICE_KIND = "service"

#: 能申请的 scope（A4）：读应用（列表、详情、页面、视图数据、实例）、发起入口和刷新视图、读待办、回复待办（只给个人密钥）、
#: 读报告和证据。另有一个不用申请就有的 me（GET /api/auth/me 自省、/api/health）
SCOPES = ("apps.read", "apps.run", "tasks.read", "tasks.respond", "reports.read")
ME = "me"
#: 新建时缺省只勾读的（集成-I3）
READ_SCOPES = ("apps.read", "tasks.read", "reports.read")
#: 只给个人密钥的 scope：服务账号不能被派待办，外部系统回调仍走 signal 钩子
PERSONAL_SCOPES = ("tasks.respond",)
#: 用密钥能回复的待办种类：审批、补充输入、编辑稿件。确认写入、核实写入结果、双盲复算裁决、工具和代码审批要人在界面上
#: 看过确认卡再按
TASK_KINDS = ("approve", "input", "edit")

EXPIRES_DEFAULT_DAYS = 90
EXPIRES_MAX_DAYS = 365
#: 轮换后旧密钥还能用多久
ROTATE_GRACE = timedelta(hours=24)
#: last_used_at 至少隔这么久才写一次（同 sessions._TOUCH_EVERY）
TOUCH_EVERY = timedelta(minutes=1)
#: 每把密钥每分钟最多几次请求
RATE_PER_MINUTE = 120
#: 请求记录攒满多少条就不等调度循环、立刻写一次
FLUSH_AT = 200
#: 请求记录保留多少天
RETAIN = timedelta(days=90)
#: 缓冲里最多攒多少条：库一直写不进去时丢最早的（记一条日志），不让进程内存无限涨
PENDING_MAX = 10_000
#: 过期的请求记录多久清一次
_PURGE_EVERY_S = 3600
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: 名字、限定应用的上限
NAME_MAX = 100
APPS_MAX = 50

#: 认不出密钥（401）：不存在（填错了或拥有者被删了）、到期、撤销、拥有者停用、个人密钥的开关关了
KEY_INVALID = "key_invalid"
KEY_EXPIRED = "key_expired"
KEY_REVOKED = "key_revoked"
KEY_OWNER_DISABLED = "key_owner_disabled"
KEY_PERSONAL_OFF = "key_personal_off"
#: 关登录时带了密钥（409）：免得调用方以为自己的权限被收窄了，其实服务端把他当成了管理员
KEYS_NEED_LOGIN = "keys_need_login"
#: 门禁（403）：接口不在表里、缺 scope、应用不在限定的应用里、个人应用和只给拥有者的运行
KEY_ROUTE_FORBIDDEN = "key_route_forbidden"
KEY_SCOPE_MISSING = "key_scope_missing"
KEY_APP_FORBIDDEN = "key_app_forbidden"
KEY_APP_PERSONAL = "key_app_personal"
#: 限流（429，带 Retry-After）
KEY_RATE_LIMITED = "key_rate_limited"
#: 幂等：没带键（400）、同一个键正在处理（409）、同一个键已经用来办过另一件事（422）
KEY_IDEMPOTENCY_REQUIRED = "key_idempotency_required"
KEY_IDEMPOTENCY_BUSY = "key_idempotency_busy"
KEY_IDEMPOTENCY_REUSED = "key_idempotency_reused"
#: 入口表单里有文件控件（422）：文件要先在应用里上传，密钥不开上传接口
KEY_FILES_UNSUPPORTED = "key_files_unsupported"
#: 这种待办不能用密钥回复（403）；服务账号的密钥不能回复待办（403）
KEY_TASK_KIND = "key_task_kind"

KEYS_NEED_LOGIN_TEXT = "未开启登录，访问密钥不生效：关登录时所有请求都等同管理员，带着密钥也不会按密钥的权限收窄。请先开启登录"
ROUTE_FORBIDDEN_TEXT = "访问密钥不能调这个接口：能调的接口见「设置 → 访问密钥 → 接口说明」"
APP_PERSONAL_TEXT = "访问密钥不能读个人应用、个人应用的刷新和蓝图的试运行：只给正式发布的应用"
APP_FORBIDDEN_TEXT = "这把访问密钥限定了应用，这个应用不在其中"


def _forbidden(detail: str, code: str) -> CodedHTTPException:
    return CodedHTTPException(403, detail, code)


# ---------------------------------------------------------------- 密钥本身


def new_secret() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def prefix_of(secret: str) -> str:
    return secret[:PREFIX_LEN]


def key_hash(secret: str) -> str:
    from app.auth.sessions import token_hash

    return token_hash(secret)


def bearer(headers: dict[str, str]) -> str | None:
    """请求头里的访问密钥：Authorization: Bearer agl_…。别的 Authorization（Basic、别的 Bearer）不归这里管，照旧按会话认。"""
    raw = (headers.get("authorization") or "").strip()
    scheme, _, value = raw.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(KEY_PREFIX):
        return None
    return value


def aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def state_of(row: Any, owner: Any, now: datetime | None = None) -> str:
    """一把密钥现在的状态：revoked 已撤销 / expired 已过期 / owner_disabled 拥有者停用了 / active 能用。"""
    from app.db.base import utcnow

    now = now or utcnow()
    if row.revoked_at is not None:
        return "revoked"
    if aware(row.expires_at) <= now:
        return "expired"
    if owner is None or owner.disabled:
        return "owner_disabled"
    return "active"


def scopes_of(row: Any, owner: Any) -> frozenset[str]:
    """这把密钥实际带的 scope：存着的里头认得的，加上 me；服务账号的另去掉只给个人密钥的（建的时候就不让勾，这里再守一道）。"""
    wanted = {s for s in (row.scopes or []) if s in SCOPES}
    if getattr(owner, "kind", None) == SERVICE_KIND:
        wanted -= set(PERSONAL_SCOPES)
    return frozenset(wanted | {ME})


# ---------------------------------------------------------------- 认人（中间件调）


@dataclass(frozen=True)
class Refusal:
    """认不出这把密钥：回 status 和这一句、这个码。key_id 是认得出是哪一把、只是不能用的（到期、撤销、停用），请求记录记它。"""

    status: int
    detail: str
    code: str
    key_id: str | None = None


def principal_for_key(row: Any, owner: Any, groups: tuple[str, ...], mode: str) -> Any:
    """按密钥认出来的身份（A3）：一律是使用者。拥有者现在的组、显示名照用；scope、限定的应用另带着，门禁里再收窄。"""
    from app.api.deps import Principal

    apps = tuple(str(a) for a in row.app_ids or [] if a) or None
    return Principal(username=owner.username, role="user", display_name=owner.display_name or owner.username,
                     user_id=owner.id, groups=groups, verified=True, mode=mode, via="key", key_id=row.id,
                     key_name=row.name, scopes=scopes_of(row, owner), app_ids=apps)


async def authenticate(secret: str, mode: str, ip: str | None) -> Any:
    """密钥 → Principal；认不出交回 Refusal。顺带按节流写 last_used_at / last_used_ip。"""
    from app.auth import personal_keys_enabled
    from app.auth.sessions import groups_of
    from app.db.base import SessionLocal, utcnow
    from app.db.models_identity import AccessKey, User

    async with SessionLocal() as session:
        row = await session.scalar(select(AccessKey).where(AccessKey.key_hash == key_hash(secret)))
        if row is None:
            return Refusal(401, "访问密钥无效：可能填错了，或者已被删除", KEY_INVALID)
        owner = await session.get(User, row.owner_user_id)
        now = utcnow()
        state = state_of(row, owner, now)
        if state == "revoked":
            return Refusal(401, "这把访问密钥已撤销，请换一把新的", KEY_REVOKED, row.id)
        if state == "expired":
            return Refusal(401, f"这把访问密钥已于 {aware(row.expires_at).astimezone():%Y-%m-%d %H:%M} 过期，请换一把新的",
                           KEY_EXPIRED, row.id)
        if state == "owner_disabled":
            return Refusal(401, "这把访问密钥的拥有者已停用，密钥随之失效", KEY_OWNER_DISABLED, row.id)
        if owner.kind != SERVICE_KIND and not personal_keys_enabled():
            return Refusal(401, "管理员已关闭个人访问密钥，这把密钥暂不生效；系统间集成请用服务账号的密钥", KEY_PERSONAL_OFF,
                           row.id)
        groups = await groups_of(session, owner.id)
        last = aware(row.last_used_at)
        if last is None or now - last > TOUCH_EVERY:
            row.last_used_at = now
            row.last_used_ip = (ip or "")[:64] or None
            await session.commit()
        return principal_for_key(row, owner, groups, mode)


async def introspect(session: Any, principal: Any) -> dict[str, Any]:
    """GET /api/auth/me 用密钥调时另给的那一块：哪一把（名字、开头）、scope、限定的应用、到期时间、拥有者是不是服务账号。"""
    from app.db.models_identity import AccessKey, User

    row = await session.get(AccessKey, principal.key_id)
    owner = await session.get(User, row.owner_user_id) if row is not None else None
    return {"id": principal.key_id, "name": principal.key_name, "prefix": row.prefix if row is not None else None,
            "scopes": sorted(principal.scopes), "app_ids": list(principal.app_ids) if principal.app_ids else None,
            "expires_at": aware(row.expires_at).isoformat() if row is not None else None,
            "service": getattr(owner, "kind", None) == SERVICE_KIND}


# ---------------------------------------------------------------- 限流


#: 密钥 id → (还剩几个令牌, 上次补的时刻)
_BUCKETS: dict[str, tuple[float, float]] = {}


def take(key_id: str, *, now: float | None = None) -> int:
    """这把密钥再发一个请求：放行交回 0，超了交回还要等几秒（Retry-After）。令牌桶：满桶 RATE_PER_MINUTE 个，每秒补
    RATE_PER_MINUTE / 60 个。"""
    rate = RATE_PER_MINUTE / 60.0
    moment = time.monotonic() if now is None else now
    tokens, at = _BUCKETS.get(key_id, (float(RATE_PER_MINUTE), moment))
    tokens = min(float(RATE_PER_MINUTE), tokens + (moment - at) * rate)
    if tokens >= 1.0:
        _BUCKETS[key_id] = (tokens - 1.0, moment)
        return 0
    _BUCKETS[key_id] = (tokens, moment)
    return max(1, math.ceil((1.0 - tokens) / rate))


# ---------------------------------------------------------------- 门禁


@dataclass(frozen=True)
class KeyRoute:
    """KEY_ROUTES 里的一行：要什么 scope、给人看的一句说明、可能回的错误码（通用的几种另列，见 COMMON_ERRORS）、要不要带幂等键。"""

    scope: str
    summary: str
    errors: tuple[str, ...] = ()
    idempotent: bool = False


_APP = ("app_not_found", "app_forbidden", KEY_APP_FORBIDDEN, KEY_APP_PERSONAL)
_PUBLISHED = ("app_unpublished",)
_RUN = ("run_not_found", "run_forbidden", KEY_APP_FORBIDDEN, KEY_APP_PERSONAL)
_IDEM = (KEY_IDEMPOTENCY_REQUIRED, KEY_IDEMPOTENCY_BUSY, KEY_IDEMPOTENCY_REUSED)

#: 访问密钥能调的接口：(方法, 路由模板) → KeyRoute。不在这里的一律 403（key_route_forbidden）。门禁和接口说明都只认这一张
KEY_ROUTES: dict[tuple[str, str], KeyRoute] = {
    ("GET", "/api/auth/me"): KeyRoute(ME, "这把密钥认出来是谁：拥有者、组、scope、限定的应用、到期时间"),
    ("GET", "/api/health"): KeyRoute(ME, "服务在不在"),
    ("GET", "/api/apps"): KeyRoute("apps.read", "能用的应用：授权给拥有者、所在组或所有人的；限定了应用的只列那几个"),
    ("GET", "/api/apps/{app_id}"): KeyRoute("apps.read", "应用概况和当前发布版的页面、入口目录", _APP),
    ("GET", "/api/apps/{app_id}/pages/{page_id}"): KeyRoute(
        "apps.read", "页面的视图模型（agentlab.view/1）：服务端渲染好的文字和证据指针，遮罩值不出服务端",
        (*_APP, *_PUBLISHED, "app_page_not_found", "app_params_invalid")),
    ("GET", "/api/apps/{app_id}/views/{view_id}/data"): KeyRoute(
        "apps.read", "视图数据（agentlab.viewdata/1）：印章、清单哈希、口径卡指标的原值和渲染文字；遮罩的指标 value 为 null。"
        "按需视图读这组参数（?params=）上次算的结果，不发起运行",
        (*_APP, *_PUBLISHED, "app_view_not_found", "app_view_refused", "app_params_invalid")),
    ("GET", "/api/apps/{app_id}/instances"): KeyRoute(
        "apps.read", "实例列表：只有「使用」授权的只列拥有者自己发起的，?who=all 要「查看全部实例」授权", _APP),
    ("GET", "/api/apps/{app_id}/instances/{run_id}"): KeyRoute(
        "apps.read", "一个实例的状态和实例页的视图模型", (*_APP, "app_instance_not_found")),
    ("POST", "/api/apps/{app_id}/entries/{entry_id}"): KeyRoute(
        "apps.run", "按入口发起一个实例（当前发布版）。必须带 Idempotency-Key：同一个键重发交回同一个 run_id",
        (*_APP, *_PUBLISHED, *_IDEM, "app_entry_not_found", "app_needs_upgrade", "form_invalid", KEY_FILES_UNSUPPORTED,
         "run_launch_refused"), idempotent=True),
    ("POST", "/api/apps/{app_id}/views/{view_id}/refresh"): KeyRoute(
        "apps.run", "按需视图按参数运行一次（命中缓存或接上同一组参数正在运行的那次就不再发起）。必须带 Idempotency-Key；"
        "沿用每个应用每天的次数上限",
        (*_APP, *_PUBLISHED, *_IDEM, "app_view_refused", "app_needs_upgrade", "app_view_daily_limit", "form_invalid",
         "run_launch_refused"), idempotent=True),
    ("GET", "/api/tasks"): KeyRoute("tasks.read", "派给拥有者（或所在组）的待办；限定了应用的只列那几个应用里的"),
    ("GET", "/api/tasks/count"): KeyRoute("tasks.read", "派给拥有者的、待处理的待办数"),
    ("GET", "/api/tasks/{task_id}"): KeyRoute("tasks.read", "一条待办的详情", ("task_not_found", KEY_APP_FORBIDDEN,
                                                                         KEY_APP_PERSONAL)),
    ("POST", "/api/tasks/{task_id}/respond"): KeyRoute(
        "tasks.respond", "回复一条待办：只限审批、补充输入、编辑稿件三种，只给个人访问密钥",
        ("task_not_found", "task_not_assigned", "task_closed", "task_invalid", "form_invalid", KEY_TASK_KIND,
         KEY_FILES_UNSUPPORTED, KEY_APP_FORBIDDEN, KEY_APP_PERSONAL)),
    ("GET", "/api/runs/{run_id}/report.html"): KeyRoute(
        "reports.read", "运行的报告导出成一个自包含的 HTML 文件；遮罩的值写「已遮罩」",
        (*_RUN, "evidence_report_not_found")),
    ("GET", "/api/runs/{run_id}/evidence"): KeyRoute("reports.read", "整次运行的证据图（遮过的）", _RUN),
    ("GET", "/api/runs/{run_id}/evidence/metrics/{node}/{metric}"): KeyRoute(
        "reports.read", "视图数据里一个指标的证据（指针里的 node、metric）：指标 → 输入 → 查询快照，遮过的",
        (*_RUN, "evidence_metric_not_found")),
    ("GET", "/api/artifacts/{artifact_id}"): KeyRoute(
        "reports.read", "按内容哈希取一件工件：只给报告文档、查询快照、指标集、动作回执几种，按使用者的遮罩处理",
        ("artifact_forbidden", "artifact_kind_forbidden", "artifact_not_found", KEY_APP_FORBIDDEN, KEY_APP_PERSONAL)),
}

#: 每个接口都可能回的几种（认人、门禁、限流）
COMMON_ERRORS = (KEY_INVALID, KEY_EXPIRED, KEY_REVOKED, KEY_OWNER_DISABLED, KEY_PERSONAL_OFF, KEYS_NEED_LOGIN,
                 KEY_ROUTE_FORBIDDEN, KEY_SCOPE_MISSING, KEY_RATE_LIMITED)


def app_allowed(principal: Any, app: Any) -> bool:
    """这把密钥碰不碰得到这个应用：个人应用一律不碰；限定了应用的只认那几个。不是密钥认出来的一律为真（不归这里管）。"""
    if getattr(principal, "via", None) != "key":
        return True
    if getattr(app, "scope", "shared") == "personal":
        return False
    return principal.app_ids is None or app.id in principal.app_ids


async def _check_app(session: Any, principal: Any, app_id: str) -> None:
    from app.db.models_apps import App

    app = await session.get(App, app_id)
    if app is None:
        return  # 接口照旧回 404
    if getattr(app, "scope", "shared") == "personal":
        raise _forbidden(APP_PERSONAL_TEXT, KEY_APP_PERSONAL)
    if principal.app_ids is not None and app.id not in principal.app_ids:
        raise _forbidden(APP_FORBIDDEN_TEXT, KEY_APP_FORBIDDEN)


async def _run_refusal(session: Any, principal: Any, run: Any) -> CodedHTTPException | None:
    """这次运行这把密钥碰不碰得到：只给拥有者的运行（个人应用的刷新、个人蓝图的试运行）不碰；限定了应用的，运行要属于那几个
    应用，或者是那几个应用当前发布版作为视图固定的工作流版本的正式运行（视图读的是定时任务那次运行，它不带应用 id）。"""
    from app.auth.access import PRIVATE_ORIGINS, _viewed_by_granted_app
    from app.db.models_apps import App

    if (getattr(run, "origin_kind", None) or "") in PRIVATE_ORIGINS:
        return _forbidden(APP_PERSONAL_TEXT, KEY_APP_PERSONAL)
    if run.app_id:
        scope = await session.scalar(select(App.scope).where(App.id == run.app_id))
        if scope == "personal":
            return _forbidden(APP_PERSONAL_TEXT, KEY_APP_PERSONAL)
    if principal.app_ids is None:
        return None
    if run.app_id in principal.app_ids or await _viewed_by_granted_app(session, run, set(principal.app_ids)):
        return None
    return _forbidden(APP_FORBIDDEN_TEXT, KEY_APP_FORBIDDEN)


async def _check_run(session: Any, principal: Any, run_id: str) -> None:
    from app.db.models import Run

    run = await session.get(Run, run_id)
    if run is not None and (refused := await _run_refusal(session, principal, run)) is not None:
        raise refused


async def _check_task(session: Any, principal: Any, task_id: str) -> None:
    from app.db.models import Run, Task

    task = await session.get(Task, task_id)
    if task is None:
        return
    if task.app_id:
        await _check_app(session, principal, task.app_id)
    elif principal.app_ids is not None:
        # 不属于应用的待办（画布、定时任务里的审批）：限定了应用的密钥碰不到
        raise _forbidden(APP_FORBIDDEN_TEXT, KEY_APP_FORBIDDEN)
    from app.auth.access import PRIVATE_ORIGINS

    run = await session.get(Run, task.run_id) if task.run_id else None
    if run is not None and (getattr(run, "origin_kind", None) or "") in PRIVATE_ORIGINS:
        raise _forbidden(APP_PERSONAL_TEXT, KEY_APP_PERSONAL)


async def _check_artifact(session: Any, principal: Any, artifact_id: str) -> None:
    """工件按内容寻址，可能被好几次运行引用：只要有一次是这把密钥碰得到的就放过（能不能读、怎么遮由工件接口照使用者判）。
    一次都没有的拒；不挂在任何运行上的放过去，由工件接口照使用者拒。"""
    from app.db.models import Artifact, Run

    run_ids = [r for r in (await session.execute(select(Artifact.run_id).where(Artifact.id == artifact_id)))
               .scalars() if r]
    refused: CodedHTTPException | None = None
    for run_id in dict.fromkeys(run_ids):
        run = await session.get(Run, run_id)
        if run is None:
            continue
        refused = await _run_refusal(session, principal, run)
        if refused is None:
            return
    if refused is not None:
        raise refused


async def check_targets(principal: Any, params: dict[str, Any]) -> None:
    """路径参数里的应用、运行、待办、工件这把密钥碰不碰得到（不存在的放过去，接口照旧回 404）。"""
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        if app_id := params.get("app_id"):
            await _check_app(session, principal, str(app_id))
        if run_id := params.get("run_id"):
            await _check_run(session, principal, str(run_id))
        if task_id := params.get("task_id"):
            await _check_task(session, principal, str(task_id))
        if artifact_id := params.get("artifact_id"):
            await _check_artifact(session, principal, str(artifact_id))


def route_of(conn: HTTPConnection) -> tuple[str, str | None]:
    route = conn.scope.get("route")
    method = "WS" if conn.scope["type"] == "websocket" else str(conn.scope.get("method", "GET")).upper()
    return method, getattr(route, "path", None)


async def key_gate(conn: HTTPConnection) -> None:
    """全站的路由级依赖（main.py 的 FastAPI(dependencies=…)，在各组路由自己的依赖之前跑）：只管按访问密钥认出来的请求，
    别的认法一律放行。按 (方法, 路由模板) 查 KEY_ROUTES，再核 scope 和路径里的应用、运行、待办、工件。"""
    from app.api.deps import current_principal

    principal = current_principal()
    if principal.via != "key":
        return
    entry = KEY_ROUTES.get(route_of(conn))
    if entry is None:
        raise _forbidden(ROUTE_FORBIDDEN_TEXT, KEY_ROUTE_FORBIDDEN)
    if entry.scope not in principal.scopes:
        raise _forbidden(f"这把访问密钥没有「{entry.scope}」权限，调不了这个接口：请换一把带这项权限的密钥",
                         KEY_SCOPE_MISSING)
    await check_targets(principal, dict(conn.path_params or {}))


# ---------------------------------------------------------------- 写进运行来源、待办事件的那几项


def trace(principal: Any) -> dict[str, Any]:
    """按密钥发起、回复时记进 run.origin、task.responded 的：经访问密钥、哪一把、叫什么。别的认法交回空的（不改原来记的样子）。"""
    if getattr(principal, "via", None) != "key":
        return {}
    return {"via": "key", "key_id": principal.key_id, "key_name": principal.key_name}


# ---------------------------------------------------------------- 幂等（A4）


def _book() -> Any:
    from app.db.models_apps import ApiReceipt
    from app.process import receipts

    return receipts.Book(ApiReceipt, "owner_user_id")


def _fingerprint(route: str, request: Any) -> str:
    from app.core.artifact_store import canonical_json

    return hashlib.sha256(canonical_json({"route": route, "request": request}).encode()).hexdigest()


def idempotency_of(raw: str | None) -> str | None:
    """请求头里的幂等键规整成存的样子（去首尾空白、截到 KEY_MAX 字）；空的是 None。"""
    from app.process import receipts

    return (raw or "").strip()[:receipts.KEY_MAX] or None


async def once(principal: Any, idem_key: str | None, *, route: str, request: Any,
               run: Callable[[bool], Awaitable[dict[str, Any]]],
               replay: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """用密钥办一件要去重的事（发起实例、刷新视图）。没带键 400；同一个键办成过的交回上次的结果（replay，duplicate: true）；
    正在办的 409；用来办过另一件事的 422。run(reclaimed) 真正去办，交回带 run_id 的结果；reclaimed 为真是上一次办到一半
    服务停了，run 要先找已经办成的那一件。run 抛出的错原样往上抛，记录记成被拒（同一个键可以重试）。"""
    from app.process import receipts

    key = idempotency_of(idem_key)
    if not key:
        raise CodedHTTPException(400, f"用访问密钥发起时要带 {IDEMPOTENCY_HEADER} 请求头：同一件事重发时要带同一个键，"
                                      "才不会多发起一次", KEY_IDEMPOTENCY_REQUIRED)
    book = _book()
    owner = str(principal.user_id)
    try:
        previous, reclaimed = await book.claim(owner, key, fingerprint=_fingerprint(route, request),
                                               extra={"key_id": principal.key_id, "route": route[:300]})
    except receipts.ReceiptBusy:
        raise CodedHTTPException(409, "同一个幂等键的请求正在处理，请稍后再查", KEY_IDEMPOTENCY_BUSY) from None
    except receipts.ReceiptMismatch:
        raise CodedHTTPException(422, "这个幂等键已经用来办过另一件事（别的接口或别的内容）：新的一件事请换一个键",
                                 KEY_IDEMPOTENCY_REUSED) from None
    if previous is not None:
        note_run(previous.get("run_id"))
        return await replay(previous)
    try:
        out = await run(reclaimed)
    except Exception as e:
        # 被拒（表单不对、发起不了）：记成 rejected，同一个键可以重试。请求被取消（客户端断开）不在这里收：记录停在
        # processing，过了 STALE_S 同一个键能重新认领（同钩子）
        await book.finish(owner, key, status="rejected", detail=(str(e) or type(e).__name__)[:500])
        raise
    note_run(out.get("run_id"))
    await book.finish(owner, key, status="accepted", run_id=out.get("run_id"), task_id=out.get("task_id"))
    return out


async def started_before(principal: Any, idem_key: str) -> str | None:
    """拥有者按这个幂等键用密钥发起过的实例（来源里记着 idempotency_key）：处理到一半服务停了、同一个键重新认领时交回它。"""
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.db.portable import json_eq

    async with SessionLocal() as session:
        return await session.scalar(select(Run.id).where(
            Run.origin_kind == "app_entry", Run.started_by == principal.username,
            json_eq(Run.origin, "idempotency_key", idem_key),
            json_eq(Run.origin, "via", "key")).order_by(Run.created_at.desc()).limit(1))


# ---------------------------------------------------------------- 请求记录（A6）

#: 这一次请求的记录（中间件按请求放一份，接口里 note_run 往里写涉及的运行）
_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("agentlab_key_request", default=None)
#: 还没写进库的请求记录
_PENDING: list[dict[str, Any]] = []
_flushing: asyncio.Task[Any] | None = None
_last_purge = 0.0


def begin_request() -> tuple[dict[str, Any], Any]:
    record: dict[str, Any] = {}
    return record, _CURRENT.set(record)


def end_request(token: Any) -> None:
    _CURRENT.reset(token)


def note_run(run_id: Any) -> None:
    """接口里交出运行 id 时调（发起实例、刷新视图）：请求记录记下涉及哪次运行。不在密钥请求里时什么都不做。"""
    record = _CURRENT.get()
    if record is not None and run_id:
        record["run_id"] = str(run_id)


def note(key_id: str, *, method: str, route: str, status: int, run_id: str | None, ip: str | None) -> None:
    """记一次请求（先进缓冲）。攒满 FLUSH_AT 条就起一个后台任务写库，不等调度循环。"""
    global _flushing
    from app.db.base import utcnow

    _PENDING.append({"key_id": key_id, "at": utcnow(), "method": method[:10], "route": (route or "")[:300],
                     "status": int(status or 0), "run_id": (run_id or None) and str(run_id)[:32],
                     "ip": (ip or "")[:64] or None})
    if len(_PENDING) > PENDING_MAX:
        dropped = len(_PENDING) - PENDING_MAX
        del _PENDING[:dropped]
        logger.warning("访问密钥的请求记录一直写不进库，丢掉了最早的 %d 条", dropped)
    if len(_PENDING) >= FLUSH_AT and (_flushing is None or _flushing.done()):
        try:
            _flushing = asyncio.get_running_loop().create_task(_flush_quietly(), name="agentlab-key-requests")
        except RuntimeError:
            pass  # 没有事件循环（不会发生在请求里）：等下一拍


async def _flush_quietly() -> None:
    try:
        await flush()
    except Exception:  # noqa: BLE001 - 没人 await 这个任务，异常只记日志
        logger.exception("访问密钥的请求记录写库失败，下一拍再试")


async def flush() -> int:
    """把缓冲里的请求记录批量写进库，交回写了几条。写失败的放回缓冲（下一拍再试），不丢。"""
    from app.db.base import SessionLocal
    from app.db.models_identity import AccessKeyRequest

    if not _PENDING:
        return 0
    batch = list(_PENDING)
    del _PENDING[:len(batch)]
    try:
        async with SessionLocal() as session:
            session.add_all(AccessKeyRequest(**item) for item in batch)
            await session.commit()
    except BaseException:
        _PENDING[:0] = batch
        raise
    return len(batch)


async def purge(now: datetime | None = None) -> int:
    """清掉超过保留期的请求记录，交回清了几条。"""
    from app.db.base import SessionLocal, utcnow
    from app.db.models_identity import AccessKeyRequest

    floor = (now or utcnow()) - RETAIN
    async with SessionLocal() as session:
        done = await session.execute(delete(AccessKeyRequest).where(AccessKeyRequest.at < floor))
        await session.commit()
    return int(done.rowcount or 0)


async def tick(now: datetime | None = None) -> list[str]:
    """调度循环每一拍（scheduler/loop.process_due）：写缓冲里的请求记录（交回写了的那几条的密钥 id，日志按条数记）；每小时
    清一次过了保留期的（给了 now 的测试每次都清）。"""
    global _last_purge
    written = list(_PENDING)
    count = await flush()
    moment = time.monotonic()
    if now is not None or moment - _last_purge >= _PURGE_EVERY_S:
        _last_purge = moment
        if purged := await purge(now):
            logger.info("清掉 %d 条超过 %d 天的访问密钥请求记录", purged, RETAIN.days)
    return [item["key_id"] for item in written[:count]]


async def recent_requests(session: Any, key_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """这把密钥最近的请求（新的在前）。先把缓冲写进库：刚发的请求在界面上马上查得到。"""
    from app.db.models_identity import AccessKeyRequest

    try:
        await flush()
    except Exception:  # noqa: BLE001 - 写不进去只是少几条最新的，照常给库里有的
        logger.exception("访问密钥的请求记录写库失败")
    rows = (await session.execute(select(AccessKeyRequest).where(AccessKeyRequest.key_id == key_id)
                                  .order_by(AccessKeyRequest.at.desc()).limit(max(1, min(limit, 200))))).scalars()
    return [{"at": r.at.isoformat() if r.at else None, "method": r.method, "route": r.route, "status": r.status,
             "run_id": r.run_id, "ip": r.ip} for r in rows]


# ---------------------------------------------------------------- 接口说明（A7）


def reference() -> dict[str, Any]:
    """从 KEY_ROUTES 生成的接口说明：方法、路径、scope、说明、可能的错误码、要不要带幂等键。通用的错误码另列。"""
    from app.core.public_url import public_base_url

    routes = [{"method": method, "path": path, "scope": entry.scope, "summary": entry.summary,
               "errors": list(dict.fromkeys(entry.errors)), "idempotent": entry.idempotent}
              for (method, path), entry in KEY_ROUTES.items()]
    return {"base_url": public_base_url(), "header": "Authorization: Bearer agl_…",
            "idempotency_header": IDEMPOTENCY_HEADER, "scopes": [*SCOPES], "implicit_scope": ME,
            "personal_scopes": [*PERSONAL_SCOPES], "rate_per_minute": RATE_PER_MINUTE,
            "common_errors": [*COMMON_ERRORS], "routes": routes}
