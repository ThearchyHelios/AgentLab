"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），运行
对比要回答「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了），反方审稿要看自己
的查询和原查询的口径是否一致。三处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、注释、不加引号的名字的大小写都不算不同。
- facets：把一条查询拆成几个方面——用到的表、连接（类型、关联进来的表、ON 条件）、WHERE 的各个合取式、其中的
  时间条件（列、运算符、取值）、分组、聚合（函数、参数、是否去重）、HAVING、取哪几行（LIMIT、OFFSET、取前几行
  时的排序）。别名一律换成真实表名（条件里套着的子查询也是），等式、比较的两边排好顺序：o.paid_at >= '2026-09-01'
  和 '2026-09-01' <= orders.paid_at 是同一个条件。
- diff_facets：逐个方面比两份 facets，每处不同给一句中文说明。

**逐段收集。** CTE、子查询里的条件、聚合和取行也算进来：盲算可能把有效记录条件写在 CTE 里，原流程写在外层，按
「整条语句里有哪些条件」比才不会把同一个口径报成不同。逗号连接（以及 INNER JOIN 之外又写在 WHERE 里的等值关联）
写在 WHERE 里的列等式算作关联条件，不算筛选。条件里套着的子查询（EXISTS、IN (SELECT …)）既单算一段，也按它自己
那一段的来源整个写进外层那个条件：别名在子查询里定义，拿外层的来源去认，同一个条件只因别名不同就会被报成不同。

**各自起的名字不进比较。** 表的别名换成真实表名；CTE、FROM 里的子查询（含 LATERAL）没有真实表名可换，单看一条
语句时按它在语句里的位置编号（@1、@2…，从外往里、按第一次用到的先后），给人看时照写名字；条件里的子查询（IN、
EXISTS、标量子查询）选出来的列名不影响取值，比较时去掉。这些名字都是各自起的，双盲复算的盲算更是：按名字比，同一个
口径会被说成换了口径。

**两边的派生来源按结构配对。** 位置编号只看先后：一边多出一个排在前面的 CTE，或者内连接两边换了先后，编号就错开
了；可也不能干脆不看是哪一个（两个 CTE 都算出 total 时，外层用的是付款合计还是退款合计，正是要报的不同）。所以
diff_facets 先给两边的每个派生来源算一份结构标识（见 _Derived）：里面读到的实表（FROM / JOIN，往里套着的也算）、
选出来的各列怎么定义（规范化后的定义式，如 total 是 SUM(orders.amount)）、按什么分组、整段规范化后的写法，以及
起的名字；再按从严到宽的几级一对一地配（见 _align）：整段一样；读的表、各列定义、分组一样（里面的筛选条件可以不
同）；读的表和选出来的列名一样（某一列的算法可以不同）；读的表和名字一样；最后三级只在两边都只剩一个时才配：只是
读的表一样、只是名字一样、只是选出来的列名一样。同一级有几个都配得上时，先挑同名的，再挑里面的条件重合最多的，最后
才看位置编号的先后。哪一级都配不上的不配：一边多出来的 CTE 不会被拿去和另一边不相干的 CTE 凑成一对。配好之后，
第二条 SQL 的派生来源改用第一条里对应的那个编号（配不上的另起编号），按这个共同的编号重新分析一遍再逐个方面比：
比较的两边谁在前、关联条件的先后都按共同的编号排，内连接换了先后也排成同一个方向。连接里的内连接（含逗号连接、
CROSS JOIN）按涉及的来源和条件比，不按哪一侧是关联进来的；一边独有的一次内连接，关联进来的那一侧另一边也有时，报
的是它接上的另一头（FROM stores JOIN orders、另一边只查 orders：多出来的是 stores）。

仍做不到的：两边给 CTE、子查询选出来、外层按名字引用的列起的名字不同（一边叫 total、一边叫 sum_amt），算两列；读同
一张表、各列定义也一样的两个派生来源，两边都改了里面的条件、又各起了名字时，只能按条件的重合和先后配，可能配反；
读的表、选出来的列名、名字全换了的两个派生来源不配对，外层按它们的列写的条件照报成不同；同一张表、同一个 CTE 自己
关联自己时两个别名比较用的写法一样，p1.total > p2.total 写反了认不出；连接上做不到的见 _Compare.joins。facets 单独
返回的 key 仍是位置编号；不是 facets 算出来的 Facets（没记原文）diff_facets 也只能按位置编号比。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就带 parse_error 返回，从不抛
异常。没加表前缀、又有好几张表可能有这一列的，照写列名（给了表结构才去对）。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from itertools import zip_longest
from typing import Any

from app.data import catalog
from app.data.names import name_key
from app.data.sqlcheck import _bare_column, _conjuncts, _parents, _unparen, dialect_of

logger = logging.getLogger(__name__)

#: 规范文本里字面量的占位符
PLACEHOLDER = "?"
#: 太长的 SQL 不做语法分析（normalize_sql 退回逐词规范化，facets 带 parse_error）：解析耗时随长度增长
MAX_SQL_CHARS = 100_000
#: 每段 SELECT 在语法树上记段号的 meta 键。条件、表达式要复制一份再改写，复制出来的子查询靠段号找回原来那一段
#: 算好的来源（别名、外层）：复制品没有父节点，自己找不到外层
_SEGMENT = "sql_semantics_segment"
#: 派生来源（引用 CTE 的表和 CTE 的定义、FROM / JOIN 里的子查询）在语法树上记比较用编号的 meta 键（见
#: _Statement._number_derived、_Statement.relabel）。复制出来的条件里套着子查询时，靠它把 CTE 名、子查询的别名
#: 换成编号
_DERIVED = "sql_semantics_derived"
#: 不写编号的写法（render 的 anon 模式）里派生来源一律写成这个：算结构标识（_Derived）时用，和编号无关
_ANON = "@"


# ==========================================================================
# 解析
# ==========================================================================


def _read(dialect: str | None) -> str | None:
    """数据源类型（sqlite / postgresql / mysql …）或 sqlglot 的方言名 → 解析用的方言。认不出的按 sqlglot 的通用读法。"""
    found = dialect_of(dialect)
    if found or not dialect:
        return found
    from sqlglot.dialects.dialect import Dialect

    name = dialect.strip().lower()
    try:
        Dialect.get_or_raise(name)
    except Exception:  # noqa: BLE001 - 不认识的方言名：按通用读法
        return None
    return name


def _parse_problem(error: Exception) -> str:
    """sqlglot 的报错 → 一句给人看的话。原文是英文、还带终端的高亮控制符，只取出错的位置。"""
    errors = getattr(error, "errors", None) or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    line, col = first.get("line"), first.get("col")
    if isinstance(line, int) and isinstance(col, int):
        return f"SQL 无法解析（第 {line} 行第 {col} 列附近）"
    return "SQL 无法解析"


def _parse(sql: Any, read: str | None) -> tuple[Any, str | None]:
    """(语法树, None) 或 (None, 为什么分析不了)。只认单条查询。"""
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not sql.strip():
        return None, "SQL 为空"
    text = sql.strip()
    if len(text) > MAX_SQL_CHARS:
        return None, f"SQL 超过 {MAX_SQL_CHARS} 个字符，没有做语法分析"
    try:
        statements = [s for s in sqlglot.parse(text, read=read) if s is not None]
    except Exception as e:  # noqa: BLE001 - 语法错、方言特性、sqlglot 不支持的写法
        return None, _parse_problem(e)
    if not statements:
        return None, "SQL 为空"
    if len(statements) > 1:
        return None, f"一次只能分析一条 SQL，这里有 {len(statements)} 条"
    if not isinstance(statements[0], exp.Query):
        return None, "不是查询语句（只分析 SELECT、WITH 开头的查询）"
    return statements[0], None


# ==========================================================================
# 规范化：字面量换成占位符
# ==========================================================================


@dataclass(frozen=True)
class NormalizedSql:
    """规范文本和字面量。text 相同就是同一个模板，literals 是这一次填进去的值（SQL 写法，字符串带引号）。

    parse_error 不为空时 text 是逐词规范化的结果（关键字转大写、不加引号的名字转小写、字面量换成占位符）：
    同一个模板只改了字面量的两条 SQL 照样得到同一个 text，只是空白、括号之外的写法差异认不出来。
    """

    text: str
    literals: tuple[str, ...] = ()
    parse_error: str | None = None

    def same_shape(self, other: "NormalizedSql") -> bool:
        """两条 SQL 是不是同一个模板（只有字面量不同也算）。"""
        return self.text == other.text

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "literals": list(self.literals)}
        if self.parse_error:
            out["parse_error"] = self.parse_error
        return out


def _is_literal(token: Any) -> bool:
    """字符串（含 N'…'、x'…'、$$…$$ 这些变体）和数字。"""
    name = token.token_type.name
    return name == "NUMBER" or name.endswith("STRING")


def _tokens(text: str, read: str | None) -> list[Any]:
    from sqlglot.dialects.dialect import Dialect

    return list(Dialect.get_or_raise(read).tokenize(text))


def normalize_sql(sql: str, dialect: str | None = None) -> NormalizedSql:
    """字面量换成占位符后的规范文本 + 字面量。

    先按方言解析、重新生成一遍（去掉注释、统一关键字和空白、按方言的规则转换不加引号的名字的大小写），再把生成的
    文本切成词，字符串和数字换成「?」：字面量按规范文本里出现的顺序排，不受语法树里子句存放顺序的影响。解析
    不了就直接对原文逐词规范化，带上 parse_error。从不抛异常。
    """
    read = _read(dialect)
    root, problem = _parse(sql, read)
    if root is not None:
        try:
            from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

            canonical = normalize_identifiers(root.copy(), dialect=read).sql(dialect=read, comments=False)
            pieces: list[str] = []
            literals: list[str] = []
            pos = 0
            for token in _tokens(canonical, read):
                if _is_literal(token):
                    pieces += [canonical[pos:token.start], PLACEHOLDER]
                    literals.append(canonical[token.start:token.end + 1])
                    pos = token.end + 1
            pieces.append(canonical[pos:])
            return NormalizedSql("".join(pieces), tuple(literals))
        except Exception:  # noqa: BLE001 - 生成、切词出错：退回逐词规范化，照实标出
            logger.warning("SQL 规范化失败，退回逐词规范化", exc_info=True)
            problem = "SQL 的写法无法规范化"
    return _by_tokens(sql if isinstance(sql, str) else "", read, problem)


def _by_tokens(text: str, read: str | None, problem: str | None) -> NormalizedSql:
    """解析不了的 SQL 逐词规范化：关键字转大写，不加引号的名字按 name_key，带引号的名字原样，字面量换成占位符。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 引号、括号没配对：只能把空白压成一个
        return NormalizedSql(" ".join(text.split()), (), problem or "SQL 无法解析")
    words: list[str] = []
    literals: list[str] = []
    for token in tokens:
        raw = text[token.start:token.end + 1]
        if _is_literal(token):
            words.append(PLACEHOLDER)
            literals.append(raw)
        elif token.token_type.name == "VAR":
            words.append(name_key(token.text))
        elif token.token_type.name == "IDENTIFIER":
            words.append(raw)
        else:
            words.append(token.text.upper())
    return NormalizedSql(" ".join(words), tuple(literals), problem)


# ==========================================================================
# 各个方面
# ==========================================================================

#: 一列：(表, 列)。表是给人看的真实表名（CTE、子查询写它的名字），认不出是哪张表时为 None
ColumnRef = tuple[str | None, str]


@dataclass(frozen=True)
class Part:
    """一个条件、一个分组键、一个聚合参数……

    key 用来比较：别名换成真实表名，CTE、子查询写编号（facets 单独返回的是位置编号，diff_facets 里是两边按结构配好
    对的共同编号，见模块说明），表名、列名按 name_key，等式和比较的两边排好顺序；loose 同 key 但不写实表的表名（派生
    来源的编号照写：它不是表名，换了表也还是配好的那一个），两边查的表换了一张（orders 换成 orders_archive）时拿它
    比其余各方面，免得同一个条件只因表名不同被报成不同。text 给人看：好几张表都有的列写「表.列」（真实表名，不是
    别名），其余只写列名；full 一律写表名，两边的 text 一样、key 却不同时改用它说明。excludes 只在 WHERE 的「排除」
    写法上有：<>、NOT、NOT IN、IS NOT NULL 去掉否定之后排除的是什么（IS NOT NULL 写成「列 为空」）。column_keys 和
    columns 一一对应，是各列比较用的写法（实表.列；CTE、子查询的列写成「编号.列」）：两边对同一列的不同条件靠它配成
    一对说。
    """

    key: str
    text: str
    columns: tuple[ColumnRef, ...] = ()
    excludes: str | None = None
    loose: str = ""
    full: str = ""
    column_keys: tuple[str, ...] = ()

    @staticmethod
    def plain(key: str, text: str) -> "Part":
        """没有列的一项（表名、COUNT(*) 的「*」）：比较用的写法都是 key，给人看的都是 text。"""
        return Part(key=key, text=text, loose=key, full=text)


@dataclass(frozen=True)
class JoinFacet:
    """一次连接。kind：inner / left / right / full / cross / semi / anti / natural。table 是关联进来的那一侧，other
    是另一侧：关联条件里出现的别的表（给人看的真实表名）；没有条件的（CROSS JOIN）是在它之前出现的各个来源。on 是
    关联条件的合取式排好序后用 AND 连起来的写法，没有条件是 None。

    sides 是这次连接涉及的来源（比较用的写法，排好序）：关联进来的那一侧，加上关联条件里直接引用的这一段的来源（不
    透过 CTE 找实表）；没有条件的加上它之前出现的各个来源。内连接、CROSS JOIN 两边换个先后意思不变（FROM paid JOIN
    refunded 和 FROM refunded JOIN paid），diff_facets 比它们时按 sides 和条件比，不按哪一侧是关联进来的。"""

    kind: str
    table: Part
    on: Part | None = None
    other: tuple[str, ...] = ()
    sides: tuple[str, ...] = ()


@dataclass(frozen=True)
class TimePredicate:
    """WHERE 里的一个时间条件。op：= / <> / > / >= / < / <= / between / in。value 是另一侧给人看的写法：
    BETWEEN 是「低 AND 高」，IN 是「(…)」。"""

    column: Part
    op: str
    value: str
    condition: Part


@dataclass(frozen=True)
class Aggregate:
    """一个聚合。func 是函数名的小写（sum / count / avg / min / max …）；arg 是参数，COUNT(*)、COUNT(1) 的 arg
    是「*」，去重聚合是 DISTINCT 里面的表达式；text 是整个聚合给人看的写法。窗口函数不算。"""

    func: str
    arg: Part
    distinct: bool
    text: str


@dataclass(frozen=True)
class LimitFacet:
    """一段查询取哪几行。count、offset 是行数和跳过行数的写法（没写为 None，OFFSET 0 也算没写）；order 是这一段的
    排序键 (Part, 是否降序)：只取前几行时，按什么排决定了取到的是哪几行。scope 是哪一段：None 是最外层，CTE 写它的
    名字，FROM 里的子查询写它的别名（没有别名写「子查询」）。条件里套着的子查询不单算：它整个写在那个条件里比。
    scope_key 是这一段所属派生来源的编号（和 Part.key 里的同一套编号；最外层、UNION 的某一支这类不是派生来源整段的
    为 None）：diff_facets 按它把两边同一个 CTE、子查询里的取行配成一对。"""

    count: str | None
    offset: str | None = None
    order: tuple[tuple[Part, bool], ...] = ()
    scope: str | None = None
    scope_key: str | None = None


@dataclass(frozen=True)
class Facets:
    """一条查询的各个方面。每一项都按在 SQL 里出现的先后排、按 key 去重；parse_error 不为空时其余都是空的。

    limit 是最外层 LIMIT 的行数（写法）；limits 是各段取哪几行（最外层在前，见 LimitFacet），只有写了 LIMIT 或
    OFFSET 的段才有。

    sources 是各段 FROM / JOIN 里的来源（实表、CTE、子查询；key 是比较用的写法，派生来源是编号，text 是给人看的
    名字），按出现的先后、按 key 去重；条件里套着的子查询（IN (SELECT … FROM users)）的来源不算，它们只筛行，不是
    连接的一头。它本身不单独比较：一边独有的一次内连接要说清多出来的是哪个来源时，拿另一边的 sources 对照（见
    _Compare.added）。

    _origin 是 facets 记下的 (原文, 表结构)，不进比较、不进 as_dict：diff_facets 把两边的派生来源按结构配好对之后，
    拿它按共同的编号重新分析一遍（见模块说明）。
    """

    dialect: str | None = None
    tables: tuple[Part, ...] = ()
    joins: tuple[JoinFacet, ...] = ()
    where: tuple[Part, ...] = ()
    time: tuple[TimePredicate, ...] = ()
    group_by: tuple[Part, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    having: tuple[Part, ...] = ()
    limit: str | None = None
    limits: tuple[LimitFacet, ...] = ()
    sources: tuple[Part, ...] = ()
    parse_error: str | None = None
    _origin: tuple[str, Mapping[str, Any] | None] | None = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value) if not f.name.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def facets(sql: str, dialect: str | None = None, *, schema: Mapping[str, Any] | None = None) -> Facets:
    """把一条查询拆成各个方面。从不抛异常：解析不了、或者分析时出了意外，返回带 parse_error 的 Facets。

    schema 是数据源的 schema_cache 形状（{"tables": {表名: {"columns": [{"name": …}]}}}），可以不给。给了的话，
    几张表连在一起、列又没加表前缀时按它认列是哪张表的；不给就照写列名，比较时可能把同一列当成两列。

    返回的 Facets 记着原文和表结构（_origin）：diff_facets 要把两边的派生来源按结构配好对再重新分析。
    """
    read = _read(dialect)
    root, problem = _parse(sql, read)
    if root is None:
        return Facets(dialect=read, parse_error=problem)
    try:
        return replace(_Statement(root, read, schema).facets(), _origin=(sql, schema))
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能连累调用方：当作分析不了，记下来修
        logger.warning("SQL 语义分析失败", exc_info=True)
        return Facets(dialect=read, parse_error="SQL 的结构无法分析")


# ---- 语法树上的小工具

#: 比较：(运算符的写法, 两边对调之后的类型)
_COMPARE: dict[str, tuple[str, str]] = {"EQ": ("=", "EQ"), "NEQ": ("<>", "NEQ"), "GT": (">", "LT"),
                                        "GTE": (">=", "LTE"), "LT": ("<", "GT"), "LTE": ("<=", "GTE")}
#: 连接类型的写法（diff_facets 里用）
_JOIN_WORDS = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL JOIN",
               "cross": "CROSS JOIN", "semi": "SEMI JOIN", "anti": "ANTI JOIN", "natural": "NATURAL JOIN"}
#: 日期、时间函数（sqlglot 把各方言的写法归成这些类型：strftime、DATE_FORMAT 都是 TimeToStr）
_DATE_FUNCS = ("CurrentDate", "CurrentDatetime", "CurrentTimestamp", "CurrentTime", "Date", "Datetime", "DateAdd",
               "DateSub", "DateDiff", "DateTrunc", "TimestampTrunc", "DatetimeTrunc", "TimeToStr", "StrToDate",
               "StrToTime", "TsOrDsToDate", "TsOrDsToTimestamp", "TsOrDsAdd", "DateStrToDate", "TimeStrToTime",
               "UnixToTime", "Interval", "Extract", "DateFromParts", "LastDay", "Year", "Month", "Day", "Week",
               "Quarter", "DayOfWeek")
#: sqlglot 不认识、原样当普通函数的日期函数名
_DATE_NAMES = frozenset({"date", "datetime", "strftime", "julianday", "now", "today", "to_date", "to_char",
                         "date_format", "unixepoch", "curdate", "sysdate", "getdate"})
#: 像日期、时间的字符串：2026-09-01、2026-09、2026/9/1、2026-09-01 08:00:00
_DATE_TEXT = re.compile(r"^\s*\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\s*$")
#: 中文列名里像日期、时间的写法（英文列名按 catalog.is_date_column）
_CN_TIME = re.compile(r"(?:日期|时间|时刻|年月|月份)$|^(?:年|月|日|周|日期|时间)$")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _nested(node: Any, stop: Any) -> bool:
    """node 在 stop 底下的某个子查询里（不属于 stop 这一段）。"""
    from sqlglot import exp

    return any(isinstance(p, (exp.Select, exp.Subquery)) for p in _parents(node, stop=stop))


def _own_columns(node: Any) -> list[Any]:
    """node 里属于这一段的列（不进嵌套的子查询），不含 t.*。"""
    from sqlglot import exp

    if node is None:
        return []
    found = [node] if isinstance(node, exp.Column) else \
        [c for c in node.find_all(exp.Column) if not _nested(c, node)]
    return [c for c in found if c.name and not c.is_star]


def _inner_queries(query: Any, skip: Iterable[Any] = ()) -> list[Any]:
    """query 里直接套着的查询（不再往里找），按出现的先后：条件、SELECT 列表里的子查询，UNION 的各支，子查询里面
    那段。WITH 里的 CTE 定义不算（在用到它的地方才算），skip 里的节点整个跳过。"""
    from sqlglot import exp

    skipped = {id(n) for n in skip}
    found = []
    stack = list(reversed(list(query.iter_expressions())))
    while stack:
        node = stack.pop()
        if id(node) in skipped or isinstance(node, exp.With):
            continue
        if isinstance(node, exp.Query):
            found.append(node)
            continue
        stack.extend(reversed(list(node.iter_expressions())))
    return found


def _named_output(select: Any) -> bool:
    """这段 SELECT 选出来的列会被别处按名字引用：它（或者它所在的 UNION）是 FROM / JOIN 里的子查询、CTE 的定义。
    IN、EXISTS、标量子查询只看取值，选出来的列叫什么不影响口径。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.SetOperation)):
        node = node.parent
    return isinstance(node.parent, (exp.From, exp.Join, exp.CTE, exp.Lateral))


def _alias(item: Any) -> str:
    """FROM / JOIN 项的别名（没有别名的实表、CTE 是它的名字）；表函数这类认不出的来源是空串。LATERAL 的别名
    挂在 Lateral 节点上，不在里面的子查询上。"""
    from sqlglot import exp

    return item.alias_or_name if isinstance(item, (exp.Table, exp.Subquery, exp.Lateral)) else ""


def _derived_body(item: Any) -> Any:
    """FROM / JOIN 项是子查询（含 LATERAL 子查询）时，里面那段查询；别的（实表、CTE、表函数）返回 None。"""
    from sqlglot import exp

    if isinstance(item, exp.Lateral):
        item = item.this
    return item.this if isinstance(item, exp.Subquery) else None


def _branches(query: Any) -> list[Any]:
    """一段查询的各支 SELECT，按出现的先后：UNION 这类拆开，外面套的括号去掉；认不出的返回空列表。"""
    from sqlglot import exp

    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return _branches(query.this) + _branches(query.expression)
    return [query] if isinstance(query, exp.Select) else []


def _own_nodes(select: Any) -> Iterator[Any]:
    """一段 SELECT 自己的节点：不进 CTE、子查询（它们是别的段）。"""
    from sqlglot import exp

    stack = [select]
    while stack:
        node = stack.pop()
        yield node
        children = [c for c in node.iter_expressions()
                    if not isinstance(c, (exp.Select, exp.Subquery, exp.CTE, exp.With))]
        stack.extend(reversed(children))


def _datey(node: Any) -> bool:
    """node 里有日期函数、转成日期的 CAST，或者像日期的字符串。"""
    from sqlglot import exp

    kinds = tuple(k for k in (getattr(exp, n, None) for n in _DATE_FUNCS) if isinstance(k, type))
    for n in node.walk():
        if isinstance(n, kinds):
            return True
        if isinstance(n, exp.Anonymous) and str(n.name).lower() in _DATE_NAMES:
            return True
        if isinstance(n, exp.Cast) and n.to is not None and any(w in n.to.this.name for w in ("DATE", "TIME")):
            return True
        if isinstance(n, exp.Literal) and n.is_string and _DATE_TEXT.match(str(n.this)):
            return True
    return False


def _temporal_name(name: str) -> bool:
    """列名像日期、时间：英文按 catalog.is_date_column 的命名规则（paid_at、order_date），中文看结尾（支付日期）。"""
    return catalog.is_date_column(name, None) or bool(_CN_TIME.search(name))


def _join_kind(join: Any) -> str:
    side = str(join.args.get("side") or "").lower()
    kind = str(join.args.get("kind") or "").lower()
    if side in ("left", "right", "full"):
        return side
    if join.args.get("method"):
        return "natural"
    if kind in ("semi", "anti"):
        return kind
    if kind == "cross" or not (join.args.get("on") is not None or join.args.get("using")):
        return "cross"
    return "inner"


def _dedupe(items: Iterable[Any], key: Any) -> tuple[Any, ...]:
    seen: dict[Any, Any] = {}
    for item in items:
        seen.setdefault(key(item), item)
    return tuple(seen.values())


class _Sources(dict):
    """一段 SELECT 的来源 {别名的 name_key: _Source}。outer 是外层那段的来源：相关子查询里引用外层的别名（o.id）
    从这里找。"""

    outer: "_Sources | None" = None


@dataclass
class _Source:
    """一段 SELECT 的一个来源。kind：table（实表）/ derived（CTE、子查询、LATERAL 子查询）/ other（表函数、认不出的
    别名）。"""

    kind: str
    name: str                       # 给人看：表名（有库名就带上）、CTE 名、子查询（含 LATERAL）的别名
    key: str                        # 比较用：实表是 name_key，派生来源是编号（见 _Statement._number_derived）
    idents: tuple[Any, ...] = ()    # 表名各段的 Identifier：给人看的写法照原样写回去
    query: Any = None               # derived：里面那段查询


@dataclass(frozen=True)
class _Derived:
    """一个派生来源（CTE、FROM / JOIN 里的子查询、LATERAL 子查询）的结构标识，和起的名字、位置编号无关的部分都在
    这里，diff_facets 拿它把两边的派生来源配成对（见 _align）。各项都用不写编号的写法（render 的 anon 模式：实表
    写表名，往里套着的派生来源一律写「@」），所以两边写法一样就相同，不受先后、名字影响。"""

    number: int                             # 位置编号（@N 的 N）：别的都分不出时才看先后
    name: str                               # CTE 名、子查询的别名（name_key），没有为空：辅助线索
    tables: tuple[str, ...]                 # 里面读到的实表：各支 FROM / JOIN，往里套着的也算；排好序
    columns: tuple[tuple[str, str], ...]    # 选出来的各列 (列名, 规范化后的定义式)，SELECT * 记成 ("*", "*")
    group: tuple[str, ...]                  # 分组键，排好序
    conditions: frozenset[str]              # 里面各段 WHERE、HAVING 的合取式：同一级配得上好几个时比重合
    body: str                               # 整段查询规范化后的写法

    @property
    def names(self) -> tuple[str, ...]:
        """选出来的列名（外层按名字引用它们）。"""
        return tuple(name for name, _ in self.columns)


#: 派生来源配对的各级，从严到宽：(这一级比什么, 是否只在两边都只剩一个时才配)。比什么返回 None 表示这一级没有
#: 可比的（没有名字、没读实表），不配
_ALIGN_LEVELS: tuple[tuple[Any, bool], ...] = (
    (lambda d: (d.body, d.columns), False),                                 # 整段一样
    (lambda d: (d.tables, d.columns, d.group), False),                      # 读的表、各列定义、分组一样
    (lambda d: (d.tables, d.names), False),                                 # 读的表、选出来的列名一样
    (lambda d: (d.tables, d.name) if d.name else None, False),              # 读的表、名字一样
    (lambda d: d.tables or None, True),                                     # 只是读的表一样
    (lambda d: d.name or None, True),                                       # 只是名字一样（里面换了表）
    (lambda d: d.names if any(n != "*" for n in d.names) else None, True),  # 只是选出来的列名一样
)


def _align(xs: list[_Derived], ys: list[_Derived]) -> dict[int, int]:
    """两条语句的派生来源配对：{第二条里的位置编号: 第一条里的位置编号}，配不上的不在里面。

    按 _ALIGN_LEVELS 从严到宽一级一级地配，每一级一对一：严的先配走，宽的只收拾剩下的。同一级几对都配得上时，先配
    同名的，再配里面的条件重合最多的，最后按位置编号的先后（几个一模一样的派生来源按先后一一对应）；这一级所有
    配得上的对一起排，不是左边挨个挑，免得前一个挑走后一个更合适的。最后三级证据弱，只在两边各只剩一个这样的时才
    配，免得猜错。哪一级都配不上的就不配：一边多出来的 CTE（盲算先筛一遍 VIP 用户）和另一边不相干的 CTE 凑成一对，
    会把外层不相干的条件说成「关联条件不同」。
    """
    rest_x, rest_y = sorted(xs, key=lambda d: d.number), sorted(ys, key=lambda d: d.number)
    pairs: dict[int, int] = {}
    for view, unique in _ALIGN_LEVELS:
        candidates = []
        for x in rest_x:
            seen = view(x)
            if seen is None:
                continue
            found = [y for y in rest_y if view(y) == seen]
            if unique and (len(found) != 1 or sum(1 for z in rest_x if view(z) == seen) != 1):
                continue
            candidates += [((y.name != x.name, -len(y.conditions & x.conditions), x.number, y.number), x, y)
                           for y in found]
        for _, x, y in sorted(candidates, key=lambda c: c[0]):
            if x.number not in pairs.values() and y.number not in pairs:
                pairs[y.number] = x.number
        rest_x = [x for x in rest_x if x.number not in pairs.values()]
        rest_y = [y for y in rest_y if y.number not in pairs]
    return pairs


class _Statement:
    """一条语句的分析：CTE、实表、每段 SELECT 的来源，以及把表达式写成 key / text 的规则。"""

    def __init__(self, root: Any, read: str | None, schema: Mapping[str, Any] | None) -> None:
        from sqlglot import exp

        self.root = root
        self.read = read
        #: 各段 SELECT（原树上的节点），下标就是记在 meta 里的段号
        self.segments: list[Any] = []
        for select in root.find_all(exp.Select, bfs=False):
            select.meta[_SEGMENT] = len(self.segments)
            self.segments.append(select)
        self.ctes: dict[str, Any] = {}
        #: CTE 的定义（exp.CTE 节点），和 ctes 同一个键：编号也记在定义上，定义写进条件时靠它换掉名字
        self._cte_defs: dict[str, Any] = {}
        for cte in root.find_all(exp.CTE):
            if cte.alias:
                self.ctes.setdefault(name_key(cte.alias), cte.this)
                self._cte_defs.setdefault(name_key(cte.alias), cte)
        self._number_derived()
        tables: dict[str, _Source] = {}
        for table in root.find_all(exp.Table, bfs=False):
            if isinstance(table.this, exp.Identifier) and table.name and not self._is_cte(table):
                source = self._table(table)
                tables.setdefault(source.key, source)
        self.tables = list(tables.values())
        #: 整条语句只用到一张实表：给人看的写法一律不写表名
        self.single = len(self.tables) <= 1
        self.columns: dict[str, set[str]] = {}
        for name, meta in ((schema or {}).get("tables") or {}).items():
            if isinstance(meta, Mapping):
                self.columns[name_key(str(name))] = {name_key(str(c.get("name"))) for c in meta.get("columns") or []
                                                     if isinstance(c, Mapping) and c.get("name")}
        self._memo: dict[int, _Sources] = {}
        self._shared: set[str] | None = None

    # ---- 来源

    def _is_cte(self, table: Any) -> bool:
        return not table.text("db") and not table.text("catalog") and name_key(table.name) in self.ctes

    @staticmethod
    def _items(select: Any) -> list[Any]:
        """一段 SELECT 的 FROM、JOIN 项，按出现的先后。"""
        first = select.args.get("from_")
        return ([first.this] if first is not None else []) + [j.this for j in select.args.get("joins") or []]

    def _number_derived(self) -> None:
        """给派生来源（引用 CTE 的表、FROM / JOIN 里的子查询和 LATERAL 子查询）编比较用的 key：@1、@2…，记在节点的
        meta 上。

        CTE 名、子查询的别名是各自起的（双盲复算的盲算更是），拿它作 key，同一个口径只因起的名字不同就被报成不同；
        拿里面那段查询的写法作 key 也不行：里面差一个条件，外层按它的列写的条件就全跟着变，同一处不同报好几遍，还会
        出现两边各说一句「只保留 total > 100 的行」的自相矛盾。所以单看一条语句时按位置编：从最外层往里一层一层地
        编，同一层按出现的先后；CTE 在第一次用到的地方编号，在哪儿用到都是这一个编号。写成 CTE 还是 FROM 里的子查询、
        CTE 定义的先后都不影响编号，里层多一段、少一段也不挪动外层的编号。两条语句之间编号错开（一边多一个 CTE、
        内连接换了先后）由 diff_facets 按结构配对之后改成共同的编号（见 relabel）。
        """
        from sqlglot import exp

        ctes: dict[str, int] = {}
        count = 0
        #: 编了号的派生来源 [(位置编号, 起的名字, 里面那段查询)]，按编号排：diff_facets 按它们的结构配对（见 derived）
        self._derived: list[tuple[int, str, Any]] = []
        #: 记了编号的节点 {id: (节点, 位置编号)}：配好对之后按共同的编号改写（见 relabel）
        self._marked: dict[int, tuple[Any, int]] = {}

        def mark(node: Any, number: int) -> None:
            node.meta[_DERIVED] = f"@{number}"
            self._marked[id(node)] = (node, number)

        def visit(select: Any) -> list[Any]:
            """给这一段还没编号的 FROM / JOIN 项编号，返回要接着往里看的查询（子查询、第一次用到的 CTE 的定义）。"""
            nonlocal count
            inner = []
            for item in self._items(select):
                body = _derived_body(item)
                if body is not None:
                    if _DERIVED not in item.meta:
                        count += 1
                        mark(item, count)
                        self._derived.append((count, _alias(item), body))
                        inner.append(body)
                elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) and item.name \
                        and self._is_cte(item):
                    name = name_key(item.name)
                    if name not in ctes:
                        count += 1
                        ctes[name] = count
                        mark(self._cte_defs[name], count)
                        self._derived.append((count, self._cte_defs[name].alias, self.ctes[name]))
                        inner.append(self.ctes[name])
                    mark(item, ctes[name])
            return inner

        queue: deque[Any] = deque([self.root])
        while queue:
            query = queue.popleft()
            items: list[Any] = []
            if isinstance(query, exp.Select):
                queue.extend(visit(query))
                items = self._items(query)
            queue.extend(_inner_queries(query, skip=items))
        # 没走到的（没用到的 CTE 里的、表函数参数里的）按出现的先后补上：每个派生来源都得有编号
        for select in self.root.find_all(exp.Select, bfs=False):
            visit(select)
        # 没用到的 CTE 也编上号（排在最后）：外层不引用它，可它照样要参加两边的配对。两边都定义了付款、退款两个 CTE，
        # 一边关联付款的、一边关联退款的，没用到的那个不参加的话，两边各剩一个只有列名对得上的，会被凑成一对
        for name, cte in self._cte_defs.items():
            if name not in ctes:
                count += 1
                ctes[name] = count
                mark(cte, count)
                self._derived.append((count, cte.alias, self.ctes[name]))

    def relabel(self, ids: Mapping[int, str]) -> None:
        """派生来源改用 ids 给的编号（{位置编号: 比较用的编号}，没给的照旧写位置编号）。diff_facets 把两边的派生
        来源配好对之后，第二条语句里配上的写第一条里对应的编号，两边比较用的写法才对得上。算好的来源、共有的列名
        都带着旧编号，跟着清掉重算。"""
        for node, number in self._marked.values():
            node.meta[_DERIVED] = ids.get(number, f"@{number}")
        self._memo.clear()
        self._shared = None

    def derived(self) -> list[_Derived]:
        """各个派生来源的结构标识（见 _Derived），按位置编号排。"""
        from sqlglot import exp

        out = []
        for number, name, body in self._derived:
            columns: list[tuple[str, str]] = []
            group: list[str] = []
            conditions: set[str] = set()
            for i, select in enumerate(_branches(body)):
                scope = self.sources(select)
                for clause in (select.args.get("where"), select.args.get("having")):
                    if clause is not None:
                        conditions.update(self.render(self.canonical(c, scope), scope, "anon")
                                          for c in _conjuncts(clause.this))
                if i:
                    continue                # UNION 选出来的列名、分组按第一支的算
                for item in select.selects:
                    if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star):
                        columns.append(("*", "*"))
                        continue
                    target = item.this if isinstance(item, exp.Alias) else item
                    columns.append((name_key(item.alias_or_name),
                                    self.render(self.canonical(target, scope), scope, "anon")))
                if (clause := select.args.get("group")) is not None:
                    selects, aliases = select.selects, self._aliases(select)
                    group += [self.render(self.canonical(self._target(g, selects, aliases), scope), scope, "anon")
                              for g in clause.expressions]
            out.append(_Derived(number=number, name=name_key(name) if name else "",
                                tables=tuple(sorted(self._row_tables(body, frozenset()))),
                                columns=tuple(sorted(columns)), group=tuple(sorted(group)),
                                conditions=frozenset(conditions),
                                body=self.render(self.canonical(body, {}), {}, "anon")))
        return out

    def _row_tables(self, query: Any, seen: frozenset[str]) -> set[str]:
        """query 的行从哪些实表来：各支 FROM / JOIN 里的实表，往里套着的子查询、CTE 也算。条件里的子查询不算（它只
        决定留哪些行，算进来的话一边多一个 IN (SELECT … FROM users) 的筛选，结构标识就整个变了）。seen 是这条路上
        已经进过的 CTE：递归 CTE 引用自己时不再往里走。"""
        from sqlglot import exp

        out: set[str] = set()
        for select in _branches(query):
            for item in self._items(select):
                body = _derived_body(item)
                if body is not None:
                    out |= self._row_tables(body, seen)
                elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) and item.name:
                    if not self._is_cte(item):
                        out.add(self._table(item).key)
                    elif (name := name_key(item.name)) not in seen:
                        out |= self._row_tables(self.ctes[name], seen | {name})
        return out

    @classmethod
    def reparse(cls, f: Facets) -> "_Statement | None":
        """按 facets 记下的原文、表结构重新分析一遍（见 Facets._origin）；没记、解析不了返回 None。"""
        if f._origin is None:
            return None
        sql, schema = f._origin
        root, _ = _parse(sql, f.dialect)
        return cls(root, f.dialect, schema) if root is not None else None

    @staticmethod
    def _table(table: Any) -> _Source:
        from sqlglot import exp

        idents = tuple(i for i in (table.args.get("catalog"), table.args.get("db"), table.this)
                       if isinstance(i, exp.Identifier))
        return _Source("table", ".".join(i.name for i in idents), ".".join(name_key(i.name) for i in idents),
                       tuple(i.copy() for i in idents))

    def sources(self, select: Any) -> _Sources:
        """{别名的 name_key: 来源}。没有别名的实表按表名；认不出的来源按位置编号。"""
        from sqlglot import exp

        if id(select) in self._memo:
            return self._memo[id(select)]
        out = _Sources()
        for i, node in enumerate(self._items(select)):
            alias = _alias(node)
            slot = name_key(alias) if alias else f"#{i}"
            if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and node.name:
                if self._is_cte(node):
                    out[slot] = _Source("derived", node.name, node.meta.get(_DERIVED, slot), (node.this.copy(),),
                                        self.ctes[name_key(node.name)])
                else:
                    out[slot] = self._table(node)
            elif (body := _derived_body(node)) is not None:
                out[slot] = _Source("derived", alias or "子查询", node.meta.get(_DERIVED, slot),
                                    (exp.to_identifier(alias),) if alias else (), body)
            else:
                out[slot] = _Source("other", alias, slot)
        self._memo[id(select)] = out
        parent = select.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        out.outer = self.sources(parent) if parent is not None else None
        return out

    def _exposes(self, source: _Source, column: str) -> bool:
        """这个来源可能有这一列：实表看表结构（没给表结构就当可能有），CTE、子查询看它选出来的列。"""
        from sqlglot import exp

        if source.kind == "table":
            return source.key not in self.columns or name_key(column) in self.columns[source.key]
        if source.kind == "derived" and isinstance(source.query, exp.Select):
            return any(isinstance(s, exp.Star) or (isinstance(s, exp.Column) and s.is_star)
                       or name_key(s.alias_or_name) == name_key(column) for s in source.query.selects)
        return True

    def locate(self, column: Any, sources: dict[str, _Source]) -> tuple[str | None, _Source | None]:
        """列 → (来源的别名键, 来源)。别名在这一段找不到的，到外层找（相关子查询引用外层），再找不到按「别的来源」
        照写（别名写错了）；没加前缀、又有不止一个来源可能有它的，返回 (None, None)。"""
        qualifier = column.table
        if qualifier:
            slot = name_key(qualifier)
            if slot in sources:
                return slot, sources[slot]
            outer = getattr(sources, "outer", None)
            while outer is not None:
                if slot in outer:
                    return None, outer[slot]        # 外层的来源：不算这一段的关联
                outer = outer.outer
            return None, _Source("other", qualifier, slot, (column.args["table"].copy(),))
        hits = [(slot, s) for slot, s in sources.items() if self._exposes(s, column.name)]
        return hits[0] if len(hits) == 1 else (None, None)

    def through(self, source: _Source | None, column: str, depth: int = 0) -> tuple[_Source | None, str]:
        """CTE、子查询只是把一张表的列原样透出来（SELECT *，或者直接选出这一列）时，换成那张表的列：
        原流程直接查 orders、盲算先在 CTE 里筛一遍再查，算的是同一列。"""
        from sqlglot import exp

        if source is None or source.kind != "derived" or depth > 3 or not isinstance(source.query, exp.Select):
            return source, column
        inner = self.sources(source.query)
        if len(inner) != 1:
            return source, column
        only = next(iter(inner.values()))
        for item in source.query.selects:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star):
                return self.through(only, column, depth + 1)
            target = _unparen(item.this if isinstance(item, exp.Alias) else item)
            if name_key(item.alias_or_name) == name_key(column) and isinstance(target, exp.Column):
                return self.through(only, target.name, depth + 1)
        return source, column

    def resolve(self, column: Any, sources: dict[str, _Source]) -> tuple[_Source | None, str]:
        return self.through(self.locate(column, sources)[1], column.name)

    def shared(self) -> set[str]:
        """好几张表都有的列名（name_key）：这条语句里出现时挂在不止一张表上（orders.order_id、refunds.order_id），
        或者表结构里不止一张用到的表有它。给人看时只有这些列写表名，其余只写列名，读起来和原 SQL 一样短。"""
        from sqlglot import exp

        if self._shared is None:
            owners: dict[str, set[str]] = {}
            for select in self.root.find_all(exp.Select, bfs=False):
                sources = self.sources(select)
                for node in _own_nodes(select):
                    if isinstance(node, exp.Column) and node.name and not node.is_star:
                        source, name = self.resolve(node, sources)
                        if source is not None:
                            owners.setdefault(name_key(name), set()).add(source.key)
            used = [t.key for t in self.tables]
            self._shared = {name for name, keys in owners.items()
                            if len(keys) > 1 or sum(1 for t in used if name in self.columns.get(t, ())) > 1}
        return self._shared

    # ---- 写成 key / text

    @staticmethod
    def _owner(node: Any) -> Any:
        """node 往上最近的一段 SELECT（带段号的，node 本身是也算）；没有就是 None：node 在调用方那一段里。"""
        from sqlglot import exp

        while node is not None:
            if isinstance(node, exp.Select) and node.meta.get(_SEGMENT) is not None:
                return node
            node = node.parent
        return None

    def scope(self, node: Any, default: dict[str, _Source]) -> dict[str, _Source]:
        """node 所在那一段的来源。条件里套着的子查询（EXISTS、IN (SELECT …)）是另一段：别名在那一段里定义，相关
        子查询还要到外层找。复制出来的子查询带着段号，按段号找回原树上那一段算好的来源；不在子查询里就是 default。"""
        owner = self._owner(node)
        return default if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])

    def render(self, node: Any, sources: dict[str, _Source], mode: str = "text") -> str:
        """五种写法：key、loose（见 Part）、anon 比较用，名字按 name_key；key、anon 写实表的表名，loose 不写；CTE、
        子查询 key 和 loose 写编号，anon 一律写「@」（算结构标识用，和编号无关，见 _Derived）。text、full 给人看
        （见 Part），text 只在好几张表都有这一列时写真实表名，full 一律写（关联条件说的就是两张表各出哪一列，用 full）。

        node 里套着的子查询逐段改写：每一列按它所在那一段的来源认，子查询里的表去掉别名（列已经写成了真实表名）。
        子查询里拿两张表的列互相比的（相关子查询），给人看时和关联条件一样写表名。比较用的写法里各自起的名字也去掉
        （见 _drop_names）。
        """
        from sqlglot import exp

        copy = node.copy()
        groups: dict[int, tuple[Any, list[Any]]] = {}
        for col in copy.find_all(exp.Column):
            if col.name and not col.is_star:
                owner = self._owner(col)
                groups.setdefault(id(owner), (owner, []))[1].append(col)
        for owner, cols in groups.values():
            scope = sources if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])
            found = [(col, *self.resolve(col, scope)) for col in cols]
            here = mode
            if owner is not None and mode == "text" and len({s.key for _, s, _ in found if s is not None}) > 1:
                here = "full"
            for col, source, name in found:
                self._write(col, source, name, here)
        for table in copy.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier) and self._owner(table) is not None:
                table.set("alias", None)
                if mode in ("key", "loose", "anon"):
                    for arg in ("catalog", "db", "this"):
                        ident = table.args.get(arg)
                        if isinstance(ident, exp.Identifier):
                            table.set(arg, exp.to_identifier(name_key(ident.name)))
        if mode in ("key", "loose", "anon"):
            self._drop_names(copy, numbered=mode != "anon")
        return copy.sql(dialect=self.read, comments=False)

    @staticmethod
    def _drop_names(copy: Any, *, numbered: bool) -> None:
        """比较用的写法里抹掉套着的子查询里各自起的名字（就地改）：引用 CTE 的表、FROM 里的子查询（含 LATERAL）、
        子查询里自带的 CTE 定义写编号（numbered 为假时写「@」；引用它们的列已经照这样写了）；只看取值的子查询（IN、
        EXISTS、标量子查询）去掉选出来的列名。FROM 里的子查询、CTE 定义选出来的列名留着：外层按这个名字引用它们，
        去掉了就分不出外层用的是哪一列。复制出来的那段本身没有父节点，认不出是 FROM 里的子查询，它选出来的列名也会
        去掉（_Derived.body 另记了各列，见 derived）。"""
        from sqlglot import exp

        for node in copy.find_all(exp.Table, exp.Subquery, exp.Lateral, exp.CTE):
            derived = node.meta.get(_DERIVED)
            if not derived:
                continue
            mark = exp.to_identifier(derived if numbered else _ANON)
            if isinstance(node, exp.Table):
                for arg in ("catalog", "db", "alias"):
                    node.set(arg, None)
                node.set("this", mark)
            elif isinstance(node.args.get("alias"), exp.TableAlias):
                node.args["alias"].set("this", mark)
            else:
                node.set("alias", exp.TableAlias(this=mark))
        for select in copy.find_all(exp.Select):
            if not _named_output(select):
                select.set("expressions", [e.this if isinstance(e, exp.Alias) else e for e in select.expressions])

    def _write(self, col: Any, source: _Source | None, name: str, mode: str) -> None:
        """按 render 的写法改写一列（就地改）。"""
        from sqlglot import exp

        quoted = isinstance(col.this, exp.Identifier) and bool(col.this.args.get("quoted"))
        if mode in ("key", "loose", "anon"):
            if source is None:
                owner = None
            elif source.kind == "derived":
                owner = _ANON if mode == "anon" else source.key
            else:
                owner = None if mode == "loose" else source.key
            col.set("this", exp.to_identifier(name_key(name)))
            col.set("table", exp.to_identifier(owner) if owner else None)
            col.set("db", None)
            col.set("catalog", None)
            return
        col.set("this", exp.Identifier(this=name, quoted=quoted or not _PLAIN_NAME.match(name)))
        if source is None or (source.kind != "other" and mode != "full"
                              and (self.single or name_key(name) not in self.shared())):
            col.set("table", None)
            col.set("db", None)
            col.set("catalog", None)
        else:
            *heads, last = source.idents or (exp.to_identifier(source.name),)
            col.set("table", last.copy())
            col.set("db", heads[-1].copy() if heads else None)
            col.set("catalog", heads[-2].copy() if len(heads) > 1 else None)

    def canonical(self, node: Any, sources: dict[str, _Source]) -> Any:
        """比较的两边排好顺序（列在左、字面量在右；两边都有列的按 _rank 排），IN 的取值排好序。返回新的树。
        套着的子查询里的比较按它那一段的来源排：拿外层的来源去认子查询的别名，排出来的先后就取决于别名怎么起。"""
        from sqlglot import exp

        def fix(n: Any) -> Any:
            found = _COMPARE.get(type(n).__name__)
            if found is not None and isinstance(n, exp.Binary):
                left, right = n.this, n.expression
                lc, rc = bool(_own_columns(left)), bool(_own_columns(right))
                here = self.scope(n, sources) if lc and rc else sources
                if (rc and not lc) or (lc and rc and self._rank(left, here) > self._rank(right, here)):
                    return getattr(exp, found[1])(this=right, expression=left)
            elif isinstance(n, exp.In) and n.expressions and not n.args.get("query") \
                    and not any(_own_columns(e) for e in n.expressions):
                n.set("expressions", sorted(n.expressions, key=lambda e: e.sql(dialect=self.read)))
            return n

        return node.transform(fix)

    def _rank(self, node: Any, sources: dict[str, _Source]) -> tuple[str, str]:
        """比较的两边谁排前面：先按不写编号的写法（实表的列、派生来源的列分得开），分不出（两个派生来源的同名列）
        再按编号。diff_facets 重新分析时用的是两边按结构配好对的共同编号（见 relabel）：p.total > r.total 和换了
        关联先后、位置编号对调的同一句排出来是同一个方向，配对之前的位置编号决定不了先后。"""
        return self.render(node, sources, "anon"), self.render(node, sources, "key")

    def part(self, node: Any, sources: dict[str, _Source], *, excludes: bool = False) -> Part:
        refs: dict[tuple[str | None, str], str] = {}
        for col in _own_columns(node):
            source, name = self.resolve(col, sources)
            refs.setdefault((source.name if source is not None else None, name),
                            f"{source.key if source is not None else ''}.{name_key(name)}")
        full = self.render(node, sources, "full")
        # 拿两张表的列互相比的（相关子查询里的 o.id = r.order_id）和关联条件一样，给人看也写表名
        across = len({table for table, _ in refs}) > 1
        return Part(key=self.render(node, sources, "key"), text=full if across else self.render(node, sources),
                    columns=tuple(refs), excludes=self._excludes(node, sources) if excludes else None,
                    loose=self.render(node, sources, "loose"), full=full, column_keys=tuple(refs.values()))

    def _excludes(self, node: Any, sources: dict[str, _Source]) -> str | None:
        """排除写法去掉否定之后排除的是什么：a <> 1 → a = 1；NOT IN、NOT LIKE、NOT BETWEEN 同理；IS NOT NULL → 「a 为空」。
        各方言把否定放的位置不一样（NOT 包一层，或者节点上带 negate），两种都认。"""
        from sqlglot import exp

        n = _unparen(node)
        if isinstance(n, exp.NEQ):
            return self.render(exp.EQ(this=n.this.copy(), expression=n.expression.copy()), sources)
        inner = None
        if isinstance(n, exp.Not):
            inner = _unparen(n.this).copy()
        elif n.args.get("negate"):
            inner = n.copy()
            inner.set("negate", None)
        if inner is None:
            return None
        if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
            return f"{self.render(inner.this, sources)} 为空"
        return self.render(inner, sources)

    # ---- 各个方面

    def facets(self) -> Facets:
        from sqlglot import exp

        joins: list[JoinFacet] = []
        where: list[Part] = []
        time: list[TimePredicate] = []
        group: list[Part] = []
        aggregates: list[Aggregate] = []
        having: list[Part] = []
        used: list[Part] = []
        for select in self.root.find_all(exp.Select, bfs=False):
            sources = self.sources(select)
            if select is self.root or not self._in_condition(select):
                used += [Part.plain(s.key, s.name) for s in sources.values()]
            drafts = self._join_drafts(select, sources)
            clause = select.args.get("where")
            for cond in _conjuncts(clause.this) if clause is not None else []:
                canon = self.canonical(cond, sources)
                if self._joins_on_where(canon, sources, drafts):
                    continue
                where.append(self.part(canon, sources, excludes=True))
                if found := self._time(canon, sources):
                    time.append(found)
            joins += [self._join(d, sources) for d in drafts]
            group += self._group(select, sources)
            aggregates += self._aggregates(select, sources)
            having += self._having(select, sources)
        return Facets(
            dialect=self.read,
            tables=tuple(Part.plain(s.key, s.name) for s in self.tables),
            joins=_dedupe(joins, lambda j: (j.kind, j.table.key, j.on.key if j.on else None)),
            where=_dedupe(where, lambda p: p.key),
            time=_dedupe(time, lambda t: t.condition.key),
            group_by=_dedupe(group, lambda p: p.key),
            aggregates=_dedupe(aggregates, lambda a: (a.func, a.arg.key, a.distinct)),
            having=_dedupe(having, lambda p: p.key),
            limit=self._limit(),
            limits=tuple(self._limits()),
            sources=_dedupe(used, lambda p: p.key),
        )

    def _join_drafts(self, select: Any, sources: dict[str, _Source]) -> list[dict[str, Any]]:
        from sqlglot import exp

        first = select.args.get("from_")
        first_alias = _alias(first.this) if first is not None else ""
        drafts: list[dict[str, Any]] = []
        for i, join in enumerate(select.args.get("joins") or []):
            alias = _alias(join.this)
            slot = name_key(alias) if alias else f"#{i + 1}"
            conds: list[Any] = []
            if join.args.get("on") is not None:
                conds = [self.canonical(c, sources) for c in _conjuncts(join.args["on"])]
            elif join.args.get("using") and first_alias and alias:
                # USING (id)：按「FROM 的那张表.id = 这张表.id」写，和 ON 的写法放在一起比
                conds = [self.canonical(exp.EQ(this=exp.column(ident.name, table=first_alias),
                                               expression=exp.column(ident.name, table=alias)), sources)
                         for ident in join.args["using"]]
            before = [s for key, s in list(sources.items())[:i + 1] if key != slot]
            drafts.append({"kind": _join_kind(join), "slot": slot, "source": sources.get(slot), "conds": conds,
                           "before": [s.name for s in before], "before_keys": [s.key for s in before]})
        return drafts

    def _joins_on_where(self, cond: Any, sources: dict[str, _Source], drafts: list[dict[str, Any]]) -> bool:
        """WHERE 里两张表的列等式（逗号连接、或者 INNER JOIN 之外又写的等值关联）并进那次连接的条件，不算筛选。
        LEFT JOIN 这类外连接不并：写在 WHERE 里会把外连接变成内连接，和写在 ON 里不是一回事。"""
        from sqlglot import exp

        n = _unparen(cond)
        if not isinstance(n, exp.EQ):
            return False
        left, right = _unparen(n.this), _unparen(n.expression)
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return False
        a, b = self.locate(left, sources)[0], self.locate(right, sources)[0]
        if a is None or b is None or a == b:
            return False
        for draft in reversed(drafts):
            if draft["slot"] in (a, b) and draft["kind"] in ("inner", "cross"):
                draft["conds"].append(cond)
                draft["kind"] = "inner"
                return True
        return False

    def _join(self, draft: dict[str, Any], sources: dict[str, _Source]) -> JoinFacet:
        source = draft["source"]
        table = Part.plain("?", "?") if source is None else \
            Part(key=source.key, text=source.name, loose=source.key, full=source.name)
        forms = sorted((self.render(c, sources, "key"), self.render(c, sources, "loose"),
                        self.render(c, sources, "full")) for c in draft["conds"])
        on = Part(key=" AND ".join(f[0] for f in forms), text=" AND ".join(f[2] for f in forms),
                  loose=" AND ".join(sorted(f[1] for f in forms)), full=" AND ".join(f[2] for f in forms)) \
            if forms else None
        mentioned = {table for c in draft["conds"] for table, _ in self.part(c, sources).columns}
        other = [name for name in dict.fromkeys(t for t in mentioned if t) if name != table.text] if draft["conds"] \
            else draft["before"]
        # 涉及的来源按条件里直接引用的算（slot 不为 None 的是这一段的来源）：透过 CTE 找到的实表两边可能一样，
        # 关联的却是两个不同的 CTE
        sides = {source.key} if source is not None else set()
        if draft["conds"]:
            sides |= {found.key for c in draft["conds"] for col in _own_columns(c)
                      for slot, found in [self.locate(col, sources)] if slot is not None and found is not None}
        else:
            sides |= set(draft["before_keys"])
        return JoinFacet(kind=draft["kind"], table=table, on=on, other=tuple(sorted(other)),
                         sides=tuple(sorted(sides)))

    def _time(self, cond: Any, sources: dict[str, _Source]) -> TimePredicate | None:
        """时间条件：一边恰好一列（可以套着日期函数、CAST），另一边没有列；列名像日期，或者用了日期函数、像日期的字符串。"""
        from sqlglot import exp

        n = _unparen(cond)
        if n.args.get("negate"):
            return None
        if isinstance(n, exp.Between):
            subject, values, op = n.this, [n.args.get("low"), n.args.get("high")], "between"
        elif isinstance(n, exp.In) and n.expressions and not n.args.get("query"):
            subject, values, op = n.this, list(n.expressions), "in"
        elif type(n).__name__ in _COMPARE and isinstance(n, exp.Binary):
            subject, values, op = n.this, [n.expression], _COMPARE[type(n).__name__][0]
        else:
            return None
        if subject is None or any(v is None or _own_columns(v) for v in values):
            return None
        column = _bare_column(subject)
        cols = [column] if column is not None else _own_columns(subject)
        if len(cols) != 1 or any(isinstance(x, (exp.AggFunc, exp.Select)) for x in subject.walk()):
            return None
        if not (_temporal_name(cols[0].name) or _datey(subject) or any(_datey(v) for v in values)):
            return None
        shown = [self.render(v, sources) for v in values]
        value = f"{shown[0]} AND {shown[1]}" if op == "between" else f"({', '.join(shown)})" if op == "in" \
            else shown[0]
        return TimePredicate(column=self.part(cols[0], sources), op=op, value=value,
                             condition=self.part(cond, sources, excludes=True))

    @staticmethod
    def _aliases(query: Any) -> dict[str, Any]:
        """SELECT 里的别名 {name_key: 别名指的表达式}。"""
        from sqlglot import exp

        return {name_key(s.alias): s.this for s in query.selects if isinstance(s, exp.Alias) and s.alias}

    @staticmethod
    def _target(item: Any, selects: list[Any], aliases: dict[str, Any]) -> Any:
        """GROUP BY、ORDER BY 里的一项：写序号（1）的换成 SELECT 里对应的表达式，写别名的换成别名指的表达式
        （MySQL、SQLite 的读法），其余原样。"""
        from sqlglot import exp

        target = _unparen(item)
        if isinstance(target, exp.Literal) and not target.is_string and str(target.this).isdigit():
            index = int(target.this) - 1
            if 0 <= index < len(selects):
                return selects[index].this if isinstance(selects[index], exp.Alias) else selects[index]
        elif isinstance(target, exp.Column) and not target.table and name_key(target.name) in aliases:
            return aliases[name_key(target.name)]
        return target

    def _group(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """分组键。GROUP BY 1 换成 SELECT 里对应的表达式，GROUP BY 别名换成别名指的表达式（MySQL、SQLite 的读法）。"""
        group = select.args.get("group")
        if group is None:
            return []
        selects, aliases = select.selects, self._aliases(select)
        return [self.part(self.canonical(self._target(item, selects, aliases), sources), sources)
                for item in group.expressions]

    def _having(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """HAVING 的各个合取式。引用 SELECT 别名的列（HAVING s > 100，MySQL、SQLite 的写法）换成别名指的表达式，
        和 GROUP BY 别名一个读法：不换的话，写别名和写表达式的同一个条件会被报成两个不同的条件。"""
        from sqlglot import exp

        clause = select.args.get("having")
        if clause is None:
            return []
        aliases = self._aliases(select)
        out = []
        for cond in _conjuncts(clause.this):
            copy = cond.copy()
            for col in _own_columns(copy):
                target = aliases.get(name_key(col.name)) if not col.table else None
                # 聚合函数里的名字指的是表里的列，不是别名：SUM(amount) AS amount 之后的 SUM(amount) 不能再换
                if target is None or any(isinstance(p, exp.AggFunc) for p in _parents(col, stop=None)):
                    continue
                if col is copy:
                    copy = target.copy()
                    break
                col.replace(target.copy())
            out.append(self.part(self.canonical(copy, sources), sources))
        return out

    def _aggregates(self, select: Any, sources: dict[str, _Source]) -> list[Aggregate]:
        from sqlglot import exp

        out = []
        for node in _own_nodes(select):
            if not isinstance(node, exp.AggFunc) or isinstance(node.parent, exp.Window):
                continue
            try:
                func = str(node.sql_name()).lower()
            except Exception:  # noqa: BLE001 - 没有固定函数名的聚合：按类型名
                func = node.key
            arg = node.this
            distinct = isinstance(arg, exp.Distinct)
            if distinct:
                items = list(arg.expressions)
                arg = items[0] if len(items) == 1 else exp.Tuple(expressions=[i.copy() for i in items])
            if arg is None or isinstance(arg, exp.Star) or \
                    (func == "count" and not distinct and isinstance(_unparen(arg), exp.Literal)):
                part = Part.plain("*", "*")         # COUNT(*)、COUNT(1)：数的是行数
            else:
                part = self.part(self.canonical(arg, sources), sources)
            out.append(Aggregate(func=func, arg=part, distinct=distinct, text=self.render(node, sources)))
        return out

    def _limit(self) -> str | None:
        from sqlglot import exp

        node = self.root.args.get("limit")
        if node is None:
            return None
        value = node.expression if isinstance(node, exp.Limit) else node.args.get("count")
        return value.sql(dialect=self.read, comments=False) if value is not None else None

    def _limits(self) -> list[LimitFacet]:
        """各段取哪几行：最外层在前，其余按出现的先后。条件里套着的子查询（WHERE、HAVING、ON 里的）不单算：
        它整个写在那个条件的 key 里，单算的话同一处不同会报两遍。"""
        from sqlglot import exp

        out = []
        for query in self.root.find_all(exp.Select, exp.SetOperation, bfs=False):
            if query is not self.root and self._in_condition(query):
                continue
            if (found := self._limit_of(query)) is not None:
                out.append(found)
        return out

    def _in_condition(self, node: Any) -> bool:
        from sqlglot import exp

        while node is not None and node is not self.root:
            if isinstance(node, (exp.Where, exp.Having)) or \
                    (isinstance(node.parent, exp.Join) and node.arg_key == "on"):
                return True
            node = node.parent
        return False

    def _limit_of(self, query: Any) -> LimitFacet | None:
        """一段的 LIMIT / OFFSET，连同这一段的排序；两样都没写返回 None。"""
        from sqlglot import exp

        def written(node: Any) -> str | None:
            return node.sql(dialect=self.read, comments=False) if node is not None else None

        limit, offset = query.args.get("limit"), query.args.get("offset")
        count = written(limit.expression if isinstance(limit, exp.Limit) else limit.args.get("count")) \
            if limit is not None else None
        if offset is None and isinstance(limit, exp.Limit):
            offset = limit.args.get("offset")        # 有的方言把 OFFSET 挂在 LIMIT 底下
        skip = written(offset.expression if isinstance(offset, exp.Offset) else offset)
        if skip is not None and skip.strip() == "0":
            skip = None                              # OFFSET 0 等于没写
        if limit is None and skip is None:
            return None
        return LimitFacet(count=count, offset=skip, order=self._order(query), scope=self._scope_name(query),
                          scope_key=self._scope_key(query))

    @staticmethod
    def _scope_key(query: Any) -> str | None:
        """query 整段是哪个派生来源（CTE 的定义、FROM 里的子查询、LATERAL 子查询）的编号；最外层、UNION 的某一支、
        SELECT 列表里的子查询这类不是派生来源整段的返回 None。编号记在 CTE 定义、FROM / JOIN 项上，往外找一两层。"""
        from sqlglot import exp

        node = query
        while isinstance(node.parent, (exp.Subquery, exp.Lateral, exp.CTE)):
            node = node.parent
            if found := node.meta.get(_DERIVED):
                return found
        return None

    def _order(self, query: Any) -> tuple[tuple[Part, bool], ...]:
        """这一段的排序键 (Part, 是否降序)。ORDER BY 1、ORDER BY 别名换成 SELECT 里的表达式（UNION 按第一支的）。"""
        from sqlglot import exp

        order = query.args.get("order")
        if order is None:
            return ()
        select = query if isinstance(query, exp.Select) else None
        sources = self.sources(select) if select is not None else _Sources()
        selects = list(query.selects)
        aliases = self._aliases(query)
        out = []
        for item in order.expressions:
            target = self._target(item.this if isinstance(item, exp.Ordered) else item, selects, aliases)
            desc = bool(item.args.get("desc")) if isinstance(item, exp.Ordered) else False
            out.append((self.part(self.canonical(target, sources), sources), desc))
        return tuple(out)

    def _scope_name(self, query: Any) -> str | None:
        from sqlglot import exp

        if query is self.root:
            return None
        parent = query.parent
        if isinstance(parent, exp.Subquery) and not parent.alias and isinstance(parent.parent, exp.Lateral):
            parent = parent.parent          # LATERAL 的别名挂在 LATERAL 上，不在里面的子查询上
        if isinstance(parent, (exp.CTE, exp.Subquery, exp.Lateral)) and parent.alias:
            return parent.alias
        return "子查询"


# ==========================================================================
# 比较
# ==========================================================================


@dataclass(frozen=True)
class FacetDiff:
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同）/ filter / aggregate / join / tables /
    group_by / having / limit（取哪几行：行数、跳过的行数、取前几行时的排序）/ parse。a、b 是两边各自的写法（给人看，
    没有就是 None），text 是一句中文说明。
    detail 只在 time 上有：两边的时间列 [{table, column}]，调用方据此对照数据目录（业务日期）。"""

    aspect: str
    a: Any
    b: Any
    text: str
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"aspect": self.aspect, "a": self.a, "b": self.b, "text": self.text}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


_CJK_PUNCT = frozenset("，。；：、！？「」『』（）《》【】…—")
#: 聚合函数的说法
_VERBS = {"sum": "求和", "count": "计数", "avg": "求平均", "min": "取最小值", "max": "取最大值"}


def _wide(ch: str) -> bool:
    return "⺀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


def _say(*parts: str) -> str:
    """拼一句话：汉字和英文、数字、SQL 之间加一个空格，挨着全角标点的不加，汉字和汉字之间不加。

    两边的叫法由调用方定（A / B，或者「原流程」「盲算」），所以空格只能拼的时候按字判断。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if out and not out[-1].isspace() and not part[0].isspace():
            a, b = out[-1], part[0]
            if not (a in _CJK_PUNCT or b in _CJK_PUNCT or (_wide(a) and _wide(b))):
                out += " "
        out += part
    return out


def catalog_labels(notes: Mapping[str, Mapping[str, Any]] | None) -> dict[str, str]:
    """数据目录（{表名: notes}）里列的中文名 → diff_facets 的 names：{「表.列」: 中文名}；只在一张表里有、或者各表
    叫法一致的列另记 {列: 中文名}。被驳回的项不算。"""
    out: dict[str, str] = {}
    bare: dict[str, tuple[str, set[str]]] = {}
    for table, raw in (notes or {}).items():
        visible = catalog.visible_notes(raw) if isinstance(raw, Mapping) else {}
        columns = visible.get("columns") if isinstance(visible.get("columns"), Mapping) else {}
        for column, items in columns.items():
            item = items.get("label") if isinstance(items, Mapping) else None
            label = item.get("value") if isinstance(item, Mapping) else None
            if isinstance(label, str) and label.strip():
                out[f"{table}.{column}"] = label.strip()
                bare.setdefault(name_key(str(column)), (str(column), set()))[1].add(label.strip())
    for column, labels in bare.values():
        if len(labels) == 1:
            out.setdefault(column, next(iter(labels)))
    return out


def _labeler(names: Mapping[str, str] | None) -> Any:
    index = {name_key(str(k)): str(v) for k, v in (names or {}).items() if isinstance(v, str) and v.strip()}

    def label(ref: ColumnRef) -> str | None:
        table, column = ref
        return (index.get(name_key(f"{table}.{column}")) if table else None) or index.get(name_key(column))

    return label


def _single_column(part: Part, *, loose: bool) -> str | None:
    """只涉及一列的条件：那一列（Part.column_keys：实表.列，CTE、子查询的列写「编号.列」），用来把两边对同一列的
    不同写法配成一对。loose 时实表的列只看列名（表换了一张），派生来源的列照样带编号：两个 CTE 的同名列不是一列。"""
    if len(part.columns) != 1 or len(part.column_keys) != 1:
        return None
    key = part.column_keys[0]
    return name_key(part.columns[0][1]) if loose and not key.startswith("@") else key


def _verb(g: Aggregate) -> str:
    return ("去重" if g.distinct else "") + _VERBS.get(g.func, f"用 {g.func.upper()} 汇总")


def _rows(limit: str | None) -> str:
    if limit is None:
        return "不限行数"
    return f"只取前 {limit} 行" if limit.isdigit() else f"按 LIMIT {limit} 取行"


def _skip(offset: str | None) -> str:
    if offset is None:
        return "不跳过行"
    return f"跳过前 {offset} 行" if offset.isdigit() else f"按 OFFSET {offset} 跳过行"


class _Compare:
    """一次比较：两边的叫法、列的中文名，以及按哪种写法比。

    两边各有一张只在自己这边出现、又不是关联进来的表（orders 换成了 orders_archive）时，其余各方面按不写表名的
    loose 比：表换了这件事由一条「tables」说清，同一个条件、同一个聚合不再因为表名不同被重复报一遍。

    派生来源的编号由 diff_facets 先按结构配好（两边配成一对的写同一个编号，见模块说明），这里各方面都只按 k 比：
    编号不同就是不同的来源，不再拿「不写编号」的写法补配（那样会把 p.total 和 r.total 当成同一列）。
    """

    def __init__(self, a: Facets, b: Facets, labels: tuple[str, str], names: Mapping[str, str] | None) -> None:
        self.a, self.b = a, b
        self.la, self.lb = labels
        self.label = _labeler(names)
        joined = {j.table.key for j in (*a.joins, *b.joins)}
        self.ta = [t for t in a.tables if t.key not in {u.key for u in b.tables} and t.key not in joined]
        self.tb = [t for t in b.tables if t.key not in {u.key for u in a.tables} and t.key not in joined]
        self.swapped = len(self.ta) == 1 and len(self.tb) == 1

    def k(self, part: Part | None) -> str | None:
        if part is None:
            return None
        return part.loose if self.swapped else part.key

    def side(self, key: str) -> str:
        """比较用的来源 key；换了一张表时，换的那两张记成同一个。"""
        return "?" if self.swapped and key in (self.ta[0].key, self.tb[0].key) else key

    def sides(self, j: JoinFacet) -> tuple[str, ...]:
        """连接涉及的来源（见 JoinFacet.sides）。"""
        return tuple(sorted(self.side(s) for s in j.sides))

    def added(self, j: JoinFacet, mine: Facets, theirs: Facets) -> tuple[str, set[str]]:
        """一边独有的一次连接，多出来的是哪个来源：(给人看的名字, 比较用的 key)。

        一般是关联进来的那一侧；可内连接、CROSS JOIN 关联进来的那一侧另一边也有时，多出来的其实是这次连接接上的
        另一头（FROM stores JOIN orders，另一边只查 orders：多出来的是 stores，不是 orders），说成「关联了 orders
        表」就错了。这时报涉及的来源里另一边没有的那几个。"""
        if j.kind in ("inner", "cross"):
            have = {self.side(s.key) for s in theirs.sources}
            new = [k for k in j.sides if self.side(k) not in have]
            if new and self.side(j.table.key) in have:
                names = {s.key: s.text for s in mine.sources}
                return "、".join(names.get(k, k) for k in new), set(new)
        return j.table.text, {j.table.key}

    @staticmethod
    def unmatched(xs: Iterable[Any], ys: Iterable[Any], key: Any) -> tuple[list[Any], list[Any]]:
        """两边各自独有的项（按 key 比），各按原来的先后。派生来源在 key 里已经是两边按结构配好的共同编号（见
        diff_facets），这里不再拿不写编号的写法补配一遍：那样两个 CTE 的同名列（p.total、r.total）会被当成一列，
        换了用哪一个的列也报不出来。"""
        xs, ys = list(xs), list(ys)
        kx, ky = {key(x) for x in xs}, {key(y) for y in ys}
        return [x for x in xs if key(x) not in ky], [y for y in ys if key(y) not in kx]

    @staticmethod
    def lookalikes(only_a: list[Part], only_b: list[Part]) -> list[tuple[Part, Part]]:
        """两边各自独有、给人看的写法却一样的条件配成一对，就地从两边拿掉：同一个条件挂在不同的来源上（p.total > 100
        和 r.total > 100，各自只读得出 total > 100），分开各说一句「只保留 total > 100 的行」读起来自相矛盾。"""
        pairs = []
        for p in list(only_a):
            q = next((q for q in only_b if q.text == p.text), None)
            if q is not None:
                pairs.append((p, q))
                only_a.remove(p)
                only_b.remove(q)
        return pairs

    @staticmethod
    def shown(xs: list[str], ys: list[str], full_xs: list[str], full_ys: list[str]) -> tuple[str, str]:
        """两边给人看的写法；读起来一样（各自只写列名）却不是同一个东西时，改成一律写表名的写法。"""
        wa, wb = "、".join(xs), "、".join(ys)
        return ("、".join(full_xs), "、".join(full_ys)) if wa == wb else (wa, wb)

    def named(self, parts: list[Part], *, full: bool = False) -> str:
        """时间列：有中文名的写「支付时间 paid_at」，几列用顿号隔开。"""
        out = []
        for p in parts:
            text = p.full if full else p.text
            name = self.label(p.columns[0]) if p.columns else None
            out.append(_say(name, text) if name else text)
        return "、".join(out)

    def one_sided(self, side: str, other: str, part: Part, *, group: bool = False) -> str:
        if group:
            return _say(side, "分组后只保留", part.text, "的组，", other, "没有这个条件")
        if part.excludes:
            return _say(side, "排除了", part.excludes, "的行，", other, "没有")
        return _say(side, "只保留", part.text, "的行，", other, "没有这个条件")

    # ---- 各个方面

    def time(self) -> list[FacetDiff]:
        a, b, la, lb = self.a, self.b, self.la, self.lb
        ca = list(_dedupe((t.column for t in a.time), self.k))
        cb = list(_dedupe((t.column for t in b.time), self.k))
        if any(self.unmatched(ca, cb, self.k)):
            wa, wb = self.named(ca), self.named(cb)
            if wa == wb:
                wa, wb = self.named(ca, full=True), self.named(cb, full=True)
            if ca and cb:
                text = _say(la, "按", wa, "筛时间，", lb, "按", wb)
            elif ca:
                text = _say(la, "按", wa, "筛时间，", lb, "没有按时间筛选")
            else:
                text = _say(la, "没有按时间筛选，", lb, "按", wb, "筛时间")
            detail = {side: [{"table": p.columns[0][0], "column": p.columns[0][1]} for p in parts if p.columns]
                      for side, parts in (("a", ca), ("b", cb))}
            return [FacetDiff("time", "、".join(p.text for p in ca) or None, "、".join(p.text for p in cb) or None,
                              text, detail)]
        if any(self.unmatched(a.time, b.time, lambda t: self.k(t.condition))):
            ra = " AND ".join(t.condition.text for t in a.time)
            rb = " AND ".join(t.condition.text for t in b.time)
            return [FacetDiff("time_range", ra, rb, _say(la, "的时间范围是", ra, "，", lb, "是", rb))]
        return []

    def filters(self) -> list[FacetDiff]:
        timed = {self.k(t.condition) for t in (*self.a.time, *self.b.time)}
        fa = [p for p in self.a.where if self.k(p) not in timed]
        fb = [p for p in self.b.where if self.k(p) not in timed]
        only_a, only_b = self.unmatched(fa, fb, self.k)
        out: list[FacetDiff] = []
        # 先配对同一列的不同写法（status = 'paid' 和 status IN (…)），再配读起来一样、挂在不同来源上的同一个条件
        for p in list(only_a):
            column = _single_column(p, loose=self.swapped)
            mine = [q for q in only_a if column and _single_column(q, loose=self.swapped) == column]
            theirs = [q for q in only_b if column and _single_column(q, loose=self.swapped) == column]
            if len(mine) == 1 and len(theirs) == 1:
                q = theirs[0]
                wa, wb = self.shown([p.text], [q.text], [p.full], [q.full])
                out.append(FacetDiff("filter", p.text, q.text, _say(self.la, "按", wa, "筛选，", self.lb, "按", wb, "筛选")))
                only_a.remove(p)
                only_b.remove(q)
        for p, q in self.lookalikes(only_a, only_b):
            wa, wb = self.shown([p.text], [q.text], [p.full], [q.full])
            out.append(FacetDiff("filter", wa, wb, _say(self.la, "按", wa, "筛选，", self.lb, "按", wb, "筛选")))
        out += [FacetDiff("filter", p.text, None, self.one_sided(self.la, self.lb, p)) for p in only_a]
        out += [FacetDiff("filter", None, p.text, self.one_sided(self.lb, self.la, p)) for p in only_b]
        return out

    def aggregates(self) -> list[FacetDiff]:
        la, lb = self.la, self.lb

        def key(g: Aggregate) -> tuple[str, str | None, bool]:
            return g.func, self.k(g.arg), g.distinct

        def pair(xs: list[Aggregate], ys: list[Aggregate], same: Any) -> list[tuple[Aggregate, Aggregate]]:
            pairs = []
            for x in list(xs):
                y = next((y for y in ys if same(x, y)), None)
                if y is not None:
                    pairs.append((x, y))
                    xs.remove(x)
                    ys.remove(y)
            return pairs

        ga, gb = self.unmatched(self.a.aggregates, self.b.aggregates, key)
        # 先配参数相同的（同一列：函数或去重不同），再配函数相同的（同一种算法：列不同）；各剩一个的也配成一对
        pairs = pair(ga, gb, lambda x, y: self.k(x.arg) == self.k(y.arg))
        pairs += pair(ga, gb, lambda x, y: x.func == y.func)
        if len(ga) == 1 and len(gb) == 1:
            pairs.append((ga.pop(), gb.pop()))
        out = []
        for x, y in pairs:
            same_arg = self.k(x.arg) == self.k(y.arg)
            wa, wb = self.shown([x.arg.text], [y.arg.text], [x.arg.full], [y.arg.full]) if not same_arg \
                else (x.arg.text, y.arg.text)
            if same_arg and x.func == y.func:
                # 只差去重：COUNT(DISTINCT order_id) 和 COUNT(order_id)
                text = _say(la, "对", wa, _verb(x) + "，", lb, "没去重") if x.distinct \
                    else _say(la, "对", wa, _verb(x) + "时没去重，", lb, _verb(y))
            elif same_arg and x.arg.key != "*":
                # 同一列、算法不同：后半句不再重复列名
                text = _say(la, "对", wa, _verb(x) + "，", lb, _verb(y))
            else:
                what_a = ["数的是行数"] if x.func == "count" and x.arg.key == "*" else ["对", wa, _verb(x)]
                what_b = ["数的是行数"] if y.func == "count" and y.arg.key == "*" else ["对", wb, _verb(y)]
                text = _say(la, *what_a, "，", lb, *what_b)
            out.append(FacetDiff("aggregate", x.text, y.text, text))
        out += [FacetDiff("aggregate", g.text, None, _say(la, "多算了", g.text, "，", lb, "没有")) for g in ga]
        out += [FacetDiff("aggregate", None, g.text, _say(lb, "多算了", g.text, "，", la, "没有")) for g in gb]
        return out

    def joins(self) -> list[FacetDiff]:
        """连接：先把完全一样的两边配掉，剩下的按关联的是哪张表（派生来源按配好的编号）、再按涉及的来源配成一对，
        说类型或条件哪里不同，再剩下的是只有一边关联了（多出来的是哪个来源见 added）。完全一样：内连接、CROSS JOIN
        比类型、涉及的来源和条件（FROM paid JOIN refunded 和 FROM refunded JOIN paid 是同一次连接），别的连接比类型、
        关联进来的表和条件。先配完全一样的，同一张表关联了两次、先后不同也不算不同。配成一对的两边名字不同（两边给
        CTE 起的名字不同、内连接换了先后）时两个名字都说。派生来源已经按结构配好了编号：一边多出来的 CTE 编号和
        另一边的都不一样，不会被配给另一边不相干的 CTE、说成「关联条件不同」。

        做不到的：LEFT JOIN 和两边对调的 RIGHT JOIN 意思一样，仍报成类型不同；内连接里只涉及一张表的条件写在 ON 里
        还是 WHERE 里意思一样，仍算两处不同；三张以上不带关联条件的表（逗号连接、CROSS JOIN）换了先后，涉及的来源
        按「它之前的各个来源」算，仍可能算不同。"""
        la, lb = self.la, self.lb

        def same(j: JoinFacet) -> tuple[Any, ...]:
            if j.kind in ("inner", "cross"):
                return j.kind, self.sides(j), self.k(j.on)
            return j.kind, j.table.key, self.k(j.on)

        rest_a, rest_b = self.unmatched(self.a.joins, self.b.joins, same)
        pairs: list[tuple[JoinFacet, JoinFacet]] = []
        for table in (lambda j: j.table.key, self.sides):
            for x in list(rest_a):
                y = next((y for y in rest_b if table(y) == table(x)), None)
                if y is not None:
                    pairs.append((x, y))
                    rest_a.remove(x)
                    rest_b.remove(y)
        out: list[FacetDiff] = []
        for x, y in pairs:
            tx, ty = x.table.text, y.table.text
            if x.kind != y.kind:
                kx, ky = _JOIN_WORDS.get(x.kind, x.kind), _JOIN_WORDS.get(y.kind, y.kind)
                tail = () if ty == tx else ("关联", ty, "表")
                out.append(FacetDiff("join", kx, ky, _say(la, "用", kx, "关联", tx, "表，", lb, "用", ky, *tail)))
            if self.k(x.on) != self.k(y.on):
                xa, ya = (x.on.text if x.on else "（无条件）"), (y.on.text if y.on else "（无条件）")
                tail = ("是", ya) if ty == tx else ("关联", ty, "表的条件是", ya)
                out.append(FacetDiff("join", xa, ya, _say(la, "关联", tx, "表的条件是", xa, "，", lb, *tail)))
        said: dict[str, set[str]] = {"a": set(), "b": set()}
        for side, rest, mine, theirs in (("a", rest_a, self.a, self.b), ("b", rest_b, self.b, self.a)):
            me, other = (la, lb) if side == "a" else (lb, la)
            for j in rest:
                name, keys = self.added(j, mine, theirs)
                said[side] |= keys
                out.append(FacetDiff("join", name if side == "a" else None, name if side == "b" else None,
                                     _say(me, "关联了", name, "表，", other, "没有")))
        # 关联之外的表（FROM 的表、子查询里的表）只在一边出现的；上面已经说过「关联了它」的不再说一遍
        if self.swapped:
            ta, tb = self.ta[0], self.tb[0]
            return out + [FacetDiff("tables", ta.text, tb.text, _say(la, "查的是", ta.text, "表，", lb, "查的是", tb.text,
                                                                     "表"))]
        out += [FacetDiff("tables", t.text, None, _say(la, "用到了", t.text, "表，", lb, "没有"))
                for t in self.ta if t.key not in said["a"]]
        out += [FacetDiff("tables", None, t.text, _say(lb, "用到了", t.text, "表，", la, "没有"))
                for t in self.tb if t.key not in said["b"]]
        return out

    def group(self) -> list[FacetDiff]:
        a, b = self.a.group_by, self.b.group_by
        if not any(self.unmatched(a, b, self.k)):
            return []
        wa, wb = self.shown([p.text for p in a], [p.text for p in b], [p.full for p in a], [p.full for p in b])
        if wa and wb:
            text = _say(self.la, "按", wa, "分组，", self.lb, "按", wb, "分组")
        elif wa:
            text = _say(self.la, "按", wa, "分组，", self.lb, "没有分组")
        else:
            text = _say(self.la, "没有分组，", self.lb, "按", wb, "分组")
        return [FacetDiff("group_by", wa or None, wb or None, text)]

    def having(self) -> list[FacetDiff]:
        only_a, only_b = self.unmatched(self.a.having, self.b.having, self.k)
        out = []
        for p, q in self.lookalikes(only_a, only_b):
            wa, wb = self.shown([p.text], [q.text], [p.full], [q.full])
            out.append(FacetDiff("having", wa, wb, _say(self.la, "分组后只保留", wa, "的组，", self.lb, "只保留", wb,
                                                        "的组")))
        return out + \
            [FacetDiff("having", p.text, None, self.one_sided(self.la, self.lb, p, group=True)) for p in only_a] + \
            [FacetDiff("having", None, p.text, self.one_sided(self.lb, self.la, p, group=True)) for p in only_b]

    def limit(self) -> list[FacetDiff]:
        """取哪几行：最外层两边比；CTE、子查询里的按所属派生来源配好的编号（LimitFacet.scope_key）配成一对，两边给
        它起的名字可以不同。配不上的派生来源各说各的，不按先后凑对：一边多一个限了行数的 CTE，不能拿它和另一边的
        CTE 比。不属于哪个派生来源整段的（UNION 的某一支这类）按出现的先后配。"""
        def outer(f: Facets) -> LimitFacet | None:
            return next((x for x in f.limits if x.scope is None), None)

        out = self.rows(outer(self.a), outer(self.b))
        ia = [x for x in self.a.limits if x.scope is not None]
        ib = [x for x in self.b.limits if x.scope is not None]
        pairs: list[tuple[LimitFacet | None, LimitFacet | None]] = []
        for x in list(ia):
            y = next((y for y in ib if x.scope_key is not None and y.scope_key == x.scope_key), None)
            if y is not None:
                pairs.append((x, y))
                ia.remove(x)
                ib.remove(y)
        pairs += zip_longest([x for x in ia if x.scope_key is None], [y for y in ib if y.scope_key is None])
        pairs += [(x, None) for x in ia if x.scope_key is not None]
        pairs += [(None, y) for y in ib if y.scope_key is not None]
        for x, y in pairs:
            out += self.rows(x, y)
        return out

    def lk(self, x: LimitFacet | None) -> tuple[Any, ...] | None:
        """一段取哪几行的比较用写法：行数、跳过的行数、排序键。"""
        if x is None:
            return None
        return x.count, x.offset, tuple((self.k(p), desc) for p, desc in x.order)

    def sorting(self, x: LimitFacet, *, full: bool = False) -> str:
        if not x.order:
            return "没有排序"
        keys = "、".join(_say(p.full if full else p.text, "降序" if desc else "升序") for p, desc in x.order)
        return _say("按", keys, "排")

    def rows(self, x: LimitFacet | None, y: LimitFacet | None) -> list[FacetDiff]:
        """一对段（一边可以没有）取的行有什么不同：行数、跳过的行数，两边都限了行时再比排序。"""
        if self.lk(x) == self.lk(y):
            return []
        sa, sb = (x.scope if x else None), (y.scope if y else None)
        la = _say(self.la, "在", sa, "里") if sa else self.la
        lb = _say(self.lb, "在", sb, "里") if sb and sb != sa else self.lb
        out = []
        ca, cb = (x.count if x else None), (y.count if y else None)
        if ca != cb:
            out.append(FacetDiff("limit", ca, cb, _say(la, _rows(ca), "，", lb, _rows(cb))))
        oa, ob = (x.offset if x else None), (y.offset if y else None)
        if oa != ob:
            out.append(FacetDiff("limit", oa, ob, _say(la, _skip(oa), "，", lb, _skip(ob))))
        if x is not None and y is not None and self.lk(x)[2] != self.lk(y)[2]:
            wa, wb = self.sorting(x), self.sorting(y)
            if wa == wb:
                wa, wb = self.sorting(x, full=True), self.sorting(y, full=True)
            out.append(FacetDiff("limit", wa, wb, _say(la, "取行前", wa, "，", lb, wb)))
        return out


def diff_facets(a: Facets, b: Facets, labels: tuple[str, str] = ("A", "B"), *,
                names: Mapping[str, str] | None = None) -> list[FacetDiff]:
    """逐个方面比两份 facets，每处不同一条，按对结果影响大致从大到小排：时间、筛选、聚合、连接和表、分组、HAVING、
    取哪几行。完全一致返回空列表。

    labels 是两边的叫法（缺省 A / B，双盲复算可以传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」:
    中文名}，可以用 catalog_labels 从数据目录取），给了的话时间列写成「支付时间 paid_at」。任一边解析不了就只返回
    一条 parse，不逐项比较：拿半份结构去比，只会报出一堆不存在的不同。

    比之前先把两边的派生来源按结构配好对，按共同的编号重新分析 b（见模块说明、_aligned）；给人看的写法不受影响。
    """
    la, lb = labels
    if a.parse_error or b.parse_error:
        if a.parse_error and b.parse_error:
            text = _say(la, "：", a.parse_error, "；", lb, "：", b.parse_error, "；没有逐项比较")
        else:
            side, error = (la, a.parse_error) if a.parse_error else (lb, b.parse_error)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return [FacetDiff("parse", a.parse_error, b.parse_error, text)]
    cmp = _Compare(a, _aligned(a, b), labels, names)
    return cmp.time() + cmp.filters() + cmp.aggregates() + cmp.joins() + cmp.group() + cmp.having() + cmp.limit()


def _aligned(a: Facets, b: Facets) -> Facets:
    """b 的派生来源按结构配给 a 的（见 _align），配上的改写成 a 里对应的编号、配不上的另起 a 里没有的编号，按这套
    编号重新分析 b 的原文。a 的编号就是它的位置编号，不用重算。

    任一边不是 facets 算出来的（没记原文）、重新解析不了，或者分析时出了意外，照原样返回 b：两边仍按各自的位置
    编号比，一边多出排在前面的 CTE 时会多报。
    """
    try:
        sa, sb = _Statement.reparse(a), _Statement.reparse(b)
        if sa is None or sb is None:
            return b
        da, db = sa.derived(), sb.derived()
        pairs = _align(da, db)
        fresh = iter(range(max((d.number for d in da), default=0) + 1, 1 << 30))
        sb.relabel({d.number: f"@{pairs[d.number]}" if d.number in pairs else f"@{next(fresh)}" for d in db})
        return replace(sb.facets(), _origin=b._origin)
    except Exception:  # noqa: BLE001 - 配对本身的缺陷不能连累比较：退回按各自的编号比，记下来修
        logger.warning("SQL 派生来源配对失败，按各自的位置编号比较", exc_info=True)
        return b


__all__ = ["MAX_SQL_CHARS", "PLACEHOLDER", "Aggregate", "ColumnRef", "FacetDiff", "Facets", "JoinFacet", "LimitFacet",
           "NormalizedSql", "Part", "TimePredicate", "catalog_labels", "diff_facets", "facets", "normalize_sql"]
