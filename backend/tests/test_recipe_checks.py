"""核对模块（recipe_checks）与 engine.open_checked_sqlite 的测试。

不依赖执行器：库和 Extraction 都在测试里按「结构仿照客流表」的参考配方手写（假名、随机假数）。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from app.data import engine
from app.data.engine import SqliteHardeningError, open_checked_sqlite
from app.data.recipe_checks import (
    col_letters,
    column_null_counts,
    lineage_cell,
    plan_checks,
    run_checks,
    same_number,
)
from app.data.recipe_types import CheckResult, DerivedItem, Extraction, Recipe, derive_tables

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))
SHEET = "客流汇总"
DAY = [f"{h}-{h + 1}" for h in range(7, 18)]          # 第 10–20 行
NIGHT = [f"{h}-{h + 1}" for h in range(18, 24)]       # 第 22–27 行
TOTALS = [("18-22时合计", 18, 22, 28), ("22-24时合计", 22, 24, 29), ("18-24时合计", 18, 24, 30)]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def make_db(path: Path, recipe: Recipe, rows: dict[str, list[dict[str, Any]]], *, pk: bool = True) -> None:
    """按 derive_tables 建 STRICT 表、按给定顺序插入（rowid 就是插入序号）。"""
    tables, problems = derive_tables(recipe)
    assert problems == []
    grains = {t.name: t.grain for t in recipe.tables}
    conn = open_checked_sqlite(str(path), readonly=False)
    try:
        for table, cols in tables.items():
            defs = [f"{_q(c.name)} {c.type}" for c in cols]
            if pk and grains.get(table):
                defs.append(f"PRIMARY KEY ({', '.join(_q(g) for g in grains[table])})")
            conn.execute(f"CREATE TABLE {_q(table)} ({', '.join(defs)}) STRICT")
            names = [c.name for c in cols]
            conn.executemany(
                f"INSERT INTO {_q(table)} ({', '.join(_q(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
                [[r.get(n) for n in names] for r in rows.get(table, [])])
        conn.commit()
    finally:
        conn.close()


@dataclass
class Flow:
    recipe: Recipe
    db: str
    extraction: Extraction
    dates: list[str]
    rows: dict[str, list[dict[str, Any]]]


def flow(tmp_path: Path, days: int = 3, *, recipe_data: dict | None = None,
         rows_hook: Callable[[dict], None] | None = None,
         items_hook: Callable[[list[DerivedItem]], None] | None = None,
         pk: bool = True, seed: int = 7, full_calc: bool = False) -> Flow:
    rnd = random.Random(seed)
    recipe = Recipe.model_validate(deepcopy(recipe_data or FLOW))
    dates = [f"2026-08-{d:02d}" for d in range(1, days + 1)]
    daily = []
    for d in dates:
        a, b = rnd.randint(3000, 9000), rnd.randint(2000, 8000)
        daily.append({"日期": d, "全日客流": a + b, "分区甲": a, "分区乙": b})
    hourly = []
    for cat, labels in (("日间", DAY), ("夜间", NIGHT)):
        for label in labels:
            s, e = (int(x) for x in label.split("-"))
            for d in dates:
                v = None if label == "7-8" else rnd.randint(50, 900)
                hourly.append({"日期": d, "时段": label, "时段类别": cat, "起始小时": s, "结束小时": e, "客流": v})
    for row in daily:   # 各时段之和 ≠ 全日客流（生成器保证）
        tot = sum(h["客流"] or 0 for h in hourly if h["日期"] == row["日期"])
        if tot == row["全日客流"]:
            next(h for h in hourly if h["日期"] == row["日期"] and h["客流"] is not None)["客流"] += 1

    def night_sum(d: str, s: int, e: int, rows: list[dict]) -> int:
        return sum(h["客流"] for h in rows if h["日期"] == d and h["时段类别"] == "夜间"
                   and h["起始小时"] >= s and h["结束小时"] <= e)

    totals = [{"日期": d, "合计项": label, "起始小时": s, "结束小时": e, "客流": night_sum(d, s, e, hourly)}
              for label, s, e, _ in TOTALS for d in dates]
    original = deepcopy(hourly)
    rows = {"日客流": daily, "时段客流": hourly, "时段客流_表内合计": totals}
    if rows_hook:
        rows_hook(rows)
    db = tmp_path / "trial.db"
    make_db(db, recipe, rows, pk=pk)

    items: list[DerivedItem] = []
    for label, s, e, r in TOTALS:
        for i, d in enumerate(dates):
            refs = [n for n, h in enumerate(rows["时段客流"], 1) if h["日期"] == d and h["时段类别"] == "夜间"
                    and h["起始小时"] >= s and h["结束小时"] <= e]
            items.append(DerivedItem(
                kind="label_range_sum", segment="夜间合计", sheet=SHEET, cell=f"{col_letters(3 + i)}{r}",
                label_raw=label.replace("时", " 时"), label=label, value=night_sum(d, s, e, original),
                is_formula=True, formula=f"=SUM({col_letters(3 + i)}22:{col_letters(3 + i)}25)",
                base_table="时段客流", base_value="客流", start=s, end=e,
                key={"日期": d, "时段类别": "夜间"}, start_col="起始小时", end_col="结束小时",
                ref_rowids=refs, func="SUM", keep_table="时段客流_表内合计"))
    if items_hook:
        items_hook(items)
    lineage = {
        "日客流": {c: [[1, SHEET, f"C{r}", days, "right"]] for c, r in (("全日客流", 5), ("分区甲", 6), ("分区乙", 7))},
        "时段客流": {"客流": [[1 + j * days, SHEET, f"C{r}", days, "right"]
                          for j, r in enumerate([*range(10, 21), *range(22, 28)])]},
    }
    ctx = [CheckResult("C1", "context_agree", "统计期", "passed", "data_quality", checked=1),
           CheckResult("C2", "filename_period", "文件名", "passed", "data_quality", checked=1)]
    ex = Extraction(ok=True, derived=items, lineage=lineage, context_checks=ctx, full_calc_on_load=full_calc,
                    expected_rows={t: len(v) for t, v in rows.items()})
    return Flow(recipe, str(db), ex, dates, rows)


def by_id(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {c.id: c for c in results}


# ---------------------------------------------------------------- open_checked_sqlite


def test_open_checked_sqlite_readonly_and_dqs_off(tmp_path):
    path = tmp_path / "a b?#中文.db"
    conn = open_checked_sqlite(str(path), readonly=False)
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    # 默认回滚日志，不开 WAL（WAL 改写文件头，库哈希会变）
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    assert path.exists() and sorted(p.name for p in tmp_path.iterdir()) == [path.name]

    ro = open_checked_sqlite(str(path))
    try:
        assert ro.execute("SELECT a FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO t VALUES (2)")
        # DQS 关了：写错的双引号列名报错，而不是当成字符串
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            ro.execute('SELECT "no_such" FROM t')
    finally:
        ro.close()


def test_open_checked_sqlite_readonly_does_not_create(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        open_checked_sqlite(str(tmp_path / "missing.db"))
    assert not (tmp_path / "missing.db").exists()


def test_open_checked_sqlite_refuses_when_dqs_cannot_be_disabled(tmp_path, monkeypatch):
    def broken(conn):
        raise SqliteHardeningError("关不掉")

    monkeypatch.setattr(engine, "_disable_dqs", broken)
    with pytest.raises(SqliteHardeningError):
        open_checked_sqlite(str(tmp_path / "x.db"), readonly=False)


# ---------------------------------------------------------------- 小工具


def test_same_number_rules():
    assert same_number(100, 100.0)
    assert not same_number(100, 100.0000001 + 1)
    assert same_number(0.3, 0.1 + 0.2)          # 非整数值按相对容差
    assert not same_number(5, 6.0)
    assert not same_number(None, 0.0)


def test_lineage_cell_reverse_lookup():
    lin = {"t": {"v": [[1, "表一", "C5", 31, "right"], [32, "表一", "B10", 4, "down"]]}}
    assert lineage_cell(lin, "t", "v", 1) == "表一!C5"
    assert lineage_cell(lin, "t", "v", 10) == "表一!L5"
    assert lineage_cell(lin, "t", "v", 33) == "表一!B11"
    assert lineage_cell(lin, "t", "v", 99) is None
    assert col_letters(27) == "AA" and col_letters(33) == "AG"


# ---------------------------------------------------------------- 全部通过（D00 的形状）


def test_reference_flow_all_checks(tmp_path):
    f = flow(tmp_path, days=5)
    before = hashlib.sha256(Path(f.db).read_bytes()).hexdigest()
    results = run_checks(f.db, f.extraction, f.recipe)
    assert [c.id for c in results] == ["C1", "C2", "K1", "G1", "R1", "R2", "N1", "N2", "N3", "P1", "P2", "P3"]
    got = by_id(results)
    assert got["K1"].status == "passed" and got["K1"].checked == 15 and got["K1"].reasons == {}
    assert got["K1"].kind == "derived_sum" and got["K1"].acceptable is False
    assert got["G1"].status == "passed" and got["G1"].checked == 15
    assert got["R1"].status == "passed" and got["R1"].checked == 5 and got["R1"].category == "data_quality"
    assert got["R2"].status == "info" and got["R2"].details == ["各时段之和与全日客流：5 天中 0 天相等"]
    assert got["R2"].checked == 5 and got["R2"].failed == 5
    assert all(got[k].status == "passed" for k in ("N1", "N2", "N3", "P1", "P2", "P3"))
    # 只读打开：核对不改库
    assert hashlib.sha256(Path(f.db).read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trial.db"]


def test_sql_text_and_params_recorded(tmp_path):
    f = flow(tmp_path)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    k = got["K1"]
    assert k.sql == ('SELECT TOTAL("客流"), COUNT("客流"), COUNT(*) FROM "时段客流" '
                     'WHERE "日期" = ? AND "时段类别" = ? AND "起始小时" >= ? AND "结束小时" <= ?')
    assert k.params == ["2026-08-01", "夜间", 18, 22]
    assert '"全日客流" <> "分区甲" + "分区乙"' in got["R1"].sql
    assert got["N1"].sql == 'SELECT COUNT(*) FROM "日客流"'
    assert 'GROUP BY "日期", "时段"' in got["P2"].sql
    assert got["G1"].sql.startswith('SELECT rowid FROM "时段客流" WHERE "日期" = ?')
    # 重放：同一条 SQL、同样的参数，在同一个库上得到同一个数
    conn = sqlite3.connect(f.db)
    assert conn.execute(k.sql, k.params).fetchone()[0] == f.extraction.derived[0].value
    conn.close()


def test_full_calc_on_load_consistent_passes_with_note(tmp_path):
    f = flow(tmp_path, full_calc=True)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "passed"
    assert "工作簿设置了打开时重算，保存值与重算一致" in k.details


# ---------------------------------------------------------------- K：各种结果


def test_k_mismatch_points_at_cell(tmp_path):
    def bump(items):
        items[4].value += 100      # 「18-22 时合计」第 5 个日期（G28）

    f = flow(tmp_path, days=5, items_hook=bump)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "mismatch" and k.category == "structure" and k.acceptable is False
    assert k.failed == 1 and k.checked == 15
    item = f.extraction.derived[4]
    assert k.details[0] == f"G28：表内为 {item.value}，按 18-22 时的明细重算为 {item.value - 100}"
    assert k.cells == [f"{SHEET}!G28"]
    assert k.params == ["2026-08-05", "夜间", 18, 22]   # 代表性 SQL 取第一处不一致


@pytest.mark.parametrize("slot,ranges", [
    ("19-20", "18-22 时、18-24 时"),      # 中间缺一段
    ("18-19", "18-22 时、18-24 时"),      # 缺起点
    ("23-24", "22-24 时、18-24 时"),      # 缺终点
])
def test_k_tiling_unverifiable(tmp_path, slot, ranges):
    def drop(rows):
        rows["时段客流"] = [h for h in rows["时段客流"] if h["时段"] != slot]

    f = flow(tmp_path, rows_hook=drop)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.acceptable is True
    assert k.reasons == {"tiling": 6}          # 两个合计各 3 个日期；另一个合计照常一致
    assert k.checked == 3 and k.failed == 0 and k.unverifiable == 6
    assert f"6 格无法核对：明细时段没有恰好铺满 {ranges}" in k.details


def test_k_overlap_is_not_tiling(tmp_path):
    """区间里多出一条跨时段的明细（18-20）：首尾相接不成立，不能判一致。"""
    data = deepcopy(FLOW)
    data["tables"][1]["grain"] = ["日期", "时段类别", "时段"]

    def extra(rows):
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            rows["时段客流"].append({"日期": d, "时段": "18-20", "时段类别": "夜间", "起始小时": 18,
                                 "结束小时": 20, "客流": 0})

    f = flow(tmp_path, recipe_data=data, rows_hook=extra)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.reasons == {"tiling": 6}


@pytest.mark.parametrize("is_formula,reason", [(True, "uncached"), (False, "blank")])
def test_k_missing_value(tmp_path, is_formula, reason):
    def blank(items):
        for it in items:
            it.value = None
            it.is_formula = is_formula

    f = flow(tmp_path, items_hook=blank)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.reasons == {reason: 9} and k.acceptable
    assert k.checked == 0 and k.unverifiable == 9


def test_k_null_detail_never_passes(tmp_path):
    """明细含空值（占位符）：哪怕保存的合计恰好等于非空明细之和，也只能判无法核对。"""
    removed: dict[str, int] = {}

    def hole(rows):
        for h in rows["时段客流"]:
            if h["时段"] == "23-24" and h["日期"] == "2026-08-02":
                removed["v"], h["客流"] = h["客流"], None

    def match_nonnull(items):
        for it in items:
            if it.key["日期"] == "2026-08-02" and it.end == 24:
                it.value -= removed["v"]      # 合计 = 非空明细之和：TOTAL 对得上，仍不能判一致

    f = flow(tmp_path, rows_hook=hole, items_hook=match_nonnull)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.reasons == {"null_detail": 2}   # 22-24、18-24 那天
    assert k.failed == 0 and k.checked == 7
    assert "2 格无法核对：明细含无数据占位符" in k.details


@pytest.mark.parametrize("mark", ["hidden_in_range", "SUBTOTAL", "AGGREGATE"])
def test_k_hidden_turns_mismatch_into_unverifiable(tmp_path, mark):
    def tweak(items):
        it = items[0]
        it.value += 5
        if mark == "hidden_in_range":
            it.hidden_in_range = True
        else:
            it.func = mark

    f = flow(tmp_path, items_hook=tweak)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.reasons == {"hidden": 1} and k.acceptable


def test_k_hidden_but_equal_is_consistent(tmp_path):
    def tweak(items):
        for it in items:
            it.hidden_in_range = True

    f = flow(tmp_path, items_hook=tweak)
    assert by_id(run_checks(f.db, f.extraction, f.recipe))["K1"].status == "passed"


def test_k_key_with_const_column_ignores_other_category(tmp_path):
    """同一张表里别的分段（别的类别）也有 18-22 时的行：key 带常量列，不会被加进来。"""
    data = deepcopy(FLOW)
    data["tables"][1]["grain"] = ["日期", "时段类别", "时段"]

    def other(rows):
        # 别的类别按两小时一段：和夜间的逐小时混在一起就互相重叠、也会被加进合计
        for d in ("2026-08-01", "2026-08-02", "2026-08-03"):
            for s in (18, 20, 22):
                rows["时段客流"].append({"日期": d, "时段": f"{s}-{s + 2}", "时段类别": "其他", "起始小时": s,
                                     "结束小时": s + 2, "客流": 999})

    f = flow(tmp_path, recipe_data=data, rows_hook=other)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    assert got["K1"].status == "passed" and got["G1"].status == "passed"


def test_k_day_without_detail_rows_is_unverifiable(tmp_path):
    """某个日期的夜间明细一行都没有（别的日期都有，所以按区间判的「铺满」照样成立）：
    COUNT(*) = 0，TOTAL 是 0，不能拿它和保存的合计比，判无法核对（tiling），不判一致也不判不一致。"""
    def drop_day(rows):
        rows["时段客流"] = [h for h in rows["时段客流"]
                         if not (h["日期"] == "2026-08-02" and h["时段类别"] == "夜间")]

    f = flow(tmp_path, rows_hook=drop_day)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable" and k.acceptable
    assert k.reasons == {"tiling": 3} and k.checked == 6 and k.failed == 0
    assert sorted(k.cells) == [f"{SHEET}!D28", f"{SHEET}!D29", f"{SHEET}!D30"]


def test_k_mixed_reasons_counted(tmp_path):
    def mix(items):
        items[0].value = None              # 18-22 第 1 天：无缓存
        items[1].value += 1
        items[1].func = "SUBTOTAL"         # 18-22 第 2 天：分类汇总
        items[3].value = None
        items[3].is_formula = False        # 22-24 第 1 天：空格

    f = flow(tmp_path, items_hook=mix)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "unverifiable"
    assert k.reasons == {"uncached": 1, "hidden": 1, "blank": 1}
    assert k.unverifiable == 3 and k.checked == 6
    assert any(d.startswith("1 格无法核对：文件未经计算保存") for d in k.details)


def test_k_mismatch_wins_over_unverifiable(tmp_path):
    def mix(items):
        items[0].value = None
        items[2].value += 3

    f = flow(tmp_path, items_hook=mix)
    k = by_id(run_checks(f.db, f.extraction, f.recipe))["K1"]
    assert k.status == "mismatch" and k.acceptable is False and k.reasons == {"uncached": 1}


def test_k_sql_error_is_structure_mismatch_not_crash(tmp_path):
    """列名写错：DQS 关闭的连接直接报错，核对判结构类不一致，而不是把「客 流」当字符串算出 0。"""
    def typo(items):
        for it in items:
            it.base_value = "客 流"

    f = flow(tmp_path, items_hook=typo)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    k = got["K1"]
    assert k.status == "mismatch" and k.category == "structure" and not k.acceptable
    assert k.details[0].startswith("核对 SQL 执行失败") and "no such column" in k.details[0]
    assert k.sql and '"客 流"' in k.sql
    assert got["R1"].status == "passed"       # 别的核对照常


# ---------------------------------------------------------------- G：公式引用


def test_g_p9_label_and_refs_disagree(tmp_path):
    def p9(items):
        it = items[0]                       # C28 改成 =SUM(C22:C24)：只到 21 时
        it.ref_rowids = it.ref_rowids[:3]

    f = flow(tmp_path, items_hook=p9)
    g = by_id(run_checks(f.db, f.extraction, f.recipe))["G1"]
    assert g.status == "mismatch" and g.category == "structure"
    assert g.details[0] == "C28：标签「18-22时合计」与公式引用的时段不一致"
    assert g.cells == [f"{SHEET}!C28"]


@pytest.mark.parametrize("extra_slot,category", [
    ("22-23", "夜间"),      # 同一类别里范围外的一行：「18-22 时合计」把 22-23 时也引用进来
    ("17-18", "日间"),      # 别的类别的一行
])
def test_g_refs_superset_of_label_range_is_mismatch(tmp_path, extra_slot, category):
    """公式引用比标签范围多出一行（如「8-12时合计」=SUM(C10:C14) 把整行占位符的 7-8 时也引进来）：
    K 按标签区间重算照样一致，能拦住的只有 G 的集合相等——「引用 ⊆ 标签范围」不够，必须相等。"""
    seen: dict[str, list[dict]] = {}

    def keep(rows):
        seen["rows"] = rows["时段客流"]

    def widen(items):
        it = items[0]                       # C28「18-22时合计」第 1 个日期
        extra = next(n for n, h in enumerate(seen["rows"], 1)
                     if h["日期"] == it.key["日期"] and h["时段"] == extra_slot and h["时段类别"] == category)
        it.ref_rowids = [*it.ref_rowids, extra]

    f = flow(tmp_path, rows_hook=keep, items_hook=widen)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    g = got["G1"]
    assert g.status == "mismatch" and g.category == "structure" and not g.acceptable
    assert g.failed == 1 and g.cells == [f"{SHEET}!C28"]
    assert g.details[0] == "C28：标签「18-22时合计」与公式引用的时段不一致"
    assert got["K1"].status == "passed"


@pytest.mark.parametrize("problem,status,reason", [
    ("unrecognized", "unverifiable", "unrecognized"), ("hidden", "unverifiable", "hidden"),
    ("outside", "mismatch", None),
])
def test_g_ref_problems(tmp_path, problem, status, reason):
    def mark(items):
        items[0].ref_problem = problem
        items[0].ref_rowids = None

    f = flow(tmp_path, items_hook=mark)
    g = by_id(run_checks(f.db, f.extraction, f.recipe))["G1"]
    assert g.status == status
    assert g.acceptable is (status == "unverifiable")
    assert g.reasons == ({reason: 1} if reason else {})


def test_g_absent_when_no_formula(tmp_path):
    def literal(items):
        for it in items:
            it.is_formula, it.formula, it.func, it.ref_rowids = False, None, None, None

    f = flow(tmp_path, items_hook=literal)
    ids = [c.id for c in run_checks(f.db, f.extraction, f.recipe)]
    assert "K1" in ids and "G1" not in ids


def test_uncached_formula_still_checks_refs(tmp_path):
    """P1b：公式无缓存——K1 无法核对（uncached），G1 照常核对引用并通过。"""
    def p1b(items):
        for it in items:
            it.value = None

    f = flow(tmp_path, items_hook=p1b)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    assert got["K1"].status == "unverifiable" and got["K1"].reasons == {"uncached": 9}
    assert got["G1"].status == "passed"


# ---------------------------------------------------------------- T：列表合计


LIST_RECIPE = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s1", "match": {"name": "销售"}, "blocks": [{
        "id": "明细", "layout": "list", "table": "销售",
        "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                    {"header": "金额", "name": "金额", "type": "INTEGER"},
                    {"header": "数量", "name": "数量", "type": "REAL"}],
        "rows": {"total_row": {"label_column": "地区", "pick": "合计", "keep_as": "销售_表内合计"}}}]}],
    "tables": [{"name": "销售", "grain": ["地区"]}, {"name": "销售_表内合计", "kind": "reported_total"}],
}


def list_case(tmp_path, *, hook=None, null_amount=False, hidden=False):
    recipe = Recipe.model_validate(deepcopy(LIST_RECIPE))
    sales = [{"地区": f"分区{n}", "金额": 100 * i, "数量": 0.1 * i} for i, n in enumerate("甲乙丙", 1)]
    if null_amount:
        sales[1]["金额"] = None
    totals = [{"合计项": "合计", "金额": 600, "数量": 0.6}]
    make_db(tmp_path / "list.db", recipe, {"销售": sales, "销售_表内合计": totals})
    items = [DerivedItem(kind="column_sum", segment="明细", sheet="销售", cell=f"{c}5", label_raw="合计",
                         label="合计", value=v, is_formula=False, formula=None, base_table="销售",
                         base_value=col, rowid_first=1, rowid_last=3, hidden_in_range=hidden,
                         keep_table="销售_表内合计")
             for c, col, v in (("B", "金额", 600), ("C", "数量", 0.6))]
    if hook:
        hook(items)
    ex = Extraction(ok=True, derived=items, expected_rows={"销售": 3, "销售_表内合计": 1})
    return recipe, str(tmp_path / "list.db"), ex


def test_t_passed_with_real_tolerance(tmp_path):
    recipe, db, ex = list_case(tmp_path)
    got = by_id(run_checks(db, ex, recipe))
    assert [c.id for c in run_checks(db, ex, recipe)] == ["T1", "N1", "N2", "P1"]
    t = got["T1"]
    assert t.kind == "column_sum" and t.status == "passed" and t.checked == 2    # 0.1+0.2+0.3 ≈ 0.6
    assert t.sql == 'SELECT TOTAL("金额"), COUNT("金额"), COUNT(*) FROM "销售" WHERE rowid BETWEEN ? AND ?'
    assert t.params == [1, 3]


def test_t_mismatch(tmp_path):
    def bump(items):
        items[0].value = 601

    recipe, db, ex = list_case(tmp_path, hook=bump)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "mismatch" and t.details[0] == "B5：表内为 601，按明细求和为 600"


def test_t_uncached(tmp_path):
    def unc(items):
        items[0].value, items[0].is_formula, items[0].formula = None, True, "=SUM(B2:B4)"

    recipe, db, ex = list_case(tmp_path, hook=unc)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "unverifiable" and t.reasons == {"uncached": 1} and t.acceptable


def test_t_null_detail(tmp_path):
    recipe, db, ex = list_case(tmp_path, null_amount=True)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "unverifiable" and t.reasons == {"null_detail": 1}
    assert "1 格无法核对：明细含空值" in t.details


def test_t_hidden_rows_turn_mismatch_unverifiable(tmp_path):
    def bump(items):
        items[0].value = 500

    recipe, db, ex = list_case(tmp_path, hook=bump, hidden=True)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "unverifiable" and t.reasons == {"hidden": 1}


def test_t_without_detail_rows_is_unverifiable(tmp_path):
    """表头下面紧接着就是合计行：执行器给的明细区间两端都是 None。不能拿 BETWEEN NULL AND NULL 算出的 0
    去比（合计写 0 时会被判「2 格全部一致」），判无法核对。"""
    def empty(items):
        for it in items:
            it.rowid_first = it.rowid_last = None
            it.value = 0

    recipe, db, ex = list_case(tmp_path, hook=empty)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "unverifiable" and t.acceptable and t.category == "structure"
    assert t.reasons == {"tiling": 2} and t.checked == 0
    assert "2 格无法核对：合计行之上没有明细行" in t.details
    assert all("一致" not in d for d in t.details)
    assert t.params == [None, None]


def test_t_empty_rowid_range_is_unverifiable(tmp_path):
    def far(items):
        for it in items:
            it.rowid_first, it.rowid_last = 10, 12

    recipe, db, ex = list_case(tmp_path, hook=far)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "unverifiable" and t.reasons == {"tiling": 2}
    assert t.params == [10, 12]


@pytest.mark.parametrize("first,last", [(None, 3), (1, None)])
def test_t_half_open_rowid_range_is_structure_failure(tmp_path, first, last):
    """区间只有一端：执行器的错，和 K 缺区间信息一样判结构类不一致，不硬算。"""
    def half(items):
        items[0].rowid_first, items[0].rowid_last = first, last

    recipe, db, ex = list_case(tmp_path, hook=half)
    t = by_id(run_checks(db, ex, recipe))["T1"]
    assert t.status == "mismatch" and t.category == "structure" and not t.acceptable
    assert t.details == ["B5：合计行对应的明细行区间只有一端"]


# ---------------------------------------------------------------- R：关系


def test_r_sum_eq_mismatch_located_by_lineage(tmp_path):
    def seven(rows):
        rows["日客流"][1]["全日客流"] += 7      # 第 2 个日期

    f = flow(tmp_path, rows_hook=seven)
    r1 = by_id(run_checks(f.db, f.extraction, f.recipe))["R1"]
    assert r1.status == "mismatch" and r1.category == "data_quality" and r1.acceptable
    assert r1.checked == 3 and r1.failed == 1
    assert r1.cells == [f"{SHEET}!D5"]
    row = f.rows["日客流"][1]
    assert r1.details[0] == (f"日期 2026-08-02（D5）：全日客流 为 {row['全日客流']}，"
                             f"分区甲、分区乙 之和为 {row['全日客流'] - 7}")


def test_r_sum_eq_nulls_unverifiable(tmp_path):
    def hole(rows):
        rows["日客流"][0]["分区甲"] = None

    f = flow(tmp_path, rows_hook=hole)
    r1 = by_id(run_checks(f.db, f.extraction, f.recipe))["R1"]
    assert r1.status == "unverifiable" and r1.acceptable and r1.category == "data_quality"
    assert r1.checked == 2 and r1.unverifiable == 1 and r1.failed == 0


def test_r_sum_eq_no_rows_is_unverifiable_not_passed(tmp_path):
    """AU-1：表里一行都没有（只有表头）：一行都没核对过，不能判通过，也不能写「0 行全部成立」。"""
    def empty(rows):
        rows["日客流"].clear()

    f = flow(tmp_path, rows_hook=empty)
    r1 = by_id(run_checks(f.db, f.extraction, f.recipe))["R1"]
    assert r1.status == "unverifiable" and r1.acceptable and r1.category == "data_quality"
    assert r1.checked == 0 and r1.unverifiable == 0 and r1.failed == 0
    assert r1.details == ["本期这张表没有数据行，关系未能核对"]


def test_r_sum_eq_real_uses_tolerance(tmp_path):
    data = deepcopy(FLOW)
    data["sheets"][0]["blocks"][0]["values"]["type"] = "REAL"

    def floats(rows):
        for r in rows["日客流"]:
            r["分区甲"], r["分区乙"], r["全日客流"] = 0.1, 0.2, 0.3
        for t in ("时段客流", "时段客流_表内合计"):
            for r in rows[t]:
                r["客流"] = None if r["客流"] is None else float(r["客流"])

    f = flow(tmp_path, recipe_data=data, rows_hook=floats)
    r1 = by_id(run_checks(f.db, f.extraction, f.recipe))["R1"]
    assert r1.status == "passed", r1.details
    assert "ABS(" in r1.sql
    # 同样的数按整数写法（<>）比，就会误判不成立
    conn = sqlite3.connect(f.db)
    assert conn.execute('SELECT COUNT(*) FROM "日客流" WHERE "全日客流" <> "分区甲" + "分区乙"').fetchone()[0] == 3
    conn.close()


def test_r_not_comparable_all_equal(tmp_path):
    def equalize(rows):
        for r in rows["日客流"]:
            r["全日客流"] = sum(h["客流"] or 0 for h in rows["时段客流"] if h["日期"] == r["日期"])
            r["分区甲"] = r["全日客流"] - r["分区乙"]

    f = flow(tmp_path, rows_hook=equalize)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    r2 = got["R2"]
    assert r2.status == "info" and r2.category == "info"
    assert r2.details == ["各时段之和与全日客流：3 天全部相等"]
    assert r2.checked == 3 and r2.failed == 0
    assert got["R1"].status == "passed"


def test_r_not_comparable_partly_equal_stays_info(tmp_path):
    def one(rows):
        r = rows["日客流"][0]
        r["全日客流"] = sum(h["客流"] or 0 for h in rows["时段客流"] if h["日期"] == r["日期"])

    f = flow(tmp_path, rows_hook=one)
    r2 = by_id(run_checks(f.db, f.extraction, f.recipe))["R2"]
    assert r2.status == "info" and r2.details == ["各时段之和与全日客流：3 天中 1 天相等"] and r2.failed == 2


# ---------------------------------------------------------------- N、P


def test_n_row_count_mismatch(tmp_path):
    f = flow(tmp_path)
    f.extraction.expected_rows["时段客流"] += 1
    n2 = by_id(run_checks(f.db, f.extraction, f.recipe))["N2"]
    assert n2.status == "mismatch" and n2.category == "structure"
    assert n2.details == ["库里 51 行，按格子账应有 52 行"]


def test_p_duplicate_key(tmp_path):
    def dup(rows):
        rows["日客流"].append(dict(rows["日客流"][0]))

    f = flow(tmp_path, rows_hook=dup, pk=False)
    got = by_id(run_checks(f.db, f.extraction, f.recipe))
    assert got["P1"].status == "mismatch" and got["P1"].details == ["1 组主键重复"]
    assert got["P2"].status == "passed"


# ---------------------------------------------------------------- 编号与空值统计


def test_plan_numbering_follows_recipe_order(tmp_path):
    f = flow(tmp_path)
    plan = plan_checks(f.recipe, f.extraction)
    assert plan.k == {"夜间合计": "K1"} and plan.g == {"夜间合计": "G1"} and plan.t == {}
    assert len(plan.items["夜间合计"]) == 9


def test_column_null_counts(tmp_path):
    f = flow(tmp_path)
    counts = column_null_counts(f.db, f.recipe)
    assert counts["时段客流"]["客流"] == 3              # 7-8 那一行整期是占位符
    assert counts["日客流"] == {"日期": 0, "全日客流": 0, "分区甲": 0, "分区乙": 0}


# ---------------------------------------------------------------- 核对结果 → 说明（同一套编号）


def test_checks_feed_notes_end_to_end(tmp_path):
    from app.data.recipe_notes import build_notes
    from app.data.recipe_types import Acceptance, AxisOut, SheetsOut

    def p1b(items):
        for it in items:
            it.value = None

    f = flow(tmp_path, items_hook=p1b)
    f.extraction.placeholders = {"·": 3}
    f.extraction.sheets = SheetsOut(matched={"s1": SHEET})
    f.extraction.axes = [AxisOut("交叉表", 4, "8月1日", "8月3日", 3, "text")]
    checks = run_checks(f.db, f.extraction, f.recipe)
    counts = column_null_counts(f.db, f.recipe)
    pending = build_notes(f.recipe, f.extraction, checks, [], null_counts=counts)
    assert pending.problems == []          # 未接受的「无法核对」只影响状态，不让说明生成失败
    assert by_id(checks)["K1"].acceptable
    notes = build_notes(f.recipe, f.extraction, checks, [Acceptance("K1", "原表未经计算保存")], null_counts=counts)
    assert notes.problems == []
    total = notes.tables["时段客流_表内合计"].comment
    assert "本期原表未保存计算结果，合计值为空，未能核对" in total and "一致" not in total
    assert "已逐日核对" in notes.tables["日客流"].comment
    assert notes.tables["日客流"].columns["全日客流"] == "单位：人次"
    assert "无数据占位符" in notes.tables["时段客流"].columns["客流"]


def test_structure_mismatch_feeds_notes_without_problem(tmp_path):
    """P9：K1、G1 都不一致，试运行因结构类核对拒收。说明不写合计那一句、也不额外记「说明没有对应模板」，
    免得被当成配方问题显示。"""
    from app.data.recipe_notes import build_notes
    from app.data.recipe_types import SheetsOut

    def p9(items):
        it = items[0]
        it.ref_rowids = it.ref_rowids[:3]
        it.value += 11

    f = flow(tmp_path, items_hook=p9)
    f.extraction.sheets = SheetsOut(matched={"s1": SHEET})
    checks = by_id(run_checks(f.db, f.extraction, f.recipe))
    assert checks["K1"].status == "mismatch" and checks["G1"].status == "mismatch"
    notes = build_notes(f.recipe, f.extraction, list(checks.values()), [],
                        null_counts=column_null_counts(f.db, f.recipe))
    assert notes.problems == []
    total = notes.tables["时段客流_表内合计"].comment
    assert "原表写明的合计" not in total and "一致" not in total


def _list_notes_from_db(tmp_path, hook):
    from app.data.recipe_notes import build_notes
    from app.data.recipe_types import Acceptance

    recipe, db, ex = list_case(tmp_path, hook=hook)
    checks = run_checks(db, ex, recipe)
    acc = [Acceptance(c.id, "已与业务方核实") for c in checks if c.acceptable]
    return by_id(checks), build_notes(recipe, ex, checks, acc, null_counts=column_null_counts(db, recipe))


def test_list_total_placeholder_cell_gets_blank_template(tmp_path):
    """合计行的 金额 格写了已声明的占位符：非空格照样生成合计格（P2-SPEC 4.4），值转成空值（4.5），
    T 判无法核对（blank）。说明要有对应的模板，不能记「没有对应的说明」把合法文件当配方问题拒收。"""
    def placeholder(items):
        items[0].value = None

    checks, notes = _list_notes_from_db(tmp_path, placeholder)
    assert checks["T1"].status == "unverifiable" and checks["T1"].reasons == {"blank": 1}
    assert notes.problems == []
    total = notes.tables["销售_表内合计"].comment
    assert total == "原表合计行的值。本期原表合计格为空，未能核对：不要与 销售 相加"
    assert "一致" not in total


def test_list_total_blank_cell_not_claimed_as_checked(tmp_path):
    """合计行的 数量 格是空的：不生成合计格，T 只核对 金额 并通过。说明不能写「各列都核对一致」。"""
    def drop(items):
        del items[1]

    checks, notes = _list_notes_from_db(tmp_path, drop)
    assert checks["T1"].status == "passed" and checks["T1"].checked == 1
    assert notes.problems == []
    assert notes.tables["销售_表内合计"].comment == (
        "原表合计行的值。本期原表 数量 的合计格为空，其余各列已按明细求和核对一致：不要与 销售 相加")
