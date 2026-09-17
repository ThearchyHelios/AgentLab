"""数据源连接层与工具层。

用真实的 SQLite 库跑——SQLAlchemy 统一了方言，SQLite 走的是和 MySQL/Oracle
同一套代码路径，所以这里验证的不只是 SQLite。各驱动特有的差异（Oracle 的
service_name、MySQL 的 charset）在 URL 构造那组用例里覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.data.engine import build_url, run_query, test_connection
from app.data.guard import QueryLimits, SqlRejected
from app.data.introspect import describe_table, introspect, summary, table_names


@pytest.fixture
def shop_db(tmp_path):
    """一个最小但真实的业务库：地区 / 商品 / 订单。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY, region_id INTEGER NOT NULL,
          qty INTEGER NOT NULL, amount REAL NOT NULL, ordered_at TEXT NOT NULL
        );
    """)
    db.executemany("INSERT INTO regions(id,name) VALUES(?,?)",
                   [(1, "华东"), (2, "华北")])
    db.executemany(
        "INSERT INTO orders(id,region_id,qty,amount,ordered_at) VALUES(?,?,?,?,?)",
        [(i, 1 + i % 2, i, i * 10.5, "2026-06-15") for i in range(1, 101)],
    )
    db.commit()
    db.close()
    return str(path)


@pytest.fixture
def source(shop_db):
    return SimpleNamespace(
        id="test-src", name="sales", kind="sqlite", database=shop_db,
        host=None, port=None, username=None, password=None, options={},
        readonly=True, description="测试销售库", schema_cache={},
    )


async def test_connection_and_introspect(source):
    assert (await test_connection(source))["ok"]

    source.schema_cache = await introspect(source)
    assert set(table_names(source)) == {"regions", "orders"}

    orders = source.schema_cache["tables"]["orders"]
    assert [c["name"] for c in orders["columns"]] == [
        "id", "region_id", "qty", "amount", "ordered_at"
    ]
    assert orders["primary_key"] == ["id"]


async def test_summary_is_compact_enough_for_a_prompt(source):
    source.schema_cache = await introspect(source)
    text = summary(source)
    assert "orders" in text and "amount" in text
    # 摘要要能塞进 system prompt——超出这个量级就该改成按需探查
    assert len(text) < 2000


async def test_summary_says_so_when_never_introspected(source):
    """没探查过就明说，别让 Copilot 对着空气编表名。"""
    assert "尚未探查" in summary(source)


async def test_describe_table_lists_unknown_table_options(source):
    source.schema_cache = await introspect(source)
    out = describe_table(source, "没有这张表")
    assert "现有的表" in out and "orders" in out


async def test_query_returns_rows(source):
    result = await run_query(source, "SELECT COUNT(*) AS c FROM orders")
    assert result.rows[0][0] == 100
    assert result.columns == ["c"]


async def test_readonly_guard_applies_on_real_connection(source):
    """守卫不能只在单测里生效——真实连接路径上也必须拦住。"""
    for sql in ("UPDATE orders SET qty=0", "DROP TABLE orders", "SELECT 1; DROP TABLE orders"):
        with pytest.raises(SqlRejected):
            await run_query(source, sql)
    # 数据确实没被动过
    assert (await run_query(source, "SELECT COUNT(*) FROM orders")).rows[0][0] == 100


async def test_result_set_is_capped(source):
    """一条 SELECT * 不该把内存和模型上下文一起打爆。"""
    result = await run_query(source, "SELECT * FROM orders", limits=QueryLimits(max_rows=10))
    assert result.row_count == 10
    assert result.truncated is True


async def test_rows_are_json_serializable(source):
    """Decimal / date / bytes 在真实业务数据里遍地都是，不处理等于不可用。"""
    result = await run_query(source, "SELECT amount, ordered_at FROM orders LIMIT 3")
    json.dumps(result.to_payload())  # 不抛就算过


# --------------------------------------------------------------------------
# URL 构造：各驱动的方言差异
# --------------------------------------------------------------------------

def _src(kind, **kw):
    base = dict(id="x", name="n", readonly=True, host="db", port=None,
                database=None, username="u", password="p", options={})
    base.update(kw)
    return SimpleNamespace(kind=kind, **base)


@pytest.mark.parametrize("kind,expect", [
    ("mysql", "mysql+aiomysql://"),
    ("postgres", "postgresql+asyncpg://"),
    ("oracle", "oracle+oracledb://"),
])
def test_url_uses_async_drivers(kind, expect):
    opts = {"service_name": "ORCL"} if kind == "oracle" else {}
    assert build_url(_src(kind, database="d", options=opts)).startswith(expect)


def test_password_is_masked_by_default():
    """密码不该出现在日志、事件流或任何返回给前端的地方。"""
    url = build_url(_src("mysql", database="d"))
    assert "***" in url and ":p@" not in url


def test_oracle_requires_service_name_or_sid():
    """Oracle 最常见的配错点，要给明确提示而不是让驱动报晦涩的 ORA 错误。"""
    with pytest.raises(ValueError, match="service_name"):
        build_url(_src("oracle"))


def test_oracle_accepts_database_as_service_name():
    assert "service_name=ORCLPDB1" in build_url(_src("oracle", database="ORCLPDB1"))


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="不支持"):
        build_url(_src("mongodb"))
