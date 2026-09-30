"""证据门禁 G1–G5 接进发布前自动修复（engine/autofix.py）。

照发布前自动修复的原则：答案唯一、只提高要求的一键修（auto）；要人拿主意的列候选、不替人选（choice）；
结构性的交给 Copilot（assist）。修完重跑 validate 和门禁，错误必须变少、不能冒出新的；降低要求的改动
（numbers、on_violation、claims 往宽里改，关掉 cite_fields，替人把沙箱代码标成取数）一律拒绝。
"""
from __future__ import annotations

import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.engine.autofix import HANDOFF, apply_fixes, forbidden_changes, plan_fixes
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec
from app.main import app

STRICT = {"numbers": "strict", "on_violation": "fail", "claims": "require_citation"}
SCHEMA = {"type": "object", "properties": {"gmv": {"type": "number"}}}


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 10, "y": 20}, "width": 240,
            "data": {"label": label or nid, "config": config}}


def traceable(*, report=None, **contract_overrides) -> dict:
    """input → 查库 → 口径卡 → 报告撰写 → 出具：受管要求（含 G1–G5）全部满足。"""
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    contract.update(contract_overrides)
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "报告撰写", instructions="写周报", **(STRICT if report is None else report)),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"},
                                             {"name": "指标清单", "value": "{{ nodes.card.text }}"}],
             contract={k: v for k, v in contract.items() if v is not None}),
    ]
    edges = [{"id": f"e{i}", "source": a["id"], "target": b["id"]} for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 1, "y": 2, "zoom": 1}}


def find(graph: dict, nid: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == nid)


def config(graph: dict, nid: str) -> dict:
    return find(graph, nid)["data"]["config"]


def insert(graph: dict, new: dict, *, before: str, after: str) -> dict:
    graph["nodes"].insert(len(graph["nodes"]) - 1, new)
    graph["edges"] += [{"source": after, "target": new["id"]}, {"source": new["id"], "target": before}]
    return graph


def plan(graph: dict, level: str = "governed") -> dict[str, dict]:
    spec = GraphSpec.model_validate(graph)
    return {f["id"]: f for f in plan_fixes(spec, publish_issues(spec, level=level), level=level)}


def errors(graph: dict, level: str = "governed") -> list:
    return [i for i in publish_issues(GraphSpec.model_validate(graph), level=level) if i.level == "error"]


def codes(issues) -> set[str]:
    return {i["code"] if isinstance(i, dict) else i.code for i in issues}


def test_the_baseline_needs_no_fix():
    assert errors(traceable()) == [] and plan(traceable()) == {}


# --------------------------------------------------------------------------
# G3：一键写明 numbers / on_violation / claims
# --------------------------------------------------------------------------


def test_report_policy_is_filled_in_with_one_auto_fix():
    graph = traceable(report={"numbers": "off"})
    fid = "governed.report_policy:write"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["node_id"] == "write"
    assert fix["preview"]["after"] == STRICT and fix["preview"]["before"] == {
        "numbers": "off", "on_violation": None, "claims": None}
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [fid] and out["rejected"] == [] and out["ok"] is True
    assert {k: config(out["graph"], "write")[k] for k in STRICT} == STRICT
    assert config(out["graph"], "write")["instructions"] == "写周报"          # 别的配置原样留着
    assert [(c["field"], c["before"], c["after"]) for c in out["changes"]] == [
        ("numbers", "off", "strict"), ("on_violation", None, "fail"), ("claims", None, "require_citation")]
    assert out["ops"] == [{"op": "update_node", "id": "write", "config": STRICT}]
    # 坐标、视口这些前端的东西原样留着
    assert find(out["graph"], "write")["position"] == {"x": 10, "y": 20} and out["graph"]["viewport"]["zoom"] == 1


def test_only_the_missing_settings_are_written():
    graph = traceable(report={"numbers": "strict", "claims": "require_citation"})
    fix = plan(graph)["governed.report_policy:write"]
    assert fix["preview"] == {"field": "on_violation", "before": None, "after": "fail"}
    out = apply_fixes(graph, [fix["id"]], level="governed")
    assert [c["field"] for c in out["changes"]] == ["on_violation"] and out["ok"] is True


def test_published_level_warnings_get_no_fix():
    graph = traceable(report={})
    assert plan(graph, "published") == {}


# --------------------------------------------------------------------------
# validate：claims 写了不支持的值
# --------------------------------------------------------------------------


def test_an_invalid_claims_value_becomes_require_citation_under_governed():
    graph = traceable(report={**STRICT, "claims": "sometimes"})
    fid = "report.claims_invalid:write"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "claims", "before": "sometimes",
                                                        "after": "require_citation"}
    out = apply_fixes(graph, [fid], level="governed")
    assert out["ok"] is True and config(out["graph"], "write")["claims"] == "require_citation"


def test_an_invalid_claims_value_is_a_choice_under_published():
    graph = traceable(report={"claims": "sometimes"})
    fid = "report.claims_invalid:write"
    fix = plan(graph, "published")[fid]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == ["off", "require_citation"]
    assert apply_fixes(graph, [fid], level="published")["applied"] == []
    out = apply_fixes(graph, [fid], {fid: "off"}, level="published")
    assert out["applied"] == [fid] and config(out["graph"], "write")["claims"] == "off" and out["ok"] is True
    # 乱写的值没有「最接近的」，不给建议
    assert "default" not in plan(traceable(report={"claims": "strict"}), "published")[fid]


# --------------------------------------------------------------------------
# G5：给口径卡供数的 agent
# --------------------------------------------------------------------------


def feeding_agent(**config_) -> dict:
    graph = insert(traceable(), node("ask", "agent", "查数员", tools=["db_query__shop"], prompt="查 orders",
                                     assign_to="facts", **config_), before="card", after="start")
    config(graph, "card")["metrics"].append({"id": "gmv2", "expression": "vars.facts.gmv"})
    return graph


def test_cite_fields_is_switched_on_when_the_schema_exists():
    graph = feeding_agent(output_schema=SCHEMA)
    fid = "governed.caliber_agent_cite_fields:ask"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "cite_fields", "before": None, "after": True}
    out = apply_fixes(graph, [fid], level="governed")
    assert out["ok"] is True and config(out["graph"], "ask")["cite_fields"] is True
    assert config(out["graph"], "ask")["output_schema"] == SCHEMA


def test_a_missing_schema_is_not_invented():
    graph = feeding_agent()
    fid = "governed.caliber_agent_schema:ask"
    fix = plan(graph)[fid]
    assert fix["kind"] == "assist" and "「结构化输出 Schema」" in fix["label"]
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == fid
    assert "output_schema" not in config(out["graph"], "ask") and out["graph"] == graph


def test_a_model_call_feeding_a_card_goes_to_copilot():
    graph = insert(traceable(), node("guess", "llm", "估算", prompt="估一个数", assign_to="g"), before="card",
                   after="start")
    config(graph, "card")["metrics"].append({"id": "g", "expression": "vars.g"})
    assert plan(graph)["governed.caliber_model_input:guess"]["kind"] == "assist"


# --------------------------------------------------------------------------
# G4：沙箱代码喂口径卡——它到底是在取数还是在算，要人说
# --------------------------------------------------------------------------


def compute_feeding() -> dict:
    graph = insert(traceable(), node("calc", "code", "计算", code="print(1)", assign_to="calc"), before="card",
                   after="fetch")
    config(graph, "card")["metrics"].append({"id": "ratio", "expression": "vars.calc.ratio"})
    return graph


def test_compute_code_is_a_choice_not_an_auto_fix():
    graph = compute_feeding()
    fid = "governed.caliber_compute_input:calc"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["node_id"] == "calc" and fix["field"] == "evidence_role"
    assert [o["value"] for o in fix["options"]] == ["source", HANDOFF]
    assert "default" not in fix and "preview" not in fix
    # 不选就不动
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [] and "evidence_role" not in config(out["graph"], "calc")
    assert out["rejected"][0]["fix_id"] == fid


def test_choosing_source_marks_the_node():
    graph = compute_feeding()
    fid = "governed.caliber_compute_input:calc"
    out = apply_fixes(graph, [fid], {fid: "source"}, level="governed")
    assert out["applied"] == [fid] and out["ok"] is True
    assert config(out["graph"], "calc")["evidence_role"] == "source"


def test_choosing_copilot_hands_the_node_over_without_touching_it():
    graph = compute_feeding()
    fid = "governed.caliber_compute_input:calc"
    out = apply_fixes(graph, [fid], {fid: HANDOFF}, level="governed")
    assert out["applied"] == [] and out["rejected"] == [] and out["handoff"] == [fid]
    assert out["graph"] == graph and out["ok"] is False


def test_copilot_may_not_mark_a_code_node_as_source_on_its_own():
    before = compute_feeding()
    after = copy.deepcopy(before)
    config(after, "calc")["evidence_role"] = "source"
    [reason] = forbidden_changes(before, after)
    assert "「计算」" in reason and "「取数」" in reason
    # 新加的沙箱代码直接标成取数也一样
    after = copy.deepcopy(before)
    after["nodes"].append(node("raw", "code", "取原始数据", code="print(2)", evidence_role="source"))
    assert any("「取原始数据」" in r for r in forbidden_changes(before, after))


# --------------------------------------------------------------------------
# G1：契约和成果字段接到报告撰写节点
# --------------------------------------------------------------------------


def narrative() -> dict:
    return traceable(report_from=None, narrative="{{ nodes.write.text }}")


def test_report_from_takes_the_only_report_upstream():
    graph = narrative()
    fid = "governed.report_from_required:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "auto" and fix["preview"] == {"field": "contract.report_from", "before": None,
                                                        "after": "write"}
    out = apply_fixes(graph, [fid], level="governed")
    assert out["ok"] is True and config(out["graph"], "done")["contract"]["report_from"] == "write"


def test_several_reports_make_report_from_a_choice():
    graph = insert(narrative(), node("write2", "report", "第二份报告", instructions="写月报", **STRICT),
                   before="done", after="card")
    fid = "governed.report_from_required:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == ["write", "write2"]
    assert apply_fixes(graph, [fid], level="governed")["applied"] == []
    out = apply_fixes(graph, [fid], {fid: "write2"}, level="governed")
    assert out["applied"] == [fid] and config(out["graph"], "done")["contract"]["report_from"] == "write2"


def test_without_a_report_node_report_from_goes_to_copilot():
    graph = narrative()
    write = find(graph, "write")
    write.update(type="llm", data={"label": "写周报", "config": {"prompt": "写周报"}})
    # 受管级别的叙述模式过不了 G1，不再给「选 narrative」这种修了也发不出去的候选
    for fid in ("governed.report_from_required:done",):
        assert plan(graph)[fid]["kind"] == "assist"
    bare = traceable(report_from=None)
    find(bare, "write").update(type="llm", data={"label": "写周报", "config": {"prompt": "写周报"}})
    assert plan(bare)["contract.report_from_missing:done"]["kind"] == "assist"


def with_story(graph: dict) -> dict:
    graph = insert(graph, node("narrate", "llm", "叙述", prompt="写周报", assign_to="story"), before="done",
                   after="card")
    config(graph, "done")["fields"] += [{"name": "摘要", "value": "{{ vars.story }}"},
                                        {"name": "点评", "value": "点评：{{ nodes.narrate.text }}"}]
    return graph


def test_exit_fields_are_repointed_at_the_chosen_report():
    graph = with_story(traceable())
    fid = "governed.exit_text_source:done"
    fix = plan(graph)[fid]
    assert fix["kind"] == "choice" and fix["field"] == "fields"
    assert [o["value"] for o in fix["options"]] == ["write"] and fix["default"] == "write"
    assert "「摘要」" in fix["label"] and "「点评」" in fix["label"]
    # 就一个候选也不自动改：字段里放什么是作者的事
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [] and out["graph"] == graph
    out = apply_fixes(graph, [fid], {fid: "write"}, level="governed")
    assert out["applied"] == [fid] and out["ok"] is True
    fields = config(out["graph"], "done")["fields"]
    assert fields == [{"name": "周报", "value": "{{ nodes.write.text }}"},
                      {"name": "指标清单", "value": "{{ nodes.card.text }}"},
                      {"name": "摘要", "value": "{{ nodes.write.text }}"},
                      {"name": "点评", "value": "{{ nodes.write.text }}"}]
    assert [(c["field"], c["before"], c["after"]) for c in out["changes"]] == [
        ("fields[2].value", "{{ vars.story }}", "{{ nodes.write.text }}"),
        ("fields[3].value", "点评：{{ nodes.narrate.text }}", "{{ nodes.write.text }}")]
    assert out["ops"] == [{"op": "update_node", "id": "done", "config": {"fields": fields}}]


def test_an_exit_without_fields_gets_one_that_takes_the_report():
    graph = traceable()
    config(graph, "done")["fields"] = []
    graph = insert(graph, node("polish", "llm", "润色", prompt="{{ nodes.write.text }}"), before="done",
                   after="write")
    graph["edges"] = [e for e in graph["edges"] if (e["source"], e["target"]) != ("write", "done")]
    fid = "governed.exit_text_source:done"
    out = apply_fixes(graph, [fid], {fid: "write"}, level="governed")
    assert out["applied"] == [fid] and out["ok"] is True
    assert config(out["graph"], "done")["fields"] == [{"name": "result", "value": "{{ nodes.write.text }}"}]


def test_exit_fields_without_any_report_go_to_copilot():
    graph = with_story(traceable())
    find(graph, "write").update(type="llm", data={"label": "写周报", "config": {"prompt": "写周报"}})
    config(graph, "done")["contract"].pop("report_from")
    assert plan(graph)["governed.exit_text_source:done"]["kind"] == "assist"


# --------------------------------------------------------------------------
# G2：结构性的，交给 Copilot
# --------------------------------------------------------------------------


def test_text_bypass_is_an_assist():
    graph = traceable()
    graph["nodes"] += [node("chat", "llm", "闲聊", prompt="你好"),
                       node("side", "output", "附带成果", fields=[{"name": "回答", "value": "{{ nodes.chat.text }}"}])]
    graph["edges"] += [{"source": "start", "target": "chat"}, {"source": "chat", "target": "side"}]
    fid = "governed.text_bypass:chat"
    fix = plan(graph)[fid]
    assert fix["kind"] == "assist" and "报告撰写" in fix["label"]
    out = apply_fixes(graph, [fid], level="governed")
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == fid


# --------------------------------------------------------------------------
# 修完更好、不降低要求
# --------------------------------------------------------------------------


def test_all_auto_fixes_together_leave_only_the_decisions():
    graph = with_story(feeding_agent(output_schema=SCHEMA))
    graph = insert(graph, node("calc", "code", "计算", code="print(1)", assign_to="calc"), before="card",
                   after="fetch")
    config(graph, "card")["metrics"].append({"id": "ratio", "expression": "vars.calc.ratio"})
    config(graph, "write").clear()
    config(graph, "done")["contract"].update(report_from=None, narrative="{{ nodes.write.text }}")
    fixes = plan(graph)
    autos = [f["id"] for f in fixes.values() if f["kind"] == "auto"]
    assert set(autos) == {"governed.report_policy:write", "governed.caliber_agent_cite_fields:ask",
                          "governed.report_from_required:done"}
    before = len(errors(graph))
    out = apply_fixes(graph, autos, level="governed")
    assert sorted(out["applied"]) == sorted(autos) and out["rejected"] == []
    assert len([i for i in out["remaining"] if i["level"] == "error"]) < before
    left = {i["code"] for i in out["remaining"] if i["level"] == "error"}
    assert left == {"governed.exit_text_source", "governed.caliber_compute_input"}
    # 剩下的都要人拿主意，没有一条被替人应用
    assert all(fixes[f]["kind"] != "auto" for f in fixes if f.split(":")[0] in left)


@pytest.mark.parametrize("key, before, after", [
    ("numbers", "strict", "off"), ("on_violation", "fail", "flag"), ("on_violation", "fail", None),
    ("claims", "require_citation", "off"), ("claims", "require_citation", None),
])
def test_loosening_the_report_policy_is_refused(key, before, after):
    old = traceable(report={**STRICT, key: before})
    new = copy.deepcopy(old)
    if after is None:
        config(new, "write").pop(key)
    else:
        config(new, "write")[key] = after
    from app.engine.labels import field_label

    [reason] = forbidden_changes(old, new)
    assert "「报告撰写」" in reason and f"「{field_label(key)}」" in reason


def test_turning_cite_fields_off_is_refused():
    old = feeding_agent(output_schema=SCHEMA, cite_fields=True)
    new = copy.deepcopy(old)
    config(new, "ask")["cite_fields"] = False
    [reason] = forbidden_changes(old, new)
    assert "「查数员」" in reason and "「按出处核对字段」" in reason


def test_tightening_is_not_refused():
    old = traceable(report={"numbers": "off", "on_violation": "flag", "claims": "off"})
    new = copy.deepcopy(old)
    config(new, "write").update(STRICT)
    assert forbidden_changes(old, new) == []


# --------------------------------------------------------------------------
# 接口：发布前检查带上 G 规则的修复；选了「交给 Copilot」就真的交给 Copilot
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class Scripted:
    """按剧本吐操作流的 Copilot 模型；记下每次收到的请求。"""

    def __init__(self, ops: list[dict]) -> None:
        self.lines = [json.dumps(o, ensure_ascii=False) for o in ops]
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(messages)
        for line in self.lines:
            yield AIMessageChunk(content=line + "\n")


def script(monkeypatch, ops: list[dict]) -> Scripted:
    import app.api.copilot as copilot

    model = Scripted([{"op": "plan", "summary": "改图"}, *ops, {"op": "done", "explanation": "改好了"}])

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    return model


async def create(client, graph: dict) -> str:
    return (await client.post("/api/workflows", json={"name": "证据门禁-修复", "graph": graph})).json()["id"]


async def test_publish_check_lists_the_g_fixes(client):
    graph = with_story(compute_feeding())
    config(graph, "write").clear()
    wf = await create(client, graph)
    body = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()
    assert body["ok"] is False
    kinds = {f["id"]: f["kind"] for f in body["fixes"]}
    assert kinds["governed.report_policy:write"] == "auto"
    assert kinds["governed.caliber_compute_input:calc"] == "choice"
    assert kinds["governed.exit_text_source:done"] == "choice"
    assert all(not any(k.startswith("_") for k in f) for f in body["fixes"])      # 内部信息不外露
    by_code = {i["code"]: i for i in body["issues"]}
    assert by_code["governed.report_policy"]["fix"] == "governed.report_policy:write"
    assert by_code["governed.exit_text_source"]["fix"] == "governed.exit_text_source:done"
    # 已发布级别：G 规则只是警告，不给修复
    body = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published"})).json()
    assert {i["level"] for i in body["issues"] if (i["code"] or "").startswith("governed.")} == {"warning"}
    assert not [f for f in body["fixes"] if f["code"].startswith("governed.")]


async def test_choosing_copilot_in_the_choice_runs_the_assist(client, monkeypatch):
    # Copilot 把口径卡里读沙箱代码的那条指标改成直接读查询结果：计算挪进了口径卡
    card = [{"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "ratio", "expression": "cell(nodes.fetch, 0, 'gmv') / 100"}]
    model = script(monkeypatch, [{"op": "update_node", "id": "card", "config": {"metrics": card}}])
    wf = await create(client, compute_feeding())
    fid = "governed.caliber_compute_input:calc"
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "apply": [fid], "choices": {fid: HANDOFF}})).json()
    assert out["handoff"] == [fid] and len(model.calls) == 1
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "「计算」" in human and "evidence_role" in human
    assert out["assist"]["ok"] is True and out["applied"] == ["assist"] and out["ok"] is True, out
    assert "evidence_role" not in config(out["graph"], "calc")
    # 没选「交给 Copilot」、也没带 assist 的，不调模型
    model.calls.clear()
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "apply": [fid], "choices": {fid: "source"}})).json()
    assert model.calls == [] and out["assist"] is None and out["applied"] == [fid]


@pytest.mark.parametrize("ops, word", [
    ([{"op": "update_node", "id": "calc", "config": {"evidence_role": "source"}}], "「证据角色」"),
    ([{"op": "update_node", "id": "write", "config": {"numbers": "off"}},
      {"op": "update_node", "id": "calc", "config": {"evidence_role": "source"}}], "「未引用的数字」"),
])
async def test_copilot_cannot_decide_for_the_author_or_loosen_the_report(client, monkeypatch, ops, word):
    script(monkeypatch, ops)
    wf = await create(client, compute_feeding())
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "apply": [], "assist": True})).json()
    assert out["assist"]["ok"] is False and out["applied"] == []
    [refused] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "降低了要求" in refused["reason"] and word in refused["reason"]
    assert out["graph"] == (await client.get(f"/api/workflows/{wf}")).json()["graph"]
