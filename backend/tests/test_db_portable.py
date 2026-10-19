"""应用库在 SQLite、PostgreSQL 上行为一致（db/portable.py、db/postgres.py、db/base.py 的迁移）。

连着库的那几项在两种库上各跑一遍（scripts/test-postgres.sh 跑 PostgreSQL），断言同一套结果；只看编译结果的不连库；
PostgreSQL 老库迁移另建一个库（只在 AGENTLAB_TEST_DATABASE_URL 时跑）。全是虚构数据。
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.db import base as db_base
from app.db.base import Base, SessionLocal
from app.db.models import Run, TableRecipe
from app.db.portable import json_eq, json_flag, json_is_string, json_text, unique_violation

PG = bool(os.environ.get("AGENTLAB_TEST_DATABASE_URL", "").strip())


def _pg_dialect():
    from sqlalchemy.dialects.postgresql.asyncpg import dialect

    from app.db import postgres

    made = dialect()
    postgres.install(made)
    return made


# --------------------------------------------------------------------------
# 守着的约定
# --------------------------------------------------------------------------


def test_every_migrated_column_is_in_the_models():
    """PostgreSQL 上补列的类型取模型里这一列的（db/base._column_ddl）：迁移表里的每一列模型里都得有。"""
    missing = [(t, c) for t, c, _ in db_base._COLUMN_MIGRATIONS
               if t not in Base.metadata.tables or c not in Base.metadata.tables[t].c]
    assert missing == []


def test_every_not_null_migrated_column_gets_a_value_for_old_rows():
    """迁移补的列模型里是 NOT NULL 的，存量行不能留 NULL（PostgreSQL 的表照模型带 NOT NULL，导入时整行插不进）：要么补列时
    带 DEFAULT 回填，要么登记在 _NOT_NULL_BACKFILLS 里每次启动按模型的缺省值填。"""
    bad = []
    for table, column, ddl in db_base._COLUMN_MIGRATIONS:
        col = Base.metadata.tables[table].c[column]
        if col.nullable:
            continue
        if not re.search(r"\bDEFAULT\b", ddl, re.I) and (table, column) not in db_base._NOT_NULL_BACKFILLS:
            bad.append((table, column))
    assert bad == []
    for table, column in db_base._NOT_NULL_BACKFILLS:
        assert db_base.model_default(Base.metadata.tables[table].c[column])[0], (table, column)


def test_checkpoint_tables_do_not_clash_with_app_tables():
    """断点和应用表放在同一个库、同一个 schema：两边的表名不能撞。名字从两个 saver 自己的建表语句里取。"""
    import inspect as pyinspect

    from langgraph.checkpoint.postgres.base import BasePostgresSaver
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", " ".join(BasePostgresSaver.MIGRATIONS)))
    created |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", pyinspect.getsource(AsyncSqliteSaver.setup)))
    assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"} <= created
    assert created & set(Base.metadata.tables) == set()


def test_the_compile_guard_catches_whole_json_values_however_they_are_written():
    """整个 JSON 值做比较、去重、分组、排序：编译时就报错（tests/jsonguard.py，conftest 挂在应用库引擎上，跑全量时一直生效）。
    按表达式的类型认，逐行正则抓不到的写法也抓得到；按键筛选的 json_eq 等、IS NULL、UNION ALL 不受影响。"""
    from sqlalchemy import union, union_all

    from tests.jsonguard import JsonMisuse

    dialect = db_base.engine.sync_engine.dialect
    alias = Run.origin
    caught = [
        select(Run.origin).distinct(),
        select(Run).distinct(),
        select(Run.id).order_by(
            Run.created_at,
            Run.origin,
        ),
        select(Run.id).order_by(alias.desc()),
        select(Run.id).where(alias == {"a": 1}),
        select(Run.id).where(Run.input.in_([{"q": 1}])),
        select(Run.id).where(Run.origin["k"] == 1),
        select(func.count()).select_from(Run).group_by(Run.options),
        union(select(Run.origin), select(Run.input)),
    ]
    for statement in caught:
        with pytest.raises(JsonMisuse):
            statement.compile(dialect=dialect)
    fine = [
        select(Run.origin).where(json_eq(Run.origin, "k", 1), Run.origin.is_(None),
                                 Run.origin != None),  # noqa: E711 - 写成 IS NOT NULL，json 上也能用
        select(Run.id).where(json_text(Run.origin, "k") == "x").order_by(json_text(Run.origin, "k")).distinct(),
        union_all(select(Run.origin), select(Run.input)),
    ]
    for statement in fine:
        statement.compile(dialect=dialect)


def test_postgres_columns_are_unbounded_varchar_bigint_and_json():
    """应用库上 String(n) 建成不限长的 VARCHAR、文本列按字节比（COLLATE "C"，同 SQLite）、Integer 是 BIGINT（自增主键
    BIGSERIAL）、JSON 是 json（不是 JSONB：JSONB 会重排对象里的键）、时间是不带时区的 TIMESTAMP。SQLite 上的建表语句不变。"""
    from sqlalchemy.dialects.sqlite.aiosqlite import dialect as sqlite_dialect

    runs = Base.metadata.tables["runs"]
    pg = CreateTable(runs).compile(dialect=_pg_dialect()).string
    assert 'id VARCHAR COLLATE "C" NOT NULL' in pg and "VARCHAR(" not in pg
    assert 'error TEXT COLLATE "C"' in pg
    assert "last_seq BIGINT" in pg and "INTEGER" not in pg
    assert "input JSON NOT NULL" in pg and "JSONB" not in pg
    assert "created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL" in pg
    changes = CreateTable(Base.metadata.tables["source_changes"]).compile(dialect=_pg_dialect()).string
    assert "seq BIGSERIAL NOT NULL" in changes
    lite = CreateTable(runs).compile(dialect=sqlite_dialect()).string
    assert "id VARCHAR(32) NOT NULL" in lite and "last_seq INTEGER" in lite and "input JSON NOT NULL" in lite
    assert "COLLATE" not in lite


def test_postgres_casts_keep_their_length():
    """CAST(x AS VARCHAR(n)) 是截断的意思，照旧写长度；只有建表时的列不限长。"""
    from sqlalchemy import String, cast

    sql = str(select(cast(Run.workflow_name, String(4))).compile(dialect=_pg_dialect()))
    assert "CAST(runs.workflow_name AS VARCHAR(4))" in sql


def test_postgres_orders_nulls_like_sqlite():
    """SQLite 升序 NULL 在前、降序 NULL 在后：PostgreSQL 上可空列照这个补写，不可空的列、写明了的不动。"""
    from app.db.models import Task

    sql = str(select(Run.id, func.row_number().over(order_by=Run.started_at).label("n"))
              .order_by(Run.started_at, Run.finished_at.desc(), Run.created_at.desc(),
                        Task.due_at.desc().nulls_first(), Task.remind_at.asc().nulls_last())
              .compile(dialect=_pg_dialect()))
    assert "OVER (ORDER BY runs.started_at ASC NULLS FIRST)" in sql
    order = sql.split("ORDER BY", 2)[2]
    assert "runs.started_at ASC NULLS FIRST" in order
    assert "runs.finished_at DESC NULLS LAST" in order
    assert "runs.created_at DESC," in order                       # 不可空：不补
    assert "tasks.due_at DESC NULLS FIRST" in order and "NULLS FIRST NULLS" not in order
    assert "tasks.remind_at ASC NULLS LAST" in order


def test_postgres_integer_binds_are_bigint():
    """和 Integer 列比较、写进 Integer 列的参数按 BIGINT 转换，超过 21 亿的值不在半路溢出。"""
    sql = str(select(Run.id).where(Run.last_seq > 5).compile(dialect=_pg_dialect()))
    assert "runs.last_seq > $1::BIGINT" in sql


def test_database_urls_are_normalized_and_never_logged_with_the_password():
    from app.db.urls import describe, libpq_conninfo, normalize_checkpoint_url, normalize_database_url

    assert normalize_database_url("postgres://u:p%40w@h:5433/db") == "postgresql+asyncpg://u:p%40w@h:5433/db"
    assert normalize_checkpoint_url("postgresql+asyncpg://u:pw@h/db") == "postgresql://u:pw@h/db"
    # asyncpg 的 ssl= 换成 libpq 的 sslmode=，只给 asyncpg 看的参数不带给 psycopg
    assert libpq_conninfo("postgresql+asyncpg://u:pw@h/db?ssl=require&prepared_statement_cache_size=0") \
        == "postgresql://u:pw@h/db?sslmode=require"
    # 应用库地址写 libpq 的 sslmode=：换成 asyncpg 的 ssl=（以前原样交给 asyncpg，连的时候 TypeError）；断点库换回 sslmode=
    app_url = normalize_database_url("postgresql://u:pw@h/db?sslmode=verify-full")
    assert app_url == "postgresql+asyncpg://u:pw@h/db?ssl=verify-full"
    assert libpq_conninfo(app_url) == "postgresql://u:pw@h/db?sslmode=verify-full"
    for bad, says in (("postgresql://u@h/db?sslrootcert=/etc/ca.pem", "PGSSLROOTCERT"),
                      ("postgresql://u@h/db?sslmode=strict", "sslmode=strict"),
                      ("postgresql://u@h/db?ssl=require&sslmode=require", "只写一个"),
                      ("postgresql://u@h/db?application_name=x", "application_name")):
        with pytest.raises(ValueError, match=says):
            normalize_database_url(bad)
    assert "pw" not in describe("postgresql+asyncpg://u:pw@h:5433/db")
    for bad in ("mysql://u@h/db", "sqlite:///x.db", "postgresql://h", "这不是地址"):
        with pytest.raises(ValueError):
            normalize_database_url(bad)
    from app.core.config import Settings

    with pytest.raises(ValueError, match="AGENTLAB_DATABASE_URL"):
        Settings(database_url="mysql://u@h/db")
    assert Settings(database_url="  ").db_dialect == "sqlite"


def test_a_postgres_url_without_the_driver_installed_says_what_to_install(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None if name == "asyncpg" else real(name, *a, **k))
    with pytest.raises(RuntimeError, match=r"backend\[postgres\]"):
        db_base.make_engine("postgresql+asyncpg://u:pw@127.0.0.1:1/db")


def test_the_startup_line_names_the_database_without_the_password():
    from sqlalchemy.engine import make_url

    line = db_base.describe_database()
    password = make_url(db_base.settings.db_url).password
    assert ("PostgreSQL" if PG else "SQLite") in line
    assert not password or password not in line


# --------------------------------------------------------------------------
# 连着库：两种库同一套结果
# --------------------------------------------------------------------------


async def _runs(origins: list[dict | None]) -> tuple[str, list[str]]:
    tag = uuid.uuid4().hex[:12]
    ids = []
    async with SessionLocal() as session:
        for i, origin in enumerate(origins):
            row = Run(workflow_name=f"便携-{tag}-{i}", origin=origin, input={"question": "虚构的问题"} if i % 2 else
                      {"question": 7})
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return tag, ids


async def _match(tag: str, *where) -> list[int]:
    async with SessionLocal() as session:
        names = (await session.execute(select(Run.workflow_name).where(
            Run.workflow_name.like(f"便携-{tag}-%"), *where))).scalars().all()
    return sorted(int(n.rsplit("-", 1)[1]) for n in names)


async def test_json_helpers_match_the_same_rows_on_both_databases():
    tag, _ = await _runs([
        {"v": 3, "s": "3", "flag": True, "k": "甲"},       # 0
        {"v": "3", "s": 3, "flag": 1, "k": None},          # 1
        {"v": 4, "flag": 0, "k": "乙", "uses": ["r-1", "r-22"]},  # 2
        {"flag": False},                                   # 3
        {},                                                # 4
        None,                                              # 5（JSON 的 null）
        {"v": True, "n": 1.0, "b": False},                 # 6
        {"v": 1, "n": 1, "b": 0},                          # 7
    ])
    # 按 JSON 的类型比：数字 3 不等于字符串 "3"；true 不等于 1、false 不等于 0（SQLite 的 json_extract 单用时分不开）；
    # 数 1 等于 1.0
    assert await _match(tag, json_eq(Run.origin, "v", 3)) == [0]
    assert await _match(tag, json_eq(Run.origin, "v", "3")) == [1]
    assert await _match(tag, json_eq(Run.origin, "s", "3")) == [0]
    assert await _match(tag, json_eq(Run.origin, "v", 1)) == [7]
    assert await _match(tag, json_eq(Run.origin, "v", True)) == [6]
    assert await _match(tag, json_eq(Run.origin, "b", False)) == [6]
    assert await _match(tag, json_eq(Run.origin, "b", 0)) == [7]
    assert await _match(tag, json_eq(Run.origin, "n", 1)) == [6, 7]
    assert await _match(tag, json_eq(Run.origin, "n", 1.0)) == [6, 7]
    with pytest.raises(TypeError):
        json_eq(Run.origin, "v", {"a": 1})
    # None：没有这个键、或者值是 null
    assert await _match(tag, json_eq(Run.origin, "k", None)) == [1, 3, 4, 5, 6, 7]
    # 标记位：true 和非零的数算真
    assert await _match(tag, json_flag(Run.origin, "flag")) == [0, 1]
    from sqlalchemy import not_

    assert await _match(tag, not_(json_flag(Run.origin, "flag"))) == [2, 3, 4, 5, 6, 7]
    # 文本：字符串是它本身，数、true / false 是 JSON 写法（两种库都是）
    assert await _match(tag, json_text(Run.origin, "k") == "乙") == [2]
    assert await _match(tag, func.coalesce(json_text(Run.origin, "k"), "").in_(["甲", ""])) == [0, 1, 3, 4, 5, 6, 7]
    assert await _match(tag, json_text(Run.origin, "v") == "3") == [0, 1]
    assert await _match(tag, json_text(Run.origin, "v") == "1") == [7]
    assert await _match(tag, json_text(Run.origin, "v") == "true") == [6]
    assert await _match(tag, json_text(Run.origin, "n") == "1.0") == [6]
    assert await _match(tag, json_text(Run.origin, "uses").contains("r-22", autoescape=True)) == [2]
    # 是不是字符串：input.question 偶数行是数字、奇数行是字符串
    assert await _match(tag, json_is_string(Run.input, "question")) == [1, 3, 5, 7]


async def test_json_columns_keep_the_order_of_keys():
    """JSON 列存原文：对象里键的先后读回来不变（JSONB 会重排，应用里按键的先后出列名、出段落的地方就乱了）。"""
    keys = ["单号", "金额", "电话", "b", "a", "aa"]
    async with SessionLocal() as session:
        row = Run(workflow_name="键序", origin={k: i for i, k in enumerate(keys)})
        session.add(row)
        await session.commit()
        rid = row.id
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.origin).where(Run.id == rid))).scalar_one()
    assert list(got) == keys


async def test_json_null_and_sql_null_stay_apart():
    """JSON 列写 None 存的是 JSON 的 null（不是 SQL 的 NULL），两种库一样：按 IS NULL 筛不到它。"""
    async with SessionLocal() as session:
        row = Run(workflow_name="空值", origin=None)
        session.add(row)
        await session.commit()
        rid = row.id
    async with SessionLocal() as session:
        is_sql_null = (await session.execute(select(Run.origin.is_(None)).where(Run.id == rid))).scalar_one()
        raw = (await session.execute(text("SELECT CAST(origin AS TEXT) FROM runs WHERE id = :i"), {"i": rid})).scalar_one()
    assert is_sql_null is False and raw == "null"


async def test_long_strings_and_big_integers_are_kept():
    """String(n) 超长、Integer 过 21 亿：SQLite 照存，PostgreSQL 上也照存（列不限长、是 BIGINT）。"""
    long_name = "长" * 500                  # workflow_name 是 String(200)
    big = 5_000_000_000
    async with SessionLocal() as session:
        row = Run(workflow_name=long_name, last_seq=big)
        session.add(row)
        await session.commit()
        rid = row.id
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.workflow_name, Run.last_seq).where(
            Run.id == rid, Run.last_seq > big - 1))).one()
    assert tuple(got) == (long_name, big)


async def test_non_finite_floats_in_json_are_written():
    """JSON 里的 NaN：SQLite 照存；PostgreSQL 的 json 不收，写成 null（同浏览器的 JSON.stringify），不让整条写入失败。"""
    async with SessionLocal() as session:
        row = Run(workflow_name="非有限", origin={"x": float("nan"), "y": 1.5, "s": "a\x00b"})
        session.add(row)
        await session.commit()
        rid = row.id
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.origin).where(Run.id == rid))).scalar_one()
    assert got["y"] == 1.5
    if db_base.engine.dialect.name == "postgresql":
        assert got["x"] is None and got["s"] == "ab"
    else:
        assert got["x"] != got["x"] and got["s"] == "a\x00b"      # NaN、NUL 原样


async def test_unique_violation_names_the_table_and_columns():
    sid = uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add(TableRecipe(source_id=sid, seq=1, recipe={}, recipe_sha256="c" * 64))
        await session.commit()
        session.add(TableRecipe(source_id=sid, seq=1, recipe={}, recipe_sha256="c" * 64))
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
    assert unique_violation(caught.value, "table_recipes", "source_id", "seq")
    assert not unique_violation(caught.value, "table_recipes", "source_id")
    assert not unique_violation(caught.value, "data_sources", "source_id", "seq")


async def test_order_by_puts_nulls_where_sqlite_does():
    tag = uuid.uuid4().hex[:12]
    from datetime import datetime, timezone

    async with SessionLocal() as session:
        for i, started in enumerate([datetime(2026, 1, 2, tzinfo=timezone.utc), None,
                                     datetime(2026, 1, 1, tzinfo=timezone.utc)]):
            session.add(Run(workflow_name=f"排序-{tag}-{i}", started_at=started))
        await session.commit()
    async with SessionLocal() as session:
        where = Run.workflow_name.like(f"排序-{tag}-%")
        up = (await session.execute(select(Run.workflow_name).where(where).order_by(Run.started_at))).scalars().all()
        down = (await session.execute(select(Run.workflow_name).where(where)
                                      .order_by(Run.started_at.desc()))).scalars().all()
    assert [n[-1] for n in up] == ["1", "2", "0"]          # 升序：NULL 在前
    assert [n[-1] for n in down] == ["0", "2", "1"]        # 降序：NULL 在后


async def test_text_sorts_and_compares_byte_by_byte_like_sqlite():
    """大写在小写前、标点按码位：两种库排出来一样（PostgreSQL 上不看建库时的区域设置）。"""
    tag = uuid.uuid4().hex[:12]
    names = ["b", "B", "a-c", "ab", "_z", "Z", "中"]
    async with SessionLocal() as session:
        for name in names:
            session.add(Run(workflow_name=f"字节-{tag}-{name}"))
        await session.commit()
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.workflow_name).where(Run.workflow_name.like(f"字节-{tag}-%"))
                                     .order_by(Run.workflow_name))).scalars().all()
        lower = (await session.execute(select(func.count()).select_from(Run).where(
            Run.workflow_name.like(f"字节-{tag}-%"), Run.workflow_name < f"字节-{tag}-a"))).scalar_one()
    assert [g.removeprefix(f"字节-{tag}-") for g in got] == sorted(names)
    assert lower == sum(1 for n in names if n < "a")


async def test_nul_characters_in_text_columns():
    """文本列里的 NUL：SQLite 照存；PostgreSQL 的 text / varchar 不收，写入时去掉（同 JSON 列），不让整条写入失败。"""
    async with SessionLocal() as session:
        row = Run(workflow_name="带\x00NUL\x00的名字", error="错\x00误")
        session.add(row)
        await session.commit()
        rid = row.id
        await session.execute(text("UPDATE runs SET end_reason = :r WHERE id = :i"), {"r": "x\x00y", "i": rid})
        await session.commit()
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.workflow_name, Run.error, Run.end_reason).where(Run.id == rid))).one()
    if db_base.engine.dialect.name == "postgresql":
        assert tuple(got) == ("带NUL的名字", "错误", "xy")
    else:
        assert tuple(got) == ("带\x00NUL\x00的名字", "错\x00误", "x\x00y")


async def test_case_insensitive_search_folds_like_the_database_does():
    """contains_ci：ASCII 两种库都不分大小写；非 ASCII（É/é、Ж/ж、全角Ａ/ａ）按库自己会不会折——SQLite 看构建，PostgreSQL 看
    建库的区域设置（列本身是 COLLATE "C"，列上的 ILIKE 只认 ASCII，所以模式带 COLLATE "default"）。% 和 _ 按字面。"""
    from app.db.portable import contains_ci

    tag = uuid.uuid4().hex[:12]
    async with SessionLocal() as session:
        session.add(Run(workflow_name=f"大小写-{tag}-Éclair Жук Ａｂ Report_100%"))
        await session.commit()
        pg = db_base.engine.dialect.name == "postgresql"
        folds = (await session.execute(text(
            "SELECT lower('ÉЖＡ' COLLATE \"default\") = 'éжａ'" if pg else "SELECT lower('ÉЖＡ') = 'éжａ'"))).scalar()

        async def hit(needle: str) -> bool:
            return bool((await session.execute(select(func.count()).select_from(Run).where(
                Run.workflow_name.like(f"大小写-{tag}-%"), contains_ci(Run.workflow_name, needle)))).scalar())

        assert await hit("report") and await hit("REPORT_100%")
        assert not await hit("report-100") and not await hit("100%%")
        assert await hit("éclair") == bool(folds) and await hit("жук") == bool(folds) and await hit("ａｂ") == bool(folds)
        assert await hit("Éclair") and await hit("Жук")


async def test_timestamps_read_back_as_aware_utc():
    from datetime import datetime, timedelta, timezone

    local = datetime(2026, 3, 4, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    async with SessionLocal() as session:
        row = Run(workflow_name="时区", started_at=local)
        session.add(row)
        await session.commit()
        rid = row.id
    async with SessionLocal() as session:
        got = (await session.execute(select(Run.started_at).where(Run.id == rid, Run.started_at == local))).scalar_one()
    assert got == local and got.tzinfo == timezone.utc and got.hour == 1


# --------------------------------------------------------------------------
# PostgreSQL 上的老库迁移
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_pg():
    """同一台服务器上另建一个空库，给迁移测试造「老版本建的库」。只在 PostgreSQL 时有。"""
    if not PG:
        pytest.skip("要 AGENTLAB_TEST_DATABASE_URL（scripts/test-postgres.sh）")
    import psycopg
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from app.db.urls import libpq_conninfo

    admin = make_url(db_base.settings.db_url)
    name = f"{admin.database}_mig_{uuid.uuid4().hex[:8]}"
    conninfo = libpq_conninfo(admin.render_as_string(hide_password=False))
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin.set(drivername="postgresql+psycopg", database=name)
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with psycopg.connect(conninfo, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _columns(conn, table: str) -> dict[str, tuple[str, str | None]]:
    rows = conn.execute(text("SELECT column_name, data_type, column_default FROM information_schema.columns "
                             "WHERE table_name = :t"), {"t": table}).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_old_postgres_databases_get_the_missing_columns_and_indexes(scratch_pg):
    """老版本建的 PostgreSQL 库（少几列、少索引、有并发留下的重复行）：补上的列类型按模型的、缺省值回填，布尔的缺省是
    false 不是 0，索引补上，唯一索引建之前先去重；再跑一遍什么都不做。"""
    with scratch_pg.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR PRIMARY KEY, workflow_id VARCHAR, version BIGINT, "
                          "status VARCHAR, last_seq BIGINT)"))
        conn.execute(text("INSERT INTO runs (id, status, last_seq) VALUES ('r1', 'succeeded', 9)"))
        conn.execute(text("CREATE TABLE schedules (id VARCHAR PRIMARY KEY, name VARCHAR)"))
        conn.execute(text("INSERT INTO schedules (id, name) VALUES ('s1', '虚构的定时')"))
        conn.execute(text("CREATE TABLE conversations (id VARCHAR PRIMARY KEY, kind VARCHAR, archived BOOLEAN, "
                          "workflow_id VARCHAR, last_active_at TIMESTAMP)"))
        for cid, at in (("c1", "2026-01-01"), ("c2", "2026-01-02")):
            conn.execute(text("INSERT INTO conversations (id, kind, archived, last_active_at) "
                              "VALUES (:i, 'app', false, :at)"), {"i": cid, "at": at})
        db_base._migrate(conn)
        db_base._migrate(conn)
        runs = _columns(conn, "runs")
        assert runs["options"][0] == "json" and runs["options"][1].startswith("'{}'")
        assert runs["schedule_id"][0] == "character varying" and runs["manifest_seq"][0] == "bigint"
        assert runs["started_by"][0] == "character varying"
        assert conn.execute(text("SELECT CAST(options AS TEXT), run_class FROM runs WHERE id = 'r1'")).one() \
            == ("{}", "exploratory")
        schedules = _columns(conn, "schedules")
        assert schedules["trigger_kind"][1].startswith("'cron'")
        assert conn.execute(text("SELECT trigger_kind FROM schedules")).scalar_one() == "cron"
        indexes = {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))}
        assert {"ix_runs_schedule_id", "ix_runs_wf_ver_status"} <= indexes
        # 同一个人在同一个应用里两段没归档的助手对话：created_by 是迁移补上的列（NULL 不受约束），唯一索引照样建上
        convs = {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'conversations'"))}
        assert "ux_conversations_app_owner" in convs


def test_old_postgres_duplicates_are_settled_before_the_unique_index(scratch_pg):
    with scratch_pg.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id VARCHAR PRIMARY KEY, kind VARCHAR, archived BOOLEAN, "
                          "app_id VARCHAR, created_by VARCHAR, last_active_at TIMESTAMP)"))
        for cid, at in (("c1", "2026-01-01"), ("c2", "2026-01-03"), ("c3", "2026-01-02")):
            conn.execute(text("INSERT INTO conversations VALUES (:i, 'app', false, 'a1', '甲', :at)"),
                         {"i": cid, "at": at})
        db_base._migrate(conn)
        kept = conn.execute(text("SELECT id FROM conversations WHERE archived = false")).scalars().all()
        assert kept == ["c2"], "留最近活跃的那一段，别的归档"
        with pytest.raises(Exception, match="ux_conversations_app_owner"):
            with conn.begin_nested():
                conn.execute(text("INSERT INTO conversations VALUES ('c4', 'app', false, 'a1', '甲', '2026-02-01')"))


async def test_new_postgres_databases_have_the_partial_unique_indexes():
    """新库靠 create_all：部分唯一索引在 PostgreSQL 上也是部分索引（postgresql_where），条件里的布尔写成 false。"""
    if not PG:
        pytest.skip("要 AGENTLAB_TEST_DATABASE_URL（scripts/test-postgres.sh）")
    async with db_base.engine.connect() as conn:
        rows = await conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE 'ux_%'"))
        got = {r[0]: r[1] for r in rows}
    assert "WHERE (((kind)::text = 'app'::text) AND (archived = false))" in got["ux_conversations_app_owner"]
    assert "WHERE ((status)::text = ANY" in got["ux_catalog_draft_jobs_active"]
