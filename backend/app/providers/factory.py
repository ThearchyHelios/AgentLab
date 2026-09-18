from __future__ import annotations

import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.db.models import Provider
from app.providers import catalog
from app.providers.mock_model import MockChatModel


class ModelSpec(BaseModel):
    """节点上对模型的请求。省略的字段会回退到 provider 默认值。"""

    provider: str | None = None  # provider 的 name 或 id
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = Field(default=None)
    thinking: str | None = None  # off | adaptive
    effort: str | None = None  # low | medium | high | xhigh | max
    timeout: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderNotConfigured(RuntimeError):
    pass


async def list_providers(session: AsyncSession) -> list[Provider]:
    rows = await session.execute(select(Provider).order_by(Provider.created_at))
    return list(rows.scalars())


async def resolve_provider(
    session: AsyncSession, ref: str | None, model: str | None = None
) -> Provider | None:
    """找出该用哪个 provider。

    顺序很关键：显式指定的 ref 最优先；其次按模型名反查 —— 节点上通常只选了
    模型（比如 mock-fast），没选 provider，这时必须找到真正提供该模型的那一个，
    否则会拿着 A 家的模型名去调 B 家的接口。最后才回退到默认 provider。
    """
    providers = await list_providers(session)
    enabled = [p for p in providers if p.enabled]

    if ref:
        for p in providers:
            if p.name == ref or p.id == ref:
                return p
        return None

    if model:
        for p in enabled:
            if any((m or {}).get("id") == model for m in (p.models or [])):
                return p
        for p in enabled:
            if p.default_model == model:
                return p
        # 模型认得出来，但它所属的 provider 被停用了。这时候不能默默换一家 ——
        # 拿着 A 家的模型名去调 B 家，必然 404，而且报错完全指不到真正的原因。
        for p in providers:
            if not p.enabled and (
                any((m or {}).get("id") == model for m in (p.models or []))
                or p.default_model == model
            ):
                raise ProviderNotConfigured(
                    f"模型 {model!r} 属于 provider {p.name!r}，但它已被停用。"
                    f"请在设置里启用它，或给节点换一个模型。"
                )
        # 认不出来的模型名就放行：用户可能手填了一个目录里还没有的新模型

    real = [p for p in enabled if p.kind != "mock"]
    return (real or enabled or [None])[0]


def _resolve_key(provider: Provider) -> str | None:
    """明文 key。没配就退回环境变量 —— 本地实验时最省事。"""
    key = decrypt(provider.api_key)
    if key:
        return key
    env_names = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        "openai": ("OPENAI_API_KEY",),
    }.get(provider.kind, ())
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name]
    return None


def build_chat_model(provider: Provider, spec: ModelSpec) -> BaseChatModel:
    """把一条 provider 记录 + 节点配置变成可调用的 LangChain 模型。

    这里最要紧的是按模型能力裁剪参数：Claude 4.6 之后的模型收到 temperature
    会直接 400，所以不能无脑透传节点上的滑块值。
    """
    model = spec.model or provider.default_model or ""
    if not model:
        raise ProviderNotConfigured(f"provider {provider.name!r} 没有可用的模型")

    kwargs: dict[str, Any] = {"model": model}
    if spec.max_tokens:
        kwargs["max_tokens"] = spec.max_tokens
    if spec.timeout:
        kwargs["timeout"] = spec.timeout

    # 采样参数：能力允许才传
    if spec.temperature is not None and catalog.supports_sampling(model):
        kwargs["temperature"] = spec.temperature

    if provider.kind == "mock":
        return MockChatModel(model_name=model)

    api_key = _resolve_key(provider)
    base_url = provider.base_url or None
    extra = dict(provider.extra or {})

    if provider.kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        if not api_key:
            raise ProviderNotConfigured(f"provider {provider.name!r} 缺少 API Key")

        bearer = extra.get("auth_style") == "bearer"
        kwargs["api_key"] = "placeholder-replaced-below" if bearer else api_key
        if base_url:
            kwargs["base_url"] = base_url
        if extra.get("headers"):
            kwargs["default_headers"] = dict(extra["headers"])

        # thinking 这类模型会先花 token 思考再输出正文；给太小的 max_tokens
        # 会出现"思考完就没额度写答案"，返回一条空正文。
        kwargs.setdefault("max_tokens", 8192)

        # 交错思考 —— 工具结果回来之后模型还能再想一段，而不是一轮开头想完
        # 就只剩埋头执行。串行跑工具时这一条最要紧：每一步之间本来就该重新
        # 判断一次"看到这个结果之后下一步该干嘛"。
        #
        # 两代模型的开法完全不同，这也是为什么这里要分叉：
        #   4.6 起  adaptive thinking **自带**交错，不需要 beta header。
        #           （Opus 4.6 上手动模式压根没有交错，只有 adaptive 有，
        #            所以这一档绝不能退回手动。）
        #   4 / 4.5 手动开 thinking + 挂 interleaved-thinking beta header。
        #           目录里没列这一代，但模型名是放行的，用户可以手填。
        model_kwargs: dict[str, Any] = {}
        thinking_on = False
        mode = spec.thinking or "summarized"

        if catalog.supports_adaptive_thinking(model):
            if mode in ("adaptive", "summarized"):
                # summarized 会把思考摘要一起返回，画布上就能展示"它在想什么"
                kwargs["thinking"] = {
                    "type": "adaptive",
                    "display": "summarized" if mode == "summarized" else "omitted",
                }
                thinking_on = True
            elif mode == "off":
                kwargs["thinking"] = {"type": "disabled"}
        elif mode != "off" and catalog.needs_interleaved_beta(model):
            # 光挂 header 是不够的：这一代默认根本没开 thinking，
            # 没有思考可交错，header 就是一条什么都不做的空设置
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, int(kwargs["max_tokens"]) // 2),
            }
            thinking_on = True
            # betas 会把调用改道 client.beta.messages。自建网关可能不认它，
            # 而那是请求时才炸的，所以留一个关得掉的口子
            if extra.get("interleaved_thinking") is not False:
                kwargs["betas"] = [catalog.INTERLEAVED_BETA]

        # 开了 thinking 就不能再传 temperature（要求是 1）。4.6 起的模型压根
        # 不收采样参数，上面 supports_sampling 已经挡住了；漏的是 4/4.5 这一代
        # ——它们本来收 temperature，一旦开了 thinking 再传就是 400
        if thinking_on:
            kwargs.pop("temperature", None)

        if spec.effort and catalog.supports_effort(model):
            model_kwargs["output_config"] = {"effort": spec.effort}
        if model_kwargs:
            kwargs["model_kwargs"] = model_kwargs

        chat = ChatAnthropic(**kwargs)
        if bearer:
            # 网关只认 Authorization: Bearer，且多带一个 x-api-key 就会 401。
            # ChatAnthropic 的 default_headers 是 dict[str,str]，塞不进 SDK 的 Omit 哨兵，
            # 所以直接换掉底层 client —— anthropic SDK 原生支持 auth_token，
            # 且 api_key=None 时不会发出 x-api-key。
            import anthropic

            client_args = {"base_url": base_url, "api_key": None, "auth_token": api_key}
            chat._client = anthropic.Anthropic(**client_args)
            chat._async_client = anthropic.AsyncAnthropic(**client_args)
        return chat

    # openai 与 openai 兼容走同一个类，区别只在 base_url
    if provider.kind == "openai_compatible" and not base_url:
        raise ProviderNotConfigured(f"provider {provider.name!r} 需要填 base_url")
    # 很多本地服务（Ollama、vLLM）不校验 key，但 SDK 要求非空
    kwargs["api_key"] = api_key or "not-needed"
    if base_url:
        kwargs["base_url"] = base_url
    if extra.get("headers"):
        kwargs["default_headers"] = extra["headers"]
    return _ReasoningAwareChatOpenAI(**kwargs)


def _reasoning_aware_openai() -> type:
    """ChatOpenAI 的子类，保留供应商的思考内容。

    langchain 的 ChatOpenAI 只认官方 OpenAI 规范，第三方加的 reasoning_content
    是**明确丢弃**的（它的文档原话："Non-standard response fields added by
    third-party providers are not extracted or preserved"），建议换用
    ChatDeepSeek 之类的专用包。

    但"用哪个供应商"在这个产品里是用户在设置页配的，可能是 DeepSeek、
    Qianfan、vLLM、OpenRouter 里任何一个——为每种装一个专用包不现实。而这
    段内容正是界面上"它大致在想什么"的唯一真实来源：实测 deepseek-v4-pro
    一次调用花 160 个 token 在推理上，全被丢掉了，于是编排期那 50 秒里
    界面上只有一行不动的"正在理解需求"。

    所以在这里把它捞回 additional_kwargs，thinking_text 再统一读出来。
    只加字段，不改 langchain 对正文的任何处理。
    """
    from langchain_openai import ChatOpenAI

    class ReasoningAwareChatOpenAI(ChatOpenAI):  # type: ignore[misc]
        def _convert_chunk_to_generation_chunk(
            self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
        ):
            gen = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if gen is None:
                return gen
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
            delta = (choices[0] or {}).get("delta") or {} if choices else {}
            # 不同网关的叫法不统一，见到哪个用哪个
            text = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(text, str) and text:
                gen.message.additional_kwargs["reasoning_content"] = text
            return gen

    return ReasoningAwareChatOpenAI


class _LazyReasoningChatOpenAI:
    """延迟导入 langchain_openai：它加载得慢，而 mock/anthropic 路径用不上。"""

    def __call__(self, **kwargs: Any) -> Any:
        return _reasoning_aware_openai()(**kwargs)


_ReasoningAwareChatOpenAI = _LazyReasoningChatOpenAI()


def bind_tools_safely(model: BaseChatModel, tools: list[Any], *, parallel: bool) -> Any:
    """绑定工具，并在需要时让模型一次只发一个工具调用。

    `parallel_tool_calls=False` 两家都认，只是落点不同：ChatAnthropic 把它翻成
    `tool_choice={"type": "auto", "disable_parallel_tool_use": True}`（type 是 auto
    而不是强制某个工具，所以和 thinking 并存没问题），ChatOpenAI 原样透传。

    但 provider 可以是任意 OpenAI 兼容网关（Ollama、vLLM、OpenRouter…），个别
    网关可能在**请求时**才拒绝这个字段——那种情况这里的 try 拦不住。真撞上了，
    把节点上的"并行执行工具"打开即可，绑定就退回原样。

    无论如何这都只是省 token 的优化：模型仍然多发了几个调用时，编排层一轮只执行
    第一个，串行语义不依赖这里能不能传成功。
    """
    if not tools:
        return model
    if parallel:
        return model.bind_tools(tools)
    try:
        return model.bind_tools(tools, parallel_tool_calls=False)
    except (TypeError, ValueError, NotImplementedError):
        return model.bind_tools(tools)


async def get_chat_model(session: AsyncSession, spec: ModelSpec) -> tuple[BaseChatModel, str]:
    """返回 (模型, 实际使用的模型 id)。找不到任何 provider 时回退到 mock，
    保证一张图在没配 key 的机器上也能跑完。"""
    provider = await resolve_provider(session, spec.provider, spec.model)
    if provider is None:
        return MockChatModel(model_name="mock-fast"), "mock-fast"
    try:
        model = build_chat_model(provider, spec)
    except ProviderNotConfigured:
        raise
    return model, (spec.model or provider.default_model or "")
