"""数据剖析：对业务库发少量只读查询，验证数据目录里推断出来的关联关系，补上基数、覆盖率和码值候选。

**为什么要有它。** 命名推断只看列名，可能推错，也给不出基数和覆盖率；而只有 verified、confirmed 的关系才会
触发错误级的 SQL 检查。剖析拿数据说话：从子表抽一批不同的键值去父表里对，对得上、父键唯一的升为 verified
（来源记为 profile，和外键约束的 fk 分开），对不上的保持 proposed 并在备注里写明覆盖率。顺带给状态类的列
取一次取值分布（码值候选，含义留给人填），给只有一个日期列的表提议业务日期。

**这是数据目录里唯一会对业务库发查询的地方**，约束都是硬的：

- 默认关闭，按数据源在 options.catalog_profile 里开启（profile_settings）；
- 每条查询先过守卫的只读判定（不论数据源本身可写与否），再交给查询层 run_query：同一套连接、守卫和
  时限，不另开连接；
- 每条查询有时限（不超过数据源自己的查询时限），每次剖析有查询次数上限和总时长上限；
- 表有多大先看数据库的统计信息，没有统计信息就数到上限为止，不做全表 COUNT；超过上限的表不做
  COUNT(DISTINCT)、MIN/MAX 这类整表统计，抽样和取值分布也只看前若干行；
- 数据源设置里遮罩的列不取样，也不出现在任何一条剖析查询里；
- 只取低基数列的取值分布（最多 21 个取值），不取明细行。
"""
from __future__ import annotations

import copy
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, guard
from app.data import engine as data_engine
from app.data.engine import CATALOG_PROFILE_OPTION, QueryResult, SnapshotTampered
from app.data.guard import QueryLimits, SqlRejected

logger = logging.getLogger(__name__)

#: 数据源 options 里剖析设置的键
PROFILE_OPTION = CATALOG_PROFILE_OPTION


# ==========================================================================
# 设置
# ==========================================================================


@dataclass(frozen=True)
class ProfileSettings:
    """一个数据源的剖析设置。缺省即「关闭」：剖析会对业务库发查询，必须有人按源明确打开。"""

    enabled: bool = False
    #: 一次剖析最多发几条查询（读统计信息、抽样、核对都算）。用完就停，剩下的记为「查询次数用完」
    max_queries: int = 60
    #: 单条查询的时限（秒）。实际取它和数据源自己的查询时限中较小的一个
    query_timeout_s: float = 10
    #: 每条关系从子表抽多少个不同的键值去父表里对
    sample_size: int = 2000
    #: 行数不超过它的表才做 COUNT(DISTINCT)、MIN/MAX 这类整表统计；更大的、或者不知道多大的表只看前若干行。
    #: 填 0 表示一律不做整表统计
    max_scan_rows: int = 100_000
    #: 一次剖析的总时长上限（秒）。接口是同步的，不能让一个请求无限地挂着
    max_total_s: float = 120

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: 数值项：(中文叫法, 下限, 上限, 是否必须是整数)。叫法和将来设置界面上的标签一致，报错里只写它
_NUMBER_FIELDS: dict[str, tuple[str, float, float, bool]] = {
    "max_queries": ("查询次数上限", 1, 500, True),
    "query_timeout_s": ("单条查询时限（秒）", 1, 60, False),
    "sample_size": ("抽样键值数", 10, 10_000, True),
    "max_scan_rows": ("整表统计行数上限", 0, 10_000_000, True),
    "max_total_s": ("总时长上限（秒）", 10, 600, False),
}
_ENABLED_LABEL = "开启数据剖析"
_KNOWN_KEYS = frozenset({"enabled", *_NUMBER_FIELDS})


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _number(value: Any, low: float, high: float, integer: bool) -> int | float | None:
    """设置里的一个数：数字和数字写成的文字都认（表单存的常是文字）。不合规返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    if number != number or not low <= number <= high:       # NaN 也在这里挡掉
        return None
    if number.is_integer():
        return int(number)
    return None if integer else number


def profile_settings(options: dict[str, Any] | None) -> ProfileSettings:
    """options → 剖析设置。读的时候宽松：库里存着的某一项不对（老数据、手改过），那一项用缺省，不报错；
    开关只认 true，写成别的一律当作没开。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if not isinstance(raw, dict):
        return ProfileSettings()
    values: dict[str, Any] = {"enabled": raw.get("enabled") is True}
    for key, (_, low, high, integer) in _NUMBER_FIELDS.items():
        number = _number(raw.get(key), low, high, integer)
        if number is not None:
            values[key] = number
    return ProfileSettings(**values)


def profile_settings_problem(options: dict[str, Any] | None) -> str | None:
    """保存数据源时查一下剖析设置；没填、或者填对了返回 None。写的时候严格：不认识的项、超出范围的值都拒。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "数据剖析设置的格式不正确，请在数据源设置中重新填写"
    if unknown := sorted(set(raw) - _KNOWN_KEYS):
        return f"数据剖析设置中有无法识别的项「{'、'.join(map(str, unknown))}」，请删除后重试"
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        return f"数据剖析的「{_ENABLED_LABEL}」只能是开启或关闭"
    for key, (label, low, high, integer) in _NUMBER_FIELDS.items():
        if key in raw and _number(raw[key], low, high, integer) is None:
            kind = "整数" if integer else "数"
            return f"数据剖析的「{label}」需要填写 {_fmt(low)} 到 {_fmt(high)} 之间的{kind}；当前为「{raw[key]}」"
    return None


# ==========================================================================
# SQL：字面量与方言
# ==========================================================================

#: 查询层不支持参数绑定（run_query 只收一段 SQL），核对键值只能把值拼进 IN 列表。所以只拼「自己刚从库里
#: 取回的原值」，并且按类型生成字面量：整数原样写，文字加单引号、单引号写两遍。下面几种文字不拼，跳过：
#: - 带反斜杠的：MySQL 在 NO_BACKSLASH_ESCAPES 开与不开时读法不同，守卫会整条拒掉；
#: - 带「:名字」的：查询层用 SQLAlchemy 的 text() 执行，它不认引号，会把字符串里的 :b 当成绑定参数；
#: - 带控制字符的、太长的：键值不该长这样，多半不是键。
_BIND_LIKE = re.compile(r"(?<![:\w\\]):\w")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INT_TEXT = re.compile(r"-?\d{1,38}")
#: 文字字面量的长度上限
MAX_LITERAL_LEN = 200


def sql_literal(value: Any, kind: str | None) -> str | None:
    """一个取回的值 → SQL 字面量；不安全或类型不支持时返回 None（调用方跳过这个值）。

    kind 是查询层记下的列类型（QueryResult.column_types：number / text / date …），None 表示拿不准。
    DECIMAL 经查询层已经转成了字符串（保精度），所以 number 列的整数文字原样写成数字。只认整数和文字：
    非整数的数做相等比较靠不住；日期、时间的字面量各家写法不同，不猜；布尔不当键。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 2 ** 53 else None
    if isinstance(value, Decimal):
        return str(int(value)) if value.is_finite() and value == value.to_integral_value() else None
    if not isinstance(value, str):
        return None
    if kind == "number":
        return value if _INT_TEXT.fullmatch(value) else None
    if kind not in ("text", None):
        return None
    if len(value) > MAX_LITERAL_LEN or "\\" in value or _CONTROL.search(value) or _BIND_LIKE.search(value):
        return None
    return "'" + value.replace("'", "''") + "'"


_DIALECT_MODULES = {"sqlite": sqlite, "postgres": postgresql, "postgresql": postgresql, "mysql": mysql,
                    "mariadb": mysql, "oracle": oracle}


class SqlDialect:
    """剖析 SQL 的方言差异：标识符怎么加引号、怎么只取前 n 行、统计信息在哪个系统视图里。只拼字符串，不连库。

    标识符交给 SQLAlchemy 各方言的 identifier_preparer：需要时才加引号（memberTags 加、visits 不加），
    MySQL 用反引号。Oracle 尤其不能一律加双引号：探查拿到的表名是 SQLAlchemy 规整过的小写，库里其实是
    大写，"visits" 加了引号就成了另一张不存在的表；不加引号 Oracle 自己转大写才对得上。
    """

    def __init__(self, kind: str | None) -> None:
        self.kind = (kind or "").lower()
        module = _DIALECT_MODULES.get(self.kind)
        if module is None:
            raise ValueError(f"不支持的数据库类型：{kind}")
        self._dialect = module.dialect()
        self._prep = self._dialect.identifier_preparer
        self.oracle = self.kind == "oracle"

    def quote(self, name: str) -> str:
        return self._prep.quote(name)

    def table(self, meta: dict[str, Any] | None, name: str) -> str:
        """写进 FROM 的表名：探查时指定了 schema 的带上它（Oracle 只读账号名下没有对象，全靠 schema 前缀）。"""
        schema = (meta or {}).get("schema")
        return f"{self._prep.quote_schema(schema)}.{self.quote(name)}" if schema else self.quote(name)

    def limit(self, sql: str, n: int) -> str:
        """只取前 n 行。Oracle 11g 没有 LIMIT 也没有 FETCH FIRST，一律套一层 ROWNUM。"""
        if self.oracle:
            return f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(n)}"
        return f"{sql} LIMIT {int(n)}"

    def _first_rows(self, table: str, column: str, scan_cap: int | None) -> str:
        """FROM 子句：整张表，或者只看前 scan_cap 行非空值的子查询（大表不做整表扫描）。"""
        if scan_cap is None:
            return table
        return f"({self.limit(f'SELECT {column} FROM {table} WHERE {column} IS NOT NULL', scan_cap)}) s"

    def stats_sql(self, meta: dict[str, Any] | None, name: str) -> str | None:
        """读这张表行数估算值的查询：MySQL information_schema.TABLES.TABLE_ROWS、PostgreSQL pg_class.reltuples、
        Oracle ALL_TABLES.NUM_ROWS。SQLite 没有可靠的统计信息，返回 None（改为数到上限为止）。"""
        schema = (meta or {}).get("schema")
        if self.kind in ("mysql", "mariadb"):
            where_schema = sql_literal(schema, "text") if schema else "DATABASE()"
            table = sql_literal(name, "text")
            if not (where_schema and table):
                return None
            return (f"SELECT TABLE_ROWS FROM information_schema.TABLES "
                    f"WHERE TABLE_SCHEMA = {where_schema} AND TABLE_NAME = {table}")
        if self.kind in ("postgres", "postgresql"):
            where_schema = sql_literal(schema, "text") if schema else "current_schema()"
            table = sql_literal(name, "text")
            if not (where_schema and table):
                return None
            return ("SELECT c.reltuples FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE n.nspname = {where_schema} AND c.relname = {table}")
        if self.oracle:
            denorm = self._dialect.denormalize_name
            owner = sql_literal(denorm(schema), "text") if schema else "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
            table = sql_literal(denorm(name), "text")
            if not (owner and table):
                return None
            return f"SELECT NUM_ROWS FROM ALL_TABLES WHERE OWNER = {owner} AND TABLE_NAME = {table}"
        return None

    def bounded_count_sql(self, table: str, cap: int) -> str:
        """数到 cap 行为止：小表得到准确行数，大表只知道「至少 cap 行」，不做全表 COUNT。"""
        return f"SELECT COUNT(*) AS n FROM ({self.limit(f'SELECT 1 AS one FROM {table}', cap)}) s"

    def distinct_sample_sql(self, table: str, column: str, n: int, *, scan_cap: int | None) -> str:
        """至多 n 个不同的非空键值。scan_cap 给了时只在前 scan_cap 行里找：键的取值少时 DISTINCT … LIMIT
        凑不满 n 个，会一直扫到表尾。"""
        if scan_cap is None:
            return self.limit(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL", n)
        return self.limit(f"SELECT DISTINCT {column} FROM {self._first_rows(table, column, scan_cap)}", n)

    def match_count_sql(self, table: str, column: str, literals: list[str]) -> str:
        """被指向表里能对上这些键值的个数（去重：被指向列有重复值时不多算）。"""
        return f"SELECT COUNT(DISTINCT {column}) AS n FROM {table} WHERE {column} IN ({', '.join(literals)})"

    def unique_check_sql(self, table: str, column: str) -> str:
        """非空值个数和不同值个数：相等即这一列（非空部分）唯一。整表统计，只对小表用。"""
        return f"SELECT COUNT({column}) AS n, COUNT(DISTINCT {column}) AS d FROM {table}"

    def value_counts_sql(self, table: str, column: str, n: int, *, scan_cap: int | None) -> str:
        """取值分布：出现最多的 n 个取值和各自的行数。只拿取值和计数，不拿明细行。"""
        sql = (f"SELECT {column}, COUNT(*) AS cnt FROM {self._first_rows(table, column, scan_cap)} "
               f"WHERE {column} IS NOT NULL GROUP BY {column} ORDER BY cnt DESC, {column}")
        return self.limit(sql, n)

    def min_max_sql(self, table: str, columns: list[str]) -> str:
        return f"SELECT {', '.join(f'MIN({c}), MAX({c})' for c in columns)} FROM {table}"


# ==========================================================================
# 发查询：守卫、查询次数、时限
# ==========================================================================

#: 总时长只剩这么一点时不再发新查询：发出去的时限也只剩零点几秒，白占一次查询次数
_MIN_WINDOW_S = 0.5
#: 连续失败这么多条就停：多半是连接断了、账号没有权限，再发只是一条条重复同一个错误
_MAX_CONSECUTIVE_ERRORS = 3


class _Stop(Exception):
    """整次剖析停下：budget 查询次数用完、deadline 总时长用完、failed 连续失败。剩下的检查都不再发查询。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Skip(Exception):
    """这一项没查成或不查：timeout 超时、error 查询失败、rejected 未通过守卫、masked 被遮罩……记下原因，接着查别的。

    carry：目录里剖析以前对这一项写的结论要不要原样留着。没查成的（超时、表太大……）留着——这次没查不等于
    结论错了；被遮罩的列上取过值的结论（码值、日期范围）不留：遮罩之后，带着原值的结论也不该再给人看。
    """

    def __init__(self, reason: str, detail: str, *, carry: bool = True) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.carry = carry


def _brief(e: BaseException) -> str:
    """驱动报错的第一行，去掉 SQLAlchemy 加的「(模块.类名)」前缀和后面附的 SQL、文档链接。"""
    lines = str(getattr(e, "orig", None) or e).strip().splitlines()
    first = re.sub(r"^\([\w.]+\)\s*", "", lines[0] if lines else "")
    return first[:160] or type(e).__name__


class _Runner:
    """剖析发的每一条查询都从这里走：先按只读过守卫，再交给查询层 run_query；记次数、管时限。

    - 只读：不论数据源本身可写与否，先用 guard.check(readonly=True) 判一遍，再进 run_query——那里还会按
      数据源自己的设置判一遍。两遍是同一个守卫，不为剖析放宽，也不另开连接。
    - 查询次数：每发一条记一次（超时、失败的也算），到上限就停。没发出去的（守卫拒了）不算。
    - 时限：单条取设置和数据源查询时限中较小的一个，并且不超过总时长剩下的部分。
    """

    def __init__(self, source: Any, settings: ProfileSettings, dialect: SqlDialect) -> None:
        self.source = source
        self.settings = settings
        self.kind = dialect.kind
        self.timeout = min(float(settings.query_timeout_s), float(data_engine.query_timeout(source)))
        self.ends = time.monotonic() + float(settings.max_total_s)
        self.used = 0
        self.stopped: str | None = None
        self._errors = 0

    def _stop(self, reason: str) -> None:
        self.stopped = reason
        raise _Stop(reason)

    async def __call__(self, sql: str, *, max_rows: int = 1) -> QueryResult:
        if self.stopped:
            raise _Stop(self.stopped)
        if self.used >= self.settings.max_queries:
            self._stop("budget")
        remaining = self.ends - time.monotonic()
        if remaining <= _MIN_WINDOW_S:
            self._stop("deadline")
        try:
            guard.check(sql, readonly=True, source_name=getattr(self.source, "name", ""), dialect=self.kind)
        except SqlRejected as e:
            raise _Skip("rejected", f"剖析查询未通过安全守卫：{e}") from e
        self.used += 1
        cut = remaining < self.timeout
        seconds = round(min(self.timeout, remaining), 3)
        limits = QueryLimits(max_rows=max(1, int(max_rows)), timeout_seconds=seconds)
        try:
            result = await data_engine.run_query(self.source, sql, limits=limits)
        except SqlRejected as e:
            # 上面已经按只读过了同一个守卫，查询层再拒只可能是时限到了
            if cut:
                self.stopped = "deadline"
            raise _Skip("timeout", f"查询超过 {_fmt(seconds)} 秒被中断，已跳过") from e
        except SnapshotTampered:
            raise
        except Exception as e:  # noqa: BLE001 - 一条查询失败不该拖垮整次剖析，原因照实记下
            self._errors += 1
            if self._errors >= _MAX_CONSECUTIVE_ERRORS:
                self.stopped = "failed"
            raise _Skip("error", f"查询失败：{_brief(e)}") from e
        self._errors = 0
        return result


# ==========================================================================
# 表有多大
# ==========================================================================


@dataclass(frozen=True)
class TableSize:
    """一张表的行数。rows 为 None 表示不知道（没有统计信息、也没数）或者超过了整表统计的上限。"""

    rows: int | None
    #: stats 数据库的统计信息（估算值）；count 数到上限为止；unknown 没能得到
    method: str
    #: 数到上限也没数完：至少这么多行
    at_least: int | None = None

    def within(self, limit: int) -> bool:
        """行数已知且不超过 limit：可以做整表统计。不知道多大的表按大表对待。"""
        return self.rows is not None and self.rows <= limit

    def to_dict(self) -> dict[str, Any]:
        return {"rows": self.rows, "method": self.method, "at_least": self.at_least}


def _first(result: QueryResult) -> Any:
    return result.rows[0][0] if result.rows and result.rows[0] else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except ValueError:
        return None
    return int(number) if number == number and abs(number) != float("inf") else None


async def estimate_size(run: _Runner, dialect: SqlDialect, meta: dict[str, Any], name: str, *,
                        max_scan_rows: int) -> tuple[TableSize, list[_Skip]]:
    """先读统计信息；读不到（SQLite、没分析过的表）再数到 max_scan_rows + 1 行为止。

    统计信息为 0 或负数（PostgreSQL 没分析过的表是 -1，MySQL 的估算值可能是 0）当作不知道：一张其实很大的表
    要是被当成空表，后面就会对它做整表统计。返回 (行数, 途中没查成的几条)。查询次数、总时长用完时抛 _Stop。
    """
    skips: list[_Skip] = []
    sql = dialect.stats_sql(meta, name)
    if sql:
        try:
            rows = _as_int(_first(await run(sql)))
        except _Skip as e:
            skips.append(e)
        else:
            if rows is not None and rows > 0:
                return TableSize(rows, "stats"), skips
    if max_scan_rows <= 0:
        return TableSize(None, "unknown"), skips
    cap = max_scan_rows + 1
    try:
        counted = _as_int(_first(await run(dialect.bounded_count_sql(dialect.table(meta, name), cap))))
    except _Skip as e:
        skips.append(e)
        return TableSize(None, "unknown"), skips
    if counted is None:
        return TableSize(None, "unknown"), skips
    if counted <= max_scan_rows:
        return TableSize(counted, "count"), skips
    return TableSize(None, "count", at_least=cap), skips


# ==========================================================================
# 一次剖析：上下文、结果
# ==========================================================================

#: 不指定表时剖析几张：按使用次数取目录里有待核实关系的表
PROFILE_DEFAULT_TABLES = 10
#: IN 列表每批最多几个值：Oracle 的 IN 列表不能超过 1000 项（ORA-01795），各家统一按它分批
IN_CHUNK = 1000
#: 剖析写的备注都以它开头：人工确认过的关系，只覆盖剖析自己写的备注，不碰人写的
NOTE_PREFIX = "数据剖析（"
#: 整次剖析停下的原因 → 剩下没查的项的说明
_STOP_DETAIL = {
    "budget": "本次剖析的查询次数已用完（上限 {max_queries} 条），未检查",
    "deadline": "本次剖析的总时长已用完（上限 {max_total_s} 秒），未检查",
    "failed": "连续多条查询失败，已停止剖析，未检查",
}


class ProfileDisabled(Exception):
    """这个数据源没有开启数据剖析。message 给人看。"""


class ProfileBusy(Exception):
    """这个数据源正在剖析。message 给人看。"""


@dataclass
class ProfileSkip:
    """没做的一项和原因。

    kind：relation 关系、codes 码值、date 日期列、row_estimate 行数估算、table 整张表。
    reason：budget 查询次数用完、deadline 总时长用完、failed 连续失败、timeout 超时、error 查询失败、
    rejected 未通过守卫、masked 字段被遮罩、too_large 表太大、view 视图、unsupported 暂不支持、
    no_data 没有数据、missing 表结构里没有、high_cardinality 取值太多（不像码值）。detail 是给人看的整句。
    """

    kind: str
    target: str
    reason: str
    detail: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "path": self.path, "reason": self.reason,
                "detail": self.detail}


@dataclass
class TableProfile:
    """一张表这次剖析的结果。findings 是写进目录的发现（给界面看，形状见 _finding_*）。"""

    table: str
    queries: int = 0
    row_estimate: TableSize | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[ProfileSkip] = field(default_factory=list)
    #: 日期类列的取值范围（MIN、MAX）。只有一个日期列时它还写进了业务日期的备注；多个时只在这里
    date_ranges: list[dict[str, Any]] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    removed: int = 0
    version: int = 0
    #: 这张表没剖析（表结构里没有）或结果没写进去（写入一直冲突）
    error: str | None = None


@dataclass
class ProfileReport:
    """一次剖析的结果。stopped：整次剖析中途停下的原因（budget / deadline / failed），做完了为 None。"""

    profiled_at: str
    settings: ProfileSettings
    tables: list[TableProfile] = field(default_factory=list)
    queries_used: int = 0
    stopped: str | None = None


@dataclass
class _RelationOutcome:
    """一条关系核对完的结论。写不写、怎么写由 _apply 按写入时目录里那条关系的状态决定。"""

    rid: str
    target: str
    coverage: float
    cardinality: str | None
    holds: bool
    sample: int
    matched: int
    note: str


@dataclass
class _CodesOutcome:
    """一列的取值分布：[(码值, 行数)]，按行数从多到少。"""

    column: str
    values: list[tuple[str, int]]
    rows: int
    note: str


@dataclass
class _DateOutcome:
    """表里唯一一个日期类列，提议作为业务日期。"""

    column: str
    low: str
    high: str
    note: str


@dataclass
class _TableOutcome:
    """一张表所有检查的结论，以及没查成、要原样保留旧结论的槽位。"""

    relations: list[_RelationOutcome] = field(default_factory=list)
    codes: list[_CodesOutcome] = field(default_factory=list)
    business_date: _DateOutcome | None = None
    #: 没查成的项：目录里剖析以前写的结论原样留着（不然会被 covered={"profile"} 当成「这次没有」删掉）
    keep_relations: set[str] = field(default_factory=set)
    keep_codes: set[str] = field(default_factory=set)
    keep_business_date: bool = False


@dataclass
class _Context:
    source: Any
    settings: ProfileSettings
    dialect: SqlDialect
    run: _Runner
    tables: dict[str, Any]
    #: 遮罩的列（小写）。遮罩按列名生效、不分大小写、不分表，和证据面板的比法一致
    masked: set[str]
    #: 写进备注的剖析日期（UTC）
    day: str
    sizes: dict[str, TableSize] = field(default_factory=dict)

    async def size(self, table: str, result: TableProfile) -> TableSize:
        """表有多大，一次剖析里每张表只估一次。途中没查成的记进 result（行数估算这一项）。"""
        if table not in self.sizes:
            size, skips = await estimate_size(self.run, self.dialect, self.tables[table], table,
                                              max_scan_rows=self.settings.max_scan_rows)
            self.sizes[table] = size
            result.skipped += [ProfileSkip("row_estimate", table, s.reason, s.detail) for s in skips]
        return self.sizes[table]

    async def size_with_data(self, table: str, result: TableProfile) -> TableSize:
        """同 size，但确知是空表时直接跳过这一项（抛 _Skip），不再为它发查询。"""
        size = await self.size(table, result)
        if size.rows == 0:
            raise _Skip("no_data", f"{table} 是空表，无法剖析")
        return size

    def is_masked(self, column: str) -> bool:
        return column.lower() in self.masked

    def scan_cap(self, size: TableSize) -> int | None:
        """大表（或不知道多大）抽样、取值分布只看前多少行；小表整表看，返回 None。"""
        if size.within(self.settings.max_scan_rows):
            return None
        return max(self.settings.sample_size, self.settings.max_scan_rows)


def _column(meta: dict[str, Any], name: str) -> str | None:
    """表结构里这一列的写法（目录里的列名大小写可能和表结构不同）；没有返回 None。"""
    lowered = name.lower()
    return next((str(c["name"]) for c in meta.get("columns") or []
                 if isinstance(c, dict) and str(c.get("name", "")).lower() == lowered), None)


def _constraint(meta: dict[str, Any], column: str) -> str | None:
    """这一列单独是主键或某个唯一约束时返回「主键」/「唯一约束」，否则 None。"""
    wanted = [column.lower()]
    if [c.lower() for c in meta.get("primary_key") or []] == wanted:
        return "主键"
    for group in meta.get("unique") or []:
        if isinstance(group, list) and [str(c).lower() for c in group] == wanted:
            return "唯一约束"
    return None


def _percent(ratio: float) -> str:
    """覆盖率写成百分数：整数不带小数，其余留一位；不到 100% 的不四舍五入成 100%。"""
    text = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
    return "99.9%" if text == "100" and ratio < 1 else f"{text}%"


def _relation_target(rel: dict[str, Any]) -> str:
    left = ", ".join(rel.get("columns") or [])
    right = ", ".join(f"{rel.get('to_table')}.{c}" for c in rel.get("to_columns") or [])
    return f"{left} → {right}"


# ==========================================================================
# 检查：关联关系
# ==========================================================================


def _relation_candidates(notes: dict[str, Any] | None) -> list[dict[str, Any]]:
    """要核对的关系：没被驳回、不是外键约束来的（外键由数据库保证，剖析也顶不掉它）。人工确认过的也核对，
    只补覆盖率和基数。"""
    return [r for r in (notes or {}).get("relations") or []
            if isinstance(r, dict) and r.get("id") and r.get("status") != "rejected" and r.get("source") != "fk"]


async def _unique(cx: _Context, table: str, meta: dict[str, Any], column: str,
                  result: TableProfile) -> tuple[bool | None, str]:
    """这一列（非空部分）唯一吗：(True / False / None 不知道, 依据)。有主键或唯一约束直接认；否则只对行数不超过
    整表统计上限的表数一次 COUNT 和 COUNT(DISTINCT)，大表不扫。"""
    how = _constraint(meta, column)
    if how:
        return True, how
    size = await cx.size(table, result)
    if not size.within(cx.settings.max_scan_rows):
        return None, "too_large"
    d = cx.dialect
    res = await cx.run(d.unique_check_sql(d.table(meta, table), d.quote(column)))
    row = res.rows[0] if res.rows else [None, None]
    total, distinct = _as_int(row[0]), _as_int(row[1])
    if total is None or distinct is None:
        return None, "count"
    return total == distinct, "count"


def _relation_note(day: str, *, sample: int, matched: int, coverage: float, scan_cap: int | None,
                   skipped_values: int, parent: tuple[bool | None, str], child: tuple[bool | None, str] | None,
                   holds: bool) -> str:
    """关系的剖析备注：日期、抽样规模、覆盖率、父键和子键唯一的依据。人据此判断这条结论有多可靠。"""
    parts: list[str] = []
    low = coverage < catalog.PROFILE_VERIFY_COVERAGE
    if low:
        parts.append(f"抽样覆盖率 {_percent(coverage)}，可能不是这条关系")
    scope = f"（只看前 {scan_cap} 行）" if scan_cap else ""
    sampled = f"子表抽样 {sample} 个不同键值{scope}，父表对上 {matched} 个"
    parts.append(sampled if low else f"{sampled}，覆盖率 {_percent(coverage)}")
    if skipped_values:
        parts.append(f"另有 {skipped_values} 个键值的类型不支持核对，未计入")
    unique, how = parent
    if unique is True:
        parts.append(f"被指向列是{how}" if how in ("主键", "唯一约束") else "被指向列经计数核对唯一")
    elif unique is False:
        parts.append("被指向列有重复值，不能作为关联的被指向键")
    else:
        parts.append("被指向的表超过整表统计的行数上限，未核对被指向列是否唯一")
    if child is not None:
        child_unique, child_how = child
        if child_unique is True:
            basis = child_how if child_how in ("主键", "唯一约束") else "计数核对"
            parts.append(f"子表一侧也唯一（{basis}），一对一")
        elif child_unique is False:
            parts.append("子表一侧有重复值，多对一")
        else:
            parts.append("子表超过整表统计的行数上限，未核对子表一侧是否唯一，按多对一记")
    if not holds and not low:
        parts.append("暂不升为有确证")
    return f"{NOTE_PREFIX}{day}）：" + "；".join(parts) + "。"


async def _check_relation(cx: _Context, table: str, rel: dict[str, Any], out: _TableOutcome,
                          result: TableProfile) -> None:
    """从子表取至多 sample_size 个不同的非空键值，去父表里数对上几个，得到覆盖率；再看父键、子键唯不唯一。"""
    cols, to_table, to_cols = list(rel.get("columns") or []), str(rel.get("to_table") or ""), \
        list(rel.get("to_columns") or [])
    if len(cols) != 1 or len(to_cols) != 1:
        raise _Skip("unsupported", "多列组成的关联关系暂不剖析")
    meta = cx.tables[table]
    parent = to_table if to_table in cx.tables else catalog.resolve_table_name({"tables": cx.tables}, to_table)
    if parent is None:
        raise _Skip("missing", f"表结构里没有被指向的表 {to_table}，请先重新探查结构")
    pmeta = cx.tables[parent]
    col, pcol = _column(meta, cols[0]), _column(pmeta, to_cols[0])
    if col is None or pcol is None:
        missing = f"{table}.{cols[0]}" if col is None else f"{parent}.{to_cols[0]}"
        raise _Skip("missing", f"表结构里没有列 {missing}，请先重新探查结构")
    if pmeta.get("is_view"):
        raise _Skip("view", f"被指向的 {parent} 是视图，不剖析")
    for name, owner in ((col, table), (pcol, parent)):
        if cx.is_masked(name):
            raise _Skip("masked", f"{owner}.{name} 在数据源设置中被遮罩，不取样", carry=True)

    d, s = cx.dialect, cx.settings
    size = await cx.size_with_data(table, result)
    scan_cap = cx.scan_cap(size)
    child_table, parent_table = d.table(meta, table), d.table(pmeta, parent)
    res = await cx.run(d.distinct_sample_sql(child_table, d.quote(col), s.sample_size, scan_cap=scan_cap),
                       max_rows=s.sample_size)
    kind = (res.column_types or {}).get(res.columns[0]) if res.columns else None
    values = [row[0] for row in res.rows if row]
    literals = list(dict.fromkeys(lit for v in values if (lit := sql_literal(v, kind)) is not None))
    if not values:
        raise _Skip("no_data", f"{table}.{col} 没有非空值，无法核对")
    if not literals:
        raise _Skip("unsupported", f"{table}.{col} 的取值类型不支持核对（只核对整数和文字）")
    matched = 0
    for start in range(0, len(literals), IN_CHUNK):
        sql = d.match_count_sql(parent_table, d.quote(pcol), literals[start:start + IN_CHUNK])
        matched += _as_int(_first(await cx.run(sql))) or 0
    matched = min(matched, len(literals))
    coverage = round(matched / len(literals), 4)

    parent_unique = await _unique(cx, parent, pmeta, pcol, result)
    child_unique = await _unique(cx, table, meta, col, result) if parent_unique[0] else None
    cardinality = None
    if child_unique is not None:
        cardinality = "one_to_one" if child_unique[0] is True else "many_to_one"
    holds = coverage >= catalog.PROFILE_VERIFY_COVERAGE and cardinality is not None
    note = _relation_note(cx.day, sample=len(literals), matched=matched, coverage=coverage, scan_cap=scan_cap,
                          skipped_values=len(values) - len(literals), parent=parent_unique, child=child_unique,
                          holds=holds)
    out.relations.append(_RelationOutcome(rid=str(rel["id"]), target=_relation_target(rel), coverage=coverage,
                                          cardinality=cardinality, holds=holds, sample=len(literals),
                                          matched=matched, note=note))


# ==========================================================================
# 检查：码值候选、日期列
# ==========================================================================

#: 码值候选最多几个取值。查询取 CODES_MAX + 1 个：取满了说明不是低基数列
CODES_MAX = 20
#: 码值的长度上限。更长的不像状态码，像说明文字
MAX_CODE_LEN = 64
#: 像状态、类型、渠道的列名里会有的词
_CODE_WORDS = frozenset({"status", "state", "type", "kind", "category", "channel", "level", "method", "mode",
                         "reason", "stage", "phase", "grade", "flag", "source", "result"})
#: 以这些词结尾的列不是码值：标识、名称、说明、时间、金额、数量（channel_id、level_name、status_time……）
_NOT_CODE_TAILS = frozenset({"id", "no", "num", "number", "name", "desc", "description", "text", "remark", "note",
                             "at", "date", "time", "on", "amount", "price", "count", "qty", "rate", "url"})
#: 文字类型的列名以这些词结尾时，按日期类列看待（SQLite 里日期都存成文字：visit_time、ordered_at、joined_on）
_DATE_TAILS = frozenset({"at", "date", "time", "on", "dt", "day"})
#: 像日期的取值：2026-07-01、2026/07/01，后面可以跟时间
_DATE_TEXT = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _words(name: str) -> list[str]:
    """列名切成小写的词：下划线和驼峰都是分隔（和目录的命名推断同一个切法）。"""
    return [w.lower() for part in name.split("_") for w in _WORDS.findall(part)]


def _code_type_ok(type_name: str | None) -> bool:
    """整数，或者短文字（定长不超过 MAX_CODE_LEN；SQLite 的 TEXT 不带长度，也算）。"""
    t = (type_name or "").upper()
    if not t or any(k in t for k in ("CLOB", "BLOB", "BINARY", "JSON", "DATE", "TIME", "INTERVAL", "REAL", "FLOAT",
                                     "DOUBLE", "BOOL")):
        return False
    if "INT" in t:
        return True
    size = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", t)
    if any(k in t for k in ("NUMBER", "NUMERIC", "DECIMAL")):
        return size is None or not size.group(2) or int(size.group(2)) == 0
    if any(k in t for k in ("CHAR", "TEXT", "STRING")):
        return size is None or int(size.group(1)) <= MAX_CODE_LEN
    return False


def _looks_like_code(name: str) -> bool:
    words = _words(name)
    return bool(words) and words[-1] not in _NOT_CODE_TAILS and any(w in _CODE_WORDS for w in words)


def _code_columns(meta: dict[str, Any], notes: dict[str, Any]) -> list[str]:
    """要取值分布的列：目录里度量类型记为状态的、剖析以前提过码值候选的（再核一次）、或者类型和命名像状态、
    类型、渠道的整数或短文字列。主键、唯一约束列、关系里的键列不算；码值人工确认或驳回过的不再取。
    被遮罩的列也列出来，由检查记为「被遮罩」跳过——要让人看得见为什么它没有码值。"""
    pk = [c.lower() for c in meta.get("primary_key") or []]
    unique = {str(g[0]).lower() for g in meta.get("unique") or [] if isinstance(g, list) and len(g) == 1}
    keys = {str(c).lower() for r in notes.get("relations") or [] if isinstance(r, dict) for c in r.get("columns") or []}
    described = notes.get("columns") if isinstance(notes.get("columns"), dict) else {}
    out: list[str] = []
    for col in meta.get("columns") or []:
        name = str(col.get("name") or "")
        items = described.get(name) if isinstance(described.get(name), dict) else {}
        codes, measure = items.get("codes"), items.get("measure")
        if isinstance(codes, dict) and codes.get("status") in ("confirmed", "rejected"):
            continue
        status_measure = (isinstance(measure, dict) and measure.get("value") == "status"
                          and measure.get("status") != "rejected")
        profiled = isinstance(codes, dict) and codes.get("source") == "profile"
        if not (status_measure or profiled or _looks_like_code(name)):
            continue
        lowered = name.lower()
        if (pk == [lowered]) or lowered in unique or lowered in keys or not _code_type_ok(col.get("type")):
            continue
        out.append(name)
    return out


def _code_key(value: Any) -> str | None:
    """取值分布里的一个取值 → 码值（文字）。只认整数和不太长的文字。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else None
    if isinstance(value, str) and value.strip() and len(value) <= MAX_CODE_LEN and not _CONTROL.search(value):
        return value
    return None


def _share(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    ratio = count / total
    return "不足 1%" if 0 < ratio < 0.01 else f"{round(ratio * 100)}%"


async def _check_codes(cx: _Context, table: str, column: str, out: _TableOutcome, result: TableProfile) -> None:
    """GROUP BY 取出现最多的 CODES_MAX + 1 个取值：不超过 CODES_MAX 个就是码值候选（值有了，含义等人填）。"""
    if cx.is_masked(column):
        raise _Skip("masked", f"{table}.{column} 在数据源设置中被遮罩，不取值", carry=False)
    d, meta = cx.dialect, cx.tables[table]
    size = await cx.size_with_data(table, result)
    scan_cap = cx.scan_cap(size)
    res = await cx.run(d.value_counts_sql(d.table(meta, table), d.quote(column), CODES_MAX + 1, scan_cap=scan_cap),
                       max_rows=CODES_MAX + 1)
    if not res.rows:
        raise _Skip("no_data", f"{table}.{column} 没有非空值，无法取值分布")
    if len(res.rows) > CODES_MAX:
        raise _Skip("high_cardinality", f"{table}.{column} 的取值超过 {CODES_MAX} 个，不作为码值候选", carry=False)
    values: list[tuple[str, int]] = []
    for row in res.rows:
        key = _code_key(row[0])
        if key is None:
            raise _Skip("unsupported", f"{table}.{column} 的取值不是整数或短文字，不作为码值候选", carry=False)
        values.append((key, _as_int(row[1]) or 0))
    if len(values) >= 3 and all(n == 1 for _, n in values):
        # 每个取值只出现一次（票种编码 T01…T09）：这是一列标识，不是状态码
        raise _Skip("high_cardinality", f"{table}.{column} 的取值各不相同，像标识而不是码值，不作为码值候选",
                    carry=False)
    rows = sum(n for _, n in values)
    scope = f"按前 {scan_cap} 行统计" if scan_cap else "统计全表"
    spread = "、".join(f"{k}（{_share(n, rows)}）" for k, n in values)
    note = f"{NOTE_PREFIX}{cx.day}）：{scope}，{rows} 行非空值共 {len(values)} 个取值：{spread}。"
    out.codes.append(_CodesOutcome(column=column, values=values, rows=rows, note=note))


def _is_date_column(col: dict[str, Any]) -> bool:
    """日期类列：类型是日期、时间戳；或者类型是文字、列名像日期（SQLite 的日期都存成文字）。"""
    t = str(col.get("type") or "").upper()
    if "DATE" in t or "TIMESTAMP" in t:
        return True
    if t and not any(k in t for k in ("CHAR", "TEXT", "STRING")):
        return False
    words = _words(str(col.get("name") or ""))
    return bool(words) and words[-1] in _DATE_TAILS and (len(words) > 1 or words[0] in ("date", "time", "day"))


async def _check_dates(cx: _Context, table: str, columns: list[str], total: int, out: _TableOutcome,
                       result: TableProfile) -> None:
    """日期类列的 MIN、MAX，作为业务日期的参考。整表统计，只对行数不超过上限的表做。表里只有一个日期类列
    （total 是表结构里日期类列的个数，含被遮罩的）时提议它作为业务日期。"""
    d, meta = cx.dialect, cx.tables[table]
    size = await cx.size_with_data(table, result)
    if not size.within(cx.settings.max_scan_rows):
        raise _Skip("too_large", f"{table} 的行数超过整表统计的行数上限（{cx.settings.max_scan_rows} 行），"
                                 "不取日期列的最小值和最大值")
    res = await cx.run(d.min_max_sql(d.table(meta, table), [d.quote(c) for c in columns]))
    row = res.rows[0] if res.rows else []
    for i, column in enumerate(columns):
        low, high = (row[2 * i], row[2 * i + 1]) if len(row) >= 2 * i + 2 else (None, None)
        if low is None or high is None or not (_DATE_TEXT.match(str(low)) and _DATE_TEXT.match(str(high))):
            continue
        result.date_ranges.append({"column": column, "min": str(low), "max": str(high)})
    if total != 1:
        return
    if not result.date_ranges:
        raise _Skip("no_data", f"{table}.{columns[0]} 没有可识别的日期值")
    found = result.date_ranges[0]
    note = (f"{NOTE_PREFIX}{cx.day}）：表里只有这一个日期类列，取值从 {str(found['min'])[:19]} 到 "
            f"{str(found['max'])[:19]}，可作为业务日期的参考。")
    out.business_date = _DateOutcome(column=found["column"], low=found["min"], high=found["max"], note=note)


# ==========================================================================
# 结论进目录
# ==========================================================================


def _relation_finding(o: _RelationOutcome, rel: dict[str, Any], *, status: str, confirmed: bool) -> dict[str, Any]:
    if confirmed:
        summary = "已人工确认，只补充覆盖率和基数"
    elif status == "verified":
        summary = f"覆盖率 {_percent(o.coverage)}，升为有确证"
    elif o.coverage < catalog.PROFILE_VERIFY_COVERAGE:
        summary = f"抽样覆盖率 {_percent(o.coverage)}，保持推断"
    else:
        summary = "被指向列未核实唯一，保持推断"
    return {"kind": "relation", "path": f"relations.{o.rid}", "target": o.target,
            "columns": list(rel.get("columns") or []), "to_table": rel.get("to_table"),
            "to_columns": list(rel.get("to_columns") or []), "status": status, "confirmed": confirmed,
            "coverage": o.coverage, "cardinality": o.cardinality, "sample": o.sample, "matched": o.matched,
            "summary": summary}


def _codes_value(c: _CodesOutcome, item: dict[str, Any] | None) -> dict[str, str]:
    """码值候选的值：观察到的取值，含义先空着。原来有别的来源（模型起草、数据库注释）写过含义的，对得上的取值
    沿用它的含义；它写了、数据里没出现的取值也留着——码值是「可能的取值」，没出现不等于不存在。"""
    old = item.get("value") if isinstance(item, dict) and isinstance(item.get("value"), dict) else {}
    value = {key: str(old.get(key) or "") for key, _ in c.values}
    for key, meaning in old.items():
        if key not in value and isinstance(meaning, str) and meaning.strip():
            value[key] = meaning
    return value


def _codes_finding(c: _CodesOutcome, value: dict[str, str]) -> dict[str, Any]:
    pending = sum(1 for v in value.values() if not v.strip())
    summary = f"{len(c.values)} 个取值" + (f"，{pending} 个含义待填写" if pending else "")
    return {"kind": "codes", "path": f"columns.{c.column}.codes", "column": c.column,
            "values": [{"value": k, "rows": n} for k, n in c.values], "rows": c.rows, "status": "proposed",
            "summary": summary}


def _patch_confirmed(rel: dict[str, Any], o: _RelationOutcome, at: str) -> bool:
    """人工确认过的关系只补覆盖率和基数：覆盖率是测量值，照新的写；基数只在原来没有时补，不改人定的；
    备注只在原来为空、或者是剖析自己写的时候换成新的。状态、来源不动。返回改没改。"""
    changed = False
    if rel.get("coverage") != o.coverage:
        rel["coverage"] = o.coverage
        changed = True
    if rel.get("cardinality") is None and o.cardinality is not None:
        rel["cardinality"] = o.cardinality
        changed = True
    note = rel.get("note")
    if (not note or str(note).startswith(NOTE_PREFIX)) and note != o.note:
        rel["note"] = o.note
        changed = True
    if changed:
        rel["updated_at"] = at
    return changed


def _apply(existing: dict[str, Any] | None, out: _TableOutcome, *,
           at: str) -> tuple[dict[str, Any], list[dict[str, Any]], catalog.MergeStats]:
    """把一张表的结论并进写入时的目录：(新目录, 写进去的发现, 计数)。纯函数，写入冲突重来时再调一次。

    - 没被确认、驳回的关系：记为 source=profile，覆盖率够、父键唯一的 verified，否则 proposed；按 merge_notes
      的规则并入（剖析顶得掉命名推断、模型起草，顶不掉外键和人工）。
    - 人工确认过的关系：只补覆盖率和基数（_patch_confirmed）。
    - 码值候选：profile / proposed，含义沿用别的来源写过的（_codes_value）；人工确认、驳回过的码值不碰。
    - 业务日期：只有一个日期类列时提议，profile / proposed；已有别的来源定的业务日期不顶掉。
    - covered={"profile"}：这次完整检查过、却没再得出的剖析结论删掉（比如取值变多、不再像码值的列，列被遮罩
      之后它的码值）；没查成的检查（超时、预算用完……）把剖析以前写的结论原样带上，不因为这次没查成就删。
    """
    existing = existing or {}
    current = {str(r["id"]): r for r in existing.get("relations") or [] if isinstance(r, dict) and r.get("id")}
    relations: list[dict[str, Any]] = []
    confirmed: dict[str, _RelationOutcome] = {}
    findings: list[dict[str, Any]] = []
    for o in out.relations:
        rel = current.get(o.rid)
        if rel is None or rel.get("status") == "rejected" or rel.get("source") == "fk":
            continue                     # 核对期间被删、被驳回了：不写
        if rel.get("status") == "confirmed":
            confirmed[o.rid] = o
            findings.append(_relation_finding(o, rel, status="confirmed", confirmed=True))
            continue
        status = "verified" if o.holds else "proposed"
        relations.append({"id": o.rid, "columns": list(rel["columns"]), "to_table": rel["to_table"],
                          "to_columns": list(rel["to_columns"]), "cardinality": o.cardinality,
                          "coverage": o.coverage, "source": "profile", "status": status, "note": o.note})
        findings.append(_relation_finding(o, rel, status=status, confirmed=False))
    for rid in out.keep_relations:
        rel = current.get(rid)
        if rel is not None and rel.get("source") == "profile":
            relations.append(copy.deepcopy(rel))
    draft: dict[str, Any] = {"relations": relations} if relations else {}

    described = existing.get("columns") if isinstance(existing.get("columns"), dict) else {}
    columns: dict[str, dict[str, Any]] = {}
    for c in out.codes:
        item = (described.get(c.column) or {}).get("codes") if isinstance(described.get(c.column), dict) else None
        if isinstance(item, dict) and item.get("status") in ("confirmed", "rejected"):
            continue
        columns[c.column] = {"codes": catalog.make_item(_codes_value(c, item), "profile", "proposed", note=c.note)}
        findings.append(_codes_finding(c, columns[c.column]["codes"]["value"]))
    for column in out.keep_codes:
        item = (described.get(column) or {}).get("codes") if isinstance(described.get(column), dict) else None
        if isinstance(item, dict) and item.get("source") == "profile":
            columns[column] = {"codes": copy.deepcopy(item)}
    if columns:
        draft["columns"] = columns

    current_date = existing.get("business_date") if isinstance(existing.get("business_date"), dict) else None
    ours = current_date is None or (current_date.get("source") == "profile"
                                    and current_date.get("status") not in ("confirmed", "rejected"))
    if out.business_date is not None and ours:
        # 已有别的来源（模型起草、人工）定的业务日期不顶掉：按来源的可信程度剖析排得更前，但它只知道
        # 「表里只有这一个日期列」，不比写了规则和时区的那条更懂业务
        b = out.business_date
        draft["business_date"] = catalog.make_item({"column": b.column}, "profile", "proposed", note=b.note)
        findings.append({"kind": "business_date", "path": "business_date", "column": b.column, "min": b.low,
                         "max": b.high, "status": "proposed", "summary": f"提议按 {b.column} 作为业务日期"})
    elif out.keep_business_date and current_date is not None and current_date.get("source") == "profile":
        draft["business_date"] = copy.deepcopy(current_date)

    merged, stats = catalog.merge_notes(existing, draft, covered={"profile"}, at=at)
    for rel in merged.get("relations") or []:
        o = confirmed.get(str(rel.get("id")))
        if o is not None and rel.get("status") == "confirmed" and _patch_confirmed(rel, o, at):
            stats += catalog.MergeStats(updated=1)
    return merged, findings, stats


# ==========================================================================
# 入口
# ==========================================================================

#: 正在剖析的数据源。检查和登记之间没有 await，在一个事件循环里是原子的；服务端是单进程部署，
#: 多进程部署时要换成库里的锁
_RUNNING: set[str] = set()


@dataclass
class _Check:
    kind: str
    target: str
    path: str | None
    run: Callable[[], Awaitable[None]]
    #: 没查成时调用：把目录里剖析以前对这一项写的结论标记为原样保留
    keep: Callable[[], None]


def _plan(cx: _Context, table: str, notes: dict[str, Any], out: _TableOutcome,
          result: TableProfile) -> list[_Check]:
    """这张表要做的检查，按价值排：关系、码值、日期。先列全再执行：中途停下时，没做的每一项都要记下原因。"""
    meta = cx.tables[table]
    checks: list[_Check] = []
    for rel in _relation_candidates(notes):
        rid = str(rel["id"])
        checks.append(_Check("relation", _relation_target(rel), f"relations.{rid}",
                             lambda rel=rel: _check_relation(cx, table, rel, out, result),
                             lambda rid=rid: out.keep_relations.add(rid)))
    for column in _code_columns(meta, notes):
        checks.append(_Check("codes", column, f"columns.{column}.codes",
                             lambda column=column: _check_codes(cx, table, column, out, result),
                             lambda column=column: out.keep_codes.add(column)))
    dates = [str(c["name"]) for c in meta.get("columns") or [] if isinstance(c, dict) and _is_date_column(c)]
    for column in dates:
        if cx.is_masked(column):
            result.skipped.append(ProfileSkip("date", column, "masked",
                                              f"{table}.{column} 在数据源设置中被遮罩，不取最小值和最大值"))
    usable = [c for c in dates if not cx.is_masked(c)]
    if usable:
        checks.append(_Check("date", "、".join(usable), "business_date" if len(dates) == 1 else None,
                             lambda: _check_dates(cx, table, usable, len(dates), out, result),
                             lambda: setattr(out, "keep_business_date", True)))
    return checks


async def _profile_table(cx: _Context, table: str, notes: dict[str, Any], result: TableProfile) -> _TableOutcome:
    out = _TableOutcome()
    if cx.tables[table].get("is_view"):
        result.skipped.append(ProfileSkip("table", table, "view", "视图没有统计信息，剖析可能触发整个视图的计算，已跳过"))
        return out
    checks = _plan(cx, table, notes, out, result)
    for i, check in enumerate(checks):
        try:
            await check.run()
        except _Skip as e:
            result.skipped.append(ProfileSkip(check.kind, check.target, e.reason, e.detail, check.path))
            if e.carry:
                check.keep()
        except _Stop as e:
            detail = _STOP_DETAIL[e.reason].format(max_queries=cx.settings.max_queries,
                                                   max_total_s=_fmt(cx.settings.max_total_s))
            for rest in checks[i:]:
                result.skipped.append(ProfileSkip(rest.kind, rest.target, e.reason, detail, rest.path))
                rest.keep()
            break
    if table in cx.sizes:
        result.row_estimate = cx.sizes[table]
    return out


async def _write(session: AsyncSession, source_id: str, table: str, out: _TableOutcome, result: TableProfile, *,
                 at: str, actor: str | None) -> None:
    """并入并写入（乐观锁）。撞上别人刚改过就重读重并，最多三次。"""
    for _attempt in range(3):
        entry = await catalog.read_entry(session, source_id, table)
        version = entry.version if entry else 0
        notes, findings, stats = _apply(entry.notes if entry else {}, out, at=at)
        result.findings = findings
        result.added, result.updated, result.removed = stats.added, stats.updated, stats.removed
        result.version = version
        if entry is None and not notes:
            return                       # 什么也没得出，不建空目录
        try:
            written = await catalog.write_entry(session, source_id, table, notes, if_version=version, actor=actor)
        except catalog.CatalogConflict:
            continue
        result.version = written.version
        return
    result.error = "这张表的数据目录正被其他人修改，剖析结果未能写入，请稍后重新剖析"
    result.findings = []
    result.added = result.updated = result.removed = 0


async def _pick_tables(session: AsyncSession, source: Any, tables: list[str] | None,
                       entries: dict[str, catalog.CatalogEntry], report: ProfileReport) -> list[str]:
    cache = getattr(source, "schema_cache", None) or {}
    all_tables = cache.get("tables") or {}
    if tables:
        picked: list[str] = []
        for name in dict.fromkeys(str(t) for t in tables):
            key = catalog.resolve_table_name(cache, name)
            if key is None:
                report.tables.append(TableProfile(table=name, error=f"表结构里没有 {name}，请先重新探查结构"))
            elif key not in picked:
                picked.append(key)
        return picked
    usage = await catalog.table_usage(session, source)
    order = {name: i for i, name in enumerate(all_tables)}
    candidates = [name for name, meta in all_tables.items()
                  if not (meta or {}).get("is_view") and name in entries
                  and _relation_candidates(entries[name].notes)]
    return sorted(candidates, key=lambda n: (-usage.get(n, 0), order[n]))[:PROFILE_DEFAULT_TABLES]


async def profile_catalog(session: AsyncSession, row: Any, source: Any, *, tables: list[str] | None = None,
                          actor: str | None = None, settings: ProfileSettings | None = None) -> ProfileReport:
    """剖析这些表并把结论写进数据目录，同步完成。

    - row：数据源记录（开关、遮罩按它的 options）；source：用来查询和读表结构的源（上传源是绑定当前快照的视图）。
    - settings 不给就按 row.options 读；没开启抛 ProfileDisabled，这个源正在剖析抛 ProfileBusy。
    - tables 为空时按使用次数取前 PROFILE_DEFAULT_TABLES 张目录里有待核实关系的表。
    - 每张表做完就写入，后面的表停下（预算、总时长）不影响前面已经写进去的。
    """
    settings = settings or profile_settings(getattr(row, "options", None))
    name = getattr(row, "name", "") or ""
    if not settings.enabled:
        raise ProfileDisabled(f"数据源「{name}」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析")
    if source.id in _RUNNING:
        raise ProfileBusy(f"数据源「{name}」正在进行数据剖析，请等待完成后再试")
    _RUNNING.add(source.id)
    try:
        return await _profile(session, row, source, tables, actor, settings)
    finally:
        _RUNNING.discard(source.id)


async def _profile(session: AsyncSession, row: Any, source: Any, tables: list[str] | None, actor: str | None,
                   settings: ProfileSettings) -> ProfileReport:
    at = catalog.now_iso()
    report = ProfileReport(profiled_at=at, settings=settings)
    dialect = SqlDialect(source.kind)
    run = _Runner(source, settings, dialect)
    cx = _Context(source=source, settings=settings, dialect=dialect, run=run,
                  tables=(getattr(source, "schema_cache", None) or {}).get("tables") or {},
                  masked={c.lower() for c in data_engine.masked_columns(getattr(row, "options", None))},
                  day=at[:10])
    entries = await catalog.read_catalog(session, source.id)
    for table in await _pick_tables(session, source, tables, entries, report):
        result = TableProfile(table=table)
        report.tables.append(result)
        if run.stopped:
            detail = _STOP_DETAIL[run.stopped].format(max_queries=settings.max_queries,
                                                      max_total_s=_fmt(settings.max_total_s))
            result.skipped.append(ProfileSkip("table", table, run.stopped, detail))
            continue
        before = run.used
        entry = entries.get(table)
        out = await _profile_table(cx, table, entry.notes if entry else {}, result)
        result.queries = run.used - before
        await _write(session, source.id, table, out, result, at=at, actor=actor)
    report.queries_used = run.used
    report.stopped = run.stopped
    logger.info("数据剖析 源=%s 表=%s 查询=%d 停止=%s 署名=%s", source.id,
                ",".join(t.table for t in report.tables), run.used, run.stopped, actor)
    return report


__all__ = ["CODES_MAX", "IN_CHUNK", "MAX_CODE_LEN", "MAX_LITERAL_LEN", "NOTE_PREFIX", "PROFILE_DEFAULT_TABLES", "PROFILE_OPTION", "ProfileBusy",
           "ProfileDisabled", "ProfileReport", "ProfileSettings", "ProfileSkip", "SqlDialect", "TableProfile",
           "TableSize", "estimate_size", "profile_catalog", "profile_settings", "profile_settings_problem",
           "sql_literal"]
