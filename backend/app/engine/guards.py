"""agent 节点的护栏：取代以前那个固定的「最多 12 步」。

固定步数挡的是四件事，每件都有更贴切的挡法：
- 循环不收敛（反复查同一张表、在两个做法之间来回）→ 重复调用直接复用上次结果，
  连续几步拿不到新信息就收尾；
- 花费越滚越大（每一步都重发前面的全部对话）→ 按令牌 / 金额记预算，可以设成不限；
- 上下文会满（满了是调用直接报错，查到的全丢）→ 接近窗口时先压缩早期工具结果，再不够就收尾；
- 真正失控的兜底 → 步数只留一个很高的上限（默认 100，设置里可改）。

规矩：
- 上限在运行发起时快照下来（RunContext.agent_limits），恢复、接着跑都用这一份。审批恢复时
  节点整个重放，上限要是变了，循环停在不同的位置，checkpoint 里的任务结果就对错号了；
- 快照为 None 是升级前发起的运行：一律走旧逻辑（默认 12 步、没有这些护栏）。它们可能正停在
  某个审批上，多一次复用、少一次调用，重放的位置同样会错开；
- 这里的每个判断只取决于重放时照样拿得到的东西（模型回复里的用量、工具结果），不读时钟。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

#: 以前画布给新节点写死的步数。几乎都是画布自动写进去的，不是谁特意选的，按「没填」处理
LEGACY_DEFAULT_STEPS = 12
#: 连续几步拿不到新信息就收尾
STALL_STEPS = 3
#: 目录里查不到窗口的模型，按这个保守值算
FALLBACK_CONTEXT = 128_000
#: 用到窗口的这个比例，开始压缩早期工具结果
COMPRESS_AT = 0.6
#: 压缩之后还到这个比例，就收尾：留出收尾轮自己要的那部分
SETTLE_AT = 0.85
#: 压缩时原样保留最近几条工具结果
KEEP_RECENT = 3
#: 超过这个字数的旧工具结果才压缩，压缩后留开头这么多字
COMPRESS_OVER, COMPRESS_HEAD = 1200, 400

DEFAULT_LIMITS: dict[str, Any] = {
    # 兜底步数；实际还受 settings.max_agent_steps（环境变量）封顶
    "agent_max_steps": 100,
    # 每个 agent 节点的令牌预算（输入 + 输出）。None 表示不限
    "agent_budget_tokens": 2_000_000,
    # 金额预算（美元）。目录里没有价格的模型估不出金额，只能靠令牌预算。None 表示不限
    "agent_budget_usd": None,
}


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def run_limits(run_defaults: dict[str, Any], hard_cap: int) -> dict[str, Any]:
    """发起运行时的快照。设置里存坏了的值按缺省算；预算写 null / 0 / 空都表示不限。"""
    steps = _positive(run_defaults.get("agent_max_steps"))
    tokens = run_defaults.get("agent_budget_tokens", DEFAULT_LIMITS["agent_budget_tokens"])
    usd = run_defaults.get("agent_budget_usd", DEFAULT_LIMITS["agent_budget_usd"])
    return {
        "max_steps": min(int(steps or DEFAULT_LIMITS["agent_max_steps"]), hard_cap),
        "budget_tokens": int(_positive(tokens)) if _positive(tokens) else None,
        "budget_usd": _positive(usd),
    }


@dataclass
class Limits:
    max_steps: int
    budget_tokens: int | None
    budget_usd: float | None


def node_limits(config: dict[str, Any], snapshot: dict[str, Any], hard_cap: int) -> Limits:
    """节点上写了的覆盖快照。步数恰好是 12 的当作没写（见 LEGACY_DEFAULT_STEPS）。"""
    raw_steps = _positive(config.get("max_steps"))
    if raw_steps is not None and int(raw_steps) == LEGACY_DEFAULT_STEPS:
        raw_steps = None
    steps = int(raw_steps) if raw_steps else int(snapshot.get("max_steps") or DEFAULT_LIMITS["agent_max_steps"])
    tokens = _positive(config.get("budget_tokens"))
    usd = _positive(config.get("budget_usd"))
    return Limits(
        max_steps=max(1, min(steps, hard_cap)),
        budget_tokens=int(tokens) if tokens else snapshot.get("budget_tokens"),
        budget_usd=usd if usd else snapshot.get("budget_usd"),
    )


def call_key(name: str, args: dict[str, Any]) -> str:
    return json.dumps({"tool": name, "args": args}, ensure_ascii=False, sort_keys=True, default=str)


#: 收尾的原因 → (给模型的一句话, 给人看的提示)
REASONS: dict[str, tuple[str, str]] = {
    "steps": ("步数上限用完了（{max_steps} 步）",
              "Agent 已用完 {max_steps} 步上限。如需完整结果，请调大节点的「最大步数」或设置中的默认步数；"
              "如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。"),
    "stall": ("连续 {stall} 步没有拿到新信息（重复的调用，或者调用都失败了）",
              "Agent 连续 {stall} 步没有获得新信息，已根据已查到的内容收尾。可能是提示词中的目标无法查到，"
              "或工具参数持续有误，请检查 Agent 最后几次的工具调用。"),
    "budget_tokens": ("这个节点的 token 预算用完了（已用 {used_tokens:,} / 上限 {budget_tokens:,}）",
                      "Agent 已用完 token 预算（{budget_tokens:,}），已根据已查到的内容收尾。如需完整结果，"
                      "请调大节点的「token 预算」或设置中的默认预算，也可在设置中改为不限。"),
    "budget_usd": ("这个节点的金额预算用完了（已用 ${used_usd:.4f} / 上限 ${budget_usd:.4f}）",
                   "Agent 已用完金额预算（${budget_usd:.2f}），已根据已查到的内容收尾。如需完整结果，"
                   "请调大节点的「金额预算（美元）」或设置中的默认预算，也可在设置中改为不限。"),
    "context": ("对话已经接近模型的上下文上限（约 {used_context:,} / {window:,} token）",
                "Agent 的对话已接近模型上下文上限，压缩早期工具结果后仍不够，已根据已查到的内容收尾。"
                "请缩小每次查询的范围（如在 SQL 中加 LIMIT、只选需要的列），或换用上下文窗口更大的模型。"),
}


@dataclass
class Guard:
    limits: Limits
    window: int
    #: 成功执行过的调用 → (第几步, 结果)
    seen: dict[str, tuple[int, str]] = field(default_factory=dict)
    stalled: int = 0
    reminded: set[str] = field(default_factory=set)
    last_input: int = 0
    compressed: bool = False
    facts: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 重复调用

    def repeat(self, name: str, args: dict[str, Any]) -> str | None:
        """同一个工具、同样的参数已经成功跑过：返回要喂回去的那句话，不再执行。"""
        hit = self.seen.get(call_key(name, args))
        if hit is None:
            return None
        step, content = hit
        return (f"这个调用（同一个工具、同样的参数）第 {step + 1} 步已经执行过，没有再执行。上次的结果：\n"
                f"{content}\n\n不要重复调用：基于已有结果继续，或者换一个能拿到新信息的查询，或者给出结论。")

    def record(self, step: int, name: str, args: dict[str, Any], ok: bool, content: str) -> None:
        if ok:
            self.seen.setdefault(call_key(name, args), (step, content))

    def step_done(self, progressed: bool) -> None:
        self.stalled = 0 if progressed else self.stalled + 1

    # ------------------------------------------------------------ 用量与上下文

    def observe(self, response: BaseMessage, messages: list[BaseMessage]) -> None:
        """记下这一次模型调用实际用了多大的上下文。供应商没报用量就按字数粗估。"""
        meta = getattr(response, "usage_metadata", None) or {}
        reported = int(meta.get("input_tokens") or 0)
        if reported:
            self.last_input = reported
        else:
            chars = sum(len(m.content) if isinstance(m.content, str) else len(json.dumps(m.content, default=str))
                        for m in messages)
            self.last_input = chars // 2

    def compress(self, messages: list[BaseMessage]) -> int:
        """上下文用到 COMPRESS_AT 以上时，把早期的长工具结果换成开头一段。返回压缩了几条。"""
        if self.last_input < self.window * COMPRESS_AT:
            return 0
        tool_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
        squeezed = 0
        for i in tool_positions[:-KEEP_RECENT] if KEEP_RECENT else tool_positions:
            m = messages[i]
            text = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
            if len(text) <= COMPRESS_OVER or text.endswith(_SQUEEZED_TAIL):
                continue
            messages[i] = ToolMessage(
                content=f"{text[:COMPRESS_HEAD]}\n…（早期的工具结果已压缩，原文 {len(text)} 字。"
                        f"需要其中的细节就重新查一个更小的范围）{_SQUEEZED_TAIL}",
                tool_call_id=m.tool_call_id,
            )
            squeezed += 1
        if squeezed:
            self.compressed = True
        return squeezed

    def stop_reason(self, usage: dict[str, Any], *, context: bool = True) -> str | None:
        """该收尾了吗。步数用完由循环自己管，这里管另外三种。context=False 时不按上下文判。"""
        if self.stalled >= STALL_STEPS:
            return "stall"
        used_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if self.limits.budget_tokens and used_tokens >= self.limits.budget_tokens:
            return "budget_tokens"
        if self.limits.budget_usd and float(usage.get("cost_usd") or 0) >= self.limits.budget_usd:
            return "budget_usd"
        if context and self.last_input >= self.window * SETTLE_AT:
            return "context"
        return None

    def reminder(self, step: int, usage: dict[str, Any]) -> str | None:
        """临近上限时提醒模型一次（每种只提醒一次）。返回要追加的一句话。"""
        notes = []
        left = self.limits.max_steps - (step + 1)
        if self.limits.max_steps > 5 and left <= 3 and "steps" not in self.reminded:
            self.reminded.add("steps")
            notes.append(f"还剩 {left} 步可以调用工具")
        used_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if self.limits.budget_tokens and used_tokens >= self.limits.budget_tokens * 0.8 \
                and "budget" not in self.reminded:
            self.reminded.add("budget")
            notes.append(f"token 预算已用 {used_tokens * 100 // self.limits.budget_tokens}%")
        if self.limits.budget_usd and float(usage.get("cost_usd") or 0) >= self.limits.budget_usd * 0.8 \
                and "budget_usd" not in self.reminded:
            self.reminded.add("budget_usd")
            notes.append("金额预算已用到八成")
        if self.stalled == STALL_STEPS - 1 and "stall" not in self.reminded:
            self.reminded.add("stall")
            notes.append(f"已经连续 {self.stalled} 步没有拿到新信息")
        if not notes:
            return None
        return f"（系统提醒：{'；'.join(notes)}。优先补齐最关键的信息，然后给出结论。）"

    def describe(self, reason: str, usage: dict[str, Any]) -> tuple[str, str]:
        """(给模型的收尾理由, 给人看的提示)"""
        values = {
            "max_steps": self.limits.max_steps, "stall": STALL_STEPS,
            "used_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
            "budget_tokens": self.limits.budget_tokens or 0,
            "used_usd": float(usage.get("cost_usd") or 0), "budget_usd": self.limits.budget_usd or 0.0,
            "used_context": self.last_input, "window": self.window,
        }
        to_model, to_user = REASONS[reason]
        return to_model.format(**values), to_user.format(**values)


_SQUEEZED_TAIL = "⁣"   # 不可见的记号：压缩过的不再压第二遍


def legacy_hint(max_steps: int, settled: bool) -> str:
    """升级前（没有护栏快照）的运行用的说法：只有步数这一种收尾原因。"""
    if settled:
        return (f"Agent 已用完 {max_steps} 步上限，以上结论基于已查到的部分。"
                "如需完整结果，请调大节点的「最大步数」；如果大部分步数用于逐张表查询结构，"
                "可在提示词中指明要查的表。")
    return (f"Agent 已用完 {max_steps} 步上限，仍未给出结论。"
            "请调大节点的「最大步数」；如果大部分步数用于逐张表查询结构，可在提示词中指明要查的表。")


#: 结果里 limited 字段的说法，下游校验失败时拿来说明根源
LIMITED_LABEL = {
    "steps": "步数用完", "stall": "连续多步没有获得新信息", "budget_tokens": "token 预算用完",
    "budget_usd": "金额预算用完", "context": "对话接近上下文上限",
}
