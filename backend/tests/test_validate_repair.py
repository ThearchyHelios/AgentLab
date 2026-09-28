"""校验节点的自动修复只许改格式，不许补数据。

一次真实运行：上游的查询 agent 没有工具，交上来的是「假设调用工具」加一段 SQL，
里面一个数都没有。校验第一次失败（缺 total_count），自动修复把原文喂给模型让它
「修正后重新输出」——模型就编了一个 total_count: 0，校验通过，运行成功，最后
交到用户手上的是一个凭空的 0。

修复提示词要说清楚「原文没有的值填 null」，但光靠提示词拦不住。修复之后要核对：
修复结果里出现了原文没有的数字或文字，这次修复就作废，报错写清是哪个值。
另外修复是一次真实的模型调用，它的用量以前没发 llm.end，也没算进 usage。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager

SCHEMA = {
    "type": "object",
    "properties": {"total_count": {"type": "integer"}, "table": {"type": "string"}},
    "required": ["total_count"],
}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def _graph(upstream_text: str, **validate):
    return {
        "nodes": [
            node("start", "input"),
            node("draft", "transform", mode="template", template=upstream_text),
            node("check", "validate", schema=SCHEMA, source="{{ nodes.draft }}", max_retries=2,
                 **validate),
            node("out", "output", fields=[{"name": "r", "value": "{{ nodes.check.data }}"}]),
        ],
        "edges": [{"source": "start", "target": "draft"}, {"source": "draft", "target": "check"},
                  {"source": "check", "target": "out"}],
    }


def _repairs_with(monkeypatch, *replies: dict) -> list[str]:
    """修复模型依次交回这些 JSON。返回它收到的提示词。"""
    from app.providers import mock_model

    prompts: list[str] = []

    def _decide(self, messages):
        prompts.append("\n".join(str(m.content) for m in messages))
        reply = replies[min(len(prompts) - 1, len(replies) - 1)]
        return AIMessage(content=json.dumps(reply, ensure_ascii=False))

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return prompts


async def _finish(graph) -> tuple[Run, list[RunEvent]]:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed"):
                break
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return row, events


PRETEND = "我假设调用工具 db_query__shop 执行：\nSELECT COUNT(*) FROM orders\n结果应该能反映订单总数。"


async def test_a_repair_that_invents_a_number_fails(monkeypatch):
    prompts = _repairs_with(monkeypatch, {"total_count": 0})
    row, events = await _finish(_graph(PRETEND))

    assert row.status == "failed", f"修复编出来的 0 被当成了结果：{row.output}"
    assert "修复时出现了原文没有的值：total_count=0" in (row.error or ""), row.error
    # 提示词里要把规矩说在前面
    assert prompts and "null" in prompts[0] and "编造" in prompts[0]


async def test_a_repair_that_only_reformats_passes(monkeypatch):
    _repairs_with(monkeypatch, {"total_count": 1284, "table": "orders"})
    row, _ = await _finish(_graph("查到了：orders 表一共 1,284 条记录。"))
    assert row.status == "succeeded", row.error
    assert json.loads(row.output["r"]) == {"total_count": 1284, "table": "orders"}


async def test_a_repair_that_admits_there_is_no_data_still_fails_the_schema(monkeypatch):
    _repairs_with(monkeypatch, {"total_count": None})
    row, _ = await _finish(_graph(PRETEND))
    assert row.status == "failed"
    assert "total_count" in (row.error or "") and "原文没有的值" not in (row.error or "")


async def test_repair_calls_are_counted(monkeypatch):
    _repairs_with(monkeypatch, {"total_count": 0})
    row, events = await _finish(_graph(PRETEND))
    ends = [e.data for e in events if e.type == "llm.end" and e.node_id == "check"]
    assert len(ends) == 2, "两次修复各是一次模型调用，都要记账"
    assert row.usage.get("output_tokens", 0) >= sum(e["output_tokens"] for e in ends) > 0


async def test_numbers_written_in_chinese_count_as_present(monkeypatch):
    _repairs_with(monkeypatch, {"total_count": 12, "table": "orders"})
    row, _ = await _finish(_graph("orders 表里一共有十二条记录。"))
    assert row.status == "succeeded", row.error


# SQL 里的数字不是证据：WHERE status > 0、LIMIT 10 这类写法到处都是，认它们的话，
# 编出来的 total_count: 0 就有了「出处」——这恰恰是「假设调用工具 + SQL」最常见的形状


@pytest.mark.parametrize("upstream, invented", [
    ('{"sql":"SELECT COUNT(*) FROM orders WHERE status > 0"}', {"total_count": 0}),
    ('假设调用工具：{"tool": "db_query__shop", "sql": "SELECT COUNT(*) FROM orders LIMIT 10"}',
     {"total_count": 10}),
    ("我假设调用工具执行了 SELECT COUNT(*) FROM orders WHERE deleted = 0，结果还没拿到",
     {"total_count": 0}),
    ("```sql\nSELECT COUNT(*) FROM orders\nWHERE status > 0\nLIMIT 10\n```\n查询已提交", {"total_count": 10}),
    ('{"sql":"SELECT COUNT(*) FROM orders WHERE status > 0"}', {"total_count": "0"}),
])
def test_numbers_inside_sql_do_not_count(upstream, invented):
    from app.engine.nodes.human import _invented

    assert _invented(invented, upstream, SCHEMA), (upstream, invented)


def test_numbers_outside_the_sql_still_count():
    from app.engine.nodes.human import _invented

    text = "执行了 SELECT COUNT(*) FROM orders WHERE status > 0，结果一共 12 条"
    assert _invented({"total_count": 12}, text, SCHEMA) == []
    fenced = '```json\n{"total_count": 12}\n```'
    assert _invented({"total_count": 12}, fenced, SCHEMA) == []


def test_a_percentage_may_be_written_as_a_fraction():
    from app.engine.nodes.human import _invented

    assert _invented({"rate": 0.125, "mom": -0.03}, "转化率 12.5%，环比 -3%", {}) == []
    assert _invented({"rate": 12.5}, "转化率 12.5%", {}) == []
    assert _invented({"rate": 0.2}, "转化率 12.5%", {}) == ["rate=0.2"]


async def test_a_repair_that_takes_its_number_from_the_sql_fails(monkeypatch):
    _repairs_with(monkeypatch, {"total_count": 0})
    row, _ = await _finish(_graph('假设调用工具：{"tool": "db_query__shop", '
                                  '"sql": "SELECT COUNT(*) FROM orders WHERE status > 0"}'))
    assert row.status == "failed", f"SQL 里的 0 被当成了出处：{row.output}"
    assert "修复时出现了原文没有的值：total_count=0" in (row.error or ""), row.error
