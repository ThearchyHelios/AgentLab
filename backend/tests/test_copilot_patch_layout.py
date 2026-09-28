"""让助手改图时，用户摆好的布局不能被整张重排。

以前 generate-stream 收尾时无条件 auto_layout 整张图：只改了一句提示词，
所有节点也被挪到排版位置。工业场景的图常被人工整理成有业务含义的分区
（上游数据、口径计算、出具），再加上没有撤销，手工布局就回不来了。

这里守三件事：已有节点坐标一个不动；新节点落在空处、不压住旧节点；
新建（没有 base_graph）照旧整体排版。顺带确认 final.issues 和 reply 照常下发。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.layout import NODE_W, _height
from app.engine.schema import GraphSpec
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _node(nid: str, ntype: str, x: float, y: float, config: dict) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": x, "y": y},
            "data": {"label": nid, "config": config}}


#: 用户手工摆过的布局：故意不是排版算法会给出的样子
BASE = {
    "nodes": [
        _node("start", "input", 500, 900, {"fields": [{"name": "question"}]}),
        _node("answer", "llm", -300, 40, {"prompt": "{{ input.question }}", "assign_to": "answer"}),
        _node("out", "output", 1200, -600, {"fields": [{"name": "答案", "value": "{{ vars.answer }}"}]}),
    ],
    "edges": [{"source": "start", "target": "answer"}, {"source": "answer", "target": "out"}],
}


class _Scripted:
    def __init__(self, lines: list[dict]) -> None:
        self.lines = [json.dumps(o, ensure_ascii=False) for o in lines]

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        for line in self.lines:
            yield AIMessageChunk(content=line + "\n")


async def _events(client, monkeypatch, ops: list[dict], base: dict | None) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return _Scripted(ops), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    body = {"instruction": "改一下", "base_graph": base}
    out: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _positions(graph: dict) -> dict[str, tuple[float, float]]:
    return {n["id"]: (n["position"]["x"], n["position"]["y"]) for n in graph["nodes"]}


def _box(graph: dict, nid: str) -> tuple[float, float, float, float]:
    spec = GraphSpec.model_validate(graph)
    node = spec.node_map()[nid]
    return node.position.x, node.position.y, NODE_W, _height(node)


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


async def test_editing_a_prompt_moves_nothing(client, monkeypatch):
    ops = [
        {"op": "plan", "summary": "改短提示词"},
        {"op": "update_node", "id": "answer",
         "config": {"prompt": "简短回答：{{ input.question }}", "assign_to": "answer"}},
        {"op": "done", "explanation": "改好了", "run": False},
    ]
    final = (await _events(client, monkeypatch, ops, BASE))[-1]
    assert final["op"] == "final"
    assert _positions(final["graph"]) == _positions(BASE), "只改了一句提示词，节点却被挪了"
    assert final["layout"] == {"mode": "keep", "placed": []}


async def test_a_new_node_lands_in_free_space_and_old_ones_stay(client, monkeypatch):
    ops = [
        {"op": "plan", "summary": "加一步复核"},
        {"op": "add_node", "node": {"id": "review", "type": "llm", "label": "复核",
                                    "config": {"prompt": "复核：{{ vars.answer }}", "assign_to": "checked"}}},
        {"op": "remove_edge", "source": "answer", "target": "out"},
        {"op": "add_edge", "edge": {"source": "answer", "target": "review"}},
        {"op": "add_edge", "edge": {"source": "review", "target": "out"}},
        {"op": "done", "explanation": "加了复核", "run": False},
    ]
    final = (await _events(client, monkeypatch, ops, BASE))[-1]
    graph = final["graph"]
    before = _positions(BASE)
    after = _positions(graph)
    assert {k: after[k] for k in before} == before, "新增一个节点，旧节点跟着被挪了"

    new_box = _box(graph, "review")
    for nid in before:
        assert not _overlaps(new_box, _box(graph, nid)), f"新节点压住了「{nid}」"
    assert after["review"] != (0, 0), "新节点没排版，堆在原点"
    # 落在上游右边：从 answer 连过来，不该跑到它左边去
    assert after["review"][0] > before["answer"][0]
    assert final["layout"] == {"mode": "keep", "placed": ["review"]}


async def test_a_node_inserted_into_a_tidy_chain_does_not_cover_its_neighbour(client, monkeypatch):
    """排得整整齐齐的链上插一步：正对着上游的那一格已经被下游占了，得另找空处。"""
    tidy = {
        "nodes": [
            _node("start", "input", 80, 0, {"fields": [{"name": "question"}]}),
            _node("answer", "llm", 414, 0, {"prompt": "{{ input.question }}", "assign_to": "answer"}),
            _node("out", "output", 748, 0, {"fields": [{"name": "答案", "value": "{{ vars.answer }}"}]}),
        ],
        "edges": BASE["edges"],
    }
    ops = [
        {"op": "add_node", "node": {"id": "review", "type": "llm", "label": "复核",
                                    "config": {"prompt": "{{ vars.answer }}", "assign_to": "checked"}}},
        {"op": "remove_edge", "source": "answer", "target": "out"},
        {"op": "add_edge", "edge": {"source": "answer", "target": "review"}},
        {"op": "add_edge", "edge": {"source": "review", "target": "out"}},
        {"op": "done", "explanation": "加了复核", "run": False},
    ]
    graph = (await _events(client, monkeypatch, ops, tidy))[-1]["graph"]
    after = _positions(graph)
    assert {k: after[k] for k in _positions(tidy)} == _positions(tidy)
    for nid in ("start", "answer", "out"):
        assert not _overlaps(_box(graph, "review"), _box(graph, nid)), f"新节点压住了「{nid}」"


async def test_several_new_nodes_do_not_pile_on_each_other(client, monkeypatch):
    ops = [{"op": "plan", "summary": "并行三路"}]
    for i in range(3):
        ops.append({"op": "add_node", "node": {"id": f"side{i}", "type": "llm", "label": f"旁路{i}",
                                               "config": {"prompt": "{{ input.question }}"}}})
        ops.append({"op": "add_edge", "edge": {"source": "start", "target": f"side{i}"}})
    ops.append({"op": "done", "explanation": "并行", "run": False})
    graph = (await _events(client, monkeypatch, ops, BASE))[-1]["graph"]

    ids = [n["id"] for n in graph["nodes"]]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in _positions(BASE) and b in _positions(BASE):
                continue
            assert not _overlaps(_box(graph, a), _box(graph, b)), f"「{a}」和「{b}」叠在一起"


async def test_building_from_scratch_still_lays_out_everything(client, monkeypatch):
    ops = [
        {"op": "plan", "summary": "两步"},
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "入口",
                                    "config": {"fields": [{"name": "question"}]}}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                    "config": {"fields": [{"name": "答案", "value": "{{ input.question }}"}]}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "out"}},
        {"op": "done", "explanation": "d", "run": False},
    ]
    final = (await _events(client, monkeypatch, ops, None))[-1]
    pos = _positions(final["graph"])
    assert pos["out"][0] > pos["start"][0], "新建的图没有排版"
    assert final["layout"]["mode"] == "full"


async def test_skipped_node_types_are_reported_in_final_issues(client, monkeypatch):
    ops = [
        {"op": "plan", "summary": "x"},
        {"op": "add_node", "node": {"id": "ghost", "type": "no_such_type", "config": {}}},
        {"op": "done", "explanation": "d", "run": False},
    ]
    final = (await _events(client, monkeypatch, ops, BASE))[-1]
    skipped = [i for i in final["issues"] if i.get("code") == "unknown_node_type"]
    assert len(skipped) == 1 and skipped[0]["node_id"] is None
    assert skipped[0]["type"] == "no_such_type" and "no_such_type" in skipped[0]["message"]


async def test_a_text_reply_is_passed_through_on_the_canvas_too(client, monkeypatch):
    ops = [{"op": "reply", "text": "按 mode 字段分成两路"}]
    events = await _events(client, monkeypatch, ops, BASE)
    kinds = [e["op"] for e in events]
    assert {"op": "reply", "text": "按 mode 字段分成两路"} in events
    assert "final" not in kinds, "纯文字回复不该再附一张图"


async def test_the_non_streaming_generate_keeps_positions_too(client, monkeypatch):
    import app.api.copilot as copilot

    class _Structured:
        def with_structured_output(self, _schema):
            return self

        async def ainvoke(self, _messages):
            return {"explanation": "改了提示词", "nodes": [
                {"id": n["id"], "type": n["type"], "label": n["id"], "config": n["data"]["config"]}
                for n in BASE["nodes"]
            ], "edges": BASE["edges"]}

    async def _model(*_a, **_k):
        return _Structured(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate", json={"instruction": "改", "base_graph": BASE})
    assert r.status_code == 200, r.text
    assert _positions(r.json()["graph"]) == _positions(BASE)
