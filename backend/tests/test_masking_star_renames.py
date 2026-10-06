"""按 SQL 认取自遮罩列的结果列（engine/masking.py 的 _derived_hidden）：* 把遮罩列带进了别的列名（复核第 2 轮的 blocker）。

* 展开的列按位置落进结果：WITH t(a, b, …) AS (SELECT * FROM sales) 的列名表、FROM 里子查询的列名表会给它们换上别的
名字，UNION 后一支的 * 落进第一支起的列名里，标量子查询里的 * 则把值交给外层的表达式。以前 * 本身不算取自哪一列，这些
写法的原值原样交给核查 agent、落进快照，审批质询按值遮的第二道防线也认不出。现在 * 带出的值取自遮罩列时，它展开落到的
那几列都算：只展开一个来源、那里的每一列都数得清的（子查询按选出项，数据源里的表按查询当时的表结构），按位置只算取自
遮罩列的那几列；数不清的（没有表结构、列数对不上）整段都算（多遮）。不改名的 *（单独一条 SELECT 的 *、UNION 第一支的 *）
照旧按名字认；* 只带出没碰遮罩列的值、EXISTS 里的 * 都不算。

查询都在内存里的 SQLite 上真跑一遍，看遮完的结果里还有没有原值。数据都是虚构的：区域、手机号、姓名、金额。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import models  # noqa: F401 - 建表（conftest）之前先登记模型：表结构快照要存进工件库
from app.engine import masking

MASKED_WORD = "已遮罩"
#: 设了遮罩的列
MASKED = ["buyer_phone", "buyer_name"]
#: 遮罩列里的值：遮完的结果里不能出现
SECRETS = ("1390000000", "张伟", "李娜", "王芳")
SCRIPT = ("CREATE TABLE sales (id INTEGER, region TEXT, buyer_phone TEXT, buyer_name TEXT, amount INTEGER, note TEXT);"
          "INSERT INTO sales VALUES (1, '华东', '13900000001', '张伟', 1200, '改址'),"
          " (2, '华南', '13900000002', '李娜', 300, '回访'), (3, '华北', '13900000003', '王芳', 900, '');"
          # 只有一列号码的表：标量子查询、UNION 后一支的 * 直接展开它
          "CREATE TABLE phones (buyer_phone TEXT);"
          "INSERT INTO phones VALUES ('13900000009');")


@pytest.fixture(scope="module")
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCRIPT)
    yield conn
    conn.close()


def masked_run(db, sql: str) -> masking.QueryMask:
    """真跑一遍 SQL，按工具边界的办法遮（mask_query_result）。"""
    cursor = db.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = [list(r) for r in cursor.fetchall()]
    payload = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "sql": sql}
    return masking.mask_query_result(payload, MASKED, "sqlite")


def _leaks(done: masking.QueryMask) -> bool:
    text = json.dumps(done.payload["rows"], ensure_ascii=False)
    return any(s in text for s in SECRETS)


@pytest.mark.parametrize("sql, hidden, shown", [
    pytest.param("WITH t(a, b, c, d, e, f) AS (SELECT * FROM sales) SELECT * FROM t", {"c", "d"}, set(),
                 id="CTE 列名表套 * 展开表"),
    pytest.param("WITH t(a, b, c, d, e, f) AS (SELECT * FROM sales) SELECT c AS 号码, e AS 金额 FROM t "
                 "ORDER BY e DESC LIMIT 1", {"号码"}, set(), id="CTE 列名表套 * 再改名"),
    pytest.param("SELECT 0 AS a, '' AS b, '' AS c, '' AS d, 0 AS e, '' AS f UNION ALL SELECT * FROM sales",
                 {"c", "d"}, set(), id="UNION 后一支的 * 展开表"),
    pytest.param("SELECT region FROM sales UNION ALL SELECT * FROM phones", {"region"}, set(),
                 id="UNION 后一支的 * 展开只有号码的表"),
    pytest.param("WITH a AS (SELECT buyer_phone FROM sales), b(x) AS (SELECT * FROM a) SELECT x FROM b", {"x"},
                 set(), id="列名表套 * 展开另一段 CTE"),
    pytest.param("SELECT r FROM (SELECT region AS r FROM sales UNION ALL "
                 "SELECT * FROM (SELECT buyer_phone FROM sales))", {"r"}, set(), id="子查询里 UNION 后一支的 *"),
    pytest.param("SELECT * FROM (SELECT region FROM sales UNION ALL SELECT * FROM (SELECT buyer_phone FROM sales))",
                 {"region"}, set(), id="外层 * 取子查询里 UNION 后一支的 *"),
    pytest.param("WITH t(k) AS (SELECT * FROM (SELECT buyer_name FROM sales)) SELECT k AS 名 FROM t", {"名"}, set(),
                 id="列名表套 * 展开子查询"),
    pytest.param("SELECT region FROM sales UNION ALL SELECT s.* FROM (SELECT buyer_name FROM sales) s", {"region"},
                 set(), id="UNION 后一支的 t.*"),
    pytest.param("SELECT region FROM sales UNION SELECT * FROM (SELECT buyer_phone FROM sales)", {"region"}, set(),
                 id="UNION 去重"),
    # 只展开一个派生来源、每一列都数得清：按位置只遮取自遮罩列的那一列，金额照常
    pytest.param("SELECT '合计' AS 区域, SUM(amount) AS 金额 FROM sales UNION ALL SELECT * FROM "
                 "(SELECT buyer_phone, amount FROM sales ORDER BY amount DESC LIMIT 2)", {"区域"}, {"金额"},
                 id="合计行 UNION 后一支的 * 按位置"),
    pytest.param("SELECT region AS 区域, amount AS 金额 FROM sales UNION ALL SELECT * FROM "
                 "(SELECT * FROM (SELECT buyer_name, amount FROM sales))", {"区域"}, {"金额"},
                 id="套了两层的 * 按位置"),
    pytest.param("WITH t(r, x) AS (SELECT * FROM (SELECT buyer_phone, amount FROM sales)) SELECT r AS 号码, x AS 金额 "
                 "FROM t", {"号码"}, {"金额"}, id="列名表套 * 按位置"),
    pytest.param("SELECT (SELECT * FROM (SELECT buyer_phone FROM sales) LIMIT 1) AS x", {"x"}, set(),
                 id="标量子查询里的 * 展开子查询"),
    pytest.param("SELECT region, (SELECT * FROM (SELECT s2.buyer_name FROM sales s2 WHERE s2.id = s.id)) AS 名 "
                 "FROM sales s", {"名"}, {"region"}, id="相关子查询里的 *"),
    pytest.param("SELECT (SELECT * FROM phones LIMIT 1) AS x", {"x"}, set(), id="标量子查询里的 * 展开表"),
])
def test_a_star_that_renames_a_masked_column_is_masked(db, sql, hidden, shown):
    """* 把遮罩列的值带进了别的列名：这几列整列遮，原值不交出去。按位置认得出的，别的列照常。"""
    done = masked_run(db, sql)
    columns = {c.lower() for c in done.columns}
    assert hidden <= columns, done.columns
    assert not shown & columns, done.columns
    assert not _leaks(done), done.payload["rows"]
    assert done.secrets.strong, "遮掉的值交回调用方（后面的查询、回答里按值遮）"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT region, amount FROM sales) t",
    "WITH t AS (SELECT * FROM sales) SELECT region, SUM(amount) AS 合计 FROM t GROUP BY region",
    "SELECT region AS 区域 FROM sales UNION ALL SELECT * FROM (SELECT region FROM sales)",
    "WITH t(a, b) AS (SELECT * FROM (SELECT region, amount FROM sales)) SELECT a, SUM(b) AS 合计 FROM t GROUP BY a",
    "SELECT region, EXISTS(SELECT * FROM sales s2 WHERE s2.id = s.id AND s2.amount > 500) AS 大单 FROM sales s",
    "SELECT '合计' AS 区域, SUM(amount) AS 金额 FROM sales UNION ALL SELECT * FROM "
    "(SELECT region, SUM(amount) FROM sales GROUP BY region)",
])
def test_a_star_that_brings_no_masked_values_stays_visible(db, sql):
    """* 只带出没碰遮罩列的值、EXISTS 里的 *（只说有没有）：一列都不遮，结果原样。"""
    done = masked_run(db, sql)
    assert done.columns == [] and MASKED_WORD not in json.dumps(done.payload, ensure_ascii=False)


def test_a_plain_star_still_masks_by_name(db):
    """不改名的 *（单独一条 SELECT）照旧只遮名字对上遮罩列的那几列。"""
    done = masked_run(db, "SELECT * FROM sales")
    assert done.columns == ["buyer_phone", "buyer_name"] and not _leaks(done)


@pytest.mark.parametrize("sql, columns, hidden", [
    pytest.param("SELECT x FROM (SELECT * FROM (SELECT buyer_phone FROM sales) s) AS q(x)", ["x"], {"x"},
                 id="子查询列名表套 *"),
    pytest.param("SELECT l.x FROM sales s CROSS JOIN LATERAL (SELECT * FROM (SELECT s.buyer_phone) z) AS l(x)", ["x"],
                 {"x"}, id="LATERAL 列名表套 *"),
    pytest.param("SELECT ARRAY(SELECT * FROM (SELECT buyer_phone FROM sales) s) AS arr", ["arr"], {"arr"},
                 id="ARRAY 子查询里的 *"),
])
def test_a_star_renaming_in_postgres_is_masked(sql, columns, hidden):
    snapshot = {"source": "shop", "sql": sql, "columns": columns, "rows": []}
    assert hidden <= masking._derived_hidden(snapshot, set(MASKED), "postgres")[0]


def test_a_metric_summed_over_a_star_renamed_masked_column_is_not_sent_to_blind_check():
    """双盲复算按同一套认法挡下取自遮罩列的指标（derived_columns）：遮罩的金额经 CTE 列名表套 * 改名之后汇总，照样认得出，
    区域照常。"""
    sql = "WITH q(r, x) AS (SELECT * FROM (SELECT region, amount FROM sales)) SELECT r AS 区域, SUM(x) AS gmv FROM q " \
          "GROUP BY r"
    snapshot = {"source": "shop", "sql": sql, "columns": ["区域", "gmv"], "rows": []}
    assert masking.derived_columns(snapshot, {"amount"}, "sqlite") == {"gmv"}


def test_a_recursive_cte_with_a_star_is_read_without_looping(db):
    """递归的 CTE 里 * 展开它自己：照样认得出、不会绕不出来。"""
    sql = ("WITH RECURSIVE r(n, p) AS (SELECT 1, buyer_phone FROM sales WHERE id = 1 UNION ALL "
           "SELECT n + 1, p FROM r WHERE n < 2) SELECT * FROM (SELECT * FROM r) t")
    done = masked_run(db, sql)
    assert "p" in {c.lower() for c in done.columns} and not _leaks(done)
    loop = "WITH RECURSIVE r AS (SELECT buyer_phone AS p FROM sales UNION ALL SELECT * FROM r) SELECT p AS 号 FROM r"
    snapshot = {"source": "shop", "sql": loop, "columns": ["号"], "rows": []}
    assert "号" in masking._derived_hidden(snapshot, set(MASKED), "sqlite")[0]


def test_a_star_with_a_replace_is_not_counted_by_position():
    """* 带着 REPLACE / EXCLUDE 这类改动（DuckDB 一类的写法）：展开成的列不再是来源那几列的原样，不按位置数，整段都算。
    这里 REPLACE 把手机号换进了「amount」那一列。"""
    sql = ("SELECT region, amount FROM sales UNION ALL SELECT * REPLACE (buyer_phone AS amount) FROM "
           "(SELECT buyer_phone, amount FROM sales) t")
    snapshot = {"source": "shop", "sql": sql, "columns": ["region", "amount"], "rows": []}
    assert {"region", "amount"} <= masking._derived_hidden(snapshot, set(MASKED), "duckdb")[0]


# --------------------------------------------------------------------------
# * 展开数据源里的表：按查询当时的表结构数位置
# --------------------------------------------------------------------------


#: 两个月的订单表列一样、次序一样（上传表格里的月度工作表常这样）；三月那张的号码和区域换了次序
MONTHS = ("CREATE TABLE jan (id INTEGER, region TEXT, buyer_phone TEXT, amount INTEGER);"
          "CREATE TABLE feb (id INTEGER, region TEXT, buyer_phone TEXT, amount INTEGER);"
          "CREATE TABLE mar (id INTEGER, buyer_phone TEXT, region TEXT, amount INTEGER);"
          "INSERT INTO jan VALUES (1, '华东', '13900000001', 1200), (2, '华南', '13900000002', 300);"
          "INSERT INTO feb VALUES (3, '华东', '13900000003', 900);"
          "INSERT INTO mar VALUES (4, '13900000004', '华北', 400);")


@pytest.fixture(scope="module")
def months():
    conn = sqlite3.connect(":memory:")
    conn.executescript(MONTHS)
    yield conn
    conn.close()


def schema_of(conn) -> dict:
    """数据源的表结构（schema_cache 的样子）：每张表的列按表里的次序。"""
    names = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {"tables": {name: {"columns": [{"name": row[1]} for row in conn.execute(f"PRAGMA table_info({name})")]}
                       for name in names}}


def run_months(conn, sql: str, schema: dict | None) -> masking.QueryMask:
    cursor = conn.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    return masking.mask_query_result(payload, ["buyer_phone"], "sqlite", schema=schema)


def test_a_union_of_tables_with_the_same_layout_is_masked_by_name_with_the_schema(months):
    """UNION 的几支都是 SELECT * FROM 表、表的列一样：按表结构数得出号码落在哪一列，只遮那一列，汇总照常。没有表结构时
    说不清号码排在第几，* 展开落到的那一段整段遮（多遮）。"""
    schema = schema_of(months)
    total = "SELECT region, SUM(amount) AS 合计 FROM (SELECT * FROM jan UNION ALL SELECT * FROM feb) t GROUP BY region"
    done = run_months(months, total, schema)
    assert done.columns == [] and done.payload["rows"] == [["华东", 2100], ["华南", 300]]
    assert {"region", "合计"} <= set(run_months(months, total, None).columns)
    both = run_months(months, "SELECT * FROM jan UNION ALL SELECT * FROM feb", schema)
    assert both.columns == ["buyer_phone"] and not _leaks(both)


def test_a_union_of_tables_in_another_order_masks_where_the_values_land(months):
    """三月那张表号码排在第二列：UNION 之后它落进第一支的「region」，按表结构认得出，这一列遮；金额照常。"""
    schema = schema_of(months)
    both = run_months(months, "SELECT * FROM jan UNION ALL SELECT * FROM mar", schema)
    assert {"region", "buyer_phone"} <= set(both.columns) and "amount" not in both.columns and not _leaks(both)
    total = "SELECT region AS 区域, SUM(amount) AS 合计 FROM (SELECT * FROM jan UNION ALL SELECT * FROM mar) t " \
            "GROUP BY region"
    done = run_months(months, total, schema)
    assert done.columns == ["区域"] and not _leaks(done)


def test_a_column_list_over_a_table_is_masked_by_position_with_the_schema(months):
    """列名表套 * 展开数据源里的表：按表结构只遮号码换了名字的那一列。"""
    done = run_months(months, "WITH t(a, b, c, d) AS (SELECT * FROM jan) SELECT * FROM t", schema_of(months))
    assert done.columns == ["c"] and [row[3] for row in done.payload["rows"]] == [1200, 300]


def test_a_schema_that_does_not_match_the_table_masks_the_whole_stretch(months):
    """表结构里没有这张表，或者列数和实际展开的对不上（表结构过期了）：照旧整段遮。"""
    stale = schema_of(months)
    stale["tables"]["feb"]["columns"] = stale["tables"]["feb"]["columns"][:3]
    for schema in (stale, {"tables": {"jan": schema_of(months)["tables"]["jan"]}}):
        both = run_months(months, "SELECT * FROM jan UNION ALL SELECT * FROM feb", schema)
        assert set(both.columns) == {"id", "region", "buyer_phone", "amount"} and not _leaks(both)


async def test_a_snapshot_is_read_with_its_frozen_schema(months):
    """事后按快照认（双盲复算挡指标、审批质询收值）用快照记着的表结构快照（schema_artifact），和查询当时工具边界的认法
    一样：几张同样的月度表 UNION 起来汇总的金额不算取自遮罩列。"""
    from app.core import artifact_store

    sql = "SELECT region, SUM(amount) AS 合计 FROM (SELECT * FROM jan UNION ALL SELECT * FROM feb) t GROUP BY region"
    schema = await artifact_store.put_json({"source": "shop", **schema_of(months)}, kind="schema_snapshot",
                                           run_id="r-star", node_id="n1")
    snapshot = {"source": "shop", "sql": sql, "columns": ["region", "合计"], "rows": [], "schema_artifact": schema}
    assert masking.derived_columns(snapshot, {"buyer_phone"}, "sqlite") == set()
    assert masking._derived_hidden(snapshot, {"buyer_phone"}, "sqlite")[0] == {"buyer_phone"}
    assert masking.derived_columns({**snapshot, "schema_artifact": "missing"}, {"buyer_phone"}, "sqlite") == {
        "region", "合计"}


# --------------------------------------------------------------------------
# 写了库名的表、过期的表结构（复核第 3 轮）
# --------------------------------------------------------------------------


#: 同名的两张表：主库的 sales 和另一个库（archive）里的 sales，号码、区域换了次序。表结构探查的是主库
TWO_SALES = ("CREATE TABLE sales (id INTEGER, region TEXT, buyer_phone TEXT, buyer_name TEXT, amount INTEGER, "
             "note TEXT);"
             "INSERT INTO sales VALUES (1, '华东', '13900000001', '张伟', 1200, '改址');"
             "CREATE TABLE archive.sales (id INTEGER, buyer_phone TEXT, region TEXT, buyer_name TEXT, amount INTEGER, "
             "note TEXT);"
             "INSERT INTO archive.sales VALUES (7, '13900000007', '华北', '王芳', 500, '回访');")
SALES_LAYOUT = ["id", "region", "buyer_phone", "buyer_name", "amount", "note"]


@pytest.fixture
def two_sales():
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS archive")
    conn.executescript(TWO_SALES)
    yield conn
    conn.close()


def probed(schema: str | None = None) -> dict:
    """探查主库得到的表结构（schema_cache 的样子）：没设 schema 的探查当前库，qualified 就是表名。"""
    meta = {"qualified": f"{schema}.sales" if schema else "sales", "schema": schema,
            "columns": [{"name": c} for c in SALES_LAYOUT]}
    return {"tables": {"sales": meta}, "schema": schema}


def run_two(conn, sql: str, schema: dict | None) -> masking.QueryMask:
    cursor = conn.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    return masking.mask_query_result(payload, MASKED, "sqlite", schema=schema)


@pytest.mark.parametrize("sql", [
    pytest.param("WITH t(a, b, c, d, e, f) AS (SELECT * FROM archive.sales) SELECT b AS j, a AS n FROM t",
                 id="列名表套 * 展开另一个库里的同名表"),
    pytest.param("SELECT 0 AS a, '' AS b, '' AS c, '' AS d, 0 AS e, '' AS f WHERE 0 UNION ALL "
                 "SELECT * FROM archive.sales", id="UNION 后一支的 * 展开另一个库里的同名表"),
    pytest.param("WITH t AS (SELECT * FROM sales UNION ALL SELECT * FROM archive.sales) SELECT region AS 区域 FROM t",
                 id="派生来源里 UNION 后一支展开另一个库里的同名表"),
])
def test_a_table_in_another_schema_is_not_counted_by_the_probed_one(two_sales, sql):
    """表结构探查的是主库：查询写了别的库名的同名表（archive.sales，号码排在第二列），不能按主库那张表的列序数位置，
    按数不清处理，* 展开落到的那一段整段遮。以前按表名查表结构，号码落进的那一列被当成区域，原样交给模型、落库。"""
    done = run_two(two_sales, sql, probed())
    assert not _leaks(done), done.payload["rows"]


def test_a_table_named_with_its_probed_schema_is_still_counted(two_sales):
    """库名和表结构记下的对得上（探查时设了 schema、qualified 带着它）：照样按表结构数位置，只遮号码那一列。没写库名的
    照旧按表名查。库名对不上表结构记下的（这里探查时没设 schema，查询写了 main.）按数不清处理，整段遮（多遮）。"""
    precise = "WITH t(a, b, c, d, e, f) AS (SELECT * FROM {}) SELECT c AS j, a AS n FROM t"
    assert run_two(two_sales, precise.format("main.sales"), probed("main")).columns == ["j"]
    assert run_two(two_sales, precise.format("sales"), probed()).columns == ["j"]
    assert set(run_two(two_sales, precise.format("main.sales"), probed()).columns) == {"j", "n"}


def test_schema_columns_are_also_keyed_by_the_probed_schema():
    """表结构按「库名.表名」另记一份（库名取 schema、qualified 的前缀）；写了别的库名的查不到。"""
    tables = masking._schema_columns(probed("public"))
    assert tables["sales"] == tables["public.sales"] == SALES_LAYOUT and "archive.sales" not in tables
    assert "public.sales" not in masking._schema_columns(probed())
    assert "archive.sales" not in (masking._schema_tables(probed("public")) or {})


#: 一月的表结构是早先探查的（那时还没有 extra 这一列），表里现在多了一列，排在第二；二月那张号码排在第二列
STALE = ("CREATE TABLE jan (id INTEGER, extra TEXT, region TEXT, buyer_phone TEXT, amount INTEGER);"
         "CREATE TABLE feb (id INTEGER, buyer_phone TEXT, region TEXT, amount INTEGER, extra TEXT);"
         "INSERT INTO jan VALUES (1, '加急', '华东', '13900000001', 1200);"
         "INSERT INTO feb VALUES (2, '13900000002', '华南', 300, '普通');")


@pytest.fixture
def stale():
    conn = sqlite3.connect(":memory:")
    conn.executescript(STALE)
    yield conn
    conn.close()


def layout(**tables: list[str]) -> dict:
    return {"tables": {name: {"qualified": name, "columns": [{"name": c} for c in columns]}
                       for name, columns in tables.items()}}


#: 过期的表结构：一月那张少了 extra（列数和表里实际的不一样）
OLD_JAN = layout(jan=["id", "region", "buyer_phone", "amount"], feb=["id", "buyer_phone", "region", "amount", "extra"])
#: 准的表结构
NOW = layout(jan=["id", "extra", "region", "buyer_phone", "amount"],
             feb=["id", "buyer_phone", "region", "amount", "extra"])


@pytest.mark.parametrize("sql", [
    pytest.param("WITH t AS (SELECT * FROM jan UNION ALL SELECT id, buyer_phone, region, amount, extra FROM feb) "
                 "SELECT extra AS 备注 FROM t", id="后一支明写各列"),
    pytest.param("WITH t AS (SELECT * FROM jan UNION ALL SELECT * FROM feb) SELECT extra AS 备注 FROM t",
                 id="后一支也是 *"),
])
def test_a_first_branch_counted_by_a_stale_schema_is_not_trusted(stale, sql):
    """UNION 第一支是 *、按表结构数出的列名（表结构过期了，少一列）和后一支的列数对不上：不按它对列名，* 所在的位置按认不出
    算，整份遮。以前照过期的列名表对，二月的号码落进的「extra」没认出来，原样交回。"""
    cursor = stale.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    for schema in (OLD_JAN, None, NOW):
        done = masking.mask_query_result(payload, ["buyer_phone"], "sqlite", schema=schema)
        assert done.columns == ["备注"] and not _leaks(done), (schema, done.payload["rows"])


def test_a_first_branch_counted_by_an_accurate_schema_still_places_the_values(stale):
    """表结构准、每一支数得出的列数都对得上：照样按第一支的列名对上位置，只遮号码落进的那一列，金额照常。"""
    sql = ("WITH t AS (SELECT * FROM jan UNION ALL SELECT id, buyer_phone, region, buyer_phone, amount FROM feb) "
           "SELECT region AS 区域, amount AS 金额 FROM t")
    cursor = stale.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    assert masking.mask_query_result(payload, ["buyer_phone"], "sqlite", schema=NOW).columns == []
    moved = sql.replace("SELECT id, buyer_phone, region,", "SELECT id, region, buyer_phone,")
    cursor = stale.execute(moved)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()],
               "sql": moved}
    done = masking.mask_query_result(payload, ["buyer_phone"], "sqlite", schema=NOW)
    assert done.columns == ["区域"] and [row[1] for row in done.payload["rows"]] == [1200, 300]
