"""分析入口接真算子跑一遍（不用替身）：agent 的五个分析工具（变化拆解含下钻，波动基线、趋势、差异检验、可比性检查各一次）、
分析卡（波动基线 + 变化拆解；输入是合并查询的一次）、口径卡的可比性挂钩、审批恢复时的重放。断言工件、台账、交回模型的那一段、
节点产出，并用 analysis.recompute 按封存的工件复算、逐字段相同。

虚构零售库 shop：orders(day, region, channel, amount)，2026-03-02 起 12 个整周，每周每个地区、渠道两笔（周三、周日），
最后一周华东掉三成。模型换成脚本（MockChatModel._decide），不调用任何模型。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, timedelta

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

import app.analysis as analysis_ops
from app.analysis import contract
from app.core import artifact_store
from app.core.artifact_store import canonical_json, content_hash
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager

TOOL = "db_query__shop"
DECOMPOSE = contract.TOOL_NAMES["decompose"]
BASELINE, TREND, DIFFER, COMPARABLE = (contract.TOOL_NAMES[op] for op in ("baseline", "trend", "differ", "comparable"))
WEEK = "date(day, '-6 days', 'weekday 1')"
BY_REGION = (f"SELECT region, {WEEK} AS week, SUM(amount) AS gmv FROM orders WHERE day >= '2026-05-11' "
             "GROUP BY region, week ORDER BY region, week")
TOTALS = f"SELECT {WEEK} AS week, SUM(amount) AS gmv FROM orders WHERE day >= '2026-05-11' GROUP BY week ORDER BY week"
EAST = (f"SELECT channel, {WEEK} AS week, SUM(amount) AS gmv FROM orders WHERE region = '华东' AND day >= '2026-05-11' "
        "GROUP BY channel, week ORDER BY channel, week")
WEEKLY = f"SELECT {WEEK} AS week, SUM(amount) AS gmv FROM orders GROUP BY week ORDER BY week"
TWO_WEEKS = {"period": "week", "base": "2026-05-11", "target": "2026-05-18", "measure": "gmv"}
A1_ARGS = {"mode": "additive", "query": "Q1", "by": ["region"], **TWO_WEEKS, "label": "销售额",
           "headline": {"base": "Q2.r0.gmv", "target": "Q2.r1.gmv"}, "hypothesis": "下降是不是集中在某个地区"}
A2_ARGS = {"mode": "additive", "query": "Q3", "by": ["channel"], **TWO_WEEKS, "parent": {"of": "A1", "key": ["华东"]},
           "hypothesis": "华东是哪个渠道掉的"}


def _orders():
    out, i = [], 0
    base = {"华东": 400, "华南": 300, "华北": 250, "西南": 50}
    for w in range(12):
        monday = date(2026, 3, 2) + timedelta(weeks=w)
        for region, amount in base.items():
            for channel, share in (("线上", 3), ("线下", 2)):
                value = amount * share // 5 + (w % 3) * 2
                if w == 11 and region == "华东":
                    value = value * 7 // 10
                for offset in (2, 6):
                    i += 1
                    out.append((i, (monday + timedelta(days=offset)).isoformat(), region, channel, value))
    return out


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def shop(tmp_path, scratch_source):
    """虚构零售库 shop（tests/conftest.py 的 scratch_source：同名的旧源连同数据目录、现查缓存先删掉，用完再删）。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, day TEXT, region TEXT, channel TEXT, amount INTEGER);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", _orders())
    db.commit()
    db.close()
    return await scratch_source("shop", path)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


class Script:
    """Agent 依次发 calls 里的工具调用，再给结论；seen 记下每次带工具的调用看到的消息。"""

    def __init__(self, monkeypatch, calls):
        from app.providers import mock_model

        self.seen: list[list] = []

        def decide(model, messages):
            if not model.tools:
                return AIMessage(content="收尾")
            self.seen.append(list(messages))
            done = sum(isinstance(m, ToolMessage) for m in messages)
            if done < len(calls):
                name, args = calls[done]
                return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
            return AIMessage(content="销售额下降主要来自华东。")

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)

    def replies(self):
        return [m.content for m in self.seen[-1] if isinstance(m, ToolMessage)]


class Counting:
    """真算子外面包一层只数次数（重放时不该再算）。"""

    def __init__(self, monkeypatch):
        self.calls = 0
        real = analysis_ops.run

        async def run(*args, **kwargs):
            self.calls += 1
            return await real(*args, **kwargs)

        monkeypatch.setattr(analysis_ops, "run", run)


async def wait(run_id, statuses=("succeeded", "failed")):
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def events(run_id, etype=None, node_id=None):
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return [e.data for e in (await session.execute(q.order_by(RunEvent.seq))).scalars()]


def agent_graph(approval="never", tools=(TOOL, DECOMPOSE)):
    return chain(node("start", "input"),
                 node("bot", "agent", prompt="销售额为什么下降", tools=list(tools), approval=approval),
                 node("out", "output", fields=[{"name": "answer", "value": "{{ nodes.bot.text }}"}]))


def recomputes(stored):
    """按封存的工件复算（查询快照、上一层结论都按 sha256 取回），和封存的逐字段相同。"""
    again = analysis_ops.recompute(stored, artifact_store.load, load_result=artifact_store.load)
    assert contract.same(stored, again), {k: (stored.get(k), again.get(k)) for k in stored if stored.get(k) != again.get(k)}


AGENT_CALLS = [(TOOL, {"sql": BY_REGION}), (TOOL, {"sql": TOTALS}), (TOOL, {"sql": EAST}),
               (DECOMPOSE, A1_ARGS), (DECOMPOSE, A2_ARGS)]


# --------------------------------------------------------------------------
# agent 分析工具：拆解、下钻
# --------------------------------------------------------------------------


async def test_an_agent_decomposes_and_drills_down_with_the_real_operator(monkeypatch):
    script = Script(monkeypatch, AGENT_CALLS)
    run = await run_manager.start(graph=agent_graph(), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    ends = await events(row.id, "tool.end", "bot")
    q1, q2, q3 = (ends[i]["query_artifact"] for i in range(3))
    a1_artifact, a2_artifact = ends[3]["analysis_artifact"], ends[4]["analysis_artifact"]
    a1, a2 = artifact_store.load(a1_artifact), artifact_store.load(a2_artifact)

    # 结论：华东是主要来源，下钻到渠道是线上；总数闭合（headline 是 Q2 的两格）
    assert a1["status"] == "ok" and a1["verdict"] == {"code": "driver", "subjects": [["华东"]], "direction": "down"}
    assert "主要来自华东" in a1["phrase"] and a1["hypothesis"] == "下降是不是集中在某个地区"
    assert {i["ref"] for i in a1["inputs"]} >= {"Q1", "Q2.r0.gmv", "Q2.r1.gmv"}
    assert a2["verdict"]["code"] == "driver" and a2["verdict"]["subjects"] == [["线上"]]
    # 下钻：算子按上一层结论的内容哈希认 parent，就是存下的那份工件
    [parent] = [i for i in a2["inputs"] if i.get("role") == "parent"]
    assert parent == {"ref": "A1", "artifact": a1_artifact, "role": "parent"}
    assert content_hash(canonical_json(a1)) == a1_artifact

    # 台账：两条 kind analysis，带 call_id，输入是那几份查询快照（下钻的另有上一层结论）
    [finished] = await events(row.id, "node.finished", "bot")
    analyses = [e for e in finished["evidence"] if e["kind"] == "analysis"]
    assert [(e["call_id"], e["artifact"]) for e in analyses] == [("call_3", a1_artifact), ("call_4", a2_artifact)]
    assert set(analyses[0]["inputs"]) == {q1, q2} and set(analyses[1]["inputs"]) == {q3, a1_artifact}
    assert analyses[0] == contract.ledger_entry(a1, node_id="bot", exec_no=1, artifact=a1_artifact, call_id="call_3")

    # 交回模型的：编号、判定语、关键数
    replies = script.replies()
    assert [r.split("\n", 1)[0] for r in replies] == ["【证据 Q1】", "【证据 Q2】", "【证据 Q3】", "【分析 A1】", "【分析 A2】"]
    body = json.loads(replies[3].split("\n", 1)[1])
    assert body["phrase"] == a1["phrase"] and body["verdict"] == "driver" and body["values"] == a1["values"]
    assert body["table"]["rows"][0][0] == "华东"

    # 按封存的工件复算，逐字段相同
    recomputes(a1)
    recomputes(a2)


async def agent_analysis(monkeypatch, tool, calls):
    """Agent 依次查库、最后调一次分析工具（真算子）：交回 (那份结论, 交回模型的那一段的 JSON)。顺带断言台账记了这一条（带
    call_id）、交回模型的编号是「【分析 A1】」、判定语和结论一字不差，并按封存的工件复算、逐字段相同。"""
    script = Script(monkeypatch, calls)
    run = await run_manager.start(graph=agent_graph(tools=(TOOL, tool)), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    ends = await events(row.id, "tool.end", "bot")
    artifact = ends[-1]["analysis_artifact"]
    stored = artifact_store.load(artifact)
    [finished] = await events(row.id, "node.finished", "bot")
    [entry] = [e for e in finished["evidence"] if e["kind"] == "analysis"]
    assert entry == contract.ledger_entry(stored, node_id="bot", exec_no=1, artifact=artifact,
                                          call_id=f"call_{len(calls) - 1}")
    assert set(entry["inputs"]) == {e["query_artifact"] for e in ends[:-1]}
    head, body = script.replies()[-1].split("\n", 1)
    assert head == "【分析 A1】" and json.loads(body)["phrase"] == stored["phrase"]
    recomputes(stored)
    return stored, json.loads(body)


def _live(stored):
    """结论里现查的表范围：按这次运行的数据源探 orders.day。"""
    return [(p["table"], p["column"], p["basis"], p["max"]) for p in stored["probes"]]


async def test_an_agent_puts_the_week_into_its_history_with_the_real_operator(monkeypatch):
    stored, body = await agent_analysis(monkeypatch, BASELINE, [
        (TOOL, {"sql": WEEKLY}),
        (BASELINE, {"query": "Q1", "period": "week", "measure": "gmv", "label": "销售额", "hypothesis": "这周是不是异常"})])
    assert stored["op"] == "baseline" and stored["status"] == "ok" and stored["hypothesis"] == "这周是不是异常"
    assert stored["verdict"] == {"code": "far_outside_band", "direction": "down", "subjects": []}
    assert stored["phrase"].startswith("销售额本期变化 -10.6%，明显超出过去 10 周的常规波动范围")
    assert "record_low" in {c["code"] for c in stored["caveats"]} and body["verdict"] == "far_outside_band"
    assert _live(stored) == [("orders", "day", "live", "2026-05-24")]      # 本期完不完整按现查的表范围判


async def test_an_agent_asks_for_a_trend_with_the_real_operator(monkeypatch):
    stored, body = await agent_analysis(monkeypatch, TREND, [
        (TOOL, {"sql": WEEKLY}), (TREND, {"query": "Q1", "period": "week", "measure": "gmv", "label": "销售额"})])
    assert stored["op"] == "trend" and stored["status"] == "ok"
    assert stored["verdict"] == {"code": "no_trend", "direction": None, "subjects": []}
    assert stored["phrase"] == "过去 12 周销售额没有明显的上升或下降趋势" and stored["values"]["n"] == "12"
    assert _live(stored) == [("orders", "day", "live", "2026-05-24")] and body["values"] == stored["values"]


async def test_an_agent_tests_a_difference_with_the_real_operator(monkeypatch):
    rows = "SELECT region, amount FROM orders WHERE region IN ('华东', '华南') ORDER BY region, day"
    stored, body = await agent_analysis(monkeypatch, DIFFER, [
        (TOOL, {"sql": rows}),
        (DIFFER, {"mode": "rows", "query": "Q1", "by": ["region"], "value": "amount", "label": "单笔金额"})])
    assert stored["op"] == "differ" and stored["mode"] == "rows" and stored["status"] == "ok"
    assert stored["verdict"] == {"code": "differs", "direction": "up", "subjects": [["华东"], ["华南"]]}
    assert stored["phrase"] == "华东的单笔金额（196.83）高于华南（152），差异站得住"
    assert stored["values"]["n_a"] == stored["values"]["n_b"] == "48" and stored["values"]["test"] == "welch"
    assert body["subjects"] == [["华东"], ["华南"]]


async def test_an_agent_checks_two_weeks_are_comparable_with_the_real_operator(monkeypatch):
    stored, body = await agent_analysis(monkeypatch, COMPARABLE, [
        (TOOL, {"sql": _week_sql("2026-05-18", "2026-05-24")}), (TOOL, {"sql": _week_sql("2026-05-11", "2026-05-17")}),
        (COMPARABLE, {"base_query": "Q2", "target_query": "Q1", "measure": "gmv"})])
    assert stored["op"] == "comparable" and stored["status"] == "ok" and stored["phrase"] == "两期可比"
    assert stored["params"]["base_query"] == "Q2" and stored["params"]["target_query"] == "Q1"
    values = stored["values"]
    assert (values["base_start"], values["base_end"], values["target_start"], values["target_end"]) == (
        "2026-05-11", "2026-05-17", "2026-05-18", "2026-05-24")
    assert {values[k] for k in ("same_query", "window", "complete", "weekdays", "data")} == {"pass"}
    assert _live(stored) == [("orders", "day", "live", "2026-05-24")] and body["verdict"] == "comparable"


async def test_replaying_after_approvals_does_not_recompute(monkeypatch):
    counted = Counting(monkeypatch)
    script = Script(monkeypatch, AGENT_CALLS)
    run = await run_manager.start(graph=agent_graph(approval="always"), input_payload={})
    for _ in AGENT_CALLS:
        row = await wait(run.id, ("interrupted", "failed", "succeeded"))
        assert row.status == "interrupted", row.error
        await run_manager.resume(run.id, {"approved": True})
    row = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert row.status == "succeeded", row.error

    assert counted.calls == 2, "重放时分析又算了一次"
    ends = await events(row.id, "tool.end", "bot")
    assert len(ends) == len(AGENT_CALLS)
    a1_artifact, a2_artifact = ends[3]["analysis_artifact"], ends[4]["analysis_artifact"]
    assert [r.split("\n", 1)[0] for r in script.replies()][-2:] == ["【分析 A1】", "【分析 A2】"]
    [parent] = [i for i in artifact_store.load(a2_artifact)["inputs"] if i.get("role") == "parent"]
    assert parent["artifact"] == a1_artifact
    [finished] = await events(row.id, "node.finished", "bot")
    assert [e["artifact"] for e in finished["evidence"] if e["kind"] == "analysis"] == [a1_artifact, a2_artifact]


# --------------------------------------------------------------------------
# 分析卡：波动基线 + 变化拆解
# --------------------------------------------------------------------------


async def test_an_analysis_card_runs_the_real_operators():
    items = [
        {"id": "wow", "op": "baseline", "from": "weekly", "params": {"period": "week", "measure": "gmv", "label": "销售额"}},
        {"id": "where", "op": "decompose", "mode": "additive", "from": "by_region",
         "params": {"by": ["region"], **TWO_WEEKS, "label": "销售额",
                    "headline": {"base": "weekly.r10.gmv", "target": "weekly.r11.gmv"}}},
    ]
    graph = chain(node("start", "input"), node("weekly", "tool", tool=TOOL, args={"sql": WEEKLY}),
                  node("by_region", "tool", tool=TOOL, args={"sql": BY_REGION}),
                  node("card", "analysis", items=items),
                  node("out", "output", fields=[{"name": "判定", "value": "{{ nodes.card.results.wow.phrase }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    [finished] = await events(row.id, "node.finished", "card")
    results = artifact_store.load(finished["artifact"])["results"]
    wow, where = results["wow"], results["where"]
    assert wow["status"] == "ok" and wow["verdict"] in ("outside_band", "far_outside_band") and wow["direction"] == "down"
    assert where["status"] == "ok" and where["verdict"] == "driver" and "主要来自华东" in where["phrase"]
    assert row.output["判定"] == wow["phrase"]

    baseline = artifact_store.load(wow["artifact"])
    # 本期完不完整按现查的表范围判（analysis.make_prober 按这次运行的数据源探 orders.day）：探到了，不再说核对不了
    [probe] = baseline["probes"]
    assert (probe["source"], probe["table"], probe["column"], probe["basis"]) == ("shop", "orders", "day", "live")
    assert probe["max"] == "2026-05-24"
    assert not [c for c in baseline["caveats"] if c["code"] == "comparability"]
    decomposed = artifact_store.load(where["artifact"])
    assert decomposed["verdict"]["subjects"] == [["华东"]]
    entries = finished["evidence"]
    assert [(e["kind"], e["item"], e["artifact"]) for e in entries] == [
        ("analysis", "wow", wow["artifact"]), ("analysis", "where", where["artifact"])]
    recomputes(baseline)
    recomputes(decomposed)


async def test_an_analysis_card_reads_a_merged_query():
    """分析卡的输入是合并查询（merge 节点）：按合并查询节点的做法取它的快照，输入写明 sqlite 方言（合并查询在 SQLite 里
    执行），真算子照常拆、复算逐字段相同。"""
    where = {"id": "where", "op": "decompose", "mode": "additive", "from": "merged",
             "params": {"by": ["region"], **TWO_WEEKS, "label": "销售额"}}
    graph = chain(node("start", "input"), node("by_region", "tool", tool=TOOL, args={"sql": BY_REGION}),
                  node("merged", "merge", inputs={"a": "by_region"}, sql="SELECT region, week, gmv FROM a"),
                  node("card", "analysis", items=[where]),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.results.where.phrase }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "card")
    results = artifact_store.load(finished["artifact"])["results"]
    stored = artifact_store.load(results["where"]["artifact"])
    assert stored["verdict"]["code"] == "driver" and stored["verdict"]["subjects"] == [["华东"]]
    assert row.output["r"] == stored["phrase"] and "主要来自华东" in stored["phrase"]
    [given] = stored["inputs"]
    [merged] = await events(row.id, "node.finished", "merged")
    assert (given["ref"], given["role"], given["dialect"]) == ("merged", "data", "sqlite")
    assert given["artifact"] == merged["evidence"][0]["artifact"]
    assert [(e["kind"], e["item"]) for e in finished["evidence"]] == [("analysis", "where")]
    recomputes(stored)


# --------------------------------------------------------------------------
# 口径卡的可比性挂钩：真 comparable
# --------------------------------------------------------------------------


def _week_sql(start: str, end: str) -> str:
    return f"SELECT SUM(amount) AS gmv FROM orders WHERE day BETWEEN '{start}' AND '{end}'"


@pytest.mark.parametrize("this_week, verdict, has_reasons", [
    (("2026-05-18", "2026-05-24"), "comparable", False),
    (("2026-05-18", "2026-05-23"), "conditional", True),       # 本期 6 天对上期 7 天：窗口长度、周末天数都不一样
])
async def test_a_caliber_card_checks_two_periods_with_the_real_operator(this_week, verdict, has_reasons):
    metrics = [{"id": "wow", "name": "销售额环比", "decimals": 4,
                "expression": "(cell(nodes.this_week, 0, 'gmv') - cell(nodes.last_week, 0, 'gmv'))"
                              " / cell(nodes.last_week, 0, 'gmv')"}]
    graph = chain(node("start", "input"), node("this_week", "tool", tool=TOOL, args={"sql": _week_sql(*this_week)}),
                  node("last_week", "tool", tool=TOOL, args={"sql": _week_sql("2026-05-11", "2026-05-17")}),
                  node("card", "metrics", caliber="周报口径", metrics=metrics),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "card")
    [metric] = artifact_store.load(finished["artifact"])["metrics"]
    found = metric["comparability"]
    stored = artifact_store.load(found["artifact"])
    assert stored["op"] == "comparable" and stored["params"]["base_query"] == "last_week"
    assert stored["params"]["target_query"] == "this_week"
    assert found == {"artifact": found["artifact"], "verdict": verdict, "phrase": stored["phrase"],
                     "reasons": analysis_ops.comparable_reasons(stored)}
    assert bool(found["reasons"]) is has_reasons
    # 整期按现查的表范围判
    assert any(p.get("table") == "orders" and p.get("basis") == "live" for p in stored["probes"])
    recomputes(stored)
    assert not [e for e in await events(row.id, "log", "card") if e.get("code") == "metric_comparability_skipped"]


def _region_sql(region: str, start: str, end: str) -> str:
    return f"SELECT SUM(amount) AS gmv FROM orders WHERE region = '{region}' AND day BETWEEN '{start}' AND '{end}'"


@pytest.mark.parametrize("south_week, verdict", [
    (("2026-05-18", "2026-05-24"), None),             # 同一周、只差地区：是两段不是两期，不查
    (("2026-05-11", "2026-05-17"), "incomparable"),   # 日期和地区都不同：两边除了日期，筛选的还不一样
])
async def test_a_ratio_between_two_regions_is_not_a_period_comparison(south_week, verdict):
    """华东对华南的倍数：两份同形 SQL 只差地区字面量，不做可比性检查，带契约的出口照常正式出具（以前判成两期不可比、降档）。
    日期和地区都不同的照查，真算子判不可比，出口记缺口降档。"""
    metrics = [{"id": "times", "name": "华东对华南的倍数", "decimals": 4,
                "expression": "cell(nodes.east, 0, 'gmv') / cell(nodes.south, 0, 'gmv')"}]
    graph = chain(node("start", "input"),
                  node("east", "tool", tool=TOOL, args={"sql": _region_sql("华东", "2026-05-18", "2026-05-24")}),
                  node("south", "tool", tool=TOOL, args={"sql": _region_sql("华南", *south_week)}),
                  node("card", "metrics", caliber="周报口径", metrics=metrics),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}],
                       contract={"metrics_from": ["card"], "required": ["times"]}))
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "card")
    [metric] = artifact_store.load(finished["artifact"])["metrics"]
    issuance = row.output["_issuance"]
    assert not [e for e in await events(row.id, "log", "card") if e.get("code") == "metric_comparability_skipped"]
    if verdict is None:
        assert "comparability" not in metric
        assert issuance["tier"] == "formal" and not issuance.get("gaps") and not issuance.get("comparability")
        return
    found = metric["comparability"]
    assert found["verdict"] == verdict and found["reasons"] == ["两边除了日期，筛选的还不一样"]
    assert issuance["tier"] == "degraded"
    assert issuance["gaps"] == ["指标「华东对华南的倍数」两期不可比：两边除了日期，筛选的还不一样"]
    recomputes(artifact_store.load(found["artifact"]))


# --------------------------------------------------------------------------
# 测试夹具：复用名叫 shop 的源，前后都删干净
# --------------------------------------------------------------------------


async def test_a_scratch_source_starts_clean_and_forgets_its_probes(tmp_path):
    """分析相关的用例都建名叫 shop 的源（tests/conftest.py 的 scratch_source）：别的用例留下的数据目录、coverage 的现查缓存里
    按同一个源 id 记下的表范围（5 分钟有效）会改掉这次的判定。sqlite_source 建的源是新的 id，同名的旧源连同数据目录、现查
    缓存先删掉；forget_source 删源（数据目录随外键级联删除）、清掉现查缓存里它的表范围。"""
    from app.analysis.probe import make_prober
    from app.data import catalog as data_catalog
    from app.data import coverage
    from app.data.catalog import make_item
    from app.db.models import CatalogNote, DataSource
    from tests.fixtures.sources import forget_source, sqlite_source

    path = tmp_path / "other.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (day TEXT, amount INTEGER); INSERT INTO orders VALUES ('2026-01-05', 1);")
    db.close()

    async def leave_traces(source_id):
        async with SessionLocal() as session:
            await data_catalog.write_entry(session, source_id, "orders", {"columns": {"amount": {
                "measure": make_item("flow", "human")}}}, if_version=0, actor=None)
            row = await session.get(DataSource, source_id)
        probe = await make_prober({"scratch": row})("scratch", "orders", "day")
        assert probe["max"] == "2026-01-05" and probe["basis"] == "live"
        assert any(k[0] == source_id for k in coverage._LIVE)

    async def traces(source_id):
        async with SessionLocal() as session:
            notes = (await session.execute(select(CatalogNote).where(CatalogNote.source_id == source_id))).scalars().all()
            row = await session.get(DataSource, source_id)
        return {"row": row is not None, "catalog": len(notes), "probes": sum(k[0] == source_id for k in coverage._LIVE)}

    old = await sqlite_source("scratch", path)
    await leave_traces(old)
    new = await sqlite_source("scratch", path)
    assert new != old and await traces(old) == {"row": False, "catalog": 0, "probes": 0}
    assert await traces(new) == {"row": True, "catalog": 0, "probes": 0}
    await leave_traces(new)
    await forget_source(new)
    assert await traces(new) == {"row": False, "catalog": 0, "probes": 0}
