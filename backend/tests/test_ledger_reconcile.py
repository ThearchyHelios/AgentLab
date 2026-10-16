"""口径对账：差多少（next-round 02 做法 6，app/ledger/reconcile.py、ledger/derive.reconcile_graph）。

真的跑探索运行（虚构店铺库、mock 模型写报告）、等后台比对做完，再点开对账：
- 动作：有相关口径的查询给「看差多少」；参数是按「同一列、同一运算符」猜的，要先核对（needs_check）；
- 不核对直接发起回 409（要核对参数）；核对过的参数发起：派生图是确定的、过 validate、出口契约 metrics_from 指向对账卡、
  不加报告；跑出来的这次、口径、差值、相对差都对，差值的出处链点得开；GET 账本列出对过的账；统计 E2 数到它；
- 认不出哪一列对哪个指标（口径的指标式不是取数的一格）：要人选，选了照记进 origin；
- 多行结果：被引用的行按维度列经合并查询对齐，每行一组对账指标；维度是遮罩列、合并键不唯一时照实拒绝。
数据是虚构店铺库。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import match as ledger_match
from app.ledger import reconcile as ledger_reconcile
from app.main import app
from tests import ledger_samples as S

BY_REGION = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
             "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}' GROUP BY region ORDER BY region")
BY_REGION_ALL = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE paid_at >= '2026-09-01' "
                 "AND paid_at < '2026-09-08' GROUP BY region ORDER BY region")
CHECKED = {"start": "2026-09-01", "end": "2026-09-08"}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    await S.fresh_source(path)
    yield path
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def ledger_of(client, run_id: str) -> dict:
    res = await client.get(f"/api/runs/{run_id}/ledger")
    assert res.status_code == 200, res.text
    return res.json()


async def card_of(run_id: str, node_id: str = "reconcile") -> dict:
    from app.api.evidence import _sealed, _sealed_cards

    sealed = await _sealed(run_id)
    [(_, card)] = [(a, c) for n, a, c in _sealed_cards(sealed) if n == node_id]
    return {m["id"]: m for m in card["metrics"]}


async def reconcile(client, run_id: str, body: dict) -> Run:
    res = await client.post(f"/api/ledger/answers/{run_id}/reconcile", json=body)
    assert res.status_code == 201, res.text
    return await S.wait(res.json()["run_id"])


async def test_reconcile_the_refunds_answer_against_the_weekly_caliber(client, monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    body = await ledger_of(client, row.id)
    [action] = [a for a in body["actions"] if a["kind"] == "reconcile"]
    assert action["workflow_id"] == wf and action["version"] == 1 and action["node_id"] == "fetch"
    assert action["needs_check"] is True and action["check_reason"] == ledger_reconcile.CHECK_REASON
    assert action["binding"] == [{"field": "end", "value": "2026-09-08"}, {"field": "start", "value": "2026-09-01"}]
    query = action["query"]

    # 参数是猜的：不核对就发起，照实拒绝
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json={"query": query, "workflow_id": wf,
                                                                              "version": 1})
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.CHECK_INPUTS
    assert res.json()["detail"] == ledger_reconcile.CONFIRM_FIRST

    done = await reconcile(client, row.id, {"query": query, "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                            "confirm": True})
    assert done.status == "succeeded", done.error
    assert done.origin_kind == "reconcile" and done.run_class == "exploratory" and done.workflow_id is None
    origin = done.origin
    assert origin["source_run"] == row.id and origin["query"] == query and origin["inputs_from"] == "checked"
    assert origin["pairs"] == [{"ours": "gmv", "metric": "gmv"}] and origin["caliber"] == f"{wf}@1"
    assert done.input == {"start": "2026-09-01", "end": "2026-09-08", "store": "all"}

    graph = done.graph
    ids = [n["id"] for n in graph["nodes"]]
    assert ids == ["derived_in", "ours_q1", "fetch", "card", "reconcile", "derived_out"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["ours_q1"]["data"]["config"]["args"]["sql"] == S.WITH_REFUNDS
    assert "{{ input.start }}" in nodes["fetch"]["data"]["config"]["args"]["sql"], "口径的取数保留模板，运行时按入口渲染"
    assert nodes["derived_out"]["data"]["config"]["contract"] == {"metrics_from": "reconcile"}
    assert not [n for n in graph["nodes"] if n["type"] in ("report", "llm", "agent")]
    assert not [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]

    metrics = await card_of(done.id)
    assert metrics["ours_0"]["value"] == 221.0 and metrics["cal_0"]["value"] == 170.5
    assert metrics["diff_0"]["value"] == 50.5 and metrics["rel_0"]["value"] == 0.2962
    assert metrics["rel_0"]["rendered"] == "29.62%" and metrics["diff_0"]["rendered"] == "50.50元"
    # 三个数各自点得开：差值的出处链追到两边的查询
    res = await client.get(f"/api/runs/{done.id}/evidence/metrics/reconcile/diff_0")
    assert res.status_code == 200, res.text
    inputs = [s for s in res.json()["chain"] if s["step"] == "input"]
    assert {i["node_id"] for i in inputs} == {"ours_q1", "card"}

    listed = (await ledger_of(client, row.id))["reconciled"]
    assert [r["run_id"] for r in listed] == [done.id] and listed[0]["inputs_from"] == "checked"

    # 统计 E2：对账运行数到了（它不进使用统计，E2 专门数它）
    stats = (await client.get("/api/ledger/stats")).json()["e2"]
    assert stats["launched"] >= 1 and stats["succeeded"] >= 1


async def test_the_derived_graph_is_deterministic():
    from app.ledger import derive

    wf = await S.publish()
    async with SessionLocal() as session:
        pinned = await derive.load_pinned(session, wf, 1, "card")
    plan = ledger_reconcile.Plan(pairs=[ledger_reconcile.Pair(ours="gmv", metric="gmv", index=0, name="销售额",
                                                              unit="元", decimals=2)])
    a = ledger_reconcile.build(pinned, sql=S.WITH_REFUNDS, tool=S.tool(), plan=plan, inputs=CHECKED, fetch="fetch")
    b = ledger_reconcile.build(pinned, sql=S.WITH_REFUNDS, tool=S.tool(), plan=plan, inputs=CHECKED, fetch="fetch")
    assert a.graph == b.graph and a.kind == "reconcile"
    assert a.origin["from"][0] == {"kind": "executed", "node_id": "ours_q1"} and a.origin["from"][1]["node_id"] == "card"


async def test_metrics_that_are_not_a_plain_cell_need_a_person_to_pick(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(expression="cell(nodes.fetch, 0, 'gmv') * 1"))
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    body = {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True}
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json=body)
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.NEEDS_PAIRS
    bad = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={**body, "pairs": [{"ours": "nope", "metric": "gmv"}]})
    assert bad.status_code == 422 and "没有列「nope」" in bad.json()["detail"]
    done = await reconcile(client, row.id, {**body, "pairs": [{"ours": "gmv", "metric": "gmv"}]})
    assert done.status == "succeeded", done.error
    assert done.origin["pairs_chosen"] is True
    assert (await card_of(done.id))["diff_0"]["value"] == 50.5


async def test_multi_row_answers_are_aligned_by_their_dimensions(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    done = await reconcile(client, row.id, {"query": action["query"], "workflow_id": wf, "version": 1,
                                            "inputs": CHECKED, "confirm": True})
    assert done.status == "succeeded", done.error
    assert done.origin["rows"] == [0, 1] and done.origin["pairs"] == [{"ours": "gmv", "caliber": "gmv"}]
    align = next(n for n in done.graph["nodes"] if n["id"] == "align")
    assert align["type"] == "merge" and align["data"]["config"]["inputs"] == {"o": "ours_q1", "c": "fetch"}
    metrics = await card_of(done.id)
    assert metrics["ours_0_0"]["name"] == "区域=east · gmv · 这次"
    assert (metrics["ours_0_0"]["value"], metrics["cal_0_0"]["value"], metrics["diff_0_0"]["value"]) == (163.0, 150.5, 12.5)
    assert (metrics["ours_1_0"]["value"], metrics["cal_1_0"]["value"], metrics["diff_1_0"]["value"]) == (58.0, 20.0, 38.0)


async def test_masked_dimensions_and_duplicate_keys_are_refused(client, monkeypatch, engine_up):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    duplicated = ("SELECT region AS 区域, amount AS gmv FROM orders WHERE paid_at >= '2026-09-01' "
                  "AND paid_at < '2026-09-08' ORDER BY id")
    row = await S.explore(monkeypatch, duplicated, text="第一单 [[v:Q1.r0.gmv]]，第二单 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                  "confirm": True})
    assert res.status_code == 409 and "有重复的行" in res.json()["detail"]

    await S.set_source(engine_up, options={"mask_columns": ["region"]})
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                  "confirm": True})
    assert res.status_code == 409 and "设置了遮罩" in res.json()["detail"]


def test_align_sql_keeps_the_referenced_rows_in_order():
    pairs = [ledger_reconcile.Pair(ours="gmv", column="销售额")]
    sql = ledger_reconcile.align_sql([("区域", "区域")], pairs, [("west",), ("o'east",)])
    assert sql == ('SELECT o."区域" AS "区域", o."gmv" AS "ours_0", c."销售额" AS "cal_0" FROM o LEFT JOIN c ON '
                   'o."区域" = c."区域" WHERE (o."区域" = \'west\') OR (o."区域" = \'o\'\'east\') '
                   'ORDER BY CASE WHEN (o."区域" = \'west\') THEN 0 WHEN (o."区域" = \'o\'\'east\') THEN 1 END')


async def test_users_reconcile_their_own_answers_read_only_and_masked(client, monkeypatch):
    """拍板 L5：对账（只读的探索运行）使用者都可以，带上问数据同一套 MASK_RESULTS / READ_ONLY；别人的答案读不到就不给对。"""
    from app.api.deps import Principal
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    async with SessionLocal() as session:
        (await session.get(Run, row.id)).started_by = "carol"
        await session.commit()
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    req = ledger_reconcile.Request(query=action["query"], workflow_id=wf, version=1, inputs=CHECKED, confirm=True)
    dave = Principal(username="dave", role="user", verified=True, mode="local")
    with pytest.raises(ledger_reconcile.ReconcileRefused) as e:
        await ledger_reconcile.reconcile(row.id, req, principal=dave)
    assert e.value.status_code == 403
    carol = Principal(username="carol", role="user", verified=True, mode="local")
    run = await ledger_reconcile.reconcile(row.id, req, principal=carol)
    assert run.started_by == "carol"
    assert run.options[MASK_RESULTS_OPTION] is True and run.options[READ_ONLY_OPTION] is True
    done = await S.wait(run.id)
    assert done.status == "succeeded", done.error


async def test_users_read_their_reconcile_runs_only_through_the_evidence(client, monkeypatch):
    """使用者发起的对账运行带 MASK_RESULTS、记在他名下，但派生图里有口径取数节点的 SQL 模板（口径的 SQL 可能写着遮罩值，
    所以问数据里使用者不看口径、不能 use_caliber）：运行的原始记录、事件、运行时的图（/api/runs/{id}、/events、/graph）只给
    他自己的问数据运行，对账运行经证据接口读（那里按遮罩渲染）。"""
    from app.tools.datasource import MASK_RESULTS_OPTION
    from tests.fakes import auth_env

    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS, origin="chat")
    # 当作开登录后使用者 carol 问的：问数据运行在工具边界遮
    async with SessionLocal() as session:
        source = await session.get(Run, row.id)
        source.started_by, source.options = "carol", {MASK_RESULTS_OPTION: True}
        await session.commit()
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    await auth_env.reset()
    try:
        admin = await auth_env.turn_on_local()
        await auth_env.make_user("carol", "user")
        assert (await admin.put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
        carol = await auth_env.login("carol")
        res = await carol.post(f"/api/ledger/answers/{row.id}/reconcile", json={
            "query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True})
        assert res.status_code == 201, res.text
        done = await S.wait(res.json()["run_id"])
        assert done.status == "succeeded" and done.started_by == "carol" and done.options[MASK_RESULTS_OPTION] is True
        for rest in ("", "/events", "/graph"):
            assert (await carol.get(f"/api/runs/{done.id}{rest}")).status_code == 403, rest
            assert (await carol.get(f"/api/runs/{row.id}{rest}")).status_code == 200, f"自己的问数据运行照旧：{rest}"
        assert (await carol.get(f"/api/runs/{done.id}/evidence")).status_code == 200
    finally:
        await auth_env.reset()


async def test_the_reconcile_view_gives_the_panel_what_it_draws(client, monkeypatch):
    """界面层不放逻辑（W4-c 账本前端）：「看差多少」那一项带上口径的入口字段（核对参数的表单照它画：哪几项、必填、有没有
    缺省值）；认不出列怎么配时 409 另带可选的列和口径卡上的指标（选对照的那一步照它画）；对过的账列出对账卡和每一组的
    这次 / 口径 / 差值 / 相对差是哪几个指标（三个数各自点得开，界面不按命名规则去拼指标 id）。"""
    wf = await S.publish(graph=S.caliber_graph(expression="cell(nodes.fetch, 0, 'gmv') * 1"))
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    assert action["fields"] == [{"name": "start", "required": True, "has_default": False},
                                {"name": "end", "required": True, "has_default": False},
                                {"name": "store", "required": False, "has_default": True}]
    body = {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED, "confirm": True}
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json=body)
    assert res.status_code == 409 and res.json()["code"] == ledger_reconcile.NEEDS_PAIRS
    assert res.json()["options"] == {"columns": ["gmv"], "metrics": [{"id": "gmv", "name": "销售额", "unit": "元"}]}
    done = await reconcile(client, row.id, {**body, "pairs": [{"ours": "gmv", "metric": "gmv"}]})
    [listed] = (await ledger_of(client, row.id))["reconciled"]
    assert listed["run_id"] == done.id and listed["card"] == "reconcile"
    assert listed["groups"] == [{"label": "销售额", "ours": "ours_0", "caliber": "cal_0", "diff": "diff_0", "rel": "rel_0"}]


async def test_multi_row_reconcile_groups_carry_the_row_labels(client, monkeypatch):
    wf = await S.publish(graph=S.caliber_graph(BY_REGION))
    row = await S.explore(monkeypatch, BY_REGION_ALL, text="东区 [[v:Q1.r0.gmv]]，西区 [[v:Q1.r1.gmv]]。")
    [action] = [a for a in (await ledger_of(client, row.id))["actions"] if a["kind"] == "reconcile"]
    await reconcile(client, row.id, {"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                     "confirm": True})
    [listed] = (await ledger_of(client, row.id))["reconciled"]
    assert [g["label"] for g in listed["groups"]] == ["区域=east · gmv", "区域=west · gmv"]
    assert listed["groups"][1] == {"label": "区域=west · gmv", "ours": "ours_1_0", "caliber": "cal_1_0", "diff": "diff_1_0",
                                   "rel": "rel_1_0"}
