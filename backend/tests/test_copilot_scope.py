"""问数据时可以把这一轮限定在某几个数据源上。

问数据页的数据源标签要能「点一下，这次只查这个库」。以前助手每一轮都看得到
全部数据源，追问时还会沿用上一轮接的那个库——用户明明想换一个库问，得到的
还是旧库上的答案，而且说不清为什么。

限定之后：给模型看的工具和结构只剩这几个库；图里用了范围外的库，按自查的
规则交回去改，改不掉就不自动运行。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def sources(tmp_path):
    """两个库：scope_shop（orders）和 scope_sales（sales_daily）。"""
    made: dict[str, str] = {}
    async with SessionLocal() as session:
        for name, table in (("scope_shop", "orders"), ("scope_sales", "sales_daily")):
            path = tmp_path / f"{name}.db"
            db = sqlite3.connect(path)
            db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, amount REAL)")
            db.commit()
            db.close()
            row = DataSource(name=name, kind="sqlite", database=str(path), description=f"{table} 库",
                             schema_cache={"tables": {table: {"qualified": table, "columns": [
                                 {"name": "id", "type": "INTEGER"}, {"name": "amount", "type": "REAL"}]}}})
            session.add(row)
            await session.flush()
            made[name] = row.id
        await session.commit()
    yield made
    async with SessionLocal() as session:
        for source_id in made.values():
            row = await session.get(DataSource, source_id)
            if row:
                await session.delete(row)
        await session.commit()


class _Scripted:
    def __init__(self, first: list[dict], fix: list[dict] | None = None) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in (fix or [{"op": "done"}])]
        self.systems: list[str] = []
        self.humans: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.systems.append(next(t for r, t in messages if r == "system"))
        human = next(t for r, t in messages if r == "human")
        self.humans.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


def _graph_using(tool: str) -> list[dict]:
    return [
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "ask", "type": "agent", "label": "查库", "config": {
            "prompt": "{{ input.question }}", "tools": [tool], "assign_to": "answer"}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "ask"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {
            "fields": [{"name": "answer", "value": "{{ vars.answer }}"}]}}},
        {"op": "add_edge", "edge": {"source": "ask", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": True},
    ]


async def _stream(client, monkeypatch, model, body: dict) -> tuple[int, list[dict]]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        if r.status_code != 200:
            return r.status_code, [json.loads(await r.aread())]
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return 200, events


async def test_without_a_scope_the_model_sees_every_source(client, monkeypatch, sources):
    model = _Scripted(_graph_using("db_query__scope_shop"))
    await _stream(client, monkeypatch, model, {"instruction": "订单多少", "intent": "answer"})
    assert "db_query__scope_shop" in model.systems[0]
    assert "db_query__scope_sales" in model.systems[0]


async def test_a_scope_narrows_what_the_model_sees(client, monkeypatch, sources):
    model = _Scripted(_graph_using("db_query__scope_shop"))
    status, events = await _stream(client, monkeypatch, model, {
        "instruction": "订单多少", "intent": "answer", "datasource_ids": [sources["scope_shop"]],
    })
    assert status == 200
    system = model.systems[0]
    assert "db_query__scope_shop" in system and "orders" in system
    assert "db_query__scope_sales" not in system and "sales_daily" not in system
    assert "只查" in system and "scope_shop" in system, "限定要明说，不然模型会沿用上一轮的库"
    final = events[-1]
    assert final["op"] == "final" and final["autorun"] is True
    assert not [i for i in final["issues"] if i.get("code") == "datasource_out_of_scope"]


async def test_a_scope_can_name_sources_too(client, monkeypatch, sources):
    model = _Scripted(_graph_using("db_query__scope_sales"))
    await _stream(client, monkeypatch, model, {
        "instruction": "销售额", "intent": "answer", "datasource_ids": ["scope_sales"],
    })
    assert "db_query__scope_sales" in model.systems[0]
    assert "db_query__scope_shop" not in model.systems[0]


async def test_using_a_source_outside_the_scope_is_sent_back(client, monkeypatch, sources):
    fix = [{"op": "update_node", "id": "ask", "config": {"tools": ["db_query__scope_shop"]}},
           {"op": "done"}]
    model = _Scripted(_graph_using("db_query__scope_sales"), fix)
    _, events = await _stream(client, monkeypatch, model, {
        "instruction": "订单多少", "intent": "answer", "datasource_ids": [sources["scope_shop"]],
    })
    checks = [e for e in events if e.get("op") == "check"]
    assert [c["status"] for c in checks] == ["repairing", "passed"], checks
    out_of_scope = [i for i in checks[0]["issues"] if "scope_sales" in i["message"]]
    assert out_of_scope, checks[0]
    assert out_of_scope[0]["node_id"] == "ask"
    assert out_of_scope[0]["code"] == "datasource_out_of_scope", "和 final.issues 同一个 code"
    final = events[-1]
    ask = next(n for n in final["graph"]["nodes"] if n["id"] == "ask")
    assert ask["data"]["config"]["tools"] == ["db_query__scope_shop"]
    assert final["autorun"] is True


async def test_a_scope_that_is_never_honoured_is_not_run(client, monkeypatch, sources):
    model = _Scripted(_graph_using("db_query__scope_sales"))
    _, events = await _stream(client, monkeypatch, model, {
        "instruction": "订单多少", "intent": "answer", "datasource_ids": [sources["scope_shop"]],
    })
    final = events[-1]
    assert final["autorun"] is False
    out_of_scope = [i for i in final["issues"] if i.get("code") == "datasource_out_of_scope"]
    assert [i["node_id"] for i in out_of_scope] == ["ask"]
    assert "scope_sales" in out_of_scope[0]["message"] and "scope_shop" in out_of_scope[0]["message"]


async def test_a_scope_of_unknown_sources_is_refused_up_front(client, monkeypatch, sources):
    status, body = await _stream(client, monkeypatch, _Scripted([]), {
        "instruction": "订单多少", "intent": "answer", "datasource_ids": ["no_such_source"],
    })
    assert status == 400
    detail = body[0]["detail"]
    assert "数据源" in detail and "no_such_source" not in detail.split("：")[0]


async def test_the_one_shot_generate_honours_the_scope(client, monkeypatch, sources):
    import app.api.copilot as copilot

    seen: list = []

    class _OneShot:
        def with_structured_output(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            seen.append(messages)
            return {"nodes": [], "edges": []}

    async def _model(*_a, **_k):
        return _OneShot(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate", json={
        "instruction": "订单多少", "datasource_ids": [sources["scope_shop"]],
    })
    assert r.status_code == 200, r.text
    system = next(t for role, t in seen[0] if role == "system")
    assert "db_query__scope_shop" in system and "db_query__scope_sales" not in system
