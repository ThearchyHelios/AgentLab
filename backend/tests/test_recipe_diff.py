"""差异卡（recipe_diff）的测试。

回执一律手写（结构照 P2-SPEC 9.1 的「结构仿照客流表」夹具，假名、假数），不依赖执行器（WP-2）和夹具
生成器（WP-1）：差异卡是纯函数，输入就是回执 dict。WP-7 会再用真夹具把 9.2 的 31 例跑一遍。

本文件还放着两个测试文件共用的回执构造器（flow_doc 等），test_recipe_confirm 从这里导入。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.data import recipe_diff as D
from app.data.recipe_confirm import ConfirmContext, confirm_items
from app.data.recipe_parsers import match_key, period_residue

FLOW: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))

SHEET = "客流汇总"
AUG = dt.date(2026, 8, 1)
SEP = dt.date(2026, 9, 1)
OCT = dt.date(2026, 10, 1)
DAILY = ["全日客流（人次）", "分区甲（人次）", "分区乙（人次）"]
DAY = ["7-8", "8-9", "9-10", "10-11", "11-12", "12-13", "13-14", "14-15", "15-16", "16-17", "17-18"]
NIGHT = ["18-19", "19-20", "20-21", "21-22", "22-23", "23-24"]
TOTAL_RAW = ["18-22 时合计", "22-24 时合计", "18-24 时合计"]
TOTAL_CAN = ["18-22时合计", "22-24时合计", "18-24时合计"]


# --------------------------------------------------------------------------
# 回执构造器（两个测试文件共用）
# --------------------------------------------------------------------------


def b2_text(start: dt.date, end: dt.date, *, omit_year: bool = False) -> str:
    second = f"{end.month}月{end.day}日" if omit_year else f"{end.year}年{end.month}月{end.day}日"
    return f"统计时间范围：{start.year}年{start.month}月{start.day}日至{second}"


def file_name(start: dt.date, days: int) -> str:
    end = start + dt.timedelta(days=days - 1)
    return f"月报导出_{start.isoformat()}_{end.isoformat()}.xlsx"


def _col(name: str, type_: str, role: str, header: str | None = None, unit: str | None = None) -> dict[str, Any]:
    return {"name": name, "type": type_, "header": header, "unit": unit, "role": role}


def _seg(segment: str, raw: list[str], canonical: list[str], rows: list[int]) -> dict[str, Any]:
    return {"segment": segment, "raw": list(raw), "canonical": list(canonical), "rows": list(rows)}


def _check(cid: str, kind: str, status: str, *, checked: int = 0, failed: int = 0,
           category: str = "info") -> dict[str, Any]:
    return {"id": cid, "kind": kind, "title": cid, "status": status, "category": category, "checked": checked,
            "failed": failed, "unverifiable": 0, "sql": None, "params": [], "details": [], "cells": [],
            "acceptable": False, "reasons": {}}


def flow_doc(start: dt.date = AUG, days: int = 31) -> dict[str, Any]:
    """「导入回执」：{"receipt": 回执, "checks": [...], "period": PeriodOut}，按参考配方、参考布局手写。"""
    end = start + dt.timedelta(days=days - 1)
    period = {"start": start.isoformat(), "end": end.isoformat(), "source": "cells", "cells": [f"{SHEET}!B2"],
              "signed_by": None, "texts": {f"{SHEET}!B2": b2_text(start, end)}, "annotated": {}}
    tables = [
        {"name": "日客流", "sheet": SHEET, "kind": "data", "grain": ["日期"], "rows": days,
         "columns": [_col("日期", "TEXT", "axis"), _col("全日客流", "INTEGER", "measure", DAILY[0], "人次"),
                     _col("分区甲", "INTEGER", "measure", DAILY[1], "人次"),
                     _col("分区乙", "INTEGER", "measure", DAILY[2], "人次")],
         "sources": ["s1/交叉表/日客流"]},
        {"name": "时段客流", "sheet": SHEET, "kind": "data", "grain": ["日期", "时段"], "rows": days * 17,
         "columns": [_col("日期", "TEXT", "axis"), _col("时段", "TEXT", "dim"), _col("时段类别", "TEXT", "const"),
                     _col("起始小时", "INTEGER", "derive"), _col("结束小时", "INTEGER", "derive"),
                     _col("客流", "INTEGER", "value", None, "人次")],
         "sources": ["s1/交叉表/日间", "s1/交叉表/夜间"]},
        {"name": "时段客流_表内合计", "sheet": SHEET, "kind": "reported_total", "grain": ["日期", "合计项"],
         "rows": days * 3,
         "columns": [_col("日期", "TEXT", "axis"), _col("合计项", "TEXT", "dim"), _col("起始小时", "INTEGER", "derive"),
                     _col("结束小时", "INTEGER", "derive"), _col("客流", "INTEGER", "value", None, "人次")],
         "sources": ["s1/交叉表/夜间合计"]},
    ]
    receipt = {
        "ledger": [{"sheet": SHEET, "nonempty_scan": 20 * days + 3 * days + 47,
                    "nonempty_read": 20 * days + 3 * days + 47, "roles": {}, "unclaimed": 0}],
        "tables": tables,
        "period": copy.deepcopy(period),
        "axes": [{"block": "交叉表", "row": 4, "first": f"{start.month}月{start.day}日",
                  "last": f"{end.month}月{end.day}日", "count": days, "form": "text"}],
        "placeholders": {"·": days},
        "canonicalized": [],
        "canonicalized_total": 0,
        "labels": [_seg("日客流", DAILY, [match_key(x) for x in DAILY], [5, 6, 7]),
                   _seg("日间", DAY, DAY, list(range(10, 21))),
                   _seg("夜间", NIGHT, NIGHT, list(range(22, 28))),
                   _seg("夜间合计", TOTAL_RAW, TOTAL_CAN, [28, 29, 30])],
        "outside_text": [{"sheet": SHEET, "cell": "B3", "text": "客流汇总表", "kind": "text", "period_source": False}],
        "hidden": {},
        "sheets": {"matched": {"s1": SHEET}, "renamed": {}, "other_visible": [], "skipped_hidden": []},
        "derived_form": {"夜间合计": "formula"},
        "block_order": {SHEET: ["日客流", "日间", "夜间", "夜间合计"]},
        "ignored_columns": {},
        "full_calc_on_load": False,
        "formula_cells_accepted": 0,
        "blank_rows_skipped": 0,
        "derived": [],
        "lineage": {},
        "db_sha256": "0" * 64,
        "table_hashes": {},
        "timings": {},
    }
    checks = [
        _check("C1", "context_agree", "passed", checked=1),
        _check("C2", "filename_period", "passed", checked=1),
        _check("K1", "derived_sum", "passed", checked=3 * days, category="structure"),
        _check("G1", "formula_refs", "passed", checked=3 * days, category="structure"),
        _check("R1", "relation_sum_eq", "passed", checked=days, category="data_quality"),
        _check("R2", "relation_not_comparable", "info", checked=days, failed=days),
    ]
    for i, t in enumerate(("日客流", "时段客流", "时段客流_表内合计"), 1):
        checks.append(_check(f"N{i}", "row_count", "passed", checked=1, category="structure"))
        checks.append(_check(f"P{i}", "pk_unique", "passed", checked=1, category="structure"))
    return {"receipt": receipt, "checks": checks, "period": period}


def extraction_of(doc: dict[str, Any], problems: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """回执 → 确认项要的 Extraction dict 形状（回执就是 Extraction 去掉 problems 和 C1、C2）。"""
    ex = copy.deepcopy(doc["receipt"])
    ex["period"] = copy.deepcopy(doc["period"])
    ex["problems"] = list(problems or [])
    ex["ok"] = True
    return ex


def set_period_cell(doc: dict[str, Any], cell: str, text: str, *, annotated: bool) -> None:
    """把 cell 设成统计期来源；annotated=True 时同时记成区域外文字（kind=text_digits、period_source）。"""
    key = f"{SHEET}!{cell}"
    for p in (doc["period"], doc["receipt"]["period"]):
        if key not in p["cells"]:
            p["cells"].append(key)
        p["texts"][key] = text
        if annotated:
            p["annotated"][key] = period_residue(text)
    out = [o for o in doc["receipt"]["outside_text"] if o["cell"] != cell]
    if annotated:
        out.append({"sheet": SHEET, "cell": cell, "text": text, "kind": "text_digits", "period_source": True})
    doc["receipt"]["outside_text"] = out


def add_outside(doc: dict[str, Any], cell: str, text: str, kind: str = "text_digits") -> None:
    doc["receipt"]["outside_text"].append({"sheet": SHEET, "cell": cell, "text": text, "kind": kind,
                                          "period_source": False})


def set_check(doc: dict[str, Any], cid: str, status: str) -> None:
    for c in doc["checks"]:
        if c["id"] == cid:
            c["status"] = status


def set_labels(doc: dict[str, Any], segment: str, raw: list[str] | None = None,
               canonical: list[str] | None = None, rows: list[int] | None = None) -> None:
    for s in doc["receipt"]["labels"]:
        if s["segment"] == segment:
            if raw is not None:
                s["raw"] = list(raw)
            if canonical is not None:
                s["canonical"] = list(canonical)
            if rows is not None:
                s["rows"] = list(rows)


def kinds(items: list[Any]) -> set[str]:
    return {i.kind for i in items}


def sep() -> tuple[dict[str, Any], str]:
    return flow_doc(SEP, 30), file_name(SEP, 30)


def run(prev: tuple[dict[str, Any], str] | None, cur: tuple[dict[str, Any], str], **kw: Any) -> list[Any]:
    """默认按参考配方、配方没变（上传新一期）调用；要测别的就显式传。"""
    kw.setdefault("recipe", FLOW)
    kw.setdefault("recipe_changed", False)
    return D.diff_reports(prev[0] if prev else None, cur[0], prev_file_name=prev[1] if prev else None,
                          cur_file_name=cur[1], **kw)


BASE = (flow_doc(AUG, 31), file_name(AUG, 31))


# --------------------------------------------------------------------------
# 9.2 的 31 例：差异的精确集合、本期类与 diff 确认项的精确集合
# --------------------------------------------------------------------------


def _d00():
    return flow_doc(AUG, 31), file_name(AUG, 31)


def _d02():
    return flow_doc(OCT, 31), file_name(OCT, 31)


def _d03():
    return flow_doc(OCT, 15), file_name(OCT, 15)


def _d03b():
    return flow_doc(OCT, 7), file_name(OCT, 7)


def _d12():
    doc, name = sep()
    text = b2_text(SEP, dt.date(2026, 9, 30), omit_year=True)
    doc["period"]["texts"][f"{SHEET}!B2"] = text
    doc["receipt"]["period"]["texts"][f"{SHEET}!B2"] = text
    return doc, name


def _d14():
    # 去掉第 8 行空行：第 8 行以下整体上移一行，标签的行号都变了，顺序没变
    doc, name = sep()
    for s in doc["receipt"]["labels"]:
        s["rows"] = [r - 1 if r > 8 else r for r in s["rows"]]
    return doc, name


def _d17():
    doc, name = sep()
    set_period_cell(doc, "B3", "客流汇总表（2026年9月）", annotated=True)
    return doc, name


def _d23():
    doc, _ = sep()
    set_check(doc, "C2", "info")
    return doc, "9月客流.xlsx"


def _d25():
    doc, name = sep()
    doc["receipt"]["full_calc_on_load"] = True
    return doc, name


def _d27():
    doc, name = sep()
    set_period_cell(doc, "B3", "客流汇总表（2026年9月1日至2026年9月30日）", annotated=True)
    return doc, name


def _d05():
    doc, name = sep()
    doc["receipt"]["block_order"][SHEET] = ["日客流", "夜间", "夜间合计", "日间"]
    set_labels(doc, "夜间", rows=list(range(10, 16)))
    set_labels(doc, "夜间合计", rows=[16, 17, 18])
    set_labels(doc, "日间", rows=list(range(21, 32)))
    return doc, name


def _d06():
    doc, name = sep()
    doc["receipt"]["block_order"][SHEET] = ["日间", "夜间", "夜间合计", "日客流"]
    add_outside(doc, "B28", "日客流（人次）", kind="text")
    return doc, name


def _d07():
    doc, name = sep()
    doc["receipt"]["derived_form"]["夜间合计"] = "literal"
    doc["checks"] = [c for c in doc["checks"] if c["id"] != "G1"]
    return doc, name


def _d10():
    doc, name = sep()
    doc["receipt"]["axes"][0]["form"] = "date"
    return doc, name


def _d11():
    doc, name = sep()
    set_labels(doc, "日间", raw=[f"{int(a):02d}:00-{int(b):02d}:00" for a, b in (x.split("-") for x in DAY)])
    set_labels(doc, "夜间", raw=[f"{int(a):02d}:00-{int(b):02d}:00" for a, b in (x.split("-") for x in NIGHT)])
    return doc, name


def _d19():
    doc, name = sep()
    order = [DAILY[1], DAILY[2], DAILY[0]]
    set_labels(doc, "日客流", raw=order, canonical=[match_key(x) for x in order])
    return doc, name


def _d22():
    doc, name = sep()
    set_labels(doc, "夜间合计", raw=["18:00-22:00合计", "22:00-24:00合计", "18:00-24:00合计"])
    return doc, name


def _d24():
    doc, name = sep()
    set_labels(doc, "日间", raw=[x.replace("-", "–") for x in DAY])
    set_labels(doc, "夜间", raw=[x.replace("-", "–") for x in NIGHT])
    return doc, name


def _d20():
    doc, name = sep()
    doc["receipt"]["sheets"]["other_visible"] = ["说明"]
    return doc, name


def _d28():
    doc, name = sep()
    add_outside(doc, "B32", "注：9月15日闸机故障，当日客流为估算值")
    return doc, name


def _d28b():
    doc, name = sep()
    set_period_cell(doc, "B32", "注：2026年9月数据为初步统计，待修订", annotated=True)
    return doc, name


def _d26():
    doc, name = sep()
    set_check(doc, "R1", "mismatch")
    return doc, name


#: 代号 → (构造, 差异的精确集合, 本期类与 diff 确认项的精确集合；带 * 的按前缀)。P2-SPEC 9.2 的表
DRIFT: dict[str, tuple[Callable[[], tuple[dict[str, Any], str]], set[str], set[str]]] = {
    "D01": (sep, {"period", "rows"}, set()),
    "D02": (_d02, {"period"}, set()),
    "D03": (_d03, {"period", "rows"}, set()),
    "D03b": (_d03b, {"period", "rows"}, set()),
    "D12": (_d12, {"period", "rows", "context_text"}, set()),
    "D14": (_d14, {"period", "rows"}, set()),
    "D15": (sep, {"period", "rows"}, set()),
    "D17": (_d17, {"period", "rows", "context_source_added"}, set()),
    "D23": (_d23, {"period", "rows", "file_name", "checks"}, set()),
    "D25": (_d25, {"period", "rows", "full_calc"}, set()),
    "D27": (_d27, {"period", "rows", "context_source_added"}, set()),
    "D05": (_d05, {"period", "rows", "block_order"}, {"diff:block_order:客流汇总"}),
    "D06": (_d06, {"period", "rows", "block_order", "outside_added"},
            {"diff:block_order:客流汇总", "diff:outside:客流汇总!B*"}),
    "D07": (_d07, {"period", "rows", "checks", "derived_form"}, {"diff:derived_form:夜间合计"}),
    "D10": (_d10, {"period", "rows", "axis_form"}, {"diff:axis_form:交叉表"}),
    "D11": (_d11, {"period", "rows", "label_writing"}, {"diff:label_writing:日间", "diff:label_writing:夜间"}),
    "D19": (_d19, {"period", "rows", "row_order"}, {"diff:row_order:日客流"}),
    "D22": (_d22, {"period", "rows", "label_writing"}, {"diff:label_writing:夜间合计"}),
    "D24": (_d24, {"period", "rows", "label_writing"}, {"diff:label_writing:日间", "diff:label_writing:夜间"}),
    "D20": (_d20, {"period", "rows"}, {"sheet_extra:说明"}),
    "D28": (_d28, {"period", "rows", "outside_added"}, {"outside_digits:客流汇总!B32", "diff:outside:客流汇总!B32"}),
    "D28b": (_d28b, {"period", "rows", "outside_added"},
             {"outside_digits:客流汇总!B32", "diff:outside:客流汇总!B32"}),
    "D26": (_d26, {"period", "rows", "checks"}, set()),
}
#: 拒收的 7 例：试运行就拒收了，9.2 不给差异和确认项的期望（diff_kinds=None），这里不跑
REJECTED = {"D04", "D09", "D13", "D16", "D18", "D21", "D08"}

#: 9.1：D00 首次导入时的确认项（精确集合）
D00_CONFIRMS = {
    "placeholder:·", "derived:夜间合计", "year_from:交叉表", "relation:R1", "relation:R2",
    "unit:日客流.全日客流", "unit:日客流.分区甲", "unit:日客流.分区乙", "unit:时段客流.客流",
    "unit:时段客流_表内合计.客流", "mode",
}


def match_ids(actual: set[str], expected: set[str]) -> None:
    """精确集合比对；expected 里以 * 结尾的按前缀匹配（至少命中一个）。"""
    exact = {e for e in expected if not e.endswith("*")}
    prefixes = [e[:-1] for e in expected if e.endswith("*")]
    rest = actual - exact
    assert exact <= actual, f"缺：{exact - actual}"
    for p in prefixes:
        assert any(a.startswith(p) for a in rest), f"没有以 {p} 开头的"
    stray = {a for a in rest if not any(a.startswith(p) for p in prefixes)}
    assert not stray, f"多出：{stray}"


def test_drift_table_covers_all_31_cases():
    assert len(DRIFT) + len(REJECTED) + 1 == 31   # + D00


def test_d00_first_import():
    doc, name = _d00()
    assert run(None, (doc, name)) == []
    items = confirm_items(ConfirmContext(kind="first", recipe=FLOW, extraction=extraction_of(doc),
                                         checks=doc["checks"], diff=[], prev=None, recipe_origin="rules"))
    assert {i.id for i in items} == D00_CONFIRMS
    assert all(i.required for i in items)


def test_d00_same_file_again_has_no_diff():
    assert run(BASE, _d00()) == []


@pytest.mark.parametrize("code", sorted(DRIFT))
def test_drift_case(code):
    build, want_kinds, want_confirms = DRIFT[code]
    cur = build()
    diff = run(BASE, cur, recipe=FLOW)
    assert kinds(diff) == want_kinds
    items = confirm_items(ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW,
                                         extraction=extraction_of(cur[0]), checks=cur[0]["checks"], diff=diff,
                                         prev=BASE[0], recipe_origin="rules"))
    match_ids({i.id for i in items}, want_confirms)


def test_drift_outside_added_cell_for_d06():
    diff = run(BASE, _d06())
    assert [d.confirm_id for d in diff if d.kind == "outside_added"] == ["diff:outside:客流汇总!B28"]


# --------------------------------------------------------------------------
# 数字遮盖后的模板
# --------------------------------------------------------------------------


def test_digit_template():
    # 先 canon（NFKC 会把全角冒号变成半角），再遮盖
    assert D.digit_template("统计时间范围：2026年9月1日至2026年9月30日") == "统计时间范围:{}年{}月{}日至{}年{}月{}日"
    assert D.digit_template("统计时间范围：2026年9月1日至9月30日") == "统计时间范围:{}年{}月{}日至{}月{}日"
    assert D.digit_template("月报导出_2026-08-01_2026-08-31.xlsx") == "月报导出_{}-{}-{}_{}-{}-{}.xlsx"
    # 全角数字先经 canon 变成 ASCII，再遮盖；连续的一段数字只算一个 {}
    assert D.digit_template("２０２６年９月") == "{}年{}月"
    assert D.digit_template("12345") == "{}"
    assert D.digit_template("第 3 号") != D.digit_template("第 3 号门")
    assert D.digit_template(None) == ""


# --------------------------------------------------------------------------
# 各 kind 一正一反
# --------------------------------------------------------------------------


def test_first_import_returns_empty():
    assert run(None, sep()) == []


def test_period_and_rows():
    diff = run(BASE, sep())
    p = next(d for d in diff if d.kind == "period")
    assert "2026-08-01 至 2026-08-31" in p.label and "2026-09-01 至 2026-09-30" in p.label
    r = next(d for d in diff if d.kind == "rows")
    assert "「日客流」31 → 30" in r.label and "「时段客流」527 → 510" in r.label
    assert "占位符「·」31 → 30 格" in r.detail
    assert not p.requires_confirm and not r.requires_confirm
    assert kinds(run(BASE, _d02())) == {"period"}      # 行数相同不出 rows


def test_rows_lists_added_and_removed_tables():
    cur = sep()
    cur[0]["receipt"]["tables"].pop()
    cur[0]["receipt"]["tables"].append({"name": "新表", "rows": 3, "columns": []})
    r = next(d for d in run(BASE, cur) if d.kind == "rows")
    assert "「新表」本期新增，3 行" in r.label and "「时段客流_表内合计」本期没有，上一期 93 行" in r.label


def test_placeholders_set_not_count():
    assert "placeholders" not in kinds(run(BASE, sep()))           # 31 → 30 只是个数
    cur = sep()
    cur[0]["receipt"]["placeholders"] = {"·": 30, "-": 2}
    item = next(d for d in run(BASE, cur) if d.kind == "placeholders")
    assert "新出现「-」" in item.label and not item.requires_confirm
    cur[0]["receipt"]["placeholders"] = {"-": 2, "·": 0}
    item = next(d for d in run(BASE, cur) if d.kind == "placeholders")
    assert "不再出现「·」" in item.label


def test_checks_set_not_count():
    assert "checks" not in kinds(run(BASE, sep()))                 # K1 93 → 90 只是核对数
    item = next(d for d in run(BASE, _d23()) if d.kind == "checks")
    assert "C2 通过 → 提示" in item.label
    item = next(d for d in run(BASE, _d07()) if d.kind == "checks")
    assert "G1 本期不再出现" in item.label


def test_file_name_template():
    assert "file_name" not in kinds(run(BASE, sep()))
    item = next(d for d in run(BASE, _d23()) if d.kind == "file_name")
    assert "9月客流.xlsx" in item.label and not item.requires_confirm
    # 目录不同不算，只看文件名本身
    assert "file_name" not in kinds(D.diff_reports(BASE[0], sep()[0], prev_file_name="a/" + BASE[1],
                                                   cur_file_name="b\\" + sep()[1], recipe=FLOW,
                                                   recipe_changed=False))
    assert "file_name" not in kinds(D.diff_reports(BASE[0], sep()[0], prev_file_name=None, cur_file_name="x.xlsx",
                                                   recipe=FLOW, recipe_changed=False))


def test_full_calc_whenever_set_this_period():
    # 规格 7.6：「这一期设了打开时重算」就出 info，不论上一期设没设
    item = next(d for d in run(BASE, _d25()) if d.kind == "full_calc")
    assert not item.requires_confirm and "上一期没有设置" in item.detail
    prev = (copy.deepcopy(BASE[0]), BASE[1])
    prev[0]["receipt"]["full_calc_on_load"] = True
    item = next(d for d in run(prev, _d25()) if d.kind == "full_calc")
    assert "上一期也设置了" in item.detail
    assert "full_calc" not in kinds(run(BASE, sep()))
    assert "full_calc" not in kinds(run(prev, sep()))             # 上一期设了、本期没设：不出


def test_context_text():
    item = next(d for d in run(BASE, _d12()) if d.kind == "context_text")
    assert "客流汇总!B2" in item.label and not item.requires_confirm
    cur = _d12()
    set_check(cur[0], "C1", "mismatch")
    assert "context_text" not in kinds(run(BASE, cur))
    assert "context_text" not in kinds(run(BASE, sep()))          # 只有日期变了


def test_context_source_added_d17_d27():
    for build in (_d17, _d27):
        diff = run(BASE, build())
        item = next(d for d in diff if d.kind == "context_source_added")
        assert not item.requires_confirm and "客流汇总!B3" in item.label
        assert not any(d.kind.startswith("outside_") for d in diff)


def test_context_source_added_needs_same_residue():
    # 附加文字不是上一期的标题：按区域外文字改了处理，要确认
    cur = sep()
    set_period_cell(cur[0], "B3", "客流汇总初步表（2026年9月）", annotated=True)
    diff = run(BASE, cur)
    assert "context_source_added" not in kinds(diff)
    item = next(d for d in diff if d.kind == "outside_changed")
    assert item.confirm_id == "diff:outside:客流汇总!B3"


def test_context_source_added_needs_c1_passed():
    cur = _d17()
    set_check(cur[0], "C1", "mismatch")
    diff = run(BASE, cur)
    assert "context_source_added" not in kinds(diff)
    assert "outside_changed" in kinds(diff)


def test_context_source_added_pure_period_cell():
    # B3 换成只是在说统计期的格：不在区域外文字里，C1 一致就是 context_source_added
    cur = sep()
    set_period_cell(cur[0], "B3", "2026年9月1日至2026年9月30日", annotated=False)
    diff = run(BASE, cur)
    assert "context_source_added" in kinds(diff) and "outside_removed" not in kinds(diff)
    set_check(cur[0], "C1", "mismatch")
    diff = run(BASE, cur)
    assert [d.confirm_id for d in diff if d.kind == "outside_changed"] == ["diff:outside:客流汇总!B3"]


def _list_doc(order: list[str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    """一张列表表（销售），lineage 里每列一段向下的溯源，列号按 order 排。"""
    doc = flow_doc(SEP, 30)
    headers = headers or {}
    cols = ["地区", "产品", "销量", "金额"]
    doc["receipt"]["tables"].append({
        "name": "销售", "sheet": "销售", "kind": "data", "grain": ["地区", "产品"], "rows": 4,
        "columns": [_col(c, "TEXT" if c in ("地区", "产品") else "INTEGER", "text" if c in ("地区", "产品") else "measure",
                         headers.get(c, c)) for c in cols]})
    doc["receipt"]["lineage"]["销售"] = {c: [[1, "销售", f"{chr(67 + order.index(c))}6", 4, "down"]] for c in cols}
    return doc


LIST_RECIPE: dict[str, Any] = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s2", "match": {"name": "销售"}, "blocks": [{
        "id": "列表1", "layout": "list", "table": "销售",
        "columns": [{"header": "地区", "name": "地区", "type": "TEXT"}, {"header": "产品", "name": "产品", "type": "TEXT"},
                    {"header": "销量", "name": "销量", "type": "INTEGER"}, {"header": "金额", "name": "金额", "type": "INTEGER"}],
    }]}],
    "tables": [{"name": "销售", "grain": ["地区", "产品"]}],
}


def test_column_order():
    prev = (_list_doc(["地区", "产品", "销量", "金额"]), "a.xlsx")
    cur = (_list_doc(["产品", "地区", "销量", "金额"]), "a.xlsx")
    item = next(d for d in run(prev, cur, recipe=LIST_RECIPE) if d.kind == "column_order")
    assert "「销售」" in item.label and not item.requires_confirm
    assert "column_order" not in kinds(run(prev, (_list_doc(["地区", "产品", "销量", "金额"]), "a.xlsx"),
                                           recipe=LIST_RECIPE))
    # 交叉表的表不比列顺序（它们的列来自配方，不是工作表上的位置）
    assert "column_order" not in kinds(run(BASE, _d19()))


def test_header_writing_keyed_by_block():
    prev = (_list_doc(["地区", "产品", "销量", "金额"]), "a.xlsx")
    cur = (_list_doc(["地区", "产品", "销量", "金额"], {"金额": "金 额", "地区": "地区　"}), "a.xlsx")
    item = next(d for d in run(prev, cur, recipe=LIST_RECIPE) if d.kind == "header_writing")
    assert item.requires_confirm and item.confirm_id == "diff:header_writing:列表1"
    assert "「金额」→「金 额」" in item.label
    assert "header_writing" not in kinds(run(prev, prev, recipe=LIST_RECIPE))
    # 配方里找不到写入这张表的列表块（传错了配方）：报错，不拿表名凑一个键
    with pytest.raises(ValueError, match="列表块"):
        run(prev, cur, recipe=FLOW)
    # 逐项比较时不给配方：报错
    with pytest.raises(ValueError, match="本期配方"):
        run(prev, cur, recipe=None)


def test_label_writing_and_row_order():
    diff = run(BASE, _d11())
    lw = {d.confirm_id for d in diff if d.kind == "label_writing"}
    assert lw == {"diff:label_writing:日间", "diff:label_writing:夜间"}
    assert "row_order" not in kinds(diff)
    item = next(d for d in diff if d.confirm_id == "diff:label_writing:日间")
    assert "「7-8」→「07:00-08:00」" in item.label
    diff = run(BASE, _d19())
    assert "label_writing" not in kinds(diff)
    assert [d.confirm_id for d in diff if d.kind == "row_order"] == ["diff:row_order:日客流"]


def test_label_writing_compares_raw_exactly():
    # en dash canon 之后与「-」相同，仍要报（E3）；只是行号变了不报
    assert "label_writing" in kinds(run(BASE, _d24()))
    assert "label_writing" not in kinds(run(BASE, _d14()))


def test_canon_values():
    prev = (_list_doc(["地区", "产品", "销量", "金额"]), "a.xlsx")
    prev[0]["receipt"]["canonicalized"] = [{"table": "销售", "column": "地区", "raw": "华 东", "canonical": "华 东",
                                            "count": 1, "first_cell": "销售!C7"}]
    cur = copy.deepcopy(prev)
    assert "canon_values" not in kinds(run(prev, cur, recipe=LIST_RECIPE))
    cur[0]["receipt"]["canonicalized"].append({"table": "销售", "column": "地区", "raw": "华北　",
                                               "canonical": "华北", "count": 2, "first_cell": "销售!C9"})
    item = next(d for d in run(prev, cur, recipe=LIST_RECIPE) if d.kind == "canon_values")
    assert item.confirm_id == "diff:canon:销售.地区" and item.requires_confirm
    # 标签的写法变化不进 canonicalized，所以 D11 没有 canon_values
    assert "canon_values" not in kinds(run(BASE, _d11()))


def test_block_order():
    diff = run(BASE, _d05())
    assert [d.confirm_id for d in diff if d.kind == "block_order"] == ["diff:block_order:客流汇总"]
    assert "block_order" not in kinds(run(BASE, _d14()))


def test_block_order_follows_renamed_sheet():
    cur = _d05()
    rec = cur[0]["receipt"]
    rec["block_order"] = {"汇总": rec["block_order"][SHEET]}
    rec["sheets"]["matched"] = {"s1": "汇总"}
    item = next(d for d in run(BASE, cur) if d.kind == "block_order")
    assert item.confirm_id == "diff:block_order:汇总"


def test_derived_and_axis_form():
    item = next(d for d in run(BASE, _d07()) if d.kind == "derived_form")
    assert item.confirm_id == "diff:derived_form:夜间合计" and "由公式改为写死的数" in item.label
    item = next(d for d in run(BASE, _d10()) if d.kind == "axis_form")
    assert item.confirm_id == "diff:axis_form:交叉表" and "由文字改为日期格" in item.label
    assert not kinds(run(BASE, sep())) & {"derived_form", "axis_form"}


def test_outside_added_removed_changed():
    cur = _d28()
    item = next(d for d in run(BASE, cur) if d.kind == "outside_added")
    assert item.confirm_id == "diff:outside:客流汇总!B32" and "注：9月15日" in item.label
    # 下一期同一句、只是日期变了：模板相同，不算改
    nxt = flow_doc(OCT, 31)
    add_outside(nxt, "B32", "注：10月3日闸机故障，当日客流为估算值")
    assert not kinds(run(cur, (nxt, file_name(OCT, 31)))) & {"outside_added", "outside_removed", "outside_changed"}
    # 写法变了：要确认
    add_outside(nxt, "B33", "x")
    nxt["receipt"]["outside_text"][-2]["text"] = "注：10月3日闸机检修，当日客流为估算值"
    diff = run(cur, (nxt, file_name(OCT, 31)))
    assert {d.confirm_id for d in diff if d.kind == "outside_changed"} == {"diff:outside:客流汇总!B32"}
    assert {d.confirm_id for d in diff if d.kind == "outside_added"} == {"diff:outside:客流汇总!B33"}
    # 删掉了
    item = next(d for d in run(cur, sep()) if d.kind == "outside_removed")
    assert item.confirm_id == "diff:outside:客流汇总!B32"


def test_outside_pairs_full_text_first():
    # 只是位置变了：info，不要求确认
    prev = (copy.deepcopy(BASE[0]), BASE[1])
    add_outside(prev[0], "B32", "注：本表为测试")
    cur = sep()
    add_outside(cur[0], "B33", "注：本表为测试")
    diff = run(prev, cur)
    moved = [d for d in diff if d.kind == "outside_moved"]
    assert len(moved) == 1 and not moved[0].requires_confirm
    assert not kinds(diff) & {"outside_added", "outside_removed", "outside_changed"}


def test_outside_full_coordinates_accepted():
    # OutsideText.cell 已经带工作表名时，确认项 id 一样
    cur = sep()
    cur[0]["receipt"]["outside_text"].append({"sheet": SHEET, "cell": f"{SHEET}!B32", "text": "注：9月15日",
                                              "kind": "text_digits", "period_source": False})
    item = next(d for d in run(BASE, cur) if d.kind == "outside_added")
    assert item.confirm_id == "diff:outside:客流汇总!B32"


def test_annotated_cell_monthly_same_sentence():
    # 上一期、本期同一格都是带附加文字的统计期格、句式相同：不出差异
    prev = sep()
    set_period_cell(prev[0], "B32", "注：2026年9月数据为初步统计，待修订", annotated=True)
    cur = (flow_doc(OCT, 31), file_name(OCT, 31))
    set_period_cell(cur[0], "B32", "注：2026年10月数据为初步统计，待修订", annotated=True)
    diff = run(prev, cur)
    assert not kinds(diff) & {"outside_added", "outside_removed", "outside_changed", "context_text"}
    set_period_cell(cur[0], "B32", "注：2026年10月数据为正式统计", annotated=True)
    assert "outside_changed" in kinds(run(prev, cur))


def test_annotated_cell_other_digits_changed():
    # AU-3：两期都是带附加文字的统计期格，统计期以外的数字变了（只含3个分区 → 只含2个分区）要出 outside_changed
    prev = sep()
    set_period_cell(prev[0], "B32", "注：2026年9月数据只含3个分区", annotated=True)
    cur = (flow_doc(OCT, 31), file_name(OCT, 31))
    set_period_cell(cur[0], "B32", "注：2026年10月数据只含2个分区", annotated=True)
    diff = run(prev, cur)
    assert [d.confirm_id for d in diff if d.kind == "outside_changed"] == ["diff:outside:客流汇总!B32"]
    set_period_cell(cur[0], "B32", "注：2026年10月数据只含3个分区", annotated=True)
    assert "outside_changed" not in kinds(run(prev, cur))


def test_same_period_sentence():
    assert D.same_period_sentence("注：2026年9月数据为初步统计", "注：2026年10月数据为初步统计")
    assert not D.same_period_sentence("注：2026年9月数据只含3个分区", "注：2026年10月数据只含2个分区")
    # 认不出统计期（没有或有两段）就不算同一句
    assert not D.same_period_sentence("注：数据只含3个分区", "注：数据只含4个分区")
    assert not D.same_period_sentence("2026年9月与2026年8月对比", "2026年10月与2026年9月对比")


def test_pure_period_cell_becomes_annotated():
    cur = sep()
    set_period_cell(cur[0], "B2", b2_text(SEP, dt.date(2026, 9, 30)) + "（初步）", annotated=True)
    diff = run(BASE, cur)
    assert [d.confirm_id for d in diff if d.kind == "outside_changed"] == ["diff:outside:客流汇总!B2"]
    assert "context_text" not in kinds(diff)


def test_ignored_columns():
    prev = (copy.deepcopy(BASE[0]), BASE[1])
    prev[0]["receipt"]["ignored_columns"] = {"列表1": ["备注"]}
    cur = sep()
    cur[0]["receipt"]["ignored_columns"] = {"列表1": ["备 注"]}
    assert "ignored_columns" not in kinds(run(prev, cur))         # 只是空白
    cur[0]["receipt"]["ignored_columns"] = {"列表1": ["备注", "经办人"]}
    item = next(d for d in run(prev, cur) if d.kind == "ignored_columns")
    assert item.confirm_id == "diff:ignored:列表1" and "「经办人」" in item.label


def test_hidden():
    prev = (copy.deepcopy(BASE[0]), BASE[1])
    prev[0]["receipt"]["hidden"] = {SHEET: {"rows": [4], "cols": [], "policy_rows": "include",
                                            "policy_cols": "reject_if_any"}}
    cur = sep()
    cur[0]["receipt"]["hidden"] = {SHEET: {"rows": [4], "cols": [], "policy_rows": "include",
                                           "policy_cols": "reject_if_any"}}
    assert "hidden" not in kinds(run(prev, cur))
    cur[0]["receipt"]["hidden"][SHEET]["rows"] = [4, 9]
    item = next(d for d in run(prev, cur) if d.kind == "hidden")
    assert item.confirm_id == "diff:hidden:客流汇总" and "第 4、9 行" in item.label
    # exclude 下被排除的行变了同样要确认（会改变导入的数据）
    for doc in (prev[0], cur[0]):
        doc["receipt"]["hidden"][SHEET]["policy_rows"] = "exclude"
    assert "hidden" in kinds(run(prev, cur))
    # 隐藏列（include）
    cur[0]["receipt"]["hidden"][SHEET].update(rows=[4], policy_cols="include", cols=[3])
    prev[0]["receipt"]["hidden"][SHEET].update(policy_cols="include", cols=[])
    item = next(d for d in run(prev, cur) if d.kind == "hidden")
    assert "C 列" in item.label


def test_limited_when_prev_is_simple_import():
    # 期 1 的 TableBuild.report：没有 ledger，只有表和行数
    prev = ({"receipt": {"tables": [{"name": "客流汇总", "rows": 29, "columns": []}]}, "checks": [], "period": None},
            "客流.xlsx")
    diff = run(prev, _d28())
    assert kinds(diff) == {"period", "rows"}
    assert all(not d.requires_confirm for d in diff)


def test_limited_when_recipe_changed():
    assert kinds(run(BASE, _d11(), recipe_changed=True)) == {"period", "rows"}
    prev = (dict(BASE[0], recipe_sha256="a" * 64), BASE[1])
    cur = _d11()
    assert kinds(run(prev, (dict(cur[0], recipe_sha256="b" * 64), cur[1]), recipe_changed=None)) == {"period", "rows"}
    assert "label_writing" in kinds(run(prev, (dict(cur[0], recipe_sha256="a" * 64), cur[1]), recipe_changed=None))


def test_recipe_sha_from_manifest_shape():
    # 导入清单（7.7）把配方哈希放在 recipe.sha256：直接拿清单当 prev 也认得
    manifest = dict(copy.deepcopy(BASE[0]), recipe={"id": "r1", "sha256": "a" * 64, "origin": "rules"})
    cur = _d11()
    changed = dict(cur[0], recipe={"sha256": "b" * 64})
    assert kinds(run((manifest, BASE[1]), (changed, cur[1]), recipe_changed=None)) == {"period", "rows"}
    same = dict(cur[0], recipe_sha256="a" * 64)
    assert "label_writing" in kinds(run((manifest, BASE[1]), (same, cur[1]), recipe_changed=None))


def test_recipe_changed_undecidable_raises():
    # 没传 recipe_changed、两边又没都带哈希：不默认「没变」去跨配方逐项比
    manifest = dict(copy.deepcopy(BASE[0]), recipe={"sha256": "a" * 64})
    with pytest.raises(ValueError, match="recipe_changed"):
        run((manifest, BASE[1]), _d11(), recipe_changed=None)
    with pytest.raises(ValueError, match="recipe_changed"):
        run(BASE, _d11(), recipe_changed=None)
    # 传了、两边哈希也都在，但互相矛盾
    cur = _d11()
    with pytest.raises(ValueError, match="不一致"):
        run((manifest, BASE[1]), (dict(cur[0], recipe_sha256="b" * 64), cur[1]), recipe_changed=False)
    with pytest.raises(ValueError, match="不一致"):
        run((manifest, BASE[1]), (dict(cur[0], recipe_sha256="a" * 64), cur[1]), recipe_changed=True)
    # 上一期是简单导入：只给 rows、period，用不着判断配方
    simple = ({"receipt": {"tables": [{"name": "客流汇总", "rows": 29, "columns": []}]}, "checks": [], "period": None},
              "客流.xlsx")
    assert kinds(run(simple, _d11(), recipe=None, recipe_changed=None)) == {"period", "rows"}


@pytest.mark.parametrize("key", D.RECEIPT_KEYS)
@pytest.mark.parametrize("side", ["prev", "cur"])
def test_missing_receipt_key_raises(side, key):
    # 早先缺键按空处理：prev 少了 axes 配 D10、少了 block_order 配 D05……需确认的项就静默消失了
    prev, cur = copy.deepcopy(BASE[0]), _d10()[0]
    del (prev if side == "prev" else cur)["receipt"][key]
    with pytest.raises(D.ReceiptIncomplete) as err:
        run((prev, BASE[1]), (cur, "a.xlsx"))
    assert err.value.side == side and err.value.missing == [key]


@pytest.mark.parametrize("side", ["prev", "cur"])
def test_missing_checks_or_receipt_raises(side):
    prev, cur = copy.deepcopy(BASE[0]), sep()[0]
    del (prev if side == "prev" else cur)["checks"]
    with pytest.raises(D.ReceiptIncomplete, match="checks"):
        run((prev, BASE[1]), (cur, "a.xlsx"))
    prev, cur = copy.deepcopy(BASE[0]), sep()[0]
    del (prev if side == "prev" else cur)["receipt"]
    with pytest.raises(D.ReceiptIncomplete, match="receipt"):
        run((prev, BASE[1]), (cur, "a.xlsx"))
    # 只给行数和统计期时（简单导入）回执里也得有 tables
    simple = {"receipt": {"warnings": []}, "checks": [], "period": None}
    with pytest.raises(D.ReceiptIncomplete, match="tables"):
        run((simple, "a.xlsx"), sep())


def test_truncated_canonicalized_raises():
    # TrialOut.receipt 的 canonicalized 只有前 50 条：拿它当 cur 会漏掉新写法
    cur = sep()
    cur[0]["receipt"]["canonicalized_total"] = 51
    cur[0]["receipt"]["canonicalized"] = [{"table": "销售", "column": "地区", "raw": f"华{i} ", "canonical": f"华{i}",
                                           "count": 1, "first_cell": "销售!C7"} for i in range(50)]
    with pytest.raises(D.ReceiptIncomplete, match="canonicalized_total"):
        run(BASE, cur)
    cur[0]["receipt"]["canonicalized_total"] = 50
    assert "canon_values" in kinds(run(BASE, cur))


def test_partial_receipt_raises():
    cur = sep()
    cur[0]["receipt"]["partial"] = True
    with pytest.raises(D.ReceiptIncomplete, match="partial"):
        run(BASE, cur)


def test_requires_confirm_first():
    cur = _d06()
    diff = run(BASE, cur)
    flags = [d.requires_confirm for d in diff]
    assert flags == sorted(flags, reverse=True) and flags[0] and not flags[-1]
    for d in diff:
        assert d.kind in D.DIFF_KINDS
        assert (d.confirm_id is not None) == d.requires_confirm
        if d.requires_confirm:
            assert d.confirm_id.startswith(D.CONFIRM_PREFIX[d.kind])


def test_accepts_dataclass_inputs():
    from app.data.recipe_types import ColumnOut, Extraction, PeriodOut, TableOut

    def doc(rows: int, start: str) -> dict[str, Any]:
        cols = [ColumnOut("日期", "TEXT", role="axis")]
        ex = Extraction(ok=True, tables=[TableOut("日客流", SHEET, "data", cols, ["日期"], rows)],
                        period=PeriodOut(start, start, "cells"), ledger=[])
        return {"receipt": ex, "checks": [], "period": ex.period}

    diff = D.diff_reports(doc(31, "2026-08-01"), doc(30, "2026-09-01"), prev_file_name="a", cur_file_name="a",
                          recipe=FLOW, recipe_changed=False)
    assert kinds(diff) == {"period", "rows"}


def test_period_missing_sides():
    cur = sep()
    cur[0]["period"] = None
    cur[0]["receipt"]["period"] = None
    item = next(d for d in run(BASE, cur) if d.kind == "period")
    assert "本期未能确定" in item.label
