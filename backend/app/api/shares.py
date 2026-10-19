"""分享链接（docs/design/next-round/04-对外集成与触达.md C2，集成-I6）：拿到链接、不用登录就能看应用的一页（只读，可以嵌进门户）。

为什么要有：会话 cookie 是 SameSite=Lax，跨站的 iframe 里带不上——想把一块看板放进别的门户，只能让每个看的人都开账号，
而且门户和 AgentLab 必须同站（同站门户嵌整站见 deploy/agentlab.env 的 AGENTLAB_FRAME_ANCESTORS，C3）。分享链接是跨站嵌入
唯一走得通的办法，所以做；但它等于把数据交出了授权范围，所以每一步都往窄处取：
- 管理员开关 apps.share_links，缺省关；关掉时已有的链接一并打不开（403 share_links_off），撤销照常能做；
- 只有应用的 manage 权限能建、能撤销；个人应用（personal.not_shareable）、没发布的、归档了的不能分享；
- 到期必填，最长 EXPIRES_MAX_DAYS 天；撤销、到期之后打开回 410，写明是撤销了还是到期了、什么时候；
- 只读：分享页只解析定时出数（latest_sealed）的视图，按需视图、实例、待办、发起按钮、记录列表换成写明原因的说明，从不
  发起运行（apps/resolve.py 的 share 旁路）；看到的就是使用者看到的那份遮罩视图（视图对所有人都遮）；
- 出处不走在线的证据接口（那等于给匿名的人一个按运行读证据的身份）：分享页给「下载报告」，GET /api/shares/report.html 只认
  这条分享此刻解析出来的那几次运行，内部用的就是报告导出 export_report_html（同样遮过，文件自己离线点得开出处）；
- key 放在链接的 # 后面（/s#<key>）：打开页面时不发给服务器、不进代理日志和 Referer；读的接口只认请求头 X-AgentLab-Share，
  地址里带的一概不认。库里只存 key 的 sha256 和开头一截；
- 建、撤销写审计（app.share_create / app.share_revoke）；打开只计数（open_count、last_opened_at）。

接口：
- POST / GET /api/apps/{app_id}/shares、DELETE /api/apps/{app_id}/shares/{share_id}：登录即可进来，接口里要应用的 manage；
- GET /api/shares/page、GET /api/shares/report.html：公开路由（中间件把 /api/shares/ 当公开前缀，不用登录、不看会话），
  只认分享 key。

都不放给访问密钥（不在 auth/keys.KEY_ROUTES 里，门禁一律 403）：分享是给人看的页面，集成要拉数走视图数据接口。
安全头：分享页 /s 用嵌入白名单 AGENTLAB_EMBED_ORIGINS 和 Referrer-Policy: no-referrer（web_security.py）；这一条分享没有允许
嵌入（embed_allowed 为假）时，页面在别的页面的 iframe 里不显示内容（服务端在 /s 上只能按全站的白名单发头：那一刻还不知道是
哪一条分享，key 不会发到服务器）。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import Principal, actor_of, require_role
from app.db.base import get_session, utcnow
from app.db.models_apps import App, AppVersion, ViewShare

router = APIRouter(tags=["shares"])
_manage = APIRouter(prefix="/api/apps")
_public = APIRouter(prefix="/api/shares")

#: 读接口认的请求头
SHARE_HEADER = "X-AgentLab-Share"
#: 到期最长多少天
EXPIRES_MAX_DAYS = 90
#: key 的随机字节数、库里存的开头几位
KEY_BYTES = 32
PREFIX_LEN = 6
#: 分享页的地址（key 接在 # 后面）
SHARE_PATH = "/s"
SHARE_FORMAT = "agentlab.share/1"

SHARE_LINKS_OFF = "share_links_off"
SHARE_KEY_MISSING = "share_key_missing"
SHARE_NOT_FOUND = "share_not_found"
SHARE_REVOKED = "share_revoked"
SHARE_EXPIRED = "share_expired"
SHARE_GONE = "share_gone"
SHARE_EXPIRES_INVALID = "share_expires_invalid"
SHARE_NO_SEALED_VIEW = "share_no_sealed_view"
SHARE_RUN_NOT_SHARED = "share_run_not_shared"
SHARE_ID_NOT_FOUND = "share_id_not_found"

LINKS_OFF_CREATE = "管理员没有打开分享链接：请管理员在「设置 → 偏好设置」的「应用」一节里打开「允许建分享链接」"
LINKS_OFF_OPEN = "管理员已关闭分享链接，这条链接暂时打不开"


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _when(moment: datetime | None) -> str:
    if moment is None:
        return ""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone().strftime("%Y-%m-%d %H:%M")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def state_of(row: ViewShare, now: datetime | None = None) -> str:
    """active 能打开；revoked 撤销了；expired 过了到期时间。"""
    if row.revoked_at is not None:
        return "revoked"
    if _aware(row.expires_at) <= (now or utcnow()):
        return "expired"
    return "active"


def share_out(row: ViewShare, titles: dict[str, str] | None = None) -> dict[str, Any]:
    """一条分享的接口形状（typesApp.ts 的 ViewShareRow）：key 不回（只在建的时候给一次），给开头一截认是哪一条。"""
    iso = lambda m: _aware(m).isoformat() if m else None  # noqa: E731
    return {"id": row.id, "prefix": row.prefix, "app_id": row.app_id, "page_id": row.page_id,
            "page_title": (titles or {}).get(row.page_id) or row.page_id, "mode": row.mode,
            "app_version": row.app_version, "embed_allowed": row.embed_allowed, "created_by": row.created_by,
            "created_by_name": row.created_by_name, "created_at": iso(row.created_at),
            "expires_at": iso(row.expires_at), "revoked_at": iso(row.revoked_at), "revoked_by": row.revoked_by,
            "open_count": row.open_count, "last_opened_at": iso(row.last_opened_at), "state": state_of(row)}


async def _enabled(session: AsyncSession) -> bool:
    from app.api.settings import apps_settings

    return bool((await apps_settings(session)).get("share_links"))


def _titles(spec: dict[str, Any] | None) -> dict[str, str]:
    return {str(p.get("id")): str(p.get("title") or p.get("id")) for p in (spec or {}).get("pages") or []
            if isinstance(p, dict) and p.get("id")}


# --------------------------------------------------------------------------
# 建、列、撤销（要应用的 manage）
# --------------------------------------------------------------------------


class ShareIn(BaseModel):
    page_id: str
    mode: Literal["latest", "fixed"] = "latest"
    #: 到期时间，必填（不给回 422 share_expires_invalid）；不带时区的按 UTC
    expires_at: datetime | None = None
    embed_allowed: bool = False


async def _managed(session: AsyncSession, app_id: str, x_actor: str | None) -> tuple[App, Principal]:
    from app.api.apps import _app, _principal

    principal = _principal(x_actor)
    app, _ = await _app(session, app_id, principal, "manage")
    return app, principal


@_manage.post("/{app_id}/shares", status_code=201)
async def create_share(app_id: str, payload: ShareIn, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """建一条分享链接：要应用的 manage、管理员开了 apps.share_links。key 只在这里交回一次（path 是 /s#<key>，url 是拼上
    对外访问地址的完整链接，没设对外访问地址时为 null，界面按当前页面的地址拼）。"""
    from app.api.apps import _current_version, _refuse_archived, _refuse_personal
    from app.apps.audit import record
    from app.apps.resolve import SharedView, share_runs, sealed_views
    from app.apps.tree import page_of
    from app.core import public_url

    app, principal = await _managed(session, app_id, x_actor)
    if not await _enabled(session):
        raise CodedHTTPException(403, LINKS_OFF_CREATE, SHARE_LINKS_OFF)
    _refuse_personal(app, "建分享链接")
    _refuse_archived(app, "建分享链接")
    version = await _current_version(session, app)
    now = utcnow()
    if payload.expires_at is None:
        raise CodedHTTPException(422, f"分享链接必须设到期时间，最长 {EXPIRES_MAX_DAYS} 天", SHARE_EXPIRES_INVALID,
                                 field="expires_at")
    expires = _aware(payload.expires_at)
    if expires <= now + timedelta(minutes=1):
        raise CodedHTTPException(422, "到期时间要在现在之后", SHARE_EXPIRES_INVALID, field="expires_at")
    # 留一小时的余量：界面按本机时间算「90 天后」，本机比服务端快一点不该被拒
    if expires > now + timedelta(days=EXPIRES_MAX_DAYS, hours=1):
        raise CodedHTTPException(422, f"分享链接最长 {EXPIRES_MAX_DAYS} 天到期，请选更早的到期时间",
                                 SHARE_EXPIRES_INVALID, field="expires_at")
    spec = version.spec or {}
    if page_of(spec, payload.page_id) is None:
        raise CodedHTTPException(404, f"应用的当前版本里没有页面「{payload.page_id}」", "app_page_not_found")
    if not sealed_views(spec, payload.page_id):
        raise CodedHTTPException(409, "这一页没有定时出数的视图（取最近一次封存运行的视图）：分享链接只显示这种视图，"
                                      "分享出去什么都看不到", SHARE_NO_SEALED_VIEW)
    runs: dict[str, str | None] | None = None
    if payload.mode == "fixed":
        found = await share_runs(session, spec, payload.page_id, SharedView(mode="latest", key=""))
        runs = {vid: (run.id if run is not None else None) for vid, run in found.items()}
        if not any(runs.values()):
            raise CodedHTTPException(409, "这一页定时出数的视图都还没有封存的运行，没有可以固定的数：先用「始终最新」，"
                                          "或者等第一次出数之后再建", SHARE_NO_SEALED_VIEW)
    key = secrets.token_urlsafe(KEY_BYTES)
    row = ViewShare(key_hash=key_hash(key), prefix=key[:PREFIX_LEN], app_id=app.id, page_id=payload.page_id,
                    mode=payload.mode, app_version=version.version, runs=runs, embed_allowed=payload.embed_allowed,
                    created_by=actor_of(x_actor), created_by_name=principal.display_name or actor_of(x_actor),
                    created_at=now, expires_at=expires)
    session.add(row)
    await session.flush()
    await record(session, action="app.share_create", subject_kind="view_share", subject_id=row.id,
                 actor=actor_of(x_actor), data={"app_id": app.id, "page_id": payload.page_id, "mode": payload.mode,
                                                "app_version": version.version, "expires_at": expires.isoformat(),
                                                "embed_allowed": payload.embed_allowed, "prefix": row.prefix})
    await session.commit()
    path = f"{SHARE_PATH}#{key}"
    base = public_url.public_base_url()
    return {**share_out(row, _titles(spec)), "key": key, "path": path, "url": f"{base}{path}" if base else None}


@_manage.get("/{app_id}/shares")
async def list_shares(app_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """这个应用的分享链接（新的在前）。enabled 是管理员开没开（关着时界面不给新建，已有的照列、能撤销）；embed_origins 是部署
    配没配允许嵌入分享页的站点（AGENTLAB_EMBED_ORIGINS）；max_days 是到期最长几天。"""
    from app.api.apps import _previous
    from app.core.config import settings

    app, _ = await _managed(session, app_id, x_actor)
    rows = list((await session.execute(select(ViewShare).where(ViewShare.app_id == app.id)
                                       .order_by(ViewShare.created_at.desc(), ViewShare.id.desc()))).scalars())
    version = await _previous(session, app)
    titles = _titles(version.spec if version is not None else None)
    return {"enabled": await _enabled(session), "embed_origins": bool(settings.embed_origins),
            "max_days": EXPIRES_MAX_DAYS, "items": [share_out(r, titles) for r in rows]}


@_manage.delete("/{app_id}/shares/{share_id}")
async def revoke_share(app_id: str, share_id: str, session: AsyncSession = Depends(get_session),
                       x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """撤销：之后打开回 410「链接已撤销」。撤销过的再撤销照旧交回它（不重复记审计）。管理员关了开关也能撤销。交回的形状和
    建、列的同一个（page_title 是页面标题，取法同列表：应用当前的版本）。"""
    from app.api.apps import _previous
    from app.apps.audit import record

    app, _ = await _managed(session, app_id, x_actor)
    row = await session.get(ViewShare, share_id)
    if row is None or row.app_id != app.id:
        raise CodedHTTPException(404, "分享链接不存在，可能已被删除", SHARE_ID_NOT_FOUND)
    if row.revoked_at is None:
        row.revoked_at, row.revoked_by = utcnow(), actor_of(x_actor)
        await record(session, action="app.share_revoke", subject_kind="view_share", subject_id=row.id,
                     actor=actor_of(x_actor), data={"app_id": app.id, "page_id": row.page_id, "prefix": row.prefix})
        await session.commit()
    version = await _previous(session, app)
    return share_out(row, _titles(version.spec if version is not None else None))


# --------------------------------------------------------------------------
# 打开（公开，只认请求头里的分享 key）
# --------------------------------------------------------------------------


def _share_principal(row: ViewShare) -> Principal:
    """分享身份：不是任何一个人。按使用者算（role=user），用户名为空：审计里不冒充谁，遮罩和使用者看到的一样。"""
    from app.auth import current_mode

    return Principal(username=None, role="user", display_name=f"分享链接 {row.prefix}", verified=False,
                     mode=current_mode(), via="share")


async def _opened(session: AsyncSession, key: str | None) -> tuple[ViewShare, App, AppVersion]:
    """按请求头里的 key 认出分享，核对开关、撤销、到期、应用还在不在，交回 (分享, 应用, 要解析的应用版本)。"""
    from app.api.apps import _previous

    if not key or not key.strip():
        raise CodedHTTPException(400, "缺少分享 key：请用完整的分享链接打开（链接里 # 后面的那一段）", SHARE_KEY_MISSING)
    row = (await session.execute(select(ViewShare).where(ViewShare.key_hash == key_hash(key.strip())))).scalar_one_or_none()
    if row is None:
        raise CodedHTTPException(404, "链接无效：可能复制得不完整，请向分享的人确认链接", SHARE_NOT_FOUND)
    if not await _enabled(session):
        raise CodedHTTPException(403, LINKS_OFF_OPEN, SHARE_LINKS_OFF)
    state = state_of(row)
    if state == "revoked":
        raise CodedHTTPException(410, f"链接已撤销（{_when(row.revoked_at)}）：请向分享的人要新的链接", SHARE_REVOKED)
    if state == "expired":
        raise CodedHTTPException(410, f"链接已到期（{_when(row.expires_at)}）：请向分享的人要新的链接", SHARE_EXPIRED)
    app = await session.get(App, row.app_id)
    if app is None or app.status == "archived" or getattr(app, "scope", "shared") == "personal" \
            or not app.published_version:
        raise CodedHTTPException(410, "分享的应用已归档或不再发布，这条链接打不开了", SHARE_GONE)
    version = None
    if row.mode == "fixed" and row.app_version:
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == row.app_version))).scalar_one_or_none()
    version = version or await _previous(session, app)
    if version is None:
        raise CodedHTTPException(410, "分享的应用已归档或不再发布，这条链接打不开了", SHARE_GONE)
    return row, app, version


def _shared(row: ViewShare) -> Any:
    from app.apps.resolve import SharedView

    return SharedView(mode=row.mode, key=row.id, runs=dict(row.runs or {}) if row.mode == "fixed" else None)


@_public.get("/page")
async def share_page(session: AsyncSession = Depends(get_session),
                     share_key: str | None = Header(default=None, alias=SHARE_HEADER)) -> dict[str, Any]:
    """分享页（agentlab.share/1）：{format, share: {页脚要的：谁分享、什么时候、什么时候到期、能不能嵌入、模式}, view:
    agentlab.view/1（share 旁路解析）, downloads: [{view, run_id, reports, as_of_text}]（下载报告用）}。打开一次计一次数。"""
    from app.apps.resolve import ViewRefused, resolve_page

    row, app, version = await _opened(session, share_key)
    try:
        view = await resolve_page(session, app, page_id=row.page_id, principal=_share_principal(row), role=None,
                                  version=version, share=_shared(row))
    except ViewRefused as e:
        if e.code == "app_page_not_found":
            raise CodedHTTPException(410, "分享的页面在应用的当前版本里已经没有了，这条链接打不开了", SHARE_GONE) from e
        raise CodedHTTPException(e.status_code, e.message, e.code or SHARE_GONE) from e
    await session.execute(update(ViewShare).where(ViewShare.id == row.id).values(
        open_count=ViewShare.open_count + 1, last_opened_at=utcnow()))
    await session.commit()
    downloads = (view.pop("share", None) or {}).get("runs") or []
    return {"format": SHARE_FORMAT,
            "share": {"mode": row.mode, "created_by": row.created_by_name or row.created_by,
                      "created_at": _aware(row.created_at).isoformat(), "expires_at": _aware(row.expires_at).isoformat(),
                      "embed_allowed": row.embed_allowed},
            "view": view, "downloads": downloads}


@_public.get("/report.html")
async def share_report(run: str = Query(...), report: str | None = None, session: AsyncSession = Depends(get_session),
                       share_key: str | None = Header(default=None, alias=SHARE_HEADER)) -> Any:
    """下载报告：只认这条分享此刻解析出来的那几次运行（apps/resolve.share_runs），别的运行 id 一律 404。内部就是报告导出
    （api/evidence.export_report_html：同样遮过，报告文件自己离线点得开出处；导出照常记 report.exported 审计，导出人记成
    分享身份——用户名为空，不冒充谁）。"""
    from app.api.deps import reset_principal, set_principal
    from app.api.evidence import export_report_html
    from app.apps.resolve import share_runs

    row, _, version = await _opened(session, share_key)
    found = await share_runs(session, version.spec or {}, row.page_id, _shared(row))
    if run not in {r.id for r in found.values() if r is not None}:
        raise CodedHTTPException(404, "这次运行不在这条分享链接里", SHARE_RUN_NOT_SHARED)
    token = set_principal(_share_principal(row))
    try:
        return await export_report_html(run, report)
    finally:
        reset_principal(token)


router.include_router(_manage, dependencies=[require_role("user")])
router.include_router(_public)
