"""SQL 差异说明的「等价」结论拿真实执行守门（data/sql_semantics.py 的 explain_sql_diff）。

explain_sql_diff 判两条 SQL 等价，当且仅当两边经过只保持语义的变换之后规范形式一字不差。规范化的每一步都必须
保持语义，可光看代码保证不了（sqlglot 自带的优化器里就有不保持语义的改写），所以这里在虚构的业务库上执行：

- 判了等价的两条，在每组数据上结果都必须一样（结果集按行排序比较）；
- 在某组数据上结果不一样的两条，绝不能判等价；
- 语料里标 diff 的（语义不同，或者只在某个方言、某种排序规则下才等价），一律不能判等价；
- 标 same 的（已知等价的改写），记下认出来的比例：目标是大多数都认得出，但不强求全部。

库和语料见 tests/fixtures/sql_pairs.py：5 组按固定种子生成的数据加一组极端数据，带空值、重复、边界值、已删除的
行、退款；语料里有手写的等价改写和语义不同的对，也有复验者以前造的、在这几组数据上结果不同的对。每一对都在不给表
结构和给表结构（数据源的 schema_cache）两种情况下各判一次，两种都要守住上面的规矩。
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
        # 换个先后问，结论一样
        assert explain_sql_diff(pair.b, pair.a, "sqlite", schema=schema).equivalent == diff.equivalent, pair.id


def test_most_known_equivalent_rewrites_are_recognized():
    """已知等价的改写里认出来的比例（给表结构时）。不强求全部认出来：认不出的给的是兜底说明，不是错误的结论。"""
    same = [p for p in sql_pairs.PAIRS if p.expect == "same"]
    hits = [p.id for p in same if explain_sql_diff(p.a, p.b, "sqlite", schema=SCHEMA).equivalent]
    bare = [p.id for p in same if explain_sql_diff(p.a, p.b, "sqlite").equivalent]
    ratio, bare_ratio = len(hits) / len(same), len(bare) / len(same)
    print(f"\n已知等价的 {len(same)} 对里认出 {len(hits)} 对（{ratio:.0%}），不给表结构时 {len(bare)} 对（{bare_ratio:.0%}）")
    print("没认出来的：", sorted(set(p.id for p in same) - set(hits)))
    assert ratio >= 0.9 and bare_ratio >= 0.9


def test_the_corpus_is_large_and_varied_enough():
    assert len(sql_pairs.PAIRS) >= 150 and len(sql_pairs.SEEDS) >= 5
    text = " ".join(p.a + " " + p.b for p in sql_pairs.PAIRS).upper()
    for construct in ("WITH ", "EXISTS", " IN (SELECT", "UNION", "OVER (", "DISTINCT", "LEFT JOIN", "HAVING",
                      "LIMIT", "ORDER BY", "RIGHT JOIN", "FULL JOIN", "NOT IN", "CROSS JOIN"):
        assert construct in text, construct
