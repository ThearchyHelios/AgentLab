"""口径卡：分母为 0 时算不出来就是空值，和「缺少输入」分开说；指标清单按格式渲染。

部署上撞过：一份周报的口径卡全写成 `round((a - b) / b, 4) if b else 0`，两周都没有数据时周环比、复访率写成 0 / 0%，
读起来像「持平」。现在：

- 助手的节点手册写明分母可能为 0 时写 `… if 分母 else None`，不要写 else 0；画布校验对「条件表达式一边有除法、另一边是
  常量 0」给提醒（metrics.zero_for_undefined，按语法树认，嵌套的也认）；
- 输入都齐、是计算式自己交回空值的指标多记 undefined: true（状态仍是 missing_input，老运行、按状态认的地方不受影响），
  清单、写作目录、引用解析不了的原因、运行对比都说「算不出来」，不说「缺少输入」；
- 指标清单（{{ nodes.卡.text }}）按卡上渲染好的字写：比率 0.0007 显示成 0.07%，不再拼原值写成「0.0007%」；值不为空
  却显示不出来的照实说。

数据、口径都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph

KPI = "{'cur': 0, 'prev': 0, 'visits': 1200, 'repeat': 9, 'gmv': 45678.5, 'tiny': 0.000000001}"
METRICS = [
    {"id": "wow", "name": "周环比", "decimals": 4, "format": "percent_of_ratio",
     "expression": "round((vars.kpi.cur - vars.kpi.prev) / vars.kpi.prev, 4) if vars.kpi.prev else None"},
    {"id": "lost", "name": "缺一项", "expression": "vars.kpi.nothing"},
    {"id": "rate", "name": "复访率", "unit": "%", "decimals": 4, "format": "percent_of_ratio",
     "expression": "round(vars.kpi.repeat / vars.kpi.visits, 4)"},
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "tiny", "name": "很小的数", "decimals": 2, "expression": "vars.kpi.tiny"},
]


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(metrics=METRICS, kpi=KPI):
    nodes = [node("start", "input"), node("fetch", "transform", mode="expression", expression=kpi, assign_to="kpi"),
             node("card", "metrics", caliber="周报口径", caliber_version="v1", metrics=metrics),
             node("out", "output", fields=[{"name": "清单", "value": "{{ nodes.card.text }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _card() -> tuple[Run, dict]:
    run = await run_manager.start(graph=graph(), input_payload={})
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
    [entry] = event.data["evidence"]
    return row, artifact_store.load(entry["artifact"])


async def test_undefined_is_told_apart_from_a_missing_input():
    row, card = await _card()
    by_id = {m["id"]: m for m in card["metrics"]}
    wow, lost = by_id["wow"], by_id["lost"]
    # 状态都是 missing_input（按状态认的地方照旧）；算不出来的多一个 undefined
    assert wow["value"] is None and wow["status"] == "missing_input" and wow["undefined"] is True
    assert lost["value"] is None and lost["status"] == "missing_input" and "undefined" not in lost
    # 有值的指标没有这个键：卡和以前一字不差
    assert all("undefined" not in by_id[k] for k in ("rate", "gmv", "tiny"))
    lines = row.output["清单"].split("\n")
    assert "- wow（周环比）= —（算不出来：这组输入下计算式没有值，比如分母为 0）" in lines
    assert "- lost（缺一项）= —（缺少输入，无值）" in lines


async def test_the_list_shows_the_rendered_value():
    row, _ = await _card()
    lines = row.output["清单"].split("\n")
    assert "- rate（复访率）= 0.75%" in lines, lines            # 不是「0.0075%」
    assert "- gmv（销售额）= 45,678.5元" in lines
    # 卡先按小数位取整，再按格式渲染：很小的数取整成 0 就显示 0.00（和报告里一样），不拼原值
    assert "- tiny（很小的数）= 0.00" in lines and "0.000000001" not in row.output["清单"]


def test_a_value_that_cannot_be_shown_is_said_so():
    """有值、按口径卡的格式显示不出来（不是有限的数这类，卡上 rendered 记成「—」）：照实说，不把原值拼出来。"""
    from app.engine.nodes.metrics import _listed

    assert _listed({"value": float("inf"), "rendered": "—"}) == "—（有值，但按设定的小数位和格式显示不出来）"
    assert _listed({"value": None, "rendered": "—", "undefined": True}).startswith("—（算不出来")


# --------------------------------------------------------------------------
# 画布校验：「分母为 0 时给 0」
# --------------------------------------------------------------------------


def _zero_warnings(expression: str) -> list:
    g = graph(metrics=[{"id": "x", "name": "x", "expression": expression}])
    issues = validate_graph(GraphSpec.model_validate(g)).issues
    return [i for i in issues if i.code == "metrics.zero_for_undefined"]


@pytest.mark.parametrize("expression", [
    "round((vars.kpi.cur - vars.kpi.prev) / vars.kpi.prev, 4) if vars.kpi.prev else 0",
    "vars.kpi.repeat / vars.kpi.visits if vars.kpi.visits else 0.0",
    "0 if not vars.kpi.visits else vars.kpi.repeat / vars.kpi.visits",              # 两边对调
    "round(vars.kpi.repeat / vars.kpi.visits if vars.kpi.visits > 0 else 0, 4)",     # 套在 round 里
    "vars.kpi.gmv if vars.kpi.cur else (vars.kpi.repeat / vars.kpi.visits if vars.kpi.visits else 0)",   # 嵌套
])
def test_zero_for_undefined_is_warned(expression):
    [issue] = _zero_warnings(expression)
    assert issue.level == "warning" and issue.field == "metrics[0].expression"
    assert "None" in issue.message and "持平" in issue.message


@pytest.mark.parametrize("expression", [
    "round((vars.kpi.cur - vars.kpi.prev) / vars.kpi.prev, 4) if vars.kpi.prev else None",
    "vars.kpi.cur if vars.kpi.prev else 0",          # 没有除法：0 可能就是想要的值
    "vars.kpi.repeat / vars.kpi.visits",
    "vars.kpi.repeat / vars.kpi.visits if vars.kpi.visits else 1",
])
def test_other_expressions_are_left_alone(expression):
    assert _zero_warnings(expression) == []


def test_the_copilot_is_told_to_write_none():
    from app.api.copilot import NODE_REFERENCE

    start = NODE_REFERENCE.index("- metrics：口径卡")
    section = NODE_REFERENCE[start:NODE_REFERENCE.index("\n- dual_check：", start)]
    assert "if vars.kpi.gmv_prev else None" in section and "不要写 else 0" in section


# --------------------------------------------------------------------------
# 写作目录、引用、对比的说法
# --------------------------------------------------------------------------


def _catalog(undefined: bool):
    from app.engine.evidence import build_catalog

    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "artifact": "c" * 64, "metrics": [
        {"id": "wow", "name": "周环比", "unit": "", "value": None, "decimals": 4, "format": "percent_of_ratio",
         "status": "missing_input", **({"undefined": True} if undefined else {}), "expression": "…"}]}
    ledger = [{"kind": "metric_set", "node_id": "card", "exec": 1, "artifact": "c" * 64, "caliber": "周报口径",
               "version": "v1", "metrics": ["wow"]}]
    return build_catalog(nodes={"card": card}, ledger=ledger, inputs={})


@pytest.mark.parametrize("undefined, said", [(True, "算不出来"), (False, "缺少输入")])
def test_the_writer_and_the_reason_say_which(undefined, said):
    from app.engine.evidence import compose_doc, writer_catalog

    catalog = _catalog(undefined)
    assert catalog["m:wow"].get("undefined", False) is undefined
    text = writer_catalog(catalog).text
    line = next(ln for ln in text.splitlines() if "[[m:wow]]" in ln)
    assert ("算不出来" in line) is undefined and "不要引用" in line
    doc = compose_doc("周环比 [[m:wow]]。", catalog)
    [violation] = [v for v in doc["violations"] if v["code"] == "unresolved_ref"]
    assert said in violation["message"], violation


def test_compare_says_which():
    from app.engine import compare

    assert compare.missing_reason("这次运行", {"status": "missing_input", "undefined": True}) \
        == "这次运行这个指标算不出来（这组输入下计算式没有值，比如分母为 0）"
    assert compare.missing_reason("基准运行", {"status": "missing_input"}) == "基准运行这个指标缺少输入，没有值"
    assert compare.missing_reason("基准运行", {"status": "ok"}) is None
