"""定时任务接口（api/schedules.py），按前后端约定的形状：

- Schedule：{id, name, workflow_id, workflow_name, cron, timezone, run_class, input_template, enabled, overlap,
  misfire_grace_s, webhook: {format, url_hint, has_secret, events} | null, next_run_at, last_fire, description_text,
  created_by, created_at, updated_at, version}；Webhook 地址和密钥加密存放，接口只给主机和末 4 位
- Fire：{id, fire_at, kind, status, run_id, run_status, reason, notified: [{event, status, at}], created_at}
- 创建、修改时校验 cron、时区、工作流存在、正式运行要有发布版本、输入模板；不对回 422 {detail（中文）, field}
- PATCH 带 version，对不上回 409；webhook.url 省略表示不改，空串表示删除 Webhook
- 预览下几次触发和渲染后的输入；变量清单；立即运行（同一套重叠规则）；触发历史翻页；测试 Webhook

工作流、任务名、地址、密钥都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.crypto import decrypt
from app.db.base import SessionLocal
from app.db.models import Run, Schedule, ScheduleFire, WebhookDelivery, Workflow, WorkflowVersion
from app.api import schedules as schedules_api
from app.engine.runner import run_manager
from app.main import app
from app.scheduler import webhook

UTC = timezone.utc
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/7c6b5a49-dddd-eeee-ffff-1234567890ab"
SECRET = "SECdemo-api-not-real-7"
SCHEDULE_KEYS = {"id", "name", "workflow_id", "workflow_name", "cron", "timezone", "run_class", "input_template",
                 "enabled", "overlap", "misfire_grace_s", "webhook", "next_run_at", "last_fire", "description_text",
                 "created_by", "created_at", "updated_at", "version"}
FIRE_KEYS = {"id", "fire_at", "kind", "status", "run_id", "run_status", "reason", "notified", "created_at"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(*fields: dict) -> dict:
    return {"nodes": [node("start", "input", fields=list(fields)),
                      node("out", "output", fields=[{"name": "r", "value": "完成 {{ input.stat_date }}"}])],
            "edges": [{"source": "start", "target": "out"}]}


DATE_FIELD = {"name": "stat_date", "required": True}


async def workflow(*, published: bool = True, fields=(DATE_FIELD,)) -> tuple[str, str]:
    name = f"门店周报-{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        wf = Workflow(name=name, graph=graph(*fields), status="published" if published else "draft",
                      published_version=1 if published else None)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph(*fields)))
        await session.commit()
        return wf.id, name


def payload(wf_id: str, **over) -> dict:
    base = {"name": "周一早报", "workflow_id": wf_id, "cron": "0 9 * * 1", "timezone": "Asia/Shanghai",
            "run_class": "formal", "input_template": {"stat_date": "{{ schedule.yesterday }}"}, "enabled": True,
            "overlap": "skip", "misfire_grace_s": 3600,
            "webhook": {"format": "feishu", "url": HOOK, "secret": SECRET, "events": ["finished", "failed"]}}
    base.update(over)
    return base


async def create(client, wf_id: str, **over) -> dict:
    res = await client.post("/api/schedules", json=payload(wf_id, **over),
                            headers={"X-Actor": quote("李小明")})
    assert res.status_code == 201, res.text
    return res.json()


async def settle(run_id: str) -> None:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


# --------------------------------------------------------------------------
# 增删改查
# --------------------------------------------------------------------------


async def test_create_returns_the_agreed_shape_and_hides_the_address(client):
    wf, wf_name = await workflow()
    res = await client.post("/api/schedules", json=payload(wf), headers={"X-Actor": quote("李小明")})
    assert res.status_code == 201
    body = res.json()
    assert set(body) == SCHEDULE_KEYS
    assert body["workflow_name"] == wf_name and body["created_by"] == "李小明" and body["version"] == 1
    assert body["webhook"] == {"format": "feishu", "url_hint": "open.feishu.cn/…90ab", "has_secret": True,
                               "events": ["finished", "failed"]}
    assert body["description_text"] == "每周一 09:00（Asia/Shanghai）"
    assert body["last_fire"] is None and body["overlap"] == "skip" and body["misfire_grace_s"] == 3600
    next_at = datetime.fromisoformat(body["next_run_at"])
    assert next_at.utcoffset().total_seconds() == 0 and next_at.astimezone(UTC).hour == 1
    assert next_at > datetime.now(UTC)
    # 地址、密钥一个字都不回到浏览器
    assert "7c6b5a49" not in res.text and SECRET not in res.text and "/hook/" not in res.text
    async with SessionLocal() as session:
        row = await session.get(Schedule, body["id"])
    assert row.webhook_url.startswith("enc::") and decrypt(row.webhook_url) == HOOK
    assert row.webhook_secret.startswith("enc::") and decrypt(row.webhook_secret) == SECRET


async def test_list_and_get(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    listed = (await client.get("/api/schedules")).json()
    assert set(listed) == {"items"} and [s["id"] for s in listed["items"]] == [made["id"]]
    got = await client.get(f"/api/schedules/{made['id']}")
    assert got.status_code == 200 and got.json() == made


async def test_patch_updates_fields_and_bumps_the_version(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    res = await client.patch(f"/api/schedules/{made['id']}",
                             json={"name": "每日早报", "cron": "30 8 * * *", "version": 1})
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["name"], body["cron"], body["version"]) == ("每日早报", "30 8 * * *", 2)
    assert body["description_text"] == "每天 08:30（Asia/Shanghai）"
    assert datetime.fromisoformat(body["next_run_at"]).astimezone(UTC).minute == 30
    # 停用：没有下一次
    paused = (await client.patch(f"/api/schedules/{made['id']}", json={"enabled": False, "version": 2})).json()
    assert paused["enabled"] is False and paused["next_run_at"] is None and paused["version"] == 3
    resumed = (await client.patch(f"/api/schedules/{made['id']}", json={"enabled": True, "version": 3})).json()
    assert resumed["next_run_at"] is not None


async def test_stale_version_is_a_conflict(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    assert (await client.patch(f"/api/schedules/{made['id']}", json={"name": "甲", "version": 1})).status_code == 200
    res = await client.patch(f"/api/schedules/{made['id']}", json={"name": "乙", "version": 1})
    assert res.status_code == 409 and "刷新" in res.json()["detail"]
    missing = await client.patch(f"/api/schedules/{made['id']}", json={"name": "丙"})
    assert missing.status_code == 422 and missing.json()["field"] == "version"
    assert (await client.get(f"/api/schedules/{made['id']}")).json()["name"] == "甲"


async def test_patch_webhook_keeps_or_removes_the_address(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    sid = made["id"]
    # 不带 url：地址不变，只改事件和格式
    res = await client.patch(f"/api/schedules/{sid}", json={
        "webhook": {"format": "dingtalk", "events": ["awaiting_approval"]}, "version": 1})
    assert res.status_code == 200, res.text
    assert res.json()["webhook"] == {"format": "dingtalk", "url_hint": "open.feishu.cn/…90ab", "has_secret": True,
                                     "events": ["awaiting_approval"]}
    async with SessionLocal() as session:
        assert decrypt((await session.get(Schedule, sid)).webhook_url) == HOOK
    # 密钥写空串：去掉签名
    res = await client.patch(f"/api/schedules/{sid}", json={"webhook": {"secret": ""}, "version": 2})
    assert res.json()["webhook"]["has_secret"] is False
    # url 写空串：删除整个 Webhook
    res = await client.patch(f"/api/schedules/{sid}", json={"webhook": {"url": ""}, "version": 3})
    assert res.json()["webhook"] is None
    async with SessionLocal() as session:
        row = await session.get(Schedule, sid)
    assert (row.webhook_url, row.webhook_secret, row.webhook_format) == (None, None, None)
    # 没有地址时只给格式不行
    res = await client.patch(f"/api/schedules/{sid}", json={"webhook": {"format": "wecom"}, "version": 4})
    assert res.status_code == 422 and res.json()["field"] == "webhook"
    # 重新配上
    res = await client.patch(f"/api/schedules/{sid}", json={
        "webhook": {"format": "wecom", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-0000-9f9f"},
        "version": 4})
    assert res.json()["webhook"] == {"format": "wecom", "url_hint": "qyapi.weixin.qq.com/…9f9f", "has_secret": False,
                                     "events": ["finished", "failed", "awaiting_approval"]}


async def test_webhook_null_removes_it(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    res = await client.patch(f"/api/schedules/{made['id']}", json={"webhook": None, "version": 1})
    assert res.json()["webhook"] is None


async def test_delete(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    sid = made["id"]
    ran = (await client.post(f"/api/schedules/{sid}/run")).json()
    await settle(ran["run_id"])
    res = await client.delete(f"/api/schedules/{sid}")
    assert res.status_code == 204
    gone = await client.get(f"/api/schedules/{sid}")
    assert gone.status_code == 404 and gone.json()["detail"] == "定时任务不存在，可能已被删除"
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduleFire)
                                    .where(ScheduleFire.schedule_id == sid)) == 0
        # 运行记录留着，来源 id 也留着：它是追溯凭证
        assert (await session.get(Run, ran["run_id"])).schedule_id == sid


@pytest.mark.parametrize("method, path", [
    ("get", ""), ("patch", ""), ("delete", ""), ("post", "/run"), ("get", "/fires"), ("post", "/webhook/test")])
async def test_unknown_schedule_is_404_in_chinese(client, method, path):
    kwargs = {"json": {"version": 1}} if method == "patch" else {}
    res = await getattr(client, method)(f"/api/schedules/nope{uuid.uuid4().hex[:6]}{path}", **kwargs)
    assert res.status_code == 404 and res.json()["detail"] == "定时任务不存在，可能已被删除"


# --------------------------------------------------------------------------
# 校验：422 {detail, field}
# --------------------------------------------------------------------------


@pytest.mark.parametrize("over, field, fragment", [
    ({"name": "  "}, "name", "名称"),
    ({"cron": "0 25 * * *"}, "cron", "「时」"),
    ({"cron": "0 0 30 2 *"}, "cron", "永远不会触发"),
    ({"timezone": "Mars/Olympus"}, "timezone", "时区"),
    ({"run_class": "weekly"}, "run_class", "正式运行"),
    ({"input_template": {"stat_date": "{{ schedule.yesterdy }}"}}, "input_template", "schedule.yesterdy"),
    ({"input_template": {"stat_dat": "{{ schedule.yesterday }}"}}, "input_template", "stat_dat"),
    ({"input_template": {}}, "input_template", "必填"),
    ({"misfire_grace_s": -1}, "misfire_grace_s", "宽限"),
    ({"misfire_grace_s": True}, "misfire_grace_s", "宽限"),
    ({"overlap": "queue"}, "overlap", "skip"),
    ({"enabled": "yes"}, "enabled", "启用"),
    ({"webhook": {"format": "slack", "url": HOOK}}, "webhook", "格式"),
    ({"webhook": {"format": "feishu", "url": "open.feishu.cn/hook"}}, "webhook", "Webhook 地址"),
    ({"webhook": {"format": "feishu", "url": HOOK, "events": ["done"]}}, "webhook", "通知事件"),
    ({"workflow_id": ""}, "workflow_id", "工作流"),
])
async def test_invalid_create_is_422_with_field(client, over, field, fragment):
    wf, _ = await workflow()
    res = await client.post("/api/schedules", json=payload(wf, **over))
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["field"] == field and fragment in body["detail"], body


async def test_missing_workflow_and_unpublished_formal(client):
    res = await client.post("/api/schedules", json=payload(f"gone{uuid.uuid4().hex[:6]}"))
    assert res.status_code == 422 and res.json()["field"] == "workflow_id" and "不存在" in res.json()["detail"]
    wf, name = await workflow(published=False)
    res = await client.post("/api/schedules", json=payload(wf))
    assert res.status_code == 422 and res.json()["field"] == "run_class"
    assert f"「{name}」还没有发布版本" in res.json()["detail"]
    # 探索运行不要求发布版本
    res = await client.post("/api/schedules", json=payload(wf, run_class="exploratory"))
    assert res.status_code == 201


async def test_non_json_body_is_422_in_chinese(client):
    res = await client.post("/api/schedules", content=b"not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 422 and "JSON" in res.json()["detail"]
    res = await client.post("/api/schedules", json=["列表"])
    assert res.status_code == 422 and "JSON" in res.json()["detail"]


async def test_webhook_with_empty_url_on_create_means_none(client):
    wf, _ = await workflow()
    made = await create(client, wf, webhook={"format": "feishu", "url": "", "events": ["failed"]})
    assert made["webhook"] is None


# --------------------------------------------------------------------------
# 预览、变量
# --------------------------------------------------------------------------


async def test_preview(client):
    res = await client.post("/api/schedules/preview", json={
        "cron": "0 9 * * *", "timezone": "Asia/Shanghai", "n": 3,
        "input_template": {"stat_date": "{{ schedule.yesterday }}", "since": "{{ schedule.last_fire_time }}"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"description_text", "next", "inputs"}
    assert body["description_text"] == "每天 09:00（Asia/Shanghai）"
    moments = [datetime.fromisoformat(t) for t in body["next"]]
    assert len(moments) == 3 and all(m.utcoffset().total_seconds() == 0 for m in moments)
    assert all((b - a).total_seconds() == 86400 for a, b in zip(moments, moments[1:]))
    first_local = moments[0].astimezone(ZoneInfo("Asia/Shanghai"))
    assert body["inputs"]["stat_date"] == (first_local.date() - timedelta(days=1)).isoformat()
    assert body["inputs"]["since"].endswith("09:00:00+08:00")


async def test_preview_defaults_to_five(client):
    body = (await client.post("/api/schedules/preview", json={"cron": "*/10 * * * *", "timezone": "UTC"})).json()
    assert len(body["next"]) == 5 and body["inputs"] == {}


@pytest.mark.parametrize("given, field", [
    ({"cron": "bad", "timezone": "UTC"}, "cron"),
    ({"cron": "0 9 * * *", "timezone": "Nowhere/City"}, "timezone"),
    ({"cron": "0 9 * * *", "timezone": "UTC", "input_template": {"d": "{{ schedule.today"}}, "input_template"),
])
async def test_preview_errors(client, given, field):
    res = await client.post("/api/schedules/preview", json=given)
    assert res.status_code == 422 and res.json()["field"] == field
    assert any("一" <= ch <= "鿿" for ch in res.json()["detail"])


async def test_preview_checks_field_names_against_the_workflow_but_not_missing_ones(client):
    wf, _ = await workflow(fields=(DATE_FIELD, {"name": "region"}))
    ok = await client.post("/api/schedules/preview", json={
        "cron": "0 9 * * *", "timezone": "UTC", "workflow_id": wf, "input_template": {"region": "华东"}})
    assert ok.status_code == 200
    bad = await client.post("/api/schedules/preview", json={
        "cron": "0 9 * * *", "timezone": "UTC", "workflow_id": wf, "input_template": {"regoin": "华东"}})
    assert bad.status_code == 422 and bad.json()["field"] == "input_template" and "regoin" in bad.json()["detail"]


async def test_variables(client):
    res = await client.get("/api/schedules/variables")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 10 and all(set(i) == {"name", "label", "example"} for i in items)
    assert {"name": "schedule.yesterday", "label": "昨天（YYYY-MM-DD）"}.items() <= next(
        i for i in items if i["name"] == "schedule.yesterday").items()
    assert (await client.get("/api/schedules/variables", params={"timezone": "Nowhere/City"})).status_code == 422


# --------------------------------------------------------------------------
# 立即运行、触发历史
# --------------------------------------------------------------------------


async def test_run_now_and_overlap(client, monkeypatch):
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    res = await client.post(f"/api/schedules/{made['id']}/run", headers={"X-Actor": quote("王芳")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"fire", "run_id"} and set(body["fire"]) == FIRE_KEYS
    fire = body["fire"]
    assert (fire["kind"], fire["status"], fire["run_id"]) == ("manual", "started", body["run_id"])
    assert fire["run_status"] in ("queued", "running", "succeeded")
    async with SessionLocal() as session:
        run = await session.get(Run, body["run_id"])
    assert run.schedule_id == made["id"] and run.started_by == "王芳" and run.run_class == "formal"
    assert set(run.input) == {"stat_date"}
    # 上一次还没结束（这里把它改成停在审批）：同一套重叠规则
    await settle(body["run_id"])
    async with SessionLocal() as session:
        await session.execute(Run.__table__.update().where(Run.id == body["run_id"]).values(status="interrupted"))
        await session.commit()
    # 换到下一秒再点（同一秒内连点交回同一条，见下一个用例）
    monkeypatch.setattr(schedules_api, "utcnow", lambda: datetime.now(UTC) + timedelta(seconds=2))
    second = (await client.post(f"/api/schedules/{made['id']}/run")).json()
    assert (second["fire"]["status"], second["run_id"]) == ("skipped_overlap", None)
    assert body["run_id"][:8] in second["fire"]["reason"]
    # 运行列表能按定时任务筛
    listed = (await client.get("/api/runs", params={"schedule_id": made["id"]})).json()
    assert [r["id"] for r in listed] == [body["run_id"]]


async def test_double_click_in_the_same_second_returns_the_same_fire(client, monkeypatch):
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    moment = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(schedules_api, "utcnow", lambda: moment)
    first, second = [(await client.post(f"/api/schedules/{made['id']}/run")).json() for _ in range(2)]
    assert first["fire"]["id"] == second["fire"]["id"] and first["run_id"] == second["run_id"]
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Run).where(Run.schedule_id == made["id"])) == 1
    await settle(first["run_id"])


async def test_run_now_refused_for_unpublished_formal(client):
    wf, name = await workflow()
    made = await create(client, wf, webhook=None)
    async with SessionLocal() as session:
        await session.execute(Workflow.__table__.update().where(Workflow.id == wf).values(published_version=None))
        await session.commit()
    body = (await client.post(f"/api/schedules/{made['id']}/run")).json()
    assert (body["fire"]["status"], body["run_id"]) == ("failed_to_start", None)
    assert body["fire"]["reason"] == f"「{name}」还没有发布版本，请先在编排页发布"


async def test_fire_history_pages_and_last_fire(client):
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    sid = made["id"]
    async with SessionLocal() as session:
        for i in range(5):
            session.add(ScheduleFire(schedule_id=sid, fire_at=datetime(2026, 9, 1 + i, 1, 0, tzinfo=UTC),
                                     kind="scheduled", status="skipped_overlap", reason=f"第 {i} 次",
                                     created_at=datetime(2026, 9, 1 + i, 1, 0, 1, tzinfo=UTC)))
        await session.commit()
        newest = (await session.execute(select(ScheduleFire).where(ScheduleFire.schedule_id == sid)
                                        .order_by(ScheduleFire.created_at.desc()))).scalars().first()
        session.add(WebhookDelivery(schedule_id=sid, fire_id=newest.id, event="failed", status="ok", attempts=1,
                                    http_status=200, created_at=datetime(2026, 9, 5, 1, 0, 5, tzinfo=UTC)))
        await session.commit()
    page1 = (await client.get(f"/api/schedules/{sid}/fires", params={"limit": 2})).json()
    assert [f["reason"] for f in page1["items"]] == ["第 4 次", "第 3 次"] and page1["next_before"]
    assert page1["items"][0]["notified"] == [{"event": "failed", "status": "ok", "at": "2026-09-05T01:00:05+00:00"}]
    assert page1["items"][0]["fire_at"] == "2026-09-05T01:00:00+00:00"
    page2 = (await client.get(f"/api/schedules/{sid}/fires",
                              params={"limit": 2, "before": page1["next_before"]})).json()
    assert [f["reason"] for f in page2["items"]] == ["第 2 次", "第 1 次"] and page2["next_before"]
    page3 = (await client.get(f"/api/schedules/{sid}/fires",
                              params={"limit": 2, "before": page2["next_before"]})).json()
    assert [f["reason"] for f in page3["items"]] == ["第 0 次"] and page3["next_before"] is None
    last = (await client.get(f"/api/schedules/{sid}")).json()["last_fire"]
    assert last["reason"] == "第 4 次" and set(last) == FIRE_KEYS
    bad = await client.get(f"/api/schedules/{sid}/fires", params={"limit": "many"})
    assert bad.status_code == 422 and bad.json()["field"] == "limit"
    bad = await client.get(f"/api/schedules/{sid}/fires", params={"before": "昨天"})
    assert bad.status_code == 422 and bad.json()["field"] == "before"


# --------------------------------------------------------------------------
# 测试 Webhook
# --------------------------------------------------------------------------


async def test_webhook_test_endpoint(client, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(handler))
    wf, _ = await workflow()
    made = await create(client, wf)
    res = await client.post(f"/api/schedules/{made['id']}/webhook/test")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "http_status": 200, "message": "测试消息已送达（HTTP 200）"}
    assert str(seen[0].url) == HOOK and "sign" in json.loads(seen[0].content)
    bare = await create(client, wf, webhook=None)
    res = await client.post(f"/api/schedules/{bare['id']}/webhook/test")
    assert res.status_code == 422 and res.json()["field"] == "webhook" and "Webhook" in res.json()["detail"]
