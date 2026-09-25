"""Copilot 流式生成：等待期间必须有反馈。

这组测试守的是一个体验契约，不是实现细节：从请求发出到第一个节点落到画布上，
本机实测有 31.7 秒（DeepSeek V4 pro，中等复杂度的需求）。这段时间里前端如果
一个事件都收不到，用户没法判断是在想还是已经卡死——所以 thinking 和 heartbeat
这两路反馈只要断了，就是回归。
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import copilot as cp
from app.api.copilot import _iter_ops, _with_heartbeat


class Chunk:
    """模拟流式 chunk。content 是块列表时对应 Claude 4.6+ 的 thinking 混排。"""

    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, chunks, delay: float = 0.0):
        self._chunks = chunks
        self._delay = delay

    async def astream(self, messages):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk


async def _collect(gen):
    return [op async for op in gen]


@pytest.mark.asyncio
async def test_thinking_blocks_become_events():
    """支持 thinking 的模型，思考要原样转给前端。"""
    model = FakeModel([
        Chunk([{"type": "thinking", "thinking": "用户要一个调研流程。"}]),
        Chunk([{"type": "thinking", "thinking": "先搜索，再抓取。"}]),
        Chunk([{"type": "text", "text": '{"op":"plan","summary":"三步"}\n'}]),
        Chunk([{"type": "text", "text": '{"op":"done","explanation":"完成"}\n'}]),
    ])
    ops = await _collect(_iter_ops(model, []))
    kinds = [o["op"] for o in ops]

    assert kinds.count("thinking") == 2
    assert ops[0]["delta"] == "用户要一个调研流程。"
    assert kinds[-1] == "done", "done 之后就该停，后面的闲话不要"


@pytest.mark.asyncio
async def test_plain_text_is_not_passed_off_as_thinking():
    """不支持 thinking 的模型不该被凭空造出思考。

    有一种取巧写法是"把非 JSON 行当成思考"，那会把模型的 markdown 注释、
    协议跑偏时的乱输出一并当作思考展示——宁可只有心跳，也不要假的思考。
    """
    model = FakeModel([
        Chunk("模型的随口注释，不是 JSON\n"),
        Chunk('{"op":"plan","summary":"x"}\n'),
        Chunk('{"op":"done","explanation":"y"}\n'),
    ])
    kinds = [o["op"] for o in await _collect(_iter_ops(model, []))]

    assert "thinking" not in kinds
    assert kinds == ["plan", "done"]


@pytest.mark.asyncio
async def test_heartbeat_fires_while_model_is_silent(monkeypatch):
    """模型长时间不吐字时要补心跳，否则前端一片死寂。

    心跳必须在 _with_heartbeat 这一层做：模型不产出 chunk 时，_iter_ops 里那个
    async for 的循环体根本不执行，在里面判断时间差是轮不到的。
    """
    monkeypatch.setattr(cp, "_HEARTBEAT_SECONDS", 0.2)

    class SlowModel:
        async def astream(self, messages):
            await asyncio.sleep(0.9)  # 模拟"正在想"
            yield Chunk('{"op":"plan","summary":"终于"}\n')
            yield Chunk('{"op":"done","explanation":"z"}\n')

    ops = await _collect(_with_heartbeat(_iter_ops(SlowModel(), [])))
    beats = [o for o in ops if o["op"] == "heartbeat"]

    assert len(beats) >= 2, "0.9 秒的沉默至少该有两拍"
    assert beats[0]["phase"] == "planning"
    assert beats[0]["elapsed_ms"] > 0
    assert [o["op"] for o in ops][-1] == "done"


@pytest.mark.asyncio
async def test_heartbeat_phase_follows_progress(monkeypatch):
    """阶段要跟着实际操作走——阶段会变本身就是"它还活着"的信号。"""
    monkeypatch.setattr(cp, "_HEARTBEAT_SECONDS", 0.2)

    class Staged:
        async def astream(self, messages):
            yield Chunk('{"op":"add_node","node":{"id":"a","type":"input"}}\n')
            await asyncio.sleep(0.5)  # 放完节点后卡一会儿
            yield Chunk('{"op":"done","explanation":"d"}\n')

    ops = await _collect(_with_heartbeat(_iter_ops(Staged(), [])))
    beats = [o for o in ops if o["op"] == "heartbeat"]

    assert beats, "add_node 之后的停顿也该有心跳"
    assert beats[0]["phase"] == "building", "放过节点了，阶段不该还停在 planning"


def test_unknown_node_types_never_reach_the_canvas():
    """模型编一个不存在的节点类型：不进图、不转给前端。

    前端按类型查节点定义，查不到以前是整站白屏、连带没保存的编辑一起丢。
    """
    nodes: dict = {}
    edges: list = []
    ghost = {"op": "add_node", "node": {"id": "x", "type": "no_such_type", "config": {}}}
    real = {"op": "add_node", "node": {"id": "a", "type": "llm", "config": {}}}
    assert cp._apply_op(nodes, edges, ghost) is False    # 生成流只转发 changed 的操作
    assert cp._apply_op(nodes, edges, real) is True
    assert set(nodes) == {"a"}
