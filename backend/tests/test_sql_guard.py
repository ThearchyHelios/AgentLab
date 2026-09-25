"""SQL 守卫。

这是数据源接入里唯一不能出错的地方：SQL 由模型生成，不是人写的。
守卫放松一分，模型某次抽风就可能打到生产库上——所以这组用例只增不减。
"""
from __future__ import annotations

import pytest

from app.data.guard import SqlRejected, check, first_verb, is_write, split_statements


def rejected(sql: str, *, readonly: bool = True) -> str | None:
    try:
        check(sql, readonly=readonly, source_name="测试库")
        return None
    except SqlRejected as e:
        return str(e)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders",
    "  select 1  ",
    "WITH t AS (SELECT 1) SELECT * FROM t",
    "(SELECT a FROM b)",
    "SHOW TABLES",
    "DESCRIBE orders",
    "EXPLAIN SELECT * FROM orders",
    "/* 注释 */ SELECT 1",
    "-- 行注释\nSELECT 2",
    "SELECT 1;",                       # 尾分号不算第二条语句
])
def test_readonly_allows_queries(sql):
    assert rejected(sql) is None, f"不该拒绝：{sql!r}"


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET x=1",
    "DELETE FROM orders",
    "INSERT INTO orders VALUES(1)",
    "MERGE INTO a USING b ON (1=1)",   # 白名单式判定才拦得住的方言私货
    "CALL some_proc()",
    "REPLACE INTO t VALUES(1)",
])
def test_readonly_blocks_writes(sql):
    reason = rejected(sql)
    assert reason and "只读" in reason


@pytest.mark.parametrize("sql", [
    # 首关键字是查询，正文在写。只看首关键字的话全都能混过去——
    # 第一条在 SQLite 上实测过：守卫放行，数据真的删了
    "WITH k AS (SELECT 1) DELETE FROM t WHERE x < 3",
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",      # PG 的数据修改 CTE
    "WITH k AS (SELECT 1) UPDATE t SET x = 1",
    "WITH k AS (SELECT 1) INSERT INTO t SELECT * FROM k",
    "SELECT * INTO backup FROM orders",                           # PG：建一张新表
    "SELECT * FROM orders INTO OUTFILE '/tmp/o.csv'",             # MySQL：写服务器上的文件
    "EXPLAIN ANALYZE DELETE FROM orders",                         # PG：ANALYZE 会真的执行
    "SELECT * FROM orders FOR UPDATE",                            # 行锁
    "SELECT set_config('default_transaction_read_only', 'off', false)",  # 关掉连接层只读
    "SELECT \"set_config\"('default_transaction_read_only', 'off', false)",  # 函数名加引号
    "SELECT pg_catalog.set_config('default_transaction_read_only', 'off', false)",
    "SELECT * FROM dblink('dbname=x', 'DELETE FROM t RETURNING *') AS r(x int)",  # 另开连接
    "SELECT nextval('order_seq')",
])
def test_readonly_blocks_writes_hidden_behind_a_query_verb(sql):
    reason = rejected(sql)
    assert reason and "只读" in reason, f"漏了：{sql!r}"


@pytest.mark.parametrize("sql", [
    "PRAGMA user_version = 7",
    "PRAGMA user_version(7)",            # 函数写法同样是赋值
    "PRAGMA query_only = 0",             # 想先把只读关掉
    "PRAGMA main.writable_schema = 1",
    "PRAGMA optimize",                   # 会跑 ANALYZE，写统计表
    "PRAGMA wal_checkpoint(TRUNCATE)",
])
def test_readonly_blocks_pragmas_that_write(sql):
    reason = rejected(sql)
    assert reason and "只读" in reason, f"漏了：{sql!r}"


@pytest.mark.parametrize("sql", [
    "PRAGMA table_info(orders)",
    "PRAGMA main.table_info('orders')",
    "PRAGMA index_list(orders)",
    "PRAGMA foreign_key_list(orders)",
    "PRAGMA user_version",
    "SELECT updated_at, deleted_flag, created_by FROM orders",   # 标识符里带关键字不算
    "SELECT 'please delete me' AS note",                         # 字符串里的不算
    'SELECT "update" FROM t',                                    # 引号里的标识符不算
    "SELECT REPLACE(name, 'a', 'b'), INSERT('abc', 1, 1, 'x'), TRUNCATE(price, 2) FROM t",
    "SHOW CREATE TABLE orders",                                  # MySQL 看建表语句
    "WITH t AS (SELECT 1 AS into_count) SELECT * FROM t",
])
def test_readonly_does_not_misfire_on_harmless_queries(sql):
    """正文扫描收紧了，误杀也得守住：同名函数、标识符、字符串都不是在写。"""
    assert rejected(sql) is None, f"误杀了：{sql!r}"


def test_is_write_sees_writes_hidden_behind_a_query_verb():
    """可写源靠 is_write 决定要不要人工审批——WITH 开头的写不能被当成查询放过去。"""
    assert is_write("WITH k AS (SELECT 1) DELETE FROM t")
    assert is_write("SELECT * INTO backup FROM t")
    assert is_write("UPDATE t SET x = 1")
    assert not is_write("SELECT * FROM t")
    assert not is_write("WITH k AS (SELECT 1) SELECT * FROM k")


@pytest.mark.parametrize("sql", [
    "DROP TABLE orders",
    "TRUNCATE TABLE orders",
    "ALTER TABLE t ADD c INT",
    "GRANT ALL ON db.* TO u",
    "CREATE TABLE t(a INT)",
])
def test_destructive_blocked_even_on_writable_source(sql):
    """结构变更和权限操作在可写源上也不放行——不可逆的事不交给模型发起。"""
    reason = rejected(sql, readonly=False)
    assert reason and "任何数据源" in reason


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE users",
    "SELECT 1;DROP TABLE users;",
    "SELECT 1 /*x*/; DELETE FROM t",
])
def test_multi_statement_blocked(sql):
    """分号拼接必须在到达驱动之前断掉，不赌驱动默认不跑第二条。"""
    reason = rejected(sql)
    assert reason and "一条语句" in reason


@pytest.mark.parametrize("sql", [
    "SELECT ';' AS x",
    "SELECT 'a;b' FROM t WHERE c = 'd;e'",
    "SELECT '它说：''你好'';' AS s",      # 连续两个引号是转义，不是字符串结束
])
def test_semicolon_inside_string_is_not_a_second_statement(sql):
    """误杀比漏杀更让人无从下手：字符串字面量里的分号不算语句分隔。"""
    assert rejected(sql) is None, f"误杀了：{sql!r}"


def test_writable_source_allows_dml():
    for sql in ("UPDATE orders SET x=1", "INSERT INTO t VALUES(1)", "DELETE FROM t WHERE id=1"):
        assert rejected(sql, readonly=False) is None


@pytest.mark.parametrize("sql", ["", "   ", "-- 只有注释", "!!!"])
def test_empty_and_malformed_rejected(sql):
    assert rejected(sql) is not None


def test_check_returns_statement_without_trailing_semicolon():
    """调用方必须执行返回值而不是原始输入，否则守卫等于没做。"""
    assert check("SELECT 1;", readonly=True) == "SELECT 1"


def test_first_verb_sees_through_comments_and_parens():
    assert first_verb("/* x */ (SELECT 1)") == "select"
    assert first_verb("-- c\nDROP TABLE t") == "drop"


def test_split_keeps_quoted_semicolons_together():
    assert len(split_statements("SELECT ';'")) == 1
    assert len(split_statements("SELECT 1; SELECT 2")) == 2
