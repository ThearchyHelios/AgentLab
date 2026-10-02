"""按期累积与版本页（期 3，WP-4）的版本底座：快照 id、多期发布、启用旧快照、回收、隔离区、单进程守卫。

要守住的几件事（P3-SPEC 2.1、2.6、2.9、2.10、7.2、第 8 节遗留项 1 与 5、11.3 的 WP-4 部分）：
- 累积快照的 id 带目标配方的哈希，替换模式、简单导入的 id 与期 2 逐字相同；
- publish_snapshot / activate_snapshot 在锁内核对 expected_current，对不上 base_changed、什么都不动；
  快照 id 已存在时复用（没回收）或复活（已回收：文件在就清标记，文件没了就用这次的临时文件 link 回去，
  哈希必须等于登记值）；任何一步失败都回滚、删掉这次新 link 的文件；
- 启用旧快照的每个错误码；遮罩列丢失要确认；查询时拿快照清单交叉核对库列里的哈希；
- 回收在当前快照、被运行引用的快照之外，另外保留每个源最近 SNAPSHOT_KEEP 个快照（前两类不占名额，已回收的也不占）；
  被运行引用的并集快照不回收；孤儿清理认得并集库和附属试运行库；
- 隔离区的三条清理规则，保留期从挪进隔离区的那一刻算；拿不到守卫时 startup 什么都不删、写入一律 StoreUnavailable。

夹具：8 月、9 月的期库用合成客流表经执行器建（假名、随机数，见 test_recipe_accumulate）；其余用 sqlite3 现造。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.data import raw_store, recipe_accumulate as ra, recipe_types, table_versions
from app.data.engine import SnapshotTampered, engines, run_query
from app.db.base import SessionLocal, new_id, utcnow
from app.db.models import (
    DataSource, ImportStaging, Run, SnapshotActivation, SourceSnapshot, TableBuild, TableImport, TableRecipe,
)
from tests.test_recipe_accumulate import REF, _built

P2_BASELINE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "p2_baseline.json").read_text("utf-8"))

ENGINE = recipe_types.RECIPE_ENGINE_VER
SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：上传目录、原件、工件、隔离区都在里面。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def unique(prefix: str = "wp4") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def raw_bytes() -> bytes:
    return f"合成原件 {uuid.uuid4().hex}".encode()


def trial_copy(src: str | Path, source_id: str) -> Path:
    """把一份期库拷成这个源的试运行库（发布时从这里 link）。"""
    path = table_versions.trial_db_path(source_id, uuid.uuid4().hex, uuid.uuid4().hex)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, path)
    os.chmod(path, 0o644)
    return path


def make_db(path: Path, rows: list[tuple[int, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "t" ("n" INTEGER, "s" TEXT) STRICT')
    conn.executemany('INSERT INTO "t" VALUES (?, ?)', rows)
    conn.commit()
    conn.close()
    table_versions.settle_journal(path)
    return path


async def plain_schema(view, info) -> dict[str, Any]:
    return await table_versions.introspect_view(view, {})


async def get(model, key):
    async with SessionLocal() as session:
        return await session.get(model, key)


async def imports_of(source_id: str) -> list[TableImport]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(TableImport).where(TableImport.source_id == source_id).order_by(TableImport.seq))).scalars())


async def activations(source_id: str) -> list[SnapshotActivation]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(SnapshotActivation).where(SnapshotActivation.source_id == source_id)
            .order_by(SnapshotActivation.created_at))).scalars())


async def current(source_id: str) -> str | None:
    return (await get(DataSource, source_id)).current_snapshot_id


async def count_rows(source_id: str, sql: str, pinned: str | None = None) -> Any:
    async with SessionLocal() as session:
        view = await table_versions.resolve_source(session, await session.get(DataSource, source_id), pinned)
    try:
        return (await run_query(view, sql)).rows[0][0]
    finally:
        await engines.invalidate(source_id)


async def gc() -> None:
    async with SessionLocal() as session:
        await table_versions.gc(session)


async def publish_small(name: str, rows: list[tuple[int, str]] | None = None, *, recipe_id: str | None = None,
                        snapshot_fields: dict | None = None, activation: dict | None = None, raw: bytes | None = None):
    """经 publish_build 发布一期小库（表 t）。返回 (源 id, ImportResult)。raw 相同、rows 相同就是同一个构建。"""
    raw = raw if raw is not None else raw_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(id=uuid.uuid4().hex, name=name, kind="sqlite", readonly=True, origin="upload")
        sid = row.id
        tmp = make_db(table_versions.trial_db_path(sid, uuid.uuid4().hex, uuid.uuid4().hex),
                      rows or [(1, "分区甲"), (2, "分区乙")])
        result = await table_versions.publish_build(
            session, row, tmp_db=tmp, build_id=table_versions.recipe_build_id(sid, raw_sha, SHA_A, {}),
            build_options=table_versions.recipe_build_options(SHA_A, {}), engine_ver=ENGINE,
            report={"tables": [{"name": "t"}]}, raw=raw, raw_sha=raw_sha, file_name="合成.xlsx",
            file_size=len(raw), schema_cache_for=plain_schema,
            import_fields={"recipe_id": recipe_id} if recipe_id else {}, expected_db_sha256=raw_store.sha256_file(tmp),
            snapshot_fields=snapshot_fields, activation=activation)
    return sid, result


# --------------------------------------------------------------------------
# 8 月 + 9 月的累积链
# --------------------------------------------------------------------------


class Chain:
    """一个累积源：S1 = [8 月]（publish_build，快照库就是 8 月的构建库），S2 = [8 月, 9 月]（publish_snapshot，并集）。"""

    def __init__(self) -> None:
        self.name = unique()
        self.sid = uuid.uuid4().hex
        self.aug_path, self.aug_sha, self.aug_start, self.aug_end, hashes = _built("2026-08-01", 31)
        self.aug_hashes = dict(hashes)
        self.sep_path, self.sep_sha, self.sep_start, self.sep_end, hashes = _built("2026-09-01", 30)
        self.sep_hashes = dict(hashes)
        self.aug_raw, self.sep_raw = raw_bytes(), raw_bytes()
        self.aug_bid = table_versions.recipe_build_id(self.sid, hashlib.sha256(self.aug_raw).hexdigest(), SHA_A, {})
        self.sep_bid = table_versions.recipe_build_id(self.sid, hashlib.sha256(self.sep_raw).hexdigest(), SHA_A, {})
        self.aug_import = self.sep_import = self.s1 = self.s2 = self.uid = None
        self.manifest: str | None = None

    async def first(self) -> table_versions.ImportResult:
        raw_sha = hashlib.sha256(self.aug_raw).hexdigest()
        async with SessionLocal() as session:
            row = DataSource(id=self.sid, name=self.name, kind="sqlite", readonly=True, origin="upload")
            result = await table_versions.publish_build(
                session, row, tmp_db=trial_copy(self.aug_path, self.sid), build_id=self.aug_bid,
                build_options=table_versions.recipe_build_options(SHA_A, {}), engine_ver=ENGINE,
                report={"tables": [], "table_hashes": self.aug_hashes}, raw=self.aug_raw, raw_sha=raw_sha,
                file_name="8月.xlsx", file_size=len(self.aug_raw), schema_cache_for=plain_schema,
                import_fields={"recipe_id": "rA", "period_start": self.aug_start, "period_end": self.aug_end},
                expected_db_sha256=self.aug_sha,
                snapshot_fields={"mode": "accumulate", "recipe_id": "rA", "recipe_sha256": SHA_A})
        self.aug_import, self.s1 = result.import_id, result.snapshot_id
        return result

    def union_parts(self, sep_db: str | Path) -> list[recipe_types.UnionPart]:
        build = Path(table_versions.build_path(self.sid, self.aug_bid))
        return [
            recipe_types.UnionPart(import_id=self.aug_import, start=self.aug_start, end=self.aug_end,
                                   db_path=str(build), recipe=REF, db_sha256=self.aug_sha,
                                   table_hashes=self.aug_hashes),
            recipe_types.UnionPart(import_id=None, start=self.sep_start, end=self.sep_end, db_path=str(sep_db),
                                   recipe=REF, db_sha256=self.sep_sha, table_hashes=self.sep_hashes),
        ]

    def union_build(self, utrial: Path, rep: recipe_types.UnionReport) -> table_versions.UnionBuild:
        return table_versions.UnionBuild(
            tmp_db=utrial, union_id=self.uid,
            options=ra.union_options([(self.aug_start, self.aug_end, self.aug_bid),
                                      (self.sep_start, self.sep_end, self.sep_bid)], SHA_A),
            report=table_versions.report_to_json(asdict(rep)), expected_sha256=rep.db_sha256)

    async def schema_with_manifest(self, view, info: table_versions.SnapshotInfo) -> dict[str, Any]:
        cache = await table_versions.introspect_view(view, {})
        self.manifest = await artifact_store.put_json(
            {"format": "agentlab-snapshot-manifest/1", "snapshot_id": info.snapshot_id,
             "union": {"build_id": info.union_id, "db_sha256": info.union_db_sha256}},
            kind="snapshot_manifest")
        cache["snapshot_manifest"] = self.manifest
        return cache

    async def second(self, *, expected: str | None = "S1", before_commit=None, drop_union_tmp: bool = False,
                     recipe_id: str = "rA") -> table_versions.SnapshotPublish:
        trial = trial_copy(self.sep_path, self.sid)
        utrial = table_versions.union_trial_path(trial)
        rep = ra.materialize_union(utrial, target=REF, parts=self.union_parts(trial))
        assert rep.ok
        self.uid = ra.union_id(self.sid, [(self.aug_start, self.aug_end, self.aug_bid),
                                          (self.sep_start, self.sep_end, self.sep_bid)], SHA_A)
        if drop_union_tmp:
            utrial.unlink()
        raw_store.put_raw(self.sep_raw)
        self.sep_import = new_id()
        raw_sha = hashlib.sha256(self.sep_raw).hexdigest()
        self.sep_trial, self.union_trial = trial, utrial
        async with SessionLocal() as session:
            source = await session.get(DataSource, self.sid)
            result = await table_versions.publish_snapshot(
                session, source, imports=[self.aug_import, self.sep_import], mode="accumulate",
                recipe_id=recipe_id, recipe_sha256=SHA_A,
                expected_current=self.s1 if expected == "S1" else expected,
                period_build=table_versions.PeriodBuild(
                    tmp_db=trial, build_id=self.sep_bid, options=table_versions.recipe_build_options(SHA_A, {}),
                    report={"tables": [], "table_hashes": self.sep_hashes}, expected_sha256=self.sep_sha),
                new_import=table_versions.NewImport(
                    import_id=self.sep_import, raw_sha=raw_sha, file_name="9月.xlsx", file_size=len(self.sep_raw),
                    fields={"recipe_id": recipe_id, "period_start": self.sep_start, "period_end": self.sep_end}),
                union=self.union_build(utrial, rep), schema_cache_for=self.schema_with_manifest,
                before_commit=before_commit(session) if before_commit else None)
        self.s2 = result.snapshot_id
        return result

    async def publish_existing(self, imports: list[str], *, expected: str, union: bool, kind: str = "remove_period",
                               union_tmp: Path | None = None, recipe_id: str = "rA") -> table_versions.SnapshotPublish:
        """不带新导入的发布（移除、作废）：union=False 时快照库就是唯一那一期的构建库；union=True 时按 8 月、9 月的
        已发布构建重新物化（union_tmp 给了就拿它冒充这次物化的结果）。"""
        ub = None
        if union and union_tmp is None:
            out = table_versions.union_trial_path(
                table_versions.trial_db_path(self.sid, uuid.uuid4().hex, uuid.uuid4().hex))
            rep = ra.materialize_union(out, target=REF, parts=self.union_parts(
                table_versions.build_path(self.sid, self.sep_bid)))
            ub = self.union_build(out, rep)
        elif union:
            ub = table_versions.UnionBuild(tmp_db=union_tmp, union_id=self.uid, options={"kind": "union"}, report={},
                                           expected_sha256=raw_store.sha256_file(union_tmp))
        async with SessionLocal() as session:
            source = await session.get(DataSource, self.sid)
            return await table_versions.publish_snapshot(
                session, source, imports=imports, mode="accumulate", recipe_id=recipe_id, recipe_sha256=SHA_A,
                expected_current=expected, period_build=None, new_import=None, union=ub,
                schema_cache_for=plain_schema, activation={"kind": kind, "import_id": self.sep_import,
                                                           "reason": "合成理由", "signed_by": "测试员"})


async def chain() -> Chain:
    c = Chain()
    await c.first()
    await c.second()
    return c


# ==========================================================================
# id 与路径
# ==========================================================================


def test_snapshot_id_for_replace_is_unchanged_and_accumulate_carries_the_recipe():
    ids = P2_BASELINE["ids"]
    assert table_versions.snapshot_id(ids["source_id"], ids["import_ids"], ENGINE, mode="replace") \
        == ids["snapshot_id_recipe"]
    assert table_versions.snapshot_id(ids["source_id"], ids["import_ids"], ENGINE, mode=None, recipe_sha=SHA_A) \
        == ids["snapshot_id_recipe"]
    acc = table_versions.snapshot_id("s1", ["i1", "i2"], ENGINE, mode="accumulate", recipe_sha=SHA_A)
    assert acc == table_versions.sha_json({"source_id": "s1", "imports": ["i1", "i2"], "engine_ver": ENGINE,
                                           "mode": "accumulate", "recipe_sha256": SHA_A})
    assert acc != table_versions.snapshot_id("s1", ["i1", "i2"], ENGINE)
    assert acc != table_versions.snapshot_id("s1", ["i1", "i2"], ENGINE, mode="accumulate", recipe_sha=SHA_B)
    assert acc != table_versions.snapshot_id("s1", ["i2", "i1"], ENGINE, mode="accumulate", recipe_sha=SHA_A)
    with pytest.raises(ValueError):
        table_versions.snapshot_id("s1", ["i1"], ENGINE, mode="accumulate")


def test_union_paths_and_the_trial_union_file_name(store):
    uid = "c" * 64
    assert table_versions.union_path("src1", uid) == store / "uploads" / "tables" / "src1" / "snapshots" / f"{uid}.db"
    with pytest.raises(ValueError):
        table_versions.union_path("../x", uid)
    with pytest.raises(ValueError):
        table_versions.union_path("src1", "not-a-hash")
    trial = table_versions.trial_db_path("src1", "stg-1", "0123456789abcdef0123")
    union = table_versions.union_trial_path(trial)
    assert union.name == "stg-1-0123456789abcdefu.db" and union.parent == trial.parent
    # 文件名里不加「-」：按最后一个「-」切出的仍是暂存区 id；附属文件认得回主文件
    assert table_versions._trial_owner(union) == "stg-1"
    assert table_versions._trial_main(union) == trial and table_versions._trial_main(trial) == trial


def test_new_models_and_migrations_are_registered():
    from app.db import base as db_base

    migrated = {(t, c) for t, c, _ in db_base._COLUMN_MIGRATIONS}
    assert {("source_snapshots", c) for c in ("mode", "recipe_id", "activated_at", "manifest_artifact")} <= migrated
    assert {("table_imports", "revoked"), ("import_stagings", "edits"), ("import_stagings", "answers_dropped"),
            ("import_stagings", "redraft_sha256")} <= migrated
    assert SnapshotActivation.__tablename__ == "snapshot_activations"
    assert set(SnapshotActivation.__table__.columns.keys()) == {
        "id", "source_id", "snapshot_id", "previous_snapshot_id", "kind", "import_id", "reason", "signed_by",
        "created_at"}
    # 暂存区结束时期 3 的三个字段也清空
    assert {"edits", "answers_dropped", "redraft_sha256"} <= set(table_versions.STAGING_SLIM)
    assert table_versions.SNAPSHOT_KEEP == 3 and table_versions.QUARANTINE_DAYS == 7


def test_migration_adds_phase3_columns_to_an_old_database(tmp_path):
    from sqlalchemy import create_engine, text

    from app.db import base as db_base

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE source_snapshots (id VARCHAR(64) PRIMARY KEY, source_id VARCHAR(32))")
    conn.execute("INSERT INTO source_snapshots VALUES ('s1', 'src')")
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as sync:
            db_base.Base.metadata.create_all(sync)
            db_base._migrate(sync)
            row = sync.execute(text("SELECT mode, recipe_id, activated_at, manifest_artifact FROM source_snapshots")
                               ).one()
            tables = {r[0] for r in sync.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert tuple(row) == (None, None, None, None) and "snapshot_activations" in tables
    finally:
        engine.dispose()


# ==========================================================================
# publish_build 的期 3 字段
# ==========================================================================


async def test_publish_build_records_mode_recipe_and_an_activation(store):
    name = unique()
    sid, first = await publish_small(name, recipe_id="r1")
    snap = await get(SourceSnapshot, first.snapshot_id)
    assert snap.mode == "replace" and snap.recipe_id == "r1" and snap.activated_at is not None
    assert first.snapshot_id == table_versions.snapshot_id(sid, [first.import_id], ENGINE)
    _, second = await publish_small(name, snapshot_fields={"mode": "accumulate", "recipe_id": "r2",
                                                           "recipe_sha256": SHA_B},
                                    activation={"kind": "commit", "signed_by": "测试员"})
    assert second.snapshot_id == table_versions.snapshot_id(sid, [second.import_id], ENGINE, mode="accumulate",
                                                            recipe_sha=SHA_B)
    snap2 = await get(SourceSnapshot, second.snapshot_id)
    assert snap2.mode == "accumulate" and snap2.recipe_id == "r2"
    rows = await activations(sid)
    assert [(a.kind, a.snapshot_id, a.previous_snapshot_id) for a in rows] == [
        ("commit", first.snapshot_id, None), ("commit", second.snapshot_id, first.snapshot_id)]
    assert rows[0].import_id == first.import_id and rows[1].signed_by == "测试员"
    assert second.build_restored is False


# ==========================================================================
# publish_snapshot
# ==========================================================================


async def test_publish_snapshot_appends_a_period_as_a_union(store):
    c = Chain()
    first = await c.first()
    assert first.snapshot_id == table_versions.snapshot_id(c.sid, [c.aug_import], ENGINE, mode="accumulate",
                                                           recipe_sha=SHA_A)
    res = await c.second()
    assert res.snapshot_id == table_versions.snapshot_id(c.sid, [c.aug_import, c.sep_import], ENGINE,
                                                         mode="accumulate", recipe_sha=SHA_A)
    assert (res.union_id, res.build_id, res.import_id) == (c.uid, c.sep_bid, c.sep_import)
    assert not (res.build_reused or res.union_reused or res.snapshot_reused or res.snapshot_revived)
    assert res.seq == 2
    snap = await get(SourceSnapshot, res.snapshot_id)
    upath = table_versions.union_path(c.sid, c.uid)
    assert snap.imports == [c.aug_import, c.sep_import] and snap.mode == "accumulate" and snap.recipe_id == "rA"
    assert Path(snap.db_path) == upath and snap.db_sha256 == raw_store.sha256_file(upath)
    assert snap.manifest_artifact == c.manifest and snap.schema_cache["snapshot_manifest"] == c.manifest
    assert oct(upath.stat().st_mode & 0o777) == "0o444"
    union = await get(TableBuild, c.uid)
    assert union.engine_ver == recipe_types.UNION_VER and union.raw_sha256 == ""
    assert union.options["kind"] == "union" and union.report["rows"]["日客流"] == 61
    assert (await get(TableBuild, c.sep_bid)).db_sha256 == c.sep_sha
    assert [(i.id, i.status) for i in await imports_of(c.sid)] == [(c.aug_import, "active"), (c.sep_import, "active")]
    sep = await get(TableImport, c.sep_import)
    assert sep.period_start == "2026-09-01" and sep.recipe_id == "rA" and sep.raw_state == "kept"
    row = await get(DataSource, c.sid)
    assert row.current_snapshot_id == res.snapshot_id and row.current_recipe_id == "rA"
    assert row.database == str(upath)
    assert [(a.kind, a.previous_snapshot_id) for a in await activations(c.sid)][-1] == ("commit", c.s1)
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 61
    # 固定在 S1 上的查询仍是 8 月那 31 行
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"', c.s1) == 31
    # 临时文件都被消耗
    assert not c.sep_trial.exists() and not c.union_trial.exists()


async def test_publish_snapshot_refuses_a_stale_base(store):
    c = Chain()
    await c.first()
    with pytest.raises(table_versions.PublishError) as err:
        await c.second(expected="0" * 64)
    assert err.value.code == "base_changed" and "刷新" in str(err.value)
    assert await current(c.sid) == c.s1
    assert not table_versions.union_path(c.sid, c.uid).exists()
    assert not table_versions.build_path(c.sid, c.sep_bid).exists()
    assert not c.sep_trial.exists() and not c.union_trial.exists()
    assert len(await imports_of(c.sid)) == 1


async def test_publish_snapshot_rolls_back_when_before_commit_fails(store):
    c = Chain()
    await c.first()

    def failing(session):
        async def hook(import_id, snap_id):
            assert import_id == c.sep_import
            assert await session.get(TableImport, import_id) is not None
            # 锁内、指针还没切
            cur = (await session.execute(select(DataSource.current_snapshot_id)
                                         .where(DataSource.id == c.sid))).scalar()
            assert cur == c.s1
            raise RuntimeError("模拟提交前回调失败")
        return hook

    with pytest.raises(RuntimeError, match="模拟"):
        await c.second(before_commit=failing)
    assert await current(c.sid) == c.s1
    assert not table_versions.union_path(c.sid, c.uid).exists()
    assert not table_versions.build_path(c.sid, c.sep_bid).exists()
    assert await get(TableBuild, c.uid) is None and await get(TableImport, c.sep_import) is None
    assert [i.status for i in await imports_of(c.sid)] == ["active"]


async def test_publish_snapshot_needs_its_union_trial_file(store):
    c = Chain()
    await c.first()
    with pytest.raises(table_versions.PublishError) as err:
        await c.second(drop_union_tmp=True)
    assert err.value.code == "trial_missing"
    assert await current(c.sid) == c.s1 and not table_versions.build_path(c.sid, c.sep_bid).exists()


async def test_publish_snapshot_with_a_tampered_union_trial_is_refused(store):
    c = Chain()
    await c.first()
    real = ra.materialize_union

    def tampering(out, **kw):
        # 物化之后、提交之前，并集试运行库被人改了一行
        rep = real(out, **kw)
        conn = sqlite3.connect(out)
        conn.execute('UPDATE "日客流" SET "分区甲" = 0 WHERE rowid = 1')
        conn.commit()
        conn.close()
        return rep

    ra.materialize_union = tampering
    try:
        with pytest.raises(table_versions.PublishError) as err:
            await c.second()
    finally:
        ra.materialize_union = real
    assert err.value.code == "trial_tampered" and await current(c.sid) == c.s1
    assert not table_versions.union_path(c.sid, c.uid).exists()


async def test_removing_the_latest_period_reuses_the_earlier_snapshot(store):
    """8 月、9 月同一个配方：移除 9 月就是原来的 S1，直接切回去（配方、模式都不变）。"""
    c = await chain()
    res = await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert res.snapshot_id == c.s1 and res.snapshot_reused is True and res.snapshot_revived is False
    assert res.import_id is None and res.union_id is None
    row = await get(DataSource, c.sid)
    assert row.current_snapshot_id == c.s1 and row.current_recipe_id == "rA"
    assert [(i.id, i.status) for i in await imports_of(c.sid)] == [(c.aug_import, "active"),
                                                                    (c.sep_import, "superseded")]
    last = (await activations(c.sid))[-1]
    assert (last.kind, last.snapshot_id, last.previous_snapshot_id) == ("remove_period", c.s1, c.s2)
    assert (last.import_id, last.reason, last.signed_by) == (c.sep_import, "合成理由", "测试员")
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 31


async def test_retired_snapshot_whose_file_is_there_is_revived(store):
    c = await chain()
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s1)).retired_at = utcnow()
        await session.commit()
    res = await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert res.snapshot_id == c.s1 and res.snapshot_revived is True and res.snapshot_reused is False
    snap = await get(SourceSnapshot, c.s1)
    assert snap.retired_at is None and snap.activated_at is not None
    assert await current(c.sid) == c.s1


async def _back_to_s1_and_retire_s2(c: Chain) -> Path:
    """切回 S1，再把 S2 和它的并集构建标成已回收、删掉并集文件（模拟回收）。返回并集路径。"""
    await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    upath = table_versions.union_path(c.sid, c.uid)
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s2)).retired_at = utcnow()
        (await session.get(TableBuild, c.uid)).retired_at = utcnow()
        await session.commit()
    upath.unlink()
    return upath


async def test_retired_union_snapshot_is_revived_by_relinking_a_rematerialized_file(store):
    c = await chain()
    registered = (await get(SourceSnapshot, c.s2)).db_sha256
    upath = await _back_to_s1_and_retire_s2(c)
    res = await c.publish_existing([c.aug_import, c.sep_import], expected=c.s1, union=True,
                                   kind="revoke_acceptance")
    assert res.snapshot_id == c.s2 and res.snapshot_revived is True and res.union_reused is False
    assert raw_store.sha256_file(upath) == registered
    assert (await get(SourceSnapshot, c.s2)).retired_at is None
    assert (await get(TableBuild, c.uid)).retired_at is None
    assert await current(c.sid) == c.s2
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 61


async def test_reviving_with_a_different_file_is_a_build_conflict(store, tmp_path):
    c = await chain()
    upath = await _back_to_s1_and_retire_s2(c)
    bogus = table_versions.union_trial_path(table_versions.trial_db_path(c.sid, uuid.uuid4().hex, uuid.uuid4().hex))
    make_db(bogus, [(9, "不是这份")])
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import, c.sep_import], expected=c.s1, union=True, union_tmp=bogus)
    assert err.value.code == "build_conflict"
    assert not upath.exists() and not bogus.exists()        # 这次 link 出来的删掉，临时文件消耗掉
    assert await current(c.sid) == c.s1
    assert (await get(SourceSnapshot, c.s2)).retired_at is not None


@pytest.mark.parametrize("field", ["recipe_id", "imports", "mode", "db_path"])
async def test_existing_snapshot_with_other_content_is_a_conflict(store, field):
    """快照 id 相同、记录里的配方、各期、模式、库路径有一样对不上：id 规则被破坏了，不能复用（每一项单独钉住）。"""
    c = await chain()
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, c.s1)
        if field == "recipe_id":
            snap.recipe_id = "someone-else"
        elif field == "imports":
            snap.imports = [c.aug_import, "someone-else"]
        elif field == "mode":
            snap.mode = "replace"
        else:
            snap.db_path = str(Path(snap.db_path).with_name("elsewhere.db"))
        await session.commit()
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert err.value.code == "snapshot_conflict" and await current(c.sid) == c.s2


@pytest.mark.parametrize("registered", [SHA_A, SHA_B])
async def test_existing_snapshot_matches_on_recipe_content_not_record_id(store, registered):
    """快照 id 按配方内容哈希算（WP-8 修补）：已有快照记的配方记录 rA 与这次传的 rB 不是同一条、内容相同时照样复用，
    现行配方跟着快照记的那条（rA，与 activate_snapshot 一致）；rA 登记的哈希与这次的不同才是 snapshot_conflict。
    改回按记录 id 比，内容相同的那一半会误报冲突。"""
    c = await chain()
    ra_id, rb_id = new_id(), new_id()        # 配方表在各测试间共用，记录 id 不能写死
    async with SessionLocal() as session:
        session.add(TableRecipe(id=ra_id, source_id=c.sid, seq=1, recipe_sha256=registered, status="superseded"))
        session.add(TableRecipe(id=rb_id, source_id=c.sid, seq=2, recipe_sha256=SHA_A, status="active"))
        (await session.get(SourceSnapshot, c.s1)).recipe_id = ra_id
        await session.commit()
    if registered != SHA_A:
        with pytest.raises(table_versions.PublishError) as err:
            await c.publish_existing([c.aug_import], expected=c.s2, union=False, recipe_id=rb_id)
        assert err.value.code == "snapshot_conflict" and await current(c.sid) == c.s2
        return
    res = await c.publish_existing([c.aug_import], expected=c.s2, union=False, recipe_id=rb_id)
    assert res.snapshot_id == c.s1 and res.snapshot_reused is True
    row = await get(DataSource, c.sid)
    assert row.current_snapshot_id == c.s1 and row.current_recipe_id == ra_id
    assert (await get(SourceSnapshot, c.s1)).recipe_id == ra_id
    assert (await get(TableRecipe, ra_id)).status == "active" and (await get(TableRecipe, rb_id)).status == "superseded"


async def test_reusing_a_snapshot_whose_record_disagrees_with_its_file_is_a_build_conflict(store):
    """快照记录的库哈希被改了（文件、构建记录都没动）：不能把它切成当前版本，之后的查询会一律报篡改。"""
    c = await chain()
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s1)).db_sha256 = "0" * 64
        await session.commit()
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert err.value.code == "build_conflict" and await current(c.sid) == c.s2


async def test_reviving_when_the_union_record_disagrees_with_the_snapshot_is_a_build_conflict(store):
    """复活时重新物化的文件等于快照记的哈希、却不等于并集构建登记的：两处登记对不上，以构建登记为准，不复活。"""
    c = await chain()
    upath = await _back_to_s1_and_retire_s2(c)
    async with SessionLocal() as session:
        (await session.get(TableBuild, c.uid)).db_sha256 = "0" * 64
        await session.commit()
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import, c.sep_import], expected=c.s1, union=True, kind="revoke_acceptance")
    assert err.value.code == "build_conflict"
    assert not upath.exists() and await current(c.sid) == c.s1
    assert (await get(SourceSnapshot, c.s2)).retired_at is not None
    assert (await get(TableBuild, c.uid)).db_sha256 == "0" * 64


async def test_publishing_the_current_snapshot_again_is_already_current(store):
    c = await chain()
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import, c.sep_import], expected=c.s2, union=True)
    assert err.value.code == "already_current" and await current(c.sid) == c.s2
    assert [a.kind for a in await activations(c.sid)] == ["commit", "commit"]
    # 这次物化的临时文件照样被消耗
    assert not [p for p in table_versions._trial_files() if p.name.endswith("u.db")]


async def test_publish_snapshot_restores_a_tampered_union_file(store):
    """回到 S1 之后 S2 的并集文件被改成 0 字节（修改时间还是一个月前）：撤销时重新物化出登记的那一份，恢复它，
    原文件挪进隔离区，并且挺过发布后紧接着的回收。"""
    c = await chain()
    registered = (await get(TableBuild, c.uid)).db_sha256
    await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    upath = table_versions.union_path(c.sid, c.uid)
    os.chmod(upath, 0o644)
    upath.write_bytes(b"")
    month_ago = time.time() - 30 * 86400
    os.utime(upath, (month_ago, month_ago))
    res = await c.publish_existing([c.aug_import, c.sep_import], expected=c.s1, union=True, kind="revoke_acceptance")
    assert res.snapshot_id == c.s2 and res.snapshot_reused is True
    assert res.union_restored is True and res.build_restored is True and res.union_reused is True
    assert raw_store.sha256_file(upath) == registered and oct(upath.stat().st_mode & 0o777) == "0o444"
    [moved] = quarantined(c.sid)
    assert moved.name.startswith(f"{c.uid}-") and moved.stat().st_size == 0
    assert await current(c.sid) == c.s2
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 61


async def test_publish_snapshot_refuses_a_union_id_whose_layout_differs(store, tmp_path):
    """同一个并集 id、表结构却不同（调用方算 id 时漏了退役列）：照常复用会把用户没核对过的那份发布出去（H2），
    宁可 build_conflict。"""
    c = await chain()
    await c.publish_existing([c.aug_import], expected=c.s2, union=False)        # 当前回到 S1，S2 还保留着
    out = table_versions.union_trial_path(table_versions.trial_db_path(c.sid, uuid.uuid4().hex, uuid.uuid4().hex))
    retired = {"日客流": [recipe_types.ColumnOut("分区丁", "INTEGER")]}
    rep = ra.materialize_union(out, target=REF, parts=c.union_parts(table_versions.build_path(c.sid, c.sep_bid)),
                               retired=retired)
    assert rep.ok and [x.name for x in rep.tables["日客流"]][-1] == "分区丁"
    wrong = table_versions.UnionBuild(tmp_db=out, union_id=c.uid, options={"kind": "union"},
                                      report=table_versions.report_to_json(asdict(rep)),
                                      expected_sha256=rep.db_sha256)
    async with SessionLocal() as session:
        source = await session.get(DataSource, c.sid)
        with pytest.raises(table_versions.PublishError) as err:
            await table_versions.publish_snapshot(
                session, source, imports=[c.aug_import, c.sep_import], mode="accumulate", recipe_id="rA",
                recipe_sha256=SHA_A, expected_current=c.s1, period_build=None, new_import=None, union=wrong,
                schema_cache_for=plain_schema, activation={"kind": "revoke_acceptance"})
    assert err.value.code == "build_conflict" and "表结构" in str(err.value)
    assert await current(c.sid) == c.s1 and not out.exists()
    # 带上退役列算 id 就是另一个并集，各走各的
    assert ra.union_id(c.sid, [(c.aug_start, c.aug_end, c.aug_bid), (c.sep_start, c.sep_end, c.sep_bid)], SHA_A,
                       retired=retired) != c.uid


async def test_single_period_snapshot_whose_build_is_gone_says_where_to_go(store):
    c = await chain()
    async with SessionLocal() as session:
        await session.delete(await session.get(SourceSnapshot, c.s1))     # 逼它新建，而不是复用
        await session.commit()
    table_versions.build_path(c.sid, c.aug_bid).unlink()
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert err.value.code == "part_missing" and await current(c.sid) == c.s2
    assert "2026-08-01 至 2026-08-31" in str(err.value) and "启用更早的版本" in str(err.value)


async def test_single_period_snapshot_needs_its_build_file(store):
    c = await chain()
    async with SessionLocal() as session:
        await session.delete(await session.get(SourceSnapshot, c.s1))     # 逼它新建，而不是复用
        await session.commit()
    path = table_versions.build_path(c.sid, c.aug_bid)
    os.chmod(path, 0o644)
    with open(path, "ab") as fh:
        fh.write(b"\0")
    with pytest.raises(table_versions.PublishError) as err:
        await c.publish_existing([c.aug_import], expected=c.s2, union=False)
    assert err.value.code == "part_tampered" and await current(c.sid) == c.s2


async def test_publish_snapshot_rejects_inconsistent_arguments(store):
    c = Chain()
    await c.first()
    async with SessionLocal() as session:
        source = await session.get(DataSource, c.sid)
        kw = dict(mode="accumulate", recipe_id="rA", recipe_sha256=SHA_A, expected_current=c.s1,
                  period_build=None, new_import=None, schema_cache_for=plain_schema)
        with pytest.raises(ValueError):
            await table_versions.publish_snapshot(session, source, imports=[c.aug_import, "x"], union=None, **kw)
        with pytest.raises(ValueError):
            await table_versions.publish_snapshot(session, source, imports=[], union=None, **kw)
        with pytest.raises(ValueError):
            await table_versions.publish_snapshot(session, DataSource(name=unique()), imports=["x"], union=None,
                                                  **kw)


# ==========================================================================
# 启用旧快照（回滚）
# ==========================================================================


async def activate(c: Chain, snapshot_id: str, *, expected: str | None = None, ack=None, source_id=None,
                   kind: str = "activate") -> str:
    async with SessionLocal() as session:
        source = await session.get(DataSource, source_id or c.sid)
        return await table_versions.activate_snapshot(
            session, source, snapshot_id, expected_current=c.s2 if expected is None else expected, kind=kind,
            reason="回到上一版", signed_by="测试员", import_id=c.sep_import if kind != "activate" else None,
            ack_mask_lost=ack)


async def test_activate_switches_back_atomically(store):
    c = await chain()
    async with SessionLocal() as session:
        session.add(TableRecipe(id="rA", source_id=c.sid, seq=1, status="superseded"))
        session.add(TableRecipe(id="rB", source_id=c.sid, seq=2, status="active"))
        (await session.get(SourceSnapshot, c.s2)).recipe_id = "rB"
        (await session.get(DataSource, c.sid)).current_recipe_id = "rB"
        await session.commit()
    previous = await activate(c, c.s1)
    assert previous == c.s2
    row = await get(DataSource, c.sid)
    s1 = await get(SourceSnapshot, c.s1)
    assert row.current_snapshot_id == c.s1 and row.current_recipe_id == "rA"
    assert row.schema_cache == s1.schema_cache and row.database == s1.db_path
    assert [(i.id, i.status) for i in await imports_of(c.sid)] == [(c.aug_import, "active"),
                                                                    (c.sep_import, "superseded")]
    assert (await get(TableRecipe, "rA")).status == "active" and (await get(TableRecipe, "rB")).status == "superseded"
    assert s1.activated_at >= (await get(SourceSnapshot, c.s2)).activated_at
    last = (await activations(c.sid))[-1]
    assert (last.kind, last.snapshot_id, last.previous_snapshot_id, last.reason, last.signed_by) == (
        "activate", c.s1, c.s2, "回到上一版", "测试员")
    # 固定在 S2 上的查询不受影响，不固定的查到 S1
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"', c.s2) == 61
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 31


async def test_activating_a_pre_phase3_snapshot_takes_the_recipe_of_its_last_import(store):
    c = await chain()
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s1)).recipe_id = None
        await session.commit()
    await activate(c, c.s1)
    assert (await get(DataSource, c.sid)).current_recipe_id == "rA"


async def _break(c: Chain, how: str) -> None:
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, c.s1)
        if how == "retired":
            snap.retired_at = utcnow()
        elif how == "file_lost":
            snap.db_path = str(Path(snap.db_path).with_name("gone.db"))
        elif how == "tampered":
            snap.db_sha256 = "0" * 64
        elif how == "revoked":
            (await session.get(TableImport, c.aug_import)).revoked = {"at": "x", "reason": "合成", "signed_by": None,
                                                                     "signed_by_verified": False}
        await session.commit()


@pytest.mark.parametrize("how,code,text", [
    ("retired", "snapshot_unavailable", "已回收"),
    ("file_lost", "snapshot_unavailable", "已丢失"),
    ("tampered", "snapshot_tampered", "不一致"),
    ("revoked", "contains_revoked", "作废"),
])
async def test_activate_refuses_unusable_snapshots(store, how, code, text):
    c = await chain()
    await _break(c, how)
    with pytest.raises(table_versions.ActivationError) as err:
        await activate(c, c.s1)
    assert err.value.code == code and text in str(err.value)
    assert await current(c.sid) == c.s2
    assert [a.kind for a in await activations(c.sid)] == ["commit", "commit"]


async def test_activate_checks_the_base_the_target_and_the_source(store):
    c = await chain()
    for snapshot_id, expected, code in [
        (c.s1, c.s1, "base_changed"),
        ("f" * 64, None, "snapshot_not_found"),
        (c.s2, None, "already_current"),
    ]:
        with pytest.raises(table_versions.ActivationError) as err:
            await activate(c, snapshot_id, expected=expected)
        assert err.value.code == code
    other = Chain()
    await other.first()
    with pytest.raises(table_versions.ActivationError) as err:
        await activate(c, other.s1)
    assert err.value.code == "snapshot_not_found"
    async with SessionLocal() as session:
        manual = DataSource(name=unique(), kind="sqlite", database="/tmp/none.db")
        session.add(manual)
        await session.commit()
        manual_id = manual.id
    with pytest.raises(table_versions.ActivationError) as err:
        await activate(c, c.s1, source_id=manual_id, expected=None)
    assert err.value.code == "not_upload_source"
    assert await current(c.sid) == c.s2


async def test_activate_needs_an_ack_when_masked_columns_get_lost(store):
    c = await chain()
    async with SessionLocal() as session:
        (await session.get(DataSource, c.sid)).options = {"mask_columns": ["分区甲", "手机号"]}
        await session.commit()
    with pytest.raises(table_versions.ActivationError) as err:
        await activate(c, c.s1)
    assert err.value.code == "mask_lost" and "「手机号」" in str(err.value) and "分区甲" not in str(err.value)
    with pytest.raises(table_versions.ActivationError):
        await activate(c, c.s1, ack=["分区甲", "手机号"])
    await activate(c, c.s1, ack=["手机号"])
    assert await current(c.sid) == c.s1


def test_mask_lost_compares_names_without_case():
    cache = {"tables": {"日客流": {"columns": [{"name": "日期"}, {"name": "Phone"}]}}}
    assert table_versions.mask_lost({"mask_columns": "phone, 邮箱"}, cache) == ["邮箱"]
    assert table_versions.mask_lost({}, cache) == []
    assert table_versions.mask_lost({"mask_columns": ["日期"]}, None) == ["日期"]


# ==========================================================================
# 查询时用快照清单交叉核对（P3-SPEC 2.9）
# ==========================================================================


async def test_bind_snapshot_cross_checks_the_snapshot_manifest(store, monkeypatch):
    c = await chain()
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 61
    # 文件和库列一起被改成一致的假值：文件哈希核对照样通过，靠快照清单抓出来
    upath = table_versions.union_path(c.sid, c.uid)
    os.chmod(upath, 0o644)
    conn = sqlite3.connect(upath)
    conn.execute('UPDATE "日客流" SET "分区甲" = 0')
    conn.commit()
    conn.close()
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s2)).db_sha256 = raw_store.sha256_file(upath)
        await session.commit()
    with pytest.raises(table_versions.SnapshotManifestMismatch, match="版本清单不一致") as err:
        await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"')
    assert isinstance(err.value, table_versions.SnapshotMissing) and isinstance(err.value, SnapshotTampered)
    assert err.value.code == "snapshot_tampered"


async def test_manifest_cross_check_is_cached_per_process(store, monkeypatch):
    c = await chain()
    calls: list[str] = []
    real = artifact_store.load

    def spy(mid):
        calls.append(mid)
        return real(mid)

    monkeypatch.setattr(artifact_store, "load", spy)
    for _ in range(3):
        assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"') == 61
    assert calls == [c.manifest]


async def test_snapshot_whose_manifest_is_gone_is_refused(store):
    c = await chain()
    await c.publish_existing([c.aug_import], expected=c.s2, union=False)      # 当前回到 S1
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, c.s2)).manifest_artifact = "0" * 64
        await session.commit()
    with pytest.raises(table_versions.SnapshotManifestMismatch):
        await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"', c.s2)
    with pytest.raises(table_versions.ActivationError) as err:
        await activate(c, c.s2, expected=c.s1)
    assert err.value.code == "snapshot_tampered" and await current(c.sid) == c.s1


# ==========================================================================
# 回收：SNAPSHOT_KEEP、被运行引用的并集快照
# ==========================================================================


async def test_gc_keeps_the_latest_snapshots_of_each_source(store, monkeypatch):
    """当前版本之外再保留 SNAPSHOT_KEEP 个（P3-3）：KEEP=2、发布 5 次，留下当前的和此前的 2 个。"""
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 2)
    name = unique()
    published = [await publish_small(name, [(i, "分区甲")]) for i in range(5)]
    sid = published[0][0]
    snaps = [await get(SourceSnapshot, r.snapshot_id) for _, r in published]
    assert [s.retired_at is None for s in snaps] == [False, False, True, True, True]
    builds = [await get(TableBuild, r.build_id) for _, r in published]
    assert [Path(b.db_path).exists() for b in builds] == [False, False, True, True, True]
    assert [i.status for i in await imports_of(sid)] == ["retired", "retired", "superseded", "superseded", "active"]
    # 启用一个还保留着的旧版本：它排到最前，最早的那个保留版本被挤出去
    async with SessionLocal() as session:
        source = await session.get(DataSource, sid)
        await table_versions.activate_snapshot(session, source, published[2][1].snapshot_id,
                                               expected_current=published[4][1].snapshot_id, reason=None,
                                               signed_by=None)
    _, extra = await publish_small(name, [(9, "分区乙")])
    alive = {s.id for s in [await get(SourceSnapshot, r.snapshot_id) for _, r in published]
             if s.retired_at is None}
    assert alive == {published[2][1].snapshot_id, published[4][1].snapshot_id}
    assert (await get(SourceSnapshot, extra.snapshot_id)).retired_at is None


async def test_gc_quota_does_not_count_the_current_or_run_referenced_snapshots(store, monkeypatch):
    """界面写的是「除当前版本和被运行引用的版本外，保留最近 N 个」：这两类不占名额。"""
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 1)
    name = unique()
    sid, p0 = await publish_small(name, [(0, "分区甲")])
    _, p1 = await publish_small(name, [(1, "分区甲")])
    async with SessionLocal() as session:
        run = Run(workflow_name="wp4-quota", status="succeeded", data_versions={sid: {"snapshot": p1.snapshot_id}})
        session.add(run)
        await session.commit()
        run_id = run.id
    _, p2 = await publish_small(name, [(2, "分区甲")])
    # 当前的 p2、运行引用的 p1 之外，名额给了 p0
    assert [(await get(SourceSnapshot, r.snapshot_id)).retired_at is None for r in (p0, p1, p2)] == [True] * 3
    async with SessionLocal() as session:
        await session.delete(await session.get(Run, run_id))
        await session.commit()
    await gc()
    # 运行没了，p1 回到名额里排名：它比 p0 新，p0 被挤出去
    assert [(await get(SourceSnapshot, r.snapshot_id)).retired_at is None for r in (p0, p1, p2)] == [
        False, True, True]


async def test_gc_quota_does_not_count_retired_snapshots(store, monkeypatch):
    """已回收的快照不占名额：排进来只会挤掉还能启用的。"""
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 5)
    name = unique()
    published = [await publish_small(name, [(i, "分区甲")]) for i in range(3)]
    async with SessionLocal() as session:
        (await session.get(SourceSnapshot, published[1][1].snapshot_id)).retired_at = utcnow()
        await session.commit()
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 1)
    await gc()
    alive = [(await get(SourceSnapshot, r.snapshot_id)).retired_at is None for _, r in published]
    assert alive == [True, False, True]
    assert Path((await get(TableBuild, published[0][1].build_id)).db_path).exists()


async def test_gc_does_not_keep_snapshots_of_deleted_sources(store, monkeypatch):
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 3)
    name = unique()
    sid, first = await publish_small(name)
    async with SessionLocal() as session:
        await table_versions.retire_source(session, sid)
        await session.delete(await session.get(DataSource, sid))
        await session.commit()
    await gc()
    assert (await get(SourceSnapshot, first.snapshot_id)).retired_at is not None
    assert not Path((await get(TableBuild, first.build_id)).db_path).exists()


async def test_run_referenced_union_snapshot_survives_gc_until_the_run_is_gone(store, monkeypatch):
    """A4 的保护规则单独验证（评审一-m8）：SNAPSHOT_KEEP=0（当前版本本来就不占名额），S2（并集）只靠运行引用保护。"""
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 0)
    c = await chain()
    async with SessionLocal() as session:
        run = Run(workflow_name="wp4-gc", status="succeeded", data_versions={c.sid: {"snapshot": c.s2}})
        session.add(run)
        await session.commit()
        run_id = run.id
    await c.publish_existing([c.aug_import], expected=c.s2, union=False)      # 当前回到 S1
    upath = table_versions.union_path(c.sid, c.uid)
    assert upath.exists() and (await get(SourceSnapshot, c.s2)).retired_at is None
    assert (await get(TableBuild, c.uid)).retired_at is None
    assert await count_rows(c.sid, 'SELECT COUNT(*) FROM "日客流"', c.s2) == 61
    async with SessionLocal() as session:
        await session.delete(await session.get(Run, run_id))
        await session.commit()
    await gc()
    assert not upath.exists()
    assert (await get(SourceSnapshot, c.s2)).retired_at is not None
    assert (await get(TableBuild, c.uid)).retired_at is not None
    # 8 月还在当前版本里，构建库不动
    assert table_versions.build_path(c.sid, c.aug_bid).exists()


# ==========================================================================
# 孤儿清理、附属试运行库
# ==========================================================================


async def test_startup_sweeps_orphan_union_files_and_follows_the_main_trial(store):
    c = await chain()
    upath = table_versions.union_path(c.sid, c.uid)
    orphan = upath.with_name("d" * 64 + ".db")
    make_db(orphan, [(1, "孤儿")])
    sid = uuid.uuid4().hex
    key = uuid.uuid4().hex
    async with SessionLocal() as session:
        staging = ImportStaging(id=uuid.uuid4().hex, source_id=sid, source_name=unique(), status="trialed",
                                file_name="合成.xlsx")
        main = make_db(table_versions.trial_db_path(sid, staging.id, key), [(1, "主")])
        staging.trial_key, staging.trial_path = key, str(main)
        session.add(staging)
        await session.commit()
    union_trial = make_db(table_versions.union_trial_path(main), [(1, "并集")])
    stale = table_versions.trial_db_path(sid, staging.id, uuid.uuid4().hex)
    stale_union = make_db(table_versions.union_trial_path(stale), [(1, "旧的并集")])
    async with SessionLocal() as session:
        await table_versions.startup(session)
    assert not orphan.exists() and upath.exists()
    assert main.exists() and union_trial.exists()        # 附属文件跟着主文件：主文件有效就留着
    assert not stale_union.exists()                      # 主文件不是暂存区记着的那个：孤儿
    assert table_versions.remove_trial_file(str(main)) is True
    assert not main.exists() and not union_trial.exists()


# ==========================================================================
# 隔离区的清理（第 8 节遗留项 1）
# ==========================================================================


def quarantine(source_id: str, build_id: str, *, age_days: float = 0) -> Path:
    folder = table_versions.quarantine_root() / source_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{build_id}-20260101T000000000000Z.db"
    path.write_bytes(b"quarantined")
    if age_days:
        then = time.time() - age_days * 86400
        os.utime(path, (then, then))
    return path


async def test_retire_source_deletes_its_quarantine(store):
    name = unique()
    sid, first = await publish_small(name)
    mine = quarantine(sid, first.build_id)
    other = quarantine(uuid.uuid4().hex, "e" * 64)
    async with SessionLocal() as session:
        await table_versions.retire_source(session, sid)
        await session.commit()
    assert not mine.exists() and not mine.parent.exists() and other.exists()


async def test_purging_a_raw_deletes_the_quarantined_files_of_those_builds(store):
    name = unique()
    sid, first = await publish_small(name)
    _, second = await publish_small(name, [(5, "分区丙")])
    gone = quarantine(sid, first.build_id)
    kept = quarantine(sid, second.build_id)
    async with SessionLocal() as session:
        imp = await session.get(TableImport, first.import_id)
        await table_versions.purge_import_raw(session, imp, reason="合成理由", signed_by="测试员")
    assert not gone.exists() and kept.exists()


def quarantined(source_id: str) -> list[Path]:
    folder = table_versions.quarantine_root() / source_id
    return sorted(folder.glob("*.db")) if folder.is_dir() else []


async def test_quarantine_keeps_a_restored_file_for_seven_days_from_the_move(store):
    """走真实路径（_publish_file → _quarantine）：原文件的修改时间早在 30 天前（构建多半是几周前发布的，磁盘位
    翻转这类损坏也不改修改时间），保留期仍从挪进隔离区的那一刻算。发布之后紧接着的回收不能把证据删掉。"""
    name, raw = unique(), raw_bytes()
    sid, first = await publish_small(name, raw=raw)
    path = Path((await get(TableBuild, first.build_id)).db_path)
    os.chmod(path, 0o644)
    path.write_bytes(b"")
    month_ago = time.time() - 30 * 86400
    os.utime(path, (month_ago, month_ago))
    _, second = await publish_small(name, raw=raw)          # publish_build 提交后紧接着回收一次
    assert second.build_restored is True and second.build_id == first.build_id
    [moved] = quarantined(sid)
    assert moved.stat().st_size == 0 and time.time() - moved.stat().st_mtime < 3600
    await gc()
    assert moved.exists()
    # 过了保留期才删
    stale = time.time() - (table_versions.QUARANTINE_DAYS + 1) * 86400
    os.utime(moved, (stale, stale))
    await gc()
    assert not moved.exists()


async def test_gc_deletes_quarantined_files_older_than_seven_days(store):
    old = quarantine(uuid.uuid4().hex, "1" * 64, age_days=table_versions.QUARANTINE_DAYS + 1)
    young = quarantine(uuid.uuid4().hex, "2" * 64, age_days=table_versions.QUARANTINE_DAYS - 1)
    await gc()
    assert not old.exists() and young.exists()


async def test_startup_does_not_touch_the_quarantine(store):
    path = quarantine(uuid.uuid4().hex, "3" * 64)
    async with SessionLocal() as session:
        await table_versions.startup(session)
    assert path.exists()


# ==========================================================================
# 单进程守卫（第 8 节遗留项 5）
# ==========================================================================

_HOLD = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def guard_reset():
    yield
    table_versions._release_guard()


def hold_guard() -> subprocess.Popen:
    path = settings.uploads_dir / ".store.lock"
    proc = subprocess.Popen([sys.executable, "-c", _HOLD, str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def release(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    proc.wait(timeout=10)


async def test_guard_is_idempotent_in_one_process(store, guard_reset):
    assert table_versions.acquire_store_guard() is True
    fd = table_versions._guard.fd
    assert table_versions.acquire_store_guard() is True and table_versions._guard.fd == fd
    table_versions.require_store_writable()
    async with SessionLocal() as session:
        await table_versions.startup(session)
        await table_versions.startup(session)


async def test_second_process_skips_startup_cleanup_and_refuses_writes(store, guard_reset, monkeypatch):
    """L5：子进程先占住守卫，本进程启动时一个文件都不删、不回收，写入一律 StoreUnavailable；子进程退出后恢复。"""
    name = unique()
    sid, first = await publish_small(name)
    half = table_versions.build_path(sid, "1" * 64).with_name(f"{'1' * 64}.db{raw_store.TMP_MARK}zzz")
    half.write_bytes(b"half written")
    orphan = make_db(table_versions.build_path(uuid.uuid4().hex, "0" * 64), [(1, "孤儿")])
    _orphan_sha, orphan_raw = raw_store.put_raw(b"raw nobody registered")
    expired_id = uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add(ImportStaging(id=expired_id, source_id=uuid.uuid4().hex, source_name=unique(), status="drafting",
                                  expires_at=utcnow() - timedelta(days=1)))
        await session.commit()
    ran: list[str] = []

    async def spy_gc(session):
        ran.append("gc")

    real_gc = table_versions.gc
    proc = hold_guard()
    try:
        assert table_versions.acquire_store_guard() is False
        monkeypatch.setattr(table_versions, "gc", spy_gc)
        async with SessionLocal() as session:
            await table_versions.startup(session)
        monkeypatch.setattr(table_versions, "gc", real_gc)
        assert ran == []
        assert half.exists() and orphan.exists() and orphan_raw.exists()
        with pytest.raises(table_versions.StoreUnavailable) as err:
            table_versions.require_store_writable()
        assert err.value.code == "store_unavailable" and "单进程" in str(err.value)
        # 写入的入口一律拒；临时库照样被消耗
        tmp = make_db(table_versions.trial_db_path(sid, uuid.uuid4().hex, uuid.uuid4().hex), [(3, "分区甲")])
        with pytest.raises(table_versions.StoreUnavailable):
            async with SessionLocal() as session:
                source = await session.get(DataSource, sid)
                raw = raw_bytes()
                await table_versions.publish_build(
                    session, source, tmp_db=tmp, build_id="9" * 64, build_options={}, engine_ver=ENGINE,
                    report={}, raw=raw, raw_sha=hashlib.sha256(raw).hexdigest(), file_name="x.xlsx",
                    file_size=len(raw), schema_cache_for=plain_schema, import_fields={})
        assert not tmp.exists()
        # 每个入口用新会话：失败时会话回滚，里面的对象随之过期
        refused = {
            "publish_upload": lambda session, source: table_versions.publish_upload(session, source, b"raw", "x.xlsx"),
            "activate_snapshot": lambda session, source: table_versions.activate_snapshot(
                session, source, first.snapshot_id, expected_current=first.snapshot_id, reason=None, signed_by=None),
            "publish_snapshot": lambda session, source: table_versions.publish_snapshot(
                session, source, imports=[first.import_id], mode="replace", recipe_id=None, recipe_sha256=None,
                expected_current=first.snapshot_id, period_build=None, new_import=None, union=None,
                schema_cache_for=plain_schema),
            "gc": lambda session, source: table_versions.gc(session),
            "retire_source": lambda session, source: table_versions.retire_source(session, source.id),
            "store_staging": lambda session, source: table_versions.store_staging(
                session, ImportStaging(source_id=uuid.uuid4().hex, source_name=unique()), raw_bytes()),
            "release_staging_raw": lambda session, source: table_versions.release_staging_raw(
                session, ImportStaging(source_id=uuid.uuid4().hex, status="discarded", raw_sha256="4" * 64)),
        }
        for what, call in refused.items():
            async with SessionLocal() as session:
                source = await session.get(DataSource, sid)
                with pytest.raises(table_versions.StoreUnavailable):
                    await call(session, source)
        async with SessionLocal() as session:
            with pytest.raises(table_versions.StoreUnavailable):
                await table_versions.purge_import_raw(session, await session.get(TableImport, first.import_id),
                                                      reason="合成", signed_by=None)
        async with SessionLocal() as session:
            # 过期处理直接跳过、不抛：具体的写入那一步再报 503
            assert await table_versions.expire_stagings(session) == []
        assert (await get(ImportStaging, expired_id)).status == "drafting"
        assert await current(sid) == first.snapshot_id
    finally:
        release(proc)
    assert table_versions.acquire_store_guard() is True
    table_versions.require_store_writable()
    async with SessionLocal() as session:
        await table_versions.startup(session)
    assert not half.exists() and not orphan.exists() and not orphan_raw.exists()


async def test_guard_state_does_not_leak_into_another_data_dir(store, guard_reset, monkeypatch, tmp_path):
    proc = hold_guard()
    try:
        assert table_versions.acquire_store_guard() is False
        other = tmp_path / "other"
        (other / "uploads").mkdir(parents=True)
        monkeypatch.setattr(settings, "data_dir", other)
        # 换了数据目录、没对它取过守卫：按能写处理（只有启动时取守卫）
        table_versions.require_store_writable()
        assert table_versions.store_writable() is True
    finally:
        release(proc)


def test_versions_page_keep_count_matches_backend():
    """版本页的保留规则那句话写死了保留数（快照列表接口只返回数组，不带这个数）：前后端必须一致，改一边忘了另一边，
    界面就会把保留规则说错。"""
    import re
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "import" / "VersionsDialog.tsx"
    found = re.search(r"^const KEEP_RECENT = (\d+)$", page.read_text(encoding="utf-8"), re.M)
    assert found, "VersionsDialog.tsx 里找不到 KEEP_RECENT"
    assert int(found.group(1)) == table_versions.SNAPSHOT_KEEP
