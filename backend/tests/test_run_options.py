"""运行选项和来源：Run.options 是发起时定下、整次运行沿用的选项快照，Run.schedule_id 是发起它的定时任务。

- 旧库升级时补这两列和 schedule_id 的索引；存量运行的选项读出来是 {}，来源为空
- RunManager.start 收下两者并落库；选项非空时写进 run.started（进封存范围），为空时 run.started 和以前
  逐字相同；恢复、继续运行沿用发起时的选项，节点从 RunContext.extra 读得到同一份
- 接口带出 schedule_id，运行列表能按定时任务筛；launch_run 原样往下交
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text

from app.db.base import SessionLocal, engine as app_engine
from app.db.models import Run, RunEvent
from app.engine import compiler
from app.engine.runner import run_manager
from app.engine.schema import NodeType
from app.main import app

#: 加 options 之前 run.started 就有的键：选项为空时一个不多、一个不少
LEGACY_STARTED_KEYS = {"nodes", "resumed", "memory_scope", "collection", "approval_default", "tool_trust",
                       "agent_limits", "data_versions", "replay_protocol"}
OPTIONS = {"verify": {"dual_check": True, "max_queries": 6}, "note": "第 40 周例行核查"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


SIMPLE = chain(node("start", "input"), node("out", "output", fields=[{"name": "r", "value": "完成"}]))
GATED = chain(node("start", "input"),
              node("before", "transform", mode="template", template="前"),
              node("gate", "human", mode="approve", title="放行吗"),
              node("after", "transform", mode="template", template="后"),
              node("out", "output", fields=[{"name": "r", "value": "{{ nodes.after }}"}]))


async def _settle(run_id: str, statuses=("succeeded", "failed", "cancelled")) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def _started(run_id: str) -> list[dict]:
    async with SessionLocal() as session:
        return [e.data for e in (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "run.started").order_by(RunEvent.seq)
        )).scalars()]


def _schedule() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# 旧库升级
# --------------------------------------------------------------------------


def test_old_databases_get_both_columns_and_the_index():
    from app.db.base import _migrate

    db = create_engine("sqlite://")
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, workflow_name VARCHAR(200), "
                          "status VARCHAR(20), last_seq INTEGER)"))
        conn.execute(text("INSERT INTO runs (id, workflow_name, status, last_seq) VALUES ('r1', '周报', 'succeeded', 9)"))
        _migrate(conn)
        _migrate(conn)  # 再执行一次不出错：列和索引都已经在了
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(runs)"))}
        assert {"options", "schedule_id"} <= cols
        assert conn.execute(text("SELECT options, schedule_id FROM runs WHERE id = 'r1'")).one() == ("{}", None)
        indexes = {row[1]: row for row in conn.execute(text("PRAGMA index_list(runs)"))}
        assert "ix_runs_schedule_id" in indexes
        assert [r[2] for r in conn.execute(text("PRAGMA index_info(ix_runs_schedule_id)"))] == ["schedule_id"]


def test_a_missing_table_is_left_alone():
    """还没有 runs 表的库（create_all 之前不会这样，这里只确认迁移不替它建索引、不报错）。"""
    from app.db.base import _migrate

    db = create_engine("sqlite://")
    with db.begin() as conn:
        _migrate(conn)
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name = 'ix_runs_schedule_id'")).first() is None


async def test_new_databases_index_schedule_id():
    from sqlalchemy import inspect

    async with app_engine.connect() as conn:
        indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("runs"))
    assert {ix["name"]: ix["column_names"] for ix in indexes}.get("ix_runs_schedule_id") == ["schedule_id"]


async def test_a_migrated_row_reads_back_empty_options():
    """迁移回填的是字符串 '{}'：经 ORM 读出来是空 dict，不是字符串，也不是 None。"""
    async with SessionLocal() as session:
        run = Run(workflow_name="迁移回填", status="succeeded", graph={}, input={})
        session.add(run)
        await session.commit()
        run_id = run.id
    async with app_engine.begin() as conn:
        await conn.execute(text("UPDATE runs SET options = '{}' WHERE id = :id"), {"id": run_id})
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
        assert row.options == {} and row.schedule_id is None


# --------------------------------------------------------------------------
# 发起：落库、run.started、沿用
# --------------------------------------------------------------------------


async def test_options_and_schedule_are_stored_and_sealed():
    sid = _schedule()
    run = await run_manager.start(graph=SIMPLE, input_payload={}, options=OPTIONS, schedule_id=sid)
    assert (run.options, run.schedule_id) == (OPTIONS, sid)
    row = await _settle(run.id)
    assert row.status == "succeeded", row.error
    assert (row.options, row.schedule_id) == (OPTIONS, sid)
    [started] = await _started(run.id)
    assert started["options"] == OPTIONS
    assert set(started) == LEGACY_STARTED_KEYS | {"options"}


@pytest.mark.parametrize("options", [None, {}])
async def test_without_options_run_started_is_unchanged(options):
    run = await run_manager.start(graph=SIMPLE, input_payload={}, options=options)
    row = await _settle(run.id)
    assert row.options == {} and row.schedule_id is None
    [started] = await _started(run.id)
    assert set(started) == LEGACY_STARTED_KEYS, "选项为空时 run.started 多出了键"


async def test_the_options_belong_to_the_run_not_to_the_callers_dict():
    mine = {"verify": {"dual_check": True}}
    run = await run_manager.start(graph=SIMPLE, input_payload={}, options=mine)
    mine["verify"]["dual_check"] = False
    mine["late"] = 1
    await _settle(run.id)
    async with SessionLocal() as session:
        assert (await session.get(Run, run.id)).options == {"verify": {"dual_check": True}}


@pytest.fixture
def seen_options(monkeypatch):
    """记下每次执行「数据整形」节点时它从运行上下文里读到的选项。"""
    from app.engine.runner import RUN_OPTIONS_KEY

    seen: list = []
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def spy(state, ctx):
        seen.append(ctx.run.extra.get(RUN_OPTIONS_KEY))
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, spy)
    return seen


async def test_resume_keeps_the_options_the_run_started_with(seen_options):
    run = await run_manager.start(graph=GATED, input_payload={}, options=OPTIONS)
    assert (await _settle(run.id, ("interrupted",))).status == "interrupted"
    await run_manager.resume(run.id, {"approved": True})
    row = await _settle(run.id)
    assert row.status == "succeeded", row.error
    assert seen_options == [OPTIONS, OPTIONS], "恢复之后节点读到的选项变了"
    first, second = await _started(run.id)
    assert first["options"] == second["options"] == OPTIONS and second["resumed"] is True


async def test_continue_keeps_the_options_too(seen_options, monkeypatch):
    original = compiler.RUNNERS[NodeType.TRANSFORM]
    calls = {"n": 0}

    async def flaky(state, ctx):
        calls["n"] += 1
        if ctx.node.id == "after" and calls["n"] <= 2:
            raise RuntimeError("临时故障")
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, flaky)
    graph = chain(node("start", "input"),
                  node("before", "transform", mode="template", template="前"),
                  node("after", "transform", mode="template", template="后"),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.after }}"}]))
    run = await run_manager.start(graph=graph, input_payload={}, options=OPTIONS)
    assert (await _settle(run.id)).status == "failed"
    await run_manager.continue_failed(run.id)
    row = await _settle(run.id)
    assert row.status == "succeeded", row.error
    assert [s.get("options") for s in await _started(run.id)] == [OPTIONS, OPTIONS]


async def test_nodes_read_an_empty_dict_when_there_are_no_options(seen_options):
    run = await run_manager.start(graph=GATED, input_payload={})
    await _settle(run.id, ("interrupted",))
    assert seen_options == [{}]


# --------------------------------------------------------------------------
# 接口：带出来源、按定时任务筛、launch_run 照转
# --------------------------------------------------------------------------


async def test_the_api_shows_and_filters_by_schedule(client):
    sid, other = _schedule(), _schedule()
    mine = [await run_manager.start(graph=SIMPLE, input_payload={}, schedule_id=sid) for _ in range(2)]
    theirs = await run_manager.start(graph=SIMPLE, input_payload={}, schedule_id=other)
    manual = await run_manager.start(graph=SIMPLE, input_payload={})
    for run in (*mine, theirs, manual):
        await _settle(run.id)

    got = (await client.get(f"/api/runs/{mine[0].id}")).json()
    assert got["schedule_id"] == sid
    assert (await client.get(f"/api/runs/{manual.id}")).json()["schedule_id"] is None

    listed = (await client.get("/api/runs", params={"schedule_id": sid})).json()
    assert sorted(r["id"] for r in listed) == sorted(r.id for r in mine)
    assert all(r["schedule_id"] == sid for r in listed)
    # 不带这个参数时照旧全列
    everything = {r["id"] for r in (await client.get("/api/runs", params={"limit": 200})).json()}
    assert {theirs.id, manual.id} <= everything


async def test_launch_run_passes_both_through():
    from app.engine.launch import launch_run

    sid = _schedule()
    async with SessionLocal() as session:
        run = await launch_run(session, graph=SIMPLE, actor="定时任务", schedule_id=sid, options=OPTIONS)
    row = await _settle(run.id)
    assert (row.schedule_id, row.options, row.started_by) == (sid, OPTIONS, "定时任务")
    [started] = await _started(run.id)
    assert started["options"] == OPTIONS


async def test_an_http_launch_has_no_schedule_and_no_options(client):
    res = await client.post("/api/runs", json={"graph": SIMPLE})
    assert res.status_code == 201, res.text
    assert res.json()["schedule_id"] is None
    row = await _settle(res.json()["id"])
    assert row.options == {}
