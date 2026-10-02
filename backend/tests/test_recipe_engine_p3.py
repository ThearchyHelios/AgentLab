"""配方执行器的期 3 改动（P3-SPEC 9.2、3.1、3.2、第 8 节遗留项 4、11.3 WP-1、11.4 WP-1 守卫点）。

- 三种忽略规则（ignore_rows / ignore_columns / ignore_outside）的执行语义，以及「只忽略命中的」这几条守卫点；
- 修复按钮的参数 Problem.fix_args：每个有修复按钮的 code 一个形状断言，另有一组用特征数 987654 放在数据格里，
  断言任何 fix_args 都不含它；
- 区域标记带块 id，按块合矩形；
- 回执里「排除的行」的六种原因及其行号、格数；
- 没用新字段的配方，D00 构建库与期 2 逐字节相同（期 2 基线由 WP-0 在 2d5e50f 上算好，见 p2_baseline.json）。

夹具一律现造：期 2 引擎测试的 flow_book / sales_book，加上 fixtures/xlsx 的漂移夹具（假名、随机数）。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.data import raw_store, recipe_engine, table_versions, xlsx_cells, xlsx_scan
from app.data.names import canon
from app.data.recipe_types import FIX_KINDS, Extraction, Recipe
from tests.fixtures.xlsx import lab
from tests.fixtures.xlsx.flow import flow_workbook
from tests.test_recipe_engine_crosstab import FLOW, flow_book, recipe_dict
from tests.test_recipe_engine_crosstab import SHEET as FS
from tests.test_recipe_engine_list import LIST, list_recipe, sales_book
from tests.test_recipe_engine_list import SHEET as LS

N = 987654
SEP = dt.date(2026, 9, 1)
BASELINE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "p2_baseline.json").read_text(encoding="utf-8"))


def run(book: tuple[bytes, str], recipe: dict[str, Any], db: str | None = None, **kw: Any) -> Extraction:
    raw, filename = book
    return recipe_engine.execute(Recipe.model_validate(recipe), raw, filename, xlsx_scan.scan(raw), db, **kw)


def drift(code: str, recipe: dict[str, Any] | None = None, db: str | None = None) -> Extraction:
    return run(flow_workbook(SEP, 30, variant=code), recipe or FLOW, db)


def codes(ex: Extraction) -> set[str]:
    return {p.code for p in ex.problems}


def problems(ex: Extraction, code: str) -> list[Any]:
    found = [p for p in ex.problems if p.code == code]
    assert found, f"没有 {code}：{[(p.code, p.message) for p in ex.problems]}"
    return found


def excluded(ex: Extraction) -> list[dict[str, Any]]:
    return [asdict(x) for x in ex.rows_excluded]


def flow_with(**block: Any) -> dict[str, Any]:
    """参考配方，交叉表块上加期 3 的字段（ignore_rows / ignore_columns）。"""
    data = copy.deepcopy(FLOW)
    data["sheets"][0]["blocks"][0].update(block)
    return data


def rule(key: str, text: str) -> dict[str, str]:
    return {key: text, "reason": "合成测试：按文字忽略"}


def _set(**cells: Any):
    def edit(ws: Any, info: dict[str, Any]) -> None:
        for ref, value in cells.items():
            ws[ref] = value
    return edit


# ==========================================================================
# 期 2 基线：没用新字段的配方，库与期 2 逐字节相同
# ==========================================================================


def test_d00_build_is_byte_identical_to_phase_2(tmp_path):
    raw, name = flow_workbook(dt.date(2026, 8, 1), 31)
    assert name == BASELINE["fixture"]["file_name"]
    db = tmp_path / "d00.db"
    ex = run((raw, name), FLOW, str(db))
    table_versions.settle_journal(db)
    assert ex.ok and not ex.problems
    assert raw_store.sha256_file(db) == BASELINE["db_sha256"]
    assert ex.table_hashes == BASELINE["table_hashes"]
    assert {t.name: t.rows for t in ex.tables} == BASELINE["rows"]
    assert ex.rows_excluded == []


def test_rules_whose_anchors_do_not_appear_change_nothing(tmp_path):
    """锚点在某一期没出现：什么也不忽略、也不报错，库和期 2 一样（9.2「锚点在某一期没出现就什么也不忽略」）。"""
    data = flow_with(ignore_rows=[rule("label", "补录（人次）")], ignore_columns=[rule("header", "合计")])
    data["sheets"][0]["ignore_outside"] = [rule("anchor", "补录说明")]
    raw, name = flow_workbook(dt.date(2026, 8, 1), 31)
    db = tmp_path / "d00.db"
    ex = run((raw, name), data, str(db))
    table_versions.settle_journal(db)
    assert ex.ok and not ex.problems
    assert raw_store.sha256_file(db) == BASELINE["db_sha256"]
    assert ex.table_hashes == BASELINE["table_hashes"]
    assert ex.rows_excluded == [] and ex.ignored_columns == {} and "ignored" not in ex.ledger[0].roles


# ==========================================================================
# PROBLEM_FIX（3.1 的取值表）
# ==========================================================================


def test_problem_fix_table_follows_spec_3_1():
    assert recipe_engine.PROBLEM_FIX == {
        "label_missing": "remove_label", "label_unexpected": "add_label", "title_not_found": "rename_title",
        "label_unparsed": "declare_total", "value_not_number": "declare_placeholder",
        "axis_extra_cells": "ignore_cells", "column_extra": "ignore_cells", "row_unclaimed": "ignore_cells",
        "outside_number": "ignore_cells", "hidden_rows": "declare_hidden", "hidden_cols": "declare_hidden",
        "sheet_missing": "rename_sheet"}
    assert set(recipe_engine.PROBLEM_FIX.values()) <= set(FIX_KINDS)
    # edit_members 只来自静态校验；cell_unclaimed、rows_after_stop 没有修复按钮
    assert "edit_members" not in recipe_engine.PROBLEM_FIX.values()
    assert not {"cell_unclaimed", "rows_after_stop"} & set(recipe_engine.PROBLEM_FIX)


# ==========================================================================
# fix_args 的形状（3.2），按 3.4 的漂移例
# ==========================================================================


def test_fix_args_of_the_self_service_drift_cases():
    """3.4 表和 final.md 2.8「拒收、修复按钮能自助」那一组：fix 和坐标与 3.4 一致，fix_args 按 3.2 的形状。"""
    (p,) = problems(drift("D04"), "label_missing")
    assert (p.fix, p.cells, p.fix_args) == ("remove_label", [f"{FS}!B10"], {"segment": "日间", "labels": ["7-8"]})
    (p,) = problems(drift("D09"), "title_not_found")
    assert p.fix == "rename_title" and p.cells == []
    assert p.fix_args == {"segment": "日间", "title": "日间时段客流（人次）",
                          "candidates": [{"cell": f"{FS}!B9", "text": "日间分时段客流（人次）"}]}
    (p,) = problems(drift("D13"), "label_unexpected")
    assert p.fix == "add_label" and p.fix_args == {
        "segment": "日客流", "role": "measures", "labels": [{"raw": "分区丙（人次）", "cell": f"{FS}!B8"}]}
    (p,) = problems(drift("D16"), "axis_extra_cells")
    assert p.fix == "ignore_cells" and p.fix_args == {
        "block": "交叉表", "how": "columns", "headers": [{"raw": "合计", "cell": f"{FS}!AG4"}]}
    (p,) = problems(drift("D18"), "label_unparsed")
    assert p.fix == "declare_total" and p.fix_args == {
        "segment": "日间", "role": "dimension", "at_end": True,
        "labels": [{"raw": "7-18 时合计", "cell": f"{FS}!B21", "total": True}]}
    (p,) = problems(drift("D21"), "row_unclaimed")
    assert p.fix == "ignore_cells" and p.fix_args == {
        "block": "交叉表", "how": "rows", "labels": [{"raw": "补录（人次）", "cell": f"{FS}!B32"}]}


def test_title_not_found_candidates_are_unclaimed_titles_in_row_order():
    """两个分段同时找不到标题：两条问题各自带 fix_args（界面按 fix_ids 分得清），候选是本块里像分段标题、
    又没被认领的行，按行号升序；被别的分段认领的标题不在候选里。"""
    def edit(ws, info):
        ws.unmerge_cells(f"B{info['rows']['日间标题']}:{info['last']}{info['rows']['日间标题']}")
        ws.unmerge_cells(f"B{info['rows']['夜间标题']}:{info['last']}{info['rows']['夜间标题']}")
        ws[f"B{info['rows']['日间标题']}"] = "白天时段客流（人次）"
        ws[f"B{info['rows']['夜间标题']}"] = "晚上时段客流（人次）"
        ws["B35"] = "附注说明"                      # 表下另起的一行文字：也像分段标题，照样列为候选
        ws["B36"] = 2026                            # 标签列上的一个数：不是文字，不能当候选
    ex = run(flow_book(days=3, edit=edit), FLOW)
    ps = problems(ex, "title_not_found")
    assert [p.fix_args["segment"] for p in ps] == ["日间", "夜间"]
    want = [{"cell": f"{FS}!B9", "text": "白天时段客流（人次）"}, {"cell": f"{FS}!B21", "text": "晚上时段客流（人次）"},
            {"cell": f"{FS}!B35", "text": "附注说明"}]
    assert all(p.fix_args["candidates"] == want for p in ps)
    # 只有日间找不到时，夜间的标题已被认领，不是候选
    ex = run(flow_book(days=3, edit=_set(B9="白天时段客流（人次）")), FLOW)
    (p,) = problems(ex, "title_not_found")
    assert p.fix_args["candidates"] == [{"cell": f"{FS}!B9", "text": "白天时段客流（人次）"}]


def test_label_unparsed_at_end_only_when_trailing_and_contiguous():
    def middle(ws, info):
        ws["B12"] = "补测"                          # 日间第 3 行写成解析不了的文字：不在分段末尾
    (p,) = problems(run(flow_book(days=3, edit=middle), FLOW), "label_unparsed")
    assert p.fix_args == {"segment": "日间", "role": "dimension", "at_end": False,
                          "labels": [{"raw": "补测", "cell": f"{FS}!B12", "total": False}]}


def test_label_unexpected_in_a_dimension_segment():
    def extra_hour(ws, info):
        ws.insert_rows(21)
        ws["B21"] = "18-19"                          # 日间末尾多出一个时段（属于夜间的写法），值随便写
        for j in range(3):
            ws.cell(21, 3 + j, 10 + j)
    ex = run(flow_book(days=3, edit=extra_hour, uncached=True), FLOW)
    (p,) = [p for p in problems(ex, "label_unexpected") if p.fix_args["segment"] == "日间"]
    assert p.fix_args == {"segment": "日间", "role": "dimension", "labels": [{"raw": "18-19", "cell": f"{FS}!B21"}]}


def test_column_extra_fix_args_and_list_title_not_found_has_no_fix():
    def extra(ws):
        ws["F3"], ws["F4"] = "备注", "说明文字"
    (p,) = problems(run(sales_book(edit=extra), LIST), "column_extra")
    assert p.fix == "ignore_cells" and p.fix_args == {
        "block": "列表1", "how": "columns", "headers": [{"raw": "备注", "cell": f"{LS}!F3"}]}
    # 列表的 after_title 找不到：rename_title 是分段标题的修复，列表没有能写的参数，不给按钮（交给框选）
    (p,) = problems(run(sales_book(), list_recipe(after_title="不存在的标题")), "title_not_found")
    assert p.fix is None and p.fix_args is None


def test_outside_number_fix_args_carry_the_row_anchor():
    """⑥ outside：一行一条；锚点取这一行区域外的文字格，优先不含数字的；没有文字格时锚点为 None。"""
    def edit(ws, info):
        ws["B33"], ws["G33"] = "补录说明", 12          # 同一行有文字
        ws["B34"], ws["G34"], ws["H34"] = "注：第2期", 13, "附注"   # 两格文字：取不含数字的那格
        ws["G35"] = 14                                  # 这一行没有文字
    ex = run(flow_book(days=3, edit=edit), FLOW)
    got = {p.fix_args["rows"][0]["row"]: p.fix_args for p in problems(ex, "outside_number")}
    assert got[33] == {"sheet": FS, "sheet_id": "s1", "how": "outside",
                       "rows": [{"row": 33, "anchor": "补录说明", "anchor_cell": f"{FS}!B33", "cells": [f"{FS}!G33"]}]}
    assert got[34]["rows"][0]["anchor"] == "附注" and got[34]["rows"][0]["anchor_cell"] == f"{FS}!H34"
    assert got[35]["rows"][0]["anchor"] is None and got[35]["rows"][0]["anchor_cell"] is None
    assert all(p.fix == "ignore_cells" for p in problems(ex, "outside_number"))


def test_value_not_number_collects_placeholder_texts():
    """⑦：texts 是 canon 后、不超过 8 个字、不像数、不含数字的写法（最多 8 种，按出现顺序），其余计入 other（格数）。"""
    def edit(ws, info):
        ws["C11"], ws["D11"], ws["E11"] = "—", "—", "无"
        ws["C12"] = "约5000"                            # 含数字：不收
        ws["D12"] = "这一格是很长的一段说明文字"          # 超过 8 个字：不收
    (p,) = problems(run(flow_book(days=3, edit=edit), FLOW), "value_not_number")
    assert p.fix == "declare_placeholder"
    # texts 是 canon 后的写法（「—」canon 成「-」）：静态校验和执行器都按 canon 比占位符，写进配方照样认得出「—」
    assert p.fix_args == {"block": "交叉表", "texts": [canon("—"), "无"], "other": 2}
    words = list("甲乙丙丁戊己庚辛壬癸")                # 十种不同的单字写法
    def ten(ws, info):
        for i, w in enumerate(words):
            ws.cell(10 + i, 3, w)
    (p,) = problems(run(flow_book(days=3, edit=ten), FLOW), "value_not_number")
    assert p.fix_args["texts"] == words[:8] and p.fix_args["other"] == 2


def test_value_not_number_in_a_list_names_the_list_block():
    data = copy.deepcopy(LIST)
    data["sheets"][0]["blocks"][0]["columns"][3]["type"] = "INTEGER"
    (p,) = problems(run(sales_book(edit=lambda ws: ws.cell(4, 4, "暂缺")), data), "value_not_number")
    assert p.fix_args == {"block": "列表1", "texts": ["暂缺"], "other": 0}


def test_hidden_rows_and_cols_fix_args():
    def edit(ws, info):
        ws.row_dimensions[23].hidden = True
        ws.column_dimensions["D"].hidden = True
    ex = run(flow_book(days=3, edit=edit), FLOW)
    (rows,) = problems(ex, "hidden_rows")
    (cols,) = problems(ex, "hidden_cols")
    assert rows.fix == cols.fix == "declare_hidden"
    assert rows.fix_args == {"sheet": FS, "sheet_id": "s1", "axis": "rows", "rows": [23], "autofilter": False}
    assert cols.fix_args == {"sheet": FS, "sheet_id": "s1", "axis": "cols", "cols": ["D"], "autofilter": False}
    # c06：自动筛选隐藏的行，autofilter 为真（界面据此提示「可能是筛选隐藏的行」）
    data = {"recipe_format": "agentlab-recipe/2", "sheets": [{"id": "m", "match": {"name": "明细"}, "blocks": [{
        "id": "明细表", "layout": "list", "table": "明细",
        "columns": [{"header": h, "name": h, "type": t} for h, t in
                    (("地区", "TEXT"), ("产品", "TEXT"), ("成本", "INTEGER"), ("金额", "INTEGER"))]}]}],
        "tables": [{"name": "明细", "grain": ["产品"]}]}
    (p,) = problems(run(lab.c06(), data), "hidden_rows")
    assert p.fix_args["autofilter"] is True and p.fix_args["rows"] == [3, 4, 5, 7, 9, 11]


def test_sheet_missing_candidates_are_unmatched_visible_sheets():
    def edit(ws, info):
        ws.parent.create_sheet("9月客流")["A1"] = "另一张表"
    data = recipe_dict()
    data["sheets"][0]["match"] = {"name": "客流统计", "fallback": "only_visible_sheet"}
    (p,) = problems(run(flow_book(days=3, edit=edit), data), "sheet_missing")
    assert p.fix == "rename_sheet"
    assert p.fix_args == {"sheet_id": "s1", "name": "客流统计", "candidates": [FS, "9月客流"]}


def test_no_fix_for_single_cells_rows_after_stop_and_overflow(monkeypatch):
    ex = run(flow_book(days=3, edit=_set(G11=5)), FLOW)
    (p,) = problems(ex, "cell_unclaimed")
    assert p.fix is None and p.fix_args is None
    def after(ws):
        ws["A11"], ws["B11"] = "名单甲", "名单乙"
    (p,) = problems(run(sales_book(total=False, edit=after), list_recipe(rows={})), "rows_after_stop")
    assert p.fix is None and p.fix_args is None
    # 超过逐条列出的上限、合成「另有 N 处」的那一条：没有坐标和参数，不给按钮
    monkeypatch.setattr(recipe_engine, "PER_CODE_MAX", 1)
    ex = run(flow_book(days=3, edit=_set(G33=1, G34=2)), FLOW)
    first, rest = problems(ex, "outside_number")
    assert first.fix == "ignore_cells" and first.fix_args is not None
    assert "另有 1 处" in rest.message and rest.fix is None and rest.fix_args is None


def test_row_unclaimed_offers_the_ignore_button_only_for_a_text_label():
    """row_unclaimed 的修复只有「按行标签忽略」（3.1）。标签是文字时给按钮，提示与按钮一致、不再让人把标签加进
    分段（那条路认领不到隔了空行的这一行）；标签是数或像数的文字时没有能写进配方的锚点：不给按钮（fix 与
    fix_args 都为空），提示也不提按标签忽略（评审意见：原来给了 raw 为 null 的按钮，点了写不进 IgnoreRow）。"""
    (p,) = problems(run(flow_book(days=3, edit=_set(B32="补录（人次）", C32=5)), FLOW), "row_unclaimed")
    assert p.fix == "ignore_cells" and p.fix_args["labels"] == [{"raw": "补录（人次）", "cell": f"{FS}!B32"}]
    assert "按行标签忽略" in p.message and "添加到某个分段" not in p.message
    for label in (N, f"{N:,}"):
        (p,) = problems(run(flow_book(days=3, edit=_set(B32=label, C32=5)), FLOW), "row_unclaimed")
        assert p.fix is None and p.fix_args is None and p.cells[0] == f"{FS}!B32"
        assert "按行标签忽略" not in p.message and "无法按标签忽略" in p.message


def test_fix_and_fix_args_come_together():
    """有 fix 就有 fix_args，反之亦然：界面和 propose_fixes 只靠 fix_args 构造补丁。"""
    cases = [drift(c) for c in ("D04", "D09", "D13", "D16", "D18", "D21")]
    cases.append(run(flow_book(days=3, edit=_set(G11=5, B33="说明", G33=6, C12="—")), FLOW))
    for ex in cases:
        for p in ex.problems:
            assert (p.fix is None) == (p.fix_args is None), (p.code, p.fix, p.fix_args)


# ==========================================================================
# fix_args 不含数据格的值（守卫点：特征数 987654 放在数据格里）
# ==========================================================================


def _hide_row(ws, info):
    ws["C23"] = N
    ws.row_dimensions[23].hidden = True


def _hide_col(ws, info):
    ws["D11"] = N
    ws.column_dimensions["D"].hidden = True


def _new_measure(ws, info):
    ws.unmerge_cells(f"B8:{info['last']}8")
    ws["B8"] = "分区丙（人次）"
    for j in range(3):
        ws.cell(8, 3 + j, N + j)


def _subtotal(ws, info):
    ws.insert_rows(21)
    ws["B21"] = "7-18 时合计"
    for j in range(3):
        ws.cell(21, 3 + j, N + j)


def _crosstab_cases() -> dict[str, tuple[Any, dict[str, Any]]]:
    missing_sheet = recipe_dict()
    missing_sheet["sheets"][0]["match"] = {"name": "别的表", "fallback": "none"}
    return {
        "label_missing": (flow_book(days=3, day_hours=tuple(range(8, 18)), edit=_set(C10=N)), FLOW),
        "label_unexpected": (flow_book(days=3, edit=_new_measure), FLOW),
        "title_not_found": (flow_book(days=3, edit=_set(B9="白天时段客流（人次）", C10=N)), FLOW),
        "label_unparsed": (flow_book(days=3, edit=_subtotal, uncached=True), FLOW),
        "row_unclaimed": (flow_book(days=3, edit=_set(B32="补录（人次）", C32=N, D32=f"{N:,}")), FLOW),
        "axis_extra_cells": (flow_book(days=3, edit=_set(F4="合计", F5=N, G4=N + 1)), FLOW),
        "outside_number": (flow_book(days=3, edit=_set(B33="补录说明", G33=N, B34=str(N), G34=N + 1)), FLOW),
        "value_not_number": (flow_book(days=3, edit=_set(C11=f"约{N}", D11="—", E11=f"{N}万元整")), FLOW),
        "hidden_rows": (flow_book(days=3, edit=_hide_row), FLOW),
        "hidden_cols": (flow_book(days=3, edit=_hide_col), FLOW),
        "sheet_missing": (flow_book(days=3, edit=_set(C11=N)), missing_sheet),
    }


def _column_extra():
    def edit(ws):
        ws["F3"], ws["F4"], ws["G3"], ws["G4"] = "备注", N, N + 1, N + 2
    return sales_book(edit=edit), LIST


@pytest.mark.parametrize("code", sorted(recipe_engine.PROBLEM_FIX))
def test_fix_args_never_carry_cell_values(code):
    book, recipe = _column_extra() if code == "column_extra" else _crosstab_cases()[code]
    ex = run(book, recipe)
    hits = [p for p in ex.problems if p.code == code and p.fix_args is not None]
    assert hits, f"{code}：{[(p.code, p.fix, p.message) for p in ex.problems]}"
    for p in ex.problems:
        text = json.dumps(p.fix_args, ensure_ascii=False)
        for form in (str(N), str(N + 1), str(N + 2), f"{N:,}"):
            assert form not in text, (p.code, text)
    # 不是文字或像一个数的表头、标签、锚点：raw 为 None（界面据此不出提议，提示改文件）
    if code == "axis_extra_cells":
        assert [h["raw"] for h in hits[0].fix_args["headers"]] == ["合计", None]
    if code == "column_extra":
        assert [h["raw"] for h in hits[0].fix_args["headers"]] == ["备注", None]
    if code == "row_unclaimed":
        assert hits[0].fix_args["labels"][0]["raw"] == "补录（人次）"
    if code == "value_not_number":
        assert hits[0].fix_args["texts"] == [canon("—")] and hits[0].fix_args["other"] == 2


# ==========================================================================
# ignore_rows（交叉表）
# ==========================================================================


def test_ignore_rows_skips_the_matching_unclaimed_row(tmp_path):
    """D21 加 ignore_rows「补录（人次）」：不报 row_unclaimed，标签格和日期列上的格去向 ignored，记进排除的行；
    三张表与 D01 逐表相同。"""
    db = str(tmp_path / "d21.db")
    ex = drift("D21", flow_with(ignore_rows=[rule("label", "补录（人次）")]), db)
    assert ex.ok and not ex.problems
    assert ex.ledger[0].roles["ignored"] == 3 and ex.ledger[0].unclaimed == 0
    assert excluded(ex) == [{"sheet": FS, "reason": "ignored_rows", "rows": [[32, 32]], "cells": 3,
                             "anchor": "补录（人次）", "block": "交叉表"}]
    assert ex.table_hashes == drift("D01", db=str(tmp_path / "d01.db")).table_hashes
    assert ("ignored", "B32:AF32", "交叉表") in [(m.role, m.ref, m.block) for m in ex.regions]


def test_ignore_rows_only_ignores_matching_rows():
    """守卫点「ignore_rows 只忽略匹配的行」：标签不同的行照样报 row_unclaimed；按 match_key 比，全角括号、
    空白不同也算命中；被分段认领的行不受影响（规则只管没有分段认领的行）。"""
    def edit(ws, info):
        ws["B32"], ws["C32"] = "补录 (人次)", 5          # 写法不同，match_key 相同
        ws["B34"], ws["C34"] = "补录乙", 6
    data = flow_with(ignore_rows=[rule("label", "补录（人次）"), rule("label", "8-9")])
    ex = run(flow_book(days=3, edit=edit), data)
    (p,) = problems(ex, "row_unclaimed")
    assert p.cells[0] == f"{FS}!B34"
    assert [x["rows"] for x in excluded(ex)] == [[[32, 32]]]
    assert ex.ledger[0].roles["ignored"] == 2
    assert ex.expected_rows["时段客流"] == 17 * 3   # 8-9 是日间的期望标签：照常导入，没有被忽略


def test_ignore_rows_leaves_the_rest_of_the_row_to_outside_accounting():
    """规则只忽略标签格和日期列上的格：同一行标签左边的数字照常算区域外数字。"""
    ex = run(flow_book(days=3, edit=_set(B32="补录（人次）", C32=5, A32=7)), flow_with(
        ignore_rows=[rule("label", "补录（人次）")]))
    (p,) = problems(ex, "outside_number")
    assert p.cells == [f"{FS}!A32"] and excluded(ex)[0]["cells"] == 2


# ==========================================================================
# ignore_columns（交叉表、列表）
# ==========================================================================


def test_crosstab_ignore_columns_right_of_the_axis(tmp_path):
    """D16 加 ignore_columns「合计」：不报 axis_extra_cells，从轴行到数据区底部的格去向 ignored_column，表头记进
    ignored_columns；三张表与 D01 相同。列不是行，不进排除的行。"""
    ex = drift("D16", flow_with(ignore_columns=[rule("header", "合计")]), str(tmp_path / "d16.db"))
    assert ex.ok and not ex.problems
    assert ex.ignored_columns == {"交叉表": ["合计"]}
    assert ex.ledger[0].roles["ignored_column"] == 24 and ex.rows_excluded == []
    assert ex.table_hashes == drift("D01", db=str(tmp_path / "d01.db")).table_hashes


def test_crosstab_ignore_columns_only_right_of_the_axis_and_within_the_block():
    """守卫点「ignore_columns 只在轴行右侧生效」：只认最后一个日期右侧、表头命中的列；没命中的列照样报
    axis_extra_cells；数据区之下这一列的数字不归这条规则管，照常报区域外数字；轴行左边的同名文字不受影响。"""
    def edit(ws, info):
        ws["F4"], ws["F5"], ws["F30"] = "合计", 1, 2     # 命中：轴行到数据区底部（第 30 行）
        ws["G4"], ws["G5"] = "备注", "说明"              # 没命中
        ws["B33"], ws["F33"] = "附注", 3                  # 数据区之下（表下的文字行不算数据区）
        ws["A4"] = "合计"                                # 轴行左边：不是多出的列
    ex = run(flow_book(days=3, edit=edit), flow_with(ignore_columns=[rule("header", "合计")]))
    (extra,) = problems(ex, "axis_extra_cells")
    assert extra.cells == [f"{FS}!G4"] and extra.fix_args["headers"] == [{"raw": "备注", "cell": f"{FS}!G4"}]
    (out,) = problems(ex, "outside_number")
    assert out.cells == [f"{FS}!F33"]
    assert ex.ignored_columns == {"交叉表": ["合计"]} and ex.ledger[0].roles["ignored_column"] == 3
    assert (f"{FS}!A4", "text") in [(o.cell, o.kind) for o in ex.outside_text]


def _total_column(ws: Any, rows: range = range(5, 31)) -> int:
    """F4「合计」，交叉表每个有数的行在 F 列写一个合计（手写假数）。返回 F 列这一段的非空格数（含表头）。"""
    ws["F4"] = "合计"
    n = 1
    for r in rows:
        if ws.cell(r, 3).value is not None:
            ws.cell(r, 6, 11)
            n += 1
    return n


def _list_below(ws: Any) -> None:
    """交叉表下方隔几行放一张列表，列范围 B:F，盖到被忽略的 F 列（假名、手写假数）。"""
    for j, h in enumerate(("地区", "甲", "乙", "丙", "丁")):
        ws.cell(34, 2 + j, h)
    for i, name in enumerate(("分区子", "分区丑")):
        ws.cell(35 + i, 2, name)
        for j in range(4):
            ws.cell(35 + i, 3 + j, 100 + i * 10 + j)


def _with_list_below(data: dict[str, Any]) -> dict[str, Any]:
    data["sheets"][0]["blocks"].append({
        "id": "列表", "layout": "list", "table": "地区表",
        "columns": [{"header": h, "name": h, "type": t} for h, t in
                    (("地区", "TEXT"), ("甲", "INTEGER"), ("乙", "INTEGER"), ("丙", "INTEGER"), ("丁", "INTEGER"))]})
    data["tables"].append({"name": "地区表", "grain": ["地区"]})
    return data


def test_crosstab_ignore_columns_stop_at_the_blocks_own_rows_above_a_list(tmp_path):
    """评审意见：数据区底部只按本块自己的行算。表下另有一张列表、列范围盖到被忽略的列时，原来按整张表的数据行
    取底部、又赶在列表之前认领，被忽略的列一直认领到列表的最后一行，列表再认领就报 segment_overlap（结构类、
    没有修复按钮）。现在按表头忽略的列只到交叉表最后一个数据行，列表照常导入，F 列上列表的格归列表。"""
    got: dict[str, int] = {}

    def edit(ws, info):
        got["n"] = _total_column(ws)
        _list_below(ws)
    book = flow_book(days=3, edit=edit)
    # 期 2 配方：只报多出的列（它的修复按钮正是「按表头忽略」）
    assert codes(run(book, _with_list_below(copy.deepcopy(FLOW)))) == {"axis_extra_cells"}
    db = str(tmp_path / "list.db")
    ex = run(book, _with_list_below(flow_with(ignore_columns=[rule("header", "合计")])), db)
    assert ex.ok and not ex.problems, [(p.code, p.message) for p in ex.problems]
    assert ex.ledger[0].roles["ignored_column"] == got["n"] and ex.ledger[0].unclaimed == 0
    assert ex.ignored_columns == {"交叉表": ["合计"]}
    assert ex.expected_rows["地区表"] == 2
    marks = [(m.role, m.ref, m.block) for m in ex.regions]
    assert ("ignored_column", "F22:F30", "交叉表") in marks         # 到交叉表最后一个数据行（第 30 行）为止
    assert ("col_header", "B34:F34", "列表") in marks and ("value", "B35:F36", "列表") in marks


def test_crosstab_ignore_columns_cover_rows_ignored_by_label():
    """按标签忽略的行也是本块的行：D16 + D21 同时出现、两条规则都加上时，被忽略那一行在合计列上的格也归
    ignored_column，不报区域外数字；只加按表头忽略时，那一行照常报 row_unclaimed（一条，不另报区域外数字）。"""
    got: dict[str, int] = {}

    def edit(ws, info):
        got["n"] = _total_column(ws)
        ws["B32"], ws["C32"], ws["D32"], ws["E32"], ws["F32"] = "补录（人次）", 5, 6, 7, 18
    book = flow_book(days=3, edit=edit)
    ex = run(book, flow_with(ignore_columns=[rule("header", "合计")], ignore_rows=[rule("label", "补录（人次）")]))
    assert ex.ok and not ex.problems, [(p.code, p.message) for p in ex.problems]
    assert ex.ledger[0].roles["ignored_column"] == got["n"] + 1 and ex.ledger[0].roles["ignored"] == 4
    assert excluded(ex) == [{"sheet": FS, "reason": "ignored_rows", "rows": [[32, 32]], "cells": 4,
                             "anchor": "补录（人次）", "block": "交叉表"}]
    ex = run(book, flow_with(ignore_columns=[rule("header", "合计")]))
    assert codes(ex) == {"row_unclaimed"}


def test_list_ignore_columns_only_for_matching_headers():
    """列表：命中的列按 extra_columns=ignore 的方式忽略；其余多出的列照 extra_columns（默认拒收）处理。"""
    def extra(ws):
        ws["F3"], ws["F4"] = "备注", "说明文字"
        ws["G3"], ws["G4"] = "核对人", "某甲"
    data = list_recipe(ignore_columns=[rule("header", "备注")])
    ex = run(sales_book(edit=extra), data)
    (p,) = problems(ex, "column_extra")
    assert p.cells == [f"{LS}!G3"] and p.fix_args["headers"] == [{"raw": "核对人", "cell": f"{LS}!G3"}]
    assert ex.ignored_columns == {"列表1": ["备注"]}
    ex = run(sales_book(edit=extra), list_recipe(ignore_columns=[rule("header", "备注")], extra_columns="ignore"))
    assert ex.ok and ex.ignored_columns == {"列表1": ["备注", "核对人"]}
    ex = run(sales_book(edit=lambda ws: (ws.cell(3, 6, "备注"), ws.cell(4, 6, "说明"))), data)
    assert ex.ok and ex.ledger[0].roles["ignored_column"] == 2 and ex.rows_excluded == []


# ==========================================================================
# ignore_outside
# ==========================================================================


def test_ignore_outside_needs_an_anchor_on_the_same_row():
    """守卫点「ignore_outside 要求同一行有锚点」：锚点所在行区域外的数字（含公式格）去向 ignored，锚点本身照常
    是区域外文字；别的行的数字照常报 outside_number。"""
    def edit(ws, info):
        ws["B33"], ws["G33"], ws["H33"] = "补录说明", 5, "=G33*2"
        info["cache"]["H33"] = 10
        ws["G34"] = 6                                    # 下一行没有锚点
        ws["B35"], ws["G35"] = "另一条说明", 7           # 有文字，但不是锚点
    data = recipe_dict()
    data["sheets"][0]["ignore_outside"] = [rule("anchor", "补录说明")]
    ex = run(flow_book(days=3, edit=edit), data)
    assert sorted(p.cells[0] for p in problems(ex, "outside_number")) == [f"{FS}!G34", f"{FS}!G35"]
    assert ex.ledger[0].roles["ignored"] == 2
    assert (f"{FS}!B33", "补录说明") in [(o.cell, o.text) for o in ex.outside_text]
    assert excluded(ex) == [{"sheet": FS, "reason": "ignored_outside", "rows": [[33, 33]], "cells": 2,
                             "anchor": "补录说明", "block": None}]
    marks = [(m.role, m.ref, m.block) for m in ex.regions]
    assert ("ignored", "G33", None) in marks and ("ignored", "H33", None) in marks


def test_ignore_outside_matches_text_numbers_too():
    """像数的文本（「1,234」）在区域外也是数字格，同样按锚点忽略。"""
    data = recipe_dict()
    data["sheets"][0]["ignore_outside"] = [rule("anchor", "补录说明")]
    ex = run(flow_book(days=3, edit=_set(B33="补录 说明", G33="1,234")), data)
    assert ex.ok and not ex.problems and ex.ledger[0].roles["ignored"] == 1


# ==========================================================================
# 区域标记的块 id
# ==========================================================================


def test_region_marks_carry_the_block_id():
    ex = run(flow_book(days=3), FLOW)
    blocks = {(m.role, m.block) for m in ex.regions}
    assert blocks == {("context", None), ("outside_text", None), ("col_header", "交叉表"), ("row_label", "交叉表"),
                      ("value", "交叉表"), ("section_title", "交叉表"), ("derived_label", "交叉表"),
                      ("derived_value", "交叉表")}
    assert all("block" in asdict(m) for m in ex.regions)


def test_regions_of_different_blocks_are_not_merged():
    """并排的两张列表（中间隔一空列）：期 2 的值区合成一个矩形，期 3 按块分开。"""
    raw, fn = lab.c08("side")
    data = {"recipe_format": "agentlab-recipe/2", "sheets": [{"id": "s", "match": {"name": "并排"}, "blocks": [
        {"id": "左表", "layout": "list", "table": "地区金额",
         "columns": [{"header": "地区", "name": "地区", "type": "TEXT"}, {"header": "金额", "name": "金额", "type": "INTEGER"}]},
        {"id": "右表", "layout": "list", "table": "部门费用",
         "columns": [{"header": "部门", "name": "部门", "type": "TEXT"}, {"header": "费用", "name": "费用", "type": "INTEGER"}]},
    ]}], "tables": [{"name": "地区金额", "grain": ["地区"]}, {"name": "部门费用", "grain": ["部门"]}]}
    ex = run((raw, fn), data)
    assert ex.ok
    assert [(m.role, m.ref, m.block) for m in ex.regions] == [
        ("col_header", "A1:B1", "左表"), ("col_header", "D1:E1", "右表"),
        ("value", "A2:B3", "左表"), ("value", "D2:E3", "右表")]


def test_stream_and_grid_agree_on_blocks_and_excluded_rows(monkeypatch):
    def gaps(ws):
        for r in (6, 7, 10):
            for c in range(1, 6):
                ws.cell(r, c).value = None
    data = list_recipe(rows={"blank_rows": "skip"})
    book = sales_book(n=12, total=False, edit=gaps)
    grid = run(book, data)
    monkeypatch.setattr(xlsx_cells, "GRID_MAX_CELLS", 30)
    stream = run(book, data)
    assert grid.ok and stream.ok
    assert stream.regions == grid.regions and stream.rows_excluded == grid.rows_excluded
    assert {m.block for m in grid.regions if m.role in ("col_header", "value")} == {"列表1"}
    assert excluded(grid) == [{"sheet": LS, "reason": "blank_skipped", "rows": [[6, 7], [10, 10]], "cells": 0,
                               "anchor": None, "block": "列表1"}]
    assert grid.blank_rows_skipped == 3


# ==========================================================================
# 排除的行：六种原因（遗留项 4、L4）
# ==========================================================================


def test_excluded_hidden_rows_crosstab_and_list():
    exclude = recipe_dict(sheets__0__hidden={"rows": "exclude", "cols": "reject_if_any"})
    def hide(ws, info):
        ws.row_dimensions[12].hidden = True
        ws.row_dimensions[13].hidden = True
    ex = run(flow_book(days=3, edit=hide), exclude)
    assert ex.ok
    assert excluded(ex) == [{"sheet": FS, "reason": "hidden_excluded", "rows": [[12, 13]], "cells": 8,
                             "anchor": None, "block": None}]
    data = {"recipe_format": "agentlab-recipe/2", "sheets": [{
        "id": "m", "match": {"name": "明细"}, "hidden": {"rows": "exclude", "cols": "include"}, "blocks": [{
            "id": "明细表", "layout": "list", "table": "明细",
            "columns": [{"header": h, "name": h, "type": t} for h, t in
                        (("地区", "TEXT"), ("产品", "TEXT"), ("成本", "INTEGER"), ("金额", "INTEGER"))]}]}],
        "tables": [{"name": "明细", "grain": ["产品"]}]}
    ex = run(lab.c06(), data)
    assert ex.ok and ex.expected_rows == {"明细": 4}
    assert excluded(ex) == [{"sheet": "明细", "reason": "hidden_excluded", "rows": [[3, 5], [7, 7], [9, 9], [11, 11]],
                             "cells": 24, "anchor": None, "block": None}]


def test_excluded_blank_rows_in_c08_gap():
    raw, fn = lab.c08("gap")
    data = {"recipe_format": "agentlab-recipe/2", "sheets": [{"id": "s", "match": {"name": "明细"}, "blocks": [{
        "id": "明细表", "layout": "list", "table": "明细", "rows": {"blank_rows": "skip"},
        "columns": [{"header": "地区", "name": "地区", "type": "TEXT"}, {"header": "产品", "name": "产品", "type": "TEXT"},
                    {"header": "金额", "name": "金额", "type": "INTEGER"}]}]}],
        "tables": [{"name": "明细", "grain": ["地区", "产品"]}]}
    ex = run((raw, fn), data)
    assert ex.ok and ex.blank_rows_skipped == 1
    assert excluded(ex) == [{"sheet": "明细", "reason": "blank_skipped", "rows": [[4, 4]], "cells": 0,
                             "anchor": None, "block": "明细表"}]


def test_excluded_rows_after_stop():
    def after(ws):
        ws["A11"], ws["B11"] = "名单甲", "名单乙"
        ws["A12"], ws["B12"], ws["C12"] = "名单丙", "名单丁", "名单戊"
    ex = run(sales_book(total=False, edit=after), list_recipe(rows={}))
    assert "rows_after_stop" in codes(ex)
    assert excluded(ex) == [{"sheet": LS, "reason": "after_stop", "rows": [[11, 12]], "cells": 5,
                             "anchor": None, "block": "列表1"}]


def test_excluded_totals_that_are_checked_but_not_kept():
    data = copy.deepcopy(LIST)
    data["sheets"][0]["blocks"][0]["rows"]["total_row"].pop("keep_as")
    data["tables"].pop()
    ex = run(sales_book(), data)
    assert ex.ok and [d.base_value for d in ex.derived] == ["销量", "金额"]
    assert excluded(ex) == [{"sheet": LS, "reason": "total_not_kept", "rows": [[9, 9]], "cells": 3,
                             "anchor": None, "block": "列表1"}]
    cross = recipe_dict()
    cross["sheets"][0]["blocks"][0]["segments"][3]["keep_as"] = None
    cross["tables"].pop()
    ex = run(flow_book(days=3), cross)
    assert ex.ok and len(ex.derived) == 9
    assert excluded(ex) == [{"sheet": FS, "reason": "total_not_kept", "rows": [[28, 30]], "cells": 12,
                             "anchor": None, "block": "交叉表"}]


def test_kept_totals_are_not_excluded():
    assert run(sales_book(), LIST).rows_excluded == [] and run(flow_book(days=3), FLOW).rows_excluded == []


def test_excluded_rows_are_listed_in_a_fixed_order():
    """同一工作表里按原因的固定顺序列出；asdict 之后就是回执里的形状。"""
    exclude = recipe_dict(sheets__0__hidden={"rows": "exclude", "cols": "reject_if_any"})
    exclude["sheets"][0]["blocks"][0]["ignore_rows"] = [rule("label", "补录（人次）")]
    exclude["sheets"][0]["ignore_outside"] = [rule("anchor", "补录说明")]
    exclude["sheets"][0]["blocks"][0]["segments"][3]["keep_as"] = None
    exclude["tables"].pop()
    def edit(ws, info):
        ws.row_dimensions[12].hidden = True
        ws["B32"], ws["C32"] = "补录（人次）", 5
        ws["B34"], ws["G34"] = "补录说明", 6
    ex = run(flow_book(days=3, edit=edit), exclude)
    assert ex.ok
    assert [(x["reason"], x["rows"]) for x in excluded(ex)] == [
        ("hidden_excluded", [[12, 12]]), ("ignored_rows", [[32, 32]]), ("ignored_outside", [[34, 34]]),
        ("total_not_kept", [[28, 30]])]


def test_excluded_rows_dropped_on_sheets_whose_blocks_failed():
    """块级定位失败的工作表：区域都不知道在哪，区域外按锚点忽略的格只是噪声，不进排除的行。"""
    data = recipe_dict()
    data["sheets"][0]["ignore_outside"] = [rule("anchor", "补录说明")]
    def edit(ws, info):
        for c in range(3, 6):
            ws.cell(4, c).value = None                  # 日期表头没了：axis_not_found
        ws["B33"], ws["G33"] = "补录说明", 5
    ex = run(flow_book(days=3, edit=edit), data)
    assert "axis_not_found" in codes(ex) and ex.rows_excluded == []
