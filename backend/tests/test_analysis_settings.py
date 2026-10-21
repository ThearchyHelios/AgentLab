"""分析设置（设置 → 偏好设置 → 分析，docs/design/analysis-round/01-算子与判定.md §10）和发起时的快照。

- 设置接口的 analysis 一组：键、缺省、范围、叫法全取 app/analysis/contract.py；GET 按生效的样子给（存坏了的项按缺省），
  PUT 先查后写（不认识的键、类型、范围、两两之间的大小）
- 发起运行时快照进 run.options["analysis"]：只在图（含子工作流）里有分析卡、或 Agent / 协作成员绑了分析工具时写；
  别的图的 run.started 一字不变
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.analysis import contract
from app.api.settings import ANALYSIS_GROUP, analysis_problem, analysis_values
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting, Workflow
from app.engine.launch import launch_run
from app.engine.runner import run_manager
from app.main import app

#: 加 options 之前 run.started 就有的键（同 tests/test_run_options.py）
LEGACY_STARTED_KEYS = {"nodes", "resumed", "memory_scope", "collection", "approval_default", "tool_trust",
                       "agent_limits", "data_versions", "replay_protocol"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def clean_group():
    yield
    async with SessionLocal() as session:
        row = await session.get(Setting, ANALYSIS_GROUP)
        if row is not None:
            await session.delete(row)
            await session.commit()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


# --------------------------------------------------------------------------
# 设置接口
# --------------------------------------------------------------------------


def test_the_group_is_the_contract():
    assert ANALYSIS_GROUP == contract.SETTINGS_KEY
    assert analysis_values(None) == contract.DEFAULTS
    # 存坏了的项按缺省；两两之间的大小对不上的那一对都按缺省
    broken = {"main_share": "一半", "max_calls": 3.5, "k_warn": 4, "k_alert": 3.5, "min_history": 20, "max_history": 10,
              "table_top_k": 5, "trend_alpha": True}
    assert analysis_values(broken) == {**contract.DEFAULTS, "table_top_k": 5}
    # 浮点的项存成浮点：发起时的快照、结论里记的阈值一个写法
    assert analysis_values({"k_warn": 3, "k_alert": 4})["k_warn"] == 3.0
    assert isinstance(analysis_values({"k_warn": 3, "k_alert": 4})["k_alert"], float)


@pytest.mark.parametrize("value, why", [
    ("宽松", "需要是一个对象"),
    ({"bogus": 1}, "不支持「bogus」"),
    ({"main_share": 0.95}, f"{contract.LABELS['main_share']}要在 0.3 到 0.9 之间"),
    ({"max_calls": 2.5}, f"{contract.LABELS['max_calls']}要填整数"),
    ({"k_warn": 3.5}, f"{contract.LABELS['k_alert']}要大于{contract.LABELS['k_warn']}"),
    ({"min_history": 60}, f"{contract.LABELS['max_history']}不能小于{contract.LABELS['min_history']}"),
])
def test_problems_are_said_with_labels(value, why):
    assert why in (analysis_problem(value) or "")


async def test_get_put_round_trip(client):
    got = (await client.get("/api/settings")).json()
    assert got[ANALYSIS_GROUP] == contract.DEFAULTS
    put = await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"main_share": 0.6, "max_calls": 5}}})
    assert put.status_code == 200, put.text
    got = (await client.get("/api/settings")).json()[ANALYSIS_GROUP]
    assert got == {**contract.DEFAULTS, "main_share": 0.6, "max_calls": 5}
    # 整组替换：这次没写的项回到缺省
    await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"k_warn": 2.5}}})
    assert (await client.get("/api/settings")).json()[ANALYSIS_GROUP] == {**contract.DEFAULTS, "k_warn": 2.5}


async def test_put_refuses_bad_values_and_stores_nothing(client):
    r = await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"main_share": 0.6, "k_alert": 1.5}}})
    assert r.status_code == 422 and contract.LABELS["k_alert"] in r.json()["detail"]
    assert (await client.get("/api/settings")).json()[ANALYSIS_GROUP] == contract.DEFAULTS


# --------------------------------------------------------------------------
# 发起时的快照
# --------------------------------------------------------------------------


async def _started(run_id: str) -> dict:
    for _ in range(200):
        async with SessionLocal() as session:
            found = (await session.execute(select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.type == "run.started"))).scalars().first()
        if found is not None:
            await run_manager.wait_idle(run_id)
            return found.data
        await asyncio.sleep(0.05)
    raise AssertionError("没有 run.started")


async def _launch(graph, **kw) -> tuple[Run, dict]:
    async with SessionLocal() as session:
        run = await launch_run(session, graph=graph, actor="测试", **kw)
    return run, await _started(run.id)


CARD = node("card", "analysis", items=[{"id": "wow", "op": "baseline", "from": "q",
                                        "params": {"period": "week", "measure": "gmv"}}])
QUERY = node("q", "tool", tool="calculator", args={"expression": "1 + 1"})
OUT = node("out", "output", fields=[{"name": "r", "value": "完成"}])


async def test_graphs_without_analysis_start_exactly_as_before():
    run, started = await _launch(chain(node("start", "input"), node("t", "transform", mode="template", template="x"),
                                       OUT))
    assert set(started) == LEGACY_STARTED_KEYS, "没有分析的图，run.started 一个键都不多"
    async with SessionLocal() as session:
        assert (await session.get(Run, run.id)).options in (None, {})
    # 绑了别的工具的 Agent：有能力治理的快照（以前就有），没有分析这一组
    _, started = await _launch(chain(node("start", "input"),
                                     node("bot", "agent", prompt="说一句", tools=["calculator"]), OUT))
    assert ANALYSIS_GROUP not in (started.get("options") or {})


async def test_an_analysis_card_snapshots_the_settings():
    async with SessionLocal() as session:
        session.add(Setting(key=ANALYSIS_GROUP, value={"main_share": 0.6}))
        await session.commit()
    run, started = await _launch(chain(node("start", "input"), QUERY, CARD, OUT))
    assert started["options"][ANALYSIS_GROUP] == {**contract.DEFAULTS, "main_share": 0.6}
    async with SessionLocal() as session:
        assert (await session.get(Run, run.id)).options[ANALYSIS_GROUP]["main_share"] == 0.6


@pytest.mark.parametrize("graph", [
    chain(node("start", "input"), node("bot", "agent", prompt="为什么", tools=[contract.TOOL_NAMES["trend"]]), OUT),
    chain(node("start", "input"), node("team", "supervisor", goal="为什么", agents=[
        {"name": "分析员", "tools": [contract.TOOL_NAMES["differ"]]}]), OUT),
])
async def test_agents_and_members_with_analysis_tools_snapshot_the_settings(graph):
    _, started = await _launch(graph)
    assert started["options"][ANALYSIS_GROUP] == contract.DEFAULTS


async def test_a_card_inside_a_subgraph_counts_and_given_options_win():
    async with SessionLocal() as session:
        inner = Workflow(name="异动分析（子流程）", graph=chain(node("start", "input"), QUERY, CARD, OUT))
        session.add(inner)
        await session.commit()
        inner_id = inner.id
    outer = chain(node("start", "input"), node("sub", "subgraph", workflow_id=inner_id), OUT)
    _, started = await _launch(outer)
    assert started["options"][ANALYSIS_GROUP] == contract.DEFAULTS
    # 调用方已经给了这一组的，照它的
    _, started = await _launch(outer, options={ANALYSIS_GROUP: {"max_calls": 2}})
    assert started["options"][ANALYSIS_GROUP] == {"max_calls": 2}
