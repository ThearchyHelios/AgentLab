"""口径卡记下每个数是怎么来的：输入取自哪个节点、代入式长什么样、复算对不对得上。

以前口径卡只产出 {id, name, unit, value, expression}：报告里一个「8.7%」点开，最多能看到
一条原式，看不到当时代入的是哪两个数、它们来自哪一步。任何一个输入是 None 时，
TypeError 也没人接，整个节点带着一句「unsupported operand type(s)」死掉。
"""
from __future__ import annotations

import ast
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent
from app.engine.expressions import eval_expression, leaf_refs, parse_expression, substitute
from app.engine.runner import run_manager

# --------------------------------------------------------------------------
# leaf_refs / substitute：纯函数
# --------------------------------------------------------------------------


def tree(expr: str) -> ast.Expression:
    return parse_expression(expr)[0]


@pytest.mark.parametrize("expr, refs", [
    ("round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)",
     ["vars.kpi.gmv", "vars.kpi.gmv_prev"]),
    ("nodes.fetch.data['amount'] + input.extra", ["nodes.fetch.data['amount']", "input.extra"]),
    # 下标里的引用也是输入：它决定取哪一格
    ("vars.rows[vars.i]", ["vars.rows[vars.i]", "vars.i"]),
    ("len(vars.items) + 1", ["vars.items"]),
    ("str(1) + 'x'", []),
    ("gate == 'ok'", []),                 # 不在根名单里的名字不是引用
    ("{{ vars.x }} * 2", ["vars.x"]),     # 模板写法按 vars.x 理解
    ("last_message", ["last_message"]),
])
def test_leaf_refs(expr, refs):
    assert leaf_refs(tree(expr)) == refs


def test_substitute_inlines_scalars_and_recomputes():
    expr = "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"
    values = {"vars.kpi.gmv": 45678.5, "vars.kpi.gmv_prev": 42010.0}
    text = substitute(tree(expr), values)
    assert text == "round((45678.5 - 42010.0) / 42010.0 * 100, 1)"
    ctx = {"vars": {"kpi": {"gmv": 45678.5, "gmv_prev": 42010.0}}}
    assert eval_expression(text, {}) == eval_expression(expr, ctx) == 8.7


def test_substitute_leaves_containers_as_paths():
    """整张表代进去既看不懂、也可能超过表达式的长度上限：容器留原路径，复算时照样取得到。"""
    ctx = {"vars": {"items": [1, 2, 3], "n": -3}}
    text = substitute(tree("len(vars.items) + vars.n ** 2"),
                      {"vars.items": [1, 2, 3], "vars.n": -3})
    assert text == "len(vars.items) + (-3) ** 2"
    assert eval_expression(text, ctx) == eval_expression("len(vars.items) + vars.n ** 2", ctx) == 12


@pytest.mark.parametrize("value", ["a{{b}}c", "x}}", "{{", "长" * 81])
def test_substitute_keeps_paths_for_strings_it_cannot_inline(value):
    """含 {{ }} 的字符串代进去，parse_expression 会拒绝整条代入式，复算就成了假的「对不上」；
    上游模型写的长文本整段抄进代入式和工件也没人看得懂。这两种都留原路径。"""
    ctx = {"vars": {"s": value}}
    text = substitute(tree("len(vars.s) + 1"), {"vars.s": value})
    assert text == "len(vars.s) + 1"
    assert eval_expression(text, ctx) == len(value) + 1


def test_substitute_still_inlines_short_plain_strings():
    text = substitute(tree("len(vars.s) + 1"), {"vars.s": "abc{x}"})
    assert text == "len('abc{x}') + 1" and eval_expression(text, {}) == 7
    assert substitute(tree("len(vars.s)"), {"vars.s": "长" * 80}) == f"len('{'长' * 80}')"


def test_substitute_does_not_touch_the_original_tree():
    t = tree("vars.a + 1")
    substitute(t, {"vars.a": 5})
    assert ast.unparse(t) == "vars.a + 1"


# --------------------------------------------------------------------------
# 口径卡节点：真跑一遍
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


KPI = "{'gmv': 45678.5, 'gmv_prev': 42010.0, 'orders': 1234, 'refunds': 29, 'name': 'abc'}"
WOW = "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"


def kpi_graph(metrics, **card):
    return {
        "nodes": [
            node("start", "input"),
            node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
            node("card", "metrics", caliber="周报口径", caliber_version="v2", assign_to="m",
                 metrics=metrics, **card),
            node("out", "output", fields=[{"name": "清单", "value": "{{ nodes.card.text }}"}]),
        ],
        "edges": [{"source": "start", "target": "fetch"}, {"source": "fetch", "target": "card"},
                  {"source": "card", "target": "out"}],
    }


async def _finish(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row.status in statuses:
                return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def _events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def _card_of(run_id: str) -> dict:
    finished = await _events(run_id, "node.finished", "card")
    assert len(finished) == 1
    artifact = finished[0].data["artifact"]
    return artifact_store.load(artifact)


STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain", "expression": WOW},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
    {"id": "refund_rate", "name": "退款率", "decimals": 4, "format": "percent_of_ratio",
     "expression": "vars.kpi.refunds / vars.kpi.orders"},
]


async def test_each_metric_records_its_inputs_substitution_and_rendering(engine_up):
    run = await run_manager.start(graph=kpi_graph(STANDARD), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    card = await _card_of(run.id)
    by_id = {m["id"]: m for m in card["metrics"]}

    wow = by_id["wow"]
    assert wow["value"] == 8.7 and wow["status"] == "ok"
    assert wow["substituted"] == "round((45678.5 - 42010.0) / 42010.0 * 100, 1)"
    assert wow["recompute_ok"] is True
    assert wow["inputs"] == [
        {"path": "vars.kpi.gmv", "value": 45678.5, "node_id": "fetch", "via": "transform",
         "field": "gmv", "status": "ok"},
        {"path": "vars.kpi.gmv_prev", "value": 42010.0, "node_id": "fetch", "via": "transform",
         "field": "gmv_prev", "status": "ok"},
    ]
    assert (wow["decimals"], wow["format"], wow["rendered"]) == (1, "plain", "8.7%")
    assert by_id["gmv"]["rendered"] == "45,678.5元"
    # 没写 format 就是千分位；没写 decimals 就按值本来的样子
    assert (by_id["orders"]["format"], by_id["orders"]["decimals"], by_id["orders"]["rendered"]) \
        == ("thousands", None, "1,234单")
    assert by_id["refund_rate"]["value"] == 0.0235 and by_id["refund_rate"]["rendered"] == "2.35%"
    # 旧字段一个不少，text 照旧——模板 ⑧ 的叙述 prompt 靠它
    assert {"id", "name", "unit", "value", "expression"} <= set(wow)
    output = row.output["清单"]
    assert "- wow（环比增幅）= 8.7%" in output and "- orders（订单数）= 1234单" in output


async def test_metric_set_artifact_is_in_the_ledger_and_node_finished(engine_up):
    run = await run_manager.start(graph=kpi_graph(STANDARD), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    finished = (await _events(run.id, "node.finished", "card"))[0].data
    ledger = finished["evidence"]
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["kind"] == "metric_set" and entry["node_id"] == "card" and entry["exec"] == 1
    assert entry["caliber"] == "周报口径" and entry["version"] == "v2"
    assert entry["metrics"] == ["gmv", "wow", "orders", "refund_rate"]

    # 工件取回时复验哈希；内容就是整张卡，id 也写进了节点产出
    card = artifact_store.load(entry["artifact"])
    assert card["kind"] == "metric_set" and card["caliber_version"] == "v2"
    assert [m["id"] for m in card["metrics"]] == entry["metrics"]
    node_output = artifact_store.load(finished["artifact"])
    assert node_output["artifact"] == entry["artifact"]
    async with SessionLocal() as session:
        stored = await session.get(Artifact, (entry["artifact"], run.id))
    assert stored is not None and stored.kind == "metric_set" and stored.node_id == "card"

    # 不产出证据的节点，node.finished 和以前一模一样：不多一个空的 evidence 键
    for other in await _events(run.id, "node.finished"):
        if other.node_id != "card":
            assert "evidence" not in other.data, other.data


MISSING = [
    {"id": "gmv", "expression": "vars.kpi.gmv"},
    {"id": "margin", "name": "毛利率", "expression": "vars.kpi.cost / vars.kpi.gmv * 100"},
]


async def test_missing_input_fails_by_default_with_a_readable_reason(engine_up):
    """on_missing 默认 fail：和以前一样整个节点失败，只是报错要说人话。"""
    run = await run_manager.start(graph=kpi_graph(MISSING), input_payload={})
    row = await _finish(run.id)
    assert row.status == "failed"
    assert "margin" in row.error and "vars.kpi.cost" in row.error and "没有值" in row.error, row.error
    for raw in ("TypeError", "NoneType", "unsupported operand"):
        assert raw not in row.error, row.error


async def test_missing_input_becomes_null_when_asked(engine_up):
    run = await run_manager.start(graph=kpi_graph(MISSING, on_missing="null"), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    by_id = {m["id"]: m for m in (await _card_of(run.id))["metrics"]}
    margin = by_id["margin"]
    assert margin["value"] is None and margin["status"] == "missing_input"
    assert margin["rendered"] == "—" and margin["recompute_ok"] is None
    assert [i["path"] for i in margin["inputs"] if i["status"] == "missing"] == ["vars.kpi.cost"]
    assert by_id["gmv"]["status"] == "ok" and by_id["gmv"]["value"] == 45678.5


async def test_type_errors_are_explained_not_crashed_on(engine_up):
    run = await run_manager.start(
        graph=kpi_graph([{"id": "x", "expression": "vars.kpi.name * 1.5"}], on_missing="null"),
        input_payload={})
    row = await _finish(run.id)
    # 类型错误不是缺输入：null 模式也不替它兜成空值
    assert row.status == "failed"
    assert "x" in row.error and "vars.kpi.name" in row.error and "类型" in row.error, row.error
    for raw in ("TypeError", "can't multiply", "执行出错"):
        assert raw not in row.error, row.error


@pytest.mark.parametrize("bad, words", [
    ({"format": "percent"}, ["format", "plain", "thousands", "percent_of_ratio"]),
    ({"decimals": "两位"}, ["decimals", "整数"]),
])
async def test_bad_display_config_is_reported(engine_up, bad, words):
    run = await run_manager.start(
        graph=kpi_graph([{"id": "gmv", "expression": "vars.kpi.gmv", **bad}]), input_payload={})
    row = await _finish(run.id)
    assert row.status == "failed"
    assert all(w in row.error for w in words), row.error


async def test_brace_strings_do_not_fake_a_recompute_mismatch(engine_up):
    run = await run_manager.start(graph=kpi_graph([{"id": "n", "expression": "len(input.memo) + 1"}]),
                                  input_payload={"memo": "a{{b}}c"})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    n = (await _card_of(run.id))["metrics"][0]
    assert n["value"] == 8 and n["recompute_ok"] is True, n
    assert n["substituted"] == "len(input.memo) + 1"


# --------------------------------------------------------------------------
# 旧工作流：以前能跑的口径卡，现在照样能跑，值一个不差
# --------------------------------------------------------------------------


@pytest.mark.parametrize("definition, value, rendered", [
    # 负的小数位是 round 到十位、百位——以前 round(value, int(decimals)) 就支持
    ({"decimals": -2, "unit": "元"}, 45700.0, "45,700元"),
    ({"decimals": "-2", "unit": "元"}, 45700.0, "45,700元"),
    ({"decimals": 12}, 45678.5, "45,678.500000000000"),
    ({"decimals": "2"}, 45678.5, "45,678.50"),
])
async def test_legacy_decimals_keep_working(engine_up, definition, value, rendered):
    run = await run_manager.start(
        graph=kpi_graph([{"id": "gmv", "expression": "vars.kpi.gmv", **definition}]), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    gmv = (await _card_of(run.id))["metrics"][0]
    assert gmv["value"] == value and gmv["rendered"] == rendered and gmv["status"] == "ok"


async def test_legacy_non_integer_decimals_on_an_integer_value_are_ignored_as_before(engine_up):
    """以前只有值是小数时才用到 decimals，整数值上写错了也照样跑完。"""
    run = await run_manager.start(
        graph=kpi_graph([{"id": "orders", "unit": "单", "expression": "vars.kpi.orders",
                          "decimals": "两位"}]), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    orders = (await _card_of(run.id))["metrics"][0]
    assert orders["value"] == 1234 and orders["decimals"] is None and orders["rendered"] == "1,234单"


@pytest.mark.parametrize("definition, words", [
    ({"format": "thousands", "decimals": 16}, ["decimals", "-15", "15"]),
    ({"format": "plain", "decimals": 1.5}, ["decimals", "整数"]),
    ({"format": "plain", "decimals": True}, ["decimals", "整数"]),
])
async def test_new_style_decimals_are_checked_strictly(engine_up, definition, words):
    run = await run_manager.start(
        graph=kpi_graph([{"id": "gmv", "expression": "vars.kpi.gmv", **definition}]), input_payload={})
    row = await _finish(run.id)
    assert row.status == "failed"
    assert all(w in row.error for w in words), row.error


async def test_huge_values_do_not_crash_the_card(engine_up):
    """以前能算出 4.6e54 的口径卡照样成功；渲染出来是完整的数字，不是崩在 InvalidOperation 上。
    （超过 64 位的整数进不了断点存档，这一点新旧代码一样，不在这里测。）"""
    run = await run_manager.start(
        graph=kpi_graph([{"id": "big", "expression": "vars.kpi.gmv * 1e50"},
                         {"id": "tiny", "expression": "vars.kpi.gmv * 1e-20"}]), input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    by_id = {m["id"]: m for m in (await _card_of(run.id))["metrics"]}
    big = by_id["big"]
    assert big["rendered"] == f"{int(Decimal(repr(big['value']))):,}" and big["rendered"].startswith("4,567,85")
    # 小到按默认 10 位小数显示成 0 的：不给假的 0，显示「—」，值照旧记着
    assert by_id["tiny"]["rendered"] == "—" and by_id["tiny"]["value"] == 45678.5 * 1e-20


# --------------------------------------------------------------------------
# 旧 checkpoint 里没有 evidence 通道：续跑时视为空
# --------------------------------------------------------------------------


async def test_resume_from_a_checkpoint_without_the_evidence_channel(engine_up):
    graph = {
        "nodes": [
            node("start", "input"),
            node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
            node("first", "metrics", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
            node("gate", "human", mode="approve", title="确认"),
            node("second", "metrics", metrics=[{"id": "orders", "expression": "vars.kpi.orders"}]),
            node("out", "output", fields=[{"name": "r", "value": "{{ nodes.second.text }}"}]),
        ],
        "edges": [{"source": a, "target": b} for a, b in [
            ("start", "fetch"), ("fetch", "first"), ("first", "gate"), ("gate", "second"),
            ("second", "out")]],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    await _finish(run.id, ("interrupted", "failed", "succeeded"))

    # 把断点改写成旧版引擎写出来的样子：通道表里没有 evidence
    saver = run_manager.checkpointer
    config = {"configurable": {"thread_id": run.id}}
    tup = await saver.aget_tuple(config)
    assert tup.checkpoint["channel_values"].get("evidence"), "前一张口径卡的台账应该已经在断点里"
    checkpoint = {**tup.checkpoint,
                  "channel_values": {k: v for k, v in tup.checkpoint["channel_values"].items()
                                     if k != "evidence"},
                  "channel_versions": {k: v for k, v in tup.checkpoint["channel_versions"].items()
                                       if k != "evidence"}}
    await saver.aput(tup.parent_config, checkpoint, tup.metadata, {})
    reread = await saver.aget_tuple(config)
    assert "evidence" not in reread.checkpoint["channel_values"]

    await run_manager.resume(run.id, {"approved": True})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    second = (await _events(run.id, "node.finished", "second"))[0].data
    assert [e["node_id"] for e in second["evidence"]] == ["second"]
    assert second["evidence"][0]["exec"] == 1
    # 断点里丢掉的那条不会凭空回来：通道从空列表起步，之后照常追加
    assert [e["node_id"] for e in await _ledger(run.id)] == ["second"]


async def _ledger(run_id: str) -> list[dict]:
    tup = await run_manager.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
    return list(tup.checkpoint["channel_values"].get("evidence") or [])


async def test_ledger_accumulates_across_nodes_and_parallel_branches(engine_up):
    """台账只追加：两张口径卡并行跑、之后再跑一张，三条都在，顺序就是写入顺序。"""
    graph = {
        "nodes": [
            node("start", "input"),
            node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
            node("east", "metrics", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
            node("west", "metrics", metrics=[{"id": "orders", "expression": "vars.kpi.orders"}]),
            node("total", "metrics", metrics=[{"id": "refunds", "expression": "vars.kpi.refunds"}]),
            node("out", "output", fields=[{"name": "r", "value": "{{ nodes.total.text }}"}]),
        ],
        "edges": [{"source": a, "target": b} for a, b in [
            ("start", "fetch"), ("fetch", "east"), ("fetch", "west"), ("east", "total"),
            ("west", "total"), ("total", "out")]],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    row = await _finish(run.id)
    assert row.status == "succeeded", row.error
    ledger = await _ledger(run.id)
    assert sorted(e["node_id"] for e in ledger[:2]) == ["east", "west"]
    assert [e["node_id"] for e in ledger[2:]] == ["total"]
    assert all(e["kind"] == "metric_set" and e["artifact"] for e in ledger)
