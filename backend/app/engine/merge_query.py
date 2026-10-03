"""合并查询（merge 节点）：几次查询的完整结果落进内存 SQLite，再用一条 SELECT / WITH 按键合并。

**为什么要有它。** 跨查询组合原来只有三种：口径卡用 cell() 组合几个单值；tool 节点的 SQL 里写 {{ vars.x }}，把前一条
查询的结果代进后一条；同一个库里写一条 JOIN / WITH。按行合并两次查询，尤其数据在两个库里时，没有路可走：用沙箱代码
合并，出处链断在代码节点上，喂给口径卡时只能提示「核对不了出处」；让模型把 id 从一条查询抄进下一条，会抄错，结果被
截断后还会漏。

**为什么不做跨库联邦查询。** 联邦查询要把两边的明细拉到一处，数据量、方言、权限一样都控制不住。方案是先在各自的库里
聚合到共同的键和粒度（日期 + 门店），再由这里在库外按键合并：两边进来的都是已经聚合过的小结果。

**为什么落成 SQLite、用标准库 sqlite3。** 照搬 data/tabular.py：SQLite 是现成的 SQL 引擎，守卫有它的读法（guard
的 _SQLITE），证据下钻的识别器（direct_select）也按它分词，不必为这一步引入 DuckDB 再多一种方言。连接同样关掉 DQS
（data.engine.open_memory_sqlite）：双引号里的列名写错要报错，不能当成字符串常量。

**守卫和三道防护。**
- SQL 先过守卫（SQLite 读法），只认 SELECT 和 WITH；表落完之后连接设成 query_only，再挂授权回调，只放行读表、
  调函数、递归公用表表达式。守卫是第一道，后两道不赌守卫没有漏洞；
- 结果的行数、字节上限和查询层相同（QueryLimits），超时按查询层的缺省时限由进度回调中断；
- 任一输入被截断 → 拒绝合并：拿只有前若干行的结果按键合并，缺的行不报错，只会悄悄少算；
- 合并键两边类型不一致（文本 '001' 对数值 1）→ 警告。SQLite 比较时按亲和性隐式转换，'001'、'01' 都会等于 1；
- 结果行数多于行数最多的输入 → 警告合并键可能不唯一（多对多匹配把行放大了）。

**逐格来历：宁可不下钻，也不下钻错。** 报告里引用合并结果的一格，证据面板要能指回它来自哪个输入的哪一格。只有一种
写法认得出来：单层 SELECT，FROM 后面是输入表（可加别名、可 JOIN），选取项是某张输入表的列（可改名）；没有分组、
聚合、DISTINCT、子查询、公用表表达式、集合运算、窗口和 NATURAL JOIN。认出来以后，在原 SQL 的选取列表末尾补上每张
输入表的 rowid 再执行一次：补列的结果去掉补的几列，必须和原结果逐行逐值相同，才把 rowid 记成来历；对不上整份不记。
另外两种情况也不给：结果里一模一样的两行（说不清哪一行来自哪一行输入），以及来历那一格和结果的值对不上的列。
表达式、聚合出来的列没有逐格来历，只有表级来历：来自哪几个输入、哪条合并 SQL。

纯函数：不连数据库、不读写工件、不发事件。取上游快照、存快照、发事件在执行器（engine/nodes/merge.py）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.data import guard
from app.data.engine import _jsonable, column_types, open_memory_sqlite
from app.data.guard import QueryLimits, SqlRejected
from app.data.names import name_key
from app.engine.direct_select import _kw, _lex, _Lex
from app.engine.expressions import same_value

#: 合并查询结果的查询快照里 source 记这个。数据源名只能是小写英文、数字和下划线（api/datasources.py 的
#: _NAME_PATTERN），永远撞不上它；证据面板、裁判摘录据此认出合并结果，不去按数据源名找遮罩
MERGE_SOURCE = "合并查询"
#: 一个合并节点最多几个输入。再多通常说明该在源库里先合并
MAX_INPUTS = 8
#: 别名（也就是内存库里的表名）最长几个字符
ALIAS_MAX = 32

_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: SQLite 的全部关键字（sqlite.org/lang_keywords.html），加上 true / false。用作表名要加引号才读得对，
#: 模型写合并 SQL 时多半不加——不如在起名时就挡住
_SQLITE_KEYWORDS = frozenset("""
abort action add after all alter always analyze and as asc attach autoincrement before begin between by cascade case
cast check collate column commit conflict constraint create cross current current_date current_time current_timestamp
database default deferrable deferred delete desc detach distinct do drop each else end escape except exclude exclusive
exists explain fail filter first following for foreign from full generated glob group groups having if ignore immediate
in index indexed initially inner insert instead intersect into is isnull join key last left like limit match
materialized natural no not nothing notnull null nulls of offset on or order others outer over partition plan pragma
preceding primary query raise range recursive references regexp reindex release rename replace restrict returning
right rollback row rows savepoint select set table temp temporary then ties to transaction trigger unbounded union
unique update using vacuum values view virtual when where window with without true false
""".split())

#: 快照里记的列类型 → 内存表的列亲和性。number 用 NUMERIC：数据源把 DECIMAL 落成的文本（"3200.50"）进来就是数，
#: 能直接参与运算；文本、日期一律 TEXT，'007' 的前导零不丢。拿不准的列不写类型，值原样存
_AFFINITY = {"number": "NUMERIC", "boolean": "INTEGER", "text": "TEXT", "date": "TEXT", "datetime": "TEXT",
             "time": "TEXT"}
_KIND_LABEL = {"number": "数值", "boolean": "布尔值", "text": "文本", "date": "日期", "datetime": "日期时间",
               "time": "时间"}
#: SQLite 表的 rowid 有三个名字；输入里恰好有同名的列时换下一个，三个都占了就不追逐格来历
_ROWID_NAMES = ("rowid", "_rowid_", "oid")
#: 补进选取列表的来历列。带引号写，和输入的列名撞不上（输入的列名里不会有这个前缀，撞了也只是对不上、不记来历）
_LINEAGE_COLUMN = "__agentlab_merge_row_{}"

SELECT_ONLY = "合并 SQL 只能是 SELECT 或 WITH 查询"
_TEMPLATE = re.compile(r"\{\{.*?\}\}", re.S)

#: 授权回调放行的动作：读表、调函数、递归公用表表达式。别的一律拒（ATTACH、PRAGMA、写）
_ALLOWED_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                              sqlite3.SQLITE_RECURSIVE})


class MergeError(ValueError):
    """合并做不成。消息直接给人看：哪个输入、为什么、怎么改。"""


@dataclass(frozen=True)
class MergeInput:
    """一个输入：别名（内存库里的表名）、上游节点、它的查询快照。"""

    alias: str
    node_id: str
    #: 上游查询快照的工件 id（tool 节点的 query_snapshot，或另一个合并节点的结果快照）
    artifact: str
    snapshot: dict[str, Any]
    #: 上游节点在画布上的名字，只用在报错里
    label: str = ""


@dataclass(frozen=True)
class Trace:
    """合并结果的一格来自哪个输入的哪一格（只走一跳；输入本身是合并结果时由调用方再追）。"""

    alias: str
    node_id: str | None
    artifact: str | None
    row: int
    column: str


@dataclass
class MergeOutcome:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    #: 守卫规范过的那一条 SQL，执行的就是它
    sql: str
    column_types: dict[str, str]
    warnings: list[dict[str, Any]]
    #: 逐格来历（见模块说明）；认不出写法、核对不上时为 None
    lineage: dict[str, Any] | None
    inputs: list[dict[str, Any]]
    #: 底层数据源（输入本身是合并结果的，展开成它的数据源），去重保序
    sources: list[str]
    mask_columns: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """存进工件库的查询快照：和数据源查询同形（columns、rows、row_count、truncated、sql、column_types、source），
        另记 inputs、sources、warnings、lineage。不含耗时之类每次不同的东西：同样的输入、同样的 SQL 得到同一个工件，
        节点重放、继续运行时快照 id 不变。"""
        out: dict[str, Any] = {
            "columns": self.columns, "rows": self.rows, "row_count": len(self.rows), "truncated": self.truncated,
            "sql": self.sql, "source": MERGE_SOURCE, "inputs": self.inputs, "sources": self.sources,
            "warnings": self.warnings,
        }
        if self.column_types:
            out["column_types"] = self.column_types
        if self.lineage is not None:
            out["lineage"] = self.lineage
        if self.mask_columns:
            out["mask_columns"] = self.mask_columns
        return out


# --------------------------------------------------------------------------
# 静态检查：别名、SQL（画图时校验用，执行时同一套规则）
# --------------------------------------------------------------------------


def alias_problem(alias: Any) -> str | None:
    """别名能不能当内存库里的表名。能用返回 None。"""
    if not isinstance(alias, str) or not alias:
        return "别名不能为空"
    if len(alias) > ALIAS_MAX:
        return f"别名「{alias[:ALIAS_MAX]}…」太长，最多 {ALIAS_MAX} 个字符"
    if not _ALIAS.match(alias):
        return f"别名「{alias}」不是合法的表名：只能用英文字母、数字和下划线，且不能以数字开头"
    if alias.lower() in _SQLITE_KEYWORDS:
        return f"别名「{alias}」是 SQL 关键字，用作表名必须加引号，容易写错。请换一个，例如 sales、visits"
    if alias.lower().startswith("sqlite_"):
        return f"别名「{alias}」以 sqlite_ 开头，这是 SQLite 保留给内部表的名字，请换一个"
    return None


def sql_problem(sql: Any) -> str | None:
    """合并 SQL 有什么问题，没问题返回 None。{{ }} 运行时才有值，先按一个空值代进去检查写法。"""
    if not isinstance(sql, str) or not sql.strip():
        return "还没有填写合并 SQL"
    try:
        _checked(_TEMPLATE.sub("NULL", sql))
    except MergeError as e:
        return str(e)
    return None


def _checked(sql: str) -> str:
    """守卫（SQLite 读法）规范过的那一条语句。只认 SELECT / WITH；守卫的原话写给数据源的，换成合并查询自己的说法。"""
    if not sql or not sql.strip():
        raise MergeError("还没有填写合并 SQL")
    statements = guard.split_statements(sql, dialect="sqlite")
    if len(statements) > 1:
        raise MergeError(f"合并 SQL 只能写一条语句，这里有 {len(statements)} 条。请把它们合成一条 SELECT（可以用 WITH）")
    try:
        stmt = guard.check(sql, readonly=True, dialect="sqlite")
    except SqlRejected as e:
        # 只读拒绝的原话是「请在数据页另建一个可写的数据源」，合并查询用不上；结构变更（ATTACH、DROP）同理
        if guard.is_write(sql, dialect="sqlite"):
            raise MergeError(f"{SELECT_ONLY}，不能写入、建表或连接别的库") from None
        raise MergeError(f"合并 SQL 无法执行：{e}") from None
    verb = guard.first_verb(stmt, dialect="sqlite")
    if verb not in ("select", "with"):
        raise MergeError(f"{SELECT_ONLY}，当前是 {verb.upper() or '无法识别的语句'}")
    return stmt


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def execute(inputs: list[MergeInput], sql: str, *, limits: QueryLimits | None = None) -> MergeOutcome:
    """把输入落进内存库，执行合并 SQL。做不成抛 MergeError（消息给人看）。"""
    limits = limits or QueryLimits()
    if not inputs:
        raise MergeError("还没有配置输入：至少选择一个上游的查询节点")
    if len(inputs) > MAX_INPUTS:
        raise MergeError(f"输入最多 {MAX_INPUTS} 个，这里有 {len(inputs)} 个。请先在源库里合并一部分")
    _check_aliases(inputs)
    tables = {inp.alias: _input_columns(inp) for inp in inputs}
    stmt = _checked(sql)

    conn = open_memory_sqlite()
    try:
        rowids = {inp.alias: _load(conn, inp, tables[inp.alias]) for inp in inputs}
        # 表落完就只读：授权回调之外的又一道（守卫、授权回调任何一处有漏洞，写也写不进去）
        conn.execute("PRAGMA query_only = 1")
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + float(limits.timeout_seconds)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        columns, rows, truncated = _read(conn, stmt, limits)
        plan = _lineage_plan(stmt, inputs, tables, rowids)
        lineage = _lineage(conn, plan, columns, rows, inputs, tables) if plan is not None else None
    except sqlite3.Error as e:
        raise MergeError(_sqlite_reason(e, limits)) from None
    finally:
        conn.close()

    warnings = _warnings(stmt, inputs, tables, rows, truncated, limits)
    return MergeOutcome(
        columns=columns, rows=rows, truncated=truncated, sql=stmt, column_types=column_types(columns, rows),
        warnings=warnings, lineage=lineage, inputs=[_summary(inp, tables[inp.alias]) for inp in inputs],
        sources=_sources(inputs), mask_columns=_masks(inputs, columns, plan),
    )


def _check_aliases(inputs: list[MergeInput]) -> None:
    seen: dict[str, str] = {}
    for inp in inputs:
        if problem := alias_problem(inp.alias):
            raise MergeError(problem)
        key = inp.alias.lower()
        if key in seen:
            raise MergeError(f"别名「{seen[key]}」和「{inp.alias}」只差大小写：SQLite 的表名不分大小写，会被当成同一张表。"
                             "请换一个别名")
        seen[key] = inp.alias


def _who(inp: MergeInput) -> str:
    return f"输入「{inp.alias}」" + (f"（{inp.label}）" if inp.label and inp.label != inp.alias else "")


def _input_columns(inp: MergeInput) -> list[str]:
    snapshot = inp.snapshot
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("columns"), list) \
            or not isinstance(snapshot.get("rows"), list):
        raise MergeError(f"{_who(inp)}不是查询结果：快照里没有列和行")
    if snapshot.get("truncated"):
        raise MergeError(
            f"{_who(inp)}的查询结果被截断，只拿到了前 {len(snapshot['rows'])} 行。拿不完整的结果按键合并，缺的行不会报错，"
            "只会让合并结果悄悄少算。请在源库里先聚合到合并需要的粒度（例如按日期和门店 GROUP BY），"
            "或者加条件缩小范围，让结果不超过查询上限")
    columns = [str(c) for c in snapshot["columns"]]
    if not columns:
        raise MergeError(f"{_who(inp)}的查询结果没有列")
    seen: dict[str, str] = {}
    for name in columns:
        key = name_key(name)
        if key in seen:
            raise MergeError(f"{_who(inp)}的结果里有重名的列「{name}」：SQLite 的列名不分大小写，同名的列放不进一张表。"
                             "请在源查询中用 AS 给它们起不同的名字")
        seen[key] = name
    return columns


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _kind(snapshot: dict[str, Any], column: str) -> str | None:
    """这一列的类型：查询时记下的（column_types）优先；老快照没记的按值猜，猜不准不说。"""
    types = snapshot.get("column_types")
    if isinstance(types, dict) and isinstance(types.get(column), str):
        return types[column]
    columns = [str(c) for c in snapshot.get("columns") or []]
    if column not in columns:
        return None
    at = columns.index(column)
    kinds = {_value_kind(row[at]) for row in snapshot.get("rows") or []
             if isinstance(row, list) and at < len(row) and row[at] is not None}
    return next(iter(kinds)) if len(kinds) == 1 and None not in kinds else None


def _value_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "text" if isinstance(value, str) else None


def _cell_in(value: Any) -> Any:
    """快照里的值 → 写进内存库的值。快照是 JSON：布尔写成 0 / 1，对象和数组写成 JSON 文本。"""
    if value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _load(conn: sqlite3.Connection, inp: MergeInput, columns: list[str]) -> str | None:
    """建表、按快照的顺序写入行，返回这张表的 rowid 用哪个名字（第 i 行的 rowid 是 i + 1）。"""
    snapshot = inp.snapshot
    decl = ", ".join(f"{_quote(c)} {_AFFINITY.get(_kind(snapshot, c) or '', '')}".rstrip() for c in columns)
    conn.execute(f"CREATE TABLE {_quote(inp.alias)} ({decl})")
    taken = {c.lower() for c in columns}
    rowid = next((n for n in _ROWID_NAMES if n not in taken), None)
    width = len(columns)
    rows: list[list[Any]] = []
    for i, row in enumerate(snapshot["rows"]):
        if isinstance(row, dict):
            row = [row.get(c) for c in columns]
        if not isinstance(row, list) or len(row) != width:
            raise MergeError(f"{_who(inp)}的快照第 {i} 行和列数对不上，快照可能已损坏")
        rows.append([i + 1, *map(_cell_in, row)] if rowid else [_cell_in(v) for v in row])
    names = ", ".join([*([rowid] if rowid else []), *map(_quote, columns)])
    marks = ", ".join("?" * (width + (1 if rowid else 0)))
    conn.executemany(f"INSERT INTO {_quote(inp.alias)} ({names}) VALUES ({marks})", rows)
    return rowid


def _authorize(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _read(conn: sqlite3.Connection, stmt: str, limits: QueryLimits) -> tuple[list[str], list[list[Any]], bool]:
    """逐行读、边读边判上限：和查询层（data.engine.run_query）同一个口径，到了行数或字节上限就停，记 truncated。"""
    cursor = conn.execute(stmt)
    columns = [str(d[0]) for d in cursor.description or []]
    if not columns:
        raise MergeError("合并 SQL 没有返回任何列")
    rows: list[list[Any]] = []
    size = 0
    truncated = False
    for row in cursor:
        values = [_jsonable(v) for v in row]
        rows.append(values)
        size += len(json.dumps(values, ensure_ascii=False, default=str))
        if len(rows) >= limits.max_rows or size >= limits.max_bytes:
            truncated = True
            break
    return columns, rows, truncated


def _sqlite_reason(e: sqlite3.Error, limits: QueryLimits) -> str:
    text = str(e)
    if "interrupted" in text:
        seconds = float(limits.timeout_seconds)
        shown = f"{seconds:g}"
        return f"合并超过 {shown} 秒被中断。请在源查询里先聚合，或在合并 SQL 中加条件缩小范围"
    if "not authorized" in text or "readonly" in text:
        return f"{SELECT_ONLY}，只能读取输入表"
    # SQLite 的原话（no such column: x、ambiguous column name: 日期）正是改 SQL 要的线索，原样带上
    return f"合并 SQL 执行失败：{text}"


def _summary(inp: MergeInput, columns: list[str]) -> dict[str, Any]:
    source = inp.snapshot.get("source")
    return {"alias": inp.alias, "node_id": inp.node_id, "artifact": inp.artifact, "rows": len(inp.snapshot["rows"]),
            "columns": columns, "source": str(source) if source else None}


def _sources(inputs: list[MergeInput]) -> list[str]:
    out: list[str] = []
    for inp in inputs:
        nested = inp.snapshot.get("sources")
        names = nested if inp.snapshot.get("source") == MERGE_SOURCE and isinstance(nested, list) \
            else [inp.snapshot.get("source")]
        out.extend(str(n) for n in names if n and str(n) not in out)
    return out


# --------------------------------------------------------------------------
# 逐格来历
# --------------------------------------------------------------------------

#: 整条语句里出现就不追来历的词：子查询、集合运算、公用表表达式、分组、窗口、去重、NATURAL JOIN
_NO_LINEAGE = frozenset({"select", "union", "intersect", "except", "with", "recursive", "group", "having", "window",
                         "over", "distinct", "values", "natural", "filter"})
#: 聚合函数（名字后面紧跟「(」）。没有 GROUP BY 的聚合把整张表压成一行，选取项里的裸列取的是哪一行说不准
_AGGREGATES = frozenset({"count", "sum", "avg", "min", "max", "total", "group_concat", "string_agg", "median",
                         "json_group_array", "json_group_object", "jsonb_group_array", "jsonb_group_object"})
#: 不加引号的字面量关键字：选取项写 NULL、CURRENT_DATE 不是列
_LITERALS = frozenset({"null", "true", "false", "current_date", "current_time", "current_timestamp"})
#: 来源表后面紧跟这些词时，它们不是省略了 AS 的表别名
_AFTER_SOURCE = frozenset({"on", "using", "where", "order", "limit", "join", "left", "right", "full", "inner", "cross",
                           "outer", "indexed", "not", "natural", "group", "having", "window", "union", "intersect",
                           "except", "as"})
_JOIN_WORDS = frozenset({"join", "left", "right", "full", "inner", "cross", "natural"})
_TAIL = frozenset({"where", "order", "limit"})


@dataclass(frozen=True)
class _Source:
    #: 输入别名（内存库里的表名）
    alias: str
    #: SQL 里怎么称呼它：写了表别名就是表别名，否则是表名本身
    ref: str


@dataclass(frozen=True)
class _Plan:
    sources: list[_Source]
    #: 第 i 个结果列：(第几个来源, 输入里的列名)；表达式、说不准的列是 None
    columns: list[tuple[int, str] | None]
    #: 在原 SQL 的哪个位置补来历列（顶层 FROM 前面）
    insert_at: int
    stmt: str
    rowids: list[str]


def _punct(lex: _Lex | None, text: str) -> bool:
    return lex is not None and lex.kind == "punct" and lex.text == text


def _named(lex: _Lex | None) -> bool:
    return lex is not None and lex.kind in ("word", "name")


def _find(names: list[str], wanted: str) -> str | None:
    key = name_key(wanted)
    return next((n for n in names if name_key(n) == key), None)


def _lineage_plan(stmt: str, inputs: list[MergeInput], tables: dict[str, list[str]],
                  rowids: dict[str, str | None]) -> _Plan | None:
    """认出「单层 SELECT、选取项是输入表的列」这种写法；认不出返回 None（整份不记来历）。"""
    toks = _lex(stmt)
    if not toks or _kw(toks[0]) != "select":
        return None
    if any(_kw(t) in _NO_LINEAGE for t in toks[1:]):
        return None
    if any(_kw(t) in _AGGREGATES and _punct(nxt, "(") for t, nxt in zip(toks, toks[1:])):
        return None
    depth, from_at = 0, None
    for i, t in enumerate(toks):
        if _punct(t, "("):
            depth += 1
        elif _punct(t, ")"):
            depth -= 1
        elif depth == 0 and _kw(t) == "from":
            from_at = i
            break
    if from_at is None:
        return None
    start = 2 if _kw(toks[1]) == "all" else 1
    parsed = _from_clause(toks, from_at + 1, tables)
    if parsed is None:
        return None
    sources, using = parsed
    columns: list[tuple[int, str] | None] = []
    for item in _split_items(toks[start:from_at]):
        mapped = _item(item, sources, tables, using)
        if mapped is None:
            return None
        columns.extend(mapped)
    if any(rowids.get(s.alias) is None for s in sources):
        return None
    frm = toks[from_at]
    return _Plan(sources=sources, columns=columns, insert_at=frm.end - len(frm.text), stmt=stmt,
                 rowids=[str(rowids[s.alias]) for s in sources])


def _split_items(toks: list[_Lex]) -> list[list[_Lex]]:
    items: list[list[_Lex]] = [[]]
    depth = 0
    for t in toks:
        if _punct(t, "("):
            depth += 1
        elif _punct(t, ")"):
            depth -= 1
        if depth == 0 and _punct(t, ","):
            items.append([])
        else:
            items[-1].append(t)
    return items


def _from_clause(toks: list[_Lex], i: int, tables: dict[str, list[str]]) -> tuple[list[_Source], set[str]] | None:
    """FROM 之后：来源表 [AS 别名]，用逗号或 JOIN 连接，JOIN 后面可跟 ON 条件或 USING (列…)；之后只许
    WHERE / ORDER BY / LIMIT。来源只能是输入表（不认子查询、表函数、带库名的表）。"""
    sources: list[_Source] = []
    using: set[str] = set()
    n = len(toks)
    while True:
        t = toks[i] if i < n else None
        if not _named(t) or (t.kind == "word" and name_key(t.text) in _SQLITE_KEYWORDS):
            return None
        table = _find(list(tables), t.text)
        if table is None or _punct(toks[i + 1] if i + 1 < n else None, ".") \
                or _punct(toks[i + 1] if i + 1 < n else None, "("):
            return None
        ref, i = t.text, i + 1
        nxt = toks[i] if i < n else None
        if _kw(nxt) == "as":
            if not _named(toks[i + 1] if i + 1 < n else None):
                return None
            ref, i = toks[i + 1].text, i + 2
        elif _named(nxt) and (nxt.kind == "name" or name_key(nxt.text) not in _AFTER_SOURCE):
            ref, i = nxt.text, i + 1
        sources.append(_Source(alias=table, ref=ref))
        word = _kw(toks[i]) if i < n else None
        if word == "on":
            depth, i = 0, i + 1
            while i < n:
                t = toks[i]
                if _punct(t, "("):
                    depth += 1
                elif _punct(t, ")"):
                    depth -= 1
                elif depth == 0 and (_punct(t, ",") or _kw(t) in _JOIN_WORDS or _kw(t) in _TAIL):
                    break
                i += 1
        elif word == "using":
            if not _punct(toks[i + 1] if i + 1 < n else None, "("):
                return None
            i += 2
            while i < n and not _punct(toks[i], ")"):
                if _named(toks[i]):
                    using.add(name_key(toks[i].text))
                elif not _punct(toks[i], ","):
                    return None
                i += 1
            i += 1
        if i >= n:
            return sources, using
        t = toks[i]
        if _punct(t, ","):
            i += 1
            continue
        word = _kw(t)
        if word in _TAIL:
            return sources, using
        if word not in ("join", "left", "right", "full", "inner", "cross"):
            return None
        while i < n and _kw(toks[i]) in ("left", "right", "full", "inner", "cross", "outer"):
            i += 1
        if i >= n or _kw(toks[i]) != "join":
            return None
        i += 1


def _source_of(ref: str, sources: list[_Source]) -> int | None:
    key = name_key(ref)
    hits = [k for k, s in enumerate(sources) if name_key(s.ref) == key]
    return hits[0] if len(hits) == 1 else None


def _item(item: list[_Lex], sources: list[_Source], tables: dict[str, list[str]],
          using: set[str]) -> list[tuple[int, str] | None] | None:
    """一个选取项展开成几个结果列的来历。返回 None 表示整份不追（* 遇上 USING：SQLite 只出一份合并掉的列）。"""
    if len(item) == 1 and _punct(item[0], "*"):
        if using:
            return None
        return [(k, c) for k, s in enumerate(sources) for c in tables[s.alias]]
    if len(item) == 3 and _named(item[0]) and _punct(item[1], ".") and _punct(item[2], "*"):
        k = _source_of(item[0].text, sources)
        return None if k is None else [(k, c) for c in tables[sources[k].alias]]
    body = item
    if len(item) >= 3 and _kw(item[-2]) == "as" and _named(item[-1]):
        body = item[:-2]
    elif len(item) >= 2 and _named(item[-1]) and _named(item[-2]) \
            and not (item[-1].kind == "word" and name_key(item[-1].text) in _SQLITE_KEYWORDS):
        body = item[:-1]
    if len(body) == 1 and _named(body[0]):
        word = body[0]
        if word.kind == "word" and name_key(word.text) in _LITERALS:
            return [None]
        owners = [(k, col) for k, s in enumerate(sources) if (col := _find(tables[s.alias], word.text)) is not None]
        if len(owners) != 1 or name_key(word.text) in using:
            return [None]
        return [owners[0]]
    if len(body) == 3 and _named(body[0]) and _punct(body[1], ".") and _named(body[2]):
        k = _source_of(body[0].text, sources)
        col = _find(tables[sources[k].alias], body[2].text) if k is not None else None
        return [(k, col) if k is not None and col is not None else None]
    return [None]


def _hashable(row: list[Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def _lineage(conn: sqlite3.Connection, plan: _Plan, columns: list[str], rows: list[list[Any]],
             inputs: list[MergeInput], tables: dict[str, list[str]]) -> dict[str, Any] | None:
    """补上 rowid 再执行一次，逐行逐值核对，得出每一格来自哪个输入的哪一行。核对不上返回 None。"""
    width = len(columns)
    if len(plan.columns) != width or not any(plan.columns):
        return None
    extra = ", ".join(f"{_quote(s.ref)}.{rowid} AS {_quote(_LINEAGE_COLUMN.format(k))}"
                      for k, (s, rowid) in enumerate(zip(plan.sources, plan.rowids)))
    head = plan.stmt[:plan.insert_at].rstrip()
    try:
        augmented = _checked(f"{head}, {extra} {plan.stmt[plan.insert_at:]}")
        cursor = conn.execute(augmented)
    except (MergeError, sqlite3.Error):
        return None
    got: list[tuple[Any, ...]] = []
    for row in cursor:
        if len(got) >= len(rows):
            break
        got.append(row)
    if len(got) != len(rows) or any(len(g) != width + len(plan.sources) for g in got):
        return None
    if any([_jsonable(v) for v in g[:width]] != r for g, r in zip(got, rows)):
        return None
    origin: list[list[int | None]] = [[v - 1 if isinstance(v, int) and v >= 1 else None for v in g[width:]]
                                      for g in got]
    # 一模一样的两行结果：说不清哪一行来自哪一行输入
    counts = Counter(_hashable(r) for r in rows)
    for i, r in enumerate(rows):
        if counts[_hashable(r)] > 1:
            origin[i] = [None] * len(plan.sources)
    snaps = {inp.alias: inp.snapshot for inp in inputs}
    mapped: list[dict[str, Any] | None] = []
    for j, target in enumerate(plan.columns):
        if target is None:
            mapped.append(None)
            continue
        k, column = target
        snap = snaps[plan.sources[k].alias]
        at = tables[plan.sources[k].alias].index(column)
        ok = True
        for i, row in enumerate(rows):
            src = origin[i][k]
            if src is None:
                continue
            if not (0 <= src < len(snap["rows"])) or not _same(row[j], _row(snap["rows"][src], tables[plan.sources[k].alias])[at]):
                ok = False
                break
        mapped.append({"source": k, "column": column} if ok else None)
    if not any(mapped):
        return None
    return {"sources": [{"input": s.alias, "ref": s.ref} for s in plan.sources], "columns": mapped, "rows": origin}


def _row(row: Any, columns: list[str]) -> list[Any]:
    return [row.get(c) for c in columns] if isinstance(row, dict) else row


def _same(merged: Any, original: Any) -> bool:
    if merged is None or original is None:
        return merged is None and original is None
    return same_value(merged, _cell_in(original))


def trace_cell(snapshot: Any, row: Any, column: Any) -> Trace | None:
    """合并结果的这一格来自哪个输入的哪一格；没有逐格来历返回 None。列按第一次出现的位置认（同 locate_cell）。"""
    if not isinstance(snapshot, dict) or snapshot.get("source") != MERGE_SOURCE:
        return None
    lineage, columns = snapshot.get("lineage"), snapshot.get("columns")
    if not isinstance(lineage, dict) or not isinstance(columns, list) or column not in columns:
        return None
    if isinstance(row, bool) or not isinstance(row, int):
        return None
    mapped, origin, sources = lineage.get("columns"), lineage.get("rows"), lineage.get("sources")
    at = columns.index(column)
    if not (isinstance(mapped, list) and isinstance(origin, list) and isinstance(sources, list)
            and at < len(mapped) and 0 <= row < len(origin)):
        return None
    target = mapped[at]
    if not isinstance(target, dict) or not isinstance(target.get("source"), int):
        return None
    k = target["source"]
    line = origin[row]
    if not isinstance(line, list) or not 0 <= k < len(line) or not 0 <= k < len(sources):
        return None
    src = line[k]
    alias = (sources[k] or {}).get("input") if isinstance(sources[k], dict) else None
    if isinstance(src, bool) or not isinstance(src, int) or not isinstance(alias, str):
        return None
    entry = next((e for e in snapshot.get("inputs") or [] if isinstance(e, dict) and e.get("alias") == alias), None)
    if entry is None:
        return None
    return Trace(alias=alias, node_id=entry.get("node_id"), artifact=entry.get("artifact"), row=src,
                 column=str(target.get("column")))


def _masks(inputs: list[MergeInput], columns: list[str], plan: _Plan | None) -> list[str]:
    """合并结果要遮罩的列：各输入当时记下的遮罩列，加上来自遮罩列、换了名字的结果列（宁可多遮）。"""
    hidden: dict[str, set[str]] = {}
    names: list[str] = []
    for inp in inputs:
        recorded = inp.snapshot.get("mask_columns")
        cols = {str(c).lower() for c in recorded} if isinstance(recorded, list) else set()
        hidden[inp.alias] = cols
        names.extend(str(c) for c in recorded or [] if isinstance(c, str))
    if plan is not None and len(plan.columns) == len(columns):
        for name, target in zip(columns, plan.columns):
            if target is not None and target[1].lower() in hidden.get(plan.sources[target[0]].alias, set()):
                names.append(name)
    return sorted(dict.fromkeys(names))


# --------------------------------------------------------------------------
# 警告：合并键类型、行数放大、结果截断
# --------------------------------------------------------------------------


def _key_pairs(stmt: str, tables: dict[str, list[str]]) -> list[tuple[str, str, str, str]]:
    """合并条件里的键：ON / WHERE 里「列 = 列」两边落在不同的输入表上，以及 USING (列)。只用来提示，认不全不要紧。"""
    toks = _lex(stmt, strict=False) or []
    n = len(toks)
    refs: dict[str, str] = {}
    pairs: list[tuple[str, str, str, str]] = []
    placed: list[str] = []          # 当前 FROM 列表里已经出现过的输入表（USING 找左边那张）
    i = 0
    while i < n:
        word = _kw(toks[i])
        if word in ("from", "join") or (_punct(toks[i], ",") and placed and i > 0 and _named(toks[i - 1])):
            if word == "from":
                placed = []
            j = i + 1
            t = toks[j] if j < n else None
            table = _find(list(tables), t.text) if _named(t) else None
            if table is not None and not _punct(toks[j + 1] if j + 1 < n else None, "."):
                ref, j = t.text, j + 1
                nxt = toks[j] if j < n else None
                if _kw(nxt) == "as" and _named(toks[j + 1] if j + 1 < n else None):
                    ref, j = toks[j + 1].text, j + 2
                elif _named(nxt) and (nxt.kind == "name" or name_key(nxt.text) not in _AFTER_SOURCE):
                    ref, j = nxt.text, j + 1
                refs[name_key(ref)] = table
                refs.setdefault(name_key(table), table)
                if _kw(toks[j] if j < n else None) == "using" and _punct(toks[j + 1] if j + 1 < n else None, "("):
                    k = j + 2
                    while k < n and not _punct(toks[k], ")"):
                        if _named(toks[k]):
                            col = _find(tables[table], toks[k].text)
                            left = next((p for p in reversed(placed) if _find(tables[p], toks[k].text)), None)
                            if col is not None and left is not None:
                                pairs.append((left, str(_find(tables[left], toks[k].text)), table, col))
                        k += 1
                placed.append(table)
                i = j
                continue
        i += 1

    def resolve(at: int, step: int) -> tuple[tuple[str, str] | None, int]:
        """从 at 开始（step=1 往右、-1 往左）读一个列引用：(表, 列)、它占了几个记号。"""
        if step > 0:
            seq = toks[at:at + 3]
        else:
            seq = toks[max(0, at - 2):at + 1]
        if len(seq) == 3 and _named(seq[0]) and _punct(seq[1], ".") and _named(seq[2]):
            table = refs.get(name_key(seq[0].text))
            col = _find(tables[table], seq[2].text) if table else None
            return ((table, col) if table and col else None), 3
        one = toks[at] if 0 <= at < n else None
        if _named(one):
            owners = [(t, c) for t in tables if (c := _find(tables[t], one.text)) is not None]
            return (owners[0] if len(owners) == 1 else None), 1
        return None, 1

    for i, t in enumerate(toks):
        if not _punct(t, "="):
            continue
        prev = toks[i - 1] if i > 0 else None
        # 「==」的第二个等号、<=、>=、!= 都不是比较的开头（分词把每个标点切成一个记号）
        if prev is None or (prev.kind == "punct" and prev.text in "=<>!"):
            continue
        right_at = i + 2 if _punct(toks[i + 1] if i + 1 < n else None, "=") else i + 1
        left, width = resolve(i - 1, -1)
        before = toks[i - 1 - width] if i - 1 - width >= 0 else None
        if before is not None and _punct(before, "."):
            left = None             # a.b.c 这种带库名的写法：不是输入表的列
        right, rwidth = resolve(right_at, 1)
        after = toks[right_at + rwidth] if right_at + rwidth < n else None
        if after is not None and (_punct(after, ".") or _punct(after, "(")):
            right = None
        if left and right and left[0] != right[0]:
            pairs.append((left[0], left[1], right[0], right[1]))
    seen: set[frozenset[tuple[str, str]]] = set()
    out: list[tuple[str, str, str, str]] = []
    for a, ac, b, bc in pairs:
        key = frozenset({(a, ac), (b, bc)})
        if key not in seen:
            seen.add(key)
            out.append((a, ac, b, bc))
    return out


def _example(snapshot: dict[str, Any], column: str) -> str | None:
    columns = [str(c) for c in snapshot.get("columns") or []]
    if column not in columns:
        return None
    at = columns.index(column)
    for row in snapshot.get("rows") or []:
        value = row[at] if isinstance(row, list) and at < len(row) else None
        if value is not None:
            return f"'{value}'" if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return None


def _clash(a: str | None, b: str | None) -> bool:
    """两边的类型放在一起比会不会出错：数值对文本类，日期对日期时间（'2026-05-01' 永远不等于 '2026-05-01T00:00:00'）。"""
    if not a or not b or a == b:
        return False
    numeric = {"number", "boolean"}
    if (a in numeric) != (b in numeric):
        return True
    return {a, b} == {"date", "datetime"}


def _warnings(stmt: str, inputs: list[MergeInput], tables: dict[str, list[str]], rows: list[list[Any]],
              truncated: bool, limits: QueryLimits) -> list[dict[str, Any]]:
    snaps = {inp.alias: inp.snapshot for inp in inputs}
    pairs = _key_pairs(stmt, tables)
    out: list[dict[str, Any]] = []
    for a, ac, b, bc in pairs:
        ka, kb = _kind(snaps[a], ac), _kind(snaps[b], bc)
        if not _clash(ka, kb):
            continue
        ea, eb = _example(snaps[a], ac), _example(snaps[b], bc)
        left = f"{a}.{ac} 是{_KIND_LABEL.get(str(ka), ka)}" + (f"（例如 {ea}）" if ea else "")
        right = f"{b}.{bc} 是{_KIND_LABEL.get(str(kb), kb)}" + (f"（例如 {eb}）" if eb else "")
        if {ka, kb} == {"date", "datetime"}:
            tail = ("日期和日期时间按文本比较永远不相等，这组键可能一行都匹配不上。请在源查询中统一格式，"
                    "或在合并 SQL 中用 date() 转换后再比较")
        else:
            tail = ("SQLite 比较时会做隐式转换：文本形式的编号（如 '001'、'01'）都会等于数值 1，也可能完全匹配不上。"
                    "请在源查询中统一类型，或在合并 SQL 中用 CAST 明确转换")
        out.append({"code": "key_type_mismatch", "keys": [f"{a}.{ac}", f"{b}.{bc}"],
                    "message": f"合并键类型不一致：{left}，{right}。{tail}"})
    largest = max(inputs, key=lambda inp: len(inp.snapshot["rows"]))
    biggest = len(largest.snapshot["rows"])
    if len(rows) > biggest:
        keys: dict[str, list[str]] = {}
        for a, ac, b, bc in pairs:
            for alias, col in ((a, ac), (b, bc)):
                if col not in keys.setdefault(alias, []):
                    keys[alias].append(col)
        dups = [_duplicates(alias, snaps[alias], cols) for alias, cols in keys.items()]
        detail = "；".join(d for d in dups if d)
        out.append({"code": "rows_grew", "message": (
            f"合并结果有 {len(rows)} 行，多于行数最多的输入「{largest.alias}」（{biggest} 行）：合并键可能不唯一，"
            "同一行被重复匹配。" + (f"{detail}。" if detail else "")
            + "请检查合并条件是否覆盖了全部键（例如同时按日期和门店），或先在源库里聚合到相同粒度")})
    if truncated:
        out.append({"code": "result_truncated", "message": (
            f"合并结果超过上限（{limits.max_rows} 行或约 {max(1, limits.max_bytes // 1_000_000)} MB），只保留了前 "
            f"{len(rows)} 行，下游拿到的不是完整结果。请在合并 SQL 中聚合或加条件缩小范围")})
    return out


def _duplicates(alias: str, snapshot: dict[str, Any], cols: list[str]) -> str | None:
    """这个输入按合并键有没有重复：有的话举第一组，说一共几组。"""
    columns = [str(c) for c in snapshot.get("columns") or []]
    if not cols or any(c not in columns for c in cols):
        return None
    at = [columns.index(c) for c in cols]
    counts = Counter(tuple(_hashable([row[j]]) for j in at) for row in snapshot.get("rows") or []
                     if isinstance(row, list))
    repeated = [(key, n) for key, n in counts.items() if n > 1]
    if not repeated:
        return None
    key, n = repeated[0]
    shown = " / ".join(str(json.loads(v)[0]) for v in key)
    more = f"等 {len(repeated)} 组" if len(repeated) > 1 else ""
    return f"「{alias}」中按（{'、'.join(cols)}）有重复的键，例如 {shown} 出现 {n} 次{more}"
