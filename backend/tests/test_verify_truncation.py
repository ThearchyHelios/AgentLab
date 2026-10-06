"""核查 agent（engine/verify_agent.py）的输出额度：截断照实说，服务方嫌额度大时退回小额度。

核查（双盲复算、反方审稿、审批质询共用）原来固定 max_tokens 4096、thinking off。经 OpenAI 兼容接入的推理模型收不到
thinking=off，思考就能把 4096 用光：这次调用 finish_reason=length，没有工具调用也没有 JSON。于是工具循环把它当成
「模型说完了」去收尾，双盲复算记成「盲算没有查到这个数」；收尾被截断时记成「核查模型没有按结构交结果」；质询回答失败
——都没说出真正的原因。这里守几件事：

- 额度：默认 16384（judge.MAX_TOKENS，核查和裁判共用）；服务方嫌大时退回 4096 再试一次，之后工具循环、收尾都用小的，
  只在还没有任何输出时重来；别的 400 不重试。被嫌大过的接入、模型记在进程里，之后的核查直接用小的；自建服务说的
  max_new_tokens、anthropic SDK 在本地就拒的额度也认
- 被截断的回复一律不采信：工具循环里截断的回复带着的工具调用是写到一半的（Anthropic 的 tool_use 截在半路时 input 是
  空的，有的网关给半截对象），不执行；截断前查到过东西的也不收尾（同时长到点，算半路被掐断）；收尾的强制工具调用被
  截断时，解析出来的空对象、半截对象都不算交了结果，也不再退回普通调用多花一次额度。截断那次的用量照记
- 时间线上记一条 verify_truncated（节点里调用时），llm.end 标出这次被截断
- 原因经现有的渠道交出去：双盲复算的无法复算原因、反方审稿的「反查未完成」、质询的报错。开头只说「未能给出结果」，
  建议按模型从哪来的说：节点里节点上指定的模型优先，两处都说；质询只看设置。没有截断时原有说法不变，触顶原因的先后
  也不变

模型是剧本，另有真实的 ChatOpenAI、ChatAnthropic（只有 HTTP 层是假的）；数据源是现造的 SQLite 文件（借
test_verify_agent 的夹具），表名、数字都是虚构的。
"""
from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Provider, Setting
from app.engine import judge as judging
from app.engine import review as rv
from app.engine import spend, verify_agent
from app.engine.inquiry import INQUIRY_SCHEMA, _error_words
from app.engine.nodes.challenge import CHALLENGE_SCHEMA, _claim_of
from app.engine.nodes.dual_check import _slim
from app.providers import budget
from app.providers.factory import build_chat_model
from tests.test_verify_agent import VALID_SUM, Script, clean_spend, priced, q, run, shop  # noqa: F401 - 夹具按名字引入

try:  # openai SDK 3 起换成了 httpx2（和 httpx 同一套接口）：假服务要装在 SDK 真正用的那个库上
    from langchain_openai._compat import httpx
except ImportError:  # pragma: no cover - 老版本的 langchain_openai
    import httpx

CUT_16K = "核查模型的输出达到上限（16384 token）被截断"
#: 节点里（双盲复算、反方审稿）：节点上指定的模型优先于设置，两处都说
ADVICE = "通常是推理占满了输出额度。请在节点上指定不带推理的模型，或在「设置 → 偏好设置 → 核查」中更换核查模型"
#: 不在节点里（审批质询）：只看设置
ADVICE_SETTINGS = "通常是推理占满了输出额度。请在「设置 → 偏好设置 → 核查」中换用不带推理的核查模型"
FAILED = f"{CUT_16K}，未能给出结果。{ADVICE}"
FAILED_SETTINGS = f"{CUT_16K}，未能给出结果。{ADVICE_SETTINGS}"
FOUND = {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致",
         "rationale": "未作废订单的金额合计是 [[v:Q1.r0.total]]。"}
#: 反方审稿那种结构写到一半：结论写完了，理由写到一半
HALF_CHALLENGE = {"verdict": "stands", "rationale": "合计是 [[v:Q1.r0.total]]，与报告"}


@pytest.fixture(autouse=True)
def forget_rejections():
    """被嫌大过的接入记在进程里（providers/budget.py）：每个测试从没记过开始，也不留给后面的测试。"""
    budget._REJECTED.clear()
    yield
    budget._REJECTED.clear()


def cut_reply(output: int = 16384) -> AIMessage:
    """推理模型把额度全用在思考上：正文为空、没有工具调用，finish_reason=length（DeepSeek 经 LiteLLM 的样子）。"""
    return AIMessage(content="", additional_kwargs={"reasoning_content": "先想想该查哪张表……"},
                     response_metadata={"finish_reason": "length"},
                     usage_metadata={"input_tokens": 900, "output_tokens": output, "total_tokens": 900 + output})


def cut_call(name: str, args: dict) -> AIMessage:
    """工具调用写到一半到了上限（ChatAnthropic 的样子）：tool_use 截在半路时 input 是空的，有的网关给半截对象；
    stop_reason=max_tokens。"""
    block = {"type": "tool_use", "id": "toolu_q", "name": f"db_query__{name}", "input": args}
    return AIMessage(content=[{"type": "thinking", "thinking": "想了很久……", "signature": "s"}, block],
                     tool_calls=[{"name": block["name"], "args": args, "id": block["id"]}],
                     response_metadata={"stop_reason": "max_tokens"},
                     usage_metadata={"input_tokens": 900, "output_tokens": 16384, "total_tokens": 17284})


def anthropic_cut(args: dict | None = None) -> dict:
    """收尾的强制工具调用写到一半到了上限（结构化输出走 function_calling，ChatAnthropic 的缺省）：原始回复
    stop_reason=max_tokens。args 为 None 是截在思考里、一个工具调用都没写出来；给了就是截在 tool_use 里——input 是
    空的（Anthropic 非流式的样子）或半截对象（有的网关），解析出来的就是它。"""
    content = [] if args is None else [{"type": "tool_use", "id": "toolu_1", "name": "verify_result", "input": args}]
    calls = [] if args is None else [{"name": "verify_result", "args": args, "id": "toolu_1"}]
    raw = AIMessage(content=content, tool_calls=calls, response_metadata={"stop_reason": "max_tokens"},
                    usage_metadata={"input_tokens": 700, "output_tokens": 16384, "total_tokens": 17084})
    return {"raw": raw, "parsed": args, "parsing_error": None}


def length_error(prompt: int = 800, completion: int = 16384) -> LengthFinishReasonError:
    """openai 的结构化输出（json_schema）在 finish_reason=length 时抛的那个错。"""
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=prompt, total_tokens=prompt + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


def query_step(name: str, sql: str = VALID_SUM, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="查一下。", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in q(name, sql, call_id)])


class BudgetRejected(Exception):
    """服务方嫌输出额度太大时的 400（DeepSeek chat 系只收 8K）。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


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

    def __init__(self, message: str = "max_tokens: 16384 > 4096, which is the maximum allowed number of output tokens",
                 status_code: int = 400):
        self.message, self.status_code = message, status_code
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        raise BudgetRejected(self.message, self.status_code)


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
    assert out.error == FAILED
    assert model.calls == ["step"], "一条查询都没有：收尾只能交「查不到」，不再为它多花一次额度"
    assert out.answered is False, "被截断不是模型自己停下来的"
    assert out.usage["output_tokens"] == 16384, "截断那次的额度是真花出去了"
    [log] = truncated_logs(events)
    assert log["level"] == "warn" and log["max_tokens"] == 16384 and "delivered" not in log
    assert log["purpose"] == "verify" and log["message"] == out.error
    [end] = [d for k, d in events if k == EventType.LLM_END]
    assert end["truncated"] is True and end["max_tokens"] == 16384 and CUT_16K in end["error"]


@pytest.mark.parametrize("args", [{}, {"sql": "SELECT SUM(amount) AS total FROM orders WHERE is_del"}],
                         ids=["empty-input", "half-sql"])
async def test_a_tool_call_cut_off_halfway_is_not_run(monkeypatch, shop, args):
    """Anthropic 系：工具调用写到一半到了上限。写到一半的调用不能执行——空参数只会报「缺少必填参数」，半截的 SQL
    查出来的数是错的——也不按它收尾，原因照截断说。"""
    name, _ = shop
    model = Reasoner([cut_call(name, args)], structured=FOUND)
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step"], "不执行、不收尾，也不再问一轮"
    assert out.result is None and out.queries == [] and out.error == FAILED
    assert not [k for k, _ in events if k in (EventType.TOOL_START, EventType.TOOL_END, EventType.TOOL_ERROR)]
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify" and log["max_tokens"] == 16384


async def test_a_cut_after_a_query_is_reported_without_settling(monkeypatch, shop):
    """查过一次、下一轮被截断：截断前查到的不一定是要的那个数（模型可能正想改查询），按它收尾交出去的结果说不清完整
    不完整，会照常参与双盲复算的比对、反方审稿的处置。同时长到点一样算半路被掐断：不收尾，原因照截断说，查过的照记。"""
    name, _ = shop
    model = Reasoner([query_step(name), cut_reply()], structured=FOUND)
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step"], "不再为收尾多花一次额度"
    assert out.result is None and out.error == FAILED and out.answered is False
    assert [q["alias"] for q in out.queries] == ["Q1"], "查过的照记"
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify" and log["max_tokens"] == 16384
    assert not [n for n in out.notes if "截断" in n], "没有「按截断前查到的结果收尾」这一说"


# --------------------------------------------------------------------------
# 收尾被截断
# --------------------------------------------------------------------------


async def test_a_cut_off_settle_is_reported_and_not_retried_as_plain(monkeypatch, shop):
    """Anthropic 系：强制工具调用写出来之前就到了上限。退回普通调用也多半被推理占满，不再白花一次额度。"""
    name, _ = shop
    model = Reasoner([query_step(name)], structured=anthropic_cut(), plain=AIMessage(content=json.dumps(FOUND)))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final"], "没有再退回普通调用"
    assert out.result is None and out.error == FAILED
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify_result" and log["max_tokens"] == 16384
    end = [d for k, d in events if k == EventType.LLM_END][-1]
    assert end["purpose"] == "verify_result" and end["truncated"] is True and CUT_16K in end["error"]


@pytest.mark.parametrize(("schema", "args"), [
    (None, {}),
    (None, {"value": {"alias": "Q1"}}),
    (CHALLENGE_SCHEMA, HALF_CHALLENGE),
    (INQUIRY_SCHEMA, {"rationale": "合计是 [[v:Q1.r0.total]]，比报告里写的"}),
], ids=["empty-input", "half-cell", "half-challenge", "half-inquiry"])
async def test_a_settle_cut_inside_the_forced_tool_call_is_not_taken(monkeypatch, shop, schema, args):
    """强制工具调用写到一半到了上限：解析出来的是空对象（Anthropic）或半截对象（有的网关）。空对象以前记成「没有按结构
    交结果」，半截的格子引用记成「引用不可用」，反方审稿的半截对象（结论写完了、理由写到一半）照单全收成「经受住」，
    质询把半句话当成回答——截断的回复一律不采信，原因照截断说，也不退回普通调用。"""
    name, _ = shop
    model = Reasoner([query_step(name)], structured=anthropic_cut(args), plain=AIMessage(content=json.dumps(FOUND)))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc", **({"output_schema": schema} if schema else {}))
    assert model.calls == ["step", "step", "final"]
    assert out.result is None and out.error == FAILED and out.cells == []
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify_result"


async def test_an_openai_length_error_in_the_settle_is_truncation_not_a_failed_call(monkeypatch, shop):
    """openai 的结构化输出解析时抛 LengthFinishReasonError：不是「调用失败，请稍后重试」，是额度被推理占满。"""
    name, _ = shop
    model = Reasoner([query_step(name)], structured=length_error(completion=16384))
    Models(monkeypatch, model)
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc")
    assert model.calls == ["step", "step", "final"]
    assert out.error == FAILED
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
    assert out.result is None and out.error == FAILED_SETTINGS


async def test_a_plain_settle_cut_after_the_json_is_not_taken_either(monkeypatch, shop):
    """普通调用的正文里有一段 JSON、回复却是截断的：JSON 后面还在写（多半是理由的补充），说不清写完了没有，同样不采信。"""
    name, _ = shop
    cut = AIMessage(content=json.dumps(FOUND, ensure_ascii=False) + "\n补充说明：合计里还应该",
                    response_metadata={"finish_reason": "length"})
    model = Reasoner([query_step(name)], plain=cut, unstructured=True)
    Models(monkeypatch, model)
    out = await run(name)
    assert out.result is None and out.error == FAILED_SETTINGS


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


async def test_an_empty_object_without_truncation_keeps_the_shape_wording(monkeypatch, shop):
    """没被截断、交回的是空对象：照旧是「没有按结构交结果」，不说成截断。"""
    name, _ = shop
    whole = {"raw": AIMessage(content="", response_metadata={"stop_reason": "tool_use"}), "parsed": {}}
    Models(monkeypatch, Reasoner([query_step(name)], structured=whole))
    out = await run(name)
    assert out.error == "核查模型没有按结构交结果（交回的内容里没有要求的字段：value、verdict）"


async def test_the_first_limit_hit_is_still_the_one_reported(monkeypatch, shop, priced):
    """循环先因时长到点停下（一分钱还没留），收尾又被每日金额上限拦下：触顶原因照旧报先撞上的时长。双盲复算按它写
    无法复算的原因，反方审稿按「每日上限」决定还派不派后面的句子——截断的改动不能把这个先后换掉。"""
    name, _ = shop
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 1.995, "calls": 9}))
        await session.commit()
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], FOUND, model_id="priced-model")
    out = await run(name, daily_max_usd=2.0, timeout_s=0)
    assert out.limit == "timeout_s"
    assert out.error == "已达全局每日核查金额上限 $2.00，没能交结果"


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


async def test_a_rejected_budget_is_remembered_for_the_next_runs(monkeypatch, shop):
    """双盲复算的几路盲算、反方审稿的每一句、每次质询各是一次核查：同一接入、模型被嫌大过一次，之后的核查直接用小的，
    不再每次先撞一次。"""
    name, _ = shop
    model = Reasoner([query_step(name), AIMessage(content="查完了。"), query_step(name, call_id="c2")],
                     structured=FOUND)
    models = Models(monkeypatch, model, rejects_above=4096)
    first, second = await run(name), await run(name)
    assert first.error is None and second.error is None and second.result["value"] == 230
    assert models.rejecting.calls == 1, "第二次核查没有再按 16384 撞一次"


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


async def test_a_self_hosted_max_new_tokens_rejection_falls_back(monkeypatch, shop):
    """text-generation-inference 一类自建服务：输入加额度超过 max_total_tokens 时回 422，说的是 max_new_tokens。"""
    name, _ = shop
    tgi = Rejecting("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 8192. "
                    "Given: 1200 `inputs` tokens and 16384 `max_new_tokens`", status_code=422)
    model = Reasoner([query_step(name)], structured=FOUND)
    models = Models(monkeypatch, model, rejects_above=4096, rejecting=tgi)
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    assert [s.max_tokens for s in models.specs] == [16384, 4096, 16384, 4096] and tgi.calls == 1


async def test_the_verify_spec_asks_for_the_larger_budget():
    """核查模型的配置（api/settings.py 的 verify_model_spec 沿用证据裁判的额度）：16384，思考照旧关掉。"""
    assert judging.MAX_TOKENS == 16384 and judging.FALLBACK_MAX_TOKENS == 4096


# --------------------------------------------------------------------------
# 原因经现有的渠道交出去
# --------------------------------------------------------------------------


async def test_the_reason_reaches_dual_check_and_challenge(monkeypatch, shop):
    name, _ = shop
    Models(monkeypatch, Reasoner([cut_reply()]))
    out = await run(name, node_id="dc")
    # 双盲复算：无法复算的原因
    slim = _slim(out)
    assert slim["value"] is None and slim["reason"] == FAILED
    # 反方审稿：这句记为无法判断，理由写「反查未完成」，没能反查的原因就是截断
    cand = judging.Candidate(unit="u1", priority=1, order=0, text="华南每周的销售额都高于华东", section="",
                             aliases=("Q1",))
    claim, why = _claim_of(cand, out)
    assert claim["verdict"] == "inconclusive" and claim["rationale"] == f"反查未完成：{FAILED}" and why == FAILED


async def test_an_inquiry_names_the_settings_and_not_a_check(monkeypatch, shop):
    """审批质询不在节点里（核查模型只看设置）：建议只说设置；开头只说「未能给出结果」——提问人问的是问题，不是在核查。"""
    name, _ = shop
    Models(monkeypatch, Reasoner([cut_reply()]))
    out = await run(name, output_schema=INQUIRY_SCHEMA)
    words = _error_words(out, timeout_s=60, daily_max_usd=1.0)
    assert words == FAILED_SETTINGS
    assert "核查未能完成" not in words and "节点" not in words


def test_the_review_layer_knows_the_truncation_log():
    """跑完之后的复核层（engine/review.py）认得 verify_truncated：同 verify_limit，是缺口不是断裂，kind 是它自己，
    不落成笼统的 warn；不触发「重跑一次大概率能好」——同样的模型再跑一遍还是撞同一堵墙。"""
    assert rv.WARN_CODES["verify_truncated"] == ("verify_truncated", rv.DEGRADED)
    event = {"type": str(EventType.LOG), "node_id": "dc",
             "data": {"level": "warn", "code": "verify_truncated", "message": FAILED}}
    [signal] = rv.scan([event], {"结论": "本周订单 120 单。"})
    assert (signal.kind, signal.severity) == ("verify_truncated", rv.DEGRADED) and not rv.should_retry([signal])


def test_the_reply_shapes_used_here_are_messages():
    assert isinstance(cut_reply(), BaseMessage) and isinstance(anthropic_cut()["raw"], BaseMessage)
    assert isinstance(cut_call("x", {}), BaseMessage)


# --------------------------------------------------------------------------
# 真实的 ChatOpenAI 和 openai SDK：只有 HTTP 层是假的（经 OpenAI 兼容网关接入的推理模型就是这一路）
# --------------------------------------------------------------------------

GATEWAY_MODEL = "reasoner-v4"


class Gateway:
    """OpenAI 兼容网关：按顺序回 chat.completion（最后一个重复用）。cap 给了，请求的额度超过它就回 400（DeepSeek chat 系
    只收 8K 那样）。bodies 记下每个请求体。"""

    def __init__(self, replies: list, *, cap: int | None = None) -> None:
        self.replies, self.cap = list(replies), cap
        self.bodies: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        budget = body.get("max_completion_tokens") or body.get("max_tokens")
        if self.cap and budget and budget > self.cap:
            message = f"max_tokens: {budget} > {self.cap}, which is the maximum allowed number of output tokens"
            return httpx.Response(400, json={"error": {"message": message, "type": "invalid_request_error"}},
                                  request=request)
        message, finish, spent = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, request=request, json={
            "id": "c-1", "object": "chat.completion", "created": 1, "model": GATEWAY_MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": spent, "total_tokens": 900 + spent}})


def gateway(monkeypatch, replies: list, *, cap: int | None = None) -> Gateway:
    fake = Gateway(replies, cap=cap)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake.handle)
    provider = Provider(name=f"gw-{uuid.uuid4().hex[:8]}", kind="openai_compatible", base_url="http://gw.example.test/v1",
                        api_key=None, models=[{"id": GATEWAY_MODEL}], default_model=GATEWAY_MODEL, enabled=True, extra={})

    async def build(session, spec):
        return build_chat_model(provider, spec), GATEWAY_MODEL

    monkeypatch.setattr(verify_agent, "get_chat_model", build)
    return fake


THINKING_ONLY = ({"role": "assistant", "content": "", "reasoning_content": "先看看有哪些表……"}, "length", 16384)


def tool_call(name: str) -> tuple:
    return ({"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": f"db_query__{name}", "arguments": json.dumps({"sql": VALID_SUM})}}]}, "tool_calls", 120)


async def test_a_real_client_cut_off_in_the_loop(monkeypatch, shop):
    name, _ = shop
    fake = gateway(monkeypatch, [THINKING_ONLY])
    out = await run(name)
    assert out.error == FAILED_SETTINGS
    assert len(fake.bodies) == 1 and fake.bodies[0]["max_completion_tokens"] == 16384
    assert "tools" in fake.bodies[0], "工具循环那次带着只读的查询工具"


async def test_a_real_client_cut_off_with_a_half_written_tool_call(monkeypatch, shop):
    """OpenAI 系：工具调用的参数写到一半到了上限（finish_reason=length，arguments 是半截 JSON）。不执行、不收尾。"""
    name, _ = shop
    half = ({"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": f"db_query__{name}", "arguments": '{"sql": "SELECT SUM(amount) AS total FROM ord'}}]},
        "length", 16384)
    fake = gateway(monkeypatch, [half])
    out = await run(name)
    assert out.error == FAILED_SETTINGS and out.queries == [] and len(fake.bodies) == 1


async def test_a_real_client_cut_off_in_the_json_schema_settle(monkeypatch, shop):
    """收尾走 openai 的结构化输出（json_schema）：finish_reason=length 时 SDK 抛 LengthFinishReasonError。"""
    name, _ = shop
    half = ({"role": "assistant", "content": '{"value": {"alias": "Q1", "row'}, "length", 16384)
    fake = gateway(monkeypatch, [tool_call(name), ({"role": "assistant", "content": "查完了。"}, "stop", 20), half])
    out = await run(name)
    assert out.error == FAILED_SETTINGS and len(out.queries) == 1
    assert len(fake.bodies) == 3 and "response_format" in fake.bodies[2], "收尾被截断：没有再退回普通调用"
    assert out.usage["output_tokens"] == 120 + 20 + 16384, "异常里带着的那次用量照记"


async def test_a_real_client_rejecting_the_budget_falls_back_to_4096(monkeypatch, shop):
    name, _ = shop
    done = ({"role": "assistant", "content": json.dumps(FOUND, ensure_ascii=False)}, "stop", 60)
    fake = gateway(monkeypatch, [tool_call(name), ({"role": "assistant", "content": "查完了。"}, "stop", 20), done],
                   cap=8192)
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    budgets = [b.get("max_completion_tokens") for b in fake.bodies]
    assert budgets == [16384, 4096, 4096, 4096], "第一次被拒后退回 4096，之后工具循环和收尾都用小的，不再先撞一次"


# --------------------------------------------------------------------------
# 真实的 ChatAnthropic 和 anthropic SDK：只有 HTTP 层是假的
# --------------------------------------------------------------------------

CLAUDE = "claude-x"


class Messages:
    """Anthropic 的 Messages 接口：按顺序回 message（最后一个重复用）。每个回复是 (content, stop_reason, 输出 token 数)，
    content 可以是按请求体算的函数（收尾那次强制调用的工具名照请求里的写）。bodies 记下每个请求体。"""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.bodies: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        content, stop, spent = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, request=request, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": content(body) if callable(content) else content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 700, "output_tokens": spent}})


def messages_api(monkeypatch, replies: list, *, model: str = CLAUDE, bearer: bool = False,
                 module=verify_agent) -> Messages:
    """接上一个 Anthropic 接入（bearer 为真时按 Authorization: Bearer 鉴权，factory 会换上没设超时的 SDK 客户端）。"""
    fake = Messages(replies)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake.handle)
    provider = Provider(name=f"an-{uuid.uuid4().hex[:8]}", kind="anthropic", base_url="http://an.example.test",
                        api_key="test-only-key", models=[{"id": model}], default_model=model, enabled=True,
                        extra={"auth_style": "bearer"} if bearer else {})

    async def build(session, spec):
        # 按这个接入的模型建：调用方的 spec 写的是别的模型名（mock-fast），SDK 在本地拦不拦要看真正发出去的 id
        return build_chat_model(provider, spec.model_copy(update={"model": model})), model

    monkeypatch.setattr(module, "get_chat_model", build)
    return fake


def forced(args: dict):
    """收尾那次强制调用的工具：名字照请求里的写，input 是 args。"""
    return lambda body: [{"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"], "input": args}]


def query_use(name: str) -> list:
    return [{"type": "tool_use", "id": "toolu_q", "name": f"db_query__{name}", "input": {"sql": VALID_SUM}}]


DONE = ([{"type": "text", "text": "查完了。"}], "end_turn", 10)


async def test_a_real_anthropic_cut_in_the_middle_of_a_tool_call(monkeypatch, shop):
    """思考占满了额度、tool_use 截在半路（input 是空的）：以前每一轮都执行一遍空参数的查询（报「缺少必填参数 sql」），
    撞到步数上限，最后记成「没有按结构交结果」。"""
    name, _ = shop
    cut = ([{"type": "thinking", "thinking": "想了很久……", "signature": "s"},
            {"type": "tool_use", "id": "toolu_q", "name": f"db_query__{name}", "input": {}}], "max_tokens", 16384)
    fake = messages_api(monkeypatch, [cut])
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc", max_queries=2)
    assert out.error == FAILED and out.queries == [] and out.limit is None
    assert len(fake.bodies) == 1 and fake.bodies[0]["max_tokens"] == 16384
    assert not [k for k, _ in events if k in (EventType.TOOL_START, EventType.TOOL_ERROR)]
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify"
    signals = rv.scan([{"type": str(k), "node_id": "dc", "data": d} for k, d in events], {"结论": "本周订单 120 单。"})
    assert [s.kind for s in signals] == ["verify_truncated"], "不是工具出错、也不是触顶"


@pytest.mark.parametrize(("schema", "args"), [(None, {}), (CHALLENGE_SCHEMA, HALF_CHALLENGE)],
                         ids=["empty-input", "half-challenge"])
async def test_a_real_anthropic_settle_cut_inside_the_forced_tool_call(monkeypatch, shop, schema, args):
    name, _ = shop
    fake = messages_api(monkeypatch, [(query_use(name), "tool_use", 50), DONE, (forced(args), "max_tokens", 16384)])
    events, emit = collector()
    out = await run(name, emit=emit, node_id="dc", **({"output_schema": schema} if schema else {}))
    assert out.error == FAILED and out.result is None and len(out.queries) == 1
    assert len(fake.bodies) == 3 and fake.bodies[2]["tool_choice"]["type"] == "tool", "没有再退回普通调用"
    [log] = truncated_logs(events)
    assert log["purpose"] == "verify_result" and log["max_tokens"] == 16384
    if schema is CHALLENGE_SCHEMA:
        cand = judging.Candidate(unit="u1", priority=1, order=0, text="华南每周的销售额都高于华东", section="",
                                 aliases=("Q1",))
        claim, why = _claim_of(cand, out)
        assert claim["verdict"] == "inconclusive" and why == FAILED, "半截的「经受住」不能当真"


async def test_an_anthropic_client_refusing_the_budget_locally_falls_back(monkeypatch, shop):
    """Bearer 鉴权的 Anthropic 接入（factory 换上的 SDK 客户端没设超时）：Opus 4、4.1 这一代的 id（经网关用的 Vertex、
    Bedrock 写法也是）非流式只能给 8192，16384 的请求 anthropic SDK 在本地就拒了（ValueError，请求没发出去）。照服务方
    嫌额度大处理，退回 4096。这条守的是 SDK 的行为：anthropic 以后不再在本地拦这个 id，第一个请求就会按 16384 发出去，
    到时改期望。"""
    name, _ = shop
    fake = messages_api(monkeypatch, [(query_use(name), "tool_use", 50), DONE,
                                      (forced(FOUND), "tool_use", 60)], model="claude-opus-4-1@20250805", bearer=True)
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    assert [b["max_tokens"] for b in fake.bodies] == [4096, 4096, 4096], "16384 的请求一个都没发出去"
