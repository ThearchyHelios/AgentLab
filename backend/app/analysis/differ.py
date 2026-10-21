"""差异检验（细案 01 §5）：这两组（或两期）真的不一样，还是样本波动。

**参数**：query（一份快照）；按组比时 by（分组列，一列）；按期比同一组时 period、base、target（各一行，SQL 里先筛好那一组）。
比哪几对：pairs（[[A, B], …]，A、B 是分组列里的值）、reference（对照组：其余各组各和它比）；都不写时两组就比这两组，三组
以上每组和「其余各组合计」比（和含它自己的整体比不独立）。可选 label。

- rate：numerator（成功数，如转化单数）、denominator（试验数，如访客数），每组一行，都要是不小于 0 的整数、成功数不超过
  试验数。每组 Wilson 区间，两组之差 Newcombe 区间；最小期望频数不小于 min_expected 用合并比例的 z 检验，否则用 Fisher
  精确检验。
- mean：每组一行，mean、sd、n 三列（均值、标准差、样本数），或者 sum、sumsq、n 三列（合计、平方和、样本数：SQLite 没有标准差
  函数，SUM(x)、SUM(x*x)、COUNT(*) 都写得出）。Welch t 检验，自由度按 Welch–Satterthwaite。
- rows：value（逐行的值）+ 分组列（或期次列），快照不超过 1000 行、没截断；算子自己算均值、方差，另做敏感性：去掉离均值
  最远的那一个值后重算，判定变了加 outlier_sensitive。

比了几对时按 Holm 校正，表里同时给原始 p 和校正后的 p，附 multiple_comparisons。校正后 p < differ_alpha → differs（direction
按 A − B 的正负）；否则 no_evidence。用了 Fisher（比率）或有一组样本数小于 min_n（均值）→ small_sample。判定语不出现「p 值」
「显著性」：differs 说「差异站得住」，no_evidence 说「差异可能只是样本波动」，p 值、区间在表和证据面板里。

拿不到 p 值的一对（比率有一组样本量为 0；均值、逐行有一组不到 2 个样本，或均值、标准差为空）不算进比了几对、不参与校正，表里
照实写哪一组怎么了；一对都拿不到时算不了（untestable），不硬判「可能只是样本波动」；只有一部分拿不到的，判定语后接一句
「另有 n 对算不了」（untested）。

subjects 是判定说的那一对 [[A], [B]]（只比一对时就是那一对，比了几对时是校正后最站得住的那一对；几对都不站得住时为空），
direction 是 A 相对 B。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.analysis import phrases as ph
from app.analysis import stats
from app.analysis.common import Env, build, column_label, column_types, label_for, need, refuse, sign_word
from app.analysis.contract import CallError
from app.analysis.inputs import Snap, as_list, hashable, same_value
from app.analysis.numbers import canon, to_fraction

TESTS = {"z": "z 检验", "fisher": "Fisher 精确检验", "welch": "Welch t 检验"}


def used_settings(mode: str | None) -> list[str]:
    """结论里记的阈值：比率看最小期望频数，均值、逐行看最小样本数。"""
    return ["differ_alpha", "min_expected"] if mode == "rate" else ["differ_alpha", "min_n"]


def refusal_basis(mode: str | None, settings: Mapping[str, Any]) -> tuple[list[str], str]:
    """还没算就算不了时结论里记的阈值和判定标准（同 decompose.refusal_basis）。"""
    return used_settings(mode), ph.criterion_differ(settings, mode)  # type: ignore[arg-type]


@dataclass(eq=False)
class Group:
    key: Any
    name: str
    rows: list[int] = field(default_factory=list)
    x: int = 0                    # rate：成功数
    n: int = 0                    # rate：试验数；mean / rows：样本数
    mean: Fraction | None = None
    var: Fraction | None = None
    values: list[Fraction] = field(default_factory=list)


@dataclass
class Pair:
    a: Group
    b: Group
    test: str | None = None
    p: float | None = None
    p_adj: float | None = None
    diff: Fraction | None = None
    ci: tuple[float, float] | None = None
    a_value: Fraction | None = None
    b_value: Fraction | None = None


def _int(value: Fraction | None, snap: Snap, r: int, column: str) -> int:
    if value is None:
        raise CallError("empty_cell", f"{snap.ref} 第 {r} 行的「{column}」是空的：计数要有值（在 SQL 里把空值写成 0）",
                        reason=f"「{column}」那一格是空的（{snap.ref} 第 {r} 行）")
    if value.denominator != 1 or value < 0:
        raise CallError("not_count", f"{snap.ref} 第 {r} 行的「{column}」不是不小于 0 的整数：比率比较的分子、分母要是计数")
    return int(value)


#: 「其余各组合计」的键（不是分组列里的值）
REST_KEY = ("__rest__",)


def _pool(name: str, groups: Sequence[Group], mode: str) -> Group:
    """「其余各组合计」：比率把成功数、试验数加起来；均值按各组的均值、方差、样本数合并；逐行的值并在一起。"""
    out = Group(REST_KEY, name, rows=[r for g in groups for r in g.rows])
    if mode == "rate":
        out.x, out.n = sum(g.x for g in groups), sum(g.n for g in groups)
        return out
    if mode == "rows":
        out.values = [v for g in groups for v in g.values]
        return _moments(out)
    out.n = sum(g.n for g in groups)
    if out.n == 0:
        return out
    out.mean = sum((g.mean * g.n for g in groups if g.mean is not None), Fraction(0)) / out.n
    if out.n > 1:
        within = sum(((g.n - 1) * g.var for g in groups if g.var is not None and g.n > 1), Fraction(0))
        between = sum((g.n * (g.mean - out.mean) ** 2 for g in groups if g.mean is not None), Fraction(0))
        out.var = (within + between) / (out.n - 1)
    return out


def _moments(group: Group) -> Group:
    group.n = len(group.values)
    if group.n:
        group.mean = sum(group.values, Fraction(0)) / group.n
    if group.n > 1:
        group.var = sum(((v - group.mean) ** 2 for v in group.values), Fraction(0)) / (group.n - 1)  # type: ignore[operator]
    return group


def _test(pair: Pair, mode: str, min_expected: Fraction) -> None:
    a, b = pair.a, pair.b
    if mode == "rate":
        if a.n == 0 or b.n == 0:
            return
        pair.a_value, pair.b_value = Fraction(a.x, a.n), Fraction(b.x, b.n)
        pair.diff = pair.a_value - pair.b_value
        pair.ci = stats.newcombe(a.x, a.n, b.x, b.n)
        if stats.min_expected(a.x, a.n, b.x, b.n) >= min_expected:
            pair.test = "z"
            pair.p = stats.pooled_z(a.x, a.n, b.x, b.n)[1]  # type: ignore[index]
        else:
            pair.test = "fisher"
            pair.p = stats.fisher_exact(a.x, a.n - a.x, b.x, b.n - b.x)
        return
    if a.mean is None or b.mean is None:
        return
    pair.a_value, pair.b_value = a.mean, b.mean
    pair.diff = a.mean - b.mean
    if a.var is None or b.var is None:
        return
    test = stats.welch(float(a.mean), float(a.var), a.n, float(b.mean), float(b.var), b.n)
    if test is None:
        return
    pair.test, pair.p = "welch", test.p
    pair.ci = stats.welch_interval(float(a.mean), float(a.var), a.n, float(b.mean), float(b.var), b.n)


def _gap(group: Group, mode: str) -> str | None:
    """一组为什么算不了检验；算得了为 None。"""
    if mode == "rate":
        return ph.say(group.name, "的样本量为 0") if group.n == 0 else None
    if group.n < 2:
        return ph.say(group.name, "没有样本" if group.n == 0 else "只有 1 个样本")
    if group.mean is None or group.var is None:
        return ph.say(group.name, "的均值或标准差为空")
    return None


def untested_text(pair: Pair, mode: str) -> str:
    """这一对拿不到 p 值的原因：哪一组、怎么了（两组都有就都写）。"""
    return "、".join(dict.fromkeys(why for g in (pair.a, pair.b) if (why := _gap(g, mode)))) or "样本不够"


def _verdict(pairs: Sequence[Pair], alpha: Fraction) -> list[Pair]:
    """Holm 校正后站得住的那几对（按校正后的 p 从小到大，一样的按先后）。"""
    adjusted = stats.holm([p.p for p in pairs])
    for pair, adj in zip(pairs, adjusted):
        pair.p_adj = adj
    hits = [p for p in pairs if p.p_adj is not None and to_fraction(p.p_adj) < alpha]  # type: ignore[operator]
    return sorted(hits, key=lambda p: p.p_adj)  # type: ignore[arg-type, return-value]


def differ(raw: Mapping[str, Any], env: Env) -> Any:
    mode = env.mode
    used = used_settings(mode)
    snap = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号")
    params: dict[str, Any] = {"query": snap.ref}
    by_period = raw.get("period") is not None
    if by_period:
        group_col = snap.column(raw.get("period"), "期次列")
        params.update(period=group_col, base=need(raw, "base", "基期（期次列里的值）"),
                      target=need(raw, "target", "本期（期次列里的值）"))
    else:
        wanted = as_list(raw.get("by"))
        if len(wanted) != 1:
            raise CallError("bad_by", "差异检验按一列分组：分组列写一列；比同一组的两期就写期次列、基期、本期")
        group_col = snap.column(wanted[0], "分组列")
        params["by"] = [group_col]
    if mode == "rate":
        cols = {"numerator": snap.column(need(raw, "numerator", "成功数列"), "成功数列"),
                "denominator": snap.column(need(raw, "denominator", "试验数列"), "试验数列")}
    elif mode == "mean":
        if raw.get("mean") is not None:
            cols = {"mean": snap.column(raw.get("mean"), "均值列"), "sd": snap.column(need(raw, "sd", "标准差列"), "标准差列"),
                    "n": snap.column(need(raw, "n", "样本数列"), "样本数列")}
        else:
            cols = {"sum": snap.column(need(raw, "sum", "合计列（或均值列）"), "合计列"),
                    "sumsq": snap.column(need(raw, "sumsq", "平方和列"), "平方和列"),
                    "n": snap.column(need(raw, "n", "样本数列"), "样本数列")}
    else:
        cols = {"value": snap.column(need(raw, "value", "值列"), "值列")}
    params.update(cols)
    measure_col = cols.get("numerator") or cols.get("mean") or cols.get("sum") or cols.get("value")
    if mode == "rate":
        label = ph.pick_label(raw.get("label"), ph.say(column_label(env, snap, cols["numerator"]), "占",
                                                       column_label(env, snap, cols["denominator"]), "的比率"))
    else:
        label = label_for(env, raw.get("label"), snap, measure_col)
    if raw.get("label") is not None:
        params["label"] = ph.clean_label(raw.get("label")) or None
    dim_label = column_label(env, snap, group_col)
    criterion = ph.criterion_differ(env.settings, mode)  # type: ignore[arg-type]
    if snap.truncated:
        env.inputs.use(snap, "data", [group_col, *cols.values()])
        return refuse(env, params, used, "truncated", f"{snap.ref} 只取回了前 {len(snap.rows)} 行，各组不完整",
                      criterion=criterion)
    groups = _groups(snap, group_col, cols, mode, by_period, params)  # type: ignore[arg-type]
    pairs = _pairs(raw, params, groups, mode, dim_label, by_period, group_col)  # type: ignore[arg-type]
    env.inputs.use(snap, "data", [group_col, *cols.values()],
                   sorted({r for p in pairs for g in (p.a, p.b) for r in g.rows}))
    return _decide(env, params, used, pairs, mode, label, criterion, kind=snap.kind(group_col))  # type: ignore[arg-type]


def _groups(snap: Snap, group_col: str, cols: Mapping[str, str], mode: str, by_period: bool,
            params: Mapping[str, Any]) -> dict[Any, Group]:
    gi = snap.index(group_col)
    groups: dict[Any, Group] = {}
    wanted = [params["target"], params["base"]] if by_period else None
    for r, row in enumerate(snap.rows):
        value = row[gi] if gi < len(row) else None
        if wanted is not None:
            if not any(same_value(w, value) for w in wanted):
                continue
        key = hashable(value)
        group = groups.setdefault(key, Group(key, ph.key_text([value])))
        if mode != "rows" and group.rows:
            raise CallError("duplicate_rows", f"{snap.ref} 里「{ph.key_text([value])}」有不止一行：在 SQL 里按 {group_col} "
                                              "汇总，每组一行")
        group.rows.append(r)
        if mode == "rate":
            group.x = _int(snap.number(r, cols["numerator"], "成功数列"), snap, r, cols["numerator"])
            group.n = _int(snap.number(r, cols["denominator"], "试验数列"), snap, r, cols["denominator"])
            if group.x > group.n:
                raise CallError("not_count", f"{snap.ref} 第 {r} 行的成功数大于试验数：分子、分母要是同一批里的「成功几个、"
                                             "一共几个」")
        elif mode == "mean":
            n = _int(snap.number(r, cols["n"], "样本数列"), snap, r, cols["n"])
            group.n = n
            if "mean" in cols:
                group.mean = snap.number(r, cols["mean"], "均值列")
                sd = snap.number(r, cols["sd"], "标准差列")
                group.var = sd * sd if sd is not None and n > 1 else None
            else:
                total = snap.number(r, cols["sum"], "合计列")
                sq = snap.number(r, cols["sumsq"], "平方和列")
                if total is not None and n > 0:
                    group.mean = total / n
                    if sq is not None and n > 1:
                        group.var = max(Fraction(0), (sq - total * total / n) / (n - 1))
        else:
            v = snap.number(r, cols["value"], "值列")
            if v is not None:
                group.values.append(v)
    if mode == "rows":
        for g in groups.values():
            _moments(g)
    if by_period:
        for w, what in ((params["target"], "本期"), (params["base"], "基期")):
            if not any(same_value(w, g.key) for g in groups.values()):
                raise CallError("period_not_found", f"期次列「{group_col}」里没有「{w}」：{what}要写 {snap.ref} 里有的期次值",
                                reason=f"期次列「{group_col}」里没有{what}「{ph.key_text([w])}」")
    return groups


def _find(groups: Mapping[Any, Group], value: Any, column: str) -> Group:
    found = next((g for g in groups.values() if same_value(value, g.key)), None)
    if found is None:
        shown = "、".join(g.name for g in list(groups.values())[:8])
        raise CallError("unknown_group", f"「{value}」不是「{column}」里的值：要比的组写 {shown} 里的",
                        reason=f"分组列「{column}」里没有「{ph.key_text([value])}」这一组")
    return found


def _pairs(raw: Mapping[str, Any], params: dict[str, Any], groups: dict[Any, Group], mode: str, dim_label: str,
           by_period: bool, group_col: str) -> list[Pair]:
    if by_period:
        target = _find(groups, params["target"], group_col)
        base = _find(groups, params["base"], group_col)
        return [Pair(target, base)]
    if raw.get("pairs") is not None:
        spec = raw.get("pairs")
        if not isinstance(spec, list) or not spec or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in spec):
            raise CallError("bad_pairs", "要比的几对写成 [[A, B], …]，A、B 是分组列里的值")
        out = []
        for a, b in spec:
            ga, gb = _find(groups, a, group_col), _find(groups, b, group_col)
            if ga is gb:
                raise CallError("bad_pairs", f"「{ga.name}」不能和自己比")
            out.append(Pair(ga, gb))
        params["pairs"] = [[p.a.key, p.b.key] for p in out]
        return out
    if len(groups) < 2:
        raise CallError("too_few_groups", f"「{group_col}」里只有 {len(groups)} 组，至少要两组才比得了",
                        reason=f"分组列「{group_col}」里只有 {len(groups)} 组，至少要两组才比得了")
    if raw.get("reference") is not None:
        ref = _find(groups, raw.get("reference"), group_col)
        params["reference"] = ref.key
        return [Pair(g, ref) for g in groups.values() if g is not ref]
    items = list(groups.values())
    if len(items) == 2:
        return [Pair(items[0], items[1])]
    rest_name = ph.say("其余各", dim_label)
    return [Pair(g, _pool(rest_name, [o for o in items if o is not g], mode)) for g in items]


def _decide(env: Env, params: dict[str, Any], used: list[str], pairs: list[Pair], mode: str, label: str,
            criterion: str, *, kind: str | None) -> Any:
    alpha = env.threshold("differ_alpha")
    min_expected = env.threshold("min_expected") if mode == "rate" else Fraction(0)
    for pair in pairs:
        _test(pair, mode, min_expected)
    tested = [p for p in pairs if p.p is not None]
    untested = [p for p in pairs if p.p is None]
    if not tested:
        shown = list(dict.fromkeys(untested_text(p, mode) for p in pairs))
        return refuse(env, params, used, "untestable",
                      "，".join(shown[:3]) + ("等" if len(shown) > 3 else "") + "，算不了差异", criterion=criterion)
    hits = _verdict(pairs, alpha)
    caveats: list[dict[str, str]] = []
    if mode == "rows":
        flipped = _sensitivity(pairs, mode, alpha, min_expected, bool(hits))
        if flipped is not None:
            caveats.append(ph.caveat("outlier_sensitive", ph.outlier_text(*flipped)))
        hits = _verdict(pairs, alpha)
    if mode == "rate" and any(p.test == "fisher" for p in pairs):
        caveats.append(ph.caveat("small_sample", ph.small_sample_rate_text(int(env.settings["min_expected"]))))
    if mode != "rate":
        min_n = int(env.settings["min_n"])
        small = sorted((g for p in pairs for g in (p.a, p.b) if g.n < min_n), key=lambda g: g.n)
        if small:
            caveats.append(ph.caveat("small_sample", ph.small_sample_mean_text(small[0].name, small[0].n, min_n)))
    if len(tested) > 1:
        caveats.append(ph.caveat("multiple_comparisons", ph.multiple_text(len(tested))))
    if untested:
        caveats.append(ph.caveat("untested", ph.untested_pairs_text(len(untested),
                                                                     [untested_text(p, mode) for p in untested])))
    fmt = (lambda v: ph.pct(v, signed=False)) if mode == "rate" else (lambda v: ph.amount(v))
    if hits:
        code = "differs"
        head = hits[0]
        main = ph.differ_phrase([ph.differ_pair(p.a.name, p.b.name, label, fmt(p.a_value), fmt(p.b_value),
                                                p.diff > 0) for p in hits],  # type: ignore[operator]
                                len(tested))
        subjects = [[head.a.name], [head.b.name]]
        direction = sign_word(head.diff)  # type: ignore[arg-type]
    else:
        code = "no_evidence"
        head = tested[0] if len(tested) == 1 else None
        sizes = (ph.amount(head.a.n), ph.amount(head.b.n)) if head is not None else None
        main = ph.no_evidence_phrase(head.a.name if head else None, head.b.name if head else None, label, sizes,
                                     len(tested))
        subjects = [[head.a.name], [head.b.name]] if head is not None else []
        direction = None
    value_name = "比率" if mode == "rate" else "均值"
    rows = []
    for p in pairs:
        flags = []
        if p.p is None:
            flags.append(f"{untested_text(p, mode)}，算不了")
        if p.test == "fisher":
            flags.append("样本少，用了精确检验")
        rows.append([p.a.name, p.b.name, canon(p.a_value), canon(p.b_value), canon(p.diff),
                     canon(p.ci[0]) if p.ci else None, canon(p.ci[1]) if p.ci else None, str(p.a.n), str(p.b.n),
                     TESTS.get(p.test or "") or None, canon(p.p), canon(p.p_adj), "、".join(flags) or None])
    columns = ["a", "b", "a_value", "b_value", "diff", "ci_lo", "ci_hi", "a_n", "b_n", "test", "p", "p_adj", "flag"]
    # 组、对照组照分组列（期次列）在快照里的类型；和「其余各组合计」比时对照组那一列有一格是叫法，写 text
    rest = any(p.b.key == REST_KEY for p in pairs)
    table = {"columns": columns,
             "labels": {"a": "组", "b": "对照组", "a_value": f"组的{value_name}", "b_value": f"对照组的{value_name}",
                        "diff": "差", "ci_lo": "差的区间下限", "ci_hi": "差的区间上限", "a_n": "组的样本量",
                        "b_n": "对照组的样本量", "test": "检验", "p": "p 值", "p_adj": "校正后的 p 值", "flag": "说明"},
             "rows": rows,
             "column_types": column_types(columns, dims={"a": kind, "b": "text" if rest else kind},
                                          texts=("test", "flag"))}
    shown = head if head is not None else tested[0]
    values = {"a": canon(shown.a_value), "b": canon(shown.b_value), "diff": canon(shown.diff),
              "ci_lo": canon(shown.ci[0]) if shown.ci else None, "ci_hi": canon(shown.ci[1]) if shown.ci else None,
              "p": canon(shown.p), "p_adj": canon(shown.p_adj), "n_a": str(shown.a.n), "n_b": str(shown.b.n),
              "test": shown.test, "pairs": str(len(pairs)), "differs": str(len(hits))}
    verdict = {"code": code, "subjects": subjects, "direction": direction}
    phrase = ph.with_caveats(main, caveats, skip=("multiple_comparisons",) if len(tested) > 1 else ())
    return build(env, params, used, table=table, values=values, verdict=verdict, criterion=criterion, phrase=phrase,
                 caveats=caveats)


def _sensitivity(pairs: list[Pair], mode: str, alpha: Fraction, min_expected: Fraction,
                 differs: bool) -> tuple[bool, bool] | None:
    """rows 模式的敏感性：去掉比较的各组里离均值最远的那一个值重算，判定变了交回 (去掉的是不是偏大的那一笔, 现在站不站得住)。
    只在真实的各组里找（「其余各组合计」是它们的值并在一起，不另算一份），去掉以后由各组重建每一对的「其余」合计。"""
    groups = list(dict.fromkeys(g for p in pairs for g in (p.a, p.b) if g.key != REST_KEY))
    everything = [(g, i, v) for g in groups for i, v in enumerate(g.values)]
    if len(everything) < 3:
        return None
    mean = sum((v for _, _, v in everything), Fraction(0)) / len(everything)
    group, index, value = max(everything, key=lambda t: abs(t[2] - mean))
    cut = {g: g for g in groups}
    cut[group] = _moments(Group(group.key, group.name, rows=group.rows,
                                values=[v for k, v in enumerate(group.values) if k != index]))
    trimmed = [Pair(cut[p.a], _pool(p.b.name, [cut[g] for g in groups if g is not p.a], mode)
                    if p.b.key == REST_KEY else cut[p.b]) for p in pairs]
    for p in trimmed:
        _test(p, mode, min_expected)
    now = bool(_verdict(trimmed, alpha))
    _verdict(pairs, alpha)
    return (value > mean, now) if now != differs else None


__all__ = ["REST_KEY", "TESTS", "differ", "refusal_basis", "untested_text", "used_settings"]
