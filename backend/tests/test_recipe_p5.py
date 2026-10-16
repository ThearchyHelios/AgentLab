"""Excel 期 5（下一轮 03 细案 E）：列表按期累积、穿插的小计、表格对象起草、CSV 走配方。

不连接口的部分：执行器、核对、静态校验、累积资格、并集物化、起草。夹具全是合成的通用名字（门店一、分区甲、
订单 A001……），数字是手写的小数，手算得出。接口的端到端在 test_recipe_p5_e2e.py。
"""
from __future__ import annotations

import copy
import io
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

from app.data import csv_grid, raw_store, recipe_checks, recipe_engine, recipe_suggest, table_versions, xlsx_cells
from app.data import xlsx_scan
from app.data.recipe import validate_recipe
from app.data.recipe_accumulate import materialize_union
from app.data.recipe_types import (
    LIST_PERIOD_END, LIST_PERIOD_START, SUBTOTAL_KEY, Recipe, UnionPart, accumulate_blockers, derive_tables,
)


def _book(rows: list[tuple[Any, ...]], *, sheet: str = "明细", top: list[tuple[str, Any]] | None = None,
          start: int = 1) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for ref, value in top or []:
        ws[ref] = value
    for r, row in enumerate(rows, start=start):
        for c, v in enumerate(row, start=1):
            if v is not None:
                ws.cell(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _run(raw: bytes, recipe: dict[str, Any], db: str | None = None, filename: str = "明细.xlsx", **kw: Any):
    scan = csv_grid.scan_any(raw, filename)
    return recipe_engine.execute(Recipe.model_validate(recipe), raw, filename, scan, db, **kw)


def _rows(db: str, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _checks(db: str, ex: Any, recipe: dict[str, Any]) -> dict[str, Any]:
    return {c.id: c for c in recipe_checks.run_checks(db, ex, Recipe.model_validate(recipe))}


def _list(columns: list[tuple[str, str]], *, table: str = "明细", sheet: str = "明细", context: bool = False,
          **block: Any) -> dict[str, Any]:
    out = {
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": sheet},
                    "context": [{"id": "统计期"}] if context else [],
                    "blocks": [{"id": "列表1", "layout": "list", "table": table,
                                "columns": [{"header": h, "name": h, "type": t} for h, t in columns], **block}]}],
        "tables": [{"name": table, "grain": []}],
    }
    return out


# ==========================================================================
# 穿插的小计
# ==========================================================================

STORES = [("地区", "门店", "金额", "数量"),
          ("分区甲", "门店一", 100, 1), ("分区甲", "门店二", 200, 2), ("小计", None, 300, 3),
          ("分区乙", "门店三", 150, 4), ("分区乙", "门店四", 150, 5), ("小计", None, 300, 9),
          ("合计", None, 600, 12)]
STORE_COLS = [("地区", "TEXT"), ("门店", "TEXT"), ("金额", "INTEGER"), ("数量", "INTEGER")]


def _stores_recipe(**sub: Any) -> dict[str, Any]:
    return _list(STORE_COLS, rows={"total_row": {"label_column": "地区", "pick": "合计"},
                                   "subtotals": {"label_column": "地区", "pick": "小计", **sub}})


def test_subtotal_rows_are_checked_against_their_group_and_kept_out_of_the_data(tmp_path):
    db = str(tmp_path / "s.db")
    recipe = _stores_recipe()
    ex = _run(_book(STORES), recipe, db)
    assert ex.ok, [p.message for p in ex.problems]
    # 只有 4 行明细进了数据；两行小计、一行合计都不在
    assert _rows(db, 'SELECT "门店", "金额" FROM "明细" ORDER BY rowid') == [
        ("门店一", 100), ("门店二", 200), ("门店三", 150), ("门店四", 150)]
    subs = [d for d in ex.derived if d.segment == f"列表1{SUBTOTAL_KEY}"]
    assert [(d.cell, d.rowid_first, d.rowid_last) for d in subs] == [
        ("明细!C4", 1, 2), ("明细!D4", 1, 2), ("明细!C7", 3, 4), ("明细!D7", 3, 4)]
    [excluded] = [x for x in ex.rows_excluded if x.reason == "subtotal"]
    assert excluded.rows == [[4, 4], [7, 7]] and excluded.anchor == "小计" and excluded.block == "列表1"
    checks = _checks(db, ex, recipe)
    assert checks["T1"].title == "合计行按列求和（块「列表1」）" and checks["T1"].status == "passed"
    assert checks["T2"].title == "小计行按各组明细求和（块「列表1」）" and checks["T2"].status == "passed"
    assert checks["T2"].checked == 4


def test_a_wrong_subtotal_is_a_mismatch(tmp_path):
    rows = list(STORES)
    rows[6] = ("小计", None, 300, 10)                 # 4 + 5 ≠ 10
    db = str(tmp_path / "s.db")
    recipe = _stores_recipe()
    ex = _run(_book(rows), recipe, db)
    checks = _checks(db, ex, recipe)
    t2 = checks["T2"]
    assert t2.status == "mismatch" and t2.failed == 1 and t2.cells == ["明细!D7"]
    assert "按明细求和为 9" in t2.details[0]


def test_group_column_scope_only_covers_the_rows_of_the_same_group(tmp_path):
    """按组小计：紧挨在小计上面、分组列取值相同的那几行。小计行的标签写在「门店」列，分组按「地区」。
    同一份数据按「上一行小计之后的全部明细」核对就对不上（350 ≠ 50）。"""
    rows = [("地区", "门店", "金额"), ("分区甲", "门店一", 100), ("分区甲", "门店二", 200),
            ("分区乙", "门店三", 50), ("分区乙", "小计", 50)]
    cols = [("地区", "TEXT"), ("门店", "TEXT"), ("金额", "INTEGER")]
    recipe = _list(cols, rows={"subtotals": {"label_column": "门店", "pick": "小计", "scope": "group_column",
                                             "group_column": "地区", "keep_as": "明细_小计"}})
    recipe["tables"].append({"name": "明细_小计", "kind": "reported_total"})
    db = str(tmp_path / "g.db")
    ex = _run(_book(rows), recipe, db)
    assert ex.ok, [p.message for p in ex.problems]
    assert _checks(db, ex, recipe)["T1"].status == "passed"
    assert _rows(db, 'SELECT * FROM "明细_小计"') == [("小计", "分区乙", 50)]
    assert [c.name for c in derive_tables(Recipe.model_validate(recipe))[0]["明细_小计"]] == ["小计项", "地区", "金额"]
    since = copy.deepcopy(recipe)
    since["sheets"][0]["blocks"][0]["rows"]["subtotals"].update(scope="since_previous", group_column=None)
    since["tables"][1]["name"] = "明细_小计"
    db2 = str(tmp_path / "g2.db")
    ex2 = _run(_book(rows), since, db2)
    assert _checks(db2, ex2, since)["T1"].status == "mismatch"


def test_subtotal_validation():
    def problems(**sub: Any) -> list[str]:
        data = _stores_recipe(**sub)
        _, probs = validate_recipe(data, facts=None, origin="rules")
        return [p.code for p in probs]

    assert problems() == []
    assert "subtotal_group_missing" in problems(scope="group_column")
    assert "column_unknown" in problems(scope="group_column", group_column="城市")
    assert "subtotal_group_missing" in problems(group_column="地区")
    assert "period_literal" in problems(pick="2026小计")
    bad = _stores_recipe()
    bad["sheets"][0]["blocks"][0]["rows"]["subtotals"]["pick"] = "合计"
    assert "subtotal_pick_conflict" in [p.code for p in validate_recipe(bad, facts=None, origin="rules")[1]]


# ==========================================================================
# 列表按期累积：每一行属于哪一期
# ==========================================================================

PERIOD_CELL = ("A1", "统计时间：2026年8月1日至2026年8月31日")
ORDERS = [("订单号", "下单日期", "金额"), ("A001", "2026-08-01", 120), ("A002", "2026-08-15", 80),
          ("A003", "2026-08-31", 40)]
ORDER_COLS = [("订单号", "TEXT"), ("下单日期", "DATE"), ("金额", "INTEGER")]


def _orders(period: dict[str, Any], *, context: bool = True, grain: list[str] | None = None) -> dict[str, Any]:
    data = _list(ORDER_COLS, table="订单", context=context, period=period)
    data["tables"][0]["grain"] = grain if grain is not None else ["订单号"]
    return data


def test_synthesized_period_columns_carry_this_periods_bounds(tmp_path):
    recipe = _orders({"synthesize": "统计期"}, grain=["订单号", LIST_PERIOD_START])
    cols = derive_tables(Recipe.model_validate(recipe))[0]["订单"]
    assert [(c.name, c.role) for c in cols][-2:] == [(LIST_PERIOD_START, "period"), (LIST_PERIOD_END, "period")]
    db = str(tmp_path / "p.db")
    ex = _run(_book(ORDERS, top=[PERIOD_CELL], start=3), recipe, db)
    assert ex.ok, [p.message for p in ex.problems]
    assert _rows(db, f'SELECT "订单号", "{LIST_PERIOD_START}", "{LIST_PERIOD_END}" FROM "订单" ORDER BY rowid') == [
        ("A001", "2026-08-01", "2026-08-31"), ("A002", "2026-08-01", "2026-08-31"),
        ("A003", "2026-08-01", "2026-08-31")]
    assert ex.table_hashes["订单"]
    # 统计期起止列没有格子来历：溯源只有表头推出来的三列
    assert set(ex.lineage["订单"]) == {"订单号", "下单日期", "金额"}
    # 干跑（不建库）同样走得通
    assert _run(_book(ORDERS, top=[PERIOD_CELL], start=3), recipe).ok


def test_synthesized_period_with_a_human_period(tmp_path):
    import datetime as dt

    from app.data.recipe_types import PeriodInput

    recipe = _orders({"synthesize": "统计期"})
    raw = _book(ORDERS)
    assert {p.code for p in _run(raw, recipe).problems} == {"period_missing"}
    db = str(tmp_path / "h.db")
    ex = _run(raw, recipe, db, context_inputs={"统计期": PeriodInput(dt.date(2026, 8, 1), dt.date(2026, 8, 31))})
    assert ex.ok and _rows(db, f'SELECT DISTINCT "{LIST_PERIOD_START}" FROM "订单"') == [("2026-08-01",)]


def test_rows_must_fall_inside_the_period_when_bound_to_a_date_column(tmp_path):
    recipe = _orders({"column": "下单日期"})
    db = str(tmp_path / "w.db")
    ex = _run(_book(ORDERS, top=[PERIOD_CELL], start=3), recipe, db)
    assert ex.ok
    d1 = _checks(db, ex, recipe)["D1"]
    assert d1.kind == "period_rows" and d1.status == "passed" and d1.checked == 3
    late = list(ORDERS) + [("A004", "2026-09-02", 10)]
    db2 = str(tmp_path / "w2.db")
    ex2 = _run(_book(late, top=[PERIOD_CELL], start=3), recipe, db2)
    d1 = _checks(db2, ex2, recipe)["D1"]
    assert d1.status == "mismatch" and d1.category == "structure" and d1.failed == 1
    assert d1.cells == ["明细!B7"] and "2026-08-01 至 2026-08-31" in d1.details[0]


def test_list_period_validation():
    def codes(data: dict[str, Any]) -> list[str]:
        return [p.code for p in validate_recipe(data, facts=None, origin="rules")[1]]

    assert codes(_orders({"column": "下单日期"})) == []
    assert "list_period_invalid" in codes(_orders({"column": "金额"}))
    assert "column_unknown" in codes(_orders({"column": "付款日期"}))
    assert "list_period_invalid" in codes(_orders({"column": "下单日期", "synthesize": "统计期"}))
    assert "list_period_invalid" in codes(_orders({}))
    assert "covers_without_context" in codes(_orders({"synthesize": "统计期"}, context=False))
    clash = _list(ORDER_COLS + [(LIST_PERIOD_START, "TEXT")], table="订单", context=True,
                  period={"synthesize": "统计期"})
    assert "name_collision" in codes(clash)


def test_lists_become_eligible_for_accumulation_once_bound():
    unbound = Recipe.model_validate({**_list(ORDER_COLS, table="订单", context=True), "mode": "accumulate"})
    [p] = accumulate_blockers(unbound)
    assert "没有指定每一行属于哪一期" in p.message
    for period in ({"column": "下单日期"}, {"synthesize": "统计期"}):
        data = {**_orders(period), "mode": "accumulate"}
        assert accumulate_blockers(Recipe.model_validate(data)) == []
        assert validate_recipe(data, facts=None, origin="rules")[1] == []
    kept = {**_orders({"column": "下单日期"}), "mode": "accumulate"}
    kept["sheets"][0]["blocks"][0]["rows"] = {"total_row": {"label_column": "订单号", "pick": "合计",
                                                            "keep_as": "订单_表内合计"}}
    kept["tables"].append({"name": "订单_表内合计", "grain": ["合计项"], "kind": "reported_total"})
    blockers = accumulate_blockers(Recipe.model_validate(kept))
    assert [p.path for p in blockers] == ["/tables/1"] and "合计行、小计行另存的表" in blockers[0].message


def _built(raw: bytes, recipe: dict[str, Any], tag: str) -> tuple[str, Any]:
    folder = Path(tempfile.mkdtemp(prefix="p5-built-"))
    db = folder / f"{tag}.db"
    ex = _run(raw, recipe, str(db))
    assert ex.ok, [p.message for p in ex.problems]
    table_versions.settle_journal(db)
    return str(db), ex


def _part(db: str, ex: Any, recipe: dict[str, Any], **kw: Any) -> UnionPart:
    return UnionPart(import_id=None, start=kw.get("start", ex.period.start), end=kw.get("end", ex.period.end),
                     db_path=db, recipe=Recipe.model_validate(recipe), db_sha256=raw_store.sha256_file(db),
                     table_hashes=dict(ex.table_hashes))


@pytest.mark.parametrize("period", [{"column": "下单日期"}, {"synthesize": "统计期"}])
def test_two_months_of_a_list_materialize_into_one_union(tmp_path, period):
    recipe = {**_orders(period), "mode": "accumulate"}
    aug = _built(_book(ORDERS, top=[PERIOD_CELL], start=3), recipe, "aug")
    sep_rows = [ORDERS[0], ("B001", "2026-09-03", 70), ("B002", "2026-09-30", 30)]
    sep = _built(_book(sep_rows, top=[("A1", "统计时间：2026年9月1日至2026年9月30日")], start=3), recipe, "sep")
    out = tmp_path / "u.db"
    report = materialize_union(out, target=Recipe.model_validate(recipe),
                               parts=[_part(*aug, recipe), _part(*sep, recipe)])
    assert report.ok, [c.details for c in report.checks]
    assert report.rows == {"订单": 5}
    assert {c.id: c.status for c in report.checks} == {"U1": "passed", "U2": "passed", "U3": "passed"}
    assert report.checks[2].checked == 2
    # 登记的统计期和库里的对不上（把 8 月的库说成 9 月）：U3 拒收
    bad = materialize_union(tmp_path / "bad.db", target=Recipe.model_validate(recipe),
                            parts=[_part(*aug, recipe, start="2026-09-01", end="2026-09-30")])
    u3 = next(c for c in bad.checks if c.id == "U3")
    assert not bad.ok and u3.status == "mismatch" and "第 1 期" in u3.details[0]


def test_the_same_order_in_two_periods_is_a_union_key_conflict(tmp_path):
    """明细跨期重复（同一订单两期都出现）按日期列分期时主键冲突，U2 拒收（细案风险条：语义要在配方里显式声明）。"""
    recipe = {**_orders({"synthesize": "统计期"}), "mode": "accumulate"}
    aug = _built(_book(ORDERS, top=[PERIOD_CELL], start=3), recipe, "aug2")
    sep = _built(_book(ORDERS, top=[("A1", "统计时间：2026年9月1日至2026年9月30日")], start=3), recipe, "sep2")
    report = materialize_union(tmp_path / "u.db", target=Recipe.model_validate(recipe),
                               parts=[_part(*aug, recipe), _part(*sep, recipe)])
    u2 = next(c for c in report.checks if c.id == "U2")
    assert not report.ok and u2.status == "mismatch"
    # 主键带上统计期起就分得开：每月一份的快照式明细
    keyed = {**_orders({"synthesize": "统计期"}, grain=["订单号", LIST_PERIOD_START]), "mode": "accumulate"}
    aug = _built(_book(ORDERS, top=[PERIOD_CELL], start=3), keyed, "aug3")
    sep = _built(_book(ORDERS, top=[("A1", "统计时间：2026年9月1日至2026年9月30日")], start=3), keyed, "sep3")
    assert materialize_union(tmp_path / "k.db", target=Recipe.model_validate(keyed),
                             parts=[_part(*aug, keyed), _part(*sep, keyed)]).rows == {"订单": 6}


# ==========================================================================
# 起草：表格对象、穿插的小计、每一行属于哪一期的问题
# ==========================================================================


def _ok_validate(data, facts, origin):
    from app.data.recipe import validate_recipe as v

    return v(data, facts=facts, origin=origin)


def _draft(raw: bytes, filename: str = "明细.xlsx"):
    scan = csv_grid.scan_any(raw, filename)
    grids = {s.name: xlsx_cells.read_grid(raw, scan, s.name, max_rows=recipe_suggest.DRAFT_MAX_ROWS)
             for s in scan.sheets if s.state == "visible" and s.nonempty}

    def dry(recipe: Recipe):
        return recipe_engine.execute(recipe, raw, filename, scan, None)
    return recipe_suggest.draft(scan, grids, filename, validate=_ok_validate, dry_run=dry), scan, grids


def _table_book(*, totals: bool = True, name: str = "门店销售") -> bytes:
    """一个表格对象：B3:D7（表头、3 行明细、汇总行），表格上面有一行说明、右边有一个空格隔开的备注列。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "销售"
    ws["B1"] = "门店销售明细"
    rows = [("门店", "日期", "金额"), ("门店一", "2026-08-01", 10), ("门店二", "2026-08-02", 20),
            (None, None, None), ("门店三", "2026-08-03", 30)]
    if totals:
        rows.append(("汇总", None, 60))
    for r, row in enumerate(rows, start=3):
        for c, v in enumerate(row, start=2):
            if v is not None:
                ws.cell(r, c, v)
    t = Table(displayName=name, ref=f"B3:D{2 + len(rows)}")
    if totals:
        t.totalsRowCount = 1
    ws.add_table(t)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_table_object_is_drafted_as_a_list_without_ai():
    raw = _table_book()
    scan = xlsx_scan.scan(raw)
    assert [(t.name, t.ref, t.totals_row_count) for t in scan.tables] == [("门店销售", "B3:D8", 1)]
    draft, _, _ = _draft(raw)
    assert draft.complete and draft.origin == "rules", draft.failures
    [block] = draft.recipe["sheets"][0]["blocks"]
    assert block["layout"] == "list" and block["table"] == "门店销售"
    assert [c["header"] for c in block["columns"]] == ["门店", "日期", "金额"]
    assert block["rows"]["total_row"] == {"label_column": "门店", "pick": "汇总", "keep_as": "门店销售_表内合计"}
    assert block["rows"]["blank_rows"] == "skip"
    # 锚点按表头文字，不记坐标
    assert "B3" not in str(block)


def test_a_table_object_with_digits_in_its_name_falls_back_to_the_title():
    draft, _, _ = _draft(_table_book(name="表1"))
    assert draft.complete and draft.recipe["sheets"][0]["blocks"][0]["table"] == "门店销售明细"


def test_drafted_subtotals_run_and_verify(tmp_path):
    raw = _book(STORES)
    draft, scan, _ = _draft(raw)
    assert draft.complete, draft.failures
    block = draft.recipe["sheets"][0]["blocks"][0]
    assert block["rows"]["subtotals"]["pick"] == "小计" and block["rows"]["total_row"]["pick"] == "合计"
    db = str(tmp_path / "d.db")
    recipe = Recipe.model_validate(draft.recipe)
    ex = recipe_engine.execute(recipe, raw, "明细.xlsx", scan, db)
    assert ex.ok and ex.tables[0].rows == 4
    assert all(c.status == "passed" for c in recipe_checks.run_checks(db, ex, recipe) if c.kind == "column_sum")


def test_period_question_binds_the_list_and_switches_to_accumulation():
    raw = _book(ORDERS, top=[PERIOD_CELL], start=3)
    draft, _, grids = _draft(raw)
    assert draft.complete, draft.failures
    from app.data import recipe as R

    recipe = draft.recipe
    cards, questions = recipe_suggest.questions_for(recipe, draft.facts, grids)
    q = next(q for q in questions if q.id == "q_list_period:列表1")
    assert [o["value"] for o in q.options] == ["none", "column:下单日期", "synthesize"] and q.default == "none"
    assert not any(q.id == "q_mode" for q in questions)
    bound = R.apply_patch(recipe, q.effects["column:下单日期"])
    assert bound["sheets"][0]["blocks"][0]["period"]["column"] == "下单日期" and bound["mode"] == "accumulate"
    assert validate_recipe(bound, facts=None, origin="rules")[1] == []
    assert R.apply_patch(recipe, q.effects["none"])["mode"] == "replace"
    # 工作表没配统计期的，选了以后一并加上
    plain = _book(ORDERS)
    draft2, _, grids2 = _draft(plain)
    assert draft2.recipe["sheets"][0]["context"] == []
    _, qs = recipe_suggest.questions_for(draft2.recipe, draft2.facts, grids2)
    q2 = next(q for q in qs if q.id == "q_list_period:列表1")
    bound2 = R.apply_patch(draft2.recipe, q2.effects["synthesize"])
    assert bound2["sheets"][0]["context"][0]["id"] == "统计期"
    assert bound2["sheets"][0]["blocks"][0]["period"]["synthesize"] == "统计期"
    assert accumulate_blockers(Recipe.model_validate(bound2)) == []


# ==========================================================================
# CSV
# ==========================================================================


def test_csv_drafts_a_list_and_the_engine_runs_it(tmp_path):
    raw = ("订单号,下单日期,门店,金额\n"
           "A001,2026-08-01,一号店,120\n"
           "A002,2026-08-02,二号店,80.5\n"
           "小计,,,200.5\n"
           "A003,2026-08-03,一号店,1\n"
           "合计,,,201.5\n").encode("gb18030")
    draft, scan, _ = _draft(raw, "orders_2026-08.csv")
    assert draft.complete and draft.origin == "rules", draft.failures
    sheet = draft.recipe["sheets"][0]
    assert sheet["match"]["name"] == csv_grid.CSV_SHEET
    block = sheet["blocks"][0]
    assert [c["type"] for c in block["columns"]] == ["TEXT", "DATE", "TEXT", "REAL"]
    assert block["rows"]["subtotals"]["pick"] == "小计"
    db = str(tmp_path / "c.db")
    recipe = Recipe.model_validate(draft.recipe)
    ex = recipe_engine.execute(recipe, raw, "orders_2026-08.csv", scan, db)
    assert ex.ok and ex.tables[0].rows == 3
    assert all(c.status == "passed" for c in recipe_checks.run_checks(db, ex, recipe) if c.kind == "column_sum")
    # 回执的坐标照 Excel 打开这个 CSV 时的 A1 写
    [excluded] = [x for x in ex.rows_excluded if x.reason == "subtotal"]
    assert excluded.sheet == "CSV" and excluded.rows == [[4, 4]]


# ==========================================================================
# 没配小计时，像小计的行：照常导入、要人确认，带「改作小计核对」的修复按钮
# ==========================================================================


def test_a_subtotal_like_row_without_subtotals_is_flagged_and_fixable(tmp_path):
    from app.data import recipe_confirm, recipe_fixes
    from app.data.recipe import apply_patch

    plain = _list(STORE_COLS, rows={"total_row": {"label_column": "地区", "pick": "合计"}})
    ex = _run(_book(STORES), plain)
    [p] = [p for p in ex.problems if p.code == "subtotal_suspect"]
    assert p.category == "confirm" and p.fix == "declare_subtotals"
    assert p.cells == ["明细!A4", "明细!A7"] and p.fix_args["pick"] == "小计" and p.fix_args["label_column"] == "地区"
    assert ex.ok                                      # 需确认类：照常导入，提交时勾确认项
    full = Recipe.model_validate(plain).model_dump(mode="json")
    [prop] = recipe_fixes.propose_fixes(full, [recipe_fixes.dataclasses.asdict(p)], [], None)
    assert prop.kind == "declare_subtotals" and [o.value for o in prop.options] == ["check_only", "keep"]
    res = recipe_fixes.fix_ops(full, prop, "check_only", None)
    assert res.ok, res.problems
    fixed = apply_patch(full, res.ops)
    assert fixed["sheets"][0]["blocks"][0]["rows"]["subtotals"]["pick"] == "小计"
    db = str(tmp_path / "f.db")
    ex2 = _run(_book(STORES), fixed, db)
    assert ex2.ok and not [p for p in ex2.problems if p.code == "subtotal_suspect"]
    assert _checks(db, ex2, fixed)["T2"].status == "passed"
    keep = apply_patch(full, recipe_fixes.fix_ops(full, prop, "keep", None).ops)
    assert keep["tables"][-1] == {"name": "明细_小计", "grain": [], "kind": "reported_total", "units": {}, "note": ""}
    assert validate_recipe(keep, facts=None, origin="rules")[1] == []
    # 确认项：没改作小计核对就提交的，要勾「确实是数据」
    items = recipe_confirm.confirm_items(recipe_confirm.ConfirmContext(kind="first", recipe=plain, extraction=ex,
                                                                       checks=[]))
    [item] = [i for i in items if i.id == "subtotal_suspect:列表1"]
    assert item.required and "改作小计核对" in item.label


def test_ordinary_rows_starting_with_a_total_word_are_not_flagged():
    """首列以「累计」开头、别的文字列有字的是数据（「累计积分卡」这种商品名），不当成像小计的行。"""
    rows = [("商品", "门店", "金额"), ("累计积分卡", "门店一", 10), ("普通卡", "门店二", 20)]
    ex = _run(_book(rows), _list([("商品", "TEXT"), ("门店", "TEXT"), ("金额", "INTEGER")]))
    assert ex.ok and not [p for p in ex.problems if p.code == "subtotal_suspect"]


def _suspects(data: list[tuple[Any, ...]], columns: list[tuple[str, str]], **block: Any) -> list[str]:
    ex = _run(_book(data), _list(columns, **block))
    assert ex.ok, [p.message for p in ex.problems]
    return [c for p in ex.problems if p.code == "subtotal_suspect" for c in p.cells]


def test_a_two_column_key_value_list_is_never_flagged():
    """复核反例：只有「标签文字列 + 数字列」的键值清单，「其余文字列为空」自然成立，不能据此说它像小计——
    「累计收入」「汇总调整」是普通的数据行。最后一行以合计字样开头也一样。"""
    cols = [("项目", "TEXT"), ("金额", "INTEGER")]
    assert _suspects([("项目", "金额"), ("门票收入", 100), ("累计收入", 300), ("汇总调整", 5), ("餐饮收入", 50)],
                     cols) == []
    assert _suspects([("项目", "金额"), ("门票收入", 100), ("餐饮收入", 50), ("合计调整项", 5)], cols) == []


def test_only_rows_between_details_are_flagged():
    """像小计的行要夹在明细中间：上下相邻的明细在某个文字列上有值、这一行空着；后面还得有明细（或者是合计行）。
    放在最后、后面没有明细的不报（那不是「夹在中间」）；上下的明细那一列本来就空着的也不报。"""
    cols = [("地区", "TEXT"), ("门店", "TEXT"), ("金额", "INTEGER")]
    head = ("地区", "门店", "金额")
    # 最后一行：后面没有明细，也没有合计行
    assert _suspects([head, ("分区甲", "门店一", 100), ("分区甲", "门店二", 200), ("小计", None, 300)], cols) == []
    # 上面的明细「门店」也空着：不是因为小计才空的
    assert _suspects([head, ("分区甲", None, 100), ("累计调整", None, 5), ("分区乙", "门店三", 50)], cols) == []
    # 下面的明细「门店」空着：同样不报
    assert _suspects([head, ("分区甲", "门店一", 100), ("累计调整", None, 5), ("分区乙", None, 50)], cols) == []
    # 夹在明细中间：报
    assert _suspects([head, ("分区甲", "门店一", 100), ("小计", None, 100), ("分区乙", "门店三", 50)],
                     cols) == ["明细!A3"]
    # 最后一组的小计后面紧跟合计行：也在列表里面，报
    total = {"rows": {"total_row": {"label_column": "地区", "pick": "合计"}}}
    assert _suspects([head, ("分区甲", "门店一", 100), ("小计", None, 100), ("合计", None, 100)], cols,
                     **total) == ["明细!A3"]


def test_declaring_no_subtotals_keeps_such_rows_as_data_without_asking(tmp_path):
    """人确认过「这张列表没有小计行」（rows.no_subtotals）：以合计字样开头的行照常是数据，不再每期报 subtotal_suspect、
    不再每期要勾确认；这个声明本身是放宽，配上的那一次要确认一次。和 subtotals 只能二选一。"""
    from app.data import recipe_confirm

    recipe = _list(STORE_COLS, rows={"total_row": {"label_column": "地区", "pick": "合计"}, "no_subtotals": True})
    assert validate_recipe(recipe, facts=None, origin="rules")[1] == []
    db = str(tmp_path / "n.db")
    ex = _run(_book(STORES), recipe, db)
    assert ex.ok and not [p for p in ex.problems if p.code == "subtotal_suspect"]
    assert _rows(db, 'SELECT COUNT(*) FROM "明细"') == [(6,)]          # 两行「小计」照常是数据
    items = recipe_confirm.confirm_items(recipe_confirm.ConfirmContext(kind="first", recipe=recipe, extraction=ex,
                                                                       checks=[]))
    ids = [i.id for i in items]
    assert "no_subtotals:列表1" in ids and not [i for i in ids if i.startswith("subtotal_suspect:")]
    sigs = recipe_confirm.recipe_item_signatures(Recipe.model_validate(recipe))
    assert sigs["no_subtotals:列表1"] != recipe_confirm.recipe_item_signatures(
        Recipe.model_validate(_list(STORE_COLS)))["no_subtotals:列表1"]
    both = _stores_recipe()
    both["sheets"][0]["blocks"][0]["rows"]["no_subtotals"] = True
    probs = validate_recipe(both, facts=None, origin="rules")[1]
    assert [p.code for p in probs] == ["subtotals_conflict"] and probs[0].path.endswith("/rows/no_subtotals")
