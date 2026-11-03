"""口径取数 SQL 的书写 lint：取整、补 0 交给口径卡，画布提示、发布门禁拦截。

- 口径卡上游取数 SQL 里的 ROUND(...)、COALESCE / IFNULL / NVL(..., 0)：evidence_issues 在取数节点上给 warning
- 受管发布门禁报 error，已发布档只 warning：和别的受管要求同一个口径
- 解析不了的 SQL 不抛异常；定义来自上游的卡（caliber_from）不在本图，不查
"""
from __future__ import annotations

import pytest

from app.data.caliber_lint import issues_for_sql
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.schema import GraphSpec, evidence_issues

ROUND = "caliber.round_in_source"
ZERO = "caliber.zero_default"
CELL = "cell(nodes.fetch, 0, 'gmv')"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def graph(sql: str, *, metrics=None, **card) -> dict:
    """input → 取数 → 口径卡。默认两个指标都读同一个取数节点，用来验证同一节点只报一次。"""
    default = [{"id": "gmv", "name": "销售额", "unit": "元", "expression": CELL},
               {"id": "orders", "name": "订单数", "unit": "单", "expression": CELL}]
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": sql}),
        node("card", "metrics", "周报口径卡", metrics=metrics or default, **card),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]}
                                      for a, b in zip(nodes, nodes[1:])]}


def gate(graph_dict: dict, code: str, level: str = "governed") -> list:
    return [i for i in lint_for_publish(GraphSpec.model_validate(graph_dict), level=level).issues
            if i.code == code]


# --------------------------------------------------------------------------
# SQL 级检查
# --------------------------------------------------------------------------


def test_round_in_source_is_detected():
    found = issues_for_sql("SELECT ROUND(SUM(amount), 2) AS gmv FROM orders")
    assert [i["code"] for i in found] == [ROUND]
    assert found[0]["excerpt"] == "ROUND(SUM(amount), 2)"
    assert "永远认不成等价" in found[0]["message"]


def test_zero_default_needs_a_literal_zero_last_argument():
    found = issues_for_sql("SELECT COALESCE(SUM(amount), 0) AS gmv FROM orders")
    assert [i["code"] for i in found] == [ZERO]
    assert "没有数据" in found[0]["message"]
    assert [i["code"] for i in issues_for_sql("SELECT COALESCE(SUM(amount), -0) AS gmv FROM orders")] == [ZERO]
    assert issues_for_sql("SELECT COALESCE(SUM(amount), SUM(refund)) AS gmv FROM orders") == []
    assert issues_for_sql("SELECT COALESCE(SUM(amount), 1) AS gmv FROM orders") == []


@pytest.mark.parametrize("sql", ["SELECT IFNULL(SUM(amount), 0) AS gmv FROM orders",
                                 "SELECT NVL(SUM(amount), 0) AS gmv FROM orders"])
def test_ifnull_and_nvl_count_as_zero_default(sql):
    assert [i["code"] for i in issues_for_sql(sql)] == [ZERO]


def test_unparsable_sql_is_ignored():
    assert issues_for_sql("SELECT (((") == []
    assert issues_for_sql("") == []
    assert issues_for_sql(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 画布校验：落在取数节点上，一个节点只报一次
# --------------------------------------------------------------------------


def test_evidence_warns_on_the_fetch_node_once():
    spec = GraphSpec.model_validate(graph("SELECT ROUND(SUM(amount), 2) AS gmv FROM orders"))
    found = [i for i in evidence_issues(spec) if i.code == ROUND]
    assert len(found) == 1
    assert found[0].level == "warning"
    assert found[0].node_id == "fetch"
    assert found[0].field == "args.sql"


def test_evidence_skips_cards_from_upstream():
    ref = {"workflow_id": "lib", "workflow_version": 1, "node_id": "card"}
    spec = GraphSpec.model_validate(
        graph("SELECT COALESCE(SUM(amount), 0) AS gmv FROM orders", caliber_from=ref))
    assert [i for i in evidence_issues(spec) if i.code in (ROUND, ZERO)] == []


# --------------------------------------------------------------------------
# 发布门禁：已发布只警告，受管挡住
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code,sql", [
    (ROUND, "SELECT ROUND(SUM(amount), 2) AS gmv FROM orders"),
    (ZERO, "SELECT COALESCE(SUM(amount), 0) AS gmv FROM orders"),
])
def test_governed_blocks_and_published_warns(code, sql):
    graph_dict = graph(sql)
    assert [i.level for i in gate(graph_dict, code, "governed")] == ["error"]
    assert [i.level for i in gate(graph_dict, code, "published")] == ["warning"]


def test_publish_issues_lists_the_problem_once():
    """validate 和门禁说的是同一处：发布时不能一正一反各来一条。"""
    spec = GraphSpec.model_validate(graph("SELECT ROUND(SUM(amount), 2) AS gmv FROM orders"))
    found = [i for i in publish_issues(spec, level="published") if i.code == ROUND]
    assert len(found) == 1
    assert found[0].level == "warning"
