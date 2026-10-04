"""探查时补读外键约束和唯一约束 / 唯一索引，以及数据目录在合成大库上的起草效果。

数据目录的关系起草靠它：外键记成有确证的关系，唯一约束用来判断一对一。读不到（方言不支持、权限不够、
某个对象反射报错）就跳过那一项，不能毁掉整次探查；跳过的表不写这两个键，下游据此分得清「读过、没有」
和「不知道」（旧缓存里也没有这两个键）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.engine.reflection import Inspector

from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from tests.fixtures.catalog import scenic


def _source(path: str, sid: str = "scenic-src") -> SimpleNamespace:
    return SimpleNamespace(id=sid, name="scenic", kind="sqlite", database=path, host=None, port=None, username=None,
                           password=None, options={}, readonly=True, description="景区业务库", schema_cache={},
                           origin="manual")


@pytest.fixture
async def scenic_cache(scenic_db):
    source = _source(scenic_db, "scenic-cache")
    try:
        cache = await introspect(source)
    finally:
        await engines.invalidate(source.id)
    return cache


async def test_fixture_is_large_and_complete(scenic_cache):
    tables = scenic_cache["tables"]
    assert len(tables) == scenic.TABLE_COUNT + len(scenic.VIEWS) > 40
    assert not scenic_cache.get("truncated") and not scenic_cache.get("skipped")


async def test_foreign_keys_are_read(scenic_cache):
    found = {(name, tuple(fk["columns"]), fk["to_table"], tuple(fk["to_columns"]))
             for name, meta in scenic_cache["tables"].items() for fk in meta["foreign_keys"]}
    assert found == scenic.FK_RELATIONS
    # 每张表、每个视图都读过：没有外键的是空列表，不是缺键
    assert all(isinstance(m.get("foreign_keys"), list) for m in scenic_cache["tables"].values())
    assert scenic_cache["tables"]["v_daily_visits"]["foreign_keys"] == []


async def test_unique_constraints_and_unique_indexes_are_read(scenic_cache):
    tables = scenic_cache["tables"]
    for name, groups in scenic.UNIQUE.items():
        for group in groups:
            assert group in tables[name]["unique"], (name, group)
    # 主键不重复记成唯一约束；没有唯一约束的是空列表
    assert ["id"] not in tables["orders"]["unique"]
    assert tables["refunds"]["unique"] == []


async def test_constraint_reflection_failure_does_not_break_introspection(scenic_db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("permission denied for information_schema")

    for name in ("get_multi_foreign_keys", "get_foreign_keys", "get_multi_unique_constraints",
                 "get_unique_constraints", "get_multi_indexes", "get_indexes"):
        monkeypatch.setattr(Inspector, name, boom)
    source = _source(scenic_db, "scenic-broken")
    try:
        cache = await introspect(source)
    finally:
        await engines.invalidate(source.id)
    assert not cache.get("failed") and len(cache["tables"]) == scenic.TABLE_COUNT + len(scenic.VIEWS)
    visits = cache["tables"]["visits"]
    assert "foreign_keys" not in visits and "unique" not in visits
    assert visits["primary_key"] == ["id"] and visits["columns"]


async def test_not_implemented_constraints_are_skipped(scenic_db, monkeypatch):
    def unsupported(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(Inspector, "get_multi_foreign_keys", unsupported)
    monkeypatch.setattr(Inspector, "get_foreign_keys", unsupported)
    source = _source(scenic_db, "scenic-nofk")
    try:
        cache = await introspect(source)
    finally:
        await engines.invalidate(source.id)
    assert "foreign_keys" not in cache["tables"]["visits"]
    assert isinstance(cache["tables"]["visits"]["unique"], list)


# ---------------------------------------------------------------- 在合成大库上起草


async def test_structure_draft_on_scenic_db(scenic_db, scenic_cache):
    source = SimpleNamespace(**{**vars(_source(scenic_db)), "schema_cache": scenic_cache})
    fk, name = set(), set()
    for table in scenic_cache["tables"]:
        for rel in catalog.draft_structure(source, table).notes.get("relations", []):
            entry = (table, tuple(rel["columns"]), rel["to_table"], tuple(rel["to_columns"]))
            (fk if rel["source"] == "fk" else name).add(entry)
            assert rel["status"] == ("verified" if rel["source"] == "fk" else "proposed")
    assert fk == scenic.FK_RELATIONS
    assert name == scenic.NAME_RELATIONS
    inferred_columns = {(t, c[0]) for t, c, _, _ in name}
    assert not inferred_columns & set(scenic.NOT_INFERRED)


async def test_join_path_through_link_table(scenic_cache):
    graph = catalog.relation_graph({}, schema_cache=scenic_cache)
    paths = catalog.join_paths(graph, "visits", "channels")
    via = [tuple(e.to_table for e in p) for p in paths]
    assert ("channel_visits", "channels") in via
    best = paths[0]
    assert all(e.status == "verified" for e in best)                # 两跳都是外键，排在推断的路径前面
    assert [e.condition() for e in best] == ["visits.id = channel_visits.visit_id",
                                             "channel_visits.channel_id = channels.id"]
