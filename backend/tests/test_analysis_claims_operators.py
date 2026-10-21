"""说法核对挂真算子算出来的结论（app/analysis，第 1 波 W1-a）：判定、点名的段、方向、新高新低、连续、抵消、结构、差异的两组、
算不了，都按算子真给的形状核（tests/analysis_samples 的样例：虚构零售库 shop）。再经组装和复核走一遍：判定语 [[a:A1]] 和
具名标量 [[a:A1.键]] 整段不扫，[[see:A1]] 当依据。"""
from __future__ import annotations

import pytest

from app.engine import analysis_claims as ac
from app.engine.evidence import build_catalog, compose_doc, make_eid, verify_doc
from tests.analysis_samples import analyze, fixed_prober, snap

SQL = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week"
TYPES = {"region": "text", "week": "text", "gmv": "number"}
WEEKLY = [["华东", "2026-W41", 820], ["华东", "2026-W42", 700], ["华北", "2026-W41", 600], ["华北", "2026-W42", 560],
          ["华南", "2026-W41", 700], ["华南", "2026-W42", 666], ["西南", "2026-W41", 100], ["西南", "2026-W42", 100]]


async def additive(rows=WEEKLY, **params):
    s = snap("Q2", SQL, ["region", "week", "gmv"], rows, types=TYPES)
    return await analyze("decompose", "additive", snaps=[s], query="Q2", by=["region"], period="week",
                         base="2026-W41", target="2026-W42", measure="gmv", label="销售额", **params)


def weekly_rows(base: dict, target: dict) -> list[list]:
    return [[k, "2026-W41", v] for k, v in base.items()] + [[k, "2026-W42", v] for k, v in target.items()]


def said(text: str, **results) -> list[str]:
    """一句话挂了 results 这几份结论：违规的代码。"""
    return [v["code"] for v in ac.check_sentence(ac.Sentence(text=text, analyses=list(results)), results)]


async def test_an_additive_driver_backs_its_segment_and_nothing_else():
    a1 = await additive()
    assert a1["verdict"] == {"code": "driver", "subjects": [["华东"]], "direction": "down"}
    assert said("销售额下降主要来自华东。", A1=a1) == []
    assert said("华东拖累了整体。", A1=a1) == []
    assert said("销售额下降主要来自华南。", A1=a1) == [ac.MISMATCH]
    assert said("华北带动了增长。", A1=a1) == [ac.MISMATCH], "华北下降、占比不到一半，不能说带动"
    assert said("各地区普遍下降。", A1=a1) == [ac.MISMATCH]


async def test_a_dispersed_change_backs_only_dispersion():
    a1 = await additive(weekly_rows({"华东": 100, "华北": 100, "华南": 100, "西南": 100},
                                    {"华东": 75, "华北": 75, "华南": 75, "西南": 75}))
    assert a1["verdict"]["code"] == "dispersed"
    assert said("销售额下降分散在各地区。", A1=a1) == []
    assert said("销售额下降主要来自华东。", A1=a1) == [ac.MISMATCH]


async def test_a_flat_total_with_opposite_segments_backs_an_offset():
    a1 = await additive(weekly_rows({"华东": 500, "华南": 500, "华北": 300}, {"华东": 550, "华南": 450, "华北": 300}))
    assert a1["verdict"]["code"] == "flat"
    assert said("华东和华南此消彼长。", A1=a1) == []
    assert said("销售额基本持平，主要来自华东。", A1=a1) == [ac.MISMATCH]


async def test_a_ratio_decomposition_backs_structure_and_its_main_source():
    s = snap("Q1", "SELECT channel, week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY 1, 2",
             ["channel", "week", "refunds", "orders"],
             [["线上", "W41", 50, 500], ["线上", "W42", 90, 1000], ["线下", "W41", 10, 1000], ["线下", "W42", 4, 500]])
    a1 = await analyze("decompose", "ratio", snaps=[s], query="Q1", by="channel", period="week", base="W41",
                       target="W42", numerator="refunds", denominator="orders", label="退款率")
    assert a1["verdict"] == {"code": "mix_driven", "subjects": [["线上"]], "direction": "up"}
    assert said("退款率上升来自结构变化。", A1=a1) == []
    assert said("退款率上升主要来自结构变化，线上占比变大。", A1=a1) == [], "判定语就是这么说的"
    assert said("退款率上升主要来自线下。", A1=a1) == [ac.MISMATCH]


async def test_a_factor_decomposition_names_its_factor():
    s = snap("Q1", "SELECT week, SUM(amount) AS gmv, COUNT(*) AS orders, ROUND(AVG(amount), 2) AS aov FROM orders "
                   "GROUP BY week", ["week", "gmv", "orders", "aov"],
             [["W41", 10000, 100, 100], ["W42", 15000, 150, 100]])
    a1 = await analyze("decompose", "factor", snaps=[s], query="Q1", period="week", base="W41", target="W42",
                       product="gmv", factors=["orders", "aov"], label="销售额")
    assert a1["verdict"]["code"] == "driver"
    [[factor]] = a1["verdict"]["subjects"]
    assert said(f"销售额增长主要来自{factor}的增长。", A1=a1) == []
    other = next(r[0] for r in a1["table"]["rows"] if r[0] != factor)
    assert said(f"销售额增长主要来自{other}。", A1=a1) == [ac.MISMATCH]


NOISE = [100, 102, 98, 101, 99, 103, 100, 97, 102, 101, 99, 100, 98, 103, 101, 100, 99, 102, 100]
SERIES_SQL = "SELECT DATE(order_date, 'weekday 0', '-6 days') AS week, COUNT(*) AS orders FROM orders GROUP BY 1"


def weeks(values):
    from datetime import date, timedelta

    start = date(2026, 10, 12) - timedelta(days=7 * (len(values) - 1))
    return [[(start + timedelta(days=7 * i)).isoformat(), v] for i, v in enumerate(values)]


async def series(op: str, values, **params):
    probe = fixed_prober({("orders", "order_date"): ("2025-01-01", "2026-10-20")})
    s = snap("Q1", SERIES_SQL, ["week", "orders"], weeks(values), types={"week": "text", "orders": "number"})
    return await analyze(op, snaps=[s], probe=probe, query="Q1", period="week", measure="orders", label="订单数",
                         **params)


async def test_a_baseline_backs_anomalies_records_and_normal_swings_in_the_right_direction():
    far = await series("baseline", NOISE + [70])
    assert far["verdict"] == {"code": "far_outside_band", "subjects": [], "direction": "down"}
    assert said("订单数本周骤降。", A1=far) == []
    assert said("订单数本周大幅下降，创历史新低。", A1=far) == []
    assert said("订单数本周异常增长。", A1=far) == [ac.MISMATCH]
    assert said("订单数创历史新高。", A1=far) == [ac.MISMATCH]
    assert said("订单数属于正常波动。", A1=far) == [ac.MISMATCH]
    within = await series("baseline", NOISE + [101])
    assert within["verdict"]["code"] == "within_band"
    assert said("订单数属于正常波动，并未出现明显异常。", A1=within) == []
    assert said("订单数异常。", A1=within) == [ac.MISMATCH]


async def test_a_trend_backs_its_direction_and_runs_only_when_long_enough():
    rising = await series("trend", [100 + 3 * i + (i % 3) for i in range(12)])
    assert rising["verdict"]["code"] == "rising" and rising["values"]["run_direction"] == "up"
    assert said("订单数持续上升。", A1=rising) == []
    assert said("订单数已连续多周上升。", A1=rising) == []
    assert said("订单数持续下降。", A1=rising) == [ac.MISMATCH]
    assert said("订单数没有明显趋势。", A1=rising) == [ac.MISMATCH]
    short = dict(rising, values={**rising["values"], "run_length": "2"})
    assert said("订单数连续上升。", A1=short) == [ac.MISMATCH]


async def test_a_difference_backs_the_higher_group_in_either_order():
    rates = snap("Q1", "SELECT region, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY region",
                 ["region", "refunds", "orders"], [["西南", 80, 1000], ["华东", 40, 1000]])
    differs = await analyze("differ", "rate", snaps=[rates], query="Q1", by="region", numerator="refunds",
                            denominator="orders", label="退款率")
    assert differs["verdict"] == {"code": "differs", "subjects": [["西南"], ["华东"]], "direction": "up"}
    assert said("西南的退款率显著高于华东。", A1=differs) == []
    assert said("华东的退款率明显低于西南。", A1=differs) == []
    assert said("西南的退款率显著低于华东。", A1=differs) == [ac.MISMATCH]
    small = snap("Q1", "SELECT region, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY region",
                 ["region", "refunds", "orders"], [["西南", 2, 40], ["华东", 5, 230]])
    nothing = await analyze("differ", "rate", snaps=[small], query="Q1", by="region", numerator="refunds",
                            denominator="orders", label="退款率")
    assert nothing["verdict"]["code"] == "no_evidence"
    assert said("两地的退款率差异不大。", A1=nothing) == []
    assert said("西南的退款率显著高于华东。", A1=nothing) == [ac.MISMATCH]


async def test_a_refused_baseline_backs_no_judgement():
    short = await series("baseline", [100, 102, 98, 70])
    assert short["status"] == "refused" and short["verdict"] is None and short["reason_code"] == "history_short"
    [miss] = ac.check_sentence(ac.Sentence(text="订单数本周异常下降。", analyses=["A1"]), {"A1": short})
    assert miss["code"] == ac.MISMATCH and "没有做成" in miss["message"]


# --------------------------------------------------------------------------
# 经组装和复核：判定语、具名标量不扫，[[see:A1]] 当依据
# --------------------------------------------------------------------------


@pytest.fixture
async def driver_catalog():
    a1 = await additive()
    catalog = build_catalog(nodes={"caliber": {
        "kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "artifact": "f" * 64,
        "metrics": [{"id": "gmv", "name": "销售额", "value": 2026, "unit": "元"}]}})
    artifact = "e" * 64
    catalog["A1"] = {"alias": "A1", "kind": "analysis", "artifact": artifact, "eid": make_eid("analysis", artifact, {}),
                     "locator": {}, "op": "decompose", "mode": "additive", "status": "ok", "item": "drop",
                     "inputs": [], "result": a1}
    return catalog


@pytest.mark.parametrize("raw", [
    "[[a:A1]]。",
    "[[a:A1]]：下降主要来自华东。",
    "华东占变化的 [[a:A1.top_share|pct]]，下降主要来自华东。[[see:A1]]",
    "下降主要来自华东。[[see:A1.r0]]",
    "下降主要来自华东。[[see:a:A1]]",
])
def test_phrases_and_scalars_are_not_scanned_and_see_markers_bind(driver_catalog, raw):
    doc = compose_doc(raw, driver_catalog, analysis_claims="check")
    assert [v for v in doc["violations"] if v["code"] in ac.CODES] == [], doc["violations"]
    assert [v for v in verify_doc(doc, driver_catalog)["violations"] if v["code"] in ac.CODES] == []


def test_a_contradicting_sentence_next_to_the_phrase_is_caught(driver_catalog):
    doc = compose_doc("[[a:A1]]。下降主要来自华南。[[see:A1]]", driver_catalog, analysis_claims="check")
    [miss] = [v for v in doc["violations"] if v["code"] in ac.CODES]
    assert miss["code"] == ac.MISMATCH and miss["analyses"] == ["A1"] and miss["unit"] and miss["segment"]
    assert doc["markdown"][slice(*miss["span"])] == "主要来自"
    # 存进文档的目录只留摘要：按工件取不回结论时只能当作没挂（交给复核的是重建的整份目录，这里看清楚退路）
    stored = {k: v for k, v in doc["catalog"]["A1"].items() if k != "result"}
    assert ac.results_of({"A1": stored}) == {}
    assert ac.results_of({"A1": stored}, lambda alias, entry: driver_catalog["A1"]["result"])["A1"]["verdict"]["code"] \
        == "driver"


def test_a_factor_source_named_after_the_verb_is_recognised():
    """V5（真实模型验收 6/2）：因子拆解正确的主来源被判不符。「这波涨幅主要由订单量放大带来」里主来源写在判断词后面
    （「订单量」），以前只看判断词前面，认成没点名、或者错认到前一个分句的「客单价」上。"""
    result = {"op": "decompose", "mode": "factor", "status": "ok",
              "verdict": {"code": "driver", "subjects": [["订单数"]], "direction": "up"},
              "table": {"columns": ["factor", "base", "target", "pct", "contrib", "share"],
                        "labels": {"factor": "因子", "share": "占变化的比重"},
                        "rows": [["订单数", "900", "1100", "0.2222", "200", "0.998"],
                                 ["客单价", "160", "160", "0", "0", "0.002"]]},
              "values": {"top_share": "0.998"}, "phrase": "销售额上升 200，主要来自订单数的变化"}
    text = "这波涨幅主要由订单量放大带来，客单价基本没动。"
    sentence = ac.Sentence(text=text, shown=text, base=0, analyses=["A2"], names=[None] * (len(text) + 1),
                           pieces=[], unit=0, phrases=[], inputs=False, quoted=False)
    assert [v["code"] for v in ac.check_sentence(sentence, {"A2": result})] == []
