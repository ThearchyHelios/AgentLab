"""门禁 G3 加的一条：受管级别的报告撰写节点要把分析说法核对设成严格（00-总方案 §3.5，D8）。

- 没写（运行时是重写一次后标出）、写了 check 或 off：受管级别挡住发布（error），已发布级别只警告；写错的值交给校验
- 自动修复一键写明；一键升级成受管、按口径出具起草的报告节点照同一份要求写（REPORT_POLICY）
- 已经发布的受管版本照常发起正式运行（报告节点按缺省的 check 核、只标注），下次发布才按新规则查
- 白话里说到分析说法
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.db.base import SessionLocal
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.autofix import apply_fixes, plan_fixes
from app.engine.governance import REPORT_POLICY, lint_for_publish, publish_issues
from app.engine.labels import field_label, option_label
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.main import app
from app.plain import plain_of

STRICT = {"numbers": "strict", "on_violation": "fail", "claims": "require_citation", "analysis_claims": "strict"}


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def traceable(report: dict) -> dict:
    """input → 查库 → 口径卡 → 报告撰写 → 出具（引用模式契约）：别的受管要求都满足。"""
    nodes = [
        node("start", "input", "周期", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "报告撰写", instructions="写周报", **report),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def policy_issues(graph: dict, level: str = "governed") -> list:
    return [i for i in lint_for_publish(GraphSpec.model_validate(graph), level=level).issues
            if i.code == "governed.report_policy"]


def test_the_rule_is_part_of_the_report_policy():
    assert ("analysis_claims", "strict") in [(k, v) for k, v, _ in REPORT_POLICY]


@pytest.mark.parametrize("value, now", [(None, "当前未设置"), ("check", "当前为「重写一次后标出」"),
                                        ("off", "当前为「不核对」")])
def test_a_governed_report_must_check_analysis_claims_strictly(value, now):
    report = {k: v for k, v in STRICT.items() if k != "analysis_claims"}
    if value is not None:
        report["analysis_claims"] = value
    [issue] = policy_issues(traceable(report))
    assert issue.level == "error" and issue.node_id == "write" and issue.field == "analysis_claims"
    assert f"「{field_label('analysis_claims')}」设为「{option_label('analysis_claims', 'strict')}」（{now}）" \
        in issue.message
    assert "「未引用的数字」" not in issue.message, "别的几项都写明了，不再点名"
    assert [i.level for i in policy_issues(traceable(report), "published")] == ["warning"]


def test_strict_passes_and_graph_defaults_count():
    assert policy_issues(traceable(STRICT)) == []
    graph = traceable({k: v for k, v in STRICT.items() if k != "analysis_claims"})
    graph["defaults"] = {"analysis_claims": "strict"}
    assert policy_issues(graph) == []


def test_a_bad_value_is_left_to_validation():
    graph = traceable({**STRICT, "analysis_claims": "loose"})
    assert policy_issues(graph) == []
    [bad] = [i for i in validate_graph(GraphSpec.model_validate(graph)).issues
             if i.code == "report.analysis_claims_invalid"]
    assert bad.level == "error" and bad.field == "analysis_claims"


def test_the_autofix_writes_it_in():
    graph = traceable({k: v for k, v in STRICT.items() if k != "analysis_claims"})
    spec = GraphSpec.model_validate(graph)
    fixes = {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level="governed"), level="governed")}
    fix = fixes["governed.report_policy:write"]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "analysis_claims", "before": None, "after": "strict"}
    out = apply_fixes(graph, [fix["id"]], level="governed")
    assert out["ok"] is True
    write = next(n for n in out["graph"]["nodes"] if n["id"] == "write")
    assert write["data"]["config"]["analysis_claims"] == "strict"


def test_an_unrecognized_value_is_left_to_its_own_fix_like_claims():
    """analysis_claims 写了认不出的值：门禁 G3 跳过、交给校验报，自动修复的 G3 那一条也不碰它（以前顺手改成了 strict），
    由校验那条问题自己的修复管——照 claims 的做法：受管级别改成 strict，别的级别三档要人选。"""
    graph = traceable({**STRICT, "numbers": "off", "analysis_claims": "loose"})
    spec = GraphSpec.model_validate(graph)
    fixes = {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level="governed"), level="governed")}
    assert fixes["governed.report_policy:write"]["preview"] == {"field": "numbers", "before": "off", "after": "strict"}
    bad = fixes["report.analysis_claims_invalid:write"]
    assert bad["kind"] == "auto" and bad["preview"] == {"field": "analysis_claims", "before": "loose", "after": "strict"}
    out = apply_fixes(graph, list(fixes), level="governed")
    write = next(n for n in out["graph"]["nodes"] if n["id"] == "write")["data"]["config"]
    assert out["ok"] is True and write["analysis_claims"] == "strict" and write["numbers"] == "strict"

    published = traceable({"analysis_claims": "loose"})
    spec = GraphSpec.model_validate(published)
    fid = "report.analysis_claims_invalid:write"
    [fix] = [f for f in plan_fixes(spec, publish_issues(spec, level="published"), level="published") if f["id"] == fid]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == ["off", "check", "strict"]
    assert [o["label"] for o in fix["options"]] == [option_label("analysis_claims", v) for v in ("off", "check", "strict")]
    assert "default" not in fix
    out = apply_fixes(published, [fid], {fid: "check"}, level="published")
    assert out["applied"] == [fid] and next(n for n in out["graph"]["nodes"] if n["id"] == "write")[
        "data"]["config"]["analysis_claims"] == "check"


def test_a_governed_upgrade_writes_the_strict_setting():
    from app.engine.upgrade import _policy

    assert _policy("governed", {"numbers": "strict"}, {})["analysis_claims"] == "strict"
    assert _policy("governed", {"analysis_claims": "strict"}, {}).get("analysis_claims") is None
    assert _policy("published", {}, {}) == {}


def test_the_plain_wording_mentions_analysis_claims():
    plain = plain_of("governed.report_policy")
    assert "分析结论" in plain.why and "分析说法核对" in plain.action


def test_template_nine_stays_publishable_as_governed():
    from app.seed import TEMPLATES

    tpl = next(t for t in TEMPLATES if t["name"].startswith("⑨"))
    graph = copy.deepcopy({"nodes": tpl["nodes"], "edges": tpl["edges"]})
    assert next(n for n in graph["nodes"] if n["type"] == "report")["data"]["config"]["analysis_claims"] == "strict"
    assert policy_issues(graph) == []


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_an_already_governed_version_without_the_setting_keeps_running(client, engine_up, monkeypatch):
    """升级前按受管发布的版本（报告节点没写分析说法核对）照常发起正式运行，按缺省的 check 核；再发布时才按新规则查。"""
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv]]"))
    graph = traceable({k: v for k, v in STRICT.items() if k != "analysis_claims"})
    graph["nodes"][1] = node("fetch", "transform", "取数", mode="expression", expression="{'gmv': 300.5}",
                             assign_to="kpi")
    graph["nodes"][2]["data"]["config"]["metrics"] = [
        {"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]
    async with SessionLocal() as session:
        wf = Workflow(name="分析说法-老受管版本", graph=graph, status="governed", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, level="governed"))
        await session.commit()
        wf_id = wf.id
    r = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert r.status_code == 201, r.text
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, r.json()["id"])
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    declared = row.output["_issuance"]["analysis_claims"]
    assert declared["mode"] == "check" and declared["counted"] is False
    check = (await client.post(f"/api/workflows/{wf_id}/publish-check", json={"level": "governed"})).json()
    assert check["ok"] is False
    assert any(i["code"] == "governed.report_policy" and i["field"] == "analysis_claims"
               for i in check["issues"] if i["level"] == "error")


@pytest.mark.parametrize("after", ["check", "off", None])
def test_loosening_analysis_claims_from_strict_is_refused(after):
    from app.engine.autofix import forbidden_changes

    old = traceable(STRICT)
    new = copy.deepcopy(old)
    write = next(n for n in new["nodes"] if n["id"] == "write")["data"]["config"]
    if after is None:
        write.pop("analysis_claims")
    else:
        write["analysis_claims"] = after
    [reason] = forbidden_changes(old, new)
    assert "「报告撰写」" in reason and "「分析说法核对」" in reason and "「重写后仍有的计入缺口」" in reason
    # 往严里改不算
    assert forbidden_changes(new, old) == []
