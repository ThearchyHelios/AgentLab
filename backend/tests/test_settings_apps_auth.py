"""设置接口的两处改动：应用一组（按需视图的每日上限，D-j；使用者能不能建个人应用，normal-user-apps N7）登记进默认设置；
登录设置不经 /api/settings 读写（D-l）。

- apps.on_demand_daily_limit 默认 200，只收 0 到 100,000 的整数，写错 422、一项都不落库；
- apps.user_personal_apps 默认关，只收开或关；
- auth 这一组（OIDC 客户端密钥、管理员映射）GET 不给，PUT 带着也不写——只经 /api/auth/settings 改；
- 改设置只给管理员（开登录时搭建者只读）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.api.settings import APPS_GROUP, apps_settings, apps_values
from app.db.base import SessionLocal
from app.db.models import Setting
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def clean():
    await auth_env.reset()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == APPS_GROUP))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == APPS_GROUP))
        await session.commit()
    await auth_env.reset()


async def test_daily_limit_is_a_registered_setting_with_default_200():
    async with auth_env.client() as c:
        body = (await c.get("/api/settings")).json()
        assert body[APPS_GROUP] == {"on_demand_daily_limit": 200, "user_personal_apps": False, "share_links": False}
        put = await c.put("/api/settings", json={"values": {APPS_GROUP: {"on_demand_daily_limit": 30}}})
        assert put.status_code == 200, put.text
        assert put.json()[APPS_GROUP] == {"on_demand_daily_limit": 30, "user_personal_apps": False, "share_links": False}
        assert (await c.get("/api/settings")).json()[APPS_GROUP] == {"on_demand_daily_limit": 30,
                                                                    "user_personal_apps": False, "share_links": False}
    async with SessionLocal() as session:
        assert await apps_settings(session) == {"on_demand_daily_limit": 30, "user_personal_apps": False,
                                                "share_links": False}
    assert apps_values({"on_demand_daily_limit": "坏的", "user_personal_apps": "开", "share_links": 1}) == {
        "on_demand_daily_limit": 200, "user_personal_apps": False, "share_links": False}, "存坏了的按默认"


@pytest.mark.parametrize("value, fragment", [
    ({"on_demand_daily_limit": -1}, "按需视图每日上限"),
    ({"on_demand_daily_limit": True}, "按需视图每日上限"),
    ({"on_demand_daily_limit": 1.5}, "按需视图每日上限"),
    ({"on_demand_daily_limit": 100_001}, "100,000"),
    ({"ttl": 3}, "不支持「ttl」"),
    ({"user_personal_apps": "yes"}, "使用者可以建个人应用"),
    ({"user_personal_apps": 1}, "只能是开或关"),
    ("200", "需要是一个对象"),
])
async def test_bad_daily_limits_are_refused(value, fragment):
    async with auth_env.client() as c:
        r = await c.put("/api/settings", json={"values": {APPS_GROUP: value, "ui": {"theme": "dark"}}})
        assert r.status_code == 422 and fragment in r.json()["detail"], r.text
    async with SessionLocal() as session:
        assert await session.get(Setting, APPS_GROUP) is None, "一项都不落库"


async def test_auth_settings_are_not_read_or_written_here():
    from app.auth import SETTING_KEY, load_settings

    admin = await auth_env.turn_on_local()
    async with SessionLocal() as session:
        row = await session.get(Setting, SETTING_KEY)
        before = dict(row.value)
    body = (await admin.get("/api/settings")).json()
    assert SETTING_KEY not in body, "登录设置不经这里读：里面有 OIDC 的客户端密钥"
    r = await admin.put("/api/settings", json={"values": {SETTING_KEY: {"mode": "off"}, "ui": {"theme": "dark"}}})
    assert r.status_code == 200 and SETTING_KEY not in r.json()
    async with SessionLocal() as session:
        assert (await session.get(Setting, SETTING_KEY)).value == before, "也不经这里写"
    assert (await load_settings(force=True))["mode"] == "local"

    await auth_env.make_user("bob", "builder")
    bob = await auth_env.login("bob")
    assert (await bob.get("/api/settings")).json()[APPS_GROUP] == {"on_demand_daily_limit": 200,
                                                                    "user_personal_apps": False, "share_links": False}
    assert (await bob.put("/api/settings", json={"values": {APPS_GROUP: {"on_demand_daily_limit": 1}}})
            ).status_code == 403, "设置页的应用一组只有管理员能改"
    assert (await bob.put("/api/settings", json={"values": {APPS_GROUP: {"user_personal_apps": True}}})
            ).status_code == 403, "个人应用的开关同样只有管理员能改"
