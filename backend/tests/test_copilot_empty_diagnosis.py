"""助手一个操作都没给（copilot_empty）时看得出模型交回了什么，自动重来那一次带一句纠正；格式跑偏的操作不再被静默丢掉。

部署上撞过：问数据建图两次都是空的——模型在思考里说「先看一下表」，却一个操作都没给，自动重来一次还是这样，错误详情为空，
看不出模型实际返回了什么。现在：

- 诊断（只记长度、个数和原因，不记原文）：结束原因、正文和思考各多少字、认出几行操作、原生工具调用几个（叫什么）、跳过了
  哪些行（不是完整的 JSON、缺 op……）、正文里有没有写成文字的工具调用标记。写进 error 的 detail（给人看的一句）和
  diagnostics（机读）；
- 重来时交给助手的原因换成一句纠正（只写了思考 → 直接输出操作、要看表先写一行 look；格式跑偏 → 每个操作单独一行完整的
  JSON；用了工具调用 → 这里不走工具调用），不原样重发；
- 原来会被静默丢掉的几种写法认出来：一个操作分成多行写的 JSON、和围栏写在同一行的操作、原生工具调用里名字是操作名的
  （look、add_node……，参数就是操作的字段）。名字不认识、参数解析不了的照实记进诊断。

需求、表名都是虚构的。
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessageChunk

from app.api.copilot_gen import StreamDiagnosis, _iter_ops
from app.chat import turn as policy
from tests.test_copilot_answer_ops import _sse, _use, client  # noqa: F401


class _Model:
    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self.chunks = chunks

    async def astream(self, messages):
        for chunk in self.chunks:
            yield chunk


def _think(text: str) -> AIMessageChunk:
    return AIMessageChunk(content="", additional_kwargs={"reasoning_content": text})


async def _ops(chunks: list[AIMessageChunk]) -> tuple[list[dict], StreamDiagnosis]:
    diag = StreamDiagnosis()
    ops = [op async for op in _iter_ops(_Model(chunks), [], diag=diag)]
    return [op for op in ops if op["op"] != "thinking"], diag


async def test_only_thinking_is_diagnosed():
    ops, diag = await _ops([_think("先看一下表结构再决定。"),
                            AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})])
    assert ops == []
    assert diag.text_chars == 0 and diag.thinking_chars == len("先看一下表结构再决定。") and diag.ops == 0
    assert diag.finish == "stop"
    assert "只写了思考" in diag.correction() and "look" in diag.correction()
    summary = diag.summary()
    assert "结束原因 stop" in summary and "正文 0 字" in summary and "思考 11 字" in summary
    assert "先看一下表" not in summary and "先看一下表" not in json.dumps(diag.as_dict(), ensure_ascii=False)


async def test_skipped_lines_are_counted_with_reasons():
    # 最后一行以「{」开头、读不成：可能是分成多行写的操作，往后攒，流读完还是读不成的算跳过
    text = '这是说明文字\n[1, 2]\n{"note": "缺 op"}\n{"op": "look", "need": \n'
    ops, diag = await _ops([AIMessageChunk(content=text)])
    assert ops == []
    assert diag.dropped == {"不是完整的 JSON": 2, "不是 JSON 对象": 1, "缺 op": 1}, diag.dropped
    assert "跳过 4 行：2 行不是完整的 JSON、1 行不是 JSON 对象、1 行缺 op" in diag.summary()
    assert "每个操作单独一行" in diag.correction()


async def test_a_pretty_printed_op_is_read_not_dropped():
    text = '{"op": "plan", "summary": "先看表"}\n{\n  "op": "look",\n  "need": "产量表的字段"\n}\n'
    ops, diag = await _ops([AIMessageChunk(content=text)])
    assert [op["op"] for op in ops] == ["plan", "look"] and ops[1]["need"] == "产量表的字段"
    assert diag.dropped == {} and diag.ops == 2


async def test_an_op_on_the_fence_line_is_read():
    text = '```json {"op": "plan", "summary": "先看表"}\n{"op": "look", "need": "产量表"}```\n'
    ops, _ = await _ops([AIMessageChunk(content=text)])
    assert [op["op"] for op in ops] == ["plan", "look"]


async def test_a_native_tool_call_named_after_an_op_becomes_that_op():
    chunks = [AIMessageChunk(content="", tool_call_chunks=[
        {"name": "look", "args": '{"need": "产量', "id": "c1", "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"name": None, "args": '表的字段"}', "id": None, "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"name": "run_sql", "args": '{"sql": "SELECT 1"}', "id": "c2",
                                                      "index": 1}]),
        AIMessageChunk(content="", tool_call_chunks=[{"name": "add_node", "args": '{"node": ', "id": "c3",
                                                      "index": 2}])]
    ops, diag = await _ops(chunks)
    assert ops == [{"op": "look", "need": "产量表的字段"}]
    assert diag.tool_calls == ["look", "run_sql", "add_node"]
    assert diag.dropped == {"工具调用的名字不认识": 1, "工具调用的参数解析不了": 1}, diag.dropped


async def test_tool_call_markup_in_text_is_diagnosed():
    ops, diag = await _ops([AIMessageChunk(content='<tool_call>{"name": "look", "arguments": {}}</tool_call>\n')])
    assert ops == [] and diag.markup == 1
    assert "不走工具调用" in diag.correction()


async def test_the_empty_error_carries_the_diagnosis_and_a_correction(client, monkeypatch):  # noqa: F811
    from tests.test_copilot_answer_ops import BODY

    _use(monkeypatch, _Model([_think("先看一下表。"), AIMessageChunk(content="", response_metadata={
        "finish_reason": "stop"})]))
    got = await _sse(client, BODY)
    [error] = [op for op in got if op["op"] == "error"]
    assert error["code"] == "copilot_empty" and error["next"] == "retry"
    assert error["message"] == "助手本轮未完成：模型没有给出任何修改，也没有说明原因"
    assert "正文 0 字" in error["detail"] and "思考 6 字" in error["detail"] and "结束原因 stop" in error["detail"]
    assert error["diagnostics"]["text_chars"] == 0 and error["diagnostics"]["thinking_chars"] == 6
    assert "只写了思考" in error["reason"]
    # 重来时交给助手的话：问题后面附这句纠正，不是原样重发
    again = policy.empty_retry_instruction("上周各条线的产量", policy.error_reason(error))
    assert again.startswith("上周各条线的产量") and "只写了思考" in again and again != "上周各条线的产量"


@pytest.mark.parametrize("reason", ["", "模型没有给出任何修改，也没有说明原因"])
def test_without_a_correction_the_retry_still_asks_for_ops(reason):
    again = policy.empty_retry_instruction("产量是多少", reason)
    assert "look" in again and "产量是多少" in again
