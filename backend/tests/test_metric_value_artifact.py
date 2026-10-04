"""口径卡给容器型的 vars.* 输入另存一份完整值（value_artifact），数字推演据此重算。

卡里的输入对列表、字典只记一个大概（「[12 项]」），事后要改一行、重算一遍，手里没有完整的值。nodes.*、input.*
的完整值在那个节点封存的产出工件里，agent 字段能按出处从查询快照还原；vars 是谁写的、写进去的是不是节点产出
本身说不准，只能在口径卡算的那一刻存下来。

守的是「只多一个键」：只有标量输入的卡逐字节和改动之前一样（工件 id 不变，引用它的报告也就不变）；带容器型
vars 输入的卡，去掉 value_artifact 之后和改动之前一样。金样是改动之前的代码真跑出来的（fixtures/whatif）。
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent
from app.engine.runner import run_manager

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "whatif" / "golden_cards.json").read_text("utf-8"))

SCALAR_KPI = "{'gmv': 45678.5, 'gmv_prev': 42010.0, 'orders': 1234, 'refunds': 29}"
SCALAR = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain",
     "expression": "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
    {"id": "refund_rate", "name": "退款率", "decimals": 4, "format": "percent_of_ratio",
     "expression": "vars.kpi.refunds / vars.kpi.orders"},
]
BOX_KPI = "{'items': [3, 5, 7], 'meta': {'a': 1, 'b': 2}, 'gmv': 10}"
BOX = [
    {"id": "n", "name": "条数", "expression": "len(vars.kpi.items)"},
    {"id": "m", "name": "字段数", "expression": "len(vars.kpi.meta)"},
    {"id": "s", "name": "合计", "expression": "sum(vars.kpi.items) + vars.kpi.gmv"},
]


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(expr, metrics):
    nodes = [node("start", "input"), node("fetch", "transform", mode="expression", expression=expr, assign_to="kpi"),
             node("card", "metrics", caliber="推演口径", caliber_version="v3", metrics=metrics),
             node("out", "output", fields=[{"name": "清单", "value": "{{ nodes.card.text }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def finished(g) -> tuple[Run, dict]:
    run = await run_manager.start(graph=g, input_payload={})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "node.finished", RunEvent.node_id == "card"))).scalars()
    return row, event.data


def without_value_artifacts(card: dict) -> dict:
    out = copy.deepcopy(card)
    for metric in out["metrics"]:
        for item in metric.get("inputs") or []:
            item.pop("value_artifact", None)
    return out


async def test_a_card_with_only_scalar_inputs_is_byte_identical_to_before():
    row, data = await finished(graph(SCALAR_KPI, SCALAR))
    [entry] = data["evidence"]
    assert entry["artifact"] == GOLDEN["scalar"]["artifact"], "只有标量输入的卡，工件 id 不能变"
    assert artifact_store.load(entry["artifact"]) == GOLDEN["scalar"]["card"]
    assert "value_artifact" not in json.dumps(artifact_store.load(entry["artifact"]))


async def test_container_vars_inputs_carry_their_full_value():
    row, data = await finished(graph(BOX_KPI, BOX))
    [entry] = data["evidence"]
    card = artifact_store.load(entry["artifact"])
    # 去掉新加的键，和改动之前一字不差：值、渲染、代入式、复算、其余字段都不变
    assert without_value_artifacts(card) == GOLDEN["box"]["card"]

    by_path = {(m["id"], i["path"]): i for m in card["metrics"] for i in m["inputs"]}
    items, meta = by_path[("n", "vars.kpi.items")], by_path[("m", "vars.kpi.meta")]
    assert items["value"] == "[3 项]" and meta["value"] == "{2 个字段}"
    assert artifact_store.load(items["value_artifact"]) == {"path": "vars.kpi.items", "value": [3, 5, 7]}
    assert artifact_store.load(meta["value_artifact"]) == {"path": "vars.kpi.meta", "value": {"a": 1, "b": 2}}
    # 同一个输入在两个指标里：同一份内容只存一份
    assert by_path[("s", "vars.kpi.items")]["value_artifact"] == items["value_artifact"]
    # 标量输入不另存
    assert "value_artifact" not in by_path[("s", "vars.kpi.gmv")]

    async with SessionLocal() as session:
        stored = await session.get(Artifact, (items["value_artifact"], row.id))
    assert stored is not None and stored.kind == "metric_input" and stored.node_id == "card"
    # 台账只有指标集这一条：完整值经指标集（内容寻址、封存范围内）引用，不另起台账条目
    assert [e["kind"] for e in data["evidence"]] == ["metric_set"]
    assert row.output["清单"] == "指标清单（口径 推演口径 @ v3）：\n- n（条数）= 3\n- m（字段数）= 2\n- s（合计）= 25"


async def test_only_vars_containers_are_stored():
    """nodes.* 的完整值在那个节点封存的产出工件里，不另存。"""
    row, data = await finished(graph(BOX_KPI, [
        {"id": "n", "name": "条数", "expression": "len(nodes.fetch.items)"},
        {"id": "v", "name": "条数", "expression": "len(vars.kpi.items)"},
    ]))
    card = artifact_store.load(data["evidence"][0]["artifact"])
    [n_input] = card["metrics"][0]["inputs"]
    [v_input] = card["metrics"][1]["inputs"]
    assert n_input["path"] == "nodes.fetch.items" and "value_artifact" not in n_input
    assert v_input["path"] == "vars.kpi.items" and "value_artifact" in v_input


async def test_a_value_too_large_to_keep_is_skipped(monkeypatch):
    """完整值大过上限就不另存：推演据此说明当时没有保存，卡照常算出来。"""
    from app.engine.nodes import metrics

    monkeypatch.setattr(metrics, "VALUE_ARTIFACT_MAX", 10)
    row, data = await finished(graph(BOX_KPI, BOX))
    card = artifact_store.load(data["evidence"][0]["artifact"])
    assert card == GOLDEN["box"]["card"]
