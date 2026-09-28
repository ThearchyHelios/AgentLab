"""运行收尾和几条并发路径上的竞态。

终态事件和运行记录要同时可见。两边都有人在读：
- 收到 run.finished 的客户端马上 GET 这次运行取完整成果（事件里的 output 是截断的）；
  以前先落事件、隔一个事务才提交状态，每次都读到 running 和空的 output，问数据页
  于是把截断过的成果标成残缺存进会话；
- 读到 failed 的一方马上去翻事件，要翻得到 run.failed。更早以前是先提交状态，这一边
  偶尔翻不到。
两边都要成立，只能放进同一个事务。

恢复 / 接着跑和「放弃这次运行」抢同一次运行：读到 interrupted 之后隔了好几次 await
才写 running，放弃正好在这中间提交的话，一次已经取消、封存、发过 run.cancelled 的
运行会被改回 running，又拉起来跑。

刚停到审批上、还在落 interrupted 状态的那一刻点「放弃」：以前直接取消执行任务，把
收尾从中间打断——状态停在 running、审批还挂着、任务没了，再点一次只剩 409。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.bus import bus
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent
from app.engine import compiler, runner
from app.engine.context import NodeError
from app.engine.runner import RunManager, run_manager
from app.engine.schema import NodeType


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
def slow(monkeypatch):
    """transform 节点慢一点，好让订阅先接上；id 为 boom 的直接失败，id 为 stuck 的一直卡着。"""
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def run(state, ctx):
        await asyncio.sleep(30 if ctx.node.id == "stuck" else 0.15)
        if ctx.node.id == "boom":
            raise NodeError(ctx.node.id, "炸了")
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, run)


def _graph(work_id: str = "work", *, gate: bool = False) -> dict:
    nodes = [node("start", "input", fields=[{"name": "q"}]),
             node(work_id, "transform", mode="template", template="成果全文")]
    if gate:
        nodes.append(node("gate", "human", mode="approve", title="放行吗"))
    nodes.append(node("out", "output", fields=[{"name": "r", "value": f"{{{{ nodes.{work_id} }}}}"}]))
    return chain(*nodes)


async def _status(run_id: str) -> str:
    async with SessionLocal() as session:
        return (await session.get(Run, run_id)).status


async def _until(check, timeout: float = 15.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等超时了")


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


async def _row_when(run_id: str, terminal: str, act=None) -> Run:
    """订阅这次运行，一收到 terminal 事件就读运行记录——客户端就是这么做的。"""
    sub = bus.attach(run_id)
    acting = asyncio.ensure_future(act()) if act is not None else None
    try:
        async for event in sub.stream():
            if str(event.type) == terminal:
                async with SessionLocal() as session:
                    return await session.get(Run, run_id)
    finally:
        sub.close()
        if acting is not None:
            await acting
    raise AssertionError(f"没等到 {terminal}")


# --------------------------------------------------------------------------
# 终态事件和运行记录同时可见
# --------------------------------------------------------------------------


async def test_the_run_is_final_when_run_finished_arrives(slow):
    for _ in range(5):
        run = await run_manager.start(graph=_graph(), input_payload={"q": "x"})
        row = await _row_when(run.id, "run.finished")
        assert row.status == "succeeded", f"收到 run.finished 时运行还是 {row.status}"
        assert row.output == {"r": "成果全文"}, row.output
        assert row.finished_at is not None and row.usage.get("wall_ms") is not None
        await run_manager.wait_idle(run.id)


async def test_the_run_is_final_when_run_failed_arrives(slow):
    for _ in range(5):
        run = await run_manager.start(graph=_graph("boom"), input_payload={"q": "x"})
        row = await _row_when(run.id, "run.failed")
        assert row.status == "failed" and "炸了" in (row.error or ""), (row.status, row.error)
        assert row.error_node_id == "boom"
        await run_manager.wait_idle(run.id)


async def test_the_terminal_event_is_there_once_the_status_is(slow):
    """反方向：读到终态状态时，终态事件一定已经在库里。"""
    for work_id, terminal in (("work", "run.finished"), ("boom", "run.failed")):
        for _ in range(3):
            run = await run_manager.start(graph=_graph(work_id), input_payload={"q": "x"})

            async def settled(run_id=run.id) -> bool:
                return await _status(run_id) in ("succeeded", "failed")

            await _until(settled)
            assert (await _events(run.id))[-1].type == terminal
            await run_manager.wait_idle(run.id)


async def test_the_run_is_final_when_a_stop_arrives(slow):
    run = await run_manager.start(graph=_graph("stuck"), input_payload={"q": "x"})
    await _until(lambda: _started(run.id, "stuck"))
    row = await _row_when(run.id, "run.cancelled", act=lambda: run_manager.cancel(run.id))
    assert row.status == "cancelled" and row.finished_at is not None


async def test_the_run_is_final_when_an_abandon_arrives(slow):
    run = await run_manager.start(graph=_graph(gate=True), input_payload={"q": "x"})
    await _until(lambda: _is(run.id, "interrupted"))
    await run_manager.wait_idle(run.id)
    row = await _row_when(run.id, "run.cancelled", act=lambda: run_manager.cancel(run.id))
    assert row.status == "cancelled" and row.finished_at is not None
    assert row.last_seq == (await _events(run.id))[-1].seq


async def _started(run_id: str, node_id: str) -> bool:
    return any(e.type == "node.started" and e.node_id == node_id for e in await _events(run_id))


async def _is(run_id: str, status: str) -> bool:
    return await _status(run_id) == status


# --------------------------------------------------------------------------
# 恢复 / 接着跑 和 放弃 抢同一次运行
# --------------------------------------------------------------------------


async def _suspended_run() -> str:
    """服务重启挂起的运行：interrupted，没有待审批，断点完好。"""
    run = await run_manager.start(graph=_graph("stuck"), input_payload={"q": "x"})
    await _until(lambda: _started(run.id, "stuck"))
    await run_manager.shutdown()
    await run_manager.setup()
    assert await _status(run.id) == "interrupted"
    return run.id


def _abandon_in_between(monkeypatch) -> None:
    """在「读到 interrupted」和「写 running」之间，放弃这次运行的请求先提交了。"""
    async def racing(run_id, spec, snapshot):
        assert await run_manager._abandon(run_id, "另一个人")
        return None

    monkeypatch.setattr(runner, "_stale_replay", racing)


@pytest.mark.parametrize("path", ["resume", "continue"])
async def test_a_run_abandoned_mid_way_is_not_revived(slow, monkeypatch, path):
    run_id = await _suspended_run()
    _abandon_in_between(monkeypatch)
    with pytest.raises(ValueError, match="已取消"):
        if path == "resume":
            await run_manager.resume(run_id, None)
        else:
            await run_manager.continue_failed(run_id)
    assert not run_manager.is_active(run_id), "取消了的运行又被拉起来跑了"
    assert await _status(run_id) == "cancelled"
    events = await _events(run_id)
    assert events[-1].type == "run.cancelled" and not [e for e in events if e.type == "run.resumed"]


async def test_an_approval_closed_by_an_abandon_is_not_answered_again(slow, monkeypatch):
    run = await run_manager.start(graph=_graph(gate=True), input_payload={"q": "x"})
    await _until(lambda: _is(run.id, "interrupted"))
    await run_manager.wait_idle(run.id)
    _abandon_in_between(monkeypatch)
    with pytest.raises(ValueError):
        await run_manager.resume(run.id, {"approved": True})
    async with SessionLocal() as session:
        approval = (await session.execute(
            select(Approval).where(Approval.run_id == run.id))).scalar_one()
    assert approval.status == "cancelled", approval.status
    assert await _status(run.id) == "cancelled" and not run_manager.is_active(run.id)


# --------------------------------------------------------------------------
# 刚停到审批上、还在落状态时点「放弃」
# --------------------------------------------------------------------------


async def test_abandoning_while_the_pause_is_being_recorded(slow, monkeypatch):
    original = RunManager._finalize
    recording = asyncio.Event()

    async def slow_finalize(self, run_id, status, *args, **kwargs):
        if status == "interrupted":
            recording.set()
            await asyncio.sleep(0.4)        # 落 interrupted 状态的这一段被拉长
        return await original(self, run_id, status, *args, **kwargs)

    monkeypatch.setattr(RunManager, "_finalize", slow_finalize)
    run = await run_manager.start(graph=_graph(gate=True), input_payload={"q": "x"})
    await asyncio.wait_for(recording.wait(), 10)
    assert run.id in run_manager._finalizing

    assert await run_manager.cancel(run.id, actor="审核员") == "cancelled"
    assert await _status(run.id) == "cancelled"
    async with SessionLocal() as session:
        approval = (await session.execute(
            select(Approval).where(Approval.run_id == run.id))).scalar_one()
    assert approval.status == "cancelled" and approval.resolved_by == "审核员"
    assert (await _events(run.id))[-1].type == "run.cancelled"


async def test_an_approval_closed_just_before_it_is_marked_answered(slow, monkeypatch):
    """放弃恰好提交在「读到这条审批还是 pending」和「把它记成已回复」之间。"""
    import sqlite3

    from sqlalchemy import event

    from app.db.base import engine

    run = await run_manager.start(graph=_graph(gate=True), input_payload={"q": "x"})
    await _until(lambda: _is(run.id, "interrupted"))
    await run_manager.wait_idle(run.id)
    armed = {"on": True}

    def abandon_first(conn, cursor, statement, params, context, executemany):
        if armed["on"] and statement.lstrip().upper().startswith("UPDATE APPROVALS"):
            armed["on"] = False
            other = sqlite3.connect(engine.url.database, timeout=5)
            other.execute("UPDATE approvals SET status='cancelled' WHERE run_id=?", (run.id,))
            other.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run.id,))
            other.commit()
            other.close()

    event.listen(engine.sync_engine, "before_cursor_execute", abandon_first)
    try:
        with pytest.raises(ValueError, match="已经处理过了|放弃"):
            await run_manager.resume(run.id, {"approved": True})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", abandon_first)
    assert not armed["on"]
    async with SessionLocal() as session:
        approval = (await session.execute(
            select(Approval).where(Approval.run_id == run.id))).scalar_one()
    assert approval.status == "cancelled", "关掉的审批被改回了已回复"
    assert not run_manager.is_active(run.id)
