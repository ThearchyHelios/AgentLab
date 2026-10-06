"""审批质询（engine/inquiry.py、api/inquiry.py）：运行停在人工审批时，审批人针对报告里的一句话提问，一个只读 agent
现查现答；问答写进这次运行（approval.inquiry 事件），随运行结束一起封存，报告本身一个字不改。

- 停在审批时质询：事件 node_id 为空、数据按前后端约定的形状；质询的查询工件只在 inquiry_artifacts / queries 里，
  不在 query_artifact 下（证据接口不把它们当成报告的证据）；批准、运行结束封存之后 verify 一致，封存范围包含这条事件；
  用量计入运行用量
- 回答期间运行被批准（竞态）：回答照样交回，recorded 为假、note 说明，事件不落库，回答里的工件照样取得到；两道审批时
  问的是第一张、回答途中运行停到了第二张上，也不写
- 同一运行同时只答一条：第二条质询、审批回复（批准 / 驳回）、继续运行都回 409 inquiry_busy，拒掉的批准不留痕迹
- 封存之后：探索运行可以追问（post_seal，落在封存之后，不计运行用量，回答末尾注明查的是现在的数据），正式运行 409；
  设置里关了 409；没停在那张审批卡上、还在执行（按情形说下一步）、封存核对不通过、开放途中运行被继续了都 409；
  事件里记的报告文档不是这次运行的 422；输入不合法 422，指明是哪一项
- 回答：先给结论，数字只能引用自己查询的格子或报告已有的指标，裸数字被标出（提问人自己写的数不标）；遮罩列（起了
  别名的、和 * 写在一起没起别名的表达式也算）的格子渲染成「已遮罩」，回答正文、查询清单、事件、质询工件里都不出现
  遮罩列的值；换着写法找某一个客户（简单 CASE、IN (VALUES …)、两头相同的范围、行值、选出列里写着同一个号码，两半
  拆到 ON 和 WHERE、WHERE 和 HAVING、INTERSECT 两支、CTE 里，边界、等值写成算式，两头不是整数，范围再排除一头，
  号码前面补零……）也一样，年龄这类短的值换着写法找（套了 CAST、汇总值两头相同、补零、取模和整数相除算出来的边界……）
  SQL 里也遮；遮罩值不误伤阈值、天数、LIMIT（长的数也一样）、提问人自己写的数、报告口径卡里的指标，分档共用的边界、
  和日期、天数修饰、标签里写着的同一个数也不牵连阈值，分档的文字标签不当遮罩值（当分组键的档位列照常显示），改了
  名字的遮罩列、UNION 另一支取的遮罩列撞上标签照样遮；SQL 里照原样留着的阈值只在回答正文里不遮，格子照旧按值遮，
  回答和质询工件对得上；质询工件按值遮只出现在报告快照里的遮罩值；一格文字里夹着遮罩值时回答整份不交，质询工件里
  只换掉那一截；质询工件里 SQL 检查结果的片段、说明照整条 SQL 换（短的数也换），给模型的那句改法不交出去
- 接线：设置里的上限、运行固定的数据版本交到 agent 手里（上传表格重传之后查的仍是运行固定的那一版）；同名重建的
  数据源、没固定过版本的上传表格不查
- 触顶、失败照样记一条事件，说法给提问人看得懂、说下一步；轮数用完不算查询次数到顶，查询失败、引用解析不了在末尾注明
- 读取：列表按报告、句子筛选，按时间排；质询工件只认质询引用过的（含回答里引用的口径卡），遮罩照回答时的办法
- 恢复后的重放不受影响：前面的节点不重复执行

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件和上传表格，区域、金额、手机号、年龄都是虚构的。
"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import timezone

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.core.config import settings
from app.data import introspect, table_versions
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Approval, Artifact, DataSource, Run, RunEvent, Setting
from app.engine import compiler
from app.engine import inquiry as inquiry_mod
from app.engine import spend, verify_agent
from app.engine.context import NodeError
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager, verify_manifest
from app.engine.schema import NodeType
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
#: 封存之后的追问查的是数据源现在的数据：回答末尾的说明
LIVE_NOTE = "注：以上回答依据的是数据源当前的数据，晚于报告生成的时间；与报告不一致时，可能是报告生成之后数据有更新。"
#: 模型用完了调用轮数、有查询执行失败、有引用解析不了：回答末尾的说明
ROUNDS_NOTE = "注：调用模型的轮数已用完，以上回答只依据已经查到的结果，可能不完整。"
FAILED_NOTE = "注：有 {n} 次查询执行失败（查询清单只列成功的查询），以上回答可能缺少依据。"
UNRESOLVED_NOTE = "注：回答中有 {n} 处引用无法解析，相关的数没有出处。"


async def _clean_settings() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(
            [spend.SPEND_KEY, JUDGE_SPEND_KEY, "verify", "judge", "copilot"])))
        await session.commit()


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    await _clean_settings()
    yield
    await run_manager.shutdown()
    # 这里写过的核查设置、花费账不留给同一进程里后面的测试文件
    await _clean_settings()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


#: 四笔订单：华东两笔（其中一笔 300 是测试账号）、华南一笔 900、华北一笔 400
SALES = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER, is_test INTEGER);"
         "INSERT INTO sales VALUES (1, '华东', '13900000001', 1200, 0), (2, '华东', '13900000002', 300, 1),"
         " (3, '华南', '13900000003', 900, 0), (4, '华北', '13900000004', 400, 0);")


async def _source(tmp_path, *, options=None, name: str | None = None, script: str = SALES) -> tuple[str, int]:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(script)
    db.commit()
    db.close()
    name = name or f"shop_{uuid.uuid4().hex[:8]}"
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


def fetching(source: str, sql: str) -> dict:
    """取数（给定的 SQL）→ 报告 → 人工审批 → 成果。"""
    return chain(node("start", "input"), node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
                 node("write", "report", "写周报", instructions="写周报"),
                 node("gate", "human", mode="approve", title="周报可以出具吗"),
                 node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))


@dataclass
class Play:
    """质询 agent 对某个问题的剧本：先看几次表结构（schema），再依次执行的查询，最后交的回答。final 给了就照它交
    （模型不按结构交的样子），不给就交 {rationale: answer}。"""

    queries: list[str] = field(default_factory=list)
    answer: str = "查不到判断所需的数据。"
    schema: int = 0
    final: dict | None = None


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
                    final = play.final if play.final is not None else {"rationale": play.answer}
                    return AIMessage(content=json.dumps(final, ensure_ascii=False))
                if not any(isinstance(m, ToolMessage) for m in messages):
                    self.tasks.append(task)
                answered = [str(m.tool_call_id) for m in messages if isinstance(m, ToolMessage)]
                looked = sum(1 for i in answered if i.startswith("is-"))
                if looked < play.schema:
                    call = {"name": f"db_schema__{source}", "args": {}, "id": f"is-{looked}"}
                    return AIMessage(content="先看表结构。", tool_calls=[call])
                done = sum(1 for i in answered if i.startswith("iq-"))
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
    # 运行的工件列表里认得出是哪个问题的回答
    async with SessionLocal() as session:
        stored = await session.get(Artifact, (answer["doc_artifact"], paused.id))
    assert stored.kind == "inquiry_answer" and stored.meta == {"report": "write", "unit": east, "question": QUESTION}
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
    verified = (await client.get(f"/api/runs/{paused.id}/verify")).json()
    assert verified["ok"] is True and verified["sealed_at"] == final.manifest_seq >= event.seq
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
    # 没写进记录，提问人手里的回答照样点得开：回答文档和查询快照都在质询工件接口上
    [query] = body["inquiry"]["queries"]
    for artifact in (body["inquiry"]["answer"]["doc_artifact"], query["artifact"]):
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{artifact}")
        assert got.status_code == 200, got.text
    assert got.json()["content"]["sql"] == NO_TEST

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
        for decision in ({"approved": True}, {"approved": False, "note": "先别批"}):
            refused = await client.post(f"/api/approvals/{approval.id}/decide", json=decision,
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
    models = Models(monkeypatch, shop)
    done = await finish(shop)
    assert done.status == "succeeded", done.error
    east = unit_of(await report_doc(done.id), EAST)

    r = await ask(client, done.id, report="write", unit=east, question=QUESTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is True and body["note"] is None
    item = body["inquiry"]
    assert item["post_seal"] is True and item["approval_id"] is None
    assert item["answer"]["text"] == f"{NO_TEST_TEXT}\n\n{LIVE_NOTE}", "封存之后查的是现在的数据，回答里交代清楚"
    [task] = models.tasks
    ended = done.finished_at.astimezone(timezone.utc).replace(tzinfo=None) if done.finished_at.tzinfo \
        else done.finished_at
    assert f"报告生成于 {ended.isoformat(timespec='seconds')}（UTC）" in task
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


async def test_a_follow_up_is_not_written_once_the_run_is_continued(monkeypatch, shop, client):
    """失败的探索运行也封存了，可以追问。追问途中运行被继续了（另一个进程、或者抢在登记之前）：记录上的封存不再是
    开始回答时的那份，回答照样交回，不写进运行记录；继续运行跑完重新封存，核对照旧一致。"""
    state = {"fail": True, "continued": False}
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def flaky(st, ctx):
        if ctx.node.id == "boom" and state["fail"]:
            raise NodeError(ctx.node.id, "下游接口暂时不可用")
        return await original(st, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, flaky)

    async def continue_meanwhile():
        if not state["continued"]:
            state["continued"], state["fail"] = True, False
            await run_manager.continue_failed(state["run"])

    Models(monkeypatch, shop, before=continue_meanwhile)
    graph = chain(node("start", "input"), node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("boom", "transform", mode="template", template="门店 17 家"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    failed = await finish(shop, graph=graph)
    assert failed.status == "failed" and failed.manifest_seq is not None
    state["run"] = failed.id

    r = await ask(client, failed.id, report="write", unit=None, question=QUESTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"] is False and body["note"] == NOT_RECORDED
    assert body["inquiry"]["post_seal"] is True
    assert body["inquiry"]["answer"]["text"] == f"{NO_TEST_TEXT}\n\n{LIVE_NOTE}"
    final = await wait(failed.id)
    assert final.status == "succeeded", final.error
    assert await events(failed.id, "approval.inquiry") == []
    assert (await verify_manifest(failed.id))["ok"] is True


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
    ({"report": "write", "unit": 0, "question": QUESTION}, "unit"),
    ({"report": "write", "unit": None, "question": QUESTION, "approval_id": 7}, "approval_id"),
    ({"report": "write", "unit": None, "question": QUESTION, "approval_id": False}, "approval_id"),
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
    assert f"设了遮罩的列（查询里不要选出原值、不要起别名，回答里不要写出这些列的值）：「{masked_shop}」的 buyer_phone" \
        in task
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
    # 回答里的指标点得开：口径卡从质询工件接口取（界面按 doc.catalog[alias].artifact 取）
    card = artifact_store.load(answer["doc_artifact"])["catalog"]["m:gmv"]["artifact"]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{card}")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["metrics"][0]["id"] == "gmv" and got.json()["masked"] == []


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
    assert (answer["ok"], answer["limit"]) == (False, "daily_max_usd")
    assert answer["error"] == "今日核查花费已达上限（核查每日金额上限 $2.00），未能给出回答，可在设置的「核查」一节调高上限"
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
    # 署名照请求头记（前端按 encodeURIComponent 编码后发）
    first = (await client.post(f"/api/runs/{paused.id}/inquiries", headers={"X-Actor": "%E5%BC%A0%E5%B7%A5"},
                               json={"report": "write", "unit": east, "question": QUESTION,
                                     "approval_id": approval.id})).json()["inquiry"]
    assert first["actor"] == "张工"
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


# --------------------------------------------------------------------------
# 遮罩：改了名字的遮罩列、只出现在报告里的遮罩值、不误伤别的数
# --------------------------------------------------------------------------

ALIASED = "SELECT buyer_phone AS 手机号, amount AS 金额 FROM sales ORDER BY amount DESC LIMIT 1"


async def test_a_masked_column_under_another_name_is_masked_everywhere(monkeypatch, masked_shop, client):
    """遮罩列起了中文别名（本仓库查询的惯例）：回答里引用到的格子、事件、回答文档、质询工件里都不露出原值。
    报告自己的查询没选手机号，原值哪里都认不出来，只能按 SQL 认出「手机号」就是 buyer_phone。"""
    plays = {"买得最多": Play([ALIASED], "这一笔来自 [[v:Q1.r0.手机号]]，金额 [[v:Q1.r0.金额]]。[[see:Q1]]")}
    models = Models(monkeypatch, masked_shop, plays=plays)
    paused = await pause(masked_shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="买得最多的是哪位客户？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    assert "1390000000" not in r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "这一笔来自 已遮罩，金额 1,200。"
    [query] = item["queries"]
    assert query["sql"] == ALIASED, "SQL 里没有遮罩值，原样"
    [event] = await events(paused.id, "approval.inquiry")
    assert "1390000000" not in json.dumps(event.data, ensure_ascii=False)
    assert "1390000000" not in json.dumps(artifact_store.load(item["answer"]["doc_artifact"]), ensure_ascii=False)
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200 and "1390000000" not in got.text
    assert got.json()["content"]["rows"] == [["已遮罩", 1200]] and got.json()["masked"] == ["手机号"]
    [task] = models.tasks
    assert "不要起别名" in task


async def test_a_masked_value_seen_only_in_the_report_is_masked_alike_everywhere(monkeypatch, masked_shop, client):
    """遮罩值只出现在报告自己的查询里（agent 照着它写了查询条件、在回答里复述）：回答、事件里的 SQL、质询工件里的
    SQL 都换成「已遮罩」，两处给的 SQL 一字不差。"""
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{masked_shop}",
                       args={"sql": "SELECT buyer_phone, amount AS 金额 FROM sales ORDER BY amount DESC"}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    asked = "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '13900000003'"
    plays = {"这位客户": Play([asked], "号码 13900000003 的客户共 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    Models(monkeypatch, masked_shop, writer="金额最高的一笔 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    paused = await pause(masked_shop, graph=graph)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="这位客户还买过别的吗？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    assert "1390000000" not in r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "号码 已遮罩 的客户共 1 笔。"
    [query] = item["queries"]
    assert query["sql"] == "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '已遮罩'"
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200 and "1390000000" not in got.text
    assert got.json()["content"]["sql"] == query["sql"], "质询工件给的 SQL 和事件里的一字不差"


#: 年龄设了遮罩的订单：年龄都是两位数，和阈值、日期、LIMIT 里的数撞在一起
AGED = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, age INTEGER, amount INTEGER, paid_on TEXT);"
        "INSERT INTO sales VALUES (1, '华东', 25, 1200, '2026-01-25'), (2, '华东', 30, 300, '2026-01-20'),"
        " (3, '华南', 20, 900, '2026-02-01'), (4, '华北', 31, 400, '2026-02-03');")


async def test_numbers_that_merely_match_a_short_masked_value_are_left_alone(monkeypatch, tmp_path, client):
    """遮罩列是年龄：阈值（300）、天数（30）、日期、LIMIT 里的数撞上年龄也不换，封存的 SQL 就是实际执行的那条；
    直接和年龄比较的值照样遮。提问人自己写的阈值复述在回答里，不算没有出处。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "age"}, script=AGED)
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{name}",
                       args={"sql": "SELECT region AS 区域, age, amount AS 金额 FROM sales ORDER BY id"}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    recent = "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount > 300 AND paid_on >= '2026-01-25' LIMIT 20"
    same_age = "SELECT COUNT(*) AS 人数 FROM sales WHERE age = 25"
    plays = {"不超过 300": Play([recent, same_age],
                                "排除金额不超过 300 元的订单后，近 30 天还剩 [[v:Q1.r0.笔数]] 笔，其中 [[v:Q2.r0.人数]] 笔"
                                "来自同一年龄段。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=graph)
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="排除金额不超过 300 元的订单会怎样？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert item["answer"]["text"] == "排除金额不超过 300 元的订单后，近 30 天还剩 3 笔，其中 1 笔来自同一年龄段。"
        doc = artifact_store.load(item["answer"]["doc_artifact"])
        assert [s["text"] for s in iter_segments(doc) if s.get("issue") == "uncited_number"] == ["30"], \
            "问题里的 300 是提问人给的，不标；30 没有出处，照标"
        assert [q["sql"] for q in item["queries"]] == [recent, "SELECT COUNT(*) AS 人数 FROM sales WHERE age = 已遮罩"]
    finally:
        await engines.invalidate(source_id)


async def test_a_threshold_matching_numbers_left_alone_elsewhere_is_sealed_as_written(monkeypatch, tmp_path, client):
    """遮罩列是年龄：阈值 30、31 和天数修饰、日期、年龄段标签里写着的同一个数撞在一起。那些地方一个字没换，阈值也
    照原样封存（回答正文本来就写着「30 岁」），质询工件的 SQL 和事件里一字不差；找某一个年龄的等值照样遮，同一条
    SQL 里的阈值也跟着遮——留着就把等值里换掉的露了。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "age"}, script=AGED)
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{name}",
                       args={"sql": "SELECT region AS 区域, age, amount AS 金额 FROM sales ORDER BY id"}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    recent = "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 30 AND paid_on >= DATE('2026-02-03', '-30 days')"
    older = "SELECT COUNT(*) AS 人数 FROM sales WHERE age > 31 AND paid_on <= '2026-01-31'"
    # 规格原句：按年龄分档的标签里写着同样的 30。标签是 agent 写在 SQL 里的字，不是遮罩值，阈值和标签都照原样
    bands = ("SELECT CASE WHEN age < 30 THEN '30岁以下' ELSE '30岁及以上' END AS 年龄段, COUNT(*) AS 人数 FROM sales "
             "GROUP BY 1 ORDER BY 1")
    exact = "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 30 AND age = 30"
    plays = {"年龄大的": Play([recent, older, bands, exact],
                            "近 30 天、30 岁及以上的有 [[v:Q1.r0.人数]] 人，31 岁以上、1 月 31 日前付款的有 "
                            "[[v:Q2.r0.人数]] 人；[[v:Q3.r0.年龄段]]的有 [[v:Q3.r0.人数]] 人。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=graph)
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="近期年龄大的客户多不多？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert item["answer"]["text"] == ("近 30 天、30 岁及以上的有 2 人，31 岁以上、1 月 31 日前付款的有 0 人；"
                                          "30岁以下的有 2 人。")
        sealed = [recent, older, bands, "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 已遮罩 AND age = 已遮罩"]
        assert [q["sql"] for q in item["queries"]] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        assert [q["sql"] for q in event.data["queries"]] == sealed
        for query, sql in zip(item["queries"], sealed):
            got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
            assert got.status_code == 200 and got.json()["content"]["sql"] == sql, "质询工件的 SQL 和事件里的一字不差"
    finally:
        await engines.invalidate(source_id)


@pytest.mark.parametrize("sql, sealed", [
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE CAST(age AS INTEGER) = 25",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE CAST(age AS INTEGER) = 已遮罩", id="套了 CAST"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales GROUP BY age HAVING MAX(age) BETWEEN 25 AND 25",
                 "SELECT COUNT(*) AS 人数 FROM sales GROUP BY age HAVING MAX(age) BETWEEN 已遮罩 AND 已遮罩",
                 id="汇总值两头相同"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age = 25.0",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age = 已遮罩", id="补零的写法"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age > 24.5 AND age < 25.5",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age > 已遮罩 AND age < 已遮罩", id="两头不是整数"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 25 AND age <= 26 AND age <> 26",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 已遮罩 AND age <= 26 AND age <> 已遮罩",
                 id="范围再排除一头"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age BETWEEN 20 + 5 AND 30 - 5",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age BETWEEN 已遮罩 + 已遮罩 AND 已遮罩 - 已遮罩",
                 id="算出来的边界"),
    # 第 3 轮复核：取模、整数相除（SQLite 里只留整数部分）算出来的边界
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 25 AND age <= 125 % 100",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 已遮罩 AND age <= 已遮罩 % 已遮罩",
                 id="取模算出来的边界"),
    pytest.param("SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 25 AND age < 53 / 2",
                 "SELECT COUNT(*) AS 人数 FROM sales WHERE age >= 已遮罩 AND age < 已遮罩 / 已遮罩",
                 id="整数相除算出来的边界"),
])
async def test_a_short_masked_value_looked_up_in_other_shapes_is_sealed_masked(monkeypatch, tmp_path, client, sql,
                                                                                 sealed):
    """年龄设了遮罩，agent 换着写法找某一个年龄（套了 CAST、汇总值两头相同、补零、两头不是整数、范围再排除一头、
    算出来的边界，取模、整数相除算出来的边界）：第 2、3 轮复核时都照原样封存。现在查询清单、事件、质询工件里的 SQL
    都换掉这个年龄，三处一字不差。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "age"}, script=AGED)
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{name}",
                       args={"sql": "SELECT region AS 区域, age, amount AS 金额 FROM sales ORDER BY id"}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    plays = {"这个年龄": Play([sql], "这个年龄的有 [[v:Q1.r0.人数]] 人。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=graph)
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="这个年龄的客户有几位？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "这个年龄的有 1 人。")
        [query] = item["queries"]
        assert query["sql"] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        assert event.data["queries"][0]["sql"] == sealed
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200 and got.json()["content"]["sql"] == sealed, "质询工件的 SQL 和事件里的一字不差"
    finally:
        await engines.invalidate(source_id)


#: 会员等级设了遮罩：等级是 1、2、3 这样的短码值，正好撞上按条件数个数写的 THEN 1 ELSE 0
LEVELS = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, vip_level INTEGER, amount INTEGER);"
          "INSERT INTO sales VALUES (1, '华东', 1, 1200), (2, '华东', 2, 300), (3, '华南', 3, 900),"
          " (4, '华北', 1, 1500);")


async def test_a_renamed_masked_column_keeps_values_that_match_labels_written_elsewhere(monkeypatch, tmp_path, client):
    """第 2 轮复核：先按等级数大单（vip_level AS 等级，SUM(CASE … THEN 1 ELSE 0 END)），再找等级为 1、2 的。以前分档标签
    1、0 不收进遮罩值，改了名字的等级列也跟着不收，WHERE vip_level = 1 照原样封存，= 2 却遮了。现在只有只装着标签的
    列不收，两条都遮；按等级数大单的那份快照里，等级整列遮，大单数照常显示。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "vip_level"}, script=LEVELS)
    by_level = ("SELECT vip_level AS 等级, SUM(CASE WHEN amount > 1000 THEN 1 ELSE 0 END) AS 大单数 FROM sales "
                "GROUP BY vip_level ORDER BY vip_level")
    first, second = (f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}" for level in (1, 2))
    plays = {"各等级": Play([by_level, first, second],
                         "大单最多的等级有 [[v:Q1.r0.大单数]] 笔大单；问到的两个等级各有 [[v:Q2.r0.人数]] 人、"
                         "[[v:Q3.r0.人数]] 人。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, AMOUNTS))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="各等级的大单多不多？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (
            True, "大单最多的等级有 2 笔大单；问到的两个等级各有 2 人、1 人。")
        sealed = [by_level, *["SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩"] * 2]
        assert [q["sql"] for q in item["queries"]] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        assert [q["sql"] for q in event.data["queries"]] == sealed
        for query, sql in zip(item["queries"], sealed):
            got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
            assert got.status_code == 200 and got.json()["content"]["sql"] == sql, "质询工件的 SQL 和事件里的一字不差"
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{item['queries'][0]['artifact']}")
        assert got.json()["content"]["rows"] == [["已遮罩", 2], ["已遮罩", 0], ["已遮罩", 0]]
        assert got.json()["masked"] == ["等级"]
    finally:
        await engines.invalidate(source_id)


@pytest.mark.parametrize("mixed", [
    pytest.param("SELECT CASE WHEN amount > 1000 THEN 1 ELSE 0 END AS 等级 FROM sales UNION ALL "
                 "SELECT vip_level FROM sales", id="标签在前、遮罩列在后"),
    pytest.param("SELECT CASE WHEN amount > 1000 THEN 1 ELSE 0 END AS 等级 FROM sales UNION ALL "
                 "SELECT b.vip_level AS 等级 FROM (SELECT vip_level FROM sales) b", id="遮罩列经子查询按原名传出"),
])
async def test_a_masked_column_after_a_label_in_a_union_keeps_its_values(monkeypatch, tmp_path, client, mixed):
    """第 3 轮复核：UNION 的第一支是按条件数大单的标签（THEN 1 ELSE 0），另一支取的是等级（遮罩列原样选出，或者经按
    原名选出的子查询、以标签的别名传出）。以前这一列被当成只装着标签，等级 1 撞上标签 1 不收，之后 WHERE vip_level = 1
    在查询清单、事件、质询工件里都照原样，= 2 却遮了。现在这一列在一支里取的是遮罩列，就照收，两条都遮。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "vip_level"}, script=LEVELS)
    first, second = (f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}" for level in (1, 2))
    plays = {"各等级": Play([mixed, first, second],
                         "问到的两个等级各有 [[v:Q2.r0.人数]] 人、[[v:Q3.r0.人数]] 人。[[see:Q2]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, AMOUNTS))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="各等级的客户有几位？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "问到的两个等级各有 2 人、1 人。")
        sealed = [mixed, *["SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩"] * 2]
        assert [q["sql"] for q in item["queries"]] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        assert [q["sql"] for q in event.data["queries"]] == sealed
        for query, sql in zip(item["queries"], sealed):
            got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
            assert got.status_code == 200 and got.json()["content"]["sql"] == sql, "质询工件的 SQL 和事件里的一字不差"
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{item['queries'][0]['artifact']}")
        assert {cell for [cell] in got.json()["content"]["rows"]} == {"已遮罩"} and got.json()["masked"] == ["等级"]
    finally:
        await engines.invalidate(source_id)


_BIG_ONE = "CASE WHEN amount > 1000 THEN 1 ELSE 0 END"


@pytest.mark.parametrize("shadowed", [
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT *, {_BIG_ONE} AS vip_level FROM sales) t", id="* 在前"),
    pytest.param(f"SELECT x.vip_level AS 档 FROM (SELECT * FROM sales s JOIN (SELECT id AS bid, {_BIG_ONE} AS vip_level "
                 "FROM sales) b ON s.id = b.bid) x", id="JOIN 子查询 * 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT (vip_level), {_BIG_ONE} AS vip_level FROM sales) t",
                 id="(col) 在前"),
    pytest.param(f"SELECT vip_level AS 档 FROM (SELECT vip_level COLLATE NOCASE, {_BIG_ONE} AS vip_level FROM sales) t",
                 id="COLLATE 在前"),
])
async def test_a_masked_column_shadowing_a_label_keeps_its_values(monkeypatch, tmp_path, client, shadowed):
    """第 3 轮复验（J2 留下的缺口）：派生来源里 * 带出的等级、套了括号或 COLLATE 的等级排在同名的标签前面，SQLite 交回
    的是真实等级。以前这一列被当成只装着标签，等级 1 撞上标签 1 不收，之后 WHERE vip_level = 1 在查询清单、事件、质询
    工件里都照原样。现在不算只装着标签的列，两条都遮。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "vip_level"}, script=LEVELS)
    first, second = (f"SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = {level}" for level in (1, 2))
    plays = {"各等级": Play([shadowed, first, second],
                         "问到的两个等级各有 [[v:Q2.r0.人数]] 人、[[v:Q3.r0.人数]] 人。[[see:Q2]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, AMOUNTS))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="各等级的客户有几位？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "问到的两个等级各有 2 人、1 人。")
        sealed = [shadowed, *["SELECT COUNT(*) AS 人数 FROM sales WHERE vip_level = 已遮罩"] * 2]
        assert [q["sql"] for q in item["queries"]] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        assert [q["sql"] for q in event.data["queries"]] == sealed
        for query, sql in zip(item["queries"], sealed):
            got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
            assert got.status_code == 200 and got.json()["content"]["sql"] == sql, "质询工件的 SQL 和事件里的一字不差"
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{item['queries'][0]['artifact']}")
        assert {cell for [cell] in got.json()["content"]["rows"]} == {"已遮罩"} and got.json()["masked"] == ["档"]
    finally:
        await engines.invalidate(source_id)


STAR_TAIL = "SELECT *, SUBSTR(buyer_phone, 4) FROM sales ORDER BY amount DESC LIMIT 1"


async def test_an_unnamed_expression_next_to_a_star_is_masked_everywhere(monkeypatch, masked_shop, client):
    """和 * 写在同一支里、没起别名的表达式（取手机号的后几位）：结果的列名对不上任何名字，按位置认（从末尾倒着数）。
    回答、事件、回答文档、质询工件里都不露出原值的任何一截。"""
    plays = {"买得最多": Play([STAR_TAIL], "金额最高的一笔来自尾号 [[v:Q1.r0.SUBSTR(buyer_phone, 4)]] 的客户，"
                                         "金额 [[v:Q1.r0.amount]]。[[see:Q1]]")}
    Models(monkeypatch, masked_shop, plays=plays)
    paused = await pause(masked_shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="买得最多的是哪位客户？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "金额最高的一笔来自尾号 已遮罩 的客户，金额 1,200。"
    [query] = item["queries"]
    assert query["sql"] == STAR_TAIL
    [event] = await events(paused.id, "approval.inquiry")
    doc = artifact_store.load(item["answer"]["doc_artifact"])
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200, got.text
    for shown in (r.text, json.dumps(event.data, ensure_ascii=False), json.dumps(doc, ensure_ascii=False), got.text):
        assert "00000001" not in shown and "1390000000" not in shown
    assert got.json()["content"]["rows"] == [[1, "华东", "已遮罩", 1200, 0, "已遮罩"]]
    assert got.json()["masked"] == ["buyer_phone", "SUBSTR(buyer_phone, 4)"]


#: 金额设了遮罩的大单：金额都是五位数，和阈值、LIMIT、访问次数撞在一起（第二单的访问次数正好等于第三单的金额）
BIG = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, visits INTEGER);"
       "INSERT INTO sales VALUES (1, '华东', 10000, 25000), (2, '华东', 12000, 20000), (3, '华南', 20000, 30000),"
       " (4, '华南', 30000, 22000), (5, '华北', 50000, 9000);")
#: 报告取数选了金额：金额列里的值都算遮罩值
AMOUNTS = "SELECT region AS 区域, amount AS 金额 FROM sales ORDER BY id"


@pytest.fixture
async def big_shop(tmp_path):
    name, source_id = await _source(tmp_path, options={"mask_columns": "amount"}, script=BIG)
    yield name
    await engines.invalidate(source_id)


async def test_thresholds_and_limits_are_sealed_as_written_even_when_they_match_masked_values(
        monkeypatch, big_shop, client):
    """遮罩列的值都是五位数：范围比较的边界（和金额比的、和访问次数比的）、LIMIT 撞上遮罩值也照原样封存，回答正文里
    复述它们也不遮——它们就摆在 SQL 里。在找某一笔的（amount = 50000）照旧遮。"""
    large = "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount >= 30000 AND visits > 20000 LIMIT 10000"
    exact = "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount = 50000"
    plays = {"大单": Play([large, exact], "金额不低于 30000 元、访问超过 20000 次的大单有 [[v:Q1.r0.笔数]] 笔，"
                                         "恰好 50000 元的有 [[v:Q2.r0.笔数]] 笔。[[see:Q1]]")}
    Models(monkeypatch, big_shop, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    paused = await pause(big_shop, graph=fetching(big_shop, AMOUNTS))
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="3 万元以上的大单有几笔？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "金额不低于 30000 元、访问超过 20000 次的大单有 1 笔，恰好 已遮罩 元的有 1 笔。"
    sealed = [large, "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount = 已遮罩"]
    assert [q["sql"] for q in item["queries"]] == sealed
    [event] = await events(paused.id, "approval.inquiry")
    assert [q["sql"] for q in event.data["queries"]] == sealed
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{item['queries'][0]['artifact']}")
    assert got.status_code == 200 and got.json()["content"]["sql"] == large
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{item['queries'][1]['artifact']}")
    assert got.status_code == 200 and got.json()["content"]["sql"] == sealed[1], "质询工件的 SQL 和事件里的一字不差"
    doc = artifact_store.load(item["answer"]["doc_artifact"])
    assert [s["text"] for s in iter_segments(doc) if s.get("issue") == "uncited_number"] == ["30000", "20000"], \
        "不遮不等于有出处：agent 自己定的阈值照样标「无出处」"


async def test_a_cell_equal_to_a_threshold_in_the_sql_is_masked_alike_in_the_answer_and_the_artifact(
        monkeypatch, big_shop, client):
    """访问次数正好等于 SQL 里照原样留着的阈值（20000，也是某一笔的金额）：正文里复述的阈值不遮，格子里的照旧按值遮
    （col >= v 再取第一行，查出来的可能就是那一笔）。回答和质询工件里这一格一样是「已遮罩」，两边对得上。"""
    least = "SELECT region AS 区域, visits AS 访问 FROM sales WHERE visits >= 20000 ORDER BY visits LIMIT 1"
    plays = {"访问": Play([least], "访问不少于 20000 次的区域里，最少的是 [[v:Q1.r0.区域]]，"
                                   "访问 [[v:Q1.r0.访问]] 次。[[see:Q1]]")}
    Models(monkeypatch, big_shop, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    paused = await pause(big_shop, graph=fetching(big_shop, AMOUNTS))
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="访问多的区域有哪些？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "访问不少于 20000 次的区域里，最少的是 华东，访问 已遮罩 次。"
    [query] = item["queries"]
    assert query["sql"] == least, "阈值、LIMIT 照原样封存"
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["rows"] == [["华东", "已遮罩"]] and got.json()["masked"] == ["访问"]
    assert got.json()["content"]["sql"] == least


async def test_a_metric_from_the_report_that_equals_a_masked_value_can_be_cited(monkeypatch, big_shop, client):
    """报告口径卡里的指标（单笔目标 20000）恰好和遮罩列里的某一笔一样：报告里本来就看得到，引用它的回答照常交出去，
    换个问法也不会被拦；别的列里正好是这个数的格子（第二单访问 20000 次）在回答和质询工件里都照常显示，两边对得上。"""
    metrics = [{"id": "target", "name": "单笔目标", "unit": "元", "expression": "vars.kpi.target"}]
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{big_shop}", args={"sql": AMOUNTS}),
                  node("calc", "transform", mode="expression", expression="{'target': 20000}", assign_to="kpi"),
                  node("caliber", "metrics", caliber="大单口径", caliber_version="v1", metrics=metrics),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    smallest = "SELECT amount AS 金额 FROM sales WHERE amount > 15000 ORDER BY amount LIMIT 1"
    second = "SELECT region AS 区域, visits AS 访问 FROM sales WHERE id = 2"
    plays = {"定得高不高": Play([smallest, second], "单笔目标为 [[m:target]]，达标的最小一笔是 [[v:Q1.r0.金额]]；"
                                                  "[[v:Q2.r0.区域]]第二单访问 [[v:Q2.r0.访问]] 次。[[see:Q1]]")}
    Models(monkeypatch, big_shop, writer="单笔目标 [[m:target]]。[[see:m:target]]", plays=plays)
    paused = await pause(big_shop, graph=graph)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="单笔目标定得高不高？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    answer = r.json()["inquiry"]["answer"]
    assert (answer["ok"], answer["error"]) == (True, None)
    assert answer["text"] == "单笔目标为 20,000元，达标的最小一笔是 已遮罩；华东第二单访问 20,000 次。"
    first, other = r.json()["inquiry"]["queries"]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{other['artifact']}")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["rows"] == [["华东", 20000]] and got.json()["masked"] == []
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{first['artifact']}")
    assert got.json()["content"]["rows"] == [["已遮罩"]] and got.json()["masked"] == ["金额"]


#: 规格原句：按金额分档的文字标签，两个 WHEN 分支共用边界 20000
BANDS = "CASE WHEN amount BETWEEN 0 AND 20000 THEN '小单' WHEN amount BETWEEN 20000 AND 60000 THEN '大单' END"


@pytest.mark.parametrize("sql, answer, text, rows, masked", [
    # 档位当分组键（每一档一行）：标签是 agent 写在 SQL 里的字，不是遮罩值，格子照常显示，正文、SQL 里照原样
    pytest.param(f"SELECT {BANDS} AS 档位, COUNT(*) AS 笔数 FROM sales GROUP BY 1 ORDER BY 1",
                 "以 20000 元为界，[[v:Q1.r0.档位]] [[v:Q1.r0.笔数]] 笔，[[v:Q1.r1.档位]] [[v:Q1.r1.笔数]] 笔，"
                 "小单更多。[[see:Q1]]",
                 "以 20000 元为界，大单 2 笔，小单 3 笔，小单更多。", [["大单", 2], ["小单", 3]], [], id="两个 WHEN 分支"),
    pytest.param("SELECT COUNT(CASE WHEN amount <= 20000 THEN 1 END) AS 小单, "
                 "COUNT(CASE WHEN amount >= 20000 THEN 1 END) AS 大单 FROM sales",
                 "不超过 20000 元的有 [[v:Q1.r0.小单]] 笔，不低于 20000 元的有 [[v:Q1.r0.大单]] 笔。[[see:Q1]]",
                 "不超过 20000 元的有 3 笔，不低于 20000 元的有 3 笔。", [[3, 3]], [], id="两个选出列"),
    # 规格原句的 SUM(CASE …)：边界照原样封存、正文里不遮；汇总的是写死的 1 和 0，就是按条件数个数（同 COUNT），
    # 格子照常显示
    pytest.param("SELECT SUM(CASE WHEN amount <= 20000 THEN 1 ELSE 0 END) AS 小单, "
                 "SUM(CASE WHEN amount >= 20000 THEN 1 ELSE 0 END) AS 大单 FROM sales",
                 "以 20000 元为界：不超过的 [[v:Q1.r0.小单]] 笔，不低于的 [[v:Q1.r0.大单]] 笔。[[see:Q1]]",
                 "以 20000 元为界：不超过的 3 笔，不低于的 3 笔。", [[3, 3]], [], id="两个 SUM(CASE…) 选出列"),
    pytest.param("SELECT '小单' AS 档位, COUNT(*) AS 笔数 FROM sales WHERE amount <= 20000 "
                 "UNION ALL SELECT '大单', COUNT(*) FROM sales WHERE amount >= 20000",
                 "不超过 20000 元的有 [[v:Q1.r0.笔数]] 笔，不低于 20000 元的有 [[v:Q1.r1.笔数]] 笔。[[see:Q1]]",
                 "不超过 20000 元的有 3 笔，不低于 20000 元的有 3 笔。", [["小单", 3], ["大单", 3]], [], id="UNION 两支"),
])
async def test_band_edges_shared_by_separate_conditions_are_sealed_and_shown_as_written(
        monkeypatch, big_shop, client, sql, answer, text, rows, masked):
    """金额设了遮罩，分档的边界 20000 正好是某一笔的金额：两个 WHEN 分支、两个选出列、UNION 两支各写一边，各是各的
    条件，没有夹住哪一笔。SQL 照原样封存，回答正文里复述的边界、档位标签也不遮，质询工件的 SQL 和事件里一字不差。"""
    plays = {"两万元上下": Play([sql], answer)}
    Models(monkeypatch, big_shop, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    paused = await pause(big_shop, graph=fetching(big_shop, AMOUNTS))
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="两万元上下各有几笔？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["text"]) == (True, text)
    [query] = item["queries"]
    assert query["sql"] == sql
    [event] = await events(paused.id, "approval.inquiry")
    assert event.data["queries"][0]["sql"] == sql
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
    assert got.status_code == 200 and got.json()["content"]["sql"] == sql, "质询工件的 SQL 和事件里的一字不差"
    assert (got.json()["content"]["rows"], got.json()["masked"]) == (rows, masked)


async def test_a_masked_value_the_asker_wrote_stays_in_the_sql(monkeypatch, tmp_path, client):
    """提问人自己写的数恰好是遮罩列里的值（金额设了遮罩，问的就是 300 那一笔）：问题里原样写着，SQL 里和金额做等值
    比较也不换，事件、质询工件里封存的就是实际执行的那条。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "amount"})
    without = "SELECT COUNT(*) AS 笔数 FROM sales WHERE amount <> 300"
    plays = {"300 的那笔": Play([without], "去掉金额为 300 的那笔后还剩 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, AMOUNTS))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="去掉金额为 300 的那笔会怎样？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert item["answer"]["text"] == "去掉金额为 300 的那笔后还剩 3 笔。"
        [query] = item["queries"]
        assert query["sql"] == without
        [event] = await events(paused.id, "approval.inquiry")
        assert event.data["queries"][0]["sql"] == without
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200 and got.json()["content"]["sql"] == without
    finally:
        await engines.invalidate(source_id)


#: 另有一张联系方式表：同一个手机号写在没设遮罩的列里
CONTACTS = SALES + ("CREATE TABLE contacts (id INTEGER PRIMARY KEY, phone_num TEXT, visits INTEGER);"
                    "INSERT INTO contacts VALUES (1, '13900000001', 5), (2, '13900000003', 2);")


async def test_a_value_seen_only_in_the_reports_snapshot_is_masked_in_the_artifact_too(monkeypatch, tmp_path, client):
    """agent 查的表没设遮罩，里面却写着报告快照里遮罩列的值（另一张表里的同一个手机号）：质询工件照回答时的办法收
    遮罩值，报告自己的查询快照也算，按值遮掉。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"}, script=CONTACTS)
    visits = "SELECT phone_num AS 号码, visits AS 次数 FROM contacts ORDER BY id"
    plays = {"常来": Play([visits], "最常来的客户来过 [[v:Q1.r0.次数]] 次。[[see:Q1]]")}
    Models(monkeypatch, name, writer="金额最高的一笔 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        report_sql = "SELECT buyer_phone, amount AS 金额 FROM sales ORDER BY amount DESC"
        paused = await pause(name, graph=fetching(name, report_sql))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="常来的客户来过几次？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        assert r.json()["inquiry"]["answer"]["text"] == "最常来的客户来过 5 次。"
        [query] = r.json()["inquiry"]["queries"]
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200, got.text
        assert got.json()["content"]["rows"] == [["已遮罩", 5], ["已遮罩", 2]]
        assert got.json()["masked"] == ["号码"] and "1390000000" not in got.text
    finally:
        await engines.invalidate(source_id)


#: 另有一张回访记录：备注里写着手机号，备注这一列没设遮罩
CALLBACKS = SALES + ("CREATE TABLE callbacks (id INTEGER PRIMARY KEY, region TEXT, note TEXT);"
                     "INSERT INTO callbacks VALUES (1, '华东', '客户13900000001来电改址'), (2, '华南', '例行回访');")
#: 回答中含有遮罩值、整份不交时给提问人的说法
LEAKED = "回答中含有设了遮罩的值，无法遮住，已不予展示，请换个问法重新提问"


async def test_a_masked_value_inside_a_text_cell_is_cut_out_of_the_artifact(monkeypatch, tmp_path, client):
    """备注里写着手机号（备注没设遮罩，手机号设了遮罩、被报告取数选中）：agent 引用这一格，回答照旧整份不交；
    质询工件里这份快照照样记着、点得开，那一格里的号码换成「已遮罩」，别的字、别的格子照原样，不然「不予展示」
    旁边一点就能看到原值。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"}, script=CALLBACKS)
    notes = "SELECT region AS 区域, note AS 备注 FROM callbacks ORDER BY id"
    plays = {"回访": Play([notes], "华东的回访备注是 [[v:Q1.r0.备注]]。[[see:Q1]]")}
    Models(monkeypatch, name, writer="金额最高的一笔 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        report_sql = "SELECT buyer_phone, amount AS 金额 FROM sales ORDER BY amount DESC"
        paused = await pause(name, graph=fetching(name, report_sql))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="华东的回访说了什么？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        assert "1390000000" not in r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"], item["answer"]["doc_artifact"]) == (False, "", None)
        assert item["answer"]["error"] == LEAKED
        [query] = item["queries"]
        assert query["sql"] == notes
        [event] = await events(paused.id, "approval.inquiry")
        assert event.data["inquiry_artifacts"] == [query["artifact"]]
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200, got.text
        assert got.json()["content"]["rows"] == [["华东", "客户已遮罩来电改址"], ["华南", "例行回访"]]
        assert got.json()["masked"] == [] and "1390000000" not in got.text
    finally:
        await engines.invalidate(source_id)


async def test_sql_check_excerpts_in_the_artifact_are_masked_like_the_sql(monkeypatch, masked_shop, client):
    """agent 的查询带着 SQL 检查结果（对照数据目录查出的问题，片段摘自这条 SQL）：质询工件里片段照查询清单的 SQL
    换掉遮罩值，给模型的那句改法照抄了片段，不交出去（同证据面板）。"""
    from app.tools import datasource

    excerpt = "buyer_phone = '13900000001'"

    async def flagged(checker, sql):
        return [{"code": "wrong_date_column", "level": "warning", "table": "sales", "column": "buyer_phone",
                 "message": "查询按「买家手机号」筛选", "for_model": f"这条 SQL 按 {excerpt} 筛选",
                 "sql_excerpt": excerpt}] if excerpt in sql else []

    monkeypatch.setattr(datasource, "_sql_checks", flagged)
    plays = {"买得最多": Play([TOP_BUYER, BUYER_ORDERS], "这位客户共 [[v:Q2.r0.笔数]] 笔。[[see:Q2]]")}
    Models(monkeypatch, masked_shop, plays=plays)
    paused = await pause(masked_shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="买得最多的客户买过几笔？",
                  approval_id=approval.id)
    assert r.status_code == 200, r.text
    _, orders = r.json()["inquiry"]["queries"]
    got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{orders['artifact']}")
    assert got.status_code == 200, got.text
    assert got.json()["content"]["checks"] == [
        {"code": "wrong_date_column", "level": "warning", "table": "sales", "column": "buyer_phone",
         "message": "查询按「买家手机号」筛选", "sql_excerpt": "buyer_phone = '已遮罩'"}]
    assert got.json()["content"]["sql"] == orders["sql"] and "1390000000" not in got.text


async def test_sql_check_results_in_the_artifact_do_not_show_short_masked_values(monkeypatch, tmp_path, client):
    """检查结果的片段是检查器按语法树重新写出的一段表达式，单独拿出来认不出在找哪一个值；说明里还照引 SQL 写的取值
    （「没有码值「25」」）。年龄设了遮罩：整条 SQL 里换掉的 25，片段、说明里一样换掉，码值表里列出的照原样。"""
    from app.tools import datasource

    name, source_id = await _source(tmp_path, options={"mask_columns": "age"}, script=AGED)
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{name}",
                       args={"sql": "SELECT region AS 区域, age, amount AS 金额 FROM sales ORDER BY id"}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    pinned = "SELECT COUNT(*) AS 人数 FROM sales WHERE age BETWEEN 25 AND 25"
    listed = "（数据目录中已列出的全部码值：18=成年、60=退休）。请核对筛选条件里的取值"

    async def flagged(checker, sql):
        return [{"code": "unknown_code", "level": "warning", "table": "sales", "column": "age",
                 "message": f"「订单」的「年龄」没有码值「25」{listed}", "for_model": "SQL 里写的 25 不在其中",
                 "sql_excerpt": "age BETWEEN 25 AND 25"}] if "BETWEEN" in sql else []

    monkeypatch.setattr(datasource, "_sql_checks", flagged)
    plays = {"这个年龄": Play([pinned], "这个年龄的有 [[v:Q1.r0.人数]] 人。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=graph)
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="这个年龄的客户有几位？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        [query] = r.json()["inquiry"]["queries"]
        assert query["sql"] == "SELECT COUNT(*) AS 人数 FROM sales WHERE age BETWEEN 已遮罩 AND 已遮罩"
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200, got.text
        assert got.json()["content"]["checks"] == [
            {"code": "unknown_code", "level": "warning", "table": "sales", "column": "age",
             "message": f"「订单」的「年龄」没有码值「已遮罩」{listed}", "sql_excerpt": "age BETWEEN 已遮罩 AND 已遮罩"}]
        assert got.json()["content"]["sql"] == query["sql"]
    finally:
        await engines.invalidate(source_id)


#: 同样的四笔订单，手机号存成整数：遮罩值在 SQL 里写成数字
PHONES = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone INTEGER, amount INTEGER, is_test INTEGER);"
          "INSERT INTO sales VALUES (1, '华东', 13900000001, 1200, 0), (2, '华东', 13900000002, 300, 1),"
          " (3, '华南', 13900000003, 900, 0), (4, '华北', 13900000004, 400, 0);")
#: 报告取数选了手机号：遮罩值从报告自己的查询快照里收
WITH_PHONE = "SELECT region AS 区域, buyer_phone, amount AS 金额 FROM sales ORDER BY id"
_COUNT = "SELECT COUNT(*) AS 笔数 FROM sales WHERE "


@pytest.mark.parametrize("script, sql, sealed", [
    # 简单 CASE 里找某一个号码，只数个数（COUNT，或者 SUM 套写死的 1）：号码换掉，笔数照常显示
    pytest.param(PHONES, "SELECT COUNT(CASE buyer_phone WHEN 13900000001 THEN 1 END) AS 笔数 FROM sales",
                 "SELECT COUNT(CASE buyer_phone WHEN 已遮罩 THEN 1 END) AS 笔数 FROM sales", id="简单 CASE"),
    pytest.param(PHONES, _COUNT + "buyer_phone IN (VALUES (13900000001))", _COUNT + "buyer_phone IN (VALUES (已遮罩))",
                 id="IN (VALUES)"),
    pytest.param(PHONES, _COUNT + "buyer_phone BETWEEN 13900000001 AND 13900000001",
                 _COUNT + "buyer_phone BETWEEN 已遮罩 AND 已遮罩", id="BETWEEN 两头相同"),
    pytest.param(PHONES, _COUNT + "buyer_phone >= 13900000001 AND buyer_phone <= 13900000001",
                 _COUNT + "buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩", id="不低于且不高于同一个值"),
    pytest.param(PHONES, _COUNT + "NOT (buyer_phone > 13900000001 OR buyer_phone < 13900000001)",
                 _COUNT + "NOT (buyer_phone > 已遮罩 OR buyer_phone < 已遮罩)", id="既不大于也不小于"),
    pytest.param(PHONES, _COUNT + "buyer_phone >= 13900000001 AND buyer_phone < 13900000002",
                 _COUNT + "buyer_phone >= 已遮罩 AND buyer_phone < 已遮罩", id="整数的半开区间"),
    # 两头都不是那个号码，夹住的是中间那一个：留着 13900000000 就等于写出了号码
    pytest.param(PHONES, _COUNT + "buyer_phone > 13900000000 AND buyer_phone < 13900000002",
                 _COUNT + "buyer_phone > 已遮罩 AND buyer_phone < 已遮罩", id="整数的开区间"),
    pytest.param(PHONES, _COUNT + "region = '华东' AND buyer_phone >= 13900000001 AND buyer_phone <= 13900000001",
                 _COUNT + "region = '华东' AND buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩", id="同一条件里还有别的条件"),
    pytest.param(PHONES, _COUNT + "(buyer_phone, region) = (13900000001, '华东')",
                 _COUNT + "(buyer_phone, region) = (已遮罩, '华东')", id="行值"),
    pytest.param(PHONES, _COUNT + "buyer_phone IS NOT DISTINCT FROM 13900000001",
                 _COUNT + "buyer_phone IS NOT DISTINCT FROM 已遮罩", id="IS NOT DISTINCT FROM"),
    pytest.param(PHONES, _COUNT + "buyer_phone = (13900000001)", _COUNT + "buyer_phone = (已遮罩)", id="套了括号"),
    pytest.param(PHONES, "SELECT COUNT(*) AS 笔数, 13900000001 AS 号码 FROM sales WHERE buyer_phone = 13900000001",
                 "SELECT COUNT(*) AS 笔数, 已遮罩 AS 号码 FROM sales WHERE buyer_phone = 已遮罩", id="选出列里的字面量"),
    pytest.param(SALES, _COUNT + "buyer_phone >= '13900000001' AND buyer_phone <= '13900000001'",
                 _COUNT + "buyer_phone >= '已遮罩' AND buyer_phone <= '已遮罩'", id="字符串范围两头相同"),
    pytest.param(SALES, _COUNT + "buyer_phone BETWEEN '13900000001' AND '13900000001'",
                 _COUNT + "buyer_phone BETWEEN '已遮罩' AND '已遮罩'", id="字符串 BETWEEN 两头相同"),
    # 两半拆到同一行集的不同地方：第 4 轮只在同一个条件里认，号码照原样封存，回答正文里也露了出来
    pytest.param(PHONES, "SELECT COUNT(*) AS 笔数 FROM sales s JOIN sales b ON b.id = s.id "
                 "AND b.buyer_phone >= 13900000001 WHERE b.buyer_phone <= 13900000001",
                 "SELECT COUNT(*) AS 笔数 FROM sales s JOIN sales b ON b.id = s.id "
                 "AND b.buyer_phone >= 已遮罩 WHERE b.buyer_phone <= 已遮罩", id="ON 和 WHERE 各写一边"),
    pytest.param(PHONES, _COUNT + "buyer_phone >= 13900000001 GROUP BY buyer_phone HAVING buyer_phone <= 13900000001",
                 _COUNT + "buyer_phone >= 已遮罩 GROUP BY buyer_phone HAVING buyer_phone <= 已遮罩",
                 id="WHERE 和 HAVING 各写一边"),
    pytest.param(PHONES, "SELECT COUNT(*) AS 笔数 FROM (SELECT id FROM sales WHERE buyer_phone >= 13900000001 "
                 "INTERSECT SELECT id FROM sales WHERE buyer_phone <= 13900000001) t",
                 "SELECT COUNT(*) AS 笔数 FROM (SELECT id FROM sales WHERE buyer_phone >= 已遮罩 "
                 "INTERSECT SELECT id FROM sales WHERE buyer_phone <= 已遮罩) t", id="INTERSECT 两支"),
    pytest.param(PHONES, _COUNT + "buyer_phone >= 13900000001 AND CASE WHEN buyer_phone <= 13900000001 THEN 1 "
                 "ELSE 0 END = 1",
                 _COUNT + "buyer_phone >= 已遮罩 AND CASE WHEN buyer_phone <= 已遮罩 THEN 1 ELSE 0 END = 1",
                 id="一半包在 CASE 里"),
    pytest.param(PHONES, "WITH a AS (SELECT * FROM sales WHERE buyer_phone >= 13900000001) "
                 "SELECT COUNT(*) AS 笔数 FROM a WHERE buyer_phone <= 13900000001",
                 "WITH a AS (SELECT * FROM sales WHERE buyer_phone >= 已遮罩) "
                 "SELECT COUNT(*) AS 笔数 FROM a WHERE buyer_phone <= 已遮罩", id="拆进 CTE"),
    # 整数上相邻的两头把中间那一个隔开
    pytest.param(PHONES, _COUNT + "NOT (buyer_phone <= 13900000000 OR buyer_phone >= 13900000002)",
                 _COUNT + "NOT (buyer_phone <= 已遮罩 OR buyer_phone >= 已遮罩)", id="相邻的整数隔开"),
    # 第 2 轮复核：一头是算式、等值的值是算式、两头不是整数、范围再用不等去掉一头、前面补零的号码。以前都照原样
    # 封存，后两种的号码还被当成阈值，回答正文跟着不遮
    pytest.param(PHONES, _COUNT + "buyer_phone BETWEEN 13900000000 + 1 AND 13900000009 - 8",
                 _COUNT + "buyer_phone BETWEEN 已遮罩 + 已遮罩 AND 已遮罩 - 已遮罩", id="算出来的边界"),
    pytest.param(PHONES, _COUNT + "buyer_phone = 13900000000 + 1", _COUNT + "buyer_phone = 已遮罩 + 已遮罩",
                 id="算出来的等值"),
    pytest.param(PHONES, _COUNT + "buyer_phone > 13900000000.5 AND buyer_phone < 13900000001.5",
                 _COUNT + "buyer_phone > 已遮罩 AND buyer_phone < 已遮罩", id="两头不是整数"),
    pytest.param(PHONES, _COUNT + "buyer_phone >= 13900000001 AND buyer_phone <= 13900000002 "
                 "AND buyer_phone <> 13900000002",
                 _COUNT + "buyer_phone >= 已遮罩 AND buyer_phone <= 已遮罩 AND buyer_phone <> 已遮罩", id="范围再排除一头"),
    pytest.param(PHONES, _COUNT + "buyer_phone = 013900000001", _COUNT + "buyer_phone = 已遮罩", id="前面补零的号码"),
])
async def test_a_customer_looked_up_in_any_shape_never_shows(monkeypatch, tmp_path, client, script, sql, sealed):
    """agent 换着写法找某一个客户（简单 CASE、IN (VALUES …)、两头相同的范围、行值、选出列里写着同一个号码……），回答里
    复述了号码：回答正文、封存的 SQL、事件、回答文档、质询工件里都不出现这个号码。报告取数选了手机号，号码是从报告
    自己的查询快照里认出来的。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"}, script=script)
    plays = {"这位客户": Play([sql], "客户 13900000001 共下了 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, WITH_PHONE))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="这位客户下了几笔？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "客户 已遮罩 共下了 1 笔。")
        [query] = item["queries"]
        assert query["sql"] == sealed
        [event] = await events(paused.id, "approval.inquiry")
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200, got.text
        assert got.json()["content"]["sql"] == sealed, "质询工件的 SQL 和事件里的一字不差"
        doc = artifact_store.load(item["answer"]["doc_artifact"])
        for shown in (r.text, json.dumps(event.data, ensure_ascii=False), json.dumps(doc, ensure_ascii=False), got.text):
            assert "1390000000" not in shown
        if "号码" in sql:
            assert got.json()["content"]["rows"] == [[1, "已遮罩"]] and got.json()["masked"] == ["号码"]
    finally:
        await engines.invalidate(source_id)


#: 同一个号码（13900000001）换了写法：带空格、连字符、点、括号、全角数字或全角标点、+86 / 0086 / 86 前缀
REWRITTEN = [
    pytest.param("139-0000-0001", id="连字符"), pytest.param("139 0000 0001", id="空格"),
    pytest.param("139.0000.0001", id="点"), pytest.param("(139)0000-0001", id="括号"),
    pytest.param("+8613900000001", id="+86"), pytest.param("+86 139 0000 0001", id="+86 加空格"),
    pytest.param("0086 139 0000 0001", id="0086"), pytest.param("86-139-0000-0001", id="86"),
    pytest.param("１３９０００００００１", id="全角数字"), pytest.param("１３９－００００－０００１", id="全角连字符"),
    pytest.param("（139）0000　0001", id="全角括号和空格"),
]


def _shown_anywhere(*texts: str) -> str:
    return "\n".join(texts)


@pytest.mark.parametrize("written", REWRITTEN)
async def test_a_masked_number_written_in_another_shape_never_shows(monkeypatch, tmp_path, client, written):
    """验收准备时复现过：模型把号码换个写法写进回答（139-0000-0001、+86 …、全角数字），回答正文、事件、回答文档原样
    带着，最后一道关也拦不住，正式运行里永久封存。现在正文里按纯数字写法认出来，换成「已遮罩」。报告取数选了手机号，
    号码是从报告自己的查询快照里认出来的。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"}, script=SALES)
    asked = "SELECT COUNT(*) AS 笔数 FROM sales WHERE buyer_phone = '13900000001'"
    plays = {"这位客户": Play([asked], f"客户 {written} 共下了 [[v:Q1.r0.笔数]] 笔。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, WITH_PHONE))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="这位客户下了几笔？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["text"]) == (True, "客户 已遮罩 共下了 1 笔。")
        [event] = await events(paused.id, "approval.inquiry")
        doc = artifact_store.load(item["answer"]["doc_artifact"])
        shown = _shown_anywhere(json.dumps(r.json(), ensure_ascii=False), json.dumps(event.data, ensure_ascii=False),
                                json.dumps(doc, ensure_ascii=False))
        assert written not in shown and "1390000000" not in shown
    finally:
        await engines.invalidate(source_id)


#: 回访记录的备注里写着换了写法的号码（备注没设遮罩）
REWRITTEN_CALLBACKS = SALES + ("CREATE TABLE callbacks (id INTEGER PRIMARY KEY, region TEXT, note TEXT);"
                               "INSERT INTO callbacks VALUES (1, '华东', '客户 (139)0000-0001 来电改址'), "
                               "(2, '华南', '例行回访');")


async def test_a_reformatted_number_inside_a_text_cell_stops_the_answer(monkeypatch, tmp_path, client):
    """备注里夹着换了写法的号码，agent 引用这一格：渲染出来才露，最后一道关认得出，回答整份不交；质询工件里只换掉那一截。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": "buyer_phone"}, script=REWRITTEN_CALLBACKS)
    notes = "SELECT region AS 区域, note AS 备注 FROM callbacks ORDER BY id"
    plays = {"回访": Play([notes], "华东的回访备注是 [[v:Q1.r0.备注]]。[[see:Q1]]")}
    Models(monkeypatch, name, writer="华东首单金额 [[v:Q1.r0.金额]]。[[see:Q1]]", plays=plays)
    try:
        paused = await pause(name, graph=fetching(name, WITH_PHONE))
        [approval] = await pending(paused.id)
        r = await ask(client, paused.id, report="write", unit=None, question="华东的回访说了什么？",
                      approval_id=approval.id)
        assert r.status_code == 200, r.text
        item = r.json()["inquiry"]
        assert (item["answer"]["ok"], item["answer"]["error"]) == (False, LEAKED)
        [query] = item["queries"]
        got = await client.get(f"/api/runs/{paused.id}/inquiries/artifacts/{query['artifact']}")
        assert got.status_code == 200, got.text
        assert got.json()["content"]["rows"] == [["华东", "客户 已遮罩 来电改址"], ["华南", "例行回访"]]
        assert "0000-0001" not in got.text
    finally:
        await engines.invalidate(source_id)


# --------------------------------------------------------------------------
# 接线：两道审批的竞态、上传表格的版本、设置、开放之后状态又变了、张冠李戴的报告文档
# --------------------------------------------------------------------------


async def test_an_answer_about_the_first_of_two_approvals_is_not_written_into_the_second(monkeypatch, shop, client):
    """问的是第一张审批卡，回答途中它被批掉、运行停到了第二张卡上：仍是「停在审批」，但问答不属于这一次停下，不写。"""
    seen: dict = {}

    async def approve_and_wait_for_the_next():
        if seen.get("done"):
            return
        seen["done"] = True
        await run_manager.resume(seen["run"], {"approved": True}, approval_id=seen["approval"])
        for _ in range(600):
            cards = await pending(seen["run"])
            if cards and cards[0].id != seen["approval"] and (await _row(seen["run"])).status == "interrupted":
                seen["next"] = cards[0].id
                return
            await asyncio.sleep(0.03)
        raise AssertionError("运行没有停到第二道审批上")

    Models(monkeypatch, shop, before=approve_and_wait_for_the_next)
    graph = chain(node("start", "input"), node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("gate2", "human", mode="approve", title="再确认一次"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    paused = await pause(shop, graph=graph)
    [first] = await pending(paused.id)
    seen.update(run=paused.id, approval=first.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=first.id)
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is False and r.json()["note"] == NOT_RECORDED
    assert (await _row(paused.id)).status == "interrupted", "运行停在第二道审批上"
    assert await events(paused.id, "approval.inquiry") == []
    r = await client.post(f"/api/approvals/{seen['next']}/decide", json={"approved": True})
    assert r.status_code == 200, r.text
    final = await wait(paused.id)
    assert final.status == "succeeded" and (await verify_manifest(paused.id))["ok"] is True


def _book(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "明细"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _publish(name: str, rows: list[list]) -> tuple[str, str]:
    """发布一版上传表格（一张「明细」），返回 (源 id, 快照 id)。源不存在就新建。"""
    (settings.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(id=uuid.uuid4().hex, name=name, kind="sqlite", readonly=True, origin="upload")
        result = await table_versions.publish_upload(session, row, _book(rows), "客流.xlsx", header_row=1,
                                                     mixed="reject", raw_mode=False)
        return row.id, result.snapshot_id


async def test_an_uploaded_table_is_queried_at_the_version_the_run_pinned(monkeypatch, client):
    """运行停在审批时上传表格重传了一版：质询查的仍是运行固定的那一版，和报告用的是同一份数据。"""
    name = f"upload_{uuid.uuid4().hex[:8]}"
    _, first = await _publish(name, [["区域", "数量"], ["分区甲", 1], ["分区乙", 2]])
    plays = {"合计": Play(['SELECT SUM("数量") AS 合计 FROM "明细"'], "合计为 [[v:Q1.r0.合计]]。[[see:Q1]]")}
    Models(monkeypatch, name, writer="数量合计 [[v:Q1.r0.total]]。[[see:Q1]]", plays=plays)
    graph = chain(node("start", "input"),
                  node("fetch", "tool", tool=f"db_query__{name}", args={"sql": 'SELECT SUM("数量") AS total FROM "明细"'}),
                  node("write", "report", "写周报", instructions="写周报"),
                  node("gate", "human", mode="approve", title="周报可以出具吗"),
                  node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    paused = await pause(name, graph=graph)
    await _publish(name, [["区域", "数量"], ["分区甲", 60], ["分区乙", 40]])
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question="数量合计是多少？", approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert item["answer"]["text"] == "合计为 3。", "按运行固定的那一版查，不是重传之后的"
    [query] = item["queries"]
    assert artifact_store.load(query["artifact"])["data_version"] == first
    assert (await _row(paused.id)).data_versions == paused.data_versions


async def test_the_settings_and_the_runs_pinned_versions_reach_the_agent(monkeypatch, shop, client):
    await set_verify({"inquiry": {"max_queries": 3, "timeout_s": 45}, "daily_max_usd": 3.5})
    seen: dict = {}
    original = inquiry_mod.run_verify_agent

    async def spy(**kwargs):
        seen.update(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(inquiry_mod, "run_verify_agent", spy)
    Models(monkeypatch, shop)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    assert (seen["max_queries"], seen["timeout_s"], seen["daily_max_usd"]) == (3, 45, 3.5)
    assert seen.get("data_versions") == paused.data_versions and seen["sources"] == [shop]
    assert seen["run_id"] == paused.id and seen.get("emit") is None


async def test_a_run_continued_while_the_inquiry_opens_is_refused(monkeypatch, shop, client):
    """读完运行状态之后、核对封存之前，运行被继续了：记录上的封存是上一次结束时的，不开放（也不白问一次模型）。"""
    from app.api import evidence

    models = Models(monkeypatch, shop)
    done = await finish(shop)
    original = evidence._sealed

    async def continued_meanwhile(run_id):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            row.status = "running"
            await session.commit()
        return await original(run_id)

    monkeypatch.setattr(evidence, "_sealed", continued_meanwhile)
    r = await ask(client, done.id, report="write", unit=None, question=QUESTION)
    assert r.status_code == 409 and r.json()["code"] == "inquiry_not_open"
    assert models.tasks == [] and models.specs == []


async def test_a_report_document_that_belongs_to_another_run_is_refused(monkeypatch, shop, client):
    """事件里记的报告文档其实是另一次运行的（内容寻址只保证没被改，不保证没被张冠李戴）：不据此质询。"""
    models = Models(monkeypatch, shop)
    paused = await pause(shop)
    other = await pause(shop)
    [approval] = await pending(paused.id)
    [checked] = await events(paused.id, "report.checked", "write")
    [foreign] = await events(other.id, "report.checked", "write")
    async with SessionLocal() as session:
        stored = await session.get(RunEvent, checked.id)
        stored.data = {**stored.data, "doc_artifact": foreign.data["doc_artifact"]}
        await session.commit()
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 422 and r.json()["field"] == "report"
    assert models.tasks == []


# --------------------------------------------------------------------------
# 数据源：同名重建的不查
# --------------------------------------------------------------------------


async def test_a_source_rebuilt_under_the_same_name_is_not_queried(monkeypatch, tmp_path, client):
    """运行用过的数据源被删了、又用同一个名字新建（手工源、上传表格都算）：那已经不是这次运行查过的数据，不交给 agent；
    上传表格也不会因此被补固定进运行记录。"""
    name, source_id = await _source(tmp_path)
    plays = {"华南为什么": Play([], "查不到判断所需的数据。")}
    models = Models(monkeypatch, name, plays=plays)
    done = await finish(name)
    assert done.status == "succeeded", done.error

    async def drop(sid) -> None:
        async with SessionLocal() as session:
            await session.delete(await session.get(DataSource, sid))
            await session.commit()
        await engines.invalidate(sid)

    await drop(source_id)
    _, rebuilt = await _source(tmp_path, name=name)
    note = f"注：这次运行还用到了数据源「{name}」，已不存在、已停用或已被重建，未能查询，以上回答不含其中的数据。"
    r = await ask(client, done.id, report="write", unit=None, question="华南为什么低？")
    assert r.status_code == 200, r.text
    assert r.json()["inquiry"]["answer"]["text"] == f"查不到判断所需的数据。\n\n{note}"
    [task] = models.tasks
    assert f"（这次运行还用到了「{name}」，已不存在、已停用或已被重建，无法查询）" in task

    # 同名新建成上传表格。升级前的查询快照引用没记源 id（认不出是不是同一个源）：这次运行没固定过它的版本，照样不查
    await drop(rebuilt)
    await _publish(name, [["区域", "数量"], ["分区甲", 1]])
    async with SessionLocal() as session:
        for ref in (await session.execute(select(Artifact).where(Artifact.run_id == done.id))).scalars():
            ref.meta = {}
        await session.commit()
    r = await ask(client, done.id, report="write", unit=None, question="华南为什么低？")
    assert r.status_code == 200 and r.json()["inquiry"]["answer"]["text"].endswith(note)
    assert (await _row(done.id)).data_versions == done.data_versions, "上传表格没有被补固定进已封存的运行"


# --------------------------------------------------------------------------
# 说法：触顶、失败、开放不了
# --------------------------------------------------------------------------


async def test_running_out_of_rounds_is_not_reported_as_the_query_limit(monkeypatch, shop, client):
    """模型一直在看表结构、轮数用完了：没查过一次，不能说「查询次数到顶」；回答里的引用一个都解析不了，末尾说明。"""
    await set_verify({"inquiry": {"max_queries": 1}})
    plays = {"排除测试账号": Play([], "仍居首：华东为 [[v:Q1.r0.销售额]]。", schema=99)}
    Models(monkeypatch, shop, plays=plays)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["ok"], item["answer"]["limit"], item["queries"]) == (True, None, [])
    assert item["answer"]["text"] == (f"仍居首：华东为 ⟦?v:Q1.r0.销售额⟧。\n\n{ROUNDS_NOTE}\n\n"
                                      f"{UNRESOLVED_NOTE.format(n=1)}")


async def test_failed_queries_are_explained(monkeypatch, shop, client):
    """两次查询都执行失败、次数用完了：查询清单只列成功的，回答末尾说清有几次失败、几处引用解析不了。"""
    await set_verify({"inquiry": {"max_queries": 2}})
    plays = {"排除测试账号": Play(["SELECT nope FROM sales", "SELECT nothing FROM sales"], NO_TEST_ANSWER)}
    Models(monkeypatch, shop, plays=plays)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    item = r.json()["inquiry"]
    assert (item["answer"]["limit"], item["queries"]) == ("max_queries", [])
    assert item["answer"]["text"] == ("仍居各区域之首：排除测试账号后华东为 ⟦?v:Q1.r0.销售额⟧，华南为 ⟦?v:Q1.r1.销售额⟧。\n\n"
                                      f"{FAILED_NOTE.format(n=2)}\n\n{UNRESOLVED_NOTE.format(n=2)}")


async def test_an_answer_not_given_in_the_asked_shape_says_what_to_do(monkeypatch, shop, client):
    plays = {"排除测试账号": Play([NO_TEST], final={"answer": "华东仍居首"})}
    Models(monkeypatch, shop, plays=plays)
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    answer = r.json()["inquiry"]["answer"]
    assert answer["ok"] is False
    assert answer["error"] == "模型未能按要求给出回答，请重新提问；如反复出现，请在设置的「核查」一节更换核查模型"


def test_internal_wording_from_the_agent_is_not_shown():
    """核查 agent 的原话（「交结果」「按结构」、裸写的字段名）换成提问人看得懂的话，并说下一步。"""
    def words(error, limit=None, result=None):
        outcome = verify_agent.VerifyOutcome(result=result, cells=[], queries=[], rationale=None, usage={},
                                             limit=limit, error=error)
        return inquiry_mod._error_words(outcome, timeout_s=120, daily_max_usd=2.0)

    assert words("收尾时超过时长上限，没能交结果", limit="timeout_s") == \
        "回答超过时长上限（120 秒），未能给出回答，可缩小问题范围后重新提问"
    assert words("已达全局每日核查金额上限 $2.00，没能交结果", limit="daily_max_usd") == \
        "今日核查花费已达上限（核查每日金额上限 $2.00），未能给出回答，可在设置的「核查」一节调高上限"
    assert words("核查模型没有按结构交结果（交回的内容里没有要求的字段：rationale）") == \
        "模型未能按要求给出回答，请重新提问；如反复出现，请在设置的「核查」一节更换核查模型"
    assert words("核查模型没有给出回复") == "模型没有给出回复，未能给出回答，请重新提问"
    assert words("核查出错（磁盘已满）") == "回答时出错（磁盘已满），请重新提问"
    assert words("核查模型不可用（没有配置模型接入）") == "核查模型不可用（没有配置模型接入）"
    assert words(None) is None


async def test_refusals_while_the_run_is_executing_say_what_to_do(monkeypatch, shop, client):
    """运行在执行：带着审批卡来的多半是卡刚被别人批了，叫人刷新；没带卡的按运行类别说什么时候能问。"""
    Models(monkeypatch, shop)
    async with SessionLocal() as session:
        exploring = Run(workflow_name="还在执行", status="running", graph={}, input={}, run_class="exploratory")
        formal = Run(workflow_name="还在执行", status="queued", graph={}, input={}, run_class="formal")
        session.add_all([exploring, formal])
        await session.commit()
        ids = exploring.id, formal.id
    r = await ask(client, ids[0], report="write", unit=None, question=QUESTION, approval_id="card-1")
    assert r.status_code == 409
    assert r.json() == {"detail": "这张审批卡已处理，运行正在继续执行，请刷新页面查看最新状态", "code": "inquiry_not_open"}
    r = await ask(client, ids[0], report="write", unit=None, question=QUESTION)
    assert r.json()["detail"] == "运行仍在执行。停在人工审批时，或运行结束并封存后，才能质询"
    r = await ask(client, ids[1], report="write", unit=None, question=QUESTION)
    assert r.json()["detail"] == "运行仍在执行，停在人工审批时才能质询"
