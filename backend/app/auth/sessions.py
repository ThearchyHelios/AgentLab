"""登录会话与登录失败退避。

会话 token 只在 HttpOnly、SameSite=Lax 的 cookie 里，库里存它的 sha256：库被拷走也拿不到能用的会话。
闲置 12 小时、绝对 7 天过期（core/config 可调）。用户被停用、会话被吊销，下一个请求就认不出来。

登录失败在进程内退避（按用户名、按来源地址各记一份）：连错 5 次之后每次加倍等待，最长 5 分钟。单进程部署，不落库。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow

COOKIE = "agentlab_session"
#: last_seen_at 至少隔这么久才写一次：每个请求都写会和运行抢 SQLite 的写锁
_TOUCH_EVERY = timedelta(minutes=1)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(session: AsyncSession, user: Any, *, ip: str | None, user_agent: str | None) -> str:
    """建一个会话，交回 token（只放进 cookie）。不提交，调用方和登录的其他改动一起提交。"""
    from app.db.models import AuthSession

    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(AuthSession(token_hash=token_hash(token), user_id=user.id, created_at=now, last_seen_at=now,
                            expires_at=now + timedelta(days=settings.auth_session_max_days),
                            ip=(ip or "")[:64] or None, user_agent=(user_agent or "")[:300] or None))
    user.last_login_at = now
    return token


async def revoke(session: AsyncSession, token: str | None) -> None:
    from app.db.models import AuthSession

    if not token:
        return
    await session.execute(update(AuthSession).where(AuthSession.token_hash == token_hash(token),
                                                    AuthSession.revoked_at.is_(None)).values(revoked_at=utcnow()))


async def revoke_user(session: AsyncSession, user_id: str) -> None:
    """吊销这个用户的全部会话（停用、重设密码时）。不提交。"""
    from app.db.models import AuthSession

    await session.execute(update(AuthSession).where(AuthSession.user_id == user_id,
                                                    AuthSession.revoked_at.is_(None)).values(revoked_at=utcnow()))


async def user_of(session: AsyncSession, token: str) -> Any | None:
    """cookie 里的 token → 用户。过期（闲置或绝对）、吊销、用户停用都认不出来（None）。"""
    from app.db.models import AuthSession, User

    row = (await session.execute(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
           ).scalar_one_or_none()
    now = utcnow()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    if now - row.last_seen_at > timedelta(hours=settings.auth_session_idle_hours):
        return None
    user = await session.get(User, row.user_id)
    if user is None or user.disabled:
        return None
    if now - row.last_seen_at > _TOUCH_EVERY:
        row.last_seen_at = now
        await session.commit()
    return user


async def groups_of(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    from app.db.models import Group, GroupMember

    names = (await session.execute(
        select(Group.name).join(GroupMember, GroupMember.group_id == Group.id).where(GroupMember.user_id == user_id)
    )).scalars()
    return tuple(sorted(names))


async def principal_of(session: AsyncSession, user: Any, mode: str) -> Any:
    from app.api.deps import Principal

    return Principal(username=user.username, role=user.role, display_name=user.display_name or user.username,
                     user_id=user.id, groups=await groups_of(session, user.id), verified=True, mode=mode)


def cookie_kwargs(secure: bool) -> dict[str, Any]:
    return {"httponly": True, "samesite": "lax", "secure": secure, "path": "/",
            "max_age": settings.auth_session_max_days * 86400}


def is_secure(scheme: str) -> bool:
    return settings.auth_cookie_secure if settings.auth_cookie_secure is not None else scheme == "https"


# ---------------------------------------------------------------- 登录失败退避

_FREE_TRIES = 5
_MAX_WAIT_S = 300
_failures: dict[str, tuple[int, float]] = {}


def wait_seconds(*keys: str) -> int:
    """这几个键（用户名、来源地址）里还要等多久才能再试；不用等为 0。"""
    now = time.monotonic()
    return max((int(until - now) + 1 for k in keys if (hit := _failures.get(k)) and (until := hit[1]) > now),
               default=0)


def failed(*keys: str) -> None:
    now = time.monotonic()
    for key in keys:
        count = _failures.get(key, (0, 0.0))[0] + 1
        wait = 0 if count < _FREE_TRIES else min(2 ** (count - _FREE_TRIES), _MAX_WAIT_S)
        _failures[key] = (count, now + wait)


def succeeded(*keys: str) -> None:
    for key in keys:
        _failures.pop(key, None)
