"""转抄另一张口径卡的指标时，出处链跨卡往下追（engine/lineage.metric_copy、engine/nodes/metrics._upstream、
api/evidence._upstream_steps）。

对账卡的「口径」、召回卡的「旧版 / 新版」都写成 nodes.<口径卡>.metrics[i].value。以前出处停在「来自口径卡指标」，对账运行、
召回运行一律降档出具（「输入没追到原始数据」）；口径卡自己的指标明明追得到取数查询。现在：
- 运行时接上那个指标自己的出处（upstream）：那个指标的输入都追到了原始数据，这一项才算追到，判档按追到底的结果；
  那个指标不完整、所依据的查询没通过 SQL 检查的，转抄出来的同样不完整、存疑；转抄的转抄一层层往下追，到了层数上限
  就算没追到；
- 证据接口：转抄的输入后面接一步 upstream（那张卡的那个指标、它的输入），一直到口径的取数查询（查询步骤照常给）；
- 真跑对账、召回：完整出具，三个数都点得到口径的取数查询。
数据是虚构店铺库。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.lineage import MAX_COPY_DEPTH, traced, untraced
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.ledger import recall
from tests import ledger_samples as S

WINDOW = "status = 'paid' AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'"


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await recall.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def metric(mid: str, expression: str, name: str = "") -> dict:
    return {"id": mid, "name": name or mid, "unit": "元", "decimals": 2, "expression": expression}


async def run_chain(*middle: dict, contract_from: str) -> object:
    graph = S.chain(S.node("start", "input"), *middle,
                    S.node("out", "output", fields=[{"name": "数", "value": f"{{{{ nodes.{contract_from}.metrics[0].value }}}}"}],
                           contract={"metrics_from": contract_from}))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    return row


async def card(run_id: str, node_id: str) -> dict:
    from app.api.evidence import _sealed, _sealed_cards

    sealed = await _sealed(run_id)
    [(_, found)] = [(a, c) for n, a, c in _sealed_cards(sealed) if n == node_id]
    return {m["id"]: m for m in found["metrics"]}


# --------------------------------------------------------------------------
# 运行时
# --------------------------------------------------------------------------


async def test_a_copied_metric_traces_through_the_card_and_issues_formally():
    fetch = S.node("fetch", "tool", tool=S.tool(), args={"sql": f"SELECT SUM(amount) AS gmv FROM orders WHERE {WINDOW}"})
    a = S.node("a", "metrics", caliber="口径", caliber_version="v1", metrics=[metric("gmv", "cell(nodes.fetch, 0, 'gmv')")])
    b = S.node("b", "metrics", caliber="转抄", caliber_version="v1", metrics=[
        metric("cal", "nodes.a.metrics[0].value"), metric("half", "(nodes.a.metrics[0].value) / 2")])
    c = S.node("c", "metrics", caliber="再转抄", caliber_version="v1", metrics=[metric("again", "nodes.b.metrics[0].value")])
    row = await run_chain(fetch, a, b, c, contract_from="c")
    first, second = await card(row.id, "b"), await card(row.id, "c")
    [item] = first["cal"]["inputs"]
    assert item["via"] == "metric" and item["node_id"] == "a"
    assert item["upstream"]["traced"] is True and item["upstream"]["depth"] == 1 and item["upstream"]["metric"] == "gmv"
    assert traced(item) and untraced(first["half"]["inputs"]) == []
    [deep] = second["again"]["inputs"]
    assert deep["upstream"]["traced"] is True and deep["upstream"]["depth"] == 2
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal", issuance["gaps"]


async def test_copying_a_metric_that_is_not_traced_still_stops_there():
    calc = S.node("calc", "transform", mode="json", template='{"v": 5}', assign_to="calc")
    a = S.node("a", "metrics", caliber="口径", caliber_version="v1", metrics=[metric("five", "vars.calc.v")])
    b = S.node("b", "metrics", caliber="转抄", caliber_version="v1", metrics=[metric("cal", "nodes.a.metrics[0].value")])
    row = await run_chain(calc, a, b, contract_from="b")
    [item] = (await card(row.id, "b"))["cal"]["inputs"]
    assert item["upstream"]["traced"] is False and not traced(item)
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("指标「cal」的输入没追到原始数据" in g for g in issuance["gaps"]), issuance["gaps"]


async def test_an_incomplete_upstream_metric_makes_the_copy_incomplete_too():
    fetch = S.node("fetch", "tool", tool=S.tool(), args={"sql": "SELECT id, amount FROM orders ORDER BY id", "limit": 2})
    parse = S.node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="rows")
    a = S.node("a", "metrics", caliber="口径", caliber_version="v1", metrics=[metric("n", "len(vars.rows.rows)")])
    b = S.node("b", "metrics", caliber="转抄", caliber_version="v1", metrics=[metric("cal", "nodes.a.metrics[0].value")])
    row = await run_chain(fetch, parse, a, b, contract_from="b")
    upstream, copied = (await card(row.id, "a"))["n"], (await card(row.id, "b"))["cal"]
    assert upstream["incomplete"] is True
    assert copied["incomplete"] is True and copied["truncated_sources"] == upstream["truncated_sources"]
    assert copied["incomplete_reason"] == upstream["incomplete_reason"]
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any(g.startswith("指标「cal」基于被截断的查询结果计算") for g in issuance["gaps"]), issuance["gaps"]


def test_copies_of_copies_stop_at_the_depth_limit():
    from app.engine.nodes.metrics import _upstream

    def card_with(depth: int) -> dict:
        inputs = [{"path": "nodes.x.metrics[0].value", "value": 1, "via": "metric", "node_id": "x",
                   "upstream": {"metric": "m", "artifact": "a" * 64, "depth": depth, "traced": True}}]
        return {"kind": "metric_set", "artifact": "b" * 64, "metrics": [{"id": "m", "value": 1, "inputs": inputs}]}

    ok = _upstream("nodes.y.metrics[0].value", {"nodes": {"y": card_with(MAX_COPY_DEPTH - 1)}})
    assert ok is not None and ok["traced"] is True and ok["depth"] == MAX_COPY_DEPTH
    too_deep = _upstream("nodes.y.metrics[0].value", {"nodes": {"y": card_with(MAX_COPY_DEPTH)}})
    assert too_deep is not None and too_deep["traced"] is False and too_deep["depth"] == MAX_COPY_DEPTH + 1
    # 整张卡、清单文字、指标的名称这类不是转抄一个指标的值：不往下追
    assert _upstream("nodes.y.metrics", {"nodes": {"y": card_with(0)}}) is None
    assert _upstream("nodes.y.metrics[0].name", {"nodes": {"y": card_with(0)}}) is None


# --------------------------------------------------------------------------
# 真跑对账、召回：完整出具，证据面板一路点到口径的取数查询
# --------------------------------------------------------------------------


def _steps(chain: list[dict], kind: str) -> list[dict]:
    return [s for s in chain if s["step"] == kind]


async def test_a_reconcile_run_traces_the_caliber_side_down_to_its_fetch(client, monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
    [action] = [a for a in body["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json={
        "query": action["query"], "workflow_id": wf, "version": 1, "confirm": True,
        "inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 201, res.text
    done = await S.wait(res.json()["run_id"])
    assert done.status == "succeeded", done.error
    issuance = done.output["_issuance"]
    assert issuance["tier"] == "formal", issuance
    metrics = await card(done.id, "reconcile")
    [cal] = metrics["cal_0"]["inputs"]
    assert cal["node_id"] == "card" and cal["upstream"]["traced"] is True

    for mid in ("cal_0", "diff_0"):
        res = await client.get(f"/api/runs/{done.id}/evidence/metrics/reconcile/{mid}")
        assert res.status_code == 200, res.text
        chain = res.json()["chain"]
        assert chain[0]["untraced"] == [], "转抄的口径那一项跨卡追到了原始数据"
        [up] = _steps(chain, "upstream")
        assert up["of"] == "nodes.card.metrics[0].value" and up["node_id"] == "card" and up["metric"] == "gmv"
        assert up["traced"] is True and up["sealed"] is True and up["hash_ok"] is True and up["value"] == 170.5
        [read] = up["inputs"]
        assert read["path"] == "cell(nodes.fetch, 0, 'gmv')" and read["via"] == "tool_cell"
        # 口径的取数查询的那一步照常给（点得到）
        fetches = [q for q in _steps(chain, "query") if q.get("artifact") == read["artifact"]]
        assert len(fetches) == 1 and fetches[0]["node_id"] == "fetch"
    # 「这次」那一项不是转抄的，没有 upstream 这一步
    ours = (await client.get(f"/api/runs/{done.id}/evidence/metrics/reconcile/ours_0")).json()["chain"]
    assert _steps(ours, "upstream") == []


async def test_a_recall_run_traces_both_versions_down_to_their_fetches(client, monkeypatch):
    from tests.test_ledger_recall import WITH_REFUNDED, answer, ledger_settings, publish_v2, run_recall

    await ledger_settings(auto_issue=False, issue_daily_limit=100_000)
    wf = await S.publish()
    await answer(monkeypatch)
    await publish_v2(wf, WITH_REFUNDED)
    [done] = await run_recall(wf)
    assert done.status == "succeeded", done.error
    assert done.output["_issuance"]["tier"] == "formal", done.output["_issuance"]
    res = await client.get(f"/api/runs/{done.id}/evidence/metrics/recall/diff_0")
    assert res.status_code == 200, res.text
    chain = res.json()["chain"]
    ups = {u["node_id"]: u for u in _steps(chain, "upstream")}
    assert set(ups) == {"old_card", "new_card"}
    assert all(u["traced"] for u in ups.values())
    reads = {u["node_id"]: u["inputs"][0]["artifact"] for u in ups.values()}
    queried = {q["node_id"] for q in _steps(chain, "query") if q.get("artifact") in reads.values()}
    assert queried == {"old_fetch", "new_fetch"}
