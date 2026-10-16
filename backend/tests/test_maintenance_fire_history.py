"""定时任务页的触发历史认得数据源维护（复核 W4R-05）。

数据源维护的一次触发建的是探查、剖析任务，没有运行：触发记录的 inputs（{steps, jobs, skipped}，app/data/source_maintenance）
要交到触发历史、页头的「上次触发」和「立即运行」的答复里，界面才写得出建了哪几个任务、哪几步没执行，不再写「未发起运行」。
工作流目标的触发不给 inputs（那是按模板渲染的工作流输入，触发历史不列）。

库、表、名字都是虚构的。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal, utcnow
from app.db.models import Schedule, ScheduleFire
from app.main import app
from app.scheduler import service
from tests.fixtures.sources import drop_source
from tests.test_source_maintenance import _schedule, _source, _wait_jobs


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def made():
    ids: list[str] = []
    yield ids
    async with SessionLocal() as session:
        await session.execute(delete(Schedule).where(Schedule.id.in_(ids)))
        await session.commit()


async def test_a_maintenance_fire_carries_the_jobs_it_started_and_the_steps_not_run(client, tmp_path, made):
    sid = await _source(client, tmp_path / "shop.db", profile=False)
    try:
        schedule_id = await _schedule(sid)
        made.append(schedule_id)
        ran = (await client.post(f"/api/schedules/{schedule_id}/run")).json()
        fire = ran["fire"]
        assert ran["run_id"] is None and fire["status"] == "started" and fire["run_id"] is None
        jobs = fire["inputs"]["jobs"]
        assert [j["kind"] for j in jobs] == ["introspect"] and jobs[0]["id"]
        assert {s["step"] for s in fire["inputs"]["skipped"]} == {"profile_needed", "observe"}
        async with SessionLocal() as session:
            await _wait_jobs(await session.get(ScheduleFire, fire["id"]))

        listed = (await client.get(f"/api/schedules/{schedule_id}/fires")).json()["items"]
        assert [f["id"] for f in listed] == [fire["id"]]
        assert listed[0]["inputs"] == fire["inputs"]
        assert set(listed[0]["inputs"]) == {"steps", "jobs", "skipped"}
        head = (await client.get(f"/api/schedules/{schedule_id}")).json()
        assert head["last_fire"]["inputs"]["jobs"] == jobs
        rows = (await client.get("/api/schedules")).json()["items"]
        assert [s["last_fire"]["inputs"]["jobs"] for s in rows if s["id"] == schedule_id] == [jobs]
    finally:
        await drop_source(sid)


async def test_a_workflow_fire_does_not_list_its_rendered_inputs(client, made):
    async with SessionLocal() as session:
        row = Schedule(name="虚构周报 · 每周", cron="0 9 * * 1", timezone="Asia/Shanghai",
                       **service.target_columns("workflow", "wf-fictional"), input_template={"date": "{{ schedule.yesterday }}"})
        session.add(row)
        await session.flush()
        session.add(ScheduleFire(schedule_id=row.id, fire_at=utcnow().replace(microsecond=0), kind="manual",
                                 status="failed_to_start", reason="虚构的原因", inputs={"date": "2026-10-15"}))
        await session.commit()
        made.append(row.id)
    [got] = (await client.get(f"/api/schedules/{row.id}/fires")).json()["items"]
    assert got["inputs"] is None


def test_the_steps_and_their_names_on_the_schedules_page_match_the_server():
    """定时任务页照服务端的步骤和叫法写（types.ts 的 ScheduleMaintenanceStep、SCHEDULE_TEXT.maintenanceStep）：服务端加了一步，
    界面没跟上就写出英文原值。直接读两份 .ts 比（同 test_frontend_type_unions 的写法）。"""
    import re
    from pathlib import Path

    from app.data.source_maintenance import STEP_LABEL, STEPS

    src = Path(__file__).resolve().parents[2] / "frontend" / "src"
    types = (src / "types.ts").read_text(encoding="utf-8")
    union = re.search(r"export type ScheduleMaintenanceStep =([^\n]*)", types)
    assert union is not None and re.findall(r"'([^']+)'", union.group(1)) == list(STEPS)
    terms = (src / "lib" / "terms.ts").read_text(encoding="utf-8")
    labels = re.search(r"\n\s*maintenanceStep: \{([^}]*)\}", terms)
    assert labels is not None and dict(re.findall(r"(\w+): '([^']+)'", labels.group(1))) == STEP_LABEL
