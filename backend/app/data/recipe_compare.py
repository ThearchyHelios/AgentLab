"""新旧配方对照（期 3，WP-3；P3-SPEC 6.2、9.4 的 RecipeComparison）。

**为什么要有对照。** 期 2 改配方之后只有两种信号：破坏性变更（breaking:*，按表一句）和配方类确认项。修复按钮、
框选、按规则重新起草都会改分段的定位、标签集合、忽略规则、工作表名，这些不在破坏性变更里，人也不会去逐字
比两份 JSON。对照把两份配方摆在一起：表、列（类型、单位、来源）、主键、表的种类、分段（标题、标签增删、
忽略规则）、关系、工作表名、导入模式，以及按期累积的分档。试运行回执、确认清单顶部、修复预览、重新起草的
「采用」都用它（界面复用同一个 RecipeCompare 组件）。

**排序**：破坏性的排在最前，单位变化最前（单位变了，同一列里就混了两种口径，按期累积时尤其要命）。

纯函数：不碰数据库、不读文件。配方收 Recipe，也收 dict（canonical 形式也行，按模型补全默认值）。
"""
from __future__ import annotations

from typing import Any

from app.data.recipe_confirm import (
    TableChange, _column_sources, _declared_types, as_recipe, table_changes,
)
from app.data.recipe_diff import plain
from app.data.recipe_parsers import match_key
from app.data.recipe_types import (
    CrosstabBlock, Dismissed, ListBlock, NotComparable, Recipe, SheetRecipe, SumEq, derive_tables,
)


def _brief(types: dict[tuple[str, str], str], sources: dict[tuple[str, str], str], table: str,
           col: Any) -> dict[str, Any]:
    return {"type": types.get((table, col.name), col.type), "unit": col.unit, "source": sources.get((table, col.name))}


def _table_entries(old: Recipe, new: Recipe, changes: list[TableChange]) -> list[dict[str, Any]]:
    ot, _ = derive_tables(old)
    nt, _ = derive_tables(new)
    otypes, ntypes = _declared_types(old, ot), _declared_types(new, nt)
    osrc, nsrc = _column_sources(old), _column_sources(new)
    ospec = {t.name: t for t in old.tables}
    nspec = {t.name: t for t in new.tables}
    by_table: dict[str, list[TableChange]] = {}
    for ch in changes:
        by_table.setdefault(ch.table, []).append(ch)
    out: list[dict[str, Any]] = []
    for name in list(dict.fromkeys([*nt, *ot])):
        ocols = {c.name: c for c in ot.get(name, [])}
        ncols = {c.name: c for c in nt.get(name, [])}
        mine = by_table.get(name, [])
        columns: list[dict[str, Any]] = []
        for cname in list(dict.fromkeys([*ncols, *ocols])):
            oc, nc = ocols.get(cname), ncols.get(cname)
            ob = _brief(otypes, osrc, name, oc) if oc is not None else None
            nb = _brief(ntypes, nsrc, name, nc) if nc is not None else None
            kinds = [ch.kind for ch in mine if ch.column == cname and ch.kind != "column_removed"]
            if oc is None:
                status = "added"
            elif nc is None:
                status = "removed"
            elif kinds or ob != nb:
                status = "changed"
            else:
                status = "same"
            columns.append({"name": cname, "status": status, "old": ob, "new": nb,
                            "changes": list(dict.fromkeys(kinds))})
        os_, ns_ = ospec.get(name), nspec.get(name)
        og = list(os_.grain) if os_ is not None and name in ot else None
        ng = list(ns_.grain) if ns_ is not None and name in nt else None
        grain_changed = og is not None and ng is not None and set(og) != set(ng)
        kind = {"old": os_.kind if os_ is not None and name in ot else None,
                "new": ns_.kind if ns_ is not None and name in nt else None}
        # 单位另列在 units_changed（排最前），这里只放其余的破坏性变化，与确认项 breaking:<表> 同一口径
        breaking = [ch.message for ch in mine if ch.kind != "unit"]
        if name not in ot:
            status = "added"
        elif name not in nt:
            status = "removed"
        elif mine or grain_changed or kind["old"] != kind["new"] or any(c["status"] != "same" for c in columns):
            status = "changed"
        else:
            status = "same"
        out.append({"name": name, "status": status, "columns": columns,
                    "grain": {"old": og, "new": ng, "changed": grain_changed}, "kind": kind, "breaking": breaking,
                    "_units": any(ch.kind == "unit" for ch in mine)})
    # 单位变化的表最前，其余破坏性变化其次，再是有变化的、新增删除的，没变的最后（sort 稳定，同档保持配方顺序）
    rank = {"changed": 2, "added": 3, "removed": 3, "same": 4}
    out.sort(key=lambda t: 0 if t["_units"] else (1 if t["breaking"] else rank.get(t["status"], 4)))
    for t in out:
        del t["_units"]
    return out


def _title(seg: Any) -> str | None:
    loc = getattr(seg, "locate", None)
    return getattr(loc, "title", None)


def _diff_texts(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """按 match_key 比两组原文：返回 (新增的原文, 去掉的原文)，各自保持原来的顺序。"""
    ok, nk = {match_key(x) for x in old}, {match_key(x) for x in new}
    return [x for x in new if match_key(x) not in ok], [x for x in old if match_key(x) not in nk]


def _block_ignores(block: Any) -> list[str]:
    out = [f"按行标签忽略「{r.label}」" for r in getattr(block, "ignore_rows", [])]
    out += [f"按表头忽略「{r.header}」" for r in getattr(block, "ignore_columns", [])]
    return out


def _sheet_ignores(sheet: SheetRecipe) -> list[str]:
    return [f"按同一行的文字「{r.anchor}」忽略区域外的数字" for r in sheet.ignore_outside]


def _entry(eid: str, status: str, title: tuple[str | None, str | None], labels: tuple[list[str], list[str]],
           ignore: tuple[list[str], list[str]]) -> dict[str, Any]:
    return {"id": eid, "status": status, "title": {"old": title[0], "new": title[1]},
            "labels": {"added": labels[0], "removed": labels[1]}, "ignore": {"added": ignore[0], "removed": ignore[1]}}


def _segment_entries(old: Recipe, new: Recipe) -> list[dict[str, Any]]:
    """分段的对照：交叉表的每个分段按 id 配对（标题、标签增删）。块和工作表上的忽略规则、列表块的 after_title
    没有分段可挂，按块 id、工作表 id 各出一条（只在有变化时出），id 就是块或工作表在配方里的 id。"""
    def segs(r: Recipe) -> dict[str, Any]:
        return {s.id: s for sh in r.sheets for b in sh.blocks if isinstance(b, CrosstabBlock) for s in b.segments}

    def blocks(r: Recipe) -> dict[str, Any]:
        return {b.id: b for sh in r.sheets for b in sh.blocks}

    out: list[dict[str, Any]] = []
    os_, ns_ = segs(old), segs(new)
    for sid in list(dict.fromkeys([*ns_, *os_])):
        a, b = os_.get(sid), ns_.get(sid)
        la = list(a.labels.expect) if a is not None else []
        lb = list(b.labels.expect) if b is not None else []
        added, removed = _diff_texts(la, lb)
        title = (_title(a) if a is not None else None, _title(b) if b is not None else None)
        if a is None:
            status = "added"
        elif b is None:
            status = "removed"
        elif added or removed or title[0] != title[1] or a.model_dump(mode="json") != b.model_dump(mode="json"):
            status = "changed"
        else:
            status = "same"
        # 新增、去掉的分段：标签全部列出（_diff_texts 对空的一边自然给出全部）。起草器换了分段 id 时，新分段的
        # 标题和标签就是它新认出的定位规则，redraft_adopted 要逐项写出来让人确认（P3-SPEC 6.3）
        out.append(_entry(sid, status, title, (added, removed), ([], [])))
    ob, nb = blocks(old), blocks(new)
    for bid in list(dict.fromkeys([*nb, *ob])):
        a, b = ob.get(bid), nb.get(bid)
        ig = _diff_texts(_block_ignores(a) if a is not None else [], _block_ignores(b) if b is not None else [])
        ta = a.after_title if isinstance(a, ListBlock) else None
        tb = b.after_title if isinstance(b, ListBlock) else None
        if ig[0] or ig[1] or ta != tb:
            out.append(_entry(bid, "changed", (ta, tb), ([], []), ig))
    osh = {s.id: s for s in old.sheets}
    nsh = {s.id: s for s in new.sheets}
    for sid in list(dict.fromkeys([*nsh, *osh])):
        a, b = osh.get(sid), nsh.get(sid)
        ig = _diff_texts(_sheet_ignores(a) if a is not None else [], _sheet_ignores(b) if b is not None else [])
        if ig[0] or ig[1]:
            out.append(_entry(sid, "changed", (None, None), ([], []), ig))
    return out


def relation_text(rel: Any) -> str:
    """关系的一句人话（对照界面用，与确认项 relation:* 的写法一致）。"""
    if isinstance(rel, SumEq):
        return f"{rel.total} = " + " + ".join(rel.parts)
    if isinstance(rel, NotComparable):
        return f"表「{rel.a.table}」与表「{rel.b.table}」口径不同，不互相推算或相加"
    if isinstance(rel, Dismissed):
        return f"不登记系统发现的关系 {rel.claims}（理由：{rel.reason}）"
    return str(getattr(rel, "id", ""))


def _relation_entries(old: Recipe, new: Recipe) -> list[dict[str, Any]]:
    orel = {r.id: r for r in old.relations}
    nrel = {r.id: r for r in new.relations}
    out = []
    for rid in list(dict.fromkeys([*nrel, *orel])):
        a, b = orel.get(rid), nrel.get(rid)
        if a is None:
            status = "added"
        elif b is None:
            status = "removed"
        elif a.model_dump(mode="json") != b.model_dump(mode="json"):
            status = "changed"
        else:
            status = "same"
        out.append({"id": rid, "status": status, "old": relation_text(a) if a is not None else None,
                    "new": relation_text(b) if b is not None else None})
    return out


def _sheet_entries(old: Recipe, new: Recipe) -> list[dict[str, Any]]:
    """工作表按配方里的 id 配对，给出匹配名的新旧（⑨ 更新工作表名就是这里的变化）。"""
    osh = {s.id: s.match.name for s in old.sheets}
    nsh = {s.id: s.match.name for s in new.sheets}
    return [{"id": sid, "name": {"old": osh.get(sid), "new": nsh.get(sid)}} for sid in dict.fromkeys([*nsh, *osh])]


def _accumulate(plan: Any) -> dict[str, Any] | None:
    p = plain(plan)
    if not isinstance(p, dict):
        return None
    return {"change": str(p.get("change") or ""), "added": list(p.get("added") or []),
            "retired_new": list(p.get("retired_new") or []), "retired_existing": list(p.get("retired_existing") or [])}


def compare_recipes(old: Recipe | dict[str, Any], new: Recipe | dict[str, Any], *,
                    plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """新旧配方逐项对照（RecipeComparison，P3-SPEC 9.4）。

    - tables：每张表的列（新增、删除、变化；类型、单位、来源，changes 是 table_changes 的 kind）、主键、种类，
      breaking 是这张表除单位以外的破坏性变化（与确认项 breaking:<表> 同一口径）。单位变化的表排最前，其余
      破坏性的其次；没变的也列出（status=same），界面自己决定显示哪些；
    - segments：交叉表的分段（标题、标签增删），以及块、工作表上的忽略规则和列表块的 after_title（有变化时，按块
      或工作表的 id 出一条）；
    - relations、sheets（工作表 id → 匹配名的新旧）、mode；
    - breaking：有没有任何破坏性变化（含单位）；units_changed：单位变化的列；
    - accumulate：给了累积计划（AccumulatePlan 或它的 asdict）时，抄它的分档、新增、退役，否则 None。
    """
    o, n = as_recipe(old), as_recipe(new)
    changes = table_changes(o, n)
    return {
        "tables": _table_entries(o, n, changes),
        "segments": _segment_entries(o, n),
        "relations": _relation_entries(o, n),
        "sheets": _sheet_entries(o, n),
        "mode": {"old": o.mode, "new": n.mode},
        "breaking": bool(changes),
        "units_changed": [{"table": ch.table, "column": ch.column, "old": ch.old, "new": ch.new}
                          for ch in changes if ch.kind == "unit"],
        "accumulate": _accumulate(plan),
    }

