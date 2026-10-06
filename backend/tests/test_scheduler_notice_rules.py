"""定时任务通知（app/scheduler/webhook.py）的两条规则：

- 失败通知的「处理」行不许诺能继续运行：服务重启时还在排队、从没开始执行的运行（被改成失败、没有开始时间）没有
  断点，「修正后可继续运行」是错的。按有没有开始执行分开写——没开始的另写一行原因（没有可继续的进度），处理行不再
  让人去「查看原因」（原因刚写过），任务还在排期上时只写「下一次触发会照常发起」，不在排期上时写怎么重新发起；开始
  执行过的写「请在 AgentLab 的「记录」中查看失败原因」（和挂起那句一样指到导航里的「记录」），任务还在排期上时补
  「下一次触发会照常发起」：失败的运行不挡之后的触发，和挂起的那种（之后的触发都会跳过）说清区别
- 扫描窗口（SCAN_DAYS）之外的触发：停在审批、有待审批的运行不受窗口限制——创建 30 天以后才停到审批上的照样推送
  「等待审批」；运行记录在窗口内有变化的（等了一个多月的审批批完、跑完）照样推送结束。早就结束、记录多日没有变化
  的旧触发仍不扫

用 httpx.MockTransport 接住请求，不连外网。任务名、地址、数字都是虚构的。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Schedule, ScheduleFire, Setting
from app.scheduler import webhook

UTC = timezone.utc
GENERIC = "https://hooks.example.com/agentlab/inbox-3e9d"
NOT_STARTED = "运行在开始执行前就已中止（通常是服务重启时仍在排队），没有可继续的进度"
LOOK = "请在 AgentLab 的「记录」中查看失败原因"
RERUN = "如需重新发起，请在 AgentLab 的「定时任务」中点击「立即运行」"


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


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
    base = dict(event="failed", schedule_id="s1", schedule_name="仓库日报", timezone="Asia/Shanghai",
                fire_id="f1", fire_at=datetime(2026, 10, 5, 1, 0, tzinfo=UTC), fire_kind="scheduled",
                run_id="r9876543210fedcba", status="failed", run_url=None, manifest_hash=None,
                started_at=datetime(2026, 10, 5, 1, 0, 2, tzinfo=UTC), finished_at=None)
    base.update(over)
    return webhook.Notice(**base)


async def make(*, run_status: str, started: bool = True, fire_age: timedelta = timedelta(0),
               run_age: timedelta = timedelta(0), enabled: bool = True, sealed: bool = True) -> tuple[str, str, str]:
    """一个配了通用 Webhook 的任务、一次触发和它发起的运行（不真的执行，状态直接写库）。fire_age、run_age 是触发
    记录、运行记录最后一次更新离现在多久；started=False 是从没开始执行的（服务重启时还在排队）。"""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        sched = Schedule(name="仓库日报", workflow_id="wf-demo", cron="0 9 * * *", timezone="Asia/Shanghai",
                         enabled=enabled, next_run_at=now + timedelta(hours=3) if enabled else None,
                         webhook_format="generic", webhook_url=encrypt(GENERIC),
                         webhook_events=["finished", "failed", "awaiting_approval"])
        session.add(sched)
        await session.flush()
        ended = run_status in ("succeeded", "failed", "cancelled") and started
        touched = now - run_age
        run = Run(workflow_id=None, workflow_name="仓库日报", status=run_status, schedule_id=sched.id,
                  run_class="formal", started_at=touched - timedelta(minutes=1) if started else None,
                  finished_at=touched if ended else None, manifest_hash=("d00d" * 16) if sealed and ended else None,
                  created_at=now - fire_age, updated_at=touched,
                  error="服务重启时这次运行仍在排队，没有可恢复的进度，请重新发起运行" if not started else None)
        session.add(run)
        await session.flush()
        fire = ScheduleFire(schedule_id=sched.id, fire_at=(now - fire_age).replace(microsecond=0), kind="scheduled",
                            status="started", run_id=run.id, created_at=now - fire_age)
        session.add(fire)
        await session.commit()
        return sched.id, fire.id, run.id


async def notified(fire_id: str) -> str | None:
    async with SessionLocal() as session:
        return (await session.get(ScheduleFire, fire_id)).notified_status


# --------------------------------------------------------------------------
# 失败通知的「处理」行
# --------------------------------------------------------------------------


def test_a_run_that_never_started_is_not_promised_a_continue():
    """服务重启时还在排队的运行没有断点：不写「修正后可继续运行」，另写一行原因说没有可继续的进度。处理行不再让人
    去「查看原因」（原因刚写过）：还在排期上的只说下一次照常，不在排期上的说怎么重新发起"""
    _, lines, _ = webhook.message(notice(started_at=None, next_fire=True))
    shown = dict(lines)
    assert shown["原因"] == NOT_STARTED
    assert shown["处理"] == "下一次触发会照常发起"
    assert not any("继续运行" in value or "查看" in value for _, value in lines), lines
    _, lines, _ = webhook.message(notice(started_at=None, next_fire=False))
    assert dict(lines)["处理"] == RERUN


def test_a_failed_run_gets_a_neutral_action_without_promising_a_continue():
    """开始执行过的运行也不许诺能继续：断点可能没有（执行第一个节点前就失败）、可能已不兼容（系统升级），能不能继续
    以记录页为准。失败节点照写，不另写原因（原始报错可能带着查询到的数据）"""
    _, lines, _ = webhook.message(notice(error_node="汇总库存", next_fire=True))
    shown = dict(lines)
    assert shown["失败节点"] == "「汇总库存」" and "原因" not in shown
    assert shown["处理"] == f"{LOOK}；下一次触发会照常发起"
    assert "修正后可继续运行" not in webhook._text(notice(error_node="汇总库存"))


def test_next_fire_is_only_promised_while_the_schedule_is_on():
    """任务已暂停（或往后不再触发）时不说「下一次触发会照常发起」"""
    _, lines, _ = webhook.message(notice(next_fire=False))
    assert dict(lines)["处理"] == LOOK


async def test_restart_failure_notice_end_to_end(recorder):
    """服务重启时还在排队、被改成失败的运行：不会封存，等过封存时限后推一次失败，原因、处理按上面写；通用格式的
    reason 字段也写这句原因。任务停用时不补「下一次触发会照常发起」"""
    _, on_fire, _ = await make(run_status="failed", started=False, sealed=False)
    _, off_fire, _ = await make(run_status="failed", started=False, sealed=False, enabled=False)
    later = datetime.now(UTC) + timedelta(seconds=webhook.SEAL_WAIT_S + 5)
    assert await webhook.notify_due(later) == 2
    by_fire = {b["fire_id"]: b for b in recorder.bodies()}
    on, off = by_fire[on_fire], by_fire[off_fire]
    assert (on["event"], on["status"], on["reason"]) == ("failed", "failed", NOT_STARTED)
    assert "处理：下一次触发会照常发起" in on["text"]
    assert f"处理：{RERUN}" in off["text"] and "照常发起" not in off["text"]
    for body in (on, off):
        assert "继续运行" not in body["text"], body["text"]
        # 运行记录上的原始报错不进通知
        assert "没有可恢复的进度" not in body["text"]


# --------------------------------------------------------------------------
# 扫描窗口之外
# --------------------------------------------------------------------------


async def test_old_fire_stopped_at_an_approval_is_still_pushed(recorder):
    """触发是 40 天前的，运行刚停到审批上（或者停下时服务没在推送）：有待审批的运行不受 30 天的窗口限制。
    运行记录本身也很久没有变化，照样推「等待审批」"""
    _, fire_id, run_id = await make(run_status="interrupted", fire_age=timedelta(days=40), run_age=timedelta(days=35))
    async with SessionLocal() as session:
        session.add(RunEvent(run_id=run_id, seq=1, type="run.started", ts=1.0, data={}))
        session.add(Approval(run_id=run_id, node_id="gate", title="审核日报", status="pending"))
        await session.commit()
    assert await webhook.notify_due() == 1
    [body] = recorder.bodies()
    assert (body["event"], body["pending_approvals"]) == ("awaiting_approval", 1)
    assert await notified(fire_id) == "interrupted:approval:1"
    assert await webhook.notify_due() == 0


async def test_old_fire_whose_run_just_finished_is_pushed(recorder):
    """等了一个多月的审批批完、刚跑完：运行记录在窗口内有变化，推「运行结束」"""
    _, fire_id, _ = await make(run_status="succeeded", fire_age=timedelta(days=45))
    assert await webhook.notify_due() == 1
    assert recorder.bodies()[0]["event"] == "finished" and await notified(fire_id) == "succeeded"


async def test_old_fire_whose_run_ended_long_ago_is_not_scanned(recorder):
    """早就结束、多日没有变化的旧触发仍不扫：窗口本来就是为了不每一拍把全部历史翻一遍"""
    _, fire_id, _ = await make(run_status="succeeded", fire_age=timedelta(days=45), run_age=timedelta(days=40))
    assert await webhook.notify_due() == 0
    assert recorder.requests == [] and await notified(fire_id) is None


async def test_old_suspended_run_without_approval_is_not_rescanned_forever(recorder):
    """挂起（没有待审批）、多日没有变化的旧触发不在例外里：例外只给停在审批上的"""
    _, fire_id, _ = await make(run_status="interrupted", fire_age=timedelta(days=40), run_age=timedelta(days=40))
    assert await webhook.notify_due() == 0 and await notified(fire_id) is None
