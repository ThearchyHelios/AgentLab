"""按 SQL 认取自遮罩列的结果列（engine/masking.py 的 _derived_hidden）：FROM、JOIN、LATERAL 里的表值函数，汇总里的 CASE。

核查 agent 的查询在工具边界按这套认法整列遮掉（mask_query_result），审批质询、双盲复算也用它：

- 表值函数（SQLite 的 json_each / json_tree，Postgres 的 unnest、jsonb_each_text、regexp_split_to_table、LATERAL
  VALUES，MySQL 的 JSON_TABLE）把遮罩列拆开时，它出的列都算取自遮罩列：遮罩列是 JSON 时，用 json_each 拆开是最自然的
  写法，以前只追 SELECT 项、子查询和 *，这些写法的原值原样交给模型、落进快照。出列名认得全的（常见的几个函数、写了
  列名表的）按名字认，认不全的整份遮
- 汇总里的 CASE / IIF 只在条件里用到遮罩列的（SUM(CASE WHEN buyer_phone IS NOT NULL THEN amount ELSE 0 END)），同
  写在 WHERE 里，不算；结果的某一支取的是遮罩列、不汇总的逐行 CASE 照旧算

数据都是虚构的：手机号、区域、金额。
"""
from __future__ import annotations

import pytest

from app.engine import masking

#: 设了遮罩的列：手机号，和装着手机号、姓名的 JSON 列
MASKED = {"buyer_phone", "profile"}
SALES = ["id", "region", "buyer_phone", "amount", "profile"]
#: SQLite 的 json_each / json_tree 出的列
JSON_EACH = ["key", "value", "type", "atom", "id", "parent", "fullkey", "path"]


def hidden_of(sql: str, columns: list[str], kind: str = "sqlite", masked: set[str] = MASKED) -> set[str]:
    snapshot = {"source": "shop", "sql": sql, "columns": columns, "rows": []}
    return masking._derived_hidden(snapshot, set(masked), kind)[0]


@pytest.mark.parametrize("kind, sql, columns, shown", [
    pytest.param("sqlite", "SELECT j.key, j.value FROM sales, json_each(sales.profile) AS j", ["key", "value"],
                 ["key", "value"], id="json_each 拆遮罩的 JSON 列"),
    pytest.param("sqlite", "SELECT t.atom AS 值 FROM sales, json_tree(sales.profile) AS t WHERE t.type = 'text'",
                 ["值"], ["值"], id="json_tree"),
    pytest.param("sqlite", "SELECT value FROM sales, json_each(json_array(buyer_phone))", ["value"], ["value"],
                 id="json_each 套 json_array"),
    pytest.param("sqlite", "SELECT r.value AS 号码 FROM sales s JOIN json_each(s.profile) r ON 1 = 1", ["号码"],
                 ["号码"], id="JOIN 里的 json_each"),
    pytest.param("sqlite", "SELECT value FROM json_each((SELECT json_group_array(buyer_phone) FROM sales))",
                 ["value"], ["value"], id="参数是子查询"),
    pytest.param("sqlite", "SELECT c.value AS 号码 FROM (SELECT json_group_array(buyer_phone) AS arr FROM sales) t, "
                 "json_each(t.arr) c", ["号码"], ["号码"], id="参数取自派生来源里改了名字的遮罩列"),
    pytest.param("sqlite", "SELECT * FROM sales, json_each(sales.profile)", [*SALES, *JSON_EACH],
                 ["buyer_phone", "profile", "key", "value", "atom", "fullkey", "path"], id="* 带出 json_each 的列"),
    pytest.param("sqlite", "SELECT x FROM (SELECT j.value AS x FROM sales, json_each(sales.profile) j) t", ["x"],
                 ["x"], id="子查询里拆开再改名"),
    pytest.param("postgres", "SELECT u FROM sales, unnest(ARRAY[buyer_phone]) AS u", ["u"], ["u"], id="unnest"),
    pytest.param("postgres", "SELECT x.p FROM sales s CROSS JOIN LATERAL unnest(ARRAY[s.buyer_phone]) AS x(p)",
                 ["p"], ["p"], id="LATERAL unnest 带列名表"),
    pytest.param("postgres", "SELECT x FROM sales, LATERAL unnest(ARRAY[buyer_phone]) AS x", ["x"], ["x"],
                 id="LATERAL unnest"),
    pytest.param("postgres", "SELECT u FROM sales CROSS JOIN LATERAL (VALUES (buyer_phone)) AS v(u)", ["u"], ["u"],
                 id="LATERAL VALUES"),
    pytest.param("postgres", "SELECT d FROM sales, regexp_split_to_table(buyer_phone, '-') AS d", ["d"], ["d"],
                 id="regexp_split_to_table"),
    pytest.param("postgres", "SELECT e.key, e.value FROM sales, jsonb_each_text(profile) AS e", ["key", "value"],
                 ["key", "value"], id="jsonb_each_text"),
    pytest.param("postgres", "SELECT x FROM sales, unnest(string_to_array(buyer_phone, ',')) AS x", ["x"], ["x"],
                 id="unnest 套 string_to_array"),
    pytest.param("postgres", "SELECT * FROM sales, unnest(ARRAY[buyer_phone])", [*SALES, "unnest"],
                 ["buyer_phone", "profile", "unnest"], id="没起别名的 unnest 按函数名出列"),
    pytest.param("mysql", "SELECT j.p FROM sales s, JSON_TABLE(JSON_ARRAY(s.buyer_phone), '$[*]' "
                 "COLUMNS (p VARCHAR(20) PATH '$')) j", ["p"], ["p"], id="JSON_TABLE"),
])
def test_a_table_function_over_a_masked_column_hides_what_it_returns(kind, sql, columns, shown):
    assert {c.lower() for c in shown} <= hidden_of(sql, columns, kind)


def test_columns_a_table_function_does_not_return_stay_visible():
    """拆的是遮罩列，别的列（区域、金额）照常；拆的不是遮罩列的，一列都不遮。"""
    hidden = hidden_of("SELECT s.region, s.amount, j.value AS 值 FROM sales s, json_each(s.profile) j",
                       ["region", "amount", "值"])
    assert "值" in hidden and not {"region", "amount"} & hidden
    assert hidden_of("SELECT s.region, j.value AS 标签 FROM sales s, json_each(s.tags) j", ["region", "标签"]) == MASKED


def test_a_table_function_that_cannot_be_read_hides_the_whole_snapshot():
    """不认得的函数拆遮罩列、又没写列名表：说不清出哪几列，整份快照的列都遮（同 SQL 解析不了）。"""
    columns = ["region", "piece"]
    assert hidden_of("SELECT s.region, t.piece FROM sales s, my_split(s.buyer_phone) t", columns, "postgres") == {
        *MASKED, "region", "piece"}


@pytest.mark.parametrize("sql", [
    "SELECT SUM(CASE WHEN buyer_phone IS NOT NULL THEN amount ELSE 0 END) AS total FROM sales",
    "SELECT SUM(CASE buyer_phone WHEN '13900000001' THEN amount END) AS total FROM sales",
    "SELECT AVG(IIF(buyer_phone LIKE '139%', amount, NULL)) AS total FROM sales",
    "SELECT MAX(CASE WHEN buyer_phone IS NULL THEN 0 ELSE amount END) AS total FROM sales",
    "SELECT SUM(CASE WHEN buyer_phone IS NULL THEN 0 ELSE CASE WHEN region = '华东' THEN amount END END) AS total "
    "FROM sales",
])
def test_a_masked_column_only_in_the_condition_of_an_aggregated_case_is_like_a_filter(sql):
    """值全部来自金额，手机号只决定这一行算不算进来：同写在 WHERE 里，不算取自遮罩列。"""
    assert hidden_of(sql, ["total"]) == MASKED
    assert hidden_of(sql.replace("CASE WHEN buyer_phone IS NOT NULL THEN amount ELSE 0 END", "amount")
                     + " WHERE buyer_phone IS NOT NULL", ["total"]) == MASKED


@pytest.mark.parametrize("sql", [
    # 结果的某一支取的就是遮罩列
    "SELECT SUM(CASE WHEN region = '华东' THEN buyer_phone END) AS total FROM sales",
    "SELECT MAX(IIF(amount > 1000, buyer_phone, NULL)) AS total FROM sales",
    # 不汇总的逐行 CASE：每一行各自落在哪一支
    "SELECT CASE WHEN buyer_phone IS NULL THEN 0 ELSE amount END AS total FROM sales",
])
def test_a_case_returning_the_masked_value_or_not_aggregated_is_still_derived(sql):
    assert "total" in hidden_of(sql, ["total"])
