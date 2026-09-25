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

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

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
    timeout_seconds: int = 30


def strip_comments(sql: str) -> str:
    """去掉注释再判断。否则 `/*x*/ DROP TABLE t` 的首关键字会被看成注释内容。"""
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))


def split_statements(sql: str) -> list[str]:
    """按分号切语句，但要认得字符串字面量里的分号。

    `SELECT ';'` 只是一条语句。简单 split(";") 会把它切成两半，然后把后半截
    当成"第二条语句"拒掉——那是误杀，比漏杀更让人无从下手。
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    text = strip_comments(sql)
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                # SQL 里连续两个引号是转义（'' 表示一个单引号）
                if i + 1 < len(text) and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def first_verb(sql: str) -> str:
    """取语句的首关键字（小写）。括号开头的 `(SELECT ...)` 也要认出来。"""
    text = strip_comments(sql).lstrip().lstrip("(").lstrip()
    match = re.match(r"[A-Za-z_]+", text)
    return match.group(0).lower() if match else ""


def _blank_quoted(sql: str, *, identifiers: bool = True) -> str:
    """把引号里的内容换成空格：字符串 'please delete' 和标识符 "update" 都不是关键字。

    identifiers=False 时只抹字符串、保留 "…" 和 `…`——找函数调用时要看得见
    被引号括起来的函数名。
    """
    quotes = ("'", '"', "`") if identifiers else ("'",)
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:   # '' 是转义，不是结束
                    out.append("  ")
                    i += 2
                    continue
                quote = None
                out.append(ch)
            else:
                out.append(" ")
        else:
            if ch in quotes:
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


def _pragma_write_reason(stmt: str) -> str | None:
    match = _PRAGMA.match(strip_comments(stmt).strip())
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


def _write_reason(stmt: str) -> str | None:
    """这条语句会不会写。会写就返回一句说给模型听的原因，不会写返回 None。

    首关键字只是第一道：以查询开头的语句正文照样能写（见 _HIDDEN_WRITE），
    所以 SELECT / WITH / EXPLAIN 还要把正文扫一遍。SHOW / DESCRIBE / VALUES
    结构上写不了，不扫——`SHOW CREATE TABLE` 里的 CREATE 不是在建表。
    """
    verb = first_verb(stmt)
    if verb not in _READONLY_VERBS:
        return f"{verb.upper()} 会修改数据"
    if verb == "pragma":
        return _pragma_write_reason(stmt)
    if verb in ("select", "with", "explain"):
        text = strip_comments(stmt)
        hidden = _HIDDEN_WRITE.search(_blank_quoted(text))
        if hidden:
            word = " ".join(hidden.group(1).split()).upper()
            return f"语句里有 {word}——以查询开头的语句也能写数据"
        call = _SIDE_EFFECT_CALL.search(_blank_quoted(text, identifiers=False))
        if call:
            return f"{call.group(1)}() 有副作用"
    return None


def check(sql: str, *, readonly: bool, source_name: str = "") -> str:
    """校验并返回规范化后的单条 SQL。不通过就抛 SqlRejected。

    返回值是去掉尾分号的那一条语句——调用方应该执行这个返回值，
    而不是原始输入，否则守卫等于没做。
    """
    if not sql or not sql.strip():
        raise SqlRejected("SQL 是空的")

    statements = split_statements(sql)
    if not statements:
        raise SqlRejected("SQL 里没有可执行的语句")
    if len(statements) > 1:
        raise SqlRejected(
            f"一次只能执行一条语句，这里有 {len(statements)} 条。"
            "拆成多次调用——分号拼接的语句不会被执行。"
        )

    stmt = statements[0]
    verb = first_verb(stmt)
    if not verb:
        raise SqlRejected("看不出这条 SQL 要做什么（没有识别到关键字）")

    if verb in _ALWAYS_DENIED:
        raise SqlRejected(
            f"{verb.upper()} 属于结构变更或权限操作，任何数据源上都不允许。"
            "这类操作请在数据库客户端里人工执行。"
        )

    if readonly:
        reason = _write_reason(stmt)
        if reason:
            where = f"「{source_name}」" if source_name else "这个数据源"
            raise SqlRejected(
                f"{where}是只读的：{reason}，不被允许。"
                "只能执行不改数据的 SELECT / WITH / SHOW / DESCRIBE / EXPLAIN。"
                "确实需要写入的话，请在设置里另建一个关闭了只读的数据源。"
            )

    return stmt


def is_write(sql: str) -> bool:
    """是不是写操作。用来决定要不要走人工审批。

    和只读判定用同一份 _write_reason：审批这道门要是只看首关键字，
    `WITH … DELETE` 在可写源上就能不经审批直接写。
    """
    statements = split_statements(sql)
    return not statements or any(_write_reason(s) is not None for s in statements)
