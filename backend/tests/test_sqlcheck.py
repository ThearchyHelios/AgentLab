"""基于数据目录的 SQL 检查（data/sqlcheck.py）：七条规则的正例、反例，级别随依据的状态变化，以及解析不了的写法。

用合成的景区业务库（tests/fixtures/catalog/scenic.py）和它的一份目录（scenic_notes.py）。检查只做有把握的判断：
解析不了、遇到不支持的写法、列对不上表，一律跳过不报——它的 error 会把图打回去改、挡住受管级别的发布、让出具降档，
误报的代价比漏报大。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data.engine import engines
from app.data.introspect import introspect
from app.data.sqlcheck import CHECK_CODES, SqlChecker, check_sql, dialect_of
from tests.fixtures.catalog import scenic_notes
from tests.fixtures.catalog.scenic_notes import ORDER_ITEMS_TO_ORDERS

_CACHE: dict[str, dict] = {}


@pytest.fixture
async def cache(scenic_db):
    """景区库的表结构缓存（真实探查一次：带外键和唯一约束，关系现推要用）。每个测试进程只探查一次。"""
    if scenic_db not in _CACHE:
        source = SimpleNamespace(id="sqlcheck-scenic", name="scenic", kind="sqlite", database=scenic_db, host=None,
                                 port=None, username=None, password=None, options={}, readonly=True, description="",
                                 schema_cache={}, origin="manual")
        try:
            _CACHE[scenic_db] = await introspect(source)
        finally:
            await engines.invalidate(source.id)
    return _CACHE[scenic_db]


def run(cache, sql, status="confirmed", override=None, kind="sqlite"):
    checker = SqlChecker(kind=kind, schema_cache=cache, notes=scenic_notes.notes(status, override),
                         source_name="scenic")
    return checker.check(sql)


def found(cache, sql, status="confirmed", override=None, kind="sqlite"):
    """[(code, level, table, column)]，按检查给出的顺序。"""
    return [(c.code, c.level, c.table, c.column) for c in run(cache, sql, status, override, kind)]


def codes(cache, sql, status="confirmed", override=None):
    return {c.code for c in run(cache, sql, status, override)}


# --------------------------------------------------------------------------
# 结果的形状
# --------------------------------------------------------------------------


async def test_result_shape_and_wording(cache):
    sql = ("SELECT o.channel_id, SUM(o.total_amount) AS amt FROM orders o "
           "JOIN order_items i ON i.order_id = o.id WHERE o.status = 1 GROUP BY o.channel_id")
    [check] = run(cache, sql)
    out = check.as_dict()
    assert set(out) == {"code", "level", "message", "for_model", "table", "column", "relation_id", "sql_excerpt"}
    assert out["code"] == "fanout_sum" and out["level"] == "error"
    assert out["table"] == "orders" and out["column"] == "total_amount"
    assert out["relation_id"] == ORDER_ITEMS_TO_ORDERS
    assert "SUM(o.total_amount)" in out["sql_excerpt"]
    assert out["message"].startswith("「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。")
    # 给模型的写 SQL 里的真名和改法；给人看的不出现工具名和对模型的指令
    assert "orders" in out["for_model"] and "order_items" in out["for_model"]
    assert "db_schema__" not in out["message"] and "db_" not in out["message"]
    # 没有的可选字段不出现
    single = run(cache, "SELECT AVG(discount_rate) FROM member_levels")[0].as_dict()
    assert "relation_id" not in single and single["column"] == "discount_rate"
    assert set(CHECK_CODES) == {"fanout_sum", "stock_summed", "join_unconfirmed", "ratio_aggregated",
                                "missing_valid_filter", "unknown_code", "wrong_date_column"}


def test_dialects():
    assert dialect_of("sqlite") == "sqlite"
    assert dialect_of("postgres") == dialect_of("postgresql") == "postgres"
    assert dialect_of("mysql") == dialect_of("mariadb") == "mysql"
    assert dialect_of("oracle") == "oracle"
    assert dialect_of("mssql") is None and dialect_of(None) is None


# --------------------------------------------------------------------------
# fanout_sum：沿一对多关联之后对「一」那一侧的度量列求和、计数
# --------------------------------------------------------------------------

FANOUT = [
    "SELECT SUM(o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 先写明细、后连订单：订单照样随明细重复
    "SELECT SUM(o.total_amount) FROM order_items i JOIN orders o ON o.id = i.order_id WHERE o.status = 1",
    # 包一层 COALESCE、CASE 里只用订单的列：还是订单的数
    "SELECT SUM(COALESCE(o.total_amount, 0)) FROM orders o LEFT JOIN order_items i ON o.id = i.order_id "
    "WHERE o.status = 1",
    "SELECT SUM(CASE WHEN o.status = 1 THEN o.total_amount END) FROM orders o JOIN order_items i "
    "ON i.order_id = o.id AND i.qty > 0",
    # 逗号连接、条件写在 WHERE 里
    "SELECT SUM(o.total_amount) FROM orders o, order_items i WHERE i.order_id = o.id AND o.status = 1",
]


@pytest.mark.parametrize("sql", FANOUT)
async def test_fanout_sum_positive(cache, sql):
    assert ("fanout_sum", "error", "orders", "total_amount") in found(cache, sql)


async def test_fanout_count_of_the_one_side_key(cache):
    sql = "SELECT COUNT(o.id) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1"
    [check] = run(cache, sql)
    assert (check.code, check.level, check.table, check.column) == ("fanout_sum", "error", "orders", "id")
    assert "计数会重复计算" in check.message


async def test_fanout_through_two_hops_with_an_inferred_foreign_key(cache):
    """支付记录没写目录：订单 → 支付记录的关系按外键现推（verified），明细经订单连支付记录，明细的金额照样重复。"""
    sql = ("SELECT SUM(i.amount) FROM order_items i JOIN orders o ON o.id = i.order_id "
           "JOIN payments p ON p.order_id = o.id WHERE o.status = 1")
    [check] = [c for c in run(cache, sql) if c.code == "fanout_sum"]
    assert (check.level, check.table, check.column) == ("error", "order_items", "amount")
    assert "「订单」关联「payments」是一对多，「订单明细」的每一行会随之重复" in check.message
    assert "去掉与「payments」的关联" in check.message


FANOUT_NEGATIVE = [
    # 对「多」那一侧求和：本来就在明细粒度上
    "SELECT SUM(i.amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    "SELECT SUM(i.amount) FROM order_items i JOIN orders o ON o.id = i.order_id WHERE o.status = 1",
    # 不关联
    "SELECT SUM(o.total_amount) FROM orders o WHERE o.status = 1",
    # 去重计数、去重求和：写的人已经在处理重复
    "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 两边的列一起算：按明细逐行算出来的数
    "SELECT SUM(i.qty * o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 按明细的主键分组：每组只有一行明细，订单不重复
    "SELECT i.id, SUM(o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1 "
    "GROUP BY i.id",
    # 明细先在子查询里按订单汇总再连：一对一
    "SELECT SUM(o.total_amount), SUM(x.n) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n FROM order_items "
    "GROUP BY order_id) x ON x.order_id = o.id WHERE o.status = 1",
    # 计数所有行：说不清是要数订单还是数明细
    "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 窗口函数：不是分组聚合
    "SELECT o.id, SUM(o.total_amount) OVER (PARTITION BY o.channel_id) FROM orders o "
    "JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 反连接：只留没有明细的订单，每笔订单至多一行
    "SELECT SUM(o.total_amount) FROM orders o LEFT JOIN order_items i ON i.order_id = o.id "
    "WHERE i.id IS NULL AND o.status = 1",
]


@pytest.mark.parametrize("sql", FANOUT_NEGATIVE)
async def test_fanout_sum_negative(cache, sql):
    assert "fanout_sum" not in codes(cache, sql)


async def test_fanout_level_follows_the_status_of_its_basis(cache):
    sql = FANOUT[0]
    assert ("fanout_sum", "error", "orders", "total_amount") in found(cache, sql, "verified")
    assert ("fanout_sum", "info", "orders", "total_amount") in found(cache, sql, "proposed")
    # 关系确认了、度量类型只是推断：依据里有推断就降为 info
    assert ("fanout_sum", "info", "orders", "total_amount") in found(
        cache, sql, override={("orders", "columns.total_amount.measure"): "proposed"})
    # 关系被驳回：不知道是一对多，不报重复计算；关联本身对不上目录，单独提示
    rejected = found(cache, sql, override={("order_items", f"relations.{ORDER_ITEMS_TO_ORDERS}"): "rejected"})
    assert not [f for f in rejected if f[0] == "fanout_sum"]
    assert ("join_unconfirmed", "warning", "order_items", None) in rejected
    # 度量类型被驳回：不知道订单金额能不能加，不报
    assert "fanout_sum" not in codes(cache, sql, override={("orders", "columns.total_amount.measure"): "rejected"})


# --------------------------------------------------------------------------
# stock_summed：存量跨时间求和
# --------------------------------------------------------------------------

STOCK = [
    "SELECT SUM(on_hand) FROM inventory_snapshots",
    'SELECT "storeID", SUM(on_hand) FROM inventory_snapshots GROUP BY "storeID"',
    "SELECT SUM(s.on_hand) FROM inventory_snapshots s WHERE s.snapshot_date BETWEEN '2026-09-01' AND '2026-09-30'",
]


@pytest.mark.parametrize("sql", STOCK)
async def test_stock_summed_positive(cache, sql):
    assert ("stock_summed", "error", "inventory_snapshots", "on_hand") in found(cache, sql)


STOCK_NEGATIVE = [
    "SELECT snapshot_date, SUM(on_hand) FROM inventory_snapshots GROUP BY snapshot_date",
    "SELECT SUM(on_hand) FROM inventory_snapshots WHERE snapshot_date = '2026-09-30'",
    "SELECT substr(snapshot_date, 1, 7) AS m, SUM(on_hand) FROM inventory_snapshots GROUP BY substr(snapshot_date, 1, 7)",
    "SELECT AVG(on_hand) FROM inventory_snapshots",
    "SELECT MAX(on_hand) FROM inventory_snapshots",
    "SELECT snapshot_date, SUM(on_hand) OVER (PARTITION BY snapshot_date) FROM inventory_snapshots",
    # 经日期维度表分组：维度表的日期和快照日期相等
    "SELECT d.biz_date, SUM(s.on_hand) FROM inventory_snapshots s JOIN dim_dates d ON d.biz_date = s.snapshot_date "
    "GROUP BY d.biz_date",
]


@pytest.mark.parametrize("sql", STOCK_NEGATIVE)
async def test_stock_summed_negative(cache, sql):
    assert "stock_summed" not in codes(cache, sql)


async def test_stock_level_follows_status(cache):
    sql = STOCK[0]
    assert ("stock_summed", "error", "inventory_snapshots", "on_hand") in found(cache, sql, "verified")
    assert ("stock_summed", "info", "inventory_snapshots", "on_hand") in found(cache, sql, "proposed")
    assert "stock_summed" not in codes(cache, sql, "rejected")
    # 业务日期被驳回：不知道按哪一列算时间，不报
    assert "stock_summed" not in codes(cache, sql, override={("inventory_snapshots", "business_date"): "rejected"})


# --------------------------------------------------------------------------
# join_unconfirmed：关联条件对不上目录里的关系
# --------------------------------------------------------------------------

async def test_join_unconfirmed_positive(cache):
    # 列连错了：订单的主键对明细的主键
    wrong = found(cache, "SELECT SUM(i.amount) FROM orders o JOIN order_items i ON i.id = o.id WHERE o.status = 1")
    assert ("join_unconfirmed", "warning", "order_items", None) in wrong
    [check] = [c for c in run(cache, "SELECT o.id FROM orders o JOIN members m ON m.id = o.channel_id "
                                     "WHERE o.status = 1") if c.code == "join_unconfirmed"]
    assert check.level == "warning" and "o.channel_id" in check.sql_excerpt
    assert check.table == "members" and "「订单」与「members」的关联条件" in check.message
    # 只对上命名推断出来的关系（入园记录.member_id → 会员）：提示一句，级别 info
    proposed = found(cache, "SELECT m.name FROM visits v JOIN members m ON m.id = v.member_id WHERE v.status = 1")
    assert ("join_unconfirmed", "info", "members", None) in proposed


JOIN_NEGATIVE = [
    "SELECT SUM(i.amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1",
    # 多写的过滤条件不影响对关系
    "SELECT SUM(i.amount) FROM orders o JOIN order_items i ON i.order_id = o.id AND i.qty > 0 WHERE o.status = 1",
    # 没写目录的表之间按外键现推（verified）
    "SELECT SUM(p.paid_amount) FROM orders o JOIN payments p ON p.order_id = o.id WHERE o.status = 1",
    # 两张表都没写目录：没有可对照的目录，不判
    "SELECT COUNT(*) FROM tour_groups t JOIN agencies a ON a.channel_id = t.agency_id",
    # 子查询、CTE 当一边：不知道它的粒度，不判
    "WITH x AS (SELECT order_id, SUM(amount) AS a FROM order_items GROUP BY order_id) "
    "SELECT SUM(x.a) FROM orders o JOIN x ON x.order_id = o.id WHERE o.status = 1",
    # 自己连自己：目录不记这种关系
    "SELECT c.name FROM categories c JOIN categories p ON p.id = c.\"parentId\"",
    # 没有等值条件
    "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.amount > o.total_amount WHERE o.status = 1",
    # OR 连起来的条件：说不清按哪条连
    "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id OR i.id = o.id WHERE o.status = 1",
]


@pytest.mark.parametrize("sql", JOIN_NEGATIVE)
async def test_join_unconfirmed_negative(cache, sql):
    assert "join_unconfirmed" not in codes(cache, sql)


async def test_join_level_follows_status(cache):
    sql = "SELECT SUM(i.amount) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1"
    assert "join_unconfirmed" not in codes(cache, sql, "confirmed")
    assert "join_unconfirmed" not in codes(cache, sql, "verified")
    # 结果记在 JOIN 引入的那张表上
    assert ("join_unconfirmed", "info", "order_items", None) in found(cache, sql, "proposed")
    assert ("join_unconfirmed", "warning", "order_items", None) in found(cache, sql, "rejected")


# --------------------------------------------------------------------------
# ratio_aggregated：对比率求和、求平均
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT AVG(discount_rate) FROM member_levels",
    "SELECT SUM(discount_rate) FROM member_levels",
    "SELECT ROUND(AVG(l.discount_rate), 2) FROM member_levels l",
])
async def test_ratio_aggregated_positive(cache, sql):
    assert found(cache, sql) == [("ratio_aggregated", "warning", "member_levels", "discount_rate")]


@pytest.mark.parametrize("sql", [
    "SELECT level_name, discount_rate FROM member_levels",
    "SELECT MAX(discount_rate) FROM member_levels",
    # 加权：比率乘上数量再加，是正确的算法
    "SELECT SUM(m.id * l.discount_rate) FROM members m JOIN member_levels l ON l.id = m.member_level_id",
])
async def test_ratio_aggregated_negative(cache, sql):
    assert "ratio_aggregated" not in codes(cache, sql)


async def test_ratio_level_follows_status(cache):
    sql = "SELECT AVG(discount_rate) FROM member_levels"
    assert found(cache, sql, "verified") == [("ratio_aggregated", "warning", "member_levels", "discount_rate")]
    assert found(cache, sql, "proposed") == [("ratio_aggregated", "info", "member_levels", "discount_rate")]
    assert found(cache, sql, "rejected") == []


# --------------------------------------------------------------------------
# missing_valid_filter：没按有效记录条件筛
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM visits",
    "SELECT SUM(visitor_count) FROM visits WHERE park_id = 1",
    "SELECT v.park_id, SUM(v.visitor_count) FROM visits v GROUP BY v.park_id",
])
async def test_missing_valid_filter_positive(cache, sql):
    [check] = run(cache, sql)
    assert (check.code, check.level, check.table) == ("missing_valid_filter", "warning", "visits")
    assert "有效记录条件「status = 1」" in check.message


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM visits WHERE status = 1",
    "SELECT COUNT(*) FROM visits v WHERE v.status IN (0, 1)",
    # 按状态分组：写的人就是要看每种状态
    "SELECT status, COUNT(*) FROM visits GROUP BY status",
    # 条件写在聚合里
    "SELECT SUM(CASE WHEN status = 1 THEN visitor_count ELSE 0 END) FROM visits",
    # CTE 里筛过
    "WITH ok AS (SELECT * FROM visits WHERE status = 1) SELECT COUNT(*) FROM ok",
    # 子查询原样透出、外层再筛
    "SELECT COUNT(*) FROM (SELECT * FROM visits) x WHERE x.status = 1",
])
async def test_missing_valid_filter_negative(cache, sql):
    assert "missing_valid_filter" not in codes(cache, sql)


async def test_valid_filter_level_follows_status(cache):
    sql = "SELECT COUNT(*) FROM visits"
    assert found(cache, sql, "verified") == [("missing_valid_filter", "warning", "visits", None)]
    assert found(cache, sql, "proposed") == [("missing_valid_filter", "info", "visits", None)]
    assert found(cache, sql, "rejected") == []


# --------------------------------------------------------------------------
# unknown_code：写了码值表里没有的值
# --------------------------------------------------------------------------

async def test_unknown_code_positive(cache):
    [check] = run(cache, "SELECT COUNT(*) FROM visits WHERE status = 3")
    assert (check.code, check.level, check.table, check.column) == ("unknown_code", "warning", "visits", "status")
    assert "「3」" in check.message and "1=有效、0=作废" in check.message
    [listed] = run(cache, "SELECT COUNT(*) FROM visits WHERE status IN (1, 5, 7)")
    assert "「5」、「7」" in listed.message and "「1」" not in listed.message
    assert found(cache, "SELECT COUNT(*) FROM channels WHERE channel_type = '线上'") == [
        ("unknown_code", "warning", "channels", "channel_type")]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM visits WHERE status = 1",
    "SELECT COUNT(*) FROM visits WHERE status = '1'",
    "SELECT COUNT(*) FROM visits WHERE 0 = status OR status = 1",
    "SELECT COUNT(*) FROM visits WHERE status IN (0, 1)",
    "SELECT COUNT(*) FROM visits WHERE status = 1.0",
    "SELECT COUNT(*) FROM channels WHERE channel_type = '分销'",
    # 模板和参数：值要到运行时才知道
    "SELECT COUNT(*) FROM visits WHERE status = '{{ input.status }}'",
    "SELECT COUNT(*) FROM visits WHERE status = {{ input.status }}",
    "SELECT COUNT(*) FROM visits WHERE status = ?",
    "SELECT COUNT(*) FROM visits WHERE status = :status",
])
async def test_unknown_code_negative(cache, sql):
    assert "unknown_code" not in codes(cache, sql)


async def test_unknown_code_level_follows_status(cache):
    sql = "SELECT COUNT(*) FROM channels WHERE channel_type = '线上'"
    assert found(cache, sql, "verified") == [("unknown_code", "warning", "channels", "channel_type")]
    assert found(cache, sql, "proposed") == [("unknown_code", "info", "channels", "channel_type")]
    assert found(cache, sql, "rejected") == []


# --------------------------------------------------------------------------
# wrong_date_column：按另一个时间列分组、筛选
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT substr(entered_at, 1, 10) AS d, SUM(fee) FROM parking_records GROUP BY substr(entered_at, 1, 10)",
    "SELECT SUM(p.fee) FROM parking_records p WHERE p.entered_at >= '2026-09-01'",
])
async def test_wrong_date_column_positive(cache, sql):
    [check] = run(cache, sql)
    assert (check.code, check.level, check.table, check.column) == (
        "wrong_date_column", "warning", "parking_records", "entered_at")
    assert "业务日期按「出场时间」计" in check.message and "「入场时间」" in check.message


@pytest.mark.parametrize("sql", [
    "SELECT substr(exited_at, 1, 10) AS d, SUM(fee) FROM parking_records GROUP BY substr(exited_at, 1, 10)",
    "SELECT SUM(fee) FROM parking_records WHERE exited_at >= '2026-09-01' AND entered_at IS NOT NULL",
    # 两个时间都用上了：写的人知道两者的区别
    "SELECT SUM(fee) FROM parking_records WHERE exited_at >= '2026-09-01' AND entered_at < '2026-09-30'",
    "SELECT entered_at, exited_at, fee FROM parking_records",
])
async def test_wrong_date_column_negative(cache, sql):
    assert "wrong_date_column" not in codes(cache, sql)


async def test_wrong_date_level_follows_status(cache):
    sql = "SELECT SUM(p.fee) FROM parking_records p WHERE p.entered_at >= '2026-09-01'"
    assert found(cache, sql, "verified") == [("wrong_date_column", "warning", "parking_records", "entered_at")]
    assert found(cache, sql, "proposed") == [("wrong_date_column", "info", "parking_records", "entered_at")]
    assert found(cache, sql, "rejected") == []


# --------------------------------------------------------------------------
# 复杂写法：照样分析得出，或者跳过；不报错、不误报
# --------------------------------------------------------------------------

CLEAN_COMPLEX = [
    # CTE + 窗口函数 + 子查询
    """WITH daily AS (
         SELECT substr(o.ordered_at, 1, 10) AS d, SUM(i.amount) AS amt
         FROM orders o JOIN order_items i ON i.order_id = o.id
         WHERE o.status = 1 GROUP BY substr(o.ordered_at, 1, 10))
       SELECT d, amt, SUM(amt) OVER (ORDER BY d) AS running,
              (SELECT COUNT(*) FROM visits v WHERE v.status = 1) AS visits
       FROM daily ORDER BY d""",
    # UNION
    "SELECT 'web' AS ch, SUM(total_amount) FROM orders WHERE status = 1 AND channel_id = 1 "
    "UNION ALL SELECT 'box', SUM(total_amount) FROM orders WHERE status = 1 AND channel_id = 3",
    # 相关子查询、EXISTS
    "SELECT o.id FROM orders o WHERE o.status = 1 AND EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id)",
    # 引号括起来的驼峰标识符
    'SELECT t.tag_name, COUNT(*) FROM member_tag_links l JOIN "memberTags" t ON t.id = l."memberTagId" '
    "GROUP BY t.tag_name",
]


@pytest.mark.parametrize("sql", CLEAN_COMPLEX)
async def test_complex_but_clean_sql_reports_nothing(cache, sql):
    assert run(cache, sql) == []


async def test_problems_inside_cte_and_union_are_found(cache):
    cte = ("WITH t AS (SELECT o.channel_id, SUM(o.total_amount) AS amt FROM orders o "
           "JOIN order_items i ON i.order_id = o.id WHERE o.status = 1 GROUP BY o.channel_id) SELECT * FROM t")
    assert ("fanout_sum", "error", "orders", "total_amount") in found(cache, cte)
    union = ("SELECT SUM(on_hand) FROM inventory_snapshots UNION ALL "
             "SELECT SUM(on_hand) FROM inventory_snapshots WHERE snapshot_date = '2026-09-30'")
    assert found(cache, union) == [("stock_summed", "error", "inventory_snapshots", "on_hand")]


@pytest.mark.parametrize("sql", [
    "SELECT FROM WHERE",
    "SELEC total_amount FROM orders",
    "SELECT SUM(o.total_amount FROM orders o JOIN order_items i ON i.order_id = o.id",
    "SELECT 1; SELECT SUM(on_hand) FROM inventory_snapshots",
    "DELETE FROM visits",
    "",
    "   ",
    # 方言特性：SQLite 读不了 Oracle 的 q 引号
    "SELECT SUM(on_hand) FROM inventory_snapshots WHERE q'[x]' = 'x'",
    # 模板里的控制语句：拼出来是什么样说不清
    "SELECT SUM(on_hand) FROM inventory_snapshots {% if x %}WHERE snapshot_date = '2026-09-30'{% endif %}",
    # 结构里没有的表、别名对不上的列
    "SELECT SUM(x.on_hand) FROM inventory_snapshots s",
    "SELECT SUM(on_hand) FROM not_a_table",
])
async def test_unparseable_or_unsupported_sql_is_skipped(cache, sql):
    assert run(cache, sql) == []


async def test_other_dialects(cache):
    pg = "SELECT s.on_hand::numeric FROM inventory_snapshots s WHERE s.snapshot_date = $1"
    assert run(cache, pg, kind="postgres") == []
    assert found(cache, "SELECT SUM(s.on_hand)::numeric FROM inventory_snapshots s", kind="postgres") == [
        ("stock_summed", "error", "inventory_snapshots", "on_hand")]
    mysql = "SELECT `v`.`status`, COUNT(*) FROM `visits` `v` WHERE `v`.`status` = 3 GROUP BY `v`.`status`"
    assert found(cache, mysql, kind="mysql") == [("unknown_code", "warning", "visits", "status")]
    # 能读就照常分析，读不了就跳过：只要求不抛
    assert isinstance(run(cache, "SELECT SUM(on_hand) FROM inventory_snapshots WHERE q'[it''s]' = 'x'",
                          kind="oracle"), list)
    assert run(cache, "SELECT SUM(on_hand) FROM inventory_snapshots", kind="mssql") == []


async def test_no_catalog_means_no_checks(cache):
    sql = "SELECT SUM(o.total_amount) FROM orders o JOIN order_items i ON i.id = o.id"
    assert SqlChecker(kind="sqlite", schema_cache=cache, notes={}).check(sql) == []
    assert check_sql(sql, kind="sqlite", schema_cache=cache, notes=None) == []
    # 没探查过结构：对不上表，一样不判
    assert check_sql(sql, kind="sqlite", schema_cache={}, notes=scenic_notes.notes()) == []


async def test_duplicates_are_folded_and_errors_come_first(cache):
    sql = ("SELECT SUM(o.total_amount), SUM(o.total_amount) * 2, AVG(l.discount_rate) "
           "FROM orders o JOIN order_items i ON i.order_id = o.id JOIN members m ON m.id = o.member_id "
           "JOIN member_levels l ON l.id = m.member_level_id WHERE o.status = 1")
    out = found(cache, sql)
    assert [f for f in out if f[0] == "fanout_sum"] == [("fanout_sum", "error", "orders", "total_amount")]
    levels = [f[1] for f in out]
    assert levels == sorted(levels, key=["error", "warning", "info"].index)


async def test_checker_builds_relation_graph_lazily_and_reuses_it(cache, monkeypatch):
    from app.data import catalog

    calls = []
    real = catalog.relation_graph
    monkeypatch.setattr(catalog, "relation_graph", lambda *a, **k: calls.append(1) or real(*a, **k))
    checker = SqlChecker(kind="sqlite", schema_cache=cache, notes=scenic_notes.notes())
    checker.check("SELECT AVG(discount_rate) FROM member_levels")
    assert calls == []                              # 单表查询用不到关系图
    checker.check(FANOUT[0])
    checker.check(FANOUT[1])
    assert calls == [1]
