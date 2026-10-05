"""反方审稿节点（engine/nodes/challenge.py）：报告写完后挑几句关键结论，各派一个只读 agent 站在反方找反例。

- 选句照结论句裁判的预筛（judge.candidates / claim_priority）：挂了依据的、带比较或因果的排前面，取前「最多审几句」句
- 每句一个只读 agent（engine/verify_agent.py）：工具只给这句引用的查询所在的数据源，任务里给这句结论和它挂的证据摘录
  （judge.prepare，遮罩照旧）；理由里的数字只能引用它自己的查询格子，裸数字被标出来
- 结论四选一：经受住 / 被削弱 / 被推翻 / 无法判断；完整记录写进 challenge_artifact，随运行封存；台账只记 kind challenge，
  它的查询不进任何报告的写作目录
- 结论要有出处：零查询的判什么都记为无法判断，推翻还得引用到自己的查询结果，照录反方的理由时不露出内部标记、
  裸数字注明无出处；没有可查的源、模型用不了时不派 agent，只停用了一部分源的照常反查，理由和时间线写明哪几个没查
- 被推翻的：on_refuted 为 degrade（默认）时写 issuance_gaps，出具降档；fail 判节点失败；flag 只记录。产出和审批卡里
  的 on_refuted 写实际生效的那一种
- 上限：句数、每句查询数、每句时长、全局每日金额（都按发起时的设置快照）；全局关闭、报告没有可审的结论句时直通
- 整个反查放在一个 @task 里：接着跑时不重复执行、不重复记账；改了审稿本身的配置才重审；节点重试不重审
- 人工审批审这份报告时（含「报告 → 反方审稿 → 人工审批」的接法），审批卡附上反方审稿的概况
- 画布校验：要审的报告必须是上游的报告撰写节点；「报错并停止运行」不能配「出错时：记录错误并继续」；不在成果节点
  上游的「出具降档」给警告

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、电话都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Approval, DataSource, Run, RunEvent, Setting
from app.engine import spend, verify_agent
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.evidence import iter_units
from app.engine.judge import SPEND_KEY as JUDGE_SPEND_KEY
from app.engine.nodes import challenge as challenge_mod
from app.engine.nodes import report as report_mod
from app.engine.nodes.human import reviewed_by_human, reviewed_report_ids
from app.engine.runner import run_manager, verify_manifest
from app.engine.schema import GraphSpec, ValidationResult, validate_graph
from app.providers.factory import ProviderNotConfigured
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
#: 各区域销售额（含退款）：华东 1500、华南 900、华北 400
QUERY = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM sales GROUP BY region ORDER BY 销售额 DESC"
#: 一句有依据的结论、一句预埋的错误结论（华南其实低于华东）
TEXT = "华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额超过华东。[[see:Q1]]"
EAST, SOUTH = "华东销售额 1,500，居各区域之首。", "华南销售额超过华东。"
NET = "SELECT region AS 区域, SUM(amount) AS 净销售额 FROM sales WHERE refunded = 0 GROUP BY region ORDER BY 净销售额 DESC"
REFUNDS = "SELECT COUNT(*) AS 退款笔数 FROM sales WHERE refunded = 1"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(
            [spend.SPEND_KEY, JUDGE_SPEND_KEY, "verify", "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


async def _source(tmp_path, *, options=None) -> tuple[str, int]:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER, refunded INTEGER);"
        "INSERT INTO sales VALUES (1, '华东', '13900000001', 1200, 0), (2, '华东', '13900000002', 300, 1),"
        " (3, '华南', '13900000003', 900, 0), (4, '华北', '13900000004', 400, 0);")
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options=options or {})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        return name, row.id


@pytest.fixture
async def shop(tmp_path):
    """只读的手工源：四笔订单，华东两笔（其中一笔 300 已退款）、华南一笔 900、华北一笔 400。返回源名。"""
    name, source_id = await _source(tmp_path)
    yield name
    await engines.invalidate(source_id)


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes, extra_edges=()):
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    return {"nodes": list(nodes), "edges": [*edges, *extra_edges]}


def graph(source: str, *, sql: str = QUERY, contract: bool = True, **challenge) -> dict:
    """取数 → 报告 → 反方审稿 → 成果。成果按报告文档复核（引用模式的出具契约），数字都有出处：不加反方审稿是完整出具。"""
    challenge.setdefault("report", "write")
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿", **challenge),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             **({"contract": {"report_from": "write"}} if contract else {})),
    )


@dataclass
class Play:
    """反查 agent 对某一句的剧本：依次执行的查询，最后交的结论和理由。"""

    queries: list[str] = field(default_factory=list)
    verdict: str = "stands"
    rationale: str = "没有找到反例。"


@dataclass
class Fickle:
    """第一次交结果判「被推翻」，之后都判「经受住」：同一句重审一遍，模型就换了说法。"""

    queries: list[str]
    rationale: str
    asked: int = 0

    @property
    def verdict(self) -> str:
        self.asked += 1
        return "refuted" if self.asked == 1 else "stands"


#: 默认剧本：错误结论被推翻（扣除退款后华南仍低于华东），首句被削弱（华东含退款）
PLAYS = {
    "华南销售额超过华东": Play([NET], "refuted", "扣除退款后华南为 [[v:Q1.r1.净销售额]]，仍低于华东的 [[v:Q1.r0.净销售额]]。[[see:Q1]]"),
    "居各区域之首": Play([REFUNDS], "weakened", "华东含 [[v:Q1.r0.退款笔数]] 笔退款，扣除后仍居首，但销售额被高估。"),
}


class Models:
    """写作者和反查 agent 两份剧本。写作者依次回 writer 里的几段；反查 agent 按任务里的结论认出是哪一句，照 plays 查询、
    交结果。calls 记下反查 agent 的每次调用：(step / final, 认出的句子片段, 收到的消息)。"""

    def __init__(self, monkeypatch, source: str, *, writer=(TEXT,), plays=None, model_id=PRICED, latency=0.0,
                 broken: Exception | None = None):
        self.calls: list[tuple[str, str | None, list]] = []
        self.writes: list[list] = []
        #: 反查 agent 每句拿到的工具：{句子片段: 工具名集合}
        self.tools: dict[str | None, set[str]] = {}
        #: 核查每次建模型时要的是哪个模型（ModelSpec）：反查一句要建两次（查库、交结果）
        self.specs: list = []
        plays = PLAYS if plays is None else plays

        def which(messages) -> str | None:
            task = str(messages[1].content) if len(messages) > 1 else ""
            return next((k for k in plays if k in task), None)

        def decide(model, messages):
            system = str(messages[0].content)
            if challenge_mod.CHALLENGE_SYSTEM in system:
                key = which(messages)
                play = plays.get(key) or Play()
                if model.response_format:
                    self.calls.append(("final", key, list(messages)))
                    return AIMessage(content=json.dumps({"verdict": play.verdict, "rationale": play.rationale},
                                                        ensure_ascii=False))
                self.calls.append(("step", key, list(messages)))
                self.tools.setdefault(key, set()).update(t["name"] for t in model.tools)
                done = sum(1 for m in messages if isinstance(m, ToolMessage))
                if done < len(play.queries):
                    call = {"name": f"db_query__{source}", "args": {"sql": play.queries[done]},
                            "id": f"{(key or 'x')[:4]}-{done}"}
                    return AIMessage(content="查一下。", tool_calls=[call])
                return AIMessage(content="查完了。")
            if report_mod.ROLE in system:
                self.writes.append(list(messages))
                return AIMessage(content=writer[min(len(self.writes), len(writer)) - 1])
            raise AssertionError(f"没想到的模型调用：{system[:60]}")

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def verify_model(session, spec):
            self.specs.append(spec)
            if broken is not None:
                raise broken
            return MockChatModel(model_name=model_id, latency=latency), model_id

        monkeypatch.setattr(verify_agent, "get_chat_model", verify_model)

    def tasks(self) -> dict[str, str]:
        """反查 agent 收到的任务：{句子片段: 任务原文}。"""
        return {key: str(messages[1].content) for kind, key, messages in self.calls if kind == "step" and key}

    def reviewed(self) -> list[str | None]:
        return list(dict.fromkeys(key for _, key, _ in self.calls))


async def finish(spec: dict, *, run_class="formal", options=None, statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=spec, input_payload={}, run_class=run_class, options=options)
    return await wait(run.id, statuses)


async def wait(run_id, statuses=("succeeded", "failed")) -> Run:
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def logs(run_id: str, code: str) -> list[dict]:
    return [e.data for e in await events(run_id, "log") if e.data.get("code") == code]


async def output_of(run_id: str, node_id: str) -> dict:
    finished = await events(run_id, "node.finished", node_id)
    return artifact_store.load(finished[-1].data["artifact"])


async def report_doc(run_id: str) -> dict:
    [checked] = await events(run_id, "report.checked", "write")
    return artifact_store.load(checked.data["doc_artifact"])


def unit_of(doc: dict, text: str) -> str:
    md = doc["markdown"]
    return next(u["id"] for _, u in iter_units(doc) if md[u["span"][0]:u["span"][1]].strip() == text)


COUNT_KEYS = ("stands", "weakened", "refuted", "inconclusive")


# --------------------------------------------------------------------------
# 被推翻 → 出具降档
# --------------------------------------------------------------------------


async def test_a_refuted_conclusion_degrades_the_issuance(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"], "按优先级、再按文档先后逐句反查"

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert issuance["gaps"] == [f"反方审稿「审稿」：反方审稿推翻了 1 句结论：「{SOUTH}」"], "缺口不带节点名，前缀由出具契约加"

    doc = await report_doc(row.id)
    east, south = unit_of(doc, EAST), unit_of(doc, SOUTH)
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 0}
    assert set(out["counts"]) == set(COUNT_KEYS)
    assert out["items"] == [{"unit": east, "text": EAST, "verdict": "weakened"},
                            {"unit": south, "text": SOUTH, "verdict": "refuted"}]
    assert out["on_refuted"] == "degrade"
    assert out["issuance_gaps"] == [f"反方审稿推翻了 1 句结论：「{SOUTH}」"]

    record = artifact_store.load(out["challenge_artifact"])
    assert set(record) == {"report", "doc_artifact", "claims", "model"}
    assert record["report"] == "write" and record["model"] == PRICED
    [checked] = await events(row.id, "report.checked", "write")
    assert record["doc_artifact"] == checked.data["doc_artifact"]
    first, second = record["claims"]
    assert set(second) == {"unit", "text", "verdict", "rationale", "rationale_doc", "queries", "usage"}
    assert (second["unit"], second["text"], second["verdict"]) == (south, SOUTH, "refuted")
    assert second["rationale"] == "扣除退款后华南为 900，仍低于华东的 1,200。"
    assert second["rationale_doc"]["violations"] == []
    [query] = second["queries"]
    assert set(query) == {"alias", "artifact", "sql", "row_count", "truncated", "source"}
    assert query["alias"] == "Q1" and query["source"] == shop and "refunded = 0" in query["sql"]
    assert artifact_store.load(query["artifact"])["rows"] == [["华东", 1200], ["华南", 900], ["华北", 400]]
    assert query["row_count"] == 3 and query["truncated"] is False
    assert second["usage"]["calls"] >= 2 and second["usage"]["input_tokens"] > 0
    assert first["verdict"] == "weakened" and first["rationale"] == "华东含 1 笔退款，扣除后仍居首，但销售额被高估。"

    # 时间线：完成、推翻各一条，按约定带结构化字段
    [done] = await logs(row.id, "challenge_done")
    assert done["counts"] == out["counts"] and done["artifact"] == out["challenge_artifact"]
    [refuted] = await logs(row.id, "challenge_refuted")
    assert refuted["items"] == [{"unit": south, "text": SOUTH}]
    # 反查的查询发 tool.end（role: verify，带查询快照），可下钻、随运行封存
    ends = [e.data for e in await events(row.id, "tool.end", "chal")]
    assert len(ends) == 2 and all(d["role"] == "verify" and d.get("query_artifact") for d in ends)
    assert {d["query_artifact"] for d in ends} == {first["queries"][0]["artifact"], query["artifact"]}
    llm_ends = [e.data for e in await events(row.id, "llm.end", "chal")]
    assert llm_ends and all(d["role"] == "verify" for d in llm_ends)
    # 台账只记 kind challenge，绝不记 query：核查的查询不能被当成报告可以引用的证据
    [finished] = await events(row.id, "node.finished", "chal")
    assert finished.data["evidence"] == [{"kind": "challenge", "node_id": "chal", "exec": 1,
                                          "artifact": out["challenge_artifact"]}]
    # 节点用量记进运行用量
    assert row.usage["calls"] >= sum(c["usage"]["calls"] for c in record["claims"])
    verdict = await verify_manifest(row.id)
    assert verdict["ok"] is True
    assert finished.seq <= row.manifest_seq


async def test_flag_only_records_and_fail_stops_the_run(monkeypatch, shop):
    Models(monkeypatch, shop)
    row = await finish(graph(shop, on_refuted="flag"))
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal" and row.output["_issuance"]["gaps"] == []
    out = await output_of(row.id, "chal")
    assert out["on_refuted"] == "flag" and "issuance_gaps" not in out and out["counts"]["refuted"] == 1
    assert len(await logs(row.id, "challenge_refuted")) == 1, "flag 也照实记一条"

    Models(monkeypatch, shop)
    failed = await finish(graph(shop, on_refuted="fail"))
    assert failed.status == "failed"
    assert failed.error_node_id == "chal"
    assert "反方审稿推翻了 1 句结论" in failed.error and SOUTH in failed.error
    assert "请核对这句结论" in failed.error, "只推翻了一句，不说「这几句」"
    assert "「结论被推翻时」改为「只标注」" in failed.error, "报错里点的选项名和检查器下拉里的一致"
    # 判失败之前审稿记录已经存下、时间线照常有完成那一条
    [done] = await logs(failed.id, "challenge_done")
    record = artifact_store.load(done["artifact"])
    assert [c["verdict"] for c in record["claims"]] == ["weakened", "refuted"]


async def test_nothing_refuted_means_no_gap(monkeypatch, shop):
    plays = {"华南销售额超过华东": Play([NET], "inconclusive", "查不到判断所需的数据。"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal"
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 1, "weakened": 0, "refuted": 0, "inconclusive": 1}
    assert "issuance_gaps" not in out
    assert await logs(row.id, "challenge_refuted") == []


async def test_the_verify_queries_never_reach_a_writer_downstream(monkeypatch, shop):
    """反方审稿之后再写一份报告：它的证据目录里只有原来的查询（Q1），没有反查 agent 的查询。"""
    Models(monkeypatch, shop, writer=(TEXT, "华东销售额 [[v:Q1.r0.销售额]]。[[see:Q1]]"))
    spec = chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿", report="write"),
        node("again", "report", "复写", instructions="再写一遍"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.again.text }}"}]),
    )
    row = await finish(spec, run_class="exploratory")
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "again")
    doc = artifact_store.load(checked.data["doc_artifact"])
    queries = [a for a, e in doc["catalog"].items() if e.get("kind") == "query"]
    assert queries == ["Q1"] and doc["catalog"]["Q1"]["node_id"] == "fetch"


# --------------------------------------------------------------------------
# 没有出处的结论不采信
# --------------------------------------------------------------------------


async def test_a_verdict_without_any_query_is_not_taken(monkeypatch, shop):
    """一条查询都没执行成功就判「被推翻」「经受住」：没有出处，记为无法判断——不让正式出具降档，也不给审批人「经受住」
    的假安心。反方原本给的结论和理由照录在理由里，审稿记录看得出发生了什么。"""
    plays = {"华南销售额超过华东": Play([], "refuted", "我认为这句不成立。"),
             "居各区域之首": Play([], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal", "零查询的推翻没有出处，不能让正式出具降档"
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 0, "refuted": 0, "inconclusive": 2}
    assert "issuance_gaps" not in out and await logs(row.id, "challenge_refuted") == []
    east, south = artifact_store.load(out["challenge_artifact"])["claims"]
    assert south["verdict"] == "inconclusive" and south["queries"] == [] and south["rationale_doc"] is None
    assert "没有执行成功任何查询" in south["rationale"]
    assert "「被推翻」" in south["rationale"] and "我认为这句不成立。" in south["rationale"]
    assert east["verdict"] == "inconclusive" and "「经受住」" in east["rationale"]
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == 2 and "没有执行成功任何查询" in warn["reason"]


async def test_a_refutation_that_cites_none_of_its_queries_is_not_taken(monkeypatch, shop):
    """查过了，可理由里一处查询结果都没引用就判「被推翻」：推翻没有出处，记为无法判断、不降档；查过的照录。"""
    plays = {"华南销售额超过华东": Play([NET], "refuted", "扣除退款后华南仍低于华东。"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal"
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 1, "weakened": 0, "refuted": 0, "inconclusive": 1}
    east, south = artifact_store.load(out["challenge_artifact"])["claims"]
    assert south["verdict"] == "inconclusive" and len(south["queries"]) == 1 and south["rationale_doc"] is None
    assert "没有引用" in south["rationale"] and "扣除退款后华南仍低于华东。" in south["rationale"]
    assert east["verdict"] == "stands", "「经受住」不要求引用：它不改变出具"
    assert await logs(row.id, "challenge_unreviewed") == [], "查过了，不算没能反查"


@pytest.mark.parametrize("queries, said, quoted", [
    ([NET], "扣除退款后华南为 [[v:Q1.r99.净销售额]]。", "扣除退款后华南为（引用无效）。"),
    ([NET], "扣除退款后华南为 [[v:Q2.r1.净销售额]] 元。", "扣除退款后华南为（引用无效）元。"),
    ([], "华南为 [[v:Q1.r1.净销售额]]，不成立。", "华南为（引用无效），不成立。"),
    ([NET], "扣除退款后华南为 900，低于华东 1200。", "扣除退款后华南为 900（无出处），低于华东 1200（无出处）。"),
])
async def test_an_unfounded_rationale_is_quoted_in_plain_words(monkeypatch, shop, queries, said, quoted):
    """不采信时照录反方的理由，界面按纯文字显示（没有理由文档）：解析不了的引用不能把内部的占位标记露出来，换成
    「（引用无效）」；裸数字逐个注明「（无出处）」——文档里它们会被标出来，照录成文字时也得标。"""
    plays = {"华南销售额超过华东": Play(queries, "refuted", said),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop), run_class="exploratory")
    assert row.status == "succeeded", row.error
    south = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])["claims"][1]
    assert south["verdict"] == "inconclusive" and south["rationale_doc"] is None
    assert south["rationale"].endswith(f"「被推翻」不予采信，记为无法判断。反方的理由：{quoted}"), south["rationale"]
    assert "⟦" not in south["rationale"] and "[[" not in south["rationale"]


async def test_claims_whose_sources_are_gone_are_not_sent_out(monkeypatch, shop):
    """报告写完、审稿开始之前数据源被停用：这句没有可查的源，不建模型、不花钱，照实记无法判断并点出是哪个源。"""
    real = report_mod._store

    async def store_then_disable(doc, ctx):
        out = await real(doc, ctx)
        async with SessionLocal() as session:
            row = (await session.execute(select(DataSource).where(DataSource.name == shop))).scalar_one()
            row.enabled = False
            await session.commit()
        return out

    monkeypatch.setattr(report_mod, "_store", store_then_disable)
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.calls == [] and models.specs == [], "没有可查的源就不建模型、不调用"
    out = await output_of(row.id, "chal")
    assert out["counts"]["inconclusive"] == 2 and row.output["_issuance"]["tier"] == "formal"
    claims = artifact_store.load(out["challenge_artifact"])["claims"]
    assert all(f"「{shop}」" in c["rationale"] and "不存在或已停用" in c["rationale"] for c in claims)
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == 2 and f"「{shop}」" in warn["reason"]


async def test_a_claim_with_part_of_its_sources_gone_says_so(monkeypatch, tmp_path, shop):
    """两个数据源的报告，审稿开始前停用了其中一个：引用两个源的那句、没挂依据的那句都只能在剩下的源里查。判断照常
    采信，但理由末尾由系统写明哪个源没查（理由文档里也有：界面按文档画），时间线上另记一条——不然「经受住」看着像
    两个源都查过了。"""
    other, other_id = await _source(tmp_path)
    try:
        text = ("华东销售额 [[v:Q1.r0.销售额]]，退款只有 [[v:Q2.r0.退款笔数]] 笔。[[see:Q1]][[see:Q2]]"
                "增长主要来自华东。")
        plays = {"退款只有": Play([NET], "stands", "没有找到反例。"),
                 "增长主要来自": Play([NET], "weakened", "华东净销售额为 [[v:Q1.r0.净销售额]]，增长说过了头。")}
        real = report_mod._store

        async def store_then_disable(doc, ctx):
            out = await real(doc, ctx)
            async with SessionLocal() as session:
                row = await session.get(DataSource, other_id)
                row.enabled = False
                await session.commit()
            return out

        monkeypatch.setattr(report_mod, "_store", store_then_disable)
        models = Models(monkeypatch, shop, writer=(text,), plays=plays)
        spec = chain(
            node("start", "input"),
            node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
            node("fetch2", "tool", tool=f"db_query__{other}", args={"sql": REFUNDS}),
            node("write", "report", "写周报", instructions="写周报"),
            node("chal", "challenge", "审稿"),
            node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        )
        row = await finish(spec, run_class="exploratory")
        assert row.status == "succeeded", row.error
        assert models.reviewed() == ["退款只有", "增长主要来自"]
        assert models.tools["退款只有"] == {f"db_schema__{shop}", f"db_query__{shop}"}, "停用的源不给工具"
        task = models.tasks()["退款只有"]
        assert f"「{other}」" in task and "已不存在或已停用" in task, "给 agent 的任务里也说了，免得它把查不到当成没有反例"
        assert "这份报告还用到了" in models.tasks()["增长主要来自"], "没挂依据的那句不说「这句引用了」"

        record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
        both, grow = record["claims"]
        assert (both["verdict"], grow["verdict"]) == ("stands", "weakened"), "查过、有出处的判断照常采信"
        assert both["rationale"] == (f"没有找到反例。\n\n注：这句引用的数据源「{other}」已不存在或已停用，未能查询，"
                                     "以上判断不含其中的数据。")
        assert grow["rationale"] == (f"华东净销售额为 1,200，增长说过了头。\n\n注：这份报告用到的数据源「{other}」"
                                     "已不存在或已停用，未能查询，以上判断不含其中的数据。")
        for claim in (both, grow):
            doc = claim["rationale_doc"]
            assert doc["markdown"] == claim["rationale"], "界面按文档画：说明要在文档里"
            segments = [s for _, u in iter_units(doc) for s in u["segments"]]
            assert "".join(s["text"] for s in segments) == doc["markdown"]
            assert [s["id"] for s in segments] == [f"s{i}" for i in range(len(segments))]
            *_, note = iter_units(doc)
            assert note[1]["kind"] == "connective" and note[1]["cites"] == [], "说明不是反方的结论，也不挂出处"
        cited = [s for _, u in iter_units(grow["rationale_doc"]) for s in u["segments"] if s.get("cite")]
        assert [s["text"] for s in cited] == ["1,200"], "反方自己的引用照旧解析"

        [lost] = await logs(row.id, "challenge_sources_lost")
        assert lost["level"] == "warn" and lost["claims"] == 2 and lost["sources"] == [other]
        assert f"「{other}」" in lost["message"] and "2 句" in lost["message"]
        assert await logs(row.id, "challenge_unreviewed") == [], "两句都查过了，不算没能反查"
    finally:
        await engines.invalidate(other_id)


def test_the_note_reads_like_one_more_paragraph_of_the_rationale():
    """理由末尾的系统说明照 compose_doc 的样子接上：和把说明写进原文再组装出来的块、句、片段、统计一模一样（片段的
    偏移连着、编号接着往下数），只是反方的原文（source）不动、传进来的那份文档不改。"""
    from app.engine.evidence import compose_doc

    said = "华南只有 900，低于华东。\n\n- 华北为 [[v:Q1.r2.销售额]]"
    note = "注：这句引用的数据源「仓库」已不存在或已停用，未能查询，以上判断不含其中的数据。"
    doc = compose_doc(said, {}, node_id="chal")
    before = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    noted = challenge_mod.with_note(doc, note)
    again = compose_doc(f"{said}\n\n{note}", {}, node_id="chal")
    assert noted["markdown"] == again["markdown"] and noted["blocks"] == again["blocks"]
    assert noted["stats"] == again["stats"]
    assert noted["source"] == doc["source"] == said
    assert json.dumps(doc, ensure_ascii=False, sort_keys=True) == before


# --------------------------------------------------------------------------
# 理由里的裸数字
# --------------------------------------------------------------------------


async def test_bare_numbers_in_the_rationale_are_flagged(monkeypatch, shop):
    plays = {"华南销售额超过华东": Play([NET], "refuted", "华南只有 900，低于华东的 [[v:Q1.r0.净销售额]]。"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    south = record["claims"][1]
    assert south["rationale"] == "华南只有 900，低于华东的 1,200。"
    bare = [v for v in south["rationale_doc"]["violations"] if v["code"] == "uncited_number"]
    assert [v["text"] for v in bare] == ["900"], "自己写的数字不采信，标成没有出处"
    assert south["rationale_doc"]["stats"]["uncited_numbers"] == 1


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


async def test_the_claim_limit_reviews_the_first_claims_only(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop, max_claims=1))
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首"]
    out = await output_of(row.id, "chal")
    assert [i["text"] for i in out["items"]] == [EAST] and sum(out["counts"].values()) == 1
    assert row.output["_issuance"]["tier"] == "formal", "没审到那句错误结论：不能凭空降档"
    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "max_claims" and limit["claims_skipped"] == 1
    assert "1 句" in limit["message"]


async def test_the_settings_snapshot_sets_the_limits_and_the_node_overrides_it(monkeypatch, shop):
    """上限按发起时快照的核查设置（run.options["verify"]）；节点上写了的盖过它。"""
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop), options={"verify": {"challenge": {"max_claims": 1}}})
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首"]

    models = Models(monkeypatch, shop)
    row = await finish(graph(shop, max_claims=2), options={"verify": {"challenge": {"max_claims": 1}}})
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"]
    assert await logs(row.id, "challenge_limit") == []


async def test_the_snapshot_decides_the_model(monkeypatch, shop):
    """反方模型也按发起时快照的核查设置：设置里现在写的是另一个模型，照样用快照里的。"""
    async with SessionLocal() as session:
        session.add(Setting(key="verify", value={"model": "stored-model"}))
        await session.commit()
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop), run_class="exploratory", options={"verify": {"model": "snap-model"}})
    assert row.status == "succeeded", row.error
    assert models.specs and {s.model for s in models.specs} == {"snap-model"}


async def test_the_snapshot_decides_the_daily_cap(monkeypatch, shop):
    """每日金额上限也按快照：设置现在放宽到 $5，发起时的 $0.50 当天已经用完，照样一句不反查。"""
    async with SessionLocal() as session:
        session.add(Setting(key="verify", value={"daily_max_usd": 5.0}))
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 1.0, "calls": 3}))
        await session.commit()
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop), options={"verify": {"daily_max_usd": 0.5}})
    assert row.status == "succeeded", row.error
    assert [kind for kind, _, _ in models.calls] == []
    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "daily_max_usd" and "$0.50" in limit["message"]


async def test_without_a_snapshot_the_current_settings_apply(monkeypatch, shop):
    async with SessionLocal() as session:
        session.add(Setting(key="verify", value={"challenge": {"max_claims": 1}}))
        await session.commit()
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首"]


async def test_the_query_limit_applies_to_each_claim(monkeypatch, shop):
    plays = {"华南销售额超过华东": Play([NET, REFUNDS], "refuted", "华南为 [[v:Q1.r1.净销售额]]。"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop, max_queries=1), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    south = record["claims"][1]
    assert [q["alias"] for q in south["queries"]] == ["Q1"], "第二条查询超过每句的查询次数上限，没有执行"
    assert south["verdict"] == "refuted" and south["rationale"] == "华南为 900。", "触顶照样按查到的结果收尾"
    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "max_queries" and limit["claims_skipped"] == 0


async def test_the_time_limit_applies_to_each_claim_and_still_settles(monkeypatch, shop):
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 2.0)
    endless = [f"SELECT COUNT(*) AS n FROM sales WHERE id > {i}" for i in range(40)]
    plays = {"华南销售额超过华东": Play(endless, "inconclusive", "时间到了，没能查完。"),
             "居各区域之首": Play([], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays, latency=0.1)
    row = await finish(graph(shop, timeout_s=0.4), run_class="exploratory")
    assert row.status == "succeeded", row.error
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    south = record["claims"][1]
    assert south["verdict"] == "inconclusive" and 1 <= len(south["queries"]) < 20
    limits = await logs(row.id, "challenge_limit")
    assert [(lm["limit"], lm["claims_skipped"]) for lm in limits] == [("timeout_s", 0)]


async def test_the_daily_cap_stops_the_remaining_claims(monkeypatch, shop):
    """当天的核查金额已经用完：一句都不反查，照实记成无法判断，不降档。"""
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 1.9999, "calls": 3}))
        await session.commit()
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop), options={"verify": {"daily_max_usd": 2.0}})
    assert row.status == "succeeded", row.error
    assert [kind for kind, _, _ in models.calls] == [], "没钱就不调用模型"
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 0, "refuted": 0, "inconclusive": 2}
    record = artifact_store.load(out["challenge_artifact"])
    words = "已达上限（全局每日核查金额上限 $2.00），这句未反查"
    assert [c["rationale"] for c in record["claims"]] == [words, words], \
        "第一句派出去就撞上上限、一次模型都没调用，和后面不再派出的句子是同一回事，同一种说法"
    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "daily_max_usd" and limit["claims_skipped"] == 2
    assert len(await logs(row.id, "verify_limit")) == 1, "第一句撞上每日上限之后，后面的句子不再派 agent 去试"
    assert row.output["_issuance"]["tier"] == "formal"


# --------------------------------------------------------------------------
# 审批卡
# --------------------------------------------------------------------------


def gated(source: str, *, message: str = "{{ nodes.write.text }}", **challenge) -> dict:
    """取数 → 报告 → 反方审稿 → 人工审批 → 成果。message 是审批说明，缺省引用报告正文。"""
    challenge.setdefault("report", "write")
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿", **challenge),
        node("gate", "human", "主管审批", mode="approve", title="审核周报", message=message),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    spec = chain(*nodes[:-1])
    spec["nodes"].append(nodes[-1])
    spec["edges"].append({"source": "gate", "target": "out", "sourceHandle": "approved"})
    return spec


async def paused(spec: dict, *, options=None) -> tuple[Run, Approval]:
    row = await finish(spec, run_class="exploratory", options=options, statuses=("interrupted", "failed", "succeeded"))
    assert row.status == "interrupted", row.error
    async with SessionLocal() as session:
        approval = (await session.execute(select(Approval).where(Approval.run_id == row.id))).scalar_one()
    return row, approval


async def test_the_approval_card_carries_the_challenge_summary(monkeypatch, shop):
    Models(monkeypatch, shop)
    row, approval = await paused(gated(shop))
    [report] = approval.payload["reports"]
    out = await output_of(row.id, "chal")
    assert report["node_id"] == "write"
    assert report["challenge"] == {"counts": out["counts"], "items": out["items"],
                                   "artifact": out["challenge_artifact"], "on_refuted": "degrade"}
    assert report["challenge"]["counts"]["refuted"] == 1
    # human.requested 事件里也带着：回放时界面一样读得到
    [requested] = await events(row.id, "human.requested", "gate")
    assert requested.data["reports"] == approval.payload["reports"]


async def test_the_approval_card_shows_what_flag_means(monkeypatch, shop):
    Models(monkeypatch, shop)
    _, approval = await paused(gated(shop, on_refuted="flag"))
    assert approval.payload["reports"][0]["challenge"]["on_refuted"] == "flag"


async def test_a_skipped_challenge_adds_nothing_to_the_card(monkeypatch, shop):
    Models(monkeypatch, shop)
    _, approval = await paused(gated(shop, enabled=False))
    [report] = approval.payload["reports"]
    assert "challenge" not in report, "没审就不加这个键：老行为逐字不变"


async def test_report_then_challenge_then_human_shows_the_report_on_the_card(monkeypatch, shop):
    """按推荐的接法「报告 → 反方审稿 → 人工审批」、审批说明里不引用正文：审批卡上照样有这份报告和它的审稿概况，
    审批人批准时知道有一句被推翻了。报告节点也认它要交给人审（两边是同一个认法，不会对不上）。"""
    Models(monkeypatch, shop)
    spec = gated(shop, message="请审批本周周报")
    row, approval = await paused(spec)
    [report] = approval.payload["reports"]
    out = await output_of(row.id, "chal")
    assert report["node_id"] == "write" and report["challenge"]["counts"] == out["counts"]
    assert report["challenge"]["counts"]["refuted"] == 1
    parsed = GraphSpec.model_validate(spec)
    assert reviewed_report_ids(parsed, parsed.node_map()["gate"]) == ["write"]
    assert reviewed_by_human(parsed, "write")


async def test_fail_with_nothing_refuted_reports_what_took_effect(monkeypatch, shop):
    """「报错并停止运行」又没有句子被推翻：走到了审批，节点产出和审批卡里的 on_refuted 写实际生效的处置（只标注），
    不写约定之外的 fail。"""
    plays = {"华南销售额超过华东": Play([NET], "stands", "没有找到反例。"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    Models(monkeypatch, shop, plays=plays)
    row, approval = await paused(gated(shop, on_refuted="fail"))
    assert approval.payload["reports"][0]["challenge"]["on_refuted"] == "flag"
    assert (await output_of(row.id, "chal"))["on_refuted"] == "flag"


def test_the_card_only_carries_the_review_of_that_report():
    """一个审批节点审两份报告，反方审稿只审了其中一份：审稿概况只挂在被审的那份上。"""
    spec = spec_of(node("start", "input"), node("a", "report", "周报甲"), node("b", "report", "周报乙"),
                   node("chal", "challenge", report="a"), node("gate", "human"),
                   edges=[{"source": "start", "target": "a"}, {"source": "start", "target": "b"},
                          {"source": "a", "target": "chal"}, {"source": "chal", "target": "gate"},
                          {"source": "b", "target": "gate"}])
    counts = {"stands": 1, "weakened": 0, "refuted": 0, "inconclusive": 0}
    items = [{"unit": "u0", "text": "周报甲的一句结论。", "verdict": "stands"}]
    outputs = {"chal": {"challenge_artifact": "a1", "counts": counts, "items": items, "on_refuted": "degrade"}}
    assert challenge_mod.challenge_summary(spec, outputs, "a") == {
        "counts": counts, "items": items, "artifact": "a1", "on_refuted": "degrade"}
    assert challenge_mod.challenge_summary(spec, outputs, "b") is None
    # 直接连着的报告，和直接连着的反方审稿审的那份，都算这个审批节点审的
    assert reviewed_report_ids(spec, spec.node_map()["gate"]) == ["a", "b"]


def test_items_keep_the_refuted_ones_when_there_are_many():
    claims = [{"unit": f"u{i}", "text": f"第 {i} 句", "verdict": "stands"} for i in range(14)]
    claims[12]["verdict"] = "refuted"
    claims[13]["verdict"] = "weakened"
    items = challenge_mod.pick_items(claims)
    assert len(items) == 10
    assert [i["unit"] for i in items][-2:] == ["u12", "u13"], "最多 10 条：被推翻、被削弱的不能被挤掉，顺序照原来的"
    assert [i["unit"] for i in items][:8] == [f"u{i}" for i in range(8)]


# --------------------------------------------------------------------------
# 重放
# --------------------------------------------------------------------------


async def test_continuing_after_a_failure_does_not_run_the_agents_again(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    real = challenge_mod._finish
    broken = {"on": True}

    def flaky(*args, **kwargs):
        if broken["on"]:
            raise RuntimeError("落盘时断了")
        return real(*args, **kwargs)

    monkeypatch.setattr(challenge_mod, "_finish", flaky)
    run = await run_manager.start(graph=graph(shop), input_payload={}, run_class="formal")
    row = await wait(run.id)
    assert row.status == "failed" and "落盘时断了" in (row.error or ""), row.error
    calls = len(models.calls)
    assert calls > 0
    before = await spend.daily_spend()
    verify_ends = len(await events(run.id, "llm.end", "chal"))
    tool_ends = len(await events(run.id, "tool.end", "chal"))

    broken["on"] = False
    await run_manager.continue_failed(run.id)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(models.calls) == calls, "接着跑时反查 agent 又执行了一遍：反查没进 checkpoint"
    assert await spend.daily_spend() == before, "接着跑时核查的每日账又记了一次"
    assert len(await events(run.id, "llm.end", "chal")) == verify_ends
    assert len(await events(run.id, "tool.end", "chal")) == tool_ends
    assert len(await logs(row.id, "challenge_done")) == 1
    assert row.output["_issuance"]["tier"] == "degraded"


async def test_retrying_after_a_refutation_does_not_review_again(monkeypatch, shop):
    """「报错并停止运行」配了失败重试：重试沿用同一次审稿，不再派 agent——再审一遍既多花一份钱，模型换个说法还会把
    「被推翻」重审掉，最严的处置反倒放行。"""
    plays = {"华南销售额超过华东": Fickle([NET], "扣除退款后华南为 [[v:Q1.r1.净销售额]]。[[see:Q1]]"),
             "居各区域之首": Play([REFUNDS], "stands", "没有找到反例。")}
    models = Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop, on_refuted="fail", retries=1, retry_backoff=0.01))
    assert row.status == "failed" and "反方审稿推翻了 1 句结论" in (row.error or ""), row.error
    assert [key for kind, key, _ in models.calls if kind == "final"] == ["居各区域之首", "华南销售额超过华东"]
    assert len(await logs(row.id, "challenge_done")) == 1
    assert len(await logs(row.id, "node_retry")) == 1, "重试照常发生，只是不再审一遍"


async def test_a_fail_that_would_be_swallowed_degrades_instead(monkeypatch, shop):
    """「报错并停止运行」配「出错时：记录错误并继续」（画布校验拦这个组合，这里模拟跳过了校验的旧图）：报的错会被吞掉，
    被推翻的结论照常完整出具。节点改按出具降档处置。"""
    monkeypatch.setattr("app.engine.runner.validate_graph", lambda spec: ValidationResult())
    Models(monkeypatch, shop)
    row = await finish(graph(shop, on_refuted="fail", on_error="continue"))
    assert row.status == "succeeded", row.error
    out = await output_of(row.id, "chal")
    assert out["on_refuted"] == "degrade" and out["issuance_gaps"] == [f"反方审稿推翻了 1 句结论：「{SOUTH}」"]
    assert row.output["_issuance"]["tier"] == "degraded"


async def test_continuing_with_only_the_disposition_changed_keeps_the_review(monkeypatch, shop):
    """判失败后只把「结论被推翻时」改成「只标注」再接着跑（报错里就是这么建议的）：审稿沿用 checkpoint 里的，不重审。"""
    models = Models(monkeypatch, shop)
    run = await run_manager.start(graph=graph(shop, on_refuted="fail"), input_payload={}, run_class="formal")
    row = await wait(run.id)
    assert row.status == "failed", row.error
    calls = len(models.calls)
    await run_manager.continue_failed(run.id, graph=graph(shop, on_refuted="flag"))
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(models.calls) == calls
    out = await output_of(row.id, "chal")
    assert out["on_refuted"] == "flag" and out["counts"]["refuted"] == 1
    assert row.output["_issuance"]["tier"] == "formal"


async def test_continuing_with_a_revised_review_reviews_again(monkeypatch, shop):
    """接着跑时改了审稿本身（句数、反方模型）：checkpoint 里那份是按旧配置审的，对不上就重审，不拿旧结果冒充。"""
    models = Models(monkeypatch, shop)
    run = await run_manager.start(graph=graph(shop, on_refuted="fail"), input_payload={}, run_class="formal")
    row = await wait(run.id)
    assert row.status == "failed", row.error
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"]
    models.calls.clear()
    models.specs.clear()
    revised = graph(shop, on_refuted="flag", max_claims=1, model="another-model")
    await run_manager.continue_failed(run.id, graph=revised)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首"], "按新的句数重审"
    assert models.specs and {s.model for s in models.specs} == {"another-model"}, "按新的反方模型重审"
    out = await output_of(row.id, "chal")
    assert [i["text"] for i in out["items"]] == [EAST] and sum(out["counts"].values()) == 1
    assert len(await logs(row.id, "challenge_done")) == 2, "两次审稿各记各的"


# --------------------------------------------------------------------------
# 直通
# --------------------------------------------------------------------------


async def test_switched_off_globally_it_passes_through(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop), options={"verify": {"challenge": {"enabled": False}}})
    assert row.status == "succeeded", row.error
    assert models.calls == []
    [skipped] = await logs(row.id, "challenge_skipped")
    assert skipped["reason"] == "disabled" and skipped["level"] == "info"
    out = await output_of(row.id, "chal")
    assert out == {"skipped": True, "reason": "disabled"}
    assert row.output["_issuance"]["tier"] == "formal"
    assert await events(row.id, "llm.end", "chal") == []


async def test_the_node_can_turn_it_on_or_off(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop, enabled=True), options={"verify": {"challenge": {"enabled": False}}})
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"], "节点写了 enabled: true，全局关闭也照审"
    assert row.output["_issuance"]["tier"] == "degraded"

    models = Models(monkeypatch, shop)
    row = await finish(graph(shop, enabled=False))
    assert models.calls == [] and (await logs(row.id, "challenge_skipped"))[0]["reason"] == "disabled"


async def test_a_report_without_conclusions_passes_through(monkeypatch, shop):
    models = Models(monkeypatch, shop, writer=("下面看细节。",))
    row = await finish(graph(shop, contract=False), run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert models.calls == []
    [skipped] = await logs(row.id, "challenge_skipped")
    assert skipped["reason"] == "no_claims"
    assert await output_of(row.id, "chal") == {"skipped": True, "reason": "no_claims"}


async def test_claims_without_any_query_behind_them_are_not_sent_out(monkeypatch):
    """报告的证据里没有一次数据库查询（数字来自整形节点）：没有数据源可查，不派 agent，照实记无法判断。"""
    metrics = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
    models = Models(monkeypatch, "unused", writer=("本周销售额 [[m:gmv]]，比上周有所增长。[[see:m:gmv]]",))
    spec = chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v1", metrics=metrics),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    row = await finish(spec, run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert models.calls == []
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 0, "refuted": 0, "inconclusive": 1}
    [claim] = artifact_store.load(out["challenge_artifact"])["claims"]
    assert claim["verdict"] == "inconclusive" and "数据库查询" in claim["rationale"]
    assert claim["queries"] == [] and claim["rationale_doc"] is None


async def test_an_unavailable_model_leaves_the_claims_inconclusive(monkeypatch, shop):
    models = Models(monkeypatch, shop, broken=ProviderNotConfigured("没有配置可用的模型接入"))
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    out = await output_of(row.id, "chal")
    assert out["counts"]["inconclusive"] == 2
    record = artifact_store.load(out["challenge_artifact"])
    assert all(c["rationale"].startswith("反查未完成：核查模型不可用") for c in record["claims"])
    assert row.output["_issuance"]["tier"] == "formal"
    assert len(models.specs) == 1, "核查模型本身用不了：后面的句子照这一次的原因记，不再逐句去建模型"
    # 时间线上看得出根本没审成：不是只有一条「审了 2 句」
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["level"] == "warn" and warn["claims_skipped"] == 2 and "核查模型不可用" in warn["reason"]
    [done] = await logs(row.id, "challenge_done")
    assert "2 句未能反查" in done["message"]


# --------------------------------------------------------------------------
# 同模型提示、默认的报告、摘录遮罩
# --------------------------------------------------------------------------


async def test_reviewing_with_the_writers_own_model_is_called_out(monkeypatch, shop):
    Models(monkeypatch, shop, model_id="mock-fast")
    row = await finish(graph(shop), run_class="exploratory")
    assert row.status == "succeeded", row.error
    [warn] = await logs(row.id, "challenge_same_model")
    assert warn["model"] == "mock-fast" and warn["level"] == "warn"
    assert "mock-fast" in warn["message"]

    Models(monkeypatch, shop)
    other = await finish(graph(shop), run_class="exploratory")
    assert await logs(other.id, "challenge_same_model") == []


async def test_sentences_the_judge_ruled_out_are_not_reviewed(monkeypatch, shop):
    """结论句裁判已经判成「不是结论」的句子不审：同一份文档，标上之后就不在要审的句子里。"""
    Models(monkeypatch, shop)
    row = await finish(graph(shop), run_class="exploratory")
    assert row.status == "succeeded", row.error
    doc = await report_doc(row.id)
    east, south = unit_of(doc, EAST), unit_of(doc, SOUTH)
    assert [c.unit for c in challenge_mod.pick_claims(doc)] == [east, south]
    for _, unit in iter_units(doc):
        if unit["id"] == south:
            unit["verdict"] = {"status": "not_a_claim"}
    assert [c.unit for c in challenge_mod.pick_claims(doc)] == [east]


async def test_the_upstream_report_is_taken_when_none_is_chosen(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    spec = graph(shop)
    spec["nodes"][3]["data"]["config"].pop("report")
    row = await finish(spec, run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"]
    assert artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])["report"] == "write"


@pytest.mark.parametrize("when", ["at_query", "after_query"])
async def test_the_task_gives_the_claim_its_evidence_and_only_its_sources(monkeypatch, tmp_path, when):
    """任务里给这句结论和它挂的证据摘录：遮罩的列照旧不给——查询当时设的（记在查询快照里），和数据源现在设的
    （运行中途才设上遮罩，同证据面板的口径）都算；工具只给这句引用的查询所在的数据源。"""
    masked = {"mask_columns": ["buyer_phone"]}
    name, source_id = await _source(tmp_path, options=masked if when == "at_query" else None)
    try:
        if when == "after_query":
            real = report_mod._store

            async def store_then_mask(doc, ctx):
                # 报告写完、反方审稿开始之前，管理员给数据源设上了遮罩
                out = await real(doc, ctx)
                async with SessionLocal() as session:
                    row = await session.get(DataSource, source_id)
                    row.options = masked
                    await session.commit()
                return out

            monkeypatch.setattr(report_mod, "_store", store_then_mask)
        sql = "SELECT region AS 区域, buyer_phone, amount AS 金额 FROM sales ORDER BY id"
        plays = {"华东有一笔": Play(["SELECT COUNT(*) AS 笔数 FROM sales WHERE region = '华东'"], "stands", "没有找到反例。")}
        models = Models(monkeypatch, name, writer=("华东有一笔 [[v:Q1.r0.金额]] 的订单。[[see:Q1]]",), plays=plays)
        row = await finish(graph(name, sql=sql), run_class="exploratory")
        assert row.status == "succeeded", row.error
        task = models.tasks()["华东有一笔"]
        assert "华东有一笔 1,200 的订单。" in task
        assert "【原报告 Q1】" in task and "SELECT region AS 区域" in task, "摘录里有这句挂的查询，编号标明属于原报告"
        assert "13900000001" not in task, "遮罩的列不给"
        assert "buyer_phone" not in task.split("列：")[1].split("\n")[0]
        assert "遮罩" in task
        assert models.tools["华东有一笔"] == {f"db_schema__{name}", f"db_query__{name}"}
        record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
        assert [q["source"] for q in record["claims"][0]["queries"]] == [name]
    finally:
        await engines.invalidate(source_id)


async def test_a_metric_excerpt_marks_the_report_query_numbers_too(monkeypatch, shop):
    """句子挂的是口径卡指标（正式报告的主路径「取数 → 口径卡 → 报告」）：摘录正文里「取自查询 Q1」「查询 Q1 的 SQL」
    也标成原报告的编号，和反查 agent 自己的 Q1 分开；工具按指标的输入取自的查询找源。"""
    metrics = [{"id": "gmv", "name": "华东销售额", "unit": "元", "decimals": 0,
                "expression": "cell(nodes.fetch, 0, '销售额')"}]
    plays = {"本周华东销售额": Play([NET], "stands", "没有找到反例。")}
    models = Models(monkeypatch, shop, writer=("本周华东销售额 [[m:gmv]]，居各区域之首。[[see:m:gmv]]",), plays=plays)
    spec = chain(
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v1", metrics=metrics),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    row = await finish(spec, run_class="exploratory")
    assert row.status == "succeeded", row.error
    task = models.tasks()["本周华东销售额"]
    evidence = task.split("写作者给这句挂的证据摘录")[1].split("可以查询的数据源")[0]
    assert "【原报告 m:gmv】" in evidence and "取自原报告 Q1 的" in evidence and "原报告 Q1 的 SQL" in evidence
    assert re.findall(r"(?<!原报告 )(?<![A-Za-z0-9])Q\d+(?!\d)", evidence) == [], evidence
    assert models.tools["本周华东销售额"] == {f"db_schema__{shop}", f"db_query__{shop}"}


def test_only_the_excerpts_own_ways_of_naming_a_query_are_marked():
    """表名、字段名摘录里的「出现在查询 Qn」也标；数据里的值（季度列里的 Q1）、不是这份报告查询的编号不动。"""
    excerpt = ("【c:sales.amount】字段 sales.amount\n出现在查询 Q1：SELECT amount FROM sales\n"
               "出现在查询 Q12：SELECT SUM(amount) FROM sales\n出现在查询 Q3：SELECT 1\n列：季度 | 金额\nr0：Q1 | 1,200")
    lines = challenge_mod.mark_original(excerpt, "c:sales.amount", {"Q1", "Q12"}).splitlines()
    assert lines[:4] == ["【原报告 c:sales.amount】字段 sales.amount", "出现在原报告 Q1：SELECT amount FROM sales",
                         "出现在原报告 Q12：SELECT SUM(amount) FROM sales", "出现在查询 Q3：SELECT 1"]
    assert lines[-1] == "r0：Q1 | 1,200", "数据里的值不动"


async def test_each_claim_only_gets_the_sources_it_cites(monkeypatch, tmp_path, shop):
    """两个数据源各查一次：挂了 Q1 的那句只拿得到 Q1 那个源的工具，挂了 Q2 的只拿得到另一个；没挂依据的结论在
    这份报告用到的数据源里反查。"""
    other, other_id = await _source(tmp_path)
    try:
        text = ("华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]退款只有 [[v:Q2.r0.退款笔数]] 笔。[[see:Q2]]"
                "增长主要来自华东。")
        plays = {"居各区域之首": Play([NET]), "退款只有": Play([REFUNDS]), "增长主要来自": Play([REFUNDS])}
        models = Models(monkeypatch, shop, writer=(text,), plays=plays)
        spec = chain(
            node("start", "input"),
            node("fetch", "tool", tool=f"db_query__{shop}", args={"sql": QUERY}),
            node("fetch2", "tool", tool=f"db_query__{other}", args={"sql": REFUNDS}),
            node("write", "report", "写周报", instructions="写周报"),
            node("chal", "challenge", "审稿"),
            node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        )
        row = await finish(spec, run_class="exploratory")
        assert row.status == "succeeded", row.error
        assert models.tools["居各区域之首"] == {f"db_schema__{shop}", f"db_query__{shop}"}
        assert models.tools["退款只有"] == {f"db_schema__{other}", f"db_query__{other}"}
        assert models.tools["增长主要来自"] == {f"db_schema__{s}" for s in (shop, other)} \
            | {f"db_query__{s}" for s in (shop, other)}
        assert models.reviewed() == ["居各区域之首", "退款只有", "增长主要来自"], "挂了依据的排在前面"
    finally:
        await engines.invalidate(other_id)


# --------------------------------------------------------------------------
# 画布校验
# --------------------------------------------------------------------------


def spec_of(*nodes, edges=None) -> GraphSpec:
    if edges is None:
        edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    return GraphSpec.model_validate({"nodes": list(nodes), "edges": edges})


def problems(spec: GraphSpec, nid: str = "chal") -> list[tuple[str | None, str, str | None]]:
    return [(i.field, i.message, i.code) for i in validate_graph(spec).issues if i.level == "error" and i.node_id == nid]


BASE = [node("start", "input"), node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT 1"}),
        node("write", "report", "写周报", instructions="写周报")]


def test_the_report_must_be_an_upstream_report_writer():
    ok = spec_of(*BASE, node("chal", "challenge", report="write"))
    assert problems(ok) == []
    assert problems(spec_of(*BASE, node("chal", "challenge"))) == [], "不写就取上游唯一的报告"

    [(fld, message, code)] = problems(spec_of(*BASE, node("chal", "challenge", report="fetch")))
    assert fld == "report" and code == "challenge.report_invalid"
    assert "「fetch」" in message and "调用工具" in message and "报告撰写" in message

    [(_, message, _)] = problems(spec_of(*BASE, node("chal", "challenge", report="ghost")))
    assert "「ghost」" in message and "不存在" in message

    later = spec_of(*BASE, node("chal", "challenge", report="late"), node("late", "report", "后写的报告"))
    [(_, message, _)] = problems(later)
    assert "后写的报告" in message and "上游" in message

    [(fld, message, _)] = problems(spec_of(node("start", "input"), node("chal", "challenge")))
    assert fld == "report" and "没有" in message and "报告撰写" in message

    two = spec_of(node("start", "input"), node("a", "report", "周报甲"), node("b", "report", "周报乙"),
                  node("chal", "challenge"),
                  edges=[{"source": "start", "target": "a"}, {"source": "start", "target": "b"},
                         {"source": "a", "target": "chal"}, {"source": "b", "target": "chal"}])
    [(fld, message, _)] = problems(two)
    assert fld == "report" and "周报甲" in message and "周报乙" in message and "选择" in message

    # 两份报告前后相接：直接连着的那一份就是要审的
    stacked = spec_of(node("start", "input"), node("a", "report", "初稿"), node("b", "report", "终稿"),
                      node("chal", "challenge"))
    assert problems(stacked) == []
    from app.engine.schema import challenge_report

    assert challenge_report(stacked, stacked.node_map()["chal"]) == ("b", None)


@pytest.mark.parametrize("config, field_name", [
    ({"max_claims": 0}, "max_claims"), ({"max_claims": 1.5}, "max_claims"), ({"max_claims": True}, "max_claims"),
    ({"max_queries": -1}, "max_queries"), ({"timeout_s": 0}, "timeout_s"), ({"timeout_s": "30"}, "timeout_s"),
    ({"on_refuted": "withhold"}, "on_refuted"), ({"enabled": "yes"}, "enabled"), ({"model": 3}, "model"),
    ({"provider": ["a"]}, "provider"),
])
def test_bad_settings_are_named_in_chinese(config, field_name):
    [(fld, message, code)] = problems(spec_of(*BASE, node("chal", "challenge", report="write", **config)))
    assert fld == field_name and code == "challenge.config_invalid"
    assert field_name not in message, "写界面上的叫法，不写键名"
    assert message.startswith("「反方审稿」的「")
    if field_name == "on_refuted":
        assert "只能选「出具降档」、「只标注」或「报错并停止运行」" in message, "选项名和检查器下拉里的一致"
    if field_name == "enabled":
        assert "开启、关闭或跟随设置" in message


def test_good_settings_pass():
    spec = spec_of(*BASE, node("chal", "challenge", report="write", max_claims=3, max_queries=2, timeout_s=45.5,
                               on_refuted="flag", enabled=None, provider=None, model="claude-sonnet-5"))
    assert problems(spec) == []


def test_fail_with_continue_on_error_is_refused():
    """「报错并停止运行」配「出错时：记录错误并继续」：报的错会被当成普通错误跳过，被推翻的结论照常完整出具——比默认的
    降档还松。挡住运行，说清怎么改。"""
    spec = spec_of(*BASE, node("chal", "challenge", report="write", on_refuted="fail", on_error="continue"))
    [(fld, message, code)] = problems(spec)
    assert fld == "on_refuted" and code == "challenge.config_invalid"
    assert "「报错并停止运行」" in message and "「记录错误并继续」" in message and "完整出具" in message
    assert "on_error" not in message and "continue" not in message
    for config in ({"on_refuted": "degrade", "on_error": "continue"}, {"on_refuted": "fail", "retries": 2}):
        assert problems(spec_of(*BASE, node("chal", "challenge", report="write", **config))) == [], config


def test_a_challenge_off_the_issuance_path_is_called_out():
    """反方审稿接在侧枝上（报告 → 成果、报告 → 反方审稿）：它写的出具缺口没有成果节点收，按默认的「出具降档」也不会
    降档。画布上说破；接在成果之前、没有出具契约、不靠降档的处置都不提醒。"""
    def warned(nodes, edges=None):
        return [i for i in validate_graph(spec_of(*nodes, edges=edges)).issues if i.code == "challenge.no_issuance"]

    out = node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
               contract={"report_from": "write"})
    side = [{"source": "start", "target": "fetch"}, {"source": "fetch", "target": "write"},
            {"source": "write", "target": "chal"}, {"source": "write", "target": "out"}]
    [issue] = warned([*BASE, node("chal", "challenge", "审稿", report="write"), out], side)
    assert issue.level == "warning" and issue.node_id == "chal"
    assert "成果" in issue.message and "降档" in issue.message
    assert warned([*BASE, node("chal", "challenge", "审稿", report="write"), out]) == [], "接在成果之前"
    loose = node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}])
    assert warned([*BASE, node("chal", "challenge", "审稿", report="write"), loose], side) == [], "没有出具契约"
    for disposition in ("flag", "fail"):
        assert warned([*BASE, node("chal", "challenge", "审稿", report="write", on_refuted=disposition), out],
                      side) == [], disposition


def test_reviewing_with_the_writer_model_is_a_warning():
    spec = spec_of(node("start", "input"), node("write", "report", "写周报", model="claude-sonnet-5"),
                   node("chal", "challenge", model="claude-sonnet-5"))
    warnings = [i for i in validate_graph(spec).issues if i.level == "warning" and i.node_id == "chal"]
    assert [(i.field, i.code) for i in warnings] == [("model", "challenge.same_model")]
    assert "自己审自己" in warnings[0].message or "难以发现" in warnings[0].message


async def test_the_node_refuses_the_same_bad_settings_at_run_time():
    """跳过画布校验直接执行（旧版本发布的图、改坏了的配置）：节点自己报同一句话。"""
    spec = spec_of(*BASE, node("chal", "challenge", report="write", on_refuted="withhold"))
    [(_, message, _)] = problems(spec)
    ctx = NodeContext(node=spec.node_map()["chal"], run=RunContext(run_id="r", thread_id="r", spec=spec))
    with pytest.raises(NodeError) as caught:
        await challenge_mod.run_challenge({"nodes": {}}, ctx)
    assert str(caught.value) == message

    wrong = spec_of(*BASE, node("chal", "challenge", report="fetch"))
    [(_, message, _)] = problems(wrong)
    ctx = NodeContext(node=wrong.node_map()["chal"], run=RunContext(run_id="r", thread_id="r", spec=wrong))
    with pytest.raises(NodeError) as caught:
        await challenge_mod.run_challenge({"nodes": {}}, ctx)
    assert str(caught.value) == message
