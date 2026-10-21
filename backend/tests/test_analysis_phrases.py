"""判定语、数的写法、label 消毒、判定标准（app/analysis/phrases.py、numbers.py）：一种说法，只在这里定义。"""
from __future__ import annotations

from fractions import Fraction

from app.analysis import phrases as ph
from app.analysis.contract import DEFAULTS
from app.analysis.numbers import canon, fixed, round_sig, to_fraction


def test_canonical_decimal_strings_are_plain_and_stable():
    assert canon(Fraction(-120)) == "-120"
    assert canon("2220.00") == "2220"                    # 去掉多余的 0
    assert canon(0.1 + 0.2) == "0.3"                      # 浮点留 12 位有效数字
    assert canon(Fraction(1, 3)) == "0.333333333333"
    assert canon(Fraction(7, 16)) == "0.4375"             # 除得尽的照写
    assert canon(-0.0) == "0" and canon(Fraction(0)) == "0"
    assert canon(1e-30) == "0.000000000000000000000000000001"     # 不带指数
    assert canon(1e20) == "100000000000000000000"
    assert canon("123456789012345678.25") == "123456789012345678.25"   # 快照里的数和它们的和差照原样
    assert canon(None) is None and canon("abc") is None and canon(True) is None
    assert to_fraction(0.1) == Fraction(1, 10) and to_fraction("45678.50") == Fraction(4567850, 100)
    assert round_sig(Fraction(2, 3), 3) == Fraction(667, 1000)


def test_fixed_rounds_half_up_like_render_cell():
    assert fixed(Fraction(2675, 1000), 2) == "2.68"        # 2.675 不走浮点的 round（会得 2.67）
    assert fixed(Fraction(-5, 100), 1, strip=False) == "-0.1"
    assert fixed(Fraction(-4, 100), 1, strip=False) == "0.0"     # 不写 -0.0
    assert fixed(Fraction(123456789, 100), 2, group=True) == "1,234,567.89"


def test_number_formats_in_phrases():
    assert ph.pct(Fraction(-87, 1000)) == "-8.7%" and ph.pct(Fraction(3, 100)) == "+3.0%"
    assert ph.pct(0) == "0.0%" and ph.pct(Fraction(6186, 10000), signed=False) == "61.9%"
    assert ph.pct(Fraction(4, 100000)) == "+0.004%"           # 不是 0 的不写成 0.0%
    assert ph.amount(Fraction(1234567891, 1000)) == "1,234,567.89"
    assert ph.amount(Fraction(120)) == "120" and ph.amount(Fraction(-120), signed=True) == "-120"
    assert ph.amount(Fraction(40), signed=True) == "+40"
    assert ph.amount(Fraction(21, 1000)) == "0.021" and ph.amount(Fraction(4567, 10 ** 7)) == "0.00046"
    assert ph.change(Fraction(-194), Fraction(2220)) == "下降 194（-8.7%）"
    assert ph.change(Fraction(120), Fraction(0)) == "上升 120"            # 基期为 0 不给百分比
    assert ph.change(0, 5) == "没有变化"
    assert ph.ratio_change(Fraction(21, 1000), Fraction(25, 1000), proportion=True) == "上升 0.4 个百分点（2.1% 到 2.5%）"
    assert ph.ratio_change(Fraction(82), Fraction(852, 10), proportion=False) == "上升 3.2（+3.9%）"
    assert ph.threshold_pct(0.005) == "0.5%" and ph.share_words(0.5) == "一半" and ph.share_words(0.6) == "60%"


def test_labels_are_scrubbed_of_numbers_units_and_markers():
    assert ph.clean_label("销售额（同比 +30%）") == "销售额（同比）"
    assert ph.clean_label("近三成客户的退款率") == "近客户的退款率"
    assert ph.clean_label("销售额[[v:Q1.r0.gmv]]") == "销售额"
    assert ph.clean_label("¥ 销售额 3.5万") == "销售额"
    assert ph.clean_label("gmv_2026") == "gmv_"
    assert len(ph.clean_label("销售额" * 30)) == ph.LABEL_MAX
    assert ph.clean_label("30%") == "" and ph.clean_label(None) == ""
    # 消毒后为空就退回下一个来源（目录里的叫法、列名）
    assert ph.pick_label("12.5%", None, "gmv") == "gmv"
    assert ph.pick_label("", "实收金额", "gmv") == "实收金额"
    assert ph.pick_label("2026", None, "2026") == "这一列"


def test_scrubbing_a_scrubbed_label_changes_nothing():
    # 去掉一处可能拼出新的一处：去完数字的「销售额[[v:Q.r.gmv]]」又是一个引用标记，要接着去
    assert ph.clean_label("销售额[ [v:Q1.r0.gmv] ]") == "销售额"
    assert ph.clean_label("[ [see:A1] ]销售额") == "销售额" and ph.clean_label("销售额[1[v:x]2]") == "销售额"
    for text in ("销售额[ [v:Q1.r0.gmv] ]", "销售额 [ [ a:A1 ] ]", "销售额（ ）（3）", "销售额（同比 +30%，3.5万）",
                 "近三成客户的退款率", "【线上】销售额", "退款率 (%)", "销售额（", "  ，销售额。", "一[二]三",
                 "销售额" * 30, "[[a:A1]]主要来自[[v:Q1.r0.region]]"):
        once = ph.clean_label(text)
        assert ph.clean_label(once) == once, text


def test_labels_stop_at_the_first_clause_and_judgments_fall_back():
    assert ph.clean_label("销售额，主要来自华东") == "销售额"
    assert ph.clean_label("订单数; 销售额") == "订单数" and ph.clean_label("订单数、销售额") == "订单数"
    assert ph.clean_label("销售额（含税，元）") == "销售额"               # 截完没配上对的括号也不要
    # 判定语不受说法核对：叫法里有判断词的整个不要，退回下一个来源（目录叫法、当叫法用的列名同样处理）
    assert ph.clean_label("销售额主要来自华东") == "" and ph.clean_label("历史新高的销售额") == ""
    assert ph.pick_label("销售额主要来自华东", "实收金额", "gmv") == "实收金额"
    assert ph.pick_label(None, "历史新高的销售额", "gmv") == "gmv"
    assert ph.pick_label(None, None, "主要来自华东") == "这一列"


def test_numbers_stuffed_into_a_label_never_reach_the_phrase():
    label = ph.pick_label("销售额（同比 +30%，3.5万）", "销售额", "gmv")
    phrase = ph.decompose_driver(label, ph.change(-194, 2220), "华东", "-120", Fraction(6186, 10000))
    assert "30" not in phrase and "3.5" not in phrase and "万" not in phrase
    assert phrase == "销售额（同比）下降 194（-8.7%），主要来自华东（-120，占 61.9%）"


def test_spacing_between_chinese_and_ascii():
    assert ph.say("gmv", "下降 194") == "gmv 下降 194"
    assert ph.say("主要来自", "online", "（", "-120") == "主要来自 online（-120"
    assert ph.say("销售额", "下降") == "销售额下降"
    assert ph.key_text(["华东", "线上"]) == "华东·线上" and ph.key_text([None]) == "（空）"
    assert ph.names(["华东", "华北", "华南"], 2) == "华东、华北等 3 段"


def test_phrase_templates():
    assert ph.decompose_concentrated("销售额", "下降 194（-8.7%）", "华东、华北", Fraction(85, 100)) == \
        "销售额下降 194（-8.7%），集中在华东、华北（合计占 85.0%）"
    assert ph.decompose_dispersed("销售额", "下降 194（-8.7%）", "地区", 0.5) == \
        "销售额下降 194（-8.7%），分散在各地区，没有哪一段占到一半"
    assert ph.decompose_dispersed("销售额", "下降 194（-8.7%）", "地区", 0.6).endswith("没有哪一段占到 60%")
    assert ph.decompose_flat("销售额", Fraction(2, 1000), ("华东", "+120", "华北", "-118")) == \
        "销售额基本持平（+0.2%），内部此消彼长：华东 +120、华北 -118"
    assert ph.decompose_unclosed("地区", Fraction(-12), ratio=False) == "按地区拆开后各段之和和总数对不上（差 12），不判断变化来自哪里"
    assert ph.baseline_phrase("outside_band", "销售额", "本期变化 -8.7%", 12, "周", "-6.1% 至 +5.3%") == \
        "销售额本期变化 -8.7%，超出过去 12 周的常规波动范围（-6.1% 至 +5.3%）"
    assert ph.trend_phrase("rising", "线上销售额", 10, "周", "周", "+3.0%", (4, "up")) == \
        "过去 10 周线上销售额呈上升趋势（每周约 +3.0%），已连续 4 周上升"
    assert ph.trend_phrase("no_trend", "销售额", 8, "个月", "月", None, None) == "过去 8 个月销售额没有明显的上升或下降趋势"
    assert ph.differ_phrase([ph.differ_pair("西南", "华东", "退款率", "5.0%", "2.1%", True)], 1) == \
        "西南的退款率（5.0%）高于华东（2.1%），差异站得住"
    assert ph.no_evidence_phrase("西南", "华东", "退款率", ("40", "230"), 1) == "西南和华东的退款率差异可能只是样本波动（样本量 40、230）"
    assert ph.comparable_phrase("incomparable", ["本期数据只到 2026-10-15，还差 3 天"], []) == \
        "两期不可比：本期数据只到 2026-10-15，还差 3 天"
    assert ph.comparable_phrase("comparable", [], ["整期"]) == "两期可比，整期核对不了"
    assert ph.refused("baseline", "只有 5 期历史，至少要 8 期") == "波动基线没有做成：只有 5 期历史，至少要 8 期"
    assert ph.current_text(Fraction(-31, 1000), basis="change", lag=7, grain="day") == "比上周同一天变化 -3.1%"
    assert ph.current_text(Fraction(1, 10), basis="change", lag=52, grain="week") == "同比变化 +10.0%"


def test_caveats_follow_the_phrase_at_most_two_in_order():
    caveats = [ph.caveat("measure_unlabeled", "目录没标"), ph.caveat("null_values", "西南为空"),
               ph.caveat("offset", "互相抵消"), ph.caveat("comparability", "两期有条件可比：周末不同")]
    assert ph.with_caveats("判定", caveats) == "判定；两期有条件可比：周末不同；互相抵消"
    assert ph.with_caveats("判定", caveats, skip=("offset",)) == "判定；两期有条件可比：周末不同；西南为空"
    assert ph.with_caveats("判定", [ph.caveat("headline_basis", "以各段之和为总数")]) == "判定"


def test_criteria_are_written_with_the_actual_thresholds():
    text = ph.criterion_additive(DEFAULTS, top_k_max=2, summed=True)
    assert text == ("一段占总变化的一半以上算主要来源，最多前 2 段合计占 80% 以上算集中，总变化不到基期的 0.5% 算基本持平，"
                    "以各段之和为总数")
    custom = {**DEFAULTS, "main_share": 0.6, "flat_pct": 0.01}
    assert "占总变化的 60% 以上" in ph.criterion_additive(custom, top_k_max=3, summed=False)
    assert "1% 算基本持平" in ph.criterion_additive(custom, top_k_max=3, summed=False)
    assert "差不到 2 倍稳健标准差" in ph.criterion_baseline(DEFAULTS, 12, "周")
    assert "3 倍以上算明显超出" in ph.criterion_baseline(DEFAULTS, 12, "周")
    assert "小于 0.05" in ph.criterion_trend(DEFAULTS, 10, "周")
    assert "Fisher" in ph.criterion_differ(DEFAULTS, "rate") and "Welch" in ph.criterion_differ(DEFAULTS, "mean")
