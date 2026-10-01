"""按期累积与版本页的数据侧（期 3，WP-5；P3-SPEC 2.3、2.6、2.9、第 5、7 节）。

两类调用方共用这里：
- 试运行、提交（recipe_imports）：组装当前快照的各期（PartInfo）、当前版本实际的表结构、快照清单里的退役记录，
  物化之后写快照清单；
- 版本页（api/source_versions、api/datasources）：列版本、启用旧版本、查看清单、移除一期、作废接受、导入记录的
  新字段（revoke_plan、raw_shared_with……）。

**各期的统计期只信导入清单。** 导入清单是内容寻址的工件（artifact_store.load 会复验内容哈希），库列
table_imports.period_start / period_end 是可改的：两者对不上说明库列被改过，按 part_tampered 拒绝（2.3，
评审一-m11），不拿被改过的统计期去判重叠、做物化。

**写操作一律经 WP-4 的原语**（publish_snapshot、activate_snapshot），锁内比对 expected_current；这里只在锁外
组装输入、映射错误码。锁外读到的东西在锁内可能已经变了：当前版本变了由 base_changed 兜住，回滚目标被回收、
被作废由 activate_snapshot 的 snapshot_unavailable、contains_revoked 兜住，都不会悄悄切到别的版本。另外，锁外
读到的当前版本必须就是确认框依据的那个（_check_base）：留下哪几期、回滚到哪里都按它算，只靠锁内比对挡不住
「改走又改回」的 ABA。只读进程（没拿到版本存储的守卫）在动手组装、物化之前就回 503。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import raw_store, recipe_types, table_versions
from app.data.recipe_imports import ImportRefused, check_from_dict, extraction_from_dict, pipeline
from app.data.recipe_types import (
    SNAPSHOT_NOT_ACTIVATABLE, Acceptance, ColumnOut, PartInfo, Recipe, SchemaNotes, UnionNotePart, UnionPart,
)
from app.db.base import utcnow
from app.db.models import (
    DataSource, ImportStaging, Run, SourceSnapshot, TableBuild, TableImport, TableRecipe,
)

logger = logging.getLogger(__name__)

#: 快照清单的格式版本（P3-SPEC 2.9）
SNAPSHOT_MANIFEST_FORMAT = "agentlab-snapshot-manifest/1"
#: GET snapshots 最多列几个（7.1）
SNAPSHOTS_MAX = 50
#: 理由的字数上限（启用、移除、作废、清除，7.2–7.6）
REASON_MAX = 500

_NO_ROLLBACK = "没有可以回滚的版本，请上传修正后的文件"
_TARGET_CHANGED = "可以回滚的版本已有变化，请刷新后重新确认"
_LAST_PERIOD = "这是当前版本里唯一的一期，不能移除；如需清空请删除数据源"
#: raw_shared_with 里另一个源已被删除时的名字（与界面 VERSIONS_TEXT.deletedSource 同文）。名字会拼进「「名字」N 条」，
#: 给 null 的话旧界面会显示成「null」（评审意见）；界面的类型已声明可空并有同文兜底，这里仍给名字，新旧界面都读得通。
#: 删除数据源只把它的导入置 retired，原件仍可能被本源共用，这条引用照样要列出来
_DELETED_SOURCE = "已删除的数据源"


class PartProblem(Exception):
    """当前快照的某一期读不出来（导入清单不在、被改过，统计期与库列对不上，构建记录不在）。

    code 是 part_tampered / part_missing：试运行把它变成拒收的问题，版本页的写操作回 409。message 给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ==========================================================================
# 当前快照的各期
# ==========================================================================


@dataclass
class Part:
    """当前快照里的一期：契约 PartInfo 之外，物化、说明还要用的几样（导入清单、登记的表哈希、原件哈希）。"""

    info: PartInfo
    manifest: dict[str, Any]
    #: 该期登记的表哈希（组合定义要用，2.6 第 6 步）：取自导入清单（内容寻址），不取可改的构建回执
    table_hashes: dict[str, str] = field(default_factory=dict)
    raw_sha: str = ""
    #: 该期统计期的来源：cells / human
    period_source: str | None = None

    def union_part(self) -> UnionPart:
        if self.info.recipe is None:
            raise PartProblem("part_tampered", f"第 {self.info.seq} 次导入没有配方记录，无法重建当前版本")
        return UnionPart(import_id=self.info.import_id, start=self.info.start, end=self.info.end,
                         db_path=self.info.db_path, recipe=self.info.recipe, db_sha256=self.info.db_sha256,
                         manifest_db_sha256=self.info.manifest_db_sha256, table_hashes=dict(self.table_hashes))


def _period_label(start: str | None, end: str | None) -> str:
    return f"{start} 至 {end}" if start and end else "统计期未记录"


def load_manifest(artifact_id: str | None) -> dict[str, Any] | None:
    """取回一份工件（导入清单、快照清单）。内容哈希对不上时抛 ValueError（artifact_store.load 自己复验）；不在为 None。"""
    if not artifact_id:
        return None
    from app.core import artifact_store

    try:
        doc = artifact_store.load(artifact_id)
    except OSError:
        return None
    return doc if isinstance(doc, dict) else None


def _rows_of(report: dict[str, Any] | None) -> dict[str, int]:
    """构建回执里各表的行数（执行器回执、期 1 的解析回执都是 tables: [{name, rows}]）。"""
    out: dict[str, int] = {}
    for t in (report or {}).get("tables") or []:
        if isinstance(t, dict) and t.get("name") is not None:
            try:
                out[str(t["name"])] = int(t.get("rows") or 0)
            except (TypeError, ValueError):
                continue
    return out


def _part_from(imp: TableImport, build: TableBuild | None, manifest: dict[str, Any] | None, seq: int) -> Part:
    """一条导入记录 → Part。统计期取自导入清单，再与库列比对（2.3）。seq 是它在当前快照里的序号（报错用）。"""
    label = f"第 {seq} 期"
    if manifest is None:
        raise PartProblem("part_tampered", f"{label}（第 {imp.seq} 次导入）的导入清单不在了，无法核对这一期的数据。"
                                           "可以在数据源卡片的「版本」中启用更早的版本")
    period = manifest.get("period") if isinstance(manifest.get("period"), dict) else {}
    start, end = period.get("start"), period.get("end")
    if (start or None) != (imp.period_start or None) or (end or None) != (imp.period_end or None):
        logger.warning("导入记录的统计期与导入清单不一致：import=%s 清单=%s~%s 库列=%s~%s",
                       imp.id, start, end, imp.period_start, imp.period_end)
        raise PartProblem("part_tampered", f"{label}（第 {imp.seq} 次导入）登记的统计期与它的导入清单不一致，"
                                           "可能被修改过。可以在数据源卡片的「版本」中启用更早的版本")
    if build is None:
        raise PartProblem("part_missing", f"{label}（{_period_label(start, end)}）的数据文件已丢失，无法重建当前版本。"
                                          "可以先移除这一期，或在数据源卡片的「版本」中启用更早的版本")
    meta = manifest.get("recipe") if isinstance(manifest.get("recipe"), dict) else {}
    recipe: Recipe | None = None
    try:
        recipe = Recipe.model_validate(meta.get("canonical")) if meta.get("canonical") is not None else None
    except ValueError:
        recipe = None
    hashes = manifest.get("table_hashes") if isinstance(manifest.get("table_hashes"), dict) else {}
    info = PartInfo(
        import_id=imp.id, seq=imp.seq, start=str(start or ""), end=str(end or ""), build_id=imp.build_id,
        db_path=build.db_path, db_sha256=str(build.db_sha256 or ""),
        manifest_db_sha256=manifest.get("db_sha256"), recipe_id=imp.recipe_id, recipe=recipe,
        recipe_sha256=meta.get("sha256"), raw_state=imp.raw_state, file_name=imp.file_name or "",
        manifest_artifact=imp.manifest_artifact, rows=_rows_of(build.report),
        overrides=len(imp.overrides or []), waivers=len(imp.waivers or []),
    )
    return Part(info=info, manifest=manifest, table_hashes={str(k): str(v) for k, v in hashes.items()},
                raw_sha=imp.raw_sha256 or "", period_source=period.get("source"))


async def load_parts(session: AsyncSession, snapshot: SourceSnapshot | None, *,
                     exclude: str | None = None) -> list[Part]:
    """当前快照的各期（按快照记录的顺序，即按统计期）。简单导入的快照没有配方各期，返回空列表。

    任何一期读不出来抛 PartProblem：被改过的统计期、不在的清单都不能悄悄跳过，否则结果版本会少一期。

    exclude：移除、作废这一期时，被移除的那一期**先排除、不校验**（7.4「移除的正好是那一期时，它的文件不参与物化，
    不报这个错」）。先全部校验再过滤的话，坏掉的正是要移除的那一期时反而移不掉，part_missing 的消息还建议「先移除
    这一期」，走不通（评审意见）。序号（报错里的「第 N 期」）仍按它在当前快照里的位置算，与版本页一致。"""
    if snapshot is None or not snapshot.imports:
        return []
    ids = [str(x) for x in snapshot.imports]
    imps = {i.id: i for i in (await session.execute(select(TableImport).where(TableImport.id.in_(ids)))).scalars()}
    if not any(i.recipe_id for i in imps.values()):
        return []
    builds = {b.id: b for b in (await session.execute(
        select(TableBuild).where(TableBuild.id.in_([i.build_id for i in imps.values()])))).scalars()}
    parts: list[Part] = []
    for k, iid in enumerate(ids, 1):
        if exclude is not None and iid == exclude:
            continue
        imp = imps.get(iid)
        if imp is None:
            raise PartProblem("part_missing", f"第 {k} 期的导入记录不在了，无法重建当前版本。"
                                              "可以在数据源卡片的「版本」中启用更早的版本")
        try:
            manifest = await asyncio.to_thread(load_manifest, imp.manifest_artifact)
        except ValueError:
            raise PartProblem("part_tampered", f"第 {k} 期（第 {imp.seq} 次导入）的导入清单与它的哈希不符，"
                                               "可能被修改过") from None
        parts.append(_part_from(imp, builds.get(imp.build_id), manifest, k))
    return parts


def _columns_from(cols: Any) -> list[ColumnOut]:
    out = []
    for c in cols or []:
        if isinstance(c, dict) and c.get("name") is not None:
            out.append(ColumnOut(name=str(c["name"]), type=str(c.get("type") or "TEXT"), header=c.get("header"),
                                 unit=c.get("unit"), role=c.get("role") or "measure"))
    return out


def _union_build_id(snapshot: SourceSnapshot) -> str | None:
    """快照库是并集库时，它的并集构建 id（文件名）。单期快照的库就是那一期的构建库，返回 None。"""
    path = Path(snapshot.db_path or "")
    if path.parent.name != table_versions.UNION_DIR:
        return None
    return path.stem


async def current_tables(session: AsyncSession, snapshot: SourceSnapshot | None) -> dict[str, list[ColumnOut]] | None:
    """当前版本实际的表结构（含早先退役、仍保留在并集里的列）：并集取并集构建登记的回执（UnionReport.tables），
    单期取那一期构建回执的 tables。两样都取不到时退回快照冻结的 schema_cache（只有列名和类型）。"""
    if snapshot is None:
        return None
    uid = _union_build_id(snapshot)
    if uid is not None:
        build = await session.get(TableBuild, uid)
        tables = (build.report or {}).get("tables") if build is not None else None
        if isinstance(tables, dict) and tables:
            return {str(name): _columns_from(cols) for name, cols in tables.items()}
    elif snapshot.imports:
        imp = await session.get(TableImport, str(snapshot.imports[-1]))
        build = await session.get(TableBuild, imp.build_id) if imp is not None else None
        tables = (build.report or {}).get("tables") if build is not None else None
        if isinstance(tables, list) and tables:
            return {str(t["name"]): _columns_from(t.get("columns")) for t in tables
                    if isinstance(t, dict) and t.get("name") is not None}
    cache = snapshot.schema_cache or {}
    out: dict[str, list[ColumnOut]] = {}
    for name, meta in (cache.get("tables") or {}).items():
        cols = meta.get("columns") if isinstance(meta, dict) else None
        out[str(name)] = _columns_from(cols)
    return out or None


def tables_in_parts(tables: dict[str, list[ColumnOut]] | None,
                    parts: list[Part], *, also: Recipe | None = None) -> dict[str, list[ColumnOut]] | None:
    """当前版本的表结构里，只留下至少一期剩下的构建里有的表和列（顺序照旧）。移除、作废一期时用。

    also：这份配方（试运行的目标配方）推出的表和列也留下。试运行路径用它（WP-8 修补）：parts 给结果各期（当前版本的
    各期去掉本期要替换的那一期，recipe_imports._replaced_part），筛掉「结果各期都没有、目标配方也没有」的幽灵列，
    免得累积计划把它当成「这次才退役」、出一条任何一期都没有数据的必勾项（文案还写「早期各期保留原值」）。两种情况：
    移除唯一含「分区丙」的那一期之后，当前并集按目标配方仍留着一列全为空值的分区丙；替换唯一含分区丙的那一期，
    被替换的旧数据不在新版本里。后一种是这一列从现行配方里消失，由 breaking:<表> 要人确认，不会变成静默。结果各期
    有数据的列照旧留着，真正的退役照样要确认（H2、H3）；目标配方里有的列也照旧留着，不会因此多报「新增列」。

    为什么要筛：当前并集的表结构是有历史的。被移除的那一期独有的列（例如只有 9 月有的「分区丙」，10 月起退役）还
    留在当前并集里，直接拿去比，剩下的 8 月、10 月都没有它，新并集却多出一列全为空值的「幽灵」退役列，快照清单把
    它记成退役、since 还是第一期；更糟的是并集 id 随之不同，而快照 id 只看（剩下各期, 目标配方, 模式）：复活已回收
    的同 id 快照时内容对不上，publish_snapshot 报 snapshot_conflict，用户根本移除不了（评审意见）。筛过之后并集只
    取决于（剩下各期, 目标配方），与快照 id 的口径一致（2.1「同一组导入、同一个目标配方、同为累积模式，得到同一个
    快照 id，内容也必然相同」）。

    各期构建库的表结构就是它的配方 derive_tables 推出的那份（执行器照它建表），所以按各期配方判断，不必打开库。
    留下的列保持当前并集里的顺序：退役列的先后进建表语句、也就进库文件的哈希，要与这组导入当初物化时一致。"""
    if tables is None:
        return None
    have: dict[str, set[str]] = {}
    for rec in [*(part.info.recipe for part in parts), also]:
        if rec is None:
            continue
        derived, _problems = recipe_types.derive_tables(rec)
        for name, cols in derived.items():
            have.setdefault(name, set()).update(c.name for c in cols)
    out: dict[str, list[ColumnOut]] = {}
    for name, cols in tables.items():
        names = have.get(name)
        if names is None:
            continue
        kept = [c for c in cols if c.name in names]
        if kept:
            out[name] = kept
    return out


def snapshot_manifest(snapshot: SourceSnapshot | None) -> dict[str, Any] | None:
    """快照的快照清单（物化过的累积快照才有）。被改过时抛 PartProblem(part_tampered)：退役记录靠它，不能当作没有。"""
    if snapshot is None or not snapshot.manifest_artifact:
        return None
    try:
        doc = load_manifest(snapshot.manifest_artifact)
    except ValueError:
        raise PartProblem("part_tampered", "当前版本的版本清单与它的哈希不符，可能被修改过") from None
    if doc is None:
        raise PartProblem("part_tampered", "当前版本的版本清单不在了，无法确认此前退役的列")
    return doc


def retired_status(manifest: dict[str, Any] | None) -> dict[str, dict[str, dict]] | None:
    """快照清单的 columns（表 → 列 → {status, …}），classify_changes 据此区分「早已退役」与「这次才退役」。"""
    cols = (manifest or {}).get("columns")
    return copy.deepcopy(cols) if isinstance(cols, dict) else None


def retired_names(manifest: dict[str, Any] | None) -> dict[str, list[str]]:
    """当前版本里退役的表名 → 该表退役的列名（框选的撞名检查用，recipe_select 的 retired_names）。"""
    out: dict[str, list[str]] = {}
    for table, cols in (retired_status(manifest) or {}).items():
        names = [str(c) for c, meta in (cols or {}).items()
                 if isinstance(meta, dict) and meta.get("status") == "retired"]
        if names:
            out[str(table)] = names
    return out


def acceptances_of(manifest: dict[str, Any]) -> list[Acceptance]:
    acc = manifest.get("acceptances") if isinstance(manifest.get("acceptances"), dict) else {}
    out = []
    for rec in [*(acc.get("overrides") or []), *(acc.get("waivers") or [])]:
        if isinstance(rec, dict) and rec.get("check_id"):
            out.append(Acceptance(check_id=str(rec["check_id"]), reason=str(rec.get("reason") or ""),
                                  signed_by=rec.get("signed_by")))
    return out


def note_part(part: Part) -> UnionNotePart:
    """一期的说明输入（同步，线程里跑：空值数要查该期的构建库）。数据取自该期的导入清单和构建库（2.8）。"""
    p = pipeline()
    recipe = part.info.recipe
    if recipe is None:
        raise PartProblem("part_tampered", f"第 {part.info.seq} 次导入没有配方记录，无法生成说明")
    receipt = part.manifest.get("receipt") if isinstance(part.manifest.get("receipt"), dict) else {}
    ex = extraction_from_dict(receipt)
    checks = [check_from_dict(c) for c in part.manifest.get("checks") or [] if isinstance(c, dict)]
    nulls = p.column_null_counts(part.info.db_path, recipe) if Path(part.info.db_path).is_file() else None
    return UnionNotePart(recipe=recipe, extraction=ex, checks=checks, acceptances=acceptances_of(part.manifest),
                         null_counts=nulls, start=part.info.start, end=part.info.end)


def single_notes(part: Part) -> SchemaNotes:
    """一期、快照库就是它的构建库时的说明（同步）：照它导入时的写法重新生成。"""
    p = pipeline()
    src = note_part(part)
    return p.build_notes(src.recipe, src.extraction, src.checks, src.acceptances, null_counts=src.null_counts)


def gaps_of(periods: list[tuple[str | None, str | None]]) -> list[dict[str, str]]:
    """相邻两期之间的空缺（前一期 end 加一天 < 后一期 start）。没有统计期的期不参与。"""
    import datetime as _dt

    spans = sorted((s, e) for s, e in periods if s and e)
    out: list[dict[str, str]] = []
    for (_s1, e1), (s2, _e2) in zip(spans, spans[1:]):
        try:
            after = _dt.date.fromisoformat(str(e1)) + _dt.timedelta(days=1)
            before = _dt.date.fromisoformat(str(s2)) - _dt.timedelta(days=1)
        except ValueError:
            continue
        if after <= before:
            out.append({"start": after.isoformat(), "end": before.isoformat()})
    return out


# ==========================================================================
# 快照清单（2.9）
# ==========================================================================


def _since(periods: list[str], having: list[str]) -> str | None:
    """一列（或一张表）自哪一期起没有了：最后一个含它的期之后的第一期。"""
    last = max((periods.index(p) for p in having if p in periods), default=-1)
    return periods[last + 1] if last + 1 < len(periods) else None


def columns_doc(report: dict[str, Any], periods: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """快照清单的 columns：部分期才有的列标 added（periods 列出哪几期有），退役的列标 retired（since、unit）。
    整张表退役时把它的每一列都标 retired：classify_changes 要每一列都是 retired 才认作「早已退役」（WP-4 交接）。"""
    tables = report.get("tables") if isinstance(report.get("tables"), dict) else {}
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def unit_of(table: str, column: str) -> str | None:
        for c in tables.get(table) or []:
            if isinstance(c, dict) and c.get("name") == column:
                return c.get("unit")
        return None

    for entry in report.get("added") or []:
        table, column, having = entry.get("table"), entry.get("column"), list(entry.get("periods") or [])
        names = [column] if column else [c.get("name") for c in tables.get(table) or [] if isinstance(c, dict)]
        for name in names:
            if name:
                out.setdefault(str(table), {})[str(name)] = {"status": "added", "periods": having}
    for entry in report.get("retired") or []:
        table, column, having = entry.get("table"), entry.get("column"), list(entry.get("periods") or [])
        names = [column] if column else [c.get("name") for c in tables.get(table) or [] if isinstance(c, dict)]
        for name in names:
            if name:
                out.setdefault(str(table), {})[str(name)] = {
                    "status": "retired", "since": _since(periods, having), "unit": unit_of(str(table), str(name))}
    return out


def manifest_doc(*, source_id: str, snapshot_id: str, action: str, recipe: dict[str, Any],
                 union: dict[str, Any], parts: list[Part | dict[str, Any]], report: dict[str, Any],
                 label_sets: list[Any], gaps: list[dict[str, str]], replaced: Any, removed: Any,
                 notes: SchemaNotes, signed_by: str | None) -> dict[str, Any]:
    """快照清单（2.9）。parts 按统计期排序，与 report.part_rows 同序；本期写成 dict（导入记录还没落库）。"""
    part_rows = report.get("part_rows") or []
    keys = []
    out_parts = []
    for k, part in enumerate(parts):
        if isinstance(part, Part):
            d = {"import_id": part.info.import_id, "seq": part.info.seq,
                 "period": {"start": part.info.start, "end": part.info.end}, "build_id": part.info.build_id,
                 "db_sha256": part.info.db_sha256, "recipe_id": part.info.recipe_id,
                 "recipe_sha256": part.info.recipe_sha256, "manifest_artifact": part.info.manifest_artifact,
                 "file_name": part.info.file_name, "raw_sha256": part.raw_sha}
        else:
            d = dict(part)
        d["rows"] = copy.deepcopy(part_rows[k]) if k < len(part_rows) else {}
        keys.append(f"{d['period']['start']}~{d['period']['end']}")
        out_parts.append(d)
    return {
        "format": SNAPSHOT_MANIFEST_FORMAT,
        "source_id": source_id, "snapshot_id": snapshot_id, "mode": "accumulate", "action": action,
        "recipe": recipe, "union": union, "parts": out_parts,
        "columns": columns_doc(report, keys),
        "label_sets": copy.deepcopy(list(label_sets or [])),
        "kinds": dict(notes.kinds),
        "gaps": list(gaps), "replaced": copy.deepcopy(replaced), "removed": copy.deepcopy(removed),
        "checks": copy.deepcopy(report.get("checks") or []),
        "notes": {"templates": dict(notes.templates),
                  "rendered": {t: {"comment": n.comment, "columns": dict(n.columns)} for t, n in notes.tables.items()}},
        "signed_by": {"name": signed_by, "verified": False},
        "created_at": utcnow().isoformat(),
    }


def union_table_report(report: dict[str, Any]) -> dict[str, Any]:
    """check_probe 要的「回执」形状（tables: [{name}]）：并集回执的 tables 是「表 → 列」的字典。"""
    tables = report.get("tables") if isinstance(report.get("tables"), dict) else {}
    return {"tables": [{"name": name} for name in tables]}


# ==========================================================================
# 版本页：列表（7.1）
# ==========================================================================


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


async def _imports_of(session: AsyncSession, ids: set[str]) -> dict[str, TableImport]:
    if not ids:
        return {}
    return {i.id: i for i in (await session.execute(select(TableImport).where(TableImport.id.in_(list(ids))))).scalars()}


async def _builds_of(session: AsyncSession, ids: set[str]) -> dict[str, TableBuild]:
    if not ids:
        return {}
    return {b.id: b for b in (await session.execute(select(TableBuild).where(TableBuild.id.in_(list(ids))))).scalars()}


async def _recipe_seqs(session: AsyncSession, ids: set[str]) -> dict[str, int]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict((await session.execute(select(TableRecipe.id, TableRecipe.seq).where(TableRecipe.id.in_(list(ids)))))
                .all())


def snapshot_recipe_id(snap: SourceSnapshot, imps: dict[str, TableImport]) -> str | None:
    """快照的配方：期 3 之前的快照没记 recipe_id，取最后一期导入的（9.6）；简单导入为 None。"""
    if snap.recipe_id:
        return snap.recipe_id
    last = imps.get(str(snap.imports[-1])) if snap.imports else None
    return last.recipe_id if last is not None else None


def _period_ref(imp: TableImport | None) -> dict[str, Any]:
    if imp is None:
        return {"start": None, "end": None, "file_name": ""}
    return {"start": imp.period_start, "end": imp.period_end, "file_name": imp.file_name or ""}


def _period_key(imp: TableImport) -> tuple[str, ...]:
    """periods_diff 按 (start, end) 比；没有统计期（简单导入）的期没法按统计期认，按导入记录本身认。"""
    if imp.period_start and imp.period_end:
        return ("period", imp.period_start, imp.period_end)
    return ("import", imp.id)


def _file_ok(path: str | None) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _size(path: str | None) -> int | None:
    try:
        return Path(path).stat().st_size if path else None
    except OSError:
        return None


async def _pinned_counts(session: AsyncSession) -> dict[str, int]:
    """快照 id → 引用它的运行数（任何状态），写法同 gc（table_versions._snapshots_in）。"""
    out: dict[str, int] = {}
    for (versions,) in (await session.execute(select(Run.data_versions).where(Run.data_versions.is_not(None)))).all():
        for sid in table_versions._snapshots_in(versions):
            out[sid] = out.get(sid, 0) + 1
    return out


async def list_snapshots(session: AsyncSession, source: DataSource) -> list[dict[str, Any]]:
    """GET /{source_id}/snapshots：最近 50 个版本，当前版本排第一，其余按 coalesce(activated_at, created_at) 倒序。"""
    if (source.origin or "manual") != "upload":
        return []
    snaps = list((await session.execute(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id))).scalars())
    current = next((s for s in snaps if s.id == source.current_snapshot_id), None)
    others = sorted((s for s in snaps if s is not current), key=lambda s: (table_versions._snapshot_time(s), s.id),
                    reverse=True)
    listed = ([current] if current is not None else []) + others
    listed = listed[:SNAPSHOTS_MAX]
    imps = await _imports_of(session, {str(x) for s in listed for x in s.imports or []})
    builds = await _builds_of(session, {i.build_id for i in imps.values()}
                              | {uid for s in listed if (uid := _union_build_id(s))})
    seqs = await _recipe_seqs(session, {snapshot_recipe_id(s, imps) or "" for s in listed})
    pinned = await _pinned_counts(session)
    cur_keys = {_period_key(imps[str(x)]): imps[str(x)] for x in (current.imports or []) if str(x) in imps} \
        if current is not None else {}
    out = []
    for s in listed:
        members = [imps[str(x)] for x in s.imports or [] if str(x) in imps]
        parts = []
        for imp in members:
            b = builds.get(imp.build_id)
            parts.append({
                "import_id": imp.id, "seq": imp.seq, "period_start": imp.period_start, "period_end": imp.period_end,
                "file_name": imp.file_name or "", "raw_state": imp.raw_state, "status": imp.status,
                "rows": _rows_of(b.report if b is not None else None),
                "overrides": len(imp.overrides or []), "waivers": len(imp.waivers or []), "revoked": bool(imp.revoked),
            })
        uid = _union_build_id(s)
        if uid is not None and builds.get(uid) is not None:
            tables = dict((builds[uid].report or {}).get("rows") or {})
        elif members:
            b = builds.get(members[-1].build_id)
            tables = _rows_of(b.report if b is not None else None)
        else:
            tables = {}
        available = s.retired_at is None and _file_ok(s.db_path)
        revoked = any(i.revoked for i in members)
        if s is current:
            code = "current"
        elif s.retired_at is not None:
            code = "retired"
        elif not available:
            code = "file_lost"
        elif revoked:
            code = "contains_revoked"
        else:
            code = None
        rid = snapshot_recipe_id(s, imps)
        mine = {_period_key(i): i for i in members}
        diff = {"added": [_period_ref(i) for k, i in mine.items() if k not in cur_keys],
                "removed": [_period_ref(i) for k, i in cur_keys.items() if k not in mine]}
        out.append({
            "id": s.id, "current": s is current, "mode": s.mode or ("replace" if rid else None),
            "created_at": _iso(s.created_at), "activated_at": _iso(s.activated_at or s.created_at),
            "recipe": {"id": rid, "seq": seqs.get(rid)} if rid else None,
            "parts": parts, "tables": tables,
            "db_sha256_prefix": (s.db_sha256 or "")[:8], "db_size": _size(s.db_path) if available else None,
            "available": available, "pinned_runs": pinned.get(s.id, 0),
            "activatable": code is None, "reason_code": code,
            "reason": SNAPSHOT_NOT_ACTIVATABLE[code] if code else None,
            "mask_lost": table_versions.mask_lost(source.options, s.schema_cache),
            "periods_diff": diff,
        })
    return out


# ==========================================================================
# 版本页：清单（7.3）
# ==========================================================================


async def import_manifest(session: AsyncSession, source: DataSource, import_id: str) -> dict[str, Any]:
    imp = await session.get(TableImport, import_id)
    if imp is None or imp.source_id != source.id:
        raise ImportRefused(404, "import_not_found", "导入记录不存在")
    if not imp.manifest_artifact:
        if imp.recipe_id:
            raise ImportRefused(404, "manifest_missing", "这次按配方导入的导入清单不在了，请联系管理员检查数据目录")
        build = await session.get(TableBuild, imp.build_id) if imp.build_id else None
        return {"artifact_id": None, "verified": False, "kind": "build_report",
                "content": copy.deepcopy(build.report or {}) if build is not None else {}}
    try:
        doc = await asyncio.to_thread(load_manifest, imp.manifest_artifact)
    except ValueError:
        raise ImportRefused(409, "manifest_tampered", "这份导入清单与它的哈希不符，可能被修改过，不能作为证据") from None
    if doc is None:
        raise ImportRefused(404, "manifest_missing", "这次导入的导入清单不在了，请联系管理员检查数据目录")
    return {"artifact_id": imp.manifest_artifact, "verified": True, "kind": "import_manifest", "content": doc}


async def snapshot_manifest_out(session: AsyncSession, source: DataSource, snapshot_id: str) -> dict[str, Any]:
    snap = await session.get(SourceSnapshot, snapshot_id)
    if snap is None or snap.source_id != source.id:
        raise ImportRefused(404, "snapshot_not_found", "这个版本不存在，或不属于这个数据源")
    if snap.manifest_artifact:
        try:
            doc = await asyncio.to_thread(load_manifest, snap.manifest_artifact)
        except ValueError:
            raise ImportRefused(409, "manifest_tampered", "这份版本清单与它的哈希不符，可能被修改过，不能作为证据") from None
        if doc is None:
            raise ImportRefused(404, "manifest_missing", "这个版本的版本清单不在了，请联系管理员检查数据目录")
        return {"artifact_id": snap.manifest_artifact, "verified": True, "kind": "snapshot_manifest", "content": doc}
    imps = await _imports_of(session, {str(x) for x in snap.imports or []})
    return {"artifact_id": None, "verified": False, "kind": "snapshot_view", "content": {
        "mode": snap.mode or "replace",
        "imports": [{"import_id": str(x), "manifest_artifact": imps[str(x)].manifest_artifact if str(x) in imps else None}
                    for x in snap.imports or []],
        "db_sha256": snap.db_sha256, "schema_cache_keys": sorted((snap.schema_cache or {}).keys()),
    }}


# ==========================================================================
# 版本页：启用旧版本（7.2）
# ==========================================================================

#: activate_snapshot 的错误码 → HTTP 状态
_ACTIVATE_STATUS = {"snapshot_not_found": 404}


def _publish_refused(e: table_versions.PublishError) -> ImportRefused:
    """版本页写操作里 publish_snapshot / activate_snapshot 的错误 → ImportRefused（接口层原样转 CodedHTTPException）。"""
    code = e.code or "publish_failed"
    if code == "store_unavailable":
        return ImportRefused(503, code, str(e))
    if code in _ACTIVATE_STATUS:
        return ImportRefused(_ACTIVATE_STATUS[code], code, str(e))
    if code in ("publish_failed", "trial_missing", "raw_missing", "snapshot_conflict"):
        return ImportRefused(500, code, str(e) or "操作失败，当前版本未受影响")
    return ImportRefused(409, code, str(e))


async def activate(session: AsyncSession, source: DataSource, snapshot_id: str, *, expected_current: str | None,
                   ack_mask_lost: list[str] | None, reason: str | None, signed_by: str | None) -> dict[str, Any]:
    """POST …/snapshots/{id}/activate（7.2）。不存在或不属于这个源的先回 404，其余交给 activate_snapshot（锁内）。"""
    snap = await session.get(SourceSnapshot, snapshot_id)
    if snap is None or snap.source_id != source.id:
        raise ImportRefused(404, "snapshot_not_found", "这个版本不存在，或不属于这个数据源")
    source_id = source.id
    try:
        previous = await table_versions.activate_snapshot(
            session, source, snapshot_id, expected_current=expected_current, kind="activate", reason=reason,
            signed_by=signed_by, ack_mask_lost=ack_mask_lost)
    except table_versions.PublishError as e:
        raise _publish_refused(e) from e
    fresh = await session.get(DataSource, source_id, populate_existing=True)
    return {"snapshot_id": snapshot_id, "previous_snapshot_id": previous,
            "recipe_id": fresh.current_recipe_id if fresh is not None else None}


# ==========================================================================
# 版本页：移除一期（7.4）、作废接受（7.5）
# ==========================================================================


@dataclass
class _Current:
    source: DataSource
    snapshot: SourceSnapshot
    imports: list[str]
    mode: str
    recipe: TableRecipe | None


async def _current(session: AsyncSession, source: DataSource) -> _Current:
    if (source.origin or "manual") != "upload" or not source.current_snapshot_id:
        raise ImportRefused(409, "not_upload_source", "这个数据源不是上传的表格，没有可以操作的版本")
    snap = await session.get(SourceSnapshot, source.current_snapshot_id)
    if snap is None:
        raise ImportRefused(409, "snapshot_missing", "当前版本的记录不在了，请联系管理员检查数据目录")
    imps = await _imports_of(session, {str(x) for x in snap.imports or []})
    rid = snapshot_recipe_id(snap, imps)
    recipe = await session.get(TableRecipe, rid) if rid else None
    return _Current(source=source, snapshot=snap, imports=[str(x) for x in snap.imports or []],
                    mode=snap.mode or "replace", recipe=recipe)


def _revoked_record(reason: str, signed_by: str | None) -> dict[str, Any]:
    return {"at": utcnow().isoformat(), "reason": reason, "signed_by": signed_by or None, "signed_by_verified": False}


async def rollback_target(session: AsyncSession, source: DataSource, import_id: str) -> SourceSnapshot | None:
    """替换模式下作废接受的回滚目标（第 5 节，评审一-M8）：同源快照里保留着、可以启用（7.1 的 activatable）、不含
    这条导入、也不含任何已作废导入的，按 coalesce(activated_at, created_at) 倒序的第一个。"""
    snaps = list((await session.execute(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id))).scalars())
    revoked = {i for (i,) in (await session.execute(
        select(TableImport.id).where(TableImport.source_id == source.id, TableImport.revoked.is_not(None)))).all()}
    snaps.sort(key=lambda s: (table_versions._snapshot_time(s), s.id), reverse=True)
    for s in snaps:
        members = {str(x) for x in s.imports or []}
        if (s.id == source.current_snapshot_id or s.retired_at is not None or not _file_ok(s.db_path)
                or import_id in members or members & revoked):
            continue
        return s
    return None


def _accumulating(cur: _Current) -> bool:
    """作废接受走「移除这一期」还是「回滚」：累积模式而且至少两期时移除；只有一期时没有可留下的，按回滚处理。"""
    return cur.mode == "accumulate" and len(cur.imports) >= 2


async def revoke_plan(session: AsyncSession, source: DataSource, imp: TableImport,
                      cur: _Current | None = None) -> dict[str, Any]:
    """导入记录的 revoke_plan（7.6）：确认框照它写，服务端在提交时按同一个定义重算、比对。"""
    cur = cur or await _current(session, source)
    if _accumulating(cur):
        keep = [i for i in cur.imports if i != imp.id]
        imps = await _imports_of(session, set(keep))
        rows = [imps[i] for i in keep if i in imps]
        target = (table_versions.snapshot_id(source.id, keep, recipe_types.RECIPE_ENGINE_VER, mode="accumulate",
                                             recipe_sha=cur.recipe.recipe_sha256) if cur.recipe is not None else None)
        return {"action": "remove_period", "target_snapshot_id": target, "target": None,
                "result_parts": [_period_ref(i) for i in rows],
                "gaps": gaps_of([(i.period_start, i.period_end) for i in rows]), "mask_lost": [], "reason": None}
    target = await rollback_target(session, source, imp.id)
    if target is None:
        return {"action": None, "target_snapshot_id": None, "target": None, "result_parts": [], "gaps": [],
                "mask_lost": [], "reason": _NO_ROLLBACK}
    imps = await _imports_of(session, {str(x) for x in target.imports or []})
    rows = [imps[str(x)] for x in target.imports or [] if str(x) in imps]
    rid = snapshot_recipe_id(target, imps)
    seqs = await _recipe_seqs(session, {rid or ""})
    refs = [_period_ref(i) for i in rows]
    return {"action": "rollback", "target_snapshot_id": target.id,
            "target": {"parts": refs, "recipe_seq": seqs.get(rid) if rid else None,
                       "mode": (target.mode or "replace") if rid else None, "simple": rid is None},
            "result_parts": refs, "gaps": gaps_of([(i.period_start, i.period_end) for i in rows]),
            "mask_lost": table_versions.mask_lost(source.options, target.schema_cache), "reason": None}


async def _rebuild_without(session: AsyncSession, cur: _Current, imp: TableImport, *, expected_current: str | None,
                           kind: str, reason: str, signed_by: str | None, revoke: bool) -> tuple[str, bool]:
    """从当前版本去掉一期、生成（或复用、复活）新快照（7.4）。配方和导入模式不变。返回 (快照 id, 是否复用)。

    revoke=True 时同一个事务里写 table_imports.revoked（7.5）：复用路径在调 activate_snapshot 之前改好（随它的提交
    生效、失败时一起回滚）；新建、复活路径在 before_commit 里写（publish_snapshot 之前不能改会话里的对象）。"""
    source = cur.source
    source_id = source.id
    target = cur.recipe
    if target is None:
        raise ImportRefused(409, "not_accumulate", "当前版本没有配方记录，不能按期移除")
    try:
        target_recipe = Recipe.model_validate(target.recipe)
    except ValueError:
        raise ImportRefused(409, "not_accumulate", "当前版本的配方记录读不出来，不能按期移除") from None
    target_sha, target_id = target.recipe_sha256, target.id
    keep = [i for i in cur.imports if i != imp.id]
    sid = table_versions.snapshot_id(source_id, keep, recipe_types.RECIPE_ENGINE_VER, mode="accumulate",
                                     recipe_sha=target_sha)
    imp_id = imp.id
    revoked = _revoked_record(reason, signed_by) if revoke else None
    activation = {"kind": kind, "import_id": imp_id, "reason": reason, "signed_by": signed_by}

    existing = await session.get(SourceSnapshot, sid)
    if (existing is not None and existing.retired_at is None and existing.source_id == source_id
            and await asyncio.to_thread(table_versions._file_matches, existing.db_path, existing.db_sha256)):
        # 同一组导入、同一个目标配方、同为累积：内容必然相同，直接切过去（7.4 第 3 步）。遮罩：目标配方不变，表结构
        # 只可能多出退役列，不会少列（7.4 第 6 步），所以把算出的遮罩丢失原样作为确认传下去，不让它额外拦一道
        lost = table_versions.mask_lost(source.options, existing.schema_cache)
        if revoked is not None:
            imp.revoked = revoked
        try:
            await table_versions.activate_snapshot(
                session, source, sid, expected_current=expected_current, kind=kind, reason=reason,
                signed_by=signed_by, import_id=imp_id, ack_mask_lost=lost)
        except table_versions.PublishError as e:
            raise _publish_refused(e) from e
        return sid, True

    # 已回收的同 id 快照要复活时，配方记录可能与目标配方不是同一条（内容相同：快照 id 按配方哈希算），由
    # publish_snapshot 按哈希认出同一个版本，现行配方跟着那个快照记的配方记录走（与上面「直接切过去」的路径一致）
    try:
        parts = await load_parts(session, cur.snapshot, exclude=imp_id)
        manifest = snapshot_manifest(cur.snapshot)
    except PartProblem as e:
        raise ImportRefused(409, e.code, e.message) from e
    cur_tables = tables_in_parts(await current_tables(session, cur.snapshot), parts)
    status = retired_status(manifest)
    p = pipeline()
    single = len(parts) == 1 and parts[0].info.recipe_sha256 == target_sha
    union_build: table_versions.UnionBuild | None = None
    report_json: dict[str, Any] | None = None
    label_sets: list[Any] = []
    gaps = gaps_of([(x.info.start, x.info.end) for x in parts])
    out: Path | None = None
    try:
        if single:
            notes = await table_versions_slot(single_notes, parts[0])
        else:
            from app.data import recipe_accumulate as ra

            classify = p.classify_changes or ra.classify_changes
            cc = classify([x.info for x in parts], target_recipe, current_tables=cur_tables, retired_status=status)
            if cc.semantic:
                raise ImportRefused(409, "union_failed", "当前配方与剩下的某一期不兼容，无法重建当前版本："
                                    + "；".join(cc.semantic[:3]))
            label_sets = list(cc.label_sets)
            retired = ra.retired_columns(cc, cur_tables)
            triples = [(x.info.start, x.info.end, x.info.build_id) for x in parts]
            uid = ra.union_id(source_id, triples, target_sha, retired=retired)
            final = table_versions.union_path(source_id, uid)
            out = final.with_name(f"{final.name}{raw_store.TMP_MARK}{secrets.token_hex(6)}")
            materialize = p.materialize_union or ra.materialize_union
            union_parts = [x.union_part() for x in parts]
            report = await table_versions_slot(materialize, out, target=target_recipe, parts=union_parts,
                                               retired=retired)
            ra.require_union_ok(report)
            report_json = _jsonable(report)
            build_union_notes = p.build_union_notes
            if build_union_notes is None:
                from app.data.recipe_notes import build_union_notes
            note_parts = [await asyncio.to_thread(note_part, x) for x in parts]
            notes = build_union_notes(
                target_recipe, note_parts, union_tables=report.tables, null_counts=report.null_counts,
                part_null_counts=report.part_null_counts, added=report.added, retired=report.retired,
                label_sets=label_sets, gaps=bool(gaps), dropped=False)
            union_build = table_versions.UnionBuild(
                tmp_db=out, union_id=uid, options=ra.union_options(triples, target_sha, retired=retired),
                report=report_json, expected_sha256=report.db_sha256)
    except PartProblem as e:
        _drop(out)
        raise ImportRefused(409, e.code, e.message) from e
    except ImportRefused:
        _drop(out)
        raise
    except Exception as e:
        _drop(out)
        from app.data.recipe_accumulate import UnionError

        if isinstance(e, UnionError):
            raise ImportRefused(409, e.code, str(e)) from e
        raise
    if notes.problems:
        _drop(out)
        raise ImportRefused(422, "notes_invalid", "说明没有通过检查：" + "；".join(notes.problems[:5]))

    manifests = [x.info.manifest_artifact for x in parts]
    removed = {"import_id": imp_id, "seq": imp.seq, "start": imp.period_start, "end": imp.period_end,
               "file_name": imp.file_name or ""}
    action = "revoke_acceptance" if revoke else "remove_period"

    async def schema_cache_for(view: table_versions.SourceView, info: table_versions.SnapshotInfo) -> dict[str, Any]:
        from app.core import artifact_store

        if union_build is not None:
            probe = union_table_report(info.union_report or report_json or {})
        else:
            probe = {"tables": [{"name": name} for name in parts[0].info.rows]} if parts[0].info.rows else \
                {"tables": [{"name": t} for t in notes.tables]}
        cache = await table_versions.introspect_view(view, probe)
        cache = p.apply_notes(cache, notes)
        if notes.problems:
            raise table_versions.PublishError("说明没能写进表结构（" + "；".join(notes.problems[:3]) + "），当前版本未受影响")
        table_versions.check_probe(cache, probe, source_id)
        cache["import_manifests"] = [m for m in manifests if m]
        if union_build is not None:
            doc = manifest_doc(
                source_id=source_id, snapshot_id=info.snapshot_id, action=action,
                recipe={"id": target_id, "seq": target.seq, "sha256": target_sha},
                union={"build_id": info.union_id, "db_sha256": info.union_db_sha256,
                       "table_hashes": dict((info.union_report or report_json or {}).get("table_hashes") or {}),
                       "union_ver": recipe_types.UNION_VER},
                parts=parts, report=info.union_report or report_json or {}, label_sets=label_sets, gaps=gaps,
                replaced=None, removed=removed, notes=notes, signed_by=signed_by)
            cache["snapshot_manifest"] = await artifact_store.put_json(
                doc, kind="snapshot_manifest", meta={"source_id": source_id, "snapshot_id": info.snapshot_id})
        return cache

    async def before_commit(_new_import: str | None, _snap_id: str) -> None:
        if revoked is not None:
            row = await session.get(TableImport, imp_id)
            if row is not None:
                row.revoked = revoked

    try:
        pub = await table_versions.publish_snapshot(
            session, source, imports=keep, mode="accumulate", recipe_id=target_id, recipe_sha256=target_sha,
            expected_current=expected_current, period_build=None, new_import=None, union=union_build,
            schema_cache_for=schema_cache_for, before_commit=before_commit, activation=activation)
    except table_versions.PublishError as e:
        raise _publish_refused(e) from e
    finally:
        _drop(out)
    return pub.snapshot_id, bool(pub.snapshot_reused or pub.snapshot_revived)


def _drop(path: Path | None) -> None:
    if path is None:
        return
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _jsonable(obj: Any) -> Any:
    from app.data.recipe_imports import _jsonable as conv

    return conv(obj)


async def table_versions_slot(fn: Any, *args: Any, **kw: Any) -> Any:
    """CPU 密集的一步（物化、查空值数）：占一个解析名额、放进线程池（与试运行同一套名额）。"""
    async with table_versions.parse_slot():
        return await asyncio.to_thread(fn, *args, **kw)


def _check_target(cur: _Current, imp: TableImport) -> None:
    if imp.id not in cur.imports:
        raise ImportRefused(409, "not_in_current", "这一期不在当前版本里，请刷新版本列表后重试")


def _check_base(cur: _Current, expected_current: str | None) -> None:
    """锁外读到的当前版本必须就是确认框依据的那个（expected_current），否则 409 base_changed。

    为什么锁内的比对不够：留下哪几期（keep）、替换模式的回滚目标，都是按这里读到的当前版本算的。锁内只比
    「指针此刻是不是 expected_current」，挡不住 ABA：用户按 E=[7,8,9] 发「移除 8 月」，处理器读到的却已是别人移除
    9 月之后的 C=[7,8]，算出 keep=[7]；处理过程中又有人把 E 启用回来，锁内 E==E 通过，接口回 200，当前版本却只剩
    [7]，9 月悄悄丢了，确认框写的是 [7,9]（评审意见）。快照 id 按内容算、含 imports，这里相等就说明 keep 的来源与
    确认框一致；锁内再比一次，管的是这之后的变化。"""
    if expected_current != cur.snapshot.id:
        raise ImportRefused(409, "base_changed", table_versions._BASE_CHANGED)


async def remove_period(session: AsyncSession, source: DataSource, import_id: str, *, expected_current: str | None,
                        reason: str, signed_by: str | None) -> dict[str, Any]:
    """POST …/imports/{id}/remove（7.4）。"""
    imp = await session.get(TableImport, import_id)
    if imp is None or imp.source_id != source.id:
        raise ImportRefused(404, "import_not_found", "导入记录不存在")
    # 只读进程不写版本存储（第 8 节遗留项 5）：在组装各期、物化并集之前就停下。publish_snapshot 进锁后也会拒，但
    # 那时并集的临时文件已经写进 snapshots/，各期哈希、说明也都白算了（评审意见）
    table_versions.require_store_writable()
    cur = await _current(session, source)
    _check_base(cur, expected_current)
    _check_target(cur, imp)
    if cur.mode != "accumulate":
        raise ImportRefused(409, "not_accumulate", "当前版本不是按期累积的，不能移除其中一期；可以在「历史版本」中启用更早的版本")
    if len(cur.imports) < 2:
        raise ImportRefused(409, "last_period", _LAST_PERIOD)
    sid, reused = await _rebuild_without(session, cur, imp, expected_current=expected_current, kind="remove_period",
                                         reason=reason, signed_by=signed_by, revoke=False)
    return {"snapshot_id": sid, "removed_import_id": import_id, "reused": reused}


async def revoke_acceptance(session: AsyncSession, source: DataSource, import_id: str, *,
                            expected_current: str | None, expected_target: str | None,
                            ack_mask_lost: list[str] | None, reason: str, signed_by: str | None) -> dict[str, Any]:
    """POST …/imports/{id}/revoke-acceptance（7.5）。累积模式（至少两期）= 移除这一期；否则回滚到第 5 节定义的目标。"""
    imp = await session.get(TableImport, import_id)
    if imp is None or imp.source_id != source.id:
        raise ImportRefused(404, "import_not_found", "导入记录不存在")
    if not (imp.overrides or imp.waivers):
        raise ImportRefused(409, "no_acceptance", "这一期没有写理由接受的核对，不需要作废")
    table_versions.require_store_writable()
    cur = await _current(session, source)
    # 替换模式的回滚目标也是在锁外按这里读到的当前版本算的：同样先确认它就是确认框依据的那个（ABA，见 _check_base）
    _check_base(cur, expected_current)
    _check_target(cur, imp)
    if _accumulating(cur):
        sid, _reused = await _rebuild_without(session, cur, imp, expected_current=expected_current,
                                              kind="revoke_acceptance", reason=reason, signed_by=signed_by,
                                              revoke=True)
        return {"snapshot_id": sid, "action": "remove_period"}
    target = await rollback_target(session, source, imp.id)
    if target is None:
        raise ImportRefused(409, "no_rollback_target", _NO_ROLLBACK)
    if target.id != expected_target:
        raise ImportRefused(409, "revoke_target_changed", _TARGET_CHANGED)
    target_id = target.id
    imp.revoked = _revoked_record(reason, signed_by)
    try:
        await table_versions.activate_snapshot(
            session, source, target_id, expected_current=expected_current, kind="revoke_acceptance", reason=reason,
            signed_by=signed_by, import_id=import_id, ack_mask_lost=ack_mask_lost)
    except table_versions.PublishError as e:
        raise _publish_refused(e) from e
    return {"snapshot_id": target_id, "action": "rollback"}


# ==========================================================================
# 导入记录（7.6）
# ==========================================================================


def _acceptance_rows(imp: TableImport) -> list[dict[str, Any]]:
    out = []
    for kind, recs in (("override", imp.overrides), ("waiver", imp.waivers)):
        for rec in recs or []:
            if isinstance(rec, dict):
                out.append({"check_id": rec.get("check_id"), "kind": kind, "reason": rec.get("reason"),
                            "signed_by": rec.get("signed_by"), "at": rec.get("at")})
    return out


async def import_records(session: AsyncSession, source: DataSource, imps: list[TableImport]) -> list[dict[str, Any]]:
    """GET /{source_id}/imports 的行（期 1 的字段照旧，期 3 补字段，7.6）。几样一次查齐，不逐条查。"""
    current_ids: set[str] = set()
    cur: _Current | None = None
    if source.current_snapshot_id:
        snap = await session.get(SourceSnapshot, source.current_snapshot_id)
        current_ids = {str(x) for x in (snap.imports or [])} if snap is not None else set()
        if snap is not None and (source.origin or "manual") == "upload":
            try:
                cur = await _current(session, source)
            except ImportRefused:
                cur = None
    builds = await _builds_of(session, {i.build_id for i in imps if i.build_id})
    seqs = await _recipe_seqs(session, {i.recipe_id or "" for i in imps})
    shas = {i.raw_sha256 for i in imps if i.raw_sha256}
    shared: dict[str, list[tuple[str, str | None]]] = {}
    opened: dict[str, int] = {}
    if shas:
        rows = (await session.execute(
            select(TableImport.id, TableImport.raw_sha256, TableImport.source_id, DataSource.name)
            .join(DataSource, DataSource.id == TableImport.source_id, isouter=True)
            .where(TableImport.raw_sha256.in_(list(shas)), TableImport.raw_state == "kept"))).all()
        for iid, sha, _src, name in rows:
            shared.setdefault(sha, []).append((iid, name if name is not None else _DELETED_SOURCE))
        opened = dict((await session.execute(
            select(ImportStaging.raw_sha256, func.count())
            .where(ImportStaging.raw_sha256.in_(list(shas)), ImportStaging.status.in_(table_versions.STAGING_OPEN))
            .group_by(ImportStaging.raw_sha256))).all())
    out = []
    for imp in imps:
        in_current = imp.id in current_ids
        refs = [name for iid, name in shared.get(imp.raw_sha256 or "", []) if iid != imp.id]
        by_name: dict[str | None, int] = {}
        for name in refs:
            by_name[name] = by_name.get(name, 0) + 1
        plan = None
        if in_current and (imp.overrides or imp.waivers) and not imp.revoked and cur is not None:
            plan = await revoke_plan(session, source, imp, cur)
        b = builds.get(imp.build_id)
        out.append({
            "id": imp.id, "seq": imp.seq, "build_id": imp.build_id,
            "file_name": imp.file_name, "file_size": imp.file_size, "raw_sha256": imp.raw_sha256,
            "raw_state": imp.raw_state, "status": imp.status, "purged": imp.purged,
            "current": in_current,
            "created_at": _iso(imp.created_at), "activated_at": _iso(imp.activated_at),
            # 按配方导入才有（简单导入为空）：配方、统计期、署名，接受的条数
            "recipe_id": imp.recipe_id, "period_start": imp.period_start, "period_end": imp.period_end,
            "signed_by": imp.signed_by, "overrides": len(imp.overrides or []), "waivers": len(imp.waivers or []),
            # 期 3（7.6）
            "acceptances": _acceptance_rows(imp), "revoked": imp.revoked, "manifest_artifact": imp.manifest_artifact,
            "recipe_seq": seqs.get(imp.recipe_id) if imp.recipe_id else None, "in_current": in_current,
            "rows": _rows_of(b.report if b is not None else None), "revoke_plan": plan,
            "raw_shared_with": [{"source_name": name, "count": n} for name, n in by_name.items()],
            "raw_open_stagings": int(opened.get(imp.raw_sha256, 0)) if imp.raw_sha256 else 0,
        })
    return out


__all__ = [
    "Part", "PartProblem", "REASON_MAX", "activate", "current_tables", "gaps_of", "import_manifest",
    "import_records", "list_snapshots", "load_manifest", "load_parts", "manifest_doc", "note_part", "remove_period",
    "retired_names", "retired_status", "revoke_acceptance", "revoke_plan", "rollback_target", "single_notes",
    "snapshot_manifest", "snapshot_manifest_out", "tables_in_parts", "union_table_report",
]
