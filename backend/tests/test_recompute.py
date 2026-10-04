"""数字推演的重算（engine/recompute.py）：纯函数，口径卡怎么算的这里就怎么算。

卡、查询快照、节点产出都是照 metrics.py 的形状手写的虚构数据；工件库换成一个字典。守的是：
- 不改任何输入时，每个能还原的指标重算出来和卡上记的一模一样
- 改输入、排除快照里的行，结果和手算一致；排除了 cell() 那一格所在的行，指标标「排除后无法计算」
- 容器输入的还原：agent 数组字段（按 locator.rows 去掉被排除的行）、nodes.* 从节点产出、vars.* 只认 value_artifact
- 遮罩列取来的输入既不给值、也不能拿来推演
- 整个指标推演不了的（遮罩列、还原不了的输入、读不出来的快照、没记输入），不改动也照实说，输入一个都不让动
- 排除了行却说不准受不受影响的指标，不报「无变化」
- 不合法的请求抛 WhatIfError（接口回 422），说清哪里不对
"""
from __future__ import annotations

import json
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
    # 推演得了的指标原样交回；整个推演不了的（老运行没存完整值的容器、遮罩列）不改动也照实说，不报「无变化」
    assert all(m["status"] == "unchanged" and m["new"] == m["old"] and m["changed"] is False
               for k, m in metrics.items() if k not in ("n", "phone_len"))
    assert [(metrics[k]["status"], metrics[k]["changed"], metrics[k]["rendered_new"]) for k in ("n", "phone_len")] == [
        ("unsupported", None, None), ("masked", None, None)]
    assert result["maybe_changed"] is False
    unsupported = {(u["metric"], u["input"]): u["reason"] for u in result["unsupported"]}
    assert "完整值" in unsupported[("n", "vars.kpi.items")]
    assert "遮罩" in unsupported[("phone_len", "cell(nodes.crm, 0, 'phone')")]
    assert "取哪" in unsupported[("pick", "vars.kpi.i")]
    # 整个指标推演不了的另有一条 input 为空的：界面据此只写原因
    assert unsupported[("n", None)] == metrics["n"]["reason"]
    assert unsupported[("phone_len", None)] == metrics["phone_len"]["reason"]
    assert {m for m, i in unsupported if i is None} == {"n", "phone_len"}
    # 还原得出的容器不算「完整值找不回来」，只是不能直接改、也没有行可勾
    assert "完整值" not in unsupported[("n2", "vars.kpi.items")] and "完整值" not in unsupported[("n3", "nodes.fetch.items")]
    # 遮罩列的值不给
    [phone] = metrics["phone_len"]["inputs"]
    assert phone["masked"] is True and phone["old"] is None and phone["new"] is None and phone["editable"] is False
    # 代入式里代进去的就是那一格：同样不给
    assert metrics["phone_len"]["substituted"] is None and "13800000000" not in str(metrics["phone_len"])
    # 能改的只有数值标量；整个推演不了的指标里，本来能改的数也不让改
    editable = {i["path"]: i["editable"] for i in metrics["wow"]["inputs"] + metrics["total"]["inputs"]}
    assert editable == {"vars.kpi.gmv": True, "vars.kpi.prev": True, "vars.kpi.amounts": False}
    assert [i["editable"] for i in metrics["n"]["inputs"]] == [False, False]


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
    # 别的指标没碰到：原样交回（整个推演不了的照旧说推演不了）
    assert {k: m["status"] for k, m in metrics.items() if k != "wow" and m["status"] != "unchanged"} == {
        "n": "unsupported", "phone_len": "masked"}


def test_excluding_rows_drops_them_from_an_agent_array_field():
    # vars.kpi.k 是 fetch 节点在快照交回之前写的：排除行影响不到它（不知道先后时说不准，见下面几条）
    result = whatif(CARD, Changes(exclude={S1: frozenset({1})}), sources(returned={S1: 10}, finished={"fetch": 5}))
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


def test_an_overflow_is_an_error_not_a_crash():
    """算出无穷大：说清算不出来；老卡上记着无穷大的也不往外送（JSON 放不下，接口会 500）。"""
    result = whatif(CARD, Changes(inputs={"vars.kpi.refunds": 1e308, "vars.kpi.orders": 1e-10}), sources(),
                    metric="ratio")
    [ratio] = result["metrics"]
    assert ratio["status"] == "error" and ratio["new"] is None and "有限" in ratio["reason"]
    card = {**CARD, "metrics": [metric("big", "vars.kpi.x * 2", "inf * 2", math.inf,
                                       [scalar("vars.kpi.x", math.inf)], rendered="—")]}
    big = whatif(card, Changes(), sources())["metrics"][0]
    assert big["old"] is None and big["inputs"][0]["old"] is None and big["rendered_old"] == "—"
    json.dumps([result, big], allow_nan=False)


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
    # 整个指标一条，再列出卡住它的那个输入；本来能改的 vars.kpi.k 不再单列
    assert [(u["input"], "遮罩" in u["reason"]) for u in baseline["unsupported"]] == [
        (None, True), ("vars.res.rows", True)]
    assert baseline["metrics"][0]["status"] == "masked"
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


# --------------------------------------------------------------------------
# 排除行：说不准受不受影响的指标不能报「无变化」
# --------------------------------------------------------------------------


def lone(mid, expression, substituted, value, inputs, **kw):
    """只有一个指标的卡（同一套快照、产出）。"""
    return {**CARD, "metrics": [metric(mid, expression, substituted, value, inputs, **kw)]}


#: 老运行：cell() 一格加一个没存完整值的 vars 容器（写它的是 parse 节点）
OLD_MIX = lone("mix", "round(cell(nodes.pull, 0, 'amount') / len(vars.res.rows), 2)",
               "round(120.5 / len(vars.res.rows), 2)", 30.13,
               [cell("pull", 0, "amount", 120.5), scalar("vars.res.rows", "[4 项]", node="parse")], decimals=2)


def test_excluding_rows_never_reports_an_unrestorable_metric_as_unchanged():
    """排除的是 cell 那一格以外的行：容器的完整值找不回来，它会不会少几行说不准，指标不能报「无变化」。"""
    [mix] = whatif(OLD_MIX, Changes(exclude={S1: frozenset({2})}), sources())["metrics"]
    assert mix["status"] == "unsupported" and mix["changed"] is None and mix["rendered_new"] is None
    assert mix["new"] is None and "vars.res.rows" in mix["reason"] and "完整值" in mix["reason"]
    # 不改动也一样：完整值找不回来，这个指标本来就推演不了，不报「无变化」
    [mix] = whatif(OLD_MIX, Changes(), sources())["metrics"]
    assert mix["status"] == "unsupported" and mix["changed"] is None and "完整值" in mix["reason"]


#: cell() 一格加一个还原得出、却对不上任何快照的容器（加工过的结果，写它的是 fetch 节点）
MIXED = lone("mixed", "cell(nodes.pull, 0, 'amount') + len(vars.kpi.items)", "120.5 + len(vars.kpi.items)", 123.5,
             [cell("pull", 0, "amount", 120.5), {**scalar("vars.kpi.items", "[3 项]"), "value_artifact": ITEMS_VALUE}],
             decimals=1)
#: 按位置取自整份结果的一格，除以整份结果的行数（都是 parse 节点写的）
FIRST_SHARE = lone("first_share", "round(vars.res.rows[0][1] / len(vars.res.rows), 2)",
                   "round(120.5 / len(vars.res.rows), 2)", 30.12,
                   [scalar("vars.res.rows[0][1]", 120.5, node="parse"),
                    {**scalar("vars.res.rows", "[4 项]", node="parse"), "value_artifact": RES_ROWS}], decimals=2)


def test_a_container_matching_no_snapshot_is_doubted_when_rows_are_excluded():
    """能还原、却对不上任何快照的容器（加工过的结果）：排除行后它会不会变说不准。"""
    [mixed] = whatif(MIXED, Changes(exclude={S1: frozenset({3})}), sources())["metrics"]
    assert mixed["status"] == "unsupported" and mixed["changed"] is None
    assert "排除行对 vars.kpi.items 的影响无法确定" in mixed["reason"] and "对不上任何查询快照" in mixed["reason"]


def test_a_scalar_taken_by_position_from_the_excluded_table_is_doubted():
    """vars.res.rows[0][1] 按位置取自整份结果：排除第 0 行后，len 变了，分子却还是原来第 0 行的值——不能算出一个
    不对应任何一份数据的数。"""
    assert recompute(FIRST_SHARE["metrics"][0], Changes(), sources())["new"] == round(120.5 / 4, 2) == 30.12
    [share] = whatif(FIRST_SHARE, Changes(exclude={S1: frozenset({0})}), sources())["metrics"]
    assert share["status"] == "unsupported" and share["new"] is None and share["changed"] is None
    assert "vars.res.rows[0][1]" in share["reason"]


def test_an_explicit_value_settles_an_input_that_would_be_doubted():
    """按位置取的那一格给了新值：它的值以新值为准，排除行影响不到它，指标照常按剩下的行重算。"""
    changes = Changes(inputs={"vars.res.rows[0][1]": 100}, exclude={S1: frozenset({0})})
    [share] = whatif(FIRST_SHARE, changes, sources())["metrics"]
    assert share["status"] == "ok" and share["new"] == round(100 / 3, 2) == 33.33


@pytest.mark.parametrize("finished, doubted", [(5, False), (10, True), (12, True)])
def test_only_inputs_written_after_the_snapshot_came_back_are_doubted(finished, doubted):
    """写这个输入的节点在快照交回之前就跑完了：排除这份快照里的行影响不到它。跑完的那条事件就是交回快照的那一条
    （合并查询的快照记在它自己跑完的事件里），或者更晚：说不准。"""
    timed = sources(returned={S1: 10}, finished={"fetch": finished})
    [mixed] = whatif(MIXED, Changes(exclude={S1: frozenset({2})}), timed)["metrics"]
    assert (mixed["status"], mixed["changed"]) == (("unsupported", None) if doubted else ("unchanged", False))


def test_a_container_that_picks_the_cell_is_doubted_when_its_rows_are_excluded():
    """cell() 的行号由整份结果算出（取最后一行）：排除了别的行，行号跟着变，取到的是哪一格说不准，不能报「无变化」。"""
    path = "cell(nodes.pull, len(vars.res.rows) - 1, 'amount')"
    card = lone("last", path, "40.0", 40.0, [
        {**cell("pull", 3, "amount", 40.0), "path": path},
        {**scalar("vars.res.rows", "[4 项]", node="parse"), "value_artifact": RES_ROWS}], decimals=1)
    assert recompute(card["metrics"][0], Changes(), sources())["new"] == 40.0
    [last] = whatif(card, Changes(exclude={S1: frozenset({1})}), sources())["metrics"]
    assert last["status"] == "unsupported" and last["changed"] is None and "vars.res.rows" in last["reason"]


def test_a_metric_without_lineage_is_unsupported_whatever_changes():
    legacy = {"id": "old", "name": "旧指标", "unit": "", "value": 3, "expression": "vars.x + 1"}
    card = {**CARD, "metrics": [legacy, CARD["metrics"][0]]}
    for changes in (Changes(), Changes(inputs={"vars.kpi.gmv": 50000}), Changes(exclude={S1: frozenset({1})})):
        old = by_id(whatif(card, changes, sources()))["old"]
        assert old["status"] == "unsupported" and old["changed"] is None and "没有记录输入" in old["reason"]


#: 整个指标推演不了的几种：(卡, Sources 的改动, 改点什么, 状态, 原因里该有的字)。每种都另有一个本来能改的输入
BLOCKED = {
    # 老运行：容器型 vars 输入没存完整值
    "old_container": (lone("n", "len(vars.kpi.items) * vars.kpi.k", "len(vars.kpi.items) * 2", 6,
                           [scalar("vars.kpi.items", "[3 项]"), scalar("vars.kpi.k", 2)]),
                      {}, Changes(inputs={"vars.kpi.k": 5}), "unsupported", ["vars.kpi.items", "完整值"]),
    # 用到了遮罩列：那一格不给、代入式不给
    "masked": (lone("mix", "len(cell(nodes.crm, 0, 'phone')) + vars.kpi.k", "len('13800000000') + 2", 13,
                    [cell("crm", 0, "phone", "13800000000", artifact=S2), scalar("vars.kpi.k", 2)]),
               {}, Changes(inputs={"vars.kpi.k": 5}), "masked", ["遮罩"]),
    # 那一格所在的查询快照读不出来：遮没遮核对不了
    "unreadable": (lone("mix2", "cell(nodes.pull, 0, 'amount') + vars.kpi.k", "120.5 + 2", 122.5,
                        [cell("pull", 0, "amount", 120.5), scalar("vars.kpi.k", 2)], decimals=1),
                   {"load": lambda a: None if a == S1 else load(a)}, Changes(inputs={"vars.kpi.k": 5}), "unsupported",
                   ["cell(nodes.pull, 0, 'amount')", "无法读取"]),
    # agent 的数组字段：节点产出不在了，完整值找不回来
    "lost_array": (lone("share", "round(cell(nodes.pull, 0, 'amount') / sum(vars.kpi.amounts) * 100, 1)",
                        "round(120.5 / sum(vars.kpi.amounts) * 100, 1)", 35.4,
                        [cell("pull", 0, "amount", 120.5), AMOUNTS], decimals=1, fmt="plain", unit="%"),
                   {"load": lambda a: None if a == BOT_OUT else load(a)}, Changes(exclude={S1: frozenset({1})}),
                   "unsupported", ["vars.kpi.amounts", "无法读取节点「bot」当时的产出"]),
    # 升级前的卡：没记输入
    "no_lineage": ({**CARD, "metrics": [{"id": "old", "name": "旧指标", "unit": "", "value": 3,
                                         "expression": "vars.x + 1"}]},
                   {}, Changes(exclude={S1: frozenset({1})}), "unsupported", ["没有记录输入"]),
}


@pytest.mark.parametrize("name", list(BLOCKED))
def test_a_metric_that_cannot_be_recomputed_says_so_before_anything_changes(name):
    """遮罩列、还原不了的最外层输入、读不出来的快照、没记输入：不论改什么都推演不了。一打开就照实说，不报「无变化」；
    输入一个都不让改、不让勾（改了、勾了也推演不出来）；unsupported 里另有一条 input 为空的，界面据此只写原因。"""
    card, kw, change, status, words = BLOCKED[name]
    result = whatif(card, Changes(), sources(**kw))
    [m] = result["metrics"]
    assert m["status"] == status and all(w in m["reason"] for w in words), m
    assert [m[k] for k in ("new", "rendered_new", "changed", "delta", "rendered_delta")] == [None] * 5
    assert all(i["editable"] is False and "row_span" not in i for i in m["inputs"]), m["inputs"]
    assert {"metric": m["id"], "input": None, "reason": m["reason"]} in result["unsupported"]
    # 什么都没改：它不会变，下游也不会受影响
    assert result["maybe_changed"] is False
    # 改了它的输入、排除了它用到的行：照样推演不了，原因不变；它可能变了，下游可能受影响
    after = whatif(card, change, sources(**kw))
    assert [(x["status"], x["reason"], x["changed"]) for x in after["metrics"]] == [(status, m["reason"], None)]
    assert after["maybe_changed"] is True
    assert "13800000000" not in json.dumps([result, after], ensure_ascii=False)


@pytest.mark.parametrize("lost", ["missing", "tampered", "value_artifact"])
def test_excluding_rows_of_an_array_whose_full_value_is_lost_is_never_unchanged(lost):
    """agent 的数组字段完整值找不回来（节点产出不在、被改过，或者另存的完整值读不出来）：排除它用到的行，它一定少了
    一项，少的是多少不知道。指标不能报「无变化」，报告里引用它的地方也不能报「不受影响」；它也不给行段，不让人勾。"""
    item = {**AMOUNTS, "value_artifact": "99" * 32} if lost == "value_artifact" else AMOUNTS
    card = lone("total", "sum(vars.kpi.amounts)", "sum(vars.kpi.amounts)", 340.0, [item], decimals=1, unit="元")
    broken = {"missing": lambda a: None if a == BOT_OUT else load(a),
              "tampered": lambda a: (_ for _ in ()).throw(ValueError("篡改")) if a == BOT_OUT else load(a),
              "value_artifact": load}[lost]
    result = whatif(card, Changes(exclude={S1: frozenset({1})}), sources(load=broken))
    [total] = result["metrics"]
    assert total["status"] == "unsupported" and total["changed"] is None and total["new"] is None
    assert total["rendered_new"] is None and "vars.kpi.amounts" in total["reason"]
    [amounts] = total["inputs"]
    assert "row_span" not in amounts and amounts["excluded_rows"] == [1] and amounts["editable"] is False
    # 少了一项是确定的，少了之后什么样不知道
    assert (amounts["old"], amounts["new"], amounts["changed"]) == ("[4 项]", None, True)
    assert result["maybe_changed"] is True
    [found] = report_segments([("write", doc_with(seg("s2", "340.0元", "m:total")))], card_node="card",
                              card_artifact="CARD", results=result["metrics"])
    assert found["rendered_new"] is None and found["changed"] is None


def test_only_excluded_rows_inside_an_array_span_reach_it():
    """数组字段只用到快照的第 1–2 行（从 0 数）：排除第 0 行影响不到它；同时排除第 0、2 行，起作用的只有第 2 行。"""
    out = "c9" * 32
    mid = {"path": "vars.kpi.mid", "value": "[2 项]", "node_id": "bot2", "via": "agent_field", "field": "mid",
           "artifact": S1, "locator": {"rows": [1, 2], "column": "amount"}, "eid": "ev:rows:y", "status": "verified"}
    card = lone("mid", "sum(vars.kpi.mid)", "sum(vars.kpi.mid)", 179.5, [mid], decimals=1, unit="元")
    span = sources(load=lambda a: {"data": {"mid": [80.0, 99.5]}} if a == out else load(a),
                   outputs={"bot": BOT_OUT, "fetch": FETCH_OUT, "bot2": out})
    assert whatif(card, Changes(), span)["metrics"][0]["inputs"][0]["row_span"] == [1, 2]
    [m] = whatif(card, Changes(exclude={S1: frozenset({0})}), span)["metrics"]
    assert m["status"] == "unchanged" and "excluded_rows" not in m["inputs"][0]
    [m] = whatif(card, Changes(exclude={S1: frozenset({0, 2})}), span)["metrics"]
    assert m["status"] == "ok" and m["new"] == 80.0 and m["inputs"][0]["excluded_rows"] == [2]


# --------------------------------------------------------------------------
# 查询快照读不出来：值不给、不推演
# --------------------------------------------------------------------------


@pytest.mark.parametrize("broken, words", [
    (lambda a: None if a == S1 else load(a), "无法读取"),
    (lambda a: (_ for _ in ()).throw(ValueError("篡改")) if a == S1 else load(a), "哈希"),
    # 读得出来，却不是一份查询结果（没有 rows）：同样核对不了
    (lambda a: {"columns": ["region", "amount", "phone"], "source": "shop"} if a == S1 else load(a), "无法读取"),
])
def test_an_unreadable_snapshot_withholds_its_cells(broken, words):
    """快照读不出来，遮没遮核对不了：卡上记着的那一格也不给、不能改、没有行可勾，用到它的指标推演不了。"""
    path = "cell(nodes.pull, 0, 'amount')"
    result = whatif(CARD, Changes(), sources(load=broken), metric="east")
    [east] = result["metrics"]
    [inp] = east["inputs"]
    assert inp["old"] is None and inp["new"] is None and inp["editable"] is False and inp["withheld"] is True
    assert "row_span" not in inp and east["status"] == "unsupported" and words in east["reason"]
    assert east["substituted"] is None and "120.5" not in str(east["inputs"]) + str(east["substituted"])
    assert [(u["input"], words in u["reason"]) for u in result["unsupported"]] == [(None, True), (path, True)]
    with pytest.raises(WhatIfError, match="快照"):
        whatif(CARD, Changes(inputs={path: 100}), sources(load=broken), metric="east")
    # 别的指标照样推演，只是用到这份快照的推演不了（数组字段的出处也在这份快照上）
    metrics = by_id(whatif(CARD, Changes(inputs={"vars.kpi.gmv": 50000}), sources(load=broken)))
    assert metrics["wow"]["status"] == "ok" and metrics["wow"]["new"] == 19.0
    assert metrics["total"]["status"] == "unsupported" and "120.5" not in str(metrics["total"]["inputs"])


def test_a_masked_column_stays_masked_when_its_snapshot_is_unreadable():
    phone = by_id(whatif(CARD, Changes(), sources(load=lambda a: None if a == S2 else load(a))))["phone_len"]
    [inp] = phone["inputs"]
    assert inp["masked"] is True and inp["old"] is None and phone["substituted"] is None
    # 既遮着、快照又读不出来：说遮罩，快照修好了它照样遮着
    assert phone["status"] == "masked" and "遮罩" in phone["reason"]


def test_a_withheld_cell_inside_another_reference_stays_out_of_the_substituted_expression():
    """取第几行由一格决定，这一格落在读不出来的快照上；取到的又是长文本（代入式不代长文本，就把行号代了进去）。
    嵌在里面不碍事，指标照样推演得了，但代入式里有那一格的值，同样不给。"""
    s3, note = "d3" * 32, "备注" * 50
    store = {**STORE, s3: {"columns": ["note"], "source": "crm", "rows": [["短"], ["短"], [note]]}}
    path = "cell(nodes.memo, vars.kpi.r, 'note')"
    r = {**scalar("vars.kpi.r", 2), "via": "agent_field", "artifact": S1, "locator": {"row": 0, "column": "amount"}}
    card = lone("memo_len", f"len({path})", "len(cell(nodes.memo, 2, 'note'))", 100,
                [{**cell("memo", 2, "note", note, artifact=s3), "path": path}, r])
    [m] = whatif(card, Changes(), sources(load=lambda a: None if a == S1 else store.get(a)))["metrics"]
    assert m["status"] == "unchanged" and m["substituted"] is None
    assert [i.get("withheld") for i in m["inputs"]] == [None, True]


# --------------------------------------------------------------------------
# 超大的数、给人看的值
# --------------------------------------------------------------------------


def test_numbers_too_large_to_compute_are_rejected_not_crashed():
    with pytest.raises(WhatIfError, match="范围"):
        whatif(CARD, Changes(inputs={"vars.kpi.gmv": 10 ** 400}), sources())
    # 1e308 本身放得下，乘出来的整数放不下：说清算不出来
    [n2] = whatif(CARD, Changes(inputs={"vars.kpi.k": 10 ** 308}), sources(), metric="n2")["metrics"]
    assert n2["status"] == "error" and n2["new"] is None and "有限" in n2["reason"]
    json.dumps(n2, allow_nan=False)


@pytest.mark.parametrize("value, shown", [(None, "空值"), ({"a": 1}, '{"a": 1}'), ([1, "两"], '[1, "两"]')])
def test_invalid_values_are_shown_the_way_a_person_writes_them(value, shown):
    with pytest.raises(WhatIfError) as caught:
        whatif(CARD, Changes(inputs={"vars.kpi.gmv": value}), sources())
    assert shown in str(caught.value) and "None" not in str(caught.value) and "'a'" not in str(caught.value)


# --------------------------------------------------------------------------
# 推演里能动什么：可勾的行、不能动的原因
# --------------------------------------------------------------------------


def test_inputs_say_which_snapshot_rows_they_use():
    """输入实际落在快照的哪几行（row_span，含两端）：单格一行、数组字段一段、整份结果全部。遮罩的不给。"""
    result = whatif(CARD, Changes(), sources())
    spans = {(m["id"], i["path"]): i.get("row_span") for m in result["metrics"] for i in m["inputs"]}
    assert spans[("east", "cell(nodes.pull, 0, 'amount')")] == [0, 0]
    assert spans[("total", "vars.kpi.amounts")] == [0, 3]
    assert spans[("rows", "vars.res.rows")] == [0, 3]
    assert spans[("phone_len", "cell(nodes.crm, 0, 'phone')")] is None
    assert spans[("wow", "vars.kpi.gmv")] is None and spans[("n2", "vars.kpi.items")] is None


def test_inputs_that_cannot_be_adjusted_say_why():
    """不能改、也没有行可勾的输入都列进「不支持推演的输入」，不让人对着一个改不了的值干看。"""
    card = {**CARD, "metrics": [
        *CARD["metrics"],
        metric("label", "len(vars.kpi.name) + vars.kpi.k", "len('华东区') + 2", 5,
               [scalar("vars.kpi.name", "华东区"), scalar("vars.kpi.k", 2)]),
        metric("flag", "vars.kpi.on * 3", "True * 3", 3, [scalar("vars.kpi.on", True)]),
        # 老运行：容器没存完整值，整个推演不了
        metric("label_n", "len(vars.kpi.name) + len(vars.kpi.items)", "len('华东区') + len(vars.kpi.items)", 6,
               [scalar("vars.kpi.name", "华东区"), scalar("vars.kpi.items", "[3 项]")])]}
    reasons = {(u["metric"], u["input"]): u["reason"] for u in whatif(card, Changes(), sources())["unsupported"]}
    assert "列表或对象不能直接修改" in reasons[("n2", "vars.kpi.items")]
    assert "列表或对象不能直接修改" in reasons[("n3", "nodes.fetch.items")]
    assert "不是数值" in reasons[("label", "vars.kpi.name")] and "不是数值" in reasons[("flag", "vars.kpi.on")]
    # 能排除行的容器、能改的数不在其中
    assert ("total", "vars.kpi.amounts") not in reasons and ("rows", "vars.res.rows") not in reasons
    assert ("label", "vars.kpi.k") not in reasons and ("wow", "vars.kpi.gmv") not in reasons
    # 整个推演不了的指标：一条整个指标的，再加卡住它的那个输入；别的输入能不能改已无从谈起，不再单列
    assert [i for m, i in reasons if m == "label_n"] == [None, "vars.kpi.items"]


def test_a_lost_array_inside_a_subscript_offers_no_rows():
    """cell() 的行号由 agent 的数组字段算出，这个数组另存的完整值又读不出来：嵌在里面不碍事（取到的那一格卡上记着），
    指标照样推演得了，那一格能改、能勾它那一行；数组本身不给行段。排除了数组用到的行，取到哪一格说不准。"""
    path = "cell(nodes.pull, len(vars.kpi.amounts) - 1, 'amount')"
    lost = {**AMOUNTS, "value_artifact": "99" * 32}
    card = lone("last2", path, "40.0", 40.0, [{**cell("pull", 3, "amount", 40.0), "path": path}, lost], decimals=1)
    [m] = whatif(card, Changes(), sources())["metrics"]
    assert m["status"] == "unchanged" and [i.get("row_span") for i in m["inputs"]] == [[3, 3], None]
    assert [i["editable"] for i in m["inputs"]] == [True, False]
    [m] = whatif(card, Changes(exclude={S1: frozenset({1})}), sources())["metrics"]
    assert m["status"] == "unsupported" and m["changed"] is None and "vars.kpi.amounts" in m["reason"]


# --------------------------------------------------------------------------
# 还原时的几处保护
# --------------------------------------------------------------------------


def test_an_array_whose_length_does_not_match_its_rows_is_not_excluded_by_row():
    """数组字段 4 行的出处、产出里却只有 3 个元素：元素和行对不上号，不能按行去掉元素（会去错）。"""
    short = sources(load=lambda a: {**STORE[BOT_OUT], "data": {"amounts": [120.5, 80.0, 99.5]}} if a == BOT_OUT
                    else load(a))
    card = lone("total3", "sum(vars.kpi.amounts)", "sum(vars.kpi.amounts)", 300.0, [AMOUNTS], decimals=1, unit="元")
    assert recompute(card["metrics"][0], Changes(), short)["new"] == 300.0
    [total] = whatif(card, Changes(exclude={S1: frozenset({1})}), short)["metrics"]
    assert total["status"] == "unsupported" and total["new"] is None
    assert "excluded_rows" not in total["inputs"][0] and total["inputs"][0].get("row_span") is None


def test_a_node_path_with_a_variable_index_is_not_restored():
    card = lone("pick2", "len(nodes.fetch.lists[vars.kpi.i])", "len(nodes.fetch.lists[vars.kpi.i])", 2,
                [{**scalar("nodes.fetch.lists[vars.kpi.i]", "[2 项]"), "field": "lists[vars.kpi.i]"},
                 scalar("vars.kpi.i", 0)])
    out = recompute(card["metrics"][0], Changes(), sources())
    assert out["status"] == "unsupported" and "变量下标" in out["reason"]


def test_text_that_merely_looks_like_a_container_summary_is_a_plain_value():
    """值恰好是「[3 项]」这几个字的文本，代入式里被代成了字面量：它就是当时的值，不是容器。"""
    card = lone("tag", "len(vars.kpi.tag)", "len('[3 项]')", 5, [scalar("vars.kpi.tag", "[3 项]")])
    out = recompute(card["metrics"][0], Changes(), sources())
    assert out["status"] == "ok" and out["new"] == 5 and out["inputs"][0]["kind"] == "text"


def test_a_query_result_carrying_its_snapshot_id_is_mapped_by_it():
    """容器带着封存范围内快照的 artifact（数据源工具交回的结果原样解析）：按它认快照，哪怕行的写法和快照不同。"""
    parsed = "b8" * 32
    rows = [["华东", "120.5", "p1"], ["华南", "80.0", "p2"], ["华北", "99.5", "p3"], ["西南", "40.0", "p4"]]
    store = {**STORE, parsed: {"path": "vars.res", "value": {"columns": ["region", "amount", "phone"], "rows": rows,
                                                               "artifact": S1}}}
    card = lone("cnt", "len(get(vars.res, 'rows'))", "len(get(vars.res, 'rows'))", 4,
                [{**scalar("vars.res", "{3 个字段}", node="parse"), "value_artifact": parsed}])
    mapped = sources(load=lambda a: store.get(a) if a != BAD else load(a))
    assert recompute(card["metrics"][0], Changes(), mapped)["new"] == 4
    [cnt] = whatif(card, Changes(exclude={S1: frozenset({0, 2})}), mapped)["metrics"]
    assert cnt["status"] == "ok" and cnt["new"] == 2
    assert cnt["inputs"][0]["artifact"] == S1 and cnt["inputs"][0]["excluded_rows"] == [0, 2]


def test_a_segment_whose_metric_had_no_value_is_listed_when_the_whatif_supplies_one():
    """缺输入、当时显示「—」的引用也列上：推演里补上了值，它会变。"""
    card = {**CARD, "metrics": [metric("cost", "vars.kpi.cost / vars.kpi.orders", "vars.kpi.cost / 1234", None,
                                       [scalar("vars.kpi.cost", None), scalar("vars.kpi.orders", 1234)], decimals=2,
                                       rendered="—")]}
    result = whatif(card, Changes(inputs={"vars.kpi.cost": 2468}), sources())
    doc = doc_with({"id": "s7", "text": "—", "kind": "value", "state": "flagged",
                    "cite": {"alias": "m:cost", "kind": "metric", "status": "unresolved"}})
    doc["catalog"]["m:cost"] = {"alias": "m:cost", "kind": "metric", "node_id": "card", "artifact": "CARD",
                                "locator": {"metric": "cost"}, "value": None, "status": "missing_input"}
    [found] = report_segments([("write", doc)], card_node="card", card_artifact="CARD", results=result["metrics"])
    assert (found["sid"], found["rendered_old"], found["rendered_new"], found["changed"]) == ("s7", "—", "2.00", True)


# --------------------------------------------------------------------------
# 下游口径卡：经中间节点引用的也标
# --------------------------------------------------------------------------


def test_downstream_cards_reached_through_other_nodes_are_flagged():
    """card → mid（transform 用了卡上的值）→ yearly：yearly 的输入写的是 vars.t.g，来自 mid，不是卡本身。
    按画布上的边从这张卡往下找：经别的节点间接用到的也标「可能受影响」。"""
    upstream = ("card", "A1", CARD)
    yearly = ("yearly", "B1", {"caliber": "年报口径", "caliber_version": "v1", "metrics": [
        metric("y", "vars.t.g * 26", "17.4 * 26", 452.4,
               [{"path": "vars.t.g", "value": 17.4, "node_id": "mid", "via": "transform", "status": "ok"}])]})
    before = ("before", "C1", {"caliber": "早口径", "caliber_version": "v1", "metrics": [
        metric("b", "vars.kpi.k", "2", 2, [scalar("vars.kpi.k", 2, node="fetch")])]})
    edges = [("fetch", "before"), ("before", "card"), ("card", "mid"), ("mid", "yearly")]
    assert downstream_cards([upstream, yearly, before], "card") == []
    assert downstream_cards([upstream, yearly, before], "card", edges=edges) == [
        {"node_id": "yearly", "artifact": "B1", "caliber": "年报口径", "version": "v1", "metrics": ["y"], "direct": False}]
