"""用量计量（providers/usage.py；细案 01 做法 3）：助手建图（含挑表、接着写、自查修正）和复核的用量也记。

守的是：
- 按模型累计 tokens 和调用次数，金额按价格目录算；目录外的模型、回复没带用量（而目录价又不是 0）的调用记 unpriced_calls，
  不当成 0 元；
- metered 只计生成过程自己的调用（含它在一步里起的任务）；调用方在两次 yield 之间做的事（发起运行）不计——运行的用量
  另有 llm.end，包进来就重复了；嵌套时内层并进外层；
- answer_ops 末尾的 usage 记下这一轮建图的每一次调用（起草、自查修正各一次）；复核的结果带 usage。

模型都是剧本，不调真接口；价格用目录里登记过的模型 id，名字之外都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.main import app
from app.providers import usage
from app.providers.catalog import estimate_cost

PRICED = "claude-opus-5"        # 价格目录里有
UNPRICED = "made-up-model-x"    # 价格目录里没有
FREE = "mock-fast"              # 目录价是 0


class Scripted(BaseChatModel):
    """真的 LangChain 模型：回调照常触发。每次调用吐 replies 里的下一份（用完重复最后一份），用量按 tokens 报；
    report=False 时回复不带用量（有的网关流式不给）。"""

    model: str = PRICED
    replies: list[list[str]] = [["好的"]]
    tokens: tuple[int, int] = (1000, 200)
    report: bool = True
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    def _next(self) -> list[str]:
        lines = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return lines

    def _usage(self) -> dict[str, int] | None:
        if not self.report:
            return None
        inp, out = self.tokens
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(content="\n".join(self._next()))
        if (meta := self._usage()) is not None:
            message.usage_metadata = meta
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        """像 Anthropic 那样报用量：第一段带输入 token，流末尾单独一段带输出 token。"""
        lines = self._next()
        inp, out = self.tokens
        for i, line in enumerate(lines):
            meta = {"input_tokens": inp, "output_tokens": 0, "total_tokens": inp} if self.report and i == 0 else None
            yield ChatGenerationChunk(message=AIMessageChunk(content=line + "\n", **({"usage_metadata": meta} if meta
                                                                                     else {})))
        if self.report:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata={
                "input_tokens": 0, "output_tokens": out, "total_tokens": out}))


# --------------------------------------------------------------------------
# 计量本身
# --------------------------------------------------------------------------


async def test_metering_counts_by_model_and_prices_from_the_catalog():
    m = usage.meter()
    with usage.metering(m):
        await Scripted(model=PRICED).ainvoke("你好")
        await Scripted(model=PRICED).ainvoke("再来")
        await Scripted(model=UNPRICED).ainvoke("你好")
        await Scripted(model=PRICED + "-silent", report=False).ainvoke("你好")
        await Scripted(model=FREE, report=False).ainvoke("你好")
    got = m.summary()
    assert got["by_model"][PRICED] == {"input_tokens": 2000, "output_tokens": 400, "calls": 2,
                                       "cost_usd": round(estimate_cost(PRICED, 2000, 400), 6), "unpriced_calls": 0}
    assert got["by_model"][UNPRICED]["unpriced_calls"] == 1 and got["by_model"][UNPRICED]["cost_usd"] == 0
    # 目录外的模型名（多了后缀）照样算估不出；目录价是 0 的本地模型没报用量也不算估不出
    assert got["by_model"][PRICED + "-silent"]["unpriced_calls"] == 1
    assert got["by_model"][FREE]["unpriced_calls"] == 0
    assert got["calls"] == 5 and got["unpriced_calls"] == 2
    assert got["cost_usd"] == pytest.approx(estimate_cost(PRICED, 2000, 400))


async def test_outside_the_meter_nothing_is_counted():
    m = usage.meter()
    with usage.metering(m):
        pass
    await Scripted().ainvoke("你好")
    assert m.summary()["calls"] == 0
    assert usage.current() is None


async def test_metered_counts_the_generator_and_its_tasks_but_not_the_caller():
    """生成过程里的调用（含它起的任务）计进来；调用方在两次 yield 之间发起的「运行」不计。"""
    m = usage.meter()
    leaked: list[asyncio.Task] = []

    async def generation():
        await Scripted(model=PRICED).ainvoke("起草")
        # 心跳那一层每一步起一个任务：任务在步里创建，拷走的上下文带着计量
        await asyncio.ensure_future(Scripted(model=PRICED).ainvoke("挑表"))
        yield "first"
        await Scripted(model=PRICED).ainvoke("自查修正")
        yield "second"

    seen = []
    async for item in usage.metered(generation(), m):
        seen.append(item)
        # 调用方拿到一条操作后自己调模型（相当于发起运行）：它的上下文里没有计量
        await Scripted(model=UNPRICED).ainvoke("运行里的调用")
        leaked.append(asyncio.ensure_future(Scripted(model=UNPRICED).ainvoke("运行任务")))
    await asyncio.gather(*leaked)
    got = m.summary()
    assert seen == ["first", "second"]
    assert got["by_model"] == {PRICED: got["by_model"][PRICED]} and got["calls"] == 3
    assert usage.current() is None


async def test_nested_meters_fold_into_the_outer_one():
    outer, inner = usage.meter(), usage.meter()

    async def generation():
        await Scripted(model=PRICED).ainvoke("起草")
        yield 1

    with usage.metering(outer):
        await Scripted(model=PRICED).ainvoke("外层自己的")
        async for _ in usage.metered(generation(), inner):
            pass
        with usage.metering(usage.meter()):
            await Scripted(model=PRICED).ainvoke("复核")
    assert inner.summary()["calls"] == 1
    assert outer.summary()["calls"] == 3


# --------------------------------------------------------------------------
# answer_ops 末尾的 usage、复核的 usage
# --------------------------------------------------------------------------


def _ops(*ops: dict) -> list[str]:
    return [json.dumps(o, ensure_ascii=False) for o in ops]


GRAPH = _ops(
    {"op": "plan", "summary": "数一数"},
    {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入",
                                "config": {"fields": [{"name": "question"}]}}},
    {"op": "add_node", "node": {"id": "lp", "type": "loop", "label": "循环",
                                "config": {"mode": "while", "condition": "foo(vars.x)", "max_iterations": 2}}},
    {"op": "add_edge", "edge": {"source": "start", "target": "lp"}},
    {"op": "add_node", "node": {"id": "body", "type": "transform", "label": "体",
                                "config": {"mode": "expression", "expression": "1"}}},
    {"op": "add_edge", "edge": {"source": "lp", "target": "body", "sourceHandle": "body"}},
    {"op": "add_edge", "edge": {"source": "body", "target": "lp"}},
    {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                "config": {"fields": [{"name": "r", "value": "1"}]}}},
    {"op": "add_edge", "edge": {"source": "lp", "target": "out", "sourceHandle": "done"}},
    {"op": "done", "explanation": "数一数", "run": True},
)
FIX = _ops({"op": "update_node", "id": "lp", "config": {"mode": "while", "condition": "len(vars.x) > 1",
                                                        "max_iterations": 2}},
           {"op": "done", "explanation": "改好了"})


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_answer_ops_ends_with_the_usage_of_every_build_call(client, monkeypatch):
    """起草、自查修正各一次。生成过程读到 done 就关流（后面的闲话不要），流末尾才报的输出 token 拿不到：这两次照样计次数、
    计已报的输入 token，记成估不出（unpriced_calls），金额只是估得出的那部分——不悄悄少算。"""
    import app.api.copilot as copilot

    model = Scripted(model=PRICED, replies=[GRAPH, FIX], tokens=(3000, 500))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events = []
    async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "数一数", "intent": "answer"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert [e["op"] for e in events if e["op"] == "check"] and events[-2]["op"] == "final"
    spent = events[-1]
    assert spent["op"] == "usage", spent
    assert model.calls == 2, spent  # 起草一次、自查修正一次
    assert spent["by_model"][PRICED]["calls"] == 2, spent
    assert spent["by_model"][PRICED]["input_tokens"] == 6000 and spent["by_model"][PRICED]["output_tokens"] == 0
    assert spent["cost_usd"] == pytest.approx(estimate_cost(PRICED, 6000, 0), abs=1e-6)
    assert spent["unpriced_calls"] == 2, "关流前没报全的两次记成估不出"


async def test_a_build_on_an_unpriced_model_says_it_cannot_be_priced(client, monkeypatch):
    import app.api.copilot as copilot

    model = Scripted(model=UNPRICED, replies=[_ops({"op": "reply", "text": "已经查过了"})])

    async def _model(*_a, **_k):
        return model, UNPRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events = []
    async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "刚才那个数是多少"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert [e["op"] for e in events] == ["model", "reply", "usage"]
    assert events[-1]["calls"] == 1 and events[-1]["unpriced_calls"] == 1 and events[-1]["cost_usd"] == 0


async def test_a_stream_read_to_the_end_is_fully_priced():
    """读完的流（直接回答那一行之后模型自己停了）：输入、输出 token 都报全了，金额估得出。"""
    m = usage.meter()

    async def generation():
        async for _ in Scripted(model=PRICED, tokens=(400, 60)).astream("问"):
            pass
        yield 1

    async for _ in usage.metered(generation(), m):
        pass
    row = m.summary()["by_model"][PRICED]
    assert (row["input_tokens"], row["output_tokens"], row["unpriced_calls"]) == (400, 60, 0)
    assert row["cost_usd"] == round(estimate_cost(PRICED, 400, 60), 6)


async def test_the_review_carries_its_own_usage(client, monkeypatch):
    import app.api.copilot as copilot
    from app.core.events import EventType
    from app.db.base import SessionLocal
    from app.db.models import Run, RunEvent

    reply = json.dumps({"note": "agent 用满了步数，结论不完整。", "answer": "", "retry": True}, ensure_ascii=False)
    model = Scripted(model=PRICED, replies=[[reply]], tokens=(800, 100))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    async with SessionLocal() as session:
        run = Run(status="succeeded", output={"answer": "大概是 3 家"}, graph={})
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type=EventType.LOG, node_id="a", ts=0.0,
                             data={"level": "warn", "code": "step_limit", "message": "用满了步数"}))
        await session.commit()
        run_id = run.id
    body = (await client.post("/api/copilot/review", json={"run_id": run_id, "question": "有几家门店"})).json()
    assert body["usage"]["calls"] == 1 and body["usage"]["by_model"][PRICED]["input_tokens"] == 800
    # 干净的运行不调模型：usage 全 0
    async with SessionLocal() as session:
        clean = Run(status="succeeded", output={"answer": "3 家"}, graph={})
        session.add(clean)
        await session.commit()
        clean_id = clean.id
    body = (await client.post("/api/copilot/review", json={"run_id": clean_id, "question": "有几家门店"})).json()
    assert body["verdict"] == "ok" and body["usage"]["calls"] == 0 and body["usage"]["by_model"] == {}
