"""剖析事实进 look 的表详情（docs/design/next-round/05-已知遗留.md (e)）。

剖析记下的行数估算和依据、各日期列的范围以前只在 catalog_profile_tasks 里，助手写 SQL 时不知道表有多大、数据到哪天。现在
经 schema_view.profile_lines 成一节，look、运行时 db_schema、/schema、表助手四处同一段文字，附「只供写查询参考……不能
作为回答里的数」。这组守的是：

- 四处文字一致；没有剖析结果时表详情和以前一字不差；
- 遮罩按渲染那一刻重滤：剖析之后才给某一列加遮罩，那一列的范围不出现；
- 上传表格只在剖析时的快照和这次读的快照一致时给，不一致（含升级前没记快照的）写「剖析早于当前版本，不给」；
- 剖析任务开始一张表时记下上传表格的快照；
- 「上次剖析」取真正开始过的最近一次（同表清单）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog_profile_jobs, schema_view
from app.data.schema_view import PROFILE_HINT_PROMPT, PROFILE_STALE_PROMPT
from app.db.base import SessionLocal, new_id
from app.db.models import CatalogProfileJob, CatalogProfileTask, DataSource
from app.main import app
from tests.fixtures.sources import drop_source, introspect_now

FACT_LINE = "数据剖析（2026-10-01）：约 12,345 行（数据库统计估算）；order_date 从 2024-01-01 到 2026-09-30；paid_at 从 2024-01-02 到 2026-09-29 18:00:01"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(client, tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_date TEXT, paid_at TEXT, amount REAL)")
    db.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT)")
    db.commit()
    db.close()
    name = f"facts_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": str(path)})
    sid = r.json()["id"]
    await introspect_now(client, sid)
    yield sid, name
    await drop_source(sid)


async def _profiled(sid: str, *, started: bool = True, snapshot: str | None = None, rows: int = 12345,
                    at: str = "2026-10-01T08:00:00+00:00") -> None:
    """记一次剖析：orders 有行数估算和两个日期列的范围。"""
    when = datetime.fromisoformat(at)
    async with SessionLocal() as session:
        job = CatalogProfileJob(id=new_id(), source_id=sid, status="succeeded", scope="selected",
                                created_at=when, finished_at=when)
        session.add(job)
        await session.flush()
        session.add(CatalogProfileTask(
            job_id=job.id, seq=0, table_name="orders", status="ok" if started else "cancelled",
            row_estimate={"rows": rows, "method": "stats", "at_least": None} if started else None,
            date_ranges=[{"column": "order_date", "min": "2024-01-01", "max": "2026-09-30"},
                         {"column": "paid_at", "min": "2024-01-02 00:00:00", "max": "2026-09-29 18:00:01"}]
            if started else None,
            snapshot_id=snapshot, started_at=when if started else None, finished_at=when))
        await session.commit()


async def _row(sid: str) -> DataSource:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        session.expunge(row)
        return row


async def _four_places(client, sid: str, name: str) -> dict[str, str]:
    from app.data import catalog_assistant
    from app.look.service import LookService
    from app.look.tables import TableKind
    from app.tools.datasource import build_datasource_tools
    from app.tools.registry import ToolContext, Versions

    row = await _row(sid)
    looks = LookService([await TableKind.load([row])])
    look = (await looks.look({"op": "look", "need": "订单", "targets": [
        {"kind": "table", "source": name, "name": "orders"}]})).text
    async with SessionLocal() as session:
        (tool,) = await build_datasource_tools([f"db_schema__{name}"], ToolContext(data_versions=Versions.CURRENT),
                                               session)
        assistant = (await catalog_assistant.prepare(session, row, row, "orders")).system
    tool_text = await tool.coroutine(table="orders")
    api = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "orders"})).json()["detail"]
    return {"look": look, "db_schema": tool_text, "/schema": api, "assistant": assistant}


async def test_the_same_profile_facts_in_four_places(client, shop):
    sid, name = shop
    before = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "orders"})).json()["detail"]
    await _profiled(sid)
    places = await _four_places(client, sid, name)
    for where, text in places.items():
        assert FACT_LINE in text, f"{where} 里没有那一段剖析事实：{text[-400:]}"
        assert PROFILE_HINT_PROMPT in text, f"{where} 里没有「只供写查询参考」"
    after = places["/schema"]
    assert after.startswith(before) and after != before, "剖析事实是追加的一节，表详情原有的部分不动"

    # 剖析之后才给 paid_at 加遮罩：渲染那一刻重滤，它的范围不出现
    r = await client.patch(f"/api/datasources/{sid}", json={"options": {"mask_columns": "paid_at"}})
    assert r.status_code == 200, r.text
    for where, text in (await _four_places(client, sid, name)).items():
        assert "paid_at 从" not in text, f"{where} 给了遮罩列的范围"
        assert "order_date 从 2024-01-01 到 2026-09-30" in text, where


async def test_without_profile_results_the_detail_is_unchanged(shop):
    sid, _ = shop
    row = await _row(sid)
    plain = schema_view.table_detail(row, "orders")
    async with SessionLocal() as session:
        facts = await catalog_profile_jobs.last_facts(session, sid)
    assert facts == {}
    assert schema_view.table_detail(row, "orders", profile=facts) == plain
    assert schema_view.table_detail(row, "orders", profile={"stores": {"row_estimate": {"rows": 1}}}) == plain


async def test_last_facts_follow_the_last_profile_that_really_started(shop):
    sid, _ = shop
    await _profiled(sid, rows=500, at="2026-09-01T08:00:00+00:00")
    await _profiled(sid, started=False, at="2026-10-02T08:00:00+00:00")
    async with SessionLocal() as session:
        facts = await catalog_profile_jobs.last_facts(session, sid)
        assert await catalog_profile_jobs.last_facts(session, sid, []) == {}
    assert facts["orders"]["row_estimate"]["rows"] == 500 and facts["orders"]["at"].startswith("2026-09-01"), \
        "没开始就停了的那一项不挡掉上一次真正剖析的结果"


def test_upload_sources_only_get_facts_of_the_current_snapshot():
    facts = {"at": "2026-10-01T00:00:00+00:00", "snapshot_id": "snap-a",
             "row_estimate": {"rows": 3, "method": "count", "at_least": None}, "date_ranges": []}
    assert schema_view.profile_lines(facts, set(), "snap-a", upload=True)[0].endswith("3 行（逐行数出）")
    assert schema_view.profile_lines(facts, set(), "snap-b", upload=True) == ["  " + PROFILE_STALE_PROMPT]
    assert schema_view.profile_lines({**facts, "snapshot_id": None}, set(), "snap-a", upload=True) \
        == ["  " + PROFILE_STALE_PROMPT], "升级前没记快照的剖析，上传表格在重新剖析之前不给"
    at_least = {**facts, "row_estimate": {"rows": None, "method": "count", "at_least": 100000}}
    assert "至少 100,000 行（数到上限为止）" in schema_view.profile_lines(at_least, set(), None)[0]
    unknown = {**facts, "row_estimate": {"rows": None, "method": "unknown", "at_least": None}}
    assert schema_view.profile_lines(unknown, set(), None) == [], "什么都没有就不出这一节"

    view = SimpleNamespace(name="up", origin="upload", snapshot_id="snap-b", options={}, kind="sqlite",
                           schema_cache={"tables": {"t": {"qualified": "t", "columns": [{"name": "id", "type": "INTEGER"}]}}})
    text = schema_view.table_detail(view, "t", profile={"t": facts})
    assert PROFILE_STALE_PROMPT in text and "3 行" not in text


async def test_profile_job_records_the_snapshot_of_an_upload_table(shop):
    sid, _ = shop
    from app.data.catalog_profile_jobs import _ProfileRun

    async with SessionLocal() as session:
        job = CatalogProfileJob(id=new_id(), source_id=sid, status="running", scope="selected",
                                created_at=datetime.now(timezone.utc))
        session.add(job)
        await session.flush()
        session.add(CatalogProfileTask(job_id=job.id, seq=0, table_name="orders", status="pending"))
        await session.commit()
    run = _ProfileRun(job.id)
    run.cx = SimpleNamespace(source=SimpleNamespace(snapshot_id="snap-z"))
    async with SessionLocal() as session:
        await run._start_table(session, 0)
    async with SessionLocal() as session:
        task = (await session.get(CatalogProfileJob, job.id)) and (await session.execute(
            __import__("sqlalchemy").select(CatalogProfileTask).where(CatalogProfileTask.job_id == job.id))).scalar_one()
    assert task.snapshot_id == "snap-z" and task.status == "running"
