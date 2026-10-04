"""助手写人工审批的说明时不说「正式出具」。

真实运行里：助手搭了「几个 Agent 查库 → 报告 → 人工审批 → 成果」，审批节点的标题和说明写着「请审核后正式出具」
「批准后才会正式出具」。可那是一次探索运行，成果节点也没有配出具契约——根本不存在「正式出具」。审批人照着这句
话批准，以为自己在签发一份正式的东西。

出具只发生在成果节点配了出具契约、从已发布版本发起的正式运行里；画布上试运行、问数据都是探索运行。审批说明
写「批准后输出」这类说法，这次是哪一类运行由审批卡自己标明。

这组守的是：
- 节点手册里 human 那一条写明这条规矩和替代说法
- 助手写出来的审批标题、说明里有「正式出具」时，自查交回去改（给人看的那句不带键名，给模型的那句说怎么改）
- 这一轮没碰过的审批节点不替人改
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.copilot import NODE_REFERENCE, authored_issues
from app.engine.schema import GraphSpec
from app.main import app


def _human_rule() -> str:
    start = NODE_REFERENCE.index("- human：人工审批")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- validate：", start)]


def test_node_reference_tells_the_assistant_not_to_promise_formal_issuance():
    rule = _human_rule()
    assert "不要写「正式出具」" in rule
    assert "批准后输出" in rule
    # 说清什么时候才谈得上出具：配了出具契约、从已发布版本发起的正式运行
    assert "出具契约" in rule and "已发布版本" in rule and "正式运行" in rule and "探索运行" in rule


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


def _spec(*nodes):
    return GraphSpec.model_validate({"nodes": list(nodes), "edges": [
        {"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]})


def _graph(**human):
    return _spec(_node("start", "input"),
                 _node("write", "report", instructions="写周报"),
                 _node("gate", "human", mode="approve", **human),
                 _node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))


@pytest.mark.parametrize("human, field", [
    ({"title": "请审核后正式出具", "message": "请审核本周报告"}, "title"),
    ({"title": "审批", "message": "请审核本周报告，批准后才会正式出具。\n\n{{ nodes.write.text }}"}, "message"),
])
def test_promising_formal_issuance_is_sent_back(human, field):
    issues = [i for i in authored_issues(_graph(**human), []) if i["code"] == "human_formal_issuance"]
    assert len(issues) == 1, issues
    [issue] = issues
    assert issue["level"] == "error" and issue["node_id"] == "gate" and issue["field"] == field
    assert "正式出具" in issue["message"] and "批准后输出" in issue["message"]
    assert "出具契约" in issue["for_model"] and "批准后输出" in issue["for_model"]


def test_accurate_wording_passes():
    spec = _graph(title="请审核本周报告", message="批准后输出这份报告。\n\n{{ nodes.write.text }}")
    assert not [i for i in authored_issues(spec, []) if i["code"] == "human_formal_issuance"]


def test_approvals_this_round_did_not_touch_are_left_alone():
    spec = _graph(title="请审核后正式出具")
    gate = next(n for n in spec.model_dump()["nodes"] if n["id"] == "gate")
    assert not [i for i in authored_issues(spec, [], baseline=[gate]) if i["code"] == "human_formal_issuance"]


class Scripted:
    """第一轮吐 first；收到自查请求（human 里有「上线前自查」）时吐 fix。"""

    def __init__(self, first: list[dict], fix: list[dict]) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in fix]
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


def _add(nid, ntype, label, **config):
    return {"op": "add_node", "node": {"id": nid, "type": ntype, "label": label, "config": config}}


def _link(a, b, handle=None):
    return {"op": "add_edge", "edge": {"source": a, "target": b, **({"sourceHandle": handle} if handle else {})}}


async def test_the_self_check_rewrites_the_approval_before_delivery(monkeypatch):
    import app.api.copilot as copilot

    first = [
        _add("start", "input", "输入", fields=[{"name": "week"}]),
        _add("fetch", "transform", "取数", mode="expression", expression="{'gmv': 1}", assign_to="kpi"),
        _add("card", "metrics", "口径卡", metrics=[{"id": "gmv", "name": "销售额", "expression": "vars.kpi.gmv"}]),
        _add("write", "report", "写周报", instructions="写周报"),
        _add("gate", "human", "审批", mode="approve", title="请审核后正式出具",
             message="批准后才会正式出具。\n\n{{ nodes.write.text }}"),
        _add("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        _link("start", "fetch"), _link("fetch", "card"), _link("card", "write"), _link("write", "gate"),
        _link("gate", "out", "approved"),
        {"op": "done", "explanation": "周报", "run": False},
    ]
    fix = [{"op": "update_node", "id": "gate", "config": {
        "mode": "approve", "title": "请审核本周报告", "message": "批准后输出这份报告。\n\n{{ nodes.write.text }}"}},
        {"op": "done"}]
    model = Scripted(first, fix)

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "写周报并审批"}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    checks = [e for e in events if e.get("op") == "check"]
    assert checks[0]["status"] == "repairing"
    assert any(i.get("code") == "human_formal_issuance" for i in checks[0]["issues"])
    assert "出具契约" in model.calls[-1], "交回模型的修正请求里写明为什么不能这么说"
    assert checks[-1]["status"] == "passed"
    final = next(e for e in events if e.get("op") == "final")
    gate = next(n for n in final["graph"]["nodes"] if n["id"] == "gate")
    assert "正式出具" not in json.dumps(gate, ensure_ascii=False)
