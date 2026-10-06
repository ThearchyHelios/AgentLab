"""定时运行结束后的自动对比（app/scheduler/auto_compare.py，由通知扫描 webhook.notify_due 在认领、推送之前触发）：

- 只比成功结束并封存的运行：失败、取消的指标不全，和上一次比只会得出「移除 N 个」，误导收到通知的人
- 基准是同一定时任务、同一工作流、比这次先发起的运行里成功结束并封存的最近一次：同一工作流更近的手动运行、别的
  定时任务的运行都不算；失败的那一次不当基准（拿它当基准每个指标都成了「新增」）；按发起先后挑，不按结束时间——
  老的失败运行事后被接着跑完，结束得比后一次晚，它的上一次仍是它前面那次。紧挨着的上一次不是成功的时，通知和触发
  历史写明比的是哪一次。运行详情「与上一次对比」的默认基准（api/compare.py 的 pick_base）和这里挑的是同一次
- 整份结果连同两边的清单哈希存成工件（kind=run_compare，不挂在运行名下、不进运行的工件列表），工件 id 记进触发
  记录的 compare_artifact。这个任务的第一次（只有同一工作流别的运行可比）、还没封存时不比；对比出错（基准的封存核对
  不一致、意外的异常）只记日志，通知照常推送、不带对比
- 接口：Fire 多一个 compare: {base_run_id, summary, previous} 或 null，summary 同对比接口的计数，previous 说基准是不是
  紧挨着的上一次；基准被删、被继续执行（不在终态）时为 null（对比页只认封存的运行）；基准不再是现在挑出来的那一次
  （中间失败的那次事后被接着运行成功）时也为 null（运行详情里同名的入口比的已是另一次）
- 通知：结束的通知多一行「与上一次相比：只数据变 2 个、口径与数据都变 0 个、SQL 结构变 0 个」，配了对外访问地址时附
  对比页链接（/runs/compare?target=…&base=…）；只给个数，报告、指标、查询里的具体数字一个不出现

封存绑定、时限、取消这几道保护另见 test_scheduler_compare_guards.py。两次周报运行用 tests/fixtures/compare_runs.py 的
虚构运行（区域、金额、日期都是编的），按 runner 的算法封存，不真的执行；Webhook 用 httpx.MockTransport 接住，不连外网。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core import artifact_store
from app.core.artifact_store import canonical_json, load, manifest_hash, put_json
from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent, Schedule, ScheduleFire, Setting, Workflow
from app.main import app
from app.scheduler import auto_compare, webhook
from tests.fixtures.compare_runs import NEXT_ROWS, Fake, region_sql, week

UTC = timezone.utc
T0 = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
WEEK = timedelta(days=7)
GENERIC = "https://hooks.example.com/agentlab/inbox-5a7c"
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/1a2b3c4d-aaaa-bbbb-cccc-0d9e8f7a6b5c"
BASE_URL = "https://agentlab.example.com"
#: 两次虚构周报（BASE_ROWS → NEXT_ROWS）之间两个指标都只是数据变了
SUMMARY = {"unchanged": 0, "data_only": 2, "caliber_and_data": 0, "sql_changed": 0, "params_only": 0, "added": 0,
           "removed": 0, "incomparable": 0}
LINE = "与上一次相比：只数据变 2 个、口径与数据都变 0 个、SQL 结构变 0 个"
#: 两次运行查到、算出的数（金额、退款、合计、变化量），通知里一个都不能有
NUMBERS = ("1200.5", "1,200.5", "1500.5", "1,500.5", "2600.5", "2,600.5", "2800.5", "2,800.5", "300.0", "800.0",
           "700.0", "600.0")


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"code": 0, "errcode": 0})

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


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def public_address() -> None:
    async with SessionLocal() as session:
        session.add(Setting(key="notify", value={"public_base_url": BASE_URL}))
        await session.commit()


async def new_workflow(name: str = "区域周报") -> str:
    async with SessionLocal() as session:
        workflow = Workflow(name=name)
        session.add(workflow)
        await session.commit()
        return workflow.id


async def new_schedule(workflow_id: str, *, fmt: str = "generic", url: str | None = GENERIC,
                       name: str = "区域周报 · 每周") -> str:
    async with SessionLocal() as session:
        row = Schedule(name=name, workflow_id=workflow_id, cron="0 9 * * 1", timezone="Asia/Shanghai",
                       enabled=True, next_run_at=datetime.now(UTC) + timedelta(days=3),
                       webhook_format=fmt if url else None, webhook_url=encrypt(url) if url else None,
                       webhook_events=["finished", "failed", "awaiting_approval"])
        session.add(row)
        await session.commit()
        return row.id


async def persist(fake: Fake, workflow_id: str, *, finished: datetime, schedule_id: str | None = None,
                  status: str = "succeeded", seal: bool = True, seal_upto: int | None = None) -> str:
    """把一次虚构运行落库：工件进工件库，事件进运行记录，按 runner 的算法封存（同 test_compare_api.py）。
    seal_upto 只封到第几条事件：失败时封存过、接着跑完又写下了终态，新的封存还没补上的那一刻。"""
    for content in fake.store.values():
        await put_json(content, kind="compare_fixture")
    async with SessionLocal() as session:
        run = Run(workflow_id=workflow_id, workflow_name="区域周报", status=status, graph=fake.graph,
                  input=dict(fake.inputs), run_class="formal", version=fake.version, schedule_id=schedule_id,
                  started_at=finished - timedelta(minutes=2), finished_at=finished, agent_limits={},
                  started_by="定时任务：区域周报 · 每周" if schedule_id else "周报编辑")
        session.add(run)
        await session.flush()
        rows = []
        for seq, etype, node_id, data in fake.events:
            session.add(RunEvent(run_id=run.id, seq=seq, type=etype, node_id=node_id, ts=float(seq), data=data))
            rows.append((seq, etype, node_id, data))
        run.last_seq = len(rows)
        if seal:
            covered = [r for r in rows if seal_upto is None or r[0] <= seal_upto]
            run.manifest_hash, run.manifest_seq = manifest_hash(covered), covered[-1][0]
        await session.commit()
        return run.id


async def reseal(run_id: str, fake: Fake, *, status: str, finished: datetime) -> None:
    """一次运行被接着跑完（或者被重新封存）：事件换成 fake 的那一份，按新的事件封存，状态、结束时间一并改。"""
    for content in fake.store.values():
        await put_json(content, kind="compare_fixture")
    async with SessionLocal() as session:
        await session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
        rows = []
        for seq, etype, node_id, data in fake.events:
            session.add(RunEvent(run_id=run_id, seq=seq, type=etype, node_id=node_id, ts=float(seq), data=data))
            rows.append((seq, etype, node_id, data))
        run = await session.get(Run, run_id)
        run.last_seq = len(rows)
        run.manifest_hash, run.manifest_seq = manifest_hash(rows), rows[-1][0]
        run.status, run.finished_at = status, finished
        await session.commit()


def failed_week(run_id: str, rows=NEXT_ROWS) -> Fake:
    """在 Agent 节点失败的一次周报：取数做完了，Agent 没交回结果，口径卡没有执行，一个指标都没有。"""
    run = Fake(run_id)
    run.tool("fetch", region_sql(), rows=rows)
    run.add("node.started", "bot", {"node_type": "agent"})
    run.add("node.failed", "bot", {"error": "模型接入超时"})
    run.add("run.failed", None, {"error": "节点「区域分析」失败"})
    return run


async def seal(run_id: str) -> None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]
        run.manifest_hash, run.manifest_seq = manifest_hash(rows), rows[-1][0]
        await session.commit()


async def fire_for(schedule_id: str, run_id: str, at: datetime, *, notified: str | None = None) -> str:
    async with SessionLocal() as session:
        fire = ScheduleFire(schedule_id=schedule_id, fire_at=at, kind="scheduled", status="started", run_id=run_id,
                            notified_status=notified)
        session.add(fire)
        await session.commit()
        return fire.id


async def fire_row(fire_id: str) -> ScheduleFire:
    async with SessionLocal() as session:
        return await session.get(ScheduleFire, fire_id)


async def set_run(run_id: str, **values) -> None:
    async with SessionLocal() as session:
        await session.execute(Run.__table__.update().where(Run.id == run_id).values(**values))
        await session.commit()


async def weekly_pair(*, target_status: str = "succeeded", target_seal: bool = True,
                      target_finished: datetime | None = None, fmt: str = "generic", url: str | None = GENERIC):
    """同一个定时任务的两次周报：上一次（已推送过）和这一次（还没处理），这次的数变了。返回 (任务, 上一次, 这一次,
    这一次的触发)。"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, fmt=fmt, url=url)
    base_id = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, base_id, T0 - timedelta(minutes=2), notified="succeeded")
    target_id = await persist(week("r2", NEXT_ROWS), workflow, finished=target_finished or T0 + WEEK,
                              schedule_id=sid, status=target_status, seal=target_seal)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    return sid, base_id, target_id, fire_id


def no_numbers(raw: str) -> list[str]:
    return [n for n in NUMBERS if n in raw]


# --------------------------------------------------------------------------
# 什么时候比、跟谁比、存在哪
# --------------------------------------------------------------------------


async def test_finished_run_is_compared_with_the_previous_run_of_its_schedule(recorder, client):
    await public_address()
    workflow = await new_workflow()
    sid = await new_schedule(workflow)
    other = await new_schedule(workflow, url=None, name="区域周报 · 补跑")
    base_id = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    base_fire = await fire_for(sid, base_id, T0 - timedelta(minutes=2), notified="succeeded")
    # 同一工作流、比上一次更近的两次：手动发起的、别的定时任务发起的。都不是「同一定时任务的上一次」
    await persist(week("m1"), workflow, finished=T0 + timedelta(days=3))
    await persist(week("o1"), workflow, finished=T0 + timedelta(days=5), schedule_id=other)
    target_id = await persist(week("r2", NEXT_ROWS), workflow, finished=T0 + WEEK, schedule_id=sid)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))

    assert await webhook.notify_due() == 1

    artifact = (await fire_row(fire_id)).compare_artifact
    assert artifact
    from app.api.compare import compare_runs

    stored = load(artifact)
    # 整份对比结果，连同比的时候两边的清单哈希（封存变了，这份结果就不再作数）
    assert stored["result"] == json.loads(canonical_json(await compare_runs(target_id, base_id)))
    assert stored["result"]["base"]["id"] == base_id and stored["result"]["summary"] == SUMMARY
    async with SessionLocal() as session:
        runs = await session.scalars(select(Run).where(Run.id.in_((target_id, base_id))))
        seals = {r.id: r.manifest_hash for r in runs}
    assert (stored["target_manifest"], stored["base_manifest"], stored["previous"]) == (
        seals[target_id], seals[base_id], True)
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Artifact).where(Artifact.id == artifact))).scalars())
    # 对比结果不是运行产出的：不挂在运行名下，不进这次运行的工件列表
    assert [(a.kind, a.run_id) for a in rows] == [("run_compare", "")]
    assert artifact not in [a["id"] for a in (await client.get(f"/api/runs/{target_id}/artifacts")).json()]

    items = {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}
    assert items[fire_id]["compare"] == {"base_run_id": base_id, "summary": SUMMARY, "previous": True}
    assert items[base_fire]["compare"] is None
    [listed] = [s for s in (await client.get("/api/schedules")).json()["items"] if s["id"] == sid]
    assert listed["last_fire"]["id"] == fire_id and listed["last_fire"]["compare"]["base_run_id"] == base_id

    [body] = recorder.bodies()
    link = f"{BASE_URL}/runs/compare?target={target_id}&base={base_id}"
    assert body["event"] == "finished" and LINE in body["text"] and f"查看对比：{link}" in body["text"]
    assert body["compare"] == {"base_run_id": base_id, "summary": SUMMARY, "previous": True, "url": link}
    query = parse_qs(urlsplit(body["compare"]["url"]).query)
    assert query == {"target": [target_id], "base": [base_id]}
    assert no_numbers(recorder.requests[0].content.decode()) == []
    # 下一拍：同一状态不再比、不再推
    assert await webhook.notify_due() == 0 and (await fire_row(fire_id)).compare_artifact == artifact


@pytest.mark.parametrize("status, fmt, url", [("failed", "feishu", FEISHU), ("failed", "generic", GENERIC),
                                              ("cancelled", "generic", GENERIC)])
async def test_failed_or_cancelled_run_is_not_compared(recorder, client, status, fmt, url):
    """失败、取消的运行指标不全（这次在 Agent 节点失败，口径卡没有执行）：和上一次比只会得出「移除 2 个」，收到通知
    的人会以为报告删了两个指标。不比：触发记录上没有对比，失败通知不带计数那一行和对比页链接"""
    await public_address()
    workflow = await new_workflow()
    sid = await new_schedule(workflow, fmt=fmt, url=url)
    base_id = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, base_id, T0 - timedelta(minutes=2), notified="succeeded")
    target_id = await persist(failed_week("r2"), workflow, finished=T0 + WEEK, schedule_id=sid, status=status)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    # 取消的不推送（有人手动停的）
    assert await webhook.notify_due() == (1 if status == "failed" else 0)
    assert (await fire_row(fire_id)).compare_artifact is None
    items = {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}
    assert items[fire_id]["compare"] is None
    if status == "failed":
        raw = recorder.requests[0].content.decode()
        text = json.loads(raw)["content"]["text"] if fmt == "feishu" else json.loads(raw)["text"]
        assert "运行失败" in text and f"{BASE_URL}/runs/{target_id}" in text
        for absent in ("相比", "移除", "查看对比", "/runs/compare"):
            assert absent not in text, text
        if fmt == "generic":
            assert json.loads(raw)["compare"] is None


async def test_after_a_failed_run_the_base_is_the_last_successful_one(recorder, client):
    """上一次失败了：基准取再往前、成功的那一次——拿失败的那次当基准，每个指标都成了「新增」。紧挨着的上一次不是
    它，通知和触发历史写明比的是哪一次（短编号和发起的日期，按定时任务的时区）"""
    await public_address()
    workflow = await new_workflow()
    sid = await new_schedule(workflow)
    first = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, first, T0 - timedelta(minutes=2), notified="succeeded")
    failed = await persist(failed_week("r2"), workflow, finished=T0 + WEEK, schedule_id=sid, status="failed")
    await fire_for(sid, failed, T0 + WEEK - timedelta(minutes=2), notified="failed")
    target_id = await persist(week("r3", NEXT_ROWS), workflow, finished=T0 + 2 * WEEK, schedule_id=sid)
    fire_id = await fire_for(sid, target_id, T0 + 2 * WEEK - timedelta(minutes=2))

    assert await webhook.notify_due() == 1
    stored = load((await fire_row(fire_id)).compare_artifact)
    assert stored["result"]["base"]["id"] == first and stored["previous"] is False
    [body] = recorder.bodies()
    # 第一次发起于北京时间 2026-09-07 08:58
    line = f"与上一次成功的运行（#{first[:6]}，2026-09-07）相比：只数据变 2 个、口径与数据都变 0 个、SQL 结构变 0 个"
    assert line in body["text"] and "新增" not in body["text"], body["text"]
    assert f"base={first}" in body["text"] and failed not in body["text"]
    assert (body["compare"]["base_run_id"], body["compare"]["previous"]) == (first, False)
    items = {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}
    assert items[fire_id]["compare"] == {"base_run_id": first, "summary": SUMMARY, "previous": False}
    # 运行详情「与上一次对比」的默认基准挑的是同一次（同一个函数）：两个同名入口比的是同一对运行、计数一致
    base = (await client.get(f"/api/runs/{target_id}/compare/base")).json()
    assert (base["base_run_id"], base["reason"]) == (first, "same_schedule")


async def fires_of(client, sid: str) -> dict[str, dict]:
    return {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}


async def default_base_of(client, run_id: str) -> tuple[str, str]:
    body = (await client.get(f"/api/runs/{run_id}/compare/base")).json()
    return body["base_run_id"], body["reason"]


async def test_base_follows_launch_order_not_finish_time(recorder, client):
    """第 2 周失败、第 3 周照常成功之后，有人把第 2 周接着跑完：它结束得比第 3 周晚，但它的上一次仍是第 1 周。以前
    按结束时间挑，拿后一次触发的第 3 周当基准，「与上一次相比」的变化方向是反的。

    第 3 周存下的那一份不改，但触发历史不再给它：第 3 周之前同一定时任务上一次成功的已经是第 2 周，运行详情「与上一次
    对比」比的是第 2 周。还给第 1 周那一份，两个同名入口就比的不是同一对运行，计数也对不上"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, url=None)
    first = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, first, T0 - timedelta(minutes=2), notified="succeeded")
    second = await persist(failed_week("r2"), workflow, finished=T0 + WEEK, schedule_id=sid, status="failed")
    second_fire = await fire_for(sid, second, T0 + WEEK - timedelta(minutes=2))
    third = await persist(week("r3", NEXT_ROWS), workflow, finished=T0 + 2 * WEEK, schedule_id=sid)
    third_fire = await fire_for(sid, third, T0 + 2 * WEEK - timedelta(minutes=2))
    await webhook.notify_due()
    third_artifact = (await fire_row(third_fire)).compare_artifact
    assert load(third_artifact)["result"]["base"]["id"] == first
    assert (await fires_of(client, sid))[third_fire]["compare"] == {"base_run_id": first, "summary": SUMMARY,
                                                                     "previous": False}
    assert await default_base_of(client, third) == (first, "same_schedule")

    await reseal(second, week("r2", NEXT_ROWS), status="succeeded", finished=T0 + 2 * WEEK + timedelta(days=1))
    await webhook.notify_due()
    stored = load((await fire_row(second_fire)).compare_artifact)
    assert stored["result"]["base"]["id"] == first and stored["previous"] is True
    assert (await fire_row(third_fire)).compare_artifact == third_artifact
    # 运行详情里的默认基准同样按发起先后：接着跑完的第 2 周，上一次仍是第 1 周
    assert await default_base_of(client, second) == (first, "same_schedule")
    items = await fires_of(client, sid)
    assert (items[second_fire]["compare"]["base_run_id"], items[second_fire]["compare"]["previous"]) == (first, True)
    # 第 3 周：运行详情比的是第 2 周了，触发历史不再给比第 1 周的那一份（不给链接、不给旧计数）
    assert await default_base_of(client, third) == (second, "same_schedule")
    assert items[third_fire]["compare"] is None
    [listed] = [s for s in (await client.get("/api/schedules")).json()["items"] if s["id"] == sid]
    assert listed["last_fire"]["id"] == third_fire and listed["last_fire"]["compare"] is None


async def test_a_stored_compare_stays_while_its_base_is_still_the_pick(recorder, client):
    """基准当时就不是紧挨着的上一次、现在仍是挑出来的那一次：照旧给出。中间那次还停着没结束、后来失败了、被删掉了，
    第 3 周之前同一定时任务上一次成功的都还是第 1 周，运行详情比的也是它。删掉之后它其实成了紧挨着的上一次，previous
    仍照当时记下的给：「与上一次成功的运行对比」照样属实，通知里当时也是这么说的"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, url=None)
    first = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, first, T0 - timedelta(minutes=2), notified="succeeded")
    second = await persist(failed_week("r2"), workflow, finished=T0 + WEEK, schedule_id=sid, status="running",
                           seal=False)
    await fire_for(sid, second, T0 + WEEK - timedelta(minutes=2), notified="running")
    third = await persist(week("r3", NEXT_ROWS), workflow, finished=T0 + 2 * WEEK, schedule_id=sid)
    third_fire = await fire_for(sid, third, T0 + 2 * WEEK - timedelta(minutes=2))
    await webhook.notify_due()
    shown = {"base_run_id": first, "summary": SUMMARY, "previous": False}
    assert (await fires_of(client, sid))[third_fire]["compare"] == shown
    assert await default_base_of(client, third) == (first, "same_schedule")
    await set_run(second, status="failed")
    await seal(second)
    assert (await fires_of(client, sid))[third_fire]["compare"] == shown
    assert await default_base_of(client, third) == (first, "same_schedule")
    assert (await client.delete(f"/api/runs/{second}", params={"force": "true"})).status_code == 204
    assert (await fires_of(client, sid))[third_fire]["compare"] == shown
    assert await default_base_of(client, third) == (first, "same_schedule")


async def test_without_an_earlier_success_there_is_no_base(recorder, client):
    """同一定时任务之前只有失败的那次：不自动对比；运行详情里的默认基准也不拿它（同一工作流、同一类别里也没有成功的），
    回 404 说清找不到，不给一个比出来全是「新增」的基准"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, url=None)
    failed = await persist(failed_week("r1"), workflow, finished=T0, schedule_id=sid, status="failed")
    await fire_for(sid, failed, T0 - timedelta(minutes=2), notified="failed")
    target_id = await persist(week("r2", NEXT_ROWS), workflow, finished=T0 + WEEK, schedule_id=sid)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    await webhook.notify_due()
    assert (await fire_row(fire_id)).compare_artifact is None
    resp = await client.get(f"/api/runs/{target_id}/compare/base")
    assert resp.status_code == 404 and resp.json()["code"] == "compare_no_base", resp.text


async def test_first_run_of_a_schedule_has_no_compare(recorder, client):
    """这个任务的第一次：同一工作流有更早的手动运行，对比接口会退回「同一工作流」那一档，那不是「上一次」，不比"""
    await public_address()
    workflow = await new_workflow()
    sid = await new_schedule(workflow)
    await persist(week("m1"), workflow, finished=T0)
    target_id = await persist(week("r2", NEXT_ROWS), workflow, finished=T0 + WEEK, schedule_id=sid)
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))
    assert await webhook.notify_due() == 1
    assert (await fire_row(fire_id)).compare_artifact is None
    [item] = (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]
    assert item["compare"] is None
    [body] = recorder.bodies()
    assert body["compare"] is None and "与上一次相比" not in body["text"] and "查看对比" not in body["text"]
    assert f"查看运行：{BASE_URL}/runs/{target_id}" in body["text"]


async def test_waits_for_the_seal_before_comparing(recorder):
    """runner 先写终态、再封存：两步之间被这一拍撞上就不比、不推，封存好了下一拍再比"""
    _, base_id, target_id, fire_id = await weekly_pair(target_seal=False, target_finished=datetime.now(UTC))
    assert await webhook.notify_due() == 0
    assert (await fire_row(fire_id)).compare_artifact is None and recorder.requests == []
    await seal(target_id)
    assert await webhook.notify_due() == 1
    assert load((await fire_row(fire_id)).compare_artifact)["result"]["base"]["id"] == base_id
    assert LINE in recorder.bodies()[0]["text"]


async def test_no_public_address_means_counts_without_a_link(recorder):
    _, _, _, fire_id = await weekly_pair()
    assert await webhook.notify_due() == 1
    [body] = recorder.bodies()
    assert LINE in body["text"] and "查看对比" not in body["text"] and body["compare"]["url"] is None
    assert (await fire_row(fire_id)).compare_artifact


# --------------------------------------------------------------------------
# 比不了、出错
# --------------------------------------------------------------------------


async def break_seal(run_id: str) -> None:
    async with SessionLocal() as session:
        event = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "tool.end"))).scalars().first()
        event.data = {**event.data, "duration_ms": 999}
        await session.commit()


async def explode(*args, **kwargs):
    raise RuntimeError("快照读取失败：磁盘错误")


@pytest.mark.parametrize("failure", ["broken_seal", "unexpected"])
async def test_compare_failure_only_logs_and_the_notice_still_goes_out(recorder, caplog, monkeypatch, failure):
    """基准的封存核对不一致（对比接口回 409）、对比中途抛了意外的异常：只记日志，通知照常推送、不带对比"""
    await public_address()
    _, base_id, target_id, fire_id = await weekly_pair()
    if failure == "broken_seal":
        await break_seal(base_id)
    else:
        from app.api import compare as compare_api

        monkeypatch.setattr(compare_api, "compare_runs", explode)
    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        assert await webhook.notify_due() == 1
    assert (await fire_row(fire_id)).compare_artifact is None
    [body] = recorder.bodies()
    assert (body["event"], body["run_id"], body["compare"]) == ("finished", target_id, None)
    assert "与上一次相比" not in body["text"] and f"{BASE_URL}/runs/{target_id}" in body["text"]
    assert "自动对比" in caplog.text and target_id in caplog.text
    if failure == "broken_seal":
        assert "封存核对不一致" in caplog.text


async def test_a_failed_run_is_compared_once_it_is_continued_to_success(client):
    """失败时不比；接着跑（不在终态）时也没有；跑完、成功并重新封存之后按新的封存比一次"""
    workflow = await new_workflow()
    sid = await new_schedule(workflow, url=None)
    base_id = await persist(week("r1"), workflow, finished=T0, schedule_id=sid)
    await fire_for(sid, base_id, T0 - timedelta(minutes=2), notified="succeeded")
    target_id = await persist(failed_week("r2"), workflow, finished=T0 + WEEK, schedule_id=sid, status="failed")
    fire_id = await fire_for(sid, target_id, T0 + WEEK - timedelta(minutes=2))

    async def shown():
        items = (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]
        return {f["id"]: f for f in items}[fire_id]

    await webhook.notify_due()
    assert (await fire_row(fire_id)).compare_artifact is None and (await shown())["compare"] is None
    await set_run(target_id, status="running")
    await webhook.notify_due()
    item = await shown()
    assert item["run_status"] == "running" and item["compare"] is None
    await reseal(target_id, week("r2", NEXT_ROWS), status="succeeded", finished=T0 + WEEK + timedelta(hours=1))
    await webhook.notify_due()
    assert (await shown())["compare"] == {"base_run_id": base_id, "summary": SUMMARY, "previous": True}


async def test_compare_is_hidden_once_the_base_run_is_gone_or_running(client):
    """基准那一次被删掉、或者被继续执行（不在终态）：对比页比不了，链过去只会报错，接口同样给 null"""
    sid, base_id, _, fire_id = await weekly_pair(url=None)
    await webhook.notify_due()
    assert (await fire_row(fire_id)).compare_artifact

    async def shown():
        items = (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]
        return {f["id"]: f for f in items}[fire_id]["compare"]

    assert (await shown())["base_run_id"] == base_id
    await set_run(base_id, status="running")
    assert await shown() is None
    await set_run(base_id, status="succeeded")
    assert (await shown())["base_run_id"] == base_id
    # 封存过的正式运行要强制删除
    assert (await client.delete(f"/api/runs/{base_id}", params={"force": "true"})).status_code == 204
    assert await shown() is None


def unreadable(artifact_id):
    raise PermissionError(13, "Permission denied")


@pytest.mark.parametrize("damage", ["missing", "tampered", "unreadable"])
async def test_a_damaged_artifact_reads_as_no_compare(client, monkeypatch, caplog, damage):
    """工件文件不在了、被改过、读不出来（权限、磁盘出错）：接口给 null，触发历史照常打开，不回 500"""
    sid, _, _, fire_id = await weekly_pair(url=None)
    await webhook.notify_due()
    artifact = (await fire_row(fire_id)).compare_artifact
    # 刚比完的摘要记在进程里，清掉才会去读盘
    auto_compare._BRIEFS.clear()
    if damage == "missing":
        async with SessionLocal() as session:
            await session.execute(ScheduleFire.__table__.update().where(ScheduleFire.id == fire_id)
                                  .values(compare_artifact="0" * 64))
            await session.commit()
    elif damage == "tampered":
        path = artifact_store._path_of(artifact)
        text = path.read_text(encoding="utf-8")
        assert '"data_only":2' in text
        path.write_text(text.replace('"data_only":2', '"data_only":7'), encoding="utf-8")
    else:
        monkeypatch.setattr(artifact_store, "load", unreadable)
    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        resp = await client.get(f"/api/schedules/{sid}/fires")
    assert resp.status_code == 200
    assert {f["id"]: f for f in resp.json()["items"]}[fire_id]["compare"] is None
    if damage != "missing":
        assert artifact[:12] in caplog.text


# --------------------------------------------------------------------------
# 计数摘要的写法
# --------------------------------------------------------------------------


def counts(**over) -> dict[str, int]:
    return {**{k: 0 for k in SUMMARY}, **over}


def test_summary_text_always_names_the_three_kinds_of_change():
    assert auto_compare.summary_text(counts(data_only=3, caliber_and_data=1)) == \
        "只数据变 3 个、口径与数据都变 1 个、SQL 结构变 0 个"
    # 其余各类有才写，次序同对比页
    assert auto_compare.summary_text(counts(sql_changed=1, params_only=4, added=1, removed=2, incomparable=1,
                                            unchanged=5)) == \
        "只数据变 0 个、口径与数据都变 0 个、SQL 结构变 1 个、只参数变 4 个、新增 1 个、移除 2 个、无法比较 1 个、未变 5 个"
    # 两边都没有指标：不写一串 0 个，免得读成「什么都没变」
    assert auto_compare.summary_text(counts()) == "两次运行都没有指标"


@pytest.mark.parametrize("fmt, url, link_text", [
    ("generic", GENERIC, "查看对比：{link}"),
    ("feishu", FEISHU, "查看对比：{link}"),
    ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=0a1b2c3d-1111-2222-3333-444455556666",
     "[查看对比]({link})"),
    ("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=0123456789abcdef0123456789abcdef",
     "[查看对比]({link})"),
])
def test_every_format_carries_the_counts_and_the_compare_link(fmt, url, link_text):
    link = f"{BASE_URL}/runs/compare?target=r2&base=r1"
    notice = webhook.Notice(event="finished", schedule_id="s1", schedule_name="区域周报", timezone="Asia/Shanghai",
                            run_id="r2", status="succeeded", run_url=f"{BASE_URL}/runs/r2",
                            compare={"base_run_id": "r1", "summary": counts(data_only=3, caliber_and_data=1)},
                            compare_url=link)
    _, _, body = webhook.build_request(fmt, url, None, notice)
    # 请求体是 ensure_ascii=False 的 JSON：中文、链接原样出现在里面
    text = body.decode()
    assert "与上一次相比：只数据变 3 个、口径与数据都变 1 个、SQL 结构变 0 个" in text
    assert link_text.format(link=link) in text
    # 运行链接照旧在前
    assert text.index("/runs/r2") < text.index("/runs/compare")
