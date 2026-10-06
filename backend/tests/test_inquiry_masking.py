"""审批质询的遮罩（engine/inquiry.py）：回答正文、查询清单、质询工件里都不露出数据源遮罩列的值，也不误伤别的数。

- 改了名字的遮罩列（起别名、套函数、经子查询或 CTE 转一道、UNION 的另一支、和 * 写在同一支里没起别名的表达式）
  照样遮；COUNT 只数个数、窗口的分区键只决定次序，不遮；认不出落在哪一列的（两个 * 之间、子查询里没起别名的表达式）、
  解析不了又写着遮罩列的，整份快照遮掉
- 按值认的遮罩值有门槛：纯数字够长（SECRET_DIGITS 位）、文字至少两个字；日期、小数、千分位里的一截不算；提问人自己
  写进问题、选中原文里的，报告列出的指标和运行输入的值，都不换；SQL 里照原样留着的阈值、行数只在回答正文里不换，
  格子里的同一个数照旧换
- SQL 里数字照原样留着的只有范围比较的边界（没把某一个值夹住的）和 LIMIT / OFFSET 的行数；在找某一个值的（等值、IN、
  LIKE、简单 CASE、IS [NOT] DISTINCT FROM，套了括号、写成行值、VALUES、ARRAY 的也算，还有 BETWEEN v AND v、
  >= v AND <= v 这类夹住某一个值的范围）和遮罩列比较时短的数也换；选出列、函数参数、算式里够长的遮罩值也换；字符串
  （范围边界也一样）、注释、名字按值换；SQL 解析不了时认不准哪个是阈值，一律不免
- 快照按列遮，另按值遮：别的列里写着遮罩值的格子也换成「已遮罩」；列名里写着遮罩值的，列名也换
- 组装回答之后还认得出遮罩值的（正文里复述的阈值除外），不交出去
- 遮罩值上万个时换一遍也不卡：不现编上万个分支的正则

数据都是虚构的：手机号、姓名、年龄、区域、金额。
"""
from __future__ import annotations

import json
import time

import pytest

from app.engine import inquiry as inquiry_mod

MASKED = "已遮罩"


def secrets_of(snapshot: dict, hidden: set[str]) -> inquiry_mod._Secrets:
    secrets = inquiry_mod._Secrets()
    secrets.add(snapshot, hidden)
    return secrets


# --------------------------------------------------------------------------
# 改了名字的遮罩列
# --------------------------------------------------------------------------


def hidden_of(sql: str, columns: list[str], masked: set[str] = frozenset({"buyer_phone"}), kind: str = "sqlite"
              ) -> set[str]:
    snapshot = {"source": "shop", "sql": sql, "columns": columns, "rows": []}
    return inquiry_mod._derived_hidden(snapshot, set(masked), kind)[0]


@pytest.mark.parametrize("sql, columns, hidden", [
    ("SELECT buyer_phone AS 手机号, amount AS 金额 FROM sales", ["手机号", "金额"], {"buyer_phone", "手机号"}),
    ("SELECT UPPER(buyer_phone), amount FROM sales", ["UPPER(buyer_phone)", "amount"],
     {"buyer_phone", "upper(buyer_phone)"}),
    ("SELECT t.p AS 号码 FROM (SELECT buyer_phone AS p FROM sales) t", ["号码"], {"buyer_phone", "p", "号码"}),
    ("WITH t AS (SELECT buyer_phone AS p, amount FROM sales) SELECT p, amount FROM t", ["p", "amount"],
     {"buyer_phone", "p"}),
    ("WITH t(p) AS (SELECT buyer_phone FROM sales) SELECT p AS 号码 FROM t", ["号码"], {"buyer_phone", "p", "号码"}),
    ("SELECT region AS 区域 FROM sales UNION ALL SELECT buyer_phone FROM sales", ["区域"], {"buyer_phone", "区域"}),
    ("SELECT (SELECT buyer_phone FROM sales LIMIT 1) AS 某个号码", ["某个号码"], {"buyer_phone", "某个号码"}),
])
def test_a_masked_column_under_another_name_is_masked(sql, columns, hidden):
    assert hidden_of(sql, columns) == hidden


SALES_COLUMNS = ["id", "region", "buyer_phone", "amount", "is_test"]


@pytest.mark.parametrize("sql, columns, hidden", [
    # * 后面没起别名的表达式：从结果末尾倒着数（SQLite 照写法原样给列名，起不出能往下传的名字）
    ("SELECT *, SUBSTR(buyer_phone, 4) FROM sales ORDER BY amount DESC LIMIT 1",
     [*SALES_COLUMNS, "SUBSTR(buyer_phone, 4)"], {"buyer_phone", "substr(buyer_phone, 4)"}),
    ("SELECT s.*, buyer_phone || '-x' FROM sales s", [*SALES_COLUMNS, "buyer_phone || '-x'"],
     {"buyer_phone", "buyer_phone || '-x'"}),
    # sqlglot 给 CAST 记的名字是里面的列名，结果的列名却是整个写法：照样按位置认
    ("SELECT *, CAST(buyer_phone AS TEXT) FROM sales", [*SALES_COLUMNS, "CAST(buyer_phone AS TEXT)"],
     {"buyer_phone", "cast(buyer_phone as text)"}),
    # * 前面的从头数，* 后面不是遮罩列的不遮
    ("SELECT UPPER(buyer_phone), s.*, amount * 2 FROM sales s", ["UPPER(buyer_phone)", *SALES_COLUMNS, "amount * 2"],
     {"buyer_phone", "upper(buyer_phone)"}),
    # 派生来源里 UNION 的另一支：结果的列名取第一支的
    ("SELECT * FROM (SELECT region FROM sales UNION ALL SELECT buyer_phone FROM sales) t", ["region"],
     {"buyer_phone", "region"}),
    ("WITH t(p) AS (SELECT region FROM sales UNION ALL SELECT buyer_phone FROM sales) SELECT p FROM t", ["p"],
     {"buyer_phone", "p"}),
    ("SELECT * FROM (SELECT *, buyer_phone AS p FROM sales) t", [*SALES_COLUMNS, "p"], {"buyer_phone", "p"}),
    # 多套几层括号不改变结果流到哪
    ("((SELECT UPPER(buyer_phone) FROM sales))", ["UPPER(buyer_phone)"], {"buyer_phone", "upper(buyer_phone)"}),
    ("SELECT * FROM ((SELECT buyer_phone AS p FROM sales)) t", ["p"], {"buyer_phone", "p"}),
])
def test_an_unnamed_expression_next_to_a_star_or_under_a_union_is_masked(sql, columns, hidden):
    assert hidden_of(sql, columns) == hidden


@pytest.mark.parametrize("sql, columns", [
    # 两个 * 之间：前后都数不出位置
    ("SELECT a.*, SUBSTR(a.buyer_phone, 4), b.* FROM sales a JOIN sales b ON a.id = b.id",
     [*SALES_COLUMNS, "SUBSTR(a.buyer_phone, 4)", *SALES_COLUMNS]),
    # 子查询里没起别名的表达式：外层的 * 原样带出来，列名各数据库起法不同，认不准
    ("SELECT * FROM (SELECT SUBSTR(buyer_phone, 4), amount FROM sales) t", ["SUBSTR(buyer_phone, 4)", "amount"]),
    ("SELECT * FROM (SELECT SUBSTR(buyer_phone, 4) FROM sales)", ["SUBSTR(buyer_phone, 4)"]),
    ("WITH t AS ((SELECT SUBSTR(buyer_phone, 4) FROM sales)) SELECT * FROM t", ["SUBSTR(buyer_phone, 4)"]),
    # UNION 第一支是 *：另一支的遮罩列落在哪个名字上说不清
    ("SELECT * FROM (SELECT * FROM sales UNION ALL SELECT id, region, SUBSTR(buyer_phone, 4), amount, is_test "
     "FROM sales) t", SALES_COLUMNS),
])
def test_a_masked_expression_that_cannot_be_placed_hides_the_whole_snapshot(sql, columns):
    assert hidden_of(sql, columns) == {"buyer_phone", *(c.lower() for c in columns)}


def test_a_lateral_subquery_is_read_like_a_derived_table():
    """Postgres 的 LATERAL：别名记在 LATERAL 上，起了别名的照名字传，没起别名的整份遮。"""
    assert hidden_of("SELECT x.* FROM sales s CROSS JOIN LATERAL (SELECT s.buyer_phone AS p) x", ["p"],
                     kind="postgresql") == {"buyer_phone", "p"}
    columns = [*SALES_COLUMNS, "substr"]
    assert hidden_of("SELECT * FROM sales s CROSS JOIN LATERAL (SELECT SUBSTR(s.buyer_phone, 4)) x", columns,
                     kind="postgresql") == {*columns}


@pytest.mark.parametrize("sql, columns", [
    ("SELECT region AS 区域, COUNT(DISTINCT buyer_phone) AS 买家数 FROM sales GROUP BY region", ["区域", "买家数"]),
    ("SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales WHERE buyer_phone = '13900000001' GROUP BY region",
     ["区域", "销售额"]),
    ("SELECT ROW_NUMBER() OVER (PARTITION BY buyer_phone ORDER BY amount) AS 序号 FROM sales", ["序号"]),
    ("SELECT (SELECT COUNT(*) FROM sales s2 WHERE s2.buyer_phone = s.buyer_phone) AS 笔数 FROM sales s", ["笔数"]),
    # 子查询里只数个数、条件里的子查询：值带不出来，外层的 * 也不怕
    ("SELECT * FROM (SELECT region, COUNT(DISTINCT buyer_phone) FROM sales GROUP BY region) t",
     ["region", "COUNT(DISTINCT buyer_phone)"]),
    ("SELECT region FROM sales WHERE amount > (SELECT AVG(LENGTH(buyer_phone)) FROM sales)", ["region"]),
])
def test_counting_filtering_and_ordering_by_a_masked_column_shows_nothing(sql, columns):
    assert hidden_of(sql, columns) == {"buyer_phone"}


def test_sql_that_cannot_be_read_but_names_a_masked_column_hides_everything():
    assert hidden_of("SELECT buyer_phone AS 号码 FROM sales WHERE (", ["号码", "金额"]) == {"buyer_phone", "号码", "金额"}
    assert hidden_of("SELECT region FROM sales WHERE (", ["region"]) == {"buyer_phone"}


def test_names_that_point_at_a_masked_column_are_known_for_comparisons():
    snapshot = {"source": "shop", "sql": "SELECT p FROM (SELECT buyer_phone AS p FROM sales) t", "columns": ["p"],
                "rows": []}
    assert inquiry_mod._derived_hidden(snapshot, {"buyer_phone"}, "sqlite")[1] == {"buyer_phone", "p"}


# --------------------------------------------------------------------------
# 按值认：门槛、边界
# --------------------------------------------------------------------------


def test_masked_values_are_matched_as_whole_values():
    """遮罩列的值按整值认：更长的数里的一截、千分位写法里的一截不换；一个字的值、太短的数不换（换了会把不相干的
    词、阈值弄坏）；数按原样和千分位两种写法认。"""
    snapshot = {"columns": ["phone", "name", "gender", "code", "salary", "region"],
                "rows": [["13900000001", "张伟", "男", "390", 12000, "华东"]]}
    scrub = secrets_of(snapshot, {"phone", "name", "gender", "code", "salary"}).scrub
    assert scrub("客户 13900000001、139000000012、1,390 元、390 笔、张伟买了男装，月薪 12,000，另 12000.5") == (
        "客户 已遮罩、139000000012、1,390 元、390 笔、已遮罩买了男装，月薪 已遮罩，另 12000.5")
    assert scrub("华东") == "华东", "没遮的列不换"
    assert inquiry_mod._Secrets().scrub("13900000001") == "13900000001"


def test_short_masked_numbers_leave_thresholds_dates_and_limits_alone():
    """遮罩列是年龄这类短的数：正文、SQL 里的阈值、日期、LIMIT 撞上同一个数也不换。"""
    snapshot = {"columns": ["age", "vip_level"], "rows": [[25, 10], [31, 10], [47, 10]]}
    scrub = secrets_of(snapshot, {"age", "vip_level"}).scrub
    text = "近 30 天里，2026-01-25 之后的 25 笔订单比 1 月 10 日多 47 笔"
    assert scrub(text) == text


def test_long_values_next_to_dates_and_decimals_are_not_cut_out_of_them():
    secrets = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    assert secrets.scrub("13900000001-2、13900000001.5、20261390000000-13900000001") == (
        "13900000001-2、13900000001.5、20261390000000-13900000001")
    assert secrets.scrub("号码：13900000001。") == "号码：已遮罩。"


def test_values_the_asker_wrote_themselves_are_left_alone():
    """提问人自己写进问题、选中原文里的数（「13900000001 这位客户」）照原样：问题本身就带着它，换了只会让回答对不上。"""
    secrets = secrets_of({"columns": ["phone"], "rows": [["13900000001"], ["13900000002"]]}, {"phone"})
    secrets.protect(["13900000001 这位客户买了几笔？", ""])
    assert secrets.scrub("13900000001 共 2 笔，13900000002 共 1 笔") == "13900000001 共 2 笔，已遮罩 共 1 笔"


def test_a_cell_holding_a_masked_value_is_masked_in_any_column():
    """别的列里写着同一个手机号（另一张表的联系方式没设遮罩）：按值换。短的数不按值换，免得把同样是 25 的笔数遮了。"""
    secrets = secrets_of({"columns": ["phone", "age"], "rows": [["13900000001", 25]]}, {"phone", "age"})
    snapshot = {"columns": ["联系方式", "笔数", "金额"], "column_types": {"联系方式": "text", "笔数": "number"},
                "rows": [["13900000001", 25, 13900000001], ["13900000009", 3, 1200]]}
    shown, masked = inquiry_mod._masked_view(snapshot, set(), secrets)
    assert shown["rows"] == [[MASKED, 25, MASKED], ["13900000009", 3, 1200]]
    assert masked == ["联系方式", "金额"]
    assert snapshot["rows"][0][0] == "13900000001", "原快照不动"
    by_name, masked = inquiry_mod._masked_view(snapshot, {"笔数"}, inquiry_mod._Secrets())
    assert [r[1] for r in by_name["rows"]] == [MASKED, MASKED] and masked == ["笔数"]
    assert by_name["column_types"]["笔数"] == "text"


# --------------------------------------------------------------------------
# SQL：只换字面量
# --------------------------------------------------------------------------


@pytest.fixture
def people() -> inquiry_mod._Secrets:
    return secrets_of({"columns": ["buyer_phone", "age"], "rows": [["13900000001", 25], ["13900000003", 31]]},
                      {"buyer_phone", "age"})


@pytest.mark.parametrize("sql, shown", [
    ("SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '13900000001'",
     "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '已遮罩'"),
    ("SELECT COUNT(*) FROM sales s WHERE s.buyer_phone IN ('13900000001', '13900000003') AND age BETWEEN 25 AND 31",
     "SELECT COUNT(*) FROM sales s WHERE s.buyer_phone IN ('已遮罩', '已遮罩') AND age BETWEEN 25 AND 31"),
    ("SELECT COUNT(*) FROM sales WHERE age = 25 AND amount > 25 AND paid_on >= '2026-01-25' LIMIT 25",
     "SELECT COUNT(*) FROM sales WHERE age = 已遮罩 AND amount > 25 AND paid_on >= '2026-01-25' LIMIT 25"),
    ("SELECT COUNT(*) FROM sales WHERE 25 = age AND note LIKE '%13900000003%' -- 13900000001 的单\nLIMIT 20",
     "SELECT COUNT(*) FROM sales WHERE 已遮罩 = age AND note LIKE '%已遮罩%' -- 已遮罩 的单\nLIMIT 20"),
    ("SELECT region FROM sales WHERE buyer_phone NOT IN ('13900000009') AND age <> 40",
     "SELECT region FROM sales WHERE buyer_phone NOT IN ('13900000009') AND age <> 40"),
    ("SELECT amount FROM sales WHERE amount = 13900000001", "SELECT amount FROM sales WHERE amount = 已遮罩"),
    # 和遮罩列做范围比较：边界是阈值，不是在找某个人，照原样（同一个数只从一边隔开：25 的几处都是「不超过 25」那一侧）
    ("SELECT COUNT(*) FROM sales WHERE age > 31 AND age <= 25 AND 25 < age AND age NOT BETWEEN 20 AND 25",
     "SELECT COUNT(*) FROM sales WHERE age > 31 AND age <= 25 AND 25 < age AND age NOT BETWEEN 20 AND 25"),
    # 不等、SQLite 的 IS、NOT LIKE 也是在找某一个值
    ("SELECT COUNT(*) FROM sales WHERE age != 25 AND age IS NOT 31 AND buyer_phone NOT LIKE '13900000001'",
     "SELECT COUNT(*) FROM sales WHERE age != 已遮罩 AND age IS NOT 已遮罩 AND buyer_phone NOT LIKE '已遮罩'"),
])
def test_sql_keeps_everything_but_the_masked_values(people, sql, shown):
    assert inquiry_mod._scrub_sql(sql, people, {"buyer_phone", "age"}, "sqlite") == shown


@pytest.fixture
def amounts() -> inquiry_mod._Secrets:
    """金额设了遮罩，金额都是五位数：和阈值、LIMIT、别的列的条件撞在一起。"""
    return secrets_of({"columns": ["amount"], "rows": [[10000], [12000], [20000], [30000], [50000]]}, {"amount"})


@pytest.mark.parametrize("sql, shown", [
    # 范围比较、LIMIT / OFFSET：长的数也照原样
    ("SELECT COUNT(*) FROM sales WHERE visits > 20000 AND amount >= 30000 AND amount BETWEEN 10000 AND 50000 "
     "LIMIT 10000 OFFSET 12000",
     "SELECT COUNT(*) FROM sales WHERE visits > 20000 AND amount >= 30000 AND amount BETWEEN 10000 AND 50000 "
     "LIMIT 10000 OFFSET 12000"),
    # 按同一个数分成两段（不低于、低于）：只从一边隔开，是阈值
    ("SELECT SUM(amount >= 30000) AS 大单, SUM(amount < 30000) AS 小单 FROM sales",
     "SELECT SUM(amount >= 30000) AS 大单, SUM(amount < 30000) AS 小单 FROM sales"),
    # 选出列、算式、函数参数里的数：认不准是不是在找某一个值，够长的遮罩值换（短的数照原样）
    ("SELECT 20000 AS 门槛, amount - 10000 AS 差额, ROUND(amount, 2) FROM sales",
     "SELECT 已遮罩 AS 门槛, amount - 已遮罩 AS 差额, ROUND(amount, 2) FROM sales"),
    ("SELECT COUNT(*) FROM sales WHERE amount - 30000 >= 0", "SELECT COUNT(*) FROM sales WHERE amount - 已遮罩 >= 0"),
    # 等值、IN：和遮罩列比较的换，和别的列比较的、够长的遮罩值也换
    ("SELECT COUNT(*) FROM sales WHERE amount = 30000 OR amount IN (10000, -12000) OR visits = 20000",
     "SELECT COUNT(*) FROM sales WHERE amount = 已遮罩 OR amount IN (已遮罩, -已遮罩) OR visits = 已遮罩"),
])
def test_long_masked_numbers_are_kept_only_as_thresholds_and_row_counts(amounts, sql, shown):
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == shown


@pytest.fixture
def phones() -> inquiry_mod._Secrets:
    """手机号存成整数（遮罩值在 SQL 里写成数字），年龄是短的数。"""
    return secrets_of({"columns": ["buyer_phone", "age"], "rows": [[13900000001, 25], [13900000002, 31]]},
                      {"buyer_phone", "age"})


@pytest.mark.parametrize("kind, sql, shown", [
    ("sqlite", "SELECT SUM(CASE buyer_phone WHEN 13900000001 THEN 1 ELSE 0 END) AS 笔数 FROM sales",
     "SELECT SUM(CASE buyer_phone WHEN 已遮罩 THEN 1 ELSE 0 END) AS 笔数 FROM sales"),
    ("sqlite", "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone IN (VALUES (13900000001), (13900000002))",
     "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone IN (VALUES (已遮罩), (已遮罩))"),
    ("sqlite", "SELECT COUNT(*) AS 笔数 FROM sales WHERE (buyer_phone, region) = (13900000001, '华东')",
     "SELECT COUNT(*) AS 笔数 FROM sales WHERE (buyer_phone, region) = (已遮罩, '华东')"),
    ("sqlite", "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone IS NOT DISTINCT FROM 13900000001",
     "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone IS NOT DISTINCT FROM 已遮罩"),
    ("sqlite", "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = (13900000001)",
     "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = (已遮罩)"),
    # 选出列里的同一个号码：认不准是不是在找某一个值，够长的遮罩值照样换，不留下「13900000001 AS 号码 … = 已遮罩」
    ("sqlite", "SELECT COUNT(*) AS 笔数, 13900000001 AS 号码 FROM sales WHERE buyer_phone = 13900000001",
     "SELECT COUNT(*) AS 笔数, 已遮罩 AS 号码 FROM sales WHERE buyer_phone = 已遮罩"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE COALESCE(buyer_phone, 0) = 13900000001",
     "SELECT COUNT(*) FROM sales WHERE COALESCE(buyer_phone, 0) = 已遮罩"),
    # 短的数：认得出被比较的是遮罩列才换
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age = (25) OR (age, region) = (31, '华东') OR age IN (VALUES (25)) "
     "OR age IS NOT DISTINCT FROM 31 OR age IS DISTINCT FROM 25",
     "SELECT COUNT(*) FROM sales WHERE age = (已遮罩) OR (age, region) = (已遮罩, '华东') OR age IN (VALUES (已遮罩)) "
     "OR age IS NOT DISTINCT FROM 已遮罩 OR age IS DISTINCT FROM 已遮罩"),
    ("sqlite", "SELECT SUM(CASE age WHEN 25 THEN 1 END) AS 人数, SUM(CASE WHEN age = 31 THEN 1 END) FROM sales",
     "SELECT SUM(CASE age WHEN 已遮罩 THEN 1 END) AS 人数, SUM(CASE WHEN age = 已遮罩 THEN 1 END) FROM sales"),
    ("postgresql", "SELECT COUNT(*) FROM sales WHERE buyer_phone = ANY(ARRAY[13900000001, 13900000002]) "
     "AND age <> ALL(ARRAY[25]) AND age = ANY(VALUES (31))",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone = ANY(ARRAY[已遮罩, 已遮罩]) "
     "AND age <> ALL(ARRAY[已遮罩]) AND age = ANY(VALUES (已遮罩))"),
])
def test_a_value_looked_up_in_any_shape_is_masked_in_the_sql(phones, kind, sql, shown):
    """在找某一个值的写法不止「col = 值」：简单 CASE、IN (VALUES …)、行值、IS NOT DISTINCT FROM、套了括号、
    = ANY(ARRAY[…])，都按语法树认出来；THEN 后面的 1、0 照原样。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, kind) == shown


@pytest.mark.parametrize("sql, shown", [
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000001 AND 13900000001",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone <= 13900000001",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE 13900000001 <= buyer_phone AND buyer_phone <= 13900000001",
     "SELECT COUNT(*) FROM sales WHERE 已遮罩 <= buyer_phone AND buyer_phone <= 已遮罩"),
    # 大于或小于就是不等于
    ("SELECT COUNT(*) FROM sales WHERE NOT (buyer_phone > 13900000001 OR buyer_phone < 13900000001)",
     "SELECT COUNT(*) FROM sales WHERE NOT (buyer_phone > 已遮罩 OR buyer_phone < 已遮罩)"),
    # 整数的 < v + 1 就是 <= v
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone < 13900000002",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone < 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 25",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= '13900000001' AND buyer_phone <= '13900000001'",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= '已遮罩' AND buyer_phone <= '已遮罩'"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN '13900000001' AND '13900000001'",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN '已遮罩' AND '已遮罩'"),
    # 一头是几个数算出来的（13900000002 - 1 就是 13900000001）：照样夹住了
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000001 AND 13900000002 - 1",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩 - 1"),
    # 同一个数在算式里被换掉了，边界上的也不留：留着就把算式里换掉的露了
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000001 AND 13900000001 + 0",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩 + 0"),
])
def test_a_range_that_pins_a_single_value_is_a_lookup(phones, sql, shown):
    """两头是同一个值的范围（BETWEEN v AND v、>= v AND <= v）、大于或小于同一个值、整数的半开区间，都只夹住了一个值，
    是在找它，和等值比较一样换。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == shown


@pytest.mark.parametrize("sql, shown", [
    ("SELECT COUNT(*) AS 笔数, 13900000001 AS 号码 FROM sales WHERE buyer_phone >= 13900000001",
     "SELECT COUNT(*) AS 笔数, 已遮罩 AS 号码 FROM sales WHERE buyer_phone >= 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone - 13900000001 < 5",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone - 已遮罩 < 5"),
])
def test_a_number_masked_elsewhere_in_the_sql_is_not_kept_as_a_threshold(phones, sql, shown):
    """同一个号码在选出列、算式里被换掉了，条件里的单边范围也不留：留着就把那里换掉的露了。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == shown
    assert inquiry_mod._shown_literals([(sql, "sqlite")]) <= {"5"}


def test_a_threshold_named_in_an_alias_or_a_comment_stays(amounts):
    """agent 把阈值写进别名、注释（AS 超过30000的笔数）：名字、注释照常按值换，阈值本身照原样，回答正文里复述也不遮。"""
    sql = "SELECT SUM(amount >= 30000) AS 超过30000的笔数 FROM sales -- 30000 以上算大单"
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == (
        "SELECT SUM(amount >= 30000) AS 超过已遮罩的笔数 FROM sales -- 已遮罩 以上算大单")
    assert inquiry_mod._shown_literals([(sql, "sqlite")]) == {"30000"}


def test_sql_starting_with_blank_lines_is_read_at_the_right_places():
    """agent 写的 SQL 常以换行开头：解析时去掉了开头的空白，按语法树认出的位置要补回来，才对得上原文里的词——阈值、
    行数照原样，找的那个号码换掉。"""
    secrets = secrets_of({"columns": ["buyer_phone", "amount"], "rows": [[13900000001, 30000]]}, {"buyer_phone", "amount"})
    sql = "\n  SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND buyer_phone IN (13900000001, 1) LIMIT 30000"
    assert inquiry_mod._scrub_sql(sql, secrets, {"buyer_phone", "amount"}, "sqlite") == (
        "\n  SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND buyer_phone IN (已遮罩, 1) LIMIT 30000")
    assert inquiry_mod._shown_literals([(sql, "sqlite")]) == {"30000"}


def test_string_range_bounds_are_masked_by_value(people):
    """字符串的范围边界按值换：写成字符串的长号码做边界，多半是在找那一个人；日期边界不是遮罩值，照原样。"""
    sql = "SELECT COUNT(*) FROM sales WHERE buyer_phone >= '13900000001' AND buyer_phone < '13900000003' " \
          "AND paid_on >= '2026-01-25'"
    assert inquiry_mod._scrub_sql(sql, people, {"buyer_phone", "age"}, "sqlite") == (
        "SELECT COUNT(*) FROM sales WHERE buyer_phone >= '已遮罩' AND buyer_phone < '已遮罩' "
        "AND paid_on >= '2026-01-25'")


def test_sql_that_cannot_be_parsed_keeps_no_thresholds():
    """SQL 解析不了：认不准哪个数是阈值，一律不免——够长的遮罩值在哪都换，简单的等值比较照样按词认（短的数也换）。"""
    secrets = secrets_of({"columns": ["age", "amount"], "rows": [[25, 30000], [31, 10000]]}, {"age", "amount"})
    sql = "SELECT COUNT(*) FROM sales WHERE age = 25 AND amount >= 30000 AND visits > 31 LIMIT 10000 GROUP"
    assert inquiry_mod._scrub_sql(sql, secrets, {"age", "amount"}, "sqlite") == (
        "SELECT COUNT(*) FROM sales WHERE age = 已遮罩 AND amount >= 已遮罩 AND visits > 31 LIMIT 已遮罩 GROUP")
    assert inquiry_mod._shown_literals([(sql, "sqlite")]) == set()


def test_only_thresholds_and_row_counts_count_as_shown():
    """照原样留在 SQL 里的只有范围边界（没夹住某一个值的）和 LIMIT / OFFSET 的行数；选出列、函数参数、CASE、夹住
    某一个值的范围里的数不算，在别处（这一条或别的查询里）被换掉的数也不算。"""
    shown = inquiry_mod._shown_literals
    assert shown([("SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND visits > 20000 LIMIT 10000 OFFSET 12000",
                   "sqlite")]) == {"30000", "20000", "10000", "12000"}
    assert shown([("SELECT 50000 AS 门槛, SUM(CASE amount WHEN 12000 THEN 1 END), ROUND(amount, 10000) FROM sales "
                   "WHERE amount BETWEEN 20000 AND 20000", "sqlite")]) == set()
    assert shown([("SELECT COUNT(*) FROM sales WHERE amount >= 30000", "sqlite"),
                  ("SELECT COUNT(*) FROM sales WHERE amount = 30000", "sqlite")]) == set()
    assert shown([("SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND note LIKE '%30000%'", "sqlite")]) == set()


@pytest.mark.parametrize("kind, sql, shown", [
    ("oracle", "SELECT COUNT(*) FROM sales WHERE buyer_phone = N'13900000001'",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone = N'已遮罩'"),
    ("postgresql", "SELECT COUNT(*) FROM sales WHERE buyer_phone = E'13900000001' OR note = $$13900000003$$ "
     "OR note = $t$13900000001$t$ OR buyer_phone = U&'13900000003'",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone = E'已遮罩' OR note = $$已遮罩$$ "
     "OR note = $t$已遮罩$t$ OR buyer_phone = U&'已遮罩'"),
    # 名字里写着遮罩值：带引号的、不带引号的（SQLite 的中文名字可以不加引号）
    ("postgresql", 'SELECT COUNT(*) AS "13900000001 的单" FROM sales',
     'SELECT COUNT(*) AS "已遮罩 的单" FROM sales'),
    ("mysql", "SELECT COUNT(*) AS `13900000001 的单` FROM sales", "SELECT COUNT(*) AS `已遮罩 的单` FROM sales"),
])
def test_prefixed_strings_and_names_are_scrubbed_too(people, kind, sql, shown):
    assert inquiry_mod._scrub_sql(sql, people, {"buyer_phone", "age"}, kind) == shown


def test_a_name_written_into_an_unquoted_alias_is_scrubbed():
    secrets = secrets_of({"columns": ["buyer_name"], "rows": [["张伟"], ["李娜"]]}, {"buyer_name"})
    assert inquiry_mod._scrub_sql("SELECT COUNT(*) AS 张伟的单数 FROM sales WHERE buyer_name = '张伟'", secrets,
                                  {"buyer_name"}, "sqlite") == (
        "SELECT COUNT(*) AS 已遮罩的单数 FROM sales WHERE buyer_name = '已遮罩'")


def test_numbers_left_as_written_in_the_sql_are_not_masked_in_the_answer(amounts):
    """范围比较的边界、LIMIT 照原样留在 SQL 里，回答正文里同样的数再遮也遮不住什么，只会和 SQL 对不上；等值比较
    里换掉的照旧遮。"""
    sql = "SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND amount <> 50000 LIMIT 10000"
    assert inquiry_mod._shown_literals([(sql, "sqlite")]) == {"30000", "10000"}
    amounts.show(inquiry_mod._shown_literals([(sql, "sqlite")]))
    assert amounts.scrub("金额不低于 30000 元、不是 50000 元的，取前 10000 条", prose=True) == (
        "金额不低于 30000 元、不是 已遮罩 元的，取前 10000 条")
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == (
        "SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND amount <> 已遮罩 LIMIT 10000")
    assert inquiry_mod._scrub_sql("SELECT COUNT(*) FROM sales WHERE amount = 30000", amounts, {"amount"},
                                  "sqlite") == "SELECT COUNT(*) FROM sales WHERE amount = 已遮罩", \
        "只免了正文里的：和遮罩列做等值比较的照旧换"


def test_a_threshold_left_in_the_sql_is_shown_in_the_prose_but_not_in_the_cells(amounts):
    """SQL 里照原样留着的阈值只在回答正文里不遮：格子里的同一个数照旧按值遮（col >= v 再取第一行，查出来的可能就是
    那一笔），列名、SQL 里的名字也照旧换。组装出来正文里的不算露出，格子里渲染出来的算。"""
    sql = "SELECT region AS 区域, visits AS 访问 FROM sales WHERE visits >= 20000 ORDER BY visits LIMIT 1"
    amounts.show(inquiry_mod._shown_literals([(sql, "sqlite")]))
    assert amounts.scrub("访问不少于 20000 次", prose=True) == "访问不少于 20000 次"
    assert amounts.scrub("访问不少于 20000 次") == "访问不少于 已遮罩 次"
    snapshot = {"columns": ["区域", "访问"], "rows": [["华东", 20000]]}
    view, masked = inquiry_mod._masked_view(snapshot, set(), amounts)
    assert view["rows"] == [["华东", MASKED]] and masked == ["访问"]
    doc = inquiry_mod._compose("访问不少于 20000 次的区域里最少的是 [[v:Q1.r0.区域]]，访问 [[v:Q1.r0.访问]] 次。",
                               {"Q1": query_entry(["区域", "访问"])}, run_id="r1", views={"a1": view},
                               secrets=amounts, allow=[])
    assert doc["markdown"] == "访问不少于 20000 次的区域里最少的是 华东，访问 已遮罩 次。"
    # 一格文字里夹着这个数：按整格比认不出来，渲染出来才露，照旧不交出去
    noted = {"columns": ["备注"], "rows": [["回访 20000 次"]]}
    views = {"a1": inquiry_mod._masked_view(noted, set(), amounts)[0]}
    with pytest.raises(inquiry_mod._Leak):
        inquiry_mod._compose("备注是 [[v:Q1.r0.备注]]。", {"Q1": query_entry(["备注"])}, run_id="r1", views=views,
                             secrets=amounts, allow=[])
    # 引用的列名里写着这个数：列名在快照、SQL 里都照旧换，标记里的列名换得一样，引用照样解析得了
    named = {"columns": ["超过20000次"], "rows": [[3]]}
    shown, _ = inquiry_mod._masked_view(named, set(), amounts)
    assert shown["columns"] == ["超过已遮罩次"]
    doc = inquiry_mod._compose("访问超过 20000 次的有 [[v:Q1.r0.超过20000次]] 笔。", {"Q1": query_entry(named["columns"])},
                               run_id="r1", views={"a1": shown}, secrets=amounts, allow=[])
    assert doc["markdown"] == "访问超过 20000 次的有 3 笔。"


def test_a_comparison_with_a_renamed_masked_column_is_masked_too(people):
    sql = "SELECT * FROM (SELECT buyer_phone AS p FROM sales) t WHERE p = '13900000003' AND 1 = 1"
    assert inquiry_mod._scrub_sql(sql, people, {"buyer_phone", "p"}, "sqlite") == (
        "SELECT * FROM (SELECT buyer_phone AS p FROM sales) t WHERE p = '已遮罩' AND 1 = 1")


def test_sql_that_cannot_be_split_into_words_is_scrubbed_like_text(people):
    assert inquiry_mod._scrub_sql("SELECT 'x FROM t WHERE p = 13900000001 LIMIT 25", people, {"buyer_phone"},
                                  "sqlite") == "SELECT 'x FROM t WHERE p = 已遮罩 LIMIT 25"


# --------------------------------------------------------------------------
# 组装回答的最后一道关
# --------------------------------------------------------------------------


def query_entry(columns: list[str]) -> dict:
    return {"alias": "Q1", "kind": "query", "artifact": "a1", "source": "shop", "columns": columns, "rows": 1,
            "truncated": False}


def test_an_answer_still_showing_a_masked_value_is_not_handed_out():
    """别的列里一格文字夹着遮罩值（备注里写着手机号）：按整格比认不出来，标记里也看不出来，渲染出来才露——宁可不交
    这份回答。"""
    secrets = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    snapshot = {"columns": ["备注"], "rows": [["客户13900000001来电改址"]]}
    views = {"a1": inquiry_mod._masked_view(snapshot, set(), secrets)[0]}
    assert views["a1"]["rows"] == [["客户13900000001来电改址"]]
    catalog = {"Q1": query_entry(["备注"])}
    with pytest.raises(inquiry_mod._Leak):
        inquiry_mod._compose("备注是 [[v:Q1.r0.备注]]。", catalog, run_id="r1", views=views, secrets=secrets, allow=[])
    doc = inquiry_mod._compose("没有遮罩值。", catalog, run_id="r1", views=views, secrets=secrets, allow=[])
    assert doc["markdown"] == "没有遮罩值。"


def test_report_values_that_happen_to_be_masked_values_are_shown():
    """报告口径卡里的指标恰好和遮罩列里的某个值一样（单笔目标 20000，金额也有 20000 的单）：报告里本来就看得到，
    引用它的回答照常交出去。"""
    secrets = secrets_of({"columns": ["amount"], "rows": [[20000], [35000]]}, {"amount"})
    catalog = {"m:target": {"alias": "m:target", "kind": "metric", "eid": "metric:c1:target", "artifact": "c1",
                            "locator": {"metric": "target"}, "value": 20000, "rendered": "20,000",
                            "label": "单笔目标 = 20,000"}}
    with pytest.raises(inquiry_mod._Leak):
        inquiry_mod._compose("单笔目标为 [[m:target]]。", catalog, run_id="r1", views={}, secrets=secrets, allow=[])
    secrets.protect(inquiry_mod._ref_values(inquiry_mod._report_refs(catalog)))
    doc = inquiry_mod._compose("单笔目标为 [[m:target]]，有一笔 35000 元。", catalog, run_id="r1", views={},
                               secrets=secrets, allow=[])
    assert doc["markdown"] == "单笔目标为 20,000，有一笔 已遮罩 元。"


def test_a_column_named_after_a_masked_value_is_renamed():
    """agent 把遮罩值写进了别名（「13900000001 的单」）：快照、回答文档里的列名同样换掉。回答引用了原列名的，标记里
    的列名照样换，正好对上换过的列名，引用照样解析得了。"""
    secrets = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    snapshot = {"columns": ["13900000001 的单", "金额"], "column_types": {"13900000001 的单": "number"},
                "rows": [[2, 1200]]}
    shown, masked = inquiry_mod._masked_view(snapshot, set(), secrets)
    assert shown["columns"] == ["已遮罩 的单", "金额"] and shown["rows"] == [[2, 1200]]
    assert shown["column_types"] == {"已遮罩 的单": "number"} and masked == []
    assert snapshot["columns"][0] == "13900000001 的单", "原快照不动"
    rows_as_dicts = {**snapshot, "rows": [{"13900000001 的单": 2, "金额": 1200}]}
    assert inquiry_mod._masked_view(rows_as_dicts, set(), secrets)[0]["rows"] == [{"已遮罩 的单": 2, "金额": 1200}]

    doc = inquiry_mod._compose("这位客户共 [[v:Q1.r0.13900000001 的单]] 笔。", {"Q1": query_entry(snapshot["columns"])},
                               run_id="r1", views={"a1": shown}, secrets=secrets, allow=[])
    assert doc["markdown"] == "这位客户共 2 笔。"
    assert "13900000001" not in json.dumps(doc, ensure_ascii=False)


# --------------------------------------------------------------------------
# 规模
# --------------------------------------------------------------------------


def test_twenty_thousand_masked_values_are_scrubbed_without_stalling():
    """上万个遮罩值（几份上千行的快照）：以前现编一个上万分支的正则，编一次一秒上下，每次回答、每次取工件都卡住整个
    进程。现在先按词、按开头两个字筛出正文里真有的几个，再换。"""
    rows = [[f"139{i:08d}", f"客户{i:05d}"] for i in range(10_000)]
    secrets = secrets_of({"columns": ["phone", "name"], "rows": rows}, {"phone", "name"})
    text = "近 30 天里客户00042 下了 3 笔，号码 13900000042，" * 60
    started = time.perf_counter()
    for _ in range(5):
        out = secrets.scrub(text)
    elapsed = (time.perf_counter() - started) / 5
    assert "13900000042" not in out and "客户00042" not in out and "近 30 天" in out
    assert elapsed < 0.5, f"换一遍用了 {elapsed:.2f} 秒"
