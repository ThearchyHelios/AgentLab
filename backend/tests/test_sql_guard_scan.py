"""SQL 守卫的单遍扫描：去注释要认得字符串和标识符引号。

以前先去注释、后认字符串：字符串里的 `--`、`/*` 被当成注释删掉，执行的 SQL 和写的不一样
（run_query 执行的正是 guard.check 的返回值）。只改成执行原文又会让守卫看到的和数据库执行的
不一致，所以扫描必须一遍走完，同时认得注释、字符串、标识符。
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from app.data.engine import engines, run_query
from app.data.guard import SqlRejected, check, is_write, split_statements, strip_comments


def rejected(sql: str, *, readonly: bool = True, dialect: str | None = None) -> str | None:
    try:
        check(sql, readonly=readonly, source_name="测试库", dialect=dialect)
        return None
    except SqlRejected as e:
        return str(e)


# ---------------------------------------------------------------- 字符串原样保留


@pytest.mark.parametrize("dialect", [None, "sqlite", "postgresql", "mysql", "oracle"])
@pytest.mark.parametrize("sql", [
    "SELECT '--' AS a",
    "SELECT '/* x */' AS a",
    "SELECT 'a -- b', 'c /* d' AS a",
    'SELECT "col--x" FROM t',
])
def test_comment_markers_inside_quotes_are_kept(sql, dialect):
    assert check(sql, readonly=True, dialect=dialect) == sql
    assert strip_comments(sql, dialect=dialect) == sql


def test_real_comments_are_still_removed():
    assert check("SELECT 1 -- 行注释", readonly=True).strip() == "SELECT 1"
    assert check("SELECT /* 块注释 */ 1", readonly=True).split() == ["SELECT", "1"]
    assert check("SELECT '--' /* 注释 */ AS a", readonly=True).split() == ["SELECT", "'--'", "AS", "a"]
    # 删掉的注释要留一个空格：'x'/**/AS 删成 'x'AS 就粘成一个词了
    assert check("SELECT 'x'/**/AS a", readonly=True).split() == ["SELECT", "'x'", "AS", "a"]


@pytest.mark.parametrize("sql,dialect,kept", [
    ("SELECT 1 /* a /* b */ c */ AS n", "postgresql", "SELECT 1   AS n"),     # PG 的块注释可以嵌套
    ("SELECT E'it\\'s -- x' AS a", "postgresql", None),                     # E'' 里认反斜杠
    ("SELECT [a--b] FROM t", "sqlite", None),                                # SQLite 的方括号标识符
    ("SELECT q'[it's -- x]' AS a FROM dual", "oracle", None),                # Oracle 的 q'[…]'
    ("SELECT a$b$ AS c -- $b$", "postgresql", "SELECT a$b$ AS c"),           # 标识符里的 $ 不开字符串
])
def test_dialect_specific_quoting(sql, dialect, kept):
    assert check(sql, readonly=True, dialect=dialect) == (kept if kept is not None else sql)


def test_doubled_quote_escape_is_one_string():
    sql = "SELECT 'it''s -- fine' AS a"
    assert check(sql, readonly=True) == sql


def test_dollar_quoted_body_is_a_string_on_postgres():
    sql = "SELECT $$ -- not a comment $$ AS a, $tag$ /* nor this */ $tag$ AS b"
    assert check(sql, readonly=True, dialect="postgresql") == sql


def test_mysql_backslash_escape_without_ambiguity_is_allowed():
    sql = r"SELECT 'O\'Brien' AS s"
    assert check(sql, readonly=True, dialect="mysql") == sql


def test_mysql_backslash_that_moves_a_comment_boundary_is_rejected():
    """默认 sql_mode 下是一个字符串，NO_BACKSLASH_ESCAPES 下后半截是注释：两种读法不一致就拒。"""
    reason = rejected(r"SELECT 'a\'b -- c' AS s", dialect="mysql")
    assert reason and "反斜杠" in reason


@pytest.fixture
async def data_source(tmp_path):
    path = tmp_path / "scan.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, note TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)",
                     [(1, "a -- b"), (2, "c /* d */"), (3, "plain")])
    conn.commit()
    conn.close()
    src = SimpleNamespace(
        id=f"scan-{uuid.uuid4().hex[:8]}", kind="sqlite", database=str(path), readonly=True,
        name="测试源", options={}, password=None, username="u", host="h", port=None,
    )
    yield src
    await engines.invalidate(src.id)


async def test_query_with_comment_markers_in_strings_runs_and_returns_right_rows(data_source):
    """字符串里的 -- 和 /* 要原样到达数据库，结果才对。"""
    result = await run_query(data_source, "SELECT id FROM t WHERE note = 'a -- b'")
    assert result.rows == [[1]]
    result = await run_query(data_source, "SELECT id FROM t WHERE note = 'c /* d */' -- 真注释")
    assert result.rows == [[2]]
    result = await run_query(data_source, "SELECT '--' AS a, '/*' AS b")
    assert result.rows == [["--", "/*"]]
    result = await run_query(data_source, "SELECT id FROM t WHERE note LIKE '%--%' /* ' */ ORDER BY id")
    assert result.rows == [[1]]


# ---------------------------------------------------------------- 拦截


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT '--'; DROP TABLE t", None),
    ("SELECT '--'; DROP TABLE t", "sqlite"),
    ("SELECT '/*'; DELETE FROM t", None),
    ("SELECT 1 /* ' */; DELETE FROM t", None),
    ("SELECT 1 -- '\n; DELETE FROM t", None),
    ("SELECT $$--$$; DROP TABLE t", "postgresql"),
    ("SELECT $x$ ; $x$; DELETE FROM t", "postgresql"),
    ("SELECT '\\'; DELETE FROM t", "sqlite"),
    ("SELECT '\\'; DELETE FROM t", "postgresql"),
    ("SELECT '\\\\'; DELETE FROM t", "mysql"),
    ("SELECT 1 # '\n; DELETE FROM t", "mysql"),
    ("SELECT \"--\"; DELETE FROM t", None),
    ("SELECT `--`; DELETE FROM t", None),
])
def test_statement_smuggling_is_rejected(sql, dialect):
    reason = rejected(sql, dialect=dialect)
    assert reason is not None, f"放过了：{sql!r} ({dialect})"
    reason_rw = rejected(sql, readonly=False, dialect=dialect)
    assert reason_rw is not None, f"可写源上放过了：{sql!r} ({dialect})"


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT '--' AS a FROM t WHERE 1=1 /* ' */ AND 0 = 0 UNION SELECT 1 FROM t FOR UPDATE", None),
    ("SELECT $$ ' $$ AS a FROM t FOR UPDATE", "postgresql"),
])
def test_hidden_writes_are_seen_through_quotes_and_comments(sql, dialect):
    reason = rejected(sql, dialect=dialect)
    assert reason and "只读" in reason, f"漏了：{sql!r}"


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT '--'; DELETE FROM t", None),
    ("SELECT 1 /* ' */; DELETE FROM t", "sqlite"),
    ("SELECT $$--$$; DELETE FROM t", "postgresql"),
])
def test_is_write_counts_smuggled_statements(sql, dialect):
    """审批这道门：看不清的一律当写，等人批。"""
    assert is_write(sql, dialect=dialect)


def test_split_statements_respects_quotes_before_comments():
    assert len(split_statements("SELECT '--'; SELECT 2")) == 2
    assert len(split_statements("SELECT '-- ; x'")) == 1
    assert len(split_statements("SELECT $$;$$", dialect="postgresql")) == 1


def test_a_write_word_seen_under_another_mysql_setting_counts():
    """默认设置下是字符串，关了反斜杠转义就成了代码：哪种读法下会写，就按会写处理。"""
    sql = r"SELECT 'can\'t delete' AS a"
    reason = rejected(sql, dialect="mysql")
    assert reason and "只读" in reason and "反斜杠" in reason
    assert is_write(sql, dialect="mysql")
    assert rejected(sql, readonly=False, dialect="mysql") is None
