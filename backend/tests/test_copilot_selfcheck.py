"""Copilot 搭完图先自查：和运行时同一套规则，有问题交回去改，改好再交付。

以前校验结果只是附在 final 上交给前端，error 级的问题照样交付、照样自动开跑，
跑到那一步才炸——开发库里四次运行就死在条件表达式上，前面的步骤全白跑。

这里守四件事：写错了会被交回去改、改好的图才自动运行；改不好也照实交付但不自动
运行；没问题的图不多花一次调用；只是多套了 {{ }} 的不算错，不为它返工。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _graph_ops(condition: str) -> list[str]:
    """一张 while 循环的图，条件由参数给。"""
    ops = [
        {"op": "plan", "summary": "数一数"},
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入",
                                    "config": {"fields": [{"name": "question"}]}}},
        {"op": "add_node", "node": {"id": "init", "type": "transform", "label": "准备",
                                    "config": {"mode": "expression", "expression": "[1, 2]",
                                               "assign_to": "items"}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "init"}},
        {"op": "add_node", "node": {"id": "lp", "type": "loop", "label": "循环",
                                    "config": {"mode": "while", "condition": condition,
                                               "max_iterations": 2}}},
        {"op": "add_edge", "edge": {"source": "init", "target": "lp"}},
        {"op": "add_node", "node": {"id": "body", "type": "transform", "label": "循环体",
                                    "config": {"mode": "expression", "expression": "1"}}},
        {"op": "add_edge", "edge": {"source": "lp", "target": "body", "sourceHandle": "body"}},
        {"op": "add_edge", "edge": {"source": "body", "target": "lp"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                    "config": {"fields": [{"name": "结果", "value": "{{ vars.items }}"}]}}},
        {"op": "add_edge", "edge": {"source": "lp", "target": "out", "sourceHandle": "done"}},
        {"op": "done", "explanation": "数一数", "run": True},
    ]
    return [json.dumps(o, ensure_ascii=False) for o in ops]


def _fix_ops(condition: str) -> list[str]:
    return [json.dumps(o, ensure_ascii=False) for o in (
        {"op": "plan", "summary": "改条件"},
        {"op": "update_node", "id": "lp",
         "config": {"mode": "while", "condition": condition, "max_iterations": 2}},
        {"op": "done", "explanation": "改好了"},
    )]


class _Scripted:
    """第一轮吐 first；收到自查请求（human 里有"上线前自查"）时吐 fix。"""

    def __init__(self, first: list[str], fix: list[str]) -> None:
        self.first, self.fix = first, fix
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


async def _generate(client, monkeypatch, model: _Scripted) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream",
                             json={"instruction": "数一数"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _condition(final: dict) -> str:
    return next(n for n in final["graph"]["nodes"] if n["id"] == "lp")["data"]["config"]["condition"]


async def test_a_broken_condition_is_sent_back_and_fixed_before_delivery(client, monkeypatch):
    model = _Scripted(_graph_ops("{{ vars.items | length }} > 5"), _fix_ops("len(vars.items) > 5"))
    events = await _generate(client, monkeypatch, model)

    checks = [e for e in events if e["op"] == "check"]
    assert [c["status"] for c in checks] == ["repairing", "passed"], checks
    assert any("len(x)" in i for i in checks[0]["issues"]), "交回去的问题里得带着能照改的提示"
    assert "len(x)" in model.calls[1], "自查请求没把问题原文交给模型"

    final = events[-1]
    assert final["op"] == "final"
    assert _condition(final) == "len(vars.items) > 5"
    assert final["autorun"] is True
    assert not [i for i in final["issues"] if i["level"] == "error"]


async def test_when_the_fix_does_not_take_it_is_delivered_but_not_run(client, monkeypatch):
    model = _Scripted(_graph_ops("foo(vars.items)"), _fix_ops("bar(vars.items)"))
    events = await _generate(client, monkeypatch, model)

    checks = [e for e in events if e["op"] == "check"]
    assert [c["status"] for c in checks] == ["repairing", "repairing", "failed"], checks
    assert len(model.calls) == 3, "最多交回去改两轮"
    final = events[-1]
    assert final["autorun"] is False, "明知跑不起来的图不能自动开跑"
    assert [i for i in final["issues"] if i["level"] == "error"]


async def test_a_clean_graph_costs_no_extra_call(client, monkeypatch):
    model = _Scripted(_graph_ops("len(vars.items) > 5"), [])
    events = await _generate(client, monkeypatch, model)
    assert len(model.calls) == 1
    assert [e["status"] for e in events if e["op"] == "check"] == ["passed"]
    assert events[-1]["autorun"] is True


async def test_template_braces_are_accepted_without_a_rework(client, monkeypatch):
    """多套了 {{ }} 意思没有歧义，照样能跑——提示一句就行，不为它返工一轮。"""
    model = _Scripted(_graph_ops("{{ vars.items }} != []"), [])
    events = await _generate(client, monkeypatch, model)
    assert len(model.calls) == 1
    final = events[-1]
    assert final["autorun"] is True
    assert any("是多余的" in i["message"] for i in final["issues"])
