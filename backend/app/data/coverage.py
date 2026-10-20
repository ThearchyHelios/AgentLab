"""查询区间里这张表有没有数据：运行时现查（查询结果整列是 0、助手搭图自查时用）。

**为什么要有它。** 部署上撞过：一份周报按 `event_date >= DATE_SUB(CURRENT_DATE, INTERVAL (WEEKDAY(CURRENT_DATE)+7) DAY)
AND event_date < …` 取上周、上上周的到访人数，那张表却只存最新一天（每天整表重灌），6 列全是 0，口径卡算出一排 0 和 0%，
周报照写「上周到访人数为 0 人」「周环比 0.00%」。这些 0 不是「没有到访」，是这段时间表里根本没有数据。剖析记过日期列的
最小、最大值，但对滚动表不可靠（剖析那天表里是前一天的数据，运行时已经换成当天），所以要运行时现查。

**做什么。** 两个入口共用一套：

- result_gaps：一次查询的结果里有「整列都是 0 或空」的数值列时（结果一行都没有的不归这里管），对每个这样的列，把 SQL
  收窄到算出这一列的那一项（去掉别的选择项、分组、HAVING、排序、取行数，同双盲复算差异说明的「收窄到这一格」，这里只做
  分析、不执行），取出它用到的表和时间谓词（列、比较符、界）；
- sql_gaps：助手搭图自查时，工具节点里写死的 SQL 还没跑、没有结果：对 SQL 里所有带时间谓词的表都查。

界是字面量的直接解析；按运行当天现算的（不含列引用的表达式：CURRENT_DATE、DATE_SUB(…)、date('now', …)……）用同一个数据源
跑一条 `SELECT 界1, 界2 …` 在库里算出来（Oracle 加 FROM DUAL）。这一列在这张表里的实际范围现查 `SELECT MIN(列), MAX(列)
FROM 表`（每条探查 PROBE_TIMEOUT_S 秒）；超时或失败退回剖析、取值观察记下的范围（facts，照实标「按 X 日的剖析」），都没有
就不判。日期按常见写法归一（DATE / DATETIME、'YYYY-MM-DD'、'YYYYMMDD'、8 位整数），认不出就不判；列是文本、界也是文本时
两边写法要一样（'20261012' 对 '2026-10-12' 按字符串比，结果和按日期比对不上），不一样的不判。区间和实际范围完全不重叠才算
「没有数据」；一列用到几段区间（本周减上周）时每一段都不重叠才算。

**和 sqlcheck 一样宁可漏报。** 所有探查都走数据源查询同一道只读守卫（data/guard.check + is_write），遮罩列上的不探查、不报
值；总耗时有上限（BUDGET_S）；解析不了、认不出、探查失败一律当没问题，出错不影响查询本身（同 tools/datasource._sql_checks）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.data.names import name_key

logger = logging.getLogger(__name__)

#: 一次检查（一份查询结果、一条自查的 SQL）所有探查加起来最多多少秒
BUDGET_S = 20.0
#: 每条探查查询的时限
PROBE_TIMEOUT_S = 10.0
#: 一次最多探查几张表的范围、几条按运行当天现算的界：再多就是 SQL 太复杂，不为它把库查一遍
MAX_RANGES = 8
MAX_BOUNDS = 24
#: 太长的 SQL 不分析（同 sqlcheck.MAX_SQL_CHARS）
MAX_SQL_CHARS = 20_000
#: 一条 no_data 里最多列几个结果列（计数照实）
MAX_COLUMNS = 12

#: 给人和模型看的说法
NO_DATA_MESSAGE = "{columns}查的是 {window}，但 {table} 的 {column} {data}：这些 0 不代表这段时间真的是 0，是这段时间表里没有数据{basis}"
NO_DATA_SQL_MESSAGE = "查的区间是 {window}，但 {table} 的 {column} {data}{basis}"
BASIS_PROFILE = "（现查表的范围没有成功，按 {day} 的剖析）"
BASIS_OBSERVED = "（现查表的范围没有成功，按 {day} 的取值观察）"
#: 写进口径卡、写作目录、双盲复算原因的那半句
NO_DATA_SHORT = "查询区间里这张表没有数据"

#: (表达式的类型, 方言) → 按运行当天现算的界：不认列、不认子查询，函数、字面量、INTERVAL 都可以
_COMPARE = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<=", "EQ": "="}
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "="}
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
                  r"(?:Z|[+-]\d{2}:?\d{2})?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


# ==========================================================================
# 日期的归一
# ==========================================================================


@dataclass(frozen=True)
class _Day:
    """归一后的一个日期：day 是哪一天，timed 是带不带一天里的时刻（00:00:00 不算），shape 是写法（iso / compact /
    number / date：库交回的是日期类型），kind 是库交回的类型（date / datetime / text / number，字面量按它的写法）。"""

    day: date
    timed: bool
    shape: str
    kind: str


def _as_day(value: Any, kind: str | None = None) -> _Day | None:
    """一个值归一成日期；认不出返回 None。kind 是库交回的列类型（run_query 的 column_types），字面量不给。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        return _Day(value.date(), bool(value.time().replace(microsecond=0) != datetime.min.time()), "date", "datetime")
    if isinstance(value, date):
        return _Day(value, False, "date", "date")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return None
        found = _COMPACT.match(str(int(value)))
        day = _ymd(found.group(1), found.group(2), found.group(3)) if found else None
        return _Day(day, False, "number", kind or "number") if day else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if found := _COMPACT.match(text):
        day = _ymd(found.group(1), found.group(2), found.group(3))
        return _Day(day, False, "compact", kind or "text") if day else None
    if found := _ISO.match(text):
        day = _ymd(found.group(1), found.group(2), found.group(3))
        if day is None:
            return None
        timed = any(int(found.group(i) or 0) for i in (4, 5, 6))
        shape = "date" if kind in ("date", "datetime") else "iso"
        return _Day(day, timed, shape, kind or "text")
    return None


def _ymd(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def _comparable(column: _Day, bound: _Day, *, bare: bool, read: str | None) -> bool:
    """库里拿这一列和这个界比，是不是按日期在比（我们的判断才对得上）：

    - 列外面套着函数（DATE(x)、STR_TO_DATE(x, …)）：比的是函数的结果，按日期算；
    - 列本身是日期类型：库把文本的界转成日期再比；
    - 界是库算出来的日期类型（DATE_SUB(CURRENT_DATE, …)）、列是文本：MySQL 把文本转成日期再比，别的库不一定（SQLite 的
      date('now') 交回的是文本，照下一条比写法），只认 MySQL；
    - 两边都是文本、都是数：写法得一样（'20261012' 对 '2026-10-12' 按字符串比，和按日期比对不上）。"""
    if not bare or column.kind in ("date", "datetime"):
        return True
    if bound.kind in ("date", "datetime"):
        return read == "mysql" and column.kind == "text"
    if column.kind == "text" and bound.kind == "text":
        return column.shape == bound.shape
    return column.kind == bound.kind == "number" and column.shape == bound.shape == "number"


# ==========================================================================
# 从 SQL 里认时间谓词
# ==========================================================================


@dataclass
class _Filter:
    """一段 SELECT 里对一张实表的一列的时间条件：table / column 是 sqlglot 的节点（探查时照它写 SQL），conds 是
    [(比较符, 界的节点, 列是不是光着的)]。key 按内容认（表、列、各个条件的写法）：几列各自收窄出来的同一段条件是同一个，
    判一次。"""

    key: tuple[Any, ...]
    table: Any
    table_name: str
    column: Any
    column_name: str
    conds: list[tuple[str, Any, bool]] = field(default_factory=list)


def _read(kind: str | None) -> str | None:
    from app.data.sqlcheck import dialect_of

    return dialect_of(kind)


def _parse(sql: str, read: str | None) -> Any:
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS or "{{" in sql:
        return None
    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    except Exception:  # noqa: BLE001 - 解析不了就不判
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


def _output_name(item: Any, read: str | None) -> str:
    from sqlglot import exp

    if isinstance(item, exp.Alias):
        return item.alias
    if isinstance(item, exp.Column):
        return item.name
    return item.sql(dialect=read)


def _same_name(a: str, b: str) -> bool:
    return "".join(a.split()).lower() == "".join(b.split()).lower()


def narrowed(tree: Any, column: str, read: str | None) -> Any:
    """收窄到算出 column 这一列的那一项：最外层 SELECT 只留它，分组、HAVING、排序、取行数去掉（只拿来认时间谓词，不执行，
    不用像双盲复算那样按这一行的分组值补等值条件）。认不准的（不是一段 SELECT、SELECT *、对不上或对上好几项）交回整条。"""
    from sqlglot import exp

    if not isinstance(tree, exp.Select):
        return tree
    items = list(tree.expressions)
    if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in items):
        return tree
    hits = [i for i in items if _same_name(_output_name(i, read), column)]
    if len(hits) != 1:
        return tree
    out = tree.copy()
    out.set("expressions", [hits[0].copy()])
    for arg in ("group", "having", "order", "limit", "offset", "qualify"):
        out.set(arg, None)
    return out


def _columns_in(node: Any) -> list[Any]:
    from sqlglot import exp

    return [c for c in node.find_all(exp.Column) if not isinstance(c.this, exp.Star)]


def _plain_bound(node: Any) -> bool:
    """界里没有列、子查询、聚合、窗口：字面量，或者按运行当天现算的表达式。"""
    from sqlglot import exp

    return not _columns_in(node) and not any(
        isinstance(n, (exp.Select, exp.Subquery, exp.AggFunc, exp.Window, exp.Placeholder, exp.Parameter))
        for n in node.walk())


def _timeish(subject: Any, column: Any, bounds: list[Any]) -> bool:
    """像是时间条件：列名像日期、时间，或者列套着日期函数、界里有日期函数、像日期的常量、按运行当天现算的写法。"""
    from app.data import sql_semantics as ss

    name = str(column.name or "")
    return bool(ss._temporal_name(name) or ss._timeish_name(name) or ss._datey(subject)
                or any(ss._datey(b) or ss._date_like_literal(b) or ss._now_based(b) for b in bounds))


def _condition(cond: Any) -> tuple[Any, Any, list[tuple[str, Any]], bool] | None:
    """一个条件是不是「一列 比 界」：(列, 列那一侧的表达式, [(比较符, 界)], 列是不是光着的)。BETWEEN、IN（常量列表）
    拆成几项；NOT、子查询、两边都有列的不算。"""
    from sqlglot import exp

    from app.data.sqlcheck import _bare_column, _unparen

    n = _unparen(cond)
    if n.args.get("negate"):
        return None
    if isinstance(n, exp.Between):
        subject, pairs = n.this, [(">=", n.args.get("low")), ("<=", n.args.get("high"))]
    elif isinstance(n, exp.In) and n.expressions and not n.args.get("query"):
        subject, pairs = n.this, [("in", e) for e in n.expressions]
    elif type(n).__name__ in _COMPARE and isinstance(n, exp.Binary):
        op = _COMPARE[type(n).__name__]
        left, right = n.this, n.expression
        if _columns_in(left) and not _columns_in(right):
            subject, pairs = left, [(op, right)]
        elif _columns_in(right) and not _columns_in(left):
            subject, pairs = right, [(_FLIP[op], left)]
        else:
            return None
    else:
        return None
    if subject is None or any(b is None or not _plain_bound(b) for _, b in pairs):
        return None
    cols = _columns_in(subject)
    if len(cols) != 1 or any(isinstance(x, (exp.AggFunc, exp.Select, exp.Window)) for x in subject.walk()):
        return None
    return cols[0], subject, pairs, _bare_column(subject) is not None


def _real_tables(select: Any, ctes: set[str]) -> dict[str, Any]:
    """这一段 FROM / JOIN 里的实表：{别名或表名的 name_key: 表节点}。CTE、子查询不算。"""
    from sqlglot import exp

    sources = [select.args.get("from_") or select.args.get("from")] + list(select.args.get("joins") or [])
    out: dict[str, Any] = {}
    for source in sources:
        table = getattr(source, "this", None)
        if isinstance(table, exp.Table) and table.name and name_key(table.name) not in ctes:
            out[name_key(table.alias_or_name)] = table
    return out


def _schema_columns(schema: Mapping[str, Any] | None, table: Any) -> set[str] | None:
    """表结构里这张表的列（name_key）；表结构里没有这张表为 None。"""
    tables = (schema or {}).get("tables") if isinstance(schema, Mapping) else None
    if not isinstance(tables, Mapping):
        return None
    for name, meta in tables.items():
        if name_key(str(name).rsplit(".", 1)[-1]) == name_key(table.name) and isinstance(meta, Mapping):
            return {name_key(str(c.get("name"))) for c in meta.get("columns") or [] if isinstance(c, Mapping)}
    return None


def filters(tree: Any, schema: Mapping[str, Any] | None = None) -> list[_Filter]:
    """语法树里各段 SELECT 的 WHERE 中，对实表的一列的时间条件，按（段、表、列）归在一起。"""
    from sqlglot import exp

    from app.data.sqlcheck import _conjuncts

    ctes = {name_key(c.alias_or_name) for c in tree.find_all(exp.CTE) if c.alias_or_name}
    found: dict[tuple[int, str, str], _Filter] = {}
    for index, select in enumerate(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None:
            continue
        tables = _real_tables(select, ctes)
        if not tables:
            continue
        for cond in _conjuncts(where.this):
            parsed = _condition(cond)
            if parsed is None:
                continue
            column, subject, pairs, bare = parsed
            if not _timeish(subject, column, [b for _, b in pairs]):
                continue
            if column.table:
                table = tables.get(name_key(column.table))
            elif len(tables) == 1:
                table = next(iter(tables.values()))
            else:
                owners = [t for t in tables.values()
                          if name_key(column.name) in (_schema_columns(schema, t) or set())]
                table = owners[0] if len(owners) == 1 else None
            if table is None:
                continue
            shown = ".".join(p for p in (table.catalog, table.db, table.name) if p)
            group = (index, name_key(shown), name_key(column.name))
            entry = found.setdefault(group, _Filter(group, table, shown, column, str(column.name)))
            entry.conds.extend((op, bound, bare) for op, bound in pairs)
    for entry in found.values():
        entry.key = (name_key(entry.table_name), name_key(entry.column_name),
                     tuple(sorted((op, bound.sql(), bare) for op, bound, bare in entry.conds)))
    return list(found.values())


# ==========================================================================
# 探查
# ==========================================================================


async def _probe(source: Any, sql: str) -> tuple[list[Any], list[str | None]] | None:
    """在数据源上跑一条探查查询：第一行的值、各列的类型（按位置）。走数据源查询同一道只读守卫；写语句、出错、超时都交回
    None。"""
    from app.data.engine import run_query
    from app.data.guard import QueryLimits, SqlRejected, check, is_write

    kind = (getattr(source, "kind", None) or "").lower()
    try:
        statement = check(sql, readonly=True, source_name=getattr(source, "name", ""), dialect=kind)
        if is_write(statement, dialect=kind):
            return None
        result = await run_query(source, statement, limits=QueryLimits(timeout_seconds=PROBE_TIMEOUT_S, max_rows=2))
    except (SqlRejected, Exception):  # noqa: BLE001 - 探查失败就当不知道
        logger.info("覆盖检查的探查查询没有成功（数据源 %s）", getattr(source, "name", ""), exc_info=True)
        return None
    if not result.rows:
        return None
    types = result.column_types or {}
    return list(result.rows[0]), [types.get(c) for c in result.columns]


def _select_sql(items: list[str], read: str | None) -> str:
    return f"SELECT {', '.join(items)}" + (" FROM DUAL" if read == "oracle" else "")


@dataclass
class Cache:
    """同一个数据源上探查过的：表里那一列的范围、按运行当天现算的界。助手自查一轮里查好几次，不重复查库。"""

    ranges: dict[tuple[str, str, str], Any] = field(default_factory=dict)
    bounds: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass
class _Range:
    """这一列在这张表里的范围。low / high 为 None 的是不知道那一头；empty 是表里这一列一个值都没有（现查出来的）。"""

    low: _Day | None
    high: _Day | None
    basis: str
    day: str | None = None
    empty: bool = False


#: 剖析事实（catalog_profile_jobs.last_facts 的一项）：表名 → 那一份，取不到为 None
Facts = Callable[[str], Awaitable[Mapping[str, Any] | None]]


async def _range(source: Any, flt: _Filter, read: str | None, cache: Cache, facts: Facts | None) -> _Range | None:
    """这一列在这张表里的范围：现查 MIN / MAX（同一个数据源、同一张表、同一列查一次），查不成退回剖析、取值观察（facts）。"""
    from sqlglot import exp

    key = (str(getattr(source, "name", "")), name_key(flt.table_name), name_key(flt.column_name))
    if key in cache.ranges:
        return cache.ranges[key]
    table = flt.table.copy()
    table.set("alias", None)
    ident = flt.column.this.copy() if isinstance(flt.column.this, exp.Identifier) \
        else exp.to_identifier(flt.column_name)
    column = exp.Column(this=ident)
    sql = (f"SELECT {exp.Min(this=column.copy()).sql(dialect=read)} AS lo, "
           f"{exp.Max(this=column.copy()).sql(dialect=read)} AS hi FROM {table.sql(dialect=read)}")
    found = await _probe(source, sql)
    out: _Range | None = None
    if found is not None:
        values, kinds = found
        low_raw, high_raw = (values + [None, None])[:2]
        kinds = (kinds + [None, None])[:2]
        if low_raw is None and high_raw is None:
            out = _Range(None, None, "live", empty=True)
        else:
            low, high = _as_day(low_raw, kinds[0]), _as_day(high_raw, kinds[1])
            out = _Range(low, high, "live") if low is not None and high is not None else None
    elif facts is not None:
        out = await _fallback(flt, facts)
    cache.ranges[key] = out
    return out


async def _fallback(flt: _Filter, facts: Facts) -> _Range | None:
    """现查不成：退回剖析记下的范围（date_ranges），再不然取值观察的最新日期（freshness，只知道最晚那一头）。"""
    try:
        found = await facts(flt.table_name)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(found, Mapping):
        return None
    wanted = name_key(flt.column_name)
    for item in found.get("date_ranges") or []:
        if isinstance(item, Mapping) and name_key(str(item.get("column") or "")) == wanted:
            lo, hi = _as_day(item.get("min")), _as_day(item.get("max"))
            if lo is not None and hi is not None:
                return _Range(lo, hi, "profile", day=str(found.get("at") or "")[:10] or None)
    observed = found.get("observed") if isinstance(found.get("observed"), Mapping) else {}
    for item in observed.get("freshness") or []:
        if isinstance(item, Mapping) and name_key(str(item.get("column") or "")) == wanted:
            hi = _as_day(item.get("max"))
            if hi is not None:
                return _Range(None, hi, "observed", day=str(item.get("at") or "")[:10] or None)
    return None


def _literal_day(node: Any) -> _Day | None:
    from sqlglot import exp

    from app.data.sqlcheck import _unparen

    node = _unparen(node)
    negative = False
    if isinstance(node, exp.Neg):
        node, negative = _unparen(node.this), True
    if not isinstance(node, exp.Literal) or negative:
        return None
    if node.is_string:
        return _as_day(str(node.this))
    try:
        value = int(str(node.this))
    except ValueError:
        return None
    return _as_day(value)


def _is_literal(node: Any) -> bool:
    from sqlglot import exp

    from app.data.sqlcheck import _unparen

    node = _unparen(node)
    if isinstance(node, exp.Neg):
        node = _unparen(node.this)
    return isinstance(node, exp.Literal)


async def _bounds(source: Any, nodes: list[Any], read: str | None, cache: Cache) -> dict[int, _Day | None]:
    """各个界归一成日期：字面量直接解析，别的（按运行当天现算的表达式）一条 SELECT 在库里算出来。{id(节点): 日期}。"""
    out: dict[int, _Day | None] = {}
    pending: dict[str, list[int]] = {}
    name = str(getattr(source, "name", ""))
    for node in nodes:
        if _is_literal(node):
            out[id(node)] = _literal_day(node)
            continue
        try:
            text = node.sql(dialect=read)
        except Exception:  # noqa: BLE001
            out[id(node)] = None
            continue
        if (name, text) in cache.bounds:
            out[id(node)] = cache.bounds[(name, text)]
        else:
            pending.setdefault(text, []).append(id(node))
    texts = list(pending)[:MAX_BOUNDS]
    found = await _probe(source, _select_sql([f"{t} AS b{i}" for i, t in enumerate(texts)], read)) if texts else None
    for i, text in enumerate(pending):
        day = None
        if found is not None and i < len(texts):
            values, kinds = found
            day = _as_day(values[i], kinds[i]) if i < len(values) and i < len(kinds) else None
        if i < len(texts):
            cache.bounds[(name, text)] = day
        for ident in pending[text]:
            out[ident] = day
    return out


@dataclass(frozen=True)
class Window:
    """一段时间条件的范围（按天，两头都含）。start / end 为 None 的是那一头没有限制。"""

    start: date | None
    end: date | None


def _window(conds: list[tuple[str, Any, bool]], days: Mapping[int, _Day | None], data: _Range,
            read: str | None) -> Window | None:
    """几个条件合成一段：下界取最晚的、上界取最早的；IN 列表取最早到最晚（中间没列到的日子也算进去，宁可判成有重叠）。
    有一个界认不出、或者和列比不起来，整段不判（None）。「>」「<」的界不带时刻时按天挪一天（> 2026-10-11 从 10-12 算起）；
    带时刻的不挪（那一天里还可能有数据，宁可判成有重叠）。"""
    lows: list[date] = []
    highs: list[date] = []
    points: list[date] = []
    sample = data.high or data.low
    for op, node, bare in conds:
        day = days.get(id(node))
        if day is None or (sample is not None and not _comparable(sample, day, bare=bare, read=read)):
            return None
        if op == "in":
            points.append(day.day)
            continue
        if op in (">=", "="):
            lows.append(day.day)
        elif op == ">":
            lows.append(day.day if day.timed else day.day + timedelta(days=1))
        if op in ("<=", "="):
            highs.append(day.day)
        elif op == "<":
            highs.append(day.day if day.timed else day.day - timedelta(days=1))
    if points:
        lows.append(min(points))
        highs.append(max(points))
    start = max(lows) if lows else None
    end = min(highs) if highs else None
    if start is None and end is None:
        return None
    if start is not None and end is not None and start > end:
        return None
    return Window(start, end)


def _disjoint(window: Window, data: _Range) -> bool:
    """区间和表里的范围完全不重叠（表里这一列一个值都没有也算）。"""
    if data.empty:
        return True
    if window.end is not None and data.low is not None and window.end < data.low.day:
        return True
    return window.start is not None and data.high is not None and window.start > data.high.day


# ==========================================================================
# 说法
# ==========================================================================


def _window_text(window: Window) -> str:
    if window.start is not None and window.end is not None:
        return window.start.isoformat() if window.start == window.end \
            else f"{window.start.isoformat()} 到 {window.end.isoformat()}"
    if window.start is not None:
        return f"{window.start.isoformat()} 起"
    return f"{window.end.isoformat()} 及以前"     # type: ignore[union-attr]


def _data_text(data: _Range) -> str:
    if data.empty:
        return "一个值都没有"
    if data.low is not None and data.high is not None:
        if data.low.day == data.high.day:
            return f"只有 {data.low.day.isoformat()} 这一天的数据"
        return f"只有 {data.low.day.isoformat()} 到 {data.high.day.isoformat()} 的数据"
    return f"最新只到 {data.high.day.isoformat()}"     # type: ignore[union-attr]


def _basis_text(data: _Range) -> str:
    if data.basis == "profile":
        return BASIS_PROFILE.format(day=data.day or "上次")
    if data.basis == "observed":
        return BASIS_OBSERVED.format(day=data.day or "上次")
    return ""


def _columns_text(columns: list[str]) -> str:
    head = f"「{columns[0]}」"
    return f"{head}等 {len(columns)} 列" if len(columns) > 1 else head


def _range_dict(data: _Range) -> list[str | None] | None:
    if data.empty:
        return None
    return [data.low.day.isoformat() if data.low else None, data.high.day.isoformat() if data.high else None]


def _window_dict(window: Window) -> list[str | None]:
    return [window.start.isoformat() if window.start else None, window.end.isoformat() if window.end else None]


# ==========================================================================
# 入口
# ==========================================================================


def zero_columns(payload: Mapping[str, Any]) -> list[str]:
    """查询结果里「整列都是 0 或空」的数值列（按 column_types 是数的、或者没记类型却全是数和空值的）。一行都没有的结果
    返回空列表：那归「没有查到」管，不归这里。"""
    from app.engine.expressions import numeric_text

    columns = [str(c) for c in payload.get("columns") or []]
    rows = payload.get("rows") or []
    if not columns or not isinstance(rows, list) or not rows:
        return []
    types = payload.get("column_types") if isinstance(payload.get("column_types"), Mapping) else {}
    out: list[str] = []
    for index, column in enumerate(columns):
        if types.get(column) not in (None, "number"):
            continue
        values = [row[index] if isinstance(row, (list, tuple)) and index < len(row)
                  else (row.get(column) if isinstance(row, Mapping) else None) for row in rows]

        def zero(value: Any) -> bool:
            if value is None:
                return True
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                return value == 0
            number = numeric_text(value) if isinstance(value, str) else None
            return number is not None and number == 0

        if all(zero(v) for v in values):
            out.append(column)
    return out


async def _judge(source: Any, flts: list[_Filter], read: str | None, cache: Cache, facts: Facts | None,
                 masked: set[str], deadline: float) -> dict[tuple[int, str, str], tuple[str, Window | None, _Range | None]]:
    """每段时间条件判一下：none（区间里表里没有数据）/ some（有重叠）/ unknown（判断不了），带上区间和范围。"""
    out: dict[tuple[int, str, str], tuple[str, Window | None, _Range | None]] = {}
    ranges: dict[tuple[str, str], _Range | None] = {}
    usable = [f for f in flts if name_key(f.column_name) not in masked]
    for flt in flts:
        if flt not in usable:
            out[flt.key] = ("unknown", None, None)
    for flt in usable:
        if time.monotonic() > deadline:
            out[flt.key] = ("unknown", None, None)
            continue
        rkey = (name_key(flt.table_name), name_key(flt.column_name))
        if rkey not in ranges:
            ranges[rkey] = await _range(source, flt, read, cache, facts) if len(ranges) < MAX_RANGES else None
        data = ranges[rkey]
        if data is None:
            out[flt.key] = ("unknown", None, None)
            continue
        days = await _bounds(source, [node for _, node, _ in flt.conds], read, cache)
        window = _window(flt.conds, days, data, read)
        if window is None:
            out[flt.key] = ("unknown", None, data)
        else:
            out[flt.key] = ("none" if _disjoint(window, data) else "some", window, data)
    return out


async def result_gaps(source: Any, sql: str, payload: Mapping[str, Any], *, facts: Facts | None = None,
                      masked: Iterable[str] = (), cache: Cache | None = None,
                      budget: float = BUDGET_S) -> list[dict[str, Any]]:
    """一份查询结果里，整列是 0 或空、而查询区间里这张表没有数据的那几列：[{columns, table, column, window, data_range,
    basis, basis_at?, message}]，同一张表、同一列、同一段区间的几列并成一条。没有问题（或者判断不了）返回空列表。从不抛异常。"""
    try:
        return await asyncio.wait_for(_result_gaps(source, sql, payload, facts, set(masked), cache or Cache(), budget),
                                      timeout=budget + PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("覆盖检查超过时限，本次跳过（数据源 %s）", getattr(source, "name", ""))
    except Exception:  # noqa: BLE001 - 检查不能让一次成功的查询变成失败
        logger.exception("覆盖检查出错，本次跳过（数据源 %s）", getattr(source, "name", ""))
    return []


async def _result_gaps(source: Any, sql: str, payload: Mapping[str, Any], facts: Facts | None, masked: set[str],
                       cache: Cache, budget: float) -> list[dict[str, Any]]:
    zeros = zero_columns(payload)
    if not zeros:
        return []
    read = _read(getattr(source, "kind", None))
    tree = _parse(sql, read)
    if tree is None:
        return []
    masked = {m.strip().lower() for m in masked if isinstance(m, str) and m.strip()}
    schema = getattr(source, "schema_cache", None)
    per_column = {c: filters(narrowed(tree, c, read), schema) for c in zeros if c.lower() not in masked}
    every = {f.key: f for flts in per_column.values() for f in flts}
    if not every:
        return []
    judged = await _judge(source, list(every.values()), read, cache, facts, masked, time.monotonic() + budget)
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for column, flts in per_column.items():
        verdicts = [judged.get(f.key) for f in flts]
        if not flts or any(v is None or v[0] != "none" for v in verdicts):
            continue
        for flt, (_, window, data) in zip(flts, verdicts):
            gkey = (name_key(flt.table_name), name_key(flt.column_name), window, data.basis if data else None)
            group = groups.setdefault(gkey, {"flt": flt, "window": window, "data": data, "columns": []})
            if column not in group["columns"]:
                group["columns"].append(column)
    out = []
    for group in groups.values():
        flt, window, data, columns = group["flt"], group["window"], group["data"], group["columns"]
        out.append({
            "columns": columns[:MAX_COLUMNS], "table": flt.table_name, "column": flt.column_name,
            "window": _window_dict(window), "data_range": _range_dict(data), "basis": data.basis,
            **({"basis_at": data.day} if data.day else {}),
            "message": NO_DATA_MESSAGE.format(columns=_columns_text(columns), window=_window_text(window),
                                              table=flt.table_name, column=flt.column_name,
                                              data=_data_text(data), basis=_basis_text(data)),
        })
    return out


async def sql_gaps(source: Any, sql: str, *, facts: Facts | None = None, masked: Iterable[str] = (),
                   cache: Cache | None = None, budget: float = BUDGET_S) -> list[dict[str, Any]]:
    """还没执行的一条 SQL（助手自查）：SQL 里每一段时间条件，区间和表里的范围完全不重叠的那几段：[{table, column,
    window, data_range, basis, basis_at?, message}]，同一张表、同一列、同一段区间只算一条。从不抛异常。"""
    try:
        return await asyncio.wait_for(_sql_gaps(source, sql, facts, set(masked), cache or Cache(), budget),
                                      timeout=budget + PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("自查的覆盖检查超过时限，本次跳过（数据源 %s）", getattr(source, "name", ""))
    except Exception:  # noqa: BLE001
        logger.exception("自查的覆盖检查出错，本次跳过（数据源 %s）", getattr(source, "name", ""))
    return []


async def _sql_gaps(source: Any, sql: str, facts: Facts | None, masked: set[str], cache: Cache,
                    budget: float) -> list[dict[str, Any]]:
    read = _read(getattr(source, "kind", None))
    tree = _parse(sql, read)
    if tree is None:
        return []
    masked = {m.strip().lower() for m in masked if isinstance(m, str) and m.strip()}
    flts = filters(tree, getattr(source, "schema_cache", None))
    if not flts:
        return []
    judged = await _judge(source, flts, read, cache, facts, masked, time.monotonic() + budget)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for flt in flts:
        verdict, window, data = judged.get(flt.key, ("unknown", None, None))
        key = (name_key(flt.table_name), name_key(flt.column_name), window)
        if verdict != "none" or window is None or data is None or key in seen:
            continue
        seen.add(key)
        out.append({"table": flt.table_name, "column": flt.column_name, "window": _window_dict(window),
                    "data_range": _range_dict(data), "basis": data.basis, **({"basis_at": data.day} if data.day else {}),
                    "message": NO_DATA_SQL_MESSAGE.format(window=_window_text(window), table=flt.table_name,
                                                          column=flt.column_name, data=_data_text(data),
                                                          basis=_basis_text(data))})
    return out


# ==========================================================================
# 读快照里记下的
# ==========================================================================


def no_data_of(snapshot: Any, column: Any) -> dict[str, Any] | None:
    """查询快照里记着 column 这一列「查询区间里这张表没有数据」的那一条（no_data）；没有为 None。按列名认（不分大小写、
    不计空白）。"""
    if not isinstance(snapshot, Mapping) or not isinstance(column, str):
        return None
    for item in snapshot.get("no_data") or []:
        if isinstance(item, Mapping) and any(isinstance(c, str) and _same_name(c, column)
                                              for c in item.get("columns") or []):
            return dict(item)
    return None


__all__ = ["BUDGET_S", "Cache", "NO_DATA_SHORT", "PROBE_TIMEOUT_S", "Window", "filters", "narrowed", "no_data_of",
           "result_gaps", "sql_gaps", "zero_columns"]
