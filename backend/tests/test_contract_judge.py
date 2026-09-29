"""出口契约（引用模式）按结论句裁判的判定判档（四期）。

- 报告节点 claims: judge 在正式运行里当场裁判，判定随文档封存。出口按文档里的判定判档：证据不支持的结论句
  按 on_unsupported 降档（degrade，默认）或不予出具（withhold）；裁判没跑成、触顶了没判完的，裁判摘要里的缺口
  照抄进出具声明——不能判完整出具，也不因此不予出具
- 判定是模型给的，出口复算不了：信按哈希取回的文档
- judge 在挂引用这件事上和 require_citation 一样严：没挂依据的结论句照样计入缺口
- 探索运行按需裁判：出口只标注（候选句都是「未裁判 · 按需」），不因判定改档位
- 契约也能写 claims: judge：只能收紧（on_unsupported 取更严的）；报告节点没开 judge 的，照实记缺口
- 升级前的运行照旧：契约写了也记「这一版还不支持」
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

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Setting, Workflow, WorkflowVersion
from app.engine import judge as judge_mod
from app.engine import runner as runner_mod
from app.engine.judge import JUDGE_ROLE, SPEND_KEY
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel

JUDGE_MODEL = "claude-sonnet-5"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 两句结论都挂了依据、一句连接性的话
CITED = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。[[see:m:gmv]]下面看细节。"
#: 第二句结论没挂依据
UNCITED = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。下面看细节。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
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
    """写作者回 text；裁判按句子里的字给判定（verdicts：{句中片段: 判定}，没对上的是 supported）。"""

    def __init__(self, monkeypatch, text=CITED, verdicts=None, *, fail_judge=None):
        self.judges: list[str] = []
        verdicts = verdicts or {}

        def decide(model, messages):
            if JUDGE_ROLE in str(messages[0].content):
                request = str(messages[-1].content)
                self.judges.append(request)
                if fail_judge:
                    raise RuntimeError(fail_judge)
                items = [{"unit": uid,
                          "verdict": next((s for frag, s in verdicts.items() if frag in line), "supported"),
                          "rationale": "摘录里有对应的指标" if "销售额" in line else "证据里没有新客维度", "used": []}
                         for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M)]
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            return AIMessage(content=text)

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


async def issued(graph, *, run_class="formal") -> dict:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row.output["_issuance"]


async def governed(client, graph) -> Run:
    """把这张图发布成受管级别，从发布版本发起正式运行。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"judge-{uuid.uuid4().hex[:6]}", graph=graph, status="governed", published_version=1)
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


async def issuance_event(run_id: str) -> dict:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "issuance"))).scalars()
    return event.data


# --------------------------------------------------------------------------
# 正式运行：按封存的判定判档
# --------------------------------------------------------------------------


async def test_every_claim_supported_is_a_full_issuance(monkeypatch):
    Models(monkeypatch)
    issuance = await issued(weekly())
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    claims = issuance["claims"]
    assert claims["policy"] == "judge" and claims["mode"] == "inline" and claims["complete"] is True
    assert claims["counts"] == {"supported": 2, "partial": 0, "unsupported": 0, "not_a_claim": 0, "unjudged": 0}
    assert claims["on_unsupported"] == "degrade" and claims["uncited_claims"] == 0
    assert claims["unsupported"] == [] and claims["model"] == JUDGE_MODEL


async def test_an_unsupported_claim_degrades_by_default(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly())
    assert issuance["tier"] == "degraded", "不支持按缺口降档，不是不予出具（契约是 strict 的）"
    assert issuance["gaps"] == [], "判了就不是「没核」：档位来自判定，不来自缺口"
    [flagged] = issuance["claims"]["unsupported"]
    assert flagged["text"] == "增长主要来自新客首单。" and flagged["rationale"] == "证据里没有新客维度"
    assert flagged["unit"] and len(flagged["span"]) == 2
    assert issuance["claims"]["counts"]["unsupported"] == 1


async def test_withhold_withholds(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}))
    assert issuance["tier"] == "withheld" and issuance["claims"]["on_unsupported"] == "withhold"


async def test_partial_support_is_listed_but_does_not_cost_a_grade(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "partial"})
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}))
    assert issuance["tier"] == "formal"
    [item] = issuance["claims"]["partial"]
    assert item["text"] == "增长主要来自新客首单。" and issuance["claims"]["counts"]["partial"] == 1


async def test_a_failed_judge_is_a_gap_not_a_withheld_report(monkeypatch):
    Models(monkeypatch, fail_judge="网关 502")
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}))
    assert issuance["tier"] == "degraded", "没判成不能盖完整出具，也不能当成「不支持」不予出具"
    [gap] = [g for g in issuance["gaps"] if "裁判" in g]
    assert gap.startswith("报告「write」") and "没裁判" in gap and "网关 502" in gap
    assert issuance["claims"]["complete"] is False and issuance["claims"]["counts"]["unjudged"] == 2


async def test_hitting_a_limit_is_a_gap(monkeypatch):
    Models(monkeypatch)
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "max_claims": 1}}))
    assert issuance["tier"] == "degraded"
    [gap] = [g for g in issuance["gaps"] if "裁判" in g]
    assert "已到上限" in gap and "1 句" in gap
    assert issuance["claims"]["limits_hit"] == ["max_claims"] and issuance["claims"]["counts"]["unjudged"] == 1


async def test_an_uncited_claim_still_counts_under_judge(monkeypatch):
    """judge 在挂引用这件事上和 require_citation 一样严：从 require_citation 换成 judge 不能让没挂依据的句子过关。"""
    Models(monkeypatch, text=UNCITED)
    issuance = await issued(weekly())
    assert issuance["tier"] == "degraded" and issuance["claims"]["uncited_claims"] == 1
    assert issuance["claims"]["uncited"][0]["text"] == "增长主要来自新客首单。"


async def test_the_issuance_event_carries_the_counts(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    run = await run_manager.start(graph=weekly(), input_payload={}, run_class="formal")
    row = await wait(run.id)
    event = await issuance_event(row.id)
    assert event["tier"] == "degraded"
    assert event["claims"] == {"policy": "judge", "uncited_claims": 0, "on_unsupported": "degrade",
                               "mode": "inline", "counts": {"supported": 1, "partial": 0, "unsupported": 1,
                                                            "not_a_claim": 0, "unjudged": 0}}


async def test_a_governed_formal_run_grades_by_the_verdicts(client, monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    graph = weekly(report={"numbers": "strict", "on_violation": "fail",
                           "judge": {"max_cost_usd": None, "on_unsupported": "withhold"}})
    row = await governed(client, graph)
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "withheld" and issuance["claims"]["unsupported"][0]["text"] == "增长主要来自新客首单。"


# --------------------------------------------------------------------------
# 探索运行：只标注
# --------------------------------------------------------------------------


async def test_explore_runs_only_annotate(monkeypatch):
    models = Models(monkeypatch, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}),
                            run_class="exploratory")
    assert models.judges == [], "探索运行节点里不花这笔钱"
    assert issuance["tier"] == "formal" and issuance["gaps"] == []
    claims = issuance["claims"]
    assert claims["mode"] == "on_demand" and claims["complete"] is False
    assert claims["counts"]["unjudged"] == 2 and claims["unjudged"] == {"on_demand": 2}
    assert claims["unsupported"] == []


async def test_explore_runs_still_count_uncited_claims(monkeypatch):
    Models(monkeypatch, text=UNCITED)
    issuance = await issued(weekly(), run_class="exploratory")
    assert issuance["tier"] == "degraded" and issuance["claims"]["uncited_claims"] == 1


# --------------------------------------------------------------------------
# 契约里的 claims: judge
# --------------------------------------------------------------------------


async def test_the_contract_can_tighten_on_unsupported(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly(contract={"claims": {"policy": "judge", "on_unsupported": "withhold"}}))
    assert issuance["tier"] == "withheld" and issuance["claims"]["on_unsupported"] == "withhold"
    assert not any("不支持" in g and "这一版" in g for g in issuance["gaps"])


async def test_the_contract_cannot_loosen_on_unsupported(monkeypatch):
    Models(monkeypatch, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly(report={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}},
                                   contract={"claims": {"policy": "judge", "on_unsupported": "degrade"}}))
    assert issuance["tier"] == "withheld"


async def test_a_contract_asking_for_judge_on_a_report_without_it_is_a_gap(monkeypatch):
    Models(monkeypatch)
    issuance = await issued(weekly(report={"claims": "require_citation"}, contract={"claims": "judge"}))
    assert issuance["tier"] == "degraded"
    [gap] = [g for g in issuance["gaps"] if "judge" in g]
    assert "「write」" in gap and "没有开" in gap and "这一版" not in gap
    assert issuance["claims"]["policy"] == "require_citation"


def test_a_run_in_flight_across_the_upgrade_keeps_the_old_gap():
    """升级前写的报告（产出里没有 claims）碰上写了 judge 的契约：照旧记「这一版还不支持」，不判档。"""
    from app.engine.nodes.io import _claims

    out = {"gaps": [], "claims_policy": None, "uncited": None, "claims": None, "unsupported": None}
    _claims(out, {"claims": {"policy": "judge", "on_unsupported": "withhold"}}, {"text": "x"}, evidence_on=True,
            uncited=[{"unit": "u1", "span": [0, 4], "text": "增长很快"}])
    assert out["claims_policy"] is None and out["claims"] is None and out["unsupported"] is None
    assert len(out["gaps"]) == 1 and "这一版还不支持" in out["gaps"][0]


async def test_runs_from_before_the_upgrade_ignore_judge(monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    models = Models(monkeypatch, text=UNCITED, verdicts={"新客": "unsupported"})
    issuance = await issued(weekly())
    assert models.judges == [] and issuance["tier"] == "formal" and "claims" not in issuance


# --------------------------------------------------------------------------
# 答案复核（review.scan）认得裁判的几条警告
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "shown"), [
    ("judge_limit", True), ("judge_failed", True), ("report_rewrite", True), ("report_rewrite_rejected", True),
    ("judge_unpriced", False), ("judge_same_model", False),
])
def test_review_knows_the_judge_warnings(code, shown):
    """没判完、没跑成、改写过的是缺口（要说明）；估不出金额、自己审自己只是配置上的提醒，轨迹里有就够了，
    不值得为它再叫一次复核模型。"""
    from app.engine.review import DEGRADED, scan

    signals = scan([{"type": "log", "data": {"level": "warn", "code": code, "message": f"{code} 的原话"}}],
                   {"answer": "本周销售额 45,678.5元。"})
    if shown:
        [signal] = signals
        assert signal.kind == code and signal.severity == DEGRADED and signal.detail == f"{code} 的原话"
    else:
        assert signals == []
