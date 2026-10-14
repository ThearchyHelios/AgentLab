"""定时任务接口（api/schedules.py），按前后端约定的形状：

- Schedule：{id, name, workflow_id, workflow_name, cron, timezone, run_class, input_template, enabled, overlap,
  misfire_grace_s, webhook: {format, url_hint, has_secret, events} | null, next_run_at, last_fire, description_text,
  created_by, created_at, updated_at, version}；Webhook 地址和密钥加密存放，接口只给主机和末 4 位
- Fire：{id, fire_at, kind, status, run_id, run_status, reason, notified: [{event, status, at}], created_at}
- 创建、修改时校验 cron、时区、工作流存在、正式运行要有发布版本、输入模板；不对回 422 {detail（中文）, field}，
  说明里不回显 Python 的值；群机器人的地址要配它平台的格式
- PATCH 带 version，对不上回 409（两人同时保存时靠条件 UPDATE）；webhook.url 省略表示不改，空串表示删除
  Webhook；看值变没变：整份表单回写不重新校验没改的工作流，也不动到点了的下一次触发
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

from app.core.crypto import decrypt, encrypt
from app.db.base import SessionLocal
from app.db.models import Run, Schedule, ScheduleFire, WebhookDelivery, Workflow, WorkflowVersion
from app.api import schedules as schedules_api
from app.engine.runner import run_manager
from app.main import app
from app.scheduler import loop, webhook

UTC = timezone.utc
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/7c6b5a49-dddd-eeee-ffff-1234567890ab"
SECRET = "SECdemo-api-not-real-7"
SCHEDULE_KEYS = {"id", "name", "workflow_id", "workflow_name", "cron", "timezone", "run_class", "input_template",
                 "enabled", "overlap", "misfire_grace_s", "webhook", "next_run_at", "last_fire", "description_text",
                 "created_by", "created_at", "updated_at", "version"}
FIRE_KEYS = {"id", "fire_at", "kind", "status", "run_id", "run_status", "reason", "notified", "compare", "created_at"}


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


async def test_concurrent_saves_of_the_same_version_conflict(client, monkeypatch):
    """两个人同时读到第 1 版、同时保存：读后比对都过了，只有先写进去的那份生效，后到的回 409、不覆盖先到的。
    这一条靠 UPDATE … WHERE version = 读到的版本 守住，串行的两次请求测不到它"""
    wf, _ = await workflow(fields=(DATE_FIELD, {"name": "region"}))
    made = await create(client, wf)
    sid = made["id"]
    real = schedules_api._check_workflow

    async def other_save_lands_first(*args, **kwargs):
        # 本次请求读完、比对完版本之后，另一个人的保存先提交了
        async with SessionLocal() as session:
            await session.execute(Schedule.__table__.update().where(Schedule.id == sid)
                                  .values(name="先到的", version=Schedule.version + 1))
            await session.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(schedules_api, "_check_workflow", other_save_lands_first)
    template = {"stat_date": "{{ schedule.yesterday }}", "region": "华东"}
    res = await client.patch(f"/api/schedules/{sid}", json={"name": "后到的", "input_template": template, "version": 1})
    assert res.status_code == 409 and "刷新" in res.json()["detail"]
    got = (await client.get(f"/api/schedules/{sid}")).json()
    assert (got["name"], got["input_template"], got["version"]) == ("先到的", made["input_template"], 2)


EDITABLE = ("name", "workflow_id", "cron", "timezone", "run_class", "input_template", "enabled", "overlap",
            "misfire_grace_s")


def full_form(made: dict, **over) -> dict:
    """编辑表单整份回写：没改的字段也原样带上"""
    return {**{k: made[k] for k in EDITABLE}, "version": made["version"], **over}


async def test_full_form_save_can_pause_after_the_workflow_is_gone(client):
    """工作流删掉了、或撤了发布：整份带回来、只把启用关掉，也要能停下来。没改的工作流、运行类别、输入不重新校验"""
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    async with SessionLocal() as session:
        await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id == wf))
        await session.execute(delete(Workflow).where(Workflow.id == wf))
        await session.commit()
    res = await client.patch(f"/api/schedules/{made['id']}", json=full_form(made, enabled=False))
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is False and res.json()["next_run_at"] is None
    # 真改了工作流相关的项，照样校验
    res = await client.patch(f"/api/schedules/{made['id']}",
                             json={"input_template": {"stat_date": "{{ schedule.today }}"}, "version": 2})
    assert res.status_code == 422 and res.json()["field"] == "workflow_id"


async def test_full_form_save_keeps_a_due_fire(client):
    """到点了、这一拍还没来：整份带回、只改名字，下一次触发时间不动，这一拍照常发起。以前只要带了 cron、时区、
    启用就从现在重算，这一次连记录都没有就丢了"""
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None, cron="* * * * *")
    due = datetime.now(UTC).replace(second=0, microsecond=0)
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == made["id"]).values(next_run_at=due))
        await session.commit()
    res = await client.patch(f"/api/schedules/{made['id']}", json=full_form(made, name="每分钟早报"))
    assert res.status_code == 200, res.text
    assert datetime.fromisoformat(res.json()["next_run_at"]) == due
    [fire] = await loop.fire_due()
    assert (fire.status, fire.fire_at) == ("started", due)
    await settle(fire.run_id)


async def test_changed_cron_keeps_a_due_fire_it_still_covers(client):
    """改了 cron：从改之前那个到点的时刻接着算。新表达式也覆盖那一刻（每分钟 → 工作日每分钟）就还发起这一次；
    只属于旧表达式的到点时刻不再发起"""
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None, cron="* * * * *")
    due = datetime.now(UTC).replace(second=0, microsecond=0)
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == made["id"]).values(next_run_at=due))
        await session.commit()
    weekday = (due.weekday() + 1) % 7      # cron 的周：0 = 周日
    covered = await client.patch(f"/api/schedules/{made['id']}",
                                 json={"cron": f"* * * * {weekday}", "timezone": "UTC", "version": 1})
    assert datetime.fromisoformat(covered.json()["next_run_at"]) == due
    other = (weekday + 1) % 7
    moved = await client.patch(f"/api/schedules/{made['id']}", json={"cron": f"* * * * {other}", "version": 2})
    assert datetime.fromisoformat(moved.json()["next_run_at"]) > datetime.now(UTC)


async def test_patch_webhook_keeps_or_removes_the_address(client):
    wf, _ = await workflow()
    made = await create(client, wf)
    sid = made["id"]
    # 不带 url：地址不变，只改事件（飞书群机器人的地址只能用飞书格式，格式对不上的见 test_known_bot_address_…）
    res = await client.patch(f"/api/schedules/{sid}", json={
        "webhook": {"format": "feishu", "events": ["awaiting_approval"]}, "version": 1})
    assert res.status_code == 200, res.text
    assert res.json()["webhook"] == {"format": "feishu", "url_hint": "open.feishu.cn/…90ab", "has_secret": True,
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
    ({"overlap": "queue"}, "overlap", "跳过"),
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


@pytest.mark.parametrize("over, field, says, never", [
    # 表单清空、没选时传 null：说「未填写」，不回显 Python 的 None
    ({"run_class": None}, "run_class", "未填写", "None"),
    ({"misfire_grace_s": None}, "misfire_grace_s", "未填写", "None"),
    ({"enabled": None}, "enabled", "未填写", "None"),
    ({"misfire_grace_s": True}, "misfire_grace_s", "宽限", "True"),
    # 列表不原样回显，点出不认识的那一项
    ({"webhook": {"format": "feishu", "url": HOOK, "events": ["finished", "done"]}}, "webhook", "「done」", "['"),
    # 格式写中文名，不列英文枚举
    ({"webhook": {"format": "slack", "url": HOOK}}, "webhook", "通用、飞书、企业微信、钉钉", "generic"),
])
async def test_invalid_values_are_described_without_python_reprs(client, over, field, says, never):
    wf, _ = await workflow()
    res = await client.post("/api/schedules", json=payload(wf, **over))
    assert res.status_code == 422 and res.json()["field"] == field
    detail = res.json()["detail"]
    assert says in detail and never not in detail, detail


FEISHU_BOT = HOOK
WECOM_BOT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=0a1b2c3d-4444-5555-6666-777788889999"
DINGTALK_BOT = "https://oapi.dingtalk.com/robot/send?access_token=0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("fmt, url, platform", [
    ("generic", FEISHU_BOT, "飞书"),
    ("dingtalk", FEISHU_BOT, "飞书"),
    ("feishu", WECOM_BOT, "企业微信"),
    ("generic", DINGTALK_BOT, "钉钉"),
])
async def test_known_bot_address_needs_its_own_format(client, fmt, url, platform):
    """群机器人只认自己平台的消息格式：格式选错了，平台回错误码、群里收不到。保存时就说出来"""
    wf, _ = await workflow()
    hook = {"format": fmt, "url": url, "events": ["failed"]}
    res = await client.post("/api/schedules", json=payload(wf, webhook=hook))
    assert res.status_code == 422 and res.json()["field"] == "webhook"
    assert f"{platform}群机器人" in res.json()["detail"] and f"「{platform}」" in res.json()["detail"]
    assert "0123456789abcdef" not in res.text and "0a1b2c3d" not in res.text


async def test_format_follows_the_address_when_left_out(client):
    wf, _ = await workflow()
    made = await create(client, wf, webhook={"url": WECOM_BOT, "events": ["failed"]})
    assert made["webhook"]["format"] == "wecom"
    custom = await create(client, wf, webhook={"url": "https://hooks.example.com/agentlab/inbox-7f3a"})
    assert custom["webhook"]["format"] == "generic"
    # 改格式（地址不变）也要和地址对得上；换了地址、没给格式，按新地址认
    res = await client.patch(f"/api/schedules/{made['id']}", json={"webhook": {"format": "feishu"}, "version": 1})
    assert res.status_code == 422 and "企业微信群机器人" in res.json()["detail"]
    res = await client.patch(f"/api/schedules/{made['id']}", json={"webhook": {"url": DINGTALK_BOT}, "version": 1})
    assert res.status_code == 200 and res.json()["webhook"]["format"] == "dingtalk"


async def test_old_mismatch_does_not_block_unrelated_edits(client):
    """库里已有对不上的设置（这条校验之前存的）：只改名字、停用，不因为 Webhook 被拦"""
    wf, _ = await workflow()
    made = await create(client, wf)
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == made["id"])
                              .values(webhook_format="generic"))
        await session.commit()
    res = await client.patch(f"/api/schedules/{made['id']}", json={
        "enabled": False, "webhook": {"format": "generic", "events": ["finished", "failed"]}, "version": 1})
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("url", [
    "https://hooks.example.com:abc/inbox-7f3a", "https://hooks.exa\u200bmple.com/inbox-7f3a"])
async def test_unsendable_address_is_refused_on_save(client, url):
    wf, _ = await workflow()
    res = await client.post("/api/schedules", json=payload(wf, webhook={"format": "generic", "url": url}))
    assert res.status_code == 422 and res.json()["field"] == "webhook"
    assert res.json()["detail"].startswith("Webhook 地址")


async def test_webhook_test_with_a_saved_unsendable_address_answers_in_chinese(client):
    """校验加严之前存进库的地址：测试接口回 200 和中文说明，不是 500"""
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == made["id"]).values(
            webhook_format="generic", webhook_url=encrypt("https://hooks.example.com:abc/inbox-7f3a"),
            webhook_events=["failed"]))
        await session.commit()
    res = await client.post(f"/api/schedules/{made['id']}/webhook/test")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False and body["http_status"] is None
    assert "hooks.example.com" in body["message"] and "7f3a" not in body["message"]


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
    assert len(items) == 16 and all(set(i) == {"name", "label", "example", "note"} for i in items)
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
    assert f"上一次运行 #{body['run_id'][:6]} " in second["fire"]["reason"]
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


async def test_a_fire_being_launched_is_not_shown_as_failed(client, monkeypatch):
    """发起要先记一条占位再调 launch_run。以前占位写成「未能发起：服务在发起运行前停止」，这期间来读列表、触发
    历史的会闪出一次假的失败；进程还在、只是还没发起完的，按已发起给（run_id 还是空的）"""
    from app.engine import launch

    real = launch.launch_run
    halfway = asyncio.Event()
    go_on = asyncio.Event()

    async def slow(*args, **kwargs):
        halfway.set()
        await go_on.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(launch, "launch_run", slow)
    wf, _ = await workflow()
    made = await create(client, wf, webhook=None)
    pressing = asyncio.create_task(client.post(f"/api/schedules/{made['id']}/run"))
    await asyncio.wait_for(halfway.wait(), timeout=5)
    last = (await client.get("/api/schedules")).json()["items"][0]["last_fire"]
    assert (last["status"], last["reason"], last["run_id"]) == ("started", None, None)
    [item] = (await client.get(f"/api/schedules/{made['id']}/fires")).json()["items"]
    assert (item["status"], item["reason"]) == ("started", None)
    go_on.set()
    done = (await pressing).json()
    assert done["fire"]["status"] == "started" and done["run_id"]
    await settle(done["run_id"])


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
    bad = await client.get(f"/api/schedules/{sid}/fires", params={"limit": "²"})
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
    assert res.json() == {"ok": True, "http_status": 200, "message": "请到接收端确认已收到"}
    assert str(seen[0].url) == HOOK and "sign" in json.loads(seen[0].content)
    bare = await create(client, wf, webhook=None)
    res = await client.post(f"/api/schedules/{bare['id']}/webhook/test")
    assert res.status_code == 422 and res.json()["field"] == "webhook" and "Webhook" in res.json()["detail"]
