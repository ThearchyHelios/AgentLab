"""期 3 的说明（recipe_notes，P3-SPEC 2.8、3.2 ⑥）：build_notes 返回片段、单期的忽略说明、按期累积的并集说明。

不依赖执行器、核对模块的库和 WP-4 的物化：核对结果、Extraction、各期空值数都在测试里手写（假名、假数），
并集的表结构用 derive_tables 加手写的退役列模拟 materialize_union 的 UnionReport.tables。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.data import recipe_parsers as P
from app.data.recipe_notes import (
    COLUMN_TEXT, CONSISTENT_KEYS, NOTE_TEXT, add_single_after_drop, build_notes, build_union_notes,
)
from app.data.recipe_types import (
    CheckResult, ColumnOut, ExcludedRows, Extraction, OutsideText, PeriodOut, Recipe, SheetsOut, UnionNotePart,
    derive_tables, prose_problems,
)
from tests.test_recipe_notes import (
    FLOW, LIST_RECIPE, NULLS, SHEET, TOTALS, accept, chk, flow_checks, flow_extraction, list_item, notes_for,
)

AUG = ("2026-08-01", "2026-08-31")
SEP = ("2026-09-01", "2026-09-30")
OCT = ("2026-10-01", "2026-10-31")

#: 期 3 新增的模板（P3-SPEC 2.8 的代码块，加上单期的 ignored_rows、ignored_cells）
NEW_NOTE_KEYS = {
    "accumulate", "accumulate_gap", "single_after_drop", "table_partial_periods", "table_absent",
    "sum_eq_union_mixed", "sum_eq_union_pending", "sum_eq_union_partial", "sum_eq_members_vary",
    "total_union_unverified", "period_conflict_union", "period_conflict_union_pending", "period_human_union",
    "period_human_all", "outside_digits_union", "hidden_excluded_union", "hidden_included_union",
    "ignored_columns_union", "ignored_rows", "ignored_rows_union", "ignored_cells", "ignored_cells_union",
    "dim_values_vary",
}
NEW_COLUMN_KEYS = {"col_partial_periods", "col_absent", "col_retired"}


def acc(data: dict | None = None) -> dict:
    d = deepcopy(data or FLOW)
    d["mode"] = "accumulate"
    return d


def part(data: dict | None = None, span: tuple[str, str] = AUG, *, checks: list[CheckResult] | None = None,
         acceptances: list | None = None, nulls: Any = NULLS, **ex: Any) -> UnionNotePart:
    ex.setdefault("period", PeriodOut(span[0], span[1], "cells", [f"{SHEET}!B2"]))
    return UnionNotePart(Recipe.model_validate(acc(data)), flow_extraction(**ex),
                         flow_checks() if checks is None else checks, acceptances or [], nulls, span[0], span[1])


def union(parts: list[UnionNotePart], target: dict | None = None, *,
          extra_cols: dict[str, list[ColumnOut]] | None = None, extra_tables: dict[str, list[ColumnOut]] | None = None,
          part_nulls: list | None = None, **kw: Any):
    """并集的表结构 = 目标配方推出的表 + 退役的列（extra_cols）+ 退役的表（extra_tables），模拟 UnionReport.tables。"""
    tgt = Recipe.model_validate(acc(target)) if target is not None else parts[-1].recipe
    tables, _ = derive_tables(tgt)
    tables = {t: list(cs) for t, cs in tables.items()}
    for t, cols in (extra_cols or {}).items():
        tables[t] = tables.get(t, []) + cols
    tables.update(extra_tables or {})
    kw.setdefault("null_counts", None)
    kw.setdefault("added", [])
    kw.setdefault("retired", [])
    kw.setdefault("label_sets", [])
    kw.setdefault("gaps", False)
    return build_union_notes(tgt, parts, union_tables=tables,
                             part_null_counts=part_nulls if part_nulls is not None else [p.null_counts for p in parts],
                             **kw)


def comment(notes, table: str = "日客流") -> str:
    return notes.tables[table].comment


def without_r1_and_b() -> dict:
    """A11：去掉 measures 的分区乙，R1 不再登记（关系删掉）。"""
    d = deepcopy(FLOW)
    seg = d["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"] = ["全日客流（人次）", "分区甲（人次）"]
    seg["measures"] = {"全日客流（人次）": "全日客流", "分区甲（人次）": "分区甲"}
    d["tables"][0]["units"] = {"全日客流": "人次", "分区甲": "人次"}
    d["relations"] = d["relations"][1:]
    return d


def with_c() -> dict:
    """D13：日客流 加「分区丙（人次）」，R1 成员按系统发现更新。"""
    d = deepcopy(FLOW)
    seg = d["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"].append("分区丙（人次）")
    seg["measures"]["分区丙（人次）"] = "分区丙"
    d["tables"][0]["units"]["分区丙"] = "人次"
    d["relations"][0]["parts"] = ["分区甲", "分区乙", "分区丙"]
    return d


def no_totals() -> dict:
    """合计行改为不另存（keep_as 置空）：时段客流_表内合计 退役。"""
    d = deepcopy(FLOW)
    d["sheets"][0]["blocks"][0]["segments"][3]["keep_as"] = None
    d["tables"] = d["tables"][:2]
    return d


NULLS_C = {**NULLS, "日客流": {**NULLS["日客流"], "分区丙": 0}}
NULLS_NO_B = {**NULLS, "日客流": {k: v for k, v in NULLS["日客流"].items() if k != "分区乙"}}


# ---------------------------------------------------------------- build_notes 返回片段，渲染不变


@pytest.mark.parametrize("override", [
    {},
    {"R1": chk("R1", "relation_sum_eq", "mismatch", "data_quality", checked=30, failed=1, acceptable=True)},
    {"K1": chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={"uncached": 3})},
    {"R2": chk("R2", "relation_not_comparable", "info", "info", checked=31, failed=0)},
    {"C1": chk("C1", "context_agree", "mismatch", "data_quality", acceptable=True)},
])
def test_fragments_join_to_the_same_template(override):
    """片段只是多返回的结构：按顺序用「。」拼起来，就是期 2 的表说明模板，一字不差。"""
    notes = notes_for(checks=flow_checks(**override))
    for table, frags in notes.fragments.items():
        assert "。".join(f.template for f in frags) == notes.templates.get(table, "")
    assert set(notes.fragments) == set(notes.tables)


def test_reference_fragments_keys_and_subjects():
    notes = notes_for()
    got = {t: [(f.key, f.subject) for f in fs] for t, fs in notes.fragments.items()}
    assert got["日客流"] == [("grain_1", "日客流"), ("sum_eq_passed", "R1"), ("not_comparable_unequal", "R2")]
    assert got["时段客流"] == [("grain_2", "时段客流"), ("not_comparable_unequal", "R2"), ("hour_range", "时段"),
                              ("const", "时段类别"), ("total_kept", TOTALS)]
    assert got[TOTALS] == [("grain_2", TOTALS), ("total_k_passed", TOTALS), ("total_overlap", "时段客流")]
    # 每个片段的 key 都是 NOTE_TEXT 的键（用户写的表说明记作 note）
    data = deepcopy(FLOW)
    data["tables"][0]["note"] = "{列:分区甲} 只统计闸机"
    keys = {f.key for fs in notes_for(data).fragments.values() for f in fs}
    assert keys <= set(NOTE_TEXT) | {"note"} and "note" in keys


# ---------------------------------------------------------------- 单期：按配方忽略的行、单元格、列（⑥）


def test_ignored_rows_note_only_for_tables_of_that_block():
    ex = flow_extraction(rows_excluded=[ExcludedRows(SHEET, "ignored_rows", [[32, 32]], 3, "补录（人次）", "交叉表")])
    notes = notes_for(extraction=ex)
    assert notes.problems == []
    for t in ("日客流", "时段客流", TOTALS):
        assert "原表另有按配方忽略的行，内容见证据面板" in comment(notes, t)
        assert "补录" not in comment(notes, t)
    # 别的块、别的原因、没有行：都不写
    for e in (ExcludedRows(SHEET, "ignored_rows", [[32, 32]], 3, "补录", "别的块"),
              ExcludedRows(SHEET, "blank_skipped", [[9, 9]], 0, None, "交叉表"),
              ExcludedRows(SHEET, "ignored_rows", [], 0, "补录", "交叉表")):
        assert "按配方忽略的行" not in comment(notes_for(extraction=flow_extraction(rows_excluded=[e])))


def test_ignored_cells_note_for_tables_of_that_sheet_and_dict_form():
    e = ExcludedRows(SHEET, "ignored_outside", [[33, 33]], 2, "补录说明", None)
    notes = notes_for(extraction=flow_extraction(rows_excluded=[e]))
    assert all("原表另有按配方忽略的单元格，内容见证据面板" in n.comment for n in notes.tables.values())
    # 暂存区、清单里存的是 JSON 形状：dict 一样认
    as_dict = {"sheet": SHEET, "reason": "ignored_outside", "rows": [[33, 33]], "cells": 2, "anchor": "补录说明",
               "block": None}
    assert "按配方忽略的单元格" in comment(notes_for(extraction=flow_extraction(rows_excluded=[as_dict])))
    other = ExcludedRows("别的表", "ignored_outside", [[33, 33]], 2, "补录说明", None)
    assert "按配方忽略的单元格" not in comment(notes_for(extraction=flow_extraction(rows_excluded=[other])))


def test_crosstab_ignore_columns_reuse_ignored_columns_note():
    data = deepcopy(FLOW)
    data["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "合计", "reason": "右侧的月合计列不导入"}]
    ex = flow_extraction(ignored_columns={"交叉表": ["合计"]})
    assert "原表另有未导入的列" in comment(notes_for(data, extraction=ex))
    # 配了规则、本期没有命中：不写；没配规则（期 2 的交叉表）：照期 2 不写
    assert "未导入的列" not in comment(notes_for(data))
    assert "未导入的列" not in comment(notes_for(extraction=ex))


# ---------------------------------------------------------------- 新模板：过数字和引用检查，不含「一致」

_SLOTS = {"col": "{列:日期}", "dim": "{列:时段}", "total": "{列:全日客流}", "parts": "{列:分区甲}、{列:分区乙}",
          "unit": "日期"}
_KNOWN = {"列": {"日期", "时段", "全日客流", "分区甲", "分区乙"}, "表": set(), "单位": set(P.UNITS)}


@pytest.mark.parametrize("key", sorted(NEW_NOTE_KEYS))
def test_new_table_templates_pass_prose_check_and_never_say_consistent(key):
    assert key in NOTE_TEXT and key not in CONSISTENT_KEYS
    text = NOTE_TEXT[key].format(**_SLOTS)
    assert prose_problems(text, known=_KNOWN) == []
    assert "一致" not in text


@pytest.mark.parametrize("key", sorted(NEW_COLUMN_KEYS))
def test_new_column_templates_pass_prose_check(key):
    assert prose_problems(COLUMN_TEXT[key], known=_KNOWN) == [] and "一致" not in COLUMN_TEXT[key]


def test_pending_templates_never_say_accepted():
    for key in ("sum_eq_union_pending", "period_conflict_union_pending"):
        assert "已由用户确认接受" not in NOTE_TEXT[key] and "尚未确认接受" in NOTE_TEXT[key]
    for key in ("sum_eq_union_partial", "sum_eq_union_mixed", "sum_eq_union_pending"):
        assert "已逐" not in NOTE_TEXT[key] and "导入时已" not in NOTE_TEXT[key]


# ---------------------------------------------------------------- 并集：参考配方两期


def test_two_periods_reference_union():
    notes = union([part(), part(span=SEP)])
    assert notes.problems == []
    assert comment(notes) == (
        "粒度：每个日期。全日客流 等于 分区甲 与 分区乙 之和（导入时已逐日核对）：求合计只用 全日客流，不要把这几列相加。"
        "与 时段客流 口径不同：各时段之和不等于 全日客流，不能互相推算或相加。"
        "本表按统计期累积多期数据，各期互不重叠：比较不同期时请按 日期 筛选")
    assert comment(notes, TOTALS).startswith("粒度：每个日期的每个合计项。原表写明的合计，已按明细重算核对一致")
    assert notes.kinds == {TOTALS: "reported_total"}
    assert notes.tables["日客流"].columns == {"日期": "格式 YYYY-MM-DD，年份按统计期补全", "全日客流": "单位：人次",
                                            "分区甲": "单位：人次", "分区乙": "单位：人次"}
    # 片段就是渲染前的模板，按顺序拼起来
    for t, frags in notes.fragments.items():
        assert "。".join(f.template for f in frags) == notes.templates[t]
    assert all("当前版本只含最近一期" not in n.comment for n in notes.tables.values())


def test_gap_uses_gap_sentence():
    notes = union([part(), part(span=OCT)], gaps=True)
    assert "各期之间有空缺：比较不同期之前请先查询 日期 的覆盖范围" in comment(notes)
    assert "各期互不重叠：比较不同期时请按" not in comment(notes)


# ---------------------------------------------------------------- 并集：sum_eq 的每一行

R1_MISMATCH = chk("R1", "relation_sum_eq", "mismatch", "data_quality", checked=30, failed=1, acceptable=True)


def test_sum_eq_one_period_accepted_mismatch_is_mixed():
    """D26 与 8 月累积（P3-SPEC 2.8 的例子）：不再出现「已逐日核对」，用 sum_eq_union_mixed。"""
    notes = union([part(), part(span=SEP, checks=flow_checks(R1=R1_MISMATCH), acceptances=accept("R1"))])
    assert notes.problems == []
    c = comment(notes)
    assert ("全日客流 与 分区甲、分区乙 之和的关系在部分期的个别日期不成立或未能核对，详见导入清单："
            "不要据此推算，也不要把这几列相加") in c
    assert "已逐日核对" not in c and "求合计只用" not in c


@pytest.mark.parametrize("r1", [
    chk("R1", "relation_sum_eq", "unverifiable", "data_quality", unverifiable=2, acceptable=True),
    chk("R1", "relation_sum_eq", "unverifiable", "data_quality", checked=0, unverifiable=0, acceptable=True),
])
def test_sum_eq_nulls_or_no_rows_is_mixed(r1):
    c = comment(union([part(checks=flow_checks(R1=r1), acceptances=accept("R1")), part(span=SEP)]))
    assert "在部分期的个别日期不成立或未能核对" in c and "已逐日核对" not in c


def test_sum_eq_pending_never_says_accepted():
    """试运行预览：本期按「全部未接受」生成。早期那一期接受过也不行：合并结果不得出现「已由用户确认接受」。"""
    old = part(checks=flow_checks(R1=R1_MISMATCH), acceptances=accept("R1"))
    new = part(span=SEP, checks=flow_checks(R1=R1_MISMATCH))
    c = comment(union([old, new]))
    assert "尚未确认接受" in c and "已由用户确认接受" not in c and "已逐日核对" not in c
    # 反例：两期都接受了，才写「不成立或未能核对」（不是「尚未确认接受」）
    both = comment(union([old, part(span=SEP, checks=flow_checks(R1=R1_MISMATCH), acceptances=accept("R1"))]))
    assert "尚未确认接受" not in both and "不成立或未能核对" in both


def test_sum_eq_members_vary():
    """D13 修复后累积：8 月 R1 = 甲 + 乙，9 月 R1 = 甲 + 乙 + 丙，都通过：sum_eq_members_vary。"""
    notes = union([part(), part(with_c(), SEP, nulls=NULLS_C)], part_nulls=[NULLS, NULLS_C])
    assert notes.problems == []
    c = comment(notes)
    assert "全日客流 等于各期登记的分项列之和，各期登记的分项列不同（某期没有的列为空值），导入时已逐期核对" in c
    assert "已逐日核对" not in c
    # 列：8 月没有分区丙，结构性空值由 col_partial_periods 说明；9 月这一列本身没有空值，不写「原表为空格」
    assert notes.tables["日客流"].columns["分区丙"] == "单位：人次。部分期没有这一列的数据，为空值"


def test_sum_eq_reordered_parts_are_the_same_members():
    """9 月的 R1 只把分项倒了个序：成员相同，用目标那一期（9 月）的「已逐日核对」句子，不写「各期登记的分项列不同」
    （这句会进模型可见的说明，评审意见）。"""
    swapped = deepcopy(FLOW)
    swapped["relations"][0]["parts"] = list(reversed(swapped["relations"][0]["parts"]))
    notes = union([part(), part(swapped, SEP)])
    assert notes.problems == []
    c = comment(notes)
    assert "全日客流 等于 分区乙 与 分区甲 之和（导入时已逐日核对）" in c
    assert "各期登记的分项列不同" not in c
    # 反例：成员真的不同（D13 加了分区丙）仍是 sum_eq_members_vary
    assert "各期登记的分项列不同" in comment(union([part(), part(with_c(), SEP, nulls=NULLS_C)],
                                                   part_nulls=[NULLS, NULLS_C]))


def test_sum_eq_partial_when_target_period_drops_relation():
    """A11：9 月去掉分区乙、R1 不再登记。8 月有 R1（通过），9 月没有：sum_eq_union_partial，成员取 8 月的。"""
    notes = union([part(), part(without_r1_and_b(), SEP, nulls=NULLS_NO_B)],
                  extra_cols={"日客流": [ColumnOut("分区乙", "INTEGER", header=None, unit=None, role="measure")]},
                  part_nulls=[NULLS, {**NULLS_NO_B, "日客流": {**NULLS_NO_B["日客流"], "分区乙": 30}}])
    assert notes.problems == []
    c = comment(notes)
    assert ("全日客流 与 分区甲、分区乙 之和的关系只在部分期登记并核对，其余各期未核对，详见导入清单："
            "不要据此推算，也不要把这几列相加") in c
    assert "已逐日核对" not in c
    # 退役列：单位取最后一个含它的那一期（8 月）的，并集给的 unit 为空也一样；说明 col_retired
    col = notes.tables["日客流"].columns["分区乙"]
    assert col == "单位：人次。这一列自某一期起不再导入，此后各期为空值"
    assert "原表为空格" not in col


def test_sum_eq_partial_when_only_new_period_registers_it():
    """反过来：8 月没登记 R1，9 月登记了。照样是「只在部分期登记并核对」，不沿用 9 月的「已逐日核对」。"""
    old = deepcopy(FLOW)
    old["relations"] = old["relations"][1:]
    c = comment(union([part(old, checks=flow_checks(R1=None)), part(span=SEP)]))
    assert "只在部分期登记并核对" in c and "已逐日核对" not in c


# ---------------------------------------------------------------- 并集：not_comparable、K、其余各族


def test_not_comparable_equal_in_one_period_drops_not_equal():
    eq = chk("R2", "relation_not_comparable", "info", "info", checked=30, failed=0)
    notes = union([part(), part(span=SEP, checks=flow_checks(R2=eq))])
    for t in ("日客流", "时段客流"):
        assert "口径不同，不能互相推算或相加" in comment(notes, t) and "不等于" not in comment(notes, t)
    both = union([part(), part(span=SEP)])
    assert "各时段之和不等于 全日客流" in comment(both)


@pytest.mark.parametrize("reason", ["uncached", "blank", "null_detail", "tiling", "hidden"])
def test_k_not_passed_in_any_period_never_says_consistent(reason):
    k = chk("K1", "derived_sum", "unverifiable", acceptable=True, reasons={reason: 3})
    for order in ((part(), part(span=SEP, checks=flow_checks(K1=k), acceptances=accept("K1"))),
                  (part(checks=flow_checks(K1=k), acceptances=accept("K1")), part(span=SEP))):
        c = comment(union(list(order)), TOTALS)
        assert "原表写明的合计，按原表保存的值存储。部分期的合计值未能核对，详见导入清单" in c
        assert "一致" not in c


def test_k_passed_g_unverifiable_in_one_period():
    g = chk("G1", "formula_refs", "unverifiable", acceptable=True, reasons={"unrecognized": 1})
    c = comment(union([part(), part(span=SEP, checks=flow_checks(G1=g), acceptances=accept("G1"))]), TOTALS)
    assert "原表写明的合计，合计值已按明细核对一致，公式引用的格子未能核对" in c


def test_k_blocking_in_new_period_is_not_inherited():
    """本期 K 不一致（试运行本来就拒收）：本期的合计片段不写，合并时不能沿用 8 月的「核对一致」。"""
    bad = chk("K1", "derived_sum", "mismatch", failed=1)
    c = comment(union([part(), part(span=SEP, checks=flow_checks(K1=bad))]), TOTALS)
    assert "一致" not in c and "部分期的合计值未能核对" in c


@pytest.mark.parametrize("ex,expect", [
    ({"hidden": {SHEET: {"rows": [12], "cols": [], "policy_rows": "exclude"}}}, "部分期的原表有被隐藏的行，本表不含这些行"),
    ({"outside_text": [OutsideText(SHEET, "B32", "注：9月15日闸机故障", "text_digits")]},
     "部分期的原表附有说明文字（可能涉及口径），内容见证据面板"),
    ({"rows_excluded": [ExcludedRows(SHEET, "ignored_rows", [[32, 32]], 3, "补录", "交叉表")]},
     "部分期的原表另有按配方忽略的行，内容见证据面板"),
    ({"rows_excluded": [ExcludedRows(SHEET, "ignored_outside", [[33, 33]], 2, "补录说明", None)]},
     "部分期的原表另有按配方忽略的单元格，内容见证据面板"),
])
def test_any_period_families_use_union_wording(ex, expect):
    data = deepcopy(FLOW)
    if "hidden" in ex:
        data["sheets"][0]["hidden"] = {"rows": "exclude"}
    notes = union([part(data), part(data, SEP, **ex)], target=data)
    assert notes.problems == []
    assert expect in comment(notes)
    assert "本期" not in comment(notes)
    assert expect not in comment(union([part(data), part(data, SEP)], target=data))


def test_hidden_included_and_ignored_columns_union():
    data = deepcopy(FLOW)
    data["sheets"][0]["hidden"] = {"rows": "include"}
    data["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "合计", "reason": "月合计列不导入"}]
    p = part(data, SEP, hidden={SHEET: {"rows": [12], "cols": [], "policy_rows": "include"}},
             ignored_columns={"交叉表": ["合计"]})
    c = comment(union([part(data), p], target=data))
    assert "部分期的原表有被隐藏的行，本表包含这些行" in c and "部分期的原表另有未导入的列" in c


def test_period_conflict_pending_and_accepted():
    c1 = chk("C1", "context_agree", "mismatch", "data_quality", acceptable=True)
    pending = comment(union([part(checks=flow_checks(C1=c1), acceptances=accept("C1")),
                             part(span=SEP, checks=flow_checks(C1=c1))]))
    assert "部分期的统计期写法在原表或文件名中有冲突，尚未确认接受" in pending
    assert "已由用户确认接受" not in pending
    accepted = comment(union([part(checks=flow_checks(C1=c1), acceptances=accept("C1")), part(span=SEP)]))
    assert "部分期的统计期写法在原表或文件名中有冲突，已由用户确认接受，详见导入清单" in accepted
    assert "冲突" not in comment(union([part(), part(span=SEP)]))


def test_period_human_some_and_all():
    human = {"period": PeriodOut(SEP[0], SEP[1], "human", signed_by="甲")}
    some = comment(union([part(), part(span=SEP, **human)]))
    assert "部分期的统计期为人工录入" in some and "均为" not in some
    both = comment(union([part(period=PeriodOut(AUG[0], AUG[1], "human")), part(span=SEP, **human)]))
    assert "各期的统计期均为人工录入" in both


def test_dim_values_vary():
    ls = [{"table": "时段客流", "column": "时段", "segment": "日间",
           "periods": [{"start": SEP[0], "end": SEP[1], "missing": ["7-8"], "extra": []}]}]
    notes = union([part(), part(span=SEP)], label_sets=ls)
    assert "各期的 时段 取值不完全相同：跨期比较前请先确认两期都有的取值" in comment(notes, "时段客流")
    assert "取值不完全相同" not in comment(notes)


# ---------------------------------------------------------------- 并集：表的种类、退役的表、部分期的表


def test_retired_total_table_keeps_kind_and_last_period_description():
    """目标配方把合计行改为不另存：时段客流_表内合计 退役。并集里仍标 reported_total，说明取 8 月的（只有 8 月有行）；
    明细表同时写着「合计在 时段客流_表内合计」（8 月）和「合计行未导入本表」（9 月）。"""
    totals_cols = derive_tables(Recipe.model_validate(FLOW))[0][TOTALS]
    notes = union([part(), part(no_totals(), SEP, checks=flow_checks(K1=None, G1=None))],
                  extra_tables={TOTALS: list(totals_cols)})
    assert notes.problems == []
    assert notes.kinds == {TOTALS: "reported_total"}
    t = comment(notes, TOTALS)
    assert "原表写明的合计，已按明细重算核对一致" in t and "部分期没有这张表的数据" in t
    base = comment(notes, "时段客流")
    assert "原表的合计在 时段客流_表内合计，不要与本表相加" in base and "原表的合计行未导入本表" in base


def test_new_table_only_in_new_period_is_partial():
    totals_free = no_totals()
    notes = union([part(totals_free, checks=flow_checks(K1=None, G1=None)), part(span=SEP)])
    assert notes.problems == []
    t = comment(notes, TOTALS)
    assert "部分期没有这张表的数据" in t and "原表写明的合计，已按明细重算核对一致" in t
    assert "部分期没有这张表的数据" not in comment(notes)


# ---------------------------------------------------------------- 并集：空值只按含这一列的期


def test_nulls_only_from_periods_that_have_the_column():
    """8 月没有分区丙（并集里是结构性空值），9 月分区丙没有空值：不写「空值表示原表为…」；9 月真有空值才写。"""
    sep_nulls = {**NULLS_C, "日客流": {**NULLS_C["日客流"], "分区丙": 2}}
    data = with_c()
    data["sheets"][0]["blocks"][0]["values"]["blank"] = "null"
    clean = union([part(), part(data, SEP, nulls=NULLS_C)], target=data, part_nulls=[NULLS, NULLS_C],
                  null_counts={"日客流": {"分区丙": 31}})
    assert clean.tables["日客流"].columns["分区丙"] == "单位：人次。部分期没有这一列的数据，为空值"
    dirty = union([part(), part(data, SEP, nulls=sep_nulls)], target=data, part_nulls=[NULLS, sep_nulls])
    col = dirty.tables["日客流"].columns["分区丙"]
    assert "空值表示原表为无数据占位符或空格" in col and col.endswith("部分期没有这一列的数据，为空值")


def test_placeholder_meanings_differ_between_periods():
    sep_data = deepcopy(FLOW)
    sep_data["sheets"][0]["blocks"][0]["values"]["placeholders"] = [{"text": "—", "meaning": "不适用"}]
    notes = union([part(), part(sep_data, SEP, placeholders={"—": 5})], target=sep_data)
    assert notes.tables["时段客流"].columns["客流"] == (
        "单位：人次。空值表示原表为无数据或不适用占位符，不代表零；求和时请同时统计非空个数")
    same = union([part(), part(span=SEP)])
    assert "无数据占位符" in same.tables["时段客流"].columns["客流"]


def test_union_null_counts_zero_skips_null_note():
    notes = union([part(), part(span=SEP)], null_counts={"时段客流": {"客流": 0}})
    assert notes.tables["时段客流"].columns["客流"] == "单位：人次"


# ---------------------------------------------------------------- 单期物化（移除、作废之后）与 restart


def test_single_materialized_part_uses_col_absent_and_no_partial_wording():
    """A17：只剩 8 月、而目标配方是 9 月加了分区丙的 r3：分区丙 col_absent，不说「部分期」；加 single_after_drop。"""
    notes = union([part()], target=with_c())
    assert notes.problems == []
    assert notes.tables["日客流"].columns["分区丙"] == "单位：人次。当前版本的数据中没有这一列，为空值"
    for n in notes.tables.values():
        assert "部分期" not in n.comment and "当前版本只含最近一期的数据，此前各期不在当前版本中" in n.comment
        assert "按统计期累积多期数据" not in n.comment
    # 单期时那一期自己的说法就是准确的
    assert "导入时已逐日核对" in comment(notes)


def test_single_materialized_part_table_absent():
    notes = union([part(no_totals(), checks=flow_checks(K1=None, G1=None))], target=FLOW)
    assert notes.problems == []
    assert comment(notes, TOTALS) == ("粒度：每个日期的每个合计项。当前版本的数据中没有这张表的行。"
                                      "当前版本只含最近一期的数据，此前各期不在当前版本中")
    assert set(notes.tables[TOTALS].columns.values()) >= {"单位：人次。当前版本的数据中没有这一列，为空值"}


def test_add_single_after_drop_appends_without_touching_input():
    notes = notes_for()
    before = {t: n.comment for t, n in notes.tables.items()}
    out = add_single_after_drop(notes)
    assert {t: n.comment for t, n in notes.tables.items()} == before
    for t, n in out.tables.items():
        assert n.comment == before[t] + "。当前版本只含最近一期的数据，此前各期不在当前版本中"
        assert out.fragments[t][-1].key == "single_after_drop"
        assert "。".join(f.template for f in out.fragments[t]) == out.templates[t]
    assert add_single_after_drop(out).tables["日客流"].comment == out.tables["日客流"].comment


# ---------------------------------------------------------------- 「目标那一期」与结构类片段


def test_structural_fragments_come_from_target_period_even_when_backfilled():
    """补传早期：本期（目标配方）排在最前。用户写的表说明取目标那一期的，不取最晚那一期的。"""
    old, new = deepcopy(FLOW), deepcopy(FLOW)
    old["tables"][0]["note"] = "{列:分区甲} 只统计东门"
    new["tables"][0]["note"] = "{列:分区甲} 只统计闸机"
    notes = union([part(new, ("2026-07-01", "2026-07-31")), part(old, AUG)], target=new)
    assert comment(notes).endswith("分区甲 只统计闸机") and "东门" not in comment(notes)


# ---------------------------------------------------------------- 输入不对：记 problems，不能保存


def test_problems_for_inconsistent_inputs():
    assert union([part(), part(span=SEP)], dropped=True).problems
    assert build_union_notes(Recipe.model_validate(acc()), [], union_tables={}, null_counts=None,
                             part_null_counts=None, added=[], retired=[], label_sets=[], gaps=False).problems
    # 并集的表结构漏了某一期的表
    tgt = Recipe.model_validate(acc())
    tables = {t: cs for t, cs in derive_tables(tgt)[0].items() if t != TOTALS}
    notes = build_union_notes(tgt, [part(), part(span=SEP)], union_tables=tables, null_counts=None,
                              part_null_counts=None, added=[], retired=[], label_sets=[], gaps=False)
    assert any(TOTALS in p for p in notes.problems)
    # 各期空值数的期数对不上
    assert union([part(), part(span=SEP)], part_nulls=[NULLS]).problems


def test_list_tables_cannot_be_merged_and_are_problems():
    """列表不能按期累积（P3-SPEC 2.2）：真把两期列表并起来，列表合计没有并集说法，记 problems，绝不沿用单期说法。"""
    data = deepcopy(LIST_RECIPE)

    def lpart(span):
        ex = Extraction(ok=True, derived=[list_item(c) for c in ("金额", "现金", "刷卡")],
                        sheets=SheetsOut(matched={"s1": "销售"}), lineage={"销售": {"金额": [[1, "销售", "D2", 6, "down"]]}})
        checks = [chk("T1", "column_sum"), chk("R1", "relation_sum_eq", category="data_quality")]
        return UnionNotePart(Recipe.model_validate(data), ex, checks, accept("T1"), None, span[0], span[1])

    tgt = Recipe.model_validate(data)
    notes = build_union_notes(tgt, [lpart(AUG), lpart(SEP)], union_tables=derive_tables(tgt)[0], null_counts=None,
                              part_null_counts=None, added=[], retired=[], label_sets=[], gaps=False)
    assert any("列表合计行的核对结论不能按期合并" in p for p in notes.problems)
    assert not any("list_total" in p for p in notes.problems)          # 不露键名
    assert "一致" not in notes.tables["销售_表内合计"].comment


def test_every_union_note_passes_prose_check():
    notes = union([part(), part(with_c(), SEP, nulls=NULLS_C, checks=flow_checks(R1=R1_MISMATCH))],
                  part_nulls=[NULLS, NULLS_C], gaps=True,
                  label_sets=[{"table": "时段客流", "column": "时段", "periods": []}])
    assert notes.problems == []
    known = {"列": {c.name for cs in derive_tables(Recipe.model_validate(with_c()))[0].values() for c in cs},
             "表": {"日客流", "时段客流", TOTALS}, "单位": set(P.UNITS)}
    for key, tpl in notes.templates.items():
        assert prose_problems(tpl, known=known) == [], key


# ---------------------------------------------------------------- 冻结白名单


async def test_store_schema_freezes_snapshot_manifest():
    from types import SimpleNamespace

    from app.core.artifact_store import load
    from app.tools.datasource import _FROZEN_SCHEMA_KEYS, _store_schema
    from tests.test_recipe_notes import _cache, _source

    assert "snapshot_manifest" in _FROZEN_SCHEMA_KEYS
    ctx = SimpleNamespace(run_id="", node_id="")
    cache = {**_cache(), "import_mode": "recipe", "import_manifests": ["a" * 64, "b" * 64],
             "snapshot_manifest": "s" * 64}
    frozen = load(await _store_schema(_source(cache), ctx))
    assert frozen["snapshot_manifest"] == "s" * 64 and frozen["import_manifests"] == ["a" * 64, "b" * 64]
    # 没有快照清单的源（单期、手工源）：冻结内容里没有这个键
    one = {**_cache(), "import_mode": "recipe", "import_manifests": ["a" * 64]}
    single = load(await _store_schema(_source(one), ctx))
    assert "snapshot_manifest" not in single
