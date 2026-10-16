"""数据剖析：对业务库发少量只读查询，验证数据目录里推断出来的关联关系，补上基数、覆盖率和码值候选。

**为什么要有它。** 命名推断只看列名，可能推错，也给不出基数和覆盖率；而只有 verified、confirmed 的关系才会
触发错误级的 SQL 检查。剖析拿数据说话：从子表抽一批不同的键值去父表里对，对得上、父键唯一的升为 verified
（来源记为 profile，和外键约束的 fk 分开），对不上的保持 proposed 并在备注里写明覆盖率。顺带给状态类的列
取一次取值分布（码值候选，含义留给人填），给只有一个日期列的表提议业务日期。

**一次剖析一张表。** 入口是 profile_table：先列全这张表要做的检查（plan_checks，纯函数——范围里的 profile_checks、
建任务时列出的检查项、执行时用的是同一份），第一条查询估行数（确知是空表就整张表跳过），再逐项执行，每项开始、结束经
hooks 回调（后台任务据此把进度落库），整张表结束把结论并进目录（_apply，乐观锁）。没查成的项，目录里剖析以前对它的
结论原样留着：这次没查成不等于结论错了。以前是一次同步请求剖析一批表、预算按整次算，选 50 张表只做得完前两三张，
其余整张记「查询次数用完」；现在每张表各有自己的预算，任务、排队、停止、留痕的落库在 data/catalog_profile_jobs.py，
这里不碰任务表。

**这是数据目录里唯一会对业务库发查询的地方**，约束都是硬的：

- 默认关闭，按数据源在 options.catalog_profile 里开启（profile_settings）；
- 每条查询先过守卫的只读判定（不论数据源本身可写与否），再交给查询层 run_query：同一套连接、守卫和
  时限，不另开连接；查询一条一条发，不并发；
- 单条查询的时限取设置和数据源查询时限中小的，且不超过这张表剩下的时长；
- 每张表有自己的预算（TableBudget：查询次数、时长），用完只停这张表，剩下的项记「这张表的查询次数（时长）用完」，下一张
  表拿满额。整个任务一个查询器（SourceRunner）管任务级止损：连续 3 条查询失败（多半是连接断了、没有权限）、最近 10 条里
  7 条超时（库响应慢，再发只是给业务库加压）就停下整个任务；最近 6 条里 4 条超时先提醒；
- 每条发出的查询（含超时、失败、守卫拒掉的）都留痕，SQL 里的字面量换成「?」（sql_semantics.normalize_sql）：抽样到的
  键值不进 AgentLab 自己的库；
- 表有多大先看数据库的统计信息，没有统计信息就数到上限为止，不做全表 COUNT；超过上限的表不做
  COUNT(DISTINCT)、MIN/MAX 这类整表统计，抽样和取值分布也只看前若干行；
- 数据源设置里遮罩的列不取样，也不出现在任何一条剖析查询里；
- 只取低基数列的取值分布（最多 21 个取值），不取明细行。

表助手的只读查询、起草时核对有效记录条件走单条查询的入口（run_single_query）：同一个只读守卫、同一个单条时限，一次一条，
不设预算、不留痕。
"""
from __future__ import annotations

import asyncio
import collections
import copy
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime, timezone
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import select, update
from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import catalog, guard
from app.data import engine as data_engine
from app.data.engine import CATALOG_PROFILE_OPTION, QueryResult, SnapshotTampered
from app.data.guard import QueryLimits, SqlRejected
from app.data.sql_semantics import normalize_sql
from app.db.base import SessionLocal

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
    #: 每张表最多发几条查询（估行数、抽样、核对都算）。用完只停这张表，剩下的项记为「这张表的查询次数用完」
    table_max_queries: int = 40
    #: 每张表的时长上限（秒）。用完同样只停这张表
    table_max_s: float = 60
    #: 单条查询的时限（秒）。实际取它和数据源自己的查询时限中较小的一个，且不超过这张表剩下的时长
    query_timeout_s: float = 10
    #: 每条关系从子表抽多少个不同的键值去父表里对
    sample_size: int = 2000
    #: 行数不超过它的表才做 COUNT(DISTINCT)、MIN/MAX 这类整表统计；更大的、或者不知道多大的表只看前若干行。
    #: 填 0 表示一律不做整表统计
    max_scan_rows: int = 100_000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> ProfileSettings:
        """任务里存着的设置快照 → 设置（读法同 profile_settings：哪一项不对就用缺省；开关按快照里的）。"""
        return profile_settings({PROFILE_OPTION: raw}) if isinstance(raw, dict) else cls()


#: 数值项：(中文叫法, 下限, 上限, 是否必须是整数)。叫法和数据源设置里剖析预算的标签一致（frontend/src/lib/terms.ts），
#: 报错里只写它
_NUMBER_FIELDS: dict[str, tuple[str, float, float, bool]] = {
    "table_max_queries": ("每张表最多查询数", 1, 200, True),
    "table_max_s": ("每张表时长上限（秒）", 10, 600, False),
    "query_timeout_s": ("单条查询时限（秒）", 1, 60, False),
    "sample_size": ("核对键值数", 10, 10_000, True),
    "max_scan_rows": ("整表统计行数上限", 0, 10_000_000, True),
}
_ENABLED_LABEL = "开启数据剖析"
_KNOWN_KEYS = frozenset({"enabled", *_NUMBER_FIELDS})
#: 以前的整次预算（一次剖析最多几条查询、总时长）。现在预算按表算，这两项不再有意义：启动时从库里删掉
#: （migrate_settings），保存时请求里还带着的（老页面）只摘掉、不报错（without_legacy）。不折算成新键：旧值按整次剖析
#: 算，和「每张表」含义不同，硬折算才是不照实
LEGACY_KEYS = ("max_queries", "max_total_s")


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
    开关只认 true，写成别的一律当作没开。认不出的项（含迁移前的旧键）不看。"""
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
    found = profile_settings_rejection(options)
    return found[0] if found else None


def profile_settings_rejection(options: dict[str, Any] | None) -> tuple[str, str | None] | None:
    """同 profile_settings_problem，另给出错的是哪一项：(给人看的一句, 字段名)。字段名是 enabled 或 _NUMBER_FIELDS
    的键（table_max_queries……），格式不对、有认不出的项时为 None。接口带着它回 422，表单不必从原话里认是哪一格。
    旧键（LEGACY_KEYS）在这之前已经由 without_legacy 摘掉。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "数据剖析设置的格式不正确，请在数据源设置中重新填写", None
    if unknown := sorted(set(raw) - _KNOWN_KEYS):
        return f"数据剖析设置中有无法识别的项「{'、'.join(map(str, unknown))}」，请删除后重试", None
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        return f"数据剖析的「{_ENABLED_LABEL}」只能是开启或关闭", "enabled"
    for key, (label, low, high, integer) in _NUMBER_FIELDS.items():
        if key in raw and _number(raw[key], low, high, integer) is None:
            kind = "整数" if integer else "数"
            return f"数据剖析的「{label}」需要填写 {_fmt(low)} 到 {_fmt(high)} 之间的{kind}；当前为「{raw[key]}」", key
    return None


def without_legacy(options: dict[str, Any] | None) -> dict[str, Any] | None:
    """保存数据源时：剖析设置里还带着旧的整次预算（老页面提交的），只摘掉，不报错。没有旧键原样返回。"""
    raw = (options or {}).get(PROFILE_OPTION)
    if not isinstance(raw, dict) or not any(key in raw for key in LEGACY_KEYS):
        return options
    return {**(options or {}), PROFILE_OPTION: {k: v for k, v in raw.items() if k not in LEGACY_KEYS}}


async def migrate_settings(session: AsyncSession) -> int:
    """服务启动时一次性迁移：库里所有数据源 options.catalog_profile 中的旧键（max_queries、max_total_s）删掉，记日志。
    不折算成新键（见 LEGACY_KEYS）。返回改了几个数据源。只读写设置，不连业务库。"""
    from app.db.models import DataSource

    rows = (await session.execute(select(DataSource.id, DataSource.name, DataSource.options))).all()
    changed = 0
    for source_id, name, options in rows:
        cleaned = without_legacy(options if isinstance(options, dict) else None)
        if cleaned is options or cleaned is None:
            continue
        dropped = {k: (options or {}).get(PROFILE_OPTION, {}).get(k) for k in LEGACY_KEYS
                   if k in ((options or {}).get(PROFILE_OPTION) or {})}
        await session.execute(update(DataSource).where(DataSource.id == source_id).values(options=cleaned)
                              .execution_options(synchronize_session=False))
        logger.info("数据剖析设置迁移：数据源 %s（%s）去掉了按整次剖析算的旧预算 %s，改按每张表的预算", source_id, name,
                    dropped)
        changed += 1
    if changed:
        await session.commit()
    return changed

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


#: 定长文字类型（CHAR(2)、NCHAR、bpchar、CHARACTER(2)）：PostgreSQL、Oracle 取回的值带尾随空格补齐
_FIXED_CHAR = re.compile(r"^\s*(?:N?CHAR|BPCHAR|CHARACTER)\b(?!\s+VARYING)", re.IGNORECASE)


def is_fixed_char(type_name: str | None) -> bool:
    """定长文字列（CHAR(n)）：值的尾随空格是补齐出来的，不是取值的一部分（SQL 比较时也不算）。"""
    return bool(type_name) and bool(_FIXED_CHAR.match(str(type_name)))


class KeyLiterals(NamedTuple):
    """key_literals 的结果。literals：去父表里对的 IN 列表字面量（去重、保持顺序）；unmatchable：按类型就对不上父键、
    算作没对上的键值个数（去重后）；unsafe：值本身不能安全拼进 SQL、不计入核对的个数。"""
    literals: list[str]
    unmatchable: int
    unsafe: int


def key_literals(values: list[Any], kind: str | None, *, parent: str | None,
                 child_type: str | None) -> KeyLiterals:
    """从子表取回的键值 → 去父表里对的 IN 列表字面量（KeyLiterals）。

    字面量按**被指向列**的类型族写（catalog.column_family），不是按子表取回的值：
    - 父列是文字、子列是数：写成 '123'。PostgreSQL 上 varchar 列 IN (123) 直接报「operator does not exist」；
    - 父列是数、子列是文字：只拼整数写法的值（'0012' 也照 12 写）。别的（'A1'、'WALKIN'）不拼——Oracle 上数字列
      IN ('A1') 会把字面量转成数报 ORA-01722，MySQL 会把 'A1' 转成 0 去比、对上不该对上的行——但它们**照样是子表
      的键值**：整数主键里永远没有 'WALKIN'，所以算作没对上，计入 unmatchable，由调用方留在覆盖率的分母里。要是
      从分母里拿掉，散客代号占九成的一列只剩 1 个整数写法的键对上，就成了「覆盖率 100%」；
    - 父列是时间：不在这里处理，_check_relation 先整条跳过（各家时间字面量写法不同、精度不同，键不核对）；
    - 父列类型认不出：照子表取回的值的类型写（sql_literal，以前的做法）。
    子表是定长文字（CHAR(n)）时先去掉尾随空格：PG、Oracle 取回的 'A ' 拿去和 VARCHAR 父列比就对不上，而按 CHAR 的
    语义尾随空格本来就不算。不安全的值（反斜杠、「:名字」、控制字符、太长）照 sql_literal 的规矩跳过，记进 unsafe
    （调用方另记「类型不支持核对，未计入」）。两个不同的取值写成同一个字面量（'0012' 和 '12'）只算一个，不算 unsafe。
    """
    if parent == "time":
        raise ValueError("被指向列是日期时间的关系不核对键值（见 _check_relation）")
    fixed = is_fixed_char(child_type)
    out: list[str] = []
    unmatchable: set[str] = set()
    unsafe = 0
    for value in values:
        if fixed and isinstance(value, str):
            value = value.rstrip(" ")
        if parent == "text":
            if isinstance(value, bool):
                unsafe += 1
                continue
            if isinstance(value, (int, float, Decimal)):
                lit = sql_literal(value, "number")
                lit = None if lit is None else "'" + lit + "'"
            else:
                lit = sql_literal(value, "text")
        elif parent == "number":
            if isinstance(value, str):
                text = value.strip()
                if not _INT_TEXT.fullmatch(text):
                    unmatchable.add(text)
                    continue
                lit = str(int(text))
            else:
                lit = sql_literal(value, "number")
        else:
            lit = sql_literal(value, kind)
        if lit is None:
            unsafe += 1
        else:
            out.append(lit)
    return KeyLiterals(list(dict.fromkeys(out)), len(unmatchable), unsafe)


_DIALECT_MODULES = {"sqlite": sqlite, "postgres": postgresql, "postgresql": postgresql, "mysql": mysql,
                    "mariadb": mysql, "oracle": oracle}


#: 日期列的最小值、最大值只看这个区间里的值（含下限、不含上限）：0000-00-00（MySQL 关了严格模式写进去的零日期）、
#: 0001-01-01、9999-12-31（「没有」「永久」的占位日期）、PostgreSQL 的 -infinity / infinity 都落在区间外。区间外的
#: 值照实数出来（「有 N 个无效日期未计入」），不让一个占位日期把业务日期的范围撑成两千年。两端整天排除：带时刻的
#: 9999-12-31 23:59:59、文字存的「0001-01-01 00:00:00」也算占位
VALID_DATE_FROM = "0001-01-02"
VALID_DATE_BEFORE = "9999-12-31"
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class SqlDialect:
    """剖析 SQL 的方言差异：标识符怎么加引号、怎么只取前 n 行、统计信息在哪个系统视图里、日期字面量和按天截断
    怎么写、有没有块采样。只拼字符串，不连库。

    标识符交给 SQLAlchemy 各方言的 identifier_preparer：需要时才加引号（memberTags 加、visits 不加），
    MySQL 用反引号。Oracle 尤其不能一律加双引号：探查拿到的表名是 SQLAlchemy 规整过的小写，库里其实是
    大写，"visits" 加了引号就成了另一张不存在的表；不加引号 Oracle 自己转大写才对得上。反过来，库里真是小写的
    对象（建表时写了 "visits"）探查时记了 quote_name，这种要强制加引号、不转大写（table、column、stats_sql）。
    """

    def __init__(self, kind: str | None) -> None:
        self.kind = (kind or "").lower()
        module = _DIALECT_MODULES.get(self.kind)
        if module is None:
            raise ValueError(f"不支持的数据库类型：{kind}")
        self._dialect = module.dialect()
        self._prep = self._dialect.identifier_preparer
        self.oracle = self.kind == "oracle"

    def quote(self, name: str, *, force: bool = False) -> str:
        """标识符。force：探查记了 quote_name 的名字，原样加引号（Oracle 小写带引号的对象）。"""
        return self._prep.quote_identifier(name) if force else self._prep.quote(name)

    def table(self, meta: dict[str, Any] | None, name: str) -> str:
        """写进 FROM 的表名：探查时指定了 schema 的带上它（Oracle 只读账号名下没有对象，全靠 schema 前缀）。"""
        schema = (meta or {}).get("schema")
        quoted = self.quote(name, force=(meta or {}).get("quote_name") is True)
        return f"{self._prep.quote_schema(schema)}.{quoted}" if schema else quoted

    def column(self, meta: dict[str, Any] | None, name: str) -> str:
        """写进 SQL 的列名：表结构里这一列记了 quote_name 的强制加引号。"""
        cols = (meta or {}).get("columns") or []
        entry = next((c for c in cols if isinstance(c, dict) and c.get("name") == name), None)
        return self.quote(name, force=bool(entry and entry.get("quote_name") is True))

    def date_literal(self, value: Any, *, text: bool = False) -> str:
        """日期字面量（取值观察的时间窗口、日期列的有效区间用）。PostgreSQL、MySQL、Oracle 都认 DATE 'YYYY-MM-DD'；
        SQLite 没有日期类型，日期存成文字，写字符串。text：列是文字存的日期（VARCHAR），按字符串比——PostgreSQL 上
        varchar 和 date 比较会直接报错。只收真实存在的日期（date 或 YYYY-MM-DD），别的抛 ValueError：拼进 SQL 的
        只能是我们自己算出来的日子。"""
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, _date):
            day = value.isoformat()
        elif isinstance(value, str) and _ISO_DAY.fullmatch(value):
            try:
                day = _date.fromisoformat(value).isoformat()
            except ValueError:
                raise ValueError(f"不是有效的日期：{value}") from None
        else:
            raise ValueError(f"不是有效的日期：{value!r}")
        if text or self.kind == "sqlite":
            return f"'{day}'"
        return f"DATE '{day}'"

    def day_trunc(self, column: str) -> str:
        """时间列截到天（按日行数用）。PostgreSQL CAST(x AS DATE)；MySQL DATE(x)；Oracle 必须用 TRUNC(x)：Oracle 的
        DATE 带时分秒，CAST(ts AS DATE) 截不掉；SQLite 的日期是文字，取前 10 个字符。"""
        if self.kind in ("postgres", "postgresql"):
            return f"CAST({column} AS DATE)"
        if self.kind in ("mysql", "mariadb"):
            return f"DATE({column})"
        if self.oracle:
            return f"TRUNC({column})"
        return f"substr({column}, 1, 10)"

    def sample_from(self, table: str, percent: float) -> str | None:
        """块采样的 FROM：PostgreSQL TABLESAMPLE SYSTEM (p)、Oracle SAMPLE BLOCK (p)，按数据块随机取约 p% 的行；
        MySQL、SQLite 没有块采样，返回 None（调用方退回「按存储顺序的前 N 行」，并照实写口径）。"""
        p = float(percent)
        if not (0 < p < 100):
            raise ValueError(f"采样比例要在 0 到 100 之间：{percent}")
        if self.kind in ("postgres", "postgresql"):
            return f"{table} TABLESAMPLE SYSTEM ({p:g})"
        if self.oracle:
            return f"{table} SAMPLE BLOCK ({p:g})"
        return None

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
            # 小写带引号的对象（quote_name）在数据字典里就是小写，不能转成大写去找
            table = sql_literal(name if (meta or {}).get("quote_name") is True else denorm(name), "text")
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

    def min_max_sql(self, table: str, columns: list[tuple[str, bool]]) -> str:
        """日期列的最小值、最大值和无效日期的个数：每列三个值（MIN、MAX、区间外的个数）。columns 是 (写进 SQL 的列,
        是不是文字存的日期)。只看有效区间（VALID_DATE_FROM ≤ x < VALID_DATE_BEFORE）里的值，见 VALID_DATE_FROM。
        整表统计，只对小表用。"""
        parts = []
        for c, text in columns:
            lo = self.date_literal(VALID_DATE_FROM, text=text)
            hi = self.date_literal(VALID_DATE_BEFORE, text=text)
            valid = f"{c} >= {lo} AND {c} < {hi}"
            parts.append(f"MIN(CASE WHEN {valid} THEN {c} END), MAX(CASE WHEN {valid} THEN {c} END), "
                         f"SUM(CASE WHEN {c} < {lo} OR {c} >= {hi} THEN 1 ELSE 0 END)")
        return f"SELECT {', '.join(parts)} FROM {table}"

    # ---- 取值观察（下一轮第 3 波用）：窗口、按日行数、块采样。这里先把各方言的写法定下来，金样和容器集成测试都覆盖

    def _window(self, column: str, since: Any, text: bool) -> str:
        """时间窗口的条件：since 起（含），并且早于占位日期 9999-12-31——PostgreSQL 的 infinity、Oracle 的 9999-12-31
        都大于任何一天，不截掉上端就会在「最近 35 天」里多出一天。"""
        return (f"{column} >= {self.date_literal(since, text=text)} "
                f"AND {column} < {self.date_literal(VALID_DATE_BEFORE, text=text)}")

    def day_volume_sql(self, table: str, column: str, since: Any, *, text: bool = False) -> str:
        """最近若干天按天计数：since 起（含）每天几行。只在日期列打头有索引时对大表发（走索引的范围扫描）。"""
        day = self.day_trunc(column)
        return (f"SELECT {day} AS d, COUNT(*) AS n FROM {table} "
                f"WHERE {self._window(column, since, text)} GROUP BY {day} ORDER BY d")

    def window_value_counts_sql(self, table: str, column: str, date_column: str, since: Any, n: int, *,
                                text: bool = False) -> str:
        """时间窗口里的取值分布：业务日期 since 起（含）的行里，出现最多的 n 个取值和各自的行数。"""
        sql = (f"SELECT {column}, COUNT(*) AS cnt FROM {table} "
               f"WHERE {self._window(date_column, since, text)} AND {column} IS NOT NULL "
               f"GROUP BY {column} ORDER BY cnt DESC, {column}")
        return self.limit(sql, n)

    def sample_value_counts_sql(self, table: str, column: str, percent: float, n: int) -> str | None:
        """块采样里的取值分布；没有块采样的方言返回 None。"""
        source = self.sample_from(table, percent)
        if source is None:
            return None
        sql = (f"SELECT {column}, COUNT(*) AS cnt FROM {source} WHERE {column} IS NOT NULL "
               f"GROUP BY {column} ORDER BY cnt DESC, {column}")
        return self.limit(sql, n)


# ==========================================================================
# 发查询：守卫、单条时限、每张表的预算、任务级止损、留痕
# ==========================================================================

#: 这张表的时长只剩这么一点时不再发新查询：发出去的时限也只剩零点几秒，白占一次查询次数
_MIN_WINDOW_S = 0.5
#: 连续失败这么多条就停下整个任务：多半是连接断了、账号没有权限，再发只是一条条重复同一个错误
MAX_CONSECUTIVE_ERRORS = 3
#: 最近 SLOW_WINDOW 条查询里 SLOW_STOP 条超时就停下整个任务：库响应慢，再发只是给业务库加压
SLOW_WINDOW = 10
SLOW_STOP = 7
#: 最近 NOTICE_WINDOW 条里 NOTICE_ON 条超时先提醒（任务的 notice），好转后清掉
NOTICE_WINDOW = 6
NOTICE_ON = 4
#: 「运行原因没查成」的跳过原因：计入 unfinished，使这张表成为 partial。其余（遮罩、视图、表太大、取值太多……）是
#: 按规则不查，不算没做完
UNFINISHED_REASONS = frozenset({"budget", "deadline", "failed", "timeout", "error", "rejected", "slow", "cancelled"})

FAILED_STOP = "连续 {n} 条查询失败，已停止剖析：{line}"
SLOW_STOP_TEXT = f"数据库响应慢：最近 {SLOW_WINDOW} 条查询 {SLOW_STOP} 条超时，已停止，避免给业务库加压"
SLOW_NOTICE = "数据库响应慢：最近 {n} 条查询有 {k} 条超时。超时再多会自动停止，也可以先停止、换个时间再剖析"
#: 停止落在一条查询上：asyncio 这边不再等它的结果，库那边由单条时限截止（run_query 的 _server_deadline）
CANCELLED_QUERY = "剖析已停止，没有等这条查询的结果；数据库按单条时限截止它"


class _Stop(Exception):
    """停下：budget 这张表的查询次数用完、deadline 这张表的时长用完（只停这张表）；failed 连续失败、slow 超时比例
    （整个任务停下）。剩下的检查都不再发查询。"""

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


class QueryFailed(Exception):
    """单条只读查询（run_single_query）没成：reason 是 rejected 未通过守卫 / timeout 超时 / error 查询失败，detail
    是给人看的整句（同剖析里这一项的说明）。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _brief(e: BaseException) -> str:
    """驱动报错的第一行，去掉 SQLAlchemy 加的「(模块.类名)」前缀和后面附的 SQL、文档链接。"""
    lines = str(getattr(e, "orig", None) or e).strip().splitlines()
    first = re.sub(r"^\([\w.]+\)\s*", "", lines[0] if lines else "")
    return first[:160] or type(e).__name__


class TableBudget:
    """一张表的预算：最多 table_max_queries 条查询、table_max_s 秒（从这张表开始算）。用完抛 _Stop（budget /
    deadline），只停这张表，下一张表另起一份满额的。"""

    def __init__(self, settings: ProfileSettings) -> None:
        self.max_queries = int(settings.table_max_queries)
        self.seconds = float(settings.table_max_s)
        self.ends = time.monotonic() + self.seconds
        self.used = 0
        #: 一条查询的时限被这张表剩下的时长截短后超时了：时长已经用完
        self.out_of_time = False

    def remaining(self) -> float:
        """要发一条查询之前：次数、时长都还有，返回这张表剩下的秒数；用完抛 _Stop。不记次数（守卫拒掉的不算）。"""
        if self.used >= self.max_queries:
            raise _Stop("budget")
        left = self.ends - time.monotonic()
        if self.out_of_time or left <= _MIN_WINDOW_S:
            raise _Stop("deadline")
        return left


class SourceRunner:
    """剖析发的每一条查询都从这里走（整个任务一个）：先按只读过守卫，再交给查询层 run_query；管单条时限、任务级止损，
    每条查询交给 record 留痕。

    - 只读：不论数据源本身可写与否，先用 guard.check(readonly=True) 判一遍，再进 run_query——那里还会按
      数据源自己的设置判一遍。两遍是同一个守卫，不为剖析放宽，也不另开连接。
    - 单条时限：取设置和数据源查询时限中较小的一个，并且不超过这张表剩下的时长（budget）。
    - 预算：每发一条记一次这张表的次数（超时、失败的也算），守卫拒掉的没发出去，不算。
    - 止损：连续 MAX_CONSECUTIVE_ERRORS 条失败 → stopped = failed；最近 SLOW_WINDOW 条里 SLOW_STOP 条超时 →
      stopped = slow。停下之后再发直接抛 _Stop，error 是整句原因。最近 NOTICE_WINDOW 条里 NOTICE_ON 条超时时 notice
      有一句提醒，好转后清掉。
    """

    def __init__(self, source: Any, settings: ProfileSettings, dialect: SqlDialect) -> None:
        self.source = source
        self.settings = settings
        self.kind = dialect.kind
        self.timeout = min(float(settings.query_timeout_s), float(data_engine.query_timeout(source)))
        #: 整个任务发出的查询数、其中超时的、失败的
        self.queries = 0
        self.timeouts = 0
        self.errors = 0
        self.stopped: str | None = None
        self.error: str | None = None
        self.notice: str | None = None
        self._consecutive = 0
        self._recent: collections.deque[bool] = collections.deque(maxlen=SLOW_WINDOW)

    def _timed(self, timed_out: bool) -> None:
        """记下这条查询超时没有，按比例判提醒和止损。"""
        self._recent.append(timed_out)
        recent = list(self._recent)
        window = recent[-NOTICE_WINDOW:]
        hits = sum(window)
        self.notice = SLOW_NOTICE.format(n=len(window), k=hits) if hits >= NOTICE_ON else None
        if sum(recent) >= SLOW_STOP and self.stopped is None:
            self.stopped = "slow"
            self.error = SLOW_STOP_TEXT

    async def run(self, sql: str, *, max_rows: int = 1, budget: TableBudget | None = None,
                  record: Callable[[dict[str, Any]], None] | None = None) -> QueryResult:
        if self.stopped:
            raise _Stop(self.stopped)
        left = budget.remaining() if budget is not None else None
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        def log(outcome: str, ms: int, rows: int | None, detail: str | None) -> None:
            if record is not None:
                record({"at": at, "sql": normalize_sql(sql, self.kind).text, "ms": ms, "rows": rows,
                        "outcome": outcome, "detail": detail})

        try:
            guard.check(sql, readonly=True, source_name=getattr(self.source, "name", ""), dialect=self.kind)
        except SqlRejected as e:
            detail = f"剖析查询未通过安全守卫：{e}"
            log("rejected", 0, None, detail)
            raise _Skip("rejected", detail) from e
        if budget is not None:
            budget.used += 1
        self.queries += 1
        cut = left is not None and left < self.timeout
        seconds = round(min(self.timeout, left) if left is not None else self.timeout, 3)
        limits = QueryLimits(max_rows=max(1, int(max_rows)), timeout_seconds=seconds)
        started = time.perf_counter()

        def ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            result = await data_engine.run_query(self.source, sql, limits=limits)
        except SqlRejected as e:
            # 上面已经按只读过了同一个守卫，查询层再拒只可能是时限到了
            self.timeouts += 1
            if cut and budget is not None:
                budget.out_of_time = True
            detail = f"查询超时：超过 {_fmt(seconds)} 秒被中断，已跳过"
            log("timeout", ms(), None, detail)
            self._timed(True)
            raise _Skip("timeout", detail) from e
        except SnapshotTampered:
            raise
        except asyncio.CancelledError:
            log("error", ms(), None, CANCELLED_QUERY)
            raise
        except Exception as e:  # noqa: BLE001 - 一条查询失败不该拖垮整张表，原因照实记下
            self.errors += 1
            self._consecutive += 1
            detail = f"查询失败：{_brief(e)}"
            log("error", ms(), None, detail)
            self._timed(False)
            if self._consecutive >= MAX_CONSECUTIVE_ERRORS and self.stopped is None:
                self.stopped = "failed"
                self.error = FAILED_STOP.format(n=MAX_CONSECUTIVE_ERRORS, line=_brief(e))
            raise _Skip("error", detail) from e
        self._consecutive = 0
        log("ok", ms(), result.row_count, None)
        self._timed(False)
        return result


async def run_single_query(source: Any, sql: str, *, settings: ProfileSettings, max_rows: int = 1,
                           kind: str | None = None) -> QueryResult:
    """单条只读查询（表助手的 run_query、起草时核对有效记录条件）：同剖析一样过只读守卫、交给 run_query，单条时限取剖析
    设置和数据源查询时限中较小的一个。只有一条：不设预算、不止损、不留痕。

    没成抛 QueryFailed（reason、detail）；快照被改动抛 SnapshotTampered；不支持的数据库类型抛 ValueError。"""
    dialect = SqlDialect(kind if kind is not None else getattr(source, "kind", None))
    try:
        return await SourceRunner(source, settings, dialect).run(sql, max_rows=max_rows)
    except _Skip as e:
        raise QueryFailed(e.reason, e.detail) from e


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


async def estimate_size(run: Callable[..., Awaitable[QueryResult]], dialect: SqlDialect, meta: dict[str, Any],
                        name: str, *, max_scan_rows: int) -> tuple[TableSize, list[_Skip]]:
    """先读统计信息；读不到（SQLite、没分析过的表）再数到 max_scan_rows + 1 行为止。

    统计信息为 0 或负数（PostgreSQL 没分析过的表是 -1，MySQL 的估算值可能是 0）当作不知道：一张其实很大的表
    要是被当成空表，后面就会对它做整表统计。统计信息说的行数不超过 max_scan_rows 时也再数一次（多一条有上限的
    计数）：统计信息可能是很久以前的（表后来涨到了几千万行，ANALYZE 没再跑过），照它把大表当小表，就会对它做
    整表的 COUNT(DISTINCT)、MIN/MAX。数过的结果是准数（count），数到上限还没数完就按大表算（at_least）。
    返回 (行数, 途中没查成的几条)。这张表的查询次数、时长用完（或者任务止损）时抛 _Stop。
    """
    skips: list[_Skip] = []
    sql = dialect.stats_sql(meta, name)
    if sql:
        try:
            rows = _as_int(_first(await run(sql)))
        except _Skip as e:
            skips.append(e)
        else:
            if rows is not None and rows > max(max_scan_rows, 0):
                return TableSize(rows, "stats"), skips
            if rows is not None and rows > 0 and max_scan_rows <= 0:
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
# 一张表的剖析：上下文、结果
# ==========================================================================

#: IN 列表每批最多几个值：Oracle 的 IN 列表不能超过 1000 项（ORA-01795），各家统一按它分批
IN_CHUNK = 1000
#: 剖析写的备注都以它开头（「数据剖析：」）：人工确认过的关系，只覆盖剖析自己写的备注，不碰人写的（认的时候连
#: 升级前带日期的「数据剖析（日期）：」一起认，catalog.is_profile_note）。不写日期，时间看这一项的 updated_at
NOTE_PREFIX = catalog.PROFILE_NOTE_PREFIX
#: 停下的原因 → 剩下没查的项的说明。failed、slow 是任务级止损，停的是整个任务；cancelled 的说法由调用方给（停止、
#: 服务重启各有各的说法，见 TableHooks.stop_text）
_STOP_DETAIL = {
    "budget": "这张表的查询次数已用完（上限 {max_queries} 条），未检查",
    "deadline": "这张表的时长已用完（上限 {max_s} 秒），未检查",
    "failed": "连续多条查询失败，已停止剖析，未检查",
    "slow": "数据库响应慢，已停止剖析，未检查",
}
#: 整张表的原因（TableResult.error）
VIEW_ERROR = "视图没有统计信息，剖析可能触发整个视图的计算，已跳过"
EMPTY_ERROR = "空表，没有可剖析的数据"
NOTHING_ERROR = "没有可剖析的项：没有要核对的关联关系、像码值的列和日期列，或者都被遮罩了"
CONFLICT_ERROR = "这张表的数据目录正被其他人修改，剖析结果未能写入，请稍后重新剖析"
WRITE_ERROR = "写入数据目录时出错：{line}"
TABLE_ERROR = "剖析这张表时出错：{line}"
#: 停止、服务重启时正在剖析的那张表：做完的项照常写进了目录，剩下的没查
STOPPED_TABLE = "{why}：已做完的 {done} 项照常写进了数据目录，剩下 {rest} 项没查"
#: 停止的说法（TableHooks.stop_text 没给时）
STOPPED = "剖析已停止"


class ProfileDisabled(Exception):
    """这个数据源没有开启数据剖析，或者数据源停用了。message 给人看；code 是接口回 409 时带的机读码
    （api/coded.py：profile_disabled / datasource_inactive）。"""

    def __init__(self, message: str, code: str = "profile_disabled") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ProfileSkip:
    """没做的一项和原因。

    kind：relation 关系、codes 码值、date 日期列、row_estimate 行数估算、table 整张表。
    reason：budget 这张表的查询次数用完、deadline 这张表的时长用完、failed 连续失败止损、slow 超时比例止损、
    cancelled 停止或服务重启、timeout 超时、error 查询失败、rejected 未通过守卫（以上算「运行原因没查成」，见
    UNFINISHED_REASONS）；masked 字段被遮罩、too_large 表太大、view 视图、unsupported 暂不支持、no_data 没有数据、
    missing 表结构里没有、high_cardinality 取值太多（不像码值）、nothing 没有可剖析的项（按规则不查）。detail 是给人看的整句。
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
class TableResult:
    """一张表这次剖析的结果。

    status：ok 每项都有结论、partial 有项因为运行原因没查成、failed 写入一直冲突或整张表出错、skipped 表结构里没有 /
    视图 / 空表 / 没有可剖析的项。checks 是检查项（[{kind, target, path, state, reason, detail, queries}]，按执行顺序，
    含按规则不查的）；findings 是写进目录的发现（给界面看，形状见 _relation_finding 等）；query_log 是发过的查询
    （字面量已换成「?」）。"""

    table: str
    status: str = "running"
    checks: list[dict[str, Any]] = field(default_factory=list)
    #: 进行中时正在做哪一项的整句
    current: str | None = None
    queries: int = 0
    elapsed_ms: int = 0
    row_estimate: TableSize | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[ProfileSkip] = field(default_factory=list)
    #: 日期类列的取值范围（MIN、MAX）。只有一个日期列时它还写进了业务日期的备注；多个时只在这里
    date_ranges: list[dict[str, Any]] = field(default_factory=list)
    query_log: list[dict[str, Any]] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    removed: int = 0
    version: int = 0
    #: 整张表的原因：表结构里没有、视图、空表、没有可剖析的项、写入一直冲突、停止 / 中断 / 止损
    error: str | None = None

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def unfinished(self) -> int:
        """跳过的项里因为运行原因没查成的（次数、时长、超时、出错、守卫拒、止损、停止）。"""
        return sum(1 for s in self.skipped if s.reason in UNFINISHED_REASONS)

    def summary(self) -> dict[str, int]:
        return summarize(self.findings)


def summarize(findings: list[dict[str, Any]] | None) -> dict[str, int]:
    """发现的合计：有结论的关系条数、其中有确证（verified）的、取到码值候选的列数、这些码值里含义还空着的个数、提议了
    业务日期的表数（一张表时是 0 或 1）。"""
    found = [f for f in findings or [] if isinstance(f, dict)]
    relations = [f for f in found if f.get("kind") == "relation"]
    codes = [f for f in found if f.get("kind") == "codes"]
    return {"relations_checked": len(relations),
            "relations_verified": sum(1 for f in relations if f.get("status") == "verified"),
            "codes": len(codes), "codes_pending": sum(int(f.get("pending") or 0) for f in codes),
            "business_dates": sum(1 for f in found if f.get("kind") == "business_date")}


@dataclass
class _RelationOutcome:
    """一条关系核对完的结论。写不写、怎么写由 _apply 按写入时目录里那条关系的状态决定。"""

    rid: str
    target: str
    #: 外键关系不抽样（外键已保证引用完整），没有覆盖率
    coverage: float | None
    cardinality: str | None
    holds: bool
    sample: int
    matched: int
    note: str
    #: 基数是用数据核实的：子表一侧是否唯一有了定论（主键、唯一约束，或数过 COUNT 和 COUNT(DISTINCT)）。
    #: 子表太大、没数成的按多对一记，但不算核实——SQL 检查据此不把一对多关联后的重复计算报成错误
    cardinality_checked: bool = False


@dataclass
class _CodesOutcome:
    """一列的取值分布：[(码值, 行数)]，按行数从多到少。complete：看的是全表（没有只看前若干行），取到的就是
    这一列的全部取值，码值项标「已列全」，SQL 检查才拿它判断某个值「不在码值表里」。"""

    column: str
    values: list[tuple[str, int]]
    rows: int
    note: str
    complete: bool = False
    #: 列定义（ENUM）列出的取值：这次没出现的也写进码值（含义待填），码值是「可能的取值」
    declared: list[str] = field(default_factory=list)


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
class ProfileContext:
    """一个剖析任务共用的：源（上传源是绑定当前快照的视图）、设置、方言、查询器（任务级止损在它身上）、表结构、遮罩的
    列（小写，任务每张表开始前按数据源最新的设置换掉）、已经估过的行数（父表的行数核对关系时会用到，不重复估）。"""

    source: Any
    settings: ProfileSettings
    dialect: SqlDialect
    runner: SourceRunner
    tables: dict[str, Any]
    #: 遮罩的列（小写）。遮罩按列名生效、不分大小写、不分表，和证据面板的比法一致
    masked: set[str]
    sizes: dict[str, TableSize] = field(default_factory=dict)


def make_context(source: Any, settings: ProfileSettings, *, options: dict[str, Any] | None) -> ProfileContext:
    """剖析任务的上下文。options 是数据源记录的 options（遮罩按它）；不支持的数据库类型抛 ValueError。"""
    dialect = SqlDialect(getattr(source, "kind", None))
    return ProfileContext(source=source, settings=settings, dialect=dialect,
                          runner=SourceRunner(source, settings, dialect),
                          tables=(getattr(source, "schema_cache", None) or {}).get("tables") or {},
                          masked=masked_of(options))


def masked_of(options: dict[str, Any] | None) -> set[str]:
    return {c.lower() for c in data_engine.masked_columns(options)}


@dataclass
class TableHooks:
    """profile_table 的回调（后台任务据此落库）。都可以不给。

    - progress：一项检查（含开头的估行数）开始、结束时；result 里的 checks、current、queries、query_log 是这时的样子；
    - query：每发出（或被守卫拒掉）一条查询之后；
    - recorded：整张表结束，和写目录在同一个事务里（session 是那一次的，shield 起来，停止时也不会被打断；不用自己提交，
      写完由 _record 和目录一起提交）；
    - stop_text：停止时的说法（「剖析已停止」「服务重启，剖析中断」），停止之后才调。
    """

    progress: Callable[[TableResult], Awaitable[None]] | None = None
    query: Callable[[TableResult], Awaitable[None]] | None = None
    recorded: Callable[[AsyncSession, TableResult], Awaitable[None]] | None = None
    stop_text: Callable[[], str] | None = None


class _TableCx:
    """一张表这次剖析的执行状态：预算、正在做的检查项、发过的查询。检查函数经它发查询、估行数。"""

    def __init__(self, job: ProfileContext, table: str, result: TableResult, hooks: TableHooks) -> None:
        self.job = job
        self.settings = job.settings
        self.dialect = job.dialect
        self.tables = job.tables
        self.table = table
        self.result = result
        self.hooks = hooks
        self.budget = TableBudget(job.settings)
        #: 正在做的检查项（result.checks 里的那一个）；None 是这张表开头的估行数
        self.check: dict[str, Any] | None = None

    def is_masked(self, column: str) -> bool:
        return column.lower() in self.job.masked

    def scan_cap(self, size: TableSize) -> int | None:
        """大表（或不知道多大）抽样、取值分布只看前多少行；小表整表看，返回 None。"""
        if size.within(self.settings.max_scan_rows):
            return None
        return max(self.settings.sample_size, self.settings.max_scan_rows)

    async def run(self, sql: str, *, max_rows: int = 1, kind: str | None = None,
                  target: str | None = None) -> QueryResult:
        """发一条查询：记进这张表的留痕（kind、target 是它属于哪一项）、计数，再回调 hooks.query。"""
        check = self.check
        label = (kind or (check["kind"] if check else "row_estimate"),
                 target or (check["target"] if check else self.table))
        result = self.result

        def record(entry: dict[str, Any]) -> None:
            result.query_log.append({"at": entry["at"], "kind": label[0], "target": label[1], "sql": entry["sql"],
                                     "ms": entry["ms"], "rows": entry["rows"], "outcome": entry["outcome"],
                                     "detail": entry["detail"]})
            if entry["outcome"] != "rejected":
                result.queries += 1
                if check is not None:
                    check["queries"] = int(check.get("queries") or 0) + 1

        try:
            found = await self.job.runner.run(sql, max_rows=max_rows, budget=self.budget, record=record)
        except _Skip:
            await self._queried()
            raise
        await self._queried()
        return found

    async def _queried(self) -> None:
        if self.hooks.query is not None:
            await self.hooks.query(self.result)

    async def size(self, table: str, result: TableResult) -> TableSize:
        """表有多大，一个任务里每张表只估一次。途中没查成的记进 result（行数估算这一项）。"""
        if table not in self.job.sizes:
            async def run(sql: str, **kw: Any) -> QueryResult:
                return await self.run(sql, kind="row_estimate", target=table, **kw)

            size, skips = await estimate_size(run, self.dialect, self.tables[table], table,
                                              max_scan_rows=self.settings.max_scan_rows)
            self.job.sizes[table] = size
            result.skipped += [ProfileSkip("row_estimate", table, s.reason, s.detail) for s in skips]
        return self.job.sizes[table]

    async def size_with_data(self, table: str, result: TableResult) -> TableSize:
        """同 size，但确知是空表时直接跳过这一项（抛 _Skip），不再为它发查询。"""
        size = await self.size(table, result)
        if size.rows == 0:
            raise _Skip("no_data", f"{table} 是空表，无法剖析")
        return size

def _column(meta: dict[str, Any], name: str) -> str | None:
    """表结构里这一列的写法（目录里的列名大小写可能和表结构不同）；没有返回 None。"""
    lowered = name.lower()
    return next((str(c["name"]) for c in meta.get("columns") or []
                 if isinstance(c, dict) and str(c.get("name", "")).lower() == lowered), None)


def _column_meta(meta: dict[str, Any], name: str) -> dict[str, Any] | None:
    """表结构里这一列（类型、取值列表、quote_name）；name 是表结构里的写法（_column 的返回值）。"""
    return next((c for c in meta.get("columns") or [] if isinstance(c, dict) and c.get("name") == name), None)


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
    """要核对的关系：没被驳回的。人工确认过的也核对，只补覆盖率和基数。

    外键约束来的也核对，但只核对基数：外键保证了引用完整（不必抽样算覆盖率），却只保证被指向的键唯一，
    子表一侧每个键是一行还是多行要看数据。以前跳过外键关系，外键来的一对多永远拿不到「用数据核实过」，
    扇出检查只能报 warning——最常见的「订单关联明细后对订单金额求和」在受管发布时也挡不住。"""
    return [r for r in (notes or {}).get("relations") or []
            if isinstance(r, dict) and r.get("id") and r.get("status") != "rejected"]


async def _unique(cx: _TableCx, table: str, meta: dict[str, Any], column: str,
                  result: TableResult) -> tuple[bool | None, str]:
    """这一列（非空部分）唯一吗：(True / False / None 不知道, 依据)。有主键或唯一约束直接认；否则只对行数不超过
    整表统计上限的表数一次 COUNT 和 COUNT(DISTINCT)，大表不扫。"""
    how = _constraint(meta, column)
    if how:
        return True, how
    size = await cx.size(table, result)
    if not size.within(cx.settings.max_scan_rows):
        return None, "too_large"
    d = cx.dialect
    res = await cx.run(d.unique_check_sql(d.table(meta, table), d.column(meta, column)))
    row = res.rows[0] if res.rows else [None, None]
    total, distinct = _as_int(row[0]), _as_int(row[1])
    if total is None or distinct is None:
        return None, "count"
    return total == distinct, "count"


def _fk_note(child: tuple[bool | None, str]) -> str:
    """外键关系的剖析备注：只说子表一侧唯不唯一、依据是什么。不写日期（同 _relation_note）。"""
    unique, how = child
    if unique is None:
        why = "子表太大，没有整表统计" if how == "too_large" else "没能数清子表的取值"
        return f"{catalog.PROFILE_NOTE_PREFIX}外键约束已保证引用完整，未抽样核对覆盖率；{why}，基数未用数据核实"
    basis = "按主键或唯一约束" if how not in ("count", "too_large") else "整表数过取值"
    shape = "每个键只有一行（一对一）" if unique else "同一个键有多行（一对多）"
    return f"{catalog.PROFILE_NOTE_PREFIX}外键约束已保证引用完整，未抽样核对覆盖率；子表一侧{basis}：{shape}"


#: MySQL 上比较文字按列的排序规则：常见的排序规则不分大小写（_ci），PAD SPACE 的还忽略尾随空格。覆盖率、唯一性、
#: 码值的分组都按它算，写进说明，人才知道「A01」和「a01」在这里算同一个值
MYSQL_COLLATION_NOTE = "MySQL 按列的排序规则比较文字（常见的排序规则不分大小写），覆盖率和唯一性按它计算"
MYSQL_CODES_COLLATION_NOTE = "MySQL 按列的排序规则分组（常见的排序规则不分大小写），大小写不同的写法可能并成一个取值"


def _collation_note(dialect: SqlDialect, family: str | None, note: str) -> str | None:
    """MySQL、MariaDB 上文字列（或类型认不出的列）的排序规则说明；别的方言、数字列没有。"""
    return note if dialect.kind in ("mysql", "mariadb") and family in ("text", None) else None


def _relation_note(*, sample: int, matched: int, coverage: float, scan_cap: int | None,
                   skipped_values: int, parent: tuple[bool | None, str], child: tuple[bool | None, str] | None,
                   holds: bool, unmatchable: int = 0, collation: str | None = None) -> str:
    """关系的剖析备注：取了多少个键值、看了多少行、覆盖率、父键和子键唯一的依据。人据此判断这条结论有多可靠。
    不写日期：什么时候剖析的看这一项的 updated_at（界面按本地时间显示）。

    不说「抽样」：DISTINCT … LIMIT 取的是数据库先给出的那几个值，大表上是按存储顺序的前若干行里的值，不是随机抽的
    （多半是最老的数据）。unmatchable：被指向列是数字、子表取到的却不是整数写法的键值个数（key_literals）——已经算在
    sample 里、算作没对上，这里说明为什么没去父表里对。"""
    parts: list[str] = []
    low = coverage < catalog.PROFILE_VERIFY_COVERAGE
    if low:
        parts.append(f"覆盖率 {_percent(coverage)}，可能不是这条关系")
    scope = f"（按存储顺序只看前 {scan_cap} 行）" if scan_cap else ""
    sampled = f"子表取了 {sample} 个不同键值{scope}，父表对上 {matched} 个"
    parts.append(sampled if low else f"{sampled}，覆盖率 {_percent(coverage)}")
    if skipped_values:
        parts.append(f"另有 {skipped_values} 个键值的类型不支持核对，未计入")
    if unmatchable:
        parts.append(f"其中 {unmatchable} 个不是整数写法，被指向列是数字，算作没对上")
    if collation:
        parts.append(collation)
    unique, how = parent
    if unique is True:
        parts.append(f"被指向列是{how}" if how in ("主键", "唯一约束") else "被指向列经计数核对唯一")
    elif unique is False:
        parts.append("被指向列有重复值，不能作为关联的被指向键")
    elif how == "too_large":
        parts.append("被指向的表超过整表统计的行数上限，未核对被指向列是否唯一")
    else:
        parts.append("未能核对被指向列是否唯一")
    if child is not None:
        child_unique, child_how = child
        if child_unique is True:
            basis = child_how if child_how in ("主键", "唯一约束") else "计数核对"
            parts.append(f"子表一侧也唯一（{basis}），一对一")
        elif child_unique is False:
            parts.append("子表一侧有重复值，多对一")
        elif child_how == "too_large":
            parts.append("子表超过整表统计的行数上限，未核对子表一侧是否唯一，按多对一记")
        else:
            parts.append("未能核对子表一侧是否唯一，按多对一记")
    if not holds and not low:
        parts.append("暂不升为有确证")
    return NOTE_PREFIX + "；".join(parts) + "。"


async def _check_relation(cx: _TableCx, table: str, rel: dict[str, Any], out: _TableOutcome,
                          result: TableResult) -> None:
    """从子表取至多 sample_size 个不同的非空键值，去父表里数对上几个，得到覆盖率；再看父键、子键唯不唯一。"""
    # 两端在表结构里的写法；按规则不查的（多列、不在表结构里、指向视图、碰到遮罩列）plan_checks 已经列成跳过，这里再判
    # 一遍只是兜底
    parent, col, pcol = _relation_ends(table, rel, cx.tables, cx.job.masked)
    meta, pmeta = cx.tables[table], cx.tables[parent]

    if rel.get("source") == "fk":
        await cx.size_with_data(table, result)
        child = await _unique(cx, table, meta, col, result)
        cardinality = None if child[0] is None else ("one_to_one" if child[0] else "many_to_one")
        out.relations.append(_RelationOutcome(rid=str(rel["id"]), target=_relation_target(rel), coverage=None,
                                              cardinality=cardinality, holds=cardinality is not None, sample=0,
                                              matched=0, note=_fk_note(child), cardinality_checked=child[0] is not None))
        return

    d, s = cx.dialect, cx.settings
    child_col, parent_col = _column_meta(meta, col), _column_meta(pmeta, pcol)
    parent_family = catalog.column_family(parent_col)
    if parent_family == "time":
        # 时间键不核对：各家时间字面量写法、精度不同，拼出来的 IN 列表对不对得上说不准。不发子表的取值查询
        raise _Skip("unsupported", f"{parent}.{pcol} 是日期时间，各库的时间写法和精度不同，不核对这种关系的覆盖率")
    size = await cx.size_with_data(table, result)
    scan_cap = cx.scan_cap(size)
    child_table, parent_table = d.table(meta, table), d.table(pmeta, parent)
    res = await cx.run(d.distinct_sample_sql(child_table, d.column(meta, col), s.sample_size, scan_cap=scan_cap),
                       max_rows=s.sample_size)
    kind = (res.column_types or {}).get(res.columns[0]) if res.columns else None
    values = [row[0] for row in res.rows if row]
    literals, unmatchable, unsafe = key_literals(values, kind, parent=parent_family,
                                                 child_type=(child_col or {}).get("type"))
    if not values:
        raise _Skip("no_data", f"{table}.{col} 没有非空值，无法核对")
    if not literals and not unmatchable:
        raise _Skip("unsupported", f"{table}.{col} 的取值类型不支持核对（只核对整数和文字）")
    # 分母是子表取到的全部键值：不是整数写法、按类型就对不上整数父键的（unmatchable）也在里面，算作没对上
    sample = len(literals) + unmatchable
    matched = 0
    for start in range(0, len(literals), IN_CHUNK):
        sql = d.match_count_sql(parent_table, d.column(pmeta, pcol), literals[start:start + IN_CHUNK])
        matched += _as_int(_first(await cx.run(sql))) or 0
    matched = min(matched, len(literals))
    coverage = round(matched / sample, 4)

    parent_unique = await _unique(cx, parent, pmeta, pcol, result)
    child_unique = await _unique(cx, table, meta, col, result) if parent_unique[0] else None
    cardinality = None
    if child_unique is not None:
        cardinality = "one_to_one" if child_unique[0] is True else "many_to_one"
    holds = coverage >= catalog.PROFILE_VERIFY_COVERAGE and cardinality is not None
    note = _relation_note(sample=sample, matched=matched, coverage=coverage, scan_cap=scan_cap,
                          skipped_values=unsafe, parent=parent_unique, child=child_unique, holds=holds,
                          unmatchable=unmatchable,
                          collation=_collation_note(d, parent_family, MYSQL_COLLATION_NOTE))
    checked = child_unique is not None and child_unique[0] is not None
    out.relations.append(_RelationOutcome(rid=str(rel["id"]), target=_relation_target(rel), coverage=coverage,
                                          cardinality=cardinality, holds=holds, sample=sample,
                                          matched=matched, note=note, cardinality_checked=checked))


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
#: 像日期的取值：2026-07-01、2026/07/01，后面可以跟时间
_DATE_TEXT = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
#: 日期列最小值、最大值不计入的值（见 VALID_DATE_FROM）在说明里的说法
INVALID_DATES = "零日期 0000-00-00、0001-01-01、9999-12-31 这类占位日期或无穷大"


def _is_enum(type_name: str | None, enum_values: Any = None) -> bool:
    """取值列表有限的列：MySQL 的 ENUM(…) / SET(…)，或者探查记了取值列表（PostgreSQL 的枚举：类型写法只是类型名，
    setup_status 这种以 set 开头的名字不算 SET，见 catalog._MYSQL_ENUM）。"""
    return bool(enum_values) or catalog._mysql_enum_kind(type_name) is not None


def _code_type_ok(type_name: str | None, enum_values: Any = None) -> bool:
    """ENUM / SET（取值列表有限，直接算码值候选）、整数，或者短文字（定长不超过 MAX_CODE_LEN；SQLite 的 TEXT 不带
    长度，也算）。类型是按方言编译的完整写法：VARCHAR2(20 CHAR) 的长度按 20 算。"""
    if _is_enum(type_name, enum_values):
        return True
    t = catalog._type_words(type_name)
    if not t or any(k in t for k in ("CLOB", "BLOB", "BINARY", "JSON", "DATE", "TIME", "INTERVAL", "REAL", "FLOAT",
                                     "DOUBLE", "BOOL")):
        return False
    if "INT" in t:
        return True
    size = re.search(r"\(\s*(\d+)(?:\s+(?:CHAR|BYTE))?\s*(?:,\s*(\d+)\s*)?\)", t)
    if any(k in t for k in ("NUMBER", "NUMERIC", "DECIMAL")):
        return size is None or not size.group(2) or int(size.group(2)) == 0
    if any(k in t for k in ("CHAR", "TEXT", "STRING")):
        return size is None or int(size.group(1)) <= MAX_CODE_LEN
    return False


def _looks_like_code(name: str) -> bool:
    words = catalog.name_words(name)
    return bool(words) and words[-1] not in _NOT_CODE_TAILS and any(w in _CODE_WORDS for w in words)


def _code_columns(meta: dict[str, Any], notes: dict[str, Any]) -> list[str]:
    """要取值分布的列：目录里度量类型记为状态的、剖析以前提过码值候选的（再核一次）、ENUM / SET 列（取值列表有限，
    不看名字）、或者类型和命名像状态、类型、渠道的整数或短文字列。主键、唯一约束列、关系里的键列不算；码值人工确认
    或驳回过的不再取。被遮罩的列也列出来，由检查记为「被遮罩」跳过——要让人看得见为什么它没有码值。"""
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
        enum = _is_enum(col.get("type"), col.get("enum_values"))
        if not (status_measure or profiled or enum or _looks_like_code(name)):
            continue
        lowered = name.lower()
        if (pk == [lowered]) or lowered in unique or lowered in keys or not _code_type_ok(col.get("type"),
                                                                                           col.get("enum_values")):
            continue
        out.append(name)
    return out


def _code_key(value: Any, *, fixed: bool = False) -> str | None:
    """取值分布里的一个取值 → 码值（文字）。只认整数和不太长的文字。fixed：定长文字列（CHAR(n)），去掉补齐的尾随
    空格（PG、Oracle 取回的是 'A '，WHERE status = 'A' 照样对得上，码值写 'A'）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else None
    if isinstance(value, str) and fixed:
        value = value.rstrip(" ")
    if isinstance(value, str) and value.strip() and len(value) <= MAX_CODE_LEN and not _CONTROL.search(value):
        return value
    return None


def _share(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    ratio = count / total
    return "不足 1%" if 0 < ratio < 0.01 else f"{round(ratio * 100)}%"


async def _check_codes(cx: _TableCx, table: str, column: str, out: _TableOutcome, result: TableResult) -> None:
    """GROUP BY 取出现最多的 CODES_MAX + 1 个取值：不超过 CODES_MAX 个就是码值候选（值有了，含义等人填）。"""
    if cx.is_masked(column):
        raise _Skip("masked", f"{table}.{column} 在数据源设置中被遮罩，不取值", carry=False)
    d, meta = cx.dialect, cx.tables[table]
    col_meta = _column_meta(meta, column) or {}
    size = await cx.size_with_data(table, result)
    scan_cap = cx.scan_cap(size)
    res = await cx.run(d.value_counts_sql(d.table(meta, table), d.column(meta, column), CODES_MAX + 1,
                                          scan_cap=scan_cap),
                       max_rows=CODES_MAX + 1)
    if not res.rows:
        raise _Skip("no_data", f"{table}.{column} 没有非空值，无法取值分布")
    if len(res.rows) > CODES_MAX:
        raise _Skip("high_cardinality", f"{table}.{column} 的取值超过 {CODES_MAX} 个，不作为码值候选", carry=False)
    fixed = is_fixed_char(col_meta.get("type"))
    enum = _is_enum(col_meta.get("type"), col_meta.get("enum_values"))
    counts: dict[str, int] = {}
    for row in res.rows:
        key = _code_key(row[0], fixed=fixed)
        if key is None:
            raise _Skip("unsupported", f"{table}.{column} 的取值不是整数或短文字，不作为码值候选", carry=False)
        counts[key] = counts.get(key, 0) + (_as_int(row[1]) or 0)
    values = list(counts.items())
    if len(values) >= 3 and all(n == 1 for _, n in values) and not enum:
        # 每个取值只出现一次（票种编码 T01…T09）：这是一列标识，不是状态码。ENUM 的取值列表是列定义给的，不按这条判
        raise _Skip("high_cardinality", f"{table}.{column} 的取值各不相同，像标识而不是码值，不作为码值候选",
                    carry=False)
    rows = sum(n for _, n in values)
    # 不说「抽样」：大表看的是按存储顺序的前若干行（多半是最老的数据），不是随机抽的
    scope = f"按存储顺序取前 {scan_cap} 行统计" if scan_cap else "统计全表"
    spread = "、".join(f"{k}（{_share(n, rows)}）" for k, n in values)
    extra: list[str] = []
    # ENUM 的取值列表（SET 的是成员，取值是成员的组合，不算）：这次没出现的也是可能的取值
    is_set = catalog._mysql_enum_kind(col_meta.get("type")) == "SET"
    declared = [] if is_set else [str(v) for v in col_meta.get("enum_values") or []]
    unseen = [v for v in declared if v not in counts]
    if unseen:
        extra.append(f"列定义里另有 {len(unseen)} 个取值这次没有出现：{'、'.join(unseen[:CODES_MAX])}")
    family = "text" if enum else catalog.column_family(col_meta)
    collation = _collation_note(d, family, MYSQL_CODES_COLLATION_NOTE)
    if collation:
        extra.append(collation)
    tail = "".join(f"；{x}" for x in extra)
    note = f"{NOTE_PREFIX}{scope}，{rows} 行非空值共 {len(values)} 个取值：{spread}{tail}。"
    out.codes.append(_CodesOutcome(column=column, values=values, rows=rows, note=note, complete=scan_cap is None,
                                   declared=unseen))


def _is_date_column(col: dict[str, Any]) -> bool:
    """日期类列（catalog.is_date_column：和 SQL 检查认时间列是同一个判断）。"""
    return catalog.is_date_column(str(col.get("name") or ""), col.get("type"), enum_values=col.get("enum_values"))


async def _check_dates(cx: _TableCx, table: str, columns: list[str], total: int, out: _TableOutcome,
                       result: TableResult) -> None:
    """日期类列的 MIN、MAX，作为业务日期的参考。整表统计，只对行数不超过上限的表做。表里只有一个日期类列
    （total 是表结构里日期类列的个数，含被遮罩的）时提议它作为业务日期。"""
    d, meta = cx.dialect, cx.tables[table]
    size = await cx.size_with_data(table, result)
    if not size.within(cx.settings.max_scan_rows):
        raise _Skip("too_large", f"{table} 的行数超过整表统计的行数上限（{cx.settings.max_scan_rows} 行），"
                                 "不取日期列的最小值和最大值")
    # 文字存的日期（SQLite 的 TEXT、业务库里的 VARCHAR 日期）按字符串和有效区间比；日期类型的按日期字面量比
    texts = [catalog.column_family(_column_meta(meta, c)) != "time" for c in columns]
    res = await cx.run(d.min_max_sql(d.table(meta, table), [(d.column(meta, c), t) for c, t in zip(columns, texts)]))
    row = res.rows[0] if res.rows else []
    invalid: dict[str, int] = {}
    for i, column in enumerate(columns):
        low, high, bad = (row[3 * i], row[3 * i + 1], row[3 * i + 2]) if len(row) >= 3 * i + 3 else (None, None, 0)
        invalid[column] = _as_int(bad) or 0
        if low is None or high is None or not (_DATE_TEXT.match(str(low)) and _DATE_TEXT.match(str(high))):
            continue
        found: dict[str, Any] = {"column": column, "min": str(low), "max": str(high)}
        if invalid[column]:
            found["invalid"] = invalid[column]
        result.date_ranges.append(found)
    if total != 1:
        return
    if not result.date_ranges:
        if invalid.get(columns[0]):
            raise _Skip("no_data", f"{table}.{columns[0]} 只有 {invalid[columns[0]]} 个无效日期（{INVALID_DATES}），"
                                   "没有可识别的日期值")
        raise _Skip("no_data", f"{table}.{columns[0]} 没有可识别的日期值")
    found = result.date_ranges[0]
    dropped = (f"另有 {found['invalid']} 个无效日期（{INVALID_DATES}）未计入，" if found.get("invalid") else "")
    note = (f"{NOTE_PREFIX}表里只有这一个日期类列，取值从 {str(found['min'])[:19]} 到 "
            f"{str(found['max'])[:19]}，{dropped}可作为业务日期的参考。")
    out.business_date = _DateOutcome(column=found["column"], low=found["min"], high=found["max"], note=note)


# ==========================================================================
# 结论进目录
# ==========================================================================


def _relation_finding(o: _RelationOutcome, rel: dict[str, Any], *, status: str, confirmed: bool) -> dict[str, Any]:
    """一条关系的核实结论。previous_status 是剖析之前目录里这条关系的状态：报告按它说「升为已验证」还是「仍为已验证」
    （重新剖析、目录没变时不能说升了）；cardinality_checked：基数是不是用数据核实的（子表一侧数过是否唯一），
    报告据此写「已用数据核实」，不从说明的原话里认。"""
    previous = rel.get("status")
    if rel.get("source") == "fk":
        summary = ("外键约束，" + ("子表每个键只有一行（一对一）" if o.cardinality == "one_to_one"
                                  else "子表同一个键有多行（一对多）") + "，基数已用数据核实") \
            if o.cardinality_checked else "外键约束，子表太大或没能数清，基数未用数据核实"
    elif confirmed:
        summary = "已人工确认，只补充覆盖率和基数"
    elif status == "verified":
        summary = f"覆盖率 {_percent(o.coverage)}，" + ("仍为有确证" if previous == "verified" else "升为有确证")
    elif o.coverage < catalog.PROFILE_VERIFY_COVERAGE:
        summary = f"覆盖率 {_percent(o.coverage)}，保持推断"
    else:
        summary = "被指向列未核实唯一，保持推断"
    return {"kind": "relation", "path": f"relations.{o.rid}", "target": o.target,
            "columns": list(rel.get("columns") or []), "to_table": rel.get("to_table"),
            "to_columns": list(rel.get("to_columns") or []), "status": status, "confirmed": confirmed,
            "previous_status": previous, "coverage": o.coverage, "cardinality": o.cardinality,
            "cardinality_checked": o.cardinality_checked, "sample": o.sample, "matched": o.matched, "summary": summary}


def _codes_value(c: _CodesOutcome, item: dict[str, Any] | None) -> dict[str, str]:
    """码值候选的值：观察到的取值，含义先空着。原来有别的来源（模型起草、数据库注释）写过含义的，对得上的取值
    沿用它的含义；它写了、数据里没出现的取值也留着——码值是「可能的取值」，没出现不等于不存在。"""
    old = item.get("value") if isinstance(item, dict) and isinstance(item.get("value"), dict) else {}
    value = {key: str(old.get(key) or "") for key, _ in c.values}
    for key in c.declared:
        value.setdefault(key, str(old.get(key) or ""))
    for key, meaning in old.items():
        if key not in value and isinstance(meaning, str) and meaning.strip():
            value[key] = meaning
    return value


def _codes_finding(c: _CodesOutcome, value: dict[str, str]) -> dict[str, Any]:
    """码值候选的结论。pending：写进目录的码值里还有几个含义空着（别的来源写过的含义会沿用），报告按它说
    「N 个含义待填写」，不从 summary 的原话里认。"""
    pending = sum(1 for v in value.values() if not v.strip())
    summary = f"{len(c.values)} 个取值" + (f"，{pending} 个含义待填写" if pending else "")
    return {"kind": "codes", "path": f"columns.{c.column}.codes", "column": c.column,
            "values": [{"value": k, "rows": n} for k, n in c.values], "rows": c.rows, "status": "proposed",
            "pending": pending, "summary": summary}


def _patch_confirmed(rel: dict[str, Any], o: _RelationOutcome, at: str) -> bool:
    """人工确认过的关系只补覆盖率和基数：覆盖率是测量值，照新的写；基数只在原来没有时补，不改人定的；
    备注只在原来为空、或者是剖析自己写的时候换成新的（只是日期变了不换）。状态、来源、确认时的依据（reason，保存提案时
    另存，不在备注里）不动。返回改没改。"""
    changed = False
    if rel.get("coverage") != o.coverage:
        rel["coverage"] = o.coverage
        changed = True
    if rel.get("cardinality") is None and o.cardinality is not None:
        rel["cardinality"] = o.cardinality
        if o.cardinality_checked:
            rel["cardinality_checked"] = True
        changed = True
    note = rel.get("note")
    if (not note or catalog.is_profile_note(note)) and not catalog.same_profile_note(note, o.note):
        rel["note"] = o.note
        changed = True
    if changed:
        rel["updated_at"] = at
    return changed


def _patch_fk(rel: dict[str, Any], o: _RelationOutcome, at: str) -> bool:
    """外键关系只补基数和「用数据核实过」：外键结构推的多对一可能其实是一对一，数据说了算。来源、状态、
    覆盖率都不动；备注同 _patch_confirmed（只是旧备注的日期不同不换）。人工确认过的（值不变、保存即确认的外键关系，
    来源仍是 fk）同 _patch_confirmed：不改人定的基数，数据核实出的不一样时也不标「用数据核实过」，测量的说法照常写进
    备注。返回改没改。"""
    changed = False
    overruled = rel.get("status") == "confirmed" and rel.get("cardinality") not in (None, o.cardinality)
    if o.cardinality_checked and not overruled:
        if rel.get("cardinality") != o.cardinality:
            rel["cardinality"] = o.cardinality
            changed = True
        if rel.get("cardinality_checked") is not True:
            rel["cardinality_checked"] = True
            changed = True
    note = rel.get("note")
    if (not note or catalog.is_profile_note(note)) and not catalog.same_profile_note(note, o.note):
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
    - 外键约束来的关系：只补基数和「用数据核实过」（_patch_fk），来源、状态、覆盖率都不动。
    - 码值候选：profile / proposed，含义沿用别的来源写过的（_codes_value）；整表统计得到的标「已列全」，只看了
      前若干行的不标；人工确认、驳回过的码值不碰。
    - 业务日期：只有一个日期类列时提议，profile / proposed；已有别的来源定的业务日期不顶掉。
    - covered={"profile"}：这次完整检查过、却没再得出的剖析结论删掉（比如取值变多、不再像码值的列，列被遮罩
      之后它的码值）；没查成的检查（超时、预算用完……）把剖析以前写的结论原样带上，不因为这次没查成就删。
    """
    existing = existing or {}
    current = {str(r["id"]): r for r in existing.get("relations") or [] if isinstance(r, dict) and r.get("id")}
    relations: list[dict[str, Any]] = []
    confirmed: dict[str, _RelationOutcome] = {}
    foreign: dict[str, _RelationOutcome] = {}
    findings: list[dict[str, Any]] = []
    for o in out.relations:
        rel = current.get(o.rid)
        if rel is None or rel.get("status") == "rejected":
            continue                     # 核对期间被删、被驳回了：不写
        if rel.get("source") == "fk":
            foreign[o.rid] = o
            findings.append(_relation_finding(o, rel, status=str(rel.get("status")), confirmed=False))
            continue
        if rel.get("status") == "confirmed":
            confirmed[o.rid] = o
            findings.append(_relation_finding(o, rel, status="confirmed", confirmed=True))
            continue
        status = "verified" if o.holds else "proposed"
        relations.append({"id": o.rid, "columns": list(rel["columns"]), "to_table": rel["to_table"],
                          "to_columns": list(rel["to_columns"]), "cardinality": o.cardinality,
                          "coverage": o.coverage, "source": "profile", "status": status, "note": o.note,
                          # 只在基数用数据核实过时有这个键：没核实的关系和以前一字不差
                          **({"cardinality_checked": True} if o.cardinality_checked else {})})
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
        columns[c.column] = {"codes": catalog.make_item(_codes_value(c, item), "profile", "proposed", note=c.note,
                                                        complete=c.complete)}
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
        rid = str(rel.get("id"))
        o = foreign.get(rid)
        if o is not None and rel.get("source") == "fk":
            if _patch_fk(rel, o, at):
                stats += catalog.MergeStats(updated=1)
            continue
        o = confirmed.get(rid)
        if o is not None and rel.get("status") == "confirmed" and _patch_confirmed(rel, o, at):
            stats += catalog.MergeStats(updated=1)
    return merged, findings, stats


# ==========================================================================
# 一张表要做哪些检查（纯函数）
# ==========================================================================


@dataclass(frozen=True)
class PlannedCheck:
    """一项检查。skip 给了的是按规则不查的（遮罩、多列关系、表结构里没有、被指向的是视图）：(原因, 说明, 要不要原样
    留着旧结论)，不发查询；relation / column / columns 是执行时要的参数。"""

    kind: str
    target: str
    path: str | None
    skip: tuple[str, str, bool] | None = None
    relation: dict[str, Any] | None = None
    column: str | None = None
    columns: tuple[str, ...] = ()
    #: 日期检查：表结构里日期类列的个数（含被遮罩的）。只有一个时才提议业务日期
    date_total: int = 0

    def as_check(self) -> dict[str, Any]:
        """检查项（TableResult.checks 的一项）：还没开始，或者按规则跳过。"""
        state, reason, detail = ("skipped", self.skip[0], self.skip[1]) if self.skip else ("pending", None, None)
        return {"kind": self.kind, "target": self.target, "path": self.path, "state": state, "reason": reason,
                "detail": detail, "queries": 0}


def _relation_ends(table: str, rel: dict[str, Any], tables: dict[str, Any], masked: set[str]) -> tuple[str, str, str]:
    """一条关系两端在表结构里的写法：(被指向的表, 子表的列, 被指向的列)。按规则不查的抛 _Skip：多列组成的、表结构里
    没有的表或列、被指向的是视图、两端有被遮罩的列（遮罩的列不出现在任何一条剖析查询里）。"""
    cols, to_cols = list(rel.get("columns") or []), list(rel.get("to_columns") or [])
    to_table = str(rel.get("to_table") or "")
    if len(cols) != 1 or len(to_cols) != 1:
        raise _Skip("unsupported", "多列组成的关联关系暂不剖析")
    meta = tables[table]
    parent = to_table if to_table in tables else catalog.resolve_table_name({"tables": tables}, to_table)
    if parent is None:
        raise _Skip("missing", f"表结构里没有被指向的表 {to_table}，请先重新探查结构")
    pmeta = tables[parent]
    col, pcol = _column(meta, cols[0]), _column(pmeta, to_cols[0])
    if col is None or pcol is None:
        missing = f"{table}.{cols[0]}" if col is None else f"{parent}.{to_cols[0]}"
        raise _Skip("missing", f"表结构里没有列 {missing}，请先重新探查结构")
    if pmeta.get("is_view"):
        raise _Skip("view", f"被指向的 {parent} 是视图，不剖析")
    for name, owner in ((col, table), (pcol, parent)):
        if name.lower() in masked:
            raise _Skip("masked", f"{owner}.{name} 在数据源设置中被遮罩，不取样", carry=True)
    return parent, col, pcol


def plan_checks(table: str, meta: dict[str, Any] | None, notes: dict[str, Any] | None, *, tables: dict[str, Any],
                masked: set[str]) -> list[PlannedCheck]:
    """这张表要做的检查，按价值排：关系、码值、日期。范围（profile_checks）、建任务时列出的检查项、执行时用的都是这一份：
    先列全再执行，中途停下时没做的每一项都要记下原因。视图、表结构里没有的表返回空列表。

    按规则不查的也列出来（带 skip）：被遮罩的码值列、日期列，多列的、两端不在表结构里的、指向视图的、碰到遮罩列的关系——
    要让人看得见为什么它没有结论。masked 是遮罩的列（小写）。"""
    if not isinstance(meta, dict) or meta.get("is_view"):
        return []
    notes = notes or {}
    checks: list[PlannedCheck] = []
    for rel in _relation_candidates(notes):
        skip: tuple[str, str, bool] | None = None
        try:
            _relation_ends(table, rel, tables, masked)
        except _Skip as e:
            skip = (e.reason, e.detail, e.carry)
        checks.append(PlannedCheck("relation", _relation_target(rel), f"relations.{rel['id']}", skip, relation=rel))
    for column in _code_columns(meta, notes):
        skip = (("masked", f"{table}.{column} 在数据源设置中被遮罩，不取值", False)
                if column.lower() in masked else None)
        checks.append(PlannedCheck("codes", column, f"columns.{column}.codes", skip, column=column))
    dates = [str(c["name"]) for c in meta.get("columns") or [] if isinstance(c, dict) and _is_date_column(c)]
    for column in dates:
        if column.lower() in masked:
            checks.append(PlannedCheck("date", column, None,
                                       ("masked", f"{table}.{column} 在数据源设置中被遮罩，不取最小值和最大值", False),
                                       column=column))
    usable = [c for c in dates if c.lower() not in masked]
    if usable:
        checks.append(PlannedCheck("date", "、".join(usable), "business_date" if len(dates) == 1 else None,
                                   columns=tuple(usable), date_total=len(dates)))
    return checks


def has_pending_relations(notes: dict[str, Any] | None) -> bool:
    """这张表还有没有待核实的关系：要核对的关系里有还是推断（proposed）的。挑表时这样的表排在前面。"""
    return any(r.get("status") == "proposed" for r in _relation_candidates(notes))


def pending_relation_paths(notes: dict[str, Any] | None) -> set[str]:
    """待核实（proposed）关系的路径（relations.<编号>）。上次剖析的检查项里没有它们，就是剖析之后冒出了新的待核实关系。"""
    return {f"relations.{r['id']}" for r in _relation_candidates(notes) if r.get("status") == "proposed"}


# ==========================================================================
# 入口：剖析一张表
# ==========================================================================

ESTIMATING = "正在估算 {table} 的行数"


def _current_text(table: str, check: PlannedCheck) -> str:
    """进行中时正在做哪一项的整句（逐表一行的最后一栏）。"""
    if check.kind == "relation":
        return f"正在核对 {table}.{check.target}"
    if check.kind == "codes":
        return f"正在取 {table}.{check.column} 的取值分布"
    if len(check.columns) == 1:
        return f"正在取 {table}.{check.columns[0]} 的最小值和最大值"
    return f"正在取 {table} 中 {check.target} 的最小值和最大值"


def _keep(out: _TableOutcome, check: PlannedCheck) -> None:
    """这一项没查成：目录里剖析以前对它写的结论标记为原样保留。"""
    if check.kind == "relation" and check.relation is not None:
        out.keep_relations.add(str(check.relation["id"]))
    elif check.kind == "codes" and check.column:
        out.keep_codes.add(check.column)
    elif check.kind == "date" and check.columns:
        out.keep_business_date = True


def _close_open(result: TableResult, plan: list[PlannedCheck], out: _TableOutcome, reason: str, detail: str) -> int:
    """还没结束的项（排着的、正在做的）都记为跳过、原因 reason，旧结论原样保留。返回记了几项。"""
    n = 0
    for check, planned in zip(result.checks, plan):
        if check["state"] in ("pending", "running"):
            check.update(state="skipped", reason=reason, detail=detail)
            result.skipped.append(ProfileSkip(check["kind"], check["target"], reason, detail, check["path"]))
            _keep(out, planned)
            n += 1
    return n


def _stop_detail(cx: _TableCx, reason: str) -> str:
    return _STOP_DETAIL[reason].format(max_queries=cx.settings.table_max_queries, max_s=_fmt(cx.settings.table_max_s))


async def _execute(cx: _TableCx, table: str, check: PlannedCheck, out: _TableOutcome) -> None:
    if check.kind == "relation":
        await _check_relation(cx, table, check.relation or {}, out, cx.result)
    elif check.kind == "codes":
        await _check_codes(cx, table, str(check.column), out, cx.result)
    else:
        await _check_dates(cx, table, list(check.columns), check.date_total, out, cx.result)


async def _progress(cx: _TableCx) -> None:
    if cx.hooks.progress is not None:
        await cx.hooks.progress(cx.result)


async def _run_checks(cx: _TableCx, table: str, plan: list[PlannedCheck], runnable: list[int],
                      out: _TableOutcome) -> None:
    """第一条查询估行数（确知是空表就整张表跳过），再逐项执行。预算用完、任务止损时剩下的项记原因、保留旧结论。"""
    result = cx.result
    result.current = ESTIMATING.format(table=table)
    await _progress(cx)
    try:
        size = await cx.size(table, result)
    except _Stop as e:
        _close_open(result, plan, out, e.reason, _stop_detail(cx, e.reason))
        if e.reason in ("failed", "slow"):
            result.error = cx.job.runner.error
        return
    finally:
        result.current = None
    if size.rows == 0:
        _close_open(result, plan, out, "no_data", f"{table} 是空表，无法剖析")
        result.status, result.error = "skipped", EMPTY_ERROR
        return
    for i in runnable:
        planned, check = plan[i], result.checks[i]
        check["state"] = "running"
        result.current = _current_text(table, planned)
        cx.check = check
        await _progress(cx)
        try:
            await _execute(cx, table, planned, out)
            check["state"] = "done"
        except _Skip as e:
            check.update(state="skipped", reason=e.reason, detail=e.detail)
            result.skipped.append(ProfileSkip(planned.kind, planned.target, e.reason, e.detail, planned.path))
            if e.carry:
                _keep(out, planned)
        except _Stop as e:
            cx.check = None
            _close_open(result, plan, out, e.reason, _stop_detail(cx, e.reason))
            if e.reason in ("failed", "slow"):
                result.error = cx.job.runner.error
            break
        finally:
            cx.check = None
            result.current = None
        await _progress(cx)


async def _own_session(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    async with SessionLocal() as session:
        return await fn(session, *args)


async def _read_notes(session: AsyncSession, source_id: str, table: str) -> dict[str, Any]:
    entry = await catalog.read_entry(session, source_id, table)
    return entry.notes if entry else {}


async def profile_table(cx: ProfileContext, table: str, *,
                        db: Callable[..., Awaitable[Any]] | None = None, hooks: TableHooks | None = None,
                        actor: str | None = None) -> TableResult:
    """剖析一张表并把结论写进目录，返回这张表的结果（TableResult）。

    - 表结构里没有、视图：skipped，不发查询。
    - 先列全检查项（plan_checks，读的是这时目录里的最新内容），按规则不查的直接记跳过；一项可查的都没有：skipped
      「没有可剖析的项」，不发查询。
    - 第一条查询估行数，确知是空表：skipped「空表」。
    - 逐项执行：这张表的查询次数、时长用完（TableBudget）只停这张表，剩下的项记 budget / deadline；任务止损（cx.runner）
      停下时剩下的项记 failed / slow，error 是止损的那一句。
    - 停止（任务被 cancel）：正在跑的那条查询由数据库按单条时限截止；已做完的项照常写进目录，没做的记 cancelled、保留旧
      结论；写完再把停止传下去（CancelledError）。
    - 整张表结束把结论并进目录（乐观锁，撞上别人刚改过重读重并，三次仍冲突记 failed），和 hooks.recorded 在同一个事务里
      提交。

    db 是执行库操作的：db(fn, *args) 开一个会话调 fn(session, *args)。后台任务给它的 SerialWriter（一条一条、shield），
    不给就各开一个会话。快照被改动（SnapshotTampered）原样抛出，不写目录：整个任务都该停。
    """
    hooks = hooks or TableHooks()
    db = db or _own_session
    started = time.monotonic()
    result = TableResult(table=table)
    meta = cx.tables.get(table)
    if not isinstance(meta, dict) or meta.get("is_view"):
        reason = "view" if isinstance(meta, dict) else "missing"
        why = VIEW_ERROR if reason == "view" else catalog.missing_table_error(table)
        result.status, result.error = "skipped", why
        result.skipped.append(ProfileSkip("table", table, reason, why))
        await db(_record, cx.source.id, table, None, result, actor, hooks.recorded)
        return result
    notes = await db(_read_notes, cx.source.id, table)
    plan = plan_checks(table, meta, notes, tables=cx.tables, masked=cx.masked)
    out = _TableOutcome()
    tcx = _TableCx(cx, table, result, hooks)
    result.checks = [p.as_check() for p in plan]
    for planned in plan:
        if planned.skip is not None:
            result.skipped.append(ProfileSkip(planned.kind, planned.target, planned.skip[0], planned.skip[1],
                                              planned.path))
            if planned.skip[2]:
                _keep(out, planned)
    runnable = [i for i, p in enumerate(plan) if p.skip is None]
    stopped: asyncio.CancelledError | None = None
    try:
        if not runnable:
            result.status, result.error = "skipped", NOTHING_ERROR
            result.skipped.append(ProfileSkip("table", table, "nothing", NOTHING_ERROR))
        else:
            await _run_checks(tcx, table, plan, runnable, out)
    except asyncio.CancelledError as e:
        stopped = e
        why = hooks.stop_text() if hooks.stop_text is not None else STOPPED
        done = sum(1 for c in result.checks if c["state"] == "done")
        rest = _close_open(result, plan, out, "cancelled", f"{why}，未检查")
        result.error = STOPPED_TABLE.format(why=why, done=done, rest=rest)
    except SnapshotTampered:
        raise
    except Exception as e:  # noqa: BLE001 - 一张表出了意外不拖垮别的表：照实记下，做完的项照常写
        logger.exception("剖析表 %s 时出错", table)
        line = _brief(e)
        _close_open(result, plan, out, "error", TABLE_ERROR.format(line=line))
        result.status, result.error = "failed", TABLE_ERROR.format(line=line)
    result.current = None
    result.row_estimate = cx.sizes.get(table)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    await db(_record, cx.source.id, table, out, result, actor, hooks.recorded)
    if stopped is not None:
        raise stopped
    return result


def _settle_status(result: TableResult) -> None:
    """表的状态：skipped、failed 的不变；有运行原因没查成的项是 partial，否则 ok。"""
    if result.status in ("skipped", "failed"):
        return
    result.status = "partial" if result.unfinished else "ok"


async def _record(session: AsyncSession, source_id: str, table: str, out: _TableOutcome | None, result: TableResult,
                  actor: str | None, recorded: Callable[[AsyncSession, TableResult], Awaitable[None]] | None) -> None:
    """整张表结束：把结论并进目录（out 为 None 的不写：表结构里没有、视图），定下表的状态，再交给 recorded（同一个会话）。
    修改时间取这一次写入的时间。

    目录和 recorded 写的这张表的结果在同一个事务里提交：进程在中间没了，两样都没写进去，下次启动收尾时这张表做完的项
    照实记「没写进目录」（catalog_profile_jobs._close_tasks），不会出现目录写了、剖析记录却说没写的情形。"""
    if out is not None:
        try:
            await _write(session, source_id, table, out, result, at=catalog.now_iso(), actor=actor)
        except Exception as e:  # noqa: BLE001 - 这一张写不进去不影响别的表
            logger.exception("剖析结果写入表 %s 的数据目录时出错", table)
            await session.rollback()
            result.error = WRITE_ERROR.format(line=_brief(e))
            result.status = "failed"
            result.findings = []
            result.added = result.updated = result.removed = 0
    _settle_status(result)
    if recorded is not None:
        await recorded(session, result)
    await session.commit()


async def _write(session: AsyncSession, source_id: str, table: str, out: _TableOutcome, result: TableResult, *,
                 at: str, actor: str | None) -> None:
    """并入并写入（乐观锁）。撞上别人刚改过就重读重并，最多三次；三次仍冲突这张表记 failed。写进事务、不提交（见 _record）。"""
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
            written = await catalog.write_entry(session, source_id, table, notes, if_version=version, actor=actor,
                                                commit=False)
        except catalog.CatalogConflict:
            continue
        result.version = written.version
        return
    result.error = CONFLICT_ERROR
    result.status = "failed"
    result.findings = []
    result.added = result.updated = result.removed = 0


def ensure_enabled(row: Any, settings: ProfileSettings | None = None) -> ProfileSettings:
    """这个数据源能不能剖析：数据源停用了、或者没开启数据剖析，抛 ProfileDisabled（message 给人看）。
    返回要用的设置（不给就按 row.options 读）。建任务之前先调它：开关是第一道门；任务每张表开始前也按它重新看一遍。"""
    settings = settings or profile_settings(getattr(row, "options", None))
    name = getattr(row, "name", "") or ""
    if getattr(row, "enabled", True) is False:
        raise ProfileDisabled(f"数据源「{name}」已停用，无法进行数据剖析。请先在数据源设置中启用", code="datasource_inactive")
    if not settings.enabled:
        raise ProfileDisabled(f"数据源「{name}」未开启数据剖析。剖析会对业务库发查询，请先在数据源设置中开启数据剖析")
    return settings


# ==========================================================================
# 核对模型提议的有效行过滤：排除了多少行
# ==========================================================================

#: 有效行过滤核对的说法（写进这一项的 note；前面是模型给的依据）
FILTER_CHECKED = "剖析核对：排除 {share} 的行（共 {rows} 行）"
FILTER_SAMPLED = "剖析核对：按存储顺序取前 {rows} 行统计，排除 {share} 的行"
FILTER_SAMPLED_EDGE = "剖析核对：按存储顺序取前 {rows} 行统计，{what}，请人工核对"
FILTER_UNCHECKED = "没有核对排除比例（{why}）"
FILTER_NOT_ENABLED = "数据源没有开数据剖析"


@dataclass(frozen=True)
class FilterCheck:
    """核对一条有效行过滤的结果。

    keep：收不收（排除 0% 或 100% 的不收：前者什么都没筛掉，后者把所有行都筛掉了，多半写错了）；note：写进这一项 note
    的那句（剖析核对的排除比例，或者没有核对的原因）；rows / excluded：数到的行数和其中被排除的行数（没核对是 None）；
    sampled：表太大或不知道多大，只数了前若干行；dropped：不收的原因（记日志用）。"""

    keep: bool
    note: str
    rows: int | None = None
    excluded: int | None = None
    sampled: bool = False
    dropped: str | None = None


def excluded_share(excluded: int, rows: int) -> str:
    """排除比例写成百分数：留一位小数（整数不带小数）；不到 0.1% 的写「不足 0.1%」，不是全部的不四舍五入成 100%。"""
    if rows <= 0:
        return "0%"
    pct = excluded * 100 / rows
    if 0 < pct < 0.1:
        return "不足 0.1%"
    if 99.9 < pct < 100:
        return "超过 99.9%"
    return f"{pct:.1f}".rstrip("0").rstrip(".") + "%"


def _unchecked(why: str) -> FilterCheck:
    return FilterCheck(True, FILTER_UNCHECKED.format(why=why))


def _unsafe_literal(value: str) -> bool:
    """拼不进核对查询的字符串取值：查询层用 SQLAlchemy 的 text() 执行，它会把字符串里的「:名字」当成绑定参数；
    反斜杠在 MySQL 上读法不定、守卫会整条拒掉；控制字符不该出现（同 sql_literal 的规矩）。"""
    return "\\" in value or bool(_CONTROL.search(value)) or bool(_BIND_LIKE.search(value))


async def check_valid_filter(source: Any, table: str, meta: dict[str, Any] | None, condition: str, *,
                             settings: ProfileSettings | None = None) -> FilterCheck:
    """开了数据剖析的源：数一数这条有效行过滤排除了多少行（模型起草时提议的那条，data/catalog_draft.py 调）。从不抛异常。

    经单条查询的入口（run_single_query：guard.check(readonly=True)、单条时限）发一条查询：只取条件里用到的列，
    先取前 max(sample_size, max_scan_rows) 行（大表不做整表扫描），再数总行数和不满足条件的行数（条件为 NULL 也算排除，
    和 WHERE 一样）。数到的不满前那么多行，就是整张表。

    - 排除 0% 或 100% 的不收（keep 为假）；只数了前若干行的照收，写明「请人工核对」；
    - 没开剖析、数据源停用、条件里有遮罩的列、视图、取值拼不进查询、查询失败或超时：照收，note 写没有核对的原因。
    """
    from app.data import sqlcheck

    options = getattr(source, "options", None)
    settings = settings or profile_settings(options)
    if not settings.enabled:
        return _unchecked(FILTER_NOT_ENABLED)
    if getattr(source, "enabled", True) is False:
        return _unchecked("数据源已停用")
    kind = getattr(source, "kind", None)
    parsed = sqlcheck.parse_valid_filter(condition, meta, kind=kind, table=table)
    if parsed.problem:
        return FilterCheck(False, parsed.problem, dropped=parsed.problem)
    masked = {c.lower() for c in data_engine.masked_columns(options)}
    if hit := [c for c in parsed.columns if c.lower() in masked]:
        return _unchecked(f"条件里的 {'、'.join(hit)} 在数据源设置中被遮罩，不取数")
    if (meta or {}).get("is_view"):
        return _unchecked("这张表是视图，剖析可能触发整个视图的计算")
    from sqlglot import exp

    if any(n.is_string and _unsafe_literal(str(n.this)) for n in parsed.expression.find_all(exp.Literal)):
        return _unchecked("条件里的取值含有冒号或反斜杠，拼不进核对查询")
    try:
        dialect = SqlDialect(kind)
    except ValueError as e:
        return _unchecked(str(e))
    cap = max(int(settings.sample_size), int(settings.max_scan_rows))
    # 和存进目录的是同一条（catalog._model_facts 存的就是这个写法）：列换成真名、按方言加引号，别的照原文
    cond = sqlcheck.canonical_valid_filter(parsed, quote=dialect.quote)
    cols = ", ".join(dialect.quote(c) for c in parsed.columns)
    inner = dialect.limit(f"SELECT {cols} FROM {dialect.table(meta, table)}", cap)
    sql = f"SELECT COUNT(*) AS n, SUM(CASE WHEN {cond} THEN 0 ELSE 1 END) AS x FROM ({inner}) s"
    try:
        res = await run_single_query(source, sql, settings=settings, kind=kind)
    except QueryFailed as e:
        return _unchecked(e.detail.removesuffix("，已跳过"))
    except SnapshotTampered as e:
        return _unchecked(str(e))
    row = res.rows[0] if res.rows else [None, None]
    rows, excluded = _as_int(row[0]), _as_int(row[1]) if len(row) > 1 else None
    if not rows:
        return _unchecked("表里还没有数据")
    excluded = excluded or 0
    sampled = rows >= cap
    counted = f"{rows:,}"
    if excluded in (0, rows):
        what = "没有排除任何一行" if excluded == 0 else "所有行都被排除了"
        if sampled:
            return FilterCheck(True, FILTER_SAMPLED_EDGE.format(rows=counted, what=what), rows, excluded, True)
        return FilterCheck(False, FILTER_CHECKED.format(share=excluded_share(excluded, rows), rows=counted), rows,
                           excluded, False, dropped=what)
    share = excluded_share(excluded, rows)
    note = (FILTER_SAMPLED if sampled else FILTER_CHECKED).format(share=share, rows=counted)
    return FilterCheck(True, note, rows, excluded, sampled)



__all__ = ["CODES_MAX", "EMPTY_ERROR", "FILTER_CHECKED", "FILTER_NOT_ENABLED", "FILTER_SAMPLED", "FILTER_UNCHECKED",
           "FilterCheck", "IN_CHUNK", "LEGACY_KEYS", "MAX_CODE_LEN", "MAX_LITERAL_LEN", "NOTE_PREFIX", "NOTHING_ERROR",
           "PROFILE_OPTION", "PlannedCheck", "ProfileContext", "ProfileDisabled", "ProfileSettings", "ProfileSkip",
           "QueryFailed", "SourceRunner", "SqlDialect", "TableBudget", "TableHooks", "TableResult", "TableSize",
           "UNFINISHED_REASONS", "VIEW_ERROR", "check_valid_filter", "ensure_enabled", "estimate_size", "excluded_share",
           "has_pending_relations", "make_context", "masked_of", "migrate_settings", "pending_relation_paths",
           "plan_checks", "profile_settings", "profile_settings_problem", "profile_settings_rejection",
           "profile_table", "run_single_query", "sql_literal", "summarize", "without_legacy"]
