"""定时任务的 Webhook（app/scheduler/webhook.py）：运行结束、失败、停在审批时推送一次。

- 四种格式的请求体和签名：通用 JSON（配了密钥时 X-AgentLab-Signature = HMAC-SHA256(密钥, 时间戳.请求体)）、
  飞书文本（timestamp + sign，sign 是以「时间戳\\n密钥」为键的 HMAC-SHA256）、企业微信 markdown（key 在地址里）、
  钉钉 markdown（地址上加 timestamp 和 sign）
- 通知里有任务名、状态、出具档位、缺口条数、运行链接（对外访问地址 + /runs/{id}）、封存哈希前 12 位、开始结束
  时间；不放报告里的具体数字，不放失败的原始报错
- 每一拍查已发起的触发：运行状态变了、任务订阅了这个事件、这个状态还没处理过才推送；停在审批只在确有待审批时
  算；结束的运行等封存好再推；没订阅、没配 Webhook 的也记下已处理，之后再配上不会补发旧的
- 失败重试 3 次（4xx 不重试），每次推送记一条 WebhookDelivery；日志和错误里只有主机名，没有地址、key、
  access_token、密钥（httpx 自己的请求日志也一样）

用 httpx.MockTransport 接住请求，不连外网。任务名、地址、密钥、数字都是虚构的。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
import pytest
from sqlalchemy import delete, select

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Approval, Run, Schedule, ScheduleFire, Setting, WebhookDelivery
from app.scheduler import webhook

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 1, 2, 3, tzinfo=UTC)
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/0f1e2d3c-aaaa-bbbb-cccc-9d8e7f6a5b4c"
WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=6a7b8c9d-1111-2222-3333-444455556666"
DINGTALK = "https://oapi.dingtalk.com/robot/send?access_token=f00dfacecafebabe0123456789abcdef"
GENERIC = "https://hooks.example.com/agentlab/inbox-7f3a"
SECRET = "SECdemo-not-a-real-secret-42"


class Recorder:
    """MockTransport 的处理函数：记下每个请求，按 replies 依次回应（用完了就一直回最后一个）。"""

    def __init__(self, *replies):
        self.requests: list[httpx.Request] = []
        self.replies = list(replies) or [httpx.Response(200, json={"code": 0, "errcode": 0})]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(rec))
    monkeypatch.setattr(webhook, "RETRY_DELAYS", (0, 0, 0))
    return rec


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.execute(delete(Setting).where(Setting.key == "notify"))
        await session.commit()
    yield


def notice(**over) -> webhook.Notice:
    base = dict(event="finished", schedule_id="s1", schedule_name="门店周报", timezone="Asia/Shanghai",
                fire_id="f1", fire_at=datetime(2026, 10, 5, 1, 0, tzinfo=UTC), fire_kind="scheduled",
                run_id="r0123456789abcdef", status="succeeded", tier="degraded", gaps=2,
                run_url="https://agentlab.example.com/runs/r0123456789abcdef",
                manifest_hash="a1b2c3d4e5f60718293a", started_at=datetime(2026, 10, 5, 1, 0, 3, tzinfo=UTC),
                finished_at=datetime(2026, 10, 5, 1, 1, 10, tzinfo=UTC), reason=None, pending_approvals=None)
    base.update(over)
    return webhook.Notice(**base)


# --------------------------------------------------------------------------
# 请求体和签名
# --------------------------------------------------------------------------


def test_generic_json_carries_the_agreed_fields():
    url, headers, body = webhook.build_request("generic", GENERIC, None, notice(), now=NOW)
    data = json.loads(body)
    assert url == GENERIC and headers["Content-Type"] == "application/json"
    assert {k: data[k] for k in ("event", "schedule_name", "run_id", "status", "tier", "tier_label", "gaps",
                                 "run_url", "manifest_hash")} == {
        "event": "finished", "schedule_name": "门店周报", "run_id": "r0123456789abcdef", "status": "succeeded",
        "tier": "degraded", "tier_label": "降档出具", "gaps": 2,
        "run_url": "https://agentlab.example.com/runs/r0123456789abcdef", "manifest_hash": "a1b2c3d4e5f6"}
    assert data["started_at"] == "2026-10-05T01:00:03+00:00" and data["finished_at"] == "2026-10-05T01:01:10+00:00"
    assert "X-AgentLab-Signature" not in headers


def test_generic_signature_when_a_secret_is_set():
    _, headers, body = webhook.build_request("generic", GENERIC, SECRET, notice(), now=NOW)
    ts = headers["X-AgentLab-Timestamp"]
    assert ts == str(int(NOW.timestamp()))
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["X-AgentLab-Signature"] == f"sha256={expected}"


def test_feishu_text_and_signature():
    url, _, body = webhook.build_request("feishu", FEISHU, SECRET, notice(), now=NOW)
    data = json.loads(body)
    assert url == FEISHU and data["msg_type"] == "text"
    ts = data["timestamp"]
    assert ts == str(int(NOW.timestamp()))
    # 飞书的签名：以「timestamp + 换行 + 密钥」为键，对空串做 HMAC-SHA256，再 base64
    key = f"{ts}\n{SECRET}".encode()
    assert data["sign"] == base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    text = data["content"]["text"]
    for fragment in ("门店周报", "降档出具", "缺口 2 条", "a1b2c3d4e5f6", "/runs/r0123456789abcdef",
                     "2026-10-05 09:00", "2026-10-05 09:00:03", "2026-10-05 09:01:10"):
        assert fragment in text, text


def test_feishu_without_secret_has_no_sign():
    _, _, body = webhook.build_request("feishu", FEISHU, None, notice(), now=NOW)
    assert "sign" not in json.loads(body) and "timestamp" not in json.loads(body)


def test_wecom_markdown_keeps_the_key_in_the_address():
    url, _, body = webhook.build_request("wecom", WECOM, None, notice(), now=NOW)
    data = json.loads(body)
    assert url == WECOM and data["msgtype"] == "markdown"
    assert "门店周报" in data["markdown"]["content"] and "降档出具" in data["markdown"]["content"]


def test_dingtalk_markdown_and_signed_address():
    url, _, body = webhook.build_request("dingtalk", DINGTALK, SECRET, notice(), now=NOW)
    data = json.loads(body)
    assert data["msgtype"] == "markdown" and "门店周报" in data["markdown"]["title"]
    assert "降档出具" in data["markdown"]["text"]
    query = parse_qs(urlsplit(url).query)
    ts = str(int(NOW.timestamp() * 1000))
    assert query["access_token"] == ["f00dfacecafebabe0123456789abcdef"] and query["timestamp"] == [ts]
    sign = base64.b64encode(hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256).digest()).decode()
    assert query["sign"] == [sign]
    assert f"sign={quote_plus(sign)}" in url


def test_awaiting_approval_and_failed_to_start_texts():
    waiting = webhook.message(notice(event="awaiting_approval", status="interrupted", tier=None, gaps=None,
                                     pending_approvals=2, finished_at=None))
    assert "等待审批" in waiting[0] and any("2 项" in value for _, value in waiting[1])
    refused = webhook.message(notice(event="failed", status="failed_to_start", run_id=None, run_url=None,
                                     tier=None, gaps=None, manifest_hash=None, started_at=None, finished_at=None,
                                     reason="「门店周报」还没有发布版本，请先在编排页发布"))
    assert "未能发起" in refused[0] and any("还没有发布版本" in value for _, value in refused[1])


def test_url_hint_shows_only_host_and_last_four():
    assert webhook.url_hint(FEISHU) == "open.feishu.cn/…5b4c"
    assert webhook.url_hint(WECOM) == "qyapi.weixin.qq.com/…6666"
    assert webhook.url_hint("http://127.0.0.1:8794/hook") == "127.0.0.1:8794/…hook"


@pytest.mark.parametrize("url", ["", "open.feishu.cn/hook", "ftp://x.example.com/a", "https://", "https://a b.com/x"])
def test_bad_addresses_are_refused(url):
    with pytest.raises(ValueError) as caught:
        webhook.check_url(url)
    assert "Webhook 地址" in str(caught.value)


# --------------------------------------------------------------------------
# 推送与重试
# --------------------------------------------------------------------------


async def test_success_on_first_try(recorder):
    outcome = await webhook.send("feishu", FEISHU, SECRET, notice())
    assert (outcome.ok, outcome.http_status, outcome.attempts, outcome.error) == (True, 200, 1, None)
    assert len(recorder.requests) == 1 and str(recorder.requests[0].url) == FEISHU


async def test_server_errors_are_retried(recorder):
    recorder.replies = [httpx.Response(502), httpx.ReadTimeout("timed out"), httpx.Response(200, json={"code": 0})]
    outcome = await webhook.send("feishu", FEISHU, None, notice())
    assert (outcome.ok, outcome.attempts) == (True, 3)


async def test_gives_up_after_three_retries(recorder):
    recorder.replies = [httpx.Response(500)]
    outcome = await webhook.send("generic", GENERIC, None, notice())
    assert (outcome.ok, outcome.http_status, outcome.attempts) == (False, 500, 4)
    assert "HTTP 500" in outcome.error


async def test_client_errors_are_not_retried(recorder):
    recorder.replies = [httpx.Response(404)]
    outcome = await webhook.send("generic", GENERIC, None, notice())
    assert (outcome.ok, outcome.http_status, outcome.attempts) == (False, 404, 1)


async def test_platform_error_code_counts_as_failure(recorder):
    recorder.replies = [httpx.Response(200, json={"code": 19021, "msg": "sign match fail or timestamp is not within one hour"})]
    outcome = await webhook.send("feishu", FEISHU, SECRET, notice())
    assert not outcome.ok and outcome.error.startswith("open.feishu.cn 返回错误码 19021：sign match fail")
    recorder.replies = [httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})]
    assert "310000" in (await webhook.send("dingtalk", DINGTALK, SECRET, notice())).error


async def test_connection_errors_name_only_the_host(recorder):
    recorder.replies = [httpx.ConnectError(f"cannot reach {WECOM}")]
    outcome = await webhook.send("wecom", WECOM, None, notice())
    assert not outcome.ok and outcome.attempts == 4
    assert "qyapi.weixin.qq.com" in outcome.error and "6a7b8c9d" not in outcome.error


async def test_logs_never_contain_the_address_key_or_secret(recorder, caplog):
    caplog.set_level(logging.DEBUG)
    recorder.replies = [httpx.Response(500, json={"errmsg": f"bad token {DINGTALK}"}), httpx.Response(200, json={"errcode": 0})]
    await webhook.send("dingtalk", DINGTALK, SECRET, notice())
    await webhook.send("feishu", FEISHU, SECRET, notice())
    text = caplog.text
    assert "oapi.dingtalk.com" in text and "open.feishu.cn" in text   # 主机名照常记
    for leaked in (DINGTALK, FEISHU, "f00dfacecafebabe0123456789abcdef", "0f1e2d3c-aaaa", SECRET, "access_token="):
        assert leaked not in text, leaked


# --------------------------------------------------------------------------
# 每一拍的通知扫描
# --------------------------------------------------------------------------


async def make(*, run_status: str | None = "succeeded", events=("finished", "failed", "awaiting_approval"),
               fmt: str = "generic", url: str | None = GENERIC, secret: str | None = None, sealed: bool = True,
               fire_status: str = "started", output: dict | None = None, reason: str | None = None):
    """建一个配了 Webhook 的任务、一次触发和它发起的运行（运行不真的执行，状态直接写库）。"""
    async with SessionLocal() as session:
        sched = Schedule(name="门店周报", workflow_id="wf-demo", cron="0 9 * * 1", timezone="Asia/Shanghai",
                         webhook_format=fmt if url else None, webhook_url=encrypt(url) if url else None,
                         webhook_secret=encrypt(secret) if secret else None, webhook_events=list(events))
        session.add(sched)
        await session.flush()
        run = None
        if run_status:
            now = datetime.now(UTC)
            run = Run(workflow_id=None, workflow_name="门店周报", status=run_status, schedule_id=sched.id,
                      run_class="formal", started_at=now - timedelta(minutes=1),
                      finished_at=now if run_status in ("succeeded", "failed", "cancelled") else None,
                      manifest_hash=("c0ffee" * 10) if sealed else None,
                      output=output if output is not None else {
                          "report": "本周销售额 98765.43 元，同比增长 12.5%",
                          "_issuance": {"tier": "formal", "gaps": []}})
            session.add(run)
            await session.flush()
        fire = ScheduleFire(schedule_id=sched.id, fire_at=datetime.now(UTC).replace(microsecond=0),
                            kind="scheduled", status=fire_status, run_id=run.id if run else None, reason=reason)
        session.add(fire)
        await session.commit()
        return sched.id, fire.id, run.id if run else None


async def fire_row(fire_id: str) -> ScheduleFire:
    async with SessionLocal() as session:
        return await session.get(ScheduleFire, fire_id)


async def deliveries(fire_id: str) -> list[WebhookDelivery]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(WebhookDelivery).where(WebhookDelivery.fire_id == fire_id).order_by(WebhookDelivery.created_at)
        )).scalars())


async def set_run(run_id: str, **values) -> None:
    async with SessionLocal() as session:
        await session.execute(Run.__table__.update().where(Run.id == run_id).values(**values))
        await session.commit()


async def test_finished_run_is_pushed_once(recorder):
    async with SessionLocal() as session:
        session.add(Setting(key="notify", value={"public_base_url": "https://agentlab.example.com"}))
        await session.commit()
    _, fire_id, run_id = await make()
    assert await webhook.notify_due() == 1
    [req] = recorder.requests
    data = json.loads(req.content)
    assert (data["event"], data["status"], data["tier"], data["gaps"]) == ("finished", "succeeded", "formal", 0)
    assert data["run_url"] == f"https://agentlab.example.com/runs/{run_id}"
    assert data["manifest_hash"] == ("c0ffee" * 2)
    # 报告里的数字不进通知
    assert "98765.43" not in req.content.decode() and "12.5" not in req.content.decode()
    [delivery] = await deliveries(fire_id)
    assert (delivery.event, delivery.status, delivery.http_status, delivery.attempts, delivery.run_id) == (
        "finished", "ok", 200, 1, run_id)
    assert (await fire_row(fire_id)).notified_status == "succeeded"
    # 下一拍：同一状态不再推送
    assert await webhook.notify_due() == 0
    assert len(recorder.requests) == 1


async def test_no_public_address_means_no_link(recorder):
    await make()
    await webhook.notify_due()
    assert json.loads(recorder.requests[0].content)["run_url"] is None


async def test_failed_run_and_failed_to_start(recorder):
    _, failed_fire, _ = await make(run_status="failed", output={})
    _, refused_fire, _ = await make(run_status=None, fire_status="failed_to_start",
                                    reason="「门店周报」还没有发布版本，请先在编排页发布")
    assert await webhook.notify_due() == 2
    bodies = sorted((json.loads(r.content) for r in recorder.requests), key=lambda b: b["status"])
    assert [(b["event"], b["status"]) for b in bodies] == [("failed", "failed"), ("failed", "failed_to_start")]
    assert bodies[1]["run_id"] is None and "还没有发布版本" in bodies[1]["reason"]
    assert (await fire_row(failed_fire)).notified_status == "failed"
    assert (await fire_row(refused_fire)).notified_status == "failed_to_start"


async def test_unsubscribed_event_is_marked_but_not_pushed(recorder):
    _, fire_id, _ = await make(events=("failed",))
    assert await webhook.notify_due() == 0
    assert recorder.requests == [] and await deliveries(fire_id) == []
    assert (await fire_row(fire_id)).notified_status == "succeeded"


async def test_without_webhook_old_fires_are_not_pushed_later(recorder):
    sid, fire_id, _ = await make(url=None)
    await webhook.notify_due()
    assert (await fire_row(fire_id)).notified_status == "succeeded"
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == sid).values(
            webhook_format="generic", webhook_url=encrypt(GENERIC), webhook_events=["finished"]))
        await session.commit()
    assert await webhook.notify_due() == 0 and recorder.requests == []


async def test_cancelled_run_is_not_pushed(recorder):
    _, fire_id, _ = await make(run_status="cancelled")
    await webhook.notify_due()
    assert recorder.requests == [] and (await fire_row(fire_id)).notified_status == "cancelled"


async def test_waits_for_the_seal_before_pushing(recorder):
    _, fire_id, run_id = await make(sealed=False)
    assert await webhook.notify_due() == 0 and (await fire_row(fire_id)).notified_status is None
    await set_run(run_id, manifest_hash="beef" * 16)
    assert await webhook.notify_due() == 1
    assert json.loads(recorder.requests[0].content)["manifest_hash"] == "beef" * 3


async def test_awaiting_approval_only_with_a_real_pending_approval(recorder):
    """服务重启挂起的运行也是 interrupted，但没有待审批：不推。有了待审批推一次，批完跑完再推完成"""
    _, fire_id, run_id = await make(run_status="interrupted")
    assert await webhook.notify_due() == 0 and (await fire_row(fire_id)).notified_status is None
    async with SessionLocal() as session:
        session.add(Approval(run_id=run_id, node_id="gate", title="审核周报", status="pending"))
        await session.commit()
    assert await webhook.notify_due() == 1
    data = json.loads(recorder.requests[0].content)
    assert (data["event"], data["status"], data["pending_approvals"]) == ("awaiting_approval", "interrupted", 1)
    assert await webhook.notify_due() == 0
    await set_run(run_id, status="succeeded", finished_at=datetime.now(UTC), manifest_hash="ab" * 32)
    assert await webhook.notify_due() == 1
    assert [json.loads(r.content)["event"] for r in recorder.requests] == ["awaiting_approval", "finished"]
    assert [d.event for d in await deliveries(fire_id)] == ["awaiting_approval", "finished"]


async def test_failed_delivery_is_recorded_without_secrets(recorder):
    recorder.replies = [httpx.Response(503, text=f"upstream {DINGTALK} down")]
    _, fire_id, _ = await make(fmt="dingtalk", url=DINGTALK, secret=SECRET)
    await webhook.notify_due()
    [delivery] = await deliveries(fire_id)
    assert (delivery.status, delivery.http_status, delivery.attempts) == ("failed", 503, 4)
    assert "HTTP 503" in delivery.error
    for leaked in (DINGTALK, "f00dfacecafebabe", SECRET):
        assert leaked not in delivery.error
    # 推送失败也算这个状态处理过了：不会每一拍都再试一遍
    assert (await fire_row(fire_id)).notified_status == "succeeded"


async def test_test_message(recorder):
    sid, _, _ = await make(fmt="feishu", url=FEISHU, secret=SECRET, run_status=None, fire_status="skipped_overlap")
    result = await webhook.send_test(sid)
    assert result == {"ok": True, "http_status": 200, "message": "测试消息已送达（HTTP 200）"}
    data = json.loads(recorder.requests[0].content)
    assert "测试" in data["content"]["text"] and "门店周报" in data["content"]["text"] and data["sign"]
    async with SessionLocal() as session:
        [row] = (await session.execute(select(WebhookDelivery).where(WebhookDelivery.schedule_id == sid))).scalars()
    assert (row.event, row.status, row.fire_id, row.attempts) == ("test", "ok", None, 1)
    recorder.replies = [httpx.ConnectError("refused")]
    failed = await webhook.send_test(sid)
    assert failed["ok"] is False and failed["http_status"] is None and "open.feishu.cn" in failed["message"]


