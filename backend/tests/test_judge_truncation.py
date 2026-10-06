"""结论句裁判（engine/judge.py）的输出额度：截断照实说，服务方嫌额度大时退回小额度。

裁判原来固定 max_tokens 4096、thinking off。经 OpenAI 兼容接入的推理模型收不到 thinking=off，思考就能把 4096 用光：
结构化调用写不出判定（openai 抛 LengthFinishReasonError，Anthropic 的强制工具调用写到一半），退回纯文本调用也一样，
最后记成「裁判返回的结果格式有误」——没说出真正的原因。这里守几件事：

- 额度：16384（MAX_TOKENS）；服务方嫌大时退回 4096（FALLBACK_MAX_TOKENS）再问一次，之后各批都用小的，只撞一次
- 输出到了上限被截断（stop_reason=max_tokens、finish_reason=length、LengthFinishReasonError）：这一批记未裁判，原因
  是 truncated，理由、缺口、llm.end、judge_failed 日志都写明「裁判模型的输出达到上限（N token）被截断」；结构化调用
  被截断的不再退回纯文本多花一次额度；截断那次的用量照记
- 没有截断时原有说法不变（格式不对仍是 format）

模型都是剧本；证据借 test_evidence_judge 的虚构夹具（口径卡、一次查询、一次检索）。
"""
from __future__ import annotations

import json
import re
import uuid

from langchain_core.messages import AIMessage
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from app.core.events import EventType
from app.db.models import Provider
from app.engine import judge as judge_mod
from app.engine.evidence import compose_doc
from app.engine.judge import BATCH_SIZE, FALLBACK_MAX_TOKENS, MAX_TOKENS, REASONS, judge_doc
from app.providers.factory import build_chat_model
from tests.test_evidence_judge import PRICED, RAW, SPEC, UNLIMITED, clean_settings, make_catalog, stored  # noqa: F401

try:  # openai SDK 3 起换成了 httpx2（和 httpx 同一套接口）：假服务要装在 SDK 真正用的那个库上
    from langchain_openai._compat import httpx
except ImportError:  # pragma: no cover - 老版本的 langchain_openai
    import httpx

CUT_16K = "裁判模型的输出达到上限（16384 token）被截断"
ADVICE = "通常是推理占满了输出额度，可换用不带推理的裁判模型"


def length_error(prompt: int = 3000, completion: int = 16384) -> LengthFinishReasonError:
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=prompt, total_tokens=prompt + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


def anthropic_cut(units) -> dict:
    """强制工具调用写到一半到了上限：原始回复 stop_reason=max_tokens，解析不出判定。"""
    raw = AIMessage(content=[], response_metadata={"stop_reason": "max_tokens"},
                    usage_metadata={"input_tokens": 3000, "output_tokens": 16384, "total_tokens": 19384})
    return {"raw": raw, "parsed": None, "parsing_error": None}


def thinking_only(units) -> AIMessage:
    """纯文本调用：额度全用在思考上，正文为空，finish_reason=length（推理模型经 OpenAI 兼容接入的样子）。"""
    return AIMessage(content="", additional_kwargs={"reasoning_content": "逐句看证据……"},
                     response_metadata={"finish_reason": "length"},
                     usage_metadata={"input_tokens": 3000, "output_tokens": 4096, "total_tokens": 7096})


def supported(units) -> dict:
    items = [{"unit": u, "verdict": "supported", "rationale": "证据支持", "missing": "", "used": []} for u in units]
    return {"raw": AIMessage(content="", response_metadata={"finish_reason": "stop"}), "parsed": {"items": items}}


class BudgetRejected(Exception):
    """服务方嫌输出额度太大时的 400。"""

    status_code = 400


class Reasoner:
    """裁判模型的剧本。structured(units) / plain(units) 给出结构化调用、退回纯文本调用的回复（返回异常就抛它）；
    unstructured 为真时不支持结构化输出。calls 记下每次调用的种类和判的句子。"""

    def __init__(self, *, structured=supported, plain=None, unstructured: bool = False) -> None:
        self.structured, self.plain, self.unstructured = structured, plain, unstructured
        self.calls: list[tuple[str, list[str]]] = []

    def with_structured_output(self, schema, **kwargs):
        if self.unstructured:
            raise NotImplementedError("这个模型不支持结构化输出")
        return _View(self)

    async def ainvoke(self, messages, *args, **kwargs):
        return self.answer("plain", messages)

    def answer(self, kind: str, messages):
        units = re.findall(r"^\[(u\d+)\]", str(messages[-1].content), re.M)
        self.calls.append((kind, units))
        make = self.structured if kind == "structured" else self.plain
        reply = make(units)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _View:
    def __init__(self, model: Reasoner) -> None:
        self.model = model

    async def ainvoke(self, messages, *args, **kwargs):
        return self.model.answer("structured", messages)


class Rejecting:
    def __init__(self) -> None:
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        raise BudgetRejected("max_tokens: 16384 > 4096, which is the maximum allowed number of output tokens")


class Models:
    def __init__(self, monkeypatch, model, *, rejects_above: int | None = None) -> None:
        self.specs = []
        self.rejecting = Rejecting()

        async def fake(session, spec):
            self.specs.append(spec)
            if rejects_above is not None and (spec.max_tokens or 0) > rejects_above:
                return self.rejecting, PRICED
            return model, PRICED

        monkeypatch.setattr(judge_mod, "get_chat_model", fake)


async def judged(stored, raw: str = RAW):
    catalog = make_catalog(stored)
    events: list[tuple] = []
    outcome = await judge_doc(compose_doc(raw, catalog), catalog, spec=SPEC, budget=UNLIMITED,
                              emit=lambda kind, **data: events.append((kind, data)))
    return outcome, events


# --------------------------------------------------------------------------
# 截断
# --------------------------------------------------------------------------


async def test_a_cut_off_structured_reply_is_unjudged_as_truncation_without_a_plain_retry(monkeypatch, stored):
    model = Reasoner(structured=anthropic_cut, plain=lambda units: AIMessage(content=json.dumps({"items": []})))
    Models(monkeypatch, model)
    outcome, events = await judged(stored)
    assert [kind for kind, _ in model.calls] == ["structured"], "判定写到一半到了上限：不再退回纯文本多花一次额度"
    verdicts = list(outcome["verdicts"].values())
    assert {(v["status"], v["reason"]) for v in verdicts} == {("unjudged", "truncated")}
    assert verdicts[0]["rationale"] == f"{CUT_16K}，这句未裁判。{ADVICE}"
    assert outcome["unjudged"] == {"truncated": 4} and outcome["limits_hit"] == []
    assert outcome["gaps"] == [f"有 4 句结论未裁判：{CUT_16K}，{ADVICE}"]
    [end] = [d for k, d in events if k == EventType.LLM_END]
    assert end["purpose"] == "judge" and end["error"] == CUT_16K and end["truncated"] is True
    assert end["max_tokens"] == 16384 and end["output_tokens"] == 16384
    [failed] = [d for k, d in events if k == EventType.LOG and d.get("code") == "judge_failed"]
    assert failed["message"] == f"结论句裁判未完成：有 4 句结论未裁判：{CUT_16K}，{ADVICE}"


async def test_an_openai_length_error_is_truncation_and_its_tokens_are_counted(monkeypatch, stored):
    model = Reasoner(structured=lambda units: length_error(completion=16384))
    Models(monkeypatch, model)
    outcome, events = await judged(stored)
    assert [kind for kind, _ in model.calls] == ["structured"]
    assert outcome["unjudged"] == {"truncated": 4}
    assert outcome["usage"]["output_tokens"] == 16384 and outcome["cost_usd"] > 0, "截断那次的额度是真花出去了"
    end = [d for k, d in events if k == EventType.LLM_END][-1]
    assert "调用" not in end["error"] and end["error"] == CUT_16K


async def test_a_plain_reply_with_only_thinking_is_truncation(monkeypatch, stored):
    """不支持结构化输出的模型直接退回纯文本调用：回复只有思考、finish_reason=length。"""
    model = Reasoner(plain=thinking_only, unstructured=True)
    Models(monkeypatch, model)
    outcome, _ = await judged(stored)
    assert [kind for kind, _ in model.calls] == ["plain"] and outcome["calls"] == 2
    assert outcome["unjudged"] == {"truncated": 4}
    assert outcome["gaps"] == [f"有 4 句结论未裁判：{CUT_16K}，{ADVICE}"]


async def test_a_truncated_batch_does_not_stop_the_others(monkeypatch, stored):
    """只有一批被截断：别的批照常判（和格式不对一样，不像调用失败那样后面的都不再问）。"""
    first: list[bool] = []

    def once(units):
        if not first:
            first.append(True)
            return anthropic_cut(units)
        return supported(units)

    Models(monkeypatch, Reasoner(structured=once))
    raw = "".join(f"第 {i} 个区域的销售额增长来自新客。[[see:m:gmv]]" for i in range(BATCH_SIZE + 3))
    outcome, _ = await judged(stored, raw)
    assert outcome["unjudged"] == {"truncated": BATCH_SIZE}
    assert outcome["counts"]["supported"] == 3


async def test_without_truncation_format_problems_keep_their_wording(monkeypatch, stored):
    model = Reasoner(structured=lambda units: {"raw": AIMessage(content="不是 JSON",
                                                                response_metadata={"finish_reason": "stop"}),
                                               "parsed": None},
                     plain=lambda units: AIMessage(content="还是不是", response_metadata={"finish_reason": "stop"}))
    Models(monkeypatch, model)
    outcome, events = await judged(stored)
    assert [kind for kind, _ in model.calls] == ["structured", "plain"]
    assert outcome["unjudged"] == {"format": 4}
    assert outcome["gaps"] == ["有 4 句结论未裁判：裁判返回的结果格式有误"]
    assert not any(d.get("truncated") for k, d in events if k == EventType.LLM_END)


# --------------------------------------------------------------------------
# 额度与退回
# --------------------------------------------------------------------------


async def test_the_judge_asks_for_the_larger_budget(monkeypatch, stored):
    models = Models(monkeypatch, Reasoner())
    outcome, _ = await judged(stored)
    assert outcome["counts"]["supported"] == 4
    assert [s.max_tokens for s in models.specs] == [MAX_TOKENS] == [16384], "平常只建一个模型"
    assert "truncated" in REASONS and FALLBACK_MAX_TOKENS == 4096


async def test_a_rejected_budget_falls_back_to_4096_once_for_all_batches(monkeypatch, stored):
    model = Reasoner()
    models = Models(monkeypatch, model, rejects_above=4096)
    raw = "".join(f"第 {i} 个区域的销售额增长来自新客。[[see:m:gmv]]" for i in range(BATCH_SIZE * 2 + 3))
    outcome, _ = await judged(stored, raw)
    assert outcome["counts"]["supported"] == BATCH_SIZE * 2 + 3 and outcome["gaps"] == []
    assert [s.max_tokens for s in models.specs] == [16384, 4096]
    assert models.rejecting.calls == 1, "第一批退回过，后面几批直接用小的"
    assert [kind for kind, _ in model.calls] == ["structured"] * 3


async def test_a_truncation_after_the_fallback_names_the_small_budget(monkeypatch, stored):
    Models(monkeypatch, Reasoner(plain=thinking_only, unstructured=True), rejects_above=4096)
    outcome, _ = await judged(stored)
    [verdict, *_] = outcome["verdicts"].values()
    assert verdict["reason"] == "truncated"
    assert verdict["rationale"].startswith("裁判模型的输出达到上限（4096 token）被截断")


# --------------------------------------------------------------------------
# 真实的 ChatOpenAI 和 openai SDK：只有 HTTP 层是假的（经 OpenAI 兼容网关接入的推理模型就是这一路）
# --------------------------------------------------------------------------


class Gateway:
    """OpenAI 兼容网关：reply(这一批的句子编号) 给出 (message, finish_reason, 输出 token 数)。cap 给了，请求的额度超过它
    就回 400。bodies 记下每个请求体。"""

    def __init__(self, reply, *, cap: int | None = None) -> None:
        self.reply, self.cap = reply, cap
        self.bodies: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        budget = body.get("max_completion_tokens") or body.get("max_tokens")
        if self.cap and budget and budget > self.cap:
            message = f"max_tokens: {budget} > {self.cap}, which is the maximum allowed number of output tokens"
            return httpx.Response(400, json={"error": {"message": message, "type": "invalid_request_error"}},
                                  request=request)
        units = re.findall(r"^\[(u\d+)\]", str(body["messages"][-1]["content"]), re.M)
        message, finish, spent = self.reply(units)
        return httpx.Response(200, request=request, json={
            "id": "c-1", "object": "chat.completion", "created": 1, "model": PRICED,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3000, "completion_tokens": spent, "total_tokens": 3000 + spent}})


def gateway(monkeypatch, reply, *, cap: int | None = None) -> Gateway:
    fake = Gateway(reply, cap=cap)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake.handle)
    provider = Provider(name=f"gw-{uuid.uuid4().hex[:8]}", kind="openai_compatible", base_url="http://gw.example.test/v1",
                        api_key=None, models=[{"id": PRICED}], default_model=PRICED, enabled=True, extra={})

    async def build(session, spec):
        return build_chat_model(provider, spec), PRICED

    monkeypatch.setattr(judge_mod, "get_chat_model", build)
    return fake


async def test_a_real_client_cut_off_in_the_json_schema_call(monkeypatch, stored):
    """json_schema 的结构化输出：finish_reason=length 时 openai SDK 抛 LengthFinishReasonError，不再退回纯文本。"""
    fake = gateway(monkeypatch, lambda units: ({"role": "assistant", "content": '{"items": [{"unit": "u'}, "length", 16384))
    outcome, _ = await judged(stored)
    assert outcome["unjudged"] == {"truncated": 4}
    assert outcome["gaps"] == [f"有 4 句结论未裁判：{CUT_16K}，{ADVICE}"]
    assert len(fake.bodies) == 1 and "response_format" in fake.bodies[0]
    assert fake.bodies[0]["max_completion_tokens"] == 16384
    assert outcome["usage"]["output_tokens"] == 16384, "异常里带着的那次用量照记"


async def test_a_real_client_rejecting_the_budget_falls_back_to_4096(monkeypatch, stored):
    def answer(units):
        items = [{"unit": u, "verdict": "supported", "rationale": "证据支持", "missing": "", "used": []} for u in units]
        return {"role": "assistant", "content": json.dumps({"items": items}, ensure_ascii=False)}, "stop", 80

    fake = gateway(monkeypatch, answer, cap=8192)
    outcome, _ = await judged(stored)
    assert outcome["counts"]["supported"] == 4 and outcome["gaps"] == []
    assert [b.get("max_completion_tokens") for b in fake.bodies] == [16384, 4096]
