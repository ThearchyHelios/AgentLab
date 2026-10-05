"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），人据此选
A / B，只差一个方面、又对得上数据目录里的事实（时间归属、有效行过滤、去重）时选择会写进数据目录；运行对比要回答
「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了）；反方审稿要看自己的查询和原
查询的口径是否一致。几处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、注释、不加引号的名字的大小写都不算不同；结构变了的一定不同。
- explain_sql_diff：两条 SQL 是不是等价（SqlDiff.equivalent），不是的话差在哪（items、text_diff）。
- diff_facets：照旧收两份 facets，交给 explain_sql_diff 比两份记下的原文，返回它的 items。
- facets：把一条查询拆成表、连接、WHERE 合取式、时间条件、分组、聚合、算出来的列、HAVING、取哪几行（给调用方取
  结构用；CTE、子查询按位置编号，见 _Statement）。

**等价怎么判。** 过去几轮按方面归纳任意 SQL，每轮修好一批又冒出一批（还回退过），收不拢；现在先判等价、再说差在哪。
两边各自规范化，规范化的每一步都只做保持语义的变换，同一级的两个规范形式一字不差（比语法树逐个节点的类型和参数，
见 _fingerprint，不比生成的 SQL：sqlglot 按方言生成时会把不同的写法写成一样，SQLite 的 CAST(x AS DATE) 生成
DATE(x)）才算等价，否则一律不等价、条目一定不为空。规范形式分三级（见 _canonical_forms），哪一级对上都算：

- optimizer：sqlglot 优化器里保持语义的几步——qualify（认列、按表结构展开 SELECT *、展开 GROUP BY 1 和别名），
  eliminate_subqueries（FROM 里的子查询写成 CTE），merge_subqueries（只筛行、透列的 CTE、子查询并进外层；外连接
  可空一侧选出算出来的列的不并），eliminate_ctes（去掉没用的 CTE），pushdown_projections（去掉没用到的列），simplify
  （去掉和类型、排序规则、方言有关的三种化简，见 _simplify）；之前把整条语句的别名改成互不相同的（merge_subqueries
  只在同一段里避重名，同一个 CTE 并进外层和相关子查询两处会互相遮住），没给表结构时把从一张表 SELECT * 的 CTE 换成
  外面用到的那几列。不用的：unnest_subqueries（把 OR 里的标量子查询改成 CROSS JOIN，子查询没有行时整条都没了；
  SQLite 的标量子查询取第一行，改成连接就成了每行都比），canonicalize（给日期比较加 CAST，SQLite 里 CAST(… AS DATE)
  取的是数字），normalize（合取范式改出来的条件人读不懂，也用不上）。认不全列（没给表结构、几张表又都没写表名）、
  WHERE / GROUP BY / HAVING 里有和列别名同名、又可能是表的列的名字（库里先认列，qualify 却当成别名）、SELECT 列表里
  有和前面列别名同名、又不确定是表的列的名字（库里不认 SELECT 列表里的别名，qualify 没有表结构时却展开）、有递归
  CTE、LATERAL、表函数、不确定的函数（RANDOM）的不出这一级（见 _alias_or_column）。
- light：轻量规范化，不认列：统一标识符大小写和引号、去注释、只引用一次的非递归 CTE 写回用到它的地方，再走下面不
  需要认列的几步。
- parsed：解析后重新生成（只统一大小写、空白、注释、引号）。

两级共用的本模块自己的几步（见 _finish）：不影响结合的括号、ORDER BY 的 ASC、OFFSET 0 去掉；COUNT(1) 写成 COUNT(*)；
IN 列表去重排序、只有一项写成 =；EXISTS 子查询写成 SELECT 1；IN 子查询里的 DISTINCT 去掉；SELECT DISTINCT 写成按
选出来的列 GROUP BY，已经按选出来的列分组的 DISTINCT 去掉；原样选出一个子查询全部列的一段换成那个子查询（SQLite 里
UNION 不换：从子查询读合并的结果会按第一支的类型亲和转换别的支的值）；外层 WHERE 里只涉及一个分组 CTE 的条件挪进它的
HAVING、HAVING 里只涉及分组键的挪进 WHERE；只有两个来源的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的
行筛掉了的写成内连接；内连接的条件统一挪进 WHERE、来源写成逗号连接；最外层和条件里子查询的列名去掉；最后起规范的名字
（CTE 按定义排序叫 cte1、cte2…，CTE 的列按定义排序叫 c1、c2…，来源的别名叫表名、内层带 @层数、同名第二个起带 #序号，
同名来源谁先谁后逐个排列着试、取写出来最小的；取到哪几行、哪一行的值看扫描先后的一段——没有 ORDER BY 的 LIMIT、
聚合里既不分组也不聚合的列——来源不调换先后，见 _unstable），交换律成立的地方（AND、OR、比较两边、两项的加乘、IN
列表、GROUP BY、PARTITION BY）排好序。合并的各支只有 UNION ALL、而且调换先后不改变结果时才排（见 _Namer._reorderable）：去重的 UNION
和 INTERSECT 留先出现的那个值（SQLite 里 2 和 2.0、MySQL 不分大小写时 'SH' 和 'sh' 算重复），带 LIMIT、按名字 ORDER BY
的要看哪支在前，SQLite 合并结果的排序规则取最左一支的（只排整条语句最外层、不排序的）。

等价的结论另有执行比对守门（tests/test_sql_equivalence.py）：虚构库上五组随机数据加一组极端数据，判等价的两条每组
结果都得一样，结果不一样的绝不能判等价。给了表结构时 SELECT * 按它展开，表结构得和库对得上。原文里有 sqlglot 解析时
会丢掉、语义却不同的写法（一元加号）的，不在语法树上判等价。假设列没有声明非默认的排序规则：SQLite 比较两列时取左边
那列声明的排序规则，表结构里看不到，比较两边对调当成保持语义（两边都写了 COLLATE 的不对调）。

**具体说法的门槛（见 _Explainer）。** 在两边的规范形式上逐段比，只把有把握的不同写成具体的中文句子：两段读的来源
对得上（一模一样，或者只差一处说得清的：换了一张表、一边多关联了一张表、同一张表的关联类型或条件不同、外连接的
主表对调）之后，才逐项比时间条件（实表上的时间列不同、同一列的范围不同、只有一边按时间筛）、筛选条件（只涉及
这一段实表列的条件一边有一边没有、同一列上两边的条件不同）、选出来的列（聚合函数、去重计数、算法、列数和先后）、
分组键、HAVING、取行和排序、UNION 还是 UNION ALL。条件里套着的子查询两边各一个、外面写法一样的，往里比（第 5 轮
漏报的「只在 IN / EXISTS / 标量子查询里用到的 CTE」并进子查询之后就在这里说出来）；CTE 两边定义一样的对齐，各剩
一个对不上的配成一对往里比。说的都是「这一段的这一项两边各写的是什么」，不推断对结果的影响；原文写的是 LEFT /
RIGHT JOIN、规范化成了内连接或者调了方向的，照原文说（「写的是 LEFT JOIN，实际是 INNER JOIN」「以 X 为主表外连接」）。
牵涉 CTE 算出来的列、外层的列、自己关联自己的表、说不准的子查询，以及 CTE 里选出来的列（规范化去掉、重排过，和原文
对不上）的，宁可不说具体的。两边给人看的写法一样的话不说（读起来自相矛盾）。

**兜底。** 规范形式不一致、一条具体说法都没有时，给一条 aspect="other" 的「两条 SQL 的写法不同，未能归纳成具体差异，
请对照原文」；具体说法只覆盖了一部分时追加「另有未能归纳的差异，请对照原文」；fallback 为真。text_diff 给出规范化后
不同的行（difflib 比两边 sqlglot pretty 的输出，最多 40 行）。

仍做不到的（只给兜底说明，或者认不出等价）：
- 认不出的等价改写：IN 子查询和 EXISTS、NOT IN 和 NOT EXISTS 互换（空值时本来就不等价）、IN 子查询和关联同一张表
  （关联的键不唯一时会多出行，不知道主键就不能当成一回事）、窗口函数前后按分区键筛、三项以上的加乘换了结合方式、
  括号展开、和方言有关的同义写法（PostgreSQL 的 DATE(x) 和 CAST(x AS DATE)）、SQLite 里套一层子查询的 UNION、
  去重的 UNION 和 INTERSECT 调换各支、SQLite 里套在子查询里或带 ORDER BY 的 UNION ALL 调换各支、LATERAL、递归 CTE；
  没给表结构时 WHERE、GROUP BY、HAVING 里用列别名的（GROUP BY r 的 r 可能是表的列）、SELECT 列表里和前面的列别名
  同名的；没有 ORDER BY 的 LIMIT、聚合里既不分组也不聚合的列的一段调换关联先后。
- 说不出具体差异、只给兜底的：自己关联自己的方向或角色不同、两个 CTE 算出同名的列用了哪一个、相关子查询内外层分不清
  的、读的来源对不上（一边多一个分组 CTE 再关联、子查询里换了表又改了条件）、CTE 里选出来的列不同、NOT IN 对 NOT
  EXISTS、几处不同交错在一起的。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就只给一条 aspect="parse"；从不抛异常，
规范化或说明本身出了意外就当作这一级没有、说不清，记日志。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import re
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
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
#: _Statement._number_derived）。复制出来的条件里套着子查询时，靠它把 CTE 名、子查询的别名换成编号
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

    key 用来比较：别名换成真实表名，CTE、子查询写位置编号（见 _Statement._number_derived），表名、列名按
    name_key，等式和比较的两边排好顺序；loose 同 key 但不写实表的表名（派生来源的编号照写：它不是表名，换了表也还是
    那一个），两边查的表换了一张（orders 换成 orders_archive）时拿它
    比其余各方面，免得同一个条件只因表名不同被报成不同。text 给人看：好几张表都有的列写「表.列」（真实表名，不是
    别名），其余只写列名；full 一律写表名，两边的 text 一样、key 却不同时改用它说明。excludes 只在 WHERE 的「排除」
    写法上有：<>、NOT、NOT IN、IS NOT NULL 去掉否定之后排除的是什么（IS NOT NULL 写成「列 为空」）。column_keys 和
    columns 一一对应，是各列比较用的写法（实表.列；CTE、子查询的列写成「编号.列」）：两边对同一列的不同条件靠它配成
    一对说。相关子查询里引用外层的列，比较用的写法（key、loose、column_keys）在列名后面带「^」（往外几层带几个，见
    _Statement._depth）：内外读同一张表时，c.parent_id = o.id 和 c.id = o.parent_id 才分得开。

    scope 是这一项在哪一层（条件、分组键、HAVING 上有，见 _Statement._layers）：空串是最外层，「|2」是最外层 UNION
    的第 2 支，「@3」是派生来源 @3 里（「@3|2」是它的 UNION 第 2 支），「?」是条件、SELECT 列表里的子查询。facets 按
    （scope, key）去重：同一个条件在外层和子查询里各写了一次是两项。

    后面四项不进比较（两个 Part 相等只看前面几项），只用来把不同说清楚：
    origins 是这一项用到的派生来源的列各自的来历 [(「t1.total」, 「SUM(orders.amount)」)]（见 _Statement._origin）：
    两边写法一模一样（都是 t1.total > 100），各自的 t1 却不是同一个时，带上它才分得清。pierce 是一路透过派生来源写到
    实表的 key，只在中途被挡住时才有（这一项按派生来源的列写，见 _Statement.through）；stage 说是被什么挡住的（「取 t
    的前 10 行」「算 t 的窗口函数」）：两边 pierce 一样、只有一边被挡住，是同一个条件筛的先后不同。roles 是用到的自己
    关联自己的别名各是哪一个 [(别名, 在关联条件里出的列, 来源的名字)]（见 _Statement._roles）：关联的方向反了，两边都
    写 a.amount，各自的 a 却不是同一个时，带上它才分得清。
    """

    key: str
    text: str
    columns: tuple[ColumnRef, ...] = ()
    excludes: str | None = None
    loose: str = ""
    full: str = ""
    column_keys: tuple[str, ...] = ()
    scope: str = ""
    origins: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    pierce: str = field(default="", compare=False)
    stage: str = field(default="", compare=False)
    roles: tuple[tuple[str, str, str], ...] = field(default=(), compare=False)

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
    refunded 和 FROM refunded JOIN paid），比它们时按 sides 和条件比，不按哪一侧是关联进来的。

    keep、nullable 只在 LEFT / RIGHT JOIN 上有：保留全部行的主表那一侧、关联不上时记空的那一侧（比较用的写法，排好
    序）。LEFT JOIN 关联进来的那一侧可空，关联条件里的其余来源（没有就是它之前的各个来源）是主表；RIGHT JOIN 反过来。
    orders LEFT JOIN refunds 和 refunds RIGHT JOIN orders 是同一回事，和 refunds LEFT JOIN orders 不是：按主表比，
    不按写的是 LEFT 还是 RIGHT。rejects 是同一段 WHERE 里把可空那一侧没关联上的行筛掉的条件（那一侧的列
    拿来比较、IN、LIKE……，空值都不成立）：有它这次外连接实际等于内连接。scope 是这次连接在哪一层（见 Part.scope）：
    外层关联了 refunds，和 EXISTS 子查询里同样关联了 refunds，是两次连接。"""

    kind: str
    table: Part
    on: Part | None = None
    other: tuple[str, ...] = ()
    sides: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    nullable: tuple[str, ...] = ()
    rejects: Part | None = None
    scope: str = ""


@dataclass(frozen=True)
class TimePredicate:
    """WHERE 里的一个时间条件。op：= / <> / > / >= / < / <= / between / in。value 是另一侧给人看的写法：
    BETWEEN 是「低 AND 高」，IN 是「(…)」。

    column 是按哪一列筛时间：条件写的是 CTE、子查询算出来的列（WITH t AS (SELECT DATE(paid_at) AS d …) … WHERE
    d >= …）时，它在里面是同一列套着日期函数、CAST、MIN / MAX 的，换成里面那一列，一直换到实表（paid_at）：按支付
    时间还是下单时间筛，日期表达式写在 CTE 里也认得出。via 是路过的派生来源的列（「编号.列」，不进比较）：时间那一面
    已经报了按哪一列筛不同，派生来源那一列的定义不同就不再另报一遍。table 是这一列一直换到实表的 (表, 列)（不进
    比较）：读同一张表的几个派生来源同在一段时，column 照写派生来源的列（已付那份的 created_at、未付那份的 created_at
    是两列，见 _Statement._seal），给调用方对照数据目录的还是 orders.created_at。"""

    column: Part
    op: str
    value: str
    condition: Part
    via: tuple[str, ...] = field(default=(), compare=False)
    table: ColumnRef | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Aggregate:
    """一个聚合。func 是函数名的小写（sum / count / avg / min / max …）；arg 是参数，COUNT(*)、COUNT(1) 的 arg
    是「*」，去重聚合是 DISTINCT 里面的表达式；text 是整个聚合给人看的写法。窗口函数不算（它们按所在的那一列比，见
    Computed）。"""

    func: str
    arg: Part
    distinct: bool
    text: str
    scope: str = ""                 # 在哪一层（见 Part.scope）：外层算的和标量子查询里算的同一个聚合是两项


@dataclass(frozen=True)
class Computed:
    """选出来的一列是怎么算的。两种：

    - 最外层（UNION 的各支）选出来的列，不是光一列（分组那一面比）、也不是字面量的，比如 SUM(p.total) - SUM(r.total)、
      SUM(amount) OVER (PARTITION BY user_id)、CASE …；ROUND、CAST、COALESCE 给默认值这类外壳先剥掉（见
      _unwrap_value）。光一个聚合也收：另一边可能拿同一个合计再算了一下（SUM(amount) 对 SUM(amount) / 100）。scope、
      scope_key 为 None，name 为空串。
    - 派生来源（CTE、FROM 里的子查询）里被外面用到、又不是原样透出一列的列：它在里面的定义（amount * 0.9、
      DATE(paid_at)、ROW_NUMBER() OVER (…)、p.total - r.total）。外面按「t.net」引用它，两边写的都是 t.net，算法却可能
      不同，只比外面的写法看不出来。scope 是派生来源给人看的名字，scope_key 是它的位置编号，
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
    为 None）。"""

    count: str | None
    offset: str | None = None
    order: tuple[tuple[Part, bool], ...] = ()
    scope: str | None = None
    scope_key: str | None = None


@dataclass(frozen=True)
class SetOpFacet:
    """一段查询把几支 SELECT 合起来（UNION、UNION ALL、INTERSECT、EXCEPT）。ops 是各处合并的写法，按从左到右的先后
    （「UNION」「UNION ALL」…）；branches 是一共几支。scope 是哪一段：None 是最外层，CTE、FROM 里的子查询写它的名字；
    scope_key 是那一段所在的层（见 Part.scope：最外层是空串，派生来源是它的位置编号）。条件里的子查询不单算：
    它整个写在那个条件里比。"""

    ops: tuple[str, ...]
    branches: int
    scope: str | None = None
    scope_key: str = ""


@dataclass(frozen=True)
class Facets:
    """一条查询的各个方面。每一项都按在 SQL 里出现的先后排、按 key 去重；parse_error 不为空时其余都是空的。

    limit 是最外层 LIMIT 的行数（写法）；limits 是各段取哪几行（最外层在前，见 LimitFacet），只有写了 LIMIT 或
    OFFSET 的段才有。

    sources 是各段 FROM / JOIN 里的来源（实表、CTE、子查询；key 是比较用的写法，派生来源是编号，text 是给人看的
    名字），按出现的先后、按 key 去重；条件里套着的子查询（IN (SELECT … FROM users)）的来源不算，它们只筛行，不是
    连接的一头。外连接的主表、可空那一侧按它写名字。

    expressions 是算出来的列（见 Computed）。derived 是用到了的派生来源（key 是编号，text 是名字，含条件里套着的子
    查询用到的 CTE）：两边写法一样、同名的派生来源却不是同一个时，拿它说清「盲算的 t1 对应原流程的 t2」。

    where、group_by、having、joins、aggregates、time 的每一项都记着在哪一层（Part.scope、JoinFacet.scope、
    Aggregate.scope），按（层, key）去重；scopes 是这条语句里有的各层（key 是层的标记，text 是给人看的说法：「外层」
    「t」「t 的 UNION 第 2 支」「条件的子查询」），给人说是哪一层时用。group_by 里也有当分组用的
    SELECT DISTINCT 选出来的各项（这一段没有 GROUP BY、没有聚合时，和按它们分组出的行一样），distinct 是这样收进来的
    那几层：说「去重」不说「分组」。set_ops 是各段的 UNION、INTERSECT、EXCEPT（见 SetOpFacet）。grain 是最后出来
    的行按什么分（key 为「group:分组键…」「one」「rows」，见 _Statement._grain；最外层是 UNION 的为 None）。

    只收用到了的段：没用到的 CTE（定义了、外面没引用）里的条件、聚合、表不影响结果，不收（见 _Statement._enliven）。

    _origin 是 facets 记下的 (原文, 表结构)，不进比较、不进 as_dict：diff_facets 拿它交给 explain_sql_diff 比原文。
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
    set_ops: tuple[SetOpFacet, ...] = ()
    distinct: tuple[str, ...] = ()
    grain: Part | None = None
    scopes: tuple[Part, ...] = ()
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

    返回的 Facets 记着原文和表结构（_origin）：diff_facets 拿它交给 explain_sql_diff 比原文。
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
    alias: str = ""                 # 这一段里引用它的名字（别名，没有别名是表名、CTE 名）：给人看时分内外层用
    opaque: bool = False            # derived：同一段里别的来源透过去读的也是同一个来源，不透过去（见 _Statement._seal）

    @property
    def ref(self) -> str:
        """比较用的「哪一个来源」：key，自己关联自己时带上 role（orders#parent_id）。"""
        return f"{self.key}#{self.role}" if self.role else self.key


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
        """定下哪些段算数：最外层那段查询，加上从它一路引用到的 CTE（extra 里的 CTE 也当作用到了）。没
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
        CTE 定义的先后都不影响编号，里层多一段、少一段也不挪动外层的编号。这套编号只给 facets 用；比两条语句不靠
        它（见 explain_sql_diff：两边各自规范化，CTE 按定义起名）。
        """
        from sqlglot import exp

        ctes: dict[str, int] = {}
        count = 0
        #: 编了号的派生来源 [(位置编号, 起的名字, 里面那段查询, CTE 的 name_key 或 None)]，按编号排：facets 按它们
        #: 找各列的定义（见 _expressions）
        self._derived: list[tuple[int, str, Any, str | None]] = []

        def mark(node: Any, number: int) -> None:
            node.meta[_DERIVED] = f"@{number}"

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
        # 没用到的 CTE 也编上号（排在最后）：每个派生来源都得有编号
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

    def column_key(self, source: _Source, column: str) -> str:
        """比较用的列名：按 name_key。"""
        return name_key(column)

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
            out[slot].alias = alias
            aliases[slot] = alias
        self._roles(select, out, aliases)
        self._memo[id(select)] = out
        parent = select.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        out.outer = self.sources(parent) if parent is not None else None
        self._seal(out)
        return out

    def _base(self, source: _Source) -> str:
        """一路透过「只读一个来源」的派生来源（through 能透过去的那一路），最后读的是哪个来源：「种类:key」。"""
        from sqlglot import exp

        for _ in range(8):
            if source.kind != "derived" or not isinstance(source.query, exp.Select):
                break
            inner = self.sources(source.query)
            if len(inner) != 1:
                break
            source = next(iter(inner.values()))
        return f"{source.kind}:{source.key}"

    def _seal(self, sources: dict[str, _Source]) -> None:
        """同一段里有几个来源透过去读的是同一个来源时，其中的派生来源都不透过去（就地标 opaque，见 through）。

        两个原样透出的 CTE 都读 orders（已付的一份、未付的一份）：透过去 p.amount、o.amount 都成了 orders.amount，外层
        用的是哪一份就分不出来了（SUM(p.amount) 对 SUM(o.amount)、p.amount > 100 对 o.amount > 100 都成了一样）；CTE 和
        它自己读的那张表关联（父单、子单）、同一个原样透出的 CTE 关联两次也一样，自己关联自己时 _roles 设的 role 透过去
        也就丢了。这时这几个派生来源的列照写它们自己的编号：用的是已付那份
        还是未付那份分得开。只有一个来源读那张表时照旧透过去：先在 CTE 里筛一遍再查和直接查
        是同一列。"""
        groups: dict[str, list[_Source]] = {}
        for source in sources.values():
            groups.setdefault(self._base(source), []).append(source)
        for found in groups.values():
            if len(found) > 1:
                for source in found:
                    if source.kind == "derived":
                        source.opaque = True

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

    @staticmethod
    def _depth(column: Any, sources: dict[str, _Source]) -> int:
        """列是往外第几层那段的来源的（相关子查询引用外层的别名 o.id 是 1）；这一段的、认不出的是 0。

        比较用的写法里外层的列另带「^」（orders."id^"）：相关子查询读的表和外层是同一张时（orders 里找子单、父单），
        c.parent_id = o.id 和 c.id = o.parent_id 不带标记都成了 orders.id = orders.parent_id，分不出来。标记按列所在
        那一段算，从哪一段开始写都一样。"""
        qualifier = column.table
        if not qualifier:
            return 0
        slot = name_key(qualifier)
        if slot in sources:
            return 0
        outer, depth = getattr(sources, "outer", None), 1
        while outer is not None:
            if slot in outer:
                return depth
            outer, depth = outer.outer, depth + 1
        return 0

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

    def through(self, source: _Source | None, column: str, *, pierce: bool = False,
                unseal: bool = False) -> tuple[_Source | None, str]:
        """CTE、子查询只是把一张表的列原样透出来（SELECT *，或者直接选出这一列）时，换成那张表的列：
        原流程直接查 orders、盲算先在 CTE 里筛一遍再查，算的是同一列。

        这一段取前几行、或者算了窗口函数时不透过去（见 _blocks），照写这个派生来源的列：外面的 t.status = 'paid'
        和写在 t 里面的 status = 'paid' 不是同一个条件，透过去就都成了 orders.status = 'paid'，分不出来。pierce 时
        不管这些，一直透到底：拿来认出两边是不是同一个条件、只是筛的先后不同（见 Part.pierce）。

        同一段里别的来源透过去读的也是同一个来源时（opaque，见 _seal）不透过去，pierce 也不透：透过去就分不出是
        哪一份了。unseal 时连这个也不管，只拿来给人指出最后读的是哪张表的哪一列（时间条件的 detail）。"""
        for _ in range(4):
            if source is not None and source.opaque and not unseal:
                break
            found = self._pass(source, column)
            if found is None or (not pierce and self._blocks(source.query, found[1])):
                break
            source, column = found
        return source, column

    def resolve(self, column: Any, sources: dict[str, _Source], *, pierce: bool = False,
                unseal: bool = False) -> tuple[_Source | None, str]:
        return self.through(self.locate(column, sources)[1], column.name, pierce=pierce, unseal=unseal)

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
        子查询里拿两张表的列互相比的（相关子查询），给人看时和关联条件一样写表名；内外读的是同一个来源（orders 里找
        子单）时写表名分不出谁是谁，照写别名、留着表别名。比较用的写法里外层的列在列名后面带「^」（见 _depth），各自
        起的名字去掉（见 _drop_names）。
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
        #: 给人看时照写别名的段（id）：里面的列和外层的列读的是同一个来源（见下）
        keep: set[int] = set()
        for owner, cols in groups.values():
            scope = sources if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])
            found = [(col, *self.resolve(col, scope, pierce=mode == "pierce"), self._depth(col, scope))
                     for col in cols]
            here = mode
            seen = {(s.ref, depth) for _, s, _, depth in found if s is not None}
            # 同一个来源既是这一段的、又是外层的（相关子查询读的表和外层是同一张）：写表名分不出谁是谁，写别名
            clash = {ref for ref, _ in seen if sum(1 for other, _ in seen if other == ref) > 1}
            if owner is not None and mode == "text" and len(seen) > 1:
                here = "full"
            for col, source, name, depth in found:
                alias = None
                if mode not in _COMPARING and source is not None and source.ref in clash:
                    located = self.locate(col, scope)[1]
                    alias = col.args["table"].copy() if col.table else \
                        exp.to_identifier(located.alias) if located is not None and located.alias else None
                    keep.add(id(owner))
                self._write(col, source, name, here, depth=depth, alias=alias)
        for table in copy.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier) and (owner := self._owner(table)) is not None:
                if id(owner) not in keep:
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
        FROM 里的子查询，它选出来的列名也会去掉。"""
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

    def _write(self, col: Any, source: _Source | None, name: str, mode: str, *, depth: int = 0,
               alias: Any = None) -> None:
        """按 render 的写法改写一列（就地改）。depth 是它属于往外第几层的来源（见 _depth）：比较用的写法里另带
        「^」。alias 是给人看时照写的别名（内外层读同一个来源时，见 render）：这时列名也照原样写，不写透过去的。"""
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
            # 外层的列在列名后面带标记（不带在表名上：表名带了标记要加引号，排比较的两边时外层的列就都排到前面去了，
            # 只该在两边表名一样时才影响先后）
            column = self.column_key(source, name) if source is not None else name_key(name)
            col.set("this", exp.to_identifier(column + "^" * depth))
            col.set("table", exp.to_identifier(owner) if owner else None)
            col.set("db", None)
            col.set("catalog", None)
            return
        if alias is not None:
            col.set("table", alias)
            col.set("db", None)
            col.set("catalog", None)
            return
        col.set("this", exp.Identifier(this=name, quoted=quoted or not _PLAIN_NAME.match(name)))
        # 自己关联自己的（有 role）一律写别名：只写列名读不出是哪一个。不透过去的派生来源（opaque）不算「整条语句只用到
        # 一张实表」：两个 CTE 都读 orders，好几个都有的列（shared）得写出是哪一个的
        if source is None or (source.kind != "other" and mode != "full" and not source.role
                              and ((self.single and not source.opaque) or name_key(name) not in self.shared())):
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

        by 是 key 时按位置编号排；算结构标识时 by 是 shape，按结构标识排：位置编号决定不了先后。"""
        return self.render(node, sources, "anon"), self.render(node, sources, by)

    def part(self, node: Any, sources: dict[str, _Source], *, excludes: bool = False) -> Part:
        #: 用到的各列 {(哪一个来源, 列): ((给人看的表名, 列), 比较用的写法)}：按「哪一个来源」分，自己关联自己的两个别名
        #: 是两列（o1.amount、o2.amount）
        refs: dict[tuple[str, str], tuple[ColumnRef, str]] = {}
        origins: list[tuple[str, str]] = []
        roles: list[tuple[str, str, str]] = []
        stage = ""
        for col in _own_columns(node):
            located = self.locate(col, sources)[1]
            # 外层的列另带标记（见 _depth）：相关子查询里 c.amount 和外层的 o.amount 是两列
            mark = "^" * self._depth(col, sources)
            source, name = self.through(located, col.name)
            refs.setdefault((source.ref if source is not None else "", name_key(name) + mark),
                            ((source.name if source is not None else None, name),
                             f"{source.ref}.{self.column_key(source, name)}{mark}" if source is not None
                             else f".{name_key(name)}{mark}"))
            if source is not None and source.role and source.idents:
                if (note := (source.idents[-1].name, source.role, source.name)) not in roles:
                    roles.append(note)
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
                    origins=tuple(origins), pierce=pierce, stage=stage, roles=tuple(roles))

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
        """各个方面。extra 是没用到、也照样收进来的 CTE（name_key），别的没用到的 CTE 不收（见 _enliven）。
        条件、分组键、连接、聚合、HAVING 都记下在哪一层（见 _layers），按（层, key）去重。"""
        from sqlglot import exp

        self._enliven(extra)
        tags = self._layers(extra)
        joins: list[JoinFacet] = []
        where: list[Part] = []
        time: list[TimePredicate] = []
        group: list[Part] = []
        distinct: list[str] = []
        aggregates: list[Aggregate] = []
        having: list[Part] = []
        used: list[Part] = []
        for select in self._alive(exp.Select):
            sources = self.sources(select)
            # 没走到的段（条件、SELECT 列表里的子查询，以及只在它们里面用到的派生来源）在「?」那一层
            layer = tags.get(id(select), "?")
            if select is self.root or not self._in_condition(select):
                used += [Part.plain(s.ref, s.name) for s in sources.values()]
            drafts = self._join_drafts(select, sources)
            clause = select.args.get("where")
            for cond in _conjuncts(clause.this) if clause is not None else []:
                canon = self.canonical(cond, sources)
                if self._joins_on_where(canon, sources, drafts):
                    continue
                part = replace(self.part(canon, sources, excludes=True), scope=layer)
                where.append(part)
                self._rejects(canon, part, sources, drafts)
                if found := self._time(canon, sources):
                    time.append(replace(found, condition=replace(found.condition, scope=layer)))
            joins += [replace(self._join(d, sources), scope=layer) for d in drafts]
            keys = self._group(select, sources)
            if not keys and (keys := self._distinct_keys(select, sources)):
                distinct.append(layer)
            group += [replace(p, scope=layer) for p in keys]
            aggregates += [replace(g, scope=layer) for g in self._aggregates(select, sources)]
            having += [replace(p, scope=layer) for p in self._having(select, sources)]
        derived = [Part.plain(self._labels[number], name or "子查询")
                   for number, name, body, _ in self._derived if id(body) not in self._dead]
        return Facets(
            dialect=self.read,
            tables=tuple(Part.plain(s.key, s.name) for s in self.tables),
            joins=_dedupe(joins, lambda j: (j.scope, j.kind, j.table.key, j.on.key if j.on else None)),
            where=_dedupe(where, lambda p: (p.scope, p.key)),
            time=_dedupe(time, lambda t: (t.condition.scope, t.condition.key)),
            group_by=_dedupe(group, lambda p: (p.scope, p.key)),
            aggregates=_dedupe(aggregates, lambda a: (a.scope, a.func, a.arg.key, a.distinct)),
            having=_dedupe(having, lambda p: (p.scope, p.key)),
            limit=self._limit(),
            limits=tuple(self._limits()),
            sources=_dedupe(used, lambda p: p.key),
            expressions=_dedupe(self._expressions(), lambda c: (c.scope_key, c.name, c.expr.key)),
            derived=_dedupe(derived, lambda p: p.key),
            set_ops=tuple(self._set_ops(tags)),
            distinct=tuple(dict.fromkeys(distinct)),
            grain=self._grain(self.root),
            scopes=self._scope_parts(tags),
        )

    # ---- 层

    def _layers(self, extra: Iterable[str] = ()) -> dict[int, str]:
        """每段 SELECT 在哪一层：{段的 id: 层的标记}（见 Part.scope）。

        最外层是空串，UNION 的第 k 支（k ≥ 2）在后面加「|k」。FROM / JOIN 里的派生来源（CTE、子查询、LATERAL 子查询）
        是它自己的位置编号；只从一个来源里筛出一部分行的（见 _transparent）
        和用到它的那一段算同一层：写在这种 CTE 里的条件和写在外层是一回事。读同一个来源的几个派生来源同在一段时
        （见 _seal）各算各的层：两个 CTE 里各有一个 is_deleted = 0 是两个条件。派生来源在哪一层按在 FROM / JOIN 里
        第一次用到它的地方定，从外往里一层一层地走。没走到的段（条件、SELECT 列表里的子查询，以及只在它们里面用到
        的派生来源）由 facets 记成「?」。extra 是没用到、也照样收进来的 CTE，在它自己的编号那一层。"""
        from sqlglot import exp

        tags: dict[int, str] = {}
        queue: deque[tuple[Any, str]] = deque()

        def push(query: Any, tag: str) -> None:
            for i, branch in enumerate(_branches(query)):
                queue.append((branch, tag if i == 0 else f"{tag}|{i + 1}"))

        def drain() -> None:
            while queue:
                select, tag = queue.popleft()
                if id(select) in tags:
                    continue
                tags[id(select)] = tag
                sources = self.sources(select)
                for i, item in enumerate(self._items(select)):
                    body, label = _derived_body(item), item.meta.get(_DERIVED)
                    if body is None and isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) \
                            and item.name and self._is_cte(item):
                        body = self.ctes[name_key(item.name)]
                    if body is None or not label:
                        continue
                    alias = _alias(item)
                    source = sources.get(name_key(alias) if alias else f"#{i}")
                    lifted = source is not None and not source.opaque and self._transparent(item, body)
                    push(body, tag if lifted else label)

        push(self.root, "")
        drain()
        for name in extra:
            if name in self._cte_defs and (label := self._cte_defs[name].meta.get(_DERIVED)):
                push(self.ctes[name], label)
        drain()
        return tags

    def _transparent(self, item: Any, body: Any) -> bool:
        """派生来源只是从一个来源里筛出一部分行（不分组、不聚合、不去重、不是 UNION，也不是 LATERAL）：写在它里面的
        条件和写在用到它的那一段是一回事，算同一层（见 _layers）。各列怎么算不影响有哪些行；取前几行、算窗口函数的
        也算同一层，筛的先后另由 Part.pierce / stage 说。"""
        from sqlglot import exp

        if isinstance(item, exp.Lateral) or not isinstance(body, exp.Select) or len(self._items(body)) != 1:
            return False
        if any(body.args.get(k) is not None for k in ("group", "having", "distinct")):
            return False
        return not self._aggregating(body)

    @staticmethod
    def _aggregating(select: Any) -> bool:
        """这一段自己算了聚合（窗口函数、套着的子查询里的不算）。"""
        from sqlglot import exp

        return any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _own_nodes(select))

    def _scope_parts(self, tags: Mapping[int, str]) -> tuple[Part, ...]:
        """各层给人看的说法（Part.plain(层的标记, 说法)）：最外层「外层」，派生来源写它的名字，UNION 的各支写「t 的
        UNION 第 2 支」，条件里的子查询「条件的子查询」。"""
        names = {self._labels[number]: name or "子查询" for number, name, _, _ in self._derived}
        unions = {self._labels[number] for number, _, body, _ in self._derived if len(_branches(body)) > 1}
        root_union = len(_branches(self.root)) > 1
        out: dict[str, Part] = {}
        for tag in dict.fromkeys([*tags.values(), "?"]):
            head, _, branch = tag.partition("|")
            if head == "?":
                text = "条件的子查询"
            elif head == "":
                text = f"UNION 第 {branch or 1} 支" if root_union else "外层"
            else:
                name = names.get(head, "子查询")
                text = _say(name, "的 UNION 第", branch or "1", "支") if head in unions else name
            out[tag] = Part.plain(tag, text)
        return tuple(out.values())

    def _set_ops(self, tags: Mapping[int, str]) -> list[SetOpFacet]:
        """各段的 UNION、INTERSECT、EXCEPT（见 SetOpFacet），条件里的子查询里的不算。"""
        from sqlglot import exp

        out = []
        for node in self._alive(exp.SetOperation):
            parent = node.parent
            while isinstance(parent, exp.Subquery):
                parent = parent.parent
            branches = _branches(node)
            if isinstance(parent, exp.SetOperation) or not branches:
                continue                    # 只看一串合并最上面的那一个
            tag = tags.get(id(branches[0]), "?")
            if tag != "?":
                out.append(SetOpFacet(ops=tuple(self._ops(node)), branches=len(branches),
                                      scope=self._scope_name(node), scope_key=tag))
        return out

    @classmethod
    def _ops(cls, node: Any) -> list[str]:
        """一串合并从左到右各处的写法：UNION、UNION ALL、INTERSECT、EXCEPT ALL……"""
        from sqlglot import exp

        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.SetOperation):
            return []
        word = {"Union": "UNION", "Intersect": "INTERSECT", "Except": "EXCEPT"}.get(type(node).__name__,
                                                                                  type(node).__name__.upper())
        if not node.args.get("distinct"):
            word += " ALL"
        return cls._ops(node.this) + [word] + cls._ops(node.expression)

    def _distinct_keys(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """SELECT DISTINCT（不带 ON）选出来的各项。没有 GROUP BY、没有聚合时，对这几项去重和按它们分组出的行一模一样，
        当这一段的分组键收（见 Facets.distinct）：SELECT DISTINCT user_id 和 GROUP BY user_id 不算不同，一边在子查询里
        先去重、另一边没有照报。DISTINCT ON 是每组取一行，按窗口函数算（见 _blocking）。"""
        from sqlglot import exp

        distinct = select.args.get("distinct")
        if not isinstance(distinct, exp.Distinct) or distinct.args.get("on") is not None \
                or select.args.get("group") is not None or self._aggregating(select):
            return []
        out = []
        for item in select.selects:
            if _is_star(item):
                out.append(Part.plain("*", "*"))
                continue
            target = item.this if isinstance(item, exp.Alias) else item
            out.append(self.part(self.canonical(target, sources), sources))
        return out

    def _grain(self, query: Any, depth: int = 0) -> Part | None:
        """最后出来的行按什么分：分组（GROUP BY、当分组用的 DISTINCT）是「group:各分组键」，没分组却算了聚合是
        「one」（只出一行），都没有、只从一个派生来源取数的看那个派生来源，别的是「rows」（逐行）。最外层是 UNION
        的不算（返回 None）。分组键挪了一层（先在子查询里按 user_id 分组、外层数用户数，对外层按 user_id 分组）按
        整条语句比分组键看不出来：外层出一行还是每个用户一行，看它。"""
        from sqlglot import exp

        while isinstance(query, exp.Subquery):
            query = query.this
        if not isinstance(query, exp.Select) or depth > 6:
            return None
        sources = self.sources(query)
        keys = self._group(query, sources)
        if not keys and self._aggregating(query):
            return Part.plain("one", "")
        keys = keys or self._distinct_keys(query, sources)
        if keys:
            return Part(key="group:" + ", ".join(sorted({p.key for p in keys})),
                        text="、".join(dict.fromkeys(p.text for p in keys)),
                        loose="group:" + ", ".join(sorted({p.loose for p in keys})),
                        full="、".join(dict.fromkeys(p.full for p in keys)))
        items = self._items(query)
        if len(items) == 1 and not isinstance(items[0], exp.Lateral):
            item, body = items[0], _derived_body(items[0])
            if body is None and isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) \
                    and item.name and self._is_cte(item):
                body = self.ctes[name_key(item.name)]
            if body is not None:
                return self._grain(body, depth + 1)
        return Part.plain("rows", "")

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
        # 给调用方对照数据目录的那一列一直换到实表：不透过去的派生来源（见 _seal）比较时照写它自己，可它的这一列
        # 读的还是那张表的那一列
        real, real_scope, _, _ = self._time_column(cols[0], sources, unseal=True)
        found = self.resolve(real, real_scope, pierce=True, unseal=True)
        table = found[0].name if found[0] is not None and found[0].kind == "table" else None
        return TimePredicate(column=self.part(base, scope), op=op, value=value,
                             condition=self.part(cond, sources, excludes=True), via=via,
                             table=(table, found[1]) if table else None)

    def _time_column(self, column: Any, sources: dict[str, _Source], *,
                     unseal: bool = False) -> tuple[Any, _Sources, tuple[str, ...], bool]:
        """时间条件里的那一列是 CTE、子查询算出来的、在里面是同一列套着日期函数、CAST、MIN / MAX 时（见 _value_column），
        换成里面那一列，一直换到实表：(那一列, 它那一段的来源, 路过的派生来源的列「编号.列」, 路上有没有日期函数)。
        原样透出的列 resolve 已经透过去了；这里补上算了一下的。不透过去的派生来源（见 _seal）到它为止：两个 CTE 都读
        orders 时，按已付那份的 created_at 筛还是按未付那份的筛，正是要分清的；unseal 时不管这个（见 through）。"""
        via: list[str] = []
        datey = False
        for _ in range(4):
            source, name = self.resolve(column, sources, unseal=unseal)
            if source is not None and source.opaque and not unseal:
                break
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
# 等价判定：只做保持语义的变换，比规范形式
# ==========================================================================

#: 规范形式的几级（见 _canonical_forms）。等价的结论只在同一级的两个规范形式一字不差时才下
_OPTIMIZER, _LIGHT, _PARSED = "optimizer", "light", "parsed"
#: 同一段里同名的几个来源（自己关联自己）谁先谁后逐个排列着试，取写出来最小的那个；排列数超过这个就只按原来的
#: 先后（认得出的等价改写少一些，不会错判）
_MAX_ORDERINGS = 64
#: 一条语句里一共试多少种排列（里层的子查询在外层每试一种时都要重排一遍）：用完了就都只按原来的先后
_ORDERING_BUDGET = 2000
#: 原来的名字（CTE 名、FROM 里子查询的别名）记在里面那段查询的 meta 里：起了规范的名字之后，差异说明还要照原样说
_ORIGINAL = "sql_semantics_name"
#: GROUP BY 是从 SELECT DISTINCT 改写来的：差异说明里说「去重」
_FROM_DISTINCT = "sql_semantics_distinct"
#: 写的是 LEFT / RIGHT JOIN、WHERE 把没关联上的行筛掉了、按内连接比的来源（记在那张表上，值是原来写的连接）：差异
#: 说明里照实说「写的是 LEFT JOIN，实际是 INNER JOIN」
_REJECTED = "sql_semantics_rejected"
#: 从 RIGHT JOIN 改写成的 LEFT JOIN（记在 JOIN 上）：差异说明里不说「用 LEFT JOIN」，说以哪一侧为主表
_FLIPPED = "sql_semantics_flipped"
#: 不确定的函数：每次求值结果都可能不同，合并子查询会把一次求值复制成几次，不走优化器那一级
_VOLATILE = frozenset({"random", "rand", "randn", "newid", "uuid", "gen_random_uuid", "uuid_generate_v4", "random_uuid",
                       "randomblob", "setseed", "nextval", "sys_guid"})
#: 拿空值比较、算术都得空值（外连接没关联上的那一侧整行是空值）的写法，WHERE 里用到它就把没关联上的行筛掉了
_STRICT_UNARY = ("Paren", "Neg", "Cast", "Lower", "Upper", "Length", "Abs")
_STRICT_BINARY = ("Add", "Sub", "Mul", "Div", "Mod", "DPipe")


class _Unsupported(Exception):
    """这一级规范化处理不了这条语句的结构（递归 CTE、LATERAL、表函数……），这一级不出规范形式。"""


#: 一元加号前面可能是这些词（不是一个值的结尾）
_OPERAND_KEYWORDS = frozenset({"SELECT", "WHERE", "AND", "OR", "NOT", "ON", "WHEN", "THEN", "ELSE", "HAVING", "BY", "IN",
                               "IS", "LIKE", "GLOB", "BETWEEN", "CASE", "AS", "DISTINCT", "ALL", "SET", "VALUES",
                               "LIMIT", "OFFSET", "RETURN", "ESCAPE", "FILTER", "OVER", "PARTITION", "USING", "IF",
                               "EXISTS", "ANY", "SOME", "FROM", "JOIN", "WITH", "ORDER", "GROUP"})


def _lost_in_parsing(text: str, read: str | None) -> bool:
    """原文里有 sqlglot 解析时丢掉、语义却不一样的写法：一元加号（SQLite 里 +a 去掉了列的类型亲和，+a = '10' 和
    a = '10' 结果不同，解析出来却是同一棵树）。有就不在语法树上判等价。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 切词都不行的，解析也过不了，那边会说
        return False
    prev = None
    for token in tokens:
        if token.token_type.name == "PLUS":
            if prev is None:
                return True
            kind, word = prev.token_type.name, prev.text
            operand = kind in ("VAR", "IDENTIFIER", "NUMBER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE", "END",
                               "STAR") or kind.endswith("STRING") or (
                _PLAIN_NAME.match(word) is not None and word.upper() not in _OPERAND_KEYWORDS)
            if not operand:
                return True
        prev = token
    return False


@dataclass
class _Form:
    """一条语句在某一级的规范形式。key 一样才算等价，text 是生成的 SQL（给人看）；pre 是起规范名字之前的树（差异
    说明按另一边的 CTE 名重新起名时用，见 _Explainer.statement）。"""

    level: str
    text: str
    tree: Any
    pre: Any = None
    qualified: bool = False

    @property
    def key(self) -> str:
        """比等价用的写法：语法树逐个节点的类型和参数（见 _fingerprint）。不拿生成的 SQL 比：sqlglot 按方言生成时
        会把不同的写法写成同一个样子（SQLite 的 CAST(x AS DATE) 生成 DATE(x)，两者在 SQLite 里结果不同）。"""
        return _fingerprint(self.tree)


def _fingerprint(node: Any) -> str:
    """语法树的确定写法：每个节点写类名和全部参数（按参数名排序，字符串原样），不同的树一定写得不一样。类型标注、
    注释、meta 不算。"""
    from sqlglot import exp

    if isinstance(node, exp.Expr):
        parts = [f"{k}={_fingerprint(v)}" for k, v in sorted(node.args.items())
                 if v is not None and v is not False and v != []]
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_fingerprint(v) for v in node) + "]"
    return repr(node)


def _key(node: Any, read: str | None) -> tuple[str, str]:
    """排序、去重用的键：先按生成的 SQL 排（好读），同样写法的再按 _fingerprint 分开。"""
    return node.sql(dialect=read, comments=False), _fingerprint(node)


def _schema_mapping(schema: Mapping[str, Any] | None) -> dict[str, dict[str, str]] | None:
    """schema_cache → sqlglot 的表结构 {表: {列: 类型}}。类型一律记 UNKNOWN：只要列名（认列属于哪张表、展开
    SELECT *）；按类型做的改写和方言有关（canonicalize 给日期比较加 CAST，SQLite 里 CAST(… AS DATE) 取的是数字），
    不做。"""
    tables = schema.get("tables") if isinstance(schema, Mapping) else None
    if not isinstance(tables, Mapping):
        return None
    out: dict[str, dict[str, str]] = {}
    for name, meta in tables.items():
        columns = meta.get("columns") if isinstance(meta, Mapping) else None
        names = [c["name"] for c in columns or [] if isinstance(c, Mapping) and isinstance(c.get("name"), str)
                 and c["name"]]
        if names:
            out[str(name)] = {n: "UNKNOWN" for n in names}
    return out or None


def _canonical_forms(root: Any, read: str | None, schema: dict[str, dict[str, str]] | None) -> dict[str, _Form]:
    """一条语句的几级规范形式：

    - optimizer：sqlglot 优化器的几步（qualify 认列、展开 SELECT *、别名展开；eliminate_subqueries 把 FROM 里的子查询
      写成 CTE；merge_subqueries 把只用一次、只是筛行透列的 CTE、子查询并进外层；eliminate_ctes 去掉没用的 CTE；
      pushdown_projections 去掉没用到的列；simplify 化简条件），加上本模块自己的几步（见 _finish）。认不全列（没给表
      结构、几张表又都没写表名）、有递归 CTE、LATERAL 之类的不出这一级。
    - light：轻量规范化，不认列：统一标识符大小写、去注释、只引用一次的非递归 CTE 写回用到它的地方，再走 _finish 里
      不需要认列的几步。
    - parsed：解析后重新生成的写法（只统一了大小写、空白、注释），总是有。

    每一级的每一步都只做保持语义的变换，某一级出不来就没有这一级，从不抛异常。"""
    out: dict[str, _Form] = {}
    for level, build in ((_OPTIMIZER, _optimized), (_LIGHT, _light)):
        try:
            out[level] = build(root, read, schema)
        except _Unsupported as e:
            logger.debug("SQL 规范化（%s）跳过：%s", level, e)
        except Exception as e:  # noqa: BLE001 - sqlglot 认不了列、写法不支持：这一级不出，别的级照样比
            from sqlglot.errors import SqlglotError

            if isinstance(e, SqlglotError):
                logger.debug("SQL 规范化（%s）跳过：%s", level, e)
            else:
                logger.warning("SQL 规范化（%s）失败", level, exc_info=True)
    try:
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

        tree = normalize_identifiers(root.copy(), dialect=read)
        _unquote(tree, read)
        out[_PARSED] = _Form(_PARSED, tree.sql(dialect=read, comments=False), tree)
    except Exception:  # noqa: BLE001
        logger.warning("SQL 重新生成失败", exc_info=True)
    return out


def _optimized(root: Any, read: str | None, schema: dict[str, dict[str, str]] | None) -> _Form:
    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.eliminate_ctes import eliminate_ctes
    from sqlglot.optimizer.eliminate_subqueries import eliminate_subqueries
    from sqlglot.optimizer.merge_subqueries import merge_subqueries
    from sqlglot.optimizer.pushdown_projections import pushdown_projections
    from sqlglot.optimizer.qualify import qualify

    tree = root.copy()
    why = _unsupported(tree) or ("不确定的函数" if _volatile(tree) else None)
    if why:
        raise _Unsupported(why)
    _remember_names(tree)
    # 给了表结构先按表结构认列；表结构里没有的列（表结构旧了）再按不给表结构认一遍：两遍都认不全就不出这一级
    qualified = None
    for mapping in ([schema, None] if schema else [None]):
        if _alias_or_column(tree, read, mapping):
            continue
        try:
            qualified = qualify(tree.copy(), dialect=read, schema=mapping, validate_qualify_columns=True,
                                quote_identifiers=False, identify=False, expand_stars=True, expand_alias_refs=True)
            break
        except OptimizeError as e:
            logger.debug("SQL 认列失败：%s", e)
    if qualified is None:
        raise _Unsupported("认不全列")
    tree = qualified
    if _has_star(tree):
        _narrow_stars(tree, read)
    if not _has_star(tree) and not _correlated_derived(tree):
        tree = eliminate_subqueries(tree)
        _split_simple_ctes(tree)
        # merge_subqueries 只在同一段里避免重名：同一个 CTE 并进外层和相关子查询两处，两处都叫 orders，子查询里本来
        # 指外层的 orders.id 就被里层遮住了。先把整条语句的别名都改成互不相同的
        _unique_aliases(tree, read)
        # merge_subqueries 会把外连接可空一侧的子查询里算出来的列（常量、COALESCE）原样并进外层：没关联上的行本该
        # 是空值，并进来就成了常量。这种写法不并
        if _merge_safe(tree):
            tree = merge_subqueries(tree)
        tree = eliminate_ctes(tree)
        _push_into_groups(tree)
        tree = pushdown_projections(tree)
    else:
        tree = eliminate_ctes(tree)
    return _finish(tree, read, level=_OPTIMIZER, qualified=True)


def _light(root: Any, read: str | None, schema: Any = None) -> _Form:
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    from sqlglot.optimizer.qualify_tables import qualify_tables

    tree = normalize_identifiers(root.copy(), dialect=read)
    why = _unsupported(tree)
    if why:
        raise _Unsupported(why)
    _remember_names(tree)
    _inline_ctes(tree)
    tree = qualify_tables(tree, dialect=read)
    return _finish(tree, read, level=_LIGHT, qualified=False)


def _finish(tree: Any, read: str | None, *, level: str, qualified: bool) -> _Form:
    """两级共用的本模块自己的几步，都只做保持语义的变换（为什么保持语义见各函数的说明）：

    1. 和名字无关的改写：不需要的引号、不影响结合的括号、ORDER BY 的 ASC、OFFSET 0 去掉；COUNT(1) 写成 COUNT(*)；
       IN 列表去重排序、只有一项写成 =；EXISTS 子查询写成 SELECT 1；IN 子查询里的 DISTINCT、不取行时的 ORDER BY
       去掉；SELECT DISTINCT 写成按选出来的列 GROUP BY，已经按选出来的列分组的 DISTINCT 去掉；原样选出一个子查询
       全部列的一段换成那个子查询；两张表的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的行筛掉了的
       写成内连接；内连接的条件挪进 WHERE、来源写成逗号连接；最外层 ORDER BY 引用的别名换成表达式，最外层和条件里
       的子查询选出来的列名去掉（列名不影响取值）。
    2. 化简条件（_simplify：sqlglot 的 simplify 去掉和类型、排序规则、方言有关的几种化简）。
    3. 起规范的名字、排好交换律成立的地方（见 _named、_Namer、_sort_own）。
    """
    _unquote(tree, read)
    _strip_parens(tree)
    _strip_asc(tree)
    _count_star(tree)
    _in_lists(tree, read)
    _exists_projection(tree)
    _in_subquery_distinct(tree)
    _distinct_to_group(tree, read)
    _redundant_distinct(tree, read)
    _zero_offset(tree)
    tree = _unwrap_passthrough(tree, read)
    _right_to_left(tree)
    _reject_nulls(tree)
    _flatten_inner(tree)
    _order_by_aliases(tree)
    _drop_output_aliases(tree)
    tree = _simplify(tree, read)
    pre = tree.copy()
    tree = _named(tree, read, qualified)
    return _Form(level, tree.sql(dialect=read, comments=False), tree, pre, qualified)


def _named(tree: Any, read: str | None, qualified: bool, hint: "_Hint | None" = None) -> Any:
    """起规范的名字、排好交换律成立的地方，最后统一引号（只给需要的标识符加）。"""
    from sqlglot import exp
    from sqlglot.optimizer.qualify_columns import quote_identifiers

    _Namer(tree, read, qualified, hint).canon(tree, 0, {})
    tree = _simplify(tree, read)
    for select in list(tree.find_all(exp.Select)):
        _sort_own(select, read)
    return quote_identifiers(tree, dialect=read, identify=False)


# ---- 守门：哪些结构不走某一级

def _volatile(tree: Any) -> bool:
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, (exp.Rand, exp.Randn)) or type(node).__name__.lower() in _VOLATILE:
            return True
        if isinstance(node, exp.Anonymous) and str(node.name).lower() in _VOLATILE:
            return True
    return False


def _unsupported(tree: Any) -> str | None:
    """递归 CTE、LATERAL、UNNEST、PIVOT、表函数、VALUES、SEMI / ANTI / ASOF 这些连接：起名、合并都不处理。"""
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, exp.With) and node.args.get("recursive"):
            return "递归 CTE"
        if isinstance(node, (exp.Lateral, exp.Unnest, exp.Pivot, exp.TableSample, exp.Values)):
            return type(node).__name__
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            return "表函数"
        if isinstance(node, exp.Join) and (node.args.get("method") or str(node.kind or "").upper() in
                                           ("SEMI", "ANTI", "ASOF", "POSITIONAL", "STRAIGHT_JOIN")):
            return "连接方式"
    return None


def _starry(select: Any) -> bool:
    from sqlglot import exp

    return any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star) for p in select.expressions)


def _takes_any(select: Any) -> bool:
    """没有 ORDER BY 的 LIMIT / OFFSET：取哪几行 SQL 没规定，库里取先出来的几行。出行的先后会变的改写（调换来源、
    DISTINCT 写成 GROUP BY、RIGHT JOIN 写成 LEFT JOIN）这一段都不做，免得取到的行不同。"""
    return (select.args.get("limit") is not None or select.args.get("offset") is not None) and \
        select.args.get("order") is None


def _unstable(select: Any) -> bool:
    """这一段取到哪几行、取到哪一行的值 SQL 本来就没规定、库里看扫描的先后：没有 ORDER BY 的 LIMIT / OFFSET；聚合的
    一段（有 GROUP BY 或聚合函数）里既不在聚合函数里、也不是分组键（的一部分）的列（SQLite 取组里任意一行的值）。
    这样的一段来源不调换先后：调换之后扫描的先后跟着变，实际取到的行就不同了。"""
    from sqlglot import exp

    if _takes_any(select):
        return True
    group = select.args.get("group")
    keys = {_fingerprint(k) for k in (group.expressions if group is not None else [])}

    def aggregates(node: Any) -> bool:
        return any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) and not _nested(n, node)
                   for n in node.walk())

    clauses = [c for c in (*select.expressions, select.args.get("having"), select.args.get("order")) if c is not None]
    if group is None and not any(aggregates(p) for p in select.expressions):
        return False

    def loose(node: Any) -> bool:
        if isinstance(node, (exp.Query, exp.Subquery)) or _fingerprint(node) in keys:
            return False
        if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window):
            return False
        if isinstance(node, exp.Column):
            return not node.is_star
        return any(loose(c) for c in node.iter_expressions())

    return any(loose(c) for c in clauses)


def _has_star(tree: Any) -> bool:
    from sqlglot import exp

    return any(_starry(s) for s in tree.find_all(exp.Select))


def _correlated_derived(tree: Any) -> bool:
    """FROM 里的子查询、CTE 引用了外层的列（相关派生表）：写成顶层的 CTE 就断了关联，不改写。"""
    from sqlglot.optimizer.scope import traverse_scope

    return any((s.is_derived_table or s.is_cte) and s.external_columns for s in traverse_scope(tree))


def _merge_safe(tree: Any) -> bool:
    """外连接可空一侧（LEFT JOIN 关联进来的、RIGHT JOIN 之前的、FULL JOIN 两侧）的子查询、CTE，只有「直接读实表、
    原样选列」的才让 merge_subqueries 并：选出来的是常量、COALESCE、CASE 这类表达式时，没关联上的行本该整行是空值，
    并进外层后成了算出来的值；里面再套着子查询的，里层先并上来也会变成这样。"""
    from sqlglot import exp

    ctes = {c.alias_or_name: c.this for c in tree.find_all(exp.CTE)}
    for select in tree.find_all(exp.Select):
        joins = select.args.get("joins") or []
        items = _items_of(select)
        nullable: list[Any] = []
        for i, join in enumerate(joins):
            side = str(join.side or "").upper()
            if side in ("LEFT", "FULL"):
                nullable.append(join.this)
            if side in ("RIGHT", "FULL"):
                nullable += items[:i + 1]
        for item in nullable:
            body = item.this if isinstance(item, exp.Subquery) else (
                ctes.get(item.name) if isinstance(item, exp.Table) and not item.args.get("db") else None)
            if body is None:
                continue
            if not isinstance(body, exp.Select):
                return False
            if not all(isinstance(p.unalias(), exp.Column) for p in body.expressions):
                return False
            if any(not isinstance(s, exp.Table) or s.name in ctes for s in _items_of(body)):
                return False
    return True


def _alias_or_column(tree: Any, read: str | None, schema: dict[str, dict[str, str]] | None) -> bool:
    """有不带表名、又和这一段的列别名同名的名字，qualify 会把它当成别名展开，库里认的却可能是表的列，这一遍不认列：

    - WHERE、GROUP BY、HAVING 里：库里认的是列还是别名各方言不一样（SQLite 的 GROUP BY、HAVING 都先认列：HAVING
      amount > 10 里的 amount 是组里任意一行的 amount，不是 SUM(amount) AS amount），qualify 却一律当成别名（GROUP BY
      created_at 成了按 DATE(created_at) 分组）。表结构里有这一段的每张表、这个名字又不是其中哪张表的列时，它只能是
      别名，照常认。
    - SELECT 列表里引用前面列的别名：SQLite、PostgreSQL、MySQL 都不认，名字指的是表的列（SELECT user_id AS amount,
      amount * 2 里的 amount 是表的 amount 列），不是表的列就报错；qualify 没有表结构时却当成别名展开成 user_id * 2，
      有表结构时不是表的列的也展开。表结构里有这一段的每张表、这个名字又是其中的列时，qualify 先认列，照常认。"""
    from sqlglot import exp
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

    columns = {name.lower(): {c.lower() for c in cols} for name, cols in (schema or {}).items()}
    copy = normalize_identifiers(tree.copy(), dialect=read)
    for select in copy.find_all(exp.Select):
        aliases = {p.alias.lower() for p in select.expressions if isinstance(p, exp.Alias) and p.alias}
        if not aliases:
            continue
        items = _items_of(select)
        known = all(isinstance(i, exp.Table) and i.name.lower() in columns for i in items)
        real = set().union(*(columns[i.name.lower()] for i in items)) if known and items else set()
        for key in ("where", "group", "having"):
            node = select.args.get(key)
            if node is None:
                continue
            names = {c.name.lower() for c in node.find_all(exp.Column) if not c.table and not _nested(c, node)}
            if (names & aliases) - (set() if not known else (aliases - real)):
                return True
        earlier: set[str] = set()
        for projection in select.expressions:
            names = {c.name.lower() for c in projection.find_all(exp.Column)
                     if not c.table and (c is projection or not _nested(c, projection))}
            if (names & earlier) - real:
                return True
            if isinstance(projection, exp.Alias) and projection.alias:
                earlier.add(projection.alias.lower())
    return False


def _remember_names(tree: Any) -> None:
    """CTE、FROM 里的子查询原来的名字记在里面那段查询上（见 _ORIGINAL）。"""
    from sqlglot import exp

    for cte in tree.find_all(exp.CTE):
        cte.this.meta.setdefault(_ORIGINAL, cte.alias_or_name)
    for sub in tree.find_all(exp.Subquery):
        if isinstance(sub.parent, (exp.From, exp.Join)) and sub.alias:
            sub.this.meta.setdefault(_ORIGINAL, sub.alias)


def _inline_ctes(tree: Any) -> None:
    """（轻量一级）只引用一次、在 FROM / JOIN 里引用的非递归顶层 CTE 写回引用它的地方，成为 FROM 里的子查询：写成
    CTE 还是子查询是同一个意思。CTE 自己写了列名的、语句里还有别的 WITH 的（名字可能被遮住）不动。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None or with_.args.get("recursive"):
        return
    if any(isinstance(n, exp.With) for n in tree.walk() if n is not with_):
        return
    changed = True
    while changed:
        changed = False
        for cte in list(with_.expressions):
            name = cte.alias_or_name
            if cte.args["alias"].columns:
                continue
            refs = [t for t in tree.find_all(exp.Table) if t.name == name and not t.args.get("db")
                    and isinstance(t.this, exp.Identifier)]
            if len(refs) != 1 or refs[0].find_ancestor(exp.CTE) is cte or \
                    not isinstance(refs[0].parent, (exp.From, exp.Join)):
                continue
            ref = refs[0]
            body = cte.this.copy()
            sub = exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(ref.alias or name)))
            ref.replace(sub)
            cte.pop()
            changed = True
            break
    if not with_.expressions:
        tree.set("with_", None)


def _unquote(tree: Any, read: str | None) -> None:
    """不需要的引号去掉：按方言的规则，不加引号时认成的还是同一个名字（PostgreSQL 的 "orders" 就是 orders，"Orders"
    不是）。最后统一引号时需要的再加回来（见 _named）。"""
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect

    dialect = Dialect.get_or_raise(read)
    for ident in tree.find_all(exp.Identifier):
        if ident.quoted and isinstance(ident.this, str):
            bare = exp.Identifier(this=ident.this, quoted=False)
            if dialect.normalize_identifier(bare).this == ident.this:
                ident.set("quoted", False)


#: 括号里是这些（运算符的一部分）时，外面是比较运算的话括号可以去掉：算术比比较结合得紧
_ARITHMETIC = ("Add", "Sub", "Mul", "Div", "Mod")
_COMPARISON = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE")


def _strip_parens(tree: Any) -> None:
    """不影响结合的括号去掉：括号外面不是运算符（函数参数、别名、SELECT 列表、ORDER BY、GROUP BY、WHERE 整个条件、
    IN 列表的一项……）、括号里是一列一个常量一次函数调用，或者括号里是算术、外面是比较。"""
    from sqlglot import exp

    for paren in list(tree.find_all(exp.Paren)):
        inner, parent = paren.this, paren.parent
        if inner is None or parent is None or isinstance(inner, (exp.Query, exp.Subquery, exp.Tuple)):
            continue
        atomic = isinstance(inner, (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Paren)) or (
            isinstance(inner, exp.Func) and not isinstance(inner, (exp.Binary, exp.Unary, exp.Predicate)))
        outside = isinstance(parent, (exp.Alias, exp.Distinct, exp.Select, exp.Ordered, exp.Where, exp.Having,
                                      exp.Group, exp.Window)) or (
            isinstance(parent, exp.Func) and not isinstance(parent, (exp.Binary, exp.Unary, exp.Predicate)))
        loose = type(inner).__name__ in _ARITHMETIC and type(parent).__name__ in _COMPARISON
        if isinstance(parent, exp.In) and paren.arg_key == "expressions":
            outside = True
        if atomic or outside or loose:
            paren.replace(inner)


def _zero_offset(tree: Any) -> None:
    from sqlglot import exp

    for offset in list(tree.find_all(exp.Offset)):
        value = offset.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) == 0:
            offset.pop()


def _redundant_distinct(tree: Any, read: str | None) -> None:
    """已经按选出来的列分组（每个分组键都原样选出来了）的 SELECT DISTINCT：每组一行、各行的分组键都不同，去重
    去不掉什么。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        distinct, group = select.args.get("distinct"), select.args.get("group")
        if distinct is None or distinct.args.get("on") or group is None or not group.expressions or \
                any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            continue
        if any(p.find(exp.Window) for p in select.expressions):
            continue
        selected = {_fingerprint(p.unalias()) for p in select.expressions}
        if all(_fingerprint(k) in selected for k in group.expressions):
            select.set("distinct", None)


def _narrow_stars(tree: Any, read: str | None) -> None:
    """没给表结构时 SELECT * 展不开：从一张实表 SELECT * 的 CTE、FROM 里的子查询（不去重、不分组、不取行、没有窗口
    函数），外面只按名字引用其中几列的，* 换成用到的那几列（外面有 SELECT * 或 t.* 读它的不换）。不去重时一段出几行
    和选几列无关，外面用不到的列不影响结果；用到的列那张表里没有的话，原文本来就出错。"""
    from sqlglot import exp

    namer = _Namer(tree, read, qualified=True)
    ctes = {c.alias_or_name: c for c in tree.find_all(exp.CTE)}
    starred: set[int] = set()
    for select in tree.find_all(exp.Select):
        for p in select.expressions:
            if isinstance(p, exp.Star):
                starred |= {id(i) for i in _items_of(select)}
            elif isinstance(p, exp.Column) and p.is_star:
                starred |= {id(i) for i in _items_of(select) if i.alias_or_name == p.table}
    bodies: list[tuple[Any, list[Any]]] = []
    for cte in ctes.values():
        refs = [t for s in tree.find_all(exp.Select) for t in _items_of(s)
                if isinstance(t, exp.Table) and namer.cte_of.get(id(t)) is cte]
        bodies.append((cte.this, refs))
    for sub in tree.find_all(exp.Subquery):
        if isinstance(sub.parent, (exp.From, exp.Join)):
            bodies.append((sub.this, [sub]))
    for body, refs in bodies:
        if not isinstance(body, exp.Select) or len(body.expressions) != 1 or \
                not isinstance(body.expressions[0], exp.Star) or not _simple_body(body):
            continue
        items = _items_of(body)
        if len(items) != 1 or not isinstance(items[0], exp.Table) or id(items[0]) in namer.cte_of or \
                any(id(r) in starred for r in refs):
            continue
        names = sorted({col.name for r in refs for col in namer.refs.get(id(r), [])})
        alias = items[0].alias_or_name
        body.set("expressions", [exp.alias_(exp.column(n, table=alias), n, copy=False) for n in names] or
                 [exp.alias_(exp.Literal.number(1), "_one", copy=False)])


def _unique_aliases(tree: Any, read: str | None) -> None:
    """整条语句里每个来源（实表、CTE 的引用）的别名改成互不相同的（表名__序号），引用它的列跟着改：按原来的别名
    认清每一列属于哪个来源之后再改（见 _Namer），相关子查询里引用外层的列不会认错。"""
    from sqlglot import exp

    namer = _Namer(tree, read, qualified=True)
    taken = {t.name for t in tree.find_all(exp.Table)} | {c.alias_or_name for c in tree.find_all(exp.CTE)}
    n = 0
    for item in [i for s in tree.find_all(exp.Select) for i in _items_of(s)]:
        n += 1
        name = f"{item.name if isinstance(item, exp.Table) else 'sub'}__{n}"
        while name in taken:
            name += "_"
        alias = item.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            alias.set("this", exp.to_identifier(name))
        else:
            item.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
        for col in namer.refs.get(id(item), []):
            col.set("table", exp.to_identifier(name))


def _cte_refs(tree: Any, name: str) -> list[Any]:
    """引用顶层 CTE name 的表（没有被里层同名的 CTE 遮住的）。"""
    from sqlglot import exp

    out = []
    for table in tree.find_all(exp.Table):
        if table.name != name or table.args.get("db") or not isinstance(table.this, exp.Identifier):
            continue
        shadowed = False
        node = table.parent
        while node is not None and node is not tree:
            with_ = node.args.get("with_") if isinstance(node, exp.Query) else None
            if with_ is not None and any(c.alias_or_name == name for c in with_.expressions):
                shadowed = True
                break
            node = node.parent
        if not shadowed:
            out.append(table)
    return out


def _simple_body(body: Any) -> bool:
    """只是从来源里筛行、算列的一段（没有分组、聚合、去重、取行、窗口函数、UNION）：在哪儿算、算几遍结果都一样。"""
    from sqlglot import exp

    if not isinstance(body, exp.Select) or body.args.get("with_") is not None:
        return False
    if any(body.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "windows")):
        return False
    return not any(isinstance(n, (exp.AggFunc, exp.Window)) for p in body.expressions for n in p.walk())


def _split_simple_ctes(tree: Any) -> None:
    """引用了好几次的「简单」CTE（见 _simple_body）每处引用各复制一份：merge_subqueries 只并只引用一次的 CTE，
    复制之后每处都能并进去。CTE 是确定的（有不确定函数的语句不走这一级），算几遍结果一样。并不进去的几份定义
    一模一样，起名时又合回一个（见 _Namer._name_ctes）。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None:
        return
    taken = {c.alias_or_name for c in with_.expressions} | {t.name for t in tree.find_all(exp.Table)}
    for cte in list(with_.expressions):
        name = cte.alias_or_name
        refs = _cte_refs(tree, name)
        if len(refs) < 2 or cte.args["alias"].columns or not _simple_body(cte.this):
            continue
        position = with_.expressions.index(cte)
        for i, ref in enumerate(refs[1:], 2):
            copy_name = f"{name}_{i}"
            while copy_name in taken:
                copy_name += "_"
            taken.add(copy_name)
            twin = exp.CTE(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(copy_name)))
            with_.expressions.insert(position + i - 1, twin)
            twin.parent = with_
            alias = ref.alias_or_name
            ref.set("this", exp.to_identifier(copy_name))
            if not ref.alias:
                ref.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        with_.set("expressions", list(with_.expressions))


def _push_into_groups(tree: Any) -> None:
    """外层 WHERE 里只涉及一个分组 CTE（只引用一次、在开头那组内连接里）的条件，换成 CTE 里对应的表达式，挪进它的
    HAVING：每一行就是一组，筛行和筛组是一回事。CTE 有窗口函数、取行、去重、没写成分组键也没聚合的列时不挪。
    然后（各段都做）HAVING 里只涉及分组键、不含聚合的条件挪进 WHERE：同一组的分组键都一样，先筛后筛一样。两步
    之后，这类条件写在里层 WHERE、HAVING 还是外层 WHERE 都落到同一处。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    ctes = {c.alias_or_name: c for c in (with_.expressions if with_ is not None else [])}
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        joins = select.args.get("joins") or []
        if where is None or select.args.get("from_") is None or any(not _inner(j) for j in joins):
            continue
        for item in _items_of(select):
            cte = ctes.get(item.name) if isinstance(item, exp.Table) and not item.args.get("db") else None
            if cte is None or len(_cte_refs(tree, item.name)) != 1:
                continue
            body = cte.this
            if not isinstance(body, exp.Select) or body.args.get("group") is None or \
                    any(body.args.get(k) for k in ("limit", "offset", "distinct", "qualify", "windows")) or \
                    any(n.find(exp.Window) for n in body.expressions):
                continue
            outputs = {p.alias_or_name: p.unalias() for p in body.expressions}
            keys = {_fingerprint(k) for k in body.args["group"].expressions}
            alias = item.alias_or_name
            moved, kept = [], []
            for cond in _conjuncts(where.this):
                cols = list(cond.find_all(exp.Column))
                if not cols or cond.find(exp.Select, exp.Subquery) is not None or \
                        any(c.table != alias or c.name not in outputs for c in cols) or \
                        not all(_grouped(outputs[c.name], keys) for c in cols):
                    kept.append(cond)
                    continue
                new = cond.copy()
                for col in list(new.find_all(exp.Column)):
                    col.replace(exp.paren(outputs[col.name].copy(), copy=False))
                moved.append(new)
            if moved:
                having = body.args.get("having")
                body.set("having", exp.Having(this=_and_all(([having.this] if having else []) + moved)))
                select.set("where", exp.Where(this=_and_all(kept)) if kept else None)
                where = select.args.get("where")
                if where is None:
                    break
    for select in list(tree.find_all(exp.Select)):
        having, group = select.args.get("having"), select.args.get("group")
        if having is None or group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            continue
        keys = {_fingerprint(k) for k in group.expressions if isinstance(k, exp.Column)}
        moved, kept = [], []
        for cond in _conjuncts(having.this):
            cols = list(cond.find_all(exp.Column))
            if cols and all(_fingerprint(c) in keys for c in cols) and not any(
                    isinstance(n, (exp.AggFunc, exp.Window, exp.Select, exp.Subquery)) for n in cond.walk()):
                moved.append(cond)
            else:
                kept.append(cond)
        if moved:
            where = select.args.get("where")
            select.set("where", exp.Where(this=_and_all(([where.this] if where else []) + moved)))
            select.set("having", exp.Having(this=_and_all(kept)) if kept else None)


def _grouped(expression: Any, keys: set[str]) -> bool:
    """分组 CTE 选出来的一列在每组里只有一个值：整个是分组键，或者每一列都在聚合函数里、或者本身是分组键。"""
    from sqlglot import exp

    if _fingerprint(expression) in keys:
        return True
    for col in expression.find_all(exp.Column):
        if _fingerprint(col) not in keys and col.find_ancestor(exp.AggFunc) is None:
            return False
    return not any(isinstance(n, exp.Window) for n in expression.walk())


def _unwrap_passthrough(tree: Any, read: str | None) -> Any:
    """原样选出一个子查询（FROM 里的子查询，或只引用一次的 CTE）全部列、什么别的都不做的一段（SELECT t.a, t.b
    FROM (…) t），换成那个子查询：出的行一样，列的先后、名字也一样。最外层和条件里的子查询列名不影响取值，列名
    可以不同；别的地方（FROM 里的子查询、CTE）外面按名字引用，名字也得一样。

    子查询是 UNION 这类合并的，SQLite（和不知道是哪种库时）不换：SQLite 从子查询里读合并的结果时，按第一支那一列的
    类型亲和转换别的支的值（REAL 列在前，另一支的整数 2 读出来成了 2.0，像数字的文本也会被转成数），套不套这一层
    结果不一样。"""
    from sqlglot import exp

    def outputs(query: Any) -> list[str] | None:
        if isinstance(query, exp.SetOperation) and read not in (None, "sqlite"):
            leaves = _leaves(query)
            return outputs(leaves[0]) if leaves else None
        if not isinstance(query, exp.Select) or _starry(query):
            return None
        names = [p.alias_or_name for p in query.expressions]
        return names if all(names) and len(set(names)) == len(names) else None

    changed = True
    while changed:
        changed = False
        for select in list(tree.find_all(exp.Select)):
            if select.args.get("joins") or select.args.get("from_") is None or any(
                    select.args.get(k) for k in ("where", "group", "having", "order", "limit", "offset", "distinct",
                                                 "qualify", "windows", "hint")):
                continue
            item = select.args["from_"].this
            with_ = tree.args.get("with_")
            cte = None
            if isinstance(item, exp.Subquery):
                body = item.this
            elif isinstance(item, exp.Table) and with_ is not None and not item.args.get("db"):
                cte = next((c for c in with_.expressions if c.alias_or_name == item.name), None)
                if cte is None or len(_cte_refs(tree, item.name)) != 1 or select.find_ancestor(exp.CTE) is cte:
                    continue
                body = cte.this
            else:
                continue
            names = outputs(body)
            alias = item.alias_or_name
            cols = [p.unalias() for p in select.expressions]
            if names is None or len(cols) != len(names) or body.args.get("with_") is not None or any(
                    not isinstance(c, exp.Column) or c.name != n or (c.table and c.table != alias)
                    for c, n in zip(cols, names)):
                continue
            renamed = any(p.alias_or_name != n for p, n in zip(select.expressions, names))
            parent = select.parent
            loose = select is tree or isinstance(parent, (exp.In, exp.Exists)) or (
                isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.From, exp.Join, exp.CTE)))
            if renamed and not loose:
                continue
            new = body.copy()
            if select is tree:
                if select.args.get("with_") is not None:
                    new.set("with_", select.args["with_"])
                tree = new
            else:
                select.replace(new)
            if cte is not None:
                cte.pop()
                with_ = tree.args.get("with_")
                if with_ is not None and not with_.expressions:
                    tree.set("with_", None)
            changed = True
            break
    return tree


# ---- 和名字无关的改写

def _items_of(select: Any) -> list[Any]:
    first = select.args.get("from_")
    return ([first.this] if first is not None else []) + [j.this for j in select.args.get("joins") or []]


def _and_all(items: list[Any]) -> Any:
    from sqlglot import exp

    if not items:
        return None
    return exp.and_(*items, copy=False) if len(items) > 1 else items[0]


def _plain_join(join: Any) -> bool:
    return not (join.args.get("using") or join.args.get("method") or join.args.get("natural"))


def _comma(join: Any) -> bool:
    """开头那组内连接里的一项：逗号连接（或者没有条件的 CROSS JOIN，sqlglot 把逗号连接也读成 CROSS），条件都在 WHERE
    里（见 _flatten_inner）。规范形式里写成逗号连接：SQLite 不会调换 CROSS JOIN 的先后，按它执行规范形式会很慢。"""
    return _plain_join(join) and not join.side and str(join.kind or "").upper() in ("", "CROSS") and \
        join.args.get("on") is None


def _inner(join: Any) -> bool:
    return _plain_join(join) and not join.side and str(join.kind or "").upper() in ("", "INNER", "CROSS")


def _constant(node: Any) -> bool:
    from sqlglot import exp

    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean)) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal))


def _strip_asc(tree: Any) -> None:
    from sqlglot import exp

    for o in tree.find_all(exp.Ordered):
        if o.args.get("desc") is False:
            o.set("desc", None)


def _count_star(tree: Any) -> None:
    """COUNT(1)、COUNT('x')：数的是不为空的常量，就是行数。COUNT(DISTINCT 1) 不是（有行就是 1）。"""
    from sqlglot import exp

    for c in list(tree.find_all(exp.Count)):
        if isinstance(c.this, exp.Literal) and not c.args.get("distinct"):
            c.set("this", exp.Star())


def _in_lists(tree: Any, read: str | None) -> None:
    """IN 列表是个集合：去重、按写法排序；只有一项的写成 =（NOT IN 跟着成了 <>，空值的结果一样）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.In)):
        if not node.expressions or node.args.get("query") or node.args.get("unnest") or node.args.get("field"):
            continue
        uniq = {_key(i, read): i for i in node.expressions}
        ordered = [uniq[k] for k in sorted(uniq)]
        if len(ordered) == 1:
            node.replace(exp.EQ(this=node.this, expression=ordered[0]))
        else:
            node.set("expressions", ordered)


def _aggregate_free(select: Any) -> bool:
    from sqlglot import exp

    return not any(isinstance(n, exp.AggFunc) and not n.find_ancestor(exp.Window)
                   for p in select.expressions for n in p.walk())


def _exists_projection(tree: Any) -> None:
    """EXISTS 只看有没有行：选什么、去不去重、怎么排（取行也只看行数）都不影响，统一写成 SELECT 1。没有 GROUP BY
    却有聚合（总出一行）的、有 HAVING 的（HAVING、GROUP BY 1 可能引用选出来的列）不动。"""
    from sqlglot import exp

    for ex in list(tree.find_all(exp.Exists)):
        sub = ex.this.this if isinstance(ex.this, exp.Subquery) else ex.this
        if not isinstance(sub, exp.Select) or sub.args.get("having") or \
                (not sub.args.get("group") and not _aggregate_free(sub)):
            continue
        distinct = sub.args.get("distinct")
        if distinct is not None and not distinct.args.get("on"):
            sub.set("distinct", None)
        sub.set("expressions", [exp.Literal.number(1)])
        sub.set("order", None)


def _in_subquery_distinct(tree: Any) -> None:
    """IN (子查询) 只看取值的集合：里面的 DISTINCT、不取行时的 ORDER BY 不影响（NOT IN 也一样，空值还在）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        sub = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(sub, exp.Select) or any(sub.args.get(k) for k in ("limit", "offset", "fetch")):
            continue
        sub.set("order", None)
        distinct = sub.args.get("distinct")
        if distinct is not None and not distinct.args.get("on") and not sub.args.get("group") and _aggregate_free(sub):
            sub.set("distinct", None)


def _distinct_to_group(tree: Any, read: str | None) -> None:
    """SELECT DISTINCT a, b（没有分组、聚合、窗口函数）和 GROUP BY a, b 出的行一样（空值也算一组）。ORDER BY 用到
    没选出来的列时不改（两种写法对这种排序的规矩不一样）；没有 ORDER BY 的 LIMIT 不改（DISTINCT 按扫到的先后出行，
    GROUP BY 按分组键排好了出，取到的行不同）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") or select.args.get("group") or select.args.get("having"):
            continue
        if _starry(select) or _takes_any(select) or not _aggregate_free(select) or \
                any(p.find(exp.Window, exp.Select, exp.Subquery) for p in select.expressions):
            continue
        keys = [p.unalias().copy() for p in select.expressions]
        if any(_constant(k) for k in keys):
            continue
        order = select.args.get("order")
        if order is not None:
            allowed = {_fingerprint(k) for k in keys}
            aliases = {p.alias for p in select.expressions if p.alias}
            if any(_fingerprint(o.this) not in allowed and not (
                    isinstance(o.this, exp.Column) and not o.this.table and o.this.name in aliases)
                   for o in order.expressions):
                continue
        group = exp.Group(expressions=keys)
        group.meta[_FROM_DISTINCT] = True
        select.set("distinct", None)
        select.set("group", group)


def _right_to_left(tree: Any) -> None:
    """只有两个来源的 a RIGHT JOIN b ON p 就是 b LEFT JOIN a ON p。选的是 * 时列的先后跟着变、没有 ORDER BY 的 LIMIT
    取先扫到的几行，不改。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if len(joins) != 1 or str(joins[0].side or "").upper() != "RIGHT" or not _plain_join(joins[0]) or \
                _starry(select) or _takes_any(select):
            continue
        first = select.args["from_"]
        left, right = first.this, joins[0].this
        first.set("this", right.copy())
        joins[0].set("this", left.copy())
        joins[0].set("side", "LEFT")
        joins[0].set("kind", None)
        joins[0].meta[_FLIPPED] = True
    for join in tree.find_all(exp.Join):
        if join.side and str(join.kind or "").upper() == "OUTER":
            join.set("kind", None)


def _null_propagating(node: Any, alias: str) -> bool:
    """alias 那一侧整行是空值时，node 一定是空值：那一侧的列，或者对它做比较、算术、取大小写、转类型。"""
    from sqlglot import exp

    if isinstance(node, exp.Column):
        return bool(node.table) and node.table == alias
    if type(node).__name__ in _STRICT_UNARY:
        return _null_propagating(node.this, alias)
    if type(node).__name__ in _STRICT_BINARY:
        return _null_propagating(node.left, alias) or _null_propagating(node.right, alias)
    return False


def _rejects(cond: Any, alias: str) -> bool:
    """alias 那一侧整行是空值时 cond 不成立（是空值或假）：比较、LIKE、IN、BETWEEN 的一边一定是空值，IS NOT NULL，
    或者 AND 里有一项不成立、OR 里每一项都不成立。IS NULL、COALESCE、CASE 不算。"""
    from sqlglot import exp

    if isinstance(cond, exp.Paren):
        return _rejects(cond.this, alias)
    if isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)):
        return _null_propagating(cond.left, alias) or _null_propagating(cond.right, alias)
    if isinstance(cond, (exp.In, exp.Between)):
        return _null_propagating(cond.this, alias)
    if isinstance(cond, exp.Not):
        inner = cond.this.unnest() if isinstance(cond.this, exp.Paren) else cond.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return _null_propagating(inner.this, alias)
        if isinstance(inner, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)):
            return _null_propagating(inner.left, alias) or _null_propagating(inner.right, alias)
        return False
    if isinstance(cond, exp.And):
        return _rejects(cond.left, alias) or _rejects(cond.right, alias)
    if isinstance(cond, exp.Or):
        return _rejects(cond.left, alias) and _rejects(cond.right, alias)
    return False


def _reject_nulls(tree: Any) -> None:
    """LEFT JOIN 关联进来的那一侧，同一段 WHERE 里有把它整行为空的行筛掉的条件（见 _rejects）：没关联上的行反正留
    不下来，这次外连接就是内连接。有 RIGHT / FULL JOIN 的段不动。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        where = select.args.get("where")
        if where is None or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        conds = _conjuncts(where.this)
        for join in joins:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join):
                continue
            alias = join.this.alias_or_name
            if alias and any(_rejects(c, alias) for c in conds):
                join.set("side", None)
                join.set("kind", None)
                join.this.meta[_REJECTED] = "RIGHT JOIN" if join.meta.get(_FLIPPED) else "LEFT JOIN"


def _flatten_inner(tree: Any) -> None:
    """内连接的 ON 条件挪进 WHERE：开头连续的内连接（和后面只引用它们的内连接）是一组来源，写成逗号连接，条件写在
    ON 还是 WHERE 都一样，来源的先后也不影响结果（后面的 LEFT JOIN 只保留左边的行，左边先筛后筛一样）。有 RIGHT /
    FULL JOIN、USING、NATURAL 的段不动；引用了 LEFT JOIN 进来的表的内连接留在原处。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins or select.args.get("from_") is None:
            continue
        if any(not _plain_join(j) or str(j.side or "").upper() in ("RIGHT", "FULL") or
               str(j.kind or "").upper() in ("SEMI", "ANTI", "ASOF") for j in joins):
            continue
        prefix = [select.args["from_"].this]
        lefts: list[Any] = []
        tail: list[Any] = []
        moved: list[Any] = []
        for join in joins:
            if tail:
                tail.append(join)
                continue
            if _inner(join):
                on = join.args.get("on")
                refs = {c.table for c in (on.find_all(exp.Column) if on is not None else []) if c.table}
                if refs & {j.this.alias_or_name for j in lefts}:
                    tail.append(join)
                    continue
                prefix.append(join.this)
                moved += _conjuncts(on) if on is not None else []
            else:
                lefts.append(join)
        select.args["from_"].set("this", prefix[0])
        select.set("joins", [exp.Join(this=s) for s in prefix[1:]] + lefts + tail)
        if moved:
            where = select.args.get("where")
            select.set("where", exp.Where(this=_and_all((_conjuncts(where.this) if where else []) + moved)))


def _order_by_aliases(tree: Any) -> None:
    """最外层 ORDER BY 引用 SELECT 的别名的，换成那个表达式：别名接下来要去掉。不换的：别名指的是数字常量（ORDER BY 1
    是按第一列排）；别名指的是一个不带表名、又和某个别名同名的列（ORDER BY 里单独一个名字先认别名：SELECT user_id AS
    amount, amount AS user_id … ORDER BY amount 换成 ORDER BY user_id 就成了按第二列排）。"""
    from sqlglot import exp

    if not isinstance(tree, exp.Select) or tree.args.get("order") is None:
        return
    aliases = {p.alias: p for p in tree.expressions if p.alias}
    for o in tree.args["order"].expressions:
        key = o.this
        if not (isinstance(key, exp.Column) and not key.table and key.name in aliases):
            continue
        target = aliases[key.name].unalias()
        if target.is_number or (isinstance(target, exp.Column) and not target.table and target.name in aliases):
            continue
        o.set("this", target.copy())


def _drop_aliases(query: Any) -> None:
    from sqlglot import exp

    if not isinstance(query, exp.Select):
        return
    inside = {id(n) for p in query.expressions for n in p.walk()}
    used = {n.name for n in _block_nodes(query) if isinstance(n, exp.Column) and not n.table and id(n) not in inside}
    query.set("expressions", [p.unalias() if isinstance(p, exp.Alias) and p.alias not in used else p
                              for p in query.expressions])


def _drop_output_aliases(tree: Any) -> None:
    """最外层（不是 UNION 时）和条件里的子查询选出来的列名不影响任何取值，去掉；本段里还按名字引用的留着。"""
    from sqlglot import exp

    if isinstance(tree, exp.SetOperation):
        if tree.args.get("order") is None:
            for leaf in _leaves(tree):
                _drop_aliases(leaf)
    else:
        _drop_aliases(tree)
    for node in tree.find_all(exp.Exists, exp.In):
        query = node.this if isinstance(node, exp.Exists) else node.args.get("query")
        _drop_aliases(query.this if isinstance(query, exp.Subquery) else query)
    for sub in tree.find_all(exp.Subquery):
        if not isinstance(sub.parent, (exp.From, exp.Join, exp.Exists, exp.In, exp.CTE)) and not sub.alias:
            _drop_aliases(sub.this)


def _leaves(query: Any) -> list[Any]:
    """一棵 UNION / INTERSECT / EXCEPT 树的各支（套着括号的也拆开）。"""
    from sqlglot import exp

    if isinstance(query, exp.Subquery):
        return _leaves(query.this)
    if isinstance(query, exp.SetOperation):
        return _leaves(query.left) + _leaves(query.right)
    return [query]


def _block_nodes(select: Any) -> Iterator[Any]:
    """这一段自己的节点：不进套着的查询；FROM / JOIN 的来源不算（ON 条件算）。"""
    from sqlglot import exp

    stack = [v for k, value in select.args.items() if k not in ("with_", "from_")
             for v in (value if isinstance(value, list) else [value]) if isinstance(v, exp.Expr)]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Query, exp.Subquery)):
            continue
        if isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append(node.args["on"])
            continue
        yield node
        stack += [v for value in node.args.values() for v in (value if isinstance(value, list) else [value])
                  if isinstance(v, exp.Expr)]


def _nested_queries(select: Any) -> list[Any]:
    """这一段自己的表达式里直接套着的查询（条件、SELECT 列表、ON 里的子查询），不含 FROM 里的子查询。"""
    from sqlglot import exp

    out: list[Any] = []
    stack = [v for k, value in select.args.items() if k not in ("with_", "from_")
             for v in (value if isinstance(value, list) else [value]) if isinstance(v, exp.Expr)]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            out.append(node.this)
        elif isinstance(node, exp.Query):
            out.append(node)
        elif isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append(node.args["on"])
        else:
            stack += [v for value in node.args.values() for v in (value if isinstance(value, list) else [value])
                      if isinstance(v, exp.Expr)]
    return out


# ---- 化简：sqlglot 的 simplify 去掉和类型、排序规则、方言有关的几种

def _simplify(tree: Any, read: str | None) -> Any:
    """sqlglot 的 simplify（AND / OR 去重排序、吸收律、德摩根、BETWEEN 拆开、常量折叠、比较的方向……），去掉三种：

    - 同一列上的几个比较合成一个（x > 5 AND x > 3 → x > 5）、字符串常量之间的比较：结果取决于列的类型和排序规则
      （SQLite 里 TEXT 列和数字比是按文本比，PostgreSQL 的语言排序规则下 'a' < 'B'）；
    - 等式两边移项（x + 1 = 3 → x = 2）：TEXT 列在 SQLite 里不成立；
    - DATE_TRUNC 比较改成区间：取决于方言的日期语义。

    另外两边都写了 COLLATE 的比较不对调两边（SQLite 用左边那个排序规则）。
    """
    from sqlglot import exp
    from sqlglot.optimizer.simplify import Simplifier

    class _Safe(Simplifier):
        def _simplify_comparison(self, expression, left, right, or_=False):  # noqa: ANN001
            return None

        def sort_comparison(self, expression):  # noqa: ANN001
            if isinstance(expression, exp.Binary) and expression.this.find(exp.Collate) is not None and \
                    expression.expression.find(exp.Collate) is not None:
                return expression
            return super().sort_comparison(expression)

        def simplify_equality(self, expression):  # noqa: ANN001
            return expression

        def simplify_datetrunc(self, expression):  # noqa: ANN001
            return expression

        def _simplify_binary(self, expression, a, b):  # noqa: ANN001
            if isinstance(expression, self.COMPARISONS) and not isinstance(expression, exp.Is) and \
                    not (a.is_number and b.is_number):
                return None
            return super()._simplify_binary(expression, a, b)

    return _Safe(dialect=read).simplify(tree)


# ---- 起规范的名字

@dataclass
class _Hint:
    """按另一边起名（差异说明用，见 _Explainer.statement）：定义一样的 CTE 叫另一边的名字；names 是这一边原来的
    CTE 名 → 另一边的名字（两边各剩一个配不上的 CTE 时配成一对）；taken 是另一边用过的名字，配不上的另起名字时
    避开。"""

    bodies: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    taken: set[str] = field(default_factory=set)


class _Namer:
    """给一条语句起规范的名字（见 _finish 第 3 步）。

    先按原来的别名认清每一列属于哪个来源（相关子查询里的列到外层找），记成「来源 → 引用它的列」；之后改来源的别名
    时把这些列一起改掉，不再按名字找，换名不会把列认错。别名带层数：内外层的别名不会同名，相关子查询里引用外层的
    列不会被里层遮住；同一层的兄弟子查询各自看不见对方，可以重名。"""

    def __init__(self, tree: Any, read: str | None, qualified: bool, hint: _Hint | None = None) -> None:
        from sqlglot import exp

        self.tree = tree
        self.read = read
        self.qualified = qualified
        self.hint = hint
        self.refs: dict[int, list[Any]] = {}
        self.cte_of: dict[int, Any] = {}
        self.base_names = {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
        self.budget = _ORDERING_BUDGET
        self._resolve(tree, [], {})

    def _text(self, node: Any) -> tuple[str, str]:
        return _key(node, self.read)

    def _resolve(self, query: Any, frames: list[dict[str, Any]], ctes: dict[str, Any]) -> None:
        from sqlglot import exp

        if isinstance(query, exp.Subquery):
            self._resolve(query.this, frames, ctes)
            return
        with_ = query.args.get("with_")
        if with_ is not None:
            ctes = dict(ctes)
            for cte in with_.expressions:
                self._resolve(cte.this, frames, ctes)
                ctes[cte.alias_or_name] = cte
        if isinstance(query, exp.SetOperation):
            self._resolve(query.left, frames, ctes)
            self._resolve(query.right, frames, ctes)
            return
        if not isinstance(query, exp.Select):
            raise _Unsupported(type(query).__name__)
        frame: dict[str, Any] = {}
        for item in _items_of(query):
            if isinstance(item, exp.Subquery):
                self._resolve(item.this, frames, ctes)
            elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
                if not item.args.get("db") and item.name in ctes:
                    self.cte_of[id(item)] = ctes[item.name]
            else:
                raise _Unsupported(type(item).__name__)
            name = item.alias_or_name
            if not name or name in frame:
                raise _Unsupported("来源没有名字或重名")
            frame[name] = item
        stack = frames + [frame]
        for node in _block_nodes(query):
            if isinstance(node, exp.Column) and node.table:
                for f in reversed(stack):
                    if node.table in f:
                        self.refs.setdefault(id(f[node.table]), []).append(node)
                        break
        for sub in _nested_queries(query):
            self._resolve(sub, stack, ctes)

    def _alias(self, base: str, depth: int, k: int) -> str:
        name = base if depth == 0 else f"{base}@{depth}"
        if k > 1:
            name = f"{name}#{k}"
        while (depth or k > 1) and name.lower() in self.base_names:
            name += "#"
        return name

    def canon(self, query: Any, depth: int, cte_names: dict[int, str]) -> tuple[str, str]:
        from sqlglot import exp

        if isinstance(query, exp.Subquery):
            return self.canon(query.this, depth, cte_names)
        with_ = query.args.get("with_")
        if with_ is not None:
            cte_names = dict(cte_names)
            self._name_ctes(with_, depth, cte_names)
        if isinstance(query, exp.SetOperation):
            self._canon_setop(query, depth, cte_names)
            return self._text(query)
        return self._canon_select(query, depth, cte_names)

    def _name_ctes(self, with_: Any, depth: int, cte_names: dict[int, str]) -> None:
        """CTE 按依赖分层，同一层按规范化后的定义排序起名；定义一模一样的合成一个（引用它们的都改叫同一个名字）。"""
        from sqlglot import exp

        hint = self.hint if depth == 0 else None
        pending = list(with_.expressions)
        kept: list[Any] = []
        by_text: dict[tuple[str, str], str] = {}
        prefix = "cte" if depth == 0 else f"cte@{depth}_"
        n = 0
        while pending:
            names = {c.alias_or_name for c in pending}
            ready = [c for c in pending if not any(t.name in names and t.name != c.alias_or_name and not t.args.get("db")
                                                   for t in c.this.find_all(exp.Table))]
            if not ready:
                raise _Unsupported("CTE 互相引用")
            texts = []
            for cte in ready:
                original = cte.alias_or_name
                body = self.canon(cte.this, depth + 1 if depth else 0, cte_names)
                if self.qualified and self._rename_columns(cte):
                    body = self._text(cte.this)
                texts.append((body, original, cte))
            texts.sort(key=lambda x: x[0])
            for body, original, cte in texts:
                pending.remove(cte)
                if body in by_text:
                    cte_names[id(cte)] = by_text[body]
                    continue
                name = (hint.names.get(original) or hint.bodies.get(body[1])) if hint else None
                while name is None or name.lower() in self.base_names or name in by_text.values():
                    n += 1
                    name = f"{prefix}{n}"
                    if hint and name in hint.taken:
                        name = None
                cte_names[id(cte)] = name
                by_text[body] = name
                cte.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
                kept.append(cte)
        with_.set("expressions", kept)

    def _rename_columns(self, cte: Any) -> bool:
        """CTE 选出来的列按定义排序后叫 c1、c2…，引用它们的列跟着改：外面按名字引用，名字是各自起的（total 还是
        sum_amt），先后也不影响。CTE 自己写了列名、是 UNION、列名重复、本段里还按名字引用自己的列的不改。"""
        from sqlglot import exp

        body = cte.this
        if not isinstance(body, exp.Select) or cte.args["alias"].columns:
            return False
        projections = body.expressions
        names = [p.alias_or_name for p in projections]
        if not projections or any(not n or isinstance(p, exp.Star) for n, p in zip(names, projections)) or \
                len(set(names)) != len(names):
            return False
        inside = {id(n) for p in projections for n in p.walk()}
        if {n.name for n in _block_nodes(body) if isinstance(n, exp.Column) and not n.table and id(n) not in inside} & \
                set(names):
            return False
        # GROUP BY 2、ORDER BY 1 按位置引用选出来的列：重排之后就指到别的列上了
        for clause in ("group", "order"):
            node = body.args.get(clause)
            keys = [k.this if isinstance(k, exp.Ordered) else k for k in (node.expressions if node is not None else [])]
            if any(k.is_number for k in keys):
                return False
        refs = [t for t in self.tree.find_all(exp.Table) if self.cte_of.get(id(t)) is cte]
        ordered = sorted(projections, key=lambda p: self._text(p.unalias()))
        mapping = {p.alias_or_name: f"c{i}" for i, p in enumerate(ordered, 1)}
        body.set("expressions", [exp.alias_(p.unalias().copy(), mapping[p.alias_or_name], copy=False)
                                 for p in ordered])
        for table in refs:
            for col in self.refs.get(id(table), []):
                if col.name in mapping:
                    col.set("this", exp.to_identifier(mapping[col.name]))
        return True

    def _canon_setop(self, query: Any, depth: int, cte_names: dict[int, str]) -> None:
        """连着同一种合并写的几支各自规范化；调换先后不改变结果的（见 _reorderable）再按规范化后的写法排序。"""
        kind = (type(query), bool(query.args.get("distinct")))
        branches: list[Any] = []

        def collect(node: Any) -> None:
            from sqlglot import exp

            if isinstance(node, exp.SetOperation) and (type(node), bool(node.args.get("distinct"))) == kind and \
                    not any(node.args.get(k) for k in ("order", "limit", "offset", "with_")):
                collect(node.left)
                collect(node.right)
            else:
                branches.append(node)

        collect(query.left)
        collect(query.right)
        texts = [(self.canon(b, depth, cte_names), i, b) for i, b in enumerate(branches)]
        if len(branches) < 2 or not self._reorderable(query):
            return
        ordered = [b for _, _, b in sorted(texts, key=lambda x: (x[0], x[1]))]
        node = ordered[0]
        for b in ordered[1:-1]:
            node = type(query)(this=node, expression=b, distinct=query.args.get("distinct"))
        query.set("this", node)
        query.set("expression", ordered[-1])

    def _reorderable(self, query: Any) -> bool:
        """这一处合并的各支调换先后，结果（行、值、排序）一样不一样。只有 UNION ALL 可能一样：

        - UNION（去重）、INTERSECT 留先出现的那个值，相等不等于一样：SQLite 里 REAL 列的 2.0 和 INTEGER 列的 2、MySQL
          默认不分大小写的排序规则下的 'SH' 和 'sh'、PostgreSQL numeric 的 2 和 2.00；EXCEPT 本来就分左右。
        - 这一层自己带 LIMIT / OFFSET 的，取的是先出来的几行（没有 ORDER BY 时就看哪支在前）。
        - ORDER BY 里有按名字排的：合并结果的列名取第一支的，调换之后 ORDER BY id 排的可能是另一列。
        - SQLite（没说方言的也按它）合并结果的列的排序规则取最左一支那列声明的（排序、从子查询读出来再比较都用它），
          表结构里看不到列声明的排序规则：只排整条语句最外层、没有 ORDER BY 的。"""
        from sqlglot import exp

        if not isinstance(query, exp.Union) or query.args.get("distinct"):
            return False
        if query.args.get("limit") is not None or query.args.get("offset") is not None:
            return False
        order = query.args.get("order")
        keys = [o.this if isinstance(o, exp.Ordered) else o for o in (order.expressions if order is not None else [])]
        if any(not k.is_number for k in keys):
            return False
        if self.read in (None, "sqlite"):
            if keys:
                return False
            top = query
            while isinstance(top.parent, exp.Subquery):
                top = top.parent
            if top is not self.tree:
                return False
        return True

    def _signature(self, item: Any, cte_names: dict[int, str], subs: dict[int, str]) -> tuple[str, str]:
        """来源的 (排序用的标识, 起别名用的名字)：实表是表名，CTE 是它的规范名，FROM 里的子查询是规范化后的写法。"""
        from sqlglot import exp

        if isinstance(item, exp.Table):
            cte = self.cte_of.get(id(item))
            if cte is not None:
                name = cte_names.get(id(cte), cte.alias_or_name)
                return f"cte:{name}", name
            return "table:" + ".".join(p for p in (item.catalog, item.db, item.name) if p), item.name
        return f"sub:{subs[id(item)][1]}", "sub"

    def _canon_select(self, select: Any, depth: int, cte_names: dict[int, str]) -> tuple[str, str]:
        from sqlglot import exp

        items = _items_of(select)
        subs: dict[int, tuple[str, str]] = {}
        for item in items:
            if isinstance(item, exp.Subquery):
                subs[id(item)] = self.canon(item.this, depth + 1, cte_names)
            elif id(item) in self.cte_of:
                cte = self.cte_of[id(item)]
                item.set("this", exp.to_identifier(cte_names.get(id(cte), cte.alias_or_name)))
        keys = {id(i): self._signature(i, cte_names, subs) for i in items}
        joins = select.args.get("joins") or []
        n_prefix = 0
        if items:
            n_prefix = 1
            for join in joins:
                if _comma(join):
                    n_prefix += 1
                else:
                    break
        prefix, rest = items[:n_prefix], joins[max(n_prefix - 1, 0):] if items else joins
        movable = not _starry(select) and not _unstable(select)
        free_lefts = movable and bool(rest) and self._independent_lefts(prefix, rest)

        def groups(xs: list[Any], key: Any) -> list[list[Any]]:
            xs = sorted(xs, key=key)
            return [list(g) for _, g in itertools.groupby(xs, key=key)]

        prefix_groups = groups(prefix, lambda s: keys[id(s)][0]) if movable else [[s] for s in prefix]
        rest_groups = groups(rest, lambda j: keys[id(j.this)][0]) if free_lefts else [[j] for j in rest]
        options = [[list(p) for p in itertools.permutations(g)] if len(g) > 1 else [g]
                   for g in prefix_groups + rest_groups]
        total = 1
        for o in options:
            total *= len(o)
        if total > _MAX_ORDERINGS or total > self.budget:
            options = [[o[0]] for o in options]
            total = 1
        self.budget -= total
        combos = list(itertools.product(*options))
        best: tuple[tuple[str, str], int] | None = None
        for index, combo in enumerate(combos):
            text = self._apply(select, depth, cte_names, keys, combo, len(prefix_groups))
            if best is None or text < best[0]:
                best = (text, index)
        if best is None:
            return self._text(select)
        if len(combos) > 1:
            self._apply(select, depth, cte_names, keys, combos[best[1]], len(prefix_groups))
        return best[0]

    @staticmethod
    def _independent_lefts(prefix: list[Any], rest: list[Any]) -> bool:
        """跟在一组内连接后面的几个 LEFT JOIN，各自的 ON 只引用这组来源和自己（不引用别的 LEFT JOIN 进来的表、没有
        子查询）：它们之间谁先谁后结果一样。"""
        from sqlglot import exp

        names = {s.alias_or_name for s in prefix}
        for join in rest:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join):
                return False
            on = join.args.get("on")
            if on is None or on.find(exp.Select, exp.Subquery) is not None:
                return False
            if not {c.table for c in on.find_all(exp.Column)} <= names | {join.this.alias_or_name}:
                return False
        return True

    def _apply(self, select: Any, depth: int, cte_names: dict[int, str], keys: dict[int, tuple[str, str]],
               combo: tuple[list[Any], ...], n_prefix_groups: int) -> tuple[str, str]:
        from sqlglot import exp

        prefix = [s for g in combo[:n_prefix_groups] for s in g]
        rest = [j for g in combo[n_prefix_groups:] for j in g]
        if prefix:
            select.args["from_"].set("this", prefix[0])
            select.set("joins", [exp.Join(this=s) for s in prefix[1:]] + rest or None)
        counts: dict[str, int] = {}
        for item in _items_of(select):
            _, base = keys[id(item)]
            counts[base] = counts.get(base, 0) + 1
            name = self._alias(base, depth, counts[base])
            item.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
            for col in self.refs.get(id(item), []):
                col.set("table", exp.to_identifier(name))
        for sub in _nested_queries(select):
            self.canon(sub, depth + 1, cte_names)
        _sort_own(select, self.read)
        return self._text(select)


_FLIP = {"GT": "LT", "LT": "GT", "GTE": "LTE", "LTE": "GTE", "EQ": "EQ", "NEQ": "NEQ"}


def _fixed(node: Any) -> bool:
    """不涉及任何列、子查询的表达式（常量、常量算出来的日期）：排比较、加乘的两边时放在右边，好读。"""
    from sqlglot import exp

    return node.find(exp.Column, exp.Select, exp.Subquery) is None


def _sort_own(select: Any, read: str | None) -> None:
    """这一段自己的表达式里交换律成立的地方按写出来的文本排（里层先排）：AND、OR（去重）、比较（不涉及列的一边在
    右，两边都涉及列时按文本排、大于小于跟着换方向）、两项的加和乘（不涉及列的一项在右，三项以上的不跨项挪动：
    浮点数加法不满足结合律）、IN 列表、GROUP BY、PARTITION BY。"""
    from sqlglot import exp

    def key(node: Any) -> tuple[str, str]:
        return _key(node, read)

    def depth(node: Any) -> int:
        d = 0
        while node is not select and node.parent is not None:
            node = node.parent
            d += 1
        return d

    for node in sorted(_block_nodes(select), key=depth, reverse=True):
        if node is not select and node.parent is None:
            continue
        if isinstance(node, (exp.And, exp.Or)):
            if isinstance(node.parent, type(node)):
                continue
            uniq: dict[str, Any] = {}
            for part in node.flatten():
                uniq.setdefault(key(part), part)
            ordered = [uniq[k] for k in sorted(uniq)]
            new = ordered[0] if len(ordered) == 1 else (
                exp.and_(*ordered, copy=False) if isinstance(node, exp.And) else exp.or_(*ordered, copy=False))
            node.replace(new)
        elif type(node).__name__ in _FLIP:
            left, right = node.this, node.expression
            if isinstance(left, (exp.Any, exp.All)) or isinstance(right, (exp.Any, exp.All)):
                continue
            # 两边都写了 COLLATE 时 SQLite 用左边那个：不对调
            if left.find(exp.Collate) is not None and right.find(exp.Collate) is not None:
                continue
            if (_fixed(left) and not _fixed(right)) or (_fixed(left) == _fixed(right) and key(left) > key(right)):
                node.replace(getattr(exp, _FLIP[type(node).__name__])(this=right, expression=left))
        elif isinstance(node, (exp.Add, exp.Mul)):
            left, right = node.this, node.expression
            if (_fixed(left) and not _fixed(right)) or (_fixed(left) == _fixed(right) and key(left) > key(right)):
                node.set("this", right)
                node.set("expression", left)
        elif isinstance(node, exp.Group):
            if node.expressions and not any(node.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
                node.set("expressions", sorted(node.expressions, key=key))
        elif isinstance(node, exp.Window):
            if node.args.get("partition_by"):
                node.set("partition_by", sorted(node.args["partition_by"], key=key))
        elif isinstance(node, exp.In) and node.expressions:
            uniq = {key(i): i for i in node.expressions}
            node.set("expressions", [uniq[k] for k in sorted(uniq)])


# ==========================================================================
# 差异说明
# ==========================================================================


@dataclass(frozen=True)
class FacetDiff:
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同）/ filter（筛选条件）/ aggregate（聚合函数、
    去重计数）/ expression（选出来的列怎么算）/ join（关联的表、关联类型、关联条件、外连接的主表）/ tables（查的是
    哪张表）/ set_op（几支查询怎么合并）/ group_by（分组键、去重）/ having / limit（取哪几行）/ order（只是排序不同）/
    parse（解析不了）/ other（兜底：未能归纳成具体差异，见 explain_sql_diff）。a、b 是两边各自的写法（给人看，没有
    就是 None），text 是一句中文说明。detail 只在 time 上有：两边的时间列 [{table, column}]，调用方据此对照数据目录
    （业务日期）；在 filter 上有 {"columns": [{table, column}]}：条件只涉及的实表列，对照有效行过滤用。"""

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


@dataclass(frozen=True)
class SqlDiff:
    """两条 SQL 差在哪（explain_sql_diff 的结果）。

    equivalent：两边经过只保持语义的变换之后，规范形式一字不差（level 是哪一级的规范形式，见 _canonical_forms）。
    items：具体差异，等价时为空；不等价时一定不为空——归纳不出具体差异时是一条 aspect="other" 的兜底说明。
    fallback：用了兜底说明（一条具体说法都没有，或者具体说法只覆盖了一部分）。
    text_diff：规范化之后两条 SQL 不同的行（difflib 比两边 sqlglot pretty 的输出，「- 」开头是 A 的、「+ 」开头是
    B 的，最多 40 行），给界面高亮；等价时为空。"""

    equivalent: bool
    items: list[FacetDiff] = field(default_factory=list)
    fallback: bool = False
    text_diff: tuple[str, ...] = ()
    level: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"equivalent": self.equivalent, "items": [d.as_dict() for d in self.items], "fallback": self.fallback,
                "text_diff": list(self.text_diff), "level": self.level}


#: 兜底说明
_FALLBACK = "两条 SQL 的写法不同，未能归纳成具体差异，请对照原文"
_FALLBACK_REST = "另有未能归纳的差异，请对照原文"
#: text_diff 最多列几行
_MAX_DIFF_LINES = 40

_CJK_PUNCT = frozenset("，。；：、！？「」『』（）《》【】…—")
#: 聚合函数的说法
_VERBS = {"sum": "求和", "count": "计数", "avg": "求平均", "min": "取最小值", "max": "取最大值"}


def _wide(ch: str) -> bool:
    return "⺀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


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
    """数据目录（{表名: notes}）里列的中文名 → diff_facets / explain_sql_diff 的 names：{「表.列」: 中文名}；只在一张
    表里有、或者各表叫法一致的列另记 {列: 中文名}。被驳回的项不算。"""
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


@dataclass
class _Rel:
    """一段里的一个来源：alias 是规范别名，name 是实表名或 CTE 的规范名，table 表示是不是实表，shown 是给人看的
    名字（实表是表名，CTE 是原来起的名字），rejected 表示写的是 LEFT JOIN、被 WHERE 变成了内连接。"""

    alias: str
    name: str
    table: bool
    shown: str
    rejected: str | None = None


@dataclass
class _Block:
    """一段 SELECT 拆开来比：开头那组内连接的来源（rels）、后面的连接（joins：LEFT JOIN 等，按先后）、WHERE、
    GROUP BY、HAVING 的各项，选出来的列，排序和取行。"""

    select: Any
    rels: list[_Rel]
    joins: list[Any]
    where: list[Any]
    group: list[Any]
    having: list[Any]
    exprs: list[Any]
    order: list[Any]
    limit: str | None
    offset: str | None
    distinct: bool
    deduped: bool
    ctes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, select: Any, read: str | None, ctes: Mapping[str, str]) -> "_Block | None":
        from sqlglot import exp

        if not isinstance(select, exp.Select) or select.args.get("with_") is not None:
            return None
        items = _items_of(select)
        if any(not isinstance(i, exp.Table) for i in items):
            return None
        joins = select.args.get("joins") or []
        n = 1
        for join in joins:
            if _comma(join):
                n += 1
            else:
                break
        ctes = dict(ctes)
        rels = [_Rel(i.alias_or_name, i.name, i.name not in ctes, ctes.get(i.name, i.name),
                     i.meta.get(_REJECTED)) for i in items[:n]]
        where, group, having = select.args.get("where"), select.args.get("group"), select.args.get("having")
        order, limit, offset = select.args.get("order"), select.args.get("limit"), select.args.get("offset")

        def number(node: Any) -> str | None:
            return node.expression.sql(dialect=read) if node is not None and node.expression is not None else None

        return cls(select, rels, joins[n - 1:] if items else joins,
                   _conjuncts(where.this) if where is not None else [],
                   list(group.expressions) if group is not None else [],
                   _conjuncts(having.this) if having is not None else [],
                   list(select.expressions), list(order.expressions) if order is not None else [],
                   number(limit), number(offset), select.args.get("distinct") is not None,
                   bool(group is not None and group.meta.get(_FROM_DISTINCT)), ctes)

    def rel(self, alias: str) -> _Rel | None:
        for r in self.rels:
            if r.alias == alias:
                return r
        for join in self.joins:
            if join.this.alias_or_name == alias:
                name = join.this.name
                return _Rel(alias, name, name not in self.ctes, self.ctes.get(name, name))
        return None

    def names(self) -> list[str]:
        return [r.name for r in self.rels] + [j.this.name for j in self.joins]


class _Explainer:
    """在两边的规范形式上逐段比，只把有把握的不同写成具体的中文句子，同时记下是不是所有不同都说到了。

    有把握的门槛：两段读的来源对得上（一模一样，或者只差一处说得清的：换了一张表、一边多关联了一张表、同一张表的
    关联类型或关联条件不同、外连接的主表对调）之后，才逐项比 WHERE、选出来的列、分组、HAVING、取行；每一项只在它
    只涉及这一段的实表列（不涉及 CTE 算出来的列、外层的列、子查询，同名的表在这一段里只出现一次）时才说具体的，
    说的都是「这一段的这一项两边各是什么」，不推断对结果的影响。来源对不上、两段结构不同、条件里套着子查询又配不成
    一对的，宁可不说具体的，交给兜底说明。CTE 两边定义一样的按另一边的名字起名后对齐；各剩一个对不上的配成一对，
    往里比。CTE 里选出来的列不比：规范化时去掉了没用到的列、按定义重排过，和原文的列对不上。"""

    def __init__(self, read: str | None, labels: tuple[str, str], names: Mapping[str, str] | None) -> None:
        self.read = read
        self.la, self.lb = labels
        self.label = _labeler(names)
        self.items: list[FacetDiff] = []
        self.ctes: dict[str, str] = {}
        #: 有一句话没说出口（两边给人看的写法一样，说出来自相矛盾）：这一处不同就没说到
        self.withheld = False

    def sql(self, node: Any) -> str:
        """给人看的写法：按方言生成；生成的写法读回来不是同一棵树时（SQLite 把 CAST(x AS DATE) 生成 DATE(x)，两者
        在 SQLite 里结果不同）改用 sqlglot 的通用写法，免得给人看的和实际的不是一回事。"""
        import sqlglot

        text = node.sql(dialect=self.read, comments=False)
        try:
            if _fingerprint(sqlglot.parse_one(text, read=self.read)) == _fingerprint(node):
                return text
        except Exception:  # noqa: BLE001 - 片段单独读不回来：用通用写法
            pass
        return node.sql(comments=False)

    @staticmethod
    def fp(node: Any) -> str:
        """比「是不是同一个」用的写法（见 _fingerprint）：生成的 SQL 可能把不同的写法写成一样的。"""
        return _fingerprint(node)

    # ---- 入口：整条语句

    def statement(self, form_a: _Form, form_b: _Form) -> bool:
        """比两条语句的规范形式，返回是不是全都说到了。B 先按 A 的 CTE 名重新起名（定义一样的叫同一个名字，各剩一个
        对不上的也配成一对），两边才能逐段对齐。"""
        ta, tb = form_a.tree, form_b.tree
        wa = ta.args.get("with_")
        names_a = {c.alias_or_name: c for c in (wa.expressions if wa is not None else [])}
        if form_b.pre is not None and names_a:
            tb = self._renamed(form_a, form_b, names_a)
        wb = tb.args.get("with_")
        names_b = {c.alias_or_name: c for c in (wb.expressions if wb is not None else [])}
        shown_a = {n: c.this.meta.get(_ORIGINAL) or n for n, c in names_a.items()}
        shown_b = {n: c.this.meta.get(_ORIGINAL) or n for n, c in names_b.items()}
        covered = set(names_a) == set(names_b)
        main_a, main_b = ta.copy(), tb.copy()
        main_a.set("with_", None)
        main_b.set("with_", None)
        self.ctes = {**shown_b, **shown_a}
        covered &= self.query(main_a, main_b, ("", ""), outputs=True)
        for name in sorted(set(names_a) & set(names_b)):
            body_a, body_b = names_a[name].this, names_b[name].this
            if self.fp(body_a) != self.fp(body_b):
                where = (_say("在", shown_a[name], "里"), _say("在", shown_b[name], "里"))
                covered &= self.query(body_a, body_b, where, outputs=False)
        return covered and not self.withheld

    def _renamed(self, form_a: _Form, form_b: _Form, names_a: dict[str, Any]) -> Any:
        """B 起名之前的树按 A 的 CTE 名重新起名：定义一样的叫 A 的名字；两边各剩一个对不上的，B 的那个也叫 A 的。"""
        from sqlglot import exp

        hint = _Hint(bodies={_fingerprint(c.this): name for name, c in names_a.items()}, taken=set(names_a))
        first = _named(form_b.pre.copy(), self.read, form_b.qualified, hint)
        wb = first.args.get("with_")
        left_b = [c for c in (wb.expressions if wb is not None else []) if c.alias_or_name not in names_a]
        used = {c.alias_or_name for c in (wb.expressions if wb is not None else [])}
        left_a = [name for name in names_a if name not in used]
        if len(left_a) == 1 and len(left_b) == 1:
            pre = form_b.pre.copy()
            original = left_b[0].this.meta.get(_ORIGINAL)
            matches = [c.alias_or_name for c in pre.find_all(exp.CTE) if c.this.meta.get(_ORIGINAL) == original]
            if original is not None and len(matches) == 1:
                hint.names[matches[0]] = left_a[0]
                return _named(pre, self.read, form_b.qualified, hint)
        return first

    def query(self, qa: Any, qb: Any, where: tuple[str, str], *, outputs: bool) -> bool:
        from sqlglot import exp

        if self.fp(qa) == self.fp(qb):
            return True
        if isinstance(qa, exp.Select) and isinstance(qb, exp.Select):
            return self.select(qa, qb, where, outputs)
        if isinstance(qa, exp.SetOperation) and isinstance(qb, exp.SetOperation):
            return self.setop(qa, qb, where, outputs)
        return False

    # ---- 说法的拼法

    def sides(self, where: tuple[str, str], first: int = 0) -> tuple[str, str]:
        """两边的叫法，带上在哪一段（「A 在 paid 里」），按 A、B 的先后返回。两边都说在哪：只说一边，「另一边没有」
        会读成整条语句都没有。"""
        return _say(self.la, where[0]), _say(self.lb, where[1])

    def add(self, aspect: str, a: Any, b: Any, text: str, detail: dict[str, Any] | None = None) -> None:
        """记一句话。两边给人看的写法一样（「A 算的是 1，B 算的是 1」）就不说：读起来自相矛盾，这一处算没说到。"""
        if isinstance(a, str) and isinstance(b, str) and a == b:
            self.withheld = True
            return
        self.items.append(FacetDiff(aspect, a, b, text, detail))

    def base_columns(self, node: Any, block: _Block) -> list[tuple[_Rel, Any]] | None:
        """node 只涉及这一段的实表列（同名的表在这一段只出现一次）、不套子查询时，返回各列和它的来源；否则 None。"""
        from sqlglot import exp

        if node.find(exp.Select, exp.Subquery) is not None:
            return None
        names = block.names()
        out = []
        for col in node.find_all(exp.Column):
            rel = block.rel(col.table) if col.table else None
            if rel is None or not rel.table or names.count(rel.name) != 1:
                return None
            out.append((rel, col))
        return out

    def show(self, node: Any, block: _Block, *, full: bool = False) -> str:
        """给人看的写法：只有一个来源时列不写表名，否则写实表名（规范别名换回表名，CTE 换回原来的名字）；选出来的
        列去掉规范化起的别名。full 时一律写表名。"""
        from sqlglot import exp

        copy = node.unalias().copy() if isinstance(node, exp.Alias) else node.copy()
        single = len(block.rels) + len(block.joins) == 1 and not full
        for col in copy.find_all(exp.Column):
            rel = block.rel(col.table) if col.table else None
            if rel is not None:
                col.set("table", None if single else exp.to_identifier(rel.shown))
        return self.sql(copy)

    def show_all(self, conds: list[Any], block: _Block) -> str:
        """几个条件用 AND 连起来给人看：OR 套上括号（按 sqlglot 的结合规则生成）。"""
        from sqlglot import exp

        if len(conds) == 1:
            return self.show(conds[0], block)
        return self.show(exp.and_(*[c.copy() for c in conds], copy=False), block)

    def named(self, rel: _Rel, column: str) -> str:
        """列带中文名（有的话）：「支付时间 paid_at」。"""
        label = self.label((rel.name, column))
        return _say(label, column) if label else column

    def what(self, rel: _Rel) -> str:
        return _say(rel.shown, "表") if rel.table else _say("子查询", rel.shown)

    # ---- 一段 SELECT

    def select(self, a: Any, b: Any, where: tuple[str, str], outputs: bool) -> bool:
        A, B = _Block.of(a, self.read, self.ctes), _Block.of(b, self.read, self.ctes)
        if A is None or B is None:
            return False
        matched = self.relations(A, B, where)
        if matched is None:
            return False
        covered, skip_a, skip_b, B = matched
        covered &= self.conditions(A, B, where, skip_a, skip_b)
        if outputs:
            covered &= self.outputs(A, B, where)
        else:
            # CTE 里选出来的列规范化时去掉过没用到的、重排过，和原文的列对不上：不同就不说具体的
            covered &= self.groups(A, B, where)
            covered &= [self.fp(e) for e in A.exprs] == [self.fp(e) for e in B.exprs]
        covered &= self.having(A, B, where)
        covered &= self.rows(A, B, where)
        return covered

    def relations(self, A: _Block, B: _Block, where: tuple[str, str]
                  ) -> tuple[bool, set[str], set[str], _Block] | None:
        """两段读的来源对不对得上。对得上返回 (说到了没有, A 已经说过的 WHERE 项, B 已经说过的 WHERE 项, B)；B 可能换了
        一张表（换成 A 的表名之后再比别的）。对不上返回 None。"""
        la, lb = self.sides(where)
        ra, rb = [r.alias for r in A.rels], [r.alias for r in B.rels]
        ja, jb = [self.fp(j) for j in A.joins], [self.fp(j) for j in B.joins]
        if ra == rb and ja == jb:
            return True, set(), set(), B
        # 换了一张表：开头那组来源只差一张实表，其余一样
        if len(ra) == len(rb) and ja == jb and not A.joins:
            only_a = [r for r in A.rels if r.alias not in rb]
            only_b = [r for r in B.rels if r.alias not in ra]
            if len(only_a) == 1 and len(only_b) == 1 and only_a[0].table and only_b[0].table and \
                    only_a[0].name == only_a[0].alias and only_b[0].name == only_b[0].alias:
                x, y = only_a[0], only_b[0]
                self.add("tables", x.name, y.name, _say(la, "查的是", x.name, "表，", lb, "查的是", y.name, "表"))
                return True, set(), set(), self._swap(B, y, x)
        # 一边多关联了一张表（内连接）
        if ja == jb and not A.joins:
            if set(rb) < set(ra) and len(ra) == len(rb) + 1:
                return self._extra(A, B, la, lb, 0)
            if set(ra) < set(rb) and len(rb) == len(ra) + 1:
                first_b = self.sides(where, 1)
                return self._extra(B, A, first_b[1], first_b[0], 1)
        # 同一张表：LEFT JOIN 对 INNER JOIN、关联条件不同、只有一边关联了（LEFT JOIN）、外连接的主表对调
        return self._outer(A, B, where)

    def _swap(self, B: _Block, old: _Rel, new: _Rel) -> _Block | None:
        from sqlglot import exp

        select = B.select.copy()
        for col in select.find_all(exp.Column):
            if col.table == old.alias:
                col.set("table", exp.to_identifier(new.alias))
        for table in select.find_all(exp.Table):
            if table.alias_or_name == old.alias:
                table.set("this", exp.to_identifier(new.name))
                table.set("alias", exp.TableAlias(this=exp.to_identifier(new.alias)))
        for s in list(select.find_all(exp.Select)):
            _sort_own(s, self.read)
        return _Block.of(select, self.read, self.ctes)

    def _uses(self, block: _Block, name: str) -> bool:
        """这一段在子查询里（不是开头的来源、连接）用到了 name 这张表。"""
        from sqlglot import exp

        own = {id(i) for i in _items_of(block.select)}
        return any(t.name == name and id(t) not in own for t in block.select.find_all(exp.Table))

    def _extra(self, big: _Block, small: _Block, lbig: str, lsmall: str, side: int
               ) -> tuple[bool, set[str], set[str], _Block] | None:
        from sqlglot import exp

        [rel] = [r for r in big.rels if r.alias not in {s.alias for s in small.rels}]
        if big.names().count(rel.name) != 1:
            return None
        conds = [c for c in big.where if rel.alias in {col.table for col in c.find_all(exp.Column)}]
        what = self.what(rel)
        shown = _say("（关联的条件是", self.show_all(conds, big), "）") if conds and \
            all(self.base_columns(c, big) is not None for c in conds) else ""
        tail = ("没有关联它（只在子查询里用到了", rel.shown, "）") if self._uses(small, rel.name) else ("没有",)
        text = _say(lbig, "关联了", what, shown, "，", lsmall, *tail)
        self.add("join", what if side == 0 else None, what if side == 1 else None, text)
        skip = {self.fp(c) for c in conds}
        return (True, skip, set(), small) if side == 0 else (True, set(), skip, big)

    def _outer(self, A: _Block, B: _Block, where: tuple[str, str]) -> tuple[bool, set[str], set[str], _Block] | None:
        from sqlglot import exp

        la, lb = self.sides(where)
        first_b = self.sides(where, 1)

        def kind(join: Any) -> str:
            return str(join.side or "").lower() or "inner"

        ra, rb = [r.alias for r in A.rels], [r.alias for r in B.rels]
        ja = {j.this.alias_or_name: j for j in A.joins}
        jb = {j.this.alias_or_name: j for j in B.joins}
        if len(A.joins) != len(ja) or len(B.joins) != len(jb):
            return None
        # 外连接的主表对调：两个来源，一边 x LEFT JOIN y，一边 y LEFT JOIN x，条件一样
        if len(ra) == len(rb) == 1 and len(ja) == len(jb) == 1 and ra != rb and set(ra) == set(jb) and \
                set(rb) == set(ja):
            x, y = A.joins[0], B.joins[0]
            if kind(x) == kind(y) == "left" and self.fp(x.args["on"]) == self.fp(y.args["on"]):
                wa = _say("以", A.rels[0].shown, "为主表外连接", B.rels[0].shown)
                wb = _say("以", B.rels[0].shown, "为主表外连接", A.rels[0].shown)
                self.add("join", wa, wb, _say(la, wa, "，", lb, wb))
                return True, set(), set(), B
            return None
        if [r for r in ra if r not in rb and r not in jb] or [r for r in rb if r not in ra and r not in ja]:
            return None
        one_sided_a = [k for k in ja if k not in jb and k not in rb]
        one_sided_b = [k for k in jb if k not in ja and k not in ra]
        if one_sided_a and one_sided_b:
            return None
        skip_a: set[str] = set()
        skip_b: set[str] = set()
        for alias in sorted(set(ja) | set(jb)):
            x, y = ja.get(alias), jb.get(alias)
            if x is not None and y is not None:
                if kind(x) != kind(y):
                    return None
                if self.fp(x) != self.fp(y):
                    on_x, on_y = self._on(x, A), self._on(y, B)
                    if on_x is None or on_y is None:
                        return None
                    rel = A.rel(alias)
                    self.add("join", on_x, on_y, _say(la, "关联", self.what(rel), "的条件是", on_x, "，", lb, "是", on_y))
                continue
            # 一边 LEFT JOIN，另一边内连接（在开头那组来源里，关联条件在 WHERE 里）
            if x is not None and alias in rb and kind(x) == "left":
                done = self._left_inner(x, A, B, alias, la, lb, 0)
                if done is None:
                    return None
                skip_b |= done
                continue
            if y is not None and alias in ra and kind(y) == "left":
                done = self._left_inner(y, B, A, alias, la, lb, 1)
                if done is None:
                    return None
                skip_a |= done
                continue
            # 只有一边关联了
            join = x if x is not None else y
            if join is None or kind(join) != "left":
                return None
            block, me, other = (A, la, lb) if x is not None else (B, first_b[1], first_b[0])
            rel = block.rel(alias)
            conds = [c for c in block.where if alias in {col.table for col in c.find_all(exp.Column)}]
            text = _say(me, *self._outer_words(join, block), self.what(rel), "，", other, "没有")
            if conds:
                if not all(self.base_columns(c, block) is not None for c in conds):
                    return None
                text += _say("（WHERE 里还有", self.show_all(conds, block), "）")
            self.add("join", rel.shown if x is not None else None, rel.shown if y is not None else None, text)
            (skip_a if x is not None else skip_b).update(self.fp(c) for c in conds)
        return True, skip_a, skip_b, B

    def _on(self, join: Any, block: _Block) -> str | None:
        on = join.args.get("on")
        if on is None or self.base_columns(on, block) is None:
            return None
        return self.show(on, block)

    def _left_inner(self, join: Any, left: _Block, inner: _Block, alias: str, la: str, lb: str,
                    side: int) -> set[str] | None:
        """left 这边 LEFT JOIN alias，inner 那边同一张表是内连接：inner 的 WHERE 里有 left 的 ON 的每一项才算关联条件
        一样，只差连接类型。返回 inner 那边已经说过的 WHERE 项。照原文说：left 那边原来写的是 RIGHT JOIN 的说以哪一侧
        为主表；inner 那边写的是 LEFT / RIGHT JOIN、被 WHERE 里的条件变成了内连接的，说写的是什么、实际是什么。side 是
        left 在哪一边（0 是 A）；la、lb 是 A、B 的叫法。"""
        on = join.args.get("on")
        conds = [self.fp(c) for c in _conjuncts(on)] if on is not None else []
        if not conds or not set(conds) <= {self.fp(c) for c in inner.where}:
            return None
        rel = inner.rel(alias)
        what = self.what(rel)
        outer = self._outer_words(join, left)
        turned = [r for r in inner.rels if r.rejected]
        if turned:
            words = "、".join(dict.fromkeys(r.rejected for r in turned))
            names = "、".join(r.shown for r in turned)
            inner_says = ("写的是", words, "，但 WHERE 里的条件把关联不上", names, "的行筛掉了，实际是 INNER JOIN")
        if side == 0:
            text = _say(la, *outer, what, "，", lb, *(inner_says if turned else ("用 INNER JOIN",)))
            self.add("join", "OUTER JOIN", "INNER JOIN", text)
        else:
            first = (*inner_says, "；") if turned else ("用 INNER JOIN 关联", what, "，")
            second = (*outer, what) if turned or join.meta.get(_FLIPPED) else ("用 LEFT JOIN",)
            text = _say(la, *first, lb, *second)
            self.add("join", "INNER JOIN", "OUTER JOIN", text)
        return set(conds)

    def _outer_words(self, join: Any, block: _Block) -> tuple[str, ...]:
        """一次外连接的说法：原文就是 LEFT JOIN 的说「用 LEFT JOIN 关联」；从 RIGHT JOIN 改写来的说「以谁为主表外连接」
        （照写 LEFT 会和原文对不上）。"""
        if join.meta.get(_FLIPPED):
            return ("以", "、".join(r.shown for r in block.rels), "为主表外连接")
        return ("用 LEFT JOIN 关联",)

    # ---- WHERE

    def timed(self, cond: Any, block: _Block) -> tuple[_Rel, Any] | None:
        """时间条件：一列（可以套着日期函数、CAST）和常量比较，列名像时间或者常量像日期。返回 (来源, 列)。"""
        from sqlglot import exp

        if not isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In)):
            return None
        if isinstance(cond, exp.In):
            column_side, others = cond.this, list(cond.expressions)
        elif _fixed(cond.expression):
            column_side, others = cond.this, [cond.expression]
        elif _fixed(cond.this):
            column_side, others = cond.expression, [cond.this]
        else:
            return None
        if not others or not all(_fixed(o) for o in others):
            return None
        cols = self.base_columns(column_side, block)
        if not cols or len({(r.alias, c.name) for r, c in cols}) != 1 or _value_column(column_side) is None:
            return None
        rel, col = cols[0]
        if _temporal_name(col.name) or any(_datey(o) for o in others) or _datey(column_side):
            return rel, col
        return None

    def conditions(self, A: _Block, B: _Block, where: tuple[str, str], skip_a: set[str], skip_b: set[str]) -> bool:
        la, lb = self.sides(where)
        only_a, only_b = self._unmatched([c for c in A.where if self.fp(c) not in skip_a],
                                         [c for c in B.where if self.fp(c) not in skip_b])
        if not only_a and not only_b:
            return True
        covered = True
        only_a, only_b = self._times(A, B, la, lb, only_a, only_b)
        # 同一列上的两个条件配成一对
        for c in list(only_a):
            col_a = self._single_column(c, A)
            if col_a is None:
                continue
            mine = [x for x in only_a if self._single_column(x, A) == col_a]
            theirs = [y for y in only_b if self._single_column(y, B) == col_a]
            if len(mine) == 1 and len(theirs) == 1:
                d = theirs[0]
                wa, wb = self.show(c, A), self.show(d, B)
                if wa == wb:
                    wa, wb = self.show(c, A, full=True), self.show(d, B, full=True)
                self.add("filter", wa, wb, _say(la, "按", wa, "筛选，", lb, "按", wb, "筛选"),
                         {"columns": self._columns(c, A)})
                only_a.remove(c)
                only_b.remove(d)
        # 两边各剩一个、涉及的列一样的条件，也配成一对说
        if len(only_a) == 1 and len(only_b) == 1:
            ca, cb = self.base_columns(only_a[0], A), self.base_columns(only_b[0], B)
            if ca and cb and {(r.name, c.name) for r, c in ca} == {(r.name, c.name) for r, c in cb}:
                wa, wb = self.show(only_a[0], A), self.show(only_b[0], B)
                self.add("filter", wa, wb, _say(la, "按", wa, "筛选，", lb, "按", wb, "筛选"),
                         {"columns": self._columns(only_a[0], A)})
                only_a, only_b = [], []
        # 只在一边的条件：「另一边没有」带上在哪一段，免得读成整条语句都没有
        fa, fb = _say(self.la, where[0]), _say(self.lb, where[1])
        for side, rest, block, me, other in ((0, only_a, A, fa, fb), (1, only_b, B, fb, fa)):
            for c in list(rest):
                if self.base_columns(c, block) is None:
                    continue
                shown = self.show(c, block)
                excluded = self._excludes(c, block)
                text = _say(me, "排除了", excluded, "的行，", other, "没有") if excluded else \
                    _say(me, "只保留", shown, "的行，", other, "没有这个条件")
                self.add("filter", shown if side == 0 else None, shown if side == 1 else None, text,
                         {"columns": self._columns(c, block)})
                rest.remove(c)
        # 条件里套着的子查询：两边各一个、套子查询之外写法一样的配成一对，往里比
        for c in list(only_a):
            template = self._template(c)
            if template is None:
                continue
            mine = [x for x in only_a if self._template(x) == template]
            theirs = [d for d in only_b if self._template(d) == template]
            if len(mine) == 1 and len(theirs) == 1:
                d = theirs[0]
                place = self._place(c)
                inner = (_deeper(where[0], place), _deeper(where[1], place))
                covered &= self.query(self._subquery(c), self._subquery(d), inner, outputs=True)
                only_a.remove(c)
                only_b.remove(d)
        return covered and not only_a and not only_b

    def _times(self, A: _Block, B: _Block, la: str, lb: str, only_a: list[Any], only_b: list[Any]
               ) -> tuple[list[Any], list[Any]]:
        """时间条件：两边差在按哪一列筛时间、同一列上的范围，或者只有一边按时间筛。说到了的从两边拿掉。"""
        ta = [(c, t) for c in only_a if (t := self.timed(c, A)) is not None]
        tb = [(c, t) for c in only_b if (t := self.timed(c, B)) is not None]
        cols_a = {(t[0].name, t[1].name) for _, t in ta}
        cols_b = {(t[0].name, t[1].name) for _, t in tb}
        if not (ta or tb) or len(cols_a) > 1 or len(cols_b) > 1:
            return only_a, only_b
        all_a = [(c, (t[0].name, t[1].name)) for c in A.where if (t := self.timed(c, A)) is not None]
        all_b = [(c, (t[0].name, t[1].name)) for c in B.where if (t := self.timed(c, B)) is not None]
        detail = {"a": [{"table": t, "column": c} for t, c in sorted(cols_a)],
                  "b": [{"table": t, "column": c} for t, c in sorted(cols_b)]}
        if cols_a and cols_b and cols_a != cols_b:
            (_, ca), (_, cb) = next(iter(cols_a)), next(iter(cols_b))
            text = _say(la, "按", self.named(ta[0][1][0], ca), "筛时间，", lb, "按", self.named(tb[0][1][0], cb))
            if self._shape([c for c, _ in ta]) != self._shape([c for c, _ in tb]):
                text += _say("（", la, "是", self.show_all(_by_bound([c for c, _ in ta]), A), "，", lb, "是",
                             self.show_all(_by_bound([c for c, _ in tb]), B), "）")
            self.add("time", ca, cb, text, detail)
        else:
            [col] = list(cols_a | cols_b)
            ra = _by_bound([c for c, k in all_a if k == col])
            rb = _by_bound([c for c, k in all_b if k == col])
            rel = (ta or tb)[0][1][0]
            if ra and rb:
                wa, wb = self.show_all(ra, A), self.show_all(rb, B)
                self.add("time_range", wa, wb, _say(la, "的时间范围是", wa, "，", lb, "是", wb))
            elif ra and not all_b:
                self.add("time", col[1], None, _say(la, "按", self.named(rel, col[1]), "筛时间，", lb, "没有按时间筛选"),
                         detail)
            elif rb and not all_a:
                self.add("time", None, col[1], _say(la, "没有按时间筛选，", lb, "按", self.named(rel, col[1]), "筛时间"),
                         detail)
            else:
                return only_a, only_b
        return [c for c in only_a if all(c is not x for x, _ in ta)], [c for c in only_b if all(c is not x for x, _ in tb)]

    def _unmatched(self, xs: list[Any], ys: list[Any]) -> tuple[list[Any], list[Any]]:
        rest = list(ys)
        only_x = []
        for x in xs:
            found = next((y for y in rest if self.fp(y) == self.fp(x)), None)
            if found is None:
                only_x.append(x)
            else:
                rest.remove(found)
        return only_x, rest

    def _shape(self, conds: list[Any]) -> list[str]:
        """时间条件把那一列换成占位符之后的写法：两边只差时间列时一样。"""
        from sqlglot import exp

        out = []
        for c in conds:
            copy = c.copy()
            for col in list(copy.find_all(exp.Column)):
                col.replace(exp.column("_time_"))
            out.append(self.fp(copy))
        return sorted(out)

    def _single_column(self, cond: Any, block: _Block) -> tuple[str, str] | None:
        cols = self.base_columns(cond, block)
        if not cols or len({(r.name, c.name) for r, c in cols}) != 1:
            return None
        return cols[0][0].name, cols[0][1].name

    def _columns(self, cond: Any, block: _Block) -> list[dict[str, str]]:
        cols = self.base_columns(cond, block) or []
        return [{"table": t, "column": c} for t, c in sorted({(r.name, col.name) for r, col in cols})]

    def _excludes(self, cond: Any, block: _Block) -> str | None:
        """「排除」的写法去掉否定之后排除的是什么：<> 写成 =，NOT x 写成 x，IS NOT NULL 写成「列 为空」。"""
        from sqlglot import exp

        if isinstance(cond, exp.NEQ):
            return self.show(exp.EQ(this=cond.this.copy(), expression=cond.expression.copy()), block)
        if isinstance(cond, exp.Not):
            inner = cond.this.unnest() if isinstance(cond.this, exp.Paren) else cond.this
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                return _say(self.show(inner.this, block), "为空")
            return self.show(inner, block)
        return None

    def _template(self, cond: Any) -> str | None:
        """条件里只套着一个子查询时，子查询换成占位符之后的写法；不然 None。"""
        from sqlglot import exp

        tops = [n for n in cond.walk() if isinstance(n, (exp.Select, exp.SetOperation)) and
                not any(isinstance(p, (exp.Select, exp.SetOperation)) for p in _ancestors(n, cond))]
        if len(tops) != 1:
            return None
        copy = cond.copy()
        for node in copy.walk():
            if isinstance(node, (exp.Select, exp.SetOperation)):
                node.replace(exp.Literal.string("<subquery>"))
                break
        return self.fp(copy)

    def _subquery(self, cond: Any) -> Any:
        from sqlglot import exp

        return next(n for n in cond.walk() if isinstance(n, (exp.Select, exp.SetOperation)))

    def _place(self, cond: Any) -> str:
        from sqlglot import exp

        if cond.find(exp.Exists) is not None:
            return "EXISTS 子查询"
        if cond.find(exp.In) is not None:
            return "IN 子查询"
        return "条件的子查询"

    # ---- 选出来的列、分组

    def groups(self, A: _Block, B: _Block, where: tuple[str, str]) -> bool:
        """分组键：两边不同、又都只涉及实表列时说各按什么分组（从 SELECT DISTINCT 改写来的说「去重」）。"""
        la, lb = self.sides(where)
        if sorted(self.fp(g) for g in A.group) == sorted(self.fp(g) for g in B.group):
            return True
        if not all(self.base_columns(g, A) is not None for g in A.group) or \
                not all(self.base_columns(g, B) is not None for g in B.group):
            return False

        def say(block: _Block, other: _Block) -> tuple[str, ...]:
            if not block.group:
                return ("没有去重",) if other.deduped else ("没有分组",)
            shown = "、".join(self.show(k, block) for k in block.group)
            return ("对", shown, "去重") if block.deduped else ("按", shown, "分组")

        wa = "、".join(self.show(k, A) for k in A.group) or None
        wb = "、".join(self.show(k, B) for k in B.group) or None
        self.add("group_by", wa, wb, _say(la, *say(A, B), "，", lb, *say(B, A)))
        return True

    def outputs(self, A: _Block, B: _Block, where: tuple[str, str]) -> bool:
        """分组键、选出来的列。分组键不同时，选出来的列只差在分组键那几列上（都排在前面、各是各的分组键）的，算在
        分组键的不同里，不另说；其余逐列比，列数不同、只是先后不同各说一句。"""
        la, lb = self.sides(where)
        ga, gb = [self.fp(g) for g in A.group], [self.fp(g) for g in B.group]
        regrouped = sorted(ga) != sorted(gb)
        covered = self.groups(A, B, where)
        ea, eb = A.exprs, B.exprs
        ta, tb = [self.fp(e) for e in ea], [self.fp(e) for e in eb]
        if ta == tb:
            return covered
        if regrouped and covered:
            ka = next((i for i, t in enumerate(ta) if t not in ga), len(ta))
            kb = next((i for i, t in enumerate(tb) if t not in gb), len(tb))
            if set(ta[:ka]) == set(ga) and set(tb[:kb]) == set(gb) and ta[ka:] == tb[kb:]:
                return covered
        confident = all(self.base_columns(e, A) is not None for e in ea) and \
            all(self.base_columns(e, B) is not None for e in eb)
        if len(ea) != len(eb) or sorted(ta) == sorted(tb):
            if not confident:
                return False
            wa = "、".join(self.show(e, A) for e in ea)
            wb = "、".join(self.show(e, B) for e in eb)
            if len(ea) != len(eb):
                text = _say(la, "选出", str(len(ea)), "列（", wa, "），", lb, "选出", str(len(eb)), "列（", wb, "）")
            else:
                text = _say("两边选出来的列一样，先后不同：", la, "是", wa, "，", lb, "是", wb)
            self.add("expression", wa, wb, text)
            return covered
        for i, (x, y) in enumerate(zip(ea, eb), 1):
            if self.fp(x) == self.fp(y):
                continue
            # 只差在列名上（最外层还按名字排序时别名留着）：取值一样，没什么可说的，也说不清排序那边，交给别的项
            if self.fp(x.unalias()) == self.fp(y.unalias()) or not self.output(x, y, A, B, la, lb, i, len(ea)):
                covered = False
        return covered

    def output(self, x: Any, y: Any, A: _Block, B: _Block, la: str, lb: str, i: int, n: int) -> bool:
        from sqlglot import exp

        if self.base_columns(x, A) is None or self.base_columns(y, B) is None:
            return False
        ga, gb = _aggregate(x.unalias()), _aggregate(y.unalias())
        if ga is not None and gb is not None:
            (fa, arg_a, da), (fb, arg_b, db) = ga, gb
            wa = self.show(arg_a, A) if arg_a is not None else "*"
            wb = self.show(arg_b, B) if arg_b is not None else "*"
            same_arg = (arg_a is None and arg_b is None) or (
                arg_a is not None and arg_b is not None and self.fp(arg_a) == self.fp(arg_b))
            if wa == wb and not same_arg:
                wa, wb = self.show(arg_a, A, full=True), self.show(arg_b, B, full=True)
            if same_arg and fa == fb:
                text = _say(la, "对", wa, _verb(fa, da) + "，", lb, "没去重") if da else \
                    _say(la, "对", wa, _verb(fa, da) + "时没去重，", lb, _verb(fb, db))
            elif same_arg and arg_a is not None:
                text = _say(la, "对", wa, _verb(fa, da) + "，", lb, _verb(fb, db))
            else:
                what_a = ("数的是行数",) if fa == "count" and arg_a is None else ("对", wa, _verb(fa, da))
                what_b = ("数的是行数",) if fb == "count" and arg_b is None else ("对", wb, _verb(fb, db))
                text = _say(la, *what_a, "，", lb, *what_b)
            self.add("aggregate", self.show(x, A), self.show(y, B), text)
            return True
        wa, wb = self.show(x, A), self.show(y, B)
        if wa == wb:
            wa, wb = self.show(x, A, full=True), self.show(y, B, full=True)
        verb = "选的是" if isinstance(x.unalias(), exp.Column) and isinstance(y.unalias(), exp.Column) else "算的是"
        where_col = _say("第", str(i), "列") if n > 1 else ""
        self.add("expression", wa, wb, _say(la, where_col, verb, wa, "，", lb, verb, wb))
        return True

    # ---- HAVING、取行

    def having(self, A: _Block, B: _Block, where: tuple[str, str]) -> bool:
        la, lb = self.sides(where)
        only_a, only_b = self._unmatched(A.having, B.having)
        if not only_a and not only_b:
            return True
        if len(only_a) == 1 and len(only_b) == 1 and self.base_columns(only_a[0], A) is not None and \
                self.base_columns(only_b[0], B) is not None:
            wa, wb = self.show(only_a[0], A), self.show(only_b[0], B)
            self.add("having", wa, wb, _say(la, "分组后只保留", wa, "的组，", lb, "只保留", wb, "的组"))
            return True
        covered = True
        fa, fb = _say(self.la, where[0]), _say(self.lb, where[1])
        for side, rest, block, me, other in ((0, only_a, A, fa, fb), (1, only_b, B, fb, fa)):
            for c in rest:
                if self.base_columns(c, block) is None:
                    covered = False
                    continue
                shown = self.show(c, block)
                self.add("having", shown if side == 0 else None, shown if side == 1 else None,
                         _say(me, "分组后只保留", shown, "的组，", other, "没有这个条件"))
        return covered

    def rows(self, A: _Block, B: _Block, where: tuple[str, str]) -> bool:
        la, lb = self.sides(where)
        if A.limit != B.limit:
            self.add("limit", A.limit, B.limit, _say(la, _rows(A.limit), "，", lb, _rows(B.limit)))
        if A.offset != B.offset:
            self.add("limit", A.offset, B.offset, _say(la, _skip(A.offset), "，", lb, _skip(B.offset)))
        if [self.fp(o) for o in A.order] != [self.fp(o) for o in B.order]:
            if not all(self.base_columns(o, A) is not None for o in A.order) or \
                    not all(self.base_columns(o, B) is not None for o in B.order):
                return False
            wa, wb = self._sorting(A.order, A), self._sorting(B.order, B)
            if A.limit is not None or B.limit is not None:
                self.add("limit", wa, wb, _say(la, "取行前", wa, "，", lb, wb))
            else:
                self.add("order", wa, wb, _say(la, wa, "，", lb, wb))
        if A.distinct != B.distinct:
            me, other = (la, lb) if A.distinct else (lb, la)
            self.add("group_by", "DISTINCT" if A.distinct else None, "DISTINCT" if B.distinct else None,
                     _say(me, "用 SELECT DISTINCT 去掉了重复的行，", other, "没有"))
        return True

    def _sorting(self, order: list[Any], block: _Block) -> str:
        if not order:
            return "没有排序"
        keys = "、".join(_say(self.show(o.this, block), "降序" if o.args.get("desc") else "升序") for o in order)
        return _say("按", keys, "排")

    # ---- UNION

    def setop(self, a: Any, b: Any, where: tuple[str, str], outputs: bool) -> bool:
        from sqlglot import exp

        la, lb = self.sides(where)
        if type(a) is not type(b):
            return False
        xa, xb = _set_branches(a), _set_branches(b)
        if len(xa) != len(xb):
            if isinstance(a, exp.Union) and bool(a.args.get("distinct")) == bool(b.args.get("distinct")):
                self.add("set_op", str(len(xa)), str(len(xb)),
                         _say(la, "合并了", str(len(xa)), "支查询，", lb, "合并了", str(len(xb)), "支"))
            return False
        covered = True
        if bool(a.args.get("distinct")) != bool(b.args.get("distinct")) and isinstance(a, exp.Union):
            wa, wb = _merging(a), _merging(b)
            self.add("set_op", wa, wb, _say(la, "用", wa, "，", lb, "用", wb))
        only_a, only_b = self._unmatched(xa, xb)
        if len(only_a) == 1 and len(only_b) == 1:
            branch = (_deeper(where[0], "合并的一支"), _deeper(where[1], "合并的一支"))
            covered &= self.query(only_a[0], only_b[0], branch, outputs=outputs)
        elif only_a or only_b:
            covered = False
        for key in ("order", "limit", "offset"):
            va = self.fp(a.args[key]) if a.args.get(key) else ""
            vb = self.fp(b.args[key]) if b.args.get(key) else ""
            if va != vb:
                covered = False
        return covered


def _deeper(where: str, place: str) -> str:
    """在哪一段里再往里一层：「在 t 里」+「IN 子查询」→「在 t 的 IN 子查询里」。"""
    if not where:
        return _say("在", place, "里")
    return _say(where[:-1] if where.endswith("里") else where, "的", place, "里")


def _by_bound(conds: list[Any]) -> list[Any]:
    """时间条件给人看时下界在前：>=、> 在前，<、<= 在后，其余照原来的先后。"""
    from sqlglot import exp

    def rank(c: Any) -> int:
        return 0 if isinstance(c, (exp.GTE, exp.GT)) else 1 if isinstance(c, (exp.LT, exp.LTE)) else 2

    return sorted(conds, key=rank)


def _ancestors(node: Any, stop: Any) -> Iterator[Any]:
    node = node.parent
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _set_branches(query: Any) -> list[Any]:
    """连着同一种合并方式的几支（规范形式里已经排好序）。"""
    kind = (type(query), bool(query.args.get("distinct")))
    out: list[Any] = []

    def collect(node: Any) -> None:
        from sqlglot import exp

        if isinstance(node, exp.SetOperation) and (type(node), bool(node.args.get("distinct"))) == kind and \
                (node is query or not any(node.args.get(k) for k in ("order", "limit", "offset"))):
            collect(node.left)
            collect(node.right)
        else:
            out.append(node)

    collect(query)
    return out


def _merging(query: Any) -> str:
    return "UNION 合并（去掉重复的行）" if query.args.get("distinct") else "UNION ALL（重复的行都留着）"


def _aggregate(node: Any) -> tuple[str, Any, bool] | None:
    """光一个聚合（SUM / AVG / COUNT / MIN / MAX）：(函数, 参数（COUNT(*) 是 None）, 是否去重)。"""
    from sqlglot import exp

    kinds = {exp.Sum: "sum", exp.Avg: "avg", exp.Count: "count", exp.Min: "min", exp.Max: "max"}
    func = kinds.get(type(node))
    if func is None:
        return None
    arg = node.this
    distinct = isinstance(arg, exp.Distinct)
    if distinct:
        if len(arg.expressions) != 1:
            return None
        arg = arg.expressions[0]
    if isinstance(arg, exp.Star):
        arg = None
    return func, arg, distinct


def _verb(func: str, distinct: bool) -> str:
    return ("去重" if distinct else "") + _VERBS.get(func, f"用 {func.upper()} 汇总")


def _rows(limit: str | None) -> str:
    if limit is None:
        return "不限行数"
    return f"只取前 {limit} 行" if limit.isdigit() else f"按 LIMIT {limit} 取行"


def _skip(offset: str | None) -> str:
    if offset is None:
        return "不跳过行"
    return f"跳过前 {offset} 行" if offset.isdigit() else f"按 OFFSET {offset} 跳过行"


def _text_diff(a: _Form | None, b: _Form | None, read: str | None) -> tuple[str, ...]:
    """规范化之后两条 SQL 不同的行：difflib 比两边 sqlglot pretty 的输出，「- 」是 A 的、「+ 」是 B 的。"""
    import difflib

    def lines(form: _Form | None) -> list[str]:
        if form is None:
            return []
        try:
            return form.tree.sql(dialect=read, pretty=True, comments=False).splitlines()
        except Exception:  # noqa: BLE001
            return form.text.splitlines()

    out = [line for line in difflib.ndiff(lines(a), lines(b)) if line[:2] in ("- ", "+ ")]
    return tuple(out[:_MAX_DIFF_LINES])


def explain_sql_diff(a: str, b: str, dialect: str | None = None, *, schema: Mapping[str, Any] | None = None,
                     names: Mapping[str, str] | None = None, labels: tuple[str, str] = ("A", "B")) -> SqlDiff:
    """两条 SQL 是不是等价、不是的话差在哪。从不抛异常。

    等价怎么判：两边各自规范化（见 _canonical_forms：sqlglot 优化器那一级、轻量那一级、只重新生成的那一级），每一步都
    只做保持语义的变换；同一级的两个规范形式完全一样（比语法树，见 _Form.key）才算等价，否则一律不等价，items 一定
    不为空。schema 是数据源的 schema_cache，给了就按它认列、展开 SELECT *（表结构要和库对得上）。

    不等价时怎么说：在两边的规范形式上逐段比（见 _Explainer），只把有把握的不同写成具体的中文句子——实表上的时间列
    不同、某个只涉及实表列的筛选条件一边有一边没有（或者同一列上两边的条件不同）、聚合函数或去重计数不同、选出来的
    某一列算法不同、分组键不同、关联的表或关联类型、关联条件、外连接的主表不同、换了一张表、取行和排序不同、UNION
    还是 UNION ALL；两段读的来源对不上、条件里套着说不准的子查询、牵涉 CTE 算出来的列的，不说具体的。具体说法一条都
    没有时给一条 aspect="other" 的兜底说明（「两条 SQL 的写法不同，未能归纳成具体差异，请对照原文」），只覆盖了一部分
    时追加一条「另有未能归纳的差异」，fallback 为真；text_diff 给出规范化之后不同的行。

    labels 是两边的叫法（缺省 A / B，双盲复算传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」: 中文名}，
    可以用 catalog_labels 从数据目录取），时间列写成「支付时间 paid_at」。任一边解析不了：只给一条 aspect="parse"。
    """
    la, lb = labels
    read = _read(dialect)
    ra, ea = _parse(a, read)
    rb, eb = _parse(b, read)
    if ea or eb:
        if ea and eb:
            text = _say(la, "：", ea, "；", lb, "：", eb, "；没有逐项比较")
        else:
            side, error = (la, ea) if ea else (lb, eb)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return SqlDiff(False, [FacetDiff("parse", ea, eb, text)])
    mapping = _schema_mapping(schema)
    forms_a = {} if _lost_in_parsing(a, read) else _canonical_forms(ra, read, mapping)
    forms_b = {} if _lost_in_parsing(b, read) else _canonical_forms(rb, read, mapping)
    for level in (_OPTIMIZER, _LIGHT, _PARSED):
        fa, fb = forms_a.get(level), forms_b.get(level)
        if fa is not None and fb is not None and fa.key == fb.key:
            return SqlDiff(True, level=level)
    level = next((lv for lv in (_OPTIMIZER, _LIGHT) if lv in forms_a and lv in forms_b), None)
    items: list[FacetDiff] = []
    covered = False
    if level is not None:
        explainer = _Explainer(read, (la, lb), names)
        try:
            covered = explainer.statement(forms_a[level], forms_b[level])
            items = explainer.items
        except Exception:  # noqa: BLE001 - 说明本身的缺陷不能连累调用方：当作说不清，记下来修
            logger.warning("SQL 差异说明失败", exc_info=True)
            items, covered = [], False
    shown = level or _PARSED
    if not items:
        items = [FacetDiff("other", None, None, _FALLBACK)]
        fallback = True
    elif not covered:
        items = items + [FacetDiff("other", None, None, _FALLBACK_REST)]
        fallback = True
    else:
        fallback = False
    text_diff = _text_diff(forms_a.get(shown), forms_b.get(shown), read)
    if not text_diff:
        text_diff = tuple(line for line in (f"- {a.strip()}", f"+ {b.strip()}"))
    return SqlDiff(False, items, fallback, text_diff, level)


def diff_facets(a: Facets, b: Facets, labels: tuple[str, str] = ("A", "B"), *,
                names: Mapping[str, str] | None = None) -> list[FacetDiff]:
    """两份 facets 记下的原文交给 explain_sql_diff 比，返回它的 items（含兜底条目）：完全一致（规范形式一字不差）
    才返回空列表，不等价时一定不为空。

    labels、names 见 explain_sql_diff。任一边解析不了：只返回一条 parse。不是 facets 算出来的 Facets（没记原文）
    比不了原文：同一个对象才返回空列表，否则返回一条兜底说明（facets 记的结构不全，两份一样不说明 SQL 一样）。
    """
    la, lb = labels
    if a.parse_error or b.parse_error:
        if a.parse_error and b.parse_error:
            text = _say(la, "：", a.parse_error, "；", lb, "：", b.parse_error, "；没有逐项比较")
        else:
            side, error = (la, a.parse_error) if a.parse_error else (lb, b.parse_error)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return [FacetDiff("parse", a.parse_error, b.parse_error, text)]
    if a._origin is None or b._origin is None:
        return [] if a is b else [FacetDiff("other", None, None, _FALLBACK)]
    (sql_a, schema_a), (sql_b, schema_b) = a._origin, b._origin
    return explain_sql_diff(sql_a, sql_b, a.dialect or b.dialect, schema=schema_a or schema_b, names=names,
                            labels=labels).items


__all__ = ["MAX_SQL_CHARS", "PLACEHOLDER", "Aggregate", "ColumnRef", "Computed", "FacetDiff", "Facets", "JoinFacet",
           "LimitFacet", "NormalizedSql", "Part", "SqlDiff", "TimePredicate", "catalog_labels", "diff_facets",
           "explain_sql_diff", "facets", "normalize_sql"]
