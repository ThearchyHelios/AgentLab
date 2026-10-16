"""生成过程抽成可直接调用的 answer_ops（细案 01 做法 2）：SSE 路由只包一层，同一个剧本下输出逐条不变。

评测和问数据页要走同一条生成路径：评测直接迭代 answer_ops 拿 model / look / check / final / reply / error 这些操作，
问数据页照旧收 SSE。这里守：

- 几种结局（建图交付、直接回答、只问不改、自查改不好、模型出错、没给操作、输出被截断）的 SSE 输出，和抽出来之前
  逐条一样（剧本见 SCENARIOS，抽出来之前录下的输出在 fixtures/copilot_answer_ops_baseline.json），末尾只多一条 usage；
- 直接迭代 answer_ops 和 SSE 收到的是同一串；
- 开流前的拒绝（模型没配好、限定的数据源都不在）照旧是 HTTP 错误，不进流。

剧本、需求都是虚构的。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.main import app

BASELINE = Path(__file__).parent / "fixtures" / "copilot_answer_ops_baseline.json"
#: 和时间有关、每次都不一样的字段：比较前去掉
_TIMING = ("elapsed_ms", "ms", "duration_ms")


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _node(nid: str, ntype: str, config: dict) -> dict:
    return {"op": "add_node", "node": {"id": nid, "type": ntype, "label": nid, "config": config}}


def _edge(a: str, b: str, **extra) -> dict:
    return {"op": "add_edge", "edge": {"source": a, "target": b, **extra}}


def _loop_graph(condition: str) -> list[dict]:
    return [
        {"op": "plan", "summary": "数一数"},
        _node("start", "input", {"fields": [{"name": "question"}]}),
        _node("init", "transform", {"mode": "expression", "expression": "[1, 2]", "assign_to": "items"}),
        _edge("start", "init"),
        _node("lp", "loop", {"mode": "while", "condition": condition, "max_iterations": 2}),
        _edge("init", "lp"),
        _node("body", "transform", {"mode": "expression", "expression": "1"}),
        _edge("lp", "body", sourceHandle="body"),
        _edge("body", "lp"),
        _node("out", "output", {"fields": [{"name": "结果", "value": "{{ vars.items }}"}]}),
        _edge("lp", "out", sourceHandle="done"),
        {"op": "done", "explanation": "数一数", "run": True},
    ]


def _fix(condition: str) -> list[dict]:
    return [{"op": "plan", "summary": "改条件"},
            {"op": "update_node", "id": "lp", "config": {"mode": "while", "condition": condition, "max_iterations": 2}},
            {"op": "done", "explanation": "改好了"}]


def _chunks(ops: list) -> list[AIMessageChunk]:
    return [op if isinstance(op, AIMessageChunk) else AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")
            for op in ops]


#: 剧本：(第一次调用吐的, 自查修正时吐的)。None 表示这一次调用直接抛错
SCENARIOS: dict[str, tuple[list | None, list | None]] = {
    "graph_ok": (_loop_graph("len(vars.items) > 5"), []),
    "graph_fixed": (_loop_graph("{{ vars.items | length }} > 5"), _fix("len(vars.items) > 5")),
    "check_failed": (_loop_graph("foo(vars.items)"), _fix("bar(vars.items)")),
    "reply": ([{"op": "reply", "text": "上一轮已经查过了：一共 3 家门店。"}], []),
    "ask_only": ([{"op": "plan", "summary": "先问清楚"},
                  {"op": "ask", "question": "要看哪个月的？", "options": ["九月", "十月"]}], []),
    "error": (None, []),
    "no_ops": (["模型的随口注释，不是 JSON"], []),
    "truncated": ([{"op": "plan", "summary": "想了很久"},
                   AIMessageChunk(content="", response_metadata={"finish_reason": "length"})], []),
}


class _Scripted:
    """照 SCENARIOS 吐：human 里有「上线前自查」时吐修正那一份。"""

    def __init__(self, first: list | None, fix: list | None) -> None:
        self.first, self.fix = first, fix

    async def astream(self, messages):
        human = next(text for role, text in messages if role == "human")
        lines = self.fix if "上线前自查" in human else self.first
        if lines is None:
            raise RuntimeError("接入返回了 502")
        for chunk in _chunks(lines):
            yield chunk


def _use(monkeypatch, model) -> None:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    # 心跳和时间有关：拍子调大，剧本里不会有
    monkeypatch.setattr(copilot, "_HEARTBEAT_SECONDS", 3600.0)


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _TIMING}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


async def _sse(client, body: dict) -> list[dict]:
    out: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return [_clean(op) for op in out if op.get("op") != "heartbeat"]


BODY = {"instruction": "数一数门店", "intent": "answer"}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_sse_output_is_unchanged_but_for_a_trailing_usage(client, monkeypatch, name):
    _use(monkeypatch, _Scripted(*SCENARIOS[name]))
    got = await _sse(client, BODY)
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))[name]
    assert got[-1]["op"] == "usage", got[-1]
    assert got[:-1] == expected


@pytest.mark.parametrize("name", ["graph_ok", "reply", "error"])
async def test_iterating_answer_ops_directly_gives_the_same_ops(client, monkeypatch, name):
    from app.api.copilot import GenerateIn, answer_ops

    _use(monkeypatch, _Scripted(*SCENARIOS[name]))
    sse = await _sse(client, BODY)
    direct = [_clean(op) async for op in answer_ops(GenerateIn(**BODY)) if op.get("op") != "heartbeat"]
    assert direct == sse


async def test_refusals_before_the_stream_stay_http_errors(client, monkeypatch):
    import app.api.copilot as copilot
    from app.providers.factory import ProviderNotConfigured

    async def _unconfigured(*_a, **_k):
        raise ProviderNotConfigured("provider 'gw' 需要填 base_url")

    monkeypatch.setattr(copilot, "get_chat_model", _unconfigured)
    r = await client.post("/api/copilot/generate-stream", json=BODY)
    assert r.status_code == 400 and "模型接入「gw」" in r.json()["detail"]

    _use(monkeypatch, _Scripted(*SCENARIOS["reply"]))
    r = await client.post("/api/copilot/generate-stream", json={**BODY, "datasource_ids": ["no-such-source"]})
    assert r.status_code == 400 and r.json()["code"] == "datasource_scope_empty"
