"""出口契约（引用模式）的分析说法复核、口径卡指标的可比性、报告引用的指标的血缘（00-总方案 §3.4、§3.5、§3.9，D9、D15）。

- 分析说法：出口按报告文档记下的那一项独立再核一遍（verify_doc）。strict 有违规记一条缺口（降档），报告节点报上来的同一条
  不重复收；check 只写进出具声明（analysis_claims，counted 为 false），不影响档位；没记这一项的老文档不核
- 可比性：报告引用了的、契约必需的指标两期不可比记缺口（降档），有条件可比的只写进出具声明（comparability）
- 血缘：报告引用的指标（[[m:]] 片段、句末 [[see:m:…]]）输入追不到原始数据的，受管级别的正式运行记缺口，其余写进出具声明的
  untraced（counted 为 false）
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow, WorkflowVersion
from app.engine import analysis_claims as ac
from app.engine import compiler
from app.engine.nodes.io import _analysis_claims, _comparability
from app.engine.runner import run_manager
from app.engine.schema import NodeType
from app.main import app

METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
           {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain", "expression": "vars.kpi.wow"}]
SOURCE = "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv]]"
PLAIN = "本周销售额 [[m:gmv]]。[[see:m:gmv]]"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(*, report=None, contract=None, fetch=None, metrics=METRICS) -> dict:
    """取数 → 口径卡 → 报告撰写 → 出口（引用模式契约）。缺省取数是数据整形（追不到原始数据）。"""
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        fetch or node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5, 'wow': -3.1}",
                      assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=metrics),
        node("write", "report", instructions="写周报", **(report or {})),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["caliber"], "required": ["gmv"], "strict": True,
                       **(contract or {})}),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def writes(monkeypatch, text: str) -> None:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def issued(graph, *, run_class="exploratory") -> dict:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row.output["_issuance"]


async def governed(client, graph) -> dict:
    """把这张图发布成受管级别，从发布版本发起正式运行。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"分析-{uuid.uuid4().hex[:6]}", graph=graph, status="governed", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        wf_id = wf.id
    res = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert res.status_code == 201, res.text
    row = await wait(res.json()["id"])
    assert row.status == "succeeded", row.error
    return row.output["_issuance"]


async def issuance_event(run_id: str) -> dict:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "issuance"))).scalars()
    return event.data


def said_gaps(issuance: dict) -> list[str]:
    return [g for g in issuance["gaps"] if "分析说法" in g]


# --------------------------------------------------------------------------
# 分析说法：出口独立复核
# --------------------------------------------------------------------------


async def test_strict_leftovers_degrade_with_one_gap_from_the_exit(monkeypatch):
    writes(monkeypatch, SOURCE)
    issuance = await issued(weekly(report={"analysis_claims": "strict"}))
    assert issuance["tier"] == "degraded"
    [gap] = said_gaps(issuance)
    assert gap.startswith("报告「write」有 1 处分析说法未通过核对（1 处判断没挂分析结论）"), "报告节点那一条不重复收"
    declared = issuance["analysis_claims"]
    assert declared["mode"] == "strict" and declared["counted"] is True and declared["count"] == 1
    [v] = declared["violations"]
    assert v["code"] == ac.UNCITED and v["text"] == "主要来自" and v["claim"] == "source" and v["span"]


async def test_check_leftovers_are_declared_but_do_not_change_the_tier(monkeypatch):
    writes(monkeypatch, SOURCE)
    issuance = await issued(weekly())
    assert issuance["tier"] == "formal" and said_gaps(issuance) == []
    declared = issuance["analysis_claims"]
    assert declared["mode"] == "check" and declared["counted"] is False and declared["counts"] == {ac.UNCITED: 1}


async def test_a_clean_report_declares_nothing_under_check_and_a_clean_slate_under_strict(monkeypatch):
    writes(monkeypatch, PLAIN)
    assert "analysis_claims" not in await issued(weekly())
    issuance = await issued(weekly(report={"analysis_claims": "strict"}))
    assert issuance["tier"] == "formal" and issuance["analysis_claims"] == {
        "mode": "strict", "count": 0, "counts": {}, "counted": True, "violations": []}


async def test_off_and_old_documents_are_not_checked(monkeypatch):
    writes(monkeypatch, SOURCE)
    issuance = await issued(weekly(report={"analysis_claims": "off"}))
    assert "analysis_claims" not in issuance and said_gaps(issuance) == [] and issuance["tier"] == "formal"


async def test_a_narrative_contract_still_takes_the_report_gap(monkeypatch):
    writes(monkeypatch, SOURCE)
    graph = weekly(report={"analysis_claims": "strict"})
    contract = graph["nodes"][-1]["data"]["config"]["contract"]
    del contract["report_from"]
    contract["narrative"] = "{{ nodes.write.text }}"
    issuance = await issued(graph)
    [gap] = said_gaps(issuance)
    assert gap.startswith("报告撰写「write」：报告有 1 处分析说法未通过核对") and issuance["tier"] == "degraded"


def test_the_exit_policy_takes_the_stricter_of_the_doc_and_the_node():
    def run(doc, payload, found=()):
        out = {"gaps": [], "analysis": None, "analysis_skip": {}}
        _analysis_claims(out, doc, payload, list(found), title="周报", node_id="write")
        return out

    violation = {"code": ac.UNCITED, "text": "主要来自", "claim": "source", "op": "decompose", "message": "…"}
    # 老文档（没有这一项）、节点也没写：不核
    assert run({}, {"claims": "off"}) == {"gaps": [], "analysis": None, "analysis_skip": {}}
    # 节点要求 strict，文档里却没有核对记录：照实记缺口
    assert run({}, {"analysis_claims": "strict"})["gaps"] == [
        "报告「周报」要求严格核对分析说法，但文档中没有分析说法的核对记录，分析说法未核对"]
    assert run({}, {"analysis_claims": "check"})["gaps"] == []
    # 文档记的是 check、节点写的是 strict：按更严的
    out = run({ac.DOC_FIELD: ac.record("check")}, {"analysis_claims": "strict", "analysis_gap": "报告有 1 处…"},
              [violation])
    assert len(out["gaps"]) == 1 and out["analysis"]["counted"] is True
    assert out["analysis_skip"] == {"write": {"报告有 1 处…"}}


# --------------------------------------------------------------------------
# 可比性
# --------------------------------------------------------------------------


def test_comparability_gaps_only_for_incomparable_metrics():
    metrics = [
        {"id": "wow", "name": "环比增幅", "comparability": {"verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据",
                                                       "reasons": ["本期只有 3 天数据"], "artifact": "c" * 64}},
        {"id": "aov", "name": "客单价", "comparability": {"verdict": "conditional",
                                                      "phrase": "两期有条件可比：两段的周末天数不同：4 天对 6 天"}},
        {"id": "gmv", "name": "销售额", "comparability": {"verdict": "comparable", "phrase": "两期可比"}},
        {"id": "orders", "name": "订单数"},
    ]
    gaps, declared = _comparability(metrics + metrics[:1])
    assert gaps == ["指标「环比增幅」两期不可比：本期只有 3 天数据"]
    assert [(d["metric"], d["verdict"]) for d in declared] == [("wow", "incomparable"), ("aov", "conditional")]
    assert declared[0]["reasons"] == ["本期只有 3 天数据"] and declared[0]["artifact"] == "c" * 64


@pytest.fixture
def comparability(monkeypatch):
    """口径卡给指标记上可比性（口径卡跨两份同形查询算环比时自动做的检查由入口包实现；这里直接记上）。"""
    marks: dict[str, dict] = {}
    original = compiler.RUNNERS[NodeType.METRICS]

    async def card(state, ctx):
        updates = await original(state, ctx)
        for metric in updates["nodes"][ctx.node.id]["metrics"]:
            if metric["id"] in marks:
                metric["comparability"] = marks[metric["id"]]
        return updates

    monkeypatch.setitem(compiler.RUNNERS, NodeType.METRICS, card)
    return marks


async def test_a_cited_incomparable_metric_degrades_and_a_conditional_one_is_noted(monkeypatch, comparability):
    comparability["wow"] = {"verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据",
                            "reasons": ["本期只有 3 天数据"]}
    writes(monkeypatch, "本周销售额 [[m:gmv]]，环比 [[m:wow]]。[[see:m:gmv,m:wow]]")
    issuance = await issued(weekly())
    assert issuance["tier"] == "degraded" and "指标「环比增幅」两期不可比：本期只有 3 天数据" in issuance["gaps"]
    assert [c["metric"] for c in issuance["comparability"]] == ["wow"]
    comparability["wow"] = {"verdict": "conditional", "phrase": "两期有条件可比：两段的周末天数不同"}
    issuance = await issued(weekly())
    assert issuance["tier"] == "formal" and issuance["comparability"][0]["verdict"] == "conditional"


async def test_an_incomparable_metric_nobody_cites_or_requires_is_left_alone(monkeypatch, comparability):
    comparability["wow"] = {"verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据"}
    writes(monkeypatch, PLAIN)
    issuance = await issued(weekly())
    assert issuance["tier"] == "formal" and "comparability" not in issuance
    # 契约必需的指标不引用也算
    issuance = await issued(weekly(contract={"required": ["gmv", "wow"]}))
    assert "指标「环比增幅」两期不可比：本期只有 3 天数据" in issuance["gaps"]


# --------------------------------------------------------------------------
# 血缘：报告引用的指标追没追到原始数据
# --------------------------------------------------------------------------


async def test_a_cited_metric_that_stops_at_a_transform_is_only_noted_outside_governed_runs(monkeypatch):
    writes(monkeypatch, PLAIN)
    issuance = await issued(weekly(), run_class="formal")
    assert issuance["tier"] == "formal" and not any("没追到原始数据" in g for g in issuance["gaps"])
    untraced = issuance["untraced"]
    assert untraced["counted"] is False and [m["metric"] for m in untraced["metrics"]] == ["gmv"]
    assert untraced["metrics"][0]["stops"][0]["node_id"] == "fetch"


async def test_a_governed_formal_run_counts_the_untraced_cited_metric(client, monkeypatch):
    writes(monkeypatch, PLAIN)
    issuance = await governed(client, weekly())
    assert issuance["tier"] == "degraded"
    [gap] = [g for g in issuance["gaps"] if "没追到原始数据" in g]
    assert gap.startswith("指标「销售额」的输入没追到原始数据：vars.kpi.gmv（停在「fetch」）")
    assert issuance["untraced"]["counted"] is True
    # 没被引用的指标（环比增幅同样停在整形节点）不算：只看报告引用了的
    assert not any("环比增幅" in g for g in issuance["gaps"])


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "2026-W37", 100.5), (2, "2026-W37", 200.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        else:
            row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


async def test_a_metric_read_from_a_query_cell_is_traced(client, monkeypatch, shop):
    writes(monkeypatch, PLAIN)
    fetch = node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"})
    graph = weekly(fetch=fetch, metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1,
                                          "expression": "cell(nodes.fetch, 0, 'gmv')"}])
    issuance = await governed(client, graph)
    assert "untraced" not in issuance and not any("没追到原始数据" in g for g in issuance["gaps"])


# --------------------------------------------------------------------------
# 引用了分析结论：输入查询的 SQL 检查问题计入缺口，具名标量记进逐个数字的出处
# --------------------------------------------------------------------------


@pytest.fixture
def analyses(monkeypatch):
    """写作目录里多几条分析结论：{A 编号: 结论工件 id}。台账里的分析结论由入口（分析卡、Agent 的分析工具）记，这里直接
    补进目录：报告节点和出口复核按同一个 report_catalog 建目录，一处补上。"""
    from app.core import artifact_store
    from app.engine.evidence import make_eid
    from app.engine.nodes import report as report_mod

    added: dict[str, str] = {}
    original = report_mod.report_catalog

    def catalog(state, spec, node):
        out = original(state, spec, node)
        for alias, artifact in added.items():
            out[alias] = {"alias": alias, "kind": "analysis", "artifact": artifact, "item": "drop",
                          "eid": make_eid("analysis", artifact, {}), "locator": {},
                          "result": artifact_store.load(artifact)}
        return out

    monkeypatch.setattr(report_mod, "report_catalog", catalog)
    return added


async def stored_driver(*, checks=None) -> str:
    """一份「销售额下降主要来自华东」的拆解结论，输入是一份（可能带着 SQL 检查问题的）查询快照，都存进工件库。"""
    from app.analysis.testing import fake_result
    from app.core import artifact_store

    query = await artifact_store.put_json({"source": "shop", "sql": "SELECT region, week, SUM(amount) AS gmv FROM orders",
                                           "columns": ["region", "week", "gmv"], "rows": [["华东", "W41", 820]],
                                           "truncated": False, **({"checks": checks} if checks else {})},
                                          kind="query_snapshot")
    result = fake_result(inputs=[{"ref": "Q2", "artifact": query, "role": "data",
                                  "columns": ["region", "week", "gmv"], "rows": "all"}])
    return await artifact_store.put_json(result, kind="analysis_result")


FANOUT = {"code": "fanout_sum", "level": "error", "message": "一对多关联后求和，订单金额被重复计算。改法：先汇总再关联"}


@pytest.mark.parametrize("text", [
    "[[a:A1]]。[[see:m:gmv]]",
    "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv,A1]]",
    "华东占变化的 [[a:A1.top_share|pct]]。[[see:m:gmv]]",
])
async def test_an_analysis_built_on_a_failing_query_degrades_like_the_query_itself(monkeypatch, analyses, text):
    analyses["A1"] = await stored_driver(checks=[FANOUT])
    writes(monkeypatch, text)
    issuance = await issued(weekly())
    assert issuance["tier"] == "degraded"
    [gap] = [g for g in issuance["gaps"] if "SQL 检查" in g]
    assert "一对多关联后求和" in gap and "A1" in gap
    analyses["A1"] = await stored_driver()
    clean = await issued(weekly())
    assert clean["tier"] == "formal" and not any("SQL 检查" in g for g in clean["gaps"])


async def test_a_named_scalar_is_listed_as_an_analysis_number(monkeypatch, analyses):
    analyses["A1"] = await stored_driver()
    writes(monkeypatch, "华东占变化的 [[a:A1.top_share|pct]]，下降主要来自华东。[[see:m:gmv,A1]]")
    issuance = await issued(weekly())
    [hit] = [m for m in issuance["matched"] if m.get("analysis")]
    assert hit["analysis"] == "A1.top_share" and hit["token"] == "61.9%" and "input" not in hit
    assert "analysis_claims" not in issuance, "判断词挂对了：check 下没有可标注的"
