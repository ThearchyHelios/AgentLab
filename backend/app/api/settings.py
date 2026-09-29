from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health
from app.core.errors import explain, not_configured, raw
from app.core.config import settings as app_settings
from app.core.crypto import encrypt, mask
from app.db.base import get_session
from app.db.models import Provider, Setting
from app.engine.judge import JUDGE_DEFAULTS, SETTING_GROUP as JUDGE_GROUP, SPEND_KEY as JUDGE_SPEND
from app.engine.judge import daily_spend, judge_settings
from app.engine.judge import sanitize_settings as judge_values, settings_problem as judge_problem
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
    #: 最近一次测连接（见 app/api/health.py）。没测过、或者连接配置改过之后都是 None
    last_checked_at: str | None = None
    last_check_ok: bool | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None


#: extra 里这一项是后端记的最近一次测连接结果，不是用户填的配置：不回显、
#: 不接受客户端写入，保存 extra 时原样留着
LAST_CHECK = "last_check"


def _user_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (extra or {}).items() if k != LAST_CHECK}


def _with_saved_headers(extra: dict[str, Any], saved: dict[str, Any] | None) -> dict[str, Any]:
    """请求头的值回显的是 ***：原样带回来的，换回存着的真值。

    设置页保存时会把读到的 extra 整块带回来。以前照单全收，于是改个名字、
    勾一下启用，自定义请求头就全变成了字面上的 ***，接口从此 401。
    """
    headers = extra.get("headers")
    if not isinstance(headers, dict):
        return extra
    old = (saved or {}).get("headers") or {}
    return {**extra, "headers": {k: (old.get(k, v) if v == "***" else v) for k, v in headers.items()}}


def _connection(provider: Provider, model: str | None = None) -> str:
    """连接配置的指纹：类型、地址、实际用的钥匙、请求头这些、测的哪个模型。"""
    return health.fingerprint(
        "provider", provider.kind, (provider.base_url or "").strip(), _resolve_key(provider) or "",
        _user_extra(provider.extra), model or provider.default_model or "",
    )


def _set_last_check(row: Provider, last: dict[str, Any] | None) -> None:
    row.extra = {**_user_extra(row.extra), **({LAST_CHECK: last} if last else {})}


def _to_out(row: Provider) -> ProviderOut:
    extra = _user_extra(row.extra)
    return ProviderOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        base_url=row.base_url,
        default_model=row.default_model,
        models=row.models or [],
        enabled=row.enabled,
        extra={k: v for k, v in extra.items() if k != "headers"} | (
            {"headers": {k: "***" for k in extra.get("headers", {})}}
            if extra.get("headers")
            else {}
        ),
        api_key_masked=mask(row.api_key),
        has_key=bool(row.api_key),
        **health.fields((row.extra or {}).get(LAST_CHECK)),
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
        extra=_user_extra(payload.extra),
    )
    # 表单里刚测过这份配置：保存下来就带着那次结果
    _set_last_check(row, health.draft_result(_connection(row)))
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
    before, last = _connection(row), (row.extra or {}).get(LAST_CHECK)
    if "api_key" in data:
        row.api_key = encrypt(data.pop("api_key")) if data["api_key"] else None
        data.pop("api_key", None)
    if data.get("extra") is not None:
        data["extra"] = _with_saved_headers(_user_extra(data["extra"]), row.extra)
    else:
        data.pop("extra", None)
    for key, value in data.items():
        setattr(row, key, value)
    after = _connection(row)
    if after != before:
        # 测过的已经不是这份配置了：旧结果作废，表单里测过这一份的话换成那一次
        last = health.draft_result(after)
    _set_last_check(row, last)
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
    result = await _try_model(row, payload.model, payload.prompt)
    # 只记默认模型的结果：拿别的模型测出 invalid_model，不等于这个接入连不上
    if (payload.model or row.default_model) == row.default_model:
        _set_last_check(row, _test_record(result))
        await session.commit()
    return result


def _test_record(result: dict[str, Any]) -> dict[str, Any]:
    return health.record(result.get("ok"), result.get("latency_ms"), result.get("error"))


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
    extra = _user_extra(payload.extra)
    if saved:
        extra = _with_saved_headers(extra, saved.extra)
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
    saved = await session.get(Provider, payload.id) if payload.id else None
    draft = _draft_provider(payload, saved)
    result = await _try_model(draft, payload.model, payload.prompt)
    last, key = _test_record(result), _connection(draft, payload.model)
    health.remember_draft(key, last)
    if saved is not None and key == _connection(saved):
        # 编辑框里没改连接、只是再测一次：测的就是已保存的那份
        _set_last_check(saved, last)
        await session.commit()
    return result


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
        # 「始终允许 · 门控把关」的工具每次调用前问的那个模型（tools/trust.py）。
        # 空着就用默认 provider 的默认模型
        "tool_gate_provider": None,
        "tool_gate_model": None,
        # agent 护栏（engine/guards.py）：兜底步数、每个节点的令牌 / 金额预算。预算为 null 表示不限
        "agent_max_steps": 100,
        "agent_budget_tokens": 2_000_000,
        "agent_budget_usd": None,
    },
    "limits": {
        "max_graph_steps": app_settings.max_graph_steps,
        "max_agent_steps": app_settings.max_agent_steps,
        "max_run_seconds": app_settings.max_run_seconds,
    },
    # 结论句裁判（engine/judge.py）：证据裁判模型和各项上限，上限存 null 表示不限。不放进 limits：
    # 那一组是环境变量定的、只读，这一组是用户自己定的
    JUDGE_GROUP: dict(JUDGE_DEFAULTS),
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
    # limits 是服务端配置（环境变量），只读：库里可能存着旧版设置页写进去的一份
    #（见过 max_agent_steps: 25），它从来不生效，却会盖住真实的上限，界面照着错的数许诺
    stored.pop("limits", None)
    # 裁判的每日计数是引擎记的账，不是用户设置：设置页整组回写时带着一份旧的，会把今天的花费清零
    stored.pop(JUDGE_SPEND, None)
    merged = {k: {**v, **(stored.get(k) or {})} for k, v in DEFAULT_SETTINGS.items()}
    # 裁判设置按引擎实际生效的样子给：存坏了的项显示默认，存了 null 的上限显示不限
    merged[JUDGE_GROUP] = judge_values(stored.get(JUDGE_GROUP))
    for key, value in stored.items():
        merged.setdefault(key, value)
    return merged


class SettingsIn(BaseModel):
    values: dict[str, Any]


@router.put("/settings")
async def put_settings(
    payload: SettingsIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    from app.tools.trust import SETTING_KEY as TRUST_KEY

    if JUDGE_GROUP in payload.values and (problem := judge_problem(payload.values[JUDGE_GROUP])):
        # 先查后写：一组里有一项不对就整个请求不落库，免得存下半组
        raise HTTPException(422, problem)
    for key, value in payload.values.items():
        if key in (TRUST_KEY, "limits", JUDGE_SPEND):
            # 信任三档只走 PUT /api/tools/trust：设置页整组回写时带着一份旧的，
            # 会把刚在工具页改的档位冲掉。limits 是环境变量定的，写进库也不生效。
            # 裁判的每日计数是引擎记的账，只有裁判自己写
            continue
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    return await get_settings(session)


@router.get("/settings/judge/spend")
async def get_judge_spend(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """今天（本地日期）结论句裁判花了多少，设置页放在每日上限旁边。只读：计数只有裁判自己记。

    {date, usd, calls, unpriced_calls, daily_max_usd}。unpriced_calls 是价格目录里没有的模型的调用次数，
    这些调用估不出金额、没有算进 usd。daily_max_usd 为 null 表示不限。
    """
    return {**await daily_spend(), "daily_max_usd": (await judge_settings(session))["daily_max_usd"]}


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
