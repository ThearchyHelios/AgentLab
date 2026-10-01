"""按期累积（期 3，WP-4 recipe_accumulate）：配方演进的分档、累积计划、并集的物化。

要守住的几件事（P3-SPEC 2.4–2.7、11.3 的 WP-4 部分）：
- 计划的判定表逐行成立：不兼容即 restart，而且优先于部分重叠的 rejected；相等的统计期是「替换该期」、要确认；
  模式切换时当前那一期有统计期、满足资格、配方兼容才作为第一期；
- 退役只对「这次才退役的」出确认项：连续三期只有第一次是 retired_new；退役名和新名只差大小写按不兼容；
  没给当前表结构时按当前各期推（新增列照样报得出）；restart、rejected 不带各期维度取值的差异；
- 并集 id 带上退役列的组成和顺序（顺序进库哈希）；retired_columns 先拼早先退役的、再拼这次才退役的；
- 物化：行数、rowid 区间、确定性、U1–U3 各自能失败、退役列、单期物化、组合表哈希；各期库被改过就 part_tampered，
  rowid 不连续就 union_failed；先写临时文件再 os.replace。

夹具：8 月、9 月用合成客流表（tests/fixtures/xlsx/flow.py，假名、随机数）按参考配方经执行器建库；其余变体的
期库用 sqlite3 直接建，数字是手写的假数。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import shutil
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from app.data import raw_store, recipe_accumulate as ra, recipe_engine, table_versions, xlsx_scan
from app.data.recipe_types import (
    UNION_VER, ColumnOut, PartInfo, Recipe, UnionPart, derive_tables,
)
from tests.fixtures.xlsx.flow import flow_workbook

FLOW = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text(encoding="utf-8"))


def recipe(edit=None, *, mode: str = "accumulate") -> Recipe:
    data = copy.deepcopy(FLOW)
    data["mode"] = mode
    if edit is not None:
        edit(data)
    return Recipe.model_validate(data)


def _segments(data: dict) -> list[dict]:
    return data["sheets"][0]["blocks"][0]["segments"]


def no_zone_b(data: dict) -> None:
    """去掉 measures 的分区乙（R1 一并去掉：sum_eq 至少两个成员）。"""
    seg = _segments(data)[0]
    seg["labels"]["expect"].remove("分区乙（人次）")
    del seg["measures"]["分区乙（人次）"]
    del data["tables"][0]["units"]["分区乙"]
    data["relations"] = [r for r in data["relations"] if r["id"] != "R1"]


def with_zone_c(data: dict) -> None:
    seg = _segments(data)[0]
    seg["labels"]["expect"].append("分区丙（人次）")
    seg["measures"]["分区丙（人次）"] = "分区丙"
    data["tables"][0]["units"]["分区丙"] = "人次"


def unit_changed(data: dict) -> None:
    data["tables"][0]["units"]["分区甲"] = "万人次"


def no_seven(data: dict) -> None:
    """D04 的修法：日间去掉「7-8」。"""
    _segments(data)[1]["labels"]["expect"].remove("7-8")


def no_covers(data: dict) -> None:
    data["sheets"][0]["blocks"][0]["axis"]["checks"] = ["contiguous"]


REF = recipe()
AUG = ("2026-08-01", "2026-08-31")
SEP = ("2026-09-01", "2026-09-30")
OCT = ("2026-10-01", "2026-10-31")
NOV = ("2026-11-01", "2026-11-30")


def part(period: tuple[str | None, str | None], *, r: Recipe | None = REF, seq: int = 1, name: str = "",
         rows: dict[str, int] | None = None) -> PartInfo:
    return PartInfo(import_id=f"imp{seq}", seq=seq, start=period[0], end=period[1], build_id=f"b{seq}",
                    db_path="", db_sha256="", manifest_db_sha256=None, recipe_id=f"r{seq}", recipe=r,
                    recipe_sha256=None, raw_state="kept", file_name=name or f"第{seq}期.xlsx", manifest_artifact=None,
                    rows=rows or {})


def tables_of(r: Recipe) -> dict[str, list[ColumnOut]]:
    return derive_tables(r)[0]


def plan(new: Recipe, period, *, kind: str = "reupload", mode: str | None = "accumulate",
         snap: str | None = "recipe", parts: list[PartInfo] = (), current=None, retired_status=None,
         source: str = "cells"):
    return ra.plan_accumulate(new_recipe=new, new_period=period, period_source=source, staging_kind=kind,
                              current_mode=mode, current_snapshot_kind=snap, parts=list(parts),
                              current_tables=current if current is not None else tables_of(REF),
                              retired_status=retired_status)


# ==========================================================================
# 累积计划：判定表
# ==========================================================================


def test_replace_recipe_stays_replace_without_mode_switch():
    p = plan(recipe(mode="replace"), SEP, mode="replace", parts=[part(AUG)])
    assert p.action == "replace" and p.mode == "replace" and p.mode_switch is None
    assert [x["new"] for x in p.parts] == [True] and p.dropped == []


def test_accumulate_to_replace_drops_every_period():
    p = plan(recipe(mode="replace"), OCT, parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "replace" and p.mode_switch == "accumulate->replace"
    assert [(d["start"], d["import_id"]) for d in p.dropped] == [("2026-08-01", "imp1"), ("2026-09-01", "imp2")]
    assert [x["new"] for x in p.parts] == [True]


@pytest.mark.parametrize("kind,mode,snap,parts", [
    ("first", None, None, []),
    ("switch", None, "simple", [part((None, None))]),
    ("reupload", "replace", None, []),
])
def test_first_import_and_switch_from_simple_start_from_this_period(kind, mode, snap, parts):
    p = plan(REF, AUG, kind=kind, mode=mode, snap=snap, parts=parts)
    assert p.action == "first" and p.mode == "accumulate"
    assert [(x["start"], x["end"], x["new"], x["import_id"]) for x in p.parts] == [(*AUG, True, None)]
    assert p.union is None


def test_switch_from_replace_keeps_the_current_period_as_the_first():
    old = recipe(mode="replace")
    p = plan(REF, SEP, mode="replace", parts=[part(AUG, r=old, rows={"日客流": 31})])
    assert p.action == "append" and p.mode_switch == "replace->accumulate"
    assert [(x["start"], x["new"]) for x in p.parts] == [("2026-08-01", False), ("2026-09-01", True)]
    assert p.parts[0]["rows"] == {"日客流": 31} and p.parts[0]["blockers"] == []
    assert p.dropped == [] and p.reason is None


def test_switch_from_replace_without_a_period_starts_over():
    p = plan(REF, SEP, mode="replace", parts=[part((None, None), r=recipe(mode="replace"))])
    assert p.action == "first" and p.mode_switch == "replace->accumulate"
    assert "没有统计期" in p.reason and [d["import_id"] for d in p.dropped] == ["imp1"]


def test_switch_from_replace_whose_recipe_is_not_eligible_starts_over():
    p = plan(REF, SEP, mode="replace", parts=[part(AUG, r=recipe(no_covers, mode="replace"))])
    assert p.action == "first" and p.mode_switch == "replace->accumulate"
    assert "不满足按期累积的要求" in p.reason and "恰好覆盖统计期" in p.reason
    assert p.dropped[0]["blockers"]


def test_switch_from_replace_with_an_incompatible_recipe_restarts():
    p = plan(recipe(unit_changed), SEP, mode="replace", parts=[part(AUG, r=recipe(mode="replace"))])
    assert p.action == "restart" and p.mode_switch == "replace->accumulate"
    assert p.change == "semantic" and any("万人次" in s for s in p.semantic)
    assert [x["new"] for x in p.parts] == [True] and len(p.dropped) == 1


def test_switch_from_replace_on_the_same_period_replaces_it():
    p = plan(REF, AUG, mode="replace", parts=[part(AUG, r=recipe(mode="replace"))])
    assert p.action == "replace_period" and p.replaces["import_id"] == "imp1"
    assert [x["new"] for x in p.parts] == [True]


def test_append_a_later_period():
    p = plan(REF, OCT, parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "append" and p.change == "same" and p.mode_switch is None
    assert [x["start"] for x in p.parts] == ["2026-08-01", "2026-09-01", "2026-10-01"]
    assert [x["new"] for x in p.parts] == [False, False, True]
    assert p.gaps == [] and p.backfill is False and p.overlaps == [] and p.replaces is None


def test_backfill_an_earlier_period_is_sorted_in():
    p = plan(REF, SEP, parts=[part(AUG), part(OCT, seq=2)])
    assert p.action == "append" and p.backfill is True
    assert [x["start"] for x in p.parts] == ["2026-08-01", "2026-09-01", "2026-10-01"]
    assert p.gaps == [] and p.period == {"start": "2026-09-01", "end": "2026-09-30", "source": "cells"}


def test_backfill_with_a_period_typed_by_hand_carries_the_source():
    """补传早期、本期统计期是人工录入：差异卡要改为需确认（2.3），计划把来源带给 WP-3。"""
    p = plan(REF, AUG, parts=[part(SEP), part(OCT, seq=2)], source="human")
    assert p.action == "append" and p.backfill is True and p.period["source"] == "human"
    assert [x["new"] for x in p.parts] == [True, False, False]


def test_gap_between_periods_is_reported():
    p = plan(REF, NOV, parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "append" and p.gaps == [{"start": "2026-10-01", "end": "2026-10-31"}]


def test_same_period_is_a_replace_candidate():
    p = plan(REF, SEP, parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "replace_period" and p.replaces["import_id"] == "imp2"
    assert [(x["start"], x["new"]) for x in p.parts] == [("2026-08-01", False), ("2026-09-01", True)]


def test_partial_overlap_is_rejected_and_listed():
    p = plan(REF, ("2026-08-15", "2026-09-14"), parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "rejected"
    assert [o["import_id"] for o in p.overlaps] == ["imp1", "imp2"]
    # 当前版本原样不动
    assert [x["import_id"] for x in p.parts] == ["imp1", "imp2"]
    msg = ra.overlap_message(p.period, p.overlaps)
    assert "2026-08-15 至 2026-09-14" in msg and "2026-08-01 至 2026-08-31" in msg
    assert "2026-09-01 至 2026-09-30" in msg and "部分重叠" in msg


def test_one_day_overlap_is_rejected():
    p = plan(REF, ("2026-08-31", "2026-09-29"), parts=[part(AUG)])
    assert p.action == "rejected" and [o["import_id"] for o in p.overlaps] == ["imp1"]


def test_incompatible_change_restarts_even_when_periods_overlap():
    """restart 优先于 rejected（评审一-m12）：重叠的各期写进 overlaps 只作提示。"""
    p = plan(recipe(unit_changed), ("2026-08-15", "2026-09-14"), parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "restart" and [o["import_id"] for o in p.overlaps] == ["imp1", "imp2"]
    assert "不兼容" in p.reason and len(p.dropped) == 2
    assert [x["new"] for x in p.parts] == [True]


def test_incompatible_change_restarts():
    p = plan(recipe(unit_changed), OCT, parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "restart" and p.change == "semantic"
    assert [d["import_id"] for d in p.dropped] == ["imp1", "imp2"]
    assert p.added == [] and p.retired_new == [] and p.retired_existing == []


def test_a_kept_period_that_is_not_eligible_restarts():
    p = plan(REF, OCT, parts=[part(AUG, r=recipe(no_covers)), part(SEP, seq=2)])
    assert p.action == "restart" and "第 1 期" in p.reason
    assert p.dropped[0]["blockers"] and p.dropped[1]["blockers"] == []


def test_redraft_replaces_the_latest_period():
    p = plan(recipe(no_seven), SEP, kind="redraft", parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "replace_period" and p.replaces["import_id"] == "imp2"
    assert p.change == "compatible"


def test_redraft_with_an_incompatible_recipe_restarts():
    p = plan(recipe(unit_changed), SEP, kind="redraft", parts=[part(AUG), part(SEP, seq=2)])
    assert p.action == "restart" and len(p.dropped) == 2


def test_accumulate_needs_a_period():
    with pytest.raises(ValueError, match="统计期"):
        plan(REF, (None, None), parts=[part(AUG)])


def test_new_recipe_that_is_not_eligible_is_rejected():
    p = plan(recipe(no_covers), OCT, parts=[part(AUG)])
    assert p.action == "rejected" and "恰好覆盖统计期" in p.reason


# ==========================================================================
# 配方演进：兼容、退役、不兼容
# ==========================================================================


def test_identical_recipes_are_the_same():
    cc = ra.classify_changes([part(AUG), part(SEP, seq=2)], REF, current_tables=tables_of(REF))
    assert cc.change == "same" and not (cc.added or cc.retired_new or cc.retired_existing or cc.semantic)


def test_new_column_is_compatible():
    target = recipe(with_zone_c)
    cc = ra.classify_changes([part(AUG), part(SEP, r=target, seq=2)], target, current_tables=tables_of(REF))
    assert cc.change == "compatible" and cc.added == [{"table": "日客流", "column": "分区丙"}]
    assert cc.semantic == [] and cc.retired_new == []


def test_unit_change_is_semantic():
    cc = ra.classify_changes([part(AUG)], recipe(unit_changed), current_tables=tables_of(REF))
    assert cc.change == "semantic" and cc.semantic and "万人次" in cc.semantic[0]


def test_removed_column_retires_only_once_over_three_periods():
    """连续三期：去掉分区乙的那一期出 retired_new，之后各期是 retired_existing、不再出确认项（评审二-M3）。"""
    target = recipe(no_zone_b)
    first = plan(target, OCT, parts=[part(AUG), part(SEP, seq=2)])
    assert first.action == "append" and first.change == "retire"
    assert first.retired_new == [{"table": "日客流", "column": "分区乙"}] and first.retired_existing == []
    # 10 月提交后，当前版本的实际表结构仍含分区乙（保留在并集里），快照清单把它标成 retired
    current = tables_of(REF)
    status = {"日客流": {"分区乙": {"status": "retired", "since": "2026-10-01~2026-10-31", "unit": "人次"}}}
    second = plan(target, NOV, parts=[part(AUG), part(SEP, seq=2), part(OCT, r=target, seq=3)],
                  current=current, retired_status=status)
    assert second.action == "append" and second.change == "compatible"
    assert second.retired_new == [] and second.retired_existing == [{"table": "日客流", "column": "分区乙"}]


def test_retired_table_is_existing_only_when_every_column_is_marked():
    target = recipe()
    current = {**tables_of(REF), "旧表": [ColumnOut("日期", "TEXT", role="axis"), ColumnOut("值", "INTEGER")]}
    fresh = ra.classify_changes([part(AUG)], target, current_tables=current)
    assert {"table": "旧表", "column": None} in fresh.retired_new
    half = ra.classify_changes([part(AUG)], target, current_tables=current,
                               retired_status={"旧表": {"日期": {"status": "retired"}}})
    assert {"table": "旧表", "column": None} in half.retired_new
    done = ra.classify_changes([part(AUG)], target, current_tables=current, retired_status={
        "旧表": {"日期": {"status": "retired"}, "值": {"status": "retired"}}})
    assert done.retired_existing == [{"table": "旧表", "column": None}] and done.retired_new == []


def test_new_name_colliding_with_a_retired_name_is_semantic():
    def add_lower(data: dict) -> None:
        seg = _segments(data)[0]
        seg["labels"]["expect"].append("PM2.5（人次）")
        seg["measures"]["PM2.5（人次）"] = "pm2_5"
    current = copy.deepcopy(tables_of(REF))
    current["日客流"].append(ColumnOut("PM2_5", "INTEGER"))
    status = {"日客流": {"PM2_5": {"status": "retired"}}}
    cc = ra.classify_changes([part(AUG)], recipe(add_lower), current_tables=current, retired_status=status)
    assert cc.change == "semantic" and "只差大小写" in cc.semantic[0]
    p = plan(recipe(add_lower), SEP, parts=[part(AUG)], current=current, retired_status=status)
    assert p.action == "restart"


def test_label_sets_record_which_periods_lack_a_label():
    target = recipe(no_seven)
    cc = ra.classify_changes([part(AUG), part(SEP, r=target, seq=2)], target, current_tables=tables_of(REF))
    assert cc.change == "compatible" and cc.semantic == []
    [entry] = cc.label_sets
    assert (entry["table"], entry["column"], entry["segment"]) == ("时段客流", "时段", "日间")
    aug, sep = entry["periods"]
    assert (aug["start"], aug["missing"], aug["extra"]) == ("2026-08-01", [], ["7-8"])
    assert (sep["start"], sep["missing"], sep["extra"]) == ("2026-09-01", ["7-8"], [])
    p = plan(target, SEP, kind="redraft", parts=[part(AUG), part(SEP, seq=2)])
    assert p.label_sets and p.label_sets[0]["segment"] == "日间"


def test_label_sets_compare_canonical_writing():
    """全角、en-dash 写法按规范写法比：只是写法不同不算取值不同。"""
    def dashed(data: dict) -> None:
        seg = _segments(data)[1]
        seg["labels"]["expect"] = [x.replace("-", "–") for x in seg["labels"]["expect"]]
    cc = ra.classify_changes([part(AUG), part(SEP, r=recipe(dashed), seq=2)], REF, current_tables=tables_of(REF))
    assert cc.label_sets == []


def test_missing_current_tables_falls_back_to_the_parts():
    target = recipe(no_zone_b)
    cc = ra.classify_changes([part(AUG)], target, current_tables=None)
    assert cc.retired_new == [{"table": "日客流", "column": "分区乙"}]


def _raw_plan(new: Recipe, period, parts: list[PartInfo], *, kind: str = "reupload", current=None):
    return ra.plan_accumulate(new_recipe=new, new_period=period, period_source="cells", staging_kind=kind,
                              current_mode="accumulate", current_snapshot_kind="recipe", parts=parts,
                              current_tables=current)


def test_plan_without_current_tables_still_reports_added_columns():
    """current_tables 为 None 时按当前各期的配方推当前表结构，不能把本期也算进「当前已有」：那样新增列永远报不出来。"""
    target = recipe(with_zone_c)
    p = _raw_plan(target, SEP, [part(AUG)])
    assert p.action == "append" and p.added == [{"table": "日客流", "column": "分区丙"}]
    assert p.added == _raw_plan(target, SEP, [part(AUG)], current=tables_of(REF)).added
    # 修改配方重建最近一期：分区丙在要被替换的 9 月里已经有了，它此刻仍在当前版本中，不算新增
    redraft = _raw_plan(target, SEP, [part(AUG), part(SEP, r=target, seq=2)], kind="redraft")
    assert redraft.action == "replace_period" and redraft.added == []
    # 退役同样比得出来
    gone = _raw_plan(recipe(no_zone_b), SEP, [part(AUG)])
    assert gone.retired_new == [{"table": "日客流", "column": "分区乙"}]


def test_restart_and_rejected_plans_carry_no_label_sets():
    """restart 的结果只有本期、rejected 没有结果版本：各期维度取值的差异没有落点，不能让差异卡说「此前各期有」。"""
    def no_seven_and_unit(data: dict) -> None:
        no_seven(data)
        unit_changed(data)
    restart = plan(recipe(no_seven_and_unit), SEP, parts=[part(AUG)])
    assert restart.action == "restart" and [x["start"] for x in restart.parts] == ["2026-09-01"]
    assert restart.label_sets == []
    rejected = plan(recipe(no_seven), ("2026-08-15", "2026-09-14"), parts=[part(AUG)])
    assert rejected.action == "rejected" and rejected.label_sets == []
    # 对照：同样的取值差异在追加时照常记下
    assert plan(recipe(no_seven), SEP, parts=[part(AUG)]).label_sets


# ==========================================================================
# 并集 id
# ==========================================================================


def test_union_id_ignores_import_ids_and_input_order():
    a = ra.union_id("src1", [("2026-08-01", "2026-08-31", "b1"), ("2026-09-01", "2026-09-30", "b2")], "r" * 64)
    b = ra.union_id("src1", [("2026-09-01", "2026-09-30", "b2"), ("2026-08-01", "2026-08-31", "b1")], "r" * 64)
    assert a == b and len(a) == 64
    assert table_versions.sha_json({"source_id": "src1", "parts": [["2026-08-01", "2026-08-31", "b1"],
                                                                     ["2026-09-01", "2026-09-30", "b2"]],
                                    "target_recipe_sha256": "r" * 64, "union_ver": UNION_VER}) == a
    assert ra.union_id("src1", [("2026-08-01", "2026-08-31", "b1")], "r" * 64) != a
    assert ra.union_id("src1", [("2026-08-01", "2026-08-31", "b1"), ("2026-09-01", "2026-09-30", "b2")],
                       "s" * 64) != a
    assert ra.union_id("src2", [("2026-08-01", "2026-08-31", "b1"), ("2026-09-01", "2026-09-30", "b2")],
                       "r" * 64) != a


def test_union_id_commits_to_the_retired_layout():
    """退役列有历史：同样的各期、同样的目标配方，退役列的组成、顺序、类型不同就是结构不同的文件，id 必须跟着不同。"""
    parts = [("2026-08-01", "2026-08-31", "b1"), ("2026-09-01", "2026-09-30", "b2")]
    base = ra.union_id("src1", parts, "r" * 64)
    assert ra.union_id("src1", parts, "r" * 64, retired=None) == base
    assert ra.union_id("src1", parts, "r" * 64, retired={}) == base
    assert ra.union_id("src1", parts, "r" * 64, retired={"日客流": []}) == base
    d, e = ColumnOut("分区丁", "INTEGER"), ColumnOut("分区戊", "INTEGER")
    one = ra.union_id("src1", parts, "r" * 64, retired={"日客流": [d]})
    assert one != base
    assert one == table_versions.sha_json({
        "source_id": "src1", "parts": [list(x) for x in parts], "target_recipe_sha256": "r" * 64,
        "union_ver": UNION_VER, "retired": [["日客流", [["分区丁", "INTEGER"]]]]})
    de = ra.union_id("src1", parts, "r" * 64, retired={"日客流": [d, e]})
    ed = ra.union_id("src1", parts, "r" * 64, retired={"日客流": [e, d]})
    assert len({one, de, ed}) == 3
    assert ra.union_id("src1", parts, "r" * 64, retired={"日客流": [ColumnOut("分区丁", "REAL")]}) != one
    # 登记的 options 与 id 的输入一一对应
    assert ra.union_options(parts, "r" * 64) == {"kind": "union", "parts": [list(x) for x in parts],
                                                 "target_recipe_sha256": "r" * 64}
    assert ra.union_options(parts, "r" * 64, retired={"日客流": [d, e]})["retired"] == [
        ["日客流", [["分区丁", "INTEGER"], ["分区戊", "INTEGER"]]]]


# ==========================================================================
# 物化
# ==========================================================================


@lru_cache(maxsize=None)
def _built(start: str, days: int, seed: int = 0) -> tuple[str, str, str, str, tuple]:
    """按参考配方（累积）经执行器建一期的库，进程内只建一次。返回 (路径, 哈希, 起, 止, 表哈希)。"""
    import tempfile

    raw, name = flow_workbook(dt.date.fromisoformat(start), days, seed=seed)
    folder = Path(tempfile.mkdtemp(prefix="wp4-built-"))
    db = folder / f"{start}-{seed}.db"
    ex = recipe_engine.execute(REF, raw, name, xlsx_scan.scan(raw), str(db))
    assert ex.ok, ex.problems
    table_versions.settle_journal(db)
    os.chmod(db, 0o444)
    return str(db), raw_store.sha256_file(db), ex.period.start, ex.period.end, tuple(sorted(ex.table_hashes.items()))


def built_part(first_day: str, days: int, *, seed: int = 0, r: Recipe = REF, **kw: Any) -> UnionPart:
    """一期的 UnionPart；kw 可以改登记的统计期、路径、哈希、表哈希（造各种失败用）。"""
    path, sha, s, e, hashes = _built(first_day, days, seed)
    return UnionPart(import_id=kw.pop("import_id", None), start=kw.pop("start", s), end=kw.pop("end", e),
                     db_path=kw.pop("db_path", path), recipe=r, db_sha256=kw.pop("db_sha256", sha),
                     table_hashes=kw.pop("table_hashes", dict(hashes)), **kw)


def aug_sep() -> list[UnionPart]:
    return [built_part("2026-08-01", 31), built_part("2026-09-01", 30)]


def count(path: Path, sql: str, params=()) -> Any:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_two_periods_materialize_with_rows_ranges_and_checks(tmp_path):
    out = tmp_path / "u.db"
    rep = ra.materialize_union(out, target=REF, parts=aug_sep())
    assert rep.ok and [c.status for c in rep.checks] == ["passed"] * 3
    assert [(c.id, c.kind, c.category) for c in rep.checks] == [
        ("U1", "union_rows", "structure"), ("U2", "union_pk", "structure"), ("U3", "union_period", "structure")]
    assert rep.rows == {"日客流": 61, "时段客流": 1037, "时段客流_表内合计": 183}
    assert rep.part_rows[0]["日客流"] == {"union": [1, 31], "part": [1, 31]}
    assert rep.part_rows[1]["日客流"] == {"union": [32, 61], "part": [1, 30]}
    assert rep.part_rows[1]["时段客流"] == {"union": [528, 1037], "part": [1, 510]}
    assert rep.db_sha256 == raw_store.sha256_file(out)
    assert count(out, 'SELECT COUNT(*) FROM "日客流"') == 61
    assert count(out, 'SELECT COUNT(*) FROM "日客流" WHERE substr("日期", 1, 7) = ?', ("2026-09",)) == 30
    assert count(out, 'SELECT COUNT(*) FROM "时段客流" WHERE "时段" = ?', ("8-9",)) == 61
    # 表结构与执行器的写法相同：STRICT、主键
    ddl = count(out, "SELECT sql FROM sqlite_master WHERE name = '日客流'")
    assert "STRICT" in ddl and 'PRIMARY KEY ("日期")' in ddl
    assert count(out, "PRAGMA journal_mode") == "delete"
    assert not list(tmp_path.glob(f"*{raw_store.TMP_MARK}*"))
    assert rep.added == [] and rep.retired == []
    assert rep.part_null_counts[1]["时段客流"]["客流"] == count(
        out, 'SELECT COUNT(*) FROM "时段客流" WHERE rowid BETWEEN 528 AND 1037 AND "客流" IS NULL')
    assert rep.null_counts["时段客流"]["客流"] == count(out, 'SELECT COUNT(*) FROM "时段客流" WHERE "客流" IS NULL')


def test_materialize_is_deterministic_and_replaces_an_existing_file(tmp_path):
    first = ra.materialize_union(tmp_path / "a.db", target=REF, parts=aug_sep())
    out = tmp_path / "b.db"
    out.write_bytes(b"stale union from an older trial")
    second = ra.materialize_union(out, target=REF, parts=aug_sep())
    assert first.db_sha256 == second.db_sha256 == raw_store.sha256_file(out)


def test_composite_table_hash_uses_registered_part_hashes(tmp_path):
    parts = aug_sep()
    rep = ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    cols = [[c.name, c.type] for c in tables_of(REF)["日客流"]]
    want = table_versions.sha_json({"union_ver": UNION_VER, "columns": cols, "grain": ["日期"],
                                    "parts": [parts[0].table_hashes["日客流"], parts[1].table_hashes["日客流"]]})
    assert rep.table_hashes["日客流"] == want
    # 换一期登记的表哈希，组合哈希跟着变；导入记录 id 不参与
    other = aug_sep()
    other[1].table_hashes = {**other[1].table_hashes, "日客流": "0" * 64}
    other[0].import_id = "another-import"
    rep2 = ra.materialize_union(tmp_path / "v.db", target=REF, parts=other)
    assert rep2.table_hashes["日客流"] != want
    assert rep2.table_hashes["时段客流"] == rep.table_hashes["时段客流"]


def test_tampered_part_is_refused_before_attaching(tmp_path):
    parts = aug_sep()
    parts[1].db_sha256 = "0" * 64
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    assert err.value.code == "part_tampered" and err.value.part == 2 and "第 2 期" in str(err.value)
    assert not (tmp_path / "u.db").exists()


def test_part_whose_bytes_changed_is_tampered(tmp_path):
    parts = aug_sep()
    copy_path = tmp_path / "aug.db"
    shutil.copyfile(parts[0].db_path, copy_path)
    with open(copy_path, "ab") as fh:
        fh.write(b"\0")
    parts[0].db_path = str(copy_path)           # 登记的哈希仍是原来的
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    assert err.value.code == "part_tampered" and err.value.part == 1


def test_manifest_hash_must_match_too(tmp_path):
    parts = aug_sep()
    parts[0].manifest_db_sha256 = "1" * 64
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    assert err.value.code == "part_tampered" and err.value.part == 1


def test_missing_part_file(tmp_path):
    parts = aug_sep()
    parts[1].db_path = str(tmp_path / "gone.db")
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    assert err.value.code == "part_missing" and err.value.part == 2
    # 接口层直接转发这句，所以出路要写在里面（7.4）
    msg = str(err.value)
    assert "第 2 期（2026-09-01 至 2026-09-30）" in msg and "移除这一期" in msg and "启用更早的版本" in msg


def test_rowid_gap_in_a_part_fails(tmp_path):
    parts = aug_sep()
    holed = tmp_path / "holed.db"
    shutil.copyfile(parts[1].db_path, holed)
    os.chmod(holed, 0o644)
    conn = sqlite3.connect(holed)
    conn.execute('DELETE FROM "日客流" WHERE rowid = 5')
    conn.commit()
    conn.close()
    parts[1].db_path, parts[1].db_sha256 = str(holed), raw_store.sha256_file(holed)
    out = tmp_path / "u.db"
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(out, target=REF, parts=parts)
    assert err.value.code == "union_failed" and err.value.part == 2 and "行号不连续" in str(err.value)
    assert not out.exists() and not list(tmp_path.glob(f"*{raw_store.TMP_MARK}*"))


def test_duplicate_primary_key_fails_u2_and_produces_no_file(tmp_path):
    """两期写了同一天（计划本该拒收的重叠）：写入撞上主键，U2 不通过并指出哪两期、哪个主键。"""
    aug = built_part("2026-08-01", 31)
    again = built_part("2026-08-01", 31, seed=0)
    out = tmp_path / "u.db"
    out.write_bytes(b"stale")
    rep = ra.materialize_union(out, target=REF, parts=[aug, again])
    assert rep.ok is False and rep.db_sha256 == ""
    u1, u2, u3 = rep.checks
    assert u2.status == "mismatch" and "第 1 期与第 2 期" in u2.details[0] and "日期=2026-08-01" in u2.details[0]
    assert u1.status == "mismatch"           # 回滚的那一期没写进去，行数也对不上
    assert u3.status == "passed"
    assert not out.exists() and not list(tmp_path.glob(f"*{raw_store.TMP_MARK}*"))


def test_dates_outside_the_period_fail_u3(tmp_path):
    aug = built_part("2026-08-01", 31, start="2026-09-01", end="2026-09-30")
    rep = ra.materialize_union(tmp_path / "u.db", target=REF, parts=[aug])
    assert rep.ok is False
    u3 = rep.checks[2]
    assert u3.status == "mismatch" and u3.failed == 3
    assert "第 1 期（2026-09-01 至 2026-09-30）的「日客流」有 31 行日期不在该期统计期内" in u3.details
    assert u3.sql and u3.params[2:] == ["2026-09-01", "2026-09-30"]


def _small_part(path: Path, r: Recipe, start: str, days: int, values: dict[str, list[int]]) -> UnionPart:
    """只含「日客流」一张表的期库（sqlite3 直接建，列按 r 推出的表结构、写法同执行器）。"""
    cols = tables_of(r)["日客流"]
    conn = sqlite3.connect(path)
    defs = ", ".join(f'"{c.name}" {c.type}' + (" NOT NULL" if c.name == "日期" else "") for c in cols)
    conn.execute(f'CREATE TABLE "日客流" ({defs}, PRIMARY KEY ("日期")) STRICT')
    first = dt.date.fromisoformat(start)
    for i in range(days):
        row = [(first + dt.timedelta(days=i)).isoformat()] + [values[c.name][i] for c in cols[1:]]
        conn.execute(f'INSERT INTO "日客流" VALUES ({", ".join("?" * len(cols))})', row)
    conn.commit()
    conn.close()
    table_versions.settle_journal(path)
    end = (first + dt.timedelta(days=days - 1)).isoformat()
    return UnionPart(import_id=None, start=start, end=end, db_path=str(path), recipe=r,
                     db_sha256=raw_store.sha256_file(path), table_hashes={"日客流": f"h-{start}"})


def test_retired_column_keeps_old_values_and_later_periods_are_null(tmp_path):
    target = recipe(no_zone_b)
    aug = built_part("2026-08-01", 31)
    sep = _small_part(tmp_path / "sep.db", target, "2026-09-01", 3,
                      {"全日客流": [30, 31, 32], "分区甲": [30, 31, 32]})
    retired = {"日客流": [ColumnOut("分区乙", "INTEGER")]}
    rep = ra.materialize_union(tmp_path / "u.db", target=target, parts=[aug, sep], retired=retired)
    assert rep.ok, rep.checks
    names = [c.name for c in rep.tables["日客流"]]
    assert names == ["日期", "全日客流", "分区甲", "分区乙"]       # 目标的列在前、退役的追加在后
    zone_b = rep.tables["日客流"][-1]
    assert zone_b.unit == "人次" and zone_b.header == "分区乙（人次）"   # 取最后一个含它的那一期
    out = tmp_path / "u.db"
    assert count(out, 'SELECT COUNT(*) FROM "日客流" WHERE "分区乙" IS NOT NULL AND rowid <= 31') == 31
    assert count(out, 'SELECT COUNT(*) FROM "日客流" WHERE "分区乙" IS NULL AND rowid > 31') == 3
    assert {"table": "日客流", "column": "分区乙", "periods": ["2026-08-01~2026-08-31"]} in rep.retired
    # 按期的空值数只记该期有的列：9 月没有分区乙，就没有这个键（结构性空值不能说成「原表为空格」）
    assert "分区乙" in rep.part_null_counts[0]["日客流"] and "分区乙" not in rep.part_null_counts[1]["日客流"]
    assert rep.null_counts["日客流"]["分区乙"] == 3
    # 9 月没有另外两张表：那几张表只有 8 月的行，记作只有部分期
    assert rep.rows["时段客流"] == 527 and "时段客流" not in rep.part_rows[1]
    assert {"table": "时段客流", "column": None, "periods": ["2026-08-01~2026-08-31"]} in rep.added


def test_require_union_ok_turns_a_failed_report_into_union_failed(tmp_path):
    """U1–U3 不通过时 materialize_union 只返回 ok=False（试运行要展示核对结果）；版本页的写操作用 require_union_ok
    换成 union_failed，消息带上不通过的细节。"""
    bad = ra.materialize_union(tmp_path / "bad.db", target=REF,
                               parts=[built_part("2026-08-01", 31, start="2026-09-01", end="2026-09-30")])
    assert bad.ok is False
    with pytest.raises(ra.UnionError) as err:
        ra.require_union_ok(bad)
    assert err.value.code == "union_failed" and err.value.part is None
    assert "日期不在该期统计期内" in str(err.value)
    ra.require_union_ok(ra.materialize_union(tmp_path / "good.db", target=REF, parts=aug_sep()))


def test_retired_columns_put_earlier_retirements_first(tmp_path):
    """先早先退役的（按当前表结构里的列序）、再这次才退役的（2.6「按退役先后追加」）；照 current_tables 直接拼会
    把新退役的排到前面。顺序进库哈希，所以 union_id 也要带上同一个 retired。"""
    current = copy.deepcopy(tables_of(REF))
    current["日客流"].append(ColumnOut("分区丁", "INTEGER", unit="人次"))
    current["旧表"] = [ColumnOut("日期", "TEXT", role="axis"), ColumnOut("值", "INTEGER")]
    status = {"日客流": {"分区丁": {"status": "retired"}}, "旧表": {"日期": {"status": "retired"},
                                                                  "值": {"status": "retired"}}}
    target = recipe(no_zone_b)
    p = plan(target, SEP, parts=[part(AUG)], current=current, retired_status=status)
    assert p.retired_existing == [{"table": "日客流", "column": "分区丁"}, {"table": "旧表", "column": None}]
    assert p.retired_new == [{"table": "日客流", "column": "分区乙"}]
    retired = ra.retired_columns(p, current)
    assert list(retired) == ["日客流", "旧表"]
    assert [c.name for c in retired["日客流"]] == ["分区丁", "分区乙"]
    assert [c.name for c in retired["旧表"]] == ["日期", "值"]
    cc = ra.classify_changes([part(AUG)], target, current_tables=current, retired_status=status)
    assert ra.retired_layout(ra.retired_columns(cc, current)) == ra.retired_layout(retired)
    rep = ra.materialize_union(tmp_path / "u.db", target=target, parts=[built_part("2026-08-01", 31)],
                               retired=retired)
    assert rep.ok, rep.checks
    assert [c.name for c in rep.tables["日客流"]] == ["日期", "全日客流", "分区甲", "分区丁", "分区乙"]
    assert rep.rows["旧表"] == 0
    # 换个顺序就是另一个文件，id 也跟着不同
    swapped = {"日客流": list(reversed(retired["日客流"])), "旧表": retired["旧表"]}
    rep2 = ra.materialize_union(tmp_path / "v.db", target=target, parts=[built_part("2026-08-01", 31)],
                                retired=swapped)
    assert rep2.db_sha256 != rep.db_sha256
    ids = [("2026-08-01", "2026-08-31", "b1")]
    assert ra.union_id("s", ids, "r" * 64, retired=retired) != ra.union_id("s", ids, "r" * 64, retired=swapped)
    assert ra.retired_columns(p, None) == {}


def test_part_column_without_a_place_in_the_union_fails(tmp_path):
    """目标去掉了分区乙、却没把它作为退役列传进来：那一列的数据没地方放，不能悄悄丢掉。"""
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=recipe(no_zone_b), parts=[built_part("2026-08-01", 31)])
    assert err.value.code == "union_failed" and "分区乙" in str(err.value)


def test_single_period_materialized_by_a_different_target(tmp_path):
    """移除、撤销之后只剩一期、而那一期的配方不是目标配方：按目标表结构物化成「一期的并集」。"""
    target = recipe(with_zone_c)
    rep = ra.materialize_union(tmp_path / "u.db", target=target, parts=[built_part("2026-08-01", 31)])
    assert rep.ok
    assert [c.name for c in rep.tables["日客流"]][-1] == "分区丙"
    assert rep.null_counts["日客流"]["分区丙"] == 31
    assert {"table": "日客流", "column": "分区丙", "periods": []} in rep.added
    assert rep.rows == {"日客流": 31, "时段客流": 527, "时段客流_表内合计": 93}


def test_missing_table_hash_is_refused(tmp_path):
    parts = aug_sep()
    parts[0].table_hashes = {}
    with pytest.raises(ra.UnionError) as err:
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=parts)
    assert err.value.code == "union_failed" and "表哈希" in str(err.value)


def test_parts_must_be_sorted(tmp_path):
    with pytest.raises(ValueError, match="排序"):
        ra.materialize_union(tmp_path / "u.db", target=REF, parts=list(reversed(aug_sep())))
