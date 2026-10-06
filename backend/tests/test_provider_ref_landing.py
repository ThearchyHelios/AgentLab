"""发布前修复点名的模型接入：修完以后执行时落到哪个接入，按执行器的取法算，落不稳的候选不给。

上一轮的「不指定接入」只去掉接入、留下同一处写着的模型，复核又只查点名的接入，于是检查、发布都通过，正式运行在那一处
照样失败：
- 节点点名停用的丙、模型写丙家的模型：去掉接入后按模型名找，找到的还是停用的丙；
- 协作节点点名启用的甲、成员点名停用的丙并写丙家的模型：成员去掉接入后跟随节点，拿甲去调丙家的模型；
- 节点点名丙并写丙家的模型、全图默认点名乙：同样拿乙去调丙家的模型；
- 双盲复算、反方审稿、结论句裁判同理：按模型名落到停用的接入，记缺口或未裁判。

现在：
- 门禁把「没有一级写接入、只写了模型，提供它的接入已停用」也记成 provider.disabled，落在写模型的那一处，原因和运行时
  resolve_provider 报的同一句：只写模型的节点（多数节点的检查器里只能选模型）发布前查得出来，复核也拒得掉这类修复；
- 修复的每个候选先在副本上改，按执行时的取法算出波及的每一处落到哪里（成员先看自己、再看节点、再看全图默认；节点看
  全图默认；都没写按模型名找；核查、裁判整组交给设置）。落到的接入不提供这一处的模型、按模型名落到停用的接入或认不出
  的模型，就把同一级写着的模型一并去掉再算；还落不稳的候选不给。候选说明写清执行时改用哪里、模型去没去掉；
- 只写了模型的那一处也有修复：改用一个启用的接入（模型换成它的），或不指定模型。

纯函数：接入表是虚构的几行，不读库。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine import autofix
from app.engine.autofix import apply_fixes, check
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec


def provider(name: str, *models: str, enabled: bool = True, kind: str = "openai_compatible") -> SimpleNamespace:
    return SimpleNamespace(id=f"pv-{name}", name=name, kind=kind, enabled=enabled,
                           models=[{"id": m} for m in models], default_model=models[0] if models else None)


#: 甲排在最前：没写接入也没写模型时落到它（第一个可用的接入）
JIA = provider("甲接入", "甲一号", "甲二号")
YI = provider("乙接入", "乙一号")
BING = provider("丙接入", "丙一号", enabled=False)
TABLE = [JIA, YI, BING]
STOPPED = "所属的模型接入「丙接入」已停用"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def flow(*middle, defaults=None) -> dict:
    nodes = [node("start", "input"), *middle,
             node("out", "output", fields=[{"name": "结论", "value": "{{ last_message }}"}])]
    graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    if defaults:
        graph["defaults"] = defaults
    return graph


def provider_issues(issues) -> list[dict]:
    return [i for i in map(autofix._as_dict, issues) if str(i.get("code") or "").startswith("provider.")]


def gate(graph: dict, table=TABLE) -> list[dict]:
    return provider_issues(publish_issues(GraphSpec.model_validate(graph), level="published", providers=table))


def fix_of(graph: dict, fid: str, table=TABLE) -> dict | None:
    found = check(GraphSpec.model_validate(graph), level="published", providers=table)
    return next((f for f in found["fixes"] if f["id"] == fid), None)


def hints(fix: dict) -> dict:
    return {o["value"]: o.get("hint") for o in fix["options"]}


def apply(graph: dict, choices: dict, table=TABLE) -> dict:
    return apply_fixes(graph, list(choices), choices, level="published", providers=table)


def config_of(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)["data"]["config"]


def changes(out: dict) -> list[tuple]:
    return [(c["node_id"], c["field"], c["before"], c["after"]) for c in out["changes"]]


# --------------------------------------------------------------------------
# 门禁：只写了模型、提供它的接入已停用
# --------------------------------------------------------------------------


def test_a_node_naming_only_a_model_of_a_disabled_provider_is_stopped_before_publishing():
    """检查器里多数节点只能选模型：选的是停用接入的模型，运行时 resolve_provider 报「所属的模型接入已停用」，发布前就该看到。"""
    [issue] = gate(flow(node("ask", "llm", "提问", model="丙一号", prompt="总结")))
    assert (issue["level"], issue["code"], issue["node_id"], issue["field"]) == (
        "error", "provider.disabled", "ask", "model")
    assert issue["message"] == ("「提问」（模型调用）指定的模型「丙一号」所属的模型接入「丙接入」已停用。"
                                "请在「设置 → 模型接入」中启用它，或在节点中换一个模型")
    # 启用的接入提供它；模型名认不出来（手填的新模型，运行时退回第一个可用的接入）；同一处点名了启用的接入（以点名的为准，
    # 不按模型名找）：都不提
    for config in ({"model": "甲二号"}, {"model": "自填的新模型"}, {"model": "丙一号", "provider": "甲接入"}):
        assert gate(flow(node("ask", "llm", "提问", prompt="总结", **config))) == [], config


def test_the_problem_is_recorded_once_where_the_model_is_written():
    """全图默认里的模型几个节点共用时只记一条；协作成员、全图默认里的模型在检查器里改不了，提示指到发布前检查。
    节点点名了启用的接入时，跟随它的成员、取全图默认模型的节点以点名的为准，不提。"""
    graph = flow(node("a", "llm", "提问", prompt="总结"), node("b", "agent", "查数", prompt="查数"),
                 node("team", "supervisor", "协作", provider="甲接入", goal="分析",
                      agents=[{"name": "分析师", "model": "丙一号", "system": "分析"}]),
                 node("team2", "supervisor", "协作二", goal="分析",
                      agents=[{"name": "研究员", "model": "丙一号", "system": "研究"}]),
                 defaults={"model": "丙一号"})
    got = [(i["node_id"], i["field"], i["message"]) for i in gate(graph)]
    tail = "请在「设置 → 模型接入」中启用它，或在「发布前检查」中改用其他接入"
    assert got == [
        ("team2", "agents[0].model", f"「协作二」（多 Agent 协作）的成员「研究员」指定的模型「丙一号」{STOPPED}。{tail}"),
        (None, "defaults.model", f"工作流默认设置指定的模型「丙一号」{STOPPED}。{tail}"),
    ]


def test_verify_nodes_and_the_judge_naming_only_a_model_of_a_disabled_provider_are_checked_too():
    """双盲复算、反方审稿、结论句裁判只写了模型时按模型名找接入（整组以节点为准，不交给设置）。"""
    graph = flow(node("write", "report", "写周报", instructions="写周报", provider="甲接入", claims="judge",
                      judge={"model": "丙一号"}),
                 node("dual", "dual_check", "复核销售额", model="丙一号"),
                 node("chal", "challenge", "审稿", report="write", model="丙一号"))
    got = {(i["node_id"], i["field"]): i["message"] for i in gate(graph)}
    assert set(got) == {("write", "judge.model"), ("dual", "model"), ("chal", "model")}
    assert got[("dual", "model")] == (f"「复核销售额」（双盲复算）的盲算模型指定的模型「丙一号」{STOPPED}。"
                                      "请在「设置 → 模型接入」中启用它，或在节点中换一个模型")
    assert got[("write", "judge.model")].startswith(f"「写周报」（报告撰写）的结论句裁判指定的模型「丙一号」{STOPPED}")


# --------------------------------------------------------------------------
# 「不指定接入」：按执行时的取法算出去掉以后落到哪里
# --------------------------------------------------------------------------


def test_stop_naming_a_disabled_provider_drops_the_model_only_it_provides_too():
    """复核发现的原样：节点点名停用的丙、模型写丙家的。只去掉接入的话，按模型名找到的还是停用的丙。"""
    graph = flow(node("ask", "llm", "提问", provider="丙接入", model="丙一号", prompt="总结"))
    fid = "provider.disabled:ask:provider"
    assert hints(fix_of(graph, fid))[""] == "去掉这一处指定的接入和模型「丙一号」，执行时改用第一个可用的模型接入「甲接入」"
    out = apply(graph, {fid: ""})
    assert out["applied"] == [fid] and out["ok"] is True, out["rejected"]
    assert config_of(out["graph"], "ask") == {"prompt": "总结"}
    assert changes(out) == [("ask", "provider", "丙接入", None), ("ask", "model", "丙一号", None)]
    assert gate(out["graph"]) == []

    # 写的模型有启用的接入提供：只去掉接入，按模型名落到提供它的那一家，模型留着
    graph = flow(node("ask", "llm", "提问", provider="丙接入", model="乙一号", prompt="总结"))
    assert hints(fix_of(graph, fid))[""] == "去掉这一处指定的接入，执行时改用提供模型「乙一号」的接入「乙接入」"
    out = apply(graph, {fid: ""})
    assert out["ok"] is True and config_of(out["graph"], "ask") == {"prompt": "总结", "model": "乙一号"}, out["rejected"]


def test_a_member_that_stops_naming_a_provider_follows_its_team_node():
    """成员去掉接入后跟随节点点名的甲：甲不提供成员写的模型，模型一并去掉（不然拿甲去调丙家的模型）；甲提供的就留着。"""
    graph = flow(node("team", "supervisor", "协作", provider="甲接入", goal="分析",
                      agents=[{"name": "分析师", "provider": "丙接入", "model": "丙一号", "system": "分析"},
                              {"name": "复核员", "provider": "丙接入", "model": "甲二号", "system": "复核"}]))
    first, second = "provider.disabled:team:agents[0].provider", "provider.disabled:team:agents[1].provider"
    assert hints(fix_of(graph, first))[""] == ("去掉这一处指定的接入和模型「丙一号」，"
                                              "执行时改用「协作」（多 Agent 协作）指定的接入「甲接入」")
    assert hints(fix_of(graph, second))[""] == "去掉这一处指定的接入，执行时改用「协作」（多 Agent 协作）指定的接入「甲接入」"
    out = apply(graph, {first: "", second: ""})
    assert out["applied"] == [first, second] and out["ok"] is True, out["rejected"]
    assert config_of(out["graph"], "team")["agents"] == [{"name": "分析师", "system": "分析"},
                                                        {"name": "复核员", "model": "甲二号", "system": "复核"}]


def test_stop_naming_a_provider_under_a_graph_default_drops_a_model_the_default_does_not_provide():
    graph = flow(node("ask", "llm", "提问", provider="丙接入", model="丙一号", prompt="总结"), defaults={"provider": "乙接入"})
    fid = "provider.disabled:ask:provider"
    assert hints(fix_of(graph, fid))[""] == ("去掉这一处指定的接入和模型「丙一号」，"
                                            "执行时改用工作流默认设置指定的接入「乙接入」")
    out = apply(graph, {fid: ""})
    assert out["ok"] is True and config_of(out["graph"], "ask") == {"prompt": "总结"}, out["rejected"]


@pytest.mark.parametrize("ntype,extra", [("dual_check", {}), ("challenge", {"report": "write"})])
def test_a_verify_node_that_stops_naming_a_provider_goes_to_the_settings_not_a_disabled_provider(ntype, extra):
    """核查节点只写了模型时按模型名找接入：丙家的模型留着就又落到停用的丙。连模型一起去掉，整组交给设置里的核查模型。"""
    graph = flow(node("write", "report", "写周报", instructions="写周报", provider="甲接入"),
                 node("v", ntype, "核查", provider="丙接入", model="丙一号", **extra))
    fid = "provider.disabled:v:provider"
    assert hints(fix_of(graph, fid))[""] == "去掉这一处指定的接入和模型「丙一号」，执行时改用设置中的核查模型"
    out = apply(graph, {fid: ""})
    assert out["applied"] == [fid] and gate(out["graph"]) == [], out["rejected"]
    assert config_of(out["graph"], "v") == extra


def test_a_judge_that_stops_naming_a_provider_goes_to_the_settings_not_a_disabled_provider():
    graph = flow(node("write", "report", "写周报", instructions="写周报", provider="甲接入", claims="judge",
                      judge={"provider": "丙接入", "model": "丙一号", "max_claims": 5}))
    fid = "provider.disabled:write:judge.provider"
    assert hints(fix_of(graph, fid))[""] == "去掉这一处指定的接入和模型「丙一号」，执行时改用设置中的裁判模型"
    out = apply(graph, {fid: ""})
    assert out["applied"] == [fid] and gate(out["graph"]) == [], out["rejected"]
    assert config_of(out["graph"], "write")["judge"] == {"max_claims": 5}


# --------------------------------------------------------------------------
# 换接入：跟着这一处的别处也要落得稳
# --------------------------------------------------------------------------


def test_a_switch_that_would_send_another_place_a_model_it_does_not_provide_is_not_offered():
    """协作节点换接入，没写接入的成员跟着换：成员自己写的乙家模型甲不提供，「改用甲」不给；「不指定接入」各处分别
    落到哪里写清。"""
    graph = flow(node("team", "supervisor", "协作", provider="丙接入", model="丙一号", goal="分析",
                      agents=[{"name": "分析师", "model": "乙一号", "system": "分析"}]))
    got = hints(fix_of(graph, "provider.disabled:team:provider"))
    assert set(got) == {"", "乙接入"}
    assert got["乙接入"] == "不提供模型「丙一号」，将改用它的默认模型「乙一号」"
    assert got[""] == ("去掉这一处指定的接入和模型「丙一号」，执行时「协作」（多 Agent 协作）改用第一个可用的模型接入「甲接入」；"
                       "「协作」（多 Agent 协作）的成员「分析师」改用提供模型「乙一号」的接入「乙接入」")

    # 全图默认同理：自己写了乙家模型的节点跟着换到甲，甲不提供
    graph = flow(node("a", "llm", "提问", prompt="总结"), node("b", "llm", "复述", model="乙一号", prompt="复述"),
                 defaults={"provider": "丙接入"})
    fid = "provider.disabled:graph:defaults.provider"
    got = hints(fix_of(graph, fid))
    assert set(got) == {"", "乙接入"} and got["乙接入"] is None
    assert got[""] == ("去掉这一处指定的接入，执行时「提问」（模型调用）改用第一个可用的模型接入「甲接入」；"
                       "「复述」（模型调用）改用提供模型「乙一号」的接入「乙接入」")
    out = apply(graph, {fid: "乙接入"})
    assert out["ok"] is True and out["graph"]["defaults"] == {"provider": "乙接入"}, out["rejected"]


def test_switching_names_the_model_actually_used_when_a_higher_level_writes_one():
    """成员换到甲、自己的丙家模型去掉以后，模型跟随节点写的甲二号，不是甲的默认模型：说明照实写。"""
    graph = flow(node("team", "supervisor", "协作", model="甲二号", goal="分析",
                      agents=[{"name": "分析师", "provider": "丙接入", "model": "丙一号", "system": "分析"}]))
    got = hints(fix_of(graph, "provider.disabled:team:agents[0].provider"))
    assert got["甲接入"] == "不提供模型「丙一号」，将改用模型「甲二号」"


def test_no_choice_lands_on_the_built_in_demo_model():
    """一个启用的接入都没有：去掉接入就会悄悄换成内置演示模型，这样的修复不给（问题照样拦着发布）。"""
    graph = flow(node("ask", "llm", "提问", provider="丙接入", model="丙一号", prompt="总结"))
    found = check(GraphSpec.model_validate(graph), level="published", providers=[BING])
    [issue] = provider_issues(found["issues"])
    assert issue["fix"] is None and found["ok"] is False


# --------------------------------------------------------------------------
# 只写了模型的那一处：改用一个启用的接入，或不指定模型
# --------------------------------------------------------------------------


def test_a_place_naming_only_a_model_gets_a_choice_of_provider_or_no_model():
    graph = flow(node("ask", "llm", "提问", model="丙一号", prompt="总结"))
    fid = "provider.disabled:ask:model"
    fix = fix_of(graph, fid)
    assert (fix["kind"], fix["field"], fix["label"]) == ("choice", "model", "选择「提问」（模型调用）改用的模型接入")
    assert [o["label"] for o in fix["options"]] == ["不指定模型", "甲接入", "乙接入"]
    got = hints(fix)
    assert got[""] == "去掉这一处指定的模型，执行时改用第一个可用的模型接入「甲接入」"
    assert got["乙接入"] == "不提供模型「丙一号」，将改用它的默认模型「乙一号」"
    out = apply(graph, {fid: "乙接入"})
    assert out["ok"] is True and config_of(out["graph"], "ask") == {"prompt": "总结", "provider": "乙接入"}, out["rejected"]
    assert changes(out) == [("ask", "provider", None, "乙接入"), ("ask", "model", "丙一号", None)]
    out = apply(graph, {fid: ""})
    assert out["ok"] is True and config_of(out["graph"], "ask") == {"prompt": "总结"}, out["rejected"]


def test_a_graph_default_model_is_fixed_once_for_every_node_that_uses_it():
    graph = flow(node("a", "llm", "提问", prompt="总结"), node("b", "agent", "查数", prompt="查数"),
                 defaults={"model": "丙一号"})
    fid = "provider.disabled:graph:defaults.model"
    out = apply(graph, {fid: "甲接入"})
    assert out["ok"] is True and out["graph"]["defaults"] == {"provider": "甲接入"}, out["rejected"]
    assert changes(out) == [(None, "defaults.provider", None, "甲接入"), (None, "defaults.model", "丙一号", None)]


# --------------------------------------------------------------------------
# 复核照样把关
# --------------------------------------------------------------------------


def test_the_review_refuses_a_fix_that_leaves_a_place_on_a_disabled_provider(monkeypatch):
    """修复规则自己只去掉接入（上一轮的做法）时，门禁查出那一处按模型名落到停用的丙，整条丢弃、说清原因。"""
    def only_the_provider(graph, fix, value):
        config = autofix._node(graph, fix["node_id"])["data"]["config"]
        return [(fix["node_id"], "provider", config.pop("provider"), None)]

    monkeypatch.setitem(autofix._EDITORS, "provider.disabled", only_the_provider)
    graph = flow(node("ask", "llm", "提问", provider="丙接入", model="丙一号", prompt="总结"))
    out = apply(graph, {"provider.disabled:ask:provider": ""})
    assert out["applied"] == [] and out["changes"] == [] and out["graph"] == graph
    [rejected] = out["rejected"]
    assert rejected["reason"] == (f"修改后出现了新的问题：「提问」（模型调用）指定的模型「丙一号」{STOPPED}。"
                                  "请在「设置 → 模型接入」中启用它，或在节点中换一个模型")
