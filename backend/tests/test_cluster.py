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


async def test_a_daily_quota_is_taken_up_to_the_limit_by_concurrent_callers():
    """先占额度再发起（cluster/quota）：20 个协程同时占、上限 5，正好 5 个占到；这一天第一次占按已有的记录起算；退回一份又能占。"""
    from app.cluster import quota

    name = f"test-quota-{new_id()[:8]}"

    async def seed(_session) -> int:
        return 2   # 今天已经做过 2 次

    got = await asyncio.gather(*(quota.take(name, 5, seed=seed) for _ in range(20)))
    assert sum(got) == 3 and await quota.used(name) == 5
    assert not await quota.take(name, 5)
    await quota.give_back(name)
    assert await quota.take(name, 5) and not await quota.take(name, 5)


def test_a_daily_quota_holds_across_two_processes():
    """两个进程同时各占 15 次、上限 10：合计正好 10 次。"""
    name = f"test-quota-{new_id()[:8]}"
    backend = Path(__file__).resolve().parents[1]
    script = ("import asyncio, sys\n"
              "from app.cluster import quota\n"
              "async def main():\n"
              f"    got = await asyncio.gather(*(quota.take({name!r}, 10) for _ in range(15)))\n"
              "    print(sum(got))\n"
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
