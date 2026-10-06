"""证据接口的核查状态（期 4 I2a）：口径卡指标那一步带双盲复算的结果，句子带反方审稿的判断。

- 指标步骤 dual_check：{status, node_id, choice?, blind_rendered?, reason?}。status 是一致（agreed）、不一致（disagreed，
  带审核人的选择，没选的不带）、无法复算（unchecked，带原因）、跳过（skipped）。跳过的原因按这个指标自己说：关闭了
  （disabled）；不在节点配置的「复算的指标」里、或值不是数（not_targeted）；这个指标没有来自查询的输入（no_inputs）——
  节点直通时产出里的 no_inputs 说的是整张卡，卡上确有来自查询的指标、只是没被选上的不能也说成没有。读双盲复算节点封存的
  产出和它的台账条目，按台账里记的指标集工件认它复算的是口径卡的哪一次结果；直通的没有台账条目，按画布上它复算的卡、
  和它跑完之前那张卡最后一次的结果认
- 句子 unit.challenge：{verdict, node_id, artifact, on_refuted, report_label, counts, unreviewed?}。读反方审稿节点的产出
  和审稿记录，只认审的就是这一份报告文档的；记录没存上时按画布上审的报告、和它跑完之前那份报告最后一次自查的文档认，
  判断取产出里列的那几句。counts 是整份报告的审稿计数（节点产出里的那一份），证据面板借审批卡的展示画这一句时，概况那
  一行不能凭这一句拼。节点设为「报错并停止运行」、句子被推翻、运行因此失败时，按审完那一刻的日志认
- 没有双盲复算、反方审稿的运行，答复里没有这两个键（老运行和以前一字不差）

双盲复算、反方审稿各真跑一遍图（虚构的 SQLite 库，模型是 MockChatModel 剧本）；认「哪一次」的边界情形用手写的封存事件。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.api import evidence
from app.core import artifact_store
from app.core.artifact_store import put_json
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Approval, DataSource, Run, Setting
from app.engine import spend, verify_agent
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import challenge as challenge_mod
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel
from tests.fixtures.dual_check_shop import DEFINITION, RIGHT, WITH_REFUNDS, Blind, add_source, build
from tests.fixtures.sources import drop_source


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()

    async def clear() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.key.in_(
                ["verify", "judge", spend.SPEND_KEY, JUDGE_SPEND_KEY])))
            await session.commit()

    await clear()
    yield
    await clear()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("succeeded", "failed", "interrupted")) -> Run:
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 停在 {row.status}：{row.error}")


async def start(spec: dict, **kwargs) -> Run:
    return await wait((await run_manager.start(graph=spec, input_payload={}, **kwargs)).id)


async def report_of(client, run_id: str) -> dict:
    body = (await client.get(f"/api/runs/{run_id}/evidence")).json()
    [report] = body["reports"]
    return artifact_store.load(report["doc_artifact"])


async def open_segment(client, run_id: str, seg_id: str) -> dict:
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg_id}")
    assert res.status_code == 200, res.text
    return res.json()


async def segment(client, run_id: str, text: str) -> dict:
    doc = await report_of(client, run_id)
    return await open_segment(client, run_id, next(s for s in iter_segments(doc) if s["text"] == text)["id"])


async def sentence(client, run_id: str, text: str | None = None, *, kind: str | None = None) -> dict:
    """点开一整句（句末徽标）时证据面板请求的那个片段：这一句里第一个不是结构性的片段。按原文认句子，或者按句子的
    种类认（标题）。"""
    doc = await report_of(client, run_id)
    md = doc["markdown"]
    unit = next(u for _, u in iter_units(doc)
                if (kind is not None and u.get("kind") == kind)
                or (text is not None and md[u["span"][0]:u["span"][1]].strip() == text))
    first = next(s for s in unit["segments"] if s["kind"] != "structural")
    return await open_segment(client, run_id, first["id"])


# --------------------------------------------------------------------------
# 双盲复算：真跑
# --------------------------------------------------------------------------

GMV = {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
       "expression": "cell(nodes.fetch, 0, 'gmv')"}
#: 不在复算范围里的指标：没有来自查询的输入
DAYS = {"id": "days", "name": "统计天数", "unit": "天", "format": "thousands", "expression": "7"}
WRITER = "本周销售额 [[m:gmv]]，统计 [[m:days]]。"


@pytest.fixture
async def shop(tmp_path):
    name, source_id = await add_source(build(tmp_path / "shop.db"))
    yield name
    await drop_source(source_id)


def dual_graph(source: str, *, dual: bool = True, **config) -> dict:
    """取数 → 口径卡 →（双盲复算）→ 报告 → 成果。"""
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": RIGHT}),
        node("card", "metrics", caliber="销售口径", caliber_version="v1", metrics=[GMV, DAYS]),
        *([node("dual", "dual_check", "复核销售额", **{"definitions": {"gmv": DEFINITION}, **config})] if dual else []),
        node("write", "report", "写周报", instructions="写一句周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


async def decide(client, run_id: str, choices: dict) -> Run:
    async with SessionLocal() as session:
        approval = (await session.execute(select(Approval).where(
            Approval.run_id == run_id, Approval.status == "pending"))).scalar_one()
    res = await client.post(f"/api/approvals/{approval.id}/decide",
                            json={"approved": True, "value": {"choices": choices, "note": ""}})
    assert res.status_code == 200, res.text
    return await wait(run_id, ("succeeded", "failed"))


async def test_an_agreeing_dual_check_shows_on_the_metric_step(client, monkeypatch, shop):
    Blind(monkeypatch, shop, {"销售额": (RIGHT, "gmv")}, writer=WRITER)
    run = await start(dual_graph(shop))
    assert run.status == "succeeded", run.error
    step = (await segment(client, run.id, "330.0元"))["chain"][0]
    assert step["step"] == "metric" and step["metric"] == "gmv"
    assert step["dual_check"] == {"status": "agreed", "node_id": "dual", "blind_rendered": "330.0元"}
    # 节点跑了、这个指标没复算（它没有来自查询的输入）：照实说没复算、为什么，不是缺省
    days = (await segment(client, run.id, "7天"))["chain"][0]
    assert days["dual_check"] == {"status": "skipped", "node_id": "dual", "reason": "no_inputs"}


async def test_a_metric_left_out_of_the_subset_is_not_said_to_lack_query_inputs(client, monkeypatch, shop):
    """「复算的指标」只选了统计天数：它没有来自查询的输入，节点以 no_inputs 直通。销售额明明取自查询，只是没被选上——
    它的说法是不在复算范围内，不能套用节点那句「没有来自查询的输入」。"""
    Blind(monkeypatch, shop, {}, writer=WRITER)
    run = await start(dual_graph(shop, metrics=["days"]))
    assert run.status == "succeeded", run.error
    gmv = (await segment(client, run.id, "330.0元"))["chain"][0]["dual_check"]
    assert gmv == {"status": "skipped", "node_id": "dual", "reason": "not_targeted"}
    days = (await segment(client, run.id, "7天"))["chain"][0]["dual_check"]
    assert days == {"status": "skipped", "node_id": "dual", "reason": "no_inputs"}


@pytest.mark.parametrize("choice", ["b", None])
async def test_a_disagreement_shows_what_the_reviewer_chose(client, monkeypatch, shop, choice):
    Blind(monkeypatch, shop, {"销售额": (WITH_REFUNDS, "gmv")}, writer=WRITER)
    run = await start(dual_graph(shop))
    assert run.status == "interrupted", run.error
    run = await decide(client, run.id, {"gmv": choice} if choice else {})
    assert run.status == "succeeded", run.error
    check = (await segment(client, run.id, "330.0元"))["chain"][0]["dual_check"]
    assert check == {"status": "disagreed", "node_id": "dual", "blind_rendered": "380.0元",
                     **({"choice": choice} if choice else {})}


async def test_a_dual_check_turned_off_is_skipped(client, monkeypatch, shop):
    Blind(monkeypatch, shop, {}, writer=WRITER)
    run = await start(dual_graph(shop, enabled=False))
    assert run.status == "succeeded", run.error
    for text in ("330.0元", "7天"):
        step = (await segment(client, run.id, text))["chain"][0]
        assert step["dual_check"] == {"status": "skipped", "node_id": "dual", "reason": "disabled"}, text


async def test_a_metric_the_blind_agent_could_not_compute_is_unchecked(client, monkeypatch, shop):
    Blind(monkeypatch, shop, {}, writer=WRITER)
    run = await start(dual_graph(shop))
    assert run.status == "succeeded", run.error
    check = (await segment(client, run.id, "330.0元"))["chain"][0]["dual_check"]
    assert check["status"] == "unchecked" and check["node_id"] == "dual" and check["reason"]
    assert "choice" not in check and "blind_rendered" not in check


async def test_runs_without_verification_nodes_answer_as_before(client, monkeypatch, shop):
    Blind(monkeypatch, shop, {}, writer=WRITER)
    run = await start(dual_graph(shop, dual=False))
    assert run.status == "succeeded", run.error
    body = await segment(client, run.id, "330.0元")
    assert "dual_check" not in body["chain"][0] and "challenge" not in body["unit"]


# --------------------------------------------------------------------------
# 反方审稿：真跑
# --------------------------------------------------------------------------

PRICED = "claude-sonnet-5"
#: 各区域销售额（含退款）：华东 1500、华南 900、华北 400
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
NET = "SELECT region AS 区域, SUM(amount) AS 净销售额 FROM sales WHERE refunded = 0 GROUP BY region ORDER BY 净销售额 DESC"
REFUNDS = "SELECT COUNT(*) AS 退款笔数 FROM sales WHERE refunded = 1"
#: 一句有依据的结论、一句预埋的错误结论（华南其实低于华东），前面一个标题
TEXT = "## 区域周报\n\n华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额超过华东。[[see:Q1]]"
EAST, SOUTH = "华东销售额 1,500，居各区域之首。", "华南销售额超过华东。"
PLAYS = {
    "华南销售额超过华东": ([NET], "refuted", "扣除退款后华南为 [[v:Q1.r1.净销售额]]，仍低于华东的 [[v:Q1.r0.净销售额]]。[[see:Q1]]"),
    "居各区域之首": ([REFUNDS], "weakened", "华东含 [[v:Q1.r0.退款笔数]] 笔退款，扣除后仍居首，但销售额被高估。"),
}


@pytest.fixture
async def sales(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, refunded INTEGER);"
        "INSERT INTO sales VALUES (1, '华东', 1200, 0), (2, '华东', 300, 1), (3, '华南', 900, 0), (4, '华北', 400, 0);")
    db.commit()
    db.close()
    name = f"sales_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True, options={})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


def reviewer(monkeypatch, source: str) -> None:
    """写作者交 TEXT；反方 agent 按任务里的结论认出是哪一句，照 PLAYS 查询、交结论和理由。"""

    def decide(model, messages):
        if challenge_mod.CHALLENGE_SYSTEM not in str(messages[0].content):
            return AIMessage(content=TEXT)
        task = str(messages[1].content) if len(messages) > 1 else ""
        queries, verdict, rationale = next((p for k, p in PLAYS.items() if k in task), ([], "stands", "没有找到反例。"))
        if model.response_format:
            return AIMessage(content=json.dumps({"verdict": verdict, "rationale": rationale}, ensure_ascii=False))
        done = sum(1 for m in messages if isinstance(m, ToolMessage))
        if done < len(queries):
            call = {"name": f"db_query__{source}", "args": {"sql": queries[done]}, "id": f"q{done}-{uuid.uuid4().hex[:6]}"}
            return AIMessage(content="查一下。", tool_calls=[call])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(MockChatModel, "_decide", decide)

    async def verify_model(session, spec):
        return MockChatModel(model_name=PRICED, latency=0.0), PRICED

    monkeypatch.setattr(verify_agent, "get_chat_model", verify_model)


def challenge_graph(source: str, *, challenge: bool = True) -> dict:
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        *([node("chal", "challenge", "审稿", report="write")] if challenge else []),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write"}),
    )


async def challenge_output(run_id: str) -> dict:
    from app.db.models import RunEvent

    async with SessionLocal() as session:
        [finished] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "node.finished", RunEvent.node_id == "chal"))).scalars()
    return artifact_store.load(finished.data["artifact"])


async def test_a_challenged_sentence_carries_the_verdict(client, monkeypatch, sales):
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales), run_class="formal")
    assert run.status == "succeeded", run.error
    output = await challenge_output(run.id)
    record = output["challenge_artifact"]
    south = (await sentence(client, run.id, SOUTH))["unit"]["challenge"]
    # counts 是整份报告的审稿计数（节点产出里的那一份）：一句被推翻、一句被削弱，不是只数这一句；节点产出带着
    # unreviewed（没能反查的句数，这里两句都查了），照样带上
    assert south == {"verdict": "refuted", "node_id": "chal", "artifact": record, "on_refuted": "degrade",
                     "report_label": "写周报", "counts": {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 0},
                     "unreviewed": 0}
    assert south["unreviewed"] == output["unreviewed"]
    assert south["counts"] == output["counts"]
    # 点开句子里的数字，片段接口答的这一句同样带着判断
    east = (await segment(client, run.id, "1,500"))["unit"]["challenge"]
    assert east["verdict"] == "weakened" and east["artifact"] == record and east["counts"] == output["counts"]
    # 反方没审的（标题不是结论）：没有这个键
    heading = await sentence(client, run.id, kind="heading")
    assert heading["unit"]["kind"] == "heading" and "challenge" not in heading["unit"]


async def test_a_lost_review_record_still_shows_the_listed_verdicts(client, monkeypatch, sales):
    """审稿记录没存上（challenge_artifact 为空）：判断取产出里列的那几句，工件为空（理由和查询看不了）。"""
    async def lost(self, record):
        return None

    monkeypatch.setattr(challenge_mod._Review, "_store", lost)
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales), run_class="formal")
    assert run.status == "succeeded", run.error
    assert (await challenge_output(run.id))["challenge_artifact"] is None
    south = (await sentence(client, run.id, SOUTH))["unit"]["challenge"]
    assert south == {"verdict": "refuted", "node_id": "chal", "artifact": None, "on_refuted": "degrade",
                     "report_label": "写周报", "counts": {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 0},
                     "unreviewed": 0}


async def test_a_review_that_stopped_the_run_still_marks_its_sentences(client, monkeypatch, sales):
    """「结论被推翻时」设为报错并停止运行：节点报错、没有 node.finished，运行失败。被推翻的是哪几句、反方凭什么，
    证据面板里照样要看得到——按审完那一刻的日志（challenge_done）认审稿记录，处置写 fail。"""
    reviewer(monkeypatch, sales)
    graph = challenge_graph(sales)
    next(n for n in graph["nodes"] if n["id"] == "chal")["data"]["config"]["on_refuted"] = "fail"
    run = await start(graph, run_class="formal")
    assert run.status == "failed" and "推翻" in (run.error or ""), run.error
    south = (await sentence(client, run.id, SOUTH))["unit"]["challenge"]
    assert south["verdict"] == "refuted" and south["on_refuted"] == "fail" and south["node_id"] == "chal"
    assert south["artifact"] and south["counts"] == {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 0}
    east = (await segment(client, run.id, "1,500"))["unit"]["challenge"]
    assert east["verdict"] == "weakened" and east["artifact"] == south["artifact"]


async def test_reports_without_a_challenge_have_no_verdicts(client, monkeypatch, sales):
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales, challenge=False), run_class="formal")
    assert run.status == "succeeded", run.error
    assert "challenge" not in (await sentence(client, run.id, SOUTH))["unit"]


# --------------------------------------------------------------------------
# 认「哪一次」：手写的封存事件
# --------------------------------------------------------------------------


def sealed_of(graph: dict, events: list[tuple]) -> evidence._Sealed:
    return evidence._Sealed(run_id="syn-run", graph=graph, events=events,
                            seal={"sealed": True, "ok": True, "manifest_seq": events[-1][0], "legacy": False})


def card_set(artifact: str) -> dict:
    return {"evidence": [{"kind": "metric_set", "node_id": "card", "artifact": artifact}]}


async def test_the_dual_check_belongs_to_the_card_result_it_checked():
    """口径卡在循环里算了两次：双盲复算只复算了第一次的结果，第二次的指标没有复算状态；之后又跑了一次直通的，第二次的
    结果记成跳过，第一次的不受影响。"""
    first = await put_json({"kind": "metric_set", "metrics": [{"id": "gmv", "value": 1.5}], "n": 1}, kind="metric_set")
    second = await put_json({"kind": "metric_set", "metrics": [{"id": "gmv", "value": 2.5}], "n": 2}, kind="metric_set")
    checked = await put_json({"status": "agreed", "counts": {"agreed": 1, "disagreed": 0, "unchecked": 0},
                              "metrics": [{"metric_id": "gmv", "name": "销售额", "unit": "元", "status": "agreed",
                                           "original_rendered": "1.5元", "blind_rendered": "1.5元", "delta_pct": 0.0}],
                              "model": "mock", "same_model": False}, kind="node_output")
    skipped = await put_json({"status": "skipped", "reason": "disabled"}, kind="node_output")
    graph = chain(node("card", "metrics"), node("dual", "dual_check", card="card"))
    events = [
        (1, "node.started", "card", {}),
        (2, "node.finished", "card", card_set(first)),
        (3, "node.started", "dual", {}),
        (4, "node.finished", "dual", {"artifact": checked, "evidence": [{
            "kind": "dual_check", "node_id": "dual", "exec": 1, "card": "card", "status": "agreed",
            "counts": {"agreed": 1, "disagreed": 0, "unchecked": 0}, "metrics": [{"id": "gmv", "status": "agreed"}],
            "metric_set": first}]}),
        (5, "node.started", "card", {}),
        (6, "node.finished", "card", card_set(second)),
    ]
    agreed = {"status": "agreed", "node_id": "dual", "blind_rendered": "1.5元"}
    assert evidence._dual_check_of(sealed_of(graph, events), "card", first, "gmv") == agreed
    assert evidence._dual_check_of(sealed_of(graph, events), "card", second, "gmv") is None
    later = [*events, (7, "node.started", "dual", {}), (8, "node.finished", "dual", {"artifact": skipped})]
    assert evidence._dual_check_of(sealed_of(graph, later), "card", second, "gmv") == {
        "status": "skipped", "node_id": "dual", "reason": "disabled"}
    assert evidence._dual_check_of(sealed_of(graph, later), "card", first, "gmv") == agreed
    # 别的口径卡、卡上没有的指标：没有
    assert evidence._dual_check_of(sealed_of(graph, events), "other", first, "gmv") is None
    assert evidence._dual_check_of(sealed_of(graph, events), "card", first, "ghost") == {
        "status": "skipped", "node_id": "dual", "reason": "not_targeted"}


async def test_an_unreadable_dual_check_output_falls_back_to_its_ledger_entry():
    """产出的文件不在了：状态和审核人的选择按台账条目说，盲算值说不了就不给。"""
    card = await put_json({"kind": "metric_set", "metrics": [{"id": "gmv", "value": 3.5}], "n": 3}, kind="metric_set")
    graph = chain(node("card", "metrics"), node("dual", "dual_check", card="card"))
    events = [
        (1, "node.finished", "card", card_set(card)),
        (2, "node.finished", "dual", {"artifact": "f0" * 32, "evidence": [{
            "kind": "dual_check", "node_id": "dual", "exec": 1, "card": "card", "status": "resolved",
            "metrics": [{"id": "gmv", "status": "disagreed", "choice": "a"}], "metric_set": card}]}),
    ]
    assert evidence._dual_check_of(sealed_of(graph, events), "card", card, "gmv") == {
        "status": "disagreed", "node_id": "dual", "choice": "a"}
    assert evidence._dual_check_of(sealed_of(graph, events), "card", card, "other") == {
        "status": "skipped", "node_id": "dual", "reason": "not_targeted"}


def report(doc_artifact: str) -> evidence._Report:
    return evidence._Report(node_id="write", doc_artifact=doc_artifact, ok=True, repairs=0, stats=None, doc=None,
                            hash_ok=True, fields=[])


@pytest.mark.parametrize("saved", [True, False])
async def test_a_review_of_an_earlier_draft_does_not_mark_the_rewritten_report(saved):
    """报告在反方审稿之后又重写了一次（循环、接着跑）：审的是前一稿，新的一稿上同一个句子编号说的可能是另一句话。
    审稿记录存上了按记录里的文档认，没存上按它跑完之前那份报告最后一次自查的文档认。"""
    draft = await put_json({"markdown": "第一稿", "n": 1}, kind="report_doc")
    final = await put_json({"markdown": "第二稿", "n": 2}, kind="report_doc")
    claims = [{"unit": "u1", "text": "华南销售额超过华东。", "verdict": "refuted"}]
    record = await put_json({"report": "write", "doc_artifact": draft, "model": "mock",
                             "claims": [{**c, "rationale": "", "rationale_doc": None, "queries": []} for c in claims]},
                            kind="challenge") if saved else None
    out = await put_json({"challenge_artifact": record, "counts": {"stands": 0, "weakened": 0, "refuted": 1,
                                                                   "inconclusive": 0},
                          "items": claims, "on_refuted": "flag"}, kind="node_output")
    graph = chain(node("write", "report", "写周报"), node("chal", "challenge", "审稿", report="write"))
    events = [
        (1, "report.checked", "write", {"doc_artifact": draft, "ok": True}),
        (2, "node.finished", "write", {}),
        (3, "node.finished", "chal", {"artifact": out}),
        (4, "report.checked", "write", {"doc_artifact": final, "ok": True}),
    ]
    sealed = sealed_of(graph, events)
    assert evidence._challenge_of(sealed, report(draft), "u1") == {
        "verdict": "refuted", "node_id": "chal", "artifact": record, "on_refuted": "flag", "report_label": "写周报",
        "counts": {"stands": 0, "weakened": 0, "refuted": 1, "inconclusive": 0}}
    assert evidence._challenge_of(sealed, report(final), "u1") is None
    assert evidence._challenge_of(sealed, report(draft), "u2") is None


async def test_the_counts_are_the_whole_reviews_and_unreviewed_rides_along():
    """counts 照产出（整份报告审了 12 句），没能反查的句数产出里有就带上；产出里的计数认不出来时按审稿记录数。"""
    doc = await put_json({"markdown": "周报", "n": 3}, kind="report_doc")
    claims = [{"unit": "u1", "text": "华南销售额超过华东。", "verdict": "weakened"},
              {"unit": "u2", "text": "华北持平。", "verdict": "stands"}]
    record = await put_json({"report": "write", "doc_artifact": doc, "model": "mock",
                             "claims": [{**c, "rationale": "", "rationale_doc": None, "queries": []} for c in claims]},
                            kind="challenge")
    graph = chain(node("write", "report", "写周报"), node("chal", "challenge", "审稿", report="write"))

    async def answer(output: dict) -> dict:
        out = await put_json({"challenge_artifact": record, "items": claims, "on_refuted": "degrade", **output},
                             kind="node_output")
        events = [(1, "report.checked", "write", {"doc_artifact": doc, "ok": True}),
                  (2, "node.finished", "chal", {"artifact": out})]
        return evidence._challenge_of(sealed_of(graph, events), report(doc), "u1")

    got = await answer({"counts": {"stands": 7, "weakened": 3, "refuted": 2, "inconclusive": 0}, "unreviewed": 1})
    assert got["counts"] == {"stands": 7, "weakened": 3, "refuted": 2, "inconclusive": 0} and got["unreviewed"] == 1
    # 计数格式不对（一个数都认不出来）：按审稿记录里的句子数，不写四个 0
    got = await answer({"counts": {"stands": "7"}, "unreviewed": True})
    assert got["counts"] == {"stands": 1, "weakened": 1, "refuted": 0, "inconclusive": 0} and "unreviewed" not in got


async def test_a_review_that_failed_the_node_needs_its_record():
    """节点报错：认审完那一刻的日志。重跑（node.started）之后没再审完的不算；日志里没有审稿记录（保存失败）认不出审的
    是哪一稿，也不算。"""
    doc = await put_json({"markdown": "周报", "n": 4}, kind="report_doc")
    claims = [{"unit": "u1", "text": "华南销售额超过华东。", "verdict": "refuted", "rationale": "", "rationale_doc": None,
               "queries": []}]
    record = await put_json({"report": "write", "doc_artifact": doc, "model": "mock", "claims": claims},
                            kind="challenge")
    graph = chain(node("write", "report", "写周报"), node("chal", "challenge", "审稿", report="write",
                                                       on_refuted="fail"))
    counts = {"stands": 0, "weakened": 0, "refuted": 1, "inconclusive": 0}
    head = [(1, "report.checked", "write", {"doc_artifact": doc, "ok": True}), (2, "node.started", "chal", {})]
    done = (3, "log", "chal", {"code": "challenge_done", "counts": counts, "artifact": record})
    failed = (4, "node.failed", "chal", {"error": "反方审稿推翻了 1 句结论"})
    got = evidence._challenge_of(sealed_of(graph, [*head, done, failed]), report(doc), "u1")
    assert got == {"verdict": "refuted", "node_id": "chal", "artifact": record, "on_refuted": "fail",
                   "report_label": "写周报", "counts": counts}
    retried = [*head, done, (4, "node.started", "chal", {}), (5, "node.failed", "chal", {"error": "模型不可用"})]
    assert evidence._challenge_of(sealed_of(graph, retried), report(doc), "u1") is None
    lost = [*head, (3, "log", "chal", {"code": "challenge_done", "counts": counts, "artifact": None}), failed]
    assert evidence._challenge_of(sealed_of(graph, lost), report(doc), "u1") is None
