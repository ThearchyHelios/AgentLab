"""核查 agent（engine/verify_agent.py）的输出额度：截断照实说，服务方嫌额度大时退回小额度。

核查（双盲复算、反方审稿、审批质询共用）原来固定 max_tokens 4096、thinking off。经 OpenAI 兼容接入的推理模型收不到
thinking=off，思考就能把 4096 用光：这次调用 finish_reason=length，没有工具调用也没有 JSON。于是工具循环把它当成
「模型说完了」去收尾，双盲复算记成「盲算没有查到这个数」；收尾被截断时记成「核查模型没有按结构交结果」；质询回答失败
——都没说出真正的原因。这里守几件事：

- 额度：默认 16384（judge.MAX_TOKENS，核查和裁判共用）；服务方嫌大时退回 4096 再试一次，之后工具循环、收尾都用小的，
  只在还没有任何输出时重来；别的 400 不重试
- 工具循环里被截断（没有工具调用、话没说完）不算模型自己停下：一条查询都没成功时不再收尾，直接交回截断的原因；
  查到过东西的照常按查到的收尾，取不到要的数时同样交回截断的原因
- 收尾被截断（Anthropic 的 stop_reason=max_tokens、openai 结构化输出抛的 LengthFinishReasonError、普通调用的
  finish_reason=length）：交回截断的原因，不再退回普通调用多花一次额度；截断那次的用量照记
- 时间线上记一条 verify_truncated（节点里调用时），llm.end 标出这次被截断
- 原因经现有的渠道交出去：双盲复算的无法复算原因、反方审稿的「反查未完成」、质询的报错；没有截断时原有说法不变

模型是剧本，数据源是现造的 SQLite 文件（借 test_verify_agent 的夹具），表名、数字都是虚构的。
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage, BaseMessage
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from app.core.events import EventType
from app.engine import judge as judging
from app.engine import verify_agent
from app.engine.inquiry import _error_words
from app.engine.nodes.challenge import _claim_of
from app.engine.nodes.dual_check import _slim
from tests.test_verify_agent import VALID_SUM, clean_spend, q, run, shop  # noqa: F401 - 夹具按名字引入

CUT_16K = "核查模型的输出达到上限（16384 token）被截断"
ADVICE = "通常是推理占满了输出额度，可在「设置 → 偏好设置 → 核查」中换用不带推理的模型"
FOUND = {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致",
         "rationale": "未作废订单的金额合计是 [[v:Q1.r0.total]]。"}


def cut_reply(output: int = 16384) -> AIMessage:
    """推理模型把额度全用在思考上：正文为空、没有工具调用，finish_reason=length（DeepSeek 经 LiteLLM 的样子）。"""
    return AIMessage(content="", additional_kwargs={"reasoning_content": "先想想该查哪张表……"},
                     response_metadata={"finish_reason": "length"},
                     usage_metadata={"input_tokens": 900, "output_tokens": output, "total_tokens": 900 + output})


def anthropic_cut() -> dict:
    """结构化输出走强制工具调用（Anthropic）时被截断：原始回复 stop_reason=max_tokens，解析不出结果。"""
    raw = AIMessage(content=[], response_metadata={"stop_reason": "max_tokens"},
                    usage_metadata={"input_tokens": 700, "output_tokens": 16384, "total_tokens": 17084})
    return {"raw": raw, "parsed": None, "parsing_error": None}


def length_error(prompt: int = 800, completion: int = 16384) -> LengthFinishReasonError:
    """openai 的结构化输出（json_schema）在 finish_reason=length 时抛的那个错。"""
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=prompt, total_tokens=prompt + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


def query_step(name: str, sql: str = VALID_SUM, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="查一下。", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in q(name, sql, call_id)])


class BudgetRejected(Exception):
    """服务方嫌输出额度太大时的 400（DeepSeek chat 系只收 8K）。"""

    status_code = 400


class Reasoner:
    """核查模型的剧本。steps：工具循环里每一轮的回复，用完了回「查完了。」；structured / plain：收尾时结构化调用、
    普通调用的回复（AIMessage、结构化结果，或者一个异常就抛它）。calls 按顺序记下每次调用是哪一种。"""

    def __init__(self, steps=(), *, structured=None, plain=None, unstructured: bool = False) -> None:
        self.steps = list(steps)
        self.structured = structured
        self.plain = plain
        self.unstructured = unstructured
        self.calls: list[str] = []

    def bind_tools(self, tools, **kwargs):
        return _View(self, "step")

    def with_structured_output(self, schema, **kwargs):
        if self.unstructured:
            raise NotImplementedError("这个模型不支持结构化输出")
        return _View(self, "final")

    async def ainvoke(self, messages, *args, **kwargs):
        return self.answer("plain")

    def answer(self, kind: str):
        self.calls.append(kind)
        if kind == "step":
            reply = self.steps.pop(0) if self.steps else AIMessage(content="查完了。")
        else:
            reply = self.structured if kind == "final" else self.plain
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _View:
    def __init__(self, model: Reasoner, kind: str) -> None:
        self.model, self.kind = model, kind

    async def ainvoke(self, messages, *args, **kwargs):
        return self.model.answer(self.kind)


class Rejecting:
    """一开口就被拒的模型：工具循环、收尾都一样。"""

    def __init__(self, message: str = "max_tokens: 16384 > 4096, which is the maximum allowed number of output tokens"):
        self.message = message
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        raise BudgetRejected(self.message)


class Models:
    """按额度给模型：rejects_above 给了，额度比它大的一律交 Rejecting。specs 记下每次建模型的配置。"""

    def __init__(self, monkeypatch, model, *, rejects_above: int | None = None, rejecting: Rejecting | None = None):
        self.specs = []
        self.rejecting = rejecting or Rejecting()

        async def fake(session, spec):
            self.specs.append(spec)
            if rejects_above is not None and (spec.max_tokens or 0) > rejects_above:
                return self.rejecting, "reasoner-x"
            return model, "reasoner-x"

        monkeypatch.setattr(verify_agent, "get_chat_model", fake)


def collector():
    events: list[tuple] = []
    return events, (lambda kind, **data: events.append((kind, data)))


def truncated_logs(events) -> list[dict]:
    return [d for k, d in events if k == EventType.LOG and d.get("code") == "verify_truncated"]


# --------------------------------------------------------------------------
# 工具循环里被截断
# --------------------------------------------------------------------------


async def test_thinking_that_eats_the_budget_in_the_loop_is_reported_as_truncation(monkeypatch, shop):
    name, _ = shop
    model = Reasoner([cut_reply()], structured={"value": None, "verdict": "无法判断", "rationale": "没有查询结果"})
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert out.result is None and out.queries == []
    assert out.error == f"{CUT_16K}，核查未能完成。{ADVICE}"
    assert model.calls == ["step"], "一条查询都没有：收尾只能交「查不到」，不再为它多花一次额度"
    assert out.answered is False, "被截断不是模型自己停下来的"
    assert out.usage["output_tokens"] == 16384, "截断那次的额度是真花出去了"
    [log] = truncated_logs(events)
    assert log["level"] == "warn" and log["max_tokens"] == 16384 and log["delivered"] is False
    assert log["purpose"] == "verify" and log["message"] == out.error
    [end] = [d for k, d in events if k == EventType.LLM_END]
    assert end["truncated"] is True and end["max_tokens"] == 16384 and CUT_16K in end["error"]


async def test_a_cut_after_a_query_still_settles_on_what_was_found(monkeypatch, shop):
    name, _ = shop
    model = Reasoner([query_step(name), cut_reply()], structured=FOUND)
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final"]
    assert out.error is None and out.result["value"] == 230, "查到的数照常交：截断前的查询是真查过的"
    assert out.answered is False
    assert any(CUT_16K in n and "已按截断前查到的结果收尾" in n for n in out.notes)
    [log] = truncated_logs(events)
    assert log["delivered"] is True and log["purpose"] == "verify" and log["max_tokens"] == 16384


async def test_a_cut_that_leaves_the_number_unfound_is_reported_as_truncation(monkeypatch, shop):
    """查过、可要的数没查到就被截断了：说「没查到」会让人以为数据里没有，原因要写截断。"""
    name, _ = shop
    model = Reasoner([query_step(name, "SELECT COUNT(*) AS n FROM orders"), cut_reply()],
                     structured={"value": None, "verdict": "无法判断", "rationale": "还没查到合计"})
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert out.result is None and out.error == f"{CUT_16K}，核查未能完成。{ADVICE}"
    assert [c["status"] for c in out.cells] == ["missing"] and len(out.queries) == 1, "查过的照记"
    [log] = truncated_logs(events)
    assert log["delivered"] is False and log["purpose"] == "verify"


# --------------------------------------------------------------------------
# 收尾被截断
# --------------------------------------------------------------------------


async def test_a_cut_off_settle_is_reported_and_not_retried_as_plain(monkeypatch, shop):
    """Anthropic 系：强制工具调用写到一半到了上限。退回普通调用也多半被推理占满，不再白花一次额度。"""
    name, _ = shop
    model = Reasoner([query_step(name)], structured=anthropic_cut(), plain=AIMessage(content=json.dumps(FOUND)))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final"], "没有再退回普通调用"
    assert out.result is None and out.error == f"{CUT_16K}，核查未能完成。{ADVICE}"
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify_result" and log["delivered"] is False
    end = [d for k, d in events if k == EventType.LLM_END][-1]
    assert end["purpose"] == "verify_result" and end["truncated"] is True and CUT_16K in end["error"]


async def test_an_openai_length_error_in_the_settle_is_truncation_not_a_failed_call(monkeypatch, shop):
    """openai 的结构化输出解析时抛 LengthFinishReasonError：不是「调用失败，请稍后重试」，是额度被推理占满。"""
    name, _ = shop
    model = Reasoner([query_step(name)], structured=length_error(completion=16384))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final"]
    assert out.error == f"{CUT_16K}，核查未能完成。{ADVICE}"
    assert out.usage["output_tokens"] == 16384 and out.usage["input_tokens"] >= 800, "异常里带着的用量照记"
    end = [d for k, d in events if k == EventType.LLM_END][-1]
    assert "请稍后重试" not in end["error"] and CUT_16K in end["error"] and end["output_tokens"] == 16384


async def test_a_plain_settle_cut_off_is_reported(monkeypatch, shop):
    """不支持结构化输出的模型：收尾直接走普通调用，回复只有思考、finish_reason=length。"""
    name, _ = shop
    model = Reasoner([query_step(name)], plain=cut_reply(), unstructured=True)
    Models(monkeypatch, model)
    out = await run(name)
    assert model.calls == ["step", "step", "plain"]
    assert out.result is None and out.error == f"{CUT_16K}，核查未能完成。{ADVICE}"


async def test_without_truncation_the_old_wording_stays(monkeypatch, shop):
    name, _ = shop
    model = Reasoner([query_step(name)], structured="不是 JSON", plain=AIMessage(content="还是不是 JSON"))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final", "plain"], "格式不对照旧退回普通调用补救一次"
    assert out.error == "核查模型没有按结构交结果"
    assert truncated_logs(events) == []
    assert not any(d.get("truncated") for k, d in events if k == EventType.LLM_END)


# --------------------------------------------------------------------------
# 额度与退回
# --------------------------------------------------------------------------


async def test_the_default_budget_leaves_room_for_reasoning(monkeypatch, shop):
    name, _ = shop
    models = Models(monkeypatch, Reasoner([query_step(name)], structured=FOUND))
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    assert [s.max_tokens for s in models.specs] == [16384, 16384], "工具循环、收尾都按 16384，不多建模型"
    assert models.specs[1].thinking == "off"


async def test_a_rejected_budget_falls_back_to_4096_for_the_loop_and_the_settle(monkeypatch, shop):
    name, _ = shop
    model = Reasoner([query_step(name)], structured=FOUND)
    models = Models(monkeypatch, model, rejects_above=4096)
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    assert [s.max_tokens for s in models.specs] == [16384, 4096, 16384, 4096]
    assert [s.thinking for s in models.specs][2:] == ["off", "off"], "收尾的小额度模型同样关掉思考"
    assert models.rejecting.calls == 1, "退回过一次之后收尾直接用小的，不再先撞一次"
    assert model.calls == ["step", "step", "final"]


async def test_a_cut_after_the_fallback_names_the_small_budget(monkeypatch, shop):
    name, _ = shop
    Models(monkeypatch, Reasoner([cut_reply(output=4096)]), rejects_above=4096)
    out = await run(name)
    assert out.error.startswith("核查模型的输出达到上限（4096 token）被截断")


async def test_other_rejections_are_not_retried_with_the_small_budget(monkeypatch, shop):
    name, _ = shop
    models = Models(monkeypatch, Reasoner(), rejects_above=4096,
                    rejecting=Rejecting("model 'reasoner-x' does not exist"))
    out = await run(name)
    assert out.result is None and "reasoner-x" in out.error
    assert [s.max_tokens for s in models.specs] == [16384], "不是额度的问题：不拿小额度再撞一次"


async def test_the_verify_spec_asks_for_the_larger_budget():
    """核查模型的配置（api/settings.py 的 verify_model_spec 沿用证据裁判的额度）：16384，思考照旧关掉。"""
    assert judging.MAX_TOKENS == 16384 and judging.FALLBACK_MAX_TOKENS == 4096


# --------------------------------------------------------------------------
# 原因经现有的渠道交出去
# --------------------------------------------------------------------------


async def test_the_reason_reaches_dual_check_challenge_and_inquiry(monkeypatch, shop):
    name, _ = shop
    Models(monkeypatch, Reasoner([cut_reply()]))
    out = await run(name)
    expected = f"{CUT_16K}，核查未能完成。{ADVICE}"
    # 双盲复算：无法复算的原因
    slim = _slim(out)
    assert slim["value"] is None and slim["reason"] == expected
    # 反方审稿：这句记为无法判断，理由写「反查未完成」，没能反查的原因就是截断
    cand = judging.Candidate(unit="u1", priority=1, order=0, text="华南每周的销售额都高于华东", section="",
                             aliases=("Q1",))
    claim, why = _claim_of(cand, out)
    assert claim["verdict"] == "inconclusive" and claim["rationale"] == f"反查未完成：{expected}" and why == expected
    # 审批质询：提问人看到的报错照原话
    assert _error_words(out, timeout_s=60, daily_max_usd=1.0) == expected


def test_the_reply_shapes_used_here_are_messages():
    assert isinstance(cut_reply(), BaseMessage) and isinstance(anthropic_cut()["raw"], BaseMessage)
