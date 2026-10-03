"""证据面板的查询步骤：合并查询的输入带上检查结果，查询步骤说明用的是哪一版数据目录。

- 合并查询的列追不到逐格来历时，证据面板只有合并步骤、没有输入步骤：输入查询的 SQL 检查结果以前在这里看不到，
  指标却因为它标了「存疑」。现在合并步骤的每个输入带上 checks（不带给模型的那句）。
- 表结构快照里冻结了数据目录（随查询冻结的那一版），以前没有任何读取方。查询步骤带上表结构快照 id 和这条查询
  用到的表的目录版本，面板上写「数据目录：入园记录 第 3 版」。
- 冻结时保留被驳回关系的编号（只放编号和状态）：拿冻结的那份重建检查器，按外键现推的关系不会把驳回的推回来。
"""
from __future__ import annotations

from app.api import evidence as api
from app.core import artifact_store
from app.engine.merge_query import MERGE_SOURCE, MergeInput, execute

FANOUT = {"code": "fanout_sum", "level": "error", "message": "「订单」关联「订单明细」是一对多，求和会重复计算。先汇总再关联",
          "for_model": "orders 关联 order_items 后对 total_amount 求和会重复计算", "table": "orders"}
NOTICE = {"code": "missing_valid_filter", "level": "warning", "message": "没有筛掉作废订单", "for_model": "加 status = 1",
          "table": "orders"}


async def put(obj, kind="query_snapshot") -> str:
    return await artifact_store.put_json(obj, kind=kind)


def _sealed(queries: dict[str, dict]) -> api._Sealed:
    seal = {"sealed": True, "ok": True, "manifest_seq": 9, "legacy": False}
    return api._Sealed(run_id="run-x", graph={}, seal=seal, events=[], queries=queries)


async def test_merge_inputs_carry_their_sql_checks():
    sales = {"columns": ["日期", "销售额"], "rows": [["2026-05-01", 10]], "row_count": 1, "truncated": False,
             "sql": "SELECT 日期, SUM(o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id",
             "source": "shop", "checks": [FANOUT, NOTICE]}
    visits = {"columns": ["日期", "到店"], "rows": [["2026-05-01", 3]], "row_count": 1, "truncated": False,
              "sql": "SELECT 日期, 到店 FROM 到店", "source": "foot"}
    sid, vid = await put(sales), await put(visits)
    outcome = execute([MergeInput(alias="s", node_id="q_sales", artifact=sid, snapshot=sales),
                       MergeInput(alias="v", node_id="q_visits", artifact=vid, snapshot=visits)],
                      'SELECT s."日期", s."销售额" * 1.0 / v."到店" AS 店均 FROM s JOIN v ON s."日期" = v."日期"')
    mid = await put(outcome.snapshot())
    doc = {"catalog": {"Q1": {"kind": "query", "artifact": sid}, "Q2": {"kind": "query", "artifact": vid},
                       "Q3": {"kind": "query", "artifact": mid, "source": MERGE_SOURCE}}}
    sealed = _sealed({sid: {"node_id": "q_sales"}, vid: {"node_id": "q_visits"}, mid: {"node_id": "merge"}})
    # 算出来的列追不到逐格来历：只有合并步骤，输入的检查结果要在合并步骤里看得到
    [step] = await api._query_steps(mid, [(0, "店均")], doc, sealed, api._Masks())
    s_in, v_in = step["merge"]["inputs"]
    assert [c["code"] for c in s_in["checks"]] == ["fanout_sum", "missing_valid_filter"]
    assert all("for_model" not in c for c in s_in["checks"])
    assert s_in["checks"][0]["level"] == "error" and s_in["checks"][0]["table"] == "orders"
    assert "checks" not in v_in
