"""双盲复算的选择写进数据目录（engine/dual_check_facts.py）。

- 只差一个方面、又对得上目录事实时才写：两边按不同的时间列筛 → 业务日期，一边多一个排除无效记录的条件 → 有效记录
  条件，聚合只差去不去重、被采纳的一边去重 → 去重规则；按被采纳那一边的写法记
- 业务日期只在两边的列都在表结构里声明为带日期的类型（DATE、DATETIME、TIMESTAMP，带精度、带时区的写法也算）时才写：
  文本、整数（Unix 秒、20260901 这类日期键）、只有时刻的 TIME、表结构里查不到、没声明类型（SQLite 探出来是 NULL）的
  不写，原因说清是哪一列、哪一边（未被采纳的那一边另说清为什么也要看它）；上传表格建的数据源，按文字保存的列看导入时
  配方里认作日期的列（候选的 upload_dates）：是的算日期列，不是的、读不出配方的说清日期是按文字保存的；数据源还没有
  可用的表结构时说表结构不可用
- 差了不止一处、有兜底说明、两条 SQL 规范化后一致、差在别的方面、被采纳的一边没有可记的写法、只有一边按时间筛：不写，
  照实说原因。同一列上的时间范围不同、跨两张表的条件，差异说明只给兜底，同样不写；说明以后真把跨两张表的条件说成一条
  筛选条件，derive 自己那道「只涉及一张表」的检查也拦得下
- 指标自己的筛选范围（只看华东、只看线上）不是有效记录条件：取值出现在指标名称、口径说明、所在分组、运行输入里的，
  条件用到分组的列的，不是在排除退款、作废、测试这类无效记录的，都不写
- 写的是人工填写、已确认，备注写明运行、节点、指标；同样的说法已确认过就不再写（不升版本），确认过的不同说法不覆盖；
  目录里已有的有效记录条件被采纳的一边也用着的，合起来写（已有的段只写一次），没用着的不动；驳回过的说法不写回
  （!= 和 <>、列名大小写、先后不同也认得出）
- 同一次裁决里几个指标落到同一项上：说法一样的写一次；有效记录条件（都在排除无效记录）用 AND 合成一句写；业务日期
  说法不同的都不写；自己的说法驳回过的指标拿出来不写、其余照写（和各自的预告一致），合起来又成了驳回的整句也不写
- 审批卡上的预告（preview）：选 a、选 b 各会写什么、不写的为什么，按目录里已有的说法算，和真写同一套判断

差异条目一律由 sql_semantics.explain_sql_diff 真算出来，和节点交给审核人看的是同一份。表名、数字都是虚构的。
"""
from __future__ import annotations

import pytest

from app.data import catalog
from app.data.sql_semantics import explain_sql_diff
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine.dual_check_facts import derive, preview, record_choices
from tests.fixtures.dual_check_shop import (
    BOTH_WRONG,
    BUYERS,
    BUYERS_DISTINCT,
    BY_ORDER_DATE,
    RIGHT,
    WEEK,
    WITH_REFUNDS,
    add_source,
    build,
)
from tests.fixtures.sources import drop_source

SCHEMA = {"tables": {
    "orders": {"name": "orders", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "region", "type": "TEXT"},
        {"name": "customer_id", "type": "INTEGER"}, {"name": "amount", "type": "REAL"},
        {"name": "status", "type": "TEXT"}, {"name": "order_date", "type": "DATE"},
        {"name": "paid_date", "type": "DATE"}, {"name": "channel", "type": "TEXT"},
        {"name": "is_test", "type": "INTEGER"}], "primary_key": ["id"]},
    "customers": {"name": "customers", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "level", "type": "TEXT"}, {"name": "status", "type": "TEXT"}],
        "primary_key": ["id"]},
}}
#: 不带时间条件、只排除退款：和 RIGHT 只差「按不按时间筛」
NO_TIME = "SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded'"
COUNT_WEEK = f"SELECT COUNT(*) AS n FROM orders WHERE paid_date {WEEK}"
SUM_WEEK = f"SELECT SUM(amount) AS gmv FROM orders WHERE paid_date {WEEK}"


def candidate(original: str, blind: str, choice: str, *, metric: str = "销售额", source: str = "shop",
              scope: dict | None = None) -> dict:
    diff = explain_sql_diff(original, blind, "sqlite", schema=SCHEMA, labels=("原流程", "盲算")).as_dict()
    out = {"metric": metric, "choice": choice, "source": source, "dialect": "sqlite",
           "sql": {"a": original, "b": blind}, "diff": diff}
    if scope is not None:
        out["scope"] = scope
    return out


def extra(condition: str, base: str = SUM_WEEK) -> str:
    """base 多一个条件。"""
    return base.replace(" WHERE ", f" WHERE {condition} AND ", 1)


# --------------------------------------------------------------------------
# 差异 → 目录事实
# --------------------------------------------------------------------------


@pytest.mark.parametrize("choice, column", [("b", "paid_date"), ("a", "order_date")])
def test_a_different_time_column_becomes_the_business_date(choice, column):
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, choice), SCHEMA)
    assert why is None
    assert (fact.table, fact.item, fact.value, fact.choice) == ("orders", "business_date", {"column": column}, choice)


def test_a_time_filter_on_one_side_only_is_not_a_business_date():
    """一边按支付日筛、另一边根本没按时间筛：审核人判断的是要不要限定时间，没在两列之间选过。"""
    picked = candidate(RIGHT, NO_TIME, "a")
    assert [i["aspect"] for i in picked["diff"]["items"]] == ["time"]
    fact, why = derive(picked, SCHEMA)
    assert fact is None and "只有被采纳的原流程按时间筛选" in why and "不是按哪一列归属" in why
    fact, why = derive(candidate(RIGHT, NO_TIME, "b"), SCHEMA)
    assert fact is None and "被采纳的盲算没有按时间筛选" in why


def test_an_extra_filter_on_the_adopted_side_becomes_the_valid_filter():
    fact, why = derive(candidate(WITH_REFUNDS, RIGHT, "b"), SCHEMA)
    assert why is None and (fact.item, fact.value) == ("valid_filter", "status <> 'refunded'")
    # 原流程多出这个条件、审核人采纳原流程：同样记这个条件
    fact, why = derive(candidate(RIGHT, WITH_REFUNDS, "a"), SCHEMA)
    assert why is None and (fact.item, fact.value) == ("valid_filter", "status <> 'refunded'")
    # 被采纳那一边的写法跟着记下来，有效记录条件合并时要看它还用了哪些条件
    assert fact.sql == RIGHT and fact.dialect == "sqlite"


def test_the_adopted_side_without_the_extra_filter_has_nothing_to_record():
    fact, why = derive(candidate(RIGHT, WITH_REFUNDS, "b"), SCHEMA)
    assert fact is None and "被采纳的盲算没有这个条件" in why and "销售额" in why


@pytest.mark.parametrize("original, blind, choice, metric", [
    # 原流程按指标名只看华东，盲算漏了：审核人自然选原流程
    (extra("region = '华东'", RIGHT), RIGHT, "a", "华东销售额"),
    # 盲算按指标名只数线上的单
    (COUNT_WEEK, extra("channel = 'online'", COUNT_WEEK), "b", "线上订单数"),
    # 只保留一部分取值的写法，状态列上也一样
    (COUNT_WEEK, extra("status IN ('paid')", COUNT_WEEK), "b", "已支付订单数"),
    (SUM_WEEK, extra("(region = '华东' OR region = '华南')"), "b", "南方销售额"),
    # 排除的写法，排除的却不是无效记录
    (COUNT_WEEK, extra("channel <> 'offline'", COUNT_WEEK), "b", "线上订单数"),
    (SUM_WEEK, extra("region <> '华东'"), "b", "非华东销售额"),
    (SUM_WEEK, extra("status <> 'paid'"), "b", "未支付订单金额"),
    (SUM_WEEK, extra("customer_id IS NOT NULL"), "b", "会员销售额"),
])
def test_a_metric_scope_filter_is_not_a_valid_filter(original, blind, choice, metric):
    picked = candidate(original, blind, choice, metric=metric)
    assert [i["aspect"] for i in picked["diff"]["items"]] == ["filter"]
    fact, why = derive(picked, SCHEMA)
    assert fact is None and "可能是指标自己的筛选范围" in why and metric in why


def test_the_metric_scope_is_read_from_the_names_groups_and_inputs():
    """排除的写法、排除的也像无效记录，但取值出现在口径说明、所在分组、运行输入里，或者用的是分组的列：拿不准，不写。"""
    blind = extra("channel <> 'test'")
    assert derive(candidate(SUM_WEEK, blind, "b"), SCHEMA)[0] is not None
    for scope in ({"texts": ["只看正式渠道：不含 test 渠道的订单"]}, {"texts": ["channel = test"]},
                  {"columns": ["channel"]}):
        fact, why = derive(candidate(SUM_WEEK, blind, "b", scope=scope), SCHEMA)
        assert fact is None and "可能是指标自己的筛选范围" in why, scope


@pytest.mark.parametrize("condition, written", [
    ("is_test = 0", "is_test = 0"),
    ("is_test <> 1", "is_test <> 1"),
    ("status NOT IN ('refunded', 'void')", "NOT status IN ('refunded', 'void')"),
    ("status <> '已作废'", "status <> '已作废'"),
])
def test_conditions_that_drop_invalid_rows_are_valid_filters(condition, written):
    fact, why = derive(candidate(SUM_WEEK, extra(condition), "b"), SCHEMA)
    assert why is None and (fact.item, fact.value) == ("valid_filter", written)


@pytest.mark.parametrize("condition", ["is_test = 1", "is_test <> 0", "status <> 9"])
def test_conditions_that_keep_invalid_rows_or_unknown_codes_are_not(condition):
    fact, why = derive(candidate(SUM_WEEK, extra(condition), "b"), SCHEMA)
    assert fact is None and "可能是指标自己的筛选范围" in why


def test_a_code_the_catalog_calls_void_counts_as_an_invalid_row():
    notes = {"orders": {"columns": {"status": {"codes": catalog.make_item({"9": "作废", "1": "已支付"}, "human")}}}}
    fact, why = derive(candidate(SUM_WEEK, extra("status <> 9"), "b"), SCHEMA, notes)
    assert why is None and fact.value == "status <> 9"
    # 码值表里写的是正常的状态：排除它就是指标自己的范围
    fact, why = derive(candidate(SUM_WEEK, extra("status <> 1"), "b"), SCHEMA, notes)
    assert fact is None and "可能是指标自己的筛选范围" in why


def test_table_names_are_dropped_from_the_recorded_filter():
    qualified = RIGHT.replace("FROM orders WHERE status", "FROM orders o WHERE o.status") \
        .replace("paid_date", "o.paid_date")
    fact, why = derive(candidate(WITH_REFUNDS, qualified, "b"), SCHEMA)
    assert why is None and fact.value == "status <> 'refunded'"


#: 订单关联客户；盲算另多一个比较两张表的列的条件
JOINED = (f"SELECT SUM(o.amount) AS gmv FROM orders o JOIN customers c ON o.customer_id = c.id "
          f"WHERE o.paid_date {WEEK}")


def test_a_condition_across_two_tables_is_not_recorded():
    """几张表交错的条件，差异说明只给兜底（归纳不成具体差异）：审核人选的是整体哪边对，不写。"""
    picked = candidate(JOINED, f"{JOINED} AND o.status <> c.status", "b")
    assert picked["diff"]["fallback"] is True and [i["aspect"] for i in picked["diff"]["items"]] == ["other"]
    fact, why = derive(picked, SCHEMA)
    assert fact is None and "未能归纳" in why and "销售额" in why


def test_a_filter_spanning_two_tables_is_not_a_valid_filter():
    """差异说明真把跨两张表的条件说成一条筛选条件（以后说明放宽了、或者别的来源交来的说明）：derive 自己也拦得下——
    有效记录条件记在一张表名下，用到两张表的列的写进哪张都不对。差异说明直接构造：从 SQL 走现在只有兜底。"""
    picked = candidate(JOINED, f"{JOINED} AND o.status <> c.status", "b")
    picked["diff"] = {"equivalent": False, "fallback": False, "items": [{
        "aspect": "filter", "a": None, "b": "o.status <> c.status",
        "text": "盲算只保留 o.status <> c.status 的行，原流程没有这个条件",
        "detail": {"columns": [{"table": "customers", "column": "status"}, {"table": "orders", "column": "status"}]},
    }]}
    fact, why = derive(picked, SCHEMA)
    assert fact is None and "不是只涉及一张表的列" in why and "销售额" in why
    # 同样的条件只涉及一张表时这道检查放行（后面照常看是不是在排除无效记录）：拦下它的确实是「两张表」
    picked["diff"]["items"][0]["detail"] = {"columns": [{"table": "orders", "column": "status"}]}
    assert "不是只涉及一张表的列" not in (derive(picked, SCHEMA)[1] or "")


def test_counting_distinct_becomes_the_dedup_rule():
    fact, why = derive(candidate(BUYERS, BUYERS_DISTINCT, "b", metric="付款客户数"), SCHEMA)
    assert why is None and (fact.table, fact.item, fact.value) == ("orders", "dedup", "对 customer_id 计数时去重")


def test_counting_without_distinct_is_not_a_dedup_rule():
    """采纳了不去重的一边：那是这个指标自己的算法（数行），写成表的「不去重」会把别的指标带偏。"""
    fact, why = derive(candidate(BUYERS, BUYERS_DISTINCT, "a", metric="付款笔数"), SCHEMA)
    assert fact is None and "被采纳的原流程没有去重" in why


def test_more_than_one_difference_is_not_recorded():
    fact, why = derive(candidate(BOTH_WRONG, RIGHT, "b"), SCHEMA)
    assert fact is None and "不止差一个方面" in why


def test_equivalent_sql_or_fallback_or_other_aspects_are_not_recorded():
    same = candidate(RIGHT, RIGHT.replace("SUM(amount) AS gmv", "SUM(amount) AS total"), "b")
    assert same["diff"]["equivalent"] is True
    assert "规范化后一致" in derive(same, SCHEMA)[1]

    fallback = candidate(BY_ORDER_DATE, RIGHT, "b")
    fallback["diff"]["fallback"] = True
    assert "未能归纳" in derive(fallback, SCHEMA)[1]

    joined = candidate(RIGHT, RIGHT.replace("SUM(amount)", "MAX(amount)"), "b")
    assert [i["aspect"] for i in joined["diff"]["items"]] == ["aggregate"]
    assert "不只是去不去重" in derive(joined, SCHEMA)[1]

    # 同一列上的时间范围不同：差异说明只给兜底（说不清差的是哪一段），不写
    ranged = candidate(RIGHT, RIGHT.replace("'2026-09-07'", "'2026-09-06'"), "b")
    assert ranged["diff"]["fallback"] is True and [i["aspect"] for i in ranged["diff"]["items"]] == ["other"]
    assert "未能归纳" in derive(ranged, SCHEMA)[1]

    # 只差一个方面、可那个方面对不上目录里的三项（关联方式）：不写，原因说清差在哪
    left = (f"SELECT SUM(o.amount) AS gmv FROM orders o LEFT JOIN customers c ON c.id = o.customer_id "
            f"WHERE o.paid_date {WEEK}")
    joins = candidate(left, left.replace("LEFT JOIN", "JOIN"), "b")
    assert joins["diff"]["fallback"] is False and [i["aspect"] for i in joins["diff"]["items"]] == ["join"]
    fact, why = derive(joins, SCHEMA)
    assert fact is None and "两边差在关联" in why and "对不上数据目录里的业务日期、有效记录条件或去重规则" in why

    assert "没有采用任何一边" in derive({**candidate(BY_ORDER_DATE, RIGHT, "b"), "choice": "neither"}, SCHEMA)[1]


def test_a_table_missing_from_the_schema_is_not_recorded():
    """认出了事实（排除退款的有效记录条件），表却不在数据源的表结构里：不写。"""
    fact, why = derive(candidate(WITH_REFUNDS, RIGHT, "b"), {"tables": {"refunds": {"columns": []}}})
    assert fact is None and "涉及的表「orders」不在数据源的表结构里" in why


def schema_with(**types: str | None) -> dict:
    """SCHEMA 的订单表里几列换成给定的声明类型；给 None 的列从表结构里拿掉。"""
    orders = SCHEMA["tables"]["orders"]
    columns = [{**c, "type": types[c["name"]]} if c["name"] in types else c
               for c in orders["columns"] if not (c["name"] in types and types[c["name"]] is None)]
    return {"tables": {**SCHEMA["tables"], "orders": {**orders, "columns": columns}}}


@pytest.mark.parametrize("order_date, paid_date", [
    ("DATE", "DATE"),
    ("TIMESTAMP", "TIMESTAMP"),
    ("datetime", "DateTime(6)"),
    ("TIMESTAMPTZ", "timestamp(3) with time zone"),
    ("TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP(6) WITH LOCAL TIME ZONE"),
])
def test_a_business_date_is_written_when_both_columns_are_declared_as_dates(order_date, paid_date):
    """两边的列都在表结构里声明为带日期的类型（日期、日期时间、时间戳，带精度、带时区、大小写不同都认）：照常记被采纳的
    那一列。"""
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "b"), schema_with(order_date=order_date, paid_date=paid_date))
    assert why is None and (fact.item, fact.value) == ("business_date", {"column": "paid_date"})


#: 未被采纳的一边的列有问题时，说清为什么也要看它
BOTH_DATES = "两边都是日期列时，审核人选的才是业务日期按哪一列"


@pytest.mark.parametrize("types, column, said", [
    # SQLite 常把日期存成文字：认不准存的是不是日期
    ({"paid_date": "TEXT"}, "orders.paid_date", "在表结构里的类型是 TEXT，无法确认存的是日期"),
    ({"paid_date": "VARCHAR(10)"}, "orders.paid_date", "在表结构里的类型是 VARCHAR(10)，无法确认存的是日期"),
    # 没被采纳的一边也得是：它要是按 Unix 秒、20260901 这类整数筛的，审核人并没有在两个日期列之间选
    ({"order_date": "INTEGER"}, "orders.order_date", "在表结构里的类型是 INTEGER，无法确认存的是日期"),
    ({"order_date": "BIGINT"}, "orders.order_date", "在表结构里的类型是 BIGINT，无法确认存的是日期"),
    # 名字里带着 date、time 的别的类型也不算
    ({"paid_date": "DATE_KEY"}, "orders.paid_date", "在表结构里的类型是 DATE_KEY，无法确认存的是日期"),
    ({"paid_date": "INTERVAL"}, "orders.paid_date", "在表结构里的类型是 INTERVAL，无法确认存的是日期"),
    # 只有时刻的 TIME：一天里的几点几分（MySQL 里还常拿它存时长）不是日期，和 catalog.is_date_column 同一个口径
    ({"paid_date": "TIME"}, "orders.paid_date", "在表结构里的类型是 TIME，只有时刻，不含日期"),
    ({"order_date": "time with time zone"}, "orders.order_date",
     "在表结构里的类型是 time with time zone，只有时刻，不含日期"),
    ({"paid_date": "TIMETZ"}, "orders.paid_date", "在表结构里的类型是 TIMETZ，只有时刻，不含日期"),
    ({"paid_date": "TIME(3)"}, "orders.paid_date", "在表结构里的类型是 TIME(3)，只有时刻，不含日期"),
    ({"paid_date": ""}, "orders.paid_date", "在表结构里没有声明类型，无法确认存的是日期"),
    # SQLite 里没写类型的列，SQLAlchemy 探出来是 NullType，表结构里记的是「NULL」：同样是没有声明类型
    ({"paid_date": "NULL"}, "orders.paid_date", "在表结构里没有声明类型，无法确认存的是日期"),
    ({"paid_date": None}, "orders.paid_date", "不在数据源的表结构里，无法确认存的是日期"),
])
def test_a_business_date_is_not_written_unless_both_columns_are_dates(types, column, said):
    """任一边的列不是带日期的声明类型（文本、整数、只有时刻、查不到、没声明类型）：不写，原因说清是哪一列。被采纳的那一边
    有问题说它；被采纳的一边没问题，就说未被采纳的那一边，另说清为什么也要看它。order_date 是原流程（a）的列，
    paid_date 是盲算（b）的。"""
    owner = "a" if column == "orders.order_date" else "b"
    label = {"a": "原流程", "b": "盲算"}[owner]
    for choice in ("a", "b"):
        fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, choice), schema_with(**types))
        assert fact is None, (types, choice)
        if choice == owner:
            assert why == f"指标「销售额」被采纳的{label}按时间筛的列「{column}」{said}，不写入数据目录", why
        else:
            assert why == f"指标「销售额」未被采纳的{label}按时间筛的列「{column}」{said}：{BOTH_DATES}，不写入数据目录", why


@pytest.mark.parametrize("schema", [None, {}, {"tables": {}}, {"tables": {}, "failed": True, "error": "timeout"}])
def test_a_business_date_is_not_written_without_the_table_structure(schema):
    """数据源还没有可用的表结构（还没探查、探查失败）：两列的类型无从确认，不写。原因说表结构不可用，不说这一列不在表结构
    里——那时并不知道有没有这一列。"""
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "b"), schema)
    assert fact is None
    assert why == ("指标「销售额」所在数据源还没有可用的表结构（尚未探查或探查失败），无法确认两边按时间筛的列存的是日期，"
                   "不写入数据目录"), why


def test_a_table_missing_from_an_available_table_structure_is_said_to_be_missing():
    """表结构是有的，里面只是没有这张表：照实说这一列不在表结构里。"""
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "b"), {"tables": {"customers": SCHEMA["tables"]["customers"]}})
    assert fact is None
    assert why == "指标「销售额」被采纳的盲算按时间筛的列「orders.paid_date」不在数据源的表结构里，无法确认存的是日期，不写入数据目录"


def test_the_type_check_follows_the_names_in_the_table_structure():
    """差异说明里的表名、列名是规范化过的（小写）：表结构里写成大写的照样认得出，类型照它声明的看。"""
    upper = {"tables": {"ORDERS": {"name": "ORDERS", "columns": [
        {"name": "STATUS", "type": "TEXT"}, {"name": "ORDER_DATE", "type": "DATE"}, {"name": "PAID_DATE", "type": "TEXT"},
        {"name": "AMOUNT", "type": "REAL"}]}}}
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "a"), upper)
    assert fact is None and "「orders.paid_date」在表结构里的类型是 TEXT" in why


def test_the_preview_follows_the_type_check():
    """审批卡上的预告和真写同一套判断：一边的列是文本，选哪一边都预告不写（认不出事实，不带写到哪），原因同 derive。"""
    schema = schema_with(paid_date="TEXT")
    said = preview(candidate(BY_ORDER_DATE, RIGHT, "a"), schema, {})
    for side in ("a", "b"):
        assert said[side] == {"write": False, "reason": derive(candidate(BY_ORDER_DATE, RIGHT, side), schema)[1]}
        assert "「orders.paid_date」在表结构里的类型是 TEXT" in said[side]["reason"]


def test_an_uploaded_table_says_its_dates_are_stored_as_text():
    """上传表格建的数据源（候选带 uploaded）：导入时日期一律按文字保存，表结构里的类型总是 TEXT。照样不写（认不准存的是
    不是日期），可原因不说「类型是 TEXT」——那不是用户声明的，导入确认时这一列标的还是「日期」，表结构也改不了——说清
    是上传的表格按文字保存日期。整数列照常说类型；不是上传的数据源，同样的文本列也照常说类型。"""
    texts = schema_with(order_date="TEXT", paid_date="TEXT")
    for choice, label, column in (("a", "原流程", "order_date"), ("b", "盲算", "paid_date")):
        fact, why = derive({**candidate(BY_ORDER_DATE, RIGHT, choice), "uploaded": True}, texts)
        assert fact is None and why == (f"指标「销售额」被采纳的{label}按时间筛的列「orders.{column}」来自上传的表格，"
                                        "表格里的日期一律按文字保存，无法确认存的是日期，不写入数据目录"), why
    fact, why = derive({**candidate(BY_ORDER_DATE, RIGHT, "b"), "uploaded": True}, schema_with(order_date="INTEGER"))
    assert fact is None and "未被采纳的原流程按时间筛的列「orders.order_date」在表结构里的类型是 INTEGER" in why
    assert "被采纳的盲算按时间筛的列「orders.paid_date」在表结构里的类型是 TEXT" in derive(
        candidate(BY_ORDER_DATE, RIGHT, "b"), texts)[1]


#: 上传表格的列按文字保存、配方里又没认作日期时的原因（同上一个用例）
STORED_AS_TEXT = "来自上传的表格，表格里的日期一律按文字保存，无法确认存的是日期"


def uploaded(original: str, blind: str, choice: str, dates: dict | None) -> dict:
    """上传表格建的数据源的候选：upload_dates 是节点从当前版本的配方读出的、导入时认作日期的列（{表: [列]}）。"""
    return {**candidate(original, blind, choice), "uploaded": True,
            **({"upload_dates": dates} if dates is not None else {})}


def test_an_uploaded_column_the_import_took_as_a_date_is_a_date():
    """上传表格建的数据源，表结构里的日期列总是 TEXT（导入时一律按文字保存），可导入时配方里这两列的类型是「日期」（候选的
    upload_dates）：照常记成业务日期，采纳哪一边记哪一列，预告是同一套判断。"""
    texts = schema_with(order_date="TEXT", paid_date="TEXT")
    both = {"orders": ["order_date", "paid_date"]}
    for choice, column in (("a", "order_date"), ("b", "paid_date")):
        fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, choice, both), texts)
        assert why is None, why
        assert (fact.table, fact.item, fact.value, fact.choice) == \
            ("orders", "business_date", {"column": column}, choice)
        said = preview(uploaded(BY_ORDER_DATE, RIGHT, choice, both), texts, {})
        assert said[choice] == {"write": True, "source": "shop", "table": "orders", "item": "business_date",
                                "value": {"column": column}}


@pytest.mark.parametrize("dates, column, owner", [
    # 配方读不出来（简单导入没有配方、配方记录坏了）：两列都照旧说按文字保存
    (None, "orders.paid_date", "b"),
    ({}, "orders.paid_date", "b"),
    # 配方里只有一列是日期，另一列是文字：没认作日期的那一列照旧说
    ({"orders": ["paid_date"]}, "orders.order_date", "a"),
    ({"orders": ["order_date"]}, "orders.paid_date", "b"),
    # 认作日期的是别的表的同名列：对不上这一列
    ({"customers": ["order_date", "paid_date"]}, "orders.paid_date", "b"),
])
def test_an_uploaded_column_the_import_did_not_take_as_a_date_keeps_the_reason(dates, column, owner):
    """配方里不是日期、或者读不出配方的列：照现在的原因不写（说按文字保存，不说「类型是 TEXT」），被采纳的一边先说；
    被采纳的一边是日期、未被采纳的不是的，说未被采纳的那一边、另说清为什么也要看它。"""
    texts = schema_with(order_date="TEXT", paid_date="TEXT")
    fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, "b", dates), texts)
    assert fact is None
    if owner == "b":
        assert why == f"指标「销售额」被采纳的盲算按时间筛的列「{column}」{STORED_AS_TEXT}，不写入数据目录", why
    else:
        assert why == (f"指标「销售额」未被采纳的原流程按时间筛的列「{column}」{STORED_AS_TEXT}：{BOTH_DATES}，"
                       "不写入数据目录"), why
    assert preview(uploaded(BY_ORDER_DATE, RIGHT, "b", dates), texts, {})["b"] == {"write": False, "reason": why}


def test_the_import_dates_only_speak_for_text_columns_of_an_uploaded_source():
    """upload_dates 只回答「按文字保存的这一列，导入时是不是日期」：
    - 不是上传源（手工登记的库）的文本列照旧说类型，候选里带着 upload_dates 也不认
    - 上传源里声明成别的类型的列（整数）照旧说类型；只有时刻的仍不算日期"""
    texts = schema_with(order_date="TEXT", paid_date="TEXT")
    both = {"orders": ["order_date", "paid_date"]}
    fact, why = derive({**candidate(BY_ORDER_DATE, RIGHT, "b"), "upload_dates": both}, texts)
    assert fact is None and "「orders.paid_date」在表结构里的类型是 TEXT，无法确认存的是日期" in why
    fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, "b", both), schema_with(order_date="INTEGER", paid_date="TEXT"))
    assert fact is None and "未被采纳的原流程按时间筛的列「orders.order_date」在表结构里的类型是 INTEGER" in why
    fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, "b", both), schema_with(order_date="TEXT", paid_date="TIME"))
    assert fact is None and "「orders.paid_date」在表结构里的类型是 TIME，只有时刻，不含日期" in why


def test_the_import_dates_follow_the_names_in_the_table_structure():
    """差异说明里的名字是规范化过的（不加引号的一律小写），配方里、表结构里的写法可能带大写：按表结构认出的那一列去对
    upload_dates，大小写不同照样对得上。"""
    upper = {"tables": {"Orders": {"name": "Orders", "columns": [
        {"name": "Status", "type": "TEXT"}, {"name": "Order_Date", "type": "TEXT"},
        {"name": "Paid_Date", "type": "TEXT"}, {"name": "Amount", "type": "REAL"}]}}}
    fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, "b", {"Orders": ["Order_Date", "Paid_Date"]}), upper)
    assert why is None and (fact.table, fact.value) == ("Orders", {"column": "paid_date"})
    fact, why = derive(uploaded(BY_ORDER_DATE, RIGHT, "b", {"orders": ["order_date", "paid_date"]}), upper)
    assert why is None and (fact.table, fact.value) == ("Orders", {"column": "paid_date"})


def test_the_joint_preview_reads_the_import_dates_too():
    """几个指标落到同一张上传表的业务日期上：preview_joint 和单个指标的预告、真写一样从候选认 upload_dates。"""
    from app.engine.dual_check_facts import preview_joint

    texts = schema_with(order_date="TEXT", paid_date="TEXT")
    both = {"orders": ["order_date", "paid_date"]}
    picked = [{**uploaded(BY_ORDER_DATE, RIGHT, "a", both), "metric": "销售额", "metric_id": "gmv"},
              {**uploaded(BY_ORDER_DATE, RIGHT, "a", both), "metric": "订单金额", "metric_id": "total"}]
    by = {tuple((m["metric_id"], m["choice"]) for m in e["members"]): e
          for e in preview_joint(picked, {"shop": (texts, {})})}
    assert by[(("gmv", "b"), ("total", "b"))]["outcomes"] == {
        "gmv": {"write": True, "value": {"column": "paid_date"}},
        "total": {"write": True, "value": {"column": "paid_date"}}}
    # 配方读不出来的同一组候选：一项都认不出，没有可预告的组合
    plain = [{**c, "upload_dates": {}} for c in picked]
    assert preview_joint(plain, {"shop": (texts, {})}) == []


async def test_an_undeclared_column_read_from_a_real_database_is_said_to_have_no_type(tmp_path):
    """真探查一个 SQLite 库（introspect 存的是 str(列的类型)）：没写类型的列记成「NULL」（SQLAlchemy 的 NullType），
    说成没有声明类型，不说「类型是 NULL」。"""
    name, source_id = await add_source(build(tmp_path / "untyped.db", date_type=""))
    try:
        async with SessionLocal() as session:
            schema = (await session.get(DataSource, source_id)).schema_cache
        assert {c["name"]: c["type"] for c in schema["tables"]["orders"]["columns"]}["paid_date"] == "NULL"
        fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "b"), schema)
        assert fact is None and why == ("指标「销售额」被采纳的盲算按时间筛的列「orders.paid_date」在表结构里没有声明类型，"
                                        "无法确认存的是日期，不写入数据目录"), why
    finally:
        await drop_source(source_id)


# --------------------------------------------------------------------------
# 写进目录
# --------------------------------------------------------------------------


@pytest.fixture
async def shop(tmp_path):
    name, source_id = await add_source(build(tmp_path / "shop.db"))
    yield name, source_id
    await drop_source(source_id)


async def entry_of(source_id: str):
    async with SessionLocal() as session:
        return await catalog.read_entry(session, source_id, "orders")


async def seed(source_id: str, notes: dict) -> None:
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "orders", notes, if_version=0, actor="数据组")


async def seed_again(source_id: str, notes: dict) -> None:
    """把目录这张表整份改回 notes（在现有版本上写）。"""
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "orders")
        await catalog.write_entry(session, source_id, "orders", notes, if_version=entry.version, actor="数据组")


async def test_a_choice_is_written_as_a_confirmed_human_item_with_its_origin(shop):
    name, source_id = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-42",
                               node_title="复核销售额", actor="张三")
    assert out == {"written": [{"source": name, "table": "orders", "item": "business_date",
                                "value": {"column": "paid_date"}}], "skipped": []}
    entry = await entry_of(source_id)
    item = entry.notes["business_date"]
    assert item["value"] == {"column": "paid_date"} and (item["source"], item["status"]) == ("human", "confirmed")
    assert "run-42" in item["note"] and "「复核销售额」" in item["note"] and "「销售额」" in item["note"]
    assert "盲算" in item["note"] and entry.version == 1 and entry.updated_by == "张三"


async def test_writing_the_same_choice_again_changes_nothing(shop):
    name, source_id = shop
    choice = [candidate(BY_ORDER_DATE, RIGHT, "b", source=name)]
    await record_choices(choice, run_id="run-1", node_title="复核", actor=None)
    again = await record_choices(choice, run_id="run-2", node_title="复核", actor=None)
    assert again["written"] == [] and "无需重复写入" in again["skipped"][0]["reason"]
    entry = await entry_of(source_id)
    assert entry.version == 1 and "run-1" in entry.notes["business_date"]["note"]


async def test_a_confirmed_different_item_is_kept(shop):
    name, source_id = shop
    await seed(source_id, {"business_date": catalog.make_item({"column": "order_date", "rule": "按下单日"}, "human",
                                                              "confirmed")})
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "已有人工确认过的说法「order_date」" in reason and "paid_date" in reason and "未覆盖" in reason
    entry = await entry_of(source_id)
    assert entry.version == 1 and entry.notes["business_date"]["value"]["column"] == "order_date"


async def test_an_inferred_condition_both_sides_use_is_extended(shop):
    """目录里推断的「is_test = 0」两边都按它筛了，盲算另外排除了退款：合起来写，原有的条件不丢。"""
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("is_test = 0", "llm")})
    base = f"SELECT SUM(amount) AS gmv FROM orders WHERE is_test = 0 AND paid_date {WEEK}"
    out = await record_choices([candidate(base, extra("status <> 'refunded'", base), "b", source=name)],
                               run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "valid_filter",
                              "value": "is_test = 0 AND status <> 'refunded'"}], out
    item = (await entry_of(source_id)).notes["valid_filter"]
    assert item["value"] == "is_test = 0 AND status <> 'refunded'"
    assert (item["source"], item["status"]) == ("human", "confirmed")


async def test_a_confirmed_condition_is_not_extended(shop):
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("is_test = 0", "human", "confirmed")})
    base = f"SELECT SUM(amount) AS gmv FROM orders WHERE is_test = 0 AND paid_date {WEEK}"
    out = await record_choices([candidate(base, extra("status <> 'refunded'", base), "b", source=name)],
                               run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "已有人工确认过的说法「is_test = 0」" in reason and "status <> 'refunded'" in reason and "未改动" in reason
    entry = await entry_of(source_id)
    assert entry.version == 1 and entry.notes["valid_filter"]["value"] == "is_test = 0"


async def test_an_inferred_condition_the_adopted_side_does_not_use_is_kept(shop):
    """目录里推断的「status = 'paid'」两边都没用：审核人的选择说明不了它对不对，不静默替换。"""
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("status = 'paid'", "llm")})
    out = await record_choices([candidate(WITH_REFUNDS, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "「status = 'paid'」" in reason and "被采纳的盲算没有按它筛选" in reason and "未改动" in reason
    entry = await entry_of(source_id)
    assert entry.version == 1 and entry.notes["valid_filter"]["value"] == "status = 'paid'"


async def test_an_inferred_same_condition_is_confirmed(shop):
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("status <> 'refunded'", "llm")})
    out = await record_choices([candidate(WITH_REFUNDS, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "valid_filter",
                              "value": "status <> 'refunded'"}]
    item = (await entry_of(source_id)).notes["valid_filter"]
    assert item["value"] == "status <> 'refunded'" and (item["source"], item["status"]) == ("human", "confirmed")


async def test_an_inferred_different_dedup_rule_is_kept(shop):
    name, source_id = shop
    await seed(source_id, {"dedup": catalog.make_item("同一订单可能有多行，按 id 去重", "llm")})
    out = await record_choices([candidate(BUYERS, BUYERS_DISTINCT, "b", metric="付款客户数", source=name)],
                               run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == [] and "按 id 去重" in out["skipped"][0]["reason"]
    assert (await entry_of(source_id)).notes["dedup"]["value"] == "同一订单可能有多行，按 id 去重"


async def test_the_same_column_inferred_is_confirmed_and_keeps_its_rule(shop):
    """目录里推断的业务日期正是这一列（还写了规则、时区）：确认它，规则、时区原样留着。"""
    name, source_id = shop
    await seed(source_id, {"business_date": catalog.make_item(
        {"column": "paid_date", "rule": "按付款成功时间", "timezone": "Asia/Shanghai"}, "profile")})
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "business_date",
                              "value": {"column": "paid_date", "rule": "按付款成功时间", "timezone": "Asia/Shanghai"}}]
    item = (await entry_of(source_id)).notes["business_date"]
    assert item["value"] == {"column": "paid_date", "rule": "按付款成功时间", "timezone": "Asia/Shanghai"}
    assert (item["source"], item["status"]) == ("human", "confirmed")


async def test_a_rejected_item_is_not_brought_back_but_a_rejected_other_value_does_not_block(shop):
    """同一个说法以前被人驳回过：两次人工判断对不上，不写；驳回的是别的说法：照写。"""
    name, source_id = shop
    await seed(source_id, {"business_date": catalog.make_item({"column": "paid_date"}, "llm", "rejected")})
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == [] and "曾被人工驳回" in out["skipped"][0]["reason"]
    assert (await entry_of(source_id)).version == 1

    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "a", source=name)], run_id="run-2",
                               node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "business_date",
                              "value": {"column": "order_date"}}]
    assert (await entry_of(source_id)).notes["business_date"]["value"] == {"column": "order_date"}


async def test_contradictory_choices_in_one_decision_write_nothing(shop):
    """同一次裁决里，一个指标采纳了按下单日、另一个采纳了按支付日：一张表只有一个业务日期，写哪个都是替人拿主意，
    都不写。"""
    name, source_id = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "a", metric="销售额", source=name),
                                candidate(BY_ORDER_DATE, RIGHT, "b", metric="复核销售额", source=name)],
                               run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == []
    [skipped] = out["skipped"]
    assert "「销售额」" in skipped["reason"] and "「复核销售额」" in skipped["reason"]
    assert "order_date" in skipped["reason"] and "paid_date" in skipped["reason"]
    assert "给出了不同的说法" in skipped["reason"] and "无法确定哪一个是数据事实" in skipped["reason"]
    assert await entry_of(source_id) is None


async def test_complementary_valid_filters_in_one_decision_are_combined(shop):
    """同一次裁决里，一个指标采纳了排除退款、另一个采纳了排除测试单：两条都在排除无效记录，各有人的选择撑着，
    不互相矛盾，用 AND 合成一句写。"""
    name, source_id = shop
    out = await record_choices([
        candidate(SUM_WEEK, extra("status <> 'refunded'"), "b", metric="销售额", source=name),
        candidate(SUM_WEEK, extra("is_test = 0"), "b", metric="订单金额", source=name),
        # 第三个指标采纳的是和第一个一样的条件：合并时不重复
        candidate(COUNT_WEEK, extra("status <> 'refunded'", COUNT_WEEK), "b", metric="订单数", source=name),
    ], run_id="run-1", node_title="复核", actor=None)
    assert out == {"written": [{"source": name, "table": "orders", "item": "valid_filter",
                                "value": "status <> 'refunded' AND is_test = 0"}], "skipped": []}
    entry = await entry_of(source_id)
    item = entry.notes["valid_filter"]
    assert item["value"] == "status <> 'refunded' AND is_test = 0" and entry.version == 1
    assert (item["source"], item["status"]) == ("human", "confirmed")
    assert all(f"「{m}」" in item["note"] for m in ("销售额", "订单金额", "订单数")) and "run-1" in item["note"]


async def test_combined_filters_extend_an_inferred_condition_every_adopted_side_uses(shop):
    """目录里推断的「channel <> 'test'」：几个被采纳的一边都按它筛了，合起来写；有一个没按它筛的，关系拿不准，不动。"""
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("channel <> 'test'", "llm")})
    base = extra("channel <> 'test'")
    both = [candidate(base, extra("status <> 'refunded'", base), "b", metric="销售额", source=name),
            candidate(base, extra("is_test = 0", base), "b", metric="订单金额", source=name)]
    out = await record_choices(both, run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "valid_filter",
                              "value": "channel <> 'test' AND status <> 'refunded' AND is_test = 0"}], out
    assert (await entry_of(source_id)).notes["valid_filter"]["value"] == \
        "channel <> 'test' AND status <> 'refunded' AND is_test = 0"

    await seed_again(source_id, {"valid_filter": catalog.make_item("channel <> 'test'", "llm")})
    one_without = [both[0], candidate(SUM_WEEK, extra("is_test = 0"), "b", metric="订单金额", source=name)]
    out = await record_choices(one_without, run_id="run-2", node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "「channel <> 'test'」" in reason and "指标「订单金额」被采纳的盲算没有按它筛选" in reason and "未改动" in reason
    assert (await entry_of(source_id)).notes["valid_filter"]["value"] == "channel <> 'test'"


async def test_a_rejected_condition_is_not_written_back_through_a_combined_decision(shop):
    """目录里「status <> 'refunded'」被人工驳回过。同一次裁决里「销售额」「订单数」采纳了排除退款、「订单金额」采纳了
    排除测试单：审批卡上预告前两个不写（曾被人工驳回）、后一个写「is_test = 0」，真写也照这样。驳回过的条件不能借着
    合并又写成人工确认，拿出来的指标照实说原因。"""
    name, source_id = shop
    rejected = {"valid_filter": catalog.make_item("status <> 'refunded'", "llm", "rejected")}
    await seed(source_id, rejected)
    refunds = candidate(SUM_WEEK, extra("status <> 'refunded'"), "b", metric="销售额", source=name)
    tests = candidate(SUM_WEEK, extra("is_test = 0"), "b", metric="订单金额", source=name)
    counted = candidate(COUNT_WEEK, extra("status <> 'refunded'", COUNT_WEEK), "b", metric="订单数", source=name)
    notes = {"orders": (await entry_of(source_id)).notes}
    assert preview(refunds, SCHEMA, notes)["b"]["write"] is False
    assert preview(tests, SCHEMA, notes)["b"]["value"] == "is_test = 0"
    out = await record_choices([refunds, tests, counted], run_id="run-1", node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "valid_filter",
                              "value": "is_test = 0"}], out
    [skipped] = out["skipped"]
    assert "「status <> 'refunded'」曾被人工驳回" in skipped["reason"] and "「销售额」、「订单数」" in skipped["reason"]
    assert "订单金额" not in skipped["reason"]
    item = (await entry_of(source_id)).notes["valid_filter"]
    assert item["value"] == "is_test = 0" and (item["source"], item["status"]) == ("human", "confirmed")
    assert "「订单金额」" in item["note"] and "销售额" not in item["note"] and "订单数" not in item["note"]

    # 并成一项的每个指标采纳的都是驳回过的说法：一个都不写，一条原因说清是哪几个指标
    await seed_again(source_id, rejected)
    out = await record_choices([refunds, counted], run_id="run-2", node_title="复核", actor=None)
    assert out["written"] == [] and len(out["skipped"]) == 1, out
    assert "曾被人工驳回" in out["skipped"][0]["reason"] and "「销售额」、「订单数」" in out["skipped"][0]["reason"]
    assert (await entry_of(source_id)).notes["valid_filter"]["status"] == "rejected"


async def test_a_rejected_statement_is_not_rebuilt_from_several_metrics(shop):
    """驳回的是「is_test = 0 AND status != 'refunded'」整句：单看每个指标都不是这句（预告各写各的），合起来却正是这句
    （!= 和 <>、先后不同也是同一个说法）。两次人工判断对不上，都不写；再并进一个别的条件，驳回的整句仍在里面，照样不写。"""
    name, source_id = shop
    rejected = catalog.make_item("is_test = 0 AND status != 'refunded'", "llm", "rejected")
    await seed(source_id, {"valid_filter": rejected})
    refunds = candidate(SUM_WEEK, extra("status <> 'refunded'"), "b", metric="销售额", source=name)
    tests = candidate(SUM_WEEK, extra("is_test = 0"), "b", metric="订单金额", source=name)
    channel = candidate(COUNT_WEEK, extra("channel <> 'test'", COUNT_WEEK), "b", metric="订单数", source=name)
    notes = {"orders": (await entry_of(source_id)).notes}
    assert all(preview(c, SCHEMA, notes)["b"]["write"] is True for c in (refunds, tests, channel))
    for picked, names in (([refunds, tests], "「销售额」、「订单金额」"),
                          ([refunds, tests, channel], "「销售额」、「订单金额」、「订单数」")):
        out = await record_choices(picked, run_id="run-1", node_title="复核", actor=None)
        assert out["written"] == [], out
        [skipped] = out["skipped"]
        assert "曾被人工驳回" in skipped["reason"] and names in skipped["reason"]
    entry = await entry_of(source_id)
    assert entry.version == 1 and entry.notes["valid_filter"]["status"] == "rejected"


@pytest.mark.parametrize("rejected", ["status != 'refunded'", "STATUS <> 'refunded'"])
async def test_a_rejected_condition_is_recognised_however_it_was_written(shop, rejected):
    """驳回时写的是 != 、列名大写：和这次要写的「status <> 'refunded'」是同一个说法，预告和真写都不写。"""
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item(rejected, "llm", "rejected")})
    picked = candidate(WITH_REFUNDS, RIGHT, "b", source=name)
    said = preview(picked, SCHEMA, {"orders": (await entry_of(source_id)).notes})
    assert said["b"]["write"] is False and f"「{rejected}」曾被人工驳回" in said["b"]["reason"]
    out = await record_choices([picked], run_id="run-1", node_title="复核", actor=None)
    assert out == {"written": [], "skipped": [{"reason": said["b"]["reason"]}]}
    assert (await entry_of(source_id)).version == 1


async def test_combined_filters_do_not_repeat_a_condition_already_in_the_catalog(shop):
    """目录里推断的「status <> 'refunded'」：「销售额」采纳了带着它的盲算，「订单金额」两边都按它筛、盲算另外排除了
    测试单。合起来写时已有的条件只出现一次（正是「订单金额」的预告）；已有的条件是人工确认过的，原因里只说另外多出
    的那一段。"""
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("status <> 'refunded'", "llm")})
    base = extra("status <> 'refunded'")
    picked = [candidate(SUM_WEEK, base, "b", metric="销售额", source=name),
              candidate(base, extra("is_test = 0", base), "b", metric="订单金额", source=name)]
    said = preview(picked[1], SCHEMA, {"orders": (await entry_of(source_id)).notes})
    out = await record_choices(picked, run_id="run-1", node_title="复核", actor=None)
    assert out == {"written": [{"source": name, "table": "orders", "item": "valid_filter",
                                "value": "status <> 'refunded' AND is_test = 0"}], "skipped": []}
    item = (await entry_of(source_id)).notes["valid_filter"]
    assert item["value"] == said["b"]["value"] == "status <> 'refunded' AND is_test = 0"

    await seed_again(source_id, {"valid_filter": catalog.make_item("status <> 'refunded'", "human", "confirmed")})
    out = await record_choices(picked, run_id="run-2", node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "已有人工确认过的说法「status <> 'refunded'」" in reason and "另外多出条件「is_test = 0」" in reason
    assert (await entry_of(source_id)).notes["valid_filter"]["value"] == "status <> 'refunded'"


def test_conditions_are_joined_without_repeating_a_part():
    """几句条件接成一句：已经有的段不再接（整句接上会把已有的段再写一遍）；只说多出的时不带原有的；最外层是 OR 的
    只在接了别的条件时加括号。"""
    from app.engine.dual_check_facts import _and_join

    assert _and_join("status <> 'refunded'", ["status <> 'refunded' AND is_test = 0"], "sqlite") == \
        "status <> 'refunded' AND is_test = 0"
    assert _and_join("status <> 'refunded'", ["STATUS != 'refunded'", "is_test = 0", "is_test = 0"], "sqlite") == \
        "status <> 'refunded' AND is_test = 0"
    assert _and_join("status <> 'refunded'", ["status <> 'refunded' AND is_test = 0"], "sqlite",
                     keep_base=False) == "is_test = 0"
    assert _and_join("status = 'void' OR is_test = 1", ["(status = 'void' OR is_test = 1)"], "sqlite") == \
        "status = 'void' OR is_test = 1"
    assert _and_join("status = 'void' OR is_test = 1", ["is_test = 0"], "sqlite") == \
        "(status = 'void' OR is_test = 1) AND is_test = 0"


async def test_the_same_choice_from_two_metrics_is_written_once(shop):
    name, source_id = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", metric="销售额", source=name),
                                candidate(BY_ORDER_DATE, RIGHT, "b", metric="订单金额", source=name)],
                               run_id="run-1", node_title="复核", actor=None)
    assert out == {"written": [{"source": name, "table": "orders", "item": "business_date",
                                "value": {"column": "paid_date"}}], "skipped": []}
    entry = await entry_of(source_id)
    assert entry.version == 1 and "「销售额」" in entry.notes["business_date"]["note"]
    assert "「订单金额」" in entry.notes["business_date"]["note"]


# --------------------------------------------------------------------------
# 审批卡上的预告：选哪一边各会写什么（和真写同一套判断）
# --------------------------------------------------------------------------


def test_the_preview_says_what_each_choice_would_write():
    picked = candidate(BY_ORDER_DATE, RIGHT, "a")
    assert preview(picked, SCHEMA, {}) == {
        "a": {"write": True, "source": "shop", "table": "orders", "item": "business_date",
              "value": {"column": "order_date"}},
        "b": {"write": True, "source": "shop", "table": "orders", "item": "business_date",
              "value": {"column": "paid_date"}},
    }
    # 只有被采纳的一边有条件时才写：选另一边照实说为什么不写
    said = preview(candidate(WITH_REFUNDS, RIGHT, "a"), SCHEMA, {})
    assert said["a"]["write"] is False and "被采纳的原流程没有这个条件" in said["a"]["reason"]
    assert said["b"] == {"write": True, "source": "shop", "table": "orders", "item": "valid_filter",
                         "value": "status <> 'refunded'"}


def test_the_preview_reads_the_catalog_as_it_is():
    """目录里已有的说法照写入时的规矩算：确认过的不覆盖、同样的说法不重写、推断的有效记录条件合起来写。"""
    confirmed = {"orders": {"business_date": catalog.make_item({"column": "order_date"}, "human", "confirmed")}}
    said = preview(candidate(BY_ORDER_DATE, RIGHT, "a"), SCHEMA, confirmed)
    assert said["a"]["write"] is False and "无需重复写入" in said["a"]["reason"]
    assert said["b"]["write"] is False and "已有人工确认过的说法「order_date」" in said["b"]["reason"]

    inferred = {"orders": {"valid_filter": catalog.make_item("is_test = 0", "llm")}}
    base = f"SELECT SUM(amount) AS gmv FROM orders WHERE is_test = 0 AND paid_date {WEEK}"
    said = preview(candidate(base, extra("status <> 'refunded'", base), "a"), SCHEMA, inferred)
    assert said["b"]["write"] is True and said["b"]["value"] == "is_test = 0 AND status <> 'refunded'"


async def test_a_missing_source_or_an_unrecordable_choice_is_reported(shop):
    name, _ = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source="no_such_source"),
                                candidate(BOTH_WRONG, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == []
    assert "数据源「no_such_source」已不存在" in out["skipped"][0]["reason"]
    assert "不止差一个方面" in out["skipped"][1]["reason"]


# --------------------------------------------------------------------------
# 预告和真写逐字一致：挡下的一边也说写到哪、为什么；几个指标落到同一项上按合并后的结果预告
# --------------------------------------------------------------------------


def test_a_side_the_catalog_blocks_still_says_where_and_why():
    """认出了事实、只是被目录挡下（已确认、驳回过、和已有条件合不起来）的一边：照样带上写到哪张表的哪一项、本来要写
    什么，原因和真写时跳过的原因一字不差。认不出事实的一边只有原因"""
    confirmed = {"orders": {"business_date": catalog.make_item({"column": "order_date"}, "human", "confirmed")}}
    said = preview(candidate(BY_ORDER_DATE, RIGHT, "a"), SCHEMA, confirmed)
    target = {"source": "shop", "table": "orders", "item": "business_date"}
    assert said["a"] == {"write": False, **target, "value": {"column": "order_date"},
                         "reason": "「orders」的业务日期已是人工确认过的「order_date」，无需重复写入"}
    assert said["b"]["write"] is False and {k: said["b"][k] for k in target} == target
    assert said["b"]["value"] == {"column": "paid_date"} and "已有人工确认过的说法「order_date」" in said["b"]["reason"]
    # 认不出事实（采纳的一边没有这个条件）：没有写到哪，只有原因
    said = preview(candidate(WITH_REFUNDS, RIGHT, "a"), SCHEMA, {})
    assert set(said["a"]) == {"write", "reason"} and said["a"]["write"] is False


def test_the_joint_preview_covers_every_way_two_metrics_land_on_one_item():
    """两个指标都只差时间列：各自选原流程是 order_date、选盲算是 paid_date，落到同一张表的业务日期上。四种组合各预告
    一次：说法一样的写，说法不同的两个都不写，原因是真写时那一句"""
    from app.engine.dual_check_facts import preview_joint

    picked = [{**candidate(BY_ORDER_DATE, RIGHT, "a", metric="销售额"), "metric_id": "gmv"},
              {**candidate(BY_ORDER_DATE, RIGHT, "a", metric="订单金额"), "metric_id": "total"}]
    joint = preview_joint(picked, {"shop": (SCHEMA, {})})
    by = {tuple((m["metric_id"], m["choice"]) for m in e["members"]): e for e in joint}
    assert set(by) == {(("gmv", a), ("total", b)) for a in "ab" for b in "ab"}
    for entry in joint:
        assert {k: entry[k] for k in ("source", "table", "item")} == \
            {"source": "shop", "table": "orders", "item": "business_date"}
    assert by[(("gmv", "b"), ("total", "b"))]["outcomes"] == {
        "gmv": {"write": True, "value": {"column": "paid_date"}},
        "total": {"write": True, "value": {"column": "paid_date"}}}
    split = by[(("gmv", "a"), ("total", "b"))]["outcomes"]
    assert split["gmv"] == split["total"] and split["gmv"]["write"] is False
    assert "给出了不同的说法" in split["gmv"]["reason"] and "「销售额」" in split["gmv"]["reason"]


def test_the_joint_preview_screens_out_a_rejected_condition_like_the_write():
    """驳回过「status <> 'refunded'」：两个指标并成一项时，带回驳回说法的那个拿出来不写（原因同真写），另一个照写自己的"""
    from app.engine.dual_check_facts import preview_joint

    notes = {"orders": {"valid_filter": catalog.make_item("status <> 'refunded'", "llm", "rejected")}}
    picked = [{**candidate(SUM_WEEK, extra("status <> 'refunded'"), "b", metric="销售额"), "metric_id": "gmv"},
              {**candidate(SUM_WEEK, extra("is_test = 0"), "b", metric="订单金额"), "metric_id": "total"}]
    [entry] = preview_joint(picked, {"shop": (SCHEMA, notes)})
    assert [(m["metric_id"], m["choice"]) for m in entry["members"]] == [("gmv", "b"), ("total", "b")]
    assert entry["outcomes"]["total"] == {"write": True, "value": "is_test = 0"}
    assert entry["outcomes"]["gmv"]["write"] is False and "曾被人工驳回" in entry["outcomes"]["gmv"]["reason"]


def test_too_many_combinations_are_left_to_the_timeline():
    """几个指标落到同一项上的组合太多（每个两边都落在这一项上，3 的指标数次方）：这一项不预告，审批卡回到有条件地说"""
    from app.engine import dual_check_facts as facts

    picked = [{**candidate(BY_ORDER_DATE, RIGHT, "a", metric=f"指标{i}"), "metric_id": f"m{i}"} for i in range(6)]
    assert facts.preview_joint(picked, {"shop": (SCHEMA, {})}) == []
    assert len(facts.preview_joint(picked[:3], {"shop": (SCHEMA, {})})) == 3 ** 3 - 1 - 3 * 2


def test_combinations_are_counted_before_they_are_listed(monkeypatch):
    """二十个指标都落到同一项上：组合数是 3 的二十次方量级，得先算出个数、超了就跳过，不能先列出来再比上限——
    列一遍就要几十 GB 内存，在服务进程里算（asyncio.to_thread）会把整个服务拖死。预告时从 itertools.product 取了
    多少个组合记下来：这一项不预告，取的不该超过上限"""
    import itertools

    from app.engine import dual_check_facts as facts

    drawn = 0
    real = itertools.product

    def guarded(*iterables, **kw):
        nonlocal drawn
        for combo in real(*iterables, **kw):
            drawn += 1
            # 红的时候不至于真把内存吃光：取得比上限多出一截就停
            assert drawn <= facts.JOINT_LIMIT + 64, "列出的组合远多于上限：没有先算个数"
            yield combo

    stem = candidate(BY_ORDER_DATE, RIGHT, "a")
    picked = [{**stem, "metric": f"指标{i}", "metric_id": f"m{i}"} for i in range(20)]
    monkeypatch.setattr(itertools, "product", guarded)
    assert facts.preview_joint(picked, {"shop": (SCHEMA, {})}) == []
    assert drawn <= facts.JOINT_LIMIT


def test_each_item_has_its_own_limit():
    """上限按每一项算，不是几项共用一份额度：两个数据源的业务日期各有四个指标落上来（各 72 种组合），两项都预告。
    以前共用 JOINT_LIMIT 的额度，前一项用掉 72 种，后一项明明没超上限也被悄悄跳过"""
    from app.engine import dual_check_facts as facts

    picked = [{**candidate(BY_ORDER_DATE, RIGHT, "a", metric=f"指标{i}", source=source), "metric_id": f"{source}-{i}"}
              for source in ("shop", "outlet") for i in range(4)]
    joint = facts.preview_joint(picked, {"shop": (SCHEMA, {}), "outlet": (SCHEMA, {})})
    per = 3 ** 4 - 1 - 4 * 2
    assert per <= facts.JOINT_LIMIT < 2 * per
    assert sorted({e["source"] for e in joint}) == ["outlet", "shop"]
    assert len(joint) == 2 * per


@pytest.mark.parametrize("choices", [("a", "b"), ("b", "b"), ("b", "a"), ("a", None)])
async def test_the_joint_preview_is_what_gets_written(shop, choices):
    """预告和真写逐字一致：按这组选择，各指标的预告（单独落一项的看 preview，几个落到同一项的看 preview_joint）说写的，
    目录里就是那个值；说不写的，原因就在真写跳过的原因里"""
    from app.engine.dual_check_facts import preview_joint

    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("is_test = 0", "llm")})
    base = f"SELECT SUM(amount) AS gmv FROM orders WHERE is_test = 0 AND paid_date {WEEK}"
    picked = [{**candidate(base, extra("status <> 'refunded'", base), "a", metric="销售额", source=name),
               "metric_id": "gmv"},
              {**candidate(base, extra("channel <> 'test'", base), "a", metric="订单金额", source=name),
               "metric_id": "total"}]
    notes = {"orders": (await entry_of(source_id)).notes}
    singles = {c["metric_id"]: preview(c, SCHEMA, notes) for c in picked}
    joint = preview_joint(picked, {name: (SCHEMA, notes)})
    chosen = {c["metric_id"]: choice for c, choice in zip(picked, choices) if choice}
    expected = said_for(chosen, singles, joint)
    out = await record_choices([{**c, "choice": chosen[c["metric_id"]]} for c in picked if c["metric_id"] in chosen],
                               run_id="run-1", node_title="复核", actor=None)
    entry = await entry_of(source_id)
    for metric_id, said in expected.items():
        if said["write"]:
            assert {"source": name, "table": "orders", "item": said["item"], "value": said["value"]} in out["written"]
            assert entry.notes[said["item"]]["value"] == said["value"]
        else:
            assert said["reason"] in [s["reason"] for s in out["skipped"]], (metric_id, said, out)


def said_for(chosen: dict, singles: dict, joint: list) -> dict:
    """审批卡的认法：一个指标选的那一边认出了写到哪，就看按当前的选择还有谁落到同一项上——只有它自己时照单个指标的
    预告，几个时照 preview_joint 里这几个成员的那一条"""
    out = {}
    for metric_id, choice in chosen.items():
        side = singles[metric_id][choice]
        if "table" not in side:
            out[metric_id] = side
            continue
        where = (side["source"], side["table"], side["item"])
        peers = sorted((m, c) for m, c in chosen.items()
                       if "table" in singles[m][c] and (singles[m][c]["source"], singles[m][c]["table"],
                                                        singles[m][c]["item"]) == where)
        if len(peers) == 1:
            out[metric_id] = side
            continue
        [entry] = [e for e in joint if sorted((m["metric_id"], m["choice"]) for m in e["members"]) == peers]
        out[metric_id] = {**entry["outcomes"][metric_id], "item": entry["item"]}
    return out
