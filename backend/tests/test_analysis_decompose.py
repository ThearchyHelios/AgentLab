"""变化拆解（app/analysis/decompose.py，细案 01 §2）：三种恒等式逐位成立、辛普森、抵消、不闭合、下钻闭合与超深、新出现 /
消失的段、比率当可加的调用错误……每个用例都顺带核对复算和运行逐字段相同（tests/analysis_samples.analyze）。"""
from __future__ import annotations

import math
import random
from decimal import Decimal
from fractions import Fraction

import pytest

from app.analysis.decompose import lmdi, ratio_effects
from tests.analysis_samples import analyze, call_error, fixed_prober, notes, snap

SQL = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week"
TYPES = {"region": "text", "week": "text", "gmv": "number"}
WEEKLY = [["华东", "2026-W41", 820], ["华东", "2026-W42", 700], ["华北", "2026-W41", 600], ["华北", "2026-W42", 560],
          ["华南", "2026-W41", 700], ["华南", "2026-W42", 666], ["西南", "2026-W41", 100], ["西南", "2026-W42", 100]]


def weekly(rows=WEEKLY, **kw):
    return snap("Q2", SQL, ["region", "week", "gmv"], rows, types=TYPES, **kw)


async def additive(rows=WEEKLY, *, snaps=None, **params):
    params = {"query": "Q2", "by": ["region"], "period": "week", "base": "2026-W41", "target": "2026-W42",
              "measure": "gmv", **params}
    return await analyze("decompose", "additive", snaps=snaps or [weekly(rows)], **params)


def _rows(base: dict, target: dict) -> list[list]:
    return [[k, "2026-W41", v] for k, v in base.items()] + [[k, "2026-W42", v] for k, v in target.items()]


# --------------------------------------------------------------------------
# additive
# --------------------------------------------------------------------------


async def test_a_driver_is_named_with_its_share_and_the_whole_shape_is_filled():
    result = await additive(label="销售额", hypothesis="华东拖累")
    assert result["method"] == "decompose.additive@1" and result["status"] == "ok"
    assert result["verdict"] == {"code": "driver", "subjects": [["华东"]], "direction": "down"}
    assert result["phrase"] == "销售额下降 194（-8.7%），主要来自华东（-120，占 61.9%）"
    assert result["values"]["delta_total"] == "-194" and result["values"]["top_key"] == "华东"
    assert result["values"]["top_share"] == "0.618556701031" and result["values"]["segments"] == "4"
    table = result["table"]
    assert table["columns"] == ["region", "base", "target", "delta", "pct", "share", "flag"]
    assert table["labels"]["region"] == "地区" and table["labels"]["share"] == "占变化的比重"
    assert table["column_types"] == {"region": "text", "base": "number", "target": "number", "delta": "number",
                                     "pct": "number", "share": "number", "flag": "text"}
    assert [r[0] for r in table["rows"]] == ["华东", "华北", "华南", "西南"]          # 按变化的绝对值从大到小
    assert result["settings"] == {"main_share": 0.5, "concentration": 0.8, "offset_ratio": 0.25, "flat_pct": 0.005,
                                  "table_top_k": 10}
    assert result["inputs"] == [{"ref": "Q2", "artifact": weekly()["artifact"], "role": "data",
                                 "columns": ["region", "week", "gmv"], "rows": "all"}]
    assert result["hypothesis"] == "华东拖累" and result["probes"] == []
    assert result["criterion"].endswith("以各段之和为总数")
    assert {c["code"] for c in result["caveats"]} == {"headline_basis"}
    assert result["params"]["catalog"] == {"labels": {"region": "地区"}, "measures": {"gmv": "flow"}}


async def test_the_segments_add_up_to_the_total_digit_for_digit():
    rng = random.Random(7)
    regions = ["华东", "华南", "华北", "西南", "东北", "西北"]
    for _ in range(20):
        base = {r: round(rng.uniform(0, 5000), rng.choice([0, 2, 3])) for r in regions}
        target = {r: round(rng.uniform(0, 5000), rng.choice([0, 2, 3])) for r in regions}
        result = await additive(_rows(base, target))
        rows = result["table"]["rows"]
        assert sum(Decimal(r[3]) for r in rows) == Decimal(result["values"]["delta_total"])
        assert sum(Decimal(r[1]) for r in rows) == Decimal(result["values"]["base_total"])
        assert sum(Decimal(r[2]) for r in rows) == Decimal(result["values"]["target_total"])
        assert Decimal(result["values"]["target_total"]) - Decimal(result["values"]["base_total"]) == \
            Decimal(result["values"]["delta_total"])
        assert result["values"]["gross_up"] and result["values"]["gross_down"]


async def test_concentrated_and_dispersed():
    base = {f"店{i}": 1000 for i in range(10)}
    target = {**base, "店0": 1450, "店1": 1400, "店2": 1050, "店3": 1050, "店4": 1050}
    result = await additive(_rows(base, target), by="region")
    assert result["verdict"]["code"] == "concentrated"
    assert result["verdict"]["subjects"] == [["店0"], ["店1"]]
    assert result["values"]["top_k"] == "2" and result["values"]["top_k_share"] == "0.85"
    assert "集中在店0、店1（合计占 85.0%）" in result["phrase"]
    spread = await additive(_rows({r: 1000 for r in "甲乙丙丁戊"}, {r: 1100 for r in "甲乙丙丁戊"}))
    assert spread["verdict"] == {"code": "dispersed", "subjects": [], "direction": "up"}
    assert "分散在各地区，没有哪一段占到一半" in spread["phrase"]


async def test_offsetting_segments_are_said_and_a_share_can_pass_one_hundred_percent():
    result = await additive(_rows({"华东": 1000, "华北": 500, "华南": 500}, {"华东": 850, "华北": 540, "华南": 490}))
    assert result["values"]["delta_total"] == "-120"
    assert result["verdict"]["code"] == "driver" and result["values"]["top_share"] == "1.25"
    offset = next(c for c in result["caveats"] if c["code"] == "offset")
    assert offset["text"] == "下降的段合计 -160，上升的段合计 +40，互相抵消了一部分"
    assert result["phrase"] == "销售额下降 120（-6.0%），主要来自华东（-150，占 125.0%）；下降的段合计 -160，上升的段合计 +40，互相抵消了一部分"


async def test_flat_with_offsets_says_what_moved_inside():
    result = await additive(_rows({"华东": 1000, "华北": 1000, "华南": 220}, {"华东": 1120, "华北": 882, "华南": 220}))
    assert result["verdict"] == {"code": "flat", "subjects": [], "direction": None}
    assert result["phrase"] == "销售额基本持平（+0.1%），内部此消彼长：华东 +120、华北 -118"


async def test_a_headline_that_does_not_close_stops_the_concentration_verdict():
    head = snap("Q1", "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week", ["week", "gmv"],
                [["2026-W42", 2050], ["2026-W41", 2220]], types={"week": "text", "gmv": "number"})
    result = await additive(snaps=[weekly(), head], headline={"base": "Q1.r1.gmv", "target": "Q1.r0.gmv"})
    assert result["verdict"] == {"code": "unclosed", "subjects": [], "direction": None}
    assert result["values"]["gap"] == "24" and result["values"]["top_share"] is None
    assert all(r[5] is None for r in result["table"]["rows"])                 # 不给占比
    assert result["phrase"] == "按地区拆开后各段之和和总数对不上（差 24），不判断变化来自哪里"
    assert [i["role"] for i in result["inputs"]] == ["headline_base", "headline_target", "data"]
    assert result["inputs"][0] == {"ref": "Q1.r1.gmv", "artifact": head["artifact"], "role": "headline_base",
                                   "row": 1, "column": "gmv", "value": "2220"}
    assert "各段之和要和总数对得上" in result["criterion"]


async def test_a_rounded_headline_closes_within_its_written_precision():
    rows = [["华东", "2026-W41", "820.25"], ["华东", "2026-W42", "700.12"], ["华北", "2026-W41", "1399.9"],
            ["华北", "2026-W42", "1325.84"]]
    for written, closes in (("2220.2", True), ("2220", True), ("2221", False)):
        head = snap("Q1", "SELECT SUM(amount) AS gmv FROM orders", ["gmv_base", "gmv"], [[written, "2025.96"]])
        result = await additive(rows, snaps=[weekly(rows), head], headline=["Q1.r0.gmv_base", "Q1.r0.gmv"])
        assert (result["verdict"]["code"] != "unclosed") is closes, written


async def test_a_headline_rounded_by_the_database_to_a_whole_number_closes_within_half_a_unit():
    # SQLite 的 ROUND() 交回浮点 2120.0：取到了个位，最短写法里的「.0」不是一位小数的书写精度
    rows = [["华东", "2026-W41", "1120.2"], ["华东", "2026-W42", "700"], ["华北", "2026-W41", "1000.2"],
            ["华北", "2026-W42", "600"]]
    for written, closes in ((2120.0, True), (2121.0, False), (2120.5, False)):
        head = snap("Q1", "SELECT ROUND(SUM(amount)) AS gmv FROM orders", ["gmv_base", "gmv"], [[written, 1300.0]])
        result = await additive(rows, snaps=[weekly(rows), head], headline=["Q1.r0.gmv_base", "Q1.r0.gmv"])
        assert (result["verdict"]["code"] != "unclosed") is closes, written


async def test_a_drill_closes_against_the_exact_value_of_the_parent_segment():
    top = await additive()                                             # 华东 820 → 700
    near = snap("Q4", "SELECT province, week, SUM(amount) AS gmv FROM orders WHERE region = '华东' GROUP BY 1, 2",
                ["province", "week", "gmv"],
                [["沪", "2026-W41", "500.4"], ["沪", "2026-W42", "410"], ["苏", "2026-W41", 320],
                 ["苏", "2026-W42", "290.3"]])
    params = {"query": "Q4", "by": ["province"], "period": "week", "base": "2026-W41", "target": "2026-W42",
              "measure": "gmv", "parent": {"of": "A3", "key": ["华东"]}}
    off = await analyze("decompose", "additive", snaps=[near], parents={"A3": top}, **params)
    # 上一层那一段是算出来的精确值，不是取整过的总数：820.4 对 820、700.3 对 700 都不闭合
    assert off["verdict"]["code"] == "unclosed" and off["values"]["gap"] == "-0.4"


async def test_drilling_down_checks_closure_against_the_parent_segment():
    top = await additive()
    province = snap("Q4", "SELECT province, week, SUM(amount) AS gmv FROM orders WHERE region = '华东' GROUP BY 1, 2",
                    ["province", "week", "gmv"],
                    [["沪", "2026-W41", 500], ["沪", "2026-W42", 410], ["苏", "2026-W41", 320], ["苏", "2026-W42", 290]])
    params = {"query": "Q4", "by": ["province"], "period": "week", "base": "2026-W41", "target": "2026-W42",
              "measure": "gmv", "parent": {"of": "A3", "key": ["华东"]}}
    child = await analyze("decompose", "additive", snaps=[province], parents={"A3": top}, **params)
    assert child["verdict"]["code"] == "driver" and child["verdict"]["subjects"] == [["沪"]]
    assert child["params"]["depth"] == 1 and child["params"]["parent"] == {"of": "A3", "key": ["华东"]}
    assert child["inputs"][0]["role"] == "parent" and child["inputs"][0]["ref"] == "A3"
    assert "max_drill_depth" in child["settings"]
    assert {c["code"] for c in child["caveats"]} == set()                    # 有 parent 就不是「以各段之和为总数」
    short = snap("Q4", province["sql"], ["province", "week", "gmv"],
                 [["沪", "2026-W41", 500], ["沪", "2026-W42", 410], ["苏", "2026-W41", 300], ["苏", "2026-W42", 290]])
    unclosed = await analyze("decompose", "additive", snaps=[short], parents={"A3": top}, **params)
    assert unclosed["verdict"]["code"] == "unclosed" and unclosed["values"]["gap"] == "20"
    # 超深：上一层已经是第 1 层，最多 1 层
    error = await call_error("decompose", "additive", snaps=[province], parents={"A3": child},
                             settings={"max_drill_depth": 1}, **{**params, "parent": {"of": "A3", "key": ["沪"]}})
    assert error.code == "too_deep" and "最多 1 层" in error.message
    missing = await call_error("decompose", "additive", snaps=[province], parents={"A3": top},
                               **{**params, "parent": {"of": "A3", "key": ["东北"]}})
    assert missing.code == "parent_key_missing"
    absent = await call_error("decompose", "additive", snaps=[province], **params)
    assert absent.code == "parent_missing"


async def test_new_and_vanished_segments_and_empty_cells_are_flagged():
    rows = [["华东", "2026-W41", 800], ["华东", "2026-W42", 700], ["西南", "2026-W42", 90],
            ["东北", "2026-W41", 40], ["华北", "2026-W41", None], ["华北", "2026-W42", 30]]
    result = await additive(rows)
    flags = {r[0]: r[6] for r in result["table"]["rows"]}
    assert flags == {"华东": None, "西南": "新出现", "东北": "消失", "华北": "为空"}
    null = next(c for c in result["caveats"] if c["code"] == "null_values")
    assert null["text"] == "华北的销售额为空，按 0 算"
    assert next(r for r in result["table"]["rows"] if r[0] == "西南")[4] is None      # 基期为 0 不给变化率


async def test_two_dimension_keys():
    sql = "SELECT region, channel, week, SUM(amount) AS gmv FROM orders GROUP BY 1, 2, 3"
    rows = [["华东", "线上", "W1", 500], ["华东", "线上", "W2", 300], ["华东", "线下", "W1", 300], ["华东", "线下", "W2", 310],
            ["华北", "线上", "W1", 200], ["华北", "线上", "W2", 205]]
    s = snap("Q2", sql, ["region", "channel", "week", "gmv"], rows)
    result = await analyze("decompose", "additive", snaps=[s], query="Q2", by=["region", "channel"], period="week",
                           base="W1", target="W2", measure="gmv")
    assert result["verdict"]["subjects"] == [["华东", "线上"]]
    assert "主要来自华东·线上" in result["phrase"]


async def test_ratio_and_non_additive_measures_are_call_errors():
    avg = snap("Q2", "SELECT region, week, AVG(amount) AS aov FROM orders GROUP BY 1, 2", ["region", "week", "aov"], WEEKLY)
    error = await call_error("decompose", "additive", snaps=[avg], query="Q2", by="region", period="week",
                             base="2026-W41", target="2026-W42", measure="aov")
    assert error.code == "ratio_as_additive" and "改用比率拆解" in error.message
    rate_sql = "SELECT region, week, SUM(refund_rate) AS r FROM orders GROUP BY 1, 2"
    labeled = {"shop": {"orders": notes(columns={"refund_rate": {"measure": "ratio"}})}}
    error = await call_error("decompose", "additive", snaps=[snap("Q2", rate_sql, ["region", "week", "r"], WEEKLY)],
                             catalogs=labeled, query="Q2", by="region", period="week", base="2026-W41",
                             target="2026-W42", measure="r")
    assert error.code == "ratio_as_additive"
    # 只是推断（没人确认）的照算，写附带说明
    guessed = {"shop": {"orders": notes(columns={"refund_rate": {"measure": {"value": "ratio", "source": "llm",
                                                                             "status": "proposed"}}})}}
    result = await analyze("decompose", "additive", snaps=[snap("Q2", rate_sql, ["region", "week", "r"], WEEKLY)],
                           catalogs=guessed, query="Q2", by="region", period="week", base="2026-W41",
                           target="2026-W42", measure="r")
    note = next(c for c in result["caveats"] if c["code"] == "measure_unlabeled")
    assert note["text"] == "目录里「r」是比率只是推断，没人确认，按可以按段相加处理"
    unlabeled = await additive(snaps=[weekly()], catalogs={})
    assert any(c["code"] == "measure_unlabeled" and "目录没标「gmv」的度量类型" in c["text"] for c in unlabeled["caveats"])


async def test_call_errors_tell_the_agent_how_to_fix_the_call():
    dup = WEEKLY + [["华东", "2026-W42", 5]]
    error = await call_error("decompose", "additive", snaps=[weekly(dup)], query="Q2", by="region", period="week",
                             base="2026-W41", target="2026-W42", measure="gmv")
    assert error.code == "duplicate_rows" and "在 SQL 里按 地区、week 汇总" in error.message
    masked = await call_error("decompose", "additive", snaps=[weekly(masked=["Region"])], query="Q2", by="region",
                              period="week", base="2026-W41", target="2026-W42", measure="gmv")
    assert masked.code == "masked_column"
    missing = await call_error("decompose", "additive", snaps=[weekly()], query="Q2", by="region", period="week",
                               base="2026-W40", target="2026-W42", measure="gmv")
    assert missing.code == "period_not_found" and "2026-W41" in missing.message
    column = await call_error("decompose", "additive", snaps=[weekly()], query="Q2", by="area", period="week",
                              base="2026-W41", target="2026-W42", measure="gmv")
    assert column.code == "unknown_column" and "region、week、gmv" in column.message
    text = await call_error("decompose", "additive", snaps=[weekly()], query="Q2", by="gmv", period="week",
                            base="2026-W41", target="2026-W42", measure="region")
    assert text.code == "not_numeric"
    unknown = await call_error("decompose", "additive", snaps=[weekly()], query="Q9", by="region", period="week",
                               base="2026-W41", target="2026-W42", measure="gmv")
    assert unknown.code == "unknown_query"
    same_column = await call_error("decompose", "additive", snaps=[weekly()], query="Q2", by="week", period="week",
                                   base="2026-W41", target="2026-W42", measure="gmv")
    assert same_column.code == "bad_by"


async def test_truncated_snapshots_and_single_segments_are_refused():
    result = await additive(snaps=[weekly(truncated=True)])
    assert result["status"] == "refused" and result["reason_code"] == "truncated"
    assert result["reason"] == "Q2 只取回了前 8 行，各段之和不是完整的和"
    assert result["phrase"] == "变化拆解没有做成：Q2 只取回了前 8 行，各段之和不是完整的和"
    assert result["verdict"] is None and result["table"] is None and result["values"] == {}
    one = await additive([["华东", "2026-W41", 1], ["华东", "2026-W42", 2]])
    assert one["reason_code"] == "too_few_segments"


async def test_two_snapshots_check_comparability_first():
    base = snap("Q1", "SELECT region, SUM(amount) AS gmv FROM orders WHERE order_date >= '2026-10-05' AND "
                      "order_date < '2026-10-12' GROUP BY region", ["region", "gmv"],
                [["华东", 820], ["华北", 600], ["华南", 700]])
    target = snap("Q2", "SELECT region, SUM(amount) AS gmv FROM orders WHERE order_date >= '2026-10-12' AND "
                        "order_date < '2026-10-19' GROUP BY region", ["region", "gmv"],
                  [["华东", 700], ["华北", 560], ["华南", 666]])
    probe = fixed_prober({("orders", "order_date"): ("2025-01-01", "2026-10-20")})
    result = await analyze("decompose", "additive", snaps=[base, target], probe=probe, base_query="Q1",
                           target_query="Q2", by="region", measure="gmv")
    assert result["verdict"]["code"] == "driver" and result["params"]["today"] == "2026-10-21"
    assert not any(c["code"] == "comparability" for c in result["caveats"])
    assert [i["role"] for i in result["inputs"]] == ["base", "target"]
    assert result["probes"] == [{"source": "shop", "table": "orders", "column": "order_date", "min": "2025-01-01",
                                 "max": "2026-10-20", "basis": "live", "at": "2026-10-21T09:00:00"}]
    # 本期没过完：照样拆，判定后附上可比性结论
    partial = snap("Q2", target["sql"].replace("'2026-10-19'", "'2026-10-25'"), ["region", "gmv"], target["rows"])
    result = await analyze("decompose", "additive", snaps=[base, partial], probe=probe, base_query="Q1",
                           target_query="Q2", by="region", measure="gmv")
    note = next(c for c in result["caveats"] if c["code"] == "comparability")
    assert note["text"].startswith("两期不可比：") and "本期数据只到 2026-10-20，还差 4 天" in note["text"]
    assert result["verdict"]["code"] == "driver" and note["text"] in result["phrase"]
    # 口径结构不同：不拆
    other = snap("Q2", "SELECT region, SUM(amount) AS gmv FROM orders JOIN refunds USING (order_id) WHERE order_date "
                       ">= '2026-10-12' GROUP BY region", ["region", "gmv"], target["rows"])
    refused = await analyze("decompose", "additive", snaps=[base, other], probe=probe, base_query="Q1",
                            target_query="Q2", by="region", measure="gmv")
    assert refused["reason_code"] == "incomparable_sides" and refused["reason"].startswith("两份查询的口径结构不同")


# --------------------------------------------------------------------------
# ratio
# --------------------------------------------------------------------------

RATIO_SQL = "SELECT channel, week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY 1, 2"


def _ratio_snap(rows):
    return snap("Q1", RATIO_SQL, ["channel", "week", "refunds", "orders"], rows)


async def ratio(rows, **params):
    params = {"query": "Q1", "by": "channel", "period": "week", "base": "W41", "target": "W42",
              "numerator": "refunds", "denominator": "orders", **params}
    return await analyze("decompose", "ratio", snaps=[_ratio_snap(rows)], **params)


def test_mix_plus_rate_effects_equal_the_ratio_change_exactly():
    rng = random.Random(11)
    for _ in range(200):
        segs = []
        for _ in range(rng.randint(2, 7)):
            d_b, d_t = rng.choice([0, rng.randint(1, 5000)]), rng.randint(1, 5000)
            segs.append((Fraction(rng.randint(0, d_b)) if d_b else Fraction(0), Fraction(d_b),
                         Fraction(rng.randint(0, d_t)), Fraction(d_t)))
        if sum(s[1] for s in segs) == 0:
            continue
        R, R2, effects = ratio_effects(segs)
        assert sum((e[4] for e in effects), Fraction(0)) + sum((e[5] for e in effects), Fraction(0)) == R2 - R
        assert sum((e[0] for e in effects), Fraction(0)) == 1 == sum((e[1] for e in effects), Fraction(0))


async def test_simpson_each_channel_improves_but_the_mix_makes_the_whole_worse():
    rows = [["线上", "W41", 50, 500], ["线上", "W42", 90, 1000], ["线下", "W41", 10, 1000], ["线下", "W42", 4, 500]]
    result = await ratio(rows, label="退款率")
    assert result["verdict"] == {"code": "mix_driven", "subjects": [["线上"]], "direction": "up"}
    # 结构变化只说一遍：正文说了结构变化、点了线上，辛普森那一句只说各段和整体反着变
    assert result["phrase"] == ("退款率上升 2.3 个百分点（4.0% 到 6.3%），主要来自结构变化：线上占比从 33.3% 到 66.7%；"
                                "各段的退款率大多在下降，整体却上升")
    simpson = next(c for c in result["caveats"] if c["code"] == "simpson")
    assert simpson["text"] == "各段的退款率大多在下降，整体却上升"
    assert result["phrase"].count("结构") == 1
    values = result["values"]
    assert Fraction(values["mix_total"]) + Fraction(values["rate_total"]) == pytest.approx(Fraction(values["delta"]))
    assert result["table"]["columns"][:5] == ["channel", "n_base", "n_target", "d_base", "d_target"]


async def test_rate_driven_both_and_small_denominators():
    rows = [["线上", "W41", 50, 1000], ["线上", "W42", 80, 1000], ["线下", "W41", 20, 1000], ["线下", "W42", 26, 1000]]
    result = await ratio(rows, label="退款率")
    assert result["verdict"]["code"] == "rate_driven" and result["verdict"]["subjects"] == [["线上"]]
    assert result["phrase"] == "退款率上升 1.8 个百分点（3.5% 到 5.3%），主要来自各段自身的变化，线上影响最大"
    tiny = [["线上", "W41", 2, 20], ["线上", "W42", 3, 25], ["线下", "W41", 20, 1000], ["线下", "W42", 30, 1000]]
    small = await ratio(tiny, label="退款率")
    note = next(c for c in small["caveats"] if c["code"] == "small_denominator")
    assert note["text"] == "线上的 orders 不到 30，比率波动大，不参与多数判断"
    assert "分母小" in next(r for r in small["table"]["rows"] if r[0] == "线上")[-1]


async def test_ratio_refusals():
    zero = [["线上", "W41", 0, 0], ["线上", "W42", 3, 100], ["线下", "W41", 0, 0], ["线下", "W42", 2, 100]]
    result = await ratio(zero)
    assert result["reason_code"] == "zero_base" and result["reason"] == "基期的 orders 合计为 0，算不了比率"
    orphan = [["线上", "W41", 5, 0], ["线上", "W42", 3, 100], ["线下", "W41", 1, 100], ["线下", "W42", 2, 100]]
    assert (await ratio(orphan))["reason_code"] == "zero_base"


# --------------------------------------------------------------------------
# factor
# --------------------------------------------------------------------------


def _factor_snap(rows):
    return snap("Q1", "SELECT week, SUM(amount) AS gmv, COUNT(*) AS orders, ROUND(AVG(amount), 2) AS aov FROM orders "
                      "GROUP BY week", ["week", "gmv", "orders", "aov"], rows)


async def factor(rows, **params):
    params = {"query": "Q1", "period": "week", "base": "W41", "target": "W42", "product": "gmv",
              "factors": ["orders", "aov"], "label": "销售额", **params}
    return await analyze("decompose", "factor", snaps=[_factor_snap(rows)], **params)


def test_lmdi_contributions_add_up_to_the_change_within_float_error():
    rng = random.Random(3)
    for _ in range(200):
        k = rng.randint(2, 4)
        base = [rng.uniform(0.5, 500) for _ in range(k)]
        target = [b * rng.uniform(0.5, 1.8) for b in base]
        m_b = math.prod(base) * (1 + rng.uniform(-1e-6, 1e-6))          # 取整过的因子：乘积差一点点
        m_t = math.prod(target) * (1 + rng.uniform(-1e-6, 1e-6))
        contrib = lmdi(base, target, m_b, m_t)
        assert math.fsum(contrib) == pytest.approx(m_t - m_b, rel=1e-9, abs=1e-9 * max(m_b, m_t))
    same = lmdi([2.0, 3.0], [2.0, 3.0], 6.0, 6.0)
    assert same == [0.0, 0.0]


async def test_factor_driver_with_rounded_factors():
    rows = [["W41", 1000, 137, "7.30"], ["W42", 1320, 160, "8.25"]]
    result = await factor(rows)
    assert result["verdict"]["code"] in ("driver", "both")
    contrib = [Fraction(r[4]) for r in result["table"]["rows"]]
    assert float(sum(contrib)) == pytest.approx(320, rel=1e-9)
    clean = await factor([["W41", "1000", 100, "10"], ["W42", "1320", 120, "11"]])
    assert clean["verdict"] == {"code": "driver", "subjects": [["orders"]], "direction": "up"}
    assert clean["phrase"].startswith("销售额上升 320（+32.0%），主要来自 orders 的变化（贡献 +")
    assert clean["table"]["columns"] == ["factor", "base", "target", "pct", "contrib", "share"]


async def test_factor_call_errors_and_refusals():
    # 客单价写到两位小数：容差是它的半个末位（加上订单数的），差 1.5% 不是取整造成的
    error = await call_error("decompose", "factor", snaps=[_factor_snap([["W41", 1000, 100, "10.00"],
                                                                        ["W42", 1300, 120, "11.00"]])],
                             query="Q1", period="week", base="W41", target="W42", product="gmv",
                             factors=["orders", "aov"])
    assert error.code == "not_product" and "本期差" in error.message
    negative = await factor([["W41", 0, 0, 10], ["W42", 1320, 120, 11]])
    assert negative["reason_code"] == "non_positive"
    flat = await factor([["W41", 1000, 100, 10], ["W42", 1000, 125, 8]])
    assert flat["verdict"]["code"] == "flat"
    few = await call_error("decompose", "factor", snaps=[_factor_snap([["W41", 1, 1, 1], ["W42", 1, 1, 1]])],
                           query="Q1", period="week", base="W41", target="W42", product="gmv", factors=["orders"])
    assert few.code == "bad_factors"


async def test_ratio_headline_closure_and_drilling_into_a_channel():
    rows = [["线上", "W41", 50, 1000], ["线上", "W42", 80, 1000], ["线下", "W41", 20, 1000], ["线下", "W42", 26, 1000]]
    head = snap("Q9", "SELECT week, SUM(refund_flag) * 1.0 / COUNT(*) AS rate FROM orders GROUP BY week",
                ["week", "rate"], [["W41", "0.035"], ["W42", "0.053"]])
    params = {"query": "Q1", "by": "channel", "period": "week", "base": "W41", "target": "W42",
              "numerator": "refunds", "denominator": "orders", "label": "退款率"}
    closed = await analyze("decompose", "ratio", snaps=[_ratio_snap(rows), head],
                           headline={"base": "Q9.r0.rate", "target": "Q9.r1.rate"}, **params)
    assert closed["verdict"]["code"] == "rate_driven"
    off = snap("Q9", head["sql"], ["week", "rate"], [["W41", "0.035"], ["W42", "0.06"]])
    unclosed = await analyze("decompose", "ratio", snaps=[_ratio_snap(rows), off],
                             headline={"base": "Q9.r0.rate", "target": "Q9.r1.rate"}, **params)
    assert unclosed["verdict"]["code"] == "unclosed"
    assert unclosed["phrase"].startswith("按渠道拆开后各段合起来的比率和总数对不上")
    # 线上按地区下钻：各地区的分子、分母之和要等于上一层线上那一段的
    region = snap("Q2", "SELECT region, week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders "
                        "WHERE channel = '线上' GROUP BY 1, 2", ["region", "week", "refunds", "orders"],
                  [["华东", "W41", 30, 600], ["华东", "W42", 55, 600], ["华北", "W41", 20, 400], ["华北", "W42", 25, 400]])
    child = await analyze("decompose", "ratio", snaps=[region], parents={"A1": closed},
                          **{**params, "query": "Q2", "by": "region", "parent": {"of": "A1", "key": ["线上"]}})
    assert child["verdict"]["code"] != "unclosed" and child["params"]["depth"] == 1
    wrong = await call_error("decompose", "additive", snaps=[weekly()], parents={"A1": closed}, query="Q2",
                             by="region", period="week", base="2026-W41", target="2026-W42", measure="gmv",
                             parent={"of": "A1", "key": ["线上"]})
    assert wrong.code == "parent_not_decompose"


async def test_the_same_query_cannot_be_both_periods():
    error = await call_error("decompose", "additive", snaps=[weekly()], base_query="Q2", target_query="Q2",
                             by="region", measure="gmv")
    assert error.code == "same_query"
    error = await call_error("comparable", snaps=[weekly()], base_query="Q2", target_query="Q2")
    assert error.code == "same_query"


async def test_dimension_columns_keep_the_type_recorded_at_query_time():
    """文本型的码值（门店编码 "1203"）在结果表里仍是 text，渲染时不会写成「1,203」；查询时没记类型的维度列不写。"""
    sql = "SELECT store_code, week, SUM(amount) AS gmv FROM orders GROUP BY 1, 2"
    rows = [["1203", "W1", 500], ["1203", "W2", 300], ["0871", "W1", 200], ["0871", "W2", 260]]
    typed = snap("Q2", sql, ["store_code", "week", "gmv"], rows,
                 types={"store_code": "text", "week": "text", "gmv": "number"})
    result = await analyze("decompose", "additive", snaps=[typed], query="Q2", by="store_code", period="week",
                           base="W1", target="W2", measure="gmv")
    assert result["table"]["column_types"]["store_code"] == "text"
    assert [r[0] for r in result["table"]["rows"]] == ["1203", "0871"]
    untyped = snap("Q2", sql, ["store_code", "week", "gmv"], rows)
    result = await analyze("decompose", "additive", snaps=[untyped], query="Q2", by="store_code", period="week",
                           base="W1", target="W2", measure="gmv")
    assert "store_code" not in result["table"]["column_types"]
    assert result["table"]["column_types"]["delta"] == "number"
    listed = snap("Q2", sql, ["store_code", "week", "gmv"], rows, types=None, column_types=["text", "text", "number"])
    result = await analyze("decompose", "additive", snaps=[listed], query="Q2", by="store_code", period="week",
                           base="W1", target="W2", measure="gmv")
    assert result["table"]["column_types"]["store_code"] == "text"          # 按列序的列表也认
    ratio_result = await ratio([["线上", "W41", 50, 1000], ["线上", "W42", 80, 1000], ["线下", "W41", 20, 1000],
                                ["线下", "W42", 26, 1000]])
    ratio_types = ratio_result["table"]["column_types"]
    assert ratio_types["flag"] == "text" and ratio_types["mix"] == "number" and "channel" not in ratio_types
    factor_result = await factor([["W41", "1000", 100, "10"], ["W42", "1320", 120, "11"]])
    assert factor_result["table"]["column_types"] == {"factor": "text", "base": "number", "target": "number",
                                                       "pct": "number", "contrib": "number", "share": "number"}



async def test_a_label_cannot_carry_a_judgment_into_the_phrase():
    cut = await additive(label="销售额，主要来自华南")
    assert cut["params"]["label"] == "销售额" and cut["phrase"].startswith("销售额下降 194")
    assert "华南" not in cut["phrase"]
    judged = await additive(label="主要来自华南的销售额")
    assert "label" not in judged["params"]                       # 消成空了，退回目录里的叫法
    assert judged["phrase"].startswith("销售额下降 194") and "华南" not in judged["phrase"]
    # 消过一遍会变的叫法：复算拿记下的叫法再消，判定语逐字相同（analyze 核对复算逐字段相同）
    marked = await additive(label="销售额[ [v:Q1.r0.gmv] ]")
    assert marked["params"]["label"] == "销售额" and marked["phrase"].startswith("销售额下降 194")
