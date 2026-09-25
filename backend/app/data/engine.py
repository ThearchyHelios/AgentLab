"""数据源连接层。各家数据库的方言差异全部收敛在这个文件里。

上层（工具、探查、API）只认 DataSource 这个模型，不需要知道 Oracle 要 service_name、
MySQL 要 charset、asyncpg 不认 sslmode 这些事。哪天要加一种库，改这里一处。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy import text

from app.core.crypto import decrypt
from app.data.guard import QueryLimits, SqlRejected, check, is_write

# 各方言的 SQLAlchemy 驱动。都选 asyncio 原生驱动，没有一个需要装数据库客户端：
# oracledb 走 thin 模式（纯 Python 协议实现），省掉了 Oracle Instant Client 这个
# 历来最劝退的一步。
_DRIVERS = {
    "mysql": "mysql+aiomysql",
    "mariadb": "mysql+aiomysql",
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "oracle": "oracle+oracledb",
    "sqlite": "sqlite+aiosqlite",
}

_DEFAULT_PORTS = {
    "mysql": 3306, "mariadb": 3306,
    "postgres": 5432, "postgresql": 5432,
    "oracle": 1521,
}

SUPPORTED_KINDS = tuple(sorted(set(_DRIVERS)))

# options 里这些 key 是 AgentLab 自己的配置，不是驱动参数，拼 URL 时要摘掉。
# schema：探查哪个 schema（企业库里只读账号名下常常什么都没有，数据在别处）
_NON_DRIVER_OPTIONS = frozenset({"schema"})


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    sql: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "sql": self.sql,
        }


def build_url(source: Any, *, reveal: bool = False) -> str:
    """拼连接串。reveal=False 时把密码换成星号——日志和错误信息里用这个。

    密码永远不该出现在日志、事件流、或者返回给前端的任何地方。默认遮掉，
    真要连库时才显式 reveal=True。
    """
    kind = (source.kind or "").lower()
    driver = _DRIVERS.get(kind)
    if not driver:
        raise ValueError(f"不支持的数据库类型：{source.kind}（支持 {', '.join(SUPPORTED_KINDS)}）")

    options: dict[str, Any] = dict(source.options or {})
    # options 混着两类东西：驱动参数（charset、service_name…）和我们自己的配置
    # （schema 指探查哪个 schema）。后者不能拼进连接串，否则驱动会把它当成
    # 未知的连接参数直接拒掉。
    for key in _NON_DRIVER_OPTIONS:
        options.pop(key, None)

    if kind == "sqlite":
        # SQLite 没有主机和账号，database 就是文件路径
        return f"{driver}:///{source.database or ':memory:'}"

    password = decrypt(source.password) or "" if reveal else "***"
    from urllib.parse import quote_plus

    user = quote_plus(source.username or "")
    pw = quote_plus(password) if reveal else password
    auth = f"{user}:{pw}@" if user else ""
    port = source.port or _DEFAULT_PORTS.get(kind)
    host = f"{source.host or 'localhost'}:{port}" if port else (source.host or "localhost")

    if kind == "oracle":
        # Oracle 用 service_name 或 sid，不是"数据库名"。这是最常见的配错点，
        # 所以两种都认，并且在都没填时给出明确提示而不是让驱动报晦涩的 ORA 错误。
        service = options.pop("service_name", None) or source.database
        sid = options.pop("sid", None)
        if not service and not sid:
            raise ValueError("Oracle 需要在 options 里给 service_name 或 sid（也可以填在库名里）")
        tail = f"?service_name={service}" if service else f"?sid={sid}"
        query = "&".join(f"{k}={v}" for k, v in options.items())
        return f"{driver}://{auth}{host}/{tail}{('&' + query) if query else ''}"

    query = "&".join(f"{k}={v}" for k, v in options.items())
    return f"{driver}://{auth}{host}/{source.database or ''}{('?' + query) if query else ''}"


def engine_args(source: Any) -> tuple[str, dict[str, Any]]:
    """真正连库用的连接串和额外参数。只读源在这里拿到**连接层**的只读。

    守卫按关键字判定，而模型能写出什么 SQL 事前无法穷举——`WITH … DELETE`
    就曾从首关键字白名单下面钻过去，在 SQLite 上真把数据删了（pysqlite 只在
    INSERT/UPDATE/DELETE 开头的语句前隐式开事务，WITH 开头的直接自动提交）。
    所以只读不能只靠猜，连接本身就得写不进去：

    - SQLite：以 mode=ro 打开文件，写操作由 SQLite 自己拒绝。
    - PostgreSQL：会话的默认事务只读。能关掉它的 SET / set_config() 守卫都拦。
    - MySQL / MariaDB：会话事务只读。能关掉它的只有 SET，守卫拦。
    - Oracle：没有会话级只读开关，只剩守卫这一道——只读数据源请配只读账号。
    """
    url = build_url(source, reveal=True)
    if not source.readonly:
        return url, {}
    kind = (source.kind or "").lower()
    if kind == "sqlite":
        path = source.database or ""
        if path and path != ":memory:":
            from urllib.parse import quote
            url = f"{_DRIVERS['sqlite']}:///file:{quote(path)}?mode=ro&uri=true"
        return url, {}
    if kind in ("postgres", "postgresql"):
        return url, {"connect_args": {"server_settings": {"default_transaction_read_only": "on"}}}
    if kind in ("mysql", "mariadb"):
        return url, {"connect_args": {"init_command": "SET SESSION TRANSACTION READ ONLY"}}
    return url, {}


class EngineCache:
    """按数据源 id 缓存 engine。连接池的建立不便宜，不该每次查询都重来一遍。

    配置变了要显式 invalidate——改了密码却还在用旧连接，排查起来很费神；
    改了只读开关却还在用旧连接，只读就形同虚设。
    """

    def __init__(self) -> None:
        self._engines: dict[str, AsyncEngine] = {}
        self._lock = asyncio.Lock()

    async def get(self, source: Any) -> AsyncEngine:
        key = str(source.id)
        cached = self._engines.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            if key in self._engines:
                return self._engines[key]
            url, extra = engine_args(source)
            engine = create_async_engine(
                url,
                pool_size=3,
                max_overflow=2,
                pool_pre_ping=True,   # 长时间空闲后连接会被数据库掐掉，先探活再用
                pool_recycle=1800,
                **extra,
            )
            self._engines[key] = engine
            return engine

    async def invalidate(self, source_id: str) -> None:
        engine = self._engines.pop(str(source_id), None)
        if engine is not None:
            await engine.dispose()

    async def close(self) -> None:
        for engine in list(self._engines.values()):
            await engine.dispose()
        self._engines.clear()


engines = EngineCache()


async def test_connection(source: Any) -> dict[str, Any]:
    """测连接。失败时把驱动的原始错误带出来——这类问题九成靠错误信息定位。"""
    import time

    started = time.perf_counter()
    try:
        engine = await engines.get(source)
        async with engine.connect() as conn:
            probe = "SELECT 1 FROM DUAL" if (source.kind or "").lower() == "oracle" else "SELECT 1"
            await conn.execute(text(probe))
        return {
            "ok": True,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "url": build_url(source),  # 遮掉密码的版本
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "url": build_url(source),
        }


async def run_query(
    source: Any, sql: str, *, limits: QueryLimits | None = None
) -> QueryResult:
    """执行一条查询。守卫先过，再谈执行。

    注意执行的是 guard.check 的返回值而不是原始输入——守卫会把尾分号之类
    规范掉，执行原文等于绕过了守卫。
    """
    import time

    limits = limits or QueryLimits()
    statement = check(sql, readonly=bool(source.readonly), source_name=source.name)

    engine = await engines.get(source)
    started = time.perf_counter()

    if not source.readonly and is_write(statement):
        return await _run_write(engine, statement, limits, started)

    async def _exec() -> tuple[list[str], list[list[Any]], bool]:
        async with engine.connect() as conn:
            cursor = await conn.stream(text(statement))
            columns = list(cursor.keys())
            rows: list[list[Any]] = []
            truncated = False
            size = 0
            async for row in cursor:
                # 逐行累加，边收边判上限：一次性 fetchall 一个亿级表就晚了
                values = [_jsonable(v) for v in row]
                rows.append(values)
                size += len(json.dumps(values, ensure_ascii=False, default=str))
                if len(rows) >= limits.max_rows or size >= limits.max_bytes:
                    truncated = True
                    break
            return columns, rows, truncated

    try:
        columns, rows, truncated = await asyncio.wait_for(
            _exec(), timeout=limits.timeout_seconds
        )
    except asyncio.TimeoutError as e:
        raise SqlRejected(
            f"查询超过 {limits.timeout_seconds}s 被中断。加上 WHERE 条件或 LIMIT 缩小范围。"
        ) from e

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        sql=statement,
    )


async def _run_write(
    engine: AsyncEngine, statement: str, limits: QueryLimits, started: float
) -> QueryResult:
    """可写源上的写操作：真的提交。

    以前写和查询走同一条 stream 路径：拿不到结果集就抛 ResourceClosedError，
    连接关闭时再回滚——可写源上的写从来没落过库，调用方只看到一个报错。
    能走到这里的写都已经过了审批（registry.call_is_dangerous → 各节点的审批关卡），
    只读源上的写在 check 里就被拒了。
    """
    import time

    async def _exec() -> tuple[list[str], list[list[Any]]]:
        async with engine.begin() as conn:   # 正常退出提交，出错回滚
            result = await conn.execute(text(statement))
            if result.returns_rows:          # RETURNING、PG 的数据修改 CTE
                return (list(result.keys()),
                        [[_jsonable(v) for v in row] for row in result.fetchmany(limits.max_rows)])
            return ["affected_rows"], [[result.rowcount]]

    try:
        columns, rows = await asyncio.wait_for(_exec(), timeout=limits.timeout_seconds)
    except asyncio.TimeoutError as e:
        raise SqlRejected(f"写操作超过 {limits.timeout_seconds}s 被中断，已回滚。") from e
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) >= limits.max_rows,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        sql=statement,
    )


def _jsonable(value: Any) -> Any:
    """把驱动返回的类型转成能进 JSON 的形态。

    Decimal、date、datetime、bytes 这些直接 json.dumps 会炸，而它们在真实业务
    数据里遍地都是——金额是 Decimal、时间是 datetime，不处理等于不可用。
    """
    from datetime import date, datetime, time as dtime
    from decimal import Decimal

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        # 转 float 会丢精度，而金额恰恰不能丢。字符串保真，下游要算再自己转。
        return str(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return f"<binary {len(raw)} bytes>"
    return str(value)
