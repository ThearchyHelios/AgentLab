"""数据目录交给模型的文本、关系图与连接路径、冻结内容、使用次数。"""
from __future__ import annotations

import uuid

from app.data import catalog
from app.data.catalog import make_item
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run
from tests.fixtures.sources import drop_source

MARK = "（推断，未确认）"


def _rel(table, columns, to_table, to_columns, source="fk", status=None, cardinality="many_to_one"):
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns, "to_table": to_table,
            "to_columns": to_columns, "cardinality": cardinality, "coverage": None, "source": source,
            "status": status or catalog.initial_status(source)}


VISITS = {
    "label": make_item("入园记录", "human"),
    "grain": make_item("每张门票每次检票一行", "llm"),
    "kind": make_item("fact", "llm", "confirmed"),
    "keys": make_item(["ticket_no"], "human"),
    "business_date": make_item({"column": "visit_time", "rule": "按检票时间计", "timezone": "+08:00"}, "human"),
    "valid_filter": make_item("status = 1", "human"),
    "dedup": make_item("同一票号只计一次", "llm", "rejected"),
    "columns": {
        "visitor_count": {"meaning": make_item("这次检票进园的人数", "human"), "unit": make_item("人", "llm"),
                          "measure": make_item("flow", "llm", "confirmed")},
        "status": {"codes": make_item({"1": "有效", "0": "作废"}, "human")},
    },
    "relations": [_rel("visits", ["park_id"], "parks", ["id"]),
                  _rel("visits", ["gate_id"], "gates", ["id"], source="name"),
                  _rel("visits", ["member_id"], "members", ["id"], source="name", status="rejected")],
}


def test_full_render_shows_every_fact_and_marks_inferences():
    text = catalog.render_table_notes(VISITS)
    assert "中文名：入园记录" in text
    assert f"粒度：每张门票每次检票一行{MARK}" in text
    assert "表类型：事实表" in text and "业务主键：ticket_no" in text
    assert "业务日期：visit_time" in text and "按检票时间计" in text and "+08:00" in text
    assert "有效记录条件：status = 1" in text
    assert "visitor_count：这次检票进园的人数" in text and f"单位：人{MARK}" in text and "流量" in text
    assert "1=有效" in text and "0=作废" in text
    assert "park_id → parks.id" in text and f"gate_id → gates.id（多对一）{MARK}" in text
    # 驳回的项模型看不到
    assert "同一票号只计一次" not in text and "members" not in text
    # 有确证、已确认的不标推断
    assert "表类型：事实表（" not in text and "中文名：入园记录（" not in text


def test_render_of_empty_or_all_rejected_notes_is_empty():
    assert catalog.render_table_notes({}) == ""
    assert catalog.render_table_notes({"dedup": make_item("x", "llm", "rejected")}) == ""


def test_render_beside_system_notes_only_adds_what_they_do_not_cover():
    """导入表格：表和列已有按核对结果生成的说明。保留说明，只追加人工确认过的、或说明没有覆盖的项。"""
    meta = {"comment": "粒度：每个日期。客流 等于 各入口之和（导入时已逐日核对）",
            "columns": [{"name": "日期", "type": "TEXT", "comment": "格式 YYYY-MM-DD"},
                        {"name": "客流", "type": "INTEGER", "comment": None}]}
    notes = {"grain": make_item("每天一行", "llm"), "label": make_item("客流日报", "llm"),
             "description": make_item("人工补充的说明", "human"),
             "columns": {"日期": {"meaning": make_item("统计日期", "llm"), "measure": make_item("attribute", "llm")},
                         "客流": {"unit": make_item("人次", "llm")}}}
    text = catalog.render_table_notes(notes, meta=meta, system_notes=True)
    assert "每天一行" not in text and "统计日期" not in text       # 说明已经覆盖、又没人确认的：不追加
    assert "客流日报" in text and "人工补充的说明" in text           # 说明没有覆盖的、人工确认过的：追加
    assert "属性" in text and "人次" in text


def test_compact_index_one_line_per_table():
    cache = {"tables": {"visits": {"qualified": "visits", "columns": []},
                        "parks": {"qualified": "main.parks", "columns": []},
                        "gates": {"qualified": "gates", "columns": []}}}
    text = catalog.render_table_index(cache, {"visits": VISITS, "parks": {"label": make_item("景区", "llm")}})
    lines = text.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("visits｜入园记录｜每张门票每次检票一行") and lines[0].endswith(MARK)
    assert lines[1].startswith("main.parks｜景区") and MARK in lines[1]
    assert lines[2] == "gates"


# ---------------------------------------------------------------- 关系图


NOTES = {
    "visits": {"relations": [_rel("visits", ["park_id"], "parks", ["id"]),
                             _rel("visits", ["member_id"], "members", ["id"], source="name", status="rejected")]},
    "channel_visits": {"relations": [_rel("channel_visits", ["channel_id"], "channels", ["id"]),
                                     _rel("channel_visits", ["visit_id"], "visits", ["id"])]},
    "orders": {"relations": [_rel("orders", ["channel_id"], "channels", ["id"]),
                             _rel("orders", ["member_id"], "members", ["id"])]},
}


def test_graph_adjacency_is_bidirectional_and_skips_rejected():
    graph = catalog.relation_graph(NOTES)
    assert {e.to_table for e in graph["visits"]} == {"parks", "channel_visits"}
    back = next(e for e in graph["channels"] if e.to_table == "orders")
    assert back.from_columns == ("id",) and back.to_columns == ("channel_id",) and back.cardinality == "one_to_many"
    assert all(e.to_table != "members" for e in graph["visits"])


def test_join_paths_up_to_two_hops():
    graph = catalog.relation_graph(NOTES)
    paths = catalog.join_paths(graph, "visits", "channels")
    assert [tuple(e.to_table for e in p) for p in paths] == [("channel_visits", "channels")]
    assert paths[0][0].condition() == "visits.id = channel_visits.visit_id"
    assert paths[0][1].condition() == "channel_visits.channel_id = channels.id"
    assert catalog.join_paths(graph, "visits", "parks")[0][0].to_table == "parks"
    # 三跳以上不给：visits → channel_visits → channels → orders
    assert catalog.join_paths(graph, "visits", "orders") == []
    # 驳回的关系不出现在路径里
    assert catalog.join_paths(graph, "visits", "members") == []


def test_graph_can_fill_in_relations_from_structure():
    """没起草过目录的表，关系图可以按表结构现推（外键 + 命名推断），驳回过的照样排除。"""
    cache = {"tables": {
        "visits": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "gate_id", "type": "INTEGER"},
                               {"name": "member_id", "type": "INTEGER"}],
                   "primary_key": ["id"], "foreign_keys": []},
        "gates": {"columns": [{"name": "id", "type": "INTEGER"}], "primary_key": ["id"], "foreign_keys": []},
        "members": {"columns": [{"name": "id", "type": "INTEGER"}], "primary_key": ["id"], "foreign_keys": []},
    }}
    rejected = {"visits": {"relations": [_rel("visits", ["member_id"], "members", ["id"], source="name",
                                              status="rejected")]}}
    graph = catalog.relation_graph(rejected, schema_cache=cache)
    assert {e.to_table for e in graph["visits"]} == {"gates"}
    assert graph["gates"][0].status == "proposed"


# ---------------------------------------------------------------- 冻结


def test_frozen_catalog_strips_rejected_and_keeps_versions():
    entries = {"visits": catalog.CatalogEntry("visits", VISITS, 4),
               "gone": catalog.CatalogEntry("gone", {"label": make_item("x", "human")}, 2),
               "empty": catalog.CatalogEntry("empty", {"dedup": make_item("x", "llm", "rejected")}, 1)}
    frozen = catalog.frozen_catalog(entries, ["visits", "empty", "parks"])
    assert set(frozen) == {"visits"}                              # 表结构里没有的、全被驳回的都不冻结
    assert frozen["visits"]["version"] == 4
    assert "dedup" not in frozen["visits"]["notes"]
    # 两条没被驳回的关系原样冻结；驳回的那条只留编号和状态（重建检查器时不被按外键、命名推回来）
    relations = frozen["visits"]["notes"]["relations"]
    assert len([r for r in relations if r.get("status") != "rejected"]) == 2
    assert [r for r in relations if r.get("status") == "rejected"] == [
        {"id": r["id"], "status": "rejected"} for r in VISITS["relations"] if r.get("status") == "rejected"]
    assert len(catalog.visible_notes(frozen["visits"]["notes"])["relations"]) == 2
    assert catalog.frozen_catalog({}, ["visits"]) == {}


# ---------------------------------------------------------------- 使用次数


async def test_table_usage_counts_run_queries_per_table(monkeypatch):
    sid = uuid.uuid4().hex
    name = f"use_{sid[:8]}"
    cache = {"tables": {"visits": {"qualified": "main.visits"}, "parks": {"qualified": "main.parks"},
                        "明细": {"qualified": "main.明细"}}}
    run_a, run_b = uuid.uuid4().hex, uuid.uuid4().hex
    legacy = {sid[:8] + "legacy1": {"source": name, "sql": 'SELECT * FROM "Visits" v JOIN main.parks p ON 1=1'}}
    monkeypatch.setattr(catalog, "_load_artifact", lambda aid: legacy.get(aid))
    async with SessionLocal() as session:
        session.add(DataSource(id=sid, name=name, kind="sqlite", database=":memory:", schema_cache=cache))
        session.add_all([Run(id=run_a, graph={}), Run(id=run_b, graph={})])
        session.add_all([
            # 新写法：查询当时把源和表名记在工件引用的 meta 里
            Artifact(id=sid[:8] + "q1", run_id=run_a, kind="query_snapshot",
                     meta={"source_id": sid, "source": name, "tables": ["visits"]}),
            Artifact(id=sid[:8] + "q2", run_id=run_b, kind="query_snapshot",
                     meta={"source_id": sid, "source": name, "tables": ["visits", "明细"]}),
            # 同一个查询快照被两次运行引用：各算一次
            Artifact(id=sid[:8] + "q2", run_id=run_a, kind="query_snapshot",
                     meta={"source_id": sid, "source": name, "tables": ["visits", "明细"]}),
            # 别的数据源、工具库试用（不属于任何运行）、别的工件：不算
            Artifact(id=sid[:8] + "q3", run_id=run_a, kind="query_snapshot",
                     meta={"source_id": "other", "source": "other", "tables": ["visits"]}),
            Artifact(id=sid[:8] + "q4", run_id="playground", kind="query_snapshot",
                     meta={"source_id": sid, "source": name, "tables": ["visits"]}),
            Artifact(id=sid[:8] + "s1", run_id=run_a, kind="schema_snapshot", meta={"source": name}),
            # 老的查询快照引用没有 meta：读工件内容里的源名和 SQL
            Artifact(id=sid[:8] + "legacy1", run_id=run_b, kind="query_snapshot", meta={}),
        ])
        await session.commit()
        source = await session.get(DataSource, sid)
        usage = await catalog.table_usage(session, source)
    await drop_source(sid)
    assert usage == {"visits": 4, "明细": 2, "parks": 1}
