"""评测的期望答案（app/eval/gold.py；细案 01 做法 8、14，评测-D4）。

- SQL 探针：{{ schedule.* }} 按截至时间渲染；以来源 eval、只读 + 遮罩的探索运行跑（不进使用统计）；期望值从封存的查询
  快照里取格、列、整张表；没有数据的题要 0 行。只读守卫拒写语句；用到遮罩列的拒收；取不到值、运行失败、超时照实说。
- 口径探针：拷出钉住版本的取数节点 + caliber_from 口径卡，算出的值和原工作流一样；不可复用的卡拒收并给原因。
- 口径升版：有新版本没处置就跳过（「口径已升版到 vN，待处置」），三种处置各自生效；钉住的版本退役同样待处置。
- 作答前后各算一次：一样才判，数据变了不判。

库、表、数字都是虚构的（tests/fixtures/dual_check_shop.py：9/1–9/7 按支付日、不含退款 330）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from app.core import artifact_store
from app.db.base import SessionLocal, utcnow
from app.db.models import Artifact, DataSource, Run, Workflow, WorkflowVersion
from app.engine.origin import counted_runs
from app.engine.runner import run_manager
from app.eval import cases, gold
from app.eval.gold import Context
from tests.fixtures import dual_check_shop as shop
from tests.fixtures.sources import drop_source

#: 2026-09-08（周二）早上：上周是 8/31–9/6
AS_OF = datetime(2026, 9, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
LAST_WEEK = "BETWEEN '{{ schedule.last_week_start }}' AND '{{ schedule.last_week_end }}'"
PAID_GMV = f"SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {LAST_WEEK}"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def source(tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "shop.db"))
    yield {"name": name, "id": sid, "path": tmp_path / "shop.db"}
    await drop_source(sid)


def sql_case(source_id: str, sql: str, *expectations: dict, seq: int = 1) -> dict:
    fields = cases.normalize_case({"question": "上周的销售额是多少？", "gold": {"kind": "sql", "datasource_id": source_id,
                                   "sql": sql}, "expectations": list(expectations)})
    return {**fields, "id": None, "seq": seq}


def ctx(**kw) -> Context:
    return Context(as_of=AS_OF, tz="Asia/Shanghai", actor="评测测试", suite_id="s1", suite_name="虚构题集", **kw)


async def run_of(run_id: str) -> Run:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


# --------------------------------------------------------------------------
# SQL 探针
# --------------------------------------------------------------------------


async def test_the_sql_probe_renders_variables_and_reads_the_sealed_snapshot(source):
    case = sql_case(source["id"], PAID_GMV, {"kind": "scalar", "column": "gmv"})
    outcome = await gold.compute_gold(case, ctx())
    assert outcome.ok, outcome.reason
    [found] = outcome.sets
    assert found.values == {"e1": {"kind": "scalar", "value": 330.0, "column": "gmv", "row": 0, "dims": {}}}
    assert "'2026-08-31' AND '2026-09-06'" in outcome.rendered["sql"]
    # 期望点得开：快照是那次运行封存的查询快照
    snapshot = artifact_store.load(found.snapshot)
    assert snapshot["rows"] == [[330.0]] and "2026-08-31" in snapshot["sql"]
    row = await run_of(found.run_id)
    assert row.status == "succeeded" and row.manifest_seq is not None
    assert row.origin_kind == "eval" and row.origin["role"] == "gold" and row.origin["suite_id"] == "s1"
    assert row.workflow_name == "评测：虚构题集·第 1 题·算期望"
    # 只读、遮罩：同使用者的问数据运行
    assert row.options["read_only_data"] is True and row.options["mask_results"] is True
    # 评测运行是系统派生的，不进使用统计
    async with SessionLocal() as session:
        counted = (await session.execute(select(Run.id).where(Run.id == row.id, counted_runs()))).all()
    assert counted == []


async def test_cells_columns_tables_and_no_data(source):
    by_region = (f"SELECT region, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {LAST_WEEK} "
                 "GROUP BY region ORDER BY gmv DESC")
    case = sql_case(source["id"], by_region,
                    {"kind": "scalar", "column": "gmv", "where": {"region": "华东"}},
                    {"kind": "table", "keys": ["region"], "ordered": True},
                    {"kind": "set", "column": "region", "top_n": 1})
    outcome = await gold.compute_gold(case, ctx())
    assert outcome.ok, outcome.reason
    values = outcome.sets[0].values
    assert values["e1"]["value"] == 300.0 and values["e1"]["dims"] == {"region": "华东"}
    assert values["e2"]["rows"] == [{"region": "华东", "gmv": 300.0}, {"region": "华北", "gmv": 30.0}]
    assert values["e3"]["values"] == ["华东"]

    none = sql_case(source["id"], f"SELECT region, SUM(amount) AS gmv FROM orders WHERE region = '华中' "
                                  f"AND paid_date {LAST_WEEK} GROUP BY region", {"kind": "none"})
    outcome = await gold.compute_gold(none, ctx())
    assert outcome.ok and outcome.sets[0].values == {"e1": {"kind": "none", "rows": 0}}


@pytest.mark.parametrize("sql, expectation, words", [
    (PAID_GMV, {"kind": "scalar", "column": "金额"}, "没有列「金额」"),
    (PAID_GMV, {"kind": "scalar", "column": "gmv", "row": 3}, "只返回了 1 行"),
    (f"SELECT SUM(amount) AS gmv FROM orders WHERE region = '华中'", {"kind": "scalar", "column": "gmv"}, "是空值"),
    (f"SELECT region FROM orders WHERE region = '华中'", {"kind": "scalar", "column": "region"}, "没有返回行"),
    (PAID_GMV, {"kind": "none"}, "返回了 1 行"),
])
async def test_values_that_cannot_be_taken_are_said_plainly(source, sql, expectation, words):
    outcome = await gold.compute_gold(sql_case(source["id"], sql, expectation), ctx())
    assert outcome.status == "no_value" and outcome.code == gold.GOLD_NO_VALUE
    assert words in outcome.reason and outcome.run_ids


async def test_the_read_only_guard_refuses_writes_before_anything_runs(source):
    async with SessionLocal() as session:
        await session.execute(update(DataSource).where(DataSource.id == source["id"]).values(readonly=False))
        await session.commit()
    case = sql_case(source["id"], "DELETE FROM orders WHERE id = 1", {"kind": "none"})
    outcome = await gold.compute_gold(case, ctx())
    assert outcome.status == "refused" and outcome.code == cases.GOLD_GUARD and outcome.run_ids == []
    with sqlite3.connect(source["path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == len(shop.ROWS)


@pytest.mark.parametrize("sql, expectation, refused", [
    ("SELECT status, COUNT(*) AS n FROM orders GROUP BY status", {"kind": "set", "column": "status"}, True),
    ("SELECT status AS 状态 FROM orders", {"kind": "set", "column": "状态"}, True),      # 改了名字也认得出
    ("SELECT * FROM orders", {"kind": "table", "keys": ["id"]}, True),
    # 结果里没有取自遮罩列的格：条件里用到遮罩列不算
    ("SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'", {"kind": "scalar", "column": "n"}, False),
])
async def test_gold_that_uses_a_masked_column_is_refused(tmp_path, sql, expectation, refused):
    name, sid = await shop.add_source(shop.build(tmp_path / "masked.db"), options={"mask_columns": ["status"]})
    try:
        outcome = await gold.compute_gold(sql_case(sid, sql, expectation), ctx())
        if refused:
            assert outcome.status == "refused" and outcome.code == gold.GOLD_MASKED, outcome.reason
            assert "遮罩" in outcome.reason
        else:
            assert outcome.ok and outcome.sets[0].values["e1"]["value"] == 4
    finally:
        await drop_source(sid)


async def test_failures_and_timeouts_are_said_plainly(tmp_path, source):
    outcome = await gold.compute_gold(sql_case(source["id"], "SELECT nope FROM orders", {"kind": "scalar",
                                                                                          "column": "nope"}), ctx())
    assert outcome.status == "failed" and "nope" in outcome.reason and outcome.run_ids

    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 500000000) "
            "SELECT SUM(x) AS s FROM c")
    name, sid = await shop.add_source(shop.build(tmp_path / "slow.db"), options={"query_timeout_s": 2})
    try:
        # 等不到：停止那次运行，照实说超时
        outcome = await gold.compute_gold(sql_case(sid, slow, {"kind": "scalar", "column": "s"}), ctx(timeout_s=0.5))
        assert outcome.status == "timeout" and outcome.code == gold.GOLD_TIMEOUT and "0.5 秒" in outcome.reason
        [run_id] = outcome.run_ids
        assert (await run_of(run_id)).status == "cancelled"
        # 数据库按数据源的查询时限停下语句：运行失败，原因照写
        outcome = await gold.compute_gold(sql_case(sid, slow, {"kind": "scalar", "column": "s"}), ctx(timeout_s=60))
        assert outcome.status == "failed" and "超过" in outcome.reason
    finally:
        await drop_source(sid)

    async with SessionLocal() as session:
        await session.execute(update(DataSource).where(DataSource.id == source["id"]).values(enabled=False))
        await session.commit()
    outcome = await gold.compute_gold(sql_case(source["id"], PAID_GMV, {"kind": "scalar", "column": "gmv"}), ctx())
    assert outcome.status == "failed" and "已停用" in outcome.reason and outcome.run_ids == []


async def test_cases_without_gold_need_no_run():
    fields = cases.normalize_case({"question": "最近卖得怎么样？", "expectations": [{"kind": "ask"}]})
    outcome = await gold.compute_gold({**fields, "id": None, "seq": 2}, ctx())
    assert outcome.ok and outcome.sets == [] and outcome.run_ids == []


# --------------------------------------------------------------------------
# 作答前后各算一次
# --------------------------------------------------------------------------


async def test_bracket_judges_only_when_the_data_did_not_move(source):
    case = sql_case(source["id"], PAID_GMV, {"kind": "scalar", "column": "gmv"})

    async def answer(before):
        assert before.ok
        return "作答"

    result, settled = await gold.bracket(case, ctx(), answer)
    assert result == "作答" and settled.ok and settled.expected[0].values["e1"]["value"] == 330.0
    assert settled.after is not None and (await run_of(settled.after.run_ids[0])).origin["role"] == "gold_after"

    async def answer_while_data_moves(before):
        with sqlite3.connect(source["path"]) as db:
            db.execute("INSERT INTO orders VALUES (9, '华东', 104, 70.0, 'paid', '2026-09-02', '2026-09-02', "
                       "'online', 0)")
        return "作答"

    _, settled = await gold.bracket(case, ctx(), answer_while_data_moves)
    assert settled.status == "drifted" and "数据变了" in settled.reason and settled.expected == []

    missing = sql_case(source["id"], PAID_GMV, {"kind": "scalar", "column": "金额"})
    called = []

    async def never(before):
        called.append(before)

    result, settled = await gold.bracket(missing, ctx(), never)
    assert result is None and called == [] and settled.status == "no_value"


# --------------------------------------------------------------------------
# 口径探针
# --------------------------------------------------------------------------


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


CARD_METRICS = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'gmv')"},
    {"id": "avg", "name": "客单价", "unit": "元", "decimals": 2, "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'n')"},
]


def weekly(source_name: str, *, refunds: bool = False, metrics=CARD_METRICS, fetch_sql: str | None = None) -> dict:
    status = "" if refunds else "status <> 'refunded' AND "
    sql = fetch_sql or (f"SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE {status}"
                        "paid_date BETWEEN '{{ input.start }}' AND '{{ input.end }}'")
    return chain(node("start", "input", fields=[{"name": "start", "required": True},
                                                {"name": "end", "required": True}]),
                 node("fetch", "tool", "取数", tool=f"db_query__{source_name}", args={"sql": sql}),
                 node("card", "metrics", "周销售口径卡", caliber="周销售", caliber_version="v1", metrics=metrics),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def publish(*graphs, name="周销售") -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"{name}_{len(graphs)}_{id(graphs)}", graph=graphs[-1], status="published",
                      published_version=len(graphs), version=len(graphs))
        session.add(wf)
        await session.flush()
        for v, graph in enumerate(graphs, start=1):
            session.add(WorkflowVersion(workflow_id=wf.id, version=v, graph=graph, level="published"))
        await session.commit()
        return wf.id


async def add_version(wf_id: str, graph: dict) -> int:
    async with SessionLocal() as session:
        wf = await session.get(Workflow, wf_id)
        wf.published_version = wf.version = (wf.published_version or 0) + 1
        session.add(WorkflowVersion(workflow_id=wf_id, version=wf.published_version, graph=graph, level="published"))
        await session.commit()
        return wf.published_version


def caliber_case(wf_id: str, version: int = 1, *, upgrade=None, metrics=("gmv", "avg")) -> dict:
    fields = cases.normalize_case({
        "question": "上周的销售额和客单价？",
        "gold": {"kind": "caliber", "workflow_id": wf_id, "version": version, "node_id": "card",
                 "inputs": {"start": "{{ schedule.last_week_start }}", "end": "{{ schedule.last_week_end }}"}},
        "expectations": [{"kind": "scalar", "metric": m} for m in metrics]})
    if upgrade:
        fields["gold"]["upgrade"] = upgrade
    return {**fields, "id": None, "seq": 3}


async def card_of(run_id: str) -> dict:
    async with SessionLocal() as session:
        aid = (await session.execute(select(Artifact.id).where(
            Artifact.run_id == run_id, Artifact.kind == "metric_set", Artifact.node_id == "card"))).scalar_one()
    return artifact_store.load(aid)


async def test_the_caliber_probe_computes_what_the_original_workflow_computes(source):
    wf_id = await publish(weekly(source["name"]))
    outcome = await gold.compute_gold(caliber_case(wf_id), ctx())
    assert outcome.ok, outcome.reason
    [found] = outcome.sets
    assert found.version == 1 and outcome.rendered["inputs"] == {"start": "2026-08-31", "end": "2026-09-06"}
    # 原工作流按同样的输入跑一次（探索运行）：口径卡算出的每个数都一样，取整、单位按口径卡
    original = await run_manager.start(graph=weekly(source["name"]), input_payload=outcome.rendered["inputs"])
    await run_manager.wait_idle(original.id, timeout=30)
    card = await card_of(original.id)
    by_metric = {v["metric"]: v for v in found.values.values()}
    assert {m["id"]: m["value"] for m in card["metrics"]} == {k: v["value"] for k, v in by_metric.items()}
    assert by_metric["gmv"]["value"] == 330.0 and by_metric["gmv"]["rendered"] == "330.0元"
    assert by_metric["avg"]["value"] == 110.0 and by_metric["avg"]["caliber"] == "周销售"
    row = await run_of(found.run_id)
    assert row.origin_kind == "eval" and row.origin["derived"]["kind"] == "caliber_probe"
    assert row.origin["derived"]["from"][0]["workflow_version"] == 1
    # 探针图里口径卡固定到那一版，取数节点照抄
    nodes = {n["id"]: n for n in row.graph["nodes"]}
    assert nodes["card"]["data"]["config"]["caliber_from"] == {"workflow_id": wf_id, "workflow_version": 1,
                                                              "node_id": "card"}
    assert nodes["fetch"]["data"]["config"] == weekly(source["name"])["nodes"][1]["data"]["config"]


@pytest.mark.parametrize("graph_of, words", [
    (lambda s: chain(node("start", "input"),
                     node("fetch", "transform", mode="expression", expression="{'gmv': 1}", assign_to="kpi"),
                     node("card", "metrics", caliber="周销售", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
                     node("out", "output")), "输入不全是查库结果"),
    (lambda s: weekly(s, fetch_sql="SELECT SUM(amount) AS gmv, COUNT(*) AS n FROM orders WHERE region = "
                                   "'{{ vars.region }}'"), "vars.region"),
])
async def test_cards_that_cannot_be_reused_are_refused_with_the_reason(source, graph_of, words):
    wf_id = await publish(graph_of(source["name"]))
    outcome = await gold.compute_gold(caliber_case(wf_id, metrics=("gmv",)), ctx())
    assert outcome.status == "refused" and outcome.code == cases.CALIBER_NOT_REUSABLE and words in outcome.reason
    assert outcome.run_ids == []


async def test_a_card_that_copies_another_card_is_not_a_source(source):
    upstream = await publish(weekly(source["name"]))
    chained = chain(node("start", "input"), node("card", "metrics", caliber_from={
        "workflow_id": upstream, "workflow_version": 1, "node_id": "card"}), node("out", "output"))
    wf_id = await publish(chained, name="转抄")
    outcome = await gold.compute_gold(caliber_case(wf_id, metrics=("gmv",)), ctx())
    assert outcome.status == "refused" and "转抄" in outcome.reason


# --------------------------------------------------------------------------
# 口径升版的处置
# --------------------------------------------------------------------------


async def test_an_upgraded_caliber_is_skipped_until_disposed(source):
    wf_id = await publish(weekly(source["name"]))
    case = caliber_case(wf_id)
    assert (await gold.compute_gold(case, ctx())).ok
    await add_version(wf_id, weekly(source["name"], refunds=True))      # v2：改成含退款
    outcome = await gold.compute_gold(case, ctx())
    assert outcome.status == "skipped" and outcome.code == gold.UPGRADE_PENDING
    assert outcome.reason == "口径已升版到 v2，待处置" and outcome.run_ids == []

    async with SessionLocal() as session:
        with pytest.raises(gold.DisposeRefused):
            await gold.plan_dispose(session, case, "keep")           # 没有缺省、没有别的选项
        recomputed, latest = await gold.plan_dispose(session, case, "recompute")
        dual, _ = await gold.plan_dispose(session, case, "dual")
        fresh, _ = await gold.plan_dispose(session, case, "incomparable")
    assert latest == 2
    # recompute：改钉新版本，指纹跟着变（历史在这里断开）
    assert recomputed["version"] == 2 and recomputed["upgrade"]["policy"] == "recompute"
    rebased = {**case, "gold": recomputed}
    assert cases.case_hash(rebased) != cases.case_hash(case)
    outcome = await gold.compute_gold(rebased, ctx())
    assert outcome.ok and [s.version for s in outcome.sets] == [2]
    assert {v["metric"]: v["value"] for v in outcome.sets[0].values.values()}["gmv"] == 380.0
    # dual：新旧两版各算一份，旧的那一份照旧可比（指纹不变）
    both = {**case, "gold": dual}
    assert cases.case_hash(both) == cases.case_hash(case)
    outcome = await gold.compute_gold(both, ctx())
    assert outcome.ok and [s.version for s in outcome.sets] == [1, 2]
    assert [{v["metric"]: v["value"] for v in s.values.values()}["gmv"] for s in outcome.sets] == [330.0, 380.0]
    # incomparable：按新版本另建新题（这里只给出新题的期望来源，归档旧题在接口里）
    assert fresh["version"] == 2 and "upgrade" not in fresh

    # 再来一版：dual 只处置到 v2，v3 又待处置
    await add_version(wf_id, weekly(source["name"]))
    outcome = await gold.compute_gold(both, ctx())
    assert outcome.status == "skipped" and outcome.reason == "口径已升版到 v3，待处置"


async def test_a_retired_pinned_version_waits_for_disposal(source):
    wf_id = await publish(weekly(source["name"]), weekly(source["name"], refunds=True))
    case = caliber_case(wf_id, upgrade={"policy": "dual", "from_version": 1, "to_version": 2})
    assert (await gold.compute_gold(case, ctx())).ok
    async with SessionLocal() as session:
        await session.execute(update(WorkflowVersion).where(WorkflowVersion.workflow_id == wf_id,
                                                            WorkflowVersion.version == 1).values(retired_at=utcnow()))
        await session.commit()
    outcome = await gold.compute_gold(case, ctx())
    assert outcome.status == "skipped" and "v1 已退役，待处置" in outcome.reason
    async with SessionLocal() as session:
        with pytest.raises(gold.DisposeRefused, match="已退役"):
            await gold.plan_dispose(session, case, "dual")
        moved, _ = await gold.plan_dispose(session, case, "recompute")
    assert (await gold.compute_gold({**case, "gold": moved}, ctx())).ok


def test_settle_reports_the_first_reason():
    now = datetime.now(timezone.utc)
    bad = gold.GoldOutcome(status="failed", as_of=now.isoformat(), reason="SQL 错了", code=gold.GOLD_FAILED)
    good = gold.GoldOutcome(status="ok", as_of=now.isoformat(), sets=[gold.GoldSet("r1", "a1", {"e1": {"value": 1}})])
    assert gold.settle(bad, good).status == "failed"
    assert gold.settle(good, bad).reason == "作答后复算期望：SQL 错了"
    assert gold.settle(good, None).status == "failed"
    again = gold.GoldOutcome.from_dict(good.to_dict())
    assert gold.settle(good, again).ok
    assert gold.GoldOutcome.from_dict(good.to_dict()).to_dict() == good.to_dict()
