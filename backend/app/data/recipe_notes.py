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

期 3（P3-SPEC 2.8）：build_notes 顺带返回每张表说明的片段（SchemaNotes.fragments，NoteFragment 的 key 是片段族、
subject 是关系 id 或表名），渲染结果与期 2 逐字相同；build_union_notes 对按期累积物化的快照库逐期调 build_notes，
按 (片段族, subject) 合并、取最弱：任一期没核对、或者还没确认接受，合并后的说法就不能比它强。
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
    NoteFragment,
    Placeholder,
    Recipe,
    SchemaNotes,
    SheetRecipe,
    SumEq,
    TableNote,
    UnionNotePart,
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
    # ---- 期 3：按配方忽略的行和格（单期也用，P3-SPEC 3.2 ⑥）
    "ignored_rows": "原表另有按配方忽略的行，内容见证据面板",
    "ignored_cells": "原表另有按配方忽略的单元格，内容见证据面板",
    # ---- 期 3：按期累积的并集说明（P3-SPEC 2.8）。各期合并时取最弱：部分期没核对、还没确认接受，都不能写成
    # 「已核对」「已由用户确认接受」；不含「一致」（CONSISTENT_KEYS 之外）
    "accumulate": "本表按统计期累积多期数据，各期互不重叠：比较不同期时请按 {col} 筛选",
    "accumulate_gap": "本表按统计期累积多期数据，各期互不重叠，但各期之间有空缺：比较不同期之前请先查询 {col} 的覆盖范围",
    "single_after_drop": "当前版本只含最近一期的数据，此前各期不在当前版本中",
    "table_partial_periods": "部分期没有这张表的数据",
    "table_absent": "当前版本的数据中没有这张表的行",
    "sum_eq_union_mixed": "{total} 与 {parts} 之和的关系在部分期的个别{unit}不成立或未能核对，详见导入清单："
                          "不要据此推算，也不要把这几列相加",
    "sum_eq_union_pending": "{total} 与 {parts} 之和的关系在部分期的个别{unit}不成立，尚未确认接受："
                            "不要据此推算，也不要把这几列相加",
    "sum_eq_union_partial": "{total} 与 {parts} 之和的关系只在部分期登记并核对，其余各期未核对，详见导入清单："
                            "不要据此推算，也不要把这几列相加",
    "sum_eq_members_vary": "{total} 等于各期登记的分项列之和，各期登记的分项列不同（某期没有的列为空值），导入时已逐期核对："
                           "求合计只用 {total}，不要把这几列相加",
    "total_union_unverified": "原表写明的合计，按原表保存的值存储。部分期的合计值未能核对，详见导入清单",
    "period_conflict_union": "部分期的统计期写法在原表或文件名中有冲突，已由用户确认接受，详见导入清单",
    "period_conflict_union_pending": "部分期的统计期写法在原表或文件名中有冲突，尚未确认接受",
    "period_human_union": "部分期的统计期为人工录入",
    "period_human_all": "各期的统计期均为人工录入",
    "outside_digits_union": "部分期的原表附有说明文字（可能涉及口径），内容见证据面板",
    "hidden_excluded_union": "部分期的原表有被隐藏的行，本表不含这些行",
    "hidden_included_union": "部分期的原表有被隐藏的行，本表包含这些行",
    "ignored_columns_union": "部分期的原表另有未导入的列",
    "ignored_rows_union": "部分期的原表另有按配方忽略的行，内容见证据面板",
    "ignored_cells_union": "部分期的原表另有按配方忽略的单元格，内容见证据面板",
    "dim_values_vary": "各期的 {dim} 取值不完全相同：跨期比较前请先确认两期都有的取值",
}

#: 列说明的片段模板
COLUMN_TEXT = {
    "unit": "单位：{unit}",
    "null_placeholder": "空值表示原表为{meaning}占位符，不代表零；求和时请同时统计非空个数",
    "null_blank": "空值表示原表为空格，不代表零；求和时请同时统计非空个数",
    "null_both": "空值表示原表为{meaning}占位符或空格，不代表零；求和时请同时统计非空个数",
    "date": "格式 YYYY-MM-DD",
    "date_year_from_period": "格式 YYYY-MM-DD，年份按统计期补全",
    # ---- 期 3：并集里的列（P3-SPEC 2.8 第 4 步）。缺这一列的期是结构性空值，不能说成「原表为空格」
    "col_partial_periods": "部分期没有这一列的数据，为空值",
    "col_absent": "当前版本的数据中没有这一列，为空值",
    "col_retired": "这一列自某一期起不再导入，此后各期为空值",
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


def _frag(key: str, subject: str | None, text: str | None) -> NoteFragment | None:
    """一个片段：key 是 NOTE_TEXT 的键（用户写的表说明是 note），subject 是关系 id 或表名。text 为空时 None。"""
    return NoteFragment(key=key, subject=subject, template=text) if text else None


def _grain_fragment(table: str, grain: list[str]) -> NoteFragment | None:
    cols = [_col(g) for g in grain]
    if not cols:
        return None
    if len(cols) == 1:
        return _frag("grain_1", table, NOTE_TEXT["grain_1"].format(a=cols[0]))
    if len(cols) == 2:
        return _frag("grain_2", table, NOTE_TEXT["grain_2"].format(a=cols[0], b=cols[1]))
    return _frag("grain_n", table, NOTE_TEXT["grain_n"].format(a=_LIST.join(cols[:-1]), b=cols[-1]))


def _sum_eq_fragment(ctx: _Ctx, table: str, rel: SumEq) -> NoteFragment | None:
    c = ctx.check(table, rel.id, f"关系「{rel.id}」")
    if c is None or _blocking(c):
        # 核对 SQL 出错时 status 也是 mismatch：不能当成「关系不成立」去写
        return None
    per, unit = _PER[_per_day(ctx, table)]
    total = _col(rel.total)
    if c.status == "passed":
        return _frag("sum_eq_passed", rel.id, NOTE_TEXT["sum_eq_passed"].format(
            total=total, parts=_join_and([_col(p) for p in rel.parts]), per=per))
    parts = _LIST.join(_col(p) for p in rel.parts)
    if c.status == "mismatch":
        key = "sum_eq_mismatch_accepted" if rel.id in ctx.accepted else "sum_eq_mismatch_pending"
        return _frag(key, rel.id, NOTE_TEXT[key].format(total=total, parts=parts, unit=unit))
    if c.status == "unverifiable":
        key = "sum_eq_no_rows" if not c.checked and not c.unverifiable else "sum_eq_nulls"
        return _frag(key, rel.id, NOTE_TEXT[key].format(total=total, parts=parts, unit=unit))
    ctx.problems.append(f"表「{table}」：关系「{rel.id}」的核对状态「{c.status}」没有对应的说明")
    return None


def _not_comparable_fragment(ctx: _Ctx, table: str, rel: NotComparable) -> NoteFragment | None:
    c = ctx.check(table, rel.id, f"关系「{rel.id}」")
    if c is None or _blocking(c):
        return None
    if c.status != "info":
        ctx.problems.append(f"表「{table}」：关系「{rel.id}」的核对没有完成，无法生成说明")
        return None
    other = rel.b.table if table == rel.a.table else rel.a.table
    if c.failed <= 0:
        # 本期全部相等（或没有可比的分组）：不写「不等于」
        return _frag("not_comparable_equal", rel.id, NOTE_TEXT["not_comparable_equal"].format(other=_tab(other)))
    dims = [g for g in ctx.grains.get(rel.a.table, []) if g != rel.by]
    if dims:
        return _frag("not_comparable_unequal", rel.id, NOTE_TEXT["not_comparable_unequal"].format(
            other=_tab(other), dims=_LIST.join(_col(d) for d in dims), value=_col(rel.b.value)))
    return _frag("not_comparable_unequal_flat", rel.id, NOTE_TEXT["not_comparable_unequal_flat"].format(
        other=_tab(other), a_value=_col(rel.a.value), by=_col(rel.by), value=_col(rel.b.value)))


def _add_unique(out: list[NoteFragment], frag: NoteFragment | None) -> None:
    """同一句只写一次（几个分段写进同一张表时，它们的提示往往相同）。"""
    if frag is not None and all(f.template != frag.template for f in out):
        out.append(frag)


def _dim_fragments(w: _Writers) -> list[NoteFragment]:
    out: list[NoteFragment] = []
    for seg in w.dims:
        if seg.dim.parser != "hour_range":
            continue
        roles = {role: name for name, role in seg.dim.derive.items()}
        if "start" in roles and "end" in roles:
            frag = _frag("hour_range", seg.dim.name, NOTE_TEXT["hour_range"].format(
                dim=_col(seg.dim.name), start=_col(roles["start"]), end=_col(roles["end"])))
        else:
            frag = _frag("hour_range_plain", seg.dim.name, NOTE_TEXT["hour_range_plain"].format(dim=_col(seg.dim.name)))
        _add_unique(out, frag)
    for seg in w.dims:
        for name in sorted(seg.const):
            _add_unique(out, _frag("const", name, NOTE_TEXT["const"].format(col=_col(name))))
    return out


def _base_total_fragments(w: _Writers) -> list[NoteFragment]:
    """明细表上的提示：原表合计另存在哪、或者没导入。"""
    out: list[NoteFragment] = []
    kept = [s.keep_as.table for s in w.verified_by if s.keep_as is not None]
    kept += [b.rows.total_row.keep_as for b in w.lists if b.rows.total_row is not None and b.rows.total_row.keep_as]
    for t in kept:
        _add_unique(out, _frag("total_kept", t, NOTE_TEXT["total_kept"].format(table=_tab(t))))
    dropped = any(s.keep_as is None for s in w.verified_by) or any(
        b.rows.total_row is not None and not b.rows.total_row.keep_as for b in w.lists)
    if dropped:
        out.append(NoteFragment("total_not_kept", None, NOTE_TEXT["total_not_kept"]))
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


def _kept_total_fragments(ctx: _Ctx, table: str, w: _Writers) -> list[NoteFragment]:
    out: list[NoteFragment] = []
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
            out.append(NoteFragment(key, table, NOTE_TEXT[key]))
        for seg in w.kept_from:
            _add_unique(out, _frag("total_overlap", seg.verify.against_table, NOTE_TEXT["total_overlap"].format(
                dim=_col(seg.keep_as.dim), base=_tab(seg.verify.against_table))))
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
            frag = _frag("list_total_passed_partial", table, NOTE_TEXT["list_total_passed_partial"].format(
                cols=_LIST.join(_col(c) for c in blank_cols), base=_tab(block.table)))
        else:
            frag = _frag(key, table, NOTE_TEXT[key].format(base=_tab(block.table)))
        _add_unique(out, frag)
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


def _hidden_fragments(ctx: _Ctx, table: str, w: _Writers) -> list[NoteFragment]:
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
        out.append(NoteFragment("hidden_excluded", None, NOTE_TEXT["hidden_excluded"]))
    if included:
        out.append(NoteFragment("hidden_included", None, NOTE_TEXT["hidden_included"]))
    return out


def _ignored_fragments(ctx: _Ctx, w: _Writers) -> list[NoteFragment]:
    """按配方忽略的列、行、单元格（期 3 ⑥）：本期确实忽略了才写，内容只进证据面板，说明里不写原文。

    - 列：期 2 的列表 extra_columns=ignore，加上期 3 的 ignore_columns（交叉表、列表），本期 ignored_columns 里
      这个块有表头才写。期 2 的配方没有 ignore_columns，判断与期 2 完全相同；
    - 行：rows_excluded 里 reason=ignored_rows、block 是写入本表的块；
    - 单元格：rows_excluded 里 reason=ignored_outside、工作表是写入本表的工作表（区域外的格不属于任何块）。
    """
    out: list[NoteFragment] = []
    ignored = ctx.extraction.ignored_columns
    blocks: list[Any] = [*w.lists, *w.crosstabs]
    if any((getattr(b, "extra_columns", None) == "ignore" or b.ignore_columns) and ignored.get(b.id) for b in blocks):
        out.append(NoteFragment("ignored_columns", None, NOTE_TEXT["ignored_columns"]))
    excluded = [_excluded(e) for e in ctx.extraction.rows_excluded or []]
    ids = {b.id for b in blocks}
    if any(e["reason"] == "ignored_rows" and e["block"] in ids and e["rows"] for e in excluded):
        out.append(NoteFragment("ignored_rows", None, NOTE_TEXT["ignored_rows"]))
    sheets = {_actual_sheet(s, ctx.extraction) for s in w.sheets}
    if any(e["reason"] == "ignored_outside" and e["sheet"] in sheets and e["rows"] for e in excluded):
        out.append(NoteFragment("ignored_cells", None, NOTE_TEXT["ignored_cells"]))
    return out


def _excluded(e: Any) -> dict[str, Any]:
    """ExcludedRows 或它的 JSON 形状（暂存区、清单里存的是 asdict 之后的样子）→ 统一的 dict。"""
    get = e.get if isinstance(e, dict) else (lambda k: getattr(e, k, None))
    return {"reason": get("reason"), "block": get("block"), "sheet": get("sheet"), "rows": get("rows") or []}


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
        on_sheet = [b for s in ctx.recipe.sheets if _actual_sheet(s, ctx.extraction) == sheet_name
                    for b in s.blocks if isinstance(b, ListBlock)]
        # 消息里写着「列表「块」」：恰好认出一个块就用它。跳过空行的列表按表下说明收尾时也报这个问题（WP-8），
        # 下面按 blank_rows=stop 找的办法找不到它
        named = [b for b in on_sheet if f"列表「{b.id}」" in p.message]
        if len(named) == 1:
            out.add(named[0].table)
            continue
        blocks = [b for b in on_sheet if b.rows.blank_rows == "stop"]
        if len(blocks) > 1:
            above = [(max(b for _, b in spans), blk) for blk in blocks
                     if (spans := _lineage_spans(ctx.extraction, blk.table, sheet_name))
                     and max(b for _, b in spans) < first_row]
            if above:
                blocks = [max(above, key=lambda x: x[0])[1]]
        out.update(b.table for b in blocks)
    return out


def _period_fragments(ctx: _Ctx, w: _Writers) -> list[NoteFragment]:
    if not any(s.context for s in w.sheets):
        return []
    out: list[NoteFragment] = []
    # 按传进来的核对结果判（run_checks 把 C1、C2 原样排在最前），与试运行、提交时存下的那份一致
    conflicts = [c for c in ctx.checks.values()
                 if c.kind in ("context_agree", "filename_period") and c.status == "mismatch"]
    if conflicts:
        key = "period_conflict_accepted" if all(c.id in ctx.accepted for c in conflicts) else "period_conflict_pending"
        out.append(NoteFragment(key, None, NOTE_TEXT[key]))
    if ctx.extraction.period is not None and ctx.extraction.period.source == "human":
        out.append(NoteFragment("period_human", None, NOTE_TEXT["period_human"]))
    return out


def _table_fragments(ctx: _Ctx, table: str, after_stop: set[str]) -> list[NoteFragment]:
    """一张表说明的全部片段，按拼接顺序。顺序与期 2 的 _table_template 相同：渲染结果逐字不变。"""
    w = ctx.writers.get(table, _Writers())
    spec = next((t for t in ctx.recipe.tables if t.name == table), None)
    frags: list[NoteFragment | None] = [_grain_fragment(table, ctx.grains.get(table, []))]
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
    frags += _ignored_fragments(ctx, w)
    if table in after_stop:
        frags.append(NoteFragment("rows_after_stop", None, NOTE_TEXT["rows_after_stop"]))
    frags += _period_fragments(ctx, w)
    sheets = {_actual_sheet(s, ctx.extraction) for s in w.sheets}
    if any(o.kind == "text_digits" and o.sheet in sheets for o in ctx.extraction.outside_text):
        frags.append(NoteFragment("outside_digits", None, NOTE_TEXT["outside_digits"]))
    if spec is not None and spec.note.strip():
        # 用户写的表说明不是 NOTE_TEXT 里的模板，片段族记作 note
        frags.append(NoteFragment("note", table, spec.note.strip()))
    return [f for f in frags if f is not None and _clean(f.template)]


def _clean(text: str) -> str:
    return text.strip().rstrip("。")


def _join(frags: list[NoteFragment]) -> str:
    """片段用「。」拼接（每段去掉首尾空白和句末的「。」，空段跳过）。"""
    return "。".join(_clean(f.template) for f in frags if _clean(f.template))


# ---------------------------------------------------------------- 列说明


def _occurred(placeholders: list[Placeholder], extraction: Extraction) -> list[Placeholder]:
    """本期真的出现过的占位符（按 canon 比对原文）。"""
    seen = {canon(k) for k, n in extraction.placeholders.items() if n}
    return [p for p in placeholders if canon(p.text) in seen]


def _null_sources(w: _Writers, extraction: Extraction) -> tuple[set[str], bool]:
    """本表空值的来源：本期出现过的占位符的含义集合，以及有没有块把空格存成空值。"""
    blocks: list[Any] = [*w.crosstabs, *w.lists]
    meanings = {p.meaning for b in blocks for p in _occurred(list(b.values.placeholders), extraction)}
    return meanings, any(b.values.blank == "null" for b in blocks)


def _null_text(meanings: set[str], blank: bool) -> str | None:
    """占位符的含义不止一种（几期、几个块各说各的）时写「无数据或不适用」。"""
    meaning = next(iter(meanings)) if len(meanings) == 1 else _MEANING_BOTH
    if meanings and blank:
        return COLUMN_TEXT["null_both"].format(meaning=meaning)
    if meanings:
        return COLUMN_TEXT["null_placeholder"].format(meaning=meaning)
    if blank:
        return COLUMN_TEXT["null_blank"]
    return None


def _null_fragment(w: _Writers, extraction: Extraction) -> str | None:
    return _null_text(*_null_sources(w, extraction))


def _year_from_period(ctx: _Ctx, w: _Writers) -> bool:
    """轴的年份是不是从统计期补的：配了 year_from，而且本期表头不全是自带年份的日期格。"""
    for block in w.crosstabs:
        if block.axis.year_from is None:
            continue
        forms = [a.form for a in ctx.extraction.axes if a.block == block.id]
        if not forms or any(f != "date" for f in forms):
            return True
    return False


def _date_key(ctx: _Ctx, w: _Writers, table: str, col: ColumnOut) -> str | None:
    """日期列说明用哪条模板（None = 不是日期列）。"""
    if col.role == "axis":
        return "date_year_from_period" if _year_from_period(ctx, w) else "date"
    if col.name in {c.name for b in w.lists for c in b.columns if c.type == "DATE"}:
        return "date"
    return None


def _null_eligible(ctx: _Ctx, w: _Writers, table: str, col: ColumnOut) -> bool:
    """这一列要不要考虑空值说明：值列；主键不会是空值；表内合计表的空值已由表说明（未能核对）交代。"""
    value_col = col.role in ("measure", "value") and col.type in ("INTEGER", "REAL")
    is_total = bool(w.kept_from or w.list_totals)
    return value_col and not is_total and col.name not in set(ctx.grains.get(table, []))


def _column_templates(ctx: _Ctx, table: str, null_counts: Mapping[str, Mapping[str, int]] | None) -> dict[str, str]:
    w = ctx.writers.get(table, _Writers())
    counts = null_counts.get(table) if null_counts is not None else None
    out: dict[str, str] = {}
    for col in ctx.tables.get(table, []):
        frags: list[str] = []
        if (date := _date_key(ctx, w, table, col)) is not None:
            frags.append(COLUMN_TEXT[date])
        if col.unit:
            frags.append(COLUMN_TEXT["unit"].format(unit=_tok("单位", col.unit)))
        has_nulls = counts is None or counts.get(col.name, 0) > 0
        if _null_eligible(ctx, w, table, col) and has_nulls:
            if (text := _null_fragment(w, ctx.extraction)) is not None:
                frags.append(text)
        if frags:
            out[col.name] = "。".join(frags)
    return out


# ---------------------------------------------------------------- 入口


def _make_ctx(recipe: Recipe, extraction: Extraction, checks: list[CheckResult],
              acceptances: list[Acceptance]) -> _Ctx:
    tables, shape_problems = derive_tables(recipe)
    problems = [f"配方推出的表结构有问题：{p.message}" for p in shape_problems]
    return _Ctx(
        recipe=recipe, extraction=extraction, checks={c.id: c for c in checks},
        accepted={a.check_id for a in acceptances}, tables=tables,
        grains={t.name: list(t.grain) for t in recipe.tables}, dates=date_columns(recipe),
        writers=_writers(recipe), problems=problems, plan=plan_checks(recipe, extraction),
    )


def _check_prose(notes: SchemaNotes, table: str, template: str, columns: dict[str, str],
                 known: dict[str, set[str]]) -> None:
    for p in prose_problems(template, known=known):
        notes.problems.append(f"表「{table}」的说明：{p}")
    for col, tpl in columns.items():
        for p in prose_problems(tpl, known=known):
            notes.problems.append(f"表「{table}」列「{col}」的说明：{p}")


def _store(notes: SchemaNotes, table: str, frags: list[NoteFragment], columns: dict[str, str]) -> None:
    template = _join(frags)
    if template:
        notes.templates[table] = template
    notes.templates.update({f"{table}.{col}": tpl for col, tpl in columns.items()})
    notes.fragments[table] = list(frags)
    notes.tables[table] = TableNote(comment=render_prose(template),
                                    columns={col: render_prose(tpl) for col, tpl in columns.items()})


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

    期 3：fragments 记下每张表说明的片段（按拼接顺序），给 build_union_notes 逐期合并用；templates、tables 的
    渲染结果与期 2 逐字相同。
    """
    ctx = _make_ctx(recipe, extraction, checks, acceptances)
    tables = ctx.tables
    # 只给真正建出来的表写说明（derive_tables 推出的表）；声明了却没有分段写入的表不在库里，写了也落不了地
    names = list(tables)
    known = {"列": {c.name for cols in tables.values() for c in cols},
             "表": set(names) | {t.name for t in recipe.tables}, "单位": set(UNITS)}
    after_stop = _rows_after_stop_tables(ctx)
    notes = SchemaNotes(problems=ctx.problems)
    notes.kinds = {t.name: t.kind for t in recipe.tables if t.kind == "reported_total" and t.name in tables}
    for table in names:
        frags = _table_fragments(ctx, table, after_stop)
        columns = _column_templates(ctx, table, null_counts)
        _check_prose(notes, table, _join(frags), columns, known)
        _store(notes, table, frags, columns)
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


# ---------------------------------------------------------------- 期 3：按期累积的并集说明（P3-SPEC 2.8）

#: 片段 key → 片段族。合并按 (片段族, subject) 进行；同一族的几个 key 是同一件事的几种结论
_FAMILY: dict[str, str] = {
    **{k: "grain" for k in ("grain_1", "grain_2", "grain_n")},
    **{k: "sum_eq" for k in ("sum_eq_passed", "sum_eq_mismatch_accepted", "sum_eq_mismatch_pending", "sum_eq_nulls",
                             "sum_eq_no_rows")},
    **{k: "not_comparable" for k in ("not_comparable_unequal", "not_comparable_unequal_flat", "not_comparable_equal")},
    "hour_range": "hour_range", "hour_range_plain": "hour_range", "const": "const",
    "total_kept": "total_kept", "total_not_kept": "total_not_kept",
    **{k: "total_k" for k in NOTE_TEXT if k.startswith("total_k_")},
    **{k: "list_total" for k in NOTE_TEXT if k.startswith("list_total_")},
    "total_overlap": "total_overlap",
    "hidden_excluded": "hidden_excluded", "hidden_included": "hidden_included",
    "ignored_columns": "ignored_columns", "ignored_rows": "ignored_rows", "ignored_cells": "ignored_cells",
    "rows_after_stop": "rows_after_stop",
    "period_conflict_accepted": "period_conflict", "period_conflict_pending": "period_conflict",
    "period_human": "period_human", "outside_digits": "outside_digits",
    "note": "note",
}
#: 合并后的拼接顺序（与 _table_fragments 一致；累积特有的几句排在用户写的表说明之前）
_FAMILY_ORDER = ("grain", "sum_eq", "not_comparable", "hour_range", "const", "total_kept", "total_not_kept", "total_k",
                 "list_total", "total_overlap", "hidden_excluded", "hidden_included", "ignored_columns",
                 "ignored_rows", "ignored_cells", "rows_after_stop", "period_conflict", "period_human",
                 "outside_digits", "union", "note")
#: 与核对结论无关、只描述表结构的片段族：用目标配方那一期的；只在早期出现的 subject（退役的表、另存表）用
#: 最后一个含它的那一期的
_STRUCTURAL = frozenset({"grain", "hour_range", "const", "total_kept", "total_not_kept", "total_overlap", "note"})
#: 「任一期出现就写」的片段族 → 并集里的说法
_ANY_UNION = {"hidden_excluded": "hidden_excluded_union", "hidden_included": "hidden_included_union",
              "ignored_columns": "ignored_columns_union", "ignored_rows": "ignored_rows_union",
              "ignored_cells": "ignored_cells_union", "outside_digits": "outside_digits_union"}
_K_PASSED = frozenset({"total_k_passed", "total_k_passed_g_unverifiable"})


@dataclass
class _UPart:
    """并集里的一期：它自己的说明（按它自己的配方、核对、接受生成）和推导出的表结构。"""

    index: int
    src: UnionNotePart
    ctx: _Ctx
    notes: SchemaNotes
    #: 表 → {列名: ColumnOut}
    cols: dict[str, dict[str, ColumnOut]]

    def label(self) -> str:
        return f"第 {self.index + 1} 期（{self.src.start} 至 {self.src.end}）"


def _canonical(recipe: Recipe) -> dict[str, Any]:
    return recipe.model_dump(mode="json", exclude_defaults=True)


def _relation(recipe: Recipe, rid: str | None) -> Any:
    return next((r for r in recipe.relations if r.id == rid), None)


def _pick(group: dict[int, NoteFragment], having: list[int], ref: int) -> tuple[int, NoteFragment] | None:
    """结构类片段取哪一期的：目标那一期有就用它；否则用最后一个还有它的那一期。"""
    if ref in group:
        return ref, group[ref]
    for i in reversed(having):
        if i in group:
            return i, group[i]
    return None


def _sum_eq_slots(part: _UPart, table: str, rid: str | None) -> dict[str, str] | None:
    rel = _relation(part.src.recipe, rid)
    if not isinstance(rel, SumEq):
        return None
    _, unit = _PER[_per_day(part.ctx, table)]
    return {"total": _col(rel.total), "parts": _LIST.join(_col(p) for p in rel.parts), "unit": unit}


def _sum_eq_members(recipe: Recipe, rid: str | None) -> tuple[str, tuple[str, ...]] | None:
    """一期配方里这条 sum_eq 的成员：(合计列, 排好序的分项列)。分项的先后不影响关系的含义。"""
    rel = _relation(recipe, rid)
    return (rel.total, tuple(sorted(rel.parts))) if isinstance(rel, SumEq) else None


def _merge_sum_eq(table: str, subject: str | None, group: dict[int, NoteFragment], having: list[int], ref: int,
                  parts: list[_UPart], problems: list[str]) -> NoteFragment | None:
    """sum_eq 按各期最弱合并（P3-SPEC 2.8 表）。优先级：有一期尚未确认接受 > 部分期没有这条关系 > 有一期没成立或
    没核对 > 各期成员不同 > 各期都通过且成员相同。理由：前两种都不能写「已核对」「已由用户确认接受」；尚未接受的
    排最前，是因为那一期还要人处理，界面上要看得见。"""
    present = [group[i] for i in having if i in group]
    keys = {f.key for f in present}
    picked = _pick(group, having, ref)
    if picked is None:
        return None
    src = parts[picked[0]]
    slots = _sum_eq_slots(src, table, subject)
    if slots is None:
        problems.append(f"表「{table}」：{src.label()}的配方里找不到关系「{subject}」，无法生成说明")
        return None
    partial = len(present) < len(having)
    if "sum_eq_mismatch_pending" in keys:
        key = "sum_eq_union_pending"
    elif partial:
        key = "sum_eq_union_partial"
    elif keys - {"sum_eq_passed"}:
        key = "sum_eq_union_mixed"
    elif len(members := {_sum_eq_members(parts[i].src.recipe, subject) for i in having if i in group}) == 1:
        # 各期登记的合计列和分项列相同（只比集合：分项只是调了顺序、或者逐日与逐行的说法不同，都不算成员不同），
        # 用目标那一期的句子。按渲染出的句子比的话，调换分项顺序就会写成「各期登记的分项列不同」，而这句会进
        # 模型可见的说明（评审意见）
        return NoteFragment("sum_eq_passed", subject, picked[1].template)
    else:
        totals = {m[0] for m in members if m is not None}
        # 各期的合计列都相同，只是分项不同，才能说「{total} 等于各期登记的分项列之和」；合计列也换过的，那几期的
        # 合计不在这一列里，按「只在部分期核对」这种更弱的说法写
        key = "sum_eq_members_vary" if len(totals) == 1 else "sum_eq_union_partial"
    return NoteFragment(key, subject, NOTE_TEXT[key].format(**slots))


def _merge_not_comparable(table: str, subject: str | None, group: dict[int, NoteFragment], having: list[int],
                          ref: int, parts: list[_UPart], problems: list[str]) -> NoteFragment | None:
    picked = _pick(group, having, ref)
    if picked is None:
        return None
    present = [group[i] for i in having if i in group]
    if len(present) == len(having) and all(f.key.startswith("not_comparable_unequal") for f in present):
        return NoteFragment(picked[1].key, subject, picked[1].template)
    # 任一期全部相等、或者部分期没有这条关系：不写「不等于」
    rel = _relation(parts[picked[0]].src.recipe, subject)
    if not isinstance(rel, NotComparable):
        problems.append(f"表「{table}」：{parts[picked[0]].label()}的配方里找不到关系「{subject}」，无法生成说明")
        return None
    other = rel.b.table if table == rel.a.table else rel.a.table
    return NoteFragment("not_comparable_equal", subject, NOTE_TEXT["not_comparable_equal"].format(other=_tab(other)))


def _merge_group(table: str, family: str, subject: str | None, group: dict[int, NoteFragment], having: list[int],
                 ref: int, parts: list[_UPart], problems: list[str]) -> NoteFragment | None:
    """一组 (片段族, subject) 跨期合并成一个片段（两期及以上时）。group：期的下标 → 那一期的片段。

    「部分期缺失」只在含这张表的那几期里算（having）：不含这张表的期在并集里没有这张表的行，不需要它的核对。
    这样退役的合计表只在早期出现时，说明取最后一个含它的那一期的（P3-SPEC 2.8 的例子），而同一张表里某一期
    没登记这条关系，照样按「部分期未核对」写。"""
    present = [group[i] for i in having if i in group]
    if family in _STRUCTURAL:
        picked = _pick(group, having, ref)
        return NoteFragment(picked[1].key, subject, picked[1].template) if picked else None
    if family == "sum_eq":
        return _merge_sum_eq(table, subject, group, having, ref, parts, problems)
    if family == "not_comparable":
        return _merge_not_comparable(table, subject, group, having, ref, parts, problems)
    if family == "total_k":
        if len(present) == len(having) and {f.key for f in present} <= _K_PASSED:
            key = ("total_k_passed_g_unverifiable" if any(f.key == "total_k_passed_g_unverifiable" for f in present)
                   else "total_k_passed")
        else:
            key = "total_union_unverified"
        return NoteFragment(key, subject, NOTE_TEXT[key])
    if family in _ANY_UNION:
        key = _ANY_UNION[family]
        return NoteFragment(key, subject, NOTE_TEXT[key])
    if family == "period_conflict":
        key = ("period_conflict_union_pending" if any(f.key == "period_conflict_pending" for f in present)
               else "period_conflict_union")
        return NoteFragment(key, subject, NOTE_TEXT[key])
    if family == "period_human":
        key = "period_human_all" if len(present) == len(having) else "period_human_union"
        return NoteFragment(key, subject, NOTE_TEXT[key])
    # list_total、rows_after_stop 只有列表才有，而列表不能按期累积（P3-SPEC 2.2）；不认识的片段族同样没有并集说法。
    # 记 problems（不能保存），绝不沿用某一期的单期说法。消息给人看，不露片段族的键名
    problems.append(f"表「{table}」：{_UNMERGEABLE.get(family, '有一段说明')}不能按期合并，无法生成说明")
    return None


#: 没有并集说法的片段族 → 给人看的叫法（problems 里不露键名）
_UNMERGEABLE = {"list_total": "列表合计行的核对结论", "rows_after_stop": "列表空行之后的文字行"}


def _axis_column(recipe: Recipe, table: str) -> str | None:
    tables, _ = derive_tables(recipe)
    axes = [c.name for c in tables.get(table, []) if c.role == "axis"]
    return axes[0] if len(axes) == 1 else None


def _union_table_fragments(table: str, having: list[int], parts: list[_UPart], ref: int, target: Recipe,
                           target_tables: dict[str, list[ColumnOut]], label_sets: list[dict], gaps: bool,
                           problems: list[str]) -> list[NoteFragment]:
    single = len(parts) == 1
    out: list[tuple[tuple[int, int], NoteFragment]] = []
    if single:
        # 物化的单期快照（移除、作废之后，P3-SPEC 7.4）：那一期自己的说法就是准确的，不说「部分期」
        out = [((_FAMILY_ORDER.index(_FAMILY.get(f.key, "note")), n), f)
               for n, f in enumerate(parts[0].notes.fragments.get(table, []))]
    else:
        groups: dict[tuple[str, str | None], dict[int, NoteFragment]] = {}
        seen: dict[tuple[str, str | None], int] = {}
        # 先按目标那一期的片段顺序，再按其余各期从新到旧，记下每组第一次出现的位置
        visit = [ref] + [i for i in reversed(having) if i != ref]
        for i in visit:
            for f in parts[i].notes.fragments.get(table, []):
                family = _FAMILY.get(f.key)
                if family is None:
                    problems.append(f"表「{table}」：{parts[i].label()}有一段说明不能按期合并，无法生成说明")
                    continue
                k = (family, f.subject)
                groups.setdefault(k, {}).setdefault(i, f)
                seen.setdefault(k, len(seen))
        for (family, subject), group in groups.items():
            merged = _merge_group(table, family, subject, group, having, ref, parts, problems)
            if merged is not None:
                out.append(((_FAMILY_ORDER.index(family), seen[(family, subject)]), merged))
    union: list[NoteFragment] = []
    if not having:
        if target_table := next((t for t in target.tables if t.name == table), None):
            if (g := _grain_fragment(table, list(target_table.grain))) is not None:
                union.append(g)
        union.append(NoteFragment("table_absent", table, NOTE_TEXT["table_absent"]))
    if single:
        union.append(NoteFragment("single_after_drop", table, NOTE_TEXT["single_after_drop"]))
    elif having:
        recipe = target if table in target_tables else parts[having[-1]].src.recipe
        axis = _axis_column(recipe, table)
        if axis is None:
            problems.append(f"表「{table}」：找不到日期列，无法写按期累积的说明")
        else:
            key = "accumulate_gap" if gaps else "accumulate"
            union.append(NoteFragment(key, table, NOTE_TEXT[key].format(col=_col(axis))))
        if len(having) < len(parts):
            union.append(NoteFragment("table_partial_periods", table, NOTE_TEXT["table_partial_periods"]))
        dims: list[str] = []
        for entry in label_sets or []:
            col = str(entry.get("column") or "") if isinstance(entry, dict) else ""
            if entry.get("table") == table and col and col not in dims:
                dims.append(col)
                union.append(NoteFragment("dim_values_vary", col, NOTE_TEXT["dim_values_vary"].format(dim=_col(col))))
    pos = _FAMILY_ORDER.index("union")
    out += [((pos, n), f) for n, f in enumerate(union)]
    out.sort(key=lambda x: x[0])
    return [f for _, f in out if _clean(f.template)]


def _union_column_templates(table: str, columns: list[ColumnOut], having: list[int], parts: list[_UPart],
                            target_cols: dict[str, ColumnOut] | None, retired: set[tuple[str, str]],
                            union_kinds: dict[str, str], null_counts: Mapping[str, Mapping[str, int]] | None,
                            part_null_counts: list[dict[str, dict[str, int]]] | None) -> dict[str, str]:
    """并集里每一列的说明（P3-SPEC 2.8 第 4 步）：
    - 日期：任一期年份取自统计期就用「年份按统计期补全」；
    - 单位：目标配方里有的列取目标配方的，退役列取最后一个还含它的那一期的；
    - 空值：只按含这一列的那几期统计（每期的空值数、那几期出现过的占位符、空格存空值的设置）。不含这一列的期是
      结构性空值，由下一句说明，不能说成「原表为空格」；
    - 部分期没有这一列：col_partial_periods；退役列：col_retired；哪一期都没有：col_absent。"""
    counts_total = null_counts.get(table) if null_counts is not None else None
    out: dict[str, str] = {}
    for col in columns:
        with_col = [i for i in having if col.name in parts[i].cols.get(table, {})]
        frags: list[str] = []
        dates = {_date_key(parts[i].ctx, parts[i].ctx.writers.get(table, _Writers()), table,
                           parts[i].cols[table][col.name]) for i in with_col}
        if "date_year_from_period" in dates:
            frags.append(COLUMN_TEXT["date_year_from_period"])
        elif "date" in dates or (not with_col and col.role == "axis"):
            frags.append(COLUMN_TEXT["date"])
        if target_cols is not None and col.name in target_cols:
            unit = target_cols[col.name].unit
        elif with_col:
            unit = parts[with_col[-1]].cols[table][col.name].unit
        else:
            unit = col.unit
        if unit:
            frags.append(COLUMN_TEXT["unit"].format(unit=_tok("单位", unit)))
        meanings: set[str] = set()
        blank = False
        none_in_union = counts_total is not None and counts_total.get(col.name, 1) == 0
        if table not in union_kinds and not none_in_union:
            for i in with_col:
                part = parts[i]
                w = part.ctx.writers.get(table, _Writers())
                if not _null_eligible(part.ctx, w, table, part.cols[table][col.name]):
                    continue
                counts = part_null_counts[i].get(table) if part_null_counts is not None else None
                if counts is not None and counts.get(col.name, 0) <= 0:
                    continue
                m, b = _null_sources(w, part.src.extraction)
                meanings |= m
                blank |= b
        if (text := _null_text(meanings, blank)) is not None:
            frags.append(text)
        if not with_col:
            frags.append(COLUMN_TEXT["col_absent"])
        elif (table, col.name) in retired:
            frags.append(COLUMN_TEXT["col_retired"])
        elif len(parts) > 1 and len(with_col) < len(having):
            frags.append(COLUMN_TEXT["col_partial_periods"])
        if frags:
            out[col.name] = "。".join(frags)
    return out


def build_union_notes(target: Recipe, parts: list[UnionNotePart], *, union_tables: dict[str, list[ColumnOut]],
                      null_counts: dict[str, dict[str, int]] | None,
                      part_null_counts: list[dict[str, dict[str, int]]] | None,
                      added: list[dict], retired: list[dict], label_sets: list[dict],
                      gaps: bool, dropped: bool = False) -> SchemaNotes:
    """按期累积物化的快照库的说明（P3-SPEC 2.8）。parts 按统计期排序，可以只有一期（物化的单期快照，7.4）。

    做法：逐期调 build_notes（该期的配方、Extraction、核对、接受、空值数），按 (片段族, subject) 合并、取最弱：
    - 部分期没有这条关系、这张合计表的结论：按「未能核对」写，绝不沿用「已核对」；
    - 任一期还没确认接受（试运行预览时本期按「全部未接受」生成）：不写「已由用户确认接受」；
    - 结构类的片段（粒度、时段写法、常量列、合计另存在哪、用户写的表说明）用目标配方那一期的，只在早期出现的
      用最后一个含它的那一期的。「目标那一期」= 配方与目标配方相同的最后一期；没有一期相同（移除、作废之后）
      时取最后一期。

    union_tables：并集的表结构（目标配方的表加退役的表和列），说明逐表、逐列按它写；某一期有、而它没有的表记
    problems（并集漏了这一期的数据）。null_counts / part_null_counts：materialize_union 的整体与按期空值数，
    part_null_counts 与 parts 同序。added / retired：并集新增、退役的表和列（UnionReport），退役以「目标配方没有、
    并集里有」推出，retired 里列出的一并按退役处理；added 只作交叉核对。label_sets：各期维度取值的差异（2.5）。
    gaps：各期之间有空缺。dropped：计划移出了此前各期（restart、模式切换），结果只能有一期。

    kinds 取各期配方的并集：任一期把一张表声明为原表写明的合计，并集里就标 reported_total（退役了也标）。
    每条模板都过 prose_problems（known = 并集的列、表、单位）；problems 非空就不能保存。
    """
    notes = SchemaNotes()
    problems = notes.problems
    if not parts:
        problems.append("没有任何一期的数据，无法生成按期累积的说明")
        return notes
    if dropped and len(parts) > 1:
        problems.append("计划移出了此前各期，但当前版本有多期，说明无法确定")
    if part_null_counts is not None and len(part_null_counts) != len(parts):
        problems.append("各期空值数的期数与各期不符，无法生成空值说明")
        part_null_counts = None
    built: list[_UPart] = []
    for i, src in enumerate(parts):
        pn = build_notes(src.recipe, src.extraction, src.checks, src.acceptances, null_counts=src.null_counts)
        ctx = _make_ctx(src.recipe, src.extraction, src.checks, src.acceptances)
        part = _UPart(index=i, src=src, ctx=ctx, notes=pn,
                      cols={t: {c.name: c for c in cs} for t, cs in ctx.tables.items()})
        problems.extend(f"{part.label()}：{p}" for p in pn.problems)
        built.append(part)
    target_sig = _canonical(target)
    ref = max((i for i, p in enumerate(built) if _canonical(p.src.recipe) == target_sig), default=len(built) - 1)
    target_tables, _ = derive_tables(target)
    retired_cols = {(t, c.name) for t, cs in union_tables.items() if t in target_tables
                    for c in cs if c.name not in {x.name for x in target_tables[t]}}
    retired_cols |= {(str(r.get("table")), str(r.get("column"))) for r in retired or []
                     if isinstance(r, dict) and r.get("column")}
    for entry in added or []:
        if isinstance(entry, dict) and entry.get("table") not in union_tables:
            problems.append(f"新增的表「{entry.get('table')}」不在当前版本的表结构中")
    for part in built:
        for t in part.cols:
            if t not in union_tables:
                problems.append(f"表「{t}」在{part.label()}有数据，但不在当前版本的表结构中")
    kinds = {t.name: t.kind for p in [*(b.src.recipe for b in built), target] for t in p.tables
             if t.kind == "reported_total"}
    notes.kinds = {t: k for t, k in kinds.items() if t in union_tables}
    for t in target_tables:
        if t not in union_tables:
            problems.append(f"目标配方的表「{t}」不在当前版本的表结构中")
    # 引用只认并集库里真有的表和列：某一期的说明提到了并集里没有的列，就是并集漏了东西，不能保存
    known = {"列": {c.name for cols in union_tables.values() for c in cols}, "表": set(union_tables),
             "单位": set(UNITS)}
    for table, columns in union_tables.items():
        having = [i for i, p in enumerate(built) if table in p.cols]
        frags = _union_table_fragments(table, having, built, ref, target, target_tables, label_sets, gaps, problems)
        target_cols = {c.name: c for c in target_tables[table]} if table in target_tables else None
        cols = _union_column_templates(table, list(columns), having, built, target_cols, retired_cols, notes.kinds,
                                       null_counts, part_null_counts)
        _check_prose(notes, table, _join(frags), cols, known)
        _store(notes, table, frags, cols)
    return notes


def add_single_after_drop(notes: SchemaNotes) -> SchemaNotes:
    """累积模式下 restart 的结果：快照库就是本期的构建库（build_notes 生成说明），但此前各期已移出当前版本
    （计划的 dropped 非空）。给每张表加 single_after_drop，免得模型以为当前版本就是全部历史（评审一-m10）。
    返回新的 SchemaNotes，不改入参。"""
    out = copy.deepcopy(notes)
    text = NOTE_TEXT["single_after_drop"]
    for table, note in out.tables.items():
        frags = list(out.fragments.get(table, []))
        if any(f.key == "single_after_drop" for f in frags):
            continue
        out.fragments[table] = [*frags, NoteFragment("single_after_drop", table, text)]
        template = out.templates.get(table, "")
        template = f"{_clean(template)}。{text}" if _clean(template) else text
        out.templates[table] = template
        note.comment = render_prose(template)
    return out
