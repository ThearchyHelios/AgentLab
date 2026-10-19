"""待办判定后恢复运行出错（D-o）。

以前只接 ValueError / KeyError：别的异常（库被锁、检查点读不出……）冒上去，待办停在 resolving，没人能再回复，只有
重启时才收拾。现在任何异常都把待办退回 pending、这一票撤回、记日志，原因交回调用方（task_resume_failed）；修好之后
同一个人可以再回复一次。超时驳回同理：退回 pending，下一拍再试。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.api.deps import Principal
from app.db.base import SessionLocal
from app.db.models import Run, Task, TaskAssignment
from app.engine.runner import run_manager
from app.process import tasks
from app.process.tasks import TaskRefused


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


FLOW = {"nodes": [node("start", "input", fields=[{"name": "q"}]),
                  node("review", "human", mode="approve", title="审一下"),
                  node("ok", "output", fields=[{"name": "结论", "value": "通过"}])],
        "edges": [{"source": "start", "target": "review"},
                  {"source": "review", "target": "ok", "sourceHandle": "approved"}]}

ANN = Principal(username="ann", role="admin", mode="off")


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(run_id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _pending(run_id: str) -> Task:
    for _ in range(100):
        async with SessionLocal() as session:
            row = (await session.execute(select(Task).where(Task.run_id == run_id, Task.status == "pending"))).scalars().first()
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("没有开出待办")


async def test_any_resume_error_puts_the_task_back(monkeypatch):
    run = await run_manager.start(graph=FLOW, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _pending(run.id)
    real = run_manager.resume

    async def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_manager, "resume", broken)
    with pytest.raises(TaskRefused) as caught:
        await tasks.respond(task.id, ANN, decision="approve")
    assert caught.value.code == "task_resume_failed" and caught.value.status_code == 500
    assert "恢复运行时出错" in str(caught.value) and "退回待处理" in str(caught.value)
    async with SessionLocal() as session:
        row = await session.get(Task, task.id)
        mine = (await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id))).scalars().all()
    assert row.status == "pending" and row.outcome is None and row.resolved_by is None
    assert [(a.status, a.responded_by) for a in mine] == [("pending", None)], "这一票撤回，可以再回复"

    monkeypatch.setattr(run_manager, "resume", real)
    out = await tasks.respond(task.id, ANN, decision="approve")
    assert out["resolved"] is True
    assert (await _wait(run.id, ("succeeded", "failed"))).status == "succeeded"


async def test_the_periodic_takeover_leaves_a_task_that_is_being_resolved_alone(monkeypatch):
    """复核 R-b2：当班进程隔一会儿的那一次接手（live.trust_mine）撞上一次正在判定、恢复的待办（停在 resolving 的那几秒），
    单进程时也不能把它退回 pending、撤回那一票——只收拾停了两个租约时长以上的。启动时、又没有别的进程的那一次照旧全收拾。"""
    from app.cluster.workers import Liveness

    run = await run_manager.start(graph=FLOW, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    task = await _pending(run.id)
    real = run_manager.resume
    reaped = asyncio.Event()

    async def slow(*a, **k):
        await asyncio.sleep(0.3)
        await tasks.startup(live=Liveness().trusting())   # 判定到一半，当班进程来了一次
        reaped.set()
        return await real(*a, **k)

    monkeypatch.setattr(run_manager, "resume", slow)
    out = await tasks.respond(task.id, ANN, decision="approve")
    assert reaped.is_set() and out["resolved"] is True
    assert (await _wait(run.id, ("succeeded", "failed"))).status == "succeeded"
    async with SessionLocal() as session:
        row = await session.get(Task, task.id)
        mine = (await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id))).scalars().all()
    assert row.status == "resolved", row.status
    assert mine and all(a.responded_by == "ann" and a.status not in ("pending", "withdrawn") for a in mine), \
        [(a.status, a.responded_by) for a in mine]
