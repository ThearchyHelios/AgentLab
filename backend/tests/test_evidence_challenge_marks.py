"""证据图上的反方审稿标记（期 4 整合 J3）：GET /api/runs/{id}/evidence 的 reports[] 带 challenge_verdicts（{句子: 判断}）。

正文（前端 EvidenceDoc）据此给被反方审过的句子挂句末标记、点开整句面板，不逐句请求片段接口。有的被审句子既没有可点的
片段、也没有裁判判定（没开裁判的报告里一句纯文字的结论），不靠这张表，正文里就打不开它的整句面板。

- 只含被反方审过的句子；读法和片段接口的 unit.challenge（_challenge_of）是同一个来源：审稿记录里记着审的哪份报告文档，
  和这一份对上才算；记录没存上时按画布上审的报告、和它跑完之前那份报告最后一次自查的文档认，判断取产出里列的那几句；
  节点「报错并停止运行」时按审完那一刻的日志认。几个反方审稿审了同一份文档时，每一句取最后跑完、审到了它的那个
- 每一句的判断和点开它时片段接口答的 unit.challenge.verdict 一字不差
- 没有反方审稿（或者它直通了、一句都没审）的报告没有这个键，老运行和以前一字不差

反方审稿真跑一遍图（虚构的 SQLite 库，模型是 MockChatModel 剧本）；认「哪一次」的边界情形用手写的封存事件。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete

from app.api import evidence
from app.core import artifact_store
from app.core.artifact_store import put_json
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Setting
from app.engine import spend, verify_agent
from app.engine.evidence import iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import challenge as challenge_mod
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()

    async def clear() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.key.in_(
                ["verify", "judge", spend.SPEND_KEY, JUDGE_SPEND_KEY])))
            await session.commit()

    await clear()
    yield
    await clear()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def start(spec: dict) -> Run:
    run_id = (await run_manager.start(graph=spec, input_payload={}, run_class="formal")).id
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 停在 {row.status}：{row.error}")


# --------------------------------------------------------------------------
# 真跑：虚构的区域月报
# --------------------------------------------------------------------------

PRICED = "claude-sonnet-5"
#: 各区销售额（含退款）：北区 1600、南区 700、西区 300
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
NET = "SELECT region AS 区域, SUM(amount) AS 净销售额 FROM sales WHERE refunded = 0 GROUP BY region ORDER BY 净销售额 DESC"
REFUNDS = "SELECT COUNT(*) AS 退款笔数 FROM sales WHERE refunded = 1"
#: 标题、一句有依据的结论、一句预埋的错误结论（南区其实低于北区）。后一句只有文字：没有可点的片段
TEXT = "## 区域月报\n\n北区销售额 [[v:Q1.r0.销售额]]，居各区之首。[[see:Q1]]南区销售额超过北区。[[see:Q1]]"
NORTH, SOUTH = "北区销售额 1,600，居各区之首。", "南区销售额超过北区。"
PLAYS = {
    "南区销售额超过北区": ([NET], "refuted", "扣除退款后南区为 [[v:Q1.r1.净销售额]]，仍低于北区的 [[v:Q1.r0.净销售额]]。[[see:Q1]]"),
    "居各区之首": ([REFUNDS], "weakened", "北区含 [[v:Q1.r0.退款笔数]] 笔退款，扣除后仍居首，但销售额被高估。"),
}


@pytest.fixture
async def sales(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount INTEGER, refunded INTEGER);"
        "INSERT INTO sales VALUES (1, '北区', 1250, 0), (2, '北区', 350, 1), (3, '南区', 700, 0), (4, '西区', 300, 0);")
    db.commit()
    db.close()
    name = f"months_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True, options={})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


def reviewer(monkeypatch, source: str) -> None:
    """写作者交 TEXT；反方 agent 按任务里的结论认出是哪一句，照 PLAYS 查询、交结论和理由。"""

    def decide(model, messages):
        if challenge_mod.CHALLENGE_SYSTEM not in str(messages[0].content):
            return AIMessage(content=TEXT)
        task = str(messages[1].content) if len(messages) > 1 else ""
        queries, verdict, rationale = next((p for k, p in PLAYS.items() if k in task), ([], "stands", "没有找到反例。"))
        if model.response_format:
            return AIMessage(content=json.dumps({"verdict": verdict, "rationale": rationale}, ensure_ascii=False))
        done = sum(1 for m in messages if isinstance(m, ToolMessage))
        if done < len(queries):
            call = {"name": f"db_query__{source}", "args": {"sql": queries[done]},
                    "id": f"q{done}-{uuid.uuid4().hex[:6]}"}
            return AIMessage(content="查一下。", tool_calls=[call])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(MockChatModel, "_decide", decide)

    async def verify_model(session, spec):
        return MockChatModel(model_name=PRICED, latency=0.0), PRICED

    monkeypatch.setattr(verify_agent, "get_chat_model", verify_model)


def challenge_graph(source: str, *, challenge: bool = True, **config) -> dict:
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写月报", instructions="写月报"),
        *([node("chal", "challenge", "审稿", report="write", **config)] if challenge else []),
        node("out", "output", fields=[{"name": "月报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write"}),
    )


async def graph_report(client, run_id: str) -> dict:
    res = await client.get(f"/api/runs/{run_id}/evidence")
    assert res.status_code == 200, res.text
    [report] = res.json()["reports"]
    return report


def units_of(doc: dict) -> dict[str, dict]:
    """句子编号 → (原文, 第一个不是结构性的片段)：点开整句时前端请求的就是这个片段。"""
    md = doc["markdown"]
    out = {}
    for _, unit in iter_units(doc):
        first = next((s for s in unit.get("segments") or [] if s.get("kind") != "structural"), None)
        start, end = unit["span"]
        out[unit["id"]] = {"text": md[start:end].strip(), "kind": unit.get("kind"), "first": first}
    return out


async def same_as_segments(client, run_id: str, report: dict) -> dict[str, str]:
    """每一句的标记和片段接口答的 unit.challenge.verdict 一字不差（没审到的两边都没有）。返回 原文 → 判断。"""
    doc = artifact_store.load(report["doc_artifact"])
    marks = report.get("challenge_verdicts", {})
    said = {}
    for uid, unit in units_of(doc).items():
        if unit["first"] is None:
            continue
        res = await client.get(f"/api/runs/{run_id}/evidence/segments/{unit['first']['id']}")
        assert res.status_code == 200, res.text
        verdict = (res.json()["unit"].get("challenge") or {}).get("verdict")
        assert marks.get(uid) == verdict, f"{unit['text']}：证据图 {marks.get(uid)!r} ≠ 片段接口 {verdict!r}"
        if verdict:
            said[unit["text"]] = verdict
    assert set(marks) <= set(units_of(doc)), "证据图标了文档里没有的句子"
    return said


async def test_the_graph_marks_every_challenged_sentence(client, monkeypatch, sales):
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales))
    assert run.status == "succeeded", run.error
    report = await graph_report(client, run.id)
    doc = artifact_store.load(report["doc_artifact"])
    units = units_of(doc)
    by_text = {u["text"]: uid for uid, u in units.items()}
    # 只含被审过的两句：标题、没被挑来审的句子不在里面
    assert report["challenge_verdicts"] == {by_text[NORTH]: "weakened", by_text[SOUTH]: "refuted"}
    heading = next(uid for uid, u in units.items() if u["kind"] == "heading")
    assert heading not in report["challenge_verdicts"]
    # 被推翻的那句只有文字：没有一个可点的片段，正文要靠这张表才挂得出入口
    south = next(u for b in doc["blocks"] for u in b["units"] if u["id"] == by_text[SOUTH])
    assert all(seg["kind"] in ("text", "structural") for seg in south["segments"])
    assert await same_as_segments(client, run.id, report) == {NORTH: "weakened", SOUTH: "refuted"}


async def test_a_lost_review_record_marks_the_listed_sentences(client, monkeypatch, sales):
    """审稿记录没存上：按产出里列的那几句标，和片段接口同一个来源。"""
    async def lost(self, record):
        return None

    monkeypatch.setattr(challenge_mod._Review, "_store", lost)
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales))
    assert run.status == "succeeded", run.error
    report = await graph_report(client, run.id)
    assert sorted(report["challenge_verdicts"].values()) == ["refuted", "weakened"]
    assert await same_as_segments(client, run.id, report) == {NORTH: "weakened", SOUTH: "refuted"}


async def test_a_review_that_stopped_the_run_still_marks(client, monkeypatch, sales):
    """「结论被推翻时」设为报错并停止运行：节点报错、运行失败，按审完那一刻的日志认审稿记录，被审的句子照样有标记。"""
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales, on_refuted="fail"))
    assert run.status == "failed" and "推翻" in (run.error or ""), run.error
    report = await graph_report(client, run.id)
    assert await same_as_segments(client, run.id, report) == {NORTH: "weakened", SOUTH: "refuted"}


@pytest.mark.parametrize("challenge", [False, True])
async def test_reports_nobody_reviewed_have_no_marks(client, monkeypatch, sales, challenge):
    """没有反方审稿、或者反方审稿关着（直通，一句都没审）：没有这个键，和以前一字不差。"""
    reviewer(monkeypatch, sales)
    run = await start(challenge_graph(sales, challenge=challenge, **({"enabled": False} if challenge else {})))
    assert run.status == "succeeded", run.error
    assert "challenge_verdicts" not in await graph_report(client, run.id)


# --------------------------------------------------------------------------
# 认「哪一次」：手写的封存事件
# --------------------------------------------------------------------------


def sealed_of(graph: dict, events: list[tuple]) -> evidence._Sealed:
    return evidence._Sealed(run_id="syn-run", graph=graph, events=events,
                            seal={"sealed": True, "ok": True, "manifest_seq": events[-1][0], "legacy": False})


def report(doc_artifact: str) -> evidence._Report:
    return evidence._Report(node_id="write", doc_artifact=doc_artifact, ok=True, repairs=0, stats=None, doc=None,
                            hash_ok=True, fields=[])


def claim(unit, verdict, text="一句结论。"):
    return {"unit": unit, "text": text, "verdict": verdict, "rationale": "", "rationale_doc": None, "queries": []}


async def review(doc: str, claims: list[dict], *, saved: bool = True, on_refuted: str = "degrade") -> str:
    """一次反方审稿的产出工件（审稿记录存上了的带 challenge_artifact）。"""
    record = await put_json({"report": "write", "doc_artifact": doc, "model": "mock", "claims": claims},
                            kind="challenge") if saved else None
    counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in ("stands", "weakened", "refuted", "inconclusive")}
    items = [{k: c[k] for k in ("unit", "text", "verdict")} for c in claims]
    return await put_json({"challenge_artifact": record, "counts": counts, "on_refuted": on_refuted, "items": items},
                          kind="node_output")


def consistent(sealed: evidence._Sealed, doc: str, units: list[str]) -> dict[str, str]:
    """标记表和逐句的 _challenge_of 一致：每一句的判断相同，没审到的两边都没有。"""
    marks = evidence._challenge_verdicts(sealed, report(doc))
    for uid in units:
        one = evidence._challenge_of(sealed, report(doc), uid)
        assert marks.get(uid) == (one or {}).get("verdict"), f"{uid}：{marks.get(uid)!r} ≠ {one!r}"
    return marks


@pytest.mark.parametrize("saved", [True, False])
async def test_a_review_of_an_earlier_draft_does_not_mark_the_rewritten_report(saved):
    """报告在反方审稿之后又重写了一次：审的是前一稿，新的一稿上同一个句子编号说的可能是另一句话，不标。"""
    draft = await put_json({"markdown": "第一稿", "n": 11}, kind="report_doc")
    final = await put_json({"markdown": "第二稿", "n": 12}, kind="report_doc")
    out = await review(draft, [claim("u1", "refuted"), claim("u2", "stands")], saved=saved)
    graph = chain(node("write", "report", "写月报"), node("chal", "challenge", "审稿", report="write"))
    events = [
        (1, "report.checked", "write", {"doc_artifact": draft, "ok": True}),
        (2, "node.finished", "write", {}),
        (3, "node.finished", "chal", {"artifact": out}),
        (4, "report.checked", "write", {"doc_artifact": final, "ok": True}),
    ]
    sealed = sealed_of(graph, events)
    assert consistent(sealed, draft, ["u1", "u2", "u3"]) == {"u1": "refuted", "u2": "stands"}
    assert consistent(sealed, final, ["u1", "u2"]) == {}


async def test_each_sentence_takes_the_last_review_that_reached_it():
    """两个反方审稿审了同一份文档：每一句取最后跑完、审到了它的那个（同片段接口）；只有前一个审到的句子照样标。"""
    doc = await put_json({"markdown": "月报", "n": 13}, kind="report_doc")
    first = await review(doc, [claim("u1", "refuted"), claim("u2", "stands")])
    second = await review(doc, [claim("u2", "weakened"), claim("u3", "inconclusive")])
    graph = chain(node("write", "report", "写月报"), node("chal1", "challenge", "审稿一", report="write"),
                  node("chal2", "challenge", "审稿二", report="write"))
    events = [
        (1, "report.checked", "write", {"doc_artifact": doc, "ok": True}),
        (2, "node.finished", "chal1", {"artifact": first}),
        (3, "node.finished", "chal2", {"artifact": second}),
    ]
    marks = consistent(sealed_of(graph, events), doc, ["u1", "u2", "u3", "u4"])
    assert marks == {"u1": "refuted", "u2": "weakened", "u3": "inconclusive"}


async def test_a_review_that_failed_the_node_marks_from_its_record():
    """节点报错：按审完那一刻的日志认审稿记录；重跑之后没再审完的不算，日志里没有审稿记录的认不出审的是哪一稿，也不算。"""
    doc = await put_json({"markdown": "月报", "n": 14}, kind="report_doc")
    record = await put_json({"report": "write", "doc_artifact": doc, "model": "mock",
                             "claims": [claim("u1", "refuted"), claim("u2", "weakened")]}, kind="challenge")
    graph = chain(node("write", "report", "写月报"), node("chal", "challenge", "审稿", report="write",
                                                       on_refuted="fail"))
    counts = {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 0}
    head = [(1, "report.checked", "write", {"doc_artifact": doc, "ok": True}), (2, "node.started", "chal", {})]
    done = (3, "log", "chal", {"code": "challenge_done", "counts": counts, "artifact": record})
    failed = (4, "node.failed", "chal", {"error": "反方审稿推翻了 1 句结论"})
    assert consistent(sealed_of(graph, [*head, done, failed]), doc, ["u1", "u2"]) == {"u1": "refuted", "u2": "weakened"}
    retried = [*head, done, (4, "node.started", "chal", {}), (5, "node.failed", "chal", {"error": "模型不可用"})]
    assert consistent(sealed_of(graph, retried), doc, ["u1", "u2"]) == {}
    lost = [*head, (3, "log", "chal", {"code": "challenge_done", "counts": counts, "artifact": None}), failed]
    assert consistent(sealed_of(graph, lost), doc, ["u1", "u2"]) == {}


async def test_unreadable_verdicts_are_left_out():
    """判断不是一个词的（缺了、空的、不是字符串）不标；同一次审稿里一句列了两遍时取前一条（同片段接口）。"""
    doc = await put_json({"markdown": "月报", "n": 15}, kind="report_doc")
    out = await review(doc, [claim("u1", None), claim("u2", ""), claim("u3", 3), claim("u4", "stands"),
                             claim("u4", "refuted"), claim("", "refuted"), claim(None, "refuted")])
    graph = chain(node("write", "report", "写月报"), node("chal", "challenge", "审稿", report="write"))
    events = [(1, "report.checked", "write", {"doc_artifact": doc, "ok": True}),
              (2, "node.finished", "chal", {"artifact": out})]
    assert consistent(sealed_of(graph, events), doc, ["u1", "u2", "u3", "u4", ""]) == {"u4": "stands"}
