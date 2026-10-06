"""审批质询的遮罩（engine/inquiry.py）：回答正文、查询清单、质询工件里都不露出数据源遮罩列的值，也不误伤别的数。

- 改了名字的遮罩列（起别名、套函数、经子查询或 CTE 转一道、UNION 的另一支、和 * 写在同一支里没起别名的表达式）
  照样遮；COUNT 只数个数、窗口的分区键只决定次序，不遮；认不出落在哪一列的（两个 * 之间、子查询里没起别名的表达式）、
  解析不了又写着遮罩列的，整份快照遮掉
- 按值认的遮罩值有门槛：纯数字够长（SECRET_DIGITS 位）、文字至少两个字；日期、小数、千分位里的一截不算；提问人自己
  写进问题、选中原文里的，报告列出的指标和运行输入的值，都不换；SQL 里照原样留着的阈值、行数只在回答正文里不换，
  格子里的同一个数照旧换
- SQL 里数字照原样留着的只有范围比较的边界（没把某一个值夹住的）和 LIMIT / OFFSET 的行数；在找某一个值的（等值、IN、
  LIKE、简单 CASE、IS [NOT] DISTINCT FROM，套了括号、写成行值、VALUES、ARRAY 的也算，还有 BETWEEN v AND v、
  >= v AND <= v 这类夹住某一个值的范围）和遮罩列比较时短的数也换，被比较的一边套了 CAST、汇总也算；同一个数换个
  写法（25.0、025、2.5e1）、写成几个数的算式（25 + 0、13900000000 + 1、125 % 100、50 DIV 2，整数相除照数据源的算法）
  照样认，算式里的数都换（CAST 的类型参数不算）；选出列、函数参数、算式里够长的遮罩值也换；字符串（范围边界也一样）、
  注释、名字按值换；SQL 解析不了时认不准哪个是阈值，一律不免
- 「夹住某一个值」在整条 SQL 里认，只有分档的几种写法各算各的：CASE 的不同分支、不同的选出列（连同 GROUP BY、
  ORDER BY）、UNION 的不同支，共用的边界是阈值；同一行集上的条件（ON 和 WHERE、WHERE 和 HAVING、INTERSECT 两支、
  包在 CASE / 函数 / 算式里的一半、子查询、CTE）照样认得出在找哪一个值。同一个条件里按合起来的真假认（旁边的别的
  条件当成不知道真假），BETWEEN v AND v、>= v AND <= v 这类本身就只认一个值的，条件里哪一层都算；整数上 <= v、
  >= v 也隔开了相邻的整数，两头不是整数的（age > 24.5 AND age < 25.5）也夹住中间那一个整数；范围再用不等、NOT IN
  去掉一头的（age >= 25 AND age <= 26 AND age <> 26）也认，不等、IN 只在同一个条件里和范围合起来看；列名认不出的
  只和写得一样的表达式配
- 阈值只在同一个数在这条 SQL 别处确实被换掉时才不留（换不换和 SQL 里换字面量是同一套判断）：日期、天数、标签里写着
  同一个短的数，没设遮罩的列上的等值，都不牵连阈值；不留的照等值的办法换，别的列上的短阈值、LIMIT 撞上别处换掉的
  短的数照原样（已知局限）
- 按遮罩列分档的标签（CASE、IIF 的结果都是写死的常量）是 agent 自己写在 SQL 里的，不当遮罩值：SQL、回答正文里照原样；
  当分组键的（每一档一行）不算派生值，格子照常显示，逐行的标签列照旧整列遮。只有只装着标签的列不收（按作用域认取自
  哪个来源：经别名、子查询、CTE 传下去、没起别名的也算），改了名字的遮罩列撞上同一条 SQL 里的 THEN 1 ELSE 0 照收，
  UNION 另一支、子查询里的遮罩列流进标签列也照收
- 快照按列遮，另按值遮：别的列里写着遮罩值的格子也换成「已遮罩」；列名里写着遮罩值的，列名也换；质询工件里，
  没设遮罩的列里一格文字夹着的遮罩值按子串换（回答一路照旧整份不交），SQL 检查结果的片段、说明照整条 SQL 换（截断的
  片段从没闭合的引号、注释起去掉，说明里的码值表和别的字一样按值换）
- 组装回答之后还认得出遮罩值的（正文里复述的阈值除外），不交出去
- 遮罩值上万个时换一遍也不卡：不现编上万个分支的正则；上千个条件连成一长串、上千项的连加照样按结构认，成百个分档
  边界看不过来的按夹住算，也不卡

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
    # 汇总里套着结果全是写死的 CASE / IIF：按条件数个数、算占比，同 COUNT
    ("SELECT SUM(CASE WHEN buyer_phone LIKE '139%' THEN 1 ELSE 0 END) AS 笔数 FROM sales", ["笔数"]),
    ("SELECT region, AVG(IIF(buyer_phone LIKE '139%', 1.0, 0)) AS 占比 FROM sales GROUP BY region", ["region", "占比"]),
])
def test_counting_filtering_and_ordering_by_a_masked_column_shows_nothing(sql, columns):
    assert hidden_of(sql, columns) == {"buyer_phone"}


@pytest.mark.parametrize("sql, columns", [
    # 汇总的是遮罩列的值本身（已知局限：SUM / AVG / MIN / MAX 算派生），CASE 里有一支取的是它也一样
    ("SELECT region, MAX(buyer_phone) AS 最大号码 FROM sales GROUP BY region", ["region", "最大号码"]),
    ("SELECT SUM(CASE WHEN region = '华东' THEN buyer_phone END) AS 合计 FROM sales", ["合计"]),
])
def test_aggregating_the_masked_values_themselves_is_still_derived(sql, columns):
    assert hidden_of(sql, columns) >= {"buyer_phone", columns[-1].lower()}


def test_sql_that_cannot_be_read_but_names_a_masked_column_hides_everything():
    assert hidden_of("SELECT buyer_phone AS 号码 FROM sales WHERE (", ["号码", "金额"]) == {"buyer_phone", "号码", "金额"}
    assert hidden_of("SELECT region FROM sales WHERE (", ["region"]) == {"buyer_phone"}


def test_names_that_point_at_a_masked_column_are_known_for_comparisons():
    snapshot = {"source": "shop", "sql": "SELECT p FROM (SELECT buyer_phone AS p FROM sales) t", "columns": ["p"],
                "rows": []}
    assert inquiry_mod._derived_hidden(snapshot, {"buyer_phone"}, "sqlite")[1] == {"buyer_phone", "p"}


BANDS = "CASE WHEN amount BETWEEN 0 AND 20000 THEN '小单' WHEN amount BETWEEN 20000 AND 60000 THEN '大单' END"


@pytest.mark.parametrize("sql", [
    # 按位置、按别名、照原样再写一遍分组
    f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1",
    f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 档位",
    f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY {BANDS}",
    # 档位编号、没写 ELSE、ELSE NULL、IIF 的两个分支也是写死的
    "SELECT CASE WHEN amount <= 20000 THEN 1 WHEN amount <= 60000 THEN 2 ELSE NULL END AS 档位, COUNT(*) AS 笔数 "
    "FROM sales GROUP BY 1",
    "SELECT IIF(amount > 20000, '大单', '小单') AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1",
])
def test_a_band_label_used_as_the_group_key_is_not_a_masked_value(sql):
    """按遮罩列分档、每一档一行（CASE、IIF 的结果全是写死的标签，又是分组键）：标签只说一行落在哪一档，带不出金额，
    档位列不遮，才分得清哪一档是几笔。笔数照样只是计数。"""
    assert hidden_of(sql, ["档位", "笔数"], {"amount"}) == {"amount"}


@pytest.mark.parametrize("sql", [
    # 不分组：每一行的档位就是这一笔落在哪一档
    f"SELECT id, {BANDS} AS 档位, 1 AS 笔数 FROM sales",
    # 结果里有一支取的是金额本身
    "SELECT CASE WHEN amount > 20000 THEN amount ELSE 0 END AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1",
    # 分档写在子查询里，外层再分组：子查询里的档位逐行带出来，照旧算
    f"SELECT 档位, COUNT(*) AS 笔数 FROM (SELECT {BANDS} AS 档位 FROM sales) t GROUP BY 档位",
])
def test_band_labels_row_by_row_still_count_as_derived(sql):
    assert hidden_of(sql, ["档位", "笔数"], {"amount"}) >= {"amount", "档位"}


def plan_of(snapshot: dict, base: set[str], kind: str = "sqlite") -> inquiry_mod._Plan:
    """一份 agent 的查询快照按回答时的办法收遮罩值、遮好（数据源设了遮罩的列是 base）。"""
    return inquiry_mod._settle_plan({"a1": snapshot}, {"a1": (base, [])}, [], {"shop": kind}, [], [], {},
                                    frozenset())


def test_band_labels_written_in_the_sql_are_not_masked_values():
    """分档的标签是 agent 自己写在 SQL 里的字，不是数据源里的值：当分组键的整列照常显示；逐行的标签列照旧整列遮
    （格子里看不出这一笔落在哪一档），但标签文字也不当遮罩值——SQL 里、回答正文里照原样，换掉了封存的就不是实际
    执行的语句，「以 20000 元为界，小单 3 笔」也会变成满屏「已遮罩」。"""
    grouped = {"source": "shop", "sql": f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1 ORDER BY 1",
               "columns": ["档位", "笔数"], "rows": [["大单", 2], ["小单", 3]]}
    plan = plan_of(grouped, {"amount"})
    assert plan.views["a1"]["rows"] == [["大单", 2], ["小单", 3]] and plan.masked["a1"] == []
    assert plan.secrets.scrub("以 20000 元为界，小单 3 笔、大单 2 笔") == "以 20000 元为界，小单 3 笔、大单 2 笔"
    assert inquiry_mod._scrub_sql(grouped["sql"], plan.secrets, plan.tainted["a1"], "sqlite") == grouped["sql"]

    rows = {"source": "shop", "sql": "SELECT id, CASE WHEN age < 30 THEN '30岁以下' ELSE '30岁及以上' END AS 年龄段 "
                                     "FROM sales ORDER BY id",
            "columns": ["id", "年龄段"], "rows": [[1, "30岁以下"], [2, "30岁及以上"]]}
    plan = plan_of(rows, {"age"})
    assert plan.views["a1"]["rows"] == [[1, MASKED], [2, MASKED]] and plan.masked["a1"] == ["年龄段"]
    assert plan.secrets.scrub("30岁以下的有 1 人") == "30岁以下的有 1 人"
    assert inquiry_mod._scrub_sql(rows["sql"], plan.secrets, plan.tainted["a1"], "sqlite") == rows["sql"]
    assert inquiry_mod._shown_literals([(rows["sql"], "sqlite", plan.tainted["a1"])], plan.secrets) == {"30"}


def test_a_value_of_the_masked_column_itself_stays_masked_even_if_written_as_a_label():
    """只有派生列里的标签不收：遮罩列自己选出来的值照收，哪怕 agent 在 SQL 里把同样的字写成了标签。"""
    snapshot = {"source": "shop", "sql": "SELECT level, CASE WHEN level = 'VIP会员' THEN 'VIP会员' ELSE '普通会员' END "
                                         "AS 等级 FROM customers",
                "columns": ["level", "等级"], "rows": [["VIP会员", "VIP会员"], ["铂金会员", "普通会员"]]}
    plan = plan_of(snapshot, {"level"})
    assert plan.views["a1"]["rows"] == [[MASKED, MASKED], [MASKED, MASKED]]
    assert plan.secrets.scrub("VIP会员、铂金会员、普通会员") == "已遮罩、已遮罩、普通会员"


#: 按会员等级数大单：等级设了遮罩、改了名字，汇总里按条件数个数写着 THEN 1 ELSE 0
LEVEL_COUNTS = ("SELECT vip_level AS 等级, SUM(CASE WHEN amount > 1000 THEN 1 ELSE 0 END) AS 大单数 FROM sales "
                "GROUP BY vip_level")


def test_a_renamed_masked_column_keeps_values_that_match_labels_written_elsewhere():
    """第 2 轮复核：分档标签不收进遮罩值，以前对改了名字的遮罩列也一样——vip_level AS 等级 的 1、2、3 撞上同一条 SQL
    里按条件数个数的 THEN 1 ELSE 0，1 就不算遮罩值，之后 WHERE vip_level = 1 照原样封存。现在只跳过只装着标签的
    那几列的格子，改了名字的遮罩列照收。"""
    snapshot = {"source": "shop", "sql": LEVEL_COUNTS, "columns": ["等级", "大单数"],
                "rows": [[1, 2], [2, 0], [3, 0]]}
    plan = plan_of(snapshot, {"vip_level"})
    assert plan.views["a1"]["rows"] == [[MASKED, 2], [MASKED, 0], [MASKED, 0]] and plan.masked["a1"] == ["等级"]
    assert {"1", "2", "3"} <= plan.secrets.weak
    for level in (1, 2, 3):
        assert inquiry_mod._scrub_sql(f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}", plan.secrets,
                                      plan.tainted["a1"], "sqlite") == (
            "SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩")
    # 同一个名字既装着标签又装着遮罩列的值（UNION 两支）：不算只装着标签的列，照收
    mixed = {"source": "shop", "sql": "SELECT vip_level AS 等级 FROM sales UNION ALL "
                                      "SELECT CASE WHEN amount > 1000 THEN 1 ELSE 0 END FROM sales",
             "columns": ["等级"], "rows": [[1], [3], [0]]}
    assert {"0", "1", "3"} <= plan_of(mixed, {"vip_level"}).secrets.weak


@pytest.mark.parametrize("sql, columns, rows", [
    # 标签列经子查询、CTE 原样传下去（外层再分组也一样：每一行的档位照旧算派生值，见上）
    (f"SELECT 档位, COUNT(*) AS 笔数 FROM (SELECT {BANDS} AS 档位 FROM sales) t GROUP BY 档位",
     ["档位", "笔数"], [["大单", 2], ["小单", 3]]),
    (f"WITH t AS (SELECT {BANDS} AS 档位 FROM sales) SELECT * FROM t", ["档位"], [["小单"], ["大单"]]),
    # 没起别名的标签列：按位置认
    (f"SELECT id, {BANDS} FROM sales", ["id", BANDS], [[1, "小单"], [2, "大单"]]),
])
def test_labels_passed_on_under_their_own_name_are_still_not_masked_values(sql, columns, rows):
    """只装着标签的列经别名、子查询、CTE 原样传下去，或者没起别名：照旧整列遮（逐行的档位），标签文字照旧不当遮罩值。"""
    plan = plan_of({"source": "shop", "sql": sql, "columns": columns, "rows": rows}, {"amount"})
    label = columns.index("档位") if "档位" in columns else 1
    assert all(row[label] == MASKED for row in plan.views["a1"]["rows"])
    assert plan.secrets.scrub("小单 3 笔、大单 2 笔") == "小单 3 笔、大单 2 笔"


#: 按条件数大单写的 1、0：和会员等级（1、2、3）撞在一起
BIG_ONE = "CASE WHEN amount > 1000 THEN 1 ELSE 0 END"
#: 标签那一支的 1、0 在前，等级 1、2、3 在后
LEVEL_ROWS = [[1], [0], [0], [1], [1], [2], [3], [1]]


@pytest.mark.parametrize("sql, columns, rows", [
    # UNION 标签在前、遮罩列在后：遮罩列没起别名，或者起了别的名字
    (f"SELECT {BIG_ONE} AS 等级 FROM sales UNION ALL SELECT vip_level FROM sales", ["等级"], LEVEL_ROWS),
    (f"SELECT {BIG_ONE} AS 等级 FROM sales UNION ALL SELECT vip_level AS lv FROM sales", ["等级"], LEVEL_ROWS),
    # 子查询里的 UNION，外层按名字取；写了列名表的 CTE 里的 UNION
    (f"SELECT 等级, COUNT(*) AS 人数 FROM (SELECT {BIG_ONE} AS 等级 FROM sales UNION ALL SELECT vip_level FROM sales) t "
     "GROUP BY 等级", ["等级", "人数"], [[0, 2], [1, 4], [2, 1], [3, 1]]),
    (f"WITH t(lv) AS (SELECT {BIG_ONE} FROM sales UNION ALL SELECT vip_level FROM sales) SELECT lv AS 等级 FROM t",
     ["等级"], LEVEL_ROWS),
    # 标签写在 CTE 里，结果的 UNION 另一支取遮罩列
    (f"WITH b AS (SELECT {BIG_ONE} AS lvl FROM sales) SELECT lvl FROM b UNION ALL SELECT vip_level FROM sales",
     ["lvl"], LEVEL_ROWS),
    # 遮罩列经按原名选出的子查询、以标签的别名传出
    (f"SELECT {BIG_ONE} AS 档 FROM sales UNION ALL SELECT b.vip_level AS 档 FROM (SELECT vip_level FROM sales) b",
     ["档"], LEVEL_ROWS),
    # 不用 UNION：两个子查询连起来，一边是标签，一边是改了名字的遮罩列，名字相同
    (f"SELECT y.档 AS 档 FROM (SELECT id, {BIG_ONE} AS 档 FROM sales) x "
     "JOIN (SELECT id, vip_level AS 档 FROM (SELECT id, vip_level FROM sales) z) y ON x.id = y.id", ["档"],
     [[1], [2], [3], [1]]),
    # USING 连起来的同名列：右连接时，左边没对上的行取的是右边那张表里的值
    (f"WITH b AS (SELECT id, {BIG_ONE} AS vip_level FROM sales) SELECT vip_level AS 等级 FROM b "
     "RIGHT JOIN members USING (vip_level)", ["等级"], [[1], [2], [3]]),
])
def test_a_column_that_also_carries_the_masked_column_is_not_a_label_column(sql, columns, rows):
    """第 3 轮复核：只装着标签的列以前按名字认，从「所有名字都只装着标签」起往下剔，遮罩列原样选出（SELECT vip_level）
    时名字就是它自己，永远剔不掉；结果的 UNION 按位置那一遍只往里加标签，另一支取的是遮罩列也不剔，派生来源里的
    UNION 根本不按位置看。于是标签在前、遮罩列在后时，等级 1 撞上 THEN 1 就不收，之后 WHERE vip_level = 1 照原样
    封存。现在按作用域认每一项取自哪个来源，结果的这一列在每一支里都只装着标签才算，取自数据源里的表的一律不算。"""
    plan = plan_of({"source": "shop", "sql": sql, "columns": columns, "rows": rows}, {"vip_level"})
    assert inquiry_mod._label_columns(sql, "sqlite", columns) == {}
    assert {"1", "2", "3"} <= plan.secrets.weak
    for level in (1, 2, 3):
        assert inquiry_mod._scrub_sql(f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}", plan.secrets,
                                      plan.tainted["a1"], "sqlite") == (
            "SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩")


_EXTRA = "CASE WHEN amount > 60000 THEN '特大单' END"


@pytest.mark.parametrize("sql, columns, texts", [
    # CTE 里分档，外层连上数据源里的表、按档位汇总：档位没写表名也认得出取自 CTE（SQL 能执行，别的表就没有同名的列，
    # 不然会报列名有歧义）
    (f"WITH banded AS (SELECT id, {BANDS} AS 档位 FROM sales) SELECT 档位, SUM(s.visits) AS 访问 FROM banded b "
     "JOIN sales s ON s.id = b.id GROUP BY 档位", ["档位", "访问"], {"小单", "大单"}),
    # 按别的列连起来（USING (id)）：档位没合成一列，照样取自 CTE
    (f"WITH banded AS (SELECT id, {BANDS} AS 档位 FROM sales) SELECT 档位, SUM(o.amount) AS 金额 FROM banded "
     "JOIN orders o USING (id) GROUP BY 档位", ["档位", "金额"], {"小单", "大单"}),
    # 写了表名、经两层子查询传下来
    (f"SELECT t.档位 FROM (SELECT b.档位 FROM (SELECT {BANDS} AS 档位 FROM sales) b) t", ["档位"], {"小单", "大单"}),
    # UNION 两支都是标签（第二支没写 ELSE、没起别名）：两支的标签都算
    (f"SELECT {BANDS} AS 档位 FROM sales UNION ALL SELECT {_EXTRA} FROM sales", ["档位"], {"小单", "大单", "特大单"}),
    # 派生来源里的 UNION 两支都是标签，外层经 * 带出来
    (f"SELECT * FROM (SELECT {BANDS} AS 档位 FROM sales UNION ALL SELECT {_EXTRA} FROM sales) t", ["档位"],
     {"小单", "大单", "特大单"}),
])
def test_label_columns_are_traced_to_where_they_are_written(sql, columns, texts):
    """改成按作用域认之后，写在别处、原样传下来的标签照样认得出：外层连上数据源里的表没写表名、按别的列 USING、写了
    表名经两层子查询；UNION 两支都是标签的，每一支的标签都算（以前派生来源里的 UNION 按名字认，只算起了这个名字的
    第一支）。"""
    assert inquiry_mod._label_columns(sql, "sqlite", columns) == {"档位": texts}


#: 派生来源里排在标签前面、和标签同名的列：SQLite 让同名的几列都留着，外层按名字取的是排在前面的那一列（真实等级）
SHADOWED = [
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT *, {BIG_ONE} AS vip_level FROM sales) t", id="* 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT s.*, {BIG_ONE} AS vip_level FROM sales s) t", id="t.* 在前"),
    pytest.param(f"WITH t AS (SELECT *, {BIG_ONE} AS vip_level FROM sales) SELECT vip_level AS 档 FROM t",
                 id="CTE 里 * 在前"),
    pytest.param(f"SELECT x.vip_level AS 档 FROM (SELECT * FROM sales s JOIN (SELECT id AS bid, {BIG_ONE} AS vip_level "
                 "FROM sales) b ON s.id = b.bid) x", id="JOIN 子查询 * 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT *, b.lbl AS vip_level FROM sales s JOIN (SELECT id AS bid, "
                 f"{BIG_ONE} AS lbl FROM sales) b ON s.id = b.bid) t", id="JOIN 之后 * 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT (vip_level), {BIG_ONE} AS vip_level FROM sales) t",
                 id="(col) 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT ((vip_level)), {BIG_ONE} AS vip_level FROM sales) t",
                 id="((col)) 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT vip_level COLLATE NOCASE, {BIG_ONE} AS vip_level FROM sales) t",
                 id="COLLATE 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT (vip_level) COLLATE BINARY, {BIG_ONE} AS vip_level "
                 "FROM sales) t", id="(col) COLLATE 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT (vip_level), {BIG_ONE} AS vip_level FROM sales UNION ALL "
                 f"SELECT vip_level, {BIG_ONE} FROM sales) t", id="多支的第一支 (col) 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT vip_level COLLATE NOCASE, {BIG_ONE} AS vip_level FROM sales "
                 f"UNION ALL SELECT vip_level, {BIG_ONE} FROM sales) t", id="多支的第一支 COLLATE 在前"),
]


@pytest.mark.parametrize("sql", SHADOWED)
def test_a_masked_column_shadowing_a_label_in_a_derived_source_is_not_a_label_column(sql):
    """第 3 轮复验（J2 留下的缺口）：派生来源里 * 带出的遮罩列、没起别名但套了括号或 COLLATE 的遮罩列排在同名的标签
    前面，SQLite 交回的是真实等级（1、2、3），以前却按标签认、短码值不收，之后 WHERE vip_level = 1 照原样封存。现在
    第一个同名项前面有 * 带出这一列（或说不清带不带）、有去掉括号和 COLLATE 后同名的项，都不算只装着标签的列。"""
    plan = plan_of({"source": "shop", "sql": sql, "columns": ["档"], "rows": [[1], [2], [3]]}, {"vip_level"})
    assert inquiry_mod._label_columns(sql, "sqlite", ["档"]) == {}
    assert {"1", "2", "3"} <= plan.secrets.weak
    for level in (1, 2, 3):
        assert inquiry_mod._scrub_sql(f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}", plan.secrets,
                                      plan.tainted["a1"], "sqlite") == (
            "SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩")


@pytest.mark.parametrize("sql", [
    # 标签排在 * 前面：SQLite 取的是排在前面的标签
    f"SELECT vip_level AS 档 FROM (SELECT {BIG_ONE} AS vip_level, * FROM sales) t",
    # * 只带出另一个派生来源（写了表名）：那里只有标签
    f"SELECT x.vip_level AS 档 FROM (SELECT b.* FROM sales s JOIN (SELECT id AS bid, {BIG_ONE} AS vip_level "
    "FROM sales) b ON s.id = b.bid) x",
    # * 只展开 CTE 里的标签
    f"WITH b AS (SELECT {BIG_ONE} AS vip_level FROM sales) SELECT vip_level AS 档 FROM (SELECT * FROM b) t",
])
def test_a_label_ahead_of_a_star_or_alone_behind_one_is_still_a_label_column(sql):
    """收紧之后，标签排在前面、* 只带出派生来源里的标签的，照旧算只装着标签的列。"""
    assert inquiry_mod._label_columns(sql, "sqlite", ["档"]) == {"档": {"0", "1"}}


#: 没写表名的 * 连着数据源里的表和装着标签的派生来源
STAR_JOIN = ("SELECT * FROM sales s JOIN (SELECT id AS bid, CASE WHEN amount > 1000 THEN '大单' ELSE '小单' END AS 档 "
             "FROM sales) b ON s.id = b.bid")
STAR_COLUMNS = ["id", "region", "amount", "bid", "档"]


def test_a_star_over_a_table_known_to_lack_the_column_keeps_the_label():
    """复核第 1 轮（收紧之后的多遮）：没写表名的 * 展开数据源里的表，以前一律算「说不清带不带这一列」，标签照收成
    遮罩值，回答里的「大单」、SQL 里的 '大单' 都换成「已遮罩」。冻结的表结构里查得到这张表、又没有同名的列时，照旧
    按派生来源认标签；表里有同名的列、查不到表结构的，照旧不算（宁可多遮）。"""
    from app.engine import masking

    lacking = {"tables": {"sales": {"columns": [{"name": "id"}, {"name": "region"}, {"name": "amount"}]}}}
    holding = {"tables": {"sales": {"columns": [{"name": "id"}, {"name": "档"}, {"name": "amount"}]}}}
    assert masking._label_columns(STAR_JOIN, "sqlite", STAR_COLUMNS, masking._schema_tables(lacking)) == {
        "档": {"大单", "小单"}}
    assert masking._label_columns(STAR_JOIN, "sqlite", STAR_COLUMNS, masking._schema_tables(holding)) == {}
    assert masking._label_columns(STAR_JOIN, "sqlite", STAR_COLUMNS) == {}
    truncated = {"tables": {"orders": {"columns": [{"name": "id"}]}}, "truncated": True}
    assert masking._label_columns(STAR_JOIN, "sqlite", STAR_COLUMNS, masking._schema_tables(truncated)) == {}
    # 写了表名的 s.* 展开的是数据源里的表：同样按表结构认它带不带这一列
    nested = ("SELECT x.档 FROM (SELECT s.*, b.* FROM sales s JOIN (SELECT id AS bid, CASE WHEN amount > 1000 "
              "THEN '大单' ELSE '小单' END AS 档 FROM sales) b ON s.id = b.bid) x")
    assert masking._label_columns(nested, "sqlite", ["档"], masking._schema_tables(lacking)) == {"档": {"大单", "小单"}}
    assert masking._label_columns(nested, "sqlite", ["档"], masking._schema_tables(holding)) == {}
    assert masking._label_columns(nested, "sqlite", ["档"]) == {}


async def test_a_snapshot_reads_labels_with_its_frozen_schema():
    """快照记着查询当时冻结的表结构（schema_artifact）：按它认，* 连着的表没有同名的列时标签不当遮罩值，回答正文、
    SQL 里的档名照原样；表结构取不到的照旧不算。"""
    from app.core import artifact_store
    from app.engine import masking

    lacking = {"source": "shop", "tables": {"sales": {"columns": [{"name": "id"}, {"name": "region"},
                                                                  {"name": "amount"}]}}}
    schema = await artifact_store.put_json(lacking, kind="schema_snapshot", run_id="r-labels", node_id="n1")
    snapshot = {"source": "shop", "sql": STAR_JOIN, "columns": STAR_COLUMNS, "schema_artifact": schema,
                "rows": [[1, "华东", 1200, 1, "大单"], [2, "华南", 300, 2, "小单"]]}
    assert masking._snapshot_labels(snapshot, "sqlite") == {"档": {"大单", "小单"}}
    plan = plan_of(snapshot, {"amount"})
    assert plan.secrets.scrub("这一笔属于大单") == "这一笔属于大单"
    sql = "SELECT COUNT(*) FROM (SELECT CASE WHEN amount > 1000 THEN '大单' ELSE '小单' END AS 档 FROM sales) WHERE 档 = '大单'"
    assert inquiry_mod._scrub_sql(sql, plan.secrets, plan.tainted["a1"], "sqlite") == sql
    assert masking._snapshot_labels({**snapshot, "schema_artifact": "missing"}, "sqlite") == {}


def test_the_tool_boundary_reads_labels_with_the_sources_frozen_schema():
    """工具边界按数据源的表结构认标签：* 连着的表没有同名的列，标签不收成遮罩值，同一份结果里别的文字格里的「大单」照常。"""
    from app.engine import masking

    payload = {"sql": STAR_JOIN, "columns": [*STAR_COLUMNS, "备注"],
               "rows": [[1, "华东", 1200, 1, "大单", "大单客户"], [2, "华南", 300, 2, "小单", "例行回访"]]}
    schema = {"tables": {"sales": {"columns": [{"name": "id"}, {"name": "region"}, {"name": "amount"},
                                               {"name": "备注"}]}}}
    done = masking.mask_query_result(payload, ["amount"], "sqlite", schema=schema)
    assert done.columns == ["amount", "档"] and not {"大单", "小单"} & done.secrets.strong
    assert [row[-1] for row in done.payload["rows"]] == ["大单客户", "例行回访"]


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
    # IN 列表里写着遮罩值，换的是它；旁边的范围边界合起来不只认一个值（25、26 都要），照原样
    ("SELECT COUNT(*) FROM sales WHERE age > 24 AND age IN (25, 26)",
     "SELECT COUNT(*) FROM sales WHERE age > 24 AND age IN (已遮罩, 26)"),
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
    # 一头是几个数算出来的（13900000002 - 1 就是 13900000001）：照样夹住了，算式里的数都换（留着哪个都可能算得出号码）
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000001 AND 13900000002 - 1",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩 - 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000001 AND 13900000001 + 0",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩 + 已遮罩"),
])
def test_a_range_that_pins_a_single_value_is_a_lookup(phones, sql, shown):
    """两头是同一个值的范围（BETWEEN v AND v、>= v AND <= v）、大于或小于同一个值、整数的半开区间，都只夹住了一个值，
    是在找它，和等值比较一样换。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == shown


@pytest.mark.parametrize("sql, sealed", [
    # 范围的一头是几个数算出来的：算式就是那个值，夹住了一样是在找它，算式里的数都换
    ("SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 25 + 0",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩 + 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age BETWEEN 20 + 5 AND 30 - 5",
     "SELECT COUNT(*) FROM sales WHERE age BETWEEN 已遮罩 + 已遮罩 AND 已遮罩 - 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age >= 50 / 2 AND age <= (5 * 5)",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 / 已遮罩 AND age <= (已遮罩 * 已遮罩)"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 13900000000 + 1 AND 13900000009 - 8",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 + 已遮罩 AND 已遮罩 - 已遮罩"),
    # 等值、IN 里的值是算出来的：按算出来的值认
    ("SELECT COUNT(*) FROM sales WHERE age = 20 + 5 OR buyer_phone IN (13900000000 + 2, 7)",
     "SELECT COUNT(*) FROM sales WHERE age = 已遮罩 + 已遮罩 OR buyer_phone IN (已遮罩 + 已遮罩, 7)"),
    # 选出列里算出来的号码：同选出列里直接写着号码，够长的遮罩值换
    ("SELECT 13900000000 + 1 AS 号码, COUNT(*) AS 笔数 FROM sales",
     "SELECT 已遮罩 + 已遮罩 AS 号码, COUNT(*) AS 笔数 FROM sales"),
])
def test_a_value_worked_out_from_several_numbers_is_read_as_that_value(phones, sql, sealed):
    """第 2 轮复核：范围的一头写成算式（25 + 0、13900000000 + 1），夹住的值照样认得出，可算式里的数照原样留着，
    一算就是那个号码。算式按算出来的值认：夹住某一个值的、等值、IN 里的，算式里的数都换；正文里也不免遮。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sealed
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "age"})], phones) == set()


def test_a_threshold_worked_out_from_several_numbers_is_kept_like_one_number(amounts):
    """算出来的阈值（20000 * 1.5）和写成一个数的阈值一样：没夹住某一个值就照原样，正文里复述它（连同算出来的
    30000）也不遮；算出来的值在别处确实被换掉了，算式也不留（同写成一个数的阈值）。"""
    sql = "SELECT COUNT(*) FROM sales WHERE amount >= 20000 * 1.5 AND amount < 20000 * 2.5"
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == sql
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"amount"})], amounts) == {
        "20000", "1.5", "2.5", "30000", "50000"}
    swapped = "SELECT COUNT(*) FROM sales WHERE amount >= 15000 + 15000 OR amount = 30000"
    assert inquiry_mod._scrub_sql(swapped, amounts, {"amount"}, "sqlite") == (
        "SELECT COUNT(*) FROM sales WHERE amount >= 已遮罩 + 已遮罩 OR amount = 已遮罩")
    assert inquiry_mod._shown_literals([(swapped, "sqlite", {"amount"})], amounts) == set()


@pytest.mark.parametrize("kind, sql, sealed", [
    # 取模、整除算出来的边界和等值：125 % 100、MOD(125, 100) 是 25，50 DIV 2、51 // 2 也是
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 125 % 100",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩 % 已遮罩"),
    ("postgresql", "SELECT COUNT(*) FROM sales WHERE age = MOD(125, 100)",
     "SELECT COUNT(*) FROM sales WHERE age = MOD(已遮罩, 已遮罩)"),
    ("mysql", "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 50 DIV 2",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩 DIV 已遮罩"),
    ("duckdb", "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 51 // 2",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩 // 已遮罩"),
    # SQLite、Postgres 里两个整数相除只留整数部分：53 / 2 是 26，age < 26 和 age >= 25 夹住 25
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age < 53 / 2",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age < 已遮罩 / 已遮罩"),
    ("postgresql", "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone < 27800000005 / 2",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone < 已遮罩 / 已遮罩"),
])
def test_a_value_worked_out_with_remainders_or_whole_division_is_read_as_that_value(phones, kind, sql, sealed):
    """第 3 轮复核：取模（%、MOD）、整除（MySQL 的 DIV、DuckDB 的 //）写成的算式以前不按算出来的值认，age >= 25 AND
    age <= 125 % 100 照原样封存；整数相除在 SQLite、Postgres 里只留整数部分，以前按小数算（53 / 2 算成 26.5），
    夹住的值也认不出。现在都按数据源算出来的那一个值认，算式里的数都换，正文里也不免遮。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, kind) == sealed
    assert not inquiry_mod._shown_literals([(sql, kind, {"buyer_phone", "age"})], phones) & {"25", "13900000001"}


@pytest.mark.parametrize("kind, divided", [("mysql", "53 / 2"), ("duckdb", "53 / 2"), ("sqlite", "53.0 / 2")])
def test_division_that_keeps_the_fraction_is_read_as_written(phones, kind, divided):
    """MySQL、DuckDB 的 / 照小数算，SQLite 里写成小数的也是：53 / 2、53.0 / 2 是 26.5，age >= 25 AND age < 26.5 里
    25、26 都成立，没夹住，照原样。"""
    sql = f"SELECT COUNT(*) FROM sales WHERE age >= 25 AND age < {divided}"
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, kind) == sql


def test_type_parameters_of_a_cast_bound_are_left_alone(amounts):
    """第 3 轮复核：夹住某一个值的边界写成 CAST(v AS DECIMAL(10, 2)) 时，类型参数 10、2 以前也当成算出边界的数换掉，
    封存的 SQL 成了 DECIMAL(已遮罩, 已遮罩)，SQL 检查结果的片段里别处的 10、2 也跟着换。只换算出边界的那个数；Oracle
    的 CAST … DEFAULT 0 ON CONVERSION ERROR 里的 0 也不算出值，照原样。"""
    sql = ("SELECT COUNT(*) FROM sales WHERE amount >= CAST(30000 AS DECIMAL(10, 2)) "
           "AND amount <= CAST(30000 AS DECIMAL(10, 2))")
    assert inquiry_mod._scrub_sql(sql, amounts, {"amount"}, "sqlite") == sql.replace("30000", MASKED)
    assert inquiry_mod._literal_swaps(sql, amounts, {"amount"}, "sqlite") == {"30000"}
    checks = [{"code": "stock_summed", "level": "warning", "table": "sales", "message": "存量跨日期求和",
               "sql_excerpt": "ROUND(SUM(amount), 2) >= 10"}]
    [check] = inquiry_mod._masked_checks(checks, sql, amounts, {"amount"}, "sqlite")
    assert check["sql_excerpt"] == "ROUND(SUM(amount), 2) >= 10"
    fallback = ("SELECT COUNT(*) FROM sales WHERE amount >= CAST('30000' AS NUMBER DEFAULT 0 ON CONVERSION ERROR) "
                "AND amount <= CAST('30000' AS NUMBER DEFAULT 0 ON CONVERSION ERROR)")
    assert inquiry_mod._scrub_sql(fallback, amounts, {"amount"}, "oracle") == fallback.replace("30000", MASKED)


@pytest.mark.parametrize("sql, sealed", [
    # 遮罩列套了 CAST、汇总：被比较的一边取的还是它的值，短的数照样换
    ("SELECT COUNT(*) FROM sales WHERE CAST(age AS INTEGER) = 25",
     "SELECT COUNT(*) FROM sales WHERE CAST(age AS INTEGER) = 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE CAST(age AS INTEGER) BETWEEN 25 AND 25",
     "SELECT COUNT(*) FROM sales WHERE CAST(age AS INTEGER) BETWEEN 已遮罩 AND 已遮罩"),
    ("SELECT region FROM sales GROUP BY region HAVING MAX(age) BETWEEN 25 AND 25",
     "SELECT region FROM sales GROUP BY region HAVING MAX(age) BETWEEN 已遮罩 AND 已遮罩"),
    # 同一个数换个写法（小数点后补零、前面补零、科学计数法）
    ("SELECT COUNT(*) FROM sales WHERE age = 25.0 OR age = 025 OR age IN (2.5e1)",
     "SELECT COUNT(*) FROM sales WHERE age = 已遮罩 OR age = 已遮罩 OR age IN (已遮罩)"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone = 013900000001 OR buyer_phone = 1.3900000001e10",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone = 已遮罩 OR buyer_phone = 已遮罩"),
    # 两头不是整数的范围：整数上只夹住中间那一个
    ("SELECT COUNT(*) FROM sales WHERE age > 24.5 AND age < 25.5",
     "SELECT COUNT(*) FROM sales WHERE age > 已遮罩 AND age < 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age BETWEEN 25 AND 25.9",
     "SELECT COUNT(*) FROM sales WHERE age BETWEEN 已遮罩 AND 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone > 13900000000.5 AND buyer_phone < 13900000001.5",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone > 已遮罩 AND buyer_phone < 已遮罩"),
    # 范围再用不等、NOT IN 去掉一头：同一个条件里合起来只剩一个值
    ("SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 26 AND age <> 26",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND age <= 26 AND age <> 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE buyer_phone >= 13900000001 AND buyer_phone <= 13900000002 "
     "AND buyer_phone <> 13900000002",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩 AND buyer_phone <> 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age BETWEEN 25 AND 27 AND age NOT IN (26, 27)",
     "SELECT COUNT(*) FROM sales WHERE age BETWEEN 已遮罩 AND 27 AND age NOT IN (已遮罩, 27)"),
])
def test_other_ways_of_looking_up_one_value_are_masked_too(phones, sql, sealed):
    """第 2 轮复核留下的几种找值写法：遮罩列套了 CAST、汇总，同一个数换个写法，整数列上两头不是整数的范围，范围再用
    不等、NOT IN 去掉一头。以前都照原样封存，长号码的那几种还因为照原样留着、被当成阈值，回答正文跟着不遮。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sealed
    shown = inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "age"})], phones)
    assert not shown & {"25", "13900000001"}


@pytest.mark.parametrize("sql", [
    # 范围加上不等、IN，合起来不只认一个值：照原样
    "SELECT COUNT(*) FROM sales WHERE age >= 25 AND age <= 30 AND age <> 26",
    "SELECT COUNT(*) FROM sales WHERE age > 0 AND age IN (26, 27)",
    "SELECT COUNT(*) FROM sales WHERE age >= 25 OR age = 26",
    # 两头不是整数、中间不只一个整数
    "SELECT COUNT(*) FROM sales WHERE age > 24.5 AND age < 26.5",
    # 不等、IN 在别的条件里（选出列的 CASE 和 WHERE）：只有范围比较跨条件配对
    "SELECT SUM(CASE WHEN age = 26 THEN 1 ELSE 0 END) AS 人数 FROM sales WHERE age >= 25",
])
def test_ranges_next_to_other_comparisons_that_do_not_pin_a_value_stay(phones, sql):
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sql


def test_a_long_sum_of_numbers_does_not_stop_the_reading(ages):
    """visits <= 1+1+…（上千项）：以前算式递归求值，超过递归深度，整条 SQL 退回按词认，同一句里夹住的年龄就照原样
    封存。现在不递归，照样按语法树认。"""
    total = "+".join(["1"] * 1500)
    sql = f"SELECT COUNT(*) FROM sales WHERE visits <= {total} AND age >= 25 AND age <= 25"
    assert inquiry_mod._literal_uses(sql, "sqlite") is not None
    started = time.perf_counter()
    assert inquiry_mod._scrub_sql(sql, ages, {"age"}, "sqlite") == sql.replace(
        "age >= 25 AND age <= 25", "age >= 已遮罩 AND age <= 已遮罩")
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"用了 {elapsed:.2f} 秒"


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
    """同一个条件里（从比较往上只越过且、或、非、异或、括号）夹住的，按条件合起来的真假认出来照样换：旁边还有别的
    条件、写在 CASE WHEN 里、整数的开区间、XOR。"""
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
    f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1",
    # 分组时把 CASE 原样再写一遍：选出列和 GROUP BY 是同一个算法的两处写法，都不筛行
    f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY {BANDS}",
    # 两个选出列各按一边数（写成汇总、写成标量子查询都一样）
    "SELECT SUM(CASE WHEN amount <= 20000 THEN 1 ELSE 0 END) AS 小单, "
    "SUM(CASE WHEN amount >= 20000 THEN 1 ELSE 0 END) AS 大单 FROM sales",
    "SELECT (SELECT COUNT(*) FROM sales WHERE amount <= 20000) AS 小单, "
    "(SELECT COUNT(*) FROM sales WHERE amount >= 20000) AS 大单",
    # UNION 的两支
    "SELECT '小单' AS 档位, COUNT(*) AS 笔数 FROM sales WHERE amount <= 20000 "
    "UNION ALL SELECT '大单', COUNT(*) FROM sales WHERE amount >= 20000",
    # IIF 的两个分支：华东按一边数，别的区域按另一边数
    "SELECT SUM(IIF(region = '华东', amount <= 20000, amount >= 20000)) AS 笔数 FROM sales",
    # WHERE 里一笔的上限、HAVING 里汇总值的下限：比的不是同一个东西（列名认不出的只和写得一样的表达式配）
    "SELECT region, COUNT(*) AS 笔数 FROM sales WHERE amount <= 20000 GROUP BY region HAVING SUM(amount) >= 20000",
    # 同一个条件里，算式不和金额这一列配：金额在 15000 到 20000 之间
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


_PHONE, _NEXT = "13900000001", "13900000002"


@pytest.mark.parametrize("kind, sql", [
    # 内连接的 ON 和 WHERE 筛的是同一批行：各写一边照样夹住
    ("sqlite", "SELECT o.region FROM sales o JOIN buyers b ON b.id = o.bid AND b.buyer_phone >= {v} "
               "WHERE b.buyer_phone <= {v}"),
    # WHERE 和 HAVING 比的是同一列（分组的键）
    ("sqlite", "SELECT buyer_phone, COUNT(*) FROM sales WHERE buyer_phone >= {v} GROUP BY buyer_phone "
               "HAVING buyer_phone <= {v}"),
    # INTERSECT 两支都要成立；EXCEPT 是前一支成立、后一支不成立（不低于 25 又不高于 25）
    ("sqlite", "SELECT id FROM sales WHERE age >= {a} INTERSECT SELECT id FROM sales WHERE age <= {a}"),
    ("sqlite", "SELECT id FROM sales WHERE age >= {a} EXCEPT SELECT id FROM sales WHERE age > {a}"),
    # 一半包在 CASE、布尔比较、函数、算式里
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= {a} AND CASE WHEN age <= {a} THEN 1 ELSE 0 END = 1"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= {a} AND (age <= {a}) = TRUE"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= {a} AND COALESCE(age <= {a}, 0)"),
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE IIF(age >= {a}, 1, 0) + IIF(age <= {a}, 1, 0) = 2"),
    # 嵌套的 CASE：里层只在外层成立时才算
    ("sqlite", "SELECT SUM(CASE WHEN age >= {a} THEN CASE WHEN age <= {a} THEN 1 END END) AS 人数 FROM sales"),
    # 选出列里的 FILTER 数的是 WHERE 筛过的行
    ("duckdb", "SELECT COUNT(*) FILTER (WHERE age >= {a}) AS 人数 FROM sales WHERE age <= {a}"),
    # 两个汇总值相减：不低于 25 的减去高于 25 的，就是正好 25 的
    ("sqlite", "SELECT SUM(age >= {a}) - SUM(age > {a}) AS 人数 FROM sales"),
    # 拆进子查询、CTE
    ("sqlite", "SELECT COUNT(*) FROM sales WHERE age >= {a} AND id IN (SELECT id FROM sales WHERE age <= {a})"),
    ("sqlite", "WITH a AS (SELECT * FROM sales WHERE buyer_phone >= {v}) SELECT COUNT(*) FROM a "
               "WHERE buyer_phone <= {v}"),
])
def test_a_lookup_split_across_clauses_is_still_a_lookup(phones, kind, sql):
    """把找某一个值的两半拆到同一行集的不同地方（ON 和 WHERE、WHERE 和 HAVING、INTERSECT 两支、包在 CASE 或函数里、
    子查询、CTE）：第 4 轮只在同一个条件里认，两半各自当阈值照原样留着，长的号码随即被当成「摆在读者眼前」，回答
    正文里也不遮，最后一道关跟着放行。只有分档的几种写法（CASE 的不同分支、不同的选出列、UNION 的不同支）各算各的，
    别的都按同一行集看：两半照样换掉，正文里也不免遮。"""
    written = sql.format(v=_PHONE, a=25)
    assert inquiry_mod._scrub_sql(written, phones, {"buyer_phone", "age"}, kind) == sql.format(v=MASKED, a=MASKED)
    assert inquiry_mod._shown_literals([(written, kind, {"buyer_phone", "age"})], phones) == set()


@pytest.mark.parametrize("sql, sealed", [
    # 整数上 age <= 24 和 age < 25 一样，从下面把 25 隔开；age >= 26 和 age > 25 一样，从上面隔开
    ("SELECT COUNT(*) FROM sales WHERE NOT (age <= 24 OR age >= 26)",
     "SELECT COUNT(*) FROM sales WHERE NOT (age <= 已遮罩 OR age >= 已遮罩)"),
    ("SELECT COUNT(*) FROM sales WHERE age >= 25 AND NOT age >= 26",
     "SELECT COUNT(*) FROM sales WHERE age >= 已遮罩 AND NOT age >= 已遮罩"),
    (f"SELECT COUNT(*) FROM sales WHERE buyer_phone >= {_PHONE} AND NOT buyer_phone >= {_NEXT}",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND NOT buyer_phone >= 已遮罩"),
    # 就是 age <> 25
    ("SELECT COUNT(*) FROM sales WHERE age <= 24 OR age >= 26",
     "SELECT COUNT(*) FROM sales WHERE age <= 已遮罩 OR age >= 已遮罩"),
])
def test_neighbouring_whole_numbers_pin_the_value_between_them(phones, sql, sealed):
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sealed
    assert inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "age"})], phones) == set()


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM sales WHERE age >= 18 AND age <= 30",
    "SELECT CASE WHEN age <= 24 THEN '24岁及以下' WHEN age >= 26 THEN '26岁及以上' END AS 年龄段, COUNT(*) AS 人数 "
    "FROM sales GROUP BY 1",
    "SELECT COUNT(*) FROM sales WHERE age BETWEEN 18 AND 24",
])
def test_ordinary_ranges_are_not_pinned_by_neighbouring_whole_numbers(phones, sql):
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sql


@pytest.mark.parametrize("sql, sealed", [
    # 被更宽的范围吸收了，条件合起来不只认这一个号码，可 BETWEEN v AND v 本身就写出了它
    (f"SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN {_PHONE} AND {_PHONE} OR buyer_phone > 13000000000",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone BETWEEN 已遮罩 AND 已遮罩 OR buyer_phone > 13000000000"),
    # 旁边还有矛盾的条件（谁也查不着）：两半紧挨着、隔着别的条件写都一样
    (f"SELECT COUNT(*) FROM sales WHERE buyer_phone >= {_PHONE} AND buyer_phone <= {_PHONE} "
     "AND buyer_phone > 13900000005",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩 AND buyer_phone > 13900000005"),
    (f"SELECT COUNT(*) FROM sales WHERE buyer_phone >= {_PHONE} AND buyer_phone > 13900000005 "
     f"AND buyer_phone <= {_PHONE}",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone >= 已遮罩 AND buyer_phone > 13900000005 AND buyer_phone <= 已遮罩"),
    # 大于或小于同一个值（就是不等于），旁边又或上一个更宽的范围
    (f"SELECT COUNT(*) FROM sales WHERE buyer_phone > {_PHONE} OR buyer_phone > 13000000000 OR buyer_phone < {_PHONE}",
     "SELECT COUNT(*) FROM sales WHERE buyer_phone > 已遮罩 OR buyer_phone > 13000000000 OR buyer_phone < 已遮罩"),
])
def test_a_range_that_names_one_value_is_a_lookup_whatever_surrounds_it(phones, sql, sealed):
    """BETWEEN v AND v、>= v AND <= v、> v OR < v 不论整数都只认一个值：条件里哪一层这样写都算在找它，旁边的条件把它
    吸收了、和它矛盾了也一样（规格要求这几种必须继续遮）。整数上的半开、开区间只看整个条件，和相邻的分档连成一段的
    不算（见下面成百个分档的那条）。"""
    assert inquiry_mod._scrub_sql(sql, phones, {"buyer_phone", "age"}, "sqlite") == sealed
    assert _PHONE not in inquiry_mod._shown_literals([(sql, "sqlite", {"buyer_phone", "age"})], phones)


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
    """年龄段的两个 WHEN 分支共用边界 30：同上，是分档的界。"""
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
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age = 30 AND visits >= 30",
     "SELECT COUNT(*) AS 人数 FROM sales WHERE age = 已遮罩 AND visits >= 30"),
    ("SELECT region FROM sales WHERE age IN (30, 31) AND visits > 30 LIMIT 31",
     "SELECT region FROM sales WHERE age IN (已遮罩, 已遮罩) AND visits > 30 LIMIT 31"),
    # 开区间夹住的 25 两头都换了，别的列上的 25 照原样
    ("SELECT COUNT(*) AS 人数 FROM sales WHERE age > 24 AND age < 26 AND visits >= 25",
     "SELECT COUNT(*) AS 人数 FROM sales WHERE age > 已遮罩 AND age < 已遮罩 AND visits >= 25"),
])
def test_a_short_number_masked_elsewhere_leaves_other_columns_and_limits_alone(ages, sql, sealed):
    """同一个短的数在别处换掉了，别的列上的阈值、LIMIT 撞上它照原样（已知局限）：短的数本来就不按值遮，撞上只是巧合，
    一律换会把阈值、行数改得面目全非（第 2 轮起「age = 25 AND amount > 25 … LIMIT 25」就照原样留着阈值和行数）。
    正文里不免遮——反正短的数正文里也不按值遮。够长的数在别的列上照样换（visits = 20000 那条）。"""
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
    sql = "SELECT SUM(balance) FROM customers c JOIN balances b ON b.cid = c.id WHERE birth_date = '1990-05-01'"
    message = "「客户」的业务日期按「注册日期」计，查询却按「生日」筛选"
    checks = [{"code": "wrong_date_column", "level": "warning", "table": "customers", "column": "birth_date",
               "message": message, "for_model": "这条 SQL 按 birth_date = '1990-05-01' 筛选",
               "sql_excerpt": "birth_date = '1990-05-01'"},
              {"code": "stock_summed", "level": "info", "table": "balances", "message": "存量跨日期求和",
               "sql_excerpt": "SUM(balance)"}]
    assert inquiry_mod._masked_checks(checks, sql, secrets, {"birth_date"}, "sqlite") == [
        {"code": "wrong_date_column", "level": "warning", "table": "customers", "column": "birth_date",
         "message": message, "sql_excerpt": "birth_date = '已遮罩'"},
        {"code": "stock_summed", "level": "info", "table": "balances", "message": "存量跨日期求和",
         "sql_excerpt": "SUM(balance)"}]


def test_check_excerpts_follow_the_sql_recorded_at_answer_time():
    """agent 自己的查询快照查询时就遮好了，事后取质询工件时收不到遮罩列里的值：片段、说明照事件里记下的那条 SQL（回答时
    按内存里的遮罩值换过）换——那条里整个换成「已遮罩」的字面量、换掉的那一截（LIKE 的 %…%），片段、说明里一样换。"""
    sql = ("SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '13900000001' AND age = 25 "
           "AND note LIKE '%13900000002%'")
    recorded = "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '已遮罩' AND age = 已遮罩 AND note LIKE '%已遮罩%'"
    assert inquiry_mod._recorded_swaps(sql, recorded, "sqlite") == ({"13900000001", "25"}, {"13900000002"})
    assert inquiry_mod._recorded_swaps(sql, sql, "sqlite") == (set(), set())
    assert inquiry_mod._recorded_swaps(sql, recorded + " LIMIT 5", "sqlite") == (set(), set()), "对不上就不认"
    checks = [{"code": "c1", "level": "warning", "table": "sales", "message": "没有码值「25」",
               "sql_excerpt": "buyer_phone = '13900000001' AND age = 25"},
              {"code": "c2", "level": "info", "table": "sales", "message": "按备注筛选",
               "sql_excerpt": "note LIKE '%13900000002%'"}]
    whole, pieces = inquiry_mod._recorded_swaps(sql, recorded, "sqlite")
    assert inquiry_mod._masked_checks(checks, sql, inquiry_mod._Secrets(), set(), "sqlite", swapped=whole,
                                      pieces=pieces) == [
        {"code": "c1", "level": "warning", "table": "sales", "message": "没有码值「已遮罩」",
         "sql_excerpt": "buyer_phone = '已遮罩' AND age = 已遮罩"},
        {"code": "c2", "level": "info", "table": "sales", "message": "按备注筛选", "sql_excerpt": "note LIKE '%已遮罩%'"}]


@pytest.mark.parametrize("sql, excerpt, shown", [
    # 片段只是一段表达式，单独拿出来认不出在找哪一个值：按整条 SQL 里换掉的字面量换
    ("SELECT SUM(CASE age WHEN 25 THEN balance END) AS 余额 FROM accounts",
     "SUM(CASE age WHEN 25 THEN balance END)", "SUM(CASE age WHEN 已遮罩 THEN balance END)"),
    ("SELECT SUM(balance) AS 余额 FROM accounts WHERE (age, region) = (25, '华东')",
     "(age, region) = (25, '华东')", "(age, region) = (已遮罩, '华东')"),
    ("SELECT SUM(CASE WHEN age BETWEEN 25 AND 25 THEN balance END) AS 余额 FROM accounts",
     "SUM(CASE WHEN age BETWEEN 25 AND 25 THEN balance END)",
     "SUM(CASE WHEN age BETWEEN 已遮罩 AND 已遮罩 THEN balance END)"),
    # 整条 SQL 里照原样留着的阈值，片段里也照原样
    ("SELECT SUM(CASE WHEN amount >= 20000 THEN balance END) AS 余额 FROM accounts",
     "SUM(CASE WHEN amount >= 20000 THEN balance END)", "SUM(CASE WHEN amount >= 20000 THEN balance END)"),
    # 检查器截过的片段：截在号码中间，那半截按值认不出，去掉
    ("SELECT SUM(balance) AS 余额 FROM accounts WHERE region = '华东' AND phone = '13900000001'",
     "region = '华东' AND phone = '1390000…", "region = '华东' AND phone = …"),
])
def test_sql_check_excerpts_follow_what_the_whole_sql_masked(sql, excerpt, shown):
    secrets = secrets_of({"columns": ["phone", "age", "amount"], "rows": [["13900000001", 25, 20000]]},
                         {"phone", "age", "amount"})
    checks = [{"code": "stock_summed", "level": "warning", "table": "accounts", "message": "存量跨日期求和",
               "sql_excerpt": excerpt}]
    [check] = inquiry_mod._masked_checks(checks, sql, secrets, {"phone", "age", "amount"}, "sqlite")
    assert check["sql_excerpt"] == shown


def test_sql_check_messages_do_not_quote_masked_values():
    """检查结果的说明照引 SQL 里写的取值（码值表已列全时「没有码值「3」」）：整条 SQL 里换掉的取值，说明里一样换成
    「已遮罩」，短的码值、文字码值都一样；码值表里列出的码值是数据目录写明的，照原样。"""
    secrets = secrets_of({"columns": ["status", "channel_type"], "rows": [[3, "线下合作"], [1, "线上"]]},
                         {"status", "channel_type"})
    sql = ("SELECT COUNT(*) FROM visits v JOIN channels c ON c.id = v.cid WHERE status IN (3, -3) "
           "AND channel_type = '线下合作'")
    listed = "（数据目录中已列出的全部码值：1=有效、0=作废）。请核对筛选条件里的取值"
    checks = [{"code": "unknown_code", "level": "warning", "table": "visits", "column": "status",
               "message": f"「入园记录」的「检票状态」没有码值「3」、「-3」{listed}", "sql_excerpt": "status IN (3, -3)"},
              {"code": "unknown_code", "level": "warning", "table": "channels", "column": "channel_type",
               "message": f"「渠道」的「渠道类型」没有码值「线下合作」{listed}",
               "sql_excerpt": "channel_type = '线下合作'"}]
    out = inquiry_mod._masked_checks(checks, sql, secrets, {"status", "channel_type"}, "sqlite")
    assert [c["message"] for c in out] == [f"「入园记录」的「检票状态」没有码值「已遮罩」、「-已遮罩」{listed}",
                                           f"「渠道」的「渠道类型」没有码值「已遮罩」{listed}"]
    assert [c["sql_excerpt"] for c in out] == ["status IN (已遮罩, -已遮罩)", "channel_type = '已遮罩'"]


def test_codes_listed_in_a_check_message_are_masked_by_value_like_any_text():
    """说明里码值表列出的码值是数据目录写明的，不从 SQL 摘，也不另外免遮：和说明里别的字一样按值换，恰好是按值认得出
    的遮罩值（两个字以上的文字、够长的数）的码值换成「已遮罩」（多遮，不漏），短的码值、别的码值照原样。"""
    secrets = secrets_of({"columns": ["channel_type", "status"], "rows": [["线上直销", 1], ["分销", 0]]},
                         {"channel_type", "status"})
    message = ("「渠道」的「渠道类型」没有码值「线下合作」（数据目录中已列出的全部码值：线上直销=官网和小程序、"
               "线下=窗口售票、分销=旅行社、1=有效）。请核对筛选条件里的取值")
    checks = [{"code": "unknown_code", "level": "warning", "table": "channels", "column": "channel_type",
               "message": message, "sql_excerpt": "channel_type = '线下合作'"}]
    [check] = inquiry_mod._masked_checks(checks, "SELECT COUNT(*) FROM channels WHERE channel_type = '线下合作'",
                                         secrets, {"channel_type", "status"}, "sqlite")
    assert check["message"] == ("「渠道」的「渠道类型」没有码值「线下合作」（数据目录中已列出的全部码值：已遮罩=官网和小程序、"
                                "线下=窗口售票、已遮罩=旅行社、1=有效）。请核对筛选条件里的取值")


@pytest.mark.parametrize("excerpt, shown", [
    # 截在带空格的地址中间：从没闭合的引号起整个去掉，不只去掉最后一段
    ("region = '华东' AND addr = '上海市 浦东新区 某…", "region = '华东' AND addr = …"),
    # 写成两个单引号的引号不算闭合
    ("region = 'O''Neil 的店' AND addr = '上海市 浦东…", "region = 'O''Neil 的店' AND addr = …"),
    # 截在带引号的名字、注释中间也一样
    ('region = \'华东\' AND "上海市 浦东新区…', "region = '华东' AND …"),
    ("region = '华东' /* 上海市 浦东…", "region = '华东' …"),
])
def test_a_cut_off_excerpt_drops_the_whole_unfinished_value(excerpt, shown):
    """检查器截过的片段停在一个带空格的遮罩文字值中间（地址）：以前只去掉「…」前最后一段连续的字，前面那几段还在；
    现在从没闭合的引号、注释起整个去掉。"""
    secrets = secrets_of({"columns": ["addr"], "rows": [["上海市 浦东新区 某某路 1 号"]]}, {"addr"})
    sql = "SELECT SUM(balance) AS 余额 FROM accounts WHERE region = '华东' AND addr = '上海市 浦东新区 某某路 1 号'"
    checks = [{"code": "stock_summed", "level": "warning", "table": "accounts", "message": "存量跨日期求和",
               "sql_excerpt": excerpt}]
    [check] = inquiry_mod._masked_checks(checks, sql, secrets, {"addr"}, "sqlite")
    assert check["sql_excerpt"] == shown


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


# --------------------------------------------------------------------------
# 回答时才认得出的值：记下位置，事后取质询工件时照着再换一遍
# --------------------------------------------------------------------------


def test_values_known_only_at_answer_time_are_recorded_as_positions_and_replayed():
    """agent 先查了备注（那时还不知道备注里的号码是遮罩值，查询时原样交回、原样存下），后来又查出了手机号（查询时
    遮掉，值只在内存里）。回答时按内存里的值换掉的地方记成位置（不含值）；事后取质询工件时内存里的值已经不在，照着
    位置再换一遍，和回答时看到的一样。"""
    from app.engine import masking

    note = "客户13900000001来电改址，备用 139-0000-0001"
    snapshot = {"columns": ["区域", "备注", "号码", "13900000001 的单"],
                "rows": [["华东", note, "13900000001", 1], ["华南", "例行回访", "13800000000", 2]]}
    seen = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    edits = masking.view_edits(snapshot, set(), seen, inquiry_mod._Secrets())
    first, second = note.index("13900000001"), note.index("139-0000-0001")
    assert edits == {"cells": [[0, 2]], "spans": [[0, 1, first, first + 11], [0, 1, second, second + 13]],
                     "names": [[3, 0, 11]], "masked": ["号码"]}
    assert "1390000000" not in json.dumps(edits, ensure_ascii=False), "只记位置，不记值"
    replayed = masking.replay_edits(snapshot, [edits])
    assert replayed["columns"] == ["区域", "备注", "号码", f"{MASKED} 的单"]
    assert replayed["rows"] == [["华东", f"客户{MASKED}来电改址，备用 {MASKED}", MASKED, 1],
                                ["华南", "例行回访", "13800000000", 2]]
    # 不靠内存里的值也认得出的（报告快照里就有），事后照样认得出，不记
    assert masking.view_edits(snapshot, set(), seen, seen) is None
    # 记下的位置对不上（越界、坏形状）的跳过，不出错
    broken = {"cells": [[9, 9], "x"], "spans": [[0, 1, 50, 60], [0, 9, 0, 1]], "names": [[7, 0, 1]], "masked": [3]}
    assert masking.replay_edits(snapshot, [broken, None, "x"]) == snapshot


def test_the_plan_records_what_only_the_values_seen_by_the_agent_masked():
    """_settle_plan 带上 agent 查询时遮掉的值（seen）时，另记下只靠它们才换掉的位置（replay）；不带 seen 的（事后取
    质询工件）不记。"""
    notes = {"source": "shop", "sql": "SELECT region AS 区域, note AS 备注 FROM callbacks", "columns": ["区域", "备注"],
             "rows": [["华东", "客户13900000001来电改址"], ["华南", "例行回访"]]}
    seen = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    plan = inquiry_mod._settle_plan({"a1": notes}, {"a1": ({"buyer_phone"}, [])}, [], {"shop": "sqlite"}, [], [], {},
                                    frozenset(), seen)
    assert plan.replay == {"a1": {"cells": [], "spans": [[0, 1, 2, 13]], "names": [], "masked": []}}
    later = inquiry_mod._settle_plan({"a1": notes}, {"a1": ({"buyer_phone"}, [])}, [], {"shop": "sqlite"}, [], [], {},
                                     frozenset({"a1"}))
    assert later.replay == {} and later.views["a1"]["rows"][0][1] == "客户13900000001来电改址"


def test_values_the_agent_saw_survive_a_report_that_fills_the_cap():
    """报告快照里的遮罩值收满了上限（SECRETS_MAX）：agent 查询时遮掉的值先收，照样认得出（以前先收报告的，满了就丢）。"""
    report = {"columns": ["buyer_phone"], "rows": [[str(13800000000 + i)] for i in range(inquiry_mod.SECRETS_MAX)]}
    seen = secrets_of({"columns": ["phone"], "rows": [["19914742785"]]}, {"phone"})
    plan = inquiry_mod._settle_plan({}, {}, [(report, {"buyer_phone"})], {}, [], [], {}, frozenset(), seen)
    assert plan.secrets.hit("19914742785")
