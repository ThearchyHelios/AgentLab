"""按核对结果生成表说明和列说明（P2-SPEC 5.4），再写进要冻结的表结构（schema_cache）。

说明进模型可见文本：introspect.describe_table 把表和列的 comment 拼进 db_schema 的输出，摘要里也有。
所以这里守三条规矩：
- **说明跟着核对结果走，规则写死**：K / T 没通过，这张表的说明里不出现「一致」；一条核对有几种无法核对的
  原因，用最弱的「原因混合」说法；找不到对应模板就记 problems（不能保存），绝不退回到别的模板——
  宁可导不进去，也不让模型读到「已核对一致」而事实上没核对。核对本身是不可接受的结构类失败（试运行已经
  拒收）时，那一段不写、也不记 problems，免得用户被引去改配方（见 _blocking）。
- **不带数字和坐标**：模板里的列名、表名、单位一律写成 {列:名} {表:名} {单位:名} 引用，每张表、每列的模板都过
  契约的 prose_problems(known=…)（遮盖引用后不许抽出数字、不许有坐标，引用必须指向真实的名字）。
  原表头、坐标、区域外文字全文只进清单和界面，不进说明。
- **模板是常量，拼接只用 .format()**：模板放在 NOTE_TEXT / COLUMN_TEXT 里，{a} {b} 这类槽位由 _tok 填入引用；
  这样模板本身经得起文案检查，填进去的名字也不会被当成格式串再解析一次。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.data.names import canon
from app.data.recipe_checks import date_columns, plan_checks, split_coord
from app.data.recipe_parsers import UNITS
from app.data.recipe_types import (
    Acceptance,
    CheckResult,
    ColumnOut,
    CrosstabBlock,
    DerivedSegment,
    DimensionSegment,
    Extraction,
    ListBlock,
    MeasuresSegment,
    NotComparable,
    Placeholder,
    Recipe,
    SchemaNotes,
    SheetRecipe,
    SumEq,
    TableNote,
    derive_tables,
    prose_problems,
    render_prose,
)

#: 表说明的片段模板。槽位里填的是 {列:名} {表:名} 引用（_tok 生成），片段之间用「。」拼接
NOTE_TEXT = {
    "grain_1": "粒度：每个{a}",
    "grain_2": "粒度：每个{a}的每个{b}",
    "grain_n": "粒度：{a} 与 {b} 的每个组合",
    "sum_eq_passed": "{total} 等于 {parts} 之和（导入时已{per}核对）：求合计只用 {total}，不要把这几列相加",
    "sum_eq_mismatch_accepted": "{total} 与 {parts} 之和的关系在本期个别{unit}不成立，已由用户确认接受，详见导入清单："
                                "不要据此推算，也不要把这几列相加",
    # 试运行回执的预览按「全部未接受」生成（P2-SPEC 7.4），这时还不能写「已由用户确认接受」
    "sum_eq_mismatch_pending": "{total} 与 {parts} 之和的关系在本期个别{unit}不成立，尚未确认接受："
                               "不要据此推算，也不要把这几列相加",
    "sum_eq_nulls": "{total} 与 {parts} 之和的关系在本期部分{unit}因空值未能核对：不要据此推算，也不要把这几列相加",
    # 本期一行数据都没有（AU-1）：不能写「已核对」，也不是「因空值」
    "sum_eq_no_rows": "{total} 与 {parts} 之和的关系在本期没有数据行，未能核对：不要据此推算，也不要把这几列相加",
    "not_comparable_unequal": "与 {other} 口径不同：各{dims}之和不等于 {value}，不能互相推算或相加",
    # 基表除了分组列没有别的粒度列时，「各……之和」无从说起，改说按分组列汇总
    "not_comparable_unequal_flat": "与 {other} 口径不同：{a_value} 按 {by} 汇总不等于 {value}，不能互相推算或相加",
    "not_comparable_equal": "与 {other} 口径不同，不能互相推算或相加",
    "hour_range": "{dim} 按规范写法存储：{start}-{end}，不带单位",
    # 配方没派生起止小时列时，没有列可以引用
    "hour_range_plain": "{dim} 按规范写法存储：起始小时与结束小时用连字符相连，不带单位",
    "const": "{col} 取自原表的分段标题",
    "total_kept": "原表的合计在 {table}，不要与本表相加",
    "total_not_kept": "原表的合计行未导入本表",
    "total_k_passed": "原表写明的合计，已按明细重算核对一致",
    "total_k_passed_g_unverifiable": "原表写明的合计，合计值已按明细核对一致，公式引用的格子未能核对",
    "total_k_uncached": "原表写明的合计。本期原表未保存计算结果，合计值为空，未能核对",
    "total_k_blank": "原表写明的合计。本期原表合计格为空，未能核对",
    "total_k_null_detail": "原表写明的合计，按原表保存的值存储。本期明细含无数据占位符，未能核对",
    "total_k_tiling": "原表写明的合计，按原表保存的值存储。本期明细未能覆盖合计的全部范围，未能核对",
    "total_k_hidden": "原表写明的合计，按原表保存的值存储。本期存在隐藏行或使用了分类汇总函数，合计口径无法确定，未能核对",
    "total_k_mixed": "原表写明的合计，按原表保存的值存储。本期合计值未能核对",
    "total_overlap": "各{dim}覆盖的时段可能互相重叠：不要彼此相加，也不要与 {base} 相加",
    "list_total_passed": "原表合计行的值，已按各列明细求和核对一致：不要与 {base} 相加",
    # 合计行里空着的数字格不生成合计格（P2-SPEC 4.4），T 只核对了其余的列：不能说「各列」都一致
    "list_total_passed_partial": "原表合计行的值。本期原表 {cols} 的合计格为空，其余各列已按明细求和核对一致："
                                 "不要与 {base} 相加",
    "list_total_uncached": "原表合计行的值。本期原表未保存计算结果，合计值为空，未能核对：不要与 {base} 相加",
    # 合计格写的是占位符（转成空值）时走到这里，与交叉表的 total_k_blank 对应
    "list_total_blank": "原表合计行的值。本期原表合计格为空，未能核对：不要与 {base} 相加",
    "list_total_tiling": "原表合计行的值，按原表保存的值存储。本期合计行之上没有明细，未能核对：不要与 {base} 相加",
    "list_total_null_detail": "原表合计行的值，按原表保存的值存储。本期明细含空值，未能核对：不要与 {base} 相加",
    "list_total_hidden": "原表合计行的值，按原表保存的值存储。本期存在隐藏行或使用了分类汇总函数，合计口径无法确定，"
                         "未能核对：不要与 {base} 相加",
    "list_total_mixed": "原表合计行的值，按原表保存的值存储。本期未能核对：不要与 {base} 相加",
    "hidden_excluded": "本表不含原表中被隐藏的行",
    "hidden_included": "本表包含原表中被隐藏的行",
    "ignored_columns": "原表另有未导入的列",
    "rows_after_stop": "原表在空行之后另有未导入的文字行，内容见证据面板",
    # 不写「不一致」：K / T 没通过的表不许出现「一致」二字，按子串判断时「不一致」也会撞上
    "period_conflict_accepted": "本期统计期的写法在原表或文件名中有冲突，已由用户确认接受，详见导入清单",
    "period_conflict_pending": "本期统计期的写法在原表或文件名中有冲突，尚未确认接受",
    "period_human": "本期统计期为人工录入",
    "outside_digits": "本期原表附有说明文字（可能涉及口径），内容见证据面板",
}

#: 列说明的片段模板
COLUMN_TEXT = {
    "unit": "单位：{unit}",
    "null_placeholder": "空值表示原表为{meaning}占位符，不代表零；求和时请同时统计非空个数",
    "null_blank": "空值表示原表为空格，不代表零；求和时请同时统计非空个数",
    "null_both": "空值表示原表为{meaning}占位符或空格，不代表零；求和时请同时统计非空个数",
    "date": "格式 YYYY-MM-DD",
    "date_year_from_period": "格式 YYYY-MM-DD，年份按统计期补全",
}

#: 「逐日 / 逐行」「个别日期 / 个别行」：粒度只有一列日期时按日说，否则按行说
_PER = {True: ("逐日", "日期"), False: ("逐行", "行")}
_AND = " 与 "
_LIST = "、"
_MEANING_BOTH = "无数据或不适用"
#: 交叉表合计的单一原因 → 模板键；列表合计同理。列表的 blank：合计格写了占位符（非空格，转成空值，照样生成
#: 合计格）；tiling：合计行之上没有明细行。unrecognized 只出现在公式引用（G）上
_K_REASON = {r: f"total_k_{r}" for r in ("uncached", "blank", "null_detail", "tiling", "hidden")}
_T_REASON = {r: f"list_total_{r}" for r in ("uncached", "blank", "null_detail", "tiling", "hidden")}
#: 允许出现「一致」的模板：只在对应的 K / T 通过时才会选用
CONSISTENT_KEYS = frozenset({"total_k_passed", "total_k_passed_g_unverifiable", "list_total_passed",
                             "list_total_passed_partial"})


def _tok(kind: str, name: str) -> str:
    """结构化引用 {列:名} / {表:名} / {单位:名}。渲染时换成名字，检查时遮盖。"""
    return "{" + kind + ":" + name + "}"


def _col(name: str) -> str:
    return _tok("列", name)


def _tab(name: str) -> str:
    return _tok("表", name)


def _join_and(names: list[str]) -> str:
    """a、b 与 c。"""
    if len(names) <= 1:
        return "".join(names)
    return _LIST.join(names[:-1]) + _AND + names[-1]


# ---------------------------------------------------------------- 配方结构：每张表从哪来


@dataclass
class _Writers:
    """一张表在配方里的来历（哪些工作表、分段、块写进它、它是谁的合计另存表）。"""

    sheets: list[SheetRecipe] = field(default_factory=list)
    crosstabs: list[CrosstabBlock] = field(default_factory=list)
    dims: list[DimensionSegment] = field(default_factory=list)
    measures: list[MeasuresSegment] = field(default_factory=list)
    #: 以本表为基表核对的 derived 段（本表是明细表）
    verified_by: list[DerivedSegment] = field(default_factory=list)
    #: 把合计另存进本表的 derived 段（本表是交叉表的表内合计表）
    kept_from: list[DerivedSegment] = field(default_factory=list)
    lists: list[ListBlock] = field(default_factory=list)
    #: 把合计行另存进本表的列表块
    list_totals: list[ListBlock] = field(default_factory=list)

    def add_sheet(self, sheet: SheetRecipe) -> None:
        if all(s is not sheet for s in self.sheets):
            self.sheets.append(sheet)


def _writers(recipe: Recipe) -> dict[str, _Writers]:
    out: dict[str, _Writers] = {}

    def w(table: str, sheet: SheetRecipe) -> _Writers:
        entry = out.setdefault(table, _Writers())
        entry.add_sheet(sheet)
        return entry

    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, MeasuresSegment):
                        e = w(seg.table, sheet)
                        e.measures.append(seg)
                        if block not in e.crosstabs:
                            e.crosstabs.append(block)
                    elif isinstance(seg, DimensionSegment):
                        e = w(seg.table, sheet)
                        e.dims.append(seg)
                        if block not in e.crosstabs:
                            e.crosstabs.append(block)
                    elif isinstance(seg, DerivedSegment):
                        w(seg.verify.against_table, sheet).verified_by.append(seg)
                        if seg.keep_as is not None:
                            e = w(seg.keep_as.table, sheet)
                            e.kept_from.append(seg)
                            if block not in e.crosstabs:
                                e.crosstabs.append(block)
            else:
                w(block.table, sheet).lists.append(block)
                total = block.rows.total_row
                if total is not None and total.keep_as:
                    w(total.keep_as, sheet).list_totals.append(block)
    return out


def _actual_sheet(sheet: SheetRecipe, extraction: Extraction) -> str:
    return extraction.sheets.matched.get(sheet.id) or sheet.match.name


def _lineage_spans(extraction: Extraction, table: str, sheet: str) -> list[tuple[int, int]]:
    """本表的值在这张工作表上占的行区间（来自溯源段）。判断隐藏行、停止后的行属于哪张表用。"""
    spans: list[tuple[int, int]] = []
    for runs in (extraction.lineage.get(table) or {}).values():
        for run in runs:
            try:
                run_sheet, cell, count, direction = run[1], run[2], int(run[3]), run[4]
            except (IndexError, TypeError, ValueError):
                continue
            parsed = split_coord(cell)
            if run_sheet != sheet or parsed is None:
                continue
            row = parsed[2]
            spans.append((row, row + count - 1) if direction == "down" else (row, row))
    return spans


# ---------------------------------------------------------------- 表说明的各个片段


@dataclass
class _Ctx:
    recipe: Recipe
    extraction: Extraction
    checks: dict[str, CheckResult]
    accepted: set[str]
    tables: dict[str, list[ColumnOut]]
    grains: dict[str, list[str]]
    dates: dict[str, set[str]]
    writers: dict[str, _Writers]
    problems: list[str]
    plan: Any

    def check(self, table: str, cid: str | None, what: str) -> CheckResult | None:
        c = self.checks.get(cid or "")
        if c is None:
            self.problems.append(f"表「{table}」：找不到{what}的核对结果，无法生成说明")
        return c


def _blocking(c: CheckResult) -> bool:
    """不可接受的结构类失败（K / G / T 不一致、核对 SQL 出错、落库不对）：试运行已经因此拒收、也提交不了。

    这时不写这一段说明、也不记 problems：problems 会被当成「配方本身不合法 / 说明生成失败」显示，
    把用户引去改配方，而真正的问题是那条核对。problems 只留给真正缺模板的情况。
    不写比写错安全：缺了这一句，说明里就不会有「一致」。
    """
    return c.status == "mismatch" and c.category == "structure" and not c.acceptable


def _per_day(ctx: _Ctx, table: str) -> bool:
    grain = ctx.grains.get(table, [])
    return len(grain) == 1 and grain[0] in ctx.dates.get(table, set())


def _grain_fragment(grain: list[str]) -> str | None:
    cols = [_col(g) for g in grain]
    if not cols:
        return None
    if len(cols) == 1:
        return NOTE_TEXT["grain_1"].format(a=cols[0])
    if len(cols) == 2:
        return NOTE_TEXT["grain_2"].format(a=cols[0], b=cols[1])
    return NOTE_TEXT["grain_n"].format(a=_LIST.join(cols[:-1]), b=cols[-1])


def _sum_eq_fragment(ctx: _Ctx, table: str, rel: SumEq) -> str | None:
    c = ctx.check(table, rel.id, f"关系「{rel.id}」")
    if c is None or _blocking(c):
        # 核对 SQL 出错时 status 也是 mismatch：不能当成「关系不成立」去写
        return None
    per, unit = _PER[_per_day(ctx, table)]
    total = _col(rel.total)
    if c.status == "passed":
        return NOTE_TEXT["sum_eq_passed"].format(total=total, parts=_join_and([_col(p) for p in rel.parts]), per=per)
    parts = _LIST.join(_col(p) for p in rel.parts)
    if c.status == "mismatch":
        key = "sum_eq_mismatch_accepted" if rel.id in ctx.accepted else "sum_eq_mismatch_pending"
        return NOTE_TEXT[key].format(total=total, parts=parts, unit=unit)
    if c.status == "unverifiable":
        key = "sum_eq_no_rows" if not c.checked and not c.unverifiable else "sum_eq_nulls"
        return NOTE_TEXT[key].format(total=total, parts=parts, unit=unit)
    ctx.problems.append(f"表「{table}」：关系「{rel.id}」的核对状态「{c.status}」没有对应的说明")
    return None


def _not_comparable_fragment(ctx: _Ctx, table: str, rel: NotComparable) -> str | None:
    c = ctx.check(table, rel.id, f"关系「{rel.id}」")
    if c is None or _blocking(c):
        return None
    if c.status != "info":
        ctx.problems.append(f"表「{table}」：关系「{rel.id}」的核对没有完成，无法生成说明")
        return None
    other = rel.b.table if table == rel.a.table else rel.a.table
    if c.failed <= 0:
        # 本期全部相等（或没有可比的分组）：不写「不等于」
        return NOTE_TEXT["not_comparable_equal"].format(other=_tab(other))
    dims = [g for g in ctx.grains.get(rel.a.table, []) if g != rel.by]
    if dims:
        return NOTE_TEXT["not_comparable_unequal"].format(
            other=_tab(other), dims=_LIST.join(_col(d) for d in dims), value=_col(rel.b.value))
    return NOTE_TEXT["not_comparable_unequal_flat"].format(
        other=_tab(other), a_value=_col(rel.a.value), by=_col(rel.by), value=_col(rel.b.value))


def _dim_fragments(w: _Writers) -> list[str]:
    out: list[str] = []
    for seg in w.dims:
        if seg.dim.parser != "hour_range":
            continue
        roles = {role: name for name, role in seg.dim.derive.items()}
        if "start" in roles and "end" in roles:
            text = NOTE_TEXT["hour_range"].format(dim=_col(seg.dim.name), start=_col(roles["start"]),
                                                  end=_col(roles["end"]))
        else:
            text = NOTE_TEXT["hour_range_plain"].format(dim=_col(seg.dim.name))
        if text not in out:
            out.append(text)
    for seg in w.dims:
        for name in sorted(seg.const):
            text = NOTE_TEXT["const"].format(col=_col(name))
            if text not in out:
                out.append(text)
    return out


def _base_total_fragments(w: _Writers) -> list[str]:
    """明细表上的提示：原表合计另存在哪、或者没导入。"""
    out: list[str] = []
    kept = [s.keep_as.table for s in w.verified_by if s.keep_as is not None]
    kept += [b.rows.total_row.keep_as for b in w.lists if b.rows.total_row is not None and b.rows.total_row.keep_as]
    for t in kept:
        text = NOTE_TEXT["total_kept"].format(table=_tab(t))
        if text not in out:
            out.append(text)
    dropped = any(s.keep_as is None for s in w.verified_by) or any(
        b.rows.total_row is not None and not b.rows.total_row.keep_as for b in w.lists)
    if dropped:
        out.append(NOTE_TEXT["total_not_kept"])
    return out


def _total_key(checks: list[CheckResult], by_reason: dict[str, str], mixed: str, *,
               g_checks: list[CheckResult] | tuple[CheckResult, ...] = (), passed: str,
               passed_g: str | None = None) -> str | None:
    """表内合计表按「状态 × 原因」选模板。返回 None 表示没有对应模板（调用方记 problems）。"""
    statuses = {c.status for c in checks} | {c.status for c in g_checks}
    if not checks or "mismatch" in statuses or statuses - {"passed", "unverifiable"}:
        return None
    if all(c.status == "passed" for c in checks):
        if any(g.status == "unverifiable" for g in g_checks):
            return passed_g
        return passed
    reasons = {r for c in checks if c.status == "unverifiable" for r, n in c.reasons.items() if n}
    some_passed = any(c.status == "passed" for c in checks)
    if len(reasons) == 1 and not some_passed:
        return by_reason.get(next(iter(reasons)))
    # 几种原因，或者一部分核对通过、一部分没有：用最弱的说法，不挑其中一种来写
    return mixed if reasons else None


def _kept_total_fragments(ctx: _Ctx, table: str, w: _Writers) -> list[str]:
    out: list[str] = []
    if w.kept_from:
        # 一张合计表可以由几个 derived 段写入（日间、夜间各有合计行）：所有段的 K、G 一起选一条模板
        ks = [ctx.check(table, ctx.plan.k.get(s.id), f"分段「{s.id}」的合计") for s in w.kept_from]
        gs = [ctx.check(table, ctx.plan.g[s.id], f"分段「{s.id}」的公式引用") for s in w.kept_from if s.id in ctx.plan.g]
        if None in ks or None in gs:
            return out
        if not any(_blocking(c) for c in ks + gs):
            key = _total_key(ks, _K_REASON, "total_k_mixed", g_checks=gs, passed="total_k_passed",
                             passed_g="total_k_passed_g_unverifiable")
            if key is None:
                ctx.problems.append(f"表「{table}」：合计核对 {_ids(ks + gs)} 的结果没有对应的说明")
                return out
            out.append(NOTE_TEXT[key])
        for seg in w.kept_from:
            text = NOTE_TEXT["total_overlap"].format(dim=_col(seg.keep_as.dim), base=_tab(seg.verify.against_table))
            if text not in out:
                out.append(text)
    for block in w.list_totals:
        t = ctx.check(table, ctx.plan.t.get(block.id), f"块「{block.id}」的合计行")
        if t is None or _blocking(t):
            continue
        key = _total_key([t], _T_REASON, "list_total_mixed", passed="list_total_passed")
        if key is None:
            ctx.problems.append(f"表「{table}」：合计核对 {t.id} 的结果没有对应的说明")
            continue
        blank_cols = _uncovered_total_columns(ctx, block) if key == "list_total_passed" else []
        if blank_cols:
            text = NOTE_TEXT["list_total_passed_partial"].format(
                cols=_LIST.join(_col(c) for c in blank_cols), base=_tab(block.table))
        else:
            text = NOTE_TEXT[key].format(base=_tab(block.table))
        if text not in out:
            out.append(text)
    return out


def _uncovered_total_columns(ctx: _Ctx, block: ListBlock) -> list[str]:
    """列表合计行里没有生成合计格的数字列（原表合计格是空的，P2-SPEC 4.4），按配方列序。

    这些列在合计表里存的是空值、T 也没核对过。按合计格算而不按库里的空值数算：4 参数调用（不带 null_counts）
    时照样成立。
    """
    covered = {it.base_value for it in ctx.plan.items.get(block.id, [])}
    return [c.name for c in block.columns if c.type in ("INTEGER", "REAL") and c.name not in covered]


def _ids(checks: list[CheckResult]) -> str:
    return "、".join(c.id for c in checks)


def _hidden_fragments(ctx: _Ctx, table: str, w: _Writers) -> list[str]:
    excluded = included = False
    for sheet in w.sheets:
        actual = _actual_sheet(sheet, ctx.extraction)
        info = ctx.extraction.hidden.get(actual) or {}
        rows = [int(r) for r in info.get("rows") or []]
        if not rows:
            continue
        policy = info.get("policy_rows") or sheet.hidden.rows
        spans = _lineage_spans(ctx.extraction, table, actual)
        if policy == "exclude":
            # 排除的行不在溯源里：落在本表占的行范围之内就算本表排除了它们
            lo, hi = (min(a for a, _ in spans), max(b for _, b in spans)) if spans else (None, None)
            excluded |= not spans or any(lo <= r <= hi for r in rows)
        elif policy == "include":
            included |= not spans or any(a <= r <= b for a, b in spans for r in rows)
    out = []
    if excluded:
        out.append(NOTE_TEXT["hidden_excluded"])
    if included:
        out.append(NOTE_TEXT["hidden_included"])
    return out


def _rows_after_stop_tables(ctx: _Ctx) -> set[str]:
    """rows_after_stop 问题落在哪张表上：同一工作表上被空行停下的列表块；不止一个时取紧挨在这些行之上的那个。"""
    out: set[str] = set()
    for p in ctx.extraction.problems:
        if p.code != "rows_after_stop" or not p.cells:
            continue
        parsed = split_coord(p.cells[0])
        if parsed is None or parsed[0] is None:
            continue
        sheet_name, first_row = parsed[0], parsed[2]
        blocks = [b for s in ctx.recipe.sheets if _actual_sheet(s, ctx.extraction) == sheet_name
                  for b in s.blocks if isinstance(b, ListBlock) and b.rows.blank_rows == "stop"]
        if len(blocks) > 1:
            above = [(max(b for _, b in spans), blk) for blk in blocks
                     if (spans := _lineage_spans(ctx.extraction, blk.table, sheet_name))
                     and max(b for _, b in spans) < first_row]
            if above:
                blocks = [max(above, key=lambda x: x[0])[1]]
        out.update(b.table for b in blocks)
    return out


def _period_fragments(ctx: _Ctx, w: _Writers) -> list[str]:
    if not any(s.context for s in w.sheets):
        return []
    out: list[str] = []
    # 按传进来的核对结果判（run_checks 把 C1、C2 原样排在最前），与试运行、提交时存下的那份一致
    conflicts = [c for c in ctx.checks.values()
                 if c.kind in ("context_agree", "filename_period") and c.status == "mismatch"]
    if conflicts:
        accepted = all(c.id in ctx.accepted for c in conflicts)
        out.append(NOTE_TEXT["period_conflict_accepted" if accepted else "period_conflict_pending"])
    if ctx.extraction.period is not None and ctx.extraction.period.source == "human":
        out.append(NOTE_TEXT["period_human"])
    return out


def _table_template(ctx: _Ctx, table: str, after_stop: set[str]) -> str:
    w = ctx.writers.get(table, _Writers())
    spec = next((t for t in ctx.recipe.tables if t.name == table), None)
    frags: list[str | None] = [_grain_fragment(ctx.grains.get(table, []))]
    for rel in ctx.recipe.relations:
        if isinstance(rel, SumEq) and rel.table == table:
            frags.append(_sum_eq_fragment(ctx, table, rel))
    for rel in ctx.recipe.relations:
        if isinstance(rel, NotComparable) and table in (rel.a.table, rel.b.table):
            frags.append(_not_comparable_fragment(ctx, table, rel))
    frags += _dim_fragments(w)
    frags += _base_total_fragments(w)
    frags += _kept_total_fragments(ctx, table, w)
    frags += _hidden_fragments(ctx, table, w)
    if any(b.extra_columns == "ignore" and ctx.extraction.ignored_columns.get(b.id) for b in w.lists):
        frags.append(NOTE_TEXT["ignored_columns"])
    if table in after_stop:
        frags.append(NOTE_TEXT["rows_after_stop"])
    frags += _period_fragments(ctx, w)
    sheets = {_actual_sheet(s, ctx.extraction) for s in w.sheets}
    if any(o.kind == "text_digits" and o.sheet in sheets for o in ctx.extraction.outside_text):
        frags.append(NOTE_TEXT["outside_digits"])
    if spec is not None and spec.note.strip():
        frags.append(spec.note.strip())
    return "。".join(f.strip().rstrip("。") for f in frags if f and f.strip().rstrip("。"))


# ---------------------------------------------------------------- 列说明


def _occurred(placeholders: list[Placeholder], extraction: Extraction) -> list[Placeholder]:
    """本期真的出现过的占位符（按 canon 比对原文）。"""
    seen = {canon(k) for k, n in extraction.placeholders.items() if n}
    return [p for p in placeholders if canon(p.text) in seen]


def _null_fragment(w: _Writers, extraction: Extraction) -> str | None:
    blocks: list[Any] = [*w.crosstabs, *w.lists]
    marks = [p for b in blocks for p in _occurred(list(b.values.placeholders), extraction)]
    blank = any(b.values.blank == "null" for b in blocks)
    meanings = {p.meaning for p in marks}
    meaning = next(iter(meanings)) if len(meanings) == 1 else _MEANING_BOTH
    if marks and blank:
        return COLUMN_TEXT["null_both"].format(meaning=meaning)
    if marks:
        return COLUMN_TEXT["null_placeholder"].format(meaning=meaning)
    if blank:
        return COLUMN_TEXT["null_blank"]
    return None


def _year_from_period(ctx: _Ctx, w: _Writers) -> bool:
    """轴的年份是不是从统计期补的：配了 year_from，而且本期表头不全是自带年份的日期格。"""
    for block in w.crosstabs:
        if block.axis.year_from is None:
            continue
        forms = [a.form for a in ctx.extraction.axes if a.block == block.id]
        if not forms or any(f != "date" for f in forms):
            return True
    return False


def _column_templates(ctx: _Ctx, table: str, null_counts: Mapping[str, Mapping[str, int]] | None) -> dict[str, str]:
    w = ctx.writers.get(table, _Writers())
    grain = set(ctx.grains.get(table, []))
    is_total = bool(w.kept_from or w.list_totals)
    list_dates = {c.name for b in w.lists for c in b.columns if c.type == "DATE"}
    counts = null_counts.get(table) if null_counts is not None else None
    out: dict[str, str] = {}
    for col in ctx.tables.get(table, []):
        frags: list[str] = []
        if col.role == "axis":
            frags.append(COLUMN_TEXT["date_year_from_period" if _year_from_period(ctx, w) else "date"])
        elif col.name in list_dates:
            frags.append(COLUMN_TEXT["date"])
        if col.unit:
            frags.append(COLUMN_TEXT["unit"].format(unit=_tok("单位", col.unit)))
        # 值列的空值说明：主键不会是空值；表内合计表的空值已由表说明（未能核对）交代
        value_col = (col.role in ("measure", "value") and col.type in ("INTEGER", "REAL"))
        has_nulls = counts is None or counts.get(col.name, 0) > 0
        if value_col and not is_total and col.name not in grain and has_nulls:
            if (text := _null_fragment(w, ctx.extraction)) is not None:
                frags.append(text)
        if frags:
            out[col.name] = "。".join(frags)
    return out


# ---------------------------------------------------------------- 入口


def build_notes(recipe: Recipe, extraction: Extraction, checks: list[CheckResult],
                acceptances: list[Acceptance], *,
                null_counts: Mapping[str, Mapping[str, int]] | None = None) -> SchemaNotes:
    """按核对结果生成全部表说明、列说明。problems 非空就不能保存（试运行判 recipe 类问题，提交拒绝）。

    acceptances：写了理由接受的核对。试运行回执的预览传空列表（按「全部未接受」生成），提交时传实际的接受。
    null_counts（recipe_checks.column_null_counts 的结果）：给了就只对本期真有空值的值列写空值说明。
    不给时只能按配方推断——Extraction.placeholders 是整份文件的计数、不分列，于是配了占位符且本期出现过、
    或空格存空值的块里，每个值列都写（宁可多写一句「空值表示……」，不能漏写）。
    **试运行预览和提交都应传 null_counts**：参考配方（D00）的 全日客流 列说明要是「单位：人次」
    （P2-SPEC 7.4、9.7 第 2 步），不传就会多出空值那一句。
    """
    tables, shape_problems = derive_tables(recipe)
    problems = [f"配方推出的表结构有问题：{p.message}" for p in shape_problems]
    ctx = _Ctx(
        recipe=recipe, extraction=extraction, checks={c.id: c for c in checks},
        accepted={a.check_id for a in acceptances}, tables=tables,
        grains={t.name: list(t.grain) for t in recipe.tables}, dates=date_columns(recipe),
        writers=_writers(recipe), problems=problems, plan=plan_checks(recipe, extraction),
    )
    # 只给真正建出来的表写说明（derive_tables 推出的表）；声明了却没有分段写入的表不在库里，写了也落不了地
    names = list(tables)
    known = {"列": {c.name for cols in tables.values() for c in cols},
             "表": set(names) | {t.name for t in recipe.tables}, "单位": set(UNITS)}
    after_stop = _rows_after_stop_tables(ctx)
    notes = SchemaNotes(problems=problems)
    notes.kinds = {t.name: t.kind for t in recipe.tables if t.kind == "reported_total" and t.name in tables}
    for table in names:
        template = _table_template(ctx, table, after_stop)
        columns = _column_templates(ctx, table, null_counts)
        for p in prose_problems(template, known=known):
            problems.append(f"表「{table}」的说明：{p}")
        for col, tpl in columns.items():
            for p in prose_problems(tpl, known=known):
                problems.append(f"表「{table}」列「{col}」的说明：{p}")
        if template:
            notes.templates[table] = template
        notes.templates.update({f"{table}.{col}": tpl for col, tpl in columns.items()})
        notes.tables[table] = TableNote(comment=render_prose(template),
                                        columns={col: render_prose(tpl) for col, tpl in columns.items()})
    return notes


def apply_notes(schema_cache: dict[str, Any], notes: SchemaNotes) -> dict[str, Any]:
    """把说明写进表结构，返回新 dict（不改入参）：表 comment、列 comment，顶层加 import_mode=recipe。

    表或列在 schema_cache 里找不到（不该发生：说明和库出自同一份配方）时记进 notes.problems，不悄悄跳过。
    表说明为空时保留原来的 comment。
    """
    out = copy.deepcopy(schema_cache) if schema_cache else {}
    tables = out.get("tables")
    if not isinstance(tables, dict):
        tables = {}
    for name, kind in notes.kinds.items():
        # 表的种类记进表结构：说明（不要相加）只有 db_schema 查单表时看得到，工具描述和写作目录靠它点名（AU-5）
        if isinstance(tables.get(name), dict):
            tables[name]["kind"] = kind
    for name, note in notes.tables.items():
        meta = tables.get(name)
        if not isinstance(meta, dict):
            notes.problems.append(f"表「{name}」不在表结构中，说明没有写入")
            continue
        if note.comment:
            meta["comment"] = note.comment
        columns = {c.get("name"): c for c in meta.get("columns") or [] if isinstance(c, dict)}
        for col, text in note.columns.items():
            target = columns.get(col)
            if target is None:
                notes.problems.append(f"表「{name}」没有列「{col}」，说明没有写入")
                continue
            target["comment"] = text
    out["import_mode"] = "recipe"
    return out
