"""Agent 的工具循环，跑真图验收。

单测能保证 split_tool_calls 分得对、prepare_args 纠得对，但保证不了它们在
run_agent 里被接对了。这里从 run_manager.start 进去、从落库的 RunEvent 出来，
验的是用户真正会看到的那条事件流。

两件必须守住的事：

- 每个 tool_use 都要有配对的 tool_result。少一条，下一次模型调用直接 400。
- 没执行的调用绝不能发 tool.start。前端靠 call_id 配对 start/end
  （frontend/src/run/decode.ts），一条没有结尾的 start 就是界面上一个永远转圈的卡片。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.runner import run_manager


@pytest.fixture
async def runner():
    """真图要跑起来就得有 checkpointer —— 平时是 FastAPI 的 lifespan 建的。"""
    await run_manager.setup()
    yield run_manager
    await run_manager.shutdown()


@pytest.fixture
async def shop(tmp_path):
    """一个真实的小库 + 一条 DataSource 记录，工具要能真的查到数。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL);")
    db.executemany("INSERT INTO orders(id,amount) VALUES(?,?)", [(i, i * 2.0) for i in range(1, 8)])
    db.commit()
    db.close()

    name = f"shop_{tmp_path.name[-6:].replace('-', '_')}"
    async with SessionLocal() as session:
        session.add(DataSource(
            name=name, kind="sqlite", database=str(path), readonly=True,
            description="测试库", options={}, schema_cache={}, enabled=True,
        ))
        await session.commit()
    return name, str(path)


def _graph(tool_name: str) -> dict:
    return {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "bot", "type": "agent", "data": {"config": {
                "prompt": "查一下订单总额",
                "tools": [tool_name],
                "approval": "never",      # 审批会挂起，这里要一路跑到底
                "max_steps": 3,
            }}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [
            {"source": "in", "target": "bot"},
            {"source": "bot", "target": "out"},
        ],
    }


async def _run_to_completion(graph: dict) -> list[RunEvent]:
    run = await run_manager.start(graph=graph, input_payload={"question": "订单总额"})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = (await session.execute(select(Run).where(Run.id == run.id))).scalar_one()
            if row.status in ("succeeded", "failed", "cancelled"):
                break
    else:
        pytest.fail("运行没有在 15 秒内结束")

    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return events, row


async def test_serial_loop_runs_one_tool_and_answers_every_call(runner, shop, monkeypatch) -> None:
    """模型一轮发两个工具调用：只跑第一个，但两个都要有回音。

    顺带把第一个的参数名写错成 query —— 这正是那条
    "_run() got an unexpected keyword argument 'query'" 的来源。
    """
    name, _ = shop
    tool_name = f"db_query__{name}"

    from app.providers import mock_model

    turns = {"n": 0}

    def _decide(self, messages):
        turns["n"] += 1
        if turns["n"] == 1:
            return AIMessage(
                content="先查一下。",
                tool_calls=[
                    # 参数名故意写错：能被纠正成 sql 才算过
                    {"name": tool_name, "args": {"query": "SELECT SUM(amount) AS total FROM orders"},
                     "id": "call_a"},
                    {"name": tool_name, "args": {"sql": "SELECT COUNT(*) FROM orders"},
                     "id": "call_b"},
                ],
            )
        return AIMessage(content="订单总额是 56。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)

    events, run = await _run_to_completion(_graph(tool_name))
    kinds = [(e.type, e.data) for e in events]

    assert run.status == "succeeded", [k for k, _ in kinds]

    # 一轮只跑了一个工具
    starts = [d for t, d in kinds if t == "tool.start"]
    assert len(starts) == 1, f"应该只有一次 tool.start，实际 {len(starts)} 次"
    assert starts[0]["call_id"] == "call_a"
    # 发出去的是纠正后的参数，不是模型原样那份
    assert "sql" in starts[0]["args"] and "query" not in starts[0]["args"]

    # 每个 tool.start 都有配对的结尾，界面上不会留下转圈的卡片
    ends = [d for t, d in kinds if t in ("tool.end", "tool.error")]
    assert {d["call_id"] for d in ends} == {"call_a"}

    # 纠正这件事说出来了，没有安静地把事办了
    warns = [d["message"] for t, d in kinds if t == "log" and d.get("level") == "warn"]
    assert any("query" in w and "sql" in w for w in warns), warns

    # 工具真的查到了数
    assert "56" in str([d.get("preview") for t, d in kinds if t == "tool.end"])


async def test_bad_args_feed_the_schema_back_instead_of_dying(runner, shop, monkeypatch) -> None:
    """参数全不认识时不执行，把"它接受什么"喂回去——模型下一步才改得对。"""
    name, _ = shop
    tool_name = f"db_query__{name}"

    from app.providers import mock_model

    seen: list[str] = []
    turns = {"n": 0}

    def _decide(self, messages):
        turns["n"] += 1
        if turns["n"] == 1:
            return AIMessage(
                content="查一下。",
                tool_calls=[{"name": tool_name, "args": {"foo": 1, "bar": 2}, "id": "call_x"}],
            )
        # 第二轮：把上一轮喂回来的内容记下来，这就是模型能看到的全部依据
        seen.extend(m.content for m in messages if isinstance(m, ToolMessage))
        return AIMessage(content="好的。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)

    events, run = await _run_to_completion(_graph(tool_name))
    assert run.status == "succeeded"

    assert seen, "参数错了也必须给模型一条 tool_result，否则下一轮直接 400"
    text = seen[0]
    assert "sql" in text and "必填" in text        # 说清楚该填什么
    assert "unexpected keyword argument" not in text  # 不再是那句天书

    # 没执行的调用不发 tool.start，界面上不会留下转不完的卡片
    assert not [e for e in events if e.type == "tool.start"]
    assert [e for e in events if e.type == "tool.error"]
