"""用量计量（providers/usage.py；细案 01 做法 3）：助手建图（含挑表、接着写、自查修正）和复核的用量也记。

守的是：
- 按模型累计 tokens 和调用次数，金额按价格目录算；目录外的模型、回复没带用量（而目录价又不是 0）的调用记 unpriced_calls，
  不当成 0 元；
- metered 只计生成过程自己的调用（含它在一步里起的任务）；调用方在两次 yield 之间做的事（发起运行）不计——运行的用量
  另有 llm.end，包进来就重复了；嵌套时内层并进外层；
- answer_ops 末尾的 usage 记下这一轮建图的每一次调用（起草、自查修正各一次）；复核的结果带 usage；
- 读到 done / look / ask 那一行之后照样把模型的流读完再关（读完只为拿到流末尾才报的用量）：输出 token 报全了、金额估得出，
  不再是下限；模型在那一行之后还往下写一大段闲话的，读到上限就当场关流、照实记成没报全；
- 评测的作答（app/eval/answer.Driver）记的助手用量就是问数据页收到的那条 usage：同一个口径。

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
                                       "cost_usd": round(estimate_cost(PRICED, 2000, 400), 6), "unpriced_calls": 0,
                                       "unfinished_calls": 0}
    assert got["by_model"][UNPRICED]["unpriced_calls"] == 1 and got["by_model"][UNPRICED]["cost_usd"] == 0
    # 目录外的模型名（多了后缀）照样算估不出；目录价是 0 的本地模型没报用量也不算估不出
    assert got["by_model"][PRICED + "-silent"]["unpriced_calls"] == 1
    assert got["by_model"][FREE]["unpriced_calls"] == 0
    assert got["calls"] == 5 and got["unpriced_calls"] == 2
    assert got["cost_usd"] == pytest.approx(estimate_cost(PRICED, 2000, 400))


class Bare(Scripted):
    """不报调用参数的模型（同以前的演示模型）：invocation_params 里没有 model，只在对象上有。"""

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {}


async def test_the_model_name_comes_from_the_model_object_when_the_call_parameters_lack_it():
    """评测汇总里助手用量的模型以前记成 unknown（下一轮验收）：演示模型的调用参数里没有模型名，计量也就认不出。现在
    调用参数里没有时看 LangChain 照模型对象给回调的 ls_model_name；演示模型自己也把 model_name 报进调用参数。"""
    from app.providers.mock_model import MockChatModel

    m = usage.meter()
    with usage.metering(m):
        await Bare(model=PRICED + "-bare").ainvoke("你好")
        await MockChatModel(model_name="mock-wide", latency=0).ainvoke("你好")
    got = m.summary()["by_model"]
    assert set(got) == {PRICED + "-bare", "mock-wide"}, got
    assert usage.UNKNOWN_MODEL not in got


@pytest.mark.parametrize("build", ["openai", "anthropic", "mock"])
def test_the_call_parameters_of_each_provider_kind_carry_the_configured_model(build):
    """三种接入（OpenAI 兼容、Anthropic、演示模型）由 providers/factory 建出来的模型：LangChain 回调时给的调用参数和
    ls_model_name（这里照它的写法直接取，不发请求）计量都认得出，记的就是接入里配的模型 id。"""
    import uuid as _uuid
    from types import SimpleNamespace

    from langchain_core.outputs import LLMResult

    from app.providers.factory import ModelSpec, build_chat_model

    provider = SimpleNamespace(kind=build, name="虚构接入", api_key=None, base_url=None, extra={},
                               default_model=None)
    model_id = {"openai": "gpt-made-up", "anthropic": "claude-made-up", "mock": "mock-wide"}[build]
    import os
    env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(build)
    old = os.environ.get(env) if env else None
    if env:
        os.environ[env] = "sk-made-up"
    try:
        model = build_chat_model(provider, ModelSpec(model=model_id))
    finally:
        if env:
            if old is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = old
    m = usage.meter()
    run_id = _uuid.uuid4()
    m.on_chat_model_start({}, [], run_id=run_id, invocation_params=model._get_invocation_params(),
                          metadata=dict(model._get_ls_params()))
    m.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="好"))]]), run_id=run_id)
    assert list(m.summary()["by_model"]) == [model_id]
    # 调用参数里本身就带着（不靠 ls_model_name 兜着）
    params = model._get_invocation_params()
    assert model_id in (params.get("model"), params.get("model_name")), params


async def test_the_eval_driver_on_the_demo_model_records_its_name(monkeypatch):
    """评测作答走演示模型时，助手用量记在演示模型的名下，不是 unknown（下一轮验收：写作用量的模型对、助手的记成 unknown）。"""
    import app.api.copilot as copilot
    from app.eval import answer
    from app.providers.mock_model import MockChatModel

    async def _model(*_a, **_k):
        return MockChatModel(model_name="mock-fast", latency=0), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    driver = answer.Driver("按天看销售额", datasource_ids=[], ctx=answer.TurnContext(
        suite_id="s", suite_name="虚构题集", eval_run_id="e", case_id=None, case_seq=1, actor=None))
    ops, refused = await driver._generate("按天看销售额", None)
    assert refused is None
    driver._take(ops)
    got = driver.turn.usage["assistant"]["by_model"]
    assert set(got) == {"mock-fast"} and got["mock-fast"]["calls"] >= 1, got


class Slow(Scripted):
    """回得很慢的模型：调用方等不及（挑表超时被 asyncio.wait_for 取消）。partial=True 时先报一段输入用量再卡住。"""

    delay: float = 5.0
    partial: bool = False

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="晚了"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.partial:
            yield ChatGenerationChunk(message=AIMessageChunk(content="{", usage_metadata={
                "input_tokens": 900, "output_tokens": 0, "total_tokens": 900}))
        await asyncio.sleep(self.delay)
        yield ChatGenerationChunk(message=AIMessageChunk(content="晚了"))


class Refused(Scripted):
    """请求刚发出去就被拒了（鉴权错、参数不对）：模型没干活，不算一次调用。"""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise ValueError("401 鉴权失败")


async def test_a_call_cancelled_before_any_result_still_counts_as_an_unfinished_call():
    """挑表超时被取消的那次调用以前一点都不计（LangChain 取消时连 on_llm_error 都不一定回调）：调用次数少了一次。现在
    记一次调用、token 不知道（没有部分结果的记 0、算没报全），标「未完成」；有部分用量的照实记已报的；被拒的仍不计。"""
    from app.look import pick

    m = usage.meter()
    with usage.metering(m):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(Slow(model=PRICED).ainvoke("挑表"), timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pick._ask(Slow(model=UNPRICED), [("human", "挑表")]), timeout=0.05)

        async def read():
            async for _ in Slow(model=PRICED + "-partial", partial=True).astream("挑表"):
                pass

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(read(), timeout=0.05)
        with pytest.raises(ValueError):
            await Refused(model=PRICED + "-refused").ainvoke("挑表")
    got = m.summary()
    rows = got["by_model"]
    assert (rows[PRICED]["calls"], rows[PRICED]["unfinished_calls"], rows[PRICED]["input_tokens"]) == (1, 1, 0), rows
    assert rows[PRICED]["unpriced_calls"] == 1, "token 不知道，金额也就估不出"
    assert (rows[UNPRICED]["calls"], rows[UNPRICED]["unfinished_calls"]) == (1, 1), "挑表（look/pick._ask）超时同样计"
    partial = rows[PRICED + "-partial"]
    assert (partial["calls"], partial["unfinished_calls"], partial["input_tokens"]) == (1, 1, 900), partial
    assert PRICED + "-refused" not in rows, "被拒的不算一次调用"
    assert got["calls"] == 3 and got["unfinished_calls"] == 3


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


async def _sse(client, body: dict) -> list[dict]:
    events = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_answer_ops_ends_with_the_usage_of_every_build_call(client, monkeypatch):
    """起草、自查修正各一次。读到 done 那一行之后照样把模型的流读完（模型照约定 done 就是最后一行，读完只多收流末尾那一段
    用量）：输出 token 报全了、金额估得出，这一轮的花费不再是下限（第 1 波的遗留：以前读到 done 就关流，输出 token 一个都
    拿不到，两次都记成估不出）。"""
    import app.api.copilot as copilot

    model = Scripted(model=PRICED, replies=[GRAPH, FIX], tokens=(3000, 500))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events = await _sse(client, {"instruction": "数一数", "intent": "answer"})
    assert [e["op"] for e in events if e["op"] == "check"] and events[-2]["op"] == "final"
    spent = events[-1]
    assert spent["op"] == "usage", spent
    assert model.calls == 2, spent  # 起草一次、自查修正一次
    row = spent["by_model"][PRICED]
    assert row["calls"] == 2, spent
    assert (row["input_tokens"], row["output_tokens"]) == (6000, 1000), "流末尾才报的输出 token 也收到了"
    assert spent["unpriced_calls"] == 0, "读完了的流用量报全了，不记估不出"
    assert spent["cost_usd"] == pytest.approx(estimate_cost(PRICED, 6000, 1000), abs=1e-6)


async def test_a_look_line_also_waits_for_the_usage_at_the_end_of_the_stream(client, monkeypatch):
    """读到 look 停下查看、ask 等用户回答：同样把这一次的流读完再关，查看前那一次的输出 token 不丢。"""
    import app.api.copilot as copilot

    looked = _ops({"op": "look", "kind": "table", "id": "不存在的表", "need": "看看有哪些列"})
    model = Scripted(model=PRICED, replies=[looked, _ops({"op": "ask", "question": "要按哪一天算？"})], tokens=(1200, 80))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events = await _sse(client, {"instruction": "看一下再问", "intent": "answer"})
    spent = events[-1]
    assert spent["op"] == "usage" and model.calls == 2, [e["op"] for e in events]
    row = spent["by_model"][PRICED]
    assert (row["input_tokens"], row["output_tokens"], row["unpriced_calls"]) == (2400, 160, 0), spent


class Chatty(Scripted):
    """done 那一行之后还往下写一大段闲话（不守约定），用量照样在最后一段才报。"""

    chatter: int = 40

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        lines = self._next()
        inp, out = self.tokens
        for i, line in enumerate(lines):
            meta = {"input_tokens": inp, "output_tokens": 0, "total_tokens": inp} if i == 0 else None
            yield ChatGenerationChunk(message=AIMessageChunk(content=line + "\n", **({"usage_metadata": meta} if meta
                                                                                     else {})))
        for _ in range(self.chatter):
            yield ChatGenerationChunk(message=AIMessageChunk(content="这是一段写在最后一行之后、没人要的闲话。\n"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata={
            "input_tokens": 0, "output_tokens": out, "total_tokens": out}))


async def test_chatter_after_the_last_line_is_cut_off_and_recorded_as_not_fully_reported(client, monkeypatch):
    """读完流只为拿用量，不为闲话付钱：最后一行之后又收到超过上限的字就当场关流。这一次的用量没报全，照实记成估不出，不当成
    报全了，也不悄悄当成 0 元。"""
    import app.api.copilot as copilot
    from app.api import copilot_gen

    model = Chatty(model=PRICED, replies=[_ops({"op": "ask", "question": "要按哪一天算？"})], tokens=(500, 300))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    assert model.chatter * len("这是一段写在最后一行之后、没人要的闲话。\n") > copilot_gen.DRAIN_CHARS
    events = await _sse(client, {"instruction": "按天看销售额", "intent": "answer"})
    assert events[-1]["op"] == "usage" and model.calls == 1, [e["op"] for e in events]
    row = events[-1]["by_model"][PRICED]
    assert (row["calls"], row["input_tokens"], row["output_tokens"], row["unpriced_calls"]) == (1, 500, 0, 1), row


async def test_the_eval_driver_records_the_same_usage_the_chat_page_receives(client, monkeypatch):
    """评测的作答（app/eval/answer.Driver._generate → _take）收下的助手用量，就是问数据页经 SSE 收到的那条 usage：同一个
    生成过程（answer_ops）、同一份计量，读完流之后两边都是报全了的数。"""
    import app.api.copilot as copilot
    from app.eval import answer

    replies = [_ops({"op": "ask", "question": "要按哪一天算？"})]
    model = Scripted(model=PRICED, replies=replies, tokens=(700, 90))

    async def _model(*_a, **_k):
        return model, PRICED

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    chat = (await _sse(client, {"instruction": "按天看销售额", "intent": "answer"}))[-1]
    assert chat["op"] == "usage"
    driver = answer.Driver("按天看销售额", datasource_ids=[], ctx=answer.TurnContext(
        suite_id="s", suite_name="虚构题集", eval_run_id="e", case_id=None, case_seq=1, actor=None))
    ops, refused = await driver._generate("按天看销售额", None)
    assert refused is None and any(o.get("op") == "ask" for o in ops)
    driver._take(ops)
    got = driver.turn.usage["assistant"]
    assert got["by_model"] == chat["by_model"] and got["cost_usd"] == chat["cost_usd"], (got, chat)
    assert got["by_model"][PRICED]["output_tokens"] == 90 and got["unpriced_calls"] == 0


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
