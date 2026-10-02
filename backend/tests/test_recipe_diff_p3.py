"""期 3 的差异卡（recipe_diff，P3-SPEC 9.5、第 8 节遗留项 4）：按期累积计划的差异项、按配方忽略的行。

回执手写（构造器在 test_recipe_diff），累积计划按契约 AccumulatePlan 的 JSON 形状手写。
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from app.data import recipe_diff as D
from app.data.recipe_confirm import ConfirmContext, confirm_items
from app.data.recipe_types import AccumulatePlan
from tests.test_recipe_confirm_p3 import plan
from tests.test_recipe_diff import BASE, FLOW, SHEET, add_outside, extraction_of, kinds, run, sep


def by_kind(items: list[Any], kind: str) -> list[Any]:
    return [d for d in items if d.kind == kind]


# --------------------------------------------------------------------------
# 按期累积的计划
# --------------------------------------------------------------------------


def test_append_period_added_info():
    diff = run(BASE, sep(), accumulate=plan())
    item, = by_kind(diff, "period_added")
    assert item.label == "按期累积：本期 2026-09-01 至 2026-09-30 加入当前版本，启用后共 2 期"
    assert not item.requires_confirm
    assert kinds(diff) == {"period", "rows", "period_added"}


def test_replace_period_is_info_and_confirmed_elsewhere():
    rep = {"start": "2026-09-01", "end": "2026-09-30", "seq": 2}
    item, = by_kind(run(BASE, sep(), accumulate=plan("replace_period", replaces=rep)), "period_replaced")
    assert item.label == "按期累积：替换已有的一期 2026-09-01 至 2026-09-30" and not item.requires_confirm


def test_gap_requires_confirmation():
    p = plan(period={"start": "2026-10-01", "end": "2026-10-31"}, gaps=[{"start": "2026-09-01", "end": "2026-09-30"}])
    item, = by_kind(run(BASE, sep(), accumulate=p), "period_gap")
    assert item.requires_confirm and item.confirm_id == "diff:period_gap:2026-09-01~2026-09-30"
    assert "2026-09-01 至 2026-09-30 没有数据" in item.label
    assert not by_kind(run(BASE, sep(), accumulate=plan()), "period_gap")


def test_backfill_info_unless_period_entered_by_hand():
    p = plan(period={"start": "2026-07-01", "end": "2026-07-31"}, backfill=True)
    info, = by_kind(run(BASE, sep(), accumulate=p), "period_backfill")
    assert not info.requires_confirm and info.confirm_id is None and "早于已有各期" in info.label
    cur = sep()
    cur[0]["period"] = {**cur[0]["period"], "source": "human"}
    item, = by_kind(run(BASE, cur, accumulate=p), "period_backfill")
    assert item.requires_confirm and item.confirm_id == "diff:period_backfill:2026-07-01~2026-07-31"
    assert item.label == "本期统计期为人工录入，且早于已有各期：请核对年份和月份无误"


def test_columns_added_retired_and_labels_vary_are_info():
    ls = [{"table": "时段客流", "column": "时段", "segment": "日间",
           "periods": [{"start": "2026-08-01", "end": "2026-08-31", "missing": [], "extra": ["7-8"]},
                       {"start": "2026-09-01", "end": "2026-09-30", "missing": ["7-8"], "extra": []}]}]
    p = plan(added=[{"table": "日客流", "column": "分区丙"}], retired_new=[{"table": "日客流", "column": "分区乙"}],
             label_sets=ls, change="retire")
    diff = run(BASE, sep(), accumulate=p)
    added, = by_kind(diff, "columns_added")
    retired, = by_kind(diff, "columns_retired")
    vary, = by_kind(diff, "labels_vary")
    assert "表「日客流」的列「分区丙」" in added.label and "其他各期没有这些数据，为空值" in added.label
    assert "表「日客流」的列「分区乙」" in retired.label
    assert vary.label == "与其他各期相比，本期「时段」没有「7-8」"
    assert vary.detail == "表「时段客流」：跨期比较时各期覆盖的取值不同；其他各期：2026-08-01 至 2026-08-31 取值齐全"
    assert not any(d.requires_confirm for d in (added, retired, vary))


def test_labels_vary_is_relative_to_all_other_periods_not_earlier_ones():
    """label_sets 的 missing / extra 相对全部各期（评审意见）：8 月没有「7-8」，9 月、10 月有。上传 10 月时 extra 是
    「7-8」，但 9 月也有它，不能写「此前各期没有」；细节按期写出是哪一期缺。补传早期时同样不写「此前」。"""
    ls = [{"table": "时段客流", "column": "时段", "segment": "日间",
           "periods": [{"start": "2026-08-01", "end": "2026-08-31", "missing": ["7-8"], "extra": []},
                       {"start": "2026-09-01", "end": "2026-09-30", "missing": [], "extra": ["7-8"]},
                       {"start": "2026-10-01", "end": "2026-10-31", "missing": [], "extra": ["7-8"]}]}]
    p = plan(period={"start": "2026-10-01", "end": "2026-10-31"}, label_sets=ls)
    vary, = by_kind(run(BASE, sep(), accumulate=p), "labels_vary")
    assert vary.label == "与其他各期相比，本期「时段」有「7-8」，但并非各期都有"
    assert "此前" not in vary.label + vary.detail
    assert vary.detail.endswith("其他各期：2026-08-01 至 2026-08-31 没有「7-8」；2026-09-01 至 2026-09-30 取值齐全")
    back = plan(period={"start": "2026-08-01", "end": "2026-08-31"}, label_sets=ls, backfill=True)
    vary, = by_kind(run(BASE, sep(), accumulate=back), "labels_vary")
    assert vary.label == "与其他各期相比，本期「时段」没有「7-8」" and "此前" not in vary.detail


@pytest.mark.parametrize("p", [
    plan("restart", semantic=["x"]), plan("first"), plan("replace", mode="replace"), plan("rejected"),
    plan(mode="replace"),
])
def test_no_accumulate_items_unless_appending_or_replacing_a_period(p):
    p = {**p, "gaps": [{"start": "2026-09-01", "end": "2026-09-30"}], "added": [{"table": "日客流", "column": "x"}]}
    assert kinds(run(BASE, sep(), accumulate=p)) == {"period", "rows"}


def test_accumulate_items_even_when_recipe_changed_and_dataclass_accepted():
    p = plan(gaps=[{"start": "2026-09-01", "end": "2026-09-30"}])
    diff = run(BASE, sep(), accumulate=AccumulatePlan(**p), recipe_changed=True)
    assert kinds(diff) == {"period", "rows", "period_added", "period_gap"}
    # 需确认的排在前面
    assert diff[0].kind == "period_gap"


def test_new_kinds_registered():
    for k in ("period_added", "period_replaced", "period_gap", "period_backfill", "columns_added", "columns_retired",
              "labels_vary", "ignored_rows"):
        assert k in D.DIFF_KINDS
    assert D.CONFIRM_PREFIX["period_gap"] == "diff:period_gap:"
    assert D.CONFIRM_PREFIX["ignored_rows"] == "diff:ignored_rows:"
    assert D.CONFIRM_PREFIX["ignored_rows"] != D.CONFIRM_PREFIX["ignored_columns"]
    assert "rows_excluded" not in D.RECEIPT_KEYS


# --------------------------------------------------------------------------
# 按配方忽略的行（rows_excluded）
# --------------------------------------------------------------------------


def excl(reason: str = "ignored_rows", rows: list[list[int]] | None = None, anchor: str = "补录（人次）",
         sheet: str = SHEET, block: str | None = "交叉表") -> dict[str, Any]:
    return {"sheet": sheet, "reason": reason, "rows": rows or [[32, 32]], "cells": 3, "anchor": anchor, "block": block}


def with_excluded(doc: tuple[dict[str, Any], str], entries: list[dict[str, Any]] | None) -> tuple[dict[str, Any], str]:
    d = copy.deepcopy(doc)
    if entries is None:
        d[0]["receipt"].pop("rows_excluded", None)
    else:
        d[0]["receipt"]["rows_excluded"] = entries
    return d


def test_ignored_rows_change_requires_confirmation():
    prev = with_excluded(BASE, [])
    cur = with_excluded(sep(), [excl()])
    item, = by_kind(run(prev, cur), "ignored_rows")
    assert item.requires_confirm and item.confirm_id == f"diff:ignored_rows:{SHEET}"
    assert "本期 1 行" in item.label and "「补录（人次）」" in item.label and "上一期 0 行" in item.detail
    # 同样的锚点、同样的行数：不出
    assert not by_kind(run(with_excluded(BASE, [excl()]), cur), "ignored_rows")
    # 行数变了、锚点没变：要确认
    assert by_kind(run(with_excluded(BASE, [excl()]), with_excluded(sep(), [excl(rows=[[32, 33]])])), "ignored_rows")
    # 区域外的忽略也算
    assert by_kind(run(prev, with_excluded(sep(), [excl("ignored_outside", block=None)])), "ignored_rows")
    # 别的原因（跳过的空行、隐藏行）不在这里比
    assert not by_kind(run(prev, with_excluded(sep(), [excl("blank_skipped")])), "ignored_rows")


def test_prev_without_rows_excluded_is_info_not_empty_list():
    item, = by_kind(run(with_excluded(BASE, None), with_excluded(sep(), [excl(), excl("hidden_excluded")])),
                    "ignored_rows")
    assert not item.requires_confirm and item.confirm_id is None
    assert item.label == "上一期未记录排除的行，本期排除了 2 行"
    # 本期也没有排除的行：不出
    assert not by_kind(run(with_excluded(BASE, None), with_excluded(sep(), [])), "ignored_rows")


def test_ignored_rows_and_ignored_columns_ids_do_not_collide():
    """块 id 与工作表名相同（都叫「客流汇总」）时，期 2 的 diff:ignored:<块> 与 diff:ignored_rows:<工作表> 是两条：
    确认清单按 id 去重（_Bag），共用前缀的话后一条会被吞掉（评审一-m4）。"""
    prev = with_excluded(BASE, [])
    prev[0]["receipt"]["ignored_columns"] = {SHEET: []}
    cur = with_excluded(sep(), [excl()])
    cur[0]["receipt"]["ignored_columns"] = {SHEET: ["合计"]}
    diff = run(prev, cur)
    ids = {d.confirm_id for d in diff if d.requires_confirm}
    assert {f"diff:ignored:{SHEET}", f"diff:ignored_rows:{SHEET}"} <= ids
    items = confirm_items(ConfirmContext(kind="reupload", recipe=FLOW, old_recipe=FLOW,
                                         extraction=extraction_of(cur[0]), checks=cur[0]["checks"], diff=diff,
                                         prev=prev[0]))
    got = {i.id for i in items}
    assert {f"diff:ignored:{SHEET}", f"diff:ignored_rows:{SHEET}"} <= got


# --------------------------------------------------------------------------
# 区域外文字：按期累积时配方变了也比（评审意见 H2）
# --------------------------------------------------------------------------


def _unit_moved(cell: str = "B34") -> tuple[tuple[dict[str, Any], str], tuple[dict[str, Any], str]]:
    prev = copy.deepcopy(BASE)
    add_outside(prev[0], cell, "单位：人次", kind="text")
    cur = sep()
    add_outside(cur[0], cell, "单位：万人次", kind="text")
    return prev, cur


def _blank_null() -> dict[str, Any]:
    r = copy.deepcopy(FLOW)
    r["mode"] = "accumulate"
    r["sheets"][0]["blocks"][0]["values"]["blank"] = "null"
    return r


@pytest.mark.parametrize("p", [plan(), plan("replace_period", replaces={"start": "2026-09-01", "end": "2026-09-30"})])
def test_outside_diff_runs_when_recipe_changed_under_accumulate(p):
    """同一暂存区里应用过一次修复（配方变了），区域外「单位：人次」→「单位：万人次」照样出差异项，
    accumulate_unit_risk 照样出（只看配方没变时，单位变化会静默累积进同一列）。"""
    prev, cur = _unit_moved()
    new = _blank_null()
    diff = run(prev, cur, recipe=new, recipe_changed=True, accumulate=p)
    item, = by_kind(diff, "outside_changed")
    assert item.requires_confirm and item.confirm_id == f"diff:outside:{SHEET}!B34"
    # 其余逐项比较照旧不做（配方变了，分段和块不一定对得上）
    assert not kinds(diff) & {"label_writing", "axis_form", "ignored_columns", "hidden", "file_name"}
    old = copy.deepcopy(FLOW)
    old["mode"] = "accumulate"
    items = {i.id for i in confirm_items(ConfirmContext(
        kind="reupload", recipe=new, old_recipe=old, extraction=extraction_of(cur[0]), checks=cur[0]["checks"],
        diff=diff, prev=prev[0], accumulate=p))}
    assert {"accumulate_unit_risk", f"diff:outside:{SHEET}!B34"} <= items


@pytest.mark.parametrize("p", [None, plan("restart", semantic=["x"]), plan("first"), plan("replace", mode="replace"),
                               plan("rejected")])
def test_outside_diff_not_run_when_recipe_changed_without_accumulating(p):
    """每期替换、重新开始累积、首次：只影响一期，配方变了照期 2 只给行数和统计期。"""
    prev, cur = _unit_moved()
    assert kinds(run(prev, cur, recipe=_blank_null(), recipe_changed=True, accumulate=p)) == {"period", "rows"}


def test_outside_items_public_and_requires_keys():
    prev, cur = _unit_moved()
    got = D.outside_items(prev[0], cur[0])
    assert [d.kind for d in got] == ["outside_changed"]
    assert D.outside_items(prev[0], prev[0]) == []
    broken = copy.deepcopy(prev[0])
    del broken["receipt"]["outside_text"]
    with pytest.raises(D.ReceiptIncomplete) as e:
        D.outside_items(broken, cur[0])
    assert e.value.side == "prev" and e.value.missing == ["outside_text"]
    # diff_reports 在累积、配方变了时同样要求两边带齐，不按空处理
    with pytest.raises(D.ReceiptIncomplete):
        D.diff_reports(broken, cur[0], prev_file_name=prev[1], cur_file_name=cur[1], recipe=_blank_null(),
                       recipe_changed=True, accumulate=plan())
