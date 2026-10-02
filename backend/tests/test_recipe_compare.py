"""新旧配方对照（recipe_compare.compare_recipes，P3-SPEC 6.2、9.4 的 RecipeComparison）。

配方手写（假名），以参考配方为基线，按修复按钮、框选、重新起草会改到的地方逐项改。
"""
from __future__ import annotations

import copy
import json
from typing import Any

from app.data.recipe_compare import compare_recipes, relation_text
from app.data.recipe_types import Recipe
from tests.test_recipe_confirm import LIST
from tests.test_recipe_confirm_p3 import plan
from tests.test_recipe_diff import FLOW


def flow() -> dict[str, Any]:
    return copy.deepcopy(FLOW)


def seg(r: dict[str, Any], i: int) -> dict[str, Any]:
    return r["sheets"][0]["blocks"][0]["segments"][i]


def table(cmp: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in cmp["tables"] if t["name"] == name)


def column(cmp: dict[str, Any], tname: str, cname: str) -> dict[str, Any]:
    return next(c for c in table(cmp, tname)["columns"] if c["name"] == cname)


def segment(cmp: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in cmp["segments"] if s["id"] == sid)


def test_identical_recipes():
    cmp = compare_recipes(FLOW, Recipe.model_validate(FLOW).model_dump(mode="json", exclude_defaults=True))
    assert cmp["breaking"] is False and cmp["units_changed"] == [] and cmp["accumulate"] is None
    assert {t["status"] for t in cmp["tables"]} == {"same"}
    assert all(c["status"] == "same" and c["changes"] == [] for t in cmp["tables"] for c in t["columns"])
    assert {s["status"] for s in cmp["segments"]} == {"same"}
    assert {r["status"] for r in cmp["relations"]} == {"same"}
    assert cmp["sheets"] == [{"id": "s1", "name": {"old": "客流汇总", "new": "客流汇总"}}]
    assert cmp["mode"] == {"old": "replace", "new": "replace"}
    json.dumps(cmp, ensure_ascii=False)                     # 原样进 TrialOut、EditPreview


def test_d13_add_column_and_update_members():
    new = flow()
    s = seg(new, 0)
    s["labels"]["expect"].append("分区丙（人次）")
    s["measures"]["分区丙（人次）"] = "分区丙"
    new["tables"][0]["units"]["分区丙"] = "人次"
    new["relations"][0]["parts"] = ["分区甲", "分区乙", "分区丙"]
    cmp = compare_recipes(FLOW, new)
    t = table(cmp, "日客流")
    assert t["status"] == "changed" and t["breaking"] == []
    assert column(cmp, "日客流", "分区丙") == {"name": "分区丙", "status": "added", "old": None,
                                             "new": {"type": "INTEGER", "unit": "人次", "source": "分区丙（人次）"},
                                             "changes": []}
    assert segment(cmp, "日客流")["labels"] == {"added": ["分区丙（人次）"], "removed": []}
    r1 = next(r for r in cmp["relations"] if r["id"] == "R1")
    assert r1 == {"id": "R1", "status": "changed", "old": "全日客流 = 分区甲 + 分区乙",
                  "new": "全日客流 = 分区甲 + 分区乙 + 分区丙"}
    assert cmp["breaking"] is False


def test_d04_remove_label_is_not_breaking():
    new = flow()
    seg(new, 1)["labels"]["expect"] = seg(new, 1)["labels"]["expect"][1:]
    cmp = compare_recipes(FLOW, new)
    assert segment(cmp, "日间")["status"] == "changed"
    assert segment(cmp, "日间")["labels"] == {"added": [], "removed": ["7-8"]}
    assert table(cmp, "时段客流")["status"] == "same" and cmp["breaking"] is False


def test_title_change_and_const_value():
    new = flow()
    seg(new, 1)["locate"]["title"] = "日间分时段客流（人次）"
    cmp = compare_recipes(FLOW, new)
    assert segment(cmp, "日间")["title"] == {"old": "日间时段客流（人次）", "new": "日间分时段客流（人次）"}
    assert cmp["breaking"] is False
    seg(new, 1)["const"]["时段类别"]["pick"] = "日间分"
    cmp = compare_recipes(FLOW, new)
    assert column(cmp, "时段客流", "时段类别")["changes"] == ["const_value"]
    assert column(cmp, "时段客流", "时段类别")["status"] == "changed"
    assert cmp["breaking"] is True and table(cmp, "时段客流")["breaking"]


def test_unit_change_first_and_separate_from_breaking():
    new = flow()
    new["tables"][2]["units"]["客流"] = "人"
    new["sheets"][0]["blocks"][0]["values"]["type"] = "REAL"
    cmp = compare_recipes(FLOW, new)
    assert cmp["tables"][0]["name"] == "时段客流_表内合计"               # 单位变化的表最前
    assert cmp["units_changed"] == [{"table": "时段客流_表内合计", "column": "客流", "old": "人次", "new": "人"}]
    assert all("单位" not in m for t in cmp["tables"] for m in t["breaking"])
    assert column(cmp, "时段客流_表内合计", "客流")["changes"] == ["type", "unit"]
    assert cmp["breaking"] is True
    # 其余破坏性的在没变化的表之前
    statuses = [(bool(t["breaking"]), t["status"]) for t in cmp["tables"][1:]]
    assert statuses == sorted(statuses, key=lambda x: (not x[0], x[1] == "same"))


def test_removed_and_added_tables_and_grain():
    new = flow()
    seg(new, 3)["keep_as"] = None
    new["tables"].pop()
    new["tables"][0]["grain"] = ["日期", "全日客流"]
    cmp = compare_recipes(FLOW, new)
    assert table(cmp, "时段客流_表内合计")["status"] == "removed"
    assert table(cmp, "时段客流_表内合计")["kind"] == {"old": "reported_total", "new": None}
    assert table(cmp, "日客流")["grain"] == {"old": ["日期"], "new": ["日期", "全日客流"], "changed": True}
    back = compare_recipes(new, FLOW)
    assert table(back, "时段客流_表内合计")["status"] == "added"


def test_added_and_removed_segments_list_all_labels():
    """起草器换了分段 id（「日间」→「日间段」）：新增的分段列出标题和全部标签，去掉的分段列出原有的标题和标签，
    redraft_adopted 据此逐项写出新认出的定位规则（6.3；早先新增分段的 labels.added 是空的）。"""
    new = flow()
    renamed = copy.deepcopy(seg(new, 1))
    renamed["id"] = "日间段"
    new["sheets"][0]["blocks"][0]["segments"][1] = renamed
    cmp = compare_recipes(FLOW, new)
    labels = list(seg(FLOW, 1)["labels"]["expect"])
    added, removed = segment(cmp, "日间段"), segment(cmp, "日间")
    assert added["status"] == "added" and added["labels"] == {"added": labels, "removed": []}
    assert added["title"] == {"old": None, "new": "日间时段客流（人次）"}
    assert removed["status"] == "removed" and removed["labels"] == {"added": [], "removed": labels}
    assert removed["title"] == {"old": "日间时段客流（人次）", "new": None}


def test_sheet_name_mode_and_ignore_rules():
    new = flow()
    new["sheets"][0]["match"]["name"] = "客流汇总（新）"
    new["mode"] = "accumulate"
    new["sheets"][0]["blocks"][0]["ignore_rows"] = [{"label": "补录（人次）", "reason": "表下补录"}]
    new["sheets"][0]["ignore_outside"] = [{"anchor": "补录说明", "reason": "说明里的数字"}]
    cmp = compare_recipes(FLOW, new)
    assert cmp["sheets"] == [{"id": "s1", "name": {"old": "客流汇总", "new": "客流汇总（新）"}}]
    assert cmp["mode"] == {"old": "replace", "new": "accumulate"}
    assert segment(cmp, "交叉表")["ignore"] == {"added": ["按行标签忽略「补录（人次）」"], "removed": []}
    assert segment(cmp, "s1")["ignore"]["added"] == ["按同一行的文字「补录说明」忽略区域外的数字"]
    assert compare_recipes(new, FLOW)["segments"][-1]["ignore"]["removed"]


def test_list_after_title_and_header_source():
    new = copy.deepcopy(LIST)
    block = new["sheets"][0]["blocks"][0]
    block["after_title"] = "一、销售"
    block["columns"][3]["header"] = "销量（件数）"
    cmp = compare_recipes(LIST, new)
    assert segment(cmp, "列表1")["title"] == {"old": None, "new": "一、销售"}
    c = column(cmp, "销售", "销量")
    assert c["changes"] == ["source"] and c["old"]["source"] == "销量（件）" and c["new"]["source"] == "销量（件数）"


def test_plan_is_copied_into_accumulate():
    p = plan(change="compatible", added=[{"table": "日客流", "column": "分区丙"}])
    cmp = compare_recipes(FLOW, FLOW, plan=p)
    assert cmp["accumulate"] == {"change": "compatible", "added": [{"table": "日客流", "column": "分区丙"}],
                                 "retired_new": [], "retired_existing": []}


def test_relation_text():
    r = Recipe.model_validate(FLOW)
    assert relation_text(r.relations[0]) == "全日客流 = 分区甲 + 分区乙"
    assert relation_text(r.relations[1]) == "表「时段客流」与表「日客流」口径不同，不互相推算或相加"
