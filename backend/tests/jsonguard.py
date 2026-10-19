"""测试时挂在应用库编译器上的守卫：整个 JSON 值做比较、去重、分组、排序就报错（两种库都挂，跑全量时一直生效）。

PostgreSQL 的 json 没有相等和大小的运算符：`=`、`!=`、`IN`、`IS DISTINCT FROM`、`<` 之类，SELECT DISTINCT、GROUP BY、
ORDER BY、UNION（去重的那几种）碰到 json 类型的值都报错；SQLite 照常执行。只在 PostgreSQL 上跑到才发现太晚：这里在编译
那一刻就拦下，SQLite 的测试也拦得到。按表达式的类型认，不按写法认：跨行的 order_by、变量别名、
`select(Run.origin).distinct()`、JSON 的下标（`Run.origin["k"] == 1`，取出来的还是 json）都算。按键筛选用 db/portable 的
json_eq 等（类型是布尔、文本），不受影响。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import UnaryExpression


class JsonMisuse(AssertionError):
    """整个 JSON 值被拿去比较、去重、分组或排序。"""


_COMPARE = {operators.eq, operators.ne, operators.in_op, operators.not_in_op, operators.is_distinct_from,
            operators.is_not_distinct_from, operators.lt, operators.le, operators.gt, operators.ge}


def _json(element: Any) -> bool:
    while isinstance(element, UnaryExpression) and element.modifier is not None:
        element = element.element
    return isinstance(getattr(element, "type", None), JSON)


class JsonGuard:
    """混进编译器类里用：class Guarded(JsonGuard, 原来的编译器)。"""

    def visit_binary(self, binary: Any, **kw: Any) -> str:
        if binary.operator in _COMPARE and (_json(binary.left) or _json(binary.right)):
            raise JsonMisuse(f"整个 JSON 值做了比较（{binary.operator.__name__}）：PostgreSQL 的 json 没有这个运算符，"
                             "按键筛选用 db/portable.json_eq 等")
        return super().visit_binary(binary, **kw)  # type: ignore[misc]

    def visit_select(self, select: Any, **kw: Any) -> str:
        if select._distinct and any(_json(c) for c in select.selected_columns):
            raise JsonMisuse("SELECT DISTINCT 里有 JSON 列：PostgreSQL 的 json 没有相等运算符，去不了重")
        if any(_json(c) for c in select._group_by_clauses):
            raise JsonMisuse("GROUP BY 了 JSON 值：PostgreSQL 的 json 没有相等运算符")
        if any(_json(c) for c in select._order_by_clauses):
            raise JsonMisuse("ORDER BY 了 JSON 值：PostgreSQL 的 json 没有大小")
        return super().visit_select(select, **kw)  # type: ignore[misc]

    def visit_compound_select(self, cs: Any, **kw: Any) -> str:
        keyword = str(getattr(cs.keyword, "value", cs.keyword)).upper()
        if "ALL" not in keyword and any(_json(c) for c in cs.selected_columns):
            raise JsonMisuse(f"{keyword or 'UNION'} 去重时碰到 JSON 列：PostgreSQL 的 json 没有相等运算符")
        return super().visit_compound_select(cs, **kw)  # type: ignore[misc]


def install(dialect: Any) -> None:
    """挂到应用库引擎的方言实例上（PostgreSQL 时是 db/postgres 换过的编译器，SQLite 时是原来的）。"""
    base = dialect.statement_compiler
    if issubclass(base, JsonGuard):
        return
    dialect.statement_compiler = type(f"JsonGuarded{base.__name__}", (JsonGuard, base), {})
