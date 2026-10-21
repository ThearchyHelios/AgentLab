"""分析用的统计函数（docs/design/analysis-round/01-算子与判定.md §3–§5，总方案 D13）：纯 Python，不加依赖、不用 numpy。

为什么不装 scipy：几十 MB 的依赖只为几个函数，本机装包慢；评测本来就有一套纯 Python 的统计（Wilson 区间、精确 McNemar），
两边要是同一套。所以 Wilson 从 eval/stats 搬到这里，评测改为引用（行为不变）。每个函数都用公认的表值钉住
（tests/test_analysis_stats.py：正态 0.975 分位 1.959964、t(10) 双侧 0.05 临界值 2.228139、Fisher 品茶表双侧 0.4857……）。

- 正态：分布函数用 math.erfc；分位数用 Acklam 的有理逼近再按分布函数做两步 Halley 修正（到浮点精度）。
- t 分布：分布函数用正则化不完全 β 函数（连分式，Lentz 法）；自由度很大（> 1e7）时和正态差在 1e-7 以内，直接按正态算；
  分位数按分布函数二分求逆。
- 比率：Wilson 区间（缺省按评测的写法四舍五入到 4 位，places=None 不取整）、两比率之差的 Newcombe 区间（混合 score
  法，即 Newcombe 1998 的方法 10）、合并比例的 z 检验、Fisher 精确检验（超几何概率用 math.lgamma 累加，双侧按「不比
  观测到的那张表更可能」的表求和，比较时留 1e-7 的相对余量，同 R 和 scipy）。
- 均值：Welch t 检验，自由度按 Welch–Satterthwaite。
- 多重比较：Holm 逐步校正。
- 趋势：Mann–Kendall（n ≤ 10 且没有并列时用精确分布：S 的分布由逆序数的分布递推算出，不查表；否则正态近似，方差按
  并列修正，带连续性校正）、Theil–Sen 斜率（两两斜率的中位数）。
- 中位数、中位绝对偏差、名次。

能精确算的照精确算：中位数、MAD、Theil–Sen 对 Fraction 输入给 Fraction，Mann–Kendall 的精确 p 是 Fraction。浮点只出现在
要开方、取对数、查分布的地方，结论里的数再按 analysis/numbers 统一成规范的十进制字符串。
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from fractions import Fraction
from typing import Any, NamedTuple

#: 95% 区间的 z（正态 0.975 分位）
Z95 = 1.959963984540054
_SQRT2 = math.sqrt(2.0)
#: t 分布自由度大到这个数以上时按正态算（相对误差约 1/自由度）
_T_AS_NORMAL_DF = 1e7

# --------------------------------------------------------------------------
# 正态分布
# --------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    """标准正态分布函数 Φ(x)。"""
    return 0.5 * math.erfc(-x / _SQRT2)


def normal_two_sided(z: float) -> float:
    """标准正态的双侧尾概率 P(|Z| ≥ |z|)。"""
    return math.erfc(abs(z) / _SQRT2)


_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
      -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
      -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
      4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)


def normal_ppf(p: float) -> float:
    """标准正态的 p 分位数 Φ⁻¹(p)。p ≤ 0 为负无穷，p ≥ 1 为正无穷。"""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    low = 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    elif p <= 1 - low:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1)
    else:
        q = math.sqrt(-2 * math.log1p(-p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    for _ in range(2):
        # Halley 修正：误差按分布函数现算，两步就到浮点精度
        e = (normal_cdf(x) - p) if p < 0.5 else -(0.5 * math.erfc(x / _SQRT2) - (1 - p))
        u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
        x = x - u / (1 + x * u / 2)
    return x


# --------------------------------------------------------------------------
# 正则化不完全 β 函数与 t 分布
# --------------------------------------------------------------------------

_CF_MAX_ITER = 20_000
_CF_EPS = 1e-15
_CF_TINY = 1e-300


def _beta_cf(a: float, b: float, x: float) -> float:
    """不完全 β 函数的连分式（Lentz 法，Numerical Recipes 的 betacf）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) >= _CF_TINY else _CF_TINY)
    h = d
    for m in range(1, _CF_MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) >= _CF_TINY else _CF_TINY)
        c = 1.0 + aa / c
        c = c if abs(c) >= _CF_TINY else _CF_TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) >= _CF_TINY else _CF_TINY)
        c = 1.0 + aa / c
        c = c if abs(c) >= _CF_TINY else _CF_TINY
        step = d * c
        h *= step
        if abs(step - 1.0) < _CF_EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 β 函数 I_x(a, b)，a、b > 0，0 ≤ x ≤ 1。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_front) * _beta_cf(a, b, x) / a
    return 1.0 - math.exp(log_front) * _beta_cf(b, a, 1.0 - x) / b


def t_two_sided(t: float, df: float) -> float:
    """自由度 df 的 t 分布的双侧尾概率 P(|T| ≥ |t|)。"""
    if math.isinf(t):
        return 0.0
    if df > _T_AS_NORMAL_DF or math.isinf(df):
        return normal_two_sided(t)
    return min(1.0, betainc(df / 2.0, 0.5, df / (df + t * t)))


def t_cdf(t: float, df: float) -> float:
    """自由度 df 的 t 分布的分布函数。"""
    tail = t_two_sided(t, df) / 2.0
    return 1.0 - tail if t >= 0 else tail


def t_ppf(p: float, df: float) -> float:
    """自由度 df 的 t 分布的 p 分位数：按分布函数二分求逆。"""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    if p == 0.5:
        return 0.0
    if df > _T_AS_NORMAL_DF or math.isinf(df):
        return normal_ppf(p)
    target = min(p, 1.0 - p)
    lo, hi = -1.0, 0.0
    while t_cdf(lo, df) > target:
        lo, hi = lo * 2.0, lo
        if lo < -1e12:
            break
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) > target:
            hi = mid
        else:
            lo = mid
        if hi - lo <= 1e-15 * max(1.0, abs(mid)):
            break
    x = (lo + hi) / 2.0
    return x if p < 0.5 else -x


# --------------------------------------------------------------------------
# 比率
# --------------------------------------------------------------------------


def wilson(passed: int, n: int, z: float = Z95, *, places: int | None = 4) -> tuple[float, float] | None:
    """Wilson 区间（缺省 95%）。n 为 0 时没有区间。places 是四舍五入的位数（缺省 4 位，评测一直这样显示）；None 不取整。"""
    if n <= 0:
        return None
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low, high = max(0.0, centre - half), min(1.0, centre + half)
    if places is None:
        return low, high
    return round(low, places), round(high, places)


def newcombe(x1: int, n1: int, x2: int, n2: int, z: float = Z95) -> tuple[float, float] | None:
    """两比率之差 p1 − p2 的 Newcombe 区间（混合 score 法：两边各自的 Wilson 区间合成）。有一边样本为 0 时没有区间。"""
    w1, w2 = wilson(x1, n1, z, places=None), wilson(x2, n2, z, places=None)
    if w1 is None or w2 is None:
        return None
    p1, p2 = x1 / n1, x2 / n2
    (l1, u1), (l2, u2) = w1, w2
    diff = p1 - p2
    delta = math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    epsilon = math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return diff - delta, diff + epsilon


def pooled_z(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float] | None:
    """两比率相等的合并比例 z 检验：(z, 双侧 p)。有一边样本为 0 时为 None；合并比例是 0 或 1（两边全是或全不是）时 z 为 0、
    p 为 1。"""
    if n1 <= 0 or n2 <= 0:
        return None
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (x1 / n1 - x2 / n2) / se
    return z, normal_two_sided(z)


def min_expected(x1: int, n1: int, x2: int, n2: int) -> float:
    """2×2 表（成功、失败 × 两组）各格的期望频数里最小的那个。"""
    total = n1 + n2
    if total <= 0:
        return 0.0
    successes, failures = x1 + x2, total - x1 - x2
    return min(r * c / total for r in (n1, n2) for c in (successes, failures))


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """2×2 表 [[a, b], [c, d]] 的 Fisher 精确检验，双侧 p：边际固定时，不比观测到的表更可能出现的那些表的概率之和。"""
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2
    if n == 0:
        return 1.0
    lo, hi = max(0, c1 - r2), min(r1, c1)
    base = _log_comb(n, c1)

    def log_p(x: int) -> float:
        return _log_comb(r1, x) + _log_comb(r2, c1 - x) - base

    observed = log_p(a) + 1e-7          # 相对余量 1 + 1e-7（对数里约等于加 1e-7），同 R 和 scipy
    total = sum(math.exp(lp) for lp in (log_p(x) for x in range(lo, hi + 1)) if lp <= observed)
    return min(1.0, total)


# --------------------------------------------------------------------------
# 均值
# --------------------------------------------------------------------------


class Welch(NamedTuple):
    t: float
    df: float
    p: float


def welch(m1: float, v1: float, n1: int, m2: float, v2: float, n2: int) -> Welch | None:
    """两组均值相等的 Welch t 检验。v 是样本方差（n − 1 作分母）。有一组样本不到 2 个时为 None（方差算不了）。两组方差都是 0：
    均值相同 p 为 1，不同 p 为 0。"""
    if n1 < 2 or n2 < 2:
        return None
    s1, s2 = v1 / n1, v2 / n2
    se2 = s1 + s2
    if se2 <= 0:
        return Welch(0.0, math.inf, 1.0) if m1 == m2 else Welch(math.copysign(math.inf, m1 - m2), math.inf, 0.0)
    t = (m1 - m2) / math.sqrt(se2)
    denom = (s1 * s1 / (n1 - 1) if s1 else 0.0) + (s2 * s2 / (n2 - 1) if s2 else 0.0)
    df = se2 * se2 / denom if denom > 0 else math.inf
    return Welch(t, df, t_two_sided(t, df))


def welch_interval(m1: float, v1: float, n1: int, m2: float, v2: float, n2: int,
                   level: float = 0.95) -> tuple[float, float] | None:
    """m1 − m2 的 Welch 区间（缺省 95%）。算不了检验时也没有区间。"""
    test = welch(m1, v1, n1, m2, v2, n2)
    if test is None:
        return None
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return m1 - m2, m1 - m2
    q = t_ppf(0.5 + level / 2.0, test.df)
    return m1 - m2 - q * se, m1 - m2 + q * se


# --------------------------------------------------------------------------
# 多重比较
# --------------------------------------------------------------------------


def holm(pvalues: Sequence[float | None]) -> list[float | None]:
    """Holm 逐步校正：按原始 p 从小到大，第 i 个（从 1 数）乘以 m − i + 1，再取到它为止的最大值，不超过 1。None（这一对算不了）
    不计入次数、原样留着。"""
    present = sorted((p, i) for i, p in enumerate(pvalues) if p is not None)
    m = len(present)
    out: list[float | None] = [None] * len(pvalues)
    running = 0.0
    for rank, (p, i) in enumerate(present):
        running = max(running, min(1.0, (m - rank) * p))
        out[i] = running
    return out


# --------------------------------------------------------------------------
# 趋势
# --------------------------------------------------------------------------


def _sign(x: Any) -> int:
    return (x > 0) - (x < 0)


class MannKendall(NamedTuple):
    s: int
    var: float               # S 的方差（并列修正后）
    z: float | None          # 正态近似的 z（精确分布时为 None）
    p: float | Fraction      # 双侧 p；精确分布时是 Fraction
    exact: bool


#: 精确分布最多到几期：再多 n! 太大，正态近似已经足够
EXACT_MAX_N = 10


def _inversion_counts(n: int) -> list[int]:
    """1..n 的排列里，逆序数为 k 的排列有几个（k = 0 … n(n−1)/2）。递推：第 m 个数插进 m 个位置，逆序数各加 0 … m−1。"""
    counts = [1]
    for m in range(2, n + 1):
        nxt = [0] * (len(counts) + m - 1)
        for k, c in enumerate(counts):
            for j in range(m):
                nxt[k + j] += c
        counts = nxt
    return counts


def _scaled(values: Sequence[Any]) -> tuple[list[int], int] | None:
    """一组精确的数（整数、Fraction）乘上公分母换成整数：(整数, 公分母)。有浮点的为 None。两两相减、比大小在整数上做，
    比 Fraction 快得多，结果一样。"""
    if not all(isinstance(v, (int, Fraction)) and not isinstance(v, bool) for v in values):
        return None
    scale = math.lcm(*(Fraction(v).denominator for v in values)) if values else 1
    return [int(Fraction(v) * scale) for v in values], scale


def mann_kendall(values: Sequence[Any]) -> MannKendall:
    """Mann–Kendall 趋势检验：S = Σ_{i<j} sgn(y_j − y_i)，双侧 p。n ≤ EXACT_MAX_N 且没有并列时用精确分布（没有并列时
    S = N − 2·逆序数，N = n(n−1)/2，逆序数的分布递推算出）；否则正态近似：Var(S) = [n(n−1)(2n+5) − Σt(t−1)(2t+5)] / 18
    （t 是每组并列的个数），z = (S − sgn S) / √Var（连续性校正）。"""
    n = len(values)
    scaled = _scaled(values)
    ys = scaled[0] if scaled is not None else list(values)
    s = sum(_sign(ys[j] - ys[i]) for i in range(n) for j in range(i + 1, n))
    ties = [t for t in Counter(ys).values() if t > 1]
    var = (n * (n - 1) * (2 * n + 5) - sum(t * (t - 1) * (2 * t + 5) for t in ties)) / 18.0
    if n < 2:
        return MannKendall(0, 0.0, None, Fraction(1), True)
    if not ties and n <= EXACT_MAX_N:
        counts = _inversion_counts(n)
        pairs = n * (n - 1) // 2
        hit = sum(c for k, c in enumerate(counts) if abs(pairs - 2 * k) >= abs(s))
        return MannKendall(s, var, None, Fraction(hit, math.factorial(n)), True)
    if var <= 0:
        return MannKendall(s, var, 0.0, 1.0, False)
    z = (s - _sign(s)) / math.sqrt(var)
    return MannKendall(s, var, z, normal_two_sided(z), False)


def theil_sen(xs: Sequence[Any], ys: Sequence[Any]) -> Any:
    """Theil–Sen 斜率：所有两点（x 不同）连线斜率的中位数。不到两个点为 None。xs、ys 都是精确的数时结果是 Fraction：斜率
    在公分母上写成整数之比，按 int / int（正确舍入、单调）的浮点排，只把浮点相等的那几个按精确值比，n = 261 的三万多个
    斜率也不逐个造 Fraction。"""
    sx, sy = _scaled(xs), _scaled(ys)
    if sx is None or sy is None:
        slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
                  if xs[j] != xs[i]]
        return median(slopes) if slopes else None
    (X, dx), (Y, dy) = sx, sy
    pairs = []
    for i in range(len(X)):
        for j in range(i + 1, len(X)):
            den = (X[j] - X[i]) * dy
            if den:
                num = (Y[j] - Y[i]) * dx
                pairs.append((num, den) if den > 0 else (-num, -den))
    if not pairs:
        return None
    keys = [num / den for num, den in pairs]
    order = sorted(range(len(pairs)), key=keys.__getitem__)

    def kth(k: int) -> Fraction:
        pivot = keys[order[k]]
        lo, hi = k, k + 1
        while lo > 0 and keys[order[lo - 1]] == pivot:
            lo -= 1
        while hi < len(order) and keys[order[hi]] == pivot:
            hi += 1
        block = sorted(Fraction(*pairs[t]) for t in order[lo:hi])
        return block[k - lo]

    m = len(pairs)
    return kth(m // 2) if m % 2 else (kth(m // 2 - 1) + kth(m // 2)) / 2


# --------------------------------------------------------------------------
# 位置与离散
# --------------------------------------------------------------------------


#: 超过这么多个值时中位数先按浮点排（Theil–Sen 的两两斜率有几万个，逐个按 Fraction 比太慢）
_FAST_MEDIAN = 512


def _kth(values: Sequence[Any], k: int) -> Any:
    """从小到大第 k 个（从 0 数），精确：Fraction 换成浮点是正确舍入的、单调的——浮点小的精确值一定小，排错的只可能是浮点
    相等的那几个，所以先按浮点排，再只把和第 k 个浮点相等的那一段按精确值排。"""
    keyed = sorted((float(v), i) for i, v in enumerate(values))
    pivot = keyed[k][0]
    lo, hi = k, k + 1
    while lo > 0 and keyed[lo - 1][0] == pivot:
        lo -= 1
    while hi < len(keyed) and keyed[hi][0] == pivot:
        hi += 1
    block = sorted(values[i] for _, i in keyed[lo:hi])
    return block[k - lo]


def median(values: Sequence[Any]) -> Any:
    """中位数（偶数个时取中间两个的平均）。Fraction 进 Fraction 出。没有值为 None。"""
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    if n > _FAST_MEDIAN:
        upper = _kth(values, mid)
        return upper if n % 2 else (_kth(values, mid - 1) + upper) / 2
    data = sorted(values)
    if n % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2


def mad(values: Sequence[Any], center: Any | None = None) -> Any:
    """中位绝对偏差：各值离中位数（或给定的中心）的绝对距离的中位数。"""
    if not values:
        return None
    c = median(values) if center is None else center
    return median([abs(v - c) for v in values])


def rank_desc(x: Any, others: Sequence[Any]) -> int:
    """x 在「others + x」里从大到小的名次（并列取最好的名次：比它大的有几个再加 1）。"""
    return 1 + sum(1 for o in others if o > x)


__all__ = [
    "EXACT_MAX_N", "MannKendall", "Welch", "Z95", "betainc", "fisher_exact", "holm", "mad", "mann_kendall", "median",
    "min_expected", "newcombe", "normal_cdf", "normal_ppf", "normal_two_sided", "pooled_z", "rank_desc", "t_cdf",
    "t_ppf", "t_two_sided", "theil_sen", "welch", "welch_interval", "wilson",
]
