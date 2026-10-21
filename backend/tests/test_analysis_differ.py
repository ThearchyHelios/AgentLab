"""差异检验（app/analysis/differ.py，细案 01 §5）：比率小样本走 Fisher、均值 Welch、Holm、rows 敏感性翻转。每个用例都顺带核对
复算和运行逐字段相同。"""
from __future__ import annotations

import math
from fractions import Fraction

import pytest

from app.analysis import stats
from tests.analysis_samples import analyze, call_error, snap

RATE_SQL = "SELECT region, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY region"


def rates(rows):
    return snap("Q1", RATE_SQL, ["region", "refunds", "orders"], rows)


async def rate(rows, **params):
    params = {"query": "Q1", "by": "region", "numerator": "refunds", "denominator": "orders", "label": "退款率", **params}
    return await analyze("differ", "rate", snaps=[rates(rows)], **params)


async def test_small_samples_use_fisher_and_say_it_may_be_noise():
    result = await rate([["西南", 2, 40], ["华东", 5, 230]])
    assert result["method"] == "differ.rate@1"
    assert result["verdict"] == {"code": "no_evidence", "subjects": [["西南"], ["华东"]], "direction": None}
    assert result["values"]["test"] == "fisher" and result["values"]["n_a"] == "40" and result["values"]["n_b"] == "230"
    assert Fraction(result["values"]["p"]) == pytest.approx(stats.fisher_exact(2, 38, 5, 225), rel=1e-9)
    assert result["phrase"] == ("西南和华东的退款率差异可能只是样本波动（样本量 40、230）；样本少（有格子的期望频数不到 5），"
                                "用了精确检验")
    assert "p 值" not in result["phrase"] and "显著" not in result["phrase"]
    assert result["settings"] == {"differ_alpha": 0.05, "min_expected": 5}


async def test_large_samples_use_the_pooled_z_test():
    result = await rate([["西南", 80, 1000], ["华东", 40, 1000]])
    assert result["verdict"] == {"code": "differs", "subjects": [["西南"], ["华东"]], "direction": "up"}
    z, p = stats.pooled_z(80, 1000, 40, 1000)
    assert Fraction(result["values"]["p"]) == pytest.approx(p, rel=1e-9) and result["values"]["test"] == "z"
    low, high = stats.newcombe(80, 1000, 40, 1000)
    assert Fraction(result["values"]["ci_lo"]) == pytest.approx(low, rel=1e-9)
    assert result["values"]["a"] == "0.08" and result["values"]["diff"] == "0.04"
    assert result["phrase"] == "西南的退款率（8.0%）高于华东（4.0%），差异站得住"
    lower = await rate([["西南", 80, 1000], ["华东", 40, 1000]], pairs=[["华东", "西南"]])
    assert lower["verdict"]["direction"] == "down" and lower["phrase"].startswith("华东的退款率（4.0%）低于西南（8.0%）")


async def test_several_groups_each_against_the_rest_with_holm():
    rows = [["西南", 80, 1000], ["华东", 40, 1000], ["华南", 42, 1000], ["华北", 38, 1000]]
    result = await rate(rows)
    table = result["table"]
    assert [r[0] for r in table["rows"]] == ["西南", "华东", "华南", "华北"]
    assert table["rows"][0][1] == "其余各地区"
    # 对照组那一列有「其余各地区」这样的叫法：写 text；没记类型的分组列（组那一列）不写
    assert table["column_types"]["b"] == "text" and "a" not in table["column_types"]
    assert table["column_types"]["p"] == "number" and table["column_types"]["test"] == "text"
    raw = [Fraction(r[10]) for r in table["rows"]]
    adjusted = [Fraction(r[11]) for r in table["rows"]]
    assert all(a >= p for a, p in zip(adjusted, raw))
    assert adjusted == pytest.approx(stats.holm([float(p) for p in raw]), rel=1e-9)
    assert result["verdict"]["code"] == "differs" and result["verdict"]["subjects"] == [["西南"], ["其余各地区"]]
    assert any(c["code"] == "multiple_comparisons" and c["text"] == "比了 4 对，已按次数校正" for c in result["caveats"])
    assert result["phrase"] == "比了 4 对，1 对差异站得住：西南的退款率（8.0%）高于其余各地区（4.0%）"
    ref = await rate(rows, reference="华东")
    assert [r[0] for r in ref["table"]["rows"]] == ["西南", "华南", "华北"] and ref["params"]["reference"] == "华东"


async def test_rate_call_errors():
    assert (await call_error("differ", "rate", snaps=[rates([["西南", 5, 4], ["华东", 1, 9]])], query="Q1", by="region",
                             numerator="refunds", denominator="orders")).code == "not_count"
    assert (await call_error("differ", "rate", snaps=[rates([["西南", 1.5, 4], ["华东", 1, 9]])], query="Q1",
                             by="region", numerator="refunds", denominator="orders")).code == "not_count"
    unknown = await call_error("differ", "rate", snaps=[rates([["西南", 1, 4], ["华东", 1, 9]])], query="Q1",
                               by="region", numerator="refunds", denominator="orders", pairs=[["西南", "东北"]])
    assert unknown.code == "unknown_group" and "西南、华东" in unknown.message
    dup = await call_error("differ", "rate", snaps=[rates([["西南", 1, 4], ["西南", 1, 9]])], query="Q1", by="region",
                           numerator="refunds", denominator="orders")
    assert dup.code == "duplicate_rows"


async def test_means_from_sums_and_squares_use_welch():
    sql = "SELECT channel, SUM(amount) AS s, SUM(amount * amount) AS ss, COUNT(*) AS n FROM orders GROUP BY channel"
    rows = [["线上", 5000, 300000, 100], ["线下", 4000, 200000, 100]]
    result = await analyze("differ", "mean", snaps=[snap("Q1", sql, ["channel", "s", "ss", "n"], rows)], query="Q1",
                           by="channel", sum="s", sumsq="ss", n="n", label="客单价")
    va = (300000 - 5000 ** 2 / 100) / 99
    vb = (200000 - 4000 ** 2 / 100) / 99
    expected = stats.welch(50, va, 100, 40, vb, 100)
    assert Fraction(result["values"]["p"]) == pytest.approx(expected.p, rel=1e-9)
    assert result["verdict"]["code"] == "differs" and result["values"]["test"] == "welch"
    assert result["phrase"] == "线上的客单价（50）高于线下（40），差异站得住"
    assert result["method"] == "differ.welch@1" and result["settings"] == {"differ_alpha": 0.05, "min_n": 30}


async def test_means_from_mean_sd_and_n_and_small_samples():
    sql = "SELECT channel, avg_amount, sd_amount, n FROM channel_stats"
    rows = [["线上", 52, 12, 12], ["线下", 50, 11, 400]]
    result = await analyze("differ", "mean", snaps=[snap("Q1", sql, ["channel", "avg_amount", "sd_amount", "n"], rows)],
                           query="Q1", by="channel", mean="avg_amount", sd="sd_amount", n="n", label="客单价")
    assert result["verdict"]["code"] == "no_evidence"
    note = next(c for c in result["caveats"] if c["code"] == "small_sample")
    assert note["text"] == "有一组的样本数不到 30（线上 12），结论不稳"
    assert result["phrase"].startswith("线上和线下的客单价差异可能只是样本波动（样本量 12、400）")


async def test_rows_mode_flags_a_single_value_that_flips_the_verdict():
    base = [["线下", v] for v in [40, 60] * 200]
    online = [["线上", f"{v:.1f}"] for v in [41.3, 61.3] * 200]
    online[-1] = ["线上", "231.3"]
    s = snap("Q1", "SELECT channel, amount FROM orders", ["channel", "amount"], online + base)
    result = await analyze("differ", "rows", snaps=[s], query="Q1", by="channel", value="amount", label="客单价")
    assert result["method"] == "differ.welch_rows@1"
    assert result["verdict"]["code"] == "differs"
    note = next(c for c in result["caveats"] if c["code"] == "outlier_sensitive")
    assert note["text"] == "去掉最大的一笔后，差异不再站得住"
    assert note["text"] in result["phrase"]
    hidden = [["线上", 50 + (i % 7)] for i in range(30)] + [["线下", 48 + (i % 5)] for i in range(30)] + [["线上", 400]]
    s = snap("Q1", "SELECT channel, amount FROM orders", ["channel", "amount"], hidden)
    flipped = await analyze("differ", "rows", snaps=[s], query="Q1", by="channel", value="amount", label="客单价")
    assert flipped["verdict"]["code"] == "no_evidence"
    assert any(c["text"] == "去掉最大的一笔后，差异变得站得住" for c in flipped["caveats"])


async def test_two_periods_of_the_same_group():
    sql = "SELECT week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders WHERE region = '西南' GROUP BY week"
    s = snap("Q1", sql, ["week", "refunds", "orders"], [["W41", 30, 1000], ["W42", 60, 1000], ["W40", 31, 1000]])
    result = await analyze("differ", "rate", snaps=[s], query="Q1", period="week", base="W41", target="W42",
                           numerator="refunds", denominator="orders", label="退款率")
    assert result["verdict"] == {"code": "differs", "subjects": [["W42"], ["W41"]], "direction": "up"}
    assert result["inputs"][0]["rows"] == [0, 1]
    assert result["phrase"] == "W42 的退款率（6.0%）高于 W41（3.0%），差异站得住"


async def test_a_group_with_one_value_cannot_be_tested():
    s = snap("Q1", "SELECT channel, amount FROM orders", ["channel", "amount"], [["线上", 5], ["线下", 4], ["线下", 6]])
    result = await analyze("differ", "rows", snaps=[s], query="Q1", by="channel", value="amount")
    assert result["verdict"]["code"] == "no_evidence" and result["values"]["p"] is None
    assert result["table"]["rows"][0][-1] == "样本不够，算不了"
    assert math.isfinite(float(result["values"]["diff"]))
