"""表结构变化：前后差异进变化记录、影响面、结构版本与 schema.drift、发布前检查（next-round 03 B1）。

用一个 SQLite 夹具（虚构的店铺库：stores、orders、refunds）走一遍真实路径：后台探查建基线 → 写目录（人工确认的码值、
有效记录条件、关联关系）→ 发布一个写死 SQL 的模板（记下结构版本）→ ALTER（删列、加列、改名、改类型、加注释）→ 重新探查：

- 变化恰好就是这几条，疑似改名只说「可能」；第一次探查只建基线、不报变化；
- 删掉的列上人工确认过的项、写死这一列的已发布模板都列进影响面，这条变化是「要处理」（action），有效记录条件用到它记
  rules_touched；
- 再探查一次（结构没变）不重复记；表真删了记 table_removed，范围外 ≠ 删除；
- 从这一版发起的正式运行写一条 schema.drift、照常发起；发布前检查列出这张表上还没处理的结构变化（警告，不拦）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
from app.db.models_sources import SourceChange, SourceObject
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.sources import drop_source, introspect_now

SQL = "SELECT o.status, o.amount FROM orders o WHERE o.status = 'paid'"


def _build(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE stores (id INTEGER PRIMARY KEY, name VARCHAR(40));
        CREATE TABLE orders (id INTEGER PRIMARY KEY, store_id INTEGER REFERENCES stores(id), status VARCHAR(10),
                             amount NUMERIC(12, 2), note VARCHAR(40), paid_on DATE);
        CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount NUMERIC(12, 2));
        INSERT INTO stores VALUES (1, 'east'), (2, 'west');
        INSERT INTO orders VALUES (1, 1, 'paid', 10.5, 'a', '2026-10-01'), (2, 2, 'void', 3, 'b', '2026-10-02');
    """)
    db.commit()
    db.close()


def _alter(path: Path) -> None:
    """删 status、加 channel、note 改名为 remark（同位置同类型）、amount 改类型（SQLite 没有改类型：整表重建）。"""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE orders_new (id INTEGER PRIMARY KEY, store_id INTEGER REFERENCES stores(id),
                                 amount NUMERIC(14, 2), remark VARCHAR(40), paid_on DATE, channel VARCHAR(8));
        INSERT INTO orders_new SELECT id, store_id, amount, note, paid_on, 'web' FROM orders;
        DROP TABLE orders;
        ALTER TABLE orders_new RENAME TO orders;
    """)
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(client, tmp_path):
    path = tmp_path / "shop.db"
    _build(path)
    name = f"shop_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": str(path),
                                                    "readonly": True})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    made: list[str] = []
    yield {"id": sid, "name": name, "path": path, "made": made}
    async with SessionLocal() as session:
        for wid in made:
            if (wf := await session.get(Workflow, wid)) is not None:
                await session.delete(wf)
        await session.commit()
    await drop_source(sid)


async def _changes(sid: str) -> list[SourceChange]:
    async with SessionLocal() as session:
        return list((await session.execute(select(SourceChange).where(SourceChange.source_id == sid)
                                           .order_by(SourceChange.seq))).scalars())


async def _write_notes(sid: str) -> None:
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", {
            "label": catalog.make_item("订单", "human"),
            "valid_filter": catalog.make_item("status <> 'void'", "human"),
            "columns": {"status": {"codes": catalog.make_item({"paid": "已支付", "void": "作废"}, "human", complete=True)},
                        "note": {"label": catalog.make_item("备注", "llm")}},
        }, if_version=0, actor="测试员")
        await catalog.write_entry(session, sid, "refunds", {
            "relations": [{"id": catalog.relation_id("refunds", ["order_id"], "orders", ["id"]),
                           "columns": ["order_id"], "to_table": "orders", "to_columns": ["id"],
                           "cardinality": "many_to_one", "source": "human", "status": "confirmed"}],
        }, if_version=0, actor="测试员")


def _graph(source: str) -> dict:
    def node(nid, ntype, **config):
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}

    nodes = [node("start", "input"), node("fetch", "tool", tool=f"db_query__{source}", args={"sql": SQL}),
             node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.fetch }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _publish(client, shop) -> str:
    r = await client.post("/api/workflows", json={"name": f"订单日报-{uuid.uuid4().hex[:6]}",
                                                  "graph": _graph(shop["name"])})
    wf = r.json()["id"]
    shop["made"].append(wf)
    body = (await client.post(f"/api/workflows/{wf}/publish", json={"level": "published"})).json()
    assert body["ok"] is True, body
    return wf


async def _settle(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def test_first_introspect_is_baseline_and_alter_reports_exactly_the_changes(client, shop):
    sid = shop["id"]
    await introspect_now(client, sid)
    assert await _changes(sid) == [], "第一次探查只建基线：不报「新出现的表」，也不报列"
    async with SessionLocal() as session:
        orders = (await session.execute(select(SourceObject).where(
            SourceObject.source_id == sid, SourceObject.name == "orders"))).scalar_one()
    assert orders.struct_version == 1
    await _write_notes(sid)
    wf = await _publish(client, shop)
    async with SessionLocal() as session:
        snap = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
    assert snap.schema_versions == {shop["name"]: {"orders": 1}}, "只记写死的 SQL 直接引用的表"
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["schema_versions"] == {shop["name"]: {"orders": 1}} and detail["schema_changes"] == []

    _alter(shop["path"])
    await introspect_now(client, sid)
    got = [(c.kind, c.column_name) for c in await _changes(sid) if c.table_name == "orders"]
    assert sorted(got) == sorted([("column_removed", "status"), ("column_removed", "note"), ("column_added", "remark"),
                                  ("column_added", "channel"), ("column_type_changed", "amount")]), got
    by = {(c.kind, c.column_name): c for c in await _changes(sid)}
    removed = by[("column_removed", "status")]
    assert removed.struct_version_before == 1 and removed.struct_version_after == 2
    assert removed.severity == "action", "人工确认过的码值、有效记录条件、写死它的模板都还说着这一列"
    paths = {(i["table"], i["path"]) for i in removed.affects["catalog"]}
    assert ("orders", "columns.status.codes") in paths and ("orders", "valid_filter") in paths
    assert removed.affects["rules_touched"] is True, "有效记录条件用到了被删的列"
    assert all(i["needs_action"] for i in removed.affects["catalog"] if i["status"] == "confirmed")
    [tpl] = removed.affects["templates"]
    assert tpl["workflow_id"] == wf and tpl["impact"] == "direct"
    [fetch] = [n for n in tpl["nodes"] if n["node_id"] == "fetch"]
    assert fetch["breaks"] is True and any(c.endswith("status") for c in fetch["unknown_columns"]), fetch
    # 疑似改名只说「可能」：note → remark 同位置同类型
    note = by[("column_removed", "note")]
    assert note.before["rename"] == {"from": "note", "to": "remark"} and note.kind == "column_removed"
    assert note.severity != "action", "只有推断的中文名说着这一列：不是要处理"
    assert by[("column_type_changed", "amount")].before == "NUMERIC(12, 2)"
    assert by[("column_type_changed", "amount")].after == "NUMERIC(14, 2)"
    assert all(c.detected_by["job_kind"] == "introspect" and c.detected_by["trigger"] == "manual"
               for c in await _changes(sid))

    # 结构没再变：再探查一次不重复记
    count = len(await _changes(sid))
    await introspect_now(client, sid)
    assert len(await _changes(sid)) == count

    # 正式运行：写一条 schema.drift，照常发起
    r = await client.post("/api/runs", json={"workflow_id": wf, "run_class": "formal"})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    await _settle(run_id)
    async with SessionLocal() as session:
        drift = [e.data for e in (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "schema.drift"))).scalars()]
    assert len(drift) == 1 and drift[0]["count"] == 1
    [table] = drift[0]["tables"]
    assert table["table"] == "orders" and table["published"] == 1 and table["current"] == 2 and not table["removed"]
    assert {c["kind"] for c in table["changes"]} >= {"column_removed", "column_type_changed"}
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert [(c["table"], c["published"], c["current"]) for c in detail["schema_changes"]] == [("orders", 1, 2)]

    # 发布前检查：这一版直接引用的 orders 上有还没处理的结构变化（警告，不拦）
    check = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published"})).json()
    [issue] = [i for i in check["issues"] if i.get("code") == "schema.change_open"]
    assert issue["level"] == "warning" and issue["node_id"] == "fetch" and "orders" in issue["message"]


async def test_table_removed_is_a_deletion_with_impact(client, shop):
    sid = shop["id"]
    await introspect_now(client, sid)
    await _write_notes(sid)
    db = sqlite3.connect(shop["path"])
    db.execute("DROP TABLE refunds")
    db.commit()
    db.close()
    await introspect_now(client, sid)
    [gone] = [c for c in await _changes(sid) if c.kind == "table_removed"]
    assert gone.table_name == "refunds" and gone.struct_version_before == 1
    assert gone.severity == "action", "人工确认过的关联关系还在这张表上"
    assert ("refunds", f"relations.{catalog.relation_id('refunds', ['order_id'], 'orders', ['id'])}") in {
        (i["table"], i["path"]) for i in gone.affects["catalog"]}


async def test_leaving_scope_is_not_a_deletion(client, shop):
    sid = shop["id"]
    await introspect_now(client, sid)
    await _write_notes(sid)
    r = await client.put(f"/api/datasources/{sid}/introspect-scope",
                         json={"scope": {"mode": "rules", "include": ["stores", "orders"], "removed": ["refunds"]}})
    assert r.status_code == 200, r.text
    await introspect_now(client, sid)
    kinds = {(c.kind, c.table_name) for c in await _changes(sid)}
    assert ("table_left_scope", "refunds") in kinds, "有目录的表离开了范围"
    assert ("table_removed", "refunds") not in kinds, "范围外 ≠ 删除"
    assert any(k == "scope_changed" for k, _ in kinds), "范围规则和上一次探查不同"


async def test_first_introspect_after_upgrade_only_builds_a_baseline(client, shop):
    """升级前同步探查写的老缓存（类型是通用写法 VARCHAR、没有 scope 摘要）：第一次按对象索引读到只建基线，不把写法的差异
    说成「类型变了」。"""
    from app.db.models import DataSource

    sid = shop["id"]
    legacy = {"tables": {"orders": {"qualified": "orders", "schema": None, "comment": None, "is_view": False,
                                    "primary_key": ["id"],
                                    "columns": [{"name": "id", "type": "INTEGER", "nullable": False},
                                                {"name": "status", "type": "VARCHAR", "nullable": True}]}},
              "truncated": False, "total": 1, "schema": None, "synced_at": "2026-09-01T00:00:00+00:00"}
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.schema_cache = legacy
        await session.commit()
    await introspect_now(client, sid)
    assert await _changes(sid) == [], "老缓存接进来的结构版本是 0：这一次只建基线"
    async with SessionLocal() as session:
        orders = (await session.execute(select(SourceObject).where(
            SourceObject.source_id == sid, SourceObject.name == "orders"))).scalar_one()
    assert orders.struct_version == 1
