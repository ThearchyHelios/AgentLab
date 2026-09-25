"""人工审批的回复怎么读。全引擎只此一份。

以前有四份：human 节点、agent 节点、工具节点、代码节点各读各的，口径不一。
后果不是风格问题：工具节点和代码节点对字符串一律 bool()，回复"拒绝"、"no"
都被当成批准，工具真的执行了；而 {"decision": "approve"} 反被判成拒绝。

规则只有一条：**只认明确的批准，看不懂的一律按拒绝处理。** 审批这道门放错了
的代价（危险工具被执行）远大于拦错了的代价（再批一次）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_YES = frozenset({
    "approve", "approved", "yes", "y", "true", "ok", "allow",
    "同意", "通过", "批准", "允许", "可以",
})
_NO = frozenset({
    "reject", "rejected", "deny", "denied", "no", "n", "false",
    "拒绝", "驳回", "不同意", "不通过", "不允许", "不可以",
})


@dataclass(frozen=True)
class Decision:
    #: 明确批准了。审批类的关卡（工具、代码、human 的 approve 模式）只认这个。
    approved: bool
    #: 明确拒绝了。填内容类的关卡（human 的 input / edit 模式）只在这时才停：
    #: 那两种模式的回复本身就是内容，没说"不"就是交了稿。
    rejected: bool
    note: str = ""
    #: 人工改过的工具参数
    args: dict[str, Any] | None = None
    #: 人工改过的代码
    code: str | None = None
    #: human 节点收到的内容（input / edit 模式）
    value: Any = None


def _word(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _verdict(value: Any) -> bool | None:
    """True / False / None（没表态）。"""
    if isinstance(value, bool):
        return value
    word = _word(value)
    if word in _YES:
        return True
    if word in _NO:
        return False
    return None


def read_decision(response: Any) -> Decision:
    if isinstance(response, dict):
        verdict = _verdict(response["approved"]) if "approved" in response \
            else _verdict(response.get("decision"))
        args = response.get("args")
        code = response.get("code")
        return Decision(
            approved=verdict is True,
            rejected=verdict is False,
            note=str(response.get("note") or response.get("comment") or ""),
            args=args if isinstance(args, dict) else None,
            code=code if isinstance(code, str) and code else None,
            value=response.get("value"),
        )
    verdict = _verdict(response)
    # 不是一个是/否词的字符串（"拒绝，这个表是生产库"）既算不上批准，本身又
    # 是理由——留作备注，agent 转告模型时才不会只剩一句"未说明"
    note = response.strip() if isinstance(response, str) and verdict is None else ""
    return Decision(
        approved=verdict is True,
        rejected=verdict is False,
        note=note,
        # 字符串回复本身就是内容：input 模式下用户可能直接回一句话
        value=response,
    )
