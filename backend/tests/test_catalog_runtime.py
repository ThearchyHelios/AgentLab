"""数据目录在运行时：db_schema 工具输出附上目录，查询时目录随表结构快照冻结进证据，查询快照记下用到的表。

- describe_table（db_schema 工具的输出）附上这张表的目录；推断的项标「推断，未确认」，驳回的不出现。
  导入表格的源保留系统生成的说明，目录只追加说明没有覆盖的、或人工确认过的项。
- 表结构快照新增顶层键 catalog：{表: {version, notes（去掉驳回项）}}。没有目录的源一个字都不加；
  目录不变则快照不变（内容寻址，哈希相同）。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from app.core.artifact_store import canonical_json, content_hash, load
from app.data import catalog
from app.data.catalog import make_item
from app.data.engine import engines
from app.data.introspect import describe_table, introspect
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource
from app.tools.datasource import _store_schema, build_datasource_tools
from app.tools.registry import ToolContext

MARK = "（推断，未确认）"


def _rel(table, columns, to_table, to_columns, source="fk", status=None):
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns, "to_table": to_table,
            "to_columns": to_columns, "cardinality": "many_to_one", "coverage": None, "source": source,
            "status": status or catalog.initial_status(source)}


CACHE = {
    "schema": "main", "synced_at": "2026-10-01T00:00:00+00:00", "truncated": False, "total": 2,
    "tables": {
        "visits": {"qualified": "visits", "schema": None, "is_view": False, "comment": None, "primary_key": ["id"],
                   "columns": [{"name": "id", "type": "INTEGER", "nullable": False, "comment": None},
                               {"name": "park_id", "type": "INTEGER", "nullable": False, "comment": None},
                               {"name": "visitor_count", "type": "INTEGER", "nullable": False, "comment": None},
                               {"name": "status", "type": "INTEGER", "nullable": False, "comment": None}]},
        "parks": {"qualified": "parks", "schema": None, "is_view": False, "comment": None, "primary_key": ["id"],
                  "columns": [{"name": "id", "type": "INTEGER", "nullable": False, "comment": None}]},
    },
}

NOTES = {
    "label": make_item("入园记录", "human"),
    "grain": make_item("每张门票每次检票一行", "llm"),
    "keys": make_item(["id"], "human"),
    "business_date": make_item({"column": "visit_time", "rule": "按检票时间计"}, "human"),
    "valid_filter": make_item("status = 1", "llm"),
    "dedup": make_item("按票号去重", "llm", "rejected"),
    "columns": {"visitor_count": {"meaning": make_item("这次检票进园的人数", "llm"), "unit": make_item("人", "human"),
                                  "measure": make_item("flow", "llm")},
                "status": {"codes": make_item({"1": "有效", "0": "作废"}, "human")}},
    "relations": [_rel("visits", ["park_id"], "parks", ["id"])],
}


def _source(cache=CACHE, **kw) -> SimpleNamespace:
    return SimpleNamespace(id=kw.pop("id", "src-x"), name=kw.pop("name", "scenic"), kind="sqlite", options={},
                           readonly=True, description="", schema_cache=cache, origin=kw.pop("origin", "manual"), **kw)


# ---------------------------------------------------------------- describe_table


def test_describe_table_appends_catalog():
    text = describe_table(_source(), "visits", catalog={"visits": NOTES})
    head, _, block = text.partition(catalog.NOTES_HEADING)
    assert head.startswith("表 visits") and "  visitor_count  INTEGER" in head     # 原来的列清单照旧
    assert "中文名：入园记录" in block and f"粒度：每张门票每次检票一行{MARK}" in block
    assert "业务主键：id" in block and "业务日期：visit_time；规则：按检票时间计" in block
    assert f"有效记录条件：status = 1{MARK}" in block
    assert f"visitor_count：这次检票进园的人数{MARK}；单位：人；度量类型：流量，可跨期加总{MARK}" in block
    assert "status：码值：1=有效、0=作废" in block
    assert "park_id → parks.id（多对一）" in block
    assert "按票号去重" not in text


def test_describe_table_without_catalog_is_unchanged():
    plain = describe_table(_source(), "visits")
    assert describe_table(_source(), "visits", catalog={}) == plain
    assert describe_table(_source(), "visits", catalog={"parks": NOTES}) == plain
    assert catalog.NOTES_HEADING not in plain
    # 全名、大小写不一致也对得上目录
    assert catalog.NOTES_HEADING in describe_table(_source(), "MAIN.Visits", catalog={"visits": NOTES})


def test_describe_table_keeps_system_notes_of_imported_tables():
    """导入表格：说明是按核对结果生成的。保留说明，目录不重复它、不和它抢话。"""
    cache = {"import_mode": "recipe", "tables": {"日报": {
        "qualified": "日报", "comment": "粒度：每个日期。不要把各入口相加", "primary_key": [],
        "columns": [{"name": "日期", "type": "TEXT", "nullable": False, "comment": "格式 YYYY-MM-DD"},
                    {"name": "客流", "type": "INTEGER", "nullable": True, "comment": "单位：人次"}]}}}
    notes = {"label": make_item("客流日报", "llm"), "grain": make_item("每天一行", "llm"),
             "columns": {"客流": {"unit": make_item("人", "llm"), "measure": make_item("flow", "llm")},
                         "日期": {"meaning": make_item("统计日期", "human")}}}
    text = describe_table(_source(cache, origin="upload"), "日报", catalog={"日报": notes})
    assert text.startswith("表 日报（粒度：每个日期。不要把各入口相加）")
    assert "单位：人次" in text and "格式 YYYY-MM-DD" in text                 # 系统说明原样保留
    assert "每天一行" not in text                                            # 表说明已写粒度：推断的粒度不追加
    assert "单位：人" + MARK not in text                                     # 列说明已写单位：推断的单位不追加
    assert "客流日报" in text and "流量" in text                              # 说明没有覆盖的照常追加
    assert "日期：统计日期" in text                                          # 人工确认过的追加


# ---------------------------------------------------------------- 冻结进表结构快照


def _entry(notes, version=1):
    return {"visits": catalog.CatalogEntry("visits", notes, version)}


async def test_store_schema_freezes_catalog_and_is_content_addressed():
    ctx = SimpleNamespace(run_id="", node_id="")
    plain = await _store_schema(_source(), ctx)
    before = {"source": "scenic", "tables": CACHE["tables"],
              **{k: CACHE[k] for k in ("schema", "synced_at", "truncated", "total")}}
    assert plain == content_hash(canonical_json(before))                   # 没有目录：和以前一字不差
    assert await _store_schema(_source(), ctx, catalog={}) == plain

    first = await _store_schema(_source(), ctx, catalog=_entry(NOTES, 3))
    frozen = load(first)
    assert frozen["catalog"]["visits"]["version"] == 3
    assert "dedup" not in frozen["catalog"]["visits"]["notes"]              # 驳回的不冻结
    assert frozen["tables"] == CACHE["tables"]                              # 已有字段的语义不变
    # 目录不变：同一件工件
    assert await _store_schema(_source(), ctx, catalog=_entry(json.loads(json.dumps(NOTES)), 3)) == first
    # 目录变了（升了版本）：换一件
    assert await _store_schema(_source(), ctx, catalog=_entry(NOTES, 4)) != first
    # 只有驳回项、或者表不在快照里：不加这个键
    assert await _store_schema(_source(), ctx, catalog={
        "visits": catalog.CatalogEntry("visits", {"dedup": make_item("x", "llm", "rejected")}, 1),
        "gone": catalog.CatalogEntry("gone", {"label": make_item("x", "human")}, 1)}) == plain


def test_snapshot_readers_ignore_catalog_key():
    """读表结构快照的地方（证据台账的表结构条目、裁判摘录的口径行）只认自己的键，多一个 catalog 不影响。"""
    from app.data.provenance import judge_lines
    from app.engine.evidence import schema_entry_fields

    tables = {"日报": {"qualified": "日报", "comment": "粒度：每个日期", "primary_key": [],
                       "columns": [{"name": "客流", "type": "INTEGER", "nullable": True, "comment": "单位：人次"}]}}
    base = {"source": "flow", "tables": tables, "schema": "main"}
    with_catalog = {**base, "catalog": {"日报": {"version": 2, "notes": {"label": make_item("客流日报", "human")}}}}
    assert schema_entry_fields(with_catalog) == schema_entry_fields(base)
    query = {"data_version": "snap1", "columns": ["客流"], "sql": "SELECT 客流 FROM 日报"}
    lines = judge_lines(query, base, lambda _id: None, tables=["日报"], hidden=set())
    assert lines and judge_lines(query, with_catalog, lambda _id: None, tables=["日报"], hidden=set()) == lines


# ---------------------------------------------------------------- 工具里串起来


async def test_tools_show_and_freeze_catalog_and_record_query_tables(scenic_db):
    async with SessionLocal() as session:
        row = DataSource(name=f"cat_{uuid.uuid4().hex[:10]}", kind="sqlite", database=scenic_db)
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await catalog.write_entry(session, row.id, "visits", {"label": make_item("入园记录", "human"),
                                                               "grain": make_item("每张门票每次检票一行", "llm")},
                                  if_version=0, actor="王敏")
        run_id = uuid.uuid4().hex
        ctx = ToolContext(run_id=run_id, node_id="n1", data_versions=None)
        tools = {t.name: t for t in await build_datasource_tools(
            [f"db_query__{row.name}", f"db_schema__{row.name}"], ctx, session)}
    try:
        shown = await tools[f"db_schema__{row.name}"].coroutine(table="visits")
        assert "中文名：入园记录" in shown and f"粒度：每张门票每次检票一行{MARK}" in shown
        other = await tools[f"db_schema__{row.name}"].coroutine(table="parks")
        assert catalog.NOTES_HEADING not in other

        result = json.loads(await tools[f"db_query__{row.name}"].coroutine(
            sql="SELECT v.id FROM visits v JOIN parks p ON p.id = v.park_id LIMIT 3"))
        frozen = load(result["schema_artifact"])
        assert frozen["catalog"] == {"visits": {"version": 1, "notes": {
            "label": make_item("入园记录", "human"), "grain": make_item("每张门票每次检票一行", "llm")}}}
        async with SessionLocal() as session:
            ref = await session.get(Artifact, (result["artifact"], run_id))
        assert ref.meta == {"source_id": row.id, "source": row.name, "tables": ["visits", "parks"]}
    finally:
        await engines.invalidate(row.id)
