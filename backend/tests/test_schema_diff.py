"""表结构的前后差异（app/data/schema_diff.py，next-round 03 B1）：纯函数。

守的是：加列、删列、类型、可空、注释（列和表）、主键、唯一约束、外键各出一条；疑似改名只说「可能」（类型和位置都对上、
而且只有一对）；读不到唯一约束、外键的一边不比；离开范围、读不到结构不算删除；升级后第一次读到（版本 0）只建基线。
表、列都是虚构的通用名字。
"""
from __future__ import annotations

import copy

from app.data import schema_diff as sd


def _meta(**over):
    meta = {
        "qualified": "orders", "schema": None, "comment": "订单", "is_view": False,
        "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False, "comment": None},
            {"name": "status", "type": "VARCHAR(10)", "nullable": True, "comment": "状态"},
            {"name": "amount", "type": "NUMERIC(12, 2)", "nullable": True, "comment": None},
            {"name": "paid_at", "type": "TIMESTAMP", "nullable": True, "comment": None},
        ],
        "primary_key": ["id"],
        "unique": [],
        "foreign_keys": [{"columns": ["store_id"], "to_table": "stores", "to_columns": ["id"]}],
    }
    meta.update(over)
    return meta


def _kinds(diffs):
    return [(d.kind, d.column) for d in diffs]


def test_same_structure_has_no_changes():
    assert sd.diff_table(_meta(), _meta()) == []


def test_added_removed_type_nullable_and_comment_each_get_one_change():
    before = _meta()
    after = copy.deepcopy(before)
    cols = after["columns"]
    cols[1]["type"] = "VARCHAR(20)"          # 类型变长
    cols[2]["nullable"] = False               # 改成非空
    cols[3]["comment"] = "支付时间"            # 补了注释
    cols.append({"name": "channel", "type": "VARCHAR(8)", "nullable": True, "comment": None})
    after["columns"] = [c for c in cols if c["name"] != "amount"] + []
    after["columns"].insert(2, {"name": "amount", "type": "NUMERIC(12, 2)", "nullable": False, "comment": None})
    after["comment"] = "订单明细"
    diffs = sd.diff_table(before, after)
    assert _kinds(diffs) == [
        ("column_added", "channel"),
        ("column_type_changed", "status"), ("column_nullable_changed", "amount"),
        ("comment_changed", "paid_at"), ("comment_changed", None),
    ]
    by = {(d.kind, d.column): d for d in diffs}
    assert by[("column_type_changed", "status")].before == "VARCHAR(10)"
    assert by[("column_type_changed", "status")].after == "VARCHAR(20)"
    assert by[("column_nullable_changed", "amount")].before is True and by[("column_nullable_changed", "amount")].after is False
    assert by[("comment_changed", None)].after == "订单明细"


def test_removed_column_is_a_deletion_and_left_scope_is_not():
    after = _meta(columns=[c for c in _meta()["columns"] if c["name"] != "amount"])
    [d] = sd.diff_table(_meta(), after)
    assert d.kind == "column_removed" and d.column == "amount" and d.before["type"] == "NUMERIC(12, 2)"
    assert d.rename is None, "没有新加的列，谈不上改名"
    assert sd.is_deletion("column_removed") and sd.is_deletion("table_removed")
    assert not sd.is_deletion("table_left_scope") and not sd.is_deletion("table_reflect_failed"), "范围外、读不到不算删除"
    assert d.severity == "warning"


def test_same_type_same_position_is_only_a_possible_rename():
    after = copy.deepcopy(_meta())
    after["columns"][1] = {"name": "state", "type": "VARCHAR(10)", "nullable": True, "comment": "状态"}
    diffs = sd.diff_table(_meta(), after)
    assert _kinds(diffs) == [("column_removed", "status"), ("column_added", "state")]
    assert diffs[0].rename == {"from": "status", "to": "state"} == diffs[1].rename
    assert diffs[0].kind == "column_removed", "改名只说「可能」：照样是一条删列、一条加列"


def test_rename_needs_same_type_and_same_position():
    moved = copy.deepcopy(_meta())
    moved["columns"] = [moved["columns"][0], moved["columns"][2], moved["columns"][3],
                        {"name": "state", "type": "VARCHAR(10)", "nullable": True, "comment": None}]
    diffs = sd.diff_table(_meta(), moved)
    assert all(d.rename is None for d in diffs), "位置不同：不说改名"
    retyped = copy.deepcopy(_meta())
    retyped["columns"][1] = {"name": "state", "type": "INTEGER", "nullable": True, "comment": None}
    assert all(d.rename is None for d in sd.diff_table(_meta(), retyped)), "类型不同：不说改名"


def test_keys_and_foreign_keys():
    after = _meta(primary_key=["id", "status"], unique=[["paid_at"]],
                  foreign_keys=[{"columns": ["store_id"], "to_table": "shops", "to_columns": ["id"]}])
    diffs = sd.diff_table(_meta(), after)
    assert [(d.kind, d.key, d.column) for d in diffs] == [
        ("key_changed", "primary", None), ("key_changed", "unique", "paid_at"),
        ("fk_changed", None, "store_id"), ("fk_changed", None, "store_id")]
    fk_gone, fk_new = diffs[2], diffs[3]
    assert fk_gone.before["to_table"] == "stores" and fk_gone.after is None
    assert fk_new.after["to_table"] == "shops" and fk_new.before is None


def test_constraints_not_read_on_one_side_are_not_compared():
    """反射读不到唯一约束、外键时不写那两个键：不知道 ≠ 删了。"""
    after = _meta()
    del after["unique"], after["foreign_keys"]
    assert sd.diff_table(_meta(), after) == []


def test_baseline_after_upgrade_reports_nothing():
    """升级前的老缓存接进来的结构版本是 0：第一次按对象索引读到只建基线，通用写法换成方言完整写法不算类型变了。"""
    legacy = _meta()
    assert sd.is_baseline(0, legacy) and sd.is_baseline(None, legacy)
    assert sd.is_baseline(3, None) and sd.is_baseline(3, {"columns": []}), "上一次的结构不在手上：没法比"
    assert not sd.is_baseline(1, legacy)


def test_to_dict_keeps_rename_and_key():
    d = sd.Diff("key_changed", None, before=["id"], after=None, key="primary")
    assert d.to_dict() == {"kind": "key_changed", "column": None, "before": ["id"], "after": None, "key": "primary"}
    assert sd.touched_columns([sd.Diff("column_removed", "a"), sd.Diff("column_type_changed", "b"),
                               sd.Diff("column_added", "c")]) == {"removed": {"a"}, "changed": {"b"}}
