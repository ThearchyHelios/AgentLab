"""说明生成（recipe_notes）、写进表结构（apply_notes）与工具描述、冻结白名单（tools/datasource）的测试。

不依赖执行器和核对模块的库：核对结果、Extraction 都在测试里手写（假名）。
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.db.models  # noqa: F401 - 会话级建表（conftest）按已导入的模型建：冻结 schema_snapshot 要用工件表
from app.data import recipe_parsers as P
from app.data.recipe_notes import CONSISTENT_KEYS, COLUMN_TEXT, NOTE_TEXT, apply_notes, build_notes
from app.data.recipe_types import (
    Acceptance,
    AxisOut,
    CheckResult,
    DerivedItem,
    Extraction,
    OutsideText,
    PeriodOut,
    Problem,
    Recipe,
    SchemaNotes,
    SheetsOut,
    prose_problems,
)

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))
SHEET = "客流汇总"
TOTALS = "时段客流_表内合计"
NULLS = {"日客流": {"日期": 0, "全日客流": 0, "分区甲": 0, "分区乙": 0},
         "时段客流": {"日期": 0, "时段": 0, "时段类别": 0, "起始小时": 0, "结束小时": 0, "客流": 31},
         TOTALS: {"日期": 0, "合计项": 0, "起始小时": 0, "结束小时": 0, "客流": 0}}


def chk(cid: str, kind: str, status: str = "passed", category: str = "structure", **kw: Any) -> CheckResult:
    return CheckResult(cid, kind, cid, status, category, **kw)


def flow_checks(**override: CheckResult) -> list[CheckResult]:
    base = {
        "C1": chk("C1", "context_agree", category="data_quality"),
        "C2": chk("C2", "filename_period", category="data_quality"),
        "K1": chk("K1", "derived_sum", checked=93),
        "G1": chk("G1", "formula_refs", checked=93),
        "R1": chk("R1", "relation_sum_eq", category="data_quality", checked=31),
        "R2": chk("R2", "relation_not_comparable", "info", "info", checked=31, failed=31),
        **{f"N{i}": chk(f"N{i}", "row_count") for i in (1, 2, 3)},
        **{f"P{i}": chk(f"P{i}", "pk_unique") for i in (1, 2, 3)},
    }
    base.update(override)
    return [c for c in base.values() if c is not None]


def night_item(**kw: Any) -> DerivedItem:
    args = dict(kind="label_range_sum", segment="夜间合计", sheet=SHEET, cell="C28", label_raw="18-22 时合计",
                label="18-22时合计", value=1, is_formula=True, formula="=SUM(C22:C25)", base_table="时段客流",
                base_value="客流", start=18, end=22, key={"日期": "2026-08-01", "时段类别": "夜间"},
                start_col="起始小时", end_col="结束小时", ref_rowids=[1], func="SUM")
    args.update(kw)
    return DerivedItem(**args)


def flow_extraction(**kw: Any) -> Extraction:
    args: dict[str, Any] = dict(
        ok=True, derived=[night_item()], placeholders={"·": 31},
        axes=[AxisOut("交叉表", 4, "8月1日", "8月31日", 31, "text")],
        outside_text=[OutsideText(SHEET, "B3", "客流汇总表", "text")],
        sheets=SheetsOut(matched={"s1": SHEET}),
        period=PeriodOut("2026-08-01", "2026-08-31", "cells", [f"{SHEET}!B2"]),
    )
    args.update(kw)
    return Extraction(**args)


def notes_for(data: dict | None = None, *, extraction: Extraction | None = None,
              checks: list[CheckResult] | None = None, acceptances: list[Acceptance] | None = None,
              null_counts: Any = NULLS):
    recipe = Recipe.model_validate(deepcopy(data or FLOW))
    return build_notes(recipe, extraction or flow_extraction(), flow_checks() if checks is None else checks,
                       acceptances or [], null_counts=null_counts)


def accept(*ids: str) -> list[Acceptance]:
    return [Acceptance(i, "已与业务方核实") for i in ids]


# ---------------------------------------------------------------- 参考配方（D00）


def test_reference_flow_notes():
    notes = notes_for()
    assert notes.problems == []
    assert notes.tables["日客流"].comment == (
        "粒度：每个日期。全日客流 等于 分区甲 与 分区乙 之和（导入时已逐日核对）：求合计只用 全日客流，不要把这几列相加。"
        "与 时段客流 口径不同：各时段之和不等于 全日客流，不能互相推算或相加")
    assert notes.tables["时段客流"].comment == (
        "粒度：每个日期的每个时段。与 日客流 口径不同：各时段之和不等于 全日客流，不能互相推算或相加。"
        "时段 按规范写法存储：起始小时-结束小时，不带单位。时段类别 取自原表的分段标题。"
        "原表的合计在 时段客流_表内合计，不要与本表相加")
    assert notes.tables[TOTALS].comment == (
        "粒度：每个日期的每个合计项。原表写明的合计，已按明细重算核对一致。"
        "各合计项覆盖的时段可能互相重叠：不要彼此相加，也不要与 时段客流 相加")
    date = "格式 YYYY-MM-DD，年份按统计期补全"
    assert notes.tables["日客流"].columns == {"日期": date, "全日客流": "单位：人次", "分区甲": "单位：人次",
                                            "分区乙": "单位：人次"}
    assert notes.tables["时段客流"].columns == {
        "日期": date, "客流": "单位：人次。空值表示原表为无数据占位符，不代表零；求和时请同时统计非空个数"}
    assert notes.tables[TOTALS].columns == {"日期": date, "客流": "单位：人次"}
    # 模板带引用，进清单可复查；渲染后的说明没有引用标记
    assert "{列:全日客流} 等于 {列:分区甲} 与 {列:分区乙} 之和" in notes.templates["日客流"]
    assert notes.templates["时段客流.客流"].startswith("单位：{单位:人次}")
    # B3「客流汇总表」是纯文字的区域外文字：不加 E7 提示
    assert all("附有说明文字" not in t.comment for t in notes.tables.values())


def test_all_rendered_notes_pass_prose_check_with_known_names():
    recipe = Recipe.model_validate(deepcopy(FLOW))
    notes = notes_for()
    known = {"列": {"日期", "全日客流", "分区甲", "分区乙", "时段", "时段类别", "起始小时", "结束小时", "客流", "合计项"},
             "表": {"日客流", "时段客流", TOTALS}, "单位": set(P.UNITS)}
    assert set(notes.templates) >= {t.name for t in recipe.tables}
    for key, tpl in notes.templates.items():
        assert prose_problems(tpl, known=known) == [], key


# ---------------------------------------------------------------- 每个模板都过数字和引用检查

_SLOTS = {
    "a": "{列:日期}", "b": "{列:时段}", "total": "{列:全日客流}", "parts": "{列:分区甲} 与 {列:分区乙}", "per": "逐日",
    "unit": "日期", "other": "{表:时段客流}", "dims": "{列:时段}", "value": "{列:全日客流}", "a_value": "{列:客流}",
    "by": "{列:日期}", "dim": "{列:时段}", "start": "{列:起始小时}", "end": "{列:结束小时}", "col": "{列:时段类别}",
    "table": "{表:时段客流_表内合计}", "base": "{表:时段客流}", "cols": "{列:现金}、{列:刷卡}",
}
_KNOWN = {"列": {"日期", "时段", "全日客流", "分区甲", "分区乙", "客流", "起始小时", "结束小时", "时段类别", "现金",
                "刷卡"},
          "表": {"时段客流", TOTALS}, "单位": set(P.UNITS)}


@pytest.mark.parametrize("key", sorted(NOTE_TEXT))
def test_every_table_template_passes_prose_check(key):
    assert prose_problems(NOTE_TEXT[key].format(**_SLOTS), known=_KNOWN) == []


@pytest.mark.parametrize("key", sorted(COLUMN_TEXT))
@pytest.mark.parametrize("unit", P.UNITS)
def test_every_column_template_passes_prose_check(key, unit):
    text = COLUMN_TEXT[key].format(unit="{单位:" + unit + "}", meaning="不适用")
    assert prose_problems(text, known=_KNOWN) == []


def test_only_passed_templates_say_consistent():
    """「一致」只许出现在 K / T 通过时才选的模板里。别的模板（包括统计期冲突那两条）一个「一致」都没有：
    K / T 没通过的表，说明由这些片段拼成，按子串判断也不会误报。"""
    assert CONSISTENT_KEYS <= set(NOTE_TEXT)
    for key, text in NOTE_TEXT.items():
        if key not in CONSISTENT_KEYS:
            assert "一致" not in text, key
    assert all("一致" not in text for text in COLUMN_TEXT.values())


# ---------------------------------------------------------------- 交叉表的表内合计：状态 × 原因

_K_EXPECT = {
    "uncached": "原表写明的合计。本期原表未保存计算结果，合计值为空，未能核对",
    "blank": "原表写明的合计。本期原表合计格为空，未能核对",
    "null_detail": "原表写明的合计，按原表保存的值存储。本期明细含无数据占位符，未能核对",
    "tiling": "原表写明的合计，按原表保存的值存储。本期明细未能覆盖合计的全部范围，未能核对",
    "hidden": "原表写明的合计，按原表保存的值存储。本期存在隐藏行或使用了分类汇总函数，合计口径无法确定，未能核对",
}


@pytest.mark.parametrize("reason", sorted(_K_EXPECT))
@pytest.mark.parametrize("accepted", [True, False])
def test_k_unverifiable_each_reason_never_says_consistent(reason, accepted):
    k = chk("K1", "derived_sum", "unverifiable", unverifiable=93, acceptable=True, reasons={reason: 93})
    notes = notes_for(checks=flow_checks(K1=k), acceptances=accept("K1") if accepted else [])
    assert notes.problems == []
    comment = notes.tables[TOTALS].comment
    assert _K_EXPECT[reason] in comment
    assert "一致" not in comment
    assert "各合计项覆盖的时段可能互相重叠" in comment


def test_k_mixed_reasons_use_weakest_wording():
    k = chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={"uncached": 2, "hidden": 1})
    notes = notes_for(checks=flow_checks(K1=k), acceptances=accept("K1"))
    comment = notes.tables[TOTALS].comment
    assert "原表写明的合计，按原表保存的值存储。本期合计值未能核对" in comment
    assert "一致" not in comment and "未保存计算结果" not in comment and "分类汇总" not in comment


def test_k_passed_but_g_unverifiable():
    g = chk("G1", "formula_refs", "unverifiable", acceptable=True, reasons={"unrecognized": 3})
    notes = notes_for(checks=flow_checks(G1=g), acceptances=accept("G1"))
    assert notes.problems == []
    assert "原表写明的合计，合计值已按明细核对一致，公式引用的格子未能核对" in notes.tables[TOTALS].comment


def test_k_passed_without_formulas_has_no_g():
    ex = flow_extraction(derived=[night_item(is_formula=False, formula=None, func=None, ref_rowids=None)])
    notes = notes_for(extraction=ex, checks=flow_checks(G1=None))
    assert notes.problems == []
    assert "原表写明的合计，已按明细重算核对一致" in notes.tables[TOTALS].comment


@pytest.mark.parametrize("k,g", [
    (chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={"unrecognized": 1}), None),
    (chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={}), None),
    (chk("K1", "derived_sum", "info", "info"), None),
    (chk("K1", "derived_sum", "mismatch", acceptable=True), None),
])
def test_no_template_records_problem_and_never_falls_back(k, g):
    """找不到对应模板就是 bug：记 problems（不能保存），绝不退回到「核对一致」之类的别的说法。"""
    override = {}
    if k is not None:
        override["K1"] = k
    if g is not None:
        override["G1"] = g
    notes = notes_for(checks=flow_checks(**override))
    assert any(TOTALS in p for p in notes.problems)
    comment = notes.tables[TOTALS].comment
    assert "原表写明的合计" not in comment and "一致" not in comment


@pytest.mark.parametrize("override", [
    {"K1": chk("K1", "derived_sum", "mismatch", failed=1)},
    {"G1": chk("G1", "formula_refs", "mismatch", failed=1)},
    {"K1": chk("K1", "derived_sum", "mismatch", failed=1), "G1": chk("G1", "formula_refs", "mismatch", failed=1)},
    # K 不一致、G 无法核对：只要有一条不可接受的结构类失败，整段都不写
    {"K1": chk("K1", "derived_sum", "mismatch", failed=1),
     "G1": chk("G1", "formula_refs", "unverifiable", acceptable=True, reasons={"hidden": 1})},
])
def test_blocking_structure_failure_skips_total_note_without_problem(override):
    """K / G 不一致（D08、P9）：试运行本来就因结构类核对拒收。说明里不写合计那一句（更不会写「一致」），
    也不记 problems——problems 会被当成「配方不合法 / 说明生成失败」，把用户引去改配方。"""
    notes = notes_for(checks=flow_checks(**override))
    assert notes.problems == []
    comment = notes.tables[TOTALS].comment
    assert "原表写明的合计" not in comment and "一致" not in comment
    # 与核对无关、本身成立的提示照写
    assert "各合计项覆盖的时段可能互相重叠" in comment


def test_blocking_relation_failure_skips_relation_note():
    """关系核对的 SQL 出错（_guarded：mismatch、structure、不可接受）：不能当成「关系不成立、尚未确认」去写。"""
    broken = {"R1": chk("R1", "relation_sum_eq", "mismatch", details=["核对 SQL 执行失败"]),
              "R2": chk("R2", "relation_not_comparable", "mismatch", details=["核对 SQL 执行失败"])}
    notes = notes_for(checks=flow_checks(**broken))
    assert notes.problems == []
    daily = notes.tables["日客流"].comment
    assert "分区甲" not in daily and "口径不同" not in daily and "尚未确认" not in daily


# ---------------------------------------------------------------- 一张合计表由两个 derived 段写入


def two_totals_recipe() -> dict:
    """日间、夜间各有一组合计行，都另存进 时段客流_表内合计（P2-SPEC 6.1 允许两段时段合表）。"""
    data = deepcopy(FLOW)
    segs = data["sheets"][0]["blocks"][0]["segments"]
    segs[1]["stop_parser"] = "hour_range_total"
    day = deepcopy(segs[3])
    day.update(id="日间合计", locate={"by": "after", "segment": "日间"}, labels={"expect": ["8-12 时合计"]})
    segs.insert(2, day)
    return data


def day_item(**kw: Any) -> DerivedItem:
    args = dict(segment="日间合计", cell="C21", label_raw="8-12 时合计", label="8-12时合计", formula="=SUM(C11:C14)",
                start=8, end=12, key={"日期": "2026-08-01", "时段类别": "日间"})
    args.update(kw)
    return night_item(**args)


def two_totals(**override: CheckResult) -> SchemaNotes:
    """K1 / G1 = 日间合计，K2 / G2 = 夜间合计（plan_checks 按配方里分段的先后编号）。"""
    base = {"K1": chk("K1", "derived_sum", checked=31), "K2": chk("K2", "derived_sum", checked=93),
            "G1": chk("G1", "formula_refs", checked=31), "G2": chk("G2", "formula_refs", checked=93)}
    base.update(override)
    ex = flow_extraction(derived=[day_item(), night_item()])
    return notes_for(two_totals_recipe(), extraction=ex, checks=flow_checks(**base), acceptances=accept(*base))


_UNC = {"status": "unverifiable", "acceptable": True}


def test_two_segments_into_one_total_table_all_passed():
    notes = two_totals()
    assert notes.problems == []
    assert "原表写明的合计，已按明细重算核对一致" in notes.tables[TOTALS].comment


@pytest.mark.parametrize("k1,k2", [
    ({"reasons": {"uncached": 31}, **_UNC}, {}),                                     # 日间无法核对、夜间通过
    ({}, {"reasons": {"hidden": 2}, **_UNC}),                                        # 反过来
    ({"reasons": {"uncached": 31}, **_UNC}, {"reasons": {"hidden": 2}, **_UNC}),     # 两段原因不同
])
def test_two_segments_partly_unverifiable_use_mixed_template(k1, k2):
    """几个 derived 段写进同一张合计表：只要有一段没通过，就不能写「一致」；一段通过、一段无法核对，或者两段
    原因不同，都用最弱的「原因混合」说法，不挑其中一段的原因来写。"""
    notes = two_totals(K1=chk("K1", "derived_sum", **k1), K2=chk("K2", "derived_sum", **k2))
    assert notes.problems == []
    comment = notes.tables[TOTALS].comment
    assert "一致" not in comment
    assert "原表写明的合计，按原表保存的值存储。本期合计值未能核对" in comment
    assert "未保存计算结果" not in comment and "分类汇总" not in comment
    assert comment.count("各合计项覆盖的时段可能互相重叠") == 1


def test_two_segments_same_reason_use_that_reason():
    unc = {"reasons": {"uncached": 3}, **_UNC}
    notes = two_totals(K1=chk("K1", "derived_sum", **unc), K2=chk("K2", "derived_sum", **unc))
    assert notes.problems == []
    comment = notes.tables[TOTALS].comment
    assert "原表写明的合计。本期原表未保存计算结果，合计值为空，未能核对" in comment and "一致" not in comment


def test_two_segments_one_g_unverifiable():
    notes = two_totals(G2=chk("G2", "formula_refs", "unverifiable", acceptable=True, reasons={"unrecognized": 1}))
    assert notes.problems == []
    assert "原表写明的合计，合计值已按明细核对一致，公式引用的格子未能核对" in notes.tables[TOTALS].comment


def test_two_segments_one_blocking_skips_without_problem():
    notes = two_totals(K1=chk("K1", "derived_sum", **{"reasons": {"uncached": 31}, **_UNC}),
                       K2=chk("K2", "derived_sum", "mismatch", failed=1))
    assert notes.problems == []
    assert "原表写明的合计" not in notes.tables[TOTALS].comment


# ---------------------------------------------------------------- 统计期冲突与「一致」


@pytest.mark.parametrize("reason", sorted(_K_EXPECT))
def test_period_conflict_with_unverifiable_k_never_says_consistent(reason):
    """K 无法核对、C1 / C2 冲突都已接受：合计表的说明里同时有统计期那一句，仍然一个「一致」都没有。"""
    k = chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={reason: 3})
    c1 = chk("C1", "context_agree", "mismatch", "data_quality", acceptable=True)
    c2 = chk("C2", "filename_period", "mismatch", "data_quality", acceptable=True)
    notes = notes_for(checks=flow_checks(K1=k, C1=c1, C2=c2), acceptances=accept("K1", "C1", "C2"))
    assert notes.problems == []
    comment = notes.tables[TOTALS].comment
    assert "本期统计期的写法在原表或文件名中有冲突，已由用户确认接受" in comment
    assert "一致" not in comment


def test_missing_check_is_problem():
    notes = notes_for(checks=flow_checks(R1=None))
    assert any("R1" in p and "日客流" in p for p in notes.problems)


# ---------------------------------------------------------------- 关系

def test_sum_eq_acceptance_rewrites_note():
    """D26：R1 不成立。接受前后都不再写「已逐日核对」；接受后写「已由用户确认接受」。"""
    assert "已逐日核对" in notes_for().tables["日客流"].comment
    r1 = chk("R1", "relation_sum_eq", "mismatch", "data_quality", checked=30, failed=1, acceptable=True)
    accepted = notes_for(checks=flow_checks(R1=r1), acceptances=accept("R1"))
    pending = notes_for(checks=flow_checks(R1=r1))
    assert accepted.problems == [] and pending.problems == []
    a, p = accepted.tables["日客流"].comment, pending.tables["日客流"].comment
    assert "已逐日核对" not in a and "已逐日核对" not in p
    assert ("全日客流 与 分区甲、分区乙 之和的关系在本期个别日期不成立，已由用户确认接受，详见导入清单："
            "不要据此推算，也不要把这几列相加") in a
    assert "尚未确认接受" in p and "已由用户确认接受" not in p
    assert "求合计只用" not in a


def test_sum_eq_with_nulls():
    r1 = chk("R1", "relation_sum_eq", "unverifiable", "data_quality", unverifiable=2, acceptable=True)
    comment = notes_for(checks=flow_checks(R1=r1), acceptances=accept("R1")).tables["日客流"].comment
    assert "全日客流 与 分区甲、分区乙 之和的关系在本期部分日期因空值未能核对：不要据此推算，也不要把这几列相加" in comment
    assert "已逐日核对" not in comment


def test_sum_eq_without_rows_says_so_and_not_checked():
    """AU-1：没有数据行时 R 判无法核对（checked=0、unverifiable=0）：说明写「没有数据行」，不写「已核对」「因空值」。"""
    r1 = chk("R1", "relation_sum_eq", "unverifiable", "data_quality", checked=0, unverifiable=0, acceptable=True)
    comment = notes_for(checks=flow_checks(R1=r1), acceptances=accept("R1")).tables["日客流"].comment
    assert "全日客流 与 分区甲、分区乙 之和的关系在本期没有数据行，未能核对：不要据此推算，也不要把这几列相加" in comment
    assert "已逐日核对" not in comment and "因空值" not in comment


def test_not_comparable_all_equal_drops_not_equal_wording():
    r2 = chk("R2", "relation_not_comparable", "info", "info", checked=31, failed=0)
    notes = notes_for(checks=flow_checks(R2=r2))
    for table in ("日客流", "时段客流"):
        comment = notes.tables[table].comment
        assert "口径不同，不能互相推算或相加" in comment and "不等于" not in comment
    assert "与 时段客流 口径不同" in notes.tables["日客流"].comment
    assert "与 日客流 口径不同" in notes.tables["时段客流"].comment


def test_not_comparable_flat_variant():
    data = deepcopy(FLOW)
    data["relations"][1]["a"] = {"table": "日客流", "value": "分区甲"}
    data["relations"][1]["b"] = {"table": TOTALS, "value": "客流"}
    notes = notes_for(data)
    assert notes.problems == []
    assert "与 时段客流_表内合计 口径不同：分区甲 按 日期 汇总不等于 客流，不能互相推算或相加" in notes.tables["日客流"].comment


# ---------------------------------------------------------------- 单位、列名里的数字


def test_digit_names_and_units_pass_checks():
    data = deepcopy(FLOW)
    seg = data["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"] = ["1月（万元）", "3号门客流（千克）", "指标2（亿元）"]
    seg["measures"] = {"1月（万元）": "1月", "3号门客流（千克）": "3号门客流", "指标2（亿元）": "指标2"}
    data["tables"][0]["units"] = {"1月": "万元", "3号门客流": "千克", "指标2": "亿元"}
    data["relations"][0].update(total="1月", parts=["3号门客流", "指标2"])
    data["relations"][1]["b"]["value"] = "1月"
    data["tables"][0]["note"] = "{列:3号门客流} 只统计闸机，{单位:万元} 与 {单位:亿元} 不要混算"
    notes = notes_for(data, null_counts=None)
    assert notes.problems == []
    comment = notes.tables["日客流"].comment
    assert "1月 等于 3号门客流 与 指标2 之和（导入时已逐日核对）" in comment
    assert comment.endswith("3号门客流 只统计闸机，万元 与 亿元 不要混算")
    assert notes.tables["日客流"].columns["1月"].startswith("单位：万元")
    assert notes.tables["日客流"].columns["指标2"].startswith("单位：亿元")


def test_unknown_unit_in_column_note_is_problem():
    """列说明同样过引用检查：单位不在系统单位表里（「万人次」），{单位:…} 不遮盖、记问题，不能保存。"""
    data = deepcopy(FLOW)
    data["tables"][0]["units"]["全日客流"] = "万人次"
    notes = notes_for(data)
    assert any("全日客流" in p and "不存在的单位「万人次」" in p for p in notes.problems)


def test_note_smuggling_numbers_is_problem():
    data = deepcopy(FLOW)
    data["tables"][0]["note"] = "{列:增长30%} 要单独看"
    notes = notes_for(data)
    assert any("日客流" in p and "不存在" in p for p in notes.problems)
    assert any("数字" in p for p in notes.problems)


# ---------------------------------------------------------------- 本期情况：E7、统计期、隐藏行、忽略列、停止之后


def test_e7_only_for_text_digits():
    plain = notes_for()
    assert all("附有说明文字" not in t.comment for t in plain.tables.values())
    for extra in (OutsideText(SHEET, "B32", "注：9月15日闸机故障，当日客流为估算值", "text_digits"),
                  OutsideText(SHEET, "B3", "客流汇总表（2026年9月）", "text_digits", period_source=True)):
        ex = flow_extraction(outside_text=[OutsideText(SHEET, "B3", "客流汇总表", "text"), extra])
        notes = notes_for(extraction=ex)
        assert notes.problems == []
        for t in notes.tables.values():
            assert "本期原表附有说明文字（可能涉及口径），内容见证据面板" in t.comment
            assert "9月" not in t.comment and "B32" not in t.comment


def test_period_conflict_and_human_period():
    c1 = chk("C1", "context_agree", "mismatch", "data_quality", acceptable=True)
    accepted = notes_for(checks=flow_checks(C1=c1), acceptances=accept("C1"))
    pending = notes_for(checks=flow_checks(C1=c1))
    assert "本期统计期的写法在原表或文件名中有冲突，已由用户确认接受，详见导入清单" in accepted.tables["日客流"].comment
    assert "本期统计期的写法在原表或文件名中有冲突，尚未确认接受" in pending.tables["日客流"].comment
    human = flow_extraction(period=PeriodOut("2026-09-01", "2026-09-30", "human", signed_by="甲"))
    assert "本期统计期为人工录入" in notes_for(extraction=human).tables["时段客流"].comment
    assert "人工录入" not in notes_for().tables["时段客流"].comment


LIST_RECIPE = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s1", "match": {"name": "销售"}, "hidden": {"rows": "exclude"}, "blocks": [{
        "id": "明细", "layout": "list", "table": "销售",
        "columns": [{"header": "日期", "name": "日期", "type": "DATE"},
                    {"header": "地区", "name": "地区", "type": "TEXT"},
                    {"header": "产品", "name": "产品", "type": "TEXT"},
                    {"header": "金额（元）", "name": "金额", "type": "INTEGER"},
                    {"header": "现金", "name": "现金", "type": "INTEGER"},
                    {"header": "刷卡", "name": "刷卡", "type": "INTEGER"}],
        "rows": {"total_row": {"label_column": "地区", "pick": "合计", "keep_as": "销售_表内合计"}}}]}],
    "tables": [{"name": "销售", "grain": ["日期", "地区", "产品"], "units": {"金额": "元"}},
               {"name": "销售_表内合计", "kind": "reported_total", "units": {"金额": "元"}}],
    "relations": [{"id": "R1", "kind": "sum_eq", "table": "销售", "total": "金额", "parts": ["现金", "刷卡"]}],
}


LIST_NUMBERS = ("金额", "现金", "刷卡")


def list_item(col: str = "金额") -> DerivedItem:
    cell = {"金额": "D9", "现金": "E9", "刷卡": "F9"}.get(col, "D9")
    return DerivedItem(kind="column_sum", segment="明细", sheet="销售", cell=cell, label_raw="合计", label="合计",
                       value=1, is_formula=False, formula=None, base_table="销售", base_value=col,
                       rowid_first=1, rowid_last=6)


def list_notes(t1: CheckResult | None = None, *, data: dict | None = None, accepted: bool = True,
               **ex: Any):
    checks = [t1 or chk("T1", "column_sum"), chk("R1", "relation_sum_eq", category="data_quality")]
    args = dict(ok=True, derived=[list_item(c) for c in LIST_NUMBERS], sheets=SheetsOut(matched={"s1": "销售"}),
                lineage={"销售": {"金额": [[1, "销售", "D2", 6, "down"]]}})
    args.update(ex)
    recipe = Recipe.model_validate(deepcopy(data or LIST_RECIPE))
    return build_notes(recipe, Extraction(**args), checks, accept("T1") if accepted else [])


def test_list_reference_notes():
    notes = list_notes()
    assert notes.problems == []
    assert notes.tables["销售"].comment == (
        "粒度：日期、地区 与 产品 的每个组合。金额 等于 现金 与 刷卡 之和（导入时已逐行核对）：求合计只用 金额，"
        "不要把这几列相加。原表的合计在 销售_表内合计，不要与本表相加")
    assert notes.tables["销售_表内合计"].comment == "原表合计行的值，已按各列明细求和核对一致：不要与 销售 相加"
    cols = notes.tables["销售"].columns
    assert cols["日期"] == "格式 YYYY-MM-DD"
    assert cols["金额"] == "单位：元。空值表示原表为空格，不代表零；求和时请同时统计非空个数"
    assert "地区" not in cols
    # 列表合计表的空值不写列说明（表说明已交代）
    assert notes.tables["销售_表内合计"].columns == {"金额": "单位：元"}


_T_EXPECT = {
    "uncached": "原表合计行的值。本期原表未保存计算结果，合计值为空，未能核对：不要与 销售 相加",
    "blank": "原表合计行的值。本期原表合计格为空，未能核对：不要与 销售 相加",
    "tiling": "原表合计行的值，按原表保存的值存储。本期合计行之上没有明细，未能核对：不要与 销售 相加",
    "null_detail": "原表合计行的值，按原表保存的值存储。本期明细含空值，未能核对：不要与 销售 相加",
    "hidden": "原表合计行的值，按原表保存的值存储。本期存在隐藏行或使用了分类汇总函数，合计口径无法确定，未能核对：不要与 销售 相加",
}


@pytest.mark.parametrize("reason", sorted(_T_EXPECT))
def test_t_unverifiable_each_reason_never_says_consistent(reason):
    notes = list_notes(chk("T1", "column_sum", "unverifiable", acceptable=True, reasons={reason: 1}))
    assert notes.problems == []
    comment = notes.tables["销售_表内合计"].comment
    assert comment == _T_EXPECT[reason]
    assert "一致" not in comment


def test_t_mixed_and_missing_templates():
    mixed = list_notes(chk("T1", "column_sum", "unverifiable", acceptable=True, reasons={"uncached": 1, "hidden": 1}))
    assert mixed.tables["销售_表内合计"].comment == "原表合计行的值，按原表保存的值存储。本期未能核对：不要与 销售 相加"
    # 列表合计没有「公式写法无法识别」这种原因（那是公式引用 G 的）：真出现了就是没有模板，记问题
    odd = list_notes(chk("T1", "column_sum", "unverifiable", acceptable=True, reasons={"unrecognized": 1}))
    assert any("T1" in p for p in odd.problems)
    assert "一致" not in odd.tables["销售_表内合计"].comment
    # T 不一致：试运行已因结构类核对拒收，不写这一句、不记问题
    mismatch = list_notes(chk("T1", "column_sum", "mismatch", failed=1))
    assert mismatch.problems == []
    assert "原表合计行的值" not in mismatch.tables["销售_表内合计"].comment


def test_list_total_with_blank_cells_does_not_claim_every_column():
    """合计行里 现金、刷卡 的格子是空的（P2-SPEC 4.4：空格不生成合计格），T 只核对了 金额 并通过：
    不能写「各列都核对一致」，要点名哪几列的合计格为空。只看合计格，不依赖 null_counts。"""
    notes = list_notes(derived=[list_item("金额")])
    assert notes.problems == []
    assert notes.tables["销售_表内合计"].comment == (
        "原表合计行的值。本期原表 现金、刷卡 的合计格为空，其余各列已按明细求和核对一致：不要与 销售 相加")
    assert "各列明细求和核对一致" not in notes.tables["销售_表内合计"].comment
    one = list_notes(derived=[list_item("金额"), list_item("刷卡")])
    assert "本期原表 现金 的合计格为空，其余各列" in one.tables["销售_表内合计"].comment


def test_list_total_not_kept():
    data = deepcopy(LIST_RECIPE)
    data["sheets"][0]["blocks"][0]["rows"]["total_row"]["keep_as"] = None
    data["tables"] = data["tables"][:1]
    notes = list_notes(data=data)
    assert notes.problems == []
    assert "原表的合计行未导入本表" in notes.tables["销售"].comment
    assert "销售_表内合计" not in notes.tables


def test_crosstab_total_not_kept():
    data = deepcopy(FLOW)
    data["sheets"][0]["blocks"][0]["segments"][3]["keep_as"] = None
    data["tables"] = data["tables"][:2]
    notes = notes_for(data)
    assert notes.problems == []
    assert "原表的合计行未导入本表" in notes.tables["时段客流"].comment


def test_hidden_rows_excluded_and_included():
    excluded = list_notes(hidden={"销售": {"rows": [4], "cols": [], "policy_rows": "exclude"}})
    assert "本表不含原表中被隐藏的行" in excluded.tables["销售"].comment
    data = deepcopy(LIST_RECIPE)
    data["sheets"][0]["hidden"] = {"rows": "include"}
    included = list_notes(data=data, hidden={"销售": {"rows": [4], "cols": [], "policy_rows": "include"}})
    assert "本表包含原表中被隐藏的行" in included.tables["销售"].comment
    # 隐藏行在本表占的行之外：不写
    far = list_notes(data=data, hidden={"销售": {"rows": [40], "cols": [], "policy_rows": "include"}})
    assert "隐藏" not in far.tables["销售"].comment
    assert "隐藏" not in list_notes().tables["销售"].comment


def test_ignored_columns_and_rows_after_stop():
    data = deepcopy(LIST_RECIPE)
    data["sheets"][0]["blocks"][0]["extra_columns"] = "ignore"
    notes = list_notes(data=data, ignored_columns={"明细": ["备注"]}, problems=[Problem(
        "rows_after_stop", "confirm", "第 9 行起有 2 行文字在空行之后，没有导入", cells=["销售!A9", "销售!B9"])])
    comment = notes.tables["销售"].comment
    assert "原表另有未导入的列" in comment
    assert "原表在空行之后另有未导入的文字行，内容见证据面板" in comment
    assert "备注" not in comment
    assert "未导入的列" not in list_notes(data=data).tables["销售"].comment


def test_rows_after_stop_goes_to_block_above():
    data = deepcopy(LIST_RECIPE)
    second = deepcopy(data["sheets"][0]["blocks"][0])
    second.update(id="费用", table="费用", after_title="二、费用")
    second["rows"] = {}
    data["sheets"][0]["blocks"].append(second)
    data["tables"].append({"name": "费用"})
    lineage = {"销售": {"金额": [[1, "销售", "D2", 6, "down"]]}, "费用": {"金额": [[1, "销售", "D20", 3, "down"]]}}
    notes = list_notes(data=data, lineage=lineage, problems=[Problem(
        "rows_after_stop", "confirm", "第 25 行起有 1 行文字在空行之后，没有导入", cells=["销售!A25", "销售!B25"])])
    assert "空行之后" in notes.tables["费用"].comment
    assert "空行之后" not in notes.tables["销售"].comment


# ---------------------------------------------------------------- 列说明：空值、日期、单位


def test_null_notes_follow_actual_nulls_when_counts_given():
    """给了每列空值数：只有本期真有空值的值列写空值说明（全日客流 只写单位）。不给时按配方推断。"""
    with_counts = notes_for()
    assert with_counts.tables["日客流"].columns["全日客流"] == "单位：人次"
    without = notes_for(null_counts=None)
    assert without.tables["日客流"].columns["全日客流"].startswith("单位：人次。空值表示原表为无数据占位符")


def test_four_arg_call_without_null_counts_is_conservative():
    """规格里 build_notes 是 4 参数调用（P2-SPEC 5.4、7.8 第 3 步）。Extraction 里的占位符计数是整份文件的、
    不分列，4 参数调用时只能对「可能有空值」的值列都写空值那一句，宁多勿漏。

    这条钉住这层依赖：9.7 第 2 步要的 全日客流 列说明是「单位：人次」，只有调用方把
    recipe_checks.column_null_counts 的结果作为 null_counts 传进来才成立（试运行预览和提交都要传）。
    """
    recipe = Recipe.model_validate(deepcopy(FLOW))
    four = build_notes(recipe, flow_extraction(), flow_checks(), [])
    assert four.problems == []
    cols = four.tables["日客流"].columns
    assert cols["全日客流"] == "单位：人次。空值表示原表为无数据占位符，不代表零；求和时请同时统计非空个数"
    assert four.tables["时段客流"].columns["客流"].startswith("单位：人次。空值表示原表为无数据占位符")
    # 合计表的空值由表说明交代，哪种调用都不写
    assert four.tables[TOTALS].columns["客流"] == "单位：人次"
    given = build_notes(recipe, flow_extraction(), flow_checks(), [], null_counts=NULLS)
    assert given.tables["日客流"].columns["全日客流"] == "单位：人次"
    # 表说明不受影响
    assert {t: n.comment for t, n in four.tables.items()} == {t: n.comment for t, n in given.tables.items()}


def test_null_note_variants():
    data = deepcopy(FLOW)
    data["sheets"][0]["blocks"][0]["values"]["placeholders"] = [{"text": "—", "meaning": "不适用"}]
    ex = flow_extraction(placeholders={"—": 31})
    assert notes_for(data, extraction=ex).tables["时段客流"].columns["客流"] == (
        "单位：人次。空值表示原表为不适用占位符，不代表零；求和时请同时统计非空个数")
    data["sheets"][0]["blocks"][0]["values"]["blank"] = "null"
    data["sheets"][0]["blocks"][0]["values"]["placeholders"].append({"text": "·"})
    ex = flow_extraction(placeholders={"—": 3, "·": 4})
    assert notes_for(data, extraction=ex).tables["时段客流"].columns["客流"] == (
        "单位：人次。空值表示原表为无数据或不适用占位符或空格，不代表零；求和时请同时统计非空个数")
    # 配了占位符但本期没出现、又不收空格：没有空值来源，不写
    ex = flow_extraction(placeholders={})
    data["sheets"][0]["blocks"][0]["values"]["blank"] = "reject"
    assert notes_for(data, extraction=ex, null_counts=None).tables["时段客流"].columns["客流"] == "单位：人次"


def test_axis_with_own_year_drops_year_hint():
    ex = flow_extraction(axes=[AxisOut("交叉表", 4, "2026-08-01", "2026-08-31", 31, "date")])
    assert notes_for(extraction=ex).tables["日客流"].columns["日期"] == "格式 YYYY-MM-DD"
    data = deepcopy(FLOW)
    data["sheets"][0]["blocks"][0]["axis"]["year_from"] = None
    assert notes_for(data).tables["日客流"].columns["日期"] == "格式 YYYY-MM-DD"


def test_hour_range_without_derive_uses_plain_template():
    data = {
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": SHEET}, "blocks": [{
            "id": "交叉表", "layout": "crosstab", "axis": {"year_from": None},
            "segments": [{"id": "日间", "role": "dimension", "table": "时段客流",
                          "locate": {"by": "section_title", "title": "日间时段客流（人次）"},
                          "labels": {"expect": ["7-8", "8-9"]}, "dim": {"name": "时段", "parser": "hour_range"},
                          "value": "客流"}]}]}],
        "tables": [{"name": "时段客流", "grain": ["日期", "时段"]}],
    }
    notes = build_notes(Recipe.model_validate(data), Extraction(ok=True), [], [])
    assert notes.problems == []
    assert "时段 按规范写法存储：起始小时与结束小时用连字符相连，不带单位" in notes.tables["时段客流"].comment


# ---------------------------------------------------------------- apply_notes


def _cache() -> dict[str, Any]:
    def cols(*names):
        return [{"name": n, "type": "INTEGER", "nullable": True} for n in names]

    return {"tables": {
        "日客流": {"columns": cols("日期", "全日客流", "分区甲", "分区乙"), "primary_key": ["日期"]},
        "时段客流": {"columns": cols("日期", "时段", "时段类别", "起始小时", "结束小时", "客流"),
                   "primary_key": ["日期", "时段"]},
        TOTALS: {"columns": cols("日期", "合计项", "起始小时", "结束小时", "客流"), "primary_key": ["日期", "合计项"]},
    }, "synced_at": "2026-09-01T00:00:00"}


def test_apply_notes_writes_comments_without_touching_input():
    cache = _cache()
    frozen = json.dumps(cache, sort_keys=True, ensure_ascii=False)
    notes = notes_for()
    out = apply_notes(cache, notes)
    assert json.dumps(cache, sort_keys=True, ensure_ascii=False) == frozen
    assert out["import_mode"] == "recipe" and out["synced_at"] == cache["synced_at"]
    assert out["tables"]["日客流"]["comment"] == notes.tables["日客流"].comment
    cols = {c["name"]: c for c in out["tables"]["时段客流"]["columns"]}
    assert cols["客流"]["comment"] == notes.tables["时段客流"].columns["客流"]
    assert "comment" not in cols["时段"]
    assert notes.problems == []


def test_apply_notes_records_unknown_tables_and_columns():
    cache = _cache()
    del cache["tables"][TOTALS]
    cache["tables"]["日客流"]["columns"] = cache["tables"]["日客流"]["columns"][:2]
    notes = notes_for()
    out = apply_notes(cache, notes)
    assert any(TOTALS in p for p in notes.problems)
    assert any("分区甲" in p for p in notes.problems)
    assert TOTALS not in out["tables"]


# ---------------------------------------------------------------- 工具描述与冻结白名单


def _source(cache: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(name="passenger_flow", description="", readonly=True, schema_cache=cache, kind="sqlite")


def test_query_description_adds_hint_only_for_recipe_sources():
    from app.tools.datasource import RECIPE_QUERY_HINT, _query_description

    recipe = _query_description(_source({**_cache(), "import_mode": "recipe"}))
    manual = _query_description(_source(_cache()))
    simple = _query_description(_source({**_cache(), "import_mode": "simple"}))
    hint = "中文表名和列名请加双引号。查单个值时，把主键列一起选出来，证据面板才能追到原表格子。"
    assert hint in recipe and RECIPE_QUERY_HINT.strip() == hint
    assert recipe.index("可用表") < recipe.index(hint)
    assert hint not in manual and hint not in simple
    assert recipe.replace(RECIPE_QUERY_HINT, "") == manual
    assert prose_problems(hint) == []


async def test_store_schema_freezes_import_manifests_and_keeps_manual_sources_identical():
    from app.core.artifact_store import canonical_json, content_hash, load
    from app.tools.datasource import _store_schema

    ctx = SimpleNamespace(run_id="", node_id="")
    manual = {**_cache(), "schema": "main", "truncated": False, "total": 3, "available_schemas": ["x"]}
    got = await _store_schema(_source(manual), ctx)
    # 改动前的写法：只复制 schema、synced_at、truncated、total 四个键
    before = {"source": "passenger_flow", "tables": manual["tables"],
              **{k: manual[k] for k in ("schema", "synced_at", "truncated", "total") if k in manual}}
    assert got == content_hash(canonical_json(before))

    recipe = {**_cache(), "import_mode": "recipe", "import_manifests": ["m" * 64]}
    frozen_id = await _store_schema(_source(recipe), ctx)
    frozen = load(frozen_id)
    assert frozen["import_manifests"] == ["m" * 64] and frozen["import_mode"] == "recipe"
    assert frozen_id != got
