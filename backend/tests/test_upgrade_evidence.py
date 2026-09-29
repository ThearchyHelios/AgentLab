"""一键升级为可追溯结构（证据五期）。

确定性改写 upgrade_for_evidence 是纯函数、幂等：
- R1 契约的 narrative 指向模型调用节点 → 换成报告撰写，契约改成 report_from
- R2 没有契约、模型调用的文字进了出口、上游有取数 → 同样换成报告撰写
- R3 input → agent → output（问数据的典型图）→ 中间插一个报告撰写，出口改取它的正文
- R4 配了 output_schema 的 agent → 打开 cite_fields
- R5 沙箱代码的产出喂口径卡 → 不改，只在 notes 里建议标成 source

禁止规则和发布前修复同一套，只多两个例外：模型调用换成报告撰写、R3 改接的那条连线。
升级接口只给预览，不改库。
"""
from __future__ import annotations

import copy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent, Setting, Workflow, WorkflowVersion
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec, validate_graph
from app.engine.upgrade import forbidden, upgrade_candidates, upgrade_for_evidence
from app.main import app

UPGRADE = "evidence.upgrade_available"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, x=0, **config):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 80}, "data": {"label": label or nid, "config": config}}


def edge(i, a, b, handle=None):
    out = {"id": f"e{i}", "source": a, "target": b}
    if handle:
        out["sourceHandle"] = handle
    return out


def edges_of(*pairs):
    return [edge(i, a, b) for i, (a, b) in enumerate(pairs)]


def narrative_graph() -> dict:
    """旧的周报：口径卡算数、模型调用写叙述、契约按数值回指叙述（R1）。"""
    return {
        "nodes": [
            node("start", "input", "周期", x=0, fields=[{"name": "week", "default": "2026-W37"}]),
            node("fetch", "code", "取数", x=280, language="python", evidence_role="source", assign_to="agg",
                 code="import json\nprint(json.dumps({'orders': 12, 'amount': 345.5}))"),
            node("caliber", "metrics", "周报口径卡", x=560, caliber="周报口径", caliber_version="v1", metrics=[
                {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.agg.orders"},
                {"id": "amount", "name": "销售额", "unit": "元", "expression": "vars.agg.amount"}]),
            node("narrate", "llm", "叙述", x=840, system="你是周报撰写人。", provider="mock", model="mock-writer",
                 temperature=0.2, prompt="{{ nodes.caliber.text }}\n\n请为 {{ input.week }} 写周报正文。",
                 assign_to="report", skills=["结构化分析"]),
            node("done", "output", "出具", x=1120, fields=[
                {"name": "周报", "value": "{{ vars.report }}"},
                {"name": "指标清单", "value": "{{ nodes.caliber.text }}"}],
                contract={"metrics_from": ["caliber"], "narrative": "{{ vars.report }}", "required": ["amount"],
                          "expected": ["orders"], "allow_numbers": ["37"], "strict": False}),
        ],
        "edges": edges_of(("start", "fetch"), ("fetch", "caliber"), ("caliber", "narrate"), ("narrate", "done")),
        "viewport": {"x": 1, "y": 2, "zoom": 0.9},
    }


def summary_graph() -> dict:
    """没有契约：调用工具查库，模型调用写总结，出口直接取它的文字（R2）。"""
    return {
        "nodes": [
            node("start", "input", "输入"),
            node("fetch", "tool", "查库", tool="db_query__shop", args={"sql": "SELECT COUNT(*) AS n FROM orders"}),
            node("sum", "llm", "总结", prompt="根据 {{ nodes.fetch }} 写一句总结"),
            node("out", "output", "成果", fields=[{"name": "总结", "value": "{{ nodes.sum.text }}"}]),
        ],
        "edges": edges_of(("start", "fetch"), ("fetch", "sum"), ("sum", "out")),
    }


def ask_graph(**agent) -> dict:
    """问数据的典型图：input → agent（绑查库工具）→ output（R3）。"""
    cfg = {"tools": ["db_query__shop"], "prompt": "查库回答：{{ input.question }}", "assign_to": "answer", **agent}
    return {
        "nodes": [
            node("start", "input", "问题", x=0),
            node("ask", "agent", "查数作答", x=280, **cfg),
            node("out", "output", "答案", x=560, fields=[{"name": "answer", "value": "{{ vars.answer }}"}]),
        ],
        "edges": edges_of(("start", "ask"), ("ask", "out")),
    }


def schema_graph() -> dict:
    """agent 配了 output_schema 却没开 cite_fields（R4），另有一个节点把它的 assign_to 当文字用。"""
    return {
        "nodes": [
            node("start", "input", "输入"),
            node("fetch", "agent", "取数", tools=["db_query__shop"], prompt="查本周销售额", assign_to="kpi",
                 output_schema={"type": "object", "properties": {"gmv": {"type": "number"}}}),
            node("card", "metrics", "口径卡", metrics=[{"id": "gmv", "name": "销售额", "expression": "vars.kpi.gmv"}]),
            node("write", "report", "报告撰写", instructions="写一句话"),
            node("peek", "output", "调试", fields=[{"name": "原样", "value": "{{ vars.kpi }}"}]),
            node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        ],
        "edges": edges_of(("start", "fetch"), ("fetch", "card"), ("card", "write"), ("write", "out"),
                          ("fetch", "peek")),
    }


def compute_graph() -> dict:
    """沙箱代码在算数、口径卡读它（R5：只建议，不改）。"""
    return {
        "nodes": [
            node("start", "input", "输入"),
            node("fetch", "tool", "查库", tool="db_query__shop", args={"sql": "SELECT 1 AS a, 2 AS b"}),
            node("calc", "code", "计算", language="python", assign_to="calc",
                 code="import json\nprint(json.dumps({'ratio': 1 / 2}))"),
            node("card", "metrics", "口径卡", metrics=[{"id": "ratio", "name": "比率", "expression": "vars.calc.ratio"}]),
            node("write", "report", "报告撰写", instructions="写一句话"),
            node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        ],
        "edges": edges_of(("start", "fetch"), ("fetch", "calc"), ("calc", "card"), ("card", "write"),
                          ("write", "out")),
    }


def nodes_by_id(graph: dict) -> dict:
    return {n["id"]: n for n in graph["nodes"]}


def cfg(graph: dict, nid: str) -> dict:
    return nodes_by_id(graph)[nid]["data"]["config"]


def pairs(graph: dict) -> set:
    return {(e["source"], e["target"]) for e in graph["edges"]}


def errors(graph: dict, level: str = "published") -> list:
    return [i for i in publish_issues(GraphSpec.model_validate(graph), level=level) if i.level == "error"]


def infos(graph: dict) -> list:
    return [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.code == UPGRADE]


# --------------------------------------------------------------------------
# R1–R5
# --------------------------------------------------------------------------


def test_r1_turns_the_narrating_llm_into_a_report_and_the_contract_into_report_from():
    before = narrative_graph()
    out = upgrade_for_evidence(before)
    g = out["graph"]
    write = nodes_by_id(g)["narrate"]
    assert write["type"] == "report" and write["data"]["label"] == "叙述"
    c = write["data"]["config"]
    # system 和 prompt 合成 instructions，模型配置照搬，assign_to 留着（出口还在读 vars.report）
    assert c["instructions"] == "你是周报撰写人。\n\n{{ nodes.caliber.text }}\n\n请为 {{ input.week }} 写周报正文。"
    assert (c["provider"], c["model"], c["temperature"], c["assign_to"]) == ("mock", "mock-writer", 0.2, "report")
    assert not {"prompt", "system", "skills"} & set(c)
    contract = cfg(g, "done")["contract"]
    assert contract == {"metrics_from": ["caliber"], "report_from": "narrate", "required": ["amount"],
                        "expected": ["orders"], "allow_numbers": ["37"], "strict": False}
    assert out["applied"] == ["R1:narrate"] and out["rejected"] == []
    type_change = next(ch for ch in out["changes"] if ch["field"] == "type")
    assert (type_change["node_id"], type_change["before"], type_change["after"], type_change["rule"]) == (
        "narrate", "llm", "report", "R1")
    assert any(ch["node_id"] == "done" and ch["field"] == "contract" for ch in out["changes"])
    assert g["viewport"] == before["viewport"] and [e["id"] for e in g["edges"]] == [e["id"] for e in before["edges"]]
    assert errors(g) == [] and infos(g) == []
    texts = " ".join(n["text"] for n in out["notes"])
    assert "结构化分析" in texts                                   # 技能不再挂载要说出来
    assert "{{ nodes.caliber.text }}" in texts                     # 指令里照搬了口径卡的文字，提醒可以删


def test_r2_replaces_an_llm_whose_text_reaches_an_exit_without_a_contract():
    out = upgrade_for_evidence(summary_graph())
    g = out["graph"]
    assert nodes_by_id(g)["sum"]["type"] == "report"
    assert cfg(g, "sum")["instructions"] == "根据 {{ nodes.fetch }} 写一句总结"
    assert out["applied"] == ["R2:sum"] and errors(g) == [] and infos(g) == []
    assert "contract" not in cfg(g, "out")                         # R2 不替人加契约


def test_r2_leaves_an_llm_with_nothing_fetched_upstream_alone():
    graph = summary_graph()
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "fetch"]
    graph["edges"] = edges_of(("start", "sum"), ("sum", "out"))
    out = upgrade_for_evidence(graph)
    assert out["changes"] == [] and out["graph"] == graph and infos(graph) == []


def test_r2_does_not_count_knowledge_retrieval_as_fetching_data():
    # 知识库问答的答案换成报告撰写，文档里的数只能当引文、不能当值引用，全会被判成裸数字：不算取数
    graph = summary_graph()
    graph["nodes"][1] = node("fetch", "retrieve", "检索", query="{{ input.question }}", collection="docs")
    out = upgrade_for_evidence(graph)
    assert out["changes"] == [] and infos(graph) == []


def test_r3_inserts_a_report_between_the_agent_and_the_exit():
    before = ask_graph()
    out = upgrade_for_evidence(before)
    g = out["graph"]
    [rid] = [n["id"] for n in g["nodes"] if n["type"] == "report"]
    assert pairs(g) == {("start", "ask"), ("ask", rid), (rid, "out")}
    assert cfg(g, "out")["fields"] == [{"name": "answer", "value": f"{{{{ nodes.{rid}.text }}}}"}]
    instructions = cfg(g, rid)["instructions"]
    assert "{{ input.question }}" in instructions and "{{ nodes.ask.text }}" in instructions
    assert cfg(g, "ask") == cfg(before, "ask")                     # agent 本身不动
    assert out["applied"] == [f"R3:ask"] and errors(g) == [] and infos(g) == []
    fields = {ch["field"] for ch in out["changes"]}
    assert {"node", "edge", "fields"} <= fields
    rewire = next(ch for ch in out["changes"] if ch["field"] == "edge" and ch["before"])
    assert rewire["before"] == {"source": "ask", "target": "out"} and rewire["after"] == {"source": rid, "target": "out"}
    assert "contract" not in cfg(g, "out")                         # 契约（和 cells）要人拿主意，不替人加
    assert any("cells" in n["text"] for n in out["notes"])


def test_r3_skips_agents_that_only_read_table_structures():
    graph = ask_graph(tools=["db_schema__shop"])
    assert upgrade_for_evidence(graph)["changes"] == [] and infos(graph) == []


def test_r4_turns_on_cite_fields_and_warns_where_the_variable_was_read_as_text():
    out = upgrade_for_evidence(schema_graph())
    g = out["graph"]
    assert cfg(g, "fetch")["cite_fields"] is True
    assert out["applied"] == ["R4:fetch"] and errors(g) == [] and infos(g) == []
    [note] = [n for n in out["notes"] if n["rule"] == "R4"]
    assert note["node_id"] == "peek" and "{{ vars.kpi }}" in note["text"]


def test_r5_only_suggests_marking_the_code_node_as_a_source():
    before = compute_graph()
    out = upgrade_for_evidence(before)
    assert out["graph"] == before and out["changes"] == []
    [note] = [n for n in out["notes"] if n["rule"] == "R5"]
    assert note["node_id"] == "calc" and "source" in note["text"]
    assert infos(before) == []                                     # 只有 R5 的不算「旧结构」


@pytest.mark.parametrize("make", [narrative_graph, summary_graph, ask_graph, schema_graph, compute_graph])
def test_upgrading_twice_changes_nothing_more(make):
    first = upgrade_for_evidence(make())
    second = upgrade_for_evidence(first["graph"])
    assert second["graph"] == first["graph"] and second["changes"] == [] and second["applied"] == []


@pytest.mark.parametrize("make", [narrative_graph, summary_graph, ask_graph, schema_graph])
def test_upgrade_is_pure(make):
    graph = make()
    kept = copy.deepcopy(graph)
    upgrade_for_evidence(graph)
    upgrade_for_evidence(GraphSpec.model_validate(graph))
    assert graph == kept


def test_graph_spec_input_gives_the_same_result():
    assert upgrade_for_evidence(GraphSpec.model_validate(ask_graph()))["applied"] == ["R3:ask"]


def test_governed_upgrades_write_the_strict_report_settings():
    out = upgrade_for_evidence(ask_graph(), level="governed")
    [rid] = [n["id"] for n in out["graph"]["nodes"] if n["type"] == "report"]
    c = cfg(out["graph"], rid)
    assert (c["numbers"], c["on_violation"], c["claims"]) == ("strict", "fail", "require_citation")
    assert out["rejected"] == []
    # published 下不替探索运行拧紧：修不掉的数字违规照常标注
    loose = cfg(upgrade_for_evidence(ask_graph())["graph"], rid)
    assert not {"numbers", "on_violation", "claims"} & set(loose)


def test_a_rewrite_that_would_add_an_error_is_dropped_with_a_reason():
    graph = narrative_graph()
    # 叙述节点没连到出口：换成报告撰写以后 report_from 指向不在上游的节点，会冒出新的 error
    graph["edges"] = edges_of(("start", "fetch"), ("fetch", "caliber"), ("caliber", "narrate"), ("caliber", "done"))
    out = upgrade_for_evidence(graph)
    assert out["graph"] == graph and out["changes"] == [] and out["applied"] == []
    [rejected] = out["rejected"]
    assert rejected["fix_id"] == "R1:narrate" and "新的问题" in rejected["reason"]


def test_a_narrative_that_mixes_two_writers_is_not_guessed():
    graph = narrative_graph()
    graph["nodes"].insert(4, node("extra", "llm", "补充", prompt="补一句", assign_to="more"))
    graph["edges"].append(edge(9, "caliber", "extra"))
    graph["edges"].append(edge(10, "extra", "done"))
    cfg(graph, "done")["contract"]["narrative"] = "{{ vars.report }}{{ vars.more }}"
    out = upgrade_for_evidence(graph)
    assert not any(a.startswith("R1:") for a in out["applied"])
    assert any(n["rule"] == "R1" and "补充" in n["text"] for n in out["notes"])
    assert infos(graph) == []                                      # 说不准怎么改的不建议升级


def two_exit_narrative_graph(*, extra_first: bool, mixed_exit_first: bool) -> dict:
    """两个出口都读「叙述」：「出具」的 narrative 还拼了「补充」的文字（说不准），「出具2」只读「叙述」。"""
    graph = narrative_graph()
    extra = node("extra", "llm", "补充", prompt="补一句", assign_to="more")
    graph["nodes"].insert(3 if extra_first else 4, extra)
    graph["edges"] += [edge(9, "caliber", "extra"), edge(10, "extra", "done"), edge(11, "narrate", "done2")]
    cfg(graph, "done")["contract"]["narrative"] = "{{ vars.report }}{{ vars.more }}"
    other = node("done2", "output", "出具2", fields=[{"name": "周报", "value": "{{ vars.report }}"}],
                 contract={"metrics_from": ["caliber"], "narrative": "{{ vars.report }}", "required": ["amount"],
                           "strict": False})
    done = next(i for i, n in enumerate(graph["nodes"]) if n["id"] == "done")
    graph["nodes"].insert(done + 1 if mixed_exit_first else done, other)
    return graph


@pytest.mark.parametrize("extra_first", [False, True])
@pytest.mark.parametrize("mixed_exit_first", [False, True])
def test_an_ambiguous_narrative_blocks_the_retype_whatever_the_node_order(extra_first, mixed_exit_first):
    # 同一个模型调用从两个出口认出来：一个出口说不准就整个不换——不看哪个出口在画布上排前面
    graph = two_exit_narrative_graph(extra_first=extra_first, mixed_exit_first=mixed_exit_first)
    out = upgrade_for_evidence(graph)
    assert out["graph"] == graph and out["changes"] == [] and out["applied"] == []
    blocked = [n for n in out["notes"] if n["rule"] == "R1" and n["level"] == "warning"]
    assert len(blocked) == 1 and "补充" in blocked[0]["text"] and "叙述" in blocked[0]["text"]
    assert infos(graph) == []


def test_a_narrative_that_mixes_an_llm_and_an_agent_is_not_guessed():
    graph = narrative_graph()
    graph["nodes"].insert(4, node("ask", "agent", "查数", tools=["db_query__shop"], prompt="查一下", assign_to="ans"))
    graph["edges"] += [edge(9, "caliber", "ask"), edge(10, "ask", "done")]
    cfg(graph, "done")["contract"]["narrative"] = "{{ vars.report }}\n{{ nodes.ask.text }}"
    out = upgrade_for_evidence(graph)
    assert out["changes"] == [] and out["graph"] == graph
    assert any(n["rule"] == "R1" and n["level"] == "warning" and "查数" in n["text"] for n in out["notes"])
    assert any(n["rule"] == "R3" and n["level"] == "warning" and "出具" in n["text"] for n in out["notes"])
    assert infos(graph) == []


def indirect_ask_graph(*, indirect_first: bool) -> dict:
    """问数据的 agent 直连「答案」，另一个出口「转述」经过整形节点才读到它的原话。"""
    graph = ask_graph()
    graph["nodes"].insert(2, node("shape", "transform", "整形", template="{{ vars.answer }}", assign_to="shaped"))
    other = node("out2", "output", "转述", fields=[{"name": "原话", "value": "{{ nodes.ask.text }}"}])
    graph["nodes"].insert(3 if indirect_first else 4, other)
    graph["edges"] += [edge(7, "ask", "shape"), edge(8, "shape", "out2")]
    return graph


@pytest.mark.parametrize("indirect_first", [False, True])
def test_r3_rewires_the_direct_exit_and_explains_the_indirect_one_whatever_the_node_order(indirect_first):
    graph = indirect_ask_graph(indirect_first=indirect_first)
    out = upgrade_for_evidence(graph)
    g = out["graph"]
    assert out["applied"] == ["R3:ask"] and out["rejected"] == []
    [rid] = [n["id"] for n in g["nodes"] if n["type"] == "report"]
    assert pairs(g) == {("start", "ask"), ("ask", rid), (rid, "out"), ("ask", "shape"), ("shape", "out2")}
    assert cfg(g, "out")["fields"] == [{"name": "answer", "value": f"{{{{ nodes.{rid}.text }}}}"}]
    assert cfg(g, "out2") == cfg(graph, "out2")                    # 经过别的节点的出口不替人改
    [skipped] = [n for n in out["notes"] if n["rule"] == "R3" and n["level"] == "warning"]
    assert "转述" in skipped["text"] and "经过别的节点" in skipped["text"]
    assert infos(graph) and infos(g) == []
    again = upgrade_for_evidence(g)
    assert again["changes"] == [] and again["graph"] == g


def test_r3_with_only_indirect_exits_is_not_guessed():
    graph = indirect_ask_graph(indirect_first=True)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "out"]
    graph["edges"] = [e for e in graph["edges"] if e["target"] != "out"]
    out = upgrade_for_evidence(graph)
    assert out["changes"] == [] and out["graph"] == graph
    [blocked] = [n for n in out["notes"] if n["rule"] == "R3"]
    assert blocked["level"] == "warning" and "转述" in blocked["text"]
    assert infos(graph) == []


@pytest.mark.parametrize("unset", ["", None])
@pytest.mark.parametrize("after_narrative", [True, False])
def test_an_unset_report_from_does_not_swallow_the_new_one(unset, after_narrative):
    graph = narrative_graph()
    contract = cfg(graph, "done")["contract"]
    rest = {k: v for k, v in contract.items() if k != "narrative"}
    cfg(graph, "done")["contract"] = ({"narrative": contract["narrative"], "report_from": unset, **rest}
                                      if after_narrative else
                                      {"report_from": unset, "narrative": contract["narrative"], **rest})
    out = upgrade_for_evidence(graph)
    assert out["applied"] == ["R1:narrate"]
    assert cfg(out["graph"], "done")["contract"] == {"report_from": "narrate", **rest}


def test_an_unset_report_from_is_dropped_when_r3_converts_the_narrative():
    graph = ask_graph()
    cfg(graph, "out")["contract"] = {"narrative": "{{ vars.answer }}", "report_from": "", "strict": True}
    out = upgrade_for_evidence(graph)
    [rid] = [n["id"] for n in out["graph"]["nodes"] if n["type"] == "report"]
    assert cfg(out["graph"], "out")["contract"] == {"report_from": rid, "strict": True}


def cells_notes(out: dict) -> list:
    return [n for n in out["notes"] if n["rule"] == "R3" and "cells: true" in n["text"]]


def test_r3_warns_that_governed_formal_runs_fail_without_cells():
    out = upgrade_for_evidence(ask_graph(), level="governed")
    [note] = cells_notes(out)
    assert note["level"] == "warning" and "每次正式运行都会失败" in note["text"] and "口径卡" in note["text"]


def test_r3_warns_when_it_turns_a_contract_on_a_graph_without_cards_into_report_from():
    graph = ask_graph()
    cfg(graph, "out")["contract"] = {"narrative": "{{ vars.answer }}", "strict": True}
    out = upgrade_for_evidence(graph)
    [note] = cells_notes(out)
    assert note["level"] == "warning" and "每次正式运行都会失败" in note["text"]
    assert "cells" not in cfg(out["graph"], "out")["contract"]      # 要人拿主意，不替人打开


def test_r3_only_informs_about_cells_at_published_level_without_a_contract():
    [note] = cells_notes(upgrade_for_evidence(ask_graph()))
    assert note["level"] == "info"


def test_r3_says_nothing_about_cells_when_the_contract_already_declares_them():
    graph = ask_graph()
    cfg(graph, "out")["contract"] = {"narrative": "{{ vars.answer }}", "strict": True, "cells": True}
    out = upgrade_for_evidence(graph, level="governed")
    assert out["applied"] == ["R3:ask"] and cells_notes(out) == []


def test_candidates_list_what_the_upgrade_would_touch():
    assert [(m.rule, m.node_id) for m in upgrade_candidates(GraphSpec.model_validate(narrative_graph()))] == [
        ("R1", "narrate")]
    assert [(m.rule, m.node_id) for m in upgrade_candidates(GraphSpec.model_validate(ask_graph()))] == [
        ("R3", "ask")]


# --------------------------------------------------------------------------
# 禁止规则
# --------------------------------------------------------------------------


def test_deleting_a_node_is_refused():
    before = narrative_graph()
    after = copy.deepcopy(before)
    after["nodes"] = [n for n in after["nodes"] if n["id"] != "fetch"]
    assert any("删了节点" in r for r in forbidden(before, after))


def test_llm_to_report_is_allowed_only_for_the_node_the_upgrade_names():
    before = narrative_graph()
    after = copy.deepcopy(before)
    nodes_by_id(after)["narrate"]["type"] = "report"
    assert forbidden(before, after, retyped={("narrate", "llm", "report")}) == []
    assert any("换成了" in r for r in forbidden(before, after))
    # 别的换类型（比如把沙箱代码换成口径卡）不在例外里
    nodes_by_id(after)["fetch"]["type"] = "metrics"
    assert any("取数" in r and "换成了" in r for r in forbidden(before, after, retyped={("narrate", "llm", "report")}))


def test_only_the_rewired_edge_may_go():
    before = ask_graph()
    after = copy.deepcopy(before)
    after["edges"] = [e for e in after["edges"] if (e["source"], e["target"]) != ("ask", "out")]
    assert forbidden(before, after, rewired={("ask", "out")}) == []
    assert any("删了连线" in r for r in forbidden(before, after))
    after["edges"] = []
    assert any("start → ask" in r for r in forbidden(before, after, rewired={("ask", "out")}))


@pytest.mark.parametrize("damage, word", [
    (lambda g: cfg(g, "done").pop("contract"), "契约"),
    (lambda g: cfg(g, "done")["contract"].update(required=[]), "required"),
    (lambda g: cfg(g, "done")["contract"].update(strict=True), None),
])
def test_contract_rules_are_the_publish_fix_rules(damage, word):
    before = narrative_graph()
    after = copy.deepcopy(before)
    damage(after)
    reasons = forbidden(before, after)
    if word:
        assert any(word in r for r in reasons)
    else:
        assert reasons == []                                        # 收紧不算


def test_strict_off_and_looser_approval_are_refused():
    before = ask_graph(approval="dangerous")
    before["nodes"].append(node("fin", "output", "出具", contract={"report_from": "w", "strict": True}))
    after = copy.deepcopy(before)
    cfg(after, "ask")["approval"] = "never"
    cfg(after, "fin")["contract"]["strict"] = False
    reasons = forbidden(before, after)
    assert any("全部自动放行" in r for r in reasons) and any("strict" in r for r in reasons)


# --------------------------------------------------------------------------
# validate 的建议
# --------------------------------------------------------------------------


def test_old_structures_get_one_info_and_nothing_else_changes():
    spec = GraphSpec.model_validate(narrative_graph())
    result = validate_graph(spec)
    [hint] = [i for i in result.issues if i.code == UPGRADE]
    assert hint.level == "info" and hint.message.startswith("可以升级为可追溯结构") and hint.node_id is None
    assert "「叙述」" in hint.message                                      # 说得出要改哪里
    assert result.ok is (not any(i.level == "error" for i in result.issues))
    assert len([i for i in validate_graph(GraphSpec.model_validate(ask_graph())).issues if i.code == UPGRADE]) == 1


def test_already_traceable_graphs_get_no_info():
    for graph in (upgrade_for_evidence(narrative_graph())["graph"], compute_graph()):
        assert infos(graph) == []


def test_the_info_is_not_a_blocker_for_runs_or_publishing():
    graph = ask_graph()
    assert errors(graph) == [] and validate_graph(GraphSpec.model_validate(graph)).ok is True


# --------------------------------------------------------------------------
# 升级接口：只给预览，不改库
# --------------------------------------------------------------------------


async def _snapshot() -> dict:
    async with SessionLocal() as session:
        out = {}
        for model in (Workflow, WorkflowVersion, Run, RunEvent, Artifact, Setting):
            rows = (await session.execute(select(model))).scalars().all()
            out[model.__tablename__] = sorted(
                (repr({c.name: getattr(r, c.name) for c in model.__table__.columns}) for r in rows))
        return out


async def test_the_endpoint_previews_without_touching_the_database(client):
    created = await client.post("/api/workflows", json={"name": "升级-只读", "graph": narrative_graph()})
    assert created.status_code == 201, created.text
    before = await _snapshot()
    r = await client.post("/api/copilot/upgrade-evidence", json={"graph": narrative_graph()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"graph", "changes", "ops", "notes", "issues", "rejected", "assist"}
    assert body["assist"] is None
    assert nodes_by_id(body["graph"])["narrate"]["type"] == "report"
    assert any(op["op"] == "replace_node" and op["id"] == "narrate" for op in body["ops"])
    assert not [i for i in body["issues"] if i["level"] == "error"]
    assert not [i for i in body["issues"] if i.get("code") == UPGRADE]
    assert await _snapshot() == before                              # 一行都没改
    stored = (await client.get(f"/api/workflows/{created.json()['id']}")).json()["graph"]
    assert nodes_by_id(stored)["narrate"]["type"] == "llm"


async def test_the_endpoint_places_the_inserted_report_without_moving_the_others(client):
    from app.engine.layout import NODE_W

    graph = ask_graph()
    # 一个不相干的节点正好摆在 agent 和出口之间偏下的空处：新插进来的报告撰写不能压在它身上
    graph["nodes"].append(node("memo", "memory", "记一笔", x=420, action="write", content="{{ input.question }}"))
    graph["nodes"][-1]["position"]["y"] = 200
    graph["edges"].append(edge(9, "start", "memo"))
    body = (await client.post("/api/copilot/upgrade-evidence", json={"graph": graph})).json()
    placed = nodes_by_id(body["graph"])
    for nid, n in nodes_by_id(graph).items():
        assert placed[nid]["position"] == n["position"]
    [rid] = [nid for nid in placed if nid not in nodes_by_id(graph)]
    mine = placed[rid]["position"]
    for nid, n in placed.items():
        if nid != rid:
            p = n["position"]
            assert abs(p["x"] - mine["x"]) >= NODE_W or abs(p["y"] - mine["y"]) >= 60, (nid, p, mine)


async def test_the_endpoint_explains_an_unreadable_graph(client):
    r = await client.post("/api/copilot/upgrade-evidence", json={"graph": {"nodes": [{"id": "bad id!", "type": "llm"}]}})
    assert r.status_code == 200
    body = r.json()
    assert body["changes"] == [] and body["issues"][0]["level"] == "error" and "读不懂" in body["issues"][0]["message"]


async def test_the_endpoint_rejects_an_unknown_level(client):
    r = await client.post("/api/copilot/upgrade-evidence", json={"graph": ask_graph(), "level": "everything"})
    assert r.status_code == 422
