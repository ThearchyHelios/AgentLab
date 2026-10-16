"""通知设置（api/settings.py 的 notify 一组）：定时任务的 Webhook 里拼运行链接用的对外访问地址，和每天的待办汇总几点发
（digest_at，缺省 09:00，04 细案 B3）。

- 默认没填（null），通知里就不附运行链接
- 只收 http(s) 开头的完整地址；末尾的 / 去掉、首尾空白去掉，空串当没填；带 ? 或 # 的拒收
- 写错整个请求回 422、detail 是中文，一项都不落库
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.settings import NOTIFY_GROUP, notify_settings, notify_values
from app.db.base import SessionLocal
from app.db.models import Setting
from app.main import app


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([NOTIFY_GROUP, "run"])))
        await session.commit()
    yield


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_default_is_no_link():
    async with client() as c:
        body = (await c.get("/api/settings")).json()
    assert body["notify"] == {"public_base_url": None, "digest_at": "09:00"}
    async with SessionLocal() as session:
        assert await notify_settings(session) == {"public_base_url": None, "digest_at": "09:00"}


@pytest.mark.parametrize("given, stored", [
    ("https://agentlab.example.com/", "https://agentlab.example.com"),
    ("  http://10.20.30.40:8080  ", "http://10.20.30.40:8080"),
    ("https://intra.example.com/agentlab/", "https://intra.example.com/agentlab"),
    ("", None),
    (None, None),
])
async def test_saved_and_normalized(given, stored):
    async with client() as c:
        put = await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"public_base_url": given}}})
        assert put.status_code == 200, put.text
        assert put.json()["notify"] == {"public_base_url": stored, "digest_at": "09:00"}
        assert (await c.get("/api/settings")).json()["notify"] == {"public_base_url": stored, "digest_at": "09:00"}
    async with SessionLocal() as session:
        assert await notify_settings(session) == {"public_base_url": stored, "digest_at": "09:00"}


@pytest.mark.parametrize("value, fragment", [
    ({"public_base_url": "agentlab.example.com"}, "http:// 或 https://"),
    ({"public_base_url": "ftp://files.example.com"}, "http:// 或 https://"),
    ({"public_base_url": "https://"}, "http:// 或 https://"),
    ({"public_base_url": 8080}, "http:// 或 https://"),
    ({"public_base_url": "https://agentlab.example.com/?from=bot"}, "? 或 #"),
    ({"public_url": "https://agentlab.example.com"}, "不支持「public_url」"),
    ("https://agentlab.example.com", "格式有误"),
])
async def test_invalid_values_are_refused_in_chinese(value, fragment):
    async with client() as c:
        put = await c.put("/api/settings", json={"values": {NOTIFY_GROUP: value, "run": {"stream_tokens": False}}})
        assert put.status_code == 422
        assert fragment in put.json()["detail"]
        assert "对外访问地址" in put.json()["detail"] or "通知" in put.json()["detail"]
        # 先查后写：同一请求里别的组也没有落库
        assert (await c.get("/api/settings")).json()["run"]["stream_tokens"] is True


def test_broken_stored_value_reads_as_unset():
    assert notify_values({"public_base_url": "不是地址", "digest_at": "25:00"}) == {"public_base_url": None,
                                                                                    "digest_at": "09:00"}
    assert notify_values("坏掉的") == {"public_base_url": None, "digest_at": "09:00"}


@pytest.mark.parametrize("given, ok", [("08:30", True), ("23:59", True), ("00:00", True), ("24:00", False),
                                        ("9:00", False), ("09:60", False), (900, False), ("", False)])
async def test_digest_time_is_hh_mm(given, ok):
    async with client() as c:
        put = await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"digest_at": given}}})
        if ok:
            assert put.status_code == 200, put.text
            assert put.json()["notify"]["digest_at"] == given
        else:
            assert put.status_code == 422
            assert "待办汇总的发送时间" in put.json()["detail"]


async def test_changing_digest_time_keeps_the_public_address():
    """只给汇总时间的一组：对外访问地址不被冲成没填（设置页的通知分别保存这两项）。"""
    async with client() as c:
        await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"public_base_url": "https://agentlab.example.com"}}})
        put = await c.put("/api/settings", json={"values": {NOTIFY_GROUP: {"digest_at": "17:45"}}})
        assert put.json()["notify"] == {"public_base_url": "https://agentlab.example.com", "digest_at": "17:45"}
