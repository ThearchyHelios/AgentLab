"""数据源的增删改查、连接测试、结构探查。

密码的处理规则和 Provider 完全一致（app/api/settings.py）：入库加密、
出站只给掩码、PATCH 时不传表示不动、传空串表示清空。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import explain, first_line, raw
from app.core.config import settings
from app.core.crypto import encrypt, mask
from app.data import introspect as introspect_mod
from app.data.engine import SUPPORTED_KINDS, build_url, engine_args, engines
from app.db.base import get_session
from app.db.models import DataSource
from app.tools.datasource import tool_names

router = APIRouter(prefix="/api/datasources", tags=["datasources"])

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


def _to_out(row: DataSource) -> DataSourceOut:
    cache = row.schema_cache or {}
    tables = cache.get("tables") or {}
    return DataSourceOut(
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
    )


async def _get_or_404(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if not row:
        raise HTTPException(404, "这个数据源不存在，可能已经被删了")
    return row


@router.get("", response_model=list[DataSourceOut])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[DataSourceOut]:
    rows = (await session.execute(select(DataSource).order_by(DataSource.name))).scalars()
    return [_to_out(r) for r in rows]


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
             "hint": "字符集默认 utf8mb4，要换就在「高级连接参数」里加 charset",
             "advanced": [{"key": "charset", "label": "字符集", "placeholder": "utf8mb4"}]},
            {"value": "postgres", "label": "PostgreSQL", "default_port": 5432,
             "needs": ["host", "database", "username", "password"],
             "hint": "「schema」留空就用 public",
             "advanced": []},
            {"value": "oracle", "label": "Oracle", "default_port": 1521,
             "needs": ["host", "username", "password"],
             "hint": "service_name 和 SID 二选一：一般填 service_name，老库只给了 SID 就切到 SID。"
                     "驱动走 thin 模式，不需要装 Instant Client。"
                     "只读账号名下通常没有对象——数据在别的 schema 里，"
                     "把它填进「schema」（如 ANALYTICS），否则探查结果是空的",
             "advanced": [
                 {"key": "service_name", "label": "service_name", "placeholder": "ORCLPDB1",
                  "help": "和 SID 二选一"},
                 {"key": "sid", "label": "SID", "placeholder": "ORCL", "help": "和 service_name 二选一"},
             ]},
            {"value": "sqlite", "label": "SQLite（文件）", "default_port": None,
             "needs": ["database"], "hint": "「数据库文件路径」填 .db 文件的绝对路径",
             "advanced": []},
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
        raise HTTPException(400, f"不支持「{payload.kind}」这种数据库，支持：{'、'.join(SUPPORTED_KINDS)}")
    exists = (await session.execute(
        select(DataSource).where(DataSource.name == payload.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"已经有叫「{payload.name}」的数据源了，换个标识")

    row = DataSource(
        **payload.model_dump(exclude={"password"}),
        password=encrypt(payload.password),
    )
    _settle_oracle(row, None, None)
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
    if "password" in data:
        # 空串表示清空，非空表示换新的
        data["password"] = encrypt(data["password"]) if data["password"] else None
    old_options, old_database = dict(row.options or {}), row.database
    for key, value in data.items():
        setattr(row, key, value)
    _settle_oracle(row, old_options, old_database)
    await session.commit()
    await session.refresh(row)
    # 连接参数可能变了，旧连接池不能再用——否则改完密码还在用旧连接，很难排查
    await engines.invalidate(source_id)
    return _to_out(row)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await _get_or_404(session, source_id)
    await session.delete(row)
    await session.commit()
    await engines.invalidate(source_id)


#: 测连接最多等多久。内网库防火墙丢包时驱动默认要等一分钟，弹窗里干转一分钟
#: 等于没有反馈
_TEST_TIMEOUT = 15


def _explain_connect(e: BaseException, kind: str) -> tuple[str, str]:
    """连库失败的常见原因。驱动的原文九成能定位问题，但得先翻译成表单上的说法。"""
    low = str(e).lower()
    if any(s in low for s in ("password authentication failed", "access denied", "ora-01017",
                              "invalid username/password", "(1045")):
        return "账号或密码不对", "核对「用户名」和「密码」；编辑时密码留空表示沿用已保存的那个"
    if "ora-12514" in low:
        return "Oracle 不认识这个 service_name", "核对 service_name；老库可能只给了 SID，切到 SID 再试"
    if "ora-12505" in low:
        return "Oracle 不认识这个 SID", "核对 SID，或者改用 service_name"
    if "service_name 或 sid" in low:
        return "没填 service_name 或 SID", "在「service_name」一栏填上，或者切到 SID"
    if "unknown database" in low or "(1049" in low or (
        "does not exist" in low and "database" in low
    ):
        return "服务器上没有这个库", "核对「数据库」一栏"
    if "unable to open database file" in low:
        return "打不开这个数据库文件", "核对路径是不是绝对路径、文件在不在，以及后端进程有没有读权限"
    if "file is not a database" in low:
        return "这个文件不是 SQLite 数据库", "核对路径指的是不是 .db 文件"
    if "no module named" in low:
        return "后端没装这种数据库的驱动", "pip install 'agentlab-backend[db]' 之后重启后端"
    reason, hint = explain(e)
    if reason in ("等了太久没有响应", "连不上对方的服务"):
        # 通用那句说的是 API，这里是数据库：多半是主机、端口或者网络不通
        hint = "核对「主机」和「端口」；内网库要确认这台机器访问得到它（防火墙、VPN）"
    elif kind == "sqlite" and not hint:
        hint = "核对数据库文件的路径"
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
        return "没填数据库文件路径", "在「数据库文件路径」里填 .db 文件的绝对路径", "database 为空"
    if path == ":memory:" or Path(path).is_file():
        return None
    return ("打不开这个数据库文件",
            "这个路径上没有文件。核对路径是不是绝对路径、文件在不在（~ 不会被展开）",
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
        return {"ok": False, "error": f"连不上：{reason}", "hint": hint, "detail": detail,
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
    except Exception as e:  # noqa: BLE001
        reason, hint = _explain_connect(e, (source.kind or "").lower())
        return {
            "ok": False,
            "error": f"连不上：{reason}",
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
        return {"ok": False, "error": f"不支持「{payload.kind}」这种数据库",
                "hint": f"支持：{'、'.join(SUPPORTED_KINDS)}", "detail": "", "elapsed_ms": 0, "url": ""}
    saved = await session.get(DataSource, payload.id) if payload.id else None
    return await _probe(_draft_source(payload, saved), cached=False)


@router.post("/{source_id}/test")
async def test_source(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """测连接。失败时说清原因和怎么办，驱动的原始报错放在 detail——这类问题九成靠它定位。"""
    row = await _get_or_404(session, source_id)
    return await _probe(row, cached=True)


@router.post("/{source_id}/introspect", response_model=DataSourceOut)
async def introspect_source(
    source_id: str, schema: str | None = None, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    """探查结构并缓存。显式动作，不做后台轮询——生产库不该被实验工具定时扫。"""
    row = await _get_or_404(session, source_id)
    try:
        row.schema_cache = await introspect_mod.introspect(row, schema=schema)
    except Exception as e:  # noqa: BLE001
        reason, hint = _explain_connect(e, (row.kind or "").lower())
        raise HTTPException(400, f"探查表结构失败：{reason}" + (f"。{hint}" if hint else "")) from e
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
    row = await _get_or_404(session, source_id)
    if table:
        return {"table": table, "detail": introspect_mod.describe_table(row, table)}
    return {
        "tables": introspect_mod.table_names(row),
        "summary": introspect_mod.summary(row),
        "synced_at": row.schema_synced_at.isoformat() if row.schema_synced_at else None,
    }


# --------------------------------------------------------------------------
# 上传表格
# --------------------------------------------------------------------------


@router.post("/upload", response_model=dict, status_code=201)
async def upload_table(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    header_row: int = Form(1),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把 Excel / CSV 变成一个可以用 SQL 查的数据源。

    走 SQLite：落成一个 .db 文件，再建一条 kind='sqlite' 的数据源指过去。
    下游一行都不用改——SQL 守卫、结构探查、db_query__<name> 工具、查询快照进
    工件库、Copilot 的数据源清单，全都白拿。

    这条路存在的理由不是"多支持一种格式"：表格传进知识库只能被切块检索，
    数字就成了模型从片段里读出来的，而这个项目的地基是"所有算术下沉到
    SQL 或口径卡"。表格必须变成表。

    **同名就地替换**，不新建。数据源名字会成为工具名（db_query__sales），
    而工具名写进了保存过的工作流——重传时另起一个 sales_2 等于悄悄让那些图失效。
    """
    import re as _re

    if not _re.match(_NAME_PATTERN, name or ""):
        raise HTTPException(
            400, "名字只能用小写字母开头的字母数字下划线——它会成为工具名的一部分"
        )

    # 边读边数，超了立刻停。和知识库上传同一套：那个检查拦的是"入库"，
    # 不是"占内存"，读完再判等于先把内存吃掉
    limit = settings.max_upload_mb * 1024 * 1024
    pieces: list[bytes] = []
    total = 0
    while piece := await file.read(1024 * 1024):
        total += len(piece)
        if total > limit:
            raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB")
        pieces.append(piece)
    raw = b"".join(pieces)

    tables_dir = settings.uploads_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    db_path = tables_dir / f"{name}.db"

    from app.data.tabular import UnsupportedTable, load_into

    existing = (await session.execute(
        select(DataSource).where(DataSource.name == name)
    )).scalar_one_or_none()
    if existing and existing.kind != "sqlite":
        raise HTTPException(
            409, f"已经有一个叫 {name} 的数据源了，而且它不是上传来的表格。换个名字。"
        )

    try:
        report = load_into(str(db_path), raw, file.filename or name, header_row=header_row)
    except UnsupportedTable as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            400, f"导入失败：{first_line(e) if isinstance(e, ValueError) else explain(e)[0]}。"
                 "确认文件是 Excel 或 CSV、没有加密，表头行号填得对",
        ) from e

    row = existing
    if row is None:
        row = DataSource(
            name=name, kind="sqlite", database=str(db_path),
            readonly=True, description=description,
        )
        session.add(row)
    else:
        row.database = str(db_path)
        if description:
            row.description = description
        # 换了文件内容，缓存的连接还指着旧的那个 engine
        await engines.invalidate(row.id)

    await session.flush()
    # 建完立刻探查：不探的话 db_query 工具的 description 里没有表清单，
    # 模型得先花一步去问"有哪些表"
    try:
        row.schema_cache = await introspect_mod.introspect(row)
        row.schema_synced_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001 - 探查失败不该让导入白做，用户可以手动再探
        pass
    await session.commit()
    await session.refresh(row)

    return {
        "source": _to_out(row).model_dump(),
        "replaced": existing is not None,
        # 推断出的列名和类型原样回显：表头行取错了（比如文件前两行是标题），
        # 这里当场就能看出来——列名会变成一行数据
        "tables": [
            {
                "name": t.name, "sheet": t.source_name, "rows": t.rows,
                "columns": [
                    {"name": c, "type": ty} for c, ty in zip(t.columns, t.types)
                ],
            }
            for t in report.tables
        ],
    }
