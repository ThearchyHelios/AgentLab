"""思考内容的提取。

界面上"它大致在想什么"只有这一个来源。两家的形态完全不同，漏掉任何一种，
那一侧的模型在编排期就只剩一行不动的"正在理解需求"——而它其实想了 50 秒。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from app.engine.state import message_text, thinking_text


def test_anthropic_thinking_blocks() -> None:
    msg = AIMessage(content=[
        {"type": "thinking", "thinking": "先看看有哪些表。"},
        {"type": "text", "text": "查到 3 张表。"},
        {"type": "thinking", "thinking": "再按月份聚合。"},
    ])
    assert thinking_text(msg) == "先看看有哪些表。再按月份聚合。"
    # 正文不能把思考混进来
    assert thinking_text(msg) not in message_text(msg)
    assert "查到 3 张表。" in message_text(msg)


def test_openai_compatible_reasoning_content() -> None:
    """DeepSeek / Qianfan / vLLM 这类兼容端点把思考放在 reasoning_content。

    langchain 的 ChatOpenAI 明确不保留这个字段（"Non-standard response fields
    added by third-party providers are not extracted or preserved"），
    providers/factory.py 子类化捞了回来。
    """
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先看看现有的节点。"})
    assert thinking_text(chunk) == "先看看现有的节点。"


def test_merged_chunks_keep_whole_reasoning() -> None:
    """流式合并后思考要连成一整段，不能只剩最后一片。"""
    parts = ["用户想要", "一个能查销量的流程。", "先看看现有的节点。"]
    merged = AIMessageChunk(content="", additional_kwargs={"reasoning_content": parts[0]})
    for p in parts[1:]:
        merged = merged + AIMessageChunk(content="", additional_kwargs={"reasoning_content": p})
    assert thinking_text(merged) == "".join(parts)


def test_plain_message_has_no_thinking() -> None:
    """不产思考的模型不该被硬凑出一段来。"""
    assert thinking_text(AIMessage(content="就是 2")) == ""
    assert thinking_text(AIMessageChunk(content="x", additional_kwargs={})) == ""


def test_empty_reasoning_is_not_reported() -> None:
    """空串不算思考，否则界面上会出现一行什么都没有的"思考"。"""
    assert thinking_text(AIMessageChunk(content="", additional_kwargs={"reasoning_content": ""})) == ""


# --------------------------------------------------------------------------
# 交错思考：工具结果回来之后还能再想一段
# --------------------------------------------------------------------------
#
# 串行跑工具时这一条最要紧——每一步之间本来就该重新判断一次"看到这个结果
# 之后下一步该干嘛"。没有它，模型只在一轮开头想一次，后面全是埋头执行。


def _anthropic(model: str, **spec_kw):
    import os

    from app.db.models import Provider
    from app.providers.factory import ModelSpec, build_chat_model

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-only")
    provider = Provider(name="t", kind="anthropic", api_key=None, models=[],
                        extra=spec_kw.pop("extra", {}) or {})
    return build_chat_model(provider, ModelSpec(model=model, **spec_kw))


def test_adaptive_models_need_no_beta_header() -> None:
    """4.6 起 adaptive thinking 自带交错。

    这里不是"忘了加"——在 Opus 4.6 上手动模式压根没有交错思考，只有 adaptive
    有，所以多挂一个 header 不但没用，还会把调用改道 client.beta.messages。
    """
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-sonnet-4-6"):
        chat = _anthropic(model)
        assert chat.betas is None, model
        assert chat.thinking == {"type": "adaptive", "display": "summarized"}, model


def test_the_previous_generation_gets_the_beta_header() -> None:
    """Claude 4 / 4.5 要显式开。目录里没列它们，但模型名是放行的（可以手填）。"""
    for model in ("claude-opus-4-5", "claude-sonnet-4-5", "claude-opus-4"):
        assert _anthropic(model).betas == ["interleaved-thinking-2025-05-14"], model
    # openrouter 那种带前缀的写法也要认得出来
    assert _anthropic("openrouter/anthropic/claude-sonnet-4-5").betas == [
        "interleaved-thinking-2025-05-14"
    ]


def test_the_beta_header_comes_with_thinking_actually_enabled() -> None:
    """光挂 header 是一条什么都不做的空设置。

    这一代默认根本没开 thinking——没有思考可交错，header 挂了也白挂。
    第一版就是这么写的：betas 有了，thinking 是 None。
    """
    chat = _anthropic("claude-sonnet-4-5")
    assert chat.thinking == {"type": "enabled", "budget_tokens": 4096}
    assert chat.betas == ["interleaved-thinking-2025-05-14"]


def test_thinking_and_temperature_never_go_out_together() -> None:
    """开了 thinking 再传 temperature 就是 400。

    4.6 起的模型压根不收采样参数，supports_sampling 已经挡住了；漏的正是
    4/4.5 这一代——它们本来是收 temperature 的。
    """
    assert _anthropic("claude-sonnet-4-5", temperature=0.7).temperature is None
    # 没开 thinking 的还照常收
    assert _anthropic("claude-haiku-4-5", temperature=0.7).temperature == 0.7
    assert _anthropic("claude-sonnet-4-5", thinking="off", temperature=0.7).temperature == 0.7


def test_models_without_the_feature_are_left_alone() -> None:
    """Haiku 4.5 不支持交错思考，给它挂 header 只是噪音。"""
    assert _anthropic("claude-haiku-4-5").betas is None


def test_thinking_off_means_no_interleaving_either() -> None:
    """都不让它想了，就别再开"想得更碎"的开关。"""
    assert _anthropic("claude-sonnet-4-5", thinking="off").betas is None


def test_a_gateway_that_rejects_the_beta_can_opt_out() -> None:
    """自建网关可能不认这个 beta，而它是在请求时才炸的——留一个关得掉的口子。

    关掉的只是交错，思考本身要留着：想要"别想了"的人写的是 thinking=off。
    """
    chat = _anthropic("claude-sonnet-4-5", extra={"interleaved_thinking": False})
    assert chat.betas is None
    assert chat.thinking == {"type": "enabled", "budget_tokens": 4096}


def test_serial_tool_choice_stays_compatible_with_interleaving() -> None:
    """交错思考要求 tool_choice 是 auto 或 none。

    串行模式给的是 {"type": "auto", "disable_parallel_tool_use": true} —— auto
    不是强制调用，两者并存没问题。这条盯的是以后有人把它改成 any/tool：
    那样会同时踩坏交错思考和 thinking 本身。
    """
    from app.providers.factory import bind_tools_safely
    from app.tools.registry import ToolContext, all_specs, build_tool

    tools = [build_tool(next(iter(all_specs().values())), ToolContext())]
    bound = bind_tools_safely(_anthropic("claude-sonnet-4-5"), tools, parallel=False)
    assert bound.kwargs["tool_choice"]["type"] in ("auto", "none")
