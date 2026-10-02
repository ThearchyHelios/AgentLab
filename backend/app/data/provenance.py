"""证据下钻（期 4「推断的来源」）的清单与格子：WP-B（P4-SPEC 2.2、2.5、2.7、3.2、7.3）。

只做纯函数：输入是已经取回的查询快照、冻结的表结构快照，以及一个按内容哈希取工件的 loader（语义同
artifact_store.load：取不到返回 None，哈希不符抛 ValueError）。不连库、不读原件、不看当前时间，所以：
- 推断来源接口（WP-C）拿它算「这一格来自原表的哪一格」，回查数据文件、补当前状态都在接口那一侧；
- 裁判摘录拿 judge_lines 生成来历行，同样的工件永远得到同样的字（摘录进断点指纹，P4-SPEC 0.2 补充 3）。

**为什么一律沿内容哈希走**（0.2 补充 2）：查询快照 → 冻结的表结构快照（import_manifests、snapshot_manifest）→
导入清单，每一跳都是内容寻址的工件，挂在封存范围内的表结构快照下面，改不了。数据库里的 manifest_artifact 列可以
被改，这里一次都不读。

**为什么宁可不下钻也不猜**（2.5）：面板上给出的格子是让人去原表对照的，指错了比不指更糟。所以凡是要「猜」的地方
（锚格不在任何值区域里、同一块匹配到不止一条轴、期 2 的清单在一张工作表上有两块、常量列找不到分段标题……）一律判
no_lineage，只给表级来历。

不 import app.api、app.engine.judge、app.engine.direct_select（7.3）：SQL 判据由调用方算好传进来（tables），
这里不依赖识别器。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.data.names import name_key
from app.data.provenance_types import (
    PROV_ACCEPT,
    PROV_CHARS,
    PROV_COLUMN_NOTES,
    PROV_OUTSIDE,
    PROV_OUTSIDE_CHARS,
    PROV_PARTS,
    AcceptanceView,
    CanonicalView,
    CellSource,
    Chain,
    ChainPart,
    ChainProblem,
    FromCell,
    Loader,
    Located,
    LocateProblem,
    PartView,
    PeriodView,
    RelatedCheck,
    RelatedCheckPlan,
    SumEqRule,
    TableRef,
    VersionView,
    YearSource,
)
from app.data.recipe_checks import col_letters, lineage_cell, plan_checks, split_coord
from app.data.recipe_types import (
    CrosstabBlock,
    ListBlock,
    NotComparable,
    Recipe,
    SumEq,
)

_log = logging.getLogger(__name__)

__all__ = ["chain_artifacts", "resolve_chain", "version_view", "locate", "related_checks", "judge_lines"]

#: 锚格所在的区域：值格、合计值格（交叉表合计行、列表合计行）。锚格的块取包含它的这类区域的 block
_VALUE_ROLES = frozenset({"value", "derived_value", "total_value"})
#: 行标签一类的区域：行标签、交叉表合计行的标签（回执里是 derived_label）、列表合计行的标签
_LABEL_ROLES = frozenset({"row_label", "derived_label", "total_label"})
#: 区域 role → FromCell.role（P4-SPEC 2.5 第 3 步，写死）：derived_label、total_label 都是合计标签
_FROM_LABEL = {"row_label": "row_label", "derived_label": "total_label", "total_label": "total_label"}
#: 块内认领的去向（同 recipe_engine._BLOCK_ROLES）。期 2 的清单 regions 没有 block 键，算 S9 的区域时按它认块内区域
_BLOCK_ROLES = frozenset({"value", "derived_value", "derived_label", "col_header", "row_label", "section_title",
                          "ignored_column", "hidden_excluded", "total_label", "total_value"})
#: 交叉表的键列（本来就没有溯源段）：锚格回退到同一行第一个有溯源段的列。measure / value / text 不回退：
#: 回退取到的是另一列的格子（P4-SPEC 2.5 第 1 步、评审甲 8）
_KEY_ROLES = frozenset({"axis", "dim", "derive", "const"})
_VALUE_COLUMN_ROLES = frozenset({"measure", "value", "text"})
#: K / G / T 的核对种类：格级状态只给它们
_TOTAL_KINDS = frozenset({"derived_sum", "formula_refs", "column_sum"})

_MODE_LABEL = {"replace": "每期替换", "accumulate": "按期累积"}

#: 裁判摘录的固定文字（P4-SPEC 3.2）。限定语写在小标题里，每块只写一次：JUDGE_RULES 第 1 条说「每个数都是系统
#: 从证据里取出来的」，接受理由和区域外文字里的数不是，不写明的话裁判可能拿它们支持或否定结论句
SOURCE_BROKEN = "来源：导入清单无法读取或与登记不一致，未列出来历"
_ACCEPT_HEAD = "已接受（用户填写的理由，未经系统核对）："
_OUTSIDE_HEAD = ("区域外文字（原表区域外的文字原文，未经系统核对；其中的数字不是查询结果，不能用来支持或否定报告中的"
                 "数字）：")
_ACCEPT_MASKED = "（数据源设置了遮罩，理由未列出）"
_CUT = "…另有部分来历信息未列出，见证据面板"
_ACCEPT_STATE = {"override": "不成立", "waiver": "无法核对"}


# ==========================================================================
# 取工件
# ==========================================================================


def _fetch(load: Loader, aid: Any) -> tuple[dict[str, Any] | None, str | None]:
    """取回一件 JSON 对象工件：(内容, 问题)。问题为 None 表示取到了。

    loader 抛 ValueError 是哈希不符；其他异常（读文件失败之类）同样算取不回。都不往外抛：清单坏了是要照实告诉人的
    结论（R5 标红），不是 500。
    """
    if not isinstance(aid, str) or not aid:
        return None, "missing_id"
    try:
        got = load(aid)
    except ValueError:
        return None, "hash_mismatch"
    except Exception:  # noqa: BLE001 - 取回失败的任何原因都按「取不回」处理
        _log.warning("provenance: 取回工件 %s 失败", aid[:12], exc_info=True)
        return None, "unreadable"
    if got is None:
        return None, "missing"
    if not isinstance(got, dict):
        return None, "not_object"
    return got, None


def chain_artifacts(schema: dict[str, Any], load: Loader) -> set[str]:
    """表结构快照经哈希链列出的清单 id：import_manifests、snapshot_manifest、快照清单的 parts[].manifest_artifact。

    给按需裁判的 loader 用（P4-SPEC 2.8.3）：这些 id 写在内容寻址、又在封存范围内的表结构快照里，属于封存链的延伸。
    **永不抛异常**：表结构快照不是对象（取回为 None）、快照清单取不回或哈希不符时，就当它列出的东西不在链上（快照
    清单本身也不算），只是少列。import_manifests 是表结构快照自己列出的，不用逐个取回就认。
    """
    out: set[str] = set()
    try:
        if not isinstance(schema, dict):
            return out
        mids = schema.get("import_manifests")
        if isinstance(mids, list):
            out.update(m for m in mids if isinstance(m, str) and m)
        sm_id = schema.get("snapshot_manifest")
        if isinstance(sm_id, str) and sm_id:
            sm, _ = _fetch(load, sm_id)
            if sm is not None:
                out.add(sm_id)
                parts = sm.get("parts")
                for p in parts if isinstance(parts, list) else []:
                    mid = p.get("manifest_artifact") if isinstance(p, dict) else None
                    if isinstance(mid, str) and mid:
                        out.add(mid)
    except Exception:  # noqa: BLE001 - 坏形状的表结构快照只会让链变短，不能让按需裁判报错
        _log.warning("provenance: 列出链上的清单失败", exc_info=True)
    return out


# ==========================================================================
# 清单链（P4-SPEC 2.2）
# ==========================================================================


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _recipe_mode(manifest: dict[str, Any]) -> str:
    """单期的 mode 取配方的 mode（规范形式省掉缺省值，没写就是 replace，同 Recipe.mode 的缺省）。"""
    canonical = (manifest.get("recipe") or {}).get("canonical") if isinstance(manifest.get("recipe"), dict) else None
    mode = canonical.get("mode") if isinstance(canonical, dict) else None
    return "accumulate" if mode == "accumulate" else "replace"


def _union_rows(rows: Any) -> dict[str, list[int]] | None:
    """快照清单 parts[].rows（表 → {union: [起, 止], part: [起, 止]}）摊平成 表 → [union 起, 止, part 起, 止]。
    形状不对返回 None（链本身对不上）。"""
    if not isinstance(rows, dict):
        return None
    out: dict[str, list[int]] = {}
    for table, r in rows.items():
        u = r.get("union") if isinstance(r, dict) else None
        p = r.get("part") if isinstance(r, dict) else None
        if not (isinstance(u, list) and isinstance(p, list) and len(u) == 2 and len(p) == 2
                and all(_is_int(x) for x in [*u, *p])):
            return None
        out[str(table)] = [u[0], u[1], p[0], p[1]]
    return out


def resolve_chain(query: dict[str, Any], schema: dict[str, Any], load: Loader) -> Chain | ChainProblem:
    """查询快照 + 冻结的表结构快照 → 清单链（P4-SPEC 2.2）。

    先取回表结构快照列出的全部清单（R5：任何一份取不回或哈希不符 → manifest_unreadable），再核对链是否自洽（R6：
    只看内容寻址的工件就能判的几项 → chain_mismatch）。要对照数据库、数据文件的（source_id、登记哈希，R13）由
    调用方拿 Chain.source_id、expected_db_sha256 去比。不往外抛。

    单期（没有 snapshot_manifest）**不比快照 id**：移除、作废之后只剩一期时，冻结的 import_manifests 沿用那一期
    原来的清单（source_versions.py 的 schema_cache_for），清单里的 snapshot_id 是当初提交时的快照，不是现在这个。
    并集比：快照清单的 snapshot_id 等于查询快照的 data_version；parts[].manifest_artifact 逐个等于冻结的
    import_manifests；每期清单的 import_id、seq、source_id 与快照清单记的一致。
    """
    version = query.get("data_version") if isinstance(query, dict) else None
    source = str((query.get("source") if isinstance(query, dict) else None) or schema.get("source") or "")
    mids = schema.get("import_manifests")
    sm_id = schema.get("snapshot_manifest") or None
    if not isinstance(mids, list):
        return ChainProblem("chain_mismatch", "import_manifests_shape")
    # R5：先全部取回、复验哈希。顺序固定（快照清单在前），同一个 id 只取一次
    loaded: dict[str, dict[str, Any]] = {}
    for aid in ([sm_id] if sm_id is not None else []) + list(mids):
        if isinstance(aid, str) and aid in loaded:
            continue
        doc, problem = _fetch(load, aid)
        if doc is None:
            return ChainProblem("manifest_unreadable", f"{problem}:{str(aid)[:12]}")
        loaded[aid] = doc
    if not isinstance(version, str) or not version:
        return ChainProblem("chain_mismatch", "no_data_version")
    # R6：链本身是否自洽
    if sm_id is None:
        if len(mids) != 1:
            return ChainProblem("chain_mismatch", "import_manifests_count")
        m = loaded[mids[0]]
        expected, source_id = m.get("db_sha256"), m.get("source_id")
        if not isinstance(expected, str) or not expected:
            return ChainProblem("chain_mismatch", "db_sha256_missing")
        if not isinstance(source_id, str) or not source_id:
            return ChainProblem("chain_mismatch", "source_id_missing")
        if not _is_int(m.get("seq")) or not isinstance(m.get("import_id"), str):
            return ChainProblem("chain_mismatch", "part_identity")
        part = ChainPart(m["seq"], m["import_id"], mids[0], m, None)
        return Chain(source, source_id, version, _recipe_mode(m), False, expected, [part], schema, None)
    sm = loaded[sm_id]
    if sm.get("snapshot_id") != version:
        return ChainProblem("chain_mismatch", "snapshot_id")
    parts = sm.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        return ChainProblem("chain_mismatch", "parts_shape")
    if [p.get("manifest_artifact") for p in parts] != list(mids):
        return ChainProblem("chain_mismatch", "parts_vs_import_manifests")
    union = sm.get("union") if isinstance(sm.get("union"), dict) else {}
    expected, source_id = union.get("db_sha256"), sm.get("source_id")
    if not isinstance(expected, str) or not expected:
        return ChainProblem("chain_mismatch", "db_sha256_missing")
    if not isinstance(source_id, str) or not source_id:
        return ChainProblem("chain_mismatch", "source_id_missing")
    out: list[ChainPart] = []
    for p in parts:
        m = loaded[p["manifest_artifact"]]
        if m.get("import_id") != p.get("import_id") or m.get("seq") != p.get("seq") \
                or not _is_int(p.get("seq")) or not isinstance(p.get("import_id"), str):
            return ChainProblem("chain_mismatch", "part_identity")
        if m.get("source_id") != source_id:
            return ChainProblem("chain_mismatch", "source_id")
        rows = _union_rows(p.get("rows"))
        if rows is None:
            return ChainProblem("chain_mismatch", "part_rows")
        out.append(ChainPart(p["seq"], p["import_id"], p["manifest_artifact"], m, rows))
    mode = sm.get("mode") if sm.get("mode") in _MODE_LABEL else "accumulate"
    return Chain(source, source_id, version, mode, True, expected, out, schema, sm)


# ==========================================================================
# 数据版本（表级来历，P4-SPEC 2.8.1 的 version）
# ==========================================================================


def _rect(ref: Any) -> tuple[int, int, int, int] | None:
    """「C5:AG7」或「B9」→ (c1, r1, c2, r2)；认不出返回 None。"""
    if not isinstance(ref, str) or not ref:
        return None
    a, _, b = ref.partition(":")
    pa, pb = split_coord(a), split_coord(b or a)
    if pa is None or pb is None:
        return None
    return min(pa[1], pb[1]), min(pa[2], pb[2]), max(pa[1], pb[1]), max(pa[2], pb[2])


def _a1(r: int, c: int) -> str:
    return f"{col_letters(c)}{r}"


def _str(x: Any) -> str | None:
    return x if isinstance(x, str) and x else None


def _receipt(manifest: dict[str, Any]) -> dict[str, Any]:
    r = manifest.get("receipt")
    return r if isinstance(r, dict) else {}


def _period(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """这一期的统计期：清单顶层的 period（与回执里的同一份），缺了退回回执里的。"""
    per = manifest.get("period")
    if not isinstance(per, dict):
        per = _receipt(manifest).get("period")
    return per if isinstance(per, dict) else None


def region_of(receipt: dict[str, Any]) -> str | None:
    """S9（P4-SPEC 调整 9）：回执 regions 里块内各角色区域的外接矩形，按工作表，多张工作表用「、」连。

    块内 = 区域带了 block（期 3 起的回执一律带 block 键，区域外文字、统计期为 None）；期 2 的回执没有 block 键，
    按块内认领的去向认。取不到为 None。
    """
    boxes: dict[str, list[int]] = {}
    for m in receipt.get("regions") or []:
        if not isinstance(m, dict) or not isinstance(m.get("sheet"), str):
            continue
        inside = bool(m.get("block")) if "block" in m else m.get("role") in _BLOCK_ROLES
        rect = _rect(m.get("ref")) if inside else None
        if rect is None:
            continue
        b = boxes.setdefault(m["sheet"], list(rect))
        b[0], b[1], b[2], b[3] = min(b[0], rect[0]), min(b[1], rect[1]), max(b[2], rect[2]), max(b[3], rect[3])
    if not boxes:
        return None
    return "、".join(f"{s}!{_a1(b[1], b[0])}:{_a1(b[3], b[2])}" for s, b in boxes.items())


def excluded_rows_of(receipt: dict[str, Any]) -> int | None:
    """S9：回执 rows_excluded 的行数之和；期 3 之前的回执没有这个键时为 None（「未记录」不等于 0）。"""
    if "rows_excluded" not in receipt:
        return None
    total = 0
    for item in receipt.get("rows_excluded") or []:
        for span in (item.get("rows") if isinstance(item, dict) else None) or []:
            if isinstance(span, list) and len(span) == 2 and _is_int(span[0]) and _is_int(span[1]):
                total += span[1] - span[0] + 1
    return total


def _check_titles(manifest: dict[str, Any]) -> dict[str, str]:
    return {str(c["id"]): str(c.get("title") or c["id"]) for c in manifest.get("checks") or []
            if isinstance(c, dict) and c.get("id")}


def acceptances_of(manifest: dict[str, Any]) -> list[AcceptanceView]:
    """这一期清单里的全部接受理由：先 overrides（数据质量类不成立），再 waivers（无法核对），各按清单里的顺序。"""
    titles = _check_titles(manifest)
    acc = manifest.get("acceptances") if isinstance(manifest.get("acceptances"), dict) else {}
    out: list[AcceptanceView] = []
    for kind, key in (("override", "overrides"), ("waiver", "waivers")):
        for a in acc.get(key) or []:
            if not isinstance(a, dict) or not a.get("check_id"):
                continue
            cid = str(a["check_id"])
            out.append(AcceptanceView(check_id=cid, title=titles.get(cid), kind=kind,  # type: ignore[arg-type]
                                      reason=str(a.get("reason") or ""), signed_by=_str(a.get("signed_by")),
                                      at=_str(a.get("at"))))
    return out


def _period_view(manifest: dict[str, Any]) -> PeriodView | None:
    per = _period(manifest)
    if per is None or not per.get("start") or not per.get("end"):
        return None
    return PeriodView(start=str(per["start"]), end=str(per["end"]),
                      source="human" if per.get("source") == "human" else "cells",
                      cells=[str(c) for c in per.get("cells") or []], signed_by=_str(per.get("signed_by")))


def _part_view(part: ChainPart) -> PartView:
    m = part.manifest
    f = m.get("file") if isinstance(m.get("file"), dict) else {}
    recipe = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    signed = m.get("signed_by") if isinstance(m.get("signed_by"), dict) else {}
    receipt = _receipt(m)
    return PartView(
        import_id=part.import_id, seq=part.seq, manifest=part.manifest_id, period=_period_view(m),
        file_name=_str(f.get("name")), raw_sha256=_str(f.get("raw_sha256")), region=region_of(receipt),
        excluded_rows=excluded_rows_of(receipt), recipe_sha256=_str(recipe.get("sha256")), recipe_seq=None,
        committed_at=_str(m.get("created_at")), signed_by=_str(signed.get("name")), acceptances=acceptances_of(m))


def _frozen_tables(schema: dict[str, Any]) -> dict[str, Any]:
    tables = schema.get("tables") if isinstance(schema, dict) else None
    return tables if isinstance(tables, dict) else {}


def _frozen_name(frozen: dict[str, Any], name: str) -> str | None:
    """按 name_key 对冻结表结构的表名（原样逐字相同的优先）。"""
    if name in frozen:
        return name
    key = name_key(name)
    return next((t for t in frozen if name_key(t) == key), None)


def _table_kind(frozen: dict[str, Any], name: str) -> str:
    meta = frozen.get(name)
    return "reported_total" if isinstance(meta, dict) and meta.get("kind") == "reported_total" else "data"


def version_view(chain: Chain, *, tables: list[str]) -> VersionView:
    """数据版本（表级来历）：全部来自清单链上的内容寻址工件。

    tables 由调用方用 direct_select.tables_in 算好（SQL 里出现的冻结表结构的表名），这里与冻结表结构按 name_key
    求交集、按出现顺序去重。recipe_seq、raw_state、purged、revoked 是当前状态，留 None 由接口一侧从数据库补；
    has_row 全 False，由接口一侧按格子所在的一期标；manifest_view 一律 True，遮罩由接口一侧判断。
    """
    frozen = _frozen_tables(chain.schema)
    refs: list[TableRef] = []
    seen: set[str] = set()
    for name in tables:
        hit = _frozen_name(frozen, str(name))
        if hit is None or hit in seen:
            continue
        seen.add(hit)
        refs.append(TableRef(name=hit, kind=_table_kind(frozen, hit)))  # type: ignore[arg-type]
    return VersionView(source=chain.source, snapshot_id=chain.snapshot_id, mode=chain.mode,  # type: ignore[arg-type]
                       union=chain.union, tables=refs, manifest_view=True,
                       parts=[_part_view(p) for p in chain.parts])


# ==========================================================================
# 从 (表, 列, rowid) 找回格子（P4-SPEC 2.5）
# ==========================================================================


@dataclass(frozen=True)
class _Mark:
    """回执里的一个区域标记，坐标已解析。keyed：回执里有没有 block 键（期 2 的清单没有）。"""

    sheet: str
    role: str
    block: str | None
    keyed: bool
    c1: int
    r1: int
    c2: int
    r2: int

    def has(self, r: int, c: int) -> bool:
        return self.r1 <= r <= self.r2 and self.c1 <= c <= self.c2

    def in_block(self, block: str) -> bool:
        # 期 2 的清单没有 block 键：能走到这里，说明这张工作表上配方只有一块（_anchor_block），按工作表认
        return self.block == block if self.keyed else True


def _marks(receipt: dict[str, Any]) -> list[_Mark]:
    out: list[_Mark] = []
    for m in receipt.get("regions") or []:
        if not isinstance(m, dict) or not isinstance(m.get("sheet"), str):
            continue
        rect = _rect(m.get("ref"))
        if rect is None:
            continue
        block = m.get("block")
        out.append(_Mark(m["sheet"], str(m.get("role") or ""), block if isinstance(block, str) else None,
                         "block" in m, *rect))
    return out


def _sheet_blocks(recipe: Recipe, receipt: dict[str, Any]) -> list[tuple[str, Any]]:
    """(实际工作表名, 块)：配方工作表 id 经回执的 sheets.matched 换成实际的工作表名（没认到的退回配方里的名字）。"""
    sheets = receipt.get("sheets") if isinstance(receipt.get("sheets"), dict) else {}
    matched = sheets.get("matched") if isinstance(sheets.get("matched"), dict) else {}
    out: list[tuple[str, Any]] = []
    for sr in recipe.sheets:
        actual = matched.get(sr.id) or sr.match.name
        out += [(str(actual), b) for b in sr.blocks]
    return out


def _anchor_block(marks: list[_Mark], sheet: str, r: int, c: int, recipe: Recipe,
                  receipt: dict[str, Any]) -> str | None:
    """锚格所在的块：包含锚格的值区域（value / derived_value / total_value）的 block。

    期 2 生成的清单 regions 没有 block 键：同一工作表上配方恰好只有一块时认它，否则不认（不猜）。找不到包含锚格的
    值区域、或者落在两块的区域里，都返回 None。
    """
    hits = [m for m in marks if m.sheet == sheet and m.role in _VALUE_ROLES and m.has(r, c)]
    if not hits:
        return None
    if all(m.keyed for m in hits):
        blocks = {m.block for m in hits}
        return blocks.pop() if len(blocks) == 1 and None not in blocks else None
    on_sheet = [b for s, b in _sheet_blocks(recipe, receipt) if s == sheet]
    return on_sheet[0].id if len(on_sheet) == 1 else None


def _find_block(recipe: Recipe, receipt: dict[str, Any], sheet: str, block_id: str) -> Any | None:
    on_sheet = [b for s, b in _sheet_blocks(recipe, receipt) if s == sheet and b.id == block_id]
    if len(on_sheet) == 1:
        return on_sheet[0]
    anywhere = [b for _, b in _sheet_blocks(recipe, receipt) if b.id == block_id]
    return anywhere[0] if len(anywhere) == 1 else None


def _table_out(receipt: dict[str, Any], table: str) -> dict[str, Any] | None:
    hits = [t for t in receipt.get("tables") or [] if isinstance(t, dict) and t.get("name") == table]
    return hits[0] if len(hits) == 1 else None


def _columns(table_out: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in table_out.get("columns") or [] if isinstance(c, dict) and isinstance(c.get("name"), str)]


def _derived_at(receipt: dict[str, Any], coord: str) -> dict[str, Any] | None:
    """合计格（DerivedItem）里 cell 等于这一格的那一项：合计标签原文取它的 label_raw，规范写法取 label。"""
    for d in receipt.get("derived") or []:
        if isinstance(d, dict) and d.get("cell") == coord:
            return d
    return None


@dataclass
class _Ctx:
    """locate 一次要用到的东西（都来自格子所在那一期的导入清单）。"""

    chain: Chain
    part: ChainPart
    part_rowid: int
    rowid: int
    table: str
    column: str
    role: str
    meta: dict[str, Any]
    table_out: dict[str, Any]
    columns: list[dict[str, Any]]
    receipt: dict[str, Any]
    recipe: Recipe
    marks: list[_Mark]
    sheet: str
    ar: int
    ac: int
    block_id: str


def _no(detail: str) -> LocateProblem:
    return LocateProblem("no_lineage", detail)


def _part_of(chain: Chain, table: str, rowid: int) -> tuple[ChainPart, int] | LocateProblem:
    """快照库的 rowid → (格子所在的一期, 该期构建库的 rowid)。

    单期：快照库就是那一期的构建库，rowid 直接是溯源段的 rowid。并集：rowid u 落在某一期 rows[表].union = [a, b]
    里，该期的 rowid = u - a + rows[表].part[0]（part[0] 恒为 1，物化时已断言）。落不进任何一期、或那一期没有这张
    表：链和数据文件对不上，chain_mismatch（标红，R17）。
    """
    if not chain.union:
        return chain.parts[0], rowid
    hits = [(p, rows) for p in chain.parts if (rows := (p.union_rows or {}).get(table)) is not None
            and rows[0] <= rowid <= rows[1]]
    if len(hits) != 1:
        return LocateProblem("chain_mismatch", "rowid_out_of_parts" if not hits else "rowid_in_two_parts")
    part, rows = hits[0]
    return part, rowid - rows[0] + rows[2]


def locate(chain: Chain, *, table: str, column: str, rowid: int) -> Located | LocateProblem:
    """(表, 列, 快照库的 rowid) → 原表的格子和附带的格（P4-SPEC 2.5）。

    并集时 rowid 是并集 rowid，先换算成某一期的 rowid（_part_of）。之后全部用那一期导入清单的回执：溯源段给锚格，
    区域标记、轴行、标签原文、合计格给附带的格，配方（解析后的模型，规范形式会省掉缺省值）给分段标题和年份规则。

    CellSource 的 pk、recheck、raw_purged 留给接口一侧（WP-C）回查后填；rowid 填调用方给的快照库 rowid（就是回查
    得到的那一个）。
    """
    if not _is_int(rowid) or rowid < 1:
        return _no("rowid")
    got = _part_of(chain, table, rowid)
    if isinstance(got, LocateProblem):
        return got
    part, part_rowid = got
    m = part.manifest
    receipt = _receipt(m)
    canonical = (m.get("recipe") or {}).get("canonical") if isinstance(m.get("recipe"), dict) else None
    try:
        recipe = Recipe.model_validate(canonical)
    except Exception:  # noqa: BLE001 - 配方解析不了就推不出附带的格，只给表级来历
        return _no("recipe_invalid")
    table_out = _table_out(receipt, table)
    if table_out is None:
        return _no("table_not_in_receipt")
    columns = _columns(table_out)
    meta = next((c for c in columns if c["name"] == column), None)
    if meta is None:
        return _no("column_not_in_receipt")
    role = str(meta.get("role") or "value")
    lineage = receipt.get("lineage") if isinstance(receipt.get("lineage"), dict) else {}
    # 第 1 步：锚格。被引用的列有溯源段就取它；交叉表的键列（本来就没有溯源段）取同一行第一个有溯源段的列
    if role in _VALUE_COLUMN_ROLES:
        anchor = lineage_cell(lineage, table, column, part_rowid)
    elif role in _KEY_ROLES:
        anchor = next((a for c in columns if (a := lineage_cell(lineage, table, c["name"], part_rowid))), None)
    else:
        return _no("column_role")
    if anchor is None:
        return _no("no_lineage_for_row")
    parsed = split_coord(anchor)
    if parsed is None or parsed[0] is None:
        return _no("anchor")
    sheet, ac, ar = parsed[0], parsed[1], parsed[2]
    marks = _marks(receipt)
    block_id = _anchor_block(marks, sheet, ar, ac, recipe, receipt)
    if block_id is None:
        return _no("anchor_block")
    block = _find_block(recipe, receipt, sheet, block_id)
    if block is None:
        return _no("block_not_in_recipe")
    ctx = _Ctx(chain, part, part_rowid, rowid, table, column, role, meta, table_out, columns, receipt, recipe, marks,
               sheet, ar, ac, block_id)
    if isinstance(block, CrosstabBlock):
        return _locate_crosstab(ctx, block)
    if isinstance(block, ListBlock):
        return _locate_list(ctx, block)
    return _no("block_layout")


def _cell_source(ctx: _Ctx, cell: tuple[int, int], froms: list[FromCell], *, year: YearSource | None,
                 canonical: CanonicalView | None, merged_fill: bool) -> Located:
    # 表的种类按冻结表结构认（同 datasource.reported_total_tables 的口径），冻结表结构里没有这张表时退回回执
    frozen = _frozen_tables(ctx.chain.schema)
    if ctx.table in frozen:
        kind = _table_kind(frozen, ctx.table)
    else:
        kind = "reported_total" if ctx.table_out.get("kind") == "reported_total" else "data"
    src = CellSource(
        table=ctx.table, column=ctx.column, column_role=ctx.role, kind=kind,  # type: ignore[arg-type]
        rowid=ctx.rowid, part_seq=ctx.part.seq, part_rowid=ctx.part_rowid, sheet=ctx.sheet,
        cell=_a1(cell[0], cell[1]), header=_str(ctx.meta.get("header")), unit=_str(ctx.meta.get("unit")),
        from_=froms, year=year, canonical=canonical, merged_fill=merged_fill)
    return Located(ctx.part, ctx.part_rowid, src)


def _canonical(raw: Any, canonical: Any) -> CanonicalView | None:
    """标签原文与库里的值（规范写法）不同时给出；任何一边拿不到都不给。"""
    if isinstance(raw, str) and raw and isinstance(canonical, str) and canonical and raw != canonical:
        return CanonicalView(raw=raw, canonical=canonical)
    return None


def _year(ctx: _Ctx, block: CrosstabBlock, form: Any) -> YearSource | None:
    """年份：交叉表的轴是文本日期（text / mixed）且配方 axis.year_from 有值时取自统计期，其余（日期格）为 None。

    mixed：清单只记每块的 form，不记哪一格是日期格、哪一格是文本，逐格判断不了，照实标 mixed（评审甲 7③）。
    """
    if form not in ("text", "mixed") or not block.axis.year_from:
        return None
    per = _period(ctx.part.manifest)
    if per is None:
        return None
    mixed = form == "mixed"
    if per.get("source") == "human":
        return YearSource(source="human", cells=[], signed_by=_str(per.get("signed_by")), mixed=mixed)
    return YearSource(source="period", cells=[str(c) for c in per.get("cells") or []], signed_by=None, mixed=mixed)


def _locate_crosstab(ctx: _Ctx, block: CrosstabBlock) -> Located | LocateProblem:
    """交叉表：值列有溯源段；日期取轴行、维度和派生列取行标签、常量列取分段标题（2.5 第 2 步的表）。"""
    receipt, sheet, ar, ac, role = ctx.receipt, ctx.sheet, ctx.ar, ctx.ac, ctx.role
    axes = [a for a in receipt.get("axes") or [] if isinstance(a, dict) and a.get("block") == ctx.block_id]
    if len(axes) != 1 or not _is_int(axes[0].get("row")):
        return _no("axis")
    axis = axes[0]
    axis_col = next((c["name"] for c in ctx.columns if c.get("role") == "axis"), block.axis.name)
    dim_col = next((c["name"] for c in ctx.columns if c.get("role") == "dim"), None)
    const_cols = [c["name"] for c in ctx.columns if c.get("role") == "const"]
    # 行标签：同块里 role 为 row_label / derived_label / total_label、矩形覆盖锚格那一行的区域
    labels = [mk for mk in ctx.marks if mk.sheet == sheet and mk.role in _LABEL_ROLES and mk.in_block(ctx.block_id)
              and mk.r1 <= ar <= mk.r2]
    label: tuple[int, str] | None = None            # (标签列, FromCell.role)
    if len({mk.c1 for mk in labels}) == 1:
        label = (labels[0].c1, _FROM_LABEL[labels[0].role])
    # 这一行属于块里哪个分段：SegmentLabels 不带工作表，先按配方取这一块的分段，再找 rows 含这一行的那一项
    seg_ids = {s.id for s in block.segments}
    seg_hits = [s for s in receipt.get("labels") or [] if isinstance(s, dict) and s.get("segment") in seg_ids
                and ar in (s.get("rows") or [])]
    seg_labels = seg_hits[0] if len(seg_hits) == 1 else None
    i = seg_labels["rows"].index(ar) if seg_labels is not None else -1
    seg_raw = seg_canon = None
    if seg_labels is not None:
        raws, canons = seg_labels.get("raw") or [], seg_labels.get("canonical") or []
        seg_raw = raws[i] if i < len(raws) else None
        seg_canon = canons[i] if i < len(canons) else None
    label_raw, label_canon = seg_raw, seg_canon
    if label is not None and label[1] == "total_label":
        # 交叉表的合计行：合计标签原文取这一格 DerivedItem 的 label_raw，规范写法（即库里合计项的值）取 label
        item = _derived_at(receipt, f"{sheet}!{_a1(ar, ac)}")
        if item is not None:
            label_raw, label_canon = item.get("label_raw"), item.get("label")
    # 分段标题：配方里这个分段按标题定位；同块 section_title 区域里行号小于这一段第一个标签行的，取最近的一个
    title: tuple[int, int, str | None] | None = None
    seg = next((s for s in block.segments if seg_labels is not None and s.id == seg_labels.get("segment")), None)
    if seg is not None and seg.locate.by == "section_title" and seg_labels.get("rows"):  # type: ignore[union-attr]
        first = min(seg_labels["rows"])  # type: ignore[index]
        above = sorted((mk.r1, mk.c1) for mk in ctx.marks if mk.sheet == sheet and mk.role == "section_title"
                       and mk.in_block(ctx.block_id) and mk.r1 < first)
        if above:
            best = max(r for r, _ in above)
            title = (best, min(c for r, c in above if r == best), seg.locate.title)
    # 被引用的格
    if role in _VALUE_COLUMN_ROLES:
        cell = (ar, ac)
    elif role == "axis":
        cell = (axis["row"], ac)
    elif role in ("dim", "derive"):
        if label is None:
            return _no("row_label")
        cell = (ar, label[0])
    else:  # const
        if title is None:
            return _no("section_title")
        cell = (title[0], title[1])
    # 附带的格：这一行其余键列各取一格，除了被引用的格本身
    froms: list[FromCell] = []
    if role != "axis":
        froms.append(FromCell(role="axis_header", column=axis_col, sheet=sheet, cell=_a1(axis["row"], ac)))
    label_in_from = False
    # 有维度列的表：行标签给出维度的值；宽表（只有指标列）：行标签是这一列的指标名，column 为 None
    if (dim_col is not None or role == "measure") and role not in ("dim", "derive"):
        if label is None:
            return _no("row_label")
        froms.append(FromCell(role=label[1], column=dim_col, sheet=sheet, cell=_a1(ar, label[0]),  # type: ignore[arg-type]
                              text=label_raw if isinstance(label_raw, str) else None))
        label_in_from = dim_col is not None
    if const_cols and role != "const":
        if title is None:
            return _no("section_title")
        # 分段标题只有配方里的定位文字（执行器按 match_key 比对，原表里的字可以不同），不当作格子原文
        froms.append(FromCell(role="section_title", column=const_cols[0], sheet=sheet, cell=_a1(title[0], title[1]),
                              text=None, locate_title=title[2]))
    # 规范写法：比的是这一行的标签列（维度列、合计项列）。被引用的就是它、或它在附带的格里时给
    canonical = _canonical(label_raw, label_canon) if dim_col is not None and (role == "dim" or label_in_from) \
        else None
    return _cell_source(ctx, cell, froms, year=_year(ctx, block, axis.get("form")), canonical=canonical,
                        merged_fill=False)


def _list_label_col(ctx: _Ctx, block: ListBlock) -> int | None:
    """列表合计行的标签列：合计标签所在的列（total_row.label_column）在明细表里的溯源段给出它在哪一列；明细表没有
    溯源段时（一行明细都没有），退回同块 total_label 区域（只有一列时）。"""
    total = block.rows.total_row
    lineage = ctx.receipt.get("lineage") if isinstance(ctx.receipt.get("lineage"), dict) else {}
    runs = ((lineage.get(block.table) or {}).get(total.label_column) or []) if total is not None else []
    for run in runs:
        if isinstance(run, list) and len(run) > 2 and run[1] == ctx.sheet \
                and (parsed := split_coord(str(run[2]))) is not None:
            return parsed[1]
    cols = {mk.c1 for mk in ctx.marks if mk.sheet == ctx.sheet and mk.role == "total_label"
            and mk.in_block(ctx.block_id) and mk.r1 <= ctx.ar <= mk.r2 and mk.c1 == mk.c2}
    return cols.pop() if len(cols) == 1 else None


def _list_header_cells(ctx: _Ctx, block: ListBlock, c: int, header: str) -> list[FromCell]:
    """列表第 c 列的表头格（2.5 第 3 步「多行表头给出每一格」），只列确知写着字的格。

    区域标记是同一去向的相邻已认领格合成的段，段内没认领的空格一并覆盖（recipe_engine._row_spans：「中间的空格一并
    覆盖，着色无妨」），而表头行只认领非空格（喂给 _ListRun 的只有非空格：Grid.row、xlsx_cells.sheet_rows）。所以：
    - 一行表头：这一列是按 (表头行, c) 这一格的字认出来的（一行表头拼表头时不看合并区，_compose），这一格必定有字，
      回执里列的 header 就是它的原文；
    - 多行表头：上层常是横向合并的「2026年上半年」，合并区里除左上格以外的格是空的，却落在段的中间（lab.c04 的
      D3 在 A3:E3 里），左边纵向合并的「地区」在下一层也是空格。清单不记合并区，段中间的格有没有字分不出，所以只列
      c 恰好是段的端点（起列或止列，必定是认领了的非空格）的那几层，其余层不列：宁可少列一格，也不把空格说成表头。
      多行表头的 header 是各层用「_」拼起来的，不是哪一格的原文，text 一律为 None。
    """
    hits = [mk for mk in ctx.marks if mk.sheet == ctx.sheet and mk.role == "col_header"
            and mk.in_block(ctx.block_id) and mk.c1 <= c <= mk.c2]
    if block.header_rows == 1:
        rows = sorted({r for mk in hits for r in range(mk.r1, mk.r2 + 1)})
        text = header if len(rows) == 1 else None
    else:
        rows = sorted({r for mk in hits if c in (mk.c1, mk.c2) for r in range(mk.r1, mk.r2 + 1)})
        text = None
    return [FromCell(role="col_header", column=None, sheet=ctx.sheet, cell=_a1(r, c), text=text) for r in rows]


def _locate_list(ctx: _Ctx, block: ListBlock) -> Located | LocateProblem:
    """列表：每一列都有溯源段（合计表的合计项列除外）；附带列表头，合计表另附合计标签（2.5 第 3 步）。"""
    receipt, sheet, ar, ac, role = ctx.receipt, ctx.sheet, ctx.ar, ctx.ac, ctx.role
    total = block.rows.total_row
    is_total = total is not None and bool(total.keep_as) and total.keep_as == ctx.table and ctx.table != block.table
    item = _derived_at(receipt, f"{sheet}!{_a1(ar, ac)}") if is_total else None
    label_c = _list_label_col(ctx, block) if is_total else None
    if role in _VALUE_COLUMN_ROLES:
        cell = (ar, ac)
    elif role == "dim" and is_total:
        if label_c is None:
            return _no("total_label")
        cell = (ar, label_c)
    else:
        return _no("column_role")
    froms: list[FromCell] = []
    header = ctx.meta.get("header")
    if is_total:
        # 合计表的列头取明细表同名列的（原表表头原文）；合计表这一列的 header 来自配方的写法
        base = _table_out(receipt, block.table)
        base_meta = next((c for c in _columns(base) if c["name"] == ctx.column), None) if base else None
        header = base_meta.get("header") if base_meta is not None else header
    if role != "dim" and isinstance(header, str) and header:
        froms += _list_header_cells(ctx, block, cell[1], header)
    dim_col = next((c["name"] for c in ctx.columns if c.get("role") == "dim"), None)
    if is_total and role != "dim":
        if label_c is None:
            return _no("total_label")
        froms.append(FromCell(role="total_label", column=dim_col, sheet=sheet, cell=_a1(ar, label_c),
                              text=_str(item.get("label_raw")) if item is not None else None))
    canonical = _canonical(item.get("label_raw"), item.get("label")) if is_total and item is not None else None
    # 列表这一块按合并单元格的左上格填充时，执行器给填充格记的溯源是填充位置，不是值所在的左上格；清单里没有合并区
    # 的信息，逐格判断不了，只能照实给出条件说明（评审甲 7②）
    return _cell_source(ctx, cell, froms, year=None, canonical=canonical, merged_fill=block.merged_data == "fill")


# ==========================================================================
# 相关核对（P4-SPEC 2.7，静态部分）
# ==========================================================================


def cell_status(check: dict[str, Any], coord: str, *, is_formula: bool, formula_check: bool) -> str | None:
    """合计核对（K、G、T）在这一格上的结论。check 是清单里的 CheckResult（dict），coord 是「工作表!A1」。

    - G 只核对公式格，写死的数直接跳过（recipe_checks._check_g），而 plan_checks 只要段里有一个公式格就给整段编
      G 号：所以这一格不是公式时为 not_formula，不能因为整段 G 通过就说「这一格一致」（评审甲 6）；
    - passed：ok；
    - 无法核对（或不一致）：格在 cells 里 → unverifiable（不一致的那几格为 None：CellStatus 没有这个取值，已提交的
      导入里也不会出现，界面看本期结论）；不在里面、而 failed + unverifiable 没超过 cells 的条数（清单列全了）→ ok；
      否则 unknown：清单只记前 20 格，这一格是否在其中无法确定，照实说，不猜。
    """
    if formula_check and not is_formula:
        return "not_formula"
    status = check.get("status")
    if status == "passed":
        return "ok"
    if status not in ("unverifiable", "mismatch"):
        return None
    cells = [str(c) for c in check.get("cells") or []]
    failed = check.get("failed") if _is_int(check.get("failed")) else 0
    unverifiable = check.get("unverifiable") if _is_int(check.get("unverifiable")) else 0
    if coord in cells:
        # cells 先列不一致的格、再列无法核对的格（recipe_checks._finish）
        return None if status == "mismatch" and cells.index(coord) < failed else "unverifiable"
    if failed + unverifiable <= len(cells):
        return "ok"
    return "unknown"


def _plan(cid: str, check: dict[str, Any], acceptances: dict[str, AcceptanceView], *,
          cell: str | None = None, rule: SumEqRule | None = None, detail: str | None = None) -> RelatedCheckPlan:
    return RelatedCheckPlan(RelatedCheck(
        id=cid, kind=str(check.get("kind") or ""), title=str(check.get("title") or cid),
        part_status=check.get("status"), row_status=None, cell_status=cell,  # type: ignore[arg-type]
        acceptance=acceptances.get(cid), detail=detail), rule)


def related_checks(chain: Chain, located: Located, *, table: str, column: str) -> list[RelatedCheckPlan]:
    """这一格的相关核对（P4-SPEC 2.7）：只取格子所在那一期的导入清单的 checks。

    顺序：关系核对（清单里的顺序：sum_eq 是 table 等于这张表、被引用的列是 total 或 parts 之一的；not_comparable
    是 a 或 b 为这张表的，只作口径说明），再 K、G、T（被引用的格就是某个合计格时，列出那一段的），最后统计期 C1、C2
    （年份取自统计期时）。part_status、cell_status、acceptance 在这里定；row_status 留 None，由接口一侧按
    RelatedCheckPlan.rule 在快照库上跑 SQL 后填。关系取格子所在那一期的配方。
    """
    m = located.part.manifest
    receipt = _receipt(m)
    checks = [c for c in m.get("checks") or [] if isinstance(c, dict) and c.get("id")]
    by_id = {str(c["id"]): c for c in checks}
    acceptances: dict[str, AcceptanceView] = {}
    for a in acceptances_of(m):
        acceptances.setdefault(a.check_id, a)
    canonical = (m.get("recipe") or {}).get("canonical") if isinstance(m.get("recipe"), dict) else None
    try:
        recipe: Recipe | None = Recipe.model_validate(canonical)
    except Exception:  # noqa: BLE001 - 配方解析不了：关系和合计核对认不出是哪一条，只列统计期
        recipe = None
    out: list[RelatedCheckPlan] = []
    if recipe is not None:
        relations = {r.id: r for r in recipe.relations}
        types = {c["name"]: str(c.get("type") or "") for c in _columns(_table_out(receipt, table) or {})}
        for c in checks:
            rel = relations.get(str(c["id"]))
            if c.get("kind") == "relation_sum_eq" and isinstance(rel, SumEq) and rel.table == table \
                    and column in (rel.total, *rel.parts):
                names = [rel.total, *rel.parts]
                rule = SumEqRule(table=rel.table, total=rel.total, parts=list(rel.parts),
                                 types={n: types[n] for n in names if n in types})
                out.append(_plan(rel.id, c, acceptances, rule=rule))
            elif c.get("kind") == "relation_not_comparable" and isinstance(rel, NotComparable) \
                    and table in (rel.a.table, rel.b.table):
                out.append(_plan(rel.id, c, acceptances))
        out += _total_checks(recipe, receipt, located, by_id, acceptances)
    if located.cell.year is not None:
        out += [_plan(cid, by_id[cid], acceptances) for cid in ("C1", "C2") if cid in by_id]
    return out


def _total_checks(recipe: Recipe, receipt: dict[str, Any], located: Located, by_id: dict[str, dict[str, Any]],
                  acceptances: dict[str, AcceptanceView]) -> list[RelatedCheckPlan]:
    """被引用的格就是某个合计格（DerivedItem.cell）时，那一段的 K、G（列表合计是 T）。编号由 plan_checks 定，
    和导入时的核对、说明生成是同一套。"""
    from app.data.recipe_imports import extraction_from_dict

    coord = f"{located.cell.sheet}!{located.cell.cell}"
    item = _derived_at(receipt, coord)
    if item is None:
        return []
    try:
        plan = plan_checks(recipe, extraction_from_dict(receipt))
    except Exception:  # noqa: BLE001 - 回执形状不对就认不出编号，不列合计核对
        _log.warning("provenance: 回执还原失败，不列合计核对", exc_info=True)
        return []
    seg = item.get("segment")
    is_formula = bool(item.get("is_formula"))
    out: list[RelatedCheckPlan] = []
    for cid, formula_check in ((plan.k.get(seg), False), (plan.g.get(seg), True), (plan.t.get(seg), False)):
        check = by_id.get(cid) if cid else None
        if check is None or check.get("kind") not in _TOTAL_KINDS:
            continue
        details = [str(d) for d in check.get("details") or []]
        detail = details[0] if check.get("status") == "unverifiable" and details else None
        out.append(_plan(cid, check, acceptances, detail=detail,  # type: ignore[arg-type]
                         cell=cell_status(check, coord, is_formula=is_formula, formula_check=formula_check)))
    return out


# ==========================================================================
# 裁判摘录的来历行（P4-SPEC 3.2）
# ==========================================================================


def _period_words(manifest: dict[str, Any]) -> str:
    per = _period(manifest)
    if per is None or not per.get("start") or not per.get("end"):
        return "统计期未记录"
    return f"统计期 {per['start']} 至 {per['end']}"


def _period_detail(manifest: dict[str, Any]) -> str:
    """来源行里的统计期：取自哪几格，或人工录入（R2 的人工录入上下文：署名照写，标「未认证」）。"""
    words = _period_words(manifest)
    per = _period(manifest)
    if per is None or words == "统计期未记录":
        return words
    if per.get("source") == "human":
        who = _str(per.get("signed_by"))
        return f"{words}（人工录入，署名「{who}」，未认证）" if who else f"{words}（人工录入，未署名）"
    cells = [str(c) for c in per.get("cells") or []]
    return f"{words}（取自 {'、'.join(cells)}）" if cells else words


def _part_line(part: ChainPart) -> str:
    m = part.manifest
    f = m.get("file") if isinstance(m.get("file"), dict) else {}
    receipt = _receipt(m)
    bits = [_period_detail(m)]
    name, sha = _str(f.get("name")), _str(f.get("raw_sha256"))
    if name:
        bits.append(f"文件「{name}」" + (f"（sha256 {sha[:12]}）" if sha else ""))
    region = region_of(receipt)
    if region:
        bits.append(f"区域 {region}")
    excluded = excluded_rows_of(receipt)
    if excluded:
        bits.append(f"排除 {excluded} 行")
    return f"第 {part.seq} 次导入：" + "，".join(bits)


def _masked(name: str, hidden: set[str]) -> bool:
    return str(name).lower() in hidden


@dataclass
class _Lines:
    """来历块的各部分。分开存，整体超长时按 3.2 的五步顺序删（_fit），删完再拼成行。"""

    head: list[str]
    parts: list[str]                         # 来源的分期行，按统计期升序
    parts_more: int
    tables: list[tuple[str, str]]            # 表的说明：(行首, 说明)
    columns: list[str]
    accepts: list[tuple[int, str]]           # (期的位置, 行)，按期次升序
    accepts_more: int
    outside: list[tuple[int, str]]           # (期的位置, 行)，按送出的顺序
    outside_more: int                        # 超出条数上限、被截断步骤删掉的
    outside_unlisted: int                    # hidden 为 True 或未记录（None）的
    outside_masked: int                      # 数据源设有遮罩时的总条数（一格都不送）
    cut: bool = False

    def render(self) -> list[str]:
        out = list(self.head)
        out += [f"  {x}" for x in self.parts]
        if self.parts_more:
            out.append(f"  另有 {self.parts_more} 期，见证据面板")
        out += [a + b for a, b in self.tables]
        out += self.columns
        if self.accepts:
            out.append(_ACCEPT_HEAD)
            out += [f"  {x}" for _, x in self.accepts]
            if self.accepts_more:
                out.append(f"  另有 {self.accepts_more} 条，见证据面板")
        if self.outside_masked:
            out.append(f"区域外另有 {self.outside_masked} 处文字（数据源设置了遮罩，未列出），见证据面板")
        else:
            unlisted = (f"另有 {self.outside_unlisted} 处区域外文字未列出（所在行列被隐藏，或导入时未记录是否隐藏），"
                        "见证据面板")
            if self.outside:
                out.append(_OUTSIDE_HEAD)
                out += [f"  {x}" for _, x in self.outside]
                if self.outside_more:
                    out.append(f"  另有 {self.outside_more} 处区域外文字未列出（超出条数上限），见证据面板")
                if self.outside_unlisted:
                    out.append(f"  {unlisted}")
            elif self.outside_unlisted:
                out.append(unlisted)
        if self.cut:
            out.append(_CUT)
        return out

    def size(self) -> int:
        return len("\n".join(self.render()))


def _fit(block: _Lines, limit: int) -> None:
    """整块超过 limit 字时按固定顺序删（P4-SPEC 3.2），保证确定：
    1. 来源的分期行，从最旧的期起（总述行保留）；2. 列的说明，从最后一列起；3. 已接受，从最旧的期起；
    4. 区域外文字，从最旧的期起；5. 表的说明截到剩余字数，末尾加「…」。删过任何一项，末尾加一行说明。"""
    while block.size() > limit:
        if block.parts:
            block.parts.pop(0)
            block.parts_more += 1
        elif block.columns:
            block.columns.pop()
        elif block.accepts:
            block.accepts.pop(0)
            block.accepts_more += 1
        elif block.outside:
            oldest = min(k for k, _ in block.outside)
            at = max(i for i, (k, _) in enumerate(block.outside) if k == oldest)
            block.outside.pop(at)
            block.outside_more += 1
        elif block.tables:
            if not block.cut:
                block.cut = True
                continue
            over = block.size() - limit
            lead, body = block.tables[-1]
            keep = len(body) - over - 1
            if keep > 0:
                block.tables[-1] = (lead, body[:keep] + "…")
            else:
                block.tables.pop()
        else:
            break
        block.cut = True


def judge_lines(query: dict[str, Any], schema: dict[str, Any] | None, load: Loader, *,
                tables: list[str], hidden: set[str]) -> list[str]:
    """裁判摘录里查询快照 SQL 之后、「列：」之前的来历行（P4-SPEC 3.2）。纯函数，loader 注入，同样的工件永远得到
    同样的行；不取数据库列、不取当前时间。

    - tables：调用方用 direct_select.tables_in 算好的 SQL 用到的表（这里与冻结表结构按 name_key 求交集）；
    - hidden：_query_excerpt 现成的那份遮罩列（小写）。非空表示数据源设有遮罩：接受理由不写、区域外文字一格不送
      （理由、注释里可能写着遮罩列的数），遮罩列的说明不写。

    生效范围：来源、已接受、区域外文字要求表结构快照是按配方导入的、链能解析；口径（表和列的说明）有 data_version
    和表结构快照即可，简单导入也写（调整 6）。查询快照没有 data_version（手工源）时返回空列表。链解析失败时写一行
    「来源：导入清单无法读取…」，不中断摘录。
    """
    if not isinstance(query, dict) or not query.get("data_version") or not isinstance(schema, dict):
        return []
    hidden = {str(h).lower() for h in hidden or ()}
    frozen = _frozen_tables(schema)
    used: list[str] = []
    for name in tables or []:
        hit = _frozen_name(frozen, str(name))
        if hit is not None and hit not in used:
            used.append(hit)
    block = _Lines(head=[], parts=[], parts_more=0, tables=[], columns=[], accepts=[], accepts_more=0, outside=[],
                   outside_more=0, outside_unlisted=0, outside_masked=0)
    _caliber(block, query, frozen, used, hidden)
    if schema.get("import_mode") == "recipe":
        chain = resolve_chain(query, schema, load)
        if isinstance(chain, ChainProblem):
            block.head = [SOURCE_BROKEN]
        else:
            _source(block, chain)
            _accepts(block, chain, hidden)
            _outside(block, chain, hidden)
    _fit(block, PROV_CHARS)
    return block.render()


def _source(block: _Lines, chain: Chain) -> None:
    mode = _MODE_LABEL.get(chain.mode, "每期替换")
    if not chain.union:
        block.head = [f"来源：上传的表格，{mode}，{_part_line(chain.parts[0])}"]
        return
    spans = [(str(p["start"]), str(p["end"])) for part in chain.parts
             if (p := _period(part.manifest)) is not None and p.get("start") and p.get("end")]
    span = f"（{min(a for a, _ in spans)} 至 {max(b for _, b in spans)}）" if spans else ""
    block.head = [f"来源：上传的表格，{mode}，共 {len(chain.parts)} 期{span}"]
    shown = chain.parts[-PROV_PARTS:]
    block.parts = [_part_line(p) for p in shown]
    block.parts_more = len(chain.parts) - len(shown)


def _caliber(block: _Lines, query: dict[str, Any], frozen: dict[str, Any], used: list[str],
             hidden: set[str]) -> None:
    """口径：SQL 用到的每张表的说明；查询结果列里按 name_key 对得上这些表某列、且列有注释的，列的说明（遮罩列不写）。
    说明来自冻结的表结构快照，本来就是模型看得到的、不含数字的散文；未规整的表的说明就是 UNSHAPED_NOTE。"""
    for name in used:
        comment = frozen[name].get("comment") if isinstance(frozen[name], dict) else None
        if isinstance(comment, str) and comment.strip():
            block.tables.append((f"口径：表 {name} 的说明：", comment.strip()))
    seen: set[str] = set()
    for col in query.get("columns") or []:
        key = name_key(str(col))
        if len(block.columns) >= PROV_COLUMN_NOTES or _masked(str(col), hidden):
            continue
        for name in used:
            meta = frozen[name] if isinstance(frozen[name], dict) else {}
            hit = next((c for c in meta.get("columns") or [] if isinstance(c, dict)
                        and name_key(str(c.get("name"))) == key), None)
            if hit is None:
                continue
            cname, comment = str(hit.get("name")), hit.get("comment")
            if cname not in seen and not _masked(cname, hidden) and isinstance(comment, str) and comment.strip():
                seen.add(cname)
                block.columns.append(f"口径：列 {cname} 的说明：{comment.strip()}")
            break


def _accepts(block: _Lines, chain: Chain, hidden: set[str]) -> None:
    """已接受：各期清单 acceptances，按期次排序，最多 PROV_ACCEPT 条（取最近的几期的），其余写「另有 n 条」。"""
    rows: list[tuple[int, str]] = []
    for k, part in enumerate(chain.parts):
        where = f"第 {part.seq} 次导入（{_period_words(part.manifest)}）"
        for a in acceptances_of(part.manifest):
            title = a.title or a.check_id
            head = f"{where}的核对「{title}」{_ACCEPT_STATE.get(a.kind, '不成立')}"
            if hidden:
                rows.append((k, f"{head}{_ACCEPT_MASKED}"))
                continue
            who = f"署名「{a.signed_by}」，未认证" if a.signed_by else "未署名"
            rows.append((k, f"{head}，理由：{a.reason}（{who}）"))
    block.accepts = rows[-PROV_ACCEPT:] if rows else []
    block.accepts_more = len(rows) - len(block.accepts)


def _outside(block: _Lines, chain: Chain, hidden: set[str]) -> None:
    """区域外文字：只送 hidden is False 的项（调整 10）；为 True 或老清单没有这个字段的只计条数。先含数字的，
    再按期次从新到旧；每格最多 PROV_OUTSIDE_CHARS 字，最多 PROV_OUTSIDE 格。每行写所属的导入和统计期：并集时 8 月
    的注释不能被读成 9 月的。数据源设有遮罩时一格都不送，只写总条数。"""
    items: list[tuple[int, int, int, int, str]] = []     # (含数字在前, 新的在前, 原顺序, 期的位置, 行)
    total = unlisted = 0
    for k, part in enumerate(chain.parts):
        where = f"第 {part.seq} 次导入（{_period_words(part.manifest)}）"
        for n, o in enumerate(_receipt(part.manifest).get("outside_text") or []):
            if not isinstance(o, dict):
                continue
            total += 1
            if o.get("hidden") is not False:
                unlisted += 1
                continue
            text = str(o.get("text") or "")
            shown = f"「{text}」" if len(text) <= PROV_OUTSIDE_CHARS else f"「{text[:PROV_OUTSIDE_CHARS]}…」（已截断）"
            items.append((0 if o.get("kind") == "text_digits" else 1, -k, n, k, f"{where}{o.get('cell')}：{shown}"))
    if hidden:
        block.outside_masked = total
        return
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    block.outside = [(k, line) for *_, k, line in items[:PROV_OUTSIDE]]
    block.outside_more = max(0, len(items) - PROV_OUTSIDE)
    block.outside_unlisted = unlisted

