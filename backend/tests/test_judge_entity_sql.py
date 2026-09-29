"""裁判看引用表名、字段名的句子时，要看得到这些名字出现过的那几次查询的 SQL。

「这个数由 orders 按 orders.member_no 去重计数得出」这种讲算法的句子，证据在 SQL 里
（COUNT(DISTINCT member_no)）。以前表名、字段名的摘录只有一行名字，裁判只能判「证据不支持」——
面板上明明写着「出现在查询 Q1」，这层关联却没交给裁判。
"""
from __future__ import annotations

from app.engine.evidence import build_catalog, compose_doc
from app.engine.judge import ENTITY_QUERIES, SQL_CHARS, prepare

SOURCE = "warehouse"
SCHEMA_ART, Q1_ART, Q2_ART, Q3_ART = "5" * 64, "a" * 64, "b" * 64, "c" * 64
SQL1 = "SELECT COUNT(DISTINCT member_no) AS active_users FROM orders WHERE created_at >= '2026-09-01'"
SQL2 = "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region"
SQL3 = "SELECT COUNT(*) AS refund_cnt FROM refunds"
SNAPS = {
    Q1_ART: {"columns": ["active_users"], "rows": [[321]], "sql": SQL1, "source": SOURCE},
    Q2_ART: {"columns": ["region", "gmv"], "rows": [["华东", 100.0]], "sql": SQL2, "source": SOURCE},
    Q3_ART: {"columns": ["refund_cnt"], "rows": [[4]], "sql": SQL3, "source": SOURCE},
}


def query(art: str, columns: list[str], tables: list[str]) -> dict:
    return {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": art, "tool": f"db_query__{SOURCE}",
            "source": SOURCE, "columns": columns, "rows": 1, "truncated": False, "schema_artifact": SCHEMA_ART,
            "tables": tables}


SCHEMA = {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": SCHEMA_ART, "source": SOURCE,
          "tables": {"orders": ["id", "member_no", "region", "amount", "created_at"], "refunds": ["id", "amount"]}}


def catalog():
    return build_catalog(nodes={}, ledger=[query(Q1_ART, ["active_users"], ["orders"]),
                                           query(Q2_ART, ["region", "gmv"], ["orders"]),
                                           query(Q3_ART, ["refund_cnt"], ["refunds"]), SCHEMA])


def excerpts_for(text: str) -> dict[str, str]:
    cat = catalog()
    doc = compose_doc(text, cat)
    return prepare(doc, cat, loader=SNAPS.get).excerpts


def test_a_table_excerpt_carries_the_sql_of_the_queries_it_appears_in():
    ex = excerpts_for("活跃用户数由 [[t:orders]] 按 [[c:orders.member_no]] 去重计数得出。")
    table = ex["t:orders"]
    assert table.startswith("【t:orders】表 orders")
    assert "COUNT(DISTINCT member_no)" in table and "Q1" in table
    assert "SUM(amount)" in table and "Q2" in table             # 用到这张表的查询都给
    assert "refunds" not in table                              # 没用到这张表的不给


def test_a_column_only_in_the_sql_falls_back_to_queries_that_mention_it():
    # member_no 不是任何查询的结果列，条目自己没记查询：按所在的表找、只留 SQL 里写了它的
    ex = excerpts_for("活跃用户数按 [[c:orders.member_no]] 去重计数得出。")
    column = ex["c:orders.member_no"]
    assert "COUNT(DISTINCT member_no)" in column and "Q1" in column
    assert "SUM(amount)" not in column                         # Q2 用了 orders 但没写 member_no


def test_a_result_column_uses_the_queries_it_came_from():
    ex = excerpts_for("各区的销售额按 [[c:orders.region]] 汇总。")
    assert "GROUP BY region" in ex["c:orders.region"] and "Q2" in ex["c:orders.region"]


def test_entity_sql_is_capped():
    long_sql = "SELECT COUNT(DISTINCT member_no) AS n FROM orders WHERE " + " AND ".join(f"f{i} = 1" for i in range(200))
    snaps = {art: {**SNAPS[Q1_ART], "sql": long_sql} for art in "defgh"}
    ledger = [query(art * 64, ["n"], ["orders"]) for art in "defgh"] + [SCHEMA]
    cat = build_catalog(nodes={}, ledger=ledger)
    doc = compose_doc("活跃用户数由 [[t:orders]] 去重计数得出。", cat)
    table = prepare(doc, cat, loader=lambda a: snaps.get(a[0])).excerpts["t:orders"]
    assert table.count("SELECT") == ENTITY_QUERIES
    assert all(len(line) < SQL_CHARS + 40 for line in table.splitlines())


def test_an_unsealed_snapshot_is_simply_left_out():
    # loader 只认封存范围内的工件：取不到的查询不给 SQL，名字那一行照样在
    cat = catalog()
    doc = compose_doc("活跃用户数由 [[t:orders]] 去重计数得出。", cat)
    table = prepare(doc, cat, loader=lambda a: None).excerpts["t:orders"]
    assert table.startswith("【t:orders】表 orders") and "SELECT" not in table


def test_a_qualified_column_in_the_sql_still_counts():
    # SQL 里写成 o.member_no（表别名前缀）也算写了这个字段；只沾了边的 member_no_hash 不算
    snaps = {Q1_ART: {**SNAPS[Q1_ART], "sql": "SELECT COUNT(DISTINCT o.member_no) AS n FROM orders o"},
             Q2_ART: {**SNAPS[Q2_ART], "sql": "SELECT COUNT(DISTINCT member_no_hash) AS n FROM orders"}}
    cat = build_catalog(nodes={}, ledger=[query(Q1_ART, ["n"], ["orders"]), query(Q2_ART, ["n"], ["orders"]), SCHEMA])
    doc = compose_doc("活跃用户数按 [[c:orders.member_no]] 去重计数得出。", cat)
    column = prepare(doc, cat, loader=snaps.get).excerpts["c:orders.member_no"]
    assert "o.member_no" in column and "member_no_hash" not in column


def test_the_same_column_name_in_another_tables_query_is_not_evidence():
    # 只引了字段、没引表（文档目录里没有 t:orders）：看全部查询时，别的表上写了同名字段的不算
    snaps = {**SNAPS, Q3_ART: {**SNAPS[Q3_ART], "sql": "SELECT COUNT(DISTINCT member_no) AS n FROM refunds"}}
    cat = catalog()
    doc = compose_doc("活跃用户数按 [[c:orders.member_no]] 去重计数得出。", cat)
    assert "t:orders" not in doc["catalog"]
    column = prepare(doc, doc["catalog"], loader=snaps.get).excerpts["c:orders.member_no"]
    assert "FROM orders" in column and "FROM refunds" not in column


# --------------------------------------------------------------------------
# 写作提示：讲算法的句子挂 [[see:Qn]]，别只写表名、字段名
# --------------------------------------------------------------------------


def test_the_writer_is_told_to_point_method_sentences_at_the_query():
    from app.engine.evidence import catalog_prompt

    prompt = catalog_prompt(catalog())
    [rule] = [line for line in prompt.splitlines() if "算出来" in line]
    assert "[[see:Q" in rule and "去重" in rule
    assert "[[t:" in rule and "[[c:" in rule                    # 点明只挂表名、字段名说明不了算法


def test_a_catalog_without_tables_does_not_carry_the_method_rule():
    from app.engine.evidence import catalog_prompt

    ledger = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": Q1_ART, "tool": f"db_query__{SOURCE}",
               "source": SOURCE, "columns": ["active_users"], "rows": 1, "truncated": False}]
    assert "算出来" not in catalog_prompt(build_catalog(nodes={}, ledger=ledger))
