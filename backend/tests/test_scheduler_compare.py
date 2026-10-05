"""定时运行结束后的自动对比（app/scheduler/auto_compare.py，由通知扫描 webhook.notify_due 在推送之前触发）：

- 运行进入终态且已封存、同一定时任务有更早的已封存运行时，用运行对比的现成函数（api/compare.py 的 default_base、
  compare_runs）和它比一次，整份结果存成工件（kind=run_compare，不挂在运行名下、不进运行的工件列表），工件 id 记进
  触发记录的 compare_artifact。基准是同一定时任务的上一次：同一工作流更近的手动运行、别的定时任务的运行都不算
- 这个任务的第一次（只有同一工作流别的运行可比）、还没封存时不比；对比出错（基准的封存核对不一致、意外的异常）只记
  日志，通知照常推送、不带对比
- 接口：Fire 多一个 compare: {base_run_id, summary} 或 null，summary 同对比接口的计数；运行又被继续执行、不在终态
  时为 null（对比页只认封存的运行）
- 通知：结束、失败的通知多一行「与上一次相比：只数据变 2 个、口径与数据都变 0 个、SQL 结构变 0 个」，配了对外访问
  地址时附对比页链接（/runs/compare?target=…&base=…）；只给个数，报告、指标、查询里的具体数字一个不出现

两次周报运行用 tests/fixtures/compare_runs.py 的虚构运行（区域、金额、日期都是编的），按 runner 的算法封存，不真的
执行；Webhook 用 httpx.MockTransport 接住，不连外网。
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

from app.core.artifact_store import canonical_json, load, manifest_hash, put_json
from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent, Schedule, ScheduleFire, Setting, Workflow
from app.main import app
from app.scheduler import auto_compare, webhook
from tests.fixtures.compare_runs import NEXT_ROWS, Fake, week

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
                  status: str = "succeeded", seal: bool = True) -> str:
    """把一次虚构运行落库：工件进工件库，事件进运行记录，按 runner 的算法封存（同 test_compare_api.py）。"""
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
            run.manifest_hash, run.manifest_seq = manifest_hash(rows), rows[-1][0]
        await session.commit()
        return run.id


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
    assert stored == json.loads(canonical_json(await compare_runs(target_id, base_id)))
    assert stored["base"]["id"] == base_id and stored["summary"] == SUMMARY
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Artifact).where(Artifact.id == artifact))).scalars())
    # 对比结果不是运行产出的：不挂在运行名下，不进这次运行的工件列表
    assert [(a.kind, a.run_id) for a in rows] == [("run_compare", "")]
    assert artifact not in [a["id"] for a in (await client.get(f"/api/runs/{target_id}/artifacts")).json()]

    items = {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}
    assert items[fire_id]["compare"] == {"base_run_id": base_id, "summary": SUMMARY}
    assert items[base_fire]["compare"] is None
    [listed] = [s for s in (await client.get("/api/schedules")).json()["items"] if s["id"] == sid]
    assert listed["last_fire"]["id"] == fire_id and listed["last_fire"]["compare"]["base_run_id"] == base_id

    [body] = recorder.bodies()
    link = f"{BASE_URL}/runs/compare?target={target_id}&base={base_id}"
    assert body["event"] == "finished" and LINE in body["text"] and f"查看对比：{link}" in body["text"]
    assert body["compare"] == {"base_run_id": base_id, "summary": SUMMARY, "url": link}
    query = parse_qs(urlsplit(body["compare"]["url"]).query)
    assert query == {"target": [target_id], "base": [base_id]}
    assert no_numbers(recorder.requests[0].content.decode()) == []
    # 下一拍：同一状态不再比、不再推
    assert await webhook.notify_due() == 0 and (await fire_row(fire_id)).compare_artifact == artifact


@pytest.mark.parametrize("fmt, url", [("feishu", FEISHU), ("generic", GENERIC)])
async def test_failed_run_notice_carries_the_counts_too(recorder, fmt, url):
    """失败的运行也封存：和上一次比一次，失败通知同样带计数摘要"""
    await public_address()
    _, base_id, target_id, fire_id = await weekly_pair(target_status="failed", fmt=fmt, url=url)
    assert await webhook.notify_due() == 1
    assert (await fire_row(fire_id)).compare_artifact
    raw = recorder.requests[0].content.decode()
    text = json.loads(raw)["content"]["text"] if fmt == "feishu" else json.loads(raw)["text"]
    assert "运行失败" in text and LINE in text and f"base={base_id}" in text and f"target={target_id}" in text
    assert no_numbers(raw) == []


async def test_cancelled_run_is_compared_but_not_pushed(recorder, client):
    """取消的运行不推送（有人手动停的），对比照样算：触发历史里一样能点「对比上一次」"""
    sid, base_id, _, fire_id = await weekly_pair(target_status="cancelled")
    assert await webhook.notify_due() == 0 and recorder.requests == []
    items = {f["id"]: f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"]}
    assert items[fire_id]["compare"]["base_run_id"] == base_id


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
    assert load((await fire_row(fire_id)).compare_artifact)["base"]["id"] == base_id
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


async def test_compare_is_hidden_once_the_run_is_running_again(client):
    """失败后被继续执行（不在终态）：对比页只认封存的运行，接口不给 compare；再次结束时重新比"""
    sid, base_id, target_id, fire_id = await weekly_pair(target_status="failed", url=None)
    await webhook.notify_due()
    assert (await fire_row(fire_id)).compare_artifact
    await set_run(target_id, status="running")
    [item] = [f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"] if f["id"] == fire_id]
    assert item["run_status"] == "running" and item["compare"] is None
    await set_run(target_id, status="succeeded", finished_at=T0 + WEEK + timedelta(hours=1))
    await webhook.notify_due()
    [item] = [f for f in (await client.get(f"/api/schedules/{sid}/fires")).json()["items"] if f["id"] == fire_id]
    assert item["compare"] == {"base_run_id": base_id, "summary": SUMMARY}


async def test_a_missing_artifact_reads_as_no_compare(client):
    """工件文件不在了、被改过：接口给 null，不报错"""
    sid, _, _, fire_id = await weekly_pair(url=None)
    async with SessionLocal() as session:
        await session.execute(ScheduleFire.__table__.update().where(ScheduleFire.id == fire_id)
                              .values(compare_artifact="0" * 64))
        await session.commit()
    resp = await client.get(f"/api/schedules/{sid}/fires")
    assert resp.status_code == 200
    assert {f["id"]: f for f in resp.json()["items"]}[fire_id]["compare"] is None


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
