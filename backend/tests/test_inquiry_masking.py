"""审批质询的遮罩（engine/inquiry.py）：回答正文、查询清单、质询工件里都不露出数据源遮罩列的值，也不误伤别的数。

- 改了名字的遮罩列（起别名、套函数、经子查询或 CTE 转一道、UNION 的另一支）照样遮；COUNT 只数个数、窗口的分区键
  只决定次序，不遮；解析不了、SQL 里又写着遮罩列的，整份快照遮掉
- 按值认的遮罩值有门槛：纯数字够长（SECRET_DIGITS 位）、文字至少两个字；日期、小数、千分位里的一截不算；提问人自己
  写进问题、选中原文里的不换
- SQL 只换字面量（和注释）里的遮罩值：直接和遮罩列比较的字面量是遮罩列出现过的值就换，短的数也换；LIMIT、日期、
  别的列的条件照原样
- 快照按列遮，另按值遮：别的列里写着遮罩值的格子也换成「已遮罩」
- 组装回答两遍之后正文里还认得出遮罩值的，不交出去
- 遮罩值上万个时换一遍也不卡：不现编上万个分支的正则

数据都是虚构的：手机号、年龄、区域、金额。
"""
from __future__ import annotations

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


def hidden_of(sql: str, columns: list[str], masked: set[str] = frozenset({"buyer_phone"})) -> set[str]:
    snapshot = {"source": "shop", "sql": sql, "columns": columns, "rows": []}
    return inquiry_mod._derived_hidden(snapshot, set(masked), "sqlite")[0]


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


@pytest.mark.parametrize("sql, columns", [
    ("SELECT region AS 区域, COUNT(DISTINCT buyer_phone) AS 买家数 FROM sales GROUP BY region", ["区域", "买家数"]),
    ("SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales WHERE buyer_phone = '13900000001' GROUP BY region",
     ["区域", "销售额"]),
    ("SELECT ROW_NUMBER() OVER (PARTITION BY buyer_phone ORDER BY amount) AS 序号 FROM sales", ["序号"]),
    ("SELECT (SELECT COUNT(*) FROM sales s2 WHERE s2.buyer_phone = s.buyer_phone) AS 笔数 FROM sales s", ["笔数"]),
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
     "SELECT COUNT(*) FROM sales s WHERE s.buyer_phone IN ('已遮罩', '已遮罩') AND age BETWEEN 已遮罩 AND 已遮罩"),
    ("SELECT COUNT(*) FROM sales WHERE age = 25 AND amount > 25 AND paid_on >= '2026-01-25' LIMIT 25",
     "SELECT COUNT(*) FROM sales WHERE age = 已遮罩 AND amount > 25 AND paid_on >= '2026-01-25' LIMIT 25"),
    ("SELECT COUNT(*) FROM sales WHERE 25 = age AND note LIKE '%13900000003%' -- 13900000001 的单\nLIMIT 20",
     "SELECT COUNT(*) FROM sales WHERE 已遮罩 = age AND note LIKE '%已遮罩%' -- 已遮罩 的单\nLIMIT 20"),
    ("SELECT region FROM sales WHERE buyer_phone NOT IN ('13900000009') AND age <> 40",
     "SELECT region FROM sales WHERE buyer_phone NOT IN ('13900000009') AND age <> 40"),
    ("SELECT amount FROM sales WHERE amount = 13900000001", "SELECT amount FROM sales WHERE amount = 已遮罩"),
])
def test_sql_keeps_everything_but_the_masked_values(people, sql, shown):
    assert inquiry_mod._scrub_sql(sql, people, {"buyer_phone", "age"}, "sqlite") == shown


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


def test_an_answer_still_showing_a_masked_value_is_not_handed_out():
    """运行输入里恰好是一个遮罩值（报告目录里的 i: 条目）：标记里看不出来，渲染出来才露，连标记一起换也换不掉——
    宁可不交这份回答。"""
    secrets = secrets_of({"columns": ["phone"], "rows": [["13900000001"]]}, {"phone"})
    catalog = {"i:客户": {"alias": "i:客户", "kind": "input", "eid": "input:客户", "locator": {"field": "客户"},
                         "value": "13900000001", "label": "客户 = 13900000001"}}
    with pytest.raises(inquiry_mod._Leak):
        inquiry_mod._compose("这位客户是 [[i:客户]]。", catalog, run_id="r1", views={}, secrets=secrets, allow=[])
    doc = inquiry_mod._compose("没有遮罩值。", catalog, run_id="r1", views={}, secrets=secrets, allow=[])
    assert doc["markdown"] == "没有遮罩值。"


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
