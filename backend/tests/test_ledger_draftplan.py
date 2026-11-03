"""口径一次起草的纯逻辑（app/ledger/draftplan.py）。

- SQL 只收单条只读 SELECT：多语句、注释后藏的第二条、写操作、目录外的表一律拒；
- 指标表达式必须是 cell(nodes.fetch, 0, '列名')；引用核不上的整条丢弃；
- build_graph 两次调用逐字一致，且能过 GraphSpec / validate_graph；
- plan 注入假模型（不发真实请求），材料里有表目录和原文。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.engine.schema import GraphSpec, validate_graph
from app.ledger import draftplan

CHUNK = "销售额按已支付订单的支付时间归期统计，退款订单不计入销售额。"
CHUNKS = {"c1": {"ordinal": 1, "content": CHUNK, "document_id": "d1"}}
TABLES = {"orders", "customers"}

METRIC = {
    "id": "m1",
    "name": "销售额",
    "synonyms": ["gmv"],
    "unit": "元",
    "decimals": 2,
    "expression": "cell(nodes.fetch, 0, 'gmv')",
    "definition": {"text": "已支付订单的销售额", "scope": "全部", "exclusions": [], "owner": "运营",
                   "effective_from": "2026-01-01",
                   "citations": [{"chunk": "c1", "quote": "销售额按已支付订单的支付时间归期统计"}]},
    "open_questions": [],
}


def raw(**extra):
    body = {
        "entry_fields": [{"name": "start", "type": "string", "required": True, "default": "2026-09-01",
                          "description": "开始日期"}],
        "sql": "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '{{ input.start }}'",
        "metrics": [dict(METRIC)],
        "open_questions": [],
    }
    body.update(extra)
    return body


def sql_problem(sql: str) -> str | None:
    plan, problems = draftplan.normalize_plan(raw(sql=sql), CHUNKS, allowed_tables=TABLES)
    assert plan["sql"] == ""
    reasons = [p["reason"] for p in problems if p["name"] == "sql"]
    assert reasons, problems
    return reasons[0]


# ==========================================================================
# normalize_plan：SQL 的硬规则
# ==========================================================================

def test_a_read_only_select_over_the_catalog_is_kept():
    plan, problems = draftplan.normalize_plan(raw(), CHUNKS, allowed_tables=TABLES)
    assert plan["sql"].endswith("'{{ input.start }}'")
    assert [m["id"] for m in plan["metrics"]] == ["m1"]
    assert plan["entry_fields"][0]["name"] == "start"
    assert not [p for p in problems if p["name"] == "sql"]


def test_multiple_statements_and_a_hidden_second_statement_are_refused():
    assert "只能是一条语句" in sql_problem(
        "SELECT SUM(amount) AS gmv FROM orders; DROP TABLE orders")
    # 注释后藏的第二条也解析成第二条语句
    assert "只能是一条语句" in sql_problem(
        "SELECT SUM(amount) AS gmv FROM orders; -- 收尾\nDELETE FROM orders")


def test_writes_and_ddl_are_refused():
    assert "只读 SELECT" in sql_problem("DELETE FROM orders")
    assert "只读 SELECT" in sql_problem("UPDATE orders SET amount = 0")
    assert "只读 SELECT" in sql_problem("DROP TABLE orders")


def test_tables_outside_the_catalog_are_refused():
    reason = sql_problem("SELECT * FROM secrets")
    assert "secrets" in reason and "数据目录" in reason
    # CTE 名字不是表；真正的表没在目录里照样拒
    reason = sql_problem("WITH x AS (SELECT * FROM secrets) SELECT * FROM x")
    assert "secrets" in reason


def test_a_cte_over_a_catalog_table_is_allowed():
    plan, problems = draftplan.normalize_plan(
        raw(sql="WITH paid AS (SELECT amount FROM orders) SELECT SUM(amount) AS gmv FROM paid"),
        CHUNKS, allowed_tables=TABLES)
    assert plan["sql"].startswith("WITH paid")
    assert not [p for p in problems if p["name"] == "sql"]


def test_an_undeclared_template_field_invalidates_the_sql():
    plan, problems = draftplan.normalize_plan(
        raw(sql="SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '{{ input.nope }}'"),
        CHUNKS, allowed_tables=TABLES)
    assert plan["sql"] == ""
    assert any("nope" in p["reason"] for p in problems)


# ==========================================================================
# normalize_plan：指标
# ==========================================================================

def test_a_bad_expression_drops_only_that_metric():
    plan, problems = draftplan.normalize_plan(
        raw(metrics=[{**METRIC, "expression": "SUM(amount)"}, {**dict(METRIC), "id": "m2"}]),
        CHUNKS, allowed_tables=TABLES)
    assert [m["id"] for m in plan["metrics"]] == ["m2"]
    assert any(p["name"] == "销售额" and "cell(nodes.fetch, 0" in p["reason"] for p in problems)


def test_a_metric_whose_quotes_do_not_match_the_source_is_dropped():
    plan, problems = draftplan.normalize_plan(
        raw(metrics=[{**METRIC, "definition": {**METRIC["definition"],
                                               "citations": [{"chunk": "c1", "quote": "原文里没有这句"}]}}]),
        CHUNKS, allowed_tables=TABLES)
    assert plan["metrics"] == []
    assert any(p["name"] == "销售额" and "引用" in p["reason"] for p in problems)
    assert any(p["name"] == "metrics" for p in problems)


def test_metric_ids_are_unique_and_entry_fields_are_normalized():
    plan, _problems = draftplan.normalize_plan(
        raw(entry_fields=[{"name": "start", "type": "date", "required": 1, "default": "2026-09-01"}],
            metrics=[dict(METRIC), {**dict(METRIC), "name": "订单数"}]),
        CHUNKS, allowed_tables=TABLES)
    assert [m["id"] for m in plan["metrics"]] == ["m1", "m2"]
    assert plan["entry_fields"][0]["type"] == "string"
    assert plan["entry_fields"][0]["required"] is True


# ==========================================================================
# build_graph
# ==========================================================================

def test_build_graph_is_deterministic_and_passes_validation():
    plan, _ = draftplan.normalize_plan(raw(), CHUNKS, allowed_tables=TABLES)
    plan["datasource"] = "shop"
    first = draftplan.build_graph(plan, name="销售额口径", question="上周销售额")
    second = draftplan.build_graph(plan, name="销售额口径", question="上周销售额")
    assert first == second
    assert [node["id"] for node in first["nodes"]] == ["start", "fetch", "card", "write", "out"]
    fetch = next(node for node in first["nodes"] if node["id"] == "fetch")
    assert fetch["data"]["config"] == {"tool": "db_query__shop", "args": {"sql": plan["sql"]}}
    card = next(node for node in first["nodes"] if node["id"] == "card")
    assert card["data"]["config"]["metrics"][0]["id"] == "m1"
    contract = next(node for node in first["nodes"] if node["id"] == "out")["data"]["config"]["contract"]
    assert contract["metrics_from"] == "card" and contract["required"] == ["m1"]
    report = next(node for node in first["nodes"] if node["id"] == "write")["data"]["config"]
    assert report["numbers"] == "strict" and report["metrics_from"] == ["card"]
    result = validate_graph(GraphSpec.model_validate(first))
    assert result.ok, [issue.message for issue in result.issues if issue.level == "error"]


# ==========================================================================
# plan：注入假模型
# ==========================================================================

class FakeModel:
    model_name = "fake-plan-model"

    def __init__(self, text: str) -> None:
        self.text = text
        self.messages: list = []

    async def ainvoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.text)


async def test_plan_hands_the_model_the_table_catalog_and_the_source_text():
    model = FakeModel(json.dumps({**raw(), "model_id": "ignored"}, ensure_ascii=False))
    result = await draftplan.plan(
        None, docs=[{"document_id": "d1", "title": "销售口径.md"}], chunks=CHUNKS,
        guidance="只看已支付", datasource={"name": "shop", "type": "sqlite"},
        tables_text="表 orders（订单）\n  amount  金额", model=model)
    assert result["model_id"] == "fake-plan-model"
    assert result["sql"].startswith("SELECT SUM(amount)")
    assert result["metrics"][0]["name"] == "销售额"
    human = model.messages[-1].content
    assert "表 orders" in human and "amount  金额" in human
    assert CHUNK in human and "只看已支付" in human


async def test_plan_refuses_when_no_draft_model_is_available(monkeypatch):
    from app.data import recipe_ai
    from app.ledger import docdraft

    async def resolve(_session):
        return None, "", SimpleNamespace(available=False, reason="还没有接入可用的起草模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", resolve)
    with pytest.raises(docdraft.DraftUnavailable, match="起草模型"):
        await draftplan.plan(None, docs=[], chunks={})
