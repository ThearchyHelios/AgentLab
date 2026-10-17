"""口径召回（next-round 02 做法 7，拍板 L6、L7；app/ledger/uses.py、recall.py）。

真的跑：虚构店铺库、mock 模型写报告。受管口径「周销售」v1 只算已支付的，问数据问过几周、正式运行跑过；把它发布成 v2
（改一个筛选条件）之后：
- 计划：按（旧版本、新版本、参数）分组去重——问数回答绑定出的参数和正式运行的输入补齐缺省值之后是同一组；上限 recall_limit
  之内自动发起，超出的写「另有 M 组未重算」，手动接着算；再排一次、服务重启之后重算计划，算过的不再发起（幂等）；
- 召回运行：派生图里新旧两版的取数和口径卡各一份（前缀不撞），召回卡里算两版之差；同一份数据上两版各算一次；
- 收尾：数变了才开 recall 待办（挂在召回运行上，旧值、新值、差值、原答案当时的值都给），推 ledger.recall，消息里没有数；
  数没变的在原答案上记 ledger.recalled（已按 vN 核对，不影响），不开待办；
- 不能重算的照实记：新版入口要的参数这一组里没有、图里会写外部系统；图里别处有人工节点的照样重算（召回图碰不到它），
  只是「按新版重新出具」不代人审批；
- 待办「按新版重新出具」走按口径出具同一套判法、「知道了」；
- 目录里影响数字的一项被改了：冻结目录里看得到它的答案标「依据有变」，按新目录多出错误级问题的才开待办；
- 表结构变了：用过这张表的答案标「依据的表结构已变」，口径试算算不出来才开待办；
- E4：每次发布召回了几组、其中数变了的比例。
数据都是编的。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting, Task, TaskAssignment
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.ledger import recall
from app.main import app
from tests import ledger_samples as S

#: v2：已支付和已退款的都算（召回要算出差值的那种改法）
WITH_REFUNDED = ("SELECT SUM(amount) AS gmv FROM orders WHERE status IN ('paid', 'refunded') "
                 "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
WEEK1 = {"start": "2026-09-01", "end": "2026-09-08"}


def week_sql(start: str, end: str) -> str:
    return (f"select sum(o.amount) as 销售额 from orders o where o.paid_at >= '{start}' and o.status = 'paid' "
            f"and o.paid_at < '{end}'")


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    await S.fresh_source(path)
    await ledger_settings(auto_issue=False, issue_daily_limit=100_000)
    yield path
    await ledger_match.drain()
    await recall.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def ledger_settings(**values) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "ledger")
        if row is not None:
            await session.delete(row)
        session.add(Setting(key="ledger", value=values))
        await session.commit()


async def answer(monkeypatch, start: str = "2026-09-01", end: str = "2026-09-08") -> Run:
    """问数据问一周的销售额（独立写出、和口径等价的 SQL）。"""
    return await S.explore(monkeypatch, week_sql(start, end), text="本周销售额 [[v:Q1.r0.销售额]]。", origin="chat")


async def publish_v2(wf: str, sql: str = WITH_REFUNDED, *, graph=None) -> None:
    await S.publish(graph=graph or S.caliber_graph(sql), version=2, workflow_id=wf)


async def run_recall(wf: str, version: int = 2, *, batch: str = "auto") -> list[Run]:
    """按计划发起一批、等召回运行跑完、等收尾做完。"""
    ids = await recall.launch(wf, version, batch=batch)
    done = [await S.wait(rid) for rid in ids]
    await recall.drain()
    return done


async def events(run_id: str, kind: EventType) -> list[dict]:
    async with SessionLocal() as session:
        return [d for d in (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == run_id, RunEvent.type == kind).order_by(RunEvent.seq))).scalars()]


async def recall_tasks(run_id: str) -> list[Task]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Task).where(Task.run_id == run_id, Task.kind == "recall"))).scalars())


# --------------------------------------------------------------------------
# 计划：分组、去重、上限、幂等
# --------------------------------------------------------------------------


async def test_plan_groups_answers_and_formal_runs_by_their_binding(monkeypatch):
    wf = await S.publish()
    a1 = await answer(monkeypatch)
    a2 = await answer(monkeypatch)                         # 同一周再问一次：同一组
    a3 = await answer(monkeypatch, "2026-09-08", "2026-09-15")
    await publish_v2(wf)
    async with SessionLocal() as session:
        plan = await recall.build_plan(session, wf, 2)
    assert plan.problem is None and [p.old.node_id for p in plan.pairs[1]] == ["card"]
    groups = {tuple(sorted(g.binding.items())): g for g in plan.groups}
    week1 = groups[(("end", "2026-09-08"), ("start", "2026-09-01"), ("store", "all"))]
    assert sorted(u.run_id for u in week1.uses) == sorted([a1.id, a2.id]), "同一组参数去重成一组（缺省值补齐后比）"
    assert week1.old_version == 1 and week1.new_version == 2 and week1.problem is None
    assert week1.inputs == {"start": "2026-09-01", "end": "2026-09-08", "store": "all"}
    assert len(plan.groups) == 2 and len(plan.todo()) == 2
    assert {u.run_id for g in plan.groups for u in g.uses} == {a1.id, a2.id, a3.id}
    assert all(u.kind == "answer" and u.turn_id is None for g in plan.groups for u in g.uses)


async def test_limit_continue_and_idempotent_replanning(monkeypatch):
    await ledger_settings(auto_issue=False, issue_daily_limit=100_000, recall_limit=1)
    wf = await S.publish()
    await answer(monkeypatch)
    await answer(monkeypatch, "2026-09-08", "2026-09-15")
    await publish_v2(wf)
    async with SessionLocal() as session:
        before = await recall.preview(session, wf, 2)
    assert before["groups"] == 2 and before["will_recompute"] == 1 and before["over_limit"] == 1
    assert before["versions"][0]["answers"] == 2 and before["caliber"] is True
    first = await run_recall(wf)
    assert len(first) == 1 and first[0].origin["batch"] == "auto"
    # 再排一次（重复的发布钩子、服务重启后重算计划）：自动那一批已经发满，不再发起
    assert await recall.launch(wf, 2, batch="auto") == []
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["counts"]["pending"] == 1 and view["remaining"] == 1 and view["can_continue"] is True
    assert sum(1 for g in view["groups"] if g.get("run_id")) == 1
    # 手动接着算：发起剩下那一组；再接着算就没有了
    second = await run_recall(wf, batch="manual")
    assert len(second) == 1 and second[0].origin["batch"] == "manual"
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["remaining"] == 0 and view["can_continue"] is False
    assert await recall.launch(wf, 2, batch="manual") == []


async def test_superseded_version_offers_no_continue(client, monkeypatch):
    """v2 自动算了一批、还剩几组没算，接着又发布了 v3：v2 那一版不再给「接着算」（v3 的召回覆盖更早的所有版本，同启动时
    接着做只认当前发布版），写明去 v3 那一版看；手动点也照样拒绝，不再开「口径升到 v2」的待办。"""
    await ledger_settings(auto_issue=False, issue_daily_limit=100_000, recall_limit=1)
    wf = await S.publish()
    await answer(monkeypatch)
    await answer(monkeypatch, "2026-09-08", "2026-09-15")
    await publish_v2(wf)
    assert len(await run_recall(wf)) == 1
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["remaining"] == 1 and view["can_continue"] is True and view["continue_reason"] is None
    await S.publish(graph=S.caliber_graph(WITH_REFUNDED), version=3, workflow_id=wf)
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["remaining"] == 1 and view["can_continue"] is False
    assert view["continue_reason"] == "已有更新的发布版 v3，召回在 v3 那一版接着看"
    res = await client.post(f"/api/ledger/recall/{wf}/2/continue")
    assert res.status_code == 409 and res.json()["detail"] == view["continue_reason"], res.text
    assert await recall.launch(wf, 2, batch="manual") == [], "直接调也不再发起"
    async with SessionLocal() as session:
        assert len(await recall.recall_runs(session, wf, 2)) == 1
        current = await recall.batches(session, wf, 3)
    assert current["can_continue"] is True and current["continue_reason"] is None


async def test_zero_limit_lists_but_does_not_recompute(monkeypatch):
    await ledger_settings(auto_issue=False, issue_daily_limit=100_000, recall_limit=0)
    wf = await S.publish()
    await answer(monkeypatch)
    await publish_v2(wf)
    assert await recall.launch(wf, 2, batch="auto") == []
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["counts"]["pending"] == 1 and view["batch_size"] == 50, "上限是 0 时手动接着算按缺省的组数"


# --------------------------------------------------------------------------
# 召回运行与收尾
# --------------------------------------------------------------------------


async def test_changed_numbers_open_a_recall_task_with_all_three_values(client, monkeypatch):
    wf = await S.publish()
    a1 = await answer(monkeypatch)
    await publish_v2(wf)
    [done] = await run_recall(wf)
    assert done.status == "succeeded", done.error
    assert done.origin_kind == "recall" and done.run_class == "exploratory" and done.workflow_id is None
    ids = [n["id"] for n in done.graph["nodes"]]
    assert ids == ["derived_in", "old_fetch", "old_card", "new_fetch", "new_card", "recall", "derived_out"]
    nodes = {n["id"]: n for n in done.graph["nodes"]}
    assert nodes["old_card"]["data"]["config"]["caliber_from"] == {
        "workflow_id": wf, "workflow_version": 1, "node_id": "card", "nodes": {"fetch": "old_fetch"}}
    assert nodes["new_card"]["data"]["config"]["caliber_from"]["workflow_version"] == 2
    assert nodes["derived_out"]["data"]["config"]["contract"] == {"metrics_from": "recall"}
    assert not [n for n in done.graph["nodes"] if n["type"] in ("report", "llm", "agent")]

    values = await recall.card_values(done.id)
    # v1 只算已支付：100 + 50.5 + 20；v2 加上这一周退款的 30 + 8
    assert values["old_0"]["value"] == 170.5 and values["new_0"]["value"] == 208.5
    assert values["diff_0"]["value"] == 38.0 and values["diff_0"]["rendered"] == "38.00元"

    [settled] = [d for d in await events(done.id, EventType.LEDGER_RECALLED) if d.get("role") == "recall"]
    assert settled["changed"] is True and settled["answers"] == 1 and len(settled["tasks"]) == 1
    assert settled["post_seal"] is True
    assert await events(a1.id, EventType.LEDGER_RECALLED) == [], "数变了的不在原答案上记「不影响」"
    [task] = await recall_tasks(done.id)
    assert task.title == "口径「周销售」升到 v2，答案的数变了"
    # 问数据的轮次、证据面板读的账本接口：这个答案有一条没处理的召回待办（界面链到待办箱），没有「已核对」
    marks = (await client.get(f"/api/runs/{a1.id}/ledger")).json()["recall"]
    assert marks == {"checked": [], "basis": [], "tasks": [{"id": task.id, "reason": "caliber", "to_version": 2}]}
    res = await client.get(f"/api/tasks/{task.id}")
    assert res.status_code == 200, res.text
    payload = res.json()["payload"]
    assert payload["kind"] == "recall" and payload["reason"] == "caliber" and payload["to_version"] == 2
    [metric] = payload["metrics"]
    assert (metric["old"]["value"], metric["new"]["value"], metric["diff"]["value"]) == (170.5, 208.5, 38.0)
    assert metric["changed"] is True and metric["old"]["metric"] == "old_0" and metric["diff"]["metric"] == "diff_0"
    [ans] = payload["answers"]
    assert ans["run_id"] == a1.id and ans["kind"] == "answer"
    assert ans["original"][0]["value"] == 170.5 and ans["original"][0]["column"] == "销售额"
    assert [a["decision"] for a in payload["actions"]] == ["reissue", "ack"]
    assert payload["actions"][0]["available"] is True
    # 三个数各自点得开：召回卡上的差值的出处链追到两版的口径卡
    chain = await client.get(f"/api/runs/{done.id}/evidence/metrics/recall/diff_0")
    assert chain.status_code == 200, chain.text
    assert {s["node_id"] for s in chain.json()["chain"] if s["step"] == "input"} == {"old_card", "new_card"}


async def test_unchanged_numbers_are_only_booked_on_the_answer(monkeypatch):
    wf = await S.publish()
    a1 = await answer(monkeypatch)
    # v2 只换了报告怎么写，口径的数据部分没变
    graph = S.caliber_graph()
    graph["nodes"][3]["data"]["config"]["instructions"] = "写周销售（新的写法）"
    await publish_v2(wf, graph=graph)
    [done] = await run_recall(wf)
    assert done.status == "succeeded", done.error
    assert await recall_tasks(done.id) == []
    [booked] = await events(a1.id, EventType.LEDGER_RECALLED)
    assert booked == {"role": "answer", "changed": False, "recall_run": done.id, "workflow_id": wf,
                      "from_version": 1, "to_version": 2, "post_seal": True}
    # 账本接口带上「已按 v2 核对，不影响」：问数据的轮次、证据面板不用再去翻运行时间线
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        marks = (await c.get(f"/api/runs/{a1.id}/ledger")).json()["recall"]
    [checked] = marks["checked"]
    assert {k: checked[k] for k in ("workflow_id", "workflow_name", "from_version", "to_version", "changed",
                                     "recall_run")} == {"workflow_id": wf, "workflow_name": "周销售", "from_version": 1,
                                                        "to_version": 2, "changed": False, "recall_run": done.id}
    assert checked["at"] and marks["basis"] == [] and marks["tasks"] == []
    # 再收一次尾不会重复记
    assert await recall.settle(done.id) is None
    assert len(await events(a1.id, EventType.LEDGER_RECALLED)) == 1


@pytest.mark.parametrize("values", [{}, {"old_0": {"value": 1, "rendered": "1.00元"}}], ids=["none", "half"])
async def test_unreadable_recall_card_is_a_failure_not_unchanged(monkeypatch, values):
    """召回运行成功了、召回卡却读不出来（封存里找不到 recall 节点、读不出封存，或少了某个比较的旧值 / 新值）：照实记重算
    失败，不当成「数没变」去给原答案盖「已核对，不影响」，也不开待办。"""
    wf = await S.publish()
    a1 = await answer(monkeypatch)
    await publish_v2(wf)

    async def unreadable(run_id):
        return dict(values)

    monkeypatch.setattr(recall, "card_values", unreadable)
    [done] = await run_recall(wf)
    assert done.status == "succeeded", done.error
    [settled] = [d for d in await events(done.id, EventType.LEDGER_RECALLED) if d.get("role") == "recall"]
    assert settled["outcome"] == "failed" and settled["changed"] is None and settled["error"] == "读不出召回卡"
    assert await events(a1.id, EventType.LEDGER_RECALLED) == [], "读不出的不在原答案上记「不影响」"
    assert await recall_tasks(done.id) == []
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    [group] = view["groups"]
    assert group["state"] == "failed" and group["reason"] == "重算失败：读不出召回卡"


async def test_incompatible_entry_and_side_effects_are_not_recomputed(monkeypatch):
    wf = await S.publish()
    await answer(monkeypatch)
    graph = S.caliber_graph(WITH_REFUNDED)
    graph["nodes"][0]["data"]["config"]["fields"].append({"name": "region", "required": True})
    await publish_v2(wf, graph=graph)
    async with SessionLocal() as session:
        [group] = (await recall.build_plan(session, wf, 2)).groups
    assert group.problem is not None and group.problem.startswith("新版 v2 的入口要的参数这一组里没有或不合规")
    assert await recall.launch(wf, 2) == []
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["counts"]["not_recomputable"] == 1 and view["groups"][0]["reason"] == group.problem
    assert view["can_continue"] is False



async def test_human_node_elsewhere_still_recomputes_but_reissue_refuses(client, monkeypatch):
    """新版图里在口径卡下游有审批（取数 → 口径卡 → 报告 → 人工确认 → 出口）：召回图只有两版的取数和口径卡，人工节点碰不到，
    照样重算、数变了照样开待办（做法 7 不重算的只有三种：缺入口字段、数据部分不能复用、会写外部系统）；「按新版重新出具」
    走完整的正式运行，仍然不代人审批，照实写不能出具。"""
    wf = await S.publish(name="周销售（要审批）")
    await answer(monkeypatch)
    human = S.caliber_graph(WITH_REFUNDED)
    human["nodes"].insert(4, S.node("ok", "human", mode="approve", title="确认"))
    human["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(human["nodes"], human["nodes"][1:])]
    await S.publish(graph=human, version=2, workflow_id=wf)
    async with SessionLocal() as session:
        plan = await recall.build_plan(session, wf, 2)
    assert plan.groups and all(g.problem is None for g in plan.groups), [g.problem for g in plan.groups]
    assert plan.version_problem == {}
    [done] = await run_recall(wf)
    assert done.status == "succeeded", done.error
    assert not [n for n in done.graph["nodes"] if n["type"] == "human"], "召回图里没有人工节点"
    [task] = await recall_tasks(done.id)
    payload = (await client.get(f"/api/tasks/{task.id}")).json()["payload"]
    reissue = next(a for a in payload["actions"] if a["decision"] == "reissue")
    assert reissue["available"] is False and "人工节点" in reissue["reason"], reissue
    res = await client.post(f"/api/tasks/{task.id}/respond", json={"decision": "reissue"})
    assert res.status_code == 409 and "人工节点" in res.json()["detail"], res.text


async def test_writing_to_an_external_system_is_not_recomputed(monkeypatch):
    """新版图里调用工具节点绑的能力会写外部系统（capabilities.registry.writes_externally，同按新口径补算、按口径出具）：
    照实记「无法重算」，不发起。"""
    from types import SimpleNamespace

    from app.capabilities import registry

    wf = await S.publish()
    await answer(monkeypatch)
    graph = S.caliber_graph(WITH_REFUNDED)
    graph["nodes"].insert(4, S.node("pay", "tool", tool="erp_pay", args={}))
    graph["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(graph["nodes"], graph["nodes"][1:])]
    await publish_v2(wf, graph=graph)
    real = registry.get_capability

    async def get_capability(session, name):
        if name == "erp_pay":
            return SimpleNamespace(name=name, effect="write", effect_system="write", endpoint_id=None)
        return await real(session, name)

    monkeypatch.setattr(registry, "get_capability", get_capability)
    async with SessionLocal() as session:
        plan = await recall.build_plan(session, wf, 2)
    assert plan.groups and all("会写外部系统（erp_pay）" in (g.problem or "") for g in plan.groups), \
        [g.problem for g in plan.groups]
    assert await recall.launch(wf, 2) == []


async def test_plan_from_formal_runs_and_exporters_as_owners(monkeypatch):
    """正式运行也是用过旧版的答案；导出过报告的人也是答案的主人（关登录时待办派给任何人，主人照记在召回运行里）。"""
    from app.apps.audit import record
    from app.engine.launch import launch_run

    wf = await S.publish()
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    async with SessionLocal() as session:
        formal = await launch_run(session, workflow_id=wf, run_class="formal", version=1, input_payload=WEEK1,
                                  actor="analyst")
    formal = await S.wait(formal.id)
    assert formal.status == "succeeded", formal.error
    async with SessionLocal() as session:
        await record(session, action="report.exported", subject_kind="run", subject_id=formal.id, actor="reader",
                     data={})
        await session.commit()
    await publish_v2(wf)
    async with SessionLocal() as session:
        [group] = (await recall.build_plan(session, wf, 2)).groups
        usage = await recall.preview(session, wf, 2)
    [use] = group.uses
    assert use.kind == "formal" and use.exporters == ["reader"]
    assert ("user", "analyst") in use.owners and ("user", "reader") in use.owners
    assert usage["versions"][0]["exports"] == 1 and usage["versions"][0]["formal"] == 1
    [done] = await run_recall(wf)
    [task] = await recall_tasks(done.id)
    async with SessionLocal() as session:
        [assignment] = (await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id))).scalars()
    assert assignment.principal_kind == "anyone", "关登录时派给任何人"
    from app.api.deps import Principal
    from app.ledger.recall import task_payload

    async with SessionLocal() as session:
        payload = await task_payload(session, await session.get(Task, task.id),
                                     Principal(username=None, role="admin", mode="off", via="off"))
    [ans] = payload["answers"]
    assert ans["kind"] == "formal" and ans["original"][0]["value"] == 170.5


# --------------------------------------------------------------------------
# 待办的两个动作、通知
# --------------------------------------------------------------------------


async def test_reissue_and_acknowledge(client, monkeypatch):
    wf = await S.publish()
    await answer(monkeypatch)
    await answer(monkeypatch, "2026-09-01", "2026-09-05")      # 这几天也有退款：两组的数都变了
    await publish_v2(wf)
    runs = await run_recall(wf)
    tasks = [t for r in runs for t in await recall_tasks(r.id)]
    assert len(tasks) == 2
    S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
    res = await client.post(f"/api/tasks/{tasks[0].id}/respond", json={"decision": "reissue"})
    assert res.status_code == 200, res.text
    body = res.json()["task"]
    assert body["status"] == "resolved" and body["outcome"]["decision"] == "reissue"
    async with SessionLocal() as session:
        issued = await session.get(Run, body["outcome"]["issued_run"])
    assert issued.origin_kind == "ledger_issue" and issued.run_class == "formal" and issued.version == 2
    assert issued.origin["recall_run"] == tasks[0].run_id and issued.origin["how"] == "recall"
    assert issued.input["start"] == "2026-09-01" and issued.input["end"] in ("2026-09-08", "2026-09-05")
    await S.wait(issued.id)
    bad = await client.post(f"/api/tasks/{tasks[1].id}/respond", json={"decision": "approve"})
    assert bad.status_code == 422
    ok = await client.post(f"/api/tasks/{tasks[1].id}/respond", json={"decision": "ack", "note": "知道了"})
    assert ok.status_code == 200 and ok.json()["task"]["outcome"]["decision"] == "ack"
    again = await client.post(f"/api/tasks/{tasks[1].id}/respond", json={"decision": "ack"})
    assert again.status_code == 409


async def test_recall_notification_has_no_numbers(monkeypatch):
    from app.core.crypto import encrypt
    from app.db.models import NotifyChannel
    from app.notify import service

    sent: list[dict] = []

    async def capture(channel, message):
        sent.append({"channel": channel.name, **message})
        from app.notify.core import Outcome

        return Outcome(True, 200, 1, None)

    monkeypatch.setattr("app.notify.send", capture)
    async with SessionLocal() as session:
        row = NotifyChannel(name="召回-全局", scope="global", url=encrypt("https://hooks.example.com/recall-7d"),
                            events=["ledger.recall", "task.opened"])
        session.add(row)
        await session.commit()
        channel_id = row.id
    try:
        wf = await S.publish()
        await answer(monkeypatch)
        await publish_v2(wf)
        await run_recall(wf)
        await service.drain()
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(NotifyChannel, channel_id))
            await session.commit()
    mine = [m for m in sent if m["channel"] == "召回-全局"]
    assert [m["event"] for m in mine] == ["ledger.recall"], "订阅了两样的渠道只推一次"
    # 看给人读的部分（标题、各行）：编号是十六进制的 id，不拿来比
    text = mine[0]["title"] + "".join(f"{k}{v}" for k, v in mine[0]["lines"])
    for number in ("170.5", "208.5", "38.0", "38.00"):
        assert number not in text, f"消息里不放数：{number}"
    assert dict(mine[0]["lines"]) == {"状态": "待处理", "口径": "「周销售」", "版本": "v1 → v2", "答案": "问数回答 1 个",
                                      "召回": dict(mine[0]["lines"])["召回"]}
    assert "周销售" in mine[0]["title"] and "v2" in mine[0]["title"]


async def test_recall_message_counts_answers_by_kind(monkeypatch):
    """召回消息按种类分开写条数：一个问数回答、一次正式运行写「问数回答 1 个、正式运行 1 个」（下一轮验收：以前写「答案：2 个」，
    读的人以为是两个问数回答）。只写条数，照旧不放数。"""
    from app.core.crypto import encrypt
    from app.db.models import NotifyChannel
    from app.engine.launch import launch_run
    from app.notify import service

    sent: list[dict] = []

    async def capture(channel, message):
        sent.append({"channel": channel.name, **message})
        from app.notify.core import Outcome

        return Outcome(True, 200, 1, None)

    monkeypatch.setattr("app.notify.send", capture)
    async with SessionLocal() as session:
        row = NotifyChannel(name="召回-按种类", scope="global", url=encrypt("https://hooks.example.com/recall-kinds"),
                            events=["ledger.recall"])
        session.add(row)
        await session.commit()
        channel_id = row.id
    try:
        wf = await S.publish()
        await answer(monkeypatch)
        S.script(monkeypatch, "本周销售额 [[m:gmv]]。")
        async with SessionLocal() as session:
            formal = await launch_run(session, workflow_id=wf, run_class="formal", version=1, input_payload=WEEK1,
                                      actor="analyst")
        assert (await S.wait(formal.id)).status == "succeeded"
        await publish_v2(wf)
        await run_recall(wf)
        await service.drain()
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(NotifyChannel, channel_id))
            await session.commit()
    mine = [m for m in sent if m["channel"] == "召回-按种类"]
    assert mine, sent
    answers = [v for m in mine for k, v in m["lines"] if k == "答案"]
    assert "问数回答 1 个、正式运行 1 个" in answers, answers
    for number in ("170.5", "208.5", "38.0"):
        assert all(number not in v for m in mine for _, v in m["lines"]), "消息里不放数"


async def test_recall_pushes_the_shared_channel_once_per_recall_run(monkeypatch):
    """开登录时一组答案有几个主人就开几条待办，但全局渠道（团队群）按召回运行只收一条（答案、待办都按这次召回汇总，链到
    待办箱）；订阅了 task.opened 的全局渠道也不再按每条待办各推一次。每个主人自己的个人渠道各收他那一条。"""
    from app.core.crypto import encrypt
    from app.db.models import NotifyChannel, User
    from app.notify import service

    sent: list[dict] = []

    async def capture(channel, message):
        sent.append({"channel": channel.name, **message})
        from app.notify.core import Outcome

        return Outcome(True, 200, 1, None)

    monkeypatch.setattr("app.notify.send", capture)
    wf = await S.publish()
    first = await answer(monkeypatch)
    second = await answer(monkeypatch)                       # 同一周：同一组，两个主人
    made: list[str] = []
    async with SessionLocal() as session:
        for name, run_id in (("analyst_a", first.id), ("analyst_b", second.id)):
            user = User(username=name, role="user")
            session.add(user)
            await session.flush()
            (await session.get(Run, run_id)).started_by = name
            channel = NotifyChannel(name=f"个人-{name}", scope="user", owner_id=user.id, format="generic",
                                    url=encrypt(f"https://hooks.example.com/{name}"), events=["ledger.recall"])
            session.add(channel)
            await session.flush()
            made.append(channel.id)
        for name, events_ in (("团队-召回", ["ledger.recall", "task.opened"]), ("团队-待办", ["task.opened"])):
            channel = NotifyChannel(name=name, scope="global", url=encrypt("https://hooks.example.com/team"),
                                    events=events_)
            session.add(channel)
            await session.flush()
            made.append(channel.id)
        await session.commit()
    monkeypatch.setenv("AGENTLAB_AUTH_MODE", "local")
    try:
        await publish_v2(wf)
        [done] = await run_recall(wf)
        await service.drain()
    finally:
        async with SessionLocal() as session:
            for channel_id in made:
                await session.delete(await session.get(NotifyChannel, channel_id))
            await session.commit()
    tasks = await recall_tasks(done.id)
    assert len(tasks) == 2, "每个主人一条待办"
    team = [m for m in sent if m["channel"] == "团队-召回"]
    assert [m["event"] for m in team] == ["ledger.recall"], [m["event"] for m in team]
    lines = dict(team[0]["lines"])
    assert lines["答案"] == "问数回答 2 个" and lines["待办"] == "2 条", lines
    assert [label for label, _ in team[0]["links"]] == ["去待办箱"] or team[0]["links"] == []
    assert [m["event"] for m in sent if m["channel"] == "团队-待办"] == ["task.opened"], "只订阅了新待办的团队渠道也只收一条"
    for name in ("analyst_a", "analyst_b"):
        mine = [m for m in sent if m["channel"] == f"个人-{name}"]
        assert [m["event"] for m in mine] == ["ledger.recall"] and dict(mine[0]["lines"])["答案"] == "问数回答 1 个", mine
    # 账本接口里的召回待办只列看的这个人手上的：第一个答案的主人看得到自己那条，另一个主人看不到
    from app.api.deps import Principal

    async with SessionLocal() as session:
        own = await recall.answer_marks(session, first.id, Principal(username="analyst_a", role="user", mode="local"))
        other = await recall.answer_marks(session, first.id, Principal(username="analyst_b", role="user", mode="local"))
    assert len(own["tasks"]) == 1 and own["tasks"][0]["reason"] == "caliber" and other["tasks"] == []


# --------------------------------------------------------------------------
# 目录事实被改、表结构变了
# --------------------------------------------------------------------------


async def _catalog(source_id: str, notes: dict) -> None:
    """探查一次表结构（查询快照才冻结得进目录）、写这张表的目录。"""
    from app.data import catalog
    from app.data.engine import engines
    from app.data.introspect import introspect
    from app.db.models import DataSource

    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        row.schema_cache = await introspect(row)
        await session.commit()
    await engines.invalidate(source_id)
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "orders")
        await catalog.write_entry(session, source_id, "orders", notes, if_version=entry.version if entry else 0,
                                  actor="tester")


def _item(value, status="confirmed"):
    return {"value": value, "source": "human", "status": status}


async def test_catalog_fact_change_marks_answers_and_notifies_only_on_new_errors(engine_up, monkeypatch):
    from app.data import catalog

    source_id = await S.set_source(engine_up)
    await _catalog(source_id, {"business_date": _item({"column": "paid_at"}),
                               "columns": {"amount": {"measure": _item("flow")}}})
    row = await answer(monkeypatch)
    unrelated = await S.explore(monkeypatch, S.ELSEWHERE, text="门店 [[v:Q1.r0.n]] 家。", origin="chat")
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "orders")
        # 改成存量：按新目录，对存量列求和、分组里没有业务日期是错误级问题（跨期加总存量）
        await catalog.save_human_edit(session, source_id, "orders",
                                      {**entry.notes, "columns": {"amount": {"measure": _item("stock")}}},
                                      if_version=entry.version, actor="tester")
    await recall.drain()
    [marked] = await events(row.id, EventType.LEDGER_BASIS_CHANGED)
    assert marked["kind"] == "catalog" and marked["table"] == "orders"
    assert marked["items"] == [{"path": "columns.amount.measure", "label": "列 amount 的度量类型", "change": "changed"}]
    assert marked["new_errors"] == 1 and marked["checks"][0]["code"] == "stock_summed"
    assert await events(unrelated.id, EventType.LEDGER_BASIS_CHANGED) == [], "没查过这张表的不标"
    [task] = await recall_tasks(row.id)
    assert task.correlation_key.startswith("catalog:")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        marks = (await c.get(f"/api/runs/{row.id}/ledger")).json()["recall"]
        quiet = (await c.get(f"/api/runs/{unrelated.id}/ledger")).json()["recall"]
    [basis] = marks["basis"]
    assert {k: basis[k] for k in ("kind", "items", "new_errors")} == {"kind": "catalog", "items": 1, "new_errors": 1}
    assert basis["at"] and marks["tasks"] == [{"id": task.id, "reason": "catalog"}] and marks["checked"] == []
    assert quiet == {"checked": [], "basis": [], "tasks": []}
    async with SessionLocal() as session:
        from app.api.deps import Principal

        payload = await recall.task_payload(session, task, Principal(username=None, role="admin", mode="off", via="off"))
    assert payload["reason"] == "catalog" and payload["basis"]["new_errors"] == 1
    assert [a["decision"] for a in payload["actions"]] == ["ack"]

    # 只改名称说明（不影响数字的项）：不标
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "orders")
        await catalog.save_human_edit(session, source_id, "orders", {**entry.notes, "label": _item("订单")},
                                      if_version=entry.version, actor="tester")
    await recall.drain()
    assert len(await events(row.id, EventType.LEDGER_BASIS_CHANGED)) == 1


async def test_catalog_edits_on_one_source_share_one_pass(engine_up, monkeypatch):
    """批量审阅一口气改了同一个数据源的几张表：只在后台做一遍（同一个数据源排着做，几张表合在一次里查），查询快照按数据源
    在库里筛，冻结的表结构快照（大库的能有几 MB）同一份只读一次。"""
    from collections import Counter

    from app.core import artifact_store
    from app.data import catalog

    source_id = await S.set_source(engine_up)
    await _catalog(source_id, {"business_date": _item({"column": "paid_at"}),
                               "columns": {"amount": {"measure": _item("flow")}}})
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "stores", {"valid_filter": _item("id > 0")}, if_version=0,
                                  actor="tester")
    week1 = await answer(monkeypatch)
    week2 = await answer(monkeypatch, "2026-09-08", "2026-09-15")
    shops = await S.explore(monkeypatch, S.ELSEWHERE, text="门店 [[v:Q1.r0.n]] 家。", origin="chat")
    scans: list[list[str]] = []
    loads: list[str] = []
    real_scan, real_load = recall._explore_snapshots, artifact_store.load

    async def scan(session, source, tables, **kw):
        scans.append(sorted(tables))
        return await real_scan(session, source, tables, **kw)

    def load(artifact_id):
        loads.append(artifact_id)
        return real_load(artifact_id)

    monkeypatch.setattr(recall, "_explore_snapshots", scan)
    monkeypatch.setattr(artifact_store, "load", load)
    async with SessionLocal() as session:
        orders = await catalog.read_entry(session, source_id, "orders")
        stores = await catalog.read_entry(session, source_id, "stores")
    rejected = {**orders.notes, "business_date": {**orders.notes["business_date"], "status": "rejected"}}
    # 批量审阅逐表写完逐表调审阅之后的钩子（catalog.after_review → recall.after_review），中间不让出事件循环
    assert recall.after_review(source_id, "orders", orders.notes, rejected, orders.version + 1) == 1
    assert recall.after_review(source_id, "stores", stores.notes,
                               {"valid_filter": {**stores.notes["valid_filter"], "status": "rejected"}},
                               stores.version + 1) == 1
    await recall.drain()
    assert scans == [["orders", "stores"]], "同一个数据源的几张表合成一遍"
    for run in (week1, week2):
        [marked] = await events(run.id, EventType.LEDGER_BASIS_CHANGED)
        assert marked["table"] == "orders" and marked["items"][0]["path"] == "business_date"
    [marked] = await events(shops.id, EventType.LEDGER_BASIS_CHANGED)
    assert marked["table"] == "stores" and marked["items"][0]["path"] == "valid_filter"
    schemas = set()
    for snapshot in {a for a in loads}:
        content = real_load(snapshot)
        if isinstance(content, dict) and content.get("schema_artifact"):
            schemas.add(content["schema_artifact"])
    assert schemas and all(Counter(loads)[sid] == 1 for sid in schemas), Counter(loads)


async def test_snapshot_scan_pages_newest_first_and_stops_when_full(engine_up, monkeypatch):
    """翻查询快照：只取这个数据源的、新的在前，每张表凑够 BASIS_MAX_RUNS 次运行就不再往下翻。"""
    from app.db.models import DataSource

    source_id = await S.set_source(engine_up)
    runs = [await answer(monkeypatch, f"2026-09-0{d}", "2026-09-08") for d in (1, 2, 3)]
    pages: list[int] = []
    monkeypatch.setattr(recall, "BASIS_MAX_RUNS", 2)
    monkeypatch.setattr(recall, "SNAPSHOT_PAGE", 1)
    async with SessionLocal() as session:
        source = await session.get(DataSource, source_id)
        real_execute = session.execute

        async def counted(stmt, *args, **kwargs):
            pages.append(1)
            return await real_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "execute", counted)
        found = await recall._explore_snapshots(session, source, ["ORDERS"])
        read = len(pages)
        other = DataSource(id="elsewhere", name="elsewhere")
        nothing = await recall._explore_snapshots(session, other, ["orders"])
    assert [r for r, _ in found["ORDERS"]] == [runs[2].id, runs[1].id], "新的在前、凑够两次运行就停"
    # 一页一行：第三页读到第三次运行时两次已经凑够，停下，不再翻第四页
    assert read == 3, f"凑够了不再往下翻（翻了 {read} 页）"
    assert nothing == {"orders": []}, "别的数据源的快照不读回来"


def test_fact_changes_only_counts_items_that_affect_numbers():
    before = {"valid_filter": _item("status <> 'open'"), "label": _item("订单"),
              "columns": {"amount": {"measure": _item("flow"), "label": _item("金额")}}}
    after = {"valid_filter": {**_item("status <> 'open'"), "status": "rejected"}, "label": _item("订单表"),
             "columns": {"amount": {"label": _item("金额")}}}
    changes = recall.fact_changes(before, after)
    assert [(c["path"], c["change"]) for c in changes] == [("valid_filter", "rejected"),
                                                          ("columns.amount.measure", "removed")]
    assert recall.fact_changes(after, after) == []


async def test_structure_change_marks_answers_and_probe_failure_notifies(engine_up, monkeypatch):
    import sqlite3

    from app.data import source_changes as sc
    from app.db.models import DataSource, WorkflowVersion

    wf = await S.publish()
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf))).scalar_one()
        row.published_by = "publisher"
        await session.commit()
    a1 = await answer(monkeypatch)
    # 列真的删掉了：口径的取数按这一版再算就算不出来
    db = sqlite3.connect(engine_up)
    db.executescript("ALTER TABLE orders DROP COLUMN amount;")
    db.close()
    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(DataSource.name == S.CURRENT["source"]))).scalar_one()
        change, _ = await sc.record(session, source, sc.NewChange(kind="column_removed", table="orders", column="amount",
                                                                  severity="action", key="v2"),
                                    sc.Detected(job_kind="introspect"))
        await session.commit()
        out = sc.change_out(change)
    assert recall.basis_kind(out) == "structure"
    assert await recall.process_change(out) == 1
    [marked] = await events(a1.id, EventType.LEDGER_BASIS_CHANGED)
    assert marked["kind"] == "structure" and marked["change"] == out["id"] and marked["column"] == "amount"
    async with SessionLocal() as session:
        [probe] = [r for r in (await session.execute(select(Run).where(Run.origin_kind == "recall"))).scalars()
                   if (r.origin or {}).get("change") == out["id"]]
    assert probe.origin["probe"] == 1 and probe.origin["workflow_id"] == wf and probe.origin["inputs_from"] == "answer"
    probe = await S.wait(probe.id)
    assert probe.status == "failed"
    await recall.drain()
    [task] = await recall_tasks(probe.id)
    assert task.title == "口径「周销售」v1 依据的表结构变了，试算没有算出来"
    # 同一条变化再处理一次：不再标、不再试算
    assert await recall.process_change(out) == 0
    async with SessionLocal() as session:
        probes = (await session.execute(select(Run).where(Run.origin_kind == "recall"))).scalars().all()
    assert len([p for p in probes if (p.origin or {}).get("change") == out["id"]]) == 1


# --------------------------------------------------------------------------
# 版本用量、预告接口、E4
# --------------------------------------------------------------------------


async def test_usage_preview_batches_and_stats_endpoints(client, monkeypatch):
    wf = await S.publish()
    await answer(monkeypatch)
    usage = (await client.get(f"/api/ledger/workflow-versions/{wf}/1")).json()
    assert usage["answers"] == 1 and usage["exports"] == 0 and usage["recall"] == {"groups": 0, "changed": 0,
                                                                                   "recalled_from": 0}
    await publish_v2(wf)
    preview = (await client.get(f"/api/workflows/{wf}/recall-preview", params={"version": 2})).json()
    assert preview["groups"] == 1 and preview["will_recompute"] == 1 and preview["over_limit"] == 0
    await run_recall(wf)
    view = (await client.get(f"/api/ledger/recall/{wf}/2")).json()
    assert view["counts"]["changed"] == 1 and view["groups"][0]["state"] == "changed"
    assert view["groups"][0]["changed_metrics"] == ["销售额"]
    again = await client.post(f"/api/ledger/recall/{wf}/2/continue")
    assert again.status_code == 409
    usage = (await client.get(f"/api/ledger/workflow-versions/{wf}/2")).json()
    assert usage["recall"]["groups"] == 1 and usage["recall"]["changed"] == 1
    old = (await client.get(f"/api/ledger/workflow-versions/{wf}/1")).json()
    assert old["recall"]["recalled_from"] == 1
    stats = (await client.get("/api/ledger/stats")).json()["e4"]["per_publish"]
    mine = next(p for p in stats["publishes"] if p["workflow_id"] == wf)
    assert mine == {"caliber": f"{wf}@2", "workflow_id": wf, "version": 2, "groups": 1, "settled": 1, "changed": 1,
                    "failed": 0, "changed_rate": 1.0}


async def test_publish_hook_queues_recall_when_automatic(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "scheduler", True)
    wf = await S.publish()
    await answer(monkeypatch)
    await S.publish(graph=S.caliber_graph(WITH_REFUNDED), version=2, workflow_id=wf, level="governed")
    async with SessionLocal() as session:
        from app.db.models import Workflow

        (await session.get(Workflow, wf)).published_version = 1
        await session.commit()
    res = await client.post(f"/api/workflows/{wf}/publish", json={"level": "published", "version": 2})
    assert res.status_code == 200 and res.json()["ok"], res.text
    await recall.drain()
    async with SessionLocal() as session:
        runs = await recall.recall_runs(session, wf, 2)
    assert len(runs) == 1 and runs[0].origin["batch"] == "auto"
    await S.wait(runs[0].id)
    await recall.drain()


async def test_recall_graph_is_deterministic_and_events_have_offline_labels(monkeypatch):
    """同样的组永远得到同一张召回图；两个新事件在可验证报告的离线核对页、封存后事件摘要里有说法（不带数）。"""
    import re
    from pathlib import Path

    from app.bundle.build import _summary

    wf = await S.publish()
    await answer(monkeypatch)
    await publish_v2(wf)
    async with SessionLocal() as session:
        plan = await recall.build_plan(session, wf, 2)
    [group] = plan.groups
    a, meta = recall.build_graph(group, plan.pairs[1])
    b, _ = recall.build_graph(group, plan.pairs[1])
    assert a.graph == b.graph and a.kind == "recall"
    assert meta == [{"k": 0, "card": "card", "metric": "gmv", "name": "销售额", "unit": "元", "decimals": 2,
                     "format": "thousands", "expression": "cell(nodes.fetch, 0, 'gmv')"}]
    card = next(n for n in a.graph["nodes"] if n["id"] == "recall")["data"]["config"]
    assert [m["id"] for m in card["metrics"]] == ["old_0", "new_0", "diff_0"] and card["on_missing"] == "null"

    script = (Path(__file__).resolve().parents[1] / "app" / "bundle" / "verifier.js").read_text(encoding="utf-8")
    block = re.search(r"const EVENT_LABEL = \{(.*?)\n  \}", script, re.S)
    labels = dict(re.findall(r"'([a-z_.]+)': '([^']+)'", block.group(1)))
    assert labels["ledger.recalled"] == "口径召回核对" and labels["ledger.basis_changed"] == "答案的依据有变"
    assert labels["ledger.matched"] == "比对已发布口径"
    assert _summary("ledger.recalled", {"role": "answer", "changed": False, "to_version": 2}) == "已按 v2 核对，不影响"
    assert _summary("ledger.basis_changed", {"kind": "structure", "items": [{"label": "x"}]}) == "依据的表结构有变（1 项）"


async def test_restart_resumes_the_auto_batch_and_settles_finished_runs(monkeypatch):
    """服务重启：计划从已有召回运行重算——自动那一批没发完的接着发，结束了还没收尾的补上收尾（不建任务表）。"""
    from app.db.base import utcnow
    from app.db.models import WorkflowVersion

    await ledger_settings(auto_issue=False, issue_daily_limit=100_000, recall_limit=2)
    wf = await S.publish()
    await answer(monkeypatch)
    await answer(monkeypatch, "2026-09-01", "2026-09-05")
    await publish_v2(wf)
    async with SessionLocal() as session:
        row = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf,
                                                                   WorkflowVersion.version == 2))).scalar_one()
        row.published_at = utcnow()
        await session.commit()
    # 「重启之前」：只发起了一组，收尾也没做（进程在这之后没了）
    monkeypatch.setattr(recall, "schedule_settle", lambda run_id: None)
    async with SessionLocal() as session:
        plan = await recall.build_plan(session, wf, 2)
    first = await recall._launch_group(plan, plan.todo()[0], batch="auto", actor=None)
    first = await S.wait(first.id)
    assert await recall_tasks(first.id) == [] and await events(first.id, EventType.LEDGER_RECALLED) == []
    monkeypatch.undo()
    out = await recall.resume()
    # 同一个测试库里别的用例的版本也会被接着做：只数这一份口径的
    assert out["launched"] >= 1 and out["settled"] >= 1
    async with SessionLocal() as session:
        runs = await recall.recall_runs(session, wf, 2)
    assert len(runs) == 2 and {r.origin["batch"] for r in runs} == {"auto"}
    for run in runs:
        await S.wait(run.id)
    await recall.drain()
    assert len(await recall_tasks(first.id)) == 1, "补上的收尾照样开了待办"
    async with SessionLocal() as session:
        view = await recall.batches(session, wf, 2)
    assert view["counts"]["changed"] == 2 and view["remaining"] == 0
    await recall.resume()
    async with SessionLocal() as session:
        assert len(await recall.recall_runs(session, wf, 2)) == 2, "再重启一次：都算过了，不再发起"
