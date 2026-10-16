"""两个数算不算对得上：「允许的相对误差」只有这一种比法（docs/design/next-round/01-效果度量与回归评测.md 做法 10）。

双盲复算（engine/nodes/dual_check.py）比原流程值和盲算值，评测（app/eval/score.py）比期望值和答案里的数，用的是同一个
函数、设置里也叫同一个名字「允许的相对误差」：两处各写一份，迟早一边改了另一边没改，同一对数在核查里算一致、在评测里
算错。所以从双盲复算里原样搬到这里（行为不变），两边都 import。

- agree：按相对误差比（|b − a| ≤ tolerance × |a|）；原值为 0 时按绝对误差（|b| ≤ tolerance）。值完全相等的直接算对得上
  （文字也行）；有一边不是有限的数就对不上。
- delta_pct：相差比例，口径同运行对比：(b − a) ÷ |a| × 100，两位小数；原值为 0、有一边不是数时为 None。
"""
from __future__ import annotations

import math
from typing import Any


def is_finite_number(value: Any) -> bool:
    """有限的数（布尔不算，NaN、无穷不算）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def agree(original: Any, other: Any, tolerance: float) -> bool:
    """按相对误差比（原值为 0 时按绝对误差）。"""
    if original == other:
        return True
    if not (is_finite_number(original) and is_finite_number(other)):
        return False
    if original == 0:
        return abs(other) <= tolerance
    return abs(other - original) <= tolerance * abs(original)


def delta_pct(original: Any, other: Any) -> float | None:
    """相差比例：(other − original) ÷ |original| × 100，两位小数；original 为 0 时为 None。"""
    if not (is_finite_number(original) and is_finite_number(other)) or original == 0:
        return None
    return round((other - original) / abs(original) * 100, 2)


__all__ = ["agree", "delta_pct", "is_finite_number"]
