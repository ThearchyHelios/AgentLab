"""配方静态校验的期 3 改动（WP-2，P3-SPEC 2.2、3.3、9.2）：按期累积的资格、沿用已确认的常量（L3）、认领只查改动过的
部分、period_literal 的扩展范围、忽略规则的 ignore_conflict / ignore_duplicate。

只用契约里的参考配方（flow_recipe.json）和手写的小配方，不读 Excel。每条规则一正一反。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.data.recipe import canonical_recipe, validate_recipe
from app.data.recipe_types import DraftFacts, Fact, Recipe

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))
SEG = "/sheets/0/blocks/0/segments"
F1 = Fact("F1", "sum_eq", "客流汇总", "第 5 行 = 第 6 行 + 第 7 行（30 列中 30 列成立）",
          {"segment": "日客流", "total": "全日客流（人次）", "parts": ["分区甲（人次）", "分区乙（人次）"], "rows": [5, 6, 7]})
F2 = Fact("F2", "not_equal_sum", "客流汇总", "日间、夜间各行之和 与 第 5 行（30 列中 0 列相等）",
          {"a": ["日间", "夜间"], "b_segment": "日客流", "b": "全日客流（人次）", "equal": 0, "checked": 30})
#: D13 加了分区丙之后本期的系统发现：全日 = 甲 + 乙 + 丙
F1_C = Fact("F1", "sum_eq", "客流汇总", "第 5 行 = 第 6 行 + 第 7 行 + 第 8 行（30 列中 30 列成立）",
            {"segment": "日客流", "total": "全日客流（人次）",
             "parts": ["分区甲（人次）", "分区乙（人次）", "分区丙（人次）"], "rows": [5, 6, 7, 8]})


def flow() -> dict:
    return copy.deepcopy(FLOW)


def segs(r: dict) -> list[dict]:
    return r["sheets"][0]["blocks"][0]["segments"]


def codes(data, **kw) -> list[tuple[str, str]]:
    _r, problems = validate_recipe(data, **kw)
    return [(p.code, p.path) for p in problems]


def list_recipe() -> dict:
    return {
        "recipe_format": "agentlab-recipe/2",
        "sheets": [{"id": "s1", "match": {"name": "月报"}, "blocks": [{
            "id": "列表1", "layout": "list", "table": "销售",
            "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                        {"header": "销量", "name": "销量", "type": "INTEGER"}]}]}],
        "tables": [{"name": "销售", "grain": ["地区"]}],
    }


# ==========================================================================
# 2.2 按期累积的资格（mode_unsupported 不再产生）
# ==========================================================================


def test_accumulate_eligible_reference_recipe_passes():
    r = flow()
    r["mode"] = "accumulate"
    assert codes(r, facts=DraftFacts(facts=[F1, F2])) == []


def test_accumulate_without_covers_context_reports_unkeyed_on_every_written_table():
    r = flow()
    r["mode"] = "accumulate"
    r["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]
    got = codes(r)
    assert ("accumulate_unkeyed", "/tables/0") in got and ("accumulate_unkeyed", "/tables/2") in got
    assert all(c != "mode_unsupported" for c, _ in got)
    # 每期替换时同一份配方只报 checks_relaxed 那一类（确认项），静态校验不报资格
    r["mode"] = "replace"
    assert codes(r) == []


def test_accumulate_list_table_message_does_not_promise_future_versions():
    r = list_recipe()
    r["mode"] = "accumulate"
    r["sheets"][0]["context"] = [{}]
    _rec, problems = validate_recipe(r)
    assert [p.code for p in problems] == ["accumulate_unkeyed"]
    assert "列表形态的表目前只支持每期替换" in problems[0].message and "后续版本" not in problems[0].message


# ==========================================================================
# 3.3 沿用已确认的常量（L3）
# ==========================================================================


def d09(pick: str = "日间") -> dict:
    """D09：日间的分段标题改成「日间分时段客流（人次）」，常量 pick 照旧或改掉。"""
    r = flow()
    segs(r)[1]["locate"]["title"] = "日间分时段客流（人次）"
    segs(r)[1]["const"]["时段类别"]["pick"] = pick
    return r


def test_const_rule_a_candidate_word_still_passes():
    # (a) 期 2 原规则：pick 是新标题的候选词（「日间分」）
    assert codes(d09("日间分"), origin="manual") == []


def test_const_rule_a_negative_without_base():
    # 没有 base、不是重放：沿用「日间」不在候选词里，照期 2 报 const_not_candidate
    assert codes(d09(), origin="manual") == [("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


def test_const_rule_b_carried_from_base_passes():
    # (b) 沿用：现行配方里同一分段同一常量列的 pick 逐字相同，且仍是新标题的子串
    assert codes(d09(), origin="manual", base=flow()) == []
    # base 给 canonical 形式也一样
    assert codes(d09(), origin="manual", base=canonical_recipe(Recipe.model_validate(flow()))) == []


def test_const_rule_b_negative_when_base_pick_differs():
    base = flow()
    segs(base)[1]["const"]["时段类别"]["pick"] = "白天"
    segs(base)[1]["locate"]["title"] = "白天时段客流（人次）"
    assert codes(d09(), origin="manual", base=base) == [("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


def test_const_rule_b_negative_when_pick_not_in_new_title():
    r = flow()
    segs(r)[1]["locate"]["title"] = "白天时段客流（人次）"
    # 现行配方的 pick「日间」逐字相同，但已不是新标题的子串：不放行
    assert codes(r, origin="manual", base=flow()) == [("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


def test_const_rule_b_negative_when_base_segment_id_differs():
    base = flow()
    segs(base)[1]["id"] = "白天段"
    segs(base)[3]["locate"]["segment"] = "夜间"
    assert codes(d09(), origin="manual", base=base) == [("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


def test_const_rule_c_replay_substring_passes_and_negative():
    # (c) 重放：修复后的配方下个月按 replay 校验、又没有 base
    assert codes(d09(), origin="replay") == []
    r = flow()
    segs(r)[1]["locate"]["title"] = "白天时段客流（人次）"
    assert codes(r, origin="replay") == [("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


@pytest.mark.parametrize("kw", [{"origin": "replay"}, {"origin": "manual", "base": FLOW}])
def test_const_with_digits_still_period_literal(kw):
    r = flow()
    segs(r)[1]["locate"]["title"] = "日间9时段客流（人次）"
    segs(r)[1]["const"]["时段类别"]["pick"] = "日间9"
    base = copy.deepcopy(r) if "base" in kw else None
    kw = {**kw, "base": base} if base is not None else kw
    assert codes(r, **kw) == [("period_literal", f"{SEG}/1/const/时段类别/pick")]


# ==========================================================================
# 3.3 第 11 条：认领只查改动过的部分
# ==========================================================================


def test_unchanged_measures_do_not_check_claims_with_base():
    """D04 的修复改的是 dimension 段：本期 R1 有一天不成立、系统发现里没有 F1 时，R1 的认领不查。"""
    r = flow()
    segs(r)[1]["labels"]["expect"] = segs(r)[1]["labels"]["expect"][1:]   # 去掉 7-8
    # 本期只发现了口径不同那一条，编号还错开成了 F1
    f_ne = Fact("F1", "not_equal_sum", "客流汇总", F2.text, dict(F2.detail))
    facts = DraftFacts(facts=[f_ne])
    assert codes(r, facts=facts, origin="manual", base=flow()) == []
    # 没有 base（首次导入）照期 2 全查
    got = codes(r, facts=facts, origin="manual")
    assert ("fact_claim_mismatch", "/relations/0/claims") in got


def test_changed_measures_still_check_claims_with_base():
    """D13 给日客流加了分区丙：R1 的成员落在改动过的段上，照查，对不上本期的新事实。"""
    r = flow()
    seg = segs(r)[0]
    seg["labels"]["expect"].append("分区丙（人次）")
    seg["measures"]["分区丙（人次）"] = "分区丙"
    r["tables"][0]["units"]["分区丙"] = "人次"
    got = codes(r, facts=DraftFacts(facts=[F1_C, F2]), origin="manual", base=flow())
    assert got == [("fact_claim_mismatch", "/relations/0/claims")]
    # 按事实更新成员之后通过
    r["relations"][0]["parts"] = ["分区甲", "分区乙", "分区丙"]
    assert codes(r, facts=DraftFacts(facts=[F1_C, F2]), origin="manual", base=flow()) == []


def test_measures_locate_change_alone_still_checks_claims_with_base():
    """measures 段只改了定位方式（expect、measures 都没变，改按分段标题定位）：仍算改动过的段（3.3 第 11 条的
    「locate 任一不同」），这一段上的关系认领照查。本期的发现里没有 R1 认领的那条 sum_eq（编号错开成口径不同那条）。"""
    r = flow()
    assert segs(r)[0]["locate"]["by"] == "labels"
    segs(r)[0]["locate"] = {"by": "section_title", "title": "日客流汇总", "segment": None}
    f_ne = Fact("F1", "not_equal_sum", "客流汇总", F2.text, dict(F2.detail))
    facts = DraftFacts(facts=[f_ne])
    got = codes(r, facts=facts, origin="manual", base=flow())
    assert ("fact_claim_mismatch", "/relations/0/claims") in got
    assert ("fact_claim_mismatch", "/relations/1/claims") in got
    # 对照：定位方式没变时这两条关系按重放处理，不查认领
    assert codes(flow(), facts=facts, origin="manual", base=flow()) == []


def test_fact_with_a_removed_label_is_located_through_the_base_recipe():
    """去掉了「分区乙」（① remove_label）：本期的事实 F1 还写着分区乙，分段 id 又是规则起草的写法，在工作配方里
    按 id、按全部标签都认不出。按现行配方认出它落在「日客流」上、这一段改动过，F1 在改动范围内：没人认领时报
    fact_unclaimed，不让它连同被删掉的关系一起不声不响地消失。"""
    r = flow()
    seg = segs(r)[0]
    seg["labels"]["expect"].remove("分区乙（人次）")
    del seg["measures"]["分区乙（人次）"]
    del r["tables"][0]["units"]["分区乙"]
    del r["relations"][0]
    drafted = Fact("F1", "sum_eq", "客流汇总", F1.text, {**F1.detail, "segment": "客流汇总_按日"})
    f_ne = Fact("F2", "not_equal_sum", "客流汇总", F2.text, dict(F2.detail))
    got = codes(r, facts=DraftFacts(facts=[drafted, f_ne]), origin="manual", base=flow())
    assert got == [("fact_unclaimed", "/relations")]
    # 分区甲、分区乙都换成了分区丙：工作配方里只剩「全日客流」一个标签对得上，过半命中也认不出，只能靠现行配方
    seg["labels"]["expect"] = ["全日客流（人次）", "分区丙（人次）"]
    seg["measures"] = {"全日客流（人次）": "全日客流", "分区丙（人次）": "分区丙"}
    r["tables"][0]["units"] = {"全日客流": "人次", "分区丙": "人次"}
    got = codes(r, facts=DraftFacts(facts=[drafted, f_ne]), origin="manual", base=flow())
    assert got == [("fact_unclaimed", "/relations")]


def test_fact_located_by_most_labels_when_neither_recipe_has_all_of_them():
    """同时加了「分区丙」、去掉了「分区乙」：事实「全日 = 甲 + 乙 + 丙」在工作配方和现行配方里都按全部标签认不出，
    按「过半标签命中」认出落在改动过的「日客流」上，没人认领时报 fact_unclaimed；只命中一个标签时不认，
    按重放处理（不在改动范围内，不报）。"""
    r = flow()
    seg = segs(r)[0]
    seg["labels"]["expect"] = ["全日客流（人次）", "分区甲（人次）", "分区丙（人次）"]
    seg["measures"] = {"全日客流（人次）": "全日客流", "分区甲（人次）": "分区甲", "分区丙（人次）": "分区丙"}
    r["tables"][0]["units"] = {"全日客流": "人次", "分区甲": "人次", "分区丙": "人次"}
    del r["relations"][0]
    f_ne = Fact("F2", "not_equal_sum", "客流汇总", F2.text, dict(F2.detail))
    mixed = Fact("F7", "sum_eq", "客流汇总", "…", {"segment": "客流汇总_按日", "total": "全日客流（人次）",
                                                    "parts": ["分区甲（人次）", "分区乙（人次）", "分区丙（人次）"]})
    assert codes(r, facts=DraftFacts(facts=[mixed, f_ne]), origin="manual", base=flow()) == [
        ("fact_unclaimed", "/relations")]
    stray = Fact("F7", "sum_eq", "客流汇总", "…", {"segment": "别处", "total": "全日客流（人次）",
                                                    "parts": ["别处甲", "别处乙"]})
    assert codes(r, facts=DraftFacts(facts=[stray, f_ne]), origin="manual", base=flow()) == []


def test_changed_relation_is_checked_even_if_segments_unchanged():
    r = flow()
    r["relations"][0]["claims"] = "F9"
    got = codes(r, facts=DraftFacts(facts=[F1, F2]), origin="manual", base=flow())
    assert ("fact_claim_mismatch", "/relations/0/claims") in got


def test_fact_on_changed_segment_must_be_claimed():
    """新发现落在改动过的 measures 段上、又没有人认领：fact_unclaimed 照报。"""
    r = flow()
    segs(r)[0]["labels"]["expect"].append("分区丙（人次）")
    segs(r)[0]["measures"]["分区丙（人次）"] = "分区丙"
    r["tables"][0]["units"]["分区丙"] = "人次"
    extra = Fact("F3", "sum_eq", "客流汇总", "第 8 行 = 第 6 行 + 第 7 行（30 列中 30 列成立）",
                 {"segment": "日客流", "total": "分区丙（人次）", "parts": ["分区甲（人次）", "分区乙（人次）"],
                  "rows": [8, 6, 7]})
    got = codes(r, facts=DraftFacts(facts=[F1, F2, extra]), origin="manual", base=flow())
    assert ("fact_unclaimed", "/relations") in got
    # 落在没改过的段上的新发现：不查
    got2 = codes(flow(), facts=DraftFacts(facts=[F1, F2, extra]), origin="manual", base=flow())
    assert got2 == []


def test_invalid_base_is_ignored():
    assert codes(d09(), origin="manual", base={"recipe_format": "x"}) == [
        ("const_not_candidate", f"{SEG}/1/const/时段类别/pick")]


# ==========================================================================
# 9.2 period_literal 的扩展：after_title 和忽略规则的锚点（origin 不是 replay 时）
# ==========================================================================


def test_after_title_with_digits_is_period_literal_except_replay():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["after_title"] = "2026年8月 销售月报"
    assert codes(r) == [("period_literal", "/sheets/0/blocks/0/after_title")]
    assert codes(r, origin="replay") == []
    r["sheets"][0]["blocks"][0]["after_title"] = "销售月报"
    assert codes(r) == []


@pytest.mark.parametrize("where,item,path", [
    ("ignore_rows", {"label": "注：9月补录", "reason": "补录不导入"}, "/sheets/0/blocks/0/ignore_rows/0/label"),
    ("ignore_columns", {"header": "2026合计", "reason": "合计列"}, "/sheets/0/blocks/0/ignore_columns/0/header"),
    ("ignore_outside", {"anchor": "9月补录说明", "reason": "说明行"}, "/sheets/0/ignore_outside/0/anchor"),
])
def test_ignore_anchor_with_digits_is_period_literal(where, item, path):
    r = flow()
    target = r["sheets"][0] if where == "ignore_outside" else r["sheets"][0]["blocks"][0]
    target[where] = [item]
    assert codes(r) == [("period_literal", path)]
    assert codes(r, origin="replay") == []
    item = {k: (v.replace("9", "").replace("2026", "") if k != "reason" else v) for k, v in item.items()}
    target[where] = [item]
    assert codes(r) == []


# ==========================================================================
# 9.2 ignore_conflict、ignore_duplicate
# ==========================================================================


def test_ignore_row_that_a_segment_expects_conflicts():
    r = flow()
    r["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "分区甲（人次）", "reason": "不要"}]
    assert codes(r) == [("ignore_conflict", "/sheets/0/blocks/0/ignore_rows/0/label")]
    # 时段按解析后的规范写法比：全角写法也算同一个标签
    r["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "８－９", "reason": "不要"}]
    assert ("ignore_conflict", "/sheets/0/blocks/0/ignore_rows/0/label") in codes(r)
    r["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "补录（人次）", "reason": "补录不导入"}]
    assert codes(r) == []


def test_crosstab_ignore_column_that_is_a_date_conflicts():
    r = flow()
    r["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "九月合计", "reason": "合计列"}]
    assert codes(r) == []
    r["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "九月合计", "reason": "合计列"},
                                                      {"header": "九月一日", "reason": "x"}]
    assert codes(r) == []
    r["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "九月合计", "reason": "合计列"},
                                                      {"header": "9月1日", "reason": "x"}]
    got = codes(r, origin="replay")
    assert got == [("ignore_conflict", "/sheets/0/blocks/0/ignore_columns/1/header")]


def test_list_ignore_column_same_as_imported_header_conflicts():
    r = list_recipe()
    r["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "销 量", "reason": "不要"}]
    assert codes(r) == [("ignore_conflict", "/sheets/0/blocks/0/ignore_columns/0/header")]
    r["sheets"][0]["blocks"][0]["ignore_columns"] = [{"header": "备注", "reason": "不要"}]
    assert codes(r) == []


@pytest.mark.parametrize("where,key,a,b", [
    ("ignore_rows", "label", "补录（人次）", "补录(人次)"),
    ("ignore_columns", "header", "合计", " 合 计 "),
    ("ignore_outside", "anchor", "补录说明", "补录说明"),
])
def test_ignore_duplicate(where, key, a, b):
    r = flow()
    target = r["sheets"][0] if where == "ignore_outside" else r["sheets"][0]["blocks"][0]
    target[where] = [{key: a, "reason": "一"}, {key: b, "reason": "二"}]
    prefix = "/sheets/0" if where == "ignore_outside" else "/sheets/0/blocks/0"
    assert codes(r) == [("ignore_duplicate", f"{prefix}/{where}/1/{key}")]


def test_new_problem_messages_do_not_expose_field_names():
    r = flow()
    r["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "分区甲（人次）", "reason": "x"},
                                                   {"label": "分区甲(人次)", "reason": "y"}]
    _rec, problems = validate_recipe(r)
    for p in problems:
        assert not any(k in p.message for k in ("ignore_rows", "label", "reason")), p.message
