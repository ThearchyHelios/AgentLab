"""数据目录核心语义的回归（2026-10 完整性审查 F1）。

- A1：经对话提案确认一条关系，不抹掉数据剖析测得的基数和覆盖率；新增关系没写基数时按目标表的键推算，
  推算不了就留空并在提案的这一项上说明。否则扇出检查（fanout_sum）看不到基数，静默失效。
- A2：在目录页去掉推断或外键来的关系即驳回，保留编号；只有人工新建的关系才真正删除。检查器、关系图
  （助手挑表也用它）、重新起草三处都不能再出现被驳回的关系。
- A4：码值只补了一部分时不当作完整清单。「码值不在码值表中」只对标了「已列全」的码值报；「已列全」只有人工
  明确确认、或者剖析没截断地看完全表取值时才有。
- B2：隔天重新剖析、数据没变，目录不升版本（剖析写的备注里只有日期变了不算变化）。
- B3：计算公式在人工整份提交、模型起草两条路径上也拦下（以前只拦提案）。
- 一致性：日期列的判定、表名的匹配、拆词各只有一份。

数据和名称全部虚构（tests/fixtures/catalog/scenic.py）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.data import catalog, catalog_profile, sqlcheck
from app.db.base import SessionLocal
from app.db.models import DataSource
from tests.fixtures.catalog import scenic, scenic_notes
from tests.fixtures.catalog.fake_model import FakeModel
from tests.test_catalog_patch import _schema
from tests.test_catalog_profile import _notes, _on, _relation, _table, _url, client, make_source  # noqa: F401

AT = "2026-10-04T08:00:00+00:00"
DAY1, DAY2, DAY3 = "2026-10-01T08:00:00+00:00", "2026-10-02T08:00:00+00:00", "2026-10-03T08:00:00+00:00"


async def _checker(sid: str) -> sqlcheck.SqlChecker | None:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        return (await sqlcheck.load_checkers(session, [row])).get(row.name)


async def _found(sid: str, sql: str) -> list[tuple]:
    checker = await _checker(sid)
    return [(c.code, c.level, c.table, c.column) for c in (checker.check(sql) if checker else [])]


async def _graph(sid: str) -> dict[str, list[catalog.JoinEdge]]:
    """助手挑表用的关系图：库里的目录原样（含驳回项）加表结构现推。"""
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        entries = await catalog.read_catalog(session, sid)
    return catalog.relation_graph({t: e.notes for t, e in entries.items()}, schema_cache=row.schema_cache)


async def _put(client, sid: str, table: str, notes: dict, version: int):
    return await client.put(f"/api/datasources/{sid}/catalog/{table}", json={"notes": notes, "if_version": version})


def _profile_rel(table: str, columns: list[str], to_table: str, to_columns: list[str], *,
                 cardinality: str | None = "many_to_one", coverage: float | None = 1.0,
                 status: str = "verified") -> dict:
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns,
            "to_table": to_table, "to_columns": to_columns, "cardinality": cardinality, "coverage": coverage,
            "source": "profile", "status": status,
            "note": "数据剖析（2026-10-01）：子表抽样 8 个不同键值，父表对上 8 个，覆盖率 100%；被指向列是主键。"}


# ==========================================================================
# A1：提案不抹掉基数和覆盖率
# ==========================================================================


async def test_patch_confirming_a_profiled_relation_keeps_cardinality_and_coverage(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    gate = _profile_rel("visits", ["gate_id"], "gates", ["id"])
    notes = {"relations": [gate]}
    value = {"columns": ["gate_id"], "to_table": "gates", "to_columns": ["id"]}
    for path in ("relations.new", f"relations.{gate['id']}"):
        [change] = catalog.plan_patch(notes, [{"path": path, "value": value}], table="visits", tables=tables).changes
        assert change.after["cardinality"] == "many_to_one" and change.after["coverage"] == 1.0
        # 两端、基数、覆盖率都和目录里的一样：保存即确认，来源不变
        assert change.state == "confirm"
    out = catalog.apply_patch(notes, [{"path": "relations.new", "value": value}], table="visits", tables=tables,
                              at=AT)
    [rel] = out["relations"]
    assert (rel["source"], rel["status"], rel["cardinality"], rel["coverage"]) == (
        "profile", "confirmed", "many_to_one", 1.0)
    assert rel["note"] == gate["note"]


async def test_patch_may_change_cardinality_but_never_coverage(scenic_db):
    tables = (await _schema(scenic_db))["tables"]
    gate = _profile_rel("visits", ["gate_id"], "gates", ["id"], coverage=0.97)
    value = {"columns": ["gate_id"], "to_table": "gates", "to_columns": ["id"], "cardinality": "one_to_one",
             "coverage": 0.2}                               # 覆盖率是测量值：模型写了也不认
    [change] = catalog.plan_patch({"relations": [gate]}, [{"path": "relations.new", "value": value}],
                                  table="visits", tables=tables).changes
    assert change.state == "change"
    assert (change.after["cardinality"], change.after["coverage"]) == ("one_to_one", 0.97)
    out = catalog.apply_patch({"relations": [gate]}, [{"path": "relations.new", "value": value}], table="visits",
                              tables=tables, at=AT)
    [rel] = out["relations"]
    assert (rel["source"], rel["status"], rel["cardinality"], rel["coverage"]) == (
        "human", "confirmed", "one_to_one", 0.97)
    assert rel["note"] == gate["note"]                      # 覆盖率留着，说明它的剖析备注也留着


async def test_new_relation_without_cardinality_is_inferred_from_target_keys(scenic_db):
    tables = (await _schema(scenic_db))["tables"]

    def plan(table: str, value: dict) -> catalog.PatchChange:
        [change] = catalog.plan_patch({}, [{"path": "relations.new", "value": value}], table=table,
                                      tables=tables).changes
        return change

    # 目标是主键、本表这一列不唯一：多对一
    gate = plan("visits", {"columns": ["gate_id"], "to_table": "gates", "to_columns": ["id"]})
    assert (gate.after["cardinality"], gate.after["coverage"], gate.note) == ("many_to_one", None, None)
    assert "note" not in gate.as_dict()
    # 目标是唯一约束、本表这一列也唯一（票号有唯一索引）：一对一
    ticket = plan("visits", {"columns": ["ticket_no"], "to_table": "orders", "to_columns": ["order_no"]})
    assert ticket.after["cardinality"] == "one_to_one"
    # 目标列不是主键也不是唯一约束：推算不了，基数留空，这一项上说明
    park = plan("visits", {"columns": ["park_id"], "to_table": "gates", "to_columns": ["park_id"]})
    assert park.after["cardinality"] is None
    assert park.note and "基数" in park.note and park.as_dict()["note"] == park.note
    # 不知道表结构（尚未探查）：不推算，也说明
    [blind] = catalog.plan_patch({}, [{"path": "relations.new", "value": {
        "columns": ["gate_id"], "to_table": "gates", "to_columns": ["id"]}}], table="visits", tables=None).changes
    assert blind.after["cardinality"] is None and blind.note


async def test_confirming_a_profiled_relation_by_patch_keeps_the_fanout_check(client, make_source, scenic_db):
    """审查探针 A1：剖析得出多对一、覆盖率 1.0，经对话提案确认这条关系之后，扇出检查照样报 error。"""
    sql = "SELECT COUNT(g.id) AS n FROM gates g JOIN visits v ON v.gate_id = g.id"
    sid = await make_source(scenic_db, options=_on(), draft=["visits", "gates"])
    r = await client.post(_url(sid), json={"tables": ["visits"]})
    assert r.status_code == 200, r.text
    detail = await _notes(client, sid, "visits")
    rel = _relation(detail["notes"], "gate_id")
    assert (rel["source"], rel["status"], rel["cardinality"], rel["coverage"]) == (
        "profile", "verified", "many_to_one", 1.0)
    assert ("fanout_sum", "error", "gates", "id") in await _found(sid, sql)

    changes = [{"path": "relations.new", "value": {"columns": ["gate_id"], "to_table": "gates", "to_columns": ["id"]},
                "reason": "用户说 gate_id 对应 gates.id"}]
    r = await client.post(f"/api/datasources/{sid}/catalog/visits/patch",
                          json={"changes": changes, "if_version": detail["version"]})
    assert r.status_code == 200, r.text
    after = _relation(r.json()["notes"], "gate_id")
    assert (after["status"], after["cardinality"], after["coverage"]) == ("confirmed", "many_to_one", 1.0)
    assert ("fanout_sum", "error", "gates", "id") in await _found(sid, sql)


# ==========================================================================
# A2：去掉推断或外键来的关系即驳回
# ==========================================================================


def _rel(columns, to_table, to_columns, source="name", status=None, table="visits"):
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns,
            "to_table": to_table, "to_columns": to_columns, "cardinality": "many_to_one", "coverage": None,
            "source": source, "status": status or catalog.initial_status(source)}


def test_human_edit_rejects_removed_inferred_relations_and_deletes_human_ones():
    park, gate = _rel(["park_id"], "parks", ["id"], "fk"), _rel(["gate_id"], "gates", ["id"], "name")
    mine = _rel(["member_id"], "members", ["id"], "human")
    out = catalog.apply_human_edit({"relations": [park, gate, mine]}, {"relations": []}, table="visits", at=AT)
    by_id = {r["id"]: r for r in out["relations"]}
    assert set(by_id) == {park["id"], gate["id"]}                    # 人工新建的那条真正删掉
    for old in (park, gate):
        assert by_id[old["id"]] == {**old, "status": "rejected", "updated_at": AT}   # 编号、来源、两端都留着
    # 已经驳回的再去掉一次：不变
    again = catalog.apply_human_edit(out, {"relations": []}, table="visits", at="2026-10-05T00:00:00+00:00")
    assert again == out


def test_human_edit_moving_an_inferred_relation_rejects_the_old_one():
    gate = _rel(["gate_id"], "gates", ["id"], "name")
    moved = {**gate, "to_table": "parks", "to_columns": ["id"]}
    out = catalog.apply_human_edit({"relations": [gate]}, {"relations": [moved]}, table="visits", at=AT)
    new_id = catalog.relation_id("visits", ["gate_id"], "parks", ["id"])
    by_id = {r["id"]: r for r in out["relations"]}
    assert (by_id[new_id]["source"], by_id[new_id]["status"]) == ("human", "confirmed")
    assert by_id[gate["id"]]["status"] == "rejected" and by_id[gate["id"]]["source"] == "name"


def test_human_edit_rejects_a_removed_human_relation_that_structure_would_bring_back():
    """人工改过基数的外键关系记成了人工填写；去掉它时若表结构还推得出来，删掉就会被现推回来，只能驳回。"""
    park = {**_rel(["park_id"], "parks", ["id"], "fk"), "cardinality": "one_to_one", "source": "human",
            "status": "confirmed"}
    out = catalog.apply_human_edit({"relations": [park]}, {"relations": []}, table="visits", at=AT,
                                   inferable={park["id"]})
    assert [(r["id"], r["status"]) for r in out["relations"]] == [(park["id"], "rejected")]
    assert catalog.apply_human_edit({"relations": [park]}, {"relations": []}, table="visits", at=AT) == {}


async def test_removed_or_rejected_relations_stay_out_of_checks_graph_and_redraft(client, make_source, scenic_db):
    sid = await make_source(scenic_db, draft=["visits"])
    detail = await _notes(client, sid, "visits")
    park, gate = _relation(detail["notes"], "park_id"), _relation(detail["notes"], "gate_id")
    ticket = _relation(detail["notes"], "ticket_type_id")
    assert (park["source"], gate["source"], ticket["source"]) == ("fk", "name", "fk")
    gone = {park["id"], gate["id"]}

    # 目录页编辑时去掉两条（外键来的、命名推断的）：转为驳回
    notes = {**detail["notes"], "relations": [r for r in detail["notes"]["relations"] if r["id"] not in gone]}
    r = await _put(client, sid, "visits", notes, detail["version"])
    assert r.status_code == 200, r.text
    saved = {x["id"]: x for x in r.json()["notes"]["relations"]}
    assert {saved[i]["status"] for i in gone} == {"rejected"}
    # 单项审阅里驳回第三条（界面上非人工来源的关系只给「驳回」）
    r = await client.post(f"/api/datasources/{sid}/catalog/visits/review",
                          json={"path": f"relations.{ticket['id']}", "action": "reject",
                                "if_version": r.json()["version"]})
    assert r.status_code == 200, r.text
    gone.add(ticket["id"])

    def absent(edges) -> bool:
        return not any(e.relation_id in gone for e in edges)

    # 检查器：被驳回的关系不当作「对上了」，也不拿来判断基数
    checker = await _checker(sid)
    for target in ("parks", "gates", "ticket_types"):
        assert checker.edges_between("visits", target) == []
    joined = await _found(sid, "SELECT COUNT(*) FROM visits v JOIN gates g ON v.gate_id = g.id")
    assert ("join_unconfirmed", "warning", "gates", None) in joined
    # 关系图（助手挑表也用它）：不按表结构现推回来
    graph = await _graph(sid)
    assert absent(graph.get("visits", [])) and absent(graph.get("parks", [])) and absent(graph.get("gates", []))
    # 重新起草：仍是驳回
    r = await client.post(f"/api/datasources/{sid}/catalog/draft", json={"tables": ["visits"]})
    assert r.status_code == 200, r.text
    again = {x["id"]: x for x in (await _notes(client, sid, "visits"))["notes"]["relations"]}
    assert {again[i]["status"] for i in gone} == {"rejected"}
    assert absent((await _graph(sid)).get("visits", []))


def test_relation_graph_keeps_structure_relations_of_partially_annotated_tables():
    """只做过部分注释的表（目录里没有关系）照样按结构补上外键和命名推断；这张表目录里驳回的不补。"""
    cache = {"tables": {
        "visits": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "park_id", "type": "INTEGER"},
                               {"name": "gate_id", "type": "INTEGER"}],
                   "primary_key": ["id"], "foreign_keys": [{"columns": ["park_id"], "to_table": "parks",
                                                            "to_columns": ["id"]}]},
        "parks": {"columns": [{"name": "id", "type": "INTEGER"}], "primary_key": ["id"], "foreign_keys": []},
        "gates": {"columns": [{"name": "id", "type": "INTEGER"}], "primary_key": ["id"], "foreign_keys": []},
    }}
    labelled = {"visits": {"label": catalog.make_item("入园记录", "human")}}
    edges = {(e.to_table, e.source) for e in catalog.relation_graph(labelled, schema_cache=cache)["visits"]}
    assert edges == {("parks", "fk"), ("gates", "name")}
    rejected = {"visits": {"relations": [_rel(["park_id"], "parks", ["id"], "fk", "rejected")]}}
    edges = {e.to_table for e in catalog.relation_graph(rejected, schema_cache=cache)["visits"]}
    assert edges == {"gates"}


# ==========================================================================
# A4：码值「已列全」
# ==========================================================================


def _codes(value: dict, *, complete: bool | None, status: str = "confirmed") -> dict:
    item = catalog.make_item(value, "human" if status == "confirmed" else "profile", status)
    if complete is not None:
        item["complete"] = complete
    return item


async def test_unknown_code_only_for_codes_marked_complete(scenic_db):
    cache = await _schema(scenic_db)
    sql = "SELECT COUNT(*) FROM visits WHERE status = 3"

    def found(item: dict) -> list[str]:
        notes = scenic_notes.notes()
        notes["visits"]["columns"]["status"]["codes"] = item
        checker = sqlcheck.SqlChecker(kind="sqlite", schema_cache=cache, notes=notes, source_name="scenic")
        return [c.code for c in checker.check(sql)]

    assert "unknown_code" not in found(_codes({"1": "有效", "0": "作废"}, complete=None))
    assert "unknown_code" in found(_codes({"1": "有效", "0": "作废"}, complete=True))


async def test_partial_codes_from_a_patch_are_not_treated_as_the_full_list(client, make_source, scenic_db):
    """审查探针 A4：经提案只写了 status=9 表示作废，之后 status = 1 不该被报「码值不在码值表中」。"""
    sid = await make_source(scenic_db, draft=["orders"])
    detail = await _notes(client, sid, "orders")
    r = await client.post(f"/api/datasources/{sid}/catalog/orders/patch", json={
        "changes": [{"path": "columns.status.codes", "value": {"9": "作废"}}], "if_version": detail["version"]})
    assert r.status_code == 200, r.text
    codes = r.json()["notes"]["columns"]["status"]["codes"]
    assert codes["value"] == {"9": "作废"} and "complete" not in codes
    sql = "SELECT COUNT(*) FROM orders WHERE status = 1"
    assert not any(c[0] == "unknown_code" for c in await _found(sid, sql))

    # 人工在目录页把码值补全，并勾上「已列出全部取值」：这时才报
    notes = r.json()["notes"]
    notes["columns"]["status"]["codes"] = {**codes, "value": {"1": "已支付", "2": "已退款", "9": "作废"},
                                           "complete": True}
    r = await _put(client, sid, "orders", notes, r.json()["version"])
    assert r.status_code == 200, r.text
    saved = r.json()["notes"]["columns"]["status"]["codes"]
    assert (saved["source"], saved["status"], saved.get("complete")) == ("human", "confirmed", True)
    assert not any(c[0] == "unknown_code" for c in await _found(sid, sql))
    checker = await _checker(sid)
    [check] = [c for c in checker.check("SELECT COUNT(*) FROM orders WHERE status = 5") if c.code == "unknown_code"]
    assert check.level == "warning"
    # 给模型的话：提示核对取值，不叫它「照目录里的码值改」
    assert "照目录里的码值改" not in check.for_model and "核对" in check.for_model


def test_codes_complete_flag_is_validated_and_counts_as_an_edit():
    base = {"columns": {"status": {"codes": _codes({"1": "", "0": ""}, complete=None, status="proposed")}}}
    assert catalog.validate_notes(base) == []
    assert catalog.validate_notes({"columns": {"status": {"codes": _codes({"1": "有效"}, complete=True)}}}) == []
    bad = catalog.validate_notes({"columns": {"status": {"codes": {**_codes({"1": "有效"}, complete=None),
                                                                   "complete": "是"}}}})
    assert bad and "码值" in bad[0]
    wrong_place = catalog.validate_notes({"label": {**catalog.make_item("入园记录", "human"), "complete": True}})
    assert wrong_place and "complete" in wrong_place[0]

    # 值没动、只勾上「已列全」：算人工改动，记为人工填写、已确认
    ticked = {"columns": {"status": {"codes": {**base["columns"]["status"]["codes"], "complete": True}}}}
    out = catalog.apply_human_edit(base, ticked, table="visits", at=AT)
    codes = out["columns"]["status"]["codes"]
    assert (codes["source"], codes["status"], codes["complete"]) == ("human", "confirmed", True)
    # 取消勾选：不留 complete: false，和没有这个标记写法一样
    unticked = {"columns": {"status": {"codes": {**codes, "complete": False}}}}
    out = catalog.apply_human_edit(out, unticked, table="visits", at=AT)
    assert "complete" not in out["columns"]["status"]["codes"]


async def test_profile_marks_codes_complete_only_when_the_whole_table_was_read(client, make_source, scenic_db):
    whole = await make_source(scenic_db, options=_on(), draft=["visits"])
    r = await client.post(_url(whole), json={"tables": ["visits"]})
    assert r.status_code == 200, r.text
    codes = (await _notes(client, whole, "visits"))["notes"]["columns"]["status"]["codes"]
    assert "统计全表" in codes["note"] and codes.get("complete") is True

    # 不做整表统计（只看前若干行）：取值可能没看全，不标
    capped = await make_source(scenic_db, options=_on(max_scan_rows=0, sample_size=10), draft=["visits"])
    r = await client.post(_url(capped), json={"tables": ["visits"]})
    assert r.status_code == 200, r.text
    codes = (await _notes(client, capped, "visits"))["notes"]["columns"]["status"]["codes"]
    assert "按前 10 行统计" in codes["note"] and "complete" not in codes


# ==========================================================================
# B2：隔天重新剖析、数据没变，不升版本
# ==========================================================================


async def test_rerun_on_another_day_with_same_data_keeps_the_version(client, make_source, tmp_path, monkeypatch):
    path = scenic.build(tmp_path / "b2.db")
    sid = await make_source(path, options=_on(), draft=["visits"])
    # 人工确认过的关系：剖析只补覆盖率和基数、换自己的备注，同样不能因为日期变了就改
    detail = await _notes(client, sid, "visits")
    gate = _relation(detail["notes"], "gate_id")
    r = await client.post(f"/api/datasources/{sid}/catalog/visits/review",
                          json={"path": f"relations.{gate['id']}", "action": "confirm", "if_version": detail["version"]})
    assert r.status_code == 200, r.text

    monkeypatch.setattr(catalog, "now_iso", lambda: DAY1)
    first = _table((await client.post(_url(sid), json={"tables": ["visits"]})).json(), "visits")
    notes_day1 = (await _notes(client, sid, "visits"))["notes"]
    monkeypatch.setattr(catalog, "now_iso", lambda: DAY2)
    second = _table((await client.post(_url(sid), json={"tables": ["visits"]})).json(), "visits")
    assert (second["added"], second["updated"], second["removed"]) == (0, 0, 0)
    assert second["version"] == first["version"]
    assert (await _notes(client, sid, "visits"))["notes"] == notes_day1        # 备注里还是第一天的日期

    # 数据变了（多了一种状态），结论跟着变：照常升版本
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT INTO visits VALUES (1501, 'TK9001501', 1, 1, 1, NULL, '2026-09-30 10:00:00', 1, 2)")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(catalog, "now_iso", lambda: DAY3)
    third = _table((await client.post(_url(sid), json={"tables": ["visits"]})).json(), "visits")
    assert third["version"] == second["version"] + 1 and third["updated"] >= 1
    codes = (await _notes(client, sid, "visits"))["notes"]["columns"]["status"]["codes"]
    assert set(codes["value"]) == {"0", "1", "2"} and "2026-10-03" in codes["note"]


def test_merge_ignores_a_profile_note_that_only_changed_its_date():
    old = {"columns": {"status": {"codes": catalog.make_item(
        {"1": "", "0": ""}, "profile", "proposed", note="数据剖析（2026-10-01）：统计全表，1500 行非空值共 2 个取值。",
        at=DAY1)}}}
    same = {"columns": {"status": {"codes": catalog.make_item(
        {"1": "", "0": ""}, "profile", "proposed", note="数据剖析（2026-10-02）：统计全表，1500 行非空值共 2 个取值。")}}}
    merged, stats = catalog.merge_notes(old, same, covered={"profile"}, at=DAY2)
    assert merged == old and stats == catalog.MergeStats()
    changed = {"columns": {"status": {"codes": catalog.make_item(
        {"1": "", "0": ""}, "profile", "proposed", note="数据剖析（2026-10-02）：统计全表，1600 行非空值共 2 个取值。")}}}
    merged, stats = catalog.merge_notes(old, changed, covered={"profile"}, at=DAY2)
    assert stats.updated == 1 and "2026-10-02" in merged["columns"]["status"]["codes"]["note"]


# ==========================================================================
# B3：公式在人工编辑、模型起草两条路径上也拦下
# ==========================================================================


def test_human_edit_rejects_formulas_in_changed_text():
    existing = {"columns": {"amount": {"label": catalog.make_item("实收金额", "llm")}}}
    for submitted in (
        {"columns": {"amount": {"label": catalog.make_item("实收金额", "llm"),
                                "meaning": {"value": "客单价=金额/人数"}}}},
        {"description": {"value": "转化率 = 下单数 / 访问数"}},
        {"columns": {"amount": {"label": {"value": "SUM(amount)"}}}},
    ):
        with pytest.raises(catalog.CatalogInvalid) as e:
            catalog.apply_human_edit(existing, submitted, table="orders", at=AT)
        assert "口径卡" in str(e.value)
    # 库里早就有的含公式的项，值没动就照常提交：不能因为旧数据挡住这张表的任何写入
    legacy = {"columns": {"amount": {"meaning": catalog.make_item("客单价=金额/人数", "llm")}}}
    out = catalog.apply_human_edit(legacy, {**legacy, "label": {"value": "订单"}}, table="orders", at=AT)
    assert out["columns"]["amount"]["meaning"]["value"] == "客单价=金额/人数"
    assert catalog.validate_notes(legacy) == []           # 结构校验不查公式


async def test_put_with_a_formula_is_422_and_says_where_formulas_go(client, make_source, scenic_db):
    sid = await make_source(scenic_db, draft=["orders"])
    detail = await _notes(client, sid, "orders")
    notes = {**detail["notes"], "columns": {"total_amount": {"meaning": {"value": "客单价=金额/人数"}}}}
    r = await _put(client, sid, "orders", notes, detail["version"])
    assert r.status_code == 422 and "口径卡" in r.json()["detail"]
    assert (await _notes(client, sid, "orders"))["version"] == detail["version"]


async def test_model_draft_drops_formula_items():
    from types import SimpleNamespace

    meta = {"qualified": "orders", "columns": [{"name": "id", "type": "INTEGER"},
                                               {"name": "total_amount", "type": "REAL"}],
            "primary_key": ["id"], "foreign_keys": []}
    source = SimpleNamespace(id="src", name="shop", kind="sqlite", origin="manual",
                             schema_cache={"tables": {"orders": meta}})
    model = FakeModel({"orders": {"label": "客单价=金额/人数", "grain": "每笔订单一行", "columns": [
        {"name": "total_amount", "label": "订单金额", "meaning": "= SUM(明细金额)", "unit": "元"}]}})
    notes = (await catalog.draft_with_model(model, source, ["orders"]))["orders"].notes
    assert "label" not in notes and notes["grain"]["value"] == "每笔订单一行"
    assert set(notes["columns"]["total_amount"]) == {"label", "unit"}


# ==========================================================================
# 一致性
# ==========================================================================


@pytest.mark.parametrize("name, type_, expected", [
    ("visit_time", "TEXT", True), ("ordered_at", "TEXT", True), ("joined_on", "VARCHAR(20)", True),
    ("createdAt", "", True), ("biz_date", "DATE", True), ("paid", "DATETIME", True), ("loaded", "TIMESTAMP", True),
    ("dt", "TEXT", True), ("date", "TEXT", True),
    ("created_at", "INTEGER", False),        # 整数存的时间戳：取值不是日期，剖析和检查都不当日期列
    ("open_time", "TIME", False),            # 只有时刻、没有日期
    ("on_hand", "INTEGER", False), ("status", "TEXT", False), ("at", "TEXT", False), ("ticket_no", "TEXT", False),
])
def test_date_column_rule_is_shared(name, type_, expected):
    assert catalog.is_date_column(name, type_) is expected
    # 剖析认的日期列（业务日期提议）和 SQL 检查认的时间列（按别的时间列统计）是同一套
    assert catalog_profile._is_date_column({"name": name, "type": type_}) is expected
    table = sqlcheck._Table(alias="t", name="t", meta={"columns": [{"name": name, "type": type_}]}, notes={},
                            order=0)
    assert table.temporal(name) is expected


def test_table_names_match_by_name_key_everywhere():
    """表名只把 ASCII 字母转小写（name_key，和 SQLite 比较标识符一致）：非 ASCII 的大小写不同就是两张表。"""
    cache = {"tables": {"CAFÉ_ORDERS": {"columns": []}, "Visits": {"columns": []}}}
    assert catalog.resolve_table_name(cache, "visits") == "Visits"
    assert catalog.resolve_table_name(cache, "main.VISITS") == "Visits"
    assert catalog.resolve_table_name(cache, "cafÉ_orders") == "CAFÉ_ORDERS"
    assert catalog.resolve_table_name(cache, "café_orders") is None
    checker = sqlcheck.SqlChecker(kind="sqlite", schema_cache=cache, notes={"CAFÉ_ORDERS": {
        "label": catalog.make_item("咖啡订单", "human")}})
    assert checker.notes_of("cafÉ_orders")["label"]["value"] == "咖啡订单"
    assert checker.notes_of("café_orders") == {}


def test_word_splitting_has_one_implementation():
    assert catalog.name_words("memberTagId") == ["member", "tag", "id"]
    assert catalog.name_words("MEMBER_TAG") == ["member", "tag"]
    assert not hasattr(catalog_profile, "_words") and not hasattr(catalog_profile, "_WORDS")
