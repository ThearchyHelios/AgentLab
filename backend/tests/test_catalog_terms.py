"""服务端上界面的数据目录说法和界面一致（阶段 4A 术语对齐）。

界面以 frontend/src/lib/terms.ts 的 CATALOG_TERMS 一组为准：表类型的 fact 写「明细表」，度量类型的 flow 写「可累加」，
关联关系两端写「本表字段 → 目标表的目标字段」。校验报错（CatalogInvalid，接口回 422 时原样上界面）以前写的是
「事实表」「流量」「被指向的表」，和界面对不上。给模型看的提示词（render_table_notes、起草提示）照旧，不在这里管。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.data import catalog
from app.data.catalog import make_item

TERMS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "terms.ts"


def _labels(name: str) -> list[str]:
    """terms.ts 里一张对照表的中文值，按书写顺序。"""
    text = TERMS.read_text(encoding="utf-8")
    block = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\n\}}", text, re.S)
    assert block, f"terms.ts 里没有 {name}"
    return re.findall(r":\s*'([^']+)'", block.group(1))


def _choices(labels: list[str]) -> str:
    return f"应为{'、'.join(labels[:-1])}或{labels[-1]}之一"


def test_kind_and_measure_hints_follow_the_ui_labels():
    kinds = _labels("CATALOG_KIND_LABEL")
    measures = _labels("CATALOG_MEASURE_LABEL")
    assert kinds[0] == "明细表" and measures[0] == "可累加"
    [kind] = catalog.validate_notes({"kind": make_item("event", "human")})
    assert kind == f"表的表类型{_choices(kinds)}"
    [measure] = catalog.validate_notes({"columns": {"amount": {"measure": make_item("sum", "human")}}})
    assert measure == f"列 amount 的度量类型{_choices(measures)}"
    for problem in (kind, measure):
        assert "事实表" not in problem and "流量" not in problem


def test_relation_problems_use_the_ui_words():
    rel = {"id": "r1", "columns": ["a", "b"], "to_table": "", "to_columns": ["id"], "cardinality": None,
           "coverage": None, "source": "human", "status": "confirmed"}
    problems = catalog.validate_notes({"relations": [rel]})
    assert "关联关系 r1 缺少目标表" in problems
    rel = {**rel, "to_table": "parks", "to_columns": "id"}
    assert "关联关系 r1 的目标字段应为列名的列表" in catalog.validate_notes({"relations": [rel]})
    rel = {**rel, "columns": "a", "to_columns": ["id"]}
    assert "关联关系 r1 的本表字段应为列名的列表" in catalog.validate_notes({"relations": [rel]})
    rel = {**rel, "columns": ["a", "b"]}
    assert "关联关系 r1 两端的字段数不一致" in catalog.validate_notes({"relations": [rel]})
    text = "；".join(catalog.validate_notes({"relations": [{**rel, "to_table": ""}]}))
    assert "被指向" not in text and "本表列" not in text


async def test_api_422_uses_the_ui_labels():
    """接口把校验报错原样交给界面：整份提交和提案保存都是这一句。"""
    from app.api.catalog import _invalid

    try:
        catalog.apply_human_edit({}, {"kind": {"value": "event"}}, table="visits")
    except catalog.CatalogInvalid as e:
        detail = _invalid(e).detail
    assert "明细表" in detail and "事实表" not in detail
    # 给模型看的渲染照旧：模型认得「事实表」「流量，可跨期加总」
    text = catalog.render_table_notes({"kind": make_item("fact", "human"),
                                       "columns": {"n": {"measure": make_item("flow", "human")}}})
    assert "表类型：事实表" in text and "流量，可跨期加总" in text
