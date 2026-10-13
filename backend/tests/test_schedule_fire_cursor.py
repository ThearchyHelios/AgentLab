"""触发历史的翻页游标（GET /api/schedules/{id}/fires 的 next_before）照原样拼进查询串也能翻页。

2026-10 验收：next_before 是「2026-10-06T14:37:03.376320+00:00」这样带 +00:00 的时间，照接口说明「原样传回」拼进
地址（不做百分号编码），「+」在查询串里被解成空格，回 422「翻页位置需要是接口返回的 next_before」。界面用
URLSearchParams 编码，不受影响；写脚本、做集成的照说明写就会撞上。现在 next_before 写成 Z 结尾（放进地址不用编码，
和运行列表的游标一个写法），解析时 Z、+00:00、被解成空格的「 00:00」都认：老的游标、编码过的照样能用。

定时任务、工作流、触发记录都是虚构的。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Schedule, ScheduleFire, Workflow
from app.main import app

UTC = timezone.utc
#: 五次触发，记录时间各差一天、带微秒（游标得精确到微秒，翻页才不重不漏）
CREATED = [datetime(2031, 3, 1 + i, 1, 0, 1, 250_000 + i, tzinfo=UTC) for i in range(5)]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def schedule_id() -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"灯塔周报-{uuid.uuid4().hex[:6]}", graph={"nodes": [], "edges": []})
        session.add(wf)
        await session.flush()
        row = Schedule(name="灯塔周报 · 每周", workflow_id=wf.id, workflow_name=wf.name, cron="0 9 * * 1",
                       timezone="Asia/Shanghai", run_class="exploratory", input_template={}, enabled=False)
        session.add(row)
        await session.flush()
        for i, at in enumerate(CREATED):
            session.add(ScheduleFire(schedule_id=row.id, fire_at=at.replace(second=0, microsecond=0), kind="scheduled",
                                     status="skipped_overlap", reason=f"第 {i} 次", created_at=at))
        await session.commit()
        return row.id


def _reasons(body: dict) -> list[str]:
    return [f["reason"] for f in body["items"]]


async def test_next_before_needs_no_encoding(client, schedule_id):
    first = (await client.get(f"/api/schedules/{schedule_id}/fires?limit=2")).json()
    assert _reasons(first) == ["第 4 次", "第 3 次"]
    cursor = first["next_before"]
    # UTC 写成 Z 结尾、精确到微秒：没有「+」，放进地址不用编码
    assert cursor == "2031-03-04T01:00:01.250003Z"
    # 照原样拼进查询串（不做百分号编码）
    second = await client.get(f"/api/schedules/{schedule_id}/fires?limit=2&before={cursor}")
    assert second.status_code == 200, second.text
    assert _reasons(second.json()) == ["第 2 次", "第 1 次"]
    last = await client.get(f"/api/schedules/{schedule_id}/fires?limit=2&before={second.json()['next_before']}")
    assert last.status_code == 200 and _reasons(last.json()) == ["第 0 次"] and last.json()["next_before"] is None


@pytest.mark.parametrize("written", [
    "2031-03-04T01:00:01.250003+00:00",    # 老的游标照原样拼进地址：「+」被解成空格
    "2031-03-04T01:00:01.250003%2B00:00",  # 老的游标编码过
    "2031-03-04T01:00:01.250003Z",
    "2031-03-04T09:00:01.250003%2B08:00",  # 别的时区的同一时刻
    "2031-03-04T09:00:01.250003+08:00",    # ……照原样拼进地址
])
async def test_every_way_the_cursor_arrives_reads_the_same_page(client, schedule_id, written):
    got = await client.get(f"/api/schedules/{schedule_id}/fires?limit=2&before={written}")
    assert got.status_code == 200, got.text
    assert _reasons(got.json()) == ["第 2 次", "第 1 次"]


@pytest.mark.parametrize("written", ["昨天", "2031-03-04T01:00:01 昨天", "2031-03-04T01:00:01 00:00:00"])
async def test_a_cursor_that_is_not_a_time_is_still_refused_in_chinese(client, schedule_id, written):
    got = await client.get(f"/api/schedules/{schedule_id}/fires", params={"before": written})
    assert got.status_code == 422 and got.json()["field"] == "before"
    assert got.json()["detail"].startswith("翻页位置需要是接口返回的 next_before")
