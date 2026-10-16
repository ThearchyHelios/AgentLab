"""有效记录条件按 quote_name 强制加引号（第 1 波留下的一处，next-round 03 C1 ③）。

Oracle 库里本来就是小写的列（建表时写了 "status"）探查时记了 quote_name。拼有效记录条件时只按 SqlDialect.quote（要加才加）
写成不带引号的 status，Oracle 转成大写 STATUS，找的是另一列。存进目录的写法（stored_valid_filter）和剖析核对查询
（check_valid_filter）都要按表结构里的 quote_name 加引号——两处是同一条。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.data import catalog_profile, sqlcheck

META = {"qualified": "ANALYTICS.visits", "schema": "ANALYTICS", "quote_name": True,
        "columns": [{"name": "status", "type": "VARCHAR2(10 CHAR)", "nullable": True, "quote_name": True},
                    {"name": "amount", "type": "NUMBER(12, 2)", "nullable": True}]}


def test_stored_filter_quotes_columns_recorded_with_quote_name():
    parsed = sqlcheck.parse_valid_filter("status <> 'X' AND amount > 0", META, kind="oracle", table="visits")
    assert parsed.problem is None
    assert sqlcheck.stored_valid_filter(parsed, "oracle", META) == "\"status\" <> 'X' AND amount > 0"
    # 不给表结构时照旧按方言要加才加（老的调用方）
    assert sqlcheck.stored_valid_filter(parsed, "oracle") == "status <> 'X' AND amount > 0"


async def test_the_profile_check_quotes_the_same_way(monkeypatch):
    seen: list[str] = []

    async def fake(source, sql, **kw):
        seen.append(sql)
        return SimpleNamespace(rows=[[10, 2]])

    monkeypatch.setattr(catalog_profile, "run_single_query", fake)
    source = SimpleNamespace(kind="oracle", enabled=True, options={catalog_profile.PROFILE_OPTION: {"enabled": True}})
    found = await catalog_profile.check_valid_filter(source, "visits", META, "status <> 'X'")
    assert found.rows == 10 and len(seen) == 1
    sql = seen[0]
    assert "CASE WHEN \"status\" <> 'X' THEN 0" in sql, sql
    assert "SELECT \"status\" FROM \"ANALYTICS\".\"visits\"" in sql, "取的列、表名也按 quote_name 加引号"
