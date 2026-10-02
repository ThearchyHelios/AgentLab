"""按配方导入（期 2）的版本底座：数据模型、publish_build、暂存区的回收与清理保护。

要守住的几件事：
- publish_build 发布的必须是用户试运行、核对过的那份库：哈希对不上不 link；试运行库不在时不能
  新建一个空库发布出去；任何一步（包括锁内的提交前回调）失败，指针不动、不留新文件；
- 发布交给 schema_cache_for 的是实际发布的库哈希（复用构建时是已有文件的），导入清单据此记录；
- 未结束暂存区的原件、配方源最近 12 次导入的原件不被回收；暂存区一结束就瘦身；
- 清除原件连带放弃引用它的暂存区；放弃导入时原件没有别的引用就当场删掉。

夹具一律现造：试运行库用 sqlite3 直接建，原件是随机字节，标签用假名。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import sqlite3
import stat
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text

from app.core.config import settings
from app.data import raw_store, recipe_parsers, recipe_types, table_versions
from app.data.engine import engines, run_query
from app.db import base as db_base
from app.db.base import SessionLocal, utcnow
from app.db.models import (
    DataSource, ImportStaging, SourceSnapshot, TableBuild, TableImport, TableRecipe,
)


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """每个测试一个独立的数据目录：上传目录、原件存档、工件都在里面。"""
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

RECIPE_SHA = "c" * 64


def unique(prefix: str = "wp5a") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def raw_bytes() -> bytes:
    """内容独一份的「原件」：原件按内容共用，别的测试传过同样字节就会互相影响。"""
    return f"合成原件 {uuid.uuid4().hex}".encode()


def make_db(path: Path, rows: list[tuple[int, str]] | None = None, *, wal: bool = False) -> Path:
    """一张 STRICT 表 t 的小库。wal=True 时切成 WAL 模式再写（文件头第 18、19 字节是 2）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute('CREATE TABLE "t" ("n" INTEGER, "s" TEXT) STRICT')
        conn.executemany('INSERT INTO "t" VALUES (?, ?)', rows or [(1, "分区甲"), (2, "分区乙")])
        conn.commit()
    finally:
        conn.close()
    return path


def new_trial(source_id: str, staging_id: str | None = None, **kw: Any) -> Path:
    return make_db(table_versions.trial_db_path(
        source_id, staging_id or uuid.uuid4().hex, uuid.uuid4().hex), **kw)


def settled_sha(path: Path) -> str:
    table_versions.settle_journal(path)
    return raw_store.sha256_file(path)


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def get(model, key):
    async with SessionLocal() as session:
        return await session.get(model, key)


async def imports_of(source_id: str) -> list[TableImport]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(TableImport).where(TableImport.source_id == source_id).order_by(TableImport.seq)
        )).scalars())


async def publish_trial(
    name: str, tmp: Path, raw: bytes, *, source_id: str | None = None, pass_raw: bool = True,
    expected: str | None | bool = True, build_id: str | None = None,
    import_fields: dict[str, Any] | None = None, import_id: str | None = None,
    before_commit=None, seen: list | None = None, schema_extra=None,
) -> tuple[str, table_versions.ImportResult]:
    """按配方导入的方式发布一个试运行库。expected=True 时先 settle 再算哈希作期望值（同 run_trial）。

    before_commit 是 (session, source_id) -> 协程函数 的工厂：回调要用同一个会话。
    """
    raw_sha = hashlib.sha256(raw).hexdigest()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(id=source_id or uuid.uuid4().hex, name=name, kind="sqlite",
                             readonly=True, origin="upload")
        sid = row.id
        bid = build_id or table_versions.recipe_build_id(sid, raw_sha, RECIPE_SHA, {})
        if expected is True:
            expected = settled_sha(tmp)

        async def schema(view, info):
            if seen is not None:
                seen.append((view, info))
            cache = await table_versions.introspect_view(view, {})
            table_versions.check_probe(cache, {"tables": [{"name": "t"}]}, sid)
            if schema_extra is not None:
                await schema_extra(cache, info)
            return cache

        result = await table_versions.publish_build(
            session, row, tmp_db=tmp, build_id=bid,
            build_options=table_versions.recipe_build_options(RECIPE_SHA, {}),
            engine_ver=recipe_types.RECIPE_ENGINE_VER, report={"tables": [{"name": "t"}]},
            raw=raw if pass_raw else None, raw_sha=raw_sha, file_name="月报导出.xlsx",
            file_size=len(raw), schema_cache_for=schema, import_fields=import_fields or {},
            expected_db_sha256=expected or None,
            before_commit=before_commit(session, sid) if before_commit else None,
            import_id=import_id,
        )
        return sid, result


async def total(source_id: str) -> int:
    async with SessionLocal() as session:
        view = await table_versions.resolve_source(session, await session.get(DataSource, source_id))
    try:
        return (await run_query(view, 'SELECT SUM("n") FROM "t"')).rows[0][0]
    finally:
        await engines.invalidate(source_id)


SLIMMED = {"scan": None, "grid_preview": None, "facts": None, "drafts": None, "trial": None,
           "answers_base": None, "cards": [], "questions": [], "draft_problems": [],
           "answers": {}, "recipe_problems": [], "context_inputs": {},
           "trial_key": None, "trial_path": None}


async def new_staging(*, raw: bytes | None = None, source_id: str | None = None,
                      status: str = "drafting", trial: bool = False, **kw: Any) -> tuple[str, Path | None]:
    """一个填满了大字段的暂存区。raw 给了就经 store_staging 存原件。返回 (暂存区 id, 试运行库路径)。"""
    kw.setdefault("id", uuid.uuid4().hex)
    staging = ImportStaging(
        source_id=source_id or uuid.uuid4().hex, source_name=unique(),
        kind="first", status=status, file_name="月报导出.xlsx",
        scan={"sheets": [{"name": "客流汇总"}]},
        grid_preview=[{"sheet": "客流汇总", "cells": [[5, 3, "6123", "number"]]}],
        facts={"facts": [{"id": "F1"}]}, drafts={"rules": {"complete": True}, "ai": None},
        recipe={"format": "agentlab-recipe/2"}, recipe_origin="rules", recipe_sha256=RECIPE_SHA,
        answers_base={"format": "agentlab-recipe/2"},
        answers={"q_relation:F2": {"value": "dismiss", "reason": "合成理由"}},
        cards=[{"id": "c1"}], questions=[{"id": "q_relation:F2"}], recipe_problems=[{"code": "x"}],
        draft_problems=[{"code": "y"}], draft_partial=True, context_inputs={"统计期": {"start": "2026-09-01"}},
        trial={"trial_id": "t", "receipt": {"outside_text": [{"text": "注：合成说明 12 处"}]}},
        ai_usage=[{"total_tokens": 10}], ai_consents=[{"signed_by": "测试员"}], signed_by="测试员",
        **kw,
    )
    path = None
    if trial:
        key = uuid.uuid4().hex
        path = make_db(table_versions.trial_db_path(staging.source_id, staging.id, key))
        staging.trial_key, staging.trial_path = key, str(path)
    async with SessionLocal() as session:
        if raw is not None:
            await table_versions.store_staging(session, staging, raw)
        else:
            session.add(staging)
            await session.commit()
    return staging.id, path


def assert_slimmed(staging: ImportStaging, status: str) -> None:
    assert staging.status == status and staging.closed_at is not None
    for name, value in SLIMMED.items():
        assert getattr(staging, name) == value, name
    # 留下的：来源、文件哈希、工作配方、用量、同意记录、署名
    assert staging.recipe == {"format": "agentlab-recipe/2"} and staging.recipe_sha256 == RECIPE_SHA
    assert staging.ai_usage == [{"total_tokens": 10}] and staging.ai_consents == [{"signed_by": "测试员"}]
    assert staging.signed_by == "测试员" and staging.file_name == "月报导出.xlsx"


async def gc() -> None:
    async with SessionLocal() as session:
        await table_versions.gc(session)


async def startup() -> None:
    async with SessionLocal() as session:
        await table_versions.startup(session)


# --------------------------------------------------------------------------
# 数据模型与迁移
# --------------------------------------------------------------------------

NEW_IMPORT_COLUMNS = {
    "recipe_id", "period_start", "period_end", "context", "checks", "overrides", "waivers",
    "confirmations", "manifest_artifact", "signed_by", "staging_id",
}


def test_new_columns_are_mapped_and_migrated():
    migrated = {(t, c) for t, c, _ in db_base._COLUMN_MIGRATIONS}
    assert ("data_sources", "current_recipe_id") in migrated
    assert {("table_imports", c) for c in NEW_IMPORT_COLUMNS} <= migrated
    assert "current_recipe_id" in DataSource.__table__.columns
    assert NEW_IMPORT_COLUMNS <= set(TableImport.__table__.columns.keys())
    # publish_build 能写的列正是这些，不多不少
    assert table_versions.IMPORT_FIELDS == NEW_IMPORT_COLUMNS
    # 配方格式的默认值与契约一致（模型不 import 数据层）
    assert TableRecipe.__table__.columns["recipe_format"].default.arg == recipe_types.RECIPE_FORMAT


def test_migration_adds_the_new_columns_to_an_old_database(tmp_path):
    """期 1 的库升级：新列补上、存量行为 NULL；新表由 create_all 建。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE data_sources (id VARCHAR(32) PRIMARY KEY, name VARCHAR(100))")
    conn.execute("CREATE TABLE table_imports (id VARCHAR(32) PRIMARY KEY, source_id VARCHAR(32), seq INTEGER)")
    conn.execute("INSERT INTO table_imports VALUES ('i1', 's1', 1)")
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as sync:
            # 和 init_db 同序：先 create_all（只建新表），再补列
            db_base.Base.metadata.create_all(sync)
            db_base._migrate(sync)
            imp_cols = {row[1] for row in sync.execute(text("PRAGMA table_info(table_imports)"))}
            src_cols = {row[1] for row in sync.execute(text("PRAGMA table_info(data_sources)"))}
            tables = {row[0] for row in sync.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
            row = sync.execute(text("SELECT recipe_id, checks, manifest_artifact FROM table_imports")).one()
        assert NEW_IMPORT_COLUMNS <= imp_cols and "current_recipe_id" in src_cols
        assert {"table_recipes", "import_stagings"} <= tables
        assert tuple(row) == (None, None, None)
        # 再跑一遍不报错（幂等）
        with engine.begin() as sync:
            db_base._migrate(sync)
    finally:
        engine.dispose()


async def test_recipe_seq_is_unique_per_source():
    sid = uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add(TableRecipe(source_id=sid, seq=1, recipe={}, recipe_sha256=RECIPE_SHA))
        await session.commit()
        session.add(TableRecipe(source_id=sid, seq=1, recipe={}, recipe_sha256=RECIPE_SHA))
        with pytest.raises(Exception, match="UNIQUE"):
            await session.commit()


# --------------------------------------------------------------------------
# id、公开名、错误码
# --------------------------------------------------------------------------


def test_recipe_build_id_covers_every_input(monkeypatch):
    base = table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {})
    assert len(base) == 64
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {}) == base
    assert table_versions.recipe_build_id("s2", "a" * 64, "b" * 64, {}) != base
    assert table_versions.recipe_build_id("s1", "d" * 64, "b" * 64, {}) != base
    assert table_versions.recipe_build_id("s1", "a" * 64, "e" * 64, {}) != base
    period = {"context": {"统计期": {"start": "2026-09-01", "end": "2026-09-30"}}}
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, period) != base
    # 与期 1 的构建 id 不会撞：组成不同
    opts = table_versions.parse_options("a.xlsx", header_row=1, mixed="reject", raw_mode=False)
    assert table_versions.build_id("s1", "a" * 64, opts) != base
    # 解析器、执行器升级换掉构建 id
    monkeypatch.setattr(recipe_parsers, "PARSER_VER", recipe_parsers.PARSER_VER + "-next")
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {}) != base
    monkeypatch.undo()
    monkeypatch.setattr(recipe_types, "RECIPE_ENGINE_VER", "recipe/2")
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {}) != base


def test_recipe_build_options_shape():
    opts = table_versions.recipe_build_options("b" * 64, {})
    assert opts == {"kind": "recipe", "recipe_sha256": "b" * 64,
                    "parser_ver": recipe_parsers.PARSER_VER, "inputs": {}}
    assert table_versions.recipe_build_options("b" * 64, {}, semantics={}) == opts


def test_engine_semantics_marks_only_lists_that_skip_blank_rows():
    """执行器语义标记（WP-8 评审意见 1）：只有含 blank_rows=skip 列表块的配方带 list_note_after_blank，构建 id 和
    构建选项随之变；交叉表、stop 模式的列表不带，构建 id 与期 2 逐字相同（不升 RECIPE_ENGINE_VER 的前提）。"""
    def recipe(**rows: Any) -> recipe_types.Recipe:
        return recipe_types.Recipe.model_validate({
            "recipe_format": "agentlab-recipe/2",
            "sheets": [{"id": "s1", "match": {"name": "明细"}, "blocks": [{
                "id": "列表1", "layout": "list", "table": "明细",
                "columns": [{"header": "地区", "name": "地区", "type": "TEXT"},
                            {"header": "金额", "name": "金额", "type": "INTEGER"}],
                "rows": rows}]}],
            "tables": [{"name": "明细", "grain": ["地区"]}]})

    mark = {table_versions.LIST_NOTE_AFTER_BLANK: 1}
    assert table_versions.recipe_engine_semantics(recipe(blank_rows="skip")) == mark
    assert table_versions.recipe_engine_semantics(recipe()) == {}
    assert table_versions.recipe_engine_semantics(recipe(blank_rows="stop")) == {}
    assert table_versions.recipe_engine_semantics(None) == {}
    base = table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {})
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {}, semantics={}) == base
    assert table_versions.recipe_build_id("s1", "a" * 64, "b" * 64, {}, semantics=mark) != base
    opts = table_versions.recipe_build_options("b" * 64, {}, semantics=mark)
    assert opts["engine_semantics"] == mark


def test_public_names_keep_the_old_aliases():
    tv = table_versions
    assert tv._store_lock is tv.store_lock and tv._parse_slot is tv.parse_slot
    assert tv._introspect_view is tv.introspect_view and tv._check_probe is tv.check_probe
    assert tv._sha_json is tv.sha_json and tv._settle_journal is tv.settle_journal
    assert tv.PublishError("x").code is None
    assert tv.PublishError("x", code="trial_missing").code == "trial_missing"


def test_trial_db_path_layout_and_validation(store):
    path = table_versions.trial_db_path("src1", "stg1", "f" * 64)
    assert path == store / "uploads" / "tables" / "src1" / "trials" / f"stg1-{'f' * 16}.db"
    assert table_versions._trial_owner(path) == "stg1"
    # 暂存区 id 带「-」也认得回来（trial_key 只有字母数字，从右边切）
    dashed = table_versions.trial_db_path("src1", "stg-1_x", "a" * 64)
    assert dashed.name == f"stg-1_x-{'a' * 16}.db" and table_versions._trial_owner(dashed) == "stg-1_x"
    for bad in (("../x", "stg", "f" * 64), ("src", "a/b", "f" * 64), ("src", "stg", "short")):
        with pytest.raises(ValueError):
            table_versions.trial_db_path(*bad)


# --------------------------------------------------------------------------
# publish_build
# --------------------------------------------------------------------------


async def test_publish_build_publishes_a_trial_db_with_recipe_fields(store):
    name, raw = unique(), raw_bytes()
    sid = uuid.uuid4().hex
    tmp = new_trial(sid)
    seen: list = []
    manifests: list[str] = []
    imp_id = uuid.uuid4().hex

    async def write_manifest(cache, info):
        # 配方导入在这里写导入清单：put_json 另开会话提交，主会话此时不能占着写锁
        from app.core import artifact_store

        mid = await artifact_store.put_json(
            {"import_id": info.import_id, "db_sha256": info.db_sha256}, kind="import_manifest")
        manifests.append(mid)
        cache["import_manifests"] = [mid]

    fields = {"recipe_id": "r" * 32, "period_start": "2026-09-01", "period_end": "2026-09-30",
              "checks": [{"id": "C2", "status": "passed"}], "signed_by": "测试员", "staging_id": "s" * 32}
    _, result = await publish_trial(name, tmp, raw, source_id=sid, import_fields=fields,
                                    import_id=imp_id, seen=seen, schema_extra=write_manifest)

    assert not tmp.exists()                                    # 试运行库被消耗
    row = await get(DataSource, sid)
    assert row.origin == "upload" and row.current_snapshot_id == result.snapshot_id
    assert result.import_id == imp_id and result.seq == 1 and result.build_reused is False
    assert result.snapshot_id == table_versions.snapshot_id(sid, [imp_id], recipe_types.RECIPE_ENGINE_VER)
    build = await get(TableBuild, result.build_id)
    assert build.engine_ver == recipe_types.RECIPE_ENGINE_VER
    assert build.options == table_versions.recipe_build_options(RECIPE_SHA, {})
    assert build.options_sha256 == table_versions.sha_json(build.options)
    assert stat.S_IMODE(Path(build.db_path).stat().st_mode) == 0o444
    assert result.db_sha256 == build.db_sha256 == raw_store.sha256_file(Path(build.db_path))
    [imp] = await imports_of(sid)
    for key, value in fields.items():
        assert getattr(imp, key) == value
    assert imp.raw_state == "kept" and raw_store.raw_exists(imp.raw_sha256)
    # schema_cache_for 收到绑定新快照的视图和实际发布的结果
    [(view, info)] = seen
    assert view.snapshot_id == result.snapshot_id and view.database == build.db_path
    assert (info.import_id, info.snapshot_id, info.seq, info.build_id) == (imp_id, result.snapshot_id, 1, result.build_id)
    assert info.db_sha256 == build.db_sha256 and info.build_reused is False
    snap = await get(SourceSnapshot, result.snapshot_id)
    assert snap.schema_cache["import_manifests"] == manifests and set(snap.schema_cache["tables"]) == {"t"}
    assert await total(sid) == 3


async def test_publish_build_refuses_a_tampered_trial(store):
    """试运行之后库被改过：不 link、不写任何记录、指针不动，试运行库照样被消耗。"""
    name, raw = unique(), raw_bytes()
    sid, first = await publish_trial(name, new_trial(uuid.uuid4().hex), raw_bytes())
    before = files_under(store / "uploads" / "tables" / sid / "builds")
    tmp = new_trial(sid)
    expected = settled_sha(tmp)
    conn = sqlite3.connect(tmp)
    conn.execute('UPDATE "t" SET "n" = 1000')
    conn.commit()
    conn.close()
    with pytest.raises(table_versions.PublishError) as info:
        await publish_trial(name, tmp, raw, expected=expected)
    assert info.value.code == "trial_tampered" and "请重新试运行" in str(info.value)
    assert files_under(store / "uploads" / "tables" / sid / "builds") == before
    assert not tmp.exists()
    assert (await get(DataSource, sid)).current_snapshot_id == first.snapshot_id
    assert len(await imports_of(sid)) == 1
    assert not raw_store.raw_exists(hashlib.sha256(raw).hexdigest())   # 这次新存的原件也删了
    assert await total(sid) == 3


@pytest.mark.parametrize("state", ["missing", "empty"])
async def test_publish_build_without_a_trial_db_never_creates_an_empty_one(store, state):
    name, raw = unique(), raw_bytes()
    sid = uuid.uuid4().hex
    tmp = table_versions.trial_db_path(sid, uuid.uuid4().hex, uuid.uuid4().hex)
    if state == "empty":
        tmp.parent.mkdir(parents=True)
        tmp.write_bytes(b"")
    with pytest.raises(table_versions.PublishError) as info:
        await publish_trial(name, tmp, raw, source_id=sid, expected=None)
    assert info.value.code == "trial_missing"
    assert not tmp.exists()
    assert not files_under(store / "uploads" / "tables" / sid / "builds")
    assert await get(DataSource, sid) is None
    assert not raw_store.raw_exists(hashlib.sha256(raw).hexdigest())


async def test_publish_build_ignores_a_trial_path_outside_the_uploads_dir(store, tmp_path):
    outside = make_db(tmp_path / "elsewhere" / "x.db")
    before = outside.read_bytes()
    with pytest.raises(table_versions.PublishError) as info:
        await publish_trial(unique(), outside, raw_bytes(), expected=None)
    assert info.value.code == "trial_missing"
    assert outside.read_bytes() == before                      # 不是我们的文件，不删


async def test_wal_trial_settled_before_hashing_matches_at_publish(store):
    """试运行库要是 WAL 模式，先 settle 再算回执哈希；发布时再 settle 一次不改文件，哈希对得上。"""
    sid = uuid.uuid4().hex
    tmp = new_trial(sid, wal=True)
    assert tmp.read_bytes()[18:20] == b"\x02\x02"
    unsettled = raw_store.sha256_file(tmp)
    settled = settled_sha(tmp)
    assert settled != unsettled                                # settle 改了文件头
    _, result = await publish_trial(unique(), tmp, raw_bytes(), source_id=sid, expected=settled)
    build = await get(TableBuild, result.build_id)
    assert raw_store.sha256_file(Path(build.db_path)) == settled == result.db_sha256
    assert Path(build.db_path).read_bytes()[18:20] == b"\x01\x01"

    # 反例：没 settle 就算的哈希，发布时对不上
    tmp2 = new_trial(sid, wal=True)
    early = raw_store.sha256_file(tmp2)
    with pytest.raises(table_versions.PublishError) as info:
        await publish_trial(unique(), tmp2, raw_bytes(), expected=early)
    assert info.value.code == "trial_tampered"


async def test_reused_build_reports_the_existing_file_hash(store, monkeypatch):
    """同一个构建 id 再发布：复用已有文件，PublishInfo.db_sha256 是已有文件的，不是这次试运行库的。"""
    # 期 3 起回收另外保留最近 SNAPSHOT_KEEP 个快照；调成 0（期 1、期 2 的规则）才测得出「被替换的那一期随回收置 retired」
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 0)
    name, raw = unique(), raw_bytes()
    sid = uuid.uuid4().hex
    bid = table_versions.recipe_build_id(sid, hashlib.sha256(raw).hexdigest(), RECIPE_SHA, {})
    _, first = await publish_trial(name, new_trial(sid), raw, source_id=sid, build_id=bid)
    # 内容相同、字节不同的试运行库（比如 SQLite 升级后文件头的版本号不同）
    tmp = new_trial(sid, rows=[(2, "分区乙"), (1, "分区甲")])
    tmp_sha = settled_sha(tmp)
    assert tmp_sha != first.db_sha256
    seen: list = []
    _, second = await publish_trial(name, tmp, raw, build_id=bid, expected=tmp_sha, seen=seen)
    [(_view, info)] = seen
    assert second.build_reused is True and info.build_reused is True
    assert info.db_sha256 == first.db_sha256 == second.db_sha256
    assert info.seq == 2 and second.seq == 2
    assert not tmp.exists()
    # 第一次导入随回收置 retired；构建文件仍被第二次导入用着，原件相同也留着
    imps = await imports_of(sid)
    assert [i.status for i in imps] == ["retired", "active"]
    assert [i.raw_state for i in imps] == ["kept", "kept"]
    assert Path((await get(TableBuild, bid)).db_path).exists()


async def test_before_commit_failure_rolls_back_and_removes_new_files(store):
    name = unique()
    sid, first = await publish_trial(name, new_trial(uuid.uuid4().hex), raw_bytes())
    builds = store / "uploads" / "tables" / sid / "builds"
    before = files_under(builds)
    raw = raw_bytes()
    tmp = new_trial(sid)

    def failing(session, source_id):
        async def hook(imp_id, snap_id):
            raise RuntimeError("模拟锁内核对失败")
        return hook

    with pytest.raises(RuntimeError, match="模拟锁内核对失败"):
        await publish_trial(name, tmp, raw, before_commit=failing)
    assert files_under(builds) == before and not tmp.exists()
    assert (await get(DataSource, sid)).current_snapshot_id == first.snapshot_id
    assert [i.status for i in await imports_of(sid)] == ["active"]
    assert not raw_store.raw_exists(hashlib.sha256(raw).hexdigest())
    assert await total(sid) == 3


async def test_before_commit_failure_does_not_create_a_new_source(store):
    sid = uuid.uuid4().hex
    name = unique()

    def failing(session, source_id):
        async def hook(imp_id, snap_id):
            raise RuntimeError("同名源已存在")
        return hook

    with pytest.raises(RuntimeError):
        await publish_trial(name, new_trial(sid), raw_bytes(), source_id=sid, before_commit=failing)
    assert await get(DataSource, sid) is None
    assert not files_under(store / "uploads" / "tables" / sid / "builds")


async def test_before_commit_runs_in_the_transaction_before_the_pointer_moves(store):
    """回调里：select 读到发布前的指针；新源还看不到；导入记录取得到；写的东西和发布一起提交。"""
    name = unique()
    sid, first = await publish_trial(name, new_trial(uuid.uuid4().hex), raw_bytes())
    observed: dict[str, Any] = {}

    def hook_for(session, source_id):
        async def hook(imp_id, snap_id):
            observed["pointer"] = (await session.execute(
                select(DataSource.current_snapshot_id).where(DataSource.id == source_id))).scalar()
            observed["active"] = (await session.execute(
                select(TableImport.id).where(TableImport.source_id == source_id,
                                             TableImport.status == "active"))).scalars().all()
            imp = await session.get(TableImport, imp_id)
            imp.manifest_artifact = "m" * 64
            session.add(TableRecipe(id=uuid.uuid4().hex, source_id=source_id, seq=1, recipe={},
                                    recipe_sha256=RECIPE_SHA, status="active"))
            observed["snap"] = snap_id
        return hook

    _, second = await publish_trial(name, new_trial(sid), raw_bytes(), before_commit=hook_for)
    assert observed["pointer"] == first.snapshot_id            # 指针还没切
    assert observed["active"] == [second.import_id]            # 旧导入已置 superseded
    assert observed["snap"] == second.snapshot_id
    imps = await imports_of(sid)
    assert imps[-1].manifest_artifact == "m" * 64
    async with SessionLocal() as session:
        recipes = (await session.execute(select(TableRecipe).where(TableRecipe.source_id == sid))).scalars().all()
    assert len(recipes) == 1

    # 新源：回调里按名字查不到这次要建的源
    fresh_id, fresh_name = uuid.uuid4().hex, unique()
    found: list = []

    def lookup(session, source_id):
        async def hook(imp_id, snap_id):
            found.extend((await session.execute(
                select(DataSource.id).where(DataSource.name == fresh_name))).scalars().all())
        return hook

    await publish_trial(fresh_name, new_trial(fresh_id), raw_bytes(), source_id=fresh_id, before_commit=lookup)
    assert found == [] and (await get(DataSource, fresh_id)).name == fresh_name


async def test_publish_build_with_raw_none_needs_the_archived_raw(store):
    raw = raw_bytes()
    sid = uuid.uuid4().hex
    tmp = new_trial(sid)
    with pytest.raises(table_versions.PublishError) as info:
        await publish_trial(unique(), tmp, raw, source_id=sid, pass_raw=False)
    assert info.value.code == "raw_missing"
    assert not tmp.exists() and await get(DataSource, sid) is None
    assert not files_under(store / "uploads" / "tables" / sid / "builds")

    sha, _ = raw_store.put_raw(raw)                            # 暂存时存过
    _, result = await publish_trial(unique(), new_trial(sid), raw, source_id=sid, pass_raw=False)
    [imp] = await imports_of(sid)
    assert imp.raw_sha256 == sha and imp.raw_state == "kept" and raw_store.raw_exists(sha)
    assert imp.file_size == len(raw) and imp.file_name == "月报导出.xlsx"


async def test_import_fields_cannot_touch_other_columns(store):
    sid = uuid.uuid4().hex
    tmp = new_trial(sid)
    with pytest.raises(ValueError, match="status"):
        await publish_trial(unique(), tmp, raw_bytes(), source_id=sid, import_fields={"status": "retired"})
    assert not tmp.exists() and await get(DataSource, sid) is None


async def test_publish_build_checks_raw_against_its_hash(store):
    sid = uuid.uuid4().hex
    tmp = new_trial(sid)
    raw = raw_bytes()

    async def run():
        async with SessionLocal() as session:
            row = DataSource(id=sid, name=unique(), kind="sqlite", readonly=True, origin="upload")
            await table_versions.publish_build(
                session, row, tmp_db=tmp, build_id="b" * 64, build_options={}, engine_ver="recipe/1",
                report={}, raw=raw, raw_sha=hashlib.sha256(b"other").hexdigest(), file_name="x.xlsx",
                file_size=len(raw), schema_cache_for=None, import_fields={})

    with pytest.raises(ValueError, match="原件"):
        await run()
    assert not tmp.exists() and await get(DataSource, sid) is None


async def test_publish_upload_goes_through_publish_build(monkeypatch):
    calls: list[dict] = []
    real = table_versions.publish_build

    async def spy(session, source, **kw):
        calls.append(kw)
        return await real(session, source, **kw)

    monkeypatch.setattr(table_versions, "publish_build", spy)
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "甲表"
    book.active.append(["区域", "数量"])
    book.active.append(["分区甲", 1])
    buf = io.BytesIO()
    book.save(buf)
    async with SessionLocal() as session:
        row = DataSource(id=uuid.uuid4().hex, name=unique(), kind="sqlite", readonly=True, origin="upload")
        result = await table_versions.publish_upload(session, row, buf.getvalue(), "客流.xlsx")
    [kw] = calls
    assert kw["engine_ver"] == table_versions.ENGINE_VER and kw["import_fields"] == {}
    assert "expected_db_sha256" not in kw and "before_commit" not in kw
    assert result.seq == 1 and result.db_sha256


# --------------------------------------------------------------------------
# 暂存区：结束即瘦身、写入
# --------------------------------------------------------------------------


async def test_close_staging_slims_and_returns_the_trial_path():
    sid, path = await new_staging(trial=True)
    async with SessionLocal() as session:
        staging = await session.get(ImportStaging, sid)
        with pytest.raises(ValueError):
            table_versions.close_staging(staging, "drafting")
        old = table_versions.close_staging(staging, "discarded")
        await session.commit()
    assert old == str(path) and path.exists()                  # 文件留给提交之后删
    assert_slimmed(await get(ImportStaging, sid), "discarded")
    assert table_versions.remove_trial_file(old) is True and not path.exists()


def test_remove_trial_file_only_touches_trial_dbs(store, tmp_path):
    outside = make_db(tmp_path / "trials" / "x.db")
    assert table_versions.remove_trial_file(str(outside)) is False and outside.exists()
    build = make_db(table_versions.tables_root() / "src" / "builds" / "x.db")
    assert table_versions.remove_trial_file(str(build)) is False and build.exists()
    assert table_versions.remove_trial_file(None) is False


async def test_store_staging_saves_raw_and_row_under_the_lock(store):
    raw = raw_bytes()
    sid, _ = await new_staging(raw=raw)
    staging = await get(ImportStaging, sid)
    assert staging.raw_sha256 == hashlib.sha256(raw).hexdigest() and raw_store.raw_exists(staging.raw_sha256)
    assert staging.file_size == len(raw)
    assert staging.expires_at is not None
    assert abs((staging.expires_at - staging.created_at) - table_versions.STAGING_TTL) < timedelta(minutes=1)


async def test_store_staging_failure_removes_a_new_raw(store):
    raw = raw_bytes()
    dup = uuid.uuid4().hex
    await new_staging(raw=raw_bytes(), id=dup)
    staging = ImportStaging(id=dup, source_id=uuid.uuid4().hex, kind="first")
    async with SessionLocal() as session:
        with pytest.raises(Exception):
            await table_versions.store_staging(session, staging, raw)
    assert not raw_store.raw_exists(hashlib.sha256(raw).hexdigest())


# --------------------------------------------------------------------------
# 回收与清理保护
# --------------------------------------------------------------------------


async def test_open_staging_raw_survives_gc_and_startup(store):
    raw = raw_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    sid, _ = await new_staging(raw=raw, status="rejected")
    await gc()
    assert raw_store.raw_exists(sha)
    await startup()
    assert raw_store.raw_exists(sha)
    assert (await get(ImportStaging, sid)).status == "rejected"


async def test_open_staging_protects_a_raw_shared_with_a_replaced_import(store, monkeypatch):
    """上传新一期被拒收、正在修改时，那份原件和一次旧导入相同：旧导入被回收，原件不能跟着删。"""
    # 同上：SNAPSHOT_KEEP 调成 0，旧导入才会随替换被回收
    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 0)
    name, raw = unique(), raw_bytes()
    src, first = await publish_trial(name, new_trial(uuid.uuid4().hex), raw)
    sha = hashlib.sha256(raw).hexdigest()
    await new_staging(raw=raw, source_id=src, status="rejected")
    await publish_trial(name, new_trial(src), raw_bytes())    # 替换掉第一期，回收一次
    old = (await imports_of(src))[0]
    assert old.status == "retired" and old.raw_state == "kept"
    assert raw_store.raw_exists(sha)


async def test_gc_removes_raws_and_trials_left_by_closed_stagings(store):
    """放弃、删源时没来得及删的原件和试运行库：暂存区已结束、没人要了，回收删掉。"""
    lone = raw_bytes()
    shared = raw_bytes()
    lone_id, lone_trial = await new_staging(raw=lone, trial=True)
    shared_id, _ = await new_staging(raw=shared)
    keeper_id, _ = await new_staging(raw=shared)               # 同一份原件还有一个未结束的暂存区
    async with SessionLocal() as session:
        for sid in (lone_id, shared_id):
            staging = await session.get(ImportStaging, sid)
            staging.status = "discarded"                       # 模拟只改了状态的结束路径
        await session.commit()
    open_id, open_trial = await new_staging(trial=True)
    orphan = new_trial(uuid.uuid4().hex)                        # 暂存区不存在：回收不碰，留给启动清理
    await gc()
    assert not raw_store.raw_exists(hashlib.sha256(lone).hexdigest())
    assert raw_store.raw_exists(hashlib.sha256(shared).hexdigest())
    assert not lone_trial.exists()
    assert open_trial.exists() and orphan.exists()
    await startup()
    assert open_trial.exists() and not orphan.exists()


async def test_recipe_sources_keep_the_raws_of_their_last_12_imports(store):
    recipe_src, plain_src = uuid.uuid4().hex, uuid.uuid4().hex
    shas: dict[str, list[str]] = {recipe_src: [], plain_src: []}
    async with SessionLocal() as session:
        session.add(DataSource(id=recipe_src, name=unique(), kind="sqlite", readonly=True,
                               origin="upload", current_recipe_id="r" * 32))
        session.add(DataSource(id=plain_src, name=unique(), kind="sqlite", readonly=True, origin="upload"))
        for src in (recipe_src, plain_src):
            for seq in range(1, 15):
                sha, _ = raw_store.put_raw(raw_bytes())
                shas[src].append(sha)
                session.add(TableImport(source_id=src, seq=seq, raw_sha256=sha, raw_state="kept",
                                        status="active" if seq == 14 else "superseded"))
        await session.commit()
    await gc()
    recipe = await imports_of(recipe_src)
    assert [i.raw_state for i in recipe] == ["purged"] * 2 + ["kept"] * 12
    assert [raw_store.raw_exists(s) for s in shas[recipe_src]] == [False] * 2 + [True] * 12
    assert {i.status for i in recipe[:13]} == {"retired"} and recipe[13].status == "active"
    plain = await imports_of(plain_src)
    assert [i.raw_state for i in plain] == ["purged"] * 13 + ["kept"]
    assert [raw_store.raw_exists(s) for s in shas[plain_src]] == [False] * 13 + [True]
    # 原件留着的导入，启动清理也不当孤儿删
    await startup()
    assert [raw_store.raw_exists(s) for s in shas[recipe_src]] == [False] * 2 + [True] * 12
    assert table_versions.RECIPE_RAW_KEEP == 12


async def test_startup_sweeps_stale_trial_dbs(store):
    current_id, current = await new_staging(trial=True)
    # 同一个暂存区的旧试运行库（trial_path 已经指向新的）
    stale_same = new_trial(
        (await get(ImportStaging, current_id)).source_id, current_id)
    closed_id, _ = await new_staging()
    async with SessionLocal() as session:
        staging = await session.get(ImportStaging, closed_id)
        staging.status = "committed"
        await session.commit()
    closed_trial = new_trial(uuid.uuid4().hex, closed_id)
    missing = new_trial(uuid.uuid4().hex)
    Path(f"{missing}-journal").write_bytes(b"")
    await startup()
    assert current.exists()
    assert not stale_same.exists() and not closed_trial.exists() and not missing.exists()
    assert not Path(f"{missing}-journal").exists()
    assert not missing.parent.exists()                         # 空了的 trials/ 和源目录一并删


async def test_trial_dbs_of_dashed_staging_ids_are_matched_to_their_staging(store):
    """暂存区 id 带「-」时，回收和启动清理按文件名找回的必须是它本身，不是「-」前面那一截。

    切错了：开着的暂存区的库被启动清理当孤儿删掉，已结束的暂存区的库回收又认不出来。
    """
    tag = uuid.uuid4().hex[:8]
    open_id, open_trial = await new_staging(trial=True, id=f"stg-{tag}-open")
    closed_id, closed_trial = await new_staging(trial=True, id=f"stg-{tag}-closed")
    async with SessionLocal() as session:
        staging = await session.get(ImportStaging, closed_id)
        staging.status = "discarded"                           # 只改状态，库留给回收删
        await session.commit()
    await gc()
    assert open_trial.exists() and not closed_trial.exists()
    await startup()
    assert open_trial.exists()
    assert (await get(ImportStaging, open_id)).trial_path == str(open_trial)


async def test_expire_stagings_slims_deletes_trials_and_releases_raws(store):
    past = utcnow() - timedelta(seconds=1)
    lone, shared = raw_bytes(), raw_bytes()
    src, _ = await publish_trial(unique(), new_trial(uuid.uuid4().hex), shared)
    expired_id, trial = await new_staging(raw=lone, trial=True, expires_at=past)
    shared_id, _ = await new_staging(raw=shared, source_id=src, status="trialed", expires_at=past)
    fresh_id, fresh_trial = await new_staging(raw=raw_bytes(), trial=True)
    # 没写 expires_at 的：按 created_at 加 TTL 算
    legacy_id, _ = await new_staging(created_at=utcnow() - table_versions.STAGING_TTL - timedelta(hours=1))
    async with SessionLocal() as session:
        expired = await table_versions.expire_stagings(session)
    assert set(expired) >= {expired_id, shared_id, legacy_id} and fresh_id not in expired
    for sid in (expired_id, shared_id, legacy_id):
        assert_slimmed(await get(ImportStaging, sid), "expired")
    assert not trial.exists() and fresh_trial.exists()
    assert not raw_store.raw_exists(hashlib.sha256(lone).hexdigest())
    assert raw_store.raw_exists(hashlib.sha256(shared).hexdigest())   # 导入记录还留着它
    assert (await get(ImportStaging, fresh_id)).status == "drafting"
    async with SessionLocal() as session:
        assert await table_versions.expire_stagings(session) == []    # 没有要处理的就不等锁


async def test_closed_stagings_older_than_90_days_are_deleted(store):
    old_id, _ = await new_staging(status="discarded",
                                  closed_at=utcnow() - table_versions.STAGING_KEEP_CLOSED - timedelta(days=1))
    recent_id, _ = await new_staging(status="committed", closed_at=utcnow() - timedelta(days=30))
    open_id, _ = await new_staging(created_at=utcnow() - timedelta(days=200),
                                   expires_at=utcnow() + timedelta(days=1))
    # 没写 closed_at 的（某条结束路径只改了状态）：按 updated_at 算，不会永远留着
    unstamped_old, _ = await new_staging(
        status="expired", updated_at=utcnow() - table_versions.STAGING_KEEP_CLOSED - timedelta(days=1))
    unstamped_recent, _ = await new_staging(status="committed", updated_at=utcnow() - timedelta(days=30))
    assert (await get(ImportStaging, unstamped_old)).closed_at is None
    await gc()                                                 # 回收开头顺手处理
    assert await get(ImportStaging, old_id) is None
    assert await get(ImportStaging, recent_id) is not None
    assert (await get(ImportStaging, open_id)).status == "drafting"
    assert await get(ImportStaging, unstamped_old) is None
    assert await get(ImportStaging, unstamped_recent) is not None


async def test_gc_expires_stagings_first(store):
    sid, trial = await new_staging(raw=raw_bytes(), trial=True, expires_at=utcnow() - timedelta(minutes=1))
    await gc()
    assert_slimmed(await get(ImportStaging, sid), "expired")
    assert not trial.exists()


# --------------------------------------------------------------------------
# 清除原件、放弃导入、删除数据源
# --------------------------------------------------------------------------


async def test_purging_a_raw_discards_open_stagings_that_use_it(store):
    raw = raw_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    src, _ = await publish_trial(unique(), new_trial(uuid.uuid4().hex), raw)
    hit_id, hit_trial = await new_staging(raw=raw, source_id=src, status="trialed", trial=True)
    closed_id, _ = await new_staging(raw=raw, status="committed")
    other_raw = raw_bytes()
    other_id, other_trial = await new_staging(raw=other_raw, trial=True)
    [imp] = await imports_of(src)
    async with SessionLocal() as session:
        imp = await session.get(TableImport, imp.id)
        result = await table_versions.purge_import_raw(session, imp, reason="合成理由", signed_by="测试员")
    deleted, others = result                                   # 期 1 的两元组解包照旧
    assert deleted is True and others == []
    assert [s.id for s in result.discarded_stagings] == [hit_id]
    assert_slimmed(await get(ImportStaging, hit_id), "discarded")
    assert not hit_trial.exists() and not raw_store.raw_exists(sha)
    assert (await get(ImportStaging, closed_id)).status == "committed"
    assert (await get(ImportStaging, other_id)).status == "drafting" and other_trial.exists()
    assert raw_store.raw_exists(hashlib.sha256(other_raw).hexdigest())


async def test_release_staging_raw_only_deletes_unreferenced_raws(store):
    raw = raw_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    a_id, _ = await new_staging(raw=raw)
    b_id, _ = await new_staging(raw=raw)

    async def discard(sid: str) -> bool:
        async with SessionLocal() as session:
            staging = await session.get(ImportStaging, sid)
            table_versions.remove_trial_file(table_versions.close_staging(staging, "discarded"))
            await session.commit()
            return await table_versions.release_staging_raw(session, staging)

    async with SessionLocal() as session:
        still_open = await session.get(ImportStaging, a_id)
        assert await table_versions.release_staging_raw(session, still_open) is False
    assert await discard(a_id) is False and raw_store.raw_exists(sha)     # b 还开着
    assert await discard(b_id) is True and not raw_store.raw_exists(sha)

    # 有留着原件的导入记录时也不删
    kept = raw_bytes()
    await publish_trial(unique(), new_trial(uuid.uuid4().hex), kept)
    c_id, _ = await new_staging(raw=kept)
    assert await discard(c_id) is False and raw_store.raw_exists(hashlib.sha256(kept).hexdigest())


async def test_release_staging_raw_keeps_the_raw_of_a_staging_still_open(store):
    """暂存区还开着就不删原件，哪怕它是这份原件唯一的引用：删了它之后的试运行、提交就拿不到原件。"""
    raw = raw_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    sid, _ = await new_staging(raw=raw, status="trialed")
    async with SessionLocal() as session:
        staging = await session.get(ImportStaging, sid)
        assert await table_versions.release_staging_raw(session, staging) is False
    assert raw_store.raw_exists(sha)
    assert (await get(ImportStaging, sid)).status == "trialed"


async def test_retire_source_retires_recipes_and_discards_open_stagings(store):
    name, raw = unique(), raw_bytes()
    src, _ = await publish_trial(name, new_trial(uuid.uuid4().hex), raw_bytes())
    staging_raw = raw_bytes()
    stg_id, trial = await new_staging(raw=staging_raw, source_id=src, status="rejected", trial=True)
    async with SessionLocal() as session:
        session.add(TableRecipe(source_id=src, seq=1, recipe={}, recipe_sha256=RECIPE_SHA, status="active"))
        await session.commit()
        await table_versions.retire_source(session, src)
        await session.delete(await session.get(DataSource, src))
        await session.commit()
        await table_versions.gc(session)
    async with SessionLocal() as session:
        recipes = (await session.execute(select(TableRecipe).where(TableRecipe.source_id == src))).scalars().all()
    assert {r.status for r in recipes} == {"retired"}
    assert_slimmed(await get(ImportStaging, stg_id), "discarded")
    assert not trial.exists()
    assert not raw_store.raw_exists(hashlib.sha256(staging_raw).hexdigest())
    assert {i.status for i in await imports_of(src)} == {"retired"}


async def test_concurrent_publish_builds_are_serialized(store):
    """同一个源同时来两次发布：序号不重、最后只有一条 active。"""
    name = unique()
    src, _ = await publish_trial(name, new_trial(uuid.uuid4().hex), raw_bytes())
    results = await asyncio.gather(
        publish_trial(name, new_trial(src), raw_bytes()), publish_trial(name, new_trial(src), raw_bytes()))
    imps = await imports_of(src)
    assert [i.seq for i in imps] == [1, 2, 3]
    assert [i.status for i in imps].count("active") == 1
    assert (await get(DataSource, src)).current_snapshot_id in {r.snapshot_id for _, r in results}
