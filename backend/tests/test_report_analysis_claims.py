"""报告撰写节点的分析说法核对（analysis_claims）和分析范围（analysis_scope），00-总方案 §3.5–3.6。

- check（缺省，问数据也是它）：判断词没挂对分析结论的句子交回写作者重写一次（不按 max_repairs 反复），仍有的标在报告里，
  节点照常产出、不写缺口
- strict：按 max_repairs 重写，仍有的写进产出的 issuance_gaps（带契约的出口降档）；即使「违规处置」是判为失败也不让节点失败
- off：不核，写作规则、文档和以前一样
- 写作规则：有分析结论和没有时各一种说法；重写请求列出违规和修法；写作者的角色说「原因只按证据写」
- 分析范围：这次有分析结论、取值 auto 时文档带系统写的 analysis_scope，off 或没有分析结论时不带
- 取值写错：画布校验拦住，执行时节点报同一句话
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.analysis.testing import fake_result
from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import analysis_claims as ac
from app.engine import runner as runner_mod
from app.engine.evidence import make_eid
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager
from app.engine.schema import analysis_claims_problem, analysis_scope_problem


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 挂了指标依据、可「主要来自」没有拆解可挂
SOURCE = "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv]]"
PLAIN = "本周销售额 [[m:gmv]]。[[see:m:gmv]]"


def weekly(**report):
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def script(monkeypatch, *replies) -> list:
    from app.providers import mock_model

    seen: list = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


async def finish(graph, *, run_class="exploratory") -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype).order_by(RunEvent.seq)
        return list((await session.execute(q)).scalars())


async def checked_of(row: Run) -> tuple[dict, dict, dict]:
    """(report.checked 的载荷, 文档, 报告节点产出)。"""
    [checked] = await events(row.id, "report.checked")
    [finished] = [e for e in await events(row.id, "node.finished") if e.node_id == "write"]
    return (checked.data, artifact_store.load(checked.data["doc_artifact"]),
            artifact_store.load(finished.data["artifact"]))


async def logs(row: Run, code: str) -> list[dict]:
    return [e.data for e in await events(row.id, "log") if e.data.get("code") == code]


def said(violations: list[dict]) -> list[str]:
    return [v["code"] for v in violations if v["code"] in ac.CODES]


def human_texts(messages) -> str:
    return "\n".join(str(m.content) for m in messages if m.type == "human")


# --------------------------------------------------------------------------
# check：重写一次，仍有的标出
# --------------------------------------------------------------------------


async def test_check_is_the_default_rewrites_once_and_flags_what_is_left(monkeypatch):
    seen = script(monkeypatch, SOURCE)
    row = await finish(weekly(max_repairs=3))
    assert row.status == "succeeded", row.error
    assert len(seen) == 2, "check 只重写一次，不按最多重写次数反复"
    first, again = human_texts(seen[0]), str(seen[1][-1].content)
    assert ac.ANALYSIS_NONE_PROMPT in first, "没有分析结论时告诉写作者不下这类判断"
    assert "这次没有变化拆解的结论，不能写「主要来自」" in again and ac.ANALYSIS_REPAIR_PROMPT in again
    checked, doc, output = await checked_of(row)
    assert doc[ac.DOC_FIELD] == {"mode": "check", "lexicon": ac.LEXICON_VERSION}
    assert said(checked["violations"]) == [ac.UNCITED] and checked["analysis_claims"] == "check"
    assert checked["failed"] is False and checked["repairs"] == 1
    assert output["analysis_claims"] == "check" and "issuance_gaps" not in output and "analysis_gap" not in output
    [log] = await logs(row, "report_analysis_claims")
    assert log["count"] == 1 and log["texts"] == ["主要来自"] and "已记为缺口" not in log["message"]
    assert "已要求模型重写 1 次仍未改掉" in log["message"] and "1 处判断没挂分析结论" in log["message"]


async def test_a_rewrite_that_drops_the_judgement_is_accepted(monkeypatch):
    seen = script(monkeypatch, SOURCE, "本周销售额 [[m:gmv]]，华东下降最多。[[see:m:gmv]]")
    row = await finish(weekly())
    checked, doc, output = await checked_of(row)
    assert len(seen) == 2 and said(checked["violations"]) == [] and checked["ok"] is True
    assert await logs(row, "report_analysis_claims") == []


async def test_check_does_not_change_how_fatal_violations_repeat(monkeypatch):
    seen = script(monkeypatch, "销售额 45678 元，下降主要来自华东。")
    row = await finish(weekly(max_repairs=2, on_violation="flag"))
    assert row.status == "succeeded" and len(seen) == 3, "裸数字照旧按最多重写次数重写"
    checked, _, _ = await checked_of(row)
    assert {"uncited_number", ac.UNCITED} <= {v["code"] for v in checked["violations"]}


async def test_check_leftovers_never_fail_the_node(monkeypatch):
    script(monkeypatch, SOURCE)
    row = await finish(weekly(on_violation="fail"))
    assert row.status == "succeeded", row.error
    checked, _, _ = await checked_of(row)
    assert checked["failed"] is False and said(checked["violations"]) == [ac.UNCITED]


# --------------------------------------------------------------------------
# strict：按最多重写次数重写，仍有的计入缺口
# --------------------------------------------------------------------------


async def test_strict_rewrites_up_to_max_repairs_and_records_a_gap(monkeypatch):
    seen = script(monkeypatch, SOURCE)
    row = await finish(weekly(analysis_claims="strict", max_repairs=2, on_violation="fail"))
    assert row.status == "succeeded", "说法违规不让节点失败（不挡生成）"
    assert len(seen) == 3
    checked, doc, output = await checked_of(row)
    assert doc[ac.DOC_FIELD]["mode"] == "strict" and checked["failed"] is False
    [gap] = output["issuance_gaps"]
    assert gap == output["analysis_gap"]
    assert gap.startswith("报告有 1 处分析说法未通过核对（1 处判断没挂分析结论：「主要来自」）") and "重写 2 次" in gap
    [log] = await logs(row, "report_analysis_claims")
    assert log["mode"] == "strict" and log["message"].endswith("已记为缺口")


async def test_strict_stops_once_the_judgements_are_fixed(monkeypatch):
    seen = script(monkeypatch, SOURCE, PLAIN)
    row = await finish(weekly(analysis_claims="strict", max_repairs=3))
    _, _, output = await checked_of(row)
    assert len(seen) == 2 and "issuance_gaps" not in output


# --------------------------------------------------------------------------
# off
# --------------------------------------------------------------------------


async def test_off_checks_nothing_and_adds_no_rules(monkeypatch):
    seen = script(monkeypatch, SOURCE)
    row = await finish(weekly(analysis_claims="off"))
    checked, doc, output = await checked_of(row)
    assert len(seen) == 1 and ac.DOC_FIELD not in doc and said(checked["violations"]) == []
    assert ac.ANALYSIS_NONE_PROMPT not in human_texts(seen[0]) and ac.ANALYSIS_RULES_PROMPT not in human_texts(seen[0])
    assert output["analysis_claims"] == "off" and "analysis_claims" not in doc["stats"]


async def test_the_writer_role_asks_for_where_changes_come_from_and_evidence_for_causes(monkeypatch):
    seen = script(monkeypatch, PLAIN)
    await finish(weekly())
    system = str(seen[0][0].content)
    assert report_mod.ROLE in system
    assert "有分析结论时说清变化来自哪里" in report_mod.ROLE and "原因只按证据写" in report_mod.ROLE
    assert "说清变化和原因" not in report_mod.ROLE


# --------------------------------------------------------------------------
# 目录里有分析结论
# --------------------------------------------------------------------------


def a_entry(alias: str, result: dict) -> dict:
    artifact = "b" * 63 + alias[-1]
    return {"alias": alias, "kind": "analysis", "artifact": artifact, "eid": make_eid("analysis", artifact, {}),
            "locator": {}, "label": f"{alias} {result.get('phrase')}", "result": result}


@pytest.fixture
def analyses(monkeypatch):
    """写作目录里多几条分析结论：{A 编号: 结论}。台账里的分析结论由入口（分析卡、Agent 的分析工具）记，这里直接补进
    目录：报告节点和出口复核按同一个 report_catalog 建目录，一处补上。"""
    added: dict[str, dict] = {}
    original = report_mod.report_catalog

    def catalog(state, spec, node):
        out = original(state, spec, node)
        out.update({alias: a_entry(alias, r) for alias, r in added.items()})
        return out

    monkeypatch.setattr(report_mod, "report_catalog", catalog)
    return added


async def test_a_judgement_matching_its_conclusion_passes(monkeypatch, analyses):
    analyses["A1"] = fake_result()
    seen = script(monkeypatch, "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv,A1]]")
    row = await finish(weekly())
    checked, doc, _ = await checked_of(row)
    assert len(seen) == 1 and said(checked["violations"]) == []
    assert ac.ANALYSIS_RULES_PROMPT in human_texts(seen[0]) and ac.ANALYSIS_NONE_PROMPT not in human_texts(seen[0])


async def test_a_judgement_that_contradicts_its_conclusion_is_handed_back(monkeypatch, analyses):
    analyses["A1"] = fake_result(verdict="dispersed")
    seen = script(monkeypatch, "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv,A1]]")
    row = await finish(weekly())
    checked, _, _ = await checked_of(row)
    assert len(seen) == 2
    [miss] = [v for v in checked["violations"] if v["code"] in ac.CODES]
    assert miss["code"] == ac.MISMATCH and miss["analyses"] == ["A1"]
    assert "A1 判定是「销售额下降 194 万（-8.7%），分散在各地区，没有哪一段占到一半」" in str(seen[1][-1].content)


async def test_the_scope_is_written_only_when_there_are_conclusions(monkeypatch, analyses):
    script(monkeypatch, PLAIN)
    _, doc, _ = await checked_of(await finish(weekly()))
    assert "analysis_scope" not in doc, "没有分析结论不写分析范围"
    analyses["A1"] = fake_result()
    analyses["A2"] = fake_result("baseline", None, "outside_band")
    _, doc, _ = await checked_of(await finish(weekly()))
    scope = doc["analysis_scope"]
    assert [i["alias"] for i in scope["phenomena"]] == ["A2"] and [i["alias"] for i in scope["decomposed"]] == ["A1"]
    assert scope["decomposed"][0]["dims"] == ["地区"] and scope["not_decomposed"] is None and scope["note"]
    assert "analysis_scope" not in doc["markdown"] and scope["tests"] == 0
    _, doc, _ = await checked_of(await finish(weekly(analysis_scope="off")))
    assert "analysis_scope" not in doc


# --------------------------------------------------------------------------
# 取值写错
# --------------------------------------------------------------------------


def test_bad_values_are_refused_by_validation():
    from app.engine.schema import GraphSpec, validate_graph

    issues = validate_graph(GraphSpec.model_validate(weekly(analysis_claims="loose", analysis_scope="always"))).issues
    bad = {i.code: i for i in issues if i.code in ("report.analysis_claims_invalid", "report.analysis_scope_invalid")}
    assert bad["report.analysis_claims_invalid"].level == "error"
    assert bad["report.analysis_claims_invalid"].message == analysis_claims_problem("loose")
    assert "「分析说法核对」只能选「不核对」、「重写一次后标出」或「重写后仍有的计入缺口」" in analysis_claims_problem("loose")
    assert bad["report.analysis_scope_invalid"].message == analysis_scope_problem("always")
    assert analysis_claims_problem(None) is None and analysis_claims_problem("strict") is None


async def test_bad_values_fail_the_node_before_any_model_call(monkeypatch):
    seen = script(monkeypatch, PLAIN)
    original = runner_mod.validate_graph

    def permissive(spec):
        report = original(spec)
        report.issues = [i for i in report.issues if i.field not in ("analysis_claims", "analysis_scope")]
        report.ok = not any(i.level == "error" for i in report.issues)
        return report

    monkeypatch.setattr(runner_mod, "validate_graph", permissive)
    row = await finish(weekly(analysis_claims="loose"))
    assert row.status == "failed" and analysis_claims_problem("loose") in (row.error or "")
    assert seen == []
