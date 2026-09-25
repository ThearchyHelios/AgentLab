"""服务关停不是用户取消；重启后接着跑，也不能替人把后面的审批答了。

dev.sh 开着 --reload，改任何一个后端文件就是一次关停。以前关停时在跑的运行
一律记成 cancelled、原因写"用户取消"——之后 resume 和 continue 都拒绝它，尽管
checkpoint 完好无损。

更要紧的是接着跑那一步：这类运行没有在等人的 interrupt，resume 却照样发一个
Command(resume=…)。LangGraph 会把这个值交给后面遇到的第一个 interrupt()——一个
还没轮到的人工关卡就这么被自动作答了，审批记录从没建过，运行直接跑完。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent
from app.engine import compiler
from app.engine.runner import run_manager
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


async def _status(run_id: str) -> str:
    async with SessionLocal() as session:
        return (await session.get(Run, run_id)).status


async def _until(check, timeout: float = 15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等超时了")


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


async def _started(run_id: str, node_id: str) -> int:
    return sum(1 for e in await _events(run_id) if e.type == "node.started" and e.node_id == node_id)


@pytest.fixture
def slow_once(monkeypatch):
    """transform 节点第一次跑时卡住（好让关停撞上它），之后立刻返回。"""
    calls = {"n": 0}
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def runner(state, ctx):
        if ctx.node.id == "slow":
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(30)
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, runner)
    return calls


GRAPH = chain(
    node("start", "input", fields=[{"name": "question"}]),
    node("a", "transform", mode="template", template="前面这步", assign_to="x"),
    node("slow", "transform", mode="template", template="慢的这步"),
    node("gate", "human", mode="approve", title="发出去之前看一眼"),
    node("out", "output", fields=[{"name": "结果", "value": "{{ vars.x }}"}]),
)


async def _run_into_shutdown() -> str:
    run = await run_manager.start(graph=GRAPH, input_payload={"question": "随便"})
    await _until(lambda: _started_is(run.id, "slow"))
    await run_manager.shutdown()
    return run.id


async def _started_is(run_id: str, node_id: str) -> bool:
    return await _started(run_id, node_id) > 0


async def test_shutdown_is_not_a_user_cancel(slow_once):
    run_id = await _run_into_shutdown()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    assert run.status == "interrupted", f"关停被记成了 {run.status}：{run.error}"
    assert "用户取消" not in (run.error or "")
    events = await _events(run_id)
    assert not [e for e in events if e.type == "run.cancelled"]
    assert "server_shutdown" in [e.data.get("code") for e in events if e.type == "log"]


async def test_resuming_after_a_restart_does_not_answer_the_next_human_gate(slow_once):
    run_id = await _run_into_shutdown()
    await run_manager.setup()                          # 重启
    await run_manager.resume(run_id, None)

    async def reached_gate() -> bool:
        return await _status(run_id) in ("interrupted", "succeeded", "failed")
    await _until(reached_gate)
    await asyncio.sleep(0.2)
    status = await _status(run_id)
    assert status == "interrupted", f"人工关卡被跳过了，运行直接 {status}"

    async with SessionLocal() as session:
        pending = list((await session.execute(
            select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
        )).scalars())
    assert [a.node_id for a in pending] == ["gate"], "关卡在等人，审批记录却没建"
    assert await _started(run_id, "a") == 1, "接着跑把前面跑完的节点又跑了一遍"


async def test_shutdown_lets_a_finishing_run_finish(monkeypatch):
    """正在收尾的运行不被关停打断：状态、封存都得落完。"""
    original = run_manager._seal

    async def slow_seal(run_id):
        await asyncio.sleep(0.5)
        await original(run_id)

    monkeypatch.setattr(run_manager, "_seal", slow_seal)
    run = await run_manager.start(graph=chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("out", "output", fields=[]),
    ), input_payload={"question": "随便"})

    async def done() -> bool:
        return await _status(run.id) == "succeeded"
    await _until(done)
    await run_manager.shutdown()                       # 撞在封存的半路上

    async with SessionLocal() as session:
        sealed = await session.get(Run, run.id)
    assert sealed.manifest_hash, "关停打断了封存"
