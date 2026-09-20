"""协作团队：一轮可以同时派多个互不依赖的专家。

在此之前调度协议是 `next: string`——"一次只能派一个"被编进了协议本身，模型
再想并行也表达不出来。于是这个节点叫「多 Agent 协作」，实际是一条严格的串行
流水线：三个专家各花 10 秒，总是 30 秒。

改成 assignments 数组之后，互不依赖的任务同一轮发出去，总耗时按最慢的那个算。

**但并发只在任务真的互不依赖时才成立。** 同一轮发出去的专家拿到的是同一份
进展快照，谁也看不见谁这一轮干了什么。派错了不会报错，只会让两个人基于一样的
旧信息重复劳动——所以判断责任压在调度者身上，提示词里写了反例，而这里守的是
机制本身：说好并发就真的并发、串行的路径一步没坏、单个专家挂掉不拖垮整轮。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager

#: 每个专家假装干这么久。并发时整轮 ≈ 这个数，串行时 ≈ 这个数 × 人数
WORK_SECONDS = 0.4


@pytest.fixture
async def runner():
    await run_manager.setup()
    yield run_manager
    await run_manager.shutdown()


def _graph(*, max_parallel: int = 3, members=("researcher", "analyst", "writer")) -> dict:
    agents = [
        {"name": n, "description": f"{n} 的职责", "system": f"你是 {n}", "max_steps": 1}
        for n in members
    ]
    return {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "team", "type": "supervisor", "data": {"config": {
                "goal": "比较三种向量库",
                "agents": agents,
                "max_rounds": 4,
                "max_parallel": max_parallel,
            }}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [
            {"source": "in", "target": "team"},
            {"source": "team", "target": "out"},
        ],
    }


def script(monkeypatch, decisions: list[dict]) -> None:
    """让调度者按剧本派活，让专家干活花真实时间。

    专家那一边必须是**异步**等待：同步 sleep 会把事件循环堵死，于是并发测试
    永远"通过"（因为根本没并发过，墙钟也就成了串行时间）。
    """
    from app.providers import mock_model

    turns = {"n": 0}

    def _decide(self, messages):
        # response_format 有值 = 调度者在做结构化路由；没有 = 专家在干活
        if self.response_format:
            i = turns["n"]
            turns["n"] += 1
            payload = decisions[i] if i < len(decisions) else {"assignments": [], "done": True}
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return AIMessage(content="（干完了）")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        if not self.response_format:
            await asyncio.sleep(WORK_SECONDS)
        return self._generate(messages, stop, **kw)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", _agenerate)


async def _run(graph: dict):
    run = await run_manager.start(graph=graph, input_payload={"question": "比较三种向量库"})
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


def steps(events, kind):
    return [e.data for e in events if e.type == kind]


# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_independent_tasks_really_run_at_the_same_time(runner, monkeypatch):
    """这块改动的全部意义：说好并发就真的并发。

    用墙钟判定而不是看事件顺序——顺序只能说明"发出去了"，说明不了"同时跑"。
    """
    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "查 A 的资料"},
            {"agent": "analyst", "instruction": "查 B 的资料"},
        ],
        "reason": "两边互不相干",
    }, {"assignments": [], "done": True}])

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error

    starts = steps(events, "agent.step.start")
    assert {d["agent"] for d in starts} == {"researcher", "analyst"}
    assert all(d["parallel"] == 2 for d in starts), starts

    # 两个人都开跑了，第一个才结束——这是"同时"的事件层证据
    first_end = next(e.ts for e in events if e.type == "agent.step.end")
    assert all(e.ts <= first_end for e in events if e.type == "agent.step.start")

    # 而这是墙钟证据：串行要 2×WORK_SECONDS，并发约 1×
    sched = (run.output or {}).get("_schedule") or _schedule_of(events)
    assert sched, "调度信息要能拿得到"
    r0 = sched[0]
    assert r0["parallel"] == 2
    assert r0["wall_ms"] < WORK_SECONDS * 2000 * 0.8, f"看着像串行：{r0}"
    assert r0["sum_ms"] > r0["wall_ms"], "并发就该省出时间"


def _schedule_of(events):
    """schedule 在节点输出里，测试从事件流兜一份等价的出来。"""
    from collections import defaultdict
    rounds = defaultdict(lambda: {"agents": [], "parallel": 0, "wall_ms": 0, "sum_ms": 0})
    for d in (e.data for e in events if e.type == "agent.step.end"):
        r = rounds[d["round"]]
        r["round"] = d["round"]
        r["agents"].append(d["agent"])
        r["parallel"] = d.get("parallel", 1)
        r["wall_ms"] = max(r["wall_ms"], d["duration_ms"])
        r["sum_ms"] += d["duration_ms"]
    return [rounds[k] for k in sorted(rounds)]


@pytest.mark.asyncio
async def test_saved_time_is_measured_not_assumed(runner, monkeypatch):
    """界面要显示"并行省了 X 秒"，那个数必须是量出来的。

    两个人快慢不同时才区分得出来：按"整轮墙钟 × 人数"推算的话，快的那个会被
    记成和慢的一样长，省下的时间就被夸大了。所以让一个人明显更快，
    再看各自的耗时是不是真的不一样。
    """
    from app.providers import mock_model

    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "慢活"},
            {"agent": "analyst", "instruction": "快活"},
        ],
    }, {"assignments": [], "done": True}])

    async def _uneven(self, messages, stop=None, run_manager=None, **kw):
        if not self.response_format:
            text = " ".join(str(getattr(m, "content", "")) for m in messages)
            await asyncio.sleep(0.05 if "快活" in text else WORK_SECONDS)
        return self._generate(messages, stop, **kw)

    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", _uneven)

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error

    took = {d["agent"]: d["duration_ms"] for d in steps(events, "agent.step.end")}
    assert took["analyst"] < took["researcher"] / 2, f"快慢该有差别：{took}"
    # 整轮墙钟按最慢的那个算，而不是两个人加起来
    assert took["researcher"] < WORK_SECONDS * 2000 * 0.8, took


@pytest.mark.asyncio
async def test_dependent_tasks_stay_sequential(runner, monkeypatch):
    """分两轮派就该老老实实跑两轮——并行是可选项，不是强加的。"""
    script(monkeypatch, [
        {"assignments": [{"agent": "researcher", "instruction": "查资料"}]},
        {"assignments": [{"agent": "writer", "instruction": "根据资料写报告"}]},
        {"assignments": [], "done": True},
    ])

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error

    starts = steps(events, "agent.step.start")
    assert [d["agent"] for d in starts] == ["researcher", "writer"]
    assert all(d["parallel"] == 1 for d in starts)

    # 第二个人开跑必须在第一个人结束之后
    first_end = next(e.ts for e in events if e.type == "agent.step.end")
    second_start = [e for e in events if e.type == "agent.step.start"][1].ts
    assert second_start >= first_end


@pytest.mark.asyncio
async def test_max_parallel_caps_the_batch(runner, monkeypatch):
    """上限设成 1 就退回严格串行——给不放心的人一个明确的开关。"""
    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "a"},
            {"agent": "analyst", "instruction": "b"},
            {"agent": "writer", "instruction": "c"},
        ],
    }, {"assignments": [], "done": True}])

    events, run = await _run(_graph(max_parallel=1))
    assert run.status == "succeeded", run.error
    starts = steps(events, "agent.step.start")
    assert len(starts) == 1, "上限 1 就只该派一个"
    assert starts[0]["parallel"] == 1


@pytest.mark.asyncio
async def test_unknown_and_duplicate_agents_are_dropped(runner, monkeypatch):
    """模型偶尔会重复派同一个人或者编一个不存在的名字，两种在并发里都是纯浪费。"""
    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "a"},
            {"agent": "researcher", "instruction": "又派一次"},
            {"agent": "根本没这个人", "instruction": "c"},
        ],
    }, {"assignments": [], "done": True}])

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error
    starts = steps(events, "agent.step.start")
    assert [d["agent"] for d in starts] == ["researcher"]


@pytest.mark.asyncio
async def test_one_failing_member_does_not_sink_the_round(runner, monkeypatch):
    """并发里一个人挂了，另一个的成果不该跟着丢。"""
    from app.providers import mock_model

    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "会挂的那个"},
            {"agent": "analyst", "instruction": "正常的那个"},
        ],
    }, {"assignments": [], "done": True}])

    original = mock_model.MockChatModel._agenerate

    async def _boom(self, messages, stop=None, run_manager=None, **kw):
        text = " ".join(str(getattr(m, "content", "")) for m in messages)
        if "会挂的那个" in text:
            raise RuntimeError("这个专家炸了")
        return await original(self, messages, stop, run_manager, **kw)

    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", _boom)

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error

    # 失败要说出来，而不是安静地少一个人的产出
    warns = [d["message"] for d in steps(events, "log") if d.get("level") == "warn"]
    assert any("researcher" in w for w in warns), warns
    # 另一个人的结束事件照常有
    ends = {d["agent"] for d in steps(events, "agent.step.end")}
    assert "analyst" in ends


@pytest.mark.asyncio
async def test_schedule_is_reported_for_the_ui(runner, monkeypatch):
    """界面要画泳道，就得拿得到每轮谁干了活、并发几路、省了多少时间。"""
    script(monkeypatch, [{
        "assignments": [
            {"agent": "researcher", "instruction": "a"},
            {"agent": "analyst", "instruction": "b"},
        ],
    }, {
        "assignments": [{"agent": "writer", "instruction": "汇总"}],
    }, {"assignments": [], "done": True}])

    events, run = await _run(_graph())
    assert run.status == "succeeded", run.error

    rounds = sorted({d["round"] for d in steps(events, "agent.step.end")})
    assert rounds == [0, 1]
    by_round = {r: [d["agent"] for d in steps(events, "agent.step.end") if d["round"] == r]
                for r in rounds}
    assert set(by_round[0]) == {"researcher", "analyst"}
    assert by_round[1] == ["writer"]

    # 调度决策本身也要在事件流里，否则界面上这个节点就是个跑了 N 秒的黑盒
    dispatches = [d["message"] for d in steps(events, "log") if d.get("round") is not None]
    assert any("同时进行" in m for m in dispatches), dispatches
