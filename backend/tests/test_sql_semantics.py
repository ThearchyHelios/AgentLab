"""SQL 语义（data/sql_semantics.py）：两条 SQL 算的是不是一回事，不是的话差在哪。

- normalize_sql：字面量换成占位符后的规范文本 + 字面量。只有字面量不同的两条 SQL 规范文本相同（定时运行每期的日期
  不同，不能算模板变了）；关键字大小写、空白、注释也不算不同；解析不了的退回逐词规范化，照样认得出只改了字面量
- facets：表、连接（类型 / 关联进来的表 / ON 条件）、WHERE 合取式、时间条件（列 / 运算符 / 取值）、分组、聚合
  （函数 / 参数 / 去重）、HAVING、取哪几行（各段的 LIMIT / OFFSET / 排序）。别名换成真实表名（条件里套着的子查询、
  HAVING 里的别名也是），CTE、子查询不按起的名字比（单看一条按位置编号），条件里的子查询选出来的列名不算；比较的
  两边排好顺序；解析不了带 parse_error，不抛
- diff_facets：每处不同一句中文：「A 按支付时间 paid_at 筛时间，B 按下单时间 created_at」「A 排除了
  is_deleted = 1 的行，B 没有」「A 对 order_id 去重计数，B 没去重」……写法不同、口径相同的不报。两边的派生来源先按
  结构（读的表、各列的定义、名字）配好对再比：一边多出一个排在前面的 CTE、内连接换了先后不报，两个 CTE 算出同名
  的列时外层用的是哪一个照报；外连接按主表比（LEFT / RIGHT 对调不报，主表换了、被 WHERE 变成内连接照报）；算出来
  的列按算法比（最外层的算式、窗口函数，CTE 里被外面用到的列的定义）；时间列透过 CTE 里的日期表达式认；取前几行、
  窗口函数前后筛的不同照报；没用到的 CTE 不算；两边同名却不是同一个的派生来源说清各是什么；自己关联自己的两个
  别名分得开

夹具全是虚构的表：订单 orders、退款 refunds、门店 stores、用户 users、付款 payments，以及一张中文表「订单」。
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
    assert not [k for k in data if k.startswith("_")], "拿来重新分析的原文不进 as_dict"


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


@pytest.mark.parametrize("dialect,a,b", [
    # 条件里的子查询给选出来的列起了名字：IN、标量子查询只看取值，列名不影响口径
    ("sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.amount > "
     "(SELECT AVG(o2.amount) AS avg_amount FROM orders o2 WHERE o2.is_deleted = 0)",
     "SELECT SUM(amount) FROM orders WHERE amount > (SELECT AVG(amount) FROM orders WHERE is_deleted = 0)"),
    ("sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT u.id AS uid FROM users u WHERE u.vip = 1)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM users WHERE vip = 1)"),
    # 条件里的子查询从 FROM 里的子查询取数，两边给它起的别名不同
    ("sqlite",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT s.user_id FROM "
     "(SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) s WHERE s.total > 100)",
     "SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT agg.user_id FROM "
     "(SELECT user_id, SUM(amount) AS total FROM payments GROUP BY user_id) agg WHERE agg.total > 100)"),
    # CTE 起的名字不同，外层按 CTE 算出来的列筛选
    ("sqlite",
     "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
     "SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
     "SELECT COUNT(*) FROM t WHERE t.total > 100"),
    # 写成 CTE 还是写成 FROM 里的子查询
    ("postgres",
     "WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM user_totals WHERE total > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) AS agg "
     "WHERE agg.total > 100"),
    # LATERAL 子查询的别名不同：别名挂在 LATERAL 上，不在子查询上
    ("postgres",
     "SELECT SUM(o.amount) FROM orders o, LATERAL (SELECT COUNT(*) AS n FROM refunds r WHERE r.order_id = o.id) l "
     "WHERE l.n > 0",
     "SELECT SUM(o.amount) FROM orders o CROSS JOIN LATERAL (SELECT COUNT(*) AS n FROM refunds r "
     "WHERE r.order_id = o.id) AS x WHERE x.n > 0"),
    # 关联进来的是子查询，两边的别名不同（一边是带反引号的中文别名）
    ("mysql",
     "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) r "
     "ON r.order_id = o.id WHERE r.n > 1",
     "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds GROUP BY order_id) "
     "`退款` ON o.id = `退款`.order_id WHERE `退款`.n > 1"),
    # 条件里的子查询用到 CTE，两边给 CTE 起的名字不同
    ("sqlite",
     "WITH vip AS (SELECT id FROM users WHERE grade >= 3) "
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT id FROM vip)",
     "WITH good_users AS (SELECT id FROM users WHERE grade >= 3) "
     "SELECT SUM(amount) FROM orders WHERE user_id IN (SELECT g.id FROM good_users g)"),
    # CTE 定义在条件里的子查询里：定义连同子查询整个写进外层的条件，定义上的名字也不算
    ("postgres",
     "SELECT SUM(amount) FROM orders WHERE user_id IN "
     "(WITH t AS (SELECT id FROM users WHERE grade >= 3) SELECT id FROM t)",
     "SELECT SUM(amount) FROM orders WHERE user_id IN "
     "(WITH g AS (SELECT id FROM users WHERE grade >= 3) SELECT g.id FROM g)"),
    # 两个 CTE 定义的先后、起的名字都不同，按用到的先后它们是同一对
    ("sqlite",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), "
     "refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
     "SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total",
     "WITH r AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "p AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id) "
     "SELECT COUNT(*) FROM p JOIN r ON p.user_id = r.user_id WHERE p.total > r.total"),
    # CTE 定义的先后和用到的先后不一致，另一边又把它们写成了 FROM 里的子查询：按用到的先后编号才对得上
    ("sqlite",
     "WITH c AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
     "d AS (SELECT user_id, COUNT(*) AS n FROM refunds GROUP BY user_id), "
     "a AS (SELECT * FROM c WHERE total > 100), b AS (SELECT * FROM d WHERE n > 1) "
     "SELECT COUNT(*) FROM b JOIN a ON a.user_id = b.user_id",
     "SELECT COUNT(*) FROM (SELECT * FROM (SELECT user_id, COUNT(*) AS n FROM refunds GROUP BY user_id) x "
     "WHERE n > 1) b JOIN (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) y "
     "WHERE total > 100) a ON a.user_id = b.user_id"),
    # 同上，两个 CTE 还算出了同名的列、外层拿它们互相比：不写编号的写法分不出是哪个的 total，编号得对得上
    ("sqlite",
     "WITH c AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
     "d AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
     "a AS (SELECT * FROM c), b AS (SELECT * FROM d) "
     "SELECT COUNT(*) FROM b JOIN a ON a.user_id = b.user_id WHERE a.total > b.total",
     "SELECT COUNT(*) FROM (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) x) b "
     "JOIN (SELECT * FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) y) a "
     "ON a.user_id = b.user_id WHERE a.total > b.total"),
    # 先用一个 CTE 筛一遍再汇总，还是直接在汇总的 CTE 里筛：里层多一段，不挪动外层那一段的编号
    ("sqlite",
     "WITH base AS (SELECT * FROM orders WHERE is_deleted = 0), "
     "per_user AS (SELECT user_id, SUM(amount) AS total FROM base GROUP BY user_id) "
     "SELECT COUNT(*) FROM per_user WHERE total > 100",
     "WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
     "SELECT COUNT(*) FROM totals WHERE total > 100"),
])
def test_names_given_to_derived_tables_are_no_difference(dialect, a, b):
    """CTE 名、子查询的别名、条件里的子查询选出来的列名都是各自起的（双盲复算的盲算更是）：只因起的名字不同，
    不算换了口径。"""
    assert texts(a, b, dialect) == []


def test_derived_tables_are_compared_by_position_and_shown_by_name():
    """派生来源（CTE、FROM 里的子查询）比较用的写法按它在语句里的位置编号，不写名字；给人看的写法照原样写名字。"""
    a = facets("WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
               "SELECT COUNT(*) FROM user_totals WHERE total > 100")
    b = facets("SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) agg "
               "WHERE agg.total > 100")
    assert a.where[0].key == b.where[0].key
    assert "user_totals" not in a.where[0].key and "agg" not in b.where[0].key
    assert (a.where[0].text, a.where[0].full) == ("total > 100", "user_totals.total > 100")
    assert (b.where[0].text, b.where[0].full) == ("total > 100", "agg.total > 100")
    joined = facets("SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM refunds "
                    "GROUP BY order_id) r ON r.order_id = o.id")
    [join] = joined.joins
    assert join.table.text == "r" and join.on.text == "orders.id = refunds.order_id"


SHIFTED_A = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
             "SELECT COUNT(*) FROM t WHERE total > 100 AND user_id IN (SELECT id FROM users WHERE vip = 1)")
SHIFTED_B = ("WITH vip AS (SELECT id FROM users WHERE vip = 1), "
             "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
             "SELECT COUNT(*) FROM vip JOIN t ON t.user_id = vip.id WHERE t.total > 100")


def test_a_shifted_derived_number_does_not_split_one_condition_in_two():
    """位置编号只看先后：一边多出一个排在前面的 CTE（筛 VIP 用户），后面那个 CTE 的位置编号就错开了。两边的派生
    来源先按结构配好对（t 配 t，vip 配不上），按共同的编号比，同一个条件不会被说成两边各有一个；真正的不同（VIP
    用 IN 筛还是关联进来）照报。"""
    found = diff_facets(facets(SHIFTED_A), facets(SHIFTED_B), labels=("原流程", "盲算"))
    filters = [d.text for d in found if d.aspect == "filter"]
    assert filters == ["原流程只保留 user_id IN (SELECT id FROM users WHERE vip = 1) 的行，盲算没有这个条件"]
    # 错开之后的条件真不同，也照样配成一对说
    other = SHIFTED_B.replace("t.total > 100", "t.total > 200")
    filters = [d.text for d in diff_facets(facets(SHIFTED_A), facets(other), labels=("原流程", "盲算"))
               if d.aspect == "filter"]
    assert filters[0] == "原流程按 total > 100 筛选，盲算按 total > 200 筛选" and len(filters) == 2


@pytest.mark.parametrize("aspect,a,b", [
    # 时间条件、分组键、聚合、HAVING、取前几行时的排序都是按 CTE 算出来的列
    ("time",
     "WITH t AS (SELECT user_id, DATE(paid_at) AS day, amount FROM orders) "
     "SELECT SUM(amount) FROM t WHERE day >= '2026-09-01' AND user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "t AS (SELECT user_id, DATE(paid_at) AS day, amount FROM orders) "
     "SELECT SUM(t.amount) FROM vip JOIN t ON t.user_id = vip.id WHERE t.day >= '2026-09-01'"),
    ("group_by",
     "WITH t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) "
     "SELECT n, COUNT(*) FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) GROUP BY n",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) "
     "SELECT t.n, COUNT(*) FROM vip JOIN t ON t.user_id = vip.id GROUP BY t.n"),
    ("aggregate",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT AVG(total) FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1)",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT AVG(t.total) FROM vip JOIN t ON t.user_id = vip.id"),
    ("having",
     "WITH t AS (SELECT store_id, user_id, SUM(amount) AS total FROM orders GROUP BY store_id, user_id) "
     "SELECT store_id FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) GROUP BY store_id "
     "HAVING MAX(total) > 100",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "t AS (SELECT store_id, user_id, SUM(amount) AS total FROM orders GROUP BY store_id, user_id) "
     "SELECT t.store_id FROM vip JOIN t ON t.user_id = vip.id GROUP BY t.store_id HAVING MAX(t.total) > 100"),
    ("limit",
     "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT user_id FROM t WHERE user_id IN (SELECT id FROM users WHERE vip = 1) ORDER BY total DESC LIMIT 10",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
     "SELECT t.user_id FROM vip JOIN t ON t.user_id = vip.id ORDER BY t.total DESC LIMIT 10"),
])
def test_a_shifted_derived_number_is_no_difference_in_any_aspect(aspect, a, b):
    """编号错开之后，按 CTE 算出来的列写的时间条件、分组键、聚合、HAVING、排序也不算不同：只剩 VIP 那处真正的
    不同。"""
    assert not [x for x in aspects(a, b) if x.startswith(aspect)]


def test_a_shifted_number_inside_a_condition_subquery_is_no_difference():
    """条件里的子查询用到的 CTE 位置编号错开了：两边的 t 按结构配成一对、写同一个编号，子查询整个写进外层条件的
    那一份照样对得上。"""
    a = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u WHERE u.vip = 1 AND u.id IN (SELECT user_id FROM t WHERE total > 100)")
    b = ("WITH vip AS (SELECT id FROM users WHERE vip = 1), "
         "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM vip WHERE vip.id IN (SELECT user_id FROM t WHERE total > 100)")
    assert texts(a, b) == []


def test_a_shifted_derived_join_is_paired_with_its_counterpart():
    """关联进来的 CTE 位置编号错开了（盲算多关联了一个排在前面的 CTE）：两边的派生来源先按结构配好对，原流程的 t
    配盲算的 t，不会和盲算的 vip 当成同一处关联、说成「关联条件不同」。"""
    a = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id WHERE t.total > 100")
    b = ("WITH vip AS (SELECT id FROM users WHERE vip = 1), "
         "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN vip ON vip.id = u.id JOIN t ON t.user_id = u.id WHERE t.total > 100")
    assert texts(a, b, labels=("原流程", "盲算")) == ["盲算只保留 vip = 1 的行，原流程没有这个条件",
                                                    "盲算关联了 vip 表，原流程没有"]


def test_swapped_join_order_of_two_derived_tables_is_no_difference():
    """两个 CTE 关联的先后对调，位置编号也跟着对调：两边按结构配好对、写同一个编号之后，拿它们的列互相比的条件
    排成同一个方向（只按位置编号排，同一个比较会排成两个方向），关联按涉及的两边比，不按哪一侧是关联进来的。"""
    ctes = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
            "u AS (SELECT user_id, COUNT(*) AS cnt FROM refunds GROUP BY user_id) ")
    assert texts(ctes + "SELECT COUNT(*) FROM t JOIN u ON u.user_id = t.user_id WHERE t.total > u.cnt",
                 ctes + "SELECT COUNT(*) FROM u JOIN t ON t.user_id = u.user_id WHERE t.total > u.cnt") == []


def test_a_reversed_comparison_between_two_derived_tables_is_still_reported():
    """两个 CTE 算出了同名的列：按结构配好对之后照样分得清是哪个 CTE 的，比较的方向反了要报出来（只按配好的编号
    比，不拿不写编号的写法补配，不会把它吞掉）。"""
    a = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
         "refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
         "SELECT COUNT(*) FROM paid p JOIN refunded r ON r.user_id = p.user_id WHERE p.total > r.total")
    b = a.replace("WHERE p.total > r.total", "WHERE r.total > p.total")
    assert texts(a, b) == ["A 只保留 paid.total > refunded.total 的行，B 没有这个条件",
                           "B 只保留 paid.total < refunded.total 的行，A 没有这个条件"]


def test_a_real_difference_next_to_renamed_derived_tables_is_reported_once():
    """派生来源起的名字不同、里面又真有不同时，只报真正不同的那一处：CTE 里少一个条件，外层按 CTE 算出来的列
    筛选的条件不跟着报（不然两边各报一句「只保留 total > 100 的行」，自相矛盾）；外层的条件本身不同，两边配成
    一对说。"""
    a = ("WITH user_totals AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 "
         "GROUP BY user_id) SELECT COUNT(*) FROM user_totals WHERE total > 100")
    inner = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
             "SELECT COUNT(*) FROM t WHERE total > 100")
    assert texts(a, inner, labels=("原流程", "盲算")) == ["原流程只保留 is_deleted = 0 的行，盲算没有这个条件"]
    outer = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE is_deleted = 0 GROUP BY user_id) "
             "SELECT COUNT(*) FROM t WHERE total > 200")
    assert texts(a, outer, labels=("原流程", "盲算")) == ["原流程按 total > 100 筛选，盲算按 total > 200 筛选"]


# --------------------------------------------------------------------------
# diff_facets：两边的派生来源按结构配对
# --------------------------------------------------------------------------

#: 两个 CTE 算出了同名的列 total：一个是付款合计，一个是退款合计
PAID_REFUNDED = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
                 "refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) ")
PR_JOIN = "FROM paid p JOIN refunded r ON r.user_id = p.user_id"
#: 同样两段写成 FROM 里的子查询
PR_SUBQUERIES = ("FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p "
                 "JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r "
                 "ON r.user_id = p.user_id")


@pytest.mark.parametrize("form", ["cte", "subquery"])
@pytest.mark.parametrize("a,b,expected", [
    ("SELECT SUM(p.total) {j}", "SELECT SUM(r.total) {j}",
     ["原流程对 paid.total 求和，盲算对 refunded.total 求和"]),
    ("SELECT SUM(p.total - r.total) {j}", "SELECT SUM(r.total - p.total) {j}",
     ["原流程对 paid.total - refunded.total 求和，盲算对 refunded.total - paid.total 求和"]),
    ("SELECT COUNT(*) {j} WHERE p.total > 100", "SELECT COUNT(*) {j} WHERE r.total > 100",
     ["原流程按 paid.total > 100 筛选，盲算按 refunded.total > 100 筛选"]),
    ("SELECT p.user_id {j} ORDER BY p.total DESC LIMIT 10", "SELECT p.user_id {j} ORDER BY r.total DESC LIMIT 10",
     ["原流程取行前按 paid.total 降序排，盲算按 refunded.total 降序排"]),
    ("SELECT p.user_id {j} GROUP BY p.user_id HAVING MAX(p.total) > 10",
     "SELECT p.user_id {j} GROUP BY p.user_id HAVING MAX(r.total) > 10",
     ["原流程对 paid.total 取最大值，盲算对 refunded.total 取最大值",
      "原流程分组后只保留 MAX(paid.total) > 10 的组，盲算只保留 MAX(refunded.total) > 10 的组"]),
    ("SELECT COUNT(*) {j} WHERE p.total - r.total > 100", "SELECT COUNT(*) {j} WHERE r.total - p.total > 100",
     ["原流程只保留 paid.total - refunded.total > 100 的行，盲算没有这个条件",
      "盲算只保留 refunded.total - paid.total > 100 的行，原流程没有这个条件"]),
], ids=["sum", "sum-of-difference", "where", "order-by", "having", "where-difference"])
def test_which_derived_table_a_column_comes_from_is_reported(form, a, b, expected):
    """两个派生来源算出了同名的列（付款合计、退款合计都叫 total），编号也对得上：外层用的是哪一个的 total 不同，
    聚合、条件、排序、HAVING 里都得照报，不能因为都是「某个 CTE 的 total」就当成一样。只读得出列名的写法两边
    一样时，改用带来源名字的写法说。"""
    if form == "cte":
        head, j = PAID_REFUNDED, PR_JOIN
    else:
        head, j = "", PR_SUBQUERIES
        expected = [t.replace("paid.", "p.").replace("refunded.", "r.") for t in expected]
    assert texts(head + a.format(j=j), head + b.format(j=j), labels=("原流程", "盲算")) == expected


def test_renamed_and_reordered_derived_tables_still_tell_their_columns_apart():
    """盲算给两个 CTE 另起了名字、定义和关联的先后也反过来：按里面读的表和各列的定义认出谁对谁，外层用的是
    哪一个的列照样分得清；同一列只是名字、先后不同的不报。"""
    b_head = ("WITH ref_sum AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
              "pay_sum AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) ")
    b_join = "FROM ref_sum r JOIN pay_sum p ON p.user_id = r.user_id"
    labels = ("原流程", "盲算")
    assert texts(PAID_REFUNDED + f"SELECT SUM(p.total) {PR_JOIN}",
                 b_head + f"SELECT SUM(r.total) {b_join}", labels=labels) == \
        ["原流程对 paid.total 求和，盲算对 ref_sum.total 求和"]
    assert texts(PAID_REFUNDED + f"SELECT COUNT(*) {PR_JOIN} WHERE p.total > 100",
                 b_head + f"SELECT COUNT(*) {b_join} WHERE r.total > 100", labels=labels) == \
        ["原流程按 paid.total > 100 筛选，盲算按 ref_sum.total > 100 筛选"]
    assert texts(PAID_REFUNDED + f"SELECT SUM(p.total) {PR_JOIN} WHERE p.total > r.total",
                 b_head + f"SELECT SUM(p.total) {b_join} WHERE r.total < p.total") == []


def test_an_extra_derived_table_on_one_side_is_not_paired_with_another():
    """盲算多一个排在前面的 CTE（vip），位置编号就错开了：原流程的 t 要和盲算的 t 配成一对（里面读的表、各列的
    定义都一样），不能按编号配给 vip。"""
    a = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id")
    b = ("WITH vip AS (SELECT id FROM users WHERE vip = 1), "
         "t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN vip ON vip.id = u.id JOIN t ON t.user_id = u.referrer_id")
    assert texts(a, b, labels=("原流程", "盲算")) == [
        "盲算只保留 vip = 1 的行，原流程没有这个条件",
        "原流程关联 t 表的条件是 orders.user_id = users.id，盲算是 orders.user_id = users.referrer_id",
        "盲算关联了 vip 表，原流程没有"]
    # 两边各一个、结构毫不相干的派生来源也不配成一对：各说各的
    other = ("WITH vip AS (SELECT id FROM users WHERE vip = 1) "
             "SELECT COUNT(*) FROM users u JOIN vip ON vip.id = u.id")
    joins = [d.text for d in diff_facets(facets(a), facets(other), labels=("原流程", "盲算")) if d.aspect == "join"]
    assert joins == ["原流程关联了 t 表，盲算没有", "盲算关联了 vip 表，原流程没有"]


def test_unused_ctes_take_part_in_pairing():
    """两边都定义了付款、退款两个 CTE，原流程关联付款的、盲算关联退款的：没用到的那个也参加配对，付款配付款、退款
    配退款，不会把两边各自用到的那个只凭列名都叫 user_id、total 凑成一对，说成「关联条件不同」。"""
    a = PAID_REFUNDED + "SELECT COUNT(*) FROM users u JOIN paid p ON p.user_id = u.id"
    b = PAID_REFUNDED + "SELECT COUNT(*) FROM users u JOIN refunded r ON r.user_id = u.id"
    assert texts(a, b, labels=("原流程", "盲算")) == ["原流程关联了 paid 表，盲算没有", "盲算关联了 refunded 表，原流程没有"]


@pytest.mark.parametrize("a,b,expected", [
    ("SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM stores s JOIN orders o ON o.store_id = s.id WHERE o.status = 'paid'",
     ["盲算关联了 stores 表，原流程没有"]),
    ("SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id",
     "SELECT SUM(o.amount) FROM users u JOIN refunds r ON r.user_id = u.id JOIN orders o ON o.id = r.order_id",
     ["盲算关联了 users 表，原流程没有"]),
    (PAID_REFUNDED + f"SELECT COUNT(*) {PR_JOIN} WHERE p.total > r.total",
     "WITH vip AS (SELECT id FROM users WHERE vip = 1), "
     "paid AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
     "refunded AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
     "SELECT COUNT(*) FROM vip v JOIN refunded r ON r.user_id = v.id JOIN paid p ON p.user_id = r.user_id "
     "WHERE p.total > r.total",
     ["盲算只保留 vip = 1 的行，原流程没有这个条件", "盲算关联了 vip 表，原流程没有", "盲算用到了 users 表，原流程没有"]),
    # 原流程只在条件的子查询里用到 users：那不是连接的一头，盲算多关联的还是 users
    ("SELECT SUM(o.amount) FROM orders o WHERE o.user_id IN (SELECT id FROM users WHERE vip = 1)",
     "SELECT SUM(o.amount) FROM users u JOIN orders o ON o.user_id = u.id WHERE u.vip = 1",
     ["原流程只保留 user_id IN (SELECT id FROM users WHERE vip = 1) 的行，盲算没有这个条件",
      "盲算关联了 users 表，原流程没有"]),
], ids=["real-table", "real-table-chain", "derived-shifted-and-swapped", "in-subquery-is-not-a-join"])
def test_an_extra_source_written_in_from_is_named_correctly(a, b, expected):
    """盲算多出来的来源写在 FROM 里、靠内连接接上原流程也有的表（FROM stores JOIN orders）：多出来的是 stores，
    不能说成「盲算关联了 orders 表」（原流程查的就是 orders）；已经说了「关联了 stores 表」的，不再另说一遍「用到了
    stores 表」。"""
    assert texts(a, b, labels=("原流程", "盲算")) == expected


def test_a_lateral_subquery_is_named_by_its_alias():
    """LATERAL 子查询的别名挂在 LATERAL 上：取哪几行的说明里照写它，不写「子查询」。"""
    a = ("SELECT o.id FROM orders o, LATERAL (SELECT amount FROM refunds r WHERE r.order_id = o.id "
         "ORDER BY amount DESC LIMIT 1) l")
    b = ("SELECT o.id FROM orders o CROSS JOIN LATERAL (SELECT amount FROM refunds r WHERE r.order_id = o.id "
         "ORDER BY amount DESC LIMIT 2) AS x")
    assert texts(a, b, "postgres") == ["A 在 l 里只取前 1 行，B 在 x 里只取前 2 行"]


def test_a_paired_derived_join_with_another_name_says_both_names():
    """配成一对的派生来源两边名字不同（原流程叫 t、盲算叫 totals）：关联条件不同时两个名字都说出来。"""
    a = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN t ON t.user_id = u.id")
    b = ("WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM users u JOIN totals ON totals.user_id = u.referrer_id")
    assert texts(a, b, labels=("原流程", "盲算")) == [
        "原流程关联 t 表的条件是 orders.user_id = users.id，盲算关联 totals 表的条件是 orders.user_id = users.referrer_id"]
    left = b.replace("JOIN totals ON totals.user_id = u.referrer_id", "LEFT JOIN totals ON totals.user_id = u.id")
    assert texts(a, left, labels=("原流程", "盲算")) == ["原流程用 INNER JOIN 关联 t 表，盲算用 LEFT JOIN 关联 totals 表"]


@pytest.mark.parametrize("a,b", [
    (PAID_REFUNDED + f"SELECT COUNT(*) {PR_JOIN} WHERE p.total > r.total",
     PAID_REFUNDED + "SELECT COUNT(*) FROM refunded r JOIN paid p ON p.user_id = r.user_id WHERE p.total > r.total"),
    (f"SELECT COUNT(*) {PR_SUBQUERIES} WHERE p.total > r.total",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r "
     "JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p ON p.user_id = r.user_id "
     "WHERE p.total > r.total"),
    # 两个 CTE 读同一张表、各列的定义也一样，只差筛选条件：按整段的写法认得出谁是谁
    ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), "
     "open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) "
     "SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > o.total",
     "WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), "
     "open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) "
     "SELECT COUNT(*) FROM open o JOIN paid p ON p.user_id = o.user_id WHERE o.total < p.total"),
    # 实表也一样：内连接换个先后写，意思不变
    ("SELECT SUM(o.amount) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE o.amount > r.amount",
     "SELECT SUM(o.amount) FROM refunds r JOIN orders o ON o.id = r.order_id WHERE o.amount > r.amount"),
    ("SELECT SUM(o.amount) FROM orders o, refunds r WHERE r.order_id = o.id",
     "SELECT SUM(o.amount) FROM refunds r, orders o WHERE r.order_id = o.id"),
], ids=["cte", "subquery", "same-shape-ctes", "real-tables", "comma-join"])
def test_swapping_the_order_of_an_inner_join_is_no_difference(a, b):
    """内连接两边换个先后（FROM paid JOIN refunded 换成 FROM refunded JOIN paid）意思不变：派生来源按结构配对，
    比较的两边按配好的编号排，不会排成两个方向、报出两条自相矛盾的话；连接按涉及的两边和条件比，不按哪一侧是
    关联进来的。"""
    assert texts(a, b) == []


def test_two_same_shape_derived_tables_are_paired_by_their_conditions():
    """两个 CTE 读同一张表、各列定义也一样（付款的、未付的），盲算两个都另起了名字、都多加了 is_deleted = 0、关联的
    先后也反过来：结构上只剩里面的条件分得开，按条件的重合配（status = 'paid' 配 status = 'paid'），只报多出来的那个
    条件，外层拿两者比的条件不跟着报。"""
    a = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), "
         "open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) "
         "SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > o.total")
    b = ("WITH unpaid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' AND is_deleted = 0 "
         "GROUP BY user_id), "
         "settled AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' AND is_deleted = 0 "
         "GROUP BY user_id) "
         "SELECT COUNT(*) FROM unpaid u JOIN settled s ON s.user_id = u.user_id WHERE u.total < s.total")
    assert texts(a, b, labels=("原流程", "盲算")) == ["盲算只保留 is_deleted = 0 的行，原流程没有这个条件"]


def test_limits_inside_derived_tables_are_paired_by_structure():
    """CTE 里取哪几行按配好对的派生来源比，不按出现的先后：盲算多一个排在前面、也限了行数的 CTE，不能拿它和
    原流程的 top 比。"""
    a = ("WITH top AS (SELECT user_id, amount FROM orders ORDER BY amount DESC LIMIT 10) "
         "SELECT SUM(amount) FROM top")
    b = ("WITH vip AS (SELECT id FROM users WHERE vip = 1 LIMIT 5), "
         "top AS (SELECT user_id, amount FROM orders ORDER BY amount DESC LIMIT 10) "
         "SELECT SUM(top.amount) FROM top JOIN vip ON vip.id = top.user_id")
    found = [d.text for d in diff_facets(facets(a), facets(b), labels=("原流程", "盲算")) if d.aspect == "limit"]
    assert found == ["原流程不限行数，盲算在 vip 里只取前 5 行"]


def test_a_changed_column_inside_a_renamed_derived_table_is_said_once():
    """CTE 里一列的算法变了（对 amount 求和换成对 paid_amount 求和），名字也另起了：按读的表和选出来的列名仍配成
    一对，只报里面那处不同，外层按这一列写的条件不跟着报成两边各有一个。"""
    a = ("WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM t WHERE total > 100")
    b = ("WITH vip AS (SELECT id FROM users WHERE vip = 1), "
         "x AS (SELECT user_id, SUM(paid_amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM vip JOIN x ON x.user_id = vip.id WHERE total > 100")
    found = diff_facets(facets(a), facets(b), labels=("原流程", "盲算"))
    assert "原流程对 amount 求和，盲算对 paid_amount 求和" in [d.text for d in found]
    assert not [d for d in found if d.aspect == "filter" and "total" in d.text]


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


# --------------------------------------------------------------------------
# diff_facets：外连接以哪一侧为主表
# --------------------------------------------------------------------------

LABELS = ("原流程", "盲算")


@pytest.mark.parametrize("dialect,a,b,expected", [
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id",
     ["原流程以 orders 为主表外连接 refunds，盲算以 refunds 为主表外连接 orders"]),
    # 「没有退款的订单数」对「order_id 为空的退款数」：IS NULL 不会把外连接变成内连接，差的是主表
    ("sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.order_id IS NULL",
     "SELECT COUNT(*) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE r.order_id IS NULL",
     ["原流程以 orders 为主表外连接 refunds，盲算以 refunds 为主表外连接 orders"]),
    ("postgres", "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id",
     ["原流程以 refunds 为主表外连接 orders，盲算以 orders 为主表外连接 refunds"]),
    ("postgres", PAID_REFUNDED + "SELECT COUNT(*) FROM paid p LEFT JOIN refunded r ON r.user_id = p.user_id",
     PAID_REFUNDED + "SELECT COUNT(*) FROM refunded r LEFT JOIN paid p ON p.user_id = r.user_id",
     ["原流程以 paid 为主表外连接 refunded，盲算以 refunded 为主表外连接 paid"]),
    ("mysql",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p "
     "LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r ON r.user_id = p.user_id",
     "SELECT COUNT(*) FROM (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) r "
     "LEFT JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) p ON p.user_id = r.user_id",
     ["原流程以 p 为主表外连接 r，盲算以 r 为主表外连接 p"]),
], ids=["left", "left-anti", "right", "cte", "subquery"])
def test_which_side_an_outer_join_keeps_is_compared(dialect, a, b, expected):
    """外连接两边对调，保留的就是另一张表的全部行（有没有退款的订单都算，还是有没有订单的退款都算）：按主表比，
    关联的表、条件一样也要报出来。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


@pytest.mark.parametrize("dialect,a,b", [
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM refunds r RIGHT JOIN orders o ON o.id = r.order_id"),
    ("postgres", PAID_REFUNDED + "SELECT COUNT(*) FROM paid p LEFT JOIN refunded r ON r.user_id = p.user_id",
     PAID_REFUNDED + "SELECT COUNT(*) FROM refunded r RIGHT JOIN paid p ON p.user_id = r.user_id"),
    # 外连接后又在 WHERE 里筛可空那一侧：等于内连接加同一个条件
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done'",
     "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id WHERE r.state = 'done'"),
    # 筛的是主表那一侧，外连接照旧
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     "SELECT COUNT(*) FROM orders AS o LEFT OUTER JOIN refunds AS r ON o.id = r.order_id WHERE o.status = 'paid'"),
    # 外连接链换了先后，各自的主表不变
    ("sqlite",
     "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id LEFT JOIN users u ON u.id = o.user_id",
     "SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.id = o.user_id LEFT JOIN refunds r ON r.order_id = o.id"),
], ids=["left-vs-right", "left-vs-right-cte", "rejected-left-vs-inner", "filter-on-kept-side", "chain-reordered"])
def test_the_same_outer_join_written_differently_is_no_difference(dialect, a, b):
    """orders LEFT JOIN refunds 和 refunds RIGHT JOIN orders 是同一次连接：按主表比，不按写的是 LEFT 还是 RIGHT。"""
    assert texts(a, b, dialect) == []


@pytest.mark.parametrize("dialect,a,b,expected", [
    ("sqlite", "SELECT COUNT(*) FROM orders o LEFT JOIN (SELECT * FROM refunds WHERE state = 'done') r "
               "ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o LEFT JOIN (SELECT * FROM refunds) r ON r.order_id = o.id WHERE r.state = 'done'",
     ["原流程以 orders 为主表外连接 r，盲算的 WHERE 里有 refunds.state = 'done'，把关联不上 r 的行筛掉了，"
      "LEFT JOIN 实际成了 INNER JOIN"]),
    ("mysql", "WITH done AS (SELECT * FROM refunds WHERE state = 'done') "
              "SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id",
     "WITH done AS (SELECT * FROM refunds) "
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN done d ON d.order_id = o.id WHERE d.state = 'done'",
     ["原流程以 orders 为主表外连接 done，盲算的 WHERE 里有 refunds.state = 'done'，把关联不上 done 的行筛掉了，"
      "LEFT JOIN 实际成了 INNER JOIN"]),
    # 盲算以 refunds 为主表，WHERE 又筛掉了没对上订单的行：没有退款的已付订单原流程算、盲算不算
    ("postgres",
     "SELECT SUM(o.amount) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     "SELECT SUM(o.amount) FROM refunds r LEFT JOIN orders o ON o.id = r.order_id WHERE o.status = 'paid'",
     ["原流程以 orders 为主表外连接 refunds，盲算的 WHERE 里有 orders.status = 'paid'，把关联不上 orders 的行筛掉了，"
      "LEFT JOIN 实际成了 INNER JOIN"]),
    ("postgres", "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id",
     "SELECT COUNT(*) FROM orders o RIGHT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'paid'",
     ["盲算只保留 status = 'paid' 的行，原流程没有这个条件",
      "原流程以 refunds 为主表外连接 orders，盲算的 WHERE 里有 orders.status = 'paid'，把关联不上 orders 的行筛掉了，"
      "RIGHT JOIN 实际成了 INNER JOIN"]),
], ids=["subquery", "cte", "swapped-and-filtered", "right"])
def test_an_outer_join_turned_inner_by_where_is_reported(dialect, a, b, expected):
    """可空那一侧的条件写在 ON / 子查询里，和写在 WHERE 里不是一回事：WHERE 里拿空值比不成立，没关联上的行被筛掉，
    LEFT JOIN 实际成了内连接。条件本身两边一样（透过子查询都是 refunds.state = 'done'），只报连接。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


def test_only_null_rejecting_where_conditions_turn_an_outer_join_inner():
    """IS NULL、和 IS NULL 用 OR 连着的、套在 COALESCE 里的，没关联上的行照样留着：外连接不变（宁可漏报）。"""
    left = "SELECT COUNT(*) FROM orders o LEFT JOIN refunds r ON r.order_id = o.id"
    inner = "SELECT COUNT(*) FROM orders o JOIN refunds r ON r.order_id = o.id"
    either = " WHERE r.state = 'done' OR r.id IS NULL"
    assert texts(left + either, inner + either) == ["A 用 LEFT JOIN 关联 refunds 表，B 用 INNER JOIN"]
    assert aspects(left + " WHERE COALESCE(r.amount, 0) < 100", left) == ["filter"]


# --------------------------------------------------------------------------
# diff_facets：算出来的列
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dialect,a,b,expected", [
    ("sqlite", PAID_REFUNDED + f"SELECT SUM(p.total) - SUM(r.total) {PR_JOIN}",
     PAID_REFUNDED + f"SELECT SUM(r.total) - SUM(p.total) {PR_JOIN}",
     ["原流程算的是 SUM(paid.total) - SUM(refunded.total)，盲算算的是 SUM(refunded.total) - SUM(paid.total)"]),
    # 退款率的分子分母对调
    ("postgres", f"SELECT SUM(r.total) / SUM(p.total) {PR_SUBQUERIES}",
     f"SELECT SUM(p.total) / SUM(r.total) {PR_SUBQUERIES}",
     ["原流程算的是 SUM(r.total) / SUM(p.total)，盲算算的是 SUM(p.total) / SUM(r.total)"]),
    # 窗口：每个用户的合计还是每家门店的合计
    ("sqlite", "SELECT order_id, SUM(amount) OVER (PARTITION BY user_id) AS s FROM orders",
     "SELECT order_id, SUM(amount) OVER (PARTITION BY store_id) AS s FROM orders",
     ["原流程算的是 SUM(amount) OVER (PARTITION BY user_id)，盲算算的是 SUM(amount) OVER (PARTITION BY store_id)"]),
    # 同一个合计再算一下（分换成元）：和光一个聚合配成一对说
    ("sqlite", "SELECT SUM(amount) FROM orders", "SELECT SUM(amount) / 100 FROM orders",
     ["原流程算的是 SUM(amount)，盲算算的是 SUM(amount) / 100"]),
    ("sqlite", "SELECT SUM(amount) - SUM(fee) FROM orders", "SELECT SUM(amount), SUM(fee) FROM orders",
     ["原流程多算了 SUM(amount) - SUM(fee)，盲算没有"]),
], ids=["difference", "ratio", "window-partition", "scaled", "one-sided"])
def test_how_an_output_column_is_computed_is_compared(dialect, a, b, expected):
    """最外层算出来的列按算法比：两边用到的聚合一模一样（SUM(paid.total)、SUM(refunded.total)），只是谁减谁、谁除谁
    换了，聚合那一面看不出来。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


@pytest.mark.parametrize("dialect,a,b", [
    ("sqlite", PAID_REFUNDED + f"SELECT SUM(p.total) - SUM(r.total) {PR_JOIN}",
     PAID_REFUNDED + "SELECT SUM(p.total) - SUM(r.total) FROM refunded r JOIN paid p ON p.user_id = r.user_id"),
    ("sqlite", "SELECT region, SUM(amount) * 1.0 / COUNT(*) AS avg_amt FROM orders GROUP BY region",
     "SELECT o.region, 1.0 * SUM(o.amount) / COUNT(*) FROM orders o GROUP BY 1"),
    ("postgres", "SELECT order_id, RANK() OVER (PARTITION BY user_id ORDER BY amount DESC) AS k FROM orders",
     "SELECT o.order_id, RANK() OVER (PARTITION BY o.user_id ORDER BY o.amount DESC) FROM orders o"),
    # 最外层取整到分、空值记 0：还是那个合计
    ("sqlite", "SELECT SUM(amount) FROM orders", "SELECT ROUND(SUM(amount), 2) FROM orders"),
    ("mysql", "SELECT SUM(amount) FROM orders", "SELECT IFNULL(SUM(amount), 0) FROM orders"),
], ids=["swapped-join", "commutative", "window", "round", "default"])
def test_the_same_output_computation_written_differently_is_no_difference(dialect, a, b):
    """别名、加法乘法的先后、最外层取整、空值记 0 这类写法不同，算的是同一个数。"""
    assert texts(a, b, dialect) == []


@pytest.mark.parametrize("dialect,a,b,expected", [
    # 往里挪一层：net 的 v 是谁减谁
    ("sqlite",
     PAID_REFUNDED + ", net AS (SELECT p.user_id, p.total - r.total AS v FROM paid p JOIN refunded r "
                     "ON r.user_id = p.user_id) SELECT SUM(v) FROM net",
     PAID_REFUNDED + ", net AS (SELECT p.user_id, r.total - p.total AS v FROM paid p JOIN refunded r "
                     "ON r.user_id = p.user_id) SELECT SUM(v) FROM net",
     ["原流程的 net.v 是 paid.total - refunded.total，盲算是 refunded.total - paid.total"]),
    ("postgres",
     PAID_REFUNDED + ", net AS (SELECT p.user_id, p.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) "
                     "SELECT COUNT(*) FROM net WHERE v > 100",
     PAID_REFUNDED + ", net AS (SELECT p.user_id, r.total AS v FROM paid p JOIN refunded r ON r.user_id = p.user_id) "
                     "SELECT COUNT(*) FROM net WHERE v > 100",
     ["原流程的 net.v 是 paid.total，盲算是 refunded.total"]),
    ("mysql",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, p.total " + PR_SUBQUERIES + ") x",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, r.total " + PR_SUBQUERIES + ") x",
     ["原流程的 x.total 是 p.total，盲算是 r.total"]),
    ("sqlite", "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH t AS (SELECT user_id, amount * 0.8 AS net FROM orders) SELECT SUM(net) FROM t",
     ["原流程的 t.net 是 amount * 0.9，盲算是 amount * 0.8"]),
    ("mysql",
     "WITH t AS (SELECT user_id, CASE WHEN status = 'paid' THEN amount ELSE 0 END AS paid_amt FROM orders) "
     "SELECT SUM(paid_amt) FROM t",
     "WITH t AS (SELECT user_id, CASE WHEN status IN ('paid', 'shipped') THEN amount ELSE 0 END AS paid_amt "
     "FROM orders) SELECT SUM(paid_amt) FROM t",
     ["原流程的 t.paid_amt 是 CASE WHEN status = 'paid' THEN amount ELSE 0 END，"
      "盲算是 CASE WHEN status IN ('paid', 'shipped') THEN amount ELSE 0 END"]),
    ("postgres", "SELECT COUNT(*) FROM (SELECT user_id, amount - fee AS net FROM orders) a WHERE a.net > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, amount + fee AS net FROM orders) b WHERE b.net > 100",
     ["原流程的 a.net 是 amount - fee，盲算的 b.net 是 amount + fee"]),
    # 按支付月份还是下单月份分组
    ("mysql", "WITH t AS (SELECT DATE_FORMAT(paid_at, '%Y-%m') AS m, amount FROM orders) "
              "SELECT m, SUM(amount) FROM t GROUP BY m",
     "WITH t AS (SELECT DATE_FORMAT(created_at, '%Y-%m') AS m, amount FROM orders) "
     "SELECT m, SUM(amount) FROM t GROUP BY m",
     ["原流程的 t.m 是 DATE_FORMAT(paid_at, '%Y-%m')，盲算是 DATE_FORMAT(created_at, '%Y-%m')"]),
    ("sqlite", "WITH t AS (SELECT strftime('%Y-%m', paid_at) AS m, amount FROM orders) "
               "SELECT m, SUM(amount) FROM t GROUP BY m",
     "WITH t AS (SELECT strftime('%Y-%m', created_at) AS m, amount FROM orders) "
     "SELECT m, SUM(amount) FROM t GROUP BY m",
     ["原流程的 t.m 是 STRFTIME('%Y-%m', paid_at)，盲算是 STRFTIME('%Y-%m', created_at)"]),
    # 窗口：每人第一笔还是最后一笔；有并列时 RANK 和 ROW_NUMBER 取到的行数不同
    ("postgres",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) "
     "AS rn FROM orders) t WHERE rn = 1",
     "SELECT SUM(amount) FROM (SELECT user_id, amount, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at DESC) "
     "AS rn FROM orders) t WHERE rn = 1",
     ["原流程的 t.rn 是 ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at)，"
      "盲算是 ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at DESC)"]),
    ("mysql", "WITH r AS (SELECT user_id, amount, RANK() OVER (ORDER BY amount DESC) AS k FROM orders) "
              "SELECT SUM(amount) FROM r WHERE k <= 10",
     "WITH r AS (SELECT user_id, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS k FROM orders) "
     "SELECT SUM(amount) FROM r WHERE k <= 10",
     ["原流程的 r.k 是 RANK() OVER (ORDER BY amount DESC)，盲算是 ROW_NUMBER() OVER (ORDER BY amount DESC)"]),
    # 外面直接 SELECT *：每一列都用到了
    ("sqlite", "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT * FROM t",
     "WITH t AS (SELECT user_id, amount * 0.8 AS net FROM orders) SELECT * FROM t",
     ["原流程的 t.net 是 amount * 0.9，盲算是 amount * 0.8"]),
    # 递归 CTE 自己写了列名：按列名找各支同一位置的定义
    ("postgres", "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 10) SELECT SUM(n) FROM t",
     "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 2 FROM t WHERE n < 10) SELECT SUM(n) FROM t",
     ["原流程的 t.n 是 1 | n + 1，盲算是 1 | n + 2"]),
], ids=["nested-difference", "nested-column", "nested-subquery", "scale", "case", "renamed-subquery", "month",
        "month-strftime", "window-order", "window-function", "select-star", "recursive"])
def test_how_a_derived_column_is_computed_is_reported(dialect, a, b, expected):
    """外面写的都是 t.net、net.v、x.total，两边一样；它在 CTE、子查询里怎么算的不一样，要报出来，而且只报这一处：
    外面按它写的条件、聚合两边写法一样，不跟着报。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


@pytest.mark.parametrize("dialect,a,b", [
    ("sqlite", "WITH t AS (SELECT user_id, amount * 0.9 AS net FROM orders) SELECT SUM(net) FROM t",
     "WITH x AS (SELECT user_id, 0.9 * amount AS net FROM orders) SELECT SUM(x.net) FROM x"),
    # 外面没用到的列算法不同，不影响结果
    ("sqlite", "WITH t AS (SELECT user_id, amount, paid_at AS ts FROM orders) SELECT SUM(amount) FROM t",
     "WITH t AS (SELECT user_id, amount, created_at AS ts FROM orders) SELECT SUM(amount) FROM t"),
    # 取日期的两种写法
    ("postgres", "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) SELECT d, SUM(amount) FROM t GROUP BY d",
     "WITH t AS (SELECT CAST(paid_at AS DATE) AS d, amount FROM orders) SELECT d, SUM(amount) FROM t GROUP BY d"),
    ("mysql",
     "SELECT SUM(x.total) FROM (SELECT p.user_id, p.total " + PR_SUBQUERIES + ") x",
     "SELECT SUM(y.total) FROM (SELECT a.user_id, a.total FROM (SELECT user_id, SUM(amount) AS total FROM refunds "
     "GROUP BY user_id) b JOIN (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) a "
     "ON a.user_id = b.user_id) y"),
], ids=["commutative", "unused-column", "date-cast", "nested-renamed"])
def test_the_same_derived_column_written_differently_is_no_difference(dialect, a, b):
    """派生来源里一列的写法不同、算的一样（0.9 * amount 和 amount * 0.9、DATE() 和 CAST(… AS DATE)），或者外面根本
    没用到那一列：不报。"""
    assert texts(a, b, dialect) == []


@pytest.mark.parametrize("dialect,a,b,expected", [
    # 换了名字、算法一样：配成同一列
    ("sqlite", "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
               "SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH x AS (SELECT user_id, SUM(amount) AS sum_amt FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM x WHERE sum_amt > 100", []),
    ("sqlite", "WITH t AS (SELECT user_id, SUM(amount) AS total, COUNT(*) AS n FROM orders GROUP BY user_id) "
               "SELECT COUNT(*) FROM t WHERE total > 100 AND n > 1",
     "WITH t AS (SELECT user_id, SUM(amount) AS s, COUNT(*) AS c FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM t WHERE s > 100 AND c > 1", []),
    ("sqlite", "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) "
               "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT user_id, amount, DATE(paid_at) AS day FROM orders) "
     "SELECT SUM(amount) FROM t WHERE day >= '2026-09-01'", []),
    # 换了名字又改了算法：两边各剩一列，配成同一列，外面的条件不报成两边各有一个
    ("sqlite", "WITH t AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
               "SELECT COUNT(*) FROM t WHERE total > 100",
     "WITH x AS (SELECT user_id, SUM(amount - discount) AS net_total FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM x WHERE net_total > 100",
     ["原流程对 amount 求和，盲算对 amount - discount 求和",
      "原流程的 t.total 是 SUM(amount)，盲算的 x.net_total 是 SUM(amount - discount)"]),
], ids=["renamed", "two-renamed", "renamed-date", "renamed-and-changed"])
def test_derived_columns_renamed_on_one_side_are_paired(dialect, a, b, expected):
    """CTE、子查询选出来的列外面按名字引用，盲算常给它另起名字（total、sum_amt）：按定义配成同一列，外面按它写的条件
    不算两边各有一个。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


# --------------------------------------------------------------------------
# diff_facets：时间列写在 CTE、子查询里
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_time_column_computed_in_a_cte_is_seen_through(dialect):
    """按支付时间还是下单时间筛，日期表达式写在 CTE 里也认得出：时间那一面透过 d = DATE(paid_at) 认出是 paid_at，带上
    表名、列名给调用方对照数据目录；d 的定义不同不再另报一遍。"""
    a = ("WITH t AS (SELECT user_id, amount, DATE(paid_at) AS d FROM orders) "
         "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'")
    b = a.replace("DATE(paid_at)", "DATE(created_at)")
    [diff] = diff_facets(facets(a, dialect), facets(b, dialect), labels=LABELS,
                         names={"orders.paid_at": "支付时间", "created_at": "下单时间"})
    assert (diff.aspect, diff.text) == ("time", "原流程按支付时间 paid_at 筛时间，盲算按下单时间 created_at")
    assert diff.detail == {"a": [{"table": "orders", "column": "paid_at"}],
                           "b": [{"table": "orders", "column": "created_at"}]}


@pytest.mark.parametrize("dialect,a,b,expected", [
    ("postgres", "SELECT SUM(x.amount) FROM (SELECT amount, CAST(paid_at AS DATE) AS day FROM orders) x "
                 "WHERE x.day BETWEEN '2026-09-01' AND '2026-09-30'",
     "SELECT SUM(y.amount) FROM (SELECT amount, CAST(created_at AS DATE) AS day FROM orders) y "
     "WHERE y.day BETWEEN '2026-09-01' AND '2026-09-30'",
     ["原流程按 paid_at 筛时间，盲算按 created_at"]),
    # 两个 CTE 同名、定义对调：原流程筛付款时间，盲算筛退款时间
    ("mysql", "WITH a AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id), "
              "b AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id) "
              "SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'",
     "WITH a AS (SELECT user_id, MAX(refunded_at) AS ts FROM refunds GROUP BY user_id), "
     "b AS (SELECT user_id, MAX(paid_at) AS ts FROM orders GROUP BY user_id) "
     "SELECT COUNT(*) FROM a JOIN b ON b.user_id = a.user_id WHERE a.ts >= '2026-09-01'",
     ["原流程按 paid_at 筛时间，盲算按 refunded_at"]),
    # 带时区偏移的不是同一天：时间列一样，d 的定义不同照报
    ("sqlite", "WITH t AS (SELECT amount, DATE(paid_at) AS d FROM orders) "
               "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT amount, DATE(paid_at, '+8 hours') AS d FROM orders) "
     "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     ["原流程的 t.d 是 DATE(paid_at)，盲算是 DATE(paid_at, '+8 hours')"]),
    ("postgres", "WITH t AS (SELECT DATE(paid_at) AS d, amount FROM orders) "
                 "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'",
     "WITH t AS (SELECT CAST(paid_at AS DATE) AS d, amount FROM orders) "
     "SELECT SUM(amount) FROM t WHERE d >= '2026-09-01'", []),
], ids=["cast-between", "same-name-swapped", "time-zone", "date-or-cast"])
def test_time_columns_inside_derived_tables(dialect, a, b, expected):
    assert texts(a, b, dialect, labels=LABELS) == expected


# --------------------------------------------------------------------------
# diff_facets：两边写法一样、意思不同时说清楚
# --------------------------------------------------------------------------


def test_same_named_derived_tables_with_swapped_definitions_are_told_apart():
    """两边的 CTE 同名、定义对调了（盲算的 t1 是原流程的 t2）：按结构配对之后 t1.total 两边不是同一列，可只写
    「t1.total > 100」两边一模一样，自相矛盾。带上各自的来历：原流程筛付款合计，盲算筛退款合计。"""
    a = ("WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
         "t2 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
         "SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100")
    b = ("WITH t1 AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
         "t2 AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
         "SELECT COUNT(*) FROM t1 JOIN t2 ON t2.user_id = t1.user_id WHERE t1.total > 100")
    assert texts(a, b, "postgres", labels=LABELS) == [
        "原流程按 t1.total > 100（t1.total 即 SUM(orders.amount)）筛选，"
        "盲算按 t1.total > 100（t1.total 即 SUM(refunds.amount)）筛选"]
    a_sum = ("WITH a AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id), "
             "b AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id) "
             "SELECT SUM(a.total) FROM a JOIN b ON b.user_id = a.user_id")
    b_sum = ("WITH a AS (SELECT user_id, SUM(amount) AS total FROM refunds GROUP BY user_id), "
             "b AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id) "
             "SELECT SUM(a.total) FROM a JOIN b ON b.user_id = a.user_id")
    assert texts(a_sum, b_sum, labels=LABELS) == [
        "原流程对 a.total（即 SUM(orders.amount)）求和，盲算对 a.total（即 SUM(refunds.amount)）求和"]


def test_same_named_derived_tables_that_differ_only_in_conditions_say_which_is_which():
    """两个 CTE 读同一张表、各列定义一样，只差里面的条件；盲算把两个名字的定义对调了、还都多了 is_deleted = 0。按条件
    的重合配（不按名字：盲算的 paid 筛的是 status = 'open'），外面的「> 100」筛的就是未付合计；来历也一样（都是
    SUM(orders.amount)），说清两边同名的各对应哪一个。"""
    a = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY user_id), "
         "open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' GROUP BY user_id) "
         "SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > 100")
    b = ("WITH paid AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'open' AND is_deleted = 0 "
         "GROUP BY user_id), "
         "open AS (SELECT user_id, SUM(amount) AS total FROM orders WHERE status = 'paid' AND is_deleted = 0 "
         "GROUP BY user_id) "
         "SELECT COUNT(*) FROM paid p JOIN open o ON o.user_id = p.user_id WHERE p.total > 100")
    assert texts(a, b, labels=LABELS) == [
        "原流程按 paid.total > 100 筛选，"
        "盲算按 paid.total > 100（盲算的 paid 对应原流程的 open、盲算的 open 对应原流程的 paid）筛选",
        "盲算只保留 is_deleted = 0 的行，原流程没有这个条件"]


# --------------------------------------------------------------------------
# diff_facets：同一个条件筛的先后
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dialect,a,b,expected", [
    # 「第一单就是已付的人数」对「有已付订单的人数」
    ("postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn "
     "FROM orders) t WHERE t.rn = 1 AND t.status = 'paid'",
     "SELECT COUNT(*) FROM (SELECT user_id, status, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn "
     "FROM orders WHERE status = 'paid') t WHERE t.rn = 1",
     ["原流程先算 t 的窗口函数、再按 status = 'paid' 筛选，盲算先按 status = 'paid' 筛选、再算 t 的窗口函数"]),
    ("sqlite", "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t "
               "WHERE status = 'paid'",
     "WITH t AS (SELECT * FROM orders WHERE status = 'paid' ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t",
     ["原流程先取 t 的前 10 行、再按 status = 'paid' 筛选，盲算先按 status = 'paid' 筛选、再取 t 的前 10 行"]),
    ("sqlite", "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t "
               "WHERE paid_at >= '2026-09-01'",
     "WITH t AS (SELECT * FROM orders WHERE paid_at >= '2026-09-01' ORDER BY amount DESC LIMIT 10) "
     "SELECT SUM(amount) FROM t",
     ["原流程先取 t 的前 10 行、再按 paid_at >= '2026-09-01' 筛选，"
      "盲算先按 paid_at >= '2026-09-01' 筛选、再取 t 的前 10 行"]),
], ids=["window", "limit", "limit-time"])
def test_filtering_before_or_after_a_window_or_limit_is_reported(dialect, a, b, expected):
    """先取前 10 行再筛已付，和先筛已付再取前 10 行，取到的不是同一批：派生来源取前几行、算窗口函数时，外面的条件
    不再透成里面那张表的条件，两边只差在筛的先后，照报。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


@pytest.mark.parametrize("dialect,a,b", [
    # 按窗口的分区键筛，窗口里外都一样
    ("postgres",
     "SELECT COUNT(*) FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn "
     "FROM orders) t WHERE t.rn = 1 AND t.user_id > 100",
     "SELECT COUNT(*) FROM (SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY paid_at) AS rn "
     "FROM orders WHERE user_id > 100) t WHERE t.rn = 1"),
    # 两边都是先取前 10 行再筛，只是起的名字不同
    ("sqlite", "WITH t AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(amount) FROM t "
               "WHERE status = 'paid'",
     "WITH top10 AS (SELECT * FROM orders ORDER BY amount DESC LIMIT 10) SELECT SUM(t.amount) FROM top10 t "
     "WHERE t.status = 'paid'"),
], ids=["partition-key", "same-order"])
def test_filtering_at_an_equivalent_stage_is_no_difference(dialect, a, b):
    assert texts(a, b, dialect) == []


# --------------------------------------------------------------------------
# diff_facets：没用到的 CTE
# --------------------------------------------------------------------------


def test_a_cte_defined_but_not_used_does_not_count():
    """盲算定义了只筛已付的 CTE，却直接查 orders：少的那个条件要报出来；定义了没用的 CTE 里多一个条件、多一张表，
    不影响结果，不报。"""
    paid = "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') "
    assert texts(paid + "SELECT SUM(amount) FROM paid", paid + "SELECT SUM(amount) FROM orders", labels=LABELS) == \
        ["原流程只保留 status = 'paid' 的行，盲算没有这个条件"]
    assert texts("SELECT SUM(amount) FROM orders WHERE status = 'paid'",
                 "WITH junk AS (SELECT * FROM users WHERE vip = 1) "
                 "SELECT SUM(amount) FROM orders WHERE status = 'paid'") == []
    assert texts(paid + "SELECT SUM(amount) FROM paid",
                 paid + "SELECT SUM(amount) FROM orders WHERE status = 'paid'") == []


# --------------------------------------------------------------------------
# diff_facets：自己关联自己
# --------------------------------------------------------------------------

SELF_BASE = ("WITH base AS (SELECT user_id, referrer_id, SUM(amount) AS total FROM orders "
             "GROUP BY user_id, referrer_id) ")


@pytest.mark.parametrize("dialect,a,b,expected", [
    ("postgres", SELF_BASE + "SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id "
                             "WHERE b1.total > b2.total",
     SELF_BASE + "SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id WHERE b2.total > b1.total",
     ["原流程只保留 b1.total > b2.total 的行，盲算没有这个条件", "盲算只保留 b1.total < b2.total 的行，原流程没有这个条件"]),
    ("sqlite", "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o1.amount > o2.amount",
     "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o2.amount > o1.amount",
     ["原流程只保留 o2.amount < o1.amount 的行，盲算没有这个条件", "盲算只保留 o2.amount > o1.amount 的行，原流程没有这个条件"]),
    # 换了别名、换了关联先后：b1 还是出 referrer_id 的那一个
    ("postgres", SELF_BASE + "SELECT COUNT(*) FROM base b1 JOIN base b2 ON b2.user_id = b1.referrer_id "
                             "WHERE b1.total > b2.total",
     SELF_BASE + "SELECT COUNT(*) FROM base y JOIN base x ON x.referrer_id = y.user_id WHERE y.total < x.total", []),
    ("sqlite", "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o2.id = o1.parent_id WHERE o1.amount > o2.amount",
     "SELECT COUNT(*) FROM orders c JOIN orders p ON c.parent_id = p.id WHERE c.amount > p.amount", []),
], ids=["cte-reversed", "table-reversed", "cte-renamed", "table-renamed"])
def test_a_self_join_tells_its_two_aliases_apart(dialect, a, b, expected):
    """同一个 CTE、同一张表自己关联自己：两个别名按各自在关联条件里出的列区分（出 referrer_id 的、出 user_id 的），
    比较写反了要报；只是换了别名、换了关联先后不报。"""
    assert texts(a, b, dialect, labels=LABELS) == expected


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
