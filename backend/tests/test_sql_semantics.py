"""SQL 语义（data/sql_semantics.py）：两条 SQL 是不是等价，不是的话差在哪。

- normalize_sql：字面量换成占位符后的规范文本 + 字面量。只有字面量不同的两条 SQL 规范文本相同（定时运行每期的日期
  不同，不能算模板变了）；关键字大小写、空白、注释也不算不同；结构变了的一定不同；解析不了的退回逐词规范化
- facets：把一条查询拆成表、连接、WHERE 合取式、时间条件、分组、聚合、HAVING、取哪几行（给调用方取结构用）
- explain_sql_diff：两边规范化后的形式一字不差才算等价（规范化的每一步都只做保持语义的变换）；不等价时只把有把握
  的不同写成具体的中文句子（时间列、筛选条件、聚合与去重、关联、分组、HAVING、取行、UNION），说不准的给兜底说明，
  并给出规范化后不同的行。等价的结论另有执行比对守门：见 test_sql_equivalence.py
- diff_facets：照旧收两份 facets，交给 explain_sql_diff 比原文，返回它的条目（含兜底条目）

夹具全是虚构的表：订单 orders、退款 refunds、门店 stores、用户 users、付款 payments，以及一张中文表「订单」。
"""
from __future__ import annotations

import json

import pytest

from app.data.sql_semantics import (
    FacetDiff,
    Facets,
    SqlDiff,
    catalog_labels,
    diff_facets,
    explain_sql_diff,
    facets,
    normalize_sql,
)
from tests.fixtures import sql_pairs

DIALECTS = ("sqlite", "mysql", "postgres")
LABELS = ("原流程", "盲算")
FALLBACK = "两条 SQL 的写法不同，未能归纳成具体差异，请对照原文"
FALLBACK_REST = "另有未能归纳的差异，请对照原文"


def texts(a: str, b: str, dialect: str = "sqlite", **kwargs) -> list[str]:
    return [d.text for d in diff_facets(facets(a, dialect), facets(b, dialect), **kwargs)]


# --------------------------------------------------------------------------
# normalize_sql
# --------------------------------------------------------------------------


def test_only_literals_differ_means_the_same_template():
    week1 = normalize_sql("SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND store_id = 3 LIMIT 10")
    week2 = normalize_sql("SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-08' AND store_id = 7 LIMIT 10")
    assert week1.same_shape(week2) and week1.text == week2.text
    assert week1.text == "SELECT SUM(amount) FROM orders WHERE paid_at >= ? AND store_id = ? LIMIT ?"
    # 字面量按规范文本里的先后排：语法树里 LIMIT 存在 WHERE 前面，不能因此把 10 排到第一个
    assert week1.literals == ("'2026-09-01'", "3", "10")
    assert week2.literals == ("'2026-09-08'", "7", "10")
    assert week1.parse_error is None


def test_case_whitespace_and_comments_do_not_count():
    a = normalize_sql("select Amount\nfrom ORDERS  -- 本周\nwhere paid_at >= '2026-09-01'", "sqlite")
    b = normalize_sql("SELECT amount FROM orders /* 上周 */ WHERE paid_at >= '2026-08-25'", "sqlite")
    assert a.text == b.text and a.literals != b.literals


def test_a_structural_change_is_a_different_template():
    base = normalize_sql("SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01'")
    for changed in ("SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01'",
                    "SELECT SUM(amount) FROM orders WHERE paid_at > '2026-09-01'",
                    "SELECT AVG(amount) FROM orders WHERE paid_at >= '2026-09-01'",
                    "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND is_deleted = 0"):
        assert not normalize_sql(changed).same_shape(base), changed


@pytest.mark.parametrize("dialect,a,b", [
    ("sqlite", "SELECT strftime('%Y-%m', paid_at) AS m, SUM(amount) AS \"销售额\" FROM \"订单\" "
               "WHERE \"支付日期\" BETWEEN '2026-09-01' AND '2026-09-30' GROUP BY 1",
               "SELECT strftime('%Y-%m', paid_at) AS m, SUM(amount) AS \"销售额\" FROM \"订单\" "
               "WHERE \"支付日期\" BETWEEN '2026-10-01' AND '2026-10-31' GROUP BY 1"),
    ("mysql", "SELECT SUM(o.`金额`) AS `销售额` FROM `订单` o WHERE o.paid_at >= \"2026-09-01\" LIMIT 5, 10",
              "SELECT SUM(o.`金额`) AS `销售额` FROM `订单` o WHERE o.paid_at >= \"2026-09-08\" LIMIT 5, 20"),
    ("postgres", "SELECT SUM(amount) AS \"销售额\" FROM orders WHERE paid_at::date >= CURRENT_DATE - INTERVAL '7 days'",
                 "SELECT SUM(amount) AS \"销售额\" FROM orders WHERE paid_at::date >= CURRENT_DATE - INTERVAL '14 days'"),
])
def test_each_dialect_reads_its_own_syntax(dialect, a, b):
    na, nb = normalize_sql(a, dialect), normalize_sql(b, dialect)
    assert na.parse_error is None and nb.parse_error is None
    assert na.text == nb.text and na.literals != nb.literals
    # 带引号的中文别名原样留在规范文本里：别名不是字面量
    assert "销售额" in na.text


def test_the_data_source_kind_is_accepted_as_the_dialect():
    assert normalize_sql("SELECT 1 FROM t WHERE a = 'x'", "postgresql").text == \
        normalize_sql("SELECT 1 FROM t WHERE a = 'y'", "postgres").text


def test_unparsable_sql_still_tells_a_literal_only_change():
    a = normalize_sql("SELECT amount FROM orders WHERE paid_at >= '2026-09-01' AND AND", "sqlite")
    b = normalize_sql("select amount from ORDERS where paid_at >= '2026-09-08' and and", "sqlite")
    assert a.parse_error and "无法解析" in a.parse_error
    assert a.text == b.text and a.literals == ("'2026-09-01'",) and b.literals == ("'2026-09-08'",)


@pytest.mark.parametrize("sql", ["", "   ", None, "SELECT 1; SELECT 2"])
def test_nothing_to_normalize_never_raises(sql):
    got = normalize_sql(sql, "sqlite")
    assert got.parse_error


# --------------------------------------------------------------------------
# facets：各个方面
# --------------------------------------------------------------------------


def test_tables_are_the_real_tables_without_cte_names():
    f = facets("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') "
               "SELECT s.name, SUM(p.amount) FROM paid p JOIN stores s ON s.id = p.store_id GROUP BY s.name")
    assert [t.text for t in f.tables] == ["orders", "stores"] or [t.text for t in f.tables] == ["stores", "orders"]
    assert "paid" not in {t.key for t in f.tables}


def test_joins_carry_kind_both_sides_and_a_normalized_condition():
    f = facets("SELECT o.id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id AND r.state = 'done'")
    [join] = f.joins
    assert join.kind == "left" and join.table.text == "refunds" and join.other == ("orders",)
    # 别名换成真实表名，两边排好顺序，几个条件按 key 排序后用 AND 连起来
    assert join.on.text == "orders.id = refunds.order_id AND refunds.state = 'done'"
    same = facets("SELECT orders.id FROM orders LEFT OUTER JOIN refunds ON refunds.state = 'done' "
                  "AND orders.id = refunds.order_id")
    assert same.joins[0].on.key == join.on.key and same.joins[0].kind == "left"


def test_a_comma_join_is_the_same_as_an_inner_join():
    comma = facets("SELECT a.x FROM orders a, stores b WHERE a.store_id = b.id AND b.city = '虚构市'")
    inner = facets("SELECT orders.x FROM orders JOIN stores ON stores.id = orders.store_id WHERE stores.city = '虚构市'")
    assert comma.joins == inner.joins and comma.joins[0].kind == "inner"
    assert comma.joins[0].table.text == "stores" and comma.joins[0].other == ("orders",)
    cross = facets("SELECT 1 FROM orders o CROSS JOIN stores s")
    assert (cross.joins[0].kind, cross.joins[0].on, cross.joins[0].other) == ("cross", None, ("orders",))
    assert [p.key for p in comma.where] == [p.key for p in inner.where] == ["stores.city = '虚构市'"]


def test_where_conjuncts_are_normalized():
    f = facets("SELECT 1 FROM orders o WHERE 0 = o.is_deleted AND o.status IN ('shipped', 'paid') "
               "AND (o.channel = 'app' OR o.channel = 'web')")
    assert [p.text for p in f.where] == ["is_deleted = 0", "status IN ('paid', 'shipped')",
                                         "channel = 'app' OR channel = 'web'"]
    assert f.where[0].columns == (("orders", "is_deleted"),)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_exclusions_are_read_in_every_dialect(dialect):
    f = facets("SELECT 1 FROM orders WHERE is_deleted <> 1 AND store_id NOT IN (8, 9) AND refund_id IS NOT NULL "
               "AND note NOT LIKE '%测试%'", dialect)
    assert [p.excludes for p in f.where] == ["is_deleted = 1", "store_id IN (8, 9)", "refund_id 为空",
                                             "note LIKE '%测试%'"]


def test_time_predicates_have_column_operator_and_value():
    f = facets("SELECT SUM(amount) FROM orders o WHERE o.paid_at >= '2026-09-01' AND '2026-10-01' > o.paid_at "
               "AND o.store_id = 3")
    assert [(t.column.text, t.op, t.value) for t in f.time] == [("paid_at", ">=", "'2026-09-01'"),
                                                                ("paid_at", "<", "'2026-10-01'")]
    between = facets("SELECT 1 FROM \"订单\" WHERE \"支付日期\" BETWEEN '2026-09-01' AND '2026-09-30'")
    [t] = between.time
    assert (t.column.text, t.op, t.value) == ('"支付日期"', "between", "'2026-09-01' AND '2026-09-30'")
    # 列套着日期函数也认；按月份字符串筛也认
    month = facets("SELECT 1 FROM orders WHERE strftime('%Y-%m', created_at) = '2026-09'")
    assert [(t.column.text, t.op) for t in month.time] == [("created_at", "=")]
    assert facets("SELECT 1 FROM orders WHERE store_id = 3 AND amount > 100").time == ()


def test_group_by_position_and_alias_mean_the_expression():
    explicit = facets("SELECT o.region, SUM(o.amount) FROM orders o GROUP BY o.region")
    by_position = facets("SELECT region AS r, SUM(amount) FROM orders GROUP BY 1")
    by_alias = facets("SELECT region AS r, SUM(amount) FROM orders GROUP BY r")
    assert [p.key for p in explicit.group_by] == [p.key for p in by_position.group_by] == \
        [p.key for p in by_alias.group_by] == ["orders.region"]


def test_aggregates_have_function_argument_and_distinct():
    f = facets("SELECT COUNT(*), COUNT(1), COUNT(DISTINCT o.order_id), SUM(o.amount), "
               "SUM(o.amount) OVER (PARTITION BY o.region) FROM orders o")
    assert [(g.func, g.arg.text, g.distinct) for g in f.aggregates] == [
        ("count", "*", False), ("count", "order_id", True), ("sum", "amount", False)], \
        "COUNT(1) 和 COUNT(*) 是同一个；窗口函数不算聚合"


def test_having_and_limit():
    f = facets("SELECT region, SUM(amount) FROM orders GROUP BY region HAVING 100 < SUM(amount) LIMIT 10")
    assert [p.text for p in f.having] == ["SUM(amount) > 100"] and f.limit == "10"
    assert facets("SELECT a FROM orders LIMIT 5, 20", "mysql").limit == "20"


def test_having_reads_select_aliases():
    """HAVING 里引用 SELECT 的别名（MySQL、SQLite 的写法）换成别名指的表达式，和 GROUP BY 别名一样。"""
    by_alias = facets("SELECT region AS r, SUM(amount) AS s FROM orders GROUP BY r HAVING s > 100")
    explicit = facets("SELECT o.region, SUM(o.amount) FROM orders o GROUP BY o.region HAVING SUM(o.amount) > 100")
    assert [p.key for p in by_alias.having] == [p.key for p in explicit.having] == ["SUM(orders.amount) > 100"]


def test_limits_are_collected_per_segment_with_offset_and_order():
    """LIMIT 逐段收集（CTE、FROM 里的子查询各算一段），连同 OFFSET 和这一段的排序：只取前几行时，按什么排、
    跳过几行决定了取到的是哪几行。OFFSET 0 等于没写。"""
    f = facets("WITH top AS (SELECT amount FROM orders ORDER BY amount DESC LIMIT 10) "
               "SELECT SUM(amount) FROM top LIMIT 1")
    assert [(x.scope, x.count, x.offset) for x in f.limits] == [(None, "1", None), ("top", "10", None)]
    [(key, desc)] = f.limits[1].order
    assert key.text == "amount" and desc is True
    mysql = facets("SELECT a FROM orders ORDER BY 1 LIMIT 5, 20", "mysql")
    assert (mysql.limits[0].count, mysql.limits[0].offset) == ("20", "5")
    assert [(k.text, d) for k, d in mysql.limits[0].order] == [("a", False)], "ORDER BY 1 换成 SELECT 里的第一项"
    assert facets("SELECT a FROM orders LIMIT 10 OFFSET 0").limits[0].offset is None


@pytest.mark.parametrize("dialect,sql", [
    ("sqlite", 'SELECT SUM("金额") AS "销售额" FROM "订单" WHERE "是否作废" = 0'),
    ("mysql", "SELECT SUM(`金额`) AS `销售额` FROM `订单` WHERE `是否作废` = 0"),
    ("mysql", 'SELECT SUM(`金额`) AS "销售额" FROM `订单` WHERE `是否作废` = 0'),
    ("postgres", 'SELECT SUM("金额") AS "销售额" FROM "订单" WHERE "是否作废" = 0'),
])
def test_quoted_chinese_names_and_aliases(dialect, sql):
    f = facets(sql, dialect)
    assert f.parse_error is None
    assert [t.key for t in f.tables] == ["订单"]
    assert [(g.func, g.arg.key) for g in f.aggregates] == [("sum", '"订单"."金额"')] or \
        [(g.func, g.arg.columns) for g in f.aggregates] == [("sum", (("订单", "金额"),))]
    assert f.where[0].columns == (("订单", "是否作废"),)


def test_a_cte_that_passes_columns_through_maps_them_to_the_table():
    wrapped = facets("WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT SUM(v.amount) FROM v")
    direct = facets("SELECT SUM(amount) FROM orders WHERE is_deleted = 0")
    assert wrapped.aggregates == direct.aggregates and wrapped.where == direct.where


def test_a_correlated_subquery_reads_the_outer_alias():
    """子查询里引用外层的别名（o.id）到外层去找：写成真实表名，两张表的列互相比的条件也写出表名。"""
    f = facets("SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)")
    assert "orders.id = refunds.order_id" in [p.text for p in f.where]
    plain = facets("SELECT orders.id FROM orders WHERE NOT EXISTS "
                   "(SELECT 1 FROM refunds WHERE orders.id = refunds.order_id)")
    assert plain.where[1].key == f.where[1].key
    # 外层那个条件（NOT EXISTS 整句）也一样：子查询按它自己那一段的来源写，别名换成真实表名
    assert plain.where[0].key == f.where[0].key
    assert f.where[0].excludes == "EXISTS(SELECT 1 FROM refunds WHERE orders.id = refunds.order_id)"


def test_a_subquery_in_a_condition_is_written_with_its_own_tables():
    f = facets("SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)")
    assert f.where[0].text == "user_id IN (SELECT id FROM users WHERE vip = 1)"
    assert f.where[0].key == "orders.user_id IN (SELECT users.id FROM users WHERE users.vip = 1)"


def test_the_schema_tells_which_table_an_unqualified_column_belongs_to():
    sql = "SELECT SUM(amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE city = '虚构市'"
    assert facets(sql).where[0].columns == ((None, "city"),), "不给表结构：几张表都可能有这一列，照写列名"
    schema = {"tables": {"orders": {"columns": [{"name": "id"}, {"name": "amount"}, {"name": "store_id"}]},
                         "stores": {"columns": [{"name": "id"}, {"name": "city"}]}}}
    assert facets(sql, schema=schema).where[0].columns == (("stores", "city"),)


@pytest.mark.parametrize("sql,words", [
    ("SELECT FROM WHERE", "无法解析"),
    ("SELECT 1; SELECT 2", "2 条"),
    ("UPDATE orders SET amount = 0", "不是查询"),
    ("", "为空"),
])
def test_unparsable_sql_carries_parse_error_and_never_raises(sql, words):
    f = facets(sql, "sqlite")
    assert f.parse_error and words in f.parse_error
    assert (f.tables, f.joins, f.where, f.aggregates) == ((), (), (), ())


def test_facets_are_json_ready():
    f = facets("SELECT COUNT(DISTINCT order_id) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY region")
    data = json.loads(json.dumps(f.as_dict(), ensure_ascii=False))
    assert data["aggregates"][0]["distinct"] is True and data["time"][0]["op"] == ">="
    assert not [k for k in data if k.startswith("_")], "拿来重新分析的原文不进 as_dict"



# --------------------------------------------------------------------------
# normalize_sql：「只改了参数」的判断不受差异说明改动影响
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("a,b", [
    ("SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'",
     "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-10-01' AND paid_at < '2026-11-01'"),
    ("WITH p AS (SELECT * FROM orders WHERE status = 'paid' AND amount > 100) SELECT store_id, SUM(amount) FROM p "
     "GROUP BY store_id HAVING SUM(amount) > 1000 ORDER BY 2 DESC LIMIT 5",
     "WITH p AS (SELECT * FROM orders WHERE status = 'done' AND amount > 50) SELECT store_id, SUM(amount) FROM p "
     "GROUP BY store_id HAVING SUM(amount) > 10 ORDER BY 2 DESC LIMIT 20"),
    ("SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM orders WHERE status IN ('paid', 'done'))",
     "SELECT COUNT(*) FROM users u WHERE u.id IN (SELECT user_id FROM orders WHERE status IN ('open', 'void'))"),
    ("SELECT o.id, CASE WHEN o.amount > 100 THEN 'big' ELSE 'small' END FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id AND r.amount > 0 WHERE o.created_at BETWEEN '2026-09-01' AND '2026-09-30'",
     "SELECT o.id, CASE WHEN o.amount > 200 THEN 'huge' ELSE 'tiny' END FROM orders o LEFT JOIN refunds r "
     "ON r.order_id = o.id AND r.amount > 5 WHERE o.created_at BETWEEN '2026-10-01' AND '2026-10-31'"),
], ids=["dates", "cte-having-limit", "in-list", "case-join-between"])
def test_only_parameters_changed_is_still_the_same_template(dialect, a, b):
    """定时运行每期只换日期、阈值：规范文本相同（同一个模板），字面量各是各的；同时 explain_sql_diff 照样说两条
    不等价（取值不同就是不同的口径）。"""
    na, nb = normalize_sql(a, dialect), normalize_sql(b, dialect)
    assert na.parse_error is None and na.same_shape(nb) and na.literals != nb.literals
    assert not explain_sql_diff(a, b, dialect).equivalent


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("b", [
    "SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01' GROUP BY store_id",
    "SELECT SUM(amount) FROM orders WHERE paid_at > '2026-09-01' GROUP BY store_id",
    "SELECT AVG(amount) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY store_id",
    "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND is_deleted = 0 GROUP BY store_id",
    "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY user_id",
    "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01'",
    "SELECT SUM(amount) FROM orders_archive WHERE paid_at >= '2026-09-01' GROUP BY store_id",
    "SELECT SUM(DISTINCT amount) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY store_id",
    "SELECT SUM(amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE paid_at >= '2026-09-01' "
    "GROUP BY store_id",
    "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY store_id ORDER BY 1",
    "SELECT SUM(amount) FROM orders WHERE paid_at IN ('2026-09-01') GROUP BY store_id",
    "WITH t AS (SELECT * FROM orders) SELECT SUM(amount) FROM t WHERE paid_at >= '2026-09-01' GROUP BY store_id",
])
def test_a_structural_change_is_never_the_same_template(dialect, b):
    a = "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' GROUP BY store_id"
    assert not normalize_sql(a, dialect).same_shape(normalize_sql(b, dialect)), b


# --------------------------------------------------------------------------
# explain_sql_diff：等价怎么判
# --------------------------------------------------------------------------


def check_shape(diff: SqlDiff) -> None:
    """SqlDiff 自己的承诺：等价才没有条目；不等价一定有条目，兜底时最后一条是 aspect=other，并给出不同的行。"""
    assert isinstance(diff, SqlDiff)
    if diff.equivalent:
        assert diff.items == [] and not diff.fallback and diff.text_diff == ()
        return
    assert diff.items and diff.text_diff and len(diff.text_diff) <= 40
    assert all(line[:2] in ("- ", "+ ") for line in diff.text_diff)
    if diff.fallback:
        assert diff.items[-1].aspect == "other" and diff.items[-1].text in (FALLBACK, FALLBACK_REST)
    else:
        assert all(d.aspect != "other" for d in diff.items)
    # 不自相矛盾：同一句话不说两遍，配成一对的两边写法不会一样
    assert len({d.text for d in diff.items}) == len(diff.items)
    assert all(d.a != d.b for d in diff.items if isinstance(d.a, str) and isinstance(d.b, str))


@pytest.mark.parametrize("dialect,a,b", [
    ("sqlite", "SELECT SUM(o.amount) FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted = 0",
     "select sum(amount) from orders where 0 = is_deleted and '2026-09-01' <= paid_at"),
    ("sqlite", "WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT COUNT(DISTINCT v.order_id) FROM v",
     "SELECT COUNT(DISTINCT order_id) FROM orders WHERE is_deleted = 0"),
    ("sqlite", "SELECT SUM(o.amount) FROM orders o, stores s WHERE o.store_id = s.id AND s.city = '虚构市'",
     "SELECT SUM(orders.amount) FROM orders INNER JOIN stores ON stores.id = orders.store_id "
     "WHERE stores.city = '虚构市'"),
    ("sqlite", "SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10 OFFSET 0",
     "SELECT o.amount FROM orders o ORDER BY o.amount DESC LIMIT 10"),
    ("postgres", "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) AS agg "
     "WHERE agg.total > 100"),
    ("mysql", "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) r "
     "ON r.order_id = o.id WHERE r.n > 1",
     "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) "
     "`退款` ON o.id = `退款`.order_id WHERE `退款`.n > 1"),
    ("postgres", "SELECT SUM(amount) FROM orders WHERE user_id IN "
     "(WITH t AS (SELECT id FROM users WHERE grade >= 3) SELECT id FROM t)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN "
     "(WITH g AS (SELECT id FROM users WHERE grade >= 3) SELECT g.id FROM g)"),
    ("postgres", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("mysql", "SELECT SUM(`amount`) FROM `orders` WHERE `status` = 'paid'",
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
    ("postgres", 'SELECT SUM("amount") FROM "orders" WHERE "status" = \'paid\'',
     "SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
    ("postgres", "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT * FROM (SELECT user_id FROM orders UNION SELECT user_id FROM refunds) t"),
    # UNION ALL 调换各支：行一样多、一样的值，各方言都认得出（SQLite 只限整条语句最外层、不排序的，见下面的反例）
    ("sqlite", "SELECT user_id FROM orders WHERE amount > 1 UNION ALL SELECT user_id FROM refunds",
     "SELECT user_id FROM refunds UNION ALL SELECT user_id FROM orders WHERE amount > 1"),
    ("postgres", "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds ORDER BY 1",
     "SELECT user_id FROM refunds UNION ALL SELECT user_id FROM orders ORDER BY 1"),
    ("mysql", "SELECT COUNT(*) FROM (SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds) t",
     "SELECT COUNT(*) FROM (SELECT user_id FROM refunds UNION ALL SELECT user_id FROM orders) t"),
], ids=["aliases-and-flips", "filter-in-cte", "comma-join", "offset-0", "cte-vs-subquery",
        "mysql-chinese-alias", "cte-inside-in", "left-vs-right", "mysql-backticks", "pg-quotes", "pg-union-wrapped",
        "sqlite-union-all-branches", "pg-union-all-order-by-position", "mysql-union-all-in-subquery"])
def test_rewrites_that_keep_the_meaning_are_equivalent(dialect, a, b):
    diff = explain_sql_diff(a, b, dialect)
    check_shape(diff)
    assert diff.equivalent and diff.level in ("optimizer", "light", "parsed")
    assert texts(a, b, dialect) == []


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.DIALECT_SAME, ids=[f"{n}-{d}-{i}" for i, (n, d, _, _)
                                                                          in enumerate(sql_pairs.DIALECT_SAME)])
def test_rewrites_from_earlier_rounds_are_recognized(name, dialect, a, b):
    """旧测试里「写法不同、意思一样」的对（各方言，SQLite 以外的不能执行，只在这里查）：都认得出是等价的。"""
    diff = explain_sql_diff(a, b, dialect)
    check_shape(diff)
    assert diff.equivalent, name


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.REGRESSIONS, ids=[f"{n}-{d}-{i}" for i, (n, d, _, _)
                                                                         in enumerate(sql_pairs.REGRESSIONS)])
def test_differences_reported_in_earlier_rounds_are_never_called_equivalent(name, dialect, a, b):
    """过去五轮复验、旧测试报过的每一处不同：一律不判等价，条目不为空、不自相矛盾；换个先后问也一样。第 5 轮回退的
    「只在 IN / EXISTS / 标量子查询里用到的 CTE 里的不同」也在里面。"""
    for x, y in ((a, b), (b, a)):
        diff = explain_sql_diff(x, y, dialect, labels=LABELS)
        check_shape(diff)
        assert not diff.equivalent, name


@pytest.mark.parametrize("dialect,a,b,why", [
    ("sqlite", "SELECT o.id, r.flag FROM orders o LEFT JOIN (SELECT order_id, 1 AS flag FROM refunds) r "
     "ON r.order_id = o.id",
     "SELECT o.id, 1 AS flag FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "merge_subqueries 会把外连接可空一侧的常量列并进外层：没关联上的行本该是空值"),
    ("sqlite", "SELECT id FROM orders WHERE status = 'paid' OR amount > (SELECT amount FROM refunds WHERE id = 999)",
     "SELECT orders.id FROM orders CROSS JOIN (SELECT amount FROM refunds WHERE id = 999) AS q "
     "WHERE orders.status = 'paid' OR orders.amount > q.amount",
     "unnest_subqueries 把 OR 里的标量子查询改成 CROSS JOIN：子查询没有行时整条都没了"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
     "SELECT COUNT(*) FROM orders WHERE paid_at >= CAST('2026-09-01' AS DATE)",
     "canonicalize 给日期比较加 CAST；SQLite 里 CAST(… AS DATE) 取的是数字"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE CAST(paid_at AS DATE) >= '2026-09-01'",
     "SELECT COUNT(*) FROM orders WHERE DATE(paid_at) >= '2026-09-01'",
     "sqlglot 生成 SQLite 时把 CAST(x AS DATE) 写成 DATE(x)：比规范形式不能比生成的文本"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE +amount = '10'", "SELECT COUNT(*) FROM orders WHERE amount = '10'",
     "一元加号在 SQLite 里去掉了类型亲和，解析时却被丢掉了"),
    ("postgres", "SELECT COUNT(*) FROM orders WHERE status > 'b' AND status > 'O'",
     "SELECT COUNT(*) FROM orders WHERE status > 'b'",
     "字符串比较的强弱取决于排序规则：simplify 的这条化简不做"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE region > 10 AND region > 9",
     "SELECT COUNT(*) FROM orders WHERE region > 10", "TEXT 列和数字比是按文本比：不合并同一列上的比较"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE amount + 1 = 101", "SELECT COUNT(*) FROM orders WHERE amount = 100",
     "等式两边移项对 TEXT 列不成立"),
    ("sqlite", "SELECT user_id, 1 AS total FROM orders GROUP BY user_id ORDER BY total DESC, user_id LIMIT 3",
     "SELECT user_id, 1 AS total FROM orders GROUP BY user_id ORDER BY 1 DESC, user_id LIMIT 3",
     "ORDER BY 别名换成常量 1 就成了按第一列排"),
    ("sqlite", "SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT * FROM (SELECT user_id FROM orders UNION SELECT user_id FROM refunds) t",
     "SQLite 从子查询读 UNION 时按第一支的类型亲和转换别的支的值"),
    ("sqlite", "SELECT COUNT(*) FROM orders o WHERE o.id NOT IN (SELECT order_id FROM refunds)",
     "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "NOT IN 遇到空值整个不成立，NOT EXISTS 不会"),
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.id IS NULL OR r.amount > 5",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE r.id IS NULL OR r.amount > 5",
     "IS NULL 留得下没关联上的行：这次外连接不是内连接"),
    # 合并的各支调换先后：下面这些都会改变结果（复验压测时撞到的是第一种，SQLite 里留下的是 2 还是 2.0）
    ("sqlite", "SELECT tax FROM orders UNION SELECT referrer_id FROM orders",
     "SELECT referrer_id FROM orders UNION SELECT tax FROM orders",
     "UNION 去重时留先出现的那个值：SQLite 里 REAL 列的 2.0 和 INTEGER 列的 2 算重复，留下哪个取决于哪支在前"),
    ("mysql", "SELECT city FROM stores UNION SELECT city FROM users",
     "SELECT city FROM users UNION SELECT city FROM stores",
     "MySQL 默认的排序规则不分大小写，'SH' 和 'sh' 去重时留先出现的那个"),
    ("postgres", "SELECT amount FROM orders INTERSECT SELECT amount FROM refunds",
     "SELECT amount FROM refunds INTERSECT SELECT amount FROM orders",
     "INTERSECT 留左边那支的值：numeric 的 2 和 2.00 相等，写出来却不一样"),
    ("postgres", "SELECT id FROM orders UNION ALL SELECT id FROM refunds LIMIT 3",
     "SELECT id FROM refunds UNION ALL SELECT id FROM orders LIMIT 3",
     "没有 ORDER BY 的 LIMIT 取先出来的几行：各支调换先后，取到的就是另一张表的行"),
    ("postgres", "SELECT id, user_id FROM orders UNION ALL SELECT user_id, id FROM refunds ORDER BY id",
     "SELECT user_id, id FROM refunds UNION ALL SELECT id, user_id FROM orders ORDER BY id",
     "合并结果的列名取第一支的：调换之后 ORDER BY id 排的是另一列"),
    ("sqlite", "SELECT name FROM users UNION ALL SELECT city FROM stores ORDER BY 1",
     "SELECT city FROM stores UNION ALL SELECT name FROM users ORDER BY 1",
     "SQLite 给合并的结果排序时用最左一支那列声明的排序规则（COLLATE NOCASE 的列在前就不分大小写）"),
    ("sqlite", "SELECT COUNT(*) FROM (SELECT name AS v FROM users UNION ALL SELECT city AS v FROM stores) t "
     "WHERE v = 'sh'",
     "SELECT COUNT(*) FROM (SELECT city AS v FROM stores UNION ALL SELECT name AS v FROM users) t WHERE v = 'sh'",
     "SQLite 从子查询读合并的结果再比较时，列的排序规则也取最左一支的"),
    ("sqlite", "SELECT COUNT(*) FROM users u JOIN stores s ON u.name COLLATE NOCASE = s.city COLLATE BINARY",
     "SELECT COUNT(*) FROM users u JOIN stores s ON s.city COLLATE BINARY = u.name COLLATE NOCASE",
     "比较的两边都写了 COLLATE 时 SQLite 用左边那个：两边不能对调"),
    # 别名和名字：qualify 没有表结构时把 SELECT 列表里和前面别名同名的名字当成别名展开，库里却不认
    ("sqlite", "SELECT amount * 2 AS x, x + 1 AS y FROM orders",
     "SELECT amount * 2 AS x, amount * 2 + 1 AS y FROM orders",
     "SELECT 列表里不能引用前面列的别名：x 不是表的列，前一条在库里报错"),
    ("sqlite", "SELECT user_id AS amount, amount AS user_id FROM orders ORDER BY amount",
     "SELECT user_id AS amount, amount AS user_id FROM orders ORDER BY user_id",
     "ORDER BY 里单独一个名字先认别名：两条排的是不同的列（第二列的 amount 是表的列）"),
    # 取到哪几行、哪一行的值 SQL 本来就没规定的：库里看扫描的先后，调换关联先后就变了（压测撞到的是第一种）
    ("sqlite", "SELECT o.created_at, COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id GROUP BY o.store_id",
     "SELECT o.created_at, COUNT(*) FROM refunds r JOIN orders o ON r.order_id = o.id GROUP BY o.store_id",
     "聚合查询里既不分组也不聚合的列，SQLite 取组里任意一行的值"),
    ("sqlite", "SELECT o.id, r.id FROM orders o JOIN refunds r ON r.order_id = o.id LIMIT 5",
     "SELECT o.id, r.id FROM refunds r JOIN orders o ON r.order_id = o.id LIMIT 5",
     "没有 ORDER BY 的 LIMIT 取先扫到的几行"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT DISTINCT user_id FROM refunds LIMIT 2)",
     "SELECT COUNT(*) FROM orders WHERE user_id IN (SELECT user_id FROM refunds GROUP BY user_id LIMIT 2)",
     "没有 ORDER BY 的 LIMIT：DISTINCT 按扫到的先后出行，GROUP BY 按分组键排好了出，取到的两个不同"),
    ("sqlite", "SELECT r.id, o.id FROM refunds r RIGHT JOIN orders o ON r.order_id = o.id LIMIT 5",
     "SELECT r.id, o.id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id LIMIT 5",
     "没有 ORDER BY 的 LIMIT：RIGHT JOIN 改写成 LEFT JOIN，扫描的先后就变了"),
], ids=["nullable-merge", "unnest-or", "cast-date", "cast-vs-date", "unary-plus", "collation", "text-number",
        "equality-move", "order-by-constant", "sqlite-union-affinity", "not-in-null", "outer-join-is-null",
        "union-dedup-storage-class", "union-dedup-collation", "intersect-numeric-scale", "union-all-limit",
        "union-all-order-by-name", "sqlite-union-all-order-collation", "sqlite-nested-union-all", "collate-sides",
        "select-list-alias-ref", "order-by-swapped-aliases", "loose-column-join-order", "limit-join-order",
        "limit-distinct-vs-group", "limit-right-vs-left"])
def test_rewrites_that_change_the_meaning_are_not_equivalent(dialect, a, b, why):
    """这些写法看着能改写成同一个样子，其实结果可能不同（why）：sqlglot 优化器里有的改写不保持语义，规范化不用它们，
    或者守住了用法。"""
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(a, b, dialect, schema=schema)
        check_shape(diff)
        assert not diff.equivalent, why


def test_the_schema_expands_select_star_and_qualifies_columns():
    """给了表结构（数据源的 schema_cache）才认得出 SELECT * 和列全写出来是一回事、几张表时不写表名的列是哪张表的。"""
    schema = sql_pairs.schema_cache()
    star = "SELECT * FROM stores"
    listed = "SELECT id, name, city, region, owner_id FROM stores"
    assert explain_sql_diff(star, listed, "sqlite", schema=schema).equivalent
    assert not explain_sql_diff(star, listed, "sqlite").equivalent
    a = "SELECT SUM(amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE city = 'SH'"
    b = "SELECT SUM(o.amount) FROM stores s JOIN orders o ON o.store_id = s.id WHERE s.city = 'SH'"
    assert explain_sql_diff(a, b, "sqlite", schema=schema).equivalent


def test_an_alias_used_in_group_by_needs_the_schema_to_be_told_from_a_column():
    """WHERE、GROUP BY、HAVING 里的名字库里先认表的列、没有这一列才认别名：GROUP BY r 里的 r 是不是 orders 的列，
    给了表结构才知道。给了就认得出和按 region 分组是一回事；没给不猜（DATE(created_at) AS created_at 再 GROUP BY
    created_at 按的是原来的时间列，当成别名就成了按日期分组）。"""
    a = "SELECT o.region, SUM(o.amount) FROM orders o GROUP BY o.region HAVING SUM(o.amount) > 100"
    b = "SELECT region AS r, SUM(amount) AS s FROM orders GROUP BY r HAVING s > 100"
    assert explain_sql_diff(a, b, "sqlite", schema=sql_pairs.schema_cache()).equivalent
    assert not explain_sql_diff(a, b, "sqlite").equivalent
    shadow = "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY created_at"
    by_day = "SELECT DATE(created_at) AS created_at, COUNT(*) FROM orders GROUP BY DATE(created_at)"
    for schema in (None, sql_pairs.schema_cache()):
        assert not explain_sql_diff(shadow, by_day, "sqlite", schema=schema).equivalent


def test_equivalence_comes_with_the_level_it_was_decided_at():
    diff = explain_sql_diff("SELECT COUNT(1) FROM orders", "select count(*) from ORDERS", "sqlite")
    assert diff.equivalent and diff.level == "optimizer"
    data = json.loads(json.dumps(diff.as_dict(), ensure_ascii=False))
    assert data == {"equivalent": True, "items": [], "fallback": False, "text_diff": [], "level": "optimizer"}


# --------------------------------------------------------------------------
# explain_sql_diff：有把握的具体说法
# --------------------------------------------------------------------------

BASE = "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"


def test_time_column_differs_with_catalog_names():
    a = BASE
    b = "SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
    diff = explain_sql_diff(a, b, names={"orders.paid_at": "支付时间", "created_at": "下单时间"})
    check_shape(diff)
    [item] = diff.items
    assert item.aspect == "time" and item.text == "A 按支付时间 paid_at 筛时间，B 按下单时间 created_at"
    assert (item.a, item.b) == ("paid_at", "created_at")
    assert item.detail == {"a": [{"table": "orders", "column": "paid_at"}],
                           "b": [{"table": "orders", "column": "created_at"}]}
    assert texts(a, b) == ["A 按 paid_at 筛时间，B 按 created_at"], "没有中文名就只写列名"


@pytest.mark.parametrize("a,b,expected", [
    # 时间
    (BASE, "SELECT SUM(amount) FROM orders", [("time", "A 按 paid_at 筛时间，B 没有按时间筛选")]),
    ("SELECT SUM(amount) FROM orders", BASE, [("time", "A 没有按时间筛选，B 按 paid_at 筛时间")]),
    (BASE, "SELECT SUM(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'",
     [("time_range", "A 的时间范围是 paid_at >= '2026-09-01' AND paid_at < '2026-10-01'，"
                     "B 是 paid_at >= '2026-09-01' AND paid_at <= '2026-09-30'")]),
    ("WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(created_at) AS d FROM orders) SELECT SUM(amount) FROM t "
     "WHERE d >= '2026-09-01'",
     [("time", "A 按 paid_at 筛时间，B 按 created_at")]),
    # 筛选
    ("SELECT SUM(amount) FROM orders WHERE is_deleted <> 1", "SELECT SUM(amount) FROM orders",
     [("filter", "A 排除了 is_deleted = 1 的行和 is_deleted 为空的行，B 没有")]),
    ("SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders WHERE refund_id IS NOT NULL",
     [("filter", "B 排除了 refund_id 为空的行，A 没有")]),
    ("SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders WHERE status = 'paid'",
     [("filter", "B 只保留 status = 'paid' 的行，A 没有这个条件")]),
    ("SELECT SUM(amount) FROM orders WHERE status = 'paid'",
     "SELECT SUM(amount) FROM orders WHERE status IN ('paid', 'shipped')",
     [("filter", "A 按 status = 'paid' 筛选，B 按 status IN ('paid', 'shipped') 筛选")]),
    ("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(amount) FROM paid",
     "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(amount) FROM orders",
     [("filter", "A 只保留 status = 'paid' 的行，B 没有这个条件")]),
    # 聚合、去重
    ("SELECT COUNT(DISTINCT order_id) FROM orders", "SELECT COUNT(order_id) FROM orders",
     [("aggregate", "A 对 order_id 去重计数，B 没去重")]),
    ("SELECT COUNT(order_id) FROM orders", "SELECT COUNT(DISTINCT order_id) FROM orders",
     [("aggregate", "A 对 order_id 计数时没去重，B 去重计数")]),
    ("SELECT SUM(amount) FROM orders", "SELECT AVG(amount) FROM orders", [("aggregate", "A 对 amount 求和，B 求平均")]),
    ("SELECT SUM(paid_amount) FROM orders", "SELECT SUM(amount) FROM orders",
     [("aggregate", "A 对 paid_amount 求和，B 对 amount 求和")]),
    ("SELECT COUNT(*) FROM orders", "SELECT COUNT(DISTINCT order_id) FROM orders",
     [("aggregate", "A 数的是行数，B 对 order_id 去重计数")]),
    ("SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     [("aggregate", "A 对 orders.amount 求和，B 对 refunds.amount 求和")]),
    ("SELECT DISTINCT region FROM orders", "SELECT region FROM orders", [("group_by", "A 对 region 去重，B 没有去重")]),
    ("SELECT user_id, SUM(amount) FROM orders GROUP BY user_id", "SELECT SUM(amount), user_id FROM orders GROUP BY user_id",
     [("expression", "两边选出来的列一样，先后不同：A 是 user_id、SUM(amount)，B 是 SUM(amount)、user_id")]),
    # 分组、HAVING、取行
    ("SELECT region, SUM(amount) FROM orders GROUP BY region",
     "SELECT region, city, SUM(amount) FROM orders GROUP BY region, city",
     [("group_by", "A 按 region 分组，B 按 city、region 分组")]),
    ("SELECT SUM(amount) FROM orders", "SELECT region, SUM(amount) FROM orders GROUP BY region",
     [("group_by", "A 没有分组，B 按 region 分组")]),
    ("SELECT region, SUM(amount) FROM orders GROUP BY region HAVING SUM(amount) > 100",
     "SELECT region, SUM(amount) FROM orders GROUP BY region",
     [("having", "A 分组后只保留 SUM(amount) > 100 的组，B 没有这个条件")]),
    ("SELECT amount FROM orders LIMIT 10", "SELECT amount FROM orders", [("limit", "A 只取前 10 行，B 不限行数")]),
    ("SELECT amount FROM orders LIMIT 10", "SELECT amount FROM orders LIMIT 100",
     [("limit", "A 只取前 10 行，B 只取前 100 行")]),
    ("SELECT amount FROM orders LIMIT 10 OFFSET 0", "SELECT amount FROM orders LIMIT 10 OFFSET 10",
     [("limit", "A 不跳过行，B 跳过前 10 行")]),
    ("SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10", "SELECT amount FROM orders ORDER BY 1 ASC LIMIT 10",
     [("limit", "A 取行前按 amount 降序排，B 按 amount 升序排")]),
    ("SELECT id, amount FROM orders ORDER BY amount DESC", "SELECT id, amount FROM orders",
     [("order", "A 按 amount 降序排，B 没有排序")]),
    # 连接、表
    ("SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     [("join", "A 用 LEFT JOIN 关联 refunds 表，B 用 INNER JOIN")]),
    ("SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.user_id = o.user_id",
     [("join", "A 关联 refunds 表的条件是 orders.id = refunds.order_id，B 是 orders.user_id = refunds.user_id")]),
    ("SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id", "SELECT SUM(amount) FROM orders",
     [("join", "A 用 LEFT JOIN 关联 refunds 表，B 没有")]),
    ("SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM stores s JOIN orders o ON o.store_id = s.id WHERE o.status = 'paid'",
     [("join", "B 关联了 stores 表（关联的条件是 orders.store_id = stores.id），A 没有")]),
    ("SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id",
     [("join", "A 以 orders 为主表外连接 refunds，B 以 refunds 为主表外连接 orders")]),
    ("SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'",
     "SELECT SUM(amount) FROM orders_archive WHERE is_deleted = 1 AND paid_at >= '2026-09-01'",
     [("tables", "A 查的是 orders 表，B 查的是 orders_archive 表"),
      ("filter", "A 按 is_deleted = 0 筛选，B 按 is_deleted = 1 筛选")]),
    # UNION
    ("SELECT user_id FROM orders UNION SELECT user_id FROM refunds",
     "SELECT user_id FROM orders UNION ALL SELECT user_id FROM refunds",
     [("set_op", "A 用 UNION 合并（去掉重复的行），B 用 UNION ALL（重复的行都留着）")]),
    ("SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders WHERE status = 'paid'",
     "SELECT user_id FROM orders WHERE status = 'paid' UNION SELECT referrer_id FROM orders",
     [("filter", "A 在合并的一支里只保留 status = 'paid' 的行，B 在合并的一支里没有这个条件")]),
], ids=lambda v: None)
def test_specific_differences_are_said_in_one_sentence_each(a, b, expected):
    diff = explain_sql_diff(a, b, "sqlite")
    check_shape(diff)
    assert not diff.fallback and [(d.aspect, d.text) for d in diff.items] == expected


def test_a_difference_inside_a_cte_used_only_in_a_condition_subquery_is_reported():
    """第 5 轮回退的例子：只在 IN 子查询里用到的 CTE 里的条件不同，以前整个漏报（返回空列表）。现在 CTE 并进了 IN
    子查询，往里比，说得出是哪个条件，位置带上 IN 左边的列（两个 IN 子查询时才分得清）。EXISTS、标量子查询一样。"""
    head = "WITH p AS (SELECT * FROM orders WHERE status = '{}') SELECT COUNT(*) FROM users u WHERE "
    for tail, place in (("u.id IN (SELECT user_id FROM p)", "在 id IN 的子查询里"),
                        ("EXISTS (SELECT 1 FROM p WHERE p.user_id = u.id)", "在 EXISTS 子查询里"),
                        ("(SELECT COUNT(*) FROM p WHERE p.user_id = u.id) > 0", "在条件的子查询里")):
        a, b = head.format("paid") + tail, head.format("open") + tail
        for schema in (None, sql_pairs.schema_cache()):
            diff = explain_sql_diff(a, b, "sqlite", schema=schema, labels=LABELS)
            check_shape(diff)
            assert [(d.aspect, d.text) for d in diff.items] == [
                ("filter", f"原流程{place}按 status = 'paid' 筛选，盲算{place}按 status = 'open' 筛选")]
        assert diff_facets(facets(a), facets(b), labels=LABELS) == diff.items


def test_a_difference_inside_an_aggregating_cte_names_the_cte_on_each_side():
    a = ("WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
         "SELECT COUNT(*) FROM user_totals WHERE total > 100")
    b = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM t WHERE total > 100")
    diff = explain_sql_diff(a, b, labels=LABELS)
    check_shape(diff)
    assert [d.text for d in diff.items] == ["原流程在 user_totals 里只保留 is_deleted = 0 的行，盲算在 t 里没有这个条件"]


def test_an_outer_join_turned_inner_by_where_says_what_was_written():
    """原文写的是 LEFT / RIGHT JOIN、WHERE 里的条件把没关联上的行筛掉了：按内连接比，说法照实写「写的是 LEFT JOIN，
    实际是 INNER JOIN」，不说成「用 INNER JOIN」。从 RIGHT JOIN 改写来的外连接说以哪一侧为主表，不说 LEFT JOIN。"""
    # 用 COUNT：PostgreSQL 里对不知道是不是精确数的值求和，结果看行出来的先后（见 _Sensitivity），这样的一段不按 WHERE
    # 把外连接改成内连接（SQLite 上降成内连接之后扫描的先后真的变了），说不出这一句
    a = "SELECT COUNT(o.id) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'"
    b = "SELECT COUNT(o.id) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE o.status = 'paid'"
    assert texts(a, b, "postgres") == [
        "A 用 LEFT JOIN 关联 refunds 表，B 写的是 LEFT JOIN，但 WHERE 里的条件把关联不上 orders 的行筛掉了，实际是 INNER JOIN"]
    a = "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id"
    b = a + " WHERE o.status = 'paid'"
    assert texts(a, b, "postgres") == [
        "A 以 refunds 为主表外连接 orders 表，B 写的是 RIGHT JOIN，但 WHERE 里的条件把关联不上 orders 的行筛掉了，"
        "实际是 INNER JOIN", "B 只保留 orders.status = 'paid' 的行，A 没有这个条件"]
    # 本来就是内连接、和被 WHERE 变成内连接的 LEFT JOIN 是同一个意思
    assert explain_sql_diff("SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.amount > 10",
                            "SELECT COUNT(*) FROM refunds r JOIN orders o ON o.id = r.order_id WHERE r.amount > 10"
                            ).equivalent


@pytest.mark.parametrize("dialect", DIALECTS)
def test_each_dialect_gives_the_same_three_findings(dialect):
    q = "`" if dialect == "mysql" else '"'
    a = (f"SELECT COUNT(DISTINCT o.order_id) AS {q}订单数{q} FROM orders o "
         "WHERE o.paid_at >= '2026-09-01' AND o.is_deleted <> 1")
    b = f"SELECT COUNT(o.order_id) AS {q}订单数{q} FROM orders o WHERE o.created_at >= '2026-09-01'"
    # <> 1 也把 is_deleted 为空的行筛掉了（第 3 轮复验：以前只说「排除了 is_deleted = 1 的行」）
    assert texts(a, b, dialect) == ["A 按 paid_at 筛时间，B 按 created_at",
                                    "A 排除了 is_deleted = 1 的行和 is_deleted 为空的行，B 没有",
                                    "A 对 order_id 去重计数，B 没去重"]


def test_side_labels_in_chinese_have_no_stray_spaces():
    a = "SELECT COUNT(DISTINCT order_id) FROM orders WHERE paid_at >= '2026-09-01'"
    b = "SELECT COUNT(order_id) FROM orders WHERE created_at >= '2026-09-01'"
    assert texts(a, b, labels=LABELS) == ["原流程按 paid_at 筛时间，盲算按 created_at", "原流程对 order_id 去重计数，盲算没去重"]


def test_filters_on_real_columns_carry_the_columns_for_the_catalog():
    """筛选条件只涉及的实表列记在 detail 里：调用方据此对照数据目录里的有效行过滤。"""
    [item] = explain_sql_diff("SELECT SUM(o.amount) FROM orders o WHERE o.is_deleted = 0",
                              "SELECT SUM(amount) FROM orders").items
    assert item.aspect == "filter" and item.detail == {"columns": [{"table": "orders", "column": "is_deleted"}]}


def test_a_displayed_condition_means_what_the_sql_means():
    """给人看的条件按方言生成；方言生成的写法读回来不是同一个意思时（SQLite 把 CAST(x AS DATE) 生成 DATE(x)）改用
    通用写法，不拿 DATE(paid_at) 冒充原文。几个条件连起来时 OR 带上括号。"""
    [item] = explain_sql_diff("SELECT COUNT(*) FROM orders WHERE CAST(paid_at AS DATE) >= '2026-09-01'",
                              "SELECT COUNT(*) FROM orders WHERE CAST(paid_at AS DATE) >= '2026-10-01'", "sqlite").items
    assert item.aspect == "time_range" and "CAST(paid_at AS DATE)" in item.text and "DATE(paid_at)" not in item.text
    a = "SELECT SUM(amount) FROM orders"
    b = "SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id AND (s.city = 'SH' OR s.city = 'BJ')"
    [item] = explain_sql_diff(a, b).items
    assert item.text == ("B 关联了 stores 表（关联的条件是 orders.store_id = stores.id AND "
                         "(stores.city = 'BJ' OR stores.city = 'SH')），A 没有")


# --------------------------------------------------------------------------
# explain_sql_diff：兜底说明
# --------------------------------------------------------------------------


def test_nothing_specific_gives_one_fallback_item_and_the_lines_that_differ():
    a = "SELECT COUNT(*) FROM orders o WHERE o.id NOT IN (SELECT order_id FROM refunds)"
    b = "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)"
    diff = explain_sql_diff(a, b, "sqlite")
    check_shape(diff)
    assert diff.fallback and diff.items == [FacetDiff("other", None, None, FALLBACK)]
    assert any(line.startswith("- ") for line in diff.text_diff)
    assert any(line.startswith("+ ") for line in diff.text_diff)
    assert diff_facets(facets(a), facets(b)) == diff.items


def test_specific_items_that_cover_only_part_end_with_the_rest_note():
    """一边多关联了 refunds（会把订单行按退款数复制），另一边只在 EXISTS 里用它：关联那一句说得准，EXISTS 那处说不准，
    补一句「另有未能归纳的差异」。"""
    a = "SELECT SUM(o.amount) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)"
    b = "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"
    diff = explain_sql_diff(a, b, labels=LABELS)
    check_shape(diff)
    assert diff.fallback and [d.text for d in diff.items] == [
        "盲算关联了 refunds 表（关联的条件是 orders.id = refunds.order_id），原流程没有关联它（只在子查询里用到了 refunds）",
        FALLBACK_REST]


def test_columns_inside_a_cte_are_not_compared_because_canonical_forms_prune_them():
    """规范化会去掉 CTE 里没用到的列、重排剩下的：CTE 里选出来的列不同不说具体的（原文里两边的 CTE 都选了三列，
    说「A 在 t 里选出 2 列」就是错的），交给兜底说明。"""
    a = ("SELECT COUNT(*) FROM (SELECT id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at, id) AS rn "
         "FROM orders) t WHERE t.rn = 1 AND t.status = 'paid'")
    b = ("SELECT COUNT(*) FROM (SELECT id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at, id) AS rn "
         "FROM orders WHERE status = 'paid') t WHERE t.rn = 1")
    diff = explain_sql_diff(a, b)
    check_shape(diff)
    assert diff.fallback and not [d for d in diff.items if "选出" in d.text]


def test_differences_that_cannot_be_checked_get_only_the_fallback():
    """两边算法不同、又判断不了是不是真的不同的（ROUND 包了一层、CASE 和 COALESCE、两种写法的时间常量），不说具体的：
    「A 算的是 X，B 算的是 Y」读起来就是两边不同，说错了比不说更糟。"""
    for a, b in (("SELECT SUM(amount) FROM orders", "SELECT ROUND(SUM(amount), 2) FROM orders"),
                 ("SELECT SUM(COALESCE(discount, 0)) FROM orders",
                  "SELECT SUM(CASE WHEN discount IS NULL THEN 0 ELSE discount END) FROM orders"),
                 ("SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
                  "SELECT COUNT(*) FROM orders WHERE paid_at >= DATE('2026-09-01')")):
        diff = explain_sql_diff(a, b, "sqlite")
        check_shape(diff)
        assert diff.fallback and diff.items == [FacetDiff("other", None, None, FALLBACK)], (a, b, diff.items)


def test_text_diff_lists_at_most_forty_lines():
    cols_a = ", ".join(f"SUM(amount * {i}) AS c{i}" for i in range(60))
    cols_b = ", ".join(f"SUM(amount * {i + 100}) AS c{i}" for i in range(60))
    diff = explain_sql_diff(f"SELECT {cols_a} FROM orders", f"SELECT {cols_b} FROM orders")
    assert not diff.equivalent and len(diff.text_diff) == 40


@pytest.mark.parametrize("a,b", [
    ("SELECT FROM WHERE", BASE),
    (BASE, "SELECT 1; SELECT 2"),
    ("", BASE),
    (None, BASE),
    (BASE, "UPDATE orders SET amount = 0"),
])
def test_an_unparsable_side_gives_one_parse_item_and_never_raises(a, b):
    diff = explain_sql_diff(a, b, "sqlite")
    assert not diff.equivalent and len(diff.items) == 1 and diff.items[0].aspect == "parse"
    assert "没有逐项比较" in diff.items[0].text
    json.dumps(diff.as_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------
# diff_facets：照旧收 facets，交给 explain_sql_diff
# --------------------------------------------------------------------------


def test_diff_facets_returns_the_items_of_explain_sql_diff():
    pairs = [(BASE, "SELECT SUM(amount) FROM orders"),
             ("SELECT COUNT(DISTINCT order_id) FROM orders", "SELECT COUNT(order_id) FROM orders"),
             ("SELECT COUNT(*) FROM orders o WHERE o.id NOT IN (SELECT order_id FROM refunds)",
              "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)")]
    for a, b in pairs:
        for dialect in DIALECTS:
            expected = explain_sql_diff(a, b, dialect, labels=LABELS, names={"paid_at": "支付时间"}).items
            assert diff_facets(facets(a, dialect), facets(b, dialect), labels=LABELS,
                               names={"paid_at": "支付时间"}) == expected


def test_diff_facets_passes_the_schema_given_to_facets():
    schema = sql_pairs.schema_cache()
    a = "SELECT SUM(amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE city = 'SH'"
    b = "SELECT SUM(o.amount) FROM stores s JOIN orders o ON o.store_id = s.id WHERE s.city = 'SH'"
    assert diff_facets(facets(a, "sqlite", schema=schema), facets(b, "sqlite", schema=schema)) == []
    assert diff_facets(facets(a, "sqlite"), facets(b, "sqlite")) != []


def test_an_unparsable_side_is_reported_once_without_item_by_item_comparison():
    [diff] = diff_facets(facets("SELECT FROM WHERE"), facets(BASE))
    assert diff.aspect == "parse" and diff.text.startswith("A：SQL 无法解析") and "没有逐项比较" in diff.text
    assert diff.b is None


def test_facets_without_the_original_sql_cannot_be_called_equivalent():
    """不是 facets 算出来的 Facets（没记原文）比不了原文：facets 记的结构不全，两份一样也不说明 SQL 一样，只有同一个
    对象才算没有不同，否则给兜底说明，不猜。"""
    f = Facets(dialect="sqlite")
    assert diff_facets(f, f) == []
    for other in (Facets(dialect="sqlite"), Facets(dialect="sqlite", limit="10")):
        assert diff_facets(f, other) == [FacetDiff("other", None, None, FALLBACK)]


def test_catalog_labels_come_from_visible_catalog_items():
    notes = {
        "orders": {"columns": {"paid_at": {"label": {"value": "支付时间", "status": "confirmed"}},
                               "status": {"label": {"value": "状态", "status": "proposed"}},
                               "amount": {"label": {"value": "错的名字", "status": "rejected"}}}},
        "refunds": {"columns": {"status": {"label": {"value": "退款状态", "status": "confirmed"}}}},
    }
    labels = catalog_labels(notes)
    assert labels["orders.paid_at"] == labels["paid_at"] == "支付时间"
    assert labels["orders.status"] == "状态" and labels["refunds.status"] == "退款状态"
    assert "status" not in labels, "两张表叫法不同的列不按光列名记"
    assert "orders.amount" not in labels and "amount" not in labels, "被驳回的不算"


def test_a_facets_object_compares_with_itself_cleanly():
    f = facets("WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT COUNT(DISTINCT order_id) FROM v")
    assert isinstance(f, Facets) and diff_facets(f, f) == []


# --------------------------------------------------------------------------
# 第 2 轮复验的反例：等价的结论、具体说法的可靠性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.REGRESSIONS_R2,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.REGRESSIONS_R2])
def test_round_two_counterexamples_are_never_equivalent(name, dialect, a, b):
    """第 2 轮复验在 PostgreSQL / MySQL 上跑出结果不同的对：常量折叠（Decimal 和浮点数）、取两次负、HAVING 挪进 WHERE
    （不分大小写的排序规则下同组的值各不一样）、ORDER BY 里有聚合的 EXISTS / IN、列名表、合并的各支调换、BETWEEN
    SYMMETRIC、有并列的取行、DISTINCT ON。不管给不给表结构、谁先谁后，一律不判等价。"""
    for schema in (None, sql_pairs.schema_cache()):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            check_shape(diff)
            assert not diff.equivalent, name


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.DIALECT_SAME_R2,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.DIALECT_SAME_R2])
def test_round_two_rewrites_are_recognized(name, dialect, a, b):
    """FETCH FIRST 和 LIMIT、MySQL 列名的大小写、各方言 IS NOT NULL / NOT LIKE 的不同解析形状、MySQL 的 IF 和 CASE：
    认得出是一回事（以前把 FETCH 读成「不限行数」）。"""
    diff = explain_sql_diff(a, b, dialect)
    check_shape(diff)
    assert diff.equivalent, (name, diff.items)


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.KNOWN_UNRECOGNIZED,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.KNOWN_UNRECOGNIZED])
def test_equivalences_left_unrecognized_on_purpose_get_only_the_fallback(name, dialect, a, b):
    """意思一样、规范化故意不认的（要假设「相等就是一样」才认得出）：只给兜底说明，不说具体的不同。"""
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(a, b, dialect, schema=schema)
        check_shape(diff)
        assert diff.equivalent or all(d.aspect == "other" for d in diff.items), (name, diff.items)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("a,b", [
    ("SELECT COUNT(*) FROM orders o WHERE o.paid_at >= '2026-09-01' AND EXISTS "
     "(SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id "
     "AND o.paid_at >= '2026-09-01')"),
    ("SELECT COUNT(*) FROM orders o WHERE o.status = 'paid' AND EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT COUNT(*) FROM orders o WHERE EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND o.status = 'paid')"),
    ("SELECT COUNT(*) FROM orders WHERE status = 'paid' AND id IN (SELECT order_id FROM refunds)",
     "SELECT COUNT(*) FROM orders WHERE id IN (SELECT order_id FROM refunds WHERE status = 'paid')"),
    ("SELECT COUNT(*) FROM users u WHERE u.city = 'BJ' AND EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
     "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND u.city = 'BJ')"),
], ids=["exists-time", "exists-status", "in-outer-column", "exists-city"])
def test_a_condition_moved_into_a_correlated_subquery_is_not_called_missing(dialect, a, b):
    """条件挪进了相关子查询（引用的还是外层那一行）：两条意思一样，B 是有这个条件的。以前说「B 没有按时间筛选」
    「B 没有这个条件」，现在说不清就只给兜底说明。"""
    for schema in (None, sql_pairs.schema_cache()):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            check_shape(diff)
            assert all(d.aspect == "other" for d in diff.items), diff.items


@pytest.mark.parametrize("dialect", DIALECTS)
def test_time_filters_written_another_way_are_never_called_no_time_filter(dialect):
    """时间条件换了写法（拆成两段、IN 和 OR、多一个下界、LIKE 和区间）：另一边是按时间筛了的，不能说「没有按时间筛选」，
    也不能说成两个范围不同。"""
    base = "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    for other in ("SELECT COUNT(*) FROM orders WHERE (paid_at >= '2026-09-01' AND paid_at < '2026-09-16') OR "
                  "(paid_at >= '2026-09-16' AND paid_at < '2026-10-01')",
                  "SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01' "
                  "AND paid_at > '2026-08-01'",
                  "SELECT COUNT(*) FROM orders WHERE paid_at LIKE '2026-09%'"):
        diff = explain_sql_diff(base, other, dialect)
        check_shape(diff)
        assert not any("没有按时间筛选" in d.text or d.aspect == "time_range" for d in diff.items), diff.items
    diff = explain_sql_diff("SELECT COUNT(*) FROM orders WHERE paid_at IN ('2026-09-01', '2026-09-30')",
                            "SELECT COUNT(*) FROM orders WHERE paid_at = '2026-09-01' OR paid_at = '2026-09-30'", dialect)
    assert all(d.aspect == "other" for d in diff.items)


def test_except_with_its_sides_swapped_is_not_one_time_difference():
    """EXCEPT 两边对调，外加一边换了时间列：以前只说时间列不同、还说全了（会写进数据目录）。EXCEPT 按先后逐支比，对不上
    就只给兜底说明；子查询、CTE、IN 子查询里套着的也一样。"""
    a = "SELECT user_id FROM orders WHERE paid_at >= '2026-09-01' EXCEPT SELECT user_id FROM refunds"
    b = "SELECT user_id FROM refunds EXCEPT SELECT user_id FROM orders WHERE created_at >= '2026-09-01'"
    for x, y in ((a, b), (f"SELECT COUNT(*) FROM ({a}) t", f"SELECT COUNT(*) FROM ({b}) t"),
                 (f"WITH e AS ({a}) SELECT COUNT(*) FROM e", f"WITH e AS ({b}) SELECT COUNT(*) FROM e"),
                 (f"SELECT COUNT(*) FROM users WHERE id IN ({a})", f"SELECT COUNT(*) FROM users WHERE id IN ({b})")):
        for dialect in ("sqlite", "postgres"):
            diff = explain_sql_diff(x, y, dialect)
            check_shape(diff)
            assert diff.fallback and all(d.aspect == "other" for d in diff.items), (x, diff.items)


@pytest.mark.parametrize("a,b", [
    (sql_pairs.PAIRS_BY_ID["diff:r2-in-not-in-swapped"].a, sql_pairs.PAIRS_BY_ID["diff:r2-in-not-in-swapped"].b),
    (sql_pairs.PAIRS_BY_ID["diff:r2-exists-not-exists-swapped"].a,
     sql_pairs.PAIRS_BY_ID["diff:r2-exists-not-exists-swapped"].b),
    (sql_pairs.PAIRS_BY_ID["diff:r2-in-left-column-swapped"].a, sql_pairs.PAIRS_BY_ID["diff:r2-in-left-column-swapped"].b),
], ids=["in-not-in", "exists-not-exists", "left-column"])
def test_subqueries_swapped_between_in_and_not_in_are_not_explained_both_ways(a, b):
    """IN 和 NOT IN（EXISTS 和 NOT EXISTS、id IN 和 referrer_id IN）两个子查询对调：以前配错了对，说出一对互相矛盾的话、
    还把 NOT IN 说成「IN 子查询」。子查询要外面的写法（含 NOT、IN 左边的列）一模一样才配对，配不上就只给兜底。"""
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(a, b, "sqlite", schema=schema, labels=LABELS)
        check_shape(diff)
        assert all(d.aspect == "other" for d in diff.items), diff.items


def test_a_difference_inside_a_not_in_subquery_says_not_in():
    """NOT IN 子查询里的条件不同，说的是「NOT IN 的子查询」（以前说成「IN 子查询」，读的人会以为是保留这些用户）。子查询
    里不带表名的列，没给表结构时分不清是子查询里的表的还是外层的（users 也可能有 status），不说具体的；给了表结构、
    或者写了表名，就说得出。"""
    a = ("SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE status = 'void' "
         "AND user_id IS NOT NULL)")
    b = a.replace("'void'", "'open'")
    expected = [("filter", "原流程在 id NOT IN 的子查询里按 status = 'void' 筛选，盲算在 id NOT IN 的子查询里按 "
                           "status = 'open' 筛选")]
    diff = explain_sql_diff(a, b, "sqlite", labels=LABELS, schema=sql_pairs.schema_cache())
    check_shape(diff)
    assert [(d.aspect, d.text) for d in diff.items] == expected
    assert explain_sql_diff(a, b, "sqlite", labels=LABELS).items == [FacetDiff("other", None, None, FALLBACK)]
    qa, qb = a.replace("FROM orders WHERE status", "FROM orders o WHERE o.status"), \
        b.replace("FROM orders WHERE status", "FROM orders o WHERE o.status")
    assert [(d.aspect, d.text) for d in explain_sql_diff(qa, qb, "sqlite", labels=LABELS).items] == expected
    diff = explain_sql_diff(a.replace("NOT IN", "IN"), b.replace("NOT IN", "IN"), "sqlite", labels=LABELS,
                            schema=sql_pairs.schema_cache())
    assert "id IN 的子查询" in diff.items[0].text and "NOT IN" not in diff.items[0].text


def test_a_swapped_table_keeps_each_sides_own_columns():
    """换了一张表：盲算的 created_at 是 users 的（注册时间），以前把 B 的表改名成 A 的表再比，中文名和 detail 都归到了
    orders。现在两边各用各的表说。"""
    names = {"orders.created_at": "下单时间", "users.created_at": "注册时间", "orders.paid_at": "支付时间"}
    diff = explain_sql_diff("SELECT COUNT(*) FROM orders WHERE paid_at >= '2026-09-01'",
                            "SELECT COUNT(*) FROM users WHERE created_at >= '2026-09-01'", "sqlite", names=names,
                            labels=LABELS)
    check_shape(diff)
    assert [(d.aspect, d.text) for d in diff.items] == [
        ("tables", "原流程查的是 orders 表，盲算查的是 users 表"),
        ("time", "原流程按支付时间 paid_at 筛时间，盲算按注册时间 created_at")]
    assert diff.items[1].detail == {"a": [{"table": "orders", "column": "paid_at"}],
                                    "b": [{"table": "users", "column": "created_at"}]}
    diff = explain_sql_diff("SELECT SUM(o.amount) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.city = 'SH'",
                            "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id WHERE u.city = 'SH'",
                            "sqlite")
    check_shape(diff)
    assert [d.text for d in diff.items] == [
        "A 查的是 stores 表，B 查的是 users 表",
        "A 关联的条件是 orders.store_id = stores.id，B 是 orders.user_id = users.id"]


def test_ordering_that_decides_which_row_distinct_on_keeps_is_not_just_ordering():
    """DISTINCT ON 留哪一行由 ORDER BY 决定：排序不同就是留下的行不同，不能说成「只是排序不同」（aspect=order）。"""
    a = "SELECT DISTINCT ON (user_id) user_id, status FROM orders ORDER BY user_id, created_at DESC NULLS LAST, id"
    b = "SELECT DISTINCT ON (user_id) user_id, status FROM orders ORDER BY user_id, paid_at DESC NULLS LAST, id"
    diff = explain_sql_diff(a, b, "postgres")
    check_shape(diff)
    assert not diff.equivalent and all(d.aspect != "order" for d in diff.items)


def test_a_table_name_differing_only_in_case_is_not_called_another_table_in_mysql():
    """MySQL 的表名在 Linux 上分大小写、在 macOS / Windows 上不分：BILLS 和 bills 不判等价，也不说「查的是另一张表」。"""
    diff = explain_sql_diff("SELECT store_id, SUM(amount) AS s FROM orders WHERE is_deleted = 0 GROUP BY store_id",
                            "select STORE_ID, sum( AMOUNT ) as s from ORDERS where IS_DELETED = 0 group by store_id",
                            "mysql")
    check_shape(diff)
    assert all(d.aspect == "other" for d in diff.items)


def test_redundant_or_constant_conditions_are_not_claimed():
    """多写一个本来就成立的条件（1 = 1、已经被 > 筛掉的空值、关联键上的 IS NOT NULL、分组后 COUNT(*) > 0）：两条意思一样，
    不说「A 只保留 … 的行，B 没有这个条件」。1 = 1 直接认成等价。"""
    assert explain_sql_diff("SELECT COUNT(*) FROM orders WHERE 1 = 1 AND status = 'paid'",
                            "SELECT COUNT(*) FROM orders WHERE status = 'paid'", "sqlite").equivalent
    for dialect in DIALECTS:
        for a, b in (("SELECT SUM(tax) FROM orders WHERE tax > 1 AND tax IS NOT NULL", "SELECT SUM(tax) FROM orders WHERE tax > 1"),
                     ("SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id WHERE o.user_id IS NOT NULL",
                      "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id"),
                     ("SELECT store_id, SUM(amount) FROM orders GROUP BY store_id HAVING COUNT(*) > 0",
                      "SELECT store_id, SUM(amount) FROM orders GROUP BY store_id"),
                     ("SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) > 1",
                      "SELECT user_id FROM orders GROUP BY user_id HAVING COUNT(*) >= 2"),
                     ("SELECT COUNT(tax) FROM orders", "SELECT COUNT(*) FROM orders WHERE tax IS NOT NULL"),
                     ("SELECT COUNT(r.id) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
                      "SELECT COUNT(r.id) FROM orders o JOIN refunds r ON r.order_id = o.id")):
            diff = explain_sql_diff(a, b, dialect)
            check_shape(diff)
            assert all(d.aspect == "other" for d in diff.items), (dialect, a, diff.items)


def test_single_item_in_list_becomes_equality_only_for_a_constant():
    """IN 列表只有一项：是常量才写成 =；是一列的话 SQLite 里 a IN (b) 是 a = +b（右边不带类型亲和），和 a = b 不同。"""
    assert explain_sql_diff("SELECT COUNT(*) FROM orders WHERE status IN ('paid')",
                            "SELECT COUNT(*) FROM orders WHERE status = 'paid'", "sqlite").equivalent
    assert not explain_sql_diff("SELECT id FROM t WHERE a IN (b)", "SELECT id FROM t WHERE a = b", "sqlite").equivalent


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("a,b", [
    ("SELECT id FROM orders ORDER BY amount DESC LIMIT 5 OFFSET 10", "SELECT id FROM orders ORDER BY amount DESC LIMIT 20 OFFSET 0"),
    ("WITH p(uid, total) AS (SELECT user_id, SUM(amount) FROM orders WHERE amount > 10 GROUP BY user_id) "
     "SELECT COUNT(*) FROM p WHERE total > 100",
     "WITH p(uid, total) AS (SELECT user_id, SUM(amount) FROM orders WHERE amount > 50 GROUP BY user_id) "
     "SELECT COUNT(*) FROM p WHERE total > 999"),
    ("SELECT COUNT(*) FROM orders WHERE amount = 0.1 + 0.2 AND status IN ('paid', 'done')",
     "SELECT COUNT(*) FROM orders WHERE amount = 0.5 + 0.25 AND status IN ('open', 'void')"),
    ("SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE created_at >= '2026-09-01')",
     "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE created_at >= '2026-10-01')"),
], ids=["limit-offset", "cte-column-list", "arith-and-in-list", "not-in"])
def test_only_literals_changed_stays_the_same_template_after_the_rewrite(dialect, a, b):
    """normalize_sql「只改了参数」的判断不受这次改动影响：只改字面量的照旧同一个模板（等价判定照样说不等价）。"""
    na, nb = normalize_sql(a, dialect), normalize_sql(b, dialect)
    assert na.parse_error is None and na.same_shape(nb) and na.literals != nb.literals
    assert not explain_sql_diff(a, b, dialect).equivalent


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("a,b", [
    ("WITH p(uid, total) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) SELECT COUNT(*) FROM p WHERE total > 1",
     "WITH p(total, uid) AS (SELECT user_id, SUM(amount) FROM orders GROUP BY user_id) SELECT COUNT(*) FROM p WHERE total > 1"),
    ("SELECT COUNT(*) FROM users WHERE id IN (SELECT user_id FROM orders)",
     "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders)"),
    ("SELECT COUNT(*) FROM orders WHERE amount = 0.1 + 0.2", "SELECT COUNT(*) FROM orders WHERE amount = 0.3"),
    ("SELECT u.city, COUNT(*) FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.city",
     "SELECT u.city, COUNT(*) FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.city"),
], ids=["cte-column-list-order", "in-vs-not-in", "folded-constant", "join-kind"])
def test_a_structural_change_is_still_a_different_template(dialect, a, b):
    """结构变了的（列名表的先后、IN 和 NOT IN、常量写成一个数、连接类型）照旧不是同一个模板。"""
    assert not normalize_sql(a, dialect).same_shape(normalize_sql(b, dialect))


# --------------------------------------------------------------------------
# 第 3 轮复验的反例：等价的结论、具体说法的可靠性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.REGRESSIONS_R3,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.REGRESSIONS_R3])
def test_round_three_counterexamples_are_never_equivalent(name, dialect, a, b):
    """第 3 轮复验报的 blocker（PostgreSQL / MySQL 的写法、执行起来每次都不同的 RANDOM()）：外连接改内连接时 NOT IN、
    ALL、NOT … ANY、数组拼接其实留得下没关联上的行；ORDER BY 里单独一个名字先认列别名；没有 GROUP BY 的 HAVING 让
    语句只出一行；MySQL 的 /*! … */、!、--1、类型不同的 BETWEEN；不确定的函数；解析时换成同一个节点的写法。不管
    给不给表结构、谁先谁后，一律不判等价。"""
    for schema in (None, sql_pairs.schema_cache()):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            check_shape(diff)
            assert not diff.equivalent, name


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.DIALECT_SAME_R3,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.DIALECT_SAME_R3])
def test_round_three_equivalent_rewrites_get_at_most_the_fallback(name, dialect, a, b):
    """第 3 轮复验报的 major（意思一样，却说了具体的不同）在 PostgreSQL / MySQL 上的写法：认得出就判等价，认不出只给
    兜底说明，不说「A 按 X 分组，B 按 Y 分组」「A 用 LEFT JOIN，B 用 INNER JOIN」这类其实不影响结果的话。"""
    for schema in (None, sql_pairs.schema_cache()):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            check_shape(diff)
            assert diff.equivalent or all(d.aspect == "other" for d in diff.items), (name, diff.items)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_irrelevant_differences_inside_exists_and_in_are_never_described(dialect):
    """EXISTS 只看有没有行、IN 只看在不在：子查询里去重、分组、UNION 还是 UNION ALL 都不影响。以前说「A 在 EXISTS
    子查询里对 amount 去重，B 没有去重」「A 在 id IN 的子查询里用 UNION 合并」，还标成说全了。"""
    ids = ["same:r3-exists-distinct", "same:r3-not-exists-distinct", "same:r3-exists-group-by",
           "same:r3-exists-union-vs-union-all", "same:r3-in-union-vs-union-all", "same:r3-in-group-by",
           "same:r3-in-group-by-two-keys", "same:r3-in-group-by-having-count-noop"]
    for pid in ids:
        pair = sql_pairs.PAIRS_BY_ID[pid]
        for schema in (None, sql_pairs.schema_cache()):
            diff = explain_sql_diff(pair.a, pair.b, dialect, schema=schema)
            check_shape(diff)
            assert diff.equivalent or all(d.aspect == "other" for d in diff.items), (pid, diff.items)


def test_only_the_time_column_is_reported_when_the_group_key_is_equal_by_join():
    """真正的差异只有时间列；分组键一边写 users.id、一边写 orders.user_id，被关联条件定成相等。给了表结构（两列都声明
    INTEGER，相等就是一样）只说时间列、说全了（只差一个方面，选择才能写进数据目录）；没给表结构时类型不知道（SQLite 里
    1 和 '1' 也相等），只说时间列，另附兜底，不说「A 按 users.id 分组，B 按 orders.user_id 分组」。"""
    pair = sql_pairs.PAIRS_BY_ID["diff:r3-time-plus-group-key-equal"]
    expected = ("time", "A 按 paid_at 筛时间，B 按 created_at")
    diff = explain_sql_diff(pair.a, pair.b, "sqlite", schema=sql_pairs.schema_cache())
    check_shape(diff)
    assert [(d.aspect, d.text) for d in diff.items] == [expected] and not diff.fallback
    diff = explain_sql_diff(pair.a, pair.b, "sqlite")
    check_shape(diff)
    assert (diff.items[0].aspect, diff.items[0].text) == expected
    assert all(d.aspect in ("time", "other") for d in diff.items)


def test_only_the_time_column_is_reported_when_the_left_join_cannot_matter():
    """结果只是可空一侧的聚合（COALESCE(SUM(r.amount), 0)）：LEFT JOIN 和 INNER JOIN 结果一样，真正的差异只有时间列。
    以前多说一句「A 用 LEFT JOIN 关联 refunds 表，B 用 INNER JOIN」，还标成说全了。"""
    pair = sql_pairs.PAIRS_BY_ID["diff:r3-time-plus-left-coalesce"]
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(pair.a, pair.b, "sqlite", schema=schema)
        check_shape(diff)
        assert [(d.aspect, d.text) for d in diff.items] == [("time", "A 按 paid_at 筛时间，B 按 created_at")]
    for dialect in ("postgres", "mysql"):
        diff = explain_sql_diff(pair.a, pair.b, dialect)
        check_shape(diff)
        assert not any(d.aspect == "join" for d in diff.items), (dialect, diff.items)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_self_join_direction_gets_only_the_fallback(dialect):
    """自己关联自己，关联的方向不同：两个别名都是 orders，以前说成「A 关联 orders 表的条件是 orders.parent_id =
    orders.id，B 是 orders.id = orders.parent_id」，读起来是同一个等式、自相矛盾。分不清哪个是哪个，只给兜底说明。"""
    pair = sql_pairs.PAIRS_BY_ID["diff:r3-self-join-roles"]
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(pair.a, pair.b, dialect, schema=schema)
        check_shape(diff)
        assert not diff.equivalent and all(d.aspect == "other" for d in diff.items), diff.items


@pytest.mark.parametrize("dialect", DIALECTS)
def test_excluding_by_not_equal_also_mentions_the_null_rows(dialect):
    """<>、NOT IN 也把这一列为空的行筛掉了：以前只说「A 排除了 is_deleted = 1 的行」，读的人会以为空值的行还在。
    NOT 套着几个条件的，照原样说「只保留 NOT (…) 的行」。"""
    diff = explain_sql_diff("SELECT SUM(amount) FROM orders WHERE is_deleted <> 1", "SELECT SUM(amount) FROM orders",
                            dialect)
    check_shape(diff)
    assert [d.text for d in diff.items] == ["A 排除了 is_deleted = 1 的行和 is_deleted 为空的行，B 没有"]
    diff = explain_sql_diff("SELECT COUNT(*) FROM orders WHERE status NOT IN ('void', 'open')",
                            "SELECT COUNT(*) FROM orders", dialect)
    check_shape(diff)
    assert [d.text for d in diff.items] == ["A 排除了 status IN ('open', 'void') 的行和 status 为空的行，B 没有"]
    # NOT 往里推成 status <> 'void' OR is_deleted <> 1（三值逻辑下一样），照原样说「只保留 … 的行」
    diff = explain_sql_diff("SELECT COUNT(*) FROM orders WHERE NOT (status = 'void' AND is_deleted = 1)",
                            "SELECT COUNT(*) FROM orders", dialect)
    check_shape(diff)
    assert [d.text for d in diff.items] == ["A 只保留 status <> 'void' OR is_deleted <> 1 的行，B 没有这个条件"]


@pytest.mark.parametrize("pid", ["hard:r3-disjoint-union-all-vs-union", "hard:r3-order-single-group",
                                 "hard:r3-distinct-join-vs-in", "hard:r3-count-distinct-join-vs-in",
                                 "hard:r3-join-vs-exists-distinct", "hard:r3-shadowed-alias-written-out"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_equivalent_rewrites_needing_reasoning_get_only_the_fallback(pid, dialect):
    """意思一样、要按语义推理才看得出的（各支本身去重、互不相交的 UNION ALL 和 UNION；只有一组时的排序；关联一张表和
    IN / EXISTS 子查询；被遮住的别名写明白）：以前说「A 用 UNION ALL（重复的行都留着）」「A 按 user_id 升序排，B 没有
    排序」「A 关联了 users 表，B 没有关联它」「A 在 EXISTS 子查询里只保留 status = 'paid' 的行」。认不出只给兜底。"""
    pair = sql_pairs.PAIRS_BY_ID[pid]
    for schema in (None, sql_pairs.schema_cache()):
        diff = explain_sql_diff(pair.a, pair.b, dialect, schema=schema)
        check_shape(diff)
        assert diff.equivalent or all(d.aspect == "other" for d in diff.items), diff.items


def test_identical_text_is_equivalent_even_when_the_parser_would_lose_something():
    """一字不差的两条就是同一条语句：带 MySQL 的 /*! … */、RANDOM() 这些不在语法树上判等价的写法，原文一样也算等价。"""
    for sql, dialect in (("SELECT COUNT(*) FROM orders /*! WHERE status = 'paid' */", "mysql"),
                         ("SELECT COUNT(*) FROM orders WHERE RANDOM() % 2 = 0", "sqlite"),
                         ("SELECT COUNT(*) FROM orders WHERE MOD(amount, 2) = 0", "sqlite")):
        diff = explain_sql_diff(sql, "  " + sql + "\n", dialect)
        check_shape(diff)
        assert diff.equivalent, sql


@pytest.mark.parametrize("dialect,a,b", [
    ("mysql", "SELECT COUNT(*) FROM orders /*! WHERE status = 'paid' */", "SELECT COUNT(*) FROM orders"),
    ("mysql", "SELECT COUNT(*) FROM orders WHERE ! status = 'paid'", "SELECT COUNT(*) FROM orders WHERE NOT status = 'paid'"),
    ("mysql", "SELECT id, amount --1 FROM orders", "SELECT id, amount FROM orders"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE MOD(amount, 2) = 0", "SELECT COUNT(*) FROM orders WHERE amount % 2 = 0"),
    ("sqlite", "SELECT CHAR_LENGTH(name) FROM users", "SELECT LENGTH(name) FROM users"),
], ids=["executable-comment", "bang", "double-dash", "mod-function", "char-length"])
def test_a_structure_the_parser_loses_is_never_the_same_template(dialect, a, b):
    """解析时丢了的结构（MySQL 会执行的 /*! … */ 注释、比比较结合得紧的 !、--1 是减负一，SQLite 的 MOD() 和 %、
    SQLite 没有的 CHAR_LENGTH）不能被 normalize_sql 当成「只改了参数」。"""
    assert not normalize_sql(a, dialect).same_shape(normalize_sql(b, dialect))


@pytest.mark.parametrize("dialect,a,b", [
    ("mysql", "SELECT COUNT(*) FROM orders /*! WHERE status = 'paid' */", "SELECT COUNT(*) FROM orders /*! WHERE status = 'void' */"),
    ("mysql", "SELECT COUNT(*) FROM orders WHERE ! status = 'paid'", "SELECT COUNT(*) FROM orders WHERE ! status = 'void'"),
    ("sqlite", "SELECT COUNT(*) FROM orders WHERE MOD(amount, 2) = 0", "SELECT COUNT(*) FROM orders WHERE MOD(amount, 3) = 1"),
    ("sqlite", "SELECT o.id FROM orders o WHERE EXISTS (SELECT DISTINCT r.amount FROM refunds r WHERE r.order_id = o.id "
               "AND r.refunded_at >= '2026-09-01')",
     "SELECT o.id FROM orders o WHERE EXISTS (SELECT DISTINCT r.amount FROM refunds r WHERE r.order_id = o.id "
     "AND r.refunded_at >= '2026-10-01')"),
    ("sqlite", "SELECT id FROM orders ORDER BY id LIMIT -1", "SELECT id FROM orders ORDER BY id LIMIT -20"),
    ("postgres", "SELECT COALESCE(SUM(r.amount), 0) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
                 "WHERE o.paid_at >= '2026-09-01'",
     "SELECT COALESCE(SUM(r.amount), 1) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id "
     "WHERE o.paid_at >= '2026-10-01'"),
], ids=["executable-comment", "bang", "mod-function", "exists-distinct", "limit", "left-coalesce"])
def test_only_literals_changed_is_still_the_same_template_in_round_three(dialect, a, b):
    """这一轮动过的写法（会执行的注释、!、MOD()、EXISTS 里去重、负数 LIMIT、LEFT JOIN 的聚合）：只改字面量的照旧是
    同一个模板，字面量各是各的。"""
    na, nb = normalize_sql(a, dialect), normalize_sql(b, dialect)
    assert na.same_shape(nb) and na.literals != nb.literals


def test_a_join_on_a_primary_key_against_an_in_subquery_is_not_described_with_the_schema():
    """按主键关联 users（每张订单最多关联上一个用户）和 user_id IN (SELECT id FROM users) 是一回事：给了表结构（知道
    users.id 是主键）就不说「A 关联了 users 表，B 没有关联它」；没给表结构时关联可能把行复制，这句话是对的。"""
    a = "SELECT o.id, o.amount FROM orders o JOIN users u ON u.id = o.user_id"
    b = "SELECT o.id, o.amount FROM orders o WHERE o.user_id IN (SELECT id FROM users)"
    diff = explain_sql_diff(a, b, "sqlite", schema=sql_pairs.schema_cache())
    check_shape(diff)
    assert all(d.aspect == "other" for d in diff.items), diff.items
    diff = explain_sql_diff(a, b, "sqlite")
    check_shape(diff)
    assert diff.fallback and diff.items[0].text.startswith("A 关联了 users 表")


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("a,b", [
    ("SELECT DISTINCT o.user_id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT o.user_id FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("SELECT SUM(r.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(IFNULL(r.amount, NULL)) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"),
    ("SELECT SUM(o.amount), COUNT(*) FROM orders o LEFT JOIN stores s ON s.id = o.store_id",
     "SELECT SUM(o.amount), COUNT(*) FROM orders o LEFT JOIN stores s ON s.id IN (o.store_id)"),
], ids=["dropped-left-join", "null-blind-left-join", "primary-key-spelled-otherwise"])
def test_a_join_rewritten_on_one_side_only_is_not_described_as_missing(dialect, a, b):
    """一边的 LEFT JOIN 被规范化去掉了（DISTINCT 之后不怕重复、按主键关联）或者按内连接比（只取可空一侧的聚合），另一边
    写法不同没认出来：原文两边都关联了这张表、都写的 LEFT JOIN，不能说「A 没有关联 refunds 表」「A 用 INNER JOIN」。"""
    for schema in (None, sql_pairs.schema_cache()):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            check_shape(diff)
            assert not any(d.aspect == "join" for d in diff.items), (dialect, diff.items)
