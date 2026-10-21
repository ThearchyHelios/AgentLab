"""口径卡的可比性挂钩（docs/design/analysis-round/00-总方案.md §3.4 第 3 条）：一个指标的输入落在两份不同的查询快照上、
两份 SQL 同形只差字面量（本周对上周）时，口径卡执行时自动做一次可比性检查，结论落成工件、记在指标的 comparability 上。

- 同形只差日期 → 有 comparability，早的那期是基期（和表达式里谁先写无关）；同一对快照只查一次
- SQL 结构不同（退款额 / 销售额）、两次是同一条 SQL → 不查、不记日志
- 算子出错、算不了、还没有算子（第 1 波的 run 抛 NotImplementedError）→ 不挡口径卡，记一条 metric_comparability_skipped
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select

from app.analysis.contract import CallError
from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.nodes.metrics import COMPARABILITY_SKIPPED, comparability_reasons
from app.engine.runner import run_manager
from tests.fixtures.analysis_ops import FakeOps

TOOL = "db_query__shop"
THIS = "SELECT SUM(amount) AS gmv FROM orders WHERE day BETWEEN '2026-10-12' AND '2026-10-18'"
LAST = "SELECT SUM(amount) AS gmv FROM orders WHERE day BETWEEN '2026-10-05' AND '2026-10-11'"
REFUNDS = "SELECT SUM(refund) AS gmv FROM orders WHERE refund > 0 AND day BETWEEN '2026-10-12' AND '2026-10-18'"
WOW = "(cell(nodes.this_week, 0, 'gmv') - cell(nodes.last_week, 0, 'gmv')) / cell(nodes.last_week, 0, 'gmv')"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, day TEXT, region TEXT, amount REAL, refund REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [
        (1, "2026-10-06", "华东", 500.0, 0.0), (2, "2026-10-08", "华南", 400.0, 20.0),
        (3, "2026-10-13", "华东", 450.0, 0.0), (4, "2026-10-15", "华北", 300.0, 15.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(metrics, *, this=THIS, last=LAST):
    nodes = [node("start", "input"),
             node("this_week", "tool", tool=TOOL, args={"sql": this}),
             node("last_week", "tool", tool=TOOL, args={"sql": last}),
             node("card", "metrics", caliber="周报口径", metrics=metrics),
             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def run_card(g) -> tuple[Run, dict, list[dict]]:
    run = await run_manager.start(graph=g, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        evts = [(e.type, e.node_id, e.data) for e in (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))).scalars()]
    [finished] = [d for t, n, d in evts if t == "node.finished" and n == "card"]
    card = artifact_store.load(finished["artifact"])
    logs = [d for t, n, d in evts if t == "log" and n == "card"]
    return row, card, logs


def by_id(card):
    return {m["id"]: m for m in card["metrics"]}


async def test_two_periods_of_the_same_query_get_a_comparability_check(monkeypatch):
    ops = FakeOps(monkeypatch)
    _, card, logs = await run_card(graph([
        {"id": "wow", "name": "销售额环比", "expression": WOW, "decimals": 4},
        {"id": "diff", "name": "销售额变化", "expression": "cell(nodes.last_week, 0, 'gmv') - cell(nodes.this_week, 0, 'gmv')"},
        {"id": "this", "name": "本周销售额", "expression": "cell(nodes.this_week, 0, 'gmv')"}]))
    metrics = by_id(card)
    [call] = ops.calls          # 同一对快照只查一次
    # 早的那期是基期：表达式里先写的是本周还是上周都一样
    assert call["request"] == {"op": "comparable", "mode": None,
                               "params": {"base_query": "last_week", "target_query": "this_week"}}
    base, target = call["snapshots"]["last_week"], call["snapshots"]["this_week"]
    assert base["sql"] == LAST and target["sql"] == THIS and base["node_id"] == "last_week"
    assert base["artifact"] == metrics["wow"]["inputs"][1]["artifact"] and base["masked"] == []
    found = metrics["wow"]["comparability"]
    assert found == metrics["diff"]["comparability"]
    stored = artifact_store.load(found["artifact"])
    assert stored["op"] == "comparable" and found == {"artifact": found["artifact"], "verdict": "comparable",
                                                      "phrase": "两期可比", "reasons": []}
    # 只用到一份快照的指标不查
    assert "comparability" not in metrics["this"]
    assert not [log for log in logs if log.get("code") == COMPARABILITY_SKIPPED]


async def test_an_incomparable_pair_lists_its_reasons(monkeypatch):
    FakeOps(monkeypatch, verdicts=["incomparable"])
    _, card, _ = await run_card(graph([{"id": "wow", "name": "销售额环比", "expression": WOW}]))
    found = by_id(card)["wow"]["comparability"]
    assert found["verdict"] == "incomparable" and found["phrase"] == "两期不可比：本期只有 3 天数据"
    assert found["reasons"] == ["本期只有 3 天数据"]


async def test_different_structures_and_the_same_query_are_not_checked(monkeypatch):
    ops = FakeOps(monkeypatch)
    ratio = "cell(nodes.this_week, 0, 'gmv') / cell(nodes.last_week, 0, 'gmv')"
    _, card, logs = await run_card(graph([{"id": "refund_rate", "name": "退款率", "expression": ratio}], last=REFUNDS))
    assert "comparability" not in by_id(card)["refund_rate"] and ops.calls == []
    _, card, logs2 = await run_card(graph([{"id": "same", "name": "同一条", "expression": ratio}], last=THIS))
    assert "comparability" not in by_id(card)["same"] and ops.calls == []
    assert not [log for log in [*logs, *logs2] if log.get("code") == COMPARABILITY_SKIPPED]


async def test_without_the_operator_the_card_still_runs():
    """第 1 波的 app.analysis.run 还抛 NotImplementedError：口径卡照常算，照实记一条没做成。"""
    _, card, logs = await run_card(graph([{"id": "wow", "name": "销售额环比", "expression": WOW, "decimals": 4}]))
    wow = by_id(card)["wow"]
    assert "comparability" not in wow and wow["value"] == pytest.approx((450 + 300 - 900) / 900, abs=1e-4)
    [log] = [log for log in logs if log.get("code") == COMPARABILITY_SKIPPED]
    assert log["level"] == "warn" and log["metric"] == "wow" and "销售额环比" in log["message"]
    assert "没做成可比性检查" in log["message"]


@pytest.mark.parametrize("ops_kw", [
    {"errors": [CallError("no_window", "认不出时间条件")]},
    {"errors": [RuntimeError("探查超时")]},
    {"statuses": ["refused"]},
])
async def test_failures_do_not_block_the_card(monkeypatch, ops_kw):
    FakeOps(monkeypatch, **ops_kw)
    _, card, logs = await run_card(graph([{"id": "wow", "name": "销售额环比", "expression": WOW}]))
    assert "comparability" not in by_id(card)["wow"]
    [log] = [log for log in logs if log.get("code") == COMPARABILITY_SKIPPED]
    assert log["reason"]


def test_reasons_come_from_the_check_table_when_there_is_one():
    result = {"verdict": {"code": "conditional"}, "phrase": "两期有条件可比：两段的周末天数不同",
              "table": {"columns": ["check", "status", "reason"], "labels": {},
                        "rows": [["同口径", "pass", None], ["窗口长度", "warn", "窗口长度不同：7 天对 6 天"],
                                 ["星期构成", "warn", "两段的周末天数不同：2 天对 1 天"], ["整期", "unknown", "核对不了"]]}}
    assert comparability_reasons(result) == ["窗口长度不同：7 天对 6 天", "两段的周末天数不同：2 天对 1 天"]
    assert comparability_reasons({"verdict": {"code": "comparable"}, "phrase": "两期可比"}) == []
