"""身份断言的 sub 与 act（D-b；设计文档 A6 第 3 条）。

sub 一律是运行的发起人（Run.started_by）：这件事是替他办的；确认这次发送的人放在 act 声明（RFC 8693：
{"act": {"sub": 确认人}}）。没有确认人就不写 act。on_behalf 的老写法（字符串，就是发起人）照旧认。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import jwt
import pytest
from sqlalchemy import delete, select

from app.api.deps import Principal
from app.auth import signing
from app.capabilities import endpoints as ep
from app.db.base import SessionLocal
from app.db.models import Capability, CustomTool, Endpoint, Run, Task
from app.engine.runner import run_manager
from app.process import tasks
from tests.fakes.fake_erp import PAY, FakeErp, endpoint_row, install


def _claims(token: str, audience: str = "erp") -> dict:
    key = jwt.PyJWK(signing.jwks()["keys"][0]).key
    return jwt.decode(token, key, algorithms=["EdDSA"], audience=audience, issuer="agentlab")


def test_sub_is_the_initiator_and_act_is_the_confirmer():
    both = _claims(signing.assertion("erp", "alice", verified=True, actor="bob"))
    assert both["sub"] == "alice" and both["act"] == {"sub": "bob"} and both["verified"] is True
    alone = _claims(signing.assertion("erp", "alice", verified=False))
    assert alone["sub"] == "alice" and "act" not in alone
    service = _claims(signing.assertion("erp", None, verified=False))
    assert "sub" not in service and "act" not in service
    assert ep.on_behalf_parts("alice") == ("alice", None)
    assert ep.on_behalf_parts({"sub": "alice", "act": "bob"}) == ("alice", "bob")
    assert ep.on_behalf_parts({"sub": None, "act": None}) == (None, None)
    assert ep.on_behalf_parts(None) == (None, None)


async def test_the_platform_executor_signs_both(monkeypatch):
    sent: list[httpx.Request] = []
    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(lambda r: (sent.append(r), httpx.Response(201, json={}))[1]),
                    **kwargs)

    monkeypatch.setattr(ep, "_client", client)
    name = f"erp_pay_{uuid.uuid4().hex[:6]}"
    aud = f"erp-{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as s:
        endpoint = Endpoint(name=aud, environments={"prod": {"base_url": "http://127.0.0.1:9", "headers": {},
                                                               "allow_intranet": True}})
        s.add(endpoint)
        await s.flush()
        s.add(CustomTool(name=name, kind="http", parameters={"type": "object", "properties": {}},
                         config={"endpoint": endpoint.id, "method": "POST", "path": "/payments"}))
        cap = Capability(name=name, effect="write", credential_mode="service_on_behalf")
        s.add(cap)
        await s.commit()
        endpoint_id = endpoint.id
    try:
        async with SessionLocal() as s:
            cap = await s.get(Capability, name)
            out = await ep.endpoint_request(s, cap, {"amount": 1}, environment="prod", idem_key="k:1",
                                            on_behalf={"sub": "alice", "act": "bob"})
            assert out.status == 201, out.error
            claims = _claims(sent[-1].headers["x-agentlab-assertion"], aud)
            assert claims["sub"] == "alice" and claims["act"] == {"sub": "bob"}
            await ep.endpoint_request(s, cap, {"amount": 1}, environment="prod", idem_key="k:2", on_behalf="alice")
            claims = _claims(sent[-1].headers["x-agentlab-assertion"], aud)
            assert claims["sub"] == "alice" and "act" not in claims, "老写法：字符串就是发起人"
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(CustomTool).where(CustomTool.name == name))
            await s.execute(delete(Capability).where(Capability.name == name))
            await s.execute(delete(Endpoint).where(Endpoint.id == endpoint_id))
            await s.commit()


# --------------------------------------------------------------------------
# 调用工具节点：发起人和确认人分开交给执行器
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(run_id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def test_the_call_tool_node_passes_initiator_and_confirmer(engine_up, monkeypatch):
    from app.capabilities import registry

    erp = FakeErp()
    install(monkeypatch, erp, await endpoint_row(test=True), lookup=False)
    seen: list = []
    fake = registry.endpoint_request

    async def recording(session, capability, args, **kw):
        seen.append(kw.get("on_behalf"))
        return await fake(session, capability, args, **kw)

    monkeypatch.setattr(registry, "endpoint_request", recording)
    graph = {"nodes": [node("start", "input", fields=[{"name": "amount"}]),
                       node("pay", "tool", tool=PAY, args={"amount": "{{ input.amount }}", "card": "6222"}),
                       node("out", "output", fields=[{"name": "单号", "value": "{{ nodes.pay.receipt.id }}"}])],
             "edges": [{"source": "start", "target": "pay"}, {"source": "pay", "target": "out"}]}
    run = await run_manager.start(graph=graph, input_payload={"amount": 5}, run_class="formal", started_by="alice")
    await _wait(run.id, ("interrupted",))
    for _ in range(100):
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.run_id == run.id, Task.kind == "action_confirm",
                                                             Task.status == "pending"))).scalars().first()
        if task is not None:
            break
        await asyncio.sleep(0.05)
    bob = Principal(username="bob", role="admin", mode="off")
    card = (await tasks.detail(task.id, bob))["payload"]
    await tasks.respond(task.id, bob, decision="approve", payload_hash=card["payload_hash"])
    final = await _wait(run.id, ("succeeded", "failed"))
    assert final.status == "succeeded", final.error
    assert seen == [{"sub": "alice", "act": "bob"}], json.dumps(seen, ensure_ascii=False)
