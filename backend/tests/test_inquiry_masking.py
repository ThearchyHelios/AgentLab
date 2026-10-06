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
- 「夹住某一个值」只在同一个条件里认，按条件合起来的真假认（旁边的别的条件当成不知道真假）：分档的两个 WHEN 分支、
  两个选出列、UNION 两支、WHERE 和 HAVING 各写一边共用的边界是阈值；列名认不出的只和写得一样的表达式配
- 阈值只在同一个数在这条 SQL 别处确实被换掉时才不留（换不换和 SQL 里换字面量是同一套判断）：日期、天数、标签里写着
  同一个短的数，没设遮罩的列上的等值，都不牵连阈值
- 快照按列遮，另按值遮：别的列里写着遮罩值的格子也换成「已遮罩」；列名里写着遮罩值的，列名也换；质询工件里，
  没设遮罩的列里一格文字夹着的遮罩值按子串换（回答一路照旧整份不交），SQL 检查结果的片段照 SQL 换
- 组装回答之后还认得出遮罩值的（正文里复述的阈值除外），不交出去
- 遮罩值上万个时换一遍也不卡：不现编上万个分支的正则；上千个条件连成一长串照样按结构认，成百个分档边界看不过来的
  按夹住算，也不卡

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
    # 和遮罩列做范围比较：边界是阈值，不是在找某个人，照原样。25 两边都有边界（<= 25、NOT BETWEEN 25 …），可这几个
    # 条件合起来谁也找不着（> 31 和 <= 25 不会同时成立），夹不住 25
    ("SELECT COUNT(*) FROM sales WHERE age > 31 AND age <= 25 AND 25 < age AND age NOT BETWEEN 25 AND 31",
     "SELECT COUNT(*) FROM sales WHERE age > 31 AND age <= 25 AND 25 < age AND age NOT BETWEEN 25 AND 31"),
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


@pytest.mark.parametrize("kind, sql, shown", [
    # 同一个条件里还有别的条件（且、或）：照样夹住了
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE region = '华东' AND buyer_phone >= 13900000001 "
     "AND buyer_phone <= 13900000001",
     "SELECT COUNT(*) FROM sales WHERE region = '华东' AND buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE (age >= 25 AND age <= 25) OR region = '华南'",
     "SELECT COUNT(*) FROM sales WHERE (age >= 已遮罩 AND age <= 已遮罩) OR region = '华南'"),
    # CASE WHEN 里的一个条件
    ("sqlite", "SELECT COUNT(CASE WHEN buyer_phone BETWEEN 13900000001 AND 13900000001 THEN 1 END) AS 笔数 FROM sales",
     "SELECT COUNT(CASE WHEN buyer_phone BETWEEN 已遮罩 AND 已遮罩 THEN 1 END) AS 笔数 FROM sales"),
    # 整数的开区间：两头都不是那个值，夹住的是中间那一个，两头一样换（留着 24、26 就等于写出了 25）
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age > 24 AND age < 26",
     "SELECT COUNT(*) FROM sales WHERE age > 已遮罩 AND age < 已遮罩"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age < 26",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age < 已遮罩"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE buyer_phone > 13900000000 AND buyer_phone < 13900000002",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone > 已遮罩 AND buyer_phone < 已遮罩"),
    # 不低于、大于同一个值，二者只成立一个（MySQL 的 XOR）：就是等于它
    ("mysql", "SELECT COUNT(*) FROM sales WHERE age >= 25 XOR age > 25",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 XOR age > 已遮罩"),
])
def test_a_value_pinned_within_one_condition_is_still_a_lookup(phones, kind, sql, shown):
    """「夹住某一个值」只在同一个条件里认（从比较往上只越过且、或、非、括号），同一个条件里夹住的照样换：旁边还有
    别的条件、写在 CASE WHEN 或 HAVING 里、整数的开区间。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, kind) == shown


@pytest.mark.parametrize("sql, shown", [
    ("SELECT region FROM sales GROUP BY region HAVING SUM(amount) BETWEEN 20000 AND 20000",
     "SELECT region FROM sales GROUP BY region HAVING SUM(amount) BETWEEN 已遮罩 AND 已遮罩"),
    ("SELECT region FROM sales GROUP BY region HAVING SUM(amount) >= 20000 AND SUM(amount) <= 20000",
     "SELECT region FROM sales GROUP BY region HAVING SUM(amount) >= 已遮罩 AND SUM(amount) <= 已遮罩"),
])
def test_the_same_aggregate_pinned_from_both_sides_is_a_lookup(amounts, sql, shown):
    """列名认不出的（汇总值、函数）只和写得一样的表达式配：HAVING 里同一个汇总值两头相同，照样是在找它。"""
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == shown


@pytest.mark.parametrize("sql", [
    # 两个 WHEN 分支共用一个边界：各是各的条件，20000 只是分档的界
    "SELECT CASE WHEN amount BETWEEN 0 AND 20000 THEN '小单' WHEN amount BETWEEN 20000 AND 60000 THEN '大单' END "
    "AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1",
    # 两个选出列各按一边数
    "SELECT SUM(CASE WHEN amount <= 20000 THEN 1 ELSE 0 END) AS 小单, "
    "SUM(CASE WHEN amount >= 20000 THEN 1 ELSE 0 END) AS 大单 FROM sales",
    # UNION 的两支
    "SELECT '小单' AS 档位, COUNT(*) AS 笔数 FROM sales WHERE amount <= 20000 "
    "UNION ALL SELECT '大单', COUNT(*) FROM sales WHERE amount >= 20000",
    # 不同子句：WHERE 里一笔的上限、HAVING 里汇总值的下限
    "SELECT region, COUNT(*) AS 笔数 FROM sales WHERE amount <= 20000 GROUP BY region HAVING SUM(amount) >= 20000",
    # 同一个条件里，算式不和金额这一列配（列名认不出的只和写得一样的表达式配）：金额在 15000 到 20000 之间
    "SELECT COUNT(*) FROM sales WHERE amount <= 20000 AND amount + 5000 >= 20000",
    # 边界两边都有比较，可合起来不只认 20000 这一个值：不超过 20000 的，或 20000 以上访问又多的
    "SELECT COUNT(*) FROM sales WHERE amount <= 20000 OR (amount >= 20000 AND visits > 25000)",
])
def test_band_edges_shared_by_separate_conditions_are_thresholds(amounts, sql):
    """第 3 轮收全句所有范围比较一起配对，列名认不出的（SUM(amount)、函数）还和哪一列都配：分档的两个 WHEN 分支、
    两个选出列、UNION 两支各写一边，共用的那个边界就被当成「夹住了某一笔」遮掉，回答里「以 20000 元为界」也跟着遮。
    这些都是阈值：照原样封存，正文里复述也不遮。"""
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == sql
    assert "20000" in inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts)


@pytest.fixture
def ages() -> inquiry_mod._Secrets:
    """年龄设了遮罩，都是两位数：和天数修饰、日期、标签、别的列上的数撞在一起。"""
    return secrets_of({"columns": ["age"], "rows": [[25], [30], [20], [31]]}, {"age"})


@pytest.mark.parametrize("sql, shown", [
    ("SELECT CASE WHEN age BETWEEN 18 AND 30 THEN '青年' WHEN age BETWEEN 30 AND 45 THEN '中年' END AS 年龄段, "
     "COUNT(*) AS 人数 FROM sales GROUP BY 1", {"18", "30", "45"}),
    # 标签里写着同样的数：短的数在字符串里不换，阈值也不跟着遮
    ("SELECT CASE WHEN age BETWEEN 18 AND 30 THEN '18-30岁' WHEN age BETWEEN 30 AND 45 THEN '30-45岁' END AS 年龄段, "
     "COUNT(*) AS 人数 FROM sales GROUP BY 1", {"18", "30", "45"}),
])
def test_overlapping_age_bands_are_thresholds(ages, sql, shown):
    assert inquiry_mod._scrub_sql(sql, ages, {"age"}, "sqlite") == sql
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"age"})], ages) == shown


@pytest.mark.parametrize("sql, shown", [
    ("SELECT COUNT(*) AS 笔数, 13900000001 AS 号码 FROM sales WHERE buyer_phone >= 13900000001",
     "SELECT COUNT(*) AS 笔数, 已遮罩 AS 号码 FROM sales WHERE buyer_phone >= 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone - 13900000001 < 5",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone - 已遮罩 < 5"),
])
def test_a_number_masked_elsewhere_in_the_sql_is_not_kept_as_a_threshold(phones, sql, shown):
    """同一个号码在选出列、算式里被换掉了，条件里的单边范围也不留：留着就把那里换掉的露了。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == shown
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "age"})], phones) <= {"5"}


@pytest.mark.parametrize("sql, shown", [
    # 天数修饰、日期里的数：短的数在字符串里从来不换，日期里的一截也不认
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 30 AND paid_on >= DATE('2026-02-03', '-30 days')", {"30"}),
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age > 31 AND paid_on <= '2026-01-31'", {"31"}),
    # 标签里的数
    ("SELECT CASE WHEN age < 30 THEN '30岁以下' ELSE '30岁及以上' END AS 年龄段, COUNT(*) AS 人数 FROM sales GROUP BY 1",
     {"30"}),
    # 没设遮罩的列上的等值：短的数只在和遮罩列比较时换
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 30 AND amount = 30", {"30"}),
])
def test_a_threshold_stays_when_the_same_number_is_left_alone_elsewhere(ages, sql, shown):
    """第 3 轮把「这条 SQL 别的字面量里写着同一个数」一律当成那里换掉了：日期、天数、标签里的数，没设遮罩的列上的
    等值，那里其实一个字没换，阈值却跟着遮了，回答正文又照样写着它。现在只有别处确实换掉了才不留。"""
    assert inquiry_mod._scrub_sql(sql, ages, {"age"}, "sqlite") == sql
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"age"})], ages) == shown


def test_a_masked_label_that_is_not_a_number_does_not_take_the_threshold_with_it(ages):
    """按年龄分档算出来的标签算派生自遮罩列（已知局限），标签文字本身成了遮罩值，SQL 里照样按值换；换掉的是标签不是
    数，阈值 30 照原样。"""
    ages.add({"columns": ["年龄段"], "rows": [["30岁以下"], ["30岁及以上"]]}, {"年龄段"})
    sql = "SELECT CASE WHEN age < 30 THEN '30岁以下' ELSE '30岁及以上' END AS 年龄段, COUNT(*) AS 人数 FROM sales GROUP BY 1"
    assert inquiry_mod._scrub_sql(sql, ages, {"age", "年龄段"}, "sqlite") == (
        "SELECT CASE WHEN age < 30 THEN '已遮罩' ELSE '已遮罩' END AS 年龄段, COUNT(*) AS 人数 FROM sales GROUP BY 1")
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"age", "年龄段"})], ages) == {"30"}


@pytest.mark.parametrize("sql, sealed", [
    # 找某一个年龄的等值把 30 换掉了：阈值留着就把它露了
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 30 AND age = 30",
     "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 已遮罩 AND age = 已遮罩"),
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age > 31 OR age IN ('31', '99')",
     "SELECT COUNT(*) AS 人数 FROM sales WHERE age > 已遮罩 OR age IN ('已遮罩', '99')"),
])
def test_a_threshold_goes_when_the_same_number_is_masked_elsewhere(ages, sql, sealed):
    assert inquiry_mod._scrub_sql(sql, ages, {"age"}, "sqlite") == sealed
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"age"})], ages) == set()


@pytest.mark.parametrize("sql, sealed", [
    # 批次号里的 20000 是日期一样的写法，按值认不出、没换：阈值照原样
    ("SELECT COUNT(*) FROM sales WHERE amount >= 20000 AND batch = '20000-01'",
     "SELECT COUNT(*) FROM sales WHERE amount >= 20000 AND batch = '20000-01'"),
    # 够长的遮罩值在别的列的等值里也换：换掉了，阈值也不留
    ("SELECT COUNT(*) FROM sales WHERE amount >= 20000 AND visits = 20000",
     "SELECT COUNT(*) FROM sales WHERE amount >= 已遮罩 AND visits = 已遮罩"),
])
def test_long_numbers_go_by_what_was_actually_masked_elsewhere(amounts, sql, sealed):
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == sealed


def test_numbers_shown_across_queries_go_by_what_was_actually_masked(amounts):
    """几条查询之间同理：别的查询里同一个数只写在批次号里、没换，正文里复述阈值照样不遮；别的查询里确实换掉了（和
    金额做等值比较、分词都分不了整条按值换），正文里也遮。"""
    shown = inquiry_mod._shown_literals
    threshold = ("SELECT COUNT(*) FROM sales WHERE amount >= 20000 LIMIT 5", "sqlite", {"amount"})
    assert shown([threshold, ("SELECT COUNT(*) FROM sales WHERE batch = '20000-01'", "sqlite", {"amount"})],
                 amounts) == {"20000", "5"}
    assert shown([threshold, ("SELECT COUNT(*) FROM sales WHERE amount = 20000", "sqlite", {"amount"})],
                 amounts) == {"5"}
    assert shown([threshold, ("SELECT 'x FROM sales WHERE amount = 20000", "sqlite", {"amount"})], amounts) == {"5"}


def test_a_threshold_named_in_an_alias_or_a_comment_stays(amounts):
    """agent 把阈值写进别名、注释（AS 超过30000的笔数）：名字、注释照常按值换，阈值本身照原样，回答正文里复述也不遮。"""
    sql = "SELECT SUM(amount >= 30000) AS 超过30000的笔数 FROM sales -- 30000 以上算大单"
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == (
        "SELECT SUM(amount >= 30000) AS 超过已遮罩的笔数 FROM sales -- 已遮罩 以上算大单")
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts) == {"30000"}


def test_sql_starting_with_blank_lines_is_read_at_the_right_places():
    """agent 写的 SQL 常以换行开头：解析时去掉了开头的空白，按语法树认出的位置要补回来，才对得上原文里的词——阈值、
    行数照原样，找的那个号码换掉。"""
    secrets = secrets_of({"columns": ["buyer_phone", "amount"], "rows": [[13900000001, 30000]]}, {"buyer_phone", "amount"})
    sql = "\n  SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND buyer_phone IN (13900000001, 1) LIMIT 30000"
    assert inquiry_mod._scrub_sql(sql, secrets, {"buyer_phone", "amount"}, "sqlite") == (
        "\n  SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND buyer_phone IN (已遮罩, 1) LIMIT 30000")
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "amount"})], secrets) == {"30000"}


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
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"age", "amount"})], secrets) == set()


def test_only_thresholds_and_row_counts_count_as_shown(amounts):
    """照原样留在 SQL 里的只有范围边界（没夹住某一个值的）和 LIMIT / OFFSET 的行数；选出列、函数参数、CASE、夹住
    某一个值的范围里的数不算，在别处（这一条或别的查询里）被换掉的数也不算。"""
    def shown(*sqls: str) -> set[str]:
        return inquiry_mod._shown_literals([(sql, "sqlite", {"amount"}) for sql in sqls], amounts)

    assert shown("SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND visits > 20000 LIMIT 10000 OFFSET 12000") == {
        "30000", "20000", "10000", "12000"}
    assert shown("SELECT 50000 AS 门槛, SUM(CASE amount WHEN 12000 THEN 1 END), ROUND(amount, 10000) FROM sales "
                 "WHERE amount BETWEEN 20000 AND 20000") == set()
    assert shown("SELECT COUNT(*) FROM sales WHERE amount >= 30000", "SELECT COUNT(*) FROM sales WHERE amount = 30000"
                 ) == set()
    assert shown("SELECT COUNT(*) FROM sales WHERE amount >= 30000 AND note LIKE '%30000%'") == set()


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
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts) == {"30000", "10000"}
    amounts.show(inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts))
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
    amounts.show(inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts))
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


def test_a_masked_value_inside_a_text_cell_is_cut_out_of_the_artifact_view():
    """质询工件这一路（embedded）：没设遮罩的列里一格文字夹着遮罩值，把那一截换成「已遮罩」，这一格别的字、别的格子
    照原样；这一列不记进 masked（界面会把记进去的列整列画成「已遮罩」，这一列在数据源里也没设遮罩）。门槛同回答
    一路：短的数不按值换。回答一路不这样换，渲染出来露了就整份不交（上一条测试）。"""
    secrets = secrets_of({"columns": ["phone", "age"], "rows": [["13900000001", 25]]}, {"phone", "age"})
    snapshot = {"columns": ["区域", "备注", "金额"], "column_types": {"金额": "number"},
                "rows": [["华东", "客户13900000001来电改址", 1200], ["华南", "例行回访，25 岁", 900]]}
    view, masked = inquiry_mod._masked_view(snapshot, set(), secrets, embedded=True)
    assert view["rows"] == [["华东", "客户已遮罩来电改址", 1200], ["华南", "例行回访，25 岁", 900]]
    assert masked == [] and view["column_types"] == {"金额": "number"}
    assert snapshot["rows"][0][1] == "客户13900000001来电改址", "原快照不动"
    as_dicts = {**snapshot, "rows": [{"区域": "华东", "备注": "客户13900000001来电改址", "金额": 1200}]}
    assert inquiry_mod._masked_view(as_dicts, set(), secrets, embedded=True)[0]["rows"] == [
        {"区域": "华东", "备注": "客户已遮罩来电改址", "金额": 1200}]
    # JSON 列（对象）按界面画出来的样子换，换过的照文字给
    nested = {"columns": ["联系人"], "rows": [[{"电话": "13900000001", "年龄": 25}]]}
    assert inquiry_mod._masked_view(nested, set(), secrets, embedded=True)[0]["rows"] == [
        ['{"电话": "已遮罩", "年龄": 25}']]
    # 整格就是遮罩值的照旧整格换、记进 masked；按列遮的照旧
    whole = {"columns": ["联系方式", "备注"], "rows": [["13900000001", "回电 13900000001"]]}
    view, masked = inquiry_mod._masked_view(whole, set(), secrets, embedded=True)
    assert view["rows"] == [[MASKED, "回电 已遮罩"]] and masked == ["联系方式"]
    # 回答一路照旧：文字格不动，交给最后一道关
    assert inquiry_mod._masked_view(snapshot, set(), secrets)[0] is snapshot


def test_sql_check_excerpts_in_the_artifact_are_masked_like_the_sql():
    """质询工件里快照记下的 SQL 检查结果：片段（sql_excerpt）摘自同一条 SQL，照查询清单的 SQL 换掉遮罩值（日期这样的
    字也认得出是在找哪一天）；给模型的那句改法（for_model）照抄了片段，不交出去（同证据面板）。"""
    secrets = secrets_of({"columns": ["birth_date"], "rows": [["1990-05-01"]]}, {"birth_date"})
    message = "「客户」的业务日期按「注册日期」计，查询却按「生日」筛选"
    checks = [{"code": "wrong_date_column", "level": "warning", "table": "customers", "column": "birth_date",
               "message": message, "for_model": "这条 SQL 按 birth_date = '1990-05-01' 筛选",
               "sql_excerpt": "birth_date = '1990-05-01'"},
              {"code": "stock_summed", "level": "info", "table": "balances", "message": "存量跨日期求和",
               "sql_excerpt": "SUM(balance)"}]
    assert inquiry_mod._masked_checks(checks, secrets, {"birth_date"}, "sqlite") == [
        {"code": "wrong_date_column", "level": "warning", "table": "customers", "column": "birth_date",
         "message": message, "sql_excerpt": "birth_date = '已遮罩'"},
        {"code": "stock_summed", "level": "info", "table": "balances", "message": "存量跨日期求和",
         "sql_excerpt": "SUM(balance)"}]


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


def test_a_long_chain_of_conditions_is_still_read_by_its_structure(ages):
    """上千个条件连成一长串（一层套一层的且）：照样按条件合起来看，夹住的值照样换，不因为层数太深退回到按词认——
    按词认不出范围夹住了值，短的数就漏了。"""
    chain = " AND ".join(f"visits{i} >= {i}" for i in range(1500))
    sql = f"SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 25 AND {chain}"
    assert inquiry_mod._literal_uses(sql, "sqlite") is not None
    assert inquiry_mod._scrub_sql(sql, ages, {"age"}, "sqlite") == sql.replace(
        "age >= 25 AND age <= 25", "age >= 已遮罩 AND age <= 已遮罩")


def test_hundreds_of_band_edges_are_read_within_a_budget():
    """成百个分档边界连成一长串：看得过来的照条件合起来认（一百个相邻的分档连成一段，谁也没夹住）；多到看不过来的，
    两边都隔开就按夹住算（宁可多换），也不会一个个细看算上好几秒。年龄从 0 到 599 都有，按夹住算的哪一档都会换。"""
    every = secrets_of({"columns": ["age"], "rows": [[i] for i in range(600)]}, {"age"})

    def bins(n: int) -> str:
        return "SELECT COUNT(*) FROM sales WHERE " + " OR ".join(f"(age >= {i} AND age < {i + 1})" for i in range(n))

    started = time.perf_counter()
    assert inquiry_mod._scrub_sql(bins(100), every, {"age"}, "sqlite") == bins(100)
    assert "已遮罩" in inquiry_mod._scrub_sql(bins(600), every, {"age"}, "sqlite")
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"用了 {elapsed:.2f} 秒"
