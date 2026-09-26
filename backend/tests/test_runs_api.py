"""运行与审批接口的形状：时区、列表筛选与翻页、审批上下文、删除保护、事件流收尾。

- 时区：库是 SQLite，DateTime 读回来是 naive 的，API 吐出 '2026-09-26T01:11:19'。
  JS 把不带偏移的 ISO 串当本地时间解析，列表整体慢 8 小时。
- 列表：只有一个 limit，待审批的运行排在第 145 位就永远翻不到。
- 审批：返回项里没有工作流名、节点名，全局待办列表拼不出一行像样的字。
- 删除：运行中的能删、封存过的正式运行一键就删——出具的追溯依据说没就没。
- 事件流：连到一条停在 interrupted 的运行，回放完就不再有下文，客户端以为它还在跑。
- 接着跑：服务重启挂起的运行被说成「在等待审批」，还让人去一张不存在的审批卡上处理；
  已取消的只说不行，不说该怎么办。不存在的运行，detail 带着 Python repr 的引号。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi import WebSocketDisconnect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow
from app.engine import compiler
from app.engine.runner import run_manager
from app.engine.schema import NodeType
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


GATED = chain(
    node("start", "input", fields=[{"name": "question"}]),
    node("gate", "human", label="发布前把关", mode="approve", title="这条公告可以发吗？"),
    node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.gate.approved }}"}]),
)


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 15.0) -> Run:
    for _ in range(int(timeout / 0.03)):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"等超时了：{run_id}")


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"时间不带时区：{value}"
    return parsed


# --------------------------------------------------------------------------
# 时区
# --------------------------------------------------------------------------


async def test_every_time_the_api_returns_carries_an_offset(client):
    r = await client.post("/api/runs", json={"graph": GATED, "input": {"question": "q"}})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    await _wait(run_id, ("interrupted",))

    body = (await client.get(f"/api/runs/{run_id}")).json()
    created = _aware(body["created_at"])
    _aware(body["started_at"])
    # 写进去的是 UTC 的"现在"，读回来也得是同一个时刻
    assert abs((datetime.now(timezone.utc) - created).total_seconds()) < 60

    listed = (await client.get("/api/runs", params={"limit": 5})).json()
    assert all(_aware(r["created_at"]) for r in listed)

    [approval] = (await client.get("/api/approvals", params={"run_id": run_id})).json()
    _aware(approval["created_at"])

    for artifact in (await client.get(f"/api/runs/{run_id}/artifacts")).json():
        _aware(artifact["created_at"])


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------


@pytest.fixture
async def seeded_runs():
    """一个专属工作流下的一组运行，时间逐条错开。只查它们，不受别的测试干扰。"""
    base = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    async with SessionLocal() as session:
        wf = Workflow(name=f"列表测试·周报-{uuid.uuid4().hex[:6]}", graph={})
        session.add(wf)
        await session.flush()
        spec = [
            ("succeeded", "formal"), ("failed", "exploratory"), ("cancelled", "exploratory"),
            ("interrupted", "formal"), ("succeeded", "exploratory"), ("failed", "formal"),
        ]
        runs = []
        for i, (status, run_class) in enumerate(spec):
            run = Run(workflow_id=wf.id, workflow_name=wf.name, status=status,
                      run_class=run_class, graph={}, input={},
                      created_at=base + timedelta(minutes=i))
            session.add(run)
            runs.append(run)
        await session.commit()
        ids = [r.id for r in runs]
    return wf.id, ids, wf.name   # 按创建时间从旧到新


async def test_status_filter_takes_a_comma_list(client, seeded_runs):
    wf_id, ids, _ = seeded_runs
    r = await client.get("/api/runs", params={"workflow_id": wf_id, "status": "failed,cancelled"})
    assert r.status_code == 200, r.text
    assert {x["status"] for x in r.json()} == {"failed", "cancelled"}
    assert len(r.json()) == 3


async def test_run_class_and_name_filters(client, seeded_runs):
    wf_id, ids, name = seeded_runs
    formal = (await client.get("/api/runs", params={"workflow_id": wf_id, "run_class": "formal"})).json()
    assert len(formal) == 3 and all(x["run_class"] == "formal" for x in formal)

    by_name = (await client.get("/api/runs", params={"q": name[3:]})).json()
    assert {x["id"] for x in by_name} == set(ids), "按工作流名模糊匹配"
    # 用户输入的 % 和 _ 按字面算，不当通配符
    assert (await client.get("/api/runs", params={"q": "列表%周报"})).json() == []


async def test_newest_first_and_before_is_a_cursor(client, seeded_runs):
    wf_id, ids, _ = seeded_runs
    page1 = (await client.get("/api/runs", params={"workflow_id": wf_id, "limit": 2})).json()
    assert [x["id"] for x in page1] == [ids[5], ids[4]]
    cursor = page1[-1]["created_at"]
    page2 = (await client.get("/api/runs", params={
        "workflow_id": wf_id, "limit": 2, "before": cursor})).json()
    assert [x["id"] for x in page2] == [ids[3], ids[2]]


async def test_limit_is_capped(client):
    assert (await client.get("/api/runs", params={"limit": 201})).status_code == 422
    assert (await client.get("/api/runs", params={"limit": 200})).status_code == 200


async def test_run_out_has_the_new_fields(client, seeded_runs):
    _, ids, _ = seeded_runs
    body = (await client.get(f"/api/runs/{ids[0]}")).json()
    for key in ("manifest_seq", "finished_at", "error_node_id"):
        assert key in body, key


async def test_graph_snapshot_endpoint(client):
    r = await client.post("/api/runs", json={"graph": GATED, "input": {"question": "q"}})
    run_id = r.json()["id"]
    got = await client.get(f"/api/runs/{run_id}/graph")
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"graph", "workflow_id", "version"}
    assert [n["id"] for n in body["graph"]["nodes"]] == ["start", "gate", "out"]
    assert (await client.get("/api/runs/nope/graph")).status_code == 404


# --------------------------------------------------------------------------
# 审批
# --------------------------------------------------------------------------


async def test_pending_approvals_carry_their_context_newest_first(client):
    wf = None
    async with SessionLocal() as session:
        wf = Workflow(name="审批测试·公告", graph=GATED)
        session.add(wf)
        await session.commit()
    first = (await client.post("/api/runs", json={
        "workflow_id": wf.id, "input": {"question": "一"}})).json()["id"]
    await _wait(first, ("interrupted",))
    second = (await client.post("/api/runs", json={
        "workflow_id": wf.id, "input": {"question": "二"}})).json()["id"]
    await _wait(second, ("interrupted",))

    items = (await client.get("/api/approvals", params={"status": "pending"})).json()
    mine = [a for a in items if a["run_id"] in (first, second)]
    assert [a["run_id"] for a in mine] == [second, first], "按发起时间倒序"
    a = mine[0]
    assert a["workflow_name"] == "审批测试·公告"
    assert a["node_label"] == "发布前把关"
    assert a["run_status"] == "interrupted"
    assert a["run_class"] == "exploratory"
    assert a["resolved_by"] is None and a["resolved_at"] is None


async def test_resolution_records_who_signed_and_when(client):
    run_id = (await client.post("/api/runs", json={
        "graph": GATED, "input": {"question": "q"}})).json()["id"]
    await _wait(run_id, ("interrupted",))
    [pending] = (await client.get("/api/approvals", params={"run_id": run_id})).json()

    # 浏览器的请求头只能是 Latin-1，中文署名按 encodeURIComponent 编码后发
    r = await client.post(f"/api/approvals/{pending['id']}/decide",
                          json={"approved": True}, headers={"X-Actor": quote("张工")})
    assert r.status_code == 200, r.text
    await _wait(run_id, ("succeeded",))

    [done] = (await client.get("/api/approvals",
                               params={"run_id": run_id, "status": "all"})).json()
    assert done["resolved_by"] == "张工"
    _aware(done["resolved_at"])

    events = (await client.get(f"/api/runs/{run_id}/events")).json()
    resolved = next(e for e in events if e["type"] == "human.resolved")
    assert resolved["data"].get("actor") == "张工"
    resumed = next(e for e in events if e["type"] == "run.resumed")
    assert resumed["data"].get("actor") == "张工"


async def test_unsigned_resolution_stays_null(client):
    run_id = (await client.post("/api/runs", json={
        "graph": GATED, "input": {"question": "q"}})).json()["id"]
    await _wait(run_id, ("interrupted",))
    [pending] = (await client.get("/api/approvals", params={"run_id": run_id})).json()
    await client.post(f"/api/approvals/{pending['id']}/decide", json={"approved": False})
    await _wait(run_id, ("succeeded", "failed"))
    [done] = (await client.get("/api/approvals",
                               params={"run_id": run_id, "status": "all"})).json()
    assert done["resolved_by"] is None


# --------------------------------------------------------------------------
# 删除保护
# --------------------------------------------------------------------------


async def _plain_run(**fields) -> str:
    async with SessionLocal() as session:
        run = Run(workflow_name="删除测试", graph={}, input={}, **fields)
        session.add(run)
        await session.commit()
        return run.id


@pytest.mark.parametrize("status", ["running", "queued"])
async def test_a_live_run_cannot_be_deleted(client, status):
    run_id = await _plain_run(status=status)
    r = await client.delete(f"/api/runs/{run_id}")
    assert r.status_code == 409
    assert "停止" in r.json()["detail"]
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 200


async def test_a_sealed_formal_run_needs_force(client):
    run_id = await _plain_run(status="succeeded", run_class="formal",
                              manifest_hash="ab" * 32, manifest_seq=12)
    r = await client.delete(f"/api/runs/{run_id}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "force" not in detail.lower() or "清单" in detail
    assert "清单" in detail and "无法" in detail, detail
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 200

    r = await client.delete(f"/api/runs/{run_id}", params={"force": "true"})
    assert r.status_code == 204
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 404


async def test_an_ordinary_finished_run_deletes_straight_away(client):
    run_id = await _plain_run(status="succeeded", run_class="exploratory", manifest_hash="cd" * 32)
    assert (await client.delete(f"/api/runs/{run_id}")).status_code == 204


# --------------------------------------------------------------------------
# 事件流收尾
# --------------------------------------------------------------------------


class _FakeSocket:
    """够 stream_run 用的最小 WebSocket：记下发出去的，客户端不主动说话。"""

    def __init__(self, after: int = 0) -> None:
        self.query_params = {"after": str(after)}
        self.sent: list[dict] = []
        self.closed = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def send_json(self, data) -> None:
        if self.closed.is_set():
            raise RuntimeError("closed")
        self.sent.append(data)

    async def receive_text(self) -> str:
        await self.closed.wait()
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000) -> None:
        self.closed.set()


async def _stream(run_id: str, timeout: float = 5.0) -> list[dict]:
    from app.api.runs import stream_run

    ws = _FakeSocket()
    await asyncio.wait_for(stream_run(ws, run_id), timeout)
    return ws.sent


async def test_connecting_to_an_interrupted_run_ends_with_a_marker(client):
    run_id = (await client.post("/api/runs", json={
        "graph": GATED, "input": {"question": "q"}})).json()["id"]
    await _wait(run_id, ("interrupted",))
    sent = await _stream(run_id)
    assert sent[-1]["type"] == "stream.end"
    assert sent[-1]["data"] == {"status": "interrupted"}
    assert any(m.get("type") == "run.interrupted" for m in sent[:-1])


async def test_connecting_to_a_finished_run_ends_with_a_marker(client):
    graph = chain(node("start", "input"), node("out", "output"))
    run_id = (await client.post("/api/runs", json={"graph": graph, "input": {}})).json()["id"]
    await _wait(run_id, ("succeeded",))
    sent = await _stream(run_id)
    assert sent[-1]["type"] == "stream.end"
    assert sent[-1]["data"] == {"status": "succeeded"}
    assert sent[-1]["status"] == "succeeded", "老客户端读的是顶层 status"


async def test_a_live_stream_ends_with_a_marker_when_the_run_finishes(client):
    run_id = (await client.post("/api/runs", json={
        "graph": GATED, "input": {"question": "q"}})).json()["id"]
    await _wait(run_id, ("interrupted",))
    [pending] = (await client.get("/api/approvals", params={"run_id": run_id})).json()
    await client.post(f"/api/approvals/{pending['id']}/decide", json={"approved": True})

    # 恢复之后连上去跟着它跑完：终态之后也要有结束标记，而不是连接悬着
    sent = await _stream(run_id)
    assert sent[-1]["type"] == "stream.end", [m.get("type") for m in sent[-3:]]
    assert sent[-1]["data"]["status"] == "succeeded"
    assert any(m.get("type") == "run.finished" for m in sent)


async def test_a_missing_run_ends_the_stream_instead_of_hanging():
    sent = await _stream("no-such-run")
    assert sent[-1]["type"] == "stream.end"


async def test_a_run_stranded_by_a_hard_kill_says_so_after_restart():
    """进程被强杀时连 server_shutdown 日志都来不及发。重启扫描把它标成 interrupted 的
    同时补一条，回放这条运行的客户端才知道它是停在了半路，而不是还在跑。"""
    from app.core.bus import bus

    async with SessionLocal() as session:
        run = Run(workflow_name="强杀", status="running", graph={}, input={}, last_seq=3)
        session.add(run)
        await session.commit()
        run_id = run.id
    await run_manager.shutdown()
    bus._seq.pop(run_id, None)                       # 新进程：内存里的序号是空的
    await run_manager.setup()

    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
    assert row.status == "interrupted"
    sent = await _stream(run_id)
    log = [m for m in sent if m.get("type") == "log"]
    assert [m["data"].get("code") for m in log] == ["server_shutdown"]
    assert log[0]["seq"] == 4, "序号要接着库里的 last_seq 往下排"
    assert sent[-1]["data"] == {"status": "interrupted"}


# --------------------------------------------------------------------------
# 接着跑：拒绝时说清楚为什么、下一步做什么
# --------------------------------------------------------------------------


async def test_continuing_a_cancelled_run_says_start_again(client):
    run_id = await _plain_run(status="cancelled")
    r = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "已取消" in detail and "重新发起" in detail, detail
    assert "审批" not in detail, detail


async def test_continuing_a_run_that_waits_for_approval_points_to_the_card(client):
    run_id = (await client.post("/api/runs", json={
        "graph": GATED, "input": {"question": "q"}})).json()["id"]
    await _wait(run_id, ("interrupted",))
    r = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409
    assert "审批卡" in r.json()["detail"]
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "interrupted"


async def test_a_run_suspended_by_a_restart_continues_from_its_checkpoint(client, monkeypatch):
    """服务重启挂起的运行没有在等谁，断点完好。「接着跑」就是从断点驱动，
    和失败后接着跑是同一件事——以前这里回 409，说它「在等待审批」。"""
    original = compiler.RUNNERS[NodeType.TRANSFORM]
    calls = {"slow": 0}

    async def slow_once(state, ctx):
        if ctx.node.id == "slow":
            calls["slow"] += 1
            if calls["slow"] == 1:
                await asyncio.sleep(30)
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, slow_once)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("a", "transform", mode="template", template="前面这步"),
        node("slow", "transform", mode="template", template="慢的这步"),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.slow }}"}]),
    )
    run_id = (await client.post("/api/runs", json={
        "graph": graph, "input": {"question": "q"}})).json()["id"]
    for _ in range(300):
        if calls["slow"]:
            break
        await asyncio.sleep(0.03)
    await run_manager.shutdown()
    await run_manager.setup()                          # 重启
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "interrupted"

    r = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 200, r.text
    row = await _wait(run_id, ("succeeded", "failed"))
    assert row.status == "succeeded", row.error
    assert row.output.get("结果") == "慢的这步"
    async with SessionLocal() as session:
        starts = [e.node_id for e in (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "node.started")
        )).scalars()]
    assert starts.count("a") == 1, f"接着跑把前面跑完的节点又跑了一遍：{starts}"


@pytest.mark.parametrize("method,action", [("post", "resume"), ("post", "continue"),
                                           ("get", "verify")])
async def test_a_missing_run_is_reported_without_python_quotes(client, method, action):
    kwargs = {"json": {}} if method == "post" else {}
    r = await getattr(client, method)(f"/api/runs/nope/{action}", **kwargs)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail and not detail.startswith("'"), detail
    assert "nope" in detail or "不存在" in detail, detail
