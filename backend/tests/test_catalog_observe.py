"""取值观察：剖析引擎的四类观察检查、口径、撤销「已列全」、遮罩（next-round 03 B2）。

SQLite 夹具（虚构的店铺订单表：状态码、支付日、金额；支付日打头有索引）走真实的后台剖析任务（范围 observe）：

- 四类观察各记一条，口径照实（小表整表；大表有索引的业务日期走 35 天窗口，没有就按存储顺序的前 N 行）；
- 确认过、标了已列全的码值列也观察；插进一个新取值后再观察：记一条 codes_new（要处理）、撤销已列全（目录版本 +1，署名
  「取值观察」，值、含义、状态都不动），SQL 检查不再按旧码值表提示 unknown_code；
- 口径不同不比；遮罩列不出现在任何一条观察查询里（看留痕）；
- PostgreSQL / Oracle 的大表没有可用的窗口时生成块采样 SQL，MySQL 退回前 N 行（假的执行上下文，只看拼出来的 SQL 和口径）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.data import catalog, catalog_profile, catalog_profile_jobs, catalog_profile_observe as observe, sqlcheck
from app.data import table_versions
from app.data.engine import MASK_COLUMNS_OPTION
from app.db.base import SessionLocal
from app.db.models import CatalogProfileTask, DataSource
from app.db.models_sources import SourceChange, TableObservation
from app.main import app
from tests.fixtures.sources import drop_source, introspect_now

TODAY = date.today()


def _build(path, *, rows: int = 60, index: bool = True) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR(10), paid_on DATE, amount NUMERIC(10, 2),"
               " secret VARCHAR(10))")
    if index:
        db.execute("CREATE INDEX ix_orders_paid_on ON orders (paid_on)")
    for i in range(1, rows + 1):
        day = (TODAY - timedelta(days=i % 30 + 1)).isoformat()
        db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", (i, "void" if i % 4 == 0 else "paid", day, i, "x"))
    db.commit()
    db.close()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make(client, path, *, max_scan_rows: int = 100_000, masked: list[str] | None = None) -> str:
    options = {"catalog_profile": {"enabled": True, "max_scan_rows": max_scan_rows, "sample_size": 10}}
    if masked:
        options[MASK_COLUMNS_OPTION] = masked
    r = await client.post("/api/datasources", json={"name": f"obs_{uuid.uuid4().hex[:8]}", "kind": "sqlite",
                                                    "database": str(path), "readonly": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    await introspect_now(client, sid)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.options = {**(row.options or {}), **options}
        await session.commit()
        await catalog.write_entry(session, sid, "orders", {
            "business_date": catalog.make_item({"column": "paid_on"}, "human"),
            "columns": {"status": {"codes": catalog.make_item({"paid": "已支付", "void": "作废"}, "human",
                                                              complete=True)},
                        "secret": {"codes": catalog.make_item({"x": "保密"}, "human")}},
            "valid_filter": catalog.make_item("status <> 'void'", "human"),
        }, if_version=0, actor="测试员")
    return sid


@pytest.fixture
async def shop(client, tmp_path):
    path = tmp_path / "obs.db"
    _build(path)
    sid = await _make(client, path)
    yield sid, path
    await drop_source(sid)


async def _observe(sid: str, scope: str = "observe") -> str:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        view = await table_versions.resolve_source(session, row)
        job, _ = await catalog_profile_jobs.create_job(session, row, view, tables=None, scope=scope,
                                                       settings=catalog_profile.ensure_enabled(row), actor="测试员")
    catalog_profile_jobs.manager.start(job.id)
    await catalog_profile_jobs.manager.wait(job.id, timeout=60)
    return job.id


async def _obs(sid: str) -> list[TableObservation]:
    async with SessionLocal() as session:
        return list((await session.execute(select(TableObservation).where(TableObservation.source_id == sid)
                                           .order_by(TableObservation.at))).scalars())


async def _age(sid: str, hours: int = 13) -> None:
    """把以前的观察挪到 hours 小时前：观察到期了才会再观察。"""
    async with SessionLocal() as session:
        for r in (await session.execute(select(TableObservation).where(TableObservation.source_id == sid))).scalars():
            r.at = r.at - timedelta(hours=hours)
        await session.commit()


async def test_four_observations_with_basis_and_no_change_on_first_run(shop):
    sid, _ = shop
    job_id = await _observe(sid)
    got = {(o.kind, o.column_name): o for o in await _obs(sid)}
    assert set(got) == {("rows", ""), ("codes_observe", "status"), ("codes_observe", "secret"),
                        ("freshness", "paid_on"), ("daily_volume", "paid_on")}
    assert got[("rows", "")].data["rows"] == 60 and got[("rows", "")].basis_key == "count"
    status = got[("codes_observe", "status")]
    assert status.basis_key == "full" and dict(map(tuple, status.data["values"])) == {"paid": 45, "void": 15}
    assert got[("freshness", "paid_on")].data["max"] == (TODAY - timedelta(days=1)).isoformat()
    days = got[("daily_volume", "paid_on")].data["days"]
    assert sum(days.values()) == 60 and got[("daily_volume", "paid_on")].basis_key == "window:paid_on:35"
    assert all(o.job_id == job_id for o in got.values())
    assert got[("codes_observe", "status")].basis["query_refs"], "留痕：这条观察是哪几条查询得出的"
    async with SessionLocal() as session:
        changes = list((await session.execute(select(SourceChange).where(SourceChange.source_id == sid))).scalars())
        [task] = (await session.execute(select(CatalogProfileTask).where(CatalogProfileTask.job_id == job_id))).scalars()
    assert changes == [], "第一次只建基线"
    assert {c["kind"] for c in task.checks} == set(observe.OBSERVE_KINDS), "只观察：不做关系、码值、日期的剖析"
    assert task.status == "ok"


async def test_new_code_in_a_complete_list_revokes_complete(shop):
    sid, path = shop
    await _observe(sid)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO orders VALUES (999, 'held', ?, 1, 'x')", ((TODAY - timedelta(days=2)).isoformat(),))
    db.commit()
    db.close()
    async with SessionLocal() as session:
        before = await catalog.read_entry(session, sid, "orders")
    # 没到期不再观察
    with pytest.raises(catalog_profile_jobs.ProfileNothing):
        await _observe(sid)
    await _age(sid)
    await _observe(sid)
    async with SessionLocal() as session:
        [change] = (await session.execute(select(SourceChange).where(
            SourceChange.source_id == sid, SourceChange.kind == "codes_new"))).scalars()
        after = await catalog.read_entry(session, sid, "orders")
        row = await session.get(DataSource, sid)
    assert change.column_name == "status" and change.severity == "action" and change.after["codes"] == ["held"]
    assert change.after["complete_revoked"] is True and change.after["catalog_version"] == after.version
    assert change.affects["rules_touched"] is True, "有效记录条件用到了这一列"
    assert change.detected_by["job_kind"] == "profile" and change.basis["key"] == "full"
    assert after.version == before.version + 1 and after.updated_by == observe.OBSERVER
    item = after.notes["columns"]["status"]["codes"]
    assert "complete" not in item and item["value"] == {"paid": "已支付", "void": "作废"}, "值、含义不动"
    assert (item["source"], item["status"]) == ("human", "confirmed"), "状态、来源不动"
    checker = sqlcheck.SqlChecker(kind="sqlite", schema_cache=row.schema_cache, notes={"orders": after.notes},
                                  source_name=row.name)
    assert not [c for c in checker.check("SELECT COUNT(*) FROM orders WHERE status = 'held'")
                if c.code == "unknown_code"], "已列全撤销以后，SQL 检查不再照旧码值表提示"
    old = sqlcheck.SqlChecker(kind="sqlite", schema_cache=row.schema_cache, notes={"orders": before.notes},
                              source_name=row.name)
    assert [c for c in old.check("SELECT COUNT(*) FROM orders WHERE status = 'held'") if c.code == "unknown_code"]

    # 同一个新码值再测到：不另起一条
    await _age(sid)
    await _observe(sid)
    async with SessionLocal() as session:
        same = list((await session.execute(select(SourceChange).where(
            SourceChange.source_id == sid, SourceChange.kind == "codes_new"))).scalars())
    assert len(same) == 1


async def test_masked_columns_never_appear_in_observation_queries(client, tmp_path):
    path = tmp_path / "masked.db"
    _build(path)
    sid = await _make(client, path, masked=["secret"])
    try:
        job_id = await _observe(sid)
        async with SessionLocal() as session:
            [task] = (await session.execute(select(CatalogProfileTask).where(
                CatalogProfileTask.job_id == job_id))).scalars()
        assert task.query_log and not any("secret" in q["sql"].lower() for q in task.query_log)
        [skip] = [c for c in task.checks if c["kind"] == "codes_observe" and c["target"] == "secret"]
        assert skip["state"] == "skipped" and skip["reason"] == "masked"
        assert ("codes_observe", "secret") not in {(o.kind, o.column_name) for o in await _obs(sid)}
    finally:
        await drop_source(sid)


async def test_large_table_basis_window_or_first_rows_and_no_cross_basis_compare(client, tmp_path):
    with_index, without = tmp_path / "big1.db", tmp_path / "big2.db"
    _build(with_index, rows=60, index=True)
    _build(without, rows=60, index=False)
    a = await _make(client, with_index, max_scan_rows=20)
    b = await _make(client, without, max_scan_rows=20)
    try:
        await _observe(a)
        await _observe(b)
        got_a = {(o.kind, o.column_name): o for o in await _obs(a)}
        got_b = {(o.kind, o.column_name): o for o in await _obs(b)}
        assert got_a[("codes_observe", "status")].basis_key == "window:paid_on:35", "大表、业务日期打头有索引：窗口"
        assert got_a[("rows", "")].data["at_least"] == 21
        assert got_b[("codes_observe", "status")].basis_key.startswith("first_rows:"), \
            "SQLite 没有块采样：按存储顺序的前 N 行，照实写口径"
        assert ("freshness", "paid_on") not in got_b and ("daily_volume", "paid_on") not in got_b, \
            "大表、业务日期没有索引：不看最新业务日期和按日行数"
        # 改了口径（整表统计上限调大，变成小表）：和以前的不比
        async with SessionLocal() as session:
            row = await session.get(DataSource, b)
            row.options = {**row.options, "catalog_profile": {**row.options["catalog_profile"], "max_scan_rows": 1000}}
            await session.commit()
        db = sqlite3.connect(without)
        db.executemany("INSERT INTO orders VALUES (?, 'void', ?, 1, 'x')",
                       [(1000 + i, (TODAY - timedelta(days=1)).isoformat()) for i in range(40)])
        db.commit()
        db.close()
        await _age(b)
        await _observe(b)
        async with SessionLocal() as session:
            shifts = list((await session.execute(select(SourceChange).where(
                SourceChange.source_id == b, SourceChange.kind == "codes_shift"))).scalars())
        assert shifts == [], "口径从前 N 行换成了整表：不跨口径比"
    finally:
        await drop_source(a)
        await drop_source(b)


class _FakeCx:
    """只看拼出来的 SQL：大表、业务日期没有索引。"""

    def __init__(self, kind: str) -> None:
        self.dialect = catalog_profile.SqlDialect(kind)
        self.settings = catalog_profile.ProfileSettings(enabled=True, max_scan_rows=1000, sample_size=100)
        self.sql: list[str] = []
        self.table_notes = {"business_date": {"value": {"column": "paid_on"}, "source": "human", "status": "confirmed"}}

    def is_masked(self, column: str) -> bool:
        return False

    def scan_cap(self, size):
        return 1000

    async def run(self, sql, *, max_rows=1, **kw):
        self.sql.append(sql)
        return SimpleNamespace(rows=[("A", 900), ("B", 100)], row_count=2)


@pytest.mark.parametrize("kind,method", [("postgresql", "sample"), ("oracle", "sample"), ("mysql", "first_rows")])
async def test_large_table_without_window_samples_on_pg_and_oracle(kind, method):
    cx = _FakeCx(kind)
    meta = {"columns": [{"name": "status", "type": "VARCHAR(2)"}, {"name": "paid_on", "type": "DATE"}],
            "index_first_columns": ["id"]}
    result = SimpleNamespace(observations=[], query_log=[])
    size = catalog_profile.TableSize(500_000, "stats")
    await observe._codes(cx, "orders", "status", meta, size, result)
    [obs] = result.observations
    assert obs["basis"]["method"] == method
    if kind == "postgresql":
        assert "TABLESAMPLE SYSTEM (0.2)" in cx.sql[0] and obs["basis_key"] == "sample:0.2"
    elif kind == "oracle":
        assert "SAMPLE BLOCK (0.2)" in cx.sql[0] and "ROWNUM" in cx.sql[0]
    else:
        assert "LIMIT 1000" in cx.sql[0] and obs["basis_key"] == "first_rows:1000"
    assert observe.sample_percent(500_000, 1000) == 0.2 and observe.sample_percent(800, 1000) is None


async def test_observation_is_not_the_last_profile_and_reaches_the_table_detail(shop):
    """只做了观察的那一项不算「上次剖析」（不顶掉剖析事实）；观察结论经 schema_view 和剖析事实同一节进表详情（X7），
    遮罩在渲染那一刻再核。"""
    from app.data import schema_view

    sid, _ = shop
    await _observe(sid)
    async with SessionLocal() as session:
        lasts = await catalog_profile_jobs.last_profiles(session, sid)
        facts = await catalog_profile_jobs.last_facts(session, sid)
        row = await session.get(DataSource, sid)
    assert "orders" not in lasts, "只观察的那一项不是一次剖析"
    observed = facts["orders"]["observed"]
    assert observed["freshness"][0]["column"] == "paid_on" and observed["rows"]["rows"] == 60
    lines = schema_view.profile_lines(facts["orders"], set(), None)
    latest = (TODAY - timedelta(days=1)).isoformat()
    assert lines[0].strip().startswith("取值观察（") and f"最新业务日期 paid_on 到 {latest}" in lines[0], lines
    assert "60 行（逐行数出）" in lines[0] and "不能作为回答里的数" in lines[1]
    assert not any("paid_on" in line for line in schema_view.profile_lines(facts["orders"], {"paid_on"}, None)), \
        "观察之后才加遮罩的列：渲染那一刻滤掉"
    detail = schema_view.table_detail(row, "orders", profile=facts)
    assert f"最新业务日期 paid_on 到 {latest}" in detail
