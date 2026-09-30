"""结论句裁判改造（JR）的判档：contradicted 沿用原来 unsupported 的处置，insufficient 按缺口处理。

- contradicted（证据相矛盾）：按 on_unsupported 降档（degrade）或不予出具（withhold），和原来的 unsupported 一样
- insufficient（证据不足）：按缺口处理，最多降档——不会不予出具，也不会判完整出具
- 不许比现在更松：受管正式运行里，原来会因「证据不支持」降档的句子，改判成 contradicted 或 insufficient
  都不能完整出具
- 旧文档里的 unsupported 判档照旧当 contradicted 处理
- 统计键（stats、counts、出具横幅的结论句计数）都有 contradicted、insufficient，unsupported 保留
- 报告节点：改写一次把证据相矛盾、证据不足的句子都交回写作者；文档的裁判摘要记下写作模型

所有模型调用都是 mock 剧本，不碰真接口。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting, Workflow, WorkflowVersion
from app.engine import judge as judge_mod
from app.engine.evidence import compose_doc, iter_units
from app.engine.issuance import decide_tier
from app.engine.judge import JUDGE_ROLE, SPEND_KEY
from app.engine.nodes.io import _claims
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel

JUDGE_MODEL = "claude-sonnet-5"
WRITER = "mock-fast"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
CITED = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。[[see:m:gmv]]下面看细节。"
SUPPORTED, CAUSAL = "本周销售额 45,678.5元。", "增长主要来自新客首单。"


# --------------------------------------------------------------------------
# 出口判档（io._claims）：直接喂文档
# --------------------------------------------------------------------------


def judged_doc(statuses: dict[str, dict], *, on_unsupported="degrade") -> dict:
    """两句结论：SUPPORTED_TEXT、CAUSAL_TEXT；statuses 按句子给判定。counts 照判定数。"""
    doc = compose_doc("本周销售额稳定增长。增长主要来自新客首单。", {})
    md = doc["markdown"]
    counts = {k: 0 for k in ("supported", "partial", "unsupported", "not_a_claim", "unjudged")}
    for _, unit in iter_units(doc):
        text = md[unit["span"][0]:unit["span"][1]].strip()
        verdict = {"status": "supported", "rationale": "有依据", "judge": JUDGE_MODEL, "post_seal": False, "used": [],
                   **statuses.get(text, {})}
        unit["verdict"] = verdict
        counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
    doc["judge"] = {"mode": "inline", "model": JUDGE_MODEL, "on_unsupported": on_unsupported, "counts": counts,
                    "unjudged": {}, "limits_hit": [], "complete": True, "gaps": []}
    return doc


def grade(doc: dict, *, governed=False, contract=None) -> tuple[str, dict]:
    out = {"gaps": [], "claims_policy": None, "uncited": None, "claims": None, "unsupported": None}
    _claims(out, contract or {}, {"claims": "judge", "text": doc["markdown"]}, evidence_on=True, governed=governed,
            uncited=[], doc=doc, title="write")
    tier = decide_tier(missing_required=[], missing_expected=[], unmatched=[], strict=True, gaps=out["gaps"],
                       unsupported=out["unsupported"], uncited_claims=out["uncited"],
                       claims_policy=out["claims_policy"])
    return tier, out


TEXT2 = "增长主要来自新客首单。"


def test_all_supported_is_still_a_full_issuance():
    tier, out = grade(judged_doc({}))
    assert tier == "formal" and out["gaps"] == []


@pytest.mark.parametrize(("on_unsupported", "tier"), [("degrade", "degraded"), ("withhold", "withheld")])
def test_contradicted_follows_on_unsupported(on_unsupported, tier):
    doc = judged_doc({TEXT2: {"status": "contradicted", "rationale": "证据里新客占比下降"}},
                     on_unsupported=on_unsupported)
    got, out = grade(doc)
    assert got == tier
    claims = out["claims"]
    [item] = claims["contradicted"]
    assert item["text"] == TEXT2 and item["rationale"] == "证据里新客占比下降"
    assert claims["counts"]["contradicted"] == 1 and claims["counts"]["insufficient"] == 0
    assert out["gaps"] == [], "判档来自判定，不来自缺口"


def test_insufficient_is_a_gap_and_never_withholds():
    doc = judged_doc({TEXT2: {"status": "insufficient", "rationale": "摘录里没有新客维度", "missing": "新客维度的查询"}},
                     on_unsupported="withhold")
    tier, out = grade(doc, governed=True, contract={"claims": {"policy": "judge", "on_unsupported": "withhold"}})
    assert tier == "degraded", "证据不足最多降档：不能不予出具，也不能完整出具"
    [gap] = [g for g in out["gaps"] if "证据不足" in g]
    assert gap.startswith("报告「write」") and "1 句" in gap and "新客维度的查询" in gap
    [item] = out["claims"]["insufficient"]
    assert item["text"] == TEXT2 and item["missing"] == "新客维度的查询"
    assert out["claims"]["counts"]["insufficient"] == 1 and out["unsupported"] == []


def test_an_old_unsupported_verdict_is_graded_as_contradicted():
    doc = judged_doc({TEXT2: {"status": "unsupported", "rationale": "证据不支持"}}, on_unsupported="withhold")
    tier, out = grade(doc)
    assert tier == "withheld"
    assert out["claims"]["counts"]["unsupported"] == 1
    assert [i["text"] for i in out["claims"]["unsupported"]] == [TEXT2]
    assert [i["text"] for i in out["unsupported"]] == [TEXT2]


def test_the_issuance_counts_carry_every_key():
    _, out = grade(judged_doc({}))
    assert set(out["claims"]["counts"]) == set(judge_mod.COUNT_KEYS)


# --------------------------------------------------------------------------
# 端到端：正式运行、受管正式运行
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(*, report=None, contract=None) -> dict:
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **{"claims": "judge", "judge": {"max_cost_usd": 0.05},
                                                           **(report or {})}),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["caliber"], "required": ["gmv"], "strict": True,
                       **(contract or {})}),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


class Models:
    """写作者依次回 writer 里的几段；裁判按句子里的字给判定：verdicts = {句中片段: (判定, missing)}。"""

    def __init__(self, monkeypatch, writer=(CITED,), verdicts=None):
        self.writes: list[list] = []
        self.judges: list[str] = []
        verdicts = verdicts or {}

        def decide(model, messages):
            if JUDGE_ROLE in str(messages[0].content):
                request = str(messages[-1].content)
                self.judges.append(request)
                items = []
                for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M):
                    status, missing = next((v for frag, v in verdicts.items() if frag in line), ("supported", ""))
                    items.append({"unit": uid, "verdict": status, "rationale": f"判的是：{line.strip()[:12]}",
                                  "missing": missing, "used": []})
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            self.writes.append(list(messages))
            return AIMessage(content=writer[min(len(self.writes), len(writer)) - 1])

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def judge_model_of(session, spec):
            return MockChatModel(model_name=JUDGE_MODEL), JUDGE_MODEL

        monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def run(graph, *, run_class="formal") -> Run:
    started = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    row = await wait(started.id)
    assert row.status == "succeeded", row.error
    return row


async def checked_of(run_id: str) -> tuple[dict, dict]:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "report.checked"))).scalars()
    return event.data, artifact_store.load(event.data["doc_artifact"])


async def issuance_event(run_id: str) -> dict:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "issuance"))).scalars()
    return event.data


async def test_a_formal_run_withholds_a_contradicted_claim(engine_up, monkeypatch):
    Models(monkeypatch, verdicts={"新客": ("contradicted", "")})
    row = await run(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "withheld"
    assert [i["text"] for i in issuance["claims"]["contradicted"]] == [CAUSAL]
    event = await issuance_event(row.id)
    assert event["claims"]["counts"] == {"supported": 1, "partial": 0, "contradicted": 1, "insufficient": 0,
                                         "not_a_claim": 0, "unjudged": 0, "unsupported": 0}


async def test_a_formal_run_only_degrades_an_insufficient_claim(engine_up, monkeypatch):
    Models(monkeypatch, verdicts={"新客": ("insufficient", "新客维度的数据")})
    row = await run(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", "证据不足：最多降档，不因 withhold 不予出具"
    assert any("证据不足" in g and "新客维度的数据" in g for g in issuance["gaps"]), issuance["gaps"]
    [item] = issuance["claims"]["insufficient"]
    assert item["text"] == CAUSAL and item["missing"] == "新客维度的数据"
    assert issuance["claims"]["counts"]["insufficient"] == 1

    checked, doc = await checked_of(row.id)
    assert doc["stats"]["insufficient"] == 1 and doc["stats"]["contradicted"] == 0 and doc["stats"]["unsupported"] == 0
    assert checked["stats"]["insufficient"] == 1
    assert doc["judge"]["writer_model"] == WRITER and checked["judge"]["writer_model"] == WRITER
    [unit] = [u for _, u in iter_units(doc) if u.get("verdict", {}).get("status") == "insufficient"]
    assert unit["verdict"]["missing"] == "新客维度的数据"


async def governed(client, graph) -> Run:
    async with SessionLocal() as session:
        wf = Workflow(name=f"jr-{uuid.uuid4().hex[:6]}", graph=graph, status="governed", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        wf_id = wf.id
    res = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert res.status_code == 201, res.text
    row = await wait(res.json()["id"])
    assert row.status == "succeeded", row.error
    return row


@pytest.mark.parametrize("status", ["contradicted", "insufficient"])
async def test_a_governed_formal_run_is_never_fully_issued_on_a_split_verdict(client, monkeypatch, status):
    """不许比现在更松：原来判「证据不支持」要降档的句子，改判成哪一档都不能完整出具。"""
    Models(monkeypatch, verdicts={"新客": (status, "新客维度的数据" if status == "insufficient" else "")})
    graph = weekly(report={"numbers": "strict", "on_violation": "fail", "judge": {"max_cost_usd": None}})
    row = await governed(client, graph)
    assert row.output["_issuance"]["tier"] == "degraded"


async def test_rewrite_once_hands_back_contradicted_and_insufficient_sentences(engine_up, monkeypatch):
    draft = ("本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。[[see:m:gmv]]"
             "华东贡献了大部分增长。[[see:m:gmv]]下面看细节。")
    rewritten = "本周销售额 [[m:gmv]]。[[see:m:gmv]]新客、华东的贡献要等分维度的数据再看。下面看细节。"
    models = Models(monkeypatch, writer=(draft, rewritten),
                    verdicts={"新客首单": ("contradicted", ""), "华东贡献": ("insufficient", "分区域的查询"),
                              "要等分维度": ("not_a_claim", "")})
    row = await run(weekly(report={"judge": {"max_cost_usd": 0.05, "rewrite_once": True}}))
    assert len(models.writes) == 2
    request = str(models.writes[1][-1].content)
    assert "增长主要来自新客首单。" in request and "华东贡献了大部分增长。" in request
    assert "分区域的查询" in request, "证据不足的句子连同缺的是什么一起交回"
    assert SUPPORTED not in request
    checked, _ = await checked_of(row.id)
    assert checked["judge"]["rewrite"]["applied"] is True and len(checked["judge"]["rewrite"]["sentences"]) == 2


async def test_an_explore_report_records_the_writer_model_too(engine_up, monkeypatch):
    models = Models(monkeypatch)
    row = await run(weekly(), run_class="exploratory")
    assert models.judges == []
    checked, doc = await checked_of(row.id)
    assert doc["judge"]["mode"] == "on_demand" and doc["judge"]["writer_model"] == WRITER
    assert set(judge_mod.COUNT_KEYS) <= set(doc["stats"])
