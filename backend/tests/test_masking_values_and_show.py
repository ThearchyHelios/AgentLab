"""认不全的写法整份遮，K3b 复核第 2 轮补的两类（engine/masking.py 的 _tainted_in、_opaque）：

- VALUES 里的 (表达式).*：Postgres 在 VALUES 里同样把复合类型的值、记录展开成好几列（文档：选出列表、VALUES、行构造器
  里的 composite.* 都展开），LATERAL VALUES 按表值函数认时一个元组项只算一列，column2 起的号码原样交给模型、落库。现在
  树上任何位置的 (表达式).* 读到遮罩列，整份结果都遮——LATERAL、JOIN LATERAL、CTE、FROM 里的 VALUES 都一样
- 记着别的会话的 SQL 原文、被锁那一行的值的来源：MySQL 的 information_schema.INNODB_TRX、OPTIMIZER_TRACE，MariaDB 的
  INNODB_LOCKS、QUERY_CACHE_INFO，Oracle 的 SQL 调优集、固定下来的执行计划、闪回事务、LogMiner；不只读表结构的 SHOW
  （ENGINE INNODB STATUS、BINLOG / RELAYLOG EVENTS、REPLICA STATUS、WARNINGS、CREATE TABLE / VIEW……）和 EXPLAIN 一条
  语句。以前工具边界的名单里没有，可验证报告那份整个挡 information_schema、不附带解析不了的快照，反而挡得住

这里没有真的数据库：结果按它交回的样子造（复合类型展开成几列、SHOW 交回的几列，照 Postgres 18、MySQL 8.0 实测的样子）。
数据都是虚构的：手机号、地址、区域。
"""
from __future__ import annotations

import json
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
#: people3.card 是复合类型（addr, tel），people.address 是没设遮罩的复合类型（street, city），sales 是订单。设了遮罩的是
#: card、buyer_phone
SCHEMA = {"tables": {
    "people3": {"qualified": "people3", "schema": None, "columns": [{"name": c} for c in ["id", "card", "city"]]},
    "people": {"qualified": "people", "schema": None, "columns": [{"name": c} for c in ["id", "address"]]},
    "sales": {"qualified": "sales", "schema": None,
              "columns": [{"name": c} for c in ["id", "region", "buyer_phone", "amount"]]},
}}
MASKED = ["card", "buyer_phone"]
PHONE = "13900000001"


def _leaks(done: masking.QueryMask) -> bool:
    return "1390000000" in json.dumps(done.payload, ensure_ascii=False)


def masked(sql: str, columns: list[str], rows: list[list], kind: str = "postgresql") -> masking.QueryMask:
    payload = {"columns": columns, "rows": rows, "sql": sql}
    return masking.mask_query_result(payload, MASKED, kind, schema=SCHEMA)


# --------------------------------------------------------------------------
# VALUES 里的 (表达式).*
# --------------------------------------------------------------------------


#: 复合类型的列、子查询交回的复合值、匿名 ROW、整行 × LATERAL、JOIN LATERAL、CTE、FROM 里的 VALUES：(SQL, 结果列, 一行)。
#: 号码都在第二列起：以前 LATERAL VALUES 一个元组项只算 column1，column2 起的号码原样交了出去
VALUES_EXPANSIONS = [
    pytest.param("SELECT v.* FROM people3 p, LATERAL (VALUES ((p.card).*)) v", ["column1", "column2"],
                 ["某路 1 号", PHONE], id="LATERAL-取 *"),
    pytest.param("SELECT v.column2 AS c FROM people3 p, LATERAL (VALUES ((p.card).*)) v", ["c"], [PHONE],
                 id="LATERAL-取第二列"),
    pytest.param("SELECT x.column2 FROM people3 p JOIN LATERAL (VALUES ((p.card).*)) x ON true", ["column2"], [PHONE],
                 id="JOIN LATERAL"),
    pytest.param("WITH v AS (VALUES ((SELECT card FROM people3 WHERE id = 1).*)) SELECT * FROM v",
                 ["column1", "column2"], ["某路 1 号", PHONE], id="CTE 里的 VALUES"),
    pytest.param("SELECT t FROM (VALUES ((SELECT card FROM people3 WHERE id = 1).*)) v(a, t)", ["t"], [PHONE],
                 id="FROM 里的 VALUES-列名表"),
    pytest.param("SELECT v.column2 FROM sales s, LATERAL (VALUES ((ROW(s.region, s.buyer_phone)).*)) v", ["column2"],
                 [PHONE], id="匿名 ROW"),
    pytest.param("SELECT v.* FROM sales s, LATERAL (VALUES ((s).*)) v", ["column1", "column2", "column3", "column4"],
                 [1, "华东", PHONE, 1200], id="整行"),
    pytest.param("SELECT v.column3 FROM people3 p, LATERAL (VALUES (p.id, (p.card).*)) v", ["column3"], [PHONE],
                 id="和别的项写在一个元组里"),
    # VALUES 里的 s.*、(s.*) 同样展开成好几列（s 是表，或者取了遮罩列的子查询）
    pytest.param("SELECT v.* FROM sales s, LATERAL (VALUES (s.*)) v", ["column1", "column2", "column3", "column4"],
                 [1, "华东", PHONE, 1200], id="表的 *"),
    pytest.param("SELECT v.column3 FROM sales s, LATERAL (VALUES ((s.*))) v", ["column3"], [PHONE],
                 id="套了括号的表的 *"),
    pytest.param("SELECT v.column2 FROM (SELECT id, buyer_phone AS p FROM sales) q, LATERAL (VALUES (q.*)) v",
                 ["column2"], [PHONE], id="子查询的 *"),
]


@pytest.mark.parametrize("sql, columns, row", VALUES_EXPANSIONS)
def test_an_expression_star_inside_values_masks_the_whole_result(sql, columns, row):
    """VALUES 里的 (表达式).* 读到遮罩列：展开成几列说不清，整份结果的每一格都遮。以前只遮了 column1（或一列不遮），
    号码原样交给模型、落库。"""
    done = masked(sql, columns, [list(row)])
    assert done.whole and done.columns == columns
    assert done.payload["rows"] == [[MASKED_WORD] * len(columns)] and not _leaks(done)


@pytest.mark.parametrize("sql, columns, row, hidden", [
    pytest.param("SELECT v.* FROM people p, LATERAL (VALUES ((p.address).*)) v", ["column1", "column2"],
                 ["某路", "某市"], [], id="没设遮罩的复合类型"),
    pytest.param("SELECT v.column1 AS r FROM sales s, LATERAL (VALUES (s.region)) v", ["r"], ["华东"], [],
                 id="没碰遮罩列的 VALUES"),
    pytest.param("SELECT v.column1 AS p FROM sales s, LATERAL (VALUES (s.buyer_phone)) v", ["p"], [PHONE], ["p"],
                 id="VALUES 里直接写遮罩列"),
    pytest.param("SELECT v.column2 AS r FROM (SELECT id, region FROM sales) q, LATERAL (VALUES (q.*)) v", ["r"],
                 ["华东"], [], id="没取遮罩列的子查询的 *"),
])
def test_values_without_an_expression_star_on_a_masked_column_is_read_as_before(sql, columns, row, hidden):
    """VALUES 里没有读到遮罩列的 (表达式).*：照旧按列认，没碰遮罩列的照常，直接写遮罩列的只遮那一列。"""
    done = masked(sql, columns, [list(row)])
    assert not done.whole and done.columns == hidden
    assert done.payload["rows"] == [[MASKED_WORD if c in hidden else v for c, v in zip(columns, row)]]


def test_an_expression_star_inside_a_function_argument_masks_the_whole_result_too():
    """行构造器、函数参数里的 (表达式).*：不逐个位置认展开不展开，读到遮罩列的一样整份遮（多遮，编号也遮）。"""
    done = masked("SELECT p.id AS n, row_to_json(ROW((p.card).*)) AS j FROM people3 p", ["n", "j"],
                  [[1, json.dumps({"f1": "某路 1 号", "f2": PHONE})]])
    assert done.whole and done.payload["rows"] == [[MASKED_WORD, MASKED_WORD]] and not _leaks(done)


# --------------------------------------------------------------------------
# 端到端：核查 agent 的真实查询工具
# --------------------------------------------------------------------------


@pytest.fixture
async def fake_source():
    """造出来的数据源（不连库，查询交给换掉的 run_query）：make(kind) 建一个、交回源名。"""
    made: list[str] = []

    async def make(kind: str) -> str:
        name = f"{kind[:2]}_{uuid.uuid4().hex[:8]}"
        async with SessionLocal() as session:
            row = DataSource(name=name, kind=kind, host="127.0.0.1", port=5432, username="probe", database="shop",
                             readonly=True, enabled=True, options={"mask_columns": ", ".join(MASKED)},
                             schema_cache=SCHEMA)
            session.add(row)
            await session.commit()
            made.append(row.id)
        return name

    yield make
    for source_id in made:
        await engines.invalidate(source_id)


async def _asked(monkeypatch, fake_source, kind: str, sql: str, columns: list[str], rows: list[list]):
    """按剧本让核查 agent 查一次（结果由换掉的 run_query 交回），交回 (工具结果原文, 落库的快照, 通用工件接口给的原文)。"""
    from app.data.engine import QueryResult
    from app.tools import datasource

    async def driver(source, statement, **kwargs):
        assert statement == sql
        return QueryResult(columns=list(columns), rows=[list(r) for r in rows], row_count=len(rows), truncated=False,
                           elapsed_ms=1, sql=statement)

    monkeypatch.setattr(datasource, "run_query", driver)
    name = await fake_source(kind)
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": None, "rationale": "x"})
    out, _ = await ask(name)
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
    assert got.status_code == 200
    return script.tool_results()["c1"], snapshot, got.text


@pytest.mark.parametrize("sql, columns, rows", [
    ("SELECT v.* FROM people3 p, LATERAL (VALUES ((p.card).*)) v", ["column1", "column2"],
     [["某路 1 号", PHONE], ["某路 2 号", "13900000002"]]),
    ("SELECT v.column2 AS c FROM people3 p, LATERAL (VALUES ((p.card).*)) v", ["c"], [[PHONE], ["13900000002"]]),
], ids=["取 *", "取第二列"])
async def test_values_expansions_never_reach_the_model_or_the_store(monkeypatch, fake_source, sql, columns, rows):
    """交给模型的、落库的、通用工件接口给的都只有「已遮罩」，masked_at_query 是全部的列，快照记着整份遮。以前
    masked_at_query 是 ['column1'] 或空的，号码原样交给模型、落库。"""
    seen, snapshot, api = await _asked(monkeypatch, fake_source, "postgresql", sql, columns, rows)
    assert MASKED_WORD in seen and "1390000000" not in seen, seen
    assert snapshot["masked_at_query"] == columns and snapshot["masked_whole"] is True
    assert "1390000000" not in json.dumps(snapshot, ensure_ascii=False) and "1390000000" not in api


# --------------------------------------------------------------------------
# 记着别的会话的 SQL、被锁那一行的值的视图
# --------------------------------------------------------------------------


#: 另一个会话正在执行的语句（号码写在条件里）：会话、事务、锁、日志一类的视图交回的就是它
BUSY = f"SELECT id, SLEEP(4) FROM sales WHERE buyer_phone = '{PHONE}' FOR UPDATE"


@pytest.mark.parametrize("sql, kind", [
    pytest.param("SELECT trx_query AS t FROM information_schema.INNODB_TRX", "mysql", id="MySQL 正在执行的事务"),
    pytest.param("SELECT trx_query FROM INFORMATION_SCHEMA.innodb_trx WHERE trx_state = 'RUNNING'", "mysql",
                 id="大小写混写"),
    pytest.param("SELECT lock_data AS d FROM information_schema.INNODB_LOCKS", "mariadb", id="MariaDB 被锁的行"),
    pytest.param("SELECT statement_text AS t FROM information_schema.QUERY_CACHE_INFO", "mariadb",
                 id="MariaDB 查询缓存"),
    pytest.param("SELECT sql_text AS t FROM dba_sqlset_statements", "oracle", id="Oracle SQL 调优集"),
    pytest.param("SELECT value_string AS t FROM dba_sqlset_binds", "oracle", id="Oracle 调优集的绑定变量"),
    pytest.param("SELECT sql_text AS t FROM dba_sql_plan_baselines", "oracle", id="Oracle 固定的执行计划"),
    pytest.param("SELECT sql_text AS t FROM dba_sql_profiles", "oracle", id="Oracle SQL Profile"),
    pytest.param("SELECT value AS t FROM dba_sqltune_binds", "oracle", id="Oracle 调优顾问的绑定变量"),
    pytest.param("SELECT undo_sql AS t FROM flashback_transaction_query", "oracle", id="Oracle 闪回事务"),
    pytest.param("SELECT sql_redo AS t FROM v$logmnr_contents", "oracle", id="Oracle LogMiner"),
    pytest.param("SELECT sql_text AS t FROM v$xml_audit_trail", "oracle", id="Oracle XML 审计"),
    pytest.param("SELECT trace AS t FROM information_schema.OPTIMIZER_TRACE", "mysql", id="MySQL 优化器跟踪"),
    # 动态视图 V$ / GV$ / X$、AWR 历史不逐个认，一律算
    pytest.param("SELECT client_info AS t FROM v$session", "oracle", id="Oracle V$ 动态视图"),
    pytest.param("SELECT kglnaobj AS t FROM x$kglob", "oracle", id="Oracle X$ 底表"),
    pytest.param("SELECT sql_text AS t FROM gv$sql_monitor", "oracle", id="Oracle GV$"),
    pytest.param("SELECT event AS t FROM dba_hist_active_sess_history", "oracle", id="Oracle AWR 历史"),
    pytest.param("SELECT plan_table_output AS t FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('a1', NULL, 'ADVANCED "
                 "+PEEKED_BINDS'))", "oracle", id="Oracle 游标的执行计划和绑定变量"),
    pytest.param("SELECT DBMS_SQLTUNE.REPORT_SQL_MONITOR(sql_id => 'a1', type => 'TEXT') AS t FROM dual", "oracle",
                 id="Oracle SQL 监控报告"),
])
def test_views_that_keep_other_sessions_sql_mask_the_whole_result(sql, kind):
    """别的会话执行过的 SQL 原文、被锁那一行的键值、闪回和日志里的改动前后的值：读的是哪一列认不出，整份遮。以前
    masked=[]，条件里的号码原样交给模型、落库。"""
    done = masked(sql, ["t"], [[BUSY]], kind=kind)
    assert done.whole and done.payload["rows"] == [[MASKED_WORD]]


@pytest.mark.parametrize("sql, columns", [
    pytest.param("SHOW ENGINE INNODB STATUS", ["Type", "Name", "Status"], id="InnoDB 状态"),
    pytest.param("show engine innodb status", ["Type", "Name", "Status"], id="小写"),
    pytest.param("SHOW BINLOG EVENTS IN 'binlog.000002'", ["Log_name", "Pos", "Info"], id="binlog 事件"),
    pytest.param("SHOW RELAYLOG EVENTS", ["Log_name", "Pos", "Info"], id="relay log 事件"),
    pytest.param("SHOW REPLICA STATUS", ["Last_SQL_Error"], id="复制状态"),
    pytest.param("SHOW SLAVE STATUS", ["Last_SQL_Error"], id="复制状态-旧写法"),
    pytest.param("SHOW PROFILES", ["Query_ID", "Duration", "Query"], id="PROFILES"),
    pytest.param("SHOW WARNINGS", ["Level", "Code", "Message"], id="WARNINGS"),
    pytest.param("SHOW CREATE VIEW vip_sales", ["View", "Create View"], id="视图定义"),
    # SHOW CREATE TABLE 对视图交回的是视图定义（MySQL 8.0 实测），建表语句里还有默认值、CHECK 约束、分区的取值
    pytest.param("SHOW CREATE TABLE vip_sales", ["View", "Create View"], id="建表语句-视图"),
    pytest.param("SHOW CREATE TABLE sales", ["Table", "Create Table"], id="建表语句"),
    pytest.param("SHOW TRIGGERS", ["Trigger", "Statement"], id="触发器"),
    pytest.param("SHOW EXPLAIN FOR 12", ["id", "Extra"], id="MariaDB 别的连接的计划"),
])
def test_show_statements_beyond_the_table_structure_mask_the_whole_result(sql, columns):
    """SHOW 里只交回表结构、库清单的几种以外（引擎状态、日志、复制、出错信息、视图和触发器的定义、建表语句……）：交回的
    是原文，可能带着别的会话 SQL 里的号码、定义里写死的号码，整份遮。以前守卫放行（SHOW 是只读的），遮罩认不出，号码
    原样。"""
    row = [BUSY] * len(columns)
    done = masked(sql, columns, [row], kind="mysql")
    assert done.whole and done.payload["rows"] == [[MASKED_WORD] * len(columns)]


@pytest.mark.parametrize("sql, columns, row", [
    pytest.param("SHOW TABLES", ["Tables_in_shop"], ["sales"], id="表清单"),
    pytest.param("SHOW FULL TABLES FROM shop", ["Tables_in_shop", "Table_type"], ["sales", "BASE TABLE"],
                 id="FULL TABLES"),
    pytest.param("SHOW COLUMNS FROM sales", ["Field", "Type"], ["region", "varchar(20)"], id="列"),
    pytest.param("SHOW FULL FIELDS FROM sales", ["Field", "Type"], ["region", "varchar(20)"], id="FULL FIELDS"),
    pytest.param("SHOW INDEX FROM sales", ["Table", "Key_name"], ["sales", "PRIMARY"], id="索引"),
    pytest.param("SHOW KEYS FROM sales", ["Table", "Key_name"], ["sales", "PRIMARY"], id="KEYS"),
    pytest.param("SHOW DATABASES", ["Database"], ["shop"], id="库清单"),
    pytest.param("SHOW CREATE DATABASE shop", ["Database", "Create Database"], ["shop", "CREATE DATABASE `shop`"],
                 id="建库语句"),
    pytest.param("SHOW TABLE STATUS LIKE 'sales'", ["Name", "Engine", "Rows"], ["sales", "InnoDB", 3], id="表状态"),
    pytest.param("DESCRIBE sales", ["Field", "Type"], ["region", "varchar(20)"], id="DESCRIBE 表"),
    pytest.param("EXPLAIN sales", ["Field", "Type"], ["region", "varchar(20)"], id="EXPLAIN 表"),
    pytest.param("DESC sales region", ["Field", "Type"], ["region", "varchar(20)"], id="DESC 表的一列"),
])
def test_show_and_describe_of_the_table_structure_stay_visible(sql, columns, row):
    """只交回表结构、库清单的 SHOW、DESCRIBE（核查 agent 探查表结构靠它们）照常。"""
    done = masked(sql, columns, [list(row)], kind="mysql")
    assert not done.whole and done.payload["rows"] == [list(row)]


@pytest.mark.parametrize("sql", [
    "EXPLAIN FORMAT=TREE SELECT s.id FROM sales s JOIN people3 p ON p.id = s.id WHERE s.id = 1",
    "EXPLAIN FORMAT = JSON SELECT id FROM sales WHERE id = 1",
    "EXPLAIN ANALYZE SELECT id FROM sales WHERE id = 1",
    "DESCRIBE SELECT id FROM sales WHERE id = 1",
    "EXPLAIN FOR CONNECTION 12",
    "EXPLAIN FORMAT=JSON FOR CONNECTION 12",
    "EXPLAIN (SELECT id FROM sales)",
    "EXPLAIN WITH t AS (SELECT id FROM sales) SELECT * FROM t",
    "EXPLAIN EXTENDED SELECT id FROM sales",
])
def test_explaining_a_statement_masks_the_whole_result(sql):
    """EXPLAIN 一条语句：MySQL 优化时把只有一行的表读出来、把值当常量代进条件，FORMAT=TREE / JSON、EXPLAIN ANALYZE 写出的
    条件里就是那一行的值；FOR CONNECTION 读的是别的连接正在执行的语句。整份遮。"""
    done = masked(sql, ["EXPLAIN"], [[f"-> Filter: (s.buyer_phone = '{PHONE}')"]], kind="mysql")
    assert done.whole and done.payload["rows"] == [[MASKED_WORD]]


@pytest.mark.parametrize("sql, columns, rows", [
    ("SELECT trx_query AS t FROM information_schema.INNODB_TRX", ["t"], [[BUSY]]),
    ("SHOW ENGINE INNODB STATUS", ["Type", "Name", "Status"],
     [["InnoDB", "", f"---TRANSACTION 1234, ACTIVE 2 sec\nMySQL thread id 9, query id 70 localhost root\n{BUSY}"]]),
], ids=["INNODB_TRX", "SHOW ENGINE INNODB STATUS"])
async def test_other_sessions_sql_never_reaches_the_model_or_the_store(monkeypatch, fake_source, sql, columns, rows):
    """端到端（MySQL）：另一个会话正在执行的 SQL 里写着号码，交给模型的、落库的、通用工件接口给的都只有「已遮罩」，
    快照记着整份遮。以前 masked_at_query 为空，SQL 原文连同号码原样交出。"""
    seen, snapshot, api = await _asked(monkeypatch, fake_source, "mysql", sql, columns, rows)
    assert MASKED_WORD in seen and "1390000000" not in seen, seen
    assert snapshot["masked_at_query"] == columns and snapshot["masked_whole"] is True
    assert "1390000000" not in json.dumps(snapshot, ensure_ascii=False) and "1390000000" not in api


def test_the_new_views_are_on_the_list_the_bundle_shares():
    """可验证报告和工具边界共用一份名单：新补的视图两边都认（报告那边本来就整个挡 information_schema）。"""
    from app.bundle import build

    for sql in ("SELECT trx_query FROM information_schema.INNODB_TRX", "SELECT sql_text FROM dba_sqlset_statements",
                "SELECT undo_sql FROM flashback_transaction_query", "SELECT sql_redo FROM v$logmnr_contents"):
        assert build._INDIRECT_READS.search(sql) and masking._INDIRECT.search(sql), sql
