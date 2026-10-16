"""数据源变化、定时维护、后台任务条的界面接口（next-round 03 B2–B4、05 (g)，第 4 波 W4-d）。

第 3 波的变化接口只给了 agentlab-change/1 的原样；界面要的几样由服务端给，界面不自己判（界面层不放逻辑）：

- 变化列表每条另带 category（结构 / 取值）、basis_text（依据的口径，照实写整表 / 窗口 / 块采样 / 前 N 行）、actions
  （现在能做哪几个处理动作、做不了的写原因）、job（处理时建的任务）、影响面里每个目录项现在的状态（清理过的照实为空）；
- 列表能按 category 筛、能看「处理过的」（解决了的、标为误报的，最近的在前）；
- GET /upkeep：数据源列表一次取齐各源要处理的变化条数（角标）和定时维护的频率；
- GET /{id}/observations：表详情「结构与取值变化」一节的最近几次观察，各带口径；遮罩在读的那一刻再核；
- GET /{id}/background-jobs：这个源进行中和排队的后台任务（探查、起草、剖析），按同源队的先后排好；
- GET /maintenance：定时维护的定时任务是定时任务页同一个形状，最近一次触发带上 inputs（建了哪些任务、哪几步没执行和原因），
  另说调度开没开；
- source.changed 打开订阅（只推全局渠道），链接落到目录页的「变化」标签页。

SQLite 夹具是虚构的店铺库；名字、数字都是假的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core import job_runtime as runtime
from app.data import catalog, source_changes as sc
from app.db.base import SessionLocal, utcnow
from app.db.models import CatalogDraftJob, CatalogProfileJob, DataSource, NotifyChannel, Schedule, ScheduleFire
from app.db.models_sources import IntrospectJob, SourceChange, TableObservation
from app.main import app
from app.notify import core, service
from app.scheduler import service as scheduler_service
from tests.fixtures.sources import drop_source, introspect_now


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(client, tmp_path):
    """探查建基线、写目录（status 列上有人工确认的码值和中文名），删掉 status 列再探查：得到一条要处理的 column_removed。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR(10), channel VARCHAR(8), amount NUMERIC(10, 2));
        INSERT INTO orders VALUES (1, 'paid', 'web', 3), (2, 'void', 'shop', 4);
    """)
    db.commit()
    db.close()
    name = f"upk_{uuid.uuid4().hex[:8]}"
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
    made: list[str] = []
    yield {"id": sid, "name": name, "schedules": made}
    async with SessionLocal() as session:
        await session.execute(delete(Schedule).where(Schedule.id.in_(made)))
        await session.commit()
    await drop_source(sid)


async def _codes_new(sid: str) -> SourceChange:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        change, _new = await sc.record(session, row, sc.NewChange(
            kind=sc.CODES_NEW, table="orders", column="channel", severity="warning",
            after={"codes": ["app"], "shares": {"app": 0.4}}, basis={"method": "first_rows", "key": "first_rows:100000"},
            affects={"catalog": [], "templates": [], "rules_touched": False}, key=("first_rows:100000", ("app",))),
            sc.Detected(job_kind="profile", job_id="job-upk", trigger="schedule"))
        await session.commit()
        return change


def _by_kind(items: list[dict]) -> dict[str, dict]:
    return {c["kind"]: c for c in items}


# --------------------------------------------------------------------------
# 变化列表：界面要的几样由服务端给
# --------------------------------------------------------------------------


async def test_changes_carry_category_basis_actions_and_current_status(client, shop):
    sid = shop["id"]
    await _codes_new(sid)
    body = (await client.get(f"/api/datasources/{sid}/changes")).json()
    by = _by_kind(body["items"])
    removed, fresh = by["column_removed"], by["codes_new"]
    assert removed["category"] == "struct" and fresh["category"] == "value"
    assert fresh["basis_text"] == "按存储顺序取前 100000 行", "依据的口径照实写"
    assert removed["basis_text"] is None, "结构变化的依据是探查读到的结构版本（界面照 struct_version 写）"

    acts = {a["action"]: a for a in removed["actions"]}
    assert list(acts) == ["clean_catalog", "redraft", "reprofile", "acknowledge", "dismiss"], acts
    assert acts["clean_catalog"]["ok"] and acts["redraft"]["ok"] and acts["dismiss"]["ok"]
    assert not acts["reprofile"]["ok"] and "剖析" in acts["reprofile"]["reason"], "剖析没开：照实写做不了的原因"
    assert "add_codes" not in acts, "只有出现了新码值才给「加入码值」"
    assert [a["action"] for a in fresh["actions"]][:1] == ["add_codes"]

    refs = removed["affects"]["catalog"]
    assert refs and all(r["now"]["status"] == "confirmed" for r in refs if r["path"].startswith("columns.status"))
    assert removed["job"] is None


async def test_category_and_handled_filters(client, shop):
    sid = shop["id"]
    change = await _codes_new(sid)
    url = f"/api/datasources/{sid}/changes"
    value = (await client.get(url, params={"category": "value"})).json()
    assert [c["kind"] for c in value["items"]] == ["codes_new"] and value["total"] == 1
    struct = (await client.get(url, params={"category": "struct"})).json()
    assert struct["items"] and all(c["category"] == "struct" for c in struct["items"])
    none = (await client.get(url, params={"category": "value", "kind": ["column_removed"]})).json()
    assert none["items"] == [] and none["total"] == 0, "种类和类别对不上：一条都没有，不是不筛"
    assert (await client.get(url, params={"category": "nope"})).status_code == 422

    assert (await client.get(url, params={"status": "handled"})).json()["items"] == []
    r = await client.post(f"{url}/{change.id}/resolve", json={"action": "dismiss", "note": "测试库造的取值"})
    assert r.status_code == 200, r.text
    got = r.json()["change"]
    assert got["status"] == "dismissed" and got["actions"] == [], "处理过的不再给动作"
    handled = (await client.get(url, params={"status": "handled"})).json()
    assert [c["id"] for c in handled["items"]] == [change.id] and handled["total"] == 1
    assert change.id not in {c["id"] for c in (await client.get(url)).json()["items"]}


async def test_acknowledged_change_offers_no_second_acknowledge_and_clean_shows_items_gone(client, shop):
    sid = shop["id"]
    url = f"/api/datasources/{sid}/changes"
    removed = _by_kind((await client.get(url)).json()["items"])["column_removed"]
    r = await client.post(f"{url}/{removed['id']}/resolve", json={"action": "acknowledge"})
    assert r.status_code == 200
    acts = [a["action"] for a in r.json()["change"]["actions"]]
    assert "acknowledge" not in acts and "clean_catalog" in acts, "知道了还没处理：还能清理、不能再点知道了"
    r = await client.post(f"{url}/{removed['id']}/resolve", json={"action": "clean_catalog"}, headers={"X-Actor": "reviewer"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["catalog_version"] >= 2 and got["change"]["resolution"]["catalog_version"] == got["catalog_version"]
    gone = [ref for ref in got["change"]["affects"]["catalog"] if ref["path"].startswith("columns.status")]
    assert gone and all(ref["now"] is None for ref in gone), "清理过的项现在不在目录里了：照实为空"


# --------------------------------------------------------------------------
# 数据源列表：角标和定时维护一次取齐
# --------------------------------------------------------------------------


async def test_upkeep_gives_badge_counts_and_maintenance_per_source(client, shop):
    sid = shop["id"]
    r = await client.post("/api/schedules", json={
        "name": "店铺库的定时维护", "target_kind": "source_maintenance", "target_id": sid, "cron": "0 3 * * *",
        "timezone": "Asia/Shanghai", "enabled": True, "overlap": "skip", "misfire_grace_s": 3600})
    assert r.status_code in (200, 201), r.text
    shop["schedules"].append(r.json()["id"])
    items = {i["source_id"]: i for i in (await client.get("/api/datasources/upkeep")).json()["items"]}
    mine = items[sid]
    assert mine["changes"]["by_severity"]["action"] >= 1 and mine["changes"]["pending"] >= 1
    [m] = mine["maintenance"]
    assert m["enabled"] is True and m["timezone"] == "Asia/Shanghai" and "03:00" in m["description_text"]
    assert m["next_run_at"], "开着的维护写下一次什么时候"


# --------------------------------------------------------------------------
# 表详情：最近几次观察，各带口径；遮罩在读的那一刻再核
# --------------------------------------------------------------------------


async def test_observations_grouped_with_basis_and_masked_columns_hidden(client, shop):
    sid = shop["id"]
    now = utcnow()
    async with SessionLocal() as session:
        for i, rows in enumerate((120, 118)):
            session.add(TableObservation(source_id=sid, table_name="orders", column_name="", kind="rows",
                                         basis={"method": "count"}, basis_key="count",
                                         data={"rows": rows, "at_least": False, "method": "count"},
                                         at=now - timedelta(days=i)))
        session.add(TableObservation(source_id=sid, table_name="orders", column_name="channel", kind="codes_observe",
                                     basis={"method": "sample", "percent": 1.0}, basis_key="sample:1",
                                     data={"values": [["web", 30], ["shop", 10]], "rows": 40, "truncated": False}, at=now))
        session.add(TableObservation(source_id=sid, table_name="orders", column_name="channel", kind="codes_observe",
                                     basis={"method": "first_rows", "rows": 1000}, basis_key="first_rows:1000",
                                     data={"values": [["web", 5]], "rows": 5, "truncated": False},
                                     at=now - timedelta(days=2)))
        session.add(TableObservation(source_id=sid, table_name="orders", column_name="amount", kind="codes_observe",
                                     basis={"method": "full"}, basis_key="full",
                                     data={"values": [["3", 1]], "rows": 1, "truncated": False}, at=now))
        await session.commit()
    url = f"/api/datasources/{sid}/observations"
    body = (await client.get(url, params={"table": "orders"})).json()
    series = {(s["kind"], s["column"]): s for s in body["series"]}
    rows = series[("rows", "")]
    assert [p["data"]["rows"] for p in rows["points"]] == [120, 118], "新的在前"
    assert rows["points"][0]["basis_text"] == "逐行数出" and rows["basis_changed"] is False
    codes = series[("codes_observe", "channel")]
    assert codes["points"][0]["basis_text"] == "块采样约 1%"
    assert codes["points"][1]["basis_text"] == "按存储顺序取前 1000 行"
    assert codes["basis_changed"] is True, "口径换过：照实说，不跨口径比"
    assert codes["points"][0]["data"]["values"] == [["web", 30, 0.75], ["shop", 10, 0.25]], "占比由服务端算"
    assert [s["kind"] for s in body["series"]][0] == "rows", "按观察种类的固定顺序"

    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.options = {**(row.options or {}), "mask_columns": ["amount"]}
        await session.commit()
    body = (await client.get(url, params={"table": "orders"})).json()
    assert ("codes_observe", "amount") not in {(s["kind"], s["column"]) for s in body["series"]}, \
        "现在被遮罩的列：观察时还没遮的也不给"
    assert (await client.get(url, params={"table": "nope"})).json()["series"] == []


# --------------------------------------------------------------------------
# 后台任务条：按同源队的先后
# --------------------------------------------------------------------------


async def test_background_jobs_follow_the_lane_order(client, shop):
    sid = shop["id"]
    async with SessionLocal() as session:
        intro = IntrospectJob(source_id=sid, status="queued", created_at=utcnow() - timedelta(seconds=30))
        draft = CatalogDraftJob(source_id=sid, status="running")
        profile = CatalogProfileJob(source_id=sid, status="queued", scope="selected", settings={})
        session.add_all([intro, draft, profile])
        await session.commit()
        ids = {"introspect": intro.id, "draft": draft.id, "profile": profile.id}
    # 队里是 起草（在做）→ 剖析（排着）；探查刚建、还没进队：排在队里的后面
    tickets = [runtime.lanes.enter(sid, "draft", ids["draft"]), runtime.lanes.enter(sid, "profile", ids["profile"])]
    try:
        items = (await client.get(f"/api/datasources/{sid}/background-jobs")).json()["items"]
        assert [i["kind"] for i in items] == ["draft", "profile", "introspect"], items
        assert [i["id"] for i in items] == [ids["draft"], ids["profile"], ids["introspect"]]
        assert items[0]["position"] == 0 and items[1]["position"] == 1 and items[2]["position"] is None
        assert items[1]["waiting_for"] == "draft", "排队的写等的是什么（服务端的 waiting_for）"
        assert {"total", "done", "status"} <= set(items[0])
    finally:
        for t in tickets:
            t.release()
        async with SessionLocal() as session:
            for model, key in ((IntrospectJob, "introspect"), (CatalogDraftJob, "draft"), (CatalogProfileJob, "profile")):
                await session.execute(delete(model).where(model.id == ids[key]))
            await session.commit()
    assert (await client.get(f"/api/datasources/{sid}/background-jobs")).json()["items"] == []


# --------------------------------------------------------------------------
# 定时维护：定时任务同一个形状，最近一次触发带 inputs
# --------------------------------------------------------------------------


async def test_maintenance_returns_schedule_shape_and_last_fire_inputs(client, shop):
    sid = shop["id"]
    url = f"/api/datasources/{sid}/maintenance"
    body = (await client.get(url)).json()
    assert body["maintained"] is True and body["schedules"] == [] and isinstance(body["scheduler_on"], bool)
    r = await client.post("/api/schedules", json={
        "name": "店铺库的定时维护", "target_kind": "source_maintenance", "target_id": sid, "cron": "30 2 * * *",
        "timezone": "Asia/Shanghai", "enabled": True, "overlap": "skip", "misfire_grace_s": 3600})
    schedule_id = r.json()["id"]
    shop["schedules"].append(schedule_id)
    inputs = {"steps": ["introspect", "profile_needed", "observe"],
              "jobs": [{"step": "introspect", "kind": "introspect", "id": "job-i", "tables": 3}],
              "skipped": [{"step": "profile_needed", "reason": "数据源「x」未开启数据剖析"}]}
    async with SessionLocal() as session:
        session.add(ScheduleFire(schedule_id=schedule_id, fire_at=utcnow(), kind="scheduled", status="started",
                                 reason="剖析需要的表没有执行：未开启", inputs=inputs))
        await session.commit()
    [s] = (await client.get(url)).json()["schedules"]
    assert s["id"] == schedule_id and s["version"] >= 1 and s["target_kind"] == "source_maintenance"
    assert "02:30" in s["description_text"] and s["next_run_at"]
    assert s["last_fire"]["status"] == "started" and s["last_fire"]["inputs"] == inputs

    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.origin = "upload"
        await session.commit()
    assert (await client.get(url)).json()["maintained"] is False, "上传表格、记录表不给这一节"


# --------------------------------------------------------------------------
# 通知：source.changed 打开订阅，链接落到「变化」标签页
# --------------------------------------------------------------------------


async def test_source_changed_is_subscribable_and_links_to_the_changes_tab(client, shop, monkeypatch):
    assert service.EVENTS["source.changed"].scopes == ("global",), "只推全局渠道（按源汇总，不归哪个人）"
    assert service.subscribe_problem("source.changed", "global") is None
    assert service.subscribe_problem("source.changed", "user") is not None
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(service, "_base_url", _fake_base)
    from app.core.crypto import encrypt

    async with SessionLocal() as session:
        channel = NotifyChannel(name=f"变化-{uuid.uuid4().hex[:6]}", scope="global", format="generic",
                                url=encrypt("https://hooks.example.com/agentlab/upkeep-3b9e"), events=["source.changed"])
        session.add(channel)
        await session.commit()
        channel_id = channel.id
    try:
        assert await service.notify_source_changed(shop["id"]) == 1
        [request] = got
        body = json.loads(request.content)
        links = json.dumps(body, ensure_ascii=False)
        assert f"/data/catalog/{shop['id']}?view=changes" in links, links
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(NotifyChannel).where(NotifyChannel.id == channel_id))
            await session.commit()


async def _fake_base(_session) -> str:
    return "https://agentlab.example.com"


def test_schedule_target_columns_are_the_ones_the_page_uses():
    """数据源设置里建的维护定时：target_kind / target_id 写法和调度那一套同一处封装（X4）。"""
    cols = scheduler_service.target_columns("source_maintenance", "src-x")
    assert cols["target_kind"] == "source_maintenance" and cols["target_id"] == "src-x" and cols["workflow_id"] == ""
