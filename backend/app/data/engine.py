"""数据源连接层。各家数据库的方言差异全部收敛在这个文件里。

上层（工具、探查、API）只认 DataSource 这个模型，不需要知道 Oracle 要 service_name、
MySQL 要 charset、asyncpg 不认 sslmode 这些事。哪天要加一种库，改这里一处。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy import event, text

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

#: 数据源 options 里要遮罩的列（列名列表，或者逗号分隔的一段文字）。证据面板展示原始行时，
#: 这些列的值一律换成「已遮罩」。在有身份体系之前，遮罩只减少暴露，不是安全边界：
#: 完整快照仍能按工件 id 取到，SQL 里给列起个别名也能绕开
MASK_COLUMNS_OPTION = "mask_columns"

# options 里这些 key 是 AgentLab 自己的配置，不是驱动参数，拼 URL 时要摘掉。
# schema：探查哪个 schema（企业库里只读账号名下常常什么都没有，数据在别处）
# query_timeout_s：查询时限，由数据层按语句下发给数据库，见 _server_deadline
# mask_columns：证据面板遮罩的列，驱动不认识它，拼进连接串会被当成未知参数拒掉
_NON_DRIVER_OPTIONS = frozenset({"schema", QUERY_TIMEOUT_OPTION, MASK_COLUMNS_OPTION})

_MASK_SPLIT = re.compile(r"[,，、;；\n]+")


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    sql: str
    #: 列名 → number / text / date / datetime / time / boolean。拿不准的列（全是空值、类型混着）不在里面
    column_types: dict[str, str] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "sql": self.sql,
        }
        if self.column_types:
            payload["column_types"] = self.column_types
        return payload


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
            raise ValueError("Oracle 需要填写 service_name 或 SID（也可填在「数据库」中）")
        tail = f"?service_name={service}" if service else f"?sid={sid}"
        query = "&".join(f"{k}={v}" for k, v in options.items())
        return f"{driver}://{auth}{host}/{tail}{('&' + query) if query else ''}"

    query = "&".join(f"{k}={v}" for k, v in options.items())
    return f"{driver}://{auth}{host}/{source.database or ''}{('?' + query) if query else ''}"


class SqliteHardeningError(sqlite3.OperationalError):
    """SQLite 连接没能关掉「双引号当字符串」的兼容行为。这条连接不交出去。

    继承 sqlite3.OperationalError：SQLAlchemy 按驱动异常包装，上层（测试连接、工具报错）
    照常拿到一个连接失败，而不是一个不认识的异常类型。
    """


class _DqsOffConnection(sqlite3.Connection):
    """打开时就关掉 DQS 的 sqlite3 连接（经 sqlite3.connect 的 factory 参数接入）。

    DQS（double-quoted string）是 SQLite 的历史兼容：双引号里的名字找不到对应的列时，
    当成字符串字面量。于是列名写错不报错：`SUM("金颔")` 得 0，`GROUP BY "金颔"`
    把整张表归成一组，结论错了却没人知道。关掉以后同一条 SQL 报 no such column。

    为什么用 factory 而不是 SQLAlchemy 的 connect 事件：aiosqlite 的连接在它自己的
    线程里建，connect 事件拿到的是异步适配层，要碰底层 sqlite3 连接只能走 aiosqlite
    的私有属性（_execute / _conn），升级就可能失效。factory 是 sqlite3.connect 的公开
    参数，aiosqlite.connect 的 **kwargs 原样转给它，SQLAlchemy 的 connect_args 又原样
    转给 aiosqlite.connect——三层都是公开接口。设置在建连接的那个线程里同步完成，
    没设上就在这里抛错，连接根本不会进连接池。而且凡是经 engine_args 建的 engine
    （连接缓存、表单里的一次性测试连接）都自动带上，不必逐处挂事件。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            _disable_dqs(self)
        except BaseException:
            self.close()
            raise


#: 要关掉的两项：DML（查询、增删改）和 DDL（建表、建索引）里的双引号字符串
_DQS_OPTIONS = (
    ("DML", getattr(sqlite3, "SQLITE_DBCONFIG_DQS_DML", None)),
    ("DDL", getattr(sqlite3, "SQLITE_DBCONFIG_DQS_DDL", None)),
)


def _disable_dqs(conn: sqlite3.Connection) -> None:
    """关掉 DQS，并读回来确认确实关了。任何一步不成立都抛 SqliteHardeningError。

    Python 3.12 起才有 setconfig / getconfig；没有的话同样拒绝——宁可连不上，
    也不交出一条会把写错的列名静默当成字符串的连接。
    """
    for label, op in _DQS_OPTIONS:
        try:
            if op is None:
                raise AttributeError(f"sqlite3 模块缺少 SQLITE_DBCONFIG_DQS_{label}")
            conn.setconfig(op, False)
            still_on = conn.getconfig(op)
        except Exception as e:  # noqa: BLE001 - 原因原样带出，统一换成拒绝连接
            raise SqliteHardeningError(_dqs_refusal(f"{type(e).__name__}: {e}")) from e
        if still_on:
            raise SqliteHardeningError(_dqs_refusal(f"设置后读回仍为开启（{label}）"))


def _dqs_refusal(cause: str) -> str:
    return ("未能关闭 SQLite 的双引号字符串兼容，已拒绝连接"
            f"（否则写错的列名会被当成文字，查询不报错却给出错误结果）：{cause}")


#: 自检先建的探针表
_SELF_CHECK_SETUP = "CREATE TABLE _dqs_probe (a INTEGER)"
#: 自检的探针：每条都写了一个不存在的双引号列名，关掉 DQS 后都必须报 no such column。
#: 查询一条验 DML 那项，建索引一条验 DDL 那项——只关一项时另一条会照常执行。
#: 用 ASCII 名字：DQS 对中英文名字一视同仁，SQL 原文也不必进文案检查
_SELF_CHECK_PROBES = (
    'SELECT "no_such_column" FROM _dqs_probe',
    'CREATE INDEX _dqs_probe_i ON _dqs_probe ("no_such_column")',
)


def sqlite_dqs_self_check() -> None:
    """启动自检：在内存库上确认 DQS 确实关得掉，写错的双引号列名确实报错。

    连接层的每条 SQLite 连接都靠 _DqsOffConnection 关 DQS；这里用同一个类开一条内存
    连接，查询和建索引各验一次。不成立就抛 RuntimeError，让服务起不来——Python 或
    SQLite 换了版本、行为变了，宁可启动失败，也不悄悄退回「列名写错照样出数」。

    只认「no such column」这一种报错：探针因为别的原因报错（表没建上、语法变了），
    说明它根本没走到双引号名字那一步，什么也没验到，同样算失败。
    """
    problem: str | None = None
    try:
        conn = sqlite3.connect(":memory:", factory=_DqsOffConnection)
    except Exception as e:  # noqa: BLE001
        problem = str(e)
    else:
        try:
            conn.execute(_SELF_CHECK_SETUP)
            for sql in _SELF_CHECK_PROBES:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    if "no such column" not in str(e):
                        problem = f"{sql} 报错，但不是预期的「no such column」：{e}"
                        break
                else:
                    problem = f"{sql} 没有报错：双引号里的名字仍被当成字符串"
                    break
        except Exception as e:  # noqa: BLE001 - 探针表都建不起来，同样是没验到
            problem = f"{type(e).__name__}: {e}"
        finally:
            conn.close()
    if problem:
        logger.critical("SQLite 自检失败，服务不启动：%s", problem)
        raise RuntimeError(f"SQLite 自检失败：无法确认写错的双引号列名会报错，服务不启动。{problem}")


def open_checked_sqlite(path: str, *, readonly: bool = True, immutable: bool = False) -> sqlite3.Connection:
    """同步打开一个本地 SQLite 文件，DQS 已关（配方导入的建库、核对用）。

    配方执行器建临时库、核对模块跑 SQL 都不经 SQLAlchemy，直接用 sqlite3。它们同样要关 DQS：
    核对 SQL 里写错一个双引号列名，在默认连接上会被当成字符串常量，`TOTAL("客流")` 得 0、
    `WHERE "时段类别" = ?` 一行都对不上，于是「合计与明细一致」或「不一致」都可能是假的。
    所以和连接层共用 _DqsOffConnection：关不掉就抛 SqliteHardeningError，连接不交出去。

    readonly=True 以 mode=ro 打开（核对只读，文件不存在直接报错，不会悄悄建一个空库）；
    readonly=False 以 mode=rwc 打开（执行器建库）。不设日志模式：沿用 SQLite 默认的回滚日志，
    不开 WAL——WAL 会改写文件头，库文件哈希就对不上了（P2-SPEC 4.2 第 9 步）。
    路径按 URI 转义：文件名里的空格、中文、「?」「#」都不会被当成 URI 的参数或片段。

    immutable=True 另加 `immutable=1`，和 EngineCache 打开版本快照的方式一致（_sqlite_url）：证据下钻在快照文件上
    单开一个连接编译原 SQL（provenance_db.compile_facts，P4-SPEC 2.9），读的必须是查询走的同一种打开方式。
    只许和 readonly 一起用：immutable 告诉 SQLite 文件不会变、不加锁，可写的连接这样开会写坏库。缺省 False，
    现有调用方的行为不变。
    """
    from urllib.parse import quote

    if immutable and not readonly:
        raise ValueError("immutable 只能用于只读连接")
    mode = "ro" if readonly else "rwc"
    extra = "&immutable=1" if immutable else ""
    return sqlite3.connect(f"file:{quote(os.fspath(path))}?mode={mode}{extra}", uri=True,
                           factory=_DqsOffConnection)


def open_memory_sqlite() -> sqlite3.Connection:
    """一个内存 SQLite 连接，DQS 已关（合并查询把几次查询的结果落进来再合并，engine/merge_query.py）。

    合并 SQL 同样是模型写的：双引号里的列名写错一个字，默认连接会把它当成字符串常量，`SUM("销售颔")` 得 0，
    `ON s."门店" = v."门店 "` 一行都对不上却不报错。所以和连接层共用 _DqsOffConnection，关不掉就不交出连接。
    """
    return sqlite3.connect(":memory:", factory=_DqsOffConnection)


def _sqlite_extra() -> dict[str, Any]:
    """每个 SQLite engine 都带的连接参数：关 DQS。"""
    return {"connect_args": {"factory": _DqsOffConnection}}


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

    SQLite 不论只读与否都关 DQS（见 _DqsOffConnection）。source 带 immutable
    （上传表格的版本快照，见 table_versions.SourceView）时，以 mode=ro&immutable=1
    打开，不看 readonly 字段：快照文件发布后就不再变，immutable 让 SQLite 不加锁、
    不找日志文件，同时也写不进去。
    """
    url = build_url(source, reveal=True)
    kind = (source.kind or "").lower()
    if kind == "sqlite":
        return _sqlite_url(source, url), _sqlite_extra()
    if not source.readonly:
        return url, {}
    if kind in ("postgres", "postgresql"):
        return url, {"connect_args": {"server_settings": {"default_transaction_read_only": "on"}}}
    if kind in ("mysql", "mariadb"):
        return url, {"connect_args": {"init_command": "SET SESSION TRANSACTION READ ONLY"}}
    return url, {}


def _sqlite_url(source: Any, url: str) -> str:
    from urllib.parse import quote

    path = source.database or ""
    if getattr(source, "immutable", False):
        if not path or path == ":memory:":
            raise ValueError("版本快照必须指向一个数据文件")
        return f"{_DRIVERS['sqlite']}:///file:{quote(path)}?mode=ro&immutable=1&uri=true"
    if source.readonly and path and path != ":memory:":
        return f"{_DRIVERS['sqlite']}:///file:{quote(path)}?mode=ro&uri=true"
    return url


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
        return (f"查询时限需要填写 {MIN_QUERY_TIMEOUT_S} 到 {MAX_QUERY_TIMEOUT_S} 之间的秒数，"
                f"例如 60；当前为「{value}」")
    return None


def masked_columns(options: dict[str, Any] | None) -> list[str]:
    """options.mask_columns → 列名列表（去空白、去重，保持顺序）。列表和「a, b」这样的文字都认。"""
    raw = (options or {}).get(MASK_COLUMNS_OPTION)
    items = _MASK_SPLIT.split(raw) if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return list(dict.fromkeys(str(c).strip() for c in items if isinstance(c, (str, int)) and str(c).strip()))


def mask_columns_problem(options: dict[str, Any] | None) -> str | None:
    """保存数据源时查一下遮罩列填得对不对；没填、或者填对了返回 None。"""
    raw = (options or {}).get(MASK_COLUMNS_OPTION)
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str) or (isinstance(raw, list) and all(isinstance(c, str) for c in raw)):
        return None
    return f"遮罩的列需要填写列名，用逗号分隔，例如「phone, email」；当前为「{raw}」"


def query_timeout(source: Any) -> float:
    """这个数据源上一条查询最多跑多久（秒）。没配、或者库里存着的值不对，用缺省。

    缺省按模块属性现取：只有 guard.QueryLimits 这一处定义。
    """
    value = _timeout_value((source.options or {}).get(QUERY_TIMEOUT_OPTION))
    return value if value is not None else guard.QueryLimits().timeout_seconds


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


#: 连接缓存最多留多少个 engine。版本快照各占一个键，长期运行下键只增不减；
#: 超出时关掉最久没用的那个，下次用到再建
MAX_ENGINES = 32


class SnapshotTampered(ValueError):
    """版本快照的数据文件和登记的哈希对不上（或者文件没了）。拒绝查询，也不缓存。"""


_TAMPERED = "数据文件与登记的版本不一致，可能被修改过，已拒绝查询"
_SNAPSHOT_UNREADABLE = "登记版本的数据文件无法读取，无法核对是否被修改过，已拒绝查询"
_NO_HASH = "数据版本缺少文件哈希的登记，无法核对数据文件是否被修改过，已拒绝查询"
_REPLACED = "数据文件在核对之后被替换或改动过，已拒绝这次查询；再次查询时会重新核对"

#: 快照文件的指纹：(st_dev, st_ino, st_size, st_mtime_ns)。整份替换（rename 覆盖）换 inode，
#: 原地改写换大小或修改时间：两种都认得出，而 stat 只要几微秒，每次取引擎、每次借连接都查得起
Fingerprint = tuple[int, int, int, int]


def cache_key(source: Any) -> str:
    """连接缓存的键：数据源 id 加版本快照 id（没有快照的源为空）。

    上传表格每次发布都是一个新文件，同一个数据源的新旧版本得各用各的连接池——
    否则固定在旧版本上的运行会查到新文件，或者反过来。
    """
    return f"{source.id}:{getattr(source, 'snapshot_id', None) or ''}"


def _source_of(key: str) -> str:
    # 快照 id 是十六进制，不含冒号；数据源 id 里就算有冒号，从右边切也切得对
    return key.rsplit(":", 1)[0]


def _file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _fingerprint(path: str) -> Fingerprint | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


class _Moving(OSError):
    """算哈希的过程中文件变了：算出来的哈希说明不了现在的文件。"""


def _hash_and_print(path: str) -> tuple[str, Fingerprint]:
    """算文件哈希，连同它的指纹。前后各 stat 一次：对不上说明读的过程中文件被替换或改写了。

    指纹和哈希必须描述同一个文件：之后每次取引擎、借连接都拿当时的 stat 和这份指纹比，
    比得上才说明 SQLite 打开的还是核对过的那个文件。
    """
    before = _fingerprint(path)
    digest = _file_sha256(path)
    after = _fingerprint(path)
    if before is None or before != after:
        raise _Moving(f"文件在核对过程中发生了变化：{path}")
    return digest, after


async def _verify_snapshot(source: Any, expected: str) -> Fingerprint:
    """建 engine 之前核对快照文件的哈希，返回核对时的文件指纹。大文件读一遍要时间，放到线程里，不卡事件循环。"""
    try:
        actual, fingerprint = await asyncio.to_thread(_hash_and_print, source.database or "")
    except _Moving as e:
        logger.warning("快照文件在核对过程中发生了变化：source=%s snapshot=%s path=%s",
                       source.id, getattr(source, "snapshot_id", None), source.database)
        raise SnapshotTampered(_TAMPERED) from e
    except OSError as e:
        raise SnapshotTampered(_SNAPSHOT_UNREADABLE) from e
    if actual.lower() != str(expected).strip().lower():
        logger.warning("快照文件哈希不符：source=%s snapshot=%s path=%s",
                       source.id, getattr(source, "snapshot_id", None), source.database)
        raise SnapshotTampered(_TAMPERED)
    return fingerprint


@dataclass
class _Pin:
    """一个快照引擎核对过的文件：路径和核对时的指纹。stale 表示已经发现对不上，不再可信。"""

    path: str
    fingerprint: Fingerprint
    stale: bool = False

    def holds(self) -> bool:
        return not self.stale and _fingerprint(self.path) == self.fingerprint


def _guard_pool(engine: AsyncEngine, pin: _Pin, key: str) -> None:
    """每次从连接池借连接时核对文件指纹，对不上就拒绝这条连接。

    取引擎时核对过一次，但引擎缓存着、连接池会在之后新开连接（并发查询、pool_recycle、
    探活失败重连），每条新连接都按路径重新打开文件。文件要是在这期间被整份替换（恢复备份、
    同步工具），新连接读到的就是没核对过的文件。所以借连接（checkout）时再比一次：新开的
    连接打开的是哪个文件，这时 stat 看到的就是哪个。对不上就把这个键标成不可信并拒绝；
    抛出的异常让 SQLAlchemy 关掉这条连接，下次取引擎时重新核对哈希（内容没变的恢复会放行）。
    """
    def check(*_args: Any) -> None:
        if not pin.holds():
            if not pin.stale:
                logger.warning("快照文件在核对之后发生了变化，已拒绝连接：key=%s path=%s", key, pin.path)
            pin.stale = True
            raise SnapshotTampered(_REPLACED)

    event.listen(engine.sync_engine, "checkout", check)


class EngineCache:
    """按数据源（和版本快照）缓存 engine。连接池的建立不便宜，不该每次查询都重来一遍。

    配置变了要显式 invalidate——改了密码却还在用旧连接，排查起来很费神；
    改了只读开关却还在用旧连接，只读就形同虚设。invalidate 清掉这个源名下所有版本的连接。

    最多留 MAX_ENGINES 个，按最近使用排序，超出时关掉最久没用的。
    source 带 expected_sha256 时，这个键新建 engine 前核对一遍文件哈希，并记下核对时的文件指纹
    （inode、大小、修改时间）。之后缓存命中、连接池借连接时都拿当时的 stat 比这份指纹：快照文件
    0444 挡不住整份替换（rename 只要目录可写），对不上就摘掉这个引擎、下次重新核对哈希——内容
    没变（备份恢复回同样的文件）就放行，变了就拒绝。同一个键并发首次使用时只核对一次，大家等同一个结果。
    source 标着 immutable（版本快照）却没给期望哈希的，一律拒绝：默认放行的话，哪条新路径漏传
    哈希，快照就不经核对地打开了。
    """

    def __init__(self, max_engines: int | None = None) -> None:
        self._engines: OrderedDict[str, AsyncEngine] = OrderedDict()
        #: 快照引擎核对过的文件，键和 _engines 相同。手工源没有
        self._pins: dict[str, _Pin] = {}
        self._lock = asyncio.Lock()
        #: None 表示按模块常量 MAX_ENGINES（现取，测试里可以改）
        self._max = max_engines
        #: 正在进行的快照核对，按（缓存键, 文件, 登记的哈希）合并。核对结束（不论成败）即移除，
        #: 失败结果不留：下次照常重新核对
        self._verifying: dict[tuple[str, str, str], asyncio.Future[Fingerprint]] = {}

    def _hit(self, key: str, doomed: list[AsyncEngine]) -> AsyncEngine | None:
        """缓存里还可信的 engine。快照文件和核对时的指纹对不上的，摘出来放进 doomed 交给调用方关掉。"""
        engine = self._engines.get(key)
        if engine is None:
            return None
        pin = self._pins.get(key)
        if pin is not None and not pin.holds():
            if not pin.stale:
                logger.warning("快照文件在核对之后发生了变化，重新核对：key=%s path=%s", key, pin.path)
            del self._engines[key]
            self._pins.pop(key, None)
            doomed.append(engine)
            return None
        self._engines.move_to_end(key)
        return engine

    async def get(self, source: Any) -> AsyncEngine:
        key = cache_key(source)
        doomed: list[AsyncEngine] = []
        try:
            cached = self._hit(key, doomed)
            if cached is not None:
                return cached
            expected = str(getattr(source, "expected_sha256", None) or "").strip()
            if getattr(source, "immutable", False) and not expected:
                logger.warning("版本快照没有登记文件哈希，已拒绝：key=%s path=%s", key, source.database)
                raise SnapshotTampered(_NO_HASH)
            fingerprint: Fingerprint | None = None
            if expected:
                # 在锁外核对：哈希一个大文件要几秒，不能让别的数据源都排在它后面
                fingerprint = await self._verify_once(key, source, expected)
            async with self._lock:
                cached = self._hit(key, doomed)
                if cached is not None:
                    return cached
                url, extra = engine_args(source)
                engine = create_async_engine(
                    url,
                    pool_size=3,
                    max_overflow=2,
                    pool_pre_ping=True,   # 长时间空闲后连接会被数据库掐掉，先探活再用
                    pool_recycle=1800,
                    **extra,
                )
                if fingerprint is not None:
                    pin = _Pin(str(source.database or ""), fingerprint)
                    _guard_pool(engine, pin, key)
                    self._pins[key] = pin
                self._engines[key] = engine
                limit = max(1, self._max if self._max is not None else MAX_ENGINES)
                while len(self._engines) > limit:
                    old_key, old = self._engines.popitem(last=False)
                    self._pins.pop(old_key, None)
                    doomed.append(old)
            return engine
        finally:
            for old in doomed:
                # 正被借出的连接不受影响：dispose 只关池子里空闲的，借出的还回来时随旧池子回收
                await old.dispose()

    def pinned(self, source: Any) -> Fingerprint | None:
        """这个源（版本快照）的引擎核对过的文件指纹；不是快照引擎、或者引擎不在缓存里时为 None。

        给证据下钻的编译核对用（provenance_db.compile_facts，P4-SPEC 2.9）：它在同一个文件上单开一个连接，
        开连接前、关连接后各拿当时的 stat 和这份指纹比，比得上才说明单开的连接读的是 get() 核对过的那个文件。
        调用方要在 `await get(source)` 返回之后**同步**调它，中间不能有 await：否则这个键可能已经被 LRU 挤掉，
        或者被别的请求摘掉重核。只读不改：已经标成不可信（stale）的也照样返回那份指纹，调用方比对时自然对不上。
        """
        pin = self._pins.get(cache_key(source))
        return pin.fingerprint if pin is not None else None

    async def _verify_once(self, key: str, source: Any, expected: str) -> Fingerprint:
        """核对快照哈希；同一个键已经有一次在核对，就等那一次的结果。

        一次运行开头往往同时发起几次工具调用，查的是同一个快照。不合并的话每个请求
        各读一遍整个文件（快照可能几百 MB），还挤占默认线程池。

        合并的范围带上文件路径和登记的哈希：键相同而这两样不同的（不该出现，但不赌）
        各核各的，绝不拿别人的结论放行。核对本身包成任务、等的时候加 shield：某个
        请求被取消，不连累别人在等的那一次。核对结束就移除——成功之后到 engine
        装进缓存之间若恰好来了新请求，它会再核对一次；多算一次，不会少算。
        """
        token = (key, str(source.database or ""), str(expected).strip().lower())
        loop = asyncio.get_running_loop()
        pending = self._verifying.get(token)
        if pending is None or pending.get_loop() is not loop:
            pending = loop.create_task(_verify_snapshot(source, expected))
            self._verifying[token] = pending
            pending.add_done_callback(lambda done: self._settle_verify(token, done))
        return await asyncio.shield(pending)

    def _settle_verify(self, token: tuple[str, str, str], done: asyncio.Future[Fingerprint]) -> None:
        # 只移除自己：失效后可能已经有新的一次登记在同一个位置上
        if self._verifying.get(token) is done:
            del self._verifying[token]

    async def invalidate(self, source_id: str) -> None:
        sid = str(source_id)
        # 正在进行的核对也撇开：失效之后来的请求重新核对，不搭失效之前那一次的便车
        for token in [t for t in self._verifying if _source_of(t[0]) == sid]:
            del self._verifying[token]
        keys = [key for key in list(self._engines) if _source_of(key) == sid]
        doomed = [self._engines.pop(key) for key in keys]
        for key in keys:
            self._pins.pop(key, None)
        for engine in doomed:
            await engine.dispose()

    async def close(self) -> None:
        self._verifying.clear()
        for engine in list(self._engines.values()):
            await engine.dispose()
        self._engines.clear()
        self._pins.clear()


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
    """一条语句的时限。数据库那边按它停下；停下时报的错据此认成超时。

    从取连接之前起算，和后端兜底同一个起点：取连接花掉的也算在时限里，数据库只拿到
    剩下的那段（remaining_ms）。否则取连接一慢，兜底就抢在数据库前面取消语句。
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.ends = time.monotonic() + seconds
        #: SQLite 的进度回调真的掐过
        self.fired = False
        #: 语句已经结束。回调这时要是还挂在连接上（摘除没成功），也不能再掐别人的语句
        self.over = False

    def remaining_ms(self) -> int:
        return max(1, int((self.ends - time.monotonic()) * 1000))

    def remaining_seconds(self) -> float:
        return max(0.001, round(self.ends - time.monotonic(), 3))

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
        await conn.execute(text(f"SET LOCAL statement_timeout = {deadline.remaining_ms()}"))
        yield
        return

    if kind in ("mysql", "mariadb"):
        # MySQL 的 max_execution_time 以毫秒计、只管 SELECT；MariaDB 没有它，
        # 用 max_statement_time（秒）。按连上的服务器认，不按表单里选的类型
        if getattr(conn.dialect, "is_mariadb", False):
            var, value = "max_statement_time", _seconds(deadline.remaining_seconds())
        else:
            var, value = "max_execution_time", str(deadline.remaining_ms())
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
            driver.call_timeout = deadline.remaining_ms()
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
    两道时限从同一刻（取连接之前）算起，数据库那道先到，兜底晚 BACKSTOP_S。
    """
    deadline: _Deadline | None = None

    async def _exec() -> Any:
        nonlocal deadline
        deadline = _Deadline(limits.timeout_seconds)
        async with opener() as conn:
            # 光等连接就把时限用完了：语句不必再发，发出去也只剩 1ms
            if deadline.expired():
                raise SqlRejected(over)
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
    kind = (source.kind or "").lower()
    # 按数据源的方言扫：MySQL 的反斜杠转义、PG 的 $$ 字符串，守卫要和数据库读得一样
    statement = check(sql, readonly=bool(source.readonly), source_name=source.name, dialect=kind)

    engine = await engines.get(source)
    started = time.perf_counter()

    if not source.readonly and is_write(statement, dialect=kind):
        return await _run_write(engine, kind, statement, limits, started)

    async def _read(conn: AsyncConnection) -> tuple[list[str], list[list[Any]], bool, dict[str, str]]:
        cursor = await conn.stream(text(statement))
        columns = list(cursor.keys())
        rows: list[list[Any]] = []
        seen = _TypeTally(len(columns))
        truncated = False
        size = 0
        async for row in cursor:
            # 逐行累加，边收边判上限：一次性 fetchall 一个亿级表就晚了
            seen.add(row)
            values = [_jsonable(v) for v in row]
            rows.append(values)
            size += len(json.dumps(values, ensure_ascii=False, default=str))
            if len(rows) >= limits.max_rows or size >= limits.max_bytes:
                truncated = True
                break
        return columns, rows, truncated, seen.types(columns)

    columns, rows, truncated, types = await _bounded(
        engine.connect, kind, limits,
        f"查询超过 {_seconds(limits.timeout_seconds)} 秒被中断。请添加 WHERE 条件或 LIMIT 缩小范围。",
        _read,
    )
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        sql=statement,
        column_types=types,
    )


class _TypeTally:
    """边读边记每列见过的值类型。只看驱动交回的原始值：_jsonable 之后 Decimal 就成了字符串，
    小数位为 0 的 DECIMAL（"45678"）和文本列的 "2026" 再也分不开。"""

    def __init__(self, width: int) -> None:
        self._kinds: list[set[str]] = [set() for _ in range(width)]

    def add(self, row: Any) -> None:
        for kinds, value in zip(self._kinds, row):
            if value is not None:
                kinds.add(_value_kind(value))

    def types(self, columns: list[str]) -> dict[str, str]:
        return {str(name): kind for name, kinds in zip(columns, self._kinds)
                if len(kinds) == 1 and (kind := next(iter(kinds))) != "other"}


def _value_kind(value: Any) -> str:
    from datetime import date, datetime, time as dtime
    from decimal import Decimal

    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, dtime):
        return "time"
    return "text" if isinstance(value, str) else "other"


def column_types(columns: list[str], rows: list[Any]) -> dict[str, str]:
    """一批原始行里每列的类型：number / text / date / datetime / time / boolean。

    全是空值、类型混着（同一列里有数也有文本）的列不记——拿不准就不说，下游照旧按值猜。
    """
    tally = _TypeTally(len(columns))
    for row in rows:
        tally.add(row)
    return tally.types(columns)


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
        f"写操作超过 {_seconds(limits.timeout_seconds)} 秒被中断，已回滚。", _write,
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
