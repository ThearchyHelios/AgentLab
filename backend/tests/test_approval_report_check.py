"""人工审批审的是报告时，审批卡附上这份报告的核对概况（human 节点的请求里带 reports）。

真实运行里：报告里有一句结论和证据相矛盾，审批人批准时完全不知道——报告节点只把正文交给了审批卡，几个数字有出处、
结论句判没判过，一个字都没有。用户事后在证据面板点开那句，裁判才判它相矛盾。

现在 human 节点挂起时，上游的报告节点（直接连着的，或者标题、说明里引用了它正文的）各带一份核对概况：
数字核对的统计（report.checked 的 stats）、结论句策略、裁判摘要（几句判了、几句相矛盾），以及报告文档的工件 id，
审批卡据此写「N 个数字都有出处」「结论句已裁判 X 句，证据相矛盾 Y 句」或「结论句尚未核对」，并给出打开证据的入口。

表名、指标、数字都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent, Setting
from app.engine import judge as judge_mod
from app.engine.judge import JUDGE_ROLE, SPEND_KEY
from app.engine.runner import run_manager
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
TEXT = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
def models(monkeypatch):
    """写作者回 TEXT；裁判把「新客」那句判成证据相矛盾，其余有依据。"""
    def decide(model, messages):
        if JUDGE_ROLE in str(messages[0].content):
            request = str(messages[-1].content)
            items = [{"unit": uid, "verdict": "contradicted" if "新客" in line else "supported",
                      "rationale": "虚构的理由", "used": []}
                     for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M)]
            return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
        return AIMessage(content=TEXT)

    monkeypatch.setattr(MockChatModel, "_decide", decide)

    async def judge_model_of(session, spec):
        return MockChatModel(model_name=PRICED), PRICED

    monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def graph(*, gate: dict, wire_report_to_gate: bool = True, **report):
    nodes = [
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", "写周报", instructions="写周报", **report),
        node("note", "transform", mode="expression", expression="'ok'", assign_to="note"),
        node("gate", "human", "主管审批", mode="approve", **gate),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    chain = ["start", "fetch", "caliber", "write"] + (["gate"] if wire_report_to_gate else ["note", "gate"])
    edges = [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]
    edges.append({"source": "gate", "target": "out", "sourceHandle": "approved"})
    return {"nodes": nodes, "edges": edges}


async def paused(g, *, run_class: str) -> tuple[Run, Approval]:
    started = await run_manager.start(graph=g, input_payload={}, run_class=run_class)
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, started.id)
            if row.status in ("interrupted", "failed", "succeeded"):
                approval = (await session.execute(select(Approval).where(Approval.run_id == row.id))).scalar_one_or_none()
                if row.status != "interrupted" or approval is not None:
                    break
    assert row.status == "interrupted", row.error
    assert approval is not None
    return row, approval


async def test_formal_run_carries_the_judged_claims_to_the_approval():
    row, approval = await paused(graph(gate={"title": "审核周报", "message": "{{ nodes.write.text }}"},
                                       claims="judge"), run_class="formal")
    [report] = approval.payload["reports"]
    assert report["node_id"] == "write" and report["label"] == "写周报"
    assert isinstance(report["doc_artifact"], str) and report["doc_artifact"]
    assert report["stats"]["numbers"] == report["stats"]["numbers_cited"] == 1
    assert report["claims"] == "judge"
    judge = report["judge"]
    assert judge["mode"] == "inline"
    assert judge["counts"]["contradicted"] == 1 and judge["counts"]["supported"] == 1
    assert judge["counts"]["unjudged"] == 0
    # 和 report.checked 是同一份：审批卡写的和时间线那一行对得上
    async with SessionLocal() as session:
        [checked] = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "report.checked"))).scalars())
    assert checked.data["doc_artifact"] == report["doc_artifact"]
    assert {k: checked.data["stats"][k] for k in report["stats"]} == report["stats"]
    assert checked.data["judge"]["counts"] == judge["counts"]
    # human.requested 事件里也带着，回放时界面一样读得到
    async with SessionLocal() as session:
        [requested] = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "human.requested"))).scalars())
    assert requested.data["reports"] == approval.payload["reports"]


async def test_exploratory_report_under_approval_is_judged_in_the_node():
    """要交给人审批的报告，探索运行也在节点里判：审批人据此拍板，批准前就该知道哪句结论站不住。
    真实运行里探索运行的报告有一句和证据相矛盾，审批卡只能写「结论句尚未核对」——按需裁判要等封存
    （运行结束）之后才能判，而封存前放开按需裁判会动证据清单的完整性约束，所以改在节点里当场判。
    没有审批的探索运行照旧按需判，不在节点里花这笔钱（test_contract_judge.test_explore_runs_only_annotate）。"""
    _, approval = await paused(graph(gate={"title": "审核周报"}, claims="judge"), run_class="exploratory")
    [report] = approval.payload["reports"]
    assert report["claims"] == "judge" and report["judge"]["mode"] == "inline"
    assert report["judge"]["counts"]["contradicted"] == 1 and report["judge"]["counts"]["unjudged"] == 0


async def test_report_quoted_in_the_approval_message_is_judged_in_the_node_too():
    _, approval = await paused(graph(gate={"title": "审核", "message": "请看：{{ nodes.write.text }}"},
                                     wire_report_to_gate=False, claims="judge"), run_class="exploratory")
    [report] = approval.payload["reports"]
    assert report["judge"]["mode"] == "inline" and report["judge"]["counts"]["contradicted"] == 1


async def test_report_without_claims_policy_still_reports_numbers():
    _, approval = await paused(graph(gate={"title": "审核周报"}), run_class="exploratory")
    [report] = approval.payload["reports"]
    assert report["claims"] == "off" and "judge" not in report
    assert report["stats"]["numbers"] == 1


async def test_a_report_quoted_in_the_message_counts_even_when_not_wired_directly():
    _, approval = await paused(graph(gate={"title": "审核", "message": "请看：{{ nodes.write.text }}"},
                                     wire_report_to_gate=False), run_class="exploratory")
    assert [r["node_id"] for r in approval.payload["reports"]] == ["write"]


async def test_a_report_quoted_through_its_variable_counts_too():
    _, approval = await paused(graph(gate={"title": "审核", "message": "{{ vars.weekly }}"}, wire_report_to_gate=False,
                                     assign_to="weekly"), run_class="exploratory")
    assert [r["node_id"] for r in approval.payload["reports"]] == ["write"]


async def test_approvals_unrelated_to_a_report_carry_nothing():
    _, approval = await paused(graph(gate={"title": "确认", "message": "继续吗"}, wire_report_to_gate=False),
                               run_class="exploratory")
    assert "reports" not in approval.payload
