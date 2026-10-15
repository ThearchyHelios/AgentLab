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
    from app.auth import SETTING_KEY, remember_settings, sessions
    from app.db.models import AuditEvent, AuthSession, Group, GroupMember, Setting, User

    async with SessionLocal() as session:
        for model in (AuthSession, GroupMember, Group, User, AuditEvent):
            await session.execute(delete(model))
        await session.execute(delete(Setting).where(Setting.key == SETTING_KEY))
        await session.commit()
    remember_settings({})
    sessions._failures.clear()


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
