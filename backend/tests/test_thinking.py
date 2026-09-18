"""思考内容的提取。

界面上"它大致在想什么"只有这一个来源。两家的形态完全不同，漏掉任何一种，
那一侧的模型在编排期就只剩一行不动的"正在理解需求"——而它其实想了 50 秒。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from app.engine.state import message_text, thinking_text


def test_anthropic_thinking_blocks() -> None:
    msg = AIMessage(content=[
        {"type": "thinking", "thinking": "先看看有哪些表。"},
        {"type": "text", "text": "查到 3 张表。"},
        {"type": "thinking", "thinking": "再按月份聚合。"},
    ])
    assert thinking_text(msg) == "先看看有哪些表。再按月份聚合。"
    # 正文不能把思考混进来
    assert thinking_text(msg) not in message_text(msg)
    assert "查到 3 张表。" in message_text(msg)


def test_openai_compatible_reasoning_content() -> None:
    """DeepSeek / Qianfan / vLLM 这类兼容端点把思考放在 reasoning_content。

    langchain 的 ChatOpenAI 明确不保留这个字段（"Non-standard response fields
    added by third-party providers are not extracted or preserved"），
    providers/factory.py 子类化捞了回来。
    """
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先看看现有的节点。"})
    assert thinking_text(chunk) == "先看看现有的节点。"


def test_merged_chunks_keep_whole_reasoning() -> None:
    """流式合并后思考要连成一整段，不能只剩最后一片。"""
    parts = ["用户想要", "一个能查销量的流程。", "先看看现有的节点。"]
    merged = AIMessageChunk(content="", additional_kwargs={"reasoning_content": parts[0]})
    for p in parts[1:]:
        merged = merged + AIMessageChunk(content="", additional_kwargs={"reasoning_content": p})
    assert thinking_text(merged) == "".join(parts)


def test_plain_message_has_no_thinking() -> None:
    """不产思考的模型不该被硬凑出一段来。"""
    assert thinking_text(AIMessage(content="就是 2")) == ""
    assert thinking_text(AIMessageChunk(content="x", additional_kwargs={})) == ""


def test_empty_reasoning_is_not_reported() -> None:
    """空串不算思考，否则界面上会出现一行什么都没有的"思考"。"""
    assert thinking_text(AIMessageChunk(content="", additional_kwargs={"reasoning_content": ""})) == ""
