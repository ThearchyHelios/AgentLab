"""报告里标识列的数不分千位（engine/cell_format.plain_columns，单元格显示规则 3 版）。

部署上撞过：产线编码这类数值型的编码列在报告的表格里写成「1,203」，读的人以为是个量。现在按序看三样信号：查询当时冻结的
数据目录给源列标的度量类型（identifier / status / attribute 不分，flow / stock / ratio 分）、源列名像编码编号（CODE、
_ID、ID、NO、编码、编号、代码）、是分组键而不是聚合；SUM、COUNT、AVG 出来的是量，照旧分千位；判断不了的照旧分千位。
真有格子因此改了写法的文档记 cell_display: 3、引用上记 plain；老文档按记下的版本复核，照旧分千位。数据是虚构的。
"""
from __future__ import annotations

import pytest

from app.core import artifact_store
from app.engine import evidence as ev
from app.engine.cell_format import code_like, plain_columns
from app.engine.evidence import build_catalog, compose_doc, verify_doc, writer_catalog

SCHEMA = {"source": "plant", "tables": {"plant_output": {"columns": []}},
          "catalog": {"plant_output": {"version": 2, "notes": {"columns": {
              "batch": {"measure": {"value": "identifier", "source": "human", "status": "confirmed"}},
              "capacity": {"measure": {"value": "stock", "source": "human", "status": "confirmed"}}}}}}}
SQL = ("SELECT p.STATION_CODE AS 产线, YEAR(p.made_on) AS 年份, p.capacity AS 产能, SUM(p.qty) AS 产量, "
       "COUNT(*) AS 工单数, MAX(p.batch) AS 批次 FROM plant_output p "
       "GROUP BY p.STATION_CODE, YEAR(p.made_on), p.capacity")
COLUMNS = ["产线", "年份", "产能", "产量", "工单数", "批次"]


def _store(obj: dict) -> str:
    text = artifact_store.canonical_json(obj)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


def _snap(schema_artifact: str) -> dict:
    return {"columns": COLUMNS, "column_types": {c: "number" for c in COLUMNS},
            "rows": [[1203, 2031, 7001, 15200, 1450, 88001], [1207, 2031, 6400, 9300, 1210, 88002]],
            "row_count": 2, "truncated": False, "elapsed_ms": 2, "sql": SQL, "source": "plant",
            "schema_artifact": schema_artifact}


AMOUNTS = {"columns": ["金额"], "column_types": {"金额": "number"}, "rows": [[12345]], "row_count": 1,
           "truncated": False, "elapsed_ms": 1, "sql": "SELECT o.amount * 2 AS 金额 FROM orders o", "source": "plant"}


def _query(artifact: str, columns: list[str], call: str) -> dict:
    return {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": artifact, "via": "b" * 64, "call_id": call,
            "tool": "db_query__plant", "source": "plant", "columns": columns, "rows": 2, "truncated": False}


@pytest.fixture
def catalog():
    snap = _store(_snap(_store(SCHEMA)))
    ledger = [_query(snap, COLUMNS, "call_1"), _query(_store(AMOUNTS), ["金额"], "call_2")]
    return build_catalog(nodes={}, ledger=ledger, inputs={})


TEXT = ("[[v:Q1.r0.年份]] 年 [[v:Q1.r0.产线]] 线产量 [[v:Q1.r0.产量]]，工单 [[v:Q1.r0.工单数]] 张，产能 [[v:Q1.r0.产能]]，"
        "批次 [[v:Q1.r0.批次]]；另一项金额 [[v:Q2.r0.金额]]。[[see:Q1,Q2]]")


def _shown(doc) -> dict[str, str]:
    return {s["ref"]: s["text"] for b in doc["blocks"] for u in b["units"] for s in u["segments"]
            if s.get("ref", "").startswith("v:")}


def test_identifier_columns_read_without_separators(catalog):
    doc = compose_doc(TEXT, catalog)
    assert doc["violations"] == [], doc["violations"]
    shown = _shown(doc)
    assert shown["v:Q1.r0.产线"] == "1203"            # 源列名像编码（STATION_CODE）
    assert shown["v:Q1.r0.年份"] == "2031"            # 分组键
    assert shown["v:Q1.r0.批次"] == "88001"           # 目录标的是标识（MAX 取的是那一列本身的值）
    assert shown["v:Q1.r0.产能"] == "7,001"           # 分组键，但目录标的是存量：是量，照旧分千位
    assert shown["v:Q1.r0.产量"] == "15,200"          # SUM：量
    assert shown["v:Q1.r0.工单数"] == "1,450"         # COUNT：量
    assert shown["v:Q2.r0.金额"] == "12,345"          # 判断不了：照旧分千位
    assert doc["cell_display"] == 3
    plain = {s["ref"] for b in doc["blocks"] for u in b["units"] for s in u["segments"]
             if (s.get("cite") or {}).get("plain")}
    assert plain == {"v:Q1.r0.产线", "v:Q1.r0.年份", "v:Q1.r0.批次"}
    assert verify_doc(doc, catalog)["ok"]


def test_tables_and_the_writer_catalog_read_the_same_way(catalog):
    doc = compose_doc("[[table:Q1 cols=产线,产量 rows=0-1]]", catalog)
    assert doc["violations"] == [] and "| 1203 | 15,200 |" in doc["markdown"] and "| 1207 | 9,300 |" in doc["markdown"]
    text = writer_catalog(catalog).text
    assert "产线=1203" in text and "产量=15,200" in text, text


def test_documents_composed_before_still_verify_with_separators(catalog, monkeypatch):
    monkeypatch.setattr(ev, "CELL_DISPLAY", 2)
    old = compose_doc(TEXT, catalog)
    monkeypatch.undo()
    assert _shown(old)["v:Q1.r0.产线"] == "1,203" and "cell_display" not in old
    assert verify_doc(old, catalog)["ok"], "按文档记下的版本复核：照旧分千位，不判成被改过"


def test_a_document_edited_back_to_separators_is_caught(catalog):
    doc = compose_doc("产线 [[v:Q1.r0.产线]]。", catalog)
    seg = next(s for b in doc["blocks"] for u in b["units"] for s in u["segments"] if s.get("ref") == "v:Q1.r0.产线")
    start = seg["span"][0]
    doc["markdown"] = doc["markdown"].replace("1203", "1,203")
    seg["text"], seg["span"] = "1,203", [start, start + 5]
    assert "render_mismatch" in {v["code"] for v in verify_doc(doc, catalog)["violations"]}


@pytest.mark.parametrize("name, yes", [
    ("STATION_CODE", True), ("order_id", True), ("ID", True), ("user_no", True), ("orderId", True), ("stationCode", True),
    ("产线编码", True), ("工单编号", True), ("物料代码", True),
    ("qty", False), ("piano", False), ("video", False), ("income", False), ("产量", False),
])
def test_code_like_names(name, yes):
    assert code_like(name) is yes


def test_unparseable_sql_falls_back_to_the_column_name():
    snap = {"columns": ["station_id", "total"], "sql": "this is not sql", "rows": []}
    assert plain_columns(snap) == frozenset({"station_id"})
