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


