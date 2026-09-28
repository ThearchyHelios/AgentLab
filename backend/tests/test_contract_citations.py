"""出具契约的引用模式：契约写了 report_from，就按报告文档逐段核对，而不是按数值回指。

旧的 narrative 模式把叙述里的每个数字按数值去指标集里找——同值的指标一多就找错，
而且只能说「对得上」，说不出「这个位置上的数来自哪里」。报告撰写节点已经把出处写进了
文档；出口要做的是独立复核一遍，不信报告节点自己的统计：

- 按 doc_artifact 取回文档（取回时复验哈希），用和报告节点同一套参数重建目录，重新
  解析、重新渲染每个带引用的片段，逐字比对
- 裸数字按未回指处理（strict 下不予出具），解析不了的引用同样处理
- 文档对不上（哈希、渲染、片段）记为 gap：校验本身没法完成，不能盖「完整出具」
- 成果字段逐字等于报告原文时标注 _evidence，不管有没有契约，界面据此能点开每个数
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


KPI = "{'gmv': 45678.5, 'gmv_prev': 42010.0, 'orders': 1234}"
STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain",
     "expression": "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
]
CLEAN = "## 本周概览\n\n本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]；订单 [[m:orders]]。[[see:m:gmv,m:wow]]"
CONTRACT = {"metrics_from": ["caliber"], "report_from": "write", "required": ["gmv", "orders"],
            "expected": ["wow"]}
REPORT_FIELD = {"name": "周报", "value": "{{ nodes.write.text }}"}


def weekly(*, contract=None, report=None, fields=None, card=None, between=()):
    out: dict = {"fields": fields or [REPORT_FIELD]}
    if contract is not None:
        out["contract"] = contract
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=STANDARD,
             **(card or {})),
        node("write", "report", instructions="写周报", **({"on_violation": "flag"} | (report or {}))),
        *between,
        node("out", "output", **out),
    ]
    chain = ["start", "fetch", "caliber", "write", *[n["id"] for n in between], "out"]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]}


def script(monkeypatch, *replies):
    from app.providers import mock_model

    seen: list[list] = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            # 状态先提交、封存随后才写：终态要等 manifest_seq 落下来再看
            if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
                return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    return await wait(run.id)


async def events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
            .order_by(RunEvent.seq))).scalars())


async def doc_id(run_id: str) -> str:
    return (await events(run_id, "report.checked"))[0].data["doc_artifact"]


# --------------------------------------------------------------------------
# 判档
# --------------------------------------------------------------------------


async def test_fully_cited_report_is_formal(monkeypatch):
    script(monkeypatch, CLEAN)
    row = await finish(weekly(contract={**CONTRACT, "strict": True}))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    doc = await doc_id(row.id)

    assert issuance["mode"] == "citations" and issuance["tier"] == "formal", issuance
    assert issuance["report"] == {"node_id": "write", "doc_artifact": doc}
    assert issuance["gaps"] == [] and issuance["unmatched_numbers"] == [] and issuance["unresolved"] == []
    # 旧字段原样保留，老前端照常能显示
    assert issuance["calibers"] == [{"node": "caliber", "caliber": "周报口径", "version": "v2"}]
    assert issuance["metrics_checked"] == 3 and issuance["missing_required"] == []
    assert issuance["missing_expected"] == [] and issuance["matched_numbers"] == 3
    assert issuance["declared_at"]

    text = row.output["周报"]
    matched = issuance["matched"]
    assert [(m["token"], m["metric"], m["caliber"]) for m in matched] == [
        ("45,678.5元", "gmv", "周报口径 @ v2"), ("8.7%", "wow", "周报口径 @ v2"), ("1,234单", "orders", "周报口径 @ v2")]
    for m in matched:
        assert text[m["start"]:m["end"]] == m["token"] and m["span"] == [m["start"], m["end"]]
        assert m["segment"].startswith("s") and m["unit"].startswith("u") and m["eid"].startswith("ev:metric:")

    # 成果字段就是报告原文：标注出来，界面据此把这个字段画成可点的证据文档
    assert row.output["_evidence"] == {"report_node": "write", "doc_artifact": doc, "fields": ["周报"]}
    event = (await events(row.id, "issuance"))[0].data
    assert event["tier"] == "formal" and event["mode"] == "citations" and event["unresolved"] == 0


@pytest.mark.parametrize("strict, tier", [(False, "degraded"), (True, "withheld")])
async def test_a_bare_number_degrades_or_withholds(monkeypatch, strict, tier):
    script(monkeypatch, "本周销售额 45678 元，订单 [[m:orders]]。")
    row = await finish(weekly(contract={**CONTRACT, "strict": strict}))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == tier and issuance["gaps"] == []
    [bare] = issuance["unmatched_numbers"]
    text = row.output["周报"]
    assert bare["token"] == "45678" and text[bare["start"]:bare["end"]] == "45678"
    assert bare["segment"] and bare["unit"] and "45678" in bare["context"]
    assert issuance["matched_numbers"] == 1


async def test_an_unresolved_reference_counts_like_an_uncited_number(monkeypatch):
    script(monkeypatch, "本周销售额 [[m:gmvx]]，订单 [[m:orders]]。")
    row = await finish(weekly(contract={**CONTRACT, "strict": True}))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "withheld" and issuance["unmatched_numbers"] == []
    [miss] = issuance["unresolved"]
    assert miss["ref"] == "m:gmvx" and "gmvx" in miss["message"] and miss["segment"]


async def test_a_tampered_document_is_a_gap_not_formal(monkeypatch):
    """报告写完、出口核对之前，文档文件被改了：取回时哈希对不上，这次校验没法完成。"""
    script(monkeypatch, CLEAN)
    graph = weekly(contract={**CONTRACT, "strict": True},
                   between=[node("gate", "human", mode="approve", title="确认")])
    run = await run_manager.start(graph=graph, input_payload={})
    await wait(run.id, ("interrupted", "failed", "succeeded"))
    doc = await doc_id(run.id)
    path = settings.data_dir / "artifacts" / doc[:2] / f"{doc}.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["markdown"] = body["markdown"].replace("45,678.5", "54,678.5")
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    await run_manager.resume(run.id, {"approved": True})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] != "formal"
    assert any("哈希" in g for g in issuance["gaps"]), issuance["gaps"]
    assert issuance["matched_numbers"] == 0


async def test_a_template_that_edits_the_report_is_a_gap(monkeypatch):
    script(monkeypatch, CLEAN)
    edited = {"name": "周报", "value": "{{ nodes.write.text }}\n\n（以上内容由系统生成）"}
    row = await finish(weekly(contract={**CONTRACT, "strict": True}, fields=[edited]))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("周报" in g and "改动了报告" in g for g in issuance["gaps"]), issuance["gaps"]
    assert "_evidence" not in row.output


async def test_fields_about_the_report_that_leave_its_text_alone_are_not_edits(monkeypatch):
    """改写次数、统计、文档 id 这类字段没碰报告正文：不算改动，完整出具照旧。"""
    script(monkeypatch, CLEAN)
    fields = [REPORT_FIELD,
              {"name": "改写次数", "value": "{{ nodes.write.repairs }}"},
              {"name": "统计", "value": "{{ nodes.write.stats | json }}"},
              {"name": "文档", "value": "{{ nodes['write']['doc_artifact'] }}"},
              {"name": "违规", "value": "{{ nodes.write.violations | length }}"}]
    row = await finish(weekly(contract={**CONTRACT, "strict": True}, fields=fields))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    assert row.output["改写次数"] == "0" and row.output["文档"] == await doc_id(row.id)
    assert row.output["_evidence"]["fields"] == ["周报"]


@pytest.mark.parametrize("template, report", [
    ("{{ nodes.write.text | lower }}", {}),
    ("{{ nodes['write']['text'] | lower }}", {}),
    ("{{ nodes.write | json }}", {}),
    ("摘录：{{ vars.draft }}", {"assign_to": "draft"}),
])
async def test_a_second_field_that_reshapes_the_report_text_is_a_gap(monkeypatch, template, report):
    """另一个字段把正文换了样子放进成果：读者看到的这一份没法逐段对应，照样记 gap。"""
    script(monkeypatch, CLEAN)
    row = await finish(weekly(contract={**CONTRACT, "strict": True}, report=report,
                              fields=[REPORT_FIELD, {"name": "副本", "value": template}]))
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert row.output["副本"].strip() != row.output["周报"]
    assert issuance["tier"] == "degraded"
    assert issuance["gaps"] == ["成果字段「副本」改动了报告，无法逐段对应"], issuance["gaps"]
    assert row.output["_evidence"]["fields"] == ["周报"]


async def test_a_report_nobody_sees_is_a_gap(monkeypatch):
    """契约核对的是报告，成果里却没有哪个字段是它：读者看到的内容没经过核对。"""
    script(monkeypatch, CLEAN)
    row = await finish(weekly(contract={**CONTRACT, "strict": True},
                              fields=[{"name": "周期", "value": "{{ input.week }}"}]))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("没有哪个字段" in g for g in issuance["gaps"]), issuance["gaps"]


async def test_rebuilds_the_catalog_with_the_report_nodes_own_parameters(monkeypatch):
    """两张卡里有同名指标，报告只取 east：m:gmv 在报告里就是 east.gmv。出口复核要是
    不按报告节点的 metrics_from 建目录，alias 就成了 m:east.gmv，一份好报告被判成引用不存在。"""
    script(monkeypatch, "东区销售额 [[m:gmv]]。")
    graph = weekly(contract={"metrics_from": ["east"], "report_from": "write", "required": ["gmv"],
                             "strict": True},
                   report={"metrics_from": ["east"]})
    graph["nodes"][2:3] = [
        node("east", "metrics", caliber="东区", metrics=[{"id": "gmv", "unit": "元", "expression": "vars.kpi.gmv"}]),
        node("west", "metrics", caliber="西区", metrics=[{"id": "gmv", "unit": "元", "expression": "vars.kpi.orders"}]),
    ]
    graph["edges"] = [{"source": a, "target": b} for a, b in [
        ("start", "fetch"), ("fetch", "east"), ("fetch", "west"), ("east", "write"), ("west", "write"),
        ("write", "out")]]
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal", issuance
    assert [(m["token"], m["metric"], m["caliber"]) for m in issuance["matched"]] == [("45,678.5元", "gmv", "东区 @ v1")]


async def test_missing_report_output_is_a_gap(monkeypatch):
    script(monkeypatch, CLEAN)
    row = await finish(weekly(contract={**CONTRACT, "strict": True},
                              report={"skip_if": "input.week == '2026-W37'"}))
    issuance = row.output["_issuance"]
    assert issuance["tier"] != "formal"
    assert any("write" in g and "报告文档" in g for g in issuance["gaps"]), issuance["gaps"]


async def test_a_required_metric_without_a_value_is_missing(monkeypatch):
    """on_missing=null 让缺输入的指标记为空值、交给契约判档：必需指标是空的，就是缺。"""
    script(monkeypatch, "订单 [[m:orders]]。")
    metrics = [*STANDARD, {"id": "margin", "name": "毛利", "expression": "vars.kpi.cost"}]
    graph = weekly(contract={**CONTRACT, "required": ["gmv", "margin"], "strict": True},
                   card={"on_missing": "null"})
    graph["nodes"][2]["data"]["config"]["metrics"] = metrics
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "withheld" and issuance["missing_required"] == ["margin"]


async def test_claim_policies_are_not_silently_skipped(monkeypatch):
    """结论句检查属于后续版本。契约声明了它，这一次就没核完，照实记一条 gap。"""
    script(monkeypatch, CLEAN)
    row = await finish(weekly(contract={**CONTRACT, "strict": True, "claims": {"policy": "judge"}}))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("结论句" in g for g in issuance["gaps"]), issuance["gaps"]


# --------------------------------------------------------------------------
# _evidence：不管有没有契约
# --------------------------------------------------------------------------


async def test_evidence_is_marked_without_a_contract(monkeypatch):
    script(monkeypatch, CLEAN)
    row = await finish(weekly(fields=[REPORT_FIELD, {"name": "周期", "value": "{{ input.week }}"}]))
    assert row.status == "succeeded", row.error
    assert "_issuance" not in row.output
    assert row.output["_evidence"] == {"report_node": "write", "doc_artifact": await doc_id(row.id),
                                       "fields": ["周报"]}


async def test_graphs_without_a_report_are_untouched(monkeypatch):
    graph = {"nodes": [node("start", "input", fields=[{"name": "q", "default": "x"}]),
                       node("out", "output", fields=[{"name": "r", "value": "{{ input.q }}"}])],
             "edges": [{"source": "start", "target": "out"}]}
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    assert row.output == {"r": "x"}


async def test_narrative_contracts_keep_their_old_shape(monkeypatch):
    """没写 report_from 的旧契约：_issuance 的键和以前一模一样，不多一个 mode。"""
    graph = weekly(contract={"metrics_from": ["caliber"], "narrative": "销售额 45678.5 元",
                             "required": ["gmv"]}, fields=[{"name": "r", "value": "{{ input.week }}"}])
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "write"]
    graph["edges"] = [{"source": a, "target": b} for a, b in [("start", "fetch"), ("fetch", "caliber"),
                                                             ("caliber", "out")]]
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert set(issuance) == {"tier", "calibers", "metrics_checked", "missing_required", "missing_expected",
                             "unmatched_numbers", "matched_numbers", "matched", "gaps", "declared_at"}
    assert issuance["tier"] == "formal"
