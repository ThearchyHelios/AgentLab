"""连接缓存按「数据源 + 版本快照」分键；版本快照以 immutable 只读打开，建连接前核对哈希。

上传表格每次发布都是一个新文件，同一个数据源的新旧版本得各用各的连接池，否则钉住
旧版本的运行会查到新文件。快照用一个带 snapshot_id / immutable / expected_sha256 的
简单对象模拟（table_versions.SourceView 的形状），不依赖版本底座的实现。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.data import engine as engine_mod
from app.data.engine import (
    MAX_ENGINES, EngineCache, SnapshotTampered, cache_key, engine_args, engines, run_query,
)


def _db(path, rows=(("分区甲", 120),)) -> str:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE 时段客流 (分区 TEXT, 客流 INTEGER)")
    conn.executemany("INSERT INTO 时段客流 VALUES (?, ?)", list(rows))
    conn.commit()
    conn.close()
    return str(path)


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _source(path: str, *, sid: str | None = None, readonly: bool = True, **extra):
    """手工源的形状；extra 里给 snapshot_id / immutable / expected_sha256 就是快照视图的形状。"""
    return SimpleNamespace(
        id=sid or f"src-{uuid.uuid4().hex[:8]}", kind="sqlite", database=path, readonly=readonly,
        name="snapshot_probe", options={}, password=None, username=None, host=None, port=None,
        description=None, schema_cache=None, enabled=True, **extra,
    )


#: _snapshot 的 expected 不传时：文件在就按它现在的内容登记哈希（版本快照一定带哈希，见 S2-7 的用例）
_AUTO = object()


def _snapshot(path: str, *, sid: str, snap: str, expected: object = _AUTO, readonly: bool = True):
    if expected is _AUTO:
        expected = _sha(path) if os.path.isfile(path) else None
    return _source(path, sid=sid, readonly=readonly, snapshot_id=snap, immutable=True,
                   expected_sha256=expected)


async def _total(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT SUM(客流) FROM 时段客流"))).scalar_one()


@pytest.fixture
async def cache():
    c = EngineCache()
    yield c
    await c.close()


@pytest.fixture
def disposed(monkeypatch):
    """记下哪些 engine 被 dispose 过。"""
    seen: list[int] = []
    original = AsyncEngine.dispose

    async def spy(self, close: bool = True):
        seen.append(id(self))
        return await original(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)
    return seen


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------

def test_cache_key_carries_the_snapshot():
    assert cache_key(SimpleNamespace(id="s1")) == "s1:"
    assert cache_key(SimpleNamespace(id="s1", snapshot_id=None)) == "s1:"
    assert cache_key(SimpleNamespace(id="s1", snapshot_id="")) == "s1:"
    assert cache_key(SimpleNamespace(id="s1", snapshot_id="ab12")) == "s1:ab12"


async def test_two_snapshots_of_one_source_get_separate_engines(tmp_path, cache):
    """同一个数据源的两个版本：两个 engine，各查各的文件。"""
    old = _db(tmp_path / "v1.db", [("分区甲", 100)])
    new = _db(tmp_path / "v2.db", [("分区甲", 100), ("分区乙", 50)])
    v1 = _snapshot(old, sid="s1", snap="a" * 64)
    v2 = _snapshot(new, sid="s1", snap="b" * 64)

    e1, e2 = await cache.get(v1), await cache.get(v2)
    assert e1 is not e2
    assert (await _total(e1), await _total(e2)) == (100, 150)
    # 再取还是同一个
    assert await cache.get(v1) is e1
    assert set(cache._engines) == {f"s1:{'a' * 64}", f"s1:{'b' * 64}"}


async def test_invalidate_clears_every_version_of_that_source_only(tmp_path, cache, disposed):
    path = _db(tmp_path / "x.db")
    plain = _source(path, sid="s1")
    v1 = _snapshot(path, sid="s1", snap="a" * 64)
    v2 = _snapshot(path, sid="s1", snap="b" * 64)
    neighbour = _source(path, sid="s10")              # 前缀相同的另一个源
    neighbour_v = _snapshot(path, sid="s10", snap="a" * 64)

    doomed = [await cache.get(s) for s in (plain, v1, v2)]
    kept = [await cache.get(s) for s in (neighbour, neighbour_v)]

    await cache.invalidate("s1")
    assert set(cache._engines) == {"s10:", f"s10:{'a' * 64}"}
    assert sorted(disposed) == sorted(id(e) for e in doomed)
    assert [await cache.get(s) for s in (neighbour, neighbour_v)] == kept
    # 失效之后再取，新建一个
    assert await cache.get(v1) is not doomed[1]


async def test_lru_evicts_the_least_recently_used(tmp_path, disposed):
    cache = EngineCache(max_engines=3)
    try:
        path = _db(tmp_path / "x.db")
        a, b, c, d = (_source(path, sid=n) for n in "abcd")
        ea, eb, ec = await cache.get(a), await cache.get(b), await cache.get(c)
        assert await cache.get(a) is ea          # a 刚用过，最久没用的是 b
        ed = await cache.get(d)
        assert list(cache._engines) == ["c:", "a:", "d:"]
        assert disposed == [id(eb)]
        assert await _total(ea) == 120 and await _total(ed) == 120
        # b 被关掉了，再取是新建的
        assert await cache.get(b) is not eb
        assert len(cache._engines) == 3
    finally:
        await cache.close()


async def test_default_limit_follows_the_module_constant(tmp_path, monkeypatch):
    assert MAX_ENGINES == 32
    monkeypatch.setattr(engine_mod, "MAX_ENGINES", 2)
    cache = EngineCache()
    try:
        path = _db(tmp_path / "x.db")
        for n in "abc":
            await cache.get(_source(path, sid=n))
        assert list(cache._engines) == ["b:", "c:"]
    finally:
        await cache.close()


async def test_global_cache_keeps_versions_apart_for_run_query(tmp_path):
    """经 run_query（全局缓存）查：钉住旧版本的查询拿到旧数，当前版本拿到新数。"""
    old = _db(tmp_path / "v1.db", [("分区甲", 100)])
    new = _db(tmp_path / "v2.db", [("分区甲", 100), ("分区乙", 50)])
    sid = f"g-{uuid.uuid4().hex[:8]}"
    v1 = _snapshot(old, sid=sid, snap="1" * 64, expected=_sha(old))
    v2 = _snapshot(new, sid=sid, snap="2" * 64, expected=_sha(new))
    try:
        assert (await run_query(v1, "SELECT SUM(客流) FROM 时段客流")).rows == [[100]]
        assert (await run_query(v2, "SELECT SUM(客流) FROM 时段客流")).rows == [[150]]
        assert (await run_query(v1, "SELECT SUM(客流) FROM 时段客流")).rows == [[100]]
    finally:
        await engines.invalidate(sid)
    assert not any(k.startswith(f"{sid}:") for k in engines._engines)


# ---------------------------------------------------------------------------
# 不可变快照的打开方式
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("readonly", [True, False], ids=["readonly=True", "readonly=False"])
def test_immutable_snapshot_url_ignores_readonly_field(tmp_path, readonly):
    path = str(tmp_path / "分区 快照#1.db")
    url, extra = engine_args(_snapshot(path, sid="s1", snap="a" * 64, readonly=readonly))
    assert url == f"sqlite+aiosqlite:///file:{quote(path)}?mode=ro&immutable=1&uri=true"
    assert extra["connect_args"]["factory"] is engine_mod._DqsOffConnection


def test_immutable_snapshot_needs_a_file():
    with pytest.raises(ValueError, match="数据文件"):
        engine_args(_snapshot("", sid="s1", snap="a" * 64))
    with pytest.raises(ValueError, match="数据文件"):
        engine_args(_snapshot(":memory:", sid="s1", snap="a" * 64))


async def test_immutable_snapshot_reads_and_refuses_writes(tmp_path, cache):
    """immutable 打开：查询照常，写由 SQLite 拒绝（标着可写也一样），双引号列名写错照样报错。"""
    path = _db(tmp_path / "分区 快照#1.db")
    engine = await cache.get(_snapshot(path, sid="s1", snap="a" * 64, readonly=False))
    assert await _total(engine) == 120
    async with engine.connect() as conn:
        with pytest.raises(OperationalError, match="readonly"):
            await conn.execute(text("DELETE FROM 时段客流"))
        with pytest.raises(OperationalError, match="no such column"):
            await conn.execute(text('SELECT SUM("客六") FROM 时段客流'))
    assert await _total(engine) == 120


async def test_immutable_snapshot_ignores_locks(tmp_path, cache):
    """immutable=1 的效果：不加锁、不看别的连接的锁。普通只读连接在排它锁下读不了，快照连接能读。

    这条钉住 URL 里的 immutable=1 确实传到了 SQLite，而不只是写在字符串里。
    """
    path = _db(tmp_path / "locked.db")
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        plain = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                plain.execute("SELECT COUNT(*) FROM 时段客流").fetchall()
        finally:
            plain.close()
        engine = await cache.get(_snapshot(path, sid="s1", snap="a" * 64))
        assert await _total(engine) == 120
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ---------------------------------------------------------------------------
# 哈希核对
# ---------------------------------------------------------------------------

async def test_matching_hash_opens(tmp_path, cache):
    path = _db(tmp_path / "ok.db")
    engine = await cache.get(_snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path).upper()))
    assert await _total(engine) == 120


async def test_hash_mismatch_is_refused_and_not_cached(tmp_path, cache):
    path = _db(tmp_path / "tampered.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected="0" * 64)
    with pytest.raises(SnapshotTampered) as info:
        await cache.get(view)
    assert str(info.value) == "数据文件与登记的版本不一致，可能被修改过，已拒绝查询"
    assert isinstance(info.value, ValueError)
    assert cache._engines == {}
    # 没缓存：每次都核对、每次都拒绝
    with pytest.raises(SnapshotTampered):
        await cache.get(view)
    assert cache._engines == {}


async def test_missing_snapshot_file_is_refused(tmp_path, cache):
    view = _snapshot(str(tmp_path / "gone.db"), sid="s1", snap="a" * 64, expected="0" * 64)
    with pytest.raises(SnapshotTampered, match="无法读取"):
        await cache.get(view)
    assert cache._engines == {}


async def test_run_query_surfaces_the_refusal(tmp_path):
    path = _db(tmp_path / "tampered.db")
    sid = f"g-{uuid.uuid4().hex[:8]}"
    view = _snapshot(path, sid=sid, snap="a" * 64, expected="0" * 64)
    with pytest.raises(SnapshotTampered, match="已拒绝查询"):
        await run_query(view, "SELECT COUNT(*) FROM 时段客流")
    assert not any(k.startswith(f"{sid}:") for k in engines._engines)


async def test_hash_is_checked_before_each_new_engine(tmp_path, cache):
    """核对发生在这个键新建 engine 之前；失效（或被 LRU 关掉）之后重建，会重新核对。"""
    path = _db(tmp_path / "snap.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path))
    engine = await cache.get(view)
    assert await cache.get(view) is engine
    await cache.invalidate("s1")
    assert await cache.get(view) is not engine


def _replace(path: str, rows) -> None:
    """另造一个库，整份 rename 覆盖到 path 上（恢复备份、同步工具都是这么换文件的）。0444 挡不住。"""
    other = _db(f"{path}.new", rows)
    os.chmod(path, 0o444)
    os.replace(other, path)


async def test_in_place_rewrite_after_caching_is_refused(tmp_path, cache):
    """引擎缓存着、文件被原地改写：缓存命中时指纹对不上，摘掉引擎、重新核对哈希，拒绝。"""
    path = _db(tmp_path / "snap.db")
    view = _snapshot(path, sid="s1", snap="a" * 64)
    await cache.get(view)

    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO 时段客流 VALUES ('分区乙', 1)")
    conn.commit()
    conn.close()

    with pytest.raises(SnapshotTampered, match="可能被修改过"):
        await cache.get(view)
    assert cache._engines == {} and cache._pins == {}


async def test_replaced_file_is_refused_for_new_pool_connections(tmp_path, cache):
    """核对通过、引擎进了缓存之后文件被整份替换：已开的连接还读着核对过的那份（旧 inode），
    逼连接池新开的连接、以及再次取引擎，都必须被拒，不能静默读到替换后的文件。"""
    path = _db(tmp_path / "snap.db", [("分区甲", 1)])
    view = _snapshot(path, sid="s1", snap="a" * 64)
    engine = await cache.get(view)
    async with engine.connect() as held:
        assert (await held.execute(text("SELECT SUM(客流) FROM 时段客流"))).scalar_one() == 1
        _replace(path, [("分区甲", 424242)])
        # 占着一条，连接池只能新开一条：按路径打开的是替换后的文件，必须被拒
        with pytest.raises(SnapshotTampered, match="替换或改动"):
            async with engine.connect() as fresh:
                await fresh.execute(text("SELECT SUM(客流) FROM 时段客流"))
        assert (await held.execute(text("SELECT SUM(客流) FROM 时段客流"))).scalar_one() == 1
    # 还回来的旧连接再借出去也不行：这个引擎已经不可信
    with pytest.raises(SnapshotTampered):
        async with engine.connect() as again:
            await again.execute(text("SELECT 1"))
    # 再取引擎：摘掉旧的、重新核对哈希，内容变了就拒绝
    with pytest.raises(SnapshotTampered, match="可能被修改过"):
        await cache.get(view)
    assert cache._engines == {}


async def test_replacement_between_get_and_connect_is_refused(tmp_path, cache):
    """取到引擎之后、第一次连接之前文件被换掉（核对完到建出连接之间的窗口）：第一条连接就被拒。"""
    path = _db(tmp_path / "snap.db", [("分区甲", 1)])
    view = _snapshot(path, sid="s1", snap="a" * 64)
    engine = await cache.get(view)
    _replace(path, [("分区甲", 424242)])
    with pytest.raises(SnapshotTampered):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT SUM(客流) FROM 时段客流"))


async def test_same_content_restored_under_a_new_inode_is_accepted(tmp_path, cache):
    """内容没变、只是换了 inode（从备份恢复回同一个文件）：重新核对哈希通过，用新指纹重建，照常查询。"""
    path = _db(tmp_path / "snap.db", [("分区甲", 7)])
    view = _snapshot(path, sid="s1", snap="a" * 64)
    engine = await cache.get(view)
    shutil.copy2(path, f"{path}.bak")
    os.replace(f"{path}.bak", path)
    fresh = await cache.get(view)
    assert fresh is not engine
    assert await _total(fresh) == 7
    assert await cache.get(view) is fresh


async def test_run_query_refuses_a_replaced_snapshot(tmp_path):
    """经 run_query（全局缓存）：第一次查询通过，文件被替换后再查被拒，不会读到替换后的数。"""
    path = _db(tmp_path / "snap.db", [("分区甲", 1)])
    sid = f"g-{uuid.uuid4().hex[:8]}"
    view = _snapshot(path, sid=sid, snap="a" * 64)
    try:
        assert (await run_query(view, "SELECT SUM(客流) FROM 时段客流")).rows == [[1]]
        _replace(path, [("分区甲", 424242)])
        with pytest.raises(SnapshotTampered):
            await run_query(view, "SELECT SUM(客流) FROM 时段客流")
    finally:
        await engines.invalidate(sid)


async def test_hash_is_computed_off_the_event_loop(tmp_path, cache, monkeypatch):
    """大文件哈希要几秒，不能在事件循环线程里算。"""
    path = _db(tmp_path / "snap.db")
    threads: list[int] = []
    original = engine_mod._file_sha256

    def recording(p: str) -> str:
        threads.append(threading.get_ident())
        return original(p)

    monkeypatch.setattr(engine_mod, "_file_sha256", recording)
    await cache.get(_snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path)))
    assert threads and threads[0] != threading.get_ident()


async def test_sources_without_expected_hash_are_not_hashed(tmp_path, cache, monkeypatch):
    """手工源：不读整个文件。"""
    path = _db(tmp_path / "plain.db")

    def boom(p: str) -> str:
        raise AssertionError("不该计算哈希")

    monkeypatch.setattr(engine_mod, "_file_sha256", boom)
    await cache.get(_source(path, sid="s1"))
    assert len(cache._engines) == 1


@pytest.mark.parametrize("expected", [None, "", "   "], ids=["None", "empty", "blank"])
async def test_snapshot_without_a_registered_hash_is_refused(tmp_path, cache, monkeypatch, expected):
    """版本快照（immutable）却没给期望哈希：拒绝，不退回「不核对照样打开」。哪条新路径漏传哈希都会被测出来。"""
    path = _db(tmp_path / "plain.db")

    def boom(p: str) -> str:
        raise AssertionError("不该计算哈希")

    monkeypatch.setattr(engine_mod, "_file_sha256", boom)
    with pytest.raises(SnapshotTampered, match="缺少文件哈希"):
        await cache.get(_snapshot(path, sid="s1", snap="a" * 64, expected=expected))
    assert cache._engines == {}


@pytest.fixture
def hashing(monkeypatch):
    """数一数算了几次哈希；gate 关着时哈希卡在线程里，用来摆出「核对进行中」的局面。"""
    state = SimpleNamespace(calls=[], gate=threading.Event(), started=threading.Event())
    state.gate.set()
    original = engine_mod._file_sha256

    def counting(p: str) -> str:
        state.calls.append(p)
        state.started.set()
        assert state.gate.wait(timeout=10), "测试没有放行哈希"
        return original(p)

    monkeypatch.setattr(engine_mod, "_file_sha256", counting)
    yield state
    state.gate.set()   # 测试中途失败也别让线程一直卡着


async def _until(flag: threading.Event) -> None:
    for _ in range(500):
        if flag.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到哈希开始")


async def test_concurrent_first_use_builds_one_engine(tmp_path, cache, hashing):
    """一次运行开头的几次工具调用同时查同一个快照：只建一个 engine，也只读一遍文件。"""
    path = _db(tmp_path / "snap.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path))
    got = await asyncio.gather(*(cache.get(view) for _ in range(5)))
    assert len({id(e) for e in got}) == 1
    assert len(cache._engines) == 1
    assert len(hashing.calls) == 1
    assert cache._verifying == {}


async def test_concurrent_mismatch_is_shared_but_not_kept(tmp_path, cache, hashing):
    """并发的几个请求共用一次失败的核对，都被拒；失败不留，下一次照常重新核对。"""
    path = _db(tmp_path / "tampered.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected="0" * 64)
    got = await asyncio.gather(*(cache.get(view) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(e, SnapshotTampered) for e in got)
    assert len(hashing.calls) == 1
    assert cache._verifying == {} and cache._engines == {}

    with pytest.raises(SnapshotTampered):
        await cache.get(view)
    assert len(hashing.calls) == 2


async def test_a_cancelled_request_does_not_cancel_the_shared_check(tmp_path, cache, hashing):
    path = _db(tmp_path / "snap.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path))
    hashing.gate.clear()
    first = asyncio.create_task(cache.get(view))
    await _until(hashing.started)
    second = asyncio.create_task(cache.get(view))
    await asyncio.sleep(0)
    first.cancel()
    hashing.gate.set()
    engine = await second
    assert first.cancelled()
    assert await _total(engine) == 120
    assert len(hashing.calls) == 1


async def test_different_registered_hash_is_checked_separately(tmp_path, cache, hashing):
    """同一个键却登记了不同的哈希（不该出现）：各核各的，错的那个不能借对的那个的结论放行。"""
    path = _db(tmp_path / "snap.db")
    good = _snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path))
    bad = _snapshot(path, sid="s1", snap="a" * 64, expected="0" * 64)
    hashing.gate.clear()
    tasks = [asyncio.create_task(cache.get(v)) for v in (good, bad)]
    await _until(hashing.started)
    hashing.gate.set()
    ok, refused = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(ok, AsyncEngine)
    assert isinstance(refused, SnapshotTampered)
    assert len(hashing.calls) == 2


async def test_requests_after_invalidate_check_again(tmp_path, cache, hashing):
    """核对进行中数据源被失效：之后来的请求重新核对，不搭失效之前那一次的便车。"""
    path = _db(tmp_path / "snap.db")
    view = _snapshot(path, sid="s1", snap="a" * 64, expected=_sha(path))
    hashing.gate.clear()
    before = asyncio.create_task(cache.get(view))
    await _until(hashing.started)
    await cache.invalidate("s1")
    assert cache._verifying == {}
    after = asyncio.create_task(cache.get(view))
    await asyncio.sleep(0.05)
    hashing.gate.set()
    await asyncio.gather(before, after)
    assert len(hashing.calls) == 2
