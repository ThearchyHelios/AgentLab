"""身份接口（BE-1）：/me、登录、登出、OIDC、登录方式设置、引导第一个管理员、JWKS。设计文档 §2.7、§3.10。

登录方式三种：off（默认）/ local / oidc，环境变量 AGENTLAB_AUTH_MODE 优先于这里的设置（env_locked）。
- 从 off（或 oidc）切到 local：库里还没有能用口令登录的管理员时，必须在同一个请求里建出第一个管理员（bootstrap），
  建好就以他的身份登录——不然切过去的那一刻谁都进不来；
- 切到 oidc：至少配一个管理员映射（管理员组或管理员用户名），并且身份提供方的配置读得到；
- 环境变量指定了 local、库里一个管理员都没有：POST /bootstrap 建第一个；
- 都进不去了：服务器上执行 python -m app.auth reset-admin。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import AUTH_REQUIRED
from app.api.deps import Principal, current_principal, decode_actor, ensure_role
from app.auth import (
    MODES, SETTING_KEY, current_mode, env_mode, load_settings, remember_settings, stored_settings, user_can_chat,
)
from app.auth import oidc, sessions
from app.auth.audit import record
from app.auth.passwords import hash_password, password_problem, verify_password
from app.core import crypto
from app.db.base import get_session
from app.db.models import Setting, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_OIDC_FIELDS = ("issuer", "client_id", "client_secret", "scopes", "username_claim", "groups_claim",
                "admin_groups", "admin_users", "builder_groups")


def principal_out(p: Principal) -> dict[str, Any]:
    return {"mode": p.mode, "username": p.username, "display_name": p.display_name or p.username,
            "role": p.role or "user", "groups": list(p.groups), "verified": p.verified,
            "user_can_chat": user_can_chat()}


async def principal_full(session: AsyncSession, p: Principal) -> dict[str, Any]:
    """认人接口、登录交回的那一份：principal_out 再加上管理员开没开「使用者可以建个人应用」（同 user_can_chat，使用者
    读不到设置，新建应用的入口、个人蓝图页按它给）。"""
    from app.blueprints.personal import enabled

    return {**principal_out(p), "user_personal_apps": await enabled(session)}


@router.get("/me")
async def me(x_actor: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> Any:
    """当前是谁。关登录：mode=off、role=admin、username 是自报的署名。开登录没登录：401，带 mode（登录页据此显示
    账号密码还是单点登录）和 can_bootstrap：账号密码登录、库里还没有能登录的管理员时为真（环境变量直接指定了 local
    的新部署），登录页据此给「建第一个管理员」——条件和 POST /bootstrap 同一个，不多说别的。"""
    principal = current_principal()
    if principal.mode == "off":
        actor = decode_actor(x_actor)
        return await principal_full(session, Principal(username=actor, display_name=actor, role="admin", mode="off"))
    if not principal.username:
        can_bootstrap = principal.mode == "local" and not await _local_admin_exists(session)
        return JSONResponse(status_code=401, content={"detail": "请先登录", "code": AUTH_REQUIRED,
                                                      "mode": principal.mode, "can_bootstrap": can_bootstrap})
    return await principal_full(session, principal)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _login_response(request: Request, response: Response, token: str) -> None:
    response.set_cookie(sessions.COOKIE, token, **sessions.cookie_kwargs(sessions.is_secure(request.url.scheme)))


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
async def login(payload: LoginIn, request: Request, response: Response,
                session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """账号密码登录。单点登录模式下也认有口令的本地账号（reset-admin 建的找回账号）。"""
    mode = current_mode()
    if mode == "off":
        raise HTTPException(409, "未开启登录，无需登录")
    keys = (f"u:{payload.username.strip().lower()}", f"ip:{_client_ip(request)}")
    if wait := sessions.wait_seconds(*keys):
        raise HTTPException(429, f"登录失败次数过多，请 {wait} 秒后再试")
    user = (await session.execute(select(User).where(User.username == payload.username.strip()))).scalar_one_or_none()
    if user is None or user.disabled or not verify_password(payload.password, user.password_hash):
        sessions.failed(*keys)
        raise HTTPException(401, "用户名或密码不正确")
    sessions.succeeded(*keys)
    token = await sessions.create_session(session, user, ip=_client_ip(request),
                                          user_agent=request.headers.get("user-agent"))
    await session.commit()
    _login_response(request, response, token)
    return await principal_full(session, await sessions.principal_of(session, user, mode))


@router.post("/logout", status_code=204)
async def logout(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    await sessions.revoke(session, request.cookies.get(sessions.COOKIE))
    await session.commit()
    out = Response(status_code=204)
    out.delete_cookie(sessions.COOKIE, path="/")
    return out


# ---------------------------------------------------------------- OIDC


def _redirect_uri(request: Request) -> str:
    """回调地址：对外访问地址（环境变量优先，其次设置，core/public_url.py），都没有按请求自己的地址拼。"""
    from app.core.public_url import public_base_url

    base = public_base_url() or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def _oidc_config() -> dict[str, Any]:
    config = dict(stored_settings().get("oidc") or {})
    if not config.get("issuer") or not config.get("client_id"):
        raise HTTPException(409, "单点登录尚未配置，请联系管理员")
    return config


@router.get("/oidc/start")
async def oidc_start(request: Request, next: str | None = None) -> Response:  # noqa: A002 - 查询参数就叫 next
    if current_mode() != "oidc":
        raise HTTPException(409, "未开启单点登录")
    try:
        url, cookie = await oidc.start(_oidc_config(), _redirect_uri(request), next)
    except oidc.OidcError as e:
        raise HTTPException(502, str(e)) from e
    out = RedirectResponse(url, status_code=302)
    out.set_cookie(oidc.STATE_COOKIE, cookie, max_age=oidc.STATE_TTL_S, httponly=True, samesite="lax",
                   secure=sessions.is_secure(request.url.scheme), path=oidc.STATE_PATH)
    return out


@router.get("/oidc/callback")
async def oidc_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, session: AsyncSession = Depends(get_session)) -> Response:
    """身份提供方跳回来。state cookie 当场作废：同一次授权回放第二次时 cookie 已经没了。出错跳回登录页并带上原因。"""
    from urllib.parse import quote

    cookie = request.cookies.get(oidc.STATE_COOKIE)

    def back(target: str) -> RedirectResponse:
        out = RedirectResponse(target, status_code=302)
        out.delete_cookie(oidc.STATE_COOKIE, path=oidc.STATE_PATH)
        return out

    try:
        if current_mode() != "oidc":
            raise oidc.OidcError("未开启单点登录")
        if error:
            raise oidc.OidcError(f"身份提供方未完成登录：{error}")
        data = oidc.read_state(cookie, state)
        if not code:
            raise oidc.OidcError("身份提供方没有给出授权码")
        config = _oidc_config()
        claims = await oidc.exchange(config, code, _redirect_uri(request), data["verifier"], data["nonce"])
        user = await oidc.upsert_user(session, claims, config)
        token = await sessions.create_session(session, user, ip=_client_ip(request),
                                              user_agent=request.headers.get("user-agent"))
        await session.commit()
    except (oidc.OidcError, HTTPException) as e:
        await session.rollback()
        reason = e.detail if isinstance(e, HTTPException) else str(e)
        return back(f"/login?error={quote(str(reason))}")
    out = back(oidc.safe_next(data.get("next")))
    _login_response(request, out, token)
    return out


@router.get("/jwks")
async def jwks() -> dict[str, Any]:
    """service_on_behalf 身份断言的公钥（EdDSA）。外部系统据此核验 X-AgentLab-Assertion。"""
    from app.auth.signing import jwks as keys

    return keys()


# ---------------------------------------------------------------- 设置与引导


def _settings_out(value: dict[str, Any]) -> dict[str, Any]:
    config = {k: v for k, v in (value.get("oidc") or {}).items() if k in _OIDC_FIELDS}
    if config.get("client_secret"):
        config["client_secret"] = crypto.mask(config["client_secret"])
    return {"mode": current_mode(), "env_locked": env_mode() is not None, "oidc": config,
            "user_can_chat": bool(value.get("user_can_chat"))}


def _require_admin() -> None:
    """改登录设置：关登录时谁都能改（所有人等同管理员）；开了登录只给管理员。"""
    if current_principal().mode != "off":
        ensure_role("admin")


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    _require_admin()
    return _settings_out(await load_settings())


class BootstrapIn(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[^\s/]+$")
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)


class SettingsIn(BaseModel):
    mode: str | None = None
    oidc: dict[str, Any] | None = None
    user_can_chat: bool | None = None
    bootstrap: BootstrapIn | None = None


async def _local_admin_exists(session: AsyncSession) -> bool:
    return (await session.execute(select(User.id).where(
        User.role == "admin", User.disabled.is_(False), User.password_hash.is_not(None)).limit(1))).first() is not None


async def _create_admin(session: AsyncSession, payload: BootstrapIn) -> User:
    if problem := password_problem(payload.password):
        raise HTTPException(422, problem)
    if (await session.execute(select(User.id).where(User.username == payload.username))).first():
        raise HTTPException(409, f"用户名「{payload.username}」已存在，请换一个，或用它的密码登录")
    user = User(username=payload.username, display_name=payload.display_name or payload.username, role="admin",
                password_hash=hash_password(payload.password), created_by=current_principal().username)
    session.add(user)
    await session.flush()
    await record(session, "user.create", "user", user.id, data={"username": user.username, "role": "admin",
                                                                 "bootstrap": True})
    return user


def _merge_oidc(given: dict[str, Any] | None, before: dict[str, Any]) -> dict[str, Any]:
    merged = dict(before)
    for key, value in (given or {}).items():
        if key not in _OIDC_FIELDS:
            continue
        if key == "client_secret":
            # 提交掩码值表示保持原值
            if value and before.get(key) and value == crypto.mask(before[key]):
                continue
            value = crypto.encrypt(str(value)) if value else None
        elif key in ("admin_groups", "admin_users", "builder_groups"):
            value = [str(v).strip() for v in (value or []) if str(v).strip()]
        elif isinstance(value, str):
            value = value.strip()
        merged[key] = value
    return merged


@router.put("/settings")
async def put_settings(payload: SettingsIn, request: Request, response: Response,
                       session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_admin()
    before = await load_settings(force=True)
    value = dict(before)
    target = payload.mode or before.get("mode") or "off"
    if target not in MODES:
        raise HTTPException(422, "登录方式只能是未开启、账号密码登录、单点登录之一")
    if payload.mode and env_mode() is not None and payload.mode != env_mode():
        raise HTTPException(409, "登录方式由服务端的环境变量指定，不能在这里修改")
    if payload.oidc is not None:
        value["oidc"] = _merge_oidc(payload.oidc, dict(before.get("oidc") or {}))
    if payload.user_can_chat is not None:
        value["user_can_chat"] = payload.user_can_chat
    value["mode"] = target

    created: User | None = None
    effective = env_mode() or target
    if effective == "local" and current_mode() != "local":
        if payload.bootstrap is not None:
            created = await _create_admin(session, payload.bootstrap)
        elif not await _local_admin_exists(session):
            raise HTTPException(422, "开启账号密码登录前，需要同时创建第一个管理员账号")
    if effective == "oidc":
        config = value.get("oidc") or {}
        if not config.get("issuer") or not config.get("client_id"):
            raise HTTPException(422, "单点登录需要填写身份提供方地址和客户端标识")
        if not (config.get("admin_groups") or config.get("admin_users")):
            raise HTTPException(422, "单点登录至少需要配置一个管理员映射（管理员组或管理员用户名），否则无人能管理系统")
        if current_mode() != "oidc" or payload.oidc is not None:
            try:
                await oidc.discover(config["issuer"])
            except oidc.OidcError as e:
                raise HTTPException(422, str(e)) from e

    row = await session.get(Setting, SETTING_KEY)
    if row is None:
        session.add(Setting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    changed = {k: value.get(k) for k in ("mode", "user_can_chat") if value.get(k) != before.get(k)}
    if payload.oidc is not None:
        changed["oidc"] = {k: v for k, v in (value.get("oidc") or {}).items() if k != "client_secret"}
    await record(session, "auth.settings", "auth", SETTING_KEY, data=changed)
    token = None
    if created is not None:
        token = await sessions.create_session(session, created, ip=_client_ip(request),
                                              user_agent=request.headers.get("user-agent"))
    await session.commit()
    remember_settings(value)
    out = _settings_out(value)
    if token is not None and created is not None:
        _login_response(request, response, token)
        out["principal"] = await principal_full(session, await sessions.principal_of(session, created, current_mode()))
    return out


@router.post("/bootstrap")
async def bootstrap(payload: BootstrapIn, request: Request, response: Response,
                    session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建第一个管理员并登录。只在账号密码登录模式下、库里还没有能登录的管理员时可用（环境变量直接指定了 local 的新部署）。"""
    if current_mode() != "local":
        raise HTTPException(409, "只有在账号密码登录模式下才能创建第一个管理员")
    if await _local_admin_exists(session):
        raise HTTPException(409, "已经有管理员了，请用管理员账号登录")
    user = await _create_admin(session, payload)
    token = await sessions.create_session(session, user, ip=_client_ip(request),
                                          user_agent=request.headers.get("user-agent"))
    await session.commit()
    _login_response(request, response, token)
    return await principal_full(session, await sessions.principal_of(session, user, "local"))
