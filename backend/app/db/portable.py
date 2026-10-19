"""应用库上 SQLite、PostgreSQL 写法不一样的那几样，收成按方言编译的表达式。

只给应用库（agentlab.db / AGENTLAB_DATABASE_URL）用；发给业务数据源的 SQL 不走这里。

**按 JSON 列里的键筛选。** 两种库同一套语义，按 JSON 的类型比（SQLite 的 json_extract 单独用时不分：true 取出来
是 1，和 1 比相等；PostgreSQL 的 JSON 列是存原文的 json，db/base.PortableJSON，比较时只把取出来的那一小段转成 JSONB）：

- json_eq(列, 键, 值)：字符串只等于同样的字符串；数只等于数（1 和 1.0 相等）；True / False 只等于 JSON 的 true / false
  （不等于 1 / 0）；None 是「没有这个键或值为 null」。只收这几种标量；
- json_text：这个键的值当文本——字符串是它本身，数、true / false、数组、对象是它的 JSON 写法（同 PostgreSQL 的 ->>，
  SQLite 上用 json_type 分开、非字符串取 -> 的写法），没有这个键或值为 null 是 NULL。所以数 1 的 json_text 是 '1'，
  两种库都是；
- json_flag：标记位「真」（true、非零的数）：没有这个键、null、false、0 都算假；
- json_is_string：这个键的值是不是字符串（SQLite 的 json_type 叫 text，PostgreSQL 的 jsonb_typeof 叫 string）。

键名只收标识符（字母、数字、下划线），直接写进 SQL：键都是代码里的常量，写进去语句缓存也按它区分。

**插入，撞了唯一键就什么都不做。** insert_ignore 按会话连着的库挑 sqlite / postgresql 的 insert，两边的
on_conflict_do_nothing 是同一个接口。

**不分大小写地找子串。** contains_ci：SQLite 上是 lower(列) LIKE lower(模式)（同 SQLAlchemy 的 ilike；大小写怎么折
看 SQLite 的构建，本机 conda 的 3.53 连 É/é、全角Ａ/ａ 都认）。PostgreSQL 上应用库的文本列是 COLLATE "C"（db/postgres.py），
列上的 ILIKE、lower() 只认 ASCII 的大小写；所以模式带 COLLATE "default"，按建库时的区域设置折大小写（en_US.UTF-8、
C.UTF-8 都认 Unicode；建库用的是纯 C 区域的话只认 ASCII）。用户输入里的 %、_、反斜杠按字面匹配。

**撞的是哪个唯一约束。** unique_violation 认 IntegrityError：SQLite 的报错是「UNIQUE constraint failed: 表.列」，
PostgreSQL（asyncpg）的是 23505，表名、DETAIL 里的「Key (列)=」分别带着。不靠约束名（命名规则改了也认得出）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import Boolean, String, bindparam, literal_column
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _key(name: str) -> Any:
    if not _KEY.match(name):
        raise ValueError(f"JSON 键名只能是标识符：{name!r}")
    return literal_column(name)


class _JsonScalar(TypeDecorator):
    """json_eq 右边的值：SQLite 原样绑定（和 json_extract 取出的标量比），PostgreSQL 绑定成 JSON 文本再转 JSONB。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return json.dumps(value, ensure_ascii=False)
        return value


class json_text(FunctionElement[str]):
    """JSON 列顶层键的值，当文本用。"""

    type = String()
    inherit_cache = True
    name = "json_text"

    def __init__(self, column: Any, key: str) -> None:
        super().__init__(column, _key(key))


class _JsonEq(FunctionElement[bool]):
    """kind 是 text / number / true / false，写进语句（语句缓存按它区分）；true / false 不用绑定参数。"""

    type = Boolean()
    inherit_cache = True
    name = "json_eq"

    def __init__(self, column: Any, key: str, kind: str, value: Any = None) -> None:
        extra = [] if kind in ("true", "false") else [bindparam(None, value, type_=_JsonScalar())]
        super().__init__(column, _key(key), literal_column(kind), *extra)


class _JsonNull(FunctionElement[bool]):
    type = Boolean()
    inherit_cache = True
    name = "json_null"

    def __init__(self, column: Any, key: str) -> None:
        super().__init__(column, _key(key))


def json_eq(column: Any, key: str, value: Any) -> Any:
    """JSON 列顶层键的值等于 value（按 JSON 的类型比，见模块说明）。value 为 None 时是「没有这个键或值为 null」。

    值的种类写进语句：语句缓存不按绑定参数的值区分，同一个表达式不能因为值的种类不同编译成两样。"""
    if value is None:
        return _JsonNull(column, key)
    if isinstance(value, bool):
        return _JsonEq(column, key, "true" if value else "false")
    if isinstance(value, (int, float)):
        return _JsonEq(column, key, "number", value)
    if isinstance(value, str):
        return _JsonEq(column, key, "text", value)
    raise TypeError(f"json_eq 只比标量（字符串、数、布尔、None），不收 {type(value).__name__}")


class json_flag(FunctionElement[bool]):
    """JSON 列顶层键是「真」的标记位：true 或非零的数（字符串也算真，同 SQLite）。没有这个键、null、false、0 是假。"""

    type = Boolean()
    inherit_cache = True
    name = "json_flag"

    def __init__(self, column: Any, key: str) -> None:
        super().__init__(column, _key(key))


class json_is_string(FunctionElement[bool]):
    """JSON 列顶层键的值是字符串。"""

    type = Boolean()
    inherit_cache = True
    name = "json_is_string"

    def __init__(self, column: Any, key: str) -> None:
        super().__init__(column, _key(key))


def _parts(element: Any, compiler: Any, **kw: Any) -> tuple[str, str, list[Any]]:
    column, key, *rest = list(element.clauses)
    return compiler.process(column, **kw), key.name, rest


# ---- SQLite（缺省）----


@compiles(json_text)
def _json_text_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    # 同 PostgreSQL 的 ->>：字符串取它本身，别的取 JSON 写法（-> 要 SQLite 3.38 以上）
    col, key, _ = _parts(element, compiler, **kw)
    path = f"'$.{key}'"
    return (f"(CASE json_type({col}, {path}) WHEN 'text' THEN json_extract({col}, {path}) WHEN 'null' THEN NULL "
            f"ELSE ({col} -> {path}) END)")


_SQLITE_KIND = {"text": "= 'text'", "number": "IN ('integer', 'real')", "true": "= 'true'", "false": "= 'false'"}


@compiles(_JsonEq)
def _json_eq_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, (kind, *value) = _parts(element, compiler, **kw)
    typed = f"json_type({col}, '$.{key}') {_SQLITE_KIND[kind.name]}"
    if not value:
        return f"({typed})"
    return f"({typed} AND json_extract({col}, '$.{key}') = {compiler.process(value[0], **kw)})"


@compiles(_JsonNull)
def _json_null_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"(json_extract({col}, '$.{key}') IS NULL)"


@compiles(json_flag)
def _json_flag_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"(coalesce(json_extract({col}, '$.{key}'), 0) != 0)"


@compiles(json_is_string)
def _json_is_string_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"(json_type({col}, '$.{key}') = 'text')"


# ---- PostgreSQL（应用库的 JSON 列是 json）----


def _got(col: str, key: str) -> str:
    """取出的那一段转成 JSONB：json 没有相等运算符。列本身是 JSONB 也照样成立。"""
    return f"CAST(({col} -> '{key}') AS JSONB)"


@compiles(json_text, "postgresql")
def _json_text_pg(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"({col} ->> '{key}')"


@compiles(_JsonEq, "postgresql")
def _json_eq_pg(element: Any, compiler: Any, **kw: Any) -> str:
    # JSONB 的相等本来就按类型：'1' = '1.0'，'1' ≠ 'true'，'"1"' ≠ '1'
    col, key, (kind, *value) = _parts(element, compiler, **kw)
    right = f"'{kind.name}'::jsonb" if not value else f"CAST({compiler.process(value[0], **kw)} AS JSONB)"
    return f"({_got(col, key)} = {right})"


@compiles(_JsonNull, "postgresql")
def _json_null_pg(element: Any, compiler: Any, **kw: Any) -> str:
    # json_extract 对「没有这个键」和「值为 null」都给 NULL；-> 对后者给的是 JSON 的 null
    col, key, _ = _parts(element, compiler, **kw)
    return f"(coalesce({_got(col, key)}, 'null'::jsonb) = 'null'::jsonb)"


@compiles(json_flag, "postgresql")
def _json_flag_pg(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"(coalesce({_got(col, key)}, 'null'::jsonb) NOT IN ('null'::jsonb, 'false'::jsonb, '0'::jsonb))"


@compiles(json_is_string, "postgresql")
def _json_is_string_pg(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, _ = _parts(element, compiler, **kw)
    return f"(jsonb_typeof({_got(col, key)}) = 'string')"


# ---- 不分大小写的子串 ----


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class _ContainsCI(FunctionElement[bool]):
    type = Boolean()
    inherit_cache = True
    name = "contains_ci"


def contains_ci(column: Any, text: str) -> Any:
    """列里含有 text，不分大小写（见模块说明）。"""
    return _ContainsCI(column, bindparam(None, f"%{_escape_like(text)}%", type_=String()))


@compiles(_ContainsCI)
def _contains_ci_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    column, pattern = list(element.clauses)
    return (f"(lower({compiler.process(column, **kw)}) LIKE lower({compiler.process(pattern, **kw)}) "
            "ESCAPE '\\')")


@compiles(_ContainsCI, "postgresql")
def _contains_ci_pg(element: Any, compiler: Any, **kw: Any) -> str:
    column, pattern = list(element.clauses)
    return (f"({compiler.process(column, **kw)} ILIKE ({compiler.process(pattern, **kw)} COLLATE \"default\") "
            "ESCAPE '\\')")


# ---- 插入，撞了唯一键就跳过 ----


def dialect_of(session: Any) -> str:
    """会话（AsyncSession / Session）连着的库的方言名。"""
    return session.get_bind().dialect.name


def insert_ignore(session: Any, table: Any) -> Any:
    """按会话连着的库挑方言的 insert，调用方接着 .values(...).on_conflict_do_nothing(index_elements=[...])。"""
    if dialect_of(session) == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


# ---- 撞的是哪个唯一约束 ----

_SQLITE_UNIQUE = "UNIQUE constraint failed:"
_PG_KEY = re.compile(r"Key \(([^)]*)\)=")


def _driver_error(error: BaseException) -> Any:
    """SQLAlchemy 的异常 → asyncpg 自己的异常（在 orig 的 __cause__ 上；orig 只抄了 sqlstate，没有表名和 DETAIL）。"""
    seen: Any = getattr(error, "orig", None) or error
    for _ in range(4):
        if getattr(seen, "sqlstate", None) and hasattr(seen, "table_name"):
            return seen
        seen = getattr(seen, "__cause__", None)
        if seen is None:
            break
    return None


def unique_violation(error: BaseException, table: str, *columns: str) -> bool:
    """这个 IntegrityError 是不是撞了 table 上正好这几列的唯一约束（主键、唯一索引都算）。"""
    text = str(getattr(error, "orig", None) or error)
    if _SQLITE_UNIQUE in text:
        failed = text.split(_SQLITE_UNIQUE, 1)[1].splitlines()[0]
        return {part.strip() for part in failed.split(",")} == {f"{table}.{c}" for c in columns}
    cause = _driver_error(error)
    if cause is None or getattr(cause, "sqlstate", None) != "23505" or getattr(cause, "table_name", None) != table:
        return False
    match = _PG_KEY.search(getattr(cause, "detail", None) or "")
    return bool(match) and [c.strip().strip('"') for c in match.group(1).split(",")] == list(columns)
