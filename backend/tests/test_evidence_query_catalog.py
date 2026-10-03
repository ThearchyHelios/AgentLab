"""证据面板的查询步骤：合并查询的输入带上检查结果，查询步骤说明用的是哪一版数据目录。

- 合并查询的列追不到逐格来历时，证据面板只有合并步骤、没有输入步骤：输入查询的 SQL 检查结果以前在这里看不到，
  指标却因为它标了「存疑」。现在合并步骤的每个输入带上 checks（不带给模型的那句）。
- 表结构快照里冻结了数据目录（随查询冻结的那一版），以前没有任何读取方。查询步骤带上表结构快照 id 和这条查询
  用到的表的目录版本，面板上写「数据目录：入园记录 第 3 版」。
- 冻结时保留被驳回关系的编号（只放编号和状态）：拿冻结的那份重建检查器，按外键现推的关系不会把驳回的推回来。
"""
from __future__ import annotations

from app.api import evidence as api
from app.core import artifact_store
from app.data import catalog, sqlcheck
from app.engine.merge_query import MERGE_SOURCE, MergeInput, execute

FANOUT = {"code": "fanout_sum", "level": "error", "message": "「订单」关联「订单明细」是一对多，求和会重复计算。先汇总再关联",
          "for_model": "orders 关联 order_items 后对 total_amount 求和会重复计算", "table": "orders"}
NOTICE = {"code": "missing_valid_filter", "level": "warning", "message": "没有筛掉作废订单", "for_model": "加 status = 1",
          "table": "orders"}


async def put(obj, kind="query_snapshot") -> str:
    return await artifact_store.put_json(obj, kind=kind)


def _sealed(queries: dict[str, dict]) -> api._Sealed:
    seal = {"sealed": True, "ok": True, "manifest_seq": 9, "legacy": False}
    return api._Sealed(run_id="run-x", graph={}, seal=seal, events=[], queries=queries)


async def test_merge_inputs_carry_their_sql_checks():
    sales = {"columns": ["日期", "销售额"], "rows": [["2026-05-01", 10]], "row_count": 1, "truncated": False,
             "sql": "SELECT 日期, SUM(o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id",
             "source": "shop", "checks": [FANOUT, NOTICE]}
    visits = {"columns": ["日期", "到店"], "rows": [["2026-05-01", 3]], "row_count": 1, "truncated": False,
              "sql": "SELECT 日期, 到店 FROM 到店", "source": "foot"}
    sid, vid = await put(sales), await put(visits)
    outcome = execute([MergeInput(alias="s", node_id="q_sales", artifact=sid, snapshot=sales),
                       MergeInput(alias="v", node_id="q_visits", artifact=vid, snapshot=visits)],
                      'SELECT s."日期", s."销售额" * 1.0 / v."到店" AS 店均 FROM s JOIN v ON s."日期" = v."日期"')
    mid = await put(outcome.snapshot())
    doc = {"catalog": {"Q1": {"kind": "query", "artifact": sid}, "Q2": {"kind": "query", "artifact": vid},
                       "Q3": {"kind": "query", "artifact": mid, "source": MERGE_SOURCE}}}
    sealed = _sealed({sid: {"node_id": "q_sales"}, vid: {"node_id": "q_visits"}, mid: {"node_id": "merge"}})
    # 算出来的列追不到逐格来历：只有合并步骤，输入的检查结果要在合并步骤里看得到
    [step] = await api._query_steps(mid, [(0, "店均")], doc, sealed, api._Masks())
    s_in, v_in = step["merge"]["inputs"]
    assert [c["code"] for c in s_in["checks"]] == ["fanout_sum", "missing_valid_filter"]
    assert all("for_model" not in c for c in s_in["checks"])
    assert s_in["checks"][0]["level"] == "error" and s_in["checks"][0]["table"] == "orders"
    assert "checks" not in v_in


def _schema_cache() -> dict:
    return {"schema": "main", "tables": {
        "visits": {"qualified": "main.visits", "primary_key": ["id"],
                   "columns": [{"name": "id", "type": "INTEGER"}, {"name": "member_id", "type": "INTEGER"},
                               {"name": "fee", "type": "REAL"}],
                   "foreign_keys": [{"columns": ["member_id"], "to_table": "members", "to_columns": ["id"]}]},
        "members": {"qualified": "main.members", "primary_key": ["id"],
                    "columns": [{"name": "id", "type": "INTEGER"}], "foreign_keys": []}}}


def _fk_relation(cache: dict) -> dict:
    [rel] = [r for r in catalog.fk_relations(cache, "visits") or [] if r["to_table"] == "members"]
    return rel


async def test_the_query_step_names_the_catalog_version_it_was_checked_against():
    cache = _schema_cache()
    notes = {"label": catalog.make_item("入园记录", "human")}
    frozen = catalog.frozen_catalog({"visits": catalog.CatalogEntry("visits", notes, 3)}, cache["tables"])
    schema_id = await put({"source": "scenic", **cache, "catalog": frozen}, kind="schema_snapshot")
    query = {"columns": ["n"], "rows": [[4]], "row_count": 1, "truncated": False, "source": "scenic",
             "sql": "SELECT COUNT(*) AS n FROM visits v JOIN members m ON m.id = v.member_id",
             "schema_artifact": schema_id}
    qid = await put(query)
    doc = {"catalog": {"Q1": {"kind": "query", "artifact": qid}}}
    step = await api._query_step(qid, [(0, "n")], doc, _sealed({qid: {"node_id": "fetch"}}), api._Masks())
    assert step["schema_artifact"] == schema_id
    # 只列这条查询用到、而且有目录的表：members 没有目录，不列
    assert step["catalog"] == [{"table": "visits", "label": "入园记录", "version": 3}]


async def test_a_query_without_a_frozen_catalog_says_nothing_extra():
    """没对照过目录的查询（老快照、没有目录的源）：步骤的形状和以前一字不差（期 4 的金样逐字比对）。"""
    schema_id = await put({"source": "scenic", **_schema_cache()}, kind="schema_snapshot")
    for extra in ({}, {"schema_artifact": schema_id}):
        query = {"columns": ["n"], "rows": [[4]], "row_count": 1, "truncated": False, "source": "scenic",
                 "sql": "SELECT COUNT(*) AS n FROM visits", **extra}
        qid = await put(query)
        doc = {"catalog": {"Q1": {"kind": "query", "artifact": qid}}}
        step = await api._query_step(qid, [(0, "n")], doc, _sealed({qid: {"node_id": "fetch"}}), api._Masks())
        assert "catalog" not in step and "schema_artifact" not in step


def test_frozen_catalog_keeps_rejected_relation_ids_without_their_content():
    cache = _schema_cache()
    rel = {**_fk_relation(cache), "status": "rejected", "note": "这条外键是历史遗留，不能拿来关联"}
    notes = {"label": catalog.make_item("入园记录", "human"), "relations": [rel]}
    frozen = catalog.frozen_catalog({"visits": catalog.CatalogEntry("visits", notes, 2)}, cache["tables"])
    assert frozen["visits"]["notes"]["relations"] == [{"id": rel["id"], "status": "rejected"}]
    assert catalog.visible_notes(frozen["visits"]["notes"]) == {"label": notes["label"]}

    # 拿冻结的那份重建检查器：驳回过的外键关系不会按表结构又推回来
    checker = sqlcheck.frozen_checker({"source": "scenic", **cache, "catalog": frozen}, kind="sqlite")
    assert checker is not None and checker.edges_between("visits", "members") == []
    live = sqlcheck.SqlChecker(kind="sqlite", schema_cache=cache, notes={"visits": notes})
    assert live.edges_between("visits", "members") == []           # 和当时库里那份一致

    # 只有驳回关系的表也要冻结：不然编号丢了，重建时又推回来
    only = catalog.frozen_catalog({"visits": catalog.CatalogEntry("visits", {"relations": [rel]}, 1)}, cache["tables"])
    assert only == {"visits": {"version": 1, "notes": {"relations": [{"id": rel["id"], "status": "rejected"}]}}}


def test_a_snapshot_without_a_catalog_rebuilds_no_checker():
    assert sqlcheck.frozen_checker({"source": "scenic", **_schema_cache()}, kind="sqlite") is None
    assert sqlcheck.frozen_checker(None, kind="sqlite") is None
