"""SQL 守卫。

这是数据源接入里唯一不能出错的地方：SQL 由模型生成，不是人写的。
守卫放松一分，模型某次抽风就可能打到生产库上——所以这组用例只增不减。
"""
from __future__ import annotations

import pytest

from app.data.guard import SqlRejected, check, first_verb, split_statements


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
