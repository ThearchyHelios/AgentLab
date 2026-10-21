"""分析结论里的数：精确的有理数进、规范的十进制字符串出（细案 01 §1「数用十进制」）。

结论要能逐字节复算（证据面板、导出、可验证报告的复算和封存的逐字段比，评测作答前后各算一次逐字比），所以：

- **算**：快照里的数一律换成 Fraction（浮点按最短写法 repr，数字文本按字面，DECIMAL 落成的文本不经浮点），加减乘除全程
  精确——各段之和逐位等于总变化，Σ结构效应 + Σ自身效应逐位等于比率的变化。只有要开方、取对数、查分布的统计量是浮点。
- **写**：canon 把数写成规范的十进制字符串：不带指数、不带正号、去掉多余的 0、没有「-0」。能精确写成有限小数的（快照里的
  数和它们的和差，0.25 这样的商）照写；除不尽的和浮点按 SIG 位有效数字四舍五入（ROUND_HALF_UP）。浮点只留 12 位有效
  数字：不同机器的数学库在最后一两位上可能不一样，留 12 位同一份输入在哪台机器上复算都是同一串字。
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

#: 除不尽的数、浮点保留几位有效数字
SIG = 12
#: 能精确写成有限小数、但有效数字超过这么多位的，也按 SIG 位写（只会出现在反复除以 2 这类极端情形）
_EXACT_MAX_DIGITS = 30


def to_fraction(value: Any) -> Fraction | None:
    """一个数换成精确的 Fraction：整数、Decimal、Fraction 原样；浮点按最短写法（repr）；数字文本按字面。布尔、非数、无穷、
    认不出的为 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Fraction(Decimal(repr(value)))
    if isinstance(value, Decimal):
        return Fraction(value) if value.is_finite() else None
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return Fraction(d) if d.is_finite() else None
    return None


def _terminating(q: Fraction) -> int | None:
    """q 能精确写成有限小数时要几位小数；不能为 None。"""
    d = q.denominator
    twos = fives = 0
    while d % 2 == 0:
        d //= 2
        twos += 1
    while d % 5 == 0:
        d //= 5
        fives += 1
    return max(twos, fives) if d == 1 else None


def _plain(digits: int, exp10: int) -> str:
    """digits × 10^exp10（digits ≥ 0）写成不带指数、去尾零的十进制。"""
    text = str(digits)
    if exp10 >= 0:
        return text + "0" * exp10 if digits else "0"
    places = -exp10
    text = text.rjust(places + 1, "0")
    head, tail = text[:-places], text[-places:].rstrip("0")
    return f"{head}.{tail}" if tail else head


def _decade(a: Fraction) -> int:
    """a > 0 的十进制量级：10^e ≤ a < 10^(e+1) 的 e。"""
    try:
        e = math.floor(math.log10(a.numerator) - math.log10(a.denominator))
    except (ValueError, OverflowError):
        e = len(str(a.numerator)) - len(str(a.denominator))
    while Fraction(10) ** e > a:
        e -= 1
    while Fraction(10) ** (e + 1) <= a:
        e += 1
    return e


def round_sig(q: Fraction, sig: int = SIG) -> Fraction:
    """按 sig 位有效数字四舍五入（ROUND_HALF_UP）。"""
    if q == 0:
        return Fraction(0)
    sign = -1 if q < 0 else 1
    a = abs(q)
    e = _decade(a)
    scale = Fraction(10) ** (sig - 1 - e)
    m = math.floor(a * scale + Fraction(1, 2))
    return sign * Fraction(m) / scale


def round_places(q: Fraction, places: int) -> Fraction:
    """按 places 位小数四舍五入（ROUND_HALF_UP，负数按绝对值舍入，同 Decimal 的 ROUND_HALF_UP）。"""
    sign = -1 if q < 0 else 1
    scale = Fraction(10) ** places
    return sign * Fraction(math.floor(abs(q) * scale + Fraction(1, 2))) / scale


def _write(q: Fraction) -> str:
    """有限小数的 q 写出来（调用方保证 q 能精确写成有限小数）。"""
    places = _terminating(q) or 0
    sign = "-" if q < 0 else ""
    digits = abs(q) * (10 ** places)
    assert digits.denominator == 1
    return sign + _plain(int(digits), -places)


def canon(value: Any) -> str | None:
    """规范的十进制字符串（见文件头）。None、非数、无穷为 None。"""
    if isinstance(value, float) and math.isfinite(value):
        q = round_sig(to_fraction(value) or Fraction(0))
    else:
        q = to_fraction(value)
        if q is None:
            return None
        places = _terminating(q)
        if places is None or len(str(int(abs(q) * 10 ** places)).rstrip("0")) > _EXACT_MAX_DIGITS:
            q = round_sig(q)
    if q == 0:
        return "0"
    return _write(q)


def fixed(q: Fraction, places: int, *, strip: bool = True, group: bool = False) -> str:
    """按 places 位小数四舍五入写出来；strip 去掉尾零，group 加千分位。没有正号；舍成 0 的写「0」（不写「-0」）。"""
    r = round_places(q, places)
    sign = "-" if r < 0 else ""
    scaled = abs(r) * (10 ** places)
    whole, frac = divmod(int(scaled), 10 ** places)
    head = f"{whole:,}" if group else str(whole)
    tail = str(frac).rjust(places, "0") if places > 0 else ""
    if strip:
        tail = tail.rstrip("0")
    text = f"{head}.{tail}" if tail else head
    return sign + text


__all__ = ["SIG", "canon", "fixed", "round_places", "round_sig", "to_fraction"]
