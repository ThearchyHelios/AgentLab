"""耗时口径：经过审批恢复或接着跑的运行，耗时不能只算最后一段。

真实运行 2609b1c7：事件从头到尾约 87 秒，列表和页脚却写 22ms——每次 _drive
都把 started_at 改成"这一段的开始"，_finalize 又拿这一段的耗时覆盖 usage.duration_ms。
审批恢复和接着跑正是治理的主路径，所以凡是走过这条路的运行，耗时全是错的。

现在的口径：
- started_at 只在第一次开始时写；
- duration_ms（= active_ms）是各段执行时长之和；
- wall_ms 是从第一次开始到结束的墙钟；wait_ms 是等人审批的总时长；
- 三者随 run.finished / run.failed / run.cancelled 的 timing 一起发出。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import compiler
from app.engine.context import NodeError
from app.engine.runner import run_manager
from app.engine.schema import NodeType

SEGMENT = 0.3


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


@pytest.fixture
def slow_transforms(monkeypatch):
    """id 以 slow 开头的整形节点各卡 SEGMENT 秒：让每一段执行都有可量的长度。"""
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def runner(state, ctx):
        if ctx.node.id.startswith("slow"):
            await asyncio.sleep(SEGMENT)
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, runner)


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"等超时了：{run_id}")


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


def _same_instant(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) < 1e-3


APPROVAL_GRAPH = chain(
    node("start", "input", fields=[{"name": "question"}]),
    node("slow1", "transform", mode="template", template="第一段"),
    node("gate", "human", mode="approve", title="看一眼"),
    node("slow2", "transform", mode="template", template="第二段"),
    node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.slow2 }}"}]),
)

WAIT = 0.6


async def test_resumed_run_keeps_its_start_and_sums_every_segment(slow_transforms):
    run = await run_manager.start(graph=APPROVAL_GRAPH, input_payload={"question": "q"})
    paused = await _wait(run.id, ("interrupted", "failed"))
    assert paused.status == "interrupted", paused.error
    first_start = paused.started_at
    assert first_start is not None

    await asyncio.sleep(WAIT)                      # 人去倒了杯水
    await run_manager.resume(run.id, {"approved": True})
    done = await _wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error

    assert _same_instant(done.started_at, first_start), \
        f"恢复把 started_at 从 {first_start} 改成了 {done.started_at}"

    usage = done.usage
    # 两段各至少 SEGMENT 秒。只算最后一段的话这里只有一个 SEGMENT
    assert usage["duration_ms"] >= 2 * SEGMENT * 1000 * 0.9, usage
    assert usage["active_ms"] == usage["duration_ms"]
    assert WAIT * 1000 * 0.8 <= usage["wait_ms"] < usage["wall_ms"], usage
    assert usage["wall_ms"] >= usage["active_ms"] + usage["wait_ms"] - 50, usage
    wall = (done.finished_at - done.started_at).total_seconds() * 1000
    assert abs(usage["wall_ms"] - wall) < 100, (usage, wall)

    finished = next(e for e in await _events(run.id) if e.type == "run.finished")
    assert finished.data["timing"] == {
        "wall_ms": usage["wall_ms"], "active_ms": usage["active_ms"], "wait_ms": usage["wait_ms"],
    }


@pytest.fixture
def fails_once(monkeypatch):
    original = compiler.RUNNERS[NodeType.TRANSFORM]
    calls = {"n": 0}

    async def runner(state, ctx):
        if ctx.node.id.startswith("slow"):
            await asyncio.sleep(SEGMENT)
        if ctx.node.id == "flaky":
            calls["n"] += 1
            if calls["n"] == 1:
                raise NodeError(ctx.node.id, "第一次故意失败")
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, runner)
    return calls


async def test_continued_run_keeps_its_start_and_sums_every_segment(fails_once):
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("slow1", "transform", mode="template", template="前面这步"),
        node("flaky", "transform", mode="template", template="会挂一次"),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.flaky }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "q"})
    failed = await _wait(run.id, ("failed", "succeeded"))
    assert failed.status == "failed"
    first_start = failed.started_at
    assert failed.usage["duration_ms"] >= SEGMENT * 1000 * 0.9

    failed_ev = next(e for e in await _events(run.id) if e.type == "run.failed")
    assert set(failed_ev.data["timing"]) == {"wall_ms", "active_ms", "wait_ms"}
    assert failed_ev.data["node_id"] == "flaky"

    await run_manager.continue_failed(run.id)
    done = await _wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert _same_instant(done.started_at, first_start)
    # 第二段只跑了 flaky 和 out，很快。只算最后一段的话会远小于 SEGMENT
    assert done.usage["duration_ms"] >= SEGMENT * 1000 * 0.9, done.usage
    # 失败到接着跑之间不是在等审批
    assert done.usage["wait_ms"] == 0, done.usage


async def test_cancel_reports_timing_too(slow_transforms):
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("slow1", "transform", mode="template", template="x"),
        node("slow2", "transform", mode="template", template="y"),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "q"})
    await asyncio.sleep(SEGMENT / 2)
    assert await run_manager.cancel(run.id)
    await _wait(run.id, ("cancelled",))
    cancelled = next(e for e in await _events(run.id) if e.type == "run.cancelled")
    assert set(cancelled.data["timing"]) == {"wall_ms", "active_ms", "wait_ms"}
    assert cancelled.data["timing"]["active_ms"] > 0


async def test_long_output_says_it_was_clipped():
    """事件里的 output 为了控制体积会截断——截了就得说，消费方才知道要回源取全文。"""
    long_text = "很长的结论。" * 1500
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("out", "output", fields=[{"name": "answer", "value": "{{ input.question }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": long_text})
    done = await _wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    finished = next(e for e in await _events(run.id) if e.type == "run.finished")
    assert finished.data.get("output_truncated") is True
    assert len(finished.data["output"]["answer"]) < len(long_text)
    assert done.output["answer"] == long_text, "库里的是完整版本"

    short = await run_manager.start(graph=graph, input_payload={"question": "短的"})
    await _wait(short.id, ("succeeded",))
    finished = next(e for e in await _events(short.id) if e.type == "run.finished")
    assert not finished.data.get("output_truncated")


async def test_event_ts_is_when_it_happened_not_when_it_was_forwarded():
    async with SessionLocal() as session:
        run = Run(workflow_name="ts", status="running", graph={}, input={})
        session.add(run)
        await session.commit()
        run_id = run.id
    produced_at = time.time() - 5
    await run_manager._handle_custom(run_id, {
        "__agentlab__": True, "type": "log", "node_id": "x", "ts": produced_at,
        "data": {"message": "早先发生的事"},
    })
    [event] = await _events(run_id)
    assert abs(event.ts - produced_at) < 1e-6


async def test_node_started_says_which_iteration_and_whether_it_was_a_replay(slow_transforms):
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("lp", "loop", mode="foreach", items='["a", "b", "c"]', max_iterations=5),
            node("body", "transform", mode="template", template="{{ vars.item }}"),
            node("gate", "human", mode="approve", title="收尾前看一眼"),
            node("out", "output", fields=[{"name": "结果", "value": "ok"}]),
        ],
        "edges": [
            {"source": "start", "target": "lp"},
            {"source": "lp", "target": "body", "sourceHandle": "body"},
            {"source": "body", "target": "lp"},
            {"source": "lp", "target": "gate", "sourceHandle": "done"},
            {"source": "gate", "target": "out"},
        ],
    }
    run = await run_manager.start(graph=graph, input_payload={"question": "q"})
    await _wait(run.id, ("interrupted",))
    await run_manager.resume(run.id, {"approved": True})
    await _wait(run.id, ("succeeded",))

    events = await _events(run.id)
    body = [e.data.get("iteration") for e in events
            if e.type == "node.started" and e.node_id == "body"]
    assert body == [1, 2, 3]
    gate = [e.data for e in events if e.type == "node.started" and e.node_id == "gate"]
    assert len(gate) == 2
    assert not gate[0].get("resumed")
    assert gate[1].get("resumed") is True, "恢复后重放的那一次要标出来"
    out = [e.data for e in events if e.type == "node.started" and e.node_id == "out"]
    assert not out[0].get("resumed"), "恢复之后新走到的节点不是重放"
