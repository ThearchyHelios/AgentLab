"""入站签名钩子（app/process/hooks.py，设计文档 §2.4 第 8 条、§7.2）。

签名同出站通用格式：HMAC-SHA256(f"{ts}." + body)，时间窗 5 分钟；Idempotency-Key 去重——处理成功过的同一个键交回上次的
结果、不再处理，被拒的可以用同一个键重试。signal 按关联键解决「等外部系统」的待办；start 按入口发起实例（来源记 hook）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import App, AppVersion, HookReceipt, Run, RunEvent, Task
from app.engine.runner import run_manager
from app.notify.core import sign
from app.process import hooks


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


#: 发布版里声明的钩子（D-g：收请求时按当前发布版的声明认 kind 和入口）
DECLARED = [{"name": "paid", "kind": "signal"}, {"name": "new", "kind": "start", "entry": "apply"}]


async def _app(declared: list | None = None) -> str:
    async with SessionLocal() as session:
        app = App(name=f"付款-{uuid.uuid4().hex[:6]}", published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec={"hooks": DECLARED if declared is None else declared},
                               spec_hash="h" * 64, bindings={}))
        await session.commit()
        return app.id


async def _hook(app_id: str, kind: str, entry_id: str | None = None) -> tuple[str, str]:
    async with SessionLocal() as session:
        row, secret, _ = await hooks.rotate_hook(session, app_id, "paid" if kind == "signal" else "new", kind=kind,
                                                 entry=entry_id)
        await session.commit()
        return row.id, secret


def signed(secret: str, payload: dict, *, key: str | None, ts: int | None = None) -> tuple[dict, bytes]:
    body = json.dumps(payload).encode()
    stamp = str(ts if ts is not None else int(time.time()))
    headers = {"X-AgentLab-Timestamp": stamp, "X-AgentLab-Signature": f"sha256={sign(secret, stamp, body)}",
               "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    return headers, body


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(row.id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _waiting_run(app_id: str, order: str) -> str:
    graph = {"nodes": [node("start", "input", fields=[{"name": "order_id"}]),
                       node("wait", "human", mode="input", title="等付款回调",
                            assignee={"kind": "hook", "correlation": "{{ input.order_id }}"}),
                       node("out", "output", fields=[{"name": "付款单", "value": "{{ nodes.wait.value.payment_id }}"}])],
             "edges": [{"source": "start", "target": "wait"}, {"source": "wait", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"order_id": order}, app_id=app_id, app_version=1)
    await _wait(run.id, ("interrupted",))
    return run.id


async def test_signal_resolves_the_waiting_task_once(client):
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "signal")
    order = f"ORD-{uuid.uuid4().hex[:6]}"
    run_id = await _waiting_run(app_id, order)
    async with SessionLocal() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run_id))).scalar_one()
    assert task.kind == "signal" and task.correlation_key == order

    payload = {"correlation": order, "payment_id": "PAY-9"}
    headers, body = signed(secret, payload, key="cb-1")
    first = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["task_id"] == task.id and not first.json().get("duplicate")
    assert (await _wait(run_id, ("succeeded", "failed"))).output.get("付款单") == "PAY-9"

    headers, body = signed(secret, payload, key="cb-1")       # 重复投递：同一个幂等键
    again = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["task_id"] == task.id
    async with SessionLocal() as session:
        resumed = (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                                                RunEvent.type == "run.resumed"))).scalars().all()
        receipts = (await session.execute(select(HookReceipt).where(
            HookReceipt.hook_id == hook_id, HookReceipt.status != "signature"))).scalars().all()
    assert len(resumed) == 1
    assert [(r.idem_key, r.status) for r in receipts] == [("cb-1", "accepted")]


async def test_signature_window_and_idempotency_key_are_required(client):
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "signal")
    payload = {"correlation": "nope"}

    headers, body = signed("wrong-secret", payload, key="k")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 401
    headers, body = signed(secret, payload, key="k", ts=int(time.time()) - 301)
    stale = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert stale.status_code == 401 and "时间窗" in stale.json()["detail"]
    headers, body = signed(secret, payload, key=None)
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 400
    tampered_headers, body = signed(secret, payload, key="k")
    tampered = await client.post(f"/api/hooks/{hook_id}", content=body.replace(b"nope", b"nopf"),
                                 headers=tampered_headers)
    assert tampered.status_code == 401
    assert (await client.post("/api/hooks/missing", content=body, headers=tampered_headers)).status_code == 404


async def test_a_rejected_signal_can_be_retried_with_the_same_key(client):
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "signal")
    order = f"ORD-{uuid.uuid4().hex[:6]}"
    headers, body = signed(secret, {"correlation": order, "payment_id": "PAY-1"}, key="early")
    early = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert early.status_code == 404                    # 回调比流程走到这一步还早
    run_id = await _waiting_run(app_id, order)
    headers, body = signed(secret, {"correlation": order, "payment_id": "PAY-1"}, key="early")
    retried = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert retried.status_code == 200, retried.text
    assert (await _wait(run_id, ("succeeded", "failed"))).status == "succeeded"


async def test_start_hook_launches_through_the_entry_with_origin_hook(client, monkeypatch):
    from app.apps import entries

    calls: list[dict] = []

    async def start_entry(app_id, entry_id, values, principal, *, origin_kind="app_entry", hook=None, hook_key=None):
        calls.append({"app_id": app_id, "entry": entry_id, "values": values, "origin_kind": origin_kind,
                      "hook": hook, "hook_key": hook_key, "verified": principal.verified})
        return SimpleNamespace(id="run-from-hook")

    monkeypatch.setattr(entries, "start_entry", start_entry)
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "start", entry_id="apply")
    headers, body = signed(secret, {"amount": 12}, key="new-1")
    resp = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert resp.status_code == 200 and resp.json()["run_id"] == "run-from-hook"
    assert calls == [{"app_id": app_id, "entry": "apply", "values": {"amount": 12}, "origin_kind": "hook",
                      "hook": "new", "hook_key": "new-1", "verified": True}]
    headers, body = signed(secret, {"amount": 12}, key="new-1")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).json()["duplicate"] is True
    assert len(calls) == 1


async def test_a_captured_request_cannot_be_replayed_under_another_key(client, monkeypatch):
    """签名里没有幂等键：同一个签名只认头一回带的那个键。原样重发照常去重，换个键重放拒绝。"""
    from app.apps import entries

    calls: list[str] = []

    async def start_entry(app_id, entry_id, values, principal, **kw):
        calls.append(kw["hook_key"])
        return SimpleNamespace(id=f"run-{len(calls)}")

    monkeypatch.setattr(entries, "start_entry", start_entry)
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "start", entry_id="apply")
    headers, body = signed(secret, {"amount": 5}, key="pay-1")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 200
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).json()["duplicate"] is True
    replay = await client.post(f"/api/hooks/{hook_id}", content=body, headers={**headers, "Idempotency-Key": "pay-2"})
    assert replay.status_code == 401 and "签名已经用过" in replay.json()["detail"]
    assert calls == ["pay-1"]


async def test_a_receipt_stuck_in_processing_can_be_reclaimed_without_starting_twice(client, monkeypatch):
    """处理中途服务停了：回执停在 processing。超过 STALE_S 之后同一个键可以重新认领；实例其实已经发起的，交回它。"""
    from datetime import timedelta

    from app.apps import entries
    from app.db.base import utcnow

    calls: list[str] = []

    async def start_entry(app_id, entry_id, values, principal, **kw):
        calls.append(kw["hook_key"])
        return SimpleNamespace(id="run-new")

    monkeypatch.setattr(entries, "start_entry", start_entry)
    app_id = await _app()
    hook_id, secret = await _hook(app_id, "start", entry_id="apply")
    async with SessionLocal() as session:
        session.add(HookReceipt(hook_id=hook_id, idem_key="fresh", status="processing"))
        session.add(HookReceipt(hook_id=hook_id, idem_key="stuck", status="processing",
                                received_at=utcnow() - timedelta(seconds=hooks.STALE_S + 5)))
        session.add(Run(workflow_name="实例", run_class="formal", app_id=app_id, origin_kind="hook",
                        origin={"hook": "new", "hook_key": "stuck"}, id="run-before-crash"))
        await session.commit()
    headers, body = signed(secret, {"amount": 1}, key="fresh")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 409
    headers, body = signed(secret, {"amount": 2}, key="stuck")
    resp = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert resp.status_code == 200 and resp.json()["run_id"] == "run-before-crash"
    assert calls == []


async def test_hook_management_functions():
    """按名字一行（D-g）：生成、轮换都落在同一行上，地址（行 id）不变、密钥换掉；删掉按名字删。"""
    app_id = await _app()
    async with SessionLocal() as session:
        with pytest.raises(ValueError):
            await hooks.rotate_hook(session, app_id, "x", kind="nope")
        row, secret, created = await hooks.rotate_hook(session, app_id, "paid", kind="signal")
        await session.commit()
        assert created and row.secret != secret and len(secret) >= 32       # 库里是密文
        again, second, created = await hooks.rotate_hook(session, app_id, "paid", kind="signal")
        await session.commit()
        assert not created and again.id == row.id and second != secret
        assert (await hooks.hook_row(session, app_id, "paid")).id == row.id
        assert await hooks.delete_hook(session, "other-app", "paid") is False
        assert await hooks.delete_hook(session, app_id, "paid") is True
        await session.commit()
        assert await hooks.hook_row(session, app_id, "paid") is None


async def test_one_name_one_row_even_under_races(client, monkeypatch):
    """库里还没有 (app_id, name) 的唯一约束：并发生成只留一行；插入后发现别人抢先插了，删掉自己、改去轮换那一行；万一
    多出一行，收请求只认最早那一行，停用按名字改全部行。"""
    from app.db.base import utcnow
    from app.db.models import AppHook

    app_id = await _app()
    async with SessionLocal() as session:
        (await session.get(App, app_id)).draft = {"hooks": DECLARED}
        await session.commit()
    first, second = await asyncio.gather(client.post(f"/api/apps/{app_id}/hooks/paid/rotate"),
                                         client.post(f"/api/apps/{app_id}/hooks/paid/rotate"))
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"] and first.json()["secret"] != second.json()["secret"]
    async with SessionLocal() as session:
        rows = list((await session.execute(select(AppHook).where(AppHook.app_id == app_id))).scalars())
    assert len(rows) == 1

    # 另一个进程在「读不到行」和「插入」之间抢先插好了：这一次插入后再查，删掉自己、轮换最早那一行
    real = hooks.hook_row
    calls = {"n": 0}

    async def stale_first_read(session, app, name):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(session, app, name)

    monkeypatch.setattr(hooks, "hook_row", stale_first_read)
    async with SessionLocal() as session:
        row, secret, created = await hooks.rotate_hook(session, app_id, "paid", kind="signal")
        await session.commit()
    monkeypatch.setattr(hooks, "hook_row", real)
    assert not created and row.id == rows[0].id
    async with SessionLocal() as session:
        assert len(list((await session.execute(select(AppHook).where(AppHook.app_id == app_id))).scalars())) == 1

    # 真的多出一行（多进程各自插入）：它的密钥收不到请求；停用两行一起停
    from app.core.crypto import encrypt

    async with SessionLocal() as session:
        stray = AppHook(app_id=app_id, name="paid", kind="signal", enabled=True, secret=encrypt("stray-secret") or "",
                        created_at=utcnow())
        session.add(stray)
        await session.commit()
        stray_id = stray.id
    headers, body = signed("stray-secret", {"correlation": "x"}, key="k-stray")
    assert (await client.post(f"/api/hooks/{stray_id}", content=body, headers=headers)).status_code == 404
    assert (await client.patch(f"/api/apps/{app_id}/hooks/paid", json={"enabled": False})).status_code == 200
    async with SessionLocal() as session:
        states = [r.enabled for r in (await session.execute(select(AppHook).where(AppHook.app_id == app_id))).scalars()]
    assert states == [False, False]


async def test_a_hook_works_by_what_the_published_version_declares(client, monkeypatch):
    """D-g：钩子做什么按应用当前发布版的声明认，不按行上记的。发布版里没声明的、停用的拒收；行上记的入口和发布版不一样时，
    以发布版为准。"""
    calls: list[str] = []

    async def start_entry(app_id, entry_id, values, principal, **kw):
        calls.append(entry_id)
        return SimpleNamespace(id=f"run-{entry_id}")

    monkeypatch.setattr("app.apps.entries.start_entry", start_entry)
    app_id = await _app(declared=[{"name": "new", "kind": "start", "entry": "apply_v2"}])
    hook_id, secret = await _hook(app_id, "start", entry_id="apply")       # 行上记的是旧入口
    headers, body = signed(secret, {"amount": 1}, key="k-1")
    resp = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert calls == ["apply_v2"] and resp.json()["kind"] == "start"

    undeclared = await _app(declared=[])                                    # 只在草稿里、还没发布
    hook_id, secret = await _hook(undeclared, "signal")
    headers, body = signed(secret, {"correlation": "x"}, key="k-2")
    resp = await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)
    assert resp.status_code == 404 and "发布版本里没有声明" in resp.json()["detail"]
    headers, body = signed("wrong", {"correlation": "x"}, key="k-3")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 401, \
        "没验过签名的请求不该探出声明"

    async with SessionLocal() as session:
        row = await hooks.hook_row(session, app_id, "new")
        row.enabled = False
        await session.commit()
        hook_id = row.id
    headers, body = signed(secret, {"amount": 1}, key="k-4")
    assert (await client.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 404


def test_outbound_generic_signature_verifies_inbound():
    """出站通用格式的签名和入站验的是同一套：接收端拿同一个密钥能验过。"""
    from app.notify import core

    msg = core.Message(title="测试")
    _, headers, body = core.build_request("generic", "https://hooks.example.com/in", "s3cret", msg,
                                          generic={"event": "test"})
    hooks.verify("s3cret", headers, body)
    with pytest.raises(hooks.HookRejected):
        hooks.verify("other", headers, body)
