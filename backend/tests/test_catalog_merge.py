"""数据目录的合并、单项审阅和人工编辑规则（纯函数）。

- 起草结果并入已有目录：人工确认过、驳回过的项一律不动；同一来源的项可以更新；不同来源按可信程度
  （人工 > 外键 > 数据剖析 > 数据库注释 > 模型起草、命名推断）决定谁留下；这一轮完整算过的来源，
  已经没有的旧项删掉（外键约束被删了，原来那条「有确证」的关系不能留着）。
- 单项审阅：confirm / reject / reset，路径写法 grain、columns.amount.measure、relations.<编号>。
- 人工提交整份目录：改动过的项记为 human / confirmed，没动过的项保持原来的来源和状态。
"""
from __future__ import annotations

import pytest

from app.data import catalog
from app.data.catalog import make_item

AT = "2026-10-03T08:00:00+00:00"


def _rel(columns, to_table, to_columns, source="name", status=None, table="visits", **kw):
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns,
            "to_table": to_table, "to_columns": to_columns, "cardinality": "many_to_one", "coverage": None,
            "source": source, "status": status or catalog.initial_status(source), **kw}


# ---------------------------------------------------------------- 合并


def test_merge_adds_new_items_and_counts():
    draft = {"label": make_item("入园记录", "llm"),
             "columns": {"amount": {"unit": make_item("元", "llm")}},
             "relations": [_rel(["gate_id"], "gates", ["id"])]}
    merged, stats = catalog.merge_notes({}, draft, at=AT)
    assert stats == catalog.MergeStats(added=3, updated=0, removed=0)
    assert merged["label"]["value"] == "入园记录" and merged["label"]["updated_at"] == AT
    assert merged["columns"]["amount"]["unit"]["status"] == "proposed"
    assert merged["relations"][0]["source"] == "name"


def test_merge_never_overwrites_confirmed_or_rejected():
    existing = {"label": make_item("入园明细", "human"),
                "grain": make_item("每张票一行", "llm", "rejected"),
                "relations": [_rel(["member_id"], "members", ["id"], status="rejected")]}
    draft = {"label": make_item("入园记录", "llm"), "grain": make_item("每张票一行", "llm"),
             "relations": [_rel(["member_id"], "members", ["id"])]}
    merged, stats = catalog.merge_notes(existing, draft, covered={"llm", "name"}, at=AT)
    assert merged == existing
    assert stats == catalog.MergeStats(0, 0, 0)


def test_merge_same_source_updates_value():
    existing = {"grain": make_item("每次入园一行", "llm", at="2026-09-01T00:00:00+00:00")}
    merged, stats = catalog.merge_notes(existing, {"grain": make_item("每张门票一次检票一行", "llm")}, at=AT)
    assert merged["grain"]["value"] == "每张门票一次检票一行" and merged["grain"]["updated_at"] == AT
    assert stats.updated == 1


def test_merge_same_value_is_not_an_update():
    existing = {"grain": make_item("每次入园一行", "llm", at="2026-09-01T00:00:00+00:00")}
    merged, stats = catalog.merge_notes(existing, {"grain": make_item("每次入园一行", "llm")}, at=AT)
    assert merged == existing and stats == catalog.MergeStats(0, 0, 0)


def test_merge_higher_rank_source_replaces_lower_but_not_the_reverse():
    # 外键约束（有确证）顶掉同一条关系的命名推断
    existing = {"relations": [_rel(["member_id"], "members", ["id"], source="name")]}
    merged, stats = catalog.merge_notes(existing, {"relations": [_rel(["member_id"], "members", ["id"], source="fk")]},
                                        at=AT)
    assert merged["relations"][0]["source"] == "fk" and merged["relations"][0]["status"] == "verified"
    assert stats.updated == 1
    # 数据库注释比模型起草可信：模型起草不顶掉注释，注释可以顶掉模型起草
    existing = {"label": make_item("入园记录", "comment")}
    merged, _ = catalog.merge_notes(existing, {"label": make_item("游客入园", "llm")}, at=AT)
    assert merged["label"]["source"] == "comment"
    merged, _ = catalog.merge_notes({"label": make_item("游客入园", "llm")}, {"label": make_item("入园记录", "comment")},
                                    at=AT)
    assert merged["label"] == {**make_item("入园记录", "comment"), "updated_at": AT}


def test_merge_removes_stale_items_only_for_covered_sources():
    stale_fk = _rel(["store_id"], "stores", ["id"], source="fk")
    keep_name = _rel(["gate_id"], "gates", ["id"], source="name")
    existing = {"relations": [stale_fk, keep_name], "label": make_item("入园记录", "llm")}
    # 这一轮完整读过外键约束（covered 含 fk）而那条外键已经没了：删掉；命名推断和模型起草这一轮没算，留着
    merged, stats = catalog.merge_notes(existing, {}, covered={"fk"}, at=AT)
    assert merged["relations"] == [keep_name] and merged["label"]["value"] == "入园记录"
    assert stats.removed == 1


def test_merge_keeps_column_entries_tidy():
    existing = {"columns": {"amount": {"unit": make_item("元", "llm")}}}
    merged, stats = catalog.merge_notes(existing, {}, covered={"llm"}, at=AT)
    assert merged == {} and stats.removed == 1


# ---------------------------------------------------------------- 单项审阅


def _notes():
    return {"grain": make_item("每次入园一行", "llm"),
            "columns": {"amount": {"measure": make_item("flow", "llm")}},
            "relations": [_rel(["park_id"], "parks", ["id"], source="fk"), _rel(["gate_id"], "gates", ["id"])]}


def test_review_confirm_reject_reset_on_each_kind_of_path():
    notes = _notes()
    rid = notes["relations"][1]["id"]
    out = catalog.review_item(notes, "grain", "confirm", at=AT)
    assert out["grain"]["status"] == "confirmed" and out["grain"]["source"] == "llm" and out["grain"]["updated_at"] == AT
    out = catalog.review_item(out, "columns.amount.measure", "reject", at=AT)
    assert out["columns"]["amount"]["measure"]["status"] == "rejected"
    out = catalog.review_item(out, f"relations.{rid}", "confirm", at=AT)
    assert out["relations"][1]["status"] == "confirmed"
    # reset 回到来源的初始状态：外键是 verified，其余是 proposed
    fk_id = notes["relations"][0]["id"]
    out = catalog.review_item(catalog.review_item(out, f"relations.{fk_id}", "reject", at=AT),
                              f"relations.{fk_id}", "reset", at=AT)
    assert out["relations"][0]["status"] == "verified"
    out = catalog.review_item(out, "columns.amount.measure", "reset", at=AT)
    assert out["columns"]["amount"]["measure"]["status"] == "proposed"
    assert notes["grain"]["status"] == "proposed"      # 不改入参


def test_review_reset_of_human_item_removes_it():
    notes = {"label": make_item("入园记录", "human")}
    assert catalog.review_item(notes, "label", "reset", at=AT) == {}


def test_review_column_names_may_contain_dots():
    notes = {"columns": {"a.b": {"unit": make_item("元", "llm")}}}
    out = catalog.review_item(notes, "columns.a.b.unit", "confirm", at=AT)
    assert out["columns"]["a.b"]["unit"]["status"] == "confirmed"


@pytest.mark.parametrize("path", ["", "nope", "grain.extra", "columns.amount", "columns.amount.formula",
                                  "columns.missing.measure", "relations.r000000000000", "label"])
def test_review_bad_paths(path):
    with pytest.raises(catalog.CatalogPathError):
        catalog.review_item(_notes(), path, "confirm", at=AT)


def test_review_bad_action():
    with pytest.raises(catalog.CatalogPathError):
        catalog.review_item(_notes(), "grain", "approve", at=AT)


# ---------------------------------------------------------------- 人工编辑


def test_human_edit_marks_changed_items_as_human_confirmed_and_keeps_the_rest():
    existing = _notes()
    submitted = {
        "grain": {**existing["grain"], "value": "每张门票每次检票一行"},       # 改了值
        "columns": {"amount": {"measure": existing["columns"]["amount"]["measure"],   # 没动
                               "unit": {"value": "元"}}},                             # 新填，没写来源和状态
        "relations": existing["relations"][:1],                                      # 去掉一条推断出来的关系
        "dedup": {"value": "同一票号只计一次", "source": "llm", "status": "verified"},   # 冒充别的来源、状态
    }
    out = catalog.apply_human_edit(existing, submitted, table="visits", at=AT)
    assert out["grain"] == {"value": "每张门票每次检票一行", "source": "human", "status": "confirmed", "updated_at": AT}
    assert out["columns"]["amount"]["measure"] == existing["columns"]["amount"]["measure"]
    assert out["columns"]["amount"]["unit"] == {"value": "元", "source": "human", "status": "confirmed",
                                                "updated_at": AT}
    assert out["dedup"]["source"] == "human" and out["dedup"]["status"] == "confirmed"
    # 去掉的推断关系转为驳回、保留编号（删掉的话下次起草、关系图现推都会把它带回来）
    assert out["relations"] == [existing["relations"][0],
                                {**existing["relations"][1], "status": "rejected", "updated_at": AT}]
    assert catalog.validate_notes(out) == []


def test_human_edit_status_only_change_keeps_source():
    existing = _notes()
    submitted = {**existing, "grain": {**existing["grain"], "status": "confirmed"}}
    out = catalog.apply_human_edit(existing, submitted, table="visits", at=AT)
    assert out["grain"]["source"] == "llm" and out["grain"]["status"] == "confirmed"
    # 不能把推断改成「有确证」：verified 只来自外键约束和数据剖析
    submitted = {**existing, "grain": {**existing["grain"], "status": "verified"}}
    assert catalog.apply_human_edit(existing, submitted, table="visits", at=AT)["grain"]["status"] == "proposed"


def test_human_edit_relation_id_follows_its_ends():
    existing = _notes()
    moved = {**existing["relations"][1], "to_table": "parks", "to_columns": ["id"], "columns": ["gate_id"]}
    out = catalog.apply_human_edit(existing, {"relations": [moved]}, table="visits", at=AT)
    assert out["relations"][0]["id"] == catalog.relation_id("visits", ["gate_id"], "parks", ["id"])
    assert out["relations"][0]["source"] == "human"
    # 两条原有的推断关系都不在提交里了（一条改了两端）：转为驳回
    assert [(r["id"], r["status"]) for r in out["relations"][1:]] == [(r["id"], "rejected")
                                                                       for r in existing["relations"]]
