"""分析用的统计函数（app/analysis/stats.py）：用公认的表值钉住，外加边界——并列、n 很小、p 接近 0 / 1、t 自由度很大趋近正态。
Wilson 从 eval/stats 搬过来，评测引用的是同一个函数、结果和以前一样。"""
from __future__ import annotations

import itertools
import math
import random
from fractions import Fraction

import pytest

from app.analysis import stats
from app.eval import stats as eval_stats


def test_normal_quantiles_and_distribution_match_the_tables():
    assert stats.normal_ppf(0.975) == pytest.approx(1.959964, abs=1e-6)
    assert stats.normal_ppf(0.975) == pytest.approx(stats.Z95, rel=1e-15)
    assert stats.normal_ppf(0.5) == 0.0
    assert stats.normal_ppf(0.95) == pytest.approx(1.644854, abs=1e-6)
    assert stats.normal_ppf(0.995) == pytest.approx(2.575829, abs=1e-6)
    assert stats.normal_ppf(1e-10) == pytest.approx(-6.361341, abs=1e-6)       # 很靠近 0 的尾巴
    assert stats.normal_ppf(0) == -math.inf and stats.normal_ppf(1) == math.inf
    for p in (1e-12, 0.001, 0.02425, 0.3, 0.7, 0.97575, 0.999, 1 - 1e-9):
        assert stats.normal_cdf(stats.normal_ppf(p)) == pytest.approx(p, rel=1e-9)
    assert stats.normal_cdf(1.959963984540054) == pytest.approx(0.975, abs=1e-15)
    assert stats.normal_two_sided(1.959963984540054) == pytest.approx(0.05, abs=1e-15)


def test_t_quantiles_match_the_tables():
    # 双侧 0.05 的临界值（t 分布表）
    table = {1: 12.706205, 2: 4.302653, 5: 2.570582, 10: 2.228139, 30: 2.042272, 100: 1.983972}
    for df, value in table.items():
        assert stats.t_ppf(0.975, df) == pytest.approx(value, abs=1e-6), df
    assert stats.t_two_sided(2.228139, 10) == pytest.approx(0.05, abs=1e-6)
    # 自由度 2 有闭式解：F(t) = 1/2 + t / (2√(2 + t²))
    for t in (0.3, 1.5, 4.0, 25.0):
        assert stats.t_cdf(t, 2) == pytest.approx(0.5 + t / (2 * math.sqrt(2 + t * t)), rel=1e-12)
    assert stats.t_cdf(0.0, 7) == 0.5 and stats.t_two_sided(0.0, 7) == 1.0
    assert stats.t_cdf(-2.0, 10) == pytest.approx(1 - stats.t_cdf(2.0, 10), rel=1e-12)


def test_t_with_a_huge_df_approaches_the_normal():
    assert stats.t_ppf(0.975, 1e6) == pytest.approx(stats.Z95, abs=1e-5)
    assert stats.t_ppf(0.975, 1e9) == pytest.approx(stats.Z95, abs=1e-9)
    assert stats.t_two_sided(1.96, 5e6) == pytest.approx(stats.normal_two_sided(1.96), abs=1e-6)
    # 非整数自由度（Welch）也行
    assert stats.t_ppf(0.975, 41.784) == pytest.approx(2.018, abs=1e-3)


def test_regularized_incomplete_beta():
    assert stats.betainc(2, 3, 0.4) == pytest.approx(0.5248, abs=1e-12)            # 多项式，手算
    assert stats.betainc(0.5, 0.5, 0.5) == pytest.approx(0.5, abs=1e-12)
    assert stats.betainc(1, 1, 0.3) == pytest.approx(0.3, abs=1e-12)
    assert stats.betainc(3, 2, 0.0) == 0.0 and stats.betainc(3, 2, 1.0) == 1.0


def test_wilson_moved_here_is_the_same_function_the_eval_uses():
    assert eval_stats.wilson is stats.wilson
    assert eval_stats.Z95 == stats.Z95
    assert stats.wilson(8, 10) == (0.4902, 0.9433)
    assert stats.wilson(0, 0) is None
    assert stats.wilson(0, 5)[0] == 0.0 and stats.wilson(5, 5)[1] == 1.0
    low, high = stats.wilson(8, 10, places=None)
    assert round(low, 4) == 0.4902 and round(high, 4) == 0.9433 and low != 0.4902


def test_newcombe_matches_the_published_example():
    # Newcombe（1998）的例子：56/70 对 48/80，混合 score 法（方法 10）0.0524 到 0.3339
    low, high = stats.newcombe(56, 70, 48, 80)
    assert low == pytest.approx(0.0524, abs=1e-4) and high == pytest.approx(0.3339, abs=1e-4)
    assert stats.newcombe(1, 0, 2, 5) is None


def test_pooled_z_and_min_expected():
    z, p = stats.pooled_z(56, 70, 48, 80)
    assert z == pytest.approx(2.6502, abs=1e-4) and p == pytest.approx(0.00805, abs=1e-5)
    assert stats.pooled_z(0, 10, 0, 20) == (0.0, 1.0)               # 两边全不是：没有差异可言
    assert stats.pooled_z(3, 0, 1, 5) is None
    assert stats.min_expected(2, 40, 5, 230) == pytest.approx(7 * 40 / 270)


def test_fisher_exact_two_sided():
    # Fisher 的品茶实验
    assert stats.fisher_exact(3, 1, 1, 3) == pytest.approx(0.4857142857, abs=1e-9)
    assert stats.fisher_exact(1, 9, 11, 3) == pytest.approx(0.002759456, abs=1e-8)
    assert stats.fisher_exact(0, 10, 10, 0) == pytest.approx(2 / math.comb(20, 10), rel=1e-9)   # p 接近 0
    assert stats.fisher_exact(5, 5, 5, 5) == pytest.approx(1.0)                                # p 接近 1
    assert stats.fisher_exact(0, 0, 0, 0) == 1.0


def test_welch_t_test():
    test = stats.welch(10, 4, 20, 8, 9, 25)
    # t = 2 / √(4/20 + 9/25) = 2.6726；自由度按 Welch–Satterthwaite
    assert test.t == pytest.approx(2.672612, abs=1e-6)
    assert test.df == pytest.approx(0.56 ** 2 / (0.2 ** 2 / 19 + 0.36 ** 2 / 24), rel=1e-12)
    assert test.p == pytest.approx(stats.t_two_sided(test.t, test.df))
    assert stats.welch(1, 1, 1, 2, 1, 5) is None                    # 一组只有一个样本：方差算不了
    assert stats.welch(5, 0, 3, 5, 0, 4).p == 1.0                    # 没有方差、均值一样
    assert stats.welch(5, 0, 3, 6, 0, 4).p == 0.0
    low, high = stats.welch_interval(10, 4, 20, 8, 9, 25)
    assert low < 2 < high and (low + high) / 2 == pytest.approx(2)


def test_holm_step_down():
    assert stats.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert stats.holm([0.01, None, 0.5]) == [0.02, None, 0.5]
    assert stats.holm([0.6, 0.9]) == [1.0, 1.0]                     # 不超过 1
    assert stats.holm([]) == []


def _exact_two_sided(values: list[int]) -> Fraction:
    """暴力枚举全部排列，算 Mann–Kendall 的精确双侧 p（对照用）。"""
    def s_of(seq: tuple[int, ...]) -> int:
        return sum((b > a) - (b < a) for i, a in enumerate(seq) for b in seq[i + 1:])

    observed = abs(s_of(tuple(values)))
    perms = list(itertools.permutations(values))
    return Fraction(sum(1 for p in perms if abs(s_of(p)) >= observed), len(perms))


def test_mann_kendall_small_samples_use_the_exact_distribution():
    mk = stats.mann_kendall([1, 2, 3, 4])
    assert mk.exact and mk.s == 6 and mk.p == Fraction(1, 12)              # 4! 里只有两头两个排列
    assert stats.mann_kendall([1, 2, 3, 4, 5]).p == Fraction(1, 60)
    for values in ([3, 1, 4, 2, 6, 5], [1, 3, 2, 5, 4, 7, 6], [2, 1, 4, 3, 6, 5, 8]):
        assert stats.mann_kendall(values).p == _exact_two_sided(values)
    # n = 10 的单调序列：2 / 10!
    assert stats.mann_kendall(list(range(10))).p == Fraction(2, math.factorial(10))


def test_mann_kendall_with_ties_or_more_points_uses_the_corrected_normal_approximation():
    values = [1, 2, 2, 3, 4, 5, 5, 6, 7, 8, 9, 10]
    mk = stats.mann_kendall(values)
    n = len(values)
    var = (n * (n - 1) * (2 * n + 5) - 2 * (2 * 1 * 9)) / 18          # 两组各两个并列
    assert not mk.exact and mk.var == pytest.approx(var)
    assert mk.z == pytest.approx((mk.s - 1) / math.sqrt(var))         # 连续性校正
    assert mk.p == pytest.approx(stats.normal_two_sided(mk.z))
    tied = stats.mann_kendall([4, 4, 4, 4, 4, 4])                      # 全是并列：方差为 0
    assert tied.s == 0 and tied.p == 1.0
    small_tied = stats.mann_kendall([1, 1, 2, 3])                      # n ≤ 10 但有并列：也走正态近似
    assert not small_tied.exact


def test_theil_sen_median_mad_and_rank():
    xs = [Fraction(i) for i in range(4)]
    assert stats.theil_sen(xs, [Fraction(1), Fraction(3), Fraction(2), Fraction(6)]) == Fraction(19, 12)
    assert stats.theil_sen([Fraction(0)], [Fraction(1)]) is None
    assert stats.median([Fraction(3), Fraction(1), Fraction(2)]) == 2
    assert stats.median([Fraction(1), Fraction(2), Fraction(3), Fraction(10)]) == Fraction(5, 2)
    assert stats.median([]) is None
    assert stats.mad([Fraction(v) for v in (1, 1, 2, 2, 4, 6, 9)]) == 1
    assert stats.rank_desc(5, [1, 7, 5, 9]) == 3 and stats.rank_desc(10, [1, 2]) == 1


def test_theil_sen_on_many_points_matches_the_plain_definition():
    rng = random.Random(9)
    for n in (2, 3, 11, 60):
        xs = [Fraction(i) for i in range(n)]
        ys = [Fraction(rng.randint(-500, 500), rng.choice([1, 2, 3, 7, 100])) for _ in range(n)]
        slopes = sorted((ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(n) for j in range(i + 1, n))
        m = len(slopes)
        expected = slopes[m // 2] if m % 2 else (slopes[m // 2 - 1] + slopes[m // 2]) / 2
        assert stats.theil_sen(xs, ys) == expected
    assert stats.theil_sen([0.0, 1.0, 2.0], [1.0, 3.0, 2.0]) == 0.5           # 浮点照样算
    # 浮点分不开的斜率：精确地比
    tiny = Fraction(1, 10 ** 25)
    ys = [Fraction(0), Fraction(1) + tiny, Fraction(2) - tiny, Fraction(3)]
    xs = [Fraction(i) for i in range(4)]
    slopes = sorted((ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(4) for j in range(i + 1, 4))
    assert stats.theil_sen(xs, ys) == (slopes[2] + slopes[3]) / 2


def test_the_fast_median_of_many_values_is_still_exact():
    rng = random.Random(5)
    for size in (513, 514, 2001):
        # 浮点分不开的值：1/3 加上远小于浮点精度的扰动
        values = [Fraction(1, 3) + Fraction(rng.randint(-3, 3), 10 ** 30) for _ in range(size)]
        values += [Fraction(rng.randint(-1000, 1000), rng.randint(1, 260)) for _ in range(size)]
        ordered = sorted(values)
        n = len(ordered)
        expected = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        assert stats.median(values) == expected
