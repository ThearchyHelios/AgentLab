"""证据门禁 G1–G5 的收尾：验收留下的小问题和 B 段的交叉请求。

- G1 和 G2 对开了 cite_fields 的 agent 字段看法一致：逐格核对过的字段是数据，不是模型写的话；
- 选项里只有标了 handoff 的那一项才是「交给 Copilot」，节点 id 恰好叫 copilot 的报告照常应用；
- 自动修复接口读不懂图时也带 handoff 键；
- 已发布级别的 G2 对每个出口都给警告（用户拍板：G 规则受管挡、已发布警告，范围一样）；
- G4、G5 挡住的地方，validate 那条同义的提示不再重复；
- 删掉开了 cite_fields 的 agent 的 output_schema 算降低要求；
- Copilot 自查带上全图默认，和真正的门禁口径一致；
- 受管契约的 claims 写 on_uncited: ignore 给警告，并能一键改成 degrade。
"""
from __future__ import annotations

import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.autofix import HANDOFF, apply_fixes, forbidden_changes
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec, validate_graph
from app.main import app
from tests.test_autofix_governance import (
    SCHEMA, STRICT, codes, compute_feeding, config, create, errors, insert, node, plan, script, traceable,
    with_story,
)


def issues(graph: dict, level: str = "governed") -> list:
    return publish_issues(GraphSpec.model_validate(graph), level=level)


def cited_agent(graph: dict, *, cite: bool = True) -> dict:
    ask = node("ask", "agent", "查数助手", prompt="查一下", tools=["db_query__shop"], approval="dangerous",
               output_schema=SCHEMA, cite_fields=cite, assign_to="facts")
    return insert(graph, ask, before="done", after="write")


# ---------------------------------------------------------------- G1 / G2 一致


@pytest.mark.parametrize("ref", ["{{ vars.facts.gmv }}", "{{ nodes.ask.data.gmv }}"])
def test_a_cited_agent_field_in_a_contract_exit_is_data(ref):
    graph = cited_agent(traceable())
    config(graph, "done")["fields"].append({"name": "GMV", "value": ref})
    found = codes(errors(graph))
    assert "governed.exit_text_source" not in found and "governed.text_bypass" not in found, found
    assert not [f for f in plan(graph) if f.startswith("governed.exit_text_source")]


def test_an_uncited_agent_field_in_a_contract_exit_is_still_text():
    graph = cited_agent(traceable(), cite=False)
    config(graph, "done")["fields"].append({"name": "GMV", "value": "{{ vars.facts.gmv }}"})
    assert "governed.exit_text_source" in codes(errors(graph))


# ---------------------------------------------------------------- handoff 只认标记


def renamed(graph: dict, old: str, new: str) -> dict:
    text = json.dumps(graph, ensure_ascii=False)
    return json.loads(text.replace(f'"{old}"', f'"{new}"').replace(f"nodes.{old}.", f"nodes.{new}."))


def test_the_handoff_option_is_marked():
    fix = plan(compute_feeding())["governed.caliber_compute_input:calc"]
    marked = [o for o in fix["options"] if o.get("handoff")]
    assert [o["value"] for o in marked] == [HANDOFF]


def test_a_report_whose_id_is_copilot_is_applied_not_handed_off():
    graph = renamed(with_story(traceable()), "write", HANDOFF)
    fid = "governed.exit_text_source:done"
    assert [o["value"] for o in plan(graph)[fid]["options"]] == [HANDOFF]
    out = apply_fixes(graph, [fid], {fid: HANDOFF}, level="governed")
    assert out["applied"] == [fid] and out["handoff"] == [], out
    assert config(out["graph"], "done")["fields"][2]["value"] == "{{ nodes.copilot.text }}"


# ---------------------------------------------------------------- 接口的早退回包


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_autofix_on_an_unreadable_graph_still_has_handoff(client):
    wf = await create(client, traceable())
    body = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "graph": {"nodes": "坏的"}, "apply": ["x"]})).json()
    assert body["ok"] is False and body["handoff"] == []


# ---------------------------------------------------------------- G2 已发布级别的范围


def test_published_g2_warns_on_every_exit():
    graph = {"nodes": [node("start", "input", "问题"), node("chat", "llm", "回答", prompt="{{ input.q }}"),
                       node("out", "output", "回答出口", fields=[{"name": "回答", "value": "{{ nodes.chat.text }}"}])],
             "edges": [{"source": "start", "target": "chat"}, {"source": "chat", "target": "out"}]}
    published = [i for i in issues(graph, "published") if i.code == "governed.text_bypass"]
    assert [i.level for i in published] == ["warning"] and published[0].node_id == "chat"
    governed = [i for i in issues(graph, "governed") if i.code == "governed.text_bypass"]
    assert [i.level for i in governed] == ["error"]


# ---------------------------------------------------------------- G4 / G5 不重复


def test_the_g4_error_is_not_repeated_by_a_validate_warning():
    graph = compute_feeding()
    spec = GraphSpec.model_validate(graph)
    # validate 自己照样提示（画布上没发布也看得到），带编号
    assert "evidence.caliber_compute_input" in {i.code for i in validate_graph(spec).issues}
    for level in ("governed", "published"):
        found = issues(graph, level)
        assert "governed.caliber_compute_input" in {i.code for i in found}
        assert "evidence.caliber_compute_input" not in {i.code for i in found}, level


def test_the_g5_error_is_not_repeated_by_a_validate_warning():
    graph = insert(traceable(), node("ask", "agent", "查数助手", prompt="查", tools=["db_query__shop"],
                                     approval="dangerous", output_schema=SCHEMA, assign_to="facts"),
                   before="card", after="fetch")
    config(graph, "card")["metrics"].append({"id": "n", "expression": "vars.facts.gmv"})
    spec = GraphSpec.model_validate(graph)
    assert "evidence.cite_fields_off" in {i.code for i in validate_graph(spec).issues}
    found = issues(graph, "governed")
    assert "governed.caliber_agent_cite_fields" in {i.code for i in found}
    assert "evidence.cite_fields_off" not in {i.code for i in found}


def test_the_validate_warning_stays_where_no_gate_rule_covers_it():
    graph = cited_agent(traceable(), cite=False)          # 不给口径卡供数：G5 不管，validate 的提示留着
    assert "evidence.cite_fields_off" in {i.code for i in issues(graph, "governed")}


# ---------------------------------------------------------------- 删 output_schema 算降低要求


def test_removing_the_schema_of_a_cited_agent_is_refused():
    before = cited_agent(traceable())
    after = copy.deepcopy(before)
    config(after, "ask").pop("output_schema")
    [reason] = forbidden_changes(before, after)
    assert "「查数助手」" in reason and "「结构化输出 Schema」" in reason
    config(after, "ask")["output_schema"] = {}
    assert forbidden_changes(before, after)


# ---------------------------------------------------------------- Copilot 自查带全图默认


async def test_copilot_self_check_sees_graph_defaults(client, monkeypatch):
    graph = compute_feeding()
    config(graph, "write").clear()
    config(graph, "write")["instructions"] = "写周报"
    graph["defaults"] = dict(STRICT)
    assert "governed.report_policy" not in codes(errors(graph))      # 真正的门禁认全图默认
    model = script(monkeypatch, [])
    wf = await create(client, graph)
    await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": [], "assist": True})
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "「计算」" in human and "要写明" not in human


async def test_copilot_self_check_still_sees_report_policy_without_defaults(client, monkeypatch):
    graph = compute_feeding()
    config(graph, "write").clear()
    config(graph, "write")["instructions"] = "写周报"
    model = script(monkeypatch, [])
    wf = await create(client, graph)
    await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": [], "assist": True})
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "需要调整核对规则" in human


def test_the_assist_rules_forbid_marking_sources_and_loosening_reports():
    from app.api.copilot import _ASSIST_RULES

    assert "evidence_role" in _ASSIST_RULES and "source" in _ASSIST_RULES
    for key in ("numbers", "on_violation", "claims", "cite_fields", "output_schema"):
        assert key in _ASSIST_RULES, key


# ---------------------------------------------------------------- 契约 claims 写 ignore


def ignoring() -> dict:
    return traceable(claims={"policy": "require_citation", "on_uncited": "ignore"})


def test_contract_ignore_warns_under_governed_only():
    found = [i for i in issues(ignoring()) if i.code == "contract.claims_ignored"]
    assert [(i.level, i.node_id, i.field) for i in found] == [("warning", "done", "contract.claims")]
    assert not [i for i in issues(ignoring(), "published") if i.code == "contract.claims_ignored"]
    assert not [i for i in issues(traceable(claims={"policy": "require_citation", "on_uncited": "withhold"}))
                if i.code == "contract.claims_ignored"]


def test_contract_ignore_is_fixed_to_degrade():
    fid = "contract.claims_ignored:done"
    fix = plan(ignoring())[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "contract.claims.on_uncited",
                                                        "before": "ignore", "after": "degrade"}
    out = apply_fixes(ignoring(), [fid], level="governed")
    assert out["applied"] == [fid], out["rejected"]
    assert config(out["graph"], "done")["contract"]["claims"] == {"policy": "require_citation",
                                                                  "on_uncited": "degrade"}
    assert "contract.claims_ignored" not in {i["code"] for i in out["remaining"]}
    # 反方向（degrade 改回 ignore）是放宽，要拒
    assert forbidden_changes(out["graph"], ignoring())
