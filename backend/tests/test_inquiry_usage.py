"""审批质询的用量计进运行用量。

质询不在图里执行、不发 llm.end，它花的 token 和金额写在 approval.inquiry 事件数据的 usage 里
（input_tokens、output_tokens、cost_usd、calls，可带 unpriced_calls）。以前运行用量只认图的状态和 llm.end，
质询花的钱在运行页上看不见。

- 运行结束（成功、失败）时：图的用量照旧（成功取状态、其余两本账取大），再加上质询的；明细另放在 inquiry 下
- 停在审批时被放弃：停下时记的用量里还没有这次停下期间的质询，放弃时补上
- 停过两次、两次都问过：每次质询只计一遍
- 没有质询的运行，用量和以前逐字相同
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import compiler
from app.engine.context import NodeError
from app.engine.runner import run_manager
from app.engine.schema import NodeType

A = {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.0123, "calls": 2}
B = {"input_tokens": 800, "output_tokens": 100, "cost_usd": 0.004, "calls": 1, "unpriced_calls": 1}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def gated(*middle, gates: int = 1) -> dict:
    nodes = [node("start", "input"), *middle]
    nodes += [node(f"gate{i}", "human", mode="approve", title=f"第 {i + 1} 道审批") for i in range(gates)]
    nodes.append(node("out", "output", fields=[{"name": "r", "value": "完成"}]))
    return chain(*nodes)


def ask(question: str, usage: dict | None) -> tuple:
    data = {"question": question, "answer": "按订单表核对一致"}
    if usage is not None:
        data["usage"] = usage
    return (EventType.APPROVAL_INQUIRY, None, data)


async def _row(run_id: str) -> Run:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


async def _until(run_id: str, statuses: tuple[str, ...]) -> Run:
    for _ in range(500):
        row = await _row(run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return await _row(run_id)
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def _ask_while_paused(run_id: str, *items: tuple) -> None:
    from app.engine.run_append import append_events

    assert await append_events(run_id, list(items), require="interrupted"), "质询没写进运行"


def _tokens(usage: dict) -> tuple:
    return (usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"),
            usage.get("calls"), usage.get("unpriced_calls"))


# --------------------------------------------------------------------------
# 按事件加总
# --------------------------------------------------------------------------


async def test_usage_of_events_keeps_inquiries_apart():
    """llm.end 照旧逐次加总；质询的另记在 inquiry 下（_finalize 比完两本账再加，见下面几条）。"""
    from app.engine.runner import _usage_of_events

    async with SessionLocal() as session:
        run = Run(workflow_name="用量加总", status="succeeded", graph={}, input={})
        session.add(run)
        await session.flush()
        rows = [
            ("llm.end", {"model": "claude-sonnet-4-5", "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001,
                         "calls": 1}),
            ("llm.end", {"model": "claude-sonnet-4-5", "input_tokens": 50, "output_tokens": 5, "cost_usd": 0.0005}),
            ("approval.inquiry", {"question": "甲", "usage": A}),
            ("approval.inquiry", {"question": "乙", "usage": B}),
            ("approval.inquiry", {"question": "没问模型就答了"}),
            ("approval.inquiry", {"question": "写坏的用量", "usage": "很多"}),
            # 别的事件里的金额不算：按需裁判的判定另有自己的账
            ("evidence.judged", {"cost_usd": 9.9, "calls": 9}),
        ]
        for seq, (kind, data) in enumerate(rows, start=1):
            session.add(RunEvent(run_id=run.id, seq=seq, type=kind, node_id=None, ts=float(seq), data=data))
        await session.commit()
        got = await _usage_of_events(session, run.id)
    assert {k: got[k] for k in ("input_tokens", "output_tokens", "calls")} == {
        "input_tokens": 150, "output_tokens": 25, "calls": 2}
    assert got["cost_usd"] == pytest.approx(0.0015)
    assert got["inquiry"] == {"input_tokens": 2000, "output_tokens": 400, "cost_usd": pytest.approx(0.0163),
                              "calls": 3, "unpriced_calls": 1}


async def test_without_inquiries_there_is_no_inquiry_key():
    from app.engine.runner import _usage_of_events

    async with SessionLocal() as session:
        run = Run(workflow_name="没有质询", status="succeeded", graph={}, input={})
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type="llm.end", node_id="ask", ts=1.0,
                             data={"model": "claude-sonnet-4-5", "input_tokens": 10, "output_tokens": 2,
                                   "cost_usd": 0.0001, "calls": 1}))
        await session.commit()
        got = await _usage_of_events(session, run.id)
    assert set(got) == {"input_tokens", "output_tokens", "cost_usd", "calls"}


# --------------------------------------------------------------------------
# 运行用量
# --------------------------------------------------------------------------


async def test_a_succeeded_run_counts_the_inquiry():
    run = await run_manager.start(graph=gated(), input_payload={})
    await _until(run.id, ("interrupted",))
    await _ask_while_paused(run.id, ask("销售额为什么涨了", A))
    await run_manager.resume(run.id, {"approved": True})
    row = await _until(run.id, ("succeeded",))
    # 图里没有模型调用，用量全是这次质询的
    assert _tokens(row.usage) == (1200, 300, 1500, 2, None)
    assert row.usage["cost_usd"] == pytest.approx(0.0123)
    assert row.usage["inquiry"] == A
    assert row.usage["wall_ms"] >= row.usage["active_ms"]


async def test_the_inquiry_is_added_on_top_of_the_model_calls():
    """图里有模型调用：成功的运行取状态里的用量，再加上质询的。"""
    graph = gated(node("ask", "llm", prompt="上周到店多少人"))
    run = await run_manager.start(graph=graph, input_payload={})
    await _until(run.id, ("interrupted",))
    await _ask_while_paused(run.id, ask("到店人数怎么算的", B))
    await run_manager.resume(run.id, {"approved": True})
    row = await _until(run.id, ("succeeded",))
    async with SessionLocal() as session:
        ends = [e.data for e in (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "llm.end"))).scalars()]
    assert ends, "图里的模型调用没有记 llm.end"
    model_in = sum(e["input_tokens"] for e in ends)
    model_out = sum(e["output_tokens"] for e in ends)
    unpriced = sum(1 for e in ends if e.get("unpriced"))
    assert row.usage["input_tokens"] == model_in + B["input_tokens"]
    assert row.usage["output_tokens"] == model_out + B["output_tokens"]
    assert row.usage["total_tokens"] == model_in + model_out + B["input_tokens"] + B["output_tokens"]
    assert row.usage["calls"] == len(ends) + B["calls"]
    assert row.usage["unpriced_calls"] == unpriced + B["unpriced_calls"]
    assert row.usage["cost_usd"] == pytest.approx(sum(e["cost_usd"] for e in ends) + B["cost_usd"])


async def test_a_failed_run_counts_the_inquiry_too(monkeypatch):
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def boom(state, ctx):
        if ctx.node.id == "boom":
            raise NodeError(ctx.node.id, "数据源暂时不可用")
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, boom)
    graph = chain(node("start", "input"), node("gate", "human", mode="approve", title="放行吗"),
                  node("boom", "transform", mode="template", template="x"),
                  node("out", "output", fields=[{"name": "r", "value": "x"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    await _until(run.id, ("interrupted",))
    await _ask_while_paused(run.id, ask("口径对吗", A))
    await run_manager.resume(run.id, {"approved": True})
    row = await _until(run.id, ("failed",))
    assert _tokens(row.usage) == (1200, 300, 1500, 2, None)
    assert row.usage["inquiry"] == A


async def test_each_inquiry_counts_once_across_two_pauses():
    run = await run_manager.start(graph=gated(gates=2), input_payload={})
    await _until(run.id, ("interrupted",))
    await _ask_while_paused(run.id, ask("第一问", A))
    await run_manager.resume(run.id, {"approved": True})
    await asyncio.sleep(0.05)
    paused = await _until(run.id, ("interrupted",))
    # 第二次停下时记的用量已经有第一问
    assert paused.usage["input_tokens"] == A["input_tokens"] and paused.usage["inquiry"] == A
    await _ask_while_paused(run.id, ask("第二问", B))
    await run_manager.resume(run.id, {"approved": True})
    row = await _until(run.id, ("succeeded",))
    assert _tokens(row.usage) == (2000, 400, 2400, 3, 1)
    assert row.usage["cost_usd"] == pytest.approx(0.0163)
    assert row.usage["inquiry"] == {"input_tokens": 2000, "output_tokens": 400, "cost_usd": pytest.approx(0.0163),
                                    "calls": 3, "unpriced_calls": 1}


@pytest.mark.parametrize("asked_before", [False, True], ids=["first-pause", "second-pause"])
async def test_abandoning_a_paused_run_counts_what_was_asked(asked_before):
    """停在审批时放弃：停下时记的用量里还没有这次停下期间的质询，放弃时补上；上一次停下时已经计过的不再计。"""
    run = await run_manager.start(graph=gated(gates=2 if asked_before else 1), input_payload={})
    await _until(run.id, ("interrupted",))
    if asked_before:
        await _ask_while_paused(run.id, ask("第一问", A))
        await run_manager.resume(run.id, {"approved": True})
        await asyncio.sleep(0.05)
        await _until(run.id, ("interrupted",))
    await _ask_while_paused(run.id, ask("最后一问", B))
    assert await run_manager.cancel(run.id) == "cancelled"
    row = await _until(run.id, ("cancelled",))
    if asked_before:
        assert _tokens(row.usage) == (2000, 400, 2400, 3, 1)
        assert row.usage["cost_usd"] == pytest.approx(0.0163)
    else:
        assert _tokens(row.usage) == (800, 100, 900, 1, 1) and row.usage["inquiry"] == B
    assert row.usage["wall_ms"] >= row.usage["active_ms"]


async def test_runs_without_inquiries_keep_their_usage():
    run = await run_manager.start(graph=gated(), input_payload={})
    await _until(run.id, ("interrupted",))
    await run_manager.resume(run.id, {"approved": True})
    row = await _until(run.id, ("succeeded",))
    assert set(row.usage) == {"duration_ms", "wall_ms", "active_ms", "wait_ms"}

    other = await run_manager.start(graph=gated(), input_payload={})
    await _until(other.id, ("interrupted",))
    assert await run_manager.cancel(other.id) == "cancelled"
    row = await _until(other.id, ("cancelled",))
    assert set(row.usage) == {"duration_ms", "wall_ms", "active_ms", "wait_ms"}
