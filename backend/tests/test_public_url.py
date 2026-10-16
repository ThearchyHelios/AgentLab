"""对外访问地址只有一个说法（04 细案 B4，core/public_url.py）。

以前有两个：环境变量 AGENTLAB_PUBLIC_URL 只管单点登录的回调和来源核对，设置里 notify 一组的 public_base_url 只管通知里的
链接——只配了环境变量的部署，通知里不带链接。现在全站经 public_base_url() 取：

- 环境变量优先，其次设置，都没有是 None；
- 设置的值缓存在进程里（中间件按请求核对来源，不能每次查库），改设置的接口写完就刷新，不用重启；
- 环境变量设了：设置页显示「由环境变量指定」（GET /api/settings 的 public_url 一组），改成别的值回 409、带 code；
- 通知、定时任务 Webhook 的链接，单点登录的回调地址，非 GET 请求的 Origin 核对都用它。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.settings import NOTIFY_GROUP
from app.core import public_url
from app.core.config import Settings, settings
from app.db.base import SessionLocal
from app.db.models import Setting
from app.main import app as main_app
from tests.fakes import auth_env

ENV_URL = "https://agentlab.example.com"
SET_URL = "https://intra.example.net/agentlab"


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == NOTIFY_GROUP))
        await session.commit()
    public_url.remember(None)
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == NOTIFY_GROUP))
        await session.commit()
    public_url.remember(None)


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test")


async def put_address(c: AsyncClient, value: str | None):
    return await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"public_base_url": value}}})


# ---------------------------------------------------------------- 取值的先后


def test_env_wins_over_the_setting(monkeypatch):
    public_url.remember(SET_URL)
    assert public_url.public_base_url() == SET_URL and not public_url.env_locked()
    monkeypatch.setattr(settings, "public_url", ENV_URL)
    assert public_url.public_base_url() == ENV_URL and public_url.env_locked()
    public_url.remember(None)
    assert public_url.public_base_url() == ENV_URL, "设置清空了也还是环境变量的"
    monkeypatch.setattr(settings, "public_url", "")
    assert public_url.public_base_url() is None, "两处都没有：None（通知里不附链接，回调按请求的 Host 拼）"


def test_remember_normalizes_and_drops_broken_values():
    public_url.remember("  https://agentlab.example.com/  ")
    assert public_url.public_base_url() == "https://agentlab.example.com"
    for broken in ("agentlab.example.com", "ftp://files.example.com", "https://a.example.com/?x=1", 8080, ""):
        public_url.remember(broken)
        assert public_url.public_base_url() is None, broken


def test_env_value_is_normalized_and_checked_at_startup(monkeypatch):
    monkeypatch.setenv("AGENTLAB_PUBLIC_URL", " https://agentlab.example.com/ ")
    assert Settings().public_url == "https://agentlab.example.com"
    monkeypatch.setenv("AGENTLAB_PUBLIC_URL", "")
    assert Settings().public_url == ""
    # 写错了启动时就报出来：以前拼出 agentlab.example.com/api/auth/oidc/callback 这样的回调，到登录时才发现
    for broken in ("agentlab.example.com", "https://agentlab.example.com/?from=env"):
        monkeypatch.setenv("AGENTLAB_PUBLIC_URL", broken)
        with pytest.raises(ValueError, match="AGENTLAB_PUBLIC_URL"):
            Settings()


async def test_refresh_reads_the_stored_setting():
    async with SessionLocal() as session:
        session.add(Setting(key=NOTIFY_GROUP, value={"public_base_url": SET_URL + "/"}))
        await session.commit()
    assert public_url.public_base_url() is None, "缓存还没对齐"
    assert await public_url.refresh() == SET_URL
    assert public_url.public_base_url() == SET_URL


# ---------------------------------------------------------------- 设置接口


async def test_saving_the_setting_refreshes_the_cache_without_restart():
    async with client() as c:
        assert (await put_address(c, SET_URL + "/")).status_code == 200
        assert public_url.public_base_url() == SET_URL
        body = (await c.get("/api/settings")).json()
        assert body["public_url"] == {"value": SET_URL, "env_locked": False}
        assert (await put_address(c, None)).status_code == 200
    assert public_url.public_base_url() is None


async def test_env_locks_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "public_url", ENV_URL)
    async with client() as c:
        body = (await c.get("/api/settings")).json()
        assert body["public_url"] == {"value": ENV_URL, "env_locked": True}
        assert body[NOTIFY_GROUP]["public_base_url"] is None, "notify 一组照旧是库里存的那一份"
        r = await put_address(c, SET_URL)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "public_url_env_locked" and r.json()["field"] == "notify.public_base_url"
        assert "AGENTLAB_PUBLIC_URL" in r.json()["detail"]
        # 没改这一项（整组原样写回）不算改：不挡同一请求里别的设置
        assert (await put_address(c, None)).status_code == 200
        # 同一组里别的项（待办汇总的时间）照常能改：环境变量只锁对外访问地址这一项
        r = await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"digest_at": "08:15"}}})
        assert r.status_code == 200 and r.json()[NOTIFY_GROUP]["digest_at"] == "08:15"
        # 只读的 public_url 一组写不进库
        r = await c.put("/api/settings", json={"values": {"public_url": {"value": SET_URL, "env_locked": False}}})
        assert r.status_code == 200
        assert (await c.get("/api/settings")).json()["public_url"] == {"value": ENV_URL, "env_locked": True}
    async with SessionLocal() as session:
        assert await session.get(Setting, "public_url") is None


# ---------------------------------------------------------------- 用到它的地方


async def test_notifications_link_with_the_env_address_alone(monkeypatch):
    """只配了环境变量的部署，通知里也带链接（以前只认设置里的那一项）。"""
    from app.notify import service

    monkeypatch.setattr(settings, "public_url", ENV_URL)
    async with SessionLocal() as session:
        assert await service._base_url(session) == ENV_URL
    monkeypatch.setattr(settings, "public_url", "")
    async with SessionLocal() as session:
        session.add(Setting(key=NOTIFY_GROUP, value={"public_base_url": SET_URL}))
        await session.commit()
    async with SessionLocal() as session:
        assert await service._base_url(session) == SET_URL


async def test_oidc_callback_uses_the_one_address(monkeypatch):
    from starlette.requests import Request

    from app.api.auth import _redirect_uri

    request = Request({"type": "http", "method": "GET", "path": "/api/auth/oidc/start", "headers": [],
                       "scheme": "http", "server": ("10.0.0.5", 8000), "query_string": b""})
    assert _redirect_uri(request) == "http://10.0.0.5:8000/api/auth/oidc/callback", "都没有：按请求的地址拼"
    public_url.remember(SET_URL)
    assert _redirect_uri(request) == f"{SET_URL}/api/auth/oidc/callback", "只在设置里填了的也按它拼"
    monkeypatch.setattr(settings, "public_url", ENV_URL)
    assert _redirect_uri(request) == f"{ENV_URL}/api/auth/oidc/callback"


async def test_origin_check_accepts_the_one_address():
    await auth_env.reset()
    try:
        c = await auth_env.turn_on_local()
        r = await c.post("/api/groups", json={"name": "ops"}, headers={"Origin": "https://intra.example.net"})
        assert r.status_code == 403 and r.json()["code"] == "csrf_origin"
        public_url.remember(SET_URL)
        r = await c.post("/api/groups", json={"name": "ops"}, headers={"Origin": "https://intra.example.net"})
        assert r.status_code == 201, r.text
    finally:
        await auth_env.reset()
