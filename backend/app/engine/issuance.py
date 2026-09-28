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
# 后缀里的千 / 百万 / 千万：不认的话 "2千家" 抽出来是 2，回指的是一个错的值。
_NUM_RE = re.compile(
    r"(?<![0-9A-Za-z_.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|％|万亿|亿|千万|百万|万|千)?"
)

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

_SUFFIX_MULT = {"千": 1e3, "万": 1e4, "百万": 1e6, "千万": 1e7, "亿": 1e8, "万亿": 1e12}

# --------------------------------------------------------------------------
# 中文数字
#
# 只认阿拉伯数字的话，换一种写法就绕过去了："增长三成""客户两千五百家"以前全判
# formal，同样的意思写成 2500 却会被拦——"出错自动改一次"的复核回路甚至会把
# 模型往这条路上推。所以中文数字也得抽出来回指。
#
# 难处在误杀：中文里的数字字大量是虚指（一些、一起、十分重要、万一、五花八门、
# 从三个方面看）。规则按"这是不是在报一个量"来定：
# - 多字且解析得通的（十二、两千五百、一万二）一律抽；
# - 单字只在后面跟计数单位时抽（两家、五天）。"一"加量词多半是"一个问题"里的
#   冠词用法，不抽——放过一个值为 1 的现编，代价远小于满篇误杀；
# - 成 / 倍 / 百分之 / 分之 / 一半这些比例写法单字也抽，容差按口语精度给；
# - 余 / 多 / 来按区间回指（两千余 = [2000, 3000]），不是跳过——跳过就是新的绕法；
# - 序数（第三）、星期（周三）、月份（十二月）、季度名（三季度）、日期、
#   "从三个方面看"这种结构性列举、几十 / 数百这种虚指，不是结论数字。
# --------------------------------------------------------------------------

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"亿": 10**8, "万": 10**4}
_CN_CHARS = "".join([*_CN_DIGIT, *_CN_UNIT, *_CN_BIG])
_CN_DIGITS = "".join(_CN_DIGIT)
_CN_N = rf"[{_CN_CHARS}]+(?:点[{_CN_DIGITS}]+[万亿]?)?"
_CN_RE = re.compile(
    rf"百分之(?P<pct>{_CN_N})"
    rf"|(?P<den>{_CN_N})分之(?P<numr>{_CN_N})"
    rf"|(?P<half>一半)"
    rf"|(?P<num>{_CN_N})(?P<suffix>成|倍|个百分点|[余多来])?"
)

#: 单字数字后面跟着这些，才算在报一个量
_CN_COUNT_UNIT = re.compile(
    r"(个月|个季度|个工作日|小时|分钟|万元|亿元|美元|公里|公斤|千克"
    r"|[家个名位人次笔单台件项条款种类批辆套份张户所座间天周年秒元块吨米克升岁页篇起宗例箱瓶包支只盒袋场轮届])"
)
#: "从三个方面看"：列举的是论述的结构，不是数据
_CN_STRUCTURAL = re.compile(
    r"个(方面|原因|问题|建议|要点|维度|阶段|步骤|部分|层面|角度|环节|关键|特点|优势|挑战|结论|措施)"
)


def _cn_section(text: str) -> int | None:
    """万以内的一段：十二、两千五百、一百零五、两千五（省略写法 = 2500）。"""
    total, digit, last_unit, zero = 0, None, None, False
    for ch in text:
        if ch in ("零", "〇"):
            if digit is not None:
                return None
            zero = True
        elif ch in _CN_DIGIT:
            if digit is not None:            # 两个数字挨着（一一、五一）：不是数
                return None
            digit = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if last_unit is not None and unit >= last_unit:
                return None                  # 单位只能往下走：十百、百千都不是数
            total += (1 if digit is None else digit) * unit   # 开头的"十" = 一十
            digit, last_unit, zero = None, unit, False
        else:
            return None
    if digit is not None:
        # 两千五 = 2500：单位后面省掉了下一级单位；有"零"隔开时（一百零五）才是个位
        total += digit * (last_unit // 10 if last_unit and not zero else 1)
    return total


def _cn_integer(text: str) -> int | None:
    for char, mult in _CN_BIG.items():
        if char in text:
            head, tail = text.split(char, 1)
            if not head:
                # 光一个"万"是一万；"万一"不是一万一千，是"万一"
                return mult if not tail else None
            head_value = _cn_integer(head)
            if head_value is None:
                return None
            if len(tail) == 1 and tail in _CN_DIGIT:   # 一万二 = 12000
                tail_value: int | None = _CN_DIGIT[tail] * mult // 10
            else:
                tail_value = _cn_integer(tail) if tail else 0
            if tail_value is None:
                return None
            return head_value * mult + tail_value
    return _cn_section(text)


def cn_to_number(text: str) -> float | None:
    """两千五百 → 2500，一万二 → 12000，三点五万 → 35000。不是数返回 None。"""
    integer, _, frac = text.partition("点")
    mult = 1
    if frac and frac[-1] in _CN_BIG:
        mult, frac = _CN_BIG[frac[-1]], frac[:-1]
    value = _cn_integer(integer)
    if value is None or (not integer):
        return None
    if frac:
        return (value + float("0." + "".join(str(_CN_DIGIT[c]) for c in frac))) * mult
    return float(value * mult)


def _cn_precision(text: str, value: float) -> float:
    """这个中文数字写到哪一位：三万 → 10000，两千五百 → 100，三点五万 → 1000。

    中文不写末尾的零，最后一个数字落在哪一位，精度就是哪一位。十以内的整数
    （十二、二十）按精确数处理——"十家"就是十家。
    """
    if "点" in text:
        integer, _, frac = text.partition("点")
        mult = _CN_BIG.get(frac[-1], 1) if frac else 1
        digits = len(frac) - (1 if mult != 1 else 0)
        return 10 ** (-digits) * mult
    whole = int(value)
    if whole < 100:
        return 1
    step = 1
    while whole % (step * 10) == 0:
        step *= 10
    return step


@dataclass
class NumberToken:
    raw: str
    value: float
    decimals: int
    is_percent: bool
    context: str
    start: int
    #: 回指时允许的偏差。缺省按写出的小数位数算；中文数字、带万亿后缀的数有自己的书写精度
    tolerance: float | None = None


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


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - 18) : min(len(text), end + 18)].replace("\n", " ")


def extract_numbers(text: str) -> list[NumberToken]:
    """抽出叙述里所有需要回指的数字，剔除日期、枚举序号、引用标记这类形态。"""
    skip = _skip_spans(text)
    tokens: list[NumberToken] = []
    taken: list[tuple[int, int]] = []
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
        decimals = len(raw_num.split(".")[1]) if "." in raw_num else 0
        tolerance = None
        if suffix in _SUFFIX_MULT:
            value *= _SUFFIX_MULT[suffix]
            # 书写精度跟着后缀走：3.5万写到千位，容差 ±500，而不是 3.5 那一位小数的 ±0.05
            tolerance = 0.5 * 10 ** (-decimals) * _SUFFIX_MULT[suffix]
        taken.append((start, end))
        tokens.append(
            NumberToken(
                raw=m.group(0).strip(),
                value=value,
                decimals=decimals,
                is_percent=suffix in ("%", "％") or text[max(0, start - 3) : start] == "百分之",
                context=_context(text, start, end),
                start=start,
                tolerance=tolerance,
            )
        )
    tokens.extend(_cn_numbers(text, skip + taken))
    tokens.sort(key=lambda t: t.start)
    return tokens


def _cn_not_a_quantity(numeral: str, suffix: str, value: float, before: str, after: str) -> bool:
    """这串中文数字字是不是在报一个量。规则见文件头"中文数字"一节。"""
    if suffix in ("成", "倍", "个百分点"):
        return False                                   # 三成、两倍：比例写法，单字也是量
    if before.endswith("第"):
        return True                                    # 第三
    if before.endswith(("周", "星期", "礼拜")) and len(numeral) == 1:
        return True                                    # 周三
    if before[-1:] in ("几", "数", "上") or after.startswith("几"):
        return True                                    # 几十、数百、上千、十几：虚指
    if not suffix:
        if (after.startswith("月") and value <= 12) or (after.startswith(("日", "号")) and value <= 31):
            return True                                # 十二月、十五日
        if after.startswith("季度"):
            return True                                # 三季度 = Q3（"三个季度"不走这里）
        if numeral in ("十一", "五一") and after.startswith(("假", "长假", "期间", "黄金周")):
            return True
        if _CN_STRUCTURAL.match(after):
            return True                                # 从三个方面看
    # 单字，或只有单位没有数字的（千万、百万）：后面跟着计数单位才是量。
    # "一"再加一道：一个问题、一次调用多半是冠词用法
    bare = len(numeral) == 1 or not any(c in _CN_DIGIT for c in numeral)
    if bare and "点" not in numeral:
        if numeral == "一":
            return True
        if not _CN_COUNT_UNIT.match(after):
            return True                                # 五花八门、十分重要、千方百计、千万不要
    return False


def _cn_numbers(text: str, blocked: list[tuple[int, int]]) -> list[NumberToken]:
    tokens: list[NumberToken] = []

    def add(m: re.Match[str], value: float, tolerance: float, *, is_percent: bool) -> None:
        tokens.append(NumberToken(
            raw=m.group(0), value=value, decimals=0, is_percent=is_percent,
            context=_context(text, m.start(), m.end()), start=m.start(), tolerance=tolerance,
        ))

    for m in _CN_RE.finditer(text):
        start, end = m.span()
        if any(s < end and start < e for s, e in blocked):
            continue
        if m.group("pct"):                             # 百分之三十：和 30% 同一个精度
            value = cn_to_number(m.group("pct"))
            if value is not None:
                decimals = len(m.group("pct").partition("点")[2])
                add(m, value, 0.5 * 10 ** -decimals, is_percent=True)
            continue
        if m.group("den"):                             # 三分之一：按百分数、±5 个点回指
            den, numr = cn_to_number(m.group("den")), cn_to_number(m.group("numr"))
            if den and numr is not None:
                add(m, numr / den * 100, 5, is_percent=True)
            continue
        if m.group("half"):                            # 一半
            add(m, 50, 5, is_percent=True)
            continue

        numeral, suffix = m.group("num"), m.group("suffix") or ""
        value = cn_to_number(numeral)
        if value is None:
            continue
        if _cn_not_a_quantity(numeral, suffix, value, text[max(0, start - 2) : start], text[end : end + 8]):
            continue
        if suffix == "成":                             # 三成 = 30%，口语精度半成
            add(m, value * 10, 5, is_percent=True)
        elif suffix in ("余", "多", "来"):             # 两千余 = [2000, 3000]
            step = 1
            while value >= step * 10 and value % (step * 10) == 0:
                step *= 10
            add(m, value + step / 2, step / 2, is_percent=False)
        else:
            add(m, value, 0.5 * _cn_precision(numeral, value), is_percent=suffix == "个百分点")
    return tokens


def _tolerance(token: NumberToken) -> float:
    """按 token 写出的精度给舍入容差：写了 12.3 就允许 |Δ|≤0.05。"""
    if token.tolerance is not None:
        return token.tolerance
    return 0.5 * 10 ** (-token.decimals)


def _close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance + 1e-9


def _token_matches(token: NumberToken, metric_value: float) -> bool:
    tolerance = _tolerance(token)
    # 直接比对（含"叙述写百分数、指标本身就是百分数"的情形）
    if _close(token.value, metric_value, tolerance):
        return True
    if token.is_percent:
        # 指标存的是比率（0.123），叙述写成 12.3%
        if _close(token.value, metric_value * 100, tolerance):
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
        if any(_close(token.value, av, _tolerance(token)) for av in allowed_values):
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
    formal（完整出具）：清单齐、数字全部可回指，**且校验确实发生过**。

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
