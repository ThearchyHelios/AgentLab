"""确认项（期 2，WP-5b；P2-SPEC 7.5）：试运行之后、提交之前必须逐项勾选的清单。

**为什么要有确认项。** 配方语言是封闭的，但仍有不少取值是「放宽」：占位符存空值、千分位文本按数字、公式按
保存值、跳过空行、忽略多出的列、不核对文件名里的日期……每一项都可能让本该拒收的文件被导入。原则（评审甲-5）：
配方里每个放宽取值，**只要配置了**就出确认项，计数类写上本期个数；配方与现行的不同时（首次、切换、改配方后
的第一次提交，或配方哈希变了）列出全部配方类；每期都要重新看的（含数字的区域外文字、空行之后的文字、
统计期人工录入、工作表改名……）每次都按本期情况算；差异卡里需确认的项原样收进来。

提交时服务端按存下的试运行重算一遍必勾集合（同一个函数），缺一项就 422 confirm_required；界面不提供全选。
id 里的键一律按契约「键的格式」：分段、块用 id，工作表用实际工作表名（sheet_renamed 用配方里的工作表 id），
格子用「工作表!A1」。

纯函数，不碰数据库。输入的 Extraction、核对结果、差异卡收 dataclass，也收 JSON 形状的 dict（暂存区里存的
就是 asdict 之后的样子）；配方收 Recipe，也收 dict（canonical 形式也行，按模型补全默认值）。

**输入不全就报错，不按空处理**：少一个键、少一个参数，对应的必勾项就会静默消失（extraction 没有 problems，
rows_after_stop 就没了；上传新一期漏传现行配方，破坏性变更和单位变化就没了），而提交时重算用的是同一个
函数，服务端也发现不了。所以 kind 只认四种；reupload、redraft 必须给现行配方和上一期回执；extraction
必须带齐 EXTRACTION_KEYS、不能是部分干跑；checks 必须给；有上一期时差异卡必须给（可以是空列表）。

期 3（P3-SPEC 6.2、9.5）：
- **只列有变化的配方类确认项**：首次、切换、采用「按规则重新起草」（recipe_origin=rules_redraft）仍列全部；其余
  上传新一期、修改配方只列 recipe_item_signatures 与现行配方不同的项。没变的项在现行配方提交时已经由人确认过
  （TableRecipe.confirmations），重复勾选只会让人习惯性地全勾。例外：按本期数据才出、或内容随数据变的几项
  （忽略的列、列表的公式按保存值、年份取自统计期、隐藏行列、忽略规则命中的行），实际内容与上一期回执不同也照列
  （_data_signatures）：配方变了时差异卡不再逐项比较，只看签名的话，上次没出、从没人确认过的这几项就漏了；
- 新确认项：修改记录（fix:*、select:*）、redraft_adopted、按期累积的计划（period_replace、accumulate_restart、
  retire、mode_switch、accumulate_unit_risk）、忽略规则（ignore_rows、ignore_outside）。ConfirmItem.source 标出
  来源（recipe / edit / accumulate / outside / sheet / context / diff / switch），界面按它分组。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.data.names import canon, to_sql_name
from app.data.recipe_diff import (
    ReceiptIncomplete, col_letter, outside_of, period_annotated, period_of, period_texts, plain, receipt_of,
    require_receipt, rows_text, same_period_sentence, short, split_coord,
)
from app.data.recipe_parsers import UNITS, match_key, period_residue, split_unit_suffix
from app.data.recipe_types import (
    ConfirmItem, CrosstabBlock, DerivedSegment, Dismissed, ListBlock, MeasuresSegment, NotComparable, Recipe,
    SheetRecipe, SumEq, derive_tables,
)

_AXIS_CHECK = {"contiguous": "逐日连续", "covers_context": "恰好覆盖统计期"}
_TABLE_KIND = {"data": "明细数据", "reported_total": "原表合计"}
#: 统计期上下文的 id（契约 ContextSpec.id 只能是它）。确认项 id「context_human:统计期」由它拼出来
_PERIOD_ID = "统计期"
#: ConfirmContext.kind 的取值（P2-SPEC 7.2、7.3）
KINDS: tuple[str, ...] = ("first", "reupload", "redraft", "switch")
#: confirm_items 读到的 Extraction 字段：缺一个，对应的确认项就会静默消失或写错个数
EXTRACTION_KEYS: tuple[str, ...] = (
    "problems", "outside_text", "ignored_columns", "sheets", "period", "placeholders", "formula_cells_accepted",
    "blank_rows_skipped", "labels", "derived", "axes", "hidden", "lineage",
)
#: 有上一期时，本期类确认项读上一期回执里的这几个键（含数字文字的免确认、新增工作表）
_PREV_KEYS: tuple[str, ...] = ("outside_text", "sheets")


@dataclass
class ConfirmContext:
    """confirm_items 的输入（字段顺序同 P2-SPEC 7.5）。"""

    #: first / reupload / redraft / switch
    kind: str
    #: 本次要提交的工作配方
    recipe: Recipe | dict[str, Any]
    #: 现行配方；首次、切换时为 None，reupload、redraft 必须给（漏传就报错：否则破坏性变更、单位变化会静默消失）
    old_recipe: Recipe | dict[str, Any] | None = None
    #: 切换时旧快照（简单导入）的 schema_cache，用来列出将消失或改变的表和列
    old_schema_cache: dict[str, Any] | None = None
    #: 本次试运行的 Extraction（或它的 asdict），必须带齐 EXTRACTION_KEYS；默认值只为保持字段顺序，不传会报错
    extraction: Any = None
    #: 本次的核对结果（CheckResult 或 dict）；必须给（没有核对也传空列表）
    checks: list[Any] | None = None
    #: 本次的差异卡（DiffItem 或 dict）；有上一期时必须给（没有差异也传空列表）
    diff: list[Any] | None = None
    #: 上一期的导入回执（diff_reports 的 prev 形状）；首次为 None，reupload、redraft 必须给
    prev: dict[str, Any] | None = None
    #: rules / ai / manual / mixed。AI 起草后又人手改过的，调用方要传 mixed（暂存区里此时记的是 manual）
    recipe_origin: str = "rules"
    #: 系统发现（DraftFacts 或 dict），只用来给 dismissed 的标题写上那条关系的原话；不给也行
    facts: Any = None
    #: 数据源现在设的遮罩列（options.mask_columns）。切换、改配方之后列名变了，遮罩按列名匹配会静默失效（AU-6）
    mask_columns: list[str] | None = None
    #: 期 3：累积计划（AccumulatePlan 或它的 asdict）。新配方 mode=accumulate、或者现行是累积时由试运行给出；
    #: period_replace、accumulate_restart、retire、mode_switch、accumulate_unit_risk 由它出
    accumulate: Any = None
    #: 期 3：暂存区里已应用的修改（StagingOut.edits 的形状），每条出 fix:<key> 或 select:<key>。调用方只传未被
    #: 覆盖的；superseded 为真的这里也会跳过（PUT 之后它们的效果可能已经不在新配方里）
    edits: list[dict[str, Any]] | None = None
    #: 期 3：recipe_origin=rules_redraft 时 compare_recipes(现行, 采用的配方) 的结果，redraft_adopted 据此逐条列出
    #: 变化；不给时按 old_recipe 现算
    redraft_compare: dict[str, Any] | None = None
    #: 期 3：区域外文字的差异项（accumulate_unit_risk 用）；不给时从 diff 里取 outside_* 和统计期写法的项。
    #: diff_reports 收到追加或替换该期的累积计划时，配方变了也比区域外文字，所以通常不用另给；另算时用
    #: recipe_diff.outside_items(上一期, 本期)，不能传空列表顶替（传了就只看它，单位变化会静默消失）
    outside_diff: list[Any] | None = None


@dataclass(frozen=True)
class TableChange:
    """新旧配方之间一张表的一处破坏性变化。kind：table_removed / column_removed / type / unit / grain /
    table_kind / placeholder_meaning / const_value / source / store。"""

    table: str
    kind: str
    message: str
    column: str | None = None
    old: Any = None
    new: Any = None


def as_recipe(data: Recipe | dict[str, Any]) -> Recipe:
    return data if isinstance(data, Recipe) else Recipe.model_validate(data)


def recipe_canonical(recipe: Recipe) -> dict[str, Any]:
    """与 recipe.canonical_recipe 同一种写法（去掉等于默认值的字段）：两份配方这个相等，哈希就相等。"""
    return recipe.model_dump(mode="json", exclude_defaults=True)


def recipe_differs(kind: str, recipe: Recipe | dict[str, Any], old: Recipe | dict[str, Any] | None) -> bool:
    """「配方与现行的不同」：首次、切换，或者与现行配方的 canonical 形式不同。"""
    if kind in ("first", "switch") or old is None:
        return True
    return recipe_canonical(as_recipe(recipe)) != recipe_canonical(as_recipe(old))


# --------------------------------------------------------------------------
# 破坏性变更
# --------------------------------------------------------------------------


def _declared_types(recipe: Recipe, tables: dict[str, list[Any]]) -> dict[tuple[str, str], str]:
    """(表, 列) → 配方声明的类型。DATE 存成 TEXT，只看 derive_tables 的 SQL 类型就分不出 TEXT ↔ DATE。"""
    out = {(t, c.name): ("DATE" if c.role == "axis" else c.type) for t, cols in tables.items() for c in cols}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                for col in block.columns:
                    out[(block.table, col.name)] = col.type
    return out


def _placeholder_meanings(recipe: Recipe) -> dict[str, dict[str, str]]:
    """表 → {占位符（canon）: 含义}。占位符按块配置，作用于这个块写入的所有表。"""
    out: dict[str, dict[str, str]] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            meanings = {canon(p.text): p.meaning for p in block.values.placeholders}
            if not meanings:
                continue
            if isinstance(block, CrosstabBlock):
                tables = [s.table for s in block.segments if not isinstance(s, DerivedSegment)]
                tables += [s.keep_as.table for s in block.segments if isinstance(s, DerivedSegment) and s.keep_as]
            else:
                tables = [block.table]
                if block.rows.total_row is not None and block.rows.total_row.keep_as:
                    tables.append(block.rows.total_row.keep_as)
            for t in tables:
                for text, meaning in meanings.items():
                    out.setdefault(t, {}).setdefault(text, meaning)
    return out


def _unit_name(unit: str | None) -> str:
    return unit or "未标注"


def _const_values(recipe: Recipe) -> dict[tuple[str, str, str], str]:
    """(表, 分段 id, 常量列) → pick。常量列的值整列取自 pick：pick 换了，这一列已有的值就换了。"""
    out: dict[tuple[str, str, str], str] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if not isinstance(block, CrosstabBlock):
                continue
            for seg in block.segments:
                for col, const in (getattr(seg, "const", None) or {}).items():
                    out[(seg.table, seg.id, col)] = const.pick
    return out


def _const_sets(recipe: Recipe) -> dict[tuple[str, str], set[str]]:
    """(表, 常量列) → 所有分段的 pick。不按分段 id 配对：id 和 pick 一起改（把新文件的规则草稿粘进来时，
    规则起草把分段 id 设成 pick 本身）时按 id 配不上对，取值换了也比不到（AU-2）。"""
    out: dict[tuple[str, str], set[str]] = {}
    for (table, _seg, col), pick in _const_values(recipe).items():
        out.setdefault((table, col), set()).add(pick)
    return out


def _column_sources(recipe: Recipe) -> dict[tuple[str, str], str]:
    """(表, 列) → 这一列取自原表的哪个标签（交叉表 measures）或哪个表头（列表）。"""
    out: dict[tuple[str, str], str] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, MeasuresSegment):
                        for label in seg.labels.expect:
                            col = seg.measures.get(label)
                            if col:
                                out[(seg.table, col)] = label
            else:
                for c in block.columns:
                    out[(block.table, c.name)] = c.header
    return out


def _text_stores(recipe: Recipe) -> dict[tuple[str, str], tuple[str, bool]]:
    """(表, 列表的 TEXT 列) → (实际生效的存法, 是否显式写了)。缺省时主键列 canonical、其余 raw。"""
    grains = {t.name: set(t.grain) for t in recipe.tables}
    out: dict[tuple[str, str], tuple[str, bool]] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                for c in block.columns:
                    if c.type == "TEXT":
                        eff = c.store or ("canonical" if c.name in grains.get(block.table, set()) else "raw")
                        out[(block.table, c.name)] = (eff, c.store is not None)
    return out


_STORE_NAME = {"canonical": "规范写法", "raw": "原文"}


def table_changes(old_recipe: Recipe | dict[str, Any], new_recipe: Recipe | dict[str, Any]) -> list[TableChange]:
    """新旧配方逐表比较（P2-SPEC 7.5）。新增表、新增列不算；比较范围：表删除或改名、列删除或改名、
    类型（含 TEXT ↔ DATE 的写法）、单位、grain、kind、值列占位符的含义、常量列的取值，以及每列取自原表的
    哪个标签或表头、列表文字列的存法（列名类型都没变、内容却整列换了的几种，AU-2）。

    期 2 是替换模式，这些变化会让已启用的工作流和报告的口径静默改变；期 3 按期累积时，同一列前后各期单位
    不一致是灾难性的。表改名和删除分不开（配方里没有改名记录），一律报「删除或改名」。
    """
    old, new = as_recipe(old_recipe), as_recipe(new_recipe)
    ot, _ = derive_tables(old)
    nt, _ = derive_tables(new)
    ospec = {t.name: t for t in old.tables}
    nspec = {t.name: t for t in new.tables}
    otypes, ntypes = _declared_types(old, ot), _declared_types(new, nt)
    oph, nph = _placeholder_meanings(old), _placeholder_meanings(new)
    oconst, nconst = _const_values(old), _const_values(new)
    osets, nsets = _const_sets(old), _const_sets(new)
    osrc, nsrc = _column_sources(old), _column_sources(new)
    ostore, nstore = _text_stores(old), _text_stores(new)
    out: list[TableChange] = []
    for t, ocols in ot.items():
        if t not in nt:
            out.append(TableChange(t, "table_removed", f"表「{t}」不再产出（删除或改名）"))
            continue
        ncols = {c.name: c for c in nt[t]}
        for oc in ocols:
            nc = ncols.get(oc.name)
            if nc is None:
                out.append(TableChange(t, "column_removed", f"删除或改名了列「{oc.name}」", column=oc.name))
                continue
            ta, tb = otypes.get((t, oc.name), oc.type), ntypes.get((t, nc.name), nc.type)
            if ta != tb:
                if {ta, tb} == {"TEXT", "DATE"}:
                    msg = f"列「{oc.name}」的写法 {ta} → {tb}（日期存为 YYYY-MM-DD 文本）"
                else:
                    msg = f"列「{oc.name}」类型 {ta} → {tb}"
                out.append(TableChange(t, "type", msg, column=oc.name, old=ta, new=tb))
            if (oc.unit or None) != (nc.unit or None):
                out.append(TableChange(t, "unit", f"列「{oc.name}」的单位 {_unit_name(oc.unit)} → {_unit_name(nc.unit)}",
                                       column=oc.name, old=oc.unit, new=nc.unit))
            # 列名、类型、单位都没变，取自原表的标签或表头换了（两列对调、改成另一行）：这一列的内容整列换了（AU-2）
            sa, sb = osrc.get((t, oc.name)), nsrc.get((t, oc.name))
            if sa is not None and sb is not None and match_key(sa) != match_key(sb):
                out.append(TableChange(t, "source", f"列「{oc.name}」的来源「{sa}」→「{sb}」",
                                       column=oc.name, old=sa, new=sb))
            # 文字的存法（规范写法 / 原文）换了：按这一列分组、筛选的结果会变。两边都是缺省、只因主键变了而变的，
            # 已由 grain 项报出
            ea, eb = ostore.get((t, oc.name)), nstore.get((t, oc.name))
            if ea is not None and eb is not None and ea[0] != eb[0] and (ea[1] or eb[1]):
                out.append(TableChange(t, "store", f"列「{oc.name}」的文字存法 {_STORE_NAME[ea[0]]} → {_STORE_NAME[eb[0]]}",
                                       column=oc.name, old=ea[0], new=eb[0]))
        os_, ns_ = ospec.get(t), nspec.get(t)
        if os_ is not None and ns_ is not None:
            if set(os_.grain) != set(ns_.grain):
                out.append(TableChange(t, "grain", f"主键 {'、'.join(os_.grain) or '无'} → {'、'.join(ns_.grain) or '无'}",
                                       old=list(os_.grain), new=list(ns_.grain)))
            if os_.kind != ns_.kind:
                out.append(TableChange(t, "table_kind",
                                       f"表的种类 {_TABLE_KIND[os_.kind]} → {_TABLE_KIND[ns_.kind]}",
                                       old=os_.kind, new=ns_.kind))
        pa, pb = oph.get(t, {}), nph.get(t, {})
        for text in pa:
            if text in pb and pa[text] != pb[text]:
                out.append(TableChange(t, "placeholder_meaning", f"值列的占位符「{text}」含义 {pa[text]} → {pb[text]}",
                                       old=pa[text], new=pb[text]))
        # 常量列的取值换了（分段标题改了写法，常量只能从新标题的候选词里另选，D09）：列名、类型都没变，
        # 但这一列的值整列变了，按它筛选、分组的查询和报告会静默变口径
        reported: set[str] = set()
        for (table, seg, col), pick in oconst.items():
            new_pick = nconst.get((table, seg, col))
            if table == t and new_pick is not None and new_pick != pick:
                out.append(TableChange(t, "const_value", f"分段「{seg}」的列「{col}」取值「{pick}」→「{new_pick}」",
                                       column=col, old=pick, new=new_pick))
                reported.add(col)
        # 按 (表, 列) 比全部取值：分段 id 跟着 pick 一起改时上面配不上对（AU-2）
        for (table, col), picks in osets.items():
            new_picks = nsets.get((table, col))
            if table == t and col not in reported and new_picks is not None and new_picks != picks:
                out.append(TableChange(t, "const_value",
                                       f"列「{col}」的取值「{'、'.join(sorted(picks))}」→「{'、'.join(sorted(new_picks))}」",
                                       column=col, old=sorted(picks), new=sorted(new_picks)))
    return out


def breaking_changes(old_recipe: Recipe | dict[str, Any], new_recipe: Recipe | dict[str, Any]) -> dict[str, list[str]]:
    """表名 → 破坏性变化的说明（含单位变化；确认项里单位另出 unit_changed:*，不在 breaking:* 里重复）。"""
    out: dict[str, list[str]] = {}
    for ch in table_changes(old_recipe, new_recipe):
        out.setdefault(ch.table, []).append(ch.message)
    return out


def _affinity(sql_type: str) -> str:
    """手工源、期 1 上传源的列类型写法 → INTEGER / REAL / TEXT（SQLite 的类型亲和规则，够比较用）。"""
    t = sql_type.upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return t


def switch_changes(old_schema_cache: dict[str, Any] | None, new_recipe: Recipe | dict[str, Any]) -> list[str]:
    """从简单导入切换为按配方导入时，旧快照里哪些表和列会消失或改变（逐表一句）。"""
    nt, _ = derive_tables(as_recipe(new_recipe))
    lines = []
    for name, meta in ((old_schema_cache or {}).get("tables") or {}).items():
        if name not in nt:
            lines.append(f"表「{name}」将消失")
            continue
        ncols = {c.name: c for c in nt[name]}
        parts = []
        for col in (meta or {}).get("columns") or []:
            cname = str(col.get("name"))
            otype = str(col.get("type") or "")
            if cname not in ncols:
                parts.append(f"列「{cname}」将消失")
            elif otype and _affinity(otype) != ncols[cname].type:
                parts.append(f"列「{cname}」类型 {_affinity(otype)} → {ncols[cname].type}")
        if parts:
            lines.append(f"表「{name}」：" + "、".join(parts))
    return lines


# --------------------------------------------------------------------------
# 确认项
# --------------------------------------------------------------------------


class _Bag:
    """按出现顺序收集，id 重复的只留第一条。keep 不为 None 时只收 keep(id) 为真的（只列有变化的配方类项）。"""

    def __init__(self, keep: Callable[[str], bool] | None = None) -> None:
        self.items: list[ConfirmItem] = []
        self._ids: set[str] = set()
        self.keep = keep

    def add(self, item_id: str, label: str, detail: str = "", source: str = "recipe") -> None:
        if item_id in self._ids or (self.keep is not None and not self.keep(item_id)):
            return
        self._ids.add(item_id)
        self.items.append(ConfirmItem(id=item_id, label=label, detail=detail, required=True, source=source))

    def into(self, other: "_Bag") -> None:
        for i in self.items:
            other.add(i.id, i.label, i.detail, i.source)


def _validate(ctx: ConfirmContext) -> dict[str, Any]:
    """输入不全就报错（见模块 docstring），返回 extraction 的 dict 形状。"""
    if ctx.kind not in KINDS:
        raise ValueError(f"未知的导入方式：{ctx.kind}")
    if ctx.kind in ("reupload", "redraft"):
        if ctx.old_recipe is None:
            raise ValueError(f"{ctx.kind} 必须给现行配方，否则破坏性变更和单位变化无从比较")
        if ctx.prev is None:
            raise ValueError(f"{ctx.kind} 必须给上一期的导入回执")
    if ctx.checks is None:
        raise ValueError("必须给本次的核对结果（没有核对时传空列表）")
    if ctx.prev is not None and ctx.diff is None:
        raise ValueError("有上一期时必须给差异卡（没有差异时传空列表）")
    ex = plain(ctx.extraction)
    if not isinstance(ex, dict):
        raise ReceiptIncomplete("extraction", ["extraction"])
    missing = [k for k in EXTRACTION_KEYS if k not in ex]
    if ex.get("partial"):
        # 部分干跑只看了前若干行，区域外文字、空行之后的文字都不全，不能拿来定必勾集合
        missing.append("partial")
    if missing:
        raise ReceiptIncomplete("extraction", missing)
    prev = plain(ctx.prev) if ctx.prev is not None else None
    if prev is not None and "ledger" in receipt_of(prev):
        require_receipt(prev, "prev", _PREV_KEYS, top=())
    return ex


def confirm_items(ctx: ConfirmContext) -> list[ConfirmItem]:
    """生成必勾的确认项（P2-SPEC 7.5、P3-SPEC 6.2、9.5）。顺序：单位变化（排最前）、其余破坏性变更、切换、
    按期累积（重新开始、切换模式、退役、替换该期、单位风险）、采用重新起草、配方类、修改记录、AI 类、本期类、差异卡。

    配方类：首次、切换、采用按规则重新起草时列全部；其余只列签名与现行配方不同的（recipe_item_signatures）。
    输入不全时抛 ValueError（缺键时是它的子类 ReceiptIncomplete），见模块 docstring。
    """
    ex = _validate(ctx)
    recipe = as_recipe(ctx.recipe)
    old = as_recipe(ctx.old_recipe) if ctx.old_recipe is not None else None
    plan = _plan(ctx.accumulate)
    bag = _Bag()
    differs = recipe_differs(ctx.kind, recipe, old)

    if differs and old is not None and ctx.kind != "switch":
        changes = table_changes(old, recipe)
        for ch in changes:
            if ch.kind == "unit":
                bag.add(f"unit_changed:{ch.table}.{ch.column}",
                        f"表「{ch.table}」列「{ch.column}」的单位 {_unit_name(ch.old)} → {_unit_name(ch.new)}："
                        "数量级或含义可能不同，引用它的报告口径会变")
        retired_tables, retired_existing = _retired_scope(plan)
        grouped: dict[str, list[str]] = {}
        for ch in changes:
            if ch.kind == "unit":
                continue
            # 同一张表已有 retire:*（按期累积时退役的列和表保留在并集里），删除或改名就不在 breaking 里再列一遍；
            # 早已退役的同样不列（P3-SPEC 2.5）
            if ch.kind in ("column_removed", "table_removed") and (
                    ch.table in retired_tables or (ch.table, ch.column) in retired_existing):
                continue
            grouped.setdefault(ch.table, []).append(ch.message)
        for table, msgs in grouped.items():
            bag.add(f"breaking:{table}", f"表「{table}」：" + "；".join(msgs))

    if ctx.kind == "switch":
        if ctx.old_schema_cache is None:
            tail = "原有的表结构未知，切换后以本次导入的表为准"
        else:
            lines = switch_changes(ctx.old_schema_cache, recipe)
            tail = ("以下表和列将消失或改变：" + "；".join(lines)) if lines else "原有的表和列都保留"
        bag.add("switch_from_simple", "从简单导入切换为按配方导入：" + tail, source="switch")

    if plan is not None:
        _accumulate_items(bag, plan, ctx, recipe, old)
    if ctx.recipe_origin == "rules_redraft":
        _redraft_item(bag, ctx, recipe, old)

    if differs and ctx.mask_columns:
        _mask_items(bag, ctx, recipe, old)

    if differs:
        # 首次、切换、采用重新起草：列全部（重新起草的分段定位、标签集合、工作表匹配名不在签名里，任何一项都
        # 兜不住，评审一-M6）。其余只列新增的、签名变了的
        full = ctx.kind in ("first", "switch") or ctx.recipe_origin == "rules_redraft" or old is None
        sub = _Bag(None if full else _changed_items(recipe, old, ex, ctx.prev))
        _recipe_items(sub, recipe, ex, ctx.facts, ai=ctx.recipe_origin in ("ai", "mixed"))
        sub.into(bag)

    _edit_items(bag, ctx.edits)

    if differs and ctx.recipe_origin in ("ai", "mixed"):
        _ai_items(bag, recipe)

    _period_items(bag, recipe, ex, ctx)

    for d in ctx.diff or []:
        d = plain(d)
        if isinstance(d, dict) and d.get("requires_confirm") and d.get("confirm_id"):
            bag.add(str(d["confirm_id"]), str(d.get("label") or ""), str(d.get("detail") or ""), source="diff")
    return bag.items


# --------------------------------------------------------------------------
# 期 3：只列有变化的配方类确认项（P3-SPEC 6.2）
# --------------------------------------------------------------------------


def _sig(*parts: Any) -> str:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)


def _signatures(recipe: Recipe, sheet_name: Callable[[SheetRecipe], str]) -> dict[str, str]:
    """确认项 id → 签名（只看配方内容，不看本期计数）。按工作表出的 id 用 sheet_name(工作表) 拼：比较时两份配方
    按工作表 id 换成同一个名字（本期实际的工作表名），所以只改了匹配名（⑨ 更新工作表名）不算变化。"""
    out: dict[str, str] = {}
    for sheet in recipe.sheets:
        name = sheet_name(sheet)
        if sheet.context:
            out[f"cross_check_off:{name}"] = _sig(sheet.context[0].cross_check)
        out[f"hidden:{name}"] = _sig(sheet.hidden.rows, sheet.hidden.cols)
        out[f"ignore_outside:{name}"] = _sig(sorted((match_key(r.anchor), r.reason) for r in sheet.ignore_outside))
        for block in sheet.blocks:
            for p in block.values.placeholders:
                out.setdefault(f"placeholder:{p.text}", _sig(p.meaning))
            out[f"text_number:{block.id}"] = _sig(block.values.text_number)
            out[f"formula_cached:{block.id}"] = _sig(block.values.formula)
            ignore_cols = sorted((match_key(r.header), r.reason) for r in block.ignore_columns)
            if isinstance(block, CrosstabBlock):
                out[f"blank_null:{block.id}"] = _sig(block.values.blank)
                out[f"checks_relaxed:{block.id}"] = _sig(sorted(k for k in _AXIS_CHECK if k not in block.axis.checks))
                out[f"year_from:{block.id}"] = _sig(block.axis.year_from)
                out[f"ignored_columns:{block.id}"] = _sig(None, ignore_cols)
                out[f"ignore_rows:{block.id}"] = _sig(sorted((match_key(r.label), r.reason) for r in block.ignore_rows))
                for seg in block.segments:
                    if isinstance(seg, DerivedSegment):
                        keep = seg.keep_as.table if seg.keep_as else None
                        out[f"derived:{seg.id}"] = _sig(keep, sorted(match_key(x) for x in seg.labels.expect))
            else:
                out[f"merged_fill:{block.id}"] = _sig(block.merged_data)
                out[f"ignored_columns:{block.id}"] = _sig(block.extra_columns, ignore_cols)
                out[f"blank_skip:{block.id}"] = _sig(block.rows.blank_rows)
                total = block.rows.total_row
                out[f"total_row:{block.id}"] = _sig(
                    None if total is None else (total.label_column, total.pick, total.keep_as))
    for r in recipe.relations:
        if isinstance(r, SumEq):
            out[f"relation:{r.id}"] = _sig("sum_eq", r.table, r.total, sorted(r.parts))
        elif isinstance(r, NotComparable):
            out[f"relation:{r.id}"] = _sig("not_comparable", r.a.table, r.a.value, r.b.table, r.b.value, r.by)
        elif isinstance(r, Dismissed):
            out[f"dismissed:{r.claims}"] = _sig(r.reason)
    tables, _ = derive_tables(recipe)
    for t, cols in tables.items():
        for c in cols:
            out[f"unit:{t}.{c.name}"] = _sig(c.unit, c.header)
    out["mode"] = _sig(recipe.mode)
    return out


def recipe_item_signatures(recipe: Recipe | dict[str, Any]) -> dict[str, str]:
    """配方类确认项 id → 签名（P3-SPEC 6.2 的表）。改配方之后只列「新增的、签名变了的」配方类项。

    签名只看配方内容：占位符的含义、空格与千分位与公式的处理、合并单元格、缺的日期断言、文件名核对、
    忽略的列（多出的列怎么处理加按表头忽略的表头和理由）、跳过空行、合计段与合计行、年份来源、关系、不登记的理由、
    单位与来源标签、隐藏行列的策略、导入模式、按行标签忽略和按同一行文字忽略的锚点加理由。
    分段定位（标题、标签集合、after_title）、工作表匹配名、列的来源标签不在签名里：来自修复按钮时有 fix:*，来自
    框选时有 select:*，来自手工修改时有破坏性变更和对照界面，来自按规则重新起草时按首次导入列全部。

    按工作表出的 id（cross_check_off、hidden、ignore_outside）这里用配方里的工作表名拼；confirm_items 比较时
    两份配方按工作表 id 换成本期实际的工作表名。

    签名只是一半：confirm_items 另按本期数据和上一期回执比较几项按数据出的确认项（_data_signatures，不在这个公开
    函数里，因为它要读回执）。
    """
    return _signatures(as_recipe(recipe), lambda s: s.match.name)


def _changed_items(recipe: Recipe, old: Recipe, ex: dict[str, Any],
                   prev: dict[str, Any] | None) -> Callable[[str], bool]:
    """「这个配方类确认项要不要列出」：新配方里新增的 id，签名与现行配方不同的，或者按本期数据出的内容与上一期
    不同的（_data_signatures）。没有签名定义的 id 照列（宁可多勾一项，不能漏）。工作表按 id 换成本期实际的名字，
    两份配方用同一个名字比。"""
    matched = (ex.get("sheets") or {}).get("matched") or {}

    def name(sheet: SheetRecipe) -> str:
        return str(matched.get(sheet.id) or sheet.match.name)

    new, old_sig = _signatures(recipe, name), _signatures(old, name)
    p_rec = receipt_of(prev) if prev is not None else {}
    now, before = _data_signatures(recipe, ex), _data_signatures(recipe, p_rec)

    def keep(item_id: str) -> bool:
        if item_id not in new or old_sig.get(item_id) != new[item_id]:
            return True
        return item_id in now and before.get(item_id) != now[item_id]

    return keep


def _data_signatures(recipe: Recipe, rec: dict[str, Any]) -> dict[str, str | None]:
    """按数据出的那几种配方类确认项 → 本期（或上一期回执）的实际内容（P3-SPEC 6.2 的补充，评审意见 H3）。

    为什么要有：签名只看配方内容，但下面几项在期 2 里只在本期数据满足条件时才出，或者内容随数据变：
    - ignored_columns:<块>：实际被忽略的表头集合（extra_columns=ignore 时本期没有多出的列就不出）；
    - formula_cached:<列表块>：本期有没有按保存值导入的公式格（列表的默认取值就是放宽，有公式格才出）；
    - year_from:<块>：日期表头的写法集合（表头都是完整日期时不出）；
    - hidden:<工作表>：照常导入或排除的是哪些隐藏行列；
    - ignore_rows:<块> / ignore_outside:<工作表>：忽略规则命中的锚点和行数。
    配方没变时这些变化由差异卡兜住（diff:ignored:、diff:axis_form:、diff:hidden:、diff:ignored_rows:），但配方变了时
    差异卡只给行数和统计期（recipe_diff 的模块 docstring）。只比签名的话，同一暂存区里应用过一次修复，上一次提交时
    没出、从未被人确认过的这几项就永远不用确认了。所以配方变了时，这几项的实际内容和上一期回执（ctx.prev，现行
    导入的导入清单）不同也照列。

    回执里缺对应字段（期 3 之前的导入没有 rows_excluded、手写的最小回执）时值为 None：一边 None、一边有值就算不同，
    照列；两边都缺则相同。工作表按 id 换成各自回执里的实际工作表名（改了名的照样对得上）。
    """
    out: dict[str, str | None] = {}
    matched = (rec.get("sheets") or {}).get("matched") or {}

    def name(sheet: SheetRecipe) -> str:
        return str(matched.get(sheet.id) or sheet.match.name)

    def has(field: str) -> bool:
        return field in rec

    ignored = rec.get("ignored_columns") or {}
    forms: dict[str, list[str]] = {}
    for a in rec.get("axes") or []:
        if isinstance(a, dict):
            forms.setdefault(str(a.get("block")), []).append(str(a.get("form") or ""))
    excluded = [e for e in (plain(x) for x in rec.get("rows_excluded") or []) if isinstance(e, dict)]

    def hits(reason: str, pred: Callable[[dict[str, Any]], bool]) -> str | None:
        if not has("rows_excluded"):
            return None
        rows: dict[str, int] = {}
        for e in excluded:
            if e.get("reason") == reason and pred(e):
                key = match_key(e.get("anchor") or "")
                rows[key] = rows.get(key, 0) + sum(int(b) - int(a) + 1 for a, b in e.get("rows") or [])
        return _sig(sorted(rows.items()))

    hidden = rec.get("hidden") or {}
    for sheet in recipe.sheets:
        sname = name(sheet)
        h = hidden.get(sname) if isinstance(hidden.get(sname), dict) else {}
        out[f"hidden:{sname}"] = _sig(sorted(int(r) for r in h.get("rows") or [] if str(r).isdigit()),
                                      sorted(str(c) for c in h.get("cols") or [])) if has("hidden") else None
        out[f"ignore_outside:{sname}"] = hits("ignored_outside", lambda e, s=sname: e.get("sheet") == s)
        for block in sheet.blocks:
            out[f"ignored_columns:{block.id}"] = (_sig(sorted({match_key(h) for h in ignored.get(block.id) or []}))
                                                  if has("ignored_columns") else None)
            if isinstance(block, CrosstabBlock):
                out[f"year_from:{block.id}"] = _sig(sorted(set(forms.get(block.id, [])))) if has("axes") else None
                out[f"ignore_rows:{block.id}"] = hits("ignored_rows", lambda e, b=block.id: e.get("block") == b)
            else:
                out[f"formula_cached:{block.id}"] = (_sig(int(rec.get("formula_cells_accepted") or 0) > 0)
                                                     if has("formula_cells_accepted") else None)
    return out


# --------------------------------------------------------------------------
# 期 3：修改记录、重新起草、按期累积（P3-SPEC 9.5）
# --------------------------------------------------------------------------


def _edit_items(bag: _Bag, edits: list[dict[str, Any]] | None) -> None:
    """每条已应用、未被覆盖的修改出一项：修复 fix:<目标键>，框选 select:<as>:<块或分段 id>。文案是修改的标题，
    细节是人话摘要和署名（未认证）。"""
    for e in edits or []:
        e = plain(e)
        if not isinstance(e, dict) or e.get("superseded") or not e.get("key"):
            continue
        prefix = "select" if e.get("kind") == "selection" else "fix"
        title = str(e.get("title") or "").strip() or ("按框选修改了配方" if prefix == "select" else "用修复按钮修改了配方")
        summary = e.get("summary")
        detail = "；".join(str(x) for x in summary) if isinstance(summary, list) else str(summary or "")
        if e.get("signed_by"):
            detail = "；".join(x for x in (detail, f"署名（未认证）：{e['signed_by']}") if x)
        bag.add(f"{prefix}:{e['key']}", title, detail, source="edit")


def _compare_lines(cmp: dict[str, Any]) -> list[str]:
    """compare_recipes 的结果里，分段、标签、工作表名、列来源的变化，逐条写成人话（redraft_adopted 用）。"""
    lines: list[str] = []
    for seg in cmp.get("segments") or []:
        sid, status = seg.get("id"), seg.get("status")
        title = seg.get("title") or {}
        labels = seg.get("labels") or {}
        if status in ("added", "removed"):
            # 新增、去掉的分段写出标题和全部标签：起草器换了分段 id 时，新分段的定位规则只在这里经人确认
            # （配方类确认项里没有分段的标题和标签）
            new = status == "added"
            head = title.get("new" if new else "old")
            tags = labels.get("added" if new else "removed") or []
            what = "".join([f"，标题「{head}」" if head else "",
                            "，标签" + "".join(f"「{x}」" for x in tags) if tags else ""])
            lines.append(f"{'新增' if new else '去掉'}分段「{sid}」{what}")
            continue
        if status == "changed" and title.get("old") != title.get("new"):
            lines.append(f"分段「{sid}」的标题「{title.get('old') or '无'}」→「{title.get('new') or '无'}」")
        if labels.get("added"):
            lines.append(f"分段「{sid}」加入标签" + "".join(f"「{x}」" for x in labels["added"]))
        if labels.get("removed"):
            lines.append(f"分段「{sid}」去掉标签" + "".join(f"「{x}」" for x in labels["removed"]))
        ignore = seg.get("ignore") or {}
        if ignore.get("added"):
            lines.append(f"「{sid}」新增忽略规则：" + "、".join(str(x) for x in ignore["added"]))
        if ignore.get("removed"):
            lines.append(f"「{sid}」去掉忽略规则：" + "、".join(str(x) for x in ignore["removed"]))
    for sh in cmp.get("sheets") or []:
        nm = sh.get("name") or {}
        if nm.get("old") != nm.get("new"):
            lines.append(f"工作表名「{nm.get('old') or '无'}」→「{nm.get('new') or '无'}」")
    for t in cmp.get("tables") or []:
        for c in t.get("columns") or []:
            o, n = c.get("old") or {}, c.get("new") or {}
            if o.get("source") != n.get("source") and (o.get("source") or n.get("source")):
                lines.append(f"表「{t.get('name')}」列「{c.get('name')}」的来源"
                             f"「{o.get('source') or '无'}」→「{n.get('source') or '无'}」")
    return lines


def _redraft_item(bag: _Bag, ctx: ConfirmContext, recipe: Recipe, old: Recipe | None) -> None:
    """采用了按规则重新起草的配方（P3-SPEC 6.3）：起草器识别出的定位规则要逐项经过人的确认，不能因为只列
    有变化的配方类项就直接成为现行配方（H3）。"""
    cmp = plain(ctx.redraft_compare)
    if not isinstance(cmp, dict):
        if old is None:
            raise ValueError("采用按规则重新起草的配方时必须给现行配方或对照结果，否则无法列出变化")
        from app.data.recipe_compare import compare_recipes  # 循环导入：recipe_compare 用本模块的 table_changes

        cmp = compare_recipes(old, recipe)
    lines = _compare_lines(cmp)
    head = "采用按规则重新起草的配方："
    if not lines:
        bag.add("redraft_adopted", head + "分段、标签、工作表名和列的来源与现行配方相同", source="edit")
        return
    shown = "；".join(lines[:6]) + (f"等 {len(lines)} 处" if len(lines) > 6 else "")
    bag.add("redraft_adopted", head + shown, "；".join(lines), source="edit")


def _plan(data: Any) -> dict[str, Any] | None:
    plan = plain(data)
    if plan is None:
        return None
    if not isinstance(plan, dict) or not plan.get("action"):
        raise ValueError("累积计划不完整：缺少 action，无法确定要确认的项")
    return plan


def _span(p: Any) -> tuple[str, str] | None:
    if not isinstance(p, dict) or not p.get("start") or not p.get("end"):
        return None
    return str(p["start"]), str(p["end"])


def _spans(parts: list[Any]) -> str:
    return "、".join(f"{s} 至 {e}" for s, e in (_span(p) for p in parts or []) if s) if parts else ""


def _blocker_text(part: dict[str, Any]) -> str:
    msgs = [str(b.get("message") if isinstance(b, dict) else b) for b in part.get("blockers") or []]
    return "；".join(m for m in msgs if m)


def _retired_scope(plan: dict[str, Any] | None) -> tuple[set[str], set[tuple[str, str | None]]]:
    """breaking 去重的范围：有 retire:* 的表（只在追加、替换该期时出），以及早已退役的 (表, 列)。"""
    if plan is None or plan.get("mode") != "accumulate" or plan.get("action") not in ("append", "replace_period"):
        return set(), set()
    tables = {str(e.get("table")) for e in plan.get("retired_new") or [] if isinstance(e, dict)}
    existing = {(str(e.get("table")), e.get("column")) for e in plan.get("retired_existing") or []
                if isinstance(e, dict)}
    return tables, existing


#: 区域外文字里提示单位或口径的词（P3-SPEC 2.5）
_SCOPE_WORDS = ("单位", "口径", "统计范围", "计量")
#: 含单位字、却不是在说单位的常见词。按规格「出现即算」，只把这些词先去掉再找单位词：单字单位（人、元、个、件、户）
#: 太容易出现在别的词里，「编制人：测试员甲」→「编制人：测试员乙」这种每期都可能变的落款不该要求确认单位。
#: 只列确定不是单位的词；拿不准的（人数、件数、户数）不列，宁可多确认一次
_UNIT_STOPWORDS = (
    "编制人", "填报人", "填表人", "制表人", "负责人", "联系人", "审核人", "复核人", "审批人", "批准人", "经办人",
    "统计人", "报送人", "签发人", "核对人", "人员", "人工", "单元", "元旦", "文件", "附件", "条件", "事件", "邮件",
    "软件", "用户", "账户", "客户", "门户", "个别", "各个", "整个", "个人", "个月",
    # 「单位」在这几个词里指填报的机构，不是计量单位
    "填报单位", "编制单位", "报送单位", "制表单位", "责任单位",
)


def _unit_pattern() -> re.Pattern[str]:
    """单位词（P3-SPEC 2.5：新旧文字里出现 UNITS 词表中的词，或出现「单位」「口径」「统计范围」「计量」之一）。
    出现就算，不论前后是什么：「货运量按吨统计」→「按件统计」、「每户」→「每人」、「元/件」→「元/个」都是单位真的变了。
    误报靠 _UNIT_STOPWORDS 先去掉（_unit_tokens）。单位前面的数量级（万、千、百、亿）算进同一个词：「人次」→「万人次」
    要能比出不同；多字的排在单字前面，「人次」不会被拆成「人」。"""
    units = sorted(UNITS, key=len, reverse=True)
    alt = "|".join(re.escape(u) for u in units)
    scope = "|".join(map(re.escape, _SCOPE_WORDS))
    return re.compile(rf"(?:{scope})|(?:[万千百亿]?(?:{alt}))")


_UNIT_RE = _unit_pattern()
#: 停用词按长度从长到短排进正则：长词先匹配，不会被其中的短词截断
_STOP_RE = re.compile("|".join(re.escape(w) for w in sorted(_UNIT_STOPWORDS, key=len, reverse=True)))


def _unit_tokens(text: str) -> list[str]:
    """文字里的单位、口径词（去掉停用词之后）。停用词换成「|」：它不是单位，也不会和前后的字拼成新词。"""
    return sorted(m.group(0) for m in _UNIT_RE.finditer(_STOP_RE.sub("|", canon(text))))


#: 区域外文字的差异项，以及统计期格写法的变化（D12 那种）：单位、口径的说明可能写在这些格里
_OUTSIDE_KINDS = ("outside_added", "outside_removed", "outside_changed", "context_text", "context_source_added")


def _unit_risks(ctx: ConfirmContext, recipe: Recipe, old: Recipe | None) -> list[str]:
    """按期累积时单位写在配方之外、只表现为区域外文字变化的那几种（评审一-M10）。返回涉及单位或口径的变化。"""
    out: list[str] = []
    source = ctx.outside_diff if ctx.outside_diff is not None else ctx.diff
    for d in source or []:
        d = plain(d)
        if not isinstance(d, dict) or d.get("kind") not in _OUTSIDE_KINDS:
            continue
        # 只看差异项里的原文（detail：全文或「上一期：…；本期：…」），不看 label 里的坐标和工作表名
        if _unit_tokens(str(d.get("detail") or "")):
            out.append(str(d.get("label") or ""))
    if old is not None:
        # 分段标题（D09 那种改字）：只在新旧标题里的单位、口径词不同时算。标题里常年写着「（人次）」，只改了别的字
        # 就报，会让每次改标题都多一项必勾
        titles = {s.id: s.locate.title for s in _segments(old) if getattr(s.locate, "title", None)}
        for seg in _segments(recipe):
            new_title, old_title = getattr(seg.locate, "title", None), titles.get(seg.id)
            if new_title and old_title and _unit_tokens(new_title) != _unit_tokens(old_title):
                out.append(f"分段「{seg.id}」的标题「{old_title}」→「{new_title}」")
    return out


def _segments(recipe: Recipe) -> list[Any]:
    return [s for sheet in recipe.sheets for b in sheet.blocks if isinstance(b, CrosstabBlock) for s in b.segments]


def _switch_to_accumulate_text(plan: dict[str, Any], ctx: ConfirmContext, dropped: list[dict[str, Any]]) -> str | None:
    """mode_switch:replace->accumulate 冒号后面那一句（P3-SPEC 2.4 表）。按 action 分开写，不能只看 parts：

    - append：当前那一期原样保留，parts 里它是 new=False 的那一项；
    - replace_period：当前那一期被本次替换（修改配方后重新导入同一个文件，或者同一统计期再传），parts 里只有本期，
      被替换的那一期在 replaces。只看 parts 找不到「第一期」，早先因此落到「当前版本没有统计期」，与同一清单里的
      period_replace 自相矛盾（评审意见）；
    - restart：配方变化不兼容；
    - first：当前那一期没有统计期，或者它的配方不满足资格，只有这里才写这两种原因；
    - rejected：试运行拒收（部分重叠），不能提交，不出这一项（拒收的原因在问题列表里），返回 None。
    """
    action = plan.get("action")
    if action == "append":
        kept = next((p for p in plan.get("parts") or [] if isinstance(p, dict) and not p.get("new")), None)
        sp = _span(kept)
        if sp is None:
            raise ValueError("累积计划是追加，但结果里没有可作为第一期保留的当前那一期")
        return f"当前版本的 {sp[0]} 至 {sp[1]} 作为第一期保留，此前被替换的各期不会补回"
    if action == "replace_period":
        rep = plan.get("replaces") if isinstance(plan.get("replaces"), dict) else None
        sp = _span(rep)
        if sp is None:
            raise ValueError("累积计划是替换该期，但没有给出被替换那一期的统计期")
        if ctx.kind == "redraft":
            return f"当前版本的 {sp[0]} 至 {sp[1]} 按修改后的配方重新导入，作为第一期，此前被替换的各期不会补回"
        return f"本次导入替换当前版本的 {sp[0]} 至 {sp[1]}，作为第一期，此前被替换的各期不会补回"
    if action == "restart":
        return "表结构有不兼容的变化，当前版本不能作为第一期：启用后当前版本只含本期"
    if action == "first":
        blocked = next((p for p in dropped if p.get("blockers")), None)
        if blocked is not None:
            return (f"当前版本的配方不满足按期累积的要求（{_blocker_text(blocked)}），不能作为第一期："
                    "启用后当前版本只含本期")
        # 计划里当前那一期是 dropped 的最后一项（plan_accumulate 取最晚的一期判资格）
        if not dropped or _span(dropped[-1]) is None:
            return "当前版本没有统计期，不能作为第一期：启用后当前版本只含本期"
        why = f"（{plan['reason']}）" if plan.get("reason") else ""
        return f"当前版本不能作为第一期{why}：启用后当前版本只含本期"
    return None


def _accumulate_items(bag: _Bag, plan: dict[str, Any], ctx: ConfirmContext, recipe: Recipe,
                      old: Recipe | None) -> None:
    """按期累积的必勾项（P3-SPEC 2.4、2.5、9.5），来源都是 accumulate。"""
    action, switch = plan.get("action"), plan.get("mode_switch")
    dropped = [p for p in plan.get("dropped") or [] if isinstance(p, dict)]
    n = len(dropped)
    before = f"此前 {n} 期" if n else "此前各期"

    if action == "restart":
        changes = [str(x) for x in plan.get("semantic") or []]
        for p in dropped:
            # 已是累积模式、某一期的配方不满足资格（blockers）也走 restart，变化一栏写明是哪一期、为什么
            text, sp = _blocker_text(p), _span(p)
            if text:
                where = f"{sp[0]} 至 {sp[1]} 这一期" if sp else "此前的一期"
                changes.append(f"{where}的配方不满足按期累积的要求：{text}")
        what = "（" + "；".join(changes) + "）" if changes else ""
        bag.add("accumulate_restart",
                f"表结构有不兼容的变化{what}：启用后当前版本只含本期，{before}不再出现在当前版本中，之后各期在本期基础上"
                "继续累积；可以在数据源卡片的「版本」中启用此前的版本",
                "如果不想重新开始累积，请返回修改配方（例如保留原来的单位、列名或常量取值）", source="accumulate")

    if switch == "accumulate->replace":
        spans = _spans(dropped)
        bag.add("mode_switch:accumulate->replace",
                f"从按期累积改为每期替换：启用后当前版本只含本期，{before}" + (f"（{spans}）" if spans else "")
                + "不再出现在当前版本中，可以在数据源卡片的「版本」中启用此前的版本", source="accumulate")
    elif switch == "replace->accumulate":
        text = _switch_to_accumulate_text(plan, ctx, dropped)
        if text is not None:
            bag.add("mode_switch:replace->accumulate", "从每期替换改为按期累积：" + text, str(plan.get("reason") or ""),
                    source="accumulate")

    accumulating = plan.get("mode") == "accumulate" and action in ("append", "replace_period")
    if accumulating:
        for e in plan.get("retired_new") or []:
            if not isinstance(e, dict) or not e.get("table"):
                continue
            t, c = str(e["table"]), e.get("column")
            if c:
                bag.add(f"retire:{t}.{c}", f"表「{t}」的列「{c}」自本期起不再导入：早期各期保留原值，本期起为空值",
                        "如果是改名：旧列名保留早期各期的值，新列名从本期起有值，两列并存", source="accumulate")
            else:
                bag.add(f"retire:{t}", f"表「{t}」自本期起不再导入：早期各期的行保留，本期起没有新的行",
                        "如果是改名：旧表保留早期各期的行，新表从本期起有行，两张表并存", source="accumulate")

    if action == "replace_period":
        rep = plan.get("replaces") if isinstance(plan.get("replaces"), dict) else {}
        sp = _span(rep) or _span(plan.get("period"))
        if sp is None:
            raise ValueError("累积计划是替换该期，但没有给出被替换那一期的统计期")
        if ctx.kind == "redraft":
            label = f"用修改后的配方重新导入最近一期 {sp[0]} 至 {sp[1]}：启用后该期以本次为准，其余各期不变"
        else:
            src = []
            if rep.get("seq") is not None:
                src.append(f"第 {rep['seq']} 次导入")
            if rep.get("file_name"):
                src.append(f"「{short(rep['file_name'])}」")
            label = (f"替换已有的一期 {sp[0]} 至 {sp[1]}" + (f"（{''.join(src)}）" if src else "")
                     + "：启用后该期以本次为准")
        bag.add(f"period_replace:{sp[0]}~{sp[1]}", label, source="accumulate")

    if accumulating:
        risks = _unit_risks(ctx, recipe, old)
        if risks:
            shown = "；".join(risks[:3]) + (f"等 {len(risks)} 处" if len(risks) > 3 else "")
            bag.add("accumulate_unit_risk",
                    f"区域外的说明文字有变化，且涉及单位或口径（{shown}）：确认后，本期与此前各期按同一单位累积在同一列中。"
                    "如果单位已经变了，请返回修改配方里的单位（这会重新开始累积）", "；".join(risks), source="accumulate")


def _mask_items(bag: _Bag, ctx: ConfirmContext, recipe: Recipe, old: Recipe | None) -> None:
    """遮罩列在新结构里找不到同名列（不区分大小写）：出必勾 mask_lost:<列>（AU-6）。

    遮罩按列名匹配：从简单导入切换过来，「金额（元）」的列名从期 1 的「金额_元」变成「金额」；改配方改了列名也一样。
    只报这次变化弄丢的：旧结构里本来就没有这一列的遮罩（早就不生效了）不算。
    """
    new_cols = {c.name.lower() for cols in derive_tables(recipe)[0].values() for c in cols}
    old_cols: set[str] | None = None
    if ctx.kind == "switch" and ctx.old_schema_cache is not None:
        old_cols = {str(c.get("name")).lower() for meta in (ctx.old_schema_cache.get("tables") or {}).values()
                    for c in (meta or {}).get("columns") or [] if isinstance(c, dict)}
    elif old is not None:
        old_cols = {c.name.lower() for cols in derive_tables(old)[0].values() for c in cols}
    for m in ctx.mask_columns or []:
        key = str(m).strip()
        if not key or key.lower() in new_cols or (old_cols is not None and key.lower() not in old_cols):
            continue
        bag.add(f"mask_lost:{key}",
                f"遮罩列「{key}」在新的表结构里没有同名列：提交后这一列的遮罩不再生效，证据面板和裁判摘录会显示它的值。"
                "如需继续遮罩，提交后请在数据源设置里改成新的列名",
                source="switch" if ctx.kind == "switch" else "recipe")


def _sheet_name(ex: dict[str, Any], sheet: SheetRecipe) -> str:
    """配方工作表 → 本期实际的工作表名（改了名的按 fallback 认到的那张）。"""
    matched = (ex.get("sheets") or {}).get("matched") or {}
    return str(matched.get(sheet.id) or sheet.match.name)


def _blocks(recipe: Recipe, ex: dict[str, Any]) -> list[tuple[SheetRecipe, str, Any]]:
    return [(sheet, _sheet_name(ex, sheet), block) for sheet in recipe.sheets for block in sheet.blocks]


def _count(counts: dict[str, Any], text: str) -> int:
    if text in counts:
        return int(counts[text] or 0)
    return sum(int(v or 0) for k, v in counts.items() if canon(k) == canon(text))


def _recipe_items(bag: _Bag, recipe: Recipe, ex: dict[str, Any], facts: Any, *, ai: bool = False) -> None:
    blocks = _blocks(recipe, ex)
    crosstabs = [b for _, _, b in blocks if isinstance(b, CrosstabBlock)]
    lists = [b for _, _, b in blocks if isinstance(b, ListBlock)]

    # 占位符：配置了就出，写上本期个数（可以是 0）
    counts = ex.get("placeholders") or {}
    meanings: dict[str, str] = {}
    for _, _, b in blocks:
        for p in b.values.placeholders:
            meanings.setdefault(p.text, p.meaning)
    for text, meaning in meanings.items():
        bag.add(f"placeholder:{text}", f"「{text}」存为空值（表示{meaning}，本期 {_count(counts, text)} 格）")

    for b in crosstabs:
        if b.values.blank == "null":
            bag.add(f"blank_null:{b.id}", f"「{b.id}」数据区的空格存为空值")
    for _, _, b in blocks:
        if b.values.text_number == "parse_thousands":
            bag.add(f"text_number:{b.id}", f"「{b.id}」中千分位写法的数字文本按数字保存")

    # 公式按保存值：交叉表配置了就出；列表的默认值就是放宽，本期确有公式格才出。回执只有全表一个计数，
    # 只有一个块按保存值导入时才写个数（多个块分不开，宁可不写也不写错）
    accepted = int(ex.get("formula_cells_accepted") or 0)
    cached = [b for _, _, b in blocks if b.values.formula == "accept_cached"]
    for b in cached:
        if isinstance(b, CrosstabBlock) or accepted > 0:
            n = f"（本期 {accepted} 格）" if len(cached) == 1 else ""
            bag.add(f"formula_cached:{b.id}", f"「{b.id}」中的公式格按文件里保存的值导入{n}")

    for b in lists:
        if b.merged_data == "fill":
            bag.add(f"merged_fill:{b.id}", f"「{b.id}」数据区的合并单元格按左上格的值填充")
    for b in crosstabs:
        missing = [name for key, name in _AXIS_CHECK.items() if key not in b.axis.checks]
        if missing:
            bag.add(f"checks_relaxed:{b.id}", f"「{b.id}」不检查日期" + "、".join(f"「{m}」" for m in missing))
    for sheet in recipe.sheets:
        if sheet.context and sheet.context[0].cross_check == "none":
            name = _sheet_name(ex, sheet)
            bag.add(f"cross_check_off:{name}", f"工作表「{name}」的统计期不与文件名里的日期核对")

    ignored = ex.get("ignored_columns") or {}
    for _, _, b in blocks:
        heads = [str(h) for h in ignored.get(b.id) or []]
        extra = isinstance(b, ListBlock) and b.extra_columns == "ignore"
        if b.ignore_columns:
            # 期 3 的按表头忽略（修复按钮 ⑥、框选）：配置了就出，写明每条规则和理由，细节里是本期实际忽略的列
            rules = "、".join(f"「{short(r.header, 20)}」（理由：{short(r.reason)}）" for r in b.ignore_columns)
            tail = "；其余多出的列也不导入" if extra else ""
            now = "本期忽略的列：" + "、".join(heads) if heads else "本期没有命中的列"
            bag.add(f"ignored_columns:{b.id}", f"「{b.id}」按表头忽略列：{rules}{tail}", now)
        elif extra and heads:
            bag.add(f"ignored_columns:{b.id}", f"「{b.id}」不导入这些列：" + "".join(f"「{short(h, 20)}」" for h in heads),
                    "、".join(heads))
    _ignore_rule_items(bag, recipe, ex)
    skips = [b for b in lists if b.rows.blank_rows == "skip"]
    for b in skips:
        n = f"（本期 {int(ex.get('blank_rows_skipped') or 0)} 行）" if len(skips) == 1 else ""
        bag.add(f"blank_skip:{b.id}", f"「{b.id}」跳过数据中间的空行{n}")

    # 合计段、合计行改作核对
    seg_rows = {str(s.get("segment")): s.get("rows") for s in ex.get("labels") or [] if isinstance(s, dict)}
    for b in crosstabs:
        for seg in b.segments:
            if isinstance(seg, DerivedSegment):
                prefix = rows_text(seg_rows.get(seg.id)) or f"分段「{seg.id}」的合计行"
                keep = f"，原值另存表「{seg.keep_as.table}」" if seg.keep_as else "，原值不另存"
                bag.add(f"derived:{seg.id}", f"{prefix}改作核对{keep}")
    derived_rows: dict[str, set[int]] = {}
    for d in ex.get("derived") or []:
        if isinstance(d, dict) and d.get("kind") == "column_sum":
            _, r, _ = split_coord(_coord_of(d))
            if r is not None:
                derived_rows.setdefault(str(d.get("segment")), set()).add(r)
    for b in lists:
        total = b.rows.total_row
        if total is not None:
            rows = rows_text(derived_rows.get(b.id))
            prefix = f"「{b.id}」的合计行" + (f"（{rows}）" if rows else "")
            keep = f"，原值另存表「{total.keep_as}」" if total.keep_as else "，原值不另存"
            # 只有数字列按明细核对：文字列（AI 可能把数字列写成 TEXT）合计行里的数不核对，写在细节里（SE-10）
            nums = [c.name for c in b.columns if c.type in ("INTEGER", "REAL")]
            detail = (f"按明细核对的列：{'、'.join(nums)}；其余列合计行里的内容不核对" if nums
                      else "这个块没有数字列，合计行里的内容不核对")
            bag.add(f"total_row:{b.id}", f"{prefix}改作核对{keep}", detail)

    # 年份取自统计期：表头是日期格时年份来自格子本身，不算
    forms: dict[str, set[str]] = {}
    for a in ex.get("axes") or []:
        if isinstance(a, dict):
            forms.setdefault(str(a.get("block")), set()).add(str(a.get("form") or ""))
    for b in crosstabs:
        if b.axis.year_from is not None and forms.get(b.id) != {"date"}:
            bag.add(f"year_from:{b.id}", f"「{b.id}」的日期年份取自统计期")

    fact_text = _fact_texts(facts)
    for r in recipe.relations:
        if isinstance(r, SumEq):
            formula = f"{r.total} = " + " + ".join(r.parts)
            bag.add(f"relation:{r.id}", f"每期核对「{formula}」（表「{r.table}」）")
        elif isinstance(r, NotComparable):
            bag.add(f"relation:{r.id}", f"表「{r.a.table}」与表「{r.b.table}」口径不同，不互相推算或相加",
                    f"按「{r.by}」汇总的「{r.a.value}」不作为「{r.b.value}」的核对依据")
    for r in recipe.relations:
        if isinstance(r, Dismissed):
            what = f"「{fact_text[r.claims]}」" if r.claims in fact_text else f"系统发现的关系 {r.claims}"
            # AI 起草的配方里，理由可能出自模型（SE-10）：标明来源，别让说服性的文字看起来像系统的结论
            who = "；理由可能由 AI 填写，请核实" if ai else ""
            bag.add(f"dismissed:{r.claims}", f"不登记{what}（理由：{r.reason}{who}）")

    tables, _ = derive_tables(recipe)
    for t, cols in tables.items():
        for c in cols:
            if c.unit:
                head = f"「{c.header}」→ " if c.header else ""
                bag.add(f"unit:{t}.{c.name}", f"{head}表「{t}」的列「{c.name}」，单位 {c.unit}")

    hidden = ex.get("hidden") or {}
    for sheet in recipe.sheets:
        name = _sheet_name(ex, sheet)
        h = hidden.get(name) or {}
        parts = []
        if sheet.hidden.rows != "reject_if_any":
            word = "包含" if sheet.hidden.rows == "include" else "排除"
            parts.append(f"隐藏行：{word}（{rows_text(h.get('rows')) or '本期没有隐藏行'}）")
        if sheet.hidden.cols != "reject_if_any":
            cols = [col_letter(c) if isinstance(c, int) else str(c) for c in h.get("cols") or []]
            parts.append(f"隐藏列：包含（{'、'.join(cols) + ' 列' if cols else '本期没有隐藏列'}）")
        if parts:
            bag.add(f"hidden:{name}", f"工作表「{name}」的" + "；".join(parts))

    # 两种取值的后果（P3-SPEC 2.2，评审三-m4）：首次导入时由人确认，这就是 D14 的「首次导入时由人确认」
    bag.add("mode", "导入模式：每期替换" if recipe.mode == "replace" else "导入模式：按期累积", MODE_DETAIL[recipe.mode])


#: 导入模式两种取值的后果（q_mode 卡片和确认项 mode 用同一句）
MODE_DETAIL = {
    "accumulate": "每期以统计期为键加入当前版本；统计期与已有各期部分重叠的文件会被拒收，同一统计期再传要确认替换",
    "replace": "每次上传新一期，当前版本只含新的一期，此前各期留在历史版本中",
}


def _excluded_rows(ex: dict[str, Any], reason: str) -> list[dict[str, Any]] | None:
    """本期 rows_excluded 里某种原因的项；回执里没有这个字段（期 3 之前）时 None，个数就不写。"""
    if "rows_excluded" not in ex:
        return None
    entries = (plain(x) for x in ex.get("rows_excluded") or [])
    return [e for e in entries if isinstance(e, dict) and e.get("reason") == reason]


def _row_count(entries: list[dict[str, Any]]) -> int:
    return sum(int(b) - int(a) + 1 for e in entries for a, b in (e.get("rows") or []))


def _rule_text(anchor: str, reason: str, entries: list[dict[str, Any]] | None) -> str:
    """「「补录（人次）」（理由：…；本期 1 行）」。本期命中几行按 rows_excluded 里锚点相同（match_key）的项数。"""
    tail = ""
    if entries is not None:
        hit = [e for e in entries if match_key(e.get("anchor") or "") == match_key(anchor)]
        tail = f"；本期 {_row_count(hit)} 行"
    return f"「{short(anchor, 20)}」（理由：{short(reason)}{tail}）"


def _ignore_rule_items(bag: _Bag, recipe: Recipe, ex: dict[str, Any]) -> None:
    """期 3 的忽略规则（修复按钮 ⑥ 和框选写入）：按行标签忽略 ignore_rows:<块>、按同一行文字忽略区域外的数字
    ignore_outside:<工作表>。配置了就出，写上每条规则的理由和本期命中的行数（可以是 0）。"""
    rows = _excluded_rows(ex, "ignored_rows")
    for sheet in recipe.sheets:
        for b in sheet.blocks:
            if isinstance(b, CrosstabBlock) and b.ignore_rows:
                mine = None if rows is None else [e for e in rows if e.get("block") == b.id]
                rules = "、".join(_rule_text(r.label, r.reason, mine) for r in b.ignore_rows)
                bag.add(f"ignore_rows:{b.id}", f"「{b.id}」按行标签忽略：{rules}",
                        "；".join(f"「{r.label}」：{r.reason}" for r in b.ignore_rows))
    outside = _excluded_rows(ex, "ignored_outside")
    for sheet in recipe.sheets:
        if not sheet.ignore_outside:
            continue
        name = _sheet_name(ex, sheet)
        mine = None if outside is None else [e for e in outside if e.get("sheet") == name]
        rules = "、".join(_rule_text(r.anchor, r.reason, mine) for r in sheet.ignore_outside)
        bag.add(f"ignore_outside:{name}", f"工作表「{name}」按同一行的文字忽略导入区域之外的数字：{rules}",
                "；".join(f"「{r.anchor}」：{r.reason}" for r in sheet.ignore_outside))


def _coord_of(d: dict[str, Any]) -> str:
    cell = str(d.get("cell") or "")
    return cell if "!" in cell or not d.get("sheet") else f"{d['sheet']}!{cell}"


def _fact_texts(facts: Any) -> dict[str, str]:
    data = plain(facts)
    rows = data.get("facts") if isinstance(data, dict) else None
    return {str(f["id"]): str(f.get("text") or "") for f in rows or [] if isinstance(f, dict) and f.get("id")}


_TYPE_NAME = {"TEXT": "文字", "INTEGER": "整数", "REAL": "小数", "DATE": "日期"}


def _col_desc(types: dict[tuple[str, str], str], table: str, c: Any) -> str:
    t = types.get((table, c.name), c.type)
    parts = [_TYPE_NAME.get(t, t)] + ([c.unit] if c.unit else [])
    return f"{c.name}（{'，'.join(parts)}）"


def _ai_items(bag: _Bag, recipe: Recipe) -> None:
    """AI 起草的配方：列名偏离机械推导的逐列确认（rename），每张表的列（带类型、单位）再整体看一遍（columns）。"""
    bag.add("ai_origin", "本配方由 AI 起草，已逐项核对")
    tables, _ = derive_tables(recipe)
    table_types = _declared_types(recipe, tables)
    pos = {(t, c.name): i for t, cols in tables.items() for i, c in enumerate(cols, 1)}

    def check(table: str, source: str, name: str, word: str) -> None:
        expected = to_sql_name(split_unit_suffix(source)[0], fallback=f"列{pos.get((table, name), 1)}")
        if name != expected:
            bag.add(f"rename:{table}.{name}", f"原{word}「{source}」改名为列「{name}」",
                    f"按{word}推出的列名是「{expected}」")

    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, MeasuresSegment):
                        for label in seg.labels.expect:
                            check(seg.table, label, seg.measures.get(label, ""), "标签")
            else:
                for col in block.columns:
                    check(block.table, col.header, col.name, "表头")
    # 每列带上类型和单位（SE-10）：AI 把数字列写成 TEXT 能过静态校验，之后既不做数字检查、合计行也不核对这一列
    for t, cols in tables.items():
        bag.add(f"columns:{t}", f"表「{t}」的列：" + "、".join(_col_desc(table_types, t, c) for c in cols))


def _period_items(bag: _Bag, recipe: Recipe, ex: dict[str, Any], ctx: ConfirmContext) -> None:
    """本期类：每次都按本期情况算，与配方变没变无关。"""
    prev = plain(ctx.prev) if ctx.prev is not None else None
    p_rec = receipt_of(prev) if prev is not None else {}
    # 上一期是简单导入时（回执里没有格子账），逐格文字比不了，按首次处理
    has_prev = prev is not None and "ledger" in p_rec
    p_out = {o["coord"]: o for o in outside_of(p_rec)} if has_prev else {}
    p_per = period_texts(prev, p_rec) if has_prev else {}
    c_per = period_texts(ex, ex)
    c_ann = period_annotated(ex, ex)

    for o in outside_of(ex):
        if o["kind"] != "text_digits":
            continue
        if has_prev and _outside_exempt(o, p_out, p_per, c_per, c_ann):
            continue
        bag.add(f"outside_digits:{o['coord']}", f"区域外有含数字的文字「{short(o['text'])}」",
                f"{o['coord']}：{o['text']}", source="outside")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in ex.get("problems") or []:
        p = plain(p)
        if isinstance(p, dict) and p.get("code") == "rows_after_stop":
            grouped.setdefault(_stop_block(p, recipe, ex), []).append(p)
    for blk, probs in grouped.items():
        cells = [str(c) for p in probs for c in p.get("cells") or []]
        bag.add(f"rows_after_stop:{blk}", "；".join(str(p.get("message") or "") for p in probs),
                "、".join(cells[:20]), source="outside")

    # 列表块只有表头、没有数据行（AU-1）：问题对象里没有块键，按消息里的「列表「块」」认
    for p in ex.get("problems") or []:
        p = plain(p)
        if isinstance(p, dict) and p.get("code") == "list_empty":
            blk = next((b.id for _, _, b in _blocks(recipe, ex)
                        if isinstance(b, ListBlock) and f"列表「{b.id}」" in str(p.get("message") or "")), None)
            table = next((b.table for _, _, b in _blocks(recipe, ex) if isinstance(b, ListBlock) and b.id == blk), "")
            key = blk or "、".join(str(c) for c in p.get("cells") or [])
            what = f"表「{table}」" if table else "这张表"
            bag.add(f"empty_block:{key}", f"「{key}」表头之下没有数据行：本期{what}将导入 0 行（上一期的数据会被替换掉）",
                    "、".join(str(c) for c in p.get("cells") or []), source="outside")

    relations = {r.id: r for r in recipe.relations}
    for c in ctx.checks or []:
        c = plain(c)
        if not isinstance(c, dict) or c.get("kind") != "relation_not_comparable":
            continue
        checked, failed = int(c.get("checked") or 0), int(c.get("failed") or 0)
        # 约定（P2-SPEC 5.2）：failed 是不相等的分组数；全部相等时 failed 记 0、checked 记分组数
        if checked > 0 and failed == 0:
            r = relations.get(str(c.get("id")))
            if isinstance(r, NotComparable):
                label = (f"表「{r.a.table}」与表「{r.b.table}」声明为口径不同，"
                         f"但本期按「{r.by}」分组的 {checked} 组全部相等")
            else:
                label = f"{c.get('title') or c.get('id')}：声明为口径不同，但本期 {checked} 组全部相等"
            bag.add(f"relation_observed:{c.get('id')}", label)

    sheets = ex.get("sheets") or {}
    p_other = {str(s) for s in (p_rec.get("sheets") or {}).get("other_visible") or []} if has_prev else set()
    for name in sheets.get("other_visible") or []:
        if str(name) not in p_other:
            bag.add(f"sheet_extra:{name}", f"另有可见工作表「{name}」，不导入", source="sheet")
    matched = {str(v): str(k) for k, v in (sheets.get("matched") or {}).items()}
    for old_name, new_name in (sheets.get("renamed") or {}).items():
        sid = next((s.id for s in recipe.sheets if s.match.name == old_name), None)
        if sid is None:
            sid = next((s.id for s in recipe.sheets if match_key(s.match.name) == match_key(old_name)), None)
        sid = sid or matched.get(str(new_name)) or str(old_name)
        bag.add(f"sheet_renamed:{sid}", f"工作表「{old_name}」现在叫「{new_name}」", source="sheet")

    period = period_of(ex) or {}
    if period.get("source") == "human":
        signed = f"署名（未认证）：{period['signed_by']}" if period.get("signed_by") else ""
        bag.add(f"context_human:{_PERIOD_ID}", f"本期统计期为人工录入：{period.get('start')} 至 {period.get('end')}",
                signed, source="context")


def _outside_exempt(o: dict[str, Any], p_out: dict[str, dict[str, Any]], p_per: dict[str, str],
                    c_per: dict[str, str], c_ann: dict[str, str]) -> bool:
    """带附加文字的统计期格免确认的两种情况（P2-SPEC 7.5 outside_digits）：

    (a) 上一期同一格是区域外纯文字，且它的 match_key 等于本期这格去掉统计期之后的文字（标题加了年月，D17、D27）；
    (b) 上一期同一格也是统计期来源，且两期是同一句（每月重复的同一句）：数字遮盖后的模板相同，**并且**只去掉
        统计期那一段之后的文字（period_residue，其余数字照留）也相同。只比遮盖模板的话，「注：2026年9月数据只含
        3个分区」→「注：2026年10月数据只含2个分区」会被当成同一句，口径数字变了没人看（AU-3）。任一期认不出
        统计期（period_residue 为 None）就不免。
    不是统计期来源的含数字文字（D28 的「注：9月15日…」）每期都要确认；上一期没有这一格的（D28b）也要。
    """
    cell = o["coord"]
    if not (o["period_source"] or cell in c_per or cell in c_ann):
        return False
    prev = p_out.get(cell)
    if prev is not None and prev["kind"] == "text":
        residue = c_ann.get(cell)
        if residue is None:
            residue = period_residue(o["text"])
        if residue is not None and match_key(prev["text"]) == match_key(residue):
            return True
    return cell in p_per and same_period_sentence(p_per[cell], o["text"])


def _stop_block(problem: dict[str, Any], recipe: Recipe, ex: dict[str, Any]) -> str:
    """rows_after_stop 属于哪个列表块。问题对象里没有块键，按工作表和溯源的列范围找：同一工作表上被空行停下的
    列表块只有一个就是它；有多个时取列范围与问题格相交、数据在问题格之上、最靠近的那个。

    执行器的消息里写着「列表「块」」，恰好认出一个块时直接用它。跳过空行的列表按表下说明收尾时也会报这个问题
    （WP-8 修补 3 与评审意见 4），下面按 blank_rows=stop 找候选的办法找不到它。"""
    message = str(problem.get("message") or "")
    named = [b.id for _, _, b in _blocks(recipe, ex) if isinstance(b, ListBlock) and f"列表「{b.id}」" in message]
    if len(named) == 1:
        return named[0]
    cells = [split_coord(c) for c in problem.get("cells") or []]
    sheet = next((s for s, _, _ in cells if s), None)
    rows = [r for _, r, _ in cells if r is not None]
    cols = {c for _, _, c in cells if c is not None}
    cands = [(name, b) for _, name, b in _blocks(recipe, ex)
             if isinstance(b, ListBlock) and b.rows.blank_rows == "stop" and (sheet is None or name == sheet)]
    if not cands:
        lists = [b for _, _, b in _blocks(recipe, ex) if isinstance(b, ListBlock)]
        return lists[0].id if lists else str(sheet or "")
    if len(cands) == 1 or not rows:
        return cands[0][1].id
    best: tuple[int, str] | None = None
    lineage = ex.get("lineage") or {}
    for name, b in cands:
        span_cols: set[int] = set()
        last = 0
        for runs in (lineage.get(b.table) or {}).values():
            for run in runs or []:
                if not isinstance(run, (list, tuple)) or len(run) < 4:
                    continue
                s, r, c = split_coord(f"{run[1]}!{run[2]}" if "!" not in str(run[2]) else run[2])
                if (s or name) != name or r is None or c is None:
                    continue
                span_cols.add(c)
                last = max(last, r + int(run[3] or 1) - 1 if (run[4] if len(run) > 4 else "down") == "down" else r)
        if span_cols and (not cols or span_cols & cols) and last < min(rows):
            if best is None or last > best[0]:
                best = (last, b.id)
    return best[1] if best else cands[0][1].id
