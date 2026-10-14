"""保存修改提案时的依据：目录项上单独的一个字段 reason（界面上叫「确认时的依据」），和备注 note 分开。

表助手、助手流里「建议更新数据目录」的提案经 /catalog/{table}/patch 保存，带了依据的项把依据记进这一项的 reason（最多
500 字，超了截成 499 字加「…」）；note 照旧是起草的依据、剖析的测量说明、双盲复算的出处这类机器写的话，不拼依据。

- 去留：值改了（关联关系改了值也一样）换成这次的依据，没有依据就去掉旧的；值不变、保存即确认的换成这次的（不叠加）；
  单项审阅的确认、驳回留着，撤销审阅（回到推断）去掉；剖析、起草、导入以外的来源改写这一项（值变了）时去掉；剖析刷新
  关联关系的测量只动 note。
- 校验认它（文字、最多 500 字）；起草、剖析的合并不碰人工确认的项，依据留着；导入时已确认、已驳回的项连依据一起带，
  推断、有确证的项不带。

以前把依据拼进 note（「依据：……。数据剖析：……」）：再剖析时测量的说法换不了新，和覆盖率那一栏对不上；改了值的关联关系
旧依据叠在新依据后面；撤销审阅回到推断，「依据：使用者说：……」还挂在推断项上，看起来像使用者确认过。

表名、列名、数据全部虚构。
"""
from __future__ import annotations

from app.data import catalog, catalog_profile, catalog_transfer
from app.data.catalog import make_item

AT = "2026-10-14T08:00:00+00:00"
LATER = "2026-10-15T08:00:00+00:00"

#: 提案核对用的表结构（只要列名和主键）
TABLES = {
    "visits": {"qualified": "visits", "primary_key": ["id"],
               "columns": [{"name": c} for c in ("id", "ticket_no", "park_id", "visitor_count", "status")]},
    "parks": {"qualified": "parks", "primary_key": ["id"], "columns": [{"name": "id"}, {"name": "name"}]},
}
PARKS = catalog.relation_id("visits", ["park_id"], "parks", ["id"])
PARK_ENDS = {"columns": ["park_id"], "to_table": "parks", "to_columns": ["id"]}
PROFILE_NOTE = "数据剖析：抽样 1,000 行，覆盖率 97%。"
FILTER_NOTE = "依据：码值 0 是作废。剖析核对：排除 3.2% 的行（共 1,000 行）"


def _rel(**extra) -> dict:
    """数据剖析核实过的入园记录 → 景区：覆盖率 97%，备注是测量的说法。"""
    return {"id": PARKS, **PARK_ENDS, "cardinality": "many_to_one", "coverage": 0.97, "source": "profile",
            "status": "verified", "note": PROFILE_NOTE, **extra}


def _visits() -> dict:
    return {
        "grain": make_item("一张门票一行", "llm"),
        "valid_filter": make_item("status = 1", "llm", note=FILTER_NOTE),
        "columns": {"visitor_count": {"measure": make_item("flow", "llm")}},
        "relations": [_rel()],
    }


def _patch(notes: dict, changes: list[dict], at: str = AT) -> dict:
    return catalog.apply_patch(notes, changes, table="visits", tables=TABLES, at=at)


# --------------------------------------------------------------------------
# 保存提案
# --------------------------------------------------------------------------


def test_saving_keeps_the_reason_apart_from_the_note():
    out = _patch(_visits(), [
        {"path": "columns.visitor_count.measure", "value": "stock", "reason": "使用者说：在园人数是存量"},
        {"path": "columns.visitor_count.unit", "value": "人"},                              # 没写依据
        {"path": "valid_filter", "value": "status = 1", "reason": "使用者说：只算有效的"},     # 值不变：保存即确认
        {"path": f"relations.{PARKS}", "value": {**PARK_ENDS, "cardinality": "one_to_one"},
         "reason": "使用者说：一个景区一条"},
        {"path": "grain", "value": "一张门票的一次入园", "reason": "长" * 600},
    ])
    measure = out["columns"]["visitor_count"]["measure"]
    assert measure["reason"] == "使用者说：在园人数是存量" and "note" not in measure
    assert (measure["value"], measure["source"], measure["status"]) == ("stock", "human", "confirmed")
    unit = out["columns"]["visitor_count"]["unit"]
    assert "reason" not in unit and "note" not in unit
    # 值不变的确认：来源、备注都不变，依据另记
    valid = out["valid_filter"]
    assert valid["note"] == FILTER_NOTE and valid["reason"] == "使用者说：只算有效的"
    assert (valid["source"], valid["status"]) == ("llm", "confirmed")
    # 关联关系改了基数：覆盖率和说明它怎么测的备注照旧沿用，备注里不拼依据
    rel = out["relations"][0]
    assert rel["note"] == PROFILE_NOTE and rel["reason"] == "使用者说：一个景区一条"
    assert (rel["cardinality"], rel["coverage"], rel["source"], rel["status"]) == (
        "one_to_one", 0.97, "human", "confirmed")
    # 超过 500 字：截成 499 字加「…」
    grain = out["grain"]
    assert grain["reason"] == "长" * 499 + "…" and "note" not in grain
    assert catalog.validate_notes(out) == []
    # 正好 500 字的不截；预览给出的依据和保存的是同一句
    exact = _patch(_visits(), [{"path": "grain", "value": "一行一张票", "reason": "短" * 500}])
    assert exact["grain"]["reason"] == "短" * 500
    plan = catalog.plan_patch(_visits(), [{"path": "grain", "value": "一行一张票", "reason": "长" * 600}],
                              table="visits", tables=TABLES)
    assert plan.changes[0].reason == "长" * 499 + "…"


def test_profiling_again_refreshes_the_measurement_and_keeps_the_reason():
    """带依据保存的关联关系再剖析：覆盖率和备注里测量的说法一起换新（以前备注成了「依据：……。数据剖析：……」，剖析
    认不出是自己写的，覆盖率变了、备注却停在保存那一刻），依据不动，人定的基数不改。"""
    saved = _patch({"relations": [_rel()]}, [
        {"path": f"relations.{PARKS}", "value": {**PARK_ENDS, "cardinality": "one_to_one"},
         "reason": "使用者说：一个景区一条"},
    ])
    outcome = catalog_profile._TableOutcome(relations=[catalog_profile._RelationOutcome(
        rid=PARKS, target="parks.id", coverage=0.99, cardinality="many_to_one", holds=True, sample=1000, matched=990,
        note="数据剖析：抽样 1,000 行，覆盖率 99%。")])
    merged, findings, stats = catalog_profile._apply(saved, outcome, at=LATER)
    rel = merged["relations"][0]
    assert rel["coverage"] == 0.99 and rel["note"] == "数据剖析：抽样 1,000 行，覆盖率 99%。"
    assert rel["reason"] == "使用者说：一个景区一条" and rel["cardinality"] == "one_to_one"
    assert (rel["source"], rel["status"], rel["updated_at"]) == ("human", "confirmed", LATER)
    assert stats.updated == 1 and findings[0]["confirmed"] is True


def test_saving_again_replaces_the_reason_instead_of_piling_it_up():
    first = _patch(_visits(), [
        {"path": f"relations.{PARKS}", "value": {**PARK_ENDS, "cardinality": "one_to_one"},
         "reason": "使用者说：一个景区一条"},
        {"path": "columns.visitor_count.measure", "value": "stock", "reason": "使用者说：在园人数是存量"},
        {"path": "valid_filter", "value": "status = 1", "reason": "使用者说：只算有效的"},
    ])
    again = _patch(first, [
        # 关联关系又改了值：换成这次的依据，旧的不叠在后面；备注照旧只是测量的说法
        {"path": f"relations.{PARKS}", "value": {**PARK_ENDS, "cardinality": "many_to_one"},
         "reason": "使用者说：一个景区有多条入园记录"},
        # 改了值、这次没写依据：旧的依据说的是旧值，去掉
        {"path": "columns.visitor_count.measure", "value": "ratio"},
    ])
    rel = again["relations"][0]
    assert rel["reason"] == "使用者说：一个景区有多条入园记录" and rel["note"] == PROFILE_NOTE
    assert "reason" not in again["columns"]["visitor_count"]["measure"]
    # 撤销审阅回到推断：依据去掉；再一次保存即确认，只有这一次的依据，备注还是起草时的那句
    reset = catalog.review_item(again, "valid_filter", "reset", at=AT)
    assert reset["valid_filter"]["status"] == "proposed" and "reason" not in reset["valid_filter"]
    confirmed = _patch(reset, [{"path": "valid_filter", "value": "status = 1", "reason": "使用者说：确认无误"}])
    assert confirmed["valid_filter"]["reason"] == "使用者说：确认无误"
    assert confirmed["valid_filter"]["note"] == FILTER_NOTE
    # 推断项上不知怎么留着一句依据（手改过的数据）：保存即确认时换成这一次的，不叠
    stray = {"grain": {**make_item("一张门票一行", "llm"), "reason": "旧的依据"}}
    assert _patch(stray, [{"path": "grain", "value": "一张门票一行", "reason": "新的依据"}])["grain"]["reason"] == "新的依据"
    assert "reason" not in _patch(stray, [{"path": "grain", "value": "一张门票一行"}])["grain"]


# --------------------------------------------------------------------------
# 审阅、人工编辑
# --------------------------------------------------------------------------


def test_review_keeps_the_reason_until_the_review_is_undone():
    notes = {"grain": make_item("一张门票一行", "llm", "confirmed", reason="使用者说：一张票一行"),
             "label": make_item("入园记录", "human", "confirmed", reason="使用者说：就叫入园记录")}
    rejected = catalog.review_item(notes, "grain", "reject", at=AT)
    assert rejected["grain"]["status"] == "rejected" and rejected["grain"]["reason"] == "使用者说：一张票一行"
    again = catalog.review_item(rejected, "grain", "confirm", at=AT)
    assert again["grain"]["status"] == "confirmed" and again["grain"]["reason"] == "使用者说：一张票一行"
    reset = catalog.review_item(again, "grain", "reset", at=AT)
    assert reset["grain"]["status"] == "proposed" and "reason" not in reset["grain"]
    # 人工填写的项撤销就是删掉，依据一起没了
    assert "label" not in catalog.review_item(notes, "label", "reset", at=AT)


def test_editing_by_hand_drops_the_reason_only_when_the_value_changes():
    """整份提交（表详情里的编辑）不写依据：改了值的项去掉旧的依据（界面原样带回来的也不认），值没动的照旧留着库里的，
    状态改回推断等同撤销审阅、去掉；去掉的推断关系转为驳回，依据留着（同单项驳回）。"""
    rel = {"id": PARKS, **PARK_ENDS, "cardinality": "many_to_one", "coverage": None, "source": "name",
           "status": "confirmed", "reason": "使用者说：一个景区一条"}
    existing = {
        "label": make_item("入园记录", "human", "confirmed", reason="使用者说：就叫入园记录"),
        "grain": make_item("一张门票一行", "llm", "confirmed", reason="使用者说：一张票一行"),
        "kind": make_item("fact", "llm", "confirmed", reason="使用者说：是明细"),
        "relations": [rel],
    }
    submitted = {
        "label": {**existing["label"], "value": "入园流水"},
        "grain": {**existing["grain"], "reason": "界面上写不了依据"},
        "kind": {**existing["kind"], "status": "proposed"},
        "columns": {"status": {"codes": {"value": {"1": "有效"}, "reason": "新填的也不收"}}},
    }
    out = catalog.apply_human_edit(existing, submitted, table="visits", at=AT)
    assert out["label"]["value"] == "入园流水" and "reason" not in out["label"]
    assert out["grain"]["reason"] == "使用者说：一张票一行"
    assert out["kind"]["status"] == "proposed" and "reason" not in out["kind"]
    codes = out["columns"]["status"]["codes"]
    assert (codes["source"], codes["status"]) == ("human", "confirmed") and "reason" not in codes
    [kept] = out["relations"]
    assert kept["status"] == "rejected" and kept["reason"] == "使用者说：一个景区一条"
    assert catalog.validate_notes(out) == []


# --------------------------------------------------------------------------
# 校验、合并、导入
# --------------------------------------------------------------------------


def test_the_reason_is_text_of_at_most_500_characters():
    ok = {"label": make_item("入园记录", "human", "confirmed", reason="短" * 500),
          "columns": {"status": {"codes": make_item({"1": "有效"}, "human", "confirmed", complete=True,
                                                    reason="使用者说：1 是有效")}},
          "relations": [_rel(status="confirmed", reason="使用者说：一个景区一条")]}
    assert catalog.validate_notes(ok) == []
    bad = catalog.validate_notes({"label": {**make_item("入园记录", "human", "confirmed"), "reason": "长" * 501},
                                  "grain": {**make_item("一行", "human", "confirmed"), "reason": 3},
                                  "relations": [_rel(reason=["使用者说"])]})
    assert len(bad) == 3 and all("确认时的依据" in p for p in bad), bad


def test_drafting_and_profiling_leave_the_reason_of_reviewed_items_alone():
    existing = {"label": make_item("入园记录", "llm", "confirmed", reason="使用者说：就叫入园记录"),
                "grain": make_item("一张门票一行", "llm", "rejected", reason="使用者说：一张票一行"),
                "columns": {"status": {"codes": make_item({"1": "有效"}, "profile", "confirmed",
                                                          reason="使用者说：1 是有效")}}}
    draft = {"label": make_item("入园流水", "llm"), "grain": make_item("每次刷闸一行", "llm"),
             "kind": make_item("fact", "llm")}
    merged, _ = catalog.merge_notes(existing, draft, covered={"llm"}, at=AT, columns=["id", "status"])
    assert merged["label"] == existing["label"] and merged["grain"] == existing["grain"]
    assert "reason" not in merged["kind"]
    outcome = catalog_profile._TableOutcome(codes=[catalog_profile._CodesOutcome(
        column="status", values=[("1", 900), ("0", 100)], rows=1000, note="数据剖析：全表 1,000 行，2 个取值。",
        complete=True)])
    profiled, _, _ = catalog_profile._apply(merged, outcome, at=LATER)
    assert profiled["columns"]["status"]["codes"] == existing["columns"]["status"]["codes"]


def test_importing_carries_the_reason_of_confirmed_and_rejected_items_only():
    cache = {"tables": {"visits": TABLES["visits"]}}
    file_notes = {
        "label": make_item("入园记录", "human", "confirmed", reason="使用者说：就叫入园记录"),
        "grain": make_item("一张门票一行", "llm", "rejected", reason="使用者说：一张票一行"),
        # 推断项上带着依据（文件被手改过）：只填空缺，不带确认时的依据
        "description": {**make_item("每次刷闸记一行", "llm"), "reason": "手改的文件"},
    }
    plan = catalog_transfer.plan_table("visits", "visits", file_notes, {}, cache, set(), "sqlite")
    assert plan.skipped == [] and plan.add == 3
    writes = {key[1]: item for key, item in plan.writes.items()}
    assert writes["label"]["reason"] == "使用者说：就叫入园记录"
    assert writes["grain"]["reason"] == "使用者说：一张票一行"
    assert writes["description"]["value"] == "每次刷闸记一行" and "reason" not in writes["description"]


def test_profiling_keeps_the_cardinality_of_a_confirmed_foreign_key():
    """外键来的关联关系经「值不变、保存即确认」带依据确认之后，来源仍是 fk。再剖析时数据核实出的基数不同：以前 _patch_fk
    不看状态，直接改写人确认过的基数，依据还挂着，前后矛盾。现在同 _patch_confirmed，不改人定的基数、也不标「用数据核实过」，
    依据留着；测量的说法照常写进备注。没确认过的外键关系照旧按数据改基数、标核实过。"""
    fk = {k: v for k, v in _rel(source="fk", status="verified").items() if k not in ("coverage", "note")}
    saved = _patch({"relations": [fk]}, [
        {"path": f"relations.{PARKS}", "value": {**PARK_ENDS, "cardinality": "many_to_one"},
         "reason": "使用者说：一个景区有多条入园记录"},
    ])
    rel = saved["relations"][0]
    assert (rel["source"], rel["status"], rel["reason"]) == ("fk", "confirmed", "使用者说：一个景区有多条入园记录")
    outcome = catalog_profile._TableOutcome(relations=[catalog_profile._RelationOutcome(
        rid=PARKS, target="parks.id", coverage=None, cardinality="one_to_one", cardinality_checked=True, holds=True,
        sample=1000, matched=1000, note="数据剖析：子表一侧每个键都只有一行。")])
    rel = catalog_profile._apply(saved, outcome, at=LATER)[0]["relations"][0]
    assert rel["cardinality"] == "many_to_one" and rel.get("cardinality_checked") is not True
    assert (rel["source"], rel["status"], rel["reason"]) == ("fk", "confirmed", "使用者说：一个景区有多条入园记录")
    assert rel["note"] == "数据剖析：子表一侧每个键都只有一行。"

    plain = catalog_profile._apply({"relations": [fk]}, outcome, at=LATER)[0]["relations"][0]
    assert plain["cardinality"] == "one_to_one" and plain["cardinality_checked"] is True
    assert (plain["source"], plain["status"]) == ("fk", "verified")
