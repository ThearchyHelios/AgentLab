"""修复按钮（recipe_fixes，WP-2，P3-SPEC 第 3 节）：九种提议各自出与不出的条件、补丁形状、应用后静态校验通过。

问题的 fix_args 按 3.2 的形状手写（执行器填写它是 WP-1 的事，这里不依赖）；干跑用期 2 的执行器
（recipe_engine.execute）。D04、D09、D13、D18 在期 2 执行器上就能验证「修复后干跑通过」；D16、D21 要用到忽略规则的
执行语义（WP-1），这里只验证补丁和静态校验，干跑留给集成验收（WP-8 A6b）。夹具全部合成、假名。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.data import recipe_engine, xlsx_cells, xlsx_scan
from app.data import recipe_fixes as F
from app.data.recipe import apply_patch, recipe_sha256, validate_recipe
from app.data.recipe_suggest import _measure_column, detect_facts
from app.data.recipe_types import FIX_KINDS, DraftFacts, Fact, FixProposal, Recipe
from tests.fixtures.xlsx.flow import flow_workbook

FLOW = Recipe.model_validate(json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json")
                                        .read_text(encoding="utf-8"))).model_dump(mode="json")
SEP = dt.date(2026, 9, 1)
SHEET = "客流汇总"
S = "/sheets/0/blocks/0/segments"


def full() -> dict[str, Any]:
    return copy.deepcopy(FLOW)


def book(variant: str):
    raw, fn = flow_workbook(SEP, 30, variant=variant)
    sc = xlsx_scan.scan(raw)
    grids = {s.name: xlsx_cells.read_grid(raw, sc, s.name, max_rows=500) for s in sc.sheets if s.state == "visible"}
    return raw, fn, sc, grids


def dry(variant: str, recipe: dict[str, Any]):
    raw, fn, sc, _ = book(variant)
    return recipe_engine.execute(Recipe.model_validate(recipe), raw, fn, sc, None)


def one(problem: dict[str, Any], recipe: dict[str, Any] | None = None) -> FixProposal:
    got = F.propose_fixes(recipe or full(), [problem], [], None)
    assert len(got) == 1, got
    return got[0]


def apply(recipe: dict[str, Any], p: FixProposal, option: str, reason: str | None = None) -> dict[str, Any]:
    r = F.fix_ops(recipe, p, option, reason)
    assert r.ok, r.problems
    new = apply_patch(recipe, r.ops)
    assert r.recipe_sha256_after == recipe_sha256(Recipe.model_validate(new))
    return new


def problems_after(new: dict[str, Any], facts: DraftFacts | None = None) -> list[tuple[str, str]]:
    _r, probs = validate_recipe(new, facts=facts, origin="manual", base=FLOW)
    return [(p.code, p.path) for p in probs]


P_D04 = {"code": "label_missing", "category": "structure", "message": "…", "cells": [f"{SHEET}!B10"],
         "fix": "remove_label", "fix_args": {"segment": "日间", "labels": ["7-8"]}}
P_D09 = {"code": "title_not_found", "category": "structure", "message": "…", "cells": [], "fix": "rename_title",
         "fix_args": {"segment": "日间", "title": "日间时段客流（人次）",
                      "candidates": [{"cell": f"{SHEET}!B9", "text": "日间分时段客流（人次）"}]}}
P_D13 = {"code": "label_unexpected", "category": "structure", "message": "…", "cells": [f"{SHEET}!B8"],
         "fix": "add_label", "fix_args": {"segment": "日客流", "role": "measures",
                                          "labels": [{"raw": "分区丙（人次）", "cell": f"{SHEET}!B8"}]}}
P_D18 = {"code": "label_unparsed", "category": "structure", "message": "…", "cells": [f"{SHEET}!B21"],
         "fix": "declare_total", "fix_args": {"segment": "日间", "role": "dimension",
                                              "labels": [{"raw": "7-18 时合计", "cell": f"{SHEET}!B21", "total": True}],
                                              "at_end": True}}
P_D16 = {"code": "axis_extra_cells", "category": "structure", "message": "…", "cells": [f"{SHEET}!AG4"],
         "fix": "ignore_cells", "fix_args": {"block": "交叉表", "how": "columns",
                                             "headers": [{"raw": "合计", "cell": f"{SHEET}!AG4"}]}}
P_D21 = {"code": "row_unclaimed", "category": "structure", "message": "…", "cells": [f"{SHEET}!B32"],
         "fix": "ignore_cells", "fix_args": {"block": "交叉表", "how": "rows",
                                             "labels": [{"raw": "补录（人次）", "cell": f"{SHEET}!B32"}]}}
P_OUT = {"code": "outside_number", "category": "structure", "message": "…", "cells": [f"{SHEET}!C33"],
         "fix": "ignore_cells", "fix_args": {"sheet": SHEET, "how": "outside", "rows": [
             {"row": 33, "anchor": "补录说明", "anchor_cell": f"{SHEET}!B33", "cells": [f"{SHEET}!C33"]}]}}
P_PH = {"code": "value_not_number", "category": "structure", "message": "…", "cells": [f"{SHEET}!C11"],
        "fix": "declare_placeholder", "fix_args": {"block": "交叉表", "texts": ["—"], "other": 0}}
P_HID = {"code": "hidden_rows", "category": "structure", "message": "…", "cells": [],
         "fix": "declare_hidden", "fix_args": {"sheet": SHEET, "axis": "rows", "rows": [12], "autofilter": True}}
P_SHEET = {"code": "sheet_missing", "category": "structure", "message": "…", "cells": [],
           "fix": "rename_sheet", "fix_args": {"sheet_id": "s1", "name": SHEET, "candidates": ["客流汇总（新）"]}}


# ==========================================================================
# 公共
# ==========================================================================


def test_ids_are_deterministic_and_prefixed():
    a = F.propose_fixes(full(), [P_D04, P_D13], [], None)
    b = F.propose_fixes(full(), [P_D04, P_D13], [], None)
    assert [p.id for p in a] == [p.id for p in b]
    assert all(re.fullmatch(r"fx-[0-9a-f]{12}", p.id) for p in a)
    assert a[0].id != a[1].id
    assert {p.kind for p in a} <= set(FIX_KINDS)


def test_problem_without_fix_args_gets_no_proposal():
    """期 2 的执行器只透传 fix、没有 fix_args：界面不能光靠 fix 字符串构造补丁，不出提议。"""
    p = dict(P_D04)
    p.pop("fix_args")
    assert F.propose_fixes(full(), [p], [], None) == []
    assert F.propose_fixes(full(), [dict(P_D04, fix_args={"labels": 3})], [], None) == []


def test_anchor_index_points_at_the_right_problem():
    """两个分段同时找不到标题：title_not_found 没有坐标，只有 anchor 下标分得清（评审三-B2）。"""
    night = {"code": "title_not_found", "category": "structure", "message": "…", "cells": [], "fix": "rename_title",
             "fix_args": {"segment": "夜间", "title": "夜间时段客流（人次）",
                          "candidates": [{"cell": f"{SHEET}!B21", "text": "夜间分时段客流（人次）"}]}}
    other = {"code": "outside_digits", "category": "confirm", "message": "…", "cells": []}
    got = F.propose_fixes(full(), [other, P_D09, night], [], None)
    assert [(p.target["segment"], p.anchor.kind, p.anchor.index) for p in got] == [
        ("日间", "problem", 1), ("夜间", "problem", 2)]


def test_fix_ops_rejects_options_not_in_the_proposal_and_missing_reason():
    p = one(P_D21)
    with pytest.raises(F.EditRequestError) as e:
        F.fix_ops(full(), p, "remove", None)
    assert e.value.code == "edit_invalid"
    with pytest.raises(F.EditRequestError) as e:
        F.fix_ops(full(), p, "ignore", "  ")
    assert e.value.code == "reason_required"
    with pytest.raises(F.EditRequestError) as e:
        F.fix_ops(full(), p, "ignore", "长" * 201)
    assert e.value.code == "edit_invalid"
    # 客户端改了选项值里的文件原文（pick 的词）：不在选项里，拒收
    t = one(P_D09)
    with pytest.raises(F.EditRequestError):
        F.fix_ops(full(), t, f"{SHEET}!B9|pick:随便写的词", None)


def test_stale_target_returns_not_ok():
    p = one(P_D04)
    r = full()
    r["sheets"][0]["blocks"][0]["segments"][1]["id"] = "白天"
    r["sheets"][0]["blocks"][0]["segments"][3]["locate"]["segment"] = "夜间"
    res = F.fix_ops(r, p, "remove", None)
    assert res.ok is False and res.ops == [] and res.problems[0].code == "edit_not_applicable"
    assert res.recipe_sha256_after is None


def test_patch_text_comes_only_from_file_text_rules_and_reason():
    """补丁里的文字只有三种来源：文件原文（fix_args）、系统按规则推出的名字、理由（3.1）。"""
    for prob in (P_D04, P_D09, P_D13, P_D18, P_D16, P_D21, P_OUT, P_PH, P_HID, P_SHEET):
        for p in F.propose_fixes(full(), [prob], [], None):
            for o in p.options:
                res = F.fix_ops(full(), p, o.value, "合成理由" if o.needs_reason else None)
                blob = json.dumps(res.ops, ensure_ascii=False)
                assert "随便写的词" not in blob
                if not o.needs_reason:
                    assert "合成理由" not in blob


# ==========================================================================
# ① 移除标签（D04）
# ==========================================================================


def test_remove_label_dimension_d04_end_to_end():
    p = one(P_D04)
    assert p.kind == "remove_label" and p.problem_code == "label_missing"
    assert p.title == "在分段「日间」的期望标签中去掉「7-8」" and p.cells == [f"{SHEET}!B10"]
    assert p.target == {"segment": "日间", "labels": ["7-8"]}
    assert [(o.value, o.label, o.breaking) for o in p.options] == [("remove", "去掉标签「7-8」", False)]
    res = F.fix_ops(full(), p, "remove", None)
    assert res.key == "remove_label:日间:7-8" and res.title == p.title
    assert res.ops == [{"op": "replace", "path": f"{S}/1/labels/expect",
                        "value": ["8-9", "9-10", "10-11", "11-12", "12-13", "13-14", "14-15", "15-16", "16-17", "17-18"]}]
    new = apply(full(), p, "remove")
    assert problems_after(new) == []
    ext = dry("D04", new)
    assert ext.ok and ext.problems == []
    assert ext.expected_rows == {"日客流": 30, "时段客流": 480, "时段客流_表内合计": 90}


def test_remove_label_measures_removes_column_and_unit():
    prob = dict(P_D04, fix_args={"segment": "日客流", "labels": ["分区乙（人次）"]})
    p = one(prob)
    assert p.options[0].breaking is True and "破坏性变更" in p.options[0].detail
    res = F.fix_ops(full(), p, "remove", None)
    assert res.ops == [
        {"op": "replace", "path": f"{S}/0/labels/expect", "value": ["全日客流（人次）", "分区甲（人次）"]},
        {"op": "remove", "path": f"{S}/0/measures/分区乙（人次）"},
        {"op": "remove", "path": "/tables/0/units/分区乙"}]
    assert res.breaking is True
    # 关系 R1 引用了去掉的列：静态校验报关系问题，接着出 ③（只剩一个成员，只能删掉或不登记）
    new = apply_patch(full(), res.ops)
    _r, rps = validate_recipe(new, origin="manual", base=FLOW)
    assert ("column_unknown", "/relations/0/parts/1") in [(x.code, x.path) for x in rps]
    # 本期没有能认领的恒等式（文件里没有分区乙了）：不登记要认领一条系统发现，这里只能删掉这条关系，并且要写理由
    m = F.propose_fixes(new, [], [asdict(x) for x in rps], DraftFacts())
    assert [(x.kind, [(o.value, o.needs_reason) for o in x.options]) for x in m] == [
        ("edit_members", [("remove", True)])]
    with pytest.raises(F.EditRequestError) as e:
        F.fix_ops(new, m[0], "remove", None)
    assert e.value.code == "reason_required"
    gone = F.fix_ops(new, m[0], "remove", "合成理由：分区乙撤销")
    assert gone.summary == ["删除关系「R1」（理由：合成理由：分区乙撤销）"]
    assert problems_after(apply_patch(new, gone.ops)) == []


def test_remove_label_measures_then_edit_members_offers_dismiss_when_the_fact_is_still_there():
    """3.2 ① 的连带：去掉的组成列被 R1 引用、本期的事实 F1「全日客流 = 分区甲 + 分区乙」还在（文件没变，
    只是不再导入分区乙）。update 给不出（分区乙映射不到列），③ 只给「不登记」，认领 F1，要写理由。
    退回不要理由的「删除这条关系」的话，F1 的分段 id 是规则起草的写法、按全部标签又认不出，3.3 第 11 条判它
    不在改动范围内，删掉 R1 以后连未认领都不报，核对和系统发现一起不声不响地消失。"""
    prob = dict(P_D04, cells=[f"{SHEET}!B7"], fix_args={"segment": "日客流", "labels": ["分区乙（人次）"]})
    new = Recipe.model_validate(apply_patch(full(), F.fix_ops(full(), one(prob), "remove", None).ops)) \
        .model_dump(mode="json")
    _raw, _fn, sc, grids = book("D01")
    facts = detect_facts(grids, sc, recipe=new)
    f1 = next(f for f in facts.facts if f.kind == "sum_eq")
    assert f1.detail["parts"] == ["分区甲（人次）", "分区乙（人次）"]
    _r, rps = validate_recipe(new, facts=facts, origin="manual", base=FLOW)
    got = F.propose_fixes(new, [], [asdict(x) for x in rps], facts)
    assert len(got) == 1 and got[0].kind == "edit_members"
    m = got[0]
    assert [(o.value, o.needs_reason) for o in m.options] == [("dismiss", True)]
    assert m.target["fact"] == f1.id and "「分区乙（人次）」" in m.options[0].detail
    dis = apply_patch(new, F.fix_ops(new, m, "dismiss", "合成理由：分区乙另行统计").ops)
    assert dis["relations"][0] == {"id": "R1", "kind": "dismissed", "claims": f1.id, "reason": "合成理由：分区乙另行统计"}
    assert problems_after(dis, facts) == []
    # 绕过提议、直接删掉 R1：带现行配方校验时照样报未认领（F1 按现行配方认出落在改动过的「日客流」上）
    bare = copy.deepcopy(new)
    del bare["relations"][0]
    assert ("fact_unclaimed", "/relations") in problems_after(bare, facts)


def test_remove_label_not_offered_when_nothing_would_remain_or_label_unknown():
    r = full()
    r["sheets"][0]["blocks"][0]["segments"][1]["labels"]["expect"] = ["7-8"]
    assert F.propose_fixes(r, [P_D04], [], None) == []
    assert F.propose_fixes(full(), [dict(P_D04, fix_args={"segment": "日间", "labels": ["6-7"]})], [], None) == []
    assert F.propose_fixes(full(), [dict(P_D04, fix_args={"segment": "早间", "labels": ["7-8"]})], [], None) == []


# ==========================================================================
# ② 加入标签 → ③ 编辑恒等式成员（D13）
# ==========================================================================


def test_add_label_then_edit_members_d13_end_to_end():
    p = one(P_D13)
    assert p.kind == "add_label" and p.target["labels"] == [{"raw": "分区丙（人次）", "cell": f"{SHEET}!B8"}]
    assert "新增列「分区丙」，单位「人次」" in p.options[0].detail
    res = F.fix_ops(full(), p, "add", None)
    assert res.ops == [
        {"op": "replace", "path": f"{S}/0/labels/expect",
         "value": ["全日客流（人次）", "分区甲（人次）", "分区乙（人次）", "分区丙（人次）"]},
        {"op": "add", "path": f"{S}/0/measures/分区丙（人次）", "value": "分区丙"},
        {"op": "add", "path": "/tables/0/units/分区丙", "value": "人次"}]
    work = Recipe.model_validate(apply_patch(full(), res.ops)).model_dump(mode="json")
    _raw, _fn, sc, grids = book("D13")
    facts = detect_facts(grids, sc, recipe=work)
    _r, rps = validate_recipe(work, facts=facts, origin="manual", base=FLOW)
    assert [(x.code, x.path) for x in rps] == [("fact_claim_mismatch", "/relations/0/claims")]
    fixes = F.propose_fixes(work, [], [asdict(x) for x in rps], facts)
    assert len(fixes) == 1
    m = fixes[0]
    assert m.kind == "edit_members" and m.anchor.kind == "recipe_problem" and m.anchor.index == 0
    assert m.target == {"relation": "R1", "fact": "F1", "total": "全日客流", "parts": ["分区甲", "分区乙", "分区丙"]}
    assert [o.value for o in m.options] == ["update", "dismiss"]
    assert m.options[0].label == "按系统发现更新为「全日客流 = 分区甲 + 分区乙 + 分区丙」"
    assert m.options[1].needs_reason is True
    upd = F.fix_ops(work, m, "update", None)
    assert upd.key == "edit_members:R1" and upd.title == f"关系「R1」：{m.options[0].label}"
    work2 = apply_patch(work, upd.ops)
    _r, rps2 = validate_recipe(work2, facts=facts, origin="manual", base=FLOW)
    assert rps2 == []
    ext = dry("D13", work2)
    assert ext.ok and ext.problems == []
    # 不登记：整条换成 dismissed，认领本期的那条事实，理由进配方
    dis = apply_patch(work, F.fix_ops(work, m, "dismiss", "合成理由：分区丙另有口径").ops)
    assert dis["relations"][0] == {"id": "R1", "kind": "dismissed", "claims": "F1", "reason": "合成理由：分区丙另有口径"}
    assert validate_recipe(dis, facts=facts, origin="manual", base=FLOW)[1] == []


def test_add_label_column_name_collision_gets_suffix():
    prob = dict(P_D13, fix_args={"segment": "日客流", "role": "measures",
                                 "labels": [{"raw": "分区甲（人）", "cell": f"{SHEET}!B8"}]})
    p = one(prob)
    res = F.fix_ops(full(), p, "add", None)
    assert {"op": "add", "path": f"{S}/0/measures/分区甲（人）", "value": "分区甲_2"} in res.ops
    assert {"op": "add", "path": "/tables/0/units/分区甲_2", "value": "人"} in res.ops


def test_add_label_unit_outside_vocabulary_keeps_the_raw_text_like_the_drafter():
    """括号里的单位不在词表中：照起草器第 8 条（_measure_column）列名保留原文、不记单位，与起草器、框选的分段
    同一规则；选项和摘要写明单位没有登记，兄弟列都是「人次」时不会看起来同名同口径。"""
    odd = dict(P_D13, fix_args={"segment": "日客流", "role": "measures",
                                "labels": [{"raw": "分区丁（千人次）", "cell": f"{SHEET}!B8"}]})
    p = one(odd)
    assert "「千人次」不在单位词表里" in p.options[0].detail
    res = F.fix_ops(full(), p, "add", None)
    assert res.ops[1:] == [{"op": "add", "path": f"{S}/0/measures/分区丁（千人次）",
                            "value": _measure_column("分区丁（千人次）", 4)[0]}]
    assert res.ops[1]["value"] == "分区丁_千人次"
    assert any("没有登记单位" in x for x in res.summary)
    assert problems_after(apply_patch(full(), res.ops)) == []


def test_add_label_not_offered_for_known_label_or_role_mismatch():
    already = dict(P_D13, fix_args={"segment": "日客流", "role": "measures",
                                    "labels": [{"raw": "分区甲（人次）", "cell": f"{SHEET}!B6"}]})
    assert F.propose_fixes(full(), [already], [], None) == []
    wrong = dict(P_D13, fix_args={**P_D13["fix_args"], "role": "dimension"})
    assert F.propose_fixes(full(), [wrong], [], None) == []


def test_edit_members_without_a_replacing_fact_offers_only_removal():
    """本期没有可以替换、也没有可以认领的恒等式（新恒等式有一天不成立，_sum_holds 不出 sum_eq）：update、dismiss
    都给不出，只能删掉这条关系；删掉一条数据质量核对要写理由。"""
    work = apply_patch(full(), F.fix_ops(full(), one(P_D13), "add", None).ops)
    rp = {"path": "/relations/0/claims", "code": "fact_claim_mismatch", "message": "…"}
    ne = Fact("F1", "not_equal_sum", SHEET, "…", {"a": ["日间", "夜间"], "b_segment": "日客流", "b": "全日客流（人次）"})
    got = F.propose_fixes(work, [], [rp], DraftFacts(facts=[ne]))
    assert len(got) == 1 and [(o.value, o.needs_reason) for o in got[0].options] == [("remove", True)]
    assert "本期未发现可以替换或认领的恒等式" in got[0].options[0].detail
    new = apply_patch(work, F.fix_ops(work, got[0], "remove", "合成理由").ops)
    assert [r["id"] for r in new["relations"]] == ["R2"]


def test_edit_members_dismiss_skips_a_fact_claimed_by_another_relation():
    """可以认领的事实已经被别的关系认领：再认领会报 fact_claimed_twice，不拿它当「不登记」的认领对象。"""
    prob = dict(P_D04, cells=[f"{SHEET}!B7"], fix_args={"segment": "日客流", "labels": ["分区乙（人次）"]})
    new = apply_patch(full(), F.fix_ops(full(), one(prob), "remove", None).ops)
    sum_eq = Fact("F1", "sum_eq", SHEET, "…", {"segment": "客流汇总_按日", "total": "全日客流（人次）",
                                                  "parts": ["分区甲（人次）", "分区乙（人次）"]})
    rp = {"path": "/relations/0/parts/1", "code": "column_unknown", "message": "…"}
    got = F.propose_fixes(new, [], [rp], DraftFacts(facts=[sum_eq]))
    assert [o.value for o in got[0].options] == ["dismiss"] and got[0].target["fact"] == "F1"
    taken = copy.deepcopy(new)
    taken["relations"].append({"id": "R9", "kind": "dismissed", "claims": "F1", "reason": "合成理由"})
    got = F.propose_fixes(taken, [], [rp], DraftFacts(facts=[sum_eq]))
    assert [o.value for o in got[0].options] == ["remove"]


def test_edit_members_only_for_sum_eq_relations():
    rp = {"path": "/relations/1/claims", "code": "fact_claim_mismatch", "message": "…"}
    assert F.propose_fixes(full(), [], [rp], DraftFacts()) == []
    assert F.propose_fixes(full(), [], [{"path": "/relations/0", "code": "fact_unclaimed", "message": "…"}],
                           DraftFacts()) == []


# ==========================================================================
# ④ 分段标题改名（D09）
# ==========================================================================


def test_rename_title_keep_d09_end_to_end():
    p = one(P_D09)
    assert p.kind == "rename_title" and p.cells == [f"{SHEET}!B9"]
    values = [o.value for o in p.options]
    assert values[0] == f"{SHEET}!B9|keep" and f"{SHEET}!B9|pick:日间分" in values
    keep = p.options[0]
    assert keep.label == "改为「日间分时段客流（人次）」，「时段类别」沿用「日间」" and keep.breaking is False
    pick = next(o for o in p.options if o.value.endswith("pick:日间分"))
    assert pick.breaking is True and "这是破坏性变更：这一列的值从「日间」变为「日间分」" in pick.detail
    res = F.fix_ops(full(), p, keep.value, None)
    assert res.ops == [{"op": "replace", "path": f"{S}/1/locate/title", "value": "日间分时段客流（人次）"}]
    assert res.title == "分段「日间」的分段标题改为「日间分时段客流（人次）」，「时段类别」沿用「日间」"
    new = apply_patch(full(), res.ops)
    # 3.3 (b)：沿用现行配方的 pick，静态校验通过；没有 base 时照期 2 报 const_not_candidate
    assert problems_after(new) == []
    assert [p.code for p in validate_recipe(new, origin="manual")[1]] == ["const_not_candidate"]
    ext = dry("D09", new)
    assert ext.ok and ext.problems == []
    # pick 选项：再改常量
    res2 = F.fix_ops(full(), p, pick.value, None)
    assert res2.ops[1] == {"op": "replace", "path": f"{S}/1/const/时段类别/pick", "value": "日间分"}
    assert res2.breaking is True


def test_rename_title_keep_only_when_pick_is_substring():
    prob = dict(P_D09, fix_args={**P_D09["fix_args"],
                                 "candidates": [{"cell": f"{SHEET}!B9", "text": "白天时段客流（人次）"}]})
    p = one(prob)
    assert all("|pick:" in o.value for o in p.options)


def test_rename_title_skips_overlong_candidates():
    prob = dict(P_D09, fix_args={**P_D09["fix_args"],
                                 "candidates": [{"cell": f"{SHEET}!B9", "text": "日间" + "长" * 40}]})
    assert F.propose_fixes(full(), [prob], [], None) == []


# ==========================================================================
# ⑤ 声明为合计行（D18）
# ==========================================================================


def test_declare_total_keep_d18_end_to_end():
    p = one(P_D18)
    assert [o.value for o in p.options] == ["keep", "check_only"]
    assert p.options[0].label == "改作合计核对，原值另存表「时段客流_表内合计」"
    res = F.fix_ops(full(), p, "keep", None)
    assert res.ops[0] == {"op": "add", "path": f"{S}/1/stop_parser", "value": "hour_range_total"}
    assert res.ops[1]["path"] == f"{S}/2"
    seg = res.ops[1]["value"]
    assert seg["id"] == "日间合计" and seg["locate"]["segment"] == "日间"
    assert seg["labels"] == {"expect": ["7-18 时合计"]}
    assert seg["verify"] == {"kind": "label_range_sum", "against_table": "时段客流", "value": "客流"}
    # 同一块里已有另存到同一张基表的合计表：沿用，不新建表
    assert seg["keep_as"] == {"table": "时段客流_表内合计", "dim": "合计项",
                              "derive": {"起始小时": "start", "结束小时": "end"}, "value": "客流"}
    assert len(res.ops) == 2
    new = apply_patch(full(), res.ops)
    assert problems_after(new) == []
    ext = dry("D18", new)
    assert ext.ok and ext.expected_rows == {"日客流": 30, "时段客流": 510, "时段客流_表内合计": 120}
    # 只核对不另存
    co = apply_patch(full(), F.fix_ops(full(), p, "check_only", None).ops)
    assert co["sheets"][0]["blocks"][0]["segments"][2]["keep_as"] is None
    assert problems_after(co) == []


def test_declare_total_new_table_when_no_sibling_total():
    r = full()
    del r["sheets"][0]["blocks"][0]["segments"][3]          # 去掉夜间合计
    r["sheets"][0]["blocks"][0]["segments"][2]["stop_parser"] = None
    r["tables"] = r["tables"][:2]
    res = F.fix_ops(r, one(P_D18, r), "keep", None)
    assert res.ops[-1] == {"op": "add", "path": "/tables/-", "value": {
        "name": "时段客流_表内合计", "grain": ["日期", "合计项"], "kind": "reported_total", "units": {"客流": "人次"},
        "note": ""}}
    assert problems_after(apply_patch(r, res.ops)) == []


@pytest.mark.parametrize("change", ["role", "not_total", "not_at_end", "no_derive", "derived_after"])
def test_declare_total_five_preconditions(change):
    prob = copy.deepcopy(P_D18)
    r = full()
    args = prob["fix_args"]
    if change == "role":
        args["role"] = "measures"
    elif change == "not_total":
        args["labels"][0]["total"] = False
    elif change == "not_at_end":
        args["at_end"] = False
    elif change == "no_derive":
        r["sheets"][0]["blocks"][0]["segments"][1]["dim"]["derive"] = {"起始小时": "start"}
    else:
        args["segment"] = "夜间"                             # 夜间后面已经有「夜间合计」
    assert F.propose_fixes(r, [prob], [], None) == []


# ==========================================================================
# ⑥ 忽略（D16 列、D21 行、区域外）
# ==========================================================================


def test_ignore_columns_d16():
    p = one(P_D16)
    assert p.kind == "ignore_cells" and p.title == "按表头忽略「合计」这一列"
    assert p.options[0].needs_reason is True
    res = F.fix_ops(full(), p, "ignore", "右侧合计列不导入")
    assert res.key == "ignore_cells:columns:交叉表:合计"
    assert res.ops == [{"op": "add", "path": "/sheets/0/blocks/0/ignore_columns/-",
                        "value": {"header": "合计", "reason": "右侧合计列不导入"}}]
    assert problems_after(apply_patch(full(), res.ops)) == []


def test_ignore_rows_d21():
    p = one(P_D21)
    assert p.title == "按行标签忽略「补录（人次）」这一行"
    res = F.fix_ops(full(), p, "ignore", "补录行不导入")
    assert res.ops == [{"op": "add", "path": "/sheets/0/blocks/0/ignore_rows/-",
                        "value": {"label": "补录（人次）", "reason": "补录行不导入"}}]
    assert problems_after(apply_patch(full(), res.ops)) == []


def test_ignore_outside():
    p = one(P_OUT)
    assert p.title == "第 33 行有文字「补录说明」：忽略这一行导入区域之外的数字"
    res = F.fix_ops(full(), p, "ignore", "说明行的数字不导入")
    assert res.ops == [{"op": "add", "path": "/sheets/0/ignore_outside/-",
                        "value": {"anchor": "补录说明", "reason": "说明行的数字不导入"}}]
    assert problems_after(apply_patch(full(), res.ops)) == []
    no_anchor = copy.deepcopy(P_OUT)
    no_anchor["fix_args"]["rows"][0]["anchor"] = None
    assert F.propose_fixes(full(), [no_anchor], [], None) == []


def test_ignore_anchor_with_digits_is_not_offered():
    """锚点含数字时这一项不给（3.2 ⑥）：全部含数字时不出提议（不出一个没有选项的修复按钮）；部分含数字时只给
    其余的，含数字的写进选项的说明。三种形状都一样。"""
    prob = copy.deepcopy(P_D21)
    prob["fix_args"]["labels"] = [{"raw": "注：9月补录", "cell": f"{SHEET}!B32"}]
    assert F.propose_fixes(full(), [prob], [], None) == []
    cols = copy.deepcopy(P_D16)
    cols["fix_args"]["headers"] = [{"raw": "第3周合计", "cell": f"{SHEET}!AG4"}]
    out = copy.deepcopy(P_OUT)
    out["fix_args"]["rows"][0]["anchor"] = "9月补录说明"
    assert F.propose_fixes(full(), [cols, out], [], None) == []
    prob["fix_args"]["labels"].append({"raw": "补录（人次）", "cell": f"{SHEET}!B33"})
    p2 = one(prob)
    assert p2.target["labels"] == ["补录（人次）"] and "「注：9月补录」含数字" in p2.options[0].detail


def test_ignore_columns_not_offered_for_non_text_or_date_headers():
    for head in (None, "9月31日", ""):
        prob = copy.deepcopy(P_D16)
        prob["fix_args"]["headers"] = [{"raw": head, "cell": f"{SHEET}!AG4"}]
        assert F.propose_fixes(full(), [prob], [], None) == [], head


def test_ignore_rows_not_offered_without_label_or_on_list_block():
    prob = copy.deepcopy(P_D21)
    prob["fix_args"]["labels"] = [{"raw": None, "cell": f"{SHEET}!B32"}]
    assert F.propose_fixes(full(), [prob], [], None) == []


# ==========================================================================
# ⑦ 声明占位符、⑧ 隐藏行列、⑨ 工作表改名
# ==========================================================================


def test_declare_placeholder():
    p = one(P_PH)
    assert [o.value for o in p.options] == ["no_data", "not_applicable"]
    assert p.options[0].label == "把「—」也声明为无数据（存为空值）"
    res = F.fix_ops(full(), p, "not_applicable", None)
    assert res.ops == [{"op": "add", "path": "/sheets/0/blocks/0/values/placeholders/-",
                        "value": {"text": "—", "meaning": "不适用"}}]
    assert problems_after(apply_patch(full(), res.ops)) == []
    for texts in ([], ["123"], ["·"], ["太长的占位符文字超过"]):
        prob = copy.deepcopy(P_PH)
        prob["fix_args"]["texts"] = texts
        assert F.propose_fixes(full(), [prob], [], None) == [], texts


def test_declare_hidden_applies_on_full_form_defaults():
    """⑧ 在 hidden 取默认值的配方上也能应用：入参是完整形式，hidden.rows 总在（评审二-M1）。"""
    canonical_like = Recipe.model_validate(full()).model_dump(mode="json", exclude_defaults=True)
    assert "hidden" not in canonical_like["sheets"][0]
    p = one(P_HID)
    assert [o.value for o in p.options] == ["include", "exclude"]
    assert "可能是筛选隐藏的行" in p.options[0].detail
    res = F.fix_ops(full(), p, "exclude", None)
    assert res.ops == [{"op": "replace", "path": "/sheets/0/hidden/rows", "value": "exclude"}]
    assert apply_patch(full(), res.ops)["sheets"][0]["hidden"]["rows"] == "exclude"
    cols = copy.deepcopy(P_HID)
    cols["fix_args"] = {"sheet": SHEET, "axis": "cols", "cols": [5], "autofilter": False}
    assert [o.value for o in one(cols).options] == ["include"]


def test_rename_sheet_from_missing_and_from_fallback():
    p = one(P_SHEET)
    assert [o.value for o in p.options] == ["客流汇总（新）"]
    res = F.fix_ops(full(), p, "客流汇总（新）", None)
    assert res.ops == [{"op": "replace", "path": "/sheets/0/match/name", "value": "客流汇总（新）"}]
    assert res.key == "rename_sheet:s1"
    got = F.propose_fixes(full(), [], [], None, {SHEET: "9月客流"})
    assert len(got) == 1 and got[0].anchor.kind == "sheet_renamed" and got[0].anchor.sheet == "9月客流"
    assert got[0].problem_code is None
    assert "下一期很可能又会改名" in got[0].options[0].detail
    assert problems_after(apply_patch(full(), F.fix_ops(full(), got[0], "9月客流", None).ops)) == []


def test_proposal_round_trips_through_json():
    for prob in (P_D09, P_D18, P_HID):
        p = one(prob)
        back = F.proposal_from_dict(json.loads(json.dumps(asdict(p), ensure_ascii=False)))
        assert back == p
