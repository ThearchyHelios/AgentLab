"""从查询快照建口径草稿图：同一份输入连着建两次必须一模一样。

build_graph 抽自 freeze。草稿图要落成工作流版本、算 graph_hash，节点顺序、位置、报告配置、指标 expression 只要有一点
抖动，同一份快照就会算出另一个版本、比对也认不出等价；所以确定性单独盯一条。
"""
from __future__ import annotations

import json

from app.ledger.certify import build_graph, shape_of

SQL = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
       "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")
KINDS = {"shop": "sqlite"}


def snapshot() -> dict:
    return {"source": "shop", "sql": SQL, "columns": ["gmv"], "rows": [[4231.5]]}


async def _labels(_snap: dict, _source: str):
    return lambda _key: None


async def test_build_graph_twice_is_identical():
    shape = shape_of(snapshot(), KINDS)
    assert shape is not None and shape.problem is None
    # 别的运行里两个日期都变了、常量 'paid' 没变：两次建图应当都认出一样的 p1 / p2
    shifted = [tuple(lit.replace("2026-09-01", start).replace("2026-09-08", end) for lit in shape.canonical)
               for start, end in (("2026-09-02", "2026-09-09"), ("2026-09-03", "2026-09-10"))]
    call = dict(shapes=[shape], snaps=[snapshot()], kinds=KINDS, labels=_labels, name="九月第一周销售额",
                question="九月第一周卖了多少", peers={(shape.source, shape.key): shifted})

    first = await build_graph(**call)
    second = await build_graph(**call)

    assert [f["name"] for f in first[1]] == ["p1", "p2"]
    assert first == second
    assert json.dumps(first[0], ensure_ascii=False) == json.dumps(second[0], ensure_ascii=False)
