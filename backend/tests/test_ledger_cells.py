"""口径标签逐格判（app/ledger/match._cells、labels._view，algo 4）。

真实模型下问「上周的销售额是多少」，助手写 SUM(amount) AS 销售额, COUNT(*) AS 订单数, MIN / MAX(paid_date) AS 区间首日 / 末日，
报告四列都引用了；口径「周销售」只取 SUM(amount)。以前按「全部引用列」投影后还是 4 列，对不上口径的 1 列，整条比、四个数都
标相关口径，说明整段兜底。现在每个被引用的格单独投影到那一列，和口径每个指标读的那一列逐个比（仍只走 match_template /
explain_sql_diff，上次的投影规则照用）：
- 销售额标「等价已证明」，订单数、首末日照实标相关口径；整题不等价（订单数不来自口径），不自动出具、不给出具入口；
- 同一个数在两份口径里只证明了一份时标那一份；口径有多个指标时按列配对；一个指标用了同一条一行查询的两格，两格各自
  证明了就算来自口径，多行的查询不拿逐格的结论合起来用；
- 不错给信任：多一个分组列、DISTINCT 的查询逐格也不投影；
- 评测逐题比 SQL 取和期望配上的那一格。
数据是虚构的。
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.runner import run_manager
from app.ledger import labels as ledger_labels
from app.ledger import match as ledger_match
from app.ledger import store
from tests import ledger_samples as S

#: 口径「周销售」的取数（照真实例子：按支付日期、两头都含）
WEEKLY = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
          "AND paid_date >= '{{ input.start }}' AND paid_date <= '{{ input.end }}'")
WINDOW = "status = 'paid' AND paid_date >= '2026-10-05' AND paid_date <= '2026-10-11'"
#: 助手写的（照真实例子）
ANSWER = ("SELECT SUM(amount) AS 销售额, COUNT(*) AS 订单数, MIN(paid_date) AS 区间首日, MAX(paid_date) AS 区间末日 "
          f"FROM orders WHERE {WINDOW}")
COLUMNS = ["销售额", "订单数", "区间首日", "区间末日"]
ROWS = [(1, "paid", 120.0, "2026-10-04"), (2, "paid", 80.0, "2026-10-05"), (3, "paid", 45.5, "2026-10-08"),
        (4, "refunded", 30.0, "2026-10-09"), (5, "paid", 64.5, "2026-10-11"), (6, "open", 10.0, "2026-10-10"),
        (7, "paid", 99.0, "2026-10-12")]


async def week_source(tmp_path) -> None:
    path = tmp_path / "week.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, amount REAL, paid_date TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    S.CURRENT["source"] = f"week_{uuid.uuid4().hex[:8]}"
    await S.set_source(path)


def caliber(sql: str = WEEKLY, metrics: list[dict] | None = None, workflow_id: str = "wf") -> ledger_match.Caliber:
    graph = S.caliber_graph(sql)
    if metrics is not None:
        graph["nodes"][2]["data"]["config"]["metrics"] = metrics
    found = ledger_match.caliber_of(workflow_id, SimpleNamespace(graph=graph, version=1, level="governed",
                                                                 graph_hash="h"))
    assert found is not None
    return found


def compare(sql: str, columns: list[str], cited: set[str], calibers: list[ledger_match.Caliber] | None = None) -> dict:
    query = ledger_match.Query("q1", "q", S.CURRENT["source"], sql, columns, frozenset({"orders"}), "sqlite")
    [row] = ledger_match.compare_queries([query], calibers or [caliber()], referenced={"q1": cited})
    return row


def label(row: dict, column: str) -> dict:
    return ledger_labels.cell_label("q1", column, {"q1": row})


def texts(cand: dict) -> list[str]:
    return [i["text"] for i in (cand.get("diff") or {}).get("items") or []]


# --------------------------------------------------------------------------
# 逐格判：同一条查询里一列等价、一列不等价各自标对
# --------------------------------------------------------------------------


def test_each_cited_column_is_judged_on_its_own():
    row = compare(ANSWER, COLUMNS, set(COLUMNS))
    assert row["status"] == "related", "按全部引用列投影还是 4 列，整条照旧不等价"
    assert row["one_row"] is True and set(row["cells"]) == set(COLUMNS)
    sales = label(row, "销售额")
    assert sales["kind"] == "caliber" and sales["how"] == "independent" and sales["column"] == "gmv"
    assert sales["binding"] == {"start": "2026-10-05", "end": "2026-10-11"}
    assert [label(row, c)["kind"] for c in COLUMNS[1:]] == ["related", "related", "related"]
    [orders] = row["cells"]["订单数"]["candidates"]
    assert texts(orders) == ["这次多查了 3 列，不影响这个数", "这次数的是行数，口径对 amount 求和"]
    assert orders["diff"]["fallback"] is False
    # 整题：订单数不来自口径，不等价
    whole = ledger_labels.whole_of({c: label(row, c) for c in COLUMNS}, {})
    assert whole["equivalent"] is False and whole["counts"]["caliber"] == 1
    assert whole["reason"] == "有的数和相关口径不一致，不来自口径"
    # 只引用销售额一格（不逐格）：整条按引用的列投影就证明了，照旧
    single = compare(ANSWER, COLUMNS, {"销售额"})
    assert single["status"] == "proven" and "cells" not in single


def test_a_number_proven_against_only_one_of_two_calibers_is_labelled_with_that_one():
    paid = caliber(workflow_id="wf-paid")
    everything = caliber(WEEKLY.replace("status = 'paid' AND ", ""), workflow_id="wf-all")
    row = compare(ANSWER, COLUMNS, set(COLUMNS), [paid, everything])
    sales = label(row, "销售额")
    assert sales["kind"] == "caliber" and sales["caliber"] == "wf-paid@1"
    cands = {c["caliber"]: c for c in row["cells"]["销售额"]["candidates"]}
    assert cands["wf-paid@1"]["status"] == "proven" and cands["wf-all@1"]["status"] == "related"
    assert any("status = 'paid'" in t for t in texts(cands["wf-all@1"]))
    # 反过来：含退款的写法只对得上另一份
    refunds = ANSWER.replace("status = 'paid' AND ", "")
    other = label(compare(refunds, COLUMNS, set(COLUMNS), [paid, everything]), "销售额")
    assert other["kind"] == "caliber" and other["caliber"] == "wf-all@1"


def test_a_caliber_with_several_metrics_is_paired_column_by_column():
    two = caliber(WEEKLY.replace("SUM(amount) AS gmv", "SUM(amount) AS gmv, COUNT(*) AS n"),
                  [{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"},
                   {"id": "n", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'n')"}])
    sql = f"SELECT COUNT(*) AS 单数, SUM(amount) AS 销售额, MAX(paid_date) AS 末日 FROM orders WHERE {WINDOW}"
    row = compare(sql, ["单数", "销售额", "末日"], {"单数", "销售额", "末日"}, [two])
    assert row["status"] == "related"
    assert (label(row, "单数")["kind"], label(row, "单数")["column"]) == ("caliber", "n")
    assert (label(row, "销售额")["kind"], label(row, "销售额")["column"]) == ("caliber", "gmv")
    assert label(row, "末日")["kind"] == "related"


def test_a_metric_over_two_cells_uses_the_cell_proofs_only_on_a_one_row_query():
    two = caliber(WEEKLY.replace("SUM(amount) AS gmv", "SUM(amount) AS gmv, COUNT(*) AS n"),
                  [{"id": "aov", "name": "客单价", "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'n')"}])
    sql = f"SELECT SUM(amount) AS 销售额, COUNT(*) AS 订单数, MAX(paid_date) AS 末日 FROM orders WHERE {WINDOW}"
    row = compare(sql, ["销售额", "订单数", "末日"], {"销售额", "订单数", "末日"}, [two])
    inputs = [{"path": f"cell(nodes.q, 0, '{c}')", "artifact": "q1", "via": "tool_cell", "locator": {"row": 0, "column": c}}
              for c in ("销售额", "订单数")]
    card = {"metrics": [{"id": "aov", "expression": "cell(nodes.q, 0, '销售额') / cell(nodes.q, 0, '订单数')",
                         "inputs": inputs}]}
    by_key = {two.key: {"expressions": list(two.expressions)}}
    got = ledger_labels.metric_label(card, "aov", {"q1": row}, by_key)
    assert got["kind"] == "caliber" and got["metric"] == "aov", got
    # 同一条查询要是多行的，每一列各自对得上不等于每一行的两列一起对得上：不拿逐格的结论合起来用
    multi = {**row, "one_row": False}
    assert ledger_labels.metric_label(card, "aov", {"q1": multi}, by_key)["kind"] == "related"


# --------------------------------------------------------------------------
# 不错给信任：逐格投影照样守上次的投影规则
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    f"SELECT paid_date, SUM(amount) AS 销售额, COUNT(*) AS 订单数 FROM orders WHERE {WINDOW} GROUP BY paid_date",
    f"SELECT DISTINCT amount AS 销售额, paid_date AS 订单数, id FROM orders WHERE {WINDOW}",
], ids=["extra-group-by", "distinct"])
def test_cells_are_not_projected_where_the_projection_rules_say_no(sql):
    columns = ["paid_date", "销售额", "订单数"] if "GROUP BY" in sql else ["销售额", "订单数", "id"]
    row = compare(sql, columns, {"销售额", "订单数"})
    assert row["status"] == "related"
    for name in ("销售额", "订单数"):
        cell = row["cells"][name]
        assert cell["status"] == "related" and all("projection" not in c for c in cell["candidates"]), name
        assert cell["candidates"] == row["candidates"], "投影不了的格照用整条查询的结论"
        assert label(row, name)["kind"] == "related"


# --------------------------------------------------------------------------
# 真跑一遍真实例子：标签、整题、出具入口、证据片段的 ledger 键
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up(tmp_path):
    await run_manager.setup()
    await week_source(tmp_path)
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


async def by_column(run_id: str, labels: dict) -> dict[str, dict]:
    """标签按它引用的列排开（报告文档里每个片段引用的是哪一列）。"""
    from app.api.evidence import _reports, _sealed
    from app.engine.evidence import iter_units

    [report] = _reports(await _sealed(run_id))
    out = {}
    for _, unit in iter_units(report.doc):
        for seg in unit.get("segments") or []:
            if seg.get("id") in labels:
                out[seg["cite"]["locator"]["column"]] = {**labels[seg["id"]], "_segment": seg["id"]}
    return out


async def test_last_weeks_sales_labels_only_the_sales_number_as_from_the_caliber(engine_up, monkeypatch):
    graph = S.caliber_graph(WEEKLY)
    wf = await S.publish(graph=graph)
    row = await S.explore(monkeypatch, ANSWER, origin="chat", text=(
        "上周销售额 [[v:Q1.r0.销售额]] 元，共 [[v:Q1.r0.订单数]] 单（[[v:Q1.r0.区间首日]] 至 [[v:Q1.r0.区间末日]]）。"))
    content = (await store.latest(row.id)).content
    assert content["algo"] == store.ALGO == "4"
    [query] = content["queries"]
    assert query["status"] == "related" and set(query["cells"]) == set(COLUMNS)
    [labels] = [r["labels"] for r in content["reports"].values()]
    cols = await by_column(row.id, labels)
    assert cols["销售额"]["kind"] == "caliber" and cols["销售额"]["caliber"] == f"{wf}@1"
    assert cols["销售额"]["binding"] == {"start": "2026-10-05", "end": "2026-10-11"}
    assert {c: cols[c]["kind"] for c in COLUMNS[1:]} == {c: "related" for c in COLUMNS[1:]}
    whole = content["whole"]
    assert whole["equivalent"] is False and whole["counts"] == {"caliber": 1, "related": 3, "reasoning": 0,
                                                                 "unknown": 0}
    # 工件 meta：逐格证明了的口径也记上（召回按旧版本反查只读它）
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import Artifact

    async with SessionLocal() as session:
        meta = (await session.execute(select(Artifact.meta).where(
            Artifact.id == (await store.latest(row.id)).artifact, Artifact.run_id == row.id))).scalar_one()
    assert meta["calibers"] == [f"{wf}@1"]

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
        assert not [a for a in body["actions"] if a["kind"] == "issue"], "整题不等价：不给按口径出具的入口（拍板 L1）"
        assert body["issued"] == [], "不自动出具"
        [q] = body["queries"]
        assert q["cells"]["销售额"]["status"] == "proven" and q["cells"]["订单数"]["status"] == "related"
        shown = (await by_column(row.id, body["reports"]["write"]["labels"]))
        assert shown["销售额"]["kind"] == "caliber" and shown["销售额"]["workflow_name"] == "周销售"
        seg = (await client.get(f"/api/runs/{row.id}/evidence/segments/{cols['销售额']['_segment']}")).json()
        assert seg["ledger"]["label"]["kind"] == "caliber"
        seg = (await client.get(f"/api/runs/{row.id}/evidence/segments/{cols['订单数']['_segment']}")).json()
        assert seg["ledger"]["label"]["kind"] == "related"
        where = seg["ledger"]["label"]["related"][0]["diff"]["items"]
        assert [i["text"] for i in where] == ["这次多查了 3 列，不影响这个数", "这次数的是行数，口径对 amount 求和"]


# --------------------------------------------------------------------------
# 评测逐题比 SQL：取和期望配上的那一格
# --------------------------------------------------------------------------


async def test_the_per_case_sql_match_takes_the_cell_that_matched_the_expectation(engine_up, monkeypatch):
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from app.eval import score

    run = await S.explore(monkeypatch, ANSWER, text="上周销售额 [[v:Q1.r0.销售额]] 元，共 [[v:Q1.r0.订单数]] 单。")
    answer = await score.read_answer(run.id)
    async with SessionLocal() as session:
        sid = (await session.execute(select(DataSource.id).where(DataSource.name == S.CURRENT["source"]))).scalar()
    fixed = "SELECT SUM(amount) AS gmv FROM orders WHERE " + WINDOW
    case = {"gold": {"kind": "sql", "datasource_id": sid, "sql": "x"}, "expectations": []}
    gold = {"status": "ok", "before": {"rendered": {"sql": fixed}, "sets": []}}
    # 没给配上的格：逐格证明了的那一格
    match = await score.sql_match(case, gold, answer)
    assert match["status"] == "proven" and match["column"] == "销售额", match
    # 期望配上的是订单数那一格：取那一格的结论，差在哪按那一格说
    query = answer.cited[0]
    match = await score.sql_match(case, gold, answer, cells=[{"artifact": query, "column": "订单数"}])
    assert match["status"] == "related" and match["column"] == "订单数" and match["aspects"] == ["aggregate"], match
