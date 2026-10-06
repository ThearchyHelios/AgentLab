"""运行对比的接口：GET /api/runs/{run_id}/compare/base 和 GET /api/runs/{run_id}/compare?base=。

两次周报运行用 tests/fixtures/compare_runs.py 的虚构运行：工件落进工件库，事件落进运行记录，按 runner 的算法封存
（不连库、不调模型）。另有一组真跑：虚构的 SQLite 库在两次运行之间改了数，再一次代进去的日期参数不同。守的是：
- 两次都结束、封存且核对一致才比：没封存、还在运行、封存后事件被改过都回 409，说清是哪一次；不是同一个工作流、
  同一次运行回 422；运行不存在 404
- 对比只读：两次运行的 last_seq、事件、工件引用、工件文件一样不多
- 遮罩列（查询当时记下的、数据源现在设的）的值不出现在结果里
- 默认基准：同一定时任务优先，否则同一工作流、同一运行类别；只挑比这次先发起、成功结束并封存的运行，按发起先后取
  最近一次（失败、取消的跳过，和定时任务的自动对比挑的是同一次，见 test_scheduler_compare.py）；previous 说它是不是
  紧挨着的上一次
- 真跑：同一条 SQL 数据变了是 data_only；只是代进去的日期不同是 params_only，图一样
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.artifact_store import manifest_hash, put_json
from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent, Workflow
from app.engine.runner import run_manager, verify_manifest
from app.main import app
from tests.fixtures.compare_runs import (
    EAST,
    NEXT_ROWS,
    Fake,
    amounts_in,
    cell_in,
    chain,
    metric,
    node,
    week,
)
from tests.fixtures.sources import drop_source

SOURCE = "cmp_shop"
T0 = datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def new_workflow(name="区域周报") -> str:
    async with SessionLocal() as session:
        workflow = Workflow(name=name)
        session.add(workflow)
        await session.commit()
        return workflow.id


async def persist(fake: Fake, workflow_id: str | None, *, finished=T0, status="succeeded", run_class="formal",
                  schedule_id=None, seal=True) -> str:
    """把一次虚构运行落库：工件进工件库（内容寻址，id 和虚构时一样），事件进运行记录，按 runner 的算法封存。"""
    for content in fake.store.values():
        await put_json(content, kind="compare_fixture")
    async with SessionLocal() as session:
        run = Run(workflow_id=workflow_id, workflow_name="区域周报", status=status, graph=fake.graph,
                  input=dict(fake.inputs), run_class=run_class, version=fake.version, schedule_id=schedule_id,
                  started_at=finished - timedelta(minutes=2), finished_at=finished if status != "running" else None,
                  agent_limits={})
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


async def pair(base=None, target=None, **kw) -> tuple[str, str, str]:
    """(工作流, 基准运行, 这次运行)：同一个工作流的两次周报，这次的数变了。"""
    workflow = await new_workflow()
    base_id = await persist(base or week("r1", source=SOURCE), workflow, finished=T0)
    target_id = await persist(target or week("r2", NEXT_ROWS, source=SOURCE), workflow,
                              finished=T0 + timedelta(days=7), **kw)
    return workflow, base_id, target_id


async def footprint(*run_ids: str) -> tuple:
    """几次运行留在库里、盘上的痕迹：各自的 last_seq、封存、事件数，加上全部工件引用行数和工件文件数。"""
    async with SessionLocal() as session:
        runs = []
        for run_id in run_ids:
            run = await session.get(Run, run_id)
            count = await session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id))
            runs.append((run.last_seq, run.manifest_hash, run.manifest_seq, run.status, count))
        artifacts = await session.scalar(select(func.count()).select_from(Artifact))
    files = sum(1 for _ in (settings.data_dir / "artifacts").rglob("*.json"))
    return runs, artifacts, files


async def get(client, path: str, status: int = 200) -> dict:
    resp = await client.get(path)
    assert resp.status_code == status, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 对比
# --------------------------------------------------------------------------


async def test_compares_two_sealed_runs(client):
    _, base_id, target_id = await pair()
    body = await get(client, f"/api/runs/{target_id}/compare?base={base_id}")
    assert body["target"]["id"] == target_id and body["base"]["id"] == base_id
    assert set(body["target"]) == {"id", "workflow_name", "version", "run_class", "started_at", "finished_at",
                                   "schedule_id"}
    assert body["target"]["finished_at"].endswith("+00:00") and body["base"]["run_class"] == "formal"
    assert body["summary"]["data_only"] == 2 and sum(body["summary"].values()) == 2
    east = next(m for m in body["metrics"] if m["metric_id"] == "east")
    assert (east["classification"], east["base"], east["target"], east["delta_rendered"]) == (
        "data_only", 1200.5, 1500.5, "+300.0元")
    total = next(m for m in body["metrics"] if m["metric_id"] == "total")
    assert [r["share"] for r in total["attribution"]["rows"]] == [1.5, -0.5, 0.0]
    assert [(q["node_id"], q["structure"]) for q in body["queries"]] == [("fetch", "same"), ("bot", "same")]
    assert body["template"] == {"base_version": 3, "target_version": 3, "same_graph": True, "nodes": []}
    assert body["notes"] == []


async def test_comparing_writes_nothing(client):
    _, base_id, target_id = await pair()
    before = await footprint(base_id, target_id)
    await get(client, f"/api/runs/{target_id}/compare?base={base_id}")
    await get(client, f"/api/runs/{target_id}/compare/base")
    assert await footprint(base_id, target_id) == before
    for run_id in (base_id, target_id):
        assert (await verify_manifest(run_id))["ok"] is True


async def test_unsealed_runs_are_refused(client):
    workflow = await new_workflow()
    base_id = await persist(week("r1", source=SOURCE), workflow)
    target_id = await persist(week("r2", NEXT_ROWS, source=SOURCE), workflow, finished=T0 + timedelta(days=7),
                              seal=False)
    body = await get(client, f"/api/runs/{target_id}/compare?base={base_id}", 409)
    assert body["code"] == "compare_unsealed" and body["detail"].startswith("这次运行")
    # 已经结束、只是没有封存清单的：说没有封存清单，不说「运行结束后才能对比」
    assert "没有封存清单" in body["detail"] and "尚未结束" not in body["detail"]
    body = await get(client, f"/api/runs/{base_id}/compare?base={target_id}", 409)
    assert body["code"] == "compare_unsealed" and body["detail"].startswith("作为基准的运行")
    # 封存过、又被接着运行（还在运行）的：封存范围之后还会变，同样不比
    running = await persist(week("r3", NEXT_ROWS, source=SOURCE), workflow, status="running")
    body = await get(client, f"/api/runs/{running}/compare?base={base_id}", 409)
    assert body["code"] == "compare_unsealed" and "尚未结束" in body["detail"]


async def test_a_broken_seal_is_refused(client):
    _, base_id, target_id = await pair()
    async with SessionLocal() as session:
        event = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == base_id, RunEvent.type == "tool.end"))).scalars().first()
        event.data = {**event.data, "duration_ms": 999}
        await session.commit()
    body = await get(client, f"/api/runs/{target_id}/compare?base={base_id}", 409)
    assert body["code"] == "compare_seal_broken" and body["detail"].startswith("作为基准的运行")
    assert "封存核对不一致" in body["detail"]


async def test_mismatched_runs_are_refused(client):
    _, base_id, target_id = await pair()
    other = await persist(week("r9", source=SOURCE), await new_workflow("另一个周报"))
    body = await get(client, f"/api/runs/{target_id}/compare?base={other}", 422)
    assert body["code"] == "compare_mismatch" and "同一个工作流" in body["detail"]
    body = await get(client, f"/api/runs/{target_id}/compare?base={target_id}", 422)
    assert body["code"] == "compare_mismatch" and "同一次运行" in body["detail"]
    unsaved = await persist(week("r8", source=SOURCE), None)
    body = await get(client, f"/api/runs/{unsaved}/compare?base={base_id}", 422)
    assert body["code"] == "compare_mismatch"
    body = await get(client, f"/api/runs/{target_id}/compare", 422)
    assert body["code"] == "compare_bad_request"
    await get(client, f"/api/runs/no-such-run/compare?base={base_id}", 404)
    body = await get(client, f"/api/runs/{target_id}/compare?base=no-such-run", 404)
    assert "基准" in body["detail"]


async def test_masked_columns_never_leave_the_server(client):
    """快照里记着查询当时遮了 refund，数据源现在又遮了 manager：两样的值都不出现在结果里。"""
    columns = ["region", "manager", "gmv", "refund"]
    types = {"region": "text", "manager": "text", "gmv": "number", "refund": "number"}
    sql = "SELECT region, manager, SUM(amount) AS gmv, SUM(refund) AS refund FROM orders GROUP BY region, manager"

    def make(run_id, rows, expression, version):
        run = Fake(run_id, source=SOURCE)
        fetch = run.tool("fetch", sql, columns, rows, types=types, masks=["refund"])
        bot = run.agent("bot", sql, columns, rows, types=types, masks=["refund"])
        inputs = [cell_in(0, "gmv", rows[0][2], fetch)]
        value = rows[0][2]
        if "refund" in expression:
            inputs.append(cell_in(0, "refund", rows[0][3], fetch))
            value = rows[0][2] - rows[0][3]
        run.card([metric("net", expression, value, inputs),
                  metric("total", "sum(vars.kpi.amounts)", sum(r[2] for r in rows), [amounts_in(bot, rows)])],
                 version=version)
        return run

    async with SessionLocal() as session:
        session.add(DataSource(name=SOURCE, kind="sqlite", database=":memory:", options={"mask_columns": ["manager"]}))
        await session.commit()
    try:
        base = make("r1", [["东区", "经理甲", 1200.5, 101.25], ["南区", "经理乙", 800.0, 52.75]], EAST, "v1")
        target = make("r2", [["东区", "经理甲", 1500.5, 133.5], ["南区", "经理乙", 700.0, 47.25]],
                      f"{EAST} - cell(nodes.fetch, 0, 'refund')", "v2")
        _, base_id, target_id = await pair(base, target)
        resp = await client.get(f"/api/runs/{target_id}/compare?base={base_id}")
        assert resp.status_code == 200, resp.text
        for secret in ("经理甲", "经理乙", "101.25", "52.75", "133.5", "47.25"):
            assert secret not in resp.text, secret
        body = resp.json()
        net = next(m for m in body["metrics"] if m["metric_id"] == "net")
        refund = next(i for i in net["inputs"] if "refund" in i["path"])
        assert refund["masked"] is True and net["decomposition"] is None and "遮罩" in net["decomposition_note"]
        total = next(m for m in body["metrics"] if m["metric_id"] == "total")
        assert total["attribution"]["columns"] == ["region"]
    finally:
        await drop_source(name=SOURCE)


# --------------------------------------------------------------------------
# 默认基准
# --------------------------------------------------------------------------


async def bare(workflow_id, finished, *, status="succeeded", run_class="formal", schedule_id=None, sealed=True,
               resumed=False, created=None) -> str:
    """只为挑基准用的运行记录：挑基准只看运行记录上的几列。resumed：结束、封存过又被接着运行（状态已回到运行中，
    结束时间和封存还没清掉），只能靠状态把它挡在外面。created：发起时间，默认在结束前 3 分钟（发起先后和结束先后
    一致）；要让两者错开的用例另给。"""
    done = status in ("succeeded", "failed", "cancelled") or resumed
    async with SessionLocal() as session:
        run = Run(workflow_id=workflow_id, workflow_name="区域周报", status=status, run_class=run_class,
                  schedule_id=schedule_id, created_at=created or finished - timedelta(minutes=3),
                  started_at=finished - timedelta(minutes=2), finished_at=finished if done else None,
                  manifest_hash="ab" * 32 if sealed else None, manifest_seq=1 if sealed else None)
        session.add(run)
        await session.commit()
        return run.id


async def test_the_default_base_prefers_the_same_schedule(client):
    workflow = await new_workflow()
    day = timedelta(days=1)
    scheduled = await bare(workflow, T0, schedule_id="sched-weekly")
    manual = await bare(workflow, T0 + day)                        # 更近，但不是同一个定时任务
    current = await bare(workflow, T0 + 2 * day, schedule_id="sched-weekly")
    body = await get(client, f"/api/runs/{current}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (scheduled, "same_schedule")
    assert body["base"]["id"] == scheduled and body["base"]["schedule_id"] == "sched-weekly"
    # 不是定时任务发起的：同一工作流、同一运行类别里最近的一次（定时的也算）
    later = await bare(workflow, T0 + 3 * day)
    body = await get(client, f"/api/runs/{later}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (current, "same_workflow")
    # 定时任务的第一次：退回同一工作流、同一运行类别
    first = await bare(workflow, T0 + 4 * day, schedule_id="sched-daily")
    body = await get(client, f"/api/runs/{first}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (later, "same_workflow")
    assert manual != body["base_run_id"]


async def test_the_default_base_skips_what_cannot_be_compared(client):
    workflow = await new_workflow()
    day = timedelta(days=1)
    good = await bare(workflow, T0)
    await bare(workflow, T0 + day, sealed=False)                       # 没封存
    await bare(workflow, T0 + 2 * day, status="running")               # 还在运行
    await bare(workflow, T0 + 2 * day, status="running", resumed=True)  # 结束过、又被接着运行
    await bare(workflow, T0 + 3 * day, run_class="exploratory")        # 别的运行类别
    await bare(await new_workflow("另一个周报"), T0 + 4 * day)          # 别的工作流
    await bare(workflow, T0 + 4 * day, status="failed")                # 失败的（封存过）
    await bare(workflow, T0 + 4 * day + timedelta(hours=1), status="cancelled")  # 取消的（封存过）
    current = await bare(workflow, T0 + 5 * day)
    await bare(workflow, T0 + 6 * day)                                 # 比它晚发起
    body = await get(client, f"/api/runs/{current}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (good, "same_workflow")
    # 第一次运行、没保存的工作流：没有可比的
    body = await get(client, f"/api/runs/{good}/compare/base", 404)
    assert body["code"] == "compare_no_base" and "上一次成功的运行" in body["detail"]
    await get(client, f"/api/runs/{await bare(None, T0)}/compare/base", 404)
    await get(client, "/api/runs/no-such-run/compare/base", 404)


async def test_the_default_base_is_the_last_successful_run(client):
    """失败、取消的不当默认基准：拿它当基准，口径卡没执行的指标都成了「新增」「移除」。同一定时任务里跳过它们，取再往前
    成功的那一次（和定时任务的自动对比同一个规则）；同一工作流的退路也一样；之前一次成功的都没有就是没有基准"""
    workflow = await new_workflow()
    day = timedelta(days=1)
    first = await bare(workflow, T0, schedule_id="sched-weekly")
    await bare(workflow, T0 + day, schedule_id="sched-weekly", status="failed")
    await bare(workflow, T0 + 2 * day, schedule_id="sched-weekly", status="cancelled")
    current = await bare(workflow, T0 + 3 * day, schedule_id="sched-weekly")
    body = await get(client, f"/api/runs/{current}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (first, "same_schedule")
    # 手动发起的：同一工作流、同一运行类别里成功的最近一次（定时的也算），中间失败的跳过
    await bare(workflow, T0 + 4 * day, status="failed")
    manual = await bare(workflow, T0 + 5 * day)
    body = await get(client, f"/api/runs/{manual}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (current, "same_workflow")
    # 之前只有失败、取消的：没有基准，原因写明要成功结束的
    lonely = await new_workflow("门店日报")
    await bare(lonely, T0, status="failed")
    await bare(lonely, T0 + day, status="cancelled", schedule_id="sched-daily")
    alone = await bare(lonely, T0 + 2 * day, schedule_id="sched-daily")
    body = await get(client, f"/api/runs/{alone}/compare/base", 404)
    assert body["code"] == "compare_no_base" and "成功结束" in body["detail"]


async def test_the_default_base_follows_launch_order(client):
    """按发起先后挑，不按结束时间：先发起、后来才结束的那次（失败后被接着运行完）仍排在这次前面；比这次晚发起的，
    结束得再早也不算。这次还没结束（停在审批上）的也按发起先后"""
    workflow = await new_workflow()
    t = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    hour, day = timedelta(hours=1), timedelta(days=1)
    await bare(workflow, T0, created=t)
    resumed = await bare(workflow, T0 + 10 * day, created=t + hour)     # 比这次先发起，结束得最晚
    current = await bare(workflow, T0 + 5 * day, created=t + 2 * hour)
    later = await bare(workflow, T0 + 4 * day, created=t + 3 * hour)    # 比这次晚发起，结束得比它早
    body = await get(client, f"/api/runs/{current}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (resumed, "same_workflow")
    waiting = await bare(workflow, T0 + 20 * day, status="interrupted", created=t + 4 * hour)
    assert (await get(client, f"/api/runs/{waiting}/compare/base"))["base_run_id"] == later


async def test_the_default_base_says_whether_it_is_the_previous_run(client):
    """previous：基准是不是紧挨着的上一次，语义同 pick_base——同一范围（同一定时任务，或同一工作流、同一运行类别）里比
    这次先发起、比基准后发起的运行一次都没有。失败、取消、还没结束、没封存的都算隔着；别的运行类别、别的定时任务（这次
    是定时任务发起的）、比这次晚发起的不在范围里，不算。运行详情「更多」里的入口按它写「与上一次对比」还是「与上一次
    成功的运行对比」，和触发历史里同名的链接一个说法"""
    day = timedelta(days=1)

    async def pick(run_id: str) -> tuple[str, str, bool]:
        body = await get(client, f"/api/runs/{run_id}/compare/base")
        return body["base_run_id"], body["reason"], body["previous"]

    # 同一定时任务：紧挨着的上一次就是成功的
    workflow = await new_workflow()
    first = await bare(workflow, T0, schedule_id="sched-weekly")
    await bare(workflow, T0 + day)                                       # 手动发起的：不在同一定时任务的范围里
    await bare(workflow, T0 + day, schedule_id="sched-daily", status="failed")  # 别的定时任务失败的：同上
    second = await bare(workflow, T0 + 2 * day, schedule_id="sched-weekly")
    await bare(workflow, T0 + 3 * day, schedule_id="sched-weekly", status="failed")  # 比 second 晚发起
    assert await pick(second) == (first, "same_schedule", True)

    # 中间隔着一次没成功的（失败、取消、还没结束、结束了没封存）：基准是再往前成功的那一次，不是紧挨着的上一次
    for between in ({"status": "failed"}, {"status": "cancelled"}, {"status": "running", "sealed": False},
                    {"status": "interrupted", "sealed": False}, {"sealed": False}):
        workflow = await new_workflow()
        first = await bare(workflow, T0, schedule_id="sched-weekly")
        await bare(workflow, T0 + day, schedule_id="sched-weekly", **between)
        current = await bare(workflow, T0 + 2 * day, schedule_id="sched-weekly")
        assert await pick(current) == (first, "same_schedule", False), between

    # 同一工作流、同一运行类别（手动发起的）：别的运行类别不算隔着，同一类别里定时任务发起的失败的那次算
    workflow = await new_workflow()
    first = await bare(workflow, T0)
    await bare(workflow, T0 + day, run_class="exploratory", status="failed")
    second = await bare(workflow, T0 + 2 * day)
    assert await pick(second) == (first, "same_workflow", True)
    await bare(workflow, T0 + 3 * day, schedule_id="sched-weekly", status="failed")
    third = await bare(workflow, T0 + 4 * day)
    assert await pick(third) == (second, "same_workflow", False)
    # 定时任务的第一次退回同一工作流：隔没隔着按退回的那个范围算
    lone = await bare(workflow, T0 + 5 * day, schedule_id="sched-monthly")
    assert await pick(lone) == (third, "same_workflow", True)


# --------------------------------------------------------------------------
# 真跑：虚构的 SQLite 库，两次运行之间改了数、换了日期参数
# --------------------------------------------------------------------------

E2E_SOURCE = "cmp_e2e"
E2E_SQL = ("SELECT region, SUM(amount) AS gmv FROM orders WHERE day BETWEEN '{{ input.start }}' AND '{{ input.end }}' "
           "GROUP BY region ORDER BY region")
WEEK36_INPUT = {"start": "2026-08-31", "end": "2026-09-06"}
WEEK37_INPUT = {"start": "2026-09-07", "end": "2026-09-13"}


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def orders(tmp_path):
    path = tmp_path / "orders.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, day TEXT, region TEXT, amount REAL);")
    db.executemany("INSERT INTO orders (day, region, amount) VALUES (?, ?, ?)", [
        ("2026-09-01", "东区", 1200.5), ("2026-09-02", "南区", 800.0), ("2026-09-03", "西区", 600.0),
        ("2026-09-08", "东区", 900.0), ("2026-09-09", "南区", 650.0), ("2026-09-10", "西区", 640.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=E2E_SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path, source_id
    await drop_source(source_id)


def e2e_graph():
    return chain(
        node("start", "input", "开始"),
        node("fetch", "tool", "区域取数", tool=f"db_query__{E2E_SOURCE}", args={"sql": E2E_SQL}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1", metrics=[
            {"id": "east", "name": "东区金额", "unit": "元", "decimals": 1, "format": "thousands",
             "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("out", "output", "成果", fields=[{"name": "指标", "value": "{{ nodes.card.text }}"}]),
    )


async def finished_run(workflow_id: str, inputs: dict) -> str:
    run = await run_manager.start(graph=e2e_graph(), input_payload=inputs, workflow_id=workflow_id,
                                  workflow_name="区域周报")
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return run.id
    raise AssertionError(f"运行 {run.id} 没有结束：{row.status}")


async def test_real_runs_split_data_and_parameter_changes(client, engine_up, orders):
    path, source_id = orders
    workflow = await new_workflow()
    first = await finished_run(workflow, WEEK36_INPUT)
    db = sqlite3.connect(path)
    db.execute("UPDATE orders SET amount = 1500.5 WHERE day = '2026-09-01'")
    db.commit()
    db.close()
    await engines.invalidate(source_id)
    second = await finished_run(workflow, WEEK36_INPUT)
    third = await finished_run(workflow, WEEK37_INPUT)

    body = await get(client, f"/api/runs/{second}/compare?base={first}")
    [east] = body["metrics"]
    assert (east["classification"], east["base"], east["target"], east["delta"]) == ("data_only", 1200.5, 1500.5, 300.0)
    [fetch] = body["queries"]
    assert (fetch["node_id"], fetch["structure"], fetch["base_rows"], fetch["target_rows"]) == ("fetch", "same", 3, 3)
    assert east["inputs"][0]["base_artifact"] == fetch["base_artifact"]
    # 东区金额只是三个区域里的一格：各区域的变化照列，占比为空
    assert [(r["key"], r["delta"]) for r in east["attribution"]["rows"]] == [
        (["东区"], 300.0), (["南区"], 0), (["西区"], 0)]

    body = await get(client, f"/api/runs/{third}/compare?base={second}")
    [east] = body["metrics"]
    assert east["classification"] == "params_only" and east["target"] == 900.0
    [fetch] = body["queries"]
    assert fetch["structure"] == "params_only"
    assert fetch["literals"] == [{"base": "'2026-08-31'", "target": "'2026-09-07'"},
                                 {"base": "'2026-09-06'", "target": "'2026-09-13'"}]
    assert body["template"]["same_graph"] is True
    assert any("start" in n for n in body["notes"]), body["notes"]

    body = await get(client, f"/api/runs/{third}/compare/base")
    assert (body["base_run_id"], body["reason"]) == (second, "same_workflow")
    json.dumps(body, allow_nan=False)
