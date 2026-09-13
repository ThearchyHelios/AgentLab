from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


def merge_dict(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """浅合并。并行分支各自写不同的 key 时不会互相覆盖。"""
    out = dict(left or {})
    out.update(right or {})
    return out


def merge_usage(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """累加 token / 成本，其余字段取新值。"""
    out = dict(left or {})
    for key, value in (right or {}).items():
        if isinstance(value, (int, float)) and isinstance(out.get(key), (int, float)):
            out[key] = out[key] + value
        else:
            out[key] = value
    return out


class GraphState(TypedDict, total=False):
    """所有节点共享的状态。

    - messages：对话历史，agent 节点之间靠它传递上下文
    - nodes：node_id -> 该节点的结构化输出，模板里用 {{ nodes.xxx.text }} 引用
    - vars：用户显式写入的变量池
    - output：output 节点收集的最终成果
    """

    messages: Annotated[list[AnyMessage], add_messages]
    input: dict[str, Any]
    vars: Annotated[dict[str, Any], merge_dict]
    nodes: Annotated[dict[str, Any], merge_dict]
    output: Annotated[dict[str, Any], merge_dict]
    usage: Annotated[dict[str, Any], merge_usage]
    trail: Annotated[list[dict[str, Any]], operator.add]
    # 循环计数器：node_id -> 已迭代次数，防止无限循环
    loops: Annotated[dict[str, Any], merge_dict]


def template_context(state: GraphState) -> dict[str, Any]:
    """把状态摊平成模板/表达式可以引用的命名空间。"""
    return {
        "input": state.get("input") or {},
        "nodes": state.get("nodes") or {},
        "vars": state.get("vars") or {},
        "output": state.get("output") or {},
        "loops": state.get("loops") or {},
        "usage": state.get("usage") or {},
        "messages": [
            {"role": getattr(m, "type", "?"), "content": _text_of(m)}
            for m in (state.get("messages") or [])
        ],
        "last_message": _text_of((state.get("messages") or [None])[-1]),
    }


def _text_of(message: Any) -> str:
    """把 LangChain 的多模态 content 压成纯文本。"""
    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "".join(parts)
    return str(content)


def message_text(message: Any) -> str:
    return _text_of(message)


def thinking_text(message: Any) -> str:
    """抽出思考块的内容。

    开了 adaptive thinking 的模型（Claude 4.6+）返回的 content 里混着 thinking 块，
    正文提取必须跳过它们，但它本身对"看清楚模型怎么想的"很有价值，所以单独取出来。
    """
    content = getattr(message, "content", message)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            parts.append(str(block.get("thinking") or ""))
    return "".join(parts)
