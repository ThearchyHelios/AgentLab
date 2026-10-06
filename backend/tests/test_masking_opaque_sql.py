"""认不全的 SQL 写法整份遮（engine/masking.py 的 _tainted），不再逐个形状补规则（K3 第 3 轮复验留下的几处）：

- (表达式).* 展开：复合类型的列 (p.profile).*、函数交回的记录 (jsonb_each_text(pj)).*、(jsonb_populate_record(…)).*、
  子查询里的匿名 ROW (q.r).*。展开成几列、各叫什么 SQL 里看不出，表达式读到遮罩列的，整份结果都遮——最外层、子查询、
  CTE、LATERAL 里都一样。以前改写成一个对不上来源的 *，最外层、没写列名表的派生来源里按「照原名带出」认，字段名
  （tel、key/value、f1/f2）和遮罩列对不上，号码原样交给模型、落库。(s).*、s 是 FROM 里的来源时照旧按 s.* 认
- 表名、SQL 写在字符串里交给函数执行、整表导出的（query_to_xml、table_to_xml、cursor_to_xml、schema_to_xml、
  database_to_xml、dblink，Oracle 的 DBMS_XMLGEN），统计信息里存着的值（pg_stats）：整份遮，解析不了的也一样
- LATERAL 看不见它自己：里面的整行引用 s 不因为 LATERAL 自己的结果列叫 s 就被当成列
- 分析不过来、分析出了意外：整份遮。派生来源对应的那块查询、一项的值取自哪些列在一次分析里记下来，一长串 CTE 一段套
  一段地整行引用（250 段，2 万字的上限里写得下）以前要二十几秒，现在零点几秒；超出工作量上限（ANALYSIS_STEPS）的整份遮

这里没有真的 Postgres：结果按它交回的样子造（复合类型、记录展开成几列）。数据都是虚构的：手机号、地址、区域。
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine import masking
from app.main import app
from tests.test_verify_agent import Script, q
from tests.test_verify_agent_masking import ask, clean_spend  # noqa: F401 - clean_spend 是自动用上的夹具

MASKED_WORD = "已遮罩"
#: people.profile 是复合类型（tel、addr），people2.pj 是 JSON（tel、addr），sales 是订单：设了遮罩的是 profile、pj、buyer_phone
SCHEMA = {"tables": {
    "people": {"qualified": "people", "schema": None, "columns": [{"name": c} for c in ["id", "profile", "address"]]},
    "people2": {"qualified": "people2", "schema": None, "columns": [{"name": c} for c in ["id", "pj", "city"]]},
    "sales": {"qualified": "sales", "schema": None,
              "columns": [{"name": c} for c in ["id", "region", "buyer_phone", "amount"]]},
}}
MASKED = ["profile", "pj", "buyer_phone"]
PHONE = "13900000001"


def _leaks(done: masking.QueryMask) -> bool:
    return "1390000000" in json.dumps(done.payload, ensure_ascii=False)


def masked(sql: str, columns: list[str], rows: list[list], kind: str = "postgresql") -> masking.QueryMask:
    payload = {"columns": columns, "rows": rows, "sql": sql}
    return masking.mask_query_result(payload, MASKED, kind, schema=SCHEMA)


# --------------------------------------------------------------------------
# (表达式).* 展开
# --------------------------------------------------------------------------


#: 三种展开（复合类型的列、函数交回的记录、子查询里的匿名 ROW）× 四个位置（最外层、子查询、CTE、LATERAL）：
#: (SQL, 结果列, 一行)
EXPANSIONS = [
    pytest.param("SELECT p.id AS n, (p.profile).* FROM people p", ["n", "tel", "addr"], [1, PHONE, "某路 1 号"],
                 id="复合类型-最外层"),
    pytest.param("SELECT x.* FROM (SELECT p.id AS n, (p.profile).* FROM people p) x", ["n", "tel", "addr"],
                 [1, PHONE, "某路 1 号"], id="复合类型-子查询"),
    pytest.param("WITH x AS (SELECT p.id AS n, (p.profile).* FROM people p) SELECT * FROM x", ["n", "tel", "addr"],
                 [1, PHONE, "某路 1 号"], id="复合类型-CTE"),
    pytest.param("SELECT x.* FROM people p, LATERAL (SELECT p.id AS n, (p.profile).*) x", ["n", "tel", "addr"],
                 [1, PHONE, "某路 1 号"], id="复合类型-LATERAL"),
    pytest.param("SELECT (jsonb_each_text(pj)).* FROM people2", ["key", "value"], ["tel", PHONE],
                 id="记录函数-最外层"),
    pytest.param("SELECT kv.value AS tel FROM (SELECT (jsonb_each_text(pj)).* FROM people2) kv WHERE kv.key = 'tel'",
                 ["tel"], [PHONE], id="记录函数-子查询"),
    pytest.param("WITH kv AS (SELECT (jsonb_each_text(pj)).* FROM people2) SELECT value AS 号码 FROM kv "
                 "WHERE key = 'tel'", ["号码"], [PHONE], id="记录函数-CTE"),
    pytest.param("SELECT x.value FROM people2 p, LATERAL (SELECT (jsonb_each_text(p.pj)).*) x", ["value"], [PHONE],
                 id="记录函数-LATERAL"),
    pytest.param("SELECT (q.r).* FROM (SELECT ROW(buyer_phone, region) AS r FROM sales) q", ["f1", "f2"],
                 [PHONE, "华东"], id="匿名 ROW-最外层"),
    pytest.param("SELECT y.* FROM (SELECT (q.r).* FROM (SELECT ROW(buyer_phone, region) AS r FROM sales) q) y",
                 ["f1", "f2"], [PHONE, "华东"], id="匿名 ROW-子查询"),
    pytest.param("WITH q AS (SELECT ROW(buyer_phone, region) AS r FROM sales) SELECT (q.r).* FROM q", ["f1", "f2"],
                 [PHONE, "华东"], id="匿名 ROW-CTE"),
    pytest.param("SELECT x.* FROM (SELECT ROW(buyer_phone, region) AS r FROM sales) q, LATERAL (SELECT (q.r).*) x",
                 ["f1", "f2"], [PHONE, "华东"], id="匿名 ROW-LATERAL"),
    pytest.param("SELECT p.id AS n, (jsonb_populate_record(NULL::contact, p.pj)).* FROM people2 p",
                 ["n", "tel", "addr"], [1, PHONE, "某路 1 号"], id="按类型展开 JSON"),
]


@pytest.mark.parametrize("sql, columns, row", EXPANSIONS)
def test_an_expression_star_reading_a_masked_column_masks_the_whole_result(sql, columns, row):
    """(表达式).* 读到遮罩列：展开成几列、叫什么说不清，整份结果的每一列都遮（同一份里的编号也遮，多遮）。"""
    done = masked(sql, columns, [list(row)])
    assert done.columns == columns
    assert done.payload["rows"] == [[MASKED_WORD] * len(columns)] and not _leaks(done)


@pytest.mark.parametrize("sql, columns, row", [
    pytest.param("SELECT p.id AS n, (p.address).* FROM people p", ["n", "street", "city"], [1, "某路", "某市"],
                 id="没设遮罩的复合类型"),
    pytest.param("SELECT (jsonb_each_text(to_jsonb(c))).* FROM (SELECT id, region FROM sales) c", ["key", "value"],
                 ["region", "华东"], id="记录里没有遮罩列"),
])
def test_an_expression_star_that_reads_no_masked_column_stays_visible(sql, columns, row):
    done = masked(sql, columns, [list(row)])
    assert done.columns == [] and done.payload["rows"] == [list(row)]


def test_a_parenthesised_source_star_is_still_read_as_its_columns():
    """(s).*、s 是 FROM 里的来源：照旧当 s.* 认，只遮号码那一列，区域、金额照常。"""
    done = masked("SELECT (s).* FROM sales s", ["id", "region", "buyer_phone", "amount"], [[1, "华东", PHONE, 1200]])
    assert done.columns == ["buyer_phone"] and done.payload["rows"] == [[1, "华东", MASKED_WORD, 1200]]


#: 造出来的 Postgres 数据源：表结构同上，查询交给换掉的 run_query（不连库）
@pytest.fixture
async def composite_shop():
    made: list[int] = []

    async def make() -> str:
        name = f"pg_{uuid.uuid4().hex[:8]}"
        async with SessionLocal() as session:
            row = DataSource(name=name, kind="postgresql", host="127.0.0.1", port=5432, username="probe",
                             database="shop", readonly=True, enabled=True,
                             options={"mask_columns": ", ".join(MASKED)}, schema_cache=SCHEMA)
            session.add(row)
            await session.commit()
            made.append(row.id)
        return name

    yield make
    for source_id in made:
        await engines.invalidate(source_id)


@pytest.mark.parametrize("sql, columns, rows", [
    ("SELECT p.id AS n, (p.profile).* FROM people p", ["n", "tel", "addr"], [[1, PHONE, "某路 1 号"]]),
    ("SELECT x.* FROM (SELECT p.id AS n, (p.profile).* FROM people p) x", ["n", "tel", "addr"],
     [[1, PHONE, "某路 1 号"]]),
    ("SELECT p.id AS n, (jsonb_populate_record(NULL::contact, p.pj)).* FROM people2 p", ["n", "tel", "addr"],
     [[1, PHONE, "某路 1 号"]]),
], ids=["复合类型", "子查询里的复合类型", "按类型展开 JSON"])
async def test_an_expression_star_never_reaches_the_model_or_the_store(monkeypatch, composite_shop, sql, columns,
                                                                       rows):
    """端到端（核查 agent 的真实查询工具）：交给模型的、落库的、通用工件接口给的都只有「已遮罩」，masked_at_query 是
    全部的列。以前三条的 masked_at_query 都是空的，号码原样交给模型、落库。"""
    from app.data.engine import QueryResult
    from app.tools import datasource

    async def driver(source, statement, **kwargs):
        assert statement == sql
        return QueryResult(columns=list(columns), rows=[list(r) for r in rows], row_count=len(rows), truncated=False,
                           elapsed_ms=1, sql=statement)

    monkeypatch.setattr(datasource, "run_query", driver)
    name = await composite_shop()
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "n"},
                                                        "rationale": "x"})
    out, _ = await ask(name)
    seen = script.tool_results()["c1"]
    assert MASKED_WORD in seen and "1390000000" not in seen, seen
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == columns and "1390000000" not in json.dumps(snapshot, ensure_ascii=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
    assert got.status_code == 200 and "1390000000" not in got.text


# --------------------------------------------------------------------------
# LATERAL 看不见它自己
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, columns, row", [
    pytest.param("SELECT y.* FROM sales t2, LATERAL (SELECT s FROM sales s LIMIT 1) y", ["s"],
                 [f"(1,华东,{PHONE},1200)"], id="选出整行"),
    pytest.param("SELECT y.j FROM sales t2 CROSS JOIN LATERAL (SELECT 1 AS s, row_to_json(s) AS j FROM sales s "
                 "LIMIT 1) y", ["j"], [json.dumps({"id": 1, "buyer_phone": PHONE})], id="结果列也叫 s"),
    pytest.param("SELECT y.j FROM sales t2, LATERAL (SELECT s.id AS s, row_to_json(s) AS j FROM sales s LIMIT 1) y",
                 ["j"], [json.dumps({"id": 1, "buyer_phone": PHONE})], id="编号起名叫 s"),
])
def test_a_whole_row_inside_a_lateral_is_not_taken_for_its_own_column(sql, columns, row):
    """LATERAL 里的 s 是它自己 FROM 里的整行：LATERAL 的结果列碰巧叫 s，也不把它当成列（Postgres 里 LATERAL 看不见
    自己）。以前当成了列，整行原样交了出去。"""
    done = masked(sql, columns, [list(row)])
    assert done.columns == columns and not _leaks(done)


def test_a_lateral_still_sees_the_sources_before_it():
    """排在 LATERAL 前面的来源照样看得见：前面的表有一列叫 s 时，s 是那一列（没设遮罩），不是整行。"""
    schema = {"tables": {**SCHEMA["tables"], "codes": {"qualified": "codes", "schema": None,
                                                       "columns": [{"name": "id"}, {"name": "s"}]}}}
    payload = {"columns": ["v"], "rows": [["A1"]],
               "sql": "SELECT y.v FROM codes c, LATERAL (SELECT s AS v FROM sales x WHERE x.id = c.id) y"}
    done = masking.mask_query_result(payload, MASKED, "postgresql", schema=schema)
    assert done.columns == [] and done.payload["rows"] == [["A1"]]


# --------------------------------------------------------------------------
# 表名、SQL 写在字符串里交给函数，统计信息里的值
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT query_to_xml('select * from sales', true, false, '') AS x",
    "SELECT table_to_xml('sales', true, false, '') AS x",
    "SELECT query_to_xml_and_xmlschema('select buyer_phone from sales', true, false, '') AS x",
    "SELECT cursor_to_xml('c1', 10, false, true, '') AS x",
    "SELECT schema_to_xml('public', true, false, '') AS x",
    "SELECT database_to_xml(true, false, '') AS x",
    "SELECT x FROM dblink('dbname=shop', 'select buyer_phone from sales') AS t(x text)",
    "SELECT DBMS_XMLGEN.GETXML('select * from sales') AS x FROM dual",
    "SELECT array_to_string(most_common_vals, ',') AS x FROM pg_stats WHERE tablename = 'sales'",
])
def test_tables_and_sql_handed_to_functions_as_text_mask_the_whole_result(sql):
    """表名、SQL 写在字符串里交给函数执行、整表导出，统计信息里存着的值：读的是哪一列认不出，整份遮。以前 masked=[]，
    号码原样。"""
    done = masked(sql, ["x"], [[f"<row><buyer_phone>{PHONE}</buyer_phone></row>"]], kind="oracle"
                  if "DBMS" in sql else "postgresql")
    assert done.columns == ["x"] and not _leaks(done)


def test_a_table_handed_to_a_function_masks_the_whole_result_even_when_the_sql_cannot_be_read():
    sql = "SELECT table_to_xml('sales', true, false, '') AS x WHERE ("
    hidden, _ = masking._derived_hidden({"sql": sql, "columns": ["x"], "rows": []}, {"buyer_phone"}, "postgresql")
    assert "x" in hidden


# --------------------------------------------------------------------------
# 分析不过来、分析出了意外：整份遮；一长串 CTE 不再卡住
# --------------------------------------------------------------------------


def _chain(k: int, tail: str) -> str:
    """一长串 CTE，一段套一段地整行引用、展开上一段（复验时 250 段要二十几秒）。"""
    parts = ["c0 AS (SELECT * FROM sales)"]
    parts += [f"c{i} AS (SELECT row_to_json(c{i - 1}) AS j, c{i - 1}.* FROM c{i - 1})" for i in range(1, k)]
    return "WITH " + ", ".join(parts) + f" {tail.format(last=f'c{k - 1}')}"


def _plain_chain(k: int) -> str:
    parts = ["c0 AS (SELECT id, region, amount FROM sales)"]
    parts += [f"c{i} AS (SELECT id, region, amount + {i} AS amount FROM c{i - 1})" for i in range(1, k)]
    return "WITH " + ", ".join(parts) + f" SELECT region, SUM(amount) AS total FROM c{k - 1} GROUP BY region"


def test_a_long_chain_of_whole_rows_is_read_quickly_and_completely():
    """250 段一段套一段地整行引用：零点几秒认完（以前二十几秒），不靠工作量上限兜底——只数个数的结果照常不遮。"""
    columns = masking._schema_columns(SCHEMA)
    sql = _chain(250, "SELECT COUNT(*) AS n FROM {last}")
    assert len(sql) < 20_000
    started = time.perf_counter()
    hidden, _ = masking._derived_hidden({"sql": sql, "columns": ["n"], "rows": []}, {"buyer_phone"}, "postgresql",
                                        columns)
    assert time.perf_counter() - started < 5
    assert "n" not in hidden
    hidden, _ = masking._derived_hidden({"sql": _chain(250, "SELECT j FROM {last}"), "columns": ["j"], "rows": []},
                                        {"buyer_phone"}, "postgresql", columns)
    assert "j" in hidden


def test_an_analysis_over_the_work_limit_masks_the_whole_result(monkeypatch):
    """超出工作量上限：不再分析，整份遮（结果本来没碰遮罩列也一样）。没超的照常。"""
    sql = _plain_chain(40)
    snapshot = {"sql": sql, "columns": ["region", "total"], "rows": []}
    assert masking._derived_hidden(snapshot, {"buyer_phone"}, "postgresql")[0] == {"buyer_phone"}
    monkeypatch.setattr(masking, "ANALYSIS_STEPS", 50)
    assert masking._derived_hidden(snapshot, {"buyer_phone"}, "postgresql")[0] == {"buyer_phone", "region", "total"}
    payload = {"sql": sql, "columns": ["region", "total"], "rows": [["华东", 1200]]}
    done = masking.mask_query_result(payload, ["buyer_phone"], "postgresql")
    assert done.columns == ["region", "total"] and done.payload["rows"] == [[MASKED_WORD, MASKED_WORD]]


def test_a_failing_analysis_masks_the_whole_result_instead_of_reading_the_text(monkeypatch):
    """解析之后的分析出了意外：整份遮。以前按「解析不了」只看文字，没写遮罩列、没有 * 的整行引用（SELECT s）就放过去了。"""
    def broken(*args, **kwargs):
        raise RuntimeError("分析里的意外")

    monkeypatch.setattr(masking, "_tainted_in", broken)
    snapshot = {"sql": "SELECT s AS r FROM sales s", "columns": ["r"], "rows": []}
    assert "r" in masking._derived_hidden(snapshot, {"buyer_phone"}, "postgresql")[0]
