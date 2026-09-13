from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


def _fake_value(schema: dict[str, Any], key: str = "") -> Any:
    """按 JSON Schema 造一个形状正确的假值，让下游校验节点也能跑通。"""
    typ = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if typ == "integer":
        return 42
    if typ == "number":
        return 3.14
    if typ == "boolean":
        return True
    if typ == "array":
        item = schema.get("items") or {"type": "string"}
        return [_fake_value(item, key)]
    if typ == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: _fake_value(v, k) for k, v in props.items()}
    return f"[mock:{key or 'value'}]"


class MockChatModel(BaseChatModel):
    """不联网的假模型。

    目的不是"能用"，而是让人在没有 API Key 的情况下就能把整条编排链路跑通：
    它会真的产出 tool_call、真的按 schema 返回结构化 JSON、真的流式吐字，
    所以画布上的高亮、工具节点、校验节点全都会被触发。
    """

    model_name: str = "mock-fast"
    latency: float = 0.012  # 每个 chunk 的间隔，模拟真实流式手感
    seed: int | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    response_format: dict[str, Any] | None = None

    @property
    def _llm_type(self) -> str:
        return "mock"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "MockChatModel":
        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if isinstance(tool, BaseTool):
                normalized.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "schema": tool.args_schema.model_json_schema()
                        if getattr(tool, "args_schema", None)
                        else {},
                    }
                )
            elif isinstance(tool, dict):
                fn = tool.get("function", tool)
                normalized.append(
                    {
                        "name": fn.get("name", "tool"),
                        "description": fn.get("description", ""),
                        "schema": fn.get("parameters") or fn.get("input_schema") or {},
                    }
                )
        return self.model_copy(update={"tools": normalized})

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        json_schema = schema
        if hasattr(schema, "model_json_schema"):
            json_schema = schema.model_json_schema()
        bound = self.model_copy(update={"response_format": json_schema})
        if hasattr(schema, "model_validate"):
            return bound | (lambda msg: schema.model_validate(json.loads(_text_of(msg))))
        return bound | (lambda msg: json.loads(_text_of(msg)))

    # ---------------- 内部 ----------------

    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        prompt = "\n".join(_text_of(m) for m in messages[-4:])
        rng = random.Random(
            self.seed
            if self.seed is not None
            else int(hashlib.sha1(prompt.encode()).hexdigest()[:8], 16)
        )

        # 要结构化输出就按 schema 造数据
        if self.response_format:
            payload = _fake_value(self.response_format)
            return AIMessage(content=json.dumps(payload, ensure_ascii=False, indent=2))

        # 绑了工具、而且这一轮还没有工具返回过 —— 那就调一次工具，
        # 让 agent 循环和画布上的工具节点真的被走到
        already_called = any(isinstance(m, ToolMessage) for m in messages)
        if self.tools and not already_called:
            tool = rng.choice(self.tools)
            args = _fake_value(tool.get("schema") or {"type": "object"})
            if not isinstance(args, dict):
                args = {}
            return AIMessage(
                content=f"我需要先调用 `{tool['name']}` 来获取信息。",
                tool_calls=[
                    {"name": tool["name"], "args": args, "id": f"call_{rng.randrange(1 << 30):08x}"}
                ],
            )

        last_user = next(
            (_text_of(m) for m in reversed(messages) if m.type == "human"), ""
        )
        topic = re.sub(r"\s+", " ", last_user).strip()[:60] or "这个问题"
        body = (
            f"（Mock 回复）关于「{topic}」，我的结论分三点：\n\n"
            f"1. 这是一个由 mock provider 生成的假响应，用来验证编排链路。\n"
            f"2. 当前节点接收到 {len(messages)} 条消息，工具 {len(self.tools)} 个。\n"
            f"3. 在设置页配一个真实 provider，把节点的模型换掉就能拿到真实结果。"
        )
        return AIMessage(content=body)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._decide(messages)
        message.usage_metadata = {
            "input_tokens": sum(len(_text_of(m)) // 4 for m in messages),
            "output_tokens": len(_text_of(message)) // 4,
            "total_tokens": 0,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(self.latency)
        return self._generate(messages, stop, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._decide(messages)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=message.content, tool_calls=message.tool_calls
                )
            )
            return
        for piece in re.findall(r".{1,6}", _text_of(message), re.DOTALL):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = self._decide(messages)
        if message.tool_calls:
            await asyncio.sleep(self.latency)
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=message.content, tool_calls=message.tool_calls
                )
            )
            return
        for piece in re.findall(r".{1,6}", _text_of(message), re.DOTALL):
            await asyncio.sleep(self.latency)
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
