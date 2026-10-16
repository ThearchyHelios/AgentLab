"""通知事件的登记表（app/notify/service.EVENTS；总方案 X6）。

本轮新增的六个事件（数据源变化、口径召回、评测退步 / 失败、待办汇总、密钥到期）在第 1 波一次登记好：名字、界面上的叫法、
推给哪几条路都在 EVENTS 一处，前端的叫法在 lib/terms.ts 的 NOTIFY_TEXT 一处。第 2、3 波实现推送的那一块只改自己那一行
的范围，不再去改同一个元组、同一张叫法表（几块并行改同一处必然冲突）。这里守：

- 登记表：六个事件都在，叫法不空；推给哪几条路是空的就是「登记了、还没有推送」——接口不收这种订阅、GET /api/notify/events
  照实给空的 scopes，设置页和应用的通知里都不列（勾了收不到就是「看着生效、其实没生效」，个人渠道「实例完成」的老问题）；
- 前后端对照：terms.ts 的 eventLabel 和 EVENTS 一一对应、逐字一致；投递记录才有的事件（改派、升级、催办、测试消息）另有一张
  叫法表，和服务端 _AS 对上（写法同 test_chat_turn_policy.py，两边一改就对不上）；
- 推给哪几条路是真的：_channels 只走事件登记了的路；没登记的事件推不出去，直接报错；
- 订阅的范围不对（全局渠道勾只推个人的事件）拒收，但只核对这次新勾的：以前存下的照旧，开关、改名不会因此 422。

渠道名、地址都是虚构的。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import NotifyChannel
from app.notify import service

TERMS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "terms.ts"
URL = "https://hooks.example.com/agentlab/notify-events-5d1c"

#: 本轮登记的六个（总方案 X6）
THIS_ROUND = ("source.changed", "ledger.recall", "eval.regressed", "eval.failed", "task.digest", "key.expiring")
#: 其中已经实现了推送、打开了订阅的（推给哪几条路）：定时评测的两个只推全局渠道（第 3 波 W3-c，notify_eval）；每天的待办汇总
#: 只推个人渠道；访问密钥快到期，个人密钥推本人、服务账号的推全局渠道（第 3 波 W3-d，notify/digest.py）。其余还没有
PUSHED_THIS_ROUND = {"eval.regressed": {"global"}, "eval.failed": {"global"},
                     "task.digest": {"user"}, "key.expiring": {"global", "user"}}


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    async with SessionLocal() as session:
        await session.execute(delete(NotifyChannel).where(NotifyChannel.name.like("事件登记-%")))
        await session.commit()


# --------------------------------------------------------------------------
# 登记表
# --------------------------------------------------------------------------


def test_this_rounds_events_are_registered_once_and_not_pushed_yet():
    for name in THIS_ROUND:
        assert name in service.EVENTS, f"{name} 没有登记"
        assert service.EVENTS[name].label, name
        if name in PUSHED_THIS_ROUND:
            assert set(service.EVENTS[name].scopes) == PUSHED_THIS_ROUND[name], name
        else:
            assert service.EVENTS[name].scopes == (), f"{name} 还没有推送：不该有人订阅得到"
    assert len({e.label for e in service.EVENTS.values()}) == len(service.EVENTS), "两个事件叫成了同一句"
    assert service.EVENT_LABEL == {k: v.label for k, v in service.EVENTS.items()}


def test_events_that_are_pushed_today_keep_their_audience():
    """现有五个事件推给哪几条路，照 _channels 和各个 notify_* 现在的做法登记（行为不变）。"""
    every = {"global", "user", "app"}
    assert {k: set(v.scopes) for k, v in service.EVENTS.items() if v.scopes} == {
        "task.opened": every, "run.finished": every, "run.failed": every, "action.unknown": every,
        # 发给提交人自己（和订阅了它的全局渠道），不归哪个应用
        "review.done": {"global", "user"},
        **PUSHED_THIS_ROUND,
    }
    for event in service.EVENTS.values():
        assert set(event.scopes) <= every


def _object_block(source: str, name: str) -> str:
    """terms.ts 里 NOTIFY_TEXT 下面名叫 name 的那个对象字面量（到配对的右花括号为止）。"""
    head = source.index("export const NOTIFY_TEXT = {")
    start = source.index(f"{name}: {{", head) + len(name) + 2
    depth, i = 1, start + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
    return source[start:i]


def _entries(block: str) -> dict[str, str]:
    return dict(re.findall(r"'?([a-z]+(?:\.[a-z_]+)?)'?:\s*'([^']*)'", block))


def test_frontend_labels_match_the_registry_word_for_word():
    source = TERMS_TS.read_text(encoding="utf-8")
    labels = _entries(_object_block(source, "eventLabel"))
    assert labels == service.EVENT_LABEL, "设置页的叫法和服务端登记的对不上（一种说法）"
    delivery = _entries(_object_block(source, "deliveryEventLabel"))
    assert set(delivery) == set(service._AS) | {"test"}, "投递记录里会出现的事件都要有叫法"
    assert not set(delivery) & set(labels)


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


async def test_events_endpoint_says_who_receives_each_event(client):
    rows = (await client.get("/api/notify/events")).json()
    assert [r["event"] for r in rows] == list(service.EVENTS)
    by = {r["event"]: r for r in rows}
    assert by["eval.regressed"] == {"event": "eval.regressed", "label": "定时评测有退步", "scopes": ["global"]}
    assert by["ledger.recall"]["scopes"] == []
    assert by["review.done"]["scopes"] == ["global", "user"]


async def test_events_not_pushed_yet_cannot_be_subscribed(client):
    resp = await client.post("/api/notify/channels", json={
        "name": "事件登记-评测", "url": URL, "events": ["task.opened", "ledger.recall"]})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "口径升版，答案的数变了" in detail and "还没有推送" in detail
    unknown = await client.post("/api/notify/channels", json={"name": "事件登记-未知", "url": URL, "events": ["nope"]})
    assert unknown.status_code == 422
    assert "口径升版，答案的数变了" not in unknown.json()["detail"], "可选的里面不列还没有推送的"
    assert "待办打开" in unknown.json()["detail"]


async def test_scope_mismatch_is_refused_only_for_newly_ticked_events(client):
    refused = await client.post("/api/notify/channels", json={
        "name": "事件登记-应用", "scope": "app", "url": URL, "events": ["review.done"]})
    assert refused.status_code == 422, refused.text
    assert "我提交审核的应用有结果" in refused.json()["detail"] and "应用" in refused.json()["detail"]
    # 以前存下的（当时界面对哪种渠道都列这一项）：开关、改名照旧，不因此 422
    async with SessionLocal() as session:
        row = NotifyChannel(name="事件登记-旧", scope="app", url=encrypt(URL), events=["task.opened", "review.done"])
        session.add(row)
        await session.commit()
        channel_id = row.id
    toggled = await client.patch(f"/api/notify/channels/{channel_id}", json={"enabled": False})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["events"] == ["task.opened", "review.done"]
    again = await client.patch(f"/api/notify/channels/{channel_id}", json={"events": ["review.done", "run.failed"]})
    assert again.status_code == 200, again.text
    added = await client.patch(f"/api/notify/channels/{channel_id}", json={"events": ["review.done", "eval.failed"]})
    assert added.status_code == 422 and "定时评测没有评完" in added.json()["detail"]


# --------------------------------------------------------------------------
# 推送只走登记了的路
# --------------------------------------------------------------------------


async def test_channels_only_walk_the_paths_the_event_is_registered_for(monkeypatch):
    monkeypatch.setitem(service.EVENTS, "demo.personal", service.Event("只推个人的演示事件", ("user",)))
    async with SessionLocal() as session:
        row = NotifyChannel(name="事件登记-全局", scope="global", url=encrypt(URL),
                            events=["demo.personal", "task.opened", "ledger.recall"])
        session.add(row)
        await session.commit()
        try:
            ids = lambda found: [c.id for c in found]  # noqa: E731
            assert row.id not in ids(await service._channels(session, "demo.personal", None, [])), "全局渠道这条路没登记"
            assert row.id in ids(await service._channels(session, "task.opened", None, []))
            assert await service._channels(session, "ledger.recall", None, []) == [], "还没有推送的：谁都不推"
            with pytest.raises(ValueError, match="没有登记"):
                await service._channels(session, "demo.unregistered", None, [])
        finally:
            await session.delete(row)
            await session.commit()
