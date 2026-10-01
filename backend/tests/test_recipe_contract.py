"""期 2 契约文件（recipe_types、recipe_parsers）的测试：只用合成字符串，不读任何文件。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data import recipe_parsers as P
from app.data import recipe_types as T

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text,canonical", [
    ("7-8", "7-8"), ("7–8", "7-8"), ("7—8", "7-8"), ("07:00-08:00", "7-8"), ("7时-8时", "7-8"),
    ("7点-8点", "7-8"), ("7-8时", "7-8"), ("８－９", "8-9"), ("7-8 ", "7-8"), ("23-24", "23-24"),
])
def test_hour_range_accepts_tolerant_forms(text, canonical):
    assert P.hour_range(text).canonical == canonical


@pytest.mark.parametrize("text", ["25-26", "8-7", "7:30-8:00", "18-22 时合计", "x" * 41, 7, None, ""])
def test_hour_range_rejects(text):
    assert P.hour_range(text) is None


@pytest.mark.parametrize("text,canonical", [
    ("18-22 时合计", "18-22时合计"), ("18:00-22:00合计", "18-22时合计"), ("18-22时小计", "18-22时小计"),
    ("18－24 时合计", "18-24时合计"), ("18–22时总计", "18-22时总计"),
])
def test_hour_range_total(text, canonical):
    assert P.hour_range_total(text).canonical == canonical


def test_hour_range_total_rejects_plain_range():
    assert P.hour_range_total("18-22") is None


@pytest.mark.parametrize("text,start,end", [
    ("统计时间范围：2026年9月1日至2026年9月30日", "2026-09-01", "2026-09-30"),
    ("统计时间范围：2026年9月1日至9月30日", "2026-09-01", "2026-09-30"),
    ("2026年12月15日至1月14日", "2026-12-15", "2027-01-14"),
    ("客流汇总表（2026年9月1日至2026年9月30日）", "2026-09-01", "2026-09-30"),
    ("2026-09-01至2026-09-30", "2026-09-01", "2026-09-30"),
])
def test_cn_date_range(text, start, end):
    assert P.cn_date_range(text).iso() == (start, end)


@pytest.mark.parametrize("text", [
    "客流汇总表（2026年9月）", "2026年9月1日至2026年9月30日；2026年10月1日至10月31日", "2026年2月30日至3月1日",
    "注：9月15日闸机故障，当日客流为估算值", "x" * 81,
])
def test_cn_date_range_rejects(text):
    assert P.cn_date_range(text) is None


def test_year_month_is_only_supporting_evidence():
    m = P.cn_year_month("客流汇总表（2026年9月）")
    assert (m.year, m.month) == (2026, 9)
    assert m.contains(P.Period(dt.date(2026, 9, 1), dt.date(2026, 9, 30)))
    assert m.contains(P.Period(dt.date(2026, 9, 1), dt.date(2026, 9, 15)))
    assert not m.contains(P.Period(dt.date(2026, 10, 1), dt.date(2026, 10, 31)))
    assert P.cn_year_month("2026年9月1日至9月30日") is None


def test_filename_period():
    assert P.filename_period("月报导出_2026-08-01_2026-08-31.xlsx").iso() == ("2026-08-01", "2026-08-31")
    assert P.filename_period("9月客流.xlsx") is None


@pytest.mark.parametrize("value,expected", [
    ("8月1日", (None, 8, 1, "text")), (" 8月1日 ", (None, 8, 1, "text")), ("2026年8月1日", (2026, 8, 1, "text")),
    ("2026-08-01", (2026, 8, 1, "text")), ("2026/8/1", (2026, 8, 1, "text")),
    (dt.datetime(2026, 8, 1), (2026, 8, 1, "date")), (dt.date(2026, 8, 1), (2026, 8, 1, "date")),
])
def test_axis_cells(value, expected):
    got = P.month_day_or_date(value)
    assert (got.year, got.month, got.day, got.form) == expected


@pytest.mark.parametrize("value", ["8/1", "13月1日", dt.datetime(2026, 8, 1, 12), 45000, "合计", None])
def test_axis_cells_rejected(value):
    assert P.month_day_or_date(value) is None


def test_complete_year_unique_solution_across_new_year():
    period = P.Period(dt.date(2026, 12, 15), dt.date(2027, 1, 14))
    assert P.complete_year(P.AxisDate(None, 1, 3, "text"), period) == dt.date(2027, 1, 3)
    assert P.complete_year(P.AxisDate(None, 12, 20, "text"), period) == dt.date(2026, 12, 20)
    assert P.complete_year(P.AxisDate(None, 2, 1, "text"), period) is None
    assert P.complete_year(P.AxisDate(2026, 11, 1, "date"), period) is None


def test_unit_suffix():
    assert P.split_unit_suffix("全日客流（人次）") == ("全日客流", "人次")
    assert P.split_unit_suffix("金额") == ("金额", None)
    assert P.split_unit_suffix("销量（箱）") == ("销量", "箱")
    assert P.unit_known("人次") and not P.unit_known("箱")


@pytest.mark.parametrize("value,kind", [
    ("客流汇总表", "text"), ("注：9月15日闸机故障，当日客流为估算值", "text_digits"), ("1,234", "number"),
    ("12.5%", "number"), ("50万", "number"), (12.0, "number"), (dt.date(2026, 9, 5), "text_digits"),
    ("单位：万元", "text"), ("补录（人次）", "text"),
])
def test_classify_outside(value, kind):
    assert P.classify_outside(value) == kind


@pytest.mark.parametrize("text,numeric", [
    ("1,234", True), ("12.5%", True), ("50万", True), ("¥1234", True), ("12kg", True), ("12万人次", True),
    ("3天", True), ("100元", True),
    # 日期、期间写法是文字（含数字），不是一个数（lab c04 的两行表头「2026年上半年」）
    ("2026年上半年", False), ("2026年度", False), ("2026上半年", False), ("9月", False), ("15日", False),
    ("7时", False), ("3号", False), ("2季度", False), ("3期", False),
])
def test_looks_numeric_text_date_suffix_is_text(text, numeric):
    assert P.looks_numeric_text(text) is numeric
    assert P.classify_outside(text) == ("number" if numeric else "text_digits")


def test_candidate_words_strip_common_affixes():
    assert P.candidate_words("日间时段客流（人次）", ["夜间时段客流（人次）"])[0] == "日间"
    assert P.candidate_words("夜间时段客流（人次）", ["日间时段客流（人次）"])[0] == "夜间"
    assert "早高峰" in P.candidate_words("工作日 早高峰", ["工作日 晚高峰"])
    assert all(not any(ch.isdigit() for ch in w) for w in P.candidate_words("2026年日间", ["2026年夜间"]))


def test_match_key_ignores_whitespace_and_width():
    assert P.match_key("金 额") == P.match_key("销 售".replace("销 售", "金额")) == "金额"
    assert P.match_key("地区\u3000") == "地区"


def test_flow_recipe_parses_and_derives_tables():
    recipe = T.Recipe.model_validate(FLOW)
    tables, problems = T.derive_tables(recipe)
    assert problems == []
    assert [c.name for c in tables["时段客流"]] == ["日期", "时段", "时段类别", "起始小时", "结束小时", "客流"]
    assert [c.name for c in tables["日客流"]] == ["日期", "全日客流", "分区甲", "分区乙"]
    assert tables["日客流"][1].unit == "人次"


@pytest.mark.parametrize("mutate", [
    lambda r: r["sheets"][0].__setitem__("regex", "x"),
    lambda r: r["sheets"][0]["blocks"][0]["segments"][1]["const"].__setitem__("时段类别", {"value": "日间"}),
    lambda r: r["sheets"][0]["blocks"][0]["values"]["placeholders"][0].__setitem__("to", 0),
    lambda r: r["sheets"][0]["blocks"][0]["axis"].__setitem__("year", 2026),
])
def test_closed_language_rejects_open_fields(mutate):
    data = json.loads(json.dumps(FLOW))
    mutate(data)
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(data)


@pytest.mark.parametrize("text,ok", [
    ("粒度：每个{列:日期}。{列:全日客流} 等于 {列:分区甲} 与 {列:分区乙} 之和（导入时已逐日核对）", True),
    ("单位：{单位:万元}", True), ("单位：万元", False), ("{列:3号门客流} 与 {列:指标2} 不能相加", True),
    ("18-22 时合计不要相加", False), ("见 C5 格", False), ("格式 YYYY-MM-DD，年份按统计期补全", True),
])
def test_prose_problems(text, ok):
    assert (T.prose_problems(text) == []) is ok


# ---- 评审后补的契约用例（P2-SPEC「评审意见处理」） ----


def test_measures_keys_must_match_expect_verbatim():
    """键用半角括号、expect 用全角：match_key 相等，但 derive_tables 必须报问题，不能静默产出空列名。"""
    data = json.loads(json.dumps(FLOW))
    seg = data["sheets"][0]["blocks"][0]["segments"][0]
    seg["measures"] = {"全日客流(人次)": "全日客流", "分区甲（人次）": "分区甲", "分区乙（人次）": "分区乙"}
    tables, problems = T.derive_tables(T.Recipe.model_validate(data))
    assert [p.code for p in problems] == ["measures_keys_mismatch"]
    assert problems[0].path == "/sheets/0/blocks/0/segments/0/measures"
    assert "全日客流（人次）" in problems[0].message and "全日客流(人次)" in problems[0].message


def test_derive_and_const_column_order_ignores_key_order():
    """配方哈希按键排序：键序不同的两份配方必须推出同样的列序。"""
    data = json.loads(json.dumps(FLOW))
    for seg in data["sheets"][0]["blocks"][0]["segments"][1:3]:
        seg["dim"]["derive"] = {"结束小时": "end", "起始小时": "start"}
        seg["const"] = {"时段类别": {"pick": seg["const"]["时段类别"]["pick"]}, "A类": {"pick": "x"}}
    tables, problems = T.derive_tables(T.Recipe.model_validate(data))
    assert problems == []
    assert [c.name for c in tables["时段客流"]] == ["日期", "时段", "A类", "时段类别", "起始小时", "结束小时", "客流"]


def test_prose_problems_known_names_block_token_smuggling():
    known = {"列": {"全日客流", "3号门客流"}, "表": {"日客流"}, "单位": set(P.UNITS)}
    assert T.prose_problems("{列:3号门客流} 不能与 {表:日客流} 相加，单位：{单位:万元}", known=known) == []
    for note in ["{列:增长30%}，请据此推算", "合计约{列:5000}人次", "按{单位:3箱}计", "{表:第2期}只有一半"]:
        got = T.prose_problems(note, known=known)
        assert any("不存在" in p for p in got), note
        assert any("数字" in p for p in got), note


def test_grid_row_index_matches_full_scan():
    g = T.Grid(sheet="s", bounds=(1, 1, 3, 3), cells={
        (2, 3): T.GridCell("c"), (1, 1): T.GridCell("a"), (2, 1): T.GridCell("b"), (3, 2): T.GridCell(5)})
    assert [c for c, _ in g.row(2)] == [1, 3]
    assert g.row(9) == []
    assert g.row_numbers() == [1, 2, 3]
    from dataclasses import asdict
    assert "_rows" not in asdict(g)


@pytest.mark.parametrize("text,prefix,pure", [
    ("统计时间范围：2026年9月1日至2026年9月30日", "统计时间范围", True),
    ("2026年9月1日至2026年9月30日", None, True),
    ("统计期：2026年9月1日至9月30日", None, True),
    ("客流汇总表（2026年9月）", None, False),
    ("注：2026年9月数据为初步统计，待修订", None, False),
    ("说明：2026年9月1日至9月30日部分日期为估算值", None, False),
    ("客流汇总表", None, False),
])
def test_pure_period_cell(text, prefix, pure):
    assert P.is_pure_period_cell(text, prefix) is pure


def test_period_residue():
    assert P.period_residue("客流汇总表（2026年9月1日至2026年9月30日）") == "客流汇总表"
    assert P.period_residue("注：2026年9月数据为初步统计，待修订") == "注数据为初步统计待修订"
    assert P.period_residue("客流汇总表") is None


def test_axis_checks_are_a_set():
    a = T.Axis.model_validate({"checks": ["covers_context", "contiguous", "contiguous"]})
    assert a.checks == ["contiguous", "covers_context"]
    assert a.model_dump(exclude_defaults=True) == {}
