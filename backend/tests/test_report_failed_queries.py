"""上游查库全失败时，报告照实说没查到（engine/nodes/report.failed_queries）。

2026-10 部署上撞过：数据源连不上，Agent 的六次查询全部「查询失败」，证据目录里一次查询都没有；写作模型照着上游文字里的
SQL 和列名写出 [[v:Q1.r0.…]]，重写一次还是那样，答案里全是没填上的 ⟦?v:Q1…⟧。现在写作提示里多一节「没有查到的数据」，
一次成功的都没有时不许写单元格引用、照实写没查到和原因；重写请求里同一句。上游节点的调用记录照 Agent 产出的形状写
（tool_calls：tool、args、ok、result），数据源和 SQL 都是虚构的。
"""
from __future__ import annotations

from tests.test_report_node import engine_up, events, finish, node, script  # noqa: F401

FAILED_CALLS = ("[{'tool': 'db_query__shop', 'args': {'sql': 'SELECT COUNT(*) AS n FROM orders'}, 'ok': True, "
                "'result': '查询失败：connect() got an unexpected keyword argument'}, "
                "{'tool': 'db_query__shop', 'args': {'sql': 'SELECT COUNT(*) AS n FROM orders'}, 'repeat': True}, "
                "{'tool': 'db_schema__shop', 'args': {}, 'ok': True, 'result': '表 orders …'}]")


def graph(calls: str = FAILED_CALLS) -> dict:
    nodes = [
        node("start", "input", fields=[{"name": "q", "default": "订单有多少"}]),
        node("agent1", "transform", mode="expression",
             expression="{'text': '三条查询都失败了：连不上数据源', 'tool_calls': " + calls + "}"),
        node("write", "report", instructions="回答：{{ input.q }}"),
        node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]),
    ]
    chain = ["start", "agent1", "write", "out"]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]}


def _prompt(messages) -> str:
    return "\n".join(str(m.content) for m in messages)


async def test_the_writer_is_told_the_queries_failed_and_not_to_cite(monkeypatch):
    seen = script(monkeypatch, "订单有 [[v:Q1.r0.n]] 单。", "没有查到订单数：数据源连接失败，请检查数据源连接。")
    run = await finish(graph())
    first = _prompt(seen[0])
    assert "## 没有查到的数据" in first and "上游有 1 次数据查询失败了" in first, first
    assert "db_query__shop：SELECT COUNT(*) AS n FROM orders（查询失败：connect() got an unexpected keyword argument）" in first
    assert "这次一次成功的查询都没有：不要写 [[v:…]] 单元格引用" in first
    assert "db_schema__shop" not in first.split("## 没有查到的数据")[1].split("\n\n")[0], "看表结构不算查询失败"
    # 第一稿照旧引用了不存在的 Q1：重写请求里同一句要求
    assert len(seen) == 2 and "这次一次成功的查询都没有" in str(seen[1][-1].content)
    assert run.output["答案"] == "没有查到订单数：数据源连接失败，请检查数据源连接。"
    [warned] = [e for e in await events(run.id, "log") if (e.data or {}).get("code") == "report_queries_failed"]
    assert warned.data["failed"] == 1 and warned.data["any_query"] is False


async def test_without_failed_queries_the_prompt_is_unchanged(monkeypatch):
    seen = script(monkeypatch, "没有数据。")
    calls = "[{'tool': 'db_schema__shop', 'args': {}, 'ok': True, 'result': '表 orders …'}]"
    run = await finish(graph(calls))
    assert "没有查到的数据" not in _prompt(seen[0])
    assert not [e for e in await events(run.id, "log") if (e.data or {}).get("code") == "report_queries_failed"]
