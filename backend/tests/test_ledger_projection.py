"""认口径时按引用的列投影（app/ledger/match.py 的 _projected，algo 3）。

真实助手的 SQL 总会多选几列（订单数、日期常量、MIN / MAX……），整条比永远不等价，「上周的销售额是多少」从不自动出具。
整条比不等价时，答案一侧按报告引用的列、口径一侧按口径卡上指标读的列投影，再交给现有的绑定和判等价。这里守住：
- 多选一列、多几个聚合、日期常量列的，按引用的那一列证明等价；口径一侧多选了列的同样认得出；留下两列先后不同的另试调换；
- 不错给信任：多一个分组列、DISTINCT、按别的列排序再取前几行、HAVING 用到多出来的聚合的，一律仍然证明不了，结果和
  不投影时一字不差；
- 没投影的（报告没引用、两边都不用投影、投影不了）逐对和以前一字不差；
- 跑一次真的探索运行：报告引用销售额那一格，整题等价；E1 照实数出几份是投影之后才证明的。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.ledger import stats as ledger_stats
from app.ledger import store
from tests import ledger_samples as S

WINDOW = "status = 'paid' AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'"


def caliber(sql: str = S.TEMPLATE, metrics: list[dict] | None = None) -> ledger_match.Caliber:
    graph = S.caliber_graph(sql)
    if metrics is not None:
        graph["nodes"][2]["data"]["config"]["metrics"] = metrics
    found = ledger_match.caliber_of("wf", SimpleNamespace(graph=graph, version=1, level="governed", graph_hash="h"))
    assert found is not None
    return found


def query(sql: str, columns: list[str], artifact: str = "q1") -> ledger_match.Query:
    return ledger_match.Query(artifact, "q", S.CURRENT["source"], sql, columns, frozenset({"orders"}), "sqlite")


def compare(sql: str, columns: list[str], cited: set[str] | None, cal: ledger_match.Caliber | None = None) -> dict:
    [row] = ledger_match.compare_queries([query(sql, columns)], [cal or caliber()],
                                         referenced={"q1": cited} if cited is not None else None)
    return row


# --------------------------------------------------------------------------
# 认得出
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, columns", [
    (f"SELECT SUM(amount) AS 销售额, COUNT(*) AS 单数 FROM orders WHERE {WINDOW}", ["销售额", "单数"]),
    (f"SELECT COUNT(*) AS 单数, MIN(paid_at) AS 起, MAX(paid_at) AS 止, SUM(amount) AS 销售额, "
     f"AVG(amount) AS 客单价 FROM orders WHERE {WINDOW}", ["单数", "起", "止", "销售额", "客单价"]),
    (f"SELECT '2026-09-01' AS 周一, '2026-09-07' AS 周日, SUM(amount) AS 销售额 FROM orders WHERE {WINDOW}",
     ["周一", "周日", "销售额"]),
], ids=["extra-count", "extra-aggregates", "date-constants"])
def test_extra_columns_are_projected_away_and_the_cited_one_is_proven(sql, columns):
    row = compare(sql, columns, {"销售额"})
    assert row["status"] == "proven", row
    [cand] = row["candidates"]
    assert cand["status"] == "proven" and cand["binding"] == {"start": "2026-09-01", "end": "2026-09-08"}
    assert cand["columns"] == [{"index": 0, "ours": "销售额", "caliber": "gmv"}]
    assert cand["projection"] == {"ours": ["销售额"], "dropped": len(columns) - 1, "caliber": ["gmv"],
                                  "caliber_dropped": 0}
    # 不投影（报告没说引用了哪一列）时整条比，照旧证明不了
    assert compare(sql, columns, None)["status"] == "related"


def test_the_caliber_side_is_projected_to_the_columns_its_metrics_read():
    two = S.TEMPLATE.replace("SUM(amount) AS gmv", "SUM(amount) AS gmv, COUNT(*) AS n")
    cal = caliber(two, [{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                        {"id": "n", "name": "单数", "expression": "cell(nodes.fetch, 0, 'n')"}])
    row = compare(f"SELECT SUM(amount) AS 销售额 FROM orders WHERE {WINDOW}", ["销售额"], {"销售额"}, cal)
    assert row["status"] == "proven"
    [cand] = row["candidates"]
    assert cand["columns"] == [{"index": 0, "ours": "销售额", "caliber": "gmv"}]
    assert cand["projection"] == {"ours": ["销售额"], "dropped": 0, "caliber": ["gmv"], "caliber_dropped": 1}
    # 两列都引用、先后和口径的不同：调换之后按位置对上
    row = compare(f"SELECT COUNT(*) AS 单数, SUM(amount) AS 销售额, MAX(amount) AS 最大 FROM orders WHERE {WINDOW}",
                  ["单数", "销售额", "最大"], {"单数", "销售额"}, cal)
    assert row["status"] == "proven"
    [cand] = row["candidates"]
    assert [(c["ours"], c["caliber"]) for c in cand["columns"]] == [("销售额", "gmv"), ("单数", "n")]


# --------------------------------------------------------------------------
# 不错给信任
# --------------------------------------------------------------------------


GROUPED = S.TEMPLATE.replace("SUM(amount) AS gmv", "DISTINCT amount AS gmv")


@pytest.mark.parametrize("sql, columns, cited, cal_sql", [
    # 多一个分组列：答案是每个地区一行，口径只有一行
    (f"SELECT region, SUM(amount) AS 销售额 FROM orders WHERE {WINDOW} GROUP BY region", ["region", "销售额"],
     {"销售额"}, S.TEMPLATE),
    # DISTINCT 按整行去重：去掉 region 之后行数就变了（口径按金额去重，投影了就会错认）
    (f"SELECT DISTINCT region, amount AS 金额 FROM orders WHERE {WINDOW}", ["region", "金额"], {"金额"}, GROUPED),
    # 按别的列排序再取前几行
    (f"SELECT SUM(amount) AS 销售额, region FROM orders WHERE {WINDOW} GROUP BY region ORDER BY region LIMIT 1",
     ["销售额", "region"], {"销售额"}, S.TEMPLATE),
    (f"SELECT amount AS 金额, paid_at FROM orders WHERE {WINDOW} ORDER BY paid_at DESC LIMIT 1", ["金额", "paid_at"],
     {"金额"}, S.TEMPLATE.replace("SUM(amount) AS gmv", "amount AS gmv") + " ORDER BY amount DESC LIMIT 1"),
    # HAVING 用到多出来的聚合（按别名、按同样的写法）
    (f"SELECT SUM(amount) AS 销售额, COUNT(*) AS 单数 FROM orders WHERE {WINDOW} HAVING 单数 > 0", ["销售额", "单数"],
     {"销售额"}, S.TEMPLATE),
    (f"SELECT SUM(amount) AS 销售额, COUNT(*) AS 单数 FROM orders WHERE {WINDOW} HAVING COUNT(*) > 0",
     ["销售额", "单数"], {"销售额"}, S.TEMPLATE),
], ids=["extra-group-by", "distinct", "group-order-limit", "order-other-column-limit", "having-alias",
        "having-same-aggregate"])
def test_projection_never_proves_what_the_whole_sql_does_not_say(sql, columns, cited, cal_sql):
    expression = "cell(nodes.fetch, 0, 'gmv')"
    cal = caliber(cal_sql, [{"id": "gmv", "name": "销售额", "expression": expression}])
    row = compare(sql, columns, cited, cal)
    assert row["status"] != "proven", row
    assert all("projection" not in c for c in row["candidates"]), "这几种都不该投影"
    # 不投影的：和没给引用列时一字不差
    assert row == compare(sql, columns, None, cal)


def test_without_a_projection_every_pair_is_exactly_the_old_result():
    """报告引用了全部的列、口径也不用投影的，整条比证明了的，投影不了的：逐对和不给引用列时一字不差。"""
    cases = [
        (S.EQUIVALENT, ["销售额"], {"销售额"}),                       # 整条比就证明了
        (S.WITH_REFUNDS, ["gmv"], {"gmv"}),                            # 两边都只有这一列：没有可投影的
        (f"SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE {WINDOW}", ["gmv", "n"], {"gmv", "n"}),
        (f"SELECT DISTINCT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE {WINDOW}", ["gmv", "n"], {"gmv"}),
        (f"SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE {WINDOW}", ["gmv"], {"gmv"}),  # 列数对不上
    ]
    for sql, columns, cited in cases:
        assert compare(sql, columns, cited) == compare(sql, columns, None), sql


def test_e1_counts_the_proofs_that_needed_a_projection():
    proven = compare(f"SELECT SUM(amount) AS 销售额, COUNT(*) AS 单数 FROM orders WHERE {WINDOW}",
                     ["销售额", "单数"], {"销售额"})
    related = compare("SELECT SUM(amount) AS 销售额, COUNT(*) AS 单数 FROM orders WHERE paid_at >= '2026-09-01' "
                      "AND paid_at < '2026-09-08'", ["销售额", "单数"], {"销售额"})
    e1 = ledger_stats.e1_of([{"queries": [proven, related], "reports": {}, "whole": {}}])
    assert e1["status"]["proven"] == 1 and e1["proven_projected"] == 1


# --------------------------------------------------------------------------
# 真的跑一遍
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


async def test_last_weeks_sales_with_extra_columns_is_whole_equivalent(engine_up, monkeypatch):
    wf = await S.publish()
    sql = ("SELECT '2026-09-01' AS 周一, SUM(amount) AS 销售额, COUNT(*) AS 单数, MAX(paid_at) AS 最后一单 "
           f"FROM orders WHERE {WINDOW}")
    row = await S.explore(monkeypatch, sql, text="上周销售额 [[v:Q1.r0.销售额]]，共 [[v:Q1.r0.单数]] 单。")
    recorded = await store.latest(row.id)
    content = recorded.content
    assert content["algo"] == "3"
    [q] = content["queries"]
    assert q["status"] == "related", "销售额、单数两列都被引用，口径只有销售额：投影之后列数对不上，照旧不等价"

    row = await S.explore(monkeypatch, sql, text="上周销售额 [[v:Q1.r0.销售额]]。")
    content = (await store.latest(row.id)).content
    [q] = content["queries"]
    assert q["status"] == "proven"
    [cand] = q["candidates"]
    assert cand["projection"]["dropped"] == 3 and cand["columns"] == [{"index": 0, "ours": "销售额", "caliber": "gmv"}]
    [labels] = [r["labels"] for r in content["reports"].values()]
    [label] = labels.values()
    assert label["kind"] == "caliber" and label["column"] == "gmv" and label["caliber"] == f"{wf}@1"
    assert content["whole"]["equivalent"] is True and content["whole"]["binding"] == {"start": "2026-09-01",
                                                                                     "end": "2026-09-08"}


async def test_reconcile_pairs_the_columns_left_after_the_projection(engine_up, monkeypatch):
    """含退款、又多选了几列的答案：对账时按投影之后两边留下的列配（这次的销售额 → 口径卡的 gmv），不用人选。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    wf = await S.publish()
    sql = ("SELECT COUNT(*) AS 单数, SUM(amount) AS 销售额, MAX(paid_at) AS 最后一单 FROM orders "
           "WHERE paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")
    row = await S.explore(monkeypatch, sql, text="上周销售额 [[v:Q1.r0.销售额]]。")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
        [q] = body["queries"]
        [cand] = q["candidates"]
        assert cand["projection"]["ours"] == ["销售额"] and cand["projection"]["caliber"] == ["gmv"]
        res = await client.post(f"/api/ledger/answers/{row.id}/reconcile", json={
            "query": q["artifact"], "workflow_id": wf, "version": 1, "confirm": True,
            "inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
        assert res.status_code == 201, res.text
        done = await S.wait(res.json()["run_id"])
    assert done.status == "succeeded", done.error
    assert done.origin["pairs"] == [{"ours": "销售额", "metric": "gmv"}]
