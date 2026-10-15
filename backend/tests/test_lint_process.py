"""流程相关的检查（engine/lint_process.py，设计文档 §3.13）。

human.*：人工节点的流程配置写得不成立，运行时没法照着执行——validate_graph 报错误，各级发布、发起运行都挡。
process.*：图能运行，按流程使用才出事——发布门禁里一律警告，应用把它作为流程钉住时由应用检查升成错误。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.lint_process import load_subgraphs, parse_duration, with_subgraphs
from app.engine.schema import GraphSpec, validate_graph


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def spec(*nodes) -> GraphSpec:
    nodes = [node("start", "input", fields=[{"name": "q"}]), *nodes, node("out", "output", fields=[])]
    return GraphSpec.model_validate({"nodes": nodes, "edges": [
        {"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]})


def codes(result) -> dict[str, str]:
    return {i.code: i.level for i in result.issues if i.code and i.code.startswith(("human.", "process."))}


def test_parse_duration():
    assert parse_duration("PT4H") == timedelta(hours=4)
    assert parse_duration("P2D") == timedelta(days=2)
    assert parse_duration("P1DT30M") == timedelta(days=1, minutes=30)
    assert parse_duration("pt90s") == timedelta(seconds=90)
    for bad in ("4h", "P", "PT", "P1M", "PT0H", "", None, 3):
        assert parse_duration(bad) is None, bad


@pytest.mark.parametrize("config, code", [
    ({"due": "4 小时"}, "human.due_invalid"),
    ({"on_timeout": {"action": "remind"}}, "human.due_invalid"),            # 有超时处理却没写时限
    ({"mode": "input", "policy": "all"}, "human.policy_mode"),
    ({"mode": "edit", "policy": {"quorum": 2}}, "human.policy_mode"),
    ({"due": "PT1H", "on_timeout": {"action": "escalate"}}, "human.escalate_slot"),
    ({"due": "PT1H", "on_timeout": {"action": "approve"}}, "human.timeout_invalid"),   # 没有「自动批准」
    ({"policy": "most"}, "human.policy_invalid"),
    ({"assignee": {"kind": "slot"}}, "human.assignee_invalid"),
    ({"form": "不是对象"}, "human.form_invalid"),
    ({"form_from": "{{ nodes.a.form }}"}, "human.form_from_invalid"),
])
def test_bad_human_config_is_an_error(config, code):
    result = validate_graph(spec(node("h", "human", **{"mode": "approve", **config})))
    assert codes(result).get(code) == "error", [i.model_dump() for i in result.issues]
    assert not result.ok
    gate = publish_issues(spec(node("h", "human", **{"mode": "approve", **config})), level="published")
    assert any(i.code == code and i.level == "error" for i in gate)


def test_form_is_checked_against_the_catalog(monkeypatch):
    from app.apps import forms

    monkeypatch.setattr(forms, "validate_form_schema", lambda form: ["不支持 pattern"])
    result = validate_graph(spec(node("h", "human", mode="input", form={"type": "object", "properties": {}})))
    assert codes(result) == {"human.form_invalid": "error"}
    monkeypatch.setattr(forms, "validate_form_schema", lambda form: [])
    assert validate_graph(spec(node("h", "human", mode="input", form={"type": "object", "properties": {}}))).ok


def test_good_human_config_passes():
    result = validate_graph(spec(node("h", "human", mode="approve", assignee={"kind": "slot", "slot": "finance"},
                                      policy={"quorum": 2}, due="PT1H",
                                      on_timeout={"action": "escalate", "to_slot": "director"})))
    assert result.ok and codes(result) == {}


def test_agent_that_may_ask_mid_way_is_warned():
    agent = node("bot", "agent", prompt="查", tools=["mcp:erp/get_order", "current_time"])
    assert codes(lint_for_publish(spec(agent), level="governed")) == {"process.wait_mid_agent": "warning"}
    quiet = node("bot", "agent", prompt="查", tools=["mcp:erp/get_order"], approval="never")
    assert "process.wait_mid_agent" not in codes(lint_for_publish(spec(quiet), level="published"))
    reads = node("bot", "agent", prompt="查", tools=["current_time", "db_query__shop"])
    assert "process.wait_mid_agent" not in codes(lint_for_publish(spec(reads), level="published"))
    team = node("team", "supervisor", goal="x", agents=[{"name": "r", "tools": ["mcp:erp/pay"]}])
    assert codes(lint_for_publish(spec(team), level="published")) == {"process.wait_mid_agent": "warning"}


def test_code_with_network_is_warned():
    code = node("c", "code", code="print(1)", network=True)
    assert codes(lint_for_publish(spec(code), level="governed")).get("process.code_network") == "warning"


async def test_human_inside_a_subgraph_is_warned_when_subgraphs_are_loaded():
    inner = {"nodes": [node("start", "input"), node("h", "human", mode="approve"), node("out", "output")],
             "edges": [{"source": "start", "target": "h"}, {"source": "h", "target": "out"}]}
    async with SessionLocal() as session:
        wf = Workflow(name="带审批的子流程", graph=inner)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=inner))
        await session.commit()
        outer = spec(node("sub", "subgraph", workflow_id=wf.id, workflow_version=1))
        graphs = await load_subgraphs(session, outer)
    assert "process.human_in_subgraph" not in codes(lint_for_publish(outer, level="published"))   # 没读就不查
    with with_subgraphs(graphs):
        assert codes(lint_for_publish(outer, level="published")) == {"process.human_in_subgraph": "warning"}


def test_seeded_expense_template_passes_validation_and_the_gate():
    from app.seed import TEMPLATES

    template = next(t for t in TEMPLATES if t["name"] == "⑩ 报销审批（会签）")
    graph = GraphSpec.model_validate({"nodes": template["nodes"], "edges": template["edges"]})
    assert validate_graph(graph).ok
    assert not [i for i in publish_issues(graph, level="published") if i.level == "error"]
    review = next(n for n in template["nodes"] if n["type"] == "human")["data"]["config"]
    assert review["assignee"] == {"kind": "slot", "slot": "finance"} and review["policy"] == "all"
    assert review["due"] == "PT1H" and review["on_timeout"] == {"action": "escalate", "to_slot": "director"}
