from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# 模型目录
#
# pricing 单位是「美元 / 百万 token」，用于 run 的成本估算。
# 新模型的价格会变，这份表只是开箱即用的默认值，用户可以在设置页改。
# --------------------------------------------------------------------------

ANTHROPIC_MODELS: list[dict[str, Any]] = [
    {"id": "claude-opus-5", "label": "Claude Opus 5", "context": 1_000_000,
     "pricing": {"input": 5.0, "output": 25.0}, "thinking": "adaptive"},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "context": 1_000_000,
     "pricing": {"input": 10.0, "output": 50.0}, "thinking": "always"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "context": 1_000_000,
     "pricing": {"input": 5.0, "output": 25.0}, "thinking": "adaptive"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "context": 1_000_000,
     "pricing": {"input": 2.0, "output": 10.0}, "thinking": "adaptive"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "context": 200_000,
     "pricing": {"input": 1.0, "output": 5.0}, "thinking": "budget"},
]

OPENAI_MODELS: list[dict[str, Any]] = [
    {"id": "gpt-5.2", "label": "GPT-5.2", "context": 400_000,
     "pricing": {"input": 1.25, "output": 10.0}},
    {"id": "gpt-5.2-mini", "label": "GPT-5.2 mini", "context": 400_000,
     "pricing": {"input": 0.25, "output": 2.0}},
    {"id": "o4", "label": "o4", "context": 200_000,
     "pricing": {"input": 3.0, "output": 12.0}},
]

# 常见的 OpenAI 兼容服务，一键添加用
COMPATIBLE_PRESETS: list[dict[str, Any]] = [
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat", "context": 128_000,
             "pricing": {"input": 0.27, "output": 1.1}},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "context": 128_000,
             "pricing": {"input": 0.55, "output": 2.19}},
        ],
    },
    {
        "key": "moonshot",
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            {"id": "kimi-k2-turbo-preview", "label": "Kimi K2 Turbo", "context": 256_000,
             "pricing": {"input": 0.6, "output": 2.5}},
            {"id": "moonshot-v1-128k", "label": "Moonshot v1 128k", "context": 128_000,
             "pricing": {"input": 0.8, "output": 3.0}},
        ],
    },
    {
        "key": "dashscope",
        "label": "阿里云百炼 / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"id": "qwen-max", "label": "Qwen Max", "context": 32_000,
             "pricing": {"input": 0.34, "output": 1.37}},
            {"id": "qwen-plus", "label": "Qwen Plus", "context": 131_000,
             "pricing": {"input": 0.11, "output": 0.28}},
        ],
    },
    {
        "key": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"id": "glm-4.6", "label": "GLM-4.6", "context": 200_000,
             "pricing": {"input": 0.6, "output": 2.2}},
        ],
    },
    {
        "key": "siliconflow",
        "label": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek V3", "context": 128_000,
             "pricing": {"input": 0.27, "output": 1.1}},
        ],
    },
    {
        "key": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {"id": "anthropic/claude-opus-5", "label": "Claude Opus 5", "context": 1_000_000,
             "pricing": {"input": 5.0, "output": 25.0}},
        ],
    },
    {
        "key": "ollama",
        "label": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "api_key_optional": True,
        "models": [
            {"id": "qwen3:8b", "label": "Qwen3 8B (本地)", "context": 32_000,
             "pricing": {"input": 0.0, "output": 0.0}},
            {"id": "llama3.2", "label": "Llama 3.2 (本地)", "context": 128_000,
             "pricing": {"input": 0.0, "output": 0.0}},
        ],
    },
]

MOCK_MODELS: list[dict[str, Any]] = [
    {"id": "mock-fast", "label": "Mock（不花钱，用来试编排）", "context": 128_000,
     "pricing": {"input": 0.0, "output": 0.0}},
]

PROVIDER_KINDS = [
    {
        "kind": "anthropic",
        "label": "Anthropic",
        "base_url_hint": "留空走官方；填了就是自定义网关（读 ANTHROPIC_BASE_URL）",
        "models": ANTHROPIC_MODELS,
        "default_model": "claude-opus-5",
    },
    {
        "kind": "openai",
        "label": "OpenAI",
        "base_url_hint": "留空走官方",
        "models": OPENAI_MODELS,
        "default_model": "gpt-5.2",
    },
    {
        "kind": "openai_compatible",
        "label": "OpenAI 兼容（DeepSeek / Kimi / 通义 / Ollama…）",
        "base_url_hint": "必填，例如 https://api.deepseek.com/v1",
        "models": [],
        "presets": COMPATIBLE_PRESETS,
        "default_model": "",
    },
    {
        "kind": "mock",
        "label": "Mock（无需 API Key）",
        "base_url_hint": "",
        "models": MOCK_MODELS,
        "default_model": "mock-fast",
    },
]


# --------------------------------------------------------------------------
# 模型能力
# --------------------------------------------------------------------------

# Claude 4.6 及以后的模型移除了采样参数，传 temperature/top_p 会直接 400。
_NO_SAMPLING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)

# 支持 adaptive thinking（而不是老的 budget_tokens）的模型
_ADAPTIVE_THINKING_PREFIXES = _NO_SAMPLING_PREFIXES

# 支持 output_config.effort
_EFFORT_PREFIXES = _NO_SAMPLING_PREFIXES + ("claude-opus-4-5",)


def _norm(model: str) -> str:
    """去掉 openrouter 之类的前缀，只看模型名本身。"""
    return (model or "").split("/")[-1].strip()


def supports_sampling(model: str) -> bool:
    """这个模型还能不能传 temperature / top_p。"""
    return not _norm(model).startswith(_NO_SAMPLING_PREFIXES)


def supports_adaptive_thinking(model: str) -> bool:
    return _norm(model).startswith(_ADAPTIVE_THINKING_PREFIXES)


def supports_effort(model: str) -> bool:
    return _norm(model).startswith(_EFFORT_PREFIXES)


def find_pricing(model: str) -> dict[str, float] | None:
    target = _norm(model)
    pools = [ANTHROPIC_MODELS, OPENAI_MODELS, MOCK_MODELS]
    pools += [p["models"] for p in COMPATIBLE_PRESETS]
    for pool in pools:
        for entry in pool:
            if _norm(entry["id"]) == target:
                return entry.get("pricing")
    return None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """按目录价估算一次调用的花费（美元）。"""
    pricing = find_pricing(model)
    if not pricing:
        return 0.0
    return (
        input_tokens / 1_000_000 * pricing.get("input", 0.0)
        + output_tokens / 1_000_000 * pricing.get("output", 0.0)
    )
