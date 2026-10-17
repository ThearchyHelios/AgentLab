"""按引用的列投影（data/sql_semantics.py 的 project_select / project_template / output_names，next-round 口径账本：整题等价
按答案引用的那几列比）。

投影本身不判等价，只把「没被引用、去掉也不改变被引用那几列取值」的选择项去掉，交给现有的绑定和判等价。这里守两件事：
- 该去的去得掉：多选一列、多几个聚合、日期常量列、带 WITH 的、只出一行的聚合带 LIMIT；口径模板里的 {{ input.x }} 原样留着；
- 去了会变的一律不去（宁可不投影）：DISTINCT / DISTINCT ON、出现在 GROUP BY 里、被 ORDER BY / HAVING / QUALIFY / 窗口
  用到（按别名、按同样的写法、按位置）、和 LIMIT / OFFSET 一起定取哪几行、集合运算、*、可能改变行数的函数、去掉仅有的聚合、
  留下按任意一行取值的列。

另有执行守门：语料（tests/fixtures/sql_pairs.py）里每一条 SQL，按每一列（和「只去掉一列」）投影，投影得了的在虚构库的每组
数据上，投影后的结果都得等于原结果的那几列。
"""
from __future__ import annotations

import pytest

from app.data.sql_semantics import output_names, project_select, project_template
from tests.fixtures import sql_pairs

WEEKLY = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
          "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")


# --------------------------------------------------------------------------
# 该去的去得掉
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, keep, want", [
    ("SELECT SUM(amount) AS sales, COUNT(*) AS n FROM orders WHERE status = 'paid'", [0],
     "SELECT SUM(amount) AS sales FROM orders WHERE status = 'paid'"),
    ("SELECT COUNT(*) AS n, MIN(paid_at) AS first_day, MAX(paid_at) AS last_day, SUM(amount) AS sales "
     "FROM orders WHERE paid_at >= '2026-10-05'", [3],
     "SELECT SUM(amount) AS sales FROM orders WHERE paid_at >= '2026-10-05'"),
    ("SELECT '2026-10-05' AS week_start, '2026-10-11' AS week_end, COALESCE(SUM(amount), 0) AS sales FROM orders", [2],
     "SELECT COALESCE(SUM(amount), 0) AS sales FROM orders"),
    ("WITH paid AS (SELECT amount FROM orders WHERE status = 'paid') SELECT SUM(amount) AS s, COUNT(*) AS n FROM paid",
     [0], "WITH paid AS (SELECT amount FROM orders WHERE status = 'paid') SELECT SUM(amount) AS s FROM paid"),
    # 不分组的聚合只出一行：LIMIT 取哪一行和选出来的列无关
    ("SELECT SUM(amount) AS s, COUNT(*) AS n FROM orders LIMIT 100", [0], "SELECT SUM(amount) AS s FROM orders LIMIT 100"),
    # 分组的一段去掉别的聚合（分组键留着）
    ("SELECT region, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY region", [0, 1],
     "SELECT region, SUM(amount) AS s FROM orders GROUP BY region"),
    # 调换先后
    ("SELECT COUNT(*) AS n, SUM(amount) AS s FROM orders", [1, 0], "SELECT SUM(amount) AS s, COUNT(*) AS n FROM orders"),
], ids=["extra-count", "extra-aggregates", "date-constants", "cte", "single-row-limit", "grouped", "reorder"])
def test_items_that_do_not_change_the_referenced_columns_are_dropped(sql, keep, want):
    p = project_select(sql, keep, "sqlite")
    assert p.refused is None and p.sql == want
    assert p.kept == tuple(keep) and p.dropped == len(output_names(sql, "sqlite")) - len(keep)


def test_the_text_around_the_select_list_is_kept_as_written():
    sql = "SELECT\n  SUM(amount) AS sales,  -- 金额\n  COUNT(*) AS n\nFROM orders o\nWHERE o.status = 'paid'  -- 只算已支付"
    p = project_select(sql, [0], "sqlite")
    assert p.sql == "SELECT\n  SUM(amount) AS sales\nFROM orders o\nWHERE o.status = 'paid'  -- 只算已支付"
    assert p.names == ("sales",)


def test_keeping_everything_in_order_gives_the_sql_back_unchanged():
    sql = "SELECT SUM(amount) AS s FROM orders"
    p = project_select(sql, [0], "sqlite", width=1)
    assert p.sql == sql and p.dropped == 0 and p.refused is None


def test_template_projection_keeps_the_slots():
    template = ("SELECT SUM(amount) AS gmv, COUNT(*) AS n, '{{ input.start }}' AS since FROM orders "
                "WHERE status = 'paid' AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
    assert output_names(template, "sqlite", template=True) == ["gmv", "n", "since"]
    p = project_template(template, [0], "sqlite")
    assert p.refused is None and p.sql == WEEKLY and p.dropped == 2 and p.names == ("gmv",)
    p = project_template(template, [1], "sqlite")
    assert p.sql == WEEKLY.replace("SUM(amount) AS gmv", "COUNT(*) AS n")


# --------------------------------------------------------------------------
# 去了会变的一律不去
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, keep, dialect", [
    ("SELECT DISTINCT region, amount FROM orders", [1], "sqlite"),
    ("SELECT DISTINCT ON (region) region, amount FROM orders ORDER BY region, amount", [1], "postgres"),
    # 多一个分组列：它在 GROUP BY 里
    ("SELECT region, SUM(amount) AS s FROM orders GROUP BY region", [1], "sqlite"),
    ("SELECT region AS r, SUM(amount) AS s FROM orders GROUP BY 1", [1], "sqlite"),
    # 按别的列排序再取前几行
    ("SELECT amount, paid_at FROM orders ORDER BY paid_at DESC LIMIT 5", [0], "sqlite"),
    ("SELECT amount, id FROM orders ORDER BY amount LIMIT 3 OFFSET 1", [0], "sqlite"),
    # HAVING 用到多出来的聚合：按别名、按同样的写法
    ("SELECT region, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY region HAVING n > 2", [0, 1], "sqlite"),
    ("SELECT region, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY region HAVING COUNT(*) > 2", [0, 1],
     "sqlite"),
    # ORDER BY 用到、按位置引用
    ("SELECT region, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY region ORDER BY n DESC", [0, 1], "sqlite"),
    ("SELECT region, SUM(amount) AS s, COUNT(*) AS n FROM orders GROUP BY region ORDER BY 3 DESC", [0, 1], "sqlite"),
    # 窗口、QUALIFY 用到
    ("SELECT id, amount, SUM(amount) OVER (ORDER BY id) AS run FROM orders", [0, 2], "sqlite"),
    ("SELECT region, amount, ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount) AS rn FROM orders QUALIFY rn = 1",
     [0, 1], "duckdb"),
    # SQLite 的 WHERE 认列别名
    ("SELECT amount, DATE(paid_at) AS d FROM orders WHERE d >= '2026-10-05'", [0], "sqlite"),
    # 集合运算、*
    ("SELECT SUM(amount) AS s, COUNT(*) AS n FROM orders UNION ALL SELECT 1, 2", [0], "sqlite"),
    ("SELECT *, amount * 2 AS twice FROM orders", [1], "sqlite"),
    # 可能把一行展开成多行的函数、不认识的函数
    ("SELECT id, unnest(tags) AS tag FROM orders", [0], "postgres"),
    ("SELECT id, generate_series(1, 3) AS k FROM orders", [0], "postgres"),
    ("SELECT id, json_array_elements(payload) AS e FROM orders", [0], "postgres"),
    # 去掉仅有的聚合：剩下的不再只是一行
    ("SELECT COUNT(*) AS n, '2026' AS y FROM orders", [1], "sqlite"),
    # 留下按任意一行取值的列（SQLite 取 MAX 那一行）
    ("SELECT region, MAX(amount) AS m, COUNT(*) AS n FROM orders", [0, 1], "sqlite"),
    # MySQL 里紧跟着字的 -- 是两个减号：改写之后位置对不上原文
    ("SELECT SUM(amount)--1 AS s, COUNT(*) AS n FROM orders", [0], "mysql"),
], ids=["distinct", "distinct-on", "group-by-column", "group-by-ordinal", "order-limit", "order-limit-offset",
        "having-alias", "having-same-aggregate", "order-by-alias", "order-by-ordinal", "window", "qualify",
        "sqlite-where-alias", "union", "star", "unnest", "generate-series", "unknown-function", "only-aggregate",
        "loose-column", "mysql-dashes"])
def test_items_whose_removal_could_change_the_rest_are_never_dropped(sql, keep, dialect):
    p = project_select(sql, keep, dialect)
    assert p.sql is None and p.refused, (sql, p)


def test_the_result_width_must_match_the_select_list():
    p = project_select("SELECT SUM(amount) AS s, COUNT(*) AS n FROM orders", [0], "sqlite", width=3)
    assert p.sql is None and "列数" in (p.refused or "")


@pytest.mark.parametrize("keep", [[], [0, 0], [5]])
def test_bad_keep_lists_are_refused(keep):
    assert project_select("SELECT SUM(amount) AS s, COUNT(*) AS n FROM orders", keep, "sqlite").sql is None


def test_unreorderable_items_keep_their_order():
    # DuckDB 认前面的列别名：调换之后 b 就找不到 a 了
    p = project_select("SELECT amount + 1 AS a, a * 2 AS b FROM orders", [1, 0], "duckdb")
    assert p.sql is None and p.refused


# --------------------------------------------------------------------------
# 执行守门：投影得了的，结果就是原结果的那几列
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def databases():
    conns = sql_pairs.build_databases()
    yield conns
    for c in conns:
        c.close()


def _keeps(width: int) -> list[tuple[int, ...]]:
    """每一列单独留下，再加上「只去掉一列」的几种（列多的只试前四列）。"""
    singles = [(i,) for i in range(min(width, 4))]
    drops = [tuple(j for j in range(width) if j != i) for i in range(min(width, 4))] if width > 2 else []
    return singles + drops


def _bag(rows: list[tuple]) -> list[tuple]:
    """同 sql_pairs.run：行按值排序（先后不算结果的一部分），浮点数按 6 位小数取整。"""
    rows = [tuple(sql_pairs._norm(v) for v in row) for row in rows]
    return sorted(rows, key=lambda r: tuple((v is None, str(type(v)), v if v is not None else 0) for v in r))


def _corpus() -> list[str]:
    seen: dict[str, None] = {}
    for pair in sql_pairs.PAIRS:
        seen.setdefault(pair.a)
        seen.setdefault(pair.b)
    return list(seen)


def test_projection_keeps_the_values_of_the_kept_columns(databases):
    """语料里每一条 SQL：投影得了的，在每组数据上投影后的结果（行的先后不算）都等于原结果只取留下的那几列。同时数一数
    投影了多少种，免得规则收得太紧、守门形同虚设。"""
    projected = 0
    for sql in _corpus():
        width = len(output_names(sql, "sqlite") or [])
        if width < 2:
            continue
        for keep in _keeps(width):
            p = project_select(sql, keep, "sqlite", width=width)
            if p.sql is None:
                continue
            projected += 1
            for conn in databases:
                try:
                    full = conn.execute(sql).fetchall()
                except Exception:  # noqa: BLE001 - 语料里有只在别的方言里成立的写法：原句跑不了的不比
                    break
                want = _bag([tuple(row[k] for k in keep) for row in full])
                assert sql_pairs.run(conn, p.sql) == want, (sql, keep, p.sql)
    assert projected >= 100, projected
