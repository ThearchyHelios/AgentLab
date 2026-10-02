"""期 4 的 SQL 判据（P4-SPEC 2.4、6.3、7.2，WP-A）：engine/direct_select 的 recognize、tables_in。

判据决定一个结果格能不能下钻到原表的格子。放过一条不是「一张表的直接选取」的 SQL，结果列装的可能是另一列的值，
值恰好相等时界面就会指着错误的格子说「来自这里」——所以这里大半的用例是拒绝的写法，尤其是评审实测过的
B7（`ESCAPE '\\'`、`AS [t']` 后面藏的 UNION ALL）。最后一组拿真的 SQLite 执行随机拼出的 SQL，核对「认可的
每一列，值确实都来自它说的那一列」，不靠逐条想到的用例。

夹具一律假名（分区甲、分区乙、日客流、时段客流），数字随机。
"""
from __future__ import annotations

import ast
import itertools
import random
import sqlite3
from pathlib import Path

import pytest

from app.data import guard
from app.data import provenance_types as P
from app.data.provenance_types import DirectSelect, SelectRefusal
from app.engine import direct_select as D
from app.engine.direct_select import recognize, tables_in

TABLES: dict[str, list[str]] = {
    "日客流": ["日期", "全日客流", "分区甲", "分区乙"],
    "时段客流": ["日期", "时段", "时段类别", "客流"],
    "时段客流_表内合计": ["日期", "合计项", "客流"],
    # P4-SPEC 附录 B2：定义名是 Key，SQLite 对 `abc.key` 返回定义名
    "abc": ["Key", "v"],
}
DAILY = TABLES["日客流"]


def ok(sql: str, result: list[str], *, tables: dict[str, list[str]] = TABLES) -> DirectSelect:
    out = recognize(sql, result_columns=result, tables=tables)
    assert isinstance(out, DirectSelect), f"应认可：{sql!r} → {out!r}"
    return out


def no(sql: str, code: str, detail: str, *, result: list[str] | None = None,
       tables: dict[str, list[str]] = TABLES) -> SelectRefusal:
    out = recognize(sql, result_columns=result if result is not None else ["日期", "全日客流"], tables=tables)
    assert isinstance(out, SelectRefusal), f"应拒绝：{sql!r} → {out!r}"
    # 每个拒绝分支的细分都得在契约的 DETAILS 里：漏了映射，接口一侧的 contract_problems 会报
    assert out.detail in P.DETAILS[out.code], out
    assert (out.code, out.detail) == (code, detail), f"{sql!r} → {out!r}"
    return out


def stored(sql: str) -> str:
    """查询快照里存的就是守卫的返回值（engine.run_query）：用例照这个形态喂给判据。"""
    return guard.check(sql, readonly=True, dialect="sqlite")


# ---------------------------------------------------------------------------
# 原型的 24 例（P4-SPEC 2.4、附录 B4）：期望照原型，改成 code 和 detail
# ---------------------------------------------------------------------------

PROTOTYPE_REFUSED = [
    ('SELECT "日期", "分区甲" AS "分区乙" FROM "日客流"', "alias", "alias"),
    ('SELECT "日期" "分区乙" FROM "日客流"', "alias", "alias"),
    ('SELECT a."日期", b."全日客流" FROM "日客流" a JOIN "日客流" b ON a."日期" = b."日期"', "multi_table", "join"),
    ('SELECT a."日期", b."全日客流" FROM "日客流" a, "日客流" b WHERE a."日期" = b."日期"', "multi_table",
     "comma_join"),
    ('WITH x AS (SELECT * FROM "日客流") SELECT "日期", "全日客流" FROM x', "multi_table", "cte"),
    ('SELECT "日期", "全日客流" FROM (SELECT * FROM "日客流")', "multi_table", "subquery"),
    ('SELECT "日期", "全日客流" FROM "日客流" UNION SELECT "日期", "分区甲" FROM "日客流"', "multi_table", "compound"),
    ('SELECT "日期", SUM("全日客流") OVER () FROM "日客流"', "expression", "window"),
    ('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" IN (SELECT "日期" FROM "时段客流")', "multi_table",
     "subquery"),
    ('SELECT SUM("全日客流") AS "合计", COUNT(*) FROM "日客流"', "expression", "expression"),
    ('SELECT "日期", "全日客流" + 0 FROM "日客流"', "expression", "expression"),
    ("SELECT '日期', \"全日客流\" FROM \"日客流\"", "expression", "string_literal"),
    ('SELECT DISTINCT "日期", "全日客流" FROM "日客流"', "expression", "distinct"),
    # 表名后面的 INDEXED 不是别名，再往后不是 WHERE / ORDER / LIMIT
    ('SELECT "日期", "全日客流" FROM "日客流" INDEXED BY x', "unparsed", "tail_shape"),
    ('SELECT "日期", "全日客流" COLLATE NOCASE FROM "日客流"', "expression", "collate"),
    ('SELECT "日期", "全日客流" FROM json_each(\'[]\')', "unparsed", "table_shape"),
    ('SELECT "日期", "全日客流" FROM main."日客流"', "unparsed", "qualified_table"),
    ('SELECT x."日期", x."全日客流" FROM "日客流" t', "unparsed", "qualifier"),
]

PROTOTYPE_ACCEPTED = [
    ('SELECT "日期", "全日客流" FROM "日客流" ORDER BY "全日客流" DESC LIMIT 1', ["日期", "全日客流"],
     ("日客流", None, ["日期", "全日客流"])),
    ("SELECT 日期, 全日客流 FROM 日客流 WHERE 日期 = '2026-08-05';", ["日期", "全日客流"],
     ("日客流", None, ["日期", "全日客流"])),
    ('SELECT t."日期", t."全日客流" FROM "日客流" AS t WHERE substr(t."日期", 1, 7) = \'2026-08\'',
     ["日期", "全日客流"], ("日客流", "t", ["日期", "全日客流"])),
    ('SELECT * FROM "日客流" WHERE "日期" = \'2026-08-05\'', DAILY, ("日客流", None, DAILY)),
    ('-- FROM 别的表\nSELECT "日期", "客流" FROM "时段客流" /* JOIN x */ WHERE "时段" = \'8-9\'', ["日期", "客流"],
     ("时段客流", None, ["日期", "客流"])),
    ('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" = \'x UNION SELECT\'', ["日期", "全日客流"],
     ("日客流", None, ["日期", "全日客流"])),
]


def test_the_prototype_has_24_cases():
    assert len(PROTOTYPE_REFUSED) + len(PROTOTYPE_ACCEPTED) == 24


@pytest.mark.parametrize(("sql", "code", "detail"), PROTOTYPE_REFUSED)
def test_prototype_refusals(sql, code, detail):
    no(sql, code, detail)


@pytest.mark.parametrize(("sql", "result", "expect"), PROTOTYPE_ACCEPTED)
def test_prototype_acceptances(sql, result, expect):
    out = ok(sql, result)
    assert (out.table, out.alias, out.columns) == expect


@pytest.mark.parametrize(("sql", "result", "expect"), PROTOTYPE_ACCEPTED)
def test_prototype_acceptances_in_the_stored_form(sql, result, expect):
    """守卫去掉注释和尾分号之后（存进查询快照的形态）结论相同。"""
    out = ok(stored(sql), result)
    assert (out.table, out.alias, out.columns) == expect


# ---------------------------------------------------------------------------
# B7：多方言扫描看不见、SQLite 照样执行的集合运算（评审甲的 bs.py、br.py）
# ---------------------------------------------------------------------------

B7_COMPOUND = [
    # bs.py 的 3 条：ESCAPE '\' 在多方言扫描里是「转义了的引号」，后面整段被当成字符串
    """SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" LIKE '2026-08%' ESCAPE '\\' UNION ALL """
    """SELECT "日期", "分区甲" FROM "日客流" WHERE "日期" LIKE '2026-09%'""",
    """SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" LIKE '%\\_%' ESCAPE '\\' OR 1 UNION ALL """
    """SELECT "日期", "分区甲" FROM "日客流" WHERE '1' = '1'""",
    """SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" = '\\' UNION ALL """
    """SELECT "日期", "分区甲" FROM "日客流" WHERE '1'='1'""",
    # br.py 的第 1 条：方括号里含引号，多方言扫描不当标识符，[t'] … [u'] 被当成一段
    """SELECT "日期", "全日客流" FROM "日客流" AS [t'] WHERE "日期" > '2026-09' UNION ALL """
    """SELECT "日期", "分区甲" FROM "日客流" AS [u']""",
    # br.py 的第 2 条
    """SELECT "日期", "全日客流" FROM "日客流" AS t ORDER BY [x'] UNION ALL """
    """SELECT "日期","分区甲" FROM "日客流" ORDER BY [y']""",
]


@pytest.mark.parametrize("sql", B7_COMPOUND)
def test_b7_compound_hidden_behind_backslash_or_bracket_is_refused(sql):
    no(stored(sql), "multi_table", "compound")


def test_b7_join_hidden_behind_escape_backslash_is_refused():
    no(stored("""SELECT a."日期", a."全日客流" LIKE '%' ESCAPE '\\', a."分区甲" FROM "日客流" a """
              """JOIN "日客流" b ON a."日期" = b."日期\""""), "multi_table", "join")
    no(stored("""SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" LIKE '%' ESCAPE '\\' """
              """CROSS JOIN "时段客流\""""), "multi_table", "join")


def test_b7_comma_join_after_escape_backslash_is_refused():
    # br.py 的第 3 条
    no(stored("""SELECT a."日期", b."全日客流" FROM "日客流" a, "日客流" b WHERE a."日期" = b."日期" """
              """AND a."日期" LIKE '%' ESCAPE '\\' """), "multi_table", "comma_join")


def test_b7_would_slip_through_the_multi_dialect_scan():
    """反证：同一串 SQL 在多方言扫描（evidence._sql_scan 的读法）里看不见 UNION，判据换回那种读法就会放过它。
    这条钉住「判据必须用 SQLite 读法」的理由，换读法的变异由上面两条抓。"""
    from app.engine.evidence import _SQL, _sql_scan

    code, _ = _sql_scan(stored(B7_COMPOUND[0]), _SQL[2])
    assert "UNION" not in code.upper()
    seen = " ".join(t.text for t in guard._tokens(stored(B7_COMPOUND[0]), guard._SQLITE) if t.kind == "code")
    assert "UNION" in seen.upper()


# ---------------------------------------------------------------------------
# 守卫读法的三个性质（P4-SPEC 2.4）：守卫以后改读法时这里先失败
# ---------------------------------------------------------------------------


def _kinds(sql: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text) for t in guard._tokens(sql, guard._SQLITE)]


def test_sqlite_reading_backslash_does_not_escape_in_strings():
    assert _kinds("'a\\' x") == [("string", "'a\\'"), ("code", " x")]


def test_sqlite_reading_brackets_are_identifiers():
    assert _kinds("[t'] x") == [("ident", "[t']"), ("code", " x")]


def test_sqlite_reading_brackets_end_at_the_first_closing_bracket():
    assert _kinds("[a]]b]") == [("ident", "[a]"), ("code", "]b]")]
    assert _kinds('[a""b]') == [("ident", '[a""b]')]


def test_bracket_names_are_taken_verbatim_and_doubled_quotes_collapse_elsewhere():
    tables = {'a""b': ["x"], 'c"d': ["y"], "e`f": ["z"]}
    assert ok('SELECT x FROM [a""b]', ["x"], tables=tables).table == 'a""b'
    assert ok('SELECT y FROM "c""d"', ["y"], tables=tables).table == 'c"d'
    assert ok("SELECT z FROM `e``f`", ["z"], tables=tables).table == "e`f"


# ---------------------------------------------------------------------------
# 字面量关键字、重名、位置对应
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["CURRENT_DATE", "current_time", "Current_Timestamp", "TRUE", "false", "NULL"])
def test_literal_keywords_are_refused(word):
    no(f'SELECT {word}, "全日客流" FROM "日客流"', "expression", "literal_keyword")
    no(f'SELECT "日期", {word} FROM "日客流"', "expression", "literal_keyword")


def test_literal_keywords_are_refused_even_when_the_table_has_such_a_column():
    # TRUE / FALSE 有同名列时才指那一列：读法取决于表结构，一律拒
    tables = {"t": ["true", "x"]}
    no("SELECT TRUE, x FROM t", "expression", "literal_keyword", result=["true", "x"], tables=tables)
    # 加了引号就是名字
    assert ok('SELECT "true", x FROM t', ["true", "x"], tables=tables).columns == ["true", "x"]


def test_two_items_naming_the_same_column_are_ambiguous():
    no('SELECT "日期", "日期" FROM "日客流"', "unparsed", "ambiguous_column", result=["日期", "日期"])
    no("SELECT Key, key FROM abc", "unparsed", "ambiguous_column", result=["Key", "key"])
    no('SELECT t."日期", "日期" FROM "日客流" t', "unparsed", "ambiguous_column", result=["日期", "日期"])


def test_result_columns_follow_the_items_by_position():
    """第 i 个结果列就是第 i 个选取项（P4-SPEC 2.4）。SQLite 对 `abc.key` 返回定义名 Key（附录 B2）。"""
    out = ok("SELECT abc.key, ABC.V FROM abc", ["Key", "v"])
    assert out.columns == ["Key", "v"]
    out = ok('SELECT "分区乙", "日期", "分区甲" FROM "日客流"', ["分区乙", "日期", "分区甲"])
    assert out.columns == ["分区乙", "日期", "分区甲"]


def test_result_columns_that_do_not_line_up_with_the_items_are_refused():
    """名字只作一致性核对：结果列的名字和对应位置的列对不上，或者个数不等，说明结果不是这条 SQL 的。
    按名字对应（变异）会把第一条认成 ['分区甲', '日期']。"""
    no('SELECT "日期", "分区甲" FROM "日客流"', "unparsed", "unknown_column", result=["分区甲", "日期"])
    no('SELECT "日期" FROM "日客流"', "unparsed", "unknown_column", result=["日期", "全日客流"])
    no('SELECT "日期", "全日客流" FROM "日客流"', "unparsed", "unknown_column", result=["日期"])


def test_star_must_match_the_table_columns_in_order():
    assert ok('SELECT * FROM "日客流"', DAILY).columns == DAILY
    # 结果列的大小写不同也认（name_key），返回的是定义名
    assert ok("SELECT * FROM abc", ["key", "V"]).columns == ["Key", "v"]
    no('SELECT * FROM "日客流"', "unparsed", "star_mismatch", result=["全日客流", "日期", "分区甲", "分区乙"])
    no('SELECT * FROM "日客流"', "unparsed", "star_mismatch", result=DAILY[:3])
    no('SELECT * FROM "日客流"', "unparsed", "star_mismatch", result=[*DAILY, "多一列"])


def test_star_is_only_allowed_alone():
    no('SELECT *, "日期" FROM "日客流"', "unparsed", "star_mismatch", result=[*DAILY, "日期"])
    no('SELECT "日期", * FROM "日客流"', "unparsed", "star_mismatch", result=["日期", *DAILY])
    no('SELECT t.* FROM "日客流" t', "unparsed", "star_mismatch", result=DAILY)


# ---------------------------------------------------------------------------
# 名字：引号、中文、大小写、空白
# ---------------------------------------------------------------------------


def test_unquoted_chinese_names():
    out = ok("SELECT 日期, 分区甲 FROM 日客流 WHERE 分区甲 > 3 ORDER BY 日期", ["日期", "分区甲"])
    assert (out.table, out.columns) == ("日客流", ["日期", "分区甲"])


def test_backticks_and_brackets():
    out = ok("SELECT `日期`, [全日客流] FROM [日客流] AS `t` WHERE [t].`日期` = '2026-08-01'", ["日期", "全日客流"])
    assert (out.table, out.alias, out.columns) == ("日客流", "t", ["日期", "全日客流"])


def test_names_are_case_insensitive_only_for_ascii():
    out = ok("SeLeCt ABC.KEY, v FrOm Abc", ["Key", "v"])
    assert (out.table, out.columns) == ("abc", ["Key", "v"])
    # 别名同样只对 ASCII 不区分大小写；返回的别名是原写法
    out = ok('SELECT "T".日期 FROM 日客流 AS t', ["日期"])
    assert out.alias == "t"
    cyr = {"Дата": ["Сумма", "Итог"]}
    no("SELECT Сумма FROM дата", "unparsed", "unknown_table", result=["Сумма"], tables=cyr)
    no("SELECT сумма FROM Дата", "unparsed", "unknown_column", result=["сумма"], tables=cyr)
    assert ok("SELECT Сумма, Итог FROM Дата", ["Сумма", "Итог"], tables=cyr).columns == ["Сумма", "Итог"]


def test_fullwidth_names_are_not_folded():
    # name_key 不做 NFKC：全角 ＡＢＣ 和 abc 是两张表
    no("SELECT Key FROM ａｂｃ", "unparsed", "unknown_table", result=["Key"])


def test_newlines_tabs_and_form_feeds_are_whitespace():
    sql = 'SELECT\n\t"日期",\r\n\t"全日客流"\fFROM\t"日客流"\nWHERE "日期" = \'2026-08-01\'\n'
    assert ok(sql, ["日期", "全日客流"]).columns == ["日期", "全日客流"]


def test_fullwidth_space_is_part_of_a_name():
    """除记号开头的 U+FEFF 之外，SQLite 把码位 ≥ 0x80 的字符一律算名字（2 版实体语法同样如此，evidence._SqlSyntax）：
    `日客流\u3000t` 是一张叫这个名字的表，不是表名加别名；`日客流\u3000WHERE` 也不是表名加 WHERE。"""
    no("SELECT 日期, 全日客流 FROM 日客流\u3000t", "unparsed", "unknown_table")
    no("SELECT 日期, 全日客流 FROM 日客流\u3000WHERE", "unparsed", "unknown_table")
    # 「日客流\u3000WHERE」是表名，「日期」成了它的别名，后面的「=」不是尾部的开头
    no("SELECT 日期, 全日客流 FROM 日客流\u3000WHERE 日期 = 'x'", "unparsed", "tail_shape")
    tables = {"日客流": ["日期\u3000x", "全日客流"]}
    assert ok("SELECT 日期\u3000x FROM 日客流", ["日期\u3000x"], tables=tables).columns == ["日期\u3000x"]
    # 不换行空格同理：「日期\u00a0FROM」是一个名字，「日客流」成了它的别名，整条语句没有 FROM
    no("SELECT 日期\u00a0FROM 日客流", "alias", "alias", result=["日客流"])


def test_vertical_tab_reads_as_whitespace():
    """竖制表符出现在记号开头时 SQLite 报非法记号，紧跟其他空白时 SQLite 当空白（CC_SPACE 的循环用 sqlite3Isspace，
    含 \\v）。判据一律当空白：前一种执行不了、不会留下查询快照；后一种读法相同，藏在它后面的 UNION ALL 照样看得见。
    当成标点时，紧跟在它后面的单引号表名 tables_in 会漏（见 tables_in 一节）。"""
    no(stored('SELECT "日期", "全日客流" FROM "日客流" WHERE 1 \x0bUNION ALL SELECT "日期", "分区甲" FROM "日客流"'),
       "multi_table", "compound")
    no(stored('SELECT "日期", "全日客流" FROM "日客流" WHERE 1 \x0bGROUP BY "日期"'), "expression", "expression")
    out = ok(stored('SELECT \x0b"日期", \x0b"全日客流" FROM \x0b"日客流" \x0bWHERE 1'), ["日期", "全日客流"])
    assert (out.table, out.alias, out.columns) == ("日客流", None, ["日期", "全日客流"])
    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.Error):
        conn.execute("SELECT\x0b1")
    assert conn.execute("SELECT 1 \x0bUNION ALL SELECT 2").fetchall() == [(1,), (2,)]


BOM = "\ufeff"

#: 评审实测（bom.py、attacks.py）：守卫照样放行，SQLite 照样执行，前两条的全日客流一列里混着分区甲的值
BOM_HIDDEN = [
    (f'SELECT "日期", "全日客流" FROM "日客流" WHERE 1 {BOM}UNION ALL {BOM}SELECT "日期", "分区甲" {BOM}FROM "日客流"',
     "multi_table", "compound"),
    (f'SELECT "日期", "全日客流" FROM "日客流" WHERE 1 {BOM}UNION {BOM}SELECT "日期", "分区甲" {BOM}FROM "日客流"',
     "multi_table", "compound"),
    (f'SELECT "日期", "全日客流" FROM "日客流" WHERE 1 {BOM}GROUP BY "日期"', "expression", "expression"),
]


@pytest.mark.parametrize(("sql", "code", "detail"), BOM_HIDDEN)
def test_bom_at_a_token_start_is_whitespace_so_the_keyword_behind_it_counts(sql, code, detail):
    """U+FEFF 出现在记号开头时 SQLite 当空白（tokenize.c 的 CC_BOM）。照「码位 ≥ 0x80 一律算名字」读，紧跟在它后面的
    UNION 就成了一个普通的词，判据把这几条认成直接选取。"""
    no(stored(sql), code, detail)


def test_bom_between_tokens_reads_like_a_space():
    """记号开头指：紧跟在空白、标点、带引号的名字、字符串后面。紧跟在不加引号的词后面就进了那个词（`SELECT\ufeff`
    是一个名字，SQLite 报语法错误），见下一条。"""
    sql = stored(f'SELECT {BOM}"日期",{BOM}"全日客流" {BOM}FROM {BOM}"日客流"{BOM} WHERE 1')
    out = ok(sql, ["日期", "全日客流"])
    assert (out.table, out.alias, out.columns) == ("日客流", None, ["日期", "全日客流"])
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE "日客流" ("日期", "全日客流")')
    assert [d[0] for d in conn.execute(sql).description] == ["日期", "全日客流"]
    with pytest.raises(sqlite3.Error):
        conn.execute(f'SELECT{BOM} "日期" FROM "日客流"')
    # 紧跟在带引号的名字、标点后面，单独一个 U+FEFF 也是记号开头
    no(stored(f'SELECT "日期", "全日客流" FROM "日客流"{BOM}UNION ALL SELECT "日期", "分区甲" FROM "日客流"'),
       "multi_table", "compound")
    no(stored(f'SELECT "日期",{BOM}"分区甲"{BOM}AS "全日客流" FROM "日客流"'), "alias", "alias")


def test_bom_inside_a_name_is_part_of_the_name():
    """名字中间（含末尾）的 U+FEFF 是名字的一部分，同 SQLite：`日客流\ufeff` 是另一张表，`UNION\ufeff` 不是关键字。"""
    tables = {"日客流": [f"日{BOM}期", "全日客流"]}
    assert ok(f"SELECT 日{BOM}期 FROM 日客流", [f"日{BOM}期"], tables=tables).columns == [f"日{BOM}期"]
    no(f"SELECT 全日客流 FROM 日客流{BOM}", "unparsed", "unknown_table", result=["全日客流"], tables=tables)
    # 「日期\ufeffFROM」是一个名字，「日客流」成了它的别名，整条语句没有 FROM
    no(f"SELECT 日期{BOM}FROM 日客流", "alias", "alias", result=["日客流"])
    conn = sqlite3.connect(":memory:")
    conn.execute(f'CREATE TABLE "日客流" ("日{BOM}期", "全日客流")')
    conn.execute("INSERT INTO 日客流 VALUES (1, 2)")
    assert conn.execute(f"SELECT 日{BOM}期 FROM 日客流").fetchall() == [(1,)]
    with pytest.raises(sqlite3.Error):
        conn.execute(f"SELECT 全日客流 FROM 日客流{BOM}")


# ---------------------------------------------------------------------------
# 语句个数、注释、字符串
# ---------------------------------------------------------------------------


def test_trailing_semicolons_are_allowed():
    assert ok('SELECT "日期" FROM "日客流";', ["日期"]).columns == ["日期"]
    assert ok('SELECT "日期" FROM "日客流" ; ; ', ["日期"]).columns == ["日期"]


def test_two_statements_are_refused():
    no('SELECT "日期" FROM "日客流"; SELECT "日期" FROM "时段客流"', "unparsed", "multi_statement", result=["日期"])
    no('SELECT "日期" FROM "日客流"; DROP TABLE x', "unparsed", "multi_statement", result=["日期"])


def test_keywords_inside_comments_and_strings_do_not_count():
    sql = ('SELECT "日期", "全日客流" /* JOIN "时段客流" */ FROM "日客流" -- UNION ALL SELECT\n'
           "WHERE \"日期\" <> 'UNION SELECT x FROM y JOIN z' AND \"日期\" <> 'GROUP BY'")
    assert ok(sql, ["日期", "全日客流"]).columns == ["日期", "全日客流"]


def test_keywords_inside_quoted_names_do_not_count():
    tables = {"union": ["select", "join"]}
    out = ok('SELECT "select", [join] FROM "union"', ["select", "join"], tables=tables)
    assert (out.table, out.columns) == ("union", ["select", "join"])


def test_comments_split_words_like_sqlite_does():
    # SQLite 把注释当空白：SEL/**/ECT 是两个词，不是 SELECT
    no('SEL/**/ECT "日期" FROM "日客流"', "unparsed", "not_select", result=["日期"])


def test_unterminated_quotes_are_refused():
    no('SELECT "日期" FROM "日客流', "unparsed", "not_select")
    # 反例：`"日期 FROM "` 是一个闭合的名字，后面的「日客流」是它的别名
    no('SELECT "日期 FROM "日客流', "alias", "alias")
    no("SELECT 日期 FROM 日客流 WHERE 日期 = 'x", "unparsed", "not_select")
    no("SELECT [日期 FROM 日客流", "unparsed", "not_select")


def test_empty_and_non_select_statements_are_refused():
    for sql in ("", "   ", ";", "VALUES (1)", "PRAGMA table_info(日客流)", "EXPLAIN SELECT 日期 FROM 日客流",
                "(SELECT 日期 FROM 日客流)"):
        no(sql, "unparsed", "not_select")


# ---------------------------------------------------------------------------
# 选取项、FROM、尾部的其余写法
# ---------------------------------------------------------------------------


def test_where_subquery_and_values_are_refused():
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" IN (SELECT "日期" FROM "日客流")', "multi_table",
       "subquery")
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE EXISTS (SELECT 1)', "multi_table", "subquery")
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" IN (VALUES (\'a\'))', "multi_table", "subquery")
    no('SELECT "日期", (SELECT 1) FROM "日客流"', "multi_table", "subquery")


def test_join_family_words_are_refused_wherever_they_appear():
    for word in ("JOIN", "NATURAL JOIN", "CROSS JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "RIGHT JOIN",
                 "FULL JOIN"):
        no(f'SELECT a."日期", a."全日客流" FROM "日客流" a {word} "时段客流" b', "multi_table", "join")


def test_compound_and_cte_words_are_refused():
    for word in ("UNION", "UNION ALL", "INTERSECT", "EXCEPT"):
        no(f'SELECT "日期", "全日客流" FROM "日客流" {word} SELECT "日期", "分区甲" FROM "日客流"', "multi_table",
           "compound")
    no('WITH RECURSIVE x(n) AS (SELECT 1) SELECT "日期", "全日客流" FROM "日客流"', "multi_table", "cte")
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" IN (WITH x AS (SELECT 1) SELECT * FROM x)',
       "multi_table", "cte")


def test_window_clause_is_refused():
    no('SELECT "日期", "全日客流" FROM "日客流" WINDOW w AS (ORDER BY "日期")', "expression", "window")


def test_aggregation_in_the_tail_is_refused():
    no('SELECT "日期", "全日客流" FROM "日客流" GROUP BY "日期"', "expression", "expression")
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE 1 GROUP BY "日期" HAVING 1', "expression", "expression")


def test_is_distinct_from_in_the_tail_is_outside_this_grammar():
    no('SELECT "日期", "全日客流" FROM "日客流" WHERE "日期" IS DISTINCT FROM NULL', "unparsed", "tail_shape")


@pytest.mark.parametrize(("item", "detail"), [
    ('CASE WHEN 1 THEN "全日客流" END', "expression"),
    ('CAST("全日客流" AS TEXT)', "expression"),
    ('"全日客流" IS NULL', "expression"),
    ('"全日客流" BETWEEN 1 AND 2', "expression"),
    ('-"全日客流"', "expression"),
    ("+全日客流", "expression"),
    ("(全日客流)", "expression"),
    ("全日客流 || ''", "expression"),
    ("x'00'", "expression"),
    ("1", "expression"),
    ("1.5", "expression"),
    ('abs("全日客流")', "expression"),
    ('"abs"("全日客流")', "expression"),
    ("'全日客流'", "string_literal"),
    ('"全日客流" COLLATE BINARY', "collate"),
])
def test_items_that_are_not_bare_columns(item, detail):
    no(f'SELECT "日期", {item} FROM "日客流"', "expression", detail)


@pytest.mark.parametrize("item", [
    '"全日客流" AS "分区甲"', '"全日客流" AS x', '"全日客流" "分区甲"', "全日客流 分区甲", "\"全日客流\" '分区甲'",
    '"全日客流" [分区甲]', '"全日客流" `分区甲`', "t.全日客流 x",
])
def test_renamed_items_are_aliases(item):
    no(f'SELECT "日期", {item} FROM "日客流" t', "alias", "alias")


def test_qualifier_must_name_this_table():
    assert ok('SELECT "日客流"."日期", 日客流.全日客流 FROM "日客流"', ["日期", "全日客流"]).alias is None
    no('SELECT "时段客流"."日期", "全日客流" FROM "日客流"', "unparsed", "qualifier")
    no('SELECT a."日期", "全日客流" FROM "日客流" AS b', "unparsed", "qualifier")
    # 起了别名之后原表名不能再当限定名（SQLite 报 no such column），只认别名
    no('SELECT 日客流."日期", "全日客流" FROM "日客流" AS t', "unparsed", "qualifier")
    # 带库名的限定名
    no('SELECT main."日客流"."日期", "全日客流" FROM "日客流"', "unparsed", "qualifier")


def test_table_shapes():
    no('SELECT "日期", "全日客流" FROM temp."日客流"', "unparsed", "qualified_table")
    no('SELECT "日期", "全日客流" FROM pragma_table_info(\'日客流\')', "unparsed", "table_shape")
    # FROM (日客流) 在 SQLite 里能执行（括起来的表列表），一律不认
    no('SELECT "日期", "全日客流" FROM ("日客流")', "multi_table", "subquery")
    # 字符串当表名、当表别名：SQLite 的历史兼容，这一版文法不认
    no("SELECT \"日期\", \"全日客流\" FROM '日客流'", "unparsed", "table_shape")
    no("SELECT \"日期\", \"全日客流\" FROM \"日客流\" AS 't'", "unparsed", "table_shape")
    no('SELECT "日期", "全日客流"', "unparsed", "table_shape")
    no('SELECT "日期", "全日客流" FROM', "unparsed", "table_shape")
    no('SELECT "日期", "全日客流" FROM WHERE', "unparsed", "table_shape")


def test_tail_shapes():
    no('SELECT "日期", "全日客流" FROM "日客流" NOT INDEXED', "unparsed", "tail_shape")
    no('SELECT "日期", "全日客流" FROM "日客流" t u', "unparsed", "tail_shape")
    no("SELECT \"日期\", \"全日客流\" FROM \"日客流\" 't'", "unparsed", "tail_shape")
    no('SELECT "日期", "全日客流" FROM "日客流" t, "时段客流" u', "multi_table", "comma_join")
    no('SELECT "日期", "全日客流" FROM "日客流", "时段客流"', "multi_table", "comma_join")


def test_accepted_tails():
    for tail in ("", " WHERE 1", " ORDER BY 2 DESC, 1", " LIMIT 3 OFFSET 1", " LIMIT 1, 2",
                 " WHERE \"日期\" BETWEEN '2026-08-01' AND '2026-08-31' ORDER BY \"日期\" COLLATE NOCASE LIMIT 5",
                 " WHERE \"日期\" LIKE '2026\\_%' ESCAPE '\\' AND \"分区甲\" IN (1, 2, 3)",
                 " WHERE CAST(\"分区甲\" AS TEXT) GLOB '1*' OR \"分区乙\" IS NULL",
                 " WHERE (CASE WHEN \"分区甲\" > 1 THEN 1 ELSE 0 END) = 1"):
        out = ok(f'SELECT "日期", "全日客流" FROM "日客流"{tail}', ["日期", "全日客流"])
        assert out.columns == ["日期", "全日客流"], tail


def test_select_all_is_allowed():
    assert ok('SELECT ALL "日期" FROM "日客流"', ["日期"]).columns == ["日期"]
    assert ok('SELECT ALL * FROM "日客流"', DAILY).columns == DAILY


def test_unknown_table_and_column():
    no('SELECT "日期" FROM "不存在"', "unparsed", "unknown_table", result=["日期"])
    no('SELECT "不存在" FROM "日客流"', "unparsed", "unknown_column", result=["不存在"])
    # rowid 不是冻结表结构里的列
    no('SELECT rowid, "日期" FROM "日客流"', "unparsed", "unknown_column", result=["rowid", "日期"])


def test_colliding_names_in_the_schema_are_treated_as_unknown():
    # 冻结表结构里不该出现：说不清是哪一个，按找不到处理
    no("SELECT x FROM T", "unparsed", "unknown_table", result=["x"], tables={"t": ["x"], "T": ["x"]})
    no("SELECT x FROM t", "unparsed", "unknown_column", result=["x"], tables={"t": ["x", "X"]})


def test_forbidden_words_are_the_spec_table():
    assert D.FORBIDDEN == {
        "select", "from", "join", "union", "intersect", "except", "with", "over", "window", "group", "having",
        "values", "natural", "cross", "inner", "left", "right", "full", "outer", "distinct", "recursive"}


# ---------------------------------------------------------------------------
# tables_in（P4-SPEC 2.4）
# ---------------------------------------------------------------------------


def test_tables_in_sees_tables_hidden_behind_escape_backslash():
    sql = stored("""SELECT "日期", "客流" FROM "时段客流" WHERE "时段" LIKE '%' ESCAPE '\\' UNION ALL """
                 """SELECT "日期", "客流" FROM "时段客流_表内合计\"""")
    assert tables_in(sql, TABLES) == ["时段客流", "时段客流_表内合计"]
    # 2 版 sql_tables 用多方言读法，这里漏掉了合计表：这正是 tables_in 存在的理由
    from app.engine.evidence import sql_tables
    assert "时段客流_表内合计" not in sql_tables(sql)


def test_tables_in_bracket_backtick_and_unquoted_chinese_names():
    assert tables_in("SELECT * FROM [时段客流] JOIN `日客流` USING (日期) JOIN 时段客流_表内合计", TABLES) == [
        "时段客流", "日客流", "时段客流_表内合计"]
    assert tables_in("SELECT * FROM [t'] UNION ALL SELECT * FROM 日客流", {**TABLES, "t'": ["x"]}) == [
        "t'", "日客流"]


def test_tables_in_ignores_names_only_in_strings_and_comments():
    assert tables_in("SELECT * FROM 日客流 WHERE 分区 = '时段客流' -- 时段客流_表内合计", TABLES) == ["日客流"]
    assert tables_in("SELECT '时段客流', \"日期\" FROM 日客流 /* abc */", TABLES) == ["日客流"]


def test_tables_in_counts_strings_where_sqlite_reads_them_as_table_names():
    # `FROM '时段客流'` 在 SQLite 里能执行（历史兼容）：读到的就是这张表
    assert tables_in("SELECT 日期 FROM 日客流 UNION ALL SELECT 日期 FROM '时段客流'", TABLES) == [
        "日客流", "时段客流"]
    assert tables_in("SELECT a.日期 FROM 日客流 AS a, '时段客流' b", TABLES) == ["日客流", "时段客流"]
    assert tables_in("SELECT 日期 FROM main.'时段客流_表内合计'", TABLES) == ["时段客流_表内合计"]
    assert tables_in("SELECT 1 FROM 日客流 a JOIN '时段客流' b ON 1", TABLES) == ["日客流", "时段客流"]


def test_tables_in_counts_a_string_right_after_in():
    """SQLite 文法 `expr IN nm` 的 nm 可以是字符串：`日期 IN '单列表'` 读的是那张表（评审 attacks.py 的 in table string）。
    括号里的字符串是值列表，不算。"""
    tables = {**TABLES, "单列": ["日期"]}
    assert tables_in("SELECT 日期, 全日客流 FROM 日客流 WHERE 日期 IN '单列'", tables) == ["日客流", "单列"]
    assert tables_in("SELECT 日期 FROM 日客流 WHERE 日期 NOT IN '单列'", tables) == ["日客流", "单列"]
    assert tables_in("SELECT 日期 FROM 日客流 WHERE 日期 IN ('单列', '时段客流')", tables) == ["日客流"]


def test_tables_in_sees_tables_behind_bom_and_vertical_tab():
    """「紧跟」按 SQLite 的分词算：记号开头的 U+FEFF、紧跟其他空白的竖制表符都是空白，不是记号（评审 bom.py、vt.py）。
    读错了，紧跟在后面的合计表就漏掉，裁判拿不到「不要相加」的口径行。"""
    head = 'SELECT "日期", "全日客流" FROM "日客流" UNION ALL SELECT "日期", "客流" '
    expect = ["日客流", "时段客流_表内合计"]
    assert tables_in(stored(f"{head}{BOM}FROM {BOM}时段客流_表内合计"), TABLES) == expect
    assert tables_in(stored(f"{head}FROM {BOM}'时段客流_表内合计'"), TABLES) == expect
    assert tables_in(stored(f"{head}FROM \x0b'时段客流_表内合计'"), TABLES) == expect
    assert tables_in(stored(f"{head}FROM main.{BOM}'时段客流_表内合计'"), TABLES) == expect
    tables = {**TABLES, "单列": ["日期"]}
    assert tables_in(stored(f"SELECT 日期 FROM 日客流 WHERE 日期 IN {BOM}'单列'"), tables) == ["日客流", "单列"]
    # 名字中间的 U+FEFF 是名字的一部分：「时段客流_表内合计\ufeff」不是那张表，SQLite 也报找不到
    assert tables_in(f"{head}FROM 时段客流_表内合计{BOM}", TABLES) == ["日客流"]


def test_tables_in_is_ordered_deduplicated_and_returns_schema_names():
    assert tables_in("select * from ABC, 日客流, abc, \"Abc\"", TABLES) == ["abc", "日客流"]
    assert tables_in("SELECT 1", TABLES) == []
    assert tables_in("", TABLES) == []


def test_tables_in_errs_on_the_side_of_more():
    # 列名恰好和某张表同名时多出这张表：后果只是多一行说明
    tables = {"日客流": ["日期", "时段客流"], "时段客流": ["日期"]}
    assert tables_in('SELECT "时段客流" FROM "日客流"', tables) == ["时段客流", "日客流"]
    # 引号没闭合也照样认能切出来的部分
    assert tables_in('SELECT * FROM 日客流 WHERE x = "时段客流', TABLES) == ["日客流", "时段客流"]


def test_tables_in_keeps_fullwidth_space_inside_the_name():
    assert tables_in("SELECT * FROM 日客流\u3000WHERE 1", TABLES) == []
    assert tables_in("SELECT * FROM 日客流 WHERE 1", TABLES) == ["日客流"]


# ---------------------------------------------------------------------------
# 模块边界、不抛异常
# ---------------------------------------------------------------------------


def test_module_imports_only_guard_names_and_the_contract():
    """P4-SPEC 7.2：判据是纯函数，只许导入守卫、names 和 provenance_types（加标准库），不依赖证据层、接口层。"""
    tree = ast.parse(Path(D.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__", "collections.abc", "dataclasses", "app.data.guard", "app.data.names",
                        "app.data.provenance_types"}, imported


_PIECES = [
    "SELECT", "FROM", "WHERE", "ORDER", "BY", "LIMIT", "AS", "JOIN", "UNION", "ALL", "DISTINCT", "*", ",", ".", "(",
    ")", ";", "'", '"', "[", "]", "`", "\\", "--", "/*", "*/", "日期", "全日客流", '"日客流"', "日客流", "t", "x'0'",
    "1", "NULL", "\u3000", "\n", " ", "COLLATE", "OVER", "abc", "KEY", "=", "'a''b'", '"a""b"', "[t']", "ESCAPE",
]


def test_recognize_never_raises_and_every_refusal_is_mapped():
    rng = random.Random(20261002)
    for _ in range(3000):
        sql = " ".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 14)))
        if rng.random() < 0.5:
            sql = "SELECT " + sql
        result = rng.choice([["日期"], ["日期", "全日客流"], DAILY, [], ["Key", "v"]])
        out = recognize(sql, result_columns=result, tables=TABLES)
        assert isinstance(out, (DirectSelect, SelectRefusal)), sql
        if isinstance(out, SelectRefusal):
            assert out.detail in P.DETAILS[out.code], (sql, out)
        else:
            assert out.table in TABLES and all(c in TABLES[out.table] for c in out.columns), (sql, out)
        assert isinstance(tables_in(sql, TABLES), list)


# ---------------------------------------------------------------------------
# 拿真的 SQLite 核对：认可的每一列，值都来自它说的那一列
# ---------------------------------------------------------------------------

_DB_TABLES = {
    "日客流": ["日期", "全日客流", "分区甲", "分区乙"],
    "时段客流": ["日期", "时段", "客流"],
    "abc": ["Key", "v"],
}


def _database(rng: random.Random, tables: dict[str, list[str]] | None = None
              ) -> tuple[sqlite3.Connection, dict[object, tuple[str, str]]]:
    """每一格的值全库唯一：结果里的任何一个值都能反查出它来自哪张表的哪一列。DQS 关掉，同 engine。"""
    conn = sqlite3.connect(":memory:")
    if hasattr(conn, "setconfig"):
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
    origin: dict[object, tuple[str, str]] = {}
    values = iter(rng.sample(range(100_000, 999_999), 200))
    for table, cols in (tables or _DB_TABLES).items():
        quoted = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f'CREATE TABLE "{table}" ({quoted})')
        for day in range(1, 6):
            row = []
            for c in cols:
                v: object = f"2026-08-{day:02d}#{table}" if c == "日期" else next(values)
                origin[v] = (table, c)
                row.append(v)
            conn.execute(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(cols))})', row)
    return conn, origin


_SELECTS = [
    '"日期", "全日客流"', '"日期", "分区甲"', "日期, 分区乙", 't."日期", t."全日客流"', "*", '"全日客流"',
    '"日期", "分区甲" AS "全日客流"', '"日期" "全日客流"', '"分区甲" "全日客流", "日期"', '"日期", "全日客流" + 0',
    '"日期", 分区甲 AS 全日客流', 'T."日期", "全日客流"', '"全日客流", "日期"', "[日期], `全日客流`",
    '"日期", (SELECT "分区甲")', '"日期", "全日客流" COLLATE NOCASE', '"日期", coalesce("全日客流", 0)',
]
_FROMS = [
    '"日客流"', '"日客流" t', '"日客流" AS t', "日客流 AS [t']", '"日客流" AS "T"', '"日客流" AS t, "日客流" u',
    '"日客流" t JOIN "日客流" u USING ("日期")', '(SELECT "日期", "分区甲" AS "全日客流", "分区乙", "分区甲" FROM "日客流")',
    '"日客流" NOT INDEXED', "'日客流'",
]
_TAILS = [
    "", " WHERE 1", " ORDER BY 1 DESC", " LIMIT 2", " WHERE \"日期\" LIKE '%' ESCAPE '\\'",
    " WHERE \"日期\" = '\\'", " ORDER BY [x']", " WHERE '\\' = '\\'",
    """ UNION ALL SELECT "日期", "分区甲" FROM "日客流\"""",
    """ WHERE "日期" LIKE '%' ESCAPE '\\' UNION ALL SELECT "日期", "分区甲" FROM "日客流\"""",
    """ AS [u'] UNION ALL SELECT "日期", "分区乙" FROM "日客流" AS [v']""",
    """ UNION SELECT "日期", "分区甲" FROM "日客流\"""",
    """ EXCEPT SELECT "日期", "分区甲" FROM "日客流" WHERE 0""",
    """ WHERE "日期" IN (SELECT "日期" FROM "时段客流")""",
    " GROUP BY \"日期\"", " /* UNION ALL SELECT 1 */", " -- JOIN x",
]


def _tables_read(conn: sqlite3.Connection, stmt: str) -> set[str]:
    """SQLite 的授权回调报出的、这条语句读到的表：tables_in 必须一张不漏（宁可多认）。"""
    read: set[str] = set()

    def authorize(action: int, table: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ and table and not table.startswith("sqlite_"):
            read.add(table)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    try:
        conn.execute(stmt).fetchall()
    finally:
        conn.set_authorizer(None)
    return read


def _check_accepted_values(out: DirectSelect, rows: list[tuple], origin: dict[object, tuple[str, str]],
                           stmt: str) -> None:
    for row in rows:
        for i, value in enumerate(row):
            assert origin.get(value) == (out.table, out.columns[i]), (
                f"{stmt!r}：第 {i} 列认作 {out.table}.{out.columns[i]}，值 {value!r} 实际来自 {origin.get(value)}")


def test_every_accepted_column_really_comes_from_the_column_it_names():
    """随机拼 SQL，经守卫、在 SQLite 上真的执行；判据认可的，结果第 i 列的每个值都必须来自 DirectSelect 说的那张表的
    第 i 列。B7 那类写法（集合运算拼进另一列的值）一旦被放过，这里就会报出值的真实来处。授权回调报出的表，
    tables_in 也必须一张不漏。"""
    rng = random.Random(4242)
    conn, origin = _database(rng)
    executed = accepted = 0
    combos = list(itertools.product(_SELECTS, _FROMS, _TAILS))
    rng.shuffle(combos)
    for select, source, tail in combos:
        sql = f"SELECT {select} FROM {source}{tail}"
        try:
            stmt = stored(sql)
            cur = conn.execute(stmt)
        except (guard.SqlRejected, sqlite3.Error):
            continue
        executed += 1
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        read = _tables_read(conn, stmt)
        assert read <= set(tables_in(stmt, _DB_TABLES)), (stmt, read)
        out = recognize(stmt, result_columns=columns, tables=_DB_TABLES)
        if isinstance(out, SelectRefusal):
            assert out.detail in P.DETAILS[out.code], (stmt, out)
            continue
        accepted += 1
        assert len(out.columns) == len(columns), stmt
        _check_accepted_values(out, rows, origin, stmt)
    # 拼出来的大多数都能执行，其中有认可的、也有被拒的：这条测试确实在比对
    assert executed > 500 and 50 < accepted < executed, (executed, accepted)


def test_b7_writings_really_return_another_columns_values_in_sqlite():
    """反证上一条的比对有牙齿：B7 的写法在 SQLite 里确实把分区甲的值放进了全日客流那一列。"""
    rng = random.Random(7)
    conn, origin = _database(rng)
    for sql in B7_COMPOUND[:1] + B7_COMPOUND[3:4]:
        rows = conn.execute(stored(sql).replace("'2026-08%'", "'%'").replace("'2026-09%'", "'%'")
                            .replace("> '2026-09'", "> ''")).fetchall()
        assert {origin[r[1]] for r in rows} == {("日客流", "全日客流"), ("日客流", "分区甲")}, sql


#: 记号之间的间隔：把模板里的 {g} 换成它。紧跟在关键字、数字后面的、紧跟在带引号的名字和标点后面的都有，
#: 记号开头和空白后面两种位置都覆盖到
_GAP_TEMPLATES = [
    'SELECT "日期", "全日客流" FROM "日客流" WHERE "分区甲" > 0{g}UNION ALL{g}SELECT "日期", "分区甲"{g}FROM "日客流"',
    'SELECT "日期", "全日客流" FROM "日客流" WHERE "分区甲" > 0{g}UNION{g}SELECT "日期", "分区甲"{g}FROM "日客流"',
    # 限定名：间隔要是被 SQLite 读成了表别名，「"日客流"."日期"」就找不到列，不会被误当成空白
    'SELECT "日客流"."日期", "全日客流" FROM "日客流"{g}UNION ALL SELECT "日期", "分区乙" FROM "日客流"',
    'SELECT "日期", "全日客流" FROM "日客流" WHERE "分区甲" > 0{g}GROUP BY "日期"',
    'SELECT "日期", "全日客流" FROM "日客流" UNION ALL SELECT "日期", "客流" FROM{g}时段客流',
    "SELECT \"日期\", \"全日客流\" FROM \"日客流\" UNION ALL SELECT \"日期\", \"客流\" FROM{g}'时段客流'",
    "SELECT \"日期\", \"全日客流\" FROM \"日客流\" WHERE \"全日客流\" NOT IN{g}'单列'",
    "SELECT{g}\"日期\",{g}\"全日客流\"{g}FROM{g}\"日客流\"{g}WHERE{g}\"日期\" <> '2026-08-01#日客流'",
]
#: 候选字符：ASCII 控制字符和空格、str.isspace 认而 SQLite 不认的、U+FEFF 及其邻居
_GAP_CHARS = [chr(c) for c in (*range(0x01, 0x21), 0x7F, 0x85, 0xA0, 0x1680, 0x200B, 0x2028, 0x2029, 0x3000, 0xFEFF,
                               0xFFFE)]
#: SQLite 当空白的：记号开头只有这六个；竖制表符只在紧跟其他空白时才是
_SQLITE_SPACE = "\t\n\x0c\r \ufeff"


def test_gaps_that_sqlite_reads_as_whitespace_read_the_same_here():
    """拿真的 SQLite 核对分词：把记号之间的空格换成各种字符，SQLite 能执行的每一条——
    - 认可的，值都来自它说的那一列；授权回调报出的表 tables_in 一张不漏；
    - 结果、列名、字节码都和用空格的那条相同（SQLite 把它当空白读）时，判据的结论和 tables_in 也必须相同。
    最后核对「SQLite 当空白的间隔」恰好是 U+FEFF、五个 ASCII 空白和空白后面的竖制表符：SQLite 升级改了读法，
    这里先失败。评审的 U+FEFF、竖制表符两处都是这条能抓到的。"""
    tables = {**_DB_TABLES, "单列": ["日期"]}
    conn, origin = _database(random.Random(5), tables)
    gaps = sorted({*_GAP_CHARS, *(" " + c for c in _GAP_CHARS), *(c + " " for c in _GAP_CHARS)})

    def run(stmt: str) -> tuple[list[str], list[tuple], list[tuple]]:
        cur = conn.execute(stmt)
        columns, rows = [d[0] for d in cur.description], cur.fetchall()
        # EXPLAIN 的最后一列是注释，会带出原文片段，只比操作码和五个操作数
        program = [tuple(r[1:7]) for r in conn.execute("EXPLAIN " + stmt).fetchall()]
        return columns, rows, program

    whitespace: set[str] = set()
    executed_with: dict[str, int] = {}
    for g in gaps:
        executed = same = 0
        for template in _GAP_TEMPLATES:
            base_stmt = stored(template.format(g=" "))
            try:
                stmt = stored(template.format(g=g))
                columns, rows, program = run(stmt)
            except (guard.SqlRejected, sqlite3.Error):
                continue
            executed += 1
            out = recognize(stmt, result_columns=columns, tables=tables)
            if isinstance(out, DirectSelect):
                _check_accepted_values(out, rows, origin, stmt)
            found = tables_in(stmt, tables)
            assert _tables_read(conn, stmt) <= set(found), (g, stmt, found)
            if (columns, rows, program) == run(base_stmt):
                same += 1
                base = recognize(base_stmt, result_columns=columns, tables=tables)
                assert out == base, f"{g!r}：{stmt!r} → {out!r}，用空格时 → {base!r}"
                assert found == tables_in(base_stmt, tables), (g, stmt)
        executed_with[g] = executed
        if executed and same == executed:
            whitespace.add(g)
    expected = {c for c in _SQLITE_SPACE} | {" " + c for c in _SQLITE_SPACE + "\x0b"} | {c + " " for c in _SQLITE_SPACE}
    assert whitespace == expected, (sorted(map(repr, whitespace - expected)), sorted(map(repr, expected - whitespace)))
    # 有牙齿：空白后面的 U+FEFF、竖制表符在每个模板里都执行了（含藏 UNION ALL、GROUP BY 的那几条）
    assert executed_with[" " + BOM] == executed_with[" \x0b"] == len(_GAP_TEMPLATES)
