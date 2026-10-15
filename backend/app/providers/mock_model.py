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


#: 助手按操作流写图时 system 里一定有这一句（api/copilot.py 的 _STREAM_PROTOCOL / APP_PROTOCOL_PROMPT）
_OPS_MARK = "输出格式（严格遵守）"
#: 看起来是在问数据的请求：演示模型据此决定要不要先 look 一次。闲聊、改提示词不看
_DATA_WORDS = re.compile(r"查|多少|几个|统计|数据|表|库|sql|金额|数量|排名|趋势|汇总|明细|报表|周报|月报|指标|销售|"
                         r"订单|人数|人次|收入|占比|同比|环比|top", re.I)
_QUERY_TOOL = re.compile(r"- db_query__(\S+?)（数据库）")


def _question_of(human: str) -> str:
    """助手请求里用户的原话：问数据是「问题：…」那一行，画布新建是「请设计一个工作流：…」，改图是最后一段。"""
    for mark in ("问题：", "请设计一个工作流："):
        if mark in human:
            return human.rsplit(mark, 1)[1].split("\n", 1)[0].strip()
    return human.strip().rsplit("\n", 1)[-1].strip()


def _ops(*ops: dict[str, Any]) -> str:
    return "\n".join(json.dumps(op, ensure_ascii=False) for op in ops) + "\n"


def _copilot_script(messages: list[BaseMessage]) -> str | None:
    """助手（画布、问数据、应用润色）的演示脚本：没配模型接入时也能看到「查看 → 接着写」的完整过程。

    - 数据类请求、有数据源：先 plan，再 look 一次（只写 kind 和数据源，按需求挑表），停下；
    - 收到查看结果（对话里已经有一条自己的输出）：接着写一张「输入 → 查库的 Agent → 成果」的图；
    - 闲聊：一行 reply，不看；
    - 自查修正、应用润色：只说明，不改。
    """
    system = next((_text_of(m) for m in messages if m.type == "system"), "")
    if _OPS_MARK not in system:
        return None
    humans = [_text_of(m) for m in messages if m.type == "human"]
    first, last = (humans[0], humans[-1]) if humans else ("", "")
    if "app_add" in system:
        return _ops({"op": "plan", "summary": "演示模型不改应用"},
                    {"op": "done", "explanation": "（演示模型）没有修改应用。如需真实的润色，请在「设置 → 模型接入」中添加模型接入"})
    if "上线前自查" in last:
        return _ops({"op": "done", "explanation": "演示模型不做修正"})
    question = _question_of(first)
    sources = _QUERY_TOOL.findall(system)
    if any(m.type == "ai" for m in messages):
        tool = f"db_query__{sources[0]}" if sources else None
        answering = "用户问了下面这个问题" in first
        return _ops(
            {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入",
                                        "config": {} if answering else {"fields": [{"name": "question"}]}}},
            {"op": "add_node", "node": {"id": "ask", "type": "agent", "label": "查库回答", "config": {
                "prompt": "{{ input.question }}", "tools": [tool] if tool else [], "assign_to": "answer"}}},
            {"op": "add_edge", "edge": {"source": "start", "target": "ask"}},
            {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {
                "fields": [{"name": "answer", "value": "{{ vars.answer }}"}]}}},
            {"op": "add_edge", "edge": {"source": "ask", "target": "out"}},
            {"op": "done", "explanation": "（演示模型）按查看结果搭了一张查库回答的图", "run": True},
        )
    if sources and '"op":"look"' in system and _DATA_WORDS.search(question):
        return _ops({"op": "plan", "summary": "先看一下相关的表，再搭查库回答的图"},
                    {"op": "look", "need": question[:80] or "这个问题要用到的表",
                     "targets": [{"kind": "table", "source": sources[0]}]})
    topic = re.sub(r"\s+", " ", question).strip()[:60] or "这个问题"
    return _ops({"op": "reply", "text": f"（演示模型的回复）收到「{topic}」。如需真实的回答，请在「设置 → 模型接入」中添加模型接入"})


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

        # 助手按操作流写图：照脚本走一遍（数据类请求先 look 一次）
        if not self.tools and (script := _copilot_script(messages)) is not None:
            return AIMessage(content=script)

        # 绑了工具、而且这一轮还没有工具返回过 —— 那就调一次工具，
        # 让 agent 循环和画布上的工具节点真的被走到
        already_called = any(isinstance(m, ToolMessage) for m in messages)
        if self.tools and not already_called:
            tool = rng.choice(self.tools)
            args = _fake_value(tool.get("schema") or {"type": "object"})
            if not isinstance(args, dict):
                args = {}
            return AIMessage(
                content=f"需要先调用 `{tool['name']}` 获取信息。",
                tool_calls=[
                    {"name": tool["name"], "args": args, "id": f"call_{rng.randrange(1 << 30):08x}"}
                ],
            )

        last_user = next(
            (_text_of(m) for m in reversed(messages) if m.type == "human"), ""
        )
        topic = re.sub(r"\s+", " ", last_user).strip()[:60] or "这个问题"
        body = (
            f"（演示模型的回复）关于「{topic}」，示例内容分三点：\n\n"
            f"1. 这是演示模型生成的示例内容，用于验证工作流能否正常运行。\n"
            f"2. 当前节点收到 {len(messages)} 条消息、{len(self.tools)} 个工具。\n"
            f"3. 如需真实结果，请在「设置 → 模型接入」中添加模型接入，并为节点换用对应模型。"
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
