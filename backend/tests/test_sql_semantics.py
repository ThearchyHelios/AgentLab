"""SQL 语义（data/sql_semantics.py）：两条 SQL 算的是不是一回事，不是的话差在哪。

- normalize_sql：字面量换成占位符后的规范文本 + 字面量。只有字面量不同的两条 SQL 规范文本相同（定时运行每期的日期
  不同，不能算模板变了）；关键字大小写、空白、注释也不算不同；解析不了的退回逐词规范化，照样认得出只改了字面量
- facets：表、连接（类型 / 关联进来的表 / ON 条件）、WHERE 合取式、时间条件（列 / 运算符 / 取值）、分组、聚合
  （函数 / 参数 / 去重）、HAVING、取哪几行（各段的 LIMIT / OFFSET / 排序）。别名换成真实表名（条件里套着的子查询、
  HAVING 里的别名也是）、比较的两边排好顺序；解析不了带 parse_error，不抛
- diff_facets：每处不同一句中文：「A 按支付时间 paid_at 筛时间，B 按下单时间 created_at」「A 排除了
  is_deleted = 1 的行，B 没有」「A 对 order_id 去重计数，B 没去重」……写法不同、口径相同的不报

夹具全是虚构的表：订单 orders、退款 refunds、门店 stores，以及一张中文表「订单」。
"""
from __future__ import annotations

import json

import pytest

from app.data.sql_semantics import (
    Facets,
    catalog_labels,
    diff_facets,
    facets,
    normalize_sql,
)

DIALECTS = ("sqlite", "mysql", "postgres")


def texts(a: str, b: str, dialect: str = "sqlite", **kwargs) -> list[str]:
    return [d.text for d in diff_facets(facets(a, dialect), facets(b, dialect), **kwargs)]


def aspects(a: str, b: str, dialect: str = "sqlite") -> list[str]:
    return [d.aspect for d in diff_facets(facets(a, dialect), facets(b, dialect))]


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


# --------------------------------------------------------------------------
# diff_facets：每个方面一对
# --------------------------------------------------------------------------

BASE = "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"


def test_time_column_differs_with_catalog_names():
    a = "SELECT SUM(amount) FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    b = "SELECT SUM(amount) FROM orders WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'"
    [diff] = diff_facets(facets(a), facets(b), names={"orders.paid_at": "支付时间", "created_at": "下单时间"})
    assert diff.aspect == "time"
    assert diff.text == "A 按支付时间 paid_at 筛时间，B 按下单时间 created_at"
    assert (diff.a, diff.b) == ("paid_at", "created_at")
    assert diff.detail == {"a": [{"table": "orders", "column": "paid_at"}],
                           "b": [{"table": "orders", "column": "created_at"}]}
    assert texts(a, b) == ["A 按 paid_at 筛时间，B 按 created_at"], "没有中文名就只写列名"


def test_time_filter_on_one_side_only():
    assert texts(BASE, "SELECT SUM(amount) FROM orders") == ["A 按 paid_at 筛时间，B 没有按时间筛选"]
    assert texts("SELECT SUM(amount) FROM orders", BASE) == ["A 没有按时间筛选，B 按 paid_at 筛时间"]


def test_same_time_column_different_range():
    other = "SELECT SUM(amount) FROM orders WHERE paid_at BETWEEN '2026-09-01' AND '2026-09-30'"
    [diff] = diff_facets(facets(BASE), facets(other))
    assert diff.aspect == "time_range"
    assert diff.text == ("A 的时间范围是 paid_at >= '2026-09-01' AND paid_at < '2026-10-01'，"
                         "B 是 paid_at BETWEEN '2026-09-01' AND '2026-09-30'")


def test_only_literals_differ_is_still_a_different_range():
    """规范文本相同（同一个模板），但比口径时日期不同就是不同的范围：两个函数回答的是两个问题。"""
    later = BASE.replace("2026-09-01", "2026-09-08").replace("2026-10-01", "2026-10-08")
    assert normalize_sql(BASE).same_shape(normalize_sql(later))
    assert aspects(BASE, later) == ["time_range"]


def test_an_exclusion_on_one_side():
    assert texts("SELECT SUM(amount) FROM orders WHERE is_deleted <> 1", "SELECT SUM(amount) FROM orders") == \
        ["A 排除了 is_deleted = 1 的行，B 没有"]
    assert texts("SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders WHERE refund_id IS NOT NULL") == \
        ["B 排除了 refund_id 为空的行，A 没有"]


def test_a_kept_condition_on_one_side():
    assert texts("SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders WHERE status = 'paid'") == \
        ["B 只保留 status = 'paid' 的行，A 没有这个条件"]


def test_two_conditions_on_the_same_column_are_paired():
    [diff] = diff_facets(facets("SELECT SUM(amount) FROM orders WHERE status = 'paid'"),
                         facets("SELECT SUM(amount) FROM orders WHERE status IN ('paid', 'shipped')"))
    assert (diff.aspect, diff.text) == ("filter", "A 按 status = 'paid' 筛选，B 按 status IN ('paid', 'shipped') 筛选")
    assert (diff.a, diff.b) == ("status = 'paid'", "status IN ('paid', 'shipped')")


def test_distinct_count_versus_plain_count():
    assert texts("SELECT COUNT(DISTINCT order_id) FROM orders", "SELECT COUNT(order_id) FROM orders") == \
        ["A 对 order_id 去重计数，B 没去重"]
    assert texts("SELECT COUNT(order_id) FROM orders", "SELECT COUNT(DISTINCT order_id) FROM orders") == \
        ["A 对 order_id 计数时没去重，B 去重计数"]


@pytest.mark.parametrize("a,b,text", [
    ("SELECT SUM(amount) FROM orders", "SELECT AVG(amount) FROM orders", "A 对 amount 求和，B 求平均"),
    ("SELECT SUM(paid_amount) FROM orders", "SELECT SUM(amount) FROM orders", "A 对 paid_amount 求和，B 对 amount 求和"),
    ("SELECT COUNT(*) FROM orders", "SELECT COUNT(DISTINCT order_id) FROM orders",
     "A 数的是行数，B 对 order_id 去重计数"),
])
def test_other_aggregate_differences(a, b, text):
    assert texts(a, b) == [text]


def test_join_differences():
    left = "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"
    inner = "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"
    other_key = "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.user_id = o.user_id"
    plain = "SELECT SUM(amount) FROM orders"
    assert texts(left, inner) == ["A 用 LEFT JOIN 关联 refunds 表，B 用 INNER JOIN"]
    assert texts(left, other_key) == ["A 关联 refunds 表的条件是 orders.id = refunds.order_id，"
                                      "B 是 orders.user_id = refunds.user_id"]
    assert texts(left, plain) == ["A 关联了 refunds 表，B 没有"]


def test_a_different_source_table_is_said_once():
    """表换了一张：说一次「查的是哪张表」就够了，同一个聚合、同一个条件不再因为表名不同各报一遍。"""
    a = "SELECT SUM(amount) FROM orders WHERE is_deleted = 0 AND paid_at >= '2026-09-01'"
    b = "SELECT SUM(amount) FROM orders_archive WHERE is_deleted = 0 AND paid_at >= '2026-09-01'"
    [diff] = diff_facets(facets(a), facets(b))
    assert (diff.aspect, diff.text) == ("tables", "A 查的是 orders 表，B 查的是 orders_archive 表")
    assert aspects(a, b.replace("is_deleted = 0", "is_deleted = 1")) == ["filter", "tables"], "换表之外的不同照报"


def test_two_columns_that_read_the_same_are_told_apart():
    """各自只写列名时两边读起来一样，其实是两张表的列：改写成带表名的写法，不说「A 对 amount 求和，B 对 amount 求和」。"""
    a = "SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"
    b = "SELECT SUM(r.amount) FROM orders o JOIN refunds r ON r.order_id = o.id"
    assert texts(a, b) == ["A 对 orders.amount 求和，B 对 refunds.amount 求和"]


def test_group_by_difference():
    assert texts("SELECT region, SUM(amount) FROM orders GROUP BY region",
                 "SELECT region, city, SUM(amount) FROM orders GROUP BY region, city") == \
        ["A 按 region 分组，B 按 region、city 分组"]
    assert texts("SELECT SUM(amount) FROM orders", "SELECT region, SUM(amount) FROM orders GROUP BY region") == \
        ["A 没有分组，B 按 region 分组"]


def test_having_difference():
    assert texts("SELECT region, SUM(amount) FROM orders GROUP BY region HAVING SUM(amount) > 100",
                 "SELECT region, SUM(amount) FROM orders GROUP BY region") == \
        ["A 分组后只保留 SUM(amount) > 100 的组，B 没有这个条件"]


def test_limit_difference():
    assert texts("SELECT amount FROM orders LIMIT 10", "SELECT amount FROM orders") == ["A 只取前 10 行，B 不限行数"]
    assert texts("SELECT amount FROM orders LIMIT 10", "SELECT amount FROM orders LIMIT 100") == \
        ["A 只取前 10 行，B 只取前 100 行"]


def test_an_unparsable_side_is_reported_once_without_item_by_item_comparison():
    [diff] = diff_facets(facets("SELECT FROM WHERE"), facets(BASE))
    assert diff.aspect == "parse" and diff.text.startswith("A：SQL 无法解析") and "没有逐项比较" in diff.text
    assert diff.b is None


@pytest.mark.parametrize("a,b", [
    # 别名、比较两边的顺序、大小写
    ("SELECT SUM(o.amount) FROM orders o WHERE o.paid_at >= '2026-09-01' AND o.is_deleted = 0",
     "select sum(amount) from orders where 0 = is_deleted and '2026-09-01' <= paid_at"),
    # 有效记录条件写在 CTE 里、还是写在外层
    ("WITH v AS (SELECT * FROM orders WHERE is_deleted = 0) SELECT COUNT(DISTINCT v.order_id) FROM v",
     "SELECT COUNT(DISTINCT order_id) FROM orders WHERE is_deleted = 0"),
    # 逗号连接和 INNER JOIN
    ("SELECT SUM(o.amount) FROM orders o, stores s WHERE o.store_id = s.id AND s.city = '虚构市'",
     "SELECT SUM(orders.amount) FROM orders INNER JOIN stores ON stores.id = orders.store_id "
     "WHERE stores.city = '虚构市'"),
    # 条件里套着的子查询用了别名：NOT EXISTS、IN (SELECT …)
    ("SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)",
     "SELECT orders.id FROM orders WHERE NOT EXISTS (SELECT 1 FROM refunds WHERE refunds.order_id = orders.id)"),
    ("SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)"),
    # HAVING 里用 SELECT 的别名
    ("SELECT o.region, SUM(o.amount) FROM orders o GROUP BY o.region HAVING SUM(o.amount) > 100",
     "SELECT region AS r, SUM(amount) AS s FROM orders GROUP BY r HAVING s > 100"),
    # OFFSET 0 等于没写；取前几行时 ORDER BY 1 和写出列名是同一个排序
    ("SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10 OFFSET 0",
     "SELECT o.amount FROM orders o ORDER BY o.amount DESC LIMIT 10"),
])
def test_the_same_meaning_written_differently_is_no_difference(a, b):
    assert texts(a, b) == []


def test_a_real_difference_inside_a_subquery_is_still_reported():
    a = "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id FROM users u WHERE u.vip = 1)"
    b = "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.referrer_id FROM users u WHERE u.vip = 1)"
    assert texts(a, b) == ["A 按 user_id IN (SELECT id FROM users WHERE vip = 1) 筛选，"
                           "B 按 user_id IN (SELECT referrer_id FROM users WHERE vip = 1) 筛选"]


@pytest.mark.parametrize("a,b,text", [
    ("WITH top AS (SELECT amount FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM top",
     "WITH top AS (SELECT amount FROM orders ORDER BY amount DESC LIMIT 100) SELECT SUM(amount) FROM top",
     "A 在 top 里只取前 10 行，B 只取前 100 行"),
    ("SELECT amount FROM orders LIMIT 10 OFFSET 0", "SELECT amount FROM orders LIMIT 10 OFFSET 10",
     "A 不跳过行，B 跳过前 10 行"),
    ("SELECT amount FROM orders ORDER BY 1 DESC LIMIT 10", "SELECT amount FROM orders ORDER BY 1 ASC LIMIT 10",
     "A 取行前按 amount 降序排，B 按 amount 升序排"),
    ("SELECT amount FROM orders ORDER BY amount DESC LIMIT 10", "SELECT amount FROM orders LIMIT 10",
     "A 取行前按 amount 降序排，B 没有排序"),
])
def test_which_rows_are_taken_is_compared(a, b, text):
    """取哪几行：每一段的行数、跳过的行数，以及取前几行时的排序。排序只在有 LIMIT / OFFSET 的段里比：
    没有取前几行，排序不影响算出来的数。"""
    assert texts(a, b) == [text]
    assert texts("SELECT SUM(amount) FROM orders ORDER BY 1 DESC", "SELECT SUM(amount) FROM orders") == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_each_dialect_gives_the_same_three_findings(dialect):
    q = {"sqlite": '"', "postgres": '"', "mysql": "`"}[dialect]
    a = (f"SELECT COUNT(DISTINCT o.order_id) AS {q}订单数{q} FROM orders o "
         "WHERE o.paid_at >= '2026-09-01' AND o.is_deleted <> 1")
    b = f"SELECT COUNT(o.order_id) AS {q}订单数{q} FROM orders o WHERE o.created_at >= '2026-09-01'"
    assert texts(a, b, dialect) == ["A 按 paid_at 筛时间，B 按 created_at", "A 排除了 is_deleted = 1 的行，B 没有",
                                    "A 对 order_id 去重计数，B 没去重"]


def test_side_labels_in_chinese_have_no_stray_spaces():
    a = "SELECT COUNT(DISTINCT order_id) FROM orders WHERE paid_at >= '2026-09-01'"
    b = "SELECT COUNT(order_id) FROM orders WHERE created_at >= '2026-09-01'"
    assert texts(a, b, labels=("原流程", "盲算"), names={"paid_at": "支付时间"}) == [
        "原流程按支付时间 paid_at 筛时间，盲算按 created_at", "原流程对 order_id 去重计数，盲算没去重"]


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
    f = facets(BASE)
    assert isinstance(f, Facets) and diff_facets(f, f) == []
