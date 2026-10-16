"""口径卡钉住别处口径卡时的节点改名映射（caliber_from.nodes，next-round 02 做法 4 ③）。

那一版的口径卡读 cell(nodes.fetch, 0, 'gmv')；拷进这张图时取数节点改名成 c1_fetch（组合式问数、对账、召回同一张图里放
两份口径会撞 id），caliber_from 带 nodes={fetch: c1_fetch}。要一起认这个映射的几处：

- 运行时（metrics._pinned_caliber）：式子按语法树把 nodes.fetch 的根换成 c1_fetch 再算，指标集的 source 照记映射；
- 画布校验（schema._caliber_from_issues）：映射的形状、改名后的节点在不在图里、在不在口径卡上游；
- 升版检查（governance.unresolved_caliber_upgrades）：照常挡、照常记事件，事件带上映射；
- 证据（指标步骤的 source、输入链到改名后的那条查询）、推演（按改名后的式子原样重算）；
- 运行对比（compare）：同一份口径换了节点名不算口径变了。

没写 nodes 的老图行为一字不差：式子原文、指标集、升版事件都和以前一样（单独测）。数据是虚构店铺库。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import RunEvent
from app.engine.nodes.metrics import rename_expression, rename_problem, source_expression
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.main import app
from tests import ledger_samples as S

RENDERED = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
            "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    from app.ledger import match as ledger_match

    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def composed(wf: str, *, rename: dict | None = None, fetch_id: str = "c1_fetch", card_id: str = "c1",
             version: int = 1, **card) -> dict:
    ref = {"workflow_id": wf, "workflow_version": version, "node_id": "card"}
    if rename is not None:
        ref["nodes"] = rename
    return S.chain(S.node("start", "input"), S.node(fetch_id, "tool", tool=S.tool(), args={"sql": RENDERED}),
                   S.node(card_id, "metrics", caliber_from=ref, **card),
                   S.node("out", "output", fields=[{"name": "r", "value": f"{{{{ nodes.{card_id}.text }}}}"}]))


async def finished(run_id: str, node_id: str) -> RunEvent:
    async with SessionLocal() as session:
        return (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "node.finished", RunEvent.node_id == node_id))).scalars().one()


async def run(graph: dict):
    return await S.wait((await run_manager.start(graph=graph, input_payload={})).id)


# --------------------------------------------------------------------------
# 改写式子
# --------------------------------------------------------------------------


def test_renaming_rewrites_only_the_roots_of_node_chains():
    rename = {"fetch": "c1_fetch"}
    assert rename_expression("cell(nodes.fetch, 0, 'gmv')", rename) == "cell(nodes.c1_fetch, 0, 'gmv')"
    assert rename_expression("nodes.fetch.rows[0]['gmv'] / 2", rename) == "nodes.c1_fetch.rows[0]['gmv'] / 2"
    assert rename_expression("nodes['fetch'].row_count", rename) == "nodes['c1_fetch'].row_count"
    # 字符串里的字、别的根、别的节点不动
    assert rename_expression("cell(nodes.other, 0, 'nodes.fetch') + vars.fetch", rename) == \
        "cell(nodes.other, 0, 'nodes.fetch') + vars.fetch"
    # 一处都没换到的原样交回（不按 unparse 改写空白）
    assert rename_expression("cell(nodes.other,0,'gmv')", rename) == "cell(nodes.other,0,'gmv')"
    assert rename_expression("cell(nodes.fetch,", rename) == "cell(nodes.fetch,"
    # 反着换回那一版里的写法：对比口径按它比
    assert source_expression("cell(nodes.c1_fetch, 0, 'gmv')", {"nodes": rename}) == "cell(nodes.fetch, 0, 'gmv')"
    assert source_expression("cell(nodes.c1_fetch, 0, 'gmv')", {"workflow_id": "w"}) == "cell(nodes.c1_fetch, 0, 'gmv')"


def test_mapping_shape_problems_are_named():
    assert rename_problem({"fetch": "c1_fetch"}) is None
    assert rename_problem([]) and rename_problem({})
    assert "节点 id" in rename_problem({"fetch": "c1 fetch"})
    assert "两个节点" in rename_problem({"a": "x", "b": "x"})


# --------------------------------------------------------------------------
# 运行时、证据、推演
# --------------------------------------------------------------------------


async def test_a_renamed_fetch_feeds_the_pinned_card(client):
    wf = await S.publish()
    row = await run(composed(wf, rename={"fetch": "c1_fetch"}))
    assert row.status == "succeeded", row.error
    event = await finished(row.id, "c1")
    card = artifact_store.load(event.data["artifact"])
    assert (card["caliber"], card["caliber_version"]) == ("周销售", "v1")
    [metric] = card["metrics"]
    assert metric["value"] == 170.5 and metric["rendered"] == "170.50元"
    # 卡上记的是这张图里实际算的式子；来源照记映射
    assert metric["expression"] == "cell(nodes.c1_fetch, 0, 'gmv')"
    assert card["source"] == {"workflow_id": wf, "workflow_version": 1, "node_id": "card",
                              "nodes": {"fetch": "c1_fetch"}}
    [entry] = event.data["evidence"]
    assert entry["source"]["nodes"] == {"fetch": "c1_fetch"}
    [item] = metric["inputs"]
    assert item["via"] == "tool_cell" and item["node_id"] == "c1_fetch" and item["artifact"]

    # 证据：指标步骤的来源带映射，输入链到改名后的那条查询的那一格
    resp = await client.get(f"/api/runs/{row.id}/evidence/metrics/c1/gmv")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [step] = [s for s in body["chain"] if s["step"] == "metric"]
    assert step["source"]["nodes"] == {"fetch": "c1_fetch"} and step["source"]["workflow_name"] == "周销售"
    [inp] = [s for s in body["chain"] if s["step"] == "input"]
    assert inp["node_id"] == "c1_fetch" and inp["artifact"] == item["artifact"]

    # 推演：按改名后的式子原样重算，改一个输入照常算
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "c1", "overrides": {"inputs": {"cell(nodes.c1_fetch, 0, 'gmv')": 200}}})
    assert resp.status_code == 200, resp.text
    [m] = resp.json()["metrics"]
    assert m["old"] == 170.5 and m["new"] == 200 and m["rendered_new"] == "200.00元"


async def test_without_the_mapping_the_renamed_fetch_is_not_found():
    """没写映射：式子照那一版读 nodes.fetch，这张图里没有它——缺输入，照实失败，不会算错。"""
    wf = await S.publish()
    row = await run(composed(wf))
    assert row.status == "failed" and "口径卡计算失败" in (row.error or "")


async def test_old_graphs_without_the_mapping_are_unchanged():
    """没写 nodes：式子原文一个字不改（不经 unparse），来源只有那三项。"""
    graph = S.caliber_graph(expression="cell(nodes.fetch,0,'gmv')")
    wf = await S.publish(graph=graph)
    pinned = {"workflow_id": wf, "workflow_version": 1, "node_id": "card"}
    row = await run(S.chain(S.node("start", "input"), S.node("fetch", "tool", tool=S.tool(), args={"sql": RENDERED}),
                            S.node("mine", "metrics", caliber_from=pinned),
                            S.node("out", "output", fields=[{"name": "r", "value": "{{ nodes.mine.text }}"}])))
    assert row.status == "succeeded", row.error
    [entry] = (await finished(row.id, "mine")).data["evidence"]
    card = artifact_store.load(entry["artifact"])
    assert card["source"] == pinned and entry["source"] == pinned
    assert [m["expression"] for m in card["metrics"]] == ["cell(nodes.fetch,0,'gmv')"]
    assert set(card) == {"kind", "caliber", "caliber_version", "metrics", "source"}


async def test_a_malformed_mapping_is_refused_before_running():
    wf = await S.publish()
    with pytest.raises(ValueError, match="节点改名映射有误"):
        await run_manager.start(graph=composed(wf, rename={"fetch": "c1 fetch"}), input_payload={})


# --------------------------------------------------------------------------
# 画布校验、升版检查
# --------------------------------------------------------------------------


def _codes(graph: dict) -> list[tuple[str | None, str]]:
    return [(i.field, i.message) for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]


def test_validation_checks_the_mapping():
    good = composed("wf1", rename={"fetch": "c1_fetch"})
    assert not [f for f, _ in _codes(good) if (f or "").startswith("caliber_from")]
    shape = _codes(composed("wf1", rename={"fetch": ""}))
    assert any(f == "caliber_from.nodes" and "映射有误" in m for f, m in shape)
    missing = _codes(composed("wf1", rename={"fetch": "nowhere"}))
    assert any(f == "caliber_from.nodes" and "没有节点「nowhere」" in m for f, m in missing)
    # 改名后的节点在口径卡下游：算卡的时候它还没执行
    late = composed("wf1", rename={"fetch": "c1_fetch"})
    late["edges"] = [{"source": "start", "target": "c1"}, {"source": "c1", "target": "c1_fetch"},
                     {"source": "c1_fetch", "target": "out"}]
    assert any(f == "caliber_from.nodes" and "不在口径卡的上游" in m for f, m in _codes(late))


async def test_upgrade_checks_treat_renamed_cards_like_any_pinned_card():
    from app.engine.governance import unresolved_caliber_upgrades

    wf = await S.publish()
    await S.publish(version=2, workflow_id=wf)
    async with SessionLocal() as session:
        errors, _ = await unresolved_caliber_upgrades(
            session, GraphSpec.model_validate(composed(wf, rename={"fetch": "c1_fetch"})))
        assert errors and "上游已有 v2" in errors[0]
        _, [event] = await unresolved_caliber_upgrades(session, GraphSpec.model_validate(
            composed(wf, rename={"fetch": "c1_fetch"}, upgrade_policy="dual")))
        assert event["caliber_nodes"] == {"fetch": "c1_fetch"} and event["pinned"] == 1 and event["latest"] == 2
        # 没改名的卡：事件和以前一样，没有这个键
        plain = composed(wf, fetch_id="fetch", card_id="mine", upgrade_policy="dual")
        _, [old] = await unresolved_caliber_upgrades(session, GraphSpec.model_validate(plain))
        assert "caliber_nodes" not in old


# --------------------------------------------------------------------------
# 运行对比
# --------------------------------------------------------------------------


def test_compare_sees_the_same_caliber_under_different_node_names():
    from app.engine.compare import _caliber_reasons

    def side(prefix: str) -> tuple[dict, dict]:
        metric = {"id": "gmv", "expression": f"cell(nodes.{prefix}fetch, 0, 'gmv')", "decimals": 2,
                  "format": "thousands", "unit": "元"}
        card = {"caliber": "周销售", "caliber_version": "v1", "metrics": [metric],
                "source": {"workflow_id": "w", "workflow_version": 1, "node_id": "card",
                           **({"nodes": {"fetch": f"{prefix}fetch"}} if prefix else {})}}
        return metric, card

    (bm, bc), (tm, tc) = side(""), side("c1_")
    assert _caliber_reasons(bm, tm, bc, tc) == []
    # 映射反着换回来之后式子还是不同的，照旧说表达式改了
    tm2 = {**tm, "expression": "cell(nodes.c1_fetch, 0, 'gmv') * 2"}
    assert any("表达式由" in r for r in _caliber_reasons(bm, tm2, bc, tc))


async def test_runs_of_the_same_composed_graph_compare_unchanged():
    from app.db.models import Workflow

    wf = await S.publish()
    graph = composed(wf, rename={"fetch": "c1_fetch"})
    async with SessionLocal() as session:
        saved = Workflow(name="组合的答案", graph=graph)
        session.add(saved)
        await session.commit()
        saved_id = saved.id
    a, b = [await S.wait((await run_manager.start(graph=graph, workflow_id=saved_id, input_payload={})).id)
            for _ in range(2)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/runs/{b.id}/compare", params={"base": a.id})
    assert resp.status_code == 200, resp.text
    [metric] = resp.json()["metrics"]
    assert metric["classification"] == "unchanged", metric
