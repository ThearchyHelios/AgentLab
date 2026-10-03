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

import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite

from app.data import engine as data_engine
from app.data import guard
from app.data.engine import CATALOG_PROFILE_OPTION, QueryResult, SnapshotTampered
from app.data.guard import QueryLimits, SqlRejected

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
    """这一项没查成：timeout 超时、error 查询失败、rejected 未通过守卫。记下原因，接着查别的。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


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


__all__ = ["MAX_LITERAL_LEN", "PROFILE_OPTION", "ProfileSettings", "SqlDialect", "TableSize", "estimate_size",
           "profile_settings", "profile_settings_problem", "sql_literal"]
