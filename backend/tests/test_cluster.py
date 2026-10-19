"""多进程协调（app/cluster，docs/design/orchestration-round §5 W2-b）在一个进程里能验的那几样：别的进程用库里的行装出来
（一个登记着、租约没过期的「同伴」，一个从没登记过的「已经没了的进程」）。两种库都跑；真起两个进程的端到端见
test_multiprocess_e2e.py。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.cluster import identity, leader as leader_mod, slots, state, workers
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import Run, RunEvent
from app.db.models_cluster import ClusterLease, ClusterRequest, ClusterWorker, LaneTicket, RunSlot

PEER = "peer-host:4242:feedbeef"
GHOST = "gone-host:999:deadbeef"


@pytest.fixture(autouse=True)
async def clean():
    async def wipe() -> None:
        async with SessionLocal() as session:
            for model in (RunSlot, LaneTicket, ClusterRequest, ClusterWorker):
                await session.execute(delete(model))
            await session.execute(delete(ClusterLease).where(ClusterLease.name.like("test-%")))
            await session.commit()
        state.forget()
        slots.forget_policy()

    await wipe()
    yield
    await wipe()


async def _peer(owner: str = PEER, *, alive: bool = True) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(ClusterWorker(id=owner, host="peer-host", pid=4242, started_at=now, heartbeat_at=now,
                                  lease_until=now + timedelta(seconds=60 if alive else -60)))
        await session.commit()
    state.forget()


def _policy(monkeypatch, total: int, **classes: tuple[int, int]) -> None:
    value = slots.values({"max_total": total, "classes": {k: {"limit": lim, "priority": pri}
                                                          for k, (lim, pri) in classes.items()}})

    async def fixed(*, fresh: bool = False):  # noqa: ARG001
        return value

    monkeypatch.setattr(slots, "policy", fixed)


async def test_liveness_counts_only_unexpired_peers_and_never_lists_itself():
    await workers.register()
    await _peer(PEER, alive=True)
    await _peer(GHOST, alive=False)
    live = await workers.liveness()
    assert live.others == frozenset({PEER})
    assert live.alive(PEER) and not live.alive(GHOST) and not live.alive(None)
    assert not live.alive(identity.owner()), "属主是自己的只看本进程里是不是真的在跑"
    assert live.alive(identity.owner(), local=True) and live.trusting().alive(identity.owner())


async def test_slots_go_to_the_higher_priority_class_first(monkeypatch):
    """全站 1 个名额：系统类的先占着、又一个系统类排着，后来的交互类先拿到空出来的名额；已经在跑的不打断。"""
    _policy(monkeypatch, 1, interactive=(1, 40), system=(1, 10))
    order: list[str] = []
    release = {name: asyncio.Event() for name in ("e1", "e2", "chat")}

    async def run(name: str, origin: str) -> None:
        async with slots.hold(f"run-{name}", origin):
            order.append(name)
            await release[name].wait()

    first = asyncio.create_task(run("e1", "eval"))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(run("e2", "eval"))
    await asyncio.sleep(0.2)
    chat = asyncio.create_task(run("chat", "chat"))
    await asyncio.sleep(0.2)
    assert order == ["e1"], "名额满了，后来的两个都在等"
    release["e1"].set()
    await asyncio.wait_for(asyncio.shield(_until(lambda: len(order) == 2)), timeout=10)
    assert order == ["e1", "chat"], "交互类的优先级高，先拿到名额"
    release["chat"].set()
    release["e2"].set()
    await asyncio.wait_for(asyncio.gather(first, second, chat), timeout=10)
    assert order == ["e1", "chat", "e2"]


async def test_a_class_at_its_cap_does_not_hold_up_the_others(monkeypatch):
    """系统类的上限是 1：第二个系统类的等着，交互类的照样拿到名额（全站还有空）。"""
    _policy(monkeypatch, 5, interactive=(5, 40), system=(1, 10))
    got: list[str] = []
    gate = asyncio.Event()

    async def run(name: str, origin: str) -> None:
        async with slots.hold(f"run-{name}", origin):
            got.append(name)
            await gate.wait()

    tasks = [asyncio.create_task(run("e1", "eval")), asyncio.create_task(run("e2", "recall"))]
    await asyncio.sleep(0.3)
    tasks.append(asyncio.create_task(run("chat", "canvas")))
    await _until(lambda: "chat" in got)
    assert len({"e1", "e2"} & set(got)) == 1, "系统类到了上限：另一个等着"
    snapshot = await slots.snapshot()
    assert snapshot["classes"]["system"] == {"running": 1, "waiting": 1}
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


async def test_slots_held_by_a_process_that_is_gone_are_taken_back(monkeypatch):
    _policy(monkeypatch, 1, interactive=(1, 40))
    async with SessionLocal() as session:
        session.add(RunSlot(run_id="ghost-run", klass="interactive", priority=40, owner=GHOST, granted=True))
        await session.commit()
    async with slots.hold("mine", "chat"):
        async with SessionLocal() as session:
            rows = (await session.execute(select(RunSlot.run_id))).scalars().all()
        assert rows == ["mine"], "没了的进程占着的名额收回了"


async def test_slots_of_a_live_peer_are_left_alone(monkeypatch):
    _policy(monkeypatch, 1, interactive=(1, 40))
    await _peer(PEER)
    async with SessionLocal() as session:
        session.add(RunSlot(run_id="peer-run", klass="interactive", priority=40, owner=PEER, granted=True))
        await session.commit()
    waiting = asyncio.create_task(_hold_once("mine"))
    await asyncio.sleep(0.5)
    assert not waiting.done(), "活着的同伴占着唯一的名额：等着"
    async with SessionLocal() as session:
        await session.execute(delete(RunSlot).where(RunSlot.run_id == "peer-run"))
        await session.commit()
    slots.wake_local()
    await asyncio.wait_for(waiting, timeout=10)


async def _hold_once(run_id: str) -> None:
    async with slots.hold(run_id, "chat"):
        pass


async def test_the_source_lane_is_shared_and_skips_tickets_of_a_gone_process():
    from app.core import job_runtime as runtime

    import app.data.catalog_jobs  # noqa: F401 - 登记 draft、profile

    async with SessionLocal() as session:
        session.add(LaneTicket(source_id="src-x", kind="profile", job_id="ghost-job", owner=GHOST, granted=True))
        session.add(LaneTicket(source_id="src-y", kind="profile", job_id="peer-job", owner=PEER, granted=True))
        await session.commit()
    await _peer(PEER)
    mine = await runtime.lanes.enter("src-x", "draft", "job-1")
    assert mine.granted.is_set(), "没了的进程占着的位置让出来了"
    other = await runtime.lanes.enter("src-y", "draft", "job-2")
    assert not other.granted.is_set(), "活着的同伴在这个源上做着事：排在后面"
    assert await runtime.lanes.ahead("src-y", "job-2") == ["profile"]
    with pytest.raises(runtime.LaneBusy) as busy:
        await runtime.lanes.try_enter("src-y", "import")
    assert busy.value.holder.kind == "profile"
    await mine.release()
    await other.release()


async def test_reaping_runs_only_touches_the_ones_whose_owner_is_gone():
    from app.engine.runner import run_manager

    await _peer(PEER)
    async with SessionLocal() as session:
        live_peer = Run(status="running", owner=PEER, graph={}, input={})
        dead = Run(status="running", owner=GHOST, graph={}, input={})
        queued = Run(status="queued", owner=GHOST, graph={}, input={})
        legacy = Run(status="running", owner=None, graph={}, input={})
        session.add_all([live_peer, dead, queued, legacy])
        await session.commit()
    done = await run_manager.reap(await state.current(fresh=True))
    assert done >= 3
    async with SessionLocal() as session:
        rows = {r.id: r for r in (await session.execute(select(Run).where(
            Run.id.in_([live_peer.id, dead.id, queued.id, legacy.id])))).scalars()}
        events = list((await session.execute(select(RunEvent.run_id, RunEvent.type, RunEvent.data).where(
            RunEvent.run_id.in_([dead.id, queued.id])))).all())
    assert rows[live_peer.id].status == "running" and rows[live_peer.id].owner == PEER, "活着的同伴在跑的不碰"
    assert rows[dead.id].status == "interrupted" and rows[dead.id].owner is None
    assert rows[queued.id].status == "failed"
    assert rows[legacy.id].status == "interrupted", "升级前没记属主的照「属主不在了」处理"
    assert any(e.type == "log" and (e.data or {}).get("code") == "server_shutdown" for e in events)
    assert any(e.type == "run.failed" for e in events)


async def test_stopping_or_noting_a_run_on_a_live_peer_goes_to_that_peer():
    from app.core.events import EventType
    from app.engine.runner import run_manager

    await _peer(PEER)
    async with SessionLocal() as session:
        run = Run(status="running", owner=PEER, graph={}, input={})
        session.add(run)
        await session.commit()
    assert await run_manager.cancel(run.id, actor="张工") == "stopping"
    await run_manager.note(run.id, EventType.LOG, level="info", message="别的进程记的一条")
    async with SessionLocal() as session:
        sent = list((await session.execute(select(ClusterRequest).where(ClusterRequest.target_id == run.id)
                                           .order_by(ClusterRequest.created_at))).scalars())
        row = await session.get(Run, run.id)
    assert [(r.owner, r.kind, r.target) for r in sent] == [(PEER, "stop", "run"), (PEER, "note", "run")]
    assert sent[0].actor == "张工" and sent[1].payload["data"]["message"] == "别的进程记的一条"
    assert row.status == "running", "停止交给属主去做，这里不改状态"


async def test_stopping_a_run_whose_owner_is_gone_takes_it_over_and_abandons_it():
    from app.engine.runner import run_manager

    async with SessionLocal() as session:
        run = Run(status="running", owner=GHOST, graph={}, input={})
        session.add(run)
        await session.commit()
    assert await run_manager.cancel(run.id, actor="张工") == "cancelled"
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.status == "cancelled" and row.end_reason == "stopped"


async def test_leadership_is_handed_over_when_the_lease_runs_out():
    me = leader_mod.Leadership("test-lead")
    assert await me.claim() and me.held()
    first = me.epoch
    async with SessionLocal() as session:
        row = await session.get(ClusterLease, "test-lead")
        row.owner, row.lease_until = PEER, utcnow() + timedelta(seconds=60)
        await session.commit()
    assert not await me.claim() and not me.held(), "别的进程拿着没到期的租约：不当班"
    async with SessionLocal() as session:
        row = await session.get(ClusterLease, "test-lead")
        row.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert await me.claim() and me.epoch == first + 1, "到期了接班，换一次当班加一"
    await me.release()
    assert (await leader_mod.current("test-lead"))["owner"] is None


async def test_the_daily_cap_is_not_overrun_by_concurrent_charges():
    from app.engine.spend import Ledger

    book = Ledger(f"test_spend_{new_id()[:8]}")
    other = Ledger(book.key)   # 同一本账的另一个实例（另一个进程里就是这样）
    got = await asyncio.gather(*(b.charge(0.1, limit=1.0, priced=True) for b in [book, other] * 10),
                               *(book.charge(0.1, limit=1.0, priced=True) for _ in range(5)))
    assert sum(1 for d in got if d) == 10
    assert (await book.daily())["usd"] <= 1.0 + 1e-9


@pytest.mark.sqlite_only
def test_sqlite_refuses_a_second_process_on_the_same_data_dir():
    from app.cluster import guard

    guard.acquire()
    try:
        backend = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(backend)}
        out = subprocess.run([sys.executable, "-c", "from app.cluster import guard; guard.acquire()"], cwd=str(backend),
                             env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode != 0 and "只支持单进程" in out.stderr, out.stderr[-2000:]
        many = subprocess.run([sys.executable, "-c", "from app.cluster import guard; guard.acquire()"], cwd=str(backend),
                              env={**env, "AGENTLAB_WORKERS": "2", "AGENTLAB_DATA_DIR": str(Path(env["AGENTLAB_DATA_DIR"]) / "other")},
                              capture_output=True, text=True, timeout=60)
        assert many.returncode != 0 and "AGENTLAB_WORKERS=2" in many.stderr, many.stderr[-2000:]
    finally:
        guard.release()


async def _until(check, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            raise AssertionError("等超时了")
        await asyncio.sleep(0.05)
