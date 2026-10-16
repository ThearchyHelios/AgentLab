from __future__ import annotations

import math
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health
from app.api.coded import PUBLIC_URL_ENV_LOCKED, CodedHTTPException
from app.core import public_url
from app.core.errors import explain, not_configured, raw
from app.core.config import settings as app_settings
from app.core.crypto import encrypt, mask
from app.data.draft_model import CATALOG_DRAFT_SETTING_KEY, setting_pair
from app.db.base import get_session
from app.db.models import Provider, Setting
from app.engine.judge import JUDGE_DEFAULTS, MAX_TOKENS as JUDGE_MAX_TOKENS
from app.engine.judge import SETTING_GROUP as JUDGE_GROUP, SETTING_LABEL as JUDGE_LABEL, SPEND_KEY as JUDGE_SPEND
from app.engine.judge import daily_spend, judge_model_spec, judge_settings
from app.engine.judge import sanitize_settings as judge_values, settings_problem as judge_problem
from app.engine.spend import SPEND_KEY as VERIFY_SPEND, daily_spend as verify_daily_spend
from app.providers import catalog
from app.providers.factory import (
    ModelSpec, ProviderNotConfigured, _resolve_key, assistant_unavailable, build_chat_model,
    list_providers as all_providers, model_note, named_provider,
)

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
        raise HTTPException(409, f"已存在名为「{payload.name}」的模型接入，请换一个名称")

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
        raise HTTPException(404, "模型接入不存在，可能已被删除")

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
        raise HTTPException(404, "模型接入不存在，可能已被删除")
    await session.delete(row)
    await session.commit()


class TestIn(BaseModel):
    model: str | None = None
    prompt: str = "用一句话介绍你自己"


def _config_problem(kind: str, base_url: str | None) -> str | None:
    """保存或测试前就能看出来的配置错误。"""
    if kind not in {k["kind"] for k in catalog.PROVIDER_KINDS}:
        return f"不支持「{kind}」接入类型"
    if kind == "openai_compatible" and not (base_url or "").strip():
        return "「OpenAI 兼容」必须填写 Base URL，例如 https://api.deepseek.com/v1"
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
        return {"ok": False, "error": "没有可测试的模型",
                "hint": "请先在「可选模型」中添加一个，或填写默认模型", "detail": ""}
    try:
        chat = build_chat_model(provider, ModelSpec(model=model, max_tokens=256,
                                                    timeout=_TEST_TIMEOUT))
    except ProviderNotConfigured as e:
        if e.code == "missing_key":
            return {"ok": False, "error": "尚未填写 API Key",
                    "hint": "请填写 API Key 后再测试，也可在服务端的环境变量中配置", "detail": str(e)}
        return {"ok": False, "error": not_configured(e, with_hint=False), "hint": "", "detail": str(e)}

    started = time.perf_counter()
    try:
        async with asyncio.timeout(_TEST_TIMEOUT):
            reply = await chat.ainvoke(prompt)
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {"ok": False, "error": f"测试未通过：{reason}", "hint": hint, "detail": raw(e),
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
        raise HTTPException(404, "模型接入不存在，可能已被删除")
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
        return {"ok": False, "error": f"未能获取模型列表：{reason}", "hint": hint,
                "detail": raw(e), "models": [], "url": url}
    models = [m.get("id", "") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "models": models, "url": url}


# --------------------------------------------------------------------------
# 核查：双盲复算、反方审稿、审批质询共用的一组设置
# --------------------------------------------------------------------------

#: 设置里 verify 这一组。三项都是让一个只读 agent 查库核对（engine/verify_agent.py），共用核查模型和每日金额上限；
#: 各自的开关和上限分开放。上限写 null 表示不限（和裁判一组的约定一样）
VERIFY_GROUP = "verify"
VERIFY_DEFAULTS: dict[str, Any] = {
    "provider": None,             # 核查模型：空着沿用证据裁判模型的取值链（verify_model_spec）
    "model": None,
    # 双盲复算：tolerance 是允许的相对误差，两边的值差在这个比例以内算一致
    "dual_check": {"enabled": True, "tolerance": 1e-6, "max_queries": 6, "timeout_s": 120},
    # 反方审稿：max_claims 是每份报告最多挑几句关键结论来审，查询次数、时长按每句算
    "challenge": {"enabled": True, "max_claims": 5, "max_queries": 4, "timeout_s": 90},
    # 审批质询：查询次数、时长按每次提问算
    "inquiry": {"enabled": True, "max_queries": 6, "timeout_s": 120},
    "daily_max_usd": 2.0,         # 全局每日核查金额上限（按本地日期），和裁判的每日上限分开记账（engine/spend.py）
}
#: 三项各自那一组的键
_VERIFY_PARTS = ("dual_check", "challenge", "inquiry")
_VERIFY_COUNTS = frozenset({"max_queries", "max_claims"})

#: 设置页「核查」一节各项的叫法：报错时写它，不写键名。嵌套的项写成「组.项」
VERIFY_SETTING_LABEL = {
    "provider": "核查模型 · 接入", "model": "核查模型 · 模型",
    "daily_max_usd": "核查每日金额上限（美元）",
    "dual_check": "双盲复算", "dual_check.enabled": "双盲复算 · 启用",
    "dual_check.tolerance": "双盲复算 · 允许的相对误差", "dual_check.max_queries": "双盲复算 · 每项最多查询次数",
    "dual_check.timeout_s": "双盲复算 · 每项时长上限（秒）",
    "challenge": "反方审稿", "challenge.enabled": "反方审稿 · 启用",
    "challenge.max_claims": "反方审稿 · 每份报告最多审几句", "challenge.max_queries": "反方审稿 · 每句最多查询次数",
    "challenge.timeout_s": "反方审稿 · 每句时长上限（秒）",
    "inquiry": "审批质询", "inquiry.enabled": "审批质询 · 启用",
    "inquiry.max_queries": "审批质询 · 每次最多查询次数", "inquiry.timeout_s": "审批质询 · 每次时长上限（秒）",
}


def _positive(value: Any, *, whole: bool) -> bool:
    """上限：null（不限），或大于 0 的有限数；whole 时还得是整数（次数、句数）。True / False 不算数。"""
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return False
    return not (whole and int(value) != value)


def _tolerance_ok(value: Any) -> bool:
    """允许的相对误差：不小于 0 的有限数（0 表示必须完全一致）。不能是 null：没有「不限」的说法。"""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _verify_item_ok(key: str, value: Any) -> bool:
    if key == "enabled":
        return isinstance(value, bool)
    if key == "tolerance":
        return _tolerance_ok(value)
    return _positive(value, whole=key in _VERIFY_COUNTS)


def _verify_item_problem(part: str, key: str, value: Any) -> str | None:
    label = VERIFY_SETTING_LABEL[f"{part}.{key}"]
    if _verify_item_ok(key, value):
        return None
    if key == "enabled":
        return f"「{label}」需要选择开启或关闭，当前为「{value}」"
    if key == "tolerance":
        return f"「{label}」需要填写不小于 0 的数（例如 0.000001 表示百万分之一），当前为「{value}」"
    words = "正整数（次数）" if key == "max_queries" else "正整数（句数）" if key == "max_claims" else "大于 0 的秒数"
    return f"「{label}」需要填写{words}，留空表示不限，当前为「{value}」"


def verify_problem(value: Any) -> str | None:
    """设置页写进来的 verify 这一组有什么问题，没问题返回 None。说明写设置页上的叫法。"""
    if not isinstance(value, dict):
        return f"「核查」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in VERIFY_DEFAULTS:
            return (f"「核查」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{VERIFY_SETTING_LABEL[k]}」" for k in VERIFY_DEFAULTS))
        label = VERIFY_SETTING_LABEL[key]
        if key in ("provider", "model"):
            if item is not None and not isinstance(item, str):
                # 「跟随证据裁判模型」是设置页接入下拉框空选项的原话
                return f"「{label}」需要填写模型接入的名称或模型 ID，留空表示跟随证据裁判模型，当前为「{item}」"
        elif key == "daily_max_usd":
            if not _positive(item, whole=False):
                return f"「{label}」需要填写大于 0 的金额（美元），留空表示不限，当前为「{item}」"
        elif not isinstance(item, dict):
            return f"「{label}」设置格式有误，需要是一个对象，当前为「{item}」"
        else:
            allowed = VERIFY_DEFAULTS[key]
            for sub, sub_value in item.items():
                if sub not in allowed:
                    return (f"「{label}」设置中不支持「{sub}」，可设置的项："
                            + "、".join(f"「{VERIFY_SETTING_LABEL[f'{key}.{k}']}」" for k in allowed))
                if problem := _verify_item_problem(key, sub, sub_value):
                    return problem
    return None


def verify_values(stored: Any) -> dict[str, Any]:
    """库里存的 verify 一组 → 补齐默认的完整设置。存坏了的项（旧版本、手改）按默认，存了 null 的上限就是不限；
    三项各自那一组逐项补，只存了一项的其余照默认。"""
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in VERIFY_DEFAULTS.items()}
    for key in ("provider", "model"):
        value = stored.get(key)
        out[key] = value.strip() or None if isinstance(value, str) else None
    if "daily_max_usd" in stored and _positive(stored["daily_max_usd"], whole=False):
        out["daily_max_usd"] = stored["daily_max_usd"]
    for part in _VERIFY_PARTS:
        saved = stored.get(part)
        if not isinstance(saved, dict):
            continue
        for key in VERIFY_DEFAULTS[part]:
            if key in saved and _verify_item_ok(key, saved[key]):
                out[part][key] = saved[key]
    return out


async def verify_settings(session: AsyncSession) -> dict[str, Any]:
    """设置里的 verify 一组，没存过的键补默认（读法同 judge_settings）。节点、质询接口都从这里读。"""
    row = await session.get(Setting, VERIFY_GROUP)
    return verify_values(row.value if row else None)


def _model_text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


async def verify_model_spec(session: AsyncSession, node_config: dict[str, Any] | None = None, *,
                            settings: dict[str, Any] | None = None) -> ModelSpec:
    """核查用哪个模型：节点配置里的 provider / model > 设置里的核查模型 > 证据裁判模型的取值链（裁判设置 >
    助手模型 > 默认接入，见 judge.judge_model_spec）。哪一级写了就整组用哪一级，不跨级拼接入和模型。

    和裁判一样关掉 thinking：核查最后要按结构交结果（强制工具调用），和思考模式不兼容。额度也和裁判一样（judge.MAX_TOKENS，
    16384）：经 OpenAI 兼容接入的推理模型收不到 thinking=off，思考算在额度里，4096 常常想完就没额度查库、交结果了；
    服务方嫌大时核查 agent 退回 judge.FALLBACK_MAX_TOKENS（engine/verify_agent.py）。
    建议和被核查的模型不同，否则等于自己核自己；比较、提醒由调用方做（它知道被核查的是哪个模型）。
    """
    node = node_config if isinstance(node_config, dict) else {}
    provider, model = _model_text(node.get("provider")), _model_text(node.get("model"))
    if not (provider or model):
        chosen = settings if settings is not None else await verify_settings(session)
        provider, model = _model_text(chosen.get("provider")), _model_text(chosen.get("model"))
    if not (provider or model):
        return await judge_model_spec(session, None)
    return ModelSpec(provider=provider, model=model, max_tokens=JUDGE_MAX_TOKENS, thinking="off")


# --------------------------------------------------------------------------
# 起草模型：数据目录的「用模型起草」和按配方导入的 AI 起草用哪个模型
# --------------------------------------------------------------------------

#: 设置里 catalog_draft 这一组：{provider, model}。都留空时沿用助手的模型，再没有就是默认接入（取值顺序见
#: data/draft_model.py，两处起草共用）。只放接入和模型：额度、思考由两处起草各自定
CATALOG_DRAFT_GROUP = CATALOG_DRAFT_SETTING_KEY
CATALOG_DRAFT_DEFAULTS: dict[str, Any] = {"provider": None, "model": None}
#: 设置页「起草模型」一节各项的叫法：报错时写它，不写键名
CATALOG_DRAFT_LABEL = {"provider": "起草模型 · 接入", "model": "起草模型 · 模型"}


def catalog_draft_problem(value: Any) -> str | None:
    """设置页写进来的 catalog_draft 这一组有什么问题，没问题返回 None。点名的接入在不在由 _provider_problem 查。"""
    if not isinstance(value, dict):
        return f"「起草模型」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in CATALOG_DRAFT_DEFAULTS:
            return (f"「起草模型」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{CATALOG_DRAFT_LABEL[k]}」" for k in CATALOG_DRAFT_DEFAULTS))
        if item is not None and not isinstance(item, str):
            # 「跟随助手的模型」是设置页接入下拉框空选项的原话
            what = "模型接入的名称" if key == "provider" else "模型 ID"
            return f"「{CATALOG_DRAFT_LABEL[key]}」需要填写{what}，留空表示跟随助手的模型，当前为「{item}」"
    return None


def catalog_draft_values(stored: Any) -> dict[str, Any]:
    """库里存的 catalog_draft 一组 → 补齐默认、规范化（去掉首尾空白，空串和存坏了的当没填）。"""
    provider, model = setting_pair(stored)
    return {"provider": provider, "model": model}


# --------------------------------------------------------------------------
# 对外访问地址：通知、Webhook 里的链接，单点登录的回调，来源核对（core/public_url.py）
# --------------------------------------------------------------------------

#: 设置里 notify 这一组。public_base_url 是别人打开 AgentLab 用的地址（https://agentlab.example.com），通知里的
#: 运行链接 = 它 + /runs/{运行 id}。服务端不知道自己在反向代理后面叫什么，只能由人填；留空的通知里不附链接。
#: 全站只认一个对外地址（core/public_url.py）：环境变量 AGENTLAB_PUBLIC_URL 设了的话这一项不生效、不能改（PUT 回 409）。
#: 键名沿用 notify 一组，界面上在「设置 → 通用」
NOTIFY_GROUP = public_url.SETTING_GROUP
NOTIFY_DEFAULTS: dict[str, Any] = {public_url.SETTING_KEY: None}
NOTIFY_SETTING_LABEL = {public_url.SETTING_KEY: "对外访问地址"}
#: GET /api/settings 另给的只读一组：{value: 生效的对外访问地址, env_locked: 是不是环境变量指定的}。设置页据此显示
#: 「由环境变量指定」；PUT 带上它也不落库（同 limits）
PUBLIC_URL_GROUP = "public_url"


def _base_url_problem(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    label = NOTIFY_SETTING_LABEL[public_url.SETTING_KEY]
    hint = (f"「{label}」需要是 http:// 或 https:// 开头的完整地址（例如 https://agentlab.example.com），"
            f"留空表示通知里不附链接，当前为「{value}」")
    if not isinstance(value, str):
        return hint
    if (problem := public_url.url_problem(value)) == "suffix":
        return f"「{label}」不能带 ? 或 # 之后的部分，链接会接在地址后面，当前为「{value}」"
    return hint if problem else None


def notify_problem(value: Any) -> str | None:
    """设置页写进来的 notify 这一组有什么问题，没问题返回 None。"""
    if not isinstance(value, dict):
        return f"「通知」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in NOTIFY_DEFAULTS:
            return (f"「通知」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{NOTIFY_SETTING_LABEL[k]}」" for k in NOTIFY_DEFAULTS))
        if problem := _base_url_problem(item):
            return problem
    return None


def notify_values(stored: Any) -> dict[str, Any]:
    """库里存的（或刚写进来的）notify 一组 → 补齐默认、规范化：去掉首尾空白和末尾的 /，空串当没填；存坏了的按没填。"""
    stored = stored if isinstance(stored, dict) else {}
    return {public_url.SETTING_KEY: public_url.normalize(stored.get(public_url.SETTING_KEY))}


async def notify_settings(session: AsyncSession) -> dict[str, Any]:
    """设置里存的 notify 一组，没存过的补默认。要拼链接的地方不读它：一律经 core/public_url（环境变量优先）。"""
    row = await session.get(Setting, NOTIFY_GROUP)
    return notify_values(row.value if row else None)


def public_url_values(stored_notify: Any) -> dict[str, Any]:
    """GET /api/settings 里只读的 public_url 一组：生效的那个地址（环境变量优先，其次库里存的这一项），是不是环境变量
    指定的。按库里存的算，不读进程缓存：设置页看到的就是落库的样子。"""
    locked = public_url.env_locked()
    value = public_url.env_url() if locked else notify_values(stored_notify)[public_url.SETTING_KEY]
    return {"value": value, "env_locked": locked}


# --------------------------------------------------------------------------
# 应用：按需视图的每日上限（D-j）、使用者能不能建个人应用（docs/design/normal-user-apps.md N7）
# --------------------------------------------------------------------------

#: 设置里 apps 这一组。on_demand_daily_limit：每个应用每天最多发起几次按需视图的正式运行（app/apps/resolve.py 的
#: daily_limit 读它），点「刷新」才运行、同一个键只运行一份之外，再兜一道花费的底。0 表示不再发起按需运行；个人应用的刷新
#: 共用这一项，按人计（blueprints/personal.py）。
#: user_personal_apps：开了登录时使用者能不能建个人应用、刷新、提交审核（默认关，同问数据的开关；搭建者及以上不受它管）。
#: 关登录时它照样生效：关着就没有个人应用的入口。只有管理员能改（整个设置接口的改动都要管理员）
APPS_GROUP = "apps"
APPS_DEFAULTS: dict[str, Any] = {"on_demand_daily_limit": 200, "user_personal_apps": False}
APPS_SETTING_LABEL = {"on_demand_daily_limit": "按需视图每日上限", "user_personal_apps": "使用者可以建个人应用"}
#: 上限的上限：再大就等于不限，要不限的不该靠这一项
APPS_DAILY_LIMIT_MAX = 100_000


def _daily_limit_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= APPS_DAILY_LIMIT_MAX


def apps_problem(value: Any) -> str | None:
    """设置页写进来的 apps 这一组有什么问题，没问题返回 None。"""
    if not isinstance(value, dict):
        return f"「应用」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in APPS_DEFAULTS:
            return (f"「应用」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{APPS_SETTING_LABEL[k]}」" for k in APPS_DEFAULTS))
        if key == "user_personal_apps":
            if not isinstance(item, bool):
                return f"「{APPS_SETTING_LABEL[key]}」只能是开或关，当前为「{item}」"
        elif not _daily_limit_ok(item):
            return (f"「{APPS_SETTING_LABEL[key]}」需要是 0 到 {APPS_DAILY_LIMIT_MAX:,} 之间的整数（0 表示不再发起按需运行），"
                    f"当前为「{item}」")
    return None


def apps_values(stored: Any) -> dict[str, Any]:
    """库里存的（或刚写进来的）apps 一组 → 补齐默认；存坏了的项按默认（个人应用的开关存坏了按关）。"""
    stored = stored if isinstance(stored, dict) else {}
    limit = stored.get("on_demand_daily_limit")
    personal = stored.get("user_personal_apps")
    return {"on_demand_daily_limit": limit if _daily_limit_ok(limit) else APPS_DEFAULTS["on_demand_daily_limit"],
            "user_personal_apps": personal if isinstance(personal, bool) else APPS_DEFAULTS["user_personal_apps"]}


async def apps_settings(session: AsyncSession) -> dict[str, Any]:
    """设置里的 apps 一组，没存过的补默认。按需视图发起前从这里读每日上限。"""
    row = await session.get(Setting, APPS_GROUP)
    return apps_values(row.value if row else None)


# --------------------------------------------------------------------------
# 评测：并发、每题时限、单次和每日金额上限、保留几次、结论裁判的缺省（docs/design/next-round/01-效果度量与回归评测.md 做法 17）
# --------------------------------------------------------------------------

#: 设置里 eval 这一组（评测-D9 拍板的缺省：同时 2 题、每题 900 秒、单次评测 2 美元、评测每日 5 美元）。前四项是全站的预算
#: 缺省，题集上改过的项盖过它（app/eval/suites.effective_budget）；评测每日的金额上限是评测那本账（engine/spend.eval_spend，
#: 单独记账，不挤占裁判、核查的额度）的上限，题集只能往下收。keep_runs：每个题集保留最近几次评测的明细（钉住的基线、A/B
#: 引用的那一对另外留着，总表永久保留，app/eval/cleanup.py）。judge：发起评测时变体里没写的话，结论裁判开不开（缺省关：
#: 裁判按句数花钱）。搭建者只读，管理员写（整个设置接口的规矩）
EVAL_GROUP = "eval"
EVAL_SPEND = "eval_spend"
EVAL_DEFAULTS: dict[str, Any] = {"max_parallel": 2, "per_case_timeout_s": 900, "per_run_max_usd": 2.0,
                                 "daily_max_usd": 5.0, "keep_runs": 20, "judge": False}
EVAL_SETTING_LABEL = {"max_parallel": "评测 · 同时评几道题", "per_case_timeout_s": "评测 · 每题时限（秒）",
                      "per_run_max_usd": "评测 · 单次评测的金额上限（美元）", "daily_max_usd": "评测 · 每日金额上限（美元）",
                      "keep_runs": "评测 · 每个题集保留最近几次的明细", "judge": "评测 · 缺省打开结论裁判"}
#: 保留的次数：至少 1（最近一次总要能和下一次比），最多 1000
EVAL_KEEP_MAX = 1000


def _eval_item_problem(key: str, item: Any) -> str | None:
    from app.eval.suites import BUDGET_RULES

    label = EVAL_SETTING_LABEL[key]
    if key == "judge":
        return None if isinstance(item, bool) else f"「{label}」只能是开或关，当前为「{item}」"
    if key == "keep_runs":
        ok = isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= EVAL_KEEP_MAX
        return None if ok else f"「{label}」需要是 1 到 {EVAL_KEEP_MAX} 之间的整数，当前为「{item}」"
    _, kind, low, high = BUDGET_RULES[key]
    ok = not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(item) \
        and (kind is float or isinstance(item, int)) and low <= item <= high
    unit = "整数" if kind is int else "数"
    return None if ok else f"「{label}」需要是 {low:g} 到 {high:g} 之间的{unit}，当前为「{item}」"


def eval_problem(value: Any) -> str | None:
    """设置页写进来的 eval 这一组有什么问题，没问题返回 None。"""
    if not isinstance(value, dict):
        return f"「评测」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key not in EVAL_DEFAULTS:
            return (f"「评测」设置中不支持「{key}」，可设置的项："
                    + "、".join(f"「{EVAL_SETTING_LABEL[k]}」" for k in EVAL_DEFAULTS))
        if problem := _eval_item_problem(key, item):
            return problem
    merged = eval_values(value)
    if merged["per_run_max_usd"] > merged["daily_max_usd"]:
        return (f"「{EVAL_SETTING_LABEL['per_run_max_usd']}」不能高于「{EVAL_SETTING_LABEL['daily_max_usd']}」，"
                f"当前为 {merged['per_run_max_usd']:g} 和 {merged['daily_max_usd']:g}")
    return None


def eval_values(stored: Any) -> dict[str, Any]:
    """库里存的（或刚写进来的）eval 一组 → 补齐默认；存坏了的项按默认。"""
    stored = stored if isinstance(stored, dict) else {}
    out = dict(EVAL_DEFAULTS)
    for key in EVAL_DEFAULTS:
        if key in stored and _eval_item_problem(key, stored[key]) is None:
            value = stored[key]
            out[key] = float(value) if key in ("per_run_max_usd", "daily_max_usd") else value
    return out


async def eval_settings(session: AsyncSession) -> dict[str, Any]:
    """设置里的 eval 一组，没存过的补默认。发起评测、清理评测从这里读。"""
    row = await session.get(Setting, EVAL_GROUP)
    return eval_values(row.value if row else None)


# --------------------------------------------------------------------------
# 用户设置
# --------------------------------------------------------------------------

#: 不经 /api/settings 读写的组：登录设置只经 /api/auth/settings 改（D-l）——那一组里有 OIDC 的客户端密钥、管理员映射，
#: 而且改登录方式要在同一个请求里引导第一个管理员、刷新进程内的缓存；从这里写进去的那份既不校验也不生效，读出来的又
#: 会把密钥带给搭建者
def _hidden_groups() -> tuple[str, ...]:
    from app.auth import SETTING_KEY as AUTH_KEY

    return (AUTH_KEY,)


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
    # 核查（双盲复算、反方审稿、审批质询）：核查模型、三项的开关和上限、每日金额上限。和裁判一组一样由用户定
    VERIFY_GROUP: verify_values(None),
    # 对外访问地址（存在 notify 一组）：通知、Webhook 里的链接，单点登录的回调，来源核对。环境变量优先（core/public_url.py）
    NOTIFY_GROUP: notify_values(None),
    # 起草模型：数据目录起草、按配方导入的 AI 起草用的模型，留空沿用助手的模型
    CATALOG_DRAFT_GROUP: catalog_draft_values(None),
    # 应用：按需视图的每日上限、使用者能不能建个人应用（管理员在设置页改）
    APPS_GROUP: apps_values(None),
    # 评测：预算的全站缺省（题集上可以改）、保留几次明细、结论裁判的缺省开关
    EVAL_GROUP: eval_values(None),
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
    stored.pop(PUBLIC_URL_GROUP, None)
    # 裁判、核查的每日计数是引擎记的账，不是用户设置：设置页整组回写时带着一份旧的，会把今天的花费清零
    stored.pop(JUDGE_SPEND, None)
    stored.pop(VERIFY_SPEND, None)
    stored.pop(EVAL_SPEND, None)
    for key in _hidden_groups():
        stored.pop(key, None)
    merged = {k: {**v, **(stored.get(k) or {})} for k, v in DEFAULT_SETTINGS.items()}
    # 裁判、核查设置按引擎实际生效的样子给：存坏了的项显示默认，存了 null 的上限显示不限
    merged[JUDGE_GROUP] = judge_values(stored.get(JUDGE_GROUP))
    merged[VERIFY_GROUP] = verify_values(stored.get(VERIFY_GROUP))
    merged[NOTIFY_GROUP] = notify_values(stored.get(NOTIFY_GROUP))
    merged[PUBLIC_URL_GROUP] = public_url_values(stored.get(NOTIFY_GROUP))
    merged[CATALOG_DRAFT_GROUP] = catalog_draft_values(stored.get(CATALOG_DRAFT_GROUP))
    merged[APPS_GROUP] = apps_values(stored.get(APPS_GROUP))
    merged[EVAL_GROUP] = eval_values(stored.get(EVAL_GROUP))
    for key, value in stored.items():
        merged.setdefault(key, value)
    return merged


class SettingsIn(BaseModel):
    values: dict[str, Any]


#: 点名的模型接入不存在或已停用时 PUT /api/settings 回 422 的机读码，field 是出错的那一项（verify.provider……）
PROVIDER_INVALID = "settings_provider_invalid"


def _provider_fields() -> dict[tuple[str, str], tuple[str, str | None]]:
    """设置里点名模型接入的项：(组, 键) → (这一项在界面上的叫法, 同一组模型那一项的叫法)。门控、裁判、核查、起草模型的
    叫法和设置页一致；助手的模型由「编排」页助手栏的「助手使用的模型」单独设定（PUT /api/copilot/model），只有这一个下拉框
    （模型那一项为 None），这里只防经设置接口写进去的那一份。"""
    from app.api.copilot import COPILOT_SETTING_KEY   # 在这里导入：copilot 模块很大，设置模块被引擎各处导入
    from app.providers.factory import ASSISTANT_MODEL_FIELD

    return {
        ("run", "tool_gate_provider"): ("门控模型 · 接入", "门控模型 · 模型"),
        (JUDGE_GROUP, "provider"): (JUDGE_LABEL["provider"], JUDGE_LABEL["model"]),
        (VERIFY_GROUP, "provider"): (VERIFY_SETTING_LABEL["provider"], VERIFY_SETTING_LABEL["model"]),
        (CATALOG_DRAFT_GROUP, "provider"): (CATALOG_DRAFT_LABEL["provider"], CATALOG_DRAFT_LABEL["model"]),
        (COPILOT_SETTING_KEY, "provider"): (ASSISTANT_MODEL_FIELD, None),
    }


async def _provider_problem(session: AsyncSession, values: dict[str, Any]) -> tuple[str, str] | None:
    """这次写进来的设置里点名的模型接入有没有问题：(字段, 说明)，没问题返回 None。说明和运行时报的原因、提示同一句
    （factory.named_provider），主语写这一项在设置页上的叫法；下拉框里选的，写「指定的」。助手那一项只有「编排」页
    助手栏一个下拉框（只选模型，选了就清掉接入），没有「换一个接入」的地方：说法同助手取不到模型时那一句
    （factory.assistant_unavailable），哪里报都一样。

    以前点名的接入不存在时运行悄悄换成演示模型，名字写错一个字就由演示模型写报告、做核查，现在运行时照实报错；保存时
    先拦一道。留空照旧表示沿用上一级——说明里不再讲留空是什么意思：设置页那一格的空选项写着（「默认接入」「跟随助手
    的模型」「跟随证据裁判模型」……），这里另写一遍只会对不上。只查这次改了的：存着的值没改（写好之后接入才被删、
    停用）不挡这次保存别的项——设置页在那一项下标着「不存在」「已停用」，运行时照实报错，同设置页「只校验改过的」。
    点名的名字恰好是某个启用接入的模型时，补一句在这一组的哪一栏用它（factory.model_note）。

    名字不是字符串（接口写进来的数字一类）：核查、裁判两组自己的格式检查在前面先报；门控、助手两组没有自己的检查，
    在这里报，免得存下去之后读它的地方（查助手当前用哪个模型）出错。"""
    providers = None
    for (group, key), (label, model_label) in _provider_fields().items():
        sent = values.get(group)
        if not isinstance(sent, dict):
            continue
        if (raw := sent.get(key)) is not None and not isinstance(raw, str):
            return f"{group}.{key}", f"「{label}」需要填写模型接入的名称，当前为「{raw}」"
        if (name := _model_text(raw)) is None:
            continue
        row = await session.get(Setting, group)
        saved = row.value if row is not None and isinstance(row.value, dict) else {}
        if _model_text(saved.get(key)) == name:
            continue
        if providers is None:
            providers = await all_providers(session)
        try:
            named_provider(providers, name)
        except ProviderNotConfigured as e:
            if model_label is None:
                return f"{group}.{key}", assistant_unavailable(e)
            said = f"「{label}」指定的{e.reason}。{e.hint}"
            if e.model_owner:
                # 这几组的模型一栏是输入框：写「填写」
                said += f"。{model_note(e.name, e.model_owner, f'「{model_label}」', f'「{label}」', '填写')}"
            return f"{group}.{key}", said
    return None


@router.put("/settings")
async def put_settings(
    payload: SettingsIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    from app.tools.trust import SETTING_KEY as TRUST_KEY

    # 先查后写：一组里有一项不对就整个请求不落库，免得存下半组
    if JUDGE_GROUP in payload.values and (problem := judge_problem(payload.values[JUDGE_GROUP])):
        raise HTTPException(422, problem)
    if VERIFY_GROUP in payload.values and (problem := verify_problem(payload.values[VERIFY_GROUP])):
        raise HTTPException(422, problem)
    if CATALOG_DRAFT_GROUP in payload.values and (
            problem := catalog_draft_problem(payload.values[CATALOG_DRAFT_GROUP])):
        raise HTTPException(422, problem)
    if NOTIFY_GROUP in payload.values:
        if problem := notify_problem(payload.values[NOTIFY_GROUP]):
            raise HTTPException(422, problem)
        # 存规范化之后的：末尾带 / 的地址拼链接会多出一个斜杠
        payload.values[NOTIFY_GROUP] = notify_values(payload.values[NOTIFY_GROUP])
        if public_url.env_locked() and payload.values[NOTIFY_GROUP] != await notify_settings(session):
            # 环境变量指定了对外访问地址：改这一项不会生效，照实拒绝，不悄悄存一个用不上的值。原样写回（整组回写）不算改
            raise CodedHTTPException(
                409, f"对外访问地址由环境变量 {public_url.ENV_NAME} 指定（{public_url.env_url()}），不能在设置里修改。"
                     "要改请改部署配置后重启服务", PUBLIC_URL_ENV_LOCKED, field=f"{NOTIFY_GROUP}.{public_url.SETTING_KEY}")
    if APPS_GROUP in payload.values:
        if problem := apps_problem(payload.values[APPS_GROUP]):
            raise HTTPException(422, problem)
        payload.values[APPS_GROUP] = apps_values(payload.values[APPS_GROUP])
    if EVAL_GROUP in payload.values:
        if problem := eval_problem(payload.values[EVAL_GROUP]):
            raise HTTPException(422, problem)
        payload.values[EVAL_GROUP] = eval_values(payload.values[EVAL_GROUP])
    if found := await _provider_problem(session, payload.values):
        raise CodedHTTPException(422, found[1], PROVIDER_INVALID, field=found[0])
    for key, value in payload.values.items():
        if key in (TRUST_KEY, "limits", PUBLIC_URL_GROUP, JUDGE_SPEND, VERIFY_SPEND, EVAL_SPEND, *_hidden_groups()):
            # 信任三档只走 PUT /api/tools/trust：设置页整组回写时带着一份旧的，
            # 会把刚在工具页改的档位冲掉。limits 是环境变量定的、public_url 是算出来的，写进库也不生效。
            # 裁判、核查的每日计数是引擎记的账，只有引擎自己写。登录设置只经 /api/auth/settings 改（_hidden_groups）
            continue
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    if NOTIFY_GROUP in payload.values:
        # 中间件、单点登录的回调按进程缓存取对外访问地址：写完库就刷新，不用重启
        public_url.remember(payload.values[NOTIFY_GROUP][public_url.SETTING_KEY])
    return await get_settings(session)


@router.get("/settings/judge/spend")
async def get_judge_spend(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """今天（本地日期）结论句裁判花了多少，设置页放在每日上限旁边。只读：计数只有裁判自己记。

    {date, usd, calls, unpriced_calls, daily_max_usd}。unpriced_calls 是价格目录里没有的模型的调用次数，
    这些调用估不出金额、没有算进 usd。daily_max_usd 为 null 表示不限。
    """
    return {**await daily_spend(), "daily_max_usd": (await judge_settings(session))["daily_max_usd"]}


@router.get("/settings/verify/spend")
async def get_verify_spend(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """今天（本地日期）核查（双盲复算、反方审稿、审批质询）花了多少，设置页放在核查的每日上限旁边。只读。

    形状同 /settings/judge/spend：{date, usd, calls, unpriced_calls, daily_max_usd}，daily_max_usd 为 null 表示不限。
    """
    return {**await verify_daily_spend(), "daily_max_usd": (await verify_settings(session))["daily_max_usd"]}


@router.get("/settings/eval/spend")
async def get_eval_spend(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """今天（本地日期）评测花了多少（评测那本账，engine/spend.eval_spend），放在评测的每日上限旁边。只读：计数只有评测
    自己记。形状同 /settings/judge/spend：{date, usd, calls, unpriced_calls, daily_max_usd}。calls 是今天开评的题数（每题
    开评前预留一次）。"""
    from app.engine.spend import eval_spend

    return {**await eval_spend.daily(), "daily_max_usd": (await eval_settings(session))["daily_max_usd"]}


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
