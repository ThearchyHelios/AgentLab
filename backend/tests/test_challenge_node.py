"""反方审稿节点（engine/nodes/challenge.py）：报告写完后挑几句关键结论，各派一个只读 agent 站在反方找反例。

- 选句照结论句裁判的预筛（judge.candidates / claim_priority）：挂了依据的、带比较或因果的排前面，取前「最多审几句」句
- 每句一个只读 agent（engine/verify_agent.py）：工具只给这句引用的查询所在的数据源，任务里给这句结论和它挂的证据摘录
  （judge.prepare，遮罩照旧）；理由里的数字只能引用它自己的查询格子，裸数字被标出来
- 结论四选一：经受住 / 被削弱 / 被推翻 / 无法判断；完整记录写进 challenge_artifact，随运行封存；台账只记 kind challenge，
  它的查询不进任何报告的写作目录
- 被推翻的：on_refuted 为 degrade（默认）时写 issuance_gaps，出具降档；fail 判节点失败；flag 只记录
- 上限：句数、每句查询数、每句时长、全局每日金额；全局关闭、报告没有可审的结论句时直通
- 整个反查放在一个 @task 里：接着跑时不重复执行、不重复记账
- 人工审批审这份报告时，审批卡附上反方审稿的概况
- 画布校验：要审的报告必须是上游的报告撰写节点

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，区域、金额、电话都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
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
from app.engine.runner import run_manager, verify_manifest
from app.engine.schema import GraphSpec, validate_graph
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


@pytest.fixture
async def masked_shop(tmp_path):
    """同样的库，买家电话按数据源的设置遮罩。"""
    name, source_id = await _source(tmp_path, options={"mask_columns": ["buyer_phone"]})
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
    assert all("$2.00" in c["rationale"] for c in record["claims"])
    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "daily_max_usd" and limit["claims_skipped"] == 2
    assert row.output["_issuance"]["tier"] == "formal"


# --------------------------------------------------------------------------
# 审批卡
# --------------------------------------------------------------------------


def gated(source: str, **challenge) -> dict:
    challenge.setdefault("report", "write")
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": QUERY}),
        node("write", "report", "写周报", instructions="写周报"),
        node("chal", "challenge", "审稿", **challenge),
        node("gate", "human", "主管审批", mode="approve", title="审核周报", message="{{ nodes.write.text }}"),
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
    Models(monkeypatch, shop, broken=ProviderNotConfigured("没有配置可用的模型接入"))
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    out = await output_of(row.id, "chal")
    assert out["counts"]["inconclusive"] == 2
    record = artifact_store.load(out["challenge_artifact"])
    assert all(c["rationale"].startswith("反查未完成：核查模型不可用") for c in record["claims"])
    assert row.output["_issuance"]["tier"] == "formal"


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


async def test_the_upstream_report_is_taken_when_none_is_chosen(monkeypatch, shop):
    models = Models(monkeypatch, shop)
    spec = graph(shop)
    spec["nodes"][3]["data"]["config"].pop("report")
    row = await finish(spec, run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ["居各区域之首", "华南销售额超过华东"]
    assert artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])["report"] == "write"


async def test_the_task_gives_the_claim_its_evidence_and_only_its_sources(monkeypatch, masked_shop):
    """任务里给这句结论和它挂的证据摘录：遮罩的列照旧不给；工具只给这句引用的查询所在的数据源。"""
    sql = "SELECT region AS 区域, buyer_phone, amount AS 金额 FROM sales ORDER BY id"
    plays = {"华东有一笔": Play(["SELECT COUNT(*) AS 笔数 FROM sales WHERE region = '华东'"], "stands", "没有找到反例。")}
    models = Models(monkeypatch, masked_shop, writer=("华东有一笔 [[v:Q1.r0.金额]] 的订单。[[see:Q1]]",), plays=plays)
    row = await finish(graph(masked_shop, sql=sql), run_class="exploratory")
    assert row.status == "succeeded", row.error
    task = models.tasks()["华东有一笔"]
    assert "华东有一笔 1,200 的订单。" in task
    assert "原报告" in task and sql.split(" FROM")[0][:20] in task, "摘录里有这句挂的查询"
    assert "13900000001" not in task and "buyer_phone" not in task.split("列：")[1].split("\n")[0]
    assert "遮罩" in task
    assert models.tools["华东有一笔"] == {f"db_schema__{masked_shop}", f"db_query__{masked_shop}"}
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    assert [q["source"] for q in record["claims"][0]["queries"]] == [masked_shop]


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


def test_good_settings_pass():
    spec = spec_of(*BASE, node("chal", "challenge", report="write", max_claims=3, max_queries=2, timeout_s=45.5,
                               on_refuted="flag", enabled=None, provider=None, model="claude-sonnet-5"))
    assert problems(spec) == []


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
