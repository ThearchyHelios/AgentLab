"""数据源的变化记录：接口、处理动作、给召回的拉取、通知（next-round 03 B3）。

SQLite 夹具（虚构的店铺库）探查建基线、写目录，再删一列、重新探查得到真实的结构变化；取值变化（新码值）直接经
source_changes.record 记一条。守的是：

- GET /changes 缺省列开着的和知道了的，带各级别计数；状态、种类筛选；不认识的状态 422；
- 处理：误报必须写理由（422），处理过的再处理 409；清理目录把影响面里的项从目录里去掉、记目录版本；加入码值走
  apply_patch（含义待填写、依据写变化编号）、记目录版本；知道了只改状态；剖析没开时「重新剖析」照实 422；
- 同一个变化再次测到只更新 last_seen_at、seen_count；
- list_changes 按 seq 游标拉、affecting 按表反查、triggers_recall 只认结构类和触及规则的新码值；
- source.changed 的推送：订阅还没打开时推不出去；打开后（测试里临时打开）消息里只有源名、条数和链接。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data import catalog, source_changes as sc
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.db.models_sources import SourceChange
from app.main import app
from app.notify import core, service
from tests.fixtures.sources import drop_source, introspect_now


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(client, tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR(10), channel VARCHAR(8), amount NUMERIC(10, 2));
        INSERT INTO orders VALUES (1, 'paid', 'web', 3), (2, 'void', 'shop', 4);
    """)
    db.commit()
    db.close()
    name = f"chg_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": str(path),
                                                    "readonly": True})
    sid = r.json()["id"]
    await introspect_now(client, sid)
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", {
            "label": catalog.make_item("订单", "human"),
            "columns": {"status": {"codes": catalog.make_item({"paid": "已支付", "void": "作废"}, "human"),
                                   "label": catalog.make_item("状态", "human")},
                        "channel": {"codes": catalog.make_item({"web": "线上"}, "profile", "proposed")}},
        }, if_version=0, actor="测试员")
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE orders DROP COLUMN status")
    db.commit()
    db.close()
    await introspect_now(client, sid)
    yield {"id": sid, "name": name}
    await drop_source(sid)


async def _change(sid: str, kind: str) -> SourceChange:
    async with SessionLocal() as session:
        return (await session.execute(select(SourceChange).where(
            SourceChange.source_id == sid, SourceChange.kind == kind))).scalars().first()


async def _codes_new(sid: str, *, rules: bool = False) -> SourceChange:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        change, new = await sc.record(session, row, sc.NewChange(
            kind=sc.CODES_NEW, table="orders", column="channel", severity="warning",
            after={"codes": ["app"], "shares": {"app": 0.4}}, basis={"method": "full", "key": "full"},
            affects={"catalog": [], "templates": [], "rules_touched": rules}, key=("full", ("app",))),
            sc.Detected(job_kind="profile", job_id="job-x", trigger="schedule"))
        await session.commit()
        assert new
        return change


async def test_list_counts_and_filters(client, shop):
    sid = shop["id"]
    r = await client.get(f"/api/datasources/{sid}/changes")
    assert r.status_code == 200, r.text
    body = r.json()
    [removed] = [c for c in body["items"] if c["kind"] == "column_removed"]
    assert removed["schema"] == "agentlab-change/1" and removed["column"] == "status" and removed["status"] == "open"
    assert removed["severity"] == "action" and "删除了列" in removed["summary"]
    assert body["counts"]["pending"] == body["total"] and body["counts"]["by_severity"]["action"] >= 1
    r = await client.get(f"/api/datasources/{sid}/changes", params={"kind": ["column_added"]})
    assert r.json()["items"] == []
    assert (await client.get(f"/api/datasources/{sid}/changes", params={"status": "nope"})).status_code == 422
    one = (await client.get(f"/api/datasources/{sid}/changes/{removed['id']}")).json()["change"]
    assert one["id"] == removed["id"]


async def test_dismiss_needs_a_reason_and_finished_changes_are_final(client, shop):
    sid = shop["id"]
    change = await _change(sid, "column_removed")
    url = f"/api/datasources/{sid}/changes/{change.id}/resolve"
    r = await client.post(url, json={"action": "dismiss"})
    assert r.status_code == 422 and "理由" in r.json()["detail"]
    r = await client.post(url, json={"action": "dismiss", "note": "测试库里故意删的列"}, headers={"X-Actor": "tester"})
    assert r.status_code == 200, r.text
    got = r.json()["change"]
    assert got["status"] == "dismissed" and got["resolution"]["note"] == "测试库里故意删的列"
    assert got["resolution"]["action"] == "dismiss" and got["resolution"]["by"] == "tester"
    assert (await client.post(url, json={"action": "acknowledge"})).status_code == 409
    counts = (await client.get(f"/api/datasources/{sid}/changes")).json()["counts"]
    assert counts["by_severity"]["action"] == 0, "误报不再算要处理"


async def test_clean_catalog_removes_the_items_and_records_the_catalog_version(client, shop):
    sid = shop["id"]
    change = await _change(sid, "column_removed")
    r = await client.post(f"/api/datasources/{sid}/changes/{change.id}/resolve", json={"action": "clean_catalog"})
    assert r.status_code == 200, r.text
    body = r.json()
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "orders")
    assert "status" not in (entry.notes.get("columns") or {}), "说到被删的列的项都清掉了"
    assert entry.notes["label"]["value"] == "订单", "别的项不动"
    assert body["catalog_version"] == entry.version == body["change"]["resolution"]["catalog_version"]
    assert body["change"]["status"] == "resolved"


async def test_add_codes_goes_through_apply_patch(client, shop):
    sid = shop["id"]
    change = await _codes_new(sid)
    r = await client.post(f"/api/datasources/{sid}/changes/{change.id}/resolve", json={"action": "add_codes"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "orders")
    item = entry.notes["columns"]["channel"]["codes"]
    assert item["value"] == {"web": "线上", "app": ""}, "码值是补充：原来的留着，新码值含义待填写"
    assert (item["source"], item["status"]) == ("human", "confirmed")
    assert change.id[:6] in item["reason"], "确认时的依据写这条变化的编号"
    assert r.json()["catalog_version"] == entry.version
    removed = await _change(sid, "column_removed")
    r = await client.post(f"/api/datasources/{sid}/changes/{removed.id}/resolve", json={"action": "add_codes"})
    assert r.status_code == 422, "只有新码值能加入码值"


async def test_acknowledge_and_reprofile_when_profiling_is_off(client, shop):
    sid = shop["id"]
    change = await _codes_new(sid)
    url = f"/api/datasources/{sid}/changes/{change.id}/resolve"
    r = await client.post(url, json={"action": "reprofile"})
    assert r.status_code == 422 and "剖析" in r.json()["detail"], "剖析没开：照实说做不了"
    r = await client.post(url, json={"action": "acknowledge"})
    assert r.status_code == 200 and r.json()["change"]["status"] == "acknowledged"
    counts = (await client.get(f"/api/datasources/{sid}/changes")).json()["counts"]
    assert counts["pending"] > counts["open"], "知道了还没处理的照样算要人看"


async def test_same_change_detected_again_only_bumps_seen_count(shop):
    sid = shop["id"]
    first = await _codes_new(sid)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        again, new = await sc.record(session, row, sc.NewChange(
            kind=sc.CODES_NEW, table="orders", column="channel", severity="action", key=("full", ("app",))),
            sc.Detected(job_kind="profile"))
        await session.commit()
    assert not new and again.id == first.id and again.seen_count == 2 and again.severity == "action"


async def test_cursor_pull_affecting_and_recall_triggers(shop):
    sid, name = shop["id"], shop["name"]
    codes = await _codes_new(sid, rules=True)
    async with SessionLocal() as session:
        everything = await sc.list_changes(session, source_id=sid)
        seqs = [c["seq"] for c in everything]
        assert seqs == sorted(seqs) and len(seqs) >= 2
        rest = await sc.list_changes(session, source_id=sid, since_seq=seqs[0])
        assert [c["seq"] for c in rest] == seqs[1:], "按 seq 游标：大于 since_seq 的"
        hit = await sc.affecting(session, name, ["ORDERS"])
        assert {c["id"] for c in hit} == {c["id"] for c in everything}, "表名不分大小写"
        assert await sc.affecting(session, name, ["refunds"]) == []
        assert await sc.affecting(session, name, ["orders"], since=seqs[-1]) == []
    by = {c["kind"]: c for c in everything}
    assert sc.triggers_recall(by["column_removed"]), "结构类触发召回"
    assert sc.triggers_recall(next(c for c in everything if c["id"] == codes.id)), "触及有效记录条件的新码值触发召回"
    assert not sc.triggers_recall({"kind": "volume_drop", "affects": {}}), "纯取值漂移只算数据质量提醒"


async def test_source_changed_message_has_only_name_counts_and_link(shop, monkeypatch):
    sid, name = shop["id"], shop["name"]
    assert await service.notify_source_changed(sid) == 0, "订阅还没打开（第 4 波）：推不出去"
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setitem(service.EVENTS, "source.changed", service.Event("数据源有变化要处理", ("global",)))
    async with SessionLocal() as session:
        from app.core.crypto import encrypt
        from app.db.models import NotifyChannel

        channel = NotifyChannel(name=f"变化-{uuid.uuid4().hex[:6]}", scope="global", format="generic",
                                url=encrypt("https://hooks.example.com/agentlab/changes-7a1c"),
                                events=["source.changed"])
        session.add(channel)
        await session.commit()
        channel_id = channel.id
    try:
        assert await service.notify_source_changed(sid) == 1
        [request] = got
        body = json.loads(request.content)
        text = body["title"] + body["text"]
        assert body["event"] == "source.changed" and name in body["title"]
        assert "要处理" in text and "orders" not in text and "status" not in text and "app" not in text, \
            "不放表名、列名、取值"
        assert body["subject"] == {"source_id": sid}
    finally:
        async with SessionLocal() as session:
            from app.db.models import NotifyChannel

            row = await session.get(NotifyChannel, channel_id)
            await session.delete(row)
            await session.commit()


def test_maintenance_option_is_hidden_from_the_datasource_form():
    from app.api import datasources
    from app.data import source_maintenance

    assert source_maintenance.MAINTENANCE_OPTION in datasources._MANAGED_OPTIONS
