"""SQL 差异说明的「等价」结论拿真实执行守门（data/sql_semantics.py 的 explain_sql_diff）。

explain_sql_diff 判两条 SQL 等价，当且仅当两边经过只保持语义的变换之后规范形式一字不差。规范化的每一步都必须
保持语义，可光看代码保证不了（sqlglot 自带的优化器里就有不保持语义的改写），所以这里在虚构的业务库上执行：

- 判了等价的两条，在每组数据上结果都必须一样（结果集按行排序比较）；
- 在某组数据上结果不一样的两条，绝不能判等价；
- 语料里标 diff 的（语义不同，或者只在某个方言、某种排序规则下才等价），一律不能判等价；
- 标 same 的（已知等价），没认出来时只许给兜底说明，不许给具体说法（具体说法说的是两边确实不同的地方）；要靠表结构
  里的约束（主键唯一、不为空）才看得出等价的（Pair.needs_schema）：规范化不靠约束改写，一律不判等价，给了表结构时
  也只许给兜底（约束已经排除了的不同不许说），没给表结构时说得出具体的不同不算错；
- 等价改写（编号不以 hard: 开头的 same）记下认出来的比例：目标是大多数都认得出，但不强求全部。

库和语料见 tests/fixtures/sql_pairs.py：5 组按固定种子生成的数据加一组极端数据、一组专挑写法差异的数据（0.1 + 0.2、
像数字的文本、大小写不同的值），带空值、重复、边界值、已删除的行、退款；语料里有手写的等价改写和语义不同的对，也有
复验者以前造的、在这几组数据上结果不同的对，以及第 2、3、4 轮复验报的反例。每一对都在不给表结构和给表结构（数据源的
schema_cache）两种情况下各判一次，两种都要守住上面的规矩。

第 4 轮复验报的反例另有几组只查结论的（见文件末尾）：只在 PostgreSQL / MySQL 才有的写法（sqlglot 读错了的结合顺序、
MySQL 字符串里的反斜杠转义、数字写法）一律不判等价、只给兜底；靠约束改写的陷阱（TEXT 主键对数值列、COLLATE、部分唯一
索引、MySQL 的隐式转换）在现造的小库上执行、一律不判等价；在 SQLite 上执行的那几对再拿 PostgreSQL / MySQL 的读法
各判一遍，意思一样的只许给兜底。

第 5 轮的反例同样分两种：PostgreSQL 写成符号的 ~ / ~* / ~~ / ~~* / @@ 和 || 混用、前缀的 @、|/、COLLATE 后面跟 ::，
不知道是哪种库时套着的块注释、紧跟着字的 --、双引号括起来的名字，一律不判等价、只给兜底；具体说法说错了的（条件写在聚合里被说成「另一边
没有这个条件」、时间窗口被说成有效行过滤或者「没有按时间筛选」、反连接被说成有效行过滤、按主键定下来的列说 MAX 对
MIN、被规范化去掉的 LEFT JOIN 说成「另一边没有」）在 SQLite 上执行，再拿三种方言各判一遍。
"""
from __future__ import annotations

import pytest

from app.data.sql_semantics import explain_sql_diff
from tests.fixtures import sql_pairs

SCHEMA = sql_pairs.schema_cache()


@pytest.fixture(scope="module")
def databases():
    conns = sql_pairs.build_databases()
    yield conns
    for c in conns:
        c.close()


def _results(databases, sql: str) -> list[list[tuple]]:
    return [sql_pairs.run(c, sql) for c in databases]


def _check_shape(diff, a: str, b: str) -> None:
    """SqlDiff 自己的承诺：等价才没有条目；不等价一定有条目，兜底时一定有 aspect=other 的那条，并给出不同的行。"""
    if diff.equivalent:
        assert diff.items == [] and not diff.fallback and not diff.text_diff, (a, b)
        return
    assert diff.items, (a, b)
    if diff.fallback:
        assert any(d.aspect == "other" for d in diff.items), (a, b, diff.items)
    assert diff.text_diff, (a, b)
    assert len(diff.text_diff) <= 40


@pytest.mark.parametrize("pair", sql_pairs.PAIRS, ids=[p.id for p in sql_pairs.PAIRS])
def test_equivalence_is_guarded_by_execution(databases, pair):
    ra, rb = _results(databases, pair.a), _results(databases, pair.b)
    differs = [i for i, (x, y) in enumerate(zip(ra, rb)) if x != y]
    if pair.expect == "same":
        assert not differs, f"语料标错了：{pair.id} 在第 {differs} 组数据上结果不同"
    for schema in (None, SCHEMA):
        diff = explain_sql_diff(pair.a, pair.b, "sqlite", schema=schema)
        _check_shape(diff, pair.a, pair.b)
        if differs:
            assert not diff.equivalent, f"{pair.id}：第 {differs} 组数据上结果不同，却判了等价（schema={bool(schema)}）"
        if pair.expect == "diff":
            assert not diff.equivalent, f"{pair.id} 不该判等价（schema={bool(schema)}）"
        if pair.expect == "same" and not diff.equivalent and not (pair.needs_schema and schema is None):
            specific = [d.text for d in diff.items if d.aspect != "other"]
            assert not specific, f"{pair.id} 意思一样，却说了具体的不同（schema={bool(schema)}）：{specific}"
        if pair.needs_schema:
            # 约束（主键唯一、不为空）不可靠，规范化不靠它改写：给不给表结构都不判等价
            assert not diff.equivalent, f"{pair.id} 只有靠约束才看得出等价，不该判等价（schema={bool(schema)}）"
        # 换个先后问，结论一样
        assert explain_sql_diff(pair.b, pair.a, "sqlite", schema=schema).equivalent == diff.equivalent, pair.id


def test_most_known_equivalent_rewrites_are_recognized():
    """已知等价的改写（别名、CTE 改名、关联先后、条件左右、子查询和 CTE 互换……）里认出来的比例。不强求全部认出来：
    认不出的给的是兜底说明，不是错误的结论。要按语义推理才看得出的（hard:）另记，不算进比例。"""
    same = [p for p in sql_pairs.PAIRS if p.expect == "same" and not p.id.startswith("hard:")]
    hits = [p.id for p in same if explain_sql_diff(p.a, p.b, "sqlite", schema=SCHEMA).equivalent]
    # 要靠约束才看得出的（needs_schema）规范化不认，单列出来：按以前的口径（算进比例）和不算进比例的都打出来
    keyed = [p for p in same if not p.needs_schema]
    keyed_hits = [p.id for p in keyed if p.id in hits]
    print(f"\n不靠约束的 {len(keyed)} 对里认出 {len(keyed_hits)} 对（{len(keyed_hits) / len(keyed):.0%}）；"
          f"要靠约束的 {len(same) - len(keyed)} 对一律不认（只给兜底）")
    # 不给表结构那一项只算不靠表结构就该认得出的（列的类型、主键不为空这些得看表结构）
    plain = [p for p in same if not p.typed and not p.needs_schema]
    bare = [p.id for p in plain if explain_sql_diff(p.a, p.b, "sqlite").equivalent]
    ratio, bare_ratio = len(hits) / len(same), len(bare) / len(plain)
    print(f"\n已知等价的改写 {len(same)} 对里认出 {len(hits)} 对（{ratio:.0%}）；不靠表结构的 {len(plain)} 对，"
          f"不给表结构时认出 {len(bare)} 对（{bare_ratio:.0%}）")
    print("没认出来的：", sorted(set(p.id for p in same) - set(hits)))
    hard = [p for p in sql_pairs.PAIRS if p.id.startswith("hard:")]
    hard_hits = [p.id for p in hard if explain_sql_diff(p.a, p.b, "sqlite", schema=SCHEMA).equivalent]
    print(f"要按语义推理的 {len(hard)} 对里认出 {len(hard_hits)} 对（其余只给兜底说明）")
    assert ratio >= 0.9 and bare_ratio >= 0.9


def test_the_corpus_is_large_and_varied_enough():
    assert len(sql_pairs.PAIRS) >= 150 and len(sql_pairs.SEEDS) >= 5
    text = " ".join(p.a + " " + p.b for p in sql_pairs.PAIRS).upper()
    for construct in ("WITH ", "EXISTS", " IN (SELECT", "UNION", "OVER (", "DISTINCT", "LEFT JOIN", "HAVING",
                      "LIMIT", "ORDER BY", "RIGHT JOIN", "FULL JOIN", "NOT IN", "CROSS JOIN", "EXCEPT", "GROUP_CONCAT",
                      "USING (", "IIF("):
        assert construct in text, construct


def test_the_datasets_expose_the_round_three_counterexamples(databases):
    """第 3 轮复验的反例在这几组数据上确实结果不同：外连接改内连接（NOT IN 空集）、列名表和 EXISTS 里的列别名、USING
    之后 ORDER BY 的别名、同名的两列、SQLite 的 % 和 MOD()、遮住外层的别名、找不到列的双引号名字。"""
    pairs = {p.id: p for p in sql_pairs.PAIRS}
    for pid in ("diff:r3-left-not-in-subquery", "diff:r3-left-not-in-empty-list", "diff:r3-cte-column-list-where-alias",
                "diff:r3-cte-column-list-union-where-alias", "diff:r3-exists-alias-where",
                "diff:r3-using-order-by-alias", "diff:r3-using-order-by-alias-left", "diff:r3-duplicate-output-name",
                "diff:r3-mod-vs-percent", "diff:r3-shadowed-alias", "diff:r3-double-quoted-not-in-scope",
                "diff:r3-double-quoted-not-in-outer-scope", "diff:r3-left-coalesce-inside-sum"):
        pair = pairs[pid]
        assert any(sql_pairs.run(c, pair.a) != sql_pairs.run(c, pair.b) for c in databases), pid


def test_the_tricky_dataset_exposes_the_round_two_counterexamples(databases):
    """第 2 轮复验的反例在专挑写法差异的那组数据上确实结果不同（不然执行比对守不住它们）。"""
    tricky = databases[-1]
    pairs = {p.id: p for p in sql_pairs.PAIRS}
    for pid in ("diff:r2-float-fold", "diff:r2-float-fold-chain", "diff:r2-double-negation", "diff:r2-in-single-column",
                "diff:r2-cast-string", "diff:r2-hex-literal", "diff:r2-recursive-without-keyword",
                "diff:r2-cte-column-list-swap-2"):
        pair = pairs[pid]
        assert sql_pairs.run(tricky, pair.a) != sql_pairs.run(tricky, pair.b), pid


# --------------------------------------------------------------------------
# 第 4 轮复验的反例：只查结论的几组
# --------------------------------------------------------------------------


def _only_fallback(diff) -> bool:
    return all(d.aspect == "other" for d in diff.items)


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.CONSTRAINT_TRAPS_R4,
                         ids=[f"{n}-{d}" for n, d, _, _ in sql_pairs.CONSTRAINT_TRAPS_R4])
def test_unique_keys_are_not_trusted_to_drop_joins_or_distinct(name, dialect, a, b):
    """第 4 轮 blocker：以前给了表结构就按主键 / 唯一键去掉没用到的 LEFT JOIN、去掉 DISTINCT，可 SQLite 的 TEXT 主键对
    INTEGER 列按数值比（'1'、'01' 都等于 1）、比较写了 COLLATE NOCASE、部分唯一索引（只管没删的行，探查照样记成
    unique）、MySQL 拿字符串键和数值比，都能让一行关联上好几行。现在不靠约束改写：给不给表结构都不判等价。SQLite 的
    几对在现造的小库上执行，确认结果真的不同。"""
    if dialect == "sqlite":
        conn = sql_pairs.build_coupon_database()
        try:
            assert sql_pairs.run(conn, a) != sql_pairs.run(conn, b), f"{name} 在小库上结果一样，反例没造好"
        finally:
            conn.close()
    for schema in (None, sql_pairs.coupon_schema(dialect)):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent, f"{name} 不该判等价（schema={bool(schema)}）"


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.REGRESSIONS_R4,
                         ids=[f"{n}-{d or 'unknown'}" for n, d, _, _ in sql_pairs.REGRESSIONS_R4])
def test_sql_the_parser_reads_wrongly_is_never_equivalent_and_gets_only_the_fallback(name, dialect, a, b):
    """第 4 轮 blocker（PostgreSQL / MySQL）：sqlglot 读错了的写法——MySQL 字符串里的 \\Z、\\0（方言是 mysql、mariadb 或者
    不知道时），比较运算和 IS / 另一个比较运算没加括号地混用，按位运算和别的运算混用，XOR，1_000、0o17 这类数字——两条
    读成了同一棵树。原文里有这些写法就不在语法树上判等价、也不给具体说法，只给兜底，附原文的逐行对照。"""
    for schema in (None, SCHEMA):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect or None, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent, f"{name} 不该判等价（schema={bool(schema)}）"
            assert _only_fallback(diff), (name, diff.items)
            assert diff.text_diff == (f"- {x}", f"+ {y}"), (name, diff.text_diff)


def _round_four_same() -> list[tuple[str, str, str, bool]]:
    pairs = [sql_pairs.PAIRS_BY_ID[pid] for pid in sql_pairs.ROUND_FOUR_SAME]
    return [(p.id, p.a, p.b, p.needs_schema) for p in pairs] + \
        [(f"{n}-{d}", a, b, keyed) for n, d, a, b, keyed in sql_pairs.DIALECT_SAME_R4]


@pytest.mark.parametrize("pid,a,b,needs_schema", _round_four_same(), ids=[p[0] for p in _round_four_same()])
@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_round_four_equivalent_rewrites_get_at_most_the_fallback_in_every_dialect(pid, a, b, needs_schema, dialect):
    """第 4 轮 major：意思一样，以前却说出了具体的不同（ON 里多余的 IS NOT NULL、IN / COUNT(DISTINCT) / MAX 底下的
    UNION 对 UNION ALL、被 WHERE 定成常量的排序键 / 分组键 / 选出来的列、LEFT JOIN 之后引用可空一侧的内连接、恒为真的
    HAVING、外层不在乎的子查询差异、WHERE 里的列别名、TOTAL / FILTER 只取可空一侧，给了表结构时主键排除了的不同）。
    SQLite 上执行过的这几对再按 PostgreSQL / MySQL 的读法各判一遍：认得出就判等价，认不出只许给兜底说明。要靠约束才
    看得出的，没给表结构时说得出具体的不同不算错。"""
    for schema in (None, SCHEMA):
        if needs_schema and schema is None:
            continue
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert diff.equivalent or _only_fallback(diff), (pid, dialect, bool(schema), diff.items)


@pytest.mark.parametrize("pid", ["diff:r4-limit-differs-order-key-pinned", "diff:r4-distinct-date-vs-time",
                                 "diff:r4-distinct-lower-vs-plain", "diff:r4-distinct-date-vs-time-in-derived"])
@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_real_differences_are_not_explained_with_differences_that_are_not_there(pid, dialect):
    """第 4 轮 major：两条确实不同，以前却多说了不存在的差异——user_id 被 WHERE 定死了还说「取行前按 user_id、id 排」；
    两边都 DISTINCT，却说「A 没有去重」「B 没有」（真正的不同是第 2 列取不取日期）。这几处现在只给兜底。"""
    pair = sql_pairs.PAIRS_BY_ID[pid]
    for schema in (None, SCHEMA):
        for x, y in ((pair.a, pair.b), (pair.b, pair.a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent and _only_fallback(diff), (pid, dialect, diff.items)


@pytest.mark.parametrize("pid", ["diff:r4-eq-then-is-null", "diff:r4-gt-then-is-not-null-in-cte", "diff:r4-lt-then-in",
                                 "diff:r4-eq-then-in", "diff:r4-compared-booleans-then-is-null",
                                 "diff:r4-digit-separator", "diff:r4-digit-separator-vs-one"])
def test_sqlite_sql_the_parser_reads_wrongly_gets_only_the_fallback(databases, pid):
    """第 4 轮 blocker（SQLite）：IS、IN 和 = 同级从左往右、< 比它们紧，sqlglot 让 IS / IN 先结合；1_000 是一千，sqlglot
    切成 1 和别名。这几对在夹具数据上结果都不同；原文里有这些写法就只给兜底和原文的逐行对照。"""
    pair = sql_pairs.PAIRS_BY_ID[pid]
    assert any(sql_pairs.run(c, pair.a) != sql_pairs.run(c, pair.b) for c in databases), pid
    for schema in (None, SCHEMA):
        diff = explain_sql_diff(pair.a, pair.b, "sqlite", schema=schema)
        _check_shape(diff, pair.a, pair.b)
        assert not diff.equivalent and _only_fallback(diff), (pid, diff.items)
        assert diff.text_diff == (f"- {pair.a}", f"+ {pair.b}")


# --------------------------------------------------------------------------
# 第 5 轮复验的反例（收尾第 1 轮之后）
# --------------------------------------------------------------------------


def _raw_line_diff(diff, x: str, y: str) -> bool:
    """text_diff 是原文的逐行对照：「- 」开头的是 x 的原文行，「+ 」开头的是 y 的原文行。"""
    lines_x = {line.rstrip() for line in x.strip().splitlines()}
    lines_y = {line.rstrip() for line in y.strip().splitlines()}
    return bool(diff.text_diff) and all(
        (line[2:] in lines_x) if line.startswith("- ") else (line.startswith("+ ") and line[2:] in lines_y)
        for line in diff.text_diff)


@pytest.mark.parametrize("name,dialect,a,b", sql_pairs.REGRESSIONS_R5,
                         ids=[f"{n}-{d or 'unknown'}" for n, d, _, _ in sql_pairs.REGRESSIONS_R5])
def test_round_five_parser_misreads_are_never_equivalent_and_get_only_the_fallback(name, dialect, a, b):
    """第 5 轮 blocker（PostgreSQL）：~、~*、~~、~~*、@@ 这些写成符号的匹配运算符和 || 同级、从左往右结合，sqlglot 让 ||
    先结合，status ~ 'p' || 'x' 和 status ~ ('p' || 'x') 读成了同一棵树（真实的 PostgreSQL 上一个是 'truex'、一个是
    false）；自己差分压测又找到前缀的 @（取绝对值，sqlglot 读成参数）、|/（双精度的平方根，sqlglot 写成 SQRT）、
    COLLATE 后面跟 ::。不知道是哪种库时，块注释里再写 /*、紧跟着字的 --、双引号括起来的名字（MySQL 默认的 sql_mode 里
    是字符串）各库读法不同。原文里有这些写法就不在语法树上判等价、也不给具体说法，只给兜底，附原文的逐行对照。"""
    for schema in (None, SCHEMA):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect or None, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent, f"{name} 不该判等价（schema={bool(schema)}）"
            assert _only_fallback(diff), (name, diff.items)
            assert _raw_line_diff(diff, x, y), (name, diff.text_diff)


@pytest.mark.parametrize("pid", [p for p in sql_pairs.ROUND_FIVE_DIFF
                                 if p != "diff:r5-dropped-left-join-vs-inner-join"])
@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_round_five_differences_are_not_explained_with_the_wrong_difference(pid, dialect):
    """第 5 轮 major：两条确实不同，以前却说错了不同在哪——条件、时间窗口写在聚合的参数里（SUM(status = 'paid')、
    SUM(amount * (1 - is_deleted))、SUM(paid_at >= …)）被说成「这一边没有这个条件」「没有按时间筛选」；反连接（LEFT
    JOIN … WHERE 可空一侧 IS NULL）被说成有效行过滤。这几处现在只给兜底。"""
    pair = sql_pairs.PAIRS_BY_ID[pid]
    for schema in (None, SCHEMA):
        for x, y in ((pair.a, pair.b), (pair.b, pair.a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent and _only_fallback(diff), (pid, dialect, bool(schema), diff.items)


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_a_left_join_the_normalization_dropped_is_not_called_missing(dialect):
    """第 5 轮 minor：COUNT(DISTINCT …) 不怕重复，规范化去掉了没用到的 LEFT JOIN refunds；另一边内连接 refunds 时，以前
    说「B 关联了 refunds 表，A 没有」，可 A 的原文明明写了 LEFT JOIN refunds。不许说「没有」（PostgreSQL、MySQL 没给表
    结构时不去掉这次 LEFT JOIN，「A 用 LEFT JOIN 关联 refunds 表，B 用 INNER JOIN」和原文对得上，可以说）。"""
    pair = sql_pairs.PAIRS_BY_ID["diff:r5-dropped-left-join-vs-inner-join"]
    for schema in (None, SCHEMA):
        for x, y in ((pair.a, pair.b), (pair.b, pair.a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent
            assert not any(d.aspect in ("join", "tables") and "没有" in d.text for d in diff.items), diff.items


@pytest.mark.parametrize("pid", sorted(sql_pairs.ROUND_FIVE_TIME))
@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_round_five_time_windows_are_never_called_a_filter(pid, dialect):
    """第 5 轮 major：时间列上的日期范围是统计窗口，不是有效行过滤。以前在结果看行先后的一段里（按日期表达式分组、取前
    N 行）BETWEEN 不拆，被说成两条「只保留 … 的行，另一边没有这个条件」，fallback 为假、正好够写进数据目录；LIKE
    '2026-09%' 取月也被说成有效行过滤。现在不许说成 filter；说成 time 时两边的时间列得对（没有按时间筛选的一边是空的）。
    SQLite 上一定说成 time。"""
    pair = sql_pairs.PAIRS_BY_ID[pid]
    cols_a, cols_b = sql_pairs.ROUND_FIVE_TIME[pid]
    for schema in (None, SCHEMA):
        for (x, y), (ca, cb) in (((pair.a, pair.b), (cols_a, cols_b)), ((pair.b, pair.a), (cols_b, cols_a))):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent
            assert not any(d.aspect == "filter" for d in diff.items), (pid, dialect, diff.items)
            times = [d for d in diff.items if d.aspect == "time"]
            for item in times:
                assert [c["column"] for c in item.detail["a"]] == ca and \
                    [c["column"] for c in item.detail["b"]] == cb, (pid, dialect, item)
            if dialect == "sqlite":
                assert len(times) == 1 and not diff.fallback, (pid, diff.items)


def _round_five_same() -> list[tuple[str, str, str, bool]]:
    return [(p.id, p.a, p.b, p.needs_schema) for p in (sql_pairs.PAIRS_BY_ID[i] for i in sql_pairs.ROUND_FIVE_SAME)]


@pytest.mark.parametrize("pid,a,b,needs_schema", _round_five_same(), ids=[p[0] for p in _round_five_same()])
@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_round_five_equivalent_rewrites_get_at_most_the_fallback_in_every_dialect(pid, a, b, needs_schema, dialect):
    """第 5 轮 major：意思一样，以前却说出了具体的不同——反连接里被 u.id IS NULL 蕴含的 u.name IS NULL、两种反连接的
    写法（查主键为空、查关联键为空）、按主键分组时 MAX 对 MIN、AVG(DISTINCT) 对 AVG（同一组里那张表的列只有一个值，
    LEFT JOIN 把行复制了也一样）。认得出就判等价，认不出只许给兜底；要靠主键的，没给表结构时说得出具体的不同不算错。"""
    for schema in (None, SCHEMA):
        if needs_schema and schema is None:
            continue
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert diff.equivalent or _only_fallback(diff), (pid, dialect, bool(schema), diff.items)


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_anti_joins_are_never_called_a_filter(dialect):
    """第 5 轮 major：LEFT JOIN … WHERE r.id IS NULL 和 … WHERE r.order_id IS NULL 是反连接最常见的两种写法。关联键为空
    就是没关联上，蕴含 r.id 为空；r.id 是主键时两者一样，没有主键时查 r.id 的一边留下的行更多（关联上了、r.id 却为空的）。
    以前说「A 只保留 refunds.id IS NULL 的行，B 没有这个条件」：把留得多的一边说成了严的一边，还当成有效行过滤。给不给
    表结构都不许判等价（要靠主键），也只许给兜底。"""
    pair = sql_pairs.PAIRS_BY_ID["hard:r5-anti-join-key-vs-join-column"]
    for schema in (None, SCHEMA):
        for x, y in ((pair.a, pair.b), (pair.b, pair.a)):
            diff = explain_sql_diff(x, y, dialect, schema=schema)
            _check_shape(diff, x, y)
            assert not diff.equivalent and _only_fallback(diff), (dialect, bool(schema), diff.items)


@pytest.mark.parametrize("name,dialect,a,b,schema,bare", sql_pairs.EXPLAIN_TRAPS_R5,
                         ids=[f"{n}-{d or 'unknown'}" for n, d, *_ in sql_pairs.EXPLAIN_TRAPS_R5])
def test_round_five_traps_that_need_their_own_schema_get_only_the_fallback(name, dialect, a, b, schema, bare):
    """第 5 轮 major（要专门的表结构，不执行）：数仓事实表按业务日期键（date_key BETWEEN 20260901 AND 20260930、
    stat_month = 202609）筛的一边，以前被说成「没有按时间筛选」、日期键的范围被说成有效行过滤；反连接查主键、查 NOT NULL
    的列、查关联键，以前被说成两条有效行过滤；经关联条件等于分组键的主键定下来的列，以前说「A 取最大值，B 取最小值」。
    这些都只给兜底（经主键推出来的那几对，没给表结构时这么说按已知信息成立，不查）。"""
    for given in ((schema, None) if bare else (schema,)):
        for x, y in ((a, b), (b, a)):
            diff = explain_sql_diff(x, y, dialect or None, schema=given)
            _check_shape(diff, x, y)
            assert not diff.equivalent and _only_fallback(diff), (name, dialect, bool(given), diff.items)
