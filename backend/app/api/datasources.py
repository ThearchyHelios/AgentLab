"""数据源的增删改查、连接测试、结构探查。

密码的处理规则和 Provider 完全一致（app/api/settings.py）：入库加密、
出站只给掩码、PATCH 时不传表示不动、传空串表示清空。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health
from app.api.coded import PROFILE_SETTINGS_INVALID, CodedHTTPException
from app.api.runs import actor_of
from app.core.errors import AUTH, NETWORK, TIMEOUT, classify, explain, raw
from app.core.config import settings
from app.core.crypto import encrypt, mask
from app.data import catalog as data_catalog
from app.data.catalog_profile import profile_settings_rejection
from app.data import introspect as introspect_mod
from app.data import table_versions
from app.data.engine import (
    MASK_COLUMNS_OPTION, MAX_QUERY_TIMEOUT_S, QUERY_TIMEOUT_OPTION, SUPPORTED_KINDS, SnapshotTampered, build_url,
    engine_args, engines, mask_columns_problem, query_timeout_problem,
)
from app.db.base import get_session, new_id
from app.db.models import DataSource, ImportStaging, SourceSnapshot, TableImport, TableRecipe
from app.tools.datasource import tool_names

router = APIRouter(prefix="/api/datasources", tags=["datasources"])
logger = logging.getLogger(__name__)

# 名字会成为工具名的一部分（db_query__<name>），而模型的 function name 只认
# ASCII。所以这里收紧成 slug，中文说明放 description 字段。
_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"


class DataSourceIn(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN,
                      description="英文标识，会成为工具名的一部分，如 sales")
    kind: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    readonly: bool = True
    description: str = ""
    enabled: bool = True


class DataSourcePatch(BaseModel):
    kind: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None  # 空串=清空；不传=不动
    options: dict[str, Any] | None = None
    readonly: bool | None = None
    description: str | None = None
    enabled: bool | None = None


class DataSourceOut(BaseModel):
    id: str
    name: str
    kind: str
    host: str | None
    port: int | None
    database: str | None
    username: str | None
    options: dict[str, Any]
    readonly: bool
    description: str
    enabled: bool
    password_masked: str = ""
    has_password: bool = False
    table_count: int = 0
    schema_synced_at: str | None = None
    #: 上次探查为什么没拿到表。"还没探查"和"探查失败了"是两回事：
    #: 前者去点一下按钮就行，后者点了也没用，得先解决超时或者换 schema
    schema_error: str = ""
    #: 这台服务器上还有哪些库/schema 可选。探不到表时它就是下一步的线索
    available_schemas: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    #: 缓存是按哪个 schema 探的："" 是默认 schema，None 是没有缓存（或老缓存没记）。
    #: 和 options.schema 对不上时，助手看到的结构不是配置里那个 schema 的
    cached_schema: str | None = None
    #: 最近一次测连接（见 app/api/health.py）。没测过、或者连接配置改过之后都是 None
    last_checked_at: str | None = None
    last_check_ok: bool | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None
    #: upload：上传的表格（连接信息由系统维护，不能手改、不能重新探查）；manual：手工登记的连接
    origin: str = "manual"
    #: 上传表格当前启用的版本：{id, created_at, file_name, raw_state}。手工源恒为 None
    current_snapshot: dict[str, Any] | None = None
    #: simple：简单导入的上传表格；recipe：按配方导入；手工源为 None
    import_mode: str | None = None
    #: 按配方导入的源当前启用的配方：{id, seq, origin, activated_at, signed_by}
    current_recipe: dict[str, Any] | None = None
    #: 这个源最近一个未结束的导入暂存区：{id, kind, status, created_at}
    open_staging: dict[str, Any] | None = None


def _cached_schema(cache: dict[str, Any]) -> str | None:
    if not cache or "schema" not in cache:
        return None
    return str(cache.get("schema") or "")


def _import_mode(row: DataSource) -> str | None:
    if row.current_recipe_id:
        return "recipe"
    return "simple" if _is_upload(row) else None


def _to_out(row: DataSource, snapshot: dict[str, Any] | None = None,
            recipe_info: dict[str, Any] | None = None) -> DataSourceOut:
    cache = row.schema_cache or {}
    tables = cache.get("tables") or {}
    recipe_info = recipe_info or {}
    return DataSourceOut(
        origin=row.origin or "manual", current_snapshot=snapshot,
        import_mode=_import_mode(row), current_recipe=recipe_info.get("current_recipe"),
        open_staging=recipe_info.get("open_staging"),
        cached_schema=_cached_schema(cache),
        schema_error=str(cache.get("error") or "") if cache.get("failed") else "",
        available_schemas=list(cache.get("available_schemas") or []),
        id=row.id, name=row.name, kind=row.kind, host=row.host, port=row.port,
        database=row.database, username=row.username, options=row.options or {},
        readonly=row.readonly, description=row.description, enabled=row.enabled,
        password_masked=mask(row.password),
        has_password=bool(row.password),
        table_count=len(tables),
        schema_synced_at=row.schema_synced_at.isoformat() if row.schema_synced_at else None,
        tools=tool_names(row),
        **health.fields(row.last_check),
    )


def _connection(source: Any) -> str | None:
    """连接配置的指纹：驱动实际拿到的连接串和参数。配置拼不出连接串时为 None。

    按连接串比而不是按字段比：Oracle 的服务名填在「数据库」还是 options 里，
    连的是同一个库；探查用的 schema 不进连接串，改它也不影响连不连得上。
    """
    try:
        url, extra = engine_args(source)
    except Exception:  # noqa: BLE001 - 拼不出来就谈不上「测的是同一份」
        return None
    return health.fingerprint("datasource", url, extra)


def _probe_record(result: dict[str, Any]) -> dict[str, Any]:
    return health.record(result.get("ok"), result.get("elapsed_ms"), result.get("error"))


async def _get_or_404(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if not row:
        raise HTTPException(404, "数据源不存在，可能已被删除")
    return row


def _is_upload(row: DataSource) -> bool:
    return (row.origin or "manual") == "upload"


async def _snapshot_infos(session: AsyncSession, rows: list[DataSource]) -> dict[str, dict[str, Any]]:
    """各上传源当前版本的摘要：{源 id: {id, created_at, file_name, raw_state, mode, periods, period_start,
    period_end, activated_at}}。两次查询取齐，不逐个查。

    文件名、原件状态取最近的那一期（按统计期排在最后的）；期 3 按期累积时一个快照有多期，卡片另写模式、期数、
    统计期范围。activated_at 是最近一次成为当前版本的时刻：回滚之后卡片写「…启用」，不再写快照当初的导入时间
    （期 3 之前的快照没有这一列，按 created_at）。"""
    wanted = {r.current_snapshot_id: r.id for r in rows if _is_upload(r) and r.current_snapshot_id}
    if not wanted:
        return {}
    snaps = list((await session.execute(
        select(SourceSnapshot).where(SourceSnapshot.id.in_(list(wanted)))
    )).scalars())
    members = {str(x) for s in snaps for x in s.imports or []}
    imports = {
        i.id: i for i in (await session.execute(
            select(TableImport).where(TableImport.id.in_(list(members)))
        )).scalars()
    } if members else {}
    out: dict[str, dict[str, Any]] = {}
    for snap in snaps:
        mine = [imports[str(x)] for x in snap.imports or [] if str(x) in imports]
        imp = mine[-1] if mine else None
        starts = [i.period_start for i in mine if i.period_start]
        ends = [i.period_end for i in mine if i.period_end]
        out[snap.source_id] = {
            "id": snap.id,
            "created_at": snap.created_at.isoformat() if snap.created_at else None,
            "file_name": imp.file_name if imp else "",
            "raw_state": imp.raw_state if imp else "absent",
            "mode": snap.mode or ("replace" if imp is not None and imp.recipe_id else None),
            "periods": len(snap.imports or []),
            "period_start": min(starts) if starts else None,
            "period_end": max(ends) if ends else None,
            "activated_at": _iso(snap.activated_at or snap.created_at),
        }
    return out


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


async def _recipe_infos(session: AsyncSession, rows: list[DataSource]) -> dict[str, dict[str, Any]]:
    """各源的当前配方摘要和最近一个未结束的暂存区：{源 id: {current_recipe, open_staging}}。两次查询取齐。"""
    out: dict[str, dict[str, Any]] = {}
    recipe_ids = {r.current_recipe_id: r.id for r in rows if r.current_recipe_id}
    if recipe_ids:
        for rec in (await session.execute(
            select(TableRecipe).where(TableRecipe.id.in_(list(recipe_ids)))
        )).scalars():
            sid = recipe_ids.get(rec.id)
            if sid is not None:
                out.setdefault(sid, {})["current_recipe"] = {
                    "id": rec.id, "seq": rec.seq, "origin": rec.origin,
                    "activated_at": _iso(rec.activated_at), "signed_by": rec.signed_by,
                }
    ids = [r.id for r in rows if _is_upload(r)]
    if ids:
        for st in (await session.execute(
            select(ImportStaging).where(
                ImportStaging.source_id.in_(ids), ImportStaging.status.in_(table_versions.STAGING_OPEN),
            ).order_by(ImportStaging.created_at)
        )).scalars():
            # 按创建时间升序，后面的覆盖前面的：留下最近的那一个
            out.setdefault(st.source_id, {})["open_staging"] = {
                "id": st.id, "kind": st.kind, "status": st.status, "created_at": _iso(st.created_at),
            }
    return out


async def _out(session: AsyncSession, row: DataSource) -> DataSourceOut:
    return _to_out(row, (await _snapshot_infos(session, [row])).get(row.id),
                   (await _recipe_infos(session, [row])).get(row.id))


#: 上传源上由系统维护的字段：连接一律从快照解析，database 只作显示
_UPLOAD_LOCKED = {
    "kind": "类型", "host": "主机", "port": "端口", "database": "数据库文件路径",
    "username": "用户名", "password": "密码", "readonly": "只读",
}


#: 手工源指向上传目录时的拒收原因：创建、修改时 422，测连接时作为失败原因交回
_UPLOAD_DIR_REFUSED = "数据库文件路径不能指向上传表格的存放目录。上传的表格请通过「上传表格」更新"
#: 升级前就指向上传目录的手工源（启动时已强制只读）想改回可写时的拒收原因
_UPLOAD_DIR_READONLY = ("这个数据库文件位于上传表格的存放目录，只能以只读方式连接。"
                        "上传的表格请通过「上传表格」更新")


#: 手工源指向 AgentLab 自己的数据文件时的拒收原因：创建、修改时 422，测连接时作为失败原因交回
_APP_FILES_REFUSED = ("数据库文件路径不能指向 AgentLab 自己的数据文件（应用数据库、运行检查点、密钥、工件等）。"
                      "请填写存放业务数据的 SQLite 文件")


def _in_upload_dir(kind: str | None, database: str | None) -> bool:
    return (kind or "").lower() == "sqlite" and table_versions.under_uploads(database)


def _in_app_files(kind: str | None, database: str | None) -> bool:
    return (kind or "").lower() == "sqlite" and table_versions.reaches_app_files(database)


def _reserved_reason(kind: str | None, database: str | None) -> str | None:
    """手工登记的 SQLite 源不许指向的位置：返回拒收原因，没问题时返回 None。

    上传目录归版本底座管，手工源指过去会绕开只读和版本；应用自己的数据文件（元数据库等）登记成
    数据源，经审批的一条 UPDATE 就能改掉上传源的连接字段和快照登记。两处都按文件身份判断。
    """
    if _in_app_files(kind, database):
        return _APP_FILES_REFUSED
    if _in_upload_dir(kind, database):
        return _UPLOAD_DIR_REFUSED
    return None


def _refuse_reserved(kind: str | None, database: str | None) -> None:
    reason = _reserved_reason(kind, database)
    if reason:
        raise HTTPException(422, reason)


def _locked_changes(row: DataSource, data: dict[str, Any]) -> list[str]:
    """上传源上这次要改的、由系统维护的连接字段（给人看的名字）。

    原样带回的值不算修改（编辑表单会把整份配置一起提交）；密码只看是否填了新的——上传源本来就没有密码。
    修改接口和测连接共用：测连接说「能连」、保存却被拒，两边就对不上了。
    """
    return [label for key, label in _UPLOAD_LOCKED.items() if key in data and (
        bool(data[key]) if key == "password" else data[key] != getattr(row, key)
    )]


def _locked_refusal(changed: list[str]) -> str:
    return f"上传的表格由系统维护连接信息，不能修改「{'」「'.join(changed)}」。如需更新数据，请重新上传"


@router.get("", response_model=list[DataSourceOut])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[DataSourceOut]:
    rows = list((await session.execute(select(DataSource).order_by(DataSource.name))).scalars())
    snaps = await _snapshot_infos(session, rows)
    recipes = await _recipe_infos(session, rows)
    return [_to_out(r, snaps.get(r.id), recipes.get(r.id)) for r in rows]


#: 每种库都有的「查询时限」。数据库按它自己停下语句，不只是后端不再等
_TIMEOUT_FIELD = {
    "key": QUERY_TIMEOUT_OPTION, "label": "查询时限（秒）", "placeholder": "30",
    "help": f"单条查询的最长执行时间，超时后由数据库终止；留空为 30 秒，最多 {MAX_QUERY_TIMEOUT_S} 秒",
}


#: 证据面板展示原始行时遮掉的列。每种库都有：遮罩只看查询结果的列名，和方言无关
_MASK_FIELD = {
    "key": MASK_COLUMNS_OPTION, "label": "遮罩的列", "placeholder": "phone, email",
    "help": "证据面板展示查询结果的原始行时，这些列显示为「已遮罩」。"
            "遮罩只减少暴露，不是安全边界：完整快照仍可通过工件获取，SQL 中给列起别名也可绕过",
}


def _refuse_bad_options(options: dict[str, Any] | None) -> None:
    problem = query_timeout_problem(options) or mask_columns_problem(options)
    if problem:
        raise HTTPException(422, problem)
    # 剖析设置：带机读码和出错的那一项（field），表单按它把报错落到那一格，不再从原话里认
    if rejected := profile_settings_rejection(options):
        message, field = rejected
        raise CodedHTTPException(422, message, PROFILE_SETTINGS_INVALID, field=field)


@router.get("/kinds")
async def list_kinds() -> dict[str, Any]:
    """支持哪些数据库，以及各自需要填什么——前端表单据此渲染。

    hint 只用表单上看得见的说法。以前写的是「在 options 里给 service_name」
    「options 可填 charset」，表单上根本没有叫 options 的地方。advanced 列出
    「高级连接参数」里常用的键，hint 提到的每个键都在那里找得到。
    """
    return {
        "kinds": [
            {"value": "mysql", "label": "MySQL / MariaDB", "default_port": 3306,
             "needs": ["host", "database", "username", "password"],
             "hint": "字符集默认为 utf8mb4；如需更改，请在「高级连接参数」中添加 charset",
             "advanced": [{"key": "charset", "label": "字符集", "placeholder": "utf8mb4"},
                          _TIMEOUT_FIELD, _MASK_FIELD]},
            {"value": "postgres", "label": "PostgreSQL", "default_port": 5432,
             "needs": ["host", "database", "username", "password"],
             "hint": "「schema」留空时使用 public",
             "advanced": [_TIMEOUT_FIELD, _MASK_FIELD]},
            {"value": "oracle", "label": "Oracle", "default_port": 1521,
             "needs": ["host", "username", "password"],
             "hint": "service_name 与 SID 二选一，通常填写 service_name；无需安装 Instant Client。"
                     "若只读账号名下没有对象，请在「schema」中填写数据所在的 schema（如 ANALYTICS）",
             "advanced": [
                 {"key": "service_name", "label": "service_name", "placeholder": "ORCLPDB1",
                  "help": "与 SID 二选一"},
                 {"key": "sid", "label": "SID", "placeholder": "ORCL", "help": "与 service_name 二选一"},
                 _TIMEOUT_FIELD,
                 _MASK_FIELD,
             ]},
            {"value": "sqlite", "label": "SQLite（文件）", "default_port": None,
             "needs": ["database"], "hint": "「数据库文件路径」请填写 .db 文件的绝对路径",
             "advanced": [_TIMEOUT_FIELD, _MASK_FIELD]},
        ],
        "supported": list(SUPPORTED_KINDS),
    }


def _oracle_target(options: dict[str, Any] | None) -> tuple[str, str]:
    opts = options or {}
    return str(opts.get("service_name") or "").strip(), str(opts.get("sid") or "").strip()


def _settle_oracle(
    row: DataSource, old_options: dict[str, Any] | None, old_database: str | None
) -> None:
    """Oracle 的 service_name / SID 只存一处：options 里，database 清空。

    引擎按 `options.service_name or database` 取服务名，两者都空才看 sid。老数据
    把服务名存在 database，于是新表单切到 SID 存下去会被它压住、悄悄无视；旧表单
    把 service_name 框绑在 database 上、保存时又原样带回 options，新填的值同样
    被压住。两处都能存，就总有一处在暗中说了算，所以存的时候收成一处：这次
    改了哪一处就听哪一处；都没改就照引擎原来的取法，连的还是同一个库。
    """
    if (row.kind or "").lower() != "oracle":
        return
    opts = {k: v for k, v in (row.options or {}).items()
            if k not in ("service_name", "sid") or str(v or "").strip()}
    database = (row.database or "").strip()
    picked = any(_oracle_target(opts)) and _oracle_target(opts) != _oracle_target(old_options)
    typed = bool(database) and database != (old_database or "").strip()
    if not picked and (typed or (database and "service_name" not in opts)):
        opts["service_name"] = database
        opts.pop("sid", None)
    row.options = opts
    row.database = None


@router.post("", response_model=DataSourceOut, status_code=201)
async def create_source(
    payload: DataSourceIn, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    if payload.kind not in SUPPORTED_KINDS:
        raise HTTPException(400, f"不支持「{payload.kind}」类型的数据库，目前支持：{'、'.join(SUPPORTED_KINDS)}")
    exists = (await session.execute(
        select(DataSource).where(DataSource.name == payload.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"已存在名为「{payload.name}」的数据源，请换一个标识")
    _refuse_bad_options(payload.options)
    _refuse_reserved(payload.kind, payload.database)

    row = DataSource(
        **payload.model_dump(exclude={"password"}),
        password=encrypt(payload.password),
    )
    _settle_oracle(row, None, None)
    # 表单里刚测过这份配置：保存下来就带着那次结果，不必再测一遍
    row.last_check = health.draft_result(_connection(row))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.patch("/{source_id}", response_model=DataSourceOut)
async def update_source(
    source_id: str, payload: DataSourcePatch, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    row = await _get_or_404(session, source_id)
    data = payload.model_dump(exclude_unset=True)
    if _is_upload(row):
        # 上传源的连接由版本底座维护（见 _locked_changes）
        changed = _locked_changes(row, data)
        if changed:
            raise HTTPException(422, _locked_refusal(changed))
        for key in _UPLOAD_LOCKED:
            data.pop(key, None)
        if data.get("options") is not None:
            # 上传库只有一个 main：指定 schema 会让探查去找不存在的对象。原样带回的不算修改，摘掉即可
            options = dict(data["options"])
            wanted = str(options.pop("schema", None) or "").strip()
            if wanted and wanted != str((row.options or {}).get("schema") or "").strip():
                raise HTTPException(422, "上传的表格只有一个库，不能指定 schema。如需更新数据，请重新上传")
            data["options"] = options
    elif "kind" in data or "database" in data:
        _refuse_reserved(data.get("kind", row.kind), data.get("database", row.database))
    elif _in_app_files(row.kind, row.database):
        # 升级前登记的、指向应用自己数据文件的源：启动时已停用（table_versions.startup）。
        # 这类登记没有正当用途，除了改路径和删除，别的修改（重新启用、改回可写……）一律拒
        raise HTTPException(422, _APP_FILES_REFUSED)
    elif data.get("readonly") is False and row.readonly and _in_upload_dir(row.kind, row.database):
        # 升级前就指向上传目录的手工源：启动时已强制只读（table_versions.startup），不许再改回可写
        raise HTTPException(422, _UPLOAD_DIR_READONLY)
    if "options" in data:
        _refuse_bad_options(data["options"])
    if "password" in data:
        # 空串表示清空，非空表示换新的
        data["password"] = encrypt(data["password"]) if data["password"] else None
    old_options, old_database = dict(row.options or {}), row.database
    before = _connection(row)
    for key, value in data.items():
        setattr(row, key, value)
    _settle_oracle(row, old_options, old_database)
    after = _connection(row)
    if after != before:
        # 连的已经不是测过的那个了：旧结果作废，表单里测过这一份的话换成那一次
        row.last_check = health.draft_result(after)
    await session.commit()
    await session.refresh(row)
    # 连接参数可能变了，旧连接池不能再用——否则改完密码还在用旧连接，很难排查
    await engines.invalidate(source_id)
    return await _out(session, row)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await _get_or_404(session, source_id)
    # 上传源的导入记录置 retired 留作审计；库文件和原件交给回收，被运行引用的版本会保留。只读进程（没拿到
    # 版本存储的守卫）不删上传源：retire_source 要删隔离文件、放弃暂存区（第 8 节遗留项 5）。
    # 手工登记的源没有导入记录、配方、暂存区和隔离文件，根本不碰版本存储：不调 retire_source，只读进程里也照常
    # 删除（9.1 只要求上传源回 503，评审意见）；删完也没有文件可回收
    upload = _is_upload(row)
    if upload:
        try:
            await table_versions.retire_source(session, source_id)
        except table_versions.StoreUnavailable as e:
            raise CodedHTTPException(503, str(e), "store_unavailable") from e
    await session.delete(row)
    await session.commit()
    await engines.invalidate(source_id)
    if upload:
        try:
            await table_versions.gc(session)
        except Exception:  # noqa: BLE001 - 回收失败不影响删除，重启或下次上传时再收
            logger.exception("删除数据源后的回收失败")


#: 测连接最多等多久。内网库防火墙丢包时驱动默认要等一分钟，弹窗里干转一分钟
#: 等于没有反馈
_TEST_TIMEOUT = 15


_WRONG_PASSWORD = ("用户名或密码错误", "请核对「用户名」和「密码」；编辑时密码留空表示沿用已保存的密码")
_CHECK_HOST = "请检查「主机」和「端口」；内网数据库请确认本机可以访问（防火墙、VPN）"


def _explain_connect(e: BaseException, kind: str) -> tuple[str, str]:
    """连库失败的常见原因。驱动的原文九成能定位问题，但得先翻译成表单上的说法。"""
    low = str(e).lower()
    if any(s in low for s in ("password authentication failed", "access denied", "ora-01017",
                              "invalid username/password", "(1045")):
        return _WRONG_PASSWORD
    if "ora-12514" in low:
        return "Oracle 无法识别该 service_name", "请核对 service_name；部分旧版数据库只提供 SID，可改用 SID 后重试"
    if "ora-12505" in low:
        return "Oracle 无法识别该 SID", "请核对 SID，或改用 service_name"
    if "service_name 或 sid" in low:
        return "未填写 service_name 或 SID", "请在「service_name」中填写，或改用 SID"
    if "unknown database" in low or "(1049" in low or (
        "does not exist" in low and "database" in low
    ):
        return "服务器上不存在该数据库", "请核对「数据库」一栏"
    if "unable to open database file" in low:
        return "无法打开该数据库文件", "请确认路径是绝对路径、文件存在，且服务端进程有读取权限"
    if "file is not a database" in low:
        return "该文件不是 SQLite 数据库", "请确认路径指向的是 .db 文件"
    if "no module named" in low:
        return "服务端未安装该数据库的驱动", "请执行 pip install 'agentlab-backend[db]'，然后重启服务端"
    # 下面按错误类别换成数据库的说法：通用的那几句说的是 API（API Key、Base URL），这里是数据库。
    # 按类别判断，不比较 explain 的文字——那句话改一个字，这里就再也对不上
    category = classify(e)
    if category == AUTH:
        return _WRONG_PASSWORD
    if category == NETWORK:
        return "网络不通，或数据库服务未启动", _CHECK_HOST
    if category == TIMEOUT:
        return "数据库长时间没有响应", _CHECK_HOST
    reason, hint = explain(e)
    if kind == "sqlite" and not hint:
        hint = "请检查数据库文件的路径"
    return reason, hint


def _safe_url(source: Any) -> str:
    try:
        return build_url(source)   # 遮掉密码的版本
    except Exception:  # noqa: BLE001 - 连串都拼不出来时，原因已经在 error 里了
        return ""


def _missing_sqlite_file(source: Any) -> tuple[str, str, str] | None:
    """SQLite 的路径指向不存在的文件时，返回 (reason, hint, detail)。

    可写模式下 SQLite 连一个不存在的路径会当场建一个空库：测试说「连得上」，
    连的却是个空文件，「只测不存」的接口还往磁盘上写了东西。没填路径时连的
    是内存库，一样是假的「连得上」。按 SQLite 自己的解析方式判断（不展开 ~）。
    """
    from pathlib import Path

    if (source.kind or "").lower() != "sqlite":
        return None
    path = source.database or ""
    if not path.strip():
        return "未填写数据库文件路径", "请在「数据库文件路径」中填写 .db 文件的绝对路径", "database 为空"
    if path == ":memory:" or Path(path).is_file():
        return None
    return ("无法打开该数据库文件",
            "该路径下没有文件。请确认路径是绝对路径且文件存在（~ 不会被展开）",
            f"文件不存在：{path}")


async def _probe(source: Any, *, cached: bool) -> dict[str, Any]:
    """真连一次。cached=False 用一次性的 engine，测完就扔——草稿不该进连接池缓存，
    否则改完配置再测，拿到的还是上一版的连接。"""
    import asyncio
    import time

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    started = time.perf_counter()
    missing = _missing_sqlite_file(source)
    if missing:
        reason, hint, detail = missing
        return {"ok": False, "error": f"连接失败：{reason}", "hint": hint, "detail": detail,
                "elapsed_ms": 0, "url": _safe_url(source)}
    engine = None
    try:
        if cached:
            engine = await engines.get(source)
        else:
            url, extra = engine_args(source)
            engine = create_async_engine(url, poolclass=NullPool, **extra)
        async with asyncio.timeout(_TEST_TIMEOUT):
            async with engine.connect() as conn:
                oracle = (source.kind or "").lower() == "oracle"
                await conn.execute(text("SELECT 1 FROM DUAL" if oracle else "SELECT 1"))
        return {
            "ok": True,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "url": _safe_url(source),
        }
    except SnapshotTampered as e:
        # 快照文件和登记的哈希对不上：原话就是原因，不翻译成泛泛的连接错误
        return {"ok": False, "error": str(e), "hint": "请重新上传这份表格", "detail": "",
                "elapsed_ms": int((time.perf_counter() - started) * 1000), "url": _safe_url(source)}
    except Exception as e:  # noqa: BLE001
        reason, hint = _explain_connect(e, (source.kind or "").lower())
        return {
            "ok": False,
            "error": f"连接失败：{reason}",
            "hint": hint,
            "detail": raw(e),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "url": _safe_url(source),
        }
    finally:
        if engine is not None and not cached:
            await engine.dispose()


class DataSourceTestIn(BaseModel):
    """一份还没保存（或改了还没保存）的配置。带 id 表示在编辑已有的那个。"""

    id: str | None = None
    name: str = ""
    kind: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None  # 编辑时留空=沿用已保存的
    options: dict[str, Any] = Field(default_factory=dict)
    readonly: bool = True


def _draft_source(payload: DataSourceTestIn, saved: DataSource | None) -> DataSource:
    """拼一个不进会话的临时数据源，只拿来连一下。

    编辑时密码框留空表示不改——测连接也得用存着的那个，否则改个端口想测一下，
    只会得到一句"密码不对"。
    """
    if payload.password:
        password = encrypt(payload.password)
    else:
        password = saved.password if saved else None
    return DataSource(
        name=payload.name or (saved.name if saved else "draft"),
        kind=payload.kind, host=payload.host, port=payload.port, database=payload.database,
        username=payload.username, password=password, options=dict(payload.options or {}),
        readonly=payload.readonly,
    )


@router.post("/test")
async def test_draft(
    payload: DataSourceTestIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """测一份没保存的配置，不落库。

    以前只能测已存在的数据源：填错一项要保存→关弹窗→点测试→看提示→再打开改，
    接内网 Oracle 这类填错一个字段就连不上的东西，来回四五趟。
    """
    if payload.kind not in SUPPORTED_KINDS:
        return {"ok": False, "error": f"不支持「{payload.kind}」类型的数据库",
                "hint": f"目前支持：{'、'.join(SUPPORTED_KINDS)}", "detail": "", "elapsed_ms": 0, "url": ""}
    saved = await session.get(DataSource, payload.id) if payload.id else None
    if saved is not None and _is_upload(saved):
        # 上传源：连接字段由系统维护。改了就和保存一样拒；没改，测的就是它当前版本的快照，
        # 不拿表单里的路径去连——否则带上一个上传源的 id，就能把上传目录里任何文件拿来试
        changed = _locked_changes(saved, payload.model_dump(exclude_unset=True, include=set(_UPLOAD_LOCKED)))
        if changed:
            return _refused(_locked_refusal(changed))
        return await _test_saved(session, saved)
    reason = _reserved_reason(payload.kind, payload.database)
    if reason:
        # 保存时会被拒（_refuse_reserved）：测连接不能先报「连接成功」，到保存才说不行
        return _refused(reason)
    draft = _draft_source(payload, saved)
    result = await _probe(draft, cached=False)
    last, key = _probe_record(result), _connection(draft)
    health.remember_draft(key, last)
    if saved is not None and key is not None and key == _connection(saved):
        # 编辑框里没改连接、只是再测一次：测的就是已保存的那份
        saved.last_check = last
        await session.commit()
    return result


@router.post("/{source_id}/test")
async def test_source(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """测连接。失败时说清原因和怎么办，驱动的原始报错放在 detail——这类问题九成靠它定位。"""
    return await _test_saved(session, await _get_or_404(session, source_id))


def _refused(reason: str) -> dict[str, Any]:
    """不去连就能判定失败的测连接结果（形状和 _probe 的一样）。"""
    return {"ok": False, "error": reason, "hint": "", "detail": "", "elapsed_ms": 0, "url": ""}


async def _test_saved(session: AsyncSession, row: DataSource) -> dict[str, Any]:
    """测一个已保存的数据源，结果记进 last_check（提交）。

    上传源测的是它当前版本的快照：只读、immutable、建引擎前核对哈希，和查询走同一条路。
    拿数据源行本身去连，既不核对哈希（快照被改过也报「连接成功」），还会在连接缓存里留下
    一个不带版本、不核对的引擎。手工源指向应用自己的数据文件（升级前登记的）时不去连。
    """
    if _is_upload(row):
        try:
            view = await table_versions.resolve_source(session, row)
        except table_versions.SnapshotMissing as e:
            result = _refused(str(e))
        else:
            result = await _probe(view, cached=True)
    elif _in_app_files(row.kind, row.database):
        result = _refused(_APP_FILES_REFUSED)
    else:
        result = await _probe(row, cached=True)
    row.last_check = _probe_record(result)
    await session.commit()
    return result


class IntrospectPreview(BaseModel):
    """只看不存的探查结果。缓存、同步时间都没动。"""

    dry_run: bool = True
    #: 这次探的是哪个 schema，"" 是默认 schema
    schema_: str = Field(default="", serialization_alias="schema")
    table_count: int = 0
    #: 带 schema 前缀的对象全名，最多 200 个（和真探查的上限一样）
    tables: list[str] = Field(default_factory=list)
    truncated: bool = False
    total: int = 0
    schema_error: str = ""
    available_schemas: list[str] = Field(default_factory=list)


@router.post("/{source_id}/introspect", response_model=DataSourceOut | IntrospectPreview)
async def introspect_source(
    source_id: str,
    schema: str | None = None,
    dry_run: bool = Query(default=False, description="只返回探到的结构，不写缓存"),
    session: AsyncSession = Depends(get_session),
) -> DataSourceOut | IntrospectPreview:
    """探查结构并缓存。显式动作，不做后台轮询——生产库不该被实验工具定时扫。

    dry_run=true 只看不存：「换个 schema 看看」以前一探就把缓存换掉，用户随后
    选了「不改」，助手此刻看到的也已经是另一套表了。

    上传的表格不探查：结构在发布那一刻随快照冻结，重新探查只会让缓存和快照对不上。
    """
    row = await _get_or_404(session, source_id)
    if _is_upload(row):
        raise HTTPException(409, "上传的表格无需重新探查，重新上传即可更新")
    try:
        cache = await introspect_mod.introspect(row, schema=schema)
    except Exception as e:  # noqa: BLE001
        reason, hint = _explain_connect(e, (row.kind or "").lower())
        raise HTTPException(400, f"探查表结构失败：{reason}" + (f"。{hint}" if hint else "")) from e
    if dry_run:
        tables = cache.get("tables") or {}
        return IntrospectPreview(
            schema_=str(cache.get("schema") or ""),
            table_count=len(tables),
            tables=[m.get("qualified", n) for n, m in tables.items()],
            truncated=bool(cache.get("truncated")),
            total=int(cache.get("total") or 0),
            schema_error=str(cache.get("error") or "") if cache.get("failed") else "",
            available_schemas=list(cache.get("available_schemas") or []),
        )
    row.schema_cache = cache
    # 失败也要把缓存存下来——里面的 available_schemas 正是下一步的线索——
    # 但**不能盖上"已同步"的戳**。以前盖了，于是界面显示同步过、工具却说
    # "还没探查过"，两边都不说真话（run 554a0f92）
    if not row.schema_cache.get("failed"):
        row.schema_synced_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.get("/{source_id}/schema")
async def get_schema(source_id: str, table: str | None = None,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """表结构。上传的表格返回当前快照里冻结的那份——和查询实际用的库是同一个版本。"""
    found = await _get_or_404(session, source_id)
    try:
        row = await table_versions.resolve_source(session, found)
    except table_versions.SnapshotMissing as e:
        raise HTTPException(409, str(e)) from e
    if table:
        meta = introspect_mod.find_table(row, table)
        # 和 db_schema 工具同一个说法：有数据目录的表，字段清单后面附上目录
        entries = await data_catalog.read_catalog(session, found.id)
        return {
            "table": table,
            # 给模型看的那段文本照旧；界面画表格用下面的结构化字段
            "detail": introspect_mod.describe_table(row, table,
                                                    catalog={n: e.notes for n, e in entries.items()}),
            "found": meta is not None,
            "qualified": meta.get("qualified", table) if meta else None,
            "kind": ("view" if meta.get("is_view") else "table") if meta else None,
            "comment": meta.get("comment") if meta else None,
            "columns": introspect_mod.table_columns(meta) if meta else [],
        }
    return {
        "tables": introspect_mod.table_names(row),
        "summary": introspect_mod.summary(row),
        "synced_at": row.schema_synced_at.isoformat() if row.schema_synced_at else None,
    }


# --------------------------------------------------------------------------
# 上传表格
# --------------------------------------------------------------------------

#: 「数字列混入非数字」的两种处理：reject 拒收并请用户选择；null 非数字的值存空值、列按数字存
_MIXED_CHOICES = ("reject", "null")


def _decision(e: Exception) -> dict[str, Any] | None:
    """解析器要用户先做决定（tabular.NeedsDecision：交叉表、数字列混入非数字）时，取出决定的内容。

    NeedsDecision 是 UnsupportedTable 的子类，多带 kind 和 details 两个属性；普通的
    UnsupportedTable 没有。按属性认，而不是按类名：接口层只关心「要不要请用户选」。
    """
    kind, details = getattr(e, "kind", None), getattr(e, "details", None)
    if not kind or not isinstance(details, dict):
        return None
    return {"kind": kind, "details": jsonable_encoder(details)}


def _table_out(t: dict[str, Any]) -> dict[str, Any]:
    """回执里的一张表。原表头和列名并排回显：表头行取错了（比如文件前两行是标题），当场看得出来。"""
    return {
        "name": t.get("name"), "sheet": t.get("sheet"), "rows": t.get("rows", 0),
        "columns": [
            {"name": c.get("name"), "type": c.get("type"), "header": c.get("header")}
            for c in t.get("columns") or [] if isinstance(c, dict)
        ],
        "region": t.get("region"),
        "unshaped": bool(t.get("unshaped")),
        "blank_rows_skipped": t.get("blank_rows_skipped", 0),
        # 原样转交解析器给的值，不替它改类型
        "columns_trimmed": t.get("columns_trimmed", []),
    }


async def read_upload(file: UploadFile) -> bytes:
    """读上传的文件，边读边数，超了立刻停（413 file_too_large）。简单上传和按配方导入共用。

    和知识库上传同一套：那个检查拦的是「入库」，不是「占内存」，读完再判等于先把内存吃掉。
    """
    limit = settings.max_upload_mb * 1024 * 1024
    pieces: list[bytes] = []
    total = 0
    while piece := await file.read(1024 * 1024):
        total += len(piece)
        if total > limit:
            raise CodedHTTPException(413, f"文件超过 {settings.max_upload_mb} MB", "file_too_large")
        pieces.append(piece)
    return b"".join(pieces)


@router.post("/upload", response_model=dict, status_code=201)
async def upload_table(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    header_row: int = Form(1),
    mixed: str = Form("reject"),
    raw_mode: bool = Form(False),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """把 Excel / CSV 变成一个可以用 SQL 查的数据源。

    落成一个 SQLite 库，数据源 kind='sqlite' 指过去。下游一行都不用改——SQL 守卫、
    结构探查、db_query__<name> 工具、查询快照进工件库、助手的数据源清单，全都白拿。

    这条路存在的理由不是"多支持一种格式"：表格传进知识库只能被切块检索，
    数字就成了模型从片段里读出来的，而这个项目的地基是"所有算术下沉到
    SQL 或口径卡"。表格必须变成表。

    **同名就地更新**，不新建。数据源名字会成为工具名（db_query__sales），
    而工具名写进了保存过的工作流——重传时另起一个 sales_2 等于悄悄让那些图失效。
    每次上传发布成一个新版本（data/table_versions.py）：写新文件、最后切指针，
    失败时旧版本完整可查；原件按哈希存档，不提供下载。

    解析器需要用户先做决定时（交叉表 / 多块结构、数字列混入非数字）回 422，body 里的
    decision 说明要选什么；mixed、raw_mode 是用户选完再传回来的答案。
    """
    import re as _re

    from app.data.tabular import UnsupportedTable

    if not _re.match(_NAME_PATTERN, name or ""):
        raise HTTPException(
            400, "名称必须以小写字母开头，只能包含字母、数字和下划线（名称会成为工具名的一部分）"
        )
    if mixed not in _MIXED_CHOICES:
        raise HTTPException(400, "数字列混入非数字时，只能选择拒收或将非数字的值存为空值")

    raw = await read_upload(file)

    existing = (await session.execute(
        select(DataSource).where(DataSource.name == name)
    )).scalar_one_or_none()
    # 按配方导入的源：重传要按已确认的配方重放（上传新一期），不能退回简单解析把配方和核对绕过去
    if existing is not None and existing.current_recipe_id:
        raise CodedHTTPException(409, f"「{name}」按配方导入，请使用「上传新一期」", "recipe_source")
    # 同名的只有上传表格可以更新。迁移前的老上传（路径还在上传目录里）也算
    if existing is not None and not _is_upload(existing) and not (
        existing.kind == "sqlite" and table_versions.under_uploads(existing.database)
    ):
        raise HTTPException(
            409, f"已存在名为「{name}」的数据源（非上传表格），请换一个名称"
        )

    row = existing
    if row is None:
        # 先不进会话：发布失败时这个源不该被建出来，由 publish_upload 在提交前加入
        row = DataSource(id=new_id(), name=name, kind="sqlite", readonly=True,
                         description=description, origin="upload")
    elif description:
        row.description = description

    try:
        result = await table_versions.publish_upload(
            session, row, raw, file.filename or name,
            header_row=header_row, mixed=mixed, raw_mode=raw_mode,
        )
    except UnsupportedTable as e:
        decision = _decision(e)
        if decision is not None:
            return JSONResponse(status_code=422, content={"detail": str(e), "decision": decision})
        raise HTTPException(400, str(e)) from e
    except table_versions.PublishError as e:
        logger.error("上传表格「%s」发布失败：%s", name, e)
        if e.code == "store_unavailable":
            raise CodedHTTPException(503, str(e), "store_unavailable") from e
        if e.code == "build_conflict":
            # 服务端已有的同版本文件被改过、这次上传也恢复不了：重试也会失败，要管理员处理（第 8 节遗留项 1）
            raise CodedHTTPException(409, str(e), "build_conflict") from e
        raise HTTPException(500, str(e)) from e

    await session.refresh(row)
    report = result.report
    return {
        "source": (await _out(session, row)).model_dump(),
        "replaced": existing is not None,
        "import_id": result.import_id,
        "snapshot_id": result.snapshot_id,
        "build_reused": result.build_reused,
        # 服务端的同版本数据文件曾被改动，已用本次上传的文件恢复（原文件挪进了隔离区）
        "build_restored": result.build_restored,
        "skipped_sheets": report.get("skipped_sheets") or [],
        "conversions": report.get("conversions") or [],
        "warnings": report.get("warnings") or [],
        "tables": [_table_out(t) for t in report.get("tables") or [] if isinstance(t, dict)],
    }


@router.get("/{source_id}/imports")
async def list_imports(source_id: str, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """上传表格的导入记录，新的在前。手工登记的源没有导入记录，返回空列表。

    期 3（P3-SPEC 7.6）补了接受明细、作废记录、导入清单、配方第几版、各表行数、作废接受的预案（revoke_plan），
    以及同一份原件在别处的引用（raw_shared_with、raw_open_stagings）：清除对话框在提交前就要列出来。"""
    from app.data import source_versions

    row = await _get_or_404(session, source_id)
    rows = list((await session.execute(
        select(TableImport).where(TableImport.source_id == source_id).order_by(TableImport.seq.desc())
    )).scalars())
    return await source_versions.import_records(session, row, rows)


#: 清除原件的理由、署名的字数上限
_PURGE_REASON_MAX = 500
_SIGNED_MAX = 100


@router.post("/{source_id}/imports/{import_id}/purge-raw")
async def purge_raw(
    source_id: str, import_id: str, request: Request, x_actor: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """清除一次导入的原件。导入记录、哈希和库都保留，原件文件删掉，记下时间、署名和理由。

    按内容清除：同一份内容可能被别的导入共用（同一个文件传过两次、传给过两个源），清除的
    目的是让这份内容从服务器上消失，所以文件照删，那些导入一并标成已清除，回执的
    also_purged 逐条列出（期 3 的界面要把它展示出来，不能静默带过）。

    期 3：请求体手工读成 dict（评审二-m8：原来用 pydantic，空理由由 FastAPI 自动回 422，响应体不是 {detail, code}），
    错误一律带机读码。confirm 键收但不强求（7.7：期 1、期 2 的调用方不带它）；带了而不是 true 时拒绝。
    """
    from app.data import source_versions

    body = await _json_body(request)
    row = await _get_or_404(session, source_id)
    imp = await session.get(TableImport, import_id)
    if imp is None or imp.source_id != source_id:
        raise CodedHTTPException(404, "导入记录不存在", "import_not_found")
    if "confirm" in body and body.get("confirm") is not True:
        raise CodedHTTPException(422, "请在确认框中确认后再清除原件", "confirm_required")
    raw_reason = body.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        raise CodedHTTPException(422, "请填写清除原件的理由", "reason_required")
    if len(reason) > _PURGE_REASON_MAX:
        raise CodedHTTPException(422, f"理由不能超过 {_PURGE_REASON_MAX} 字", "reason_required")
    signed = body.get("signed_by")
    signed_by = signed.strip()[:_SIGNED_MAX] if isinstance(signed, str) and signed.strip() else actor_of(x_actor)
    if imp.raw_state == "absent":
        raise CodedHTTPException(409, "这次导入没有保存原件", "raw_absent")
    if imp.raw_state == "purged":
        raise CodedHTTPException(409, "这次导入的原件已经清除", "raw_already_purged")
    try:
        purged = await table_versions.purge_import_raw(session, imp, reason=reason, signed_by=signed_by)
    except table_versions.StoreUnavailable as e:
        raise CodedHTTPException(503, str(e), "store_unavailable") from e
    deleted, others = purged.deleted, purged.others
    names = dict((await session.execute(
        select(DataSource.id, DataSource.name).where(DataSource.id.in_({o.source_id for o in others}))
    )).all()) if others else {}
    [record] = await source_versions.import_records(session, row, [imp])
    return {
        "import": record,
        "file_deleted": deleted,
        "also_purged": [
            {"id": o.id, "source_id": o.source_id, "source_name": names.get(o.source_id),
             "seq": o.seq, "file_name": o.file_name}
            for o in others
        ],
        # 引用同一份原件、随之一并放弃的未完成导入（暂存区，已清空）
        "discarded_stagings": [st.id for st in purged.discarded_stagings],
    }


async def _json_body(request: Request) -> dict[str, Any]:
    """JSON 请求体手工读成 dict（空 body 当作 {}）；不是 JSON 对象回 422 body_invalid（不触发 FastAPI 自带的 422）。"""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CodedHTTPException(422, "请求体必须是一个 JSON 对象", "body_invalid")
    return data
