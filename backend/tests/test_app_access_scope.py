"""应用详情按授权给（D-m），应用编辑器的助手对话（D-e）。

- GET /api/apps/{id}：只有 manage 拿得到草稿和草稿里的岗位绑定；use / observe 只拿概况和当前发布版的页面、入口目录；
- GET /api/apps/{id}/assistant-conversation：manage 才能用，取或建这个人在这个应用上的助手对话（kind=app、app_id、
  created_by），同一个人再取是同一段，换个人是另一段；
- /api/copilot/app-stream：要这个应用的 manage；带的对话必须是这个应用的助手对话；历史按对话拼进提示（同问数据）。
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.api import apps as apps_api
from app.api.deps import Principal
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Conversation, ConversationTurn, Workflow, WorkflowVersion
from app.db.models_apps import AppGrant
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


GRAPH = {"nodes": [node("start", "input", "开始", fields=[{"name": "amount", "type": "number"}]),
                   node("out", "output", "成果", fields=[{"name": "r", "value": "{{ input.amount }}"}])],
         "edges": [{"source": "start", "target": "out"}]}


async def published_app(client) -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"报销-{uuid.uuid4().hex[:6]}", graph=GRAPH, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=GRAPH, graph_hash=graph_hash(GRAPH),
                                    level="published", published_at=utcnow()))
        await session.commit()
        wf_id = wf.id
    spec = {"format": "agentlab.app/1", "name": "报销",
            "pins": [{"alias": "main", "workflow_id": wf_id, "version": 1, "graph_hash": graph_hash(GRAPH),
                      "use": "entry"}],
            "entries": [{"id": "apply", "label": "发起报销", "pin": "main", "form": {"$ref": "#/forms/apply"},
                         "map": {"amount": "form.amount"}}],
            "forms": {"apply": {"type": "object", "properties": {"amount": {"type": "number", "title": "金额"}}}},
            "pages": [{"id": "home", "title": "首页", "body": [
                {"id": "new", "type": "entry_button", "props": {"entry": "apply"}}]}]}
    res = await client.post("/api/apps", json={"name": "报销", "spec": spec}, headers={"X-Actor": quote("建立人")})
    app_id = res.json()["id"]
    assert (await client.post(f"/api/apps/{app_id}/publish", json={})).json()["ok"]
    return app_id


def as_user(monkeypatch, name: str = "carol", role: str = "user") -> Principal:
    who = Principal(username=name, role=role, verified=True, mode="local")
    monkeypatch.setattr(apps_api, "current_principal", lambda: who)
    return who


async def grant(app_id: str, who: str, role: str) -> None:
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=app_id, principal_kind="user", principal=who, role=role))
        await session.commit()


# --------------------------------------------------------------------------
# D-m
# --------------------------------------------------------------------------


async def test_only_managers_get_the_draft(client, monkeypatch):
    app_id = await published_app(client)
    managed = (await client.get(f"/api/apps/{app_id}")).json()
    assert managed["my_role"] == "manage" and managed["draft"]["entries"][0]["id"] == "apply"
    assert managed["draft_bindings"] == {"slots": {}} and managed["created_by"] == "建立人"
    outline = {"version": 1, "pages": [{"id": "home", "title": "首页"}], "entries": [{"id": "apply", "label": "发起报销"}]}
    assert managed["published"] == outline

    as_user(monkeypatch)
    await grant(app_id, "carol", "observe")
    seen = (await client.get(f"/api/apps/{app_id}")).json()
    assert seen["my_role"] == "observe" and seen["published"] == outline
    for key in ("draft", "draft_bindings", "created_by", "backfill"):
        assert key not in seen, key


# --------------------------------------------------------------------------
# D-e
# --------------------------------------------------------------------------


async def test_assistant_conversation_is_per_app_and_per_person(client, monkeypatch):
    app_id = await published_app(client)
    first = (await client.get(f"/api/apps/{app_id}/assistant-conversation", headers={"X-Actor": quote("张三")})).json()
    assert (first["kind"], first["app_id"], first["created_by"], first["turns"]) == ("app", app_id, "张三", [])
    again = (await client.get(f"/api/apps/{app_id}/assistant-conversation", headers={"X-Actor": quote("张三")})).json()
    assert again["id"] == first["id"]
    other = (await client.get(f"/api/apps/{app_id}/assistant-conversation", headers={"X-Actor": quote("李四")})).json()
    assert other["id"] != first["id"] and other["created_by"] == "李四"

    as_user(monkeypatch)
    await grant(app_id, "carol", "use")
    assert (await client.get(f"/api/apps/{app_id}/assistant-conversation")).status_code == 403


async def test_concurrent_requests_share_one_assistant_conversation(client):
    """编辑器开发模式下 effect 执行两次、同一个人开两个标签页：同时来取的只建一段，历史不被拆开。"""
    import asyncio

    from sqlalchemy import select

    app_id = await published_app(client)
    headers = {"X-Actor": quote("王五")}
    replies = await asyncio.gather(*(client.get(f"/api/apps/{app_id}/assistant-conversation", headers=headers)
                                     for _ in range(3)))
    assert len({r.json()["id"] for r in replies}) == 1
    async with SessionLocal() as session:
        rows = (await session.execute(select(Conversation.id).where(
            Conversation.kind == "app", Conversation.app_id == app_id, Conversation.created_by == "王五"))).all()
    assert len(rows) == 1


class Script:
    """按行吐操作流的假模型，记下每次收到的消息。"""

    def __init__(self, *ops: dict) -> None:
        self.lines = [json.dumps(o, ensure_ascii=False) for o in ops]
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(list(messages))
        for line in self.lines:
            yield AIMessageChunk(content=line + "\n")


async def _stream(client, monkeypatch, body: dict, model: Script | None = None) -> tuple[int, list[dict]]:
    import app.api.copilot as copilot
    from app.apps import lint

    async def _model(*_a, **_k):
        return model, "mock-fast"

    async def _lint(session, spec, bindings, **_):
        return []

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    monkeypatch.setattr(lint, "lint_app", _lint)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/app-stream", json=body) as r:
        if r.status_code != 200:
            return r.status_code, [json.loads(await r.aread())]
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return 200, events


async def test_app_stream_reads_the_apps_own_conversation(client, monkeypatch):
    app_id = await published_app(client)
    other_app = await published_app(client)
    conv = (await client.get(f"/api/apps/{app_id}/assistant-conversation")).json()
    async with SessionLocal() as session:
        session.add(ConversationTurn(conversation_id=conv["id"], seq=1, question="按钮文字写成我要报销",
                                     explanation="改了发起按钮的文字", status="done"))
        chat = Conversation(kind="chat", title="问数据")
        session.add(chat)
        await session.commit()
        chat_id = chat.id
    foreign = (await client.get(f"/api/apps/{other_app}/assistant-conversation")).json()

    status, body = await _stream(client, monkeypatch, {"instruction": "改标题", "app_id": app_id,
                                                        "conversation_id": chat_id})
    assert status == 409 and "不是这个应用的助手对话" in body[0]["detail"]
    status, _ = await _stream(client, monkeypatch, {"instruction": "改标题", "app_id": app_id,
                                                     "conversation_id": foreign["id"]})
    assert status == 409

    model = Script({"op": "plan", "summary": "改首页标题"}, {"op": "app_page", "page": {"id": "home", "title": "报销"}},
                   {"op": "done", "explanation": "首页标题改成了报销"})
    status, events = await _stream(client, monkeypatch, {"instruction": "首页标题写成报销", "app_id": app_id,
                                                          "conversation_id": conv["id"]}, model)
    assert status == 200 and events[-1]["op"] == "final", events
    human = model.calls[0][1][1]
    assert "这次对话之前聊过的" in human and "按钮文字写成我要报销" in human and "改了发起按钮的文字" in human
    assert events[-1]["spec"]["pages"][0]["title"] == "报销"


async def test_app_stream_needs_manage(client, monkeypatch):
    app_id = await published_app(client)
    import app.api.deps as deps

    bob = Principal(username="bob", role="builder", verified=True, mode="local")
    monkeypatch.setattr(deps, "current_principal", lambda: bob)
    status, body = await _stream(client, monkeypatch, {"instruction": "改标题", "app_id": app_id})
    assert status == 403 and "管理" in body[0]["detail"]
    # 有 manage 授权的搭建者能用
    await grant(app_id, "bob", "manage")
    model = Script({"op": "reply", "text": "好的"})
    status, events = await _stream(client, monkeypatch, {"instruction": "你好", "app_id": app_id}, model)
    assert status == 200 and any(e["op"] == "reply" for e in events)
