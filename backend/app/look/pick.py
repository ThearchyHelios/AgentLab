"""按需求挑选：表、能力、工作流版本共用同一份挑选提示词（L3），每种对象只给自己的目录。

挑选是一次分类：同一个助手模型，关掉思考、额度取小、温度 0，PICK_TIMEOUT_S 时限。只在主模型写了 look、只给了需求、
候选又超过 PICK_LIMIT 时才调；失败（拿不到模型、超时、报错、回复认不出）不挡生成，由查看服务交回目录、让主模型点名。

演示模型挑不了：pick_model 照实交回原因，不拿它乱挑。
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from app.look.base import PICK_LIMIT, PICK_TIMEOUT_S
from app.providers.factory import ModelSpec, get_chat_model

#: 挑选调用的输出额度：十几个名字的 JSON 用不了几百 token
PICK_MAX_TOKENS = 1024

PICK_SYSTEM_PROMPT = (
    "你在帮工作流助手挑选它要查看的对象。用户这一轮提了一个需求，助手接下来要据此搭工作流；对象太多，没法每个都把"
    "详情给它看，它只会看到你挑出的那些。请从目录里挑出完成这个需求要用到的。\n\n"
    "挑选规则：\n"
    f"- 最多挑 {PICK_LIMIT} 个，按和需求的相关程度从高到低排\n"
    "- 完成需求要一起用的也挑上：要按渠道、门店、日期之类分组或筛选时，把对应的维度表挑上；两张表要靠一张关联表"
    "才能连起来时，关联表也挑上\n"
    "- 标「当前工作流已用到」的，需求还用得着就挑上\n"
    "- 拿不准要不要的，宁可挑上\n"
    "- name 照抄目录里的名字（含 schema 前缀），不要改写、不要编造；source 写它在目录里所属的那一组的名字\n"
    "- 需求和这份目录无关时返回空列表\n\n"
    '只输出 JSON：{"items": [{"source": "所属的组", "name": "名字"}]}'
)
PICK_HUMAN_PROMPT = "需求：{need}\n\n{used}{what}目录（{line}；行尾标「推断，未确认」的是推断出来的）：\n\n{index}"
PICK_USED_PROMPT = "当前工作流已用到：{items}\n\n"
PICK_SCHEMA = {
    "title": "picked_items",
    "description": "完成需求要用到的对象，按相关程度从高到低排",
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "它在目录里所属的那一组的名字"},
                    "name": {"type": "string", "description": "照抄目录里的名字"},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["items"],
}


class PickFailed(Exception):
    """挑选没成：原因写给人看（look 事件的 reason），也交回给模型。"""


async def pick_model(spec: ModelSpec) -> tuple[Any | None, str | None]:
    """挑选用的模型：(模型, None)，拿不到时 (None, 原因)。spec 是助手的模型规格（copilot_model_spec）。

    在流里拿：请求的会话那时已经不能用了，这里另开一个短会话读接入设置。没配接入时 get_chat_model 静默退回演示模型，
    演示模型挑不了，直接按挑选失败处理。
    """
    from app.core.errors import explain
    from app.db.base import SessionLocal
    from app.providers.mock_model import MockChatModel

    spec = spec.model_copy(update={"thinking": "off", "max_tokens": PICK_MAX_TOKENS, "temperature": 0,
                                   "timeout": math.ceil(PICK_TIMEOUT_S)})
    try:
        async with SessionLocal() as session:
            model, _ = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 拿不到模型只是挑不了，生成照常
        return None, f"助手使用的模型不可用：{explain(e)[0]}"
    if isinstance(model, MockChatModel):
        return None, "演示模型不能按需求挑选"
    return model, None


def picked(raw: Any) -> list[tuple[str | None, str]] | None:
    """模型的回复 → [(组名或 None, 名字)]。结构化输出的对象、正文里的 JSON、字符串列表都认；键名认 items / tables、
    name / table（以前挑表时的写法）。对不上格式返回 None。"""
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, str):
        text = raw.strip()
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        end = text.rfind("}" if text[start] == "{" else "]")
        try:
            raw = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if isinstance(raw, dict):
        raw = raw.get("items", raw.get("tables"))
    if not isinstance(raw, list):
        return None
    out: list[tuple[str | None, str]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append((None, item.strip()))
        elif isinstance(item, dict):
            name = item.get("name", item.get("table"))
            if isinstance(name, str) and name.strip():
                src = item.get("source")
                out.append((src.strip() if isinstance(src, str) and src.strip() else None, name.strip()))
    return out


async def _ask(model: Any, messages: list[tuple[str, str]]) -> list[tuple[str | None, str]]:
    """问一次。先走结构化输出；不支持、报错、或给回来的不是约定的结构，再退回纯文本、从正文里取 JSON。

    结构化输出报错也退回（同 catalog._ask_model）：有的网关不支持工具调用，一调就报错，纯文本却能用。
    """
    from app.engine.state import message_text

    try:
        parsed = picked(await model.with_structured_output(PICK_SCHEMA).ainvoke(messages))
    except Exception:  # noqa: BLE001 - 退回纯文本
        parsed = None
    if parsed is not None:
        return parsed
    parsed = picked(message_text(await model.ainvoke(messages)))
    if parsed is None:
        raise PickFailed("模型的回复不是约定的格式，无法识别挑出的对象")
    return parsed


class Picker:
    """一轮里共用的挑选：模型第一次要用时才拿（多数轮次根本不挑），拿不到的原因记住，不反复拿。"""

    def __init__(self, spec: ModelSpec | None) -> None:
        self.spec = spec
        self._model: Any | None = None
        self._unavailable: str | None = None if spec is not None else "没有可用的模型"
        self.calls = 0

    async def _get(self) -> Any:
        if self._model is None and self._unavailable is None:
            self._model, self._unavailable = await pick_model(self.spec)
        if self._model is None:
            raise PickFailed(self._unavailable or "没有可用的模型")
        return self._model

    async def pick(self, *, need: str, what: str, line: str, index: str,
                   used: str = "") -> list[tuple[str | None, str]]:
        """按需求从目录里挑。what 是对象的类别名（数据表、能力……），line 说目录一行写的是什么。失败抛 PickFailed。"""
        model = await self._get()
        human = PICK_HUMAN_PROMPT.format(need=need.strip(), used=PICK_USED_PROMPT.format(items=used) if used else "",
                                         what=what, line=line, index=index)
        self.calls += 1
        try:
            return await asyncio.wait_for(_ask(model, [("system", PICK_SYSTEM_PROMPT), ("human", human)]),
                                          timeout=PICK_TIMEOUT_S)
        except PickFailed:
            raise
        except asyncio.TimeoutError as e:
            raise PickFailed(f"按需求挑选超过 {PICK_TIMEOUT_S:g} 秒未完成") from e
        except Exception as e:  # noqa: BLE001 - 挑选失败不挡生成
            from app.core.errors import explain

            raise PickFailed(f"按需求挑选的模型调用失败：{explain(e)[0]}") from e
