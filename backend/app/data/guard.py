"""SQL 安全守卫。

这是数据源接入里唯一不能出错的地方：SQL 是 Copilot 或 agent 生成的，不是人写的。
模型会写出什么，事前无法穷举——所以防线不能建立在"模型应该不会这么干"上。

三层，从外到内：

1. **只读判定**：只读数据源只放行确定无副作用的语句。用白名单而不是黑名单——
   黑名单永远漏，`TRUNCATE`、`MERGE`、`CALL`、`GRANT`、各家方言的私货写不完。
2. **多语句拦截**：`SELECT 1; DROP TABLE users` 这种拼接必须在到达驱动之前就断掉。
   DBAPI 层的 `execute` 多数驱动只跑第一条，但"多数"不是"全部"，MySQL 开了
   `CLIENT_MULTI_STATEMENTS` 就会全跑。不赌驱动的默认值。
3. **结果集上限**：行数 + 字节 + 超时。一条 `SELECT * FROM 亿级表` 不该把后端打爆，
   也不该把几百 MB 塞进模型的上下文。

刻意不引入 sqlparse 之类的重型依赖：完整解析 SQL 方言是个无底洞，而这里需要的
判断很窄——"第一个关键字是什么"和"有没有第二条语句"。窄问题用窄解法，
把复杂度留在能看懂的范围内。

所有判断都建立在同一次扫描上（_tokens）：一遍从左到右，同时认注释、字符串、标识符引号和
分号。以前先用正则去注释、再认字符串，字符串里的 `--`、`/*` 被当成注释删掉，执行的 SQL
和写的不一样。扫描要和数据库的读法一致，而读法跟方言、跟服务器设置走（MySQL 的
NO_BACKSLASH_ESCAPES / ANSI_QUOTES，PG 的 standard_conforming_strings）。守卫不知道
服务器开了哪个，就按每种可能的读法各扫一遍，读出来不一致的直接拒掉。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 只读数据源放行的首关键字。只有确定不产生副作用的才进这个名单。
# 注意 SHOW/DESCRIBE/EXPLAIN 要留着：agent 探查表结构靠它们。
_READONLY_VERBS = frozenset({
    "select", "with", "show", "describe", "desc", "explain", "pragma", "values",
})

# 即便在可写数据源上也一律拒绝的语句。这类操作的破坏是不可逆的，
# 不该由一个实验工具里的模型来发起——要做请人去数据库客户端里做。
_ALWAYS_DENIED = frozenset({
    "drop", "truncate", "grant", "revoke", "alter", "create", "shutdown",
    "attach", "detach",
})

# 首关键字是查询、正文却能写的语句。只看首关键字的话它们全都能混过去：
#   WITH k AS (SELECT 1) DELETE FROM t        SQLite / MySQL / PG 都认
#   WITH x AS (DELETE … RETURNING *) SELECT … PG 的数据修改 CTE
#   SELECT … INTO backup / INTO OUTFILE '…'   PG 建表 / MySQL 写服务器文件
#   EXPLAIN ANALYZE DELETE …                  PG 的 ANALYZE 会真的执行
# 关键字后面紧跟 ( 的不算：那是同名函数（MySQL 的 INSERT()、TRUNCATE()）。
_HIDDEN_WRITE = re.compile(
    r"\b(insert|update|delete|merge|upsert|truncate|drop|alter|create|grant|revoke|"
    r"call|copy|into|attach|detach|vacuum|reindex|lock\s+tables?|replace\s+into)\b(?!\s*\()",
    re.I,
)

# 查询里一调用就有副作用的函数。函数没法白名单，这里只收一小撮要命的：
# set_config 能把 PG 连接层的只读关掉（真库实测：关掉后同一连接的下一个事务
# 就能写），dblink 另开一条不受只读约束的连接，nextval/setval 推进序列，其余是
# 杀连接、读写服务器文件、加载本地扩展。函数名可以加引号写（"set_config"(…)），
# 所以名字两边允许引号，扫描时也只抹字符串、不抹标识符。
_SIDE_EFFECT_CALL = re.compile(
    r"[\"`]?\b(set_config|nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
    r"lo_import|lo_export|lo_unlink|dblink\w*|load_extension)\b[\"`]?\s*\(",
    re.I,
)

# SQLite 的 PRAGMA 有读有写，而且写有两种写法：`= 值` 和 `(值)`。
# 只放行确定只读的：前一组的参数是表名/索引名，后一组不带参数。
_PRAGMA_READ_WITH_ARG = frozenset({
    "table_info", "table_xinfo", "table_list", "index_list", "index_info",
    "index_xinfo", "foreign_key_list",
})
_PRAGMA_READ_NO_ARG = frozenset({
    "table_list", "database_list", "collation_list", "function_list", "module_list",
    "pragma_list", "compile_options", "encoding", "page_count", "page_size",
    "freelist_count", "schema_version", "user_version", "application_id", "data_version",
})
_PRAGMA = re.compile(r"pragma\s+(?:\w+\s*\.\s*)?(\w+)\s*(.*)$", re.I | re.S)


class SqlRejected(ValueError):
    """SQL 没通过守卫。消息直接给模型看，所以要说清楚为什么被拒、该怎么改。"""


@dataclass(frozen=True)
class QueryLimits:
    max_rows: int = 1000
    max_bytes: int = 1_000_000
    #: 数据源可以在 options.query_timeout_s 里自己定（见 data.engine.query_timeout），可以带小数
    timeout_seconds: float = 30


@dataclass(frozen=True)
class _Reading:
    """数据库怎么切分一段 SQL：哪些字符开始注释、字符串、带引号的标识符。"""

    name: str
    #: '…' 里的反斜杠转义下一个字符（MySQL 默认；PG 关了 standard_conforming_strings 时）
    backslash: bool = False
    #: "…" 是字符串而不是标识符（MySQL 没开 ANSI_QUOTES 时），跟 '…' 一样认反斜杠
    dq_string: bool = False
    #: `…` 是标识符（MySQL、SQLite）
    backtick: bool = False
    #: […] 是标识符（SQLite）
    brackets: bool = False
    #: $$…$$、$tag$…$tag$ 是字符串（PostgreSQL）
    dollar: bool = False
    #: E'…' 里认反斜杠转义（PostgreSQL）
    e_strings: bool = False
    #: /* /* */ */ 可以嵌套（PostgreSQL）
    nested: bool = False
    #: # 开始行注释（MySQL）
    hash_comment: bool = False
    #: q'[…]' 这类自选定界符的字符串（Oracle）
    q_quote: bool = False


_GENERIC = _Reading("generic", backtick=True)
_SQLITE = _Reading("sqlite", backtick=True, brackets=True)
_PG = _Reading("postgresql", dollar=True, e_strings=True, nested=True)
_PG_OLD = _Reading("postgresql-backslash", backslash=True, dollar=True, e_strings=True, nested=True)
_MYSQL = _Reading("mysql", backslash=True, dq_string=True, backtick=True, hash_comment=True)
_MYSQL_NBE = _Reading("mysql-no-backslash", dq_string=True, backtick=True, hash_comment=True)
_MYSQL_ANSI = _Reading("mysql-ansi", backslash=True, backtick=True, hash_comment=True)
_MYSQL_BOTH = _Reading("mysql-ansi-no-backslash", backtick=True, hash_comment=True)
_ORACLE = _Reading("oracle", q_quote=True)

# 每种方言的读法：第一种是服务器默认设置下的读法，其余是服务器可能开着的其他设置。
# 守卫看不到服务器的设置，所以每种都要扫，读出来不一样就拒
_READINGS: dict[str, tuple[_Reading, ...]] = {
    "sqlite": (_SQLITE,),
    "postgresql": (_PG, _PG_OLD),
    "postgres": (_PG, _PG_OLD),
    "mysql": (_MYSQL, _MYSQL_NBE, _MYSQL_ANSI, _MYSQL_BOTH),
    "mariadb": (_MYSQL, _MYSQL_NBE, _MYSQL_ANSI, _MYSQL_BOTH),
    "oracle": (_ORACLE,),
}
# 不知道方言时每种读法都算上
_ALL_READINGS: tuple[_Reading, ...] = (_GENERIC, *dict.fromkeys(
    r for readings in _READINGS.values() for r in readings))

_AMBIGUOUS = (
    "这条 SQL 里的引号、反斜杠或注释，在数据库的不同设置下会读成不同的语句"
    "（比如 MySQL 的 NO_BACKSLASH_ESCAPES、ANSI_QUOTES）。"
    "字符串里的单引号写成两个单引号 ''，不要用反斜杠转义；注释删掉再试"
)

_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")
_Q_CLOSE = {"[": "]", "(": ")", "{": "}", "<": ">"}


def _readings(dialect: str | None) -> tuple[_Reading, ...]:
    return _READINGS.get((dialect or "").lower(), _ALL_READINGS)


def _reading(dialect: str | None) -> _Reading:
    """服务器默认设置下的读法。不知道方言时用通用读法（标准 SQL 加反引号）。"""
    return _READINGS.get((dialect or "").lower(), (_GENERIC,))[0]


def _word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _prefixed(sql: str, i: int, letters: str) -> bool:
    """i 处的引号前面紧挨着一个前缀字母（E'…'），而且这个字母不是某个标识符的尾巴。"""
    return i >= 1 and sql[i - 1] in letters and not (i >= 2 and _word_char(sql[i - 2]))


def _q_quote_at(sql: str, i: int) -> bool:
    """Oracle 的 q'…' / nq'…'：引号后面紧跟的字符是定界符，不能是空白。"""
    if i + 1 >= len(sql) or sql[i + 1].isspace() or not (i >= 1 and sql[i - 1] in "qQ"):
        return False
    j = i - 2
    if j >= 0 and sql[j] in "nN":
        j -= 1
    return not (j >= 0 and _word_char(sql[j]))


@dataclass(frozen=True)
class _Token:
    #: code / comment / string / ident / sep
    kind: str
    text: str
    #: 开、闭定界符的长度，抹内容时保留它们
    open: int = 0
    close: int = 0


def _quoted_end(sql: str, i: int, quote: str, backslash: bool) -> tuple[int, int]:
    """从 i 处的开引号往后找闭引号。返回 (结束位置, 闭引号长度)；没闭合的一直到末尾。"""
    j = i + 1
    while j < len(sql):
        ch = sql[j]
        if backslash and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if j + 1 < len(sql) and sql[j + 1] == quote:     # 两个引号是转义，不是结束
                j += 2
                continue
            return j + 1, 1
        j += 1
    return len(sql), 0


def _tokens(sql: str, reading: _Reading) -> list[_Token]:
    """一遍扫完：注释、字符串、带引号的标识符、分号各自成段，其余是代码。"""
    out: list[_Token] = []
    n = len(sql)
    code = 0            # 当前这段代码的起点
    i = 0

    def flush(upto: int) -> None:
        if upto > code:
            out.append(_Token("code", sql[code:upto]))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        prev = sql[i - 1] if i else ""
        token: _Token | None = None
        end = i
        if (ch == "-" and nxt == "-") or (ch == "#" and reading.hash_comment):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            token = _Token("comment", sql[i:end])
        elif ch == "/" and nxt == "*":
            if reading.nested:
                depth, end = 0, i
                while end < n:
                    if sql.startswith("/*", end):
                        depth, end = depth + 1, end + 2
                    elif sql.startswith("*/", end):
                        depth, end = depth - 1, end + 2
                        if depth == 0:
                            break
                    else:
                        end += 1
                end = min(end, n)
            else:
                close = sql.find("*/", i + 2)
                end = n if close < 0 else close + 2
            token = _Token("comment", sql[i:end])
        elif ch == ";":
            end = i + 1
            token = _Token("sep", ";")
        elif ch == "'":
            if reading.q_quote and _q_quote_at(sql, i):
                closer = _Q_CLOSE.get(nxt, nxt) + "'"
                found = sql.find(closer, i + 2)
                end, tail = (n, 0) if found < 0 else (found + 2, 2)
                token = _Token("string", sql[i:end], 2, tail)
            else:
                e_string = reading.e_strings and _prefixed(sql, i, "eE")
                end, tail = _quoted_end(sql, i, "'", reading.backslash or e_string)
                token = _Token("string", sql[i:end], 1, tail)
        elif ch == '"':
            end, tail = _quoted_end(sql, i, '"', reading.backslash and reading.dq_string)
            token = _Token("string" if reading.dq_string else "ident", sql[i:end], 1, tail)
        elif ch == "`" and reading.backtick:
            end, tail = _quoted_end(sql, i, "`", False)
            token = _Token("ident", sql[i:end], 1, tail)
        elif ch == "[" and reading.brackets:
            close = sql.find("]", i + 1)
            end, tail = (n, 0) if close < 0 else (close + 1, 1)
            token = _Token("ident", sql[i:end], 1, tail)
        elif ch == "$" and reading.dollar and not (prev and _word_char(prev)):
            tag = _DOLLAR_TAG.match(sql, i)
            if tag:
                found = sql.find(tag.group(0), tag.end())
                width = len(tag.group(0))
                end, tail = (n, 0) if found < 0 else (found + width, width)
                token = _Token("string", sql[i:end], width, tail)
        if token is None:
            i += 1
            continue
        flush(i)
        out.append(token)
        i = code = end
    flush(n)
    return out


def _joined(tokens: list[_Token]) -> str:
    """注释换成一个空格，其余原样拼回去。空格不能省：'a'/**/'b' 删成 'a''b' 就成了一个字符串。"""
    return "".join(" " if t.kind == "comment" else t.text for t in tokens)


def _statements(sql: str, reading: _Reading) -> list[str]:
    out: list[str] = []
    buf: list[_Token] = []
    for token in [*_tokens(sql, reading), _Token("sep", ";")]:
        if token.kind == "sep":
            stmt = _joined(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(token)
    return out


def strip_comments(sql: str, *, dialect: str | None = None) -> str:
    """去掉注释再判断。否则 `/*x*/ DROP TABLE t` 的首关键字会被看成注释内容。

    字符串、带引号的标识符里的 `--`、`/*` 不是注释，原样保留。
    """
    return _joined(_tokens(sql, _reading(dialect)))


def split_statements(sql: str, *, dialect: str | None = None) -> list[str]:
    """按分号切语句（去掉注释），但要认得字符串字面量里的分号。

    `SELECT ';'` 只是一条语句。简单 split(";") 会把它切成两半，然后把后半截
    当成"第二条语句"拒掉——那是误杀，比漏杀更让人无从下手。
    """
    return _statements(sql, _reading(dialect))


def first_verb(sql: str, *, dialect: str | None = None) -> str:
    """取语句的首关键字（小写）。括号开头的 `(SELECT ...)` 也要认出来。"""
    text = strip_comments(sql, dialect=dialect).lstrip().lstrip("(").lstrip()
    match = re.match(r"[A-Za-z_]+", text)
    return match.group(0).lower() if match else ""


def _blanked(tokens: list[_Token], *, identifiers: bool) -> str:
    out: list[str] = []
    for t in tokens:
        if t.kind == "comment":
            out.append(" ")
        elif t.kind == "string" or (t.kind == "ident" and identifiers):
            inner = len(t.text) - t.open - t.close
            out.append(t.text[:t.open] + " " * inner + t.text[len(t.text) - t.close:])
        else:
            out.append(t.text)
    return "".join(out)


def _blank_quoted(sql: str, *, identifiers: bool = True, dialect: str | None = None) -> str:
    """把引号里的内容换成空格：字符串 'please delete' 和标识符 "update" 都不是关键字。

    identifiers=False 时只抹字符串、保留 "…" 和 `…`——找函数调用时要看得见
    被引号括起来的函数名。
    """
    return _blanked(_tokens(sql, _reading(dialect)), identifiers=identifiers)


def _pragma_write_reason(stmt: str, reading: _Reading = _GENERIC) -> str | None:
    match = _PRAGMA.match(_joined(_tokens(stmt, reading)).strip())
    if not match:
        return "看不出这条 PRAGMA 读的是什么"
    name, rest = match.group(1).lower(), match.group(2).strip()
    if rest.startswith("="):
        return f"PRAGMA {name} = … 会改数据库设置"
    if name in _PRAGMA_READ_WITH_ARG or (name in _PRAGMA_READ_NO_ARG and not rest):
        return None
    return (
        f"PRAGMA {name}{' ' + rest if rest else ''} 不在只读白名单里"
        "（看表结构用 table_info / index_list / foreign_key_list）"
    )


def _write_reason(stmt: str, reading: _Reading = _GENERIC) -> str | None:
    """这条语句会不会写。会写就返回一句说给模型听的原因，不会写返回 None。

    首关键字只是第一道：以查询开头的语句正文照样能写（见 _HIDDEN_WRITE），
    所以 SELECT / WITH / EXPLAIN 还要把正文扫一遍。SHOW / DESCRIBE / VALUES
    结构上写不了，不扫——`SHOW CREATE TABLE` 里的 CREATE 不是在建表。
    """
    tokens = _tokens(stmt, reading)
    text = _joined(tokens).lstrip().lstrip("(").lstrip()
    match = re.match(r"[A-Za-z_]+", text)
    verb = match.group(0).lower() if match else ""
    if verb not in _READONLY_VERBS:
        return f"{verb.upper()} 会修改数据"
    if verb == "pragma":
        return _pragma_write_reason(stmt, reading)
    if verb in ("select", "with", "explain"):
        hidden = _HIDDEN_WRITE.search(_blanked(tokens, identifiers=True))
        if hidden:
            word = " ".join(hidden.group(1).split()).upper()
            return f"语句里有 {word}——以查询开头的语句也能写数据"
        call = _SIDE_EFFECT_CALL.search(_blanked(tokens, identifiers=False))
        if call:
            return f"{call.group(1)}() 有副作用"
    return None


def _write_reason_any(stmt: str, readings: tuple[_Reading, ...]) -> str | None:
    """按每种读法都判一遍：哪种读法下会写，就当它会写。"""
    reasons = [_write_reason(stmt, r) for r in readings]
    if reasons[0]:
        return reasons[0]
    other = next((r for r in reasons[1:] if r), None)
    return f"数据库换一种设置来读的话，{other}。{_AMBIGUOUS}" if other else None


def _one_view(sql: str, readings: tuple[_Reading, ...]) -> list[str] | None:
    """每种读法切出来的语句都一样时返回它，不一样返回 None。"""
    views = [_statements(sql, r) for r in readings]
    return views[0] if all(v == views[0] for v in views[1:]) else None


def check(sql: str, *, readonly: bool, source_name: str = "", dialect: str | None = None) -> str:
    """校验并返回规范化后的单条 SQL。不通过就抛 SqlRejected。

    返回值是去掉注释和尾分号的那一条语句——调用方应该执行这个返回值，
    而不是原始输入，否则守卫等于没做。字符串、带引号的标识符原样保留。

    dialect 是数据源的 kind（sqlite / postgresql / mysql / oracle …）。
    不知道的话按所有方言、所有设置的读法各判一遍，任何一种读法不过都拒。
    """
    if not sql or not sql.strip():
        raise SqlRejected("SQL 是空的")

    readings = _readings(dialect)
    statements = _one_view(sql, readings)
    if statements is None:
        raise SqlRejected(_AMBIGUOUS)
    if not statements:
        raise SqlRejected("SQL 里没有可执行的语句")
    if len(statements) > 1:
        raise SqlRejected(
            f"一次只能执行一条语句，这里有 {len(statements)} 条。"
            "拆成多次调用——分号拼接的语句不会被执行。"
        )

    stmt = statements[0]
    # 交给数据库的就是 stmt：它在每种读法下都得还是完整的一条，没有注释、没有分号
    if any(_tokens_have(stmt, r, ("comment", "sep")) for r in readings):
        raise SqlRejected(_AMBIGUOUS)

    verb = first_verb(stmt, dialect=dialect)
    if not verb:
        raise SqlRejected("看不出这条 SQL 要做什么（没有识别到关键字）")

    if verb in _ALWAYS_DENIED:
        raise SqlRejected(
            f"{verb.upper()} 属于结构变更或权限操作，任何数据源上都不允许。"
            "这类操作请在数据库客户端里人工执行。"
        )

    if readonly:
        reason = _write_reason_any(stmt, readings)
        if reason:
            where = f"「{source_name}」" if source_name else "这个数据源"
            raise SqlRejected(
                f"{where}是只读的：{reason}，不被允许。"
                "只能执行不改数据的 SELECT / WITH / SHOW / DESCRIBE / EXPLAIN。"
                "确实需要写入的话，请在设置里另建一个关闭了只读的数据源。"
            )

    return stmt


def _tokens_have(sql: str, reading: _Reading, kinds: tuple[str, ...]) -> bool:
    return any(t.kind in kinds for t in _tokens(sql, reading))


def is_write(sql: str, *, dialect: str | None = None) -> bool:
    """是不是写操作。用来决定要不要走人工审批。

    和只读判定用同一份 _write_reason：审批这道门要是只看首关键字，
    `WITH … DELETE` 在可写源上就能不经审批直接写。几种读法切出来不一样的，
    一律当写：看不清的交给人批。
    """
    readings = _readings(dialect)
    statements = _one_view(sql, readings)
    if not statements:
        return True
    return any(_write_reason(s, r) is not None for s in statements for r in readings)
