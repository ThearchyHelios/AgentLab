"""自我隔离（cluster/fence.py，复核 R-b1）：本进程卡住了、续不上租约，别的进程已经把它在跑的运行接手了——它缓过来之后不再写：
不写事件（不和接手的一边撞号）、不写断点、不写终态（不把接手的结果盖掉）。在一个进程里装出来：把「上次续上租约」拨到一个租约
时长以前，再把运行改成接手之后的样子。两种库都跑。"""
from __future__ import annotations

import asyncio
import contextlib
import os

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
    fence.renewed(ago=lease_s() + 1)                   # 卡了一个多租约时长
    await _taken_over(run.id)
    slow["gate"].set()                                 # 缓过来，手上的执行接着往下走
    for _ in range(200):
        if not run_manager.is_active(run.id):
            break
        await asyncio.sleep(0.05)
    assert not run_manager.is_active(run.id), "执行停了"
    assert fence.generation() == before + 1, "发现续晚了：隔离了一次"
    return run.id


async def test_a_stalled_executor_does_not_overwrite_the_takeover(slow, caplog):
    import gc
    import logging

    caplog.set_level(logging.ERROR, logger="asyncio")
    run_id = await _stall_and_take_over(slow)
    await asyncio.sleep(0.1)
    gc.collect()
    await asyncio.sleep(0)
    noisy = [r.getMessage() for r in caplog.records if r.name == "asyncio"]
    assert noisy == [], f"隔离时 asyncio 不该记回调异常、没人取的任务异常（复核 R-b8）：{noisy}"
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


@pytest.mark.parametrize("jump", [3600.0, -3600.0])
async def test_a_wall_clock_jump_does_not_fence_a_running_run(slow, monkeypatch, jump):
    """复核 R-b14：执行到一半本机墙钟往前 / 往后跳一小时（NTP 一步校时、手动改时间）——计入休眠的钟没问题，不隔离，运行照常跑完。"""
    import time as _time

    monkeypatch.setattr(fence, "SLEEP_AWARE", True)
    run = await run_manager.start(graph=GRAPH, input_payload={"question": "q"})
    await asyncio.wait_for(slow["entered"].wait(), timeout=15)
    before = fence.generation()
    monkeypatch.setattr(fence, "_wall", lambda: _time.time() + jump)
    slow["gate"].set()
    for _ in range(300):
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed", "interrupted") and not run_manager.is_active(run.id):
            break
        await asyncio.sleep(0.05)
    assert row.status == "succeeded", row.error
    assert fence.generation() == before, "没有隔离"


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


# ---------------- 复核 R-b9：谁会被打停、谁不会 ----------------


async def test_only_executions_bound_to_a_generation_are_stopped_by_check():
    """续晚之后第一个问 check() 的不一定是运行本身：没绑定世代的调用者（HTTP 处理、调度那一拍）只隔离、不抛；绑定了的抛。
    隔离之后新身份还没登记上，新开始的执行在 ready() 上等，等不到抛 Fenced；登记上了就放行。"""
    gate = asyncio.Event()
    seen: list[str] = []

    async def bound_writer() -> None:
        fence.bind()
        await gate.wait()
        try:
            fence.check()
            seen.append("wrote")
        except fence.Fenced:
            seen.append("fenced")

    task = asyncio.create_task(bound_writer())
    await asyncio.sleep(0)
    before = fence.generation()
    fence.renewed(ago=lease_s() + 1)
    fence.check()                                      # 没绑定世代：只隔离
    assert fence.generation() == before + 1 and fence.pending()
    gate.set()
    await task
    assert seen == ["fenced"], "绑定在隔离之前的那一段执行：不许再写"

    with pytest.raises(fence.Fenced):
        await fence.ready(timeout=0.1)                 # 新身份还没登记上：新开始的执行等不到就不开始
    waiting = asyncio.create_task(fence.ready(timeout=5))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    fence.renewed()                                    # 登记上了
    await asyncio.wait_for(waiting, timeout=2)
    fence.bind()
    fence.check()                                      # 新世代照常


async def test_the_scheduler_loop_survives_a_fenced_tick(monkeypatch):
    from app.scheduler import loop as sloop

    calls = {"n": 0}

    async def fire_due(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fence.Fenced()
        return []

    monkeypatch.setattr(sloop, "fire_due", fire_due)
    await sloop.runner.start(interval=0.02)
    try:
        for _ in range(200):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.02)
        assert calls["n"] >= 3 and sloop.runner.running(), "被 Fenced 打到的那一拍之后照常往下"
    finally:
        await sloop.runner.stop()


async def test_the_lease_task_keeps_going_when_registering_again_fails(monkeypatch):
    """隔离之后重新登记：库一抖、版本守卫拦下都不让续租的任务退出，退避着重试；登记上了照常续租。"""
    from app.cluster import identity, service as service_mod, workers
    from app.cluster.version import VersionMismatch

    monkeypatch.setattr(identity, "OWNER", f"{identity.host()}:{os.getpid()}:retired1")   # 测试完换回原来的身份
    monkeypatch.setattr(workers, "heartbeat_s", lambda: 0.01)
    monkeypatch.setattr(workers, "lease_s", lambda: 5.0)
    svc = service_mod._Service()
    tries = {"register": 0, "renew": 0}

    async def register(*, exclusive: bool = False) -> None:  # noqa: ARG001
        tries["register"] += 1
        if tries["register"] == 1:
            raise VersionMismatch("库里还有版本不同的进程活着")
        if tries["register"] == 2:
            raise OSError("库一时连不上")

    async def renew() -> bool:
        tries["renew"] += 1
        return True

    monkeypatch.setattr(workers, "register", register)
    monkeypatch.setattr(svc, "renew", renew)
    old = identity.owner()
    fence.trip("测试")
    svc._on_fenced()
    assert identity.owner() != old and svc.pending_since is not None
    task = asyncio.create_task(svc._lease_loop())
    try:
        for _ in range(300):
            if tries["renew"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert not task.done(), "续租的任务还在"
        assert tries["register"] == 3 and svc.pending_since is None and not fence.pending()
        assert tries["renew"] >= 2, "登记上之后照常续租"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
