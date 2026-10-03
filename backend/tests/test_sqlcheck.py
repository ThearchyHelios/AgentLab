"""基于数据目录的 SQL 检查（data/sqlcheck.py）：七条规则的正例、反例，级别随依据的状态变化，以及解析不了的写法。

用合成的景区业务库（tests/fixtures/catalog/scenic.py）和它的一份目录（scenic_notes.py）。检查只做有把握的判断：
解析不了、遇到不支持的写法、列对不上表，一律跳过不报——它的 error 会把图打回去改、挡住受管级别的发布、让出具降档，
误报的代价比漏报大。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data.engine import engines
from app.data.introspect import introspect
from app.data.sqlcheck import SqlChecker, check_sql, dialect_of
from tests.fixtures.catalog import scenic_notes

_CACHE: dict[str, dict] = {}


@pytest.fixture
async def cache(scenic_db):
    """景区库的表结构缓存（真实探查一次：带外键和唯一约束，关系现推要用）。每个测试进程只探查一次。"""
    if scenic_db not in _CACHE:
        source = SimpleNamespace(id="sqlcheck-scenic", name="scenic", kind="sqlite", database=scenic_db, host=None,
                                 port=None, username=None, password=None, options={}, readonly=True, description="",
                                 schema_cache={}, origin="manual")
        try:
            _CACHE[scenic_db] = await introspect(source)
        finally:
            await engines.invalidate(source.id)
    return _CACHE[scenic_db]


def run(cache, sql, status="confirmed", override=None, kind="sqlite"):
    checker = SqlChecker(kind=kind, schema_cache=cache, notes=scenic_notes.notes(status, override),
                         source_name="scenic")
    return checker.check(sql)


def found(cache, sql, status="confirmed", override=None, kind="sqlite"):
    """[(code, level, table, column)]，按检查给出的顺序。"""
    return [(c.code, c.level, c.table, c.column) for c in run(cache, sql, status, override, kind)]


def codes(cache, sql, status="confirmed", override=None):
    return {c.code for c in run(cache, sql, status, override)}


def test_dialects():
    assert dialect_of("sqlite") == "sqlite"
    assert dialect_of("postgres") == dialect_of("postgresql") == "postgres"
    assert dialect_of("mysql") == dialect_of("mariadb") == "mysql"
    assert dialect_of("oracle") == "oracle"
    assert dialect_of("mssql") is None and dialect_of(None) is None


# --------------------------------------------------------------------------
# 复杂写法：照样分析得出，或者跳过；不报错、不误报
# --------------------------------------------------------------------------

CLEAN_COMPLEX = [
    # CTE + 窗口函数 + 子查询
    """WITH daily AS (
         SELECT substr(o.ordered_at, 1, 10) AS d, SUM(i.amount) AS amt
         FROM orders o JOIN order_items i ON i.order_id = o.id
         WHERE o.status = 1 GROUP BY substr(o.ordered_at, 1, 10))
       SELECT d, amt, SUM(amt) OVER (ORDER BY d) AS running,
              (SELECT COUNT(*) FROM visits v WHERE v.status = 1) AS visits
       FROM daily ORDER BY d""",
    # UNION
    "SELECT 'web' AS ch, SUM(total_amount) FROM orders WHERE status = 1 AND channel_id = 1 "
    "UNION ALL SELECT 'box', SUM(total_amount) FROM orders WHERE status = 1 AND channel_id = 3",
    # 相关子查询、EXISTS
    "SELECT o.id FROM orders o WHERE o.status = 1 AND EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id)",
    # 引号括起来的驼峰标识符
    'SELECT t.tag_name, COUNT(*) FROM member_tag_links l JOIN "memberTags" t ON t.id = l."memberTagId" '
    "GROUP BY t.tag_name",
]


@pytest.mark.parametrize("sql", CLEAN_COMPLEX)
async def test_complex_but_clean_sql_reports_nothing(cache, sql):
    assert run(cache, sql) == []


@pytest.mark.parametrize("sql", [
    "SELECT FROM WHERE",
    "SELEC total_amount FROM orders",
    "SELECT SUM(o.total_amount FROM orders o JOIN order_items i ON i.order_id = o.id",
    "SELECT 1; SELECT SUM(on_hand) FROM inventory_snapshots",
    "DELETE FROM visits",
    "",
    "   ",
    # 方言特性：SQLite 读不了 Oracle 的 q 引号
    "SELECT SUM(on_hand) FROM inventory_snapshots WHERE q'[x]' = 'x'",
    # 模板里的控制语句：拼出来是什么样说不清
    "SELECT SUM(on_hand) FROM inventory_snapshots {% if x %}WHERE snapshot_date = '2026-09-30'{% endif %}",
    # 结构里没有的表、别名对不上的列
    "SELECT SUM(x.on_hand) FROM inventory_snapshots s",
    "SELECT SUM(on_hand) FROM not_a_table",
])
async def test_unparseable_or_unsupported_sql_is_skipped(cache, sql):
    assert run(cache, sql) == []


async def test_no_catalog_means_no_checks(cache):
    sql = "SELECT SUM(o.total_amount) FROM orders o JOIN order_items i ON i.id = o.id"
    assert SqlChecker(kind="sqlite", schema_cache=cache, notes={}).check(sql) == []
    assert check_sql(sql, kind="sqlite", schema_cache=cache, notes=None) == []
    # 没探查过结构：对不上表，一样不判
    assert check_sql(sql, kind="sqlite", schema_cache={}, notes=scenic_notes.notes()) == []
