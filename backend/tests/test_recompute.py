"""数字推演的重算（engine/recompute.py）：纯函数，口径卡怎么算的这里就怎么算。

卡、查询快照、节点产出都是照 metrics.py 的形状手写的虚构数据；工件库换成一个字典。守的是：
- 不改任何输入时，每个能还原的指标重算出来和卡上记的一模一样
- 改输入、排除快照里的行，结果和手算一致；排除了 cell() 那一格所在的行，指标标「排除后无法计算」
- 容器输入的还原：agent 数组字段（按 locator.rows 去掉被排除的行）、nodes.* 从节点产出、vars.* 只认 value_artifact
- 遮罩列取来的输入既不给值、也不能拿来推演
- 不合法的请求抛 WhatIfError（接口回 422），说清哪里不对
"""
from __future__ import annotations

import math

import pytest

from app.engine.recompute import Changes, Sources, WhatIfError, downstream_cards, recompute, report_segments, whatif

S1, S2, BAD = "a1" * 32, "b2" * 32, "c3" * 32
SNAPSHOTS = {
    S1: {"columns": ["region", "amount", "phone"], "source": "shop", "sql": "SELECT region, amount, phone FROM sales",
         "rows": [["华东", 120.5, "p1"], ["华南", 80.0, "p2"], ["华北", 99.5, "p3"], ["西南", 40.0, "p4"]]},
    S2: {"columns": ["store", "phone"], "source": "crm", "sql": "SELECT store, phone FROM stores",
         "rows": [["一号店", "13800000000"], ["二号店", "13900000000"]]},
}
BOT_OUT = "d4" * 32
FETCH_OUT = "e5" * 32
ITEMS_VALUE = "f6" * 32
RES_ROWS = "a7" * 32
STORE = {
    **SNAPSHOTS,
    BOT_OUT: {"text": "四个区域合计 340", "data": {"amounts": [120.5, 80.0, 99.5, 40.0]},
              "data_evidence": {"amounts": {"ref": "Q1.r0-3.amount", "call": "Q1", "artifact": S1,
                                            "locator": {"rows": [0, 3], "column": "amount"}, "status": "verified"}}},
    FETCH_OUT: {"items": ["甲", "乙", "丙"], "k": 2},
    ITEMS_VALUE: {"path": "vars.kpi.items", "value": ["甲", "乙", "丙"]},
    RES_ROWS: {"path": "vars.res.rows", "value": SNAPSHOTS[S1]["rows"]},
}


def load(artifact):
    if artifact == BAD:
        raise ValueError("工件内容与哈希不符，疑似被篡改")
    return STORE.get(artifact)


def sources(**kw):
    base = {"load": load, "queries": (S1, S2, BAD), "outputs": {"bot": BOT_OUT, "fetch": FETCH_OUT},
            "masked": {S2: frozenset({"phone"})}}
    return Sources(**{**base, **kw})


def scalar(path, value, node="fetch", via="transform"):
    return {"path": path, "value": value, "node_id": node, "via": via, "field": path.split(".", 2)[-1], "status": "ok"}


def cell(node, row, column, value, artifact=S1):
    return {"path": f"cell(nodes.{node}, {row}, '{column}')", "value": value, "node_id": node, "via": "tool_cell",
            "locator": {"row": row, "column": column}, "artifact": artifact, "eid": "ev:cell:x", "status": "verified"}


AMOUNTS = {"path": "vars.kpi.amounts", "value": "[4 项]", "node_id": "bot", "via": "agent_field", "field": "amounts",
           "ref": "Q1.r0-3.amount", "artifact": S1, "locator": {"rows": [0, 3], "column": "amount"},
           "eid": "ev:rows:x", "status": "verified"}


def metric(mid, expression, substituted, value, inputs, *, decimals=None, fmt="thousands", unit="", rendered=None):
    from app.engine.evidence import render_number

    return {"id": mid, "name": mid, "unit": unit, "value": value, "expression": expression, "decimals": decimals,
            "format": fmt, "rendered": rendered if rendered is not None else render_number(
                value, unit=unit, decimals=decimals, fmt=fmt),
            "inputs": inputs, "substituted": substituted, "recompute_ok": True, "status": "ok"}


WOW = "round((vars.kpi.gmv - vars.kpi.prev) / vars.kpi.prev * 100, 1)"
CARD = {"kind": "metric_set", "caliber": "推演口径", "caliber_version": "v3", "metrics": [
    metric("wow", WOW, "round((45678.5 - 42010.0) / 42010.0 * 100, 1)", 8.7,
           [scalar("vars.kpi.gmv", 45678.5), scalar("vars.kpi.prev", 42010.0)], decimals=1, fmt="plain", unit="%"),
    metric("total", "sum(vars.kpi.amounts)", "sum(vars.kpi.amounts)", 340.0, [AMOUNTS], decimals=1, unit="元"),
    metric("east", "cell(nodes.pull, 0, 'amount')", "120.5", 120.5, [cell("pull", 0, "amount", 120.5)],
           decimals=1, unit="元"),
    metric("share", "round(cell(nodes.pull, 0, 'amount') / sum(vars.kpi.amounts) * 100, 1)",
           "round(120.5 / sum(vars.kpi.amounts) * 100, 1)", 35.4, [cell("pull", 0, "amount", 120.5), AMOUNTS],
           decimals=1, fmt="plain", unit="%"),
    metric("ratio", "vars.kpi.refunds / vars.kpi.orders", "29 / 1234", 0.0235,
           [scalar("vars.kpi.refunds", 29), scalar("vars.kpi.orders", 1234)], decimals=4, fmt="percent_of_ratio"),
    # 老运行：容器型 vars 输入没有 value_artifact
    metric("n", "len(vars.kpi.items) * vars.kpi.k", "len(vars.kpi.items) * 2", 6,
           [scalar("vars.kpi.items", "[3 项]"), scalar("vars.kpi.k", 2)]),
    # 新运行：有 value_artifact
    metric("n2", "len(vars.kpi.items) * vars.kpi.k", "len(vars.kpi.items) * 2", 6,
           [{**scalar("vars.kpi.items", "[3 项]"), "value_artifact": ITEMS_VALUE}, scalar("vars.kpi.k", 2)]),
    # nodes.* 容器从节点产出还原
    metric("n3", "len(nodes.fetch.items) + vars.kpi.k", "len(nodes.fetch.items) + 2", 5,
           [{**scalar("nodes.fetch.items", "[3 项]"), "field": "items"}, scalar("vars.kpi.k", 2)]),
    # 整份查询结果赋给了变量：排除行就去掉那几行
    metric("rows", "len(vars.res.rows) * vars.kpi.k", "len(vars.res.rows) * 2", 8,
           [{**scalar("vars.res.rows", "[4 项]", node="parse"), "value_artifact": RES_ROWS}, scalar("vars.kpi.k", 2)]),
    # 取自遮罩列
    metric("phone_len", "len(cell(nodes.crm, 0, 'phone'))", "len('13800000000')", 11,
           [cell("crm", 0, "phone", "13800000000", artifact=S2)]),
    # 下标里的引用决定取哪一格
    metric("pick", "vars.kpi.list[vars.kpi.i] + 1", "20 + 1", 21,
           [scalar("vars.kpi.list[vars.kpi.i]", 20), scalar("vars.kpi.i", 1)]),
]}


def by_id(result):
    return {m["id"]: m for m in result["metrics"]}


def one(mid, changes=Changes(), **kw):
    found = next(m for m in CARD["metrics"] if m["id"] == mid)
    return recompute(found, changes, sources(**kw))


# --------------------------------------------------------------------------
# 不改输入：还原是忠实的
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mid", ["wow", "total", "east", "share", "ratio", "n2", "n3", "rows", "pick"])
def test_unchanged_inputs_reproduce_the_card(mid):
    out = one(mid)
    old = next(m for m in CARD["metrics"] if m["id"] == mid)
    assert out["status"] == "ok", out
    assert out["new"] == old["value"] and out["rendered_new"] == old["rendered"]
    assert out["changed"] is False and out["delta"] == 0


def test_an_old_run_container_cannot_be_restored():
    out = one("n")
    assert out["status"] == "unsupported" and out["new"] is None and out["rendered_new"] is None
    assert "当时没有保存这个输入的完整值" in out["reason"] and "vars.kpi.items" in out["reason"]


def test_a_baseline_lists_what_cannot_be_used_without_recomputing_anything():
    result = whatif(CARD, Changes(), sources())
    metrics = by_id(result)
    assert all(m["status"] == "unchanged" and m["new"] == m["old"] and m["changed"] is False
               for m in result["metrics"])
    unsupported = {(u["metric"], u["input"]): u["reason"] for u in result["unsupported"]}
    assert "完整值" in unsupported[("n", "vars.kpi.items")]
    assert "遮罩" in unsupported[("phone_len", "cell(nodes.crm, 0, 'phone')")]
    assert "取哪" in unsupported[("pick", "vars.kpi.i")]
    assert ("n2", "vars.kpi.items") not in unsupported and ("n3", "nodes.fetch.items") not in unsupported
    # 遮罩列的值不给
    [phone] = metrics["phone_len"]["inputs"]
    assert phone["masked"] is True and phone["old"] is None and phone["new"] is None and phone["editable"] is False
    # 能改的只有数值标量
    editable = {i["path"]: i["editable"] for i in metrics["wow"]["inputs"] + metrics["total"]["inputs"]}
    assert editable == {"vars.kpi.gmv": True, "vars.kpi.prev": True, "vars.kpi.amounts": False}


# --------------------------------------------------------------------------
# 改输入、排除行：和手算一致
# --------------------------------------------------------------------------


def test_changing_an_input_recomputes_only_the_metrics_that_use_it():
    result = whatif(CARD, Changes(inputs={"vars.kpi.gmv": 50000}), sources())
    metrics = by_id(result)
    wow = metrics["wow"]
    assert wow["status"] == "ok" and wow["new"] == round((50000 - 42010.0) / 42010.0 * 100, 1) == 19.0
    assert wow["old"] == 8.7 and wow["changed"] is True and wow["delta"] == 10.3
    assert (wow["rendered_old"], wow["rendered_new"], wow["rendered_delta"]) == ("8.7%", "19.0%", "+10.3 个百分点")
    assert wow["substituted"] == "round((50000 - 42010.0) / 42010.0 * 100, 1)"
    gmv = next(i for i in wow["inputs"] if i["path"] == "vars.kpi.gmv")
    assert (gmv["old"], gmv["new"], gmv["changed"]) == (45678.5, 50000, True)
    assert all(m["status"] == "unchanged" for k, m in metrics.items() if k != "wow")


def test_excluding_rows_drops_them_from_an_agent_array_field():
    result = whatif(CARD, Changes(exclude={S1: frozenset({1})}), sources())
    metrics = by_id(result)
    total = metrics["total"]
    assert total["new"] == 340.0 - 80.0 and total["rendered_new"] == "260.0元" and total["delta"] == -80.0
    assert total["rendered_delta"] == "-80.0元"
    [amounts] = total["inputs"]
    assert amounts["excluded_rows"] == [1] and (amounts["old"], amounts["new"]) == ("[4 项]", "[3 项]")
    assert metrics["share"]["new"] == round(120.5 / 260.0 * 100, 1) == 46.3
    # 第 0 行没排除：cell 取的那一格不受影响
    assert metrics["east"]["status"] == "unchanged"
    # 整份结果赋给变量的，同一行也去掉
    assert metrics["rows"]["new"] == 3 * 2
    assert next(i for i in metrics["rows"]["inputs"] if i["path"] == "vars.res.rows")["excluded_rows"] == [1]


def test_excluding_the_row_of_a_cell_makes_the_metric_uncomputable():
    result = whatif(CARD, Changes(exclude={S1: frozenset({0, 3})}), sources())
    metrics = by_id(result)
    east = metrics["east"]
    assert east["status"] == "excluded" and east["new"] is None and east["rendered_new"] == "—"
    assert "排除后无法计算" in east["reason"] and "第 1 行" in east["reason"]
    [inp] = east["inputs"]
    assert inp["new"] is None and inp["excluded_rows"] == [0]
    assert metrics["share"]["status"] == "excluded"
    assert metrics["total"]["new"] == 80.0 + 99.5


def test_an_explicit_value_wins_over_an_excluded_row():
    path = "cell(nodes.pull, 0, 'amount')"
    result = whatif(CARD, Changes(inputs={path: 100}, exclude={S1: frozenset({0})}), sources())
    east = by_id(result)["east"]
    assert east["status"] == "ok" and east["new"] == 100 and east["rendered_new"] == "100.0元"


def test_a_ratio_moves_in_percentage_points():
    ratio = by_id(whatif(CARD, Changes(inputs={"vars.kpi.refunds": 37}), sources()))["ratio"]
    assert ratio["new"] == round(37 / 1234, 4) == 0.03
    assert (ratio["rendered_old"], ratio["rendered_new"], ratio["rendered_delta"]) == ("2.35%", "3.00%", "+0.65 个百分点")


def test_division_by_zero_is_explained():
    wow = by_id(whatif(CARD, Changes(inputs={"vars.kpi.prev": 0}), sources()))["wow"]
    assert wow["status"] == "error" and wow["new"] is None and wow["reason"] == "除以零"


def test_a_missing_input_can_be_supplied():
    card = {**CARD, "metrics": [metric("cost", "vars.kpi.cost / vars.kpi.orders", "vars.kpi.cost / 1234", None,
                                       [scalar("vars.kpi.cost", None), scalar("vars.kpi.orders", 1234)], decimals=2,
                                       rendered="—")]}
    cost = whatif(card, Changes(inputs={"vars.kpi.cost": 2468}), sources())["metrics"][0]
    assert cost["old"] is None and cost["new"] == 2.0 and cost["rendered_old"] == "—" and cost["rendered_new"] == "2.00"


# --------------------------------------------------------------------------
# 容器输入的还原
# --------------------------------------------------------------------------


def test_new_runs_restore_vars_containers_from_the_value_artifact():
    metrics = by_id(whatif(CARD, Changes(inputs={"vars.kpi.k": 5}), sources()))
    assert metrics["n2"]["status"] == "ok" and metrics["n2"]["new"] == 15
    assert metrics["n3"]["status"] == "ok" and metrics["n3"]["new"] == 3 + 5
    assert metrics["rows"]["new"] == 4 * 5
    # 老运行的那个照实说做不到
    assert metrics["n"]["status"] == "unsupported" and "完整值" in metrics["n"]["reason"]


def test_a_tampered_or_missing_full_value_is_not_used():
    metrics = by_id(whatif(CARD, Changes(inputs={"vars.kpi.k": 5}), sources(
        load=lambda a: (_ for _ in ()).throw(ValueError("篡改")) if a == ITEMS_VALUE else load(a))))
    assert metrics["n2"]["status"] == "unsupported" and "哈希" in metrics["n2"]["reason"]
    metrics = by_id(whatif(CARD, Changes(inputs={"vars.kpi.k": 5}), sources(outputs={})))
    assert metrics["n3"]["status"] == "unsupported" and "nodes.fetch.items" in metrics["n3"]["reason"]


def test_an_agent_array_is_restored_from_the_node_output_and_the_snapshot_rows():
    """老运行没有 value_artifact：agent 的数组字段按封存的节点产出还原，行号按 locator.rows 对到快照。"""
    out = one("total", Changes(exclude={S1: frozenset({2, 3})}))
    assert out["new"] == 120.5 + 80.0 and out["inputs"][0]["excluded_rows"] == [2, 3]


# --------------------------------------------------------------------------
# 遮罩
# --------------------------------------------------------------------------


def test_masked_inputs_cannot_drive_a_whatif():
    with pytest.raises(WhatIfError, match="遮罩"):
        whatif(CARD, Changes(inputs={"cell(nodes.crm, 0, 'phone')": 1}), sources())
    phone = by_id(whatif(CARD, Changes(exclude={S2: frozenset({0})}), sources()))["phone_len"]
    assert phone["status"] == "masked" and phone["new"] is None and phone["rendered_new"] is None
    assert "遮罩" in phone["reason"]
    assert all(i["old"] is None and i["new"] is None for i in phone["inputs"])


def test_a_query_result_with_a_masked_column_is_masked_as_a_whole():
    card = {**CARD, "metrics": [metric("cnt", "len(vars.res.rows) * vars.kpi.k", "len(vars.res.rows) * 2", 8,
                                       [{**scalar("vars.res.rows", "[4 项]"), "value_artifact": RES_ROWS},
                                        scalar("vars.kpi.k", 2)])]}
    masked = sources(masked={S1: frozenset({"phone"})})
    baseline = whatif(card, Changes(), masked)
    assert [(u["input"], "遮罩" in u["reason"]) for u in baseline["unsupported"]] == [("vars.res.rows", True)]
    cnt = whatif(card, Changes(inputs={"vars.kpi.k": 3}), masked)["metrics"][0]
    assert cnt["status"] == "masked" and cnt["new"] is None


# --------------------------------------------------------------------------
# 不合法的请求
# --------------------------------------------------------------------------


@pytest.mark.parametrize("changes, words", [
    (Changes(inputs={"vars.kpi.nope": 1}), ["vars.kpi.nope"]),
    (Changes(inputs={"vars.kpi.gmv": "很多"}), ["vars.kpi.gmv", "数值"]),
    (Changes(inputs={"vars.kpi.gmv": True}), ["数值"]),
    (Changes(inputs={"vars.kpi.gmv": math.nan}), ["数值"]),
    (Changes(inputs={"vars.kpi.gmv": math.inf}), ["数值"]),
    (Changes(inputs={"vars.kpi.amounts": 3}), ["列表", "排除"]),
    (Changes(inputs={"vars.kpi.i": 0}), ["取哪"]),
    (Changes(exclude={"f" * 64: frozenset({0})}), ["不属于这次运行", "封存范围"]),
    (Changes(exclude={S1: frozenset({4})}), ["行号 4", "4 行"]),
    (Changes(exclude={S1: frozenset({-1})}), ["行号"]),
    (Changes(exclude={BAD: frozenset({0})}), ["哈希"]),
])
def test_invalid_requests_say_what_is_wrong(changes, words):
    with pytest.raises(WhatIfError) as caught:
        whatif(CARD, changes, sources())
    assert all(w in str(caught.value) for w in words), str(caught.value)


def test_text_inputs_cannot_be_changed():
    card = {**CARD, "metrics": [metric("len", "len(vars.kpi.name) + vars.kpi.k", "len('华东区') + 2", 5,
                                       [scalar("vars.kpi.name", "华东区"), scalar("vars.kpi.k", 2)])]}
    with pytest.raises(WhatIfError, match="不是数值"):
        whatif(card, Changes(inputs={"vars.kpi.name": 3}), sources())
    [name, k] = whatif(card, Changes(), sources())["metrics"][0]["inputs"]
    assert name["editable"] is False and name["kind"] == "text" and k["editable"] is True


def test_a_single_metric_is_checked_against_its_own_inputs():
    with pytest.raises(WhatIfError, match="vars.kpi.amounts"):
        whatif(CARD, Changes(inputs={"vars.kpi.amounts": 1}), sources(), metric="wow")
    result = whatif(CARD, Changes(inputs={"vars.kpi.gmv": 50000}), sources(), metric="wow")
    assert [m["id"] for m in result["metrics"]] == ["wow"]


def test_a_card_from_before_lineage_cannot_be_recomputed():
    legacy = {"id": "old", "name": "旧指标", "unit": "", "value": 3, "expression": "vars.x + 1"}
    out = recompute(legacy, Changes(), sources())
    assert out["status"] == "unsupported" and "没有记录输入" in out["reason"]


# --------------------------------------------------------------------------
# 报告里引用的地方、下游口径卡
# --------------------------------------------------------------------------


def doc_with(*segments):
    catalog = {
        "m:wow": {"alias": "m:wow", "kind": "metric", "node_id": "card", "artifact": "CARD", "locator": {"metric": "wow"}},
        "m:total": {"alias": "m:total", "kind": "metric", "node_id": "card", "artifact": "CARD",
                    "locator": {"metric": "total"}},
        "m:other": {"alias": "m:other", "kind": "metric", "node_id": "card", "artifact": "OLDER",
                    "locator": {"metric": "wow"}},
    }
    return {"catalog": catalog, "blocks": [{"id": "b1", "type": "paragraph", "units": [
        {"id": "u1", "kind": "claim", "segments": list(segments)}]}]}


def seg(sid, text, alias, conv=None):
    cite = {"alias": alias, "kind": "metric", "status": "resolved", **({"conv": conv} if conv else {})}
    return {"id": sid, "text": text, "kind": "value", "state": "deterministic", "cite": cite}


def test_report_segments_render_the_new_value_the_way_the_report_does():
    result = whatif(CARD, Changes(inputs={"vars.kpi.gmv": 50000}), sources())
    doc = doc_with(seg("s1", "8.7%", "m:wow"), seg("s2", "340.0元", "m:total"),
                   seg("s3", "8.7%", "m:other"), {"id": "s4", "text": "本周", "kind": "text", "state": "neutral"})
    found = report_segments([("write", doc)], card_node="card", card_artifact="CARD", results=result["metrics"])
    assert found == [
        {"report": "write", "unit": "u1", "sid": "s1", "metric": "wow", "rendered_old": "8.7%", "rendered_new": "19.0%",
         "changed": True},
        {"report": "write", "unit": "u1", "sid": "s2", "metric": "total", "rendered_old": "340.0元",
         "rendered_new": "340.0元", "changed": False},
    ]
    # 报告里写了换算的，按同样的换算渲染
    total = whatif(CARD, Changes(exclude={S1: frozenset({0})}), sources())
    [one_seg] = report_segments([("write", doc_with(seg("s9", "0.03万元", "m:total", conv="万")))], card_node="card",
                                card_artifact="CARD", results=total["metrics"])
    assert (one_seg["rendered_old"], one_seg["rendered_new"]) == ("0.03万元", "0.02万元")


def test_downstream_cards_that_use_this_card_are_flagged_not_recomputed():
    upstream = ("card", "A1", CARD)
    yearly = ("yearly", "B1", {"caliber": "年报口径", "caliber_version": "v1", "metrics": [
        metric("y", "vars.m.metrics[0].value * 52", "8.7 * 52", 452.4,
               [{"path": "vars.m.metrics[0].value", "value": 8.7, "node_id": "card", "via": "metric", "status": "ok"}]),
        metric("z", "vars.kpi.k", "2", 2, [scalar("vars.kpi.k", 2)])]})
    board = ("board", "C1", {"caliber": "看板口径", "caliber_version": "v2", "metrics": [
        metric("b", "nodes.yearly.metrics[0].value + 1", "452.4 + 1", 453.4,
               [{"path": "nodes.yearly.metrics[0].value", "value": 452.4, "node_id": "yearly", "via": "metric",
                 "status": "ok"}])]})
    unrelated = ("misc", "D1", {"caliber": "其他", "caliber_version": "v1", "metrics": [
        metric("q", "vars.kpi.k", "2", 2, [scalar("vars.kpi.k", 2)])]})
    assert downstream_cards([upstream, yearly, board, unrelated], "card") == [
        {"node_id": "yearly", "artifact": "B1", "caliber": "年报口径", "version": "v1", "metrics": ["y"], "direct": True},
        {"node_id": "board", "artifact": "C1", "caliber": "看板口径", "version": "v2", "metrics": ["b"], "direct": False},
    ]
