"""四期：受管模板放开 claims: judge（结论句由另一个模型按证据逐句裁判），但要写明预算。

- G3：报告撰写节点的 claims 在受管级别认 require_citation 或 judge；写 judge 的，生效的 judge 配置里
  必须有 max_cost_usd 这个键——写金额，或者显式写 null 表示不限。没写就是 error（已发布级别只给警告）：
  受管出具不能让每份报告的裁判花费悄悄按默认值走，也不能悄悄不设上限
- 发布前自动修复：缺预算是 choice，候选是默认金额和「不限」，不替人选；G3 另外那几项的一键修复不再把
  judge 改回 require_citation
- 降低要求的判断：require_citation → judge 是往严里改；judge → require_citation / off / 删掉都算放宽，
  报告节点上和出具契约里一样
"""
from __future__ import annotations

import copy

import pytest

from app.engine.autofix import NEED_CHOICE, apply_fixes, forbidden_changes, plan_fixes, public
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.judge import JUDGE_DEFAULTS
from app.engine.schema import GraphSpec, validate_graph

STRICT = {"numbers": "strict", "on_violation": "fail", "claims": "require_citation"}
JUDGED = {**STRICT, "claims": "judge", "judge": {"max_cost_usd": 0.05}}
BUDGET = "governed.judge_budget"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 10, "y": 20},
            "data": {"label": label or nid, "config": config}}


def traceable(*, report=None, defaults=None, **contract_overrides) -> dict:
    """input → 查库 → 口径卡 → 报告撰写 → 出具：受管要求（含 G1–G5）全部满足。"""
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    contract.update(contract_overrides)
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "报告撰写", instructions="写周报", **(JUDGED if report is None else report)),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={k: v for k, v in contract.items() if v is not None}),
    ]
    edges = [{"id": f"e{i}", "source": a["id"], "target": b["id"]} for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    graph = {"nodes": nodes, "edges": edges}
    if defaults is not None:
        graph["defaults"] = defaults
    return graph


def config(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)["data"]["config"]


def by_code(graph: dict, code: str, level: str = "governed") -> list:
    return [i for i in lint_for_publish(GraphSpec.model_validate(graph), level=level).issues if i.code == code]


def errors(graph: dict, level: str = "governed") -> list:
    return [i for i in publish_issues(GraphSpec.model_validate(graph), level=level) if i.level == "error"]


def plan(graph: dict, level: str = "governed") -> dict[str, dict]:
    spec = GraphSpec.model_validate(graph)
    return {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level=level), level=level)}


# --------------------------------------------------------------------------
# G3：judge 可以发布成受管，但要写预算
# --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [0.05, 1, None])
def test_judge_with_a_budget_publishes_as_governed(budget):
    graph = traceable(report={**JUDGED, "judge": {"max_cost_usd": budget, "max_claims": 20}})
    assert errors(graph) == [], [i.message for i in errors(graph)]
    assert by_code(graph, "governed.report_policy") == [] and by_code(graph, BUDGET) == []


@pytest.mark.parametrize("judge", [None, {}, {"max_claims": 20, "model": "judge-model"}])
def test_judge_without_a_budget_is_an_error_at_governed(judge):
    report = {**STRICT, "claims": "judge", **({"judge": judge} if judge is not None else {})}
    graph = traceable(report=report)
    [issue] = by_code(graph, BUDGET)
    assert issue.level == "error" and issue.node_id == "write" and issue.field == "judge.max_cost_usd"
    assert "max_cost_usd" in issue.message and "不限" in issue.message and "null" in issue.message
    assert by_code(graph, "governed.report_policy") == [], "judge 本身合规，只缺预算"
    assert [i.level for i in by_code(graph, BUDGET, "published")] == ["warning"]


def test_the_budget_follows_the_same_config_rule_as_run_time():
    """和运行时同一个取值规则：节点没写 judge 取图级 defaults；节点写了就整个用节点的（不和 defaults 拼）。"""
    graph = traceable(report={**STRICT, "claims": "judge"}, defaults={"judge": {"max_cost_usd": 0.02}})
    assert by_code(graph, BUDGET) == []
    graph = traceable(report={**STRICT, "claims": "judge", "judge": {"max_claims": 5}},
                      defaults={"judge": {"max_cost_usd": 0.02}})
    assert [i.field for i in by_code(graph, BUDGET)] == ["judge.max_cost_usd"]
    # claims 本身也可以来自图级 defaults
    graph = traceable(report={"numbers": "strict", "on_violation": "fail"},
                      defaults={"claims": "judge", "judge": {"max_cost_usd": None}})
    assert by_code(graph, BUDGET) == [] and by_code(graph, "governed.report_policy") == []


def test_a_malformed_budget_is_left_to_validate():
    graph = traceable(report={**STRICT, "claims": "judge", "judge": {"max_cost_usd": "cheap"}})
    assert by_code(graph, BUDGET) == [], "写错的值由 validate 的 report.judge_invalid 报，同一处不说两遍"
    [bad] = [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.code == "report.judge_invalid"]
    assert bad.field == "judge.max_cost_usd" and bad.level == "error"


@pytest.mark.parametrize("claims", [None, "off"])
def test_a_loose_claims_setting_names_both_accepted_values(claims):
    report = {"numbers": "strict", "on_violation": "fail", **({"claims": claims} if claims else {})}
    [issue] = by_code(traceable(report=report), "governed.report_policy")
    assert issue.field == "claims" and "claims: require_citation" in issue.message and "judge" in issue.message


def test_judge_needs_no_other_report_policy_fix():
    graph = traceable(report={**JUDGED, "numbers": "off"})
    [issue] = by_code(graph, "governed.report_policy")
    assert issue.field == "numbers" and "claims" not in issue.message


# --------------------------------------------------------------------------
# 发布前自动修复
# --------------------------------------------------------------------------


def test_the_report_policy_fix_leaves_judge_alone():
    graph = traceable(report={**JUDGED, "numbers": "off", "on_violation": "flag"})
    fix = plan(graph)["governed.report_policy:write"]
    assert fix["kind"] == "auto" and fix["preview"]["after"] == {"numbers": "strict", "on_violation": "fail"}
    out = apply_fixes(graph, [fix["id"]], level="governed")
    assert out["ok"] is True and config(out["graph"], "write")["claims"] == "judge"
    assert config(out["graph"], "write")["judge"] == {"max_cost_usd": 0.05}


def test_a_missing_budget_is_a_choice_between_the_default_and_unlimited():
    graph = traceable(report={**STRICT, "claims": "judge", "judge": {"model": "judge-model"}})
    fid = f"{BUDGET}:write"
    fix = public(plan(graph)[fid])
    assert fix["kind"] == "choice" and fix["node_id"] == "write" and fix["field"] == "judge.max_cost_usd"
    assert [o["value"] for o in fix["options"]] == [JUDGE_DEFAULTS["report_max_cost_usd"], None]
    assert "default" not in fix, "花多少钱要人定：不替人选，也不给建议"
    unlimited = fix["options"][1]
    assert "不限" in unlimited["label"] and "不设上限" in unlimited["hint"] and "约束" in unlimited["hint"]
    assert all(not k.startswith("_") for k in fix), "内部键不交给前端"

    [refused] = apply_fixes(graph, [fid], level="governed")["rejected"]
    assert refused["reason"] == NEED_CHOICE.format(label=fix["label"])

    out = apply_fixes(graph, [fid], {fid: 0.05}, level="governed")
    assert out["applied"] == [fid] and out["ok"] is True
    assert config(out["graph"], "write")["judge"] == {"model": "judge-model", "max_cost_usd": 0.05}
    [change] = out["changes"]
    assert change["field"] == "judge" and change["before"] == {"model": "judge-model"}
    assert out["ops"] == [{"op": "update_node", "id": "write",
                           "config": {"judge": {"model": "judge-model", "max_cost_usd": 0.05}}}]

    out = apply_fixes(graph, [fid], {fid: None}, level="governed")
    judge = config(out["graph"], "write")["judge"]
    assert out["ok"] is True and "max_cost_usd" in judge and judge["max_cost_usd"] is None, "不限要显式写成 null"


def test_the_budget_fix_keeps_what_the_graph_defaults_said():
    """judge 写在图级 defaults 里：修复写到节点上时带上 defaults 里的整份 judge，节点写了就不再跟随 defaults，
    不能因为补一个预算把 defaults 里选的裁判模型丢了。"""
    graph = traceable(report={**STRICT, "claims": "judge"},
                      defaults={"judge": {"model": "judge-model", "rewrite_once": True}})
    fid = f"{BUDGET}:write"
    out = apply_fixes(graph, [fid], {fid: None}, level="governed")
    assert out["ok"] is True
    assert config(out["graph"], "write")["judge"] == {"model": "judge-model", "rewrite_once": True,
                                                      "max_cost_usd": None}
    assert out["graph"]["defaults"] == {"judge": {"model": "judge-model", "rewrite_once": True}}


def test_a_choice_outside_the_options_is_refused():
    graph = traceable(report={**STRICT, "claims": "judge"})
    fid = f"{BUDGET}:write"
    out = apply_fixes(graph, [fid], {fid: 99}, level="governed")
    assert out["applied"] == [] and "不在候选里" in out["rejected"][0]["reason"]


def test_published_level_budget_warnings_get_no_fix():
    assert f"{BUDGET}:write" not in plan(traceable(report={**STRICT, "claims": "judge"}), "published")


def test_invalid_claims_fixes_no_longer_mention_a_later_version():
    graph = traceable(report={**STRICT, "claims": "sometimes"})
    governed = plan(graph)["report.claims_invalid:write"]
    published = plan(graph, "published")["report.claims_invalid:write"]
    assert "后续版本" not in governed["label"] and "default" not in published
    assert [o["value"] for o in published["options"]] == ["off", "require_citation"]


# --------------------------------------------------------------------------
# 降低要求的判断
# --------------------------------------------------------------------------


def _changed(report_before: dict, report_after: dict | None, **graph_kw) -> list[str]:
    old = traceable(report=report_before, **graph_kw)
    new = copy.deepcopy(old)
    if report_after is None:
        config(new, "write").pop("claims")
    else:
        config(new, "write").clear()
        config(new, "write").update({"instructions": "写周报", **report_after})
    return forbidden_changes(old, new)


def test_moving_up_to_judge_is_not_loosening():
    assert _changed(STRICT, JUDGED) == []
    assert _changed({**STRICT, "claims": "off"}, JUDGED) == []
    assert _changed({"numbers": "strict", "on_violation": "fail"}, JUDGED) == []


@pytest.mark.parametrize("after", ["off", "require_citation", None])
def test_moving_down_from_judge_is_loosening(after):
    report_after = None if after is None else {**JUDGED, "claims": after}
    [reason] = _changed(JUDGED, report_after)
    assert "「报告撰写」" in reason and "claims" in reason and "judge" in reason


def test_dropping_a_judge_set_in_the_graph_defaults_is_loosening():
    [reason] = _changed({"numbers": "strict", "on_violation": "fail"}, {**STRICT, "claims": "off"},
                        defaults={"claims": "judge", "judge": {"max_cost_usd": 0.05}})
    assert "claims" in reason and "judge" in reason


def test_changing_the_budget_is_not_loosening():
    assert _changed(JUDGED, {**JUDGED, "judge": {"max_cost_usd": None}}) == []


# claims 一直是 judge 时，judge.on_unsupported 从 withhold 往回改也是放宽：不支持的结论句从「不予出具」变成
# 「降档出具」。Copilot 改图的 update_node 会把 judge 整个盖掉，补预算时写 judge={max_cost_usd: 0.05}
# 就悄悄丢了 withhold——这一类要和契约里的 on_unsupported 一样拒掉
WITHHOLD = {**JUDGED, "judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}


@pytest.mark.parametrize("judge, dropped", [
    ({"max_cost_usd": 0.05, "on_unsupported": "degrade"}, False),
    ({"max_cost_usd": 0.05}, True),
    ({"max_cost_usd": 0.05, "on_unsupported": "sometimes"}, False),
])
def test_relaxing_on_unsupported_on_the_node_is_loosening(judge, dropped):
    [reason] = _changed(WITHHOLD, {**WITHHOLD, "judge": judge})
    assert "「报告撰写」" in reason and "judge.on_unsupported" in reason
    assert "withhold" in reason and "degrade" in reason
    assert ("去掉了写明的值" in reason) is dropped, reason


def test_replacing_the_whole_judge_block_to_add_a_budget_is_loosening():
    """补预算把 judge 整个换成 {max_cost_usd}：连同裁判模型、withhold 一起丢了。"""
    before = {**STRICT, "claims": "judge", "judge": {"model": "judge-model", "on_unsupported": "withhold"}}
    [reason] = _changed(before, {**before, "judge": {"max_cost_usd": 0.05}})
    assert "judge.on_unsupported" in reason and "withhold" in reason
    assert _changed(before, {**before, "judge": {**before["judge"], "max_cost_usd": 0.05}}) == [], \
        "带着原来的键补预算不算放宽"


def test_a_withhold_from_the_graph_defaults_lost_by_a_node_judge_is_loosening():
    """judge 按运行时的规则取：节点写了自己的 judge 就整个用节点的，图级 defaults 里的 withhold 不再生效。"""
    defaults = {"judge": {"on_unsupported": "withhold", "max_cost_usd": 0.05}}
    report = {**STRICT, "claims": "judge"}
    [reason] = _changed(report, {**report, "judge": {"max_cost_usd": 0.05}}, defaults=defaults)
    assert "judge.on_unsupported" in reason and "从 withhold（跟随全图默认）" in reason, reason
    assert "不再跟随全图默认" in reason, "说清楚为什么全图默认里的 withhold 不生效了"
    assert _changed(report, {**report, "judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}},
                    defaults=defaults) == []


def test_relaxing_on_unsupported_in_the_graph_defaults_is_loosening():
    old = traceable(report={**STRICT, "claims": "judge"},
                    defaults={"judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}})
    new = copy.deepcopy(old)
    new["defaults"]["judge"]["on_unsupported"] = "degrade"
    [reason] = forbidden_changes(old, new)
    assert "「报告撰写」" in reason and "judge.on_unsupported" in reason


@pytest.mark.parametrize("before, after", [
    (JUDGED, {**JUDGED, "judge": {"max_cost_usd": 0.05, "on_unsupported": "withhold"}}),
    (WITHHOLD, {**WITHHOLD, "judge": {**WITHHOLD["judge"], "max_cost_usd": None, "rewrite_once": True}}),
    (WITHHOLD, WITHHOLD),
    # 升到 judge 本身是收紧：不管带不带 withhold
    ({**STRICT, "judge": {"on_unsupported": "withhold"}}, JUDGED),
])
def test_tightening_or_keeping_on_unsupported_is_fine(before, after):
    assert _changed(before, after) == []


def test_dropping_withhold_along_with_judge_is_one_reason():
    """从 judge 退回 require_citation：claims 那一条已经说了，不再为 on_unsupported 另记一条。"""
    [reason] = _changed(WITHHOLD, {**STRICT, "judge": {"max_cost_usd": 0.05}})
    assert "claims" in reason and "on_unsupported" not in reason


@pytest.mark.parametrize("before", ["require_citation", "judge"])
def test_moving_claims_to_an_unknown_value_is_loosening(before):
    """require_citation、judge 改成认不出的写法：出口按认不出的走，要求没了——照样算放宽，别只落个
    「没让图变好」。原来就认不出的不比较。"""
    report = {**STRICT, "claims": before, **({"judge": {"max_cost_usd": 0.05}} if before == "judge" else {})}
    [reason] = _changed(report, {**report, "claims": "sometimes"})
    assert "「报告撰写」" in reason and before in reason and "sometimes" in reason and "不认识" in reason
    assert _changed({**STRICT, "claims": "sometimes"}, {**STRICT, "claims": "off"}) == []
    assert _changed({**STRICT, "claims": "off"}, {**STRICT, "claims": "sometimes"}) == []


def _contract_changed(before, after) -> list[str]:
    old = traceable(claims=before)
    new = copy.deepcopy(old)
    contract = config(new, "done")["contract"]
    if after is None:
        contract.pop("claims", None)
    else:
        contract["claims"] = after
    return forbidden_changes(old, new)


@pytest.mark.parametrize("before, after", [
    ("judge", "off"),
    ("judge", None),
    ("judge", "require_citation"),
    ({"policy": "judge", "on_unsupported": "withhold"}, "judge"),
    ({"policy": "judge", "on_unsupported": "withhold"}, {"policy": "judge", "on_unsupported": "degrade"}),
    ({"policy": "require_citation", "on_uncited": "withhold"}, "judge"),
])
def test_loosening_a_judge_contract_is_refused(before, after):
    [reason] = _contract_changed(before, after)
    assert "「出具」" in reason and "claims" in reason


@pytest.mark.parametrize("before, after", [
    ("require_citation", "judge"),
    ("off", {"policy": "judge", "on_unsupported": "withhold"}),
    ("judge", {"policy": "judge", "on_unsupported": "withhold"}),
    ({"policy": "require_citation", "on_uncited": "withhold"}, {"policy": "judge", "on_uncited": "withhold"}),
])
def test_tightening_a_contract_towards_judge_is_fine(before, after):
    assert _contract_changed(before, after) == []
