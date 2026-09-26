"""设置页的「运行默认值」必须真的被读到。

以前这三项只存在于 DEFAULT_SETTINGS 里：发起运行永远落在 "default"，
「危险工具默认需要人工确认」勾不勾都一样。用户改了默认知识库，以为以后的
运行都会用它——这比没有这个开关更糟，它给的是虚假的确定感。

这里守发起运行这一侧：请求没带的 memory_scope / collection 取设置里的值；
审批默认值的换算给引擎一个唯一的出处。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run, Setting
from app.main import app

GRAPH = {
    "nodes": [
        {"id": "a", "type": "input", "data": {"label": "入口", "config": {"fields": [{"name": "q"}]}}},
        {"id": "b", "type": "output", "data": {"label": "成果",
                                               "config": {"fields": [{"name": "x", "value": "{{ input.q }}"}]}}},
    ],
    "edges": [{"source": "a", "target": "b"}],
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean_run_settings():
    async def drop() -> None:
        async with SessionLocal() as session:
            row = await session.get(Setting, "run")
            if row:
                await session.delete(row)
                await session.commit()

    await drop()
    yield
    await drop()


@pytest.fixture
def started(monkeypatch):
    """截下交给 run_manager.start 的参数，不真的起执行任务（测试里没有 checkpointer）。"""
    from app.api import runs as runs_api

    captured: dict = {}

    async def fake_start(**kw):
        captured.update(kw)
        async with SessionLocal() as session:
            run = Run(workflow_name=kw.get("workflow_name", ""), status="queued",
                      graph=kw["graph"], input=kw["input_payload"])
            session.add(run)
            await session.commit()
            await session.refresh(run)
        return run

    monkeypatch.setattr(runs_api.run_manager, "start", fake_start)
    return captured


async def _put_run_settings(client, **values) -> None:
    base = {"default_memory_scope": "default", "default_collection": "default",
            "stream_tokens": True, "confirm_dangerous_tools": True}
    r = await client.put("/api/settings", json={"values": {"run": {**base, **values}}})
    assert r.status_code == 200


async def test_a_run_without_scope_uses_the_settings_defaults(client, started):
    await _put_run_settings(client, default_memory_scope="ops", default_collection="manuals")

    r = await client.post("/api/runs", json={"graph": GRAPH, "input": {"q": "x"}})
    assert r.status_code == 201, r.text
    assert started["memory_scope"] == "ops"
    assert started["collection"] == "manuals"


async def test_what_the_request_says_wins_over_the_defaults(client, started):
    await _put_run_settings(client, default_memory_scope="ops", default_collection="manuals")

    r = await client.post("/api/runs", json={"graph": GRAPH, "memory_scope": "team",
                                             "collection": "specs"})
    assert r.status_code == 201, r.text
    assert (started["memory_scope"], started["collection"]) == ("team", "specs")


async def test_blank_or_missing_settings_fall_back_to_default(client, started):
    r = await client.post("/api/runs", json={"graph": GRAPH})
    assert r.status_code == 201, r.text
    assert (started["memory_scope"], started["collection"]) == ("default", "default")

    # 设置页把输入框清空再保存，存下来的是空串——不能拿空串当记忆域
    await _put_run_settings(client, default_memory_scope="", default_collection="  ")
    r = await client.post("/api/runs", json={"graph": GRAPH})
    assert (started["memory_scope"], started["collection"]) == ("default", "default")


async def test_run_defaults_reads_through_to_the_stored_group(client):
    from app.api.settings import run_defaults

    await _put_run_settings(client, default_collection="manuals", confirm_dangerous_tools=False)
    async with SessionLocal() as session:
        values = await run_defaults(session)
    assert values["default_collection"] == "manuals"
    assert values["confirm_dangerous_tools"] is False
    # 没存过的键回落到内置默认，而不是 KeyError
    assert values["stream_tokens"] is True


def test_confirm_dangerous_tools_maps_onto_an_approval_policy():
    """引擎那边只认 approval 的取值，这里是唯一的换算出处。"""
    from app.api.settings import tool_approval_default

    assert tool_approval_default({"confirm_dangerous_tools": True}) == "dangerous"
    assert tool_approval_default({"confirm_dangerous_tools": False}) == "never"
    # 缺省、存坏了都按开着算：关审批必须是一次明确的选择
    assert tool_approval_default({}) == "dangerous"
    assert tool_approval_default({"confirm_dangerous_tools": None}) == "dangerous"
