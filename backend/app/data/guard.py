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

    if readonly and verb not in _READONLY_VERBS:
        where = f"「{source_name}」" if source_name else "这个数据源"
        raise SqlRejected(
            f"{where}是只读的，{verb.upper()} 不被允许。"
            "只能执行 SELECT / WITH / SHOW / DESCRIBE / EXPLAIN。"
            "确实需要写入的话，请在设置里另建一个关闭了只读的数据源。"
        )

    return stmt


def is_write(sql: str) -> bool:
    """是不是写操作。用来决定要不要走人工审批。"""
    return first_verb(sql) not in _READONLY_VERBS
