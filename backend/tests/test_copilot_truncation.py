"""Copilot 没说完就不能按「无需修改」交付。

2026-09 部署实例上，复杂的改图需求（并行算同比、环比，再加一个单独的 agent）连续两次得到
「已检查，画布无需修改 · 自查通过」。按同一模型、同一张底图、同一句指令重放：输出额度 8192
全部用在思考上（finish_reason=length），正文一个字都没有。读流时不看 finish_reason，也不要求
收到 done，于是自查对着原图报「通过」，final 交回原图，前端比出来没变化就说「无需修改」。

这里守几件事：
- 输出被截断（OpenAI 系的 finish_reason=length、Anthropic 的 stop_reason=max_tokens）要报错，
  不跑自查、不交 final；
- 流正常结束却一个操作都没给、也没有 done（格式跑偏，全被跳过了）同样报错；
- 模型明确说完了（有 done）但没改任何东西，照旧交付——那才是真的无需修改；
- 改图的额度要够思考之后还写得下操作；服务方一开口就嫌额度太大时退回 8192 重来。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.api import copilot as cp
from app.api.copilot import _iter_ops
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _node(nid: str, ntype: str, config: dict) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


#: 一张本来就能过自查的图：截断时自查对着它报「通过」，正是那次的样子
BASE = {
    "nodes": [
        _node("start", "input", {"fields": [{"name": "question"}]}),
        _node("answer", "llm", {"prompt": "{{ input.question }}", "assign_to": "answer"}),
        _node("out", "output", {"fields": [{"name": "答案", "value": "{{ vars.answer }}"}]}),
    ],
    "edges": [{"source": "start", "target": "answer"}, {"source": "answer", "target": "out"}],
}


def _line(op: dict) -> AIMessageChunk:
    return AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


def _thinking(text: str) -> AIMessageChunk:
    # OpenAI 兼容系（DeepSeek 经 LiteLLM）的思考走 reasoning_content
    return AIMessageChunk(content="", additional_kwargs={"reasoning_content": text})


def _openai_cut() -> AIMessageChunk:
    return AIMessageChunk(content="", response_metadata={"finish_reason": "length"})


def _anthropic_cut() -> AIMessageChunk:
    return AIMessageChunk(content=[], response_metadata={"stop_reason": "max_tokens"})


class _Scripted:
    """按剧本吐 chunk 的模型。each 是每次调用吐的那一串；调用次数多于剧本时重复最后一串。"""

    def __init__(self, *each: list[AIMessageChunk], max_tokens: int | None = None) -> None:
        self.each = each
        self.calls = 0
        self.max_tokens = max_tokens

    async def astream(self, messages):
        chunks = self.each[min(self.calls, len(self.each) - 1)]
        self.calls += 1
        for chunk in chunks:
            yield chunk


async def _events(client, monkeypatch, model, base: dict | None = BASE) -> list[dict]:
    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(cp, "get_chat_model", _model)
    out: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream",
                             json={"instruction": "并行算同比和环比", "base_graph": base}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _kinds(events: list[dict]) -> list[str]:
    return [e["op"] for e in events if e["op"] not in ("thinking", "heartbeat", "model")]


# ---------------------------------------------------------------------------
# 读流这一层
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cut", [_openai_cut, _anthropic_cut], ids=["finish_reason", "stop_reason"])
async def test_a_cut_off_stream_raises(cut):
    model = _Scripted([_thinking("先想想怎么拆成并行分支……"), _line({"op": "plan", "summary": "拆成三支"}),
                       AIMessageChunk(content='{"op":"add_node","node":{"id":"yo'), cut()])
    got: list[dict] = []
    with pytest.raises(cp.CopilotIncomplete) as info:
        async for op in _iter_ops(model, []):
            got.append(op)
    assert [o["op"] for o in got] == ["thinking", "plan"], "截断之前的操作照常转出去"
    assert "长度上限" in str(info.value)
    assert info.value.hint


async def test_done_that_lands_exactly_at_the_limit_is_complete():
    """最后一行 done 刚好写完（没来得及换行）就到了上限：模型已经说完了，不算截断。"""
    model = _Scripted([_line({"op": "plan", "summary": "x"}),
                       AIMessageChunk(content='{"op":"done","explanation":"好了"}'), _openai_cut()])
    ops = [o async for o in _iter_ops(model, [])]
    assert [o["op"] for o in ops] == ["plan", "done"]


async def test_a_normal_finish_is_not_a_cut():
    model = _Scripted([_line({"op": "plan", "summary": "x"}), _line({"op": "done", "explanation": "y"}),
                       AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})])
    ops = [o async for o in _iter_ops(model, [])]
    assert [o["op"] for o in ops] == ["plan", "done"]


# ---------------------------------------------------------------------------
# 画布改图：截断和空手而归都不能冒充「无需修改」
# ---------------------------------------------------------------------------

async def test_thinking_that_eats_the_whole_budget_is_an_error_not_unchanged(client, monkeypatch):
    """就是截图里那一轮：只有思考、没有正文，额度用完。"""
    model = _Scripted([_thinking("Let me design the modification. "), _thinking("report_ans has 4 incoming edges."),
                       _openai_cut()], max_tokens=8192)
    events = await _events(client, monkeypatch, model)

    kinds = _kinds(events)
    assert "final" not in kinds, "没说完的一轮不能把原图当成结果交回去"
    assert "check" not in kinds, "自查对着原图报「通过」，是在替一个没发生的修改作证"
    [err] = [e for e in events if e["op"] == "error"]
    assert err["message"].startswith("助手本轮未完成：") and "长度上限" in err["message"]
    assert "画布未修改" in err["hint"]
    assert "length" in err["detail"] and "8192" in err["detail"]


async def test_a_cut_in_the_middle_of_the_ops_is_not_delivered_as_done(client, monkeypatch):
    """写到一半被截断：半张图不能当成品交付，更不能自动开跑。"""
    model = _Scripted([
        _line({"op": "plan", "summary": "加一支"}),
        _line({"op": "add_node", "node": {"id": "yoy", "type": "llm", "label": "同比",
                                          "config": {"prompt": "算同比", "assign_to": "yoy"}}}),
        _anthropic_cut(),
    ])
    events = await _events(client, monkeypatch, model)
    kinds = _kinds(events)
    assert "final" not in kinds
    assert kinds[-1] == "error"


async def test_output_that_parses_to_nothing_is_an_error(client, monkeypatch):
    """格式跑偏（整段放进 markdown 围栏、JSON 分了行），每一行都被跳过：一个操作都没有，也没有 done。"""
    pretty = "```json\n" + json.dumps({"op": "add_node", "node": {"id": "yoy", "type": "llm"}}, indent=2) + "\n```\n"
    model = _Scripted([AIMessageChunk(content=pretty),
                       AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})])
    events = await _events(client, monkeypatch, model)
    kinds = _kinds(events)
    assert "final" not in kinds
    [err] = [e for e in events if e["op"] == "error"]
    assert err["message"].startswith("助手本轮未完成：") and "画布未修改" in err["hint"]


async def test_a_model_that_says_done_without_changes_is_still_unchanged(client, monkeypatch):
    """模型看过、明确收尾、一处不改：这才是真的「无需修改」，照旧交付。"""
    model = _Scripted([_line({"op": "plan", "summary": "现有的图已经满足"}),
                       _line({"op": "done", "explanation": "不需要改"})])
    events = await _events(client, monkeypatch, model)
    final = events[-1]
    assert final["op"] == "final" and final["explanation"] == "不需要改"
    assert {n["id"] for n in final["graph"]["nodes"]} == {"start", "answer", "out"}
    assert "error" not in _kinds(events)


async def test_a_cut_in_the_repair_round_is_reported(client, monkeypatch):
    """自查交回去修的那一轮被截断：要说修正没完成，不能说「自查通过」。"""
    broken = [
        _line({"op": "plan", "summary": "加个循环"}),
        _line({"op": "add_node", "node": {"id": "lp", "type": "loop", "label": "循环",
                                          "config": {"mode": "while", "condition": "foo(vars.x)",
                                                     "max_iterations": 2}}}),
        _line({"op": "add_edge", "edge": {"source": "answer", "target": "lp"}}),
        _line({"op": "add_edge", "edge": {"source": "lp", "target": "out", "sourceHandle": "done"}}),
        _line({"op": "done", "explanation": "加好了"}),
    ]
    model = _Scripted(broken, [_thinking("修一下条件"), _openai_cut()])
    events = await _events(client, monkeypatch, model)
    checks = [e for e in events if e["op"] == "check"]
    assert checks[0]["status"] == "repairing"
    assert any(c["status"] == "error" and "长度上限" in c["message"] for c in checks), checks
    assert not any(c["status"] == "passed" for c in checks)


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------

async def test_the_stream_asks_for_a_budget_that_leaves_room_after_thinking(client, monkeypatch):
    asked: list[int | None] = []

    async def _model(_session, spec):
        asked.append(spec.max_tokens)
        return _Scripted([_line({"op": "plan", "summary": "x"}), _line({"op": "done", "explanation": "y"})]), "m"

    monkeypatch.setattr(cp, "get_chat_model", _model)
    async with client.stream("POST", "/api/copilot/generate-stream",
                             json={"instruction": "改一下", "base_graph": BASE}) as r:
        [line async for line in r.aiter_lines()]
    assert asked and asked[0] >= 32768, asked


class _BudgetTooLarge(Exception):
    """仿 openai / anthropic SDK 的 400：一发请求就被拒，还没吐出任何东西。"""

    status_code = 400


class _Rejects:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    async def astream(self, messages):
        self.calls += 1
        raise _BudgetTooLarge(self.message)
        yield  # pragma: no cover - 让它成为异步生成器


@pytest.mark.parametrize("message", [
    "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]",
    "max_tokens: 32768 > 8192, which is the maximum allowed number of output tokens",
    "This model's maximum context length is 32768 tokens. However, you requested 40369 tokens "
    "(7601 in the messages, 32768 in the completion).",
])
async def test_a_rejected_budget_falls_back_to_the_old_one(client, monkeypatch, message):
    big = _Rejects(message)
    small = _Scripted([_line({"op": "plan", "summary": "x"}),
                       _line({"op": "add_node", "node": {"id": "yoy", "type": "llm", "label": "同比",
                                                         "config": {"prompt": "算同比", "assign_to": "yoy"}}}),
                       _line({"op": "add_edge", "edge": {"source": "answer", "target": "yoy"}}),
                       _line({"op": "add_edge", "edge": {"source": "yoy", "target": "out"}}),
                       _line({"op": "done", "explanation": "加好了"})])
    asked: list[int | None] = []

    async def _model(_session, spec):
        asked.append(spec.max_tokens)
        return (big if spec.max_tokens and spec.max_tokens > 8192 else small), "m"

    monkeypatch.setattr(cp, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream",
                             json={"instruction": "加一支同比", "base_graph": BASE}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert 8192 in asked
    assert big.calls == 1 and small.calls == 1
    final = events[-1]
    assert final["op"] == "final" and "yoy" in {n["id"] for n in final["graph"]["nodes"]}


async def test_other_rejections_are_not_retried(client, monkeypatch):
    """别的 400（模型名写错之类）照实报错，不拿小额度再撞一次。"""
    big = _Rejects("model 'no-such-model' does not exist")
    small = _Scripted([_line({"op": "done", "explanation": "不该到这里"})])

    async def _model(_session, spec):
        return (big if spec.max_tokens and spec.max_tokens > 8192 else small), "m"

    monkeypatch.setattr(cp, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream",
                             json={"instruction": "改一下", "base_graph": BASE}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert small.calls == 0
    assert events[-1]["op"] == "error"


def test_the_protocol_asks_not_to_draft_the_json_while_thinking():
    """思考里把整段 JSON 先写一遍，额度就去了一半：那一轮重放时思考的结尾正在逐字写口径卡的表达式。"""
    assert "思考" in cp._STREAM_PROTOCOL and "不要在思考里" in cp._STREAM_PROTOCOL
