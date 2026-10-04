"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），运行
对比要回答「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了），反方审稿要看自己
的查询和原查询的口径是否一致。三处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、注释、不加引号的名字的大小写都不算不同。
- facets：把一条查询拆成几个方面——用到的表、连接（类型、关联进来的表、ON 条件、外连接以哪一侧为主表）、WHERE
  的各个合取式、其中的时间条件（列、运算符、取值）、分组、聚合（函数、参数、是否去重）、算出来的列（最外层的算法、
  CTE 和子查询里被外面用到的列的定义，窗口函数也在这里）、HAVING、取哪几行（LIMIT、OFFSET、取前几行时的排序）。
  别名一律换成真实表名（条件里套着的子查询也是），等式、比较、加法、乘法的两边排好顺序：o.paid_at >= '2026-09-01'
  和 '2026-09-01' <= orders.paid_at 是同一个条件。
- diff_facets：逐个方面比两份 facets，每处不同给一句中文说明。

**逐段收集。** CTE、子查询里的条件、聚合和取行也算进来：盲算可能把有效记录条件写在 CTE 里，原流程写在外层，按
「整条语句里有哪些条件」比才不会把同一个口径报成不同。只收用到了的段：定义了却没被引用的 CTE 不影响结果，不收
（盲算定义了只筛已付的 CTE 却直接查 orders，少的那个条件要报出来）。逗号连接（以及 INNER JOIN 之外又写在 WHERE
里的等值关联）写在 WHERE 里的列等式算作关联条件，不算筛选。条件里套着的子查询（EXISTS、IN (SELECT …)）既单算
一段，也按它自己那一段的来源整个写进外层那个条件：别名在子查询里定义，拿外层的来源去认，同一个条件只因别名不同
就会被报成不同。

**各自起的名字不进比较。** 表的别名换成真实表名；CTE、FROM 里的子查询（含 LATERAL）没有真实表名可换，单看一条
语句时按它在语句里的位置编号（@1、@2…，从外往里、按第一次用到的先后），给人看时照写名字；条件里的子查询（IN、
EXISTS、标量子查询）选出来的列名不影响取值，比较时去掉。这些名字都是各自起的，双盲复算的盲算更是：按名字比，同一个
口径会被说成换了口径。同一个来源在一段里出现两次（自己关联自己）时，两个别名按各自在关联条件里出的列区分
（b2.user_id = b1.referrer_id：b1 出 referrer_id、b2 出 user_id），b1.total > b2.total 写反了认得出，换了别名、
换了先后不算不同。

**两边的派生来源按结构配对。** 位置编号只看先后：一边多出一个排在前面的 CTE，或者内连接两边换了先后，编号就错开
了；可也不能干脆不看是哪一个（两个 CTE 都算出 total 时，外层用的是付款合计还是退款合计，正是要报的不同）。所以
diff_facets 先给两边的每个派生来源算一份结构标识（见 _Derived）：里面读到的实表（FROM / JOIN，往里套着的也算）、
选出来的各列怎么定义（规范化后的定义式，如 total 是 SUM(orders.amount)）、按什么分组、里面的条件、整段规范化后的
写法，以及起的名字。这些写法里往里套着的派生来源写它自己的结构标识（见 _Statement._signature），和名字、编号无关：
net 读的是 paid.total - refunded.total 还是反过来，写出来不一样。再按从严到宽的几级一对一地配（见 _align）：整段
一样；读的表、各列定义、分组一样（里面的筛选条件可以不同）；读的表和选出来的列名一样（某一列的算法可以不同）；读的
表和名字一样；最后三级只在两边都只剩一个时才配：只是读的表一样、只是名字一样、只是选出来的列名一样。同一级有几个
都配得上时，先挑里面的条件重合最多的，再挑同名的（名字是各自起的：两边都叫 paid，一边筛已付、一边筛未付，不是一
回事），最后才看位置编号的先后。哪一级都配不上的不配：一边多出来的 CTE 不会被拿去和另一边不相干的 CTE 凑成一对。
配成一对的派生来源里，两边起名不同的列按定义配成同一列（一边叫 total、一边叫 sum_amt，见 _renamed）。配好之后，
第二条 SQL 的派生来源改用第一条里对应的那个编号（配不上的另起编号），按这个共同的编号重新分析两边再逐个方面比：
比较的两边谁在前、关联条件的先后都按共同的编号排，内连接换了先后也排成同一个方向。

**派生来源里的算法。** 外面引用的是「t.net」，两边写的一样，net 在里面怎么算的（amount * 0.9 还是 * 0.8、窗口
函数按什么分区排序、是 paid.total 还是 refunded.total）只看外面看不出来：派生来源里被外面用到、又不是原样透出
一列的列，按定义比（见 Computed）。最外层算出来的列（SUM(p.total) - SUM(r.total)、窗口函数、CASE）也按算法比：
聚合两边的集合一样、位置换了，聚合那一面看不出来。

**什么时候能透过派生来源。** CTE、子查询原样透出一张表的列（SELECT *，或者直接选出这一列）时，外面按它写的条件、
聚合换成那张表的列：先在 CTE 里筛一遍再查和直接查是同一列。它要取前几行（LIMIT、OFFSET）、或者算了窗口函数（这一列
又不是分区键）时不透：先取前 10 行再筛已付，和先筛已付再取前 10 行取到的不是同一批；两边只差在这里的，报筛的先后
不同。时间条件另外透过「同一列套着日期函数、CAST、MIN / MAX」算出来的列（DATE(paid_at) AS d），日期表达式写在 CTE
里，按支付时间还是下单时间筛照样认得出。

**外连接。** 按以哪一侧为主表（保留它的全部行）比，不按写的是 LEFT 还是 RIGHT：orders LEFT JOIN refunds 和
refunds RIGHT JOIN orders 一样，和 refunds LEFT JOIN orders 不一样。同一段 WHERE 里拿可空那一侧的列比较、IN、
LIKE 的（空值都不成立），把没关联上的行筛掉了，按内连接比：写的是 LEFT JOIN，实际成了 INNER JOIN，要报出来。连接
里的内连接（含逗号连接、CROSS JOIN）按涉及的来源和条件比，不按哪一侧是关联进来的；一边独有的一次内连接，关联
进来的那一侧另一边也有时，报的是它接上的另一头（FROM stores JOIN orders、另一边只查 orders：多出来的是 stores）。

**写法一样、意思不同时说清楚。** 两边同名的派生来源其实不是同一个（盲算的 t1 是原流程的 t2）时，光写「t1.total」
两边一模一样：带上各列的来历（t1.total 即 SUM(orders.amount)）；来历也一样（两个 CTE 只差里面的条件）时，说
「盲算的 paid 对应原流程的 open」。

仍做不到的：
- 两边起名不同的派生来源的列，两边各自多出不止一列、定义又都不一样时配不上，算不同的列，外层按它们写的条件照报
  成两边各有一个；读的表、选出来的列名、名字全换了的两个派生来源不配对，同样照报。
- 读同一张表、各列定义也一样的两个派生来源，两边都改了里面的条件、改得一个都不重合，又各起了名字时，只能按先后配，
  可能配反。
- 自己关联自己、两个别名在关联条件里出的列一样（b1.user_id = b2.user_id）时分不出谁是谁；这时 WHERE 里对调过来，
  算出来的行数也一样，只有最外层直接选其中一个别名的列（不比，见下）时会漏。
- 同一个算法写法不同：三项以上的加法、乘法换了先后（a + b + c 对 c + b + a）、括号展开（a * (b + c) 对 a * b +
  a * c）、取日期以外的等价函数，算不同的算法。最外层只比算出来的列：光一列、字面量不比；ROUND、CAST、COALESCE
  给默认值这类外壳当作同一个数（最外层取整到分不算口径不同）。派生来源里没被外面用到的列不比；UNION 按各支同一个
  位置比。
- 筛的先后只认取前几行（LIMIT、OFFSET）、窗口函数（含 DISTINCT ON）挡住的；QUALIFY、GROUP BY 之后才筛的不认。
  外连接被 WHERE 变成内连接只认同一段 WHERE 里直接用到可空那一侧列的比较、IN、LIKE、BETWEEN、IS NOT NULL；套在
  COALESCE、CASE 里，或者写在外面一层 WHERE 里的不认。连接上别的做不到的见 _Compare.joins。
- facets 单独返回的 key 仍是位置编号；不是 facets 算出来的 Facets（没记原文）diff_facets 也只能按位置编号比，
  没用到的 CTE、起名不同的列也不再另配。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就带 parse_error 返回，从不抛
异常。没加表前缀、又有好几张表可能有这一列的，照写列名（给了表结构才去对）。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import hashlib
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
#: 不写编号的写法（render 的 anon 模式）里派生来源一律写成这个：排比较的两边谁在前时先看实表的列还是派生来源的列
_ANON = "@"
#: render 里比较用的写法（见 render）：名字按 name_key，别名、各自起的名字都去掉
_COMPARING = frozenset({"key", "loose", "anon", "shape", "pierce"})
#: 外连接写在 WHERE 里会把可空那一侧没关联上的行（那一侧的列都是空）筛掉的条件：拿空值比都不成立
_NULL_REJECTING = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike", "Between", "In")


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

    后面三项不进比较（两个 Part 相等只看前面几项），只用来把不同说清楚：
    origins 是这一项用到的派生来源的列各自的来历 [(「t1.total」, 「SUM(orders.amount)」)]（见 _Statement._origin）：
    两边写法一模一样（都是 t1.total > 100），各自的 t1 却不是同一个时，带上它才分得清。pierce 是一路透过派生来源写到
    实表的 key，只在中途被挡住时才有（这一项按派生来源的列写，见 _Statement.through）；stage 说是被什么挡住的（「取 t
    的前 10 行」「算 t 的窗口函数」）：两边 pierce 一样、只有一边被挡住，是同一个条件筛的先后不同。
    """

    key: str
    text: str
    columns: tuple[ColumnRef, ...] = ()
    excludes: str | None = None
    loose: str = ""
    full: str = ""
    column_keys: tuple[str, ...] = ()
    origins: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    pierce: str = field(default="", compare=False)
    stage: str = field(default="", compare=False)

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
    refunded 和 FROM refunded JOIN paid），diff_facets 比它们时按 sides 和条件比，不按哪一侧是关联进来的。

    keep、nullable 只在 LEFT / RIGHT JOIN 上有：保留全部行的主表那一侧、关联不上时记空的那一侧（比较用的写法，排好
    序）。LEFT JOIN 关联进来的那一侧可空，关联条件里的其余来源（没有就是它之前的各个来源）是主表；RIGHT JOIN 反过来。
    orders LEFT JOIN refunds 和 refunds RIGHT JOIN orders 是同一回事，和 refunds LEFT JOIN orders 不是：diff_facets
    按主表比，不按写的是 LEFT 还是 RIGHT。rejects 是同一段 WHERE 里把可空那一侧没关联上的行筛掉的条件（那一侧的列
    拿来比较、IN、LIKE……，空值都不成立）：有它这次外连接实际等于内连接。"""

    kind: str
    table: Part
    on: Part | None = None
    other: tuple[str, ...] = ()
    sides: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    nullable: tuple[str, ...] = ()
    rejects: Part | None = None


@dataclass(frozen=True)
class TimePredicate:
    """WHERE 里的一个时间条件。op：= / <> / > / >= / < / <= / between / in。value 是另一侧给人看的写法：
    BETWEEN 是「低 AND 高」，IN 是「(…)」。

    column 是按哪一列筛时间：条件写的是 CTE、子查询算出来的列（WITH t AS (SELECT DATE(paid_at) AS d …) … WHERE
    d >= …）时，它在里面是同一列套着日期函数、CAST、MIN / MAX 的，换成里面那一列，一直换到实表（paid_at）：按支付
    时间还是下单时间筛，日期表达式写在 CTE 里也认得出。via 是路过的派生来源的列（「编号.列」，不进比较）：时间那一面
    已经报了按哪一列筛不同，派生来源那一列的定义不同就不再另报一遍。"""

    column: Part
    op: str
    value: str
    condition: Part
    via: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class Aggregate:
    """一个聚合。func 是函数名的小写（sum / count / avg / min / max …）；arg 是参数，COUNT(*)、COUNT(1) 的 arg
    是「*」，去重聚合是 DISTINCT 里面的表达式；text 是整个聚合给人看的写法。窗口函数不算（它们按所在的那一列比，见
    Computed）。"""

    func: str
    arg: Part
    distinct: bool
    text: str


@dataclass(frozen=True)
class Computed:
    """选出来的一列是怎么算的。两种：

    - 最外层（UNION 的各支）选出来的列，不是光一列（分组那一面比）、也不是字面量的，比如 SUM(p.total) - SUM(r.total)、
      SUM(amount) OVER (PARTITION BY user_id)、CASE …；ROUND、CAST、COALESCE 给默认值这类外壳先剥掉（见
      _unwrap_value）。光一个聚合也收：另一边可能拿同一个合计再算了一下（SUM(amount) 对 SUM(amount) / 100）。scope、
      scope_key 为 None，name 为空串。
    - 派生来源（CTE、FROM 里的子查询）里被外面用到、又不是原样透出一列的列：它在里面的定义（amount * 0.9、
      DATE(paid_at)、ROW_NUMBER() OVER (…)、p.total - r.total）。外面按「t.net」引用它，两边写的都是 t.net，算法却可能
      不同，只比外面的写法看不出来。scope 是派生来源给人看的名字，scope_key 是它的编号（两边配好对的共同编号），
      name 是列名的 name_key，shown 是列名的原样写法。

    expr 是定义（按它那一段的来源写的 Part；UNION 的各支用「|」连起来）。template 是把里面的聚合换成占位符之后的写法、
    aggregates 是里面的聚合（按出现的先后）：两边只差在聚合上（SUM(amount) * 100 对 SUM(paid_amount) * 100）、聚合
    那一面又已经报了，这里不再报一遍；聚合的集合一样、只是位置换了（SUM(a) - SUM(b) 对 SUM(b) - SUM(a)）才在这里报。
    """

    scope: str | None
    scope_key: str | None
    name: str
    shown: str
    expr: Part
    template: Part
    aggregates: tuple[Aggregate, ...] = ()


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
    _Compare.added），外连接的主表、可空那一侧也按它写名字。

    expressions 是算出来的列（见 Computed）。derived 是用到了的派生来源（key 是编号，text 是名字，含条件里套着的子
    查询用到的 CTE）：两边写法一样、同名的派生来源却不是同一个时，拿它说清「盲算的 t1 对应原流程的 t2」。

    只收用到了的段：没用到的 CTE（定义了、外面没引用）里的条件、聚合、表不影响结果，不收（见 _Statement._enliven）。

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
    expressions: tuple[Computed, ...] = ()
    derived: tuple[Part, ...] = ()
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


def _is_star(item: Any) -> bool:
    """SELECT 列表里的一项是 * 或 t.*。"""
    from sqlglot import exp

    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star)


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


def _output_names(query: Any) -> list[str | None]:
    """派生来源选出来的各列叫什么（外面按这个名字引用它们）：按第一支 SELECT 的位置，* 为 None。CTE、子查询自己
    写了列名的（WITH t(n) AS …、(…) AS t(a, b)，递归 CTE 常这么写）按写的列名。"""
    from sqlglot import exp

    branches = _branches(query)
    if not branches:
        return []
    names = [None if _is_star(item) else item.alias_or_name for item in branches[0].selects]
    holder = query.parent
    if isinstance(holder, exp.Subquery) and isinstance(holder.parent, exp.Lateral) and not holder.alias:
        holder = holder.parent          # LATERAL 的别名（连同列名）挂在 LATERAL 上
    alias = holder.args.get("alias") if isinstance(holder, (exp.CTE, exp.Subquery, exp.Lateral)) else None
    columns = [c.name for c in alias.columns] if isinstance(alias, exp.TableAlias) else []
    for i, column in enumerate(columns[:len(names)]):
        names[i] = column
    return names


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


def _value_column(node: Any) -> Any:
    """派生来源一列的定义是不是「还是那一列」：光一列，或者同一列套着日期函数、CAST、MIN / MAX（按支付时间算的日期、
    每人最后一次付款的时间）；是的话返回那一列，不是返回 None。窗口函数（ROW_NUMBER() OVER (ORDER BY paid_at) 是排名，
    不是时间）、别的聚合、好几列算出来的、套着子查询的都不算。"""
    from sqlglot import exp

    n = _unparen(node)
    if isinstance(n, exp.Column):
        return n if n.name and not n.is_star else None
    if any(isinstance(x, (exp.Window, exp.Select, exp.Subquery)) or
           (isinstance(x, exp.AggFunc) and not isinstance(x, (exp.Min, exp.Max))) for x in n.walk()):
        return None
    cols = _own_columns(n)
    if len(cols) != 1:
        return None
    if _datey(n) or _bare_column(n) is not None or any(isinstance(x, (exp.Min, exp.Max)) for x in n.walk()):
        return cols[0]
    return None


def _unwrap_value(node: Any) -> Any:
    """剥掉只改写法、不改是哪个数的外壳（括号、类型转换、ROUND、COALESCE / IFNULL 给默认值）：最外层的
    ROUND(SUM(amount), 2)、COALESCE(SUM(amount), 0) 还是那个合计，只是显示上的取舍。"""
    from sqlglot import exp

    while True:
        node = _unparen(node)
        if isinstance(node, (exp.Cast, exp.TryCast, exp.Round)) and node.this is not None:
            node = node.this
        elif isinstance(node, exp.Coalesce) and node.expressions and \
                all(isinstance(_unparen(e), exp.Literal) for e in node.expressions):
            node = node.this
        else:
            return node


def _as_date(node: Any) -> Any:
    """「取日期」的几种写法统一成 CAST(… AS DATE)（比较用，给人看的照原样）：DATE(paid_at)、paid_at::date、
    TRY_CAST(paid_at AS DATE)，MySQL 的 DATE() 读成 TsOrDsToDate。各方言、各人写法不同，取的都是同一天；带时区偏移、
    格式的（DATE(paid_at, '+8 hours')）不是同一天，不动。"""
    from sqlglot import exp

    def to_date(n: Any) -> bool:
        return isinstance(n, exp.DataType) and n.this == exp.DataType.Type.DATE

    if isinstance(node, exp.Date) and node.this is not None and not node.args.get("zone") \
            and not node.args.get("expressions"):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    if isinstance(node, exp.TsOrDsToDate) and node.this is not None and not node.args.get("format"):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    if isinstance(node, exp.TryCast) and to_date(node.args.get("to")):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    return node


def _atomic(node: Any) -> bool:
    """算式里的一项不是别的加减乘除：两边对调不会改变怎么结合（a * b / c 换成 b / c * a 就不一样了）。"""
    from sqlglot import exp

    return not isinstance(_unparen(node), (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv))


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
    role: str = ""                  # 同一个来源在这一段里出现了不止一次时，这一次是哪一个（见 _Statement._roles）

    @property
    def ref(self) -> str:
        """比较用的「哪一个来源」：key，自己关联自己时带上 role（orders#parent_id）。"""
        return f"{self.key}#{self.role}" if self.role else self.key


@dataclass(frozen=True)
class _Derived:
    """一个派生来源（CTE、FROM / JOIN 里的子查询、LATERAL 子查询）的结构标识，和起的名字、位置编号无关的部分都在
    这里，diff_facets 拿它把两边的派生来源配成对（见 _align）。各项都用按结构写的写法（render 的 shape 模式：实表
    写表名，往里套着的派生来源写它自己的结构标识，见 _Statement._signature），所以两边写法一样就相同，不受先后、
    名字影响；往里套着的两个派生来源（付款合计、退款合计）也分得开：net 是 paid.total - refunded.total 还是反过来，
    写出来不一样。"""

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
    里面的条件重合最多的，再配同名的，最后按位置编号的先后（几个一模一样的派生来源按先后一一对应）；这一级所有
    配得上的对一起排，不是左边挨个挑，免得前一个挑走后一个更合适的。条件排在名字前面：名字是各自起的，两边都叫
    paid 的两个 CTE，一边筛 status = 'paid'、一边筛 status = 'open'，算的不是一回事；条件一个都不重合时才看名字。
    最后三级证据弱，只在两边各只剩一个这样的时才配，免得猜错。哪一级都配不上的就不配：一边多出来的 CTE（盲算先筛
    一遍 VIP 用户）和另一边不相干的 CTE 凑成一对，会把外层不相干的条件说成「关联条件不同」。
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
            candidates += [((-len(y.conditions & x.conditions), y.name != x.name, x.number, y.number), x, y)
                           for y in found]
        for _, x, y in sorted(candidates, key=lambda c: c[0]):
            if x.number not in pairs.values() and y.number not in pairs:
                pairs[y.number] = x.number
        rest_x = [x for x in rest_x if x.number not in pairs.values()]
        rest_y = [y for y in rest_y if y.number not in pairs]
    return pairs


def _renamed(xs: list[_Derived], ys: list[_Derived], pairs: Mapping[int, int]) -> dict[tuple[int, str], str]:
    """配成一对的派生来源里两边起名不同的列，配成同一列：{(第二条里的位置编号, 列名): 第一条里的列名}。

    外面按名字引用这些列，一边叫 total、一边叫 sum_amt，不配起来就是两列，外面按它写的条件、聚合全成了两边各有
    一个。只在两边各自独有的列名之间配：定义（结构写法）一样的一对一配上；剩下两边各只有一列时也配上（盲算换了名字
    又改了算法：SUM(amount) AS total 对 SUM(amount - discount) AS net_total，外面用在同一个位置），定义的不同由算出来
    的列那一面报（见 _Compare.expressions）。"""
    by_number = {x.number: x for x in xs}
    out: dict[tuple[int, str], str] = {}
    for y in ys:
        x = by_number.get(pairs.get(y.number, -1))
        if x is None:
            continue
        cx = {name: shape for name, shape in x.columns if name and name != "*"}
        cy = {name: shape for name, shape in y.columns if name and name != "*"}
        rest_x = [name for name in cx if name not in cy]
        rest_y = [name for name in cy if name not in cx]
        for name in list(rest_y):
            same = [other for other in rest_x if cx[other] == cy[name]]
            if len(same) == 1 and sum(1 for other in rest_y if cy[other] == cy[name]) == 1:
                out[(y.number, name)] = same[0]
                rest_x.remove(same[0])
                rest_y.remove(name)
        if len(rest_x) == 1 and len(rest_y) == 1:
            out[(y.number, rest_y[0])] = rest_x[0]
    return out


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
        self.columns: dict[str, set[str]] = {}
        for name, meta in ((schema or {}).get("tables") or {}).items():
            if isinstance(meta, Mapping):
                self.columns[name_key(str(name))] = {name_key(str(c.get("name"))) for c in meta.get("columns") or []
                                                     if isinstance(c, Mapping) and c.get("name")}
        self._memo: dict[int, _Sources] = {}
        self._shared: set[str] | None = None
        #: 各派生来源的结构标识 {位置编号: 摘要}（见 _signature）、各派生来源一列的来历 {(里面那段的 id, 列名): 来历}
        #: （见 _origin）：都和编号无关，重新编号不用清
        self._signatures: dict[int, str] = {}
        self._origins: dict[tuple[int, str], str | None] = {}
        #: 派生来源各列的名字 {里面那段的 id: [列名]}、每段外面筛和里面筛一不一样 {(那段的 id, 列名): 说法}：只看
        #: 语法树，算一次记下来（透视、认列时反复要用）
        self._names_memo: dict[int, list[str | None]] = {}
        self._blocks_memo: dict[tuple[int, str], str | None] = {}
        self._number_derived()
        self._enliven(())

    # ---- 来源

    def _is_cte(self, table: Any) -> bool:
        return not table.text("db") and not table.text("catalog") and name_key(table.name) in self.ctes

    def _reach(self, extra: Iterable[str] = ()) -> set[str]:
        """从最外层那段查询出发（再加上 extra 里的 CTE）一路引用到的 CTE 名：FROM / JOIN 里的、条件和 SELECT 列表里的
        子查询里的都算，用到的 CTE 里面再引用的也算。CTE 的定义本身不算引用：没人用的 CTE 里引用的别的 CTE 也没用到。"""
        from sqlglot import exp

        live = {name for name in extra if name in self.ctes}
        stack = [self.root, *(self.ctes[name] for name in live)]
        while stack:
            for node in stack.pop().walk(prune=lambda n: isinstance(n, exp.CTE)):
                if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and node.name \
                        and self._is_cte(node) and (name := name_key(node.name)) not in live:
                    live.add(name)
                    stack.append(self.ctes[name])
        return live

    def _enliven(self, extra: Iterable[str]) -> None:
        """定下哪些段算数：最外层那段查询，加上从它一路引用到的 CTE（extra 里的 CTE 也当作用到了，见 _aligned）。没
        用到的 CTE 里的条件、聚合、表不影响结果：收进来的话，盲算定义了只筛已付的 CTE 却直接查 orders，会被当成也筛了
        已付；定义了没用的 CTE 里多一个条件，又会被报成多筛了一遍。用到的实表（给人看时要不要写表名）、共有的列名跟着
        重算。"""
        from sqlglot import exp

        live = self._reach(extra)
        #: 没用到的 CTE 里的节点（id）：facets 只收别的段
        self._dead = {id(n) for name, cte in self._cte_defs.items() if name not in live for n in cte.this.walk()}
        tables: dict[str, _Source] = {}
        for table in self._alive(exp.Table):
            if isinstance(table.this, exp.Identifier) and table.name and not self._is_cte(table):
                source = self._table(table)
                tables.setdefault(source.key, source)
        self.tables = list(tables.values())
        #: 整条语句只用到一张实表：给人看的写法一律不写表名
        self.single = len(self.tables) <= 1
        self._shared = None

    def _alive(self, *types: Any) -> Iterator[Any]:
        """用到了的段里这些类型的节点（深度优先、按出现的先后），没用到的 CTE 里的不算（见 _enliven）。"""
        return (node for node in self.root.find_all(*types, bfs=False) if id(node) not in self._dead)

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
        #: 编了号的派生来源 [(位置编号, 起的名字, 里面那段查询, CTE 的 name_key 或 None)]，按编号排：diff_facets 按
        #: 它们的结构配对（见 derived），facets 按它们找各列的定义（见 _expressions）
        self._derived: list[tuple[int, str, Any, str | None]] = []
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
                        self._derived.append((count, _alias(item), body, None))
                        inner.append(body)
                elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) and item.name \
                        and self._is_cte(item):
                    name = name_key(item.name)
                    if name not in ctes:
                        count += 1
                        ctes[name] = count
                        mark(self._cte_defs[name], count)
                        self._derived.append((count, self._cte_defs[name].alias, self.ctes[name], name))
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
                self._derived.append((count, cte.alias, self.ctes[name], name))
        #: {位置编号: 里面那段查询}、{位置编号: 现在比较用的编号}、反过来 {比较用的编号: 位置编号}
        self._bodies = {number: body for number, _, body, _ in self._derived}
        self._labels = {number: f"@{number}" for number in self._bodies}
        self._numbers = {label: number for number, label in self._labels.items()}
        #: 派生来源里改叫另一边那个名字的列 {(比较用的编号, 列名的 name_key): 另一边的列名}（见 relabel）
        self._renames: dict[tuple[str, str], str] = {}

    def relabel(self, ids: Mapping[int, str], renames: Mapping[tuple[int, str], str] | None = None) -> None:
        """派生来源改用 ids 给的编号（{位置编号: 比较用的编号}，没给的照旧写位置编号）。diff_facets 把两边的派生
        来源配好对之后，第二条语句里配上的写第一条里对应的编号，两边比较用的写法才对得上。算好的来源、共有的列名
        都带着旧编号，跟着清掉重算。

        renames 是派生来源里改叫第一条里那个名字的列 {(位置编号, 列名的 name_key): 第一条里的列名}（见 _renamed）：
        比较用的写法照它写，给人看的照原样。"""
        for node, number in self._marked.values():
            node.meta[_DERIVED] = ids.get(number, f"@{number}")
        self._labels = {number: ids.get(number, f"@{number}") for number in self._bodies}
        self._numbers = {label: number for number, label in self._labels.items()}
        self._renames = {(self._labels[number], column): name for (number, column), name in (renames or {}).items()
                         if number in self._labels}
        self._memo.clear()
        self._shared = None

    def column_key(self, source: _Source, column: str) -> str:
        """比较用的列名：派生来源里改叫另一边那个名字的列（见 relabel）照改过的写，别的按 name_key。"""
        key = name_key(column)
        return self._renames.get((source.key, key), key) if source.kind == "derived" else key

    def used_labels(self) -> dict[str, str | None]:
        """用到了的派生来源（见 _enliven）：{比较用的编号: CTE 的 name_key，FROM 里的子查询为 None}。"""
        return {self._labels[number]: cte for number, _, body, cte in self._derived if id(body) not in self._dead}

    def defined_labels(self) -> dict[str, str | None]:
        """定义了的派生来源，用没用到都算：{比较用的编号: CTE 的 name_key，FROM 里的子查询为 None}。"""
        return {self._labels[number]: cte for number, _, _, cte in self._derived}

    def derived(self) -> list[_Derived]:
        """各个派生来源的结构标识（见 _Derived），按位置编号排。"""
        from sqlglot import exp

        out = []
        for number, name, body, _ in self._derived:
            columns: list[tuple[str, str]] = []
            group: list[str] = []
            conditions: set[str] = set()
            for i, select in enumerate(_branches(body)):
                scope = self.sources(select)
                for clause in (select.args.get("where"), select.args.get("having")):
                    if clause is not None:
                        conditions.update(self._shape(c, scope) for c in _conjuncts(clause.this))
                if i:
                    continue                # UNION 选出来的列名、分组按第一支的算
                for item, called in zip(select.selects, self.names(body)):
                    if called is None:
                        columns.append(("*", "*"))
                        continue
                    target = item.this if isinstance(item, exp.Alias) else item
                    columns.append((name_key(called), self._shape(target, scope)))
                if (clause := select.args.get("group")) is not None:
                    selects, aliases = select.selects, self._aliases(select)
                    group += [self._shape(self._target(g, selects, aliases), scope) for g in clause.expressions]
            out.append(_Derived(number=number, name=name_key(name) if name else "",
                                tables=tuple(sorted(self._row_tables(body, frozenset()))),
                                columns=tuple(sorted(columns)), group=tuple(sorted(group)),
                                conditions=frozenset(conditions), body=self._shape(body, {})))
        return out

    def _shape(self, node: Any, sources: dict[str, _Source]) -> str:
        """按结构写（render 的 shape 模式，比较的两边也按结构排）：配对用，和名字、编号无关。"""
        return self.render(self.canonical(node, sources, by="shape"), sources, "shape")

    def _signature(self, number: int) -> str:
        """派生来源的结构标识（shape 写法里引用它的地方写这个）：里面那段查询按 shape 写法写出来再取摘要。里面再套着
        的派生来源也写它们各自的结构标识，所以付款合计、退款合计两个 CTE 的同名列 total 在外面一层也分得开，和起的
        名字、编号无关；取摘要是免得一层套一层越写越长。递归 CTE 引用自己时写「@」。"""
        if number not in self._signatures:
            self._signatures[number] = _ANON
            text = self._shape(self._bodies[number], {})
            self._signatures[number] = "@" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return self._signatures[number]

    def _signature_of(self, label: str) -> str:
        """比较用的编号 → 那个派生来源的结构标识；认不出的编号写「@」。"""
        number = self._numbers.get(label)
        return self._signature(number) if number is not None else _ANON

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
        aliases: dict[str, str] = {}
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
            aliases[slot] = alias
        self._roles(select, out, aliases)
        self._memo[id(select)] = out
        parent = select.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        out.outer = self.sources(parent) if parent is not None else None
        return out

    @staticmethod
    def _roles(select: Any, sources: dict[str, _Source], aliases: Mapping[str, str]) -> None:
        """同一个来源在这一段里出现了不止一次（自己关联自己：orders o1 JOIN orders o2、base b1 JOIN base b2）时，各自
        按在关联条件（ON、WHERE 里两列相等）里出的是哪几列区分，记在 role 上（就地改）：比较用的写法写「来源#列」，
        和别名、先后都无关。b2.user_id = b1.referrer_id 里 b1 出 referrer_id、b2 出 user_id，b1.total > b2.total 写反了
        就认得出来；不然两边都写成 base.total，写反了也一样。出的列一样（b1.user_id = b2.user_id）分不出，也不用分：
        两个对调过来意思一样。给人看的写法这时写别名：都写 orders.amount 读不出是哪一个。"""
        from sqlglot import exp

        groups: dict[str, list[str]] = {}
        for slot, source in sources.items():
            if source.kind in ("table", "derived"):
                groups.setdefault(source.key, []).append(slot)
        repeated = [slots for slots in groups.values() if len(slots) > 1]
        if not repeated:
            return
        conds = [j.args["on"] for j in select.args.get("joins") or [] if j.args.get("on") is not None]
        if (where := select.args.get("where")) is not None:
            conds.append(where.this)
        joined: dict[str, set[str]] = {}
        for cond in conds:
            for eq in _conjuncts(cond):
                n = _unparen(eq)
                if not isinstance(n, exp.EQ):
                    continue
                cols = [_unparen(n.this), _unparen(n.expression)]
                slots = [name_key(c.table) if isinstance(c, exp.Column) and c.table else None for c in cols]
                if None in slots or slots[0] == slots[1] or not all(s in sources for s in slots):
                    continue
                for col, slot in zip(cols, slots):
                    joined.setdefault(slot, set()).add(name_key(col.name))
        for slots in repeated:
            roles = {slot: "+".join(sorted(joined.get(slot, ()))) for slot in slots}
            for slot in slots:
                if roles[slot] and list(roles.values()).count(roles[slot]) == 1:
                    sources[slot].role = roles[slot]
                    if aliases.get(slot):
                        sources[slot].idents = (exp.to_identifier(aliases[slot]),)

    def _exposes(self, source: _Source, column: str) -> bool:
        """这个来源可能有这一列：实表看表结构（没给表结构就当可能有），CTE、子查询看它选出来的列。"""
        from sqlglot import exp

        if source.kind == "table":
            return source.key not in self.columns or name_key(column) in self.columns[source.key]
        if source.kind == "derived" and isinstance(source.query, exp.Query):
            return any(called is None or name_key(called) == name_key(column)
                       for called in self.names(source.query))
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

    def _pass(self, source: _Source | None, column: str) -> tuple[_Source, str] | None:
        """派生来源只读一个来源、把它的这一列原样透出来（直接选出这一列，或者 SELECT *）时，里面一层的 (来源, 列名)；
        不是原样透出（算出来的列、读好几个来源）返回 None。先认写了名字的列：SELECT *, amount * 0.9 AS net 里的 net
        是算出来的，不是里面那张表的 net。"""
        from sqlglot import exp

        if source is None or source.kind != "derived" or not isinstance(source.query, exp.Select):
            return None
        inner = self.sources(source.query)
        if len(inner) != 1:
            return None
        only = next(iter(inner.values()))
        names = self.names(source.query)
        named = [item for item, called in zip(source.query.selects, names)
                 if called is not None and name_key(called) == name_key(column)]
        if named:
            target = _unparen(named[0].this if isinstance(named[0], exp.Alias) else named[0])
            return (only, target.name) if isinstance(target, exp.Column) else None
        return (only, column) if None in names else None

    def names(self, query: Any) -> list[str | None]:
        """派生来源选出来的各列叫什么（见 _output_names），算一次记下来。"""
        if id(query) not in self._names_memo:
            self._names_memo[id(query)] = _output_names(query)
        return self._names_memo[id(query)]

    def _blocks(self, select: Any, column: str) -> str | None:
        """见 _blocking，算一次记下来。"""
        memo = (id(select), name_key(column))
        if memo not in self._blocks_memo:
            self._blocks_memo[memo] = self._blocking(select, column)
        return self._blocks_memo[memo]

    @staticmethod
    def _blocking(select: Any, column: str) -> str | None:
        """在这一段外面按 column 筛和在它里面筛结果不同时，说它做了什么（「取前几行」「窗口函数」）；一样返回 None。

        取前几行（LIMIT、OFFSET）：先取前 10 行再筛已付，和先筛已付再取前 10 行，取到的不是同一批。窗口函数（含
        DISTINCT ON）：先算每人的第一单再筛已付（第一单就是已付的人），和先筛已付再算（有已付订单的人）不是一回事；
        只有 column 是每个窗口的分区键时一样（按分区键筛掉的是整个分区，分区里的排名、合计不受影响）。"""
        from sqlglot import exp

        if select.args.get("limit") is not None or select.args.get("offset") is not None:
            return "limit"
        distinct = select.args.get("distinct")
        if isinstance(distinct, exp.Distinct) and distinct.args.get("on") is not None:
            keys = {name_key(_unparen(e).name) for e in distinct.args["on"].expressions
                    if isinstance(_unparen(e), exp.Column)}
            if name_key(column) not in keys:
                return "window"
        for node in _own_nodes(select):
            if isinstance(node, exp.Window):
                keys = {name_key(_unparen(e).name) for e in node.args.get("partition_by") or []
                        if isinstance(_unparen(e), exp.Column)}
                if name_key(column) not in keys:
                    return "window"
        return None

    def through(self, source: _Source | None, column: str, *, pierce: bool = False) -> tuple[_Source | None, str]:
        """CTE、子查询只是把一张表的列原样透出来（SELECT *，或者直接选出这一列）时，换成那张表的列：
        原流程直接查 orders、盲算先在 CTE 里筛一遍再查，算的是同一列。

        这一段取前几行、或者算了窗口函数时不透过去（见 _blocks），照写这个派生来源的列：外面的 t.status = 'paid'
        和写在 t 里面的 status = 'paid' 不是同一个条件，透过去就都成了 orders.status = 'paid'，分不出来。pierce 时
        不管这些，一直透到底：拿来认出两边是不是同一个条件、只是筛的先后不同（见 Part.pierce）。"""
        for _ in range(4):
            found = self._pass(source, column)
            if found is None or (not pierce and self._blocks(source.query, found[1])):
                break
            source, column = found
        return source, column

    def resolve(self, column: Any, sources: dict[str, _Source], *, pierce: bool = False) -> tuple[_Source | None, str]:
        return self.through(self.locate(column, sources)[1], column.name, pierce=pierce)

    def _definition(self, source: _Source | None, column: str) -> list[tuple[Any, _Sources]] | None:
        """派生来源一列在里面的定义：[(定义的表达式, 它那一支的来源)]，UNION 每支一项（按列的位置）。只有 SELECT *
        透出来的列，定义就是里面那一支的同名列。认不出（不是派生来源、没有这一列、某一支在那个位置是 *）返回 None。"""
        from sqlglot import exp

        if source is None or source.kind != "derived" or source.query is None:
            return None
        branches = _branches(source.query)
        if not branches:
            return None
        names = [None if called is None else name_key(called) for called in self.names(source.query)]
        if name_key(column) not in names:
            if None not in names:
                return None
            return [(exp.column(column), self.sources(branch)) for branch in branches]
        position = names.index(name_key(column))
        out = []
        for branch in branches:
            if position >= len(branch.selects) or _is_star(branch.selects[position]):
                return None
            item = branch.selects[position]
            out.append((item.this if isinstance(item, exp.Alias) else item, self.sources(branch)))
        return out

    def shared(self) -> set[str]:
        """好几张表都有的列名（name_key）：这条语句里出现时挂在不止一张表上（orders.order_id、refunds.order_id），
        或者表结构里不止一张用到的表有它。给人看时只有这些列写表名，其余只写列名，读起来和原 SQL 一样短。"""
        from sqlglot import exp

        if self._shared is None:
            owners: dict[str, set[str]] = {}
            for select in self._alive(exp.Select):
                sources = self.sources(select)
                for node in _own_nodes(select):
                    if isinstance(node, exp.Column) and node.name and not node.is_star:
                        source, name = self.resolve(node, sources)
                        if source is not None:
                            owners.setdefault(name_key(name), set()).add(source.ref)
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
        """七种写法。比较用的五种（_COMPARING），名字按 name_key：key、loose 见 Part，key 写实表的表名，loose 不写，
        CTE、子查询都写编号；pierce 同 key，只是一路透过取前几行、算窗口函数的派生来源（见 through），拿来认出同一个
        条件筛的先后不同；anon 写实表的表名、派生来源一律写「@」（排比较的两边谁在前时先分实表的列和派生来源的列）；
        shape 写实表的表名、派生来源写它的结构标识（见 _signature：配对用，和名字、编号都无关）。text、full 给人看
        （见 Part），text 只在好几张表都有这一列时写真实表名，full 一律写（关联条件说的就是两张表各出哪一列，用 full）。

        node 里套着的子查询逐段改写：每一列按它所在那一段的来源认，子查询里的表去掉别名（列已经写成了真实表名）。
        子查询里拿两张表的列互相比的（相关子查询），给人看时和关联条件一样写表名。比较用的写法里各自起的名字也去掉
        （见 _drop_names）。
        """
        from sqlglot import exp

        copy = node.copy()
        if mode in _COMPARING:
            copy = copy.transform(_as_date, copy=False)
        groups: dict[int, tuple[Any, list[Any]]] = {}
        for col in copy.find_all(exp.Column):
            if col.name and not col.is_star:
                owner = self._owner(col)
                groups.setdefault(id(owner), (owner, []))[1].append(col)
        for owner, cols in groups.values():
            scope = sources if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])
            found = [(col, *self.resolve(col, scope, pierce=mode == "pierce")) for col in cols]
            here = mode
            if owner is not None and mode == "text" and len({s.ref for _, s, _ in found if s is not None}) > 1:
                here = "full"
            for col, source, name in found:
                self._write(col, source, name, here)
        for table in copy.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier) and self._owner(table) is not None:
                table.set("alias", None)
                if mode in _COMPARING:
                    for arg in ("catalog", "db", "this"):
                        ident = table.args.get(arg)
                        if isinstance(ident, exp.Identifier):
                            table.set(arg, exp.to_identifier(name_key(ident.name)))
        if mode in _COMPARING:
            self._drop_names(copy, mode)
        return copy.sql(dialect=self.read, comments=False)

    def _drop_names(self, copy: Any, mode: str) -> None:
        """比较用的写法里抹掉套着的子查询里各自起的名字（就地改）：引用 CTE 的表、FROM 里的子查询（含 LATERAL）、
        子查询里自带的 CTE 定义写编号（anon 写「@」，shape 里引用 CTE 的表写它的结构标识、别的写「@」；引用它们的列
        已经照这样写了）；只看取值的子查询（IN、EXISTS、标量子查询）去掉选出来的列名。FROM 里的子查询、CTE 定义选出来
        的列名留着：外层按这个名字引用它们，去掉了就分不出外层用的是哪一列。复制出来的那段本身没有父节点，认不出是
        FROM 里的子查询，它选出来的列名也会去掉（_Derived.body 另记了各列，见 derived）。"""
        from sqlglot import exp

        for node in copy.find_all(exp.Table, exp.Subquery, exp.Lateral, exp.CTE):
            derived = node.meta.get(_DERIVED)
            if not derived:
                continue
            if mode == "shape":
                # 引用 CTE 的表写它是哪个（结构标识）；FROM 里的子查询、CTE 定义本身整段写在这里，不用再标
                mark = exp.to_identifier(self._signature_of(derived) if isinstance(node, exp.Table) else _ANON)
            else:
                mark = exp.to_identifier(_ANON if mode == "anon" else derived)
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
        if mode in _COMPARING:
            if source is None:
                owner = None
            elif source.kind == "derived":
                owner = _ANON if mode == "anon" else source.ref if mode != "shape" else \
                    self._signature_of(source.key) + (f"#{source.role}" if source.role else "")
            else:
                owner = None if mode == "loose" else source.ref
            col.set("this", exp.to_identifier(self.column_key(source, name) if source is not None else name_key(name)))
            col.set("table", exp.to_identifier(owner) if owner else None)
            col.set("db", None)
            col.set("catalog", None)
            return
        col.set("this", exp.Identifier(this=name, quoted=quoted or not _PLAIN_NAME.match(name)))
        # 自己关联自己的（有 role）一律写别名：只写列名读不出是哪一个
        if source is None or (source.kind != "other" and mode != "full" and not source.role
                              and (self.single or name_key(name) not in self.shared())):
            col.set("table", None)
            col.set("db", None)
            col.set("catalog", None)
        else:
            *heads, last = source.idents or (exp.to_identifier(source.name),)
            col.set("table", last.copy())
            col.set("db", heads[-1].copy() if heads else None)
            col.set("catalog", heads[-2].copy() if len(heads) > 1 else None)

    def canonical(self, node: Any, sources: dict[str, _Source], *, by: str = "key") -> Any:
        """比较的两边排好顺序（列在左、字面量在右；两边都有列的按 _rank 排），IN 的取值排好序；加法、乘法的两项也按
        这个规矩排（amount * 0.9 和 0.9 * amount 是同一个算法），只排两项都不是别的加减乘除的（见 _atomic）。返回新的
        树。套着的子查询里的比较按它那一段的来源排：拿外层的来源去认子查询的别名，排出来的先后就取决于别名怎么起。"""
        from sqlglot import exp

        def fix(n: Any) -> Any:
            found = _COMPARE.get(type(n).__name__)
            swap = (type(n).__name__, type(n).__name__) if isinstance(n, (exp.Add, exp.Mul)) and \
                _atomic(n.this) and _atomic(n.expression) else found
            if swap is not None and isinstance(n, exp.Binary):
                left, right = n.this, n.expression
                lc, rc = bool(_own_columns(left)), bool(_own_columns(right))
                here = self.scope(n, sources) if lc and rc else sources
                if (rc and not lc) or (lc and rc and self._rank(left, here, by) > self._rank(right, here, by)):
                    return getattr(exp, swap[1])(this=right, expression=left)
            elif isinstance(n, exp.In) and n.expressions and not n.args.get("query") \
                    and not any(_own_columns(e) for e in n.expressions):
                n.set("expressions", sorted(n.expressions, key=lambda e: e.sql(dialect=self.read)))
            return n

        return node.transform(fix)

    def _rank(self, node: Any, sources: dict[str, _Source], by: str = "key") -> tuple[str, str]:
        """比较的两边谁排前面：先按不写编号的写法（实表的列、派生来源的列分得开），分不出（两个派生来源的同名列）
        再按 by 的写法分。

        diff_facets 重新分析时 by 是 key，按两边配好对的共同编号排（见 relabel）：p.total > r.total 和换了关联先后、
        位置编号对调的同一句排出来是同一个方向。不按结构标识排：配成一对的两个派生来源里面稍有不同（一边多一个条件）
        时结构标识就不同，按它排，同一个比较在两边可能排成两个方向，报出两条自相矛盾的话。配对之前（算结构标识时）
        还没有共同的编号，by 是 shape，按结构标识排：位置编号决定不了先后。"""
        return self.render(node, sources, "anon"), self.render(node, sources, by)

    def part(self, node: Any, sources: dict[str, _Source], *, excludes: bool = False) -> Part:
        #: 用到的各列 {(哪一个来源, 列): ((给人看的表名, 列), 比较用的写法)}：按「哪一个来源」分，自己关联自己的两个别名
        #: 是两列（o1.amount、o2.amount）
        refs: dict[tuple[str, str], tuple[ColumnRef, str]] = {}
        origins: list[tuple[str, str]] = []
        stage = ""
        for col in _own_columns(node):
            located = self.locate(col, sources)[1]
            source, name = self.through(located, col.name)
            refs.setdefault((source.ref if source is not None else "", name_key(name)),
                            ((source.name if source is not None else None, name),
                             f"{source.ref}.{self.column_key(source, name)}" if source is not None
                             else f".{name_key(name)}"))
            if source is None or source.kind != "derived":
                continue
            # 透视在这个派生来源上停住了，一路透到底却还能往里走：是它取前几行、算窗口函数挡住的
            if not stage and self.through(located, col.name, pierce=True)[0] is not source:
                stage = self._stage(source, name)
            if (origin := self._origin(source, name)) is not None:
                if (pair := (self.render(col, sources, "full"), origin)) not in origins:
                    origins.append(pair)
        full = self.render(node, sources, "full")
        key = self.render(node, sources, "key")
        pierce = self.render(node, sources, "pierce") if stage else ""
        if pierce == key:
            pierce = stage = ""
        columns = tuple(ref for ref, _ in refs.values())
        # 拿两张表的列互相比的（相关子查询里的 o.id = r.order_id）和关联条件一样，给人看也写表名
        across = len({table for table, _ in columns}) > 1
        return Part(key=key, text=full if across else self.render(node, sources),
                    columns=columns, excludes=self._excludes(node, sources) if excludes else None,
                    loose=self.render(node, sources, "loose"), full=full,
                    column_keys=tuple(written for _, written in refs.values()),
                    origins=tuple(origins), pierce=pierce, stage=stage)

    def _stage(self, source: _Source, column: str) -> str:
        """挡住了透视的派生来源做了什么（给人看，见 _blocks）：「取 t 的前 10 行」「算 t 的窗口函数」。"""
        from sqlglot import exp

        query = source.query
        if self._blocks(query, column) == "limit":
            limit = query.args.get("limit")
            count = limit.expression if isinstance(limit, exp.Limit) else limit.args.get("count") \
                if limit is not None else None
            written = count.sql(dialect=self.read, comments=False) if count is not None else ""
            return _say("取", source.name, "的前", written, "行") if written.isdigit() else \
                _say("按", "LIMIT / OFFSET", "取", source.name, "的行")
        return _say("算", source.name, "的窗口函数")

    def _origin(self, source: _Source, column: str, depth: int = 0) -> str | None:
        """派生来源一列的来历（给人看）：它在里面怎么算的，里面再用到的派生来源的列也换成各自的来历，一直换到实表的
        列，一律写表名（SUM(orders.amount)）。两边同名的派生来源其实不是同一个时（盲算的 t1 是原流程的 t2），光写
        「t1.total」两边一样，带上来历才说得清各自是付款合计还是退款合计。UNION 只写第一支；认不出定义、套得太深
        返回 None。"""
        if source.kind != "derived" or source.query is None or depth > 4:
            return None
        memo = (id(source.query), name_key(column))
        if memo not in self._origins:
            self._origins[memo] = None          # 递归 CTE 绕回自己时到这里为止
            found = self._definition(source, column)
            self._origins[memo] = self._expand(*found[0], depth + 1) if found else None
        return self._origins[memo]

    def _expand(self, node: Any, sources: dict[str, _Source], depth: int) -> str:
        """把 node 写成给人看的 full 写法，里面用到的派生来源的列换成各自的来历（见 _origin）。换进去的是一段
        写好的文字：先用占位符占住位置，整段写好之后再换回来；放在运算符里的加括号，免得优先级变了。"""
        from sqlglot import exp

        bare = _unparen(node)
        if isinstance(bare, exp.Column):
            source, name = self.resolve(bare, sources)
            inner = self._origin(source, name, depth) if source is not None else None
            return inner if inner is not None else self.render(bare, sources, "full")
        copy = node.copy()
        subs: dict[str, str] = {}
        for col in _own_columns(copy):
            source, name = self.resolve(col, sources)
            inner = self._origin(source, name, depth) if source is not None else None
            if inner is None:
                continue
            mark = exp.Placeholder(this=f"sqlsem{len(subs)}")
            wrap = " " in inner and isinstance(col.parent, (exp.Binary, exp.Unary)) and \
                not isinstance(col.parent, exp.Paren)
            subs[mark.sql(dialect=self.read)] = f"({inner})" if wrap else inner
            col.replace(mark)
        text = self.render(copy, sources, "full")
        for mark, inner in subs.items():
            text = text.replace(mark, inner)
        return text

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

    def facets(self, extra: Iterable[str] = ()) -> Facets:
        """各个方面。extra 是没用到、也照样收进来的 CTE（name_key，见 _aligned），别的没用到的 CTE 不收（见 _enliven）。"""
        from sqlglot import exp

        self._enliven(extra)
        joins: list[JoinFacet] = []
        where: list[Part] = []
        time: list[TimePredicate] = []
        group: list[Part] = []
        aggregates: list[Aggregate] = []
        having: list[Part] = []
        used: list[Part] = []
        for select in self._alive(exp.Select):
            sources = self.sources(select)
            if select is self.root or not self._in_condition(select):
                used += [Part.plain(s.ref, s.name) for s in sources.values()]
            drafts = self._join_drafts(select, sources)
            clause = select.args.get("where")
            for cond in _conjuncts(clause.this) if clause is not None else []:
                canon = self.canonical(cond, sources)
                if self._joins_on_where(canon, sources, drafts):
                    continue
                part = self.part(canon, sources, excludes=True)
                where.append(part)
                self._rejects(canon, part, sources, drafts)
                if found := self._time(canon, sources):
                    time.append(found)
            joins += [self._join(d, sources) for d in drafts]
            group += self._group(select, sources)
            aggregates += self._aggregates(select, sources)
            having += self._having(select, sources)
        derived = [Part.plain(self._labels[number], name or "子查询")
                   for number, name, body, _ in self._derived if id(body) not in self._dead]
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
            expressions=_dedupe(self._expressions(), lambda c: (c.scope_key, c.name, c.expr.key)),
            derived=_dedupe(derived, lambda p: p.key),
        )

    def _rejects(self, cond: Any, part: Part, sources: dict[str, _Source], drafts: list[dict[str, Any]]) -> None:
        """同一段 WHERE 里的条件把外连接可空那一侧没关联上的行筛掉了的，记在那次连接上（见 JoinFacet.rejects）：LEFT
        JOIN 关联进来的那一侧、RIGHT JOIN 之前的各个来源可空，那一侧的列直接拿来比较、IN、LIKE、BETWEEN，或者写 IS NOT
        NULL，空值都不成立。IS NULL、OR、套在 COALESCE / IFNULL / CASE 里的不认（宁可漏报）。"""
        from sqlglot import exp

        n = _unparen(cond)
        if isinstance(n, exp.Not):
            n = _unparen(n.this)
            if isinstance(n, exp.Is):
                if not isinstance(_unparen(n.expression), exp.Null):
                    return
                n = n.this                  # IS NOT NULL
            elif type(n).__name__ not in _NULL_REJECTING:
                return
        elif type(n).__name__ not in _NULL_REJECTING:
            return
        hit = set()
        for col in _own_columns(n):
            if col is not n and any(isinstance(p, (exp.Coalesce, exp.Case, exp.If)) for p in _parents(col, stop=n)):
                continue
            if (slot := self.locate(col, sources)[0]) is not None:
                hit.add(slot)
        for draft in drafts:
            nullable = {draft["slot"]} if draft["kind"] == "left" else \
                set(draft["before_slots"]) if draft["kind"] == "right" else set()
            if draft.get("rejects") is None and hit & nullable:
                draft["rejects"] = part

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
            before = [(key, s) for key, s in list(sources.items())[:i + 1] if key != slot]
            drafts.append({"kind": _join_kind(join), "slot": slot, "source": sources.get(slot), "conds": conds,
                           "before": [s.name for _, s in before], "before_keys": [s.ref for _, s in before],
                           "before_slots": [key for key, _ in before]})
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
            Part(key=source.ref, text=source.name, loose=source.ref, full=source.name)
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
        sides = {source.ref} if source is not None else set()
        if draft["conds"]:
            sides |= {found.ref for c in draft["conds"] for col in _own_columns(c)
                      for slot, found in [self.locate(col, sources)] if slot is not None and found is not None}
        else:
            sides |= set(draft["before_keys"])
        # 外连接的主表、可空那一侧：LEFT JOIN 关联进来的那一侧可空，RIGHT JOIN 反过来；另一侧按关联条件里出现的来源
        # 算（没有就是它之前的各个来源），和内连接的 sides 一样不受前面别的连接先后的影响
        keep: set[str] = set()
        nullable: set[str] = set()
        if draft["kind"] in ("left", "right") and source is not None:
            near, far = {source.ref}, (sides - {source.ref}) or set(draft["before_keys"])
            keep, nullable = (far, near) if draft["kind"] == "left" else (near, far)
        return JoinFacet(kind=draft["kind"], table=table, on=on, other=tuple(sorted(other)),
                         sides=tuple(sorted(sides)), keep=tuple(sorted(keep)), nullable=tuple(sorted(nullable)),
                         rejects=draft.get("rejects"))

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
        base, scope, via, datey = self._time_column(cols[0], sources)
        if not (_temporal_name(base.name) or _temporal_name(cols[0].name) or datey or _datey(subject)
                or any(_datey(v) for v in values)):
            return None
        shown = [self.render(v, sources) for v in values]
        value = f"{shown[0]} AND {shown[1]}" if op == "between" else f"({', '.join(shown)})" if op == "in" \
            else shown[0]
        return TimePredicate(column=self.part(base, scope), op=op, value=value,
                             condition=self.part(cond, sources, excludes=True), via=via)

    def _time_column(self, column: Any, sources: dict[str, _Source]) -> tuple[Any, _Sources, tuple[str, ...], bool]:
        """时间条件里的那一列是 CTE、子查询算出来的、在里面是同一列套着日期函数、CAST、MIN / MAX 时（见 _value_column），
        换成里面那一列，一直换到实表：(那一列, 它那一段的来源, 路过的派生来源的列「编号.列」, 路上有没有日期函数)。
        原样透出的列 resolve 已经透过去了；这里补上算了一下的。"""
        via: list[str] = []
        datey = False
        for _ in range(4):
            source, name = self.resolve(column, sources)
            found = self._definition(source, name) if source is not None else None
            inner = _value_column(found[0][0]) if found and len(found) == 1 else None
            if inner is None:
                break
            via.append(f"{source.key}.{self.column_key(source, name)}")
            datey = datey or _datey(found[0][0])
            column, sources = inner, found[0][1]
        return column, sources, tuple(via), datey

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

        return [self._aggregate(node, sources) for node in _own_nodes(select)
                if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window)]

    def _aggregate(self, node: Any, sources: dict[str, _Source]) -> Aggregate:
        from sqlglot import exp

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
        return Aggregate(func=func, arg=part, distinct=distinct, text=self.render(node, sources))

    # ---- 算出来的列

    def _expressions(self) -> list[Computed]:
        """算出来的列（见 Computed）：最外层（UNION 的各支）选出来、不是光一列也不是字面量的；用到了的派生来源里
        被外面用到、又不是原样透出一列的列（原样透出的，外面的写法 resolve 已经透成了里面那一列，不用再比定义）。"""
        from sqlglot import exp

        out: list[Computed] = []
        for select in _branches(self.root):
            sources = self.sources(select)
            for item in select.selects:
                target = _unwrap_value(item.this if isinstance(item, exp.Alias) else item)
                if not self._plain_output(target):
                    out.append(self._computed([(target, sources)], None, None, "", ""))
        used = self._used()
        for number, name, body, _ in self._derived:
            label = self._labels[number]
            if id(body) in self._dead or label not in used:
                continue
            source = _Source("derived", name or "子查询", label, (), body)
            columns = [called for called in self.names(body) if called]
            columns += sorted(c for c in used[label] if c not in {name_key(x) for x in columns})
            for column in dict.fromkeys(columns):
                if name_key(column) not in used[label] or self.through(source, column)[0] is not source:
                    continue
                if found := self._definition(source, column):
                    out.append(self._computed(found, source.name, label, self.column_key(source, column), column))
        return out

    @staticmethod
    def _plain_output(target: Any) -> bool:
        """最外层选出来的一项不用按算法比：光一列（分组那一面比）、*、字面量。光一个聚合照收：另一边可能是拿同一个
        聚合再算了一下（SUM(amount) 对 SUM(amount) / 100），得配成一对说；两边只差聚合本身的，聚合那一面已经报了。"""
        from sqlglot import exp

        return isinstance(_unparen(target), (exp.Column, exp.Star, exp.Literal, exp.Null, exp.Boolean))

    def _computed(self, branches: list[tuple[Any, _Sources]], scope: str | None, scope_key: str | None, name: str,
                  shown: str) -> Computed:
        """一列的定义（UNION 每支一个）→ Computed：定义本身、聚合换成占位符之后的写法、里面的聚合。"""
        from sqlglot import exp

        exprs: list[Part] = []
        templates: list[Part] = []
        aggregates: list[Aggregate] = []
        for node, sources in branches:
            canon = self.canonical(node, sources)
            exprs.append(self.part(canon, sources))
            # 这一段自己的聚合（窗口函数、套着的子查询里的不算），按出现的先后
            own = [n for n in canon.walk(prune=lambda x: isinstance(x, (exp.Select, exp.Subquery)))
                   if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window)
                   and not any(isinstance(p, exp.AggFunc) for p in _parents(n, stop=None))]
            aggregates += [self._aggregate(n, sources) for n in own]
            if own and own[0] is canon:
                templates.append(Part.plain(PLACEHOLDER, PLACEHOLDER))     # 光一个聚合
                continue
            for n in own:
                n.replace(exp.Placeholder())
            templates.append(Part(key=self.render(canon, sources, "key"), text="",
                                  loose=self.render(canon, sources, "loose")))

        def joined(parts: list[Part]) -> Part:
            if len(parts) == 1:
                return parts[0]
            return Part(key=" | ".join(p.key for p in parts), text=" | ".join(p.text for p in parts),
                        loose=" | ".join(p.loose for p in parts), full=" | ".join(p.full for p in parts),
                        columns=tuple(c for p in parts for c in p.columns),
                        column_keys=tuple(k for p in parts for k in p.column_keys),
                        origins=tuple(o for p in parts for o in p.origins))

        return Computed(scope=scope, scope_key=scope_key, name=name, shown=shown, expr=joined(exprs),
                        template=joined(templates), aggregates=tuple(aggregates))

    def _used(self) -> dict[str, set[str]]:
        """用到了的段里引用了派生来源的哪些列：{派生来源的编号: {列名的 name_key}}。原样透出的列连同里面那一层也算
        用到了；最外层的 SELECT *（t.*）用到了那个来源的每一列；USING 的列两边都算。"""
        from sqlglot import exp

        used: dict[str, set[str]] = {}

        def mark(source: _Source | None, column: str) -> None:
            for _ in range(8):
                if source is None or source.kind != "derived":
                    return
                used.setdefault(source.key, set()).add(name_key(column))
                if (found := self._pass(source, column)) is None:
                    return
                source, column = found

        def mark_all(source: _Source | None, depth: int = 0) -> None:
            if source is None or source.kind != "derived" or depth > 4:
                return
            for branch in _branches(source.query)[:1]:
                inner = self.sources(branch)
                for item, called in zip(branch.selects, self.names(source.query)):
                    if called is None:
                        for found in self._starred(item, inner):
                            mark_all(found, depth + 1)
                    else:
                        mark(source, called)

        outermost = {id(s) for s in _branches(self.root)}
        for select in self._alive(exp.Select):
            sources = self.sources(select)
            for node in _own_nodes(select):
                if isinstance(node, exp.Column) and node.name and not node.is_star:
                    mark(self.locate(node, sources)[1], node.name)
            if id(select) in outermost:
                for item in select.selects:
                    if _is_star(item):
                        for found in self._starred(item, sources):
                            mark_all(found)
            for join in select.args.get("joins") or []:
                for ident in join.args.get("using") or []:
                    for found in sources.values():
                        if self._exposes(found, ident.name):
                            mark(found, ident.name)
        return used

    @staticmethod
    def _starred(item: Any, sources: dict[str, _Source]) -> list[_Source]:
        """SELECT 列表里的 *、t.* 选的是哪几个来源。"""
        from sqlglot import exp

        if isinstance(item, exp.Column) and item.table:
            found = sources.get(name_key(item.table))
            return [found] if found is not None else []
        return list(sources.values())

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
        for query in self._alive(exp.Select, exp.SetOperation):
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
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同，或者同一个时间条件筛的先后不同）/ filter /
    aggregate / expression（算出来的列：最外层的算法、CTE 和子查询里被用到的列的定义，见 Computed）/ join（类型、
    外连接的主表、关联条件、只有一边关联了）/ tables / group_by / having / limit（取哪几行：行数、跳过的行数、取前
    几行时的排序）/ parse。a、b 是两边各自的写法（给人看，没有就是 None），text 是一句中文说明。
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

    几个方面之间互相交代，同一处不同只报一遍：聚合那一面报过的聚合（reported）、时间那一面报过的派生来源的列
    （explained），算出来的列那一面不再重复报。
    """

    def __init__(self, a: Facets, b: Facets, labels: tuple[str, str], names: Mapping[str, str] | None) -> None:
        self.a, self.b = a, b
        self.la, self.lb = labels
        self.label = _labeler(names)
        joined = {j.table.key for j in (*a.joins, *b.joins)}
        self.ta = [t for t in a.tables if t.key not in {u.key for u in b.tables} and t.key not in joined]
        self.tb = [t for t in b.tables if t.key not in {u.key for u in a.tables} and t.key not in joined]
        self.swapped = len(self.ta) == 1 and len(self.tb) == 1
        #: 两边的派生来源 {编号: 名字}（同名却不是同一个时拿来说清对应关系）、各段来源 {key: 名字}（外连接的主表）
        self.names = ({p.key: p.text for p in a.derived}, {p.key: p.text for p in b.derived})
        self.called = ({p.key: p.text for p in a.sources}, {p.key: p.text for p in b.sources})
        #: 聚合那一面报过的两边独有的聚合、时间那一面已经说了的派生来源的列（「编号.列」）
        self.reported: tuple[set[Any], set[Any]] = (set(), set())
        self.explained: set[str] = set()

    def k(self, part: Part | None) -> str | None:
        if part is None:
            return None
        return part.loose if self.swapped else part.key

    def side(self, key: str) -> str:
        """比较用的来源 key；换了一张表时，换的那两张记成同一个。"""
        return "?" if self.swapped and key in (self.ta[0].key, self.tb[0].key) else key

    def sides(self, j: JoinFacet) -> tuple[str, ...]:
        """连接涉及的来源（见 JoinFacet.sides）。"""
        return self.keys(j.sides)

    def keys(self, keys: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted(self.side(s) for s in keys))

    def agg_key(self, g: Aggregate) -> tuple[str, str | None, bool]:
        return g.func, self.k(g.arg), g.distinct

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
        换了用哪一个的列也报不出来。配不上的派生来源（一边多出来的 CTE）编号和另一边的都不一样，按它写的项照样
        各算各的。"""
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
    def staged(only_a: list[Part], only_b: list[Part]) -> list[tuple[Part, Part]]:
        """两边各自独有、一路透到底却是同一个条件的配成一对，就地从两边拿掉（见 Part.pierce）：只有一边被取前几行、
        算窗口函数的派生来源挡住，是同一个条件筛的先后不同，不是两个条件。"""
        pairs = []
        for p in list(only_a):
            q = next((q for q in only_b
                      if bool(p.stage) != bool(q.stage) and (p.pierce or p.key) == (q.pierce or q.key)), None)
            if q is not None:
                pairs.append((p, q))
                only_a.remove(p)
                only_b.remove(q)
        return pairs

    def stage_text(self, p: Part, q: Part) -> str:
        """同一个条件筛的先后不同：哪一边先取前几行 / 算窗口函数、再筛，哪一边先筛。条件照没被挡住的那一边写：它
        写的是里面那一列，读起来和原 SQL 一样。"""
        cond, stage = (q if p.stage else p).text, p.stage or q.stage
        if p.stage:
            return _say(self.la, "先", stage, "、再按", cond, "筛选，", self.lb, "先按", cond, "筛选、再", stage)
        return _say(self.la, "先按", cond, "筛选、再", stage, "，", self.lb, "先", stage, "、再按", cond, "筛选")

    def shown(self, xs: list[Part], ys: list[Part], *, text: Any = None, full: Any = None) -> tuple[str, str]:
        """两边给人看的写法。读起来一样却不是同一个东西时，依次改成：一律写表名的写法（各自只写列名时两张表的
        amount 读起来一样）；带上派生来源各列的来历（两边同名的 t1 一个算付款合计、一个算退款合计）；两边同名的派生
        来源各对应另一边的哪一个（两个 CTE 只差里面的条件，来历也一样）。"""
        text = text or (lambda ps: "、".join(p.text for p in ps))
        full = full or (lambda ps: "、".join(p.full for p in ps))
        wa, wb = text(xs), text(ys)
        if wa != wb:
            return wa, wb
        wa, wb = full(xs), full(ys)
        return (wa, wb) if wa != wb else self.annotated(wa, wb, xs, ys)

    def annotated(self, wa: str, wb: str, xs: list[Part], ys: list[Part]) -> tuple[str, str]:
        """两边一律写表名的写法 wa、wb 也一样时（见 shown）：带上派生来源各列的来历，来历也一样就说清对应关系。"""
        ga, gb = self.gloss(xs), self.gloss(ys)
        if ga != gb:
            return _say(wa, ga), _say(wb, gb)
        return wa, _say(wb, self.correspondence(xs, ys))

    @staticmethod
    def gloss(parts: list[Part]) -> str:
        """用到的派生来源的列各自的来历（见 Part.origins）：「（即 SUM(orders.amount)）」，好几列时「（p.total 即 …、
        r.total 即 …）」。没有返回空串。"""
        notes: list[str] = []
        for p in parts:
            for column, origin in p.origins:
                note = _say("即", origin) if len(p.origins) == 1 and p.full == column else _say(column, "即", origin)
                if note not in notes:
                    notes.append(note)
        return f"（{'、'.join(notes)}）" if notes else ""

    def correspondence(self, xs: list[Part], ys: list[Part]) -> str:
        """用到的派生来源两边同名、却各自对应另一边别的派生来源时，说清对应关系：「（盲算的 paid 对应原流程的 open）」；
        一边的在另一边配不上的也说。没有返回空串。"""
        keys = dict.fromkeys(ck.split(".", 1)[0].split("#", 1)[0] for p in (*ys, *xs) for ck in p.column_keys
                             if ck.startswith("@"))
        na, nb = self.names
        notes = []
        for key in keys:
            a, b = na.get(key), nb.get(key)
            if a and b and a != b:
                notes.append(_say(self.lb, "的", b, "对应", self.la, "的", a))
            elif b and not a:
                notes.append(_say(self.lb, "的", b, "在", self.la, "里没有对应的"))
            elif a and not b:
                notes.append(_say(self.la, "的", a, "在", self.lb, "里没有对应的"))
        return f"（{'、'.join(notes)}）" if notes else ""

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
            wa, wb = self.shown(ca, cb, text=self.named, full=lambda ps: self.named(ps, full=True))
            if ca and cb:
                text = _say(la, "按", wa, "筛时间，", lb, "按", wb)
            elif ca:
                text = _say(la, "按", wa, "筛时间，", lb, "没有按时间筛选")
            else:
                text = _say(la, "没有按时间筛选，", lb, "按", wb, "筛时间")
            detail = {side: [{"table": p.columns[0][0], "column": p.columns[0][1]} for p in parts if p.columns]
                      for side, parts in (("a", ca), ("b", cb))}
            # 时间列是从 CTE、子查询算出来的列透过去的（见 TimePredicate.column）：那一列的定义不同，这里已经说了
            self.explained |= {v for t in (*a.time, *b.time) for v in t.via}
            return [FacetDiff("time", "、".join(p.text for p in ca) or None, "、".join(p.text for p in cb) or None,
                              text, detail)]
        only_a, only_b = self.unmatched(a.time, b.time, lambda t: self.k(t.condition))
        conds_a, conds_b = [t.condition for t in only_a], [t.condition for t in only_b]
        pairs = self.staged(conds_a, conds_b)
        out = [FacetDiff("time_range", p.text, q.text, self.stage_text(p, q)) for p, q in pairs]
        if conds_a or conds_b:
            staged = {id(p) for pair in pairs for p in pair}
            ra = [t.condition for t in a.time if id(t.condition) not in staged]
            rb = [t.condition for t in b.time if id(t.condition) not in staged]
            wa, wb = self.shown(ra, rb, text=lambda ps: " AND ".join(p.text for p in ps),
                                full=lambda ps: " AND ".join(p.full for p in ps))
            out.append(FacetDiff("time_range", wa, wb, _say(la, "的时间范围是", wa, "，", lb, "是", wb)))
        return out

    def filters(self) -> list[FacetDiff]:
        timed = {self.k(t.condition) for t in (*self.a.time, *self.b.time)}
        fa = [p for p in self.a.where if self.k(p) not in timed]
        fb = [p for p in self.b.where if self.k(p) not in timed]
        only_a, only_b = self.unmatched(fa, fb, self.k)
        # 先配同一个条件筛的先后不同的，再配同一列的不同写法（status = 'paid' 和 status IN (…)），再配读起来一样、
        # 挂在不同来源上的同一个条件
        out = [FacetDiff("filter", p.text, q.text, self.stage_text(p, q)) for p, q in self.staged(only_a, only_b)]
        for p in list(only_a):
            column = _single_column(p, loose=self.swapped)
            mine = [q for q in only_a if column and _single_column(q, loose=self.swapped) == column]
            theirs = [q for q in only_b if column and _single_column(q, loose=self.swapped) == column]
            if len(mine) == 1 and len(theirs) == 1:
                q = theirs[0]
                wa, wb = self.shown([p], [q])
                out.append(FacetDiff("filter", p.text, q.text, _say(self.la, "按", wa, "筛选，", self.lb, "按", wb, "筛选")))
                only_a.remove(p)
                only_b.remove(q)
        for p, q in self.lookalikes(only_a, only_b):
            wa, wb = self.shown([p], [q])
            out.append(FacetDiff("filter", wa, wb, _say(self.la, "按", wa, "筛选，", self.lb, "按", wb, "筛选")))
        out += [FacetDiff("filter", p.text, None, self.one_sided(self.la, self.lb, p)) for p in only_a]
        out += [FacetDiff("filter", None, p.text, self.one_sided(self.lb, self.la, p)) for p in only_b]
        return out

    def aggregates(self) -> list[FacetDiff]:
        la, lb = self.la, self.lb

        def pair(xs: list[Aggregate], ys: list[Aggregate], same: Any) -> list[tuple[Aggregate, Aggregate]]:
            pairs = []
            for x in list(xs):
                y = next((y for y in ys if same(x, y)), None)
                if y is not None:
                    pairs.append((x, y))
                    xs.remove(x)
                    ys.remove(y)
            return pairs

        ga, gb = self.unmatched(self.a.aggregates, self.b.aggregates, self.agg_key)
        self.reported = ({self.agg_key(g) for g in ga}, {self.agg_key(g) for g in gb})
        # 先配参数相同的（同一列：函数或去重不同），再配函数相同的（同一种算法：列不同）；各剩一个的也配成一对
        pairs = pair(ga, gb, lambda x, y: self.k(x.arg) == self.k(y.arg))
        pairs += pair(ga, gb, lambda x, y: x.func == y.func)
        if len(ga) == 1 and len(gb) == 1:
            pairs.append((ga.pop(), gb.pop()))
        out = []
        for x, y in pairs:
            same_arg = self.k(x.arg) == self.k(y.arg)
            wa, wb = self.shown([x.arg], [y.arg]) if not same_arg else (x.arg.text, y.arg.text)
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

    def expressions(self) -> list[FacetDiff]:
        """算出来的列（见 Computed）。最外层的按算法比，两边各自独有的按先后配成一对说「算的是什么」；派生来源里被
        用到的列按两边配好对的编号和列名配，定义不同就报。只差在聚合上、聚合那一面已经报了的，按透过去的那一列
        筛时间、时间那一面已经报了的，不再报一遍。"""
        la, lb = self.la, self.lb
        out: list[FacetDiff] = []

        def aggs(c: Computed) -> list[str]:
            return sorted(repr(self.agg_key(g)) for g in c.aggregates)

        def pair(xs: list[Computed], ys: list[Computed], same: Any) -> list[tuple[Computed, Computed]]:
            pairs = []
            for x in list(xs):
                y = next((y for y in ys if same(x, y)), None)
                if y is not None:
                    pairs.append((x, y))
                    xs.remove(x)
                    ys.remove(y)
            return pairs

        xa = [c for c in self.a.expressions if c.scope_key is None]
        xb = [c for c in self.b.expressions if c.scope_key is None]
        only_a, only_b = self.unmatched(xa, xb, lambda c: self.k(c.expr))
        # 先配用到的聚合一样的（同样的合计，算法不同：SUM(amount) 对 SUM(amount) / 100），再配算法一样的（聚合不同），
        # 各剩一个的也配成一对
        pairs = pair(only_a, only_b, lambda x, y: aggs(x) == aggs(y))
        pairs += pair(only_a, only_b, lambda x, y: self.k(x.template) == self.k(y.template))
        if len(only_a) == 1 and len(only_b) == 1:
            pairs.append((only_a.pop(), only_b.pop()))
        for x, y in pairs:
            if not self.explained_by_aggregates(x, y):
                wa, wb = self.shown([x.expr], [y.expr])
                out.append(FacetDiff("expression", x.expr.text, y.expr.text,
                                     _say(la, "算的是", wa, "，", lb, "算的是", wb)))
        # 一边独有的光一个聚合不说：聚合那一面按整条语句比过了（另一边可能在子查询里算了同一个合计、外面只选那一列）
        out += [FacetDiff("expression", c.expr.text, None, _say(la, "多算了", c.expr.text, "，", lb, "没有"))
                for c in only_a if c.template.key != PLACEHOLDER]
        out += [FacetDiff("expression", None, c.expr.text, _say(lb, "多算了", c.expr.text, "，", la, "没有"))
                for c in only_b if c.template.key != PLACEHOLDER]
        ib = {(c.scope_key, c.name): c for c in self.b.expressions if c.scope_key is not None}
        for x in self.a.expressions:
            y = ib.get((x.scope_key, x.name))
            if x.scope_key is None or y is None or self.k(x.expr) == self.k(y.expr) or \
                    f"{x.scope_key}.{x.name}" in self.explained:
                continue
            # 两边起名不同、按位置配成同一列的（见 _renamed）照报：聚合那一面只说了合计不同，没说外面用的是这一列
            if name_key(y.shown) == y.name and self.explained_by_aggregates(x, y):
                continue
            wa, wb = self.shown([x.expr], [y.expr])
            ca, cb = f"{x.scope}.{x.shown}", f"{y.scope}.{y.shown}"
            out.append(FacetDiff("expression", wa, wb,
                                 _say(la, "的", ca, "是", wa, "，", lb, *(("是",) if ca == cb else ("的", cb, "是")), wb)))
        return out

    def explained_by_aggregates(self, x: Computed, y: Computed) -> bool:
        """两边的算法只差在里面的聚合上（聚合换成占位符之后一样），差的那几个聚合聚合那一面又都报了：不再报一遍。
        聚合的集合一样、只是换了位置（SUM(a) - SUM(b) 对 SUM(b) - SUM(a)），聚合那一面什么也没报，照报。"""
        if self.k(x.template) != self.k(y.template) or len(x.aggregates) != len(y.aggregates):
            return False
        ka, kb = self.reported
        differing = [(g, h) for g, h in zip(x.aggregates, y.aggregates) if self.agg_key(g) != self.agg_key(h)]
        return bool(differing) and all(self.agg_key(g) in ka or self.agg_key(h) in kb for g, h in differing)

    def joins(self) -> list[FacetDiff]:
        """连接：先把完全一样的两边配掉，剩下的按关联的是哪张表（派生来源按配好的编号）、再按涉及的来源配成一对，
        说类型、主表或条件哪里不同，再剩下的是只有一边关联了（多出来的是哪个来源见 added）。

        完全一样：内连接、CROSS JOIN 比类型、涉及的来源和条件（FROM paid JOIN refunded 和 FROM refunded JOIN paid 是
        同一次连接）；LEFT / RIGHT JOIN 比主表、涉及的来源和条件，不比写的是 LEFT 还是 RIGHT（orders LEFT JOIN refunds
        和 refunds RIGHT JOIN orders 是同一次连接，和 refunds LEFT JOIN orders 不是）；FULL JOIN 比涉及的来源和条件；
        其余（SEMI、ANTI、NATURAL）比类型、关联进来的表和条件。外连接被同一段 WHERE 里的条件筛掉了没关联上的行
        （JoinFacet.rejects）按内连接比：写的是 LEFT JOIN、实际是内连接，和另一边真的 LEFT JOIN 要报出来，和另一边写的
        INNER JOIN 是一回事。先配完全一样的，同一张表关联了两次、先后不同也不算不同。配成一对的两边名字不同（两边给
        CTE 起的名字不同、内连接换了先后）时两个名字都说。派生来源已经按结构配好了编号：一边多出来的 CTE 编号和
        另一边的都不一样，不会被配给另一边不相干的 CTE、说成「关联条件不同」。

        做不到的：内连接里只涉及一张表的条件写在 ON 里还是 WHERE 里意思一样，仍算两处不同；三张以上不带关联条件的
        表（逗号连接、CROSS JOIN）换了先后，涉及的来源按「它之前的各个来源」算，仍可能算不同；外连接链（a LEFT JOIN b
        LEFT JOIN c）的主表按关联条件里出现的来源算，不按整个左边算；WHERE 筛掉了 c 没关联上的行时，c 是按 b 的列关联的，
        b 没关联上的行连带也没了，前面那次 LEFT JOIN 其实也成了内连接，这里只认后面那次；FULL JOIN 被 WHERE 筛掉一边的
        不认。"""
        la, lb = self.la, self.lb

        def effective(j: JoinFacet) -> str:
            return "inner" if j.rejects is not None and j.kind in ("left", "right") else j.kind

        def same(j: JoinFacet) -> tuple[Any, ...]:
            kind = effective(j)
            if kind in ("inner", "cross"):
                return kind, self.sides(j), self.k(j.on)
            if kind in ("left", "right"):
                return "outer", self.keys(j.keep), self.sides(j), self.k(j.on)
            if kind == "full":
                return kind, self.sides(j), self.k(j.on)
            return kind, j.table.key, self.k(j.on)

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
            ex, ey = effective(x), effective(y)
            outer_x, outer_y = ex in ("left", "right"), ey in ("left", "right")
            if outer_x and outer_y:
                if self.keys(x.keep) != self.keys(y.keep):
                    wa, wb = self.outer(x, 0), self.outer(y, 1)
                    out.append(FacetDiff("join", wa, wb, _say(la, wa, "，", lb, wb)))
            elif (outer_x and y.rejects is not None) or (outer_y and x.rejects is not None):
                wa = self.outer(x, 0) if outer_x else self.rejected(x, 0)
                wb = self.outer(y, 1) if outer_y else self.rejected(y, 1)
                out.append(FacetDiff("join", self.outer(x, 0) if outer_x else _JOIN_WORDS[x.kind] + "（实际是 INNER JOIN）",
                                     self.outer(y, 1) if outer_y else _JOIN_WORDS[y.kind] + "（实际是 INNER JOIN）",
                                     _say(la, wa, "，", lb, wb)))
            elif ex != ey:
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

    def called_as(self, keys: Iterable[str], side: int) -> str:
        """来源 key → 给人看的名字（那一边的叫法），几个用顿号隔开。"""
        return "、".join(self.called[side].get(k, k) for k in keys)

    def outer(self, j: JoinFacet, side: int) -> str:
        """一次 LEFT / RIGHT JOIN 保留哪一侧：「以 orders 为主表外连接 refunds」。不说 LEFT 还是 RIGHT：orders RIGHT
        JOIN refunds 保留的是 refunds，照写关键字反倒容易读反。"""
        return _say("以", self.called_as(j.keep, side), "为主表外连接", self.called_as(j.nullable, side))

    def rejected(self, j: JoinFacet, side: int) -> str:
        """写的是外连接、WHERE 里的条件又把没关联上的行筛掉了（见 JoinFacet.rejects）。"""
        word = _JOIN_WORDS.get(j.kind, j.kind)
        cond = j.rejects.full if j.rejects is not None else ""
        return _say("的 WHERE 里有", cond, "，把关联不上", self.called_as(j.nullable, side), "的行筛掉了，", word,
                    "实际成了 INNER JOIN")

    def group(self) -> list[FacetDiff]:
        a, b = self.a.group_by, self.b.group_by
        if not any(self.unmatched(a, b, self.k)):
            return []
        wa, wb = self.shown(list(a), list(b))
        if a and b:
            text = _say(self.la, "按", wa, "分组，", self.lb, "按", wb, "分组")
        elif a:
            text = _say(self.la, "按", wa, "分组，", self.lb, "没有分组")
        else:
            text = _say(self.la, "没有分组，", self.lb, "按", wb, "分组")
        return [FacetDiff("group_by", wa or None, wb or None, text)]

    def having(self) -> list[FacetDiff]:
        only_a, only_b = self.unmatched(self.a.having, self.b.having, self.k)
        out = []
        for p, q in self.lookalikes(only_a, only_b):
            wa, wb = self.shown([p], [q])
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
            if wa == wb:
                wa, wb = self.annotated(wa, wb, [p for p, _ in x.order], [p for p, _ in y.order])
            out.append(FacetDiff("limit", wa, wb, _say(la, "取行前", wa, "，", lb, wb)))
        return out


def diff_facets(a: Facets, b: Facets, labels: tuple[str, str] = ("A", "B"), *,
                names: Mapping[str, str] | None = None) -> list[FacetDiff]:
    """逐个方面比两份 facets，每处不同一条，按对结果影响大致从大到小排：时间、筛选、聚合、算出来的列、连接和表、
    分组、HAVING、取哪几行。完全一致返回空列表。

    labels 是两边的叫法（缺省 A / B，双盲复算可以传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」:
    中文名}，可以用 catalog_labels 从数据目录取），给了的话时间列写成「支付时间 paid_at」。任一边解析不了就只返回
    一条 parse，不逐项比较：拿半份结构去比，只会报出一堆不存在的不同。

    比之前先把两边的派生来源按结构配好对，按共同的编号重新分析两边（见模块说明、_aligned）；给人看的写法不受影响。
    """
    la, lb = labels
    if a.parse_error or b.parse_error:
        if a.parse_error and b.parse_error:
            text = _say(la, "：", a.parse_error, "；", lb, "：", b.parse_error, "；没有逐项比较")
        else:
            side, error = (la, a.parse_error) if a.parse_error else (lb, b.parse_error)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return [FacetDiff("parse", a.parse_error, b.parse_error, text)]
    cmp = _Compare(*_aligned(a, b), labels, names)
    return cmp.time() + cmp.filters() + cmp.aggregates() + cmp.expressions() + cmp.joins() + cmp.group() + \
        cmp.having() + cmp.limit()


def _aligned(a: Facets, b: Facets) -> tuple[Facets, Facets]:
    """b 的派生来源按结构配给 a 的（见 _align），配上的改写成 a 里对应的编号、配不上的另起 a 里没有的编号，按这套
    编号重新分析两边的原文（a 的编号就是它的位置编号）。

    没用到的 CTE 不收进 facets（见 _Statement._enliven），只有一种例外：两边都定义了同一个 CTE（配成了一对），一边
    用到、一边没用到，而用到的那边是把它关联进来的。连接那一面会说「X 关联了它，Y 没有」；它里面的条件、聚合两边的
    定义里都有，只收用到的那一边，就会再被说成一边独有的一堆条件、聚合（两边都定义了付款、退款两个 CTE，一边关联
    付款的、一边关联退款的）。这时没用到的那一边也照样收它。用到的那边是从它取数（FROM 它）而不是关联它时不收：
    盲算定义了只筛已付的 CTE 却直接查 orders，少的那个条件正要报出来。

    任一边不是 facets 算出来的（没记原文）、重新解析不了，或者分析时出了意外，照原样返回：两边仍按各自的位置编号
    比，一边多出排在前面的 CTE 时会多报。
    """
    try:
        sa, sb = _Statement.reparse(a), _Statement.reparse(b)
        if sa is None or sb is None:
            return a, b
        da, db = sa.derived(), sb.derived()
        pairs = _align(da, db)
        fresh = iter(range(max((d.number for d in da), default=0) + 1, 1 << 30))
        sb.relabel({d.number: f"@{pairs[d.number]}" if d.number in pairs else f"@{next(fresh)}" for d in db},
                   _renamed(da, db, pairs))
        fa, fb = sa.facets(), sb.facets()
        extra_a = _switched(sb, fb, sa)
        extra_b = _switched(sa, fa, sb)
        if extra_a or extra_b:
            fa, fb = sa.facets(extra_a), sb.facets(extra_b)
        return replace(fa, _origin=a._origin), replace(fb, _origin=b._origin)
    except Exception:  # noqa: BLE001 - 配对本身的缺陷不能连累比较：退回按各自的编号比，记下来修
        logger.warning("SQL 派生来源配对失败，按各自的位置编号比较", exc_info=True)
        return a, b


def _switched(user: "_Statement", facets_of_user: Facets, other: "_Statement") -> set[str]:
    """user 用到、而且关联进来了的派生来源里，other 也定义了（配成了一对）却没用到的 CTE：other 照样收它（见
    _aligned）。返回 other 里这些 CTE 的 name_key。"""
    joined = {k for j in facets_of_user.joins for k in (*j.sides, j.table.key)}
    used, defined = other.used_labels(), other.defined_labels()
    return {cte for label in user.used_labels() if label in joined and label not in used
            and (cte := defined.get(label)) is not None}


__all__ = ["MAX_SQL_CHARS", "PLACEHOLDER", "Aggregate", "ColumnRef", "Computed", "FacetDiff", "Facets", "JoinFacet",
           "LimitFacet", "NormalizedSql", "Part", "TimePredicate", "catalog_labels", "diff_facets", "facets",
           "normalize_sql"]
