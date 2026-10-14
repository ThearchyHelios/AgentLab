"""验收 V1 的服务端部分（docs/design/app-platform-and-look.md §7.1）：定时任务上的看板应用。

用虚构零售库（tests/fixtures/acceptance/retail.py）建源，customers.phone 设成遮罩列。工作流：取数 → 口径卡 → 报告 → 成果，
受管级 v1，配一个定时任务；定时任务的正式运行用 launch_run 直接发起（带 schedule_id，补一条触发记录），报告的写作者用
演示模型照固定文字写。从 v1 派生应用、发布，然后：

- 印章：截至（含统计期）/ 工作流名 v1 · 哈希前 8 位 / 口径卡的口径名和版本 / 完整出具 / 定时任务「…」第 2 次
- 指标块带证据指针，点开指标链一路追到查询快照的那一格；表格里的遮罩列是「已遮罩」
- 发布 v2、定时任务改跑 v2：应用照旧取 v1 最近一次封存的运行，印章写明「定时任务已改为运行 v2，本应用固定在 v1」
- 把钉住项升到 v2：不选处置被 app.upgrade_policy 拦下；选 incomparable，趋势在升版处断开
- 退役：退役当前版被拒；发布 v3 之后退役 v1，列出还固定着 v1 的应用，它标「需升级」，从 v1 发起正式运行被拒
- 可见性表达式写了算术，被 app.expr_arith 拦下
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.apps.ops import apply_app_op
from app.core.artifact_store import graph_hash
from app.data.engine import engines
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Run, Schedule, ScheduleFire, Workflow, WorkflowVersion
from app.engine.evidence import render_number
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.acceptance import retail

TODAY = date(2026, 10, 6)
SCHEDULE = "周一 10 点"
GMV_SQL = "SELECT ROUND(SUM(amount), 2) AS gmv, COUNT(*) AS orders FROM orders WHERE status = 'paid'"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def writer(monkeypatch):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda model, messages: AIMessage(content="本周销售额 [[m:gmv]]。"))


@pytest.fixture
async def source(tmp_path):
    path = retail.build(tmp_path / "retail.db", TODAY)
    name = f"retail_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options={"mask_columns": ["phone"]})
        session.add(row)
        await session.commit()
        source_id = row.id
    with sqlite3.connect(path) as db:
        gmv = db.execute(GMV_SQL).fetchone()[0]
    yield name, gmv
    await engines.invalidate(source_id)


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def weekly(source: str, version: str) -> dict:
    nodes = [
        node("start", "input", "开始", fields=[{"name": "week", "default": "2026-W40", "description": "统计周"}]),
        node("fetch", "tool", "销售取数", tool=f"db_query__{source}", args={"sql": GMV_SQL}),
        node("people", "tool", "会员样本", tool=f"db_query__{source}",
             args={"sql": "SELECT name, phone FROM customers ORDER BY id LIMIT 3"}),
        node("card", "metrics", "周销售口径卡", caliber="周销售口径", caliber_version=version, metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "写周报", instructions="写周报"),
        node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def publish_version(wf_id: str, version: int, graph: dict) -> str:
    """受管级发布一个版本（直接落库：门禁另有测试，这里要的是一个发布过的不可变版本）。"""
    async with SessionLocal() as session:
        session.add(WorkflowVersion(workflow_id=wf_id, version=version, graph=graph, graph_hash=graph_hash(graph),
                                    level="governed", published_at=utcnow(), published_by="周报编辑"))
        wf = await session.get(Workflow, wf_id)
        wf.published_version = wf.version = version
        wf.graph, wf.status = graph, "governed"
        await session.commit()
    return graph_hash(graph)


async def wait(run_id: str) -> Run:
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def fire(wf_id: str, schedule_id: str) -> Run:
    """定时任务到点：从当前发布版本发起正式运行，记一条触发。"""
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf_id, run_class="formal", actor=f"定时任务：{SCHEDULE}",
                               schedule_id=schedule_id, origin_kind="schedule")
        session.add(ScheduleFire(schedule_id=schedule_id, fire_at=utcnow(), kind="manual", status="started",
                                 run_id=run.id))
        await session.commit()
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]
    return row


def walk(tree):
    for item in tree:
        yield item
        yield from walk(item.get("children") or [])


def find(vm, cid):
    return next((n for n in walk(vm["tree"]) if n["id"] == cid), None)


async def create_app(client, spec: dict, name: str) -> str:
    res = await client.post("/api/apps", json={"name": name, "spec": spec})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_dashboard_app_on_a_schedule(client, source):
    from app.engine.launch import LaunchRefused, launch_run

    name, gmv = source
    v1 = weekly(name, "v1")
    async with SessionLocal() as session:
        wf = Workflow(name=f"周销售-{uuid.uuid4().hex[:4]}", graph=v1)
        session.add(wf)
        await session.flush()
        sch = Schedule(name=SCHEDULE, workflow_id=wf.id, cron="0 10 * * 1", timezone="Asia/Shanghai",
                       run_class="formal", enabled=True)
        session.add(sch)
        await session.commit()
        wf_id, wf_name, sch_id = wf.id, wf.name, sch.id
    hash1 = await publish_version(wf_id, 1, v1)
    await fire(wf_id, sch_id)
    second = await fire(wf_id, sch_id)

    # ---- 派生、发布 ----
    derived = (await client.post("/api/apps/derive", json={"workflow_id": wf_id, "version": 1})).json()["spec"]
    assert derived["views"]["latest"]["source"]["from"] == f"schedule:{sch_id}"
    spec = derived
    assert apply_app_op(spec, {"op": "app_add", "parent": "home", "component": {
        "id": "warn", "type": "notice", "props": {"text": "本期为降档出具"}, "visible": "run.tier != 'formal'",
        "scope": "latest"}}).changed
    app_id = await create_app(client, spec, "周销售看板")
    published = (await client.post(f"/api/apps/{app_id}/publish", json={"note": "第一版"})).json()
    assert published["ok"] and published["version"] == 1 and published["level"] == "governed", published
    old_app = await create_app(client, derived, "周销售看板（旧）")
    assert (await client.post(f"/api/apps/{old_app}/publish", json={})).json()["ok"]

    # ---- 印章、指标、证据、遮罩 ----
    vm = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert vm["format"] == "agentlab.view/1" and vm["problems"] == [], vm["problems"]
    assert vm["app"] == {"id": app_id, "name": "周销售看板", "version": 1, "level": "governed", "needs_upgrade": False}
    src = vm["sources"]["latest"]
    assert src["state"] == "ok" and src["run_id"] == second.id
    stamp = src["stamp"]
    assert stamp["as_of_text"].startswith("截至 ") and "统计期 week 2026-W40" in stamp["as_of_text"]
    assert (stamp["pin"], stamp["graph_hash"]) == (f"{wf_name} v1", hash1[:8])
    assert stamp["calibers"] == [{"name": "周销售口径", "version": "v1"}]
    assert (stamp["tier"], stamp["tier_text"], stamp["sealed"]) == ("formal", "完整出具", True)
    assert stamp["from"] == f"定时任务「{SCHEDULE}」第 2 次" and "notes" not in stamp
    assert find(vm, "home_stamp")["data"]["stamp"] == stamp
    assert find(vm, "warn") is None, "完整出具时降档提示不该下发"
    metric = find(vm, "home_m_card_gmv")["data"]
    assert metric["text"] == render_number(gmv, unit="元", decimals=2) and metric["state"] == "ok"
    evidence = metric["evidence"]
    assert (evidence["run_id"], evidence["kind"], evidence["node"], evidence["metric"]) == (second.id, "metric", "card", "gmv")
    people = find(vm, "home_table_people")["data"]
    assert people["masked"] == ["phone"] and people["columns"] == ["name", "phone"]
    assert all(row[1] == "已遮罩" and row[0] != "已遮罩" for row in people["rows"]) and len(people["rows"]) == 3
    report = find(vm, "home_report_write")["data"]
    assert report["state"] == "ok" and report["evidence"]["kind"] == "report" and report["doc_artifact"]

    chain = (await client.get(f"/api/runs/{second.id}/evidence/metrics/card/gmv")).json()
    assert chain["rendered"] == metric["text"] and chain["eid"] == evidence["eid"] and chain["seal"]["covered"]
    steps = [s["step"] for s in chain["chain"]]
    assert steps[0] == "metric" and "input" in steps and steps[-1] == "query"
    query = chain["chain"][-1]
    assert query["columns"] == ["gmv", "orders"] and query["highlight"]["cells"] == [[0, "gmv"]]
    missing = await client.get(f"/api/runs/{second.id}/evidence/metrics/card/nope")
    assert missing.status_code == 404 and missing.json()["code"] == "evidence_metric_not_found"

    # ---- 发布 v2、定时任务改跑 v2：应用照实写明固定在 v1 ----
    hash2 = await publish_version(wf_id, 2, weekly(name, "v2"))
    third = await fire(wf_id, sch_id)
    assert third.version == 2
    vm = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert vm["sources"]["latest"]["run_id"] == second.id
    assert vm["sources"]["latest"]["stamp"]["notes"] == ["定时任务已改为运行 v2，本应用固定在 v1"]
    # F1：固定在旧的发布版也能发起正式运行
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf_id, run_class="formal", version=1, expect_hash=hash1,
                               actor="测试")
    await wait(run.id)

    # ---- 钉住项升到 v2：必须处置 ----
    detail = (await client.get(f"/api/apps/{app_id}")).json()
    draft = detail["draft"]
    draft["pins"][0].update(version=2, graph_hash=hash2)
    assert apply_app_op(draft, {"op": "app_add", "parent": "home", "index": 1, "component": {
        "id": "trend", "type": "series", "props": {"label": "销售额趋势"},
        "bind": {"view": "latest", "series": "card.gmv", "last": 6}}}).changed
    assert (await client.patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    refused = (await client.post(f"/api/apps/{app_id}/publish", json={})).json()
    assert refused["ok"] is False and refused["needs_disposition"] == [{"alias": "main", "from": 1, "to": 2}]
    assert "app.upgrade_policy" in {i["code"] for i in refused["issues"]}
    upgraded = (await client.post(f"/api/apps/{app_id}/publish", json={"dispositions": {"main": "incomparable"}})).json()
    assert upgraded["ok"] and upgraded["version"] == 2, upgraded
    versions = (await client.get(f"/api/apps/{app_id}/versions")).json()["items"]
    assert [(v["version"], v["dispositions"]) for v in versions] == [(2, {"main": "incomparable"}), (1, {})]

    vm = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert vm["sources"]["latest"]["run_id"] == third.id
    stamp = vm["sources"]["latest"]["stamp"]
    assert stamp["pin"] == f"{wf_name} v2" and stamp["from"] == f"定时任务「{SCHEDULE}」第 3 次" and "notes" not in stamp
    trend = find(vm, "trend")["data"]
    assert trend["mode"] == "incomparable" and trend["unit"] == "元"
    assert [(s["version"], len(s["points"])) for s in trend["series"]] == [(1, 2), (2, 1)]
    assert trend["series"][0]["points"][-1]["run_id"] == second.id
    assert all(p["evidence"]["kind"] == "metric" for s in trend["series"] for p in s["points"])
    assert trend["breaks"] == [{"after_run": second.id, "from": 1, "to": 2, "text": "口径从 v1 升到 v2，前后不可比"}]

    # ---- 退役 ----
    current = await client.post(f"/api/workflows/{wf_id}/versions/2/retire", json={})
    assert current.status_code == 409 and current.json()["code"] == "version_retire_current"
    await publish_version(wf_id, 3, weekly(name, "v3"))
    res = await client.post(f"/api/workflows/{wf_id}/versions/1/retire", json={"note": "改按支付日"})
    assert res.status_code == 200, res.text
    assert [a["app_id"] for a in res.json()["affected_apps"]] == [old_app]
    apps = {a["id"]: a for a in (await client.get("/api/apps")).json()["items"]}
    assert apps[old_app]["needs_upgrade"] is True and apps[app_id]["needs_upgrade"] is False
    assert apps[app_id]["my_role"] == "manage"
    old_vm = (await client.get(f"/api/apps/{old_app}/pages/home")).json()
    assert old_vm["app"]["needs_upgrade"] is True
    assert old_vm["sources"]["latest"]["run_id"] == second.id
    assert old_vm["sources"]["latest"]["stamp"]["notes"] == ["固定的 v1 已退役，这里保留最后一次的数据；应用需要升级"]
    async with SessionLocal() as session:
        with pytest.raises(LaunchRefused) as caught:
            await launch_run(session, workflow_id=wf_id, run_class="formal", version=1, actor="测试")
    assert caught.value.code == "run_version_retired"
    lint = (await client.post(f"/api/apps/{old_app}/lint")).json()
    assert "app.pin_retired" in {i["code"] for i in lint["issues"]}

    # ---- 可见性表达式里的算术 ----
    draft = (await client.get(f"/api/apps/{app_id}")).json()["draft"]
    assert apply_app_op(draft, {"op": "app_update", "id": "warn", "visible": "metric.card.gmv * 2 > 0"}).changed
    await client.patch(f"/api/apps/{app_id}", json={"draft": draft})
    issues = (await client.post(f"/api/apps/{app_id}/lint")).json()["issues"]
    arith = [i for i in issues if i["code"] == "app.expr_arith"]
    assert len(arith) == 1 and arith[0]["level"] == "error" and arith[0]["path"].endswith(".visible")
    assert (await client.post(f"/api/apps/{app_id}/publish", json={})).json()["ok"] is False


async def test_a_draft_preview_reads_sealed_runs_without_launching(client, source):
    name, _ = source
    graph = weekly(name, "v1")
    async with SessionLocal() as session:
        wf = Workflow(name=f"周销售-{uuid.uuid4().hex[:4]}", graph=graph)
        session.add(wf)
        await session.commit()
        wf_id = wf.id
    await publish_version(wf_id, 1, graph)
    spec = (await client.post("/api/apps/derive", json={"workflow_id": wf_id, "version": 1})).json()["spec"]
    spec["views"]["od"] = {"source": {"kind": "on_demand", "pin": "main", "ttl_s": 600, "auto": True}}
    spec["pages"][0]["body"].append({"id": "od_stamp", "type": "stamp", "bind": {"view": "od"}})
    app_id = await create_app(client, spec, "预览")
    async with SessionLocal() as session:
        before = len((await session.execute(select(Run.id).where(Run.workflow_id == wf_id))).all())
    vm = (await client.get(f"/api/apps/{app_id}/pages/home", params={"draft": 1})).json()
    assert vm["app"]["version"] == 0 and vm["sources"]["latest"]["state"] == "empty"
    assert vm["sources"]["od"]["state"] == "empty"
    assert {p["code"] for p in vm["problems"]} == {"view.source_empty"}
    async with SessionLocal() as session:
        after = len((await session.execute(select(Run.id).where(Run.workflow_id == wf_id))).all())
    assert after == before, "草稿预览不该发起运行"
    # 没发布的应用不能按发布版打开
    res = await client.get(f"/api/apps/{app_id}/pages/home")
    assert res.status_code == 409 and res.json()["code"] == "app_unpublished"
