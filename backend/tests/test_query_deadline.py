"""查询时限要让数据库自己停下，而不只是后端不再等。

以前时限只有数据层的一道 asyncio.wait_for：到点取消、放弃等待，可数据库那边的语句
照样在跑，占着连接池里的一个连接，直到它自己跑完（Oracle 实测多占了 60 秒）；
aiosqlite 更糟，语句在线程里跑，取消之后关连接要排在它后面，一条不收敛的查询
就能让这次调用永远回不来。

现在每条语句执行前先给数据库下一个语句级时限，到点由数据库自己中止：
- SQLite：进度回调（progress handler），超时后返回非零，SQLite 中止当前语句；
- PostgreSQL：SET LOCAL statement_timeout，只管这一个事务；
- MySQL：SET SESSION max_execution_time（MariaDB 是 max_statement_time），用完恢复；
- Oracle：驱动连接的 call_timeout，用完恢复。

SQLite 用真库测；另外三家用一个记账的假连接，断言下发的语句和参数。
数据源还可以在 options.query_timeout_s 里按源配置时限，db_query 工具把它声明在
metadata["timeout_s"] 上，引擎据此告诉界面上限是多少。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
import types
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

from app.data import engine as data_engine
from app.data.engine import build_url, engines, run_query
from app.data.guard import QueryLimits, SqlRejected
from app.main import app

#: 永远跑不完的查询：递归 CTE 没有终止条件。不靠机器快慢，时限不生效它就不会结束
RUNAWAY = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
#: 跑得完、但要走过成千上万条 SQLite 指令的查询：进度回调要是没摘掉，它会被立刻中止
BUSY = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000) "
        "SELECT count(*) FROM c")


def _source(path, *, readonly: bool = True, **options: Any) -> Any:
    return types.SimpleNamespace(
        id=f"deadline_{uuid.uuid4().hex[:8]}", name="shop", kind="sqlite", database=str(path),
        readonly=readonly, options=options, host=None, port=None, username=None, password=None,
        description="", schema_cache={},
    )


@pytest.fixture
def shop_db(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
async def sqlite_source(shop_db):
    """真实的 SQLite 数据源。测完把还卡在语句上的连接打断，免得时限没生效时测试进程挂住。"""
    made: list[Any] = []

    async def make(**kw: Any) -> Any:
        source = _source(shop_db, **kw)
        engine = (await engines.get(source)).sync_engine
        event.listen(engine, "connect", lambda dbapi_conn, _rec: raws.append(dbapi_conn))
        made.append(source)
        return source

    raws: list[Any] = []
    yield make
    for dbapi_conn in raws:
        try:
            dbapi_conn._connection._conn.interrupt()
        except Exception:  # noqa: BLE001 - 已经关掉的连接
            pass
    for source in made:
        await engines.invalidate(source.id)


async def _within(coro, seconds: float) -> tuple[bool, Any, float]:
    """跑 coro，最多等 seconds 秒；超时不取消（取消本身也会卡住），只报没回来。"""
    started = time.perf_counter()
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        task.add_done_callback(lambda f: f.cancelled() or f.exception())
        return False, None, time.perf_counter() - started
    return True, task.exception() or task.result(), time.perf_counter() - started


# --------------------------------------------------------------------------
# SQLite：真库
# --------------------------------------------------------------------------


async def test_sqlite_stops_a_runaway_query_at_the_limit(sqlite_source):
    source = await sqlite_source()
    back, outcome, took = await _within(
        run_query(source, RUNAWAY, limits=QueryLimits(timeout_seconds=1)), 6)
    assert back, "查询时限到了，语句还在数据库里跑，调用一直没回来"
    assert isinstance(outcome, SqlRejected), outcome
    assert "1s" in str(outcome) and "被中断" in str(outcome)
    assert took < 3, f"1s 的时限等了 {took:.1f}s"
    engine = await engines.get(source)
    assert engine.sync_engine.pool.checkedout() == 0, "被中断的查询还占着连接"


async def test_the_limit_does_not_leak_onto_the_next_user_of_the_connection(sqlite_source):
    source = await sqlite_source()
    back, outcome, _ = await _within(
        run_query(source, RUNAWAY, limits=QueryLimits(timeout_seconds=1)), 6)
    assert back and isinstance(outcome, SqlRejected)

    # 同一个连接（池里只剩它）接着给别人用：时限早就过了，回调要是还挂着，这条会被立刻中止
    engine = await engines.get(source)
    async with engine.connect() as conn:
        assert (await conn.execute(text(BUSY))).scalar() == 200000
    result = await run_query(source, BUSY)
    assert result.rows == [[200000]]


async def test_a_runaway_write_is_stopped_and_rolled_back(sqlite_source, shop_db):
    source = await sqlite_source(readonly=False)
    runaway_insert = ("INSERT INTO orders (id) WITH RECURSIVE c(x) AS "
                      "(SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c")
    back, outcome, took = await _within(
        run_query(source, runaway_insert, limits=QueryLimits(timeout_seconds=1)), 6)
    assert back, "写操作的时限到了，语句还在跑"
    assert isinstance(outcome, SqlRejected) and "已回滚" in str(outcome), outcome
    assert took < 3
    conn = sqlite3.connect(shop_db)
    assert conn.execute("SELECT count(*) FROM orders").fetchone() == (5,), "被中断的写没有回滚"
    conn.close()


async def test_a_source_can_set_its_own_limit(sqlite_source):
    # 表单里填的是字符串
    source = await sqlite_source(query_timeout_s="1")
    back, outcome, took = await _within(run_query(source, RUNAWAY), 6)
    assert back and isinstance(outcome, SqlRejected), outcome
    assert "1s" in str(outcome) and took < 3


def test_the_limit_is_read_from_options_and_defaults_otherwise(shop_db):
    from app.data.engine import query_timeout

    assert query_timeout(_source(shop_db)) == QueryLimits().timeout_seconds
    assert query_timeout(_source(shop_db, query_timeout_s="45")) == 45
    assert query_timeout(_source(shop_db, query_timeout_s=2.5)) == 2.5
    # 库里存着的坏值不让查询跑不起来：退回缺省
    for bad in ("abc", "0", "-3", "", None, True, "99999"):
        assert query_timeout(_source(shop_db, query_timeout_s=bad)) == QueryLimits().timeout_seconds, bad


def test_the_limit_is_not_passed_to_the_driver(shop_db):
    source = types.SimpleNamespace(
        id="x", name="shop", kind="mysql", host="db.example", port=3306, database="shop",
        username="reader", password=None, readonly=True,
        options={"charset": "utf8mb4", "query_timeout_s": "60", "schema": "sales"})
    url = build_url(source)
    assert "charset=utf8mb4" in url
    assert "query_timeout_s" not in url, "时限是 AgentLab 自己的配置，驱动不认识它"


async def test_the_query_tool_declares_the_limit(sqlite_source, monkeypatch):
    from app.engine.toolcalls import limit_of
    from app.tools.datasource import _make_query_tool
    from app.tools.registry import ToolContext

    ctx = ToolContext(run_id="t", node_id="n")
    default = _make_query_tool(await sqlite_source(), ctx)
    assert default.metadata["timeout_s"] == QueryLimits().timeout_seconds

    tool = _make_query_tool(await sqlite_source(query_timeout_s="1"), ctx)
    assert tool.metadata["timeout_s"] == 1
    limit = limit_of(tool, tool.name, {"sql": RUNAWAY})
    assert limit.seconds == 1 and limit.kind == "query" and limit.self_timed

    back, outcome, took = await _within(tool.coroutine(sql=RUNAWAY), 6)
    assert back and "1s" in str(outcome) and took < 3, (outcome, took)


# --------------------------------------------------------------------------
# PostgreSQL / MySQL / MariaDB / Oracle：假连接记账
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[list[Any]]) -> None:
        self._rows = rows
        self.returns_rows = True
        self.rowcount = len(rows)

    def keys(self) -> list[str]:
        return ["n"]

    def fetchmany(self, n: int) -> list[list[Any]]:
        return self._rows[:n]

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


class _FakeConn:
    def __init__(self, log: list[str], driver: Any, mariadb: bool, fail: Exception | None) -> None:
        self.log, self.driver, self.fail = log, driver, fail
        self.dialect = types.SimpleNamespace(is_mariadb=mariadb)

    async def execute(self, clause):
        sql = str(clause)
        self.log.append(sql)
        if sql.startswith("SET"):
            return _Result([])
        if self.fail:
            raise self.fail
        return _Result([[1]])

    async def stream(self, clause):
        self.log.append(str(clause))
        self.log.append(f"call_timeout={getattr(self.driver, 'call_timeout', None)}")
        if self.fail:
            raise self.fail
        return _Result([[1]])

    async def get_raw_connection(self):
        return types.SimpleNamespace(driver_connection=self.driver)


class _FakeEngine:
    def __init__(self, *, mariadb: bool = False, fail: Exception | None = None) -> None:
        self.log: list[str] = []
        self.driver = types.SimpleNamespace(call_timeout=0)
        self.mariadb, self.fail = mariadb, fail

    @asynccontextmanager
    async def connect(self):
        yield _FakeConn(self.log, self.driver, self.mariadb, self.fail)

    begin = connect


def _fake(monkeypatch, **kw: Any) -> _FakeEngine:
    fake = _FakeEngine(**kw)

    async def _get(_source):
        return fake

    monkeypatch.setattr(engines, "get", _get)
    return fake


def _remote(kind: str, *, readonly: bool = True) -> Any:
    return types.SimpleNamespace(id=f"r_{kind}", name="shop", kind=kind, readonly=readonly,
                                 options={}, database="shop")


async def test_postgres_gets_a_statement_timeout_for_this_transaction_only(monkeypatch):
    fake = _fake(monkeypatch)
    await run_query(_remote("postgres"), "SELECT 1", limits=QueryLimits(timeout_seconds=5))
    assert fake.log[:2] == ["SET LOCAL statement_timeout = 5000", "SELECT 1"], fake.log
    # SET LOCAL 随事务回滚作废，不用也不该再发一条恢复
    assert not [s for s in fake.log[2:] if s.startswith("SET")]


async def test_postgres_writes_are_bounded_too(monkeypatch):
    fake = _fake(monkeypatch)
    await run_query(_remote("postgresql", readonly=False), "UPDATE orders SET id = 1",
                    limits=QueryLimits(timeout_seconds=5))
    assert fake.log[:2] == ["SET LOCAL statement_timeout = 5000", "UPDATE orders SET id = 1"]


async def test_mysql_gets_max_execution_time_and_it_is_put_back(monkeypatch):
    fake = _fake(monkeypatch)
    await run_query(_remote("mysql"), "SELECT 1", limits=QueryLimits(timeout_seconds=5))
    assert fake.log[0] == "SET SESSION max_execution_time = 5000"
    assert "SELECT 1" in fake.log
    # 会话级设置跟着连接回池子：用完恢复成服务器的缺省，探查结构这类长操作不受它牵连
    assert fake.log[-1] == "SET SESSION max_execution_time = DEFAULT"


async def test_mariadb_uses_its_own_variable_in_seconds(monkeypatch):
    fake = _fake(monkeypatch, mariadb=True)
    await run_query(_remote("mysql"), "SELECT 1", limits=QueryLimits(timeout_seconds=5))
    assert fake.log[0] == "SET SESSION max_statement_time = 5"
    assert fake.log[-1] == "SET SESSION max_statement_time = DEFAULT"


async def test_oracle_gets_a_call_timeout_and_it_is_put_back(monkeypatch):
    fake = _fake(monkeypatch)
    await run_query(_remote("oracle"), "SELECT 1 FROM DUAL", limits=QueryLimits(timeout_seconds=5))
    assert "call_timeout=5000" in fake.log, fake.log
    assert fake.driver.call_timeout == 0, "call_timeout 跟着连接回池子，探查结构时也会被它掐"


@pytest.mark.parametrize("kind, driver_error", [
    ("postgres", "canceling statement due to statement timeout"),
    ("mysql", "(3024, 'Query execution was interrupted, maximum statement execution time exceeded')"),
    ("mariadb", "(1969, 'Query execution was interrupted (max_statement_time exceeded)')"),
    ("oracle", "DPY-4024: call timeout of 5000 ms exceeded"),
])
async def test_a_query_stopped_by_the_database_reads_as_a_timeout(monkeypatch, kind, driver_error):
    _fake(monkeypatch, mariadb=kind == "mariadb", fail=RuntimeError(driver_error))
    with pytest.raises(SqlRejected) as caught:
        await run_query(_remote(kind), "SELECT 1", limits=QueryLimits(timeout_seconds=5))
    assert "查询超过 5s 被中断" in str(caught.value), caught.value


class _PgCanceled(Exception):
    """asyncpg 的 QueryCanceledError：错误码在 sqlstate 上，文字跟着服务器的语言走。"""

    sqlstate = "57014"


class _Wrapped(Exception):
    """SQLAlchemy 包过一层的驱动异常：原始异常在 .orig 上。"""

    def __init__(self, orig: Exception) -> None:
        super().__init__(f"(wrapped) {type(orig).__name__}")
        self.orig = orig


@pytest.mark.parametrize("kind, error", [
    # 中文环境的 PostgreSQL（真库实测就是这句）：按英文原文一个也认不出来，得认错误码
    ("postgres", _PgCanceled("由于语句执行超时，正在取消查询命令")),
    ("postgres", _Wrapped(_PgCanceled("由于语句执行超时，正在取消查询命令"))),
    ("mysql", RuntimeError(3024, "查询执行被中断，超过了最长执行时间")),
    ("mariadb", _Wrapped(RuntimeError(1969, "查询执行被中断"))),
])
async def test_a_localized_timeout_is_recognised_by_its_code(monkeypatch, kind, error):
    _fake(monkeypatch, mariadb=kind == "mariadb", fail=error)
    with pytest.raises(SqlRejected) as caught:
        await run_query(_remote(kind), "SELECT 1", limits=QueryLimits(timeout_seconds=5))
    assert "查询超过 5s 被中断" in str(caught.value), caught.value


async def test_other_errors_are_not_mistaken_for_a_timeout(monkeypatch):
    _fake(monkeypatch, fail=RuntimeError('relation "orderz" does not exist'))
    with pytest.raises(RuntimeError, match="orderz"):
        await run_query(_remote("postgres"), "SELECT 1", limits=QueryLimits(timeout_seconds=5))


# --------------------------------------------------------------------------
# 接口：按源配置的时限
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_the_form_offers_the_limit_for_every_kind(client):
    kinds = (await client.get("/api/datasources/kinds")).json()
    for kind in kinds["kinds"]:
        field = next((a for a in kind["advanced"] if a["key"] == "query_timeout_s"), None)
        assert field and "秒" in field["label"], kind["value"]


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "100000"])
async def test_a_bad_limit_is_refused_in_words(client, shop_db, bad):
    r = await client.post("/api/datasources", json={
        "name": f"shop_{uuid.uuid4().hex[:6]}", "kind": "sqlite", "database": str(shop_db),
        "options": {"query_timeout_s": bad}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "查询时限" in detail, detail


async def test_a_good_limit_is_saved_and_can_be_changed(client, shop_db):
    r = await client.post("/api/datasources", json={
        "name": f"shop_{uuid.uuid4().hex[:6]}", "kind": "sqlite", "database": str(shop_db),
        "options": {"query_timeout_s": "45"}})
    assert r.status_code == 201, r.text
    row = r.json()
    try:
        r = await client.patch(f"/api/datasources/{row['id']}",
                               json={"options": {"query_timeout_s": "nope"}})
        assert r.status_code == 422
        r = await client.patch(f"/api/datasources/{row['id']}",
                               json={"options": {"query_timeout_s": "90"}})
        assert r.status_code == 200 and r.json()["options"]["query_timeout_s"] == "90"
    finally:
        await client.delete(f"/api/datasources/{row['id']}")


def test_the_data_layer_backstop_stays_inside_the_engine_grace():
    """数据库按时限停下之后，数据层自己的兜底要比引擎放弃得早：引擎放弃时只撒手、
    不取消（见 toolcalls.Limit.self_timed），数据层这句「超过 Ns 被中断」才是给人看的那句。"""
    from app.engine.toolcalls import GRACE_S

    assert 0 < data_engine.BACKSTOP_S < GRACE_S
