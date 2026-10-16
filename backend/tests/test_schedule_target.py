"""定时任务的目标（docs/design/next-round/00-总方案.md X4：定时只有一套）。

骨架给 Schedule 加了 target_kind / target_id；workflow_id 是老表上的非空约束，只加不改的迁移放宽不了。规矩只在
scheduler/service.py 的一处封装里：workflow 目标的 target_id 同 workflow_id；不是工作流的目标 workflow_id 存空串。
这里守：

- 存量任务（target_kind 是缺省的 workflow、target_id 为空）照旧按 workflow_id 发起，行为一字不变；
- 建任务、改工作流都经同一处封装写目标，两列对得上；
- fire_once 按目标种类分派。这一波只实现 workflow，别的种类照实记「未能发起」并写明原因，不发起运行、不悄悄当成
  工作流去跑空串；
- 接口这一波不暴露新种类（接口返回的形状不变，测试见 test_schedules_api.py 的 SCHEDULE_KEYS）。

工作流、任务名都是虚构的。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.db.base import SessionLocal
from app.db.models import Run, Schedule, ScheduleFire, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.main import app
from app.scheduler import service

UTC = timezone.utc
NINE = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await run_manager.shutdown()


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _graph() -> dict:
    return {"nodes": [_node("start", "input", fields=[{"name": "stat_date", "required": True}]),
                      _node("out", "output", fields=[{"name": "r", "value": "完成 {{ input.stat_date }}"}])],
            "edges": [{"source": "start", "target": "out"}]}


async def _workflow() -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"门店日报-{uuid.uuid4().hex[:6]}", graph=_graph(), status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=_graph()))
        await session.commit()
        return wf.id


def test_target_of_a_legacy_row_reads_the_workflow():
    """加列之前建的任务：target_kind 缺省 workflow、target_id 为空，目标就是 workflow_id。"""
    row = Schedule(name="老任务", workflow_id="wf-legacy", cron="0 9 * * *")
    assert service.target_of(row) == ("workflow", "wf-legacy")
    row.target_kind = "workflow"
    row.target_id = None
    assert service.target_of(row) == ("workflow", "wf-legacy")


def test_target_columns_keep_the_two_columns_in_step():
    assert service.target_columns("workflow", "wf-1") == {"target_kind": "workflow", "target_id": "wf-1",
                                                           "workflow_id": "wf-1"}
    # 非工作流目标：workflow_id 存空串（老表的非空约束），目标 id 只在 target_id
    assert service.target_columns("eval_suite", "suite-1") == {"target_kind": "eval_suite", "target_id": "suite-1",
                                                                "workflow_id": ""}
    row = Schedule(name="评测", cron="0 9 * * *", **service.target_columns("eval_suite", "suite-1"))
    assert service.target_of(row) == ("eval_suite", "suite-1")
    with pytest.raises(ValueError):
        service.target_columns("nonsense", "x")
    with pytest.raises(ValueError):
        service.target_columns("workflow", "")


async def test_creating_and_retargeting_through_the_api_write_both_columns():
    wf_a, wf_b = await _workflow(), await _workflow()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        made = await client.post("/api/schedules", json={
            "name": "每日早报", "workflow_id": wf_a, "cron": "0 9 * * *", "timezone": "Asia/Shanghai",
            "input_template": {"stat_date": "{{ schedule.yesterday }}"}})
        assert made.status_code == 201, made.text
        body = made.json()
        assert "target_kind" not in body and "target_id" not in body, "这一波接口不暴露新种类"
        async with SessionLocal() as session:
            row = await session.get(Schedule, body["id"])
            assert (row.target_kind, row.target_id, row.workflow_id) == ("workflow", wf_a, wf_a)
        moved = await client.patch(f"/api/schedules/{body['id']}", json={"workflow_id": wf_b, "version": body["version"]})
        assert moved.status_code == 200, moved.text
    async with SessionLocal() as session:
        row = await session.get(Schedule, body["id"])
        assert (row.target_kind, row.target_id, row.workflow_id) == ("workflow", wf_b, wf_b)


async def test_a_legacy_schedule_still_fires_its_workflow():
    wf = await _workflow()
    async with SessionLocal() as session:
        row = Schedule(name="老任务", workflow_id=wf, cron="0 9 * * *", timezone="Asia/Shanghai", run_class="formal",
                       input_template={"stat_date": "{{ schedule.yesterday }}"}, next_run_at=NINE)
        session.add(row)
        await session.commit()
        schedule_id = row.id
    fire = await service.fire_once(schedule_id, NINE, "manual")
    assert fire is not None and fire.status == "started" and fire.run_id
    async with SessionLocal() as session:
        run = await session.get(Run, fire.run_id)
        assert run.workflow_id == wf and run.input == {"stat_date": "2026-10-04"}


async def test_a_target_kind_this_wave_cannot_fire_is_recorded_not_run():
    async with SessionLocal() as session:
        row = Schedule(name="题集回归", cron="0 9 * * *", timezone="Asia/Shanghai",
                       **service.target_columns("eval_suite", "suite-x"), next_run_at=NINE)
        session.add(row)
        await session.commit()
        schedule_id = row.id
        runs_before = await session.scalar(select(func.count()).select_from(Run))
    fire = await service.fire_once(schedule_id, NINE, "manual")
    assert fire is not None and fire.status == "failed_to_start" and fire.run_id is None
    assert fire.reason == service.TARGET_UNSUPPORTED.format(kind=service.TARGET_LABEL["eval_suite"])
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == runs_before, "不能发起任何运行"
        assert await session.scalar(select(func.count()).select_from(ScheduleFire)
                                    .where(ScheduleFire.schedule_id == schedule_id)) == 1
