"""按 SQL 认取自遮罩列的结果列（engine/masking.py 的 _derived_hidden）：整行、函数参数里的 *、列名表把遮罩列带进别的列名
（复核第 3 轮）。

Postgres 里不带表名、名字是 FROM 里一个来源的列引用指它的整行（row_to_json(s)、json_agg(t)、SELECT s、s::text），函数
参数里的 s.*（to_json(s.*)、concat_ws(',', s.*)、ROW(s.*)）也把整行交给函数；数据源里的表可以带列名表（FROM sales AS
s(i, r, p)，只改前几列的名字），TABLE sales 等于 SELECT * FROM sales；Oracle 的 JSON_OBJECT(*) 把整行拼成 JSON，UNPIVOT、
PIVOT、MATCH_RECOGNIZE 把遮罩列的值转进它们自己起名的列。这些写法以前都认不出：遮罩列的值落进了别的列名，原样交给核查
agent、落进快照。现在整行、函数参数里的 * 按 * 展开的来源认（数据源里的表一律算带出了遮罩列），列名表按位置对上（表结构
查得到这张表的只遮号码改了名字的那一列），TABLE x 当 SELECT * FROM x，PIVOT 这类读到遮罩列的整份遮。

这里没有真的 Postgres、Oracle：结果按它们交回的样子造（行转成 JSON、记录转成文字），只看遮完的结果里还有没有原值。数据
都是虚构的：区域、手机号、姓名、金额。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.engine import masking

MASKED_WORD = "已遮罩"
MASKED = ["buyer_phone", "buyer_name"]
SECRETS = ("1390000000", "张伟", "李娜")
LAYOUT = ["id", "region", "buyer_phone", "buyer_name", "amount", "note"]
#: 探查得到的表结构（没设 schema，探查的是当前库）
SCHEMA = {"tables": {"sales": {"qualified": "sales", "schema": None, "columns": [{"name": c} for c in LAYOUT]}}}
ROWS = [[1, "华东", "13900000001", "张伟", 1200, "改址"], [2, "华南", "13900000002", "李娜", 300, "回访"]]


def as_json(row: list) -> str:
    return json.dumps(dict(zip(LAYOUT, row)), ensure_ascii=False)


def as_record(row: list) -> str:
    return "(" + ",".join(str(v) for v in row) + ")"


def hidden_of(sql: str, columns: list[str], kind: str = "postgres", schema: dict | None = None) -> set[str]:
    snapshot = {"source": "shop", "sql": sql, "columns": columns, "rows": []}
    return masking._derived_hidden(snapshot, set(MASKED), kind, masking._schema_columns(schema))[0]


def masked(sql: str, columns: list[str], rows: list[list], kind: str = "postgres",
           schema: dict | None = SCHEMA) -> masking.QueryMask:
    payload = {"columns": columns, "rows": rows, "sql": sql}
    return masking.mask_query_result(payload, MASKED, kind, schema=schema)


def leaks(done: masking.QueryMask) -> bool:
    text = json.dumps(done.payload["rows"], ensure_ascii=False)
    return any(s in text for s in SECRETS)


# --------------------------------------------------------------------------
# 整行引用、函数参数里的 *
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, cells", [
    pytest.param("SELECT row_to_json(s) AS j, s.id AS n FROM sales s", [as_json(r) for r in ROWS], id="row_to_json(s)"),
    pytest.param("SELECT to_jsonb(s) AS j, s.id AS n FROM sales s", [as_json(r) for r in ROWS], id="to_jsonb(s)"),
    pytest.param("SELECT s AS j, s.id AS n FROM sales s", [as_record(r) for r in ROWS], id="SELECT s"),
    pytest.param("SELECT s::text AS j, s.id AS n FROM sales s", [as_record(r) for r in ROWS], id="s::text"),
    pytest.param("SELECT to_json(s.*) AS j, s.id AS n FROM sales s", [as_json(r) for r in ROWS], id="to_json(s.*)"),
    pytest.param("SELECT concat_ws(',', s.*) AS j, s.id AS n FROM sales s",
                 [",".join(str(v) for v in r) for r in ROWS], id="concat_ws(',', s.*)"),
    pytest.param("SELECT ROW(s.*) AS j, s.id AS n FROM sales s", [as_record(r) for r in ROWS], id="ROW(s.*)"),
    pytest.param("SELECT (s).buyer_phone AS j, s.id AS n FROM sales s", [r[2] for r in ROWS], id="(s).buyer_phone"),
    pytest.param("SELECT (s.*).buyer_name AS j, s.id AS n FROM sales s", [r[3] for r in ROWS], id="(s.*).buyer_name"),
    pytest.param("SELECT (SELECT row_to_json(s)) AS j, s.id AS n FROM sales s", [as_json(r) for r in ROWS],
                 id="相关子查询里的整行"),
    pytest.param("SELECT kv.value AS j, s.id AS n FROM sales s, LATERAL jsonb_each_text(to_jsonb(s)) AS kv "
                 "WHERE kv.key = 'buyer_phone'", [r[2] for r in ROWS], id="LATERAL 拆开整行"),
])
def test_a_whole_row_carrying_masked_columns_is_masked(sql, cells):
    """整行、函数参数里的 s.* 把遮罩列的值交给了别的列：这一列遮，编号照常。"""
    done = masked(sql, ["j", "n"], [[cell, row[0]] for cell, row in zip(cells, ROWS)])
    assert done.columns == ["j"] and not leaks(done), done.payload["rows"]
    assert [row[1] for row in done.payload["rows"]] == [1, 2]


@pytest.mark.parametrize("sql", [
    "SELECT json_agg(t) AS j FROM (SELECT id, buyer_phone FROM sales) t",
    "SELECT row_to_json(t) AS j FROM (SELECT buyer_name AS 名 FROM sales) t",
    "WITH t AS (SELECT id, buyer_phone AS p FROM sales) SELECT json_agg(t) AS j FROM t",
])
def test_a_whole_row_of_a_derived_source_is_read_through_its_columns(sql):
    """整行取自子查询、CTE：按它选出的列认，里面有遮罩列（改了名字的也算）就算。"""
    assert "j" in hidden_of(sql, ["j"])


@pytest.mark.parametrize("sql", [
    "SELECT (SELECT j FROM (SELECT row_to_json(s) AS j) z) AS j FROM sales s",
    "SELECT (SELECT j FROM (SELECT json_agg(t) AS j) z) AS j FROM (SELECT id, buyer_phone FROM sales) t",
    "SELECT (WITH w AS (SELECT row_to_json(s) AS j) SELECT j FROM w) AS j FROM sales s",
    "SELECT (SELECT j FROM (SELECT to_json(s.*) AS j) z) AS j FROM sales s",
])
def test_a_whole_row_of_an_outer_query_is_found_from_a_nested_derived_table(sql):
    """FROM 里的子查询、CTE 看不见同一层 FROM 里的别的来源，却看得见再外层的查询：写在里面的整行引用、s.* 指外层的来源，
    照样认得出。"""
    assert "j" in hidden_of(sql, ["j"])


@pytest.mark.parametrize("sql", [
    "SELECT json_agg(t) AS j FROM (SELECT id, region, amount FROM sales) t",
    "SELECT count(s.*) AS j FROM sales s",
    "SELECT COUNT(*) AS j FROM sales s",
    "SELECT region AS j, SUM(amount) AS total FROM sales s GROUP BY region",
    "SELECT EXISTS(SELECT * FROM sales s2 WHERE s2.id = s.id) AS j FROM sales s",
])
def test_rows_and_stars_that_bring_no_masked_values_stay_visible(sql):
    """整行取自没有遮罩列的子查询、count(s.*) 只数个数：不算。"""
    assert hidden_of(sql, ["j", "total"]) == set(MASKED)


def test_a_column_named_like_its_table_is_a_column_where_the_schema_says_so():
    """Postgres 先按列认：表里有一列和表同名（visits 表的 visits 列），SELECT visits 取的是这一列，不是整行。表结构里查得到
    这一列就不按整行算；SQLite、MySQL 没有整行引用，不看表结构也不按整行算。"""
    schema = {"tables": {"visits": {"columns": [{"name": c} for c in ("id", "visits", "buyer_phone")]}}}
    sql = "SELECT visits AS j FROM visits"
    assert hidden_of(sql, ["j"], schema=schema) == set(MASKED)
    assert hidden_of(sql, ["j"], kind="sqlite") == set(MASKED)
    assert "j" in hidden_of(sql, ["j"]), "表结构里查不到这一列：可能是整行，按整行算（多遮）"


def test_a_star_inside_a_function_reads_every_source_it_expands():
    """Oracle 的 JSON_OBJECT(*) 把整行拼成 JSON：同 to_json(s.*)。"""
    done = masked("SELECT JSON_OBJECT(*) AS j FROM sales", ["j"], [[as_json(r)] for r in ROWS], kind="oracle")
    assert done.columns == ["j"] and not leaks(done)


# --------------------------------------------------------------------------
# 列名表、TABLE x
# --------------------------------------------------------------------------


def test_a_table_with_a_column_list_masks_the_renamed_masked_column():
    """数据源里的表带列名表：号码改叫 p。列名表写全了的，按表结构对上位置，只遮 p 和姓名改叫的 q，编号照常；Postgres 的
    列名表只写前几列（后面的照原名）时，列名表和表的列数对不上，改了名的几列整段遮（多遮），照原名的姓名按名字遮。"""
    full = "SELECT p AS j, i AS n FROM sales AS s(i, r, p, q, a, o)"
    done = masked(full, ["j", "n"], [[r[2], r[0]] for r in ROWS])
    assert done.columns == ["j"] and not leaks(done)
    assert set(masked(full, ["j", "n"], [[r[2], r[0]] for r in ROWS], schema=None).columns) == {"j", "n"}, \
        "表结构里查不到这张表：说不清哪几列改了名，都遮"
    short = masked("SELECT p AS j, i AS n FROM sales AS s(i, r, p)", ["j", "n"], [[r[2], r[0]] for r in ROWS])
    assert set(short.columns) == {"j", "n"} and not leaks(short)
    star = masked("SELECT * FROM sales AS s(i, r, p)", ["i", "r", "p", *LAYOUT[3:]], [list(r) for r in ROWS])
    assert star.columns == ["i", "r", "p", "buyer_name"] and not leaks(star)


def test_a_column_list_on_a_cte_reference_renames_it_too():
    """引用 CTE 时再写列名表（FROM t AS x(p, a)）：同样按位置对上。"""
    sql = "WITH t AS (SELECT buyer_phone, amount FROM sales) SELECT p AS j, a AS total FROM t AS x(p, a)"
    assert hidden_of(sql, ["j", "total"]) >= {"j"}
    assert "total" not in hidden_of(sql, ["j", "total"])


@pytest.mark.parametrize("sql, columns, shown", [
    pytest.param("WITH t(a, b, c, d, e, f) AS (TABLE sales) SELECT c AS j, a AS n FROM t", ["j", "n"], {"n"},
                 id="CTE 里的 TABLE"),
    pytest.param("SELECT * FROM (TABLE sales) AS t(a, b, c, d, e, f)", ["a", "b", "c", "d", "e", "f"], {"a", "b", "e"},
                 id="子查询里的 TABLE"),
    pytest.param("WITH t(a, b, c) AS (TABLE sales) SELECT c AS j FROM t", ["j"], set(), id="列名表只写前几列"),
])
def test_a_table_statement_is_read_as_select_star(sql, columns, shown):
    """TABLE sales（Postgres、MySQL）就是 SELECT * FROM sales：列名表照样按位置对上，号码改叫 c 的那一列遮。"""
    hidden = hidden_of(sql, columns, schema=SCHEMA)
    assert {"j", "c"} & hidden and not shown & hidden, hidden


@pytest.mark.parametrize("sql", [
    "SELECT 0 AS a, '' AS b, '' AS c, '' AS d, 0 AS e, '' AS f WHERE false UNION ALL TABLE sales",
    "WITH t(a, b, c) AS (TABLE archive.sales) SELECT c AS j FROM t",
    "WITH t(a, b, c) AS (TABLE sales ORDER BY id LIMIT 1) SELECT c AS j FROM t",
    "WITH t(a, b, c, d, e, f) AS (SELECT * FROM sales) SELECT c AS j FROM t WHERE (",
])
def test_sql_that_cannot_be_read_but_has_a_star_or_a_table_statement_hides_everything(sql):
    """解析器认不出的写法（UNION 后一支的 TABLE、带库名或 ORDER BY 的 TABLE）：SQL 里没写遮罩列，可 TABLE 语句、* 会把它
    带进别的列名，整份快照的列都算，同解析不了又写着遮罩列的。"""
    columns = ["a", "b", "c", "j"]
    assert hidden_of(sql, columns) >= set(columns)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS j FROM sales WHERE (",
    "SELECT amount * 2 AS j FROM sales WHERE (",
])
def test_sql_that_cannot_be_read_with_only_a_count_or_a_product_shows_the_rest(sql):
    """只有 COUNT(*)、乘号：不会把遮罩列带出来，照旧只遮遮罩列本身。"""
    assert hidden_of(sql, ["j"]) == set(MASKED)


# --------------------------------------------------------------------------
# UNPIVOT、PIVOT、MATCH_RECOGNIZE
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, columns", [
    pytest.param("SELECT id, val FROM sales UNPIVOT (val FOR col IN (buyer_phone, region))", ["id", "val"],
                 id="UNPIVOT"),
    pytest.param("SELECT * FROM (SELECT region, buyer_phone FROM sales) PIVOT (MAX(buyer_phone) FOR region IN "
                 "('华东' AS e, '华南' AS s))", ["e", "s"], id="PIVOT"),
    pytest.param("SELECT * FROM sales MATCH_RECOGNIZE (PARTITION BY region ORDER BY id MEASURES buyer_phone AS x "
                 "ONE ROW PER MATCH PATTERN (a) DEFINE a AS amount > 0)", ["region", "x"], id="MATCH_RECOGNIZE"),
])
def test_pivots_reading_masked_columns_hide_everything(sql, columns):
    """UNPIVOT 的值列、PIVOT 按 IN 里的值起的列、MATCH_RECOGNIZE 的 MEASURES 装着遮罩列的值：出的是哪几列不好认，整份遮。"""
    assert hidden_of(sql, columns, kind="oracle") >= {c.lower() for c in columns}


@pytest.mark.parametrize("sql, columns", [
    ("SELECT id, val FROM sales UNPIVOT (val FOR col IN (region, note))", ["id", "val"]),
    ("SELECT * FROM (SELECT region, buyer_phone FROM sales) PIVOT (COUNT(buyer_phone) FOR region IN "
     "('华东' AS e, '华南' AS s))", ["e", "s"]),
])
def test_pivots_that_bring_no_masked_values_stay_visible(sql, columns):
    assert hidden_of(sql, columns, kind="oracle") == set(MASKED)


# --------------------------------------------------------------------------
# 选出项套了括号的 *
# --------------------------------------------------------------------------


def test_a_star_in_parentheses_is_counted_as_a_star():
    """Postgres 里选出项 (s.*)、(s).* 照样展开成好几列：后面没起别名的表达式按结果末尾倒着数，不按一列数位置。"""
    columns = [*LAYOUT, "upper"]
    for item in ("(s.*)", "(s).*"):
        sql = f"SELECT {item}, UPPER(s.buyer_name) FROM sales s"
        assert hidden_of(sql, columns) == {*MASKED, "upper"}, item


def test_sqlite_runs_of_the_same_shapes_are_unchanged():
    """SQLite 没有整行引用、列名表：同样的写法照旧（表和列同名时按列认，不多遮）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE region (id INTEGER, region TEXT, buyer_phone TEXT);"
                       "INSERT INTO region VALUES (1, '华东', '13900000001');")
    sql = "SELECT region AS 区域, COUNT(*) AS n FROM region GROUP BY region"
    cursor = conn.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    assert masking.mask_query_result(payload, MASKED, "sqlite").payload is payload
    conn.close()
