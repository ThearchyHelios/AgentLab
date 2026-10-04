"""报告里单元格的 ISO 日期时间显示成「2026-06-30 08:21:39」，不再是「2026-06-30T08:21:39」。

只改显示：快照里的值、引用记下的 value、eid、证据下钻都不变。复核要按文档生成时的规则：老文档里存的是带 T 的字，
拿新规则重新渲染会对不上，被当成文档被改过（render_mismatch）——所以新文档在顶层记 cell_display: 2（只在真有日期
时间被改写时记，没有的文档和以前一字不差），没有这个字段的老文档按 1 版（原样）复核。期 4 的金样
（test_provenance_unchanged）照旧逐字不变。

夹具合成：门店、卡券。
"""
from __future__ import annotations

import pytest

from app.core.artifact_store import canonical_json
from app.engine import evidence as ev
from app.engine.evidence import (
    CELL_DISPLAY,
    StreamRenderer,
    build_catalog,
    cell_eid,
    compose_doc,
    doc_cell_display,
    iter_segments,
    verify_doc,
    writer_catalog,
)

ART = "d" * 64
SNAP = {"columns": ["store_name", "redeemed_at", "biz_date", "amount", "note"],
        "column_types": {"store_name": "text", "redeemed_at": "datetime", "biz_date": "date", "amount": "number",
                         "note": "text"},
        "rows": [["门店01", "2026-06-30T08:21:39", "2026-06-30", 120, "T 开头的备注"],
                 ["门店02", "2026-06-29T23:05:00.250000+08:00", "2026-06-29", 80, "2026-06-29T10:00:00Z"]],
        "truncated": False}
STORE = {ART: SNAP}


def load(artifact):
    return STORE.get(artifact)


@pytest.fixture
def catalog():
    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": ART, "tool": "db_query__shop",
               "columns": SNAP["columns"], "rows": 2, "truncated": False}]
    return build_catalog(nodes={}, ledger=ledger)


def cells(doc):
    return [s for s in iter_segments(doc) if s.get("ref", "").startswith("v:")]


def test_iso_datetimes_read_with_a_space(catalog):
    doc = compose_doc("最近一笔核销在 [[v:Q1.r0.redeemed_at]]，门店 [[v:Q1.r0.store_name]]。[[see:Q1]]", catalog, loader=load)
    [when, store] = cells(doc)
    assert when["text"] == "2026-06-30 08:21:39" and "2026-06-30 08:21:39" in doc["markdown"]
    # 只改显示：引用记下的值、证据标识照旧
    assert when["cite"]["value"] == "2026-06-30T08:21:39"
    assert when["cite"]["eid"] == cell_eid(ART, 0, "redeemed_at")
    assert store["text"] == "门店01"
    assert doc["cell_display"] == CELL_DISPLAY == 2 and doc_cell_display(doc) == 2
    assert '"cell_display":2' in canonical_json(doc), "在文档顶层，参与内容哈希"
    assert verify_doc(doc, catalog, loader=load)["ok"]


def test_fractions_and_offsets_are_kept_and_dates_are_untouched(catalog):
    doc = compose_doc("时间 [[v:Q1.r1.redeemed_at]]，日期 [[v:Q1.r1.biz_date]]，备注 [[v:Q1.r1.note]]。", catalog,
                      loader=load)
    assert [s["text"] for s in cells(doc)] == ["2026-06-29 23:05:00.250000+08:00", "2026-06-29", "2026-06-29 10:00:00Z"]
    assert verify_doc(doc, catalog, loader=load)["ok"]


def test_tables_read_the_same_way(catalog):
    doc = compose_doc("[[table:Q1 cols=store_name,redeemed_at]]", catalog, loader=load)
    assert "| 门店01 | 2026-06-30 08:21:39 |" in doc["markdown"]
    assert "T08:21:39" not in doc["markdown"]
    assert verify_doc(doc, catalog, loader=load)["ok"]


def test_documents_without_datetimes_are_unchanged(catalog):
    doc = compose_doc("门店 [[v:Q1.r0.store_name]]，金额 [[v:Q1.r0.amount]]。", catalog, loader=load)
    assert "cell_display" not in doc, "没有被改写的日期时间：文档和以前一字不差"
    assert doc_cell_display(doc) == 1


def test_old_documents_still_verify_as_they_were(catalog, monkeypatch):
    """改动前组装的文档：存的是带 T 的字、没有 cell_display。按 1 版复核，不报 render_mismatch。"""
    monkeypatch.setattr(ev, "CELL_DISPLAY", 1)
    old = compose_doc("最近一笔核销在 [[v:Q1.r0.redeemed_at]]。", catalog, loader=load)
    monkeypatch.undo()
    assert "cell_display" not in old and "2026-06-30T08:21:39" in old["markdown"]
    checked = verify_doc(old, catalog, loader=load)
    assert checked["ok"], checked["violations"]


def test_a_document_edited_back_is_caught(catalog):
    doc = compose_doc("最近一笔核销在 [[v:Q1.r0.redeemed_at]]。", catalog, loader=load)
    [seg] = cells(doc)
    start = seg["span"][0]
    doc["markdown"] = doc["markdown"].replace("2026-06-30 08:21:39", "2026-06-30T08:21:39")
    seg["text"] = "2026-06-30T08:21:39"
    seg["span"] = [start, start + len(seg["text"])]
    codes = {v["code"] for v in verify_doc(doc, catalog, loader=load)["violations"]}
    assert "render_mismatch" in codes


def test_an_unknown_display_version_is_reported(catalog):
    doc = compose_doc("最近一笔核销在 [[v:Q1.r0.redeemed_at]]。", catalog, loader=load)
    doc["cell_display"] = 9
    assert "bad_schema" in {v["code"] for v in verify_doc(doc, catalog, loader=load)["violations"]}


def test_the_writer_and_the_stream_see_what_the_report_will_show(catalog):
    text = writer_catalog(catalog, loader=load).text
    assert "redeemed_at=2026-06-30 08:21:39" in text and "T08:21:39" not in text
    stream = StreamRenderer(catalog, loader=load)
    shown = stream.feed("最近一笔在 [[v:Q1.r0.redeemed_at]]。") + stream.flush()
    assert "2026-06-30 08:21:39" in shown
