"""助手写查库 SQL 时给结果列起中文别名，报告里的表格表头直接可读。

真实运行里：报告节点用 [[table:…]] 把查询结果原样生成表格，表头就是 Agent 写的 SQL 里的列名——一串英文缩写的
原始列名，读报告的人得猜。结果列起中文别名（AS 月份、AS 曝光量）以后，表头、单元格引用、口径卡的
cell() 都直接用中文名。

中文别名要按方言加标识符引号：MySQL / MariaDB 用反引号，PostgreSQL、Oracle、SQLite 用双引号；单引号是字符串。

这组守的是：
- 节点手册里 Agent 查库的说明、写 SQL 的硬性要求（搭查库节点时的规则）都有这一条，按方言给出示例
- 加了引号的中文别名不让自查放弃列名核对：以前 SQL 里只要有带引号的标识符就整条不判，照这条规矩写的 SQL
  会把猜错的英文列名一起放过去
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.copilot import NODE_REFERENCE, sql_unknown_columns
from app.api.copilot_context import SQL_RULES_PROMPT


def _agent_rule() -> str:
    start = NODE_REFERENCE.index("- agent：带工具循环的 agent")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- supervisor：", start)]


@pytest.mark.parametrize("text", [_agent_rule(), SQL_RULES_PROMPT], ids=["agent", "sql-rules"])
def test_guidance_asks_for_chinese_aliases_with_dialect_quotes(text):
    assert "中文别名" in text
    assert "AS `月份`" in text and "MySQL" in text           # MySQL / MariaDB：反引号
    assert 'AS "月份"' in text and "PostgreSQL" in text and "Oracle" in text and "SQLite" in text
    assert "单引号" in text                                    # 单引号是字符串，不是别名


def _source(**tables):
    return SimpleNamespace(name="shop", schema_cache={"tables": {
        name: {"qualified": name, "columns": [{"name": c} for c in cols]} for name, cols in tables.items()}})


SHOP = _source(daily_stats=["id", "stat_date", "impressions", "clicks", "region"])


@pytest.mark.parametrize("sql", [
    'SELECT stat_date AS "日期", SUM(impressions) AS "曝光量" FROM daily_stats GROUP BY stat_date ORDER BY "日期"',
    "SELECT stat_date AS `日期`, SUM(impressions) AS `曝光量` FROM daily_stats GROUP BY stat_date ORDER BY `曝光量` DESC",
    "SELECT stat_date AS 日期, SUM(clicks) AS 点击量 FROM daily_stats GROUP BY stat_date",
    'SELECT region AS "区域", COUNT(*) AS "天数" FROM daily_stats WHERE region = \'华东\' GROUP BY region',
])
def test_quoted_chinese_aliases_are_fine(sql):
    assert sql_unknown_columns(sql, SHOP) is None, sql_unknown_columns(sql, SHOP)


@pytest.mark.parametrize("sql, unknown", [
    ('SELECT stat_date AS "日期", SUM(impresions) AS "曝光量" FROM daily_stats GROUP BY stat_date', ["impresions"]),
    ("SELECT stat_month AS `月份`, SUM(impressions) AS `曝光量` FROM daily_stats GROUP BY stat_month",
     ["stat_month"]),
])
def test_guessed_columns_next_to_quoted_chinese_aliases_are_still_caught(sql, unknown):
    found = sql_unknown_columns(sql, SHOP)
    assert found is not None and found[0] == unknown, found


def test_quoted_ascii_identifiers_are_still_not_guessed():
    # 带引号的英文标识符：大小写、空格都可能，照旧不判
    assert sql_unknown_columns('SELECT "StatDate", impresions FROM daily_stats', SHOP) is None
