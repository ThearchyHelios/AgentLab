"""审批质询（engine/inquiry.py、api/inquiry.py）：运行停在人工审批时，审批人针对报告里的一句话提问，一个只读 agent
现查现答；问答写进这次运行（approval.inquiry 事件），随运行结束一起封存，报告本身一个字不改。

- 停在审批时质询：事件 node_id 为空、数据按前后端约定的形状；质询的查询工件只在 inquiry_artifacts / queries 里，
  不在 query_artifact 下（证据接口不把它们当成报告的证据）；批准、运行结束封存之后 verify 一致，封存范围包含这条事件；
  用量计入运行用量
- 回答期间运行被批准（竞态）：回答照样交回，recorded 为假、note 说明，事件不落库
- 同一运行同时只答一条：第二条质询、审批回复（批准 / 驳回）、继续运行都回 409 inquiry_busy，拒掉的批准不留痕迹
- 封存之后：探索运行可以追问（post_seal，落在封存之后，不计运行用量），正式运行 409；设置里关了 409；
  没停在那张审批卡上、还在执行、封存核对不通过都 409；输入不合法 422，指明是哪一项
- 回答：先给结论，数字只能引用自己查询的格子或报告已有的指标，裸数字被标出；遮罩列的格子渲染成「已遮罩」，
  回答正文、查询清单、事件里都不出现遮罩列的值
- 触顶、失败照样记一条事件
- 读取：列表按报告、句子筛选，按时间排；质询工件只认质询引用过的，遮罩照证据面板
- 恢复后的重放不受影响：前面的节点不重复执行

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、手机号都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Approval, DataSource, Run, RunEvent, Setting
from app.engine import inquiry as inquiry_mod
from app.engine import spend, verify_agent
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager, verify_manifest
from app.main import app
from app.providers.factory import ProviderNotConfigured
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
#: 各区域销售额：华东 1500（其中 300 是测试账号下的单）、华南 900、华北 400
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
TEXT = "华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额 [[v:Q1.r1.销售额]]。[[see:Q1]]"
EAST, SOUTH = "华东销售额 1,500，居各区域之首。", "华南销售额 900。"
NO_TEST = ("SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales WHERE is_test = 0 GROUP BY region "
           "ORDER BY 销售额 DESC")
NO_TEST_ANSWER = "仍居各区域之首：排除测试账号后华东为 [[v:Q1.r0.销售额]]，华南为 [[v:Q1.r1.销售额]]。[[see:Q1]]"
NO_TEST_TEXT = "仍居各区域之首：排除测试账号后华东为 1,200，华南为 900。"
QUESTION = "排除测试账号会怎样？"
USAGE_KEYS = {"input_tokens", "output_tokens", "cost_usd", "calls", "unpriced_calls"}
ITEM_KEYS = {"seq", "at", "actor", "report", "unit", "quote", "question", "answer", "queries", "model", "usage",
             "post_seal", "approval_id"}
EVENT_KEYS = {"question", "quote", "report", "unit", "approval_id", "actor", "answer_text", "answer_doc", "ok",
              "limit", "error", "inquiry_artifacts", "queries", "model", "usage"}
QUERY_KEYS = {"alias", "artifact", "sql", "row_count", "truncated", "source"}
NOT_RECORDED = "运行状态已经变化，这次质询的问答没有写入运行记录"
BUSY_DECIDE = {"detail": "正在回答质询，回答完再批准或驳回", "code": "inquiry_busy"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(
            [spend.SPEND_KEY, JUDGE_SPEND_KEY, "verify", "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(tmp_path, *, options=None) -> tuple[str, int]:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER, is_test INTEGER);"
        "INSERT INTO sales VALUES (1, '华东', '13900000001', 1200, 0), (2, '华东', '13900000002', 300, 1),"
        " (3, '华南', '13900000003', 900, 0), (4, '华北', '13900000004', 400, 0);")
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options=options or {})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        return name, row.id


@pytest.fixture
async def shop(tmp_path):
    """只读的手工源：四笔订单，华东两笔（其中一笔 300 是测试账号）、华南一笔 900、华北一笔 400。返回源名。"""
    name, source_id = await _source(tmp_path)
    yield name
    await engines.invalidate(source_id)


@pytest.fixture
async def masked_shop(tmp_path):
    """同样的四笔订单，买家手机号这一列设了遮罩。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"})
    yield name
    await engines.invalidate(source_id)


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def gated(source: str, *, gate: bool = True) -> dict:
    """取数 → 报告 → 人工审批 → 成果。gate 为假时没有审批，一口气跑完、封存。"""
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        *([node("gate", "human", mode="approve", title="周报可以出具吗")] if gate else []),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


@dataclass
class Play:
    """质询 agent 对某个问题的剧本：依次执行的查询，最后交的回答。"""

    queries: list[str] = field(default_factory=list)
    answer: str = "查不到判断所需的数据。"


PLAYS = {"排除测试账号": Play([NO_TEST], NO_TEST_ANSWER)}


class Models:
    """写作者和质询 agent 两份剧本。写作者回 writer；质询 agent 按任务里的问题认出剧本，照着查询、交回答。

    before：质询 agent 每次调用模型之前先 await 它（测试在这里卡住回答、或者在回答途中批掉审批）。
    tasks 记下质询 agent 收到的任务原文，finals 记下交回答那次调用收到的消息，specs 记下每次要的模型。"""

    def __init__(self, monkeypatch, source: str | None = None, *, writer: str = TEXT, plays=None,
                 model_id: str = PRICED, broken: Exception | None = None, before=None):
        self.tasks: list[str] = []
        self.finals: list[list] = []
        self.writes = 0
        self.specs: list = []
        plays = PLAYS if plays is None else plays

        def which(task: str) -> Play:
            return next((p for k, p in plays.items() if k in task), Play())

        def decide(model, messages):
            system = str(messages[0].content)
            if inquiry_mod.INQUIRY_SYSTEM in system:
                task = str(messages[1].content) if len(messages) > 1 else ""
                play = which(task)
                if model.response_format:
                    self.finals.append(list(messages))
                    return AIMessage(content=json.dumps({"rationale": play.answer}, ensure_ascii=False))
                if not any(isinstance(m, ToolMessage) for m in messages):
                    self.tasks.append(task)
                done = sum(1 for m in messages if isinstance(m, ToolMessage))
                if done < len(play.queries):
                    call = {"name": f"db_query__{source}", "args": {"sql": play.queries[done]}, "id": f"iq-{done}"}
                    return AIMessage(content="查一下。", tool_calls=[call])
                return AIMessage(content="查完了。")
            if report_mod.ROLE in system:
                self.writes += 1
                return AIMessage(content=writer)
            raise AssertionError(f"没想到的模型调用：{system[:60]}")

        monkeypatch.setattr(MockChatModel, "_decide", decide)
        original = MockChatModel._agenerate

        async def held(chat, messages, stop=None, run_manager=None, **kwargs):
            if before is not None and inquiry_mod.INQUIRY_SYSTEM in str(messages[0].content):
                await before()
            return await original(chat, messages, stop, run_manager, **kwargs)

        monkeypatch.setattr(MockChatModel, "_agenerate", held)

        async def verify_model(session, spec):
            self.specs.append(spec)
            if broken is not None:
                raise broken
            return MockChatModel(model_name=model_id), model_id

        monkeypatch.setattr(verify_agent, "get_chat_model", verify_model)


async def _row(run_id: str) -> Run:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(600):
        row = await _row(run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return await _row(run_id)
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def pause(source: str, *, run_class: str = "exploratory", graph: dict | None = None) -> Run:
    run = await run_manager.start(graph=graph or gated(source), input_payload={}, run_class=run_class)
    return await wait(run.id, ("interrupted",))


async def finish(source: str, *, run_class: str = "exploratory", graph: dict | None = None) -> Run:
    run = await run_manager.start(graph=graph or gated(source, gate=False), input_payload={}, run_class=run_class)
    return await wait(run.id)


async def pending(run_id: str) -> list[Approval]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(Approval).where(Approval.run_id == run_id, Approval.status == "pending"))).scalars())


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def report_doc(run_id: str) -> dict:
    [checked] = await events(run_id, "report.checked", "write")
    return artifact_store.load(checked.data["doc_artifact"])


def unit_of(doc: dict, text: str) -> str:
    md = doc["markdown"]
    return next(u["id"] for _, u in iter_units(doc) if md[u["span"][0]:u["span"][1]].strip() == text)


async def ask(client, run_id: str, **body):
    return await client.post(f"/api/runs/{run_id}/inquiries", json=body)


async def set_verify(value: dict) -> None:
    async with SessionLocal() as session:
        session.add(Setting(key="verify", value=value))
        await session.commit()


# --------------------------------------------------------------------------
# 停在审批时质询 → 写进事件 → 批准 → 封存
# --------------------------------------------------------------------------


async def test_asking_while_waiting_is_recorded_and_sealed_with_the_run(monkeypatch, shop, client):
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    east = unit_of(doc, EAST)

    r = await ask(client, paused.id, report="write", unit=east, quote="居各区域之首", question=QUESTION,
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.keys() == {"inquiry", "recorded", "note"}
    assert body["recorded"] is True and body["note"] is None
    item = body["inquiry"]
    assert item.keys() == ITEM_KEYS
    assert (item["report"], item["unit"], item["quote"], item["question"], item["approval_id"]) == (
        "write", east, "居各区域之首", QUESTION, approval.id)
    assert item["post_seal"] is False and item["actor"] is None and isinstance(item["at"], str)
    answer = item["answer"]
    assert answer.keys() == {"text", "doc_artifact", "ok", "limit", "error"}
    assert (answer["ok"], answer["limit"], answer["error"]) == (True, None, None)
    assert answer["text"] == NO_TEST_TEXT, "回答先给结论，数字按查询结果换好"
    answer_doc = artifact_store.load(answer["doc_artifact"])
    assert answer_doc["markdown"] == answer["text"] and answer_doc["violations"] == []
    cited = [s["cite"] for s in iter_segments(answer_doc) if s.get("cite")]
    assert [(c["alias"], c["locator"]["row"], c["value"]) for c in cited] == [("Q1", 0, 1200), ("Q1", 1, 900)]
    [query] = item["queries"]
    assert query.keys() == QUERY_KEYS
    assert (query["alias"], query["source"], query["sql"], query["row_count"], query["truncated"]) == (
        "Q1", shop, NO_TEST, 3, False)
    assert artifact_store.load(query["artifact"])["rows"] == [["华东", 1200], ["华南", 900], ["华北", 400]]
    assert item["model"] == PRICED
    assert item["usage"].keys() == USAGE_KEYS and item["usage"]["calls"] >= 2 and item["usage"]["cost_usd"] > 0

    # 交给质询 agent 的：被问的那句、它挂的证据摘录（原报告的编号标明）、选中的原文、问题、可以查的数据源
    [task] = models.tasks
    assert EAST in task and "【原报告 Q1】" in task and "居各区域之首" in task and QUESTION in task and shop in task

    # 事件：node_id 为空，数据按约定；查询工件不在 query_artifact 下
    [event] = await events(paused.id, "approval.inquiry")
    assert event.node_id is None and event.seq == item["seq"]
    assert event.data.keys() == EVENT_KEYS
    assert event.data["inquiry_artifacts"] == [query["artifact"]]
    assert event.data["queries"] == item["queries"]
    assert (event.data["answer_text"], event.data["answer_doc"]) == (answer["text"], answer["doc_artifact"])
    assert "query_artifact" not in event.data
    assert (await _row(paused.id)).status == "interrupted"

    # 列表里是同一条
    listed = await client.get(f"/api/runs/{paused.id}/inquiries")
    assert listed.status_code == 200 and listed.json() == {"items": [item]}

    # 批准 → 运行结束封存：这条事件在封存范围里，verify 一致
    r = await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": True})
    assert r.status_code == 200, r.text
    final = await wait(paused.id)
    assert final.status == "succeeded", final.error
    assert final.manifest_seq > event.seq
    assert (await verify_manifest(paused.id))["ok"] is True
    # 用量计进运行用量，明细在 inquiry 下
    assert final.usage["inquiry"]["calls"] == item["usage"]["calls"]
    assert final.usage["inquiry"]["input_tokens"] == item["usage"]["input_tokens"]
    # 证据接口不把质询的查询当成报告的证据
    from app.api.evidence import _sealed

    assert query["artifact"] not in (await _sealed(paused.id)).queries
    # 报告本身一个字没改
    assert (await report_doc(paused.id)) == doc
    # 改它一个字，封存核对就对不上：它确实在封存范围里
    async with SessionLocal() as session:
        stored = await session.get(RunEvent, event.id)
        stored.data = {**stored.data, "answer_text": "改过的回答"}
        await session.commit()
    assert (await verify_manifest(paused.id))["ok"] is False


async def test_an_approval_that_lands_while_answering_leaves_no_record(monkeypatch, shop, client):
    """回答途中运行被批准了（另一个进程、或者抢在登记之前的批准）：回答照样交回给提问的人，但不写进运行记录。"""
    seen: dict = {}

    async def approve_meanwhile():
        if not seen.get("done"):
            seen["done"] = True
            await run_manager.resume(seen["run"], {"approved": True}, approval_id=seen["approval"])

    Models(monkeypatch, shop, before=approve_meanwhile)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    seen.update(run=paused.id, approval=approval.id)

    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is False and body["note"] == NOT_RECORDED
    assert body["inquiry"]["seq"] is None and body["inquiry"]["answer"]["ok"] is True
    assert body["inquiry"]["answer"]["text"] == NO_TEST_TEXT

    final = await wait(paused.id)
    assert final.status == "succeeded", final.error
    assert await events(paused.id, "approval.inquiry") == []
    assert (await verify_manifest(paused.id))["ok"] is True
    assert "inquiry" not in final.usage
    assert (await client.get(f"/api/runs/{paused.id}/inquiries")).json() == {"items": []}


async def test_while_answering_approving_resuming_and_continuing_are_refused(monkeypatch, shop, client):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    Models(monkeypatch, shop, before=hold)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    asking = asyncio.create_task(ask(client, paused.id, report="write", unit=None, question=QUESTION,
                                     approval_id=approval.id))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        refused = await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": False, "note": "先别批"},
                                    headers={"X-Actor": "reviewer"})
        assert refused.status_code == 409 and refused.json() == BUSY_DECIDE
        refused = await client.post(f"/api/runs/{paused.id}/resume",
                                    json={"response": {"approved": True}, "approval_id": approval.id})
        assert refused.status_code == 409 and refused.json() == BUSY_DECIDE
        refused = await client.post(f"/api/runs/{paused.id}/continue", json={})
        assert refused.status_code == 409
        assert refused.json() == {"detail": "正在回答质询，回答完再继续运行", "code": "inquiry_busy"}
        second = await ask(client, paused.id, report="write", unit=None, question="华南为什么低？",
                           approval_id=approval.id)
        assert second.status_code == 409 and second.json()["code"] == "inquiry_busy"
        # 拒掉的批准不留痕迹：卡仍待审批，没记下签批人
        async with SessionLocal() as session:
            card = await session.get(Approval, approval.id)
        assert card.status == "pending" and card.resolved_by is None
        assert (await _row(paused.id)).status == "interrupted"
    finally:
        release.set()
    answered = await asyncio.wait_for(asking, 30)
    assert answered.status_code == 200 and answered.json()["recorded"] is True

    # 回答完了，照常批准
    r = await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": True})
    assert r.status_code == 200, r.text
    assert (await wait(paused.id)).status == "succeeded"


# --------------------------------------------------------------------------
# 封存之后：探索运行可以追问，正式运行不行
# --------------------------------------------------------------------------


async def test_after_the_seal_only_exploratory_runs_take_inquiries(monkeypatch, shop, client):
    Models(monkeypatch, shop)
    done = await finish(shop)
    assert done.status == "succeeded", done.error
    east = unit_of(await report_doc(done.id), EAST)

    r = await ask(client, done.id, report="write", unit=east, question=QUESTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True and body["note"] is None
    item = body["inquiry"]
    assert item["post_seal"] is True and item["approval_id"] is None and item["answer"]["text"] == NO_TEST_TEXT
    [event] = await events(done.id, "approval.inquiry")
    assert event.data["post_seal"] is True and event.seq > done.manifest_seq
    after = await _row(done.id)
    assert (after.manifest_seq, after.manifest_hash) == (done.manifest_seq, done.manifest_hash)
    assert (await verify_manifest(done.id))["ok"] is True
    assert "inquiry" not in after.usage, "封存之后的问答是批注，不计进运行用量"
    assert (await client.get(f"/api/runs/{done.id}/inquiries")).json()["items"] == [item]

    # 带着审批卡来问：那张卡已经不在等了
    r = await ask(client, done.id, report="write", unit=east, question=QUESTION, approval_id="no-such-card")
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"

    formal = await finish(shop, run_class="formal")
    assert formal.status == "succeeded", formal.error
    r = await ask(client, formal.id, report="write", unit=unit_of(await report_doc(formal.id), EAST),
                  question=QUESTION)
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"
    assert "正式运行" in r.json()["detail"]
    assert await events(formal.id, "approval.inquiry") == []


async def test_a_formal_run_waiting_for_approval_takes_inquiries(monkeypatch, shop, client):
    """正式运行停在审批时可以质询：问答随运行一起封存，正式运行同样可以复核。"""
    Models(monkeypatch, shop)
    paused = await pause(shop, run_class="formal")
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200 and r.json()["recorded"] is True
    await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": True})
    final = await wait(paused.id)
    [event] = await events(paused.id, "approval.inquiry")
    assert final.manifest_seq > event.seq and (await verify_manifest(paused.id))["ok"] is True


async def test_switched_off_in_settings_it_is_refused(monkeypatch, shop, client):
    await set_verify({"inquiry": {"enabled": False}})
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 409 and r.json()["code"] == "inquiry_disabled"
    assert "「审批质询 · 启用」" in r.json()["detail"]
    assert models.tasks == [] and models.specs == []
    assert await events(paused.id, "approval.inquiry") == []


async def test_the_run_must_be_waiting_on_that_approval(monkeypatch, shop, client):
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)

    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION)
    assert r.status_code == 422 and r.json() == {"detail": r.json()["detail"], "field": "approval_id"}
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id="no-such-card")
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"

    # 还在执行的运行
    async with SessionLocal() as session:
        running = Run(workflow_name="还在执行", status="running", graph={}, input={})
        session.add(running)
        await session.commit()
        running_id = running.id
    r = await ask(client, running_id, report="write", unit=None, question=QUESTION)
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"

    r = await ask(client, "no-such-run", report="write", unit=None, question=QUESTION)
    assert r.status_code == 404

    # 封存核对不通过的探索运行：报告和证据已不可信，不再质询
    done = await finish(shop)
    async with SessionLocal() as session:
        first = (await session.execute(select(RunEvent).where(RunEvent.run_id == done.id)
                                       .order_by(RunEvent.seq).limit(1))).scalar_one()
        first.data = {**(first.data or {}), "tampered": True}
        await session.commit()
    r = await ask(client, done.id, report="write", unit=None, question=QUESTION)
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"
    assert models.tasks == [], "开放条件不满足时不问模型"


@pytest.mark.parametrize("body, field_name", [
    ({"unit": None, "question": QUESTION}, "report"),
    ({"report": 3, "question": QUESTION}, "report"),
    ({"report": "write", "unit": None}, "question"),
    ({"report": "write", "unit": None, "question": "   "}, "question"),
    ({"report": "write", "unit": None, "question": "问" * 1001}, "question"),
    ({"report": "write", "unit": None, "question": QUESTION, "quote": "引" * 501}, "quote"),
    ({"report": "write", "unit": None, "question": QUESTION, "quote": 5}, "quote"),
    ({"report": "write", "unit": 12, "question": QUESTION}, "unit"),
    ({"report": "write", "unit": None, "question": QUESTION, "approval_id": 7}, "approval_id"),
])
async def test_bad_requests_name_the_field(monkeypatch, shop, client, body, field_name):
    Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, **{"approval_id": approval.id, **body})
    assert r.status_code == 422, r.text
    assert r.json().keys() == {"detail", "field"} and r.json()["field"] == field_name
    assert r.json()["detail"] and "[" not in r.json()["detail"]


async def test_the_report_and_the_sentence_must_exist(monkeypatch, shop, client):
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="no-such-report", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 422 and r.json()["field"] == "report"
    r = await ask(client, paused.id, report="write", unit="u999", question=QUESTION, approval_id=approval.id)
    assert r.status_code == 422 and r.json()["field"] == "unit"
    r = await client.post(f"/api/runs/{paused.id}/inquiries", content=b"[1, 2]",
                          headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json().keys() == {"detail", "field"}
    r = await client.post(f"/api/runs/{paused.id}/inquiries", content=b"{not json",
                          headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json().keys() == {"detail", "field"}
    assert models.tasks == []


# --------------------------------------------------------------------------
# 回答：出处、裸数字、遮罩
# --------------------------------------------------------------------------

TOP_BUYER = "SELECT buyer_phone, amount AS 金额 FROM sales ORDER BY amount DESC LIMIT 1"
BUYER_ORDERS = "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '13900000001'"


async def test_bare_numbers_are_flagged_and_masked_values_never_show(monkeypatch, masked_shop, client):
    plays = {"买得最多": Play([TOP_BUYER, BUYER_ORDERS],
                           "金额最高的一笔来自 [[v:Q1.r0.buyer_phone]]（手机号 13900000001），金额 [[v:Q1.r0.金额]]；"
                           "这位客户共 [[v:Q2.r0.笔数]] 笔订单，另有 3 笔小额订单。")}
    models = Models(monkeypatch, masked_shop, plays=plays)
    paused = await pause(masked_shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit="", question="买得最多的是哪位客户？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    assert r.json()["inquiry"]["unit"] is None, "不指明句子时写空串和写 null 一样"
    [task] = models.tasks
    assert f"设了遮罩的列（回答里不要写出这些列的值）：「{masked_shop}」的 buyer_phone" in task
    assert "1390000000" not in r.text, "回答、查询清单里都不能出现遮罩列的值"
    item = r.json()["inquiry"]
    answer = item["answer"]
    assert answer["ok"] is True
    assert answer["text"] == "金额最高的一笔来自 已遮罩（手机号 已遮罩），金额 1,200；这位客户共 1 笔订单，另有 3 笔小额订单。"
    doc = artifact_store.load(answer["doc_artifact"])
    assert "1390000000" not in json.dumps(doc, ensure_ascii=False)
    # 裸数字被标出来：界面在它下面画「无出处」
    assert [s["text"] for s in iter_segments(doc) if s.get("issue") == "uncited_number"] == ["3"]
    # 遮罩列的格子：出处照记，值是「已遮罩」
    masked = [s["cite"] for s in iter_segments(doc) if (s.get("cite") or {}).get("locator", {}).get("column")
              == "buyer_phone"]
    assert [(c["alias"], c["status"], c["value"]) for c in masked] == [("Q1", "resolved", "已遮罩")]
    first, second = item["queries"]
    assert first["sql"] == TOP_BUYER
    assert second["sql"] == "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '已遮罩'"
    [event] = await events(paused.id, "approval.inquiry")
    assert "1390000000" not in json.dumps(event.data, ensure_ascii=False)

    # 质询工件：遮罩照证据面板，查询里写着的遮罩列的值同样换掉
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{first['artifact']}")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["rows"] == [["已遮罩", 1200]] and got.json()["masked"] == ["buyer_phone"]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{second['artifact']}")
    assert got.status_code == 200 and "1390000000" not in got.text
    assert got.json()["content"]["rows"] == [[1]]


async def test_the_answer_may_cite_the_reports_own_metrics(monkeypatch, client):
    """报告里已有的出处（口径卡指标）回答里可以直接引用；这次运行没用到查询工具，agent 只能依据证据作答。"""
    metrics = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
    graph = chain(
        node("start", "input"),
        node("calc", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=metrics),
        node("write", "report", instructions="写周报"),
        node("gate", "human", mode="approve", title="确认"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    plays = {"销售额": Play([], "按口径卡，本周销售额为 [[m:gmv]]，这是本周的口径值。[[see:m:gmv]]")}
    models = Models(monkeypatch, None, writer="本周销售额 [[m:gmv]]。[[see:m:gmv]]", plays=plays)
    paused = await pause("", graph=graph)
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    r = await ask(client, paused.id, report="write", unit=unit_of(doc, "本周销售额 45,678.5元。"),
                  question="销售额是怎么算的？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    answer = r.json()["inquiry"]["answer"]
    assert answer["text"] == "按口径卡，本周销售额为 45,678.5元，这是本周的口径值。"
    assert artifact_store.load(answer["doc_artifact"])["violations"] == []
    [task] = models.tasks
    assert "[[m:gmv]]" in task and "销售额 = 45,678.5元" in task
    assert r.json()["inquiry"]["queries"] == []


async def test_a_cell_in_a_table_comes_with_the_whole_table(monkeypatch, shop, client):
    """问的是表格里的一格：一格的值离了行列说不清是什么，任务里另带整张表（表头在前）和这一格的出处摘录。"""
    models = Models(monkeypatch, shop, writer="各区域销售额如下。\n\n[[table:Q1]]")
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    cell = unit_of(doc, "1,500")
    r = await ask(client, paused.id, report="write", unit=cell, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    [task] = models.tasks
    assert "「1,500」" in task and "区域 | 销售额\n华东 | 1,500\n华南 | 900\n华北 | 400" in task
    # 摘录照裁判的办法：被引用的列排在前面，只给被引用的那一行
    assert "【原报告 Q1】" in task and "列：销售额 | 区域\nr0：1,500 | 华东\n" in task


def test_masked_values_are_matched_as_whole_values():
    """遮罩列的值按整值认：更长的数里的一截、千分位写法里的一截不换；一个字的值不换（换了会把不相干的词弄坏）；
    数按原样和千分位两种写法认。"""
    secrets = inquiry_mod._Secrets()
    snapshot = {"columns": ["phone", "name", "gender", "code", "salary", "region"],
                "rows": [["13900000001", "张伟", "男", "390", 12000, "华东"]]}
    secrets.add(snapshot, {"phone", "name", "gender", "code", "salary"})
    scrub = secrets.scrubber()
    assert scrub("客户 13900000001、139000000012、1,390 元、390 笔、张伟买了男装，月薪 12,000，另 12000.5") == (
        "客户 已遮罩、139000000012、1,390 元、已遮罩 笔、已遮罩买了男装，月薪 已遮罩，另 12000.5")
    assert scrub("华东") == "华东", "没遮的列不换"
    assert inquiry_mod._Secrets().scrubber()("13900000001") == "13900000001"


# --------------------------------------------------------------------------
# 触顶与失败
# --------------------------------------------------------------------------


async def test_hitting_the_query_limit_is_recorded(monkeypatch, shop, client):
    await set_verify({"inquiry": {"max_queries": 1}})
    plays = {"排除测试账号": Play([NO_TEST, QUERY], NO_TEST_ANSWER)}
    models = Models(monkeypatch, shop, plays=plays)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["limit"]) == (True, "max_queries"), "触顶照样按查到的结果收尾"
    # 没指明句子：任务里给报告正文，不给哪一句的摘录
    [task] = models.tasks
    assert "提问人没有指明是哪一句" in task and f"报告正文：\n{EAST}{SOUTH}" in task and "原报告" not in task
    assert [q["sql"] for q in item["queries"]] == [NO_TEST]
    [event] = await events(paused.id, "approval.inquiry")
    assert (event.data["ok"], event.data["limit"]) == (True, "max_queries")


async def test_an_unavailable_model_is_recorded_as_a_failure(monkeypatch, shop, client):
    Models(monkeypatch, shop, broken=ProviderNotConfigured("没有配置模型接入"))
    # 写作者照常用演示模型：只有质询 agent 的模型用不了
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True
    answer = body["inquiry"]["answer"]
    assert answer["ok"] is False and answer["text"] == "" and answer["doc_artifact"] is None
    assert answer["error"] and "模型" in answer["error"]
    assert body["inquiry"]["queries"] == [] and body["inquiry"]["usage"]["calls"] == 0
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["ok"] is False and event.data["error"] == answer["error"]


async def test_an_answer_that_cannot_be_put_together_is_recorded_as_a_failure(monkeypatch, shop, client):
    """回答原文没能组装成文档：原文里的引用没换成值、遮罩也没做，不交出去；查过的、花掉的照记。"""
    Models(monkeypatch, shop)

    def broken(*args, **kwargs):
        raise RuntimeError("文档结构异常")

    monkeypatch.setattr(inquiry_mod, "compose_doc", broken)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["text"], item["answer"]["doc_artifact"]) == (False, "", None)
    assert item["answer"]["error"].startswith("整理回答时出错")
    assert [q["sql"] for q in item["queries"]] == [NO_TEST] and item["usage"]["calls"] >= 2
    assert r.json()["recorded"] is True
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["ok"] is False and event.data["usage"] == item["usage"]


async def test_the_daily_cap_is_recorded_as_a_limit(monkeypatch, shop, client):
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 5.0, "calls": 3}))
        await session.commit()
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    answer = r.json()["inquiry"]["answer"]
    assert (answer["ok"], answer["limit"]) == (False, "daily_max_usd") and answer["error"]
    assert models.finals == []
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["limit"] == "daily_max_usd"


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


async def test_listing_filters_by_report_and_sentence_and_artifacts_are_whitelisted(monkeypatch, shop, client):
    plays = {"排除测试账号": Play([NO_TEST], NO_TEST_ANSWER), "华南为什么": Play([], "查不到判断所需的数据。")}
    Models(monkeypatch, shop, plays=plays)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    doc = await report_doc(paused.id)
    east, south = unit_of(doc, EAST), unit_of(doc, SOUTH)
    first = (await ask(client, paused.id, report="write", unit=east, question=QUESTION,
                       approval_id=approval.id)).json()["inquiry"]
    second = (await ask(client, paused.id, report="write", unit=south, question="华南为什么低？",
                        approval_id=approval.id)).json()["inquiry"]
    assert first["seq"] < second["seq"]

    listed = await client.get(f"/api/runs/{paused.id}/inquiries")
    assert listed.json() == {"items": [first, second]}
    assert (await client.get(f"/api/runs/{paused.id}/inquiries", params={"report": "write", "unit": south})
            ).json() == {"items": [second]}
    assert (await client.get(f"/api/runs/{paused.id}/inquiries", params={"report": "other"})).json() == {"items": []}
    assert (await client.get("/api/runs/no-such-run/inquiries")).status_code == 404

    [query] = first["queries"]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == query["artifact"] and got.json()["masked"] == []
    assert got.json()["content"]["rows"] == [["华东", 1200], ["华南", 900], ["华北", 400]]
    # 回答文档也是质询引用过的工件
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{first['answer']['doc_artifact']}")
    assert got.status_code == 200 and got.json()["content"]["markdown"] == NO_TEST_TEXT
    # 报告自己的查询快照不是质询引用过的
    [fetched] = [e for e in await events(paused.id, "tool.end", "fetch")]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{fetched.data['query_artifact']}")
    assert got.status_code == 404
    assert (await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/nothing")).status_code == 404


# --------------------------------------------------------------------------
# 重放
# --------------------------------------------------------------------------


async def test_resuming_after_an_inquiry_does_not_repeat_the_nodes(monkeypatch, shop, client):
    """质询不发 tool.start（_stale_replay 只看它），批准后从断点继续：取数、报告都不再执行一遍。"""
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.json()["recorded"] is True
    assert [e for e in await events(paused.id, "tool.start") if e.node_id is None] == []
    await client.post(f"/api/approvals/{approval.id}/decide", json={"approved": True})
    final = await wait(paused.id)
    assert final.status == "succeeded", final.error
    for node_id in ("fetch", "write"):
        assert len(await events(paused.id, "node.started", node_id)) == 1, f"{node_id} 又执行了一遍"
    assert len(await events(paused.id, "tool.start", "fetch")) == 1
    assert models.writes == 1
