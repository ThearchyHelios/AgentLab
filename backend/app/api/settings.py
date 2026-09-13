from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.crypto import encrypt, mask
from app.db.base import get_session
from app.db.models import Provider, Setting
from app.providers import catalog
from app.providers.factory import ModelSpec, ProviderNotConfigured, build_chat_model

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
    exists = (
        await session.execute(select(Provider).where(Provider.name == payload.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"已经有叫 {payload.name!r} 的 provider 了")

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
        raise HTTPException(404, "provider 不存在")

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
        raise HTTPException(404, "provider 不存在")
    await session.delete(row)
    await session.commit()


class TestIn(BaseModel):
    model: str | None = None
    prompt: str = "用一句话介绍你自己"


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str, payload: TestIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """真发一次请求验证配置。设置页的"测试连接"按钮调它。"""
    row = await session.get(Provider, provider_id)
    if not row:
        raise HTTPException(404, "provider 不存在")

    import time

    from app.engine.state import message_text

    try:
        model = build_chat_model(
            row, ModelSpec(model=payload.model or row.default_model, max_tokens=256)
        )
    except ProviderNotConfigured as e:
        return {"ok": False, "error": str(e)}

    started = time.perf_counter()
    try:
        reply = await model.ainvoke(payload.prompt)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    usage = getattr(reply, "usage_metadata", None) or {}
    return {
        "ok": True,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "model": payload.model or row.default_model,
        "reply": message_text(reply)[:500],
        "usage": usage,
    }


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
        },
    }
