"""静态地认（engine/lineage.static_stops）和运行时同一套：转抄同一张图里另一张口径卡的指标时跨卡接着认。

受管级发布检查（governance）和应用检查（apps/lint）都用它：被抄的那个指标追到了查询格，转抄的这一项就算追到；被抄的
指标停在表达式整形节点，转抄的这一项停在被抄的那张卡；被抄的卡按 caliber_from 固定在别处版本的，静态看不到那一版的
图，照旧停在那张卡；转抄成环的到层数上限算没追到。
"""
from __future__ import annotations

from app.engine.lineage import Stop, static_stops


def _nodes(**extra):
    nodes = {
        "q": {"type": "tool", "config": {"tool": "db_query__shop", "args": {"sql": "SELECT 1 AS gmv"}}},
        "base": {"type": "metrics", "config": {"metrics": [
            {"id": "gmv", "expression": "cell(nodes.q, 0, 'gmv')"}]}},
    }
    nodes.update(extra)
    return nodes


def test_a_copied_metric_that_reaches_a_query_cell_is_traced():
    nodes = _nodes(copy={"type": "metrics", "config": {"metrics": [
        {"id": "x", "expression": "nodes.base.metrics[0].value"}]}})
    assert static_stops(nodes, "nodes.base.metrics[0].value") == []
    # 转抄的转抄也接着认
    nodes["copy2"] = {"type": "metrics", "config": {"metrics": [{"id": "y", "expression": "nodes.copy.metrics[0].value"}]}}
    assert static_stops(nodes, "nodes.copy2.metrics[0].value") == []


def test_a_copied_metric_whose_source_is_untraced_stops_at_that_card():
    nodes = _nodes(shape={"type": "transform", "config": {"expression": "1"}},
                   loose={"type": "metrics", "config": {"metrics": [{"id": "z", "expression": "nodes.shape.value"}]}})
    assert static_stops(nodes, "nodes.loose.metrics[0].value") == [Stop("nodes.loose.metrics[0].value", "loose")]


def test_a_card_pinned_elsewhere_still_stops_and_out_of_range_still_stops():
    nodes = _nodes(pinned={"type": "metrics", "config": {"caliber_from": {"workflow_id": "w", "version": 1},
                                                          "metrics": [{"id": "gmv", "expression": "cell(nodes.q, 0, 'gmv')"}]}})
    assert static_stops(nodes, "nodes.pinned.metrics[0].value") == [Stop("nodes.pinned.metrics[0].value", "pinned")]
    assert static_stops(_nodes(), "nodes.base.metrics[3].value") == [Stop("nodes.base.metrics[3].value", "base")]


def test_a_copy_cycle_gives_up_at_the_depth_limit():
    nodes = {"a": {"type": "metrics", "config": {"metrics": [{"id": "x", "expression": "nodes.b.metrics[0].value"}]}},
             "b": {"type": "metrics", "config": {"metrics": [{"id": "y", "expression": "nodes.a.metrics[0].value"}]}}}
    assert static_stops(nodes, "nodes.a.metrics[0].value") == [Stop("nodes.a.metrics[0].value", "a")]
