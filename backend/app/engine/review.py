from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.events import EventType
from app.engine.issuance import extract_numbers, trace_numbers

# --------------------------------------------------------------------------
# 跑完之后的复核层。
#
# 编排跑完只说明「流程走完了」，不说明「答得对」。库里能查到大量 status=succeeded
# 的运行，过程中检索降级了、工具报错了、agent 步数用满了，而交到用户手上的答案
# 对此只字不提——最糟的一次（run dd9927e6）交出去的干脆是一句内部管道文案。
#
# 出具体系（issuance.py）本来就是干这个的，但它挂在输出节点的可选契约上，还要配
# 口径卡节点提供受控指标集；问数据页生成的图基本都是 input → agent → output，
# 走不到那条路。所以这里补一层轻的：
#
#   1. scan()   纯规则扫一遍事件流，零成本。干净的运行到此为止，不调模型。
#   2. review() 只在扫出信号时调一次模型，重组答案并把异常说破。
#   3. 复核模型看不到原始数据，所以它写出来的任何新数字都是凭空来的
#      —— check_numbers() 机械拦截，对不上就整条退回原答案。
# --------------------------------------------------------------------------

BROKEN = "broken"      # 答案不可信
DEGRADED = "degraded"  # 能用，但有缺口要说明
BENIGN = "benign"      # 记在轨迹里就够了，不值得为它叫一次模型

_SEVERITY_RANK = {BENIGN: 0, DEGRADED: 1, BROKEN: 2}


@dataclass(frozen=True)
class Signal:
    """一条「这次运行有什么不对」。detail 同时给模型和用户看，所以写人话。"""

    kind: str
    detail: str
    severity: str = DEGRADED

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


# warn 日志按 code 分类，而不是靠匹配中文消息串——消息是给人读的，随时会改，
# 把复核的判断挂在它上面等于给自己埋一颗定时炸弹。各 emit 点带上 code。
#
# 没有 code 的（老运行留在库里的事件）一律按 degraded 处理：宁可多说一句，
# 也好过把真问题当噪音吞掉。
WARN_CODES: dict[str, tuple[str, str]] = {
    # 对答案内容零影响，轨迹里有就够了
    "stream_fallback": ("stream_fallback", BENIGN),
    # 安全属性上的降级，不是答案内容的问题；轨迹里已经说清楚了
    "isolation_fallback": ("isolation_fallback", BENIGN),
    # 答案本身就没写完
    "step_limit": ("step_limit", BROKEN),
    "empty_completion": ("empty_completion", BROKEN),
    # 能用但有缺口
    # 步数用尽，但收尾轮让模型基于已查到的部分收了口：答案是完整的一段话，
    # 只是覆盖面不全——这是缺口不是断裂，所以不判 BROKEN。
    # 连带效果是不再触发自动重跑，那正是想要的：同样配置再跑一遍还撞同一堵墙
    "step_limit_settled": ("step_limit_settled", DEGRADED),
    # 收尾轮自己挂了。影响由同时发出的 step_limit(BROKEN) 承担，这条是说清原因
    "settle_failed": ("settle_failed", DEGRADED),
    "loop_limit": ("loop_limit", DEGRADED),
    "tool_args_fixed": ("tool_args_fixed", DEGRADED),
    "node_retry": ("node_retry", DEGRADED),
    "validate_retry": ("validate_retry", DEGRADED),
    "sandbox_reset": ("sandbox_reset", DEGRADED),
    "retrieve_degraded": ("retrieve_degraded", DEGRADED),
}

# 这些 kind 重跑一次大概率能好：原因说得清，且不是「问题本身没问明白」。
# 工具参数错了、步数不够、图没校验过，都属于改一下再来一遍就行的。
RETRYABLE = {"step_limit", "tool_error", "empty_completion", "node_failed"}

#: 串行工具模式下塞给未执行调用的占位说明。它漏到用户面前当过答案，
#: 所以复核要认得出来。从 nodes.llm 导入会绕成循环，这里按内容识别。
_PLACEHOLDER_MARKS = ("本轮只执行了第一个工具",)


def answer_text(output: Mapping[str, Any] | None) -> str:
    """把 output 各字段拼成答案正文。和前端 answerText 同一套规则。"""
    if not output:
        return ""
    parts = [
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for key, value in output.items()
        if not key.startswith("_") and value not in (None, "")
    ]
    return "\n".join(parts).strip()


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    """事件既可能是 ORM 行也可能是 dict，两种都要吃得下。"""
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def scan(events: Iterable[Any], output: Mapping[str, Any] | None = None) -> list[Signal]:
    """纯规则扫一遍运行事件，挑出会影响答案可信度的那些。

    不调模型、不碰网络。返回空列表就意味着这次运行干净，可以直接交付。
    """
    signals: list[Signal] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, detail: str, severity: str) -> None:
        if severity == BENIGN:
            return
        key = (kind, detail)
        if key in seen:
            return
        seen.add(key)
        kinds.add(kind)
        signals.append(Signal(kind, detail, severity))

    kinds: set[str] = set()

    rows = list(events)

    # 工具报错之后，同一个工具在后面又跑成了，说明自愈接住了——那是缺口不是断裂。
    # 不区分的话，参数自动纠正这条路每修一次就报一次「答案不可信」。
    recovered: set[str] = set()
    failed_at: dict[str, int] = {}
    for i, e in enumerate(rows):
        etype = str(_event_field(e, "type", ""))
        data = _event_field(e, "data") or {}
        tool = str(data.get("tool") or "")
        if etype == EventType.TOOL_ERROR and tool:
            failed_at.setdefault(tool, i)
        elif etype == EventType.TOOL_END and tool and tool in failed_at:
            recovered.add(tool)

    for e in rows:
        etype = str(_event_field(e, "type", ""))
        data = _event_field(e, "data") or {}
        node_id = _event_field(e, "node_id") or ""

        if etype == EventType.TOOL_ERROR:
            tool = str(data.get("tool") or "工具")
            err = str(data.get("error") or "").strip()[:200]
            if tool in recovered:
                add("tool_args_fixed", f"工具 {tool} 第一次调用失败（{err}），重试后成功", DEGRADED)
            else:
                add("tool_error", f"工具 {tool} 调用失败：{err}", BROKEN)

        elif etype == EventType.NODE_FAILED:
            err = str(data.get("error") or "").strip()[:200]
            add("node_failed", f"节点 {node_id} 执行失败：{err}", BROKEN)

        elif etype == EventType.RUN_FAILED:
            err = str(data.get("error") or "").strip()[:200]
            add("run_failed", f"运行失败：{err}", BROKEN)

        elif etype == EventType.RETRIEVE_END:
            if data.get("degraded") and "retrieve_degraded" not in kinds:
                # knowledge.py 在这条事件**之前**已经把每条具体原因发成了 warn
                #（"重排结果解析不出来"、"多少条向量对不上"）。两边都收就是同一件事
                # 报两遍，一条含糊一条具体——这里只兜没有 warn 的情形
                add("retrieve_degraded", "知识库检索能力降级，可能漏掉换了说法的内容", DEGRADED)
            if not data.get("count"):
                q = str(data.get("query") or "").strip()[:60]
                add("retrieve_empty", f"知识库里没有检索到与「{q}」相关的内容", BROKEN)

        elif etype == EventType.LOG and data.get("level") == "warn":
            message = str(data.get("message") or "").strip()
            code = str(data.get("code") or "")
            kind, severity = WARN_CODES.get(code, ("warn", DEGRADED))
            if message:
                add(kind, message[:300], severity)

    text = answer_text(output)
    if not text:
        add("empty_output", "这次运行没有产出任何成果内容", BROKEN)
    elif any(mark in text for mark in _PLACEHOLDER_MARKS):
        add("placeholder_output", "交出来的是一句内部流程说明，不是对问题的回答", BROKEN)

    return signals


def worst(signals: Iterable[Signal]) -> str:
    """一组信号里最重的那档。没有信号时返回空串。"""
    ranked = sorted(signals, key=lambda s: _SEVERITY_RANK.get(s.severity, 1), reverse=True)
    return ranked[0].severity if ranked else ""


def should_retry(signals: Iterable[Signal]) -> bool:
    """这些信号里有没有「重跑一次大概率能好」的。"""
    return any(s.kind in RETRYABLE and s.severity == BROKEN for s in signals)


def check_numbers(original: str, revised: str) -> list[str]:
    """复核重写的答案里，有哪些数字是原答案里没有的。

    复核模型手上只有原答案和一份异常清单，没有任何原始数据——它写出来的新数字
    只能是凭空来的。这条是硬拦截，不是提示：叙述层无算术权限是这个项目的地基
    （见 issuance.decide_tier）。

    直接复用出具校验那套回指逻辑，把原答案的数字当成受控指标集。
    """
    controlled = [
        {"id": tok.raw, "value": tok.value} for tok in extract_numbers(original)
    ]
    report = trace_numbers(revised, controlled)
    return [item["token"] for item in report.unmatched]


REVIEW_SYSTEM = (
    "你负责复核一次数据问答的结果。执行过程中检测到了异常，你的任务是让用户"
    "在读这个答案之前就知道它哪里不可靠。\n\n"
    "你会拿到：用户的问题、系统给出的原答案、这次运行的异常清单。\n\n"
    "铁律：\n"
    "1. 你看不到任何原始数据。**不许写出原答案里没有的数字**——一个都不行。"
    "需要改写时只能重组原答案已有的内容。\n"
    "2. 异常必须写进 note，不许淡化或略过。降级要明说，不能悄悄地降。\n"
    "3. 如果异常说明这个答案根本不可信（比如取数工具就没跑通、检索什么都没查到），"
    "就直说答不了、说清缺什么，不要拿一个看起来完整的答案去糊。\n"
    "4. note 写给用户看，一到两句中文，说人话：出了什么事、对这个答案意味着什么。"
    "不要复述事件名或代码。\n\n"
    "输出 JSON：\n"
    '- note：给用户的异常说明（必填）\n'
    '- answer：重写后的答案。原答案本身没问题、只是需要补一句说明时，留空字符串\n'
    '- retry：这次运行是不是「换个配置重跑一遍大概率就好了」（比如步数不够、'
    "工具参数写错）。问题本身没问清楚导致的，填 false"
)

REVIEW_SCHEMA: dict[str, Any] = {
    "title": "review",
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "给用户的异常说明，一到两句中文"},
        "answer": {"type": "string", "description": "重写后的答案；不需要改写就留空"},
        "retry": {"type": "boolean", "description": "重跑一次是否大概率能好"},
    },
    "required": ["note"],
}


def review_prompt(question: str, answer: str, signals: list[Signal]) -> str:
    """拼给复核模型的请求。

    只给信号清单，不给原始事件流：几百条事件塞进 prompt 既贵又没用，而信号
    已经是规则层提炼过的结论。
    """
    lines = "\n".join(
        f"- [{'严重' if s.severity == BROKEN else '降级'}] {s.detail}" for s in signals
    )
    return (
        f"用户的问题：\n{question or '（未记录）'}\n\n"
        f"系统给出的原答案：\n{answer or '（空）'}\n\n"
        f"检测到的异常：\n{lines}"
    )


@dataclass
class ReviewResult:
    """复核结论。verdict 是对「系统实际做了什么」的陈述，不是模型的自我评价。"""

    verdict: str          # ok | annotated | rewritten | reverted | failed
    note: str = ""
    answer: str | None = None   # None 表示沿用原答案
    #: 改写之前的那份。改写是有损的，原件得跟着一起落库——否则刷新之后
    #: 只剩一个被重组过的答案，想对照都没得对照
    original: str | None = None
    retry: bool = False
    signals: list[Signal] = None  # type: ignore[assignment]
    severity: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "note": self.note,
            "answer": self.answer,
            "original": self.original,
            "retry": self.retry,
            "severity": self.severity,
            "signals": [s.as_dict() for s in (self.signals or [])],
        }


async def review(
    model: Any, *, question: str, answer: str, signals: list[Signal]
) -> ReviewResult:
    """叫一次模型复核。调用方负责确保 signals 非空——空信号根本不该走到这里。

    复核本身失败不能让整轮挂掉：答案已经在手上了，复核只是加一层说明。
    所以这里把异常全兜住，最差也要把原答案原样交出去。
    """
    severity = worst(signals)
    rule_retry = should_retry(signals)
    fallback_note = "；".join(s.detail for s in signals if s.severity == BROKEN) or "；".join(
        s.detail for s in signals
    )

    messages = [("system", REVIEW_SYSTEM), ("human", review_prompt(question, answer, signals))]
    try:
        raw = await model.with_structured_output(REVIEW_SCHEMA).ainvoke(messages)
        if not isinstance(raw, dict):
            raise TypeError("结构化输出没有返回对象")
    except Exception:  # noqa: BLE001 - 不支持结构化输出的模型退回文本解析
        try:
            from app.api.copilot import _extract_json
            from app.engine.state import message_text

            raw = _extract_json(message_text(await model.ainvoke(messages)))
        except Exception:  # noqa: BLE001
            # 模型这条路彻底不通。规则层已经知道出了什么事，照样说得出话
            return ReviewResult(
                verdict="annotated", note=fallback_note, retry=rule_retry,
                signals=signals, severity=severity,
            )

    note = str(raw.get("note") or "").strip() or fallback_note
    revised = str(raw.get("answer") or "").strip()
    # 规则层管**资格**，模型只有**否决权**。反过来（两边取并）的话，模型就能
    # 越过规则强推重跑——而"检索什么都没查到"这类，重跑一遍还是查不到，
    # 白烧一次建图加一次跑图。模型没表态时按规则走
    retry = rule_retry and bool(raw.get("retry", True))

    if not revised or revised == answer.strip():
        return ReviewResult(
            verdict="annotated", note=note, retry=retry, signals=signals, severity=severity,
        )

    invented = check_numbers(answer, revised)
    if invented:
        # 重写里冒出了原答案没有的数字。整条退回——说明还是要给的，
        # 但那份正文一个字都不能用
        return ReviewResult(
            verdict="reverted",
            note=f"{note}\n\n（复核改写里出现了原答案中没有的数字"
                 f"（{'、'.join(invented[:5])}），已退回原答案。）",
            retry=retry, signals=signals, severity=severity,
        )

    return ReviewResult(
        verdict="rewritten", note=note, answer=revised, original=answer, retry=retry,
        signals=signals, severity=severity,
    )
