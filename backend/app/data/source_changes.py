"""数据源的变化记录（docs/design/next-round/03-数据接入与目录维护.md B3）：结构变化、取值变化的记录、去重、处理和拉取。

**为什么要有它。** 以前表结构变了、码值多了一个新取值都没有地方记：审阅页看不到，通知发不了，口径账本的召回也没有可以
订阅的源。现在全站只有 source_changes 这一份记录（格式 agentlab-change/1）：

- **记**（record）：后台探查读到的结构差异（data/schema_diff → introspect_jobs）、取值观察和同口径历史的比较
  （data/value_drift → catalog_profile_observe）都经这里写。fingerprint 去重：同一个变化再次测到只更新 last_seen_at、
  seen_count（还开着的顺带换上最新的影响面和严重程度），不另起一条。
- **处理**（handle / resolve）：动作都落到目录已有的写入接口上——清理目录里说到被删的列的项（catalog.write_entry，
  整份写、乐观锁）、加入码值（catalog.save_patch：apply_patch 配 _merge_codes，含义待填写，依据写这条变化的编号）、重新
  起草（建起草任务）、重新剖析（建剖析任务）、知道了、误报（必须写理由）。每个动作写 resolution，动了目录的记下目录版本，
  建了任务的记下任务编号。
- **拉取**（list_changes / affecting）：给召回按 seq 游标拉；seq 自增、不复用。触发召回的只有结构类、处理时改了目录的、
  以及 rules_touched 的新码值（triggers_recall）；纯取值漂移只算数据质量提醒。
- **说法**（summary）：一条变化给人看的一句（接口、日志用）；通知里不放它（不放表名、列名、取值，D9），见
  notify/service.notify_source_changed。

不记遮罩列的取值（观察查询本来就不碰遮罩列）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import schema_diff as sd
from app.data.names import name_key
from app.db.base import utcnow
from app.db.models_sources import SourceChange

logger = logging.getLogger(__name__)

SCHEMA = "agentlab-change/1"

#: 取值类的变化（data/value_drift）
CODES_NEW = "codes_new"
CODES_SHIFT = "codes_shift"
DATE_STALLED = "date_stalled"
DATE_GAP = "date_gap"
VOLUME_DROP = "volume_drop"
ROWS_DROP = "rows_drop"
ROWS_JUMP = "rows_jump"
COVERAGE_DROP = "coverage_drop"
VALUE_KINDS = (CODES_NEW, CODES_SHIFT, DATE_STALLED, DATE_GAP, VOLUME_DROP, ROWS_DROP, ROWS_JUMP, COVERAGE_DROP)
KINDS = sd.STRUCT_KINDS + VALUE_KINDS

SEVERITIES = ("info", "warning", "action")
SEVERITY_RANK = {"info": 1, "warning": 2, "action": 3}
STATUSES = ("open", "acknowledged", "resolved", "dismissed")
#: 还要人看的：开着的、知道了但还没处理的
PENDING = ("open", "acknowledged")

#: 处理动作：clean_catalog 清理目录里说到这些列的项、add_codes 加入码值、redraft 重新起草这张表、reprofile 重新剖析、
#: acknowledge 知道了、dismiss 误报（必须写理由）
ACTIONS = ("clean_catalog", "add_codes", "redraft", "reprofile", "acknowledge", "dismiss")
#: 动作之后的状态
ACTION_STATUS = {"clean_catalog": "resolved", "add_codes": "resolved", "redraft": "resolved", "reprofile": "resolved",
                 "acknowledge": "acknowledged", "dismiss": "dismissed"}
#: 理由最长几个字
NOTE_MAX = 500

#: 变化的叫法（接口给人看的 summary 用；前端第 4 波的变化标签页在 lib/terms.ts 另有一份，逐字对照）
KIND_LABEL = {
    sd.TABLE_ADDED: "新出现的表", sd.TABLE_REMOVED: "表已删除", sd.TABLE_LEFT_SCOPE: "表离开了探查范围",
    sd.TABLE_REFLECT_FAILED: "这次读不到表结构", sd.COLUMN_ADDED: "新加了列", sd.COLUMN_REMOVED: "删除了列",
    sd.COLUMN_TYPE: "列的类型变了", sd.COLUMN_NULLABLE: "列的可空变了", sd.KEY: "键变了", sd.FK: "外键变了",
    sd.COMMENT: "注释变了", sd.SCOPE: "探查范围的规则变了",
    CODES_NEW: "出现了新码值", CODES_SHIFT: "码值的比例变化大", DATE_STALLED: "最新业务日期没有前进",
    DATE_GAP: "有的日子没有数据", VOLUME_DROP: "按日行数大幅下跌", ROWS_DROP: "行数大幅下跌", ROWS_JUMP: "行数大幅上涨",
    COVERAGE_DROP: "关联的覆盖率下降",
}


class ChangeNotFound(Exception):
    pass


class ChangeFinished(Exception):
    """这条变化已经处理过（resolved / dismissed）了。"""


class ChangeInvalid(ValueError):
    """处理动作不对：不认识的动作、误报没写理由、这种变化做不了这个动作。message 给人看。"""


# ==========================================================================
# 记
# ==========================================================================


def fingerprint(*parts: Any) -> str:
    """去重用的指纹：几段内容的规范 JSON 的 sha256。"""
    text = json.dumps(list(parts), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Detected:
    """谁、经什么测到的（detected_by）：job_kind 是 introspect / profile / on_demand，trigger 是 manual / schedule /
    on_demand，actor 是署名。"""

    job_kind: str
    job_id: str | None = None
    task_seq: int | None = None
    trigger: str = "manual"
    actor: str | None = None
    fire_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"job_kind": self.job_kind, "job_id": self.job_id, "task_seq": self.task_seq, "trigger": self.trigger,
               "actor": self.actor}
        if self.fire_at:
            out["fire_at"] = self.fire_at
        return out


@dataclass
class NewChange:
    """要记的一条变化（还没去重）。key 是去重指纹的组成部分（除了源、种类、表、列以外的那部分：结构版本、码值……）。"""

    kind: str
    table: str
    column: str | None = None
    severity: str = "info"
    before: Any = None
    after: Any = None
    basis: dict[str, Any] | None = None
    affects: dict[str, Any] | None = None
    struct_version_before: int | None = None
    struct_version_after: int | None = None
    key: Any = None


async def record(session: AsyncSession, source: Any, change: NewChange, detected: Detected) -> tuple[SourceChange, bool]:
    """记一条变化（不提交）。同一个指纹已经有了：只更新 last_seen_at、seen_count（还开着的另换上最新的影响面、前后值、
    严重程度），返回 (那一条, False)；新的返回 (新的一条, True)。"""
    if change.kind not in KINDS:
        raise ValueError(f"不认识的变化种类：{change.kind}")
    if change.severity not in SEVERITIES:
        raise ValueError(f"不认识的严重程度：{change.severity}")
    fp = fingerprint(change.kind, change.table, change.column, change.key)
    now = utcnow()
    found = (await session.execute(select(SourceChange).where(
        SourceChange.source_id == source.id, SourceChange.fingerprint == fp))).scalar_one_or_none()
    if found is not None:
        found.last_seen_at = now
        found.seen_count = int(found.seen_count or 0) + 1
        if found.status == "open":
            found.affects, found.before, found.after = change.affects, change.before, change.after
            if SEVERITY_RANK[change.severity] > SEVERITY_RANK.get(found.severity, 0):
                found.severity = change.severity
        return found, False
    row = SourceChange(source_id=source.id, source_name=str(getattr(source, "name", "") or ""),
                       table_name=change.table or "", column_name=change.column, kind=change.kind,
                       severity=change.severity, detected_at=now, last_seen_at=now, seen_count=1,
                       detected_by=detected.to_dict(), basis=change.basis, before=change.before, after=change.after,
                       affects=change.affects, struct_version_before=change.struct_version_before,
                       struct_version_after=change.struct_version_after, fingerprint=fp, status="open",
                       schema_tag=SCHEMA)
    try:
        async with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # 同一个指纹刚被别处写进去（单进程下几乎不会）：按已有的那条更新
        found = (await session.execute(select(SourceChange).where(
            SourceChange.source_id == source.id, SourceChange.fingerprint == fp))).scalar_one()
        found.last_seen_at, found.seen_count = now, int(found.seen_count or 0) + 1
        return found, False
    return row, True


async def record_all(session: AsyncSession, source: Any, changes: Iterable[NewChange],
                     detected: Detected) -> list[SourceChange]:
    """一批变化逐条 record（不提交），返回这次新记的那几条。"""
    fresh: list[SourceChange] = []
    for change in changes:
        row, new = await record(session, source, change, detected)
        if new:
            fresh.append(row)
    return fresh


# ==========================================================================
# 读
# ==========================================================================


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def summary(row: Any) -> str:
    """一条变化给人看的一句（接口、日志）：「orders：删除了列 amount（可能是改名为 total，未确认）」。"""
    kind = row.kind
    label = KIND_LABEL.get(kind, kind)
    table = row.table_name or ""
    column = row.column_name
    where = f"{table}.{column}" if table and column else table
    text = f"{where}：{label}" if where else label
    after = row.after if isinstance(row.after, Mapping) else {}
    before = row.before if isinstance(row.before, Mapping) else {}
    rename = after.get("rename") or before.get("rename")
    if isinstance(rename, Mapping):
        text += f"（可能是 {rename.get('from')} 改名为 {rename.get('to')}，未确认）"
    elif kind == sd.COLUMN_TYPE:
        text += f"（{row.before} → {row.after}）"
    elif kind == CODES_NEW and isinstance(after.get("codes"), list):
        text += f"（{'、'.join(str(c) for c in after['codes'][:8])}）"
    return text


def change_out(row: SourceChange) -> dict[str, Any]:
    """接口给的一条变化（agentlab-change/1）。"""
    return {
        "schema": row.schema_tag or SCHEMA, "seq": row.seq, "id": row.id, "source_id": row.source_id,
        "source_name": row.source_name, "table": row.table_name or None, "column": row.column_name, "kind": row.kind,
        "severity": row.severity, "summary": summary(row), "detected_at": _iso(row.detected_at),
        "last_seen_at": _iso(row.last_seen_at), "seen_count": row.seen_count or 1,
        "detected_by": dict(row.detected_by or {}), "basis": row.basis, "before": row.before, "after": row.after,
        "affects": row.affects or {"catalog": [], "templates": [], "rules_touched": False},
        "struct_version_before": row.struct_version_before, "struct_version_after": row.struct_version_after,
        "status": row.status, "resolution": row.resolution,
    }


async def load(session: AsyncSession, source_id: str, change_id: str) -> SourceChange | None:
    row = (await session.execute(select(SourceChange).where(SourceChange.id == change_id))).scalar_one_or_none()
    return row if row is not None and row.source_id == source_id else None


async def counts(session: AsyncSession, source_id: str) -> dict[str, Any]:
    """一个源还要人看的变化（开着的、知道了还没处理的）：{pending, by_severity: {action, warning, info}, open}。数据源卡片的
    角标「N 个变化要处理」按 by_severity.action（第 4 波）。"""
    rows = (await session.execute(select(SourceChange.status, SourceChange.severity, func.count()).where(
        SourceChange.source_id == source_id, SourceChange.status.in_(PENDING))
        .group_by(SourceChange.status, SourceChange.severity))).all()
    by = dict.fromkeys(SEVERITIES, 0)
    opened = 0
    for status, severity, n in rows:
        by[severity] = by.get(severity, 0) + n
        if status == "open":
            opened += n
    return {"pending": sum(by.values()), "open": opened, "by_severity": by}


async def list_for_source(session: AsyncSession, source: Any, *, status: str | None = "pending",
                          kinds: Iterable[str] | None = None, table: str | None = None, limit: int = 100,
                          offset: int = 0) -> tuple[list[SourceChange], int]:
    """一个源的变化（审阅页「变化」标签页）：按「使用次数权重 × 严重程度」排（catalog_review 的权重），同分新的在前。
    status：pending 开着的和知道了的（缺省）、all、或者某一个状态。返回 (这一页, 一共几条)。"""
    from app.data import catalog_review

    filt = [SourceChange.source_id == source.id]
    if status == "pending":
        filt.append(SourceChange.status.in_(PENDING))
    elif status and status != "all":
        filt.append(SourceChange.status == status)
    if kinds:
        filt.append(SourceChange.kind.in_(list(kinds)))
    if table:
        filt.append(SourceChange.table_name == table)
    total = int((await session.execute(select(func.count()).select_from(SourceChange).where(*filt))).scalar() or 0)
    rows = list((await session.execute(select(SourceChange).where(*filt))).scalars())
    usage = (await catalog_review.table_usage_counts(session, source)).queries if rows else {}

    def rank(r: SourceChange) -> tuple[float, int]:
        weight = catalog_review.usage_weight(usage.get(r.table_name, 0)) * SEVERITY_RANK.get(r.severity, 1)
        return -weight, -int(r.seq or 0)

    rows.sort(key=rank)
    return rows[offset:offset + limit], total


async def list_changes(session: AsyncSession, *, since_seq: int = 0, source_id: str | None = None,
                       kinds: Iterable[str] | None = None, status: Iterable[str] | str | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
    """按 seq 游标拉取（给召回）：seq 大于 since_seq 的，按 seq 从小到大，最多 limit 条。拉到的最后一条的 seq 就是下一次的
    since_seq。"""
    filt = [SourceChange.seq > int(since_seq or 0)]
    if source_id:
        filt.append(SourceChange.source_id == source_id)
    if kinds:
        filt.append(SourceChange.kind.in_(list(kinds)))
    if isinstance(status, str):
        filt.append(SourceChange.status == status)
    elif status:
        filt.append(SourceChange.status.in_(list(status)))
    rows = (await session.execute(select(SourceChange).where(*filt).order_by(SourceChange.seq)
                                  .limit(max(1, min(int(limit), 1000))))).scalars()
    return [change_out(r) for r in rows]


async def affecting(session: AsyncSession, source_name: str, tables: Iterable[str], since: int | datetime | None = None,
                    ) -> list[dict[str, Any]]:
    """动到这个源这几张表的变化（给召回反查「这次答案用到的表之后有没有变」）：表名按 name_key 比；since 是 seq（拉取的
    游标）或时刻（测到的时间晚于它的）。源级的变化（范围规则改了）不算。"""
    keys = {name_key(str(t).rsplit(".", 1)[-1]) for t in tables if t}
    if not keys:
        return []
    filt = [SourceChange.source_name == source_name, SourceChange.table_name != ""]
    if isinstance(since, datetime):
        filt.append(or_(SourceChange.detected_at > since, SourceChange.last_seen_at > since))
    elif since:
        filt.append(SourceChange.seq > int(since))
    rows = (await session.execute(select(SourceChange).where(*filt).order_by(SourceChange.seq))).scalars()
    return [change_out(r) for r in rows if name_key(r.table_name) in keys]


def triggers_recall(change: Mapping[str, Any]) -> bool:
    """这条变化触发不触发召回：结构类、处理时改了目录的（resolution 里有目录版本）、触及有效记录条件的新码值。纯取值漂移
    只算数据质量提醒。"""
    kind = change.get("kind")
    if kind in sd.STRUCT_KINDS:
        return True
    if (change.get("resolution") or {}).get("catalog_version"):
        return True
    return kind == CODES_NEW and bool((change.get("affects") or {}).get("rules_touched"))


async def notify_after_job(source_id: str, job_id: str) -> int:
    """一次探查、剖析任务结束时：这次新记了还开着的「要处理」级变化，就在后台按源推一条 source.changed（不等它推完）。返回
    这次新记了几条要处理的。出错只记日志：通知是附加的。"""
    from app.db.base import SessionLocal

    try:
        async with SessionLocal() as session:
            n = int((await session.execute(select(func.count()).select_from(SourceChange).where(
                SourceChange.source_id == source_id, SourceChange.severity == "action",
                SourceChange.status == "open",
                func.json_extract(SourceChange.detected_by, "$.job_id") == job_id))).scalar() or 0)
        if n:
            from app.notify import service

            service.spawn(service.notify_source_changed(source_id))
        return n
    except Exception:  # noqa: BLE001
        logger.exception("任务 %s 结束后推送数据源变化的通知时出错", job_id)
        return 0


# ==========================================================================
# 处理
# ==========================================================================


def resolve(row: SourceChange, *, action: str, by: str | None, note: str | None = None,
            catalog_version: int | None = None, job_id: str | None = None) -> SourceChange:
    """把一条变化记成处理过（不提交）：状态按动作（ACTION_STATUS），resolution 写谁、什么时候、什么动作、理由、目录版本、
    任务编号。已经 resolved / dismissed 的抛 ChangeFinished；误报没写理由抛 ChangeInvalid。"""
    if action not in ACTIONS:
        raise ChangeInvalid(f"不认识的处理动作：{action}。可选：{'、'.join(ACTIONS)}")
    if row.status in ("resolved", "dismissed"):
        raise ChangeFinished("这条变化已经处理过了")
    text = (note or "").strip()
    if action == "dismiss" and not text:
        raise ChangeInvalid("标为误报要写明理由")
    if len(text) > NOTE_MAX:
        raise ChangeInvalid(f"理由最多 {NOTE_MAX} 个字")
    resolution: dict[str, Any] = {"by": by, "at": utcnow().isoformat(), "action": action, "note": text or None}
    if catalog_version is not None:
        resolution["catalog_version"] = catalog_version
    if job_id is not None:
        resolution["job_id"] = job_id
    row.status = ACTION_STATUS[action]
    row.resolution = resolution
    return row


def _catalog_paths(row: SourceChange) -> dict[str, list[str]]:
    """影响面里的目录项，按表分（清理目录时删的就是它们）：这张表上的，和别的表指向它的关联关系。"""
    out: dict[str, list[str]] = {}
    for item in (row.affects or {}).get("catalog") or []:
        if isinstance(item, Mapping) and item.get("table") and item.get("path"):
            out.setdefault(str(item["table"]), []).append(str(item["path"]))
    return out


def without_paths(notes: Mapping[str, Any], paths: Iterable[str]) -> dict[str, Any]:
    """目录去掉这几项（路径同审阅接口：字段名、columns.<列>.<字段>、relations.<编号>）。"""
    from app.data import catalog

    slots = catalog._slots(notes)
    for path in paths:
        try:
            slots.pop(catalog.parse_path(path), None)
        except catalog.CatalogPathError:
            continue
    return catalog._assemble(slots)


async def handle(session: AsyncSession, source: Any, row: SourceChange, action: str, *, actor: str | None,
                 note: str | None = None, codes: Mapping[str, str] | None = None) -> dict[str, Any]:
    """执行一个处理动作并记下 resolution（提交）。返回 {change, catalog_version?, job?}。

    - clean_catalog：把影响面里说到这些列（这张表）的目录项去掉——这张表上的、别的表指向它的关联关系——逐表整份写入
      （乐观锁），记这张表的目录版本（别的表也动了的另记 catalog_versions）。影响面里没有目录项的抛 ChangeInvalid。
    - add_codes：码值的新取值加进目录（catalog.save_patch，含义待填写；依据写「数据源变化 #编号」），只给 codes_new。codes
      给了就按它加（{码: 含义}，含义可以空），不给就加这条变化测到的新码值。
    - redraft：建一个只含这张表的起草任务；reprofile：建一个只含这张表的剖析任务。建不了（模型没配、剖析没开、同源正忙）
      抛 ChangeInvalid，原因照实。
    - acknowledge、dismiss（必须写理由）只改状态。
    """
    from app.data import catalog

    if action not in ACTIONS:
        raise ChangeInvalid(f"不认识的处理动作：{action}。可选：{'、'.join(ACTIONS)}")
    if row.status in ("resolved", "dismissed"):
        raise ChangeFinished("这条变化已经处理过了")
    if action == "dismiss" and not (note or "").strip():
        raise ChangeInvalid("标为误报要写明理由")
    out: dict[str, Any] = {}
    version: int | None = None
    job_id: str | None = None
    table = row.table_name
    if action == "clean_catalog":
        by_table = _catalog_paths(row)
        if not by_table:
            raise ChangeInvalid("这条变化的影响面里没有目录项，不需要清理")
        versions: dict[str, int] = {}
        for owner, paths in by_table.items():
            entry = await catalog.read_entry(session, source.id, owner)
            if entry is None:
                continue
            written = await catalog.write_entry(session, source.id, owner, without_paths(entry.notes, paths),
                                                if_version=entry.version, actor=actor, commit=False)
            versions[owner] = written.version
        if not versions:
            raise ChangeInvalid(f"{table} 已经没有数据目录了")
        # 记这张表的目录版本；别的表（指向它的关联关系）也动了的，另记在 catalog_versions 里
        version = versions.get(table, next(iter(versions.values())))
        if len(versions) > 1:
            out["catalog_versions"] = versions
    elif action == "add_codes":
        if row.kind != CODES_NEW or not row.column_name:
            raise ChangeInvalid("只有「出现了新码值」能加入码值")
        wanted = dict(codes) if codes else {str(c): "" for c in ((row.after or {}).get("codes") or [])}
        if not wanted or not all(isinstance(k, str) and isinstance(v, str) for k, v in wanted.items()):
            raise ChangeInvalid("要加入的码值应为「码值 → 含义」的对照，含义可以先空着")
        entry = await catalog.read_entry(session, source.id, table)
        current = entry.version if entry else 0
        meta_tables = (getattr(source, "schema_cache", None) or {}).get("tables") or None
        changes = [{"path": f"columns.{row.column_name}.codes", "value": wanted,
                    "reason": f"数据源变化 #{row.id[:6]}：取值观察测到的新码值，含义待填写"}]
        try:
            notes = catalog.apply_patch(entry.notes if entry else {}, changes, table=table, tables=meta_tables)
        except catalog.CatalogInvalid as e:
            raise ChangeInvalid("；".join(e.problems) if hasattr(e, "problems") else str(e)) from e
        written = await catalog.write_entry(session, source.id, table, notes, if_version=current, actor=actor,
                                            commit=False)
        version = written.version
    elif action == "reprofile":
        job_id = await _reprofile(session, source, table, actor)
        out["job"] = {"kind": "profile", "id": job_id}
    elif action == "redraft":
        job_id = await _redraft(session, source, table, actor, row.id)
        out["job"] = {"kind": "draft", "id": job_id}
    resolve(row, action=action, by=actor, note=note, catalog_version=version, job_id=job_id)
    if "catalog_versions" in out:
        row.resolution = {**(row.resolution or {}), "catalog_versions": out["catalog_versions"]}
    await session.commit()
    if version is not None:
        out["catalog_version"] = version
    out["change"] = change_out(row)
    return out


async def _reprofile(session: AsyncSession, source: Any, table: str, actor: str | None) -> str:
    from app.data import catalog_profile, catalog_profile_jobs, table_versions

    try:
        settings = catalog_profile.ensure_enabled(source)
    except catalog_profile.ProfileDisabled as e:
        raise ChangeInvalid(str(e)) from e
    try:
        view = await table_versions.resolve_source(session, source)
    except table_versions.SnapshotMissing as e:
        raise ChangeInvalid(str(e)) from e
    try:
        job, _tasks = await catalog_profile_jobs.create_job(session, source, view, tables=[table], scope="selected",
                                                            settings=settings, actor=actor)
    except (catalog_profile_jobs.ProfileBusy, catalog_profile_jobs.ProfileNothing) as e:
        raise ChangeInvalid(str(e)) from e
    catalog_profile_jobs.manager.start(job.id)
    return job.id


async def _redraft(session: AsyncSession, source: Any, table: str, actor: str | None, change_id: str) -> str:
    """建一个只含这张表的起草任务。起草模型用不了时只按表结构起草（数据库注释、外键、命名推断），不挡这个动作。"""
    from app.data import catalog, catalog_jobs, table_versions

    try:
        view = await table_versions.resolve_source(session, source)
    except table_versions.SnapshotMissing as e:
        raise ChangeInvalid(str(e)) from e
    if not ((getattr(view, "schema_cache", None) or {}).get("tables") or {}):
        raise ChangeInvalid("这个数据源还没有表结构，不能起草")
    use_model = True
    try:
        await catalog_jobs.check_model(session)
    except catalog.CatalogModelUnavailable:
        use_model = False
    try:
        job, _tasks = await catalog_jobs.create_job(session, source, view, tables=[table], use_model=use_model,
                                                    note=f"数据源变化 #{change_id[:6]}", actor=actor)
    except catalog_jobs.DraftBusy as e:
        raise ChangeInvalid(str(e)) from e
    catalog_jobs.manager.start(job.id)
    return job.id


__all__ = [
    "ACTIONS", "ACTION_STATUS", "CODES_NEW", "CODES_SHIFT", "COVERAGE_DROP", "ChangeFinished", "ChangeInvalid",
    "ChangeNotFound", "DATE_GAP", "DATE_STALLED", "Detected", "KINDS", "KIND_LABEL", "NewChange", "PENDING", "ROWS_DROP",
    "ROWS_JUMP", "SCHEMA", "SEVERITIES", "SEVERITY_RANK", "STATUSES", "VALUE_KINDS", "VOLUME_DROP", "affecting",
    "change_out", "counts", "fingerprint", "handle", "list_changes", "list_for_source", "load", "notify_after_job",
    "record", "record_all",
    "resolve", "summary", "triggers_recall", "without_paths",
]
