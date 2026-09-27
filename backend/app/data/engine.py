"""数据源连接层。各家数据库的方言差异全部收敛在这个文件里。

上层（工具、探查、API）只认 DataSource 这个模型，不需要知道 Oracle 要 service_name、
MySQL 要 charset、asyncpg 不认 sslmode 这些事。哪天要加一种库，改这里一处。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy import text

from app.core.crypto import decrypt
from app.data import guard
from app.data.guard import QueryLimits, SqlRejected, check, is_write

logger = logging.getLogger(__name__)

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

#: 数据源 options 里按源配置的查询时限（秒）。表单上叫「查询时限」
QUERY_TIMEOUT_OPTION = "query_timeout_s"
#: 按源配置时能填的范围。再长的查询不该让一个 agent 干等：该做成离线任务了
MIN_QUERY_TIMEOUT_S = 1
MAX_QUERY_TIMEOUT_S = 600

# options 里这些 key 是 AgentLab 自己的配置，不是驱动参数，拼 URL 时要摘掉。
# schema：探查哪个 schema（企业库里只读账号名下常常什么都没有，数据在别处）
# query_timeout_s：查询时限，由数据层按语句下发给数据库，见 _server_deadline
_NON_DRIVER_OPTIONS = frozenset({"schema", QUERY_TIMEOUT_OPTION})


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


def _timeout_value(value: Any) -> float | None:
    """options 里的查询时限：表单存的是字符串，也认数字。填得不对返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if not MIN_QUERY_TIMEOUT_S <= seconds <= MAX_QUERY_TIMEOUT_S:
        return None
    return int(seconds) if seconds.is_integer() else seconds


def query_timeout_problem(options: dict[str, Any] | None) -> str | None:
    """保存数据源时查一下查询时限填得对不对；没填、或者填对了返回 None。"""
    value = (options or {}).get(QUERY_TIMEOUT_OPTION)
    if value is None or str(value).strip() == "":
        return None
    if _timeout_value(value) is None:
        return (f"查询时限要填 {MIN_QUERY_TIMEOUT_S} 到 {MAX_QUERY_TIMEOUT_S} 之间的秒数，"
                f"比如 60；现在填的是「{value}」")
    return None


def query_timeout(source: Any) -> float:
    """这个数据源上一条查询最多跑多久（秒）。没配、或者库里存着的值不对，用缺省。

    缺省按模块属性现取：只有 guard.QueryLimits 这一处定义。
    """
    value = _timeout_value((source.options or {}).get(QUERY_TIMEOUT_OPTION))
    return value if value is not None else guard.QueryLimits().timeout_seconds


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


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


#: 数据库按时限停下语句之后，数据层自己的兜底再多等多久。要比引擎的宽限
#: （toolcalls.GRACE_S）短：数据库报上来的「超过 Ns 被中断」才是给人看的那句，
#: 引擎那边只在数据层也卡住时才出面
BACKSTOP_S = 0.5

#: SQLite 每执行多少条虚拟机指令问一次进度回调。回调只比一次时间，几毫秒一问足够准
_SQLITE_STEPS = 10_000

#: 各家数据库按时限停下语句时的报错。认出来就说「超时」，而不是把驱动原文当成
#: SQL 写错了交给模型——它会去改一条本来没错的 SQL。
#:
#: 先认错误码：服务器的报错文字会跟着它的语言设置走，中文环境的 PostgreSQL 报的是
#: 「由于语句执行超时，正在取消查询命令」，按英文原文找一个也认不出来
_PG_CANCELED = "57014"                    # PostgreSQL query_canceled（statement_timeout 到点）
_MYSQL_TIMEOUT_CODES = frozenset({3024,   # MySQL：max_execution_time 到点
                                  1969})  # MariaDB：max_statement_time 到点
_TIMEOUT_MARKERS = (
    "statement timeout",                   # PostgreSQL（英文环境）
    "maximum statement execution time",    # MySQL 3024
    "max_statement_time",                  # MariaDB 1969
    "dpy-4024", "dpi-1067", "call timeout",  # python-oracledb：call timeout of N ms exceeded（驱动自己的话，不随服务器语言变）
)


def _causes(e: BaseException) -> list[BaseException]:
    """异常连同它包着的原始驱动异常。SQLAlchemy 包一层（.orig），适配器再包一层（__cause__），
    有的路径（服务端游标取行）又原样抛出驱动异常，所以几条链都走一遍。"""
    out: list[BaseException] = []
    todo: list[Any] = [e]
    while todo and len(out) < 8:
        err = todo.pop(0)
        if isinstance(err, BaseException) and err not in out:
            out.append(err)
            todo += [getattr(err, "orig", None), err.__cause__, err.__context__]
    return out


class _Deadline:
    """一条语句的时限。数据库那边按它停下；停下时报的错据此认成超时。"""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.ends = time.monotonic() + seconds
        #: SQLite 的进度回调真的掐过
        self.fired = False
        #: 语句已经结束。回调这时要是还挂在连接上（摘除没成功），也不能再掐别人的语句
        self.over = False

    @property
    def ms(self) -> int:
        return max(1, int(self.seconds * 1000))

    def expired(self) -> bool:
        return time.monotonic() >= self.ends

    def sqlite_progress(self) -> int:
        # 在 SQLite 执行语句的那个线程里被调用；返回非零，SQLite 就中止当前语句
        if not self.over and time.monotonic() >= self.ends:
            self.fired = True
            return 1
        return 0

    def stopped(self, e: BaseException) -> bool:
        """这个报错是不是数据库按时限停下语句造成的。"""
        if self.fired:
            return True
        for err in _causes(e):
            if _PG_CANCELED in (getattr(err, "sqlstate", None), getattr(err, "pgcode", None)):
                return True
            args = getattr(err, "args", None) or ()
            if args and isinstance(args[0], int) and args[0] in _MYSQL_TIMEOUT_CODES:
                return True
            low = str(err).lower()
            if any(marker in low for marker in _TIMEOUT_MARKERS):
                return True
            # 各家的措辞不一（interrupted / canceled，或者干脆只在类名里）；
            # 过了时限才报出来的，就是时限掐的
            if self.expired() and any(w in f"{type(err).__name__} {low}".lower()
                                      for w in ("interrupt", "cancel")):
                return True
        return False


async def _driver(conn: AsyncConnection) -> Any:
    return (await conn.get_raw_connection()).driver_connection


@asynccontextmanager
async def _server_deadline(conn: AsyncConnection, kind: str, deadline: _Deadline) -> AsyncIterator[None]:
    """让数据库自己在时限处停下这条语句。

    只靠后端这边 wait_for 放弃等待是不够的：数据库那边的语句照样在跑，占着连接池
    里的一个连接直到跑完（Oracle 实测多占了 60 秒）；aiosqlite 更糟，语句在线程里
    跑，取消之后关连接要排在它后面，一条不收敛的查询能让调用永远回不来。

    会话级的设置用完要恢复：连接会回到池子里，探查结构这类长操作也用它。
    下发失败（老版本的 MySQL 没有这个变量）不挡查询，只剩后端这边的兜底。
    """
    if kind in ("postgres", "postgresql"):
        # SET LOCAL 只管这一个事务：连接还回池子时回滚，设置随之作废
        await conn.execute(text(f"SET LOCAL statement_timeout = {deadline.ms}"))
        yield
        return

    if kind in ("mysql", "mariadb"):
        # MySQL 的 max_execution_time 以毫秒计、只管 SELECT；MariaDB 没有它，
        # 用 max_statement_time（秒）。按连上的服务器认，不按表单里选的类型
        if getattr(conn.dialect, "is_mariadb", False):
            var, value = "max_statement_time", _seconds(deadline.seconds)
        else:
            var, value = "max_execution_time", str(deadline.ms)
        try:
            await conn.execute(text(f"SET SESSION {var} = {value}"))
        except Exception as e:  # noqa: BLE001
            logger.warning("MySQL 语句时限没下发成功（%s），只剩后端兜底", e)
            yield
            return
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            # 连接还卡在那条语句上，这时再发一条只会排在它后面
            cancelled = True
            raise
        finally:
            if not cancelled:
                try:
                    await conn.execute(text(f"SET SESSION {var} = DEFAULT"))
                except Exception:  # noqa: BLE001 - 连接已经坏了的话，连接池会把它作废
                    pass
        return

    if kind == "oracle":
        driver = await _driver(conn)
        before = getattr(driver, "call_timeout", 0)
        try:
            driver.call_timeout = deadline.ms
        except Exception as e:  # noqa: BLE001
            logger.warning("Oracle call_timeout 没设上（%s），只剩后端兜底", e)
            yield
            return
        try:
            yield
        finally:
            try:
                driver.call_timeout = before
            except Exception:  # noqa: BLE001 - 超时后连接可能已经被驱动关掉
                pass
        return

    if kind == "sqlite":
        driver = await _driver(conn)
        await driver.set_progress_handler(deadline.sqlite_progress, _SQLITE_STEPS)
        try:
            yield
        finally:
            deadline.over = True
            try:
                await driver.set_progress_handler(None, 0)
            except Exception:  # noqa: BLE001 - over 已经让它不再掐人
                pass
        return

    yield


async def _bounded(
    opener: Callable[[], Any], kind: str, limits: QueryLimits, over: str,
    work: Callable[[AsyncConnection], Awaitable[Any]],
) -> Any:
    """在 opener() 开的连接上跑 work，时限交给数据库执行，后端这边再留一道兜底。

    数据库按时限停下语句时报的错，翻成 over 这句话：「超过 Ns 被中断」。
    """
    deadline: _Deadline | None = None

    async def _exec() -> Any:
        nonlocal deadline
        async with opener() as conn:
            deadline = _Deadline(limits.timeout_seconds)
            async with _server_deadline(conn, kind, deadline):
                return await work(conn)

    try:
        return await asyncio.wait_for(_exec(), timeout=limits.timeout_seconds + BACKSTOP_S)
    except asyncio.TimeoutError as e:
        raise SqlRejected(over) from e
    except Exception as e:
        if deadline is not None and deadline.stopped(e):
            raise SqlRejected(over) from e
        raise


async def run_query(
    source: Any, sql: str, *, limits: QueryLimits | None = None
) -> QueryResult:
    """执行一条查询。守卫先过，再谈执行。

    注意执行的是 guard.check 的返回值而不是原始输入——守卫会把尾分号之类
    规范掉，执行原文等于绕过了守卫。

    时限没指定时按数据源自己的配置（query_timeout），由数据库执行（_server_deadline）。
    """
    limits = limits or QueryLimits(timeout_seconds=query_timeout(source))
    statement = check(sql, readonly=bool(source.readonly), source_name=source.name)
    kind = (source.kind or "").lower()

    engine = await engines.get(source)
    started = time.perf_counter()

    if not source.readonly and is_write(statement):
        return await _run_write(engine, kind, statement, limits, started)

    async def _read(conn: AsyncConnection) -> tuple[list[str], list[list[Any]], bool]:
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

    columns, rows, truncated = await _bounded(
        engine.connect, kind, limits,
        f"查询超过 {_seconds(limits.timeout_seconds)}s 被中断。加上 WHERE 条件或 LIMIT 缩小范围。",
        _read,
    )
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        sql=statement,
    )


async def _run_write(
    engine: AsyncEngine, kind: str, statement: str, limits: QueryLimits, started: float
) -> QueryResult:
    """可写源上的写操作：真的提交。

    以前写和查询走同一条 stream 路径：拿不到结果集就抛 ResourceClosedError，
    连接关闭时再回滚——可写源上的写从来没落过库，调用方只看到一个报错。
    能走到这里的写都已经过了审批（registry.call_is_dangerous → 各节点的审批关卡），
    只读源上的写在 check 里就被拒了。
    """

    async def _write(conn: AsyncConnection) -> tuple[list[str], list[list[Any]]]:
        result = await conn.execute(text(statement))
        if result.returns_rows:          # RETURNING、PG 的数据修改 CTE
            return (list(result.keys()),
                    [[_jsonable(v) for v in row] for row in result.fetchmany(limits.max_rows)])
        return ["affected_rows"], [[result.rowcount]]

    # engine.begin()：正常退出提交，出错（包括数据库按时限停下）回滚
    columns, rows = await _bounded(
        engine.begin, kind, limits,
        f"写操作超过 {_seconds(limits.timeout_seconds)}s 被中断，已回滚。", _write,
    )
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
