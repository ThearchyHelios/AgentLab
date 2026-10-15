"""通知渠道与投递（app/notify/，设计文档 §2.4 第 7 条）。

发送核心从定时任务的 Webhook 抽出来共用（格式、签名、发送、脱敏），定时任务的老测试一个字不改照样通过；
渠道的地址、密钥加密落库，接口只回主机名；待办派出、应用实例结束、动作结果未知时按应用的订阅推送，带直达链接，
不放报告里的数字。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import App, AppVersion, NotifyChannel, NotifyDelivery, Run, Setting
from app.engine.runner import run_manager
from app.notify import core, service

URL = "https://hooks.example.com/agentlab/inbox-7f3a9c2e1b"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await service.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def received(monkeypatch):
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))
    return got


async def test_channel_crud_hides_the_address_and_secret(client):
    resp = await client.post("/api/notify/channels", json={
        "name": "审批群", "format": "generic", "url": URL, "secret": "s3cret", "events": ["task.opened"]})
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["url_host"] == "hooks.example.com" and row["signed"] is True
    assert URL not in json.dumps(row) and "s3cret" not in json.dumps(row)
    async with SessionLocal() as session:
        stored = await session.get(NotifyChannel, row["id"])
    assert stored.url != URL and stored.secret != "s3cret"     # 密文

    updated = await client.put(f"/api/notify/channels/{row['id']}", json={
        "name": "审批群", "format": "generic", "events": ["task.opened", "action.unknown"]})
    assert updated.status_code == 200 and updated.json()["events"] == ["task.opened", "action.unknown"]
    assert updated.json()["url_host"] == "hooks.example.com" and updated.json()["signed"] is True   # 不传就保持原值
    assert row["id"] in [c["id"] for c in (await client.get("/api/notify/channels")).json()["items"]]
    toggled = await client.patch(f"/api/notify/channels/{row['id']}", json={"enabled": False})
    assert toggled.status_code == 200 and toggled.json()["enabled"] is False
    assert toggled.json()["events"] == ["task.opened", "action.unknown"] and toggled.json()["signed"] is True
    assert (await client.delete(f"/api/notify/channels/{row['id']}")).status_code == 204


async def test_channel_validation(client):
    bad_event = await client.post("/api/notify/channels", json={"name": "x", "url": URL, "events": ["nope"]})
    assert bad_event.status_code == 422
    wrong_format = await client.post("/api/notify/channels", json={
        "name": "x", "format": "generic", "url": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef123456"})
    assert wrong_format.status_code == 422 and "飞书" in wrong_format.json()["detail"]
    personal = await client.post("/api/notify/channels", json={"name": "x", "scope": "user", "url": URL})
    assert personal.status_code == 422       # 关登录时没有用户，个人渠道无从认起


async def test_personal_channels_cannot_point_at_the_intranet(received):
    """个人渠道权限最低的使用者也能建：地址过 SSRF 检查，保存、发送时各一次；投递记录只看得到自己渠道的。"""
    from tests.fakes import auth_env

    await auth_env.reset()
    try:
        await auth_env.turn_on_local()
        uma_row = await auth_env.make_user("uma", "user")
        uma = await auth_env.login("uma")
        for url in ("http://127.0.0.1:8080/hook", "http://169.254.169.254/latest/meta-data", "http://10.0.0.8/x"):
            r = await uma.post("/api/notify/channels", json={"name": "我的", "scope": "user", "url": url})
            assert r.status_code == 422 and "公网" in r.json()["detail"], (url, r.text)
            assert "10.0.0.8" not in r.json()["detail"], "说法不带检查的细节"
        async with SessionLocal() as session:
            # 存进库之后域名才改指向内网的：发送前再查一次，不发
            from app.core.crypto import encrypt

            row = NotifyChannel(name="我的", scope="user", owner_id=uma_row.id, url=encrypt("http://127.0.0.1:9/x"),
                                events=[])
            other = NotifyChannel(name="全局", scope="global", url=encrypt(URL), events=[])
            session.add_all([row, other])
            await session.flush()
            session.add(NotifyDelivery(channel_id=other.id, subject_kind="run", subject_id="r", event="run.failed",
                                       status="ok"))
            await session.commit()
        tested = (await uma.post(f"/api/notify/channels/{row.id}/test")).json()
        assert tested["ok"] is False and "公网" in tested["message"] and received == []
        assert [d["channel_id"] for d in (await uma.get("/api/notify/deliveries")).json()["items"]] == [row.id]
    finally:
        async with SessionLocal() as session:
            for r in (await session.execute(select(NotifyChannel))).scalars():
                await session.delete(r)
            await session.commit()
        await auth_env.reset()


async def test_test_message_is_signed_and_recorded(client, received):
    row = (await client.post("/api/notify/channels", json={
        "name": "审批群", "format": "generic", "url": URL, "secret": "s3cret"})).json()
    resp = await client.post(f"/api/notify/channels/{row['id']}/test")
    assert resp.json()["ok"] is True
    [request] = received
    from app.process.hooks import verify

    verify("s3cret", dict(request.headers), request.content)      # 同一套签名
    deliveries = (await client.get(f"/api/notify/deliveries?channel_id={row['id']}")).json()["items"]
    assert [(d["event"], d["status"]) for d in deliveries] == [("test", "ok")]


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


async def test_task_opened_is_pushed_to_the_app_subscription_without_numbers(received):
    async with SessionLocal() as session:
        channel = NotifyChannel(name="财务群", format="generic", url=core_encrypt(URL), events=[])
        session.add(channel)
        app = App(name=f"报销-{uuid.uuid4().hex[:6]}", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec_hash="h" * 64, bindings={},
                               spec={"notify": [{"channel": channel.id, "events": ["task.opened"]}]}))
        setting = await session.get(Setting, "notify")
        if setting is None:
            session.add(Setting(key="notify", value={"public_base_url": "https://agentlab.example"}))
        await session.commit()
        app_id, channel_id = app.id, channel.id
    graph = {"nodes": [node("start", "input", fields=[{"name": "amount"}]),
                       node("review", "human", mode="approve", title="审批 {{ input.amount }} 元的报销"),
                       node("out", "output", fields=[])],
             "edges": [{"source": "start", "target": "review"}, {"source": "review", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"amount": 45678}, app_id=app_id, app_version=1)
    for _ in range(200):
        async with SessionLocal() as session:
            status = await session.scalar(select(Run.status).where(Run.id == run.id))
        if status == "interrupted":
            break
        await asyncio.sleep(0.05)
    await service.drain()
    assert len(received) == 1
    body = json.loads(received[0].content)
    assert body["event"] == "task.opened" and "45678" not in received[0].content.decode()
    assert body["title"] == "新的待办：review"
    assert body["links"][0]["url"].startswith("https://agentlab.example/apps/inbox/")
    async with SessionLocal() as session:
        rows = (await session.execute(select(NotifyDelivery).where(NotifyDelivery.channel_id == channel_id)))\
            .scalars().all()
    assert [(r.event, r.status, r.subject_kind) for r in rows] == [("task.opened", "ok", "task")]


def core_encrypt(value: str) -> str:
    from app.core.crypto import encrypt

    return encrypt(value)


def test_webhook_module_still_exports_the_moved_names():
    """抽出去的名字照原名再导出：调用方（api/schedules.py 等）和定时任务的测试不用改。"""
    from app.scheduler import webhook

    for name in ("FORMATS", "FORMAT_LABEL", "check_url", "bot_format", "format_problem", "host_of", "url_hint",
                 "Outcome", "send", "build_request", "send_test", "notify_due"):
        assert hasattr(webhook, name), name
    assert webhook.check_url is core.check_url
