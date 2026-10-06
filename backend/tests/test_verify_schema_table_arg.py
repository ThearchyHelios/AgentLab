"""核查 agent（engine/verify_agent.py）的 db_schema 认「整个写成一个字符串的 JSON 数组」的表名。

2026-10 验收：盲算、反方审稿的模型把 table 写成 '["orders", "stores"]'（一个字符串），工具只按逗号拆，回「中没有
["orders"」「中没有 "stores"]」，两张表明明都在；模型照原样重试、再失败，到第三轮才改成不传 table，白白多调了几轮
模型。现在第一次调用就拿到两张表的结构（tools/datasource.py 的 _schema_tables），模型看到的工具结果、时间线上的
tool.end 都是表结构。

模型是 MockChatModel 剧本，数据源是现造的 SQLite 文件，表名、数字都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.core.events import EventType
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine import verify_agent
from app.engine.verify_agent import run_verify_agent
from app.providers.factory import ModelSpec
from app.providers.mock_model import MockChatModel
from app.tools.registry import Versions

SCHEMA = {"type": "object", "properties": {"value": {"type": "number", "description": "包裹运费合计"}},
          "required": ["value"]}


@pytest.fixture
async def depots(tmp_path):
    """两张表：包裹（运费 12、8、5）和仓库。返回源名。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE parcels (id INTEGER PRIMARY KEY, depot_id INTEGER, fee INTEGER);"
                     "CREATE TABLE depots (id INTEGER PRIMARY KEY, zone TEXT);"
                     "INSERT INTO parcels VALUES (1, 1, 12), (2, 1, 8), (3, 2, 5);"
                     "INSERT INTO depots VALUES (1, '东区'), (2, '西区');")
    db.commit()
    db.close()
    name = f"depots_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


async def test_a_json_array_written_as_one_string_gets_both_tables_at_once(monkeypatch, depots):
    seen: list[list] = []
    steps = [[(f"db_schema__{depots}", {"table": '["parcels", "depots"]'}, "s1")],
             [(f"db_query__{depots}", {"sql": "SELECT SUM(fee) AS total FROM parcels"}, "c1")]]

    def decide(model, messages):
        seen.append(list(messages))
        if model.response_format:
            return AIMessage(content=json.dumps({"value": {"alias": "Q1", "row": 0, "column": "total"},
                                                 "rationale": "运费合计见 [[see:Q1]]。"}, ensure_ascii=False))
        if not steps:
            return AIMessage(content="查完了。")
        return AIMessage(content="查一下。", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in steps.pop(0)])

    async def fake_model(session, spec):
        return MockChatModel(model_name="mock-fast"), "mock-fast"

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    monkeypatch.setattr(verify_agent, "get_chat_model", fake_model)
    events: list[tuple] = []
    out = await run_verify_agent(spec=ModelSpec(model="mock-fast"), system="核对包裹运费。", task="算一遍包裹运费合计。",
                                 sources=[depots], output_schema=SCHEMA, data_versions=Versions.CURRENT,
                                 emit=lambda kind, **data: events.append((kind, data)), run_id="run-m4", node_id="dual")
    assert out.error is None and out.result["value"] == 25
    # 模型在循环里看到的那次工具结果：两张表的结构，没有「中没有」
    [answer] = [str(m.content) for messages in seen for m in messages
                if isinstance(m, ToolMessage) and m.tool_call_id == "s1"][:1]
    assert "表 parcels" in answer and "表 depots" in answer and "没有" not in answer
    # 时间线上那一条 tool.end 也是表结构
    [end] = [d for kind, d in events if kind == EventType.TOOL_END and d.get("call_id") == "s1"]
    assert "表 parcels" in end["preview"] and "没有" not in end["preview"]
