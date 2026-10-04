"""核查用的只读 agent（engine/verify_agent.py）：查库、交回有出处的结果。双盲复算、反方审稿、审批质询共用。

- 工具只有给定数据源的 db_schema / db_query，建的时候 force_readonly：可写源上的写语句也被拒绝，数据不动
- 每次成功的查询是一份查询快照，按这个 agent 自己的顺序编 Q1、Q2…；节点里调用时发 tool.end（带 query_artifact、
  role: verify），但不往证据台账里写 query：核查的查询进了报告的写作目录，写作者就会把它们当成可以引用的证据
- 最后按调用方的结构交结果：数值字段一律是格子引用 {alias, row, column}，由代码从快照取值，模型自己写的数字
  不采信（type 列表、anyOf、$ref、allOf 里的数也是；认不出的结构下写的数一律丢掉）；没交的字段和交了 null 分开记；
  理由里的数字要写成 [[v:Qn.rX.列]]，用本 agent 自己的台账组装成文档，裸数字会被标出
- 查询次数、时长、金额（单次和每日）任何一项到顶就停止查询、按已有结果收尾；收尾的钱在查询之前就按最贵的样子
  留好；每日上限缺省按设置；模型不在价格目录里时金额上限无效，照裁判的说法记一条说明
- 永远不抛：库写不进去、模型不支持结构化输出，都落在返回值里
- 上传表格按调用方给的数据版本查（运行固定的那一版），不拿当前版本顶上

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，表名、数字都是虚构的。
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.core.config import settings
from app.core.events import EventType
from app.data import introspect, table_versions
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Setting
from app.engine import spend, verify_agent
from app.engine.toolcalls import Limit, ToolTimeout
from app.engine.verify_agent import result_schema, run_verify_agent
from app.providers import catalog as pricing
from app.providers.factory import ModelSpec, ProviderNotConfigured
from app.providers.mock_model import MockChatModel
from app.tools.registry import Versions

SPEC = ModelSpec(model="mock-fast")
SCHEMA = {"type": "object", "properties": {
    "value": {"type": "number", "description": "未作废订单的金额合计"},
    "verdict": {"type": "string", "enum": ["一致", "不一致", "无法判断"]},
}, "required": ["value", "verdict"]}
VALID_SUM = "SELECT SUM(amount) AS total FROM orders WHERE is_deleted = 0"


@pytest.fixture(autouse=True)
async def clean_spend():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([spend.SPEND_KEY, "verify"])))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == "verify"))
        await session.commit()


@pytest.fixture
async def shop(tmp_path):
    """可写的手工源：四笔订单，金额 120、80、50（已作废）、30；未作废的合计 230。返回 (源名, 文件路径)。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER, is_deleted INTEGER, paid_at TEXT);"
        "INSERT INTO orders VALUES (1, 120, 0, '2026-09-02'), (2, 80, 0, '2026-09-03'),"
        " (3, 50, 1, '2026-09-03'), (4, 30, 0, '2026-09-05');")
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=False, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name, str(path)
    await engines.invalidate(source_id)


class Script:
    """核查模型的剧本。steps：循环里每一轮发的工具调用 [(工具名, 参数, 调用 id)]，或者一段说明文字；剧本用完就收手。
    final：交结果那一轮返回的 JSON（可以是按消息算的函数）。calls 按顺序记下每次调用的种类和收到的消息。"""

    def __init__(self, monkeypatch, steps, final, *, model_id: str = "mock-fast", latency: float = 0.0):
        self.calls: list[tuple[str, list]] = []
        self.formats: list[dict] = []
        self.specs: list[ModelSpec] = []
        pending = list(steps)

        def decide(model, messages):
            if model.response_format:
                self.calls.append(("final", list(messages)))
                self.formats.append(model.response_format)
                body = final(messages) if callable(final) else final
                return AIMessage(content=json.dumps(body, ensure_ascii=False))
            self.calls.append(("step", list(messages)))
            if not pending:
                return AIMessage(content="查完了。")
            step = pending.pop(0)
            if isinstance(step, str):
                return AIMessage(content=step)
            return AIMessage(content="查一下。", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in step])

        monkeypatch.setattr(MockChatModel, "_decide", decide)

        async def fake_model(session, spec):
            self.specs.append(spec)
            return MockChatModel(model_name=model_id, latency=latency), model_id

        monkeypatch.setattr(verify_agent, "get_chat_model", fake_model)

    def tool_results(self) -> dict[str, str]:
        """模型在循环里看到的工具结果：{调用 id: 内容}。"""
        out: dict[str, str] = {}
        for _, messages in self.calls:
            for m in messages:
                if isinstance(m, ToolMessage):
                    out[m.tool_call_id] = str(m.content)
        return out

    @property
    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def q(name: str, sql: str, call_id: str) -> list[tuple]:
    return [(f"db_query__{name}", {"sql": sql}, call_id)]


async def run(name: str, **kwargs):
    base = dict(spec=SPEC, system="核对口径卡里的「销售额」。", task="算一遍未作废订单的金额合计。", sources=[name],
                output_schema=SCHEMA, data_versions=Versions.CURRENT)
    return await run_verify_agent(**{**base, **kwargs})


def counts(path: str) -> tuple[int, int]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*), SUM(amount) FROM orders").fetchone()
    finally:
        db.close()


# --------------------------------------------------------------------------
# 正常查询、取格子
# --------------------------------------------------------------------------


async def test_cells_are_taken_from_the_snapshot(monkeypatch, shop):
    name, _ = shop
    script = Script(monkeypatch, [[(f"db_schema__{name}", {"table": "orders"}, "s1")], q(name, VALID_SUM, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致",
                     "rationale": "未作废订单的金额合计是 [[v:Q1.r0.total]]。[[see:Q1]]"})
    out = await run(name)
    assert out.error is None and out.limit is None
    assert out.result["value"] == 230 and out.result["verdict"] == "一致"
    [cell] = out.cells
    assert cell["status"] == "resolved" and cell["ref"] == "Q1.r0.total" and cell["value"] == 230
    assert cell["path"] == "value" and cell["eid"].startswith("ev:cell:")
    [query] = out.queries
    assert query["alias"] == "Q1" and query["row_count"] == 1 and query["truncated"] is False
    assert "SUM(amount)" in query["sql"]
    snapshot = artifact_store.load(query["artifact"])
    assert snapshot["rows"] == [[230]] and cell["artifact"] == query["artifact"]
    # 理由按本 agent 自己的台账组装：引用渲染成快照里的值，没有违规
    assert out.rationale["markdown"] == "未作废订单的金额合计是 230。" and out.rationale["violations"] == []
    assert out.result["rationale"] == "未作废订单的金额合计是 230。"
    # 结果里的数值要求写格子引用：交给模型的结构里，value 是 {alias, row, column}
    assert "alias" in json.dumps(script.formats[0]) and "rationale" in script.formats[0]["properties"]
    assert script.kinds == ["step", "step", "step", "final"]
    assert out.usage["calls"] == 4 and out.usage["input_tokens"] > 0
    assert script.tool_results()["c1"].startswith("【证据 Q1】")


async def test_events_mark_the_queries_as_verification_and_nothing_goes_to_the_ledger(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1"), q(name, "SELEC broken", "c2")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "见 [[see:Q1]]。"})
    events: list[tuple] = []
    out = await run(name, emit=lambda kind, **data: events.append((kind, data)), run_id="run-x", node_id="check")
    ends = {d["call_id"]: d for k, d in events if k == EventType.TOOL_END}
    starts = [d for k, d in events if k == EventType.TOOL_START]
    assert [d["call_id"] for d in starts] == ["c1", "c2"], "每个工具调用一条 tool.start，和 tool.end 配对"
    assert all(d["role"] == "verify" for d in starts)
    end = ends["c1"]
    assert end["role"] == "verify" and end["query_artifact"] == out.queries[0]["artifact"] and end["alias"] == "Q1"
    # 被拒的语句照常发 tool.end（和 Agent 节点一样），运行面板按「SQL 被拒绝」开头标成失败；它没有查询快照
    assert ends["c2"]["role"] == "verify" and ends["c2"]["preview"].startswith("SQL 被拒绝")
    assert "query_artifact" not in ends["c2"] and "alias" not in ends["c2"]
    llm_ends = [d for k, d in events if k == EventType.LLM_END]
    assert len(llm_ends) == out.usage["calls"] and all(d["role"] == "verify" for d in llm_ends)
    # 工件归属这次运行、这个节点
    assert artifact_store.load(out.queries[0]["artifact"])["rows"] == [[230]]
    # 只交回结果，不交台账：没有任何 kind: query 的条目能流进 state["evidence"]
    assert "evidence" not in out.as_dict() and all("kind" not in qd for qd in out.queries)


async def test_without_emit_nothing_is_announced(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "见 [[see:Q1]]。"})
    out = await run(name)
    assert out.result["value"] == 230, "质询接口这类不在节点里的调用不传 emit，照常工作"


# --------------------------------------------------------------------------
# 只读
# --------------------------------------------------------------------------


async def test_writes_are_refused_on_a_writable_source(monkeypatch, shop):
    name, path = shop
    script = Script(monkeypatch, [q(name, "DELETE FROM orders WHERE is_deleted = 1", "w1"),
                                  q(name, "WITH k AS (SELECT 1) UPDATE orders SET amount = 0", "w2"),
                                  q(name, VALID_SUM, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致",
                     "rationale": "见 [[see:Q1]]。"})
    out = await run(name)
    seen = script.tool_results()
    assert seen["w1"].startswith("SQL 被拒绝：") and seen["w2"].startswith("SQL 被拒绝：")
    assert counts(path) == (4, 280), "数据原样不动"
    assert [qd["alias"] for qd in out.queries] == ["Q1"], "被拒的语句不编号"
    assert out.result["value"] == 230


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


async def test_the_query_limit_stops_the_loop_and_settles(monkeypatch, shop):
    name, _ = shop
    script = Script(monkeypatch, [q(name, "SELECT COUNT(*) AS n FROM orders", "c1"),
                                  [(f"db_query__{name}", {"sql": "SELECT MAX(amount) AS m FROM orders"}, "c2"),
                                   (f"db_query__{name}", {"sql": VALID_SUM}, "c3")],
                                  q(name, "SELECT MIN(amount) AS m FROM orders", "c4")],
                    {"value": {"alias": "Q2", "row": 0, "column": "m"}, "verdict": "无法判断",
                     "rationale": "查询次数用完了，最大一笔是 [[v:Q2.r0.m]]。"})
    out = await run(name, max_queries=2)
    assert out.limit == "max_queries"
    assert [qd["alias"] for qd in out.queries] == ["Q1", "Q2"]
    assert all("is_deleted" not in qd["sql"] for qd in out.queries), "同一轮里超出上限的那条没有执行"
    assert script.kinds == ["step", "step", "final"], "到顶就收尾，不再给模型下一轮"
    assert out.result["value"] == 120
    final_prompt = str(script.calls[-1][1][-1].content)
    assert "已达上限（最多查询 2 次）" in final_prompt, "收尾时告诉模型为什么不能再查"


async def test_the_time_limit_stops_the_loop_and_still_settles(monkeypatch, shop):
    name, _ = shop
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 2.0)
    endless = [q(name, f"SELECT COUNT(*) AS n FROM orders WHERE id > {i}", f"c{i}") for i in range(50)]
    script = Script(monkeypatch, endless,
                    {"value": {"alias": "Q1", "row": 0, "column": "n"}, "verdict": "无法判断", "rationale": "超时了。"},
                    latency=0.15)
    started = time.monotonic()
    out = await run(name, timeout_s=0.5, max_queries=None)
    assert out.limit == "timeout_s"
    assert script.kinds[-1] == "final", "到点也要收尾交结果"
    assert time.monotonic() - started < 0.5 + 2.0 + 1.0
    assert 1 <= len(out.queries) < 10


async def test_a_model_call_that_runs_past_the_deadline_is_cut(monkeypatch, shop):
    name, _ = shop
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.3)
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": None, "verdict": "无法判断", "rationale": "没查到。"}, latency=1.0)
    started = time.monotonic()
    out = await run(name, timeout_s=0.2)
    assert out.limit == "timeout_s" and out.queries == []
    assert time.monotonic() - started < 1.5, "超时的那次调用当场放弃，收尾那次也只等宽限的时长"
    assert out.result is None and out.error, "收尾那次也没赶上：照实说没交成结果"


async def test_a_query_that_hits_its_own_time_limit_is_not_the_overall_deadline(monkeypatch, shop):
    """一条查询超过它自己的时限（ToolTimeout，也是一种 TimeoutError）：这条查询作罢、原因交回模型让它改，核查
    照常往下走。不能当成整个核查的时长到了，把还剩的时间全扔掉。"""
    name, _ = shop
    real = verify_agent.run_bounded
    seen = {"n": 0}

    async def stuck_once(call, limit, tool_name):
        seen["n"] += 1
        if seen["n"] == 1:
            call.close()
            raise ToolTimeout(tool_name, Limit(seconds=30.0, enforce=True, kind="query"))
        return await real(call, limit, tool_name)

    monkeypatch.setattr(verify_agent, "run_bounded", stuck_once)
    script = Script(monkeypatch, [q(name, "SELECT COUNT(*) AS n FROM orders", "c1"), q(name, VALID_SUM, "c2")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"})
    events: list[tuple] = []
    out = await run(name, timeout_s=600, emit=lambda kind, **data: events.append((kind, data)))
    assert out.limit is None, "整个核查的时长远没到"
    assert script.tool_results()["c1"].startswith("查询超过 30 秒没有返回")
    assert out.result["value"] == 230 and [qd["alias"] for qd in out.queries] == ["Q1"]
    [error] = [d for k, d in events if k == EventType.TOOL_ERROR]
    assert error["call_id"] == "c1" and "核查的时长上限" not in error["error"]


@pytest.fixture
def priced(monkeypatch):
    """每次调用按 0.01 美元估算、按 0.01 美元结算：金额上限的算术好核对。"""
    monkeypatch.setattr(pricing, "find_pricing", lambda model: {"input": 1.0, "output": 1.0})
    monkeypatch.setattr(pricing, "estimate_cost", lambda model, inp, out: 0.01)


@pytest.fixture
def by_tokens(monkeypatch):
    """按 token 数估算（每个 token 0.000001 美元）：消息越长估得越多，收尾时交给模型的查询结果也要算钱。"""
    monkeypatch.setattr(pricing, "find_pricing", lambda model: {"input": 1.0, "output": 1.0})
    monkeypatch.setattr(pricing, "estimate_cost", lambda model, inp, out: (inp + out) / 1_000_000)


@pytest.fixture
async def wordy(tmp_path):
    """200 条留言，每条一大段中文：查出来交给收尾那次调用时很占 token。返回源名。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, note TEXT)")
    db.executemany("INSERT INTO notes VALUES (?, ?)", [(i, f"第{i}条留言" + "虚构的长段落文字" * 50) for i in range(200)])
    db.commit()
    db.close()
    name = f"notes_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


@pytest.mark.parametrize("cap", [0.003, 0.004, 0.005, 0.02, 0.05])
async def test_a_query_never_runs_without_money_left_to_settle_it(monkeypatch, wordy, by_tokens, cap):
    """收尾那次调用要把查到的结果交给模型：结果越长越贵。查询之前就要按收尾最贵的样子把钱留好，不然查完了却
    付不起收尾，查到的结果白查。"""
    count = {"type": "object", "properties": {"count": {"type": "integer"}}}
    Script(monkeypatch, [q(wordy, "SELECT id, note FROM notes ORDER BY id", "c1")],
           {"count": {"alias": "Q1", "row": 0, "column": "id"}, "rationale": "x"}, model_id="priced-model")
    out = await run(wordy, output_schema=count, max_cost_usd=cap, max_queries=1)
    assert out.error is None and out.result is not None, "不管上限多紧，都交得出结果"
    assert out.usage["cost_usd"] <= cap, "收尾那次也在上限以内：钱是查询之前就留好的，不是收尾时硬超出去"
    if out.queries:
        assert out.result["count"] == 0, "查了就用上了"
    else:
        assert out.limit == "max_cost_usd", "钱不够查询加收尾，就不查"


async def test_a_final_answer_budgeted_in_the_loop_is_not_refused_by_the_daily_cap(monkeypatch, shop, priced):
    """查询之后、收尾之前，别的核查把当天的余额用光了（几个核查并行）：收尾那次已经在循环里留过钱，照常交结果，
    查到的结果不白查。"""
    name, _ = shop

    async def crowded(kind, **data):
        if kind == EventType.TOOL_END:
            async with SessionLocal() as session:
                row = await session.get(Setting, spend.SPEND_KEY)
                row.value = {**row.value, "usd": 1.999}
                await session.commit()

    Script(monkeypatch, [q(name, VALID_SUM, "c1"), q(name, "SELECT COUNT(*) AS n FROM orders", "c2")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"},
           model_id="priced-model")
    out = await run(name, daily_max_usd=2.0, emit=crowded)
    assert out.limit == "daily_max_usd", "下一轮查询付不起了"
    assert out.error is None and out.result["value"] == 230


async def test_the_daily_cap_comes_from_the_settings_unless_given(monkeypatch, shop, priced):
    """不传每日上限就按设置里的「核查每日金额上限」：节点、质询接口不必各自记得去读；明确传 None 才是不限。"""
    name, _ = shop
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 0.995, "calls": 3}))
        session.add(Setting(key="verify", value={"daily_max_usd": 1.0}))
        await session.commit()
    final = {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"}
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")], final, model_id="priced-model")
    out = await run(name)
    assert out.limit == "daily_max_usd" and script.calls == [] and out.result is None
    assert "$1.00" in out.error

    Script(monkeypatch, [q(name, VALID_SUM, "c1")], final, model_id="priced-model")
    unlimited = await run(name, daily_max_usd=None)
    assert unlimited.limit is None and unlimited.result["value"] == 230


async def test_the_cost_limit_keeps_room_for_the_final_answer(monkeypatch, shop, priced):
    name, _ = shop
    endless = [q(name, f"SELECT COUNT(*) AS n FROM orders WHERE id > {i}", f"c{i}") for i in range(10)]
    script = Script(monkeypatch, endless,
                    {"value": {"alias": "Q1", "row": 0, "column": "n"}, "verdict": "无法判断", "rationale": "钱用完了。"},
                    model_id="priced-model")
    out = await run(name, max_cost_usd=0.035, max_queries=None)
    assert out.limit == "max_cost_usd"
    assert script.kinds == ["step", "step", "final"], "每一轮都给收尾那次留出钱：两轮之后再查就付不起收尾了"
    assert out.usage["cost_usd"] == pytest.approx(0.03) and out.usage["cost_usd"] <= 0.035
    assert out.result["value"] == 4


async def test_the_daily_cap_is_shared_by_every_verification(monkeypatch, shop, priced):
    name, _ = shop
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 1.995, "calls": 9}))
        await session.commit()
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"},
                    model_id="priced-model")
    out = await run(name, daily_max_usd=2.0)
    assert out.limit == "daily_max_usd" and script.calls == [] and out.usage["calls"] == 0
    assert out.result is None and out.error
    assert (await spend.daily_spend())["usd"] == pytest.approx(1.995), "没调用就不记账"


async def test_spending_goes_into_the_verify_ledger(monkeypatch, shop, priced):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "见 [[see:Q1]]。"},
           model_id="priced-model")
    out = await run(name, daily_max_usd=2.0)
    day = await spend.daily_spend()
    assert day["calls"] == out.usage["calls"] == 3 and day["usd"] == pytest.approx(out.usage["cost_usd"])


async def test_an_unpriced_model_makes_the_cost_limits_moot_and_says_so(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "见 [[see:Q1]]。"},
           model_id="house-model-unpriced")
    events: list[tuple] = []
    out = await run(name, max_cost_usd=0.0001, daily_max_usd=0.0001,
                    emit=lambda kind, **data: events.append((kind, data)))
    assert out.limit is None and out.result["value"] == 230, "估不出金额：金额上限拦不住，也不该拦"
    assert any("不在价格目录中" in n and "金额上限" in n for n in out.notes)
    assert any(k == EventType.LOG and d.get("code") == "verify_unpriced" for k, d in events)
    assert out.usage["unpriced_calls"] == out.usage["calls"] == 3
    assert (await spend.daily_spend())["unpriced_calls"] == 3


# --------------------------------------------------------------------------
# 数值只认格子
# --------------------------------------------------------------------------


async def test_numbers_the_model_writes_itself_are_ignored(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": 999, "verdict": "一致", "rationale": "合计是 [[v:Q1.r0.total]]。"})
    out = await run(name)
    assert out.result["value"] is None, "直接写的数字不采信，也不兜底成 0"
    [cell] = out.cells
    assert cell["status"] == "unresolved" and cell["model_value"] == 999 and "格子引用" in cell["reason"]


async def test_a_value_tucked_into_the_reference_is_ignored_too(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total", "value": 999}, "verdict": "一致", "rationale": "x"})
    out = await run(name)
    assert out.result["value"] == 230, "引用里夹带的值不看，按快照取"


@pytest.mark.parametrize("ref,reason", [
    ({"alias": "Q7", "row": 0, "column": "total"}, "Q7"),
    ({"alias": "Q1", "row": 5, "column": "total"}, "第 5 行"),
    ({"alias": "Q1", "row": 0, "column": "amount"}, "amount"),
])
async def test_a_reference_that_points_nowhere_is_unresolved(monkeypatch, shop, ref, reason):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {"value": ref, "verdict": "一致", "rationale": "x"})
    out = await run(name)
    assert out.result["value"] is None
    assert out.cells[0]["status"] == "unresolved" and reason in out.cells[0]["reason"]


async def test_bare_numbers_in_the_rationale_are_flagged(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致",
            "rationale": "合计是 230，比原流程多 3 成。"})
    out = await run(name)
    flagged = [v["text"] for v in out.rationale["violations"] if v["code"] == "uncited_number"]
    assert flagged == ["230", "3"] and out.rationale["stats"]["uncited_numbers"] == 2
    bare = [s for _, u in [(b, u) for b in out.rationale["blocks"] for u in b["units"]] for s in u["segments"]
            if s.get("issue") == "uncited_number"]
    assert [s["text"] for s in bare] == ["230", "3"], "界面要在这几个字底下画「无出处」"


async def test_nested_numbers_and_arrays_are_references_too(monkeypatch, shop):
    name, _ = shop
    schema = {"type": "object", "properties": {
        "totals": {"type": "array", "items": {"type": "number"}},
        "detail": {"type": "object", "properties": {"count": {"type": "integer"}, "note": {"type": "string"}}},
    }}
    Script(monkeypatch, [q(name, "SELECT id, amount FROM orders ORDER BY id", "c1")],
           {"totals": [{"alias": "Q1", "row": 0, "column": "amount"}, {"alias": "Q1", "row": 3, "column": "amount"}],
            "detail": {"count": {"alias": "Q1", "row": 3, "column": "id"}, "note": "四笔"}, "rationale": "x"})
    out = await run(name, output_schema=schema)
    assert out.result["totals"] == [120, 30] and out.result["detail"] == {"count": 4, "note": "四笔"}
    assert [c["path"] for c in out.cells] == ["totals[0]", "totals[1]", "detail.count"]


#: Pydantic 这类工具生成的结构：可为 null 的数写成 anyOf，嵌套的对象写成 $ref 到 $defs，带说明的引用包一层 allOf，
#: Optional[对象] 是 anyOf [$ref, null]
PYDANTIC_STYLE = {
    "type": "object",
    "$defs": {"Region": {"type": "object", "properties": {"name": {"type": "string"}, "total": {"type": "number"}}}},
    "properties": {
        "value": {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "未作废订单的金额合计"},
        "count": {"type": ["integer", "null"]},
        "top": {"$ref": "#/$defs/Region"},
        "backup": {"anyOf": [{"$ref": "#/$defs/Region"}, {"type": "null"}]},
        "wrapped": {"allOf": [{"$ref": "#/$defs/Region"}], "description": "带说明的引用"},
        "regions": {"type": "array", "items": {"$ref": "#/$defs/Region"}},
    },
}


def test_every_way_of_writing_a_number_becomes_a_reference():
    """anyOf、type 列表、$ref、allOf 里的数也换成格子引用：认不出这些写法，模型在那里写的数就绕过了格子这一关。"""
    shaped = result_schema(PYDANTIC_STYLE)
    assert "$ref" not in json.dumps(shaped) and "$defs" not in shaped

    def is_ref(node):
        return node["anyOf"][0]["required"] == ["alias", "row", "column"] and node["anyOf"][1] == {"type": "null"}

    props = shaped["properties"]
    assert is_ref(props["value"]) and "未作废订单的金额合计" in props["value"]["description"]
    assert is_ref(props["count"])
    assert is_ref(props["top"]["properties"]["total"]) and props["top"]["properties"]["name"] == {"type": "string"}
    assert is_ref(props["wrapped"]["properties"]["total"])
    assert is_ref(props["regions"]["items"]["properties"]["total"])
    [region, null] = props["backup"]["anyOf"]
    assert is_ref(region["properties"]["total"]) and null == {"type": "null"}


async def test_numbers_written_directly_in_any_of_those_shapes_are_ignored(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": 999, "count": 7, "top": {"name": "分区甲", "total": 999}, "backup": {"name": "分区乙", "total": 1},
            "wrapped": {"name": "分区丙", "total": 2}, "regions": [{"name": "分区丁", "total": 3}], "rationale": "x"})
    out = await run(name, output_schema=PYDANTIC_STYLE)
    assert out.result == {"value": None, "count": None, "top": {"name": "分区甲", "total": None},
                          "backup": {"name": "分区乙", "total": None}, "wrapped": {"name": "分区丙", "total": None},
                          "regions": [{"name": "分区丁", "total": None}], "rationale": "x"}
    assert {c["path"]: c["model_value"] for c in out.cells} == {
        "value": 999, "count": 7, "top.total": 999, "backup.total": 1, "wrapped.total": 2, "regions[0].total": 3}
    assert all(c["status"] == "unresolved" and "没有给出格子引用" in c["reason"] for c in out.cells), \
        "都认成了数值字段（该写格子引用），不是碰巧被当成认不出的结构丢掉"


async def test_references_in_those_shapes_are_resolved(monkeypatch, shop):
    name, _ = shop
    ref = {"alias": "Q1", "row": 0, "column": "total"}
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": ref, "count": None, "top": {"name": "分区甲", "total": ref}, "backup": None,
            "wrapped": {"name": "分区丙", "total": ref}, "regions": [{"name": "分区丁", "total": ref}], "rationale": "x"})
    out = await run(name, output_schema=PYDANTIC_STYLE)
    assert out.result["value"] == 230 and out.result["top"]["total"] == 230 and out.result["wrapped"]["total"] == 230
    assert out.result["regions"] == [{"name": "分区丁", "total": 230}]
    assert out.result["count"] is None and out.result["backup"] is None, "可以为 null 的照实是 null"


async def test_numbers_under_a_shape_that_is_not_recognised_are_dropped(monkeypatch, shop):
    """认不出的结构（没写类型、没写 items 的数组、没写属性的对象）、文本字段里冒出来的数：一律不采信，照记下来。
    码值（enum）里的数是调用方给的选项，不是查出来的数，照收。"""
    name, _ = shop
    schema = {"type": "object", "properties": {
        "anything": {}, "notes": {"type": "array"}, "label": {"type": "string"}, "level": {"enum": [1, 2, 3]},
        "extra": {"type": "object"}}}
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"anything": 999, "notes": ["见 Q1", 120], "label": 5, "level": 2,
            "extra": {"note": "按支付时间算", "share": 0.3}, "rationale": "x"})
    out = await run(name, output_schema=schema)
    assert out.result == {"anything": None, "notes": ["见 Q1", None], "label": None, "level": 2,
                          "extra": {"note": "按支付时间算", "share": None}, "rationale": "x"}
    assert {c["path"]: c["model_value"] for c in out.cells} == {"anything": 999, "notes[1]": 120, "label": 5,
                                                                "extra.share": 0.3}
    assert all(c["status"] == "unresolved" for c in out.cells)


async def test_references_written_as_json_text_are_read(monkeypatch, shop):
    """有的模型把嵌套的对象、数组写成一段 JSON 文本：照样认（裁判也是这么认的）。还不是数组的照实记下格式不对。"""
    name, _ = shop
    schema = {"type": "object", "properties": {
        "value": {"type": "number"}, "totals": {"type": "array", "items": {"type": "number"}},
        "broken": {"type": "array", "items": {"type": "number"}}}}
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": '{"alias": "Q1", "row": 0, "column": "total"}',
            "totals": '[{"alias": "Q1", "row": 0, "column": "total"}]', "broken": "没有这一项", "rationale": "x"})
    out = await run(name, output_schema=schema)
    assert out.result == {"value": 230, "totals": [230], "broken": None, "rationale": "x"}
    broken = next(c for c in out.cells if c["path"] == "broken")
    assert broken["status"] == "unresolved" and "数组" in broken["reason"]


async def test_a_number_field_pointing_at_a_text_cell_is_unresolved(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, "SELECT paid_at, amount FROM orders ORDER BY id", "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "paid_at"}, "verdict": "一致", "rationale": "x"})
    out = await run(name)
    assert out.result["value"] is None, "数值字段只收数：日期文本拿去和原流程的数比，比较会出错"
    [cell] = out.cells
    assert cell["status"] == "unresolved" and "不是数值" in cell["reason"] and cell["ref"] == "Q1.r0.paid_at"
    assert cell["value"] == "2026-09-02"


async def test_a_field_left_out_is_not_a_field_reported_missing(monkeypatch, shop):
    """没交这个字段和交了 null（模型说查不到）是两回事：前者是没按结构交，后者才是 missing。"""
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {"verdict": "无法判断", "rationale": "x"})
    left_out = await run(name)
    [cell] = left_out.cells
    assert cell["status"] == "unresolved" and "没有交" in cell["reason"]

    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {"value": None, "verdict": "无法判断", "rationale": "x"})
    said_null = await run(name)
    assert said_null.cells == [{"path": "value", "status": "missing"}]


async def test_an_answer_with_none_of_the_fields_is_an_error(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {"total": 230, "rationale": "合计 230"})
    out = await run(name)
    assert out.result is None and out.cells == [] and "没有按结构交结果" in out.error


# --------------------------------------------------------------------------
# 数据版本
# --------------------------------------------------------------------------


def book(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "明细"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_pinned_data_versions_are_used(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    name = f"upload_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(id=uuid.uuid4().hex, name=name, kind="sqlite", readonly=True, origin="upload")
        first = await table_versions.publish_upload(session, row, book([["区域", "数量"], ["分区甲", 1], ["分区乙", 2]]),
                                                    "客流.xlsx", header_row=1, mixed="reject", raw_mode=False)
        source_id = row.id
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one()
        await table_versions.publish_upload(session, row, book([["区域", "数量"], ["分区甲", 60], ["分区乙", 40]]),
                                            "客流.xlsx", header_row=1, mixed="reject", raw_mode=False)
    total = {"type": "object", "properties": {"total": {"type": "number"}}}
    final = {"total": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "x"}
    sql = 'SELECT SUM("数量") AS total FROM "明细"'

    Script(monkeypatch, [q(name, sql, "c1")], final)
    pinned = await run(name, output_schema=total,
                       data_versions={source_id: {"snapshot": first.snapshot_id, "name": name}})
    assert pinned.result["total"] == 3, "按运行固定的那一版查"
    assert artifact_store.load(pinned.queries[0]["artifact"])["data_version"] == first.snapshot_id

    Script(monkeypatch, [q(name, sql, "c1")], final)
    current = await run(name, output_schema=total, data_versions=Versions.CURRENT)
    assert current.result["total"] == 100


# --------------------------------------------------------------------------
# 出错不抛
# --------------------------------------------------------------------------


async def test_a_model_that_is_not_configured_is_reported_not_raised(monkeypatch, shop):
    name, _ = shop

    async def broken(session, spec):
        raise ProviderNotConfigured("模型接入「无」没有可用的模型", "请在「设置 → 模型接入」中添加", code="no_model")

    monkeypatch.setattr(verify_agent, "get_chat_model", broken)
    out = await run(name)
    assert out.result is None and "核查模型不可用" in out.error and out.usage["calls"] == 0


async def test_an_unknown_source_is_noted(monkeypatch, shop):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"})
    out = await run(name, sources=[name, "no_such_source"])
    assert out.result["value"] == 230
    assert any("no_such_source" in n for n in out.notes)


async def test_a_final_answer_in_plain_text_json_is_still_read(monkeypatch, shop):
    """结构化输出失败（模型不支持、给的不是合法 JSON）：退回普通调用，从回复里把 JSON 抠出来。"""
    name, _ = shop
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")], "不是 JSON")

    original = MockChatModel._decide

    def decide(model, messages):
        if not model.response_format and any("交结果" in str(m.content) for m in messages[-1:]):
            return AIMessage(content='好的：```json\n{"value": {"alias": "Q1", "row": 0, "column": "total"}, '
                                     '"verdict": "一致", "rationale": "x"}\n```')
        return original(model, messages)

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    out = await run(name)
    assert out.result["value"] == 230 and script.kinds.count("final") == 1


def plain_reply(monkeypatch, reply):
    """普通调用（退回的那一次）的回复：reply(最后一条消息的正文) → 回复正文。其余照剧本。"""
    original = MockChatModel._decide
    seen: list[str] = []

    def decide(model, messages):
        text = str(messages[-1].content)
        if not model.response_format and "交结果" in text:
            seen.append(text)
            return AIMessage(content=reply(text))
        return original(model, messages)

    monkeypatch.setattr(MockChatModel, "_decide", decide)
    return seen


async def test_the_plain_fallback_is_told_which_fields_to_fill(monkeypatch, shop):
    """结构化输出不成、退回普通调用时，结构不在请求里了：要写进消息，不然模型不知道字段叫什么，交回来的字段
    对不上，全成了「查不到」。"""
    name, _ = shop
    schema = {"type": "object", "properties": {
        "blind_total_amount": {"type": "number", "description": "盲算的金额合计"},
        "consistency_verdict": {"type": "string", "enum": ["一致", "不一致"]}}}
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], "不是 JSON")

    def reply(text):
        # 模型只能按消息里看得到的字段名交
        body = {"rationale": "x"}
        if "blind_total_amount" in text:
            body["blind_total_amount"] = {"alias": "Q1", "row": 0, "column": "total"}
        if "consistency_verdict" in text:
            body["consistency_verdict"] = "一致"
        return json.dumps(body, ensure_ascii=False)

    seen = plain_reply(monkeypatch, reply)
    out = await run(name, output_schema=schema)
    assert len(seen) == 1 and "盲算的金额合计" in seen[0] and "alias" in seen[0]
    assert out.error is None and out.result == {"blind_total_amount": 230, "consistency_verdict": "一致",
                                                "rationale": "x"}


async def test_a_model_without_structured_output_goes_straight_to_the_plain_call(monkeypatch, shop):
    """模型不支持结构化输出（建的时候就报错）：直接走普通调用，不算一次调用，也不抛。"""
    name, _ = shop

    def unsupported(self, schema, **kwargs):
        raise NotImplementedError("这个模型不支持结构化输出")

    monkeypatch.setattr(MockChatModel, "with_structured_output", unsupported)
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")], "不会用到")
    plain_reply(monkeypatch, lambda text: '{"value": {"alias": "Q1", "row": 0, "column": "total"}, '
                                          '"verdict": "一致", "rationale": "x"}')
    out = await run(name)
    assert out.error is None and out.result["value"] == 230
    assert out.usage["calls"] == len(script.calls) + 1 == 3, "两轮查库加一次普通调用，建不起来的那次不算"


async def test_a_failing_ledger_is_reported_not_raised(monkeypatch, shop):
    """每日账写不进去（库被锁住）：照实交回，不抛。节点因此降成「核不了」，质询接口不会回 500。"""
    name, _ = shop

    async def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(spend, "charge", locked)
    Script(monkeypatch, [q(name, VALID_SUM, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致", "rationale": "x"})
    out = await run(name)
    assert out.result is None and out.error
