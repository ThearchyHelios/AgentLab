"""model_message 契约测试（P2-SPEC 6.2、第 10 节 WP-2）。

AI 修订时回灌给模型的是 Problem.model_message，绝不退回 message（message 可以带格子里的值）。这里为执行器
能产出的**每个** problem code 各造一个触发夹具，触发格里放特征数（987654）或特征文字，断言：
- 这个 code 真的出现了；
- 它的 model_message 非空；
- model_message 里没有触发格的值。
测试里维护一份全量 code 表，和 recipe_engine.PROBLEM_CODES 比对：执行器新增 code 不补用例，这个测试就失败。
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.data import recipe_engine, xlsx_cells, xlsx_scan
from app.data.recipe_types import Extraction, Recipe
from tests.test_recipe_engine_crosstab import FLOW, flow_book, recipe_dict
from tests.test_recipe_engine_list import LIST, list_recipe, sales_book

N = 987654


def _flow(edit: Callable[[Any, dict[str, Any]], None] | None = None, **kw: Any) -> tuple[bytes, str]:
    return flow_book(days=3, edit=edit, **kw)


def _set(**cells: Any) -> Callable[[Any, dict[str, Any]], None]:
    def edit(ws: Any, info: dict[str, Any]) -> None:
        for ref, value in cells.items():
            ws[ref] = value
    return edit


def _run(book: tuple[bytes, str], recipe: dict[str, Any], *, scan_fix: Callable[[Any], None] | None = None,
         **kw: Any) -> Extraction:
    raw, filename = book
    scan = xlsx_scan.scan(raw)
    if scan_fix is not None:
        scan_fix(scan)
    return recipe_engine.execute(Recipe.model_validate(recipe), raw, filename, scan, None, **kw)


# --------------------------------------------------------------------------
# 每个 code 的触发夹具：返回 (Extraction, 触发格的值)
# --------------------------------------------------------------------------

Case = Callable[[pytest.MonkeyPatch], tuple[Extraction, list[Any]]]


def c_table_shape_conflict(mp):
    data = recipe_dict()
    data["sheets"][0]["blocks"][0]["segments"][2]["table"] = "日客流"
    return _run(_flow(_set(C11=N)), data), [N]


def c_measures_keys_mismatch(mp):
    data = recipe_dict()
    seg = data["sheets"][0]["blocks"][0]["segments"][0]
    seg["measures"] = {"全日客流(人次)": "全日客流", "分区甲（人次）": "分区甲", "分区乙（人次）": "分区乙"}
    return _run(_flow(_set(C5=N)), data), [N]


def c_sheet_missing(mp):
    data = recipe_dict()
    data["sheets"][0]["match"] = {"name": "另一张表", "fallback": "none"}
    return _run(_flow(_set(C5=N)), data), [N]


def c_sheet_unexpected(mp):
    def edit(ws, info):
        ws.parent.create_sheet("说明")["A1"] = N
    return _run(_flow(edit), recipe_dict(other_visible_sheets="reject")), [N]


def c_sheet_too_large_for_layout(mp):
    mp.setattr(xlsx_cells, "GRID_MAX_CELLS", 10)
    return _run(_flow(_set(C11=N)), FLOW), [N]


def c_merged_too_many(mp):
    def fix(scan):
        scan.sheets[0].merged_total = len(scan.sheets[0].merged) + 1
    return _run(_flow(_set(C11=N)), FLOW, scan_fix=fix), [N]


def c_sparse(mp):
    return _run(_flow(_set(Z100000=N)), FLOW), [N]


def c_pass_mismatch(mp):
    def fix(scan):
        a, b, n = scan.sheets[0].row_runs[0]
        scan.sheets[0].row_runs[0] = (a, b, n + 1)
    return _run(_flow(_set(C11=N)), FLOW, scan_fix=fix), [N]


def c_cell_unclaimed(mp):
    return _run(_flow(_set(G11=N)), FLOW), [N]


def c_outside_number(mp):
    return _run(_flow(_set(B1=N)), FLOW), [N]


def c_outside_digits(mp):
    text = f"注：{N}号闸机故障"
    return _run(_flow(_set(B32=text)), FLOW), [text, N]


def c_hidden_rows(mp):
    def edit(ws, info):
        ws["C23"] = N
        ws.row_dimensions[23].hidden = True
    return _run(_flow(edit), FLOW), [N]


def c_hidden_cols(mp):
    def edit(ws, info):
        ws["D11"] = N
        ws.column_dimensions["D"].hidden = True
    return _run(_flow(edit), FLOW), [N]


def c_period_missing(mp):
    text = f"统计时间：第{N}期"
    return _run(_flow(period=text, filename=f"客流{N}.xlsx"), FLOW), [text, N]


def c_axis_not_found(mp):
    return _run(_flow(_set(C4=N, D4=N + 1, E4=N + 2)), FLOW), [N, N + 1, N + 2]


def c_axis_ambiguous(mp):
    def edit(ws, info):
        for j in range(3):
            ws.cell(40, 3 + j).value = ws.cell(4, 3 + j).value
        ws["B40"] = N
    return _run(_flow(edit), FLOW), ["8月1日", "8月3日", N]


def c_axis_gap(mp):
    return _run(flow_book(days=5, edit=_set(E4=N)), FLOW), [N]


def c_axis_extra_cells(mp):
    text = f"合计{N}"
    def edit(ws, info):
        ws["F4"] = text
        ws["F5"] = N
    return _run(_flow(edit), FLOW), [text, N]


def c_axis_year_missing(mp):
    book = _flow(period="统计时间范围：2026年9月1日至2026年9月3日")
    return _run(book, FLOW), ["8月1日", "8月2日", "2026年9月1日"]


def c_axis_year_ambiguous(mp):
    book = flow_book(start=dt.date(2026, 1, 5), days=3, period="统计时间范围：2026年1月1日至2027年1月31日")
    return _run(book, FLOW), ["1月5日", "1月6日"]


def c_axis_duplicate(mp):
    return _run(flow_book(days=5, edit=_set(E4="8月2日")), FLOW), ["8月2日", "2026-08-02"]


def c_axis_not_contiguous(mp):
    return _run(flow_book(days=5, edit=_set(D4="8月3日", E4="8月2日")), FLOW), ["8月3日", "2026-08-03",
                                                                               "2026-08-02"]


def c_axis_coverage(mp):
    book = flow_book(days=5, period="统计时间范围：2026年8月1日至2026年8月6日")
    return _run(book, FLOW), ["8月1日", "8月5日", "2026-08-01", "2026-08-06"]


def c_row_without_label(mp):
    return _run(_flow(_set(D32=N)), FLOW), [N]


def c_row_unclaimed(mp):
    label = f"补录{N}"
    return _run(_flow(_set(B32=label, C32=N + 1)), FLOW), [label, N + 1]


def c_title_not_found(mp):
    title = f"日间{N}时段"
    return _run(_flow(_set(B9=title)), FLOW), [title]


def c_title_ambiguous(mp):
    title = "日间时段客流 （人次）"            # 与配方的标题 match_key 相同，原文不同
    def edit(ws, info):
        ws.unmerge_cells("B8:E8")
        ws["B8"] = title
    return _run(_flow(edit), FLOW), [title]


def c_segment_not_found(mp):
    data = recipe_dict()
    seg = data["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"] = ["甲（人次）", "乙（人次）"]
    seg["measures"] = {"甲（人次）": "甲", "乙（人次）": "乙"}
    data["tables"][0]["units"] = {}
    data["relations"] = []
    return _run(_flow(_set(B5=f"全日{N}")), data), [f"全日{N}"]


def c_segment_overlap(mp):
    data = recipe_dict()
    data["sheets"][0]["blocks"][0]["segments"].append({
        "id": "日间按标签", "role": "dimension", "table": "另表", "locate": {"by": "labels"},
        "labels": {"expect": ["8-9", "9-10"]}, "dim": {"name": "时段", "parser": "hour_range"}, "value": "客流"})
    data["tables"].append({"name": "另表", "grain": ["日期", "时段"]})
    return _run(_flow(_set(C11=N)), data), [N]


def c_label_missing(mp):
    return _run(flow_book(days=3, day_hours=tuple(range(8, 18)), edit=_set(C10=N)), FLOW), [N]


def c_label_unexpected(mp):
    label = f"分区{N}（人次）"
    def edit(ws, info):
        ws.unmerge_cells("B8:E8")
        ws["B8"] = label
        for j in range(3):
            ws.cell(8, 3 + j, N + 1)
    return _run(_flow(edit), FLOW), [label, N + 1]


def c_label_duplicate(mp):
    return _run(_flow(_set(B12="8－9")), FLOW), ["8－9"]


def c_label_unparsed(mp):
    label = "7-18 时合计"
    def edit(ws, info):
        ws.insert_rows(21)
        ws["B21"] = label
        for j in range(3):
            ws.cell(21, 3 + j, N)
    return _run(flow_book(days=3, edit=edit, uncached=True), FLOW), [label, N]


def c_const_missing(mp):
    data = recipe_dict()
    data["sheets"][0]["blocks"][0]["segments"][2]["const"] = {"时段类别": {"pick": "晚上"}}
    return _run(_flow(), data), ["夜间时段客流（人次）"]


def c_merged_in_values(mp):
    def edit(ws, info):
        ws["C12"] = N
        ws.merge_cells("C12:D12")
    return _run(_flow(edit), FLOW), [N]


def c_header_not_found(mp):
    def edit(ws):
        for c in range(1, 6):
            ws.cell(3, c).value = f"列{N}{c}"
    return _run(sales_book(edit=edit), LIST), [f"列{N}1", f"列{N}5"]


def c_header_ambiguous(mp):
    def edit(ws):
        for c, h in enumerate(("地区 ", "产品", "日期", "销量", "金额（万元）"), 1):
            ws.cell(20, c, h)
        ws["D21"] = N
    return _run(sales_book(edit=edit), LIST), ["地区 ", N]


def c_column_missing(mp):
    header = f"销售额{N}"
    return _run(sales_book(edit=lambda ws: ws.cell(3, 5, header)), LIST), [header]


def c_column_extra(mp):
    header = f"备注{N}"
    def edit(ws):
        ws["F3"] = header
        ws["F4"] = N
    return _run(sales_book(edit=edit), LIST), [header, N]


def c_rows_after_stop(mp):
    text = f"名单{N}"
    def edit(ws):
        ws["A11"], ws["B11"] = text, "名单乙"
    return _run(sales_book(total=False, edit=edit), list_recipe(rows={})), [text, "名单乙"]


def c_list_empty(mp):
    """AU-1：只有表头、没有数据行。触发格是表头之下本该有数据的地方，这里放不了值；断言表头文字之外的特征不外露。"""
    def edit(ws):
        for row in ws.iter_rows(min_row=4):
            for cell in row:
                cell.value = None
    return _run(sales_book(total=False, edit=edit), list_recipe(rows={})), [N]


def c_total_row_missing(mp):
    def edit(ws):
        ws["D4"] = N
    return _run(sales_book(total=False, edit=edit), LIST), [N]


def c_total_row_blank(mp):
    def edit(ws):
        ws["D4"] = N
        ws["D9"] = ws["E9"] = None                  # 第 9 行是合计行：标签还在，数字格全空
    return _run(sales_book(edit=edit), LIST), [N]


def c_value_not_integer(mp):
    return _run(_flow(_set(C11=9876.54)), FLOW), [9876.54]


def c_value_text_number(mp):
    return _run(_flow(_set(C11="987,654")), FLOW), ["987,654", N]


def c_value_blank(mp):
    return _run(_flow(_set(C11=None, D11=N)), FLOW), [N]


def c_value_error(mp):
    return _run(_flow(_set(C11="#DIV/0!")), FLOW), ["#DIV/0!"]


def c_value_not_number(mp):
    return _run(_flow(_set(C11=f"约{N}")), FLOW), [f"约{N}", N]


def c_value_not_date(mp):
    return _run(sales_book(edit=lambda ws: ws.cell(4, 3, f"{N}日")), LIST), [f"{N}日", N]


def c_value_formula(mp):
    def edit(ws, info):
        ws["C11"] = f"=C12+{N}"
        info["cache"]["C11"] = N + 1
    return _run(_flow(edit), FLOW), [N, N + 1]


def c_formula_uncached(mp):
    data = recipe_dict(sheets__0__blocks__0__values__formula="accept_cached")
    return _run(_flow(_set(C11=f"=C12+{N}")), data), [N]


def c_formula_full_calc(mp):
    data = recipe_dict(sheets__0__blocks__0__values__formula="accept_cached")
    def edit(ws, info):
        ws["C11"] = f"=C12+{N}"
        info["cache"]["C11"] = N + 1
    return _run(flow_book(days=3, edit=edit, keep_full_calc=True), data), [N, N + 1]


def c_pk_duplicate(mp):
    def edit(ws):
        ws["A4"] = ws["A5"] = f"地区{N}"
        ws["B5"] = ws["B4"].value
    return _run(sales_book(edit=edit), LIST), [f"地区{N}", "产品000"]


CASES: dict[str, Case] = {
    name[2:]: fn for name, fn in globals().items() if name.startswith("c_") and callable(fn)
}


def test_case_table_covers_every_problem_code():
    assert set(CASES) == set(recipe_engine.PROBLEM_CODES)
    assert set(recipe_engine.PROBLEM_FIX) <= set(recipe_engine.PROBLEM_CODES)


def _forms(value: Any) -> list[str]:
    if isinstance(value, float):
        return [repr(value), str(value)]
    return [str(value)]


@pytest.mark.parametrize("code", sorted(recipe_engine.PROBLEM_CODES))
def test_model_message_is_filled_and_has_no_cell_values(code, monkeypatch):
    ex, triggers = CASES[code](monkeypatch)
    hits = [p for p in ex.problems if p.code == code]
    assert hits, f"{code} 没有出现：{[(p.code, p.message) for p in ex.problems]}"
    for p in ex.problems:                       # 同一夹具里顺带出现的别的 code 也要守这条
        assert p.model_message.strip(), (p.code, p.message)
        assert p.category == recipe_engine.PROBLEM_CODES[p.code]
        assert len(p.cells) <= recipe_engine.CELLS_MAX
    for p in hits:
        for value in triggers:
            for form in _forms(value):
                assert form not in p.model_message, (code, form, p.model_message)
        assert p.message                         # 给人看的那句照样有


def test_message_may_carry_values_but_model_message_does_not():
    ex, _ = c_value_not_number(None)
    p = next(p for p in ex.problems if p.code == "value_not_number")
    assert f"约{N}" in p.message and f"约{N}" not in p.model_message
    assert "客流汇总!C11" in p.cells and "C11" in p.model_message


#: 下一步指到配方面板上真实存在的「字段」和「选项」（terms.ts：报错写界面上的中文标签）。check-copy 看不出这种
#: 跨包的不一致，这里把引用的标签逐个钉住
NEXT_STEPS = {
    "value_text_number": "在配方面板的「文本写的数字」中选择「千分位写法按数字保存」",
    "value_blank": "在配方面板的「空格」中选择「存为空值」",
    "value_formula": "在配方面板的「公式」中选择「按保存值导入」",
    "value_not_number": "在配方面板中添加占位符",
    "value_not_integer": "在配方面板中把「类型」改为「小数」",
    "value_not_date": "在配方面板中把这一列的「类型」改为「文字」",
    "sheet_unexpected": "在配方面板的「配方没有列出的其他工作表」中选择「记入回执，需确认」",
    "rows_after_stop": "在配方面板的「数据中间的空行」中选择「跳过继续」",
    "hidden_rows": "在配方面板的「隐藏行」中选择「照常导入」或「跳过」",
    "hidden_cols": "在配方面板的「隐藏列」中选择「照常导入」",
    "total_row_missing": "在配方面板的「合计行」中取消「有合计行」",
    "total_row_blank": "在配方面板的「合计行」中取消「有合计行」",
}


@pytest.mark.parametrize("code", sorted(NEXT_STEPS))
def test_next_steps_name_recipe_panel_labels(code, monkeypatch):
    ex, _ = CASES[code](monkeypatch)
    p = next(p for p in ex.problems if p.code == code)
    assert NEXT_STEPS[code] in p.message, p.message


def test_next_step_labels_exist_in_the_recipe_panel():
    """引用的每个标签都要在前端的配方面板文案里（RECIPE_TEXT / RECIPE_CHOICE_LABEL）。前端的配方面板合入之前跳过。"""
    terms = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "terms.ts"
    text = terms.read_text(encoding="utf-8") if terms.exists() else ""
    if "RECIPE_CHOICE_LABEL" not in text:
        pytest.skip("前端的配方面板文案还没有合入")
    labels = {x for step in NEXT_STEPS.values() for x in re.findall(r"「(.+?)」", step)} | {"主键"}
    missing = sorted(x for x in labels if f"'{x}" not in text)
    assert not missing, missing
