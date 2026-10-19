"""自我隔离（cluster/fence.py，复核 R-b1）：本进程卡住了、续不上租约，别的进程已经把它在跑的运行接手了——它缓过来之后不再写：
不写事件（不和接手的一边撞号）、不写断点、不写终态（不把接手的结果盖掉）。在一个进程里装出来：把「上次续上租约」拨到一个租约
时长以前，再把运行改成接手之后的样子。两种库都跑。"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.cluster import fence
from app.cluster.workers import lease_s
from app.db.base import SessionLocal, new_id
from app.db.models import Run, RunEvent
from app.engine import compiler
from app.engine.runner import run_manager
from app.engine.schema import NodeType


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    fence.activate()
    yield
    fence.deactivate()
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


GRAPH = {"nodes": [node("start", "input", fields=[{"name": "question"}]),
                   node("slow", "transform", mode="template", template="慢的这步"),
                   node("next", "transform", mode="template", template="后面这步"),
                   node("out", "output", fields=[{"name": "r", "value": "{{ nodes.next }}"}])],
         "edges": [{"source": "start", "target": "slow"}, {"source": "slow", "target": "next"},
                   {"source": "next", "target": "out"}]}


@pytest.fixture
def slow(monkeypatch):
    """slow 那一步：第一次跑时卡住，等测试放行（好在它卡着的时候装出「续不上租约、被接手」）；之后立刻返回。"""
    calls = {"n": 0}
    gate = asyncio.Event()
    entered = asyncio.Event()
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def runner(state, ctx):
        if ctx.node.id == "slow":
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                await gate.wait()
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, runner)
    return {"calls": calls, "gate": gate, "entered": entered}


async def _taken_over(run_id: str) -> None:
    """别的进程看它的租约过期了、接手：标中断、属主清空（同 run_manager.reap 做的）。"""
    from sqlalchemy import update

    async with SessionLocal() as session:
        await session.execute(update(Run).where(Run.id == run_id).values(
            status="interrupted", owner=None, error="服务重启，运行已挂起，可从断点继续运行"))
        await session.commit()


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                                           .order_by(RunEvent.seq))).scalars())


async def _no_repeated_seq(run_id: str) -> None:
    async with SessionLocal() as session:
        rows = (await session.execute(select(RunEvent.seq, func.count()).where(RunEvent.run_id == run_id)
                                      .group_by(RunEvent.seq).having(func.count() > 1))).all()
    assert rows == [], f"撞号：{rows}"


async def _checkpointed_before_slow(run_id: str) -> None:
    """等 slow 前面那一步的断点落了库（LangGraph 存断点和下一步并行：慢一点的库上 slow 已经开始、断点还在写）。"""
    config = {"configurable": {"thread_id": run_id}}
    for _ in range(300):
        tup = await run_manager.checkpointer.aget_tuple(config)
        if tup is not None and int((tup.metadata or {}).get("step", -1)) >= 1:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("slow 前面那一步的断点一直没落库")


async def _stall_and_take_over(slow) -> str:
    run = await run_manager.start(graph=GRAPH, input_payload={"question": "q"})
    await asyncio.wait_for(slow["entered"].wait(), timeout=15)
    await _checkpointed_before_slow(run.id)
    before = fence.generation()
    fence.renewed(time.monotonic() - lease_s() - 1)    # 卡了一个多租约时长
    await _taken_over(run.id)
    slow["gate"].set()                                 # 缓过来，手上的执行接着往下走
    for _ in range(200):
        if not run_manager.is_active(run.id):
            break
        await asyncio.sleep(0.05)
    assert not run_manager.is_active(run.id), "执行停了"
    assert fence.generation() == before + 1, "发现续晚了：隔离了一次"
    return run.id


async def test_a_stalled_executor_does_not_overwrite_the_takeover(slow):
    run_id = await _stall_and_take_over(slow)
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
    assert row.status == "interrupted" and row.owner is None, "接手之后的样子没被盖掉"
    events = await _events(run_id)
    assert not [e for e in events if e.type in ("run.finished", "run.failed", "run.cancelled")]
    assert not [e for e in events if e.node_id == "next"], "缓过来之后一条都没写"
    await _no_repeated_seq(run_id)


async def test_a_run_continued_elsewhere_finishes_once_and_the_stalled_side_stays_quiet(slow):
    run_id = await _stall_and_take_over(slow)
    fence.renewed()                                    # 换了身份重新登记之后，这一世代照常
    await run_manager.continue_failed(run_id)          # 接手的那一边接着跑
    for _ in range(300):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_hash:
            break
        await asyncio.sleep(0.05)
    assert row.status == "succeeded", row.error
    events = await _events(run_id)
    assert len([e for e in events if e.type == "run.finished"]) == 1
    assert len([e for e in events if e.type == "node.started" and e.node_id == "next"]) == 1, "后面那一步只跑了一次"
    await _no_repeated_seq(run_id)


async def test_the_final_write_checks_the_owner():
    async with SessionLocal() as session:
        run = Run(status="running", owner="peer-host:1:abcd", graph={}, input={})
        session.add(run)
        await session.commit()
    await run_manager._finalize(run.id, "succeeded", None, {}, 10, owner="mine:1:ffff")
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.status == "running" and row.finished_at is None, "属主已经不是自己：不写终态"


async def test_a_repeated_seq_is_refused_by_the_database():
    async with SessionLocal() as session:
        run = Run(status="running", graph={}, input={})
        session.add(run)
        await session.flush()
        session.add_all([RunEvent(id=new_id(), run_id=run.id, seq=1, type="log", ts=0, data={}),
                         RunEvent(id=new_id(), run_id=run.id, seq=1, type="log", ts=0, data={})])
        with pytest.raises(IntegrityError):
            await session.commit()
