"""按需求挑选：表、能力、工作流版本共用同一份挑选提示词（L3），每种对象只给自己的目录。

挑选是一次分类：同一个助手模型，关掉思考、温度 0，PICK_TIMEOUT_S 时限。只在主模型写了 look、只给了需求、
候选又超过 PICK_LIMIT 时才调；失败（拿不到模型、超时、报错、被截断、回复认不出）不挡生成，由查看服务交回目录、让主模型
点名。没成的原因照实分开说：到了额度上限被截断、回复是空的、写了字却不是约定的格式，不混成一句「格式不对」。

演示模型挑不了：pick_model 照实交回原因，不拿它乱挑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any

from app.look.base import PICK_LIMIT, PICK_TIMEOUT_S
from app.providers.budget import BudgetFallback, budget_refused, cut_off, cut_words, truncated_error
from app.providers.factory import ModelSpec, get_chat_model

logger = logging.getLogger(__name__)

#: 挑选调用的输出额度。挑出的十几个名字本身用不了几百 token，可经 OpenAI 兼容接入的推理模型（DeepSeek、GLM、Kimi 这一类）
#: 收不到 thinking=off，思考也算在额度里：原来的 1024 常常想完就没额度写结果，回复是空的或 JSON 只写了一半，被当成
#: 「不是约定的格式」，每次都把目录交回主模型。和证据裁判、核查（engine/judge.MAX_TOKENS）同一档
PICK_MAX_TOKENS = 16384
#: 服务方嫌 PICK_MAX_TOKENS 太大（400 / 422，或 anthropic SDK 在本地就拒了）时退回这个。被嫌大过的按 (接入, 模型, 额度)
#: 记在进程里（providers/budget.BudgetFallback），和裁判、核查的记法相同、互相认
PICK_FALLBACK_MAX_TOKENS = 4096
#: 从正文里找 JSON 时最多试几个起点（每个 { 或 [ 是一个）
_MAX_STARTS = 200
#: 有的网关把思考写在正文里，包在 <think> 里：先去掉，不然思考里打的草稿会被当成结果
_THINK = re.compile(r"<think>.*?</think>", re.S)
#: 挑选没成的原因（look 事件的 reason，也交回给模型）
REASON_FORMAT = "模型的回复不是约定的格式，无法识别挑出的对象"
REASON_EMPTY = "模型没有写出挑选结果（回复是空的）"

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
    演示模型挑不了，直接按挑选失败处理。额度按 PICK_MAX_TOKENS，服务方嫌大时退回 PICK_FALLBACK_MAX_TOKENS（要退回时才建）。
    """
    from app.core.errors import explain
    from app.db.base import SessionLocal
    from app.providers.mock_model import MockChatModel

    spec = spec.model_copy(update={"thinking": "off", "max_tokens": PICK_MAX_TOKENS, "temperature": 0,
                                   "timeout": math.ceil(PICK_TIMEOUT_S)})
    try:
        async with SessionLocal() as session:
            model, model_id = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 拿不到模型只是挑不了，生成照常
        return None, f"助手使用的模型不可用：{explain(e)[0]}"
    if isinstance(model, MockChatModel):
        return None, "演示模型不能按需求挑选"

    async def build() -> Any:
        async with SessionLocal() as session:
            return (await get_chat_model(session, spec.model_copy(update={"max_tokens": PICK_FALLBACK_MAX_TOKENS})))[0]

    return BudgetFallback(model, build=build, limits=(PICK_MAX_TOKENS, PICK_FALLBACK_MAX_TOKENS),
                          rejected=budget_refused, key=(spec.provider, model_id, PICK_MAX_TOKENS)), None


def picked(raw: Any) -> list[tuple[str | None, str]] | None:
    """模型的回复 → [(组名或 None, 名字)]。结构化输出的对象、正文里的 JSON、字符串列表都认；键名认 items / tables、
    name / table（以前挑表时的写法）。对不上格式返回 None。

    正文里的 JSON 前后可以有说明、围栏，前面可以有 <think> 包着的思考：逐个起点试，第一个对得上形状的算数——
    {"items": [...]} 一类的对象（空列表也算，是「和需求无关」），或者至少有一个名字的列表（正文里顺手写的 [1] 不算）。"""
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, str):
        return _items(raw)
    text = _THINK.sub("", raw)
    decoder = json.JSONDecoder()
    starts = 0
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        starts += 1
        if starts > _MAX_STARTS:
            break
        try:
            value, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        got = _items(value)
        if got is not None and (got or isinstance(value, dict)):
            return got
    return None


def _items(raw: Any) -> list[tuple[str | None, str]] | None:
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


def _unpack(out: Any) -> tuple[Any, Any]:
    """with_structured_output(include_raw=True) 交回 {raw, parsed, parsing_error}；测试的假模型直接交回解析好的对象。"""
    if isinstance(out, dict) and "raw" in out and ("parsed" in out or "parsing_error" in out):
        return out.get("raw"), out.get("parsed")
    return None, out


def _in_raw(raw: Any) -> list[Any]:
    """结构化输出没解析出来时，原始回复里还可能有结果的地方：工具调用的参数、正文。"""
    from app.engine.state import message_text

    if raw is None:
        return []
    found: list[Any] = [c.get("args") for c in getattr(raw, "tool_calls", None) or [] if isinstance(c, dict)]
    found += [c.get("args") for c in getattr(raw, "invalid_tool_calls", None) or [] if isinstance(c, dict)]
    return [*found, message_text(raw)]


def _cut(model: Any) -> str:
    return f"{cut_words('按需求挑选的模型', getattr(model, 'max_tokens', None))}，没写完挑选结果"


async def _ask(model: Any, messages: list[tuple[str, str]]) -> list[tuple[str | None, str]]:
    """问一次。先走结构化输出（带原始回复）：解析出来的不对，再从原始回复的工具调用参数、正文里找；不支持结构化输出
    （有的网关不支持工具调用，一调就报错，纯文本却能用，同 catalog._ask_model）或都没找到，再退回纯文本、从正文里找。

    到了额度上限被截断的照实说是截断（同证据裁判 engine/judge._ask）：结构化调用截断的不再退回纯文本——思考占满额度时
    纯文本一样截断，白花一次额度；写了一半的清单也不采信，分不清最后一个名字写完了没有。
    """
    from app.engine.state import message_text

    try:
        raw, parsed = _unpack(await model.with_structured_output(PICK_SCHEMA, include_raw=True).ainvoke(messages))
    except Exception as e:  # noqa: BLE001 - 退回纯文本
        if truncated_error(e):
            raise PickFailed(_cut(model)) from e
    else:
        if cut_off(raw):
            raise PickFailed(_cut(model))
        for found in (parsed, *_in_raw(raw)):
            if (got := picked(found)) is not None:
                return got
    try:
        reply = await model.ainvoke(messages)
    except Exception as e:
        if truncated_error(e):
            raise PickFailed(_cut(model)) from e
        raise
    if cut_off(reply):
        raise PickFailed(_cut(model))
    text = message_text(reply)
    if (got := picked(text)) is not None:
        return got
    logger.warning("按需求挑选：回复认不出（正文 %d 字，开头：%r）", len(text), text[:200])
    raise PickFailed(REASON_FORMAT if text.strip() else REASON_EMPTY)


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
