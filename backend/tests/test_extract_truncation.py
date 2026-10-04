"""cite_fields 的结构化抽取：输出被截断时加大额度重试，仍截断就照实说是额度在推理阶段用完了。

2026-10 一次运行里 4 个开了 cite_fields 的 Agent 抽取全部失败：抽取用 thinking: off、额度 4096，而经 OpenAI 兼容接口
接入的推理模型关不掉思考，4096 在推理阶段就用完了，openai 抛 LengthFinishReasonError（「Could not parse response
content as the length limit was reached」）。日志却写「请稍后重试；如持续失败，请到设置测试该模型」——重试、测试都没用。

- 额度默认至少 16384，节点配了更大的用节点的
- 认出「输出被截断」：openai 的 length limit 类错误、finish_reason=length、Anthropic 的 stop_reason=max_tokens；
  被截断时额度翻倍（最多 32768）重试一次
- 仍然截断：日志 code agent_field_truncated，说清是额度在推理阶段用完了，给出能做的下一步；字段记空值，不以 0 代替
- 服务方嫌额度太大（400，报错里有 max_tokens 一类字样）：退回小一档的额度重来，不能因为额度提高反而抽不成
- 非截断类的失败照旧（agent_field_unverified）

夹具全部合成：门店、会员这类虚构的表。
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from app.engine.nodes import llm as llm_mod
from app.engine.runner import run_manager
from tests.test_agent_cited_fields import (  # noqa: F401 - 夹具按名字引入
    EXTRACTED,
    FINAL,
    SCHEMA,
    SQLS,
    TOOL,
    Script,
    engine_up,
    events,
    graph,
    logs,
    output_of,
    shop,
    wait,
)


def length_error(prompt: int = 12000, completion: int = 4096) -> LengthFinishReasonError:
    """openai 的结构化输出在 finish_reason=length 时抛的那个错（和真实运行里的原话一样）。"""
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=prompt, total_tokens=prompt + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


class BudgetRejected(Exception):
    """服务方嫌输出额度太大时的 400（DeepSeek chat 系只收 8K）。"""

    status_code = 400


class Extraction:
    """记下每次抽取调用的额度（thinking 关掉的那次 get_chat_model），并按额度决定这次怎么回。

    outcome(budget) 返回 None 表示照常回 EXTRACTED，抛异常就是这次调用失败。
    """

    def __init__(self, monkeypatch, outcome):
        from app.providers import mock_model

        self.budgets: list[int | None] = []
        real = llm_mod.get_chat_model

        async def spy(session, spec):
            if spec.thinking == "off":
                self.budgets.append(spec.max_tokens)
            return await real(session, spec)

        monkeypatch.setattr(llm_mod, "get_chat_model", spy)
        base = Script(monkeypatch)
        loop = mock_model.MockChatModel._decide

        def decide(model, messages):
            if model.response_format:
                outcome(self.budgets[-1])
                return AIMessage(content=json.dumps(EXTRACTED, ensure_ascii=False))
            return loop(model, messages)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
        self.script = base


# --------------------------------------------------------------------------
# 认出「输出被截断」
# --------------------------------------------------------------------------


def test_length_errors_are_recognised():
    assert llm_mod.truncated_error(length_error())
    assert llm_mod.truncated_error(RuntimeError("Could not parse response content as the length limit was reached"))
    assert not llm_mod.truncated_error(RuntimeError("网关超时"))
    assert not llm_mod.truncated_error(BudgetRejected("max_tokens is too large: 16384"))


def test_cut_off_replies_are_recognised_for_both_families():
    assert llm_mod.truncated_reply(AIMessage(content="", response_metadata={"finish_reason": "length"}))
    assert llm_mod.truncated_reply(AIMessage(content="", response_metadata={"stop_reason": "max_tokens"}))
    assert not llm_mod.truncated_reply(AIMessage(content="", response_metadata={"finish_reason": "stop"}))
    assert not llm_mod.truncated_reply(None)


# --------------------------------------------------------------------------
# 额度与重试
# --------------------------------------------------------------------------


async def test_the_default_budget_leaves_room_for_reasoning(monkeypatch):
    spy = Extraction(monkeypatch, lambda budget: None)
    row = await wait((await run_manager.start(graph=graph(card=False), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [16384], "推理模型的思考也算在额度里：默认至少 16384"


async def test_a_larger_node_budget_is_used(monkeypatch):
    spy = Extraction(monkeypatch, lambda budget: None)
    row = await wait((await run_manager.start(graph=graph(card=False, max_tokens=24000), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [24000]


async def test_a_cut_off_extraction_is_retried_with_a_larger_budget(monkeypatch):
    def outcome(budget):
        if budget < 32768:
            raise length_error(completion=budget)

    spy = Extraction(monkeypatch, outcome)
    row = await wait((await run_manager.start(graph=graph(card=False), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [16384, 32768], "截断后翻倍重试一次"
    bot = await output_of(row.id, "bot")
    assert bot["data"]["gmv"] == 600.5 and bot["data_evidence"]["gmv"]["status"] == "verified"
    evts = await events(row.id)
    assert not logs(evts, "agent_field_truncated")
    # EXTRACTED 里 region 指向本节点没有的 Q9：只有它核对不了，别的字段都照常核对
    assert [w["fields"] for w in logs(evts, "agent_field_unverified")] == [["region"]]
    # 两次调用各记一行：第一次写明为什么重来
    ends = [e.data for e in evts if e.type == "llm.end" and e.data.get("purpose") == "cite_fields"]
    assert len(ends) == 2 and "32768" in ends[0]["error"] and not ends[1].get("error")
    # 被截断那次的用量照样记账（花出去的 token 是真的）
    assert ends[0]["output_tokens"] == 16384


async def test_cut_off_twice_is_reported_as_truncation_with_fields_empty(monkeypatch):
    def outcome(budget):
        raise length_error(completion=budget)

    spy = Extraction(monkeypatch, outcome)
    row = await wait((await run_manager.start(graph=graph(card=False), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [16384, 32768], "只重试一次"
    bot = await output_of(row.id, "bot")
    assert bot["data"] == {k: None for k in SCHEMA["properties"]}, "记空值，不以 0 代替"
    assert all(e["status"] == "unresolved" for e in bot["data_evidence"].values())
    evts = await events(row.id)
    assert not logs(evts, "agent_field_unverified")
    [warn] = logs(evts, "agent_field_truncated")
    assert warn["level"] == "warn" and warn["fields"] == list(SCHEMA["properties"])
    message = warn["message"]
    assert "推理阶段就用完了" in message and "结构化结论没能写出来" in message
    assert "32768" in message and "非推理模型" in message and "最大输出 token" in message
    assert "请稍后重试" not in message and "测试该模型" not in message, "重试、测试模型都不解决额度问题"
    assert "未以 0 代替" in message
    assert warn.get("max_tokens") == 32768


async def test_a_node_budget_at_the_cap_is_not_retried(monkeypatch):
    def outcome(budget):
        raise length_error(completion=budget)

    spy = Extraction(monkeypatch, outcome)
    row = await wait((await run_manager.start(graph=graph(card=False, max_tokens=40000), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [40000], "额度已经超过重试上限，再调一次也一样"
    [warn] = logs(await events(row.id), "agent_field_truncated")
    assert "40000" in warn["message"]


async def test_a_budget_the_service_rejects_falls_back(monkeypatch):
    def outcome(budget):
        if budget > 8192:
            raise BudgetRejected(f"Invalid max_tokens value, the valid range of max_tokens is [1, 8192]: {budget}")

    spy = Extraction(monkeypatch, outcome)
    row = await wait((await run_manager.start(graph=graph(card=False), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [16384, 8192]
    bot = await output_of(row.id, "bot")
    assert bot["data_evidence"]["gmv"]["status"] == "verified"


async def test_other_failures_are_reported_as_before(monkeypatch):
    def outcome(budget):
        raise RuntimeError("网关超时")

    spy = Extraction(monkeypatch, outcome)
    row = await wait((await run_manager.start(graph=graph(card=False), input_payload={})).id)
    assert row.status == "succeeded", row.error
    assert spy.budgets == [16384], "不是截断就不重试"
    evts = await events(row.id)
    assert not logs(evts, "agent_field_truncated")
    [warn] = logs(evts, "agent_field_unverified")
    assert "网关超时" in warn["message"] and "未以 0 代替" in warn["message"]
