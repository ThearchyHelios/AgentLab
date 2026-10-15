"""白话层（docs/design/normal-user-apps.md N10）：问题编号 → 一句话、为什么要紧、谁来改、怎么办。

覆盖工作流发布门禁（contract.* / governed.* / action.* / process.*）、应用检查（app.*）、蓝图（bp.*）、模板（tpl.*）、
个人应用（personal.*）、发布单步骤失败（plan.*）。没登记白话的照旧显示技术说法；检查脚本报「有问题编号没有白话」。
who：you（你来定）/ assistant（助手可以修）/ admin（要找管理员），按自动修复的 kind、APP_HUMAN_ONLY、要管理员的编号判。

框架在这里；内容由包 B2 补齐（B1、B3、B4 新增的编号各自补进来）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WHO = ("you", "assistant", "admin")


@dataclass(frozen=True)
class Plain:
    title: str
    why: str = ""
    who: str = "you"
    action: str = ""


#: 问题编号 → 白话。前缀匹配用 PLAIN_PREFIX（如 "subgraph_version_" 一类）
PLAIN: dict[str, Plain] = {}
PLAIN_PREFIX: dict[str, Plain] = {}


def plain_of(code: str | None) -> Plain | None:
    if not code:
        return None
    if code in PLAIN:
        return PLAIN[code]
    for prefix, plain in PLAIN_PREFIX.items():
        if code.startswith(prefix):
            return plain
    return None


def with_plain(issue: dict[str, Any]) -> dict[str, Any]:
    """给一条问题补上 plain = {title, why, who, action}；没登记的不补。"""
    plain = plain_of(issue.get("code"))
    if plain is None:
        return issue
    return {**issue, "plain": {"title": plain.title, "why": plain.why, "who": plain.who, "action": plain.action}}
