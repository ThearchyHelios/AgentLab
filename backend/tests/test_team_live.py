"""协作团队的实时性：谁先做完谁先报到，调度者在想什么也要有事件。

以前成员的 agent.step.end 要等整轮 gather 结束才按派活顺序统一发出——并行时先做完
的成员一直显示"进行中"，直到最慢的那个做完；矩阵声称能回答的"谁还在跑"其实答
不了。调度者那次结构化模型调用前后没有任何事件，一个 74 秒的团队节点里约 66 秒
是调度者在决策，界面上是个黑盒。

用量也一样：agent 节点、成员、调度者的模型调用都不发 llm.end，界面只能等终态
才知道花了多少 token。补上之后必须守住另一头：run.finished 的 usage 是后端累计
的权威值，事件只是把同一笔账逐次摊开，加起来要对得上，不能记两遍。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager

SLOW, FAST = 0.6, 0.05
ROUTE_SECONDS = 0.15


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def _team_graph() -> dict:
    agents = [
        {"name": n, "description": f"{n} 的职责", "system": f"你是 {n}", "max_steps": 1}
        for n in ("researcher", "analyst")
    ]
    return {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "team", "type": "supervisor", "data": {"label": "协作团队", "config": {
                "goal": "比较两种向量库", "agents": agents, "max_rounds": 3, "max_parallel": 2,
            }}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [{"source": "in", "target": "team"}, {"source": "team", "target": "out"}],
    }


def _usage(messages, reply: str) -> dict:
    text = " ".join(str(getattr(m, "content", m)) for m in messages)
    return {"input_tokens": 10 + len(text) // 50, "output_tokens": 3 + len(reply) // 20,
            "total_tokens": 0}


def _script(monkeypatch, decisions: list[dict]) -> None:
    """调度者按剧本派活；成员 researcher 慢、analyst 快；每次调用都报 token。

    调度者走 with_structured_output(include_raw=True)：真实供应商会连原始消息
    一起给回来，用量就在那条消息上。mock 默认只给解析结果，这里补齐这层行为。
    """
    from app.providers import mock_model

    turns = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            i = turns["n"]
            turns["n"] += 1
            payload = decisions[i] if i < len(decisions) else {"assignments": [], "done": True}
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return AIMessage(content="（干完了）")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        text = " ".join(str(getattr(m, "content", "")) for m in messages)
        if self.response_format:
            await asyncio.sleep(ROUTE_SECONDS)
        else:
            await asyncio.sleep(FAST if "快活" in text else SLOW)
        result = self._generate(messages, stop, **kw)
        msg = result.generations[0].message
        msg.usage_metadata = _usage(messages, str(msg.content))
        return result

    def _structured(self, schema, *, include_raw=False, **kw):
        bound = self.model_copy(update={"response_format": schema})

        async def parse(msg):
            parsed = json.loads(str(msg.content))
            return {"raw": msg, "parsed": parsed, "parsing_error": None} if include_raw else parsed

        return bound | parse

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", _agenerate)
    monkeypatch.setattr(mock_model.MockChatModel, "with_structured_output", _structured)


async def _run(graph: dict) -> tuple[list[RunEvent], Run]:
    run = await run_manager.start(graph=graph, input_payload={"question": "比较"})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed", "cancelled"):
                break
    else:
        pytest.fail("运行没有在 20 秒内结束")
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return events, row


PARALLEL_ROUND = [{
    "assignments": [
        {"agent": "researcher", "instruction": "慢活"},
        {"agent": "analyst", "instruction": "快活"},
    ],
    "reason": "两边互不相干",
}, {"assignments": [], "done": True, "reason": "够了"}]


async def test_members_report_done_in_the_order_they_actually_finish(monkeypatch):
    _script(monkeypatch, PARALLEL_ROUND)
    events, run = await _run(_team_graph())
    assert run.status == "succeeded", run.error

    ends = [e for e in events if e.type == "agent.step.end"]
    assert [e.data["agent"] for e in ends] == ["analyst", "researcher"], \
        "快的那个先做完，就该先报到——而不是按派活顺序等整轮结束再一起报"

    start = next(e for e in events if e.type == "agent.step.start")
    fast_end = ends[0]
    # 快的那个结束事件的时刻要贴着它自己做完的时刻，而不是被慢的那个拖到整轮结束
    assert fast_end.ts - start.ts < (SLOW + FAST) / 2, \
        f"analyst {FAST}s 就做完了，结束事件却在 {fast_end.ts - start.ts:.2f}s 才出现"
    # 字段不变：界面照旧按这些字段画矩阵
    for e in ends:
        assert {"agent", "duration_ms", "round", "parallel", "preview"} <= set(e.data)


async def test_the_coordinator_is_not_a_black_box(monkeypatch):
    _script(monkeypatch, PARALLEL_ROUND)
    events, run = await _run(_team_graph())
    assert run.status == "succeeded", run.error

    starts = [e for e in events if e.type == "agent.route.start"]
    ends = [e for e in events if e.type == "agent.route.end"]
    assert [e.data["round"] for e in starts] == [0, 1]
    assert [e.data["round"] for e in ends] == [0, 1]
    assert all(e.node_id == "team" for e in starts + ends)

    first, last = ends
    assert first.data["agents"] == ["researcher", "analyst"]
    assert first.data["parallel"] == 2
    assert first.data["done"] is False
    assert first.data["reason"] == "两边互不相干"
    assert last.data["done"] is True and last.data["agents"] == []
    assert last.data["reason"] == "够了"
    for e in ends:
        assert e.data["duration_ms"] >= ROUTE_SECONDS * 1000 * 0.8, e.data

    # 先"开始想"，再"想好了"，然后才派人出去
    seq = {e.type + str(e.data.get("round", "")): e.seq for e in events}
    first_dispatch = next(e.seq for e in events if e.type == "agent.step.start")
    assert seq["agent.route.start0"] < seq["agent.route.end0"] < first_dispatch


async def test_every_model_call_reports_usage_and_the_sum_matches_the_run(monkeypatch):
    _script(monkeypatch, PARALLEL_ROUND)
    events, run = await _run(_team_graph())
    assert run.status == "succeeded", run.error

    calls = [e for e in events if e.type == "llm.end" and e.node_id == "team"]
    by_agent: dict[str, list[dict]] = {}
    for e in calls:
        by_agent.setdefault(e.data["agent"], []).append(e.data)
    assert set(by_agent) == {"调度者", "researcher", "analyst"}, by_agent.keys()
    assert len(by_agent["调度者"]) == 2, "调度了两次，就是两次模型调用"
    for d in (d for items in by_agent.values() for d in items):
        assert {"model", "input_tokens", "output_tokens", "cost_usd"} <= set(d), d
        assert d["input_tokens"] > 0 and d["output_tokens"] > 0, d

    finished = next(e for e in events if e.type == "run.finished")
    usage = finished.data["usage"]
    # 权威值是后端累计；逐次事件加起来必须等于它——多一笔少一笔都说明记重或漏记了
    assert usage["input_tokens"] == sum(e.data["input_tokens"] for e in calls)
    assert usage["output_tokens"] == sum(e.data["output_tokens"] for e in calls)
    assert usage["calls"] == len(calls)
    assert run.usage["input_tokens"] == usage["input_tokens"]


async def test_agent_node_reports_each_model_call(monkeypatch):
    """agent 节点以前只有 llm.start 没有 llm.end：用量要等整个节点结束才知道。"""
    from app.providers import mock_model

    async def _astream(self, messages, stop=None, run_manager=None, **kw):
        reply = "查完了，结论如下。"
        await asyncio.sleep(0.01)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=reply, usage_metadata=_usage(messages, reply)))

    monkeypatch.setattr(mock_model.MockChatModel, "_astream", _astream)
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "ag", "type": "agent", "data": {"label": "分析员", "config": {
                "prompt": "{{ input.question }}", "tools": [], "max_steps": 3}}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [{"source": "in", "target": "ag"}, {"source": "ag", "target": "out"}],
    }
    events, run = await _run(graph)
    assert run.status == "succeeded", run.error

    starts = [e for e in events if e.type == "llm.start" and e.node_id == "ag"]
    ends = [e for e in events if e.type == "llm.end" and e.node_id == "ag"]
    assert len(ends) == len(starts) >= 1
    assert all(e.data["agent"] == "分析员" for e in ends)
    usage = next(e for e in events if e.type == "run.finished").data["usage"]
    assert usage["input_tokens"] == sum(e.data["input_tokens"] for e in ends) > 0
    assert usage["output_tokens"] == sum(e.data["output_tokens"] for e in ends) > 0
