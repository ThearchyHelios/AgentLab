"""受管出具的证据门禁 G1–G5（governance.lint_for_publish）。

报告里的每个数都点得开出处，前提是这张图在结构上走得通：出口核对报告撰写节点的文档（G1）、
模型写的话不绕过报告直接进出口（G2）、报告节点按最严的规则自查（G3）、口径卡的输入不是沙箱里
算出来的（G4）、给口径卡供数的 agent 交的是逐格核对过的字段（G5）。

受管级别是硬错误、挡住发布；已发布级别只给警告。判定只看图和配置，不看运行结果；已经发布的
版本照常运行，下次发布时按新规则查。
"""
from __future__ import annotations

import asyncio
import copy

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.db.base import SessionLocal
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.main import app
from app.seed import TEMPLATES

#: 报告撰写节点在受管模板里要写明的三项
STRICT = {"numbers": "strict", "on_violation": "fail", "claims": "require_citation"}

G_CODES = {
    "governed.report_from_required", "governed.exit_text_source", "governed.text_bypass",
    "governed.report_policy", "governed.caliber_compute_input", "governed.caliber_agent_schema",
    "governed.caliber_agent_cite_fields", "governed.caliber_model_input",
}


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def traceable(*, report=None, fields=None, **contract_overrides) -> dict:
    """input → 查库 → 口径卡 → 报告撰写 → 出具（引用模式契约）：受管要求全部满足，包括 G1–G5。"""
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    contract.update(contract_overrides)
    nodes = [
        node("start", "input", "周期", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "tool", "取数", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "orders", "name": "订单数", "unit": "单", "expression": "cell(nodes.fetch, 0, 'orders')"}]),
        node("write", "report", "报告撰写", instructions="写周报", **(STRICT if report is None else report)),
        node("done", "output", "出具",
             fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}] if fields is None else fields,
             contract={k: v for k, v in contract.items() if v is not None}),
    ]
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    return {"nodes": nodes, "edges": edges}


def find(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)


def config(graph: dict, nid: str) -> dict:
    return find(graph, nid)["data"]["config"]


def insert(graph: dict, new: dict, *, before: str, after: str) -> dict:
    """把 new 接在 after 和 before 之间（原来的直连保留）。"""
    graph["nodes"].insert(len(graph["nodes"]) - 1, new)
    graph["edges"] += [{"source": after, "target": new["id"]}, {"source": new["id"], "target": before}]
    return graph


def lint(graph: dict, level: str = "governed") -> list:
    return lint_for_publish(GraphSpec.model_validate(graph), level=level).issues


def g_issues(graph: dict, level: str = "governed") -> list:
    return [i for i in lint(graph, level) if i.code in G_CODES]


def by_code(graph: dict, code: str, level: str = "governed") -> list:
    return [i for i in lint(graph, level) if i.code == code]


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def publish(client, graph: dict, level: str = "governed") -> dict:
    wf = (await client.post("/api/workflows", json={"name": "证据门禁-受管", "graph": graph})).json()
    body = (await client.post(f"/api/workflows/{wf['id']}/publish", json={"level": level})).json()
    await client.delete(f"/api/workflows/{wf['id']}")
    return body


def test_the_baseline_passes_every_rule():
    for level in ("governed", "published"):
        issues = publish_issues(GraphSpec.model_validate(traceable()), level=level)
        assert [i for i in issues if i.level == "error"] == [], [i.message for i in issues]
        assert not {i.code for i in issues} & G_CODES


def test_every_rule_carries_a_code_and_points_at_a_node():
    graph = traceable(report={}, narrative="{{ nodes.write.text }}", report_from=None)
    graph = insert(graph, node("calc", "code", "计算", code="print(1)", assign_to="calc"), before="card",
                   after="fetch")
    config(graph, "card")["metrics"].append({"id": "extra", "expression": "vars.calc"})
    issues = g_issues(graph)
    assert {i.code for i in issues} >= {"governed.report_from_required", "governed.report_policy",
                                        "governed.caliber_compute_input"}
    assert all(i.node_id and i.field for i in issues), [(i.code, i.node_id, i.field) for i in issues]


# --------------------------------------------------------------------------
# G1：带契约的出口核对报告撰写节点的文档，成果字段的文字只能来自报告撰写节点
# --------------------------------------------------------------------------


async def test_a_governed_contract_without_report_from_is_refused(client):
    # 旧的叙述模式：数字按数值回头匹配，出处可能不唯一
    graph = traceable(report_from=None, narrative="{{ nodes.write.text }}")
    [issue] = by_code(graph, "governed.report_from_required")
    assert issue.level == "error" and issue.node_id == "done" and issue.field == "contract.report_from"
    assert "report_from" in issue.message and "narrative" in issue.message
    body = await publish(client, graph)
    assert body["ok"] is False
    assert "governed.report_from_required" in {i["code"] for i in body["issues"] if i["level"] == "error"}
    # 两样都没写的，照旧由 contract.report_from_missing 报，不重复报一条
    bare = traceable(report_from=None)
    assert by_code(bare, "governed.report_from_required") == []
    assert (await publish(client, bare))["ok"] is False


@pytest.mark.parametrize("producer, label", [
    (node("narrate", "llm", "叙述", prompt="写周报", assign_to="story"), "模型调用"),
    (node("shape", "transform", "整形", mode="template", template="{{ nodes.card.text }}", assign_to="story"),
     "数据整形"),
    (node("calc", "code", "计算", code="print('x')", assign_to="story"), "沙箱代码"),
])
@pytest.mark.parametrize("ref", ["{{ nodes.%s.text }}", "{{ vars.story }}"])
def test_an_exit_field_taking_text_from_a_non_report_node_is_named(producer, label, ref):
    graph = insert(traceable(), producer, before="done", after="write")
    config(graph, "done")["fields"].append({"name": "摘要", "value": "本周：" + ref % producer["id"]
                                            if "%s" in ref else "本周：" + ref})
    [issue] = by_code(graph, "governed.exit_text_source")
    assert issue.level == "error" and issue.node_id == "done" and issue.field == "fields[1].value"
    assert "「摘要」" in issue.message and f"「{producer['data']['label']}」（{label}）" in issue.message
    assert [i.level for i in by_code(graph, "governed.exit_text_source", "published")] == ["warning"]


@pytest.mark.parametrize("value", [
    "{{ nodes.write.text }}", "{{ vars.report }}", "{{ nodes.write.stats | json }}", "{{ nodes.card.text }}",
    "{{ input.week }}", "{{ vars.week }}", "{{ nodes.fetch }}", "{{ usage.total_tokens }}", "由系统生成，仅供内部参考",
    "{{ last_message }}",
])
def test_deterministic_sources_and_report_text_are_fine(value):
    graph = traceable()
    config(graph, "write")["assign_to"] = "report"
    config(graph, "done")["fields"].append({"name": "附", "value": value})
    assert g_issues(graph) == [], [i.message for i in g_issues(graph)]


def test_a_source_code_node_is_a_data_source_but_a_compute_one_is_not():
    graph = insert(traceable(), node("raw", "code", "取原始数据", code="print(1)", evidence_role="source"),
                   before="done", after="fetch")
    config(graph, "done")["fields"].append({"name": "原始数据", "value": "{{ nodes.raw }}"})
    assert by_code(graph, "governed.exit_text_source") == []
    config(graph, "raw")["evidence_role"] = "compute"
    assert len(by_code(graph, "governed.exit_text_source")) == 1


def test_an_exit_without_fields_hands_over_the_last_message():
    # 没配字段就把最后一条消息当成果：最后一条是报告的，没问题；是模型调用写的，就绕过了报告
    graph = traceable(fields=[])
    assert g_issues(graph) == []
    graph = insert(graph, node("polish", "llm", "润色", prompt="{{ nodes.write.text }}"), before="done",
                   after="write")
    graph["edges"] = [e for e in graph["edges"] if (e["source"], e["target"]) != ("write", "done")]
    [issue] = by_code(graph, "governed.exit_text_source")
    assert issue.field == "fields" and "最后一条消息" in issue.message and "「润色」" in issue.message
    # 出口没有契约时 G1 不管，G2 照样追到这段绕过报告的文字
    config(graph, "done").pop("contract")
    assert [i.node_id for i in by_code(graph, "governed.text_bypass")] == ["polish"]


# --------------------------------------------------------------------------
# G2：模型写的文字没经过报告撰写节点，从图上追到出口
# --------------------------------------------------------------------------


def test_an_llm_bypassing_the_report_is_caught_through_intermediate_nodes():
    graph = traceable()
    graph = insert(graph, node("narrate", "llm", "叙述", prompt="写周报", assign_to="story"), before="done",
                   after="card")
    graph = insert(graph, node("shape", "transform", "整形", mode="template",
                               template="【周报】{{ vars.story }}", assign_to="shaped"), before="done", after="narrate")
    config(graph, "done")["fields"].append({"name": "摘要", "value": "{{ vars.shaped }}"})
    [bypass] = by_code(graph, "governed.text_bypass")
    assert bypass.level == "error" and bypass.node_id == "narrate"
    assert "「叙述」（模型调用）" in bypass.message and "「出具」" in bypass.message and "「整形」" in bypass.message
    # 出口字段点的是整形节点：G1 点名字段，G2 点名写文字的那个模型节点，两处各报各的
    assert [i.field for i in by_code(graph, "governed.exit_text_source")] == ["fields[1].value"]


def test_a_direct_reference_in_a_contract_exit_is_left_to_g1():
    graph = insert(traceable(), node("narrate", "llm", "叙述", prompt="写周报"), before="done", after="card")
    config(graph, "done")["fields"].append({"name": "摘要", "value": "{{ nodes.narrate.text }}"})
    assert len(by_code(graph, "governed.exit_text_source")) == 1
    assert by_code(graph, "governed.text_bypass") == []


def test_every_exit_counts_at_both_levels():
    graph = traceable()
    graph["nodes"] += [node("chat", "llm", "闲聊", prompt="你好"),
                       node("side", "output", "附带成果", fields=[{"name": "回答", "value": "{{ nodes.chat.text }}"}])]
    graph["edges"] += [{"source": "start", "target": "chat"}, {"source": "chat", "target": "side"}]
    [issue] = by_code(graph, "governed.text_bypass")
    assert issue.node_id == "chat" and "「附带成果」" in issue.message and issue.level == "error"
    # 已发布级别范围一样，只是轻重不同：没带契约的出口也给一条警告（用户拍板：G 规则受管挡、已发布警告）
    [issue] = by_code(graph, "governed.text_bypass", "published")
    assert issue.node_id == "chat" and issue.level == "warning"


def test_text_that_goes_through_a_report_does_not_bypass_it():
    graph = insert(traceable(), node("notes", "agent", "查数员", tools=["db_query__shop"], prompt="查 orders"),
                   before="write", after="fetch")
    config(graph, "write")["notes_from"] = ["notes"]
    assert g_issues(graph) == []


@pytest.mark.parametrize("value, flagged", [
    ("{{ vars.facts.gmv }}", False),          # 开了 cite_fields：assign_to 拿到的是逐格核对过的字段
    ("{{ nodes.ask.data.gmv }}", False),
    ("{{ nodes.ask.text }}", True),           # 它的原话照样是模型写的
])
def test_cited_agent_fields_are_data_not_text(value, flagged):
    graph = insert(traceable(), node("ask", "agent", "查数员", tools=["db_query__shop"], prompt="查 orders",
                                     output_schema={"type": "object", "properties": {"gmv": {"type": "number"}}},
                                     cite_fields=True, assign_to="facts"), before="done", after="fetch")
    graph = insert(graph, node("shape", "transform", "整形", mode="template", template=value, assign_to="x"),
                   before="done", after="ask")
    graph["nodes"].append(node("side", "output", "附带成果", fields=[{"name": "值", "value": "{{ vars.x }}"}]))
    graph["edges"].append({"source": "shape", "target": "side"})
    assert bool(by_code(graph, "governed.text_bypass")) is flagged


# --------------------------------------------------------------------------
# G3：报告撰写节点按最严的规则自查，并显式声明结论句策略
# --------------------------------------------------------------------------


def test_a_report_node_must_spell_out_the_strict_settings():
    graph = traceable(report={})
    [issue] = by_code(graph, "governed.report_policy")
    assert issue.level == "error" and issue.node_id == "write" and issue.field == "numbers"
    for piece in ("numbers: strict", "on_violation: fail", "claims: require_citation"):
        assert piece in issue.message
    assert [i.level for i in by_code(graph, "governed.report_policy", "published")] == ["warning"]


@pytest.mark.parametrize("key, value", [("numbers", "off"), ("on_violation", "flag"), ("claims", "off")])
def test_a_looser_setting_is_named(key, value):
    graph = traceable(report={**STRICT, key: value})
    [issue] = by_code(graph, "governed.report_policy")
    assert issue.field == key and f"现在是 {value}" in issue.message
    for other in set(STRICT) - {key}:
        assert f"{other}:" not in issue.message


def test_graph_defaults_count_like_they_do_at_run_time():
    graph = traceable(report={"claims": "require_citation"})
    graph["defaults"] = {"numbers": "strict", "on_violation": "fail"}
    assert by_code(graph, "governed.report_policy") == []
    graph["defaults"]["on_violation"] = "flag"
    assert [i.field for i in by_code(graph, "governed.report_policy")] == ["on_violation"]


def test_an_unsupported_claims_value_is_left_to_validate():
    graph = traceable(report={**STRICT, "claims": "sometimes"})
    assert by_code(graph, "governed.report_policy") == []
    [bad] = [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.code == "report.claims_invalid"]
    assert bad.level == "error" and "off / require_citation" in bad.message


# --------------------------------------------------------------------------
# G4：口径卡的输入不能来自计算角色的沙箱代码
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", [None, "compute"])
def test_compute_code_feeding_a_caliber_card_is_refused(role):
    graph = traceable()
    extra = {} if role is None else {"evidence_role": role}
    graph = insert(graph, node("calc", "code", "计算", code="print(1)", assign_to="calc", **extra), before="card",
                   after="fetch")
    config(graph, "card")["metrics"].append({"id": "ratio", "expression": "vars.calc.ratio"})
    [issue] = by_code(graph, "governed.caliber_compute_input")
    assert issue.level == "error" and issue.node_id == "calc" and issue.field == "evidence_role"
    assert "「周报口径卡」" in issue.message and "source" in issue.message
    assert [i.level for i in by_code(graph, "governed.caliber_compute_input", "published")] == ["warning"]
    config(graph, "calc")["evidence_role"] = "source"
    assert by_code(graph, "governed.caliber_compute_input") == []


def test_compute_code_is_caught_behind_a_transform():
    graph = insert(traceable(), node("calc", "code", "计算", code="print(1)"), before="card", after="fetch")
    graph = insert(graph, node("shape", "transform", "整形", mode="json", template="{{ nodes.calc.stdout }}",
                               assign_to="shaped"), before="card", after="calc")
    config(graph, "card")["metrics"].append({"id": "ratio", "expression": "vars.shaped.ratio"})
    assert [i.node_id for i in by_code(graph, "governed.caliber_compute_input")] == ["calc"]


# --------------------------------------------------------------------------
# G5：给口径卡供数的 agent 配 output_schema 并开 cite_fields
# --------------------------------------------------------------------------


SCHEMA = {"type": "object", "properties": {"gmv": {"type": "number"}}}


def feeding_agent(**config_) -> dict:
    graph = insert(traceable(), node("ask", "agent", "查数员", tools=["db_query__shop"], prompt="查 orders",
                                     assign_to="facts", **config_), before="card", after="start")
    config(graph, "card")["metrics"].append({"id": "gmv2", "expression": "vars.facts.gmv"})
    return graph


def test_an_agent_feeding_a_card_needs_an_output_schema():
    [issue] = by_code(feeding_agent(), "governed.caliber_agent_schema")
    assert issue.level == "error" and issue.node_id == "ask" and issue.field == "output_schema"
    assert "「周报口径卡」" in issue.message


def test_an_agent_with_a_schema_needs_cite_fields():
    graph = feeding_agent(output_schema=SCHEMA)
    [issue] = by_code(graph, "governed.caliber_agent_cite_fields")
    assert issue.node_id == "ask" and issue.field == "cite_fields"
    assert by_code(graph, "governed.caliber_agent_schema") == []
    assert g_issues(feeding_agent(output_schema=SCHEMA, cite_fields=True)) == []


def test_an_agent_that_only_feeds_the_report_is_not_a_card_input():
    graph = insert(traceable(), node("ask", "agent", "查数员", tools=["db_query__shop"], prompt="查 orders"),
                   before="write", after="fetch")
    assert by_code(graph, "governed.caliber_agent_schema") == []


def test_a_model_call_feeding_a_card_is_refused():
    graph = insert(traceable(), node("guess", "llm", "估算", prompt="估一个数", assign_to="g"), before="card",
                   after="start")
    config(graph, "card")["metrics"].append({"id": "g", "expression": "vars.g"})
    [issue] = by_code(graph, "governed.caliber_model_input")
    assert issue.node_id == "guess" and "「估算」（模型调用）" in issue.message


# --------------------------------------------------------------------------
# 已发布级别只给警告；已经发布的版本照常运行
# --------------------------------------------------------------------------


def everything_wrong() -> dict:
    graph = feeding_agent()
    graph = insert(graph, node("calc", "code", "计算", code="print(1)", assign_to="calc"), before="card",
                   after="fetch")
    config(graph, "card")["metrics"].append({"id": "ratio", "expression": "vars.calc.ratio"})
    graph = insert(graph, node("narrate", "llm", "叙述", prompt="写周报", assign_to="story"), before="done",
                   after="card")
    graph = insert(graph, node("shape", "transform", "整形", mode="template", template="{{ vars.story }}",
                               assign_to="shaped"), before="done", after="narrate")
    config(graph, "done")["fields"].append({"name": "摘要", "value": "{{ vars.shaped }}"})
    config(graph, "done")["contract"].update(report_from=None, narrative="{{ nodes.write.text }}")
    config(graph, "write").clear()
    return graph


async def test_the_published_level_only_warns(client):
    graph = everything_wrong()
    assert {i.code for i in g_issues(graph)} == G_CODES - {"governed.caliber_agent_cite_fields",
                                                           "governed.caliber_model_input"}
    assert all(i.level == "error" for i in g_issues(graph))
    warned = g_issues(graph, "published")
    assert {i.code for i in warned} == {i.code for i in g_issues(graph)}
    assert all(i.level == "warning" for i in warned)
    assert (await publish(client, graph, "published"))["ok"] is True
    assert (await publish(client, graph, "governed"))["ok"] is False


def test_template_nine_passes_once_completed():
    tpl = next(t for t in TEMPLATES if t["name"].startswith("⑨"))
    graph = copy.deepcopy({"nodes": tpl["nodes"], "edges": tpl["edges"]})
    # 模板原样：G 规则里至多差报告撰写节点那三项
    assert {i.code for i in g_issues(graph)} <= {"governed.report_policy"}
    next(n for n in graph["nodes"] if n["type"] == "report")["data"]["config"].update(STRICT)
    issues = publish_issues(GraphSpec.model_validate(graph), level="governed")
    assert [i for i in issues if i.level == "error"] == [], [i.message for i in issues if i.level == "error"]
    assert not {i.code for i in issues} & G_CODES


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def test_an_already_governed_version_keeps_running_formally(client, engine_up, monkeypatch):
    """升级前按受管发布的版本（报告节点没写那三项）照常发起正式运行；再发布时才按新规则查。"""
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="本周销售额 [[m:gmv]]。[[see:m:gmv]]"))
    graph = traceable(report={})
    card = config(graph, "card")
    card["metrics"] = [{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]
    graph["nodes"][1] = node("fetch", "transform", "取数", mode="expression", expression="{'gmv': 300.5}",
                             assign_to="kpi")
    async with SessionLocal() as session:
        wf = Workflow(name="证据门禁-老受管版本", graph=graph, status="governed", published_version=1)
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
    # 下次发布按新规则：报告节点那三项没写明，受管发布被拦
    check = (await client.post(f"/api/workflows/{wf_id}/publish-check", json={"level": "governed"})).json()
    assert check["ok"] is False
    assert "governed.report_policy" in {i["code"] for i in check["issues"] if i["level"] == "error"}


# --------------------------------------------------------------------------
# validate：报告撰写节点的 claims 只接受 off / require_citation / judge（四期放开 judge）
# --------------------------------------------------------------------------


def claims_issues(value, *, on_defaults: bool = False) -> list:
    graph = traceable(report={} if on_defaults else {"claims": value})
    if on_defaults:
        graph["defaults"] = {"claims": value}
    return [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.code == "report.claims_invalid"]


@pytest.mark.parametrize("value", [None, "", "off", "require_citation"])
def test_supported_claims_values_pass(value):
    assert claims_issues(value) == []


def test_judge_is_supported_now():
    assert claims_issues("judge") == []
    assert claims_issues("judge", on_defaults=True) == []


@pytest.mark.parametrize("value", ["strict", "Require_Citation", 1, ["off"], {"policy": "judge"}])
def test_other_claims_values_are_errors(value):
    [issue] = claims_issues(value)
    assert issue.level == "error" and "off / require_citation" in issue.message and repr(value) in issue.message


def test_claims_from_graph_defaults_are_checked_too():
    [issue] = claims_issues("sometimes", on_defaults=True)
    assert issue.node_id == "write"
