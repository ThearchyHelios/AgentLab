"""事件触发的定时任务接口（api/schedules.py，docs/design/orchestration-round/00-总方案.md §4）。

- 建任务收 trigger_kind（cron 缺省，字段和行为一字不变）/ trigger：事件触发时 cron 存空串、下一次触发为空，目标只能是工作流；
  核对对象（表格在、是按期累积的上传表格；上游工作流在、已发布；数据源在；只认的应用在），输入模板按事件种类核对 {{ event.* }}；
  不对回 422 {detail（中文）, field}
- 防环：workflow.finished 串成环（A→B→A、自己触发自己）拒收，说清是哪几个工作流；改触发条件、改目标同样查
- 返回带 trigger_kind、trigger、trigger_text（给人看的触发说明，也写进 description_text）、trigger_state（几条事件在等、
  去抖到什么时候、调度开没开）；触发历史里事件触发的那几次带 event（事件的说明）
- 预览按一个示例事件渲染（上游最近一次成功的正式运行的出口字段……）；变量清单按事件种类给 {{ event.* }}；
  立即运行按示例事件发起，还没发生过这种事件、模板又用到 {{ event.* }} 的照实记未能发起
- 触发方式建好之后不能改；Webhook 照旧可配

工作流、表格、数据源、应用都是虚构的。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Schedule, TableRecipe, Workflow, WorkflowVersion
from app.db.models_apps import App
from app.db.models_triggers import TriggerDelivery, TriggerEvent
from app.engine.launch import launch_run
from app.engine.runner import run_manager
from app.main import app

HOOK = "https://hooks.example.com/agentlab/inbox-7f2e"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(TriggerDelivery))
        await session.execute(delete(TriggerEvent))
        await session.execute(delete(Schedule))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(fields=(), value="完成") -> dict:
    return {"nodes": [node("start", "input", fields=list(fields)),
                      node("out", "output", fields=[{"name": "r", "value": value}])],
            "edges": [{"source": "start", "target": "out"}]}


async def workflow(name: str, *, published: bool = True, fields=(), value: str = "完成") -> tuple[str, str]:
    name = f"{name}-{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        wf = Workflow(name=name, graph=graph(fields, value), status="published" if published else "draft",
                      published_version=1 if published else None)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph(fields, value)))
        await session.commit()
        return wf.id, name


async def upload_table(name: str, *, mode: str = "accumulate") -> str:
    async with SessionLocal() as session:
        source = DataSource(name=f"{name}-{uuid.uuid4().hex[:6]}", kind="sqlite", origin="upload", readonly=True)
        session.add(source)
        await session.flush()
        recipe = TableRecipe(source_id=source.id, seq=1, recipe={"mode": mode}, status="active")
        session.add(recipe)
        await session.flush()
        source.current_recipe_id = recipe.id
        await session.commit()
        return source.id


async def manual_source(name: str = "门店库") -> str:
    async with SessionLocal() as session:
        source = DataSource(name=f"{name}-{uuid.uuid4().hex[:6]}", kind="sqlite", database=":memory:")
        session.add(source)
        await session.commit()
        return source.id


def body(workflow_id: str, trigger: dict, **extra) -> dict:
    return {"name": "下游汇总", "workflow_id": workflow_id, "trigger_kind": "event", "trigger": trigger,
            "timezone": "Asia/Shanghai", **extra}


async def settle(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


# --------------------------------------------------------------------------
# 建、读
# --------------------------------------------------------------------------


async def test_create_table_version_schedule(client):
    table = await upload_table("门店销售")
    wf, _ = await workflow("月报", fields=[{"name": "month", "required": True}])
    r = await client.post("/api/schedules", json=body(wf, {"event": "table.version", "source_id": table,
                                                          "period": "new"},
                                                     input_template={"month": "{{ event.period }}"},
                                                     webhook={"url": HOOK}))
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["trigger_kind"], s["cron"], s["next_run_at"], s["target_kind"]) == ("event", "", None, "workflow")
    assert {k: v for k, v in s["trigger"].items() if k != "subject_name"} == {
        "event": "table.version", "source_id": table, "period": "new", "debounce_s": 60}
    assert s["trigger"]["subject_name"].startswith("门店销售-")             # 保存时记下的对象名
    assert s["trigger_text"].startswith("上传表格「门店销售-") and "（只认新增的一期；去抖 1 分钟）" in s["trigger_text"]
    assert s["description_text"] == s["trigger_text"]
    assert s["trigger_state"] == {"events": 0, "waiting": 0, "due_at": None, "scheduler_on": False}
    assert s["webhook"]["url_hint"].startswith("hooks.example.com")
    async with SessionLocal() as session:
        row = await session.get(Schedule, s["id"])
    assert (row.trigger_kind, row.cron, row.next_run_at) == ("event", "", None)
    # 列表里同一份
    listed = {x["id"]: x for x in (await client.get("/api/schedules")).json()["items"]}
    assert listed[s["id"]]["trigger_text"] == s["trigger_text"]


async def test_cron_schedules_are_unchanged_and_say_so(client):
    wf, _ = await workflow("周报", fields=[{"name": "stat_date", "required": True}])
    r = await client.post("/api/schedules", json={"name": "周一早报", "workflow_id": wf, "cron": "0 9 * * 1",
                                                  "timezone": "Asia/Shanghai",
                                                  "input_template": {"stat_date": "{{ schedule.yesterday }}"}})
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["trigger_kind"], s["trigger"], s["trigger_text"], s["trigger_state"]) == ("cron", None, None, None)
    assert s["description_text"] == "每周一 09:00（Asia/Shanghai）" and s["next_run_at"]


@pytest.mark.parametrize("make, trigger, says", [
    ("replace", {"event": "table.version"}, "不是按期累积的上传表格"),
    ("manual", {"event": "table.version"}, "不是上传表格"),
    ("gone", {"event": "table.version"}, "上传表格不存在"),
    ("draft", {"event": "workflow.finished"}, "还没有发布版本"),
    ("gone", {"event": "source.changed"}, "数据源不存在"),
])
async def test_trigger_objects_are_checked(client, make, trigger, says):
    target, _ = await workflow("汇总")
    if make == "replace":
        subject = await upload_table("门店销售", mode="replace")
    elif make == "manual":
        subject = await manual_source()
    elif make == "draft":
        subject, _ = await workflow("草稿日报", published=False)
    else:
        subject = "nope"
    key = "workflow_id" if trigger["event"] == "workflow.finished" else "source_id"
    r = await client.post("/api/schedules", json=body(target, {**trigger, key: subject}))
    assert r.status_code == 422 and r.json()["field"] == "trigger" and says in r.json()["detail"], r.text


async def test_event_schedule_rules(client):
    src = await manual_source()
    wf, _ = await workflow("汇总", fields=[{"name": "n"}])
    trigger = {"event": "source.changed", "source_id": src}
    cases = [
        ({"cron": "0 9 * * 1"}, "cron", "不写 cron"),
        ({"target_kind": "eval_suite", "target_id": "suite-1", "workflow_id": ""}, "target_kind", "只能指向工作流"),
        ({"input_template": {"n": "{{ event.period }}"}}, "input_template", "event.counts.new"),
        ({"trigger_kind": "hourly"}, "trigger_kind", "定时或事件触发"),
        ({"trigger": {**trigger, "debounce_s": 999999}}, "debounce_s", "去抖"),
    ]
    for extra, field, says in cases:
        r = await client.post("/api/schedules", json={**body(wf, trigger), **extra})
        assert r.status_code == 422, (extra, r.text)
        assert r.json()["field"] == field and says in r.json()["detail"], (extra, r.json())


async def test_unknown_app_filter_is_refused(client):
    a, _ = await workflow("日报")
    b, _ = await workflow("汇总")
    r = await client.post("/api/schedules", json=body(b, {"event": "workflow.finished", "workflow_id": a,
                                                         "app_id": "nope"}))
    assert r.status_code == 422 and "只认的应用不存在" in r.json()["detail"]
    async with SessionLocal() as session:
        shop = App(name="门店看板")
        session.add(shop)
        await session.commit()
        app_id = shop.id
    r = await client.post("/api/schedules", json=body(b, {"event": "workflow.finished", "workflow_id": a,
                                                         "app_id": app_id}))
    assert r.status_code == 201 and "只认应用「门店看板」发起的" in r.json()["trigger_text"]


# --------------------------------------------------------------------------
# 防环
# --------------------------------------------------------------------------


async def test_cycles_are_refused_when_saving(client):
    a, a_name = await workflow("日报")
    b, b_name = await workflow("汇总")
    c, c_name = await workflow("全国")
    first = await client.post("/api/schedules", json=body(b, {"event": "workflow.finished", "workflow_id": a}))
    assert first.status_code == 201
    second = await client.post("/api/schedules", json=body(c, {"event": "workflow.finished", "workflow_id": b}))
    assert second.status_code == 201
    loop = await client.post("/api/schedules", json=body(a, {"event": "workflow.finished", "workflow_id": c}))
    assert loop.status_code == 422 and loop.json()["field"] == "trigger"
    assert f"「{c_name}」→「{a_name}」→「{b_name}」→「{c_name}」" in loop.json()["detail"], loop.json()
    own = await client.post("/api/schedules", json=body(a, {"event": "workflow.finished", "workflow_id": a}))
    assert own.status_code == 422 and f"「{a_name}」→「{a_name}」" in own.json()["detail"]
    # 停用着的也算：启用不该绕过这道检查
    s2 = second.json()
    off = await client.patch(f"/api/schedules/{s2['id']}", json={"enabled": False, "version": s2["version"]})
    assert off.status_code == 200 and off.json()["next_run_at"] is None
    assert (await client.post("/api/schedules", json=body(a, {"event": "workflow.finished", "workflow_id": c}))
            ).status_code == 422
    # 改目标、改触发条件造出环：同样拒
    s1 = first.json()
    moved = await client.patch(f"/api/schedules/{s1['id']}", json={
        "trigger": {"event": "workflow.finished", "workflow_id": c}, "version": s1["version"]})
    assert moved.status_code == 422 and "循环触发" in moved.json()["detail"]
    retarget = await client.patch(f"/api/schedules/{s2['id']}", json={"workflow_id": a, "version": off.json()["version"]})
    assert retarget.status_code == 422 and "循环触发" in retarget.json()["detail"]


# --------------------------------------------------------------------------
# 改
# --------------------------------------------------------------------------


async def test_patch_event_schedule(client):
    src = await manual_source()
    wf, _ = await workflow("汇总", fields=[{"name": "n"}])
    s = (await client.post("/api/schedules", json=body(wf, {"event": "source.changed", "source_id": src}))).json()
    r = await client.patch(f"/api/schedules/{s['id']}", json={"trigger_kind": "cron", "version": s["version"]})
    assert r.status_code == 422 and r.json()["field"] == "trigger_kind"
    r = await client.patch(f"/api/schedules/{s['id']}", json={"cron": "0 9 * * *", "version": s["version"]})
    assert r.status_code == 422 and r.json()["field"] == "cron"
    r = await client.patch(f"/api/schedules/{s['id']}", json={
        "trigger": {"event": "source.changed", "source_id": src, "debounce_s": 300},
        "input_template": {"n": "{{ event.counts.new }}"}, "version": s["version"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["trigger"]["debounce_s"] == 300 and "去抖 5 分钟" in out["trigger_text"]
    assert out["version"] == s["version"] + 1 and out["next_run_at"] is None and out["cron"] == ""
    # 整份表单带回来（cron 空串、触发条件没变）照常保存
    same = await client.patch(f"/api/schedules/{s['id']}", json={
        "name": "变化处理", "cron": "", "trigger": out["trigger"], "trigger_kind": "event", "version": out["version"]})
    assert same.status_code == 200 and same.json()["name"] == "变化处理"
    # 模板里换成别的事件的变量：拒
    bad = await client.patch(f"/api/schedules/{s['id']}", json={
        "input_template": {"n": "{{ event.outputs.r }}"}, "version": same.json()["version"]})
    assert bad.status_code == 422 and bad.json()["field"] == "input_template"


# --------------------------------------------------------------------------
# 预览、变量、立即运行、触发历史
# --------------------------------------------------------------------------


async def test_preview_and_run_now_with_a_sample_event(client):
    a, a_name = await workflow("日报", value="日报完成")
    b, _ = await workflow("汇总", fields=[{"name": "basis", "required": True}])
    trigger = {"event": "workflow.finished", "workflow_id": a}
    template = {"basis": "{{ event.outputs.r }} · {{ event.workflow }}"}
    # 上游还没成功过：示例值，照实说
    r = await client.post("/api/schedules/preview", json={"trigger_kind": "event", "trigger": trigger,
                                                           "timezone": "Asia/Shanghai", "input_template": template,
                                                           "workflow_id": b})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["next"] == [] and p["event"]["real"] is False and p["event"]["text"].startswith(f"工作流「{a_name}」")
    s = (await client.post("/api/schedules", json=body(b, trigger, input_template=template))).json()
    early = (await client.post(f"/api/schedules/{s['id']}/run")).json()
    assert early["run_id"] is None and early["fire"]["status"] == "failed_to_start"
    assert "还没有发生过这种事件" in early["fire"]["reason"] and early["fire"]["kind"] == "manual"
    # 上游成功过一次：预览、立即运行都按它
    async with SessionLocal() as session:
        up = await launch_run(session, workflow_id=a, run_class="formal", input_payload={}, actor="检查员",
                              origin_kind="launcher")
    await settle(up.id)
    p = (await client.post("/api/schedules/preview", json={"trigger_kind": "event", "trigger": trigger,
                                                            "timezone": "Asia/Shanghai", "input_template": template,
                                                            "workflow_id": b})).json()
    assert p["inputs"] == {"basis": f"日报完成 · {a_name}"} and p["event"]["real"] is True
    assert p["description_text"].startswith(f"工作流「{a_name}」的正式运行成功结束时触发")
    await asyncio.sleep(1.05)               # 立即运行按秒去重：上一次落在同一秒就交回那一条
    run = (await client.post(f"/api/schedules/{s['id']}/run")).json()
    assert run["run_id"] and run["fire"]["kind"] == "manual" and run["fire"]["event"]["sample"] is True
    done = await settle(run["run_id"])
    assert done.input == {"basis": f"日报完成 · {a_name}"}
    assert done.origin["trigger"]["sample"] is True and done.origin["trigger"]["event"] == "workflow.finished"
    assert done.origin["trigger"]["real"] is True and done.origin["trigger"]["chain"] == [up.id]
    # 应用页印章：立即运行按示例事件发起的不说「由事件触发」
    from app.apps import resolve

    async with SessionLocal() as session:
        said = await resolve._from_text(session, resolve.Source(id="v", kind="latest_sealed", state="ok", run=done))
    assert said.startswith("定时任务「下游汇总」第 ")
    # 触发历史：事件的说明照写
    [newest, *_] = (await client.get(f"/api/schedules/{s['id']}/fires")).json()["items"]
    assert newest["event"]["text"].startswith(f"工作流「{a_name}」的正式运行成功结束（#{up.id[:6]}）")


async def test_variables_for_an_event(client):
    names = [v["name"] for v in (await client.get(
        "/api/schedules/variables", params={"trigger_kind": "event", "event": "workflow.finished"})).json()]
    assert "event.outputs.<字段名>" in names and "event.run_id" in names and "schedule.yesterday" in names
    assert "schedule.last_fire_time" not in names
    plain = [v["name"] for v in (await client.get("/api/schedules/variables")).json()]
    assert "schedule.last_fire_time" in plain and not any(n.startswith("event.") for n in plain)
    bad = await client.get("/api/schedules/variables", params={"trigger_kind": "event", "event": "nope"})
    assert bad.status_code == 422


async def test_trigger_state_and_event_fires(client):
    """调度关着：事件留在表里，接口照实说几条在等、调度没开；派发之后触发历史里是 kind=event、带事件的说明"""
    from app.scheduler import outbox
    from app.db.base import utcnow
    from datetime import timedelta

    a, a_name = await workflow("日报")
    b, _ = await workflow("汇总")
    s = (await client.post("/api/schedules", json=body(b, {"event": "workflow.finished", "workflow_id": a,
                                                           "debounce_s": 0}))).json()
    async with SessionLocal() as session:
        up = await launch_run(session, workflow_id=a, run_class="formal", input_payload={}, actor="检查员",
                              origin_kind="launcher")
    await settle(up.id)
    state = (await client.get(f"/api/schedules/{s['id']}")).json()["trigger_state"]
    assert state == {"events": 1, "waiting": 0, "due_at": None, "scheduler_on": False}
    [fire] = await outbox.dispatch_due(utcnow() + timedelta(seconds=1))
    await settle(fire.run_id)
    got = (await client.get(f"/api/schedules/{s['id']}")).json()
    assert got["trigger_state"]["events"] == 0 and got["last_fire"]["kind"] == "event"
    assert got["last_fire"]["event"]["text"] == f"工作流「{a_name}」的正式运行成功结束（#{up.id[:6]}）"
    assert got["last_fire"]["inputs"] is None                  # 工作流目标的输入不在触发历史里列


async def test_trigger_options(client):
    table = await upload_table("门店销售")
    flat = await upload_table("门店名单", mode="replace")
    wf, wf_name = await workflow("日报")
    draft, _ = await workflow("草稿", published=False)
    r = await client.get("/api/schedules/trigger-options")
    assert r.status_code == 200
    out = r.json()
    events = {e["event"]: e for e in out["events"]}
    assert list(events) == ["table.version", "workflow.finished", "source.changed"]
    tables = {o["id"]: o for o in events["table.version"]["objects"]}
    assert tables[table]["ok"] is True and tables[flat]["ok"] is False and "按期累积" in tables[flat]["reason"]
    flows = {o["id"]: o for o in events["workflow.finished"]["objects"]}
    assert flows[wf] == {"id": wf, "name": wf_name, "ok": True, "reason": None}
    assert flows[draft]["ok"] is False and "发布" in flows[draft]["reason"]
    assert "hook" in [o["kind"] for o in out["origins"]] and out["max_depth"] == 5
    assert out["default_debounce_s"] == 60


async def test_include_rejected_and_backfill_through_the_api(client):
    """复核 M1、C5：上游工作流的触发缺省不认途经驳回的、应用补算的，说法照实写；勾了「也认」的存下来、说法跟着变；
    别的事件带这两项拒收"""
    a, a_name = await workflow("日报")
    b, _ = await workflow("汇总", fields=[{"name": "rej"}])
    plain = (await client.post("/api/schedules", json=body(b, {"event": "workflow.finished", "workflow_id": a},
                                                         input_template={"rej": "{{ event.rejected }}"}))).json()
    assert "途经驳回、应用补算的不算" in plain["trigger_text"] and "include_rejected" not in plain["trigger"]
    r = await client.patch(f"/api/schedules/{plain['id']}", json={
        "trigger": {"event": "workflow.finished", "workflow_id": a, "include_rejected": True}, "version": plain["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["trigger"]["include_rejected"] is True and "途经驳回的也算；应用补算的不算" in r.json()["trigger_text"]
    src = await upload_table("门店销售")
    bad = await client.post("/api/schedules", json=body(b, {"event": "table.version", "source_id": src,
                                                           "include_backfill": True}))
    assert bad.status_code == 422 and bad.json()["field"] == "trigger"
