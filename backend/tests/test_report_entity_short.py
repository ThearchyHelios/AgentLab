"""字段标记紧跟在同一张表名后面时只显示字段名（engine/evidence._short_column）。

部署上撞过「取自 某表 的 某表.某列」：[[c:表.列]] 渲染成「表.列」全名，写作者前面又写了「某表 的」。现在：标记前面紧挨着
（隔空白、「的」「中的」「里的」）刚写过同一张表的名字（写成文字或 [[t:同一张表]] 都算）时，只显示列名；引用、出处、证据
标识照旧是那个字段，引用上记 short: true，复核照它核对显示的字。没有这一项的老文档照旧按全名核对。写作规则也写一句。
表名、列名都是虚构的。
"""
from __future__ import annotations

import copy

import pytest

from app.engine.evidence import ENTITY_RULES, StreamRenderer, build_catalog, compose_doc, iter_segments, \
    render_markers, verify_doc

SCHEMA_ART = "6" * 64
SCHEMA = {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": SCHEMA_ART, "source": "plant",
          "tables": {"产量表": ["工厂", "产量"], "产线表": ["工厂", "产线"], "plant_output": ["factory", "qty"]}}
QUERY = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": "a" * 64, "tool": "db_query__plant",
         "source": "plant", "columns": ["工厂", "产量"], "rows": 1, "truncated": False, "schema_artifact": SCHEMA_ART,
         "tables": ["产量表"]}


@pytest.fixture
def catalog():
    return build_catalog(nodes={}, ledger=[QUERY, SCHEMA])


def column_segments(doc):
    return [s for s in iter_segments(doc) if s.get("ref", "").startswith("c:")]


@pytest.mark.parametrize("raw, shown", [
    ("工厂取自[[t:产量表]]的[[c:产量表.工厂]]。", "工厂取自产量表的工厂。"),
    ("工厂取自 产量表 的 [[c:产量表.工厂]]。", "工厂取自 产量表 的 工厂。"),        # 表名写成文字也算
    ("[[t:产量表]]中的[[c:产量表.产量]]按工厂汇总。", "产量表中的产量按工厂汇总。"),
    ("产量表里的 [[c:产量表.产量]] 按工厂汇总。", "产量表里的 产量 按工厂汇总。"),
    ("[[t:plant_output]] 的 [[c:plant_output.qty]]。", "plant_output 的 qty。"),
    ("[[t:PLANT_OUTPUT]] [[c:plant_output.qty]]。", "PLANT_OUTPUT qty。"),            # 只隔空白；英文名不分大小写
])
def test_a_column_right_after_its_table_shows_only_the_column(catalog, raw, shown):
    doc = compose_doc(raw, catalog)
    assert doc["markdown"] == shown and render_markers(raw, catalog) == shown
    [seg] = column_segments(doc)
    assert seg["state"] == "deterministic" and seg["cite"]["short"] is True
    assert seg["cite"]["alias"] == f"c:{seg['ref'][2:]}" or seg["cite"]["alias"].lower() == seg["ref"].lower()
    assert verify_doc(doc, catalog)["ok"], verify_doc(doc, catalog)["violations"]


@pytest.mark.parametrize("raw, shown", [
    ("工厂取自[[t:产线表]]的[[c:产量表.工厂]]。", "工厂取自产线表的产量表.工厂。"),   # 前面是另一张表
    ("[[c:产量表.工厂]]取自产量表。", "产量表.工厂取自产量表。"),                    # 前面没有表名
    ("产量表，[[c:产量表.工厂]]。", "产量表，产量表.工厂。"),                        # 隔着逗号：不是紧挨着
    ("myplant_output 的 [[c:plant_output.qty]]。", "myplant_output 的 plant_output.qty。"),   # 英文名前面还连着字母
])
def test_other_columns_keep_the_full_name(catalog, raw, shown):
    doc = compose_doc(raw, catalog)
    assert doc["markdown"] == shown and render_markers(raw, catalog) == shown
    assert not any(s["cite"].get("short") for s in column_segments(doc))


def test_a_document_written_before_still_verifies_with_the_full_name(catalog):
    """改动前组装的文档：字段照全名显示、引用上没有 short。照全名核对，不判成被改过。"""
    doc = compose_doc("工厂取自[[t:产量表]]的[[c:产量表.工厂]]。", catalog)
    old = copy.deepcopy(doc)
    [seg] = column_segments(old)
    start = seg["span"][0]
    old["markdown"] = old["markdown"].replace("的工厂。", "的产量表.工厂。")
    seg["text"], seg["span"] = "产量表.工厂", [start, start + len("产量表.工厂")]
    seg["cite"].pop("short")
    seg["cite"]["rendered"] = "产量表.工厂"
    tail = next(s for s in iter_segments(old) if s["span"][0] == start + len("工厂"))
    tail["span"] = [start + len("产量表.工厂"), start + len("产量表.工厂") + len(tail["text"])]
    for unit in (u for b in old["blocks"] for u in b["units"]):
        unit["span"] = [unit["span"][0], unit["span"][1] + len("产量表.")]
    assert verify_doc(old, catalog)["ok"], verify_doc(old, catalog)["violations"]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 40])
def test_the_stream_shows_the_same(catalog, size):
    raw = "工厂取自[[t:产量表]]的[[c:产量表.工厂]]，[[t:产线表]]的[[c:产量表.工厂]]不同。"
    renderer = StreamRenderer(catalog)
    out = [renderer.feed(raw[i:i + size]) for i in range(0, len(raw), size)] + [renderer.flush()]
    assert "".join(out) == render_markers(raw, catalog) == compose_doc(raw, catalog)["markdown"] \
        == "工厂取自产量表的工厂，产线表的产量表.工厂不同。"


def test_the_writer_is_told():
    assert "只显示字段名" in ENTITY_RULES
