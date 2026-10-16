"""评测执行测试共用的虚构样本：dual_check_shop 的订单库（tests/fixtures/dual_check_shop.py）、助手和报告撰写的剧本、建题集和
题目、等评测跑完。库、表、数字、问法都是编的。

助手剧本（Copilot）：先 look 一眼订单表，查看结果里看到数据目录写着「按支付日期归属」就按支付日写 SQL（330），没看到就按
下单日写（310）——目录开着和关着答得不一样，「目录的贡献」对照才量得出来。图是入口 → 调用工具（查库）→ 报告撰写 → 出口；
报告撰写的剧本引用查到的那一格（[[v:Q1.r0.gmv]]）。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import delete

from app.data import catalog
from app.data.catalog import make_item
from app.db.base import SessionLocal
from tests.fixtures import dual_check_shop as shop

#: 题目：9/1–9/7 按支付日、不含退款的销售额（期望 330）
QUESTION = "2026-09-01 至 2026-09-07 按支付日统计、不含退款的销售额是多少？"
PAID_RULE = "按支付日期归属"
NOTES = {"label": make_item("订单", "human"),
         "business_date": make_item({"column": "paid_date", "rule": PAID_RULE}, "human"),
         "valid_filter": make_item("status <> 'refunded'", "human")}
REPORT = "按支付日统计，销售额 [[v:Q1.r0.gmv]]。"


def _line(op: dict[str, Any]) -> AIMessageChunk:
    return AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


def graph_ops(source: str, sql: str) -> list[dict[str, Any]]:
    def node(nid: str, ntype: str, label: str, **config: Any) -> dict[str, Any]:
        return {"op": "add_node", "node": {"id": nid, "type": ntype, "label": label, "config": config}}

    def edge(a: str, b: str) -> dict[str, Any]:
        return {"op": "add_edge", "edge": {"source": a, "target": b}}

    return [node("start", "input", "输入"), node("q", "tool", "查订单", tool=f"db_query__{source}", args={"sql": sql}),
            edge("start", "q"), node("write", "report", "写答案", instructions="回答：{{ input.question }}"),
            edge("q", "write"),
            node("out", "output", "答案", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]),
            edge("write", "out"), {"op": "done", "explanation": "查一下", "run": True}]


class Copilot:
    """助手剧本（见模块说明）。mode：answer 照上面答；fail 每次都抛错（助手调用失败）；reply 直接回答；ask 反问；
    slow 每一步都慢（测超时）。calls 记下每次收到的整串消息。"""

    def __init__(self, source: str, *, mode: str = "answer", delay: float = 0.0) -> None:
        self.source = source
        self.mode = mode
        self.delay = delay
        self.calls: list[list] = []

    async def astream(self, messages):
        import asyncio

        self.calls.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "fail":
            raise RuntimeError("接入返回了 502")
        if "上线前自查" in messages[1][1]:
            yield _line({"op": "done"})
            return
        if self.mode == "reply":
            yield _line({"op": "reply", "text": "销售额是 330。"})
            return
        if self.mode == "ask":
            yield _line({"op": "plan", "summary": "先问清楚"})
            yield _line({"op": "ask", "question": "要按下单日还是支付日？", "options": ["下单日", "支付日"]})
            return
        if len(messages) == 2:
            yield _line({"op": "plan", "summary": "查销售额"})
            yield _line({"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]})
            return
        looked = str(messages[-1][1])
        sql = shop.RIGHT if PAID_RULE in looked else shop.BY_ORDER_DATE
        for op in graph_ops(self.source, sql):
            yield _line(op)


def use_copilot(monkeypatch, model: Any) -> None:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    monkeypatch.setattr(copilot, "_HEARTBEAT_SECONDS", 3600.0)


def use_report(monkeypatch, text: str = REPORT) -> None:
    """运行里的模型调用（报告撰写）一律答 text。"""
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def make_source(tmp_path, *, notes: dict[str, Any] | None = NOTES) -> tuple[str, str]:
    name, sid = await shop.add_source(shop.build(tmp_path / "shop.db"))
    if notes is not None:
        async with SessionLocal() as session:
            await catalog.write_entry(session, sid, "orders", notes, if_version=0, actor=None)
    return name, sid


async def set_notes(source_id: str, notes: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        current = (await catalog.read_catalogs(session, [source_id])).get(source_id, {}).get("orders")
        await catalog.write_entry(session, source_id, "orders", notes, if_version=current.version if current else 0,
                                  actor=None)


async def make_suite(source_id: str, *, name: str = "虚构零售题集", cases: list[dict[str, Any]] | None = None,
                     default_variant: dict[str, Any] | None = None, budget: dict[str, Any] | None = None) -> str:
    """直接写库建题集和题目（不走接口的试算：期望在评测里每次都重算）。cases 不给就是一道 330 的题。"""
    from app.db.models_eval import EvalCase, EvalSuite
    from app.eval.cases import case_hash, normalize_case

    cases = cases if cases is not None else [paid_case(source_id)]
    async with SessionLocal() as session:
        suite = EvalSuite(name=name, default_variant=default_variant or {}, budget=budget or {})
        session.add(suite)
        await session.flush()
        for seq, body in enumerate(cases, start=1):
            fields = normalize_case(body)
            session.add(EvalCase(suite_id=suite.id, seq=seq, case_hash=case_hash(fields), **fields))
        await session.commit()
        return suite.id


def paid_case(source_id: str, *, question: str = QUESTION, **extra: Any) -> dict[str, Any]:
    return {"question": question, "datasource_ids": [source_id],
            "gold": {"kind": "sql", "datasource_id": source_id, "sql": shop.RIGHT},
            "expectations": [{"kind": "scalar", "column": "gmv"}], **extra}


async def wait_eval(leader_id: str, timeout: float = 120.0) -> None:
    from app.eval import cleanup, runner
    from app.notify import service as notify

    await runner.manager.wait(leader_id, timeout=timeout)
    await cleanup.drain()
    await notify.drain()


async def drop_suites() -> None:
    from app.db.models_eval import EvalSuite

    async with SessionLocal() as session:
        await session.execute(delete(EvalSuite))
        await session.commit()
