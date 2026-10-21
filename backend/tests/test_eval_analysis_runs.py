"""分析期望走真运行（app/eval/gold.py、score.py、runner.py；docs/design/analysis-round/00-总方案.md §3.8）。库是虚构的
retail_trends（tests/fixtures/acceptance/retail_trends.py，按今天生成）；写作模型、助手都换成剧本，不调任何模型。

- 算期望：期望 SQL 的探针运行封存的那份快照上，用平台的算子（真算子、真现查）算出判定；作答前后两次期望照样对得上（运行当天、
  现查时刻、输入快照的工件不比）；阈值按交进来的分析设置快照；参数和结果对不上的照实说、整题没有可用的值。
- 读答案：作答运行台账里的分析结论、报告怎么引用了它（判定语、结果表的格、句末依据）、有判断词的句子；逐题 E1 按分析结论的
  输入查询比。
- 重评老结果：明细里没有分析结论那一项的照样重评（从封存重新读）。
- 从头到尾：助手剧本搭「查库 → 分析卡 → 报告」，报告引用判定语；评测给这一题「对且可追溯」，逐题 E1 等价已证明，作答运行和
  算期望用的是同一份分析设置快照（记进指纹）。
- 分析工具开、关（变体 analysis.tools，「分析工具贡献」预设对照）：一次发起两臂；关的那一臂助手建图时工具清单里没有分析工具、
  节点手册里没有分析卡，作答运行里没有分析结论；开的那一臂照常；两臂的指纹、同一变体的指纹都不同。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run
from app.db.models_eval import EvalCase, EvalResult, EvalRun
from app.engine.runner import run_manager
from app.eval import cases, gold, judgements, runner, score
from app.eval.gold import Context
from tests import eval_samples as es
from tests.fixtures.acceptance import retail_trends

TZ = "Asia/Shanghai"
SUITE = json.loads((Path(__file__).parent / "fixtures" / "eval" / "analysis_suite.json").read_text(encoding="utf-8"))
#: 题集里的第 1 题（上周销售额下降主要来自华东）和第 3 题（订单数的常规波动）
REGION = next(c for c in SUITE["cases"] if c["seq"] == 1)
ORDERS = next(c for c in SUITE["cases"] if c["seq"] == 3)


@pytest.fixture(autouse=True)
async def engine_up(monkeypatch):
    from app.eval import snapshots

    monkeypatch.setattr(snapshots, "MAX_SOURCES", 10_000)
    await run_manager.setup()
    yield
    await runner.manager.shutdown()
    await es.drop_suites()
    await run_manager.shutdown()


@pytest.fixture
async def source(tmp_path, scratch_source):
    path = retail_trends.build(tmp_path / "trends.db", date.today())
    sid = await scratch_source(retail_trends.SOURCE_NAME, path, schema=True)
    return {"id": sid, "name": retail_trends.SOURCE_NAME, "path": path}


def now() -> datetime:
    return datetime.now(ZoneInfo(TZ))


def ctx(**kw) -> Context:
    return Context(as_of=now(), tz=TZ, actor="评测测试", suite_id="s1", suite_name="虚构分析题集", **kw)


def case_of(raw: dict, source_id: str, *, expectations: list[dict] | None = None, sql: str | None = None) -> dict:
    fields = cases.normalize_case({"question": raw["question"], "datasource_ids": [source_id],
                                   "gold": {"kind": "sql", "datasource_id": source_id, "sql": sql or raw["gold"]["sql"]},
                                   "expectations": expectations or raw["expectations"]})
    return {**fields, "id": None, "seq": raw["seq"]}


def rendered(sql: str) -> str:
    return cases.render(sql, cases.variable_values(now(), TZ))


# --------------------------------------------------------------------------
# 算期望
# --------------------------------------------------------------------------


async def test_the_gold_runs_the_platform_analysis_on_the_probe_snapshot(source):
    import sqlite3

    case = case_of(ORDERS, source["id"])
    before = await gold.compute_gold(case, ctx())
    assert before.ok, before.reason
    band, orders = before.sets[0].values["band"], before.sets[0].values["orders"]
    assert (band["kind"], band["status"], band["judgement"]) == ("verdict", "ok", "normal"), band
    assert band["phrase"].startswith("订单数本期变化") and "常规波动范围内" in band["phrase"]
    result = band["result"]
    assert result["inputs"][0]["ref"] == judgements.GOLD_REF and result["inputs"][0]["artifact"] == before.sets[0].snapshot
    assert result["params"]["today"] == date.today().isoformat()
    assert result["settings"]["k_warn"] == 2.0, "没有设置快照（试算）时现读设置，缺省 2 倍"
    # 本期齐不齐按现查的表范围判（SQL 里按日期算出来的界也在库里算一次），都记进结论
    assert [(p["table"], p["column"], p["basis"]) for p in result["probes"] if "table" in p] == \
        [("orders", "order_date", "live")], result["probes"]
    assert all(p.get("at") for p in result["probes"])
    with sqlite3.connect(source["path"]) as db:
        direct = db.execute(rendered(ORDERS["gold"]["sql"])).fetchall()
    assert orders["value"] == direct[24][1] and len(direct) == 25

    # 作答后再算一次：快照的工件（记着查询用时）、现查时刻都不同，期望照样对得上
    after = await gold.compute_gold(case, ctx(role="gold_after"))
    assert after.sets[0].snapshot != before.sets[0].snapshot
    assert gold.settle(before, after).status == "ok"


async def test_the_settings_snapshot_decides_the_thresholds(source):
    case = case_of(ORDERS, source["id"])
    outcome = await gold.compute_gold(case, ctx(analysis={"min_history": 30, "max_history": 52}))
    assert outcome.ok, outcome.reason
    band = outcome.sets[0].values["band"]
    assert band["status"] == "refused" and band["reason_code"] == "history_short", band
    assert band["result"]["settings"]["min_history"] == 30
    graded = score.score_case(case, gold.settle(outcome, outcome).to_dict(), score.Answer(run_id="r"), path="ran")
    assert graded.scores[0]["outcome"] == "ungradable" and graded.scores[0]["reason"].startswith("期望算不了：")


async def test_parameters_that_do_not_fit_the_sql_are_said_plainly(source):
    wrong_column = case_of(REGION, source["id"], expectations=[
        {"kind": "contribution", "mode": "additive", "params": {"by": ["门店"], "period": "周", "measure": "销售额"}}])
    outcome = await gold.compute_gold(wrong_column, ctx())
    assert outcome.status == "no_value" and "期望 SQL 里没有「门店」这一列" in outcome.reason, outcome.reason
    three_weeks = REGION["gold"]["sql"].replace("'-7 days'", "'-14 days'")
    outcome = await gold.compute_gold(case_of(REGION, source["id"], sql=three_weeks), ctx())
    assert outcome.status == "no_value" and "有 3 期" in outcome.reason, outcome.reason


# --------------------------------------------------------------------------
# 读答案、重评老结果
# --------------------------------------------------------------------------

REPORT = ("## 上周销售额\n\n上周销售额下降：[[a:A1]]。华东的变化是 [[v:A1.r0.delta]]。\n\n"
          "下降主要来自华东，其余地区变化不大。[[see:A1]]")


def _node(nid: str, ntype: str, **config) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _card_item() -> dict:
    values = cases.variable_values(now(), TZ)
    target = values["last_week_start"]
    base = (date.fromisoformat(target) - timedelta(days=7)).isoformat()
    return {"id": "where", "op": "decompose", "mode": "additive", "from": "q",
            "params": {"by": ["地区"], "period": "周", "base": base, "target": target, "measure": "销售额",
                       "label": "销售额"}}


def answer_graph() -> dict:
    nodes = [_node("start", "input"),
             _node("q", "tool", tool=f"db_query__{retail_trends.SOURCE_NAME}", args={"sql": rendered(REGION["gold"]["sql"])}),
             _node("card", "analysis", items=[_card_item()]),
             _node("write", "report", instructions="写上周的周报"),
             _node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


async def _answer_run(monkeypatch) -> Run:
    from tests.test_analysis_card import wait

    es.use_report(monkeypatch, REPORT)
    started = await run_manager.start(graph=answer_graph(), input_payload={})
    row = await wait(started.id)
    assert row.status == "succeeded", row.error
    return row


async def test_reading_the_analyses_of_the_answer_run(monkeypatch, source):
    row = await _answer_run(monkeypatch)
    answer = await score.read_answer(row.id)
    assert answer.problem is None and answer.report == "write"
    [record] = answer.analyses
    assert (record["alias"], record["op"], record["mode"], record["judgement"]) == ("A1", "decompose", "additive",
                                                                                     "source")
    assert record["subjects"][0] == "华东" and record["direction"] == "down"
    assert record["cited"] == ["phrase", "cell", "see"], record
    # 判定语不是答案里的数；结果表的那一格是，维度是那一段（华东）
    [cell] = answer.values
    assert cell["kind"] == "cell" and cell["dims"] == {"地区": "华东"} and cell["artifact"] == record["artifact"]
    # 逐题 E1 拿分析结论的输入查询比
    [query] = answer.queries
    assert answer.cited == [query["artifact"]] and record["inputs"] == [query["artifact"]]
    [sentence] = answer.sentences
    assert sentence["analyses"] == ["A1"] and "主要来自华东" in sentence["text"]

    case = case_of(REGION, source["id"])
    before = await gold.compute_gold(case, ctx())
    settled = gold.settle(before, before).to_dict()
    graded = score.score_case(case, settled, answer, path="ran")
    assert graded.verdict == "pass" and graded.traced, graded.scores
    assert graded.scores[0]["found"]["alias"] == "A1"
    match = await score.sql_match(case, settled, answer)
    assert match["status"] == "proven" and match["basis"] == "cited", match


async def test_rescoring_old_results_reads_the_analyses_from_the_seal(monkeypatch, source):
    from app.eval import rescore
    from app.eval.cases import case_hash

    row = await _answer_run(monkeypatch)
    case = case_of(REGION, source["id"])
    settled = gold.settle(*(2 * [await gold.compute_gold(case, ctx())])).to_dict()
    old = score.Answer(run_id=row.id, text="老的答案").to_dict()
    old.pop("analyses")
    old.pop("sentences")
    details = await artifact_store.put_json({"format": 1, "turn": {"reply": None}, "answer": old}, kind="eval_result")
    suite_id = await es.make_suite(source["id"], cases=[{k: case[k] for k in ("question", "datasource_ids", "gold",
                                                                                "expectations")}])
    async with SessionLocal() as session:
        stored = (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id))).scalar_one()
        ev = EvalRun(suite_id=suite_id, status="succeeded", fingerprint={"scorer_version": 2})
        session.add(ev)
        await session.flush()
        session.add(EvalResult(eval_run_id=ev.id, case_id=stored.id, case_hash=case_hash(case), status="ok",
                               path="ran", run_id=row.id, verdict="fail", details_artifact=details, gold=settled,
                               scores=[{"id": "source", "kind": "contribution", "required": True, "outcome": "wrong"}]))
        await session.commit()
        ev_id = ev.id
    assert await rescore.rescore(ev_id) == 1
    async with SessionLocal() as session:
        result = (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == ev_id))).scalar_one()
        assert result.verdict == "pass" and result.scores[0]["outcome"] == "traced", result.scores
        assert (await session.get(EvalRun, ev_id)).fingerprint["scorer_version"] == score.SCORER_VERSION == 3
        assert (await session.get(EvalRun, ev_id)).summary["outcomes_by_kind"] == {"contribution": {"traced": 1}}


# --------------------------------------------------------------------------
# 从头到尾
# --------------------------------------------------------------------------


def _line(op: dict) -> AIMessageChunk:
    return AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


def _graph_lines(graph: dict, summary: str):
    yield _line({"op": "plan", "summary": summary})
    for n in graph["nodes"]:
        yield _line({"op": "add_node", "node": {"id": n["id"], "type": n["type"], "label": n["data"]["label"],
                                                "config": n["data"]["config"]}})
    for e in graph["edges"]:
        yield _line({"op": "add_edge", "edge": e})
    yield _line({"op": "done", "explanation": "拆一下", "run": True})


class AnalysisCopilot:
    """助手剧本：直接搭「入口 → 查库 → 分析卡 → 报告 → 出口」并运行；上线前自查直接过。"""

    async def astream(self, messages):
        if "上线前自查" in messages[1][1]:
            yield _line({"op": "done"})
            return
        for line in _graph_lines(answer_graph(), "按地区拆上周销售额的变化"):
            yield line


async def test_an_analysis_case_end_to_end(monkeypatch, source):
    from app.api.settings import analysis_settings

    es.use_copilot(monkeypatch, AnalysisCopilot())
    es.use_report(monkeypatch, REPORT)
    case = case_of(REGION, source["id"])
    suite_id = await es.make_suite(source["id"], cases=[{k: case[k] for k in ("question", "datasource_ids", "gold",
                                                                                "expectations")}])
    from app.db.models_eval import EvalSuite
    from app.eval.variants import normalize_variant

    async with SessionLocal() as session:
        suite = await session.get(EvalSuite, suite_id)
        [ev] = await runner.start(session, suite, [normalize_variant({})], actor="评测测试")
        expected_settings = await analysis_settings(session)
    await es.wait_eval(ev.id)
    async with SessionLocal() as session:
        ev = await session.get(EvalRun, ev.id)
        [item] = (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == ev.id))).scalars()
        answer_run = await session.get(Run, item.run_id)
    assert ev.status == "succeeded", (ev.error, item.error)
    assert item.path == "ran" and item.verdict == "pass", (item.error, item.scores)
    [scored] = item.scores
    assert scored["outcome"] == "traced" and scored["want"]["subjects"] == ["华东"], scored
    assert item.sql_match["status"] == "proven", item.sql_match
    # 算期望和作答用同一份分析设置快照，记进指纹
    assert ev.fingerprint["analysis"] == expected_settings
    assert answer_run.options["analysis"] == expected_settings
    assert item.gold["before"]["sets"][0]["values"]["source"]["result"]["settings"]["main_share"] == \
        expected_settings["main_share"]
    assert ev.summary["outcomes_by_kind"] == {"contribution": {"traced": 1}}
    assert ev.fingerprint["scorer_version"] == 3


async def test_suites_without_analysis_expectations_take_no_settings_snapshot(monkeypatch, tmp_path):
    from tests.fixtures.sources import drop_source

    es.use_copilot(monkeypatch, es.Copilot("none"))
    name, sid = await es.make_source(tmp_path)
    try:
        es.use_copilot(monkeypatch, es.Copilot(name))
        es.use_report(monkeypatch)
        suite_id = await es.make_suite(sid)
        from app.db.models_eval import EvalSuite
        from app.eval.variants import normalize_variant

        async with SessionLocal() as session:
            suite = await session.get(EvalSuite, suite_id)
            [ev] = await runner.start(session, suite, [normalize_variant({})], actor="评测测试")
        await es.wait_eval(ev.id)
        async with SessionLocal() as session:
            ev = await session.get(EvalRun, ev.id)
            [item] = (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == ev.id))).scalars()
            answer_run = await session.get(Run, item.run_id)
        assert item.verdict == "pass", item.error
        assert "analysis" not in ev.fingerprint and "analysis" not in (answer_run.options or {}), \
            "别的题集的指纹、作答运行的选项和以前一样"
    finally:
        await drop_source(sid)


# --------------------------------------------------------------------------
# 分析工具开、关（变体 analysis.tools；「分析工具贡献」预设对照）
# --------------------------------------------------------------------------

#: 关的那一臂搭的图里，报告撰写的要求带这个记号：报告撰写的剧本据此不引用分析结论（那一臂没有）
PLAIN_MARK = "只按查询结果写"
PLAIN = "## 上周销售额\n\n各地区上周的销售额都在查询结果里。"


def plain_graph() -> dict:
    """没有分析卡的图：入口 → 查库 → 报告 → 出口。"""
    nodes = [_node("start", "input"),
             _node("q", "tool", tool=f"db_query__{retail_trends.SOURCE_NAME}",
                   args={"sql": rendered(REGION["gold"]["sql"])}),
             _node("write", "report", instructions=f"{PLAIN_MARK}上周的周报"),
             _node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


class SwitchCopilot:
    """助手剧本：两臂都先照「查库 → 分析卡 → 报告」搭——关掉分析工具的那一臂靠真代码（answer_ops 的自查）挡回去，
    替身不替它判断。自查交回来时（只有关的那一臂会有）去掉分析卡、把查库直接接到报告。systems 记下每次建图收到的 system。"""

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def astream(self, messages):
        if "上线前自查" in messages[1][1]:
            yield _line({"op": "remove_node", "id": "card"})
            yield _line({"op": "add_edge", "edge": {"source": "q", "target": "write"}})
            # 这一臂没有分析结论可引：报告只按查询结果写（报告撰写的剧本认这个记号，见 use_writers）
            yield _line({"op": "update_node", "id": "write", "config": {"instructions": f"{PLAIN_MARK}上周的周报"}})
            yield _line({"op": "done", "explanation": "这一轮没有分析工具，去掉分析卡"})
            return
        role, system = messages[0]
        assert role == "system"
        self.systems.append(system)
        for line in _graph_lines(answer_graph(), "看上周销售额"):
            yield line


def use_writers(monkeypatch) -> None:
    """报告撰写的剧本：要求里带 PLAIN_MARK 的（关掉分析工具那一臂）写 PLAIN，别的写引用了 A1 的 REPORT。"""
    from app.providers import mock_model

    def decide(self, messages):
        said = "\n".join(str(getattr(m, "content", "")) for m in messages)
        return AIMessage(content=PLAIN if PLAIN_MARK in said else REPORT)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def test_the_analysis_tools_preset_runs_with_and_without_them(monkeypatch, source):
    from httpx import ASGITransport, AsyncClient

    from app.eval import compare
    from app.main import app
    from app.tools.analysis import ANALYSIS_TOOLS

    copilot = SwitchCopilot()
    es.use_copilot(monkeypatch, copilot)
    use_writers(monkeypatch)
    case = case_of(REGION, source["id"])
    suite_id = await es.make_suite(source["id"], cases=[{k: case[k] for k in ("question", "datasource_ids", "gold",
                                                                                "expectations")}])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/api/evals/suites/{suite_id}/evaluations",
                                    json={"preset": "analysis_tools", "variant": {"repeats": 1}})
        assert started.status_code == 202, started.text
        body = started.json()
        on, off = body["evaluations"]
        assert (on["arm"], off["arm"]) == ("分析工具开", "分析工具关") and body["pair_id"] == on["id"] == off["pair_id"]
        assert on["variant"]["analysis"] == {"tools": "on"} and off["variant"]["analysis"] == {"tools": "off"}
        assert on["variant"]["catalog"] == off["variant"]["catalog"] == {"mode": "live", "snapshot": None}
        assert "· 分析工具开 ·" in on["variant_text"] and "· 分析工具关 ·" in off["variant_text"], off["variant_text"]
        await es.wait_eval(on["id"])
        pair = (await client.get(f"/api/evals/pairs/{body['pair_id']}")).json()
    assert [a["arm"] for a in pair["table"]["arms"]] == ["分析工具开", "分析工具关"]

    async with SessionLocal() as session:
        runs = {r.arm: r for r in (await session.execute(select(EvalRun).where(EvalRun.pair_id == on["id"]))).scalars()}
        items = {arm: (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == r.id))).scalar_one()
                 for arm, r in runs.items()}
        answer_runs = {arm: await session.get(Run, item.run_id) for arm, item in items.items()}
    assert all(r.status == "succeeded" for r in runs.values()), [(r.arm, r.error) for r in runs.values()]
    # 建图：两臂各一次，关的那一臂工具清单里没有分析工具、节点手册里没有分析卡；开的那一臂都有
    assert len(copilot.systems) == 2
    with_tools = [s for s in copilot.systems if all(name in s for name in ANALYSIS_TOOLS)]
    without = [s for s in copilot.systems if not any(name in s for name in ANALYSIS_TOOLS)]
    assert len(with_tools) == len(without) == 1
    assert "- analysis：分析卡" in with_tools[0] and "- analysis：分析卡" not in without[0]

    # 开的那一臂照常：图里有分析卡，答案引用了分析结论，对且可追溯
    lit, dark = items["分析工具开"], items["分析工具关"]
    assert {n["type"] for n in answer_runs["分析工具开"].graph["nodes"]} >= {"analysis", "report"}
    assert lit.verdict == "pass" and lit.scores[0]["outcome"] == "traced", lit.scores
    assert [a["alias"] for a in (await score.read_answer(lit.run_id)).analyses] == ["A1"]
    # 关的那一臂：图里没有分析卡，作答运行里没有分析结论；正文没写这个判断，算错
    assert "analysis" not in {n["type"] for n in answer_runs["分析工具关"].graph["nodes"]}
    assert (await score.read_answer(dark.run_id)).analyses == []
    assert dark.verdict == "fail" and dark.scores[0]["outcome"] == "wrong", dark.scores
    assert "没有做变化拆解" in dark.scores[0]["reason"], dark.scores[0]
    # 两臂共用期望；指纹、同一变体的指纹都不同，对比照实写「分析工具：开 → 关」
    assert lit.gold_run_ids == dark.gold_run_ids
    fp_on, fp_off = runs["分析工具开"].fingerprint, runs["分析工具关"].fingerprint
    assert (fp_on["analysis_tools"], fp_off["analysis_tools"]) == ("on", "off")
    assert fp_on["variant_key"] != fp_off["variant_key"]
    assert [d for d in compare.fingerprint_diff(fp_on, fp_off) if d["key"] == "analysis_tools"] == \
        [{"key": "analysis_tools", "label": "分析工具", "before": "开", "after": "关"}]
