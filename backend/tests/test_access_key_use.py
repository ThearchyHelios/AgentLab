"""用访问密钥用应用（docs/design/next-round/04-对外集成与触达.md A3–A6）：封顶在使用者、幂等、留痕、回复待办、视图数据、
钩子的收件记录。

- 封顶：管理员的密钥也按使用者算——取工件时查询快照是遮过的、节点产出 403；有「查看全部实例」授权的服务账号能列全部实例、
  能下载报告，报告里遮罩的值写「已遮罩」；
- 幂等：同一个 Idempotency-Key 发起两次只有一个实例；轮换密钥后同一个键照样去重；处理到一半服务停了，同一个键认领回已
  发起的实例；不带键 400；同一个键办另一件事 422；有文件控件的入口 422；刷新视图同样要带键；
- 留痕：run.origin 里有 via=key、key_id、密钥名（在清单哈希的封存范围内）；task.responded 记了 via；
- 待办：个人密钥能回复审批、补充输入、编辑稿件；确认写入、双盲裁决、核实写入结果、工具审批 403（key_task_kind）；
- 视图数据（agentlab.viewdata/1）：印章、清单哈希、指标原值；遮罩的 value 为 null、state 为 masked，和页面同一个判断；
  按需视图没算过时 empty、不发起运行；改了页面布局形状不变；
- 钩子收件记录：只有管理权限能读，列出状态和原因、近 1 小时验签失败次数。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.artifact_store import graph_hash, put_json
from app.db.base import SessionLocal, utcnow
from app.db.models import (
    App, AppGrant, AppVersion, DataSource, Run, RunEvent, Task, TaskAssignment, Workflow, WorkflowVersion,
)
from app.db.models_apps import ApiReceipt
from app.engine.runner import run_manager
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def world():
    await auth_env.reset()
    await run_manager.setup()
    await auth_env.turn_on_local()
    await auth_env.personal_keys(True)
    made: dict[str, list[str]] = {"apps": [], "workflows": [], "sources": []}
    try:
        yield made
    finally:
        await run_manager.shutdown()
        async with SessionLocal() as s:
            await s.execute(delete(AppGrant).where(AppGrant.app_id.in_(made["apps"] or [""])))
            await s.execute(delete(App).where(App.id.in_(made["apps"] or [""])))
            await s.execute(delete(DataSource).where(DataSource.name.in_(made["sources"] or [""])))
            await s.commit()
        await auth_env.reset()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


ECHO = chain(node("start", "input", "开始", fields=[{"name": "amount", "type": "number"}, {"name": "who"}]),
             node("out", "output", "成果", fields=[{"name": "r", "value": "{{ input.who }}"}]))
KPI = chain(
    node("start", "input", "开始", fields=[{"name": "week"}]),
    node("fetch", "transform", "取数", mode="expression", expression="{'gmv': 4321.5, 'n': 7}", assign_to="kpi"),
    node("card", "metrics", "口径卡", caliber="周报口径", caliber_version="v3",
         metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"},
                  {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.n"}]),
    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def workflow(graph: dict) -> tuple[str, str]:
    async with SessionLocal() as s:
        wf = Workflow(name=f"密钥测试-{uuid.uuid4().hex[:6]}", graph=graph, status="published", published_version=1,
                      version=1)
        s.add(wf)
        await s.flush()
        s.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                              level="published", published_at=utcnow()))
        await s.commit()
        return wf.id, graph_hash(graph)


async def published(made: dict, spec: dict, grants: list[tuple[str, str, str]]) -> str:
    admin = await auth_env.login("root")
    r = await admin.post("/api/apps", json={"name": spec.get("name", "应用"), "spec": spec})
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    made["apps"].append(app_id)
    items = [{"principal_kind": k, "principal": p, "role": role} for k, p, role in grants]
    assert (await admin.put(f"/api/apps/{app_id}/grants", json={"items": items})).status_code == 200
    out = (await admin.post(f"/api/apps/{app_id}/publish", json={})).json()
    assert out["ok"], out
    return app_id


def entry_spec(wf: str, hash_: str, *, files: bool = False) -> dict:
    props = {"amount": {"type": "number", "title": "金额"}}
    if files:
        props["receipt"] = {"type": "object", "title": "发票", "x-widget": "file"}
    return {"format": "agentlab.app/1", "name": "报销",
            "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "entry"}],
            "entries": [{"id": "apply", "label": "发起", "pin": "main", "form": {"type": "object", "properties": props},
                         "map": {"amount": "form.amount", "who": "user.username"}}],
            "pages": [{"id": "home", "title": "首页", "body": [{"id": "new", "type": "entry_button",
                                                                  "props": {"entry": "apply"}}]}]}


def view_spec(wf: str, hash_: str, *, on_demand: bool = False, extra: list | None = None) -> dict:
    source = ({"kind": "on_demand", "pin": "main", "params": {"type": "object", "properties": {"week": {"type": "string"}}}}
              if on_demand else {"kind": "latest_sealed", "pin": "main", "from": "formal"})
    return {"format": "agentlab.app/1", "name": "周报看板",
            "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "view"}],
            "views": {"latest": {"source": source}},
            "pages": [{"id": "home", "title": "首页", "body": [
                {"id": "gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "latest", "metric": "card.gmv"}},
                *(extra or [])]}]}


async def wait(run_id: str, statuses=("succeeded", "failed", "cancelled")) -> Run:
    for _ in range(600):
        async with SessionLocal() as s:
            row = await s.get(Run, run_id)
        if row is not None and row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有到 {statuses}")


async def formal(wf: str, week: str = "W1") -> Run:
    from app.engine.launch import launch_run

    async with SessionLocal() as s:
        run = await launch_run(s, workflow_id=wf, run_class="formal", version=1, input_payload={"week": week},
                               actor="编辑", origin_kind="launcher")
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row


# ---------------------------------------------------------------- 幂等与留痕


async def test_starting_with_a_key_is_idempotent_and_leaves_a_trace(world):
    pia = await auth_env.make_user("pia", "user")
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h), [("user", "pia", "use")])
    row, secret = await auth_env.make_key(pia, name="报销脚本")
    c = auth_env.keyed(secret)
    url = f"/api/apps/{app_id}/entries/apply"
    missing = await c.post(url, json={"values": {"amount": 3}})
    assert missing.status_code == 400 and missing.json()["code"] == "key_idempotency_required"
    first = await c.post(url, json={"values": {"amount": 3}}, headers={"Idempotency-Key": "order-1"})
    assert first.status_code == 201, first.text
    again = await c.post(url, json={"values": {"amount": 3}}, headers={"Idempotency-Key": "order-1"})
    assert again.json()["run_id"] == first.json()["run_id"] and again.json()["duplicate"] is True
    other = await c.post(url, json={"values": {"amount": 4}}, headers={"Idempotency-Key": "order-1"})
    assert other.status_code == 422 and other.json()["code"] == "key_idempotency_reused", "同一个键办另一件事"
    run = await wait(first.json()["run_id"])
    assert run.started_by == "pia"
    assert {k: run.origin.get(k) for k in ("via", "key_id", "key_name", "idempotency_key")} == {
        "via": "key", "key_id": row.id, "key_name": "报销脚本", "idempotency_key": "order-1"}
    async with SessionLocal() as s:
        note = (await s.execute(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.origin")))\
            .scalar_one()
    assert (note.data["via"], note.data["key_id"]) == ("key", row.id) and note.seq <= run.manifest_seq, \
        "来源在封存范围内"
    async with SessionLocal() as s:
        n = len(list((await s.execute(select(Run).where(Run.app_id == app_id))).scalars()))
    assert n == 1, "同一个键只发起一个实例"

    # 轮换之后：同一个键照样认得出（按拥有者去重，不按密钥）
    admin = await auth_env.login("root")
    rotated = (await admin.post(f"/api/access-keys/{row.id}/rotate")).json()["key"]
    again = await auth_env.keyed(rotated).post(url, json={"values": {"amount": 3}}, headers={"Idempotency-Key": "order-1"})
    assert again.json()["run_id"] == first.json()["run_id"]


async def test_a_half_done_start_is_reclaimed_after_a_crash(world):
    from datetime import timedelta

    from app.process import receipts

    pia = await auth_env.make_user("pia", "user")
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h), [("user", "pia", "use")])
    row, secret = await auth_env.make_key(pia)
    # 上一次处理到一半服务停了：回执停在 processing，实例其实已经发起
    async with SessionLocal() as s:
        s.add(ApiReceipt(owner_user_id=pia.id, idem_key="crash-1", status="processing", route="x",
                         received_at=utcnow() - timedelta(seconds=receipts.STALE_S + 5)))
        s.add(ApiReceipt(owner_user_id=pia.id, idem_key="busy-1", status="processing", route="x"))
        s.add(Run(id=f"run-{uuid.uuid4().hex[:8]}", workflow_name="实例", run_class="formal", app_id=app_id,
                  origin_kind="app_entry", started_by="pia", origin={"via": "key", "idempotency_key": "crash-1"}))
        await s.commit()
        before = (await s.execute(select(Run.id).where(Run.app_id == app_id))).scalar_one()
    c = auth_env.keyed(secret)
    url = f"/api/apps/{app_id}/entries/apply"
    busy = await c.post(url, json={"values": {}}, headers={"Idempotency-Key": "busy-1"})
    assert busy.status_code == 409 and busy.json()["code"] == "key_idempotency_busy"
    back = await c.post(url, json={"values": {}}, headers={"Idempotency-Key": "crash-1"})
    assert back.status_code == 201 and back.json()["run_id"] == before, back.text
    async with SessionLocal() as s:
        assert len(list((await s.execute(select(Run.id).where(Run.app_id == app_id))).scalars())) == 1
        receipt = (await s.execute(select(ApiReceipt).where(ApiReceipt.idem_key == "crash-1"))).scalar_one()
    assert (receipt.status, receipt.run_id, receipt.key_id) == ("accepted", before, row.id)


async def test_entries_with_file_controls_cannot_be_started_with_a_key(world):
    pia = await auth_env.make_user("pia", "user")
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h, files=True), [("user", "pia", "use")])
    _, secret = await auth_env.make_key(pia)
    r = await auth_env.keyed(secret).post(f"/api/apps/{app_id}/entries/apply", json={"values": {}},
                                          headers={"Idempotency-Key": "f1"})
    assert r.status_code == 422 and r.json()["code"] == "key_files_unsupported"


# ---------------------------------------------------------------- 待办


def human(mode: str) -> dict:
    nodes = [node("start", "input", fields=[{"name": "q"}]),
             node("ask", "human", mode=mode, title="处理一下", assignee={"kind": "initiator"}),
             node("ok", "output", fields=[{"name": "结论", "value": "通过"}])]
    edges = [{"source": "start", "target": "ask"}, {"source": "ask", "target": "ok"}]
    return {"nodes": nodes, "edges": edges}


async def _task_for(app_id: str, graph: dict, started_by: str) -> Task:
    run = await run_manager.start(graph=graph, input_payload={"q": "x"}, app_id=app_id, app_version=1,
                                  started_by=started_by)
    await wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    for _ in range(100):
        async with SessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.run_id == run.id, Task.status == "pending"))).scalars().first()
        if task is not None:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError("没有开出待办")


async def test_personal_keys_answer_only_three_kinds_of_tasks(world):
    pia = await auth_env.make_user("pia", "user")
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h), [("user", "pia", "use")])
    key, secret = await auth_env.make_key(pia, name="审批机器人")
    c = auth_env.keyed(secret)
    for mode, body in (("approve", {"decision": "approve"}), ("input", {"decision": "submit", "values": {"value": "补上"}}),
                       ("edit", {"decision": "submit", "values": {"value": "改好"}})):
        task = await _task_for(app_id, human(mode), "pia")
        listed = [t["id"] for t in (await c.get("/api/tasks")).json()["items"]]
        assert task.id in listed, "派给拥有者的待办列得出来"
        r = await c.post(f"/api/tasks/{task.id}/respond", json=body)
        assert r.status_code == 200 and r.json()["resolved"] is True, (mode, r.text)
        async with SessionLocal() as s:
            ev = (await s.execute(select(RunEvent).where(RunEvent.run_id == task.run_id,
                                                         RunEvent.type == "task.responded"))).scalar_one()
        assert (ev.data["via"], ev.data["key_id"], ev.data["key_name"], ev.data["actor"]) == \
            ("key", key.id, "审批机器人", "pia")
    async with SessionLocal() as s:
        run = Run(workflow_name="写入", run_class="formal", app_id=app_id, origin_kind="app_entry", started_by="pia")
        s.add(run)
        await s.flush()
        tasks = []
        for kind in ("action_confirm", "dual_check", "reconcile", "tool"):
            t = Task(run_id=run.id, kind=kind, app_id=app_id)
            s.add(t)
            await s.flush()
            s.add(TaskAssignment(task_id=t.id, principal_kind="user", principal="pia", source="initiator"))
            tasks.append(t.id)
        await s.commit()
    for task_id in tasks:
        r = await c.post(f"/api/tasks/{task_id}/respond", json={"decision": "approve"})
        assert r.status_code == 403 and r.json()["code"] == "key_task_kind", r.text


async def test_service_accounts_are_never_assigned_tasks(world):
    svc = await auth_env.make_service()
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h), [("user", "svc-portal", "use")])
    task = await _task_for(app_id, human("approve"), "svc-portal")
    async with SessionLocal() as s:
        rows = list((await s.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id))).scalars())
    assert [(a.principal_kind, a.principal) for a in rows] == [("anyone", "")], \
        "服务账号发起的实例里派给发起人的，交给任何人"
    _, secret = await auth_env.make_key(svc, scopes=("tasks.read", "tasks.respond"))
    r = await auth_env.keyed(secret).post(f"/api/tasks/{task.id}/respond", json={"decision": "approve"})
    assert r.json()["code"] == "key_scope_missing", "服务账号的密钥没有回复待办这一项"
    admin = await auth_env.login("root")
    r = await admin.post(f"/api/tasks/{task.id}/reassign", json={"to": [{"kind": "user", "name": "svc-portal"}]})
    assert r.status_code == 422 and r.json()["code"] == "reassign_service", "改派给服务账号拒收"


# ---------------------------------------------------------------- 封顶在使用者


async def test_an_admins_key_gets_masked_artifacts_and_no_raw_outputs(world):
    root = (await auth_env.login("root"))
    await root.get("/api/auth/me")
    async with SessionLocal() as s:
        from app.db.models import User

        admin_row = (await s.execute(select(User).where(User.username == "root"))).scalar_one()
        name = f"crm-{uuid.uuid4().hex[:6]}"
        s.add(DataSource(name=name, kind="sqlite", options={"mask_columns": ["phone"]}))
        app = App(name="自己的", published_version=1, created_by="root")
        s.add(app)
        await s.flush()
        run = Run(workflow_name="自己发起的实例", run_class="formal", app_id=app.id, origin_kind="app_entry",
                  started_by="root")
        s.add(run)
        await s.commit()
        world["sources"].append(name)
        world["apps"].append(app.id)
    _, secret = await auth_env.make_key(admin_row)
    c = auth_env.keyed(secret)
    snap = {"source": name, "columns": ["name", "phone"], "rows": [["客户甲", "19900000123"]],
            "sql": "SELECT name, phone FROM customers", "truncated": False}
    query_id = await put_json(snap, kind="query_snapshot", run_id=run.id, node_id="q")
    output_id = await put_json({"q": snap}, kind="node_output", run_id=run.id, node_id="q")
    got = await c.get(f"/api/artifacts/{query_id}")
    assert got.status_code == 200 and got.json()["content"]["rows"][0][1] != "19900000123", "遮罩列换掉了"
    assert (await c.get(f"/api/artifacts/{output_id}")).json()["code"] == "artifact_kind_forbidden"
    assert (await root.get(f"/api/artifacts/{output_id}")).status_code == 200, "同一个管理员用会话照旧拿原文"
    assert (await c.get("/api/apps")).json()["items"] == [], "没有授权给他的应用，管理员的密钥也看不到"


async def test_viewdata_and_report_for_an_observer_service_account(world, monkeypatch, tmp_path):
    from tests import test_report_masked_metrics as vipmod
    from app.data.engine import engines

    path = tmp_path / "vip.db"
    import sqlite3

    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", vipmod.CUSTOMERS)
    db.commit()
    db.close()
    source_id = await vipmod.set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    world["sources"].append("vip")
    vipmod.script(monkeypatch, vipmod.CLEAN)
    graph = vipmod.graph(on_violation="flag")
    wf, h = await workflow(graph)
    svc = await auth_env.make_service()
    spec = {"format": "agentlab.app/1", "name": "会员看板",
            "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": h, "use": "view"}],
            "views": {"latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}},
            "pages": [{"id": "home", "title": "首页", "body": [
                {"id": "phone", "type": "metric", "props": {"label": "手机号"},
                 "bind": {"view": "latest", "metric": "card.phone_raw"}}]}]}
    app_id = await published(world, spec, [("user", "svc-portal", "observe")])
    from app.engine.launch import launch_run

    async with SessionLocal() as s:
        launched = await launch_run(s, workflow_id=wf, run_class="formal", version=1, input_payload={},
                                    actor="编辑", origin_kind="launcher")
    run = await wait(launched.id)
    assert run.status == "succeeded", run.error
    _, secret = await auth_env.make_key(svc, scopes=("apps.read", "reports.read"))
    c = auth_env.keyed(secret)

    data = (await c.get(f"/api/apps/{app_id}/views/latest/data")).json()
    assert data["format"] == "agentlab.viewdata/1" and data["state"] == "ok" and data["run_id"] == run.id
    assert data["manifest_hash"] == run.manifest_hash and data["stamp"]["sealed"] is True
    by_id = {m["id"]: m for m in data["metrics"]}
    assert (by_id["phone_raw"]["value"], by_id["phone_raw"]["state"], by_id["phone_raw"]["text"]) == \
        (None, "masked", "已遮罩"), "遮罩的指标不给原值"
    assert (by_id["n_cust"]["value"], by_id["n_cust"]["state"]) == (3, "ok")
    assert by_id["n_cust"]["caliber"] == "会员口径" and by_id["n_cust"]["evidence"]["run_id"] == run.id
    assert vipmod.leaks(data) == []
    page = (await c.get(f"/api/apps/{app_id}/pages/home")).json()
    assert page["tree"][0]["data"]["text"] == by_id["phone_raw"]["text"], "和页面同一个判断"
    evidence = await c.get(f"/api/runs/{run.id}/evidence/metrics/card/n_cust")
    assert evidence.status_code == 200, "证据指针走得通"
    report = await c.get(f"/api/runs/{run.id}/report.html")
    assert report.status_code == 200 and vipmod.leaks(report.text) == [], "报告里没有遮罩列的原值"


async def test_viewdata_on_demand_does_not_launch_and_keeps_its_shape(world):
    pia = await auth_env.make_user("pia", "user")
    wf, h = await workflow(KPI)
    app_id = await published(world, view_spec(wf, h, on_demand=True), [("user", "pia", "use")])
    _, secret = await auth_env.make_key(pia, scopes=("apps.read", "apps.run"))
    c = auth_env.keyed(secret)
    params = {"params": json.dumps({"week": "W9"})}
    empty = (await c.get(f"/api/apps/{app_id}/views/latest/data", params=params)).json()
    assert empty["state"] == "empty" and empty["metrics"] == [] and "刷新" in empty["message"]
    async with SessionLocal() as s:
        assert (await s.execute(select(Run.id).where(Run.app_id == app_id))).first() is None, "读数据不发起运行"
    missing = await c.post(f"/api/apps/{app_id}/views/latest/refresh", json={"params": {"week": "W9"}})
    assert missing.json()["code"] == "key_idempotency_required", "刷新视图同样要带幂等键"
    r = await c.post(f"/api/apps/{app_id}/views/latest/refresh", json={"params": {"week": "W9"}},
                     headers={"Idempotency-Key": "w9"})
    assert r.status_code == 200, r.text
    run = await wait(r.json()["run_id"])
    assert run.origin.get("via") == "key"
    again = await c.post(f"/api/apps/{app_id}/views/latest/refresh", json={"params": {"week": "W9"}},
                         headers={"Idempotency-Key": "w9"})
    assert again.json()["run_id"] == run.id and again.json()["duplicate"] is True
    full = (await c.get(f"/api/apps/{app_id}/views/latest/data", params=params)).json()
    assert full["state"] == "ok" and full["params"] == {"week": "W9"}
    assert {m["id"]: m["value"] for m in full["metrics"]} == {"gmv": 4321.5, "orders": 7}
    assert full["stamp"]["calibers"] == [{"name": "周报口径", "version": "v3"}]
    # 管理应用的人改了页面布局（加一块、换个 id）：数据接口的形状不变
    admin = await auth_env.login("root")
    draft = (await admin.get(f"/api/apps/{app_id}")).json()["draft"]
    draft["pages"][0]["body"] = [{"id": "renamed", "type": "metric", "props": {"label": "x"},
                                  "bind": {"view": "latest", "metric": "card.orders"}}]
    assert (await admin.patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    assert (await admin.post(f"/api/apps/{app_id}/publish", json={})).json()["ok"]
    after = (await c.get(f"/api/apps/{app_id}/views/latest/data", params=params)).json()
    assert {k: v for k, v in after.items() if k != "app"} == {k: v for k, v in full.items() if k != "app"}
    assert (await c.get(f"/api/apps/{app_id}/views/nope/data")).json()["code"] == "app_view_not_found"


async def test_service_account_with_observe_lists_all_instances(world):
    pia = await auth_env.make_user("pia", "user")
    svc = await auth_env.make_service()
    wf, h = await workflow(ECHO)
    app_id = await published(world, entry_spec(wf, h), [("user", "pia", "use"), ("user", "svc-portal", "observe")])
    _, mine = await auth_env.make_key(pia)
    started = await auth_env.keyed(mine).post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": 1}},
                                              headers={"Idempotency-Key": "a"})
    run = await wait(started.json()["run_id"])
    _, secret = await auth_env.make_key(svc, scopes=("apps.read",))
    listed = (await auth_env.keyed(secret).get(f"/api/apps/{app_id}/instances", params={"who": "all"})).json()
    assert [i["run_id"] for i in listed["items"]] == [run.id]
    one = await auth_env.keyed(secret).get(f"/api/apps/{app_id}/instances/{run.id}")
    assert one.status_code == 200


# ---------------------------------------------------------------- 钩子的收件记录


async def test_hook_receipts_are_listed_for_managers_with_signature_failures(world):
    import time

    from app.notify.core import sign
    from app.process import hooks

    pia = await auth_env.make_user("pia", "user")  # noqa: F841 - 只有「查看全部实例」，没有管理权限
    admin = await auth_env.login("root")
    async with SessionLocal() as s:
        app = App(name="收款", published_version=1, created_by="root")
        s.add(app)
        await s.flush()
        s.add(AppVersion(app_id=app.id, version=1, spec={"hooks": [{"name": "paid", "kind": "signal"}]},
                         spec_hash="h" * 64, bindings={}))
        s.add(AppGrant(app_id=app.id, principal_kind="user", principal="pia", role="observe"))
        row, secret, _ = await hooks.rotate_hook(s, app.id, "paid", kind="signal")
        await s.commit()
        world["apps"].append(app.id)
        hook_id = row.id

    def signed(key: str, body: dict, *, bad: bool = False) -> tuple[dict, bytes]:
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        return {"X-AgentLab-Timestamp": ts, "X-AgentLab-Signature": f"sha256={'0' * 64 if bad else sign(secret, ts, raw)}",
                "Idempotency-Key": key, "Content-Type": "application/json"}, raw

    plain = auth_env.client()
    headers, body = signed("k1", {"correlation": "none-waiting"})
    assert (await plain.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 404
    headers, body = signed("k2", {"correlation": "x"}, bad=True)
    assert (await plain.post(f"/api/hooks/{hook_id}", content=body, headers=headers)).status_code == 401
    out = (await admin.get(f"/api/apps/{app.id}/hooks/paid/receipts")).json()
    assert [(i["idem_key"], i["status"]) for i in out["items"]] == [("k1", "rejected")], "验签失败的不落库"
    assert "没有在等" in out["items"][0]["detail"] and out["signature_failures"] == 1
    pia_c = await auth_env.login("pia")
    assert (await pia_c.get(f"/api/apps/{app.id}/hooks/paid/receipts")).status_code == 403, "要管理权限"
