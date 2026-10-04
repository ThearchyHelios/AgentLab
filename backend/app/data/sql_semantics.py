"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），运行
对比要回答「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了），反方审稿要看自己
的查询和原查询的口径是否一致。三处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、注释、不加引号的名字的大小写都不算不同。
- facets：把一条查询拆成几个方面——用到的表、连接（类型、关联进来的表、ON 条件）、WHERE 的各个合取式、其中的
  时间条件（列、运算符、取值）、分组、聚合（函数、参数、是否去重）、HAVING、LIMIT。别名一律换成真实表名，
  等式、比较的两边排好顺序：o.paid_at >= '2026-09-01' 和 '2026-09-01' <= orders.paid_at 是同一个条件。
- diff_facets：逐个方面比两份 facets，每处不同给一句中文说明。

**逐段收集。** CTE、子查询里的条件和聚合也算进来：盲算可能把有效记录条件写在 CTE 里，原流程写在外层，按
「整条语句里有哪些条件」比才不会把同一个口径报成不同。逗号连接（以及 INNER JOIN 之外又写在 WHERE 里的等值关联）
写在 WHERE 里的列等式算作关联条件，不算筛选。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就带 parse_error 返回，从不抛
异常。没加表前缀、又有好几张表可能有这一列的，照写列名（给了表结构才去对）。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from app.data import catalog
from app.data.names import name_key
from app.data.sqlcheck import _bare_column, _conjuncts, _parents, _unparen, dialect_of

logger = logging.getLogger(__name__)

#: 规范文本里字面量的占位符
PLACEHOLDER = "?"
#: 太长的 SQL 不做语法分析（normalize_sql 退回逐词规范化，facets 带 parse_error）：解析耗时随长度增长
MAX_SQL_CHARS = 100_000


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

    key 用来比较：别名换成真实表名，表名、列名按 name_key，等式和比较的两边排好顺序；loose 同 key 但不写表名，两边
    查的表换了一张（orders 换成 orders_archive）时拿它比其余各方面，免得同一个条件只因表名不同被报成不同。
    text 给人看：好几张表都有的列写「表.列」（真实表名，不是别名），其余只写列名；full 一律写表名，两边的 text
    一样、key 却不同时改用它说明。excludes 只在 WHERE 的「排除」写法上有：<>、NOT、NOT IN、IS NOT NULL 去掉
    否定之后排除的是什么（IS NOT NULL 写成「列 为空」）。
    """

    key: str
    text: str
    columns: tuple[ColumnRef, ...] = ()
    excludes: str | None = None
    loose: str = ""
    full: str = ""

    @staticmethod
    def plain(key: str, text: str) -> "Part":
        """没有列的一项（表名、COUNT(*) 的「*」）：四种写法相同。"""
        return Part(key=key, text=text, loose=key, full=text)


@dataclass(frozen=True)
class JoinFacet:
    """一次连接。kind：inner / left / right / full / cross / semi / anti / natural。on 是关联条件的合取式排好序后
    用 AND 连起来的写法，没有条件是 None。"""

    kind: str
    table: Part
    on: Part | None = None


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
class Facets:
    """一条查询的各个方面。每一项都按在 SQL 里出现的先后排、按 key 去重；parse_error 不为空时其余都是空的。"""

    dialect: str | None = None
    tables: tuple[Part, ...] = ()
    joins: tuple[JoinFacet, ...] = ()
    where: tuple[Part, ...] = ()
    time: tuple[TimePredicate, ...] = ()
    group_by: tuple[Part, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    having: tuple[Part, ...] = ()
    limit: str | None = None
    parse_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def facets(sql: str, dialect: str | None = None, *, schema: Mapping[str, Any] | None = None) -> Facets:
    """把一条查询拆成各个方面。从不抛异常：解析不了、或者分析时出了意外，返回带 parse_error 的 Facets。

    schema 是数据源的 schema_cache 形状（{"tables": {表名: {"columns": [{"name": …}]}}}），可以不给。给了的话，
    几张表连在一起、列又没加表前缀时按它认列是哪张表的；不给就照写列名，比较时可能把同一列当成两列。
    """
    read = _read(dialect)
    root, problem = _parse(sql, read)
    if root is None:
        return Facets(dialect=read, parse_error=problem)
    try:
        return _Statement(root, read, schema).facets()
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
    """一段 SELECT 的一个来源。kind：table（实表）/ derived（CTE、子查询）/ other（表函数、认不出的别名）。"""

    kind: str
    name: str                       # 给人看：表名（有库名就带上）、CTE 名、子查询别名
    key: str                        # 比较用：name_key
    idents: tuple[Any, ...] = ()    # 表名各段的 Identifier：给人看的写法照原样写回去
    query: Any = None               # derived：里面那段查询


class _Statement:
    """一条语句的分析：CTE、实表、每段 SELECT 的来源，以及把表达式写成 key / text 的规则。"""

    def __init__(self, root: Any, read: str | None, schema: Mapping[str, Any] | None) -> None:
        from sqlglot import exp

        self.root = root
        self.read = read
        self.ctes: dict[str, Any] = {}
        for cte in root.find_all(exp.CTE):
            if cte.alias:
                self.ctes.setdefault(name_key(cte.alias), cte.this)
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
        items = ([select.args["from_"].this] if select.args.get("from_") is not None else []) + \
            [j.this for j in select.args.get("joins") or []]
        out = _Sources()
        for i, node in enumerate(items):
            alias = node.alias_or_name if isinstance(node, (exp.Table, exp.Subquery)) else ""
            slot = name_key(alias) if alias else f"#{i}"
            if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and node.name:
                if self._is_cte(node):
                    out[slot] = _Source("derived", node.name, name_key(node.name), (node.this.copy(),),
                                        self.ctes[name_key(node.name)])
                else:
                    out[slot] = self._table(node)
            elif isinstance(node, exp.Subquery):
                out[slot] = _Source("derived", alias or "子查询", slot, (exp.to_identifier(alias),) if alias else (),
                                    node.this)
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

    def render(self, node: Any, sources: dict[str, _Source], mode: str = "text") -> str:
        """四种写法（见 Part）：key、loose 比较用，名字按 name_key，key 写真实表名、loose 不写；text、full 给人看，
        text 只在好几张表都有这一列时写真实表名，full 一律写（关联条件说的就是两张表各出哪一列，用 full）。"""
        from sqlglot import exp

        copy = node.copy()
        for col in _own_columns(copy):
            source, name = self.resolve(col, sources)
            quoted = isinstance(col.this, exp.Identifier) and bool(col.this.args.get("quoted"))
            if mode in ("key", "loose"):
                col.set("this", exp.to_identifier(name_key(name)))
                col.set("table", exp.to_identifier(source.key) if source is not None and mode == "key" else None)
                col.set("db", None)
                col.set("catalog", None)
                continue
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
        return copy.sql(dialect=self.read, comments=False)

    def canonical(self, node: Any, sources: dict[str, _Source]) -> Any:
        """比较的两边排好顺序（列在左、字面量在右；两边都有列的按 key 排），IN 的取值排好序。返回新的树。"""
        from sqlglot import exp

        def fix(n: Any) -> Any:
            found = _COMPARE.get(type(n).__name__)
            if found is not None and isinstance(n, exp.Binary):
                left, right = n.this, n.expression
                lc, rc = bool(_own_columns(left)), bool(_own_columns(right))
                if (rc and not lc) or (lc and rc and self.render(left, sources, "key")
                                       > self.render(right, sources, "key")):
                    return getattr(exp, found[1])(this=right, expression=left)
            elif isinstance(n, exp.In) and n.expressions and not n.args.get("query") \
                    and not any(_own_columns(e) for e in n.expressions):
                n.set("expressions", sorted(n.expressions, key=lambda e: e.sql(dialect=self.read)))
            return n

        return node.transform(fix)

    def part(self, node: Any, sources: dict[str, _Source], *, excludes: bool = False) -> Part:
        refs: dict[tuple[str | None, str], None] = {}
        for col in _own_columns(node):
            source, name = self.resolve(col, sources)
            refs.setdefault((source.name if source is not None else None, name))
        full = self.render(node, sources, "full")
        # 拿两张表的列互相比的（相关子查询里的 o.id = r.order_id）和关联条件一样，给人看也写表名
        across = len({table for table, _ in refs}) > 1
        return Part(key=self.render(node, sources, "key"), text=full if across else self.render(node, sources),
                    columns=tuple(refs), excludes=self._excludes(node, sources) if excludes else None,
                    loose=self.render(node, sources, "loose"), full=full)

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
        for select in self.root.find_all(exp.Select, bfs=False):
            sources = self.sources(select)
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
            clause = select.args.get("having")
            having += [self.part(self.canonical(c, sources), sources)
                       for c in (_conjuncts(clause.this) if clause is not None else [])]
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
        )

    def _join_drafts(self, select: Any, sources: dict[str, _Source]) -> list[dict[str, Any]]:
        from sqlglot import exp

        first = select.args.get("from_")
        first_alias = first.this.alias_or_name if first is not None and \
            isinstance(first.this, (exp.Table, exp.Subquery)) else ""
        drafts: list[dict[str, Any]] = []
        for i, join in enumerate(select.args.get("joins") or []):
            target = join.this
            alias = target.alias_or_name if isinstance(target, (exp.Table, exp.Subquery)) else ""
            slot = name_key(alias) if alias else f"#{i + 1}"
            conds: list[Any] = []
            if join.args.get("on") is not None:
                conds = [self.canonical(c, sources) for c in _conjuncts(join.args["on"])]
            elif join.args.get("using") and first_alias and alias:
                # USING (id)：按「FROM 的那张表.id = 这张表.id」写，和 ON 的写法放在一起比
                conds = [self.canonical(exp.EQ(this=exp.column(ident.name, table=first_alias),
                                               expression=exp.column(ident.name, table=alias)), sources)
                         for ident in join.args["using"]]
            drafts.append({"kind": _join_kind(join), "slot": slot, "source": sources.get(slot), "conds": conds})
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
        table = Part.plain(source.key, source.name) if source is not None else Part.plain("?", "?")
        triples = sorted((self.render(c, sources, "key"), self.render(c, sources, "loose"),
                          self.render(c, sources, "full")) for c in draft["conds"])
        on = Part(key=" AND ".join(t[0] for t in triples), text=" AND ".join(t[2] for t in triples),
                  loose=" AND ".join(sorted(t[1] for t in triples)), full=" AND ".join(t[2] for t in triples)) \
            if triples else None
        return JoinFacet(kind=draft["kind"], table=table, on=on)

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

    def _group(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """分组键。GROUP BY 1 换成 SELECT 里对应的表达式，GROUP BY 别名换成别名指的表达式（MySQL、SQLite 的读法）。"""
        from sqlglot import exp

        group = select.args.get("group")
        if group is None:
            return []
        selects = select.selects
        by_alias = {name_key(s.alias): s.this for s in selects if isinstance(s, exp.Alias) and s.alias}
        out = []
        for item in group.expressions:
            target = _unparen(item)
            if isinstance(target, exp.Literal) and not target.is_string and str(target.this).isdigit():
                index = int(target.this) - 1
                if 0 <= index < len(selects):
                    target = selects[index].this if isinstance(selects[index], exp.Alias) else selects[index]
            elif isinstance(target, exp.Column) and not target.table and name_key(target.name) in by_alias:
                target = by_alias[name_key(target.name)]
            out.append(self.part(self.canonical(target, sources), sources))
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


# ==========================================================================
# 比较
# ==========================================================================


@dataclass(frozen=True)
class FacetDiff:
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同）/ filter / aggregate / join / tables /
    group_by / having / limit / parse。a、b 是两边各自的写法（给人看，没有就是 None），text 是一句中文说明。
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
    """只涉及一列的条件：那一列（表.列；loose 时只看列名），用来把两边对同一列的不同写法配成一对。"""
    if len(part.columns) != 1:
        return None
    table, column = part.columns[0]
    return name_key(column) if loose else f"{name_key(table or '')}.{name_key(column)}"


def _verb(g: Aggregate) -> str:
    return ("去重" if g.distinct else "") + _VERBS.get(g.func, f"用 {g.func.upper()} 汇总")


def _rows(limit: str | None) -> str:
    if limit is None:
        return "不限行数"
    return f"只取前 {limit} 行" if limit.isdigit() else f"按 LIMIT {limit} 取行"


class _Compare:
    """一次比较：两边的叫法、列的中文名，以及按哪种写法比。

    两边各有一张只在自己这边出现、又不是关联进来的表（orders 换成了 orders_archive）时，其余各方面按不写表名的
    loose 比：表换了这件事由一条「tables」说清，同一个条件、同一个聚合不再因为表名不同被重复报一遍。
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
        if {self.k(p) for p in ca} != {self.k(p) for p in cb}:
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
        if {self.k(t.condition) for t in a.time} != {self.k(t.condition) for t in b.time}:
            ra = " AND ".join(t.condition.text for t in a.time)
            rb = " AND ".join(t.condition.text for t in b.time)
            return [FacetDiff("time_range", ra, rb, _say(la, "的时间范围是", ra, "，", lb, "是", rb))]
        return []

    def filters(self) -> list[FacetDiff]:
        timed = {self.k(t.condition) for t in (*self.a.time, *self.b.time)}
        fa = [p for p in self.a.where if self.k(p) not in timed]
        fb = [p for p in self.b.where if self.k(p) not in timed]
        only_a = [p for p in fa if self.k(p) not in {self.k(q) for q in fb}]
        only_b = [p for p in fb if self.k(p) not in {self.k(q) for q in fa}]
        out: list[FacetDiff] = []
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

        ga = [g for g in self.a.aggregates if key(g) not in {key(h) for h in self.b.aggregates}]
        gb = [g for g in self.b.aggregates if key(g) not in {key(h) for h in self.a.aggregates}]
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
        la, lb = self.la, self.lb

        def index(f: Facets) -> dict[str, list[JoinFacet]]:
            out: dict[str, list[JoinFacet]] = {}
            for j in f.joins:
                out.setdefault(j.table.key, []).append(j)
            return out

        ja, jb = index(self.a), index(self.b)
        out: list[FacetDiff] = []
        for key in dict.fromkeys([*ja, *jb]):
            xs, ys = ja.get(key, []), jb.get(key, [])
            for x, y in zip(xs, ys):
                if x.kind != y.kind:
                    kx, ky = _JOIN_WORDS.get(x.kind, x.kind), _JOIN_WORDS.get(y.kind, y.kind)
                    out.append(FacetDiff("join", kx, ky, _say(la, "用", kx, "关联", x.table.text, "表，", lb, "用", ky)))
                if self.k(x.on) != self.k(y.on):
                    xa, ya = (x.on.text if x.on else "（无条件）"), (y.on.text if y.on else "（无条件）")
                    out.append(FacetDiff("join", xa, ya, _say(la, "关联", x.table.text, "表的条件是", xa, "，", lb, "是",
                                                              ya)))
            out += [FacetDiff("join", x.table.text, None, _say(la, "关联了", x.table.text, "表，", lb, "没有"))
                    for x in xs[len(ys):]]
            out += [FacetDiff("join", None, y.table.text, _say(lb, "关联了", y.table.text, "表，", la, "没有"))
                    for y in ys[len(xs):]]
        # 关联之外的表（FROM 的表、子查询里的表）只在一边出现的
        if self.swapped:
            ta, tb = self.ta[0], self.tb[0]
            return out + [FacetDiff("tables", ta.text, tb.text, _say(la, "查的是", ta.text, "表，", lb, "查的是", tb.text,
                                                                     "表"))]
        out += [FacetDiff("tables", t.text, None, _say(la, "用到了", t.text, "表，", lb, "没有")) for t in self.ta]
        out += [FacetDiff("tables", None, t.text, _say(lb, "用到了", t.text, "表，", la, "没有")) for t in self.tb]
        return out

    def group(self) -> list[FacetDiff]:
        a, b = self.a.group_by, self.b.group_by
        if {self.k(p) for p in a} == {self.k(p) for p in b}:
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
        ka, kb = {self.k(p) for p in self.a.having}, {self.k(p) for p in self.b.having}
        return [FacetDiff("having", p.text, None, self.one_sided(self.la, self.lb, p, group=True))
                for p in self.a.having if self.k(p) not in kb] + \
            [FacetDiff("having", None, p.text, self.one_sided(self.lb, self.la, p, group=True))
             for p in self.b.having if self.k(p) not in ka]

    def limit(self) -> list[FacetDiff]:
        a, b = self.a.limit, self.b.limit
        return [FacetDiff("limit", a, b, _say(self.la, _rows(a), "，", self.lb, _rows(b)))] if a != b else []


def diff_facets(a: Facets, b: Facets, labels: tuple[str, str] = ("A", "B"), *,
                names: Mapping[str, str] | None = None) -> list[FacetDiff]:
    """逐个方面比两份 facets，每处不同一条，按对结果影响大致从大到小排：时间、筛选、聚合、连接和表、分组、HAVING、
    LIMIT。完全一致返回空列表。

    labels 是两边的叫法（缺省 A / B，双盲复算可以传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」:
    中文名}，可以用 catalog_labels 从数据目录取），给了的话时间列写成「支付时间 paid_at」。任一边解析不了就只返回
    一条 parse，不逐项比较：拿半份结构去比，只会报出一堆不存在的不同。
    """
    la, lb = labels
    if a.parse_error or b.parse_error:
        if a.parse_error and b.parse_error:
            text = _say(la, "：", a.parse_error, "；", lb, "：", b.parse_error, "；没有逐项比较")
        else:
            side, error = (la, a.parse_error) if a.parse_error else (lb, b.parse_error)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return [FacetDiff("parse", a.parse_error, b.parse_error, text)]
    cmp = _Compare(a, b, labels, names)
    return cmp.time() + cmp.filters() + cmp.aggregates() + cmp.joins() + cmp.group() + cmp.having() + cmp.limit()


__all__ = ["MAX_SQL_CHARS", "PLACEHOLDER", "Aggregate", "ColumnRef", "FacetDiff", "Facets", "JoinFacet",
           "NormalizedSql", "Part", "TimePredicate", "catalog_labels", "diff_facets", "facets", "normalize_sql"]
