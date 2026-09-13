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

        model_kwargs: dict[str, Any] = {}
        if catalog.supports_adaptive_thinking(model):
            mode = spec.thinking or "summarized"
            if mode in ("adaptive", "summarized"):
                # summarized 会把思考摘要一起返回，画布上就能展示"它在想什么"
                kwargs["thinking"] = {
                    "type": "adaptive",
                    "display": "summarized" if mode == "summarized" else "omitted",
                }
            elif mode == "off":
                kwargs["thinking"] = {"type": "disabled"}
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
    from langchain_openai import ChatOpenAI

    if provider.kind == "openai_compatible" and not base_url:
        raise ProviderNotConfigured(f"provider {provider.name!r} 需要填 base_url")
    # 很多本地服务（Ollama、vLLM）不校验 key，但 SDK 要求非空
    kwargs["api_key"] = api_key or "not-needed"
    if base_url:
        kwargs["base_url"] = base_url
    if extra.get("headers"):
        kwargs["default_headers"] = extra["headers"]
    return ChatOpenAI(**kwargs)


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
