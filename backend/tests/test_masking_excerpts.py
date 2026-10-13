"""交给核查 agent 的证据摘录分段遮（engine/inquiry.py 的 ExcerptMask、_scrub_excerpt_sql，engine/masking.py 的
masked_cell）：

- SQL 照质询封存 SQL 的办法换：范围比较的阈值、LIMIT 的行数照原样（恰好等于某个遮罩值也一样），字符串、注释、名字里的
  遮罩值换；和遮罩列（含改了名字的）做等值、IN、LIKE 一类比较的字面量不论认不认得出都换（报告没选出遮罩列时快照里收
  不到这些值），同一个数写成阈值的跟着换；已经摆在读者眼前的值（问题、被问的那句）照旧不换
- 格子逐格遮：数值格（数、列类型是 number 的）照原样，文字格整格的、夹着的那一截换掉（换了写法的号码也认）

以前对渲染好的整段摘录按值换，分不出格子和 SQL 里的用法：阈值、没设遮罩的访问次数恰好等于某一笔被遮的金额，都成了
「已遮罩」。端到端的用例在 test_challenge_masking、test_inquiry_masking_e2e。号码、姓名、金额都是虚构的。
"""
from __future__ import annotations

import pytest

from app.engine import masking
from app.engine.inquiry import ExcerptMask, _scrub_excerpt_sql

MASKED = "已遮罩"


def secrets_of(*values, protect: tuple[str, ...] = ()) -> masking._Secrets:
    """报告快照里收到的遮罩值（金额、号码），去掉已经摆在读者眼前的（protect）。"""
    secrets = masking._Secrets()
    secrets.add({"columns": ["v"], "rows": [[v] for v in values]}, {"v"})
    secrets.protect(protect)
    return secrets


AMOUNTS = secrets_of(20000, 30000, 50000)


@pytest.mark.parametrize("sql", [
    "SELECT id, visits FROM sales WHERE amount >= 20000 ORDER BY id",
    "SELECT id FROM sales WHERE amount BETWEEN 20000 AND 50000 LIMIT 30000",
    "SELECT region, COUNT(*) AS n FROM sales WHERE visits > 30000 GROUP BY region",
])
def test_thresholds_and_limits_stay_as_written(sql):
    """范围比较的边界、LIMIT 的行数是报告自己定的条件，恰好等于某一笔被遮的金额也照原样：agent 要看得到报告按什么取数。"""
    assert _scrub_excerpt_sql(sql, AMOUNTS, {"amount"}, "sqlite") == sql


def test_a_value_the_report_looked_for_is_masked():
    """在找某一笔的（等值比较）照旧换：这是在点名一个遮罩值。"""
    assert _scrub_excerpt_sql("SELECT id FROM sales WHERE amount = 50000", AMOUNTS, {"amount"}, "sqlite") \
        == f"SELECT id FROM sales WHERE amount = {MASKED}"


@pytest.mark.parametrize("sql, expected", [
    ("SELECT region FROM sales WHERE buyer_phone NOT IN ('13900000002', '139-0000-0003')",
     f"SELECT region FROM sales WHERE buyer_phone NOT IN ('{MASKED}', '{MASKED}')"),
    ("SELECT region FROM sales WHERE buyer_phone = '13900000009' OR buyer_phone LIKE '%0001'",
     f"SELECT region FROM sales WHERE buyer_phone = '{MASKED}' OR buyer_phone LIKE '{MASKED}'"),
    ("SELECT region FROM (SELECT region, buyer_phone AS 手机号 FROM sales) t WHERE 手机号 IN ('13900000002')",
     f"SELECT region FROM (SELECT region, buyer_phone AS 手机号 FROM sales) t WHERE 手机号 IN ('{MASKED}')"),
    ("SELECT region FROM sales WHERE region = '华东'", "SELECT region FROM sales WHERE region = '华东'"),
])
def test_literals_compared_with_a_masked_column_are_masked_even_when_never_collected(sql, expected):
    """和遮罩列（含改了名字的）做等值、IN、LIKE 比较的字面量：报告快照里收不到这些值（报告没选出号码），照样换掉；和没设
    遮罩的列比较的照原样。"""
    assert _scrub_excerpt_sql(sql, masking._Secrets(), {"buyer_phone", "手机号"}, "sqlite") == expected


def test_a_threshold_equal_to_a_masked_literal_goes_with_it():
    """同一个数既在找某一笔、又写成阈值：阈值留着就把换掉的值露了，跟着换（同封存 SQL 的 _literals）。"""
    sql = "SELECT id FROM sales WHERE buyer_phone = 13900000009 OR id >= 13900000009"
    assert _scrub_excerpt_sql(sql, masking._Secrets(), {"buyer_phone"}, "sqlite") \
        == f"SELECT id FROM sales WHERE buyer_phone = {MASKED} OR id >= {MASKED}"


def test_values_already_in_front_of_the_reader_are_not_masked():
    """提问人、被问的那句里写着的值（protect）照旧不换，换了写法写的也认得出。"""
    secrets = secrets_of(protect=("139-0000-0009 这位客户的单",))
    sql = "SELECT id FROM sales WHERE buyer_phone = '13900000009' OR buyer_phone = '13900000008'"
    assert _scrub_excerpt_sql(sql, secrets, {"buyer_phone"}, "sqlite") \
        == f"SELECT id FROM sales WHERE buyer_phone = '13900000009' OR buyer_phone = '{MASKED}'"


def test_strings_and_comments_are_masked_by_value():
    """字符串、注释里写着的遮罩值按值换（换了写法的号码也认），同封存 SQL。"""
    secrets = secrets_of("13900000001")
    sql = "SELECT id FROM sales /* 客户 139-0000-0001 */ WHERE note LIKE '%13900000001%'"
    assert _scrub_excerpt_sql(sql, secrets, set(), "sqlite") \
        == f"SELECT id FROM sales /* 客户 {MASKED} */ WHERE note LIKE '%{MASKED}%'"


@pytest.mark.parametrize("value, numeric, expected", [
    (20000, False, 20000),
    (20000.0, False, 20000.0),
    ("20000.00", True, "20000.00"),
    ("20000", False, MASKED),
    ("上月消费 20000 元", False, f"上月消费 {MASKED} 元"),
    ("回访：联系 139-0000-0003", False, f"回访：联系 {MASKED}"),
    ({"phone": "13900000003"}, False, '{"phone": "' + MASKED + '"}'),
    ("例行回访", False, "例行回访"),
    (None, False, None),
])
def test_cells_are_masked_like_the_tool_boundary(value, numeric, expected):
    """格子同工具边界：数（整数、小数，列类型是 number 的字符串）照原样；文字格整格是遮罩值的、夹着的那一截换掉，JSON
    按画出来的样子换。"""
    secrets = secrets_of(20000, "13900000003")
    assert masking.masked_cell(value, secrets, numeric=numeric) == expected


def test_an_excerpt_mask_without_values_only_masks_by_column():
    """没有可按值换的（报告快照里没收到值）：格子、文字照原样，SQL 里和遮罩列做等值比较的照样换。"""
    mask = ExcerptMask({"shop": "sqlite"})
    assert mask.cell("13900000001", numeric=False) == "13900000001"
    assert mask.text("客户 13900000001") == "客户 13900000001"
    assert mask.sql("SELECT 1 FROM sales WHERE buyer_phone = '13900000001'", {"source": "shop"}, {"buyer_phone"}) \
        == f"SELECT 1 FROM sales WHERE buyer_phone = '{MASKED}'"
