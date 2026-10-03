"""数据目录接口：/api/datasources/{source_id}/catalog。

- GET    /catalog                表清单（没有目录的表也列出来）
- GET    /catalog/{table}        单表目录 + 表结构
- PUT    /catalog/{table}        整份提交（乐观锁），改动过的项记为人工确认
- POST   /catalog/{table}/review 单项审阅：确认、驳回、撤销审阅
- POST   /catalog/draft          同步起草（注释、外键、命名推断，可选模型）

规则都在 data/catalog.py，这里只做取源、转换形状和把异常翻成状态码：版本不符 409，结构不合规、审阅路径
不对 422，源或表不存在 404。所有写操作记录请求头 X-Actor 的署名（自报、未认证）。

上传表格的源按当前快照的表结构回答（table_versions.resolve_source），和查询实际用的库是同一个版本。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runs import actor_of
from app.data import catalog, introspect, table_versions
from app.db.base import get_session
from app.db.models import DataSource

router = APIRouter(prefix="/api/datasources", tags=["catalog"])


class CatalogPutIn(BaseModel):
    notes: dict[str, Any]
    #: 读到的版本；这张表还没有目录时为 0
    if_version: int = Field(ge=0)


class CatalogReviewIn(BaseModel):
    #: grain、columns.amount.measure、relations.<编号>
    path: str = Field(min_length=1, max_length=600)
    action: Literal["confirm", "reject", "reset"]
    if_version: int = Field(ge=0)


class CatalogDraftIn(BaseModel):
    #: 要起草的表；不给或为空时按使用次数取前 20 张
    tables: list[str] | None = Field(default=None, max_length=200)
    #: 是否请助手的模型起草中文名、粒度、列的含义等
    use_model: bool = False


async def _resolved(session: AsyncSession, source_id: str) -> tuple[DataSource, Any]:
    """(数据源记录, 用来读表结构的源)：上传源解析成绑定当前快照的视图。"""
    row = await session.get(DataSource, source_id)
    if row is None:
        raise HTTPException(404, "数据源不存在，可能已被删除")
    try:
        return row, await table_versions.resolve_source(session, row)
    except table_versions.SnapshotMissing as e:
        raise HTTPException(409, str(e)) from e


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _item_value(notes: dict[str, Any], name: str) -> tuple[Any, str | None]:
    item = notes.get(name)
    if not isinstance(item, dict) or item.get("status") == "rejected":
        return None, None
    return item.get("value"), item.get("status")


def _summary_row(name: str, meta: dict[str, Any] | None, entry: catalog.CatalogEntry | None,
                 usage: int) -> dict[str, Any]:
    notes = entry.notes if entry else {}
    label, label_status = _item_value(notes, "label")
    kind, _ = _item_value(notes, "kind")
    relations = notes.get("relations") if isinstance(notes.get("relations"), list) else []
    return {
        "table_name": name,
        "qualified": (meta or {}).get("qualified", name),
        "is_view": bool((meta or {}).get("is_view")),
        # 表结构里已经没有这张表（被删了、改名了），目录还留着：界面据此提示清理
        "in_schema": meta is not None,
        "label": label,
        "label_status": label_status,
        "kind": kind,
        "counts": catalog.status_counts(notes),
        "relations": sum(1 for r in relations if isinstance(r, dict) and r.get("status") != "rejected"),
        "usage": usage,
        "version": entry.version if entry else 0,
        "updated_at": _iso(entry.updated_at) if entry else None,
        "updated_by": entry.updated_by if entry else None,
    }


def _structure(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "qualified": meta.get("qualified"),
        "is_view": bool(meta.get("is_view")),
        "comment": meta.get("comment"),
        "columns": introspect.table_columns(meta),
        "primary_key": list(meta.get("primary_key") or []),
        # 升级前探查的缓存里没有这两项：为 null 表示「不知道」，和空列表（读过、没有）分开
        "foreign_keys": meta.get("foreign_keys"),
        "unique": meta.get("unique"),
    }


async def _detail(session: AsyncSession, source: Any, table: str, entry: catalog.CatalogEntry | None,
                  usage: int | None = None) -> dict[str, Any]:
    meta = ((source.schema_cache or {}).get("tables") or {}).get(table)
    if usage is None:
        usage = (await catalog.table_usage(session, source)).get(table, 0)
    return {
        "table_name": table,
        "in_schema": meta is not None,
        "notes": entry.notes if entry else {},
        "version": entry.version if entry else 0,
        "updated_at": _iso(entry.updated_at) if entry else None,
        "updated_by": entry.updated_by if entry else None,
        "structure": _structure(meta) if meta is not None else None,
        "system_notes": catalog.system_notes_source(source),
        "usage": usage,
    }


async def _table_key(session: AsyncSession, source: Any, table: str) -> tuple[str, catalog.CatalogEntry | None]:
    """路径里的表名 → 目录的键。表结构里有的按表结构的写法；表结构里没有、但还留着目录的也认（便于清理）。"""
    key = catalog.resolve_table_name(source.schema_cache, table)
    entry = await catalog.read_entry(session, source.id, key or table)
    if key is None and entry is None:
        raise HTTPException(404, f"数据源中没有表 {table}，可能已被删除或尚未探查结构")
    return key or table, entry


def _conflict(e: catalog.CatalogConflict) -> HTTPException:
    return HTTPException(409, str(e))


def _invalid(e: catalog.CatalogInvalid) -> HTTPException:
    shown = "；".join(e.problems[:8]) + (f"；另有 {len(e.problems) - 8} 处" if len(e.problems) > 8 else "")
    return HTTPException(422, f"数据目录的格式不正确：{shown}")


@router.get("/{source_id}/catalog")
async def list_catalog(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """表清单：表结构里的每张表各一行（没有目录的也列），使用次数多的在前；目录还在、表已不在的排最后。"""
    _, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    entries = await catalog.read_catalog(session, source.id)
    usage = await catalog.table_usage(session, source)
    order = {name: i for i, name in enumerate(tables)}
    rows = [_summary_row(name, meta, entries.get(name), usage.get(name, 0)) for name, meta in tables.items()]
    rows.sort(key=lambda r: (-r["usage"], order[r["table_name"]]))
    rows += [_summary_row(name, None, entry, 0) for name, entry in sorted(entries.items()) if name not in tables]
    return {
        "tables": rows,
        "system_notes": catalog.system_notes_source(source),
        # 没有表结构时说明原因（尚未探查 / 探查失败），界面据此引导去探查
        "schema_note": None if tables else introspect.why_empty(cache),
    }


@router.get("/{source_id}/catalog/{table}")
async def get_catalog_table(source_id: str, table: str,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _, source = await _resolved(session, source_id)
    key, entry = await _table_key(session, source, table)
    return await _detail(session, source, key, entry)


@router.put("/{source_id}/catalog/{table}")
async def put_catalog_table(source_id: str, table: str, payload: CatalogPutIn,
                            x_actor: str | None = Header(default=None),
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """整份提交一张表的目录。改动过的项记为人工确认；版本不符 409。"""
    _, source = await _resolved(session, source_id)
    key, _ = await _table_key(session, source, table)
    try:
        entry = await catalog.save_human_edit(session, source.id, key, payload.notes, if_version=payload.if_version,
                                              actor=actor_of(x_actor))
    except catalog.CatalogConflict as e:
        raise _conflict(e) from e
    except catalog.CatalogInvalid as e:
        raise _invalid(e) from e
    return await _detail(session, source, key, entry)


@router.post("/{source_id}/catalog/draft")
async def draft_catalog(source_id: str, payload: CatalogDraftIn,
                        x_actor: str | None = Header(default=None),
                        session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """同步起草。返回每张表新增、更新、删除了几项；模型用不了或某张表的模型起草失败，照实写在结果里。"""
    _, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    if not cache.get("tables"):
        raise HTTPException(409, f"{introspect.why_empty(cache)}，无法起草数据目录。请先在数据源卡片上点「探查结构」")
    report = await catalog.draft_catalog(session, source, tables=payload.tables or None,
                                         use_model=payload.use_model, actor=actor_of(x_actor))
    rows = [{"table_name": r.table, "added": r.added, "updated": r.updated, "removed": r.removed,
             "version": r.version, "error": r.error, "model_error": r.model_error} for r in report.tables]
    return {
        "tables": rows,
        "total": {k: sum(r[k] for r in rows) for k in ("added", "updated", "removed")},
        "model_used": report.model_used,
        "model": report.model or None,
        "model_error": report.model_error,
    }


@router.post("/{source_id}/catalog/{table}/review")
async def review_catalog_item(source_id: str, table: str, payload: CatalogReviewIn,
                              x_actor: str | None = Header(default=None),
                              session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """单项审阅。版本不符 409；路径不对、项不存在 422。"""
    _, source = await _resolved(session, source_id)
    key, _ = await _table_key(session, source, table)
    try:
        entry = await catalog.review_entry(session, source.id, key, payload.path, payload.action,
                                           if_version=payload.if_version, actor=actor_of(x_actor))
    except catalog.CatalogConflict as e:
        raise _conflict(e) from e
    except catalog.CatalogPathError as e:
        raise HTTPException(422, str(e)) from e
    return await _detail(session, source, key, entry)
