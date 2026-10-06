"""模型调用的输出额度（providers/budget.py）：助手改图、核查、证据裁判共用的那几样。

推理模型的思考也算在输出额度里，额度小了，想完就没额度写正文：回复是空的，或者 JSON 只写了一半。这里守几件事：
- 认得出「输出到了上限被截断」：OpenAI 系 finish_reason=length、Anthropic stop_reason=max_tokens，以及 openai 结构化
  输出解析时抛的 LengthFinishReasonError；服务方嫌额度太大的 400 不算截断（那是请求被拒，一个字都没写）
- 认得出「服务方嫌额度太大」：400 / 422，报错里有 max_tokens 一类字样；别的 400 不算
- 截断的那次调用花掉的用量从异常里取得到（额度是真花出去了）
- BudgetFallback：先按大额度调，被嫌大时按小额度重来一次，之后的调用都用小的；流式、非流式都认；bind_tools、
  with_structured_output 包出来的和原来的共用同一个开关；只在还没吐出任何东西时重来；小额度的模型要退回时才建
- 核查、证据裁判另认两种「额度被挡下」（budget_refused）：自建服务（text-generation-inference 一类）说的
  max_new_tokens，和 anthropic SDK 在本地就拒的额度（请求没发出去）；助手改图的认法（budget_rejected）不变
- 给了 key（接入、模型、大额度）的，被嫌大一次就记在进程里：之后新建的调用直接用小的，不再每次先撞一次；没给 key 的
  （助手改图）不记也不查

模型都是剧本，不碰真接口。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from app.providers import budget
from app.providers.budget import (
    BUDGET_WORDS,
    BudgetFallback,
    budget_refused,
    budget_rejected,
    cut_off,
    cut_words,
    error_usage,
    truncated_error,
)


@pytest.fixture(autouse=True)
def forget_rejections():
    """被嫌大过的接入记在进程里：每个测试从没记过开始，也不留给后面的测试。"""
    budget._REJECTED.clear()
    yield
    budget._REJECTED.clear()


def length_error(prompt: int = 9000, completion: int = 16384) -> LengthFinishReasonError:
    """openai 的结构化输出在 finish_reason=length 时抛的那个错（原话和真实运行里一样）。"""
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=prompt, total_tokens=prompt + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


class Rejected(Exception):
    """仿 openai / anthropic SDK 的 400：一发请求就被拒，还没吐出任何东西。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


TOO_LARGE = "max_tokens: 16384 > 8192, which is the maximum allowed number of output tokens"
#: text-generation-inference 的 OpenAI 兼容接口：输入加额度超过 max_total_tokens 时的 422（原话的样子）
TGI_TOO_LARGE = ("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 8192. "
                 "Given: 1200 `inputs` tokens and 16384 `max_new_tokens`")
#: anthropic SDK 在本地拒绝时的原话：没设超时的客户端非流式调用、额度超过这个模型不流式能给的上限
SDK_REFUSED = ("Streaming is required for operations that may take longer than 10 minutes. See "
               "https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python#long-requests for more details")


class Model:
    """记下每次调用的剧本模型。fail：每次调用都抛它；stream：流式时吐的块，吐到 boom 处抛 fail。"""

    def __init__(self, name: str, *, fail: Exception | None = None, stream: list | None = None,
                 boom: int | None = None, max_tokens: int | None = None) -> None:
        self.name = name
        self.fail = fail
        self.stream = stream if stream is not None else [AIMessageChunk(content=name)]
        self.boom = boom
        self.max_tokens = max_tokens
        self.calls: list[tuple[str, object]] = []
        self.bound: list[tuple[str, object, dict]] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls.append(("ainvoke", messages))
        if self.fail is not None:
            raise self.fail
        return AIMessage(content=self.name)

    async def astream(self, messages, *args, **kwargs):
        self.calls.append(("astream", messages))
        for i, chunk in enumerate(self.stream):
            if self.boom is not None and i == self.boom:
                raise self.fail
            yield chunk
        if self.fail is not None and self.boom is None:
            raise self.fail

    def bind_tools(self, tools, **kwargs):
        self.bound.append(("tools", tools, kwargs))
        return self

    def with_structured_output(self, schema, **kwargs):
        self.bound.append(("structured", schema, kwargs))
        return self


# --------------------------------------------------------------------------
# 认出截断、认出嫌额度太大
# --------------------------------------------------------------------------


def test_cut_off_replies_are_recognised_for_both_families():
    assert cut_off(AIMessage(content="", response_metadata={"finish_reason": "length"})) == "length"
    assert cut_off(AIMessageChunk(content=[], response_metadata={"stop_reason": "max_tokens"})) == "max_tokens"
    assert cut_off(AIMessage(content="好了", response_metadata={"finish_reason": "stop"})) == ""
    assert cut_off(AIMessage(content="", response_metadata={"stop_reason": "end_turn"})) == ""
    assert cut_off(None) == "" and cut_off({"finish_reason": "length"}) == ""


def test_length_errors_are_recognised_but_a_rejected_budget_is_not():
    assert truncated_error(length_error())
    assert truncated_error(RuntimeError("Could not parse response content as the length limit was reached"))
    assert not truncated_error(None)
    assert not truncated_error(RuntimeError("网关超时"))
    assert not truncated_error(Rejected(TOO_LARGE)), "嫌额度太大是请求被拒，模型一个字都没写，不是截断"


@pytest.mark.parametrize("message", [
    "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]",
    TOO_LARGE,
    "max_completion_tokens is too large: 16384. This model supports at most 8192 completion tokens",
    "This model's maximum context length is 32768 tokens. However, you requested 40369 tokens "
    "(7601 in the messages, 32768 in the completion).",
])
def test_a_rejected_budget_is_recognised(message):
    assert budget_rejected(Rejected(message))
    assert budget_rejected(Rejected(message, status_code=422))


def test_other_rejections_are_not_a_budget_problem():
    assert not budget_rejected(Rejected("model 'no-such-model' does not exist"))
    assert not budget_rejected(Rejected(TOO_LARGE, status_code=500)), "服务方自己出错不是嫌额度大"
    assert not budget_rejected(RuntimeError(TOO_LARGE)), "没有状态码的不认"
    assert "max_tokens" in BUDGET_WORDS


def test_verify_and_judge_also_recognise_tgi_and_a_local_sdk_refusal():
    """核查、证据裁判的额度从 4096 提到了 16384，才撞得上这两种：自建服务说的是 max_new_tokens；Bearer 鉴权的 Anthropic
    接入（客户端没设超时）调 Opus 4、4.1 这一代时，SDK 嫌 16384 太大、请求没发出去就抛 ValueError。"""
    assert budget_refused(Rejected(TGI_TOO_LARGE, status_code=422))
    assert budget_refused(ValueError(SDK_REFUSED))
    assert budget_refused(Rejected(TOO_LARGE)), "原来认的照样认"
    assert not budget_refused(ValueError("别的本地错误"))
    assert not budget_refused(RuntimeError(SDK_REFUSED)), "只认 SDK 抛的 ValueError"
    assert not budget_refused(Rejected("model 'no-such-model' does not exist"))
    assert not budget_refused(Rejected(TGI_TOO_LARGE, status_code=500))


def test_the_copilot_wording_list_stays_as_it_was():
    """助手改图按 budget_rejected 认（流式调用，SDK 不在本地拦）：认法不变，data/recipe_ai.py 的测试拿 BUDGET_WORDS 比对。"""
    assert BUDGET_WORDS == ("max_tokens", "max_completion_tokens", "max_output_tokens", "maximum context length")
    assert not budget_rejected(Rejected(TGI_TOO_LARGE, status_code=422))
    assert not budget_rejected(ValueError(SDK_REFUSED))


def test_the_tokens_spent_by_a_cut_off_call_come_from_the_error():
    assert error_usage(length_error(prompt=9000, completion=16384)) == (9000, 16384)
    assert error_usage(RuntimeError("网关超时")) == (0, 0) and error_usage(None) == (0, 0)


def test_the_words_name_who_was_cut_off_and_the_limit():
    assert cut_words("核查模型", 16384) == "核查模型的输出达到上限（16384 token）被截断"
    assert cut_words("裁判模型", None) == "裁判模型的输出达到上限被截断", "额度不知道就不写数"


# --------------------------------------------------------------------------
# 退回小额度
# --------------------------------------------------------------------------


async def test_a_rejected_budget_is_retried_once_with_the_small_one_and_stays_small():
    big, small = Model("big", fail=Rejected(TOO_LARGE)), Model("small")
    model = BudgetFallback(big, small)
    assert (await model.ainvoke(["问"])).content == "small"
    assert (await model.ainvoke(["再问"])).content == "small", "之后的调用都用小的，不再先撞一次"
    assert len(big.calls) == 1 and len(small.calls) == 2
    assert model.model is small


async def test_other_errors_are_not_retried():
    big, small = Model("big", fail=Rejected("model 'no-such-model' does not exist")), Model("small")
    with pytest.raises(Rejected):
        await BudgetFallback(big, small).ainvoke(["问"])
    assert small.calls == []


async def test_the_small_model_is_only_built_when_needed():
    built: list[str] = []
    small = Model("small")

    async def build():
        built.append("small")
        return small

    fine = BudgetFallback(Model("big"), build=build, limits=(16384, 4096))
    assert (await fine.ainvoke(["问"])).content == "big"
    assert built == [] and fine.max_tokens == 16384, "平常一个小额度的模型都不多建"

    rejecting = BudgetFallback(Model("big", fail=Rejected(TOO_LARGE)), build=build, limits=(16384, 4096))
    assert (await rejecting.ainvoke(["问"])).content == "small"
    assert (await rejecting.ainvoke(["再问"])).content == "small"
    assert built == ["small"], "退回之后建一次，之后沿用"
    assert rejecting.max_tokens == 4096


async def test_without_a_small_model_the_rejection_is_raised():
    with pytest.raises(Rejected):
        await BudgetFallback(Model("big", fail=Rejected(TOO_LARGE))).ainvoke(["问"])
    same = Model("same", fail=Rejected(TOO_LARGE))
    with pytest.raises(Rejected):
        await BudgetFallback(same, same).ainvoke(["问"])
    assert len(same.calls) == 1, "大小是同一个模型：重来也是同样的额度，不白撞第二次"


async def test_bound_and_structured_views_share_the_switch():
    """核查的工具循环里退回过小额度，收尾那次直接用小的；包出来的视图各自转给对应的小模型。"""
    big, small = Model("big", fail=Rejected(TOO_LARGE)), Model("small")
    model = BudgetFallback(big, small, limits=(16384, 4096))
    tools = model.bind_tools(["db_query"], parallel_tool_calls=False)
    assert big.bound == [("tools", ["db_query"], {"parallel_tool_calls": False})]
    assert small.bound == [("tools", ["db_query"], {"parallel_tool_calls": False})]
    assert (await tools.ainvoke(["查"])).content == "small"
    structured = model.with_structured_output({"type": "object"}, include_raw=True)
    assert (await structured.ainvoke(["交结果"])).content == "small"
    assert len(big.calls) == 1, "收尾那次不再先按大额度撞一次"
    assert structured.max_tokens == 4096 and model.max_tokens == 4096


async def test_a_sibling_shares_the_switch_and_builds_its_own_small_model():
    """收尾用的是另一份配置（关掉思考）建的模型：和工具循环的模型共用开关，要退回时按自己的配置建小的。"""
    built: list[str] = []
    loop_small, settle_small = Model("loop-small"), Model("settle-small")

    async def build_loop():
        built.append("loop")
        return loop_small

    async def build_settle():
        built.append("settle")
        return settle_small

    loop = BudgetFallback(Model("loop-big", fail=Rejected(TOO_LARGE)), build=build_loop, limits=(16384, 4096))
    assert (await loop.ainvoke(["查"])).content == "loop-small"
    settle_big = Model("settle-big")
    settle = BudgetFallback(settle_big, build=build_settle, limits=(16384, 4096), shared_with=loop)
    assert (await settle.with_structured_output({"type": "object"}).ainvoke(["交结果"])).content == "settle-small"
    assert settle_big.calls == [] and built == ["loop", "settle"]


async def test_streaming_falls_back_only_before_anything_came_out():
    big = Model("big", fail=Rejected(TOO_LARGE), stream=[])
    small = Model("small", stream=[AIMessageChunk(content="a"), AIMessageChunk(content="b")])
    model = BudgetFallback(big, small)
    assert [c.content async for c in model.astream(["问"])] == ["a", "b"]
    assert len(big.calls) == 1 and len(small.calls) == 1

    started = Model("big", fail=Rejected(TOO_LARGE), stream=[AIMessageChunk(content="x"), AIMessageChunk(content="y")],
                    boom=1)
    unused = Model("small")
    got: list[str] = []
    with pytest.raises(Rejected):
        async for chunk in BudgetFallback(started, unused).astream(["问"]):
            got.append(chunk.content)
    assert got == ["x"] and unused.calls == [], "已经吐出东西了：重来会重复输出，原样抛出"


async def test_without_limits_max_tokens_reads_the_current_model():
    big = Model("big", fail=Rejected(TOO_LARGE), max_tokens=32768)
    small = Model("small", max_tokens=8192)
    model = BudgetFallback(big, small)
    assert model.max_tokens == 32768
    await model.ainvoke(["问"])
    assert model.max_tokens == 8192


async def test_a_wider_check_can_be_given():
    """核查、证据裁判按 budget_refused 认：服务方说的是 max_new_tokens 也退回小额度。缺省（助手改图）不认。"""
    tgi = Rejected(TGI_TOO_LARGE, status_code=422)
    model = BudgetFallback(Model("big", fail=tgi), Model("small"), rejected=budget_refused)
    assert (await model.ainvoke(["问"])).content == "small"
    with pytest.raises(Rejected):
        await BudgetFallback(Model("big", fail=tgi), Model("small")).ainvoke(["问"])
    local = BudgetFallback(Model("big", fail=ValueError(SDK_REFUSED)), Model("small"), rejected=budget_refused)
    structured = local.with_structured_output({"type": "object"})
    assert (await structured.ainvoke(["交结果"])).content == "small", "包出来的视图照样按这个认"


async def test_a_rejection_is_remembered_for_later_calls_to_the_same_model():
    """双盲复算的几路盲算、反方审稿的每一句、每次质询、每次点击裁判，各是一组新建的调用：同一接入、模型、额度被嫌大
    过一次，之后新建的直接用小的，不再先撞一次。别的模型、别的额度照旧先按大的试。"""
    built: list[str] = []

    def builder(name: str):
        async def build():
            built.append(name)
            return Model(name)
        return build

    key = ("gw", "reasoner-x", 16384)
    first = BudgetFallback(Model("big", fail=Rejected(TOO_LARGE)), build=builder("small-1"), limits=(16384, 4096),
                           key=key)
    assert first.max_tokens == 16384
    assert (await first.ainvoke(["问"])).content == "small-1"
    later_big = Model("big", fail=Rejected(TOO_LARGE))
    later = BudgetFallback(later_big, build=builder("small-2"), limits=(16384, 4096), key=key)
    assert later.max_tokens == 4096, "还没调用就知道要用小的：截断时报的额度对得上"
    assert (await later.bind_tools(["db_query"]).ainvoke(["再问"])).content == "small-2"
    assert later_big.calls == [], "不再先按大额度撞一次"
    other = Model("other-big")
    assert (await BudgetFallback(other, build=builder("x"), key=("gw", "other", 16384)).ainvoke(["问"])).content \
        == "other-big"
    bigger = Model("bigger")
    assert (await BudgetFallback(bigger, build=builder("y"), key=("gw", "reasoner-x", 32768)).ainvoke(["问"])).content \
        == "bigger"
    assert built == ["small-1", "small-2"]


async def test_without_a_key_nothing_is_remembered():
    """助手改图不给 key：每次改图照旧先按大额度试（行为和挪过来之前一样）。"""
    await BudgetFallback(Model("big", fail=Rejected(TOO_LARGE)), Model("small")).ainvoke(["问"])
    assert budget._REJECTED == set()
    fresh = Model("big")
    assert (await BudgetFallback(fresh, Model("small")).ainvoke(["问"])).content == "big"


async def test_a_streaming_rejection_is_remembered_too():
    key = ("gw", "streamer", 16384)
    big = Model("big", fail=Rejected(TOO_LARGE), stream=[])
    model = BudgetFallback(big, Model("small", stream=[AIMessageChunk(content="a")]), key=key)
    assert [c.content async for c in model.astream(["问"])] == ["a"]
    again = Model("big", stream=[AIMessageChunk(content="b")])
    assert [c.content async for c in BudgetFallback(again, Model("small", stream=[AIMessageChunk(content="c")]),
                                                    key=key).astream(["问"])] == ["c"]
    assert again.calls == []
