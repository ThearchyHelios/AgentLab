"""节点上、设置里点名的模型接入不存在或已停用时，每条用到模型的路径都照实报给人，不落成演示模型。

以前点名的接入不存在时 get_chat_model 悄悄换成演示模型（mock-fast）：报告由它写成假文本，双盲复算、反方审稿、审批
质询由它来「核查」，正式运行照样出具。现在（providers/factory.named_provider）：

- 模型调用、Agent、报告撰写、多 Agent 协作、让模型分类的条件分支、让模型修复的结构校验：节点失败，报错带原因和去哪里改，
  一次模型调用都不发；让模型重排的知识检索：不重排，降级日志写明原因；
- 双盲复算：该指标无法复算，原因「核查模型不可用（…）」，记缺口；反方审稿：这几句未能反查，原因同上；
- 审批质询：回答 ok 为假，error 说明原因；结论句裁判（运行中、按需）：按「裁判模型不可用」照实记未裁判和缺口；
- 助手：报错给用户，查当前用哪个模型的接口照常返回、写明原因；门控模型：转人工审批、写明原因；
- 定时任务发起的正式运行：运行失败，通知照常推送。

每条路径都看一遍：产出、事件、llm.end 里不出现 mock-fast。没点名接入的节点（写作者）用一个显式建的演示接入，模型名是
「写作演示模型」，所以事件里一出现 mock-fast 就是回退到了内置的演示模型。接入名、数据源、数字都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.core.crypto import encrypt
from app.data import introspect
from app.db.base import SessionLocal
from app.db.models import (
    Approval, DataSource, Provider, Run, RunEvent, Schedule, ScheduleFire, Setting, Workflow, WorkflowVersion,
)
from app.engine import spend
from app.engine.evidence import iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel
from tests.fixtures.dual_check_shop import DEFINITION, RIGHT, add_source, build
from tests.fixtures.sources import drop_source

#: 没点名接入的节点用的演示接入：正常的调用在事件里记「写作演示模型」
WRITER_PROVIDER, WRITER_MODEL = "测试演示接入", "写作演示模型"
#: 写错一个字的接入名；一个停用了的演示接入（停用前照样能调用：以前点名它会照常返回、照常调用）
MISSING, DISABLED = "测试演示接人", "停用演示接入"
REASONS = {MISSING: f"模型接入「{MISSING}」不存在", DISABLED: f"模型接入「{DISABLED}」已停用"}
HINTS = {MISSING: "请在「设置 → 模型接入」中核对名称，或换一个接入",
         DISABLED: "请在「设置 → 模型接入」中启用它，或换一个接入"}
SETTING_KEYS = ["verify", "judge", "copilot", "run", spend.SPEND_KEY, JUDGE_SPEND_KEY]
UTC = timezone.utc


async def _clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(SETTING_KEYS)))
        await session.execute(delete(Provider).where(Provider.name.in_([WRITER_PROVIDER, DISABLED])))
        await session.commit()


@pytest.fixture(autouse=True)
async def engine_up():
    await _clean()
    async with SessionLocal() as session:
        session.add_all([
            Provider(name=WRITER_PROVIDER, kind="mock", models=[{"id": WRITER_MODEL}], default_model=WRITER_MODEL,
                     enabled=True, extra={}),
            Provider(name=DISABLED, kind="mock", models=[{"id": "停用演示模型"}], default_model="停用演示模型",
                     enabled=False, extra={}),
        ])
        await session.commit()
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    # 建过的接入、写过的设置不留给同一进程里后面的测试：留着的启用接入会改变别处「没点名」时的回退
    await _clean()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def script(monkeypatch, writer: str = "本周情况平稳。") -> list[str]:
    """演示模型的剧本：写作者回 writer；按结构交结果的调用交空对象、绑了工具的直接收尾（核查、裁判、质询的模型照理
    一次都不该被调用到）。返回每次调用的模型名。"""
    called: list[str] = []

    def decide(model, messages):
        called.append(model.model_name)
        if model.response_format:
            return AIMessage(content="{}")
        if model.tools:
            return AIMessage(content="查完了。")
        return AIMessage(content=writer)

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    return called


async def put_setting(key: str, value: dict) -> None:
    """直接写库：PUT /api/settings 现在会拦下不存在、已停用的接入，这里模拟的是写好之后接入才被删掉、停用。"""
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
        await session.commit()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}：{row.error}")


async def start(spec: dict, *, run_class: str = "exploratory", statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=spec, input_payload={}, run_class=run_class)
    return await wait(run.id, statuses)


async def events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))).scalars())


def output_of(evs: list[RunEvent], node_id: str) -> dict:
    finished = [e for e in evs if e.type == "node.finished" and e.node_id == node_id]
    assert finished, f"节点 {node_id} 没有执行完"
    return artifact_store.load(finished[-1].data["artifact"])


def logs(evs: list[RunEvent], code: str) -> list[dict]:
    return [e.data for e in evs if e.type == "log" and e.data.get("code") == code]


def no_mock(evs: list[RunEvent], *more) -> None:
    """事件和给出的产出里都不出现内置演示模型的名字。"""
    blob = json.dumps([e.data for e in evs] + list(more), ensure_ascii=False, default=str)
    assert "mock-fast" not in blob


# --------------------------------------------------------------------------
# 模型调用、Agent、报告撰写节点：节点失败，说清原因和去哪里改
# --------------------------------------------------------------------------

NODES = {
    "llm": lambda ref: node("ask", "llm", "提问", provider=ref, prompt="总结本周情况"),
    "agent": lambda ref: node("ask", "agent", "查数作答", provider=ref, prompt="回答问题"),
    "report": lambda ref: node("ask", "report", "写周报", provider=ref, instructions="写周报"),
    # 下面两种的执行器不自己接这个错，由编排层按「执行出错：原因」报出，原因和提示一样都在
    "supervisor": lambda ref: node("ask", "supervisor", "协作", provider=ref, goal="分析本周销售",
                                   agents=[{"name": "分析师", "system": "你是销售分析师"}]),
    "branch": lambda ref: node("ask", "branch", "分流", mode="llm", provider=ref,
                               cases=[{"key": "follow", "label": "需要跟进"}]),
    # 输入不是 JSON：第一次校验不通过，要让模型修复时才取模型
    "validate": lambda ref: node("ask", "validate", "校验", provider=ref, source="门店甲本周销售平稳",
                                 schema={"type": "object", "required": ["区域"]}),
}


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
@pytest.mark.parametrize("kind", list(NODES))
async def test_a_node_naming_a_broken_provider_fails_and_says_why(monkeypatch, kind, ref):
    called = script(monkeypatch)
    out = node("out", "output", fields=[{"name": "结果", "value": "{{ last_message }}"}])
    spec = chain(node("start", "input"), NODES[kind](ref), out)
    if kind == "branch":
        spec["edges"][1]["sourceHandle"] = "follow"
        spec["edges"].append({"source": "ask", "target": "out", "sourceHandle": "default"})
    run = await start(spec)
    assert run.status == "failed"
    assert REASONS[ref] in run.error and HINTS[ref] in run.error, run.error
    evs = await events(run.id)
    [failed] = [e for e in evs if e.type == "node.failed"]
    assert failed.node_id == "ask" and REASONS[ref] in failed.data["error"] and HINTS[ref] in failed.data["error"]
    assert not [e for e in evs if e.type in ("llm.start", "llm.end")] and called == []
    no_mock(evs, run.output, run.error)


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_a_retrieve_reranked_by_a_broken_provider_keeps_its_hits_and_says_why(monkeypatch, ref):
    """让模型重排的知识检索：接入取不到就不重排、照初筛的顺序给命中，降级日志写明原因（不悄悄让演示模型去排）。"""
    from app.db.models import Document
    from app.memory import kb

    called = script(monkeypatch)
    collection = f"k1_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await kb.ingest_document(session, collection=collection, title="门店巡检规范",
                                 content="门店巡检每周一次，巡检结果当天上报区域经理。")
    try:
        spec = chain(node("start", "input"),
                     node("look", "retrieve", "检索", query="门店巡检多久一次", collection=collection, rerank="model",
                          provider=ref),
                     node("out", "output", fields=[{"name": "结论", "value": "见检索结果"}]))
        run = await start(spec)
        assert run.status == "succeeded", run.error
        evs = await events(run.id)
        assert output_of(evs, "look")["count"] >= 1
        said = [d["message"] for d in logs(evs, "retrieve_degraded")]
        assert any(m.startswith("重排不可用：") and REASONS[ref] in m and HINTS[ref] in m for m in said), said
        assert called == [] and not [e for e in evs if e.type in ("llm.start", "llm.end")]
        no_mock(evs, run.output)
    finally:
        async with SessionLocal() as session:
            for doc in list((await session.execute(select(Document).where(Document.collection == collection))).scalars()):
                await kb.delete_document(session, doc.id)


# --------------------------------------------------------------------------
# 双盲复算：无法复算，记缺口
# --------------------------------------------------------------------------

GMV = {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "cell(nodes.fetch, 0, 'gmv')"}


@pytest.fixture
async def shop(tmp_path):
    name, source_id = await add_source(build(tmp_path / "shop.db"))
    yield name
    await drop_source(source_id)


def dual_graph(source: str, **dual) -> dict:
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": RIGHT}),
        node("card", "metrics", caliber="销售口径", caliber_version="v1", metrics=[GMV]),
        node("dual", "dual_check", "复核销售额", definitions={"gmv": DEFINITION}, **dual),
        node("out", "output", fields=[{"name": "结论", "value": "本周销售额见口径卡"}],
             contract={"metrics_from": "card", "narrative": "本周销售额见口径卡"}),
    )


@pytest.mark.parametrize("where", ["settings", "node"])
@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_dual_check_with_a_broken_verify_provider_records_the_metric_as_unchecked(monkeypatch, shop, ref, where):
    called = script(monkeypatch)
    if where == "settings":
        await put_setting("verify", {"provider": ref})
    run = await start(dual_graph(shop, **({"provider": ref} if where == "node" else {})), run_class="formal")
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = output_of(evs, "dual")
    assert out["status"] == "unchecked"
    [metric] = out["metrics"]
    assert metric["status"] == "unchecked"
    assert metric["reason"].startswith("核查模型不可用") and REASONS[ref] in metric["reason"], metric["reason"]
    assert out["issuance_gaps"] == [f"指标「销售额」无法复算：{metric['reason']}"]
    assert any(REASONS[ref] in gap for gap in run.output["_issuance"]["gaps"])
    [said] = logs(evs, "dual_check_unchecked")
    assert REASONS[ref] in said["metrics"][0]["reason"]
    assert called == [] and not [e for e in evs if e.type == "llm.end"]
    no_mock(evs, run.output, out)


# --------------------------------------------------------------------------
# 反方审稿：未能反查；审批质询：回答失败、说明原因
# --------------------------------------------------------------------------

SALES = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, refunded INTEGER);"
         "INSERT INTO sales VALUES (1, '华东', 1200, 0), (2, '华东', 300, 1), (3, '华南', 900, 0), (4, '华北', 400, 0);")
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
#: 一句有依据的结论、一句预埋的错误结论（华南其实低于华东）
TEXT = "华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额超过华东。[[see:Q1]]"


@pytest.fixture
async def sales(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(SALES)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=f"sales_{uuid.uuid4().hex[:8]}", kind="sqlite", database=str(path), readonly=True,
                         enabled=True, options={})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        name, source_id = row.name, row.id
    yield name
    await drop_source(source_id)


def report_graph(source: str, *between) -> dict:
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        *between,
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write"}),
    )


@pytest.mark.parametrize("where", ["settings", "node"])
@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_challenge_with_a_broken_verify_provider_reviews_no_claim_and_says_why(monkeypatch, sales, ref, where):
    called = script(monkeypatch, writer=TEXT)
    if where == "settings":
        await put_setting("verify", {"provider": ref})
    chal = node("chal", "challenge", "审稿", report="write", **({"provider": ref} if where == "node" else {}))
    run = await start(report_graph(sales, chal), run_class="formal")
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    out = output_of(evs, "chal")
    assert out["counts"]["inconclusive"] == out["unreviewed"] == 2
    assert out["counts"]["refuted"] == out["counts"]["stands"] == 0
    [said] = logs(evs, "challenge_unreviewed")
    assert said["reason"].startswith("核查模型不可用") and REASONS[ref] in said["reason"], said["reason"]
    record = artifact_store.load(out["challenge_artifact"])
    assert all(REASONS[ref] in c["rationale"] for c in record["claims"])
    # 只有写作者调用了模型
    assert called == [WRITER_MODEL]
    no_mock(evs, run.output, out, record)


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_an_inquiry_with_a_broken_verify_provider_answers_with_the_reason(monkeypatch, sales, client, ref):
    called = script(monkeypatch, writer=TEXT)
    await put_setting("verify", {"provider": ref})
    paused = await start(report_graph(sales, node("gate", "human", mode="approve", title="周报可以出具吗")),
                         statuses=("interrupted",))
    async with SessionLocal() as session:
        [approval] = list((await session.execute(select(Approval).where(
            Approval.run_id == paused.id, Approval.status == "pending"))).scalars())
    r = await client.post(f"/api/runs/{paused.id}/inquiries",
                          json={"report": "write", "unit": None, "question": "华南真的超过华东吗？",
                                "approval_id": approval.id})
    assert r.status_code == 200, r.text
    inquiry = r.json()["inquiry"]
    answer = inquiry["answer"]
    assert answer["ok"] is False and answer["text"] == ""
    assert answer["error"].startswith("核查模型不可用") and REASONS[ref] in answer["error"], answer["error"]
    assert inquiry["model"] is None and inquiry["usage"]["calls"] == 0
    evs = await events(paused.id)
    [event] = [e for e in evs if e.type == "approval.inquiry"]
    assert event.data["ok"] is False and event.data["error"] == answer["error"]
    assert called == [WRITER_MODEL]
    no_mock(evs, r.json())


# --------------------------------------------------------------------------
# 结论句裁判：照「裁判模型不可用」记未裁判和缺口
# --------------------------------------------------------------------------

METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
JUDGED = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。"


def weekly(**report) -> dict:
    return chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


@pytest.mark.parametrize("where", ["node", "settings"])
@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_a_broken_judge_provider_leaves_the_claims_unjudged_and_says_why(monkeypatch, where, ref):
    called = script(monkeypatch, writer=JUDGED)
    if where == "settings":
        await put_setting("judge", {"provider": ref})
    report = {"claims": "judge", **({"judge": {"provider": ref}} if where == "node" else {})}
    run = await start(weekly(**report), run_class="formal")
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    [checked] = [e for e in evs if e.type == "report.checked"]
    summary = checked.data["judge"]
    assert summary["complete"] is False and summary["counts"]["unjudged"] == 2
    assert summary["gaps"] and all("裁判模型不可用" in g and REASONS[ref] in g for g in summary["gaps"]), summary["gaps"]
    doc = artifact_store.load(checked.data["doc_artifact"])
    verdicts = [u["verdict"] for _, u in iter_units(doc) if u.get("verdict")]
    assert len(verdicts) == 2 and all(v["status"] == "unjudged" and v["reason"] == "error" for v in verdicts)
    assert not [e for e in evs if e.type == "llm.end" and e.data.get("purpose") == "judge"]
    assert called == [WRITER_MODEL]
    no_mock(evs, run.output, doc)


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_judging_on_demand_with_a_broken_judge_provider_says_why(monkeypatch, client, ref):
    """探索运行里按需裁判几句（POST /evidence/judge）：设置里的裁判接入取不到，照「裁判模型不可用」说清，句子仍是未裁判，
    不调用模型、不记判定。"""
    called = script(monkeypatch, writer=JUDGED)
    run = await start(weekly(claims="require_citation"))
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    [checked] = [e for e in evs if e.type == "report.checked"]
    doc = artifact_store.load(checked.data["doc_artifact"])
    units = [u["id"] for _, u in iter_units(doc)]
    await put_setting("judge", {"provider": ref})
    r = await client.post(f"/api/runs/{run.id}/evidence/judge", json={"units": units})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["judged"] == [] and body["calls"] == 0 and body["event"] is None
    assert body["gaps"] and all("裁判模型不可用" in g and REASONS[ref] in g for g in body["gaps"]), body["gaps"]
    assert body["message"].startswith("结论句裁判未完成：") and REASONS[ref] in body["message"]
    assert all(v["status"] == "unjudged" for v in body["verdicts"].values()) and body["verdicts"]
    assert called == [WRITER_MODEL]
    no_mock(await events(run.id), body)


# --------------------------------------------------------------------------
# 助手、门控模型
# --------------------------------------------------------------------------


#: 助手报给人的那一句：照实说是哪个接入、在设置里能做什么，或者给助手换一个模型（不再说「尚未配置完成」）
SAID = {MISSING: f"助手使用的{REASONS[MISSING]}。请在「设置 → 模型接入」中核对名称，或为助手换一个模型",
        DISABLED: f"助手使用的{REASONS[DISABLED]}。请在「设置 → 模型接入」中启用它，或为助手换一个模型"}


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_the_assistant_with_a_broken_provider_tells_the_user(monkeypatch, client, ref):
    called = script(monkeypatch)
    await put_setting("copilot", {"provider": ref, "model": ""})
    r = await client.post("/api/copilot/generate", json={"instruction": "做一张门店周报"})
    assert r.status_code == 400
    assert r.json()["detail"] == SAID[ref]
    assert called == []
    # 查当前用哪个模型：照常返回，不 500，写明为什么用不了
    got = await client.get("/api/copilot/model")
    assert got.status_code == 200, got.text
    body = got.json()
    assert (body["configured"], body["provider"]) == (True, ref)
    assert body["effective_provider"] is None and body["effective_model"] is None
    assert body["error"] == SAID[ref]
    no_mock([], r.json(), body)


async def test_the_assistant_model_lookup_survives_a_name_that_is_not_text(client):
    """设置里存进了不是字符串的接入名（以前经设置接口就能写进来）：照常返回、说清用不了，不 500。"""
    await put_setting("copilot", {"provider": 123, "model": ""})
    got = await client.get("/api/copilot/model")
    assert got.status_code == 200, got.text
    assert got.json()["effective_provider"] is None and "模型接入「123」不存在" in got.json()["error"]


async def test_drafting_with_the_assistant_model_says_which_provider_is_broken(monkeypatch):
    """表格导入、数据目录的 AI 起草也用助手的模型：说法和助手同一句（以前停用的接入说「尚未配置完成」）。"""
    from app.data import catalog, recipe_ai

    called = script(monkeypatch)
    for ref in (MISSING, DISABLED):
        await put_setting("copilot", {"provider": ref, "model": ""})
        async with SessionLocal() as session:
            assert (await recipe_ai.ai_availability(session)).reason == SAID[ref]
            with pytest.raises(catalog.CatalogModelUnavailable) as e:
                await catalog.resolve_draft_model(session)
        assert str(e.value) == SAID[ref]
    assert called == []


async def test_the_assistant_model_lookup_survives_a_model_whose_provider_is_disabled(client):
    """只存了模型名、提供它的接入已停用：以前这里直接 500。"""
    await put_setting("copilot", {"provider": "", "model": "停用演示模型"})
    got = await client.get("/api/copilot/model")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["effective_provider"] is None and body["effective_model"] is None
    assert f"所属的模型接入「{DISABLED}」已停用" in body["error"]


async def test_the_assistant_model_lookup_reports_no_error_when_it_works(client):
    await put_setting("copilot", {"provider": WRITER_PROVIDER, "model": ""})
    body = (await client.get("/api/copilot/model")).json()
    assert body["effective_provider"] == WRITER_PROVIDER and body["effective_model"] == WRITER_MODEL
    assert body["error"] is None


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_a_broken_gate_provider_hands_the_call_to_a_person_and_says_why(monkeypatch, ref):
    from langchain_core.tools import StructuredTool

    from app.tools.trust import ask_gate

    called = script(monkeypatch)
    await put_setting("run", {"tool_gate_provider": ref})

    def lookup(name: str) -> str:
        return name

    tool = StructuredTool.from_function(lookup, name="crm_lookup", description="按客户名称查询客户档案")
    verdict = await ask_gate(tool=tool, args={"name": "甲公司"}, node_title="查客户", task="查甲公司的档案")
    assert verdict.allowed is False
    assert verdict.reason.startswith("门控模型未给出判断") and REASONS[ref] in verdict.reason, verdict.reason
    assert verdict.model == "" and called == []


# --------------------------------------------------------------------------
# 定时任务发起的正式运行：运行失败，通知照常
# --------------------------------------------------------------------------

#: 上海 2026-10-05（周一）09:00
NINE = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
HOOK = "https://hooks.example.com/agentlab/inbox-k1"


@pytest.mark.parametrize("ref", [MISSING, DISABLED])
async def test_a_scheduled_formal_run_naming_a_broken_provider_fails_and_is_still_notified(monkeypatch, ref):
    from app.scheduler import loop, webhook

    called = script(monkeypatch)
    sent: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    monkeypatch.setattr(webhook, "_transport", httpx.MockTransport(receive))
    monkeypatch.setattr(webhook, "RETRY_DELAYS", (0, 0, 0))
    spec = chain(node("start", "input"), node("ask", "llm", "写日报", provider=ref, prompt="写今天的门店日报"),
                 node("out", "output", fields=[{"name": "日报", "value": "{{ last_message }}"}]))
    async with SessionLocal() as session:
        wf = Workflow(name=f"门店日报-{uuid.uuid4().hex[:6]}", graph=spec, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=spec))
        sched = Schedule(name="门店日报", workflow_id=wf.id, cron="0 9 * * *", timezone="Asia/Shanghai",
                         run_class="formal", input_template={}, enabled=True, misfire_grace_s=3600, next_run_at=NINE,
                         webhook_format="generic", webhook_url=encrypt(HOOK), webhook_events=["finished", "failed"])
        session.add(sched)
        await session.commit()
        sid = sched.id
    try:
        await loop.fire_due(NINE + timedelta(seconds=5))
        async with SessionLocal() as session:
            fired = await session.execute(select(ScheduleFire).where(ScheduleFire.schedule_id == sid))
            [fire] = list(fired.scalars())
        assert fire.status == "started" and fire.run_id
        run = await wait(fire.run_id, ("succeeded", "failed", "cancelled"))
        assert run.status == "failed" and run.run_class == "formal"
        assert REASONS[ref] in run.error and HINTS[ref] in run.error, run.error
        assert await webhook.notify_due(datetime.now(UTC) + timedelta(seconds=webhook.SEAL_WAIT_S + 5)) == 1
        [body] = sent
        assert (body["event"], body["status"], body["run_id"]) == ("failed", "failed", run.id)
        assert called == []
        no_mock(await events(run.id), run.output, body)
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Schedule).where(Schedule.id == sid))
            await session.commit()


# --------------------------------------------------------------------------
# 发布前检查里修好的图，正式运行不再失败在那一处
# --------------------------------------------------------------------------


async def test_after_the_publish_fix_stops_naming_a_disabled_provider_the_formal_run_succeeds(monkeypatch, client):
    """复核发现的原样：节点点名停用的接入、模型写它家的。以前发布前检查里选「不指定接入」只去掉接入，检查、保存、发布都
    通过，正式运行按模型名找到的还是那个停用的接入，照样失败。现在模型一并去掉，候选说明写清执行时改用哪个接入，正式
    运行就用它。"""
    called = script(monkeypatch)
    spec = chain(node("start", "input"),
                 node("ask", "llm", "写日报", provider=DISABLED, model="停用演示模型", prompt="写今天的门店日报"),
                 node("out", "output", fields=[{"name": "日报", "value": "{{ last_message }}"}]))
    async with SessionLocal() as session:
        wf = Workflow(name=f"门店日报-{uuid.uuid4().hex[:6]}", graph=spec, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=spec))
        await session.commit()
        wid = wf.id
    r = await client.post(f"/api/workflows/{wid}/publish-check", json={"level": "published"})
    [issue] = [i for i in r.json()["issues"] if i.get("code") == "provider.disabled"]
    [fix] = [f for f in r.json()["fixes"] if f["id"] == issue["fix"]]
    [clear] = [o for o in fix["options"] if o["value"] == ""]
    # 没点名接入、也没写模型时落到第一个可用的接入：本文件只建了一个启用的接入
    assert clear["hint"] == f"去掉这一处指定的接入和模型「停用演示模型」，执行时改用第一个可用的模型接入「{WRITER_PROVIDER}」"
    r = await client.post(f"/api/workflows/{wid}/autofix", json={"level": "published", "apply": [fix["id"]],
                                                                 "choices": {fix["id"]: ""}})
    fixed = r.json()
    assert fixed["applied"] == [fix["id"]] and fixed["ok"] is True, fixed["rejected"]
    assert (await client.patch(f"/api/workflows/{wid}", json={"graph": fixed["graph"]})).status_code == 200
    r = await client.post(f"/api/workflows/{wid}/publish", json={"level": "published"})
    assert r.json()["ok"] is True, r.text
    r = await client.post("/api/runs", json={"workflow_id": wid, "run_class": "formal"})
    assert r.status_code == 201, r.text
    run = await wait(r.json()["id"])
    assert run.status == "succeeded" and run.run_class == "formal", run.error
    assert called == [WRITER_MODEL]
    no_mock(await events(run.id), run.output)
