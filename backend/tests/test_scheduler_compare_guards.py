"""定时运行自动对比的几道保护（app/scheduler/auto_compare.py、webhook.notify_due）：

- 和封存绑定：运行的封存要盖住它最后一条结束事件才算封存完。runner 先写终态、再另起会话封存，失败后接着跑完的运行
  在两步之间还带着失败时的清单哈希——这时去比，只比得到旧封存范围里的事件，通知里的清单哈希也是旧的。等不来新的
  封存（两步之间进程没了）时照常推送，不带对比、不写旧的哈希
- 对比结果记下两边的清单哈希：任何一边后来变了，触发历史就不再给这份对比（悬停的计数和点进去的对比页对不上）；
  对比途中封存变了，结果不记
- 时限：对比放在认领之前、锁外做，最多等 WAIT_S 秒，同一时刻最多 SLOTS 个。慢的对比不压住这条通知，也不压住别的
  定时任务的通知；扫描在对比途中被取消（停机），这次触发还没认领，下次照常比、照常推。同一份封存已经比过的（比完
  还没推送就停机了），下次直接用记下的结果

运行、任务名、金额都是虚构的（tests/fixtures/compare_runs.py），不真的执行；Webhook 用 httpx.MockTransport 接住。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import scheduler
from app.api import compare as compare_api
from app.core.artifact_store import load
from app.core.config import Settings
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.main import app
from app.scheduler import auto_compare, webhook
from tests.fixtures.compare_runs import BASE_ROWS, NEXT_ROWS, Fake, default_metrics, region_sql, week
from tests.test_scheduler_compare import (  # noqa: F401 - clean、recorder 是夹具，按名字引入
    BASE_URL, LINE, T0, WEEK, Recorder, clean, fire_for, fire_row, new_schedule, new_workflow, persist,
    public_address, recorder, reseal, seal, set_run, weekly_pair,
)

UTC = timezone.utc


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def slow_compare(monkeypatch):
    """对比要 5 秒：真的对比之前先等着。返回原来的函数，用例里可以换回去"""
    real = compare_api.compare_runs

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return await real(*args, **kwargs)

    monkeypatch.setattr(compare_api, "compare_runs", slow)
    return real


def failed_then_continued(run_id: str, rows=NEXT_ROWS) -> tuple[Fake, int]:
    """在 Agent 节点失败、之后从断点接着跑完的一次周报。返回 (运行, 失败时封存到第几条事件)。"""
    run = Fake(run_id)
    sql = region_sql()
    fetch = run.tool("fetch", sql, rows=rows)
    run.add("node.started", "bot", {"node_type": "agent"})
    run.add("node.failed", "bot", {"error": "模型接入超时"})
    run.add("run.failed", None, {"error": "节点「区域分析」失败"})
    failed_at = len(run.events)
    run.add("run.resumed", None, {"from": ["bot"], "message": "从「区域分析」继续运行"})
    bot = run.agent("bot", sql, rows=rows)
    run.card(default_metrics(fetch, bot, rows, rows))
    run.add("run.finished", None, {"output": {}})
    return run, failed_at


async def previous_week(*, url: str | None = None):
    """一个定时任务和它上一次成功、已推送过的周报。返回 (工作流, 任务, 上一次)。"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, url=url) if url else await new_schedule(workflow)
    base_id = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, base_id, T0 - timedelta(minutes=2), notified="succeeded")
    return workflow, sid, base_id


async def seal_of(run_id: str) -> str | None:
    async with SessionLocal() as session:
        return (await session.get(Run, run_id)).manifest_hash


# --------------------------------------------------------------------------
# 和封存绑定
# --------------------------------------------------------------------------


async def test_waits_for_the_seal_of_this_ending_not_the_failed_one(recorder):
    """失败后接着跑完：终态和结束时间已经写下，封存还是失败时那一份（只盖到 run.failed）。这一拍不比、不推，新的
    封存补上之后再比，比的是完整的一次，通知里的清单哈希是新的"""
    await public_address()
    workflow, sid, base_id = await previous_week()
    fake, failed_at = failed_then_continued("r2")
    target_id = await persist(fake, workflow, finished=datetime.now(UTC), schedule_id=sid, seal_upto=failed_at)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    stale = await seal_of(target_id)

    assert await webhook.notify_due() == 0
    assert recorder.requests == [] and (await fire_row(fire_id)).compare_artifact is None
    await seal(target_id)
    fresh = await seal_of(target_id)
    assert fresh != stale
    assert await webhook.notify_due() == 1
    stored = load((await fire_row(fire_id)).compare_artifact)
    assert (stored["target_manifest"], stored["result"]["base"]["id"]) == (fresh, base_id)
    [body] = recorder.bodies()
    assert LINE in body["text"] and body["manifest_hash"] == fresh[:12]


async def test_a_seal_that_never_catches_up_is_not_used(recorder):
    """新的封存一直没补上（写完终态、封存之前进程没了）：等过时限照常推送，但不比，也不写失败时那份旧的清单哈希"""
    await public_address()
    workflow, sid, _ = await previous_week()
    fake, failed_at = failed_then_continued("r2")
    target_id = await persist(fake, workflow, finished=datetime.now(UTC), schedule_id=sid, seal_upto=failed_at)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    later = datetime.now(UTC) + timedelta(seconds=webhook.SEAL_WAIT_S + 5)
    assert await webhook.notify_due(later) == 1
    [body] = recorder.bodies()
    assert (body["event"], body["manifest_hash"], body["compare"]) == ("finished", None, None)
    assert "清单哈希" not in body["text"] and "相比" not in body["text"]
    assert (await fire_row(fire_id)).compare_artifact is None


async def test_approval_notice_does_not_carry_the_seal_of_an_earlier_failure(recorder):
    """失败后接着跑、停在审批上：运行上还留着失败时的清单哈希，那不是这一次的封存，「等待审批」里不写"""
    workflow, sid, _ = await previous_week()
    fake, failed_at = failed_then_continued("r2")
    target_id = await persist(fake, workflow, finished=T0 + WEEK, schedule_id=sid, status="interrupted",
                              seal_upto=failed_at)
    await set_run(target_id, finished_at=None)
    async with SessionLocal() as session:
        from app.db.models import Approval

        session.add(Approval(run_id=target_id, node_id="card", title="审核周报口径", status="pending"))
        await session.commit()
    await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    assert await webhook.notify_due() == 1
    [body] = recorder.bodies()
    assert (body["event"], body["manifest_hash"]) == ("awaiting_approval", None)
    assert "清单哈希" not in body["text"]


@pytest.mark.parametrize("side", ["target", "base"])
async def test_fire_history_drops_a_compare_whose_seal_changed(client, side):
    """比过之后哪一边被重新封存（内容变了）：记下的计数已经不是对比页现场算出来的那样，触发历史不再给这份对比"""
    sid, base_id, target_id, fire_id = await weekly_pair(url=None)
    await webhook.notify_due()

    async def shown():
        items = (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]
        return {f["id"]: f for f in items}[fire_id]["compare"]

    assert (await shown())["base_run_id"] == base_id
    # 换成和另一边一样的数：现场再比就是「未变」
    run_id, rows = (target_id, BASE_ROWS) if side == "target" else (base_id, NEXT_ROWS)
    async with SessionLocal() as session:
        finished = (await session.get(Run, run_id)).finished_at
    await reseal(run_id, week("rx", rows), status="succeeded", finished=finished)
    assert await shown() is None
    # 对比页现场算出来的和记下的确实不一样
    assert (await compare_api.compare_runs(target_id, base_id))["summary"]["unchanged"] == 2


async def test_result_is_dropped_when_a_seal_changes_mid_compare(recorder, monkeypatch):
    """对比途中基准被重新封存：比出来的是旧封存的结果，不记进触发记录、不写进通知"""
    await public_address()
    _, base_id, _, fire_id = await weekly_pair()
    real = compare_api.compare_runs

    async def resealing(run_id, other):
        out = await real(run_id, other)
        await set_run(base_id, manifest_hash="ab" * 32)
        return out

    monkeypatch.setattr(compare_api, "compare_runs", resealing)
    assert await webhook.notify_due() == 1
    assert (await fire_row(fire_id)).compare_artifact is None
    [body] = recorder.bodies()
    assert body["compare"] is None and "相比" not in body["text"]


# --------------------------------------------------------------------------
# 时限、取消、并发
# --------------------------------------------------------------------------


async def test_a_slow_compare_is_cut_off_and_the_notice_goes_out(recorder, monkeypatch, caplog, slow_compare):
    """对比超过时限：通知不等它，照常推送、不带对比，记一条警告"""
    monkeypatch.setattr(auto_compare, "WAIT_S", 0.3, raising=False)
    await public_address()
    _, _, target_id, fire_id = await weekly_pair()
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        assert await webhook.notify_due() == 1
    assert time.monotonic() - began < 2
    [body] = recorder.bodies()
    assert (body["event"], body["compare"]) == ("finished", None) and "相比" not in body["text"]
    assert f"{BASE_URL}/runs/{target_id}" in body["text"]
    assert (await fire_row(fire_id)).compare_artifact is None
    assert "超过" in caplog.text and target_id in caplog.text


async def test_scan_cancelled_mid_compare_pushes_next_time(recorder, monkeypatch, slow_compare):
    """停机时通知扫描只等 1.5 秒就被取消：撞在对比途中的那次触发还没认领，下次启动照常比、照常推。以前先认领、后比，
    取消之后这条「运行结束」再也不会推"""
    await public_address()
    _, _, _, fire_id = await weekly_pair()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(webhook.notify_due(), timeout=0.5)
    assert (await fire_row(fire_id)).notified_status is None and recorder.requests == []
    monkeypatch.setattr(compare_api, "compare_runs", slow_compare)
    assert await webhook.notify_due() == 1
    [body] = recorder.bodies()
    assert LINE in body["text"]
    assert (await fire_row(fire_id)).notified_status == "succeeded"


async def test_a_slow_compare_does_not_hold_other_schedules(monkeypatch, slow_compare):
    """任务 A 的对比慢：调度循环里通知扫描同一时刻只跑一份，以前 A 比多久，之后才结束的任务 B（第一次，不用比）
    的通知就要等多久。现在 A 的对比等过时限就不等了，B 的通知在下一拍推出"""
    rec = Recorder()
    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(rec))
    monkeypatch.setattr(webhook, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(auto_compare, "WAIT_S", 0.5, raising=False)
    await weekly_pair()
    on = Settings(_env_file=None, scheduler=True)
    assert await scheduler.start(interval=0.05, config=on) is True
    try:
        await asyncio.sleep(0.2)
        workflow = await new_workflow("仓库日报")
        sid = await new_schedule(workflow, name="仓库日报 · 每天")
        run_id = await persist(week("b1"), workflow, finished=T0, schedule_id=sid)
        await fire_for(sid, run_id, T0 - timedelta(minutes=2))
        began = time.monotonic()
        while not any(b.get("run_id") == run_id for b in rec.bodies()) and time.monotonic() - began < 8:
            await asyncio.sleep(0.02)
        waited = time.monotonic() - began
    finally:
        await scheduler.stop()
    assert any(b.get("run_id") == run_id for b in rec.bodies())
    assert waited < 2, f"任务 B 的通知等了 {waited:.2f} 秒"


async def test_compares_take_turns(monkeypatch):
    """同一拍里好几个任务都要比：同一时刻最多 SLOTS 个，不一起占满线程池（接口请求也要用）"""
    monkeypatch.setattr(auto_compare, "SLOTS", 2, raising=False)
    real = compare_api.compare_runs
    active = peak = 0

    async def counted(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.2)
            return await real(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(compare_api, "compare_runs", counted)
    fires = [(await weekly_pair(url=None))[3] for _ in range(4)]
    await webhook.notify_due()
    assert peak == 2
    assert all([(await fire_row(f)).compare_artifact for f in fires])


async def test_a_compare_already_made_for_this_seal_is_reused(recorder, monkeypatch):
    """比完、还没认领就停机了，结果已经记在触发记录上：下次直接用，不再算一遍"""
    await public_address()
    _, _, target_id, fire_id = await weekly_pair()
    assert await auto_compare.compare_fire(fire_id, target_id) is not None
    artifact = (await fire_row(fire_id)).compare_artifact
    assert artifact
    calls = 0

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("不该再算一遍")

    monkeypatch.setattr(compare_api, "compare_runs", counted)
    auto_compare._BRIEFS.clear()
    assert await webhook.notify_due() == 1
    assert calls == 0 and LINE in recorder.bodies()[0]["text"]
    assert (await fire_row(fire_id)).compare_artifact == artifact


async def test_seal_covers_the_last_ending():
    """封存盖没盖住最后一条结束事件：老运行（没有 manifest_seq）只看有没有哈希"""
    workflow = await new_workflow()
    fake, failed_at = failed_then_continued("r9")
    partial = await persist(fake, workflow, finished=T0, seal_upto=failed_at)
    full = await persist(failed_then_continued("r8")[0], workflow, finished=T0)
    legacy = await persist(week("r7"), workflow, finished=T0)
    await set_run(legacy, manifest_seq=None)
    unsealed = await persist(week("r6"), workflow, finished=T0, seal=False)
    async with SessionLocal() as session:
        got = {rid: await auto_compare.sealed_now(session, await session.get(Run, rid))
               for rid in (partial, full, legacy, unsealed)}
        assert await session.scalar(select(RunEvent.seq).where(
            RunEvent.run_id == partial, RunEvent.type == "run.finished")) > failed_at
    assert got == {partial: False, full: True, legacy: True, unsealed: False}
