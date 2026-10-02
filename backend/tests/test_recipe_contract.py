"""期 2、期 3 契约文件（recipe_types、recipe_parsers）的测试：只用合成字符串和合成夹具（参考配方、期 2 基线、
flow 夹具），不读任何真实文件。"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import tempfile
import typing
from dataclasses import MISSING, asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data import recipe_parsers as P
from app.data import recipe_types as T
from app.data.recipe import canonical_recipe, recipe_sha256

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


# ==========================================================================
# 期 3 契约（P3-SPEC 9.2、12.1 WP-0）：新字段的默认值等于旧行为、语言仍封闭、资格函数、字段名钉住
# ==========================================================================

#: 参考配方的哈希（期 2 实测值）：期 3 加了字段也不能变，期 2 的构建 id 才不失效
REF_RECIPE_SHA = "971a8eba5ac606bd0552c43ff83b5c361d134c77d471bfe2d28e440ebfcc07b4"
P2_BASELINE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "p2_baseline.json").read_text(encoding="utf-8"))


def _flow(**changes) -> dict:
    data = json.loads(json.dumps(FLOW))
    data.update(changes)
    return data


def test_reference_recipe_hash_unchanged_with_p3_fields():
    recipe = T.Recipe.model_validate(FLOW)
    assert recipe_sha256(recipe) == REF_RECIPE_SHA == P2_BASELINE["recipe"]["recipe_sha256"]
    # 完整形式里新字段都在、都取默认值；再解析一遍哈希不变（新字段的默认值不进 canonical）
    full = recipe.model_dump(mode="json")
    sheet, block = full["sheets"][0], full["sheets"][0]["blocks"][0]
    assert sheet["ignore_outside"] == [] and block["ignore_rows"] == [] and block["ignore_columns"] == []
    assert recipe_sha256(T.Recipe.model_validate(full)) == REF_RECIPE_SHA
    canon = json.dumps(canonical_recipe(recipe), ensure_ascii=False)
    assert "ignore_" not in canon
    # 用了新字段，哈希才变
    full["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "补录（人次）", "reason": "补录数据另行核对"}]
    assert recipe_sha256(T.Recipe.model_validate(full)) != REF_RECIPE_SHA


def test_list_block_ignore_columns_default_keeps_hash():
    data = json.loads(json.dumps(C01_LIKE))
    base = recipe_sha256(T.Recipe.model_validate(data))
    data["sheets"][0]["blocks"][0]["ignore_columns"] = []
    data["sheets"][0]["ignore_outside"] = []
    assert recipe_sha256(T.Recipe.model_validate(data)) == base
    data["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "备注", "reason": "备注列不导入"}]
    parsed = T.Recipe.model_validate(data)
    assert parsed.sheets[0].blocks[0].ignore_columns[0].header == "备注"
    assert recipe_sha256(parsed) != base


@pytest.mark.parametrize("where,key,item", [
    ("block", "ignore_rows", {"label": "补录（人次）", "reason": "补录"}),
    ("block", "ignore_columns", {"header": "合计", "reason": "右侧合计列"}),
    ("sheet", "ignore_outside", {"anchor": "补录说明", "reason": "说明行"}),
])
def test_ignore_rules_parse_and_are_closed(where, key, item):
    def put(value):
        data = json.loads(json.dumps(FLOW))
        target = data["sheets"][0]["blocks"][0] if where == "block" else data["sheets"][0]
        target[key] = value
        return data

    T.Recipe.model_validate(put([item]))
    with pytest.raises(ValidationError):                     # extra="forbid"：不能多写字段（如坐标、正则）
        T.Recipe.model_validate(put([{**item, "cell": "B32"}]))
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(put([{**item, "regex": ".*"}]))
    first = next(iter(item))
    with pytest.raises(ValidationError):                     # 锚点文字不能为空
        T.Recipe.model_validate(put([{**item, first: ""}]))
    with pytest.raises(ValidationError):                     # 理由必填
        T.Recipe.model_validate(put([{k: v for k, v in item.items() if k != "reason"}]))
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(put([{**item, "reason": "x" * 201}]))
    limit = 20 if key == "ignore_outside" else 50
    T.Recipe.model_validate(put([item] * limit))
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(put([item] * (limit + 1)))


def test_ignore_rule_lengths():
    T.IgnoreRow(label="x" * 40, reason="r")
    T.IgnoreColumn(header="x" * 80, reason="r")
    T.IgnoreOutside(anchor="x" * 40, reason="r")
    for bad in (lambda: T.IgnoreRow(label="x" * 41, reason="r"),
                lambda: T.IgnoreColumn(header="x" * 81, reason="r"),
                lambda: T.IgnoreOutside(anchor="x" * 41, reason="r")):
        with pytest.raises(ValidationError):
            bad()


def test_ignore_fields_only_where_the_spec_puts_them():
    """ignore_rows 只在交叉表块上；列表块只有 ignore_columns；ignore_outside 只在工作表上。"""
    data = json.loads(json.dumps(C01_LIKE))
    data["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "合计", "reason": "r"}]
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(data)
    data = json.loads(json.dumps(FLOW))
    data["sheets"][0]["blocks"][0]["ignore_outside"] = [{"anchor": "注", "reason": "r"}]
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(data)
    data = json.loads(json.dumps(FLOW))
    data["sheets"][0]["ignore_columns"] = [{"header": "合计", "reason": "r"}]
    with pytest.raises(ValidationError):
        T.Recipe.model_validate(data)


# ---- accumulate_blockers（P3-SPEC 2.2）

#: c01 规则草稿的形状（列表块，表头 地区 产品 销量 金额，合计行另存；没有可解析的统计期区间）
C01_LIKE = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{
        "id": "s1", "match": {"name": "月报"},
        "blocks": [{
            "id": "列表1", "layout": "list", "table": "月报",
            "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                        {"header": "产品", "name": "产品", "type": "TEXT"},
                        {"header": "销量", "name": "销量", "type": "INTEGER"},
                        {"header": "金额", "name": "金额", "type": "INTEGER"}],
            "rows": {"total_row": {"label_column": "地区", "pick": "合计", "keep_as": "月报_表内合计"}},
        }],
    }],
    "tables": [{"name": "月报", "grain": ["地区", "产品"], "units": {"金额": "万元"}},
               {"name": "月报_表内合计", "grain": ["合计项"], "kind": "reported_total", "units": {"金额": "万元"}}],
}


def test_accumulate_blockers_reference_recipe_is_eligible():
    assert T.accumulate_blockers(T.Recipe.model_validate(_flow(mode="accumulate"))) == []
    # 不看 mode：替换模式的同一份配方也符合资格（起草器据此决定写不写 mode=accumulate）
    assert T.accumulate_blockers(T.Recipe.model_validate(FLOW)) == []


def test_accumulate_blockers_list_shape_is_unkeyed():
    data = json.loads(json.dumps(C01_LIKE))
    data["sheets"][0]["context"] = [{}]                     # 即使配了统计期，列表写入的表也不能累积
    got = T.accumulate_blockers(T.Recipe.model_validate(data))
    assert [(p.code, p.path) for p in got] == [("accumulate_unkeyed", "/tables/0"), ("accumulate_unkeyed", "/tables/1")]
    assert all("列表形态的表目前只支持每期替换" in p.message for p in got)
    assert "「月报」" in got[0].message and "「月报_表内合计」" in got[1].message
    # c01 的草稿本来就没有统计期：两条都报，统计期那条在前（起草卡片取第一条的人话）
    got = T.accumulate_blockers(T.Recipe.model_validate(C01_LIKE))
    assert [p.code for p in got] == ["accumulate_needs_period", "accumulate_unkeyed", "accumulate_unkeyed"]


def test_accumulate_blockers_needs_period():
    data = _flow(mode="accumulate")
    data["sheets"][0]["context"] = []
    got = T.accumulate_blockers(T.Recipe.model_validate(data))
    assert [(p.code, p.path) for p in got] == [("accumulate_needs_period", "/mode")]
    assert "统计期" in got[0].message


def test_accumulate_blockers_axis_checks_and_grain():
    data = _flow(mode="accumulate")
    data["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]
    got = T.accumulate_blockers(T.Recipe.model_validate(data))
    assert [(p.code, p.path) for p in got] == [("accumulate_unkeyed", f"/tables/{i}") for i in range(3)]
    assert all("恰好覆盖统计期" in p.message and "交叉表「交叉表」" in p.message for p in got)

    data = _flow(mode="accumulate")
    data["tables"][1]["grain"] = ["时段"]
    got = T.accumulate_blockers(T.Recipe.model_validate(data))
    assert [(p.code, p.path) for p in got] == [("accumulate_unkeyed", "/tables/1")]
    assert "「时段客流」" in got[0].message and "主键不含日期列「日期」" in got[0].message


def test_accumulate_blockers_messages_follow_copy_rules():
    data = json.loads(json.dumps(C01_LIKE))
    data["tables"][0]["grain"] = []
    msgs = [p.message for p in T.accumulate_blockers(T.Recipe.model_validate(data))]
    for m in msgs:
        assert not any(w in m for w in ("跑", "后端", "放行", "钉", "accumulate", "covers_context")), m


# ---- 新字段的默认值等于旧行为

def test_new_fields_default_to_old_behaviour():
    assert T.RegionMark("s", "value", "C5").block is None
    assert T.Problem("x", "structure", "m").fix_args is None
    assert T.Extraction(ok=True).rows_excluded == []
    assert T.SchemaNotes().fragments == {}
    assert T.ConfirmItem("mode", "导入模式").source == "recipe"
    assert T.RECIPE_ENGINE_VER == "recipe/1" and T.UNION_VER == "union/1"
    # 旧回执里没有 rows_excluded / block / fix_args：asdict 之后多出来的键都取默认值
    ex = asdict(T.Extraction(ok=True, regions=[T.RegionMark("s", "value", "C5")],
                             problems=[T.Problem("x", "structure", "m")]))
    assert ex["rows_excluded"] == [] and ex["regions"][0]["block"] is None and ex["problems"][0]["fix_args"] is None


def test_region_mark_json_always_carries_block():
    """RegionMark 的 JSON 一律带 block 键（None 也带）。期 2 的 test_recipe_imports.py 的
    test_stage_first_does_not_create_the_source_and_keeps_the_raw 逐字比 marks、不含 block，WP-0 合并后会失败；
    那个文件归 WP-5（P3-SPEC 12.0），由 WP-5 改期望值。这条测试防止有人为了让那条期 2 断言通过而去掉这个字段
    或者改成不输出 None。"""
    assert asdict(T.RegionMark("甲表", "value", "A2:B3")) == {"sheet": "甲表", "role": "value", "ref": "A2:B3",
                                                             "block": None}
    assert asdict(T.RegionMark("甲表", "value", "A2:B3", block="列表1"))["block"] == "列表1"


def test_trial_receipt_view_carries_rows_excluded_once_wp5_lands():
    """评审：TrialOut.receipt 只抄 recipe_imports._RECEIPT_VIEW 里的键。不把 rows_excluded 加进去，向导回执就一律
    显示「这次导入未记录排除的行」。这一行归 WP-5（recipe_imports.py），所以这条测试在 WP-5 合并前跳过：
    以 Pipeline 有没有 propose_fixes 字段（WP-5 按 P3-SPEC 12.6 加的接缝）判断 WP-5 是否已合并。"""
    from app.data import recipe_imports

    if "propose_fixes" not in {f.name for f in dataclasses.fields(recipe_imports.Pipeline)}:
        pytest.skip("WP-5 还没有合并：试运行回执的白名单由 WP-5 补 rows_excluded")
    rows = [asdict(T.ExcludedRows("甲表", "blank_skipped", [[5, 5]], 2, block="列表1"))]
    view = recipe_imports._receipt_view({"tables": [], "rows_excluded": rows}, None)
    assert view.get("rows_excluded") == rows, "TrialOut.receipt 缺 rows_excluded：把它加进 recipe_imports._RECEIPT_VIEW"


def test_edit_result_breaking_is_internal_bool():
    """EditResult.breaking 是布尔；EditPreview JSON 的 breaking 是「表名 → 人话列表」，由 WP-5 另算并覆盖（9.4）。
    这里只钉住契约这一侧的类型，免得有人把两者改成同一个值。"""
    assert typing.get_type_hints(T.EditResult)["breaking"] is bool
    assert T.EditResult(True, "fix", "k", "t", [], [], [], [], []).breaking is False


def test_snapshot_not_activatable_codes_match_versions_text():
    """SnapshotOut.reason_code 的取值与文字（契约补充，7.1 只有人话 reason）。文字与附录 C 的 notActivatable
    逐字相同：界面按 code 取 VERSIONS_TEXT（types.ts 的 SnapshotReasonCode 用 satisfies 钉住键），没有 code 时
    显示 reason，两条路显示一样。顺序即几种同时成立时的优先顺序。"""
    assert typing.get_args(T.SnapshotReasonCode) == ("current", "retired", "file_lost", "contains_revoked")
    assert list(T.SNAPSHOT_NOT_ACTIVATABLE) == list(typing.get_args(T.SnapshotReasonCode))
    assert T.SNAPSHOT_NOT_ACTIVATABLE == {
        "current": "已是当前版本",
        "retired": "已回收，无法启用",
        "file_lost": "数据文件已丢失，无法启用",
        "contains_revoked": "包含已作废接受的导入，无法启用",
    }


def _literal(cls, name: str) -> tuple:
    return typing.get_args(typing.get_type_hints(cls)[name])


def test_closed_value_sets():
    assert _literal(T.ConfirmItem, "source") == (
        "recipe", "diff", "outside", "sheet", "context", "switch", "edit", "accumulate")
    assert {"union_rows", "union_pk", "union_period"} <= set(_literal(T.CheckResult, "kind"))
    assert "ignored" in typing.get_args(T.Role) and "ignored_column" in typing.get_args(T.Role)
    assert typing.get_args(T.ExcludedReason) == (
        "hidden_excluded", "blank_skipped", "ignored_rows", "ignored_outside", "after_stop", "total_not_kept")
    assert _literal(T.FixAnchor, "kind") == ("problem", "recipe_problem", "sheet_renamed")
    assert _literal(T.Anchor, "kind") == (
        "header", "row_label", "section_title", "total_word", "axis", "after_title", "outside_text")
    assert _literal(T.EditResult, "kind") == ("fix", "selection")
    assert _literal(T.ChangeClass, "change") == ("same", "compatible", "retire", "semantic")
    assert T.FIX_KINDS == ("remove_label", "add_label", "edit_members", "rename_title", "declare_total",
                           "ignore_cells", "declare_placeholder", "declare_hidden", "rename_sheet")
    assert T.SELECTION_AS == ("list", "crosstab", "segment", "derived", "section_title", "ignore_rows",
                              "ignore_columns", "ignore_outside")
    assert T.ACCUMULATE_ACTIONS == ("replace", "first", "append", "replace_period", "restart", "rejected")


# ---- Selection：JSON 里的键是 as

def test_selection_json_key_is_as():
    body = {"sheet": "月报", "ref": "C5:F8", "as": "list", "options": {"header_rows": 1, "bottom": "box"}}
    sel = T.Selection.from_json(body)
    assert (sel.sheet, sel.ref, sel.as_, sel.options) == ("月报", "C5:F8", "list", {"header_rows": 1, "bottom": "box"})
    out = sel.to_json()
    assert out == body and "as_" not in out
    assert T.Selection.from_json({"sheet": "月报", "ref": "C5", "as": "ignore_rows"}).options == {}
    assert T.Selection.from_json({"sheet": "月报", "ref": "C5", "as": "ignore_rows", "options": None}).options == {}
    # 写成 as_ 的请求不认（JSON 里的键是 as）：和别的形状错误一样抛 ValueError
    with pytest.raises(ValueError):
        T.Selection.from_json({"sheet": "月报", "ref": "C5", "as_": "list"})
    # 不经 to_json 直接 asdict 得到的是 as_：接口层必须用 to_json
    assert "as_" in asdict(sel)


@pytest.mark.parametrize("body", [
    None, [], ["月报", "C5", "list"], "月报",
    {"ref": "C5", "as": "list"}, {"sheet": "", "ref": "C5", "as": "list"}, {"sheet": "  ", "ref": "C5", "as": "list"},
    {"sheet": 3, "ref": "C5", "as": "list"},
    {"sheet": "月报", "as": "list"}, {"sheet": "月报", "ref": "", "as": "list"}, {"sheet": "月报", "ref": 5, "as": "list"},
    {"sheet": "月报", "ref": "月报!C5", "as": "list"}, {"sheet": "月报", "ref": "c5", "as": "list"},
    {"sheet": "月报", "ref": "$C$5", "as": "list"}, {"sheet": "月报", "ref": "C0", "as": "list"},
    {"sheet": "月报", "ref": "C5:", "as": "list"}, {"sheet": "月报", "ref": "C:F", "as": "list"},
    {"sheet": "月报", "ref": "C5", "as": "table"}, {"sheet": "月报", "ref": "C5", "as": None},
    {"sheet": "月报", "ref": "C5", "as": ["list"]},
    {"sheet": "月报", "ref": "C5", "as": "list", "options": "x"},
    {"sheet": "月报", "ref": "C5", "as": "list", "options": [1]},
    {"sheet": "月报", "ref": "C5", "as": "list", "options": [["header_rows", 1]]},
    {"sheet": "月报", "ref": "C5", "as": "list", "options": 1},
])
def test_selection_from_json_rejects_malformed_with_value_error_only(body):
    """评审：options 写成字符串、列表时原来抛的是 dict() 的 ValueError / TypeError，文档却只说 KeyError，接口层照
    文档只接 KeyError 的话这些请求会变成 500。现在任何形状不对都只抛 ValueError（不是 KeyError、TypeError 的
    子类），消息是给人看的中文，接口层 except ValueError → 422 edit_invalid。"""
    with pytest.raises(ValueError) as exc:
        T.Selection.from_json(body)
    assert type(exc.value) is ValueError
    assert any("一" <= ch <= "鿿" for ch in str(exc.value))
    for word in ("sheet", "ref", "options", "as_", "SELECTION_AS"):
        assert word not in str(exc.value)


@pytest.mark.parametrize("ref", ["C5", "C5:F8", "AA10:AB12", "XFD1048576", "A1:A1"])
def test_selection_from_json_accepts_a1_ranges(ref):
    for as_ in T.SELECTION_AS:
        assert T.Selection.from_json({"sheet": "月报", "ref": ref, "as": as_}).to_json() == {
            "sheet": "月报", "ref": ref, "as": as_, "options": {}}


# ---- 9.2 每个 dataclass 的字段名、顺序和默认值（防止实现者改名；REQ = 必填）

REQ = "<必填>"
FACTORY = "<默认工厂>"


def _shape(cls) -> list[tuple[str, object]]:
    out = []
    for f in dataclasses.fields(cls):
        if f.default is not MISSING:
            out.append((f.name, f.default))
        elif f.default_factory is not MISSING:
            out.append((f.name, FACTORY))
        else:
            out.append((f.name, REQ))
    return out


CONTRACT_SHAPES = {
    "ExcludedRows": [("sheet", REQ), ("reason", REQ), ("rows", REQ), ("cells", REQ), ("anchor", None),
                     ("block", None)],
    "NoteFragment": [("key", REQ), ("subject", REQ), ("template", REQ)],
    "FixOption": [("value", REQ), ("label", REQ), ("detail", ""), ("needs_reason", False), ("breaking", False)],
    "FixAnchor": [("kind", REQ), ("index", None), ("sheet", None)],
    "FixProposal": [("id", REQ), ("kind", REQ), ("problem_code", REQ), ("title", REQ), ("cells", REQ),
                    ("target", REQ), ("options", REQ), ("anchor", REQ)],
    "Selection": [("sheet", REQ), ("ref", REQ), ("as_", REQ), ("options", FACTORY)],
    "Anchor": [("kind", REQ), ("text", REQ), ("cell", None)],
    "EditResult": [("ok", REQ), ("kind", REQ), ("key", REQ), ("title", REQ), ("summary", REQ), ("ops", REQ),
                   ("anchors", REQ), ("notes", REQ), ("problems", REQ), ("recipe_sha256_after", None),
                   ("expected", None), ("block", None), ("breaking", False)],
    "ReplayCompare": [("expected", REQ), ("actual", REQ), ("match", REQ), ("diffs", REQ), ("window_rows", None)],
    "ChangeClass": [("change", REQ), ("added", REQ), ("retired_new", REQ), ("retired_existing", REQ),
                    ("semantic", REQ), ("label_sets", REQ)],
    "AccumulatePlan": [(n, REQ) for n in (
        "mode", "action", "period", "parts", "replaces", "dropped", "overlaps", "gaps", "backfill", "change",
        "added", "retired_new", "retired_existing", "semantic", "label_sets", "mode_switch", "reason", "union")],
    "PartInfo": [(n, REQ) for n in (
        "import_id", "seq", "start", "end", "build_id", "db_path", "db_sha256", "manifest_db_sha256", "recipe_id",
        "recipe", "recipe_sha256", "raw_state", "file_name", "manifest_artifact")]
        + [("rows", FACTORY), ("overrides", 0), ("waivers", 0)],
    "UnionPart": [("import_id", REQ), ("start", REQ), ("end", REQ), ("db_path", REQ), ("recipe", REQ),
                  ("db_sha256", REQ), ("manifest_db_sha256", None), ("table_hashes", FACTORY)],
    "UnionReport": [(n, REQ) for n in (
        "tables", "grains", "rows", "part_rows", "table_hashes", "null_counts", "part_null_counts", "added",
        "retired", "checks", "ok", "db_sha256")],
    "UnionNotePart": [(n, REQ) for n in (
        "recipe", "extraction", "checks", "acceptances", "null_counts", "start", "end")],
}


@pytest.mark.parametrize("name", sorted(CONTRACT_SHAPES))
def test_p3_dataclass_shapes(name):
    assert _shape(getattr(T, name)) == CONTRACT_SHAPES[name]


def test_p3_fields_added_to_p2_dataclasses():
    """期 2 已有的 dataclass 只在末尾追加字段（位置构造照旧成立），新字段都有默认值。"""
    assert _shape(T.RegionMark)[-1] == ("block", None) and [n for n, _ in _shape(T.RegionMark)] == [
        "sheet", "role", "ref", "block"]
    assert _shape(T.Problem)[-2:] == [("fix", None), ("fix_args", None)]
    assert _shape(T.Extraction)[-1] == ("rows_excluded", FACTORY)
    assert _shape(T.SchemaNotes) == [("tables", FACTORY), ("templates", FACTORY), ("problems", FACTORY),
                                     ("kinds", FACTORY), ("fragments", FACTORY)]
    assert _shape(T.ConfirmItem) == [("id", REQ), ("label", REQ), ("detail", ""), ("required", True),
                                     ("source", "recipe")]


def test_p3_json_shapes_match_spec_examples():
    """asdict 出来的键与 P3-SPEC 9.4 的 JSON 示例一致（dataclass 字段与 JSON 同名）。"""
    proposal = T.FixProposal(
        id="fx-1a2b3c4d5e6f", kind="remove_label", problem_code="label_missing",
        title="在分段「日间」的期望标签中去掉「7-8」", cells=["客流汇总!B10"],
        target={"segment": "日间", "labels": ["7-8"]},
        options=[T.FixOption("remove", "去掉标签「7-8」", detail="今后各期如果又出现「7-8」，会再次拒收")],
        anchor=T.FixAnchor("problem", 0))
    d = asdict(proposal)
    assert list(d) == ["id", "kind", "problem_code", "title", "cells", "target", "options", "anchor"]
    assert d["options"][0] == {"value": "remove", "label": "去掉标签「7-8」", "needs_reason": False, "breaking": False,
                               "detail": "今后各期如果又出现「7-8」，会再次拒收"}
    assert d["anchor"] == {"kind": "problem", "index": 0, "sheet": None}
    plan_keys = {"mode", "action", "period", "parts", "replaces", "dropped", "overlaps", "gaps", "backfill", "change",
                 "added", "retired_new", "retired_existing", "semantic", "label_sets", "mode_switch", "reason", "union"}
    assert {f.name for f in dataclasses.fields(T.AccumulatePlan)} == plan_keys
    replay = asdict(T.ReplayCompare({"header": "C5:F5"}, {"header": "C5:F5"}, True, []))
    assert set(replay) == {"expected", "actual", "match", "diffs", "window_rows"}
    ex_rows = asdict(T.ExcludedRows("客流汇总", "ignored_rows", [[32, 32]], 3, anchor="补录（人次）", block="交叉表"))
    assert ex_rows == {"sheet": "客流汇总", "reason": "ignored_rows", "rows": [[32, 32]], "cells": 3,
                       "anchor": "补录（人次）", "block": "交叉表"}


# ---- 期 2 基线（WP-1 的测试常量；这里先确认现在的代码能复现，免得基线本身算错）

def test_p2_baseline_reproduces():
    from app.data import raw_store, recipe_engine, table_versions, xlsx_scan
    from tests.fixtures.xlsx.flow import flow_workbook

    raw, name = flow_workbook(dt.date(2026, 8, 1), 31)
    assert name == P2_BASELINE["fixture"]["file_name"]
    assert hashlib.sha256(raw).hexdigest() == P2_BASELINE["fixture"]["raw_sha256"]
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "d00.db"
        ex = recipe_engine.execute(T.Recipe.model_validate(FLOW), raw, name, xlsx_scan.scan(raw), str(db))
        table_versions.settle_journal(db)
        assert ex.ok
        assert raw_store.sha256_file(db) == P2_BASELINE["db_sha256"]
    assert ex.table_hashes == P2_BASELINE["table_hashes"]
    assert {t.name: t.rows for t in ex.tables} == P2_BASELINE["rows"]
    ids = P2_BASELINE["ids"]
    raw_sha = P2_BASELINE["fixture"]["raw_sha256"]
    assert table_versions.recipe_build_id(ids["source_id"], raw_sha, REF_RECIPE_SHA, {}) == ids["recipe_build_id"]
    # 参考配方（交叉表）不带执行器语义标记（WP-8 评审意见 1）：提交路径按配方算出的构建 id 仍是基线记的那个
    marks = table_versions.recipe_engine_semantics(T.Recipe.model_validate(FLOW))
    assert marks == {}
    assert table_versions.recipe_build_id(ids["source_id"], raw_sha, REF_RECIPE_SHA, {},
                                          semantics=marks) == ids["recipe_build_id"]
    assert table_versions.snapshot_id(ids["source_id"], ids["import_ids"], T.RECIPE_ENGINE_VER) == ids["snapshot_id_recipe"]
    assert table_versions.snapshot_id(ids["source_id"], ids["import_ids"]) == ids["snapshot_id_simple"]
