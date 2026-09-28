"""报告撰写节点的类型登记，以及搭图时就能认出来的两种错接。

报告节点从哪几张口径卡取指标（metrics_from）、出口契约核对哪个报告节点
（report_from），都是按节点 id 写死的引用。指到一个不是口径卡的节点、或者指到
下游，运行时只会得到一个空目录、一份无从核对的报告——而这在画图时就看得出来。
"""
from __future__ import annotations

import pytest

from app.core.events import EventType
from app.engine.schema import TYPE_LABEL, GraphSpec, NodeType, type_label, validate_graph
from app.engine.variables import analyze


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def spec(nodes, edges):
    return GraphSpec.model_validate({
        "nodes": nodes, "edges": [{"source": a, "target": b} for a, b in edges]})


def issues(graph: GraphSpec, field: str) -> list[tuple[str, str]]:
    return [(i.level, i.message) for i in validate_graph(graph).issues if i.field == field]


def test_report_type_is_registered():
    assert NodeType("report") is NodeType.REPORT
    assert TYPE_LABEL["report"] == "报告撰写" == type_label(NodeType.REPORT)
    assert EventType("report.checked") is EventType.REPORT_CHECKED


def report_graph(metrics_from=None, *, report_from="write", extra=(), edges=None, **contract):
    cfg = {"instructions": "写周报", "model": "mock"}
    if metrics_from is not None:
        cfg["metrics_from"] = metrics_from
    nodes = [
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 1}", assign_to="kpi"),
        node("caliber", "metrics", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
        node("write", "report", **cfg),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"metrics_from": ["caliber"], "report_from": report_from, **contract}),
        *extra,
    ]
    return spec(nodes, edges or [("start", "fetch"), ("fetch", "caliber"), ("caliber", "write"),
                                 ("write", "out")])


def test_a_well_formed_report_graph_has_no_new_errors():
    graph = report_graph(["caliber"])
    assert issues(graph, "metrics_from") == []
    assert issues(graph, "contract.report_from") == []
    assert issues(report_graph("caliber"), "metrics_from") == []   # 单个 id 写成字符串也认


@pytest.mark.parametrize("metrics_from, words", [
    (["nope"], ["nope", "不存在"]),
    (["fetch"], ["fetch", "不是「口径卡」"]),
])
def test_metrics_from_must_name_a_caliber(metrics_from, words):
    found = issues(report_graph(metrics_from), "metrics_from")
    assert len(found) == 1 and found[0][0] == "error", found
    assert all(w in found[0][1] for w in words), found


def test_metrics_from_must_be_upstream():
    """口径卡接在报告后面：报告跑的时候它还没算，目录里一个指标都没有。"""
    graph = report_graph(["late"], extra=[node("late", "metrics", metrics=[{"id": "x", "expression": "1"}])],
                         edges=[("start", "fetch"), ("fetch", "caliber"), ("caliber", "write"),
                                ("write", "late"), ("late", "out")])
    found = issues(graph, "metrics_from")
    assert len(found) == 1 and found[0][0] == "error" and "上游" in found[0][1], found


@pytest.mark.parametrize("report_from, words", [
    ("nope", ["nope", "不存在"]),
    ("caliber", ["caliber", "不是「报告撰写」"]),
])
def test_report_from_must_name_a_report_node(report_from, words):
    found = issues(report_graph(["caliber"], report_from=report_from), "contract.report_from")
    assert len(found) == 1 and found[0][0] == "error", found
    assert all(w in found[0][1] for w in words), found


def test_report_from_must_be_upstream_of_the_output():
    graph = report_graph(["caliber"], report_from="side", extra=[node("side", "report", instructions="x")],
                         edges=[("start", "fetch"), ("fetch", "caliber"), ("caliber", "write"),
                                ("write", "out"), ("caliber", "side")])
    found = issues(graph, "contract.report_from")
    assert len(found) == 1 and found[0][0] == "error" and "上游" in found[0][1], found


def test_old_contracts_are_untouched():
    """没写 report_from 的旧契约：不多一条报错，缺 metrics_from 照旧报。"""
    graph = spec([node("start", "input"),
                  node("out", "output", contract={"narrative": "x"})], [("start", "out")])
    assert issues(graph, "contract.report_from") == []
    assert [lv for lv, _ in issues(graph, "contract.metrics_from")] == ["error"]


# --------------------------------------------------------------------------
# 变量分析扫口径卡的表达式
# --------------------------------------------------------------------------


def test_caliber_expressions_count_as_references():
    """口径卡对上游的引用以前在变量表里看不见：vars.kpi 被报成「产出了但没有任何地方引用」，
    画布上也画不出口径卡依赖谁。"""
    graph = spec([
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 1, 'prev': 1}", assign_to="kpi"),
        node("caliber", "metrics", metrics=[
            {"id": "gmv", "expression": "vars.kpi.gmv"},
            {"id": "wow", "expression": "(vars.kpi.gmv - vars.kpi.prev) / vars.kpi.prev"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.caliber.text }}"}]),
    ], [("start", "fetch"), ("fetch", "caliber"), ("caliber", "out")])
    report = analyze(graph)
    kpi = next(v for v in report.variables if v.path == "vars.kpi")
    assert {(r.node_id, r.field) for r in kpi.refs} == {("caliber", "metrics[0].expression"),
                                                        ("caliber", "metrics[1].expression")}
    assert not [i for i in report.issues if i.path == "vars.kpi"], report.issues
