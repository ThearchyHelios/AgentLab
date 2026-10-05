"""双盲复算测试用的虚构零售库和盲算模型的剧本。表名、数字、地名都是编的。

订单表埋了两个陷阱：退款行（status = 'refunded'），以及下单日和支付日不在同一周的单。2026-09-01 至 2026-09-07 这一周：

- 按支付日、排除退款（口径说明要的）：1、2、5 号单，330.0
- 按下单日、排除退款：2、4、5 号单，310.0
- 按支付日、不排除退款：1、2、3、5 号单，380.0
- 按下单日、不排除退款：2、3、4、5 号单，360.0
- 付款客户数（按支付日、排除退款）：按行计数 3，去重计数 2
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from app.data import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource

ROWS = [
    (1, "华东", 101, 100.0, "paid", "2026-08-31", "2026-09-01"),
    (2, "华东", 102, 200.0, "paid", "2026-09-02", "2026-09-02"),
    (3, "华南", 101, 50.0, "refunded", "2026-09-03", "2026-09-03"),
    (4, "华南", 103, 80.0, "paid", "2026-09-07", "2026-09-08"),
    (5, "华北", 102, 30.0, "paid", "2026-09-05", "2026-09-05"),
]
WEEK = "BETWEEN '2026-09-01' AND '2026-09-07'"
#: 口径说明要的写法：按支付日、排除退款
RIGHT = f"SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {WEEK}"
#: 只错在时间列：按下单日
BY_ORDER_DATE = f"SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND order_date {WEEK}"
#: 只错在没排除退款
WITH_REFUNDS = f"SELECT SUM(amount) AS gmv FROM orders WHERE paid_date {WEEK}"
#: 两处都错
BOTH_WRONG = f"SELECT SUM(amount) AS gmv FROM orders WHERE order_date {WEEK}"
#: 付款客户数：按行计数 / 去重计数
BUYERS = f"SELECT COUNT(customer_id) AS buyers FROM orders WHERE status <> 'refunded' AND paid_date {WEEK}"
BUYERS_DISTINCT = (f"SELECT COUNT(DISTINCT customer_id) AS buyers FROM orders "
                   f"WHERE status <> 'refunded' AND paid_date {WEEK}")
#: 按地区分组：华东一行是 300.0（1、2 号单）
BY_REGION = (f"SELECT region, SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {WEEK} "
             "GROUP BY region ORDER BY region")
DEFINITION = "已支付订单的金额合计：按支付日期（paid_date）归属到当周，不含退款"


def build(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, customer_id INTEGER, amount REAL, status TEXT, "
        "order_date TEXT, paid_date TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    return path


async def add_source(path: Path, *, options: dict | None = None) -> tuple[str, str]:
    """登记成只读的手工源，探查好表结构。返回 (数据源名, id)。"""
    name = f"dc_shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options=dict(options or {}))
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        return name, row.id


def _text(messages) -> str:
    return "\n".join(m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
                     for m in messages)


class Blind:
    """盲算模型的剧本，也管图里别的模型调用。

    plans：{任务里出现的指标名: (SQL, 要交的列) 或 [SQL, …]、列}。盲算循环里依次查这几条 SQL，收尾时交最后一条查询的
    第 0 行那一列（row 可改）。没对上指标名的盲算交 null。不是盲算的调用（报告撰写）返回 writer 的正文并记下收到的消息。
    calls 记下每次盲算调用（step / final），tasks 记下盲算收到的任务原文。
    """

    def __init__(self, monkeypatch, source: str, plans: dict, *, writer: str = "本周销售额见口径卡。", row: int = 0):
        from app.providers import mock_model

        self.source = source
        self.plans = plans
        self.writer = writer
        self.row = row
        self.calls: list[str] = []
        self.tasks: list[str] = []
        self.writer_prompts: list[str] = []
        blind = self

        def decide(model, messages):
            text = _text(messages)
            plan = blind.plan_of(text)
            if model.response_format:
                blind.calls.append("final")
                done = len(re.findall(r"【证据 Q\d+】", text))
                if plan is None or not done:
                    return AIMessage(content=json.dumps({"value": None, "rationale": "没有查到。"}, ensure_ascii=False))
                _, column = plan
                alias = f"Q{len(blind.sqls(plan))}"
                body = {"value": {"alias": alias, "row": blind.row, "column": column},
                        "rationale": f"按口径说明统计，结果是 [[v:{alias}.r{blind.row}.{column}]]。"}
                return AIMessage(content=json.dumps(body, ensure_ascii=False))
            if model.tools:
                blind.calls.append("step")
                # 盲算的任务是第一条用户消息；同一次盲算的几轮收到的是同一条，只记一次
                task_text = next((str(m.content) for m in messages if m.type == "human"), "")
                if not blind.tasks or blind.tasks[-1] != task_text:
                    blind.tasks.append(task_text)
                done = sum(isinstance(m, ToolMessage) for m in messages)
                sqls = blind.sqls(plan) if plan is not None else []
                if done < len(sqls):
                    call = {"name": f"db_query__{blind.source}", "args": {"sql": sqls[done]},
                            "id": f"blind-{uuid.uuid4().hex[:6]}"}
                    return AIMessage(content="查一下。", tool_calls=[call])
                return AIMessage(content="查完了。")
            blind.writer_prompts.append(text)
            return AIMessage(content=blind.writer)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)

    def plan_of(self, text: str):
        for name, plan in self.plans.items():
            if f"指标：{name}" in text:
                return plan
        return None

    @staticmethod
    def sqls(plan) -> list[str]:
        sql = plan[0]
        return list(sql) if isinstance(sql, (list, tuple)) else [sql]

    @property
    def runs(self) -> int:
        """盲算 agent 一共执行了几次（每次收尾交一次结果）。"""
        return self.calls.count("final")
