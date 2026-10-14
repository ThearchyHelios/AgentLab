"""运行接口的来源和待办（D-r）：RunOut 带 origin_kind、origin、app_id、app_version；列表按来源筛（none 是来源未记录的
老运行）；/api/approvals 每项带对应的待办 id（回复改走 /api/tasks/{id}/respond），还没补上待办的为空。"""
from __future__ import annotations

import uuid

from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Approval, Run, Task
from tests.fakes import auth_env


async def test_run_out_carries_the_origin():
    await auth_env.reset()
    tag = uuid.uuid4().hex[:8]
    origin = {"kind": "app_entry", "app_id": "a1", "entry": "submit", "actor": "张三", "actor_verified": False}
    async with SessionLocal() as s:
        app_run = Run(workflow_name=f"来源-{tag}", graph={}, input={}, origin_kind="app_entry", origin=origin,
                      app_id="a1", app_version=3, run_class="formal")
        old = Run(workflow_name=f"来源-{tag}", graph={}, input={})
        s.add_all([app_run, old])
        await s.commit()
        ids = (app_run.id, old.id)
    try:
        async with auth_env.client() as c:
            got = (await c.get(f"/api/runs/{ids[0]}")).json()
            assert (got["origin_kind"], got["origin"], got["app_id"], got["app_version"]) == (
                "app_entry", origin, "a1", 3)
            listed = (await c.get("/api/runs", params={"q": f"来源-{tag}"})).json()
            assert {r["id"]: r["origin"] for r in listed} == {ids[0]: origin, ids[1]: None}
            none = (await c.get("/api/runs", params={"q": f"来源-{tag}", "origin_kind": "none"})).json()
            assert [r["id"] for r in none] == [ids[1]]
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Run).where(Run.id.in_(ids)))
            await s.commit()


async def test_each_approval_names_its_task():
    await auth_env.reset()
    async with SessionLocal() as s:
        run = Run(workflow_name="待办", graph={}, input={}, status="interrupted")
        s.add(run)
        await s.flush()
        with_task = Approval(run_id=run.id, node_id="gate", title="批一下")
        without = Approval(run_id=run.id, node_id="gate2", title="还没补待办")
        s.add_all([with_task, without])
        await s.flush()
        task = Task(run_id=run.id, node_id="gate", approval_id=with_task.id, title="批一下")
        s.add(task)
        await s.commit()
        run_id, expected = run.id, {with_task.id: task.id, without.id: None}
    try:
        async with auth_env.client() as c:
            items = (await c.get("/api/approvals", params={"run_id": run_id})).json()
        assert {i["id"]: i["task_id"] for i in items} == expected
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Run).where(Run.id == run_id))
            await s.commit()
