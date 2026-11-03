"""口径的内容指纹（app/data/caliber_identity.py、engine/nodes/metrics.py、ledger/match.py、ledger/labels.py）。

- definition_hash / card_hash_of：同一份定义两次哈希相同，指标顺序不影响；改 expression、name、unit 哈希就变；值、渲染
  后的字、输入出处这些运行时字段不进哈希；空的语义字段和缺省一样不写。
- version_hash：一份版本里各卡哈希排序后取 sha256，和卡片先后无关。
- 跑一张真的口径卡：metric_set 工件、节点产出、证据条目都带 definition_hash，工件 meta 也记。
- caliber_of 对一个 WorkflowVersion 形状算出每张卡的哈希、整份版本的指纹；候选里记 caliber_hash，工件 meta 记
  definition_hashes；标签用候选里的哈希。
- 已知边界：表达式里写节点 id，取数节点改名，式子跟着改，哈希就变（内容指纹，不是跨图稳定 ID）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import artifact_store
from app.data.caliber_identity import card_hash_of, definition_hash, version_hash
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent
from app.engine.runner import run_manager
from app.ledger import labels as ledger_labels
from app.ledger import match as ledger_match
from app.ledger import store as ledger_store
from tests import ledger_samples as S

#: 两张卡共用的指标定义（语义字段齐全，够看出每一栏进不进哈希）
METRICS = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'gmv')"},
    {"id": "orders", "name": "订单数", "unit": "单", "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'n')"},
]
#: 运行时口径卡的定义：从 vars 取数，不碰数据源，跑一次很便宜
KPI = "{'gmv': 45678.5, 'orders': 1234}"
STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands", "expression": "vars.kpi.gmv"},
    {"id": "orders", "name": "订单数", "unit": "单", "format": "thousands", "expression": "vars.kpi.orders"},
]


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    from sqlalchemy import select

    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


def caliber() -> ledger_match.Caliber:
    found = ledger_match.caliber_of("wf", SimpleNamespace(graph=S.caliber_graph(), version=1, level="governed",
                                                         graph_hash="h"))
    assert found is not None
    return found


# --------------------------------------------------------------------------
# 纯函数：什么进哈希、什么不进
# --------------------------------------------------------------------------


def test_the_same_definitions_hash_the_same():
    assert definition_hash("", "", METRICS) == definition_hash("", "", METRICS)
    assert definition_hash("周销售", "v1", METRICS) == definition_hash("周销售", "v1", METRICS)


def test_metric_order_does_not_matter():
    assert definition_hash("", "", METRICS) == definition_hash("", "", list(reversed(METRICS)))


def test_semantic_changes_change_the_hash():
    base = definition_hash("", "", METRICS)
    changed_expression = [{**METRICS[0], "expression": "cell(nodes.fetch, 0, 'gmv') * 2"}, METRICS[1]]
    changed_name = [{**METRICS[0], "name": "成交额"}, METRICS[1]]
    changed_unit = [{**METRICS[0], "unit": "万元"}, METRICS[1]]
    changed_decimals = [{**METRICS[0], "decimals": 0}, METRICS[1]]
    hashes = [base] + [definition_hash("", "", metrics) for metrics in
                       (changed_expression, changed_name, changed_unit, changed_decimals)]
    assert len(set(hashes)) == len(hashes), "每一处语义字段的改动都应改变哈希"


def test_runtime_values_do_not_change_the_hash():
    """值、渲染后的字、输入出处这些是运行时产物：同一份定义重跑一次哈希不变。"""
    runtime = [{**metric, "value": 1.0, "rendered": "1.0元", "inputs": [{"path": "x"}], "substituted": "1 + 1",
                "recompute_ok": True, "status": "ok", "missing_reason": "无关字段"} for metric in METRICS]
    assert card_hash_of({"metrics": runtime}) == card_hash_of({"node_id": "card", "metrics": METRICS})


def test_empty_semantic_fields_are_the_same_as_missing():
    """空串、None 不写进规范 JSON：同一份定义补了个空的无关字段，哈希不变。"""
    sparse = [{"id": "gmv", "expression": "vars.kpi.gmv"}, {"id": "orders", "expression": "vars.kpi.orders"}]
    padded = [{**metric, "unit": "", "name": None, "params": {}} for metric in sparse]
    assert definition_hash("", "", sparse) == definition_hash("", "", padded)


def test_the_caliber_name_and_version_only_count_when_given():
    assert card_hash_of({"metrics": METRICS}) == definition_hash("", "", METRICS)
    assert definition_hash("周销售", "", METRICS) != definition_hash("", "", METRICS)
    assert definition_hash("周销售", "v1", METRICS) != definition_hash("周销售", "v2", METRICS)


def test_renaming_the_fetch_node_changes_the_hash():
    """已知边界：表达式里写节点 id，改名后哈希就变——这是内容指纹，不是跨图稳定 ID。"""
    renamed = [{**METRICS[0], "expression": "cell(nodes.orders_query, 0, 'gmv')"}, METRICS[1]]
    assert definition_hash("", "", renamed) != definition_hash("", "", METRICS)


def test_version_hash_ignores_card_order():
    a, b = definition_hash("", "", METRICS), definition_hash("", "", METRICS[:1])
    assert version_hash([a, b]) == version_hash([b, a])
    assert version_hash([a, b]) != version_hash([a])
    assert len(version_hash([])) == 64


# --------------------------------------------------------------------------
# 跑一张真的口径卡：工件、节点产出、证据、meta
# --------------------------------------------------------------------------


async def test_a_running_card_records_its_definition_hash():
    graph = chain(node("start", "input"),
                  node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
                  node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1", metrics=STANDARD),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id)
    assert row.status == "succeeded", row.error

    [finished] = await events(row.id, "node.finished", "card")
    output = artifact_store.load(finished.data["artifact"])       # 节点产出（结果字典原样）
    card = artifact_store.load(output["artifact"])                # metric_set 工件
    expected = card_hash_of({"metrics": STANDARD})
    # 运行时算出来的 value / rendered / inputs 不进哈希：和只拿原始定义算的一样
    assert card["kind"] == "metric_set" and card["definition_hash"] == expected
    assert output["definition_hash"] == expected
    [entry] = finished.data["evidence"]
    assert entry["kind"] == "metric_set" and entry["definition_hash"] == expected
    async with SessionLocal() as session:
        ref = await session.get(Artifact, (output["artifact"], row.id))
    assert ref.meta["definition_hash"] == expected


# --------------------------------------------------------------------------
# 账本一侧：caliber_of、候选、工件 meta、标签
# --------------------------------------------------------------------------


def test_caliber_of_fingerprints_each_source_card():
    found = caliber()
    metrics = S.caliber_graph()["nodes"][2]["data"]["config"]["metrics"]
    card_hash = card_hash_of({"metrics": metrics})
    assert found.card_hashes == {"card": card_hash}
    assert found.definition_hash == version_hash([card_hash])
    summary = found.summary()
    assert summary["card_hashes"] == {"card": card_hash} and summary["definition_hash"] == found.definition_hash


def test_candidates_and_meta_carry_the_version_hash():
    found = caliber()
    query = ledger_match.Query("a1", "q", S.CURRENT["source"], S.EQUIVALENT, tables=frozenset({"orders"}),
                               dialect="sqlite")
    [row] = ledger_match.compare_queries([query], [found])
    assert row["status"] == "proven"
    [cand] = row["candidates"]
    assert cand["caliber_hash"] == found.definition_hash
    meta = ledger_store.meta_of({"queries": [row]})
    assert meta["calibers"] == [found.key] and meta["definition_hashes"] == {found.key: found.definition_hash}


def test_the_label_uses_the_candidate_hash_when_there_is_one():
    label = ledger_labels._caliber({"caliber": "wf@1", "workflow_id": "wf", "version": 1, "caliber_hash": "abc"})
    assert label["definition_hash"] == "abc"
    assert "definition_hash" not in ledger_labels._caliber({"caliber": "wf@1", "workflow_id": "wf", "version": 1})
