"""确认项（recipe_confirm）的测试：7.5 每行一正一反、AI 类、破坏性变更、切换、本期类。

回执和配方都是手写的（假名、假数），构造器在 test_recipe_diff 里；不依赖执行器、核对、静态校验（各包的桩
都不需要：confirm_items 只读回执和配方）。
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from app.data.recipe_confirm import (
    EXTRACTION_KEYS, ConfirmContext, breaking_changes, confirm_items, recipe_differs, switch_changes, table_changes,
)
from app.data.recipe_diff import ReceiptIncomplete, diff_reports
from app.data.recipe_types import DiffItem, Recipe
from app.data.recipe_parsers import match_key, period_residue
from tests.test_recipe_diff import (
    AUG, BASE, D00_CONFIRMS, FLOW, SEP, SHEET, add_outside, extraction_of, file_name, flow_doc, sep, set_period_cell,
)

LIST: dict[str, Any] = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s1", "match": {"name": "销售"}, "blocks": [{
        "id": "列表1", "layout": "list", "table": "销售",
        "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                    {"header": "产品", "name": "产品", "type": "TEXT"},
                    {"header": "日期", "name": "日期", "type": "DATE"},
                    {"header": "销量（件）", "name": "销量", "type": "INTEGER"},
                    {"header": "金额（元）", "name": "金额", "type": "REAL"}],
        "rows": {"total_row": {"label_column": "地区", "pick": "合计", "keep_as": "销售_表内合计"}},
    }]}],
    "tables": [{"name": "销售", "grain": ["地区", "产品"], "units": {"销量": "件", "金额": "元"}},
               {"name": "销售_表内合计", "grain": ["合计项"], "kind": "reported_total",
                "units": {"销量": "件", "金额": "元"}}],
}


def flow(**patch: Any) -> dict[str, Any]:
    r = copy.deepcopy(FLOW)
    for path, value in patch.items():
        cur = r
        keys = path.split("__")
        for k in keys[:-1]:
            cur = cur[int(k)] if k.isdigit() else cur[k]
        last = keys[-1]
        cur[int(last) if last.isdigit() else last] = value
    return r


def lst(**patch: Any) -> dict[str, Any]:
    r = copy.deepcopy(LIST)
    block = r["sheets"][0]["blocks"][0]
    for k, v in patch.items():
        block[k] = v
    return r


def list_ex(**extra: Any) -> dict[str, Any]:
    ex = {"ok": True, "problems": [], "tables": [], "placeholders": {}, "labels": [], "outside_text": [],
          "sheets": {"matched": {"s1": "销售"}, "renamed": {}, "other_visible": [], "skipped_hidden": []},
          "derived": [{"kind": "column_sum", "segment": "列表1", "sheet": "销售", "cell": "F12"}],
          "axes": [], "hidden": {}, "ignored_columns": {}, "formula_cells_accepted": 0, "blank_rows_skipped": 0,
          "period": None, "lineage": {}}
    ex.update(extra)
    return ex


def ids(ctx: ConfirmContext) -> set[str]:
    return {i.id for i in confirm_items(ctx)}


def first(recipe: dict[str, Any], ex: dict[str, Any] | None = None, **kw: Any) -> ConfirmContext:
    kw.setdefault("checks", [])
    return ConfirmContext(kind=kw.pop("kind", "first"), recipe=recipe,
                          extraction=ex if ex is not None else extraction_of(BASE[0]), **kw)


def label(ctx: ConfirmContext, item_id: str) -> str:
    return next(i.label for i in confirm_items(ctx) if i.id == item_id)


# --------------------------------------------------------------------------
# 配方类：每行一正一反
# --------------------------------------------------------------------------


def test_reference_recipe_first_import_exact():
    items = confirm_items(first(FLOW))
    assert {i.id for i in items} == D00_CONFIRMS
    got = {i.id: i for i in items}
    assert got["placeholder:·"].label == "「·」存为空值（表示无数据，本期 31 格）"
    assert got["derived:夜间合计"].label == "第 28–30 行改作核对，原值另存表「时段客流_表内合计」"
    assert got["relation:R1"].label == "每期核对「全日客流 = 分区甲 + 分区乙」（表「日客流」）"
    assert got["unit:日客流.全日客流"].label == "「全日客流（人次）」→ 表「日客流」的列「全日客流」，单位 人次"
    assert got["mode"].label == "导入模式：每期替换"
    assert all(i.required and i.source == "recipe" for i in items)


def test_placeholder_counts_and_absence():
    r = flow()
    r["sheets"][0]["blocks"][0]["values"]["placeholders"] = []
    assert not any(i.startswith("placeholder:") for i in ids(first(r)))
    r["sheets"][0]["blocks"][0]["values"]["placeholders"] = [{"text": "·"}, {"text": "-", "meaning": "不适用"}]
    ctx = first(r)
    assert label(ctx, "placeholder:-") == "「-」存为空值（表示不适用，本期 0 格）"   # 配置了就出，个数可以是 0


@pytest.mark.parametrize("field,value,item", [
    ("blank", "null", "blank_null:交叉表"),
    ("text_number", "parse_thousands", "text_number:交叉表"),
    ("formula", "accept_cached", "formula_cached:交叉表"),
])
def test_crosstab_value_relaxations(field, value, item):
    assert item not in ids(first(FLOW))
    r = flow()
    r["sheets"][0]["blocks"][0]["values"][field] = value
    assert item in ids(first(r))


def test_formula_cached_list_only_when_formulas_seen():
    assert "formula_cached:列表1" not in ids(first(LIST, list_ex()))
    ctx = first(LIST, list_ex(formula_cells_accepted=3))
    assert label(ctx, "formula_cached:列表1") == "「列表1」中的公式格按文件里保存的值导入（本期 3 格）"
    assert "formula_cached:列表1" not in ids(first(lst(values={"formula": "reject"}), list_ex(formula_cells_accepted=3)))


def test_merged_fill():
    assert "merged_fill:列表1" not in ids(first(LIST, list_ex()))
    assert "merged_fill:列表1" in ids(first(lst(merged_data="fill"), list_ex()))


def test_text_number_list():
    assert "text_number:列表1" in ids(first(lst(values={"text_number": "parse_thousands"}), list_ex()))


def test_checks_relaxed():
    assert not any(i.startswith("checks_relaxed:") for i in ids(first(FLOW)))
    r = flow()
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]
    assert label(first(r), "checks_relaxed:交叉表") == "「交叉表」不检查日期「恰好覆盖统计期」"
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = []
    assert "「逐日连续」、「恰好覆盖统计期」" in label(first(r), "checks_relaxed:交叉表")


def test_cross_check_off_uses_actual_sheet_name():
    assert not any(i.startswith("cross_check_off:") for i in ids(first(FLOW)))
    r = flow()
    r["sheets"][0]["context"][0]["cross_check"] = "none"
    assert "cross_check_off:客流汇总" in ids(first(r))
    ex = extraction_of(BASE[0])
    ex["sheets"]["matched"] = {"s1": "汇总"}
    assert "cross_check_off:汇总" in ids(first(r, ex))


def test_ignored_columns_only_when_something_ignored():
    assert "ignored_columns:列表1" not in ids(first(lst(extra_columns="ignore"), list_ex()))
    ex = list_ex(ignored_columns={"列表1": ["备注", "经办人"]})
    assert label(first(lst(extra_columns="ignore"), ex), "ignored_columns:列表1") == "「列表1」不导入这些列：「备注」「经办人」"
    assert "ignored_columns:列表1" not in ids(first(LIST, ex))


def test_blank_skip():
    assert "blank_skip:列表1" not in ids(first(LIST, list_ex()))
    r = lst(rows={"blank_rows": "skip"})
    assert label(first(r, list_ex(blank_rows_skipped=2)), "blank_skip:列表1") == "「列表1」跳过数据中间的空行（本期 2 行）"
    # 配置了就出，本期 0 行也出：首期恰好没有空行时不出，之后几期真跳过了行、配方又没变，就没人确认过了
    assert label(first(r, list_ex(blank_rows_skipped=0)), "blank_skip:列表1") == "「列表1」跳过数据中间的空行（本期 0 行）"


def test_total_row_and_derived():
    ctx = first(LIST, list_ex())
    assert label(ctx, "total_row:列表1") == "「列表1」的合计行（第 12 行）改作核对，原值另存表「销售_表内合计」"
    detail = next(i.detail for i in confirm_items(ctx) if i.id == "total_row:列表1")
    assert detail == "按明细核对的列：销量、金额；其余列合计行里的内容不核对"      # SE-10
    assert "total_row:列表1" not in ids(first(lst(rows={}), list_ex()))
    r = flow()
    r["sheets"][0]["blocks"][0]["segments"][3]["keep_as"] = None
    r["tables"].pop()
    assert label(first(r), "derived:夜间合计").endswith("改作核对，原值不另存")
    r["sheets"][0]["blocks"][0]["segments"].pop()
    r["relations"] = []
    assert "derived:夜间合计" not in ids(first(r))


def test_year_from():
    assert "year_from:交叉表" in ids(first(FLOW))
    ex = extraction_of(BASE[0])
    ex["axes"][0]["form"] = "date"            # 日期格自带年份
    assert "year_from:交叉表" not in ids(first(FLOW, ex))
    r = flow()
    r["sheets"][0]["blocks"][0]["axis"]["year_from"] = None
    assert "year_from:交叉表" not in ids(first(r))


def test_relations_and_dismissed():
    r = flow()
    r["relations"] = [{"id": "R1", "kind": "dismissed", "claims": "F1", "reason": "巧合"}]
    facts = {"facts": [{"id": "F1", "kind": "sum_eq", "sheet": SHEET, "text": "第 5 行 = 第 6 行 + 第 7 行", "detail": {}}]}
    got = ids(first(r, facts=facts))
    assert "dismissed:F1" in got and not any(i.startswith("relation:") for i in got)
    assert label(first(r, facts=facts), "dismissed:F1") == "不登记「第 5 行 = 第 6 行 + 第 7 行」（理由：巧合）"
    assert label(first(r), "dismissed:F1") == "不登记系统发现的关系 F1（理由：巧合）"
    assert "relation:R2" in ids(first(FLOW))
    assert "口径不同" in label(first(FLOW), "relation:R2")


def test_units_each_column():
    r = flow()
    for t in r["tables"]:
        t["units"] = {}
    assert not any(i.startswith("unit:") for i in ids(first(r)))
    list_ids = ids(first(LIST, list_ex()))
    assert {"unit:销售.销量", "unit:销售.金额", "unit:销售_表内合计.销量", "unit:销售_表内合计.金额"} <= list_ids


def test_hidden_policy():
    assert not any(i.startswith("hidden:") for i in ids(first(FLOW)))
    r = flow()
    r["sheets"][0]["hidden"] = {"rows": "exclude", "cols": "include"}
    ex = extraction_of(BASE[0])
    ex["hidden"] = {SHEET: {"rows": [4, 9], "cols": [3], "policy_rows": "exclude", "policy_cols": "include"}}
    assert label(first(r, ex), "hidden:客流汇总") == "工作表「客流汇总」的隐藏行：排除（第 4、9 行）；隐藏列：包含（C 列）"
    r["sheets"][0]["hidden"] = {"rows": "include"}
    assert "本期没有隐藏行" in label(first(r), "hidden:客流汇总")


def test_recipe_class_only_when_recipe_differs():
    ex = extraction_of(sep()[0])
    # 同一份配方（一份写全默认值、一份省略）：canonical 相同，配方类全不出
    explicit = Recipe.model_validate(FLOW).model_dump(mode="json")
    compact = Recipe.model_validate(FLOW).model_dump(mode="json", exclude_defaults=True)
    assert not recipe_differs("reupload", explicit, compact)
    assert ids(ConfirmContext(kind="reupload", recipe=explicit, old_recipe=compact, extraction=ex, checks=[], diff=[],
                              prev=BASE[0])) == set()
    # 改了配方：配方类全部重新出
    r = flow()
    r["sheets"][0]["blocks"][0]["values"]["blank"] = "null"
    got = ids(ConfirmContext(kind="redraft", recipe=r, old_recipe=FLOW, extraction=ex, checks=[], diff=[],
                             prev=BASE[0]))
    assert D00_CONFIRMS | {"blank_null:交叉表"} <= got
    # 首次、切换、没有现行配方都算不同
    assert recipe_differs("first", FLOW, FLOW) and recipe_differs("switch", FLOW, None)
    assert recipe_differs("reupload", FLOW, None)


# --------------------------------------------------------------------------
# AI 类
# --------------------------------------------------------------------------


def test_ai_origin_items():
    r = flow()
    r["sheets"][0]["blocks"][0]["segments"][0]["measures"]["全日客流（人次）"] = "全天客流"
    r["tables"][0]["units"] = {"全天客流": "人次", "分区甲": "人次", "分区乙": "人次"}
    r["relations"][0]["total"] = "全天客流"
    r["relations"][1]["b"]["value"] = "全天客流"
    got = ids(first(r, recipe_origin="ai"))
    assert {"ai_origin", "rename:日客流.全天客流", "columns:日客流", "columns:时段客流", "columns:时段客流_表内合计"} <= got
    assert not any(i.startswith("rename:日客流.分区") for i in got)      # 按标签推出来的名字不算改名
    assert label(first(r, recipe_origin="ai"), "rename:日客流.全天客流") == "原标签「全日客流（人次）」改名为列「全天客流」"
    # 每列带类型和单位（SE-10）
    assert label(first(r, recipe_origin="ai"), "columns:时段客流") == (
        "表「时段客流」的列：日期（日期）、时段（文字）、时段类别（文字）、起始小时（整数）、结束小时（整数）、客流（整数，人次）")
    assert "ai_origin" in ids(first(r, recipe_origin="mixed"))
    assert not {i for i in ids(first(r, recipe_origin="rules")) if i.startswith(("ai_", "rename:", "columns:"))}
    assert not {i for i in ids(first(r, recipe_origin="manual")) if i.startswith(("ai_", "rename:", "columns:"))}


def test_ai_items_not_repeated_on_reupload():
    # 现行配方出自 AI：每月重传配方没变，不再要求 AI 类确认
    got = ids(ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW, extraction=extraction_of(sep()[0]),
                             checks=[], diff=[], prev=BASE[0], recipe_origin="ai"))
    assert "ai_origin" not in got


def test_ai_rename_list_columns():
    r = lst()
    r["sheets"][0]["blocks"][0]["columns"][4]["name"] = "销售额"
    r["tables"][0]["units"] = {"销量": "件", "销售额": "元"}
    r["tables"][1]["units"] = {"销量": "件", "销售额": "元"}
    got = ids(first(r, list_ex(), recipe_origin="ai"))
    assert "rename:销售.销售额" in got
    assert "rename:销售.金额" not in got and "rename:销售.销量" not in got   # 「销量（件）」→ 销量 不算改名


# --------------------------------------------------------------------------
# 破坏性变更
# --------------------------------------------------------------------------


def test_breaking_changes_scope():
    new = flow()
    new["sheets"][0]["blocks"][0]["values"]["type"] = "REAL"                       # 类型
    new["tables"][1]["units"] = {"客流": "人"}                                      # 单位
    new["tables"][0]["grain"] = ["日期", "全日客流"]                                 # 主键
    new["tables"][2]["kind"] = "data"                                              # 种类
    new["sheets"][0]["blocks"][0]["values"]["placeholders"] = [{"text": "·", "meaning": "不适用"}]   # 占位符含义
    out = breaking_changes(FLOW, new)
    assert "列「全日客流」类型 INTEGER → REAL" in out["日客流"]
    assert "列「客流」的单位 人次 → 人" in out["时段客流"]
    assert "主键 日期 → 日期、全日客流" in out["日客流"]
    assert "表的种类 原表合计 → 明细数据" in out["时段客流_表内合计"]
    assert "值列的占位符「·」含义 无数据 → 不适用" in out["时段客流"]
    assert breaking_changes(FLOW, FLOW) == {}


def test_breaking_changes_const_pick():
    """分段标题改了写法、常量只能另选候选词（D09：「日间分时段客流」的候选词只有「日间分」）：列名、类型都没变，
    这一列的值却整列变了。必须进 breaking:<表> 让人确认，不能静默换值。"""
    new = flow()
    seg = new["sheets"][0]["blocks"][0]["segments"][1]
    seg["locate"]["title"] = "日间分时段客流（人次）"
    seg["const"]["时段类别"]["pick"] = "日间分"
    out = breaking_changes(FLOW, new)
    assert out == {"时段客流": [f"分段「{seg['id']}」的列「时段类别」取值「日间」→「日间分」"]}
    assert [c.kind for c in table_changes(FLOW, new)] == ["const_value"]
    ex = extraction_of(sep()[0])
    got = [i.id for i in confirm_items(ConfirmContext(kind="redraft", recipe=new, old_recipe=FLOW, extraction=ex,
                                                      checks=[], diff=[], prev=BASE[0]))]
    assert "breaking:时段客流" in got
    # 只改了标题、pick 不变：不算
    same = flow()
    same["sheets"][0]["blocks"][0]["segments"][1]["locate"]["title"] = "日间时段客流"
    assert breaking_changes(FLOW, same) == {}


def test_ai_columns_label_shows_text_type_of_a_numeric_column():
    """SE-10：AI 把列表的金额列声明成 TEXT 能过静态校验：columns:<表> 的文案里看得见「文字」。"""
    r = copy.deepcopy(LIST)
    col = next(c for c in r["sheets"][0]["blocks"][0]["columns"] if c["name"] == "金额")
    col["type"] = "TEXT"
    text = label(first(r, list_ex(), recipe_origin="ai"), "columns:销售")
    assert "金额（文字，元）" in text and "销量（整数，件）" in text


def test_dismissed_reason_marked_as_possibly_ai_written():
    """SE-10：AI（或 AI 起草后改过的）配方里，dismissed 的理由标明可能出自 AI；规则起草的不标。"""
    r = flow()
    r["relations"][0] = {"id": "R1", "kind": "dismissed", "claims": "F1", "reason": "巧合，无需核对"}
    for origin, marked in (("ai", True), ("mixed", True), ("rules", False), ("manual", False)):
        text = next(i.label for i in confirm_items(first(r, recipe_origin=origin)) if i.id == "dismissed:F1")
        assert ("可能由 AI 填写" in text) is marked, (origin, text)


def test_mask_lost_when_switch_or_redraft_drops_a_masked_column():
    """AU-6：源上遮罩的列在新结构里没有同名列（不区分大小写）→ 必勾 mask_lost:<列>；旧结构里本来就没有的不报。"""
    old_cache = {"tables": {"销售": {"columns": [{"name": "地区", "type": "TEXT"}, {"name": "金额_万元", "type": "REAL"}]}}}
    ctx = first(LIST, list_ex(), kind="switch", old_schema_cache=old_cache, mask_columns=["金额_万元", "不存在的列"])
    items = {i.id: i for i in confirm_items(ctx)}
    assert "mask_lost:金额_万元" in items and items["mask_lost:金额_万元"].source == "switch"
    assert "mask_lost:不存在的列" not in items
    # 新结构里有同名列（大小写不同也算）：不报
    ctx = first(LIST, list_ex(), kind="switch", old_schema_cache=old_cache, mask_columns=["金额", "地区"])
    assert not {i for i in ids(ctx) if i.startswith("mask_lost:")}
    # 改配方把列改了名
    new = copy.deepcopy(LIST)
    next(c for c in new["sheets"][0]["blocks"][0]["columns"] if c["name"] == "金额")["name"] = "销售额"
    new["tables"][0]["units"] = {"销量": "件", "销售额": "元"}
    ex = list_ex()
    ctx = ConfirmContext(kind="redraft", recipe=new, old_recipe=LIST, extraction=ex, checks=[], diff=[],
                         prev={"receipt": {"tables": []}}, mask_columns=["金额"])
    assert "mask_lost:金额" in ids(ctx)


def test_breaking_changes_column_source_swapped():
    """AU-2：交叉表两列的来源标签对调、列表两列的表头对调：列名类型都没变，内容整列换了，要进 breaking。"""
    new = flow()
    m = new["sheets"][0]["blocks"][0]["segments"][0]["measures"]
    m["分区甲（人次）"], m["分区乙（人次）"] = "分区乙", "分区甲"
    out = breaking_changes(FLOW, new)
    assert "列「分区甲」的来源「分区甲（人次）」→「分区乙（人次）」" in out["日客流"]
    assert "列「分区乙」的来源「分区乙（人次）」→「分区甲（人次）」" in out["日客流"]
    ex = extraction_of(sep()[0])
    got = [i.id for i in confirm_items(ConfirmContext(kind="redraft", recipe=new, old_recipe=FLOW, extraction=ex,
                                                      checks=[], diff=[], prev=BASE[0]))]
    assert "breaking:日客流" in got
    lst2 = copy.deepcopy(LIST)
    cols = lst2["sheets"][0]["blocks"][0]["columns"]
    cols[0]["name"], cols[1]["name"] = cols[1]["name"], cols[0]["name"]
    kinds = [c.kind for c in table_changes(LIST, lst2)]
    assert kinds.count("source") == 2
    # 只是写法（空白、全角）不同：match_key 相同，不算
    same = flow()
    seg = same["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"] = [x.replace("（", "(").replace("）", ")") for x in seg["labels"]["expect"]]
    seg["measures"] = {k.replace("（", "(").replace("）", ")"): v for k, v in seg["measures"].items()}
    assert not [c for c in table_changes(FLOW, same) if c.kind == "source"]


def test_breaking_changes_const_pick_with_segment_id_changed():
    """AU-2：分段 id 跟着 pick 一起改（把新文件的规则草稿粘进来）：按 (表, 列) 比全部取值，照样报 const_value。"""
    new = flow()
    seg = new["sheets"][0]["blocks"][0]["segments"][1]
    old_id = seg["id"]
    seg["id"] = old_id + "分段"
    seg["locate"]["title"] = "日间分时段客流（人次）"
    seg["const"]["时段类别"]["pick"] = "日间分"
    for other in new["sheets"][0]["blocks"][0]["segments"]:
        if (other.get("locate") or {}).get("segment") == old_id:
            other["locate"]["segment"] = seg["id"]
    changes = [c for c in table_changes(FLOW, new) if c.kind == "const_value"]
    assert len(changes) == 1 and "日间分" in changes[0].message and "日间" in changes[0].message


def test_breaking_changes_text_store():
    """AU-2：列表文字列的存法（规范写法 / 原文）显式改了要报；缺省与显式写成同一个生效值不报。"""
    base = copy.deepcopy(LIST)
    base["tables"][0]["grain"] = ["地区"]                     # 产品不在主键里：缺省按原文存
    raw_explicit = copy.deepcopy(base)
    raw_explicit["sheets"][0]["blocks"][0]["columns"][1]["store"] = "raw"
    assert not [c for c in table_changes(base, raw_explicit) if c.kind == "store"]
    canon_ = copy.deepcopy(base)
    canon_["sheets"][0]["blocks"][0]["columns"][1]["store"] = "canonical"
    ch = [c for c in table_changes(base, canon_) if c.kind == "store"]
    assert len(ch) == 1 and ch[0].message == "列「产品」的文字存法 原文 → 规范写法"


def test_breaking_changes_removed_and_added():
    new = flow()
    seg = new["sheets"][0]["blocks"][0]["segments"][0]
    seg["labels"]["expect"] = ["全日客流（人次）", "分区甲（人次）", "分区丙（人次）"]
    seg["measures"] = {"全日客流（人次）": "全日客流", "分区甲（人次）": "分区甲", "分区丙（人次）": "分区丙"}
    new["sheets"][0]["blocks"][0]["segments"].pop()          # 夜间合计没了，表内合计表随之消失
    out = breaking_changes(FLOW, new)
    assert out["日客流"] == ["删除或改名了列「分区乙」"]       # 新增的「分区丙」不算
    assert out["时段客流_表内合计"] == ["表「时段客流_表内合计」不再产出（删除或改名）"]
    # 反过来：表内合计表是新增的，不算；分区丙被删了，照报
    assert breaking_changes(new, FLOW) == {"日客流": ["删除或改名了列「分区丙」"]}


def test_breaking_changes_text_date():
    new = lst()
    new["sheets"][0]["blocks"][0]["columns"][2]["type"] = "TEXT"
    out = breaking_changes(LIST, new)
    assert out == {"销售": ["列「日期」的写法 DATE → TEXT（日期存为 YYYY-MM-DD 文本）"]}
    assert [c.kind for c in table_changes(LIST, new)] == ["type"]


def test_breaking_and_unit_changed_items():
    new = flow()
    new["tables"][0]["units"] = {"全日客流": "人", "分区甲": "人次", "分区乙": "人次"}
    ex = extraction_of(sep()[0])
    items = confirm_items(ConfirmContext(kind="redraft", recipe=new, old_recipe=FLOW, extraction=ex, checks=[], diff=[],
                                         prev=BASE[0]))
    assert items[0].id == "unit_changed:日客流.全日客流"                    # 排最前
    assert "人次 → 人" in items[0].label
    assert not any(i.id.startswith("breaking:") for i in items)            # 只变了单位：不重复出 breaking
    new["sheets"][0]["blocks"][0]["values"]["type"] = "REAL"
    got = [i.id for i in confirm_items(ConfirmContext(kind="redraft", recipe=new, old_recipe=FLOW, extraction=ex,
                                                      checks=[], diff=[], prev=BASE[0]))]
    assert got[0] == "unit_changed:日客流.全日客流"
    assert {"breaking:日客流", "breaking:时段客流", "breaking:时段客流_表内合计"} <= set(got)
    # 首次导入没有现行配方，不报破坏性变更
    assert not any(i.startswith(("breaking:", "unit_changed:")) for i in ids(first(new)))


def test_switch_from_simple():
    cache = {"tables": {
        "客流汇总": {"columns": [{"name": "项目", "type": "TEXT"}]},
        "日客流": {"columns": [{"name": "日期", "type": "TEXT"}, {"name": "全日客流", "type": "TEXT"},
                               {"name": "备注", "type": "TEXT"}, {"name": "分区甲", "type": "BIGINT"}]},
    }}
    lines = switch_changes(cache, FLOW)
    assert lines == ["表「客流汇总」将消失", "表「日客流」：列「全日客流」类型 TEXT → INTEGER、列「备注」将消失"]
    items = confirm_items(ConfirmContext(kind="switch", recipe=FLOW, old_schema_cache=cache,
                                         extraction=extraction_of(BASE[0]), checks=[], prev=None))
    sw = next(i for i in items if i.id == "switch_from_simple")
    assert sw.source == "switch" and "表「客流汇总」将消失" in sw.label
    assert D00_CONFIRMS <= {i.id for i in items}                          # 切换时配方类照出
    assert not any(i.id.startswith("breaking:") for i in items)
    assert "switch_from_simple" not in ids(first(FLOW))
    keep = confirm_items(ConfirmContext(kind="switch", recipe=FLOW, old_schema_cache={"tables": {}},
                                        extraction=extraction_of(BASE[0]), checks=[]))
    assert next(i for i in keep if i.id == "switch_from_simple").label.endswith("原有的表和列都保留")


# --------------------------------------------------------------------------
# 本期类
# --------------------------------------------------------------------------


def reupload(cur_doc: dict[str, Any], prev_doc: dict[str, Any] | None = BASE[0], **kw: Any) -> ConfirmContext:
    kw.setdefault("diff", [])
    return ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW, extraction=kw.pop("ex", extraction_of(cur_doc)),
                          checks=cur_doc["checks"], prev=prev_doc, **kw)


def test_outside_digits_plain_text_with_digits_every_period():
    cur, _ = sep()
    add_outside(cur, "B32", "注：9月15日闸机故障，当日客流为估算值")
    item = next(i for i in confirm_items(reupload(cur)) if i.id == "outside_digits:客流汇总!B32")
    assert item.source == "outside" and "注：9月15日" in item.label
    # 下一期同一格同一句（只是日期变了）：不是统计期来源，仍要确认
    nxt = flow_doc(dt_oct(), 31)
    add_outside(nxt, "B32", "注：10月3日闸机故障，当日客流为估算值")
    assert "outside_digits:客流汇总!B32" in ids(reupload(nxt, cur))
    # 纯文字不出
    assert not any(i.startswith("outside_digits:") for i in ids(reupload(sep()[0])))


def dt_oct():
    import datetime as dt
    return dt.date(2026, 10, 1)


def test_outside_digits_exempt_a_title_gained_period():
    for text in ("客流汇总表（2026年9月）", "客流汇总表（2026年9月1日至2026年9月30日）"):
        cur, _ = sep()
        set_period_cell(cur, "B3", text, annotated=True)
        assert "outside_digits:客流汇总!B3" not in ids(reupload(cur))          # D17、D27
        assert "outside_digits:客流汇总!B3" in ids(first(FLOW, extraction_of(cur)))   # 首次没有上一期，不免


def test_outside_digits_exempt_a_requires_same_text():
    cur, _ = sep()
    set_period_cell(cur, "B3", "客流汇总初步表（2026年9月）", annotated=True)
    assert "outside_digits:客流汇总!B3" in ids(reupload(cur))


def test_outside_digits_exempt_a_requires_prev_plain_text():
    # 上一期这一格就含数字（不是纯文字）：(a) 不适用，即使附加文字和它相同
    prev = copy.deepcopy(BASE[0])
    prev["receipt"]["outside_text"] = [{"sheet": SHEET, "cell": "B3", "text": "客流汇总表3号门", "kind": "text_digits",
                                        "period_source": False}]
    cur, _ = sep()
    set_period_cell(cur, "B3", "客流汇总表3号门（2026年9月）", annotated=True)
    assert "outside_digits:客流汇总!B3" in ids(reupload(cur, prev))


def test_outside_digits_exempt_only_for_period_source_cells():
    # 上一期 B3 是纯文字「客流汇总表」，本期 B3 加了年月，但没被认成统计期来源（kind=text_digits、
    # period_source=False、不在 PeriodOut.texts 里）：(a) 不适用，要确认。去掉「只有统计期来源格才能免」的
    # 入口判断，这里就会被 (a) 免掉（D15、E7）
    cur, _ = sep()
    cur["receipt"]["outside_text"] = [o for o in cur["receipt"]["outside_text"] if o["cell"] != "B3"]
    add_outside(cur, "B3", "客流汇总表（2026年9月）")
    assert match_key(period_residue("客流汇总表（2026年9月）")) == match_key("客流汇总表")   # 真走到 (a) 是会免的
    assert "outside_digits:客流汇总!B3" in ids(reupload(cur))


def test_outside_digits_d28b_not_exempt():
    cur, _ = sep()
    set_period_cell(cur, "B32", "注：2026年9月数据为初步统计，待修订", annotated=True)
    assert "outside_digits:客流汇总!B32" in ids(reupload(cur))


def test_outside_digits_exempt_b_same_sentence_monthly():
    prev, _ = sep()
    set_period_cell(prev, "B32", "注：2026年9月数据为初步统计，待修订", annotated=True)
    cur = flow_doc(dt_oct(), 31)
    set_period_cell(cur, "B32", "注：2026年10月数据为初步统计，待修订", annotated=True)
    assert "outside_digits:客流汇总!B32" not in ids(reupload(cur, prev))
    set_period_cell(cur, "B32", "注：2026年10月数据为正式统计", annotated=True)
    assert "outside_digits:客流汇总!B32" in ids(reupload(cur, prev))


def test_outside_digits_exempt_b_other_digits_changed():
    # AU-3：附加文字里统计期以外的数字（口径）变了，遮盖模板相同也不算同一句，要确认
    prev, _ = sep()
    set_period_cell(prev, "B32", "注：2026年9月数据只含3个分区", annotated=True)
    cur = flow_doc(dt_oct(), 31)
    set_period_cell(cur, "B32", "注：2026年10月数据只含2个分区", annotated=True)
    assert "outside_digits:客流汇总!B32" in ids(reupload(cur, prev))
    set_period_cell(cur, "B32", "注：2026年10月数据只含3个分区", annotated=True)
    assert "outside_digits:客流汇总!B32" not in ids(reupload(cur, prev))


def test_outside_digits_prev_simple_import_not_exempt():
    cur, _ = sep()
    set_period_cell(cur, "B3", "客流汇总表（2026年9月）", annotated=True)
    simple = {"receipt": {"tables": [{"name": "客流汇总", "rows": 29}],
                          "outside_text": [{"sheet": SHEET, "cell": "B3", "text": "客流汇总表", "kind": "text"}]},
              "checks": [], "period": None}
    assert "outside_digits:客流汇总!B3" in ids(reupload(cur, simple))


def test_rows_after_stop():
    prob = {"code": "rows_after_stop", "category": "confirm", "message": "第 14 行起有 2 行文字在空行之后，没有导入",
            "cells": ["销售!C14", "销售!D14", "销售!C15", "销售!D15"]}
    ctx = first(LIST, list_ex(problems=[prob]))
    item = next(i for i in confirm_items(ctx) if i.id == "rows_after_stop:列表1")
    assert item.label == prob["message"] and item.source == "outside" and "销售!C14" in item.detail
    assert not any(i.startswith("rows_after_stop:") for i in ids(first(LIST, list_ex())))


def test_empty_list_block_needs_confirmation():
    """AU-1：执行器报 list_empty（列表块只有表头）→ 必勾 empty_block:<块>，首次、重传都出。"""
    prob = {"code": "list_empty", "category": "confirm", "cells": ["销售!A3"],
            "message": "工作表「销售」的列表「列表1」的表头之下没有数据行：本期这张表将导入 0 行。"}
    item = next(i for i in confirm_items(first(LIST, list_ex(problems=[prob]))) if i.id == "empty_block:列表1")
    assert item.required and item.source == "outside" and "0 行" in item.label and "表「销售」" in item.label
    assert not any(i.startswith("empty_block:") for i in ids(first(LIST, list_ex())))


def test_rows_after_stop_two_blocks_uses_lineage():
    r = copy.deepcopy(LIST)
    r["sheets"][0]["blocks"][0]["rows"] = {}
    second = copy.deepcopy(r["sheets"][0]["blocks"][0])
    second.update(id="列表2", table="费用", after_title="二、费用")
    r["sheets"][0]["blocks"].append(second)
    r["tables"] = [{"name": "销售"}, {"name": "费用"}]
    lineage = {"销售": {"地区": [[1, "销售", "C3", 4, "down"]], "金额": [[1, "销售", "G3", 4, "down"]]},
               "费用": {"地区": [[1, "销售", "C12", 3, "down"]], "金额": [[1, "销售", "G12", 3, "down"]]}}
    prob = {"code": "rows_after_stop", "category": "confirm", "message": "第 16 行起有 1 行文字在空行之后，没有导入",
            "cells": ["销售!C16", "销售!D16"]}
    got = ids(first(r, list_ex(problems=[prob], lineage=lineage, derived=[])))
    assert "rows_after_stop:列表2" in got and "rows_after_stop:列表1" not in got
    prob["cells"] = ["销售!C8", "销售!D8"]
    got = ids(first(r, list_ex(problems=[prob], lineage=lineage, derived=[])))
    assert "rows_after_stop:列表1" in got


def test_relation_observed():
    cur, _ = sep()
    assert not any(i.startswith("relation_observed:") for i in ids(reupload(cur)))   # 30 天中 0 天相等
    for c in cur["checks"]:
        if c["id"] == "R2":
            c["failed"] = 0
    lab = label(reupload(cur), "relation_observed:R2")
    assert lab == "表「时段客流」与表「日客流」声明为口径不同，但本期按「日期」分组的 30 组全部相等"


def test_sheet_extra():
    cur, _ = sep()
    cur["receipt"]["sheets"]["other_visible"] = ["说明"]
    assert "sheet_extra:说明" in ids(first(FLOW, extraction_of(cur)))       # 首次就有
    prev = copy.deepcopy(BASE[0])
    prev["receipt"]["sheets"]["other_visible"] = ["说明"]
    assert "sheet_extra:说明" not in ids(reupload(cur, prev))                 # 上一期也有
    cur["receipt"]["sheets"]["other_visible"] = ["说明", "备注"]
    got = ids(reupload(cur, prev))
    assert "sheet_extra:备注" in got and "sheet_extra:说明" not in got


def test_sheet_renamed():
    cur, _ = sep()
    cur["receipt"]["sheets"].update(matched={"s1": "汇总"}, renamed={"客流汇总": "汇总"})
    item = next(i for i in confirm_items(reupload(cur)) if i.id == "sheet_renamed:s1")
    assert item.label == "工作表「客流汇总」现在叫「汇总」" and item.source == "sheet"
    assert not any(i.startswith("sheet_renamed:") for i in ids(reupload(sep()[0])))


def test_context_human():
    cur, _ = sep()
    cur["period"] = {"start": "2026-09-01", "end": "2026-09-30", "source": "human", "cells": [],
                     "signed_by": "测试员甲", "texts": {}, "annotated": {}}
    item = next(i for i in confirm_items(reupload(cur)) if i.id == "context_human:统计期")
    assert item.label == "本期统计期为人工录入：2026-09-01 至 2026-09-30" and item.source == "context"
    assert "测试员甲" in item.detail
    assert "context_human:统计期" not in ids(reupload(sep()[0]))


def test_diff_items_collected():
    diff = [DiffItem("label_writing", "分段「日间」的标签写法变了", requires_confirm=True,
                     confirm_id="diff:label_writing:日间"),
            DiffItem("period", "统计期变了")]
    items = confirm_items(reupload(sep()[0], diff=diff))
    assert [(i.id, i.source) for i in items] == [("diff:label_writing:日间", "diff")]
    # dict 形状也收
    items = confirm_items(reupload(sep()[0], diff=[{"kind": "row_order", "label": "x", "detail": "",
                                                     "requires_confirm": True, "confirm_id": "diff:row_order:日客流"}]))
    assert [i.id for i in items] == ["diff:row_order:日客流"]


def test_accepts_dataclass_extraction_and_checks():
    from app.data.recipe_types import CheckResult, Extraction, OutsideText, SheetsOut

    ex = Extraction(ok=True, outside_text=[OutsideText(SHEET, "B32", "注：9月15日", "text_digits")],
                    sheets=SheetsOut(matched={"s1": SHEET}))
    checks = [CheckResult("R2", "relation_not_comparable", "R2", "info", "info", checked=30, failed=0)]
    got = ids(ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW, extraction=ex, checks=checks, diff=[],
                             prev=BASE[0]))
    assert got == {"outside_digits:客流汇总!B32", "relation_observed:R2"}


def test_end_to_end_with_diff_reports():
    # D28：差异卡和确认项一起跑，必勾集合正好是 outside_digits + diff:outside
    cur, name = sep()
    add_outside(cur, "B32", "注：9月15日闸机故障，当日客流为估算值")
    diff = diff_reports(BASE[0], cur, prev_file_name=BASE[1], cur_file_name=name, recipe=FLOW, recipe_changed=False)
    got = ids(reupload(cur, diff=diff))
    assert got == {"outside_digits:客流汇总!B32", "diff:outside:客流汇总!B32"}
    assert file_name(AUG, 31) == BASE[1] and SEP.month == 9


# --------------------------------------------------------------------------
# 输入不全就报错：漏一个键、一个参数，对应的必勾项会静默消失，提交时重算也发现不了
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["reupload", "redraft"])
def test_old_recipe_required_for_reupload_and_redraft(kind):
    # 漏传现行配方：早先 breaking:*、unit_changed:* 整段跳过（万元 → 元 静默放过）
    new = flow()
    new["tables"][0]["units"] = {"全日客流": "人", "分区甲": "人次", "分区乙": "人次"}
    ctx = ConfirmContext(kind=kind, recipe=new, old_recipe=None, extraction=extraction_of(sep()[0]), checks=[],
                         diff=[], prev=BASE[0])
    with pytest.raises(ValueError, match="现行配方"):
        confirm_items(ctx)
    ctx.old_recipe = FLOW
    assert "unit_changed:日客流.全日客流" in ids(ctx)


@pytest.mark.parametrize("kind", ["reupload", "redraft"])
def test_prev_required_for_reupload_and_redraft(kind):
    ctx = ConfirmContext(kind=kind, recipe=FLOW, old_recipe=FLOW, extraction=extraction_of(sep()[0]), checks=[],
                         diff=[], prev=None)
    with pytest.raises(ValueError, match="上一期"):
        confirm_items(ctx)


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="未知的导入方式"):
        confirm_items(first(FLOW, kind="upload"))


def test_checks_and_diff_required():
    with pytest.raises(ValueError, match="核对结果"):
        confirm_items(ConfirmContext(kind="first", recipe=FLOW, extraction=extraction_of(BASE[0])))
    with pytest.raises(ValueError, match="差异卡"):
        confirm_items(ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW, extraction=extraction_of(sep()[0]),
                                     checks=[], prev=BASE[0]))
    # 首次没有上一期，不用给差异卡
    assert ids(ConfirmContext(kind="first", recipe=FLOW, extraction=extraction_of(BASE[0]), checks=[])) == D00_CONFIRMS


@pytest.mark.parametrize("key", EXTRACTION_KEYS)
def test_missing_extraction_key_raises(key):
    # 例如 extraction 没有 problems：rows_after_stop 早先直接没了
    ex = extraction_of(BASE[0])
    del ex[key]
    with pytest.raises(ReceiptIncomplete) as err:
        confirm_items(first(FLOW, ex))
    assert err.value.side == "extraction" and err.value.missing == [key]


def test_missing_extraction_raises():
    with pytest.raises(ReceiptIncomplete, match="extraction"):
        confirm_items(ConfirmContext(kind="first", recipe=FLOW, checks=[]))


def test_partial_extraction_raises():
    ex = extraction_of(BASE[0])
    ex["partial"] = True
    with pytest.raises(ReceiptIncomplete, match="partial"):
        confirm_items(first(FLOW, ex))


@pytest.mark.parametrize("key", ["outside_text", "sheets"])
def test_prev_receipt_missing_key_raises(key):
    prev = copy.deepcopy(BASE[0])
    del prev["receipt"][key]
    with pytest.raises(ReceiptIncomplete) as err:
        confirm_items(reupload(sep()[0], prev))
    assert err.value.side == "prev" and err.value.missing == [key]
