"""SQLite 连接一律关掉 DQS：写错的双引号列名必须报错，而不是被当成字符串。

DQS（double-quoted string）是 SQLite 的历史兼容：双引号里的名字找不到列时当成字符串。
于是 `SUM("金颔")` 得 0、`GROUP BY "金颔"` 把整张表归成一组，查询不报错，结论却是错的。
中文列名常被加上双引号，正好踩中。

这组测试覆盖项目实际用到的三种连接形式（只读源的 mode=ro URI、可写源的普通路径、
表单里的一次性测试连接），以及关不掉时连接被拒、启动自检。
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.data import engine as engine_mod
from app.data.engine import engine_args, engines, run_query, sqlite_dqs_self_check


def _db(path) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE 时段客流 (分区 TEXT, 客流 INTEGER)")
    conn.executemany("INSERT INTO 时段客流 VALUES (?, ?)",
                     [("分区甲", 120), ("分区乙", 80), ("分区甲", 30)])
    conn.commit()
    conn.close()
    return str(path)


def _source(path: str, *, readonly: bool = True):
    return SimpleNamespace(
        id=f"dqs-{uuid.uuid4().hex[:8]}", kind="sqlite", database=path, readonly=readonly,
        name="dqs_probe", options={}, password=None, username=None, host=None, port=None,
    )


@pytest.fixture
async def ro_source(tmp_path):
    src = _source(_db(tmp_path / "客流 样例.db"))
    yield src
    await engines.invalidate(src.id)


@pytest.fixture
async def rw_source(tmp_path):
    src = _source(_db(tmp_path / "rw.db"), readonly=False)
    yield src
    await engines.invalidate(src.id)


# ---------------------------------------------------------------------------
# 三种连接形式下，写错的双引号列名都报错
# ---------------------------------------------------------------------------

async def test_misspelled_quoted_column_errors_on_readonly_uri_form(ro_source):
    """只读源走 sqlite+aiosqlite:///file:…?mode=ro&uri=true——critic C4 实测踩坑的正是这个形式。"""
    url, _ = engine_args(ro_source)
    assert url.startswith("sqlite+aiosqlite:///file:") and url.endswith("?mode=ro&uri=true")

    for sql in ('SELECT SUM("客六") FROM 时段客流',
                'SELECT "客六", COUNT(*) FROM 时段客流 GROUP BY "客六"',
                'SELECT 分区 FROM 时段客流 WHERE "分去" = 1'):
        with pytest.raises(OperationalError, match="no such column"):
            await run_query(ro_source, sql)

    # 写对的双引号列名照常可用
    ok = await run_query(ro_source, 'SELECT SUM("客流") AS n FROM "时段客流"')
    assert ok.rows == [[230]]
    # 单引号字符串照常是字符串
    lit = await run_query(ro_source, "SELECT COUNT(*) FROM 时段客流 WHERE 分区 = '分区甲'")
    assert lit.rows == [[2]]


async def test_misspelled_quoted_column_errors_on_writable_form(rw_source):
    """可写源走普通路径；DDL 里的双引号字符串也一并关掉。"""
    url, _ = engine_args(rw_source)
    assert "mode=ro" not in url

    with pytest.raises(OperationalError, match="no such column"):
        await run_query(rw_source, 'SELECT SUM("客六") FROM 时段客流')

    engine = await engines.get(rw_source)
    async with engine.connect() as conn:
        with pytest.raises(OperationalError, match="no such column"):
            await conn.execute(text('CREATE INDEX 错索引 ON 时段客流 ("不存在")'))


async def test_one_off_probe_engine_is_covered_too(ro_source):
    """表单里的「测试连接」用 engine_args 现建一次性 engine（datasources._probe），不走缓存，
    也得关 DQS：关闭措施挂在 engine_args 给出的连接参数上，而不是只挂在缓存建的 engine 上。"""
    url, extra = engine_args(ro_source)
    engine = create_async_engine(url, poolclass=NullPool, **extra)
    try:
        async with engine.connect() as conn:
            with pytest.raises(OperationalError, match="no such column"):
                await conn.execute(text('SELECT SUM("客六") FROM 时段客流'))
    finally:
        await engine.dispose()


async def test_aiosqlite_passes_factory_through():
    """钉住中间那一层：aiosqlite.connect 把关键字参数原样交给 sqlite3.connect。

    关 DQS 靠 sqlite3.connect 的 factory 参数，一路经 SQLAlchemy connect_args →
    aiosqlite.connect(**kwargs) → sqlite3.connect(factory=…) 传下去。哪天 aiosqlite
    不再转交，这里先失败。
    """
    async with aiosqlite.connect(":memory:", factory=engine_mod._DqsOffConnection) as conn:
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await conn.execute('SELECT "不存在"')


# ---------------------------------------------------------------------------
# 关不掉就拒绝这条连接
# ---------------------------------------------------------------------------

def _raise_on_setconfig(self, op, enable=True):
    raise sqlite3.OperationalError("模拟：setconfig 失败")


def _ignore_setconfig(self, op, enable=True):
    return None   # 调用成功，但什么也没设上


@pytest.mark.parametrize("fake", [_raise_on_setconfig, _ignore_setconfig],
                         ids=["setconfig 抛错", "setconfig 没生效"])
async def test_connection_is_refused_when_dqs_cannot_be_disabled(ro_source, monkeypatch, fake):
    monkeypatch.setattr(engine_mod._DqsOffConnection, "setconfig", fake)
    engine = await engines.get(ro_source)
    with pytest.raises(OperationalError, match="未能关闭 SQLite 的双引号字符串兼容，已拒绝连接"):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    # 走查询入口同样连不上，不会退回一条没关 DQS 的连接
    with pytest.raises(OperationalError, match="已拒绝连接"):
        await run_query(ro_source, "SELECT COUNT(*) FROM 时段客流")


async def test_connection_is_refused_when_sqlite_lacks_the_option(ro_source, monkeypatch):
    """Python 3.12 之前的 sqlite3 没有这两个常量：照样拒绝，而不是当作没这回事。"""
    monkeypatch.setattr(engine_mod, "_DQS_OPTIONS", (("DML", None), ("DDL", None)))
    engine = await engines.get(ro_source)
    with pytest.raises(OperationalError, match="已拒绝连接"):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


def test_refusal_is_a_driver_error():
    """拒绝用的异常是 sqlite3.OperationalError 的子类：SQLAlchemy 按驱动异常包装，
    上层照常当作「连接失败」处理。"""
    assert issubclass(engine_mod.SqliteHardeningError, sqlite3.OperationalError)


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------

def test_self_check_passes_on_this_platform():
    sqlite_dqs_self_check()


@pytest.mark.parametrize("fake", [_raise_on_setconfig, _ignore_setconfig],
                         ids=["setconfig 抛错", "setconfig 没生效"])
def test_self_check_fails_when_dqs_cannot_be_disabled(monkeypatch, fake):
    monkeypatch.setattr(engine_mod._DqsOffConnection, "setconfig", fake)
    with pytest.raises(RuntimeError, match="SQLite 自检失败"):
        sqlite_dqs_self_check()


def test_self_check_fails_when_quoted_names_still_pass(monkeypatch):
    """连接建得起来、却没关 DQS（比如关闭措施被人改没了）：自检得看结果，不只看有没有抛错。"""
    monkeypatch.setattr(engine_mod, "_disable_dqs", lambda conn: None)
    with pytest.raises(RuntimeError, match="没有报错"):
        sqlite_dqs_self_check()


def test_self_check_catches_ddl_left_on(monkeypatch):
    """只关了 DML 那项、DDL 里的双引号字符串还开着：查询探针照常报错，建索引探针不报——
    自检得靠建索引那条把它认出来。"""
    dml_only = tuple(item for item in engine_mod._DQS_OPTIONS if item[0] == "DML")
    assert len(dml_only) == 1
    monkeypatch.setattr(engine_mod, "_DQS_OPTIONS", dml_only)
    with pytest.raises(RuntimeError, match="CREATE INDEX") as info:
        sqlite_dqs_self_check()
    assert "没有报错" in str(info.value)


@pytest.mark.parametrize("probe", ["SELECT a FROM no_such_table",
                                   'SELECT "no_such_column" FROM'],
                         ids=["查不存在的表", "语法错误"])
def test_self_check_rejects_the_wrong_kind_of_error(monkeypatch, probe):
    """探针因为别的原因报错，说明它没走到双引号名字那一步，什么也没验到：自检照样失败，
    不能见到报错就当通过。"""
    monkeypatch.setattr(engine_mod, "_SELF_CHECK_PROBES", (probe,))
    with pytest.raises(RuntimeError, match="不是预期的「no such column」"):
        sqlite_dqs_self_check()


def test_self_check_fails_when_the_probe_table_cannot_be_made(monkeypatch):
    monkeypatch.setattr(engine_mod, "_SELF_CHECK_SETUP", "CREATE TABLE")
    with pytest.raises(RuntimeError, match="SQLite 自检失败"):
        sqlite_dqs_self_check()


async def test_startup_runs_the_self_check_first(monkeypatch):
    """main 的启动流程第一步就自检，失败时服务起不来（异常从 lifespan 抛出）。"""
    from app.main import app, lifespan, settings

    calls: list[str] = []

    def failing() -> None:
        calls.append("self_check")
        raise RuntimeError("SQLite 自检失败：模拟")

    def must_not_run(self) -> None:
        calls.append("ensure_dirs")

    monkeypatch.setattr(engine_mod, "sqlite_dqs_self_check", failing)
    monkeypatch.setattr(type(settings), "ensure_dirs", must_not_run)
    with pytest.raises(RuntimeError, match="SQLite 自检失败"):
        async with lifespan(app):
            pass
    assert calls == ["self_check"]
