"""Copilot 搭图自查拦得下配错的报告撰写节点（证据五期）。

- metrics_from 指向的不是口径卡：validate 本来就报 error，自查交回去改
- 报告撰写的上游没有任何证据来源（没有口径卡、查库的 agent / 调用工具、知识检索）：运行时只会给一条警告、
  报告里每个数都被判成没有出处——搭图时就打回去，按 error 交给模型改
- 配对了的报告撰写节点不多花一次自查调用
- 旧结构「可以升级为可追溯结构」的建议不混进这一轮的问题清单
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.copilot import authored_issues
from app.engine.schema import GraphSpec
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def add(nid, ntype, label, **config):
    return {"op": "add_node", "node": {"id": nid, "type": ntype, "label": label, "config": config}}


def link(a, b):
    return {"op": "add_edge", "edge": {"source": a, "target": b}}


class Scripted:
    """第一轮吐 first；收到自查请求（human 里有「上线前自查」）时吐 fix。"""

    def __init__(self, first: list[dict], fix: list[dict] | None = None) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in fix or []]
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


async def generate(client, monkeypatch, model: Scripted) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "写一份订单周报"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def wrap(*ops) -> list[dict]:
    return [{"op": "plan", "summary": "周报"}, *ops, {"op": "done", "explanation": "周报", "run": False}]


FETCH = add("fetch", "tool", "查库", tool="db_query__shop", args={"sql": "SELECT COUNT(*) AS n FROM orders"})
CARD = add("card", "metrics", "口径卡", metrics=[{"id": "n", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'n')"}])
OUT = add("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}])


def issues_of(events, status):
    return [i for e in events if e["op"] == "check" and e.get("status") == status for i in e["issues"]]


async def test_metrics_from_pointing_at_a_non_card_is_sent_back(client, monkeypatch):
    first = wrap(add("start", "input", "输入"), FETCH, link("start", "fetch"), CARD, link("fetch", "card"),
                 add("write", "report", "报告撰写", instructions="写周报", metrics_from=["fetch"]),
                 link("card", "write"), OUT, link("write", "out"))
    fix = [{"op": "update_node", "id": "write", "config": {"metrics_from": ["card"]}}, {"op": "done"}]
    model = Scripted(first, fix)
    events = await generate(client, monkeypatch, model)
    sent = issues_of(events, "repairing")
    assert any(i["code"] == "report.metrics_from_invalid" and i["node_id"] == "write" for i in sent), sent
    assert "不是口径卡" in model.calls[1]
    assert [e["status"] for e in events if e["op"] == "check"] == ["repairing", "passed"]


async def test_a_report_with_nothing_to_cite_upstream_is_sent_back(client, monkeypatch):
    first = wrap(add("start", "input", "输入"), add("write", "report", "报告撰写", instructions="写周报"),
                 link("start", "write"), OUT, link("write", "out"))
    fix = [FETCH, link("start", "fetch"), link("fetch", "write"), {"op": "done"}]
    model = Scripted(first, fix)
    events = await generate(client, monkeypatch, model)
    [sent] = [i for i in issues_of(events, "repairing") if i["code"] == "report_no_source"]
    assert sent["level"] == "error" and sent["node_id"] == "write"
    assert "没有能引用数字的证据来源" in model.calls[1]
    assert [e["status"] for e in events if e["op"] == "check"] == ["repairing", "passed"]
    final = events[-1]
    assert final["op"] == "final" and not [i for i in final["issues"] if i.get("code") == "report_no_source"]


async def test_a_well_wired_report_costs_no_extra_call(client, monkeypatch):
    first = wrap(add("start", "input", "输入"),
                 add("ask", "agent", "查数", tools=["db_query__shop"], prompt="查本周订单数"),
                 link("start", "ask"), add("write", "report", "报告撰写", instructions="写周报"),
                 link("ask", "write"), OUT, link("write", "out"))
    model = Scripted(first)
    events = await generate(client, monkeypatch, model)
    assert len(model.calls) == 1 and [e["status"] for e in events if e["op"] == "check"] == ["passed"]


async def test_the_upgrade_hint_is_not_part_of_the_copilot_issue_list(client, monkeypatch):
    # 只问清单的问数据图就是 agent → output：validate 会建议升级，但这一轮的问题清单里不该有它
    first = wrap(add("start", "input", "输入"),
                 add("ask", "agent", "查数", tools=["db_query__shop"], prompt="列出订单表里的店铺"),
                 link("start", "ask"),
                 add("out", "output", "成果", fields=[{"name": "answer", "value": "{{ nodes.ask.text }}"}]),
                 link("ask", "out"))
    events = await generate(client, monkeypatch, Scripted(first))
    final = events[-1]
    assert final["op"] == "final"
    assert not [i for i in final["issues"] if i.get("code") == "evidence.upgrade_available" or i["level"] == "info"]


def spec_of(nodes, edges):
    return GraphSpec.model_validate({"nodes": nodes, "edges": edges})


def n(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


@pytest.mark.parametrize("source", [
    n("fetch", "tool", tool="db_query__shop"),
    n("fetch", "agent", tools=["db_query__shop"]),
    n("fetch", "retrieve", collection="docs"),
    n("fetch", "metrics", metrics=[{"id": "a", "expression": "1"}]),
    n("fetch", "subgraph", workflow_id="w1"),
    n("fetch", "supervisor", goal="查清楚", agents=[{"name": "analyst", "tools": ["db_query__shop"]}]),
    # 运行输入也是能引用的来源（[[i:字段]]）：声明了数值字段的入口算
    n("fetch", "input", fields=[{"name": "target", "default": 100}]),
    n("fetch", "input", fields=[{"name": "week"}, {"name": "rate", "default": "0.35"}]),
    n("fetch", "input", fields=[{"name": "budget", "type": "number"}]),
])
def test_any_evidence_source_upstream_is_enough(source):
    spec = spec_of([n("start", "input"), source, n("write", "report"), n("out", "output")],
                   [{"source": "start", "target": "fetch"}, {"source": "fetch", "target": "write"},
                    {"source": "write", "target": "out"}])
    assert not [i for i in authored_issues(spec, []) if i["code"] == "report_no_source"]


@pytest.mark.parametrize("fields", [[], [{"name": "question", "required": True}], [{"name": "topic", "default": "本周"}]])
def test_an_input_without_numeric_fields_is_not_a_source(fields):
    # 只声明了文字字段的入口：报告只能照引问题本身，要写的数照样没有出处
    spec = spec_of([n("start", "input", fields=fields), n("write", "report"), n("out", "output")],
                   [{"source": "start", "target": "write"}, {"source": "write", "target": "out"}])
    [issue] = [i for i in authored_issues(spec, []) if i["code"] == "report_no_source"]
    assert issue["node_id"] == "write" and issue["level"] == "error"
    assert "运行输入" in issue["message"] and "[[i:" in issue["message"]


def test_a_numeric_input_upstream_is_enough_for_the_copilot_self_check():
    spec = spec_of([n("start", "input", fields=[{"name": "question"}, {"name": "gmv", "default": 1200.5}]),
                    n("write", "report"), n("out", "output")],
                   [{"source": "start", "target": "write"}, {"source": "write", "target": "out"}])
    assert not [i for i in authored_issues(spec, []) if i["code"] == "report_no_source"]


def test_a_tool_less_agent_or_a_downstream_source_does_not_count():
    spec = spec_of([n("start", "input"), n("chat", "agent", prompt="随便聊"), n("write", "report"),
                    n("late", "tool", tool="db_query__shop"), n("out", "output")],
                   [{"source": "start", "target": "chat"}, {"source": "chat", "target": "write"},
                    {"source": "write", "target": "late"}, {"source": "late", "target": "out"}])
    [issue] = [i for i in authored_issues(spec, []) if i["code"] == "report_no_source"]
    assert issue["node_id"] == "write" and issue["level"] == "error"


def test_reports_this_round_did_not_touch_are_left_alone():
    nodes = [n("start", "input"), n("write", "report", instructions="写"), n("out", "output")]
    spec = spec_of(nodes, [{"source": "start", "target": "write"}, {"source": "write", "target": "out"}])
    assert not [i for i in authored_issues(spec, [], baseline=nodes) if i["code"] == "report_no_source"]


# --------------------------------------------------------------------------
# 问数据的图发成受管：Copilot 补契约（用户报过的那一幕）
# --------------------------------------------------------------------------


def ask_report_graph() -> dict:
    """input → agent（查库）→ 报告撰写（受管三项都写了、正文引单元格）→ 出口，出口还没有契约。"""
    def g(nid, ntype, label, **config):
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}

    return {
        "nodes": [
            g("start", "input", "问题"),
            g("ask", "agent", "查数", tools=["db_query__shop"], prompt="查活跃用户数和用户总数", approval="dangerous"),
            g("report", "report", "写答案", instructions="回答：{{ input.question }}", numbers="strict",
              on_violation="fail", claims="require_citation"),
            g("out", "output", "输出答案", fields=[{"name": "answer", "value": "{{ nodes.report.text }}"}]),
        ],
        "edges": [{"id": f"e{i}", "source": a, "target": b}
                  for i, (a, b) in enumerate([("start", "ask"), ("ask", "report"), ("report", "out")])],
    }


class Assist:
    def __init__(self, ops: list[dict]) -> None:
        self.lines = [json.dumps(o, ensure_ascii=False) for o in ops]

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        for line in self.lines:
            yield AIMessageChunk(content=line + "\n")


async def governed_assist(client, monkeypatch, ops: list[dict]) -> dict:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return Assist(ops), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    wf = (await client.post("/api/workflows", json={"name": "问数据-受管", "graph": ask_report_graph()})).json()["id"]
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": [], "assist": True})
    assert r.status_code == 200, r.text
    return r.json()


CELLS_QUESTION = {"op": "question", "node_id": "out",
                  "text": "报告正文直接引用了查询单元格：契约里写 cells: true，还是改成口径卡指标？"}


async def test_the_copilots_contract_for_a_cells_only_graph_is_adopted(client, monkeypatch):
    contract = {"report_from": "report", "strict": True}
    out = await governed_assist(client, monkeypatch, [
        {"op": "plan", "summary": "补出具契约"},
        {"op": "update_node", "id": "out", "config": {"contract": contract}}, CELLS_QUESTION,
        {"op": "done", "explanation": "给出口加上契约，cells 要你拿主意"}])
    assert out["assist"]["ok"] is True and "assist" in out["applied"], out["rejected"]
    assert not [r for r in out["rejected"] if "metrics_from" in r["reason"]]
    assert next(n for n in out["graph"]["nodes"] if n["id"] == "out")["data"]["config"]["contract"] == contract
    assert out["assist"]["questions"] == ["「输出答案」：" + CELLS_QUESTION["text"]]
    # 没有契约那一处没了；cells 要人拍板（Copilot 不许替人打开），改完冒出来的这一处不算 Copilot 改坏的，
    # 发布弹窗里给一键选项：写 cells: true，或交给 Copilot 改成口径卡
    assert out["ok"] is False
    assert [i["code"] for i in out["remaining"] if i["level"] == "error"] == ["contract.cells_undeclared"]
    [fix] = [f for f in out["fixes"] if f["code"] == "contract.cells_undeclared"]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == [True, "copilot"]


async def test_the_copilot_still_may_not_switch_cells_on_by_itself(client, monkeypatch):
    out = await governed_assist(client, monkeypatch, [
        {"op": "plan", "summary": "补出具契约"},
        {"op": "update_node", "id": "out", "config": {"contract": {"report_from": "report", "strict": True,
                                                                   "cells": True}}},
        {"op": "done", "explanation": "补上契约并打开 cells"}])
    [rejected] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "单元格引用" in rejected["reason"] and out["ok"] is False
