"""每天的待办汇总、访问密钥快到期（notify/digest.py + notify/service，docs/design/next-round/04-对外集成与触达.md B3）。

守的是：
- 待办汇总到设置的时间（notify.digest_at，按服务端时区）才发，只发给订阅了 task.digest 的个人渠道；内容是条数、最早时限、
  最多 5 条节点在图上的名字（不是按运行数据渲染出来的待办标题）、去待办箱的链接，不放数；
- 同一天只发一次，重启（进程里记的清掉）也不重发：靠 settings 表里按日期认领的一行；第二天照常发；
- 没有待办的人不发；
- 访问密钥到期前 7 天、1 天各推一次，不多推；个人密钥推本人的个人渠道，服务账号的推全局渠道；每次推都记审计；
- 轮换下来的旧密钥、撤销了的、建的时候就已经在这一档里的（自己设了短有效期），不推。

都用 Webhook 渠道和 httpx.MockTransport 接收；人名、应用名是虚构的。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.core.crypto import encrypt
from app.db.base import SessionLocal, utcnow
from app.db.models import App, AuditEvent, NotifyChannel, NotifyDelivery, Run, Setting, Task, TaskAssignment, User
from app.notify import core, digest, service
from tests.fakes import auth_env

URL = "https://hooks.example.com/agentlab/digest-91c2"


@pytest.fixture
def received(monkeypatch):
    got: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))

    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(service, "personal_url_problem", public)
    return got


@pytest.fixture(autouse=True)
async def login_on(received):
    """开着账号密码登录（个人渠道挂在认证过的账号上）。依赖 received：收尾时等推送推完，假的接收端还在。"""
    await auth_env.reset()
    digest.forget()
    await auth_env.turn_on_local()
    async with SessionLocal() as session:
        session.add(Setting(key="notify", value={"public_base_url": "https://agentlab.example.com",
                                                 "digest_at": "09:00"}))
        await session.commit()
    yield
    await service.drain()
    async with SessionLocal() as session:
        for model in (NotifyDelivery, NotifyChannel, TaskAssignment, Task):
            await session.execute(delete(model))
        await session.execute(delete(Run).where(Run.workflow_name == "汇总测试"))
        await session.execute(delete(App).where(App.name == "报销审批"))
        await session.execute(delete(Setting).where(Setting.key.like(digest.STATE_PREFIX + "%")))
        await session.execute(delete(Setting).where(Setting.key == "notify"))
        await session.commit()
    digest.forget()
    await auth_env.reset()


def local(hour: int, minute: int, *, day: int = 16) -> datetime:
    """服务端所在时区的某个时刻（汇总按服务端时区算）→ UTC。"""
    return datetime(2026, 10, day, hour, minute).astimezone().astimezone(timezone.utc)


async def personal(user: User, events: list[str]) -> NotifyChannel:
    async with SessionLocal() as session:
        row = NotifyChannel(name=f"个人-{user.username}", scope="user", owner_id=user.id, format="generic",
                            url=encrypt(URL), events=events)
        session.add(row)
        await session.commit()
        return row


async def tasks_for(username: str, n: int, *, label: str = "主管审批") -> None:
    graph = {"nodes": [{"id": "review", "type": "human", "data": {"label": label}}], "edges": []}
    async with SessionLocal() as session:
        app = App(name="报销审批", published_version=1)
        session.add(app)
        run = Run(workflow_name="汇总测试", status="interrupted", graph=graph, input={"amount": 45678})
        session.add(run)
        await session.flush()
        for i in range(n):
            task = Task(run_id=run.id, node_id="review", app_id=app.id, kind="approve",
                        title=f"审批 45678 元的报销 #{i}", due_at=(utcnow() + timedelta(hours=i + 1)).replace(tzinfo=None))
            session.add(task)
            await session.flush()
            session.add(TaskAssignment(task_id=task.id, principal_kind="user", principal=username))
        await session.commit()


# --------------------------------------------------------------------------
# 待办汇总
# --------------------------------------------------------------------------


async def test_digest_is_sent_once_a_day_at_the_set_time(received):
    ana = await auth_env.make_user("ana", "user")
    bo = await auth_env.make_user("bo", "user")
    await personal(ana, ["task.digest"])
    await personal(bo, ["task.opened"])            # 没订阅汇总
    await tasks_for("ana", 7)
    await tasks_for("bo", 2)

    assert await digest.digest_due(local(8, 59)) == [], "还没到点"
    assert await digest.digest_due(local(9, 0)) == [ana.id]
    await service.drain()
    [body] = received
    assert body["event"] == "task.digest" and body["title"] == "你有 7 条待办"
    text = body["text"]
    assert "条数：7 条" in text and "最早时限" in text
    assert text.count("「主管审批」") == 5, "最多列 5 条，用节点在图上的名字"
    assert "另有：2 条" in text and "报销审批" in text
    assert "45678" not in json.dumps(body, ensure_ascii=False), "不放按运行数据渲染出来的标题、不放数"
    assert body["links"] == [{"label": "去待办箱", "url": "https://agentlab.example.com/apps?tab=inbox"}]

    assert await digest.digest_due(local(9, 15)) == [], "同一天只发一次"
    digest.forget()                                   # 重启：进程里记的没了
    assert await digest.digest_due(local(10, 0)) == [], "重启了也不重发：认领在 settings 表里"
    await service.drain()
    assert len(received) == 1
    async with SessionLocal() as session:
        rows = (await session.execute(select(NotifyDelivery).where(NotifyDelivery.event == "task.digest"))).scalars()
        assert [(r.subject_kind, r.subject_id, r.status) for r in rows] == [("digest", ana.id, "ok")]

    assert await digest.digest_due(local(9, 1, day=17)) == [ana.id], "第二天照常"


async def test_digest_follows_the_configured_time_and_skips_people_with_nothing_pending(received):
    cy = await auth_env.make_user("cy", "user")
    await personal(cy, ["task.digest"])
    async with SessionLocal() as session:
        row = await session.get(Setting, "notify")
        row.value = {**row.value, "digest_at": "17:30"}
        await session.commit()
    assert await digest.digest_due(local(9, 0)) == []
    assert await digest.digest_due(local(17, 30)) == [cy.id]
    await service.drain()
    assert received == [], "没有待办不发「0 条」"


async def test_claim_rows_are_not_listed_as_settings(received):
    await digest.digest_due(local(9, 0))
    async with auth_env.client() as c:
        root = await auth_env.login("root")
        body = (await root.get("/api/settings")).json()
    assert not [k for k in body if k.startswith(digest.STATE_PREFIX)]


# --------------------------------------------------------------------------
# 访问密钥快到期
# --------------------------------------------------------------------------


async def _expiring_audits(key_id: str) -> list[int]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(AuditEvent).where(AuditEvent.action == "access_key.expiring",
                                                               AuditEvent.subject_id == key_id))).scalars()
        return sorted(r.data["days"] for r in rows)


async def _age(key, days: float) -> None:
    from app.db.models_identity import AccessKey

    async with SessionLocal() as session:
        row = await session.get(AccessKey, key.id)
        row.created_at = utcnow() - timedelta(days=days)
        await session.commit()


async def test_personal_key_reminds_its_owner_at_7_and_1_days(received):
    await auth_env.personal_keys(True)
    dee = await auth_env.make_user("dee", "user")
    mine = await personal(dee, ["key.expiring"])
    async with SessionLocal() as session:
        session.add(NotifyChannel(name="全局", scope="global", format="generic", url=encrypt(URL),
                                  events=["key.expiring"]))
        await session.commit()
    key, secret = await auth_env.make_key(dee, days=30, name="周报脚本")
    await _age(key, 60)
    now = utcnow()
    assert await digest.keys_due(now) == [], "离到期还有 30 天"
    assert await digest.keys_due(now + timedelta(days=23, hours=1)) == [key.id]
    await service.drain()
    [body] = received
    assert body["event"] == "key.expiring" and "周报脚本" in body["title"] and "7 天后" in body["title"]
    assert secret not in json.dumps(body) and key.prefix in body["text"]
    assert body["links"] == [{"label": "去轮换", "url": "https://agentlab.example.com/settings/keys"}]
    async with SessionLocal() as session:
        to = (await session.execute(select(NotifyDelivery.channel_id))).scalars().all()
    assert to == [mine.id], "个人密钥只推本人，不推全局渠道"
    assert await digest.keys_due(now + timedelta(days=24)) == [], "这一档推过了"
    digest.forget()
    assert await digest.keys_due(now + timedelta(days=25)) == [], "重启后按审计认得出推过了"
    assert await digest.keys_due(now + timedelta(days=29, hours=1)) == [key.id], "到期前 1 天再推一次"
    assert await digest.keys_due(now + timedelta(days=29, hours=12)) == []
    assert await _expiring_audits(key.id) == [1, 7]
    await service.drain()
    assert len(received) == 2 and "1 天内" in received[1]["title"]


async def test_service_account_keys_remind_global_channels(received):
    svc = await auth_env.make_service("svc-portal")
    async with SessionLocal() as session:
        glob = NotifyChannel(name="运维群", scope="global", format="generic", url=encrypt(URL), events=["key.expiring"])
        session.add(glob)
        await session.commit()
    key, _ = await auth_env.make_key(svc, days=5, name="门户拉数")
    await _age(key, 30)
    assert await digest.keys_due(utcnow()) == [key.id]
    await service.drain()
    [body] = received
    assert "服务账号 svc-portal" in body["text"]
    async with SessionLocal() as session:
        assert (await session.execute(select(NotifyDelivery.channel_id))).scalars().all() == [glob.id]


async def test_keys_that_should_not_remind(received):
    from app.db.models_identity import AccessKey

    svc = await auth_env.make_service("svc-bi")
    async with SessionLocal() as session:
        session.add(NotifyChannel(name="运维群", scope="global", format="generic", url=encrypt(URL),
                                  events=["key.expiring"]))
        await session.commit()
    short, _ = await auth_env.make_key(svc, days=3, name="临时")              # 建的时候就只有 3 天：不推 7 天那一档
    old, _ = await auth_env.make_key(svc, days=0.5, name="轮换下来的")
    await _age(old, 90)
    new, _ = await auth_env.make_key(svc, days=90, name="轮换上去的")
    revoked, _ = await auth_env.make_key(svc, days=2, name="撤销了的")
    await _age(revoked, 30)
    async with SessionLocal() as session:
        (await session.get(AccessKey, new.id)).rotated_from = old.id
        (await session.get(AccessKey, revoked.id)).revoked_at = utcnow()
        await session.commit()
    assert await digest.keys_due(utcnow()) == []
    assert await digest.keys_due(utcnow() + timedelta(days=2, hours=1)) == [short.id], "1 天那一档照推"
    await service.drain()
    assert len(received) == 1
