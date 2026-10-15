"""指标出具（engine/issuance.metrics_only，用户 2026-10-15 拍板）：纯指标看板——契约只写「指标来自」，没有「报告来自」
也没有「叙述」——出具的就是指标本身。三档的语义不变，核对的对象从叙述换成指标：

- 必需指标齐、每个指标的输入追到原始数据（查询快照的格、运行输入）、成果字段里给人看的数都回指得上：完整出具
- 指标的输入停在别的节点（表达式整形、带过滤器的解析……）：降档，缺口写明哪个指标停在哪个节点
- 成果字段里有回指不上的数：降档（strict 下不予出具）；必需指标缺了：不予出具
- 受管门禁：只写「指标来自」不再报「既没有报告来自、也没有叙述」；每个指标都要静态地追到原始数据，追不到的挡住发布

库和数字都是编的。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.governance import lint_for_publish
from app.engine.issuance import metrics_only
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec

SQL = "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def board(source: str, *, gmv: str = "vars.q.rows[0][0]", fields=None, required=("gmv", "orders"), strict=False):
    return chain(
        node("start", "input", "开始", fields=[{"name": "week", "default": "2026-W40"}]),
        node("fetch", "tool", "取数", tool=f"db_query__{source}", args={"sql": SQL}),
        node("parse", "transform", "解析查询结果", mode="json", template="{{ nodes.fetch }}", assign_to="q"),
        node("raw_n", "transform", "整理查询结果", mode="json", template="{{ nodes.fetch | trim }}", assign_to="raw"),
        node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": gmv},
            {"id": "orders", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'orders')"},
        ]),
        node("out", "output", "成果", fields=fields if fields is not None else [
            {"name": "本期", "value": "{{ input.week }}"}, {"name": "指标", "value": "{{ nodes.card.text }}"}],
            contract={"metrics_from": ["card"], "required": list(required), "strict": strict}),
    )


@pytest.mark.parametrize("contract, want", [
    ({"metrics_from": ["card"], "required": ["gmv"]}, True),
    ({"metrics_from": "card"}, True),
    ({"metrics_from": ["card"], "narrative": "销售额 {{ nodes.card.text }}"}, False),
    ({"metrics_from": ["card"], "report_from": "write"}, False),
    ({"required": ["gmv"]}, False),
    ("card", False),
])
def test_metrics_only(contract, want):
    assert metrics_only(contract) is want


# --------------------------------------------------------------------------
# 运行时
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL);")
        db.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 100.5), (2, 200.0), (3, 300.0)])
    name = f"mx_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


async def issuance_of(graph: dict) -> dict:
    run = await run_manager.start(graph=graph, input_payload={"week": "2026-W40"})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return (row.output or {})["_issuance"]
    raise AssertionError(f"运行 {run.id} 没跑完：{row.status}")


async def test_a_board_whose_metrics_reach_the_data_is_formal(shop):
    issuance = await issuance_of(board(shop))
    assert (issuance["tier"], issuance["mode"]) == ("formal", "metrics")
    # 不再记「出具契约未设置『叙述』」：没有叙述不是缺口，核的是指标本身
    assert issuance["gaps"] == [] and issuance["unmatched_numbers"] == []
    # 成果字段里的数（口径卡的文字）都回指到了指标
    assert issuance["matched_numbers"] >= 2


async def test_an_input_that_stops_short_of_the_data_degrades(shop):
    issuance = await issuance_of(board(shop, gmv="vars.raw.rows[0][0]"))
    assert issuance["tier"] == "degraded"
    [gap] = issuance["gaps"]
    assert "销售额" in gap and "没追到原始数据" in gap and "整理查询结果" in gap


async def test_numbers_in_the_fields_must_trace_and_required_metrics_must_be_there(shop):
    loose = await issuance_of(board(shop, fields=[{"name": "备注", "value": "本周目标 12345 元"}]))
    assert loose["tier"] == "degraded" and [u["token"] for u in loose["unmatched_numbers"]] == ["12345"]
    strict = await issuance_of(board(shop, fields=[{"name": "备注", "value": "本周目标 12345 元"}], strict=True))
    assert strict["tier"] == "withheld"
    missing = await issuance_of(board(shop, required=("gmv", "orders", "refund_rate")))
    assert (missing["tier"], missing["missing_required"]) == ("withheld", ["refund_rate"])


# --------------------------------------------------------------------------
# 受管门禁
# --------------------------------------------------------------------------


def gate(graph: dict, level: str = "governed") -> dict[str, str]:
    return {i.code: i.level for i in lint_for_publish(GraphSpec.model_validate(graph), level=level).issues if i.code}


def test_a_metrics_only_contract_is_not_missing_its_checks():
    codes = gate(board("shop"))
    assert "contract.report_from_missing" not in codes and "governed.report_from_required" not in codes
    assert "governed.metric_untraced" not in codes


def test_governed_metrics_must_reach_the_data_statically():
    codes = gate(board("shop", gmv="vars.raw.rows[0][0]"))
    assert codes.get("governed.metric_untraced") == "error"
    # 已发布级不按这一条挡：运行时照样降档、照实写缺口
    assert "governed.metric_untraced" not in gate(board("shop", gmv="vars.raw.rows[0][0]"), level="published")


def test_a_contract_that_checks_nothing_is_still_refused():
    graph = board("shop")
    graph["nodes"][-1]["data"]["config"]["contract"] = {"required": ["gmv"]}
    assert gate(graph).get("contract.report_from_missing") == "error"


def test_a_board_with_a_report_or_model_text_is_not_metrics_only():
    """上游有报告撰写节点、或成果字段里有模型写的话：那是叙述，不能因为契约少写了「报告来自」就改成只核指标。"""
    with_report = board("shop")
    with_report["nodes"].insert(-1, node("write", "report", "写周报", metrics_from=["card"]))
    with_report["edges"] = [*with_report["edges"][:-1], {"source": "card", "target": "write"},
                            {"source": "write", "target": "out"}]
    assert gate(with_report).get("contract.report_from_missing") == "error"
    with_model = board("shop", fields=[{"name": "点评", "value": "{{ nodes.say.text }}"}])
    with_model["nodes"].insert(-1, node("say", "llm", "点评", prompt="点评一下"))
    with_model["edges"] = [*with_model["edges"][:-1], {"source": "card", "target": "say"},
                           {"source": "say", "target": "out"}]
    assert gate(with_model, level="published").get("contract.report_from_missing") == "warning"
