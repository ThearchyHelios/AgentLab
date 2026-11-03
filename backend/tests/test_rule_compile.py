"""规则 DSL 编译（app/data/rule_compile.py、POST /api/ledger/rules/compile）：结构化条件 → 只读 SQL，fixture 验证。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.data.rule_compile import RuleInvalid, compile_rule, run_fixtures
from app.main import app

RULE = {"inputs": {"status": "status", "amount": "amount"},
        "all": [{"input": "status", "op": "eq", "value": "paid"},
                {"input": "amount", "op": "gte", "value": 50}],
        "then": 1, "else": 0}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def test_compile_rule_is_a_select_with_a_case():
    sql = compile_rule(RULE, "orders")
    assert sql.lower().startswith("select") and "case when" in sql.lower()
    assert "from orders" in sql.lower()
    assert "insert" not in sql.lower() and "update" not in sql.lower() and "delete" not in sql.lower()


def test_fixtures_run_against_the_compiled_sql():
    checks = run_fixtures(RULE, "orders", [
        {"rows": [{"status": "paid", "amount": 100}], "expected": 1},
        {"rows": [{"status": "paid", "amount": 10}], "expected": 0},
        {"rows": [{"status": "open", "amount": 100}], "expected": 0},
    ])
    assert [c["ok"] for c in checks] == [True, True, True]


@pytest.mark.parametrize("rule", [
    {"inputs": {}, "all": [{"input": "x", "op": "eq", "value": 1}]},
    {"inputs": {"x": "x"}, "all": [{"input": "x", "op": "like", "value": 1}]},
    {"inputs": {"x": "x"}, "all": [{"input": "y", "op": "eq", "value": 1}]},
])
def test_bad_rules_are_refused(rule):
    with pytest.raises(RuleInvalid):
        compile_rule(rule, "orders")


async def test_the_compile_endpoint_returns_sql_and_fixture_results(client):
    res = await client.post("/api/ledger/rules/compile", json={
        "rule": RULE, "table": "orders",
        "fixtures": [{"rows": [{"status": "paid", "amount": 100}], "expected": 1},
                     {"rows": [{"status": "paid", "amount": 10}], "expected": 0}],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sql"].lower().startswith("select") and [c["ok"] for c in body["fixtures"]] == [True, True]
    bad = await client.post("/api/ledger/rules/compile", json={"rule": {"inputs": {}}, "table": "orders"})
    assert bad.status_code == 422
