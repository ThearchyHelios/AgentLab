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
_NUM_RE = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|％|万亿|亿|万)?")

# 不参与回指的数字形态——这些不是"结论数字"
_SKIP_PATTERNS = [
    re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}(\s*[-/月]\s*\d{1,2}\s*日?)?"),  # 日期
    re.compile(r"(?<!\d)(19|20)\d{2}\s*年?(?!\d)"),  # 年份
    re.compile(r"\d{4}-W\d{1,2}", re.IGNORECASE),  # ISO 周（2026-W37）
    re.compile(r"^\s*\d{1,2}[.、)]\s", re.MULTILINE),  # 行首枚举 "1. " "2、"
    re.compile(r"\[\d+\]"),  # 引用标记 [1]
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
) -> str:
    """三档出具的判定。

    withheld（不予出具）：必需指标缺失，或 strict 下有未回指数字——
    结论的地基不完整，宁可不出。
    degraded（降档出具）：期望指标缺失或有未回指数字，出但明确声明缺口。
    formal（正式出具）：清单齐、数字全部可回指。
    """
    if missing_required:
        return "withheld"
    if strict and unmatched:
        return "withheld"
    if missing_expected or unmatched:
        return "degraded"
    return "formal"
