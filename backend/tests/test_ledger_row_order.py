"""按行号取多行查询里的一格，重跑、核对时行序不一定一样（next-round 验收遗留）：

- 认法（data/sql_semantics.row_stable）：最外层带 ORDER BY 的按位置认；一定只出一行的第 0 行认得住；别的（多行、又没有
  ORDER BY）行序不定——库可以按任何顺序交回这些行；
- 口径账本的指标标签：答案的一格和口径卡的一格按「同一行」认来自口径，只在行认得住时成立；行序不定的照实标「相关」、
  写明行序不定，不再标成来自口径；
- 口径召回：两版都按行号取一格、查询又行序不定的指标，照实记「行序不定」（changed 为空），不按位置硬比；
- 对账：多行结果按维度对齐之后，每一组对账指标先核对那一行的维度是不是它（重跑时缺了一行、多了一行，后面的行会错位），
  对不上的记空值，不拿别的维度的数冒充。
数据是虚构店铺库。
"""
from __future__ import annotations

import pytest

from app.data.sql_semantics import ordered, row_stable
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.ledger import recall as ledger_recall
from app.ledger import reconcile as ledger_reconcile
from tests import ledger_samples as S

UNORDERED = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
             "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}' GROUP BY region")
UNORDERED_ALL = ("SELECT region AS 区域, SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
                 "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08' GROUP BY region")


def test_which_rows_survive_a_rerun():
    assert ordered("SELECT a, b FROM t ORDER BY a") and not ordered("SELECT a, b FROM t")
    assert row_stable("SELECT a, SUM(b) FROM t GROUP BY a ORDER BY a", 3)
    assert row_stable("SELECT SUM(b) FROM t", 0) and not row_stable("SELECT SUM(b) FROM t", 1)
    assert not row_stable("SELECT a, SUM(b) FROM t GROUP BY a", 0)
    assert not row_stable("这不是 SQL", 0)


@pytest.fixture
async def shop(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


async def _label(monkeypatch, caliber_sql: str, answer_sql: str, row: int) -> dict:
    from tests.test_ledger_match import matched, only_label

    await S.publish(graph=S.caliber_graph(caliber_sql, expression=f"cell(nodes.fetch, {row}, 'gmv')"))
    run = await S.explore(monkeypatch, answer_sql, text="这一区销售额 [[m:gmv]]。",
                          card_expression=f"cell(nodes.q, {row}, 'gmv')")
    recorded, _ = await matched(run.id)
    return only_label(recorded.content)


async def test_an_unordered_multi_row_cell_is_not_claimed_to_come_from_the_caliber(shop, monkeypatch):
    label = await _label(monkeypatch, UNORDERED, UNORDERED_ALL, 1)
    assert label["kind"] == "related" and label["reason"] == ledger_match.ROW_ORDER_UNSTABLE, label


async def test_with_order_by_the_same_row_still_comes_from_the_caliber(shop, monkeypatch):
    label = await _label(monkeypatch, UNORDERED + " ORDER BY region", UNORDERED_ALL + " ORDER BY region", 1)
    assert label["kind"] == "caliber", label


def _pinned(sql: str, expression: str, version: int):
    from app.ledger.derive import Pinned

    graph = S.caliber_graph(sql, expression=expression)
    card = next(n for n in graph["nodes"] if n["id"] == "card")
    fetch = next(n for n in graph["nodes"] if n["id"] == "fetch")
    return Pinned(workflow_id="wf", workflow_name="周销售", version=version, node_id="card", level="governed",
                  published=True, retired=False, card=card, fetches=(fetch,), reads=(("fetch", S.tool(), ("gmv",)),),
                  entry_fields=(), defaults={})


def test_recall_marks_unordered_row_comparisons_instead_of_comparing_by_position():
    pair = ledger_recall.Pair(old=_pinned(UNORDERED, "cell(nodes.fetch, 1, 'gmv')", 1),
                              new=_pinned(UNORDERED + " AND 1 = 1", "cell(nodes.fetch, 1, 'gmv')", 2))
    _, meta = ledger_recall.recall_card([pair])
    assert meta[0]["row_order"] == "unstable"
    values = {"old_0": {"value": 1.0, "rendered": "1"}, "new_0": {"value": 2.0, "rendered": "2"},
              "diff_0": {"value": 1.0, "rendered": "1"}}
    [item] = ledger_recall.compare(meta, values)
    assert item["changed"] is None and item["row_order"] == "unstable"
    # 带 ORDER BY 的照常比
    pair = ledger_recall.Pair(old=_pinned(UNORDERED + " ORDER BY region", "cell(nodes.fetch, 1, 'gmv')", 1),
                              new=_pinned(UNORDERED + " ORDER BY region", "cell(nodes.fetch, 1, 'gmv')", 2))
    _, meta = ledger_recall.recall_card([pair])
    assert "row_order" not in meta[0]
    [item] = ledger_recall.compare(meta, values)
    assert item["changed"] is True


def test_reconcile_checks_each_aligned_row_is_the_one_it_stands_for():
    plan = ledger_reconcile.Plan(pairs=[ledger_reconcile.Pair(ours="gmv", column="gmv")], rows=[0, 1],
                                 keys=[("区域", "区域")], key_values=[("east",), ("west",)])
    nodes = ledger_reconcile.tail_nodes(plan, card_id="card", fetch_id="fetch")
    card = next(n for n in nodes if n["id"] == "reconcile")
    metrics = {m["id"]: m["expression"] for m in card["data"]["config"]["metrics"]}
    from app.engine.expressions import eval_expression

    def run(rows):
        table = {"columns": ["区域", "ours_0", "cal_0"], "rows": rows}
        return {mid: _eval(eval_expression, expr, table) for mid, expr in metrics.items() if mid.startswith("ours_")}

    # 重跑时东区那一行没了：西区挪到了第 0 行。以前东区那一组拿的是西区的数
    got = run([["west", 58.0, 20.0]])
    assert got["ours_0_0"] is None, "东区对不上：记空值，不拿西区的数冒充"
    assert run([["east", 163.0, 150.5], ["west", 58.0, 20.0]]) == {"ours_0_0": 163.0, "ours_1_0": 58.0}


def _eval(fn, expression, table):
    from app.engine.expressions import CellError, ExpressionError

    try:
        return fn(expression, {"nodes": {"align": table}})
    except (CellError, ExpressionError, IndexError, KeyError, TypeError):
        return None
