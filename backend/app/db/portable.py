"""应用库上 SQLite、PostgreSQL 写法不一样的那几样，收成按方言编译的表达式。

只给应用库（agentlab.db / AGENTLAB_DATABASE_URL）用；发给业务数据源的 SQL 不走这里。

**按 JSON 列里的键筛选。** SQLite 用 json_extract：取出来是 SQL 标量（字符串是文本、数字是数字、true 是 1、
没有这个键或值为 null 是 NULL），和 Python 值比是带类型的比较（数字 3 不等于字符串 "3"）。PostgreSQL 的 JSON 列是
存原文的 json（db/base.PortableJSON），用 -> / ->> 取，要比较的只把取出来的那一小段转成 JSONB：

- json_eq：PostgreSQL 上按 JSONB 相等比（`(col -> 'k')::jsonb = '3'::jsonb`），同样带类型，和 SQLite 一致；
- json_text：只要文本（字符串原样、数字是它的写法），PostgreSQL 上是 ->>；
- json_flag：标记位「真」（true、非零的数）：没有这个键、null、false、0 都算假；
- json_is_string：这个键的值是不是字符串（SQLite 的 json_type 叫 text，PostgreSQL 的 jsonb_typeof 叫 string）。

键名只收标识符（字母、数字、下划线），直接写进 SQL：键都是代码里的常量，写进去语句缓存也按它区分。

**插入，撞了唯一键就什么都不做。** insert_ignore 按会话连着的库挑 sqlite / postgresql 的 insert，两边的
on_conflict_do_nothing 是同一个接口。

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
    type = Boolean()
    inherit_cache = True
    name = "json_eq"

    def __init__(self, column: Any, key: str, value: Any) -> None:
        super().__init__(column, _key(key), bindparam(None, value, type_=_JsonScalar()))


class _JsonNull(FunctionElement[bool]):
    type = Boolean()
    inherit_cache = True
    name = "json_null"

    def __init__(self, column: Any, key: str) -> None:
        super().__init__(column, _key(key))


def json_eq(column: Any, key: str, value: Any) -> Any:
    """JSON 列顶层键的值等于 value（带类型的比较，见模块说明）。value 为 None 时是「没有这个键或值为 null」。

    None 用另一个表达式：语句缓存不按绑定参数的值区分，同一个表达式不能因为值是 None 编译成两样。"""
    return _JsonNull(column, key) if value is None else _JsonEq(column, key, value)


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
    col, key, _ = _parts(element, compiler, **kw)
    return f"json_extract({col}, '$.{key}')"


@compiles(_JsonEq)
def _json_eq_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    col, key, (value,) = _parts(element, compiler, **kw)
    return f"(json_extract({col}, '$.{key}') = {compiler.process(value, **kw)})"


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
    col, key, (value,) = _parts(element, compiler, **kw)
    return f"({_got(col, key)} = CAST({compiler.process(value, **kw)} AS JSONB))"


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
