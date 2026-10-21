"""分析能力第 1 波四个包贯穿起来的一条真运行（不用替身）：Agent 查两次库（两周的总数、按地区的两周），调
analysis_decompose（带 headline 核对闭合，W1-c 入口 + W1-a 算子）；写作模型引用判定语 [[a:A1]]、句末依据 [[see:A1]]
（W1-b 证据接入），另一句故意写「主要来自华南」（和结论「主要来自华东」不符，W1-d 说法核对）：

- check（缺省）：交回写作者重写一次，仍不符就标出（报告、出具声明、审计都看得到），不让节点失败、不改档位
- strict：按最多重写次数重写，仍不符的计入出具缺口、降档；报告节点报的和出口复核的只算一条
- 出口独立复核（verify_doc）核出来的和报告节点核出来的是同几处
- 证据面板里判定语的出处链：判定 → 方法 → 计算 → 输入 → 复算 → 封存，复算逐字段相同
- 文档带着系统写的分析范围：拆过按地区（判定主要来自华东），候选维度里没拆的渠道（按数据目录算），Agent 写的假设不进

虚构零售库 shop：orders(week, region, channel, amount)，华东、华南、华北、西南两周，华东掉得最多；数据目录标了 orders 是
事实表、地区和渠道是属性。模型换成脚本（MockChatModel._decide），不调用任何模型。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.analysis import contract
from app.core import artifact_store
from app.data import catalog as data_catalog
from app.data.catalog import make_item
from app.data.engine import engines
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine import analysis_claims as ac
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.sources import drop_source

TOOL = "db_query__shop"
DECOMPOSE = contract.TOOL_NAMES["decompose"]
TOTALS = "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week ORDER BY week"
BY_REGION = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week ORDER BY region, week"
HYPOTHESIS = "下降是不是集中在某个地区"
ARGS = {"mode": "additive", "query": "Q2", "by": ["region"], "period": "week", "base": "2026-W41", "target": "2026-W42",
        "measure": "gmv", "headline": {"base": "Q1.r0.gmv", "target": "Q1.r1.gmv"}, "label": "销售额",
        "hypothesis": HYPOTHESIS}
#: 各地区两周的销售额（每周按线上六成、线下四成拆成两笔）：总共 2700 → 2360，华东 -300，占总变化 88.2%
WEEKS = {"华东": (1000, 700), "华南": (800, 780), "华北": (600, 590), "西南": (300, 290)}
PHRASE = "销售额下降 340（-12.6%），主要来自华东（-300，占 88.2%）"
#: 第二句和结论不符：结论说主要来自华东
WRONG = "## 本周销售\n\n[[a:A1]]。[[see:A1]]\n\n其中下降主要来自华南。[[see:A1]]"
RIGHT = "## 本周销售\n\n[[a:A1]]。[[see:A1]]\n\n其中下降主要来自华东。[[see:A1]]"
#: 判定语的出处链（同一步连着几条的算一步：几份输入，输入后面接它们的查询步骤）
CHAIN = ["analysis", "analysis_method", "analysis_calc", "analysis_input", "analysis_recompute", "analysis_seal"]


def _column(label, measure, values=None):
    out = {"label": make_item(label, "human"), "measure": make_item(measure, "human")}
    if values:
        out["codes"] = make_item({v: "" for v in values}, "profile", "verified")
    return out


NOTES = {"orders": {"kind": make_item("fact", "human"), "columns": {
    "region": _column("地区", "attribute", WEEKS), "channel": _column("渠道", "attribute", ("线上", "线下")),
    "week": _column("周", "attribute"), "amount": _column("实收金额", "flow")}}}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, region TEXT, channel TEXT, amount INTEGER);")
    rows = [(region, week, channel, total * share // 10)
            for region, both in WEEKS.items() for week, total in zip(("2026-W41", "2026-W42"), both)
            for channel, share in (("线上", 6), ("线下", 4))]
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [(i, w, r, c, a) for i, (r, w, c, a) in enumerate(rows, 1)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        for table, notes in NOTES.items():
            current = await data_catalog.read_entry(session, row.id, table)
            await data_catalog.write_entry(session, row.id, table, notes, if_version=current.version if current else 0,
                                           actor=None)
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    # 删掉这个源（数据目录随外键级联删除）：同一个测试进程里别的用例也用名叫 shop 的源，留着这份目录会改掉它们的判定
    # （比如金额标成了流量，窗口长度不同就从有条件可比变成不可比）
    await drop_source(source_id)


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _graph(**report):
    """Agent（查库 + 变化拆解）→ 报告撰写（最多重写 2 次）→ 成果（引用模式的出具契约，报告来自 write）。"""
    nodes = [_node("start", "input"),
             _node("bot", "agent", prompt="销售额为什么下降", tools=[TOOL, DECOMPOSE], approval="never"),
             _node("write", "report", instructions="写周报", max_repairs=2, **report),
             _node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
                   contract={"report_from": "write"})]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def _script(monkeypatch, text: str) -> list[list]:
    """Agent 依次查总数、查按地区、拆解；写作模型每一稿都交 text。交回写作模型每次收到的消息。"""
    from app.providers import mock_model

    calls = [(TOOL, {"sql": TOTALS}), (TOOL, {"sql": BY_REGION}), (DECOMPOSE, ARGS)]
    writer: list[list] = []

    def decide(model, messages):
        if not model.tools:
            writer.append(list(messages))
            return AIMessage(content=text)
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(calls):
            name, args = calls[done]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
        return AIMessage(content="销售额下降，主要来自华东。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    return writer


async def _finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没有结束：{row.status}")


async def _events(run_id: str, etype: str, node_id: str | None = None) -> list[dict]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return [e.data for e in (await session.execute(q.order_by(RunEvent.seq))).scalars()]


async def _report(row: Run) -> tuple[dict, dict, dict]:
    """(report.checked 的载荷, 报告文档, 报告节点产出)。"""
    [checked] = await _events(row.id, "report.checked")
    [finished] = await _events(row.id, "node.finished", "write")
    return checked, artifact_store.load(checked["doc_artifact"]), artifact_store.load(finished["artifact"])


def _said(violations: list[dict]) -> list[tuple]:
    """说法违规里两边都该一样的几样：代码、判断词、在正文里的起止、所在片段、挂的结论。"""
    return [(v["code"], v.get("text"), v.get("span"), v.get("segment"), v.get("analyses"))
            for v in violations if v["code"] in ac.CODES]


def _analysis_gaps(issuance: dict) -> list[str]:
    return [g for g in issuance["gaps"] if "分析说法" in g]


async def test_check_rewrites_once_flags_the_wrong_source_and_keeps_the_tier(monkeypatch):
    writer = _script(monkeypatch, RIGHT)
    clean = await _finish(_graph())
    assert clean.status == "succeeded", clean.error
    assert len(writer) == 1 and "analysis_claims" not in clean.output["_issuance"]

    writer = _script(monkeypatch, WRONG)
    row = await _finish(_graph())
    assert row.status == "succeeded", row.error
    # 重写一次（不按最多重写次数反复），重写请求照结论说清哪里不对
    assert len(writer) == 2
    assert "A1 判的主要来源是华东，不是华南，不能写「主要来自华南」" in str(writer[1][-1].content)
    checked, doc, output = await _report(row)
    found = _said(checked["violations"])
    assert [(code, text) for code, text, *_ in found] == [(ac.MISMATCH, "主要来自")] and found[0][4] == ["A1"]
    assert checked["repairs"] == 1 and checked["failed"] is False and checked["analysis_claims"] == "check"
    assert doc[ac.DOC_FIELD] == {"mode": "check", "lexicon": ac.LEXICON_VERSION}
    assert "issuance_gaps" not in output and "analysis_gap" not in output

    # 出口独立复核：核出来的是同一处，只写进出具声明，档位和写对了的那次一样
    issuance = row.output["_issuance"]
    assert issuance["tier"] == clean.output["_issuance"]["tier"] == "formal"
    assert _analysis_gaps(issuance) == []
    declared = issuance["analysis_claims"]
    assert (declared["mode"], declared["counted"], declared["counts"]) == ("check", False, {ac.MISMATCH: 1})
    assert _said(declared["violations"]) == found

    # 判定语是系统渲染的片段；它的出处链六步，复算逐字段相同
    segs = {s["ref"]: s for s in iter_segments(doc) if s.get("ref")}
    assert (segs["a:A1"]["kind"], segs["a:A1"]["text"]) == ("analysis", PHRASE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{segs['a:A1']['id']}")).json()
        steps = [s["step"] for s in body["chain"]]
        assert [s for i, s in enumerate(steps) if s.startswith("analysis") and steps[i - 1:i] != [s]] == CHAIN
        judged = body["chain"][0]
        assert (judged["origin"], judged["call_id"], judged["verdict"]["subjects"]) == ("agent", "call_2", [["华东"]])
        assert judged["hypothesis"] == HYPOTHESIS, "Agent 写的假设只在证据面板显示"
        again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
        assert again["status"] == "same", again
        assert body["seal"]["covered"] is True, body["seal"]
        # 审计：标出的那一处单列一行
        audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
        [flagged] = [r for g in audit["groups"] for r in g["rows"] if r.get("kind") == "violation"]
        assert (flagged["issue"], flagged["text"], flagged["sentence"]) == (ac.MISMATCH, "主要来自", "其中下降主要来自华南。")

    # 分析范围：系统按写作目录和数据目录写的，不进正文，Agent 写的假设不进
    scope = doc["analysis_scope"]
    [a1] = scope["decomposed"]
    assert (a1["alias"], a1["dims"], a1["verdict"], a1["phrase"]) == ("A1", ["地区"], "driver", PHRASE)
    assert scope["not_decomposed"] == ["渠道"] and scope["note"] is None and scope["tests"] == 0
    assert HYPOTHESIS not in json.dumps(scope, ensure_ascii=False) and HYPOTHESIS not in doc["markdown"]


async def test_strict_counts_the_wrong_source_once_and_degrades(monkeypatch):
    writer = _script(monkeypatch, WRONG)
    row = await _finish(_graph(analysis_claims="strict"))
    assert row.status == "succeeded", "说法违规不让节点失败（不挡生成）"
    assert len(writer) == 3, "strict 按最多重写次数重写"
    checked, doc, output = await _report(row)
    found = _said(checked["violations"])
    assert [code for code, *_ in found] == [ac.MISMATCH] and doc[ac.DOC_FIELD]["mode"] == "strict"
    assert output["issuance_gaps"] == [output["analysis_gap"]]
    assert output["analysis_gap"].startswith("报告有 1 处分析说法未通过核对（1 处与所挂结论的判定对不上")

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    # 出口自己复核记了一条，报告节点报上来的同一件事不再重复收
    [gap] = _analysis_gaps(issuance)
    assert gap.startswith("报告「write」有 1 处分析说法未通过核对（1 处与所挂结论的判定对不上）")
    assert not any(output["analysis_gap"] in g for g in issuance["gaps"])
    declared = issuance["analysis_claims"]
    assert (declared["mode"], declared["counted"], declared["count"]) == ("strict", True, 1)
    assert _said(declared["violations"]) == found
    [event] = await _events(row.id, "issuance")
    assert event["analysis_claims"] == {"mode": "strict", "count": 1, "counted": True}
