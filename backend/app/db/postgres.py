"""应用库是 PostgreSQL 时，让它在 SQLite 上写成的那些假设下照样成立。

只装在应用库那个引擎的方言实例上（db/base.make_engine），业务数据源的 PostgreSQL 引擎不受影响。

**建表的列类型。** 应用是在 SQLite 上写成的：SQLite 不查 VARCHAR 的长度，INTEGER 是 64 位。PostgreSQL 上
VARCHAR(n) 超长直接报错、INTEGER 只有 32 位。模型里的 String(n) 是写给人看的「大致多长」，从来没有被库约束过：
逐列核对只核得到测试走到的值，漏一处就是线上的写入失败，从 SQLite 导过来的存量数据里也可能已经有超长的。所以
应用库上 String(n) 建成不限长的 VARCHAR（PostgreSQL 里 varchar、varchar(n)、text 存储和性能一样，限长只多一次
检查）。CAST(x AS VARCHAR(n)) 这种查询里的写法不动——那是截断的意思。Integer 列在 PostgreSQL 上是 BIGINT：
那是在模型这一层换的（db/base._portable_integer），绑定参数的类型转换跟着列走，大数不在半路溢出。

**文本怎么比大小、怎么排序。** SQLite 缺省按字节比（BINARY）；PostgreSQL 按建库时的区域设置（常见的 en_US.UTF-8 不分
大小写地先比字母、忽略标点），同一列排出来的先后、`key < '…'` 这种比较的结果都会变。应用库上的文本列建表时一律带
COLLATE "C"（按字节比），和 SQLite 一致，不看建库时选了什么区域；LIKE 'xx%' 这种前缀匹配也因此用得上普通索引。
ILIKE、lower() 照旧按库的字符集分大小写。

**ORDER BY 里 NULL 的位置。** SQLite 升序 NULL 在前、降序 NULL 在后，PostgreSQL 正好相反。应用里两百多处排序，
可空的列（开始时间、到期时间、发布时间……）排出来的先后要和 SQLite 一致：可空列上没写 nulls_first / nulls_last
的 ASC 补 NULLS FIRST、DESC 补 NULLS LAST，ORDER BY 里直接写列名（隐含 ASC）的同样补上。不可空的列不动——
补了会让索引用不上，又没有 NULL 可排。窗口函数 OVER (ORDER BY …) 里同样补。原生 text() 里写的 ORDER BY 不经过
这里，要自己写明（db/base 的去重语句里就写了）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql.asyncpg import PGCompiler_asyncpg
from sqlalchemy.dialects.postgresql.base import PGTypeCompiler
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import ClauseList, UnaryExpression

#: 外层已经写了 nulls_first / nulls_last：里面那一层 ASC / DESC 不再补
_EXPLICIT = "_agentlab_explicit_nulls"


def _nullable(element: Any) -> bool:
    return isinstance(element, Column) and bool(element.nullable) and not element.primary_key


#: 建表时文本列的排序规则：按字节比，同 SQLite 的 BINARY（见模块说明）
_COLLATE = ' COLLATE "C"'


class AppTypeCompiler(PGTypeCompiler):
    def visit_string(self, type_: Any, **kw: Any) -> str:
        if isinstance(kw.get("type_expression"), Column):
            return ("VARCHAR" if type_.length else super().visit_string(type_, **kw)) + _COLLATE
        return super().visit_string(type_, **kw)

    def visit_text(self, type_: Any, **kw: Any) -> str:
        text = super().visit_text(type_, **kw)
        return text + _COLLATE if isinstance(kw.get("type_expression"), Column) else text


def _ordered(clauses: Any) -> list[Any]:
    """ORDER BY 里直接写的可空列补成 ASC（visit_unary 再给它补 NULLS FIRST）。"""
    return [c.asc() if _nullable(c) else c for c in clauses]


class AppCompiler(PGCompiler_asyncpg):
    def order_by_clause(self, select: Any, **kw: Any) -> str:
        order_by = self._generate_delimited_list(_ordered(select._order_by_clauses), ", ", **kw)
        return " ORDER BY " + order_by if order_by else ""

    def visit_over(self, over: Any, **kw: Any) -> str:
        clauses = list(over.order_by) if over.order_by is not None else []
        if any(_nullable(c) for c in clauses):
            # 在副本上换：语句对象可能被缓存、被别处共用，不能改原件
            over = over._clone()
            over.order_by = ClauseList(*_ordered(clauses))
        return super().visit_over(over, **kw)

    def visit_unary(self, unary: UnaryExpression, **kw: Any) -> str:
        modifier = unary.modifier
        if modifier in (operators.nulls_first_op, operators.nulls_last_op):
            return super().visit_unary(unary, **{**kw, _EXPLICIT: True})
        explicit = kw.pop(_EXPLICIT, False)
        text = super().visit_unary(unary, **kw)
        if not explicit and modifier in (operators.asc_op, operators.desc_op) and _nullable(unary.element):
            text += " NULLS FIRST" if modifier is operators.asc_op else " NULLS LAST"
        return text


def install(dialect: Any) -> None:
    """装到应用库引擎的方言实例上。"""
    dialect.statement_compiler = AppCompiler
    dialect.type_compiler_instance = dialect.type_compiler = AppTypeCompiler(dialect)
