"""受管门禁看全图默认的审批策略，自动修复改的也是全图默认。

agent 自己没写 approval 时，运行时跟随图级 defaults（context.approval_mode：节点 > 图级 defaults >
全局设置）。全图默认写着「全部自动放行」，带工具的 agent 就是全部自动放行，门禁以前只看节点
自己写的值，漏了这一类。

- 门禁：每个跟随它的 agent 各报一条（能定位到节点），code 是 governed.default_approval_never；
- 修复：只有一条 auto 修复（落在全图上，不落在节点上），把 defaults.approval 改成 dangerous，
  跟随它的几个节点一起好；节点自己的配置一个字不动；
- 放宽方向照旧禁止。
"""
from __future__ import annotations

import copy

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine import autofix
from app.engine.autofix import annotate, apply_fixes, check, forbidden_changes, plan_fixes
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.schema import GraphSpec
from app.main import app

CODE = "governed.default_approval_never"
FIX = f"{CODE}:graph"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 10, "y": 20}, "width": 240,
            "data": {"label": label or nid, "config": config}}


def weekly(default: str | None = "never", **ask_config) -> dict:
    """input → 查数员（agent，自己没写审批）→ 口径卡 → 报告撰写 → 出口，其余受管要求都满足。"""
    ask = {"tools": ["db_query__shop"], "prompt": "用 db_query__shop 查 orders 的 gmv", **ask_config}
    nodes = [
        node("start", "input", "周期"),
        node("ask", "agent", "查数员", **ask),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.gmv"}]),
        node("write", "report", "报告撰写", instructions="写周报",
             numbers="strict", on_violation="fail", claims="require_citation"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}),
    ]
    edges = [{"id": f"e{i}", "source": a["id"], "target": b["id"]} for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    graph = {"nodes": nodes, "edges": edges, "viewport": {"x": 1, "y": 2, "zoom": 1}}
    if default is not None:
        graph["defaults"] = {"approval": default, "model": "gpt-4o-mini"}
    return graph


def add_agent(graph: dict, nid: str = "ask2", label: str = "复核员", **config) -> dict:
    graph["nodes"].insert(2, node(nid, "agent", label, prompt="用 db_query__shop 复核", **config))
    graph["edges"] += [{"source": "start", "target": nid}, {"source": nid, "target": "card"}]
    return graph


def issues_of(graph: dict, level: str = "governed", code: str = CODE) -> list:
    return [i for i in publish_issues(GraphSpec.model_validate(graph), level=level) if i.code == code]


def config(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)["data"]["config"]


# --------------------------------------------------------------------------
# 门禁
# --------------------------------------------------------------------------


def test_an_agent_following_a_never_default_is_blocked_on_the_governed_level():
    found = issues_of(weekly())
    assert len(found) == 1
    issue = found[0]
    assert issue.level == "error" and issue.node_id == "ask" and issue.field == "approval"
    assert "跟随全图默认" in issue.message and "全部自动放行" in issue.message and "「查数员」" in issue.message
    # 已发布档照旧只给警告
    assert [i.level for i in issues_of(weekly(), level="published")] == ["warning"]


@pytest.mark.parametrize("graph", [
    weekly(default="dangerous"),
    weekly(default="always"),
    weekly(default=None),                         # 没写全图默认：退到全局设置，门禁不查全局
    weekly(approval="dangerous"),                 # 节点自己写明了，不跟随全图默认
    weekly(approval="always"),
    weekly(tools=[]),                             # 没配工具，没有可放行的
], ids=["default-dangerous", "default-always", "no-default", "own-dangerous", "own-always", "no-tools"])
def test_nothing_to_report_when_the_agent_does_not_run_wide_open(graph):
    assert issues_of(graph) == []


def test_an_empty_approval_counts_as_not_written():
    """运行时 ctx.cfg 把 None 和空串都当没写、往全图默认回退：门禁同一个口径。"""
    assert [i.node_id for i in issues_of(weekly(approval=""))] == ["ask"]
    assert [i.node_id for i in issues_of(weekly(approval=None))] == ["ask"]


def test_an_explicit_never_keeps_its_own_code():
    """节点自己写着全部自动放行的，照旧是 agent_approval_never（修的是节点），不重复报一条跟随默认的。"""
    graph = weekly(approval="never")
    assert [i.node_id for i in issues_of(graph, code="governed.agent_approval_never")] == ["ask"]
    assert issues_of(graph) == []


def test_every_following_agent_is_named_on_its_own_node():
    graph = add_agent(weekly(), tools=["db_query__shop"])
    assert sorted(i.node_id for i in issues_of(graph)) == ["ask", "ask2"]


# --------------------------------------------------------------------------
# 修复
# --------------------------------------------------------------------------


def test_one_graph_level_auto_fix_serves_every_follower():
    graph = add_agent(weekly(), tools=["db_query__shop"])
    spec = GraphSpec.model_validate(graph)
    fixes = {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level="governed"), level="governed")}
    assert list(fixes) == [FIX]
    fix = fixes[FIX]
    assert fix["kind"] == "auto" and fix["node_id"] is None and fix["code"] == CODE
    assert fix["preview"] == {"field": "defaults.approval", "before": "never", "after": "dangerous"}
    assert "全图默认" in fix["label"] and "「查数员」" in fix["label"] and "「复核员」" in fix["label"]
    # 两条问题都挂上同一个修复 id（问题在节点上，修复不在）
    out = check(spec, level="governed")
    assert {i["node_id"]: i["fix"] for i in out["issues"] if i["code"] == CODE} == {"ask": FIX, "ask2": FIX}
    assert [f["id"] for f in out["fixes"]] == [FIX]


def test_applying_it_changes_the_graph_default_and_nothing_else():
    graph = add_agent(weekly(), tools=["db_query__shop"])
    before = copy.deepcopy(graph)
    spec = GraphSpec.model_validate(graph)
    label = plan_fixes(spec, publish_issues(spec, level="governed"), level="governed")[0]["label"]
    out = apply_fixes(graph, [FIX], level="governed")
    assert out["applied"] == [FIX] and out["rejected"] == []
    assert out["graph"]["defaults"] == {"approval": "dangerous", "model": "gpt-4o-mini"}
    assert out["ok"] is True and CODE not in {i["code"] for i in out["remaining"]}
    # 节点一个字不动：没给它们写上自己的审批，位置、尺寸、连线、视口原样
    assert out["graph"]["nodes"] == before["nodes"] and out["graph"]["edges"] == before["edges"]
    assert out["graph"]["viewport"] == before["viewport"]
    assert graph == before                                   # 入参没被改
    assert out["changes"] == [{"fix_id": FIX, "node_id": None, "node_title": "", "field": "defaults.approval",
                               "before": "never", "after": "dangerous", "label": label}]
    assert out["ops"] == [{"op": "update_defaults", "defaults": {"approval": "dangerous"}}]
    assert forbidden_changes(before, out["graph"]) == []


def test_a_graph_without_defaults_gets_one():
    """defaults 缺成别的类型也补成对象再写（编辑器不崩、不丢修复）。"""
    graph = weekly()
    out = apply_fixes(graph, [FIX], level="governed")
    assert out["graph"]["defaults"]["approval"] == "dangerous"
    editor = autofix._EDITORS[CODE]
    trial = {"nodes": [], "edges": [], "defaults": None}
    edits = editor(trial, {"_set": {"defaults.approval": "dangerous"}, "kind": "auto"}, None)
    assert trial["defaults"] == {"approval": "dangerous"}
    assert edits == [(None, "defaults.approval", None, "dangerous")]


def test_mixed_explicit_and_following_agents_get_one_fix_each_way():
    graph = add_agent(weekly(), approval="never", tools=["db_query__shop"])
    spec = GraphSpec.model_validate(graph)
    ids = [f["id"] for f in plan_fixes(spec, publish_issues(spec, level="governed"), level="governed")]
    assert sorted(ids) == sorted([FIX, "governed.agent_approval_never:ask2"])
    out = apply_fixes(graph, ids, level="governed")
    assert sorted(out["applied"]) == sorted(ids) and out["ok"] is True
    assert out["graph"]["defaults"]["approval"] == "dangerous" and config(out["graph"], "ask2")["approval"] == "dangerous"
    assert "approval" not in config(out["graph"], "ask")


def test_a_rule_that_does_not_really_fix_it_is_rejected(monkeypatch):
    """图级修复要管住这个编号下挂在各个节点上的每一条：改完还在就不采纳，不能因为问题都挂在
    节点上、修复不在节点上，就当成「本来就没有这处 error」放行。"""
    monkeypatch.setitem(autofix._EDITORS, CODE, lambda graph, fix, value: [])
    out = apply_fixes(weekly(), [FIX], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == FIX
    assert "还在" in out["rejected"][0]["reason"]


@pytest.mark.parametrize("prev, now, word", [
    ("dangerous", "never", "全部自动放行"),
    ("always", "never", "全部自动放行"),
    ("always", "dangerous", "放宽"),
    ("dangerous", None, "全图默认"),
])
def test_loosening_the_graph_default_is_still_forbidden(prev, now, word):
    before = weekly(default=prev)
    after = copy.deepcopy(before)
    if now is None:
        after["defaults"].pop("approval")
    else:
        after["defaults"]["approval"] = now
    reasons = forbidden_changes(before, after)
    assert reasons and any(word in r for r in reasons), reasons


def test_annotate_hands_a_graph_level_fix_to_node_issues_only_by_code():
    """图级修复认同一编号的每一条问题；别的编号、没有编号的不认。"""
    fixes = [{"id": FIX, "code": CODE, "node_id": None}]
    issues = [{"level": "error", "message": "a", "code": CODE, "node_id": "ask"},
              {"level": "error", "message": "b", "code": "governed.agent_approval_never", "node_id": "ask"},
              {"level": "error", "message": "c", "code": None, "node_id": "ask"}]
    assert [i["fix"] for i in annotate(issues, fixes)] == [FIX, None, None]


# --------------------------------------------------------------------------
# 接口：发布前检查和自动修复都认，而且只读
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_the_endpoints_report_and_fix_the_graph_default(client):
    graph = add_agent(weekly(), tools=["db_query__shop"])
    r = await client.post("/api/workflows", json={"name": "周报-全图默认", "graph": graph})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    stored = (await client.get(f"/api/workflows/{wid}")).json()

    r = await client.post(f"/api/workflows/{wid}/publish-check", json={"level": "governed"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False
    assert {i["node_id"]: i["fix"] for i in body["issues"] if i["code"] == CODE} == {"ask": FIX, "ask2": FIX}
    assert [f["id"] for f in body["fixes"]] == [FIX]

    r = await client.post(f"/api/workflows/{wid}/autofix", json={"level": "governed", "apply": [FIX]})
    out = r.json()
    assert r.status_code == 200 and out["ok"] is True and out["applied"] == [FIX]
    assert out["graph"]["defaults"]["approval"] == "dangerous"
    # 只读：库里还是原来那张
    after = (await client.get(f"/api/workflows/{wid}")).json()
    assert after["graph"] == stored["graph"] and after["version"] == stored["version"]

    # 真发布同一个口径：原图被拦，修好的图存下去能发成受管
    blocked = (await client.post(f"/api/workflows/{wid}/publish", json={"level": "governed"})).json()
    assert blocked["ok"] is False and CODE in {i.get("code") for i in blocked["issues"]}
    await client.patch(f"/api/workflows/{wid}", json={"graph": out["graph"]})
    assert (await client.post(f"/api/workflows/{wid}/publish", json={"level": "governed"})).json()["ok"] is True


def test_lint_reads_defaults_off_the_spec_not_the_node():
    """GraphSpec 的 defaults 缺省是空对象：老图没有这个键，门禁照旧。"""
    spec = GraphSpec.model_validate({k: v for k, v in weekly().items() if k != "defaults"})
    assert [i for i in lint_for_publish(spec, level="governed").issues if i.code == CODE] == []
