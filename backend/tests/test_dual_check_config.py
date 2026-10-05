"""双盲复算的配置、发起时的设置快照、助手的节点说明。

- 画布校验（engine/schema.py）：口径卡必须是上游的口径卡（没写时上游只能有一张），复算的指标、口径说明的键必须是
  那张卡上的指标 ID，开关、误差、处置的取值；报错写检查器上的中文叫法
- 发起（engine/launch.py）：图里有双盲复算或反方审稿节点时把核查设置快照进 options["verify"]，没有时 options 不变、
  run.started 和以前逐字相同；调用方给了的照它的
- 助手的节点说明（api/copilot.py 的 NODE_REFERENCE）有 dual_check 一条：什么时候加、各配置项

表名、数字都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


CARD = node("card", "metrics", "销售口径卡", caliber="销售口径", metrics=[
    {"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
    {"id": "orders", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'n')"}])


def spec(*middle: dict, dual: dict | None = None, edges: list | None = None) -> dict:
    nodes = [node("start", "input"), node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT 1"}), *middle,
             node("dual", "dual_check", "复核", **(dual or {})), node("out", "output", fields=[])]
    chain = edges or [(a["id"], b["id"]) for a, b in zip(nodes, nodes[1:])]
    return {"nodes": nodes, "edges": [{"source": s, "target": t} for s, t in chain]}


def problems(graph: dict) -> list[tuple[str | None, str]]:
    report = validate_graph(GraphSpec.model_validate(graph))
    return [(i.field, i.message) for i in report.issues if i.level == "error" and i.node_id == "dual"]


# --------------------------------------------------------------------------
# 画布校验
# --------------------------------------------------------------------------


def test_a_valid_config_has_no_problems():
    assert problems(spec(CARD, dual={"card": "card", "metrics": ["gmv"], "definitions": {"gmv": "按支付日期，不含退款"},
                                     "tolerance": 0.001, "on_reject": "fail", "enabled": True})) == []
    # 不写口径卡：上游只有一张，就是它
    assert problems(spec(CARD)) == []


def test_the_card_must_be_an_upstream_metrics_card():
    [(where, text)] = problems(spec(CARD, dual={"card": "fetch"}))
    assert where == "card" and "「口径卡」选择了「fetch」" in text and "不是口径卡" in text
    [(where, text)] = problems(spec(CARD, dual={"card": "ghost"}))
    assert where == "card" and "不存在这个节点" in text
    # 口径卡接在双盲复算后面：复算时它还没算
    nodes = [node("start", "input"), node("dual", "dual_check", "复核", card="card"), CARD]
    graph = {"nodes": nodes, "edges": [{"source": "start", "target": "dual"}, {"source": "dual", "target": "card"}]}
    [(where, text)] = problems(graph)
    assert where == "card" and "不在双盲复算的上游" in text


def test_without_a_card_there_must_be_exactly_one_upstream():
    [(where, text)] = problems(spec())
    assert where == "card" and "上游没有口径卡" in text
    second = node("card2", "metrics", "利润口径卡", metrics=[{"id": "profit", "expression": "1"}])
    [(where, text)] = problems(spec(CARD, second))
    assert where == "card" and "2 张口径卡" in text and "「销售口径卡」" in text and "「利润口径卡」" in text
    assert problems(spec(CARD, second, dual={"card": "card2"})) == []


def test_metrics_and_definitions_must_be_on_the_card():
    found = dict(problems(spec(CARD, dual={"metrics": ["gmv", "refund"],
                                           "definitions": {"gmv": "按支付日", "cost": "成本"}})))
    assert "「复算的指标」中的「refund」不是口径卡「销售口径卡」上的指标 ID" in found["metrics"]
    assert "可选的指标 ID：gmv、orders" in found["metrics"]
    assert "「口径说明」中的「cost」不是口径卡「销售口径卡」上的指标 ID" in found["definitions"]
    [(where, text)] = problems(spec(CARD, dual={"definitions": ["gmv"]}))
    assert where == "definitions" and "指标 ID → 口径说明" in text


def test_a_card_pinned_elsewhere_is_not_checked_for_metric_ids():
    pinned = node("card", "metrics", "引用的口径卡", caliber_from={"workflow_id": "w", "workflow_version": 1, "node_id": "c"})
    assert problems(spec(pinned, dual={"definitions": {"anything": "说明"}})) == []


def test_option_values_are_checked():
    found = dict(problems(spec(CARD, dual={"enabled": "yes", "on_reject": "ignore", "tolerance": -1})))
    assert "「启用双盲复算」" in found["enabled"]
    assert "「未采用原流程时」只能选「计入缺口、出具降档」或「判为失败」" in found["on_reject"]
    assert "「允许的相对误差」需要填写不小于 0 的数" in found["tolerance"]


# --------------------------------------------------------------------------
# 发起：设置快照
# --------------------------------------------------------------------------


@pytest.fixture
async def engine():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == "verify"))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == "verify"))
        await session.commit()
    await run_manager.shutdown()


async def settle(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.03)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def started_of(run_id: str) -> dict:
    async with SessionLocal() as session:
        return (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "run.started"))).scalars().first().data


PLAIN = {"nodes": [node("start", "input"), node("out", "output", fields=[{"name": "r", "value": "完成"}])],
         "edges": [{"source": "start", "target": "out"}]}
GATED = {"nodes": [node("start", "input"), node("card", "metrics", metrics=[{"id": "x", "expression": "1"}]),
                   node("dual", "dual_check"), node("out", "output", fields=[])],
         "edges": [{"source": "start", "target": "card"}, {"source": "card", "target": "dual"},
                   {"source": "dual", "target": "out"}]}


async def test_a_graph_with_a_verify_node_snapshots_the_settings(engine):
    from app.api.settings import verify_settings
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        session.add(Setting(key="verify", value={"dual_check": {"tolerance": 0.01}, "daily_max_usd": 3}))
        await session.commit()
        expected = await verify_settings(session)
        run = await launch_run(session, graph=GATED, actor="测试")
    assert expected["dual_check"]["tolerance"] == 0.01 and run.options == {"verify": expected}
    row = await settle(run.id)
    assert row.status == "succeeded", row.error
    assert (await started_of(run.id))["options"] == {"verify": expected}


async def test_a_challenge_node_also_snapshots_the_settings(engine):
    from app.engine.launch import _with_verify_snapshot

    graph = {"nodes": [node("start", "input"), node("review", "challenge")], "edges": []}
    async with SessionLocal() as session:
        out = await _with_verify_snapshot(session, graph, None)
    assert set(out) == {"verify"} and out["verify"]["challenge"]["enabled"] is True


async def test_without_verify_nodes_options_and_run_started_are_unchanged(engine):
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, graph=PLAIN, actor="测试")
    assert run.options == {}
    await settle(run.id)
    assert "options" not in await started_of(run.id)


async def test_options_given_by_the_caller_are_kept(engine):
    from app.engine.launch import launch_run

    mine = {"verify": {"dual_check": {"enabled": False}}, "note": "例行"}
    async with SessionLocal() as session:
        run = await launch_run(session, graph=GATED, actor="测试", options=mine)
    assert run.options == mine
    await settle(run.id)


# --------------------------------------------------------------------------
# 助手的节点说明
# --------------------------------------------------------------------------


def test_the_copilot_reference_explains_when_and_how_to_add_it():
    from app.api.copilot import NODE_REFERENCE

    assert "- dual_check：" in NODE_REFERENCE
    block = NODE_REFERENCE.split("- dual_check：", 1)[1].split("\n- ", 1)[0]
    for key in ("card", "metrics", "definitions", "provider", "model", "tolerance", "on_reject", "enabled"):
        assert key in block, key
    assert "正式出具" in block and "口径卡之后" in block and "degrade" in block and "fail" in block
    # 排在口径卡和报告之间：搭图的先后就是这样
    at = NODE_REFERENCE.index
    assert at("- metrics：") < at("- dual_check：") < at("- report：")
