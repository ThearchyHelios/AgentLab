"""发布前自动修复的确定性规则（engine/autofix.py）。

原则：只修「答案唯一、不降低要求」的；要人拿主意的给候选、不替人选；修完重跑 validate 和门禁，
错误必须变少、不能冒出新错误；删节点连线、删空契约、审批改成全部放行、strict 改成 false
这些降低要求的操作一律拒绝。
"""
from __future__ import annotations

import copy

import pytest

from app.engine import autofix
from app.engine.autofix import apply_fixes, forbidden_changes, judge, plan_fixes
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec, ValidationIssue


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 10, "y": 20}, "width": 240,
            "data": {"label": label or nid, "config": config}}


def weekly(**contract_overrides) -> dict:
    """input → 查库 → 口径卡 → 报告撰写 → 出口（引用模式契约），受管要求都满足。"""
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    contract.update(contract_overrides)
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "orders", "name": "订单数", "unit": "单", "expression": "cell(nodes.fetch, 0, 'orders')"}]),
        node("write", "report", "报告撰写", instructions="写周报",
             numbers="strict", on_violation="fail", claims="require_citation"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={k: v for k, v in contract.items() if v is not None}),
    ]
    edges = [{"id": f"e{i}", "source": a["id"], "target": b["id"]} for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 1, "y": 2, "zoom": 1}}


def find(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)


def config(graph: dict, nid: str) -> dict:
    return find(graph, nid)["data"]["config"]


def plan(graph: dict, level: str = "governed", **kw) -> dict[str, dict]:
    spec = GraphSpec.model_validate(graph)
    return {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level=level), level=level, **kw)}


def errors(graph: dict, level: str = "governed") -> list:
    return [i for i in publish_issues(GraphSpec.model_validate(graph), level=level) if i.level == "error"]


def codes(issues) -> set[str]:
    return {i["code"] if isinstance(i, dict) else i.code for i in issues}


def add_card(graph: dict, nid: str = "card2") -> dict:
    """第二张口径卡：接在查库后面、报告前面。"""
    graph["nodes"].insert(3, node(nid, "metrics", "第二张口径卡", caliber="周报口径", metrics=[
        {"id": "refunds", "name": "退款数", "expression": "cell(nodes.fetch, 0, 'orders')"}]))
    graph["edges"] += [{"source": "fetch", "target": nid}, {"source": nid, "target": "write"}]
    return graph


def add_report(graph: dict, nid: str = "write2") -> dict:
    graph["nodes"].insert(4, node(nid, "report", "第二份报告", instructions="写月报",
                                  numbers="strict", on_violation="fail", claims="require_citation"))
    graph["edges"] += [{"source": "card", "target": nid}, {"source": nid, "target": "done"}]
    return graph


def test_the_baseline_graph_passes_the_governed_gate():
    assert errors(weekly()) == []
    assert plan(weekly()) == {}


# --------------------------------------------------------------------------
# auto：一键修好
# --------------------------------------------------------------------------


def test_agent_approval_never_becomes_dangerous():
    graph = weekly()
    graph["nodes"].insert(2, node("ask", "agent", "查数员", tools=["db_query__shop"], approval="never",
                                  prompt="用 db_query__shop 查"))
    graph["edges"] += [{"source": "start", "target": "ask"}, {"source": "ask", "target": "card"}]
    fid = "governed.agent_approval_never:ask"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["node_id"] == "ask"
    assert fix["preview"] == {"field": "approval", "before": "never", "after": "dangerous"}
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [fid] and out["rejected"] == []
    assert config(out["graph"], "ask")["approval"] == "dangerous"
    assert out["ok"] is True and "governed.agent_approval_never" not in codes(out["remaining"])
    assert out["changes"] == [{"fix_id": fid, "node_id": "ask", "node_title": "查数员", "field": "approval",
                               "before": "never", "after": "dangerous", "label": fix["label"]}]
    assert out["ops"] == [{"op": "update_node", "id": "ask", "config": {"approval": "dangerous"}}]


def test_an_unpinned_subgraph_is_pinned_to_the_published_version():
    graph = weekly()
    graph["nodes"].insert(1, node("lib", "subgraph", "方法卡", workflow_id="wf-lib"))
    graph["edges"] += [{"source": "start", "target": "lib"}, {"source": "lib", "target": "fetch"}]
    fid = "governed.subgraph_unpinned:lib"
    fix = plan(graph, versions={"wf-lib": 3})[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "workflow_version", "before": None, "after": 3}
    out = apply_fixes(graph, [fid], level="governed", versions={"wf-lib": 3})
    assert config(out["graph"], "lib")["workflow_version"] == 3 and out["ok"] is True
    # 上游没有发布过：不替人挑一个草稿版本
    assert plan(graph, versions={"wf-lib": None})[fid]["kind"] == "assist"
    assert plan(graph)[fid]["kind"] == "assist"


def test_a_missing_metrics_from_takes_the_only_upstream_card():
    graph = weekly(metrics_from=None)
    fid = "contract.metrics_from_missing:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"]["after"] == ["card"]
    out = apply_fixes(graph, [fid], level="governed")
    assert config(out["graph"], "done")["contract"]["metrics_from"] == ["card"]
    assert out["ok"] is True and len(errors(graph)) >= 2       # validate 和门禁各报一条，都消掉了


def test_several_cards_make_metrics_from_a_choice():
    graph = add_card(weekly(metrics_from=None))
    fid = "contract.metrics_from_missing:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["multiple"] is True and "preview" not in fix
    assert [o["value"] for o in fix["options"]] == ["card", "card2"]
    assert fix.get("default") is None
    # 没选就不应用
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == fid
    assert "metrics_from" not in config(out["graph"], "done")["contract"]
    out = apply_fixes(graph, [fid], {fid: ["card2"]}, level="governed")
    assert config(out["graph"], "done")["contract"]["metrics_from"] == ["card2"] and out["applied"] == [fid]
    # 选了候选之外的
    out = apply_fixes(graph, [fid], {fid: ["ghost"]}, level="governed")
    assert out["applied"] == [] and "候选" in out["rejected"][0]["reason"]


def test_a_missing_report_from_takes_the_only_upstream_report():
    graph = weekly(report_from=None)
    fid = "contract.report_from_missing:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "contract.report_from", "before": None,
                                                         "after": "write"}
    out = apply_fixes(graph, [fid], level="governed")
    assert config(out["graph"], "done")["contract"]["report_from"] == "write" and out["ok"] is True


def test_several_reports_make_report_from_a_choice():
    graph = add_report(weekly(report_from="fetch"))
    fid = "contract.report_from_invalid:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and not fix.get("multiple")
    assert [o["value"] for o in fix["options"]] == ["write", "write2"]
    out = apply_fixes(graph, [fid], {fid: "write2"}, level="governed")
    assert config(out["graph"], "done")["contract"]["report_from"] == "write2" and out["ok"] is True


def test_a_wrong_report_from_takes_the_only_upstream_report():
    graph = weekly(report_from="fetch")
    fid = "contract.report_from_invalid:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"]["before"] == "fetch" and fix["preview"]["after"] == "write"
    out = apply_fixes(graph, [fid], level="governed")
    assert out["ok"] is True and config(out["graph"], "done")["contract"]["report_from"] == "write"


def test_without_a_report_node_report_from_goes_to_copilot():
    # 以前这里让人在模型节点里选一段 narrative。受管级别的叙述模式过不了门禁 G1
    # （governed.report_from_required），选了也发不出去：没有报告撰写节点就交给 Copilot 加一个
    graph = weekly(report_from=None)
    write = find(graph, "write")
    write.update(type="llm", data={"label": "写周报", "config": {"prompt": "写周报", "assign_to": "report"}})
    fid = "contract.report_from_missing:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "assist" and fix["field"] == "contract.report_from"
    out = apply_fixes(graph, [fid], {fid: "{{ nodes.write.text }}"}, level="governed")
    assert out["applied"] == [] and "narrative" not in config(out["graph"], "done")["contract"]


def test_strict_off_is_turned_on():
    graph = weekly(strict=False)
    fid = "contract.strict_off:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "contract.strict", "before": False, "after": True}
    out = apply_fixes(graph, [fid], level="governed")
    assert config(out["graph"], "done")["contract"]["strict"] is True
    assert "contract.strict_off" not in codes(out["remaining"])


def test_report_metrics_from_drops_bad_entries_and_adds_the_only_card():
    graph = weekly()
    config(graph, "write")["metrics_from"] = ["ghost", "fetch"]
    fid = "report.metrics_from_invalid:write"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "metrics_from", "before": ["ghost", "fetch"],
                                                         "after": ["card"]}
    out = apply_fixes(graph, [fid], level="governed")
    assert config(out["graph"], "write")["metrics_from"] == ["card"] and out["ok"] is True
    # 留下有效的那几项，不重复补
    graph = add_card(weekly())
    config(graph, "write")["metrics_from"] = ["card2", "ghost"]
    fix = plan(graph)[fid]
    assert fix["preview"]["after"] == ["card2"]


def test_a_contract_metrics_from_pointing_nowhere_is_repaired():
    graph = weekly(metrics_from=["ghost"])
    fid = "contract.metrics_from_invalid:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"]["after"] == ["card"]
    assert apply_fixes(graph, [fid], level="governed")["ok"] is True


# --------------------------------------------------------------------------
# choice：要人拿主意，不替人选
# --------------------------------------------------------------------------


def test_required_is_a_choice_over_the_contract_cards_metrics():
    graph = weekly(required=None)
    fid = "contract.required_missing:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["multiple"] is True and fix.get("default") is None
    assert [o["value"] for o in fix["options"]] == ["gmv", "orders"]
    assert all("周报口径卡" in (o.get("hint") or "") for o in fix["options"])
    # 「一键修复」只应用 auto 的：required 不会被顺手填上
    autos = [f for f, v in plan(graph).items() if v["kind"] == "auto"]
    assert fid not in autos
    out = apply_fixes(graph, autos + [fid], level="governed")
    assert "required" not in config(out["graph"], "done")["contract"]
    assert any(r["fix_id"] == fid for r in out["rejected"])
    out = apply_fixes(graph, [fid], {fid: ["gmv"]}, level="governed")
    assert config(out["graph"], "done")["contract"]["required"] == ["gmv"] and out["ok"] is True
    # 空选择不算选了
    out = apply_fixes(graph, [fid], {fid: []}, level="governed")
    assert out["applied"] == []


def test_required_candidates_follow_pinned_cards():
    graph = weekly(required=None)
    card = config(graph, "card")
    card.pop("metrics")
    card["caliber_from"] = {"workflow_id": "wf-lib", "workflow_version": 2, "node_id": "kpi"}
    fid = "contract.required_missing:done"
    fix = plan(graph, cards={"card": [{"id": "net", "name": "净额"}]})[fid]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == ["net"]
    # 查不到钉住那张卡的指标：列不出候选，交给 Copilot（它只能提问，不能编）
    assert plan(graph)[fid]["kind"] == "assist"


def test_a_governed_graph_without_a_contract_gets_a_skeleton():
    graph = weekly()
    config(graph, "done").pop("contract")
    fid = "governed.no_contract:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["node_id"] == "done" and fix["multiple"] is True
    assert [o["value"] for o in fix["options"]] == ["gmv", "orders"]
    out = apply_fixes(graph, [fid], {fid: ["orders"]}, level="governed")
    assert config(out["graph"], "done")["contract"] == {
        "report_from": "write", "metrics_from": ["card"], "required": ["orders"], "strict": True}
    assert out["ok"] is True
    # 口径卡不止一张：骨架该指向哪张说不准，交给 Copilot
    graph = add_card(graph)
    assert plan(graph)[fid]["kind"] == "assist"


def test_a_skeleton_issue_carries_the_fix_on_the_graph_level_issue():
    graph = weekly()
    config(graph, "done").pop("contract")
    spec = GraphSpec.model_validate(graph)
    issues = publish_issues(spec, level="governed")
    marked = autofix.annotate(issues, plan_fixes(spec, issues, level="governed"))
    [issue] = [i for i in marked if i["code"] == "governed.no_contract"]
    assert issue["node_id"] is None and issue["fix"] == "governed.no_contract:done"


def test_structural_problems_go_to_copilot():
    graph = weekly()
    graph["nodes"].insert(1, node("team", "supervisor", "协作团队", goal="查数", agents=[{"name": "a"}]))
    graph["edges"] += [{"source": "start", "target": "team"}, {"source": "team", "target": "fetch"}]
    fix = plan(graph)["governed.supervisor:team"]
    assert fix["kind"] == "assist" and "配置固定" in fix["label"] and fix["node_id"] == "team"
    out = apply_fixes(graph, ["governed.supervisor:team"], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == "governed.supervisor:team"


# --------------------------------------------------------------------------
# 修完必须更好；降低要求的一律拒
# --------------------------------------------------------------------------


def broken() -> dict:
    graph = weekly(metrics_from=None, report_from="fetch", strict=False)
    graph["nodes"].insert(2, node("ask", "agent", "查数员", tools=["db_query__shop"], approval="never",
                                  prompt="用 db_query__shop 查"))
    graph["edges"] += [{"source": "start", "target": "ask"}, {"source": "ask", "target": "card"}]
    return graph


def test_all_auto_fixes_together_leave_fewer_errors_and_keep_the_rest_of_the_graph():
    graph = broken()
    before = errors(graph)
    autos = [f for f, v in plan(graph).items() if v["kind"] == "auto"]
    assert len(autos) == 4, autos
    out = apply_fixes(graph, autos, level="governed")
    assert sorted(out["applied"]) == sorted(autos) and out["rejected"] == []
    assert len(errors(out["graph"])) < len(before) and out["ok"] is True
    # 只动了修复涉及的字段：坐标、宽度、连线 id、视口原样
    assert out["graph"]["viewport"] == graph["viewport"]
    assert [e.get("id") for e in out["graph"]["edges"]] == [e.get("id") for e in graph["edges"]]
    assert all(n.get("width") == 240 and n["position"] == {"x": 10, "y": 20} for n in out["graph"]["nodes"])
    # 原图没被就地改掉
    assert graph == broken()


def test_a_stale_fix_id_is_rejected():
    out = apply_fixes(weekly(), ["contract.strict_off:done"], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == "contract.strict_off:done"


def test_an_edit_that_makes_things_worse_is_discarded(monkeypatch):
    """修复规则自己出错（比如顺手把 metrics_from 删了）：复核发现冒出新错误，整条丢弃。"""
    real = autofix._EDITORS["contract.strict_off"]

    def sloppy(graph, fix, value):
        edits = real(graph, fix, value)
        contract = autofix._node(graph, fix["node_id"])["data"]["config"]["contract"]
        contract.pop("metrics_from")
        return edits

    monkeypatch.setitem(autofix._EDITORS, "contract.strict_off", sloppy)
    graph = weekly(strict=False)
    out = apply_fixes(graph, ["contract.strict_off:done"], level="governed")
    assert out["applied"] == [] and out["graph"] == graph
    [rejected] = out["rejected"]
    assert "新" in rejected["reason"], rejected


def test_judge_wants_fewer_errors_and_nothing_new():
    a = ValidationIssue(level="error", message="a", node_id="n", code="x.a")
    b = ValidationIssue(level="error", message="b", node_id="n", code="x.b")
    c = ValidationIssue(level="error", message="c", node_id="m", code="x.c")
    w = ValidationIssue(level="warning", message="w", node_id="n", code="x.w")
    assert judge([a, b], [b]) is None
    assert judge([a, b], [a, b]) is not None                 # 没变少
    assert judge([a, b], [b, c]) is not None                 # 冒出新的
    assert judge([a, w], [a], target=("x.w", "n")) is None     # 只修警告：错误不增加、那条警告没了
    assert judge([a, w], [a, w], target=("x.w", "n")) is not None


def test_judge_knows_an_issue_by_its_code_not_its_wording():
    """节点改了名，它上面没修好的 error 文案跟着变：有编号的按编号认，还是同一处问题。"""
    a = ValidationIssue(level="error", message="「复核团队」不允许", node_id="n", code="x.a")
    renamed = ValidationIssue(level="error", message="「复核团队（待拆）」不允许", node_id="n", code="x.a")
    b = ValidationIssue(level="error", message="b", node_id="m", code="x.b")
    assert judge([a, b], [renamed]) is None
    assert judge([a, b], [renamed, ValidationIssue(level="error", message="c", node_id="n", code="x.c")]) is not None
    # 没编号的问题只能按文案认
    plain = ValidationIssue(level="error", message="旧文案", node_id="n")
    assert judge([plain, b], [ValidationIssue(level="error", message="新文案", node_id="n")]) is not None


def raised(default: str = "dangerous") -> dict:
    """要求拧得比较高的基线：取数节点每次调用都审批，查数员仅危险工具需要审批，全图默认也写明了，
    契约只放行 7 这一个不带出处的数字。"""
    graph = weekly(allow_numbers=["7"])
    config(graph, "fetch")["approval"] = "always"
    graph["nodes"].insert(2, node("ask", "agent", "查数员", tools=["db_query__shop"], approval="dangerous",
                                  prompt="用 db_query__shop 查"))
    graph["defaults"] = {"approval": default}
    return graph


@pytest.mark.parametrize("mutate, word", [
    (lambda g: g["nodes"].pop(1), "删"),
    (lambda g: g["edges"].pop(0), "连线"),
    (lambda g: config(g, "done").pop("contract"), "契约"),
    (lambda g: config(g, "done").update(contract={}), "契约"),
    (lambda g: config(g, "done")["contract"].update(strict=False), "严格模式"),
    (lambda g: config(g, "done")["contract"].update(required=[]), "必需指标"),
    (lambda g: config(g, "write").update(approval="never"), "全部无需审批"),
    (lambda g: g.setdefault("defaults", {}).update(approval="never"), "全部无需审批"),
    # 审批按「每次调用都审批 > 仅危险工具需要审批 > 全部自动放行」排，往宽里改、删掉都算降低要求
    (lambda g: config(g, "fetch").update(approval="dangerous"), "放宽"),
    (lambda g: config(g, "fetch").update(approval="Always"), "无法识别"),
    # 删掉写明的审批，运行时退回全图默认（这里是仅危险工具需要审批），比原来的每次调用都审批宽
    (lambda g: config(g, "fetch").pop("approval"), "删除了"),
    (lambda g: config(g, "fetch").update(approval=None), "删除了"),
    (lambda g: config(g, "fetch").update(approval=""), "删除了"),
    # 全图默认删掉，退回全局设置：可能是全部自动放行
    (lambda g: g["defaults"].pop("approval"), "工作流默认设置"),
    # 白名单里的数字不用引用也能过；cells 要作者显式声明，不由模型替人打开
    (lambda g: config(g, "done")["contract"]["allow_numbers"].append("120"), "允许无出处的数字"),
    (lambda g: config(g, "done")["contract"].update(allow_numbers=["7", "37"]), "允许无出处的数字"),
    (lambda g: config(g, "done")["contract"].update(cells=True), "单元格引用"),
    # 同一个 id 换了类型：等于删了重建
    (lambda g: find(g, "write").update(type="llm"), "换成"),
])
def test_lowering_the_bar_is_forbidden(mutate, word):
    before = raised()
    after = copy.deepcopy(before)
    mutate(after)
    reasons = forbidden_changes(before, after)
    assert reasons and any(word in r for r in reasons), reasons


def test_unpinning_a_subgraph_is_forbidden():
    before = weekly()
    before["nodes"].insert(1, node("lib", "subgraph", "方法卡", workflow_id="wf-lib", workflow_version=2))
    after = copy.deepcopy(before)
    config(after, "lib").pop("workflow_version")
    assert any("版本" in r for r in forbidden_changes(before, after))


@pytest.mark.parametrize("op", [{"op": "remove_node", "id": "fetch"},
                                {"op": "remove_edge", "source": "start", "target": "fetch"},
                                {"op": "publish", "level": "published"}])
def test_forbidden_ops_are_named(op):
    assert forbidden_changes(weekly(), weekly(), [op])


@pytest.mark.parametrize("mutate, word", [
    (lambda g: config(g, "card").update(approval="dangerous"), "跟随工作流默认设置"),   # 原来跟全图默认：每次调用都审批
    (lambda g: g["defaults"].update(approval="dangerous"), "工作流默认设置"),
    (lambda g: (g["defaults"].pop("approval"), config(g, "card").update(approval="dangerous")), "放宽"),
])
def test_lowering_a_strict_graph_default_is_forbidden(mutate, word):
    before = raised("always")
    after = copy.deepcopy(before)
    mutate(after)
    reasons = forbidden_changes(before, after)
    assert reasons and any(word in r for r in reasons), reasons


def test_dropping_a_node_approval_under_a_stricter_default_is_fine():
    """全图默认是每次调用都审批：删掉节点上的「仅危险工具需要审批」，运行时反而更严。"""
    before = raised("always")
    after = copy.deepcopy(before)
    config(after, "ask").pop("approval")
    assert forbidden_changes(before, after) == []


def test_a_new_contract_cannot_open_cells_or_whitelist_numbers():
    """原来没有契约的出口（或者新加的出口）补上契约：骨架没问题，但 cells、allow_numbers 不许替人写。"""
    before = weekly()
    config(before, "done").pop("contract")
    skeleton = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    after = copy.deepcopy(before)
    config(after, "done")["contract"] = dict(skeleton)
    assert forbidden_changes(before, after) == []
    config(after, "done")["contract"].update(cells=True, allow_numbers=["2026"])
    reasons = forbidden_changes(before, after)
    assert any("单元格引用" in r for r in reasons) and any("允许无出处的数字" in r for r in reasons), reasons
    fresh = copy.deepcopy(before)
    fresh["nodes"].append(node("extra", "output", "另一个出口", contract={**skeleton, "cells": True}))
    assert any("单元格引用" in r for r in forbidden_changes(before, fresh))


def test_overwriting_an_existing_node_is_forbidden():
    """add_node 用了图里已有的 id：Copilot 的改图流程会拿新节点整个盖掉旧的。"""
    before = weekly()
    op = {"op": "add_node", "node": {"id": "write", "type": "report", "label": "报告撰写", "config": {}}}
    assert any("覆盖" in r and "报告撰写" in r for r in forbidden_changes(before, before, [op]))
    # 新 id 的 add_node 没问题
    assert forbidden_changes(before, before, [{**op, "node": {**op["node"], "id": "write2"}}]) == []
    after = copy.deepcopy(before)
    find(after, "write")["type"] = "llm"
    [reason] = forbidden_changes(before, after)
    assert "报告撰写" in reason and "换成" in reason and "删除后重建" in reason, reason


def test_a_type_change_shows_in_the_preview():
    before = weekly()
    after = copy.deepcopy(before)
    find(after, "write")["type"] = "llm"
    changes = autofix.diff_changes(before, after, fix_id="assist", label="Copilot")
    assert {"fix_id": "assist", "node_id": "write", "node_title": "报告撰写", "field": "type", "before": "report",
            "after": "llm", "label": "Copilot"} in changes


def test_raising_the_bar_is_fine():
    before = weekly(strict=False)
    after = copy.deepcopy(before)
    config(after, "done")["contract"].update(strict=True, required=["gmv", "orders"])
    assert forbidden_changes(before, after, [{"op": "update_node", "id": "done", "config": {}}]) == []
    # 审批往严里改、去掉一条「全部自动放行」、白名单收窄、cells 关掉，都不算降低要求
    before = raised()
    config(before, "write")["approval"] = "never"
    config(before, "done")["contract"].update(cells=True, allow_numbers=["7", "37"])
    after = copy.deepcopy(before)
    config(after, "ask")["approval"] = "always"
    config(after, "write").pop("approval")
    config(after, "done")["contract"].update(allow_numbers=["37"])
    config(after, "done")["contract"].pop("cells")
    assert forbidden_changes(before, after) == []
    # 原来跟随全局默认（至多「仅危险工具需要审批」）的节点，写明「仅危险工具需要审批」不算放宽
    before, after = weekly(), weekly()
    config(after, "fetch")["approval"] = "dangerous"
    assert forbidden_changes(before, after) == []


def test_published_level_only_fixes_errors():
    """已发布级别门禁只给警告：不借「一键修复」替人把审批、strict 这些往受管的要求上拧。"""
    graph = broken()
    fixes = plan(graph, level="published")
    assert "governed.agent_approval_never:ask" not in fixes and "contract.strict_off:done" not in fixes
    assert {"contract.metrics_from_missing:done", "contract.report_from_invalid:done"} <= set(fixes)
