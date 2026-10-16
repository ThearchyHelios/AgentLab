"""对象索引、探查范围、后台探查任务（docs/design/next-round/03-数据接入与目录维护.md A1）。

以前探查结构每个源最多取 200 个对象、按数据库列出的顺序截断，同步请求、整份替换、不排队。这组守的是：

- 对象索引列全库的对象（3,000 张表的合成库：3,000 个），dry_run 只刷新索引、数命中，不反射、不动表结构；
- 缺省范围（全部、500 张）碰到 3,000 张表拒绝开始并说出命中数，不悄悄截断，也不建任务；
- 规则（通配、按 name_key 不分大小写、排除优先、显式列出）圈出 400 张，后台任务逐表读完，schema_cache 形状不变、
  total 是对象总数、truncated 是「有范围外的」，每张表有结构指纹和版本；
- 停止：停在当前一批之后，读完的照样物化进表结构；服务重启：标中断、不续跑；
- 和同源的剖析、起草排同一条队：排在剖析后面时 waiting_for 是 profile，探查进行中同步起草 409 introspect_busy；
- 真删除记 removed_at（目录清单说「表已不在」），范围外、读不到结构分开说；改了结构版本加 1，没改不变；
- 字典查询不认时退回 Inspector；对象索引被截断时不判删除。

合成库在临时目录里建，用完连同数据源一起删掉。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core import job_runtime as runtime
from app.data import introspect_jobs, source_objects as so
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.db.models_sources import IntrospectJob, IntrospectTask, SourceObject
from app.main import app
from tests.fixtures.catalog import large
from tests.fixtures.sources import drop_source, introspect_now


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(scope="module")
def big_db(tmp_path_factory) -> str:
    return str(large.build(tmp_path_factory.mktemp("large") / "large.db"))


async def _make(client, path: str) -> str:
    r = await client.post("/api/datasources", json={"name": f"big_{uuid.uuid4().hex[:8]}", "kind": "sqlite",
                                                    "database": path, "readonly": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def big(client, big_db):
    sid = await _make(client, big_db)
    yield sid
    await drop_source(sid)


@pytest.fixture
async def small(client, tmp_path):
    path = tmp_path / "small.db"
    db = sqlite3.connect(path)
    for name in ("alpha", "beta", "gamma", "Delta_Orders", "eta"):
        db.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, amount INTEGER)')
    db.execute("CREATE VIEW v_alpha AS SELECT * FROM alpha")
    db.commit()
    db.close()
    sid = await _make(client, str(path))
    yield sid, path
    await drop_source(sid)


async def _scope(client, sid: str, **rules) -> dict:
    r = await client.put(f"/api/datasources/{sid}/introspect-scope", json={"scope": rules})
    assert r.status_code == 200, r.text
    return r.json()


async def _rows(sid: str) -> dict[str, SourceObject]:
    async with SessionLocal() as session:
        return {r.name: r for r in (await session.execute(
            select(SourceObject).where(SourceObject.source_id == sid))).scalars()}


async def _cache(sid: str) -> dict:
    async with SessionLocal() as session:
        return dict((await session.get(DataSource, sid)).schema_cache or {})


# --------------------------------------------------------------------------
# 3,000 张表：对象索引、缺省范围超上限、规则圈 400 张
# --------------------------------------------------------------------------


async def test_index_lists_every_object_and_the_default_scope_refuses_without_truncating(client, big):
    r = await client.post(f"/api/datasources/{big}/introspect", params={"dry_run": "true"})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["table_count"] == large.TOTAL == 3000 and preview["index_method"] == "dictionary"
    assert preview["stored"] is True and len(preview["tables"]) == 200 and preview["truncated"] is True
    assert preview["scope"]["hits"] == 3000 and preview["scope"]["cap"] == so.SCOPE_DEFAULT
    assert preview["scope"]["over"] is True
    assert await _cache(big) == {}, "dry_run 只刷新对象索引，不碰表结构缓存"

    r = await client.post(f"/api/datasources/{big}/introspect")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "scope_too_large" and body["hits"] == 3000 and body["cap"] == 500
    assert "3,000" in body["detail"] and "500" in body["detail"], "说出命中了多少张、上限多少"
    assert (await client.get(f"/api/datasources/{big}/introspect-jobs/latest")).json()["job"] is None, \
        "超上限不建任务"
    rows = await _rows(big)
    assert len(rows) == 3000 and not any(r.in_scope for r in rows.values()), "不悄悄截断成前 500 张"


async def test_rules_pick_400_tables_and_the_background_job_reflects_them(client, big):
    explicit = [t.name for t in large.TARGETS]
    await _scope(client, big, mode="rules", include=["ORD_*", "sal_*", "inv_*"], exclude=["*_tmp_*", "*_bak_*"],
                 tables=explicit)
    r = await client.post(f"/api/datasources/{big}/introspect-scope/preview",
                          json={"scope": {"mode": "rules", "include": ["ord_*"]}})
    assert r.status_code == 200 and r.json()["preview"] is None, "还没有对象索引：命中数要先刷新索引才数得出"
    r = await client.post(f"/api/datasources/{big}/introspect")
    assert r.status_code == 202, r.text
    started = r.json()["job"]
    assert started["status"] == "queued" and started["total"] == 400
    assert started["scope"]["rule"] == 390 and started["scope"]["explicit"] == 10
    await introspect_jobs.manager.wait(started["id"], timeout=180)
    body = (await client.get(f"/api/datasources/{big}/introspect-jobs/{started['id']}")).json()
    job, source = body["job"], body["source"]
    assert job["status"] == "succeeded" and job["counts"]["ok"] == 400 and job["done"] == 400
    assert source["table_count"] == 400 and source["schema_scope"]["out_of_scope"] == 2600
    assert source["introspect_job"]["status"] == "succeeded"

    cache = await _cache(big)
    assert set(cache) >= {"tables", "truncated", "total", "schema", "synced_at", "scope"}, "形状不变，另加 scope"
    assert cache["total"] == 3000 and cache["truncated"] is True and len(cache["tables"]) == 400
    assert "ord_hdr_east" in cache["tables"] and "ord_tmp_east" not in cache["tables"], "排除优先"
    assert "member_points_ledger" in cache["tables"], "显式列出的进范围"
    meta = cache["tables"]["ord_hdr_east"]
    assert {c["name"] for c in meta["columns"]} == {"id", "biz_date", "amount", "status", "ord_id"}

    rows = await _rows(big)
    reflected = [r for r in rows.values() if r.in_scope]
    assert len(reflected) == 400 and all(r.struct_version == 1 and r.fingerprint for r in reflected)
    assert all(r.meta is None for r in rows.values() if not r.in_scope), "范围外的不存结构"

    # 目录清单只列范围内的；没有一张范围外的表被说成「表已不在」
    listed = (await client.get(f"/api/datasources/{big}/catalog")).json()
    assert len(listed["tables"]) == 400 and listed["schema_total"] == 3000 and listed["schema_truncated"] is True
    assert listed["scope"]["in_scope"] == 400


async def test_stop_keeps_what_was_read_and_restart_marks_interrupted(client, big, monkeypatch):
    await _scope(client, big, mode="rules", include=["ord_*"])
    real = so.reflect
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        got = await real(*args, **kwargs)
        await asyncio.sleep(0.05)
        await gate.wait()
        return got

    monkeypatch.setattr(so, "reflect", slow)
    job = (await client.post(f"/api/datasources/{big}/introspect")).json()["job"]
    for _ in range(200):
        await asyncio.sleep(0.02)
        if (await client.get(f"/api/datasources/{big}/introspect-jobs/active")).json()["job"]["status"] == "running":
            break
    gate.set()                 # 放第一批过去，读完、记下了再关上
    for _ in range(300):
        await asyncio.sleep(0.02)
        if (await client.get(f"/api/datasources/{big}/introspect-jobs/{job['id']}")).json()["job"]["done"]:
            break
    gate.clear()
    r = await client.post(f"/api/datasources/{big}/introspect-jobs/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    gate.set()
    stopped = r.json()["job"]
    assert stopped["status"] == "cancelled"
    assert 0 < stopped["counts"]["ok"] < 150 and stopped["counts"]["cancelled"] > 0
    cache = await _cache(big)
    assert len(cache["tables"]) == stopped["counts"]["ok"], "读完的照样物化进表结构"
    r = await client.post(f"/api/datasources/{big}/introspect-jobs/{job['id']}/cancel")
    assert r.status_code == 409 and r.json()["code"] == "introspect_finished"

    # 服务重启：排队、进行中的标中断，没读的表 cancelled，不续跑
    async with SessionLocal() as session:
        row = await session.get(DataSource, big)
        queued, n = await introspect_jobs.prepare(session, row, actor="tester")
    assert n == 150
    async with SessionLocal() as session:
        await session.execute(update(IntrospectJob).where(IntrospectJob.id == queued.id).values(status="running"))
        await session.commit()
    assert await introspect_jobs.startup() >= 1
    async with SessionLocal() as session:
        again = await session.get(IntrospectJob, queued.id)
        tasks = list((await session.execute(select(IntrospectTask).where(IntrospectTask.job_id == queued.id)))
                     .scalars())
    assert again.status == "interrupted" and again.error == introspect_jobs.INTERRUPTED_ERROR
    assert {t.status for t in tasks} == {"cancelled"} and not introspect_jobs.manager.running(queued.id)


# --------------------------------------------------------------------------
# 和同源的起草、剖析排队
# --------------------------------------------------------------------------


async def test_introspect_queues_behind_profile_and_blocks_sync_writes(client, small, monkeypatch):
    sid, _ = small
    held = runtime.lanes.enter(sid, "profile", "someone-else")
    try:
        job = (await client.post(f"/api/datasources/{sid}/introspect")).json()["job"]
        await asyncio.sleep(0.1)
        now = (await client.get(f"/api/datasources/{sid}/introspect-jobs/{job['id']}")).json()["job"]
        assert now["status"] == "queued" and now["waiting_for"] == "profile", "排在同源的剖析后面"
        r = await client.post(f"/api/datasources/{sid}/introspect")
        assert r.status_code == 409 and r.json()["code"] == "introspect_busy"
    finally:
        held.release()
    await introspect_jobs.manager.wait(job["id"], timeout=30)
    assert (await client.get(f"/api/datasources/{sid}/introspect-jobs/{job['id']}")).json()["job"]["status"] \
        == "succeeded"

    # 探查进行中：同步起草、导入目录占不到队，照实说是探查在做
    gate = asyncio.Event()
    real = so.reflect

    async def wait(*args, **kwargs):
        await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(so, "reflect", wait)
    job = (await client.post(f"/api/datasources/{sid}/introspect")).json()["job"]
    for _ in range(100):
        await asyncio.sleep(0.02)
        if runtime.lanes.holders(sid) and runtime.lanes.holders(sid)[0].kind == "introspect":
            break
    r = await client.post(f"/api/datasources/{sid}/catalog/draft", json={"tables": ["alpha"]})
    assert r.status_code == 409 and r.json()["code"] == "introspect_busy" and "探查" in r.json()["detail"]
    gate.set()
    await introspect_jobs.manager.wait(job["id"], timeout=30)


# --------------------------------------------------------------------------
# 删除、结构版本、三种「不在」
# --------------------------------------------------------------------------


async def test_real_deletion_versions_and_the_three_kinds_of_absent(client, small):
    sid, path = small
    await introspect_now(client, sid)
    rows = await _rows(sid)
    assert {r.name: r.struct_version for r in rows.values()} == dict.fromkeys(
        ["Delta_Orders", "alpha", "beta", "eta", "gamma", "v_alpha"], 1)
    first = {n: r.fingerprint for n, r in rows.items()}

    db = sqlite3.connect(path)
    db.execute("ALTER TABLE beta ADD COLUMN note TEXT")
    db.execute("DROP TABLE gamma")
    db.commit()
    db.close()
    # 有目录的表：gamma（删掉了）、eta（排除了、显式移出 pinned 才能排掉）、alpha（规则排除也排不掉）
    from app.data import catalog

    async with SessionLocal() as session:
        for table in ("gamma", "eta", "alpha"):
            await catalog.write_entry(session, sid, table, {"label": {"value": table, "source": "human",
                                                                       "status": "confirmed"}},
                                      if_version=0, actor=None)
    await _scope(client, sid, mode="all", exclude=["alpha", "eta"], removed=["ETA"])
    out = await introspect_now(client, sid)
    rows = await _rows(sid)
    assert rows["beta"].struct_version == 2 and rows["beta"].fingerprint != first["beta"], "改了结构版本加 1"
    assert rows["Delta_Orders"].struct_version == 1 and rows["Delta_Orders"].fingerprint == first["Delta_Orders"]
    assert rows["gamma"].removed_at is not None and not rows["gamma"].in_scope, "真删除记 removed_at"
    assert rows["alpha"].in_scope and rows["alpha"].scope_reason == "pinned", "有目录的表规则排不掉"
    assert not rows["eta"].in_scope, "显式移出才排得掉 pinned"
    assert out["table_count"] == 4

    listed = {t["table_name"]: t for t in (await client.get(f"/api/datasources/{sid}/catalog")).json()["tables"]}
    assert listed["gamma"]["absent_reason"] == "removed" and listed["gamma"]["in_schema"] is False
    assert listed["eta"]["absent_reason"] == "out_of_scope", "范围外不是「表已不在」"
    assert listed["alpha"]["absent_reason"] is None

    # 读不到结构：在范围里、反射失败
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        await session.execute(update(SourceObject).where(SourceObject.source_id == sid, SourceObject.name == "alpha")
                              .values(meta=None, reflect_error="权限不够，读不到列"))
        await so.rebuild_schema_cache(session, row)
        await session.commit()
    listed = {t["table_name"]: t for t in (await client.get(f"/api/datasources/{sid}/catalog")).json()["tables"]}
    assert listed["alpha"]["absent_reason"] == "reflect_failed"
    detail = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "alpha"})).json()["detail"]
    assert "读不到它的结构" in detail and "权限不够" in detail
    detail = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "eta"})).json()["detail"]
    assert "探查范围外" in detail and "已删除" not in detail
    detail = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "gamma"})).json()["detail"]
    assert "已经没有 gamma" in detail

    # 删了又建回来：清掉 removed_at
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE gamma (id INTEGER PRIMARY KEY)")
    db.commit()
    db.close()
    await introspect_now(client, sid)
    assert (await _rows(sid))["gamma"].removed_at is None


async def test_index_falls_back_to_the_inspector_and_a_capped_index_does_not_delete(client, small, monkeypatch):
    sid, _ = small
    from app.data import engine as data_engine

    async def broken(*_a, **_k):
        raise RuntimeError("no such table: sqlite_master")

    monkeypatch.setattr(data_engine, "run_query", broken)
    preview = (await client.post(f"/api/datasources/{sid}/introspect", params={"dry_run": "true"})).json()
    assert preview["index_method"] == "inspector" and preview["table_count"] == 6
    monkeypatch.undo()
    await introspect_now(client, sid)

    monkeypatch.setattr(so, "INDEX_LIMIT", 3)
    preview = (await client.post(f"/api/datasources/{sid}/introspect", params={"dry_run": "true"})).json()
    assert preview["index_capped"] is True and preview["table_count"] == 3
    rows = await _rows(sid)
    assert not any(r.removed_at for r in rows.values()), "对象索引被截断时不判删除"


# --------------------------------------------------------------------------
# 范围规则（纯函数）、published 模板的 pinned
# --------------------------------------------------------------------------


def test_scope_rules():
    objects = [("Orders", "table"), ("order_line", "table"), ("tmp_orders", "table"), ("v_orders", "view"),
               ("Customers", "table"), ("audit_log", "table")]
    rules = so.ScopeRules(mode="rules", include=["ORDER*", "*orders"], exclude=["tmp_*"], views=False)
    plan = so.plan_scope(objects, rules, pinned=set(), on_demand=set())
    assert list(plan.in_scope) == ["order_line", "Orders"], "按 name_key 不分大小写、排除优先、views=false 排掉视图"
    plan = so.plan_scope(objects, rules, pinned={"tmp_orders", "audit_log"}, on_demand={"customers"})
    assert plan.in_scope == {"audit_log": "pinned", "Customers": "on_demand", "order_line": "rule",
                             "Orders": "rule", "tmp_orders": "pinned"}
    assert plan.capped_count == 4, "按需加入不计上限"
    rules = so.ScopeRules(mode="rules", include=[], tables=["customers"], removed=["TMP_ORDERS"])
    plan = so.plan_scope(objects, rules, pinned={"tmp_orders"}, on_demand=set())
    assert plan.in_scope == {"Customers": "explicit"}, "显式移出压过 pinned；mode=rules 没写包含只剩显式的"
    rules = so.ScopeRules(max_tables=2)
    plan = so.plan_scope(objects, rules, pinned=set(), on_demand=set())
    assert plan.over and "6 张" in so.too_large_message(plan) and "上限 2 张" in so.too_large_message(plan)
    assert so.rules_problem({"mode": "rules", "max_tables": 2001}) and so.rules_problem({"include": "x"})
    assert so.rules_problem({"mode": "rules", "include": ["a*"], "max_tables": 2000}) is None


async def test_published_template_tables_are_pinned(client, small, monkeypatch):
    sid, _ = small
    async with SessionLocal() as session:
        name = (await session.get(DataSource, sid)).name
    graph = {"nodes": [
        {"id": "start", "type": "input", "data": {"label": "start", "config": {}}},
        {"id": "q", "type": "tool", "data": {"label": "q", "config": {"tool": f"db_query__{name}",
                                                                       "args": {"sql": "SELECT * FROM beta"}}}},
        {"id": "out", "type": "output", "data": {"label": "out", "config": {"fields": []}}},
    ], "edges": [{"source": "start", "target": "q"}, {"source": "q", "target": "out"}]}
    from types import SimpleNamespace

    from app.data import catalog_impact

    async def published(_session):
        return [(SimpleNamespace(id="w"), SimpleNamespace(graph=graph))]

    monkeypatch.setattr(catalog_impact, "_published", published)
    await _scope(client, sid, mode="rules", include=["alpha"])
    out = await introspect_now(client, sid)
    rows = await _rows(sid)
    assert rows["beta"].scope_reason == "pinned" and out["table_count"] == 2


# --------------------------------------------------------------------------
# 每种方言的字典查询（金样）
# --------------------------------------------------------------------------

GOLDEN = {
    ("sqlite", None): "SELECT name, type, NULL AS comment, NULL AS est_rows FROM sqlite_master WHERE type IN ('table', "
                      "'view') AND substr(name, 1, 7) <> 'sqlite_' ORDER BY name",
    ("mysql", "shop"): "SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, TABLE_ROWS FROM information_schema.TABLES WHERE "
                       "TABLE_SCHEMA = 'shop' ORDER BY TABLE_NAME",
    ("mysql", None): "SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT, TABLE_ROWS FROM information_schema.TABLES WHERE "
                     "TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
    ("postgresql", "public"): "SELECT c.relname, c.relkind, obj_description(c.oid, 'pg_class'), c.reltuples FROM "
                              "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE "
                              "n.nspname = 'public' AND c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT "
                              "c.relispartition ORDER BY c.relname",
    ("oracle", "ANALYTICS"): "SELECT o.object_name, o.object_type, c.comments, t.num_rows FROM all_objects o LEFT JOIN "
                             "all_tab_comments c ON c.owner = o.owner AND c.table_name = o.object_name LEFT JOIN "
                             "all_tables t ON t.owner = o.owner AND t.table_name = o.object_name WHERE o.owner = "
                             "'ANALYTICS' AND o.object_type IN ('TABLE', 'VIEW') AND o.generated = 'N' AND "
                             "o.object_name NOT LIKE 'BIN$%' ORDER BY o.object_name",
}


@pytest.mark.parametrize("kind,schema", list(GOLDEN))
def test_index_sql_golden_parses_and_passes_the_guard(kind, schema):
    import sqlglot

    from app.data.guard import check

    sql = so.index_sql(kind, schema)
    assert sql == GOLDEN[(kind, schema)]
    read = {"postgresql": "postgres"}.get(kind, kind)
    assert sqlglot.parse_one(sql, read=read) is not None
    check(sql, readonly=True, source_name="x", dialect=kind)
    assert so.index_sql("mariadb", "shop") == GOLDEN[("mysql", "shop")]
    assert so.index_sql("db2", None) is None
