"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），人据此选
A / B，只差一个方面、又对得上数据目录里的事实（时间归属、有效行过滤、去重）时选择会写进数据目录；运行对比要回答
「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了）；反方审稿要看自己的查询和原
查询的口径是否一致。几处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、普通注释、不加引号的名字的大小写都不算不同；结构变了的一定不同——解析时会
  丢掉写法的（MySQL 会执行的 /*! … */ 注释、写成 ! 的 NOT、SQLite 的 MOD() 这类重新生成时换了名字的函数）改按原文
  逐词规范化，会执行的注释里的代码也算结构。
- explain_sql_diff：两条 SQL 是不是等价（SqlDiff.equivalent），不是的话差在哪（items、text_diff）。
- diff_facets：照旧收两份 facets，交给 explain_sql_diff 比两份记下的原文，返回它的 items（含兜底条目）：完全一致
  （规范形式一字不差）才返回空列表。
- facets：把一条查询拆成表、连接、WHERE 合取式、时间条件、分组、聚合、算出来的列、HAVING、取哪几行（给调用方取
  结构用；CTE、子查询按位置编号，见 _Statement）。facets 不受下面等价判定的改动影响。

**等价怎么判。** 过去几轮按方面归纳任意 SQL，每轮修好一批又冒出一批（还回退过），收不拢；现在先判等价、再说差在哪。
两边各自规范化，规范化的每一步都只做保持语义的变换，同一级的两个规范形式一字不差（比语法树逐个节点的类型和参数，
见 _fingerprint，括号不算；不比生成的 SQL：sqlglot 按方言生成时会把不同的写法写成一样，SQLite 的 CAST(x AS DATE)
生成 DATE(x)）才算等价，否则一律不等价、条目一定不为空。原文一字不差的两条直接算等价。规范形式分三级（见
_canonical_forms），哪一级对上都算：

- optimizer：先按方言统一名字（_normalize_names），CTE、子查询的列名表写成里面那段的列别名，USING 写成 ON，FETCH
  FIRST 写成 LIMIT，IS NOT NULL、NOT LIKE 这类带 negate 的写法统一成 NOT (…)；然后 sqlglot 的 qualify 认列（给了
  数据源的 schema_cache 就按它认、展开 SELECT *）；再把整条语句的来源别名改成互不相同的，引用了好几次、只筛行选列的
  CTE 每处写回一份，只引用一次的 CTE 写回用到它的地方，只从几张表筛行选列的子查询并进外层（_merge_derived：只并
  内连接一侧的），外层 WHERE 里只涉及分组子查询选出来的列的条件挪进它的 HAVING（同一个值），去掉没用的 CTE。
- light：不认列，只做上面不需要认列的几步。
- parsed：解析后只统一名字的大小写、引号。

两级共用的几步（见 _finish，各函数的说明写了为什么保持语义）：原样透出子查询全部列的一段换成那个子查询；两个来源
的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的行筛掉了的写成内连接（_rejects 分「一定是空值」和
「一定不成立」：NOT 只能套前者，NOT IN 子查询、NOT IN ()、x op ALL (…)、NOT (x = ANY …)、PostgreSQL 的 || 都留得下
没关联上的行）；不分组、结果只是跳过空值的聚合、参数在可空一侧为空时一定为空的，LEFT JOIN 写成内连接
（_null_blind_left_joins：COALESCE(SUM(r.amount), 0) 和 INNER JOIN 一样）；ON 之外没用到、结果又不怕重复的行（去重、
只有 MIN / MAX / 去重的聚合、IN / EXISTS 子查询）的 LEFT JOIN 去掉；开头那组内连接的条件挪进 WHERE；WHERE、HAVING、
ON 里的 TRUE、1 = 1、分组后的 COUNT(*) >= 1 去掉（没有 GROUP BY、别处也没有聚合的 HAVING TRUE 留着：PostgreSQL
里它让语句只出一行）；同一组 AND 里另一项已经保证不为空、或者表结构里确定不为空（NOT NULL、主键）的 x IS NOT NULL
去掉；给了表结构、两列声明的类型一样又「相等就是一样」时，WHERE 里两列相等的，SELECT / GROUP BY / HAVING / ORDER BY
里都换成同一列（_substitute_equal_columns）；给了表结构时一定不为空的列上的 COUNT(x) 写成 COUNT(*)、选出来的列包含
唯一的表的主键时 DISTINCT 去掉（_constraint_rewrites）；按唯一键关联、又没用到的 LEFT JOIN 去掉；EXISTS 子查询写成 SELECT 1（去重、分组、排序、取前 n 行、UNION 还是
UNION ALL 都不影响）；IN 子查询里不影响取值集合的 DISTINCT、GROUP BY、ORDER BY 去掉、UNION 写成 UNION ALL（只在
那一列「相等就是一样」时）；SELECT DISTINCT 写成按选出来的列 GROUP BY、已经按选出来的列分组的 DISTINCT 去掉；
COUNT(1) 写成 COUNT(*)、MIN / MAX 里的 DISTINCT 去掉；SQLite 的 IIF、MySQL 的 IF 写成 CASE，CASE 的 ELSE NULL 去掉；
IN 列表去重排序、只有一个常量的写成 =；BETWEEN 写成两个比较（SYMMETRIC、MySQL 里上下界类型不同或不是常量的不改）；
NOT 往里推：套比较写成反过来的比较（ANY / ALL 不动）、NOT NOT 条件去掉、德摩根律（三值逻辑下照样成立）；LIMIT / OFFSET 的整数算式算成一个数（SQLite、
PostgreSQL），SQLite 的负数 LIMIT、PostgreSQL 的 LIMIT NULL 写成不限行数；ORDER BY 的 ASC、OFFSET 0、和前面一模一样
的排序键、分组键去掉；最外层 ORDER BY 引用的别名换成表达式，最外层和条件里的子查询选出来的列名去掉；最后起规范的
名字、交换律成立的地方排好序。

**不走规范化两级的（只比解析后的原样）**：不确定的函数（RANDOM()：两次求值不是同一个值，去重 AND / OR / IN 列表里
的项、并子查询都会改变求值次数）；递归 CTE（SQLite 不写 RECURSIVE 的也是）、LATERAL、表函数、VALUES、实表别名带
列名、ROLLUP 这些结构；子查询里写了表名的列、而这个别名在里外层各指一个不同来源、认不准是哪一个的（SQLite 里层找
不到这一列就往外层找）。optimizer 一级另外不出的：认不全列；WHERE / GROUP BY / HAVING 里、或者套着的子查询里有和
列别名同名、又可能是表的列的名字（SQLite 的 WHERE、ON、相关子查询都认列别名）；FROM 里的子查询、CTE 选出来的列有
同名的。原文里有 sqlglot 解析时会丢掉、语义却不同的写法（一元加号、0x10 和 x'10' 这类字面量、换了名字的类型名、
MySQL 会执行的注释、写成 ! 的 NOT）不在语法树上判等价；MySQL 里后面不跟空白的 -- 是两个减号，先拆开再解析；函数
节点上记下原文写的名字（_mark_surfaces：SQLite 的 MOD() 和 % 解析成同一个节点，记下名字就不会判成等价）。

**故意不做的改写**（复验在这些地方抓到过不保持语义的例子）：sqlglot 的 simplify（数字常量按 Decimal 折叠，SQLite
按浮点数算 0.1 + 0.2 ≠ 0.3；-(-x) 化成 x；BETWEEN SYMMETRIC 丢了 SYMMETRIC）、unnest_subqueries、canonicalize、
pushdown_projections；HAVING 里的条件挪进 WHERE；只有一项、又不是常量的 IN 列表写成 =（SQLite 里 a IN (b) 是
a = +b）；改列别名、把 EXISTS 写成 SELECT 1、去掉列名之前，本段（含 WHERE、ON、相关子查询）还按名字引用这些别名的
（见 _alias_refs）；USING 的列和某个列别名同名的（ORDER BY 里单独一个名字先认列别名）。

**结果看行出来的先后的一段（见 _Sensitivity）不调换来源的先后、不排 GROUP BY 的键、不改 DISTINCT、不并子查询、不把
引用了好几次的 CTE 拆开，也不按 WHERE 把外连接改成内连接、不去掉条件（连一模一样写了两遍的也不去）、不去重排序键和
分组键、不换列、不把 COUNT(x) 换成 COUNT(*)、不把 BETWEEN 拆成两个比较、不去掉 MIN / MAX 里的 DISTINCT**：取行（LIMIT / OFFSET / FETCH，有
并列时取哪几行也看先后）、DISTINCT ON、窗口函数、拼串拼数组的聚合（GROUP_CONCAT）、聚合里既不分组也不聚合的列、
当成标量用的子查询，去重、分组、MIN / MAX、去重的 UNION 留下的值「相等却不一样」的时候（MySQL 默认不分大小写的
字符串、PostgreSQL numeric 的 2.0 和 2.00、算出来的表达式），以及 PostgreSQL、MySQL 里对不知道是不是精确数的值求和；
给这样一段供行的子查询、CTE 也算。第 3 轮压测在真实的库上看到：SQLite 里 WHERE 让 LEFT JOIN 降成内连接之后，扫描
的先后和直接写内连接的不一样（EXPLAIN QUERY PLAN 一个先扫 refunds、一个先扫 orders）；MySQL 里同一个条件写两遍，按
条件估算的行数变了、换了关联的先后——GROUP_CONCAT 拼出来的先后都跟着变。

**假设（做不到的）**：SQLite 没给表结构时按列有声明的类型、默认的 BINARY 排序规则算——声明了 COLLATE NOCASE 的列、
没有类型亲和的列（能同时存 1 和 1.0）表结构里看不出来；比较两边都是列时对调两边（SQLite 比较两列用左边那列声明的
排序规则）。SQLite 的求和当成和先后无关。PostgreSQL、MySQL 没给表结构时一律不当成「相等就是一样」、也不当成精确数。
MySQL 的表名按 Linux 上分大小写算。主键、NOT NULL 只认 schema_cache 里的 nullable 和 primary_key（SQLite 只有单独一列、
声明成 INTEGER 的主键算不为空）；没给这两项时，WHERE r.id IS NOT NULL 就照实说成「排除了 r.id 为空的行」。

等价的结论另有执行比对守门（tests/test_sql_equivalence.py）：虚构库上五组随机数据、一组极端数据、一组专挑写法差异的
数据，判等价的两条每组结果都得一样，结果不一样的绝不能判等价。

**具体说法的门槛（见 _Explainer）。** 在两边的规范形式上逐段比，只把有把握、确实不同的地方写成具体的中文句子：

- 来源对得上（一模一样，或者只差一处说得清的：换了一张表、一边多关联了一张表、同一张表的关联类型或条件不同、
  外连接的主表对调）才往下比；换了表的那一边，列照它自己的表说。FROM 里的子查询只有一处不同时往里比（「在 t 里」）。
- 说「X 不一样」之前先确认真的不一样：同一个对象只和常量比的几个条件按取值逐段算一遍（_Range），留下的行一样的
  不说；关联条件只是两列相等的，比相等关系分出的组；聚合只在参数是同一列、或者各是一列时说；被 WHERE 里两列相等
  定成同一个值的列，选哪一列、按哪一列分组、按哪一列排序都不说（类型不知道时也说不准是不是一样）。
- 说「另一边没有这个条件 / 没有按时间筛选」之前，先确认另一边整条语句（含子查询、CTE、相关子查询里引用外层的列、
  CASE 里、判空时连聚合的参数）都没用这些列筛选（_Usage），没有像时间的列、像日期的常量。
- 「排除了 X = v 的行」时照实带上「和 X 为空的行」（<>、NOT IN、NOT LIKE 也筛掉空值；表结构里确定不为空的列不带）；
  NOT 套着几个条件的说「只保留 NOT (…) 的行」。
- 条件里的子查询：两边除了子查询之外写法一模一样（含 NOT、IN 左边的列）的才配成一对往里比，位置照实说；IN / EXISTS
  子查询里去不去重、分不分组、UNION 还是 UNION ALL 不说（只看在不在、有没有）。
- 只是排序不同（order）只在整条语句最外层、没有取行、DISTINCT ON、窗口函数、不是只出一行（不分组的聚合、分组键都被
  WHERE 里的「键 = 常量」定死）时说；取行前的排序算 limit。
- LEFT JOIN 对 INNER JOIN、「A 用 LEFT JOIN 关联了 X、B 没有」只在整条语句最外层、WHERE 里没用到可空一侧的列、
  有没有这次 LEFT JOIN 结果可能不同（不是只取可空一侧跳过空值的聚合；不是没用到 X、又不怕重复的行）时说；「A 关联了
  X、B 没有关联它（只在子查询里用到了 X）」在 A 不怕重复的行（可能只是 IN / EXISTS 改写成了关联）时不说。
- UNION 对 UNION ALL 在 UNION ALL 那边的各支自己都已经去重（有没有重复要看各支相不相交）时不说。
- 自己关联自己、几个来源同名的，关联条件写出来分不清谁是谁，不说。
- 说的都是「这一段的这一项两边各写的是什么」；两边给人看的写法一样、和已经说过的一句正好反过来的不说。

**兜底。** 规范形式不一致、一条具体说法都没有时，给一条 aspect="other" 的「两条 SQL 的写法不同，未能归纳成具体差异，
请对照原文」；具体说法只覆盖了一部分时追加「另有未能归纳的差异，请对照原文」；fallback 为真。text_diff 给出规范化后
不同的行（difflib 比两边 sqlglot pretty 的输出，最多 40 行）。

仍做不到的（只给兜底说明，或者认不出等价）：
- 认不出的等价改写：IN 子查询和 EXISTS、NOT IN 和 NOT EXISTS 互换（空值时本来就不等价）、IN / EXISTS 子查询和关联
  同一张表再去重、条件挪进相关子查询、IN 和 OR、冗余的条件、COALESCE 和 CASE 这类写法不同的等价表达式、窗口函数的
  PARTITION BY 换先后、HAVING 里分组键上的条件和 WHERE、去重的 UNION / INTERSECT 调换各支、SQLite 里套在子查询里的
  UNION、三项以上的加乘换结合、和方言有关的同义写法（DATE(x) 和 CAST(x AS DATE)、不在 _SAFE_RESPELLINGS 里的函数
  别名）、各支互不相交的 UNION ALL 和 UNION、只出一组时的排序、LATERAL、递归 CTE、实表别名带列名、USING 用在第一个
  连接以外的地方；没给表结构时 WHERE、GROUP BY、HAVING、相关子查询里用列别名的；结果看行出来的先后的一段调换关联
  先后、按 WHERE 把外连接改成内连接、换成相等的列；没给表结构（或者类型不一样）时按关联条件相等的两列换着分组、排序；
  PostgreSQL、MySQL 没给表结构时去重、分组、求和的一段调换关联先后、并子查询、去掉没用到的 LEFT JOIN、把只取可空一侧
  聚合的 LEFT JOIN 写成内连接；没给 nullable / primary_key 时主键上的 IS NOT NULL。
- 说不出具体差异、只给兜底的：自己关联自己的方向或角色不同、几个派生来源互相交错、相关子查询内外层的条件、
  另一边别处（别的子查询、CTE）也用到同一列筛选的、读的来源对不上、CTE 和子查询里选出来的列不同、条件或算法写法
  不同又判断不了是不是真不一样的、EXCEPT 两边对调、IN / EXISTS 子查询里去重分组的不同、被关联条件定成相等的列、
  几处不同交错在一起的。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就只给一条 aspect="parse"；从不抛异常，
规范化或说明本身出了意外就当作这一级没有、说不清，记日志。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import math
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
    不了就直接对原文逐词规范化，带上 parse_error。解析时会丢掉写法的（MySQL 会执行的 /*! … */ 注释、写成 ! 的 NOT、
    SQLite 的 MOD() 这类重新生成时换了名字的函数，见 _lost_in_parsing）也对原文逐词规范化（不带 parse_error）：
    重新生成会把不同的结构写成一样。MySQL 里后面不跟空白的 -- 是两个减号，先拆开（见 _mysql_dashes）。从不抛异常。
    """
    read = _read(dialect)
    if read == "mysql" and isinstance(sql, str):
        sql = _mysql_dashes(sql)
    root, problem = _parse(sql, read)
    if root is not None and (_lost_in_parsing(sql.strip(), read, root) or _respelled(sql.strip(), read, root)):
        # 解析时会丢掉写法的（MySQL 会执行的注释、!、SQLite 的 MOD()……）：重新生成的文本会把不同的结构写成一样，
        # 改按原文逐词规范化（会执行的注释里的代码也算进去）
        return _by_tokens(sql, read, None)
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
    """逐词规范化（解析不了的 SQL、解析时会丢掉写法的 SQL）：关键字转大写，不加引号的名字按 name_key，带引号的名字
    原样，字面量换成占位符；! 和 NOT 各写各的。MySQL 会执行的注释（/*!版本号 … */）照样逐词规范化后写进去：里面是
    要执行的代码，版本号算结构（决定执不执行），里面的字面量照样换成占位符。普通注释不算。"""
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
            words.append(raw if token.token_type.name == "NOT" and token.text == "!" else token.text.upper())
        for comment in token.comments or []:
            found = re.match(r"\s*(M?!)(\d*)(.*)", comment, re.S)
            if found is None:
                continue
            inner = _by_tokens(found.group(3), read, None)
            words += [f"/*{found.group(1)}{found.group(2)}", inner.text, "*/"]
            literals += inner.literals
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
#: 写的是 LEFT / RIGHT JOIN、WHERE 把没关联上的行筛掉了、按内连接比的来源（记在那张表上，值是原来写的连接）：差异
#: 说明里照实说「写的是 LEFT JOIN，实际是 INNER JOIN」
_REJECTED = "sql_semantics_rejected"
#: 从 RIGHT JOIN 改写成的 LEFT JOIN（记在 JOIN 上）：差异说明里不说「用 LEFT JOIN」，说以哪一侧为主表
_FLIPPED = "sql_semantics_flipped"
#: GROUP BY 是从 SELECT DISTINCT 改写来的：差异说明里说「去重」
_FROM_DISTINCT = "sql_semantics_distinct"
#: 认不准是哪一列的列（记在列名的标识符上）：SQLite 里不带表名、带双引号、表结构里又没有的名字（找不到这一列时被当成
#: 字符串）；条件、标量子查询里不带表名的列（没给表结构时 qualify 归给子查询里的表，可它也可能是外层的列）。差异说明
#: 不拿它说具体的
_UNSURE = "sql_semantics_unsure"
#: 不确定的函数：每次求值结果都可能不同（两次 RANDOM() 不是同一个值），去重 AND / OR / IN 列表里一模一样的项、并子查询、
#: 合并定义一样的 CTE、改写 BETWEEN 都会改变求值的次数。有就不走规范化的两级（只比解析后的原样）
_VOLATILE = frozenset({"random", "rand", "randn", "newid", "uuid", "gen_random_uuid", "uuid_generate_v4", "random_uuid",
                       "randomblob", "setseed", "nextval", "sys_guid", "changes", "last_insert_rowid", "total_changes",
                       "sysdate", "clock_timestamp", "timeofday", "uuid_short", "random_bytes", "gen_random_bytes",
                       "pg_sleep", "sleep", "currval", "lastval", "last_insert_id", "found_rows", "row_count",
                       "get_lock", "release_lock", "benchmark", "uuid_v4", "uuidv4", "uuidv7", "gen_random_uuid_v7"})
#: 结果看行出来的先后的聚合：拼成串、拼成数组、取任意一个、取第一个 / 最后一个（类名和函数名都按小写去下划线比）
_ORDERED_AGGS = frozenset({"groupconcat", "stringagg", "arrayagg", "arrayconcatagg", "arrayuniqueagg", "jsonarrayagg",
                           "jsonobjectagg", "jsonagg", "jsonbagg", "jsongrouparray", "jsongroupobject", "listagg",
                           "xmlagg", "anyvalue", "first", "last", "firstvalue", "lastvalue", "nthvalue",
                           "jsonbobjectagg", "jsonobjectaggstrict", "arrayaggdistinct", "bitstringagg"})
#: 浮点数求和类的聚合：浮点数加法不满足结合律，加的先后不同结果的最后几位就不同（1902.2 和 1902.2000000000003）
_FLOAT_AGGS = frozenset({"sum", "avg", "stddev", "stddevpop", "stddevsamp", "variance", "variancepop", "varpop",
                         "varsamp", "covarpop", "covarsamp", "corr", "total"})
#: 拿空值做比较、算术都得空值（外连接没关联上的那一侧整行是空值）
_STRICT_UNARY = ("Neg", "Cast", "Lower", "Upper", "Length", "Abs")
_STRICT_BINARY = ("Add", "Sub", "Mul", "Div", "Mod")
#: 解析时只留下值、丢了写法的字面量：SQLite 里 0x10 是整数 16、x'10' 是 BLOB，解析出来是同一个节点；N'…'、E'…'、
#: $$…$$ 这些也一样，带了就不在语法树上判等价
_LOSSY_LITERALS = frozenset({"HEX_STRING", "BIT_STRING", "BYTE_STRING", "NATIONAL_STRING", "UNICODE_STRING",
                             "HEREDOC_STRING", "RAW_STRING"})
#: 类型名的同义写法：解析后重新生成换了个名字、在这个方言里却是同一个类型（SQLite 按名字里的字样定类型亲和：INT 和
#: INTEGER 都是整数亲和，VARCHAR 和 TEXT 都是文本亲和）。不在这里的换了名字就算解析丢了写法（SQLite 的 STRING 是数值
#: 亲和、TEXT 是文本亲和，sqlglot 解析成同一个类型）
_TYPE_SYNONYMS = {
    "sqlite": {"INT": "INTEGER", "VARCHAR": "TEXT", "CHAR": "TEXT", "NVARCHAR": "TEXT", "CLOB": "TEXT",
               "DOUBLE": "REAL", "FLOAT": "REAL"},
    "postgres": {"INTEGER": "INT", "INT4": "INT", "INT8": "BIGINT", "INT2": "SMALLINT", "FLOAT8": "DOUBLE",
                 "FLOAT4": "REAL", "BOOL": "BOOLEAN", "NUMERIC": "DECIMAL", "CHARACTER": "CHAR"},
    "mysql": {"INTEGER": "INT", "DEC": "DECIMAL", "NUMERIC": "DECIMAL", "BOOL": "BOOLEAN"},
}
#: FROM 列表到这些词就结束了（之后的逗号不再是连接）
_CLAUSE_ENDS = frozenset({"WHERE", "GROUP_BY", "HAVING", "ORDER_BY", "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT",
                          "EXCEPT", "WINDOW", "QUALIFY", "SEMICOLON", "RETURNING", "FOR", "CONNECT_BY", "START_WITH",
                          "CLUSTER_BY", "DISTRIBUTE_BY", "SORT_BY", "PREWHERE", "LOCK"})
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_FLIP = {"GT": "LT", "LT": "GT", "GTE": "LTE", "LTE": "GTE", "EQ": "EQ", "NEQ": "NEQ"}
_NEGATE = {"EQ": "NEQ", "NEQ": "EQ", "GT": "LTE", "LTE": "GT", "GTE": "LT", "LT": "GTE"}


class _Unsupported(Exception):
    """这一级规范化处理不了这条语句的结构（递归 CTE、LATERAL、表函数……），这一级不出规范形式。"""


#: 一元加号前面可能是这些词（不是一个值的结尾）
_OPERAND_KEYWORDS = frozenset({"SELECT", "WHERE", "AND", "OR", "NOT", "ON", "WHEN", "THEN", "ELSE", "HAVING", "BY", "IN",
                               "IS", "LIKE", "GLOB", "BETWEEN", "CASE", "AS", "DISTINCT", "ALL", "SET", "VALUES",
                               "LIMIT", "OFFSET", "RETURN", "ESCAPE", "FILTER", "OVER", "PARTITION", "USING", "IF",
                               "EXISTS", "ANY", "SOME", "FROM", "JOIN", "WITH", "ORDER", "GROUP"})


#: 函数名的同义写法（原文写的名字 → 规范的名字，空串是规范形式里不是函数调用）：在这个方言里意思确实一样，sqlglot
#: 也解析成同一个节点。别的写法解析成同一个节点、意思却不同的（SQLite 的 MOD(12.5, 2) 是 0.5，12.5 % 2 先转整数得 0；
#: PostgreSQL 的 LOG10(x) 是浮点数、LOG(10, x) 是 numeric；SQLite 没有 CHAR_LENGTH，sqlglot 读成 LENGTH），两边写的
#: 函数名不一样就不判等价（见 _call_signature）。每一项都对着方言的文档核过：
#: - SQLite：IFNULL(a, b) 就是两个参数的 COALESCE；SUBSTR 和 SUBSTRING、POW 和 POWER、CEILING 和 CEIL 是同一个函数；
#:   IIF 和 CASE 生成同样的字节码；like(x, y) 就是 y LIKE x、glob(x, y) 就是 y GLOB x（SQLite 文档原话）。
#: - PostgreSQL：substr 和 substring、char_length 和 length（按字符）、now() 和 current_timestamp、pow 和 power、
#:   ceiling 和 ceil、mod(a, b) 和 a % b、variance 和 var_samp、btrim(x) 和 trim(x) 都是同一个。
#: - MySQL：IFNULL 和两个参数的 COALESCE、SUBSTR 和 SUBSTRING、INSTR(s, t) 和 LOCATE(t, s)、POSITION(t IN s) 和
#:   LOCATE(t, s)、POW 和 POWER、CEILING 和 CEIL、MOD(a, b) 和 a % b、LCASE / UCASE 和 LOWER / UPPER、一个参数的 LOG
#:   和 LN、ISNULL(x) 和 x IS NULL、IF 和 CASE 都是同一个。
_SAFE_RESPELLINGS: dict[str, dict[str, str]] = {
    "sqlite": {"IFNULL": "COALESCE", "SUBSTR": "SUBSTRING", "POW": "POWER", "CEILING": "CEIL", "IIF": "", "LIKE": "",
               "GLOB": ""},
    "postgres": {"SUBSTR": "SUBSTRING", "CHAR_LENGTH": "LENGTH", "CHARACTER_LENGTH": "LENGTH", "NOW": "",
                 "POW": "POWER", "CEILING": "CEIL", "MOD": "", "VARIANCE": "VAR_SAMP", "BTRIM": "TRIM"},
    "mysql": {"IFNULL": "COALESCE", "SUBSTR": "SUBSTRING", "INSTR": "LOCATE", "POSITION": "LOCATE", "POW": "POWER",
              "CEILING": "CEIL", "MOD": "", "LCASE": "LOWER", "UCASE": "UPPER", "LOG": "LN", "ISNULL": "", "IF": ""},
}
#: 后面跟着括号却不是函数名的词
_NOT_CALLS = frozenset({"IN", "EXISTS", "OVER", "VALUES", "AS", "ON", "USING", "FILTER", "WITHIN", "ANY", "ALL", "SOME",
                        "ROW", "AND", "OR", "NOT", "WHERE", "SELECT", "FROM", "JOIN", "HAVING", "BY", "WHEN", "THEN",
                        "ELSE", "CASE", "UNION", "INTERSECT", "EXCEPT", "WITH", "RECURSIVE", "LATERAL", "TABLE",
                        "INTO", "SET", "DISTINCT", "LIMIT", "OFFSET", "FETCH", "ORDER", "GROUP", "PARTITION", "RANGE",
                        "ROWS", "BETWEEN", "IS", "ESCAPE", "INTERVAL", "RETURN", "NULLS", "OUTER", "INNER", "CROSS",
                        "NATURAL", "FULL", "MATERIALIZED", "QUALIFY", "WINDOW", "TUPLE"})


def _mysql_dashes(text: str) -> str:
    """MySQL 里 -- 后面要跟空白（或控制字符）才是注释，不然就是两个减号：credit--1 是 credit - (-1)（MySQL 文档的
    原例）。sqlglot 一律当成注释、把后面整行吃掉，这里先把这样的 -- 拆成 「- -」 再交给它解析，意思不变。字符串
    （'…'、"…"，反斜杠和成对的引号转义）、反引号里的名字、/* … */ 和 # 注释、真正的 -- 注释里面的不动。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(text[i:j])
            i = j
            continue
        if ch == "#" or (text.startswith("--", i) and (i + 2 >= n or text[i + 2].isspace() or ord(text[i + 2]) < 32)):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("--", i):
            out.append("- ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _executable_comment(tokens: list[Any]) -> bool:
    """MySQL / MariaDB 会执行 /*! … */、/*M! … */ 里的代码（版本号写在 ! 后面的，服务器版本够了才执行），sqlglot 当成
    普通注释扔掉。别的库里这只是注释，一样不在语法树上判等价（只会少认几对等价）。"""
    return any(c.lstrip().startswith(("!", "M!")) for t in tokens for c in (t.comments or []))


def _call_names(tokens: list[Any]) -> list[str]:
    """原文里写成「名字(」的函数名（大写）。IN (、EXISTS (、OVER ( 这类后面跟括号的关键字不算。"""
    out = []
    for token, nxt in zip(tokens, tokens[1:]):
        if nxt.token_type.name == "L_PAREN" and token.token_type.name not in ("STRING", "NUMBER", "IDENTIFIER") \
                and _PLAIN_NAME.match(token.text or "") and token.text.upper() not in _NOT_CALLS:
            out.append(token.text.upper())
    return out


#: 原文写的函数名（按同义写法换算之后）和 sqlglot 重新生成的不一样时，记在函数节点的 meta 里，比等价时算进去（见
#: _mark_surfaces、_fingerprint）
_SURFACE = "sql_semantics_surface"


def _mark_surfaces(tree: Any, text: str, read: str | None) -> bool:
    """函数节点上记下原文写的名字（换算成规范的同义写法，见 _SAFE_RESPELLINGS）——只在它和 sqlglot 重新生成的名字
    不一样时记：sqlglot 把方言里意思不同的写法解析成同一个节点时（SQLite 的 MOD(a, 2) 和 a % 2 都是 Mod；PostgreSQL 的
    LOG10(x) 和 LOG(10, x) 都是 Log），两边的语法树就因为这一项而不同，不会判等价。解析器在函数节点的 meta 里留着
    函数名在原文里的位置（start / end），按位置取原文。

    返回能不能记全：原文写的某个函数名在重新生成的写法里不见了（又不是同义写法），却没有哪个节点记下了它（解析器
    换了写法、没留位置，PostgreSQL 的 DATE_PART(…) 解析成 EXTRACT 就是这样）时返回 False，调用方就不在语法树上判等价。"""
    from sqlglot import exp

    mapping = _SAFE_RESPELLINGS.get(read or "", {})
    attached: set[str] = set()
    leaves = (exp.Column, exp.Identifier, exp.Literal, exp.Star, exp.Null, exp.Boolean, exp.Var, exp.Placeholder,
              exp.Parameter, exp.DataType)
    for node in tree.walk():
        if isinstance(node, leaves):
            continue
        start, end = node.meta.get("start"), node.meta.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end < len(text):
            continue
        written = text[start:end + 1].upper()
        # 只认写成「名字(」的函数调用（Mod 这类运算节点从 MOD(…) 解析出来时也带着名字的位置）
        if not _PLAIN_NAME.match(written) or not text[end + 1:].lstrip().startswith("("):
            continue
        written = mapping.get(written, written)
        try:
            generated = node.sql(dialect=read, comments=False)
        except Exception:  # noqa: BLE001 - 生成不了：照写的记
            generated = ""
        found = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", generated)
        if written and (found is None or found.group(1).upper() != written):
            node.meta[_SURFACE] = written
            attached.add(text[start:end + 1].upper())
    try:
        tokens = _tokens(text, read)
        present = {t.text.upper() for t in _tokens(tree.sql(dialect=read, comments=False), read)}
    except Exception:  # noqa: BLE001 - 切词、重新生成不了：当成记不全
        return False
    # CAST 一类（解析成 Cast 节点、不留位置）换写法也是同一个意思（CAST(x AS INT) 和 x::INT、MySQL 的 CONVERT(x, CHAR)），
    # 也不会和别的函数撞成同一个节点（SQLite 的 CAST(x AS DATE) 重新生成成 DATE(x)，可 DATE(x) 解析出来是 Date 节点）
    return all(name in present or name in mapping or name in attached or name in ("CAST", "TRY_CAST", "CONVERT")
               for name in _call_names(tokens))


def _respelled(text: str, read: str | None, tree: Any) -> bool:
    """解析后重新生成时，原文写的函数名不见了、又不是这个方言里的同义写法（normalize_sql 用：重新生成的文本会把
    MOD() 和 % 写成一样，这样的语句改按原文逐词规范化）。"""
    try:
        tokens = _tokens(text, read)
        again = _tokens(tree.sql(dialect=read, comments=False), read)
    except Exception:  # noqa: BLE001 - 切词、重新生成不了：当成丢了写法
        return True
    present = {t.text.upper() for t in again}
    safe = _SAFE_RESPELLINGS.get(read or "", {})
    return any(name not in present and name not in safe for name in _call_names(tokens))


def _type_words(tokens: list[Any], read: str | None) -> list[str]:
    from sqlglot.parser import Parser

    synonyms = _TYPE_SYNONYMS.get(read or "", {})
    words = [t.text.upper() for t in tokens if t.token_type in Parser.TYPE_TOKENS]
    return sorted(synonyms.get(w, w) for w in words)


def _lost_in_parsing(text: str, read: str | None, tree: Any) -> bool:
    """原文里有 sqlglot 解析时丢掉、语义却不一样的写法，有就不在语法树上判等价：

    - 一元加号：SQLite 里 +a 去掉了列的类型亲和，+a = '10' 和 a = '10' 结果不同，解析出来却是同一棵树；
    - 只留下值、丢了写法的字面量（0x10 和 x'10'，见 _LOSSY_LITERALS）；
    - 会执行的注释（MySQL 的 /*! … */，见 _executable_comment）；
    - 写成 ! 的 NOT：MySQL 里 ! 比比较运算结合得紧（! status = 'paid' 是 (!status) = 'paid'），NOT 比比较结合得松，
      sqlglot 都按 NOT 读；
    - 类型名：解析后重新生成换了名字、又不是这个方言里的同义写法（SQLite 的 CAST(x AS STRING) 是数值亲和，解析成
      和 CAST(x AS TEXT) 一样的节点）。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 切词都不行的，解析也过不了，那边会说
        return False
    if _executable_comment(tokens):
        return True
    prev = None
    for token in tokens:
        kind = token.token_type.name
        if kind in _LOSSY_LITERALS or (kind == "NOT" and token.text == "!"):
            return True
        if kind == "PLUS":
            if prev is None:
                return True
            pk, word = prev.token_type.name, prev.text
            operand = pk in ("VAR", "IDENTIFIER", "NUMBER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE", "END",
                             "STAR") or pk.endswith("STRING") or (
                _PLAIN_NAME.match(word) is not None and word.upper() not in _OPERAND_KEYWORDS)
            if not operand:
                return True
        prev = token
    try:
        again = _tokens(tree.sql(dialect=read, comments=False), read)
    except Exception:  # noqa: BLE001 - 重新生成不了：当成丢了写法
        return True
    return _type_words(tokens, read) != _type_words(again, read)


def _mark_comma_joins(tree: Any, text: str, read: str | None) -> None:
    """SQLite 里逗号连接和 CROSS JOIN 解析成同一个节点（都是 kind=CROSS），可 SQLite 不调换 CROSS JOIN 两边的先后、
    逗号连接会调换：结果看行出来的先后时（GROUP_CONCAT、有并列的 LIMIT）两种写法不一样。按原文认出哪些是逗号连接，
    去掉它们的 CROSS（和别的方言解析出来的一样）。认不出时整条语句的 CROSS 连接标成认不出（见 _fingerprint）。"""
    from sqlglot import exp

    joins = [j for j in tree.find_all(exp.Join) if str(j.args.get("kind") or "").upper() == "CROSS"]
    if not joins:
        return
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001
        tokens = []
    marks: list[tuple[int, bool]] = []
    depth = 0
    in_from: set[int] = set()
    for tok in tokens:
        name = tok.token_type.name
        if name == "L_PAREN":
            depth += 1
        elif name == "R_PAREN":
            in_from.discard(depth)
            depth -= 1
        elif name == "FROM":
            in_from.add(depth)
        elif name in _CLAUSE_ENDS:
            in_from.discard(depth)
        elif name == "COMMA" and depth in in_from:
            marks.append((tok.start, True))
        elif name in ("JOIN", "STRAIGHT_JOIN"):
            marks.append((tok.start, False))
    for join in joins:
        starts = [i.meta["start"] for i in join.this.find_all(exp.Identifier) if isinstance(i.meta.get("start"), int)]
        before = [m for m in marks if starts and m[0] < min(starts)]
        if not before:
            join.meta["sql_semantics_join"] = f"unknown:{id(tree)}"
            continue
        comma = max(before)[1]
        if comma:
            join.set("kind", None)


@dataclass
class _Form:
    """一条语句在某一级的规范形式。key 一样才算等价，text 是生成的 SQL（给人看）；pre 是起规范名字之前的树。"""

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
    """语法树的确定写法：每个节点写类名和全部参数（按参数名排序，字符串原样），不同的树一定写得不一样。括号不算
    （语法树的结构已经定了怎么结合：SQLite 里 (a) 和 a 的类型亲和也一样）；类型标注、注释、meta 不算，只有认不出
    是逗号还是 CROSS JOIN 的连接带上标记（见 _mark_comma_joins）。"""
    from sqlglot import exp

    if isinstance(node, exp.Paren):
        return _fingerprint(node.this)
    if isinstance(node, exp.Expr):
        parts = [f"{k}={_fingerprint(v)}" for k, v in sorted(node.args.items())
                 if v is not None and v is not False and v != []]
        mark = node.meta.get("sql_semantics_join") if isinstance(node, exp.Join) else None
        if mark:
            parts.append(f"@join={mark}")
        surface = node.meta.get(_SURFACE)
        if surface:
            parts.append(f"@surface={surface}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_fingerprint(v) for v in node) + "]"
    return repr(node)


def _key(node: Any, read: str | None = None) -> tuple[str, str]:
    """排序、去重用的键：只看语法树（见 _fingerprint），括号写没写不影响先后。"""
    return _fingerprint(node), ""


@dataclass
class _Schema:
    """数据源的表结构：mapping 给 qualify（只要列名），types 记各列声明的类型（判断「相等的值是不是写法也一样」，见
    _Sensitivity），columns 是所有表的列名（小写）；not_null 是确定不会为空的列（声明了 NOT NULL，或者是主键：
    PostgreSQL / MySQL 的主键列都不为空，SQLite 只有单独一列、声明成 INTEGER 的主键是 rowid、不会为空——别的主键
    在 SQLite 里照样能存空值），pk 是各表的主键列。"""

    mapping: dict[str, dict[str, str]] | None = None
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    columns: set[str] = field(default_factory=set)
    not_null: dict[str, set[str]] = field(default_factory=dict)
    pk: dict[str, list[str]] = field(default_factory=dict)
    unique: dict[str, list[list[str]]] = field(default_factory=dict)


def _schema_info(schema: Mapping[str, Any] | None) -> _Schema:
    """schema_cache → _Schema。qualify 用的类型一律记 UNKNOWN：只要列名（认列属于哪张表、展开 SELECT *）；按类型做
    的改写和方言有关，不做。nullable 为 False 的列、主键列另记下来（哪些主键列确定不为空和方言有关，见
    _Schema.not_null 和 _not_null_column）。"""
    tables = schema.get("tables") if isinstance(schema, Mapping) else None
    if not isinstance(tables, Mapping):
        return _Schema()
    mapping: dict[str, dict[str, str]] = {}
    types: dict[str, dict[str, str]] = {}
    not_null: dict[str, set[str]] = {}
    pk: dict[str, list[str]] = {}
    unique: dict[str, list[list[str]]] = {}
    for name, meta in tables.items():
        columns = meta.get("columns") if isinstance(meta, Mapping) else None
        cols = [c for c in columns or [] if isinstance(c, Mapping) and isinstance(c.get("name"), str) and c["name"]]
        if cols:
            key = str(name).translate(_ASCII_LOWER)
            mapping[str(name)] = {c["name"]: "UNKNOWN" for c in cols}
            types[key] = {c["name"].translate(_ASCII_LOWER): str(c.get("type") or "") for c in cols}
            not_null[key] = {c["name"].translate(_ASCII_LOWER) for c in cols if c.get("nullable") is False}
            keys = meta.get("primary_key") if isinstance(meta, Mapping) else None
            if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
                pk[key] = [k.translate(_ASCII_LOWER) for k in keys]
            groups = meta.get("unique") if isinstance(meta, Mapping) else None
            if isinstance(groups, list):
                unique[key] = [[c.translate(_ASCII_LOWER) for c in g] for g in groups
                               if isinstance(g, list) and g and all(isinstance(c, str) for c in g)]
    return _Schema(mapping or None, types, {c for cols in types.values() for c in cols}, not_null, pk, unique)


def _not_null_column(info: _Schema, table: str, column: str, read: str | None) -> bool:
    """表结构里确定不会为空的列：声明了 NOT NULL；或者是主键——PostgreSQL / MySQL 的主键列都不为空，SQLite 只有单独
    一列、声明的类型正好是 INTEGER 的主键（rowid 的别名）不为空，别的主键因为历史原因能存空值。"""
    t, c = table.translate(_ASCII_LOWER), column.translate(_ASCII_LOWER)
    if c in info.not_null.get(t, set()):
        return True
    keys = info.pk.get(t) or []
    if c not in keys:
        return False
    if read == "sqlite":
        return len(keys) == 1 and info.types.get(t, {}).get(c, "").strip().upper() == "INTEGER"
    return read in ("postgres", "mysql")


def _canonical_forms(root: Any, read: str | None, info: _Schema) -> dict[str, _Form]:
    """一条语句的几级规范形式：

    - optimizer：认列（sqlglot 的 qualify：认清每一列属于哪个来源，给了表结构就展开 SELECT *）之后的全套改写（见
      _optimized、_finish）。认不全列、有和列别名同名又可能是表的列的名字、不确定的函数、递归 CTE、LATERAL 之类的
      不出这一级。
    - light：不认列，只做不需要认列的改写（见 _light）。
    - parsed：解析后只统一名字的大小写、引号（见 _normalize_names），总是有。

    每一级的每一步都只做保持语义的变换，某一级出不来就没有这一级，从不抛异常。"""
    out: dict[str, _Form] = {}
    for level, build in ((_OPTIMIZER, _optimized), (_LIGHT, _light)):
        try:
            out[level] = build(root, read, info)
        except _Unsupported as e:
            logger.debug("SQL 规范化（%s）跳过：%s", level, e)
        except Exception as e:  # noqa: BLE001 - sqlglot 认不了列、写法不支持：这一级不出，别的级照样比
            from sqlglot.errors import SqlglotError

            if isinstance(e, SqlglotError):
                logger.debug("SQL 规范化（%s）跳过：%s", level, e)
            else:
                logger.warning("SQL 规范化（%s）失败", level, exc_info=True)
    try:
        tree = root.copy()
        _normalize_names(tree, read, info)
        out[_PARSED] = _Form(_PARSED, tree.sql(dialect=read, comments=False), tree)
    except Exception:  # noqa: BLE001
        logger.warning("SQL 重新生成失败", exc_info=True)
    return out


def _optimized(root: Any, read: str | None, info: _Schema) -> _Form:
    """认列之后的规范形式：统一名字 → 列名表写进定义 → USING 写成 ON → FETCH 写成 LIMIT → qualify 认列（给了表结构
    先按表结构，表结构里没有的列再不给表结构认一遍）→ 来源别名改成整条语句里互不相同的 → 只引用一次的 CTE 写回用到
    它的地方 → 只筛行、透列的子查询并进外层 → 去掉没用的 CTE → 两级共用的几步（_finish）。"""
    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify

    tree = root.copy()
    why = _unsupported(tree, read) or ("不确定的函数" if _volatile(tree) else None) or \
        ("派生来源的列同名" if _duplicate_outputs(tree) else None) or \
        ("别名被子查询遮住" if _shadowed_reference(tree, info) else None)
    if why:
        raise _Unsupported(why)
    _normalize_names(tree, read, info)
    _remember_names(tree)
    _mark_unsure(tree)
    _push_column_lists(tree)
    _expand_using(tree)
    _qualify_stars(tree)
    _fetch_to_limit(tree)
    _normalize_negation(tree)
    qualified = None
    for mapping in ([info.mapping, None] if info.mapping else [None]):
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
    if _duplicate_outputs(tree):
        raise _Unsupported("派生来源的列同名（展开 * 之后）")
    _split_simple_ctes(tree, read, info)
    _inline_ctes(tree)
    # 写回的 CTE 定义各是一份副本，里面的别名一样：同一个 CTE 写回外层和相关子查询两处，并进去之后子查询里本来指外层
    # 的列就被里层同名的别名遮住了。写回之后再把整条语句的别名改成互不相同的
    _unique_aliases(tree)
    _merge_derived(tree, read, info)
    _push_into_groups(tree, read, info)
    _drop_unused_ctes(tree)
    return _finish(tree, read, info, level=_OPTIMIZER, qualified=True)


def _light(root: Any, read: str | None, info: _Schema) -> _Form:
    """不认列的规范形式：统一名字、列名表写进定义、USING 写成 ON、FETCH 写成 LIMIT、只引用一次的 CTE 写回用到它的
    地方，再走 _finish 里不需要认列的几步。"""
    tree = root.copy()
    why = _unsupported(tree, read) or ("不确定的函数" if _volatile(tree) else None) or \
        ("别名被子查询遮住" if _shadowed_reference(tree, info) else None)
    if why:
        raise _Unsupported(why)
    _normalize_names(tree, read, info)
    _remember_names(tree)
    _mark_unsure(tree)
    _push_column_lists(tree)
    _expand_using(tree)
    _qualify_stars(tree)
    _fetch_to_limit(tree)
    _normalize_negation(tree)
    _inline_ctes(tree)
    _drop_unused_ctes(tree)
    return _finish(tree, read, info, level=_LIGHT, qualified=False)


def _finish(tree: Any, read: str | None, info: _Schema, *, level: str, qualified: bool) -> _Form:
    """两级共用的几步，都只做保持语义的变换（为什么保持语义见各函数的说明），按这个先后：

    0. 原样选出 FROM 里一个子查询全部列、什么别的都不做的一段换成那个子查询（_unwrap_passthrough）。
    1. 动来源的：两个来源的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的行筛掉了的写成内连接；只取可空
       一侧跳过空值的聚合时 LEFT JOIN 写成内连接；开头那组内连接的条件挪进 WHERE、写成逗号连接；恒为真的条件、已经
       保证不为空的 IS NOT NULL 去掉；没用到、又不怕重复（或者按唯一键关联）的 LEFT JOIN 去掉；（认列之后）按关联条件
       相等、类型一样的两列换成同一列。结果看行出来的先后的一段（见 _Sensitivity）这些都不做——只有挪内连接的 ON
       条件、去掉 1 = 1 这类常量条件照做（库里一视同仁，扫描的先后不变）。
    2. 只看有没有、在不在的子查询：EXISTS 写成 SELECT 1，IN 子查询里不影响取值集合的去重、分组、排序、UNION 去掉。
    3. 去重的：SELECT DISTINCT 写成按选出来的列 GROUP BY、已经按选出来的列分组的 DISTINCT 去掉（只在「相等」就是
       「一样」、不看行出来的先后时做）。
    4. 和来源无关的：COUNT(1) 写成 COUNT(*)、MIN / MAX 里的 DISTINCT 去掉、IIF / IF 写成 CASE；IN 列表去重排序、只有一个
       常量的写成 =；BETWEEN 写成两个比较；NOT 套比较写成反过来的比较、NOT NOT 去掉；LIMIT 的同义写法；不影响结合的
       括号、ORDER BY 的 ASC、OFFSET 0、重复的排序键去掉；最外层 ORDER BY 引用的别名换成表达式，最外层和条件里的子查询
       选出来的列名去掉。
    5. 起规范的名字、排好交换律成立的地方（见 _named）。
    """
    tree = _unwrap_passthrough(tree, read)
    sens = _Sensitivity(tree, read, info)
    _right_to_left(tree, sens)
    sens = _Sensitivity(tree, read, info)
    _fold_not(tree, sens)
    _reject_nulls(tree, read, sens)
    _null_blind_left_joins(tree, read, sens)
    _flatten_prefix(tree, sens, read)
    _drop_true_conjuncts(tree)
    _drop_implied_not_null(tree, read, info, sens)
    _drop_unused_left_joins(tree, sens, info, read)
    if qualified:
        _substitute_equal_columns(tree, read, info, sens)
        _constraint_rewrites(tree, read, info, sens)
    _exists_subqueries(tree)
    sens = _Sensitivity(tree, read, info)
    _membership_subqueries(tree, sens)
    _distinct_to_group(tree, sens)
    _count_star(tree)
    _minmax_distinct(tree, sens)
    _case_forms(tree, read)
    _in_lists(tree)
    _expand_between(tree, read, sens)
    _fold_not(tree, sens)
    _normalize_limits(tree, read)
    _strip_parens(tree)
    _strip_asc(tree)
    _zero_offset(tree)
    _order_by_aliases(tree)
    _dedupe_order(tree, sens)
    _drop_output_aliases(tree)
    pre = tree.copy()
    sens = _Sensitivity(tree, read, info)
    tree = _named(tree, read, qualified, sens)
    return _Form(level, tree.sql(dialect=read, comments=False), tree, pre, qualified)


def _named(tree: Any, read: str | None, qualified: bool, sens: "_Sensitivity", hint: "_Hint | None" = None) -> Any:
    """起规范的名字、排好交换律成立的地方，最后统一引号（只给需要的标识符加）。"""
    from sqlglot.optimizer.qualify_columns import quote_identifiers

    _Namer(tree, read, qualified, sens, hint).canon(tree, 0, {})
    return quote_identifiers(tree, dialect=read, identify=False)


# ---- 守门：哪些结构不走规范化的两级

def _volatile(tree: Any) -> bool:
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, (exp.Rand, exp.Randn)) or type(node).__name__.lower() in _VOLATILE:
            return True
        if isinstance(node, exp.Anonymous) and str(node.name).lower() in _VOLATILE:
            return True
    return False


def _unsupported(tree: Any, read: str | None) -> str | None:
    """规范化的两级不处理的结构：递归 CTE（写了 RECURSIVE，或者 CTE 引用自己、引用后面的 CTE——SQLite 里不写
    RECURSIVE 也是递归）、LATERAL、UNNEST、PIVOT、表函数、VALUES、SEMI / ANTI / ASOF / NATURAL 这些连接、实表别名
    带列名（PostgreSQL 的 orders AS o(a, b) 给列改了名）、BETWEEN SYMMETRIC、FETCH … WITH TIES / PERCENT、ROLLUP /
    CUBE / GROUPING SETS、SELECT INTO、加锁。"""
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, exp.With):
            if node.args.get("recursive"):
                return "递归 CTE"
            names = [c.alias_or_name for c in node.expressions]
            for i, cte in enumerate(node.expressions):
                later = set(names[i:])
                if any(t.name in later and not t.args.get("db") for t in cte.this.find_all(exp.Table)):
                    return "CTE 引用自己或后面的 CTE"
        if isinstance(node, (exp.Lateral, exp.Unnest, exp.Pivot, exp.TableSample, exp.Values, exp.Into, exp.Lock)):
            return type(node).__name__
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                return "表函数"
            alias = node.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.columns:
                return "实表别名带列名"
        if isinstance(node, exp.Join) and (node.args.get("method") or str(node.kind or "").upper() in
                                           ("SEMI", "ANTI", "ASOF", "POSITIONAL", "STRAIGHT_JOIN", "ARRAY")):
            return "连接方式"
        if isinstance(node, exp.Between) and node.args.get("symmetric"):
            return "BETWEEN SYMMETRIC"
        if isinstance(node, exp.Fetch):
            options = node.args.get("limit_options")
            if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
                return "FETCH WITH TIES / PERCENT"
        if isinstance(node, exp.Group) and any(node.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return "ROLLUP / CUBE / GROUPING SETS"
    return None


def _duplicate_outputs(tree: Any) -> bool:
    """FROM 里的子查询、CTE 选出来的列有同名的（SELECT user_id AS k, SUM(amount) AS k …）：外面按名字引用 k 时 SQLite
    认第一个、PostgreSQL / MySQL 报错，按名字找定义的改写（外层条件挪进 HAVING、并子查询、展开 *）都可能找错。有就
    不走认列那一级。"""
    from sqlglot import exp

    for holder in tree.find_all(exp.CTE, exp.Subquery):
        if isinstance(holder, exp.Subquery) and not isinstance(holder.parent, (exp.From, exp.Join)):
            continue
        leaves = _leaves(holder.this)
        if leaves and isinstance(leaves[0], exp.Select):
            names = [p.alias_or_name for p in leaves[0].expressions if p.alias_or_name and not _is_star(p)]
            if len(names) != len(set(names)):
                return True
    return False


def _shadowed_reference(tree: Any, info: _Schema) -> bool:
    """子查询里写了表名（别名）的列，而这个别名在子查询和外层各指一个不同的来源（FROM orders o … EXISTS (SELECT 1 FROM
    refunds AS o WHERE o.status = 'paid')）：SQLite 先在里层的 o 里找 status，找不到就往外层找（真实的 SQLite 上
    试过），sqlglot 一律认里层的。里层是实表、表结构里有这一列（或者是派生来源、选出来的列里有它）时才确定是里层的；
    认不准就不走规范化的两级。同一张表在里外层用同一个别名的（FROM orders WHERE … (SELECT … FROM orders)）不算：
    里外层的列一样。"""
    from sqlglot import exp

    def signature(item: Any, ctes: dict[str, Any]) -> Any:
        if isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
            key = item.name.translate(_ASCII_LOWER)
            if not item.args.get("db") and key in ctes:
                return "cte", id(ctes[key])
            return "table", ".".join(p for p in (item.catalog, item.db, item.name) if p).translate(_ASCII_LOWER)
        return "derived", id(item)

    def certain(item: Any, column: str, ctes: dict[str, Any]) -> bool:
        kind, _ = signature(item, ctes)
        if kind == "table":
            columns = info.types.get(item.name.translate(_ASCII_LOWER))
            return columns is not None and column.translate(_ASCII_LOWER) in columns
        body = item.this if isinstance(item, exp.Subquery) else ctes[item.name.translate(_ASCII_LOWER)].this
        return any(n is not None and n == column for n in _output_names(body))

    def walk(query: Any, frames: list[dict[str, Any]], ctes: dict[str, Any]) -> bool:
        if isinstance(query, exp.Subquery):
            return walk(query.this, frames, ctes)
        with_ = query.args.get("with_")
        if with_ is not None:
            ctes = dict(ctes)
            for cte in with_.expressions:
                if walk(cte.this, frames, ctes):
                    return True
                ctes[cte.alias_or_name.translate(_ASCII_LOWER)] = cte
        if isinstance(query, exp.SetOperation):
            return walk(query.left, frames, ctes) or walk(query.right, frames, ctes)
        if not isinstance(query, exp.Select):
            return False
        frame = {item.alias_or_name: item for item in _items_of(query) if item.alias_or_name}
        for item in _items_of(query):
            if isinstance(item, exp.Subquery) and walk(item.this, frames, ctes):
                return True
        stack = frames + [frame]
        for node in _block_nodes(query):
            if isinstance(node, exp.Column) and node.table:
                defs = [f[node.table] for f in stack if node.table in f]
                if len({signature(d, ctes) for d in defs}) > 1 and not certain(defs[-1], node.name, ctes):
                    return True
        return any(walk(sub, stack, ctes) for sub in _nested_queries(query))

    return walk(tree, [], {})


# ---- 名字

def _identifier_role(ident: Any) -> str:
    """一个标识符在语法树里是什么：column（列名）/ column_alias（列的别名、CTE 和子查询的列名表）/ table（表名、
    列前面的表名或别名）/ table_alias（表、子查询、CTE 的别名）/ other。"""
    from sqlglot import exp

    parent, key = ident.parent, ident.arg_key
    if isinstance(parent, exp.Column):
        return "column" if key == "this" else "table"
    if isinstance(parent, exp.Table):
        return "table"
    if isinstance(parent, exp.TableAlias):
        return "column_alias" if key == "columns" else "table_alias"
    if isinstance(parent, exp.Alias):
        return "column_alias"
    return "other"


def _visible_ctes(node: Any) -> dict[str, Any]:
    """node 所在位置看得见的 CTE（名字 → CTE），里层的遮住外层的。"""
    from sqlglot import exp

    chain = []
    cur = node
    while cur is not None:
        with_ = cur.args.get("with_") if isinstance(cur, exp.Expr) else None
        if with_ is not None:
            chain.append(with_)
        cur = cur.parent
    out: dict[str, Any] = {}
    for with_ in reversed(chain):
        for cte in with_.expressions:
            out[cte.alias_or_name.translate(_ASCII_LOWER)] = cte
    return out


def _column_in_scope(col: Any, name: str, info: _Schema) -> bool:
    """SQLite 里不带表名的名字 name（已转小写）在 col 所在的这一段、外层各段的来源里确实是一列：实表在表结构里、有这一
    列；CTE、FROM 里的子查询选出来的列（或者写的列名表）里有这个名字。认不出来源的列（没给表结构、选的是 *）一律当成
    不知道（返回 False）：调用方就原样留着，不会把可能是字符串的 "State" 和 "state" 认成同一列。"""
    from sqlglot import exp

    ctes = _visible_ctes(col)
    node = col.parent
    while node is not None:
        if isinstance(node, exp.Select):
            for item in _items_of(node):
                body = None
                if isinstance(item, exp.Subquery):
                    body = item.this
                elif isinstance(item, exp.Table) and not item.args.get("db") and \
                        item.name.translate(_ASCII_LOWER) in ctes:
                    body = ctes[item.name.translate(_ASCII_LOWER)].this
                if body is not None:
                    names = _output_names(body)
                    if any(n is not None and n.translate(_ASCII_LOWER) == name for n in names):
                        return True
                    continue
                if isinstance(item, exp.Table):
                    columns = info.types.get(item.name.translate(_ASCII_LOWER))
                    if columns is not None and name in columns:
                        return True
        node = node.parent
    return False


def _normalize_names(tree: Any, read: str | None, info: _Schema) -> None:
    """按方言的规则统一名字的大小写和引号，只做库里认成同一个名字的改写；处理过的标识符标成 case_sensitive，后面
    qualify 不再按 sqlglot 的规则改（sqlglot 认为 SQLite 带引号的名字也不分大小写，可 SQLite 找不到这一列时把双引号
    里的当成字符串，'Paid' 和 'paid' 就不同了）。只认 ASCII 字母的大小写（库里也只折 ASCII）。

    - SQLite：不带引号的名字、带引号的表名和别名不分大小写，统一成小写；条件、SELECT 列表里不带表名、带引号的列名
      （"Paid"）原样留着，除非它所在的这一段或外层各段的来源里确实有这一列（见 _column_in_scope：找得到这一列就不会被
      当成字符串）。只看这条语句用到的表：表结构里别的表有同名的列不算（orders 没有 state 列时 "State" 是字符串，
      refunds 有 state 列不相干）。
    - PostgreSQL（和认不出的方言）：不带引号的名字折成小写；带引号的里面没有大写字母就和不带引号的是同一个名字。
    - MySQL：列名、列的别名不分大小写，统一成小写；表名、表的别名在 Linux 上分大小写，原样。
    """
    from sqlglot import exp

    for ident in list(tree.find_all(exp.Identifier)):
        name = ident.this
        if not isinstance(name, str):
            continue
        role = _identifier_role(ident)
        quoted = bool(ident.args.get("quoted"))
        lower = name.translate(_ASCII_LOWER)
        if read == "sqlite":
            keep = quoted and role == "column" and not ident.parent.args.get("table") and \
                not _column_in_scope(ident.parent, lower, info)
            if keep:
                ident.meta[_UNSURE] = True
            else:
                name, quoted = lower, False
        elif read == "mysql":
            if role in ("column", "column_alias"):
                name = lower
            quoted = False
        elif not quoted or lower == name:
            name, quoted = lower, False
        if quoted or not _PLAIN_NAME.match(name):
            quoted = True
        ident.set("this", name)
        ident.set("quoted", quoted)
        ident.meta["case_sensitive"] = True


def _mark_unsure(tree: Any) -> None:
    """条件里的子查询（IN、EXISTS、标量）里不带表名的列标成认不准（见 _UNSURE）：它可能是子查询里的表的列，也可能是
    外层的列，表结构里没有就分不清。只影响差异说明说不说具体的，不影响规范形式。"""
    from sqlglot import exp

    for col in tree.find_all(exp.Column):
        if col.table or not isinstance(col.this, exp.Identifier):
            continue
        node = col.parent
        while node is not None:
            if isinstance(node, exp.Select) and (_scalar_context(node) or _condition_query(node)):
                col.this.meta[_UNSURE] = True
                break
            node = node.parent


def _remember_names(tree: Any) -> None:
    """CTE、FROM 里的子查询原来的名字记在里面那段查询上（见 _ORIGINAL）。"""
    from sqlglot import exp

    for cte in tree.find_all(exp.CTE):
        cte.this.meta.setdefault(_ORIGINAL, cte.alias_or_name)
    for sub in tree.find_all(exp.Subquery):
        if isinstance(sub.parent, (exp.From, exp.Join)) and sub.alias:
            sub.this.meta.setdefault(_ORIGINAL, sub.alias)


def _push_column_lists(tree: Any) -> None:
    """CTE、FROM 里的子查询自己写了列名表的（WITH p(uid, total) AS (…)、(…) AS t(x, y)），列名写成里面那段（UNION 是
    第一支）选出来的列的别名，列名表去掉：外面按这些名字引用，两种写法是一回事。里面那段选的是 *（不知道有几列）、
    列数对不上、本段 ORDER BY / GROUP BY / HAVING 还按原来的别名引用的，没法改写，不走规范化的两级。"""
    from sqlglot import exp

    for holder in list(tree.find_all(exp.CTE, exp.Subquery)):
        alias = holder.args.get("alias")
        if not isinstance(alias, exp.TableAlias) or not alias.columns:
            continue
        body = holder.this
        target = _leaves(body)[0] if isinstance(body, exp.SetOperation) else body
        names = [c.name for c in alias.columns]
        if not isinstance(target, exp.Select) or _starry(target) or len(target.expressions) != len(names):
            raise _Unsupported("列名表对不上选出来的列")
        own = {p.alias for p in target.expressions if isinstance(p, exp.Alias) and p.alias}
        if isinstance(body, exp.SetOperation):
            if body.args.get("order") is not None:
                raise _Unsupported("合并的结果按名字排序")
        # 本段的 WHERE、ON、GROUP BY、HAVING、ORDER BY、子查询里按名字引用原来的列别名、或者正好叫新名字的（SQLite 的
        # WHERE 认列别名：WITH t(a, b) AS (SELECT amount AS b, id AS a FROM orders WHERE b > 50) 里的 b 是 amount），
        # 改名之后就指到别的列上了
        if _alias_refs(target) & (own | set(names)):
            raise _Unsupported("列名表改名之后本段的引用对不上")
        target.set("expressions", [exp.alias_(p.unalias().copy() if isinstance(p, exp.Alias) else p.copy(), n,
                                              copy=False) for p, n in zip(target.expressions, names)])
        alias.set("columns", None)


def _expand_using(tree: Any) -> None:
    """JOIN … USING (c) 写成 ON 左表.c = 右表.c，这一段里不带表名的 c 写成左表.c（内连接、LEFT JOIN 时合并出来的 c
    就是左表的 c）。只改得了「FROM 只有一个来源、第一个连接就是 USING、SELECT 里没有 *」的写法：别的（左边好几个
    来源时 c 是哪一张的、SELECT * 列的先后、RIGHT / FULL JOIN 合并出来的值、子查询里引用 c）没法照原样改写，不走
    规范化的两级。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not any(j.args.get("using") for j in joins):
            continue
        if len(joins) < 1 or not joins[0].args.get("using") or any(j.args.get("using") for j in joins[1:]):
            raise _Unsupported("USING")
        join = joins[0]
        if str(join.side or "").upper() not in ("", "LEFT") or join.args.get("on") is not None or \
                any(_is_star(p) for p in select.expressions):
            raise _Unsupported("USING")
        left, right = select.args["from_"].this, join.this
        la, ra = left.alias_or_name, right.alias_or_name
        if not la or not ra:
            raise _Unsupported("USING")
        names = [u.name for u in join.args["using"]]
        for sub in _nested_queries(select):
            if any(not c.table and c.name in names for c in sub.find_all(exp.Column)):
                raise _Unsupported("子查询里引用 USING 的列")
        # 有列别名和 USING 的列同名：ORDER BY（SQLite 的 WHERE、GROUP BY、HAVING 也可能）里单独一个名字先认列别名，
        # SELECT -o.id AS id … USING (id) ORDER BY id 是按 -o.id 排，不能写成左表的 id
        if any(isinstance(p, exp.Alias) and p.alias in names for p in select.expressions):
            raise _Unsupported("列别名和 USING 的列同名")
        for node in list(_block_nodes(select)):
            if isinstance(node, exp.Column) and not node.table and node.name in names:
                node.set("table", exp.to_identifier(la))
        conds = [exp.EQ(this=exp.column(n, table=la), expression=exp.column(n, table=ra)) for n in names]
        join.set("using", None)
        join.set("on", _and_all(conds))


def _qualify_stars(tree: Any) -> None:
    """几个来源的 SELECT * 写成按 FROM 的先后逐个来源的 t.*：两种写法是一回事（没有 USING / NATURAL 时 * 就是各来源的
    全部列依次排开）。sqlglot 的 qualify 展开 * 时把实表的列排在 FROM 里的子查询前面，和库里的先后不一样，先写成 t.*
    它就按写的先后展开。有来源没有名字的不动（这时也不走规范化的两级）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        if not any(isinstance(p, exp.Star) for p in select.expressions):
            continue
        items = _items_of(select)
        if len(items) < 2 or any(not i.alias_or_name for i in items):
            continue
        stars = [exp.Column(this=exp.Star(), table=exp.to_identifier(i.alias_or_name)) for i in items]
        new: list[Any] = []
        for p in select.expressions:
            new += [c.copy() for c in stars] if isinstance(p, exp.Star) else [p]
        select.set("expressions", new)


def _fetch_to_limit(tree: Any) -> None:
    """FETCH FIRST n ROWS ONLY（不带 WITH TIES / PERCENT）就是 LIMIT n（sqlglot 把 FETCH 放在 limit 这个参数上）。"""
    from sqlglot import exp

    for query in list(tree.find_all(exp.Select, exp.SetOperation)):
        for key in ("limit", "fetch"):
            fetch = query.args.get(key)
            if not isinstance(fetch, exp.Fetch):
                continue
            count = fetch.args.get("count") or exp.Literal.number(1)
            query.set(key, None)
            query.set("limit", exp.Limit(expression=count.copy()))


# ---- 认列属于哪个来源

class _Resolver:
    """按别名认清每一列属于哪个来源（相关子查询里的列到外层找）：refs[id(来源)] 是引用它的列，cte_of[id(表)] 是它
    引用的 CTE，owner[id(来源)] 是来源所在的那段 SELECT。不带表名的列不认（轻量一级）。"""

    def __init__(self, tree: Any) -> None:
        self.refs: dict[int, list[Any]] = {}
        self.cte_of: dict[int, Any] = {}
        self.owner: dict[int, Any] = {}
        self.source_of: dict[int, Any] = {}
        self._walk(tree, [], {})

    def _walk(self, query: Any, frames: list[dict[str, Any]], ctes: dict[str, Any]) -> None:
        from sqlglot import exp

        if isinstance(query, exp.Subquery):
            self._walk(query.this, frames, ctes)
            return
        with_ = query.args.get("with_")
        if with_ is not None:
            ctes = dict(ctes)
            for cte in with_.expressions:
                self._walk(cte.this, frames, ctes)
                ctes[cte.alias_or_name] = cte
        if isinstance(query, exp.SetOperation):
            self._walk(query.left, frames, ctes)
            self._walk(query.right, frames, ctes)
            return
        if not isinstance(query, exp.Select):
            raise _Unsupported(type(query).__name__)
        frame: dict[str, Any] = {}
        for item in _items_of(query):
            if isinstance(item, exp.Subquery):
                self._walk(item.this, frames, ctes)
            elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
                if not item.args.get("db") and item.name in ctes:
                    self.cte_of[id(item)] = ctes[item.name]
            else:
                raise _Unsupported(type(item).__name__)
            name = item.alias_or_name
            if not name or name in frame:
                raise _Unsupported("来源没有名字或重名")
            frame[name] = item
            self.owner[id(item)] = query
            self.refs.setdefault(id(item), [])
        stack = frames + [frame]
        for node in _block_nodes(query):
            if isinstance(node, exp.Column) and node.table:
                for f in reversed(stack):
                    if node.table in f:
                        self.refs[id(f[node.table])].append(node)
                        self.source_of[id(node)] = f[node.table]
                        break
        for sub in _nested_queries(query):
            self._walk(sub, stack, ctes)

    def body(self, item: Any) -> Any:
        """派生来源里面那段查询（FROM 里的子查询、引用的 CTE）；实表是 None。"""
        from sqlglot import exp

        if isinstance(item, exp.Subquery):
            return item.this
        cte = self.cte_of.get(id(item))
        return cte.this if cte is not None else None


def _unique_aliases(tree: Any) -> None:
    """（认列之后）整条语句里每个来源的别名改成互不相同的（_s1、_s2…），引用它的列跟着改：按原来的别名认清每一列
    属于哪个来源之后再改，相关子查询里引用外层的列不会认错；之后并子查询、写回 CTE 时名字不会撞上。"""
    from sqlglot import exp

    resolver = _Resolver(tree)
    taken = {t.name for t in tree.find_all(exp.Table)} | {c.alias_or_name for c in tree.find_all(exp.CTE)}
    n = 0
    for item in [i for s in tree.find_all(exp.Select) for i in _items_of(s)]:
        n += 1
        name = f"_s{n}"
        while name in taken:
            name += "_"
        alias = item.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            alias.set("this", exp.to_identifier(name))
        else:
            item.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
        for col in resolver.refs.get(id(item), []):
            col.set("table", exp.to_identifier(name))


def _nested_with(tree: Any) -> bool:
    from sqlglot import exp

    top = tree.args.get("with_")
    return any(isinstance(n, exp.With) and n is not top for n in tree.walk())


def _inline_ctes(tree: Any) -> None:
    """只引用一次的非递归顶层 CTE 写回引用它的地方，成为 FROM 里的子查询（别名照旧）：写成 CTE 还是子查询是同一个
    意思。语句里还有别的 WITH 的不动（名字可能被里层的 CTE 遮住）。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None or with_.args.get("recursive") or _nested_with(tree):
        return
    changed = True
    while changed and with_.expressions:
        changed = False
        resolver = _Resolver(tree)
        for cte in list(with_.expressions):
            refs = [t for t in tree.find_all(exp.Table) if resolver.cte_of.get(id(t)) is cte]
            if len(refs) != 1:
                continue
            ref = refs[0]
            sub = exp.Subquery(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(ref.alias_or_name)))
            ref.replace(sub)
            cte.pop()
            changed = True
            break
    if not with_.expressions:
        tree.set("with_", None)


def _drop_unused_ctes(tree: Any) -> None:
    """没有被引用的非递归 CTE 去掉：查询语句里没用到的 CTE 不影响结果。"""
    from sqlglot import exp

    changed = True
    while changed:
        changed = False
        resolver = _Resolver(tree)
        used = {id(c) for c in resolver.cte_of.values()}
        for with_ in list(tree.find_all(exp.With)):
            if with_.args.get("recursive"):
                continue
            for cte in list(with_.expressions):
                if id(cte) not in used:
                    cte.pop()
                    changed = True
            if not with_.expressions and isinstance(with_.parent, exp.Expr):
                with_.parent.set("with_", None)


def _mergeable(body: Any) -> bool:
    """FROM 里的子查询能不能并进外层：只从几张实表（内连接）里筛行、选列（没有分组、聚合、去重、排序、取行、窗口函数、
    UNION、WITH，选出来的列里没有子查询），并进去之后每一行、每个值都一样。"""
    from sqlglot import exp

    if not isinstance(body, exp.Select):
        return False
    if {k for k, v in body.args.items() if v} - {"expressions", "from_", "where", "joins"}:
        return False
    sources = _items_of(body)
    if not sources or any(not isinstance(i, exp.Table) for i in sources):
        return False
    if any(not _inner(j) for j in body.args.get("joins") or []):
        return False
    stars = [p for p in body.expressions if _is_star(p)]
    if stars and (len(body.expressions) != 1 or not isinstance(body.expressions[0], exp.Star) or len(sources) != 1):
        return False
    for p in body.expressions:
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Query, exp.Subquery)) for n in p.walk()):
            return False
    names = [p.alias_or_name for p in body.expressions]
    if not stars and (not all(names) or len(set(names)) != len(names)):
        return False
    where = body.args.get("where")
    return where is None or not any(isinstance(n, (exp.AggFunc, exp.Window)) for n in where.walk())


def _merge_derived(tree: Any, read: str | None, info: _Schema) -> None:
    """（认列之后）只筛行、选列的子查询（见 _mergeable）并进外层：外层引用它的列换成里面的定义，里面的 WHERE 和外层的
    WHERE（和它里面内连接的条件）用 AND 连起来，来源换成里面那几张表。只并内连接一侧（FROM 或内连接进来的）的：外连接
    可空一侧没关联上的行该是空值，并进来就成了算出来的值。结果看行出来的先后的外层（见 _Sensitivity）不并。
    外层有 SELECT * 的：子查询选的就是 *（换成那张表，列一样）或者它是唯一的来源（* 换成它选出来的列）才并。"""
    from sqlglot import exp

    def substitute(col: Any, expr: Any) -> None:
        new = expr.copy()
        if not isinstance(new, (exp.Column, exp.Literal, exp.Null, exp.Boolean)):
            new = exp.Paren(this=new)
        col.replace(new)

    for _ in range(64):
        resolver = _Resolver(tree)
        # 浮点数求和这一条不挡并子查询：只筛行选列的子查询三种库自己也会并进外层（PostgreSQL 的 subquery pull-up、
        # MySQL 的 derived_merge、SQLite 的 flattening），并不并执行起来先后一样
        sens = _Sensitivity(tree, read, info, resolver, floats=False)
        done = False
        for select in reversed(list(tree.find_all(exp.Select))):
            if sens.of(select):
                continue
            joins = select.args.get("joins") or []
            if any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
                continue
            items = _items_of(select)
            for index, item in enumerate(items):
                if not isinstance(item, exp.Subquery) or not _mergeable(item.this):
                    continue
                if index and not _inner(joins[index - 1]):
                    continue
                body = item.this
                sources = _items_of(body)
                src = sources[0]
                star_body = isinstance(body.expressions[0], exp.Star)
                defs = {} if star_body else {p.alias_or_name: p.unalias() for p in body.expressions}
                refs = resolver.refs.get(id(item), [])
                if not star_body and any(c.name not in defs for c in refs):
                    continue
                stars = [p for p in select.expressions if _is_star(p)]
                bare = [p for p in stars if isinstance(p, exp.Star)]
                if bare and not star_body and len(items) != 1:
                    continue
                alias = item.alias_or_name
                for col in refs:
                    if star_body:
                        col.set("table", exp.to_identifier(src.alias_or_name))
                    else:
                        substitute(col, defs[col.name])
                if not star_body:
                    expanded = [exp.alias_(e.copy(), n, copy=False) for n, e in defs.items()]
                    new_list = []
                    for p in select.expressions:
                        if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star and p.table == alias):
                            new_list += [e.copy() for e in expanded]
                        else:
                            new_list.append(p)
                    select.set("expressions", new_list)
                else:
                    for p in stars:
                        if isinstance(p, exp.Column) and p.table == alias:
                            p.set("table", exp.to_identifier(src.alias_or_name))
                conds = []
                inner_where = body.args.get("where")
                if inner_where is not None:
                    conds += _conjuncts(inner_where.this)
                for join in body.args.get("joins") or []:
                    if join.args.get("on") is not None:
                        conds += _conjuncts(join.args["on"])
                if index:
                    own = joins[index - 1]
                    if own.args.get("on") is not None:
                        conds += _conjuncts(own.args["on"])
                    position = index - 1
                    new_joins = joins[:position] + [exp.Join(this=s.copy()) for s in sources] + joins[position + 1:]
                    select.set("joins", new_joins)
                else:
                    select.args["from_"].set("this", src.copy())
                    select.set("joins", [exp.Join(this=s.copy()) for s in sources[1:]] + list(joins) or None)
                outer = select.args.get("where")
                conds += _conjuncts(outer.this) if outer is not None else []
                if conds:
                    select.set("where", exp.Where(this=_and_all([c.copy() for c in conds])))
                done = True
                break
            if done:
                break
        if not done:
            return


def _split_simple_ctes(tree: Any, read: str | None, info: "_Schema") -> None:
    """引用了好几次、只从几张表里筛行选列的 CTE（见 _mergeable），每处引用各写回一份 FROM 里的子查询（别名照旧）：
    这样的 CTE 是确定的（有不确定函数的语句不走这一级），算几遍结果一样，写回之后每处都能并进外层。语句里还有别的
    WITH 的不动（名字可能被里层的 CTE 遮住）；有一处引用在结果看行出来先后的一段里的不动（PostgreSQL 把引用了好几次
    的 CTE 先算出来存着，写回之后扫描的先后可能就变了）。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None or with_.args.get("recursive") or _nested_with(tree):
        return
    resolver = _Resolver(tree)
    sens = _Sensitivity(tree, read, info, resolver)
    for cte in list(with_.expressions):
        if not _mergeable(cte.this):
            continue
        refs = [t for t in tree.find_all(exp.Table) if resolver.cte_of.get(id(t)) is cte]
        if len(refs) < 2 or any(r.find_ancestor(exp.CTE) is not None for r in refs) or \
                any(sens.of(resolver.owner[id(r)]) for r in refs):
            continue
        for ref in refs:
            ref.replace(exp.Subquery(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(ref.alias_or_name))))
        cte.pop()
    if not with_.expressions:
        tree.set("with_", None)


def _grouped(expression: Any, keys: set[str]) -> bool:
    """分组的子查询选出来的一列在每组里只有一个值：整个是分组键，或者每一列都在聚合函数里、或者本身是分组键。"""
    from sqlglot import exp

    if _fingerprint(expression) in keys:
        return True
    if any(isinstance(n, (exp.Window, exp.Query, exp.Subquery)) for n in expression.walk()):
        return False
    for col in expression.find_all(exp.Column):
        if _fingerprint(col) not in keys and col.find_ancestor(exp.AggFunc) is None:
            return False
    return True


def _push_into_groups(tree: Any, read: str | None, info: "_Schema") -> None:
    """（认列之后）外层 WHERE 里只涉及一个分组子查询（FROM 或内连接进来的）选出来的列的条件，换成子查询里对应的表达式、
    挪进它的 HAVING：子查询的每一行就是一组，外层按这一行的值筛和 HAVING 按这一组的值筛，用的是同一个值（分组键取的
    是同一个代表值，聚合是同一个结果）。条件涉及分组键时，分组键得是「相等就是一样」的（见 _Sensitivity._identity_safe）：
    MySQL 会把外层只涉及分组键的条件推进子查询的 WHERE 里先筛行，不分大小写时 BINARY city = 'SH' 推进去和放在 HAVING
    里结果不同（在真实的 MySQL 8.0 上跑出来过）。子查询有窗口函数、取行、去重、ROLLUP 这些的不挪；HAVING 里的条件
    不往 WHERE 挪（相等不等于一样时，组里的行各是各的值）。"""
    from sqlglot import exp

    sens = _Sensitivity(tree, read, info)
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        joins = select.args.get("joins") or []
        if where is None or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        for index, item in enumerate(_items_of(select)):
            if not isinstance(item, exp.Subquery) or (index and not _inner(joins[index - 1])):
                continue
            body = item.this
            if not isinstance(body, exp.Select) or body.args.get("group") is None or sens.of(body) or \
                    any(body.args.get(k) for k in ("limit", "offset", "distinct", "qualify", "windows", "with_")) or \
                    any(n.find(exp.Window) for n in body.expressions):
                continue
            outputs = {p.alias_or_name: p.unalias() for p in body.expressions if not _is_star(p)}
            keys = {_fingerprint(k) for k in body.args["group"].expressions}
            alias = item.alias_or_name
            moved, kept = [], []
            for cond in _conjuncts(where.this):
                cols = list(cond.find_all(exp.Column))
                if not cols or cond.find(exp.Query, exp.Subquery) is not None or \
                        any(c.table != alias or c.name not in outputs or not _grouped(outputs[c.name], keys)
                            for c in cols) or \
                        any(k is not None and not sens._identity_safe(k, body)
                            for c in cols for k in [outputs[c.name] if _fingerprint(outputs[c.name]) in keys else None]):
                    kept.append(cond)
                    continue
                new = cond.copy()
                for col in list(new.find_all(exp.Column)):
                    col.replace(exp.Paren(this=outputs[col.name].copy()))
                moved.append(new)
            if moved:
                having = body.args.get("having")
                body.set("having", exp.Having(this=_and_all(([having.this] if having else []) + moved)))
                select.set("where", exp.Where(this=_and_all(kept)) if kept else None)
                where = select.args.get("where")
                if where is None:
                    break


def _distinct_to_group(tree: Any, sens: "_Sensitivity") -> None:
    """SELECT DISTINCT a, b（没有分组、聚合、窗口函数）和 GROUP BY a, b 出的行一样（空值也算一组）——只在选出来的都是
    「相等就是一样」的列（见 _Sensitivity._identity_safe）、这一段不看行出来的先后（DISTINCT 按扫到的先后出行，GROUP BY
    按分组键排好了出）时改：不然两种写法留下的代表值、取到的行可能不同。ORDER BY 用到没选出来的列时不改（两种写法对
    这种排序的规矩不一样）。已经按选出来的列分组的 DISTINCT 去掉（每组一行、各行的分组键都不同）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") or sens.of(select) or _starry(select):
            continue
        group = select.args.get("group")
        if group is not None:
            selected = {_fingerprint(p.unalias()) for p in select.expressions}
            if group.expressions and all(_fingerprint(k) in selected for k in group.expressions) and \
                    not any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")) and \
                    not any(p.find(exp.Window) for p in select.expressions):
                select.set("distinct", None)
            continue
        if select.args.get("having") is not None or _aggregating(select) or \
                any(p.find(exp.Window, exp.Query, exp.Subquery) for p in select.expressions):
            continue
        keys = [p.unalias() for p in select.expressions]
        if not all(isinstance(k, exp.Column) and sens._identity_safe(k, select) for k in keys):
            continue
        order = select.args.get("order")
        if order is not None:
            allowed = {_fingerprint(k) for k in keys}
            aliases = {p.alias for p in select.expressions if p.alias}
            if any(_fingerprint(o.this) not in allowed and not (
                    isinstance(o.this, exp.Column) and not o.this.table and o.this.name in aliases)
                   for o in order.expressions):
                continue
        new = exp.Group(expressions=[k.copy() for k in keys])
        new.meta[_FROM_DISTINCT] = True
        select.set("distinct", None)
        select.set("group", new)


def _membership_subqueries(tree: Any, sens: "_Sensitivity") -> None:
    """IN / NOT IN (子查询) 只看取值的集合（含不含空值）：里面不取行时，

    - 没有聚合的 ORDER BY 不影响（有聚合的 ORDER BY 让它成了聚合查询，只出一行）；
    - 选出来的只是一列、「相等就是一样」（见 _Sensitivity._identity_safe）时，DISTINCT 不影响；按这一列（和别的列）
      分组、没有 HAVING 也没有聚合时 GROUP BY 不影响（每一行都在某一组里，各组选出来的就是这一列在各行的取值）；
      合并的各支都是这样的一列时，UNION 和 UNION ALL 一样。「相等却不一样」时不改：MySQL 默认不分大小写，去重留下
      'SH' 之后 BINARY 'sh' 就找不到了。"""
    from sqlglot import exp

    def single_column(sub: Any) -> Any:
        if not isinstance(sub, exp.Select) or len(sub.expressions) != 1:
            return None
        column = sub.expressions[0].unalias()
        return column if isinstance(column, exp.Column) and not column.is_star and \
            sens._identity_safe(column, sub) else None

    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        body = query.this if isinstance(query, exp.Subquery) else query
        if body is None:
            continue
        if isinstance(body, exp.SetOperation):
            leaves = _union_leaves(body)
            if leaves is None or any(single_column(leaf) is None for leaf in leaves):
                continue
            _set_union_all(body)
        else:
            leaves = [body]
        for sub in leaves:
            if not isinstance(sub, exp.Select) or any(sub.args.get(k) for k in ("limit", "offset", "fetch", "qualify")):
                continue
            if any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(sub)) or \
                    sub.args.get("having") is not None:
                continue
            if not any(n.find(exp.Window) for n in sub.expressions):
                sub.set("order", None)
            column = single_column(sub)
            group = sub.args.get("group")
            if group is not None:
                if column is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")) or \
                        _fingerprint(column) not in {_fingerprint(k) for k in group.expressions}:
                    continue
                sub.set("group", None)
            distinct = sub.args.get("distinct")
            if distinct is not None and not distinct.args.get("on") and column is not None:
                sub.set("distinct", None)


# ---- 结果看不看行出来的先后

class _Sensitivity:
    """哪几段 SELECT 的结果看行出来的先后：这样的一段不调换来源的先后、不把 CROSS JOIN 写成逗号连接、不排 GROUP BY
    的键、不把 RIGHT JOIN 写成 LEFT JOIN，不往里并子查询——这些改写都会改变库里扫描的先后，实际取到的行、值就不同了。

    本段自己看先后的：取行（LIMIT / OFFSET / FETCH，有 ORDER BY 也可能有并列）；DISTINCT ON；窗口函数；拼串、拼数组、
    取任意一个这类聚合（GROUP_CONCAT）；聚合里既不分组也不聚合的列（SQLite、MySQL 取组里任意一行的值）；当成标量
    用的子查询（SQLite 取第一行），除非是不分组的聚合（只出一行）；去重、分组、MIN / MAX、去重的 UNION 留下的是先
    出现的那个值，而「相等」不等于「写法一样」的时候（MySQL 默认不分大小写的排序规则下的 'SH' 和 'sh'、PostgreSQL
    numeric 的 2.0 和 2.00、SQLite 没有类型亲和的列里的 1 和 1.0、算出来的表达式）看先后，见 _identity_safe；
    PostgreSQL、MySQL 里对不知道是不是精确数的值求和、求平均（浮点数加的先后不同，结果的最后几位就不同，见 _exact）。
    给看先后的一段供行的（它 FROM 里的子查询、引用的 CTE，层层往里）也看先后。"""

    def __init__(self, tree: Any, read: str | None, info: _Schema, resolver: _Resolver | None = None,
                 floats: bool = True) -> None:
        from sqlglot import exp

        self.read = read
        self.info = info
        self.floats = floats
        try:
            self.resolver = resolver or _Resolver(tree)
        except _Unsupported:
            self.resolver = None
        selects = list(tree.find_all(exp.Select))
        marked = {id(s) for s in selects if self._own(s)}
        for op in tree.find_all(exp.SetOperation):
            if op.args.get("distinct") or isinstance(op, (exp.Intersect, exp.Except)):
                for leaf in _leaves(op):
                    if isinstance(leaf, exp.Select) and \
                            not all(self._identity_safe(p.unalias(), leaf) for p in leaf.expressions):
                        marked.add(id(leaf))
        by_id = {id(s): s for s in selects}
        stack = [by_id[i] for i in marked if i in by_id]
        while stack:
            select = stack.pop()
            for item in _items_of(select):
                body = self.resolver.body(item) if self.resolver is not None else (
                    item.this if isinstance(item, exp.Subquery) else None)
                for leaf in _leaves(body) if body is not None else []:
                    if isinstance(leaf, exp.Select) and id(leaf) not in marked:
                        marked.add(id(leaf))
                        stack.append(leaf)
        self.marked = marked

    def of(self, select: Any) -> bool:
        return id(select) in self.marked

    def _own(self, select: Any) -> bool:
        from sqlglot import exp

        if any(select.args.get(k) for k in ("limit", "offset", "fetch")):
            return True
        distinct = select.args.get("distinct")
        if distinct is not None and distinct.args.get("on"):
            return True
        own = list(_block_nodes(select))
        if any(isinstance(n, exp.Window) or _order_dependent(n) for n in own):
            return True
        if _scalar_context(select) and not (_aggregating(select) and select.args.get("group") is None):
            return True
        if _loose_columns(select):
            return True
        if distinct is not None and not all(self._identity_safe(p.unalias(), select) for p in select.expressions):
            return True
        group = select.args.get("group")
        if group is not None and not all(self._identity_safe(k, select) for k in group.expressions):
            return True
        for n in own:
            if isinstance(n, (exp.Min, exp.Max)) and not n.expressions and \
                    not self._identity_safe(n.this.expressions[0] if isinstance(n.this, exp.Distinct) and
                                            n.this.expressions else n.this, select):
                return True
            if self.floats and self.read != "sqlite" and _float_aggregate(n) and not self._exact(n.this, select):
                return True
        return False

    def _exact(self, node: Any, select: Any, depth: int = 0) -> bool:
        """node 的取值是不是精确的数（整数、定点小数）：浮点数求和的先后不同结果就不同（PostgreSQL、MySQL 按扫到的先后
        一个个加；SQLite 用 Kahan-Babuska-Neumaier 求和、关联先后也不跟着 FROM 的先后变，当成精确的）。不知道类型的不算。"""
        from sqlglot import exp

        node = _unparen(node)
        if isinstance(node, exp.Distinct) and len(node.expressions) == 1:
            node = node.expressions[0]
        if isinstance(node, exp.Count) or (isinstance(node, exp.Literal) and node.is_int):
            return True
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
            return self._exact(node.left, select, depth) and self._exact(node.right, select, depth)
        if not isinstance(node, exp.Column) or node.is_star or depth > 16 or self.resolver is None:
            return False
        item = self.resolver.source_of.get(id(node))
        if item is None:
            items = _items_of(select)
            item = items[0] if len(items) == 1 and not node.table else None
        if item is None:
            return False
        body = self.resolver.body(item)
        if body is None:
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER)) if isinstance(item, exp.Table) else None
            declared = (columns or {}).get(node.name.translate(_ASCII_LOWER), "").lower()
            return any(declared.startswith(w) for w in ("int", "integer", "bigint", "smallint", "tinyint", "mediumint",
                                                        "serial", "bigserial", "decimal", "numeric", "dec"))
        if isinstance(body, exp.Select):
            for p in body.expressions:
                if not _is_star(p) and p.alias_or_name == node.name:
                    return self._exact(p.unalias(), body, depth + 1)
        return False

    def _identity_safe(self, node: Any, select: Any, depth: int = 0) -> bool:
        """node 的取值里「相等」是不是就是「一样」：COUNT 是整数；光一列的话看它是哪张表的哪一列、声明的什么类型。"""
        from sqlglot import exp

        node = _unparen(node)
        if isinstance(node, exp.Count):
            return True
        if not isinstance(node, exp.Column) or node.is_star or depth > 16:
            return False
        item = self.resolver.source_of.get(id(node)) if self.resolver is not None else None
        if item is None:
            items = _items_of(select)
            item = items[0] if len(items) == 1 and not node.table else None
        if item is None:
            return self._safe_type(None)
        body = self.resolver.body(item) if self.resolver is not None else (
            item.this if isinstance(item, exp.Subquery) else None)
        if body is None:
            if not isinstance(item, exp.Table):
                return False
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER))
            declared = columns.get(node.name.translate(_ASCII_LOWER)) if columns is not None else None
            return self._safe_type(declared)
        if not isinstance(body, exp.Select):
            return False
        for p in body.expressions:
            if not _is_star(p) and p.alias_or_name == node.name:
                return self._identity_safe(p.unalias(), body, depth + 1)
        sources = _items_of(body)
        if any(isinstance(p, exp.Star) for p in body.expressions) and len(sources) == 1:
            return self._through_star(sources[0], node.name, depth + 1)
        return False

    def _through_star(self, item: Any, name: str, depth: int) -> bool:
        """SELECT * 原样透出来的一列：到它唯一的来源里找同名的列。"""
        from sqlglot import exp

        body = self.resolver.body(item) if self.resolver is not None else None
        if body is None:
            if not isinstance(item, exp.Table):
                return False
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER))
            return self._safe_type(columns.get(name.translate(_ASCII_LOWER)) if columns is not None else None)
        if not isinstance(body, exp.Select) or depth > 16:
            return False
        for p in body.expressions:
            if not _is_star(p) and p.alias_or_name == name:
                return self._identity_safe(p.unalias(), body, depth + 1)
        sources = _items_of(body)
        if any(isinstance(p, exp.Star) for p in body.expressions) and len(sources) == 1:
            return self._through_star(sources[0], name, depth + 1)
        return False

    def _safe_type(self, declared: str | None) -> bool:
        """声明的类型下「相等」是不是就是「一样」。SQLite 没给表结构时按列有声明的类型、默认的排序规则算（假设）；
        有类型的话 BLOB、没写类型（没有类型亲和）的列可能混着 1 和 1.0。PostgreSQL、MySQL 不知道类型就不算：numeric
        的 2.0 和 2.00、MySQL 默认不分大小写的字符串都是相等写法却不一样。"""
        if self.read == "sqlite":
            if declared is None:
                return True
            upper = declared.upper()
            if "INT" in upper or any(w in upper for w in ("CHAR", "CLOB", "TEXT", "REAL", "FLOA", "DOUB")):
                return True
            return bool(upper) and "BLOB" not in upper
        if declared is None:
            return False
        lower = declared.lower().strip()
        if self.read == "mysql":
            return any(lower.startswith(w) for w in ("int", "integer", "bigint", "smallint", "tinyint", "mediumint",
                                                     "date", "datetime", "timestamp", "time", "year", "decimal",
                                                     "numeric", "bit", "bool"))
        return any(lower.startswith(w) for w in ("int", "integer", "bigint", "smallint", "serial", "bigserial",
                                                 "text", "varchar", "character", "char", "bpchar", "date",
                                                 "timestamp", "time", "bool", "uuid"))


def _float_aggregate(node: Any) -> bool:
    """浮点数求和类的聚合（不在窗口函数里的）。"""
    from sqlglot import exp

    if isinstance(node.parent, exp.Window):
        return False
    name = type(node).__name__.lower()
    if isinstance(node, exp.Anonymous):
        name = str(node.name).lower().replace("_", "")
    return name in _FLOAT_AGGS


def _order_dependent(node: Any) -> bool:
    from sqlglot import exp

    name = type(node).__name__.lower()
    if isinstance(node, exp.Anonymous):
        name = str(node.name).lower().replace("_", "")
    return name in _ORDERED_AGGS


def _aggregating(select: Any) -> bool:
    """这一段是聚合查询：有 GROUP BY，或者自己的 SELECT 列表、HAVING、ORDER BY 里有聚合函数（不算窗口函数）。"""
    from sqlglot import exp

    if select.args.get("group") is not None or select.args.get("having") is not None:
        return True
    return any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(select)
               if not isinstance(n.parent, exp.Window))


def _scalar_context(query: Any) -> bool:
    """这段查询当成一个值用（标量子查询）：不在 FROM / JOIN、IN、EXISTS、ANY / ALL、CTE 里，也不是整条语句。"""
    from sqlglot import exp

    node = query
    while True:
        parent = node.parent
        if parent is None:
            return False
        if isinstance(parent, exp.Subquery):
            node = parent
            continue
        if isinstance(parent, exp.SetOperation):
            node = parent
            continue
        if isinstance(parent, (exp.From, exp.Join, exp.Exists, exp.CTE, exp.Any, exp.All)):
            return False
        if isinstance(parent, exp.In) and node.arg_key == "query":
            return False
        return True


def _loose_columns(select: Any) -> bool:
    """聚合的一段里既不在聚合函数里、也不是分组键（的一部分）的列：SQLite、MySQL 取组里任意一行的值。"""
    from sqlglot import exp

    if not _aggregating(select):
        return False
    group = select.args.get("group")
    keys = {_fingerprint(k) for k in (group.expressions if group is not None else [])}

    def loose(node: Any) -> bool:
        if isinstance(node, (exp.Query, exp.Subquery)) or _fingerprint(node) in keys:
            return False
        if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window):
            return False
        if isinstance(node, exp.Window):
            return False
        if isinstance(node, exp.Column):
            return not node.is_star
        return any(loose(c) for c in node.iter_expressions())

    clauses = [c for c in (*select.expressions, select.args.get("having"), select.args.get("order")) if c is not None]
    return any(loose(c) for c in clauses)


# ---- 动来源的改写

def _unwrap_passthrough(tree: Any, read: str | None) -> Any:
    """原样选出 FROM 里唯一一个子查询的全部列、什么别的都不做的一段（SELECT * FROM (…) t、SELECT t.a, t.b FROM (…) t），
    换成那个子查询：出的行一样，列的先后也一样。最外层和条件里的子查询列名不影响取值，列名可以不同；别的地方（FROM 里
    的子查询、CTE）外面按名字引用，名字也得一样。不换的：子查询自己带 ORDER BY / LIMIT / OFFSET（套一层之后顺序、取行
    不一定照旧）；子查询是 UNION 这类合并、库是 SQLite（和不知道是哪种库时）——SQLite 从子查询里读合并的结果时，按第一支
    那一列的类型亲和转换别的支的值（REAL 列在前，另一支的整数 2 读出来成了 2.0），套不套这一层结果不一样。"""
    from sqlglot import exp

    def outputs(query: Any) -> list[str] | None:
        if any(query.args.get(k) is not None for k in ("order", "limit", "offset", "fetch")):
            return None
        if isinstance(query, exp.SetOperation):
            if read in (None, "sqlite"):
                return None
            leaves = _leaves(query)
            return outputs(leaves[0]) if leaves and isinstance(leaves[0], exp.Select) else None
        if not isinstance(query, exp.Select) or _starry(query) or query.args.get("with_") is not None:
            return None
        names = [p.alias_or_name for p in query.expressions]
        return names if all(names) and len(set(names)) == len(names) else None

    changed = True
    while changed:
        changed = False
        for select in list(tree.find_all(exp.Select)):
            if select.args.get("joins") or select.args.get("from_") is None or \
                    {k for k, v in select.args.items() if v} - {"expressions", "from_", "with_"} or \
                    (select.args.get("with_") is not None and select is not tree):
                continue
            item = select.args["from_"].this
            if not isinstance(item, exp.Subquery):
                continue
            body = item.this
            names = outputs(body)
            if names is None:
                continue
            alias = item.alias_or_name
            star = len(select.expressions) == 1 and isinstance(select.expressions[0], exp.Star)
            cols = [p.unalias() for p in select.expressions]
            if not star and (len(cols) != len(names) or any(
                    not isinstance(c, exp.Column) or c.is_star or c.name != n or (c.table and c.table != alias)
                    for c, n in zip(cols, names))):
                continue
            renamed = not star and any(p.alias_or_name != n for p, n in zip(select.expressions, names))
            parent = select.parent
            loose = select is tree or isinstance(parent, (exp.In, exp.Exists)) or (
                isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.From, exp.Join, exp.CTE)))
            if renamed and not loose:
                continue
            new = body.copy()
            if select is tree:
                if select.args.get("with_") is not None:
                    if new.args.get("with_") is not None:
                        continue
                    new.set("with_", select.args["with_"])
                tree = new
            else:
                select.replace(new)
            changed = True
            break
    return tree


def _bare_star(select: Any) -> bool:
    from sqlglot import exp

    return any(isinstance(p, exp.Star) for p in select.expressions)


def _right_to_left(tree: Any, sens: _Sensitivity) -> None:
    """只有两个来源的 a RIGHT JOIN b ON p 就是 b LEFT JOIN a ON p。选的是 * 时列的先后跟着变、结果看行出来的先后的
    一段扫描的先后跟着变，不改。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if len(joins) != 1 or str(joins[0].side or "").upper() != "RIGHT" or not _plain_join(joins[0]) or \
                joins[0].args.get("on") is None or _bare_star(select) or sens.of(select):
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


def _quantified(node: Any) -> bool:
    """ANY / SOME / ALL (子查询或数组)：sqlglot 有时解析成 Any / All 节点，有时（ALL (ARRAY[]::int[])）解析成名字叫
    ALL 的函数调用。拿它比较的结果不是普通比较的规矩：x > ALL (空集) 是真，NOT (x = ANY …) 也不是 x <> ANY …。"""
    from sqlglot import exp

    node = _unparen(node)
    return isinstance(node, (exp.Any, exp.All)) or (
        isinstance(node, exp.Anonymous) and str(node.name).upper() in ("ALL", "ANY", "SOME"))


def _null_propagating(node: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时，node 一定是空值：那一侧的列，或者对它做算术、取负、取大小写、取长度、绝对值、转类型；
    SQLite 里的 || 拼接（拿空值拼得空值）。PostgreSQL 的 || 不算（空的数组拼上一个数组还是那个数组：NULL || ARRAY['x']
    是 ARRAY['x']），MySQL 的 || 默认是 OR，CONCAT 在 PostgreSQL 里跳过空值：都不算。alias 也可以是判断「这一列是不是
    那个为空的东西」的函数（见 _drop_implied_not_null：按一列为空算）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Column):
        if callable(alias):
            return bool(alias(node))
        return bool(node.table) and node.table == alias
    if type(node).__name__ in _STRICT_UNARY:
        return _null_propagating(node.this, alias, read)
    if type(node).__name__ in _STRICT_BINARY or (isinstance(node, exp.DPipe) and read == "sqlite"):
        return _null_propagating(node.left, alias, read) or _null_propagating(node.right, alias, read)
    return False


#: 拿空值比较得空值的比较（ANY / ALL 另算，见 _quantified）
_COMPARISON_NODES = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike")


def _strict(cond: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时，cond 一定是空值（不是真、也不是假）：一边一定是空值的比较、LIKE、BETWEEN，非空常量
    列表的 IN，以及这些用 NOT、AND、OR 连起来（NOT 空值还是空值，空值 AND / OR 空值还是空值）。
    不算的：IN 子查询（子查询没有行时是假，不是空值）、x IN ()（SQLite 允许空列表，恒为假）、ANY / ALL（子查询没有行
    时是假或真）、IS [NOT] NULL、COALESCE、CASE。"""
    from sqlglot import exp

    cond = _unparen(cond)
    if type(cond).__name__ in _COMPARISON_NODES:
        if _quantified(cond.left) or _quantified(cond.right):
            return False
        return _null_propagating(cond.left, alias, read) or _null_propagating(cond.right, alias, read)
    if isinstance(cond, exp.In):
        if cond.args.get("query") or cond.args.get("unnest") or cond.args.get("field") or not cond.expressions:
            return False
        return _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Between):
        return _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Not):
        return _strict(cond.this, alias, read)
    if isinstance(cond, (exp.And, exp.Or)):
        return _strict(cond.left, alias, read) and _strict(cond.right, alias, read)
    return False


def _rejects(cond: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时 cond 一定不成立（是空值或假），WHERE 里有它就留不下没关联上的行：

    - 一定是空值的（见 _strict）；
    - x IS NOT NULL；x IN (子查询)、x op ANY (子查询)（子查询没有行时是假，有行时 x 为空是空值）；x IN ()（恒为假）；
      BETWEEN 的上下界一定是空值的（x >= 空值 AND x <= b 不会是真）；
    - NOT 只能套一定是空值的（NOT 假是真：NOT (x IN (空集)) 在 x 为空时是真，NOT IN (空集) 留得下没关联上的行）；
    - AND 里有一项不成立、OR 里每一项都不成立。

    x op ALL (…) 不算（子查询没有行时是真）；IS NULL、COALESCE、CASE、<=>、IS DISTINCT FROM 之类不算。"""
    from sqlglot import exp

    cond = _unparen(cond)
    if _strict(cond, alias, read):
        return True
    if type(cond).__name__ in _COMPARISON_NODES:
        for side, other in ((cond.left, cond.right), (cond.right, cond.left)):
            if isinstance(_unparen(other), exp.Any) and _null_propagating(side, alias, read):
                return True
        return False
    if isinstance(cond, exp.In):
        if cond.args.get("unnest") or cond.args.get("field"):
            return False
        if not cond.expressions and not cond.args.get("query"):
            return True
        return bool(cond.args.get("query")) and _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Between):
        return any(_null_propagating(cond.args.get(k), alias, read) for k in ("low", "high") if cond.args.get(k))
    if isinstance(cond, exp.Not):
        inner = _unparen(cond.this)
        if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
            return _null_propagating(inner.this, alias, read)
        return _strict(inner, alias, read)
    if isinstance(cond, exp.And):
        return _rejects(cond.left, alias, read) or _rejects(cond.right, alias, read)
    if isinstance(cond, exp.Or):
        return _rejects(cond.left, alias, read) and _rejects(cond.right, alias, read)
    return False


def _reject_nulls(tree: Any, read: str | None, sens: "_Sensitivity") -> None:
    """LEFT JOIN 关联进来的那一侧，同一段 WHERE 里有把它整行为空的行筛掉的条件（见 _rejects）：没关联上的行反正留
    不下来，这次外连接就是内连接。有 RIGHT / FULL JOIN 的段不动；结果看行出来先后的一段（GROUP_CONCAT、有并列的取行，
    见 _Sensitivity）也不动：SQLite 自己把这样的 LEFT JOIN 降成内连接之后，扫描的先后和直接写内连接的不一样（真实的
    SQLite 上 EXPLAIN QUERY PLAN 一个先扫 refunds、一个先扫 orders，GROUP_CONCAT 拼出来的先后就不同，第 3 轮自己压测
    时找到的）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        where = select.args.get("where")
        if where is None or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins) or sens.of(select):
            continue
        conds = _conjuncts(where.this)
        for join in joins:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join):
                continue
            alias = join.this.alias_or_name
            if alias and any(_rejects(c, alias, read) for c in conds):
                join.set("side", None)
                join.set("kind", None)
                join.this.meta[_REJECTED] = "RIGHT JOIN" if join.meta.get(_FLIPPED) else "LEFT JOIN"


#: 跳过空值的聚合：参数在一行里是空值时这一行不算数（COUNT(*) 数的是行数，不算）
_NULL_SKIPPING = ("Sum", "Avg", "Min", "Max", "Count")


def _aggregate_args(select: Any) -> list[Any] | None:
    """这一段 SELECT 列表、HAVING、ORDER BY 里的聚合（不算窗口函数）都是跳过空值的那几种时，返回它们的参数（DISTINCT
    拆开）；有别的聚合、COUNT(*)、多个参数的、套着子查询或窗口函数的返回 None。"""
    from sqlglot import exp

    clauses = [c for c in (*select.expressions, select.args.get("having"), select.args.get("order")) if c is not None]
    if any(c.find(exp.Query, exp.Subquery, exp.Window) is not None for c in clauses):
        return None
    args = []
    for clause in clauses:
        for agg in clause.find_all(exp.AggFunc):
            if type(agg).__name__ not in _NULL_SKIPPING or agg.args.get("expressions"):
                return None
            arg = agg.this
            if isinstance(arg, exp.Distinct):
                if len(arg.expressions) != 1:
                    return None
                arg = arg.expressions[0]
            if arg is None or isinstance(arg, exp.Star):
                return None
            args.append(arg)
    return args


def _null_blind_left_joins(tree: Any, read: str | None, sens: _Sensitivity) -> None:
    """不分组、结果只是聚合的一段（只出一行），所有聚合都跳过空值（SUM、AVG、MIN、MAX、COUNT(x)，可以去重）、参数在
    LEFT JOIN 可空一侧整行为空时一定是空值（见 _null_propagating）：这次 LEFT JOIN 写成内连接结果一样。LEFT JOIN 的
    结果是内连接的结果加上没关联上的行（那一侧整行是空值），之后的内连接、LEFT JOIN、WHERE 都是一行一行处理的，没
    关联上的行带来的那些行里这一侧也都是空值，在这些聚合里都被跳过；COALESCE(SUM(r.amount), 0)、SUM(x) / COUNT(y)、
    旁边多一个常量列都只是拿这几个聚合再算。不改的：分组（没关联上的行自己成一组）、COUNT(*)、参数可能不为空的
    （COALESCE(r.amount, 0)）、聚合以外还有列、有 RIGHT / FULL JOIN、结果看行出来先后的一段（PostgreSQL / MySQL 里
    浮点数求和的先后会变，见 _Sensitivity）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        group = select.args.get("group")
        if (group is not None and group.expressions) or sens.of(select) or _loose_columns(select):
            continue
        args = _aggregate_args(select)
        if not args:
            continue
        for join in joins:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join) or join.args.get("on") is None:
                continue
            alias = join.this.alias_or_name
            if alias and all(_null_propagating(arg, alias, read) for arg in args):
                join.set("side", None)
                join.set("kind", None)


def _membership_context(select: Any) -> bool:
    """这一段（或者它所在的 UNION 的一支）是 IN 子查询、EXISTS 子查询的全部：重复的行不影响结果。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.Union)):
        if isinstance(node.parent, exp.Subquery) and node.parent.alias:
            return False
        node = node.parent
    parent = node.parent
    return isinstance(parent, exp.Exists) or (isinstance(parent, exp.In) and node.arg_key == "query")


def _dup_insensitive(select: Any) -> bool:
    """这一段的结果不怕来源的行重复：聚合查询里的聚合都是 MIN、MAX、COUNT / SUM / AVG(DISTINCT …) 这类去重的；不聚合
    时 SELECT DISTINCT（不是 DISTINCT ON），或者整段是 IN / EXISTS 子查询。有窗口函数的不算。"""
    from sqlglot import exp

    own = list(_block_nodes(select))
    if any(isinstance(n, exp.Window) for n in own):
        return False
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    if _aggregating(select):
        aggs = [n for n in own if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window)]
        return all((isinstance(a, (exp.Min, exp.Max)) and not a.args.get("expressions")) or
                   (type(a).__name__ in ("Count", "Sum", "Avg") and isinstance(a.this, exp.Distinct) and
                    len(a.this.expressions) == 1) for a in aggs)
    return distinct is not None or _membership_context(select)


def _unique_key_bound(item: Any, conds: list[Any], info: _Schema, read: str | None) -> bool:
    """关联进来的实表 item 的某个唯一键（主键、唯一约束，见 schema_cache 的 primary_key / unique）的每一列，都被 conds
    里的「这一列 = 不涉及这张表的表达式」定住了：左边每一行最多关联上它一行（相等的条件匹配不上空值）。SQLite 里只认
    整数、文本亲和的键列（没有类型亲和的列能同时存 1 和 '1'，比较时按数值转换就能匹配上两行）。"""
    from sqlglot import exp

    if not isinstance(item, exp.Table) or item.args.get("db") or \
            item.name.translate(_ASCII_LOWER) in _visible_ctes(item):
        return False
    alias, table = item.alias_or_name, item.name.translate(_ASCII_LOWER)
    bound: set[str] = set()
    for cond in conds:
        cond = _unparen(cond)
        if not isinstance(cond, exp.EQ):
            continue
        for x, y in ((cond.this, cond.expression), (cond.expression, cond.this)):
            x = _unparen(x)
            if isinstance(x, exp.Column) and x.table == alias and y.find(exp.Query, exp.Subquery) is None and \
                    not any(c.table == alias or not c.table for c in y.find_all(exp.Column)) and not _volatile(y):
                bound.add(x.name.translate(_ASCII_LOWER))
    keys = ([info.pk[table]] if info.pk.get(table) else []) + info.unique.get(table, [])
    types = info.types.get(table, {})
    for key in keys:
        if key and all(k in bound for k in key) and \
                (read != "sqlite" or all(_identity_type(types.get(k, ""), read, None) for k in key)):
            return True
    return False


def _drop_unused_left_joins(tree: Any, sens: _Sensitivity, info: _Schema, read: str | None) -> None:
    """LEFT JOIN 进来的表除了它自己的 ON 之外哪儿都没用到（本段、相关子查询、后面的连接都不引用它），而且下面两种情况
    之一：结果不怕重复的行（SELECT DISTINCT；聚合都是 MIN、MAX、COUNT(DISTINCT …) 这类去重的；整段是 IN / EXISTS
    子查询）；或者 ON 用它的唯一键（主键、唯一约束）把它定死了、左边每一行最多关联上一行（见 _unique_key_bound）。
    这次 LEFT JOIN 去掉结果一样：LEFT JOIN 不会让左边的行变少，只会让关联上好几行的左边的行重复出现（唯一键定死时
    连重复都没有），别的列的取值不变。不去的：有 COUNT(*)、SUM 这类怕重复的聚合又没有唯一键，有 RIGHT / FULL JOIN、
    窗口函数、SELECT *，有不带表名的列（分不清是不是这张表的），结果看行出来先后的一段（DISTINCT 留下先扫到的那个值、
    取行，见 _Sensitivity）。"""
    from sqlglot import exp

    changed = True
    while changed:
        changed = False
        for select in list(tree.find_all(exp.Select)):
            joins = select.args.get("joins") or []
            if not joins or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins) or sens.of(select) or \
                    any(isinstance(p, exp.Star) for p in select.expressions):
                continue
            insensitive = _dup_insensitive(select)
            own = list(_block_nodes(select))
            nested = [c for sub in _nested_queries(select) for c in sub.find_all(exp.Column)]
            columns = [n for n in own if isinstance(n, exp.Column)] + nested
            if any(not c.table for c in columns):
                continue
            for join in joins:
                if str(join.side or "").upper() != "LEFT" or not _plain_join(join) or join.args.get("on") is None:
                    continue
                alias = join.this.alias_or_name
                on = {id(n) for n in join.args["on"].walk()}
                if alias and not any(c.table == alias and id(c) not in on for c in columns) and \
                        (insensitive or _unique_key_bound(join.this, _conjuncts(join.args["on"]), info, read)):
                    select.set("joins", [j for j in joins if j is not join] or None)
                    changed = True
                    break
            if changed:
                break


def _drop_implied_not_null(tree: Any, read: str | None, info: _Schema, sens: _Sensitivity) -> None:
    """WHERE、HAVING、ON 里 AND 连着的 x IS NOT NULL，在下面两种情况下去掉（这几处只认「是真」，假和空值一样筛掉）：

    - 同一组 AND 里另有一项在 x 为空时一定不成立（x = y、x > 3、x IN (…)，见 _rejects）：那一项成立时 x 已经不为空；
    - 表结构里 x 确定不为空（声明了 NOT NULL，或者是主键，见 _not_null_column），而且 x 所在的表在这一段里不是外连接
      可空的一侧（在这次 LEFT JOIN 自己的 ON 里时，它就是这次关联进来的那一行，不会是空值）。

    只认带表名的一列；外连接改内连接（_reject_nulls）在这之前做完，靠 IS NOT NULL 改成的内连接不受影响。"""
    from sqlglot import exp

    def target(part: Any) -> Any:
        part = _unparen(part)
        if isinstance(part, exp.Not):
            inner = _unparen(part.this)
            if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null) and \
                    isinstance(_unparen(inner.this), exp.Column) and _unparen(inner.this).table:
                return _unparen(inner.this)
        return None

    for select in list(tree.find_all(exp.Select)):
        if sens.of(select):
            # 少一个条件可能换一种扫描的先后（走不走索引），结果看行出来先后的一段不动
            continue
        joins = select.args.get("joins") or []
        items = {i.alias_or_name: i for i in _items_of(select)}
        outer_join = any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins)
        nullable = {j.this.alias_or_name for j in joins if str(j.side or "").upper() == "LEFT"}
        ctes = _visible_ctes(select)
        holders = [(select.args.get("where"), "this", None), (select.args.get("having"), "this", None)] + \
            [(j, "on", j.this.alias_or_name) for j in joins if j.args.get("on") is not None]
        for holder, key, own in holders:
            if holder is None:
                continue
            parts = _conjuncts(holder.args.get(key))
            kept: list[Any] = []
            for i, part in enumerate(parts):
                column = target(part)
                if column is not None:
                    fp = _fingerprint(column)
                    others = kept + parts[i + 1:]
                    if any(_rejects(o, lambda c, fp=fp: _fingerprint(c) == fp, read) for o in others):
                        continue
                    item = items.get(column.table)
                    if isinstance(item, exp.Table) and not outer_join and \
                            (column.table == own or column.table not in nullable) and \
                            not (not item.args.get("db") and item.name.translate(_ASCII_LOWER) in ctes) and \
                            _not_null_column(info, item.name, column.name, read):
                        continue
                kept.append(part)
            if len(kept) == len(parts):
                continue
            if kept:
                holder.set(key, _and_all([p.copy() for p in kept]))
            elif isinstance(holder, exp.Join):
                holder.set(key, exp.true())
            elif isinstance(holder, exp.Having) and not _grouped_select(select):
                holder.set(key, exp.true())
            else:
                select.set(holder.arg_key, None)


def _not_null_source(select: Any, column: Any, info: _Schema, read: str | None) -> bool:
    """column（带表名）在这一段里一定不为空：来源是这一段 FROM / JOIN 里的实表（不是 CTE），表结构里这一列确定不为空
    （见 _not_null_column），这一段没有 RIGHT / FULL JOIN，它也不是 LEFT JOIN 可空的一侧（没关联上时整行补空）。"""
    from sqlglot import exp

    if not isinstance(column, exp.Column) or not column.table:
        return False
    joins = select.args.get("joins") or []
    if any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins) or \
            any(str(j.side or "").upper() == "LEFT" and j.this.alias_or_name == column.table for j in joins):
        return False
    item = next((i for i in _items_of(select) if i.alias_or_name == column.table), None)
    return isinstance(item, exp.Table) and not item.args.get("db") and \
        item.name.translate(_ASCII_LOWER) not in _visible_ctes(select) and \
        _not_null_column(info, item.name, column.name, read)


def _constraint_rewrites(tree: Any, read: str | None, info: _Schema, sens: _Sensitivity) -> None:
    """（认列之后，靠表结构里的约束）两处同义写法：

    - COUNT(x)：x 在这一段里一定不为空（见 _not_null_source）时就是 COUNT(*)（COUNT(x) 只是跳过 x 为空的行）。结果看
      行出来先后的一段不改（COUNT(*) 可能换一个索引扫）。
    - SELECT DISTINCT：只有一个来源（实表，不关联别的表）、选出来的列里包含它的一个唯一键（主键，或者每一列都确定不为空
      的唯一约束），又不分组不聚合时，每一行本来就不一样，DISTINCT 去掉。SQLite 里不是 INTEGER 的主键能存好几个空值，
      不算（见 _not_null_column）。结果看行出来先后的一段不动（去不去重可能换一种扫描的先后）。"""
    from sqlglot import exp

    if not info.types:
        return
    for count in list(tree.find_all(exp.Count)):
        arg = count.this
        select = count.find_ancestor(exp.Select)
        if select is None or isinstance(arg, exp.Distinct) or count.args.get("expressions") or \
                isinstance(count.parent, exp.Window) or not isinstance(arg, exp.Column) or sens.of(select):
            continue
        if _not_null_source(select, arg, info, read):
            count.set("this", exp.Star())
    for select in list(tree.find_all(exp.Select)):
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") or sens.of(select) or select.args.get("joins") or \
                _aggregating(select) or select.args.get("from_") is None:
            continue
        item = select.args["from_"].this
        if not isinstance(item, exp.Table) or item.args.get("db") or \
                item.name.translate(_ASCII_LOWER) in _visible_ctes(select):
            continue
        table = item.name.translate(_ASCII_LOWER)
        chosen = {p.unalias().name.translate(_ASCII_LOWER) for p in select.expressions
                  if isinstance(p.unalias(), exp.Column) and p.unalias().table == item.alias_or_name}
        keys = ([info.pk[table]] if info.pk.get(table) else []) + info.unique.get(table, [])
        if any(key and set(key) <= chosen and all(_not_null_column(info, item.name, k, read) for k in key)
               for key in keys):
            select.set("distinct", None)


#: 换成同一组相等的列之后，原文写的那一列（实表名、列名）记在新列的 meta 里：比等价时不算（见 _fingerprint），差异
#: 说明给人看时照原文写（见 _Explainer.show）
_WRITTEN_COLUMN = "sql_semantics_written_column"


def _substitute_equal_columns(tree: Any, read: str | None, info: _Schema, sens: _Sensitivity) -> None:
    """（认列之后）WHERE 里 AND 连着的「两列相等」（内连接的条件挪进 WHERE 之后也在这里）对 WHERE 之后的每一行都成立：
    两列都不为空、而且相等。两列声明的类型完全一样、这个类型下「相等就是一样」（整数、文本这类，见 _identity_type）时，
    SELECT 列表、GROUP BY、HAVING、ORDER BY 里用哪一列都是同一个值：都换成同一组里排在最前的那一列（按表名、列名排；
    同一张表关联了两次、分不清该选哪个别名的不换）。这样「按 users.id 分组」和「按 orders.user_id 分组」（ON
    o.user_id = u.id）就认得出是一回事。不换的：没给表结构、类型不一样（SQLite 里 1 和 '1' 也相等）、LEFT JOIN 的
    ON 里的相等（没关联上的行一边是空值）、本段有既不分组也不聚合的列（PostgreSQL 里那是错的写法，换了反而成了对的）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None or _loose_columns(select) or sens.of(select):
            continue
        items = {i.alias_or_name: i for i in _items_of(select)}
        ctes = _visible_ctes(select)

        def base(col: Any) -> tuple[str, str, str] | None:
            item = items.get(col.table)
            if not isinstance(item, exp.Table) or item.args.get("db") or \
                    item.name.translate(_ASCII_LOWER) in ctes:
                return None
            table, column = item.name.translate(_ASCII_LOWER), col.name.translate(_ASCII_LOWER)
            declared = info.types.get(table, {}).get(column)
            if not declared or not _identity_type(declared, read, sens):
                return None
            return table, column, declared.strip().upper()

        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(x: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(x, x)
            while parent[x] != x:
                x = parent[x]
            return x

        bases: dict[tuple[str, str], tuple[str, str, str]] = {}
        for cond in _conjuncts(where.this):
            cond = _unparen(cond)
            if not isinstance(cond, exp.EQ):
                continue
            left, right = _unparen(cond.this), _unparen(cond.expression)
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table and right.table):
                continue
            bl, br = base(left), base(right)
            if bl is None or br is None or bl[2] != br[2]:
                continue
            kl, kr = (left.table, left.name), (right.table, right.name)
            bases[kl], bases[kr] = bl, br
            parent[find(kl)] = find(kr)
        classes: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for member in list(parent):
            classes.setdefault(find(member), []).append(member)
        replace: dict[tuple[str, str], tuple[str, str]] = {}
        for members in classes.values():
            if len(members) < 2:
                continue
            ranked = sorted(members, key=lambda m: bases[m][:2])
            if len({bases[m][:2] for m in members}) != len(members):
                continue
            for m in members:
                if m != ranked[0]:
                    replace[m] = ranked[0]
        if not replace:
            continue
        clauses = [*select.expressions, select.args.get("group"), select.args.get("having"), select.args.get("order")]
        for clause in [c for c in clauses if c is not None]:
            for col in list(clause.find_all(exp.Column)):
                if col.find_ancestor(exp.Select) is not select or (col.table, col.name) not in replace:
                    continue
                table, name = replace[(col.table, col.name)]
                new = exp.column(name, table=table)
                new.meta[_WRITTEN_COLUMN] = bases[(col.table, col.name)][:2]
                if col.parent is select and col.arg_key == "expressions":
                    new = exp.alias_(new, col.alias_or_name, copy=False)
                col.replace(new)


def _identity_type(declared: str, read: str | None, sens: _Sensitivity | None) -> bool:
    """声明的类型下「相等就是一样」、可以拿同一组相等的列互换：SQLite 里整数、文本亲和的列（实数亲和的 0.0 和 -0.0
    相等却不一样，数值亲和、没有亲和的列能同时存 1 和 1.0）；PostgreSQL、MySQL 按 _Sensitivity._safe_type。"""
    upper = declared.upper()
    if read == "sqlite":
        return "INT" in upper or any(w in upper for w in ("CHAR", "CLOB", "TEXT"))
    return sens is not None and sens._safe_type(declared)


def _int_value(node: Any) -> int | None:
    """只由整数常量和加、减、乘、取负组成的式子的值（LIMIT 1 + 2 是 3）；别的返回 None。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.this)
    if isinstance(node, exp.Neg):
        value = _int_value(node.this)
        return None if value is None else -value
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        left, right = _int_value(node.left), _int_value(node.right)
        if left is None or right is None:
            return None
        if isinstance(node, exp.Add):
            return left + right
        return left - right if isinstance(node, exp.Sub) else left * right
    return None


def _normalize_limits(tree: Any, read: str | None) -> None:
    """取行的几种同义写法：

    - SQLite、PostgreSQL 的 LIMIT / OFFSET 可以写算式：只由整数常量加减乘出来的算成一个数（LIMIT 1 + 2 就是 LIMIT 3）。
      MySQL 的 LIMIT 只能写常量，算式是语法错，不算。
    - SQLite 的 LIMIT 是负数就是不限行数（SQLite 文档原话）：没有 OFFSET 时去掉 LIMIT，有 OFFSET 时统一写成 LIMIT -1。
      PostgreSQL 的负数 LIMIT 报错，不动。
    - PostgreSQL 的 LIMIT NULL 和 LIMIT ALL（sqlglot 解析时已经去掉）就是不限行数（PostgreSQL 文档原话）。SQLite 的
      LIMIT NULL 报错，不动。"""
    from sqlglot import exp

    def number(value: int) -> Any:
        return exp.Literal.number(value) if value >= 0 else exp.Neg(this=exp.Literal.number(-value))

    if read not in ("sqlite", "postgres"):
        return
    for query in list(tree.find_all(exp.Select, exp.SetOperation)):
        for key in ("limit", "offset"):
            node = query.args.get(key)
            if not isinstance(node, (exp.Limit, exp.Offset)) or node.expression is None:
                continue
            value = _int_value(node.expression)
            if value is not None:
                node.set("expression", number(value))
        limit = query.args.get("limit")
        if not isinstance(limit, exp.Limit) or limit.expression is None:
            continue
        if isinstance(query, exp.Select) and (_loose_columns(query) or any(
                isinstance(n, exp.Window) or _order_dependent(n) for n in _block_nodes(query))):
            # 有 GROUP_CONCAT、窗口函数、既不分组也不聚合的列：去不去掉 LIMIT 子句可能换一种扫描的先后，结果跟着变
            continue
        value = _int_value(limit.expression)
        if read == "sqlite" and value is not None and value < 0:
            if query.args.get("offset") is None:
                query.set("limit", None)
            else:
                limit.set("expression", number(-1))
        elif read == "postgres" and isinstance(_unparen(limit.expression), exp.Null):
            query.set("limit", None)


def _dedupe_order(tree: Any, sens: _Sensitivity) -> None:
    """ORDER BY 里和前面某一项一模一样的项去掉（不管升序降序）：前面那项相等的行，这一项也相等，排不出先后。结果看行
    出来先后的一段（取行、窗口函数、GROUP_CONCAT）不去：并列的行谁在前要看排序的实现，多一个排序键可能就换了。"""
    from sqlglot import exp

    for order in list(tree.find_all(exp.Order)):
        owner = order.find_ancestor(exp.Select)
        if owner is None or sens.of(owner):
            continue
        seen: set[str] = set()
        kept = []
        for item in order.expressions:
            key = _fingerprint(item.this if isinstance(item, exp.Ordered) else item)
            if key not in seen:
                seen.add(key)
                kept.append(item)
        if len(kept) != len(order.expressions):
            order.set("expressions", kept)


def _flatten_prefix(tree: Any, sens: _Sensitivity, read: str | None) -> None:
    """开头那组内连接（FROM 和紧跟着的内连接、CROSS JOIN、逗号连接，到第一个外连接为止）的 ON 条件挪进 WHERE、写成
    逗号连接：内连接的条件写在 ON 还是 WHERE 一样；后面的 LEFT JOIN 对左边每一行各自关联，左边先筛后筛一样。后面有
    RIGHT / FULL JOIN 的不动：没关联上的那一侧整行空值，挪到 WHERE 的条件会把它们筛掉（PostgreSQL、MySQL 里逗号还比
    JOIN 结合得松，a, b RIGHT JOIN c 是 a 乘上 b RIGHT JOIN c）。内连接的条件写在 ON 还是 WHERE，优化器一视同仁，结果
    看行出来先后的一段也照做；只有 SQLite 里结果看先后、又写了 CROSS JOIN 的一段不动（SQLite 不调换 CROSS JOIN 两边、
    会调换逗号连接）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins or select.args.get("from_") is None or \
                any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        if read in (None, "sqlite") and sens.of(select) and \
                any(str(j.kind or "").upper() == "CROSS" or j.meta.get("sql_semantics_join") for j in joins):
            continue
        moved: list[Any] = []
        for join in joins:
            if not _inner(join):
                break
            on = join.args.get("on")
            if on is not None:
                moved += _conjuncts(on)
                join.set("on", None)
            join.set("kind", None)
            if not sens.of(select):
                join.meta.pop("sql_semantics_join", None)
        if moved:
            where = select.args.get("where")
            select.set("where", exp.Where(this=_and_all((_conjuncts(where.this) if where else []) + moved)))


# ---- 和来源无关的改写

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
    """开头那组内连接里的一项：写成逗号连接、条件都在 WHERE 里（见 _flatten_prefix）。"""
    return _plain_join(join) and not join.side and not join.args.get("kind") and join.args.get("on") is None


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


def _normalize_negation(tree: Any) -> None:
    """x IS NOT NULL、x NOT LIKE y 这类带 negate 的写法统一写成 NOT (x IS NULL)、NOT (x LIKE y)：各方言解析出来的形状
    不一样（PostgreSQL 的 IS NOT NULL 是 Is(negate=True)，SQLite 的是 Not(Is)），意思一样。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Predicate)):
        if node.args.get("negate") is True:
            node.set("negate", None)
            copy = node.copy()
            node.replace(exp.Not(this=exp.Paren(this=copy)))


def _case_forms(tree: Any, read: str | None) -> None:
    """CASE … ELSE NULL END 的 ELSE NULL 去掉（不写 ELSE 就是空值）；SQLite 的 IIF(c, a, b)、MySQL 的 IF(c, a, b) 写成
    CASE WHEN c THEN a ELSE b END（SQLite 文档：两者生成同样的字节码；MySQL 的 IF 在 c 为真时取 a，否则取 b）。"""
    from sqlglot import exp

    if read in ("sqlite", "mysql"):
        for node in list(tree.find_all(exp.If)):
            if isinstance(node.parent, exp.Case) or node.args.get("true") is None:
                continue
            case = exp.Case(ifs=[exp.If(this=node.this.copy(), true=node.args["true"].copy())])
            if node.args.get("false") is not None:
                case.set("default", node.args["false"].copy())
            node.replace(case)
    for case in tree.find_all(exp.Case):
        if isinstance(case.args.get("default"), exp.Null):
            case.set("default", None)


def _drop_true_conjuncts(tree: Any) -> None:
    """WHERE、HAVING、ON 里恒为真的项（TRUE、1 = 1 这种同一个常量和自己相等；分组之后 HAVING COUNT(*) >= 1 这种对
    每一组都成立的）去掉：x AND TRUE 就是 x。全都去掉了的 HAVING 只在已经是聚合查询时去掉（见 _grouped_select）。"""
    from sqlglot import exp

    def true(node: Any) -> bool:
        node = _unparen(node)
        if isinstance(node, exp.Boolean):
            return bool(node.this)
        if isinstance(node, (exp.EQ, exp.GTE, exp.LTE)):
            left, right = _unparen(node.this), _unparen(node.expression)
            return isinstance(left, exp.Literal) and isinstance(right, exp.Literal) and \
                _fingerprint(left) == _fingerprint(right)
        return False

    def always(node: Any, holder: Any) -> bool:
        # 分组之后每组至少一行：HAVING COUNT(*) >= 1、COUNT(*) > 0 对每一组都成立（不分组时没有行也出一组，COUNT(*)
        # 是 0，不算）。按取值逐段算（见 _Range），COUNT(x) 可能是 0，不算
        if not isinstance(holder, exp.Having) or not isinstance(holder.parent, exp.Select):
            return False
        group = holder.parent.args.get("group")
        if group is None or not group.expressions:
            return False
        found = _Range(None).subject(node)
        subject = _unparen(found[1]) if found is not None else None
        if not (isinstance(subject, exp.Count) and isinstance(subject.this, exp.Star)):
            return False
        return _Range(None).compare([node], [], grouped=True) is True

    holders = [(w, "this") for w in tree.find_all(exp.Where, exp.Having)] + \
        [(j, "on") for j in tree.find_all(exp.Join) if j.args.get("on") is not None]
    for holder, key in holders:
        cond = holder.args.get(key)
        parts = _conjuncts(cond)
        kept = [p for p in parts if not true(p) and not always(p, holder)]
        if len(kept) == len(parts):
            continue
        if kept:
            holder.set(key, _and_all([p.copy() for p in kept]))
        elif isinstance(holder, exp.Join):
            holder.set(key, exp.true())
        elif isinstance(holder, exp.Having) and not _grouped_select(holder.parent):
            # 没有 GROUP BY、别处也没有聚合的 HAVING 让整条语句成了只出一行的聚合（PostgreSQL：SELECT 1 FROM orders
            # HAVING 1 = 1 只出一行），MySQL 里又是按行筛：整个去掉就变了，统一写成 HAVING TRUE
            holder.set(key, exp.true())
        else:
            holder.parent.set(holder.arg_key, None)


def _grouped_select(select: Any) -> bool:
    """这一段不看 HAVING 也已经是聚合查询：有 GROUP BY，或者 SELECT 列表、ORDER BY 里有聚合函数（不算窗口函数）。这时
    HAVING TRUE 去不去掉都是那些组。"""
    from sqlglot import exp

    if not isinstance(select, exp.Select):
        return False
    group = select.args.get("group")
    if group is not None and group.expressions:
        return True
    clauses = [*select.expressions, select.args.get("order")]
    return any(not isinstance(n.parent, exp.Window) and n.find_ancestor(exp.Select) is select
               for c in clauses if c is not None for n in c.find_all(exp.AggFunc))


def _count_star(tree: Any) -> None:
    """COUNT(1)、COUNT('x')：数的是不为空的常量，就是行数。COUNT(DISTINCT 1)（有行就是 1）、COUNT(NULL) 不是。"""
    from sqlglot import exp

    for c in list(tree.find_all(exp.Count)):
        if isinstance(c.this, exp.Literal) and not c.args.get("distinct"):
            c.set("this", exp.Star())


def _minmax_distinct(tree: Any, sens: "_Sensitivity | None" = None) -> None:
    """MIN(DISTINCT x)、MAX(DISTINCT x) 就是 MIN(x)、MAX(x)：去不去重最小最大的都是那个值。结果看行出来先后的一段不改
    （带 DISTINCT 的聚合可能换一种执行方式）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Min, exp.Max)):
        owner = node.find_ancestor(exp.Select)
        if sens is not None and owner is not None and sens.of(owner):
            continue
        arg = node.this
        if not node.expressions and isinstance(arg, exp.Distinct) and len(arg.expressions) == 1 and \
                not arg.args.get("on"):
            node.set("this", arg.expressions[0])


def _simple_value(node: Any) -> bool:
    """只是列、常量、* 和它们的加减乘除（没有函数调用：PostgreSQL 里 SELECT 列表的集合返回函数会改变行数）。"""
    from sqlglot import exp

    return all(isinstance(n, (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Star, exp.Identifier, exp.Paren,
                              exp.Neg, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Alias)) for n in node.walk())


def _union_leaves(query: Any) -> list[Any] | None:
    """一串只用 UNION / UNION ALL 连起来、哪一层都不排序不取行的合并的各支；有 INTERSECT / EXCEPT（看的是取值）、
    ORDER BY / LIMIT / OFFSET（取哪几行看去没去重）、WITH 的返回 None。"""
    from sqlglot import exp

    leaves: list[Any] = []
    stack = [query]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            if node.alias:
                return None
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            if not isinstance(node, exp.Union) or \
                    any(node.args.get(k) is not None for k in ("order", "limit", "offset", "fetch", "with_")):
                return None
            stack += [node.expression, node.this]
        else:
            leaves.append(node)
    return leaves


def _set_union_all(query: Any) -> None:
    """一串 UNION 都改成 UNION ALL（调用方确认了重复的行不影响结果）。"""
    from sqlglot import exp

    for node in [query, *query.find_all(exp.Union)]:
        if isinstance(node, exp.Union):
            node.set("distinct", False)


def _exists_rewritable(sub: Any) -> bool:
    """EXISTS 里的这一段能不能写成 SELECT 1、去掉去重 / 分组 / 排序 / 取前 n 行（见 _exists_subqueries）。"""
    from sqlglot import exp

    if not isinstance(sub, exp.Select):
        return False
    if any(sub.args.get(k) is not None for k in ("having", "offset", "fetch", "qualify")):
        return False
    if any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(sub)):
        return False
    distinct = sub.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    group = sub.args.get("group")
    if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    limit = sub.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int and int(value.this) >= 1):
            return False
    if not all(_simple_value(p) for p in sub.expressions):
        return False
    # WHERE 里按名字引用了选出来的列别名（SQLite 认：WHERE … AND a2 > 100 里的 a2 是 r.amount * 2 AS a2）：写成
    # SELECT 1 之后这个名字就指不到原来的列了
    return not (_alias_refs(sub) & {p.alias for p in sub.expressions if isinstance(p, exp.Alias) and p.alias})


def _exists_subqueries(tree: Any) -> None:
    """EXISTS 只看有没有行：选什么、去不去重、怎么分组、怎么排都不影响，统一写成 SELECT 1；只取前 n 行（n ≥ 1、不跳过
    行）也不影响有没有行；分组（没有 HAVING、没有聚合）时有行就至少有一组；合并的各支是 UNION 还是 UNION ALL 也不
    影响（有没有行和去不去重无关），各支都写得成 SELECT 1 时一起写。动之前先确认这一段不是聚合查询（SELECT、HAVING、
    ORDER BY 里有聚合函数时总出一行，ORDER BY COUNT(*) 去掉就成了普通查询）、不跳过行（DISTINCT、GROUP BY 在 OFFSET
    之前算，去掉它们行数就变了）、选的只是列和常量（PostgreSQL 的集合返回函数会改变行数）、WHERE 里没按名字引用选出来
    的列别名。"""
    from sqlglot import exp

    for ex in list(tree.find_all(exp.Exists)):
        body = ex.this.this if isinstance(ex.this, exp.Subquery) else ex.this
        if isinstance(body, exp.SetOperation):
            leaves = _union_leaves(body)
            if leaves is None:
                continue
            _set_union_all(body)
        else:
            leaves = [body]
        if not all(_exists_rewritable(leaf) for leaf in leaves):
            continue
        for sub in leaves:
            for key in ("limit", "distinct", "order", "group"):
                sub.set(key, None)
            sub.set("expressions", [exp.Literal.number(1)])


def _in_lists(tree: Any) -> None:
    """IN 列表是个集合：一模一样的项去掉、排序；只有一个常量的写成 =（NOT IN 跟着成了 <>，空值的结果一样）。只有
    一项、又不是常量的不改：SQLite 里 a IN (b) 是 a = +b，右边那一列不带类型亲和，和 a = b 结果不同。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.In)):
        if not node.expressions or node.args.get("query") or node.args.get("unnest") or node.args.get("field"):
            continue
        uniq = {_fingerprint(i): i for i in node.expressions}
        ordered = [uniq[k] for k in sorted(uniq)]
        if len(ordered) == 1 and _constant(ordered[0]):
            node.replace(exp.EQ(this=node.this, expression=ordered[0]))
        else:
            node.set("expressions", ordered)


def _expand_between(tree: Any, read: str | None, sens: "_Sensitivity | None" = None) -> None:
    """x BETWEEN a AND b 就是 x >= a AND x <= b（SQLite 文档原话，PostgreSQL、MySQL 也是）。x 里有子查询、不确定的
    函数的不改（BETWEEN 只算一次 x）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Between)):
        x = node.this
        if node.args.get("symmetric") or x is None or x.find(exp.Query, exp.Subquery) is not None or _volatile(x):
            continue
        owner = node.find_ancestor(exp.Select)
        if sens is not None and owner is not None and sens.of(owner):
            # 结果看行出来先后的一段不改：一个 BETWEEN 和两个比较估算出的行数可能不同（MySQL 的 condition filtering），
            # 换一种扫描的先后，GROUP_CONCAT 拼出来的先后就跟着变
            continue
        if read == "mysql":
            # MySQL 的 BETWEEN 按三个参数一起定比较的类型，两个比较各定各的：上下界一个数字一个字符串（name BETWEEN 2
            # AND '3'，name = '10' 时 BETWEEN 是 0、两个比较是 1）、或者上下界不是常量（类型不知道）时不改
            low, high = _literal_value(node.args.get("low")), _literal_value(node.args.get("high"))
            if low is None or high is None or low[0] != high[0]:
                continue
        pair = exp.And(this=exp.GTE(this=x.copy(), expression=node.args["low"].copy()),
                       expression=exp.LTE(this=x.copy(), expression=node.args["high"].copy()))
        node.replace(exp.Paren(this=pair))


def _predicate(node: Any) -> bool:
    """取值只会是真、假、空值的条件（比较、IS、IN、LIKE、EXISTS、BETWEEN，和它们的 AND / OR / NOT）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, (exp.Predicate, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In,
                         exp.Like, exp.ILike, exp.Exists, exp.Between)):
        return True
    if isinstance(node, (exp.And, exp.Or)):
        return _predicate(node.left) and _predicate(node.right)
    if isinstance(node, exp.Not):
        return _predicate(node.this)
    return False


def _fold_not(tree: Any, sens: "_Sensitivity | None" = None) -> None:
    """NOT 往里推（三值逻辑下都成立，两边有空值时结果一样）：NOT 套比较写成反过来的比较（NOT a = b 就是 a <> b，NOT a > b
    就是 a <= b）；NOT NOT p 写成 p（p 是条件时；NOT NOT 5 是 1，不是 5）；NOT (p AND q) 写成 NOT p OR NOT q、NOT (p OR q)
    写成 NOT p AND NOT q（德摩根律，p、q 是条件时；空值 AND / OR 的真值表左右对称，三值逻辑下照样成立）。ANY / ALL 不动；
    结果看行出来先后的一段只做前两样（换了条件的形状可能换一种扫描的先后）。"""
    from sqlglot import exp

    def sensitive(node: Any) -> bool:
        owner = node.find_ancestor(exp.Select)
        return sens is not None and owner is not None and sens.of(owner)

    changed = True
    while changed:
        changed = False
        for node in list(tree.find_all(exp.Not)):
            if node.parent is None and node is not tree:
                continue
            inner = _unparen(node.this)
            name = type(inner).__name__
            if name in _NEGATE and not _quantified(inner.this) and not _quantified(inner.expression):
                node.replace(getattr(exp, _NEGATE[name])(this=inner.this, expression=inner.expression))
                changed = True
            elif isinstance(inner, exp.Not) and _predicate(inner.this):
                node.replace(inner.this)
                changed = True
            elif isinstance(inner, (exp.And, exp.Or)) and _predicate(inner.left) and _predicate(inner.right) and \
                    not sensitive(node):
                left = exp.Not(this=exp.Paren(this=inner.left.copy()))
                right = exp.Not(this=exp.Paren(this=inner.right.copy()))
                flipped = exp.Or(this=left, expression=right) if isinstance(inner, exp.And) else \
                    exp.And(this=left, expression=right)
                node.replace(exp.Paren(this=flipped))
                changed = True
            if changed:
                break


#: 括号里是这些（运算符的一部分）时，外面是比较运算的话括号可以去掉：算术比比较结合得紧
_ARITHMETIC = ("Add", "Sub", "Mul", "Div", "Mod")
_COMPARISON = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE")


def _strip_parens(tree: Any) -> None:
    """不影响结合的括号去掉（比较时本来就不看括号，见 _fingerprint；这里只为生成的写法好读）：括号外面不是运算符
    （函数参数、别名、SELECT 列表、ORDER BY、GROUP BY、WHERE 整个条件、IN 列表的一项……）、括号里是一列一个常量一次
    函数调用，或者括号里是算术、外面是比较。"""
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


def _order_by_aliases(tree: Any) -> None:
    """最外层 ORDER BY 引用 SELECT 的别名的，换成那个表达式：别名接下来要去掉。不换的：别名指的是数字常量（ORDER BY 1
    是按第一列排）；别名指的是一个不带表名、又和某个别名同名的列（ORDER BY 里单独一个名字先认别名：SELECT user_id AS
    amount, amount AS user_id … ORDER BY amount 换成 ORDER BY user_id 就成了按第二列排）。"""
    from sqlglot import exp

    if not isinstance(tree, exp.Select) or tree.args.get("order") is None:
        return
    aliases = {p.alias: p for p in tree.expressions if p.alias}
    names = [p.alias_or_name for p in tree.expressions]
    for o in tree.args["order"].expressions:
        key = o.this
        if not (isinstance(key, exp.Column) and not key.table and key.name in aliases) or names.count(key.name) > 1:
            continue
        target = aliases[key.name].unalias()
        if target.is_number or (isinstance(target, exp.Column) and not target.table and target.name in aliases):
            continue
        o.set("this", target.copy())


def _drop_aliases(query: Any) -> None:
    from sqlglot import exp

    if not isinstance(query, exp.Select):
        return
    used = _alias_refs(query)
    names = [p.alias_or_name for p in query.expressions]
    query.set("expressions", [p.unalias() if isinstance(p, exp.Alias) and p.alias not in used and
                              names.count(p.alias) == 1 else p for p in query.expressions])


def _drop_output_aliases(tree: Any) -> None:
    """最外层（UNION 不按名字排序时各支都是）和条件里的子查询选出来的列名不影响任何取值，去掉；本段里还按名字引用
    的留着。"""
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


def _alias_refs(select: Any) -> set[str]:
    """这一段里可能按名字引用本段列别名的不带表名的名字：WHERE、ON、GROUP BY、HAVING、ORDER BY、窗口里的，以及套在
    本段里的子查询里的全部（SQLite 的 WHERE、ON、相关子查询都认外层的列别名：EXISTS (… WHERE r.amount = b) 里的 b
    是外层 SELECT amount AS b 的 b，在真实的 SQLite 上试过）。SELECT 列表自己的表达式不算（库里不认同一个列表里前面
    的别名），FROM 里的来源不算。改名、去掉列别名之前（列名表写进定义、EXISTS 写成 SELECT 1、去掉没用的列名、CTE 的
    列改叫 c1…）都先看这里，有同名的就不动：不然这些名字就指到别处去了。"""
    from sqlglot import exp

    out: set[str] = set()
    stack: list[tuple[Any, bool]] = []
    for key, value in select.args.items():
        if key in ("with_", "from_"):
            continue
        for v in (value if isinstance(value, list) else [value]):
            if isinstance(v, exp.Expr):
                stack.append((v, key == "expressions"))
    while stack:
        node, projection = stack.pop()
        if isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append((node.args["on"], False))
            continue
        if isinstance(node, (exp.Query, exp.Subquery)):
            out |= {c.name for c in node.find_all(exp.Column) if not c.table}
            continue
        if isinstance(node, exp.Window):
            out |= {c.name for c in node.find_all(exp.Column) if not c.table}
            continue
        if isinstance(node, exp.Column) and not node.table and not projection:
            out.add(node.name)
        stack += [(child, projection) for child in node.iter_expressions()]
    return out


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


def _starry(select: Any) -> bool:
    from sqlglot import exp

    return any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star) for p in select.expressions)


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

    columns = {name.lower(): {c.lower() for c in cols} for name, cols in (schema or {}).items()}
    for select in tree.find_all(exp.Select):
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
        # 套在这一段里的子查询用了和这一段列别名同名、不带表名的名字：SQLite 的相关子查询认外层的列别名（EXISTS (…
        # WHERE r.amount = b) 里的 b 是外层 SELECT id AS b 的 id），qualify 却把它归给子查询里的表（r.b），两条把 b 指向
        # 不同列的语句会认成一样
        nested = {c.name.lower() for sub in _nested_queries(select) for c in sub.find_all(exp.Column) if not c.table}
        if nested & aliases:
            return True
    return False


# ---- 起规范的名字

@dataclass
class _Hint:
    """按另一边起名（差异说明用）：定义一样的 CTE 叫另一边的名字；names 是这一边原来的 CTE 名 → 另一边的名字；taken
    是另一边用过的名字，配不上的另起名字时避开。"""

    bodies: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    taken: set[str] = field(default_factory=set)


class _Namer:
    """给一条语句起规范的名字（见 _finish 第 3 步）。

    先按原来的别名认清每一列属于哪个来源（见 _Resolver）；之后改来源的别名时把这些列一起改掉，不再按名字找，换名不会
    把列认错。别名带层数：内外层的别名不会同名，相关子查询里引用外层的列不会被里层遮住；同一层的兄弟子查询各自看不见
    对方，可以重名。"""

    def __init__(self, tree: Any, read: str | None, qualified: bool, sens: _Sensitivity,
                 hint: _Hint | None = None) -> None:
        from sqlglot import exp

        self.tree = tree
        self.read = read
        self.qualified = qualified
        self.sens = sens
        self.hint = hint
        resolver = _Resolver(tree)
        self.refs = resolver.refs
        self.cte_of = resolver.cte_of
        self.base_names = {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
        self.budget = _ORDERING_BUDGET
        self.starred = self._starred_sources(tree)

    def _starred_sources(self, tree: Any) -> set[int]:
        """被 SELECT * 读着的来源（* 没展开）：它选出来的列名、先后会原样传到外面，不能改名、重排。"""
        from sqlglot import exp

        out: set[int] = set()
        for select in tree.find_all(exp.Select):
            for p in select.expressions:
                if isinstance(p, exp.Star):
                    out |= {id(i) for i in _items_of(select)}
                elif isinstance(p, exp.Column) and p.is_star:
                    out |= {id(i) for i in _items_of(select) if i.alias_or_name == p.table}
        return out

    def _text(self, node: Any) -> tuple[str, str]:
        return _key(node, self.read)

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
                refs = [t for t in self.tree.find_all(exp.Table) if self.cte_of.get(id(t)) is cte]
                if self._rename_outputs(cte.this, refs):
                    body = self._text(cte.this)
                texts.append((body, original, cte))
            texts.sort(key=lambda x: x[0])
            for body, original, cte in texts:
                pending.remove(cte)
                if body in by_text:
                    cte_names[id(cte)] = by_text[body]
                    continue
                name = (hint.names.get(original) or hint.bodies.get(body[0])) if hint else None
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

    def _rename_outputs(self, body: Any, holders: list[Any]) -> bool:
        """（认列之后）CTE、FROM 里的子查询选出来的列改叫 c1、c2…，引用它们的列跟着改：外面按名字引用，名字是各自起的
        （total 还是 sum_amt）。没有被 SELECT * 原样读着（* 没展开时列名、先后会传到外面）的，先按定义排序：外面按名字
        引用，先后不影响。UNION、选了 *、列名重复、本段里还按名字引用自己的列、GROUP BY / ORDER BY 按位置引用的不改。"""
        from sqlglot import exp

        if not self.qualified or not isinstance(body, exp.Select) or any(id(h) in self.starred for h in holders):
            return False
        projections = body.expressions
        names = [p.alias_or_name for p in projections]
        if not projections or any(not n or _is_star(p) for n, p in zip(names, projections)) or \
                len(set(names)) != len(names):
            return False
        # 本段（含相关子查询）按名字引用了自己的列别名、或者正好用了要起的名字 c1…：改名之后就指到别处了。没写别名
        # 的光一列（SELECT user_id … WHERE user_id > 1）改不改名，WHERE 里的 user_id 都还是那一列
        aliases = {p.alias for p in projections if isinstance(p, exp.Alias) and p.alias}
        if _alias_refs(body) & (aliases | {f"c{i}" for i in range(1, len(projections) + 1)}):
            return False
        for clause in ("group", "order"):
            node = body.args.get(clause)
            keys = [k.this if isinstance(k, exp.Ordered) else k for k in (node.expressions if node is not None else [])]
            if any(k.is_number for k in keys):
                return False
        ordered = sorted(projections, key=lambda p: self._text(p.unalias()))
        mapping = {p.alias_or_name: f"c{i}" for i, p in enumerate(ordered, 1)}
        body.set("expressions", [exp.alias_(p.unalias().copy(), mapping[p.alias_or_name], copy=False)
                                 for p in ordered])
        for holder in holders:
            for col in self.refs.get(id(holder), []):
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
        """这一处合并的各支调换先后，结果（行、值）一样不一样。只认整条语句最外层、不排序不取行的 UNION ALL：

        - UNION（去重）、INTERSECT 留先出现的那个值，相等不等于一样（SQLite 里 REAL 列的 2.0 和 INTEGER 列的 2、MySQL
          默认不分大小写的 'SH' 和 'sh'）；EXCEPT 本来就分左右。
        - 带 LIMIT / OFFSET 的取的是先出来的几行；ORDER BY 按名字排的看第一支的列名；SQLite 给合并的结果排序时用最左
          一支那列声明的排序规则。PostgreSQL、MySQL 按位置排序（ORDER BY 1）的可以排。
        - 套在 CTE、子查询里的：SQLite 从子查询读合并的结果时按第一支的类型亲和、排序规则转换别的支，不排；别的方言
          只在各支选出来的列名、先后都一样（外面按列名引用，列名取第一支的）、外面不看行出来的先后（不去重、不取行，
          见 _Sensitivity）时排。"""
        from sqlglot import exp

        if not isinstance(query, exp.Union) or query.args.get("distinct"):
            return False
        if any(query.args.get(k) is not None for k in ("limit", "offset", "fetch")):
            return False
        sqlite = self.read in (None, "sqlite")
        order = query.args.get("order")
        if order is not None and (sqlite or any(not (isinstance(o.this, exp.Literal) and o.this.is_int)
                                                for o in order.expressions)):
            return False
        top = query
        while isinstance(top.parent, exp.Subquery):
            top = top.parent
        if top is self.tree:
            return True
        if sqlite or order is not None:
            return False
        leaves = _leaves(query)
        if any(not isinstance(leaf, exp.Select) or self.sens.of(leaf) or _starry(leaf) for leaf in leaves):
            return False
        names = [[p.alias_or_name for p in leaf.expressions] for leaf in leaves]
        return all(n == names[0] for n in names) and all(names[0])

    def _signature(self, item: Any, cte_names: dict[int, str], subs: dict[int, tuple[str, str]]) -> tuple[str, str]:
        """来源的 (排序用的标识, 起别名用的名字)：实表是表名，CTE 是它的规范名，FROM 里的子查询是规范化后的写法。"""
        from sqlglot import exp

        if isinstance(item, exp.Table):
            cte = self.cte_of.get(id(item))
            if cte is not None:
                name = cte_names.get(id(cte), cte.alias_or_name)
                return f"cte:{name}", name
            return "table:" + ".".join(p for p in (item.catalog, item.db, item.name) if p), item.name
        return f"sub:{subs[id(item)][0]}", "sub"

    def _canon_select(self, select: Any, depth: int, cte_names: dict[int, str]) -> tuple[str, str]:
        from sqlglot import exp

        items = _items_of(select)
        subs: dict[int, tuple[str, str]] = {}
        for item in items:
            if isinstance(item, exp.Subquery):
                subs[id(item)] = self.canon(item.this, depth + 1, cte_names)
                if self._rename_outputs(item.this, [item]):
                    subs[id(item)] = self._text(item.this)
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
        # 后面有 RIGHT / FULL JOIN 时开头那组来源不调换：PostgreSQL、MySQL 里 a, b RIGHT JOIN c 是 a 乘上 (b RIGHT JOIN c)，
        # 换成 b, a 就成了 b 乘上 (a RIGHT JOIN c)
        movable = not _bare_star(select) and not self.sens.of(select) and \
            not any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins)
        free_lefts = movable and bool(rest) and self._independent_lefts(prefix, rest)

        def groups(xs: list[Any], key: Any) -> list[list[Any]]:
            xs = sorted(xs, key=key)
            return [list(g) for _, g in itertools.groupby(xs, key=key)]

        prefix_groups = groups(prefix, lambda s: keys[id(s)][0]) if movable else [[s] for s in prefix]
        rest_groups = groups(rest, lambda j: keys[id(j.this)][0]) if free_lefts else [[j] for j in rest]
        total = 1
        for g in prefix_groups + rest_groups:
            total *= math.factorial(len(g))
            if total > _MAX_ORDERINGS:
                break
        if total > _MAX_ORDERINGS or total > self.budget:
            # 排列太多（十几个同名来源）：先算个数再展开，超了就只按排好的先后
            options = [[g] for g in prefix_groups + rest_groups]
            total = 1
        else:
            options = [[list(p) for p in itertools.permutations(g)] if len(g) > 1 else [g]
                       for g in prefix_groups + rest_groups]
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
        _sort_own(select, self.read, self.sens.of(select))
        return self._text(select)


def _fixed(node: Any) -> bool:
    """不涉及任何列、子查询的表达式（常量、常量算出来的日期）：排比较、加乘的两边时放在右边，好读。"""
    from sqlglot import exp

    return node.find(exp.Column, exp.Select, exp.Subquery) is None


def _sort_own(select: Any, read: str | None, sensitive: bool = False) -> None:
    """这一段自己的表达式里交换律成立的地方排好（里层先排）：AND、OR（各项都是条件时去掉一模一样的项：x AND x 在
    SELECT 列表里不是 x）、比较（不涉及列的一边在右，两边都涉及列时排好、大于小于跟着换方向；两边都写了 COLLATE 的
    不换，SQLite 用左边那个）、两项的加和乘（只换这一个节点的两边，不跨项挪动：浮点数加法不满足结合律）、IN 列表、
    GROUP BY（结果看行出来的先后时不排：SQLite 按分组键排好了出行）。"""
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
            if isinstance(_unparen_parent(node), type(node)):
                continue
            parts = _flat(node, type(node))
            if all(_predicate(p) for p in parts) and not sensitive:
                # 一模一样的条件去掉一个。结果看行出来先后的一段不去：MySQL 按条件估算行数，同一个条件写两遍就换了一种
                # 关联的先后，GROUP_CONCAT 拼出来的先后跟着变（第 3 轮在真实的 MySQL 8.0 上压测时看到的）
                uniq: dict[tuple[str, str], Any] = {}
                for part in parts:
                    uniq.setdefault(key(part), part)
                ordered = [uniq[k] for k in sorted(uniq)]
            else:
                ordered = sorted(parts, key=key)
            new = ordered[0] if len(ordered) == 1 else (
                exp.and_(*ordered, copy=False) if isinstance(node, exp.And) else exp.or_(*ordered, copy=False))
            node.replace(new)
        elif type(node).__name__ in _FLIP:
            left, right = node.this, node.expression
            if _quantified(left) or _quantified(right):
                continue
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
            # 一模一样的分组键写两遍和写一遍分出的组一样；结果看行出来先后的一段不动（分组键变了可能换一种扫描的先后）
            if node.expressions and not sensitive:
                uniq_keys: dict[tuple[str, str], Any] = {}
                for k in node.expressions:
                    uniq_keys.setdefault(key(k), k)
                node.set("expressions", sorted(uniq_keys.values(), key=key))
        elif isinstance(node, exp.In) and node.expressions:
            uniq = {key(i): i for i in node.expressions}
            node.set("expressions", [uniq[k] for k in sorted(uniq)])


def _flat(node: Any, cls: type) -> list[Any]:
    """AND / OR 连着的各项（穿过括号：BETWEEN 改写出来的 (x >= a AND x <= b) 也拆开）。"""
    out: list[Any] = []
    stack = [node]
    while stack:
        n = stack.pop()
        inner = _unparen(n)
        if isinstance(inner, cls):
            stack += [inner.expression, inner.this]
        else:
            out.append(n)
    return out


def _unparen_parent(node: Any) -> Any:
    from sqlglot import exp

    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return parent


# ==========================================================================
# 差异说明
# ==========================================================================


@dataclass(frozen=True)
class FacetDiff:
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同）/ filter（筛选条件）/ aggregate（聚合函数、
    去重计数）/ expression（选出来的列）/ join（关联的表、关联类型、关联条件、外连接的主表）/ tables（查的是哪张表）/
    set_op（几支查询怎么合并）/ group_by（分组键、去重）/ having / limit（取哪几行，含取行前的排序）/ order（只是最终
    结果的排序不同，不影响取到哪些行）/ parse（解析不了）/ other（兜底：未能归纳成具体差异，见 explain_sql_diff）。a、b
    是两边各自的写法（给人看，没有就是 None），text 是一句中文说明。detail 只在 time 上有：两边的时间列
    [{table, column}]（各是各的表），调用方据此对照数据目录（业务日期）；在 filter 上有 {"columns": [{table, column}]}：
    条件只涉及的实表列，对照有效行过滤用。"""

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
#: 只取日期的字符串（2026-09-01）：PostgreSQL、MySQL 里拿它和时间列比，按字符串比的先后和按日期比的一样
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPARE_OPS = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE")


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


# ---- 同一个对象上的条件：留下的取值一样不一样

def _literal_value(node: Any) -> tuple[str, Any] | None:
    """常量的值：("n", Decimal) 或 ("s", 字符串)；不是数字、字符串常量返回 None。"""
    from decimal import Decimal, InvalidOperation

    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Neg):
        inner = _literal_value(node.this)
        return ("n", -inner[1]) if inner is not None and inner[0] == "n" else None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "s", str(node.this)
        try:
            return "n", Decimal(str(node.this))
        except (InvalidOperation, ValueError):
            return None
    return None


class _Range:
    """一个对象（一列、一个聚合、一个函数）上只和常量比的条件（=、<>、<、<=、>、>=、IN 常量列表、IS [NOT] NULL、
    BETWEEN，用 AND / OR / NOT 连起来），在对象的各段取值上成立不成立：用到的常量把取值切成一段段（小于最小的、
    每个常量本身、相邻两个之间、大于最大的），每段里条件的真假都一样，各取一个代表算一遍，再算对象是空值时。两组
    条件在每一段、空值上都同真同不真，留下的行就一样。

    字符串的大小先后：SQLite 按默认的 BINARY 排序规则；PostgreSQL（语言排序规则下 'a' < 'B'）、MySQL（默认不分
    大小写）只认等不等，除非常量都是只取日期的字符串（2026-09-01，按字符串比和按日期比先后一样）。MySQL 的字符串
    等不等不分大小写。COUNT 的取值是整数（分组时至少是 1）。"""

    def __init__(self, read: str | None) -> None:
        self.read = read

    def subject(self, cond: Any) -> tuple[str, Any, list[tuple[str, Any]], bool] | None:
        """cond 只是同一个对象和常量比时，返回 (对象的指纹, 对象, 常量, 有没有比大小)；不然 None。"""
        from sqlglot import exp

        subjects: dict[str, Any] = {}
        literals: list[tuple[str, Any]] = []
        ranged = [False]

        def side(subj: Any, lits: list[Any]) -> bool:
            if subj is None or _literal_value(subj) is not None or isinstance(_unparen(subj), exp.Null) or \
                    subj.find(exp.Query, exp.Subquery) is not None or _volatile(subj):
                return False
            subjects[_fingerprint(subj)] = subj
            literals.extend(lits)
            return True

        def walk(n: Any) -> bool:
            n = _unparen(n)
            if isinstance(n, (exp.And, exp.Or)):
                return walk(n.left) and walk(n.right)
            if isinstance(n, exp.Not):
                return walk(n.this)
            name = type(n).__name__
            if name in _COMPARE_OPS:
                lv, rv = _literal_value(n.expression), _literal_value(n.this)
                if name not in ("EQ", "NEQ"):
                    ranged[0] = True
                if lv is not None and rv is None:
                    return side(n.this, [lv])
                if rv is not None and lv is None:
                    return side(n.expression, [rv])
                return False
            if isinstance(n, exp.In):
                if n.args.get("query") or n.args.get("unnest") or n.args.get("field") or not n.expressions:
                    return False
                lits = []
                for item in n.expressions:
                    if isinstance(_unparen(item), exp.Null):
                        continue
                    value = _literal_value(item)
                    if value is None:
                        return False
                    lits.append(value)
                return side(n.this, lits)
            if isinstance(n, exp.Is):
                return isinstance(_unparen(n.expression), exp.Null) and side(n.this, [])
            if n.args.get("negate"):
                return False
            if isinstance(n, exp.Between):
                low, high = _literal_value(n.args.get("low")), _literal_value(n.args.get("high"))
                ranged[0] = True
                return not n.args.get("symmetric") and low is not None and high is not None and \
                    side(n.this, [low, high])
            return False

        if not walk(cond) or len(subjects) != 1:
            return None
        kinds = {k for k, _ in literals}
        if len(kinds) > 1:
            return None
        if ranged[0] and kinds == {"s"} and self.read != "sqlite" and \
                not all(_DATE_ONLY.match(v) for _, v in literals):
            return None
        fp, subj = next(iter(subjects.items()))
        return fp, subj, literals, ranged[0]

    def _norm(self, value: tuple[str, Any]) -> Any:
        kind, v = value
        if kind == "s" and self.read == "mysql":
            return v.casefold()
        return v

    def _pieces(self, subject: Any, literals: list[tuple[str, Any]], ranged: bool, grouped: bool) -> list[Any]:
        """代表各段取值的值（不含空值）。"""
        from decimal import Decimal

        from sqlglot import exp

        values = sorted({self._norm(v) for v in literals})
        kind = literals[0][0] if literals else "n"
        if not values:
            return [Decimal(0)] if kind == "n" else ["x"]
        if not ranged:
            other = values[-1] + 1 if kind == "n" else values[-1] + "\x00~"
            return values + [other]
        out: list[Any] = []
        if kind == "n":
            integral = isinstance(_unparen(subject), exp.Count)
            low = Decimal(1) if integral and grouped and isinstance(_unparen(subject).this, exp.Star) else (
                Decimal(0) if integral else None)
            candidates = [values[0] - 1] + [x for i, v in enumerate(values)
                                             for x in ([v] + ([(v + values[i + 1]) / 2] if i + 1 < len(values) else []))]
            candidates.append(values[-1] + 1)
            if integral:
                ints: set[Decimal] = set()
                for i, v in enumerate(values):
                    ints |= {v.to_integral_value(rounding="ROUND_FLOOR") + d for d in (-1, 0, 1)}
                    ints |= {v.to_integral_value(rounding="ROUND_CEILING") + d for d in (-1, 0, 1)}
                candidates = sorted(ints)
            for c in candidates:
                if low is not None and c < low:
                    continue
                out.append(c)
            return out or [low or Decimal(0)]
        if values[0] != "":
            out.append("")
        for i, v in enumerate(values):
            out.append(v)
            between = v + "\x00"
            if i + 1 >= len(values) or between < values[i + 1]:
                out.append(between)
        return out

    def _truth(self, n: Any, v: Any) -> str:
        """n 在对象取 v（None 是空值）时是真（T）、假（F）还是空值（N）。"""
        from sqlglot import exp

        n = _unparen(n)
        if isinstance(n, exp.And):
            a, b = self._truth(n.left, v), self._truth(n.right, v)
            return "F" if "F" in (a, b) else ("N" if "N" in (a, b) else "T")
        if isinstance(n, exp.Or):
            a, b = self._truth(n.left, v), self._truth(n.right, v)
            return "T" if "T" in (a, b) else ("N" if "N" in (a, b) else "F")
        if isinstance(n, exp.Not):
            t = self._truth(n.this, v)
            return {"T": "F", "F": "T"}.get(t, "N")
        if isinstance(n, exp.Is):
            hit = v is None
            return "T" if hit != bool(n.args.get("negate")) else "F"
        if v is None:
            return "N"
        name = type(n).__name__
        if name in _COMPARE_OPS:
            right = _literal_value(n.expression)
            if right is not None:
                lit, op = self._norm(right), name
            else:
                lit, op = self._norm(_literal_value(n.this)), _FLIP[name]
            if type(v) is not type(lit):
                return "N"
            result = {"EQ": v == lit, "NEQ": v != lit, "GT": v > lit, "GTE": v >= lit, "LT": v < lit,
                      "LTE": v <= lit}[op]
            return "T" if result else "F"
        if isinstance(n, exp.In):
            hit = any(not isinstance(_unparen(i), exp.Null) and self._norm(_literal_value(i)) == v
                      for i in n.expressions)
            if hit:
                return "T"
            return "N" if any(isinstance(_unparen(i), exp.Null) for i in n.expressions) else "F"
        if isinstance(n, exp.Between):
            low, high = self._norm(_literal_value(n.args["low"])), self._norm(_literal_value(n.args["high"]))
            return "T" if low <= v <= high else "F"
        return "N"

    def compare(self, conds_a: list[Any], conds_b: list[Any], grouped: bool = False) -> bool | None:
        """两组条件（各自 AND 连，可以一边为空 = 不筛）留下的取值是不是一样：一样 True，不一样 False，认不出 None。"""
        parsed = [self.subject(c) for c in conds_a + conds_b]
        if any(p is None for p in parsed) or len({p[0] for p in parsed}) != 1:
            return None
        subject = parsed[0][1]
        literals = [lit for p in parsed for lit in p[2]]
        if len({k for k, _ in literals}) > 1:
            return None
        ranged = any(p[3] for p in parsed)
        from sqlglot import exp

        values = self._pieces(subject, literals, ranged, grouped)
        if not isinstance(_unparen(subject), exp.Count):
            values.append(None)
        for v in values:
            ta = all(self._truth(c, v) == "T" for c in conds_a)
            tb = all(self._truth(c, v) == "T" for c in conds_b)
            if ta != tb:
                return False
        return True


# ---- 一边用到了哪些列（判断「另一边没有」能不能说）

class _Usage:
    """一条语句里每一列（按定义找到实表列，认不出来源的只记列名）用在了哪儿：

    - cond：WHERE、ON、HAVING 里，条件里的子查询（IN、EXISTS、标量）选出来的列；
    - case：CASE / IF / FILTER 里（SUM(CASE WHEN status = 'paid' THEN amount END) 等于先按 status 筛再求和）；
    - agg：聚合函数、窗口函数的参数（COUNT(x)、SUM(x) 跳过空值，等于先筛掉 x 为空的行）。

    光选出来、分组、排序的列不记。说「另一边没有这个条件」「另一边没有按时间筛选」之前，先看另一边在这一段能影响到的
    地方（整条语句，除了合并里别的支）用没用到这些列。"""

    def __init__(self, tree: Any) -> None:
        from sqlglot import exp

        self.nodes: dict[tuple[str | None, str], dict[int, str]] = {}
        self.temporal = False
        try:
            self.resolver: _Resolver | None = _Resolver(tree)
        except _Unsupported:
            self.resolver = None
        for col in tree.find_all(exp.Column):
            if col.is_star or not col.name:
                continue
            kind = self._kind(col)
            if kind is None:
                continue
            bases = self._bases(col, 0)
            if isinstance(col.this, exp.Identifier) and col.this.meta.get(_UNSURE):
                bases.append((None, col.name))
            for table, column in bases:
                self.nodes.setdefault((table, column), {})[id(col)] = kind
                if kind != "agg" and _temporal_name(column):
                    self.temporal = True
        for node in tree.find_all(exp.Where, exp.Having, exp.Join):
            target = node.args.get("on") if isinstance(node, exp.Join) else node.this
            if target is not None and _datey(target):
                self.temporal = True

    @staticmethod
    def _owner(node: Any) -> tuple[Any, str | None, bool, bool]:
        """node 属于哪一段 SELECT、在它的哪个子句里、路上有没有 CASE / IF / FILTER、有没有聚合 / 窗口函数。"""
        from sqlglot import exp

        case = agg = False
        child, parent = node, node.parent
        while parent is not None:
            if isinstance(parent, (exp.Case, exp.If, exp.Filter)):
                case = True
            if isinstance(parent, (exp.AggFunc, exp.Window)):
                agg = True
            if isinstance(parent, exp.Select):
                return parent, child.arg_key, case, agg
            child, parent = parent, parent.parent
        return None, None, case, agg

    def _kind(self, col: Any) -> str | None:
        select, key, case, agg = self._owner(col)
        if select is None:
            return None
        if key == "expressions":
            if _scalar_context(select) or _condition_query(select):
                return "cond"
            return "case" if case else ("agg" if agg else None)
        if key in ("group", "order", "distinct"):
            return "case" if case else None
        return "cond"

    def _bases(self, col: Any, depth: int) -> list[tuple[str | None, str]]:
        from sqlglot import exp

        if depth > 16 or self.resolver is None:
            return [(None, col.name)]
        item = self.resolver.source_of.get(id(col))
        if item is None:
            select = self._owner(col)[0]
            items = _items_of(select) if select is not None else []
            if col.table or len(items) != 1:
                return [(None, col.name)]
            item = items[0]
        return self._from_item(item, col.name, depth)

    def _from_item(self, item: Any, name: str, depth: int) -> list[tuple[str | None, str]]:
        from sqlglot import exp

        body = self.resolver.body(item) if self.resolver is not None else None
        if body is None:
            return [(item.name, name)] if isinstance(item, exp.Table) else [(None, name)]
        leaves = _leaves(body)
        if not leaves or not isinstance(leaves[0], exp.Select) or depth > 16:
            return [(None, name)]
        names = [p.alias_or_name for p in leaves[0].expressions]
        if name not in names:
            sources = _items_of(leaves[0])
            if len(leaves) == 1 and any(isinstance(p, exp.Star) for p in leaves[0].expressions) and len(sources) == 1:
                return self._from_item(sources[0], name, depth + 1)
            return [(None, name)]
        index = names.index(name)
        out: list[tuple[str | None, str]] = []
        for leaf in leaves:
            if not isinstance(leaf, exp.Select) or index >= len(leaf.expressions):
                return [(None, name)]
            expr = leaf.expressions[index].unalias()
            for c in ([expr] if isinstance(expr, exp.Column) else list(expr.find_all(exp.Column))):
                if not c.is_star:
                    out += self._bases(c, depth + 1)
        return out or [(None, name)]

    def touches(self, refs: Iterable[tuple[str, str]], skip: Iterable[Any] = (),
                kinds: tuple[str, ...] = ("cond", "case"), scope: Any = None) -> bool:
        """这些实表列用在了 kinds 里（认不出来源的同名列也算）；skip 里的条件中的那几处不算，scope 所在合并的别的支
        （不碰 scope 的行）不算。"""
        from sqlglot import exp

        skipped = {id(c) for cond in skip for c in cond.find_all(exp.Column)}
        node = scope
        while node is not None and node.parent is not None:
            parent = node.parent
            if isinstance(parent, exp.SetOperation):
                for branch in (parent.this, parent.expression):
                    if branch is not node:
                        skipped |= {id(c) for c in branch.find_all(exp.Column)}
            node = parent
        for table, column in refs:
            for key in ((table, column), (None, column)):
                if any(i not in skipped and k in kinds for i, k in self.nodes.get(key, {}).items()):
                    return True
        return False


def _kinds_for(conds: list[Any]) -> tuple[str, ...]:
    """说某个条件只在一边有之前，要看另一边哪些地方用到了这些列：条件里有判空（IS NULL / IS NOT NULL）的连聚合的
    参数也看（COUNT(x) 本来就跳过 x 为空的行）。"""
    from sqlglot import exp

    if any(isinstance(n, exp.Is) for c in conds for n in c.walk()):
        return "cond", "case", "agg"
    return "cond", "case"


def _condition_query(select: Any) -> bool:
    """这段查询是条件里的子查询（IN、EXISTS、ANY / ALL）：选出来的列参与比较。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.SetOperation)):
        node = node.parent
    parent = node.parent
    return isinstance(parent, (exp.Exists, exp.Any, exp.All)) or (isinstance(parent, exp.In) and
                                                                    node.arg_key == "query")


# ---- 一段 SELECT

@dataclass
class _Rel:
    """一段里的一个来源：alias 是规范别名，name 是实表名（CTE 是规范名，子查询是 sub），kind 是 table / cte / derived，
    shown 是给人看的名字（实表是表名，CTE、子查询是原来起的名字），rejected 表示写的是 LEFT JOIN、被 WHERE 变成了
    内连接。"""

    alias: str
    name: str
    kind: str
    shown: str
    node: Any
    rejected: str | None = None

    @property
    def table(self) -> bool:
        return self.kind == "table"


def _rel_of(item: Any, ctes: Mapping[str, str]) -> _Rel:
    from sqlglot import exp

    alias = item.alias_or_name
    if isinstance(item, exp.Subquery):
        return _Rel(alias, "sub", "derived", item.this.meta.get(_ORIGINAL) or alias, item, item.meta.get(_REJECTED))
    if item.name in ctes:
        return _Rel(alias, item.name, "cte", ctes[item.name], item, item.meta.get(_REJECTED))
    return _Rel(alias, item.name, "table", item.name, item, item.meta.get(_REJECTED))


@dataclass
class _Block:
    """一段 SELECT 拆开来比：开头那组逗号连接的来源（rels）、后面的连接（joins：LEFT JOIN、没写成逗号连接的内连接，
    按先后）、WHERE、GROUP BY、HAVING 的各项，选出来的列，排序和取行。"""

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
    distinct_on: bool
    windowed: bool
    ctes: dict[str, str]
    deduped: bool = False

    @classmethod
    def of(cls, select: Any, read: str | None, ctes: Mapping[str, str], root: bool = False) -> "_Block | None":
        from sqlglot import exp

        if not isinstance(select, exp.Select) or (select.args.get("with_") is not None and not root):
            return None
        items = _items_of(select)
        if not items or any(not isinstance(i, (exp.Table, exp.Subquery)) for i in items):
            return None
        joins = select.args.get("joins") or []
        n = 1
        for join in joins:
            if _comma(join):
                n += 1
            else:
                break
        where, group, having = select.args.get("where"), select.args.get("group"), select.args.get("having")
        order, limit, offset = select.args.get("order"), select.args.get("limit"), select.args.get("offset")
        distinct = select.args.get("distinct")

        def number(node: Any) -> str | None:
            return node.expression.sql(dialect=read) if node is not None and node.expression is not None else None

        return cls(select, [_rel_of(i, ctes) for i in items[:n]], joins[n - 1:],
                   _conjuncts(where.this) if where is not None else [],
                   list(group.expressions) if group is not None else [],
                   _conjuncts(having.this) if having is not None else [],
                   list(select.expressions), list(order.expressions) if order is not None else [],
                   number(limit), number(offset), distinct is not None,
                   bool(distinct is not None and distinct.args.get("on")),
                   any(isinstance(n, exp.Window) for n in _block_nodes(select)), dict(ctes),
                   bool(group is not None and group.meta.get(_FROM_DISTINCT)))

    def all_rels(self) -> list[_Rel]:
        return self.rels + [_rel_of(j.this, self.ctes) for j in self.joins]

    def rel(self, alias: str) -> _Rel | None:
        for r in self.all_rels():
            if r.alias == alias:
                return r
        return None

    def names(self) -> list[str]:
        return [r.name for r in self.all_rels()]


class _Explainer:
    """在两边的规范形式上逐段比，只把有把握的不同写成具体的中文句子，同时记下是不是所有不同都说到了。

    有把握的门槛：
    - 两段读的来源对得上（一模一样，或者只差一处说得清的：换了一张表、一边多关联了一张表、同一张表的关联类型或
      关联条件不同、外连接的主表对调）才往下比；换了表的那一边，列照它自己的表说（不借另一边的表名）。
    - 说「X 不一样」之前先确认真的不一样：同一个对象只和常量比的几个条件按取值逐段算一遍（_Range），留下的行一样的
      不说（IN 和 OR、多写一个 IS NOT NULL、把一段时间拆成两段）；关联条件只是列相等的，比相等关系分出的组。认不出
      的写法不说。
    - 说「另一边没有」之前先确认另一边整条语句（含子查询、CTE、相关子查询里引用外层的列）的筛选里都没用到这些列
      （_Usage）；说「没有按时间筛选」之前确认另一边哪儿都没有像时间的列、像日期的常量。
    - 条件里的子查询两边写法除子查询外一模一样（含 NOT、IN 左边的列）才配成一对往里比，说清是哪个子查询。
    - 只是排序不同的说法（order）只在整条语句最外层、没有取行、DISTINCT ON、窗口函数时说；取行前的排序算 limit。
    - LEFT JOIN 对 INNER JOIN 只在整条语句最外层、WHERE 里没用到可空一侧的列、结果不只是可空一侧的列的聚合（那样
      两种写法结果一样）时说。
    说的都是「这一段的这一项两边各写的是什么」。两边给人看的写法一样的话不说（读起来自相矛盾），说过的一对反过来再说
    一遍的也不说。"""

    def __init__(self, read: str | None, labels: tuple[str, str], names: Mapping[str, str] | None,
                 info: _Schema) -> None:
        self.read = read
        self.la, self.lb = labels
        self.label = _labeler(names)
        self.info = info
        self.items: list[FacetDiff] = []
        self.ctes: dict[str, str] = {}
        self.range = _Range(read)
        self.usage_a: _Usage | None = None
        self.usage_b: _Usage | None = None
        self.roots: set[int] = set()
        #: 有一句话没说出口（两边给人看的写法一样，或者和前面的话矛盾）：这一处不同就没说到
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
        self.usage_a, self.usage_b = _Usage(ta), _Usage(tb)
        wb = tb.args.get("with_")
        names_b = {c.alias_or_name: c for c in (wb.expressions if wb is not None else [])}
        shown_a = {n: c.this.meta.get(_ORIGINAL) or n for n, c in names_a.items()}
        shown_b = {n: c.this.meta.get(_ORIGINAL) or n for n, c in names_b.items()}
        covered = set(names_a) == set(names_b)
        self.ctes = {**shown_b, **shown_a}
        self.roots = {id(ta), id(tb)}
        covered &= self.query(ta, tb, ("", ""), "top")
        for name in sorted(set(names_a) & set(names_b)):
            body_a, body_b = names_a[name].this, names_b[name].this
            if self.fp(body_a) != self.fp(body_b):
                where = (_say("在", shown_a[name], "里"), _say("在", shown_b[name], "里"))
                covered &= self.query(body_a, body_b, where, "derived")
        return covered and not self.withheld

    def _renamed(self, form_a: _Form, form_b: _Form, names_a: dict[str, Any]) -> Any:
        """B 起名之前的树按 A 的 CTE 名重新起名：定义一样的叫 A 的名字；两边各剩一个对不上的，B 的那个也叫 A 的。"""
        from sqlglot import exp

        hint = _Hint(bodies={_fingerprint(c.this): name for name, c in names_a.items()}, taken=set(names_a))

        def name(pre: Any) -> Any:
            return _named(pre, self.read, form_b.qualified, _Sensitivity(pre, self.read, self.info), hint)

        first = name(form_b.pre.copy())
        wb = first.args.get("with_")
        left_b = [c for c in (wb.expressions if wb is not None else []) if c.alias_or_name not in names_a]
        used = {c.alias_or_name for c in (wb.expressions if wb is not None else [])}
        left_a = [n for n in names_a if n not in used]
        if len(left_a) == 1 and len(left_b) == 1:
            pre = form_b.pre.copy()
            original = left_b[0].this.meta.get(_ORIGINAL)
            matches = [c.alias_or_name for c in pre.find_all(exp.CTE) if c.this.meta.get(_ORIGINAL) == original]
            if original is not None and len(matches) == 1:
                hint.names[matches[0]] = left_a[0]
                return name(pre)
        return first

    def query(self, qa: Any, qb: Any, where: tuple[str, str], ctx: str) -> bool:
        from sqlglot import exp

        if self.fp(qa) == self.fp(qb):
            return True
        if isinstance(qa, exp.Select) and isinstance(qb, exp.Select):
            return self.select(qa, qb, where, ctx)
        if isinstance(qa, exp.SetOperation) and isinstance(qb, exp.SetOperation):
            return self.setop(qa, qb, where, ctx)
        return False

    # ---- 说法的拼法

    def sides(self, where: tuple[str, str]) -> tuple[str, str]:
        """两边的叫法，带上在哪一段（「A 在 paid 里」）。两边都说在哪：只说一边，「另一边没有」会读成整条语句都没有。"""
        return _say(self.la, where[0]), _say(self.lb, where[1])

    def add(self, aspect: str, a: Any, b: Any, text: str, detail: dict[str, Any] | None = None) -> None:
        """记一句话。两边给人看的写法一样（「A 算的是 1，B 算的是 1」）、和已经说过的一句正好反过来（A 说 X、B 说 Y
        对上 A 说 Y、B 说 X）的不说：读起来自相矛盾，这一处算没说到。"""
        if isinstance(a, str) and isinstance(b, str) and a == b:
            self.withheld = True
            return
        for old in self.items:
            if old.aspect == aspect and old.a == b and old.b == a and (a is not None or b is not None):
                self.items.remove(old)
                self.withheld = True
                return
            if old.text == text:
                return
        self.items.append(FacetDiff(aspect, a, b, text, detail))

    def base_columns(self, node: Any, block: _Block) -> list[tuple[_Rel, Any]] | None:
        """node 只涉及这一段的实表列（同名的表在这一段只出现一次）、不套子查询时，返回各列和它的来源；否则 None。"""
        from sqlglot import exp

        if node.find(exp.Select, exp.Subquery) is not None:
            return None
        names = block.names()
        aliases = {e.alias for e in block.exprs if isinstance(e, exp.Alias) and e.alias}
        out = []
        for col in node.find_all(exp.Column):
            if col.is_star or (not col.table and col.name in aliases):
                return None
            rel = block.rel(col.table) if col.table else (block.rels[0] if len(block.all_rels()) == 1 else None)
            if rel is None or not rel.table or names.count(rel.name) != 1:
                return None
            if isinstance(col.this, exp.Identifier) and col.this.meta.get(_UNSURE):
                columns = self.info.types.get(rel.name.translate(_ASCII_LOWER))
                if columns is None or col.name.translate(_ASCII_LOWER) not in columns:
                    return None
            out.append((rel, col))
        return out

    def refs(self, node: Any, block: _Block) -> set[tuple[str, str]] | None:
        cols = self.base_columns(node, block)
        return None if cols is None else {(r.name, c.name) for r, c in cols}

    def show(self, node: Any, block: _Block, *, full: bool = False) -> str:
        """给人看的写法：只有一个来源时列不写表名，否则写实表名（规范别名换回表名，CTE 换回原来的名字）；选出来的
        列去掉规范化起的别名。full 时一律写表名。"""
        from sqlglot import exp

        copy = node.unalias().copy() if isinstance(node, exp.Alias) else node.copy()
        single = len(block.all_rels()) == 1 and not full
        for col in copy.find_all(exp.Column):
            written = col.meta.get(_WRITTEN_COLUMN)
            if written is not None:
                # 规范化按关联条件换成了同一组里的另一列（见 _substitute_equal_columns）：给人看原文写的那一列
                col.set("this", exp.to_identifier(written[1]))
                col.set("table", None if single else exp.to_identifier(written[0]))
                continue
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

    def named(self, table: str, column: str) -> str:
        """列带中文名（有的话）：「支付时间 paid_at」。"""
        label = self.label((table, column))
        return _say(label, column) if label else column

    def what(self, rel: _Rel) -> str:
        return _say(rel.shown, "表") if rel.table else _say("子查询", rel.shown)

    @staticmethod
    def mapped(node: Any, mapping: Mapping[str, str]) -> Any:
        """B 的一个节点按来源的对应关系（B 的别名 → A 的别名）改名后的副本：只用来和 A 比是不是同一个写法。"""
        from sqlglot import exp

        if not mapping:
            return node
        copy = node.copy()
        for col in copy.find_all(exp.Column):
            if col.table in mapping:
                col.set("table", exp.to_identifier(mapping[col.table]))
        return copy

    def mfp(self, node: Any, mapping: Mapping[str, str]) -> str:
        return self.fp(self.mapped(node, mapping))

    def equal_classes(self, A: _Block, B: _Block, mapping: Mapping[str, str]) -> frozenset[frozenset[str]]:
        """两边 WHERE 里「两列相等」的条件把列分成的组（两边合起来：只用来判断「这两列可能是同一个值」，从而不说出
        「A 按 X 分组、B 按 Y 分组」这类其实可能一样的话；规范化在给了表结构、类型一样时已经把它们换成同一列了）。"""
        return _partition([c for c in A.where if _column_equality(c)] +
                          [self.mapped(c, mapping) for c in B.where if _column_equality(c)])

    def classed(self, nodes: list[Any], classes: frozenset[frozenset[str]], mapping: Mapping[str, str]) -> list[str]:
        """几个表达式里属于同一个相等组的列都换成同一个占位的列之后的写法：用来比「除了换成相等的列，是不是一样」。"""
        from sqlglot import exp

        groups = sorted(sorted(g) for g in classes)
        out = []
        for node in nodes:
            copy = self.mapped(node, mapping).copy()
            for col in list(copy.find_all(exp.Column)):
                index = next((i for i, g in enumerate(groups) if _fingerprint(col) in g), None)
                if index is not None:
                    placeholder = exp.column(f"__equal_{index}")
                    if col is copy:
                        copy = placeholder
                    else:
                        col.replace(placeholder)
            out.append(self.fp(copy))
        return out

    # ---- 一段 SELECT

    def select(self, a: Any, b: Any, where: tuple[str, str], ctx: str) -> bool:
        A = _Block.of(a, self.read, self.ctes, id(a) in self.roots)
        B = _Block.of(b, self.read, self.ctes, id(b) in self.roots)
        if A is None or B is None:
            return False
        matched = self.relations(A, B, where, ctx)
        if matched is None:
            return False
        covered, mapping, skip_a, skip_b = matched
        covered &= self.derived(A, B, mapping, where)
        covered &= self.conditions(A, B, mapping, where, skip_a, skip_b)
        covered &= self.outputs(A, B, mapping, where, ctx)
        covered &= self.having(A, B, mapping, where)
        covered &= self.rows(A, B, mapping, where, ctx)
        return covered

    @staticmethod
    def _join_sig(join: Any) -> tuple[str, str]:
        return str(join.side or "").upper() or str(join.kind or "").upper(), join.this.alias_or_name

    def relations(self, A: _Block, B: _Block, where: tuple[str, str], ctx: str
                  ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """两段读的来源对不对得上。对得上返回 (说到了没有, B 的别名 → A 的别名, A 已经说过的 WHERE 项, B 已经说过的
        WHERE 项)；对不上返回 None。"""
        la, lb = self.sides(where)
        ra, rb = [r.alias for r in A.rels], [r.alias for r in B.rels]
        ja = [self._join_sig(j) + (self.fp(j.args.get("on")) if j.args.get("on") is not None else "",)
              for j in A.joins]
        jb = [self._join_sig(j) + (self.fp(j.args.get("on")) if j.args.get("on") is not None else "",)
              for j in B.joins]
        kinds_a = {r.alias: r.kind for r in A.all_rels()}
        kinds_b = {r.alias: r.kind for r in B.all_rels()}
        if ra == rb and ja == jb and all(kinds_a[k] == kinds_b.get(k) for k in kinds_a):
            return True, {}, set(), set()
        # 换了一张表：开头那组来源只差一张实表（各自只出现一次），后面的连接一样
        if len(ra) == len(rb) and not A.joins and not B.joins:
            only_a = [r for r in A.rels if r.alias not in rb]
            only_b = [r for r in B.rels if r.alias not in ra]
            if len(only_a) == 1 and len(only_b) == 1 and only_a[0].table and only_b[0].table and \
                    A.names().count(only_a[0].name) == 1 and B.names().count(only_b[0].name) == 1:
                x, y = only_a[0], only_b[0]
                if x.name.translate(_ASCII_LOWER) == y.name.translate(_ASCII_LOWER):
                    return None
                self.add("tables", x.name, y.name, _say(la, "查的是", x.name, "表，", lb, "查的是", y.name, "表"))
                return True, {y.alias: x.alias}, set(), set()
        # 一边多关联了一张表（内连接）
        if not A.joins and not B.joins:
            if set(rb) < set(ra) and len(ra) == len(rb) + 1:
                return self._extra(A, B, la, lb, 0)
            if set(ra) < set(rb) and len(rb) == len(ra) + 1:
                return self._extra(B, A, lb, la, 1)
        if ctx != "top":
            return None
        return self._outer(A, B, where)

    def _extra(self, big: _Block, small: _Block, lbig: str, lsmall: str, side: int
               ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        from sqlglot import exp

        [rel] = [r for r in big.rels if r.alias not in {s.alias for s in small.rels}]
        if not rel.table or big.names().count(rel.name) != 1:
            return None
        conds = [c for c in big.where if rel.alias in {col.table for col in c.find_all(exp.Column)}]
        what = self.what(rel)
        shown = _say("（关联的条件是", self.show_all(conds, big), "）") if conds and \
            all(self.base_columns(c, big) is not None for c in conds) else ""
        used = any(t.name == rel.name and id(t) not in {id(i) for i in _items_of(small.select)}
                   for t in small.select.find_all(exp.Table))
        item = next((i for i in _items_of(big.select) if i.alias_or_name == rel.alias), None)
        if used and (_dup_insensitive(big.select) or
                     (item is not None and _unique_key_bound(item, conds, self.info, self.read))):
            # 另一边在子查询里用到了这张表、这一边又不怕重复的行：可能只是把 IN / EXISTS 改写成了关联（去重之后一样），
            # 说不准是不是真的不同。怕重复的（SUM、COUNT(*)）关联会把行按关联上的条数复制，那才说
            return None
        tail = ("没有关联它（只在子查询里用到了", rel.shown, "）") if used else ("没有",)
        text = _say(lbig, "关联了", what, shown, "，", lsmall, *tail)
        self.add("join", what if side == 0 else None, what if side == 1 else None, text)
        skip = {self.fp(c) for c in conds}
        return (True, {}, skip, set()) if side == 0 else (True, {}, set(), skip)

    def _nullable_used(self, block: _Block, alias: str) -> bool:
        """这一段在 ON 之外（WHERE、HAVING）用到了外连接可空一侧的列：可能把没关联上的行筛掉了（COALESCE(x, 0) > 1
        这类规范化认不出的写法），LEFT JOIN 就不一定是外连接。"""
        from sqlglot import exp

        clauses = block.where + block.having
        return any(c.table == alias for cond in clauses for c in cond.find_all(exp.Column))

    def _null_blind(self, block: _Block, alias: str) -> bool:
        """这一段不分组、结果只是跳过空值的聚合（COUNT(x)、SUM、AVG、MIN、MAX，外面可以再套 COALESCE、ROUND、相除、
        旁边多常量列），参数在可空一侧整行为空时一定是空值：LEFT JOIN 多出来的没关联上的行在这些聚合里都被跳过，和
        INNER JOIN 结果一样（见 _null_blind_left_joins；PostgreSQL / MySQL 里浮点数求和的先后可能变，规范化不改写，
        这里也不说成不同）。"""
        if block.group or _loose_columns(block.select):
            return False
        args = _aggregate_args(block.select)
        return bool(args) and all(_null_propagating(arg, alias, self.read) for arg in args)

    def _unused_left(self, block: _Block, alias: str) -> bool:
        """LEFT JOIN 进来的 alias 在 ON 之外哪儿都没用到、这一段又不怕重复的行（去重、只有去重的聚合、整段是 IN /
        EXISTS 子查询）：有没有这次 LEFT JOIN 结果都一样（见 _drop_unused_left_joins），不能说「A 关联了，B 没有」。"""
        from sqlglot import exp

        select = block.select
        joins = select.args.get("joins") or []
        own = list(_block_nodes(select))
        on = {id(n) for j in joins if j.this.alias_or_name == alias and j.args.get("on") is not None
              for n in j.args["on"].walk()}
        columns = [n for n in own if isinstance(n, exp.Column)] + \
            [c for sub in _nested_queries(select) for c in sub.find_all(exp.Column)]
        if any(c.table == alias and id(c) not in on for c in columns) or any(not c.table for c in columns) or \
                any(isinstance(p, exp.Star) for p in select.expressions):
            return False
        join = next((j for j in joins if j.this.alias_or_name == alias), None)
        return _dup_insensitive(select) or (join is not None and join.args.get("on") is not None and
                                            _unique_key_bound(join.this, _conjuncts(join.args["on"]), self.info,
                                                              self.read))

    def _outer(self, A: _Block, B: _Block, where: tuple[str, str]
               ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """（整条语句最外层）同一张表：LEFT JOIN 对 INNER JOIN、关联条件不同、只有一边关联了（LEFT JOIN）、外连接的
        主表对调。"""
        la, lb = self.sides(where)

        def kind(join: Any) -> str:
            return str(join.side or "").lower() or "inner"

        ra, rb = [r.alias for r in A.rels], [r.alias for r in B.rels]
        ja = {j.this.alias_or_name: j for j in A.joins}
        jb = {j.this.alias_or_name: j for j in B.joins}
        if len(A.joins) != len(ja) or len(B.joins) != len(jb) or any(not r.table for r in A.all_rels() + B.all_rels()):
            return None
        # 外连接的主表对调：两个来源，一边 x LEFT JOIN y，一边 y LEFT JOIN x，条件一样
        if len(ra) == len(rb) == 1 and len(ja) == len(jb) == 1 and ra != rb and set(ra) == set(jb) and \
                set(rb) == set(ja):
            x, y = A.joins[0], B.joins[0]
            if kind(x) == kind(y) == "left" and self.fp(x.args.get("on")) == self.fp(y.args.get("on")) and \
                    not self._nullable_used(A, x.this.alias_or_name) and not self._nullable_used(B, y.this.alias_or_name):
                wa = _say("以", A.rels[0].shown, "为主表外连接", B.rels[0].shown)
                wb = _say("以", B.rels[0].shown, "为主表外连接", A.rels[0].shown)
                self.add("join", wa, wb, _say(la, wa, "，", lb, wb))
                return True, {}, set(), set()
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
                if kind(x) != kind(y) or not _plain_join(x) or not _plain_join(y):
                    return None
                if self.fp(x) != self.fp(y):
                    on_x, on_y = x.args.get("on"), y.args.get("on")
                    if on_x is None or on_y is None or self._same_condition(_conjuncts(on_x), _conjuncts(on_y),
                                                                             A, B) is not False:
                        return None
                    if kind(x) == "left" and (self._nullable_used(A, alias) or self._nullable_used(B, alias)):
                        return None
                    # 两边的条件都得只涉及各自这一段里只出现一次的实表（自己关联自己时两个别名都叫 orders，写出来是同一个
                    # 等式、自相矛盾，分不清就不说）
                    if self.base_columns(on_x, A) is None or self.base_columns(on_y, B) is None:
                        return None
                    rel = A.rel(alias)
                    wa, wb = self.show(on_x, A, full=True), self.show(on_y, B, full=True)
                    self.add("join", wa, wb, _say(la, "关联", self.what(rel), "的条件是", wa, "，", lb, "是", wb))
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
            if join is None or kind(join) != "left" or not _plain_join(join):
                return None
            block, me, other = (A, la, lb) if x is not None else (B, lb, la)
            if self._nullable_used(block, alias) or self._unused_left(block, alias):
                return None
            rel = block.rel(alias)
            text = _say(me, *self._outer_words(join, block), self.what(rel), "，", other, "没有")
            self.add("join", rel.shown if x is not None else None, rel.shown if y is not None else None, text)
        return True, {}, skip_a, skip_b

    def _left_inner(self, join: Any, left: _Block, inner: _Block, alias: str, la: str, lb: str,
                    side: int) -> set[str] | None:
        """left 这边 LEFT JOIN alias，inner 那边同一张表是内连接：inner 的 WHERE 里有 left 的 ON 的每一项才算关联条件
        一样，只差连接类型。left 那边 WHERE 里用到可空一侧的列（可能已经把没关联上的行筛掉了）、结果只是可空一侧
        的列的聚合（两种写法结果一样）时不说。照原文说：left 那边原来写的是 RIGHT JOIN 的说以哪一侧为主表；inner
        那边写的是 LEFT / RIGHT JOIN、被 WHERE 里的条件变成了内连接的，说写的是什么、实际是什么。"""
        if not _plain_join(join) or self._nullable_used(left, alias) or self._null_blind(left, alias):
            return None
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

    def derived(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str]) -> bool:
        """FROM 里的子查询：两边别名对得上、定义不同的，只有一处时往里比（说「在 t 里」，照原来起的名字）。"""
        pairs = []
        for ra in A.all_rels():
            if ra.kind != "derived":
                continue
            rb = next((r for r in B.all_rels() if mapping.get(r.alias, r.alias) == ra.alias), None)
            if rb is None or rb.kind != "derived":
                return False
            if self.fp(ra.node.this) != self.fp(rb.node.this):
                pairs.append((ra, rb))
        if not pairs:
            return True
        if len(pairs) > 1:
            return False
        ra, rb = pairs[0]
        inner = (_deeper(where[0], ra.shown, plain=True), _deeper(where[1], rb.shown, plain=True))
        return self.query(ra.node.this, rb.node.this, inner, "derived")

    # ---- WHERE

    def timed(self, cond: Any, block: _Block) -> tuple[str, str] | None:
        """时间条件：一列（可以套着日期函数、CAST）和常量比较，列名像时间或者常量像日期。返回 (实表, 列)。"""
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
            return rel.name, col.name
        return None

    def conditions(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str],
                   skip_a: set[str], skip_b: set[str]) -> bool:
        la, lb = self.sides(where)
        xa = [c for c in A.where if self.fp(c) not in skip_a]
        xb = [c for c in B.where if self.fp(c) not in skip_b]
        only_a, only_b = self._unmatched(xa, xb, mapping)
        if not only_a and not only_b:
            return True
        covered = True
        only_a, only_b, ok = self._times(A, B, mapping, la, lb, only_a, only_b)
        covered &= ok
        only_a, only_b, ok = self._same_subject(A, B, mapping, la, lb, only_a, only_b)
        covered &= ok
        only_a, only_b, ok = self._join_equalities(A, B, mapping, la, lb, only_a, only_b)
        covered &= ok
        fa, fb = _say(self.la, where[0]), _say(self.lb, where[1])
        for side, rest, block, other_block, other_usage, me, other in (
                (0, only_a, A, B, self.usage_b, fa, fb), (1, only_b, B, A, self.usage_a, fb, fa)):
            for c in list(rest):
                refs = self.refs(c, block)
                if not refs or other_usage is None or \
                        other_usage.touches(refs, kinds=_kinds_for([c]), scope=other_block.select):
                    continue
                if self.range.subject(c) is not None and self.range.compare([c], []) is not False:
                    continue
                shown = self.show(c, block)
                excluded = self._excludes(c, block)
                text = _say(me, "排除了", excluded, "的行，", other, "没有") if excluded else \
                    _say(me, "只保留", shown, "的行，", other, "没有这个条件")
                self.add("filter", shown if side == 0 else None, shown if side == 1 else None, text,
                         {"columns": [{"table": t, "column": col} for t, col in sorted(refs)]})
                rest.remove(c)
        only_a, only_b, ok = self._subqueries(A, B, mapping, where, only_a, only_b)
        covered &= ok
        return covered and not only_a and not only_b

    def _unmatched(self, xs: list[Any], ys: list[Any], mapping: Mapping[str, str]) -> tuple[list[Any], list[Any]]:
        rest = list(ys)
        only_x = []
        for x in xs:
            found = next((y for y in rest if self.mfp(y, mapping) == self.fp(x)), None)
            if found is None:
                only_x.append(x)
            else:
                rest.remove(found)
        return only_x, rest

    def _on_column(self, conds: list[Any], block: _Block, key: tuple[str, str]) -> list[Any]:
        return [c for c in conds if self.refs(c, block) == {key}]

    def _times(self, A: _Block, B: _Block, mapping: Mapping[str, str], la: str, lb: str, only_a: list[Any],
               only_b: list[Any]) -> tuple[list[Any], list[Any], bool]:
        """时间条件：两边差在按哪一列筛时间、同一列上的范围，或者只有一边按时间筛。说到了的从两边拿掉。"""
        ta = [(c, t) for c in only_a if (t := self.timed(c, A)) is not None]
        tb = [(c, t) for c in only_b if (t := self.timed(c, B)) is not None]
        cols_a, cols_b = {t for _, t in ta}, {t for _, t in tb}
        if not (ta or tb) or len(cols_a) > 1 or len(cols_b) > 1 or self.usage_a is None or self.usage_b is None:
            return only_a, only_b, True
        detail = {"a": [{"table": t, "column": c} for t, c in sorted(cols_a)],
                  "b": [{"table": t, "column": c} for t, c in sorted(cols_b)]}
        all_a = {t for c in A.where if (t := self.timed(c, A)) is not None}
        all_b = {t for c in B.where if (t := self.timed(c, B)) is not None}
        if cols_a and cols_b and cols_a != cols_b:
            (ka,), (kb,) = cols_a, cols_b
            if all_a != cols_a or all_b != cols_b or self.usage_b.touches([ka], scope=B.select) or \
                    self.usage_a.touches([kb], scope=A.select):
                return only_a, only_b, False
            text = _say(la, "按", self.named(*ka), "筛时间，", lb, "按", self.named(*kb))
            if self._shape([c for c, _ in ta]) != self._shape([c for c, _ in tb]):
                text += _say("（", la, "是", self.show_all(_by_bound([c for c, _ in ta]), A), "，", lb, "是",
                             self.show_all(_by_bound([c for c, _ in tb]), B), "）")
            self.add("time", ka[1], kb[1], text, detail)
        elif cols_a == cols_b:
            (key,) = cols_a
            ra = _by_bound(self._on_column(A.where, A, key))
            rb = _by_bound(self._on_column(B.where, B, key))
            same = self.range.compare(ra, [self.mapped(c, mapping) for c in rb])
            if same is not False or self.usage_a.touches([key], ra, scope=A.select) or \
                    self.usage_b.touches([key], rb, scope=B.select):
                return only_a, only_b, False
            wa, wb = self.show_all(ra, A), self.show_all(rb, B)
            self.add("time_range", wa, wb, _say(la, "的时间范围是", wa, "，", lb, "是", wb))
        else:
            (key,) = cols_a or cols_b
            if cols_a:
                if self.usage_b.temporal or self.usage_b.touches([key], scope=B.select):
                    return only_a, only_b, False
                self.add("time", key[1], None, _say(la, "按", self.named(*key), "筛时间，", lb, "没有按时间筛选"), detail)
            else:
                if self.usage_a.temporal or self.usage_a.touches([key], scope=A.select):
                    return only_a, only_b, False
                self.add("time", None, key[1], _say(la, "没有按时间筛选，", lb, "按", self.named(*key), "筛时间"), detail)
        return [c for c in only_a if all(c is not x for x, _ in ta)], \
            [c for c in only_b if all(c is not x for x, _ in tb)], True

    def _same_subject(self, A: _Block, B: _Block, mapping: Mapping[str, str], la: str, lb: str,
                      only_a: list[Any], only_b: list[Any]) -> tuple[list[Any], list[Any], bool]:
        """同一个对象只和常量比的条件：两边这个对象上的条件合起来按取值逐段算（_Range），留下的行一样的不说；不一样
        的，两边都有没配上的条件时说「A 按 X 筛选，B 按 Y 筛选」，只有一边有时说「A 只保留 X 的行，B 没有这个条件」
        （另一边在这个对象上的条件这一边都有，多出来的确实多筛掉了行）。这一列在两边别的地方（别的条件、子查询、
        CTE）还用来筛选的不说：留下哪些行就不只是这几个条件定的。"""
        covered = True
        done_a: list[Any] = []
        done_b: list[Any] = []

        def subject(c: Any, block: _Block, m: Mapping[str, str]) -> str | None:
            s = self.range.subject(self.mapped(c, m))
            return s[0] if s is not None and self.refs(c, block) is not None else None

        keys_a = {id(c): subject(c, A, {}) for c in A.where}
        keys_b = {id(c): subject(c, B, mapping) for c in B.where}
        wanted = [k for k in dict.fromkeys([keys_a.get(id(c)) for c in only_a] + [keys_b.get(id(c)) for c in only_b])
                  if k is not None]
        for key in wanted:
            mine = [c for c in only_a if keys_a.get(id(c)) == key]
            theirs = [c for c in only_b if keys_b.get(id(c)) == key]
            all_a = [c for c in A.where if keys_a.get(id(c)) == key]
            all_b = [c for c in B.where if keys_b.get(id(c)) == key]
            refs_a = set().union(*(self.refs(c, A) for c in all_a)) if all_a else set()
            refs_b = set().union(*(self.refs(c, B) for c in all_b)) if all_b else set()
            done_a += mine
            done_b += theirs
            same = self.range.compare(all_a, [self.mapped(c, mapping) for c in all_b])
            usage_a, usage_b = self.usage_a, self.usage_b
            kinds = _kinds_for(all_a + all_b)
            if same is not False or usage_a is None or usage_b is None or \
                    usage_a.touches(refs_a | refs_b, all_a, kinds, A.select) or \
                    usage_b.touches(refs_a | refs_b, all_b, kinds, B.select):
                covered = False
                continue
            detail_a = {"columns": [{"table": t, "column": col} for t, col in sorted(refs_a)]}
            detail_b = {"columns": [{"table": t, "column": col} for t, col in sorted(refs_b)]}
            if mine and theirs:
                wa, wb = self.show_all(mine, A), self.show_all(theirs, B)
                if wa == wb:
                    wa, wb = self.show(exp_and(mine), A, full=True), self.show(exp_and(theirs), B, full=True)
                self.add("filter", wa, wb, _say(la, "按", wa, "筛选，", lb, "按", wb, "筛选"), detail_a)
            else:
                side, conds, block, me, other, detail = (0, mine, A, la, lb, detail_a) if mine else \
                    (1, theirs, B, lb, la, detail_b)
                shown = self.show_all(conds, block)
                excluded = self._excludes(conds[0], block) if len(conds) == 1 else None
                text = _say(me, "排除了", excluded, "的行，", other, "没有") if excluded else \
                    _say(me, "只保留", shown, "的行，", other, "没有这个条件")
                self.add("filter", shown if side == 0 else None, shown if side == 1 else None, text, detail)
        return [c for c in only_a if all(c is not d for d in done_a)], \
            [c for c in only_b if all(c is not d for d in done_b)], covered

    def _join_equalities(self, A: _Block, B: _Block, mapping: Mapping[str, str], la: str, lb: str,
                         only_a: list[Any], only_b: list[Any]) -> tuple[list[Any], list[Any], bool]:
        """两边没配上的条件都是「两列相等」的关联条件时，比相等关系把列分成的组：分法不同就是关联的条件不同（列各是各的
        表）；分法一样（a = b AND b = c 和 a = b AND a = c）不说。"""
        ea = [c for c in only_a if _column_equality(c) and self.base_columns(c, A) is not None]
        eb = [c for c in only_b if _column_equality(c) and self.base_columns(c, B) is not None]
        if not ea or not eb or len(ea) != len(only_a) or len(eb) != len(only_b):
            return only_a, only_b, True
        all_a = [c for c in A.where if _column_equality(c)]
        all_b = [self.mapped(c, mapping) for c in B.where if _column_equality(c)]
        if _partition(all_a) == _partition(all_b):
            return [], [], False
        wa, wb = self.show_all(ea, A), self.show_all(eb, B)
        if wa == wb:
            wa, wb = self.show(exp_and(ea), A, full=True), self.show(exp_and(eb), B, full=True)
        self.add("join", wa, wb, _say(la, "关联的条件是", wa, "，", lb, "是", wb))
        return [], [], True

    def _same_condition(self, xs: list[Any], ys: list[Any], A: _Block, B: _Block) -> bool | None:
        """两组条件（各自 AND 连）是不是一样：只差在两列相等的分法上、或者同一个对象只和常量比时能判断；不然 None。"""
        only_x, only_y = self._unmatched(xs, ys, {})
        if not only_x and not only_y:
            return True
        if all(_column_equality(c) for c in only_x + only_y):
            return _partition([c for c in xs if _column_equality(c)]) == \
                _partition([c for c in ys if _column_equality(c)])
        subjects = {self.range.subject(c)[0] if self.range.subject(c) else None for c in only_x + only_y}
        if None in subjects or len(subjects) != 1:
            return None
        (fp,) = subjects
        all_x = [c for c in xs if (s := self.range.subject(c)) is not None and s[0] == fp]
        all_y = [c for c in ys if (s := self.range.subject(c)) is not None and s[0] == fp]
        return self.range.compare(all_x, all_y)

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

    def _excludes(self, cond: Any, block: _Block) -> str | None:
        """「排除」的说法：排除的是哪些行。<> 常量、NOT IN 常量列表、NOT LIKE 除了等于 / 落在列表里 / 匹配上的行，
        这一列为空的行也筛掉了（拿空值比是空值，WHERE 只留下真的），照实说出来（表结构里确定不为空的列不说）；IS NOT
        NULL 排除的就是为空的行。别的 NOT（NOT (a AND b) 还把 a、b 有一个为空、另一个成立的行也筛掉了）说不清排除了
        哪些，返回 None，调用方改说「只保留 … 的行」。"""
        from sqlglot import exp

        def and_null(subject: Any, shown: str) -> str:
            cols = self.base_columns(subject, block) if isinstance(_unparen(subject), exp.Column) else None
            if cols and all(_not_null_column(self.info, r.name, c.name, self.read) for r, c in cols):
                return shown
            return _say(shown, "的行和", self.show(subject, block), "为空")

        cond = _unparen(cond)
        if isinstance(cond, exp.NEQ):
            left, right = cond.this, cond.expression
            subject = left if _fixed(right) and not _fixed(left) else (
                right if _fixed(left) and not _fixed(right) else None)
            if subject is None or _quantified(left) or _quantified(right):
                return None
            return and_null(subject, self.show(exp.EQ(this=left.copy(), expression=right.copy()), block))
        if isinstance(cond, exp.Not):
            inner = _unparen(cond.this)
            if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
                return _say(self.show(inner.this, block), "为空")
            if isinstance(inner, exp.In) and inner.expressions and not inner.args.get("query") and \
                    all(_fixed(i) and not isinstance(_unparen(i), exp.Null) for i in inner.expressions) and \
                    not _fixed(inner.this):
                return and_null(inner.this, self.show(inner, block))
            if isinstance(inner, (exp.Like, exp.ILike)) and _fixed(inner.expression) and not _fixed(inner.this):
                return and_null(inner.this, self.show(inner, block))
        return None

    def _subqueries(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str],
                    only_a: list[Any], only_b: list[Any]) -> tuple[list[Any], list[Any], bool]:
        """条件里套着的子查询：两边各一个、除了子查询之外写法一模一样（含 NOT、IN 左边的列）的配成一对往里比。"""
        covered = True
        done_a, done_b = [], []
        for c in only_a:
            template = self._template(c, {})
            if template is None:
                continue
            mine = [x for x in only_a if self._template(x, {}) == template]
            theirs = [d for d in only_b if self._template(d, mapping) == template]
            if len(mine) != 1 or len(theirs) != 1:
                continue
            d = theirs[0]
            place_a, place_b = self._place(c, A), self._place(d, B)
            inner = (_deeper(where[0], place_a), _deeper(where[1], place_b))
            kind = _subquery_kind(c)
            covered &= self.query(self._subquery(c), self._subquery(d), inner, kind)
            done_a.append(c)
            done_b.append(d)
        return [c for c in only_a if all(c is not x for x in done_a)], \
            [c for c in only_b if all(c is not x for x in done_b)], covered

    def _template(self, cond: Any, mapping: Mapping[str, str]) -> str | None:
        """条件里只套着一个子查询时，子查询换成占位符之后的写法；不然 None。"""
        from sqlglot import exp

        tops = [n for n in cond.walk() if isinstance(n, (exp.Select, exp.SetOperation)) and
                not any(isinstance(p, (exp.Select, exp.SetOperation)) for p in _ancestors(n, cond))]
        if len(tops) != 1:
            return None
        copy = self.mapped(cond, mapping).copy() if mapping else cond.copy()
        for node in copy.walk():
            if isinstance(node, (exp.Select, exp.SetOperation)):
                node.replace(exp.Literal.string("<subquery>"))
                break
        return self.fp(copy)

    @staticmethod
    def _subquery(cond: Any) -> Any:
        from sqlglot import exp

        return next(n for n in cond.walk() if isinstance(n, (exp.Select, exp.SetOperation)))

    def _place(self, cond: Any, block: _Block) -> str:
        """子查询在条件里的位置：「users.id NOT IN 的子查询」「NOT EXISTS 子查询」「条件的子查询」。"""
        from sqlglot import exp

        sub = self._subquery(cond)
        node = sub
        while isinstance(node.parent, (exp.Subquery, exp.Paren)):
            node = node.parent
        parent = node.parent
        negated = isinstance(_unparen_parent(parent), exp.Not) if parent is not None else False
        if isinstance(parent, exp.Exists):
            return "NOT EXISTS 子查询" if negated else "EXISTS 子查询"
        if isinstance(parent, exp.In):
            return _say(self.show(parent.this, block), "NOT IN" if negated else "IN", "的子查询")
        return "条件的子查询"

    # ---- 选出来的列、分组

    def groups(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str],
               ctx: str = "top") -> bool:
        """分组键：两边不同、又都只涉及实表列、多出来的键不是另一边的键算出来的时，说各按什么分组。不说的：在 IN /
        EXISTS 子查询里（只看在不在、有没有，分不分组常常不影响）；两边的键只差在被关联条件定成相等的列上（ON
        o.user_id = u.id 之后按 u.id 还是 o.user_id 分组，类型一样时分出的组一样，类型不知道时也说不准）。"""
        la, lb = self.sides(where)
        ka = sorted(self.fp(g) for g in A.group)
        kb = sorted(self.mfp(g, mapping) for g in B.group)
        if ka == kb:
            return True
        if ctx in ("in", "exists") or sorted(set(ka)) == sorted(set(kb)):
            # 只差在重复写了一遍的分组键上（结果看行出来先后的一段规范化不去重）：分出的组一样
            return False
        classes = self.equal_classes(A, B, mapping)
        if sorted(self.classed(A.group, classes, {})) == sorted(self.classed(B.group, classes, mapping)):
            return False
        ra = [self.refs(g, A) for g in A.group]
        rb = [self.refs(g, B) for g in B.group]
        if any(r is None for r in ra + rb):
            return False
        cols_a = set().union(*ra) if ra else set()
        cols_b = set().union(*rb) if rb else set()
        extra_a = [g for g, r in zip(A.group, ra) if self.fp(g) not in kb]
        extra_b = [g for g, r in zip(B.group, rb) if self.mfp(g, mapping) not in ka]
        if any(self.refs(g, A) <= cols_b for g in extra_a) or any(self.refs(g, B) <= cols_a for g in extra_b):
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

    def _dedup_swap(self, A: _Block, B: _Block, mapping: Mapping[str, str]) -> bool:
        """一边 SELECT DISTINCT（不分组），另一边按选出来的那几列分组、不去重、不聚合：两种写法都是去掉重复的行，
        不能说成一边去重一边没去重。"""
        def plain(block: _Block) -> bool:
            return block.distinct and not block.distinct_on and not block.group and not _aggregating(block.select)

        def grouped(block: _Block, m: Mapping[str, str]) -> set[str] | None:
            if block.distinct or not block.group or block.having or \
                    any(_aggregate(e.unalias()) is not None for e in block.exprs):
                return None
            keys = {self.mfp(g, m) for g in block.group}
            outs = {self.mfp(e.unalias(), m) for e in block.exprs}
            return keys if keys == outs else None

        outs_a = {self.fp(e.unalias()) for e in A.exprs}
        outs_b = {self.mfp(e.unalias(), mapping) for e in B.exprs}
        return (plain(A) and grouped(B, mapping) == outs_a) or (plain(B) and grouped(A, {}) == outs_b)

    def outputs(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str], ctx: str) -> bool:
        """分组键、选出来的列。选出来的列只在整条语句最外层、合并的一支、标量子查询里比（CTE、FROM 里的子查询选出来的
        列规范化时改过名、排过序，和原文对不上）；分组键不同时，选出来的列只差在分组键那几列上的算在分组键的不同里。
        其余逐列比：都是一个聚合、都只是一列时才说，两列被关联条件定成相等的不说。"""
        la, lb = self.sides(where)
        ga = [self.fp(g) for g in A.group]
        gb = [self.mfp(g, mapping) for g in B.group]
        regrouped = sorted(ga) != sorted(gb)
        if self._dedup_swap(A, B, mapping):
            return False
        covered = self.groups(A, B, mapping, where, ctx)
        ea, eb = A.exprs, B.exprs
        ta, tb = [self.fp(e.unalias()) for e in ea], [self.mfp(e.unalias(), mapping) for e in eb]
        if ta == tb:
            return covered
        if ctx not in ("top", "branch", "scalar") or any(_is_star(e) for e in ea + eb):
            return False
        if regrouped and covered:
            na = next((i for i, t in enumerate(ta) if t not in ga), len(ta))
            nb = next((i for i, t in enumerate(tb) if t not in gb), len(tb))
            if set(ta[:na]) == set(ga) and set(tb[:nb]) == set(gb) and ta[na:] == tb[nb:]:
                return covered
        if len(ea) != len(eb) or sorted(ta) == sorted(tb):
            if any(self.base_columns(e, A) is None for e in ea) or any(self.base_columns(e, B) is None for e in eb):
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
            if self.fp(x.unalias()) == self.mfp(y.unalias(), mapping):
                continue
            if not self.output(x, y, A, B, mapping, la, lb, i, len(ea)):
                covered = False
        return covered

    def output(self, x: Any, y: Any, A: _Block, B: _Block, mapping: Mapping[str, str], la: str, lb: str, i: int,
               n: int) -> bool:
        from sqlglot import exp

        if self.base_columns(x, A) is None or self.base_columns(y, B) is None:
            return False
        xu, yu = x.unalias(), y.unalias()
        equal = _partition([c for c in A.where if _column_equality(c)] +
                           [self.mapped(c, mapping) for c in B.where if _column_equality(c)])
        ga, gb = _aggregate(xu), _aggregate(yu)
        if ga is not None and gb is not None:
            (fa, arg_a, da), (fb, arg_b, db) = ga, gb
            same_arg = (arg_a is None and arg_b is None) or (
                arg_a is not None and arg_b is not None and (self.fp(arg_a) == self.mfp(arg_b, mapping) or (
                    isinstance(_unparen(arg_a), exp.Column) and isinstance(_unparen(arg_b), exp.Column) and
                    self.refs(arg_a, A) == self.refs(arg_b, B))))
            if not same_arg and any(arg is not None and not isinstance(_unparen(arg), exp.Column)
                                    for arg in (arg_a, arg_b)):
                return False
            if same_arg and fa == fb and da == db:
                return False
            if not same_arg and arg_a is not None and arg_b is not None and \
                    _same_class(equal, self.fp(arg_a), self.mfp(arg_b, mapping)):
                return False
            if fa == fb and same_arg and fa in ("min", "max"):
                return False
            if {fa, fb} == {"count"} and (arg_a is None) != (arg_b is None):
                arg = arg_a if arg_a is not None else arg_b
                block = A if arg_a is not None else B
                refs = self.refs(arg, block) or set()
                if self.usage_a is None or self.usage_b is None or \
                        self.usage_a.touches(refs, [xu], scope=A.select) or self.usage_b.touches(refs, [yu], scope=B.select):
                    return False
            wa = self.show(arg_a, A) if arg_a is not None else "*"
            wb = self.show(arg_b, B) if arg_b is not None else "*"
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
        if isinstance(xu, exp.Column) and isinstance(yu, exp.Column):
            if _same_class(equal, self.fp(xu), self.mfp(yu, mapping)) or self.refs(xu, A) == self.refs(yu, B):
                return False
            wa, wb = self.show(x, A), self.show(y, B)
            if wa == wb:
                wa, wb = self.show(x, A, full=True), self.show(y, B, full=True)
            where_col = _say("第", str(i), "列") if n > 1 else ""
            self.add("expression", wa, wb, _say(la, where_col, "选的是", wa, "，", lb, "选的是", wb))
            return True
        return False

    # ---- HAVING、取行

    def having(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str]) -> bool:
        """HAVING：同一个对象（聚合）只和常量比的，两边合起来逐段算，留下的组一样的不说（COUNT(*) > 1 和 >= 2、分组
        之后 COUNT(*) > 0）；只有一边有、另一边 HAVING 里没有别的条件的，说「分组后只保留」。"""
        from sqlglot import exp

        la, lb = self.sides(where)
        only_a, only_b = self._unmatched(A.having, B.having, mapping)
        if not only_a and not only_b:
            return True
        if any(isinstance(_unparen(c), exp.Boolean) for c in only_a + only_b):
            # 留着的 HAVING TRUE 只说明「没有 GROUP BY 时它让语句成了只出一行的聚合」（见 _drop_true_conjuncts），说
            # 「分组后只保留 TRUE 的组」读不懂，也没说到真正的不同
            return False
        grouped = bool(A.group) and bool(B.group)
        if any(self.base_columns(c, A) is None for c in only_a) or any(self.base_columns(c, B) is None for c in only_b):
            return False
        subjects = {s[0] for c in only_a + [self.mapped(c, mapping) for c in only_b]
                    if (s := self.range.subject(c)) is not None}
        if len(subjects) == 1:
            (fp,) = subjects
            all_a = [c for c in A.having if (s := self.range.subject(c)) is not None and s[0] == fp]
            all_b = [self.mapped(c, mapping) for c in B.having
                     if (s := self.range.subject(self.mapped(c, mapping))) is not None and s[0] == fp]
            if len(all_a) != len(A.having) or len(all_b) != len(B.having) or \
                    self.range.compare(all_a, all_b, grouped=grouped) is not False:
                return False
        elif subjects or only_a and only_b:
            return False
        if only_a and only_b:
            wa, wb = self.show_all(only_a, A), self.show_all(only_b, B)
            self.add("having", wa, wb, _say(la, "分组后只保留", wa, "的组，", lb, "只保留", wb, "的组"))
            return True
        side, rest, block, me, other = (0, only_a, A, la, lb) if only_a else (1, only_b, B, lb, la)
        usage = self.usage_b if side == 0 else self.usage_a
        refs = set().union(*(self.refs(c, block) or set() for c in rest))
        if (B.having if side == 0 else A.having) or usage is None or \
                usage.touches(refs, scope=(B if side == 0 else A).select):
            return False
        shown = self.show_all(rest, block)
        self.add("having", shown if side == 0 else None, shown if side == 1 else None,
                 _say(me, "分组后只保留", shown, "的组，", other, "没有这个条件"))
        return True

    def rows(self, A: _Block, B: _Block, mapping: Mapping[str, str], where: tuple[str, str], ctx: str) -> bool:
        """取行、排序、去重。取行在标量子查询、EXISTS 子查询里不说（SQLite 的标量子查询本来就取第一行），不分组的聚合
        （只出一行：取行、排序、去重都不起作用）不说；排序只在整条语句最外层、没有取行、DISTINCT ON、窗口函数时说成
        order，有取行时算 limit，别的地方不说（子查询里的排序可能决定 GROUP_CONCAT 的先后），按常量排的不说。"""
        from sqlglot import exp

        la, lb = self.sides(where)
        covered = True

        def single(block: _Block) -> bool:
            # 只出一行：不分组的聚合；或者每个分组键都被 WHERE 里「键 = 常量」定死了（WHERE user_id = 3 GROUP BY
            # user_id 最多一组）
            if _aggregating(block.select) and not block.group:
                return True
            if not block.group:
                return False
            pinned = set()
            for c in block.where:
                c = _unparen(c)
                if isinstance(c, exp.EQ):
                    for x, y in ((c.this, c.expression), (c.expression, c.this)):
                        if _literal_value(y) is not None:
                            pinned.add(self.fp(_unparen(x)))
            return all(self.fp(g) in pinned for g in block.group)

        if A.limit != B.limit or A.offset != B.offset:
            if ctx in ("scalar", "exists") or single(A) or single(B):
                covered = False
            else:
                if A.limit != B.limit:
                    self.add("limit", A.limit, B.limit, _say(la, _rows(A.limit), "，", lb, _rows(B.limit)))
                if A.offset != B.offset:
                    self.add("limit", A.offset, B.offset, _say(la, _skip(A.offset), "，", lb, _skip(B.offset)))
        orders_a, orders_b = [self.fp(o) for o in A.order], [self.mfp(o, mapping) for o in B.order]

        def keys_once(block: _Block, m: Mapping[str, str]) -> list[str]:
            out: list[str] = []
            for o in block.order:
                key = self.mfp(o.this if isinstance(o, exp.Ordered) else o, m)
                if key not in out:
                    out.append(key)
            return out

        if orders_a != orders_b:
            limited = A.limit is not None or B.limit is not None or A.offset is not None or B.offset is not None
            classes = self.equal_classes(A, B, mapping)
            same_once = keys_once(A, {}) == keys_once(B, mapping) and all(
                self.fp(x) == self.mfp(y, mapping) for x, y in zip(_first_orders(A.order), _first_orders(B.order)))
            if same_once or self.classed(A.order, classes, {}) == self.classed(B.order, classes, mapping):
                # 排序键只差在被关联条件定成相等的列上：排出来的先后可能一样，说不准
                covered = False
            elif A.distinct_on or B.distinct_on or A.windowed or B.windowed or single(A) or single(B) or \
                    any(not self.base_columns(o.this, A) for o in A.order) or \
                    any(not self.base_columns(o.this, B) for o in B.order) or \
                    (not limited and ctx != "top") or (limited and ctx in ("scalar", "exists")):
                covered = False
            else:
                wa, wb = self._sorting(A.order, A), self._sorting(B.order, B)
                if limited:
                    self.add("limit", wa, wb, _say(la, "取行前", wa, "，", lb, wb))
                else:
                    self.add("order", wa, wb, _say(la, wa, "，", lb, wb))
        if A.distinct != B.distinct:
            if A.distinct_on or B.distinct_on or self._dedup_swap(A, B, mapping) or ctx in ("in", "exists") or \
                    single(A) or single(B):
                return False
            block = A if A.distinct else B
            keys = {self.fp(g) for g in block.group}
            if keys and keys <= {self.fp(e.unalias()) for e in block.exprs}:
                return False
            me, other = (la, lb) if A.distinct else (lb, la)
            self.add("group_by", "DISTINCT" if A.distinct else None, "DISTINCT" if B.distinct else None,
                     _say(me, "用 SELECT DISTINCT 去掉了重复的行，", other, "没有"))
        return covered

    def _sorting(self, order: list[Any], block: _Block) -> str:
        if not order:
            return "没有排序"
        keys = "、".join(_say(self.show(o.this, block), "降序" if o.args.get("desc") else "升序") for o in order)
        return _say("按", keys, "排")

    # ---- UNION

    def setop(self, a: Any, b: Any, where: tuple[str, str], ctx: str) -> bool:
        """合并：种类不同（UNION 对 INTERSECT）不说具体的；UNION 对 UNION ALL 说一句。最外层的 UNION ALL 各支排过序，
        按写法配对；别的（去重的 UNION、EXCEPT、INTERSECT、套在里面的）按先后逐支比（EXCEPT 两边对调了就对不上）。
        只有一支不同的往里比。"""
        from sqlglot import exp

        la, lb = self.sides(where)
        if type(a) is not type(b):
            return False
        xa, xb = _set_branches(a), _set_branches(b)
        covered = True
        if bool(a.args.get("distinct")) != bool(b.args.get("distinct")) and isinstance(a, exp.Union):
            every = xa if not a.args.get("distinct") else xb
            if ctx in ("in", "exists") or all(_self_deduped(x) for x in every):
                # IN / EXISTS 里去不去重不影响；UNION ALL 的各支自己都已经去重时，有没有重复的行要看各支之间相不相交
                # （WHERE user_id > 5 和 user_id <= 5 两支不相交，UNION 和 UNION ALL 一样），说不准
                covered = False
            else:
                wa, wb = _merging(a), _merging(b)
                self.add("set_op", wa, wb, _say(la, "用", wa, "，", lb, "用", wb))
        if len(xa) != len(xb):
            if isinstance(a, exp.Union) and bool(a.args.get("distinct")) == bool(b.args.get("distinct")):
                self.add("set_op", str(len(xa)), str(len(xb)),
                         _say(la, "合并了", str(len(xa)), "支查询，", lb, "合并了", str(len(xb)), "支"))
            return False
        free = isinstance(a, exp.Union) and not a.args.get("distinct") and not b.args.get("distinct") and \
            ctx == "top" and not any(a.args.get(k) or b.args.get(k) for k in ("order", "limit", "offset"))
        if free:
            only_a, only_b = self._unmatched(xa, xb, {})
            pairs = list(zip(only_a, only_b)) if len(only_a) == len(only_b) == 1 else None
            if only_a and pairs is None:
                return False
        else:
            pairs = [(x, y) for x, y in zip(xa, xb) if self.fp(x) != self.fp(y)]
            if len(pairs) > 1:
                return False
        for x, y in pairs or []:
            if isinstance(a, exp.Except):
                index = next(i for i, v in enumerate(xa) if v is x)
                place = "EXCEPT 前面那支" if index == 0 else "EXCEPT 后面那支"
            else:
                place = "合并的一支"
            branch = (_deeper(where[0], place), _deeper(where[1], place))
            covered &= self.query(x, y, branch, "branch" if ctx == "top" else ctx)
        for key in ("order", "limit", "offset"):
            va = self.fp(a.args[key]) if a.args.get(key) else ""
            vb = self.fp(b.args[key]) if b.args.get(key) else ""
            if va != vb:
                covered = False
        return covered


def exp_and(conds: list[Any]) -> Any:
    from sqlglot import exp

    return conds[0] if len(conds) == 1 else exp.and_(*[c.copy() for c in conds], copy=False)


def _column_equality(cond: Any) -> bool:
    from sqlglot import exp

    cond = _unparen(cond)
    return isinstance(cond, exp.EQ) and isinstance(_unparen(cond.this), exp.Column) and \
        isinstance(_unparen(cond.expression), exp.Column)


def _partition(conds: list[Any]) -> frozenset[frozenset[str]]:
    """「两列相等」的几个条件把列分成的组（传递：a = b、b = c 时 a、b、c 一组）。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in conds:
        c = _unparen(c)
        a, b = _fingerprint(_unparen(c.this)), _fingerprint(_unparen(c.expression))
        parent[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    return frozenset(frozenset(g) for g in groups.values() if len(g) > 1)


def _same_class(partition: frozenset[frozenset[str]], x: str, y: str) -> bool:
    return any(x in g and y in g for g in partition)


def _subquery_kind(cond: Any) -> str:
    from sqlglot import exp

    sub = next(n for n in cond.walk() if isinstance(n, (exp.Select, exp.SetOperation)))
    node = sub
    while isinstance(node.parent, (exp.Subquery, exp.Paren)):
        node = node.parent
    parent = node.parent
    if isinstance(parent, exp.Exists):
        return "exists"
    if isinstance(parent, exp.In):
        return "in"
    return "scalar"


def _deeper(where: str, place: str, plain: bool = False) -> str:
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


def _first_orders(order: list[Any]) -> list[Any]:
    """ORDER BY 各项里每个表达式第一次出现的那一项（后面重复的不影响先后）。"""
    from sqlglot import exp

    seen: set[str] = set()
    out = []
    for o in order:
        key = _fingerprint(o.this if isinstance(o, exp.Ordered) else o)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _self_deduped(query: Any) -> bool:
    """合并的一支自己出不了重复的行：SELECT DISTINCT、按选出来的全部列分组（不出别的列）、不分组的聚合（只出一行）。"""
    from sqlglot import exp

    if isinstance(query, exp.Subquery):
        query = query.this
    if not isinstance(query, exp.Select):
        return False
    distinct = query.args.get("distinct")
    if distinct is not None and not distinct.args.get("on"):
        return True
    group = query.args.get("group")
    if group is not None and group.expressions:
        keys = {_fingerprint(k) for k in group.expressions}
        return all(_fingerprint(p.unalias()) in keys for p in query.expressions)
    return _aggregating(query) and group is None and query.args.get("having") is None


def _merging(query: Any) -> str:
    return "UNION 合并（去掉重复的行）" if query.args.get("distinct") else "UNION ALL（重复的行都留着）"


def _aggregate(node: Any) -> tuple[str, Any, bool] | None:
    """光一个聚合（SUM / AVG / COUNT / MIN / MAX，MIN / MAX 只有一个参数：两个参数的 max(a, b) 是取较大的那个，不是
    聚合）：(函数, 参数（COUNT(*) 是 None）, 是否去重)。"""
    from sqlglot import exp

    kinds = {exp.Sum: "sum", exp.Avg: "avg", exp.Count: "count", exp.Min: "min", exp.Max: "max"}
    func = kinds.get(type(node))
    if func is None or node.args.get("expressions"):
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

    等价怎么判：两边各自规范化（见 _canonical_forms：认列之后的那一级、不认列的轻量一级、只统一名字的一级），每一步都
    只做保持语义的变换；同一级的两个规范形式完全一样（比语法树，见 _Form.key）才算等价，否则一律不等价，items 一定
    不为空。schema 是数据源的 schema_cache，给了就按它认列、展开 SELECT *（表结构要和库对得上）。原文里有解析时丢了
    写法的（一元加号、0x10、换了名字的类型名，见 _lost_in_parsing）不在语法树上判等价。

    不等价时怎么说：在两边的规范形式上逐段比（见 _Explainer），只把有把握、确实不同的地方写成具体的中文句子；具体说法
    一条都没有时给一条 aspect="other" 的兜底说明（「两条 SQL 的写法不同，未能归纳成具体差异，请对照原文」），只覆盖了
    一部分时追加一条「另有未能归纳的差异」，fallback 为真；text_diff 给出规范化之后不同的行。

    labels 是两边的叫法（缺省 A / B，双盲复算传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」: 中文名}，
    可以用 catalog_labels 从数据目录取），时间列写成「支付时间 paid_at」。任一边解析不了：只给一条 aspect="parse"。
    """
    la, lb = labels
    read = _read(dialect)
    if read == "mysql":
        a, b = (_mysql_dashes(x) if isinstance(x, str) else x for x in (a, b))
    ra, ea = _parse(a, read)
    rb, eb = _parse(b, read)
    if ea or eb:
        if ea and eb:
            text = _say(la, "：", ea, "；", lb, "：", eb, "；没有逐项比较")
        else:
            side, error = (la, ea) if ea else (lb, eb)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return SqlDiff(False, [FacetDiff("parse", ea, eb, text)])
    if a.strip() == b.strip():
        # 一字不差就是同一条语句（带会执行的注释、RANDOM() 这些不在语法树上判等价的写法也一样）
        return SqlDiff(True, level=_PARSED)
    info = _schema_info(schema)
    forms_a: dict[str, _Form] = {}
    forms_b: dict[str, _Form] = {}
    try:
        marked_a = _mark_surfaces(ra, a.strip(), read)
        marked_b = _mark_surfaces(rb, b.strip(), read)
        if read == "sqlite":
            _mark_comma_joins(ra, a.strip(), read)
            _mark_comma_joins(rb, b.strip(), read)
        forms_a = {} if not marked_a or _lost_in_parsing(a.strip(), read, ra) else _canonical_forms(ra, read, info)
        forms_b = {} if not marked_b or _lost_in_parsing(b.strip(), read, rb) else _canonical_forms(rb, read, info)
    except Exception:  # noqa: BLE001 - 规范化本身的缺陷不能连累调用方：当作说不清，记下来修
        logger.warning("SQL 规范化失败", exc_info=True)
        forms_a, forms_b = {}, {}
    for level in (_OPTIMIZER, _LIGHT, _PARSED):
        fa, fb = forms_a.get(level), forms_b.get(level)
        if fa is not None and fb is not None and fa.key == fb.key:
            return SqlDiff(True, level=level)
    level = next((lv for lv in (_OPTIMIZER, _LIGHT) if lv in forms_a and lv in forms_b), None)
    items: list[FacetDiff] = []
    covered = False
    if level is not None:
        explainer = _Explainer(read, (la, lb), names, info)
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
