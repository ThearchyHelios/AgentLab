"""记录表的登记（docs/design/normal-user-apps.md N8、§3.7）：records_tables 一行一个表版本，各自登记成 origin=records 的
只读数据源，看板照常用 db_query__<名字> 查、数据目录照常起草。

**表版本。** 「填什么」卡片变了时按列的变化分档（column_changes）：
- 只加可选字段、改中文名、改敏感标记：兼容，在同一个版本上 ADD COLUMN、更新登记；
- 改类型、删字段、加必填字段、可选改必填：不兼容，出新版本（新文件、新数据源、records_tables 再登记一行），
  旧版本标 retired_at、只读保留——旧记录照原样留着，照样查得到（新一版的查询按 UNION ALL 并进来，records/views.py），
  只是不再往里写：停用之前发起、还在半路的运行照常写完（records/actions.py），之后发起的拒。
不兼容的判定沿用按期累积的 INCOMPATIBLE_KINDS（data/recipe_accumulate.py，列类型变了同样是不兼容），再加上记录表
特有的三种：删字段、加必填字段、可选改必填（旧行在那一列上是空的）。

**草稿不影响线上。** 同一个蓝图（或没有蓝图的应用）的各版本共用一个 table_key。蓝图这边（按卡片生成、试运行、
填关节）用 draft_table：兼容的只补列，不兼容的出下一版，线上应用在写的那一版不停用、照常收提交；发布单发布应用那一步
用 publish_table 切换——图里写的那一版成为往里写的那一版，别的版本这时才停用。ensure_table 当场切换，只给没有线上应用
的场合（测试、手工整理）。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.recipe_accumulate import INCOMPATIBLE_KINDS
from app.db.base import utcnow
from app.db.models import DataSource
from app.db.models_blueprints import RecordsTable
from app.records import RECORDS_ORIGIN, SYSTEM_COLUMNS
from app.records import store
from app.records.store import Column, RecordsRefused

#: 记录表特有的不兼容改动：删字段、加必填字段、可选改必填。和 INCOMPATIBLE_KINDS（类型等）合起来是出新版本的全部情形
RECORDS_INCOMPATIBLE = (*INCOMPATIBLE_KINDS, "column_removed", "required_added", "required_changed")
#: 数据源名字的前缀。数据源名只能是小写字母开头、字母数字下划线（会成为工具名的一部分）
SOURCE_PREFIX = "rec_"
#: 系统列在表结构里的说明（工具描述、db_schema、数据目录起草看得到）
SYSTEM_NOTES = {
    "_seq": "序号：同一版内只增不减，一次运行只看得到开始时已有的序号以内的记录。表单改过、出了新版本时，旧版本的记录"
            "一并列在这张表里，各版本各自编号，按 _schema_version 分开",
    "_id": "这一行的编号",
    "_submitted_by": "提交人",
    "_submitted_at": "提交时间（UTC）",
    "_run_id": "写入这一行的运行",
    "_supersedes": "更正的是哪一行（那一行的编号）；更正过的行不再出现在查询里",
    "_schema_version": "写入时的表版本（并进来的旧版本的记录是旧的版本号）",
}
_SQL_TYPE_LABEL = {"string": "TEXT", "number": "REAL", "integer": "INTEGER", "boolean": "INTEGER", "array": "TEXT",
                   "file": "TEXT"}


@dataclass(frozen=True)
class Change:
    kind: str
    column: str
    message: str

    @property
    def incompatible(self) -> bool:
        return self.kind in RECORDS_INCOMPATIBLE


def column_changes(old: list[Column], new: list[Column]) -> list[Change]:
    """两份列定义之间的变化，按列名（不分大小写，SQLite 的列名就不分）对上。"""
    before = {c.name.lower(): c for c in old}
    after = {c.name.lower(): c for c in new}
    out: list[Change] = []
    for key, c in before.items():
        if key not in after:
            out.append(Change("column_removed", c.name, f"删掉了字段「{c.label or c.name}」"))
    for key, c in after.items():
        was = before.get(key)
        if was is None:
            out.append(Change("required_added" if c.required else "column_added", c.name,
                              f"加了{'必填' if c.required else '可选'}字段「{c.label or c.name}」"))
            continue
        if was.name != c.name:
            out.append(Change("column_removed", was.name, f"字段「{was.name}」改名为「{c.name}」"))
            continue
        if was.type != c.type:
            out.append(Change("type", c.name, f"字段「{c.label or c.name}」的类型从 {was.type} 改成了 {c.type}"))
        if c.required and not was.required:
            out.append(Change("required_changed", c.name, f"字段「{c.label or c.name}」从可选改成了必填"))
        if was.label != c.label:
            out.append(Change("label", c.name, f"字段「{c.name}」的中文名改成了「{c.label}」"))
        if was.sensitive != c.sensitive:
            out.append(Change("sensitive", c.name, f"字段「{c.label or c.name}」{'标为' if c.sensitive else '不再是'}敏感"))
    return out


# --------------------------------------------------------------------------
# 列定义从哪来
# --------------------------------------------------------------------------


def _form_type(prop: dict[str, Any]) -> str:
    if prop.get("x-widget") == "file":
        return "file"
    kind = prop.get("type")
    return kind if kind in ("string", "number", "integer", "boolean", "array") else "string"


def columns_from_form(form: dict[str, Any], *,
                      sensitive: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """入口表单（agentlab.form/1）→ 记录表的列定义。只看得到的展示字段（x-widget: note）不进表。"""
    props = form.get("properties") if isinstance(form, dict) else None
    if not isinstance(props, dict):
        return []
    required = set(form.get("required") or [])
    listed = [n for n in form.get("x-order") or [] if n in props]
    order = listed + [n for n in props if n not in listed]
    out: list[dict[str, Any]] = []
    for name in order:
        prop = props.get(name)
        if not isinstance(prop, dict) or prop.get("x-widget") == "note":
            continue
        out.append({"name": name, "label": str(prop.get("title") or name), "type": _form_type(prop),
                    "required": name in required, "sensitive": name in sensitive})
    return out


def columns_from_fields(fields: list[Any]) -> list[dict[str, Any]]:
    """蓝图「填什么」卡片（blueprints/spec.FieldItem：{name, spec: {value: 表单属性}, sensitive}）→ 列定义。
    必填写在属性的 required（布尔）上；没写按可选。"""
    out: list[dict[str, Any]] = []
    for item in fields or []:
        data = item.model_dump() if hasattr(item, "model_dump") else item
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            continue
        spec = data.get("spec")
        prop = spec.get("value") if isinstance(spec, dict) else None
        prop = prop if isinstance(prop, dict) else {}
        if prop.get("x-widget") == "note":
            continue
        out.append({"name": data["name"], "label": str(prop.get("title") or data["name"]), "type": _form_type(prop),
                    "required": bool(prop.get("required")), "sensitive": bool(data.get("sensitive"))})
    return out


# --------------------------------------------------------------------------
# 登记
# --------------------------------------------------------------------------


def _owner(owner: Any) -> tuple[str, str | None, str | None, str]:
    """(table_key, app_id, blueprint_id, 名字)。应用有蓝图时按蓝图认，这样试运行时建的表发布后接着用。"""
    from app.db.models_apps import App

    if isinstance(owner, App):
        blueprint_id = getattr(owner, "blueprint_id", None)
        return blueprint_id or owner.id, owner.id, blueprint_id, owner.name or "记录"
    blueprint_id = getattr(owner, "id", None)
    if not blueprint_id:
        raise ValueError("记录表要属于一个应用或蓝图")
    return blueprint_id, getattr(owner, "app_id", None), blueprint_id, getattr(owner, "name", None) or "记录"


def schema_cache_of(columns: list[Column], label: str) -> dict[str, Any]:
    """登记到数据源上的表结构，形状同 data/introspect.introspect：工具描述、db_schema、数据目录都按它读。
    不露 _action_key（查询走的临时视图里也没有它）。"""
    cols = [{"name": n, "type": "INTEGER" if n in ("_seq", "_schema_version") else "TEXT",
             "nullable": n not in ("_seq", "_id", "_submitted_at", "_schema_version"), "comment": SYSTEM_NOTES[n]}
            for n in SYSTEM_COLUMNS]
    cols += [{"name": c.name, "type": _SQL_TYPE_LABEL[c.type], "nullable": True,
              "comment": c.label if c.label and c.label != c.name else None} for c in columns]
    return {"tables": {store.TABLE: {"qualified": store.TABLE, "schema": None, "columns": cols,
                                     "primary_key": ["_seq"], "comment": label, "is_view": False}},
            "truncated": False, "total": 1, "synced_at": datetime.now(timezone.utc).isoformat(), "schema": None}


def _description(label: str, version: int) -> str:
    return (f"记录表「{label}」{'' if version == 1 else f'（第 {version} 版）'}：平台托管、只许追加的填报记录，表名 records。"
            "一次运行只看得到开始时已有的记录；更正过的行只留最新的一行"
            + ("" if version == 1 else "；前几版的记录一并列在里面，旧版本没有的字段为空"))


async def _free_name(session: AsyncSession, base: str) -> str:
    name, n = base, 2
    while (await session.execute(select(DataSource.id).where(DataSource.name == name))).first() is not None:
        name, n = f"{base}_{n}", n + 1
    return name


async def _register(session: AsyncSession, *, key: str, version: int, columns: list[Column], label: str,
                    app_id: str | None, blueprint_id: str | None) -> RecordsTable:
    path = store.table_path(key, version)
    async with store.write_lock():
        await asyncio.to_thread(store.create_file, path, columns)
    base = f"{SOURCE_PREFIX}{re.sub(r'[^a-z0-9]', '', key.lower())[:12] or 'table'}"
    name = await _free_name(session, base if version == 1 else f"{base}_v{version}")
    source = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, origin=RECORDS_ORIGIN,
                        description=_description(label, version), options=_options(columns),
                        schema_cache=schema_cache_of(columns, label), schema_synced_at=utcnow())
    session.add(source)
    await session.flush()
    row = RecordsTable(table_key=key, version=version, name=label, app_id=app_id, blueprint_id=blueprint_id,
                       source_id=source.id, columns=[c.out() for c in columns])
    session.add(row)
    await session.flush()
    return row


def _options(columns: list[Column]) -> dict[str, Any]:
    from app.data.engine import MASK_COLUMNS_OPTION

    masked = [c.name for c in columns if c.sensitive]
    return {MASK_COLUMNS_OPTION: masked} if masked else {}


async def current_table(session: AsyncSession, table_key: str) -> RecordsTable | None:
    """这张记录表现在往里写的那个版本（没退役的最新一版）。"""
    return (await session.execute(select(RecordsTable).where(
        RecordsTable.table_key == table_key, RecordsTable.retired_at.is_(None))
        .order_by(RecordsTable.version.desc()).limit(1))).scalar_one_or_none()


async def versions_of(session: AsyncSession, table_key: str) -> list[RecordsTable]:
    """各版本，新的在前（看板跨版本汇总、记录列表合并各版本时用）。"""
    return list((await session.execute(select(RecordsTable).where(RecordsTable.table_key == table_key)
                                       .order_by(RecordsTable.version.desc()))).scalars())


async def tables_of_app(session: AsyncSession, app_id: str) -> list[RecordsTable]:
    """这个应用的记录表，每张取现在的版本，按登记先后。"""
    rows = list((await session.execute(select(RecordsTable).where(RecordsTable.app_id == app_id)
                                       .order_by(RecordsTable.created_at, RecordsTable.version))).scalars())
    latest: dict[str, RecordsTable] = {}
    for row in rows:
        if row.table_key not in latest or row.version > latest[row.table_key].version:
            latest[row.table_key] = row
    return list(latest.values())


async def find_table(session: AsyncSession, ref: str) -> RecordsTable | None:
    """调用工具节点 args.table 写的那张表：records_tables 的 id、数据源名、或「table_key.v版本」都认。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    row = await session.get(RecordsTable, ref)
    if row is not None:
        return row
    source = (await session.execute(select(DataSource.id).where(
        DataSource.name == ref, DataSource.origin == RECORDS_ORIGIN))).scalar_one_or_none()
    if source is not None:
        return await table_of_source(session, source)
    key, dot, version = ref.rpartition(".v")
    if dot and version.isdigit():
        return (await session.execute(select(RecordsTable).where(
            RecordsTable.table_key == key, RecordsTable.version == int(version)))).scalar_one_or_none()
    return None


async def table_of_source(session: AsyncSession, source_id: str) -> RecordsTable | None:
    return (await session.execute(select(RecordsTable).where(RecordsTable.source_id == source_id))).scalar_one_or_none()


async def ensure_table(session: AsyncSession, owner: Any, columns: list[dict[str, Any]]) -> RecordsTable:
    """按「填什么」的列定义取（或建）这张记录表现在的版本，当场切换，交回 records_tables 的那一行。

    - 第一次：建 v1 的文件、登记数据源和 records_tables；
    - 兼容的改动：同一版本上 ADD COLUMN，更新登记（中文名、敏感标记只改这次变了的那几列，见 _sync_registration）；
    - 不兼容的改动：建下一版（新文件、新数据源），旧版本标 retired_at、只读保留。
    owner 是应用（App）或蓝图（Blueprint）。当场切换会让线上在写的那一版停用，所以只给没有线上应用的场合用（管理员手工
    整理、测试）；蓝图那边起草、试运行用 draft_table，发布单发布应用时用 publish_table。只 flush 不提交：和调用方的
    其余改动一起提交；文件先建好，调用方回滚时留下的空文件下次建同一版本时重建（有数据的文件绝不覆盖）。"""
    key, app_id, blueprint_id, label = _owner(owner)
    wanted = store.parse_columns(columns)
    current = await current_table(session, key)
    if current is None:
        return await _register(session, key=key, version=1, columns=wanted, label=label, app_id=app_id,
                               blueprint_id=blueprint_id)
    if app_id and current.app_id != app_id:
        # 试运行时按蓝图建的表，发布成应用之后归到应用名下（各版本一起）
        for row in await versions_of(session, key):
            row.app_id = app_id
    changes = column_changes(columns_of(current), wanted)
    if any(c.incompatible for c in changes):
        current.retired_at = utcnow()
        current.note = "；".join(c.message for c in changes if c.incompatible)
        return await _register(session, key=key, version=await _next_version(session, key), columns=wanted,
                               label=label, app_id=app_id or current.app_id,
                               blueprint_id=blueprint_id or current.blueprint_id)
    if changes:
        await _sync_registration(session, current, wanted, label=label, draft=False)
    await session.flush()
    return current


async def _next_version(session: AsyncSession, key: str) -> int:
    """下一个版本号：停用的版本也占着号（文件和 records_tables 的唯一约束都按它）。"""
    return max([row.version for row in await versions_of(session, key)] or [0]) + 1


async def _open_versions(session: AsyncSession, key: str) -> list[RecordsTable]:
    """没停用的各版本，老的在前。线上应用在写的那一版是最老的那个；比它新的是起草时出的、还没发布的版本。"""
    return sorted([row for row in await versions_of(session, key) if row.retired_at is None], key=lambda r: r.version)


def _fits(row: RecordsTable, wanted: list[Column]) -> bool:
    return not any(c.incompatible for c in column_changes(columns_of(row), wanted))


async def pick_table(session: AsyncSession, key: str, columns: list[dict[str, Any]]) -> RecordsTable | None:
    """只读：起草这边（draft_table）会用的那一版——没停用、和「填什么」兼容的版本里最老的那个（线上在写的优先，少出版本）。
    要出新版本才对得上的（或者还没建过）为 None。"""
    wanted = store.parse_columns(columns)
    return next((row for row in await _open_versions(session, key) if _fits(row, wanted)), None)


async def draft_table(session: AsyncSession, owner: Any, columns: list[dict[str, Any]]) -> RecordsTable:
    """起草这边（按卡片生成、试运行、填关节）要的那一版：草稿和试运行不影响线上应用。

    - 还没建过：建 v1；
    - 有一版兼容（pick_table）：用它。缺的可选字段在它上面 ADD COLUMN、登记补上这几列（只加不改：线上的图不写这几列，
      写不坏）；已有列的中文名、敏感标记等发布时再改（publish_table）——起草时取消一个敏感标记，不该当场让线上的数露出来；
    - 都不兼容：出下一版，线上在写的那一版不停用、照常收提交。「停用旧版、切到新版」只在发布单发布应用那一步做。
    试运行的写入一律演练（capabilities/platform.py），起草时出的版本在发布前一行都不会有。只 flush，调用方提交。"""
    key, app_id, blueprint_id, label = _owner(owner)
    wanted = store.parse_columns(columns)
    row = await pick_table(session, key, columns)
    if row is None:
        have = await versions_of(session, key)
        return await _register(session, key=key, version=await _next_version(session, key), columns=wanted,
                               label=label, app_id=next((r.app_id for r in have if r.app_id), app_id),
                               blueprint_id=blueprint_id)
    if column_changes(columns_of(row), wanted):
        await _sync_registration(session, row, wanted, label=label, draft=True)
    await session.flush()
    return row


async def publish_table(session: AsyncSession, app: Any, ref: str, columns: list[dict[str, Any]]) -> RecordsTable:
    """发布单发布应用那一步（应用的新一版已经发布）：图里写的那一版（ref，「键.v版本」）成为这张表往里写的那一版。

    各版本归到应用名下；别的没停用的版本这时才停用——线上原来在写的那一版（写明换成了第几版），以及起草时出过、最后没用上的
    版本。登记按「填什么」整理：中文名、敏感标记照卡片改（只改这次变了的那几列，管理员在数据页手工加的遮罩列留着）。
    只 flush，调用方提交。"""
    row = await find_table(session, ref)
    if row is None:
        raise RecordsRefused("records.table_missing", f"找不到记录表「{ref}」，请重新出一份发布单")
    if row.retired_at is not None:
        refuse_retired(row)
    wanted = store.parse_columns(columns)
    changes = column_changes(columns_of(row), wanted)
    if any(c.incompatible for c in changes):
        raise RecordsRefused("records.column_unknown", f"记录表「{row.name}」第 {row.version} 版和「填什么」对不上了："
                                                       + "；".join(c.message for c in changes if c.incompatible)
                                                       + "。请重新试运行、再出一份发布单")
    now = utcnow()
    for other in await versions_of(session, row.table_key):
        other.app_id = app.id
        if other.id == row.id or other.retired_at is not None:
            continue
        other.retired_at = now
        other.note = (f"应用发布了新的表单，改写第 {row.version} 版" if other.version < row.version
                      else "起草时出的版本，发布时没有用上")
    if changes:
        await _sync_registration(session, row, wanted, label=app.name or row.name, draft=False)
    await session.flush()
    return row


async def _sync_registration(session: AsyncSession, row: RecordsTable, wanted: list[Column], *, label: str,
                             draft: bool) -> None:
    """兼容的改动落到这一版上：缺的列 ADD COLUMN（文件里已有的跳过），登记的列定义、数据源的表结构和遮罩列跟着改。

    遮罩列只改这次变了的那几列（新加的敏感列加上；发布时，敏感标记取消的去掉、新标上的加上），别的原样留着——管理员在
    数据页手工加的遮罩列不会被下一次表单改动抹掉。draft=True 只加不改：已有列的定义、遮罩照旧，只补新列。"""
    have = columns_of(row)
    known = {h.name.lower() for h in have}
    added = [c for c in wanted if c.name.lower() not in known]
    async with store.write_lock():
        await asyncio.to_thread(store.add_columns, store.table_path(row.table_key, row.version), added)
    by_name = {w.name.lower(): w for w in wanted}
    # 列的顺序：原有的在前（照原来的顺序），新加的在后——和文件里的列序一致。删掉的列（只在不兼容时出现）这里不会有
    kept = [h if draft else by_name.get(h.name.lower(), h) for h in have]
    merged = kept + added
    row.columns = [c.out() for c in merged]
    if not draft:
        row.name = label
    source = await session.get(DataSource, row.source_id) if row.source_id else None
    if source is None:
        return
    from app.data.engine import MASK_COLUMNS_OPTION

    masked = [str(m) for m in (source.options or {}).get(MASK_COLUMNS_OPTION) or []]
    before = {h.name.lower(): h for h in have}
    drop = set() if draft else {c.name.lower() for c in merged
                                if not c.sensitive and before.get(c.name.lower()) is not None
                                and before[c.name.lower()].sensitive}
    add = [c.name for c in (added if draft else merged) if c.sensitive and c.name not in masked]
    masked = [m for m in masked if m.lower() not in drop] + add
    options = {k: v for k, v in (source.options or {}).items() if k != MASK_COLUMNS_OPTION}
    source.options = {**options, **({MASK_COLUMNS_OPTION: masked} if masked else {})}
    source.schema_cache = schema_cache_of(merged, row.name)
    source.schema_synced_at = utcnow()


def columns_of(row: RecordsTable) -> list[Column]:
    return store.parse_columns(row.columns)


def path_of(row: RecordsTable) -> Any:
    return store.table_path(row.table_key, row.version)


def refuse_retired(row: RecordsTable, *, launched_at: datetime | None = None) -> None:
    """往停用的版本写：拒。launched_at 是要写的那次运行发起的时刻——停用之前发起的照常写完（同进行中的实例照常走完，
    写进它发起时图里写的那一版）；停用之后发起的、不在运行里的一律拒。"""
    if row.retired_at is not None and (launched_at is None or launched_at >= row.retired_at):
        raise RecordsRefused("records.table_retired",
                             f"记录表「{row.name}」的第 {row.version} 版已经停用（表单改过，出了新版本），旧记录只读保留。"
                             "请重新发布应用，让它写到新版本")
