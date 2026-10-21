"""判定语、判定标准、附带说明：一种说法，只在这里定义（细案 01 §7）。

判定语由系统渲染（[[a:A3]] 渲染成结论里的 phrase），写作者只引用、不改写——「叙述层无算术」往前一步：分析判定和数字一样
由系统写。前后端都只读结论里渲染好的字符串，所以模板、数的写法、附带说明的那一句都在这里，算子只填关节。

**数的写法**（和 engine/evidence.render_cell 同一种风格：Decimal 式的四舍五入 ROUND_HALF_UP，千分位用「,」）：

- 百分比：一位小数、带正负号（+8.7%、-3.1%，0 写 0.0%）；占比、比重不带正号（占 61.9%）。不是 0 的值写出来成了 0.0%
  的，多给小数位（最多 4 位），不写一个假的 0。
- 量（金额、件数）：千分位，最多两位小数、去尾零；绝对值小于 1 的保留两位有效数字（最多 6 位小数），同样不写假的 0。
- 百分点：比率的变化（2.1% 到 2.5% 是上升 0.4 个百分点）。

**label 消毒**（§7.3）：判定语里的度量叫法、因子名、维度叫法是 agent 或数据给的。agent 给的 label（以及目录里的叫法、
当叫法用的列名）去掉数字、百分号、单位符号、引用标记，不超过 20 字，消毒后为空就退回下一个来源：不然模型可以把
「销售额（同比 +30%）」塞进 label，借系统渲染绕过裸数字检测。去数字按出具校验抽数字的同一个函数（engine/issuance 的
extract_numbers：阿拉伯数字、中文数字、带万 / 亿 / 成 / 倍的写法），再去掉剩下的数字字符。维度值是数据，照原样。

**附带说明**：判定语后面用「；」接，最多两条（按 PHRASE_ORDER 的先后），其余只在证据面板。度量类型没标、以各段之和为
总数这类方法上的交代不进判定语。
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from typing import Any

from app.analysis.numbers import fixed, to_fraction

#: 算子的叫法：「算不了」的判定语、报错都用它
OP_NAMES = {"decompose": "变化拆解", "baseline": "波动基线", "trend": "趋势判断", "differ": "差异检验",
            "comparable": "可比性检查"}
#: 附带说明进判定语的先后；不在这里的（measure_unlabeled、headline_basis）只在证据面板
PHRASE_ORDER = ("comparability", "simpson", "offset", "outlier_sensitive", "small_sample", "record_high", "record_low",
                "multiple_comparisons", "weekday_effect", "small_denominator", "null_values")
#: 判定语里最多接几条附带说明
PHRASE_CAVEATS = 2
#: 消毒后的叫法最多几个字
LABEL_MAX = 20
#: 维度键有两列时，一段的写法（华东·线上）
KEY_JOIN = "·"
#: 维度值为空时的写法
EMPTY_KEY = "（空）"

# --------------------------------------------------------------------------
# 拼字：汉字和英文、数字之间加一个空格，挨着全角标点的不加（同 data/sql_semantics._say）
# --------------------------------------------------------------------------

_CJK_PUNCT = frozenset("，。；：、！？「」『』（）《》【】…—·")


def _wide(ch: str) -> bool:
    return "⺀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


def say(*parts: str | None) -> str:
    """拼一句话：汉字和英文、数字之间补一个空格，挨着全角标点、空白的不补，汉字和汉字之间不补。"""
    out = ""
    for part in parts:
        if not part:
            continue
        if out and not out[-1].isspace() and not part[0].isspace():
            a, b = out[-1], part[0]
            if not (a in _CJK_PUNCT or b in _CJK_PUNCT or (_wide(a) and _wide(b)) or (not _wide(a) and not _wide(b))):
                out += " "
        out += part
    return out


# --------------------------------------------------------------------------
# 数
# --------------------------------------------------------------------------


def _q(value: Any) -> Fraction:
    q = value if isinstance(value, Fraction) else to_fraction(value)
    if q is None:
        raise ValueError(f"不是数：{value!r}")
    return q


def _nonzero_fixed(q: Fraction, places: int, *, most: int, strip: bool, group: bool = False) -> str:
    """按 places 位小数写；不是 0 的值写出来成了 0 的，逐位多给，最多 most 位。"""
    for p in range(places, most + 1):
        text = fixed(q, p, strip=strip if p == places else True, group=group)
        if q == 0 or text.strip("-0.,"):
            return text
    return fixed(q, most, strip=True, group=group)


def pct(value: Any, *, signed: bool = True) -> str:
    """比率写成百分比：一位小数；signed 时正数带「+」（0 不带）。"""
    q = _q(value) * 100
    text = _nonzero_fixed(q, 1, most=4, strip=False)
    if signed and q > 0 and not text.startswith("-"):
        text = "+" + text
    return text + "%"


def points(value: Any) -> str:
    """比率的变化写成百分点（不带符号，调用方写上升 / 下降）：0.004 → 0.4。"""
    return _nonzero_fixed(abs(_q(value)) * 100, 1, most=4, strip=True)


def amount(value: Any, *, signed: bool = False) -> str:
    """量：千分位，最多两位小数、去尾零；绝对值小于 1 的保留两位有效数字（最多 6 位小数）。signed 时正数带「+」。"""
    q = _q(value)
    places = 2
    if q != 0 and abs(q) < 1:
        places = max(2, -math.floor(math.log10(float(abs(q)))) + 1)
        places = min(places, 6)
    text = _nonzero_fixed(q, places, most=6, strip=True, group=True)
    if signed and q > 0:
        text = "+" + text
    return text


def threshold_pct(value: Any) -> str:
    """阈值写成百分比：去尾零、不带正号（0.005 → 0.5%，0.8 → 80%）。"""
    return fixed(_q(value) * 100, 4, strip=True) + "%"


def share_words(value: Any) -> str:
    """阈值写进判定标准：0.5 写「一半」，其余写百分比。"""
    q = _q(value)
    return "一半" if q == Fraction(1, 2) else threshold_pct(q)


def number(value: Any) -> str:
    """阈值本身（倍数、显著性水平、期数）：去尾零、不带千分位。"""
    return fixed(_q(value), 6, strip=True)


def change(delta: Any, base: Any) -> str:
    """一个量的变化：下降 194（-8.7%）/ 上升 120（+5.3%）；基期为 0 不给百分比；没变写「没有变化」。"""
    d = _q(delta)
    if d == 0:
        return "没有变化"
    word = "上升" if d > 0 else "下降"
    b = _q(base) if base is not None else Fraction(0)
    tail = f"（{pct(d / abs(b))}）" if b != 0 else ""
    return f"{word} {amount(abs(d))}{tail}"


def ratio_change(r_base: Any, r_target: Any, *, proportion: bool) -> str:
    """一个比率的变化。proportion（各段的分子都不超过分母，比率是一个占比，如退款率）写百分点：上升 0.4 个百分点（2.1% 到
    2.5%）；其余（客单价这类）按量写：上升 3.2（+3.9%）。"""
    rb, rt = _q(r_base), _q(r_target)
    d = rt - rb
    if not proportion:
        return change(d, rb)
    if d == 0:
        return f"没有变化（{pct(rt, signed=False)}）"
    word = "上升" if d > 0 else "下降"
    return f"{word} {points(d)} 个百分点（{pct(rb, signed=False)} 到 {pct(rt, signed=False)}）"


def ratio_value(value: Any, *, proportion: bool) -> str:
    """比率本身：占比写百分比，其余按量写。"""
    return pct(value, signed=False) if proportion else amount(value)


# --------------------------------------------------------------------------
# 叫法与段
# --------------------------------------------------------------------------

_MARKER = re.compile(r"\[\[[^\]\n]*\]\]|\[\[|\]\]")
_DIGITS = re.compile(r"[0-9０-９]")
_SYMBOLS = re.compile(r"[%％‰‱¥￥$＄€£₩℃°+＋\-−–±×÷=＝<>＜＞~～^*]")
_EMPTY_BRACKETS = re.compile(r"[（(【\[]\s*[)）】\]]")
_EDGE_PUNCT = "，,、：:；;。.！!？? 　"


def clean_label(text: Any) -> str:
    """消毒（见文件头）：去引用标记、数字（出具校验认的数字写法连同后缀，再加剩下的数字字符）、百分号和单位符号、空括号，
    压掉多余空白，不超过 LABEL_MAX 字。可能消成空串。"""
    if not isinstance(text, str):
        return ""
    from app.engine.issuance import extract_numbers

    raw = _MARKER.sub("", text)
    spans = sorted((n.start, n.end) for n in extract_numbers(raw))
    kept, pos = [], 0
    for start, end in spans:
        if start >= pos:
            kept.append(raw[pos:start])
            pos = end
    kept.append(raw[pos:])
    out = _SYMBOLS.sub("", _DIGITS.sub("", "".join(kept)))
    # 去掉数字以后留下的空白、标点：括号、标点两边的空白，括号里头尾的标点，连着的几个标点
    out = re.sub(r"\s*([（）()【】\[\]，,、：:；;])\s*", r"\1", out)
    out = re.sub(r"[，,、：:；;]+(?=[）)】\]])", "", out)
    out = re.sub(r"(?<=[（(【\[])[，,、：:；;]+", "", out)
    out = re.sub(r"([，,、：:；;])[，,、：:；;]+", r"\1", out)
    while _EMPTY_BRACKETS.search(out):
        out = _EMPTY_BRACKETS.sub("", out)
    out = re.sub(r"\s+", " ", out).strip(_EDGE_PUNCT)
    return out[:LABEL_MAX].strip(_EDGE_PUNCT)


def pick_label(*candidates: Any, fallback: str = "这一列") -> str:
    """依次取第一个消毒后不为空的叫法；都为空用 fallback。"""
    for c in candidates:
        cleaned = clean_label(c)
        if cleaned:
            return cleaned
    return fallback


def key_text(key: Sequence[Any] | Any) -> str:
    """一段（维度键）的写法：一列就是那个值，两列用「·」连（华东·线上）；空值写「（空）」。数据里的值照原样（压成一行）。"""
    parts = key if isinstance(key, (list, tuple)) else [key]
    shown = []
    for p in parts:
        if p is None:
            shown.append(EMPTY_KEY)
        elif isinstance(p, bool):
            shown.append("是" if p else "否")
        else:
            shown.append(re.sub(r"\s+", " ", str(p)).strip() or EMPTY_KEY)
    return KEY_JOIN.join(shown)


def names(items: Iterable[str], limit: int | None = None) -> str:
    """几段的写法：华东、华北；超过 limit 段时写前 limit 段加「等 n 段」。"""
    items = list(items)
    if limit is not None and len(items) > limit:
        return "、".join(items[:limit]) + f"等 {len(items)} 段"
    return "、".join(items)


def dims_text(labels: Sequence[str]) -> str:
    """维度的叫法：地区；两列时写「地区、渠道」。"""
    return "、".join(labels)


# --------------------------------------------------------------------------
# 附带说明（caveats[].text）：一种说法
# --------------------------------------------------------------------------


def caveat(code: str, text: str) -> dict[str, str]:
    return {"code": code, "text": text}


def offset_text(delta_total: Any, gross_up: Any, gross_down: Any) -> str:
    """抵消：和总变化方向相反的段合计多少（下降的段合计 -120，上升的段合计 +40，互相抵消了一部分）。"""
    up, down = amount(gross_up, signed=True), amount(gross_down, signed=True)
    if _q(delta_total) >= 0:
        return f"上升的段合计 {up}，下降的段合计 {down}，互相抵消了一部分"
    return f"下降的段合计 {down}，上升的段合计 {up}，互相抵消了一部分"


def simpson_text(label: str, overall_up: bool, subject: str, w_base: Any, w_target: Any) -> str:
    """辛普森：各段的比率大多和整体反着变，整体的变化来自结构。"""
    seg = "上升" if not overall_up else "下降"
    whole = "上升" if overall_up else "下降"
    move = "升" if _q(w_target) > _q(w_base) else "降"
    return say(f"各段的", label, f"大多在{seg}，整体却{whole}：变化来自结构，", subject,
               f"占比从 {pct(w_base, signed=False)} {move}到 {pct(w_target, signed=False)}")


def small_denominator_text(segments: Sequence[str], denominator: str, minimum: int) -> str:
    return say(names(segments, 3), "的", denominator, f"不到 {minimum}，比率波动大，不参与多数判断")


def null_segments_text(segments: Sequence[str], label: str) -> str:
    return say(names(segments, 3), "的", label, "为空，按 0 算")


def null_periods_text(count: int, label: str) -> str:
    return say(f"有 {count} 期的", label, "为空，跳过了这几期")


def unlabeled_text(column: str, tail: str) -> str:
    return say("目录没标「", column, "」的度量类型，", tail)


def unconfirmed_text(column: str, measure: str, tail: str) -> str:
    return say("目录里「", column, f"」是{MEASURE_WORDS.get(measure, measure)}只是推断，没人确认，", tail)


MEASURE_WORDS = {"flow": "流量", "stock": "存量", "ratio": "比率", "identifier": "标识", "status": "状态", "attribute": "属性"}
HEADLINE_BASIS = "没给总数，以各段之和为总数"
WEEKDAY_EFFECT = "日数据有星期效应，看趋势建议按周汇总"


def record_text(high: bool, label: str, n: int, unit: str) -> str:
    return say("本期的", label, f"是过去 {n} {unit}里{'最高' if high else '最低'}的")


def multiple_text(pairs: int) -> str:
    return f"比了 {pairs} 对，已按次数校正"


def outlier_text(largest: bool, now_differs: bool) -> str:
    which = "最大" if largest else "最小"
    return f"去掉{which}的一笔后，差异{'变得站得住' if now_differs else '不再站得住'}"


def small_sample_rate_text(minimum: int) -> str:
    return f"样本少（有格子的期望频数不到 {minimum}），用了精确检验"


def small_sample_mean_text(group: str, n: int, minimum: int) -> str:
    return say("有一组的样本数不到 ", str(minimum), "（", group, f" {n}），结论不稳")


UNVERIFIED_COMPLETE = "没能核对本期的数据是否到齐（查不到表里这一列的日期范围）"


def with_caveats(main: str, caveats: Sequence[Mapping[str, str]], *, skip: Iterable[str] = ()) -> str:
    """判定语接上最多 PHRASE_CAVEATS 条附带说明（按 PHRASE_ORDER 的先后，用「；」接）。skip 里的那几条（已经在正文里说过的、
    只在证据面板说的）不接。"""
    skipped = set(skip)
    ranked = sorted((PHRASE_ORDER.index(c["code"]), i, c["text"]) for i, c in enumerate(caveats)
                    if c["code"] in PHRASE_ORDER and c["code"] not in skipped)
    texts = list(dict.fromkeys(t for _, _, t in ranked))[:PHRASE_CAVEATS]
    return "；".join([main, *texts])


def refused(op: str, reason: str) -> str:
    """算不了的判定语：{算子的叫法}没有做成：{原因}。"""
    return f"{OP_NAMES[op]}没有做成：{reason}"


# --------------------------------------------------------------------------
# 判定语模板（§7.2）
# --------------------------------------------------------------------------


def decompose_driver(label: str, total: str, subject: str, subject_change: str, share: Any) -> str:
    return say(label, total, "，主要来自", subject, f"（{subject_change}，占 {pct(share, signed=False)}）")


def decompose_concentrated(label: str, total: str, subjects: str, share: Any) -> str:
    return say(label, total, "，集中在", subjects, f"（合计占 {pct(share, signed=False)}）")


def decompose_dispersed(label: str, total: str, dims: str, main_share: Any) -> str:
    return say(label, total, "，分散在各", dims, "，没有哪一段占到", share_words(main_share))


def decompose_flat(label: str, pct_total: Any | None, offset: tuple[str, str, str, str] | None) -> str:
    """基本持平；有抵消时接「，内部此消彼长：华东 +120、华北 -118」（最大的上升段、最大的下降段）。"""
    head = say(label, "基本持平") + (f"（{pct(pct_total)}）" if pct_total is not None else "")
    if offset is None:
        return head
    up_key, up_change, down_key, down_change = offset
    return head + say("，内部此消彼长：", up_key, f" {up_change}、", down_key, f" {down_change}")


def decompose_unclosed(dims: str, gap: Any, *, ratio: bool) -> str:
    what = "各段合起来的比率和总数对不上" if ratio else "各段之和和总数对不上"
    return say("按", dims, f"拆开后{what}（差 {amount(abs(_q(gap)))}），不判断变化来自哪里")


def ratio_mix(label: str, total: str, subject: str, w_base: Any, w_target: Any) -> str:
    return say(label, total, "，主要来自结构变化：", subject,
               f"占比从 {pct(w_base, signed=False)} 到 {pct(w_target, signed=False)}")


def ratio_rate(label: str, total: str, subject: str) -> str:
    return say(label, total, "，主要来自各段自身的变化，", subject, "影响最大")


def ratio_both(label: str, total: str, mix_share: Any, rate_share: Any) -> str:
    return say(label, total, f"，结构变化和各段自身变化都有影响（各占 {pct(mix_share, signed=False)}、"
                             f"{pct(rate_share, signed=False)}）")


def factor_driver(label: str, total: str, factor: str, contrib: Any, share: Any) -> str:
    return say(label, total, "，主要来自", factor, f"的变化（贡献 {amount(contrib, signed=True)}，占 "
                                               f"{pct(share, signed=False)}）")


def factor_both(label: str, total: str, parts: Sequence[tuple[str, Any]]) -> str:
    """乘积拆解里没有哪个因子占到「主要」：列出各因子占的比重。"""
    shown = "、".join(say(name, f"占 {pct(share, signed=False)}") for name, share in parts)
    return say(label, total, f"，几个因子都有影响（{shown}）")


def band_text(lo: Any, hi: Any, *, basis: str) -> str:
    fmt = (lambda v: pct(v)) if basis == "change" else (lambda v: amount(v))
    return f"{fmt(lo)} 至 {fmt(hi)}"


def current_text(x: Any, *, basis: str, lag: int, grain: str) -> str:
    """本期：变化率（和比较期比）或值本身。日数据 lag 7 写「比上周同一天」，lag 52 周 / 12 月 / 4 季写「同比」。"""
    if basis == "level":
        return f"本期为 {amount(x)}"
    if lag == 1:
        lead = "本期变化"
    elif grain == "day" and lag == 7:
        lead = "比上周同一天变化"
    elif (grain, lag) in (("week", 52), ("month", 12), ("quarter", 4)):
        lead = "同比变化"
    else:
        lead = f"比 {lag} 期前变化"
    return f"{lead} {pct(x)}"


def baseline_phrase(verdict: str, label: str, current: str, n: int, unit: str, band: str) -> str:
    where = {"within_band": "在过去 {n} {u}的常规波动范围内", "outside_band": "超出过去 {n} {u}的常规波动范围",
             "far_outside_band": "明显超出过去 {n} {u}的常规波动范围"}[verdict].format(n=n, u=unit)
    return say(label, current, f"，{where}（{band}）")


def baseline_flat_phrase(verdict: str, label: str, current: str, n: int, unit: str, per: str, value: str) -> str:
    """历史每期都一样（稳健标准差为 0）。"""
    if verdict == "within_band":
        return say(label, current, f"，和过去 {n} {unit}一样（每{per}都是 {value}）")
    return say(label, current, f"，明显超出过去 {n} {unit}的常规波动范围（每{per}都是 {value}）")


def trend_phrase(verdict: str, label: str, n: int, unit: str, per: str, slope: str | None,
                 run: tuple[int, str] | None) -> str:
    if verdict == "no_trend":
        return say(f"过去 {n} {unit}", label, "没有明显的上升或下降趋势")
    word = "上升" if verdict == "rising" else "下降"
    text = say(f"过去 {n} {unit}", label, f"呈{word}趋势") + (f"（每{per}约 {slope}）" if slope else "")
    if run is not None:
        length, direction = run
        text += f"，已连续 {length} {unit}{'上升' if direction == 'up' else '下降'}"
    return text


def differ_pair(a: str, b: str, label: str, va: str, vb: str, higher: bool) -> str:
    """一对的差异站得住：西南的退款率（5.0%）高于华东（2.1%）。"""
    return say(a, "的", label, f"（{va}）{'高于' if higher else '低于'}", b, f"（{vb}）")


def differ_phrase(pairs: Sequence[str], total: int) -> str:
    """差异站得住。比了几对时写「比了 4 对，2 对差异站得住：…」。"""
    if total == 1:
        return f"{pairs[0]}，差异站得住"
    shown = "；".join(pairs[:2]) + ("等" if len(pairs) > 2 else "")
    return f"比了 {total} 对，{len(pairs)} 对差异站得住：{shown}"


def no_evidence_phrase(a: str | None, b: str | None, label: str, sizes: tuple[str, str] | None, total: int) -> str:
    if total == 1 and a is not None and b is not None and sizes is not None:
        return say(a, "和", b, "的", label, f"差异可能只是样本波动（样本量 {sizes[0]}、{sizes[1]}）")
    return say(f"比了 {total} 对，", label, "的差异可能都只是样本波动")


def comparable_phrase(verdict: str, reasons: Sequence[str], unknown: Sequence[str]) -> str:
    if verdict == "incomparable":
        return "两期不可比：" + "；".join(reasons)
    if verdict == "conditional":
        return "两期有条件可比：" + "；".join(reasons)
    return "两期可比" + (f"，{'、'.join(unknown)}核对不了" if unknown else "")


# --------------------------------------------------------------------------
# 判定标准（criterion）：照实际阈值写的一句
# --------------------------------------------------------------------------


def criterion_additive(s: Mapping[str, Any], *, top_k_max: int | None, summed: bool) -> str:
    k = f"最多前 {top_k_max} 段" if top_k_max else "前几段"
    text = say("一段占总变化的", share_words(s["main_share"]), "以上算主要来源，", k, "合计占",
               share_words(s["concentration"]), "以上算集中，总变化不到基期的", threshold_pct(s["flat_pct"]),
               "算基本持平")
    return text + ("，以各段之和为总数" if summed else "，各段之和要和总数对得上才判断变化来自哪里")


def criterion_ratio(s: Mapping[str, Any]) -> str:
    return say("结构变化或各段自身变化占比率变化的", share_words(s["main_share"]), "以上算主要来自它，比率变化不到基期的",
               threshold_pct(s["flat_pct"]), f"算基本持平，分母不到 {number(s['min_denominator'])} 的段不参与多数判断")


def criterion_factor(s: Mapping[str, Any]) -> str:
    return say("按对数平均拆分（LMDI），一个因子的贡献占总变化的", share_words(s["main_share"]),
               "以上算主要来自它，总变化不到基期的", threshold_pct(s["flat_pct"]), "算基本持平")


def criterion_baseline(s: Mapping[str, Any], n: int | None, unit: str) -> str:
    span = f"过去 {n} {unit}" if n else f"过去最多 {number(s['max_history'])} 期"
    warn, alert = number(s["k_warn"]), number(s["k_alert"])
    return (f"本期和{span}的中位数比，差不到 {warn} 倍稳健标准差（1.4826 倍的中位绝对偏差）算常规波动，{warn} 到 "
            f"{alert} 倍算超出，{alert} 倍以上算明显超出；历史至少要 {number(s['min_history'])} 期")


def criterion_trend(s: Mapping[str, Any], n: int | None, unit: str) -> str:
    span = f"最后 {n} {unit}" if n else f"最后最多 {number(s['max_history'])} 期"
    return (f"用{span}（至少 {number(s['trend_min_points'])} 期）：Mann–Kendall 检验的双侧 p 值小于 "
            f"{number(s['trend_alpha'])} 时按 Theil–Sen 斜率的正负判上升或下降，否则判没有明显趋势")


def criterion_differ(s: Mapping[str, Any], mode: str) -> str:
    alpha = number(s["differ_alpha"])
    if mode == "rate":
        return (f"双侧 p 值（比了几对时按 Holm 校正）小于 {alpha} 算差异站得住；有格子的期望频数不到 "
                f"{number(s['min_expected'])} 时用 Fisher 精确检验，否则用合并比例的 z 检验")
    return (f"Welch t 检验的双侧 p 值（比了几对时按 Holm 校正）小于 {alpha} 算差异站得住；一组样本数不到 "
            f"{number(s['min_n'])} 算样本少")


CRITERION_COMPARABLE = ("只差日期、窗口一样长、是整期、星期构成相同、两边查询都没有数据问题才算可比；只有提醒项算有条件"
                        "可比，有一项不通过算不可比；核对不了的项不判不可比")


__all__ = [
    "CRITERION_COMPARABLE", "EMPTY_KEY", "HEADLINE_BASIS", "KEY_JOIN", "LABEL_MAX", "MEASURE_WORDS", "OP_NAMES",
    "PHRASE_CAVEATS", "PHRASE_ORDER", "UNVERIFIED_COMPLETE", "WEEKDAY_EFFECT", "amount", "band_text",
    "baseline_flat_phrase", "baseline_phrase", "caveat", "change", "clean_label", "comparable_phrase",
    "criterion_additive", "criterion_baseline", "criterion_differ", "criterion_factor", "criterion_ratio",
    "criterion_trend", "current_text", "decompose_concentrated", "decompose_dispersed", "decompose_driver",
    "decompose_flat", "decompose_unclosed", "differ_pair", "differ_phrase", "dims_text", "factor_both",
    "factor_driver", "key_text", "multiple_text", "names", "no_evidence_phrase", "null_periods_text",
    "null_segments_text", "number", "offset_text", "outlier_text", "pct", "pick_label", "points", "ratio_both",
    "ratio_change", "ratio_mix", "ratio_rate", "ratio_value", "record_text", "refused", "say", "share_words",
    "simpson_text", "small_denominator_text", "small_sample_mean_text", "small_sample_rate_text", "threshold_pct",
    "trend_phrase", "unconfirmed_text", "unlabeled_text", "with_caveats",
]
