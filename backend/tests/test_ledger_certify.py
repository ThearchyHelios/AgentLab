"""沉淀：可沉淀的问题（认证队列）和「固化成口径草稿」（next-round 02 做法 8，app/ledger/certify.py）。

真的跑三次问数据（两段对话、同一个 SQL 形状、日期不同，虚构店铺库），等后台比对做完：
- 队列：确定性重复进队（3 次运行、2 段对话），变了的字面量是参数候选、不变的是常量，形状里不带取值；同一个问题问了 3 次
  进问题聚类；导出过两次的、做成过应用的答案进「被反复用的答案」；点「不是口径」写审计后不再进队；
- 固化：报告引用的查询变成固定 SQL 的取数节点，跨轮变化的日期成为入口参数（缺省值取代表运行的），常量 'paid' 照写；单行
  结果的数值列成口径卡指标；出口契约 report_from + metrics_from；过 validate_graph 和受管级别的发布门禁；origin.sources 记
  三个来源问题；
- 按受管级别发布之后：来源运行在后台按它补比（trigger=certified），查询变成等价已证明、出了队；再问一次同样的形状，标
  「等价已证明」；
- 统计 E3：认证队列的转化（固化、发布、不是口径）有数了。
数据是虚构店铺库。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Conversation, ConversationTurn, Run, Workflow
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import certify
from app.ledger import match as ledger_match
from app.ledger import store
from app.main import app
from tests import ledger_samples as S

QUESTION = "九月第一周卖了多少"
WEEKS = [("2026-09-01", "2026-09-08"), ("2026-09-02", "2026-09-09"), ("2026-09-03", "2026-09-10")]


def weekly(start: str, end: str) -> str:
    return (f"SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '{start}' "
            f"AND paid_at < '{end}'")


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def conversation() -> str:
    async with SessionLocal() as session:
        conv = Conversation(title="问销售", kind="chat")
        session.add(conv)
        await session.commit()
        return conv.id


async def ask(monkeypatch, conversation_id: str, sql: str, question: str = QUESTION) -> Run:
    """问数据的一轮：建轮次、跑一次问数据运行（来源记着对话和轮次）、挂回轮次、等比对做完。"""
    async with SessionLocal() as session:
        turn = ConversationTurn(conversation_id=conversation_id, question=question)
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    S.script(monkeypatch, "这一周销售额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=S.explore_graph(sql), input_payload={"question": question}, origin_kind="chat",
                                  origin={"conversation_id": conversation_id, "turn_id": turn_id})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        (await session.get(ConversationTurn, turn_id)).run_id = row.id
        await session.commit()
    await ledger_match.drain()
    return row


async def three_asks(monkeypatch) -> list[Run]:
    a, b = await conversation(), await conversation()
    return [await ask(monkeypatch, conv, weekly(*week)) for conv, week in zip((a, a, b), WEEKS)]


def shape_items(body: dict) -> list[dict]:
    return [i for i in body["items"] if i["kind"] == "shape" and i["source"] == S.CURRENT["source"]]


# --------------------------------------------------------------------------
# 队列
# --------------------------------------------------------------------------


def test_varying_literals_are_parameters_and_the_template_puts_them_back():
    shape = certify.Shape("shop", "SELECT SUM(amount) AS gmv FROM orders WHERE status = ? AND paid_at >= ? AND x = ?",
                          ("'paid'", "'2026-09-01'", "3"))
    assert certify.varying([shape.literals, ("'paid'", "'2026-09-02'", "4")]) == [1, 2]
    template, fields = certify.template_of(shape, [1, 2], "sqlite", start=3)
    assert template == ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '{{ input.p3 }}' "
                        "AND x = {{ input.p4 }}")
    assert fields == [{"name": "p3", "type": "string", "required": True, "default": "2026-09-01"},
                      {"name": "p4", "type": "number", "required": True, "default": 3}]


async def test_repeated_shapes_questions_and_reused_answers_queue_up(client, monkeypatch):
    runs = await three_asks(monkeypatch)
    body = (await client.get("/api/ledger/certify-queue")).json()
    [item] = shape_items(body)
    assert item["runs"] == 3 and item["conversations"] == 2 and item["question"] == QUESTION
    assert item["params"] == [{"index": 1, "distinct": 3}, {"index": 2, "distinct": 3}] and item["constants"] == 1
    assert "2026-09" not in item["shape"] and "'paid'" not in item["shape"] and "?" in item["shape"]
    assert {t["run_id"] for t in item["turns"]} == {r.id for r in runs}
    assert all(t["turn_id"] and t["conversation_id"] for t in item["turns"])
    assert item["representative"]["run_id"] in {r.id for r in runs} and len(item["peers"]) == 2
    assert item["related"] == [] and item["frozen_into"] == []
    clusters = [i for i in body["items"] if i["kind"] == "cluster" and i["question"] == QUESTION]
    assert clusters and clusters[0]["count"] >= 3

    # 被反复用的答案：导出过两次的、做成过应用的
    from app.db.models_blueprints import Blueprint

    async with SessionLocal() as session:
        from app.auth.audit import record

        for _ in range(2):
            await record(session, "report.exported", "run", runs[0].id, data={"format": "html"})
        session.add(Blueprint(name="周销售应用", source={"kind": "run", "id": runs[1].id}, spec={}))
        await session.commit()
    body = (await client.get("/api/ledger/certify-queue")).json()
    reused = {i["representative"]["run_id"]: i for i in body["items"] if i["kind"] == "reused"}
    assert reused[runs[0].id]["uses"]["exports"] == 2 and reused[runs[1].id]["uses"]["app"] is True
    assert runs[2].id not in reused

    # 「不是口径」：记审计，之后不再进队
    res = await client.post("/api/ledger/certify-queue/dismiss", json={"item_id": item["id"], "kind": "shape"})
    assert res.status_code == 200
    assert shape_items((await client.get("/api/ledger/certify-queue")).json()) == []
    stats = (await client.get("/api/ledger/stats")).json()["e3"]["certify"]
    assert stats["available"] is True and stats["dismissed"] >= 1


async def test_two_runs_or_one_conversation_are_not_enough(client, monkeypatch):
    conv = await conversation()
    for week in WEEKS:
        await ask(monkeypatch, conv, weekly(*week))
    assert shape_items((await client.get("/api/ledger/certify-queue")).json()) == [], "只来自一段对话"


# --------------------------------------------------------------------------
# 固化、发布、再问
# --------------------------------------------------------------------------


async def test_freezing_makes_a_fixed_sql_draft_that_passes_the_governed_gate(client, monkeypatch):
    from app.engine.governance import publish_issues

    runs = await three_asks(monkeypatch)
    [item] = shape_items((await client.get("/api/ledger/certify-queue")).json())
    rep = item["representative"]["run_id"]
    res = await client.post("/api/copilot/from-run", json={"run_id": rep, "freeze": True, "peers": item["peers"]})
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["frozen"] is True and made["params"] == ["p1", "p2"] and made["metrics"] == ["m1"]
    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
    assert wf.status == "draft" and "certified" in wf.tags
    nodes = {n["id"]: n for n in wf.graph["nodes"]}
    assert set(nodes) == {"start", "fetch", "card", "write", "out"}
    rep_run = next(r for r in runs if r.id == rep)
    start, end = WEEKS[[r.id for r in runs].index(rep)]
    assert nodes["start"]["data"]["config"]["fields"] == [
        {"name": "p1", "type": "string", "required": True, "default": start},
        {"name": "p2", "type": "string", "required": True, "default": end}]
    sql = nodes["fetch"]["data"]["config"]["args"]["sql"]
    assert "'{{ input.p1 }}'" in sql and "'{{ input.p2 }}'" in sql and "'paid'" in sql
    assert nodes["fetch"]["data"]["config"]["tool"] == S.tool()
    assert nodes["card"]["data"]["config"]["metrics"] == [
        {"id": "m1", "name": "gmv", "expression": "cell(nodes.fetch, 0, 'gmv')"}]
    assert nodes["out"]["data"]["config"]["contract"] == {"report_from": "write", "metrics_from": "card",
                                                          "required": ["m1"]}
    assert {k: nodes["write"]["data"]["config"].get(k) for k in ("numbers", "on_violation", "claims")} == {
        "numbers": "strict", "on_violation": "fail", "claims": "require_citation"}
    # 有入口参数：不写代表那一轮的问题原文（「九月第一周」参数化之后就不对了），写运行时渲染的参数
    instructions = nodes["write"]["data"]["config"]["instructions"]
    assert QUESTION not in instructions and "2026-09" not in instructions, instructions
    assert "{{ input.p1 }}" in instructions and "{{ input.p2 }}" in instructions
    assert nodes["card"]["data"]["config"]["caliber"] == QUESTION, "没传名字：口径卡照旧用代表问题"
    spec = GraphSpec.model_validate(wf.graph)
    assert not [i for i in validate_graph(spec).issues if i.level == "error"]
    assert not [i for i in publish_issues(spec, level="governed") if i.level == "error"], \
        [i.message for i in publish_issues(spec, level="governed") if i.level == "error"]
    assert [s["run_id"] for s in wf.origin["sources"]][0] == rep and len(wf.origin["sources"]) == 3
    assert all(s["question"] == QUESTION for s in wf.origin["sources"])
    assert wf.origin["run_id"] == rep_run.id and wf.origin["freeze"] is True

    # 队列上标着已经固化过：同一个工作流只列一次（三个来源运行都指着它）
    body = (await client.get("/api/ledger/certify-queue")).json()
    [again] = shape_items(body)
    assert [w["workflow_id"] for w in again["frozen_into"]] == [wf.id]
    [cluster] = [i for i in body["items"] if i["kind"] == "cluster" and i["question"] == QUESTION]
    assert [w["workflow_id"] for w in cluster["frozen_into"]] == [wf.id], cluster["frozen_into"]

    # 按受管级别发布：来源运行在后台补比，变成等价已证明、出了队
    res = await client.post(f"/api/workflows/{wf.id}/publish", json={"level": "governed"})
    assert res.status_code == 200, res.text
    await ledger_match.drain()
    for run in runs:
        recorded = await store.latest(run.id)
        assert recorded.trigger == "certified" and recorded.retro is True
        [query] = recorded.content["queries"]
        assert query["status"] == "proven" and query["candidates"][0]["caliber"] == f"{wf.id}@1"
    body = (await client.get(f"/api/runs/{runs[0].id}/ledger")).json()
    assert body["retro_note"] == "按之后发布的口径回看"
    assert shape_items((await client.get("/api/ledger/certify-queue")).json()) == []

    # 再问一次同样的形状：标等价已证明
    later = await ask(monkeypatch, await conversation(), weekly("2026-09-05", "2026-09-12"))
    [label] = (await store.latest(later.id)).content["reports"]["write"]["labels"].values()
    assert label["kind"] == "caliber" and label["how"] == "independent" and label["caliber"] == f"{wf.id}@1"
    assert label["binding"] == {"p1": "2026-09-05", "p2": "2026-09-12"}

    stats = (await client.get("/api/ledger/stats")).json()["e3"]["certify"]
    assert stats["frozen"] >= 1 and stats["published"] >= 1


async def test_freezing_uses_the_given_name_and_catalog_labels_and_no_dates_without_parameters(client, monkeypatch):
    """传了名字：口径卡用它（下一轮验收：以前用代表问题的原文）。指标名能从数据目录取到中文名的用中文名，取不到照旧用
    列名。没有入口参数（字面量都没变过）时报告指令照旧写问题原文：那时问题里的日期就是口径的常量。"""
    from app.data import catalog
    from app.db.models import DataSource
    from sqlalchemy import select

    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(DataSource.name == S.CURRENT["source"]))).scalar_one()
        await catalog.write_entry(session, source.id, "orders", {"columns": {
            "amount": {"label": catalog.make_item("金额", "human")}}}, if_version=0, actor="测试员")
    sql = "SELECT SUM(amount) AS amount, COUNT(*) AS n FROM orders WHERE status = 'paid' AND paid_at >= '2026-09-01'"
    S.script(monkeypatch, "金额 [[v:Q1.r0.amount]]，[[v:Q1.r0.n]] 单。")
    conv = await conversation()
    async with SessionLocal() as session:
        turn = ConversationTurn(conversation_id=conv, question=QUESTION)
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    run = await run_manager.start(graph=S.explore_graph(sql), input_payload={"question": QUESTION}, origin_kind="chat",
                                  origin={"conversation_id": conv, "turn_id": turn_id})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        (await session.get(ConversationTurn, turn_id)).run_id = row.id
        await session.commit()
    await ledger_match.drain()
    res = await client.post("/api/copilot/from-run", json={"run_id": row.id, "freeze": True, "name": "九月销售额口径"})
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["params"] == [] and made["name"] == "九月销售额口径"
    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
    nodes = {n["id"]: n for n in wf.graph["nodes"]}
    card = nodes["card"]["data"]["config"]
    assert card["caliber"] == "九月销售额口径"
    assert [(m["name"], m["expression"]) for m in card["metrics"]] == [
        ("金额", "cell(nodes.fetch, 0, 'amount')"), ("n", "cell(nodes.fetch, 0, 'n')")]
    assert nodes["write"]["data"]["config"]["instructions"] == f"回答：{QUESTION}"


async def test_freezing_needs_a_matched_successful_run(client, monkeypatch):
    res = await client.post("/api/copilot/from-run", json={"run_id": "nope", "freeze": True})
    assert res.status_code == 404
    row = await S.explore(monkeypatch, S.ELSEWHERE, text="一共几家店，没有引用。")
    res = await client.post("/api/copilot/from-run", json={"run_id": row.id, "freeze": True})
    assert res.status_code == 409 and res.json()["detail"] == certify.NO_QUERIES


# --------------------------------------------------------------------------
# 确定性重复按规范形分组：条件的先后不算，条件真不同的不算一组
# --------------------------------------------------------------------------

#: 同一个形状的三种写法：条件换序、AND 两边换位、比较两边换位
REORDERED = [
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '{s}' AND paid_at < '{e}'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at < '{e}' AND paid_at >= '{s}' AND status = 'paid'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE '{s}' <= paid_at AND ('paid' = status AND paid_at < '{e}')",
]


def test_conditions_in_another_order_are_the_same_shape_and_their_literals_line_up():
    from app.data.sql_semantics import sql_shape

    shapes = [sql_shape(sql.format(s=s, e=e), "sqlite") for sql, (s, e) in zip(REORDERED, WEEKS)]
    assert all(sh is not None for sh in shapes)
    assert len({sh.key for sh in shapes}) == 1
    # 按规范形里的先后对齐：同一个位置是同一个条件里的那个字面量——变了的是两个日期，'paid' 在每一条里都落在同一个位置
    assert len(certify.varying([sh.literals for sh in shapes])) == 2
    assert len({sh.literals.index("'paid'") for sh in shapes}) == 1
    starts = {sh.literals.index(f"'{s}'") for sh, (s, _) in zip(shapes, WEEKS)}
    assert len(starts) == 1, "起始日期在每一条里都落在同一个位置"


@pytest.mark.parametrize("other", [
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND created_at >= '2026-09-01' AND paid_at < '2026-09-08'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' OR paid_at >= '2026-09-01' AND paid_at < '2026-09-08'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '2026-09-01'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at > '2026-09-01' AND paid_at < '2026-09-08'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE status = 1 AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'",
    "SELECT SUM(amount) AS gmv FROM orders WHERE status IN ('paid', 'open') AND paid_at >= '2026-09-01' "
    "AND paid_at < '2026-09-08'",
    "SELECT COUNT(*) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'",
], ids=["other-column", "or", "missing-condition", "other-operator", "number-not-string", "in-list", "other-aggregate"])
def test_really_different_conditions_are_different_shapes(other):
    from app.data.sql_semantics import sql_shape

    base = sql_shape(REORDERED[0].format(s="2026-09-01", e="2026-09-08"), "sqlite")
    found = sql_shape(other, "sqlite")
    assert base is not None and found is not None and found.key != base.key


async def test_queue_groups_reordered_conditions_and_freezing_aligns_their_parameters(client, monkeypatch):
    a, b = await conversation(), await conversation()
    runs = [await ask(monkeypatch, conv, sql.format(s=s, e=e))
            for conv, sql, (s, e) in zip((a, a, b), REORDERED, WEEKS)]
    # 条件真不同的（多筛了一个地区）不进这一组
    other = await ask(monkeypatch, b, "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND region = 'east' "
                                      "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")
    [item] = shape_items((await client.get("/api/ledger/certify-queue")).json())
    assert item["runs"] == 3 and {t["run_id"] for t in item["turns"]} == {r.id for r in runs}
    assert other.id not in {t["run_id"] for t in item["turns"]}
    assert len(item["params"]) == 2 and all(p["distinct"] == 3 for p in item["params"]) and item["constants"] == 1

    rep = item["representative"]["run_id"]
    res = await client.post("/api/copilot/from-run", json={"run_id": rep, "freeze": True, "peers": item["peers"]})
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["params"] == ["p1", "p2"], "条件换了序的几轮照样认得出两个日期是参数"
    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
    sql = {n["id"]: n for n in wf.graph["nodes"]}["fetch"]["data"]["config"]["args"]["sql"]
    assert "'paid'" in sql and "'{{ input.p1 }}'" in sql and "'{{ input.p2 }}'" in sql


async def test_a_not_a_caliber_recorded_by_the_old_text_id_still_counts(client, monkeypatch):
    """以前「不是口径」按规范文本算的 id 记：分组改按规范形之后，组里任何一条的规范文本对得上就照样不进队。"""
    await three_asks(monkeypatch)
    [item] = shape_items((await client.get("/api/ledger/certify-queue")).json())
    old = certify.item_id("shape", f"{S.CURRENT['source']}\n{item['shape']}")
    assert old != item["id"]
    res = await client.post("/api/ledger/certify-queue/dismiss", json={"item_id": old, "kind": "shape"})
    assert res.status_code == 200
    assert shape_items((await client.get("/api/ledger/certify-queue")).json()) == []


# --------------------------------------------------------------------------
# 问题聚类的 id 由成员定；「不是口径」按成员记
# --------------------------------------------------------------------------


async def test_cluster_ids_come_from_the_members_and_do_not_change_between_reads():
    from datetime import datetime, timezone

    from app.chat.questions import Question, QuestionCluster, cluster_questions

    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # 同一时刻问的三句（时间相同，库里读出来的先后不定）：两次读出来先后不同，种子就不同，类的 id 仍然一样
    texts = ["仓库乙本月出库多少件", "仓库乙本月出库多少件？", "仓库乙本月出库了多少件"]
    questions = [Question(text=t, at=at, turn_id=f"t-{i}") for i, t in enumerate(texts)]
    first = await cluster_questions(questions)
    again = await cluster_questions(list(reversed(questions)))
    assert len(first) == 1 and len(again) == 1 and first[0].members[0].text != again[0].members[0].text
    assert [c.id for c in first] == [c.id for c in again]
    assert first[0].keys == ["turn:t-0", "turn:t-1", "turn:t-2"]
    assert QuestionCluster(members=list(reversed(questions))).id == first[0].id
    # 成员不同，id 就不同
    assert QuestionCluster(members=questions[:2]).id != first[0].id


async def test_not_a_caliber_on_a_cluster_survives_a_new_member_and_old_records_still_count(client, monkeypatch):
    import uuid

    from app.auth.audit import record

    text = f"仓库甲本周入库了多少件（{uuid.uuid4().hex[:6]}）"
    a, b = await conversation(), await conversation()
    asked = {(await ask(monkeypatch, conv, weekly(*WEEKS[0]), question=text)).id for conv in (a, a, b)}

    async def mine() -> list[dict]:
        body = (await client.get("/api/ledger/certify-queue")).json()
        return [i for i in body["items"] if i["kind"] == "cluster" and asked & {t["run_id"] for t in i["turns"]}]

    [item] = await mine()
    [again] = await mine()
    assert item["id"] == again["id"], "同一批问题两次读取，类的 id 一样"
    # 读不懂的老记录照实忽略：不报错、不把这一类藏起来
    async with SessionLocal() as session:
        await record(session, certify.DISMISS_ACTION, certify.SUBJECT_KIND, "cluster:0000000000000000",
                     data={"kind": "cluster", "members": "看不懂"})
        await session.commit()
    assert [i["id"] for i in await mine()] == [item["id"]]

    res = await client.post("/api/ledger/certify-queue/dismiss", json={"item_id": item["id"], "kind": "cluster"})
    assert res.status_code == 200
    assert await mine() == []
    # 再换个说法问一句：成员多了一个、种子换成了新的这句，类的 id 变了；过半的成员是点「不是口径」时记下的，照样不进队
    newer = await ask(monkeypatch, b, weekly(*WEEKS[1]), question=text + "？")
    asked.add(newer.id)
    assert await mine() == []


async def test_a_not_a_caliber_recorded_by_the_old_seed_text_id_still_counts(client, monkeypatch):
    import uuid

    from app.auth.audit import record

    text = f"仓库丙上周退货多少件（{uuid.uuid4().hex[:6]}）"
    a, b = await conversation(), await conversation()
    for conv in (a, a, b):
        await ask(monkeypatch, conv, weekly(*WEEKS[0]), question=text)
    body = (await client.get("/api/ledger/certify-queue")).json()
    assert [i for i in body["items"] if i["kind"] == "cluster" and i["question"] == text]
    async with SessionLocal() as session:
        await record(session, certify.DISMISS_ACTION, certify.SUBJECT_KIND, certify.item_id("cluster", text),
                     data={"kind": "cluster"})
        await session.commit()
    body = (await client.get("/api/ledger/certify-queue")).json()
    assert not [i for i in body["items"] if i["kind"] == "cluster" and i["question"] == text]
