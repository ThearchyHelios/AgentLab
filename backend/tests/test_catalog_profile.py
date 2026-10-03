"""数据剖析接口：POST /api/datasources/{id}/catalog/profile（阶段 2A）。

剖析是数据目录里唯一对业务库发查询的地方，这里逐条钉住方案定的约束：默认关闭、只读、每条查询都过守卫、
查询次数和单条时限、总时长、遮罩的列不取样、大表不做整表统计、同一个数据源同时只有一次剖析；
以及剖析的结论怎么进目录：命名推断对上数据的升为有确证（来源记为数据剖析，和外键分开）、对不上的写明
覆盖率、人工确认过的只补覆盖率和基数、状态列得到码值候选、只有一个日期列的表提议业务日期。

所有测试都在合成的景区库上做（tests/fixtures/catalog/scenic.py），数据和名称全部虚构。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.data import catalog, catalog_profile
from app.data import engine as data_engine
from app.data.catalog_profile import PROFILE_OPTION, ProfileSettings
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app as _fastapi_app
from tests.fixtures.catalog import scenic
from tests.fixtures.sources import drop_source

ACTOR = {"X-Actor": quote("周宁")}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


def _on(**settings) -> dict:
    return {PROFILE_OPTION: {"enabled": True, **settings}}


@pytest.fixture
async def make_source(client):
    """登记一个接上 path 的数据源并走真实的探查接口；draft 给了就先起草这些表的目录。"""
    made: list[str] = []

    async def _make(path: str, *, options: dict | None = None, draft: list[str] | None = None) -> str:
        async with SessionLocal() as session:
            row = DataSource(name=f"prof_{uuid.uuid4().hex[:10]}", kind="sqlite", database=str(path),
                             options=options or {})
            session.add(row)
            await session.commit()
            sid = row.id
        made.append(sid)
        r = await client.post(f"/api/datasources/{sid}/introspect")
        assert r.status_code == 200, r.text
        if draft is not None:
            r = await client.post(f"/api/datasources/{sid}/catalog/draft", json={"tables": draft})
            assert r.status_code == 200, r.text
        return sid

    yield _make
    for sid in made:
        await drop_source(sid)


class _Spy:
    """记下两样东西：守卫放行（guard.check 返回）的语句，和真正发到这个业务库的语句。

    后者挂在 SQLAlchemy 的 before_cursor_execute 上，任何一条到达驱动的语句都逃不掉——包括绕开查询层
    另开连接发的。断言「发到库的 ⊆ 守卫放行的」就是「所有查询都经过守卫」。
    """

    def __init__(self, db_name: str) -> None:
        self.db_name = db_name
        self.checked: list[str] = []
        self.executed: list[str] = []

    def clear(self) -> None:
        self.checked.clear()
        self.executed.clear()

    def all_guarded(self) -> bool:
        return bool(self.executed) and all(s in self.checked for s in self.executed)

    def mentions(self, word: str) -> list[str]:
        return [s for s in self.executed if word.lower() in s.lower()]


@pytest.fixture
def spy(monkeypatch, scenic_db):
    holder = SimpleNamespace(spy=None)

    def watch(path: str) -> _Spy:
        holder.spy = _Spy(Path(path).name)
        return holder.spy

    real_check = data_engine.check

    def checking(sql, **kw):
        out = real_check(sql, **kw)
        if holder.spy is not None:
            holder.spy.checked.append(out)
        return out

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if holder.spy is not None and holder.spy.db_name in str(conn.engine.url):
            holder.spy.executed.append(statement)

    monkeypatch.setattr(data_engine, "check", checking)
    event.listen(Engine, "before_cursor_execute", on_execute)
    yield watch
    event.remove(Engine, "before_cursor_execute", on_execute)


def _url(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog/profile"


async def _notes(client, sid: str, table: str) -> dict:
    r = await client.get(f"/api/datasources/{sid}/catalog/{table}")
    assert r.status_code == 200, r.text
    return r.json()


def _relation(notes: dict, column: str) -> dict:
    return next(r for r in notes.get("relations") or [] if r["columns"] == [column])


def _table(body: dict, name: str) -> dict:
    return next(t for t in body["tables"] if t["table_name"] == name)


def _reasons(table: dict) -> set[str]:
    return {s["reason"] for s in table["skipped"]}


# ---------------------------------------------------------------- 开关与设置


async def test_profile_is_off_by_default_and_says_where_to_turn_it_on(client, make_source, scenic_db, spy):
    sid = await make_source(scenic_db, draft=["visits"])
    watch = spy(scenic_db)
    watch.clear()
    r = await client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "数据源设置" in detail and "开启数据剖析" in detail
    assert watch.executed == []          # 一条查询都没发


async def test_settings_are_saved_and_validated_through_datasource_update(client, make_source, scenic_db):
    sid = await make_source(scenic_db)
    r = await client.patch(f"/api/datasources/{sid}", json={"options": _on(max_queries=0)})
    assert r.status_code == 422 and "查询次数上限" in r.json()["detail"]
    r = await client.patch(f"/api/datasources/{sid}", json={"options": _on(max_queries=40, sample_size=500)})
    assert r.status_code == 200, r.text
    assert r.json()["options"][PROFILE_OPTION] == {"enabled": True, "max_queries": 40, "sample_size": 500}
    # 设好以后就能剖析；返回里带着这次实际用的设置
    r = await client.post(_url(sid), json={"tables": ["channels"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["max_queries"] == 40 and r.json()["settings"]["sample_size"] == 500


# ---------------------------------------------------------------- 剖析的结论


async def test_profile_verifies_name_relations_and_proposes_codes_and_business_date(client, make_source, scenic_db,
                                                                                    spy):
    sid = await make_source(scenic_db, options=_on(), draft=["visits", "channels"])
    watch = spy(scenic_db)
    watch.clear()
    r = await client.post(_url(sid), json={"tables": ["visits", "channels"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    body = r.json()

    # 所有发到业务库的语句都经过了守卫
    assert watch.all_guarded(), watch.executed
    assert body["queries_used"] == len(watch.executed) <= body["settings"]["max_queries"]
    assert body["queries_used"] == sum(t["queries"] for t in body["tables"])
    assert body["stopped"] is None and body["actor"] == "周宁" and body["profiled_at"]

    notes = (await _notes(client, sid, "visits"))
    assert notes["updated_by"] == "周宁"
    notes = notes["notes"]
    # 命名推断出来的两条关系：数据对得上、父表主键唯一 → 有确证，来源记为数据剖析
    for column, parent in (("gate_id", "gates"), ("member_id", "members")):
        rel = _relation(notes, column)
        assert rel["to_table"] == parent
        assert (rel["source"], rel["status"]) == ("profile", "verified")
        assert rel["coverage"] == 1.0 and rel["cardinality"] == "many_to_one"
        assert "数据剖析" in rel["note"] and "抽样" in rel["note"]
        # 子表一侧数过 COUNT 和 COUNT(DISTINCT)、确有重复：基数是用数据核实的（A6，一对多关联的检查据此给 error）
        assert rel["cardinality_checked"] is True
        # A7：说明里不写日期，时间看这一项的 updated_at（界面按本地时间显示）
        assert rel["note"].startswith("数据剖析：") and not re.search(r"\d{4}-\d{2}-\d{2}", rel["note"])
        assert rel["updated_at"]
    # 外键约束来的关系不动：外键是外键，剖析是剖析
    park = _relation(notes, "park_id")
    assert (park["source"], park["status"], park["coverage"]) == ("fk", "verified", None)
    assert "cardinality_checked" not in park              # 外键的基数是按表结构推的，没用数据核对

    # 状态列：码值候选，值有了、含义留空，推断
    codes = notes["columns"]["status"]["codes"]
    assert codes["value"] == {"1": "", "0": ""}
    assert (codes["source"], codes["status"]) == ("profile", "proposed") and "1500" in codes["note"]
    # 只有一个日期类列：提议为业务日期，备注里写明取值范围
    bdate = notes["business_date"]
    assert bdate["value"] == {"column": "visit_time"} and (bdate["source"], bdate["status"]) == ("profile", "proposed")
    assert "2026-07" in bdate["note"] and "2026-09" in bdate["note"]

    channels = (await _notes(client, sid, "channels"))["notes"]
    assert set(channels["columns"]["channel_type"]["codes"]["value"]) == {"线上直销", "线下", "分销"}
    assert "channel_code" not in channels.get("columns", {})       # 唯一约束列不是码值

    visits = _table(body, "visits")
    kinds = sorted(f["kind"] for f in visits["findings"])
    assert kinds == ["business_date", "codes", "relation", "relation"]
    gate = next(f for f in visits["findings"] if f["kind"] == "relation" and f["columns"] == ["gate_id"])
    assert gate["status"] == "verified" and gate["path"] == f"relations.{_relation(notes, 'gate_id')['id']}"
    assert gate["sample"] == 8 and gate["matched"] == 8 and gate["summary"]
    assert visits["row_estimate"] == {"rows": 1500, "method": "count", "at_least": None}
    assert visits["added"] == 2 and visits["updated"] == 2 and visits["error"] is None


async def test_wrong_name_relation_stays_proposed_with_coverage_in_note(client, make_source, tmp_path):
    """推错的命名关系：parking_records.lot_id 按名字对上了新建的 lots 表，但停车记录里的区号大多不在里面。"""
    path = scenic.build(tmp_path / "lots.db")
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE lots (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.executemany("INSERT INTO lots VALUES (?, ?)", [(i, f"{i} 号停车区") for i in (1, 2, 3)])
        db.executemany("INSERT INTO parking_records VALUES (?, ?, ?, ?, ?, ?)",
                       [(i, 1 + i % 20, None, "2026-08-01 09:00:00", None, 10.0) for i in range(1, 101)])
        db.commit()
    finally:
        db.close()
    sid = await make_source(path, options=_on(), draft=["parking_records"])
    before = _relation((await _notes(client, sid, "parking_records"))["notes"], "lot_id")
    assert (before["to_table"], before["source"], before["status"]) == ("lots", "name", "proposed")

    r = await client.post(_url(sid), json={"tables": ["parking_records"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    rel = _relation((await _notes(client, sid, "parking_records"))["notes"], "lot_id")
    assert rel["status"] == "proposed" and rel["source"] == "profile"
    assert rel["coverage"] == 0.15
    assert "抽样覆盖率 15%" in rel["note"] and "可能不是这条关系" in rel["note"] and "20 个" in rel["note"]
    finding = _table(r.json(), "parking_records")["findings"][0]
    assert finding["status"] == "proposed" and finding["coverage"] == 0.15
    # 两个日期类列：不提议业务日期，取值范围只在返回里
    ranges = {d["column"] for d in _table(r.json(), "parking_records")["date_ranges"]}
    assert ranges == {"entered_at"}           # exited_at 全是空值
    assert "business_date" not in (await _notes(client, sid, "parking_records"))["notes"]


async def test_confirmed_relation_only_gets_coverage_and_cardinality(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    detail = await _notes(client, sid, "visits")
    gate = _relation(detail["notes"], "gate_id")
    r = await client.post(f"/api/datasources/{sid}/catalog/visits/review",
                          json={"path": f"relations.{gate['id']}", "action": "confirm",
                                "if_version": detail["version"]})
    assert r.status_code == 200, r.text

    r = await client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    rel = _relation((await _notes(client, sid, "visits"))["notes"], "gate_id")
    assert (rel["source"], rel["status"]) == ("name", "confirmed")
    assert rel["coverage"] == 1.0 and rel["cardinality"] == "many_to_one"
    # 人定的基数不改；原来就有基数（命名推断记的多对一），剖析也不给它记「用数据核实过」
    assert "cardinality_checked" not in rel
    finding = next(f for f in _table(r.json(), "visits")["findings"] if f.get("columns") == ["gate_id"])
    assert finding["status"] == "confirmed" and finding["confirmed"] is True


async def test_rerun_with_same_data_changes_nothing(client, make_source, scenic_db):
    """同一天、数据没变：第二次剖析不改目录、不升版本（冻结进快照的目录不会无故变）。"""
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    first = (await client.post(_url(sid), json={"tables": ["visits"]})).json()
    second = (await client.post(_url(sid), json={"tables": ["visits"]})).json()
    a, b = _table(first, "visits"), _table(second, "visits")
    assert (b["added"], b["updated"], b["removed"]) == (0, 0, 0)
    assert b["version"] == a["version"]


# ---------------------------------------------------------------- 安全约束


async def test_masked_columns_are_never_queried(client, make_source, scenic_db, spy):
    sid = await make_source(scenic_db, options={**_on(), "mask_columns": "member_id, Channel_Type"},
                            draft=["visits", "channels"])
    watch = spy(scenic_db)
    watch.clear()
    r = await client.post(_url(sid), json={"tables": ["visits", "channels"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    assert watch.all_guarded()
    assert watch.mentions("member_id") == [] and watch.mentions("channel_type") == []
    visits, channels = _table(r.json(), "visits"), _table(r.json(), "channels")
    masked = [s for s in visits["skipped"] + channels["skipped"] if s["reason"] == "masked"]
    assert {s["target"] for s in masked} == {"member_id → members.id", "channel_type"}
    assert all("遮罩" in s["detail"] for s in masked)
    notes = (await _notes(client, sid, "visits"))["notes"]
    assert _relation(notes, "member_id")["source"] == "name"                    # 没动
    assert _relation(notes, "gate_id")["status"] == "verified"                  # 其余照常
    assert "channel_type" not in ((await _notes(client, sid, "channels"))["notes"].get("columns") or {})


async def test_query_budget_stops_profiling_and_reports_the_rest(client, make_source, scenic_db, spy):
    sid = await make_source(scenic_db, options=_on(max_queries=3), draft=["visits"])
    watch = spy(scenic_db)
    watch.clear()
    r = await client.post(_url(sid), json={"tables": ["visits", "channels"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queries_used"] == 3 == len(watch.executed) and body["stopped"] == "budget"
    visits, channels = _table(body, "visits"), _table(body, "channels")
    assert "budget" in _reasons(visits)
    assert channels["queries"] == 0 and _reasons(channels) == {"budget"}
    assert all("查询次数" in s["detail"] for s in visits["skipped"] + channels["skipped"] if s["reason"] == "budget")
    # 没查完的关系不改：原来的命名推断原样留着
    rel = _relation((await _notes(client, sid, "visits"))["notes"], "member_id")
    assert (rel["source"], rel["status"]) == ("name", "proposed")


async def test_single_query_timeout_is_recorded_and_profiling_continues(client, make_source, scenic_db, monkeypatch):
    """闸口的抽样查询换成一条停不下来的递归查询：1 秒时限到点由数据库掐断，记为超时，其余照常。"""
    sid = await make_source(scenic_db, options=_on(query_timeout_s=1), draft=["visits"])
    real = data_engine.run_query
    endless = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r"

    async def slow_gate(source, sql, *, limits=None):
        if "gate_id" in sql and "DISTINCT" in sql and "COUNT" not in sql:
            assert limits is not None and limits.timeout_seconds == 1
            return await real(source, endless, limits=limits)
        return await real(source, sql, limits=limits)

    monkeypatch.setattr(data_engine, "run_query", slow_gate)
    r = await client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    visits = _table(r.json(), "visits")
    timeout = [s for s in visits["skipped"] if s["reason"] == "timeout"]
    assert [s["target"] for s in timeout] == ["gate_id → gates.id"] and "超时" in timeout[0]["detail"]
    assert r.json()["stopped"] is None
    notes = (await _notes(client, sid, "visits"))["notes"]
    assert _relation(notes, "gate_id")["source"] == "name"                       # 超时的不改
    assert _relation(notes, "member_id")["status"] == "verified"                 # 其余照常


async def test_total_deadline_stops_profiling(make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, draft=["visits"])
    real = data_engine.run_query

    async def slow(source, sql, *, limits=None):
        await asyncio.sleep(0.5)
        return await real(source, sql, limits=limits)

    monkeypatch.setattr(data_engine, "run_query", slow)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        settings = ProfileSettings(enabled=True, max_total_s=1.4)
        report = await catalog_profile.profile_catalog(session, row, row, tables=["visits"], actor=None,
                                                       settings=settings)
    assert report.stopped == "deadline"
    assert 1 <= report.queries_used <= 3
    reasons = {s.reason for t in report.tables for s in t.skipped}
    assert "deadline" in reasons


async def test_large_tables_get_no_full_scans(client, make_source, scenic_db, spy):
    """行数超过整表统计上限（这里设成 100 行）：不做 COUNT(DISTINCT)、MIN/MAX，抽样和取值分布都带上限。"""
    sid = await make_source(scenic_db, options=_on(max_scan_rows=100), draft=["visits"])
    watch = spy(scenic_db)
    watch.clear()
    r = await client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR)
    assert r.status_code == 200, r.text
    visits = _table(r.json(), "visits")
    assert visits["row_estimate"] == {"rows": None, "method": "count", "at_least": 101}
    on_visits = [s for s in watch.executed if "from visits" in s.lower()]
    assert on_visits and all("LIMIT" in s for s in on_visits), on_visits
    assert not any("MIN(" in s for s in on_visits)
    assert "too_large" in _reasons(visits)
    assert any("太大" in s["detail"] or "上限" in s["detail"] for s in visits["skipped"] if s["reason"] == "too_large")
    # 父表有主键，覆盖率又够：照样有确证；子表一侧没核对唯一，按多对一记，不算用数据核实过基数
    rel = _relation((await _notes(client, sid, "visits"))["notes"], "gate_id")
    assert rel["status"] == "verified" and rel["cardinality"] == "many_to_one"
    assert not rel.get("cardinality_checked")


# ---------------------------------------------------------------- 接口


async def test_concurrent_profile_on_same_source_returns_409(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    real = data_engine.run_query
    started, release = asyncio.Event(), asyncio.Event()

    async def held(source, sql, *, limits=None):
        started.set()
        await release.wait()
        return await real(source, sql, limits=limits)

    monkeypatch.setattr(data_engine, "run_query", held)
    first = asyncio.create_task(client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR))
    await asyncio.wait_for(started.wait(), 10)
    second = await client.post(_url(sid), json={"tables": ["visits"]}, headers=ACTOR)
    assert second.status_code == 409 and "正在进行数据剖析" in second.json()["detail"]
    release.set()
    done = await asyncio.wait_for(first, 30)
    assert done.status_code == 200, done.text
    # 做完以后可以再来
    again = await client.post(_url(sid), json={"tables": ["channels"]}, headers=ACTOR)
    assert again.status_code == 200, again.text


async def test_default_tables_are_used_tables_with_relation_candidates(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options=_on(), draft=[])
    r = await client.post(_url(sid), json={}, headers=ACTOR)
    assert r.status_code == 200, r.text
    names = [t["table_name"] for t in r.json()["tables"]]
    assert names and len(names) <= catalog_profile.PROFILE_DEFAULT_TABLES
    async with SessionLocal() as session:
        entries = await catalog.read_catalog(session, sid)
    for name in names:
        rels = entries[name].notes.get("relations") or []
        assert any(rel["source"] not in ("fk",) for rel in rels), name
    assert "parks" not in names and "channels" not in names          # 没有待核实关系的表不选


async def _candidates(sid: str) -> tuple[list[str], set[str]]:
    """默认挑表的候选（目录里有要核对的关系的表，按表结构顺序）和其中还有待核实（推断）关系的那些。"""
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        entries = await catalog.read_catalog(session, sid)
    order = list((row.schema_cache or {}).get("tables") or {})
    rels = {name: catalog_profile._relation_candidates(entries[name].notes) for name in order if name in entries}
    names = [name for name in order if rels.get(name)]
    return names, {name for name in names if any(r["status"] == "proposed" for r in rels[name])}


async def test_default_pick_skips_empty_tables_and_prefers_pending_relations(client, make_source, tmp_path,
                                                                           monkeypatch):
    """B1：表都没被运行查询过（使用次数都是 0）时，以前按表结构顺序取前几张，结果大半是空表（景区库里排在前面的
    候选表正好多数没有数据）。现在跳过行数估计为 0 的表（记在 empty_tables 里），并且先挑还有待核实关系的表，
    不把刚核实过的再剖析一遍。默认张数调成 3，好在一个小库上看出先后。"""
    monkeypatch.setattr(catalog_profile, "PROFILE_DEFAULT_TABLES", 3)
    path = scenic.build(tmp_path / "b1.db")
    db = sqlite3.connect(path)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
        empty = {t for t in tables if db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == 0}
    finally:
        db.close()
    sid = await make_source(str(path), options=_on(), draft=tables)
    names, pending = await _candidates(sid)
    full = [name for name in names if name not in empty]
    # 前提：按表结构顺序的前 3 张候选里有空表（以前就会挑中它们），有数据、还待核实的候选不止 6 张
    assert set(names[:3]) & empty
    assert len([name for name in full if name in pending]) >= 6

    first = (await client.post(_url(sid), json={}, headers=ACTOR)).json()
    picked = [t["table_name"] for t in first["tables"]]
    assert len(picked) == 3 and not set(picked) & empty, picked
    assert first["empty_tables"] and set(first["empty_tables"]) <= empty
    assert all(t["queries"] > 0 for t in first["tables"])
    assert first["queries_used"] >= sum(t["queries"] for t in first["tables"]) + len(first["empty_tables"])

    # 第二次：先挑还有待核实关系的表；第一次核实过的（关系都升为有确证了）排到后面，这次挑不到
    names, pending = await _candidates(sid)
    assert not set(picked) & pending                         # 第一次挑的三张，关系都核实了
    second = (await client.post(_url(sid), json={}, headers=ACTOR)).json()
    again = [t["table_name"] for t in second["tables"]]
    assert len(again) == 3 and all(name in pending for name in again), (again, pending)
    assert not set(again) & set(picked) and not set(again) & empty


async def test_profile_note_when_every_candidate_is_empty(client, make_source, tmp_path):
    path = scenic.build(tmp_path / "b1-empty.db")
    sid = await make_source(str(path), options=_on(), draft=[])
    names, _ = await _candidates(sid)
    db = sqlite3.connect(path)
    try:
        for name in names:
            db.execute(f'DELETE FROM "{name}"')
        db.commit()
    finally:
        db.close()
    body = (await client.post(_url(sid), json={}, headers=ACTOR)).json()
    assert body["tables"] == [] and set(body["empty_tables"]) == set(names)
    assert "空表" in body["note"]


async def test_unknown_table_and_missing_source(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    r = await client.post(_url(sid), json={"tables": ["nope"]}, headers=ACTOR)
    assert r.status_code == 200
    assert _table(r.json(), "nope")["error"] and r.json()["queries_used"] == 0
    r = await client.post(_url("0" * 32), json={}, headers=ACTOR)
    assert r.status_code == 404
