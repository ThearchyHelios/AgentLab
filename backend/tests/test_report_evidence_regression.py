"""回归：「4 个专项 Agent 并行查库 → 报告」整条跑一遍，复现 2026-10 那次运行的形状。

那次运行状态是 succeeded，报告却不可靠：4 个 Agent 的结构化抽取全部被截断（推理模型的额度在推理阶段就用完了）；
写作者的出处清单写死 12000 字，上百次查询只有前二十来次露了面；写作者于是在清单被截断的情况下写了「数据缺失：
本轮没有查到任何核销结果」，可那个 Agent 查到了上百条。

这里用合成的门店、会员、卡券、活动库（SQLite）真跑：4 个 Agent 分别查 26、17、79、42 次（虚构的次数，一次并行发完），都配了
output_schema 和按出处核对字段；写作模型、抽取模型都是脚本。验证：

- 抽取被截断后加大额度重试成功；两次都截断的记 agent_field_truncated，字段为空值
- 出处清单在预算内，每个 Agent 都有代表，被省略的查询有编号和摘要，开头说明各来源省略了几次
- 清单有省略时的缺失断言被认出来（absence_claim）、交回重写；重写后仍有的只标注、不判失败
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from openai import LengthFinishReasonError
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import ABSENCE_RULE, DEFAULT_CATALOG_BUDGET, MARKER_RULES
from app.engine.nodes import llm as llm_mod
from app.engine.nodes import report as report_mod
from app.engine.runner import run_manager
from tests.fixtures.sources import drop_source

SOURCE = "fa_store"
TOOL = f"db_query__{SOURCE}"
#: (节点 id, 画布上的名字, 记号, 查询次数, 表, 列)
AGENTS = [
    ("agent_member", "会员专项", "member", 26, "member_daily", ["store_name", "biz_date", "new_members", "active_members"]),
    ("agent_store", "门店专项", "store", 17, "store_daily", ["store_name", "biz_date", "visits", "sales"]),
    ("agent_coupon", "卡券核销", "coupon", 79, "coupon_redemption", ["store_name", "redeemed_at", "coupon_type", "amount"]),
    ("agent_campaign", "活动内容", "campaign", 42, "campaign_view", ["campaign", "biz_date", "views", "clicks"]),
]
SCHEMA = {"type": "object", "properties": {
    "first_store": {"type": "string", "description": "第一次查询第一行的门店或活动"},
    "first_value": {"type": "number", "description": "第一次查询第一行的第三列"},
    "churn_rate": {"type": "number", "description": "流失率（要计算，填 null）"},
}}
ROWS_PER_QUERY = 5

ABSENT = ("## 概览\n\n各专项都已完成查询。[[see:Q1]]\n\n"
          "## 卡券核销\n\n卡券数据缺失：本轮没有查到核销、兑换的任何结果。")
COVERED = ("## 概览\n\n各专项都已完成查询。[[see:Q1]]\n\n"
           "## 卡券核销\n\n本报告未覆盖卡券核销的明细：证据目录有省略，相关查询未展开，依据不足。[[see:Q1]]")


def length_error(completion: int) -> LengthFinishReasonError:
    usage = CompletionUsage(completion_tokens=completion, prompt_tokens=14000, total_tokens=14000 + completion)
    return LengthFinishReasonError(completion=ChatCompletion.model_construct(
        id="x", choices=[], created=0, model="m", object="chat.completion", usage=usage))


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def store_db(tmp_path):
    """合成库：每张表 120 行，第 i 次查询取 OFFSET i 的 5 行，次次结果不同。"""
    path = tmp_path / "store.db"
    db = sqlite3.connect(path)
    for _, _, _, _, table, cols in AGENTS:
        db.execute(f"CREATE TABLE {table} ({cols[0]} TEXT, {cols[1]} TEXT, {cols[2]} {'TEXT' if table == 'coupon_redemption' else 'INTEGER'}, {cols[3]} REAL)")
        rows = []
        for r in range(120):
            when = f"2026-06-{r % 28 + 1:02d}" + (f"T08:{r % 60:02d}:39" if table == "coupon_redemption" else "")
            third = f"券型{r % 4 + 1}" if table == "coupon_redemption" else (r * 7) % 300 + 5
            rows.append((f"门店{r % 30 + 1:02d}", when, third, round((r * 13) % 900 + 0.5, 1)))
        db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await drop_source(source_id)


def sql_of(table: str, cols: list[str], i: int) -> str:
    return f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid LIMIT {ROWS_PER_QUERY} OFFSET {i}"


def node(nid, ntype, label, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}


def graph(counts: dict[str, int] | None = None, **report):
    nodes = [node("start", "input", "开始")]
    for nid, title, key, count, table, cols in AGENTS:
        nodes.append(node(nid, "agent", title, prompt=f"【{key}】按{title}的口径查数，每次查一段", tools=[TOOL],
                          approval="never", parallel_tools=True, output_schema=SCHEMA, cite_fields=True,
                          assign_to=f"{key}_findings"))
    nodes.append(node("write", "report", "专项报告", instructions="汇总各专项的发现", **report))
    nodes.append(node("out", "output", "成果", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))
    edges = [{"source": "start", "target": a[0]} for a in AGENTS] + [{"source": a[0], "target": "write"} for a in AGENTS]
    edges.append({"source": "write", "target": "out"})
    return {"nodes": nodes, "edges": edges}


class World:
    """脚本化的模型：Agent 一步并行发完自己的查询再给结论；抽取按 outcome 决定；写作者依次回 drafts。"""

    def __init__(self, monkeypatch, db_path, *, counts: dict[str, int], drafts: list[str], outcome):
        from app.providers import mock_model

        self.counts = counts
        self.drafts = drafts
        self.writer: list[list] = []
        self.extract_calls: dict[str, list[int | None]] = {a[2]: [] for a in AGENTS}
        self.budgets: list[int | None] = []
        db = sqlite3.connect(db_path)
        self.first_rows = {key: db.execute(sql_of(table, cols, 1)).fetchone() for _, _, key, _, table, cols in AGENTS}
        db.close()
        real = llm_mod.get_chat_model

        async def spy(session, spec):
            if spec.thinking == "off":
                self.budgets.append(spec.max_tokens)
            return await real(session, spec)

        monkeypatch.setattr(llm_mod, "get_chat_model", spy)
        world = self

        def text(messages) -> str:
            return "\n".join(m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
                             for m in messages)

        def who(messages) -> str:
            found = re.search(r"【(member|store|coupon|campaign)】", text(messages))
            assert found, "认不出是哪个 Agent"
            return found.group(1)

        def decide(model, messages):
            if model.response_format:
                key = who(messages)
                world.extract_calls[key].append(world.budgets[-1])
                outcome(key, len(world.extract_calls[key]), world.budgets[-1])
                return AIMessage(content=json.dumps(world.extracted(key), ensure_ascii=False))
            if any(isinstance(m, SystemMessage) and "报告撰写人" in str(m.content) for m in messages):
                world.writer.append(list(messages))
                return AIMessage(content=world.drafts[min(len(world.writer), len(world.drafts)) - 1])
            key = who(messages)
            if model.tools and not any(isinstance(m, ToolMessage) for m in messages):
                _, _, _, _, table, cols = next(a for a in AGENTS if a[2] == key)
                return AIMessage(content="分段查询。", tool_calls=[
                    {"name": TOOL, "args": {"sql": sql_of(table, cols, i)}, "id": f"{key}_{i}"}
                    for i in range(1, world.counts[key] + 1)])
            return AIMessage(content=f"【{key}】查完了，结论见各次查询。")

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)

    def extracted(self, key: str) -> dict:
        first = self.first_rows[key]
        _, _, _, _, _, cols = next(a for a in AGENTS if a[2] == key)
        return {"first_store": {"value": first[0], "from": {"call": "Q1", "row": 0, "column": cols[0]}},
                "first_value": {"value": first[3], "from": {"call": "Q1", "row": 0, "column": cols[3]}},
                "churn_rate": {"value": None, "from": None}}


async def finish(graph_json) -> Run:
    run = await run_manager.start(graph=graph_json, input_payload={})
    for _ in range(1200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


def logs(evts, code):
    return [e.data for e in evts if e.type == "log" and e.data.get("code") == code]


async def output_of(run_id: str, node_id: str) -> dict:
    [finished] = [e for e in await events(run_id, "node.finished") if e.node_id == node_id]
    return artifact_store.load(finished.data["artifact"])


def catalog_text(messages) -> str:
    """写作者第一次收到的提示里出处清单那一段（从「可引用的证据」到写作规则之前）。"""
    prompt = next(m.content for m in messages if isinstance(m, HumanMessage))
    return prompt[prompt.index("可引用的证据"):prompt.index(MARKER_RULES)].rstrip()


# --------------------------------------------------------------------------
# 和那次运行同样的形状：上百次查询、分布不均
# --------------------------------------------------------------------------


async def test_the_reported_run_shape_end_to_end(monkeypatch, store_db):
    counts = {key: count for _, _, key, count, _, _ in AGENTS}

    def outcome(key, attempt, budget):
        if key == "coupon":
            raise length_error(budget)                  # 两次都截断
        if key == "member" and attempt == 1:
            raise length_error(budget)                  # 截断一次，加大额度后成功

    world = World(monkeypatch, store_db, counts=counts, drafts=[ABSENT, COVERED], outcome=outcome)
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    evts = await events(row.id)

    # 1. 抽取：会员专项截断后重试成功；卡券核销两次都截断，字段为空值
    assert world.extract_calls["member"] == [16384, 32768]
    assert world.extract_calls["coupon"] == [16384, 32768]
    assert world.extract_calls["store"] == world.extract_calls["campaign"] == [16384]
    member = await output_of(row.id, "agent_member")
    assert member["data"]["first_store"] == world.first_rows["member"][0]
    assert member["data_evidence"]["first_store"]["status"] == "verified"
    coupon = await output_of(row.id, "agent_coupon")
    assert coupon["data"] == {"first_store": None, "first_value": None, "churn_rate": None}
    [cut] = logs(evts, "agent_field_truncated")
    assert cut["fields"] == ["first_store", "first_value", "churn_rate"] and "推理阶段就用完了" in cut["message"]
    assert not logs(evts, "agent_field_unverified")

    # 2. 出处清单：预算内，每个 Agent 都有代表，被省略的有编号和摘要，开头说明各来源省略了几次
    first = world.writer[0]
    text = catalog_text(first)
    assert len(text) <= DEFAULT_CATALOG_BUDGET, len(text)
    head = text[:900]
    assert "目录有省略" in head
    for _, title, _, count, table, _ in AGENTS:
        assert f"{title} {count} 次" in head
        group = text[text.index(f"来自「{title}」的查询"):]
        group = group[:group.index("\n\n", 1)] if "\n\n" in group[1:] else group
        assert f"共 {count} 次" in group and re.search(r"\n {2}r0：", group), f"「{title}」没有展开的查询"
    briefs = re.findall(r"\n {2}- (Q\d+)：(\w+)，5 行", text)
    assert briefs, "没有列出被省略的查询"
    assert {t for _, t in briefs} <= {a[4] for a in AGENTS}
    assert all(f"Q{n}：" in text for n in range(1, 165)), "每次查询都要在清单里（展开的或摘要）"
    # 结构化结论排在查询前面；抽取失败的卡券核销写明结构化结论为空、不代表没有数据
    assert text.index("各 Agent 交来的结构化结论") < text.index("来自「")
    assert "「卡券核销」：结构化结论为空" in text
    # 写作规则里有缺失断言那一条
    assert ABSENCE_RULE in next(m.content for m in first if isinstance(m, HumanMessage))
    [trimmed] = logs(evts, "report_catalog_trimmed")
    assert trimmed["queries"] == 164 and trimmed["budget"] == DEFAULT_CATALOG_BUDGET
    assert [n["title"] for n in sorted(trimmed["nodes"], key=lambda n: n["title"])] == \
        sorted(a[1] for a in AGENTS)
    assert trimmed["omitted"] == sum(n["omitted"] for n in trimmed["nodes"]) > 0

    # 3. 缺失断言：认出来、交回重写一次；改成「本报告未覆盖」后通过
    assert len(world.writer) == 2
    request = str(world.writer[1][-1].content)
    assert "本报告未覆盖" in request and "没有查到" in request
    [repair] = logs(evts, "report_repair")
    assert "数据缺失" in repair["message"] or "没有查到" in repair["message"]
    [checked] = [e.data for e in evts if e.type == "report.checked"]
    assert not [v for v in checked["violations"] if v["code"] == "absence_claim"]
    assert not logs(evts, "report_absence_claim")


# --------------------------------------------------------------------------
# 重写后仍然写缺失断言：只标注、不判失败
# --------------------------------------------------------------------------

SMALL = {"member": 6, "store": 4, "coupon": 12, "campaign": 8}


async def test_an_absence_claim_that_survives_the_rewrite_is_flagged_not_failed(monkeypatch, store_db):
    monkeypatch.setattr(report_mod, "catalog_budget", lambda window: 4000)
    world = World(monkeypatch, store_db, counts=SMALL, drafts=[ABSENT], outcome=lambda *a: None)
    row = await finish(graph(on_violation="fail", max_repairs=3))
    assert row.status == "succeeded", row.error
    assert len(world.writer) == 2, "缺失断言只交回重写一次，不按最多重写次数反复重写"
    evts = await events(row.id)
    [checked] = [e.data for e in evts if e.type == "report.checked"]
    assert checked["failed"] is False
    claims = [v for v in checked["violations"] if v["code"] == "absence_claim"]
    assert {v["text"] for v in claims} == {"数据缺失", "没有查到"}
    assert all(v.get("segment") and v.get("context") for v in claims), "带着片段和原句：证据面板能定位"
    [flag] = logs(evts, "report_absence_claim")
    assert flag["level"] == "warn" and flag["count"] == 2 and flag["texts"] == ["数据缺失", "没有查到"]
    assert "本报告未覆盖" in flag["message"] or "依据不足" in flag["message"]


async def test_without_omissions_an_absence_statement_is_left_alone(monkeypatch, store_db):
    """目录全给了：「没有查到」可能就是看了数据说的，不查。"""
    counts = {"member": 1, "store": 1, "coupon": 1, "campaign": 1}
    world = World(monkeypatch, store_db, counts=counts, drafts=[ABSENT], outcome=lambda *a: None)
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    assert len(world.writer) == 1
    evts = await events(row.id)
    assert not logs(evts, "report_catalog_trimmed") and not logs(evts, "report_absence_claim")
    prompt = next(m.content for m in world.writer[0] if isinstance(m, HumanMessage))
    assert ABSENCE_RULE not in prompt and "目录有省略" not in prompt
