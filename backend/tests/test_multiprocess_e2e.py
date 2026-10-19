"""多进程端到端（docs/design/orchestration-round/00-总方案.md §5 W2-b 的验收）：PostgreSQL 上起两个工作进程（uvicorn，A、B），
指着同一个库、同一个数据目录，用演示模型（Mock）跑：

1. 两个进程都登记了、只有一个当调度班；
2. 运行由接到请求的那个进程认领；另一个进程上的 WebSocket 收得到实时事件（含不落库的流式增量）；同一个审批决定同时发给两个
   进程只执行一次；两边的事件流都按 seq 递增、不重不漏，封存核对得上；
3. 在 A 上跑的运行，从 B 停止：照本进程的停止办（run.cancelled 带上是谁停的）；
4. 定时任务到点只触发一次（两个进程都起着调度循环，只有当班的做）；
5. 评测和问数据并发时优先级生效：全站只给 1 个名额（没有留底），评测的作答运行（系统类，由本测试进程作为第三个登记的工作进程
   以评测的来源发起——工作进程里发起评测要真的去调模型建图）排着队时，后来的问数据运行（交互类，经 A 的接口发起）先拿到名额；
   全站 2 个、给交互留底 1 个：评测借满了别的名额、还有题排着，问数据一来马上拿到留底，不等评测跑完；
6. 每日花费上限跨进程不越过：两个进程同时往同一本账上记，记下的合计不超过上限；
7. 强杀 B：租约过期后，B 上在跑的运行被照实标中断（可从断点继续），停在审批上的运行照常能在 A 上批、接着跑完；B 当着调度班的
   话 A 接班；
8. 卡住但没死（复核 R-b1）：A 在跑一次运行时被 SIGSTOP，B 等它的租约过期两倍时长后接手、标中断；A 缓过来（SIGCONT）之后发现续晚了，
   停手、不写终态、不写事件，换一个身份重新登记——不管 B 有没有在这期间把那次运行接着跑完，都不双写、不盖掉终态、序号不重。

缺省不跑：要 AGENTLAB_MULTIPROCESS_E2E=1 和 PostgreSQL 测试库（scripts/test-multiprocess.sh 起临时容器、设好这两样）。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTLAB_MULTIPROCESS_E2E") != "1" or not os.environ.get("AGENTLAB_TEST_DATABASE_URL"),
    reason="多进程端到端缺省不跑：用 scripts/test-multiprocess.sh（要本机 docker 的 postgres:16）")

BACKEND = Path(__file__).resolve().parents[1]
#: 工作进程的租约和心跳：调小，强杀之后几秒就能看到接手
HEARTBEAT_S = 0.5
LEASE_S = 2.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node(nid: str, ntype: str, **config: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def _sleep_code(seconds: float) -> dict[str, Any]:
    return _node("wait", "code", language="python", code=f"import time\ntime.sleep({seconds})\nprint('ok')")


def slow_graph(seconds: float) -> dict[str, Any]:
    return _chain(_node("start", "input", fields=[{"name": "question"}]), _sleep_code(seconds),
                  _node("out", "output", fields=[{"name": "r", "value": "{{ nodes.wait.text }}"}]))


APPROVAL_GRAPH = _chain(
    _node("start", "input", fields=[{"name": "question"}]),
    _sleep_code(1.0),
    _node("gate", "human", mode="approve", title="看一眼再接着"),
    _node("write", "llm", prompt="用三句话写一段说明"),
    _node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.write.text }}"}]),
)


class Worker:
    def __init__(self, name: str, env: dict[str, str], log_dir: Path) -> None:
        self.name = name
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log_dir / f"worker-{name}.log"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(BACKEND), env=env, stdout=open(self.log, "wb"), stderr=subprocess.STDOUT)
        self.owner: str | None = None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def pause(self) -> None:
        self.proc.send_signal(signal.SIGSTOP)

    def resume(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.kill()

    def tail(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")[-12000:]
        except OSError:
            return ""


def _worker_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                        "PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST")}
    env.update({
        "PYTHONPATH": str(BACKEND),
        # 应用库、数据目录：和本测试进程同一个（conftest 已经设好）
        "AGENTLAB_DATABASE_URL": os.environ["AGENTLAB_DATABASE_URL"],
        "AGENTLAB_DATA_DIR": os.environ["AGENTLAB_DATA_DIR"],
        "AGENTLAB_DB_POOL_SIZE": "5",
        "AGENTLAB_SCHEDULER": "1",
        "AGENTLAB_SCHEDULER_TICK_S": "0.5",
        "AGENTLAB_WORKER_HEARTBEAT_S": str(HEARTBEAT_S),
        "AGENTLAB_WORKER_LEASE_S": str(LEASE_S),
        "AGENTLAB_SANDBOX_BACKEND": "local",
        "AGENTLAB_AUTH_MODE": "off",
    })
    return env


@pytest.fixture(scope="module")
def workers(tmp_path_factory):
    import httpx

    log_dir = tmp_path_factory.mktemp("multiprocess")
    env = _worker_env()
    started = [Worker("A", env, log_dir), Worker("B", env, log_dir)]
    try:
        deadline = time.monotonic() + 90
        for w in started:
            while True:
                if w.proc.poll() is not None:
                    raise AssertionError(f"工作进程 {w.name} 没起来：\n{w.tail()}")
                try:
                    if httpx.get(f"{w.base}/api/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(f"工作进程 {w.name} 90 秒没起来：\n{w.tail()}")
                time.sleep(0.3)
            w.owner = httpx.get(f"{w.base}/api/system", timeout=10).json()["cluster"]["me"]
        yield {w.name: w for w in started}
    finally:
        for w in started:
            w.stop()
        for w in started:
            print(f"---- 工作进程 {w.name} 的日志（末尾）----\n{w.tail()}")


async def _client():
    import httpx

    return httpx.AsyncClient(timeout=30)


async def _until(check, timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"等超时了：{what}（最后一次：{last!r}）")


async def _run(client, worker: Worker, run_id: str) -> dict[str, Any]:
    return (await client.get(f"{worker.base}/api/runs/{run_id}")).json()


async def _db(sql: str, *args: Any) -> list[Any]:
    from sqlalchemy import text

    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        return list((await session.execute(text(sql), _params(args))).all())


def _params(args: tuple[Any, ...]) -> dict[str, Any]:
    return {f"p{i}": v for i, v in enumerate(args)}


class Stream:
    """一条 WebSocket（/api/runs/{id}/stream）收到的全部消息。"""

    def __init__(self, worker: Worker, run_id: str) -> None:
        self.url = f"ws://127.0.0.1:{worker.port}/api/runs/{run_id}/stream?after=0"
        self.messages: list[dict[str, Any]] = []
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> Stream:
        import websockets

        ready = asyncio.Event()

        async def pump() -> None:
            async with websockets.connect(self.url) as ws:
                ready.set()
                async for raw in ws:
                    msg = json.loads(raw)
                    self.messages.append(msg)
                    if msg.get("type") == "stream.end":
                        return

        self.task = asyncio.create_task(pump())
        await asyncio.wait_for(ready.wait(), timeout=10)
        return self

    def types(self) -> list[str]:
        return [m.get("type") for m in self.messages]

    def seqs(self) -> list[int]:
        """落库事件的 seq（不落库的流式增量另算）。"""
        return [int(m["seq"]) for m in self.messages if m.get("seq") and m.get("type") not in ("llm.token",
                                                                                                "llm.thinking.delta")]

    async def ended(self, timeout: float = 30.0) -> None:
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=timeout)


# --------------------------------------------------------------------------


async def test_1_two_workers_one_leader(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        async def both() -> Any:
            info = (await client.get(f"{a.base}/api/system")).json()["cluster"]
            alive = {w["id"] for w in info["workers"] if w["alive"]}
            return info if {a.owner, b.owner} <= alive else None

        info = await _until(both, what="两个进程都登记")
        assert info["database"] == "postgresql"
        assert info["leader"]["owner"] in (a.owner, b.owner)
        other = (await client.get(f"{b.base}/api/system")).json()["cluster"]
        assert other["leader"]["owner"] == info["leader"]["owner"], "两边看到的当班是同一个"


async def test_2_live_events_across_workers_and_one_decision(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        made = (await client.post(f"{a.base}/api/runs", json={"graph": APPROVAL_GRAPH, "input": {"question": "q"},
                                                               "origin": "canvas"})).json()
        run_id = made["id"]
        # 两边各接一条流（趁它还在跑：停在审批上之后才接的流回放完就结束，要等恢复之后重新接）
        on_b = await Stream(b, run_id).start()
        on_a = await Stream(a, run_id).start()
        owner = await _until(lambda: _owner_while_active(run_id), what="运行被认领")
        assert owner == a.owner, "运行由接到请求的进程认领"

        async def paused() -> Any:
            return (await _run(client, b, run_id))["status"] == "interrupted"

        await _until(paused, what="停在审批上")
        await _until(lambda: _has(on_b, "run.interrupted"), what="B 上的流收到 run.interrupted")
        # 同一个决定同时发给两个进程：只执行一次
        decided = await asyncio.gather(
            client.post(f"{a.base}/api/runs/{run_id}/resume", json={"response": {"approved": True}}),
            client.post(f"{b.base}/api/runs/{run_id}/resume", json={"response": {"approved": True}}))
        assert sorted(r.status_code for r in decided)[0] == 200, [r.text for r in decided]

        async def finished() -> Any:
            return (await _run(client, a, run_id))["status"] in ("succeeded", "failed")

        await _until(finished, timeout=60, what="运行跑完")
        assert (await _run(client, a, run_id))["status"] == "succeeded"
        await on_a.ended()
        await on_b.ended()
        for stream in (on_a, on_b):
            assert "run.finished" in stream.types(), stream.types()
            seqs = stream.seqs()
            assert seqs == sorted(set(seqs)), f"事件流有重复或乱序：{seqs}"
        started = [m for m in on_a.messages if m.get("type") == "node.started" and m.get("node_id") == "write"]
        assert len(started) == 1, "审批之后的那一步只执行了一次"
        # 执行后半段的进程不是谁、另一个进程上的流照样收到了流式增量（不落库，只转给在看的进程）
        assert any(m.get("type") == "llm.token" for m in on_a.messages) and \
            any(m.get("type") == "llm.token" for m in on_b.messages), "两边都收到了正文增量"
        rows = await _db("SELECT seq, count(*) FROM run_events WHERE run_id = :p0 GROUP BY seq HAVING count(*) > 1", run_id)
        assert rows == [], f"库里的事件撞了号：{rows}"
        verified = (await client.get(f"{b.base}/api/runs/{run_id}/verify")).json()
        assert verified["ok"] is True, verified


async def _owner_while_active(run_id: str) -> str | None:
    rows = await _db("SELECT owner, status FROM runs WHERE id = :p0", run_id)
    if rows and rows[0][1] in ("running", "queued") and rows[0][0]:
        return rows[0][0]
    return None


async def _has(stream: Stream, kind: str) -> bool:
    return kind in stream.types()


async def test_3_stop_from_the_other_worker(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        run_id = (await client.post(f"{a.base}/api/runs", json={"graph": slow_graph(20), "input": {"question": "q"},
                                                                 "origin": "canvas"})).json()["id"]
        await _until(lambda: _owner_while_active(run_id), what="运行被认领")
        t0 = time.monotonic()
        stop = await client.post(f"{b.base}/api/runs/{run_id}/cancel", headers={"X-Actor": "e2e-stopper"})
        assert stop.status_code == 200 and stop.json()["status"] == "stopping", stop.text

        async def cancelled() -> Any:
            row = await _run(client, b, run_id)
            return row if row["status"] == "cancelled" else None

        row = await _until(cancelled, timeout=15, what="停止生效")
        assert row["end_reason"] == "stopped"
        assert time.monotonic() - t0 < 10, "不是等它跑完才停的"
        events = (await client.get(f"{a.base}/api/runs/{run_id}/events")).json()
        stopped = [e for e in events if e["type"] == "run.cancelled"]
        assert stopped and stopped[-1]["data"].get("actor") == "e2e-stopper", stopped


async def test_4_a_schedule_fires_once(workers):
    a = workers["A"]
    async with await _client() as client:
        wf = (await client.post(f"{a.base}/api/workflows", json={"name": "定时验收", "graph": slow_graph(0.1)})).json()
        made = await client.post(f"{a.base}/api/schedules", json={
            "name": "每分钟一次", "workflow_id": wf["id"], "cron": "* * * * *", "timezone": "Asia/Shanghai", "run_class": "exploratory",
            "input_template": {"question": "定时"}})
        assert made.status_code == 201, made.text
        schedule_id = made.json()["id"]
        # 拨到「刚过点」：两个进程的调度循环都在转，只有当班的那个做
        from sqlalchemy import text

        from app.db.base import SessionLocal, utcnow

        due = utcnow().replace(microsecond=0)
        async with SessionLocal() as session:
            await session.execute(text("UPDATE schedules SET next_run_at = :t WHERE id = :i"), {"t": due.replace(tzinfo=None),
                                                                                                "i": schedule_id})
            await session.commit()

        async def fired() -> Any:
            rows = await _db("SELECT id FROM schedule_fires WHERE schedule_id = :p0", schedule_id)
            return rows or None

        await _until(fired, timeout=15, what="定时触发")
        await asyncio.sleep(3)   # 再等几拍：不会再发一次
        fires = await _db("SELECT kind, status, run_id FROM schedule_fires WHERE schedule_id = :p0", schedule_id)
        runs = await _db("SELECT id FROM runs WHERE schedule_id = :p0", schedule_id)
        assert len(fires) == 1 and len(runs) == 1, (fires, runs)
        await client.patch(f"{a.base}/api/schedules/{schedule_id}", json={"enabled": False})


async def test_5_chat_goes_before_queued_evaluation_runs(workers, monkeypatch):
    """全站 1 个名额：评测的作答运行（系统类）排着两个，问数据的运行（交互类）后来，先拿到名额。"""
    from app import cluster
    from app.core.config import settings
    from app.cluster import slots
    from app.cluster.relay import relay
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.engine.runner import run_manager

    a = workers["A"]
    async with await _client() as client:
        put = await client.put(f"{a.base}/api/settings", json={"values": {"concurrency": {
            "max_total": 1, "classes": {"interactive": {"limit": 1, "priority": 40}, "system": {"limit": 1, "priority": 10}}}}})
        assert put.status_code == 200, put.text
        slots.forget_policy()
        monkeypatch.setattr(settings, "sandbox_backend", "local")
        await cluster.start()          # 本测试进程登记成第三个工作进程，替评测发起作答运行
        await relay.start()
        await run_manager.setup()
        try:
            evals = [await run_manager.start(graph=slow_graph(1.5), input_payload={"question": f"评测 {i}"},
                                             origin_kind="eval", origin={"kind": "eval"}) for i in range(3)]

            async def first_running() -> Any:
                async with SessionLocal() as session:
                    return (await session.get(Run, evals[0].id)).status == "running"

            await _until(first_running, what="第一题的作答运行拿到名额")
            await asyncio.sleep(0.3)
            chat_id = (await client.post(f"{a.base}/api/runs", json={
                "graph": slow_graph(0.2), "input": {"question": "问数据"}, "origin": "chat"})).json()["id"]

            async def all_done() -> Any:
                async with SessionLocal() as session:
                    rows = [await session.get(Run, rid, populate_existing=True) for rid in [*[e.id for e in evals], chat_id]]
                return rows if all(r.status in ("succeeded", "failed") for r in rows) else None

            rows = await _until(all_done, timeout=60, what="都跑完")
            assert all(r.status == "succeeded" for r in rows), [(r.id, r.status, r.error) for r in rows]
            e1, e2, e3, chat = rows
            assert e2.created_at < chat.created_at and e3.created_at < chat.created_at, "评测的两题先来、排着队"
            assert e1.started_at < chat.started_at < e2.started_at < e3.started_at, \
                [(r.origin_kind, r.created_at, r.started_at) for r in rows]
        finally:
            await run_manager.shutdown()
            await relay.stop()
            await cluster.stop()
            await client.put(f"{a.base}/api/settings", json={"values": {"concurrency": slots.defaults()}})


async def test_5b_the_interactive_reserve_is_there_when_evaluations_fill_the_rest(workers, monkeypatch):
    """全站 2 个名额、留底 1 个：评测（系统类）只借得到 1 个，第二题排着；问数据一来马上拿到留底，评测的第一题还在跑。"""
    from app import cluster
    from app.cluster import slots
    from app.cluster.relay import relay
    from app.core.config import settings
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.engine.runner import run_manager

    a = workers["A"]
    async with await _client() as client:
        put = await client.put(f"{a.base}/api/settings", json={"values": {"concurrency": {
            "max_total": 2, "interactive_reserve": 1, "classes": {}}}})
        assert put.status_code == 200, put.text
        got = put.json()["concurrency"]
        assert got["max_total"] - got["interactive_reserve"] == 1, "别的类最多借得到 全站 − 留底 = 1 个"
        slots.forget_policy()
        monkeypatch.setattr(settings, "sandbox_backend", "local")
        await cluster.start()
        await relay.start()
        await run_manager.setup()
        try:
            evals = [await run_manager.start(graph=slow_graph(3), input_payload={"question": f"评测 {i}"},
                                             origin_kind="eval", origin={"kind": "eval"}) for i in range(2)]

            async def first_running() -> Any:
                async with SessionLocal() as session:
                    return (await session.get(Run, evals[0].id)).status == "running"

            await _until(first_running, what="第一题的作答运行拿到名额")
            chat_id = (await client.post(f"{a.base}/api/runs", json={
                "graph": slow_graph(0.2), "input": {"question": "问数据"}, "origin": "chat"})).json()["id"]

            async def all_done() -> Any:
                async with SessionLocal() as session:
                    rows = [await session.get(Run, rid, populate_existing=True) for rid in [*[e.id for e in evals], chat_id]]
                return rows if all(r.status in ("succeeded", "failed") for r in rows) else None

            e1, e2, chat = await _until(all_done, timeout=60, what="都跑完")
            assert chat.started_at < e1.finished_at, "问数据拿到的是留底：评测的第一题还没跑完它就开始了"
            assert e2.started_at >= e1.finished_at, "评测的第二题借不到留底，等第一题跑完"
        finally:
            await run_manager.shutdown()
            await relay.stop()
            await cluster.stop()
            await client.put(f"{a.base}/api/settings", json={"values": {"concurrency": slots.defaults()}})


def test_6_the_daily_cap_holds_across_processes(workers):
    """两个进程同时往评测那本账上各记 20 次、每次 0.1 美元，上限 1 美元：合计记下的正好 10 次、不超过上限。"""
    script = (
        "import asyncio, json\n"
        "from app.engine.spend import Ledger\n"
        "async def main():\n"
        "    book = Ledger('e2e_spend')\n"
        "    got = await asyncio.gather(*(book.charge(0.1, limit=1.0, priced=True) for _ in range(20)))\n"
        "    print(json.dumps(sum(1 for d in got if d)))\n"
        "asyncio.run(main())\n")
    env = {**_worker_env(), "AGENTLAB_SCHEDULER": "0"}
    procs = [subprocess.Popen([sys.executable, "-c", script], cwd=str(BACKEND), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1].decode()[-2000:] for o in outs]
    charged = sum(int(o[0].decode().strip().splitlines()[-1]) for o in outs)
    assert charged == 10, f"两个进程合计记下 {charged} 次（上限只容得下 10 次）"
    total = asyncio.run(_spent())
    assert total <= 1.0 + 1e-9, total


async def _spent() -> float:
    from app.engine.spend import Ledger

    return float((await Ledger("e2e_spend").daily())["usd"])


async def _stall_a_during_a_run(client, a: Worker, b: Worker, seconds: float) -> str:
    """A 上发起一次运行，跑起来之后 SIGSTOP A，等 B 接手（标中断）。交回运行 id（A 还停着，调用方负责 SIGCONT）。"""
    run_id = (await client.post(f"{a.base}/api/runs", json={"graph": slow_graph(seconds), "input": {"question": "q"},
                                                             "origin": "canvas"})).json()["id"]
    await _until(lambda: _owner_while_active(run_id), what="A 认领了运行")
    await asyncio.sleep(0.5)
    a.pause()

    async def taken_over() -> Any:
        row = await _run(client, b, run_id)
        return row if row["status"] == "interrupted" else None

    await _until(taken_over, timeout=LEASE_S * 6 + 10, what="B 接手了卡住的 A 上的运行")
    return run_id


async def _a_reidentified(client, a: Worker) -> None:
    """A 缓过来之后换了身份重新登记：fixture 里记的 owner 跟着换。"""
    old = a.owner

    async def fresh() -> Any:
        try:
            me = (await client.get(f"{a.base}/api/system")).json()["cluster"]["me"]
        except Exception:  # noqa: BLE001
            return None
        return me if me != old else None

    a.owner = await _until(fresh, timeout=15, what="A 换身份重新登记")


async def _settled_events(client, worker: Worker, run_id: str) -> list[dict[str, Any]]:
    return (await client.get(f"{worker.base}/api/runs/{run_id}/events")).json()


def _seqs_unique(events: list[dict[str, Any]]) -> None:
    seqs = [e["seq"] for e in events]
    assert len(seqs) == len(set(seqs)), f"撞号：{sorted(seqs)}"


async def test_6b_a_stalled_worker_does_not_write_after_it_was_taken_over(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        try:
            run_id = await _stall_a_during_a_run(client, a, b, 6)
        finally:
            a.resume()
        await _a_reidentified(client, a)
        await asyncio.sleep(max(4.0, 3 * HEARTBEAT_S))     # A 手上那一步本来再过几秒就跑完：给它机会（它不该写）
        row = await _run(client, b, run_id)
        assert row["status"] == "interrupted", f"接手之后的状态被盖掉了：{row['status']}"
        events = await _settled_events(client, b, run_id)
        assert not [e for e in events if e["type"] in ("run.finished", "run.failed", "run.cancelled")]
        assert any(e["type"] == "log" and e["data"].get("code") == "server_shutdown" for e in events)
        _seqs_unique(events)


async def test_6c_a_run_continued_by_the_other_worker_is_not_written_twice(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        try:
            run_id = await _stall_a_during_a_run(client, a, b, 3)
            cont = await client.post(f"{b.base}/api/runs/{run_id}/continue", json={})
            assert cont.status_code == 200, cont.text
        finally:
            a.resume()
        await _a_reidentified(client, a)

        async def done() -> Any:
            row = await _run(client, b, run_id)
            return row if row["status"] in ("succeeded", "failed") and row.get("manifest_hash") else None

        row = await _until(done, timeout=60, what="B 接着跑完")
        await asyncio.sleep(2.0)
        assert row["status"] == "succeeded", row
        assert (await _run(client, b, run_id))["status"] == "succeeded", "A 缓过来之后没有把终态改掉"
        events = await _settled_events(client, b, run_id)
        assert len([e for e in events if e["type"] == "run.finished"]) == 1
        _seqs_unique(events)
        verified = (await client.get(f"{b.base}/api/runs/{run_id}/verify")).json()
        assert verified["ok"] is True, verified


async def test_7_kill_a_worker(workers):
    a, b = workers["A"], workers["B"]
    async with await _client() as client:
        leader_before = (await client.get(f"{a.base}/api/system")).json()["cluster"]["leader"]["owner"]
        # B 上：一次在跑（会被照实标中断），一次停在审批上（不属于任何进程，B 没了照样能在 A 上批）
        busy = (await client.post(f"{b.base}/api/runs", json={"graph": slow_graph(30), "input": {"question": "q"},
                                                               "origin": "canvas"})).json()["id"]
        paused = (await client.post(f"{b.base}/api/runs", json={"graph": APPROVAL_GRAPH, "input": {"question": "q"},
                                                                 "origin": "canvas"})).json()["id"]
        await _until(lambda: _owner_while_active(busy), what="B 认领了运行")

        async def waiting() -> Any:
            return (await _run(client, a, paused))["status"] == "interrupted"

        await _until(waiting, what="停在审批上")
        b.kill()
        t0 = time.monotonic()

        async def taken_over() -> Any:
            row = await _run(client, a, busy)
            return row if row["status"] == "interrupted" else None

        row = await _until(taken_over, timeout=LEASE_S * 6 + 10, what="租约过期后被接手")
        assert "可从断点继续运行" in (row["error"] or "")
        events = (await client.get(f"{a.base}/api/runs/{busy}/events")).json()
        assert any(e["type"] == "log" and e["data"].get("code") == "server_shutdown" for e in events)
        print(f"强杀 B 之后 {time.monotonic() - t0:.1f} 秒被接手")
        info = (await client.get(f"{a.base}/api/system")).json()["cluster"]
        assert b.owner not in {w["id"] for w in info["workers"] if w["alive"]}
        if leader_before == b.owner:
            async def a_leads() -> Any:
                got = (await client.get(f"{a.base}/api/system")).json()["cluster"]["leader"]
                return got["owner"] == a.owner

            await _until(a_leads, timeout=LEASE_S * 6, what="A 接班")
        # 停在审批上的那一次：在 A 上批，接着跑完
        ok = await client.post(f"{a.base}/api/runs/{paused}/resume", json={"response": {"approved": True}})
        assert ok.status_code == 200, ok.text

        async def done() -> Any:
            return (await _run(client, a, paused))["status"] == "succeeded"

        await _until(done, timeout=60, what="批了之后在 A 上跑完")
        # 被标中断的那一次：在 A 上能接着跑（这里接上就停掉，不等它睡完）
        cont = await client.post(f"{a.base}/api/runs/{busy}/continue", json={})
        assert cont.status_code == 200, cont.text
        await client.post(f"{a.base}/api/runs/{busy}/cancel")
