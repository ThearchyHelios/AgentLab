"""OpenAI 兼容接入的流式用量回传（stream_options.include_usage）。

真实运行里：模型走 LiteLLM 的 OpenAI 兼容接口，上百次模型调用，运行用量却是 0 token、0 美元。ChatOpenAI
只在没填 base_url 时默认开流式用量回传；填了 base_url（所有兼容接口都填）就不开，流里没有 usage 那一块，
llm.end 记下的全是 0，运行的用量是 llm.end 逐次加总的，于是也是 0。

这组守的是：
- 兼容接入构造出来的模型开着流式用量回传，请求里带 stream_options.include_usage
- 流里最后那块 usage 合进响应，llm.end 和运行的用量里 token、金额都对
- 不认 stream_options 的服务：同一次调用去掉它重发一次，照常流式输出，不让调用失败；同一个接口之后不再带它
- 和用量回传无关的报错不重发、原样抛出

假的是 HTTP：openai SDK 发出去的请求在它的 HTTP 库的传输层被接住，按请求体回 SSE。模型名、内容都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import Provider, Run, RunEvent
from app.engine.runner import run_manager
from app.providers import catalog, factory
from app.providers.factory import ModelSpec, build_chat_model

try:  # openai SDK 3 起换成了 httpx2（和 httpx 同一套接口）：假服务要装在 SDK 真正用的那个库上
    from langchain_openai._compat import httpx
except ImportError:  # pragma: no cover - 老版本的 langchain_openai
    import httpx

PRICED = "gpt-5.2-mini"
USAGE = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}


def _chunk(delta: dict | None = None, *, finish: str | None = None, usage: dict | None = None) -> str:
    body = {"id": "c-1", "object": "chat.completion.chunk", "created": 1, "model": PRICED,
            "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


class FakeServer:
    """OpenAI 兼容服务：带了 stream_options.include_usage 才在流尾补 usage 那一块。

    rejects：不认 stream_options 的服务，带了就回 status（400 / 422）和 message；
    fail：每个请求都回这个错误（和用量回传无关的报错）。"""

    def __init__(self, *, rejects: tuple[int, str] | None = None, fail: tuple[int, str] | None = None) -> None:
        self.rejects, self.fail = rejects, fail
        self.bodies: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        wants = bool((body.get("stream_options") or {}).get("include_usage"))
        if self.fail or (self.rejects and "stream_options" in body):
            status, message = self.fail or self.rejects
            return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error"}},
                                  request=request)
        if not body.get("stream"):
            reply = {"id": "c-1", "object": "chat.completion", "created": 1, "model": PRICED,
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": "上周到店 120 人。"},
                                  "finish_reason": "stop"}], "usage": USAGE}
            return httpx.Response(200, json=reply, request=request)
        sse = (_chunk({"role": "assistant", "content": "上周"}) + _chunk({"content": "到店 120 人。"})
               + _chunk({}, finish="stop") + (_chunk(usage=USAGE) if wants else "") + "data: [DONE]\n\n")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode(),
                              request=request)


@pytest.fixture
def server(monkeypatch):
    def install(**kwargs) -> FakeServer:
        fake = FakeServer(**kwargs)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake.handle)
        return fake
    # 记住「这个接口不认 stream_options」的表是进程级的：每个用例从空表开始
    monkeypatch.setattr(factory, "_NO_STREAM_USAGE", set(), raising=False)
    return install


def _provider(base_url: str | None = None, *, kind: str = "openai_compatible") -> Provider:
    return Provider(name=f"compat-{uuid.uuid4().hex[:8]}", kind=kind,
                    base_url=base_url or f"http://llm-{uuid.uuid4().hex[:6]}.example.test/v1",
                    api_key=None, models=[{"id": PRICED}], default_model=PRICED, enabled=True, extra={})


async def _stream(model) -> tuple[str, dict | None, int]:
    """流式调用一次：(正文, 合并后的 usage_metadata, 块数)。"""
    merged, count = None, 0
    async for chunk in model.astream([HumanMessage(content="上周到店多少人")]):
        merged = chunk if merged is None else merged + chunk
        count += 1
    return str(merged.content), merged.usage_metadata, count


# ---------------------------------------------------------------- 构造


@pytest.mark.parametrize("kind", ["openai_compatible", "openai"])
def test_compatible_providers_turn_on_streaming_usage(kind):
    model = build_chat_model(_provider(kind=kind), ModelSpec())
    assert model.stream_usage is True


# ---------------------------------------------------------------- 流式调用


async def test_usage_chunk_is_requested_and_merged(server):
    fake = server()
    model = build_chat_model(_provider(), ModelSpec())
    text, usage, _ = await _stream(model)
    assert text == "上周到店 120 人。"
    assert fake.bodies[0]["stream_options"] == {"include_usage": True}
    assert usage and usage["input_tokens"] == 1200 and usage["output_tokens"] == 300


@pytest.mark.parametrize("status,message", [
    (400, "Unrecognized request argument supplied: stream_options"),
    (422, "body -> stream_options: extra fields not permitted"),
    (400, "Bad Request"),
], ids=["named-400", "named-422", "generic-400"])
async def test_service_rejecting_stream_options_falls_back_and_is_remembered(server, status, message):
    fake = server(rejects=(status, message))
    provider = _provider()
    model = build_chat_model(provider, ModelSpec())
    text, usage, count = await _stream(model)
    # 照常流式输出：去掉 stream_options 重发了一次，正文分块到达
    assert text == "上周到店 120 人。" and count >= 2
    assert ["stream_options" in b for b in fake.bodies] == [True, False]
    assert usage is None or not usage.get("input_tokens"), "这个服务给不了流式用量，不编"

    # 同一个接口之后不再带 stream_options（新构造的模型也一样），不再每次先失败一次
    again = build_chat_model(provider, ModelSpec())
    await _stream(again)
    assert ["stream_options" in b for b in fake.bodies] == [True, False, False]


async def test_other_errors_are_not_retried(server):
    fake = server(fail=(401, "invalid api key"))
    model = build_chat_model(_provider(), ModelSpec())
    with pytest.raises(Exception, match="invalid api key"):
        await _stream(model)
    assert len(fake.bodies) == 1


async def test_a_failing_retry_is_not_remembered(server):
    """去掉 stream_options 重发也失败：说明不是它的事，报重发的那个错，也不记成这个接口不认它。"""
    fake = server(fail=(400, "max_tokens is too large"))
    provider = _provider()
    model = build_chat_model(provider, ModelSpec())
    with pytest.raises(Exception, match="max_tokens is too large"):
        await _stream(model)
    assert ["stream_options" in b for b in fake.bodies] == [True, False]
    assert provider.base_url not in factory._NO_STREAM_USAGE


# ---------------------------------------------------------------- 运行的用量


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def test_run_usage_counts_streamed_tokens(server, engine_up):
    """整条链：兼容接入 → llm 节点流式调用 → llm.end → 运行的用量。修之前这里是 0 token、0 美元。"""
    server()
    provider = _provider()
    async with SessionLocal() as session:
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        session.expunge(provider)
    try:
        graph = {
            "nodes": [
                {"id": "in", "type": "input", "data": {"config": {}}},
                {"id": "ask", "type": "llm", "data": {"label": "问答", "config": {
                    "prompt": "上周到店多少人", "provider": provider.name, "model": PRICED}}},
                {"id": "out", "type": "output", "data": {"config": {
                    "fields": [{"name": "回答", "value": "{{ nodes.ask.text }}"}]}}},
            ],
            "edges": [{"source": "in", "target": "ask"}, {"source": "ask", "target": "out"}],
        }
        started = await run_manager.start(graph=graph, input_payload={})
        for _ in range(200):
            await asyncio.sleep(0.05)
            async with SessionLocal() as session:
                row = await session.get(Run, started.id)
            if row.status in ("succeeded", "failed"):
                break
        assert row.status == "succeeded", row.error
        async with SessionLocal() as session:
            ends = list((await session.execute(select(RunEvent).where(
                RunEvent.run_id == started.id, RunEvent.type == "llm.end"))).scalars())
        assert [(e.data["input_tokens"], e.data["output_tokens"]) for e in ends] == [(1200, 300)]
        assert row.usage["input_tokens"] == 1200 and row.usage["output_tokens"] == 300
        assert row.usage["total_tokens"] == 1500
        expected = round(catalog.estimate_cost(PRICED, 1200, 300), 6)
        assert expected > 0 and row.usage["cost_usd"] == pytest.approx(expected)
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Provider).where(Provider.id == provider.id))
            await session.commit()
