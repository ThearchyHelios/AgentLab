"""分享链接（api/shares.py + apps/resolve.py 的 share 旁路，docs/design/next-round/04-对外集成与触达.md C2）。

守的是：
- 管理员开关缺省关，关着建不了、已有的打不开；只有应用的 manage 能建；个人应用、没发布的、这一页没有定时出数的视图都建不了；
- 到期必填，最长 90 天；
- 读的接口不用登录，只认请求头 X-AgentLab-Share：地址里带 key 不认；库里只有 key 的哈希和开头一截；
- 只解析定时出数（最近封存）的视图：按需视图写明原因、从不发起运行（声明了 auto 的也不）；流程组件、报告换成写明原因的说明；
- fixed 模式钉在建分享时的那几次运行上（趋势也停在那一期），latest 跟着最新的封存走；
- 下载报告只认这条分享解析出来的运行，内部就是报告导出（遮罩值写「已遮罩」）；
- 撤销、到期回 410 并写明；建、撤销写审计，打开只计数；
- 访问密钥一律调不了分享的接口；
- /s 上的安全头：嵌入白名单用 AGENTLAB_EMBED_ORIGINS、Referrer-Policy: no-referrer。

应用、工作流、人名都是虚构的。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.config import BACKEND_DIR
from app.db.base import SessionLocal, utcnow
from app.db.models import AuditEvent, Run, Setting
from app.db.models_apps import ViewShare
from tests.fakes import auth_env
from tests.test_access_key_use import KPI, formal, published, wait, workflow
from tests.test_access_key_use import world  # noqa: F401 - 开登录、建管理员、收尾清掉应用

HEADER = "X-AgentLab-Share"


@pytest.fixture(autouse=True)
async def switch(world):  # noqa: F811
    await set_switch(True)
    yield
    async with SessionLocal() as s:
        await s.execute(delete(ViewShare))
        await s.execute(delete(Setting).where(Setting.key == "apps"))
        await s.commit()


async def set_switch(on: bool) -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, "apps")
        if row is None:
            s.add(Setting(key="apps", value={"share_links": on}))
        else:
            row.value = {**row.value, "share_links": on}
        await s.commit()


def anonymous(**headers: str) -> AsyncClient:
    """不带会话 cookie、不带 CSRF 头的客户端：拿到链接的人就是这样打开的。"""
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


def board(wf: str, h: str, *, body: list | None = None, views: dict | None = None) -> dict:
    return {"format": "agentlab.app/1", "name": "周报看板",
            "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": h, "use": "view"}],
            "views": views or {"latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}},
            "pages": [{"id": "home", "title": "首页", "body": body or [
                {"id": "gmv", "type": "metric", "props": {"label": "销售额"},
                 "bind": {"view": "latest", "metric": "card.gmv"}},
                {"id": "trend", "type": "series", "bind": {"view": "latest", "series": "card.gmv"}}]}]}


def soon(days: float = 30) -> str:
    return (utcnow() + timedelta(days=days)).isoformat()


async def share(app_id: str, **body) -> dict:
    admin = await auth_env.login("root")
    r = await admin.post(f"/api/apps/{app_id}/shares", json={"page_id": "home", "expires_at": soon(), **body})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------
# 建：开关、权限、到期
# --------------------------------------------------------------------------


async def test_switch_and_manage_are_both_required(world):  # noqa: F811
    wf, h = await workflow(KPI)
    await auth_env.make_user("pia", "user")
    app_id = await published(world, board(wf, h), [("user", "pia", "observe")])
    admin = await auth_env.login("root")
    await set_switch(False)
    r = await admin.post(f"/api/apps/{app_id}/shares", json={"page_id": "home", "expires_at": soon()})
    assert r.status_code == 403 and r.json()["code"] == "share_links_off" and "允许建分享链接" in r.json()["detail"]
    listed = (await admin.get(f"/api/apps/{app_id}/shares")).json()
    assert listed["enabled"] is False and listed["items"] == [] and listed["max_days"] == 90
    await set_switch(True)
    pia = await auth_env.login("pia")
    r = await pia.post(f"/api/apps/{app_id}/shares", json={"page_id": "home", "expires_at": soon()})
    assert r.status_code == 403 and r.json()["code"] == "app_forbidden", "查看全部实例也不够，要管理权限"
    made = await share(app_id)
    assert made["path"] == f"/s#{made['key']}" and made["state"] == "active" and made["mode"] == "latest"
    async with SessionLocal() as s:
        row = await s.get(ViewShare, made["id"])
    assert row.key_hash != made["key"] and made["key"] not in json.dumps(
        {c.name: str(getattr(row, c.name)) for c in ViewShare.__table__.columns}), "库里没有 key 的明文"
    assert row.prefix == made["key"][:6] and made["prefix"] == row.prefix
    listed = (await admin.get(f"/api/apps/{app_id}/shares")).json()
    assert [i["id"] for i in listed["items"]] == [made["id"]] and "key" not in listed["items"][0]
    assert listed["items"][0]["page_title"] == "首页"


@pytest.mark.parametrize("expires, fragment", [(None, "必须设到期时间"), (-1, "现在之后"), (91, "最长 90 天")])
async def test_expiry_is_required_and_at_most_90_days(world, expires, fragment):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    admin = await auth_env.login("root")
    body = {"page_id": "home", **({"expires_at": soon(expires)} if expires is not None else {})}
    r = await admin.post(f"/api/apps/{app_id}/shares", json=body)
    assert r.status_code == 422 and r.json()["code"] == "share_expires_invalid" and fragment in r.json()["detail"]
    ok = await admin.post(f"/api/apps/{app_id}/shares", json={"page_id": "home", "expires_at": soon(90)})
    assert ok.status_code == 201


async def test_pages_without_sealed_views_and_personal_apps_cannot_be_shared(world):  # noqa: F811
    wf, h = await workflow(KPI)
    on_demand = board(wf, h, views={"latest": {"source": {"kind": "on_demand", "pin": "main"}}},
                      body=[{"id": "gmv", "type": "metric", "bind": {"view": "latest", "metric": "card.gmv"}}])
    app_id = await published(world, on_demand, [])
    admin = await auth_env.login("root")
    r = await admin.post(f"/api/apps/{app_id}/shares", json={"page_id": "home", "expires_at": soon()})
    assert r.status_code == 409 and r.json()["code"] == "share_no_sealed_view"
    r = await admin.post(f"/api/apps/{app_id}/shares", json={"page_id": "nope", "expires_at": soon()})
    assert r.status_code == 404
    draft_only = (await admin.post("/api/apps", json={"name": "草稿", "spec": board(wf, h)})).json()["id"]
    world["apps"].append(draft_only)
    r = await admin.post(f"/api/apps/{draft_only}/shares", json={"page_id": "home", "expires_at": soon()})
    assert r.status_code == 409 and r.json()["code"] == "app_unpublished"
    from app.db.models import App

    async with SessionLocal() as s:
        (await s.get(App, draft_only)).scope = "personal"
        await s.commit()
    r = await admin.post(f"/api/apps/{draft_only}/shares", json={"page_id": "home", "expires_at": soon()})
    assert r.json()["code"] in ("personal.not_shareable", "app_not_found"), r.text


# --------------------------------------------------------------------------
# 打开
# --------------------------------------------------------------------------


async def test_the_page_is_public_but_only_trusts_the_header(world):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    run = await formal(wf)
    made = await share(app_id)
    async with anonymous() as c:
        missing = await c.get("/api/shares/page", params={"key": made["key"]})
        assert missing.status_code == 400 and missing.json()["code"] == "share_key_missing", "地址里带的 key 不认"
        wrong = await c.get("/api/shares/page", headers={HEADER: made["key"][:-2] + "zz"})
        assert wrong.status_code == 404 and wrong.json()["code"] == "share_not_found"
        r = await c.get("/api/shares/page", headers={HEADER: made["key"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "agentlab.share/1"
    assert body["share"]["created_by"] == "root" and body["share"]["mode"] == "latest"
    view = body["view"]
    assert view["format"] == "agentlab.view/1" and view["sources"]["latest"]["run_id"] == run.id
    gmv = view["tree"][0]["data"]
    assert gmv["state"] == "ok" and "4,321.5" in gmv["text"], "看到的就是使用者看到的那份"
    assert view["sources"]["latest"]["stamp"]["sealed"] is True, "印章照常算"
    assert body["downloads"] == [{"view": "latest", "run_id": run.id, "reports": [],
                                  "as_of_text": view["sources"]["latest"]["stamp"]["as_of_text"]}]


async def test_only_sealed_views_and_flow_components_become_placeholders(world):  # noqa: F811
    wf, h = await workflow(KPI)
    spec = board(wf, h, views={
        "latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}},
        "ask": {"source": {"kind": "on_demand", "pin": "main", "auto": True}}}, body=[
        {"id": "gmv", "type": "metric", "bind": {"view": "latest", "metric": "card.gmv"}},
        {"id": "asked", "type": "metric", "bind": {"view": "ask", "metric": "card.gmv"}},
        {"id": "todo", "type": "task_list"},
        {"id": "mine", "type": "instance_list"}])
    app_id = await published(world, spec, [])
    await formal(wf)
    made = await share(app_id)
    async with SessionLocal() as s:
        before = await s.scalar(select(func.count(Run.id)))
    async with anonymous(**{HEADER: made["key"]}) as c:
        view = (await c.get("/api/shares/page")).json()["view"]
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count(Run.id))) == before, "打开分享页从不发起运行（声明了 auto 的按需视图也不）"
    by = {n["id"]: n for n in view["tree"]}
    assert by["gmv"]["data"]["state"] == "ok"
    assert by["asked"]["data"]["state"] == "empty" and "只显示定时出数的视图" in by["asked"]["data"]["message"]
    for cid, kind in (("todo", "task_list"), ("mine", "instance_list")):
        assert by[cid]["type"] == "notice" and by[cid]["share_placeholder"] == kind and by[cid]["props"]["text"]
        assert "data" not in by[cid], "不取实例、待办"


async def test_fixed_mode_stays_on_the_runs_it_was_created_with(world):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    first = await formal(wf, "W1")
    fixed = await share(app_id, mode="fixed")
    latest = await share(app_id)
    second = await formal(wf, "W2")
    async with anonymous(**{HEADER: fixed["key"]}) as c:
        pinned = (await c.get("/api/shares/page")).json()
        other = await c.get("/api/shares/report.html", params={"run": second.id})
        assert other.status_code == 404 and other.json()["code"] == "share_run_not_shared"
    async with anonymous(**{HEADER: latest["key"]}) as c:
        moving = (await c.get("/api/shares/page")).json()
    assert pinned["view"]["sources"]["latest"]["run_id"] == first.id
    points = pinned["view"]["tree"][1]["data"]["series"][-1]["points"]
    assert [p["run_id"] for p in points] == [first.id], "趋势也停在建分享的那一期"
    assert moving["view"]["sources"]["latest"]["run_id"] == second.id


async def test_runs_issued_from_questions_are_never_the_latest(world):  # noqa: F811
    """问数据的答案按口径正式出具的运行（ledger_issue）参数是从某人的提问里绑出来的：应用页、趋势、分享链接（latest 和建
    fixed 时取的那次）、视图数据接口的「最近一次封存」都不取它，下载报告也不认它（复核 H1）。"""
    from app.engine.launch import launch_run

    svc = await auth_env.make_service()
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [("user", "svc-portal", "observe")])
    regular = await formal(wf, "W1")
    async with SessionLocal() as s:
        launched = await launch_run(s, workflow_id=wf, run_class="formal", version=1, input_payload={"week": "W9"},
                                    actor="编辑", origin_kind="ledger_issue", origin={"source_run": "提问那次"},
                                    expect_hash=h)
    issued = await wait(launched.id)
    assert issued.status == "succeeded" and issued.manifest_hash, "出具的运行成功并封存了，是更新的那一次"
    admin = await auth_env.login("root")
    page = (await admin.get(f"/api/apps/{app_id}/pages/home")).json()
    assert page["sources"]["latest"]["run_id"] == regular.id, "看板不换成某人提问的那组参数"
    points = page["tree"][1]["data"]["series"][-1]["points"]
    assert [p["run_id"] for p in points] == [regular.id], "趋势里也没有它"
    _, secret = await auth_env.make_key(svc, scopes=("apps.read",))
    data = (await auth_env.keyed(secret).get(f"/api/apps/{app_id}/views/latest/data")).json()
    assert data["state"] == "ok" and data["run_id"] == regular.id, "视图数据接口同一个取法"
    fixed = await share(app_id, mode="fixed")
    latest = await share(app_id)
    for made in (fixed, latest):
        async with anonymous(**{HEADER: made["key"]}) as c:
            body = (await c.get("/api/shares/page")).json()
            assert body["view"]["sources"]["latest"]["run_id"] == regular.id, made["mode"]
            refused = await c.get("/api/shares/report.html", params={"run": issued.id})
            assert refused.status_code == 404 and refused.json()["code"] == "share_run_not_shared", made["mode"]


def test_every_run_origin_is_classified_for_latest_sealed():
    """每种运行来源都要说清算不算「最近一次封存」：以后加了来源、没在 apps/resolve 归类的，这里先失败。"""
    from app.apps.resolve import NOT_SEALED_ORIGINS, SEALED_ORIGINS
    from app.engine.origin import ORIGIN_KINDS

    assert not set(SEALED_ORIGINS) & set(NOT_SEALED_ORIGINS)
    assert set(SEALED_ORIGINS) | set(NOT_SEALED_ORIGINS) == set(ORIGIN_KINDS)
    assert "ledger_issue" in NOT_SEALED_ORIGINS and "app_backfill" in NOT_SEALED_ORIGINS


async def test_report_download_only_for_the_shares_runs_and_masked(world, monkeypatch, tmp_path):  # noqa: F811
    import sqlite3

    from app.data.engine import engines
    from app.engine.launch import launch_run
    from tests import test_report_masked_metrics as vipmod

    path = tmp_path / "vip.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", vipmod.CUSTOMERS)
    db.commit()
    db.close()
    source_id = await vipmod.set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    world["sources"].append("vip")
    vipmod.script(monkeypatch, vipmod.CLEAN)
    wf, h = await workflow(vipmod.graph(on_violation="flag"))
    spec = board(wf, h, body=[{"id": "n", "type": "metric", "bind": {"view": "latest", "metric": "card.n_cust"}},
                              {"id": "doc", "type": "report", "bind": {"view": "latest", "report": "write"}}])
    app_id = await published(world, spec, [])
    async with SessionLocal() as s:
        launched = await launch_run(s, workflow_id=wf, run_class="formal", version=1, input_payload={},
                                    actor="编辑", origin_kind="launcher")
    run = await wait(launched.id)
    assert run.status == "succeeded", run.error
    made = await share(app_id)
    kpi_wf, _ = await workflow(KPI)
    stranger = await formal(kpi_wf)
    async with anonymous(**{HEADER: made["key"]}) as c:
        body = (await c.get("/api/shares/page")).json()
        by = {n["id"]: n for n in body["view"]["tree"]}
        assert by["doc"]["type"] == "notice" and "下载报告" in by["doc"]["props"]["text"], "报告正文不在线打开"
        [download] = body["downloads"]
        assert download["run_id"] == run.id and download["reports"] == ["write"]
        report = await c.get("/api/shares/report.html", params={"run": run.id})
        assert report.status_code == 200, report.text
        assert "attachment" in report.headers["content-disposition"]
        assert vipmod.leaks(report.text) == [], "报告里没有遮罩列的原值"
        refused = await c.get("/api/shares/report.html", params={"run": stranger.id})
        assert refused.status_code == 404 and refused.json()["code"] == "share_run_not_shared"
        no_header = await anonymous().get("/api/shares/report.html", params={"run": run.id})
        assert no_header.status_code == 400
    async with SessionLocal() as s:
        exported = (await s.execute(select(AuditEvent).where(AuditEvent.action == "report.exported",
                                                             AuditEvent.subject_id == run.id))).scalars().all()
    assert [(e.actor, e.verified) for e in exported] == [(None, False)], "导出记审计，导出人不冒充谁"


async def test_revoked_and_expired_links_say_so_with_410(world):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    await formal(wf)
    gone = await share(app_id)
    old = await share(app_id)
    admin = await auth_env.login("root")
    r = await admin.delete(f"/api/apps/{app_id}/shares/{gone['id']}")
    assert r.status_code == 200 and r.json()["state"] == "revoked" and r.json()["revoked_by"] == "root"
    # 撤销交回的那一条和建、列的同一个形状：page_title 是页面标题，不是页面 id（下一轮验收：以前撤销回的是「home」）
    assert gone["page_title"] == "首页" and r.json()["page_title"] == gone["page_title"], r.json()["page_title"]
    again = await admin.delete(f"/api/apps/{app_id}/shares/{gone['id']}")
    assert again.json()["page_title"] == "首页", "撤销过的再撤销照旧交回它，标题一样"
    async with SessionLocal() as s:
        (await s.get(ViewShare, old["id"])).expires_at = utcnow() - timedelta(minutes=5)
        await s.commit()
    async with anonymous() as c:
        r = await c.get("/api/shares/page", headers={HEADER: gone["key"]})
        assert r.status_code == 410 and r.json()["code"] == "share_revoked" and "链接已撤销" in r.json()["detail"]
        r = await c.get("/api/shares/page", headers={HEADER: old["key"]})
        assert r.status_code == 410 and r.json()["code"] == "share_expired" and "链接已到期" in r.json()["detail"]
    live = await share(app_id)
    await set_switch(False)
    async with anonymous(**{HEADER: live["key"]}) as c:
        r = await c.get("/api/shares/page")
        assert r.status_code == 403 and r.json()["code"] == "share_links_off", "管理员关掉开关，已有的一并打不开"
    assert (await admin.delete(f"/api/apps/{app_id}/shares/{live['id']}")).status_code == 200, "关着也能撤销"


async def test_create_and_revoke_are_audited_and_opening_only_counts(world):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    await formal(wf)
    made = await share(app_id, embed_allowed=True)
    async with anonymous(**{HEADER: made["key"]}) as c:
        for _ in range(3):
            assert (await c.get("/api/shares/page")).status_code == 200
    admin = await auth_env.login("root")
    await admin.delete(f"/api/apps/{app_id}/shares/{made['id']}")
    await admin.delete(f"/api/apps/{app_id}/shares/{made['id']}")
    async with SessionLocal() as s:
        row = await s.get(ViewShare, made["id"])
        actions = (await s.execute(select(AuditEvent.action, AuditEvent.actor).where(
            AuditEvent.subject_id == made["id"]).order_by(AuditEvent.at))).all()
    assert row.open_count == 3 and row.last_opened_at is not None and row.embed_allowed is True
    assert actions == [("app.share_create", "root"), ("app.share_revoke", "root")], "打开不记审计；撤销两次只记一次"


async def test_access_keys_cannot_reach_share_routes(world):  # noqa: F811
    wf, h = await workflow(KPI)
    app_id = await published(world, board(wf, h), [])
    made = await share(app_id)
    svc = await auth_env.make_service()
    _, secret = await auth_env.make_key(svc)
    c = auth_env.keyed(secret, **{HEADER: made["key"]})
    for method, path in (("GET", "/api/shares/page"), ("GET", f"/api/apps/{app_id}/shares"),
                         ("POST", f"/api/apps/{app_id}/shares")):
        r = await c.request(method, path, json={"page_id": "home", "expires_at": soon()} if method == "POST" else None)
        assert r.status_code == 403 and r.json()["code"] == "key_route_forbidden", (method, path)


# --------------------------------------------------------------------------
# /s 的安全头
# --------------------------------------------------------------------------


_PROBE = r"""
import asyncio, json, httpx
from app.main import app

async def main():
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        out = {}
        for p in ("/s", "/apps/x", "/api/shares/page"):
            r = await c.get(p)
            out[p] = {"status": r.status_code, **{k: r.headers.get(k) for k in (
                "content-security-policy", "x-frame-options", "referrer-policy")}}
        print(json.dumps(out))

asyncio.run(main())
"""


def test_share_page_uses_the_embed_whitelist_and_no_referrer(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body><div id=root></div></body></html>")
    env = {**os.environ, "AGENTLAB_DATA_DIR": str(tmp_path / "data"), "AGENTLAB_WEB_DIST": str(dist),
           "AGENTLAB_SECRET_KEY": "test-only-not-a-real-key", "AGENTLAB_EMBED_ORIGINS": "https://bi.example.org",
           "AGENTLAB_FRAME_ANCESTORS": ""}
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["/s"]["status"] == 200, "分享页由页面托管给出（不进外壳由前端定）"
    assert out["/s"]["content-security-policy"] == "frame-ancestors 'self' https://bi.example.org"
    assert out["/s"]["x-frame-options"] is None and out["/s"]["referrer-policy"] == "no-referrer"
    assert out["/apps/x"]["content-security-policy"] == "frame-ancestors 'self'", "别的页面不跟着放开嵌入"
    assert out["/apps/x"]["x-frame-options"] == "SAMEORIGIN"
    assert out["/api/shares/page"]["status"] == 400, "关登录时也只认请求头"
