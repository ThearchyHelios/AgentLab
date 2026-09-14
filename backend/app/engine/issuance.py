from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 出具校验：叙述里的每个数字必须能回指到指标集
#
# 前提是派生边界已经收死：所有算术都发生在口径卡（metrics 节点）里，
# 叙述层只能引用。所以这里不需要"验证模型算得对不对"——只需要验证
# "它写的每个数都在清单里"。集合外的数字没有合法来源，一律视为现编。
# --------------------------------------------------------------------------

# 数字 token：可带千分位逗号、小数，后缀 %/％ 或 万/亿
#
# 后瞻里刻意只排 ASCII 词字符，**不能**写成 (?<![\w.])：Python 的 \w 按 Unicode
# 判定，汉字也算词字符，于是"营收9999元"这种中文常态（数字紧贴汉字、不加空格）
# 整个 token 都抽不出来。那等于说校验器只对西式排版生效，中文叙述完全不设防——
# 而这套机制的全部意义就是拦住叙述层现编数字。
# 排 ASCII 字母是为了放过 v2 / sha1 这类标识符，排 . 是为了放过 v1.2.3 的版本号。
_NUM_RE = re.compile(r"(?<![0-9A-Za-z_.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|％|万亿|亿|万)?")

# 不参与回指的数字形态——这些不是"结论数字"。
#
# 这里的规则一律要求**语境**，不能只看形态：免检名单放宽一分，叙述层就多一分
# 现编数字的空间。之前 `年` 是可选的，等于把 1900–2099 的任何独立整数都放行了——
# 而客户数、门店数、万元金额大量落在这个区间，"本周新增客户 2050 家"里那个
# 2050 就这么免检了。
_SKIP_PATTERNS = [
    # 日期：年份限 19xx/20xx、月份限 1-12，否则 "9600/12"（比值）这类写法
    # 会被整段当日期吞掉，斜杠两边的数字就都免检了
    re.compile(
        r"(?<!\d)(19|20)\d{2}\s*[-/年]\s*(0?[1-9]|1[0-2])"
        r"(\s*[-/月]\s*(0?[1-9]|[12]\d|3[01])\s*日?)?(?!\d)"
    ),
    # 年份：必须带"年"字。没有"年"的四位数按结论数字处理——
    # 宁可误杀（要求它回指指标集）也不放过，这是校验不是排版
    re.compile(r"(?<!\d)(19|20)\d{2}\s*年(?!\d)"),
    re.compile(r"\d{4}-W\d{1,2}", re.IGNORECASE),  # ISO 周（2026-W37）
    re.compile(r"^\s*\d{1,2}[.、)]\s", re.MULTILINE),  # 行首枚举 "1. " "2、"
    re.compile(r"\[\d{1,3}\]"),  # 引用标记 [1]，限三位——[98765] 不是引用
    re.compile(r"\d{1,2}:\d{2}(:\d{2})?"),  # 时间
]

_SUFFIX_MULT = {"万": 1e4, "亿": 1e8, "万亿": 1e12}


@dataclass
class NumberToken:
    raw: str
    value: float
    decimals: int
    is_percent: bool
    context: str
    start: int


@dataclass
class TraceReport:
    matched: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unmatched


def _skip_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in _SKIP_PATTERNS:
        for m in pattern.finditer(text):
            spans.append(m.span())
    return spans


def extract_numbers(text: str) -> list[NumberToken]:
    """抽出叙述里所有需要回指的数字，剔除日期、枚举序号、引用标记这类形态。"""
    skip = _skip_spans(text)
    tokens: list[NumberToken] = []
    for m in _NUM_RE.finditer(text):
        start, end = m.span()
        if any(s <= start < e for s, e in skip):
            continue
        raw_num = m.group(1).replace(",", "")
        suffix = m.group(2) or ""
        try:
            value = float(raw_num)
        except ValueError:
            continue
        if suffix in _SUFFIX_MULT:
            value *= _SUFFIX_MULT[suffix]
        decimals = len(raw_num.split(".")[1]) if "." in raw_num else 0
        ctx_lo = max(0, start - 18)
        tokens.append(
            NumberToken(
                raw=m.group(0).strip(),
                value=value,
                decimals=decimals,
                is_percent=suffix in ("%", "％"),
                context=text[ctx_lo : min(len(text), end + 18)].replace("\n", " "),
                start=start,
            )
        )
    return tokens


def _close(a: float, b: float, decimals: int) -> bool:
    """按 token 自己写出的小数位数给舍入容差：写了 12.3 就允许 |Δ|≤0.05。"""
    tolerance = 0.5 * 10 ** (-decimals)
    return abs(a - b) <= tolerance + 1e-9


def _token_matches(token: NumberToken, metric_value: float) -> bool:
    # 直接比对（含"叙述写百分数、指标本身就是百分数"的情形）
    if _close(token.value, metric_value, token.decimals):
        return True
    if token.is_percent:
        # 指标存的是比率（0.123），叙述写成 12.3%
        if _close(token.value, metric_value * 100, token.decimals):
            return True
    return False


def trace_numbers(
    narrative: str,
    metrics: list[dict[str, Any]],
    *,
    allow: list[Any] | None = None,
) -> TraceReport:
    """叙述 vs 指标集的回指校验。

    allow 是模板作者显式放行的字面量（比如报告标题里的期数）——
    白名单是配置的一部分，进版本、被审查，而不是校验器私藏的宽容。
    """
    numeric_values = [
        float(m["value"])
        for m in metrics
        if isinstance(m.get("value"), (int, float)) and not isinstance(m.get("value"), bool)
    ]
    allowed = {str(a).strip() for a in (allow or [])}
    allowed_values = set()
    for a in allowed:
        try:
            allowed_values.add(float(a.replace(",", "").rstrip("%％")))
        except ValueError:
            pass

    report = TraceReport()
    for token in extract_numbers(narrative):
        if token.raw in allowed or token.raw.rstrip("%％").strip() in allowed:
            continue
        if any(_close(token.value, av, token.decimals) for av in allowed_values):
            continue
        hit = next(
            (
                m
                for m, mv in zip(
                    [m for m in metrics if isinstance(m.get("value"), (int, float))],
                    numeric_values,
                )
                if _token_matches(token, mv)
            ),
            None,
        )
        if hit is not None:
            report.matched.append({"token": token.raw, "metric": hit.get("id")})
        else:
            report.unmatched.append({"token": token.raw, "context": token.context})
    return report


def decide_tier(
    *,
    missing_required: list[str],
    missing_expected: list[str],
    unmatched: list[dict[str, Any]],
    strict: bool,
    gaps: list[str] | None = None,
) -> str:
    """三档出具的判定。

    withheld（不予出具）：必需指标缺失，或 strict 下有未回指数字——
    结论的地基不完整，宁可不出。
    degraded（降档出具）：期望指标缺失、有未回指数字，或校验本身没能完整跑起来。
    formal（正式出具）：清单齐、数字全部可回指，**且校验确实发生过**。

    最后那句是这个函数最容易出错的地方：missing/unmatched 全空既可能是
    "查过了都对"，也可能是"根本没查"——叙述模板里一个笔误让 narrative 渲染成
    空串、metrics_from 指向不存在的节点，都会得到一模一样的空列表。
    校验没发生就盖 formal 章，比漏检某个数字更糟：它让整套机制看起来在工作。
    所以 gaps 用来承载"校验为什么不完整"，非空就不允许 formal。
    """
    if missing_required:
        return "withheld"
    if strict and unmatched:
        return "withheld"
    if missing_expected or unmatched or gaps:
        return "degraded"
    return "formal"
