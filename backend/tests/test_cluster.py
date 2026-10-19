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
from app.core.config import settings as app_settings
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


def _policy(monkeypatch, total: int, reserve: int | None = None, **classes: tuple[int, int]) -> None:
    stored: dict = {"max_total": total, "classes": {k: {"limit": lim, "priority": pri} for k, (lim, pri) in classes.items()}}
    if reserve is not None:
        stored["interactive_reserve"] = reserve
    value = slots.values(stored)

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
    assert snapshot["classes"]["system"] == {"running": 1, "waiting": 1}, snapshot
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


def test_the_defaults_keep_a_reserve_for_interactive_and_lend_the_rest():
    pol = slots.defaults(20)
    assert pol["interactive_reserve"] == 5 and pol["classes"]["interactive"]["limit"] == 20
    assert {pol["classes"][k]["limit"] for k in ("app", "schedule", "system")} == {15}, "别的类缺省借满到 全站 − 留底"
    assert slots.defaults(2)["interactive_reserve"] == 1 and slots.defaults(1)["interactive_reserve"] == 0, \
        "至少留 1 个；全站只有 1 个名额时不留（别的类才跑得起来）"
    assert slots.values({"max_total": 8})["classes"]["system"]["limit"] == 6, "缺省按存着的全站上限现算"
    assert slots.problem({"max_total": 4, "interactive_reserve": 4}) is not None


async def _holders(names_origins: list[tuple[str, str]], got: list[str], gate: asyncio.Event) -> list[asyncio.Task]:
    async def run(name: str, origin: str) -> None:
        async with slots.hold(f"run-{name}", origin):
            got.append(name)
            await gate.wait()

    tasks = []
    for name, origin in names_origins:
        tasks.append(asyncio.create_task(run(name, origin)))
        await asyncio.sleep(0.05)
    return tasks


async def test_idle_system_runs_borrow_all_but_the_interactive_reserve(monkeypatch):
    """全站 4 个、留底 1 个：没人问数据时评测能同时跑 3 个（全站 − 留底），第 4 个等着。"""
    _policy(monkeypatch, 4, reserve=1)
    got: list[str] = []
    gate = asyncio.Event()
    tasks = await _holders([(f"e{i}", "eval") for i in range(4)], got, gate)
    await _until(lambda: len(got) == 3)
    await asyncio.sleep(0.3)
    assert len(got) == 3, "留底那一个不借给评测"
    assert (await slots.snapshot())["classes"]["system"] == {"running": 3, "waiting": 1}
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


async def test_interactive_takes_the_reserve_at_once_and_it_is_never_lent(monkeypatch):
    """评测借满了（3 个在跑），应用类的再来也借不到留底（全站还空着 1 个）；问数据一来马上拿到留底，不用等评测跑完。"""
    _policy(monkeypatch, 4, reserve=1)
    got: list[str] = []
    gate = asyncio.Event()
    tasks = await _holders([("e1", "eval"), ("e2", "eval"), ("e3", "eval")], got, gate)
    await _until(lambda: len(got) == 3)
    tasks += await _holders([("app", "app_view")], got, gate)
    await asyncio.sleep(0.3)
    assert "app" not in got, "留底不借给应用类"
    tasks += await _holders([("chat", "chat")], got, gate)
    await _until(lambda: "chat" in got)
    assert "app" not in got and set(got) == {"e1", "e2", "e3", "chat"}
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="SQLite 只有一个进程，名额在进程里（复核 R-b3），没有别的进程的名额行")
async def test_slots_held_by_a_process_that_is_gone_are_taken_back(monkeypatch):
    _policy(monkeypatch, 1, interactive=(1, 40))
    async with SessionLocal() as session:
        session.add(RunSlot(run_id="ghost-run", klass="interactive", priority=40, owner=GHOST, granted=True))
        await session.commit()
    async with slots.hold("mine", "chat"):
        async with SessionLocal() as session:
            rows = (await session.execute(select(RunSlot.run_id))).scalars().all()
        assert rows == ["mine"], "没了的进程占着的名额收回了"


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="SQLite 只有一个进程，名额在进程里（复核 R-b3），没有别的进程的名额行")
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


async def test_the_source_lane_does_not_look_up_liveness_while_holding_its_lock(monkeypatch):
    """复核：持着这个源的咨询锁时不另取连接查「谁还活着」（连接池用尽时会卡到取连接超时）——拿锁之前备好；拿锁之后才出现的
    别的属主这一次当活着，不删它的行。"""
    from app.core import job_runtime as runtime

    import app.data.catalog_jobs  # noqa: F401 - 登记 draft、profile

    lanes = runtime.lanes
    holding = {"now": False, "looked_inside": 0}
    real_locked, real_rows, real_live_for = lanes._locked, lanes._rows, state.live_for

    async def locked(session, source_id):
        await real_locked(session, source_id)
        holding["now"] = True

    async def rows(session, source_id, dead_owners):
        try:
            return await real_rows(session, source_id, dead_owners)
        finally:
            holding["now"] = False

    async def live_for(owners):
        if holding["now"]:
            holding["looked_inside"] += 1
        return await real_live_for(owners)

    monkeypatch.setattr(lanes, "_locked", locked)
    monkeypatch.setattr(lanes, "_rows", rows)
    monkeypatch.setattr(state, "live_for", live_for)
    async with SessionLocal() as session:
        session.add(LaneTicket(source_id="src-lock", kind="profile", job_id="ghost-job", owner=GHOST, granted=True))
        await session.commit()
    mine = await lanes.enter("src-lock", "draft", "job-lock")
    assert mine.granted.is_set(), "没了的进程占着的位置照旧让出来（拿锁之前就查清楚了）"
    assert holding["looked_inside"] == 0
    await mine.release()

    async with SessionLocal() as session:
        session.add(LaneTicket(source_id="src-late", kind="profile", job_id="late-job", owner=GHOST, granted=True))
        await session.commit()
        kept = await real_rows(session, "src-late", frozenset())
        await session.commit()
    assert [r.owner for r in kept] == [GHOST], "拿锁之前那份里没有的属主这一次当活着"
    async with SessionLocal() as session:
        await session.execute(delete(LaneTicket).where(LaneTicket.source_id.in_(["src-lock", "src-late"])))
        await session.commit()


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


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="SQLite 只有一个进程，名额在进程里（复核 R-b3）")
async def test_a_slot_is_released_under_the_identity_it_was_taken_with(monkeypatch):
    """复核 R-b10：拿着名额的时候隔离换了身份，放名额照样删得到那一行（按要名额时的身份删），不用等旧身份过期才收回。"""
    _policy(monkeypatch, 2, interactive=(2, 40))
    monkeypatch.setattr(identity, "OWNER", identity.OWNER)
    async with slots.hold("rb10-run", "chat"):
        identity.regenerate()
    async with SessionLocal() as session:
        left = list((await session.execute(select(RunSlot).where(RunSlot.run_id == "rb10-run"))).scalars())
    assert left == []


async def test_registering_again_tidies_up_what_the_retired_identity_left(monkeypatch):
    """复核 R-b10：隔离之后新身份登记上，旧身份的登记、名额、同源队的位置删掉，当班租约交出，发给它的请求转到新身份名下照办——
    停止请求对停在断点上的那次运行照停止的规矩收成 cancelled。"""
    from app.cluster import requests, version
    from app.cluster import service as service_mod
    from app.cluster.fence import now as fence_now

    monkeypatch.setattr(identity, "OWNER", f"{identity.host()}:{os.getpid()}:retired0")   # 测试完换回原来的身份
    old = identity.owner()
    now = utcnow()
    async with SessionLocal() as session:
        session.add(ClusterWorker(id=old, host="this-host", pid=1, started_at=now, heartbeat_at=now,
                                  lease_until=now + timedelta(seconds=60), code_version=version.code_version(),
                                  schema_version=version.schema_version()))
        session.add(RunSlot(run_id="retired-slot", klass="interactive", priority=40, owner=old, granted=True,
                            requested_at=now))
        session.add(LaneTicket(source_id="src-retired", kind="profile", job_id="retired-job", owner=old, granted=True))
        session.add(ClusterLease(name="test-retired", owner=old, lease_until=now + timedelta(seconds=60), epoch=1))
        run = Run(status="interrupted", owner=old, graph={}, input={})
        session.add(run)
        await session.flush()
        session.add(ClusterRequest(owner=old, kind="stop", target="run", target_id=run.id, actor="张工", payload={}))
        await session.commit()
    svc = service_mod._Service()
    svc.retired = [old]
    identity.regenerate()
    svc.pending_since = fence_now()
    await svc.register_again()
    new = identity.owner()
    async with SessionLocal() as session:
        ids = set((await session.execute(select(ClusterWorker.id))).scalars())
        slot_rows = list((await session.execute(select(RunSlot).where(RunSlot.owner == old))).scalars())
        lane_rows = list((await session.execute(select(LaneTicket).where(LaneTicket.owner == old))).scalars())
        lease = await session.get(ClusterLease, "test-retired")
        sent = list((await session.execute(select(ClusterRequest.owner).where(ClusterRequest.target_id == run.id))).scalars())
    assert old not in ids and new in ids, "旧身份的登记删掉：别的进程马上按属主不在了接手"
    assert slot_rows == [] and lane_rows == [] and lease.owner is None
    assert sent == [new], "发给旧身份的请求转到新身份名下"
    await requests.drain()
    await requests.settle()
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.status == "cancelled" and row.end_reason == "stopped", "停止请求照停止的规矩落下了"


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


def test_the_fence_counts_what_the_monotonic_clock_missed(monkeypatch):
    """复核 R-b7：单调钟不走（休眠不计、虚拟机挂起）而墙钟过了两个租约——照样算续晚了；两只钟差出一个租约算跳了。"""
    from app.cluster import fence

    clocks = {"mono": 1000.0, "wall": 5000.0}
    monkeypatch.setattr(fence, "_clock", lambda: clocks["mono"])
    monkeypatch.setattr(fence, "_wall", lambda: clocks["wall"])
    fence.activate()
    try:
        assert not fence.stale() and not fence.jumped()
        clocks["wall"] += 2 * workers.lease_s()          # 合盖两个租约：单调钟没计，墙钟走了
        assert fence.stale() and fence.jumped()
        fence.renewed()
        clocks["mono"] += 2 * workers.lease_s()          # 反过来：墙钟被往回拨了，单调钟照走
        assert fence.stale() and fence.jumped()
        fence.renewed()
        clocks["mono"] += workers.lease_s() / 2
        clocks["wall"] += workers.lease_s() / 2
        assert not fence.stale() and not fence.jumped()
    finally:
        fence.deactivate()


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="SQLite 只有一个进程，租约用本机的钟")
async def test_leases_use_the_database_clock_not_the_local_wall_clock(monkeypatch):
    """复核 R-b7：本机墙钟快了一个小时——别的进程照样算活着、它拿着的当班租约照样抢不走；按库的时钟自己的租约已经过期时，续租
    不再续这个身份，而是隔离。"""
    from app.cluster import fence
    from app.cluster.service import service
    from app.db.base import utcnow as real_utcnow
    import app.db.base as db_base

    await _peer(PEER)
    lead = leader_mod.Leadership("test-skew")
    async with SessionLocal() as session:
        session.add(ClusterLease(name="test-skew", owner=PEER, lease_until=real_utcnow() + timedelta(seconds=60), epoch=1))
        await session.commit()
    skewed = lambda: real_utcnow() + timedelta(hours=1)  # noqa: E731
    monkeypatch.setattr(workers, "utcnow", skewed)
    monkeypatch.setattr(db_base, "utcnow", skewed)
    assert PEER in (await workers.liveness()).others, "按库的时钟它的租约没过期"
    assert not await lead.claim(), "当班租约按库的时钟判：抢不走"
    async with SessionLocal() as session:
        await session.execute(delete(ClusterWorker).where(ClusterWorker.id == PEER))   # 它没记版本：登记时会被版本守卫拦下
        await session.execute(delete(ClusterLease).where(ClusterLease.name == "test-skew"))
        await session.commit()

    await workers.register()
    fence.activate()
    before = fence.generation()
    try:
        assert await service.renew(), "按库的时钟没过期：续上"
        async with SessionLocal() as session:
            row = await session.get(ClusterWorker, identity.owner())
            row.lease_until = real_utcnow() - timedelta(seconds=1)   # 库的时钟已经过了它的租约，本机的两只钟都没觉得
            await session.commit()
        assert not await service.renew() and fence.generation() == before + 1, "过期了不续这个身份：隔离"
    finally:
        fence.deactivate()
        if service._lease_conn is not None:
            await service._lease_conn.close()
            service._lease_conn = None


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


async def test_a_daily_quota_is_taken_up_to_the_limit_by_concurrent_callers():
    """先占额度再发起（cluster/quota）：10 个协程同时占、上限 5，正好 5 个占到；这一天第一次占按已有的记录起算；退回一份又能占。
    （同时占的个数不超过连接池能直接给的连接数：SQLite 的池子是各测试共用的，排队等连接会把它绑到这个测试的事件循环上。）"""
    from app.cluster import quota

    name = f"test-quota-{new_id()[:8]}"

    async def seed(_session) -> int:
        return 2   # 今天已经做过 2 次

    got = await asyncio.gather(*(quota.take(name, 5, seed=seed) for _ in range(10)))
    assert sum(1 for g in got if g) == 3 and await quota.used(name) == 5
    assert not await quota.take(name, 5)
    await quota.give_back(name)
    assert await quota.take(name, 5) and not await quota.take(name, 5)


async def test_a_quota_is_given_back_to_the_day_it_was_taken(monkeypatch):
    """复核 R-b11：23:59:59 占、00:00:01 发起失败——退回前一天那一份，第二天的不动。"""
    from app.cluster import quota

    name = f"test-quota-{new_id()[:8]}"
    days = iter(["2026-10-18", "2026-10-19", "2026-10-19", "2026-10-19"])
    monkeypatch.setattr(quota, "today", lambda: next(days))
    day = await quota.take(name, 5)
    assert day == "2026-10-18"
    assert await quota.take(name, 5) == "2026-10-19"   # 过了零点又占了一份
    await quota.give_back(name, day=day)                 # 前一天占的那一份发起失败
    assert await quota.used(name, day="2026-10-18") == 0
    assert await quota.used(name, day="2026-10-19") == 1, "第二天的那一份不动"


def test_a_daily_quota_holds_across_two_processes():
    """两个进程同时各占 15 次、上限 10：合计正好 10 次。"""
    name = f"test-quota-{new_id()[:8]}"
    backend = Path(__file__).resolve().parents[1]
    script = ("import asyncio, sys\n"
              "from app.cluster import quota\n"
              "async def main():\n"
              f"    got = await asyncio.gather(*(quota.take({name!r}, 10) for _ in range(15)))\n"
              "    print(sum(1 for g in got if g))\n"
              "asyncio.run(main())\n")
    env = {**os.environ, "PYTHONPATH": str(backend), "AGENTLAB_DB_POOL_SIZE": "5"}
    procs = [subprocess.Popen([sys.executable, "-c", script], cwd=str(backend), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-2000:] for o in outs]
    assert sum(int(o[0].strip().splitlines()[-1]) for o in outs) == 10


# --------------------------------------------------------------------------
# 不许新旧版本混跑（cluster/version.py）
# --------------------------------------------------------------------------

async def _peer_version(owner: str, code: str | None, *, lease_s: float = 60.0) -> None:
    now = utcnow()
    async with SessionLocal() as session:
        session.add(ClusterWorker(id=owner, host="peer-host", pid=4242, started_at=now, heartbeat_at=now,
                                  lease_until=now + timedelta(seconds=lease_s), code_version=code,
                                  schema_version=None if code is None else _schema()))
        await session.commit()


def _schema() -> str:
    from app.cluster import version

    return version.schema_version()


async def _rows():
    async with SessionLocal() as session:
        return list((await session.execute(select(ClusterWorker))).scalars())


async def test_a_live_process_of_another_version_is_named_and_refused():
    from app.cluster import version

    await _peer_version(PEER, "0.0.9+old+aaaaaaaaaaaa")
    with pytest.raises(version.VersionMismatch) as e:
        version.check_rows(await _rows(), utcnow())
    assert "peer-host 上的进程 4242" in str(e.value) and "0.0.9+old+aaaaaaaaaaaa" in str(e.value) and "先停掉" in str(e.value)


async def test_the_same_version_joins_and_expired_or_unversioned_dead_ones_do_not_count():
    from app.cluster import version

    await _peer_version(PEER, version.code_version())
    await _peer_version(GHOST, "0.0.9+old+aaaaaaaaaaaa", lease_s=-5)
    version.check_rows(await _rows(), utcnow())
    assert version.same("0.1.0+other-build+" + version.source_fingerprint(), version.schema_version()), \
        "比的是源码指纹：构建号写法不同、代码相同算同一版"


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="只在 PostgreSQL 上拦（SQLite 只有一个进程）")
async def test_preflight_refuses_a_beating_peer_and_waits_out_a_stale_one():
    from app.cluster import version

    await _peer_version(PEER, "0.0.9+old+aaaaaaaaaaaa")

    async def beat() -> None:
        for _ in range(20):
            await asyncio.sleep(0.1)
            async with SessionLocal() as session:
                row = await session.get(ClusterWorker, PEER)
                row.heartbeat_at = utcnow()
                await session.commit()

    beating = asyncio.create_task(beat())
    with pytest.raises(version.VersionMismatch):
        await version.preflight(observe_s=0.5)
    await beating
    async with SessionLocal() as session:
        await session.execute(delete(ClusterWorker).where(ClusterWorker.id == PEER))
        await session.commit()
    # 强杀之后留下的旧版登记（心跳停着、租约还有 1 秒）：等它过期就起
    await _peer_version(GHOST, "0.0.9+old+aaaaaaaaaaaa", lease_s=1.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await version.preflight(observe_s=0.3)
    assert 0.5 < loop.time() - t0 < 5
    with pytest.raises(version.VersionMismatch):
        await _peer_version(PEER, "0.0.9+old+aaaaaaaaaaaa")
        await workers.register()


@pytest.mark.skipif(app_settings.db_dialect != "postgresql", reason="preflight 只在 PostgreSQL 上看")
async def test_preflight_names_every_live_process_of_another_version():
    """复核 R-b13：一个心跳在走、一个心跳停着——报错两个都列出来（只点名先看到心跳的那个，人停掉它再起还会被另一个拦下）。"""
    from app.cluster import version

    await _peer_version(PEER, "0.0.9+old+aaaaaaaaaaaa")
    await _peer_version(GHOST, "0.0.8+older+bbbbbbbbbbbb")

    async def beat() -> None:
        for _ in range(15):
            await asyncio.sleep(0.1)
            async with SessionLocal() as session:
                row = await session.get(ClusterWorker, PEER)
                row.heartbeat_at = utcnow()
                await session.commit()

    beating = asyncio.create_task(beat())
    with pytest.raises(version.VersionMismatch) as e:
        await version.preflight(observe_s=0.5)
    await beating
    text = str(e.value)
    assert "2 个版本不同" in text and "aaaaaaaaaaaa" in text and "bbbbbbbbbbbb" in text
    assert "心跳在走" in text and "心跳停着" in text


def test_the_fingerprint_changes_with_the_dependency_lock(monkeypatch, tmp_path):
    """复核 R-b13：依赖锁文件变了（升了依赖）源码指纹跟着变；锁文件取不到时换成几个关键包的版本。"""
    from app.cluster import version

    lock = tmp_path / "requirements.lock"
    monkeypatch.setattr(version, "LOCK_FILE", lock)

    def fingerprint() -> str:
        version.source_fingerprint.cache_clear()
        return version.source_fingerprint()

    try:
        lock.write_text("sqlalchemy==2.0.40\n")
        first = fingerprint()
        assert fingerprint() == first
        lock.write_text("sqlalchemy==2.0.41\n")
        assert fingerprint() != first, "升了一个依赖：指纹不同"
        lock.unlink()
        assert version.dependency_marker().startswith(b"packages:") and b"sqlalchemy==" in version.dependency_marker()
        assert fingerprint() not in (first,)
    finally:
        version.source_fingerprint.cache_clear()


async def test_a_known_seq_collision_is_not_rescanned_on_every_start(tmp_path, caplog):
    """复核 R-b13：存量库里有撞了号的事件——第一次启动整表数一遍、记下结果、不建唯一索引；之后启动只核对记下的那几处，不再整表
    GROUP BY；撞号处理掉之后再数一遍、建索引、删掉记录。用一个单独的 SQLite 库（两种库的测试里都跑）。"""
    import logging

    from sqlalchemy import event, inspect, text

    from app.db import base as db_base
    from app.db.models import Setting

    target = db_base.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'seq.db'}")
    scans: list[str] = []
    event.listen(target.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *a: scans.append(statement) if "GROUP BY run_id, seq" in statement else None)
    try:
        await db_base.create_schema(target)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with target.begin() as conn:
            await conn.execute(text("DROP INDEX ux_run_events_run_seq"))
            await conn.execute(text("CREATE INDEX ix_run_events_run_seq ON run_events (run_id, seq)"))
        async with async_sessionmaker(target, expire_on_commit=False)() as session:
            session.add(Run(id="r1", status="succeeded", graph={}, input={}))
            await session.flush()
            session.add_all([RunEvent(id=eid, run_id="r1", seq=seq, type="log", ts=0, data={})
                             for eid, seq in (("e1", 1), ("e2", 1), ("e3", 2))])
            await session.commit()

        async def start() -> set[str]:
            async with target.begin() as conn:
                def run(sync_conn):
                    cols = {t: {c["name"] for c in inspect(sync_conn).get_columns(t)} for t in ("run_events", "settings")}
                    db_base._unique_run_event_seq(sync_conn, cols)
                    return {ix["name"] for ix in inspect(sync_conn).get_indexes("run_events")}
                return await conn.run_sync(run)

        caplog.set_level(logging.INFO, logger="app.db.base")
        assert "ux_run_events_run_seq" not in await start() and len(scans) == 1
        async with target.connect() as conn:
            marker = (await conn.execute(text(f"SELECT value FROM settings WHERE key = '{db_base.SEQ_DUPLICATES_KEY}'"))).scalar()
        assert '"r1"' in str(marker) and "1" in str(marker)
        assert "ux_run_events_run_seq" not in await start() and len(scans) == 1, "第二次启动不再整表 GROUP BY"
        assert any("还有撞了号的事件" in r.getMessage() for r in caplog.records)
        async with target.begin() as conn:
            await conn.execute(text("DELETE FROM run_events WHERE id = 'e2'"))
        assert "ux_run_events_run_seq" in await start() and len(scans) == 2, "处理掉之后再数一遍、建上索引"
        async with target.connect() as conn:
            left = (await conn.execute(text(f"SELECT COUNT(*) FROM settings WHERE key = '{db_base.SEQ_DUPLICATES_KEY}'"))).scalar()
        assert left == 0
        assert Setting.__tablename__ == "settings"
    finally:
        await target.dispose()


@pytest.mark.sqlite_only
async def test_an_idle_single_process_writes_nothing_for_coordination(monkeypatch):
    """复核 R-b3：SQLite（只有一个进程）空闲时协调不写库——不续租约、不写当班、不周期接手；要名额、放名额也不落库。用另一条连接
    读 PRAGMA data_version：别的连接提交过写事务它就会变。"""
    import sqlite3

    from app import cluster
    from app.core.config import settings

    monkeypatch.setattr(settings, "worker_heartbeat_s", 0.05)
    await cluster.start()
    try:
        await asyncio.sleep(0.2)
        probe = sqlite3.connect(settings.db_path)
        try:
            before = probe.execute("PRAGMA data_version").fetchone()[0]
            await asyncio.sleep(0.6)                       # 十来拍心跳
            async with slots.hold("idle-check", "chat"):    # 一段运行要一个名额、用完放掉
                pass
            after = probe.execute("PRAGMA data_version").fetchone()[0]
        finally:
            probe.close()
        assert before == after, "空闲的单进程为协调写了库"
        assert cluster.leading(), "只有本进程：直接当班"
    finally:
        await cluster.stop()


def test_the_worker_count_comes_from_the_backend_settings():
    """复核 R-b4：prod.sh、Docker 的入口按 python -m app.cluster workers 起几个进程（后端自己读环境变量和 .env）；SQLite 而大于 1 时
    照实报错、退出码 2。"""
    backend = Path(__file__).resolve().parents[1]
    base = {k: v for k, v in os.environ.items() if not k.startswith("AGENTLAB_")}
    base.update(PYTHONPATH=str(backend), AGENTLAB_DATA_DIR=os.environ["AGENTLAB_DATA_DIR"],
                AGENTLAB_SECRET_KEY="test-only-not-a-real-key")

    def ask(**env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "app.cluster", "workers"], cwd=str(backend), env={**base, **env},
                              capture_output=True, text=True, timeout=60)

    sqlite = ask(AGENTLAB_DATABASE_URL="", AGENTLAB_WORKERS="3")
    assert sqlite.returncode == 2 and "只支持单进程" in sqlite.stderr, sqlite.stderr[-1000:]
    assert ask(AGENTLAB_DATABASE_URL="", AGENTLAB_WORKERS="1").stdout.strip() == "1"
    pg = ask(AGENTLAB_DATABASE_URL="postgresql://u:p@db.internal:5432/agentlab", AGENTLAB_WORKERS="3")
    assert pg.returncode == 0 and pg.stdout.strip() == "3", pg.stderr[-1000:]


async def test_the_relay_rereads_a_little_behind_so_a_late_commit_is_not_skipped():
    """复核 R-b5：号先取后提交，号大的先提交时游标会越过号小的那一条——转发器往回留一小段重读、按发布过的 seq 去重。"""
    from app.cluster.relay import relay
    from app.core.bus import bus

    async with SessionLocal() as session:
        run = Run(status="running", owner=PEER, graph={}, input={})
        session.add(run)
        await session.flush()
        session.add_all([RunEvent(id=new_id(), run_id=run.id, seq=s, type="log", ts=0, data={"n": s}) for s in (1, 3)])
        await session.commit()
    got: list[int] = []
    with bus.attach(run.id) as sub, relay.follow_run(run.id, 0):
        async def collect() -> None:
            async for event in sub.stream():
                got.append(event.seq)

        reader = asyncio.create_task(collect())
        await relay.pump(run.id)
        async with SessionLocal() as session:   # 号小的那一条后提交
            session.add(RunEvent(id=new_id(), run_id=run.id, seq=2, type="log", ts=0, data={"n": 2}))
            await session.commit()
        await relay.pump(run.id)
        await relay.pump(run.id)
        await asyncio.sleep(0.1)
        reader.cancel()
    assert sorted(got) == [1, 2, 3] and len(got) == 3, got


async def test_cache_invalidations_committed_out_of_order_are_all_applied(monkeypatch):
    """复核 R-b5：缓存失效的通知 id 大的先提交，id 小的后到也照样处理（往回留一小段重读、按 id 去重）。"""
    from app.cluster import caches
    from app.db.models_cluster import CacheEvent

    seen: list[str | None] = []
    caches.on("test-cache", lambda key: seen.append(key))
    monkeypatch.setattr(caches, "_enabled", lambda: True)
    await caches.start()
    base = caches._last_seen or 0
    async with SessionLocal() as session:
        session.add(CacheEvent(id=base + 5, name="test-cache", key="late-big", owner=PEER))
        await session.commit()
    assert await caches.poll() == 1
    async with SessionLocal() as session:
        session.add(CacheEvent(id=base + 3, name="test-cache", key="early-small", owner=PEER))
        await session.commit()
    assert await caches.poll() == 1 and await caches.poll() == 0
    assert seen == ["late-big", "early-small"]


def test_a_long_merged_delta_is_split_not_cut():
    """复核 R-b6：几条正文增量合成一条之后超过载荷上限，按字切成几条（不切坏 UTF-8），拼回来一字不少。"""
    import json

    from app.cluster.relay import PAYLOAD_MAX, _chunks

    items = [{"type": "llm.token", "node_id": "w", "seq": i, "data": {"delta": "数据" * 500 + "😀"}} for i in range(1, 9)]
    chunks = list(_chunks(items))
    assert all(len(json.dumps(c, ensure_ascii=False).encode()) <= PAYLOAD_MAX for c in chunks)
    text = "".join(e["data"]["delta"] for c in chunks for e in c)
    assert text == ("数据" * 500 + "😀") * 8
