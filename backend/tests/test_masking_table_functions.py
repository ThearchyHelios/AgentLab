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


# --------------------------------------------------------------------------
# 函数参数里的 JSON 箭头（复核第 2 轮）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind, sql, columns, shown", [
    pytest.param("sqlite", "SELECT j.value AS v FROM sales, json_each(profile->'$.tels') AS j", ["v"], ["v"],
                 id="SQLite json_each 参数带箭头"),
    pytest.param("postgres", "SELECT e AS 号 FROM sales, json_array_elements_text(profile->'tels') AS e", ["号"],
                 ["号"], id="Postgres json_array_elements_text"),
    pytest.param("postgres", "SELECT e.value AS 号 FROM sales, jsonb_array_elements(profile->'tels') AS e", ["号"],
                 ["号"], id="Postgres jsonb_array_elements"),
    pytest.param("postgres", "SELECT t.tel FROM sales, jsonb_to_recordset(profile->'contacts') AS t(tel text)",
                 ["tel"], ["tel"], id="Postgres jsonb_to_recordset 列名表"),
    pytest.param("postgres", "SELECT k, v FROM sales, jsonb_each_text(profile->'contact') AS x(k, v)", ["k", "v"],
                 ["k", "v"], id="Postgres jsonb_each_text 列名表"),
    pytest.param("postgres", "SELECT jsonb_array_elements_text(profile->'tels') AS 号 FROM sales", ["号"], ["号"],
                 id="Postgres 选出项里拆开"),
    pytest.param("postgres", "SELECT jsonb_array_elements_text(profile -> 'contact' -> 'tels') AS 号 FROM sales",
                 ["号"], ["号"], id="Postgres 连着两个箭头"),
    pytest.param("mysql", "SELECT JSON_UNQUOTE(profile->'$.tel') AS 电话 FROM sales", ["电话"], ["电话"],
                 id="MySQL JSON_UNQUOTE"),
    pytest.param("mysql", "SELECT CONCAT(profile->'$.tel', '') AS 电话 FROM sales", ["电话"], ["电话"],
                 id="MySQL CONCAT"),
])
def test_a_json_arrow_inside_function_arguments_is_read_as_the_masked_column(kind, sql, columns, shown):
    """函数参数里没写表名的 col->'path' 被解析器当成以 col 为参数的 lambda（不是一列）：以前认不出遮罩列，拆开、取出来的
    值原样交给模型、落库。遮罩列是 JSON 时这几种是最自然的写法。写了表名、用 ->> 的本来就认得出。"""
    assert {c.lower() for c in shown} <= hidden_of(sql, columns, kind)


def test_a_json_arrow_over_an_unmasked_column_stays_visible():
    """箭头取的不是遮罩列：一列都不遮。真的 lambda 的参数照列名收，只有和遮罩列同名时才多遮。"""
    assert hidden_of("SELECT j.value AS 标签 FROM sales, json_each(tags->'$.list') AS j", ["标签"]) == MASKED
    assert hidden_of("SELECT transform(scores, x -> x + 1) AS 分 FROM sales", ["分"], "postgres") == MASKED


@pytest.mark.parametrize("sql, columns, shown", [
    pytest.param("SELECT t.profile.tel AS 电话 FROM sales t", ["电话"], ["电话"], id="JSON 点号"),
    pytest.param("SELECT t.profile.contact.tel FROM sales t", ["tel"], ["tel"], id="JSON 点号两层"),
    pytest.param("SELECT UPPER(t.profile.tel) AS 电话, t.region FROM sales t", ["电话", "region"], ["电话"],
                 id="套函数"),
    pytest.param("SELECT x.电话 FROM (SELECT t.profile.tel AS 电话 FROM sales t) x", ["电话"], ["电话"],
                 id="子查询里取出再选"),
])
def test_oracle_dot_notation_into_a_masked_json_column_is_read_as_that_column(sql, columns, shown):
    """Oracle 的 JSON 点号、对象类型的属性写法（t.profile.tel）：解析器读成「库 t、表 profile、列 tel」，以前只认 tel，
    取出来的号码原样交给模型、落库。中间几段也算列（多遮：写了库名、表名的列，表名碰巧和遮罩列同名时也算）。"""
    hidden = hidden_of(sql, columns, "oracle")
    assert {c.lower() for c in shown} <= hidden and "region" not in hidden


def test_oracle_dot_notation_into_an_unmasked_column_stays_visible():
    """点号取的不是遮罩列、写了库名表名的普通列：一列都不遮。"""
    assert hidden_of("SELECT t.tags.name AS 标签 FROM sales t", ["标签"], "oracle") == MASKED
    assert hidden_of("SELECT shop.sales.region FROM shop.sales", ["region"], "oracle") == MASKED


def test_a_json_arrow_inside_json_each_is_masked_when_really_run():
    """SQLite 里真跑一遍：json_each(profile->'$.tels') 拆出来的号码在交给模型之前就遮掉。"""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE sales (id INTEGER, profile TEXT);"
                     "INSERT INTO sales VALUES (1, '{\"tels\": [\"13900000001\", \"13900000002\"]}');")
    sql = "SELECT j.value AS v FROM sales, json_each(profile->'$.tels') AS j"
    cursor = db.execute(sql)
    payload = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()], "sql": sql}
    db.close()
    done = masking.mask_query_result(payload, ["profile"], "sqlite")
    assert done.columns == ["v"] and done.payload["rows"] == [["已遮罩"], ["已遮罩"]]


# --------------------------------------------------------------------------
# 交回复合类型的表值函数（K3b）：Postgres 把复合类型展开成它的各个字段
# --------------------------------------------------------------------------


#: people3.card 是设了遮罩的复合类型（addr, tel），people2.pj 是设了遮罩的 jsonb（照着 contact 类型拆），sales 是订单
COMPOSITE_SCHEMA = {"tables": {
    "people3": {"qualified": "people3", "schema": None, "columns": [{"name": c} for c in ["id", "card", "city"]]},
    "people2": {"qualified": "people2", "schema": None, "columns": [{"name": c} for c in ["id", "pj"]]},
    "sales": {"qualified": "sales", "schema": None, "columns": [{"name": c} for c in SALES]},
}}
COMPOSITE_MASKED = ["card", "pj", "buyer_phone"]


def _boundary(sql: str, columns: list[str], row: list) -> masking.QueryMask:
    payload = {"columns": columns, "rows": [list(row)], "sql": sql}
    return masking.mask_query_result(payload, COMPOSITE_MASKED, "postgresql", schema=COMPOSITE_SCHEMA)


#: (SQL, 结果列, 一行)：结果的样子照 Postgres 18 实测。unnest 的数组元素是复合类型时，FROM 里的 unnest 展开成复合类型的
#: 各个字段（addr、tel），写了列名表也只改前几列的名字；交回复合类型的函数（jsonb_populate_record(NULL::contact, …)）
#: 写了只有名字、没写类型的列名表时同样只改前几列，后面的字段照原名出
COMPOSITE_RESULTS = [
    pytest.param("SELECT v.* FROM people3 p, unnest(ARRAY[p.card]) v", ["addr", "tel"], ["某路 1 号", "13900000001"],
                 id="unnest 复合类型的数组"),
    pytest.param("SELECT * FROM unnest(ARRAY[(SELECT card FROM people3 WHERE id = 1)])", ["addr", "tel"],
                 ["某路 1 号", "13900000001"], id="参数是子查询"),
    pytest.param("SELECT v.tel FROM people3 p, LATERAL unnest(ARRAY[p.card]) v", ["tel"], ["13900000001"],
                 id="LATERAL 取一个字段"),
    pytest.param("SELECT * FROM people3 p, unnest(ARRAY[p.card]) AS u(a)", ["id", "card", "city", "a", "tel"],
                 [1, "(某路 1 号,13900000001)", "杭州", "某路 1 号", "13900000001"], id="列名表比字段少"),
    pytest.param("SELECT u.* FROM people3 p, unnest(ARRAY[p.card], ARRAY[p.city]) AS u", ["addr", "tel", "unnest"],
                 ["某路 1 号", "13900000001", "杭州"], id="unnest 几个数组"),
    pytest.param("SELECT u.* FROM people3 p, unnest(ARRAY[p.card]) WITH ORDINALITY AS u(a)",
                 ["a", "tel", "ordinality"], ["某路 1 号", "13900000001", 1], id="WITH ORDINALITY"),
    pytest.param("SELECT * FROM (SELECT p.card FROM people3 p) s, LATERAL unnest(ARRAY[s.card]) WITH ORDINALITY u",
                 ["card", "addr", "tel", "ordinality"], ["(某路 1 号,13900000001)", "某路 1 号", "13900000001", 1],
                 id="参数取自派生来源"),
    pytest.param("SELECT r.* FROM people2 p, jsonb_populate_record(NULL::contact, p.pj) AS r(a)", ["a", "tel"],
                 ["丙路", "13900000021"], id="populate_record 列名表比字段少"),
    pytest.param("SELECT r.* FROM people2 p, LATERAL jsonb_populate_recordset(NULL::contact, jsonb_build_array(p.pj)) "
                 "AS r(a)", ["a", "tel"], ["丙路", "13900000021"], id="populate_recordset 列名表比字段少"),
]


@pytest.mark.parametrize("sql, columns, row", COMPOSITE_RESULTS)
def test_a_table_function_spreading_a_composite_masks_the_whole_result(sql, columns, row):
    """FROM 里的函数交回复合类型、读到遮罩列：展开成哪几个字段、各叫什么 SQL 里看不出（同 (表达式).*），整份遮。以前
    unnest 按「出一列，名字是别名或函数名」认，列名表按写全了认，tel 这一列的号码原样交给模型、落库。"""
    done = _boundary(sql, columns, row)
    assert done.whole and done.columns == columns
    assert done.payload["rows"] == [["已遮罩"] * len(columns)]


@pytest.mark.parametrize("sql, columns, row, hidden", [
    pytest.param("SELECT r.* FROM people2 p, jsonb_to_record(p.pj) AS r(tel text, addr text)", ["tel", "addr"],
                 ["13900000021", "丙路"], ["tel", "addr"], id="写了带类型的列定义"),
    pytest.param("SELECT k, v FROM people2 p, jsonb_each_text(p.pj) AS x(k, v)", ["k", "v"], ["tel", "13900000021"],
                 ["k", "v"], id="出列认得全的函数写了列名表"),
    pytest.param("SELECT s.id, u FROM sales s, unnest(ARRAY[s.region]) AS u", ["id", "u"], [1, "华东"], [],
                 id="unnest 没碰遮罩列"),
])
def test_a_table_function_whose_columns_are_spelled_out_is_read_as_before(sql, columns, row, hidden):
    """列写全了的（带类型的列定义、出列认得全的函数）照旧按名字认，没碰遮罩列的照常。"""
    done = _boundary(sql, columns, row)
    assert not done.whole and done.columns == hidden
    assert done.payload["rows"] == [["已遮罩" if c in hidden else v for c, v in zip(columns, row)]]
