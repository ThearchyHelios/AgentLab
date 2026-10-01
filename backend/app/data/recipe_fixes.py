"""修复按钮（期 3，WP-2，P3-SPEC 第 3 节）：从问题算出封闭的修复提议，再把选中的选项换算成配方补丁。

**客户端只发 fix_id、选项值和理由，不发补丁**（3.1）。补丁里的文字只有三种来源：
- 文件里的原文：标签、表头、分段标题、锚点文字，都来自执行器填的 Problem.fix_args（执行器从格子里取）；
- 系统按固定规则推出的名字：起草器第 8 条的列名（recipe_suggest._measure_column）、{基表}_表内合计、{前一段 id}合计；
- 用户写的理由：只用作说明，不参与匹配。
没有正则，没有用户输入的匹配常量。所以 fix_ops 只认 proposal.options 里有的选项值：带文件原文的选项值
（「<候选格>|pick:<词>」、新工作表名）都是服务端算出来放进去的，客户端改不了内容。

**入参 recipe 一律是完整形式**（Recipe.model_validate(x).model_dump(mode="json")，与 answers_base 相同，评审二-M1）：
暂存区存的是 canonical 形式，等于默认值的字段被去掉了，在它上面写补丁，replace /sheets/{si}/hidden/rows 这类路径会
不存在。WP-5 调用前先转成完整形式。补丁只用 recipe.apply_patch 支持的 add / replace / remove，路径按 id 在完整
形式里找下标。

提议不存库：staging_out 每次按暂存区里存的问题现算。同样的问题得到同样的提议 id（"fx-" + sha1(kind|目标键)[:12]），
界面按问题上的 fix_ids 放按钮（FixAnchor，评审三-B2）。
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any, Callable

from pydantic import ValidationError

from app.data.names import canon, collide_key
from app.data.recipe import apply_patch, recipe_sha256
from app.data.recipe_parsers import (
    candidate_words,
    looks_numeric_text,
    match_key,
    month_day_or_date,
)
from app.data.recipe_suggest import _measure_column
from app.data.recipe_types import (
    DraftFacts,
    EditResult,
    Fact,
    FixAnchor,
    FixOption,
    FixProposal,
    Problem,
    Recipe,
    derive_tables,
)

#: 理由的长度（契约 IgnoreRow.reason、Dismissed.reason 都是 1–200 字）
REASON_MAX = 200
#: 交叉表的分段上限（契约 CrosstabBlock.segments max_length）
_SEGMENTS_MAX = 16
#: 分段标题、工作表名、锚点、表头的长度上限（契约 Locate.title、SheetMatch.name、IgnoreRow.label / IgnoreOutside.anchor、
#: IgnoreColumn.header）。超长的原文写不进配方，不出这个选项
_TITLE_MAX, _SHEET_MAX, _ANCHOR_MAX, _HEADER_MAX = 40, 31, 40, 80
#: 占位符：最长 8 字、每块最多 8 个（契约 Placeholder.text、CrossValues.placeholders）
_PLACEHOLDER_MAX, _PLACEHOLDERS_MAX = 8, 8
#: rename_title 的 pick 类选项最多给几个候选词
_PICK_OPTIONS_MAX = 3


class EditRequestError(ValueError):
    """修改请求本身不对（不是「泛化不了」）。code 是接口层要回的机读码（P3-SPEC 9.1）：

    - edit_invalid：选项不在提议里、理由超长、框选的选项写错（422）；
    - reason_required：需要理由的选项没给理由（422；界面要先填理由再预览，10.2）。
    是 ValueError 的子类：接口层 ``except ValueError`` 时 code 缺省按 edit_invalid。消息可以直接给人看。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _Stale(Exception):
    """提议的目标在当前工作配方里已经找不到了（分段、关系、工作表被改掉）：换算成 ok=False 的 EditResult。"""


# ==========================================================================
# 小工具（recipe_select 也用）
# ==========================================================================


def esc(token: Any) -> str:
    """JSON Pointer 的一段（RFC 6901：~ 写成 ~0、/ 写成 ~1）。"""
    return str(token).replace("~", "~0").replace("/", "~1")


def ptr(*parts: Any) -> str:
    return "".join("/" + esc(p) for p in parts)


def quoted(items: list[str] | tuple[str, ...]) -> str:
    return "、".join(f"「{x}」" for x in items)


def has_digit(text: str) -> bool:
    """与静态校验的 period_literal 同一判断：含任何数字（含全角数字）。"""
    return any(ch.isdigit() for ch in text)


def fix_id(kind: str, key: str) -> str:
    return "fx-" + hashlib.sha1(f"{kind}|{key}".encode("utf-8")).hexdigest()[:12]


def iter_segments(recipe: dict[str, Any]):
    """完整形式的配方里每个交叉表分段：(si, bi, gi, 块, 分段)。"""
    for si, sheet in enumerate(recipe.get("sheets") or []):
        for bi, block in enumerate(sheet.get("blocks") or []):
            if block.get("layout") != "crosstab":
                continue
            for gi, seg in enumerate(block.get("segments") or []):
                yield si, bi, gi, block, seg


def find_segment(recipe: dict[str, Any], seg_id: Any) -> tuple[int, int, int, dict, dict] | None:
    return next(((si, bi, gi, b, s) for si, bi, gi, b, s in iter_segments(recipe) if s.get("id") == seg_id), None)


def find_block(recipe: dict[str, Any], block_id: Any) -> tuple[int, int, dict] | None:
    for si, sheet in enumerate(recipe.get("sheets") or []):
        for bi, block in enumerate(sheet.get("blocks") or []):
            if block.get("id") == block_id:
                return si, bi, block
    return None


def sheet_index(recipe: dict[str, Any], actual: Any, renamed: dict[str, str] | None = None) -> int | None:
    """实际工作表名 → 配方里的工作表下标：按 match_key 同名；按 fallback 认的（renamed：配方名 → 实际名）也算；
    配方只有一个工作表时就是它（执行器的 only_visible_sheet 也只在那时生效）。"""
    sheets = recipe.get("sheets") or []
    if not isinstance(actual, str):
        return None
    key = match_key(actual)
    for si, sheet in enumerate(sheets):
        if match_key((sheet.get("match") or {}).get("name")) == key:
            return si
    for old, new in (renamed or {}).items():
        if match_key(new) == key:
            for si, sheet in enumerate(sheets):
                if match_key((sheet.get("match") or {}).get("name")) == match_key(old):
                    return si
    return 0 if len(sheets) == 1 else None


def table_index(recipe: dict[str, Any], name: Any) -> int | None:
    return next((ti for ti, t in enumerate(recipe.get("tables") or []) if t.get("name") == name), None)


def unique_name(name: str, used: set[str], *, limit: int = 48) -> str:
    """名字撞了（collide_key）就加 _2、_3……（与起草器同一规则）。used 原地更新。"""
    base = name[:limit]
    out, n = base, 2
    while collide_key(out) in used:
        tail = f"_{n}"
        out = base[: limit - len(tail)] + tail
        n += 1
    used.add(collide_key(out))
    return out


def unique_id(want: str, used: set[str], *, limit: int = 32) -> str:
    """分段、块的 id：整份配方内唯一（逐字比较，同静态校验 segment_id_duplicate / block_id_duplicate）。"""
    base = want[:limit]
    out, n = base, 2
    while out in used:
        tail = f"_{n}"
        out = base[: limit - len(tail)] + tail
        n += 1
    used.add(out)
    return out


def all_ids(recipe: dict[str, Any]) -> set[str]:
    """配方里已用的分段 id 和块 id（两者同在 block_order 里，也不能彼此重名）。"""
    out: set[str] = set()
    for sheet in recipe.get("sheets") or []:
        for block in sheet.get("blocks") or []:
            out.add(str(block.get("id")))
            for seg in block.get("segments") or []:
                out.add(str(seg.get("id")))
    return out


def finish(recipe: dict[str, Any] | None, ops: list[dict[str, Any]], *, kind: str, key: str, title: str,
           summary: list[str], anchors: list | None = None, notes: list[str] | None = None,
           expected: dict[str, str | None] | None = None, block: str | None = None,
           breaking: bool = False) -> EditResult:
    """应用补丁、按 schema 解析、算哈希（与暂存区的哈希同一口径：apply_patch → 解析 → recipe_sha256）。

    补丁应用不上或解析不过时 ok=False：正常流程不会出现（补丁按当前工作配方写），出现说明提议已经过期，
    界面照原因提示重新打开问题。语义问题（静态校验的其余规则）不在这里判：预览时 WP-5 照常静态校验、干跑。"""
    base = recipe if isinstance(recipe, dict) else {}
    try:
        new = apply_patch(base, ops)
        sha = recipe_sha256(Recipe.model_validate(new))
    except (ValueError, ValidationError):
        msg = "这条修改已无法应用到当前的配方上：配方在提出修改之后又被改过。请重新查看问题和修复建议"
        return EditResult(ok=False, kind=kind, key=key, title=title, summary=[], ops=[], anchors=[],  # type: ignore[arg-type]
                          notes=list(notes or []), problems=[Problem("edit_not_applicable", "recipe", msg,
                                                                     model_message="edit_not_applicable")])
    return EditResult(ok=True, kind=kind, key=key, title=title, summary=summary, ops=ops,  # type: ignore[arg-type]
                      anchors=list(anchors or []), notes=list(notes or []), problems=[], recipe_sha256_after=sha,
                      expected=expected, block=block, breaking=breaking)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _facts(facts: dict[str, Any] | DraftFacts | None) -> list[Fact]:
    if facts is None:
        return []
    if isinstance(facts, DraftFacts):
        return list(facts.facts)
    out: list[Fact] = []
    for f in (facts.get("facts") or []) if isinstance(facts, dict) else []:
        if isinstance(f, Fact):
            out.append(f)
        elif isinstance(f, dict) and f.get("id") and f.get("kind"):
            out.append(Fact(f["id"], f["kind"], f.get("sheet") or "", f.get("text") or "", dict(f.get("detail") or {})))
    return out


def _proposal(kind: str, key: str, *, code: str | None, title: str, cells: list[str], target: dict[str, Any],
              options: list[FixOption], anchor: FixAnchor) -> FixProposal:
    return FixProposal(id=fix_id(kind, key), kind=kind, problem_code=code, title=title,
                       cells=[c for c in cells if isinstance(c, str)][:40], target=target, options=options,
                       anchor=anchor)


def _key(kind: str, target: dict[str, Any]) -> str:
    """目标键（3.2）：确认项 fix:<目标键>、提议 id 都由它来。只取目标里的文件原文和配方里的 id。"""
    t = target
    if kind == "remove_label":
        return f"remove_label:{t['segment']}:{'|'.join(t['labels'])}"
    if kind == "add_label":
        return f"add_label:{t['segment']}:{'|'.join(x['raw'] for x in t['labels'])}"
    if kind == "edit_members":
        return f"edit_members:{t['relation']}"
    if kind == "rename_title":
        return f"rename_title:{t['segment']}"
    if kind == "declare_total":
        return f"declare_total:{t['segment']}"
    if kind == "ignore_cells":
        how = t["how"]
        texts = t.get("labels") or t.get("headers") or [r["anchor"] for r in t.get("rows") or []]
        where = t.get("block") if how != "outside" else t.get("sheet")
        return f"ignore_cells:{how}:{where}:{'|'.join(texts)}"
    if kind == "declare_placeholder":
        return f"declare_placeholder:{t['block']}:{'|'.join(t['texts'])}"
    if kind == "declare_hidden":
        return f"declare_hidden:{t['sheet']}:{t['axis']}"
    if kind == "rename_sheet":
        return f"rename_sheet:{t['sheet_id']}"
    raise KeyError(kind)


# ==========================================================================
# ① 移除标签
# ==========================================================================


def _p_remove_label(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    found = find_segment(recipe, args.get("segment"))
    labels = [x for x in args.get("labels") or [] if isinstance(x, str)]
    if found is None or not labels:
        return None
    _si, _bi, _gi, _block, seg = found
    expect = list((seg.get("labels") or {}).get("expect") or [])
    keys = {match_key(x) for x in labels}
    removed = [e for e in expect if e in labels or match_key(e) in keys]
    if not removed or len(removed) == len(expect):
        # 剩下的 expect 为空：分段不能为空，请在配方面板里删掉整个分段
        return None
    measures = seg.get("role") == "measures"
    cols = [seg.get("measures", {}).get(e) for e in removed if seg.get("measures", {}).get(e)]
    if measures:
        detail = (f"表「{seg.get('table')}」不再导入列{quoted(cols)}，这是破坏性变更（按期累积时按退役处理，"
                  f"早期各期保留原值）；今后各期如果又出现{quoted(removed)}，会再次拒收")
    else:
        detail = f"今后各期如果又出现{quoted(removed)}，会再次拒收"
    target = {"segment": seg["id"], "labels": removed}
    return _proposal("remove_label", _key("remove_label", target), code=_get(prob, "code"),
                     title=f"在分段「{seg['id']}」的期望标签中去掉{quoted(removed)}",
                     cells=list(_get(prob, "cells") or []), target=target,
                     options=[FixOption("remove", f"去掉标签{quoted(removed)}", detail=detail, breaking=measures)],
                     anchor=anchor)


def _o_remove_label(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    found = find_segment(recipe, p.target.get("segment"))
    if found is None:
        raise _Stale
    si, bi, gi, _block, seg = found
    sp = ("sheets", si, "blocks", bi, "segments", gi)
    expect = list(seg["labels"]["expect"])
    removed = [e for e in expect if e in p.target["labels"]]
    remaining = [e for e in expect if e not in removed]
    if not removed or not remaining:
        raise _Stale
    ops: list[dict[str, Any]] = [{"op": "replace", "path": ptr(*sp, "labels", "expect"), "value": remaining}]
    summary = [f"分段「{seg['id']}」的期望标签去掉{quoted(removed)}"]
    if seg.get("role") == "measures":
        ti = table_index(recipe, seg.get("table"))
        cols = []
        for e in removed:
            col = seg["measures"].get(e)
            if col is None:
                continue
            cols.append(col)
            ops.append({"op": "remove", "path": ptr(*sp, "measures", e)})
            if ti is not None and col in (recipe["tables"][ti].get("units") or {}):
                ops.append({"op": "remove", "path": ptr("tables", ti, "units", col)})
        if cols:
            summary.append(f"表「{seg.get('table')}」不再导入列{quoted(cols)}")
    return ops, summary


# ==========================================================================
# ② 加入标签
# ==========================================================================


def _new_columns(recipe: dict, seg: dict, raws: list[str]) -> list[tuple[str, str, str | None, str | None]]:
    """measures 段新加的标签 → [(原文, 列名, 词表内单位, 括号原文)]。列名和单位照起草器第 8 条推
    （recipe_suggest._measure_column，框选的分段也用它）：括号里的单位在词表里时列名去掉单位、记单位；不在词表里
    时列名保留原文（「分区丙（千人次）」→「分区丙_千人次」）、不记单位。只按 3.2 ② 的字面去掉括号的话，兄弟列
    都是「人次」时新列看起来同名同口径，却没有单位，确认项也不提示（unit:* 只对有单位的列出）。
    与这张表已有的列 collide_key 撞名时加 _2。位置号与起草器相同（第几个标签 + 1，轴列是第 1 列）。"""
    try:
        cols, _ = derive_tables(Recipe.model_validate(recipe))
    except ValidationError:
        cols = {}
    used = {collide_key(c.name) for c in cols.get(seg.get("table"), []) if c.name}
    start = len((seg.get("labels") or {}).get("expect") or [])
    out = []
    for k, raw in enumerate(raws):
        name, unit, paren = _measure_column(raw, start + k + 2)
        out.append((raw, unique_name(name, used), unit, paren))
    return out


def _column_text(col: str, unit: str | None, paren: str | None) -> str:
    """新列的人话：「新增列「分区丙」，单位「人次」」；括号里的不是词表里的单位时写明没有登记单位。"""
    if unit:
        return f"新增列「{col}」，单位「{unit}」"
    if paren:
        return f"新增列「{col}」（括号里的「{paren}」不在单位词表里，列名保留原文，没有登记单位）"
    return f"新增列「{col}」"


def _p_add_label(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    found = find_segment(recipe, args.get("segment"))
    if found is None:
        return None
    _si, _bi, _gi, _block, seg = found
    role = args.get("role")
    if role is not None and role != seg.get("role"):
        return None
    expect = (seg.get("labels") or {}).get("expect") or []
    have = {match_key(e) for e in expect}
    labels: list[dict[str, Any]] = []
    for item in args.get("labels") or []:
        raw = _get(item, "raw")
        if isinstance(raw, str) and raw.strip() and match_key(raw) not in have:
            have.add(match_key(raw))
            labels.append({"raw": raw.strip(), "cell": _get(item, "cell")})
    if not labels:
        return None
    raws = [x["raw"] for x in labels]
    if seg.get("role") == "measures":
        cols = _new_columns(recipe, seg, raws)
        detail = "；".join(_column_text(col, unit, paren) for _r, col, unit, paren in cols)
        detail += "。今后各期必须有这些标签，没有会拒收；按期累积时早期各期这一列为空值"
    else:
        detail = "今后各期必须有这些标签，没有会拒收"
    target = {"segment": seg["id"], "role": seg.get("role"), "labels": labels}
    return _proposal("add_label", _key("add_label", target), code=_get(prob, "code"),
                     title=f"在分段「{seg['id']}」的期望标签中加入{quoted(raws)}",
                     cells=[x["cell"] for x in labels if x.get("cell")] or list(_get(prob, "cells") or []),
                     target=target, options=[FixOption("add", f"加入标签{quoted(raws)}", detail=detail)],
                     anchor=anchor)


def _o_add_label(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    found = find_segment(recipe, p.target.get("segment"))
    if found is None:
        raise _Stale
    si, bi, gi, _block, seg = found
    sp = ("sheets", si, "blocks", bi, "segments", gi)
    expect = list(seg["labels"]["expect"])
    have = {match_key(e) for e in expect}
    raws = [x["raw"] for x in p.target["labels"] if match_key(x["raw"]) not in have]
    if not raws:
        raise _Stale
    ops: list[dict[str, Any]] = [{"op": "replace", "path": ptr(*sp, "labels", "expect"), "value": expect + raws}]
    summary = [f"分段「{seg['id']}」的期望标签加入{quoted(raws)}"]
    if seg.get("role") == "measures":
        ti = table_index(recipe, seg.get("table"))
        for raw, col, unit, paren in _new_columns(recipe, seg, raws):
            ops.append({"op": "add", "path": ptr(*sp, "measures", raw), "value": col})
            if unit and ti is not None:
                # 满足静态校验第 13 条 unit_label_conflict：原表标签写着单位，列的单位也要写
                ops.append({"op": "add", "path": ptr("tables", ti, "units", col), "value": unit})
            summary.append(f"表「{seg.get('table')}」" + _column_text(col, unit if ti is not None else None, paren))
    return ops, summary


# ==========================================================================
# ③ 编辑恒等式成员
# ==========================================================================

_REL_PATH = re.compile(r"/relations/(\d+)(?:/.*)?")


def _measures_of(recipe: dict, seg_id: Any, labels: list[str]) -> dict | None:
    """Fact 指向的 measures 分段：按 id 找；找不到时按标签找唯一含这些标签的分段（同静态校验 measures_seg）。"""
    segs = [s for *_r, s in iter_segments(recipe) if s.get("role") == "measures"]
    hit = next((s for s in segs if s.get("id") == seg_id), None)
    if hit is not None:
        return hit
    keys = {match_key(x) for x in labels}
    found = [s for s in segs if keys <= {match_key(e) for e in (s.get("labels") or {}).get("expect") or []}]
    return found[0] if len(found) == 1 else None


def _fact_columns(recipe: dict, fact: Fact) -> tuple[str, str, list[str]] | None:
    """sum_eq 事实 → (表, 合计列, 组成列)：标签原文经该 measures 段的映射换成列名，映射不到为 None。"""
    d = fact.detail or {}
    total, parts = d.get("total"), d.get("parts")
    if fact.kind != "sum_eq" or not isinstance(total, str) or not isinstance(parts, list):
        return None
    seg = _measures_of(recipe, d.get("segment"), [total, *[p for p in parts if isinstance(p, str)]])
    if seg is None:
        return None
    by_key = {match_key(e): col for e, col in (seg.get("measures") or {}).items()}
    t = by_key.get(match_key(total))
    ps = [by_key.get(match_key(x)) for x in parts if isinstance(x, str)]
    if t is None or None in ps or len(ps) < 2:
        return None
    return seg.get("table"), t, ps  # type: ignore[return-value]


def _total_col(recipe: dict, table: Any, fact: Fact) -> str | None:
    """sum_eq 事实的合计标签在写入 table 的 measures 段里映射到的列（事实所在分段同 id 的优先）。组成标签映射
    不全也照样算：只用来认「这条事实说的是不是这条关系的合计列」。"""
    d = fact.detail or {}
    total = d.get("total")
    if fact.kind != "sum_eq" or not isinstance(total, str):
        return None
    segs = [s for *_r, s in iter_segments(recipe) if s.get("role") == "measures" and s.get("table") == table]
    for s in sorted(segs, key=lambda x: x.get("id") != d.get("segment")):
        col = {match_key(e): c for e, c in (s.get("measures") or {}).items()}.get(match_key(total))
        if col is not None:
            return col
    return None


def _claimable(recipe: dict, rel: dict, facts: list[Fact]) -> Fact | None:
    """成员映射不全时，这条关系还能认领的本期 sum_eq 事实：
    1. 它原来认领的那条（按编号），合计标签映射到关系的合计列；
    2. 否则同一张表上合计标签映射到同一合计列的 sum_eq，恰好一条（编号按本期的发现顺序排，可能错开）。
    已经被别的关系认领的不算：再认领一次会报 fact_claimed_twice。"""
    others = {r.get("claims") for r in recipe.get("relations") or [] if r is not rel and r.get("claims")}
    ok = [f for f in facts if f.id not in others and _total_col(recipe, rel.get("table"), f) == rel.get("total")]
    own = next((f for f in ok if f.id == rel.get("claims")), None)
    if own is not None:
        return own
    return ok[0] if len(ok) == 1 else None


def _unmapped(recipe: dict, rel: dict, fact: Fact) -> list[str]:
    """事实里在写入这张表的 measures 段里映射不到列的标签（被 ① 去掉的那些）。"""
    have = {match_key(e) for *_r, s in iter_segments(recipe) if s.get("role") == "measures"
            and s.get("table") == rel.get("table") for e in (s.get("measures") or {})}
    d = fact.detail or {}
    labels = [d.get("total"), *(d.get("parts") or [])]
    return [x for x in labels if isinstance(x, str) and match_key(x) not in have]


def _fact_text(fact: Fact) -> str:
    d = fact.detail or {}
    parts = [x for x in d.get("parts") or [] if isinstance(x, str)]
    return f"{d.get('total')} = {' + '.join(parts)}"


def _p_edit_members(recipe: dict, rp: Any, facts: list[Fact], anchor: FixAnchor) -> FixProposal | None:
    code = _get(rp, "code")
    m = _REL_PATH.fullmatch(str(_get(rp, "path") or ""))
    if code not in ("fact_claim_mismatch", "column_unknown") or m is None:
        return None
    rels = recipe.get("relations") or []
    ri = int(m.group(1))
    if ri >= len(rels) or rels[ri].get("kind") != "sum_eq":
        return None
    rel = rels[ri]
    if code == "column_unknown" and not str(_get(rp, "path")).startswith(ptr("relations", ri, "parts")) \
            and _get(rp, "path") != ptr("relations", ri, "total"):
        return None
    by_id = {f.id: f for f in facts}
    fact = by_id.get(rel.get("claims"))
    cols = _fact_columns(recipe, fact) if fact is not None else None
    if cols is None or cols[0] != rel.get("table"):
        # 认领的那条不是能对上的 sum_eq（编号错开、或本期不成立）：找同一张表上合计列相同的那条 sum_eq，恰好一条才用
        hits = [(f, c) for f in facts if (c := _fact_columns(recipe, f)) is not None
                and c[0] == rel.get("table") and c[1] == rel.get("total")]
        fact, cols = hits[0] if len(hits) == 1 else (None, None)
    options: list[FixOption] = []
    manual = "也可以在配方面板中手工修改成员并去掉认领，之后每期照常核对"
    if cols is not None and fact is not None:
        _t, total, parts = cols
        if (total, sorted(parts), fact.id) != (rel.get("total"), sorted(rel.get("parts") or []), rel.get("claims")):
            options.append(FixOption("update", f"按系统发现更新为「{total} = {' + '.join(parts)}」",
                                     detail="今后每期按新的成员核对"))
        options.append(FixOption("dismiss", "不再登记这条关系", needs_reason=True,
                                 detail="今后不再核对这条关系；系统发现记为已说明不登记"))
    elif (fact := _claimable(recipe, rel, facts)) is not None:
        # 本期的事实还在、只是成员映射不全（3.2 ① 的连带：去掉的组成列被这条关系引用）：update 给不出，
        # 「不登记」认领这条事实（3.2 ① 写明这种情形下 ③ 只给「不登记」）。退回「删除这条关系」的话，事实在
        # 改动范围之外时连未认领都不报，核对连同它的系统发现一起不声不响地消失
        gone = _unmapped(recipe, rel, fact)
        why = f"本期的系统发现「{_fact_text(fact)}」含有配方不再导入的标签{quoted(gone)}，无法按它更新成员" if gone \
            else f"本期的系统发现「{_fact_text(fact)}」无法换算成这张表的列"
        options.append(FixOption("dismiss", "不再登记这条关系", needs_reason=True,
                                 detail=f"{why}；不再登记后今后不再核对这条关系，系统发现记为已说明不登记。{manual}"))
    else:
        # 本期没有可以认领的恒等式（D13 加了分项而新恒等式某天不成立时 _sum_holds 不出 sum_eq 事实）。
        # 不登记要认领一条系统发现，这里没有能认领的，只能删掉这条关系；删掉一条数据质量核对要写明理由
        fact = None
        options.append(FixOption("remove", "删除这条关系", needs_reason=True,
                                 detail=f"本期未发现可以替换或认领的恒等式，删除后不再核对这条关系；{manual}"))
    target: dict[str, Any] = {"relation": rel.get("id"), "fact": fact.id if fact else None,
                              "total": cols[1] if cols else None, "parts": list(cols[2]) if cols else []}
    return _proposal("edit_members", _key("edit_members", target), code=code,
                     title=f"关系「{rel.get('id')}」的成员与本期的系统发现对不上", cells=[], target=target,
                     options=options, anchor=anchor)


def _o_edit_members(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    rels = recipe.get("relations") or []
    rid = p.target.get("relation")
    ri = next((i for i, r in enumerate(rels) if r.get("id") == rid), None)
    if ri is None:
        raise _Stale
    rel = rels[ri]
    if opt.value == "remove":
        return [{"op": "remove", "path": ptr("relations", ri)}], [f"删除关系「{rid}」（理由：{reason}）"]
    if opt.value == "dismiss":
        value = {"id": rid, "kind": "dismissed", "claims": p.target["fact"], "reason": reason}
        return [{"op": "replace", "path": ptr("relations", ri), "value": value}], \
            [f"关系「{rid}」不再登记（理由：{reason}）"]
    if rel.get("kind") != "sum_eq":
        raise _Stale
    total, parts, fid = p.target["total"], list(p.target["parts"]), p.target["fact"]
    ops: list[dict[str, Any]] = [{"op": "replace", "path": ptr("relations", ri, "parts"), "value": parts}]
    if rel.get("total") != total:
        ops.append({"op": "replace", "path": ptr("relations", ri, "total"), "value": total})
    if rel.get("claims") != fid:
        ops.append({"op": "add", "path": ptr("relations", ri, "claims"), "value": fid})
    return ops, [f"关系「{rid}」的成员更新为「{total} = {' + '.join(parts)}」"]


# ==========================================================================
# ④ 分段标题改名
# ==========================================================================


def _siblings(recipe: dict, seg: dict) -> list[str]:
    """写进同一张表的其他分段的分段标题（候选词按兄弟标题去掉公共部分，同静态校验 check_const）。"""
    out = []
    for *_r, s in iter_segments(recipe):
        loc = s.get("locate") or {}
        if (s is not seg and s.get("role") == "dimension" and s.get("table") == seg.get("table")
                and loc.get("by") == "section_title" and loc.get("title")):
            out.append(loc["title"])
    return out


def keep_ok(seg: dict, title: str) -> bool:
    """改标题时常量能不能沿用：每个常量的 canon(pick) 都是 canon(新标题) 的子串（3.3 (b)，静态校验据此放行）。"""
    return all(canon(c.get("pick", "")) in canon(title) for c in (seg.get("const") or {}).values())


def title_options(recipe: dict, seg: dict, cell: str, text: str) -> list[FixOption]:
    """一个候选标题的选项：沿用常量（keep_ok 时）和改用候选词（只有一个常量列时，最多 _PICK_OPTIONS_MAX 个）。"""
    consts = seg.get("const") or {}
    opts: list[FixOption] = []
    after = "今后各期只认新标题；改回旧标题时会再次拒收"
    if keep_ok(seg, text):
        kept = "，" + "、".join(f"「{k}」沿用「{v.get('pick')}」" for k, v in consts.items()) if consts else ""
        opts.append(FixOption(f"{cell}|keep", f"改为「{text}」{kept}", detail=after))
    if len(consts) == 1:
        col, c = next(iter(consts.items()))
        words = [w for w in candidate_words(text, _siblings(recipe, seg)) if w != c.get("pick")][:_PICK_OPTIONS_MAX]
        for w in words:
            opts.append(FixOption(f"{cell}|pick:{w}", f"改为「{text}」，「{col}」改用「{w}」",
                                  detail=f"这是破坏性变更：这一列的值从「{c.get('pick')}」变为「{w}」。{after}",
                                  breaking=True))
    return opts


def _p_rename_title(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    found = find_segment(recipe, args.get("segment"))
    if found is None:
        return None
    _si, _bi, _gi, _block, seg = found
    loc = seg.get("locate") or {}
    if loc.get("by") != "section_title" or seg.get("role") == "derived":
        return None
    cands: list[dict[str, str]] = []
    options: list[FixOption] = []
    for item in args.get("candidates") or []:
        cell, text = _get(item, "cell"), _get(item, "text")
        if not isinstance(cell, str) or not isinstance(text, str) or not text.strip() or len(text) > _TITLE_MAX:
            continue
        opts = title_options(recipe, seg, cell, text.strip())
        if opts:
            cands.append({"cell": cell, "text": text.strip()})
            options += opts
    if not options:
        return None
    target = {"segment": seg["id"], "title": loc.get("title"), "candidates": cands}
    return _proposal("rename_title", _key("rename_title", target), code=_get(prob, "code"),
                     title=f"分段「{seg['id']}」的分段标题「{loc.get('title')}」没有找到，可以改用文件里的新标题",
                     cells=[c["cell"] for c in cands], target=target, options=options, anchor=anchor)


def _o_rename_title(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    found = find_segment(recipe, p.target.get("segment"))
    if found is None:
        raise _Stale
    si, bi, gi, _block, seg = found
    sp = ("sheets", si, "blocks", bi, "segments", gi)
    cell, _, how = opt.value.rpartition("|")
    cand = next((c for c in p.target.get("candidates") or [] if c["cell"] == cell), None)
    if cand is None:
        raise _Stale
    text = cand["text"]
    ops: list[dict[str, Any]] = [{"op": "replace", "path": ptr(*sp, "locate", "title"), "value": text}]
    summary = [f"分段「{seg['id']}」的分段标题改为「{text}」"]
    if how.startswith("pick:"):
        consts = seg.get("const") or {}
        if len(consts) != 1:
            raise _Stale
        col = next(iter(consts))
        word = how[len("pick:"):]
        ops.append({"op": "replace", "path": ptr(*sp, "const", col, "pick"), "value": word})
        summary.append(f"「{col}」的值改为「{word}」")
    elif seg.get("const"):
        summary.append("、".join(f"「{k}」沿用「{v.get('pick')}」" for k, v in seg["const"].items()))
    return ops, summary


# ==========================================================================
# ⑤ 声明为合计行（框选的 derived 也用）
# ==========================================================================


def total_blockers(recipe: dict, si: int, bi: int, gi: int) -> str | None:
    """分段能不能接一个合计分段：不能时返回给人看的原因（3.2 ⑤ 的五个前提里与配方有关的三条）。"""
    block = recipe["sheets"][si]["blocks"][bi]
    seg = block["segments"][gi]
    if seg.get("role") != "dimension":
        return f"分段「{seg.get('id')}」不是按维度展开的分段，合计行无法按时段区间核对"
    if sorted((seg.get("dim") or {}).get("derive", {}).values()) != ["end", "start"]:
        return f"分段「{seg.get('id')}」没有派生起始小时和结束小时，合计行无法按时段区间核对"
    if any(s.get("role") == "derived" and (s.get("locate") or {}).get("segment") == seg.get("id")
           for s in block["segments"]):
        return f"分段「{seg.get('id')}」后面已经有合计分段"
    if len(block["segments"]) >= _SEGMENTS_MAX:
        return f"交叉表「{block.get('id')}」的分段已经到上限（{_SEGMENTS_MAX} 个）"
    return None


def total_ops(recipe: dict, si: int, bi: int, gi: int, labels: list[str], keep: bool) -> tuple[list, list[str], str | None]:
    """在分段后面插入合计分段（derived）：(补丁, 摘要, 另存的表名或 None)。

    分段 id = 前一段 id +「合计」，撞了加 _2；另存时同一块里已有 derived 段另存到同一张基表的合计表的，沿用那张表的
    table、dim、derive、value（D18 的日间合计和原来的夜间合计存进同一张「时段客流_表内合计」）；否则新建
    {基表}_表内合计，并在表清单里加上（grain = 轴列 + 合计项，单位取基表值列的单位）。"""
    block = recipe["sheets"][si]["blocks"][bi]
    seg = block["segments"][gi]
    bp = ("sheets", si, "blocks", bi)
    sp = bp + ("segments", gi)
    new_id = unique_id(f"{seg['id']}合计", all_ids(recipe))
    keep_as = None
    table_ops: list[dict[str, Any]] = []
    kept: str | None = None
    if keep:
        same = next((s["keep_as"] for s in block["segments"] if s.get("role") == "derived" and s.get("keep_as")
                     and (s.get("verify") or {}).get("against_table") == seg.get("table")), None)
        if same is not None:
            keep_as = {k: same[k] for k in ("table", "dim", "derive", "value")}
        else:
            used = {collide_key(t.get("name", "")) for t in recipe.get("tables") or []}
            name = unique_name(f"{seg['table']}_表内合计", used)
            keep_as = {"table": name, "dim": "合计项", "derive": dict((seg.get("dim") or {}).get("derive") or {}),
                       "value": seg.get("value")}
            ti = table_index(recipe, seg.get("table"))
            unit = ((recipe["tables"][ti].get("units") or {}).get(seg.get("value")) if ti is not None else None)
            table_ops.append({"op": "add", "path": "/tables/-", "value": {
                "name": name, "grain": [block.get("axis", {}).get("name", "日期"), "合计项"], "kind": "reported_total",
                "units": {seg.get("value"): unit} if unit else {}, "note": ""}})
        kept = keep_as["table"]
    derived = {"id": new_id, "role": "derived", "locate": {"by": "after", "title": None, "segment": seg["id"]},
               "labels_parser": "hour_range_total", "labels": {"expect": list(labels)},
               "verify": {"kind": "label_range_sum", "against_table": seg.get("table"), "value": seg.get("value")},
               "keep_as": keep_as}
    ops = [{"op": "add", "path": ptr(*sp, "stop_parser"), "value": "hour_range_total"},
           {"op": "add", "path": ptr(*bp, "segments", gi + 1), "value": derived}, *table_ops]
    summary = [f"分段「{seg['id']}」遇到合计行就结束",
               f"新增合计分段「{new_id}」：{quoted(labels)}改作合计核对"
               + (f"，原值另存表「{kept}」" if kept else "，不另存")]
    return ops, summary, kept


def _p_declare_total(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    found = find_segment(recipe, args.get("segment"))
    if found is None or args.get("role", "dimension") != "dimension" or not args.get("at_end"):
        return None
    si, bi, gi, _block, seg = found
    labels = list(args.get("labels") or [])
    raws = [_get(x, "raw") for x in labels]
    if not labels or not all(_get(x, "total") for x in labels) or not all(isinstance(r, str) and r for r in raws):
        return None
    if total_blockers(recipe, si, bi, gi) is not None:
        return None
    _ops, _s, kept = total_ops(recipe, si, bi, gi, raws, True)
    target = {"segment": seg["id"], "labels": raws}
    return _proposal("declare_total", _key("declare_total", target), code=_get(prob, "code"),
                     title=f"分段「{seg['id']}」末尾的{quoted(raws)}是合计行",
                     cells=[_get(x, "cell") for x in labels if _get(x, "cell")] or list(_get(prob, "cells") or []),
                     target=target,
                     options=[FixOption("keep", f"改作合计核对，原值另存表「{kept}」",
                                        detail="今后各期必须有这一行，没有会拒收"),
                              FixOption("check_only", "改作合计核对，不另存",
                                        detail="今后各期必须有这一行，没有会拒收；这一行只参与核对，记进回执的「排除的行」")],
                     anchor=anchor)


def _o_declare_total(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    found = find_segment(recipe, p.target.get("segment"))
    if found is None:
        raise _Stale
    si, bi, gi, _block, _seg = found
    if total_blockers(recipe, si, bi, gi) is not None:
        raise _Stale
    ops, summary, _kept = total_ops(recipe, si, bi, gi, list(p.target["labels"]), opt.value == "keep")
    return ops, summary


# ==========================================================================
# ⑥ 忽略（按文字记）
# ==========================================================================


def _split_digits(texts: list[str], limit: int) -> tuple[list[str], list[str]]:
    """(可以作锚点的, 含数字的)。超长的写不进配方，直接不要；按 match_key 去重。"""
    clean: list[str] = []
    digits: list[str] = []
    seen: set[str] = set()
    for t in texts:
        t = t.strip()
        k = match_key(t)
        if not t or k in seen or len(t) > limit:
            continue
        seen.add(k)
        (digits if has_digit(t) else clean).append(t)
    return clean, digits


def _ignore_proposal(target: dict, *, code: Any, cells: list[str], anchor: FixAnchor, label: str,
                     clean: list[str], digits: list[str], extra: str = "") -> FixProposal | None:
    if not clean:
        # 锚点都含数字：不出提议（3.2 ⑥「锚点含数字时不出提议」）。出一个不带选项的提议的话，staging_out 按 anchor
        # 给问题写上 fix_ids，界面照 10.2 放修复按钮，点开却没有可选的处理方式。部分含数字时只给其余的锚点，
        # 含数字的那几个写进选项的 detail（见下）；框选忽略同样拦（selection_anchor_has_digits）
        return None
    detail = "今后各期按这段文字忽略；某一期没有出现时什么也不忽略，也不报错"
    if digits:
        detail += f"。{quoted(digits)}含数字，下一期很可能对不上，不按文字忽略：请修改文件，或把它框选为新的分段"
    if extra:
        detail += f"。{extra}"
    return _proposal("ignore_cells", _key("ignore_cells", target), code=code, title=label, cells=cells,
                     target=target, options=[FixOption("ignore", label, detail=detail, needs_reason=True)],
                     anchor=anchor)


def _p_ignore_cells(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    how = args.get("how")
    code = _get(prob, "code")
    cells = list(_get(prob, "cells") or [])
    if how == "rows":
        found = find_block(recipe, args.get("block"))
        if found is None or found[2].get("layout") != "crosstab":
            return None
        have = {match_key(x.get("label")) for x in found[2].get("ignore_rows") or []}
        raws = [r for x in args.get("labels") or [] if isinstance(r := _get(x, "raw"), str)
                and match_key(r) not in have]
        clean, digits = _split_digits(raws, _ANCHOR_MAX)
        if not clean and not digits:
            return None     # 标签列为空：那是 row_without_label，没有锚点
        target = {"block": found[2]["id"], "how": "rows", "labels": clean or digits}
        return _ignore_proposal(target, code=code, cells=cells, anchor=anchor, clean=clean, digits=digits,
                                label=f"按行标签忽略{quoted(clean)}" + ("这几行" if len(clean) > 1 else "这一行"),
                                extra="被忽略的行记进回执的「排除的行」")
    if how == "columns":
        found = find_block(recipe, args.get("block"))
        if found is None:
            return None
        heads = [_get(x, "raw") for x in args.get("headers") or []]
        if not heads or any(not isinstance(h, str) or not h.strip() for h in heads):
            return None     # 有表头不是文字
        if any(month_day_or_date(h) is not None for h in heads):
            return None     # 能解析成日期：和日期轴冲突
        have = {match_key(x.get("header")) for x in found[2].get("ignore_columns") or []}
        clean, digits = _split_digits([h for h in heads if match_key(h) not in have], _HEADER_MAX)
        if not clean and not digits:
            return None
        target = {"block": found[2]["id"], "how": "columns", "headers": clean or digits}
        return _ignore_proposal(target, code=code, cells=cells, anchor=anchor, clean=clean, digits=digits,
                                label=f"按表头忽略{quoted(clean)}" + ("这几列" if len(clean) > 1 else "这一列"))
    if how == "outside":
        si = sheet_index(recipe, args.get("sheet"))
        if si is None:
            return None
        have = {match_key(x.get("anchor")) for x in recipe["sheets"][si].get("ignore_outside") or []}
        rows = [r for r in args.get("rows") or [] if isinstance(r, dict)]
        texts = [a for r in rows if isinstance(a := r.get("anchor"), str) and match_key(a) not in have]
        clean, digits = _split_digits(texts, _ANCHOR_MAX)
        bare = [r.get("row") for r in rows if not isinstance(r.get("anchor"), str) or not r.get("anchor")]
        if not clean and not digits:
            return None     # 这几格没有可以作锚点的文字，只能修改文件
        lines = {match_key(r["anchor"]): r.get("row") for r in rows if isinstance(r.get("anchor"), str)}
        where = "；".join(f"第 {lines.get(match_key(a))} 行有文字「{a}」" for a in clean)
        target = {"sheet": args.get("sheet"), "how": "outside",
                  "rows": [{"row": lines.get(match_key(a)), "anchor": a} for a in (clean or digits)]}
        extra = ("另有" + "、".join(f"第 {r} 行" for r in bare if r) + "没有可以作锚点的文字，只能修改文件") if bare else ""
        return _ignore_proposal(target, code=code, cells=cells, anchor=anchor, clean=clean, digits=digits,
                                label=f"{where}：忽略{'这些行' if len(clean) > 1 else '这一行'}导入区域之外的数字",
                                extra=extra)
    return None


def _o_ignore_cells(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    t = p.target
    if t["how"] == "outside":
        si = sheet_index(recipe, t.get("sheet"))
        if si is None:
            raise _Stale
        anchors = [r["anchor"] for r in t["rows"]]
        ops = [{"op": "add", "path": ptr("sheets", si, "ignore_outside", "-"), "value": {"anchor": a, "reason": reason}}
               for a in anchors]
        return ops, [f"工作表「{t.get('sheet')}」：同一行有{quoted(anchors)}时，忽略这一行导入区域之外的数字（理由：{reason}）"]
    found = find_block(recipe, t.get("block"))
    if found is None:
        raise _Stale
    si, bi, block = found
    if t["how"] == "rows":
        if block.get("layout") != "crosstab":
            raise _Stale
        ops = [{"op": "add", "path": ptr("sheets", si, "blocks", bi, "ignore_rows", "-"),
                "value": {"label": x, "reason": reason}} for x in t["labels"]]
        return ops, [f"交叉表「{block['id']}」按行标签忽略{quoted(t['labels'])}（理由：{reason}）"]
    ops = [{"op": "add", "path": ptr("sheets", si, "blocks", bi, "ignore_columns", "-"),
            "value": {"header": h, "reason": reason}} for h in t["headers"]]
    return ops, [f"块「{block['id']}」按表头忽略{quoted(t['headers'])}（理由：{reason}）"]


# ==========================================================================
# ⑦ 声明占位符
# ==========================================================================


def _p_declare_placeholder(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    found = find_block(recipe, args.get("block"))
    if found is None:
        return None
    block = found[2]
    have = {canon(x.get("text", "")) for x in (block.get("values") or {}).get("placeholders") or []}
    room = _PLACEHOLDERS_MAX - len(have)
    texts: list[str] = []
    for t in args.get("texts") or []:
        if (isinstance(t, str) and t.strip() and len(t.strip()) <= _PLACEHOLDER_MAX
                and not looks_numeric_text(t) and canon(t) not in have and t.strip() not in texts):
            texts.append(t.strip())
    texts = texts[:max(room, 0)]
    if not texts:
        return None
    target = {"block": block["id"], "texts": texts}
    after = "今后各期这种写法都存为空值"
    return _proposal("declare_placeholder", _key("declare_placeholder", target), code=_get(prob, "code"),
                     title=f"块「{block['id']}」的数据格里有{quoted(texts)}，不是数字",
                     cells=list(_get(prob, "cells") or []), target=target,
                     options=[FixOption("no_data", f"把{quoted(texts)}也声明为无数据（存为空值）", detail=after),
                              FixOption("not_applicable", f"把{quoted(texts)}声明为不适用（存为空值）", detail=after)],
                     anchor=anchor)


def _o_declare_placeholder(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    found = find_block(recipe, p.target.get("block"))
    if found is None:
        raise _Stale
    si, bi, block = found
    meaning = "无数据" if opt.value == "no_data" else "不适用"
    ops = [{"op": "add", "path": ptr("sheets", si, "blocks", bi, "values", "placeholders", "-"),
            "value": {"text": t, "meaning": meaning}} for t in p.target["texts"]]
    return ops, [f"块「{block['id']}」把{quoted(p.target['texts'])}声明为{meaning}（存为空值）"]


# ==========================================================================
# ⑧ 声明隐藏行列怎么处理
# ==========================================================================


def _p_declare_hidden(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    axis = args.get("axis")
    si = sheet_index(recipe, args.get("sheet"))
    if axis not in ("rows", "cols") or si is None:
        return None
    sheet = args.get("sheet")
    filt = "可能是筛选隐藏的行。" if args.get("autofilter") else ""
    if axis == "rows":
        options = [FixOption("include", "隐藏行照常导入", detail=f"{filt}今后各期的隐藏行都照常导入"),
                   FixOption("exclude", "隐藏行不导入", detail=f"{filt}今后各期的隐藏行都跳过，记进回执的「排除的行」")]
        what = "行"
    else:
        options = [FixOption("include", "隐藏列照常导入", detail="今后各期的隐藏列都照常导入")]
        what = "列"
    target = {"sheet": sheet, "axis": axis}
    return _proposal("declare_hidden", _key("declare_hidden", target), code=_get(prob, "code"),
                     title=f"工作表「{sheet}」的数据区里有隐藏的{what}", cells=list(_get(prob, "cells") or []),
                     target=target, options=options, anchor=anchor)


def _o_declare_hidden(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    si = sheet_index(recipe, p.target.get("sheet"))
    if si is None:
        raise _Stale
    return ([{"op": "replace", "path": ptr("sheets", si, "hidden", p.target["axis"]), "value": opt.value}],
            [f"工作表「{p.target.get('sheet')}」：{opt.label}"])


# ==========================================================================
# ⑨ 更新工作表名（P3-SPEC 6.1）
# ==========================================================================


def _sheet_option(name: str) -> FixOption:
    detail = "下一期不再需要确认改名"
    if has_digit(name):
        detail = "新名字里有月份或期次数字，下一期很可能又会改名；可以不更新，每期确认改名即可"
    return FixOption(name, f"把配方里的工作表名更新为「{name}」", detail=detail)


def _p_rename_sheet(recipe: dict, prob: Any, args: dict, anchor: FixAnchor) -> FixProposal | None:
    sheets = recipe.get("sheets") or []
    si = next((i for i, s in enumerate(sheets) if s.get("id") == args.get("sheet_id")), None)
    if si is None:
        return None
    cands = [c for c in args.get("candidates") or [] if isinstance(c, str) and c and len(c) <= _SHEET_MAX]
    if not cands:
        return None
    old = (sheets[si].get("match") or {}).get("name")
    target = {"sheet_id": sheets[si]["id"], "name": old, "candidates": cands}
    return _proposal("rename_sheet", _key("rename_sheet", target), code=_get(prob, "code"),
                     title=f"没有找到工作表「{old}」，可以改用文件里的另一个工作表", cells=list(_get(prob, "cells") or []),
                     target=target, options=[_sheet_option(c) for c in cands], anchor=anchor)


def _p_sheet_renamed(recipe: dict, old: str, new: str) -> FixProposal | None:
    """按 fallback 认出了改名的工作表（Extraction.sheets.renamed）：不是问题，但给提议（6.1 触发一）。"""
    sheets = recipe.get("sheets") or []
    si = next((i for i, s in enumerate(sheets) if match_key((s.get("match") or {}).get("name")) == match_key(old)), None)
    if si is None or not isinstance(new, str) or not new or len(new) > _SHEET_MAX:
        return None
    target = {"sheet_id": sheets[si]["id"], "name": old, "candidates": [new]}
    return _proposal("rename_sheet", _key("rename_sheet", target), code=None,
                     title=f"工作表「{old}」改名为「{new}」", cells=[], target=target, options=[_sheet_option(new)],
                     anchor=FixAnchor("sheet_renamed", None, new))


def _o_rename_sheet(recipe: dict, p: FixProposal, opt: FixOption, reason: str | None) -> tuple[list, list[str]]:
    sheets = recipe.get("sheets") or []
    si = next((i for i, s in enumerate(sheets) if s.get("id") == p.target.get("sheet_id")), None)
    if si is None or opt.value not in p.target.get("candidates", []):
        raise _Stale
    return ([{"op": "replace", "path": ptr("sheets", si, "match", "name"), "value": opt.value}],
            [f"配方里的工作表名「{p.target.get('name')}」更新为「{opt.value}」"])


# ==========================================================================
# 入口
# ==========================================================================

_Builder = Callable[[dict, Any, dict, FixAnchor], "FixProposal | None"]
_BUILDERS: dict[str, _Builder] = {
    "remove_label": _p_remove_label, "add_label": _p_add_label, "rename_title": _p_rename_title,
    "declare_total": _p_declare_total, "ignore_cells": _p_ignore_cells, "declare_placeholder": _p_declare_placeholder,
    "declare_hidden": _p_declare_hidden, "rename_sheet": _p_rename_sheet,
}
_OPS: dict[str, Callable[[dict, FixProposal, FixOption, str | None], tuple[list, list[str]]]] = {
    "remove_label": _o_remove_label, "add_label": _o_add_label, "edit_members": _o_edit_members,
    "rename_title": _o_rename_title, "declare_total": _o_declare_total, "ignore_cells": _o_ignore_cells,
    "declare_placeholder": _o_declare_placeholder, "declare_hidden": _o_declare_hidden, "rename_sheet": _o_rename_sheet,
}


def propose_fixes(recipe: dict[str, Any], problems: list[dict[str, Any]], recipe_problems: list[dict[str, Any]],
                  facts: dict[str, Any] | DraftFacts | None,
                  renamed: dict[str, str] | None = None) -> list[FixProposal]:
    """问题 → 修复提议（纯函数，不抛异常）。

    - problems：执行器的问题（Problem 的 JSON，或 Problem 本身）。按 fix 字段分派、按 fix_args 构造；没有 fix_args
      的不出提议（界面不能光靠 fix 字符串构造补丁）；
    - recipe_problems：静态校验的问题。fact_claim_mismatch（以及关系成员列不存在的 column_unknown）出 ③；
    - facts：本期的系统发现（DraftFacts 或它的 JSON），③ 的成员只能取自这里；
    - renamed：试运行的 Extraction.sheets.renamed（配方名 → 实际名），出 ⑨。
    每个提议带 anchor：index 是该问题在传进来的 problems / recipe_problems 里的下标，staging_out 据此把 fix_ids
    写到对应的问题上。同样的目标只出一个提议（按 id 去重，anchor 取第一条）。"""
    if not isinstance(recipe, dict):
        return []
    out: list[FixProposal] = []
    seen: set[str] = set()

    def push(p: FixProposal | None) -> None:
        if p is not None and p.id not in seen:
            seen.add(p.id)
            out.append(p)

    for i, prob in enumerate(problems or []):
        args = _get(prob, "fix_args")
        builder = _BUILDERS.get(_get(prob, "fix") or "")
        if builder is None or not isinstance(args, dict):
            continue
        try:
            push(builder(recipe, prob, args, FixAnchor("problem", i)))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue    # fix_args 的形状不对（执行器的契约问题）：不出提议，不拖垮 staging_out
    fl = _facts(facts)
    for i, rp in enumerate(recipe_problems or []):
        try:
            push(_p_edit_members(recipe, rp, fl, FixAnchor("recipe_problem", i)))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue
    for old, new in (renamed or {}).items():
        push(_p_sheet_renamed(recipe, old, new))
    return out


def _edit_title(p: FixProposal, opt: FixOption) -> str:
    """EditResult.title 是确认项 fix:<目标键> 的文案，要写明选了哪一项（选项的 label 加上主语）。"""
    t = p.target
    if p.kind == "edit_members":
        return f"关系「{t.get('relation')}」：{opt.label}"
    if p.kind == "rename_title":
        return f"分段「{t.get('segment')}」的分段标题{opt.label}"
    if p.kind == "declare_total":
        return f"分段「{t.get('segment')}」末尾的{quoted(t.get('labels') or [])}{opt.label}"
    if p.kind == "declare_placeholder":
        return f"块「{t.get('block')}」{opt.label}"
    if p.kind == "declare_hidden":
        return f"工作表「{t.get('sheet')}」：{opt.label}"
    if p.kind == "rename_sheet":
        return opt.label
    return p.title


def fix_ops(recipe: dict[str, Any], proposal: FixProposal, option: str, reason: str | None) -> EditResult:
    """选中的选项 → EditResult（补丁、人话摘要、修改后的配方哈希）。

    选项不在 proposal.options 里、需要理由而没给（或超长）时抛 EditRequestError（code 见该类）。提议的目标在
    当前工作配方里已经找不到时返回 ok=False（problems 写原因）。理由去掉首尾空白后写进配方，进配方哈希：预览和
    应用必须用同一个理由（评审三-B1）。"""
    opt = next((o for o in proposal.options if o.value == option), None)
    if opt is None:
        raise EditRequestError("edit_invalid", "所选的处理方式不在这条修复建议的选项里，请重新打开修复建议")
    text = reason.strip() if isinstance(reason, str) else ""
    if opt.needs_reason:
        if not text:
            raise EditRequestError("reason_required", "这个选项需要写明理由")
        if len(text) > REASON_MAX:
            raise EditRequestError("edit_invalid", f"理由不能超过 {REASON_MAX} 字")
    key = _key(proposal.kind, proposal.target)
    title = _edit_title(proposal, opt)
    try:
        ops, summary = _OPS[proposal.kind](recipe, proposal, opt, text if opt.needs_reason else None)
    except (_Stale, KeyError, TypeError, IndexError):
        msg = "这条修复建议对应的内容已不在当前的配方里：配方在提出建议之后又被改过。请重新查看问题和修复建议"
        return EditResult(ok=False, kind="fix", key=key, title=title, summary=[], ops=[], anchors=[], notes=[],
                          problems=[Problem("edit_not_applicable", "recipe", msg, model_message="edit_not_applicable")])
    return finish(recipe, ops, kind="fix", key=key, title=title, summary=summary, breaking=opt.breaking)


def proposal_from_dict(data: dict[str, Any]) -> FixProposal:
    """FixProposal 的 JSON（asdict）→ dataclass。WP-5 把提议存进响应、按 fix_id 找回来时用。"""
    opts = [FixOption(**{k: v for k, v in o.items() if k in {f.name for f in dataclasses.fields(FixOption)}})
            for o in data.get("options") or []]
    a = data.get("anchor") or {}
    return FixProposal(id=data["id"], kind=data["kind"], problem_code=data.get("problem_code"), title=data["title"],
                       cells=list(data.get("cells") or []), target=dict(data.get("target") or {}), options=opts,
                       anchor=FixAnchor(a.get("kind", "problem"), a.get("index"), a.get("sheet")))
