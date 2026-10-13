"""起草用哪个模型：数据目录的「用模型起草」和按配方导入的 AI 起草共用这一处取值。

顺序（哪一级写了就整组用哪一级，不跨级拼接入和模型）：

1. 设置里的起草模型（catalog_draft 一组：{provider, model}），填了接入或模型就用它；
2. 否则沿用助手的模型设置（copilot 一组，「编排」页助手栏的「助手使用的模型」）；
3. 再没有就是默认接入（providers/factory.resolve_provider 的回退）。

这里只决定「用哪个接入、哪个模型」。额度、思考、温度由两边各自定：数据目录起草保留思考、额度 32768
（data/catalog.resolve_draft_model），配方 AI 起草照旧关思考（data/recipe_ai.resolve_draft_model）。

**为什么不用 get_chat_model。** 它在没有接入时静默退回演示模型，拿演示模型起草出来的东西会被当成真的。
两边都自己解析接入：没有、停用、只有演示接入、缺 key，都在调用模型之前判定为不可用，说清是哪一级的设置出了问题。

数据层不导入接口层：设置键在这里定义，api/settings.py 从这里取（助手那一项和 api/copilot.py 同值，有测试核对）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: 设置里起草模型那一组的键
CATALOG_DRAFT_SETTING_KEY = "catalog_draft"
#: 助手的模型设置键（与 api/copilot.py 的 COPILOT_SETTING_KEY 同值）
COPILOT_SETTING_KEY = "copilot"
#: 起草模型在设置页上的位置。报错里让人去这里改；和前端设置页放这一组的地方一致
DRAFT_MODEL_PLACE = "「设置 → 偏好设置」的「起草模型」"

#: 取自哪一级：draft 设置里的起草模型、assistant 助手的模型、default 默认接入
ORIGINS = ("draft", "assistant", "default")


@dataclass(frozen=True)
class DraftModelChoice:
    """起草用的接入和模型（都可以为 None：接入为 None 时按模型名找、再退回默认接入；模型为 None 时用接入的默认模型）。"""

    provider: str | None
    model: str | None
    origin: str


def _text(value: Any) -> str | None:
    """设置里的一项：字符串去掉首尾空白，空串和别的类型（存坏了的）都当没填。"""
    return value.strip() or None if isinstance(value, str) else None


def setting_pair(stored: Any) -> tuple[str | None, str | None]:
    """库里存的一组 → (接入, 模型)，规范化同 _text。"""
    stored = stored if isinstance(stored, dict) else {}
    return _text(stored.get("provider")), _text(stored.get("model"))


async def draft_model_choice(session: AsyncSession) -> DraftModelChoice:
    """按上面的顺序取。只读设置，不查接入在不在（resolve_provider 去查）。"""
    from app.db.models import Setting

    for key, origin in ((CATALOG_DRAFT_SETTING_KEY, "draft"), (COPILOT_SETTING_KEY, "assistant")):
        row = await session.get(Setting, key)
        provider, model = setting_pair(row.value if row is not None else None)
        if provider or model:
            return DraftModelChoice(provider, model, origin)
    return DraftModelChoice(None, None, "default")


def draft_unavailable(e: BaseException, origin: str) -> str:
    """取到的接入用不了（点名的不存在、已停用，缺 key、没有模型……）时给人看的那句话。

    沿用助手的模型（或默认接入）时，和助手取不到模型时同一句（factory.assistant_unavailable）；用的是设置里的起草
    模型时，照实说是起草模型那一项、去设置页哪里改。"""
    from app.core.errors import not_configured
    from app.providers.factory import NAMED_FIX, assistant_unavailable, model_fact

    if origin != "draft":
        return assistant_unavailable(e)
    code = getattr(e, "code", "")
    if code in NAMED_FIX:
        said = f"起草模型使用的{getattr(e, 'reason', '') or e}。"
        if owner := getattr(e, "model_owner", ""):
            return (f"{said}{NAMED_FIX[code]}；{model_fact(getattr(e, 'name', ''), owner)}，"
                    f"也可在{DRAFT_MODEL_PLACE}中把它填进模型一栏")
        return f"{said}{NAMED_FIX[code]}，或在{DRAFT_MODEL_PLACE}中重新选择"
    return (f"起草模型尚未配置完成：{not_configured(e, with_hint=False)}。请到「设置 → 模型接入」检查，"
            f"或在{DRAFT_MODEL_PLACE}中重新选择")


class DraftModelUnavailable(RuntimeError):
    """没有可用于起草的模型。message 给人看；在调用模型之前判定。"""


async def resolve_draft_provider(session: AsyncSession, *, none_reason: str, mock_reason: str) -> tuple[Any, DraftModelChoice]:
    """(接入, 取值)。用不了时抛 DraftModelUnavailable：没有任何启用的接入说 none_reason，只有演示接入说 mock_reason
    （两边各有各的说法），点名的接入不存在、已停用按取自哪一级说（draft_unavailable）。

    只解析接入，不建模型对象：建的时候缺 key 之类的问题由调用方经 draft_unavailable 说。"""
    from app.providers.factory import ProviderNotConfigured, resolve_provider

    choice = await draft_model_choice(session)
    try:
        # 存的接入名不存在、已停用，只存了模型名、提供它的接入已停用：resolve_provider 都会报出来
        provider = await resolve_provider(session, choice.provider, choice.model)
    except ProviderNotConfigured as e:
        raise DraftModelUnavailable(draft_unavailable(e, choice.origin)) from e
    if provider is None:
        raise DraftModelUnavailable(none_reason)
    if provider.kind == "mock":
        raise DraftModelUnavailable(mock_reason)
    return provider, choice


__all__ = ["CATALOG_DRAFT_SETTING_KEY", "COPILOT_SETTING_KEY", "DRAFT_MODEL_PLACE", "DraftModelChoice",
           "DraftModelUnavailable", "draft_model_choice", "draft_unavailable", "resolve_draft_provider",
           "setting_pair"]
