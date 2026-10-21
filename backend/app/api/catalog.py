"""数据目录接口：/api/datasources/{source_id}/catalog。

- GET    /catalog                表清单（没有目录的表也列出来）
- GET    /catalog/{table}        单表目录 + 表结构
- PUT    /catalog/{table}        整份提交（乐观锁），改动过的项记为人工确认
- POST   /catalog/{table}/review 单项审阅：确认、驳回、撤销审阅
- POST   /catalog/{table}/patch/preview  目录修改提案的预览（只读）：对着当前目录算改前、改后
- POST   /catalog/{table}/patch  保存目录修改提案（乐观锁），记为人工确认
- GET    /catalog/{table}/impact 影响面：引用这张表的已发布、受管模板（data/catalog_impact.py）
- POST   /catalog/draft          同步起草（注释、外键、命名推断，可选模型）。界面改用下面的后台起草任务，这个留着；
                                 同一个源有排队或进行中的起草任务（或正在同步起草）时 409 catalog_draft_busy，有剖析任务时
                                 409 profile_busy；同步起草期间建起草任务、重试、导入也是 409，新建的剖析任务排在它后面
- PUT    /catalog/guidance       数据源级的起草要求（使用者补充的背景和要求，起草时交给模型）
- POST   /catalog/draft-jobs     建后台起草任务（202）；GET /draft-jobs/active、/latest、/{job_id} 读任务；
                                 POST /draft-jobs/{job_id}/cancel 停止，/retry 只重试没写好的表（data/catalog_jobs.py）。
                                 同源有剖析任务时照建，排在它后面（waiting_for）
- POST   /catalog/profile-jobs   建后台剖析任务（202）：对业务库逐表发少量只读查询，核对关系、取码值候选、提议业务日期
                                 （data/catalog_profile_jobs.py，引擎在 data/catalog_profile.py）；GET /profile-jobs/active、
                                 /latest、/{job_id} 读任务，/{job_id}/tasks/{seq} 读一张表的明细（检查项、发现、发过的查询）；
                                 POST /profile-jobs/{job_id}/cancel 停止，/retry 只重试没做完的表。同源有起草任务时照建，
                                 排在它后面（waiting_for）
- GET    /catalog/review-queue   审阅队列：推断的项按使用次数、风险、来源排好；POST /catalog/review/batch 批量审阅，
                                 GET /catalog/review-progress 审阅进度，GET /catalog/{table}/usage 用过这张表的运行
                                 （data/catalog_review.py）
- GET    /catalog/export         导出数据目录（JSON 附件）；POST /catalog/import/preview 导入预览（只读），
                                 POST /catalog/import 导入（data/catalog_transfer.py）
- POST   /catalog/{table}/assistant  表助手：对话完善这张表的目录（text/event-stream），提案不写目录
                                 （data/catalog_assistant.py）
- GET    /catalog/search         在对象索引里按名字、注释找表（含探查范围外的）；POST /catalog/scope/add 把范围外的表加进
                                 探查范围（当场只读读结构、记进规则的显式列出，next-round 03 A2）

规则都在 data/catalog.py，这里只做取源、转换形状和把异常翻成状态码：版本不符 409，结构不合规、审阅路径
不对 422，源或表不存在 404。所有写操作记下署名（api/deps.actor_of：开了登录是认证过的用户名，否则请求头 X-Actor 的自报）。
起草、剖析任务的 409 带机读码（api/coded.py）：同一个源已有排队或进行中的同种任务、起草模型用不了、剖析没开启、按范围
挑不出表、要停止的任务已经结束。

上传表格的源按当前快照的表结构回答（table_versions.resolve_source），和查询实际用的库是同一个版本。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import (
    CATALOG_DRAFT_BUSY, CATALOG_DRAFT_FINISHED, CATALOG_DRAFT_MODEL_UNAVAILABLE, CATALOG_PROFILE_FINISHED,
    INTROSPECT_BUSY, PROFILE_BUSY, PROFILE_NOTHING, SCHEMA_MISSING, SNAPSHOT_TAMPERED, CodedHTTPException,
)
from app.api.deps import actor_of
from app.data import catalog, catalog_impact, catalog_jobs, catalog_profile, catalog_profile_jobs, introspect
from app.data import introspect_jobs, source_objects, table_versions
from app.data import catalog_assistant, catalog_review, catalog_transfer
from app.data.engine import SnapshotTampered
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


class CatalogPatchChange(BaseModel):
    #: 审阅接口的路径写法；新增关联关系写 relations.new
    path: str = Field(min_length=1, max_length=600)
    value: Any
    reason: str | None = Field(default=None, max_length=2000)


class CatalogPatchPreviewIn(BaseModel):
    changes: list[CatalogPatchChange] = Field(min_length=1, max_length=catalog.PATCH_MAX_CHANGES)


class CatalogPatchIn(CatalogPatchPreviewIn):
    if_version: int = Field(ge=0)


class CatalogDraftIn(BaseModel):
    #: 要起草的表；不给或为空时按使用次数取前 20 张
    tables: list[str] | None = Field(default=None, max_length=200)
    #: 是否请起草模型（设置里的起草模型，没设沿用助手的模型）写中文名、粒度、列的含义等
    use_model: bool = False


class DraftJobIn(BaseModel):
    #: 要起草的表；不给或为空时按使用次数取前 20 张。最多 500 张（超了 422，说明是中文）
    tables: list[str] | None = None
    #: 是否请起草模型写中文名、粒度、列的含义等
    use_model: bool = False
    #: 这一次的补充说明，和数据源级的起草要求一起交给模型（最多 1000 字）
    note: str | None = None


#: PUT /catalog/guidance 的请求 {guidance: string}：数据源级的起草要求，空串（或 null）清除，最多 2000 字。必须写
#: 这一项：表名恰好叫 guidance 时，整份提交那张表目录的请求也落到这个路径上，不能把它当成「清除起草要求」。
#: 没写、不是文字都回 422，说明是中文的一句话（同超长）
GUIDANCE_MISSING = "请求里要写起草要求（guidance 一项，一段文字；要清除起草要求请传空串）"
GUIDANCE_NOT_TEXT = "起草要求要是一段文字（要清除起草要求请传空串）"


class ProfileJobIn(BaseModel):
    #: 要剖析的表（范围 selected，按给的顺序去重）；最多 500 张（超了 422，说明是中文）
    tables: list[str] | None = None
    #: 不指定表时的范围：needed 需要剖析的表（缺省）、all 全部表（都不含视图、没有可剖析的项的表）、observe 取值观察（只观察
    #: 有数据目录、观察到期的表，不剖析；见 data/catalog_profile_observe）。maintenance 是定时维护用的，这里不收
    scope: Literal["needed", "all", "observe"] | None = None


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


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _item_value(notes: dict[str, Any], name: str) -> tuple[Any, str | None]:
    item = notes.get(name)
    if not isinstance(item, dict) or item.get("status") == "rejected":
        return None, None
    return item.get("value"), item.get("status")


def _summary_row(name: str, meta: dict[str, Any] | None, entry: catalog.CatalogEntry | None,
                 usage: int, last_draft: dict[str, Any] | None = None,
                 profiling: dict[str, Any] | None = None, absent: str | None = None) -> dict[str, Any]:
    notes = entry.notes if entry else {}
    label, label_status = _item_value(notes, "label")
    kind, _ = _item_value(notes, "kind")
    relations = notes.get("relations") if isinstance(notes.get("relations"), list) else []
    return {
        "table_name": name,
        "qualified": (meta or {}).get("qualified", name),
        "is_view": bool((meta or {}).get("is_view")),
        # 表结构里没有这张表，目录还留着：界面据此提示。为什么不在（absent_reason）分开说：removed 表已不在（真删除）、
        # missing 数据源里从来没有、out_of_scope 在探查范围外、reflect_failed 在范围里却读不到结构、unknown 说不清
        # （升级前只探查了一部分的老缓存）。只有 removed 才说「表已不在」
        "in_schema": meta is not None,
        "absent_reason": None if meta is not None else absent,
        "label": label,
        "label_status": label_status,
        "kind": kind,
        "counts": catalog.status_counts(notes),
        "relations": sum(1 for r in relations if isinstance(r, dict) and r.get("status") != "rejected"),
        "usage": usage,
        "version": entry.version if entry else 0,
        "updated_at": _iso(entry.updated_at) if entry else None,
        "updated_by": entry.updated_by if entry else None,
        # 这张表最近一次起草任务里的那一项：{status, job_id, finished_at, error, model_error, blank_columns}，没起草过
        # 是 null
        "last_draft": last_draft,
        # 剖析：最近一次剖析（CatalogLastProfile，没剖析过是 null）、要不要剖析和为什么、按当前目录和表结构要做几项检查
        **(profiling or {"last_profile": None, "profile_due": False, "profile_due_reason": None, "profile_checks": 0}),
    }


def _profiling(name: str, meta: dict[str, Any] | None, entry: catalog.CatalogEntry | None, *,
               tables: dict[str, Any], masked: set[str], last: dict[str, Any] | None,
               outside: frozenset[str] = frozenset()) -> dict[str, Any]:
    notes = entry.notes if entry else {}
    planned = catalog_profile.plan_checks(name, meta, notes, tables=tables, masked=masked, outside=outside)
    checks = sum(1 for c in planned if c.skip is None)
    reason = catalog_profile_jobs.due_reason(meta, notes, last, planned)
    return {"last_profile": catalog_profile_jobs.public_last(last), "profile_due": reason is not None,
            "profile_due_reason": reason, "profile_checks": checks}


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
    absent = None if meta is not None else (await _absent(session, source, [table])).get(table)
    return {
        "table_name": table,
        "in_schema": meta is not None,
        # 表结构里没有时为什么（同表清单的 absent_reason）
        "absent_reason": absent,
        "notes": entry.notes if entry else {},
        "version": entry.version if entry else 0,
        "updated_at": _iso(entry.updated_at) if entry else None,
        "updated_by": entry.updated_by if entry else None,
        "structure": _structure(meta) if meta is not None else None,
        "system_notes": catalog.system_notes_source(source),
        "usage": usage,
        # 这张表最近一次剖析（CatalogProfileTaskDetail：检查项、发现、跳过的项、日期范围、发过的查询）；任务还在进行就是
        # 进行中的状态；没剖析过是 null
        "last_profile": await catalog_profile_jobs.last_detail(session, source.id, table),
    }


async def _table_key(session: AsyncSession, source: Any, table: str) -> tuple[str, catalog.CatalogEntry | None]:
    """路径里的表名 → 目录的键。表结构里有的按表结构的写法；表结构里没有、但还留着目录的也认（便于清理）。都没有时 404，
    说清是哪一种「不在」（_missing）。"""
    key = catalog.resolve_table_name(source.schema_cache, table)
    entry = await catalog.read_entry(session, source.id, key or table)
    if key is None and entry is None:
        raise await _missing(session, source, table)
    return key or table, entry


async def _missing(session: AsyncSession, source: Any, table: str) -> HTTPException:
    """表结构里没有这张表的 404：表已不在、数据源里没有、在探查范围外、读不到结构、说不清，和 /schema、/catalog/search、
    目录清单的 absent_reason 同一套（source_objects.absent_message）。"""
    return HTTPException(404, await source_objects.absent_message(session, source, table))


def _conflict(e: catalog.CatalogConflict) -> HTTPException:
    return HTTPException(409, str(e))


def _invalid(e: catalog.CatalogInvalid) -> HTTPException:
    shown = "；".join(e.problems[:8]) + (f"；另有 {len(e.problems) - 8} 处" if len(e.problems) > 8 else "")
    return HTTPException(422, f"数据目录的格式不正确：{shown}")


@router.get("/{source_id}/catalog")
async def list_catalog(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """表清单：探查范围内、读到了结构的每张表各一行（没有目录的也列），使用次数多的在前；目录还在、表结构里没有的排最后，
    各带 absent_reason（表已不在、数据源里没有、在探查范围外、读不到结构，见 _summary_row）。

    几千张表的库只列探查范围内的（next-round 03 A1）：范围外的在对象索引里，用 GET /catalog/search 找、POST /catalog/scope/add
    加进来。schema_truncated 是「有范围外的对象」，schema_total 是对象索引里一共几张（含视图）；scope 是范围的摘要
    （schema_cache 的 scope：in_scope、reflected、failed、out_of_scope、cap、各原因几张），升级前探查的源为 null。
    """
    row, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    tables = cache.get("tables") or {}
    entries = await catalog.read_catalog(session, source.id)
    usage = await catalog.table_usage(session, source)
    drafts = await catalog_jobs.last_drafts(session, source.id)
    lasts = await catalog_profile_jobs.last_profiles(session, source.id)
    masked = catalog_profile.masked_of(row.options)
    outside = await catalog_profile_jobs.outside_of(session, row)

    def profiling(name: str, meta: dict[str, Any] | None) -> dict[str, Any]:
        return _profiling(name, meta, entries.get(name), tables=tables, masked=masked, last=lasts.get(name),
                          outside=outside)

    order = {name: i for i, name in enumerate(tables)}
    rows = [_summary_row(name, meta, entries.get(name), usage.get(name, 0), drafts.get(name), profiling(name, meta))
            for name, meta in tables.items()]
    rows.sort(key=lambda r: (-r["usage"], order[r["table_name"]]))
    gone = [name for name in sorted(entries) if name not in tables]
    absent = await _absent(session, row, gone)
    rows += [_summary_row(name, None, entries[name], 0, drafts.get(name), profiling(name, None), absent.get(name))
             for name in gone]
    return {
        "tables": rows,
        "system_notes": catalog.system_notes_source(source),
        # 没有表结构时说明原因（尚未探查 / 探查失败），界面据此引导去探查
        "schema_note": None if tables else introspect.why_empty(cache),
        "schema_truncated": bool(cache.get("truncated")),
        "schema_total": max(_int(cache.get("total")), len(tables)),
        "scope": dict(cache["scope"]) if isinstance(cache.get("scope"), dict) else None,
        # 数据源级的起草要求（没有是 null）
        "guidance": catalog_jobs.guidance_of(row),
        # 排队或进行中的起草任务 {id, status, total, done, waiting_for}，界面据此显示进度；没有是 null
        "active_job": await catalog_jobs.job_brief(session, await catalog_jobs.active_job(session, source.id)),
        # 排队或进行中的剖析任务 {id, status, total, done, waiting_for}；没有是 null
        "active_profile_job": await catalog_profile_jobs.job_brief(
            session, await catalog_profile_jobs.active_job(session, source.id)),
    }


async def _absent(session: AsyncSession, row: DataSource, names: list[str]) -> dict[str, str]:
    """目录还在、表结构里没有的表为什么不在（source_objects.absent_reasons）。上传表格、记录表的结构随版本走：不在就是那一版
    里没有这张表（removed）。"""
    if not names:
        return {}
    if (row.origin or "manual") != "manual":
        return dict.fromkeys(names, source_objects.ABSENT_REMOVED)
    return await source_objects.absent_reasons(session, row, names)


# ---- 对象索引里找表、加入探查范围（要登记在 GET /catalog/{table} 前面：不然 search 会被当成表名）


@router.get("/{source_id}/catalog/search")
async def search_catalog_tables(source_id: str, q: str = Query("", max_length=200),
                                limit: int = Query(50, ge=1, le=200),
                                session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """在对象索引里按名字、注释找表（不分大小写的包含）：{items: [{name, qualified, kind, comment, est_rows, in_scope,
    scope_reason, absent_reason}], total, indexed}。清单只列探查范围内的表，范围外的靠这里找到、再「加入范围」。没有对象
    索引的源（上传表格、升级前探查的）只在表结构里找（indexed=false）。"""
    row, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    schema = cache.get("schema")
    text = q.strip()
    if (row.origin or "manual") == "manual" and await source_objects.has_index(
            session, row.id, source_objects.index_schema(row)):
        items, total = await source_objects.search_index(session, row, text, limit=limit)
        for item in items:
            item["qualified"] = f"{schema}.{item['name']}" if schema else item["name"]
        return {"items": items, "total": total, "indexed": True}
    from app.data.names import name_key

    key = name_key(text)
    hits = [(n, m) for n, m in (cache.get("tables") or {}).items() if key and (
        key in name_key(n) or text in str((m or {}).get("comment") or ""))]
    return {"items": [{"name": n, "qualified": (m or {}).get("qualified", n),
                       "kind": "view" if (m or {}).get("is_view") else "table", "comment": (m or {}).get("comment"),
                       "est_rows": None, "in_scope": True, "scope_reason": None, "absent_reason": None}
                      for n, m in hits[:limit]], "total": len(hits), "indexed": False}


class ScopeAddIn(BaseModel):
    #: 要加进探查范围的表（对象索引里的名字），一次最多 ADD_LIMIT 张
    tables: list[str] = Field(min_length=1, max_length=source_objects.ADD_LIMIT)


@router.post("/{source_id}/catalog/scope/add")
async def add_catalog_scope(source_id: str, payload: ScopeAddIn, x_actor: str | None = Header(default=None),
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """把对象索引里探查范围外的表加进探查范围：当场只读读结构、写进表结构（schema_cache），并记进探查范围规则的「显式
    列出」，以后每次探查都带上；留痕记谁、什么时候。返回 {added, already, failed: [{name, error}], refused: [{name, reason}],
    scope}。上传表格、记录表 409（结构随版本走）。"""
    row = await _source_row(session, source_id)
    if (row.origin or "manual") != "manual":
        raise HTTPException(409, "上传的表格、记录表没有探查范围：结构随版本走")
    got = await source_objects.add_to_scope(row.id, [str(t) for t in payload.tables], reason="explicit",
                                            trail=source_objects.Trail(via=source_objects.VIA_CATALOG,
                                                                       actor=actor_of(x_actor)))
    cache = got.cache if got.cache is not None else (row.schema_cache or {})
    return {"added": got.added, "already": got.already,
            "failed": [{"name": n, "error": e} for n, e in got.failed.items()],
            "refused": [{"name": n, "reason": r} for n, r in got.refused.items()],
            "scope": dict(cache["scope"]) if isinstance(cache.get("scope"), dict) else None}


# ---- 审阅队列、审阅进度（要登记在 GET /catalog/{table} 前面：不然 review-queue、review-progress 会被当成表名）


@router.get("/{source_id}/catalog/review-queue")
async def catalog_review_queue(source_id: str,
                               limit: int = Query(50, ge=1, le=catalog_review.QUEUE_LIMIT_MAX),
                               offset: int = Query(0, ge=0),
                               kind: Literal["all", "facts", "names"] = Query("all"),
                               table: str | None = Query(None, max_length=255),
                               session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """审阅队列：只列推断的项，按使用次数、风险、来源排好（data/catalog_review.py）。kind 筛影响数字的项（facts）或名称
    和说明（names），table 只看一张表。"""
    _, source = await _resolved(session, source_id)
    return await catalog_review.review_queue(session, source, kind=kind, table=table or None, limit=limit,
                                             offset=offset)


@router.get("/{source_id}/catalog/review-progress")
async def catalog_review_progress(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """审阅进度：常用 20 张表、全部、影响数字的项各自确认了多少。"""
    _, source = await _resolved(session, source_id)
    return await catalog_review.review_progress(session, source)


# ---- 导出导入（导出同样要登记在 GET /catalog/{table} 前面）


@router.get("/{source_id}/catalog/export")
async def export_catalog(source_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    """导出这个数据源的数据目录：JSON 附件「<数据源名>-数据目录.json」，含全部状态的项（data/catalog_transfer.py）。"""
    from fastapi.responses import JSONResponse

    row = await _source_row(session, source_id)
    body = await catalog_transfer.export_catalog(session, row)
    name = catalog_transfer.attachment_name(row.name)
    return JSONResponse(body, headers={"Content-Disposition": catalog_transfer.content_disposition(name, row.id)})


class CatalogImportIn(BaseModel):
    #: 导出的那个对象（格式在 data/catalog_transfer.read_file 里核对，不对 422，说明是中文）
    content: Any = None
    #: 目标还没有起草要求时，是否写入文件里的起草要求
    include_guidance: bool = False


async def _import_source(session: AsyncSession, source_id: str) -> tuple[DataSource, Any]:
    row, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    if not cache.get("tables"):
        raise CodedHTTPException(409, f"{introspect.why_empty(cache)}，无法导入数据目录。请先在数据源卡片上点「探查结构」",
                                 SCHEMA_MISSING)
    return row, source


@router.post("/{source_id}/catalog/import/preview")
async def preview_catalog_import(source_id: str, payload: CatalogImportIn,
                                 session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """导入预览（只读）：每张对上的表新增、更新、保留几项，冲突和跳过的逐条列出。文件格式不对 422；没有表结构 409。"""
    row, source = await _import_source(session, source_id)
    try:
        report = await catalog_transfer.plan_import(session, row, source, payload.content,
                                                    include_guidance=payload.include_guidance)
    except catalog_transfer.TransferInvalid as e:
        raise HTTPException(422, str(e)) from e
    return report.as_dict()


@router.post("/{source_id}/catalog/import")
async def import_catalog(source_id: str, payload: CatalogImportIn, x_actor: str | None = Header(default=None),
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """导入：规则同预览，逐表写入（乐观锁），返回同样形状的报告和每张表写入后的版本。有排队或进行中的起草任务（或正在
    同步起草、导入）409 catalog_draft_busy，有剖析任务 409 profile_busy（说法的最后一句说「再导入」）；文件格式不对 422；
    没有表结构 409。"""
    row, source = await _import_source(session, source_id)
    try:
        async with catalog_jobs.import_write(session, row):
            report = await catalog_transfer.run_import(session, row, source, payload.content,
                                                       include_guidance=payload.include_guidance,
                                                       actor=actor_of(x_actor))
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    except catalog_profile_jobs.ProfileBusy as e:
        raise CodedHTTPException(409, str(e), PROFILE_BUSY) from e
    except introspect_jobs.IntrospectBusy as e:
        raise CodedHTTPException(409, str(e), INTROSPECT_BUSY) from e
    except catalog_transfer.TransferInvalid as e:
        raise HTTPException(422, str(e)) from e
    return report.as_dict()


# ---- 后台剖析任务（同样登记在 GET /catalog/{table} 前面：active、latest 要先于 /profile-jobs/{job_id}）


async def _profile_source(session: AsyncSession, source_id: str) -> tuple[DataSource, Any]:
    """建剖析任务、重试之前的几道门，409 都带机读码（界面按它分情况给下一步，不从原话里认）：未开启 profile_disabled、
    数据源停用 datasource_inactive、快照登记被改动 snapshot_tampered、没有表结构 schema_missing。返回 (数据源记录,
    读表结构用的源)。"""
    row = await _source_row(session, source_id)
    try:
        catalog_profile.ensure_enabled(row)
    except catalog_profile.ProfileDisabled as e:
        raise CodedHTTPException(409, str(e), e.code) from e
    try:
        source = await table_versions.resolve_source(session, row)
    except SnapshotTampered as e:          # 快照清单对不上（SnapshotManifestMismatch 同时是 SnapshotMissing）
        raise CodedHTTPException(409, str(e), SNAPSHOT_TAMPERED) from e
    except table_versions.SnapshotMissing as e:
        raise HTTPException(409, str(e)) from e
    cache = source.schema_cache or {}
    if not cache.get("tables"):
        raise CodedHTTPException(409, f"{introspect.why_empty(cache)}，无法进行数据剖析。请先在数据源卡片上点「探查结构」",
                                 SCHEMA_MISSING)
    return row, source


async def _start_profile(session: AsyncSession, row: DataSource, source: Any, *, tables: list[str] | None,
                         scope: str, actor: str | None, retry_of: str | None = None) -> dict[str, Any]:
    try:
        job, tasks = await catalog_profile_jobs.create_job(
            session, row, source, tables=tables, scope=scope, settings=catalog_profile.ensure_enabled(row),
            actor=actor, retry_of=retry_of)
    except catalog_profile_jobs.ProfileBusy as e:
        raise CodedHTTPException(409, str(e), PROFILE_BUSY) from e
    except catalog_profile_jobs.ProfileNothing as e:
        raise CodedHTTPException(409, str(e), PROFILE_NOTHING) from e
    catalog_profile_jobs.manager.start(job.id)
    return {"job": catalog_profile_jobs.job_dict(job, tasks,
                                                 waiting_for=await catalog_profile_jobs.waiting_for(session, job))}


@router.post("/{source_id}/catalog/profile-jobs", status_code=202)
async def create_profile_job(source_id: str, payload: ProfileJobIn, x_actor: str | None = Header(default=None),
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建后台剖析任务，马上返回（202，任务是排队中）。tables 非空时范围是 selected（表结构里没有、视图、没有可剖析的项照样
    建进任务、状态 skipped），否则按 scope（缺省 needed）挑。同源有排队或进行中的剖析任务 409 profile_busy（说清做完了几张；
    界面改去 GET /profile-jobs/active 显示那一个），按范围挑出 0 张表 409 profile_nothing；同源有起草任务时照建，排在它后面
    （waiting_for = "draft"），起草结束后自动开始。"""
    if payload.tables and len(payload.tables) > catalog_profile_jobs.MAX_TABLES:
        raise HTTPException(422, f"一次最多剖析 {catalog_profile_jobs.MAX_TABLES} 张表，当前 {len(payload.tables)} 张，"
                                 "请分几次剖析")
    row, source = await _profile_source(session, source_id)
    tables = [str(t) for t in payload.tables] if payload.tables else None
    scope = "selected" if tables else (payload.scope or "needed")
    return await _start_profile(session, row, source, tables=tables, scope=scope, actor=actor_of(x_actor))


@router.get("/{source_id}/catalog/profile-jobs/active")
async def active_profile_job(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """排队或进行中的那个剖析任务，没有是 null。"""
    row = await _source_row(session, source_id)
    return {"job": await catalog_profile_jobs.job_out(session, await catalog_profile_jobs.active_job(session, row.id))}


@router.get("/{source_id}/catalog/profile-jobs/latest")
async def latest_profile_job(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """最近建的一个剖析任务（任何状态），没有是 null。"""
    row = await _source_row(session, source_id)
    return {"job": await catalog_profile_jobs.job_out(session, await catalog_profile_jobs.latest_job(session, row.id))}


async def _profile_job_or_404(session: AsyncSession, source_id: str, job_id: str) -> tuple[DataSource, Any]:
    row = await _source_row(session, source_id)
    job = await catalog_profile_jobs.load(session, row.id, job_id)
    if job is None:
        raise HTTPException(404, "剖析任务不存在，可能属于别的数据源，已随数据源删除，或者超过保留数被清理了")
    return row, job


@router.get("/{source_id}/catalog/profile-jobs/{job_id}")
async def get_profile_job(source_id: str, job_id: str,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _, job = await _profile_job_or_404(session, source_id, job_id)
    return {"job": await catalog_profile_jobs.job_out(session, job)}


@router.get("/{source_id}/catalog/profile-jobs/{job_id}/tasks/{seq}")
async def get_profile_task(source_id: str, job_id: str, seq: int,
                           session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """剖析任务里的一张表（按剖析顺序的序号）：检查项、发现、跳过的项、日期范围、发过的查询（字面量已换成「?」）。"""
    _, job = await _profile_job_or_404(session, source_id, job_id)
    task = await catalog_profile_jobs.load_task(session, job, seq)
    if task is None:
        raise HTTPException(404, "这次剖析里没有这一张表")
    return {"task": catalog_profile_jobs.task_detail(job, task)}


@router.post("/{source_id}/catalog/profile-jobs/{job_id}/cancel")
async def cancel_profile_job(source_id: str, job_id: str,
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """停止：排队的直接停止；进行中的不再等正在跑的那条查询（数据库按单条时限截止它），这张表做完的项照常写进目录、没做的
    保留旧结论，后面的表记停止。已经结束的 409 catalog_profile_finished。返回停止之后的任务。"""
    row, job = await _profile_job_or_404(session, source_id, job_id)
    owner = row.id
    try:
        await catalog_profile_jobs.cancel(session, job)
    except catalog_profile_jobs.ProfileFinished as e:
        raise CodedHTTPException(409, str(e), CATALOG_PROFILE_FINISHED) from e
    session.expire_all()                  # 后台在别的会话里收的尾：重新读
    return {"job": await catalog_profile_jobs.job_out(session, await catalog_profile_jobs.load(session, owner, job_id))}


@router.post("/{source_id}/catalog/profile-jobs/{job_id}/retry", status_code=202)
async def retry_profile_job(source_id: str, job_id: str, x_actor: str | None = Header(default=None),
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """重试：新任务，只含原任务里 failed、partial、cancelled 的表（按原来的顺序），范围 selected，设置取数据源当前的。
    同源有排队或进行中的剖析任务 409 profile_busy；没有可重试的表 422；开始不了的几种情况同建任务。"""
    _, old = await _profile_job_or_404(session, source_id, job_id)
    row, source = await _profile_source(session, source_id)
    try:
        await catalog_profile_jobs.ensure_free(session, row)
    except catalog_profile_jobs.ProfileBusy as e:
        raise CodedHTTPException(409, str(e), PROFILE_BUSY) from e
    try:
        tables = await catalog_profile_jobs.retry_tables(session, old)
    except catalog_profile_jobs.NothingToRetry as e:
        raise HTTPException(422, str(e)) from e
    return await _start_profile(session, row, source, tables=tables, scope="selected", actor=actor_of(x_actor),
                                retry_of=old.id)


@router.get("/{source_id}/catalog/{table}")
async def get_catalog_table(source_id: str, table: str,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _, source = await _resolved(session, source_id)
    key, entry = await _table_key(session, source, table)
    return await _detail(session, source, key, entry)


async def _source_row(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if row is None:
        raise HTTPException(404, "数据源不存在，可能已被删除")
    return row


@router.put("/{source_id}/catalog/guidance")
async def put_catalog_guidance(source_id: str, body: dict[str, Any] = Body(...),
                               x_actor: str | None = Header(default=None),
                               session: AsyncSession = Depends(get_session)) -> Any:
    """存数据源级的起草要求：去首尾空白，空串清除，最多 2000 字（超了 422）。上传表格的源存在它自己的数据源记录上。

    要登记在 PUT /catalog/{table} 前面：不然「guidance」会被当成表名。反过来，表名恰好叫 guidance 时，整份提交那张表
    目录的请求（{notes, if_version}，没有 guidance 这一项）也落到这里：照旧当作那张表的整份提交。"""
    if "guidance" not in body and "notes" in body:
        try:
            table_payload = CatalogPutIn.model_validate(body)
        except ValidationError as e:
            raise RequestValidationError(e.errors()) from e
        return await put_catalog_table(source_id, "guidance", table_payload, x_actor=x_actor, session=session)
    if "guidance" not in body:
        raise HTTPException(422, GUIDANCE_MISSING)
    guidance = body["guidance"]
    if guidance is not None and not isinstance(guidance, str):
        raise HTTPException(422, GUIDANCE_NOT_TEXT)
    row = await _source_row(session, source_id)
    try:
        value = await catalog_jobs.save_guidance(session, row, guidance)
    except catalog_jobs.TextTooLong as e:
        raise HTTPException(422, str(e)) from e
    return {"guidance": value}


async def _ensure_model(session: AsyncSession) -> None:
    """起草模型用不了（没有接入、停用、只有演示接入……）：409，不建任务。说法同同步起草时的 model_error。"""
    try:
        await catalog_jobs.check_model(session)
    except catalog.CatalogModelUnavailable as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_MODEL_UNAVAILABLE) from e


def _no_schema(cache: dict[str, Any]) -> HTTPException:
    return HTTPException(409, f"{introspect.why_empty(cache)}，无法起草数据目录。请先在数据源卡片上点「探查结构」")


@router.post("/{source_id}/catalog/draft-jobs", status_code=202)
async def create_draft_job(source_id: str, payload: DraftJobIn, x_actor: str | None = Header(default=None),
                           session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """建后台起草任务，马上返回（202，任务是排队中）。同一个源已有排队或进行中的任务 409 catalog_draft_busy（界面改去
    GET /draft-jobs/active 显示那一个）；用模型而起草模型用不了 409 catalog_draft_model_unavailable；没有表结构 409。
    表结构里没有的表照样建进任务、状态 skipped。"""
    if payload.tables and len(payload.tables) > catalog_jobs.MAX_TABLES:
        raise HTTPException(422, f"一次最多起草 {catalog_jobs.MAX_TABLES} 张表，当前 {len(payload.tables)} 张，"
                                 "请分几次起草")
    try:
        note = catalog_jobs.clean_text(payload.note, catalog_jobs.NOTE_MAX, "本次说明")
    except catalog_jobs.TextTooLong as e:
        raise HTTPException(422, str(e)) from e
    row, source = await _resolved(session, source_id)
    try:
        await catalog_jobs.ensure_draft_free(session, row)
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    cache = source.schema_cache or {}
    if not cache.get("tables"):
        raise _no_schema(cache)
    if payload.use_model:
        await _ensure_model(session)
    try:
        job, tasks = await catalog_jobs.create_job(session, row, source, tables=payload.tables or None,
                                                   use_model=payload.use_model, note=note, actor=actor_of(x_actor))
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    catalog_jobs.manager.start(job.id)
    return {"job": catalog_jobs.job_dict(job, tasks, waiting_for=await catalog_jobs.waiting_for(session, job))}


@router.get("/{source_id}/catalog/draft-jobs/active")
async def active_draft_job(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """排队或进行中的那个起草任务，没有是 null。"""
    row = await _source_row(session, source_id)
    return {"job": await catalog_jobs.job_out(session, await catalog_jobs.active_job(session, row.id))}


@router.get("/{source_id}/catalog/draft-jobs/latest")
async def latest_draft_job(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """最近建的一个起草任务（任何状态），没有是 null。"""
    row = await _source_row(session, source_id)
    return {"job": await catalog_jobs.job_out(session, await catalog_jobs.latest_job(session, row.id))}


async def _job_or_404(session: AsyncSession, source_id: str, job_id: str) -> tuple[DataSource, Any]:
    row = await _source_row(session, source_id)
    job = await catalog_jobs.load(session, row.id, job_id)
    if job is None:
        raise HTTPException(404, "起草任务不存在，可能属于别的数据源，或者已随数据源删除")
    return row, job


@router.get("/{source_id}/catalog/draft-jobs/{job_id}")
async def get_draft_job(source_id: str, job_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _, job = await _job_or_404(session, source_id, job_id)
    return {"job": await catalog_jobs.job_out(session, job)}


@router.post("/{source_id}/catalog/draft-jobs/{job_id}/cancel")
async def cancel_draft_job(source_id: str, job_id: str,
                           session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """停止：排队的直接停止；进行中的停掉正在进行的模型调用，没开始和没写完的表记停止，已经写进目录的不回滚。
    已经结束的 409 catalog_draft_finished。返回停止之后的任务。"""
    row, job = await _job_or_404(session, source_id, job_id)
    owner = row.id
    try:
        await catalog_jobs.cancel(session, job)
    except catalog_jobs.DraftFinished as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_FINISHED) from e
    session.expire_all()                  # 后台在别的会话里收的尾：重新读
    return {"job": await catalog_jobs.job_out(session, await catalog_jobs.load(session, owner, job_id))}


@router.post("/{source_id}/catalog/draft-jobs/{job_id}/retry", status_code=202)
async def retry_draft_job(source_id: str, job_id: str, x_actor: str | None = Header(default=None),
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """重试：新任务，只含原任务里没写好（failed、partial）和停止了（cancelled）的表；用不用模型、本次说明沿用，起草要求
    取数据源当前的。有排队或进行中的任务 409 catalog_draft_busy；没有可重试的表 422；起草模型用不了 409。"""
    _, old = await _job_or_404(session, source_id, job_id)
    row, source = await _resolved(session, source_id)
    try:
        await catalog_jobs.ensure_draft_free(session, row)
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    try:
        tables = await catalog_jobs.retry_tables(session, old)
    except catalog_jobs.NothingToRetry as e:
        raise HTTPException(422, str(e)) from e
    cache = source.schema_cache or {}
    if not cache.get("tables"):
        raise _no_schema(cache)
    if old.use_model:
        await _ensure_model(session)
    try:
        job, tasks = await catalog_jobs.create_job(session, row, source, tables=tables, use_model=old.use_model,
                                                   note=old.note, actor=actor_of(x_actor), retry_of=old.id)
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    catalog_jobs.manager.start(job.id)
    return {"job": catalog_jobs.job_dict(job, tasks, waiting_for=await catalog_jobs.waiting_for(session, job))}


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
    """同步起草（界面改用后台起草任务，这个接口留着，返回形状不变）。引擎同后台任务（分段、按失败原因处理），起草要求
    取数据源级的那份。返回每张表新增、更新、删除了几项；模型用不了或某张表的模型起草失败，照实写在结果里。

    model_items：模型给了这张表几项可用内容（没用模型、模型这部分失败时为 null）。界面据此区分「模型给了内容、
    只是和现有目录一致」（model_items > 0、三个数都是 0）和「模型没给内容」（model_error 有值）。

    双向互斥（catalog_jobs.sync_draft，进程内登记）：同一个源有排队或进行中的后台起草任务、或者正在同步起草时 409
    catalog_draft_busy（和建任务同一句话）；反过来，同步起草进行期间同一个源建任务、重试、导入目录也是 409。两边同时写
    同一个源的目录、抢模型名额。
    """
    row, source = await _resolved(session, source_id)
    cache = source.schema_cache or {}
    try:
        async with catalog_jobs.sync_draft(session, row):
            if not cache.get("tables"):
                raise _no_schema(cache)
            report = await catalog.draft_catalog(session, source, tables=payload.tables or None,
                                                 use_model=payload.use_model, actor=actor_of(x_actor),
                                                 guidance=catalog_jobs.guidance_of(row))
    except catalog_jobs.DraftBusy as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_BUSY) from e
    except catalog_profile_jobs.ProfileBusy as e:
        raise CodedHTTPException(409, str(e), PROFILE_BUSY) from e
    except introspect_jobs.IntrospectBusy as e:
        raise CodedHTTPException(409, str(e), INTROSPECT_BUSY) from e
    rows = [{"table_name": r.table, "added": r.added, "updated": r.updated, "removed": r.removed,
             "version": r.version, "error": r.error, "model_error": r.model_error, "model_items": r.model_items}
            for r in report.tables]
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


class CatalogBatchReviewItem(BaseModel):
    table_name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=600)
    action: Literal["confirm", "reject", "reset"]
    if_version: int = Field(ge=0)


class CatalogBatchReviewIn(BaseModel):
    items: list[CatalogBatchReviewItem] = Field(min_length=1, max_length=catalog_review.BATCH_MAX)


@router.post("/{source_id}/catalog/review/batch")
async def review_catalog_batch(source_id: str, payload: CatalogBatchReviewIn,
                               x_actor: str | None = Header(default=None),
                               session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """批量审阅：逐条结果和请求一一对应。逐表按 if_version 乐观锁，同一张表的几条一次写入；一张表冲突、路径不对、表不存在
    只记在那几条上（ok 为假，code 是 catalog_conflict / catalog_path_invalid / table_missing），不影响别的。"""
    _, source = await _resolved(session, source_id)
    items = [catalog_review.BatchItem(i.table_name, i.path, i.action, i.if_version) for i in payload.items]
    return {"results": await catalog_review.review_batch(session, source, items, actor=actor_of(x_actor))}


@router.get("/{source_id}/catalog/{table}/usage")
async def catalog_table_runs(source_id: str, table: str,
                             limit: int = Query(20, ge=1, le=catalog_review.RUNS_LIMIT_MAX),
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """用过这张表的运行，新的在前。按运行计：total 是几次运行，一次运行查了这张表几次都只算一次。和表清单的使用次数
    （catalog.table_usage）数的是同一批查询快照，但使用次数按查询计（同一次运行里查了两次算两次），所以使用次数可能比 total
    大。"""
    _, source = await _resolved(session, source_id)
    key, _ = await _table_key(session, source, table)
    return await catalog_review.table_runs(session, source, key, limit=limit)


def _tables_of(source: Any) -> dict[str, Any] | None:
    """核对提案用的表结构；没有表结构（尚未探查）时不核对列名。"""
    return ((source.schema_cache or {}).get("tables") or None) if source is not None else None


@router.post("/{source_id}/catalog/{table}/patch/preview")
async def preview_catalog_patch(source_id: str, table: str, payload: CatalogPatchPreviewIn,
                                session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """目录修改提案的预览：对着当前目录逐项算改前、改后，不写库。

    助手转出的提案卡片在保存遇到 409（别人刚改过这张表）时用它重新载入：改前的值、码值补充后的样子都按
    最新的目录重算，人看过之后再带新版本保存。
    """
    _, source = await _resolved(session, source_id)
    key, entry = await _table_key(session, source, table)
    plan = catalog.plan_patch(entry.notes if entry else {}, [c.model_dump() for c in payload.changes], table=key,
                              tables=_tables_of(source))
    return {"table_name": key, "version": entry.version if entry else 0,
            "changes": [c.as_dict() for c in plan.changes], "problems": plan.problems}


@router.post("/{source_id}/catalog/{table}/patch")
async def save_catalog_patch(source_id: str, table: str, payload: CatalogPatchIn,
                             x_actor: str | None = Header(default=None),
                             session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """保存一条目录修改提案：改动过的项记为人工填写、已确认，写了依据的项依据另存成这一项确认时的依据（reason，和备注
    分开，规则见 catalog.apply_patch）。版本不符 409；有一项不合法 422，整条不写。"""
    _, source = await _resolved(session, source_id)
    key, _ = await _table_key(session, source, table)
    try:
        entry = await catalog.save_patch(session, source.id, key, [c.model_dump() for c in payload.changes],
                                         if_version=payload.if_version, actor=actor_of(x_actor),
                                         tables=_tables_of(source))
    except catalog.CatalogConflict as e:
        raise _conflict(e) from e
    except catalog.CatalogInvalid as e:
        raise _invalid(e) from e
    return await _detail(session, source, key, entry)


class AssistantMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=catalog_assistant.MESSAGE_MAX)


class AssistantIn(BaseModel):
    #: 整段对话（服务端不存）；没有使用者的话时助手先开口
    messages: list[AssistantMessageIn] = Field(default_factory=list, max_length=catalog_assistant.MAX_MESSAGES)
    #: 界面上还没保存的修改，让模型知道。不设上限：宽表上几轮攒下来常常超过一次保存的 200 项，不能因此不让问（背景里只列
    #: 最近的 catalog_assistant.PENDING_SHOWN 项，码值照全部合并）
    pending: list[CatalogPatchChange] = Field(default_factory=list)


@router.post("/{source_id}/catalog/{table}/assistant")
async def catalog_table_assistant(source_id: str, table: str, payload: AssistantIn,
                                  session: AsyncSession = Depends(get_session)) -> Any:
    """表助手：和使用者对话完善这张表的目录，返回 text/event-stream（事件见 data/catalog_assistant.py）。提案只交给界面，
    不写目录；保存走 /catalog/{table}/patch。开始前：表不在 404；对话不合规 422；起草模型用不了 409
    catalog_draft_model_unavailable（说法同起草任务）。"""
    from fastapi.responses import StreamingResponse

    row, source = await _resolved(session, source_id)
    key = catalog.resolve_table_name(source.schema_cache, table)
    if key is None:
        raise await _missing(session, source, table)
    messages = [m.model_dump() for m in payload.messages]
    if problem := catalog_assistant.conversation_problem(messages):
        raise HTTPException(422, problem)
    try:
        model, model_id = await catalog_assistant.resolve_model(session)
    except catalog.CatalogModelUnavailable as e:
        raise CodedHTTPException(409, str(e), CATALOG_DRAFT_MODEL_UNAVAILABLE) from e
    context = await catalog_assistant.prepare(session, row, source, key,
                                              pending=[c.model_dump() for c in payload.pending])
    return StreamingResponse(catalog_assistant.stream(model, model_id, context, messages),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{source_id}/catalog/{table}/impact")
async def catalog_table_impact(source_id: str, table: str,
                               session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """引用这张表的已发布、受管模板，每个模板按当前的已发布版本算：调用工具节点的 SQL 用到了这张表算直接引用，
    Agent 绑定了这个源的查询工具算可能涉及，合并查询按输入追溯。改目录之前、保存纠正之后都看它。"""
    row, source = await _resolved(session, source_id)
    key, _ = await _table_key(session, source, table)
    tables = _tables_of(source)
    templates = await catalog_impact.table_impact(session, row.name, key, list(tables) if tables else None)
    return {"table": key, "templates": templates}
