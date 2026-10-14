"""「已发布且未退役」的唯一说法（app/apps/versions.py）和它修掉的三处（应用平台 A1）：

- F1：正式运行以前只认当前发布版，应用固定不住旧的发布版。现在接受已发布且未退役的版本；草稿快照、退役的、指纹对不上
  的照旧拒绝，各带机器码（run_version_unpublished / run_version_retired / run_version_hash_mismatch）
- F3：口径升版的判定以前取 max(version)，上游改一下画布（存出一个草稿快照）下游就被逼着处置。现在只看已发布且未退役的
- F4：删除工作流时版本级联删除。应用固定着、或者发布过的版本有生效记录时拒绝（409，列出原因）
- 退役：当前发布版不能退；退役确认列出受影响的应用；写审计；只给管理员（关登录时放行）
- 发布时每一版记下发布人、发布时间、能力版本（F5）；发起时能力有漂移只提醒、不拦；版本详情带表单、退役、使用情况
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.apps.versions import is_published, latest_published, published_versions
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
from app.db.models_apps import App, AppVersion
from app.db.models_identity import AuditEvent
from app.db.models_process import ActionRecord
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


def simple(text="完成"):
    return chain(node("start", "input", fields=[{"name": "week", "type": "string", "required": True,
                                                 "description": "统计周"},
                                                {"name": "top", "type": "integer", "default": 3}]),
                 node("out", "output", fields=[{"name": "r", "value": text}]))


async def workflow(versions: dict[int, str | None], *, current: int, graphs: dict[int, dict] | None = None,
                   status: str = "published") -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"版本测试-{uuid.uuid4().hex[:6]}", graph=simple(), status=status, published_version=current,
                      version=max(versions))
        session.add(wf)
        await session.flush()
        for v, level in versions.items():
            graph = (graphs or {}).get(v) or simple(f"v{v}")
            session.add(WorkflowVersion(workflow_id=wf.id, version=v, graph=graph, graph_hash=graph_hash(graph),
                                        level=level))
        await session.commit()
        return wf.id


async def wait(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def launch(wf, **kw):
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        return await launch_run(session, workflow_id=wf, run_class="formal", actor="测试",
                                input_payload={"week": "2026-W40"}, **kw)


async def refused(wf, **kw):
    from app.engine.launch import LaunchRefused

    with pytest.raises(LaunchRefused) as caught:
        await launch(wf, **kw)
    return caught.value


# --------------------------------------------------------------------------
# 说法本身
# --------------------------------------------------------------------------


async def test_published_means_level_set_and_not_retired():
    wf = await workflow({1: None, 2: "published", 3: None, 4: "governed", 5: None}, current=3)
    async with SessionLocal() as session:
        rows = {r.version: r for r in (await session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf))).scalars()}
        rows[4].retired_at = utcnow()
        await session.commit()
        # 老数据 level 为空：只认当前发布版（v3）
        assert [v.version for v in await published_versions(session, wf)] == [2, 3]
        assert (await latest_published(session, wf)).version == 3
    assert is_published(rows[2]) and not is_published(rows[1], current=3) and is_published(rows[3], current=3)
    assert not is_published(rows[4]) and not is_published(rows[5], current=3)


# --------------------------------------------------------------------------
# F1：发起
# --------------------------------------------------------------------------


async def test_an_older_published_version_can_still_run():
    wf = await workflow({1: "published", 2: "published"}, current=2)
    run = await launch(wf, version=1)
    assert run.version == 1 and (await wait(run.id)).status == "succeeded"


async def test_launch_refusals_carry_codes():
    wf = await workflow({1: "published", 2: None, 3: "published"}, current=3)
    draft = await refused(wf, version=2)
    assert (draft.status_code, draft.code) == (409, "run_version_unpublished")
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
        row.retired_at = utcnow()
        await session.commit()
    retired = await refused(wf, version=1)
    assert (retired.status_code, retired.code) == (409, "run_version_retired") and "v3" in retired.message
    mismatch = await refused(wf, version=3, expect_hash="0" * 64)
    assert (mismatch.status_code, mismatch.code) == (409, "run_version_hash_mismatch")
    run = await launch(wf, version=3, expect_hash=graph_hash(simple("v3")))
    assert run.version_hash == graph_hash(simple("v3"))


async def test_launch_records_the_origin_note_in_the_sealed_range():
    wf = await workflow({1: "published"}, current=1)
    run = await launch(wf, origin_kind="app_entry", app_id="app_x", app_version=2,
                       origin={"entry": "apply", "title": "张三 的报销", "actor_verified": False, "auth_mode": "off"})
    row = await wait(run.id)
    assert (row.origin_kind, row.app_id, row.app_version) == ("app_entry", "app_x", 2)
    async with SessionLocal() as session:
        [note] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "run.origin"))).scalars()
    assert note.data["title"] == "张三 的报销" and note.data["app_version"] == 2 and note.seq <= row.manifest_seq


async def test_capability_snapshot_and_drift(monkeypatch, client):
    """能力快照写进 options["capabilities"]；发布之后能力的治理版本变了，只记一条提醒、照常发起。"""
    from app.capabilities import registry

    graph = chain(node("start", "input"), node("calc", "tool", tool="calculator", args={"expression": "1+1"}),
                  node("out", "output", fields=[{"name": "r", "value": "完成"}]))
    async with SessionLocal() as session:
        wf = Workflow(name=f"能力-{uuid.uuid4().hex[:6]}", graph=graph)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash=graph_hash(graph)))
        await session.commit()
        wf_id = wf.id
    versions = {"calculator": 1}

    async def versions_for(session, names):
        return {n: versions[n] for n in names if n in versions}

    async def snapshot(session, names):
        return {n: {"effect": "read", "approval": "none", "version": versions[n]} for n in names if n in versions}

    monkeypatch.setattr(registry, "capability_versions_for", versions_for)
    monkeypatch.setattr(registry, "capability_snapshot", snapshot)
    res = await client.post(f"/api/workflows/{wf_id}/publish", json={"level": "published"},
                            headers={"X-Actor": "%E5%B0%8F%E7%8E%8B"})
    assert res.json()["ok"], res.text
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf_id))).scalar_one()
    assert row.capability_versions == {"calculator": 1} and row.published_by == "小王" and row.published_at
    versions["calculator"] = 2
    run = await launch(wf_id)
    assert run.options["capabilities"]["calculator"]["version"] == 2
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        logs = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "log"))).scalars()
        drift = [e for e in logs if e.data.get("code") == "capability_drift"]
    assert len(drift) == 1 and drift[0].data["capabilities"] == [{"name": "calculator", "published": 1, "current": 2}]
    assert drift[0].data["level"] == "warning" and "「calculator」" in drift[0].data["message"]


# --------------------------------------------------------------------------
# F3：口径升版只看发布过的版本
# --------------------------------------------------------------------------


def library(version):
    return chain(node("start", "input"),
                 node("caliber", "metrics", "周报口径卡", caliber="周报口径", caliber_version=version,
                      metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.caliber.text }}"}]))


def downstream(lib_id):
    return chain(node("start", "input"),
                 node("fetch", "transform", mode="expression", expression="{'gmv': 4321.5}", assign_to="kpi"),
                 node("card", "metrics", "本地口径卡",
                      caliber_from={"workflow_id": lib_id, "workflow_version": 1, "node_id": "caliber"}),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def test_a_draft_upstream_does_not_force_a_disposition():
    lib = await workflow({1: "published", 2: None}, current=1, graphs={1: library("v1"), 2: library("v2")})
    wf = await workflow({1: "published"}, current=1, graphs={1: downstream(lib)})
    run = await launch(wf)
    assert (await wait(run.id)).status == "succeeded"
    # 上游真的发布了 v2：照旧逼着处置
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == lib, WorkflowVersion.version == 2))).scalar_one()
        row.level = "published"
        (await session.get(Workflow, lib)).published_version = 2
        await session.commit()
    refusal = await refused(wf)
    assert refusal.status_code == 409 and "v2" in refusal.message


# --------------------------------------------------------------------------
# 退役、版本详情、删除约束
# --------------------------------------------------------------------------


async def pinned_app(wf: str, version: int, *, name="周报看板") -> str:
    async with SessionLocal() as session:
        row = App(name=name, draft={}, published_version=1)
        session.add(row)
        await session.flush()
        graph = simple(f"v{version}")
        pins = [{"alias": "main", "workflow_id": wf, "version": version, "graph_hash": graph_hash(graph), "use": "view"}]
        session.add(AppVersion(app_id=row.id, version=1, spec={"pins": pins}, spec_hash="x", pins=pins))
        await session.commit()
        return row.id


async def test_retiring_versions(client):
    wf = await workflow({1: "published", 2: "published", 3: None}, current=2)
    app_id = await pinned_app(wf, 1)
    current = await client.post(f"/api/workflows/{wf}/versions/2/retire", json={})
    assert current.status_code == 409 and current.json()["code"] == "version_retire_current"
    draft = await client.post(f"/api/workflows/{wf}/versions/3/retire", json={})
    assert draft.status_code == 409 and draft.json()["code"] == "version_retire_unpublished"
    res = await client.post(f"/api/workflows/{wf}/versions/1/retire", json={"note": "口径作废"},
                            headers={"X-Actor": "admin"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["affected_apps"] == [{"app_id": app_id, "name": "周报看板", "app_version": 1, "alias": "main",
                                      "version": 1}]
    assert body["retired"]["retire_note"] == "口径作废" and body["retired"]["retired_by"] == "admin"
    again = await client.post(f"/api/workflows/{wf}/versions/1/retire", json={})
    assert again.status_code == 409 and again.json()["code"] == "version_retire_already"
    async with SessionLocal() as session:
        audit = (await session.execute(select(AuditEvent).where(
            AuditEvent.subject_id == f"{wf}:1", AuditEvent.action == "workflow_version.retire"))).scalar_one()
    assert audit.actor == "admin" and audit.verified is False and audit.data["affected_apps"][0]["app_id"] == app_id
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["retired_at"] and detail["retire_note"] == "口径作废"
    versions = (await client.get(f"/api/workflows/{wf}/versions")).json()
    assert next(v for v in versions if v["version"] == 1)["retired_at"]


async def test_retiring_needs_an_admin_when_login_is_on(client, monkeypatch):
    from app.api import deps

    wf = await workflow({1: "published", 2: "published"}, current=2)
    monkeypatch.setattr(deps, "current_principal",
                        lambda: deps.Principal(username="bob", role="builder", verified=True, mode="local"))
    res = await client.post(f"/api/workflows/{wf}/versions/1/retire", json={})
    assert res.status_code == 403 and res.json()["code"] == "admin_only"
    governed = await client.post(f"/api/workflows/{wf}/publish", json={"level": "governed"})
    assert governed.status_code == 403


async def test_version_detail_has_form_and_usage(client):
    wf = await workflow({1: "published"}, current=1)
    await pinned_app(wf, 1)
    async with SessionLocal() as session:
        run = Run(workflow_id=wf, workflow_name="x", status="succeeded", run_class="formal", version=1)
        session.add(run)
        await session.flush()
        session.add(ActionRecord(action_key="k", idem_key=f"k:{uuid.uuid4().hex}", run_id=run.id, node_id="pay",
                                 workflow_id=wf, workflow_version=1, capability="erp_pay", payload_hash="h",
                                 state="confirmed"))
        await session.commit()
    detail = (await client.get(f"/api/workflows/{wf}/versions/1")).json()
    assert detail["input_schema"]["properties"]["week"] == {"type": "string", "title": "week", "description": "统计周"}
    assert detail["input_schema"]["required"] == ["week"]
    assert detail["usage"]["effects"] == 1 and detail["usage"]["formal_runs"] == 1
    assert [a["name"] for a in detail["usage"]["apps"]] == ["周报看板"]
    ledger = (await client.get(f"/api/ledger/workflow-versions/{wf}/1")).json()
    assert ledger == detail["usage"]
    effects = (await client.get("/api/ledger/effects", params={"workflow_id": wf})).json()["items"]
    assert [(e["capability"], e["state"], e["workflow_version"]) for e in effects] == [("erp_pay", "confirmed", 1)]


async def test_a_workflow_in_use_cannot_be_deleted(client):
    pinned = await workflow({1: "published"}, current=1)
    app_id = await pinned_app(pinned, 1, name="被固定的看板")
    res = await client.delete(f"/api/workflows/{pinned}")
    assert res.status_code == 409 and res.json()["code"] == "workflow_in_use"
    assert "应用「被固定的看板」v1 固定了 v1" in res.json()["detail"]
    # 应用归档之后不再算「正在用」
    async with SessionLocal() as session:
        (await session.get(App, app_id)).status = "archived"
        await session.commit()
    assert (await client.delete(f"/api/workflows/{pinned}")).status_code == 204

    effected = await workflow({1: "published"}, current=1)
    async with SessionLocal() as session:
        run = Run(workflow_id=effected, workflow_name="x", status="succeeded", run_class="formal", version=1)
        session.add(run)
        await session.flush()
        session.add(ActionRecord(action_key="k", idem_key=f"k:{uuid.uuid4().hex}", run_id=run.id, node_id="pay",
                                 workflow_id=effected, workflow_version=1, capability="erp_pay", payload_hash="h",
                                 state="sent"))
        await session.commit()
    res = await client.delete(f"/api/workflows/{effected}")
    assert res.status_code == 409 and "v1 有 1 条生效记录" in res.json()["detail"]
    plain = await workflow({1: "published"}, current=1)
    assert (await client.delete(f"/api/workflows/{plain}")).status_code == 204
