"""应用的使用面（服务端）：入口发起实例、实例、按需视图、应用级权限（应用平台 §2.3、§3.3、§3.6、§3.10、§3.12）。

- 入口：按入口表单校验，失败回 422 带 {errors}；映射成钉住版本的运行输入（表单值、当前用户、常量），发起正式运行
  （来源 app_entry），实例标题在服务端按 instance_title 拼好，记进 run.origin；应用需要升级时拒绝发起
- 实例：我发起的 / 全部；实例页的视图模型（概况、进度、绑定的运行输入）
- 按需视图：默认点刷新才运行；同一组参数只运行一份（缓存、并发时接上同一次）；参数按表单校验；每个应用每天有上限；
  声明了 auto 的打开页面时自动发起
- 权限：关登录全部放行（my_role=manage）；开登录时 user 角色按授权，没有授权看不到应用
"""
from __future__ import annotations

import asyncio
import uuid
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api import apps as apps_api
from app.api.deps import Principal
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Setting, Workflow, WorkflowVersion
from app.db.models_apps import AppGrant
from app.engine.runner import run_manager
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


EXPENSE = chain(
    node("start", "input", "开始", fields=[{"name": "amount", "type": "number", "required": True},
                                          {"name": "applicant"}, {"name": "kind"}]),
    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ input.applicant }}"}]))

KPI = chain(
    node("start", "input", "开始", fields=[{"name": "week", "required": True}]),
    node("fetch", "transform", "取数", mode="expression", expression="{'gmv': 4321.5}", assign_to="kpi"),
    node("card", "metrics", "口径卡", caliber="周报口径", caliber_version="v1",
         metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def workflow(graph: dict, versions=(1,), current=1) -> tuple[str, str]:
    async with SessionLocal() as session:
        wf = Workflow(name=f"应用测试-{uuid.uuid4().hex[:6]}", graph=graph, status="published", published_version=current,
                      version=max(versions))
        session.add(wf)
        await session.flush()
        for v in versions:
            session.add(WorkflowVersion(workflow_id=wf.id, version=v, graph=graph, graph_hash=graph_hash(graph),
                                        level="published", published_at=utcnow()))
        await session.commit()
        return wf.id, graph_hash(graph)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def published_app(client, spec: dict, name: str = "报销") -> str:
    res = await client.post("/api/apps", json={"name": name, "spec": spec})
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    out = (await client.post(f"/api/apps/{app_id}/publish", json={})).json()
    assert out["ok"], out
    return app_id


def actor(name: str) -> dict:
    return {"X-Actor": quote(name)}


def walk(tree):
    for item in tree:
        yield item
        yield from walk(item.get("children") or [])


def find(vm, cid):
    return next((n for n in walk(vm["tree"]) if n["id"] == cid), None)


def expense_spec(wf: str, hash_: str) -> dict:
    return {
        "format": "agentlab.app/1", "name": "报销",
        "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "entry"}],
        "entries": [{"id": "apply", "label": "发起报销", "pin": "main", "form": {"$ref": "#/forms/apply"},
                     "map": {"amount": "form.amount", "applicant": "user.display_name", "kind": "'差旅'"},
                     "instance_title": {"pattern": "{applicant} 的报销", "fields": ["applicant"]}}],
        "forms": {"apply": {"type": "object", "required": ["amount"],
                            "properties": {"amount": {"type": "number", "title": "金额（元）", "minimum": 0}}}},
        "views": {"inst": {"source": {"kind": "instance"}}},
        "pages": [
            {"id": "home", "title": "首页", "body": [
                {"id": "new", "type": "entry_button", "props": {"entry": "apply"}},
                {"id": "mine", "type": "instance_list", "props": {"entry": "apply", "who": "mine"}}]},
            {"id": "instance", "title": "实例", "body": [
                {"id": "hdr", "type": "instance_header"}, {"id": "tl", "type": "instance_timeline"},
                {"id": "who", "type": "field", "props": {"label": "申请人"}, "bind": {"view": "inst", "input": "applicant"}}]},
        ],
    }


# --------------------------------------------------------------------------
# 入口与实例
# --------------------------------------------------------------------------


async def test_entries_validate_map_and_title(client):
    wf, hash_ = await workflow(EXPENSE)
    app_id = await published_app(client, expense_spec(wf, hash_))
    bad = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": "很多"}})
    assert bad.status_code == 422
    assert bad.json()["errors"] == [{"path": "amount", "code": "type", "message": "「金额（元）」需要填数"}]
    empty = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {}})
    assert empty.status_code == 422 and empty.json()["errors"][0]["code"] == "required"
    negative = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": -3}})
    assert negative.json()["errors"][0]["code"] == "minimum"
    gone = await client.post(f"/api/apps/{app_id}/entries/nope", json={"values": {}})
    assert gone.status_code == 404 and gone.json()["code"] == "app_entry_not_found"

    res = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": 120}}, headers=actor("张三"))
    assert res.status_code == 201, res.text
    assert res.json()["title"] == "张三 的报销"
    run = await wait(res.json()["run_id"])
    assert run.status == "succeeded", run.error
    assert (run.run_class, run.version, run.origin_kind, run.app_id, run.app_version, run.started_by) == \
        ("formal", 1, "app_entry", app_id, 1, "张三")
    assert run.input == {"amount": 120, "applicant": "张三", "kind": "差旅"}
    assert run.origin["title"] == "张三 的报销" and run.origin["entry"] == "apply" and run.origin["actor_verified"] is False
    async with SessionLocal() as session:
        [note] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "run.origin"))).scalars()
    assert note.data["kind"] == "app_entry" and note.data["title"] == "张三 的报销" and note.seq <= run.manifest_seq

    mine = (await client.get(f"/api/apps/{app_id}/instances", headers=actor("张三"))).json()["items"]
    assert [(i["run_id"], i["title"], i["entry_id"], i["status"]) for i in mine] == \
        [(run.id, "张三 的报销", "apply", "succeeded")]
    assert (await client.get(f"/api/apps/{app_id}/instances", headers=actor("李四"))).json()["items"] == []
    assert len((await client.get(f"/api/apps/{app_id}/instances", params={"who": "all"},
                                 headers=actor("李四"))).json()["items"]) == 1

    inst = (await client.get(f"/api/apps/{app_id}/instances/{run.id}")).json()
    assert inst["title"] == "张三 的报销" and inst["view"]["page"]["id"] == "instance"
    header = find(inst["view"], "hdr")["data"]
    assert header["status_text"] == "已完成" and header["started_by"] == "张三"
    assert [s["state"] for s in find(inst["view"], "tl")["data"]["steps"]] == ["done", "done"]
    who = find(inst["view"], "who")["data"]
    assert who["text"] == "张三" and who["evidence"]["kind"] == "input" and who["evidence"]["eid"].startswith("ev:input:")

    home = (await client.get(f"/api/apps/{app_id}/pages/home", headers=actor("张三"))).json()
    assert find(home, "new")["data"] == {"entry": "apply", "label": "发起报销", "disabled": False}
    assert [i["run_id"] for i in find(home, "mine")["data"]["items"]] == [run.id]
    assert home["forms"]["apply"]["properties"]["amount"]["title"] == "金额（元）"


async def test_an_app_on_a_retired_version_refuses_new_instances(client):
    wf, hash_ = await workflow(EXPENSE, versions=(1, 2), current=2)
    app_id = await published_app(client, expense_spec(wf, hash_))
    assert (await client.post(f"/api/workflows/{wf}/versions/1/retire", json={})).status_code == 200
    res = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": 1}})
    assert res.status_code == 409 and res.json()["code"] == "app_needs_upgrade"
    home = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert home["app"]["needs_upgrade"] is True
    button = find(home, "new")["data"]
    assert button["disabled"] is True and "已退役" in button["reason"]


# --------------------------------------------------------------------------
# 按需视图
# --------------------------------------------------------------------------


def kpi_spec(wf: str, hash_: str, *, auto=False) -> dict:
    return {
        "format": "agentlab.app/1", "name": "按需看板",
        "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "view"}],
        "forms": {"p": {"type": "object", "required": ["week"], "properties": {"week": {"type": "string", "title": "统计周"}}}},
        "views": {"od": {"source": {"kind": "on_demand", "pin": "main", "params": {"$ref": "#/forms/p"}, "auto": auto}}},
        "pages": [{"id": "home", "title": "首页", "body": [
            {"id": "stamp", "type": "stamp", "bind": {"view": "od"}},
            {"id": "gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "od", "metric": "card.gmv"}}]}],
    }


async def test_on_demand_views(client):
    wf, hash_ = await workflow(KPI)
    app_id = await published_app(client, kpi_spec(wf, hash_), name="按需看板")
    params = {"params": '{"week": "W40"}'}
    vm = (await client.get(f"/api/apps/{app_id}/pages/home", params=params)).json()
    assert vm["sources"]["od"]["state"] == "empty" and vm["problems"][0]["code"] == "view.source_empty"
    assert find(vm, "gmv")["data"]["state"] == "empty"

    bad = await client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {}})
    assert bad.status_code == 422 and bad.json()["errors"][0]["path"] == "week"
    # 同一组参数并发刷新：只运行一份
    first, second = await asyncio.gather(
        client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {"week": "W40", "junk": "x"}}),
        client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {"week": "W40"}}))
    assert first.json()["run_id"] == second.json()["run_id"]
    run = await wait(first.json()["run_id"])
    assert (run.origin_kind, run.app_id, run.input, run.origin["view"]) == ("app_view", app_id, {"week": "W40"}, "od")
    again = (await client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {"week": "W40"}})).json()
    assert again == {"run_id": run.id, "cached": True}

    vm = (await client.get(f"/api/apps/{app_id}/pages/home", params=params)).json()
    assert vm["sources"]["od"]["state"] == "ok" and vm["sources"]["od"]["run_id"] == run.id
    assert vm["sources"]["od"]["stamp"]["from"] == "按需运行" and vm["problems"] == []
    assert find(vm, "gmv")["data"]["text"] == "4,321.5元"
    # 按需运行不算实例
    assert (await client.get(f"/api/apps/{app_id}/instances", params={"who": "all"})).json()["items"] == []

    async with SessionLocal() as session:
        session.add(Setting(key="apps", value={"on_demand_daily_limit": 1}))
        await session.commit()
    try:
        limited = await client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {"week": "W41"}})
        assert limited.status_code == 429 and limited.json()["code"] == "app_view_daily_limit"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.key == "apps"))
            await session.commit()


async def test_auto_views_launch_when_the_page_opens(client):
    wf, hash_ = await workflow(KPI)
    app_id = await published_app(client, kpi_spec(wf, hash_, auto=True), name="自动看板")
    vm = (await client.get(f"/api/apps/{app_id}/pages/home", params={"params": '{"week": "W42"}'})).json()
    assert vm["sources"]["od"]["state"] in ("running", "ok") and vm["sources"]["od"]["run_id"]
    run = await wait(vm["sources"]["od"]["run_id"])
    assert run.origin_kind == "app_view" and run.input == {"week": "W42"}
    # 参数不全：不发起，照实写进问题
    vm = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert vm["sources"]["od"]["state"] == "empty"
    assert "view.auto_refused" in {p["code"] for p in vm["problems"]}


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------


async def test_login_off_allows_everything(client):
    wf, hash_ = await workflow(EXPENSE)
    app_id = await published_app(client, expense_spec(wf, hash_))
    detail = (await client.get(f"/api/apps/{app_id}")).json()
    assert detail["my_role"] == "manage" and detail["draft_bindings"] == {"slots": {}}
    grants = await client.put(f"/api/apps/{app_id}/grants", json={"items": [
        {"principal_kind": "group", "principal": "财务", "role": "observe"},
        {"principal_kind": "everyone", "principal": "随便", "role": "use"}]})
    assert grants.json()["items"] == [{"principal_kind": "group", "principal": "财务", "role": "observe"},
                                      {"principal_kind": "everyone", "principal": "", "role": "use"}]
    assert (await client.get(f"/api/apps/{app_id}/grants")).json() == grants.json()
    # 钩子按名字（D-g）：草稿里声明了才能生成密钥
    assert (await client.post(f"/api/apps/{app_id}/hooks/erp/rotate")).status_code == 404
    draft = (await client.get(f"/api/apps/{app_id}")).json()["draft"]
    draft["hooks"] = [{"name": "erp", "kind": "start", "entry": "apply"}]
    assert (await client.patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    hook = (await client.post(f"/api/apps/{app_id}/hooks/erp/rotate")).json()
    assert hook["secret"] and hook["kind"] == "start" and hook["entry_id"] == "apply" and hook["has_secret"]
    assert (hook["in_draft"], hook["in_published"]) == (True, False)
    listed = (await client.get(f"/api/apps/{app_id}/hooks")).json()["items"]
    assert [h["name"] for h in listed] == ["erp"] and "secret" not in listed[0]
    rotated = (await client.post(f"/api/apps/{app_id}/hooks/erp/rotate")).json()
    assert rotated["secret"] != hook["secret"] and rotated["id"] == hook["id"]
    assert (await client.patch(f"/api/apps/{app_id}/hooks/erp", json={"enabled": False})).json()["enabled"] is False
    assert (await client.delete(f"/api/apps/{app_id}/hooks/erp")).status_code == 204
    listed = (await client.get(f"/api/apps/{app_id}/hooks")).json()["items"]
    assert [(h["name"], h["id"], h["has_secret"]) for h in listed] == [("erp", None, False)], "声明还在草稿里"


async def test_users_see_only_granted_apps(client, monkeypatch):
    wf, hash_ = await workflow(EXPENSE)
    app_id = await published_app(client, expense_spec(wf, hash_))
    carol = Principal(username="carol", role="user", verified=True, mode="local")
    monkeypatch.setattr(apps_api, "current_principal", lambda: carol)
    assert app_id not in {a["id"] for a in (await client.get("/api/apps")).json()["items"]}
    assert (await client.get(f"/api/apps/{app_id}")).status_code == 404
    assert (await client.post("/api/apps", json={"name": "我的"})).status_code == 403
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=app_id, principal_kind="user", principal="carol", role="use"))
        await session.commit()
    listed = {a["id"]: a for a in (await client.get("/api/apps")).json()["items"]}
    assert listed[app_id]["my_role"] == "use"
    res = await client.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"amount": 5}})
    assert res.status_code == 201 and res.json()["title"] == "carol 的报销"
    run = await wait(res.json()["run_id"])
    assert run.started_by == "carol" and run.origin["actor_verified"] is True and run.origin["auth_mode"] == "local"
    assert (await client.get(f"/api/apps/{app_id}/instances", params={"who": "all"})).status_code == 403
    assert len((await client.get(f"/api/apps/{app_id}/instances")).json()["items"]) == 1
    assert (await client.post(f"/api/apps/{app_id}/publish", json={})).status_code == 403
    assert (await client.get(f"/api/apps/{app_id}/pages/home", params={"draft": 1})).status_code == 403


async def test_pages_are_cached_by_version_page_runs_and_role(client, monkeypatch):
    """同一应用版本、同一页、各来源取的是同一次运行、同一个角色：第二次打开不再读封存范围（清单哈希要把整次运行的事件
    重算一遍）。工作流发布了新版本（印章上的错位提示会变）就重新算。"""
    from app.api import evidence

    wf, hash_ = await workflow(KPI)
    app_id = await published_app(client, kpi_spec(wf, hash_), name="缓存看板")
    run_id = (await client.post(f"/api/apps/{app_id}/views/od/refresh", json={"params": {"week": "W50"}})).json()["run_id"]
    await wait(run_id)
    calls: list[str] = []
    real = evidence._sealed

    async def counting(rid):
        calls.append(rid)
        return await real(rid)

    monkeypatch.setattr(evidence, "_sealed", counting)
    params = {"params": '{"week": "W50"}'}
    first = (await client.get(f"/api/apps/{app_id}/pages/home", params=params)).json()
    second = (await client.get(f"/api/apps/{app_id}/pages/home", params=params)).json()
    assert first == second and first["sources"]["od"]["state"] == "ok" and calls == [run_id]
    async with SessionLocal() as session:
        session.add(WorkflowVersion(workflow_id=wf, version=2, graph=KPI, graph_hash=hash_, level="published"))
        (await session.get(Workflow, wf)).published_version = 2
        await session.commit()
    third = (await client.get(f"/api/apps/{app_id}/pages/home", params=params)).json()
    assert calls == [run_id, run_id]
    assert third["sources"]["od"]["stamp"]["notes"] == ["工作流的当前发布版本是 v2，本应用固定在 v1"]
    # 管理员新加了遮罩列：来源运行没换，缓存的视图也不能再交出去（P4）
    from app.db.models import DataSource

    name = f"masked-{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", options={"mask_columns": ["phone"]}))
        await session.commit()
    try:
        await client.get(f"/api/apps/{app_id}/pages/home", params=params)
        assert calls == [run_id, run_id, run_id]
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(DataSource).where(DataSource.name == name))
            await session.commit()
