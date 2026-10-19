"""按口径正式出具（next-round 02 做法 5，拍板 L1、L5，app/ledger/issue.py）。

真的跑探索运行（虚构店铺库、mock 模型写报告），等后台比对做完，看自动出具、一键出具、拒绝原因、去重：
- 问数据的答案整题独立等价于受管口径：比对完自动发起一次正式运行（来源 ledger_issue），origin 记来源运行、口径、绑定
  哈希，参数是绑定 + 缺省值，按 graph_hash 钉住；GET 账本的 actions 写能自动、issued 列出它；再点一次交回同一次；
- 已发布级别、助手选用的、不是问数据的（画布试运行等）：不自动，只给一键（写明为什么），一键照常发起；设置关了自动出具
  也不自动；
- 拒绝写明原因：整题不等价、点名别的口径、版本退役、图里有人工节点、缺入口字段、超过每日上限、使用者没有应用授权；
  使用者有授权（应用固定着这一版）就能出具，origin 记靠的是哪个应用；
- 设置 ledger 一组：缺省值、写坏了拒收。
数据是虚构店铺库。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import Principal
from app.db.base import SessionLocal
from app.db.models import Run, Setting, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import issue as ledger_issue
from app.ledger import match as ledger_match
from app.main import app
from tests import ledger_samples as S

ADMIN = Principal(username=None, role="admin", mode="off", via="off")


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    # 测试库按进程共用：每日上限数的是今天全部的按口径出具，别的用例发起的也算。每个用例从缺省设置起步、上限放宽，
    # 要测上限的用例自己改
    await reset_ledger(issue_daily_limit=100_000)
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def reset_ledger(**values) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "ledger")
        if row is not None:
            await session.delete(row)
        if values:
            session.add(Setting(key="ledger", value=values))
        await session.commit()


async def set_ledger(**values) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "ledger")
        merged = {**(row.value if row else {}), **values}
        if row:
            row.value = merged
        else:
            session.add(Setting(key="ledger", value=merged))
        await session.commit()


async def issued(run_id: str) -> list[Run]:
    async with SessionLocal() as session:
        return await ledger_issue.issued_runs(session, run_id)


async def settle(run_id: str) -> list[Run]:
    """等自动出具发起的正式运行跑完。"""
    # 自动出具在比对里就发起了（ledger/match.match_run 等它），drain 之后有没有已经定了：只等发起了的跑完
    for _ in range(200):
        found = await issued(run_id)
        if all(r.status in ("succeeded", "failed", "cancelled") for r in found):
            return found
        await asyncio.sleep(0.05)
    return await issued(run_id)


def two_writers(monkeypatch, answer: str = "本周销售额 [[v:Q1.r0.销售额]]。") -> None:
    """mock 模型：口径那张图的报告（指令「写周销售」）引用口径卡的指标，问数据那张图的报告照 answer 写。"""
    from langchain_core.messages import AIMessage

    from app.providers import mock_model

    def decide(self, messages):
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        return AIMessage(content="周销售 [[m:gmv]]。" if "写周销售" in text else answer)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def equivalent_run(monkeypatch, origin: str = "chat"):
    """一次整题独立等价于「周销售」v1 的探索运行（缺省是问数据的答案），等它封存、比对完。"""
    from app.engine.runner import run_manager as manager

    two_writers(monkeypatch)
    run = await manager.start(graph=S.explore_graph(S.EQUIVALENT), input_payload={}, origin_kind=origin)
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    return row


# --------------------------------------------------------------------------
# 自动出具
# --------------------------------------------------------------------------


async def test_an_independent_equivalent_answer_to_a_governed_caliber_is_issued_automatically(client, monkeypatch):
    wf = await S.publish()
    row = await equivalent_run(monkeypatch)
    [formal] = await settle(row.id)
    assert formal.run_class == "formal" and formal.origin_kind == "ledger_issue" and formal.status == "succeeded"
    assert (formal.workflow_id, formal.version) == (wf, 1)
    assert formal.input == {"start": "2026-09-01", "end": "2026-09-08", "store": "all"}
    async with SessionLocal() as session:
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).scalar_one()
    assert formal.version_hash == version.graph_hash
    origin = formal.origin
    assert origin["source_run"] == row.id and origin["caliber"] == f"{wf}@1" and origin["how"] == "independent"
    assert origin["auto"] is True and origin["binding_hash"] and origin["match"]

    body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
    [action] = [a for a in body["actions"] if a["kind"] == "issue"]
    assert action["available"] is True and action["auto"] is True and action["issued_run"] == formal.id
    assert action["level_text"] == "受管" and action["workflow_name"] == "周销售"
    [item] = body["issued"]
    assert item["run_id"] == formal.id and item["auto"] is True and item["status"] == "succeeded"

    # 再点一次「按口径出具」：同一运行、同一口径、同一组参数，交回已经发起的那一次
    again = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["run_id"] == formal.id
    assert len(await issued(row.id)) == 1


async def test_only_answers_in_chat_are_issued_automatically(client, monkeypatch):
    """自动出具只给问数据的答案（拍板 L1、做法 5）：画布上反复试跑一份和受管口径等价的草图，每次都是新的来源运行，
    自动的话每试一次就多一次正式运行（含报告撰写的模型费用）。画布试运行只给一键，写明为什么，一键照常发起。"""
    wf = await S.publish()
    row = await equivalent_run(monkeypatch, origin="canvas")
    assert await settle(row.id) == []
    [action] = [a for a in (await client.get(f"/api/runs/{row.id}/ledger")).json()["actions"] if a["kind"] == "issue"]
    assert action["available"] is True and action["auto"] is False
    assert action["auto_reason"] == ledger_issue.AUTO_NOT_CHAT.format(what="画布试运行")
    assert await ledger_issue.auto_issue(row.id) is None and await issued(row.id) == []
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 200 and res.json()["created"] is True
    [formal] = await settle(row.id)
    assert formal.origin["auto"] is False and formal.status == "succeeded"


async def test_published_level_calibers_only_get_one_click(client, monkeypatch):
    wf = await S.publish(level="published")
    row = await equivalent_run(monkeypatch)
    assert await settle(row.id) == []
    body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
    [action] = [a for a in body["actions"] if a["kind"] == "issue"]
    assert action["available"] is True and action["auto"] is False and action["auto_reason"] == ledger_issue.AUTO_LEVEL
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 200 and res.json()["created"] is True
    [formal] = await settle(row.id)
    assert formal.origin["auto"] is False and formal.status == "succeeded"
    assert res.json()["issued"]["run_id"] == formal.id


async def test_switching_auto_issue_off_keeps_one_click(client, monkeypatch):
    await S.publish()
    await set_ledger(auto_issue=False)
    row = await equivalent_run(monkeypatch)
    assert await settle(row.id) == []
    [action] = (await client.get(f"/api/runs/{row.id}/ledger")).json()["actions"]
    assert action["auto"] is False and action["auto_reason"] == ledger_issue.AUTO_OFF


async def test_composed_answers_are_never_issued_automatically(monkeypatch):
    from app.ledger import compose
    from app.look.calibers import CaliberKind

    wf = await S.publish()
    async with SessionLocal() as session:
        kind = await CaliberKind.load(session, compose=True)
    made = compose.Composer(list(kind.options)).compose(
        {"op": "use_caliber", "id": "c1", "workflow_id": wf, "version": 1, "metrics": ["gmv"],
         "inputs": {"start": "2026-09-01", "end": "2026-09-08"}}, taken={"start"})
    graph = S.chain(S.node("start", "input"), *made.nodes, S.node("write", "report", instructions="写答案"),
                    S.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]))
    two_writers(monkeypatch, "本周销售额 [[m:gmv]]。")
    row = await S.wait((await run_manager.start(graph=graph, input_payload={}, origin_kind="chat")).id)
    await ledger_match.drain()
    assert await settle(row.id) == []
    async with SessionLocal() as session:
        recorded = await ledger_match.store.latest(row.id)
        plan = await ledger_issue.plan_issue(session, await session.get(Run, row.id), recorded.content, wf, 1,
                                             principal=ADMIN)
    assert plan.available and plan.how == "composed" and plan.auto_reason == ledger_issue.AUTO_COMPOSED


# --------------------------------------------------------------------------
# 拒绝
# --------------------------------------------------------------------------


async def test_refusals_say_why(client, monkeypatch):
    wf = await S.publish(level="published")
    other = await S.publish(name="另一份口径", level="published")
    unrelated = await S.explore(monkeypatch, S.WITH_REFUNDS)
    res = await client.post(f"/api/ledger/answers/{unrelated.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 409 and res.json()["code"] == ledger_issue.REFUSED
    assert res.json()["detail"].startswith("整题没有等价于这份口径")

    row = await equivalent_run(monkeypatch)
    whole_on = (await ledger_match.store.latest(row.id)).content["whole"]["workflow_id"]
    named = other if whole_on == wf else wf
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": named, "version": 1})
    assert res.status_code == 409 and "整题等价的是另一份口径" in res.json()["detail"]

    # 版本退役
    async with SessionLocal() as session:
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == whole_on, WorkflowVersion.version == 1))).scalar_one()
        version.retired_at = datetime.now(timezone.utc)
        await session.commit()
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": whole_on, "version": 1})
    assert res.status_code == 409 and "已退役" in res.json()["detail"]


async def test_a_caliber_with_a_human_step_is_not_issued_on_anyones_behalf(client, monkeypatch):
    graph = S.caliber_graph()
    graph["nodes"].insert(3, S.node("ok", "human", mode="approve", prompt="确认"))
    graph["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(graph["nodes"], graph["nodes"][1:])]
    wf = await S.publish(graph=graph)
    row = await equivalent_run(monkeypatch)
    assert await settle(row.id) == []
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 409 and "人工节点" in res.json()["detail"] and "不代人做这些事" in res.json()["detail"]


async def test_missing_entry_fields_are_named(client, monkeypatch):
    graph = S.caliber_graph()
    graph["nodes"][0]["data"]["config"]["fields"].append({"name": "region", "required": True})
    wf = await S.publish(graph=graph)
    row = await equivalent_run(monkeypatch)
    [action] = (await client.get(f"/api/runs/{row.id}/ledger")).json()["actions"]
    assert action["available"] is False and "请填写「region」" in action["reason"]
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 409 and "入口字段还不齐" in res.json()["detail"]


async def test_the_daily_limit_counts_every_issue(client, monkeypatch):
    wf = await S.publish(level="published")
    await set_ledger(issue_daily_limit=0)
    row = await equivalent_run(monkeypatch)
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 429 and res.json()["code"] == ledger_issue.DAILY_LIMIT and "上限（0 次）" in res.json()["detail"]


async def test_users_issue_only_versions_pinned_by_apps_they_can_use(monkeypatch):
    from app.db.models_apps import App, AppGrant, AppVersion

    wf = await S.publish(level="published")
    row = await equivalent_run(monkeypatch)
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
        run.started_by = "carol"
        await session.commit()
    carol = Principal(username="carol", role="user", verified=True, mode="local")
    with pytest.raises(ledger_issue.IssueRefused) as e:
        await ledger_issue.issue(row.id, wf, 1, principal=carol)
    assert e.value.status_code == 403 and e.value.message == ledger_issue.ASK_BUILDER
    # 别人的运行读不到：先说读不到
    dave = Principal(username="dave", role="user", verified=True, mode="local")
    with pytest.raises(ledger_issue.IssueRefused) as e:
        await ledger_issue.issue(row.id, wf, 1, principal=dave)
    assert e.value.status_code == 403 and e.value.message == ledger_issue.NO_RUN_ACCESS

    async with SessionLocal() as session:
        board = App(name="周销售看板", published_version=1, created_by="bob")
        session.add(board)
        await session.flush()
        session.add(AppVersion(app_id=board.id, version=1, spec={}, spec_hash="x",
                               pins=[{"alias": "week", "workflow_id": wf, "version": 1}]))
        session.add(AppGrant(app_id=board.id, principal_kind="user", principal="carol", role="use"))
        await session.commit()
        board_id = board.id
    formal, created = await ledger_issue.issue(row.id, wf, 1, principal=carol)
    assert created and formal.started_by == "carol" and formal.origin["granted_by_app"] == board_id


async def test_different_answers_issued_at_once_stop_exactly_at_the_daily_limit(monkeypatch):
    """几份不同的答案同时出具（不再一把全站锁一个接一个）：先在库里占今天的额度再发起，正好到上限为止，多出来的回 429。"""
    wf = await S.publish(level="published")
    rows = [await equivalent_run(monkeypatch) for _ in range(3)]
    from app.cluster import quota

    async with SessionLocal() as session:
        issued = await ledger_issue.issued_today(session)
    # 今天的额度在库里记着（同一个测试进程里前面的测试也出具过）；还没记过的按今天已出具的起算
    already = await quota.used(ledger_issue.QUOTA)
    already = issued if already is None else already
    await set_ledger(issue_daily_limit=max(already, issued) + 2)
    results = await asyncio.gather(*(ledger_issue.issue(r.id, wf, 1, principal=ADMIN) for r in rows),
                                   return_exceptions=True)
    made = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, ledger_issue.IssueRefused)]
    assert len(made) == 2 and all(created for _, created in made), results
    assert len(refused) == 1 and refused[0].status_code == 429 and refused[0].code == ledger_issue.DAILY_LIMIT
    async with SessionLocal() as session:
        assert await ledger_issue.issued_today(session) == issued + 2


async def test_concurrent_requests_issue_once(monkeypatch):
    wf = await S.publish(level="published")
    row = await equivalent_run(monkeypatch)
    results = await asyncio.gather(*(ledger_issue.issue(row.id, wf, 1, principal=ADMIN) for _ in range(3)))
    assert sorted(created for _, created in results) == [False, False, True]
    assert len({r.id for r, _ in results}) == 1


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


async def test_ledger_settings_have_defaults_and_refuse_bad_values(client):
    await reset_ledger()
    body = (await client.get("/api/settings")).json()
    assert body["ledger"] == {"auto_issue": True, "issue_daily_limit": 20, "match_timeout_s": 8, "recall_limit": 50}
    bad = await client.put("/api/settings", json={"values": {"ledger": {"auto_issue": "yes"}}})
    assert bad.status_code == 422 and "自动按口径正式出具" in bad.json()["detail"]
    bad = await client.put("/api/settings", json={"values": {"ledger": {"match_timeout_s": 600}}})
    assert bad.status_code == 422 and "口径比对时限" in bad.json()["detail"]
    bad = await client.put("/api/settings", json={"values": {"ledger": {"recall_limit": -1}}})
    assert bad.status_code == 422 and "口径升版后每次最多重算几组" in bad.json()["detail"]
    ok = await client.put("/api/settings", json={"values": {"ledger": {"auto_issue": False, "issue_daily_limit": 5,
                                                                        "match_timeout_s": 3}}})
    assert ok.status_code == 200 and ok.json()["ledger"] == {"auto_issue": False, "issue_daily_limit": 5,
                                                             "match_timeout_s": 3, "recall_limit": 50}
    ok = await client.put("/api/settings", json={"values": {"ledger": {"recall_limit": 0}}})
    assert ok.status_code == 200 and ok.json()["ledger"]["recall_limit"] == 0


async def test_auto_issue_goes_by_the_askers_permissions_when_login_is_on(monkeypatch):
    """开登录时自动出具按这次运行的发起人判（拍板 L5）：使用者没有应用授权就不自动出具，有了才出具；账号不在了的按使用者算。"""
    from app.db.models import User
    from app.db.models_apps import App, AppGrant, AppVersion

    monkeypatch.setattr("app.auth.current_mode", lambda: "local")
    await set_ledger(auto_issue=False)
    wf = await S.publish()
    row = await equivalent_run(monkeypatch)
    await set_ledger(auto_issue=True)
    async with SessionLocal() as session:
        session.add(User(username=f"erin_{row.id[:6]}", role="user", password_hash="x"))
        (await session.get(Run, row.id)).started_by = f"erin_{row.id[:6]}"
        await session.commit()
    assert await ledger_issue.auto_issue(row.id) is None and await issued(row.id) == []

    async with SessionLocal() as session:
        board = App(name="周销售看板", published_version=1, created_by="bob")
        session.add(board)
        await session.flush()
        session.add(AppVersion(app_id=board.id, version=1, spec={}, spec_hash="x",
                               pins=[{"alias": "week", "workflow_id": wf, "version": 1}]))
        session.add(AppGrant(app_id=board.id, principal_kind="user", principal=f"erin_{row.id[:6]}", role="use"))
        await session.commit()
    launched = await ledger_issue.auto_issue(row.id)
    [formal] = await settle(row.id)
    assert launched == formal.id and formal.started_by == f"erin_{row.id[:6]}" and formal.origin["auto"] is True


async def test_binding_values_that_would_break_the_sql_are_not_filled_in(client, monkeypatch):
    """绑定出来的值要渲染进口径的 SQL 模板：写在引号里的值带着引号（这次的 SQL 里写的是转义过的 ''，绑定照原样取出）就不代填
    出具——和组合时校验助手给的参数是同一个判断，宁可少出具。"""
    template = "SELECT SUM(amount) AS gmv FROM orders WHERE status = '{{ input.status }}'"
    graph = S.caliber_graph(template)
    graph["nodes"][0]["data"]["config"]["fields"] = [{"name": "status", "required": True}]
    wf = await S.publish(graph=graph, level="published")
    two_writers(monkeypatch, "本周销售额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=S.explore_graph("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'pa''id'"),
                                  input_payload={}, origin_kind="canvas")
    row = await S.wait(run.id)
    await ledger_match.drain()
    whole = (await ledger_match.store.latest(row.id)).content["whole"]
    assert whole["equivalent"] is True and whole["binding"] == {"status": "pa''id"}
    res = await client.post(f"/api/ledger/answers/{row.id}/issue", json={"workflow_id": wf, "version": 1})
    assert res.status_code == 409 and "不能有引号" in res.json()["detail"] and "不代填出具" in res.json()["detail"]
