"""查询结果里文本格的名字（「冲压5线」「KX-205 线」）照抄进报告时，名字里的数字不算裸数字。

部署上撞过：查询结果的产线名里带数字，写作者照着写「冲压5线的产量 [[v:…]]」，名字里的那个 5 被当成没有出处的数字打回
重写，重写还是同一个名字。现在：写作目录的查询结果里、文本格的值（一字不差的子串；值里至少有一个不是数字的字，本身
不是数、也不是带单位的数）出现在正文里时，那一段不查裸数字。只认快照里真有的值，不做模糊匹配：同一个数单独写出来照样
要出处；数值格不受影响。数据是虚构的。
"""
from __future__ import annotations

import pytest

from app.core import artifact_store
from app.engine.evidence import build_catalog, compose_doc, verify_doc

SNAP = {
    "columns": ["产线", "产量", "备注"],
    "column_types": {"产线": "text", "产量": "number", "备注": "text"},
    "rows": [
        ["冲压5线", 1520, "120台"],          # 带单位的数：不算名字
        ["KX-205 线", 830, "45%"],
        ["焊装 2 线", 64, "3.5万元"],
    ],
    "row_count": 3, "truncated": False, "elapsed_ms": 2,
    "sql": "SELECT line AS 产线, SUM(qty) AS 产量, MAX(memo) AS 备注 FROM output GROUP BY line",
    "source": "plant",
}


def _store(snap: dict) -> str:
    text = artifact_store.canonical_json(snap)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


@pytest.fixture
def catalog():
    art = _store(SNAP)
    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": art, "via": "b" * 64, "call_id": "call_1",
               "tool": "db_query__plant", "source": "plant", "columns": SNAP["columns"], "rows": 3, "truncated": False}]
    return build_catalog(nodes={}, ledger=ledger, inputs={})


def _uncited(doc) -> list[str]:
    return [v["text"] for v in doc["violations"] if v["code"] == "uncited_number"]


def test_a_number_inside_a_name_from_the_results_is_allowed(catalog):
    text = "冲压5线的产量是 [[v:Q1.r0.产量]]，KX-205 线是 [[v:Q1.r1.产量]]，焊装 2 线是 [[v:Q1.r2.产量]]。[[see:Q1]]"
    doc = compose_doc(text, catalog)
    assert _uncited(doc) == [], doc["violations"]
    assert verify_doc(doc, catalog)["ok"], "出口复核按同一条规则：组装时放过的，复核也放过"


def test_the_same_number_on_its_own_still_needs_a_citation(catalog):
    doc = compose_doc("冲压5线有 5 个班组，产量 [[v:Q1.r0.产量]]。[[see:Q1]]", catalog)
    assert _uncited(doc) == ["5"]
    # 名字只认一字不差的：快照里没有「冲压15线」，15 照样要出处；名字前后紧挨着数字的也不算那个名字
    assert _uncited(compose_doc("冲压15线的产量 [[v:Q1.r0.产量]]。", catalog)) == ["15"]
    assert _uncited(compose_doc("KX-2050 线的产量 [[v:Q1.r1.产量]]。", catalog)) == ["2050"]
    assert _uncited(compose_doc("焊装 2 线以外还有 2 线。", catalog)) == ["2"]


def test_numbers_and_numbers_with_units_in_text_cells_are_not_names(catalog):
    doc = compose_doc("备注里写着 120台、45%、3.5万元。", catalog)
    assert _uncited(doc) == ["120", "45%", "3.5万"], doc["violations"]


def test_numeric_cells_are_unaffected(catalog):
    # 产量格的 1520 是数值格：照抄成裸数字照样违规
    assert _uncited(compose_doc("冲压5线的产量是 1520。", catalog)) == ["1520"]
