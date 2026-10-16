"""应用实例结束的通知（notify/service.notify_run）：说法取自实例页，个人渠道按「我发起的实例」推。

- 被驳回的实例（cancelled + end_reason=rejected）走「实例结束」，状态写实例页那一句（「「review」被驳回」），不推
  「实例失败」（05-已知遗留 (a)、总方案 X6）；
- 走到「已退回」那种结局的运行，实例页写成果节点的标题、记成人的决定，推送以前却写「已完成」：现在两边一致；
- 个人渠道勾了「实例完成 / 失败」以前永远收不到（_channels 拿到的 users 是空的）：发起人是认证过的身份时，他自己的
  个人渠道收得到；自报的署名不算（04-对外集成与触达 B1）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import App, AppVersion, NotifyChannel, NotifyDelivery, Run, User
from app.engine.runner import run_manager
from app.notify import core, service

URL = "https://hooks.example.com/agentlab/run-3b8e1f"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await service.drain()
    await run_manager.shutdown()


@pytest.fixture
def received(monkeypatch):
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))
    # 个人渠道发送前要解析域名、核对不是内网：测试里不连外网
    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(service, "personal_url_problem", public)
    return got


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def flow(**human):
    nodes = [node("start", "input", fields=[{"name": "q"}]),
             node("review", "human", mode="approve", title="审一下", **human),
             node("ok", "output", fields=[{"name": "结论", "value": "通过"}]),
             node("no", "output", fields=[{"name": "结论", "value": "退回"}])]
    edges = [{"source": "start", "target": "review"},
             {"source": "review", "target": "ok", "sourceHandle": "approved"},
             {"source": "review", "target": "no", "sourceHandle": "rejected"}]
    return {"nodes": nodes, "edges": edges}


async def _app(events: list[str]) -> tuple[str, str]:
    async with SessionLocal() as session:
        channel = NotifyChannel(name=f"群-{uuid.uuid4().hex[:6]}", format="generic", url=encrypt(URL), events=[])
        session.add(channel)
        app = App(name=f"报销-{uuid.uuid4().hex[:6]}", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec_hash="h" * 64, bindings={},
                               spec={"notify": [{"channel": channel.id, "events": events}]}))
        await session.commit()
        return app.id, channel.id


async def _wait(run_id: str, statuses: tuple[str, ...]) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _decide(run_id: str, approved: bool) -> Run:
    """经待办服务回复（同界面上点同意 / 驳回）：实例页认结局靠待办的结论。"""
    from app.api.deps import Principal
    from app.db.models import Task
    from app.process import tasks

    await _wait(run_id, ("interrupted",))
    await run_manager.wait_idle(run_id)
    for _ in range(100):
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.run_id == run_id, Task.status == "pending")))\
                .scalars().first()
        if task is not None:
            break
        await asyncio.sleep(0.05)
    await tasks.respond(task.id, Principal(username=None, role="admin", mode="off"),
                        decision="approve" if approved else "reject", note="金额 45678 不对")
    final = await _wait(run_id, ("succeeded", "failed", "cancelled"))
    await run_manager.wait_idle(run_id)
    await service.drain()
    return final


async def _deliveries(channel_id: str) -> list[tuple[str, str]]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(NotifyDelivery).where(NotifyDelivery.channel_id == channel_id))).scalars()
        return [(r.event, r.status) for r in rows]


async def test_a_rejected_instance_is_pushed_as_finished_in_the_instance_page_words(received):
    app_id, channel_id = await _app(["run.finished", "run.failed"])
    run = await run_manager.start(graph=flow(stop_on_reject=True), input_payload={"q": "x"}, app_id=app_id,
                                  app_version=1)
    final = await _decide(run.id, approved=False)
    assert (final.status, final.end_reason) == ("cancelled", "rejected")
    assert await _deliveries(channel_id) == [("run.finished", "ok")], "驳回不进「实例失败」"
    [request] = received
    body = json.loads(request.content)
    assert body["event"] == "run.finished" and body["title"].endswith("的实例已结束")
    assert "状态：「review」被驳回" in body["text"] and "失败" not in body["text"]
    assert "45678" not in request.content.decode(), "驳回的备注不进消息（可能带着数）"


async def test_a_manual_stop_is_not_pushed(received):
    app_id, channel_id = await _app(["run.finished", "run.failed"])
    run = await run_manager.start(graph=flow(), input_payload={"q": "x"}, app_id=app_id, app_version=1)
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    assert await run_manager.cancel(run.id) == "cancelled"
    await service.drain()
    assert await _deliveries(channel_id) == [] and received == []


async def test_reaching_the_returned_outcome_no_longer_says_completed(received):
    app_id, channel_id = await _app(["run.finished"])
    run = await run_manager.start(graph=flow(), input_payload={"q": "x"}, app_id=app_id, app_version=1)
    final = await _decide(run.id, approved=False)
    assert final.status == "succeeded" and final.output.get("结论") == "退回"
    [request] = received
    body = json.loads(request.content)
    # 实例页写走到的成果节点（no），记成人的决定：推送也这么写，不再说「已完成」
    assert "状态：no" in body["text"] and "已完成" not in body["title"] and body["title"].endswith("的实例已结束")


async def _personal(username: str, events: list[str]) -> str:
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, role="user")
            session.add(user)
            await session.flush()
        channel = NotifyChannel(name="我的", scope="user", owner_id=user.id, format="generic", url=encrypt(URL),
                                events=events)
        session.add(channel)
        await session.commit()
        return channel.id


async def _finished_instance(app_id: str, *, started_by: str, verified: bool) -> str:
    async with SessionLocal() as session:
        run = Run(workflow_name="报销", status="succeeded", graph=flow(), input={}, app_id=app_id, app_version=1,
                  started_by=started_by, origin_kind="app_entry",
                  origin={"kind": "app_entry", "actor": started_by, "actor_verified": verified})
        session.add(run)
        await session.commit()
        return run.id


async def test_the_initiator_s_personal_channel_receives_my_instance_finished(received):
    """B1：个人渠道订阅了 run.finished，发起人自己发起的实例结束了收得到；应用自己没订阅也一样。"""
    app_id, _ = await _app([])
    name = f"uma{uuid.uuid4().hex[:6]}"
    mine = await _personal(name, ["run.finished"])
    run_id = await _finished_instance(app_id, started_by=name, verified=True)
    assert await service.notify_run(run_id, "run.finished") == 1
    assert await _deliveries(mine) == [("run.finished", "ok")]
    body = json.loads(received[0].content)
    assert body["title"].endswith("的实例已完成") and "状态：已完成" in body["text"]


async def test_a_self_declared_initiator_does_not_reach_a_personal_channel(received):
    """关登录时署名是自己填的，谁都能填别人的名字：不按它推给那个人的个人渠道。"""
    app_id, _ = await _app([])
    name = f"vic{uuid.uuid4().hex[:6]}"
    mine = await _personal(name, ["run.finished"])
    run_id = await _finished_instance(app_id, started_by=name, verified=False)
    assert await service.notify_run(run_id, "run.finished") == 0
    assert await _deliveries(mine) == [] and received == []
