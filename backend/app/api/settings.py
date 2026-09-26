from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import explain, not_configured, raw
from app.core.config import settings as app_settings
from app.core.crypto import encrypt, mask
from app.db.base import get_session
from app.db.models import Provider, Setting
from app.providers import catalog
from app.providers.factory import ModelSpec, ProviderNotConfigured, _resolve_key, build_chat_model

router = APIRouter(prefix="/api", tags=["settings"])


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    default_model: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 传空字符串表示清空；不传表示不动
    models: list[dict[str, Any]] | None = None
    default_model: str | None = None
    enabled: bool | None = None
    extra: dict[str, Any] | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: str
    base_url: str | None
    default_model: str | None
    models: list[dict[str, Any]]
    enabled: bool
    extra: dict[str, Any]
    # 只回掩码，明文 key 永远不出后端
    api_key_masked: str = ""
    has_key: bool = False


def _to_out(row: Provider) -> ProviderOut:
    return ProviderOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        base_url=row.base_url,
        default_model=row.default_model,
        models=row.models or [],
        enabled=row.enabled,
        extra={k: v for k, v in (row.extra or {}).items() if k != "headers"} | (
            {"headers": {k: "***" for k in (row.extra or {}).get("headers", {})}}
            if (row.extra or {}).get("headers")
            else {}
        ),
        api_key_masked=mask(row.api_key),
        has_key=bool(row.api_key),
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(session: AsyncSession = Depends(get_session)) -> list[ProviderOut]:
    rows = (await session.execute(select(Provider).order_by(Provider.created_at))).scalars()
    return [_to_out(r) for r in rows]


@router.get("/providers/catalog")
async def provider_catalog() -> dict[str, Any]:
    """内置的 provider 预设和模型目录，前端用它渲染"添加 Provider"表单。"""
    return {
        "kinds": catalog.PROVIDER_KINDS,
        "env_detected": {
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_AUTH_TOKEN": bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL") or None,
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "TAVILY_API_KEY": bool(os.environ.get("TAVILY_API_KEY")),
        },
    }


@router.post("/providers", response_model=ProviderOut, status_code=201)
async def create_provider(
    payload: ProviderIn, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    problem = _config_problem(payload.kind, payload.base_url)
    if problem:
        raise HTTPException(400, problem)
    exists = (
        await session.execute(select(Provider).where(Provider.name == payload.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"已经有叫「{payload.name}」的模型接入了，换个名字")

    row = Provider(
        name=payload.name,
        kind=payload.kind,
        base_url=payload.base_url or None,
        api_key=encrypt(payload.api_key),
        models=payload.models,
        default_model=payload.default_model,
        enabled=payload.enabled,
        extra=payload.extra,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: str, payload: ProviderPatch, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    row = await session.get(Provider, provider_id)
    if not row:
        raise HTTPException(404, "这个模型接入不存在，可能已经被删了")

    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data:
        row.api_key = encrypt(data.pop("api_key")) if data["api_key"] else None
        data.pop("api_key", None)
    for key, value in data.items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    row = await session.get(Provider, provider_id)
    if not row:
        raise HTTPException(404, "这个模型接入不存在，可能已经被删了")
    await session.delete(row)
    await session.commit()


class TestIn(BaseModel):
    model: str | None = None
    prompt: str = "用一句话介绍你自己"


def _config_problem(kind: str, base_url: str | None) -> str | None:
    """保存或测试前就能看出来的配置错误。"""
    if kind not in {k["kind"] for k in catalog.PROVIDER_KINDS}:
        return f"不认识「{kind}」这种接入类型"
    if kind == "openai_compatible" and not (base_url or "").strip():
        return "「OpenAI 兼容」必须填 Base URL，例如 https://api.deepseek.com/v1"
    return None


#: 测试连接最多等多久。真正跑节点用的是 model_timeout_seconds（默认 5 分钟），
#: 弹窗里转 5 分钟等于没有反馈
_TEST_TIMEOUT = 30


async def _try_model(provider: Provider, model: str | None, prompt: str) -> dict[str, Any]:
    """真发一次请求。失败也是正常结果，照实返回原因，不抛。"""
    import asyncio
    import time

    from app.engine.state import message_text

    model = model or provider.default_model
    if not model:
        return {"ok": False, "error": "还没有可测的模型",
                "hint": "先在「可选模型」里加一个，或者填上默认模型", "detail": ""}
    try:
        chat = build_chat_model(provider, ModelSpec(model=model, max_tokens=256,
                                                    timeout=_TEST_TIMEOUT))
    except ProviderNotConfigured as e:
        if "API Key" in str(e):
            return {"ok": False, "error": "还没有填 API Key",
                    "hint": "填上 Key 再测；也可以在后端的环境变量里配", "detail": str(e)}
        return {"ok": False, "error": not_configured(e), "hint": "", "detail": str(e)}

    started = time.perf_counter()
    try:
        async with asyncio.timeout(_TEST_TIMEOUT):
            reply = await chat.ainvoke(prompt)
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {"ok": False, "error": f"测试没通过：{reason}", "hint": hint, "detail": raw(e),
                "model": model}

    usage = getattr(reply, "usage_metadata", None) or {}
    return {
        "ok": True,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model": model,
        "reply": message_text(reply)[:500],
        "usage": usage,
    }


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str, payload: TestIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """真发一次请求验证配置。设置页的"测试连接"按钮调它。"""
    row = await session.get(Provider, provider_id)
    if not row:
        raise HTTPException(404, "这个模型接入不存在，可能已经被删了")
    return await _try_model(row, payload.model, payload.prompt)


class ProviderDraftIn(BaseModel):
    """一份还没保存（或改了还没保存）的接入配置。带 id 表示在编辑已有的那个。"""

    id: str | None = None
    name: str = "草稿"
    kind: str = "anthropic"
    base_url: str | None = None
    api_key: str | None = None  # 编辑时留空=沿用已保存的
    models: list[dict[str, Any]] = Field(default_factory=list)
    default_model: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    #: 这次测哪个模型，不填用 default_model
    model: str | None = None
    prompt: str = "用一句话介绍你自己"


def _draft_provider(payload: ProviderDraftIn, saved: Provider | None) -> Provider:
    """拼一个不进会话的临时接入点，只拿来试一下。

    编辑时 Key 框留空表示不改、请求头回显的是 ***——测试都得换回存着的真值，
    否则改个 Base URL 想测一下，只会得到一句"密钥不对"。
    """
    extra = dict(payload.extra or {})
    headers = extra.get("headers")
    if saved and isinstance(headers, dict):
        old = (saved.extra or {}).get("headers") or {}
        extra["headers"] = {k: (old.get(k, v) if v == "***" else v) for k, v in headers.items()}
    return Provider(
        name=payload.name or (saved.name if saved else "草稿"),
        kind=payload.kind,
        base_url=(payload.base_url or "").strip() or None,
        api_key=encrypt(payload.api_key) if payload.api_key else (saved.api_key if saved else None),
        models=payload.models,
        default_model=payload.default_model,
        enabled=True,
        extra=extra,
    )


async def _draft(payload: ProviderDraftIn, session: AsyncSession) -> Provider:
    saved = await session.get(Provider, payload.id) if payload.id else None
    return _draft_provider(payload, saved)


@router.post("/providers/test")
@router.post("/settings/providers/test", include_in_schema=False)
async def test_provider_draft(
    payload: ProviderDraftIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """测一份没保存的配置，不落库。

    以前只能测已保存的：Base URL 填错要保存→关弹窗→点测试→看提示→再打开改。
    接内网网关这种填错一个字就连不上的东西，来回四五趟。
    """
    problem = _config_problem(payload.kind, payload.base_url)
    if problem:
        return {"ok": False, "error": problem, "hint": "", "detail": ""}
    return await _try_model(await _draft(payload, session), payload.model, payload.prompt)


@router.post("/providers/models")
async def probe_provider_models(
    payload: ProviderDraftIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """问这个接入点有哪些模型，省得手敲模型 id。

    知识库那边的探测不带鉴权，接需要 Key 的网关时必然 401，所以这里单独做一份：
    Key、自定义请求头都按真正调用时的方式带上。
    """
    import httpx

    problem = _config_problem(payload.kind, payload.base_url)
    if problem:
        return {"ok": False, "error": problem, "hint": "", "detail": "", "models": []}
    provider = await _draft(payload, session)
    if provider.kind == "mock":
        return {"ok": True, "models": [m["id"] for m in catalog.MOCK_MODELS], "url": ""}

    extra = dict(provider.extra or {})
    # 和真正调用时取同一把钥匙：只有官方 OpenAI / Anthropic 才退回环境变量。
    # 兼容网关是别人家的服务，Key 框空着就不带——否则点一下「拉取模型」，
    # 环境里那把 OpenAI 的钥匙就发给了表单上随便填的一个地址
    key = _resolve_key(provider) or ""
    headers: dict[str, str] = {}
    if provider.kind == "anthropic":
        base = provider.base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        url = base.rstrip("/") + "/v1/models"
        headers["anthropic-version"] = "2023-06-01"
        if key:
            if extra.get("auth_style") == "bearer":
                headers["Authorization"] = f"Bearer {key}"
            else:
                headers["x-api-key"] = key
    else:
        url = (provider.base_url or "https://api.openai.com/v1").rstrip("/") + "/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    headers.update({k: str(v) for k, v in (extra.get("headers") or {}).items()})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {"ok": False, "error": f"没拿到模型列表：{reason}", "hint": hint,
                "detail": raw(e), "models": [], "url": url}
    models = [m.get("id", "") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "models": models, "url": url}


# --------------------------------------------------------------------------
# 用户设置
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "ui": {"theme": "system", "language": "zh", "canvas_snap": True, "autosave": True},
    "run": {
        "default_memory_scope": "default",
        "default_collection": "default",
        "stream_tokens": True,
        "confirm_dangerous_tools": True,
    },
    "limits": {
        "max_graph_steps": app_settings.max_graph_steps,
        "max_agent_steps": app_settings.max_agent_steps,
        "max_run_seconds": app_settings.max_run_seconds,
    },
}


async def run_defaults(session: AsyncSession) -> dict[str, Any]:
    """设置里 run 这一组，没存过的键补内置默认。

    发起运行和引擎都从这里读。各拼各的默认值，两边迟早会对「没存过」理解得
    不一样——以前这三项就是只存不读，设置页上改了等于没改。
    """
    row = await session.get(Setting, "run")
    return {**DEFAULT_SETTINGS["run"], **((row.value if row else None) or {})}


def tool_approval_default(run: dict[str, Any]) -> str:
    """「危险工具默认需要人工确认」换算成节点 approval 的取值，节点自己配了的不看它。

    只有明确存了 False 才关：值缺了、存坏了都按开着算。关掉审批得是一次明确的
    选择，不能是某个字段没写上的副作用。
    """
    return "never" if run.get("confirm_dangerous_tools") is False else "dangerous"


async def resolve_run_scope(
    session: AsyncSession, memory_scope: str | None, collection: str | None
) -> tuple[str, str]:
    """请求没带的记忆域和知识库，取设置里的运行默认值。

    空串等于没填：设置页把输入框清空再保存，存下来的就是空串，拿它当记忆域
    写进去的东西谁也读不回来。
    """
    defaults = await run_defaults(session)

    def pick(explicit: str | None, key: str) -> str:
        for value in (explicit, defaults.get(key)):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "default"

    return pick(memory_scope, "default_memory_scope"), pick(collection, "default_collection")


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars()
    stored = {r.key: r.value for r in rows}
    merged = {k: {**v, **(stored.get(k) or {})} for k, v in DEFAULT_SETTINGS.items()}
    for key, value in stored.items():
        merged.setdefault(key, value)
    return merged


class SettingsIn(BaseModel):
    values: dict[str, Any]


@router.put("/settings")
async def put_settings(
    payload: SettingsIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    for key, value in payload.values.items():
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    return await get_settings(session)


@router.get("/system")
async def system_info() -> dict[str, Any]:
    """给设置页顶部展示的运行环境概览。"""
    import sys

    from app.sandbox.manager import sandbox_manager

    return {
        "python": sys.version.split()[0],
        "data_dir": str(app_settings.data_dir),
        "sandbox": await sandbox_manager.health(),
        "limits": {
            "max_graph_steps": app_settings.max_graph_steps,
            "max_agent_steps": app_settings.max_agent_steps,
            "max_concurrent_runs": app_settings.max_concurrent_runs,
            "max_run_seconds": app_settings.max_run_seconds,
            "model_timeout_seconds": app_settings.model_timeout_seconds,
        },
    }
