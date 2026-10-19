"""评测题集的定时与通知（app/eval/scheduled.py、notify/service.notify_eval；细案 01 做法 15，总方案 X4、X6）。

- 定时接口按 target_kind 收评测题集：核对目标交给 app/eval/scheduled.check_target（题集在、没归档、有启用的题）；非工作流
  目标不收工作流、输入模板、Webhook（Webhook 的事件说的是那一次运行，非工作流目标收不到，照实拒）；接口返回目标的种类、id、
  名字；工作流的定时行为一字不变（test_schedules_api.py）；
- 到点建一次评测：定时跑 1 次，截至时间是计划触发时间，ScheduleFire.eval_run_id 指向它；上一次还没结束就 skipped_overlap；
  错过的按宽限补（同一套调度循环）；
- 有一道题从通过变成不通过推「定时评测有退步」，没评完推「定时评测没有评完」：只推订阅了的全局渠道，消息里只有题集名、
  通过数、退步题数、链接，不放期望值和答案里的数。手动发起的评测不推（叫法照实带「定时」）。题集名、渠道都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.crypto import encrypt
from app.db.base import SessionLocal, utcnow
from app.db.models import NotifyChannel, Schedule
from app.db.models_eval import EvalResult, EvalRun
from app.engine.runner import run_manager
from app.eval import answer as answer_mod
from app.eval import runner
from app.main import app
from app.notify import core
from app.notify import service as notify
from app.scheduler import loop, service
from tests import eval_samples as es

UTC = timezone.utc
URL = "https://hooks.example.com/agentlab/eval-7c2a"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await runner.manager.shutdown()
    await notify.drain()
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.execute(delete(NotifyChannel).where(NotifyChannel.name.like("评测通知-%")))
        await session.commit()
    await es.drop_suites()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def received(monkeypatch):
    got: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(core, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(core, "RETRY_DELAYS", (0, 0, 0))
    return got


async def _public_url(url: str):
    """临时设对外访问地址（通知里的链接按它拼），交回复原的函数：别的测试不受影响。"""
    from app.core import public_url
    from app.db.models import Setting

    async with SessionLocal() as session:
        row = await session.get(Setting, "notify")
        before = dict(row.value or {}) if row is not None else None
        if row is None:
            session.add(Setting(key="notify", value={"public_base_url": url}))
        else:
            row.value = {**(row.value or {}), "public_base_url": url}
        await session.commit()
        await public_url.refresh(session)

    async def restore() -> None:
        async with SessionLocal() as session:
            row = await session.get(Setting, "notify")
            if before is None and row is not None:
                await session.delete(row)
            elif row is not None:
                row.value = before
            await session.commit()
            await public_url.refresh(session)

    return restore


def stub(monkeypatch, *, delay: float = 0.0, path: str = "asked"):
    async def drive(self):
        await asyncio.sleep(delay)
        self.turn.path = path
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)


ASK = [{"question": "含糊的问题", "expectations": [{"kind": "ask"}]}]


async def _schedule(client, suite_id: str, **extra) -> dict:
    made = await client.post("/api/schedules", json={"name": "每周回归", "target_kind": "eval_suite",
                                                     "target_id": suite_id, "cron": "0 9 * * 1",
                                                     "timezone": "Asia/Shanghai", **extra})
    assert made.status_code == 201, made.text
    return made.json()


async def test_the_schedule_api_takes_an_eval_suite(client):
    suite_id = await es.make_suite("none", name="虚构回归题集", cases=ASK)
    body = await _schedule(client, suite_id)
    assert (body["target_kind"], body["target_id"], body["target_name"]) == ("eval_suite", suite_id, "虚构回归题集")
    assert body["target_label"] == "评测题集" and body["workflow_id"] == "" and body["webhook"] is None
    assert body["input_template"] == {} and body["target_exists"] is True
    listed = (await client.get("/api/schedules")).json()["items"]
    assert [s["target_name"] for s in listed] == ["虚构回归题集"]
    for bad, field in (({"target_id": "nope"}, "target_id"), ({"target_id": None}, "target_id"),
                       ({"webhook": {"url": URL}}, "webhook"), ({"input_template": {"a": "b"}}, "input_template"),
                       ({"workflow_id": "wf"}, "workflow_id"), ({"target_kind": "nope"}, "target_kind")):
        refused = await client.post("/api/schedules", json={"name": "x", "target_kind": "eval_suite",
                                                            "target_id": suite_id, "cron": "0 9 * * 1",
                                                            "timezone": "Asia/Shanghai", **bad})
        assert refused.status_code == 422 and refused.json()["field"] == field, (bad, refused.text)
    # 改名、改时间、停用都照常；目标种类建好不能改
    patched = await client.patch(f"/api/schedules/{body['id']}", json={"name": "每天回归", "cron": "0 9 * * *",
                                                                       "enabled": False, "webhook": None,
                                                                       "version": body["version"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "每天回归" and patched.json()["next_run_at"] is None
    moved = await client.patch(f"/api/schedules/{body['id']}", json={"target_kind": "workflow",
                                                                     "version": patched.json()["version"]})
    assert moved.status_code == 422 and moved.json()["field"] == "target_kind"
    # 题集归档了：照实拒
    archived = await es.make_suite("none", name="归档的题集", cases=ASK)
    async with SessionLocal() as session:
        from app.db.models_eval import EvalSuite

        (await session.get(EvalSuite, archived)).archived = True
        await session.commit()
    refused = await client.post("/api/schedules", json={"name": "x", "target_kind": "eval_suite", "target_id": archived,
                                                        "cron": "0 9 * * 1", "timezone": "Asia/Shanghai"})
    assert refused.status_code == 422 and "已归档" in refused.json()["detail"]


async def test_firing_starts_one_evaluation_and_skips_on_overlap(client, monkeypatch):
    stub(monkeypatch, delay=0.5)
    suite_id = await es.make_suite("none", cases=ASK, default_variant={"repeats": 3})
    body = await _schedule(client, suite_id)
    now = utcnow().replace(microsecond=0)
    fire = await service.fire_once(body["id"], now, "manual", actor="王芳")
    assert fire.status == "started" and fire.eval_run_id and fire.run_id is None
    async with SessionLocal() as session:
        run = await session.get(EvalRun, fire.eval_run_id)
    assert run.trigger == "schedule" and run.schedule_id == body["id"] and run.repeats == 1, "定时只跑 1 次"
    assert run.as_of.replace(tzinfo=UTC) == now and run.created_by == "王芳"
    again = await service.fire_once(body["id"], now + timedelta(seconds=1), "manual")
    assert again.status == "skipped_overlap" and f"#{run.id[:6]}" in again.reason
    same = await service.fire_once(body["id"], now, "manual")
    assert same.id == fire.id, "同一次触发只发起一次"
    await es.wait_eval(run.id)
    history = (await client.get(f"/api/schedules/{body['id']}/fires")).json()["items"]
    assert {f["eval_run_id"] for f in history if f["status"] == "started"} == {run.id}


async def test_a_missed_fire_within_grace_is_made_up(client, monkeypatch):
    stub(monkeypatch)
    suite_id = await es.make_suite("none", cases=ASK)
    body = await _schedule(client, suite_id)
    due = utcnow().replace(microsecond=0) - timedelta(minutes=20)
    async with SessionLocal() as session:
        await session.execute(Schedule.__table__.update().where(Schedule.id == body["id"]).values(next_run_at=due))
        await session.commit()
    fires = await loop.fire_due(due + timedelta(minutes=20))
    [made_up] = [f for f in fires if f.status == "started"]
    assert made_up.kind == "misfire" and made_up.eval_run_id
    await es.wait_eval(made_up.eval_run_id)


async def _channel(events: list[str]) -> str:
    async with SessionLocal() as session:
        row = NotifyChannel(name=f"评测通知-{uuid.uuid4().hex[:6]}", scope="global", format="generic", url=encrypt(URL),
                            events=events)
        session.add(row)
        await session.commit()
        return row.id


async def test_a_regression_is_pushed_without_numbers(client, monkeypatch, received, tmp_path):
    name, sid = await es.make_source(tmp_path)
    es.use_copilot(monkeypatch, es.Copilot(name))
    es.use_report(monkeypatch)
    suite_id = await es.make_suite(sid, name="虚构周回归")
    body = await _schedule(client, suite_id)
    await _channel(["eval.regressed", "eval.failed"])
    first = await service.fire_once(body["id"], utcnow().replace(microsecond=0) - timedelta(minutes=5), "manual")
    await es.wait_eval(first.eval_run_id)
    assert received == [], "第一次没有可比的、也评完了：不推"
    await es.set_notes(sid, {"label": es.NOTES["label"]})
    second = await service.fire_once(body["id"], utcnow().replace(microsecond=0), "manual")
    await es.wait_eval(second.eval_run_id)
    [request] = received
    message = json.loads(request.content)
    assert message["event"] == "eval.regressed" and message["title"] == "定时评测「虚构周回归」有退步"
    text = message["text"]
    assert "通过：0 / 1 题" in text and "退步：1 题" in text
    # 消息里的编号（32 位十六进制）和发送时间是随机的、可能正好带着「310」：去掉再查，查的是写出来的文字
    readable = re.sub(r"[0-9a-f]{32}", "<id>", json.dumps({**message, "sent_at": ""}, ensure_ascii=False))
    for secret in ("330", "310", "gmv", "orders", "paid_date"):
        assert secret not in readable, f"通知里不该有 {secret}"
    from app.db.models import NotifyDelivery

    async with SessionLocal() as session:
        [delivery] = (await session.execute(select(NotifyDelivery).where(
            NotifyDelivery.subject_id == second.eval_run_id))).scalars().all()
    assert delivery.subject_kind == "eval_run" and delivery.status == "ok"
    from tests.fixtures.sources import drop_source

    await drop_source(sid)


async def test_an_unfinished_scheduled_evaluation_pushes_failed(client, monkeypatch, received):
    async def drive(self):
        self.turn.path, self.turn.copilot_failed, self.turn.reason = "build_failed", True, "助手本轮未完成"
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    suite_id = await es.make_suite("none", cases=ASK * 4, name="虚构失败题集", budget={"max_parallel": 1})
    body = await _schedule(client, suite_id)
    await _channel(["eval.failed"])
    restore = await _public_url("https://agentlab.example.com")
    try:
        fire = await service.fire_once(body["id"], utcnow().replace(microsecond=0), "manual")
        await es.wait_eval(fire.eval_run_id)
    finally:
        await restore()
    async with SessionLocal() as session:
        run = await session.get(EvalRun, fire.eval_run_id)
        skipped = (await session.execute(select(EvalResult).where(
            EvalResult.eval_run_id == run.id, EvalResult.status == "skipped"))).scalars().all()
    assert run.status == "partial" and run.stop_reason.startswith("连续 3 题") and len(skipped) == 1
    [request] = received
    message = json.loads(request.content)
    assert message["event"] == "eval.failed" and message["title"] == "定时评测「虚构失败题集」没有评完"
    assert "通过：0 / 3 题" in message["text"]
    # 链接去这次评测的详情页（第 4 波评测页上线之前去定时任务的触发历史）；设了对外访问地址才有链接
    assert [link["url"] for link in message["links"]] == [f"https://agentlab.example.com/evals/runs/{fire.eval_run_id}"], \
        message["links"]


def test_the_eval_events_say_they_are_for_scheduled_evaluations():
    """这两个事件只在定时发起的评测收尾时推（runner._conclude 认 trigger == "schedule"）：手动发起的那次人自己在看，不推。
    叫法得照实说「定时」——只写「评测失败」，人勾上之后手动评测失败了永远收不到，界面上也没处说为什么（复核 L3）；
    eval.failed 也不只是失败：到了上限、止损停下（partial）同样推，所以说「没有评完」。"""
    assert notify.EVENTS["eval.regressed"].label == "定时评测有退步"
    assert notify.EVENTS["eval.failed"].label == "定时评测没有评完"


async def test_a_manual_evaluation_that_stops_short_pushes_nothing(client, monkeypatch, received):
    """手动发起的评测止损停下：订阅了「定时评测没有评完」的渠道也不推（叫法说的就是定时的）。"""
    async def drive(self):
        self.turn.path, self.turn.copilot_failed, self.turn.reason = "build_failed", True, "助手本轮未完成"
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    suite_id = await es.make_suite("none", cases=ASK * 4, name="虚构手动题集", budget={"max_parallel": 1})
    await _channel(["eval.regressed", "eval.failed"])
    started = await client.post(f"/api/evals/suites/{suite_id}/evaluations", json={})
    assert started.status_code == 202, started.text
    [ev] = started.json()["evaluations"]
    await es.wait_eval(ev["id"])
    await notify.drain()
    async with SessionLocal() as session:
        run = await session.get(EvalRun, ev["id"])
    assert run.trigger == "manual" and run.status == "partial" and run.stop_reason.startswith("连续 3 题")
    assert received == [], "手动发起的评测不推"
