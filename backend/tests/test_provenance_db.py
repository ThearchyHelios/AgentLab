"""证据下钻（期 4）里碰数据库和数据文件的几步：provenance_db 的各函数，engine.py 的两处小改，sum_eq_conditions。

数据文件都是测试里现造的小 SQLite 库（表名「日客流」「时段客流」，分区甲、分区乙，数字随机），数据文件记录
直接写进测试库的 source_snapshots，不经导入管线：这里要钉的是回查、编译核对、强制重算、行级核对各自的口径，
整条链（真导入、真运行）由 WP-E 的验收覆盖。
"""
from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

import pytest

from app.data import engine as engine_mod
from app.data import provenance_db
from app.data import recipe_checks
from app.data.engine import EngineCache, SnapshotTampered, cache_key, engines, open_checked_sqlite
from app.data.provenance_types import _QUOTED, CompileFacts, StateNote, SumEqRule
from app.data.recipe_checks import sum_eq_conditions
from app.data.recipe_types import ColumnOut, SumEq
from app.data.table_versions import SnapshotManifestMismatch, SnapshotMissing
from app.db.base import SessionLocal
from app.db.models import DataSource, SourceSnapshot, TableImport, TableRecipe

RNG = random.Random(4004)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _sha(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _build(path, *, daily, hourly=(), real=()) -> str:
    """造一个快照库：日客流（单列主键，写成 PRIMARY KEY DESC，和执行器一样不让它成为 rowid 的别名）、
    时段客流（两列主键）、分区占比（REAL 列）。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE "日客流" ("日期" TEXT NOT NULL, "全日客流" INTEGER, "分区甲" INTEGER, "分区乙" INTEGER,
                              PRIMARY KEY ("日期" DESC));
        CREATE TABLE "时段客流" ("日期" TEXT NOT NULL, "时段" TEXT NOT NULL, "客流" INTEGER,
                                PRIMARY KEY ("日期", "时段"));
        CREATE TABLE "分区占比" ("日期" TEXT NOT NULL, "合计" REAL, "分区甲" REAL, "分区乙" REAL,
                                PRIMARY KEY ("日期" DESC));
    """)
    conn.executemany('INSERT INTO "日客流" VALUES (?, ?, ?, ?)', list(daily))
    conn.executemany('INSERT INTO "时段客流" VALUES (?, ?, ?)', list(hourly))
    conn.executemany('INSERT INTO "分区占比" VALUES (?, ?, ?, ?)', list(real))
    conn.commit()
    conn.close()
    os.chmod(path, 0o444)
    return str(path)


def _daily_rows(n=6):
    """前几行成立，第 3 行不成立（合计多 1），第 5 行分区乙为空。"""
    rows = []
    for i in range(n):
        a, b = RNG.randint(1000, 9000), RNG.randint(1000, 9000)
        total = a + b
        if i == 2:
            total += 1
        rows.append((f"2026-08-{i + 1:02d}", total, a, None if i == 4 else b))
    return rows


async def _register(path, *, source_id=None, retired=False, sha=None, manifest=None) -> SourceSnapshot:
    snap = SourceSnapshot(
        id=uuid.uuid4().hex + uuid.uuid4().hex, source_id=source_id or uuid.uuid4().hex,
        db_path=str(path), db_sha256=_sha(path) if sha is None else sha, schema_cache={},
        retired_at=datetime.now(timezone.utc) if retired else None, manifest_artifact=manifest,
    )
    async with SessionLocal() as session:
        session.add(snap)
        await session.commit()
        await session.refresh(snap)
    return snap


@pytest.fixture
async def snapshot(tmp_path):
    """一个登记好的快照库，数据源记录不存在（模拟数据源已删除）。用完清掉引擎缓存。"""
    path = _build(tmp_path / "snap.db", daily=_daily_rows(),
                  hourly=[("2026-08-03", "7-8", None), ("2026-08-03", "8-9", 321)],
                  real=[("2026-08-01", 0.3, 0.1, 0.2), ("2026-08-02", 1.5, 0.5, 0.75)])
    snap = await _register(path)
    yield snap
    await engines.invalidate(snap.source_id)


async def _view(snap: SourceSnapshot):
    return provenance_db.view_of(snap, source_name="flow_demo")


def _poke(path: str, *, keep_mtime: bool) -> None:
    """原地改一个字节（大小不变）。keep_mtime 时再用 os.utime 还原修改时间：EngineCache 的指纹认不出这种改动。"""
    st = os.stat(path)
    os.chmod(path, 0o644)
    with open(path, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last[0] ^ 0x01]))
    os.chmod(path, 0o444)
    if keep_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


# --------------------------------------------------------------------------
# sum_eq_conditions：导入时的核对和行级核对共用一段条件
# --------------------------------------------------------------------------


def test_sum_eq_conditions_integer_and_real():
    nonnull, anynull, mismatch = sum_eq_conditions("全日客流", ["分区甲", "分区乙"],
                                                   {"全日客流": "INTEGER", "分区甲": "INTEGER"})
    assert nonnull == '"全日客流" IS NOT NULL AND "分区甲" IS NOT NULL AND "分区乙" IS NOT NULL'
    assert anynull == '"全日客流" IS NULL OR "分区甲" IS NULL OR "分区乙" IS NULL'
    assert mismatch == '"全日客流" <> "分区甲" + "分区乙"'
    # 任一列是 REAL 就按相对误差比：0.1 + 0.2 不严格等于 0.3
    _, _, real = sum_eq_conditions("合计", ["分区甲", "分区乙"], {"分区乙": "REAL"})
    assert real == 'ABS("合计" - ("分区甲" + "分区乙")) > 1e-9 * MAX(1, ABS("合计"))'
    # 名字里的双引号照样转义（DQS 关闭的连接上写错的名字直接报错）
    assert sum_eq_conditions('a"b', ["c", "d"], {})[2] == '"a""b" <> "c" + "d"'


@pytest.mark.parametrize("table,total,parts,types", [
    ("日客流", "全日客流", ["分区甲", "分区乙"], {"全日客流": "INTEGER", "分区甲": "INTEGER", "分区乙": "INTEGER"}),
    ("分区占比", "合计", ["分区甲", "分区乙"], {"合计": "REAL", "分区甲": "REAL", "分区乙": "REAL"}),
])
async def test_row_status_agrees_with_the_import_check(snapshot, table, total, parts, types):
    """逐行的 row_status 和导入时的 _check_sum_eq 在同一个库上的结论对得上：成立、不成立、含空值的行数都相同。
    REAL 表里 0.1 + 0.2 = 0.3 那一行两边都判成立（改成严格比较的条件会被这里抓到）。"""
    view = await _view(snapshot)
    rule = SumEqRule(table=table, total=total, parts=parts, types=types)
    conn = open_checked_sqlite(snapshot.db_path)
    try:
        rowids = [r[0] for r in conn.execute(f'SELECT rowid FROM "{table}" ORDER BY rowid')]
        result = recipe_checks._check_sum_eq(
            recipe_checks._Sql(conn), SumEq(id="R1", kind="sum_eq", table=table, total=total, parts=parts), "核对",
            [ColumnOut(name=n, type=t) for n, t in types.items()], ["日期"], {})
    finally:
        conn.close()
    statuses = [await provenance_db.row_status(view, rule, rid) for rid in rowids]
    assert statuses.count("mismatch") == result.failed
    assert statuses.count("unverifiable") == result.unverifiable
    assert statuses.count("passed") + statuses.count("mismatch") == result.checked
    if table == "日客流":
        assert statuses == ["passed", "passed", "mismatch", "passed", "unverifiable", "passed"]
    else:
        assert statuses == ["passed", "mismatch"]


async def test_row_status_follows_the_snapshot_rowid_it_is_given(snapshot):
    """按传进来的快照库 rowid 判，不按别的编号：同一条规则，第 3 行不成立、第 4 行成立。"""
    view = await _view(snapshot)
    rule = SumEqRule(table="日客流", total="全日客流", parts=["分区甲", "分区乙"], types={})
    assert await provenance_db.row_status(view, rule, 3) == "mismatch"
    assert await provenance_db.row_status(view, rule, 4) == "passed"


async def test_row_status_is_none_when_the_row_or_a_column_is_not_there(snapshot):
    view = await _view(snapshot)
    assert await provenance_db.row_status(
        view, SumEqRule(table="日客流", total="全日客流", parts=["分区甲", "分区乙"], types={}), 999) is None
    assert await provenance_db.row_status(
        view, SumEqRule(table="日客流", total="全日客流", parts=["分区甲", "分区丙"], types={}), 1) is None
    assert await provenance_db.row_status(
        view, SumEqRule(table="没有这张表", total="a", parts=["b", "c"], types={}), 1) is None
    # 名字只对 ASCII 不区分大小写（和 SQLite 一致），中文名原样比
    assert await provenance_db.row_status(
        view, SumEqRule(table="日客流", total="全日客流", parts=["分区甲", "分区乙"], types={}), 1) == "passed"


# --------------------------------------------------------------------------
# 数据文件记录与绑定
# --------------------------------------------------------------------------


async def test_snapshot_record(snapshot):
    async with SessionLocal() as session:
        got = await provenance_db.snapshot_record(session, snapshot.id)
        assert got is not None and got.id == snapshot.id and got.db_path == snapshot.db_path
        assert await provenance_db.snapshot_record(session, "f" * 64) is None
        assert await provenance_db.snapshot_record(session, "") is None


async def test_view_of_uses_a_stand_in_keyed_like_the_query(snapshot):
    """数据源记录不存在（已删除）也能绑定；id 用 snapshot.source_id，缓存键和查询时相同，首次哈希只算一次。"""
    async with SessionLocal() as session:
        assert await session.get(DataSource, snapshot.source_id) is None
    view = provenance_db.view_of(snapshot, source_name="flow_demo")
    assert view.id == snapshot.source_id and view.name == "flow_demo"
    assert view.snapshot_id == snapshot.id and view.database == snapshot.db_path
    assert view.immutable is True and view.readonly is True
    assert view.expected_sha256 == snapshot.db_sha256
    assert view.options == {} and view.description == "" and view.enabled is True
    assert cache_key(view) == f"{snapshot.source_id}:{snapshot.id}"


async def test_view_of_refuses_unregistered_hash_and_gone_files(tmp_path):
    path = _build(tmp_path / "a.db", daily=_daily_rows(2))
    with pytest.raises(SnapshotMissing):
        provenance_db.view_of(await _register(path, sha=""), source_name="flow_demo")
    with pytest.raises(SnapshotMissing):
        provenance_db.view_of(await _register(path, retired=True), source_name="flow_demo")
    gone = await _register(path)
    os.chmod(path, 0o644)
    os.remove(path)
    # 文件不在：绑定前就认出来（snapshot_gone，不标红），不留给 EngineCache 报成「文件被改」
    with pytest.raises(SnapshotMissing) as info:
        provenance_db.view_of(gone, source_name="flow_demo")
    assert not isinstance(info.value, SnapshotTampered)


async def test_view_of_raises_manifest_mismatch_untouched(tmp_path):
    """快照清单里的库哈希和登记的不一致：bind_snapshot 抛的 SnapshotManifestMismatch 原样抛出。"""
    from app.core import artifact_store

    path = _build(tmp_path / "u.db", daily=_daily_rows(2))
    mid = await artifact_store.put_json({"snapshot_id": None, "union": {"db_sha256": "0" * 64}},
                                        kind="snapshot_manifest")
    snap = await _register(path, manifest=mid)
    with pytest.raises(SnapshotManifestMismatch):
        provenance_db.view_of(snap, source_name="flow_demo")


# --------------------------------------------------------------------------
# R16：回查
# --------------------------------------------------------------------------


async def test_recheck_sql_params_and_rows(snapshot):
    view = await _view(snapshot)
    rows, sql, params = await provenance_db.recheck(view, table="日客流", column="全日客流", pk={"日期": "2026-08-02"})
    assert sql == 'SELECT rowid, "全日客流" FROM "日客流" WHERE "日期" = ?'
    assert params == ["2026-08-02"]
    conn = sqlite3.connect(snapshot.db_path)
    want = conn.execute('SELECT rowid, "全日客流" FROM "日客流" WHERE "日期" = ?', ["2026-08-02"]).fetchall()
    conn.close()
    assert rows == want and isinstance(rows[0][1], int)
    # 契约 Recheck 的口径：去掉双引号里的标识符后 ? 的个数等于主键列数
    assert _QUOTED.sub("", sql).count("?") == 1


async def test_recheck_composite_key_keeps_primary_key_order_and_types(snapshot):
    view = await _view(snapshot)
    rows, sql, params = await provenance_db.recheck(view, table="时段客流", column="客流",
                                                    pk={"日期": "2026-08-03", "时段": "8-9"})
    assert sql == 'SELECT rowid, "客流" FROM "时段客流" WHERE "日期" = ? AND "时段" = ?'
    assert params == ["2026-08-03", "8-9"] and [v for _, v in rows] == [321]
    # 空值照实回（R11 已拦下空值，这里只回事实）；REAL 回 float，不和 int 混
    rows, _, _ = await provenance_db.recheck(view, table="时段客流", column="客流", pk={"日期": "2026-08-03", "时段": "7-8"})
    assert [v for _, v in rows] == [None]
    rows, _, _ = await provenance_db.recheck(view, table="分区占比", column="合计", pk={"日期": "2026-08-02"})
    assert rows[0][1] == 1.5 and isinstance(rows[0][1], float)
    rows, _, _ = await provenance_db.recheck(view, table="日客流", column="全日客流", pk={"日期": "1999-01-01"})
    assert rows == []


async def test_recheck_quotes_identifiers_with_double_quotes(tmp_path):
    path = tmp_path / "q.db"
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "表""甲" ("键""一" TEXT NOT NULL, "值?" INTEGER, PRIMARY KEY ("键""一" DESC))')
    conn.execute('INSERT INTO "表""甲" VALUES (?, ?)', ["k", 7])
    conn.commit()
    conn.close()
    snap = await _register(path)
    try:
        rows, sql, params = await provenance_db.recheck(
            await _view(snap), table='表"甲', column="值?", pk={'键"一': "k"})
        assert sql == 'SELECT rowid, "值?" FROM "表""甲" WHERE "键""一" = ?'
        assert [v for _, v in rows] == [7] and params == ["k"]
        assert _QUOTED.sub("", sql).count("?") == 1
    finally:
        await engines.invalidate(snap.source_id)


async def test_recheck_raises_tampered_when_the_file_changed(snapshot):
    """改过一个字节（修改时间也变了）：经 EngineCache 取引擎时重新核对哈希，SnapshotTampered 原样抛出。"""
    view = await _view(snapshot)
    await provenance_db.recheck(view, table="日客流", column="全日客流", pk={"日期": "2026-08-01"})
    _poke(snapshot.db_path, keep_mtime=False)
    with pytest.raises(SnapshotTampered):
        await provenance_db.recheck(view, table="日客流", column="全日客流", pk={"日期": "2026-08-01"})


async def test_recheck_needs_a_primary_key(snapshot):
    with pytest.raises(ValueError):
        await provenance_db.recheck(await _view(snapshot), table="日客流", column="全日客流", pk={})


# --------------------------------------------------------------------------
# R16：强制重算
# --------------------------------------------------------------------------


async def test_rehash_catches_an_in_place_edit_that_keeps_the_fingerprint(snapshot, monkeypatch):
    """原地改一个字节、再还原修改时间：EngineCache 的指纹不变、照样给缓存的引擎，强制重算认得出（R16，A25②）。
    重算放在线程里。"""
    view = await _view(snapshot)
    await engines.get(view)
    assert await provenance_db.rehash(view) is True
    threads: list[bool] = []
    real = engine_mod._file_sha256

    def spy(path):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(path)

    monkeypatch.setattr(engine_mod, "_file_sha256", spy)
    before = engines.pinned(view)
    _poke(snapshot.db_path, keep_mtime=True)
    assert engine_mod._fingerprint(snapshot.db_path) == before
    await engines.get(view)                   # 指纹没变，缓存命中，不重新核对
    assert await provenance_db.rehash(view) is False
    assert threads and not any(threads)


async def test_rehash_is_false_without_a_hash_or_a_file(snapshot):
    view = await _view(snapshot)
    view.expected_sha256 = None
    assert await provenance_db.rehash(view) is False
    view = await _view(snapshot)
    view.database = str(snapshot.db_path) + ".missing"
    assert await provenance_db.rehash(view) is False


# --------------------------------------------------------------------------
# R15：编译核对
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql,tables,rows,yields", [
    ('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" = \'2026-08-01\'', {"日客流"}, 1, 0),
    ('SELECT * FROM "日客流" ORDER BY "全日客流" DESC LIMIT 3', {"日客流"}, 1, 0),
    ('SELECT "日期","全日客流" FROM "日客流" UNION ALL SELECT "日期","分区甲" FROM "日客流"', {"日客流"}, 2, 0),
    ('SELECT "日期","全日客流" FROM "日客流" WHERE "日期" LIKE \'2026%\' ESCAPE \'\\\' '
     'UNION ALL SELECT "日期","分区甲" FROM "日客流"', {"日客流"}, 2, 0),
    ('SELECT "日期","全日客流" FROM "日客流" WHERE "日期" IN (SELECT "日期" FROM "时段客流")',
     {"日客流", "时段客流"}, 1, 0),
])
async def test_compile_facts_reports_tables_and_result_rows(snapshot, sql, tables, rows, yields):
    facts = await provenance_db.compile_facts(await _view(snapshot), sql)
    assert facts == CompileFacts(tables=tables, result_rows=rows, yields=yields)


@pytest.mark.parametrize("op", ["UNION", "INTERSECT", "EXCEPT"])
async def test_compile_facts_sees_yield_in_set_operations(snapshot, op):
    facts = await provenance_db.compile_facts(
        await _view(snapshot), f'SELECT "日期","全日客流" FROM "日客流" {op} SELECT "日期","分区甲" FROM "日客流"')
    assert facts.yields >= 1


async def test_compile_facts_runs_in_a_thread_on_an_immutable_read_only_connection(snapshot, monkeypatch):
    seen: list[dict] = []
    real_open = provenance_db.open_checked_sqlite

    def spy(path, **kw):
        seen.append({"path": path, **kw, "main": threading.current_thread() is threading.main_thread()})
        return real_open(path, **kw)

    monkeypatch.setattr(provenance_db, "open_checked_sqlite", spy)
    await provenance_db.compile_facts(await _view(snapshot), 'SELECT "日期" FROM "日客流"')
    assert seen == [{"path": snapshot.db_path, "readonly": True, "immutable": True, "main": False}]


async def test_compile_facts_compares_against_the_engine_cache_fingerprint(snapshot, monkeypatch):
    """开连接前的指纹和 EngineCache 核对过的那份不等：SnapshotTampered（→ db_tampered）。"""
    view = await _view(snapshot)
    monkeypatch.setattr(engines, "pinned", lambda source: (0, 0, 0, 0))
    with pytest.raises(SnapshotTampered):
        await provenance_db.compile_facts(view, 'SELECT "日期" FROM "日客流"')


async def test_compile_facts_checks_the_fingerprint_again_after_closing(snapshot, monkeypatch):
    """编译期间文件被换（关连接后的指纹对不上）：同样 SnapshotTampered，不交出编译结果。"""
    view = await _view(snapshot)
    real = provenance_db._fingerprint
    calls: list[int] = []

    def drifting(path):
        calls.append(1)
        fp = real(path)
        return fp if len(calls) == 1 else (fp[0], fp[1] + 1, fp[2], fp[3])

    monkeypatch.setattr(provenance_db, "_fingerprint", drifting)
    with pytest.raises(SnapshotTampered):
        await provenance_db.compile_facts(view, 'SELECT "日期" FROM "日客流"')
    assert len(calls) == 2


async def test_compile_facts_without_a_pinned_engine_is_snapshot_missing(snapshot, monkeypatch):
    monkeypatch.setattr(engines, "pinned", lambda source: None)
    with pytest.raises(SnapshotMissing) as info:
        await provenance_db.compile_facts(await _view(snapshot), 'SELECT "日期" FROM "日客流"')
    assert not isinstance(info.value, SnapshotTampered)


async def test_compile_facts_refuses_a_replaced_file(snapshot):
    """引擎已在缓存里、文件随后被改（修改时间变了）：取引擎时就重新核对，SnapshotTampered。"""
    view = await _view(snapshot)
    await provenance_db.compile_facts(view, 'SELECT "日期" FROM "日客流"')
    _poke(snapshot.db_path, keep_mtime=False)
    with pytest.raises(SnapshotTampered):
        await provenance_db.compile_facts(view, 'SELECT "日期" FROM "日客流"')


# --------------------------------------------------------------------------
# 导入记录上的当前状态
# --------------------------------------------------------------------------


async def test_import_states():
    sid = uuid.uuid4().hex
    async with SessionLocal() as session:
        recipe = TableRecipe(source_id=sid, seq=3, recipe={}, recipe_sha256="a" * 64)
        session.add(recipe)
        await session.flush()
        kept = TableImport(source_id=sid, seq=1, raw_state="kept", recipe_id=recipe.id)
        purged = TableImport(source_id=sid, seq=2, raw_state="purged", recipe_id="nosuchrecipe",
                             purged={"at": "2026-09-30T01:02:03+00:00", "reason": "合成理由：清除原件",
                                     "signed_by": "录入员甲", "signed_by_verified": False})
        revoked = TableImport(source_id=sid, seq=3, raw_state="kept",
                              revoked={"at": "2026-10-01T00:00:00+00:00", "reason": "合成理由：作废",
                                       "signed_by": None, "signed_by_verified": False})
        session.add_all([kept, purged, revoked])
        await session.commit()
        ids = [kept.id, purged.id, revoked.id]
        got = await provenance_db.import_states(session, [*ids, "nosuchimport", kept.id])
    assert set(got) == set(ids)
    assert got[kept.id] == {"raw_state": "kept", "purged": None, "revoked": None, "recipe_seq": 3}
    assert got[purged.id] == {"raw_state": "purged", "revoked": None, "recipe_seq": None, "purged": StateNote(
        at="2026-09-30T01:02:03+00:00", signed_by="录入员甲", reason="合成理由：清除原件")}
    assert got[revoked.id]["revoked"] == StateNote(at="2026-10-01T00:00:00+00:00", signed_by=None,
                                                   reason="合成理由：作废")
    assert got[revoked.id]["recipe_seq"] is None
    async with SessionLocal() as session:
        assert await provenance_db.import_states(session, []) == {}


# --------------------------------------------------------------------------
# engine.py：EngineCache.pinned、open_checked_sqlite 的 immutable
# --------------------------------------------------------------------------


async def test_pinned_returns_the_verified_fingerprint(tmp_path):
    path = _build(tmp_path / "p.db", daily=_daily_rows(2))
    snap = await _register(path)
    view = await _view(snap)
    cache = EngineCache()
    try:
        assert cache.pinned(view) is None              # 还没建引擎
        await cache.get(view)
        st = os.stat(path)
        assert cache.pinned(view) == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        manual = type("S", (), {"id": "m1", "kind": "sqlite", "database": str(path), "readonly": True,
                                "options": {}, "name": "手工源"})()
        await cache.get(manual)
        assert cache.pinned(manual) is None            # 手工源没有核对过的指纹
        await cache.invalidate(snap.source_id)
        assert cache.pinned(view) is None
    finally:
        await cache.close()


def test_open_checked_sqlite_immutable_flag(tmp_path, monkeypatch):
    path = _build(tmp_path / "o.db", daily=_daily_rows(1))
    uris: list[str] = []
    real = sqlite3.connect

    def spy(database, *a, **kw):
        uris.append(database)
        return real(database, *a, **kw)

    monkeypatch.setattr(engine_mod.sqlite3, "connect", spy)
    for kwargs in ({}, {"readonly": True}, {"readonly": True, "immutable": True}):
        conn = open_checked_sqlite(path, **kwargs)
        try:
            assert conn.execute('SELECT COUNT(*) FROM "日客流"').fetchone() == (1,)
        finally:
            conn.close()
    # 缺省行为不变：只读、不带 immutable
    assert uris[0].endswith("?mode=ro") and uris[1].endswith("?mode=ro")
    assert uris[2].endswith("?mode=ro&immutable=1")
    with pytest.raises(ValueError):
        open_checked_sqlite(path, readonly=False, immutable=True)

