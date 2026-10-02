"""期 3 的确认项（recipe_confirm，P3-SPEC 6.2、9.5）：只列有变化的配方类项、修改记录、采用重新起草、按期累积。

回执和配方都是手写的（假名、假数），构造器在 test_recipe_diff、test_recipe_confirm 里；累积计划按契约
AccumulatePlan 的 JSON 形状手写（WP-4 的 plan_accumulate 在这一波还没合并，这里不依赖它）。
"""
from __future__ import annotations

import copy
from typing import Any, Callable

import pytest

from app.data.recipe_confirm import ConfirmContext, confirm_items, recipe_item_signatures
from app.data.recipe_types import AccumulatePlan, ConfirmItem, DiffItem
from tests.test_recipe_confirm import LIST, flow, list_ex
from tests.test_recipe_diff import BASE, D00_CONFIRMS, FLOW, SHEET, extraction_of, sep

#: 配方类确认项的前缀（6.2 的签名表 + 期 3 新增的两种）
RECIPE_PREFIXES = ("placeholder:", "blank_null:", "text_number:", "formula_cached:", "merged_fill:",
                   "checks_relaxed:", "cross_check_off:", "ignored_columns:", "blank_skip:", "derived:", "total_row:",
                   "year_from:", "relation:", "dismissed:", "unit:", "hidden:", "mode", "ignore_rows:",
                   "ignore_outside:")


def recipe_ids(items: list[ConfirmItem]) -> set[str]:
    return {i.id for i in items if i.id.startswith(RECIPE_PREFIXES)}


def redraft(new: dict[str, Any], old: dict[str, Any] = FLOW, ex: dict[str, Any] | None = None,
            **kw: Any) -> list[ConfirmItem]:
    kw.setdefault("checks", [])
    kw.setdefault("diff", [])
    kw.setdefault("prev", BASE[0])
    return confirm_items(ConfirmContext(kind=kw.pop("kind", "redraft"), recipe=new, old_recipe=old,
                                        extraction=ex if ex is not None else extraction_of(sep()[0]), **kw))


def by_id(items: list[ConfirmItem]) -> dict[str, ConfirmItem]:
    return {i.id: i for i in items}


def seg(r: dict[str, Any], i: int) -> dict[str, Any]:
    return r["sheets"][0]["blocks"][0]["segments"][i]


def block(r: dict[str, Any]) -> dict[str, Any]:
    return r["sheets"][0]["blocks"][0]


# --------------------------------------------------------------------------
# 6.2：签名表的每一行「变了才出」
# --------------------------------------------------------------------------


def _set(path: Callable[[dict[str, Any]], dict[str, Any]], key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def go(r: dict[str, Any]) -> None:
        path(r)[key] = value
    return go


def _values(r: dict[str, Any]) -> dict[str, Any]:
    return block(r)["values"]


def _ctx0(r: dict[str, Any]) -> dict[str, Any]:
    return r["sheets"][0]["context"][0]


def _r1(r: dict[str, Any]) -> dict[str, Any]:
    return r["relations"][0]


def _add_placeholder(r: dict[str, Any]) -> None:
    _values(r)["placeholders"].append({"text": "—", "meaning": "不适用"})


def _ignore_rows(reason: str) -> Callable[[dict[str, Any]], None]:
    def go(r: dict[str, Any]) -> None:
        block(r)["ignore_rows"] = [{"label": "补录（人次）", "reason": reason}]
    return go


def _ignore_outside(reason: str) -> Callable[[dict[str, Any]], None]:
    def go(r: dict[str, Any]) -> None:
        r["sheets"][0]["ignore_outside"] = [{"anchor": "补录说明", "reason": reason}]
    return go


def _derived_not_kept(r: dict[str, Any]) -> None:
    seg(r, 3)["keep_as"] = None
    r["tables"].pop()


def _derived_expect(r: dict[str, Any]) -> None:
    seg(r, 3)["labels"]["expect"] = ["18-22 时合计", "18-24 时合计"]


def _relaxed(checks: list[str]) -> Callable[[dict[str, Any]], None]:
    def go(r: dict[str, Any]) -> None:
        block(r)["axis"]["checks"] = checks
    return go


def _source_label(r: dict[str, Any]) -> None:
    s = seg(r, 0)
    s["labels"]["expect"][0] = "全天客流（人次）"
    s["measures"] = {"全天客流（人次）": "全日客流", "分区甲（人次）": "分区甲", "分区乙（人次）": "分区乙"}


#: (说明, 改法, 期望列出的配方类 id)。每一行都是「改了这一项 → 只列这一项」
FLOW_ROWS: list[tuple[str, Callable[[dict[str, Any]], None], set[str]]] = [
    ("占位符含义", lambda r: _values(r)["placeholders"][0].update(meaning="不适用"), {"placeholder:·"}),
    ("新增占位符", _add_placeholder, {"placeholder:—"}),
    ("空格存空值", _set(_values, "blank", "null"), {"blank_null:交叉表"}),
    ("千分位", _set(_values, "text_number", "parse_thousands"), {"text_number:交叉表"}),
    ("公式按保存值", _set(_values, "formula", "accept_cached"), {"formula_cached:交叉表"}),
    ("缺的日期断言", _relaxed(["contiguous"]), {"checks_relaxed:交叉表"}),
    ("文件名核对", _set(_ctx0, "cross_check", "none"), {"cross_check_off:客流汇总"}),
    ("合计不另存", _derived_not_kept, {"derived:夜间合计"}),
    ("合计标签集合", _derived_expect, {"derived:夜间合计"}),
    ("关系成员", _set(_r1, "parts", ["分区乙", "全日客流"]), {"relation:R1"}),
    ("关系换成不登记", lambda r: r["relations"].__setitem__(0, {"id": "R1", "kind": "dismissed", "claims": "F1",
                                                               "reason": "巧合"}), {"dismissed:F1"}),
    ("单位", lambda r: r["tables"][0]["units"].update(分区甲="人"), {"unit:日客流.分区甲"}),
    ("单位的来源标签", _source_label, {"unit:日客流.全日客流"}),
    ("隐藏行", lambda r: r["sheets"][0].update(hidden={"rows": "exclude"}), {"hidden:客流汇总"}),
    ("导入模式", lambda r: r.update(mode="accumulate"), {"mode"}),
    ("按行标签忽略", _ignore_rows("表下补录，不属于任何分区"), {"ignore_rows:交叉表"}),
    ("按同一行文字忽略", _ignore_outside("说明里的数字"), {"ignore_outside:客流汇总"}),
    ("按表头忽略列", lambda r: block(r).update(ignore_columns=[{"header": "合计", "reason": "月合计"}]),
     {"ignored_columns:交叉表"}),
]


@pytest.mark.parametrize("what,mutate,want", FLOW_ROWS, ids=[r[0] for r in FLOW_ROWS])
def test_signature_row_listed_only_when_changed(what, mutate, want):
    new = flow()
    mutate(new)
    assert recipe_ids(redraft(new)) == want
    # 反例：改了的配方作为现行配方，同一份再提交（只改了签名表之外的东西）：这一项不再列出
    again = copy.deepcopy(new)
    seg(again, 1)["locate"]["title"] = "日间时段客流"
    assert recipe_ids(redraft(again, old=new)) == set()


def test_year_from_row():
    old = flow()
    block(old)["axis"]["year_from"] = None
    assert recipe_ids(redraft(FLOW, old=old)) == {"year_from:交叉表"}
    assert recipe_ids(redraft(FLOW, old=FLOW)) == set()


def test_checks_relaxed_same_missing_set_not_listed_again():
    old = flow()
    block(old)["axis"]["checks"] = ["contiguous"]
    new = copy.deepcopy(old)
    new["sheets"][0]["blocks"][0]["values"]["blank"] = "null"
    assert recipe_ids(redraft(new, old=old)) == {"blank_null:交叉表"}
    block(new)["axis"]["checks"] = []                     # 缺的断言集合变了：再列
    assert "checks_relaxed:交叉表" in recipe_ids(redraft(new, old=old))


def test_reordered_members_not_listed():
    new = flow()
    _r1(new)["parts"] = ["分区乙", "分区甲"]
    assert recipe_ids(redraft(new)) == set()


def test_reason_change_relists_ignore_rule():
    old = flow()
    _ignore_rows("第一次写的理由")(old)
    new = copy.deepcopy(old)
    _ignore_rows("改过的理由")(new)
    assert recipe_ids(redraft(new, old=old)) == {"ignore_rows:交叉表"}


LIST_ROWS: list[tuple[str, Callable[[dict[str, Any]], None], set[str]]] = [
    ("合并填充", lambda r: block(r).update(merged_data="fill"), {"merged_fill:列表1"}),
    ("跳过空行", lambda r: block(r)["rows"].update(blank_rows="skip"), {"blank_skip:列表1"}),
    ("合计行的词", lambda r: block(r)["rows"]["total_row"].update(pick="总计"), {"total_row:列表1"}),
    ("多出的列", lambda r: block(r).update(extra_columns="ignore"), {"ignored_columns:列表1"}),
]


@pytest.mark.parametrize("what,mutate,want", LIST_ROWS, ids=[r[0] for r in LIST_ROWS])
def test_list_signature_rows(what, mutate, want):
    new = copy.deepcopy(LIST)
    mutate(new)
    ex = list_ex(ignored_columns={"列表1": ["备注"]}, blank_rows_skipped=1)
    assert recipe_ids(redraft(new, old=LIST, ex=ex, prev={"receipt": {"tables": []}})) == want


# --------------------------------------------------------------------------
# 6.2 的补充：按本期数据才出的配方类项，数据变了也照列（评审意见 H3）
# --------------------------------------------------------------------------


def _list_recipe_items(new: dict[str, Any], old: dict[str, Any], ex: dict[str, Any],
                       prev_ex: dict[str, Any]) -> set[str]:
    return recipe_ids(redraft(new, old=old, ex=ex, prev={"receipt": prev_ex}, kind="reupload"))


def test_newly_ignored_columns_listed_even_if_signature_same():
    """extra_columns=ignore：上一期没有多出的列（上次提交时没出 ignored_columns，从没人确认过），本期冒出「备注金额」；
    同一暂存区里另改了合并填充。只比签名会只列 merged_fill，被忽略的列没人看。"""
    old = copy.deepcopy(LIST)
    block(old)["extra_columns"] = "ignore"
    new = copy.deepcopy(old)
    block(new)["merged_data"] = "fill"
    now = list_ex(ignored_columns={"列表1": ["备注金额"]})
    assert _list_recipe_items(new, old, now, list_ex()) == {"merged_fill:列表1", "ignored_columns:列表1"}
    # 上一期已经忽略同样的列（match_key 相同）：签名和数据都没变，不再列
    same = list_ex(ignored_columns={"列表1": ["备注 金额"]})
    assert _list_recipe_items(new, old, now, same) == {"merged_fill:列表1"}


def test_list_formula_cells_appearing_listed():
    """列表的公式默认按保存值导入，有公式格才出 formula_cached。上一期 0 格、本期 12 格：列出。"""
    new = copy.deepcopy(LIST)
    block(new)["merged_data"] = "fill"
    got = _list_recipe_items(new, LIST, list_ex(formula_cells_accepted=12), list_ex(formula_cells_accepted=0))
    assert got == {"merged_fill:列表1", "formula_cached:列表1"}
    # 两期都有公式格（个数不同）：上次已经确认过「按保存值导入」，不再列
    got = _list_recipe_items(new, LIST, list_ex(formula_cells_accepted=12), list_ex(formula_cells_accepted=3))
    assert got == {"merged_fill:列表1"}


def _flow_prev(**receipt: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE[0])
    doc["receipt"].update(receipt)
    return doc


def test_year_from_listed_when_header_form_changes():
    """上一期表头是完整日期（year_from 不出），本期变成「9月1日」这种写法（年份取自统计期）：列出 year_from。"""
    new = flow()
    _values(new)["blank"] = "null"
    ex = extraction_of(sep()[0])
    prev = _flow_prev(axes=[{"block": "交叉表", "form": "date"}])
    assert recipe_ids(redraft(new, ex=ex, prev=prev, kind="reupload")) == {"blank_null:交叉表", "year_from:交叉表"}
    # 两期都是文字表头：不再列
    assert recipe_ids(redraft(new, ex=ex, kind="reupload")) == {"blank_null:交叉表"}


def test_ignore_rows_hits_change_listed():
    """忽略规则命中从 1 行变成 9 行：配方没改时差异卡出 diff:ignored_rows:，配方改了别处时由这里兜住。"""
    old = flow()
    _ignore_rows("表下补录")(old)
    new = copy.deepcopy(old)
    _values(new)["blank"] = "null"
    hit = {"sheet": SHEET, "reason": "ignored_rows", "anchor": "补录（人次）", "block": "交叉表", "cells": 3}
    ex = extraction_of(sep()[0])
    ex["rows_excluded"] = [{**hit, "rows": [[32, 40]]}]
    prev = _flow_prev(rows_excluded=[{**hit, "rows": [[32, 32]]}])
    assert recipe_ids(redraft(new, old=old, ex=ex, prev=prev)) == {"blank_null:交叉表", "ignore_rows:交叉表"}
    same = _flow_prev(rows_excluded=[{**hit, "rows": [[30, 38]]}])          # 行号挪了、行数和锚点没变
    assert recipe_ids(redraft(new, old=old, ex=ex, prev=same)) == {"blank_null:交叉表"}
    # 上一期回执没有 rows_excluded（期 3 之前的导入）：不知道上次命中几行，照列
    assert "ignore_rows:交叉表" in recipe_ids(redraft(new, old=old, ex=ex, prev=BASE[0]))


def test_ignore_outside_and_hidden_content_change_listed():
    old = flow()
    _ignore_outside("说明里的数字")(old)
    old["sheets"][0]["hidden"] = {"rows": "exclude"}
    new = copy.deepcopy(old)
    _values(new)["blank"] = "null"
    hit = {"sheet": SHEET, "reason": "ignored_outside", "anchor": "补录说明", "block": None, "cells": 2,
           "rows": [[33, 34]]}
    ex = extraction_of(sep()[0])
    ex["rows_excluded"] = [hit]
    ex["hidden"] = {SHEET: {"rows": [12], "cols": [], "policy_rows": "exclude", "policy_cols": "reject_if_any"}}
    prev = _flow_prev(rows_excluded=[], hidden={})
    assert recipe_ids(redraft(new, old=old, ex=ex, prev=prev)) == {
        "blank_null:交叉表", "ignore_outside:客流汇总", "hidden:客流汇总"}
    prev = _flow_prev(rows_excluded=[hit], hidden=copy.deepcopy(ex["hidden"]))
    assert recipe_ids(redraft(new, old=old, ex=ex, prev=prev)) == {"blank_null:交叉表"}


def test_data_signature_does_not_apply_when_recipe_unchanged():
    """配方没变时不列配方类项（期 2 起如此），数据的变化由差异卡兜住。"""
    ex = extraction_of(sep()[0])
    prev = _flow_prev(axes=[{"block": "交叉表", "form": "date"}])
    assert recipe_ids(redraft(FLOW, ex=ex, prev=prev, kind="reupload")) == set()


def test_d04_fix_lists_only_the_fix():
    """A15：D04 修复（日间去掉「7-8」）后，配方类只剩 fix:*，没有 placeholder:·、unit:* 等没变的项。"""
    new = flow()
    seg(new, 1)["labels"]["expect"] = seg(new, 1)["labels"]["expect"][1:]
    edits = [{"seq": 1, "kind": "fix", "key": "remove_label:日间:7-8", "title": "在分段「日间」的期望标签中去掉「7-8」",
              "signed_by": "测试员甲", "superseded": False, "undoable": True}]
    items = redraft(new, kind="reupload", edits=edits)
    assert [i.id for i in items] == ["fix:remove_label:日间:7-8"]
    fix = items[0]
    assert fix.source == "edit" and fix.label == "在分段「日间」的期望标签中去掉「7-8」"
    assert "署名（未认证）：测试员甲" in fix.detail


def test_sheet_compared_by_recipe_id_not_name():
    """⑨ 更新工作表名：匹配名变了、工作表 id 没变。按 id 换成本期实际的名字比，cross_check_off 不重复列出。"""
    old = flow()
    _ctx0(old)["cross_check"] = "none"
    new = copy.deepcopy(old)
    new["sheets"][0]["match"]["name"] = "客流汇总（新）"
    ex = extraction_of(sep()[0])
    ex["sheets"]["matched"] = {"s1": "客流汇总（新）"}
    assert recipe_ids(redraft(new, old=old, ex=ex)) == set()
    _ctx0(new)["cross_check"] = "filename"
    _ctx0(old)["cross_check"] = "filename"
    new["sheets"][0]["hidden"] = {"rows": "include"}
    assert recipe_ids(redraft(new, old=old, ex=ex)) == {"hidden:客流汇总（新）"}


def test_recipe_item_signatures_public_shape():
    sig = recipe_item_signatures(FLOW)
    assert {"placeholder:·", "derived:夜间合计", "year_from:交叉表", "relation:R1", "relation:R2", "mode",
            "unit:日客流.全日客流", "cross_check_off:客流汇总", "hidden:客流汇总"} <= set(sig)
    assert all(isinstance(v, str) for v in sig.values())
    # 写全默认值和省略默认值是同一份配方：签名相同
    from app.data.recipe_types import Recipe

    compact = Recipe.model_validate(FLOW).model_dump(mode="json", exclude_defaults=True)
    assert recipe_item_signatures(compact) == sig == recipe_item_signatures(Recipe.model_validate(FLOW))
    changed = flow(mode="accumulate")
    assert {k for k in sig if recipe_item_signatures(changed)[k] != sig[k]} == {"mode"}


def test_first_and_switch_still_list_everything():
    ex = extraction_of(BASE[0])
    first = confirm_items(ConfirmContext(kind="first", recipe=FLOW, extraction=ex, checks=[]))
    assert {i.id for i in first} == D00_CONFIRMS
    mode = by_id(first)["mode"]
    assert mode.detail == "每次上传新一期，当前版本只含新的一期，此前各期留在历史版本中"
    acc = confirm_items(ConfirmContext(kind="first", recipe=flow(mode="accumulate"), extraction=ex, checks=[]))
    assert by_id(acc)["mode"].label == "导入模式：按期累积"
    assert "部分重叠的文件会被拒收" in by_id(acc)["mode"].detail


# --------------------------------------------------------------------------
# 采用按规则重新起草（6.3）
# --------------------------------------------------------------------------


def test_rules_redraft_lists_everything_and_redraft_adopted():
    new = flow()
    seg(new, 1)["locate"]["title"] = "日间分时段客流（人次）"
    new["sheets"][0]["match"]["name"] = "客流汇总表"
    items = redraft(new, recipe_origin="rules_redraft")
    got = by_id(items)
    assert D00_CONFIRMS <= set(got)
    adopted = got["redraft_adopted"]
    assert adopted.source == "edit"
    assert adopted.label.startswith("采用按规则重新起草的配方：")
    assert "分段「日间」的标题「日间时段客流（人次）」→「日间分时段客流（人次）」" in adopted.label
    assert "工作表名「客流汇总」→「客流汇总表」" in adopted.label
    # 普通的手工修改：只列有变化的，没有 redraft_adopted
    assert "redraft_adopted" not in by_id(redraft(new, recipe_origin="manual"))


def test_redraft_adopted_uses_given_compare_and_lists_labels_and_sources():
    cmp = {"segments": [{"id": "日间", "status": "changed", "title": {"old": "a", "new": "a"},
                         "labels": {"added": ["6-7"], "removed": ["7-8"]}, "ignore": {"added": [], "removed": []}}],
           "sheets": [], "tables": [{"name": "日客流", "columns": [
               {"name": "分区甲", "old": {"source": "分区甲（人次）"}, "new": {"source": "东区（人次）"}}]}]}
    adopted = by_id(redraft(FLOW, recipe_origin="rules_redraft", redraft_compare=cmp))["redraft_adopted"]
    assert "分段「日间」加入标签「6-7」" in adopted.label and "分段「日间」去掉标签「7-8」" in adopted.label
    assert "表「日客流」列「分区甲」的来源「分区甲（人次）」→「东区（人次）」" in adopted.label
    same = by_id(redraft(FLOW, recipe_origin="rules_redraft"))["redraft_adopted"]
    assert same.label == "采用按规则重新起草的配方：分段、标签、工作表名和列的来源与现行配方相同"


def test_redraft_adopted_lists_title_and_labels_of_added_and_removed_segments():
    """起草器把「日间」段换成新 id「日间段」：新分段的标题和全部标签就是它新认出的定位规则，要逐项写出来（6.3）。"""
    new = flow()
    segs = block(new)["segments"]
    renamed = copy.deepcopy(segs[1])
    renamed["id"] = "日间段"
    segs[1] = renamed
    adopted = by_id(redraft(new, recipe_origin="rules_redraft"))["redraft_adopted"]
    labels = "".join(f"「{x}」" for x in seg(FLOW, 1)["labels"]["expect"])
    assert f"新增分段「日间段」，标题「日间时段客流（人次）」，标签{labels}" in adopted.detail
    assert f"去掉分段「日间」，标题「日间时段客流（人次）」，标签{labels}" in adopted.detail


def test_rules_redraft_without_old_or_compare_raises():
    with pytest.raises(ValueError, match="重新起草"):
        confirm_items(ConfirmContext(kind="first", recipe=FLOW, extraction=extraction_of(BASE[0]), checks=[],
                                     recipe_origin="rules_redraft"))


# --------------------------------------------------------------------------
# 修改记录
# --------------------------------------------------------------------------


def test_edit_items_fix_select_and_superseded():
    edits = [
        {"kind": "fix", "key": "add_label:日客流:分区丙（人次）", "title": "在分段「日客流」加入标签「分区丙（人次）」",
         "summary": ["加入标签「分区丙（人次）」", "新增列「分区丙」，单位 人次"], "superseded": False},
        {"kind": "selection", "key": "list:列表1", "title": "按框选替换列表「列表1」：表头按文字定位", "superseded": False},
        {"kind": "fix", "key": "remove_label:日间:7-8", "title": "去掉「7-8」", "superseded": True},
    ]
    got = by_id(redraft(FLOW, kind="reupload", edits=edits))
    assert set(got) == {"fix:add_label:日客流:分区丙（人次）", "select:list:列表1"}
    assert got["fix:add_label:日客流:分区丙（人次）"].detail == "加入标签「分区丙（人次）」；新增列「分区丙」，单位 人次"
    assert all(i.source == "edit" for i in got.values())


# --------------------------------------------------------------------------
# 按期累积（2.4、2.5、9.5）
# --------------------------------------------------------------------------


def plan(action: str = "append", **kw: Any) -> dict[str, Any]:
    base = AccumulatePlan(
        mode="accumulate", action=action, period={"start": "2026-09-01", "end": "2026-09-30"},
        parts=[{"import_id": "i1", "seq": 1, "start": "2026-08-01", "end": "2026-08-31", "file_name": "八月.xlsx",
                "new": False, "rows": {}, "blockers": []},
               {"import_id": None, "seq": None, "start": "2026-09-01", "end": "2026-09-30", "file_name": "九月.xlsx",
                "new": True, "rows": {}, "blockers": []}],
        replaces=None, dropped=[], overlaps=[], gaps=[], backfill=False, change="same", added=[], retired_new=[],
        retired_existing=[], semantic=[], label_sets=[], mode_switch=None, reason=None, union=None)
    out = {k: getattr(base, k) for k in base.__dataclass_fields__}
    out.update(kw)
    return out


def acc_items(p: Any, new: dict[str, Any] | None = None, **kw: Any) -> dict[str, ConfirmItem]:
    r = new if new is not None else flow(mode="accumulate")
    kw.setdefault("old", flow(mode="accumulate"))
    kw.setdefault("kind", "reupload")
    return by_id(redraft(r, accumulate=p, **kw))


def test_append_has_no_accumulate_items_and_accepts_dataclass():
    got = acc_items(plan())
    assert not {i for i in got if i.startswith(("period_replace", "accumulate_", "retire:", "mode_switch"))}
    dc = AccumulatePlan(**plan())
    assert acc_items(dc) == got


def test_period_replace_reupload_and_redraft():
    rep = {"import_id": "i2", "seq": 2, "start": "2026-09-01", "end": "2026-09-30", "file_name": "九月.xlsx"}
    item = acc_items(plan("replace_period", replaces=rep))["period_replace:2026-09-01~2026-09-30"]
    assert item.label == "替换已有的一期 2026-09-01 至 2026-09-30（第 2 次导入「九月.xlsx」）：启用后该期以本次为准"
    assert item.source == "accumulate"
    red = acc_items(plan("replace_period", replaces=rep), kind="redraft")["period_replace:2026-09-01~2026-09-30"]
    assert red.label == "用修改后的配方重新导入最近一期 2026-09-01 至 2026-09-30：启用后该期以本次为准，其余各期不变"
    with pytest.raises(ValueError, match="统计期"):
        acc_items(plan("replace_period", period={}, replaces=None))


def test_accumulate_restart_lists_changes_and_count():
    dropped = [{"import_id": "i1", "seq": 1, "start": "2026-08-01", "end": "2026-08-31", "blockers": []},
               {"import_id": "i2", "seq": 2, "start": "2026-09-01", "end": "2026-09-30",
                "blockers": [{"path": "/tables/0", "code": "accumulate_unkeyed", "message": "表「日客流」不能按期累积"}]}]
    p = plan("restart", dropped=dropped, semantic=["表「日客流」列「全日客流」的单位 人次 → 人"], change="semantic")
    item = acc_items(p)["accumulate_restart"]
    assert item.source == "accumulate"
    assert "表「日客流」列「全日客流」的单位 人次 → 人" in item.label
    assert "2026-09-01 至 2026-09-30 这一期的配方不满足按期累积的要求：表「日客流」不能按期累积" in item.label
    assert "此前 2 期不再出现在当前版本中" in item.label and "数据源卡片的「版本」" in item.label
    assert "如果不想重新开始累积，请返回修改配方" in item.detail
    assert "accumulate_restart" not in acc_items(plan())


def test_retire_only_for_retired_new_and_dedupes_breaking():
    """去掉分区乙：期 2 的 breaking:日客流（删除或改名了列）在有 retire:* 时不再重复；早已退役的不出确认项。"""
    new = flow(mode="accumulate")
    s = seg(new, 0)
    s["labels"]["expect"] = ["全日客流（人次）", "分区甲（人次）"]
    s["measures"] = {"全日客流（人次）": "全日客流", "分区甲（人次）": "分区甲"}
    new["tables"][0]["units"] = {"全日客流": "人次", "分区甲": "人次"}
    new["relations"] = new["relations"][1:]
    p = plan(retired_new=[{"table": "日客流", "column": "分区乙"}], change="retire")
    got = acc_items(p, new)
    item = got["retire:日客流.分区乙"]
    assert item.label == "表「日客流」的列「分区乙」自本期起不再导入：早期各期保留原值，本期起为空值"
    assert item.source == "accumulate" and "两列并存" in item.detail
    assert "breaking:日客流" not in got
    # 没有计划（每期替换）：照期 2 出 breaking
    assert "breaking:日客流" in by_id(redraft(new, old=flow(mode="accumulate"), kind="reupload"))
    # 只在早已退役里：不出 retire，也不出 breaking
    got = acc_items(plan(retired_existing=[{"table": "日客流", "column": "分区乙"}]), new)
    assert not {i for i in got if i.startswith(("retire:", "breaking:"))}
    # 退役的表
    assert "retire:时段客流_表内合计" in acc_items(plan(retired_new=[{"table": "时段客流_表内合计", "column": None}]))
    # restart：结果只有本期，不出 retire，breaking 照出
    got = acc_items(plan("restart", retired_new=[{"table": "日客流", "column": "分区乙"}], semantic=["x"]), new)
    assert "retire:日客流.分区乙" not in got and "breaking:日客流" in got


def test_mode_switch_texts():
    dropped = [{"import_id": "i1", "seq": 1, "start": "2026-08-01", "end": "2026-08-31", "blockers": []}]
    a2r = acc_items(plan("replace", mode="replace", dropped=dropped, mode_switch="accumulate->replace"),
                    flow())["mode_switch:accumulate->replace"]
    assert a2r.label == ("从按期累积改为每期替换：启用后当前版本只含本期，此前 1 期（2026-08-01 至 2026-08-31）"
                         "不再出现在当前版本中，可以在数据源卡片的「版本」中启用此前的版本")
    r2a = acc_items(plan(mode_switch="replace->accumulate"), old=FLOW)["mode_switch:replace->accumulate"]
    assert r2a.label == "从每期替换改为按期累积：当前版本的 2026-08-01 至 2026-08-31 作为第一期保留，此前被替换的各期不会补回"
    no_period = [{"import_id": "i1", "seq": 1, "start": None, "end": None, "blockers": []}]
    first = acc_items(plan("first", dropped=no_period, mode_switch="replace->accumulate", reason="当前版本没有统计期"),
                      old=FLOW)["mode_switch:replace->accumulate"]
    assert "当前版本没有统计期，不能作为第一期：启用后当前版本只含本期" in first.label
    blocked = [{**dropped[0], "blockers": [{"message": "交叉表「交叉表」的日期检查没有选「恰好覆盖统计期」"}]}]
    first = acc_items(plan("first", dropped=blocked, mode_switch="replace->accumulate"),
                      old=FLOW)["mode_switch:replace->accumulate"]
    assert "当前版本的配方不满足按期累积的要求（交叉表「交叉表」的日期检查没有选「恰好覆盖统计期」），不能作为第一期" in first.label
    assert first.source == "accumulate"


def test_mode_switch_with_replace_period():
    """替换改累积走「修改配方」（redraft）或同一统计期再传：plan_accumulate 给 action=replace_period、parts 只有本期、
    dropped 为空、replaces 是当前那一期。mode_switch 要写那一期由本次重新导入后作为第一期，不能落到「没有统计期」，
    与同一清单里的 period_replace 自相矛盾（评审意见）。"""
    cur = {"import_id": "i1", "seq": 1, "start": "2026-08-01", "end": "2026-08-31", "file_name": "八月.xlsx",
           "new": False, "rows": {}, "blockers": []}
    new_part = {"import_id": None, "seq": None, "start": "2026-08-01", "end": "2026-08-31", "file_name": "八月.xlsx",
                "new": True, "rows": {}, "blockers": []}
    p = plan("replace_period", period={"start": "2026-08-01", "end": "2026-08-31"}, parts=[new_part], replaces=cur,
             dropped=[], mode_switch="replace->accumulate")
    got = acc_items(p, old=FLOW, kind="redraft")
    switch = got["mode_switch:replace->accumulate"]
    assert switch.label == ("从每期替换改为按期累积：当前版本的 2026-08-01 至 2026-08-31 按修改后的配方重新导入，作为第一期，"
                            "此前被替换的各期不会补回")
    assert "没有统计期" not in switch.label and "不能作为第一期" not in switch.label
    assert got["period_replace:2026-08-01~2026-08-31"].label.startswith("用修改后的配方重新导入最近一期 2026-08-01 至")
    again = acc_items(p, old=FLOW)["mode_switch:replace->accumulate"]
    assert again.label == ("从每期替换改为按期累积：本次导入替换当前版本的 2026-08-01 至 2026-08-31，作为第一期，"
                           "此前被替换的各期不会补回")
    # 计划不完整（替换该期却没有被替换的那一期、追加却没有保留的那一期）：报错，不按「没有统计期」写
    with pytest.raises(ValueError, match="被替换"):
        acc_items({**p, "replaces": None}, old=FLOW)
    with pytest.raises(ValueError, match="第一期"):
        acc_items(plan(parts=[new_part], mode_switch="replace->accumulate"), old=FLOW)


def test_mode_switch_rejected_has_no_item():
    """部分重叠被拒收：不能提交，不出 mode_switch（原因在试运行的问题里）。"""
    p = plan("rejected", mode_switch="replace->accumulate",
             overlaps=[{"start": "2026-08-15", "end": "2026-09-14"}])
    assert "mode_switch:replace->accumulate" not in acc_items(p, old=FLOW)


def outside_change(old: str, new: str, cell: str = "客流汇总!B33") -> DiffItem:
    return DiffItem("outside_changed", f"{cell} 的区域外文字改了：「{old}」→「{new}」", f"上一期：{old}；本期：{new}",
                    requires_confirm=True, confirm_id=f"diff:outside:{cell}")


@pytest.mark.parametrize("old,new,risky", [
    ("单位：人次", "单位：万人次", True),
    ("注：含东门", "注：含东门（万人次）", True),
    ("金额（元）", "金额（千元）", True),
    ("统计范围：东区", "统计范围：东区、西区", True),
    ("注：12% 为估算", "注：15% 为估算", True),
    ("编制人：测试员甲", "编制人：测试员乙", False),
    ("注：东门闸机故障", "注：西门闸机故障", False),
    # 单字单位出现即算（规格 2.5），不只认紧跟数字或在括号里的写法（评审意见）
    ("货运量按吨统计", "货运量按件统计", True),
    ("金额：元", "金额：万", True),
    ("每户", "每人", True),
    ("单价 元/件", "单价 元/个", True),
    ("注：按人计", "注：按户计", True),
    # 停用词：含单位字、却不是在说单位
    ("填报人：测试员甲", "填报人：测试员乙", False),
    ("联系人：测试员甲；审核人：测试员乙", "联系人：测试员丙；审核人：测试员丁", False),
    ("填报单位：东区站", "填报单位：西区站", False),
    ("附件：说明一", "附件：说明二", False),
    ("注：个别日期闸机故障", "注：整个月闸机故障", False),
])
def test_accumulate_unit_risk(old, new, risky):
    got = acc_items(plan(), diff=[outside_change(old, new)])
    assert ("accumulate_unit_risk" in got) is risky
    if risky:
        item = got["accumulate_unit_risk"]
        assert item.source == "accumulate" and "涉及单位或口径" in item.label and "返回修改配方里的单位" in item.label


def test_accumulate_unit_risk_scope():
    d = [outside_change("单位：人次", "单位：万人次")]
    # 每期替换只影响一期：不出
    assert "accumulate_unit_risk" not in acc_items(plan("replace", mode="replace"), flow(), diff=d)
    assert "accumulate_unit_risk" not in acc_items(plan("restart", semantic=["x"]), diff=d)
    # 替换该期也出
    assert "accumulate_unit_risk" in acc_items(plan("replace_period", replaces={"start": "2026-09-01",
                                                                                "end": "2026-09-30"}), diff=d)
    # outside_diff 给了就用它，不看 diff
    assert "accumulate_unit_risk" not in acc_items(plan(), diff=d, outside_diff=[])
    assert "accumulate_unit_risk" in acc_items(plan(), diff=[], outside_diff=d)
    # 统计期格的写法变化（context_text）也看
    ctx = DiffItem("context_text", "统计期格 客流汇总!B2 的写法变了", "上一期：统计（人次）；本期：统计（万人次）")
    assert "accumulate_unit_risk" in acc_items(plan(), diff=[ctx])
    # 分段标题的变化（配方里的 locate.title）：单位词变了才算；D09 只改了别的字、单位还是人次，不算
    new = flow(mode="accumulate")
    seg(new, 1)["locate"]["title"] = "日间时段客流（万人次）"
    assert "分段「日间」的标题" in acc_items(plan(), new)["accumulate_unit_risk"].label
    seg(new, 1)["locate"]["title"] = "日间分时段客流（人次）"
    assert "accumulate_unit_risk" not in acc_items(plan(), new)


def test_ignore_rule_items_with_counts():
    r = flow()
    _ignore_rows("表下补录，不属于任何分区")(r)
    _ignore_outside("说明里的数字")(r)
    ex = extraction_of(BASE[0])
    ex["rows_excluded"] = [
        {"sheet": SHEET, "reason": "ignored_rows", "rows": [[32, 32]], "cells": 3, "anchor": "补录（人次）",
         "block": "交叉表"},
        {"sheet": SHEET, "reason": "ignored_outside", "rows": [[33, 34]], "cells": 2, "anchor": "补录说明",
         "block": None}]
    got = by_id(confirm_items(ConfirmContext(kind="first", recipe=r, extraction=ex, checks=[])))
    assert got["ignore_rows:交叉表"].label == "「交叉表」按行标签忽略：「补录（人次）」（理由：表下补录，不属于任何分区；本期 1 行）"
    assert got["ignore_outside:客流汇总"].label == (
        "工作表「客流汇总」按同一行的文字忽略导入区域之外的数字：「补录说明」（理由：说明里的数字；本期 2 行）")
    assert got["ignore_rows:交叉表"].source == "recipe"
    # 本期没有命中：照出，0 行；回执里没有 rows_excluded（期 3 之前）：不写行数
    ex["rows_excluded"] = []
    assert "本期 0 行" in by_id(confirm_items(ConfirmContext(kind="first", recipe=r, extraction=ex,
                                                            checks=[])))["ignore_rows:交叉表"].label
    del ex["rows_excluded"]
    assert "本期" not in by_id(confirm_items(ConfirmContext(kind="first", recipe=r, extraction=ex,
                                                           checks=[])))["ignore_rows:交叉表"].label


def test_crosstab_ignore_columns_item():
    r = flow()
    block(r)["ignore_columns"] = [{"header": "合计", "reason": "右侧月合计列"}]
    ex = extraction_of(BASE[0])
    ex["ignored_columns"] = {"交叉表": ["合计"]}
    item = by_id(confirm_items(ConfirmContext(kind="first", recipe=r, extraction=ex, checks=[])))["ignored_columns:交叉表"]
    assert item.label == "「交叉表」按表头忽略列：「合计」（理由：右侧月合计列）" and item.detail == "本期忽略的列：合计"


def test_sources_and_order():
    """ConfirmItem.source 的新取值；高风险的一档排在配方类、本期类、差异卡之前（9.5 的排序）。"""
    new = flow(mode="accumulate")
    new["tables"][0]["units"]["分区甲"] = "人"
    edits = [{"kind": "fix", "key": "k", "title": "修改", "superseded": False}]
    diff = [outside_change("单位：人次", "单位：万人次")]
    items = redraft(new, old=flow(mode="accumulate"), kind="reupload", edits=edits, diff=diff,
                    accumulate=plan("replace_period", replaces={"start": "2026-09-01", "end": "2026-09-30"}))
    order = [i.id for i in items]
    assert order[0] == "unit_changed:日客流.分区甲"
    src = {i.id: i.source for i in items}
    assert src["period_replace:2026-09-01~2026-09-30"] == "accumulate" == src["accumulate_unit_risk"]
    assert src["fix:k"] == "edit" and src["unit:日客流.分区甲"] == "recipe" and src["diff:outside:客流汇总!B33"] == "diff"
    assert order.index("accumulate_unit_risk") < order.index("unit:日客流.分区甲") < order.index("fix:k") \
        < order.index("diff:outside:客流汇总!B33")


def test_plan_without_action_raises():
    with pytest.raises(ValueError, match="action"):
        acc_items({"mode": "accumulate"})
