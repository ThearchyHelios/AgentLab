"""报告节点的结论句策略 claims：off（默认，和以前一样）/ require_citation（没挂依据的结论句计入缺口）。

结论句要靠裁判模型判断支不支持，那是后续版本的事；这一版只做确定性的一半：
- require_citation 时写作提示明确要求每句结论挂 [[see:…]]；没挂的照常产出、计数，
  出口按出具档位降档（io.py），报告节点自己不为它失败、不为它重写
- report.checked 事件和节点产出都带上 claims，出口据此判档
- 写 judge 时节点直接报错「结论句裁判在后续版本支持」，不能悄悄当成 off
- 升级前发起的运行一个字都不变
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.evidence import CLAIMS_RULE, build_catalog, compose_doc, uncited_claims
from app.engine.runner import run_manager
from app.engine.schema import CLAIMS_LATER, REPORT_CLAIMS, claims_problem


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 第一句挂了依据，第二句是没挂依据的结论（有方向词），第三句是连接性的话
TEXT = "本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客。下面看细节。"


def weekly(**report):
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def script(monkeypatch, reply=TEXT) -> list:
    from app.providers import mock_model

    seen: list = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=reply)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


def lenient(monkeypatch):
    """装作画布校验没拦住（校验以后放宽、或者绕过校验发起的运行）：节点执行时自己也要守住这一关，
    报的是同一句话。"""
    original = runner_mod.validate_graph

    def permissive(spec):
        report = original(spec)
        report.issues = [i for i in report.issues if i.field != "claims"]
        report.ok = not any(i.level == "error" for i in report.issues)
        return report

    monkeypatch.setattr(runner_mod, "validate_graph", permissive)


async def finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype).order_by(RunEvent.seq)
        return list((await session.execute(q)).scalars())


async def checked_and_output(run_id: str) -> tuple[dict, dict]:
    [checked] = await events(run_id, "report.checked")
    finished = [e for e in await events(run_id, "node.finished") if e.node_id == "write"][0]
    return checked.data, artifact_store.load(finished.data["artifact"])


def prompt_of(seen: list) -> str:
    return "\n".join(str(m.content) for m in seen[0])


async def test_default_is_off_and_changes_nothing(monkeypatch):
    seen = script(monkeypatch)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    checked, output = await checked_and_output(row.id)
    assert checked["claims"] == "off" and output["claims"] == "off"
    assert CLAIMS_RULE not in prompt_of(seen)
    assert checked["stats"]["uncited_claims"] == 1


async def test_require_citation_asks_for_support_but_does_not_block(monkeypatch):
    seen = script(monkeypatch)
    row = await finish(weekly(claims="require_citation", on_violation="fail"))
    assert row.status == "succeeded", row.error
    assert len(seen) == 1, "没挂依据的结论句不触发重写"
    assert CLAIMS_RULE in prompt_of(seen)
    checked, output = await checked_and_output(row.id)
    assert checked["claims"] == "require_citation" and output["claims"] == "require_citation"
    assert checked["ok"] is True and checked["violations"] == []
    assert checked["stats"]["uncited_claims"] == output["stats"]["uncited_claims"] == 1
    doc = artifact_store.load(checked["doc_artifact"])
    [flagged] = uncited_claims(doc)
    assert flagged["text"] == "增长主要来自新客。" and doc["markdown"][slice(*flagged["span"])] == flagged["text"]


async def test_judge_is_refused_until_it_exists(monkeypatch):
    seen = script(monkeypatch)
    with pytest.raises(ValueError) as info:            # 画布校验先拦（E3-gov 的 validate）
        await run_manager.start(graph=weekly(claims="judge"), input_payload={})
    assert CLAIMS_LATER in str(info.value)
    lenient(monkeypatch)
    row = await finish(weekly(claims="judge"))
    assert row.status == "failed" and claims_problem("judge") in (row.error or ""), row.error
    assert seen == [], "不支持的策略不该先花一次模型调用"


@pytest.mark.parametrize("value", ["sometimes", 1, ["require_citation"]])
async def test_other_values_are_refused(monkeypatch, value):
    script(monkeypatch)
    lenient(monkeypatch)
    row = await finish(weekly(claims=value))
    assert row.status == "failed", row.error
    assert claims_problem(value) in row.error and "require_citation" in row.error


async def test_runs_from_before_the_upgrade_are_untouched(monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    seen = script(monkeypatch)
    row = await finish(weekly())
    assert row.status == "succeeded", row.error
    checked, output = await checked_and_output(row.id)
    assert "claims" not in checked and "claims" not in output
    assert CLAIMS_RULE not in prompt_of(seen)


# --------------------------------------------------------------------------
# 出口和界面共用的纯函数
# --------------------------------------------------------------------------


def test_the_supported_policies_match_the_validator():
    from app.engine.nodes.report import CLAIMS, report_claims
    from app.engine.schema import GraphSpec

    assert CLAIMS == REPORT_CLAIMS == ("off", "require_citation")
    spec = GraphSpec.model_validate(weekly(claims="require_citation"))
    assert report_claims(spec, spec.node_map()["write"]) == "require_citation"
    spec = GraphSpec.model_validate(weekly())
    assert report_claims(spec, spec.node_map()["write"]) == "off"
    spec.defaults["claims"] = "require_citation"          # 图级 defaults 同样生效，和执行时一个规则
    assert report_claims(spec, spec.node_map()["write"]) == "require_citation"


def test_uncited_claims_lists_claim_units_without_support():
    catalog = build_catalog(nodes={"caliber": {"kind": "metric_set", "caliber": "c", "caliber_version": "v1",
                                            "artifact": "f" * 64,
                                            "metrics": [{"id": "gmv", "name": "销售额", "value": 1, "unit": ""}]}})
    doc = compose_doc("销售额 [[m:gmv]]。环比下降。[[see:m:gmv]]主要来自东区。连接的话。", catalog)
    assert [u["text"] for u in uncited_claims(doc)] == ["主要来自东区。"]
    assert all(set(u) == {"unit", "span", "text"} for u in uncited_claims(doc))
