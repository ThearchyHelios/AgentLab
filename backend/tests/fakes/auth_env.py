"""身份相关测试的公共部分：把登录设置、用户、组、会话、审计清回关登录的样子，建用户，登录拿一个带会话的客户端。

登录设置是进程内缓存 + settings 表的一行：一个测试开了登录不复原，同一个进程里后面的测试全会 401。fixture 一律在
finally 里调 reset()。
"""
from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal

#: 开了登录时非 GET 请求必须带的头
CSRF = {"X-AgentLab-Request": "1"}


async def reset() -> None:
    from app.auth import SETTING_KEY, keys, remember_settings, sessions
    from app.db.models import AuditEvent, AuthSession, Group, GroupMember, Setting, User
    from app.db.models_apps import ApiReceipt
    from app.db.models_identity import AccessKey, AccessKeyRequest

    async with SessionLocal() as session:
        for model in (AccessKeyRequest, ApiReceipt, AccessKey, AuthSession, GroupMember, Group, User, AuditEvent):
            await session.execute(delete(model))
        await session.execute(delete(Setting).where(Setting.key == SETTING_KEY))
        await session.commit()
    remember_settings({})
    sessions._failures.clear()
    # 访问密钥的进程内状态：限流的桶、还没写库的请求记录
    keys._BUCKETS.clear()
    keys._PENDING.clear()


def client(**headers: str) -> AsyncClient:
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={**CSRF, **headers})


async def make_user(username: str, role: str, password: str = "password-123", **extra: Any) -> Any:
    from app.auth.passwords import hash_password
    from app.db.models import User

    async with SessionLocal() as session:
        user = User(username=username, display_name=username, role=role, password_hash=hash_password(password),
                    **extra)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def turn_on_local(admin: str = "root", password: str = "password-123") -> AsyncClient:
    """关登录 → 账号密码登录，同一个请求建出第一个管理员；交回已经以管理员登录的客户端。"""
    c = client()
    r = await c.put("/api/auth/settings", json={"mode": "local",
                                                 "bootstrap": {"username": admin, "password": password}})
    assert r.status_code == 200, r.text
    return c


async def login(username: str, password: str = "password-123") -> AsyncClient:
    c = client()
    r = await c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def keyed(secret: str, **headers: str) -> AsyncClient:
    """带访问密钥的客户端：只有 Authorization 头，不带 CSRF 头、不带 cookie（外部系统就是这样调的）。"""
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": f"Bearer {secret}", **headers})


async def make_key(owner: Any, *, scopes: tuple[str, ...] = ("apps.read", "apps.run", "tasks.read", "tasks.respond",
                                                             "reports.read"),
                   app_ids: list[str] | None = None, days: float = 90, name: str = "测试密钥") -> tuple[Any, str]:
    """直接在库里签一把密钥（不经接口），交回 (行, 明文)。owner 是 User 行。"""
    from datetime import timedelta

    from app.auth import keys
    from app.db.base import utcnow
    from app.db.models_identity import AccessKey

    secret = keys.new_secret()
    async with SessionLocal() as session:
        row = AccessKey(owner_user_id=owner.id, name=name, key_hash=keys.key_hash(secret), prefix=keys.prefix_of(secret),
                        scopes=list(scopes), app_ids=app_ids, expires_at=utcnow() + timedelta(days=days))
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row, secret


async def make_service(username: str = "svc-portal", **extra: Any) -> Any:
    from app.db.models import User

    async with SessionLocal() as session:
        user = User(username=username, display_name=username, role="user", kind="service", **extra)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def personal_keys(on: bool = True) -> None:
    """个人访问密钥的开关（登录设置里的 personal_keys），直接改缓存和库。"""
    from app.auth import PERSONAL_KEYS, SETTING_KEY, load_settings, remember_settings
    from app.db.models import Setting

    value = {**await load_settings(force=True), PERSONAL_KEYS: on}
    async with SessionLocal() as session:
        row = await session.get(Setting, SETTING_KEY)
        if row is None:
            session.add(Setting(key=SETTING_KEY, value=value))
        else:
            row.value = value
        await session.commit()
    remember_settings(value)
