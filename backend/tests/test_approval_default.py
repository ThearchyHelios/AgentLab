"""「危险工具默认需要人工确认」是工具审批策略的全局默认，而不是一个摆设。

以前审批只看节点上的 approval（缺省 dangerous），设置页的开关勾不勾都一样——
取消勾选以为关掉了审批，勾上以为全局开了，两种理解都不对。

规则：节点自己配了就用节点的；没配时取全局设置（勾上 = dangerous，取消 = never）。
正式运行不吃"取消"：它跑的是封存的发布版，受管门禁要求危险工具至少人工确认，
一个全局开关不能把它悄悄降掉。

另外守一件连带的事：记忆域和知识库以前只在第一次 _drive 时传进去，审批恢复、
接着跑时回落成 "default"——同一次运行前后两段查的不是同一个库。
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Setting, Workflow
from app.engine import compiler
from app.engine.runner import run_manager
from app.engine.schema import NodeType


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def confirm_setting():
    """把设置里的 run 组改成给定值；测试结束删掉，别影响别的测试。"""
    async def put(value: bool) -> None:
        async with SessionLocal() as session:
            row = await session.get(Setting, "run")
            if row:
                row.value = {**row.value, "confirm_dangerous_tools": value}
            else:
                session.add(Setting(key="run", value={"confirm_dangerous_tools": value}))
            await session.commit()

    yield put
    async with SessionLocal() as session:
        row = await session.get(Setting, "run")
        if row:
            await session.delete(row)
            await session.commit()


@pytest.fixture
async def writable(tmp_path):
    path = tmp_path / "w.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (x INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    c.commit()
    c.close()
    name = f"w{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database=str(path), readonly=False,
                               description="可写测试库", options={}, schema_cache={}, enabled=True))
        await session.commit()

    def count() -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            conn.close()

    return f"db_query__{name}", count


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def _delete_graph(tool: str, **approval):
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("q", "tool", tool=tool, args={"sql": "DELETE FROM t WHERE x = 0"}, **approval),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.q }}"}]),
    )


async def _settle(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row.status in ("interrupted", "succeeded", "failed", "cancelled"):
                return row
        await asyncio.sleep(0.03)
    raise AssertionError("等超时了")


async def test_unchecked_setting_lets_unconfigured_nodes_run(writable, confirm_setting):
    tool, count = writable
    await confirm_setting(False)
    run = await run_manager.start(graph=_delete_graph(tool), input_payload={"question": "q"})
    row = await _settle(run.id)
    assert row.status == "succeeded", f"全局关了审批，节点没配，却还是停下来了：{row.status}"
    assert count() == 4


async def test_checked_setting_keeps_asking(writable, confirm_setting):
    tool, count = writable
    await confirm_setting(True)
    run = await run_manager.start(graph=_delete_graph(tool), input_payload={"question": "q"})
    row = await _settle(run.id)
    assert row.status == "interrupted"
    assert count() == 5


async def test_what_the_node_says_wins_over_the_setting(writable, confirm_setting):
    tool, count = writable
    await confirm_setting(False)
    run = await run_manager.start(
        graph=_delete_graph(tool, approval="dangerous"), input_payload={"question": "q"})
    row = await _settle(run.id)
    assert row.status == "interrupted", "节点明说了要审批，全局开关不该盖过它"
    assert count() == 5


async def test_governed_formal_runs_do_not_take_the_unchecked_setting(writable, confirm_setting):
    tool, count = writable
    await confirm_setting(False)
    graph = _delete_graph(tool)
    async with SessionLocal() as session:
        wf = Workflow(name=f"受管-{uuid.uuid4().hex[:4]}", graph=graph, status="governed",
                      published_version=1)
        session.add(wf)
        await session.commit()
    run = await run_manager.start(graph=graph, input_payload={"question": "q"},
                                  workflow_id=wf.id, run_class="formal", version=1)
    row = await _settle(run.id)
    assert row.status == "interrupted", "受管门禁要独立生效"
    assert count() == 5


def _governable(tool: str):
    """过得了受管门禁的最小图：危险工具节点不配 approval，后面接口径卡和出具契约。"""
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("q", "tool", tool=tool, args={"sql": "DELETE FROM t WHERE x = 0"}),
        node("caliber", "metrics", caliber="删除口径", caliber_version="v1", assign_to="m",
             metrics=[{"id": "done", "name": "已执行", "unit": "次", "expression": "1"}]),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.q }}"}],
             contract={"metrics_from": ["caliber"], "narrative": "执行了 {{ vars.m.done }} 次",
                       "required": ["done"], "strict": True}),
    )


@pytest.mark.parametrize("level", ["governed", "published"])
async def test_formal_runs_keep_asking_after_the_canvas_is_edited(
    writable, confirm_setting, level
):
    """发布之后在画布上再存一次，status 就退回 draft，published_version 仍指着发布的那一版。

    以前下限看的是 status == "governed"：受管模板只要被编辑过，它的正式运行就跟着
    全局开关降到「全部自动放行」，DELETE 不经人手直接执行。现在正式运行一律不吃
    "取消"——跑的是封存的发布版，它的审批行为不该取决于一个随时能改的偏好。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    tool, count = writable
    await confirm_setting(False)
    graph = _governable(tool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        wf = (await c.post("/api/workflows", json={"name": f"受管-{uuid.uuid4().hex[:4]}",
                                                   "graph": graph})).json()
        published = (await c.post(f"/api/workflows/{wf['id']}/publish",
                                   json={"level": level})).json()
        assert published["ok"], published
        edited = {**graph, "nodes": [*graph["nodes"][:-1],
                                     {**graph["nodes"][-1], "data": {
                                         **graph["nodes"][-1]["data"], "label": "改过的出口"}}]}
        after = (await c.patch(f"/api/workflows/{wf['id']}", json={"graph": edited})).json()
        assert after["status"] == "draft" and after["published_version"] == 1
        r = await c.post("/api/runs", json={"workflow_id": wf["id"], "run_class": "formal",
                                            "input": {"question": "q"}})
        assert r.status_code == 201, r.text
    row = await _settle(r.json()["id"])
    assert row.status == "interrupted", f"编辑过画布后，正式运行跳过了审批：{row.status}"
    assert row.approval_default == "dangerous"
    assert count() == 5


async def test_exploratory_runs_of_a_governed_workflow_follow_the_setting(
    writable, confirm_setting
):
    """下限只管正式运行。探索运行照旧听全局开关，不然这个开关又成了摆设。"""
    tool, count = writable
    await confirm_setting(False)
    graph = _delete_graph(tool)
    async with SessionLocal() as session:
        wf = Workflow(name=f"受管-{uuid.uuid4().hex[:4]}", graph=graph, status="governed",
                      published_version=1)
        session.add(wf)
        await session.commit()
    run = await run_manager.start(graph=graph, input_payload={"question": "q"}, workflow_id=wf.id)
    row = await _settle(run.id)
    assert row.status == "succeeded", row.status
    assert count() == 4


async def test_effective_values_are_sealed_into_the_event_stream(confirm_setting):
    await confirm_setting(False)
    graph = chain(node("start", "input"), node("out", "output"))
    run = await run_manager.start(graph=graph, input_payload={}, memory_scope="ops",
                                  collection="manuals")
    await _settle(run.id)
    async with SessionLocal() as session:
        started = (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.started")
        )).scalars().first()
    assert started.data["memory_scope"] == "ops"
    assert started.data["collection"] == "manuals"
    assert started.data["approval_default"] == "never"


async def test_resume_keeps_the_scope_the_run_started_with(monkeypatch):
    seen: list[tuple[str, str]] = []
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def spy(state, ctx):
        seen.append((ctx.run.memory_scope, ctx.run.collection))
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, spy)
    graph = chain(
        node("start", "input"),
        node("before", "transform", mode="template", template="前"),
        node("gate", "human", mode="approve", title="看一眼"),
        node("after", "transform", mode="template", template="后"),
        node("out", "output"),
    )
    run = await run_manager.start(graph=graph, input_payload={}, memory_scope="ops",
                                  collection="manuals")
    assert (await _settle(run.id)).status == "interrupted"
    await run_manager.resume(run.id, {"approved": True})
    for _ in range(200):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.03)
    assert seen == [("ops", "manuals"), ("ops", "manuals")], "恢复之后换了个库在查"
