"""期 4「推断的来源」的 SQL 判据：认出「从一张表里直接选取若干列」这一种形状（P4-SPEC 2.4、7.2）。

下钻要回答「查询结果的第 i 列是哪张表的哪一列」。只有一张表、每个结果列都是这张表某列的直接引用时，这个问题才有
唯一的答案；改名、表达式、多表、子查询、集合运算之后，结果列装的可能是另一列的值，值恰好相等时就会下钻到错误的
格子。所以这里只认一种形状，形状之外一律拒绝：**宁可不下钻，也不能下钻错**。

**读法和执行这条 SQL 的 SQLite 相同**：分词用守卫的 `guard._tokens(sql, guard._SQLITE)`。查询快照里存的 SQL 是
`guard.check(…, dialect="sqlite")` 的返回值，执行的也正是它（engine.run_query）。`_SQLITE` 读法：单引号字符串里
反斜杠不是转义；`[…]` 是标识符，里面没有转义，遇到第一个 `]` 就结束；`"…"`、`` `…` `` 是标识符，两个引号表示
一个引号。识别器和数据库读的是同一串记号，中间没有第二种读法。

**SQLite 分词的两处细节**（tokenize.c，实测 3.53）：
- U+FEFF（字节 EF BB BF，CC_BOM）出现在记号开头时是空白，出现在名字中间时是名字的一部分。不能照「码位 ≥ 0x80
  一律算名字」读：那样紧跟在 U+FEFF 后面的 UNION 成了一个普通的词，藏在它后面的 UNION ALL、GROUP BY 就看不见了，
  SQLite 却照样执行，结果列里混着另一列的值；
- 竖制表符出现在记号开头时是非法字符，紧跟在其他空白后面时是空白（CC_SPACE 的循环用 sqlite3Isspace，含 \\v）。
  这里一律当空白：SQLite 不当空白的位置整条 SQL 执行不了，不会留下查询快照，判据碰不到；能执行的位置读法相同。

**为什么不用 `evidence._sql_scan`**：它按多方言「宁可少认一张表」设计，单引号里认反斜杠转义、方括号里含引号时不当
标识符。藏在 `ESCAPE '\\'` 或 `AS [t']` 后面的 `UNION ALL` 在它眼里不存在，SQLite 却照样执行（P4-SPEC 附录 B7）。
对 `sql_tables` 那只是少认一张表，对下钻就是错格子。`_sql_scan`、`sql_tables` 本身不动（H7）。

**执行时与存下的字面不同的一处**：engine 经 SQLAlchemy 的 `text()` 执行，它把 `\\:` 改成 `:`、把 `:名字` 当成绑定
参数。后者没有给值，执行会失败，不会留下查询快照；前者只删掉冒号前的反斜杠，SQLite 读法里反斜杠不影响任何记号的
边界，只改字符串和带引号名字的内容，而配方导入的表名、列名不含反斜杠和冒号（names.name_problem），对不上任何表列。

**分组**（界面显示分组的原文，细分给测试和日志用，取值见 provenance_types.DETAILS）：
- expression：表达式、聚合、字符串或字面量关键字、窗口、COLLATE、DISTINCT；
- alias：给列改名（含省略 AS 的改名）；
- multi_table：JOIN、逗号连接、子查询、公用表表达式、集合运算；
- unparsed：不是 SELECT、多条语句、表的写法不认识、限定名不是本表、列或表找不到、重名、`*` 对不上、尾部写法不认识。

判据绑在文档标记 `DOC_PROVENANCE = 1` 上（P4-SPEC 1.4）：以后放宽文法要升版本号、给 recognize 加 version 参数，
已有 `provenance: 1` 的文档仍按这一版判，结论不跟着变。

只许导入守卫、names 和 provenance_types（P4-SPEC 7.2）：判据是纯函数，不连库，不依赖证据层和接口层。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.data.guard import _SQLITE, _tokens
from app.data.names import name_key
from app.data.provenance_types import DirectSelect, SelectRefusal

# ==========================================================================
# 分词：守卫的 SQLite 读法之上，再按 SQLite 的规则把代码段切成词和标点
# ==========================================================================

#: SQLite 在记号之间认的空白：CC_SPACE 的五个，加竖制表符。不能用 str.isspace：全角空格、不换行空格在 SQLite 里是
#: 名字的一部分（除记号开头的 U+FEFF 之外，码位 ≥ 0x80 的字符一律算名字）。竖制表符出现在记号开头时 SQLite 报非法
#: 记号，紧跟其他空白时当空白；一律当空白是安全的：前一种位置执行不了，后一种读法相同（模块说明）
_SPACE = frozenset(" \t\n\v\f\r")
#: U+FEFF 只在记号开头是空白（CC_BOM），名字中间是名字的一部分（「日\ufeff期」是一个名字），所以不放进 _SPACE、
#: 只在 _split_code 判断记号开头的地方单独认
_BOM = "\ufeff"
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGITS = frozenset("0123456789")


def _name_start(ch: str) -> bool:
    """不加引号的名字的开头：ASCII 字母、下划线、码位 ≥ 0x80 的字符（同 SQLite，也同 2 版实体语法）。

    U+FEFF 例外：出现在记号开头时 SQLite 当空白，调用处（_split_code）先把它判掉，轮不到这里。名字中间的 U+FEFF
    仍算名字：_name_char 借这个函数认后续字符，所以这里不排除它。"""
    return ch in _ASCII_LETTERS or ch == "_" or ord(ch) >= 0x80


def _name_char(ch: str) -> bool:
    """名字的后续字符：开头字符之外再加 ASCII 数字和 $（SQLite 的 IdChar）。不用 isalnum：「٣」这类非 ASCII
    数字本来就 ≥ 0x80，算名字；ASCII 以外的 isdigit 不能决定切词。"""
    return _name_start(ch) or ch in _ASCII_DIGITS or ch == "$"


@dataclass(frozen=True)
class _Lex:
    #: word：不加引号的词；name：带引号的名字（"…" `…` […]）；str：字符串；blob：x'…'；num：数字；punct：标点；
    #: sep：分号
    kind: str
    #: word、num、punct 是原文；name、str 是去掉定界符、还原了成对引号的内容
    text: str
    #: 在原 SQL 里的结束位置：认 x'…' 时要知道 x 和引号之间有没有空白
    end: int


def _split_code(text: str, base: int, out: list[_Lex]) -> None:
    """把一段代码（守卫已经把注释、字符串、带引号的名字、分号切出去了）按 SQLite 的规则切成词、数字和标点。

    数字只吃数字和小数点：`1e5`、`0x1F` 会被切成「数字 + 词」，`1union` 也会切出 UNION。SQLite 遇到数字后紧跟
    名字字符会报非法记号、根本不执行，所以这样切只会让判据多看见词、多拒，不会少看见。

    循环顶层的每个位置都是一个记号的开头（代码段的开头紧跟在注释、字符串、带引号的名字或分号后面，也是）：
    U+FEFF 在这里当空白，同 SQLite 的 CC_BOM；进了名字以后由 _name_char 当名字字符吃掉。
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _SPACE or ch == _BOM:
            i += 1
        elif _name_start(ch):
            j = i + 1
            while j < n and _name_char(text[j]):
                j += 1
            out.append(_Lex("word", text[i:j], base + j))
            i = j
        elif ch in _ASCII_DIGITS or (ch == "." and i + 1 < n and text[i + 1] in _ASCII_DIGITS):
            j = i + 1
            while j < n and (text[j] in _ASCII_DIGITS or text[j] == "."):
                j += 1
            out.append(_Lex("num", text[i:j], base + j))
            i = j
        else:
            out.append(_Lex("punct", ch, base + i + 1))
            i += 1


def _lex(sql: str, *, strict: bool = True) -> list[_Lex] | None:
    """整条 SQL → 词序列。引号没闭合时 strict 返回 None（SQLite 会报非法记号，存下的 SQL 不该这样）；
    不 strict 时照样切，没闭合的那一段内容取到末尾（tables_in 宁可多认）。

    注释当空白跳过：SQLite 也这样读，注释两边的代码各自成词（`SEL/**/ECT` 是两个词）。
    """
    out: list[_Lex] = []
    pos = 0
    for tok in _tokens(sql, _SQLITE):
        start, pos = pos, pos + len(tok.text)
        if tok.kind == "comment":
            continue
        if tok.kind == "sep":
            out.append(_Lex("sep", ";", pos))
        elif tok.kind in ("string", "ident"):
            if tok.close == 0 and strict:
                return None
            inner = tok.text[tok.open:len(tok.text) - tok.close]
            quote = tok.text[0]
            if tok.kind == "string":
                inner = inner.replace("''", "'")
                prev = out[-1] if out else None
                # x'00'：紧挨在单独一个 x 后面的单引号串是 BLOB 字面量，不是「名字 x 加字符串别名」
                if prev is not None and prev.kind == "word" and prev.text in ("x", "X") and prev.end == start:
                    out[-1] = _Lex("blob", inner, pos)
                else:
                    out.append(_Lex("str", inner, pos))
            else:
                # 方括号里没有转义；双引号、反引号里两个引号表示一个
                if quote in "\"`":
                    inner = inner.replace(quote * 2, quote)
                out.append(_Lex("name", inner, pos))
        else:
            _split_code(tok.text, start, out)
    return out


def _kw(lex: _Lex | None) -> str | None:
    """不加引号的词按关键字比较时用的键（只对 ASCII 不区分大小写，同 SQLite）；不是词返回 None。

    带引号的名字永远不是关键字：`"union"` 是一个叫 union 的列。
    """
    return name_key(lex.text) if lex is not None and lex.kind == "word" else None


def _is_name(lex: _Lex | None) -> bool:
    return lex is not None and lex.kind in ("word", "name")


# ==========================================================================
# 关键字表
# ==========================================================================

#: P4-SPEC 2.4：尾部（以及整条语句里）不得出现的词，按细分归组。七组的并集就是规格的那张表，测试钉住
_COMPOUND = frozenset({"union", "intersect", "except"})
_CTE = frozenset({"with", "recursive"})
_SUBQUERY = frozenset({"select", "values"})
_JOIN = frozenset({"join", "natural", "cross", "inner", "left", "right", "full", "outer"})
_WINDOW = frozenset({"over", "window"})
#: GROUP BY / HAVING：结果是聚合出来的
_AGGREGATE = frozenset({"group", "having"})
#: 尾部的 FROM、DISTINCT 只会出现在 `IS [NOT] DISTINCT FROM` 这类写法里：不影响结果列，但超出这一版文法
_TAIL_ODD = frozenset({"from", "distinct"})
FORBIDDEN = _COMPOUND | _CTE | _SUBQUERY | _JOIN | _WINDOW | _AGGREGATE | _TAIL_ODD

#: 不加引号时是字面量的关键字（P4-SPEC 2.4）。CURRENT_* 永远是关键字；TRUE、FALSE 只在表里有同名列时才指那一列，
#: 读法取决于表结构，一律拒最保守
_LITERAL_KEYWORDS = frozenset({"null", "true", "false", "current_date", "current_time", "current_timestamp"})

#: SQLite 的保留字里不能当不加引号的名字用的那些：出现在选取项、表名的位置，这一项就是表达式（CASE …、NOT x、
#: EXISTS (…)）或者写法不对。CAST、RAISE、MATCH、GLOB、END、FILTER 之类不在里面：SQLite 允许它们当名字（fallback），
#: 后面紧跟「(」时由下一步按函数调用拒，单独出现、又真有这一列时它就是那一列
_EXPR_WORDS = frozenset({
    "case", "not", "exists", "when", "then", "else", "select", "distinct", "all",
    "from", "where", "group", "having", "order", "limit", "as", "on", "using",
    "and", "or", "is", "in", "between", "escape", "collate", "isnull", "notnull",
})

#: 选取项后面紧跟这些词：这一项是运算的一部分（`"日期" IS NULL`、`"a" BETWEEN …`），不是省略 AS 的改名
_OPERATOR_WORDS = frozenset({
    "is", "isnull", "notnull", "not", "and", "or", "in", "like", "glob", "regexp", "match", "between",
    "escape", "filter",
})

#: 表名后面紧跟这些词时它们不是表别名：子句开头、连接、`INDEXED BY` / `NOT INDEXED`、ON / USING
_AFTER_TABLE = frozenset({
    "where", "order", "limit", "group", "having", "window", "indexed", "not", "on", "using", "as",
    *_JOIN, *_COMPOUND,
})

#: 尾部允许的开头（P4-SPEC 2.4 文法）：只过滤、排序、截取行，不改变结果列的来历
_TAIL_START = frozenset({"where", "order", "limit"})


def _refuse(code: str, detail: str) -> SelectRefusal:
    return SelectRefusal(code, detail)  # type: ignore[arg-type]


# ==========================================================================
# recognize
# ==========================================================================


def _statement(lexes: list[_Lex]) -> list[_Lex] | SelectRefusal:
    """去掉尾部的分号；分号后面还有东西就是多条语句。

    存下的 SQL 是 guard.check 的返回值，本来就没有分号和注释。P4-SPEC 7.2 写的是「出现就按 multi_statement 拒」，
    但 2.4 的文法写了 `[;]`，原型的 24 例（期望不变）和 6.3 要求注释里的 JOIN、尾分号照样认可，两处矛盾。这里取
    后者，按 SQLite 的读法接受尾分号、把注释当空白：两者都不改变这条语句选了什么，读法又和执行时相同，不会因此
    认错；真正的第二条语句照样拒。
    """
    end = len(lexes)
    while end and lexes[end - 1].kind == "sep":
        end -= 1
    body = lexes[:end]
    if any(lx.kind == "sep" for lx in body):
        return _refuse("unparsed", "multi_statement")
    return body


def _global_shape(body: list[_Lex]) -> SelectRefusal | None:
    """整条语句里只要出现就不是「一张表的直接选取」的关键字：集合运算、公用表表达式、子查询、连接、窗口。

    先于逐项的形状判断：B7 那类写法（`… ESCAPE '\\' UNION ALL …`）不管前面哪一项长什么样，都要报成集合运算。
    词只取代码段里的：字符串、带引号的名字、注释里出现的不算，和 SQLite 的读法一样。
    """
    words = {_kw(lx) for lx in body[1:] if lx.kind == "word"}
    for group, detail, code in ((_COMPOUND, "compound", "multi_table"), (_CTE, "cte", "multi_table"),
                                (_SUBQUERY, "subquery", "multi_table"), (_JOIN, "join", "multi_table"),
                                (_WINDOW, "window", "expression")):
        if words & group:
            return _refuse(code, detail)
    return None


@dataclass
class _Item:
    qualifier: str | None
    column: str


def _item_start(lex: _Lex | None) -> SelectRefusal | None:
    """选取项的第一个记号不是名字时，这一项是什么。是名字返回 None。"""
    if lex is None:
        return _refuse("unparsed", "not_select")
    if lex.kind == "str":
        return _refuse("expression", "string_literal")
    kw = _kw(lex)
    if kw in _LITERAL_KEYWORDS:
        return _refuse("expression", "literal_keyword")
    if kw in _EXPR_WORDS or not _is_name(lex):
        # 数字、blob、括号、正负号、CASE / CAST / NOT …
        return _refuse("expression", "expression")
    return None


def _after_item(lex: _Lex | None) -> SelectRefusal:
    """选取项后面只能紧跟「,」或 FROM（P4-SPEC 2.4）。其余的分别是什么。"""
    kw = _kw(lex)
    if lex is None:
        # 没有 FROM：不是从表里选
        return _refuse("unparsed", "table_shape")
    if kw == "as":
        return _refuse("alias", "alias")
    if kw == "collate":
        return _refuse("expression", "collate")
    if kw in _OPERATOR_WORDS:
        return _refuse("expression", "expression")
    if lex.kind in ("name", "str") or (lex.kind == "word" and kw not in _EXPR_WORDS):
        # `"日期" "分区乙"`、`"日期" '分区乙'`、`"日期" 别名`：SQLite 都当省略 AS 的改名
        return _refuse("alias", "alias")
    # 「(」是函数调用，其余标点、数字是运算
    return _refuse("expression", "expression")


def _select_list(body: list[_Lex], k: int) -> tuple[list[_Item] | None, int] | SelectRefusal:
    """解析选取项，返回 (项列表，FROM 的位置)。`*` 单独出现时项列表为 None。"""
    if k < len(body) and body[k].kind == "punct" and body[k].text == "*":
        nxt = body[k + 1] if k + 1 < len(body) else None
        if _kw(nxt) != "from":
            if nxt is not None and nxt.kind == "punct" and nxt.text == ",":
                # `*, "日期"`：* 只许单独出现，否则结果列和表的列对不上位置
                return _refuse("unparsed", "star_mismatch")
            return _after_item(nxt)
        return None, k + 1
    items: list[_Item] = []
    while True:
        lex = body[k] if k < len(body) else None
        if lex is not None and lex.kind == "punct" and lex.text == "*":
            # `"日期", *`
            return _refuse("unparsed", "star_mismatch")
        refusal = _item_start(lex)
        if refusal is not None:
            return refusal
        assert lex is not None
        k += 1
        qualifier: str | None = None
        column = lex.text
        if k < len(body) and body[k].kind == "punct" and body[k].text == ".":
            target = body[k + 1] if k + 1 < len(body) else None
            if target is not None and target.kind == "punct" and target.text == "*":
                # `t.*`：不在这一版文法里
                return _refuse("unparsed", "star_mismatch")
            if not _is_name(target):
                return _refuse("expression", "expression")
            assert target is not None
            qualifier, column = lex.text, target.text
            k += 2
            if k < len(body) and body[k].kind == "punct" and body[k].text == ".":
                # `main.日客流.日期`：带库名的限定名
                return _refuse("unparsed", "qualifier")
        items.append(_Item(qualifier, column))
        nxt = body[k] if k < len(body) else None
        if nxt is not None and nxt.kind == "punct" and nxt.text == ",":
            k += 1
            continue
        if _kw(nxt) == "from":
            return items, k
        return _after_item(nxt)


def _from_clause(body: list[_Lex], k: int) -> tuple[str, str | None, int] | SelectRefusal:
    """FROM 后面：表名、可选的表别名。返回 (表名，别名，尾部的起点)。"""
    lex = body[k] if k < len(body) else None
    if lex is not None and lex.kind == "punct" and lex.text == "(":
        # FROM (SELECT …)；`FROM (日客流)` 在 SQLite 里也能执行，一律不认
        return _refuse("multi_table", "subquery")
    if not _is_name(lex) or _kw(lex) in _AFTER_TABLE | _EXPR_WORDS:
        # 字符串当表名（SQLite 的历史兼容）、数字、缺表名
        return _refuse("unparsed", "table_shape")
    assert lex is not None
    table = lex.text
    k += 1
    nxt = body[k] if k < len(body) else None
    if nxt is not None and nxt.kind == "punct" and nxt.text == ".":
        return _refuse("unparsed", "qualified_table")
    if nxt is not None and nxt.kind == "punct" and nxt.text == "(":
        # 表函数（json_each(…)）
        return _refuse("unparsed", "table_shape")
    alias: str | None = None
    if _kw(nxt) == "as":
        target = body[k + 1] if k + 1 < len(body) else None
        if not _is_name(target) or _kw(target) in _AFTER_TABLE | _EXPR_WORDS:
            # `AS '别名'`：SQLite 认字符串当别名，这一版文法不认
            return _refuse("unparsed", "table_shape")
        assert target is not None
        alias = target.text
        k += 2
    elif nxt is not None and (nxt.kind == "name"
                              or (nxt.kind == "word" and _kw(nxt) not in _AFTER_TABLE | _EXPR_WORDS)):
        alias = nxt.text
        k += 1
    return table, alias, k


def _tail(body: list[_Lex], k: int) -> SelectRefusal | None:
    """尾部只能以 WHERE / ORDER / LIMIT 开头，里面不得有 P4-SPEC 2.4 那张表里的词。

    整条语句里的集合运算、子查询、连接、窗口已经在 _global_shape 拦了，这里只剩 GROUP / HAVING（聚合）和尾部的
    FROM、DISTINCT。
    """
    if k >= len(body):
        return None
    head = body[k]
    kw = _kw(head)
    if kw not in _TAIL_START:
        if head.kind == "punct" and head.text == ",":
            return _refuse("multi_table", "comma_join")
        if kw in _AGGREGATE:
            return _refuse("expression", "expression")
        # INDEXED BY、NOT INDEXED、第二个别名、括号……
        return _refuse("unparsed", "tail_shape")
    words = {_kw(lx) for lx in body[k:] if lx.kind == "word"}
    if words & _AGGREGATE:
        return _refuse("expression", "expression")
    if words & _TAIL_ODD:
        return _refuse("unparsed", "tail_shape")
    return None


def _index(names: Iterable[str]) -> dict[str, str | None]:
    """name_key → 原名。两个名字撞了键（冻结表结构里不该出现）时记 None：说不清是哪一个，按找不到处理。"""
    out: dict[str, str | None] = {}
    for name in names:
        key = name_key(name)
        out[key] = None if key in out else name
    return out


def recognize(sql: str, *, result_columns: list[str], tables: dict[str, list[str]]
              ) -> DirectSelect | SelectRefusal:
    """tables：冻结表结构里的 {表名: [列名…]}（按定义顺序）。

    认可（P4-SPEC 2.4 的文法）→ DirectSelect；否则 → SelectRefusal(code, detail)。纯函数，不连库，不抛异常。
    文法对应 DOC_PROVENANCE = 1（1.4）；以后放宽文法时加 version 参数，不改这一版的结论。

    判断顺序：分词和语句个数 → 是不是 SELECT（WITH 开头是公用表表达式）→ 整条语句里的集合运算、公用表表达式、
    子查询、连接、窗口 → 选取项 → FROM → 尾部 → 名字解析（表、限定名、列、重名）→ 结果列与选取项逐个对应。
    """
    lexes = _lex(sql)
    if lexes is None:
        return _refuse("unparsed", "not_select")
    body = _statement(lexes)
    if isinstance(body, SelectRefusal):
        return body
    first = _kw(body[0]) if body else None
    if first == "with":
        return _refuse("multi_table", "cte")
    if first != "select":
        return _refuse("unparsed", "not_select")
    refusal = _global_shape(body)
    if refusal is not None:
        return refusal

    k = 1
    if _kw(body[k] if k < len(body) else None) == "all":
        k += 1
    if _kw(body[k] if k < len(body) else None) == "distinct":
        return _refuse("expression", "distinct")
    parsed = _select_list(body, k)
    if isinstance(parsed, SelectRefusal):
        return parsed
    items, k = parsed
    shape = _from_clause(body, k + 1)
    if isinstance(shape, SelectRefusal):
        return shape
    table_written, alias, k = shape
    refusal = _tail(body, k)
    if refusal is not None:
        return refusal

    # ---- 名字解析：一律按 name_key（只对 ASCII 不区分大小写，同 SQLite） ----
    table = _index(tables).get(name_key(table_written))
    if table is None:
        return _refuse("unparsed", "unknown_table")
    defined = list(tables[table])
    if items is None:
        # `*`：结果列必须和表的列按 name_key 逐个相等、顺序一致，才能按位置对应
        if [name_key(c) for c in result_columns] != [name_key(c) for c in defined]:
            return _refuse("unparsed", "star_mismatch")
        return DirectSelect(table, alias, defined)
    # 有别名时只认别名：SQLite 里表起了别名之后，原表名不能再当限定名（`SELECT 日客流.日期 FROM 日客流 AS t`
    # 报 no such column），所以这一条比「表名或别名」严，严出来的只是执行不了的写法
    owner = name_key(alias if alias is not None else table_written)
    if any(it.qualifier is not None and name_key(it.qualifier) != owner for it in items):
        return _refuse("unparsed", "qualifier")
    columns_index = _index(defined)
    columns: list[str] = []
    for it in items:
        column = columns_index.get(name_key(it.column))
        if column is None:
            return _refuse("unparsed", "unknown_column")
        if column in columns:
            # `SELECT "日期", "日期"`、`SELECT Key, key`：locate_cell 按第一次出现取，说不清是哪一个
            return _refuse("unparsed", "ambiguous_column")
        columns.append(column)
    # 结果列按位置对应第 i 个选取项，不按名字认（SQLite 返回的列名有时是原写法、有时是定义名，P4-SPEC 2.4）。
    # 个数必须相等；名字只作一致性核对：直接引用的列，SQLite 返回的名字按 name_key 一定等于那一列，
    # 不等说明这份结果不是这条 SQL 的，不下钻
    if len(result_columns) != len(columns) or any(
            name_key(r) != name_key(c) for r, c in zip(result_columns, columns)):
        return _refuse("unparsed", "unknown_column")
    return DirectSelect(table, alias, columns)


# ==========================================================================
# tables_in
# ==========================================================================

#: 这些词后面紧跟的单引号串，SQLite 当表名读（`FROM '日客流'` 能执行，历史兼容）。IN：SQLite 文法 `expr IN nm`
#: 的 nm 可以是字符串，`WHERE 日期 IN '单列表'` 读的是那张表（列数和左边对得上就能执行，行值也行）
_TABLE_BEFORE_STRING = frozenset({"from", "join", "in"})
#: 逗号后面的单引号串当表名读的子句：FROM 列表里（含 ON / USING 之后接着逗号连接）
_TABLE_LIST_CLAUSES = frozenset({"from", "join", "on", "using"})
#: 切换子句的词：tables_in 据此判断逗号后面是不是还在 FROM 列表里
#: 不含 AS：`FROM a AS x, '时段客流'` 的逗号仍在 FROM 列表里
_CLAUSE_WORDS = frozenset({
    "select", "from", "join", "where", "group", "having", "order", "limit", "on", "using", "window", "values",
    "union", "intersect", "except", "with",
})


def tables_in(sql: str, tables: dict[str, list[str]]) -> list[str]:
    """SQL 里出现的冻结表结构的表名（原样），按第一次出现的顺序去重。读法同 recognize，宁可多认（P4-SPEC 2.4）。

    每个带引号的名字、代码里的每个词都按 name_key 对表名：列名或别名恰好和某张表同名时会多出这张表，后果只是
    摘录里多一行说明；漏认的后果是裁判拿不到「原表写明的合计，不要相加」。不用 2 版的 `sql_tables`：它的读法和
    SQLite 不同，藏在 `ESCAPE '\\'` 后面的合计表会漏掉。

    字符串一般不算（`WHERE 分区 = '日客流'` 不是读表），只有 SQLite 会把它当表名读的位置才算：紧跟 FROM / JOIN /
    IN、紧跟「.」（`main.'日客流'`），以及 FROM 列表里的逗号、括号之后。「紧跟」按 SQLite 的分词算：中间隔着的空白
    （含记号开头的 U+FEFF、空白后面的竖制表符）不算记号。注释里的不算：SQLite 不读注释。
    """
    index = _index(tables)
    found: dict[str, None] = {}
    lexes = _lex(sql, strict=False) or []
    clause: str | None = None
    prev: _Lex | None = None
    for lx in lexes:
        candidate: str | None = None
        if lx.kind in ("word", "name"):
            candidate = lx.text
        elif lx.kind == "str" and prev is not None:
            pkw = _kw(prev)
            if (pkw in _TABLE_BEFORE_STRING or (prev.kind == "punct" and prev.text == ".")
                    or (prev.kind == "punct" and prev.text in ",(" and clause in _TABLE_LIST_CLAUSES)):
                candidate = lx.text
        if candidate is not None:
            table = index.get(name_key(candidate))
            if table is not None:
                found.setdefault(table, None)
        kw = _kw(lx)
        if kw in _CLAUSE_WORDS:
            clause = kw
        prev = lx
    return list(found)
