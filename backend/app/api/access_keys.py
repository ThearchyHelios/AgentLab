"""访问密钥与服务账号（docs/design/next-round/04-对外集成与触达.md A1–A7）：建、撤销、轮换密钥，建、停用服务账号，个人
访问密钥的开关，每把密钥最近的请求，接口说明。签发、认人、门禁的规则在 app/auth/keys.py。

main.py 挂登录即可（require_role("user")），接口里再按本人 / 管理员判：
- 管理员看得到服务账号和全部密钥，能给服务账号建密钥、撤销和轮换任何一把；
- 每个人看得到自己的个人访问密钥；管理员打开开关（auth.personal_keys，缺省关）之后才能建；
- 关登录时访问密钥不生效：列表照给（空的，带 login_on=false），建密钥、建服务账号回 409（keys_need_login）。

建、撤销、轮换密钥，建、停用服务账号，开关个人密钥都写审计（同一个事务）。密钥明文只在建、轮换的回包里出现一次。
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import current_principal
from app.auth import keys
from app.auth.audit import record
from app.db.base import get_session, utcnow
from app.db.models_identity import AccessKey, User

router = APIRouter(prefix="/api/access-keys", tags=["access-keys"])

#: 错误码（403 / 404 / 409 / 422）
KEY_NOT_FOUND = "access_key_not_found"
KEY_FORBIDDEN = "access_key_forbidden"
KEY_INVALID_INPUT = "access_key_invalid"
KEY_NOT_ACTIVE = "access_key_not_active"
PERSONAL_KEYS_OFF = "personal_keys_off"
SERVICE_NOT_FOUND = "service_account_not_found"
SERVICE_INVALID = "service_account_invalid"
SERVICE_TAKEN = "service_account_taken"

_SERVICE_NAME = re.compile(r"^svc-[A-Za-z0-9._-]{1,96}$")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _need_login() -> None:
    if current_principal().mode == "off":
        raise CodedHTTPException(409, keys.KEYS_NEED_LOGIN_TEXT, keys.KEYS_NEED_LOGIN)


def _is_admin() -> bool:
    principal = current_principal()
    return principal.mode != "off" and principal.at_least("admin") and principal.via != "key"


def _require_admin() -> None:
    from app.api.deps import ensure_role

    ensure_role("admin")


def key_out(row: AccessKey, owner: User | None) -> dict[str, Any]:
    """接口里的一把密钥：没有明文、没有哈希，只有开头一截（prefix）给人认。"""
    return {"id": row.id, "name": row.name, "prefix": row.prefix, "scopes": [s for s in row.scopes or []],
            "app_ids": list(row.app_ids) if row.app_ids else None,
            "owner": {"id": row.owner_user_id, "username": owner.username if owner else None,
                      "display_name": (owner.display_name or owner.username) if owner else None,
                      "kind": owner.kind if owner else None},
            "personal": owner is None or owner.kind != keys.SERVICE_KIND,
            "state": keys.state_of(row, owner),
            "expires_at": _iso(row.expires_at), "revoked_at": _iso(row.revoked_at), "revoked_by": row.revoked_by,
            "last_used_at": _iso(row.last_used_at), "last_used_ip": row.last_used_ip,
            "rotated_from": row.rotated_from, "created_by": row.created_by, "created_at": _iso(row.created_at)}


def service_out(row: User, key_count: int = 0) -> dict[str, Any]:
    return {"id": row.id, "username": row.username, "display_name": row.display_name or row.username,
            "disabled": row.disabled, "created_by": row.created_by, "created_at": _iso(row.created_at),
            "keys": key_count}


async def _owners(session: AsyncSession, rows: list[AccessKey]) -> dict[str, User]:
    ids = {r.owner_user_id for r in rows}
    if not ids:
        return {}
    return {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).scalars()}


# ---------------------------------------------------------------- 开关与概况


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """这一页要的几样：开没开登录（关登录时密钥不生效）、个人访问密钥的开关、我能不能建个人密钥、是不是管理员。"""
    from app.auth import personal_keys_enabled

    principal = current_principal()
    login_on = principal.mode != "off"
    return {"login_on": login_on, "personal_keys": personal_keys_enabled(), "admin": _is_admin(),
            "can_create_personal": login_on and personal_keys_enabled() and bool(principal.user_id)
            and principal.via != "key",
            "scopes": [*keys.SCOPES], "read_scopes": [*keys.READ_SCOPES], "personal_scopes": [*keys.PERSONAL_SCOPES],
            "expires_default_days": keys.EXPIRES_DEFAULT_DAYS, "expires_max_days": keys.EXPIRES_MAX_DAYS}


class SettingsIn(BaseModel):
    personal_keys: bool


@router.put("/settings")
async def put_settings(payload: SettingsIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """个人访问密钥的开关（只给管理员）。存在登录设置那一行里（auth.personal_keys），和「使用者可以问数据」同一处缓存。
    关掉之后建不了个人密钥，已有的个人密钥也不认（401 key_personal_off），重新打开就又能用。"""
    from app.auth import PERSONAL_KEYS, SETTING_KEY, load_settings, remember_settings
    from app.db.models import Setting

    _need_login()
    _require_admin()
    before = await load_settings(force=True)
    value = {**before, PERSONAL_KEYS: payload.personal_keys}
    row = await session.get(Setting, SETTING_KEY)
    if row is None:
        session.add(Setting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    if bool(before.get(PERSONAL_KEYS)) != payload.personal_keys:
        await record(session, "auth.personal_keys", "auth", None,
                     data={"before": bool(before.get(PERSONAL_KEYS)), "after": payload.personal_keys})
    await session.commit()
    remember_settings(value)
    from app.cluster import caches

    await caches.broadcast("auth_settings")
    return await get_settings()


@router.get("/reference")
async def reference() -> dict[str, Any]:
    """接口说明（A7）：从门禁那张表（auth/keys.KEY_ROUTES）生成，方法、路径、scope、说明、可能的错误码。要登录（main.py 挂的
    require_role("user")），不对匿名的人敞开。"""
    return keys.reference()


# ---------------------------------------------------------------- 服务账号（只给管理员）


@router.get("/service-accounts")
async def list_service_accounts(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_admin()
    rows = list((await session.execute(select(User).where(User.kind == keys.SERVICE_KIND)
                                       .order_by(User.created_at))).scalars())
    counts: dict[str, int] = {}
    for owner_id in (await session.execute(select(AccessKey.owner_user_id).where(
            AccessKey.owner_user_id.in_([r.id for r in rows] or [""]), AccessKey.revoked_at.is_(None)))).scalars():
        counts[owner_id] = counts.get(owner_id, 0) + 1
    return {"items": [service_out(r, counts.get(r.id, 0)) for r in rows]}


class ServiceIn(BaseModel):
    username: str = Field(min_length=5, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)


@router.post("/service-accounts", status_code=201)
async def create_service_account(payload: ServiceIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建一个服务账号：用户名统一 svc- 开头，没有口令、不能登录，角色固定使用者。它就是一个用户——授权、组、名录全部沿用
    现有的机制（应用授权里能选它；岗位绑定、选人候选里排除它：服务账号不能被派待办）。"""
    _need_login()
    _require_admin()
    username = payload.username.strip()
    if not _SERVICE_NAME.match(username):
        raise CodedHTTPException(422, "服务账号的用户名要以 svc- 开头，后面只用字母、数字、点、下划线和短横线",
                                 SERVICE_INVALID, field="username")
    if (await session.execute(select(User.id).where(User.username == username))).first():
        raise CodedHTTPException(409, f"用户名「{username}」已存在", SERVICE_TAKEN, field="username")
    row = User(username=username, display_name=(payload.display_name or "").strip() or username, role="user",
               kind=keys.SERVICE_KIND, password_hash=None, created_by=current_principal().username)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        raise CodedHTTPException(409, f"用户名「{username}」已存在", SERVICE_TAKEN, field="username") from e
    await record(session, "service_account.create", "user", row.id, data={"username": row.username})
    await session.commit()
    return service_out(row)


class ServicePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    disabled: bool | None = None


async def _service(session: AsyncSession, user_id: str) -> User:
    row = await session.get(User, user_id)
    if row is None or row.kind != keys.SERVICE_KIND:
        raise CodedHTTPException(404, "服务账号不存在", SERVICE_NOT_FOUND)
    return row


@router.patch("/service-accounts/{user_id}")
async def update_service_account(user_id: str, payload: ServicePatch,
                                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """改显示名、停用 / 启用。停用之后它的密钥全部失效（401 key_owner_disabled），重新启用就又能用（没到期、没撤销的）。"""
    _need_login()
    _require_admin()
    row = await _service(session, user_id)
    changes: dict[str, Any] = {}
    if payload.display_name is not None and payload.display_name.strip() != row.display_name:
        changes["display_name"] = [row.display_name, payload.display_name.strip() or row.username]
        row.display_name = payload.display_name.strip() or row.username
    if payload.disabled is not None and payload.disabled != row.disabled:
        changes["disabled"] = [row.disabled, payload.disabled]
        row.disabled = payload.disabled
    if changes:
        action = ("service_account.disable" if payload.disabled else "service_account.enable") \
            if "disabled" in changes else "service_account.update"
        await record(session, action, "user", row.id, data={"username": row.username, **changes})
    await session.commit()
    return service_out(row)


# ---------------------------------------------------------------- 密钥


@router.get("")
async def list_keys(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """管理员：全部密钥（服务账号的和每个人的个人密钥）；别人：自己的个人密钥。关登录时是空的。"""
    principal = current_principal()
    if principal.mode == "off" or not principal.user_id:
        return {"items": []}
    query = select(AccessKey).order_by(AccessKey.created_at.desc())
    if not _is_admin():
        query = query.where(AccessKey.owner_user_id == principal.user_id)
    rows = list((await session.execute(query)).scalars())
    owners = await _owners(session, rows)
    return {"items": [key_out(r, owners.get(r.owner_user_id)) for r in rows]}


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=keys.NAME_MAX)
    scopes: list[str] = Field(default_factory=lambda: list(keys.READ_SCOPES))
    #: 不给（或空）是不限应用
    app_ids: list[str] | None = None
    expires_in_days: int = Field(default=keys.EXPIRES_DEFAULT_DAYS, ge=1, le=keys.EXPIRES_MAX_DAYS)
    #: 给服务账号建（只给管理员）；不给是建自己的个人访问密钥
    service_account_id: str | None = None


async def _check_input(session: AsyncSession, scopes: list[str], app_ids: list[str] | None, *,
                       service: bool) -> tuple[list[str], list[str] | None]:
    """scope 只能是认得的那几个（至少一个）；服务账号不能勾只给个人密钥的（回复待办）。限定的应用要是在的、共享的应用。"""
    from app.db.models_apps import App

    picked = list(dict.fromkeys(s for s in scopes))
    if not picked or any(s not in keys.SCOPES for s in picked):
        raise CodedHTTPException(422, f"权限只能从 {'、'.join(keys.SCOPES)} 里选，至少选一项", KEY_INVALID_INPUT,
                                 field="scopes")
    if service and (bad := [s for s in picked if s in keys.PERSONAL_SCOPES]):
        raise CodedHTTPException(422, f"服务账号不能被派待办，它的密钥不能有「{'、'.join(bad)}」：外部系统回调请用应用的 signal 钩子",
                                 KEY_INVALID_INPUT, field="scopes")
    apps = list(dict.fromkeys(str(a).strip() for a in app_ids or [] if str(a).strip())) or None
    if apps is not None:
        if len(apps) > keys.APPS_MAX:
            raise CodedHTTPException(422, f"一把密钥最多限定 {keys.APPS_MAX} 个应用", KEY_INVALID_INPUT, field="app_ids")
        found = {a.id: a for a in (await session.execute(select(App).where(App.id.in_(apps)))).scalars()}
        missing = [a for a in apps if a not in found]
        personal = [found[a].name for a in apps if a in found and found[a].scope == "personal"]
        if missing:
            raise CodedHTTPException(422, f"限定的应用不存在：{'、'.join(missing)}", KEY_INVALID_INPUT, field="app_ids")
        if personal:
            raise CodedHTTPException(422, f"访问密钥不能限定到个人应用：{'、'.join(personal)}", KEY_INVALID_INPUT,
                                     field="app_ids")
    return picked, apps


async def _issue(session: AsyncSession, *, owner: User, name: str, scopes: list[str], app_ids: list[str] | None,
                 days: float, rotated_from: str | None = None) -> tuple[AccessKey, str]:
    secret = keys.new_secret()
    row = AccessKey(owner_user_id=owner.id, name=name, key_hash=keys.key_hash(secret), prefix=keys.prefix_of(secret),
                    scopes=scopes, app_ids=app_ids, expires_at=utcnow() + timedelta(days=days),
                    rotated_from=rotated_from, created_by=current_principal().username)
    session.add(row)
    await session.flush()
    return row, secret


@router.post("", status_code=201)
async def create_key(payload: KeyIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建一把密钥。明文（agl_…）只在这一次的回包里（key）；库里只存哈希和开头一截。"""
    from app.auth import personal_keys_enabled

    _need_login()
    principal = current_principal()
    if principal.via == "key":
        raise CodedHTTPException(403, "不能用访问密钥建访问密钥", KEY_FORBIDDEN)
    if payload.service_account_id:
        _require_admin()
        owner = await _service(session, payload.service_account_id)
        if owner.disabled:
            raise CodedHTTPException(409, f"服务账号「{owner.username}」已停用，先启用再建密钥", KEY_NOT_ACTIVE)
    else:
        if not personal_keys_enabled():
            raise CodedHTTPException(409, "管理员还没有打开个人访问密钥：系统间集成请让管理员建服务账号的密钥",
                                     PERSONAL_KEYS_OFF)
        owner = await session.get(User, principal.user_id) if principal.user_id else None
        if owner is None:
            raise CodedHTTPException(409, "找不到你的账号，请重新登录", KEY_FORBIDDEN)
    scopes, apps = await _check_input(session, payload.scopes, payload.app_ids,
                                      service=owner.kind == keys.SERVICE_KIND)
    row, secret = await _issue(session, owner=owner, name=payload.name.strip() or "访问密钥", scopes=scopes,
                               app_ids=apps, days=payload.expires_in_days)
    await record(session, "access_key.create", "access_key", row.id,
                 data={"name": row.name, "owner": owner.username, "owner_kind": owner.kind, "prefix": row.prefix,
                       "scopes": scopes, "app_ids": apps, "expires_at": _iso(row.expires_at)})
    await session.commit()
    return {**key_out(row, owner), "key": secret}


async def _own_or_admin(session: AsyncSession, key_id: str) -> tuple[AccessKey, User | None]:
    """这把密钥能不能由这个人管：管理员能管任何一把；别人只能管自己的。看不到的一律当不存在（404）。"""
    _need_login()
    principal = current_principal()
    if principal.via == "key":
        raise CodedHTTPException(403, "不能用访问密钥管理访问密钥", KEY_FORBIDDEN)
    row = await session.get(AccessKey, key_id)
    if row is None or (not _is_admin() and row.owner_user_id != principal.user_id):
        raise CodedHTTPException(404, "访问密钥不存在", KEY_NOT_FOUND)
    return row, await session.get(User, row.owner_user_id)


@router.post("/{key_id}/revoke")
async def revoke_key(key_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """撤销：立刻失效（401 key_revoked），不能恢复。撤销过的再撤销一次照样回成功（不再改时间）。"""
    row, owner = await _own_or_admin(session, key_id)
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        row.revoked_by = current_principal().username
        await record(session, "access_key.revoke", "access_key", row.id,
                     data={"name": row.name, "owner": owner.username if owner else None, "prefix": row.prefix})
        await session.commit()
    return key_out(row, owner)


@router.post("/{key_id}/rotate")
async def rotate_key(key_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """轮换：签一把新的（名字、权限、限定的应用、有效期长短照旧），旧的 24 小时后到期——换钥匙不用停机。新密钥的明文只在
    这一次的回包里（key）。撤销了的、过期了的、已经轮换过的不能再轮换（新建一把）。"""
    from app.auth import personal_keys_enabled

    row, owner = await _own_or_admin(session, key_id)
    state = keys.state_of(row, owner)
    if state != "active":
        raise CodedHTTPException(409, "这把密钥已撤销、过期或拥有者已停用，不能轮换：请新建一把", KEY_NOT_ACTIVE)
    if await session.scalar(select(AccessKey.id).where(AccessKey.rotated_from == row.id).limit(1)):
        raise CodedHTTPException(409, "这把密钥已经轮换过：请用轮换出来的那一把", KEY_NOT_ACTIVE)
    if owner is None:
        raise CodedHTTPException(404, "访问密钥不存在", KEY_NOT_FOUND)
    if owner.kind != keys.SERVICE_KIND and not personal_keys_enabled():
        raise CodedHTTPException(409, "管理员已关闭个人访问密钥，不能轮换", PERSONAL_KEYS_OFF)
    span = keys.aware(row.expires_at) - keys.aware(row.created_at)
    days = min(max(span.total_seconds() / 86400, 1.0), float(keys.EXPIRES_MAX_DAYS))
    fresh, secret = await _issue(session, owner=owner, name=row.name, scopes=list(row.scopes or []),
                                 app_ids=list(row.app_ids) if row.app_ids else None, days=days, rotated_from=row.id)
    grace = utcnow() + keys.ROTATE_GRACE
    if keys.aware(row.expires_at) > grace:
        row.expires_at = grace
    await record(session, "access_key.rotate", "access_key", row.id,
                 data={"name": row.name, "owner": owner.username, "prefix": row.prefix, "new_key": fresh.id,
                       "new_prefix": fresh.prefix, "old_expires_at": _iso(row.expires_at)})
    await session.commit()
    return {**key_out(fresh, owner), "key": secret, "previous": key_out(row, owner)}


@router.get("/{key_id}/requests")
async def key_requests(key_id: str, limit: int = 100, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """这把密钥最近的请求（新的在前，最多 100 条）：时间、方法、接口（路由模板）、状态码、涉及的运行、来源地址。"""
    row, _ = await _own_or_admin(session, key_id)
    return {"items": await keys.recent_requests(session, row.id, min(max(limit, 1), 100)),
            "retain_days": keys.RETAIN.days}
