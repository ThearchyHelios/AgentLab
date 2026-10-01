"""配方静态校验（recipe.py，P2-SPEC 2.2、2.3）：每条规则一正一反，反例断言 code 和 path。

只用契约里的参考配方（flow_recipe.json）和在测试里手写的小配方，不读任何 Excel。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.data.recipe import (
    RecipeInvalid,
    apply_patch,
    canonical_recipe,
    parse_recipe,
    recipe_sha256,
    validate_recipe,
)
from app.data.recipe_types import DraftFacts, Fact, Recipe

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))
SEG = "/sheets/0/blocks/0/segments"

F1 = Fact("F1", "sum_eq", "客流汇总", "第 5 行 = 第 6 行 + 第 7 行（31 列中 31 列成立）",
          {"segment": "日客流", "total": "全日客流（人次）", "parts": ["分区甲（人次）", "分区乙（人次）"], "rows": [5, 6, 7]})
F2 = Fact("F2", "not_equal_sum", "客流汇总", "日间、夜间各行之和 与 第 5 行（31 列中 0 列相等）",
          {"a": ["日间", "夜间"], "b_segment": "日客流", "b": "全日客流（人次）", "equal": 0, "checked": 31})
FACTS = DraftFacts(facts=[F1, F2])


def flow() -> dict:
    return copy.deepcopy(FLOW)


def segs(r: dict) -> list[dict]:
    return r["sheets"][0]["blocks"][0]["segments"]


def table(r: dict, name: str) -> dict:
    return next(t for t in r["tables"] if t["name"] == name)


def list_recipe() -> dict:
    """lab c01 那种列表：表头 地区 / 产品 / 销量 / 金额（万元），合计行另存。"""
    return {
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": "月报"}, "blocks": [{
            "id": "列表1", "layout": "list", "table": "销售",
            "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                        {"header": "产品", "name": "产品", "type": "TEXT"},
                        {"header": "销量", "name": "销量", "type": "INTEGER"},
                        {"header": "金额（万元）", "name": "金额", "type": "REAL"}],
            "rows": {"total_row": {"label_column": "地区", "pick": "合计", "keep_as": "销售_表内合计"}}}]}],
        "tables": [{"name": "销售", "grain": ["地区", "产品"], "units": {"金额": "万元"}},
                   {"name": "销售_表内合计", "kind": "reported_total", "grain": ["合计项"], "units": {"金额": "万元"}}],
    }


def run(data, *, facts=FACTS, origin="manual"):
    _recipe, problems = validate_recipe(data, facts=facts, origin=origin)
    return problems


def only(data, code, path=None, **kw):
    """恰好一条问题，code（和 path）对得上：反例不能连带一串别的问题，否则看不出规则本身对不对。"""
    problems = run(data, **kw)
    assert [p.code for p in problems] == [code], [(p.code, p.path, p.message) for p in problems]
    if path is not None:
        assert problems[0].path == path
    return problems[0]


def has(data, code, path=None, **kw):
    problems = run(data, **kw)
    hits = [p for p in problems if p.code == code and (path is None or p.path == path)]
    assert hits, [(p.code, p.path, p.message) for p in problems]
    return hits[0]


# ==========================================================================
# 参考配方
# ==========================================================================


@pytest.mark.parametrize("origin", ["rules", "ai", "manual", "replay"])
def test_reference_recipe_has_no_problems(origin):
    recipe, problems = validate_recipe(flow(), facts=FACTS, origin=origin)
    assert problems == []
    assert isinstance(recipe, Recipe)


def test_reference_recipe_without_facts_and_as_model():
    assert run(flow(), facts=None) == []
    model = Recipe.model_validate(flow())
    recipe, problems = validate_recipe(model)
    assert recipe is model and problems == []


def test_list_recipe_has_no_problems():
    assert run(list_recipe(), facts=None) == []


# ==========================================================================
# schema（规则 1、5、7）：parse_recipe 转 pydantic 的错误
# ==========================================================================


def test_closed_language_extra_fields_are_schema_problems_with_clean_paths():
    r = flow()
    r["sheets"][0]["regex"] = "x"
    segs(r)[1]["const"]["时段类别"] = {"value": "日间"}
    recipe, problems = validate_recipe(r)
    assert recipe is None
    assert {p.code for p in problems} == {"schema"}
    # loc 里 pydantic 插的判别字段（crosstab、dimension）去掉，path 对得上配方里的位置
    assert {p.path for p in problems} == {"/sheets/0/regex", f"{SEG}/1/const/时段类别/value",
                                          f"{SEG}/1/const/时段类别/pick"}
    by_path = {p.path: p.message for p in problems}
    assert by_path["/sheets/0/regex"] == "第 1 个工作表「客流汇总」：有配方语言不支持的字段「regex」"
    assert by_path[f"{SEG}/1/const/时段类别/pick"].startswith("第 1 个工作表「客流汇总」的分段「日间」：")


@pytest.mark.parametrize("mutate,path", [
    (lambda r: r.__setitem__("recipe_format", "agentlab-recipe/1"), "/recipe_format"),
    (lambda r: segs(r)[1]["dim"].__setitem__("parser", "regex"), f"{SEG}/1/dim/parser"),
    (lambda r: r["relations"][0].__setitem__("id", "X1"), "/relations/0/id"),
    (lambda r: segs(r)[0].pop("measures"), f"{SEG}/0/measures"),
    (lambda r: r["sheets"][0]["blocks"][0].__setitem__("layout", "grid"), "/sheets/0/blocks/0"),
    (lambda r: r["sheets"][0]["blocks"][0]["values"]["placeholders"][0].__setitem__("to", 0),
     "/sheets/0/blocks/0/values/placeholders/0/to"),
])
def test_schema_problems_point_at_the_field(mutate, path):
    r = flow()
    mutate(r)
    recipe, problems = validate_recipe(r)
    assert recipe is None
    assert [(p.code, p.path) for p in problems] == [("schema", path)]
    assert "：" in problems[0].message


def test_parse_recipe_raises_with_problems():
    with pytest.raises(RecipeInvalid) as info:
        parse_recipe({"recipe_format": "agentlab-recipe/2"})
    assert {p.code for p in info.value.problems} == {"schema"}
    assert {p.path for p in info.value.problems} == {"/sheets", "/tables"}
    with pytest.raises(RecipeInvalid):
        parse_recipe(["not", "a", "dict"])  # type: ignore[arg-type]
    assert isinstance(parse_recipe(flow()), Recipe)


# ==========================================================================
# 规则 2：常量只能 pick 分段标题的候选词
# ==========================================================================


def test_const_pick_must_be_a_candidate_word():
    r = flow()
    segs(r)[2]["const"]["时段类别"]["pick"] = "晚上"
    p = only(r, "const_not_candidate", f"{SEG}/2/const/时段类别/pick")
    assert p.message == ("第 1 个工作表「客流汇总」的分段「夜间」：常量「时段类别」的取值「晚上」不在分段标题"
                         "「夜间时段客流（人次）」的候选词中，可选：夜间、夜间时段客流")


def test_const_pick_other_candidate_is_fine():
    r = flow()
    segs(r)[2]["const"]["时段类别"]["pick"] = "夜间时段客流"
    assert run(r) == []


def test_const_needs_a_section_title():
    r = flow()
    segs(r)[1]["locate"] = {"by": "labels"}
    has(r, "const_not_candidate", f"{SEG}/1/const/时段类别/pick")


# ==========================================================================
# 规则 3：占位符不能像数
# ==========================================================================


@pytest.mark.parametrize("text", ["0", "-1", "1,234", "12.5%"])
def test_placeholder_must_not_look_numeric(text):
    r = flow()
    r["sheets"][0]["blocks"][0]["values"]["placeholders"][0]["text"] = text
    only(r, "placeholder_numeric", "/sheets/0/blocks/0/values/placeholders/0/text")


@pytest.mark.parametrize("text", ["·", "—", "无", "N/A"])
def test_placeholder_text_ok(text):
    r = flow()
    r["sheets"][0]["blocks"][0]["values"]["placeholders"][0]["text"] = text
    assert run(r) == []


def test_list_placeholder_must_not_look_numeric():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["values"] = {"placeholders": [{"text": "0"}]}
    only(r, "placeholder_numeric", "/sheets/0/blocks/0/values/placeholders/0/text", facts=None)


# ==========================================================================
# 规则 4：单位封闭词表
# ==========================================================================


def test_unit_must_be_in_vocabulary():
    r = flow()
    # 标签不带单位，只看词表这一条
    seg = segs(r)[0]
    seg["labels"]["expect"][0] = "全日客流"
    seg["measures"] = {"全日客流": "全日客流", "分区甲（人次）": "分区甲", "分区乙（人次）": "分区乙"}
    table(r, "日客流")["units"]["全日客流"] = "人头"
    p = only(r, "unit_unknown", "/tables/0/units/全日客流", facts=None)
    assert p.message == "表「日客流」：列「全日客流」的单位「人头」不在单位词表中"


def test_unit_in_vocabulary_ok():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["columns"][2]["header"] = "销量（件）"
    r["tables"][0]["units"]["销量"] = "件"
    r["tables"][1]["units"]["销量"] = "件"
    assert run(r, facts=None) == []


# ==========================================================================
# 规则 5：解析器固定集合（标签解析不了的、重复的在保存时就报）
# ==========================================================================


def test_dimension_labels_must_parse():
    r = flow()
    segs(r)[1]["labels"]["expect"][1] = "8:30-9:00"
    only(r, "label_unparsed", f"{SEG}/1/labels/expect/1")


def test_derived_labels_must_parse_as_totals():
    r = flow()
    segs(r)[3]["labels"]["expect"][0] = "18-22"
    only(r, "label_unparsed", f"{SEG}/3/labels/expect/0")


def test_duplicate_labels_by_canonical_form():
    r = flow()
    segs(r)[1]["labels"]["expect"][1] = "07:00-08:00"     # 规范写法是 7-8，与第一项重复
    only(r, "label_duplicate", f"{SEG}/1/labels/expect/1")


def test_text_dimension_labels_ok():
    """文本维度：标签只要非空、不超长；不能派生起止小时。日间改成文本维度、单独成表。"""
    r = flow()
    seg = segs(r)[1]
    seg.update(table="日间客流", dim={"name": "时段", "parser": "text"}, const={})
    seg["labels"]["expect"] = ["早上", "中午", "下午"]
    r["tables"].insert(1, {"name": "日间客流", "grain": ["日期", "时段"], "units": {"客流": "人次"}})
    # 夜间独占「时段客流」后没有兄弟标题，候选词只剩整个标题
    has(r, "const_not_candidate", f"{SEG}/2/const/时段类别/pick")
    segs(r)[2]["const"]["时段类别"]["pick"] = "夜间时段客流"
    assert run(r) == []
    seg["labels"]["expect"].append("很" * 41)
    only(r, "label_unparsed", f"{SEG}/1/labels/expect/3")


# ==========================================================================
# 规则 6：不能写死年份或统计期
# ==========================================================================


def test_const_pick_with_digits_is_period_literal():
    r = flow()
    segs(r)[1]["const"]["时段类别"]["pick"] = "2026日间"
    only(r, "period_literal", f"{SEG}/1/const/时段类别/pick")


def test_prefer_prefix_with_digits_is_period_literal():
    r = flow()
    r["sheets"][0]["context"][0]["prefer_prefix"] = "2026年统计"
    only(r, "period_literal", "/sheets/0/context/0/prefer_prefix")


def test_total_row_pick_with_digits_is_period_literal():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["rows"]["total_row"]["pick"] = "2026合计"
    only(r, "period_literal", "/sheets/0/blocks/0/rows/total_row/pick", facts=None)


def test_year_in_list_header_is_not_an_error():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["columns"][2]["header"] = "2026年上半年_销量"
    assert run(r, facts=None) == []


# ==========================================================================
# 规则 7：格式版本
# ==========================================================================


def test_accumulate_mode_follows_eligibility():
    """期 3 改写（P3-SPEC 2.2）：按期累积不再一律报 mode_unsupported，改按资格判（契约 accumulate_blockers）。"""
    r = flow()
    r["mode"] = "accumulate"
    assert run(r) == []                      # 参考配方符合资格：交叉表、有统计期、covers_context、grain 含日期
    # 去掉统计期：accumulate_needs_period（同时断言 covers_context、补年份都要求统计期，照期 2 报）
    r2 = flow()
    r2["mode"] = "accumulate"
    r2["sheets"][0]["context"] = []
    codes = [p.code for p in run(r2)]
    assert "accumulate_needs_period" in codes and "mode_unsupported" not in codes
    # 列表写入的表：accumulate_unkeyed，path 指向那张表
    lr = list_recipe()
    lr["mode"] = "accumulate"
    lr["sheets"][0]["context"] = [{}]
    probs = run(lr, facts=None)
    assert [(p.code, p.path) for p in probs] == [("accumulate_unkeyed", "/tables/0"),
                                                ("accumulate_unkeyed", "/tables/1")]
    assert "列表形态的表目前只支持每期替换" in probs[0].message
    # 同一份配方每期替换时不报
    lr["mode"] = "replace"
    assert run(lr, facts=None) == []


# ==========================================================================
# 规则 8：名字、键唯一
# ==========================================================================


def test_table_name_must_be_valid():
    r = flow()
    table(r, "时段客流_表内合计")["name"] = "select"
    segs(r)[3]["keep_as"]["table"] = "select"
    p = only(r, "name_invalid", "/tables/2/name")
    assert "关键字" in p.message


def test_column_name_must_be_valid():
    r = flow()
    for seg in segs(r)[1:3]:
        seg["const"] = {"1类别": seg["const"]["时段类别"]}
    problems = run(r)
    assert [(p.code, p.path) for p in problems] == [
        ("name_invalid", f"{SEG}/1/const/1类别"), ("name_invalid", f"{SEG}/2/const/1类别")]


def test_column_names_collide_within_a_table():
    r = flow()
    for seg in segs(r)[1:3]:
        seg["const"] = {"时段": seg["const"]["时段类别"]}
    problems = run(r)
    assert [(p.code, p.path) for p in problems] == [
        ("name_collision", f"{SEG}/1/const/时段"), ("name_collision", f"{SEG}/2/const/时段")]


def test_column_names_collide_case_insensitively():
    r = list_recipe()
    cols = r["sheets"][0]["blocks"][0]["columns"]
    cols[0]["name"], cols[1]["name"] = "Area", "area"
    r["sheets"][0]["blocks"][0]["rows"]["total_row"]["label_column"] = "Area"
    r["tables"][0]["grain"] = ["Area"]
    only(r, "name_collision", "/sheets/0/blocks/0/columns/1/name", facts=None)


def test_table_names_collide():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["rows"]["total_row"]["keep_as"] = "ＳＡＬＥＳ"
    r["sheets"][0]["blocks"][0]["table"] = "sales"
    r["tables"][0]["name"] = "sales"
    r["tables"][1]["name"] = "ＳＡＬＥＳ"
    problems = run(r, facts=None)
    assert ("name_collision", "/tables/1/name") in [(p.code, p.path) for p in problems]


def _second_sheet(r: dict, block: dict, tables: list[dict]) -> dict:
    r["sheets"].append({"id": "s2", "match": {"name": "附表"}, "blocks": [block]})
    r["tables"] += tables
    return r


def _extra_measures(seg_id: str, block_id: str = "交叉表2") -> tuple[dict, list[dict]]:
    block = {"id": block_id, "layout": "crosstab", "axis": {"year_from": None, "checks": ["contiguous"]},
             "segments": [{"id": seg_id, "role": "measures", "table": "附表客流", "locate": {"by": "labels"},
                           "labels": {"expect": ["附表客流"]}, "measures": {"附表客流": "附表客流"}}]}
    return block, [{"name": "附表客流", "grain": ["日期"]}]


def test_second_sheet_ok():
    block, tables = _extra_measures("附表")
    assert run(_second_sheet(flow(), block, tables)) == []


def test_segment_id_unique_across_blocks():
    block, tables = _extra_measures("日间")
    only(_second_sheet(flow(), block, tables), "segment_id_duplicate", "/sheets/1/blocks/0/segments/0/id")


def test_block_id_unique_across_sheets():
    block, tables = _extra_measures("附表", block_id="交叉表")
    only(_second_sheet(flow(), block, tables), "block_id_duplicate", "/sheets/1/blocks/0/id")


def test_block_id_must_not_reuse_a_segment_id():
    block, tables = _extra_measures("附表", block_id="夜间")
    only(_second_sheet(flow(), block, tables), "block_id_duplicate", "/sheets/1/blocks/0/id")


def test_sheet_id_and_sheet_name_unique():
    block, tables = _extra_measures("附表")
    r = _second_sheet(flow(), block, tables)
    r["sheets"][1]["id"] = "s1"
    only(r, "sheet_id_duplicate", "/sheets/1/id")
    r["sheets"][1]["id"] = "s2"
    r["sheets"][1]["match"]["name"] = "客流 汇总"
    only(r, "sheet_name_duplicate", "/sheets/1/match/name")


def test_one_crosstab_per_sheet():
    r = flow()
    block, tables = _extra_measures("附表")
    r["sheets"][0]["blocks"].append(block)
    r["tables"] += tables
    only(r, "crosstab_twice", "/sheets/0/blocks/1")


# ==========================================================================
# 规则 9：引用完整（逐字比较）
# ==========================================================================


def test_segment_table_must_match_verbatim():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["table"] = "Sales"
    r["tables"][0]["name"] = "sales"
    problems = run(r, facts=None)
    assert [(p.code, p.path) for p in problems] == [("table_unknown", "/sheets/0/blocks/0/table"),
                                                    ("table_unused", "/tables/0")]
    assert "「sales」" in problems[0].message


def test_crosstab_segment_table_case_differs():
    r = flow()
    table(r, "日客流")["name"] = "sales"
    segs(r)[0]["table"] = "Sales"
    r["relations"][0]["table"] = r["relations"][1]["b"]["table"] = "sales"
    p = has(r, "table_unknown", f"{SEG}/0/table")
    assert "「sales」" in p.message


def test_crosstab_segment_table_unknown():
    r = flow()
    segs(r)[0]["table"] = "日客流表"
    has(r, "table_unknown", f"{SEG}/0/table")


def test_keep_as_table_unknown():
    r = flow()
    segs(r)[3]["keep_as"]["table"] = "时段客流_合计"
    problems = run(r)
    assert [(p.code, p.path) for p in problems] == [("table_unknown", f"{SEG}/3/keep_as/table"),
                                                    ("table_unused", "/tables/2")]


def test_verify_against_table_unknown_and_invalid():
    r = flow()
    segs(r)[3]["verify"]["against_table"] = "时段明细"
    only(r, "table_unknown", f"{SEG}/3/verify/against_table")
    r["sheets"][0]["blocks"][0]["segments"][3]["verify"]["against_table"] = "日客流"
    only(r, "verify_against_invalid", f"{SEG}/3/verify/against_table")


def test_verify_value_must_be_the_value_column():
    r = flow()
    segs(r)[3]["verify"]["value"] = "人次"
    only(r, "column_unknown", f"{SEG}/3/verify/value")
    segs(r)[3]["verify"]["value"] = "起始小时"
    only(r, "verify_against_invalid", f"{SEG}/3/verify/value")


@pytest.mark.parametrize("key", ["销 量", "Qty"])
def test_units_key_must_be_a_column_verbatim(key):
    r = list_recipe()
    if key == "Qty":
        r["sheets"][0]["blocks"][0]["columns"][2]["name"] = "qty"
    r["tables"][0]["units"][key] = "件"
    only(r, "column_unknown", f"/tables/0/units/{key}", facts=None)


def test_units_key_with_extra_space_in_flow():
    r = flow()
    units = table(r, "日客流")["units"]
    units["全日客流 "] = units.pop("全日客流")
    has(r, "column_unknown", "/tables/0/units/全日客流 ")


def test_grain_column_unknown():
    r = flow()
    table(r, "时段客流")["grain"] = ["日期", "时段名"]
    only(r, "column_unknown", "/tables/1/grain/1")


def test_relation_columns_and_types():
    r = flow()
    r["relations"][0]["parts"] = ["分区甲", "分区丁"]
    has(r, "column_unknown", "/relations/0/parts/1")
    r = flow()
    r["relations"][0]["total"] = "日期"
    has(r, "relation_invalid", "/relations/0/total", facts=None)
    r = flow()
    r["relations"][0]["table"] = "日客流表"
    only(r, "table_unknown", "/relations/0/table", facts=None)


def test_not_comparable_by_must_exist_on_both_sides():
    r = flow()
    r["relations"][1]["by"] = "时段"
    only(r, "column_unknown", "/relations/1/by")


def test_relation_ids_unique():
    r = flow()
    r["relations"][1]["id"] = "R1"
    only(r, "relation_invalid", "/relations/1/id")


def test_total_row_label_column_must_exist():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["rows"]["total_row"]["label_column"] = "区域"
    only(r, "column_unknown", "/sheets/0/blocks/0/rows/total_row/label_column", facts=None)


def ascii_flow() -> dict:
    """参考配方换成英文表名、列名（标签和分段 id 不动，FACTS 照样能认领）。

    「差一点」的引用要两种各造一个才能把逐字比较钉死：只差 ASCII 大小写的（name_key 相同、match_key 不同）
    和只差空白的（match_key 相同、name_key 不同）。中文名没有大小写，前一种只能用英文名造。
    """
    r = flow()
    s = segs(r)
    r["sheets"][0]["blocks"][0]["axis"]["name"] = "Day"
    s[0]["table"] = "Daily"
    s[0]["measures"] = {"全日客流（人次）": "Total", "分区甲（人次）": "PartA", "分区乙（人次）": "PartB"}
    for seg in s[1:3]:
        seg.update(table="Hourly", value="Flow", const={"Kind": seg["const"]["时段类别"]})
        seg["dim"].update(name="Slot", derive={"StartH": "start", "EndH": "end"})
    s[3]["verify"].update(against_table="Hourly", value="Flow")
    s[3]["keep_as"] = {"table": "Hourly_total", "dim": "Item", "derive": {"StartH": "start", "EndH": "end"},
                       "value": "Flow"}
    r["tables"] = [
        {"name": "Daily", "grain": ["Day"], "units": {"Total": "人次", "PartA": "人次", "PartB": "人次"}},
        {"name": "Hourly", "grain": ["Day", "Slot"], "units": {"Flow": "人次"}},
        {"name": "Hourly_total", "kind": "reported_total", "grain": ["Day", "Item"], "units": {"Flow": "人次"}},
    ]
    r["relations"] = [
        {"id": "R1", "kind": "sum_eq", "table": "Daily", "total": "Total", "parts": ["PartA", "PartB"], "claims": "F1"},
        {"id": "R2", "kind": "not_comparable", "a": {"table": "Hourly", "value": "Flow"},
         "b": {"table": "Daily", "value": "Total"}, "by": "Day", "claims": "F2"},
    ]
    return r


def ascii_list() -> dict:
    r = list_recipe()
    block = r["sheets"][0]["blocks"][0]
    block["columns"][0]["name"] = "Region"
    block["rows"]["total_row"]["label_column"] = "Region"
    r["tables"][0]["grain"] = ["Region", "产品"]
    return r


#: 差一点的写法：只差大小写（name_key 当成同一个）、多一个空格（match_key 当成同一个）
NEAR = {"case": str.lower, "space": lambda s: s + " "}


def _get(doc, pointer: str):
    for part in pointer.split("/")[1:]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


def test_ascii_variants_have_no_problems():
    assert run(ascii_flow()) == []
    assert run(ascii_list(), facts=None) == []


@pytest.mark.parametrize("near", NEAR)
@pytest.mark.parametrize("base,pointer,expected", [
    # 关系里的表和列（table_ref、numeric_col、not_comparable.by）
    (ascii_flow, "/relations/0/table", [("table_unknown", "/relations/0/table")]),
    (ascii_flow, "/relations/0/total", [("column_unknown", "/relations/0/total")]),
    (ascii_flow, "/relations/0/parts/0", [("column_unknown", "/relations/0/parts/0")]),
    (ascii_flow, "/relations/1/a/table", [("table_unknown", "/relations/1/a/table")]),
    (ascii_flow, "/relations/1/b/table", [("table_unknown", "/relations/1/b/table")]),
    (ascii_flow, "/relations/1/a/value", [("column_unknown", "/relations/1/a/value")]),
    (ascii_flow, "/relations/1/b/value", [("column_unknown", "/relations/1/b/value")]),
    (ascii_flow, "/relations/1/by", [("column_unknown", "/relations/1/by")]),
    # 主键
    (ascii_flow, "/tables/1/grain/1", [("column_unknown", "/tables/1/grain/1")]),
    # 合计分段的核对基表、核对的值列、另存的表
    (ascii_flow, f"{SEG}/3/verify/against_table", [("table_unknown", f"{SEG}/3/verify/against_table")]),
    (ascii_flow, f"{SEG}/3/verify/value", [("column_unknown", f"{SEG}/3/verify/value")]),
    (ascii_flow, f"{SEG}/3/keep_as/table", [("table_unknown", f"{SEG}/3/keep_as/table"),
                                            ("table_unused", "/tables/2")]),
    # 分段写入的表：日客流一改，表清单里的 Daily 没人写，关系里的 Daily 也就不是配方产出的表
    (ascii_flow, f"{SEG}/0/table", [("table_unknown", f"{SEG}/0/table"), ("table_unused", "/tables/0"),
                                    ("table_unknown", "/relations/0/table"),
                                    ("table_unknown", "/relations/1/b/table")]),
    # 列表合计行的标签列
    (ascii_list, "/sheets/0/blocks/0/rows/total_row/label_column",
     [("column_unknown", "/sheets/0/blocks/0/rows/total_row/label_column")]),
])
def test_internal_references_are_verbatim(near, base, pointer, expected):
    """规则 9：配方内的引用逐字比较。每类引用各改成「差一点」的写法，必须报出来、且只报这些。"""
    r = base()
    wrong = NEAR[near](_get(r, pointer))
    r = apply_patch(r, [{"op": "replace", "path": pointer, "value": wrong}])
    problems = run(r, facts=None)
    assert [(p.code, p.path) for p in problems] == expected, [(p.code, p.path, p.message) for p in problems]


@pytest.mark.parametrize("near", NEAR)
def test_verify_against_table_must_be_the_target_table_verbatim(near):
    """核对基表在表清单里、与紧邻分段写入的表只差大小写或空格：仍不是那张表（名字本身另报撞名或不合规）。"""
    r = ascii_flow()
    other = NEAR[near]("Hourly")
    r["tables"].append({"name": other})
    segs(r)[3]["verify"]["against_table"] = other
    has(r, "verify_against_invalid", f"{SEG}/3/verify/against_table", facts=None)


def test_table_kind_must_match_writer():
    r = flow()
    table(r, "时段客流_表内合计")["kind"] = "data"
    only(r, "table_kind_mismatch", "/tables/2/kind")


def test_declared_table_without_writer():
    r = flow()
    r["tables"].append({"name": "多余的表"})
    only(r, "table_unused", "/tables/3")


# ==========================================================================
# 规则 10：结构一致
# ==========================================================================


def test_measures_keys_half_width_brackets():
    r = flow()
    segs(r)[0]["measures"] = {"全日客流(人次)": "全日客流", "分区甲（人次）": "分区甲", "分区乙（人次）": "分区乙"}
    # 只报根因：引用「全日客流」的单位、关系不再连带报「列不存在」
    p = only(r, "measures_keys_mismatch", f"{SEG}/0/measures")
    assert "全日客流(人次)" in p.message


def test_table_shape_conflict():
    r = flow()
    segs(r)[2]["value"] = "人次数"
    only(r, "table_shape_conflict", f"{SEG}/2")


def test_derived_must_follow_a_dimension_with_stop_parser():
    r = flow()
    segs(r)[2]["stop_parser"] = None
    only(r, "derived_after_invalid", f"{SEG}/3/locate/segment")
    r = flow()
    segs(r)[3]["locate"]["segment"] = "日客流"
    only(r, "derived_after_invalid", f"{SEG}/3/locate/segment")
    r = flow()
    segs(r)[3]["locate"]["segment"] = "不存在"
    only(r, "derived_after_invalid", f"{SEG}/3/locate/segment")
    r = flow()
    segs(r)[3]["locate"] = {"by": "labels"}
    only(r, "derived_after_invalid", f"{SEG}/3/locate/by")


def test_locate_rules_for_measures_and_dimension():
    r = flow()
    segs(r)[0]["locate"] = {"by": "after", "segment": "日间"}
    has(r, "locate_invalid", f"{SEG}/0/locate/by")
    r = flow()
    segs(r)[1]["locate"] = {"by": "section_title"}
    has(r, "locate_invalid", f"{SEG}/1/locate/title")


def test_crosstab_grain_must_contain_axis():
    r = flow()
    table(r, "日客流")["grain"] = []
    only(r, "grain_invalid", "/tables/0/grain")


def test_derive_role_duplicate():
    r = flow()
    segs(r)[3]["keep_as"]["derive"] = {"起始小时": "start", "结束小时": "start"}
    only(r, "derive_role_duplicate", f"{SEG}/3/keep_as/derive")


def test_derive_only_for_hour_range():
    r = flow()
    for seg in segs(r)[1:3]:
        seg["dim"]["parser"] = "text"
    has(r, "derive_invalid", f"{SEG}/1/dim/derive")


def test_overlapping_dimension_segments_need_a_distinguishing_const_in_grain():
    r = flow()
    segs(r)[1]["labels"]["expect"].append("18-19")
    only(r, "grain_invalid", "/tables/1/grain")
    table(r, "时段客流")["grain"] = ["日期", "时段", "时段类别"]
    assert run(r) == []


def test_overlapping_segments_with_same_pick_still_invalid():
    r = flow()
    segs(r)[1]["labels"]["expect"].append("18-19")
    segs(r)[2]["const"]["时段类别"]["pick"] = "夜间时段客流"
    table(r, "时段客流")["grain"] = ["日期", "时段", "时段类别"]
    assert run(r) == []
    segs(r)[1]["locate"]["title"] = "夜间时段客流（人次）"
    segs(r)[1]["const"]["时段类别"]["pick"] = "夜间时段客流"
    has(r, "grain_invalid", "/tables/1/grain")


def test_covers_context_and_year_need_a_context():
    r = flow()
    r["sheets"][0]["context"] = []
    problems = run(r)
    assert [(p.code, p.path) for p in problems] == [
        ("covers_without_context", "/sheets/0/blocks/0/axis/checks"),
        ("covers_without_context", "/sheets/0/blocks/0/axis/year_from")]
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]
    r["sheets"][0]["blocks"][0]["axis"]["year_from"] = None
    assert run(r) == []


# ==========================================================================
# 规则 11：认领（计数与语义）
# ==========================================================================


def test_fact_unclaimed():
    r = flow()
    r["relations"][0]["claims"] = None
    p = only(r, "fact_unclaimed", "/relations")
    assert p.message == "系统发现的关系「第 5 行 = 第 6 行 + 第 7 行」没有被认领：请登记为每期核对，或说明不登记的理由"


def test_fact_claimed_twice():
    r = flow()
    r["relations"].append({"id": "R3", "kind": "sum_eq", "table": "日客流", "total": "全日客流",
                           "parts": ["分区乙", "分区甲"], "claims": "F1"})
    only(r, "fact_claimed_twice", "/relations/2/claims")


def test_claim_of_unknown_fact():
    r = flow()
    r["relations"].append({"id": "R3", "kind": "dismissed", "claims": "F9", "reason": "巧合"})
    only(r, "fact_claim_mismatch", "/relations/2/claims")


def test_claim_with_wrong_members():
    r = flow()
    r["relations"][0].update(total="分区甲", parts=["全日客流", "分区乙"])
    p = only(r, "fact_claim_mismatch", "/relations/0/claims")
    assert "「全日客流（人次）」" in p.message


def test_claim_with_wrong_kind():
    r = flow()
    r["relations"][0], r["relations"][1] = (
        {"id": "R1", "kind": "sum_eq", "table": "日客流", "total": "全日客流", "parts": ["分区甲", "分区乙"], "claims": "F2"},
        {**r["relations"][1], "claims": "F1"})
    problems = run(r)
    assert sorted((p.code, p.path) for p in problems) == [("fact_claim_mismatch", "/relations/0/claims"),
                                                          ("fact_claim_mismatch", "/relations/1/claims")]


def test_not_comparable_sides_must_match_fact():
    r = flow()
    rel = r["relations"][1]
    rel["a"], rel["b"] = rel["b"], rel["a"]
    only(r, "fact_claim_mismatch", "/relations/1/claims")
    r = flow()
    r["relations"][1]["b"]["value"] = "分区甲"
    only(r, "fact_claim_mismatch", "/relations/1/claims")


def test_sum_eq_claim_must_be_on_the_fact_segment_table():
    """另一张表的列名碰巧相同：R1 指过去，按 Fact 那个分段的 measures 反查列名能对上，但表不是那张表。"""
    block = {"id": "交叉表2", "layout": "crosstab", "axis": {"year_from": None, "checks": ["contiguous"]},
             "segments": [{"id": "附表", "role": "measures", "table": "附表客流", "locate": {"by": "labels"},
                           "labels": {"expect": ["全日客流", "分区甲", "分区乙"]},
                           "measures": {"全日客流": "全日客流", "分区甲": "分区甲", "分区乙": "分区乙"}}]}
    r = _second_sheet(flow(), block, [{"name": "附表客流", "grain": ["日期"]}])
    assert run(r) == []
    r["relations"][0]["table"] = "附表客流"
    assert run(r, facts=None) == []
    p = only(r, "fact_claim_mismatch", "/relations/0/claims")
    assert "「日客流」" in p.message and "「附表客流」" in p.message


def test_not_comparable_a_value_must_be_the_value_column():
    """F2 说的是日间、夜间「各行之和」，加总的是值列；换成派生列「起始小时」（也是数字列）口径就写错了。"""
    r = flow()
    r["relations"][1]["a"]["value"] = "起始小时"
    assert run(r, facts=None) == []
    p = only(r, "fact_claim_mismatch", "/relations/1/claims")
    assert "值列「客流」" in p.message and "「起始小时」" in p.message


def _rename_dimension_segments(r: dict, day: str | None, night: str | None) -> dict:
    s = segs(r)
    if day:
        s[1]["id"] = day
    if night:
        s[2]["id"] = night
        s[3]["locate"]["segment"] = night
    return r


@pytest.mark.parametrize("day,night", [("白天", "晚上"), ("白天", None), (None, "晚上")])
def test_renamed_dimension_segments_are_found_in_the_b_block(day, night):
    """F2 的 a 只记分段 id（起草时取 pick：日间、夜间）；改名后按 b 那个 measures 段同块的全部 dimension 段认，
    照样核对内容，而不是报一条看不懂的认领错误。"""
    r = _rename_dimension_segments(flow(), day, night)
    assert run(r) == []
    r["relations"][1]["a"]["value"] = "起始小时"
    only(r, "fact_claim_mismatch", "/relations/1/claims")


def test_renamed_dimension_segments_with_a_different_count_do_not_match():
    """改名后同块的 dimension 段个数与 a 不同（少了日间）：内容变了，不按同块认，照样报对不上。"""
    r = flow()
    del segs(r)[1]
    seg = segs(r)[1]
    seg["id"], seg["const"] = "晚上", {}
    segs(r)[2]["locate"]["segment"] = "晚上"
    assert run(r, facts=None) == []
    p = only(r, "fact_claim_mismatch", "/relations/1/claims")
    assert "（配方里找不到这些分段）" in p.message


def test_dismissed_claims_any_fact():
    r = flow()
    r["relations"][1] = {"id": "R2", "kind": "dismissed", "claims": "F2", "reason": "两块口径不同，不作关系登记"}
    assert run(r) == []


def test_claims_not_checked_without_facts():
    r = flow()
    r["relations"][0]["claims"] = None
    r["relations"][1]["claims"] = "F9"
    assert run(r, facts=None) == []


def test_renamed_measures_segment_is_found_by_labels():
    """起草时分段 id 是表名，改名后 Fact.detail.segment 对不上：按标签找到唯一的分段，照样核对内容。"""
    r = flow()
    segs(r)[0]["id"] = "按日"
    assert run(r) == []
    r["relations"][0].update(total="分区甲", parts=["全日客流", "分区乙"])
    only(r, "fact_claim_mismatch", "/relations/0/claims")


def test_new_fact_numbering_is_not_an_empty_claim():
    """改配方后重新探测：F1 现在是「甲 + 乙 + 丙」（D13），旧的 R1 claims=F1 报出来，而不是空认领通过。"""
    new_f1 = Fact("F1", "sum_eq", "客流汇总", "第 5 行 = 第 6 行 + 第 7 行 + 第 8 行（30 列中 30 列成立）",
                  {"segment": "日客流", "total": "全日客流（人次）",
                   "parts": ["分区甲（人次）", "分区乙（人次）", "分区丙（人次）"], "rows": [5, 6, 7, 8]})
    only(flow(), "fact_claim_mismatch", "/relations/0/claims", facts=DraftFacts(facts=[new_f1, F2]))


# ==========================================================================
# 规则 12：说明散文
# ==========================================================================


def test_note_ok():
    r = flow()
    table(r, "日客流")["note"] = "粒度：每个{列:日期}。{列:全日客流} 等于 {列:分区甲} 与 {列:分区乙} 之和，单位{单位:人次}"
    assert run(r) == []


@pytest.mark.parametrize("note", ["导入时已逐日核对，共 31 天", "数据见 C5 格", "约 5000 人次"])
def test_note_with_numbers_or_coordinates(note):
    r = flow()
    table(r, "日客流")["note"] = note
    has(r, "note_numbers", "/tables/0/note")
    assert {p.code for p in run(r)} == {"note_numbers"}


@pytest.mark.parametrize("note", ["{列:增长30%}，请据此推算", "{列:客流} 不能相加", "{表:第2期}只有一半", "按{单位:箱}计"])
def test_note_with_unknown_token(note):
    r = flow()
    table(r, "日客流")["note"] = note
    has(r, "note_token_unknown", "/tables/0/note")


def test_ai_note_forbidden():
    r = flow()
    table(r, "日客流")["note"] = "按{列:日期}逐日记录"
    only(r, "ai_note_forbidden", "/tables/0/note", origin="ai")
    assert run(r, origin="rules") == []


# ==========================================================================
# 规则 13：单位与标签一致
# ==========================================================================


def test_unit_label_conflict_wrong_unit():
    r = list_recipe()
    r["tables"][0]["units"]["金额"] = "元"
    p = only(r, "unit_label_conflict", "/tables/0/units/金额", facts=None)
    assert "「金额（万元）」" in p.message and "万元" in p.message


def test_unit_label_conflict_missing_unit():
    r = list_recipe()
    del r["tables"][0]["units"]["金额"]
    p = only(r, "unit_label_conflict", "/tables/0/units", facts=None)
    assert p.message == "表「销售」：原表表头「金额（万元）」写着单位「万元」，列「金额」的单位也必须写「万元」"


def test_unit_label_conflict_unit_outside_vocabulary():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["columns"][2]["header"] = "销量（箱）"
    assert run(r, facts=None) == []
    r["tables"][0]["units"]["销量"] = "件"
    only(r, "unit_label_conflict", "/tables/0/units/销量", facts=None)


def test_unit_label_conflict_measures_and_section_title():
    r = flow()
    table(r, "日客流")["units"]["分区甲"] = "人"
    only(r, "unit_label_conflict", "/tables/0/units/分区甲")
    r = flow()
    table(r, "时段客流")["units"]["客流"] = "人"
    only(r, "unit_label_conflict", "/tables/1/units/客流")


def test_unit_label_conflict_list_total_table():
    """列表合计表的数字列就是那几个表头：主表写对了、合计表写成「元」或漏写，同样要报。"""
    r = list_recipe()
    r["tables"][1]["units"]["金额"] = "元"
    p = only(r, "unit_label_conflict", "/tables/1/units/金额", facts=None)
    assert p.message.startswith("表「销售_表内合计」：") and "「金额（万元）」" in p.message
    del r["tables"][1]["units"]["金额"]
    only(r, "unit_label_conflict", "/tables/1/units", facts=None)


def test_unit_label_conflict_list_total_table_unit_outside_vocabulary():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["columns"][2]["header"] = "销量（箱）"
    r["tables"][1]["units"]["销量"] = "件"
    only(r, "unit_label_conflict", "/tables/1/units/销量", facts=None)


def test_list_without_kept_total_has_no_total_table_units():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["rows"]["total_row"]["keep_as"] = None
    del r["tables"][1]
    assert run(r, facts=None) == []


def test_unit_label_conflict_crosstab_total_table():
    """表内合计和它紧跟的夜间明细同在「夜间时段客流（人次）」下：合计表的客流写错或漏写，同样要报。"""
    r = flow()
    table(r, "时段客流_表内合计")["units"]["客流"] = "人"
    p = only(r, "unit_label_conflict", "/tables/2/units/客流")
    assert p.message.startswith("表「时段客流_表内合计」：") and "「夜间时段客流（人次）」" in p.message
    del table(r, "时段客流_表内合计")["units"]["客流"]
    only(r, "unit_label_conflict", "/tables/2/units")


# ==========================================================================
# 规范形式与哈希
# ==========================================================================


def _strip_defaults(r: dict) -> dict:
    """手写的省略写法：去掉参考配方里显式写出的默认值。"""
    r = copy.deepcopy(r)
    for k in ("mode", "other_visible_sheets"):
        r.pop(k)
    sheet = r["sheets"][0]
    sheet["match"].pop("fallback")
    sheet.pop("hidden")
    ctx = sheet["context"][0]
    for k in ("id", "kind", "parser", "cross_check"):
        ctx.pop(k)
    block = sheet["blocks"][0]
    block.pop("axis")
    block.pop("label_offset")
    for k in ("type", "blank", "text_number", "formula"):
        block["values"].pop(k)
    block["values"]["placeholders"][0].pop("meaning")
    segs(r)[3].pop("labels_parser")
    for t in r["tables"]:
        t.pop("note")
        if t["kind"] == "data":
            t.pop("kind")
    return r


def test_hash_ignores_explicit_defaults():
    assert recipe_sha256(parse_recipe(_strip_defaults(FLOW))) == recipe_sha256(parse_recipe(flow()))


def test_hash_ignores_axis_checks_order_and_key_order():
    r = flow()
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = ["covers_context", "contiguous"]
    assert recipe_sha256(parse_recipe(r)) == recipe_sha256(parse_recipe(flow()))
    shuffled = json.loads(json.dumps(flow(), sort_keys=True))
    shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}
    assert recipe_sha256(parse_recipe(shuffled)) == recipe_sha256(parse_recipe(flow()))


def test_hash_changes_with_content():
    r = flow()
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]
    assert recipe_sha256(parse_recipe(r)) != recipe_sha256(parse_recipe(flow()))


def test_canonical_form_is_compact_and_round_trips():
    recipe = parse_recipe(flow())
    canon = canonical_recipe(recipe)
    assert "mode" not in canon and "hidden" not in canon["sheets"][0]
    assert "axis" not in canon["sheets"][0]["blocks"][0]
    assert parse_recipe(canon) == recipe
    assert canonical_recipe(parse_recipe(canon)) == canon
    text = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert recipe_sha256(recipe) == hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==========================================================================
# apply_patch
# ==========================================================================


def test_apply_patch_add_replace_remove():
    doc = {"a": {"b": 1}, "l": [1, 2], "x/y": {"~k": 0}}
    out = apply_patch(doc, [
        {"op": "add", "path": "/a/c", "value": [1]},
        {"op": "add", "path": "/l/1", "value": 9},
        {"op": "add", "path": "/l/-", "value": 3},
        {"op": "replace", "path": "/a/b", "value": {"n": 2}},
        {"op": "remove", "path": "/l/0"},
        {"op": "replace", "path": "/x~1y/~0k", "value": 5},
        {"op": "add", "path": "/a/b", "value": "overwritten"},
    ])
    assert out == {"a": {"b": "overwritten", "c": [1]}, "l": [9, 2, 3], "x/y": {"~k": 5}}
    assert doc == {"a": {"b": 1}, "l": [1, 2], "x/y": {"~k": 0}}


def test_apply_patch_question_effects_on_recipe():
    """起草问题「不登记」的 effects：把关系整条换成 dismissed；占位符选「不是」：删掉占位符。"""
    out = apply_patch(flow(), [
        {"op": "replace", "path": "/relations/1",
         "value": {"id": "R2", "kind": "dismissed", "claims": "F2", "reason": "口径不同"}},
        {"op": "remove", "path": "/sheets/0/blocks/0/values/placeholders/0"},
        {"op": "replace", "path": f"{SEG}/2/const/时段类别/pick", "value": "夜间时段客流"},
    ])
    assert out["relations"][1]["kind"] == "dismissed"
    assert out["sheets"][0]["blocks"][0]["values"]["placeholders"] == []
    assert segs(out)[2]["const"]["时段类别"]["pick"] == "夜间时段客流"
    assert segs(FLOW)[2]["const"]["时段类别"]["pick"] == "夜间"
    assert run(out) == []


def test_apply_patch_root_replace():
    assert apply_patch({"a": 1}, [{"op": "replace", "path": "", "value": {"b": 2}}]) == {"b": 2}


@pytest.mark.parametrize("ops", [
    [{"op": "replace", "path": "/nope", "value": 1}],
    [{"op": "remove", "path": "/a/zz"}],
    [{"op": "add", "path": "/l/5", "value": 1}],
    [{"op": "replace", "path": "/l/2", "value": 1}],
    [{"op": "remove", "path": "/l/01"}],
    [{"op": "remove", "path": "/l/x"}],
    [{"op": "add", "path": "/q/r", "value": 1}],
    [{"op": "add", "path": "a", "value": 1}],
    [{"op": "move", "from": "/a", "path": "/b"}],
    [{"op": "add", "path": "/b"}],
    [{"path": "/a"}],
    [{"op": "remove", "path": ""}],
    [{"op": "replace", "path": "", "value": [1]}],
    [{"op": "add", "path": "/a/b/c", "value": 1}],
    ["not an op"],
])
def test_apply_patch_errors(ops):
    doc = {"a": {"b": 1}, "l": [1, 2]}
    with pytest.raises(ValueError):
        apply_patch(doc, ops)
    assert doc == {"a": {"b": 1}, "l": [1, 2]}


def test_apply_patch_is_all_or_nothing():
    doc = {"a": 1}
    with pytest.raises(ValueError):
        apply_patch(doc, [{"op": "replace", "path": "/a", "value": 2}, {"op": "remove", "path": "/zz"}])
    assert doc == {"a": 1}


def test_many_unknown_unit_keys_on_a_wide_table_validate_quickly():
    """SE-5：200 列的宽表上 2 万个不存在的单位键：_near 按名字列表建一次键索引，不再逐键对全表各列重算 NFKC
    （改之前约 11 秒）。提示照旧：大小写、全半角不同的名字仍会点出来。"""
    import time

    from app.data.recipe import validate_recipe

    data = copy.deepcopy(FLOW)
    seg = data["sheets"][0]["blocks"][0]["segments"][0]
    labels = [f"指标{i}（人次）" for i in range(200)]
    seg["labels"]["expect"] = labels
    seg["measures"] = {lab: f"指标{i}" for i, lab in enumerate(labels)}
    data["tables"][0]["units"] = {f"指标{i}": "人次" for i in range(200)}
    data["relations"] = []
    data["tables"][0]["units"].update({f"col_{i}_x": "人次" for i in range(20000)})
    data["tables"][0]["units"]["指标７"] = "人次"          # 全角数字：提示「有「指标7」」
    t = time.perf_counter()
    _parsed, probs = validate_recipe(data)
    assert time.perf_counter() - t < 4
    assert sum(1 for p in probs if p.code == "column_unknown") == 20001
    hint = next(p.message for p in probs if "指标７" in p.message)
    assert "（有「指标7」，名字必须逐字相同）" in hint
