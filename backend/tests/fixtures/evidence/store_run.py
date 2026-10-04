"""合成的「4 个专项 Agent 并行查库 → 报告」场景：查询多、分布不均，每个 Agent 都交了按出处核对的字段。

形状仿一次真实运行（4 个 Agent 并行、查询次数悬殊，合计上百次），次数和内容全部虚构：门店、会员、卡券、活动。
不连库、不调模型：台账条目、查询快照、表结构快照、Agent 的 data / data_evidence 都在内存里拼好，快照经 loader 取。

台账按节点跑完的先后排（state["evidence"] 是各节点结束时追加的），所以全局编号 Q1… 前 26 个都是会员专项的。
"""
from __future__ import annotations

from typing import Any

from app.engine.evidence import cell_eid, make_eid

#: (节点 id, 画布上的名字, 查询次数, 查的表)
AGENTS: list[tuple[str, str, int, str]] = [
    ("agent_member", "会员专项", 26, "member_daily"),
    ("agent_store", "门店专项", 17, "store_daily"),
    ("agent_coupon", "卡券核销", 79, "coupon_redemption"),
    ("agent_campaign", "活动内容", 42, "campaign_view"),
]
COLUMNS = ["store_name", "biz_time", "cnt", "amount"]
TYPES = {"store_name": "text", "biz_time": "datetime", "cnt": "number", "amount": "number"}
ROWS = 30
#: 结构化抽取失败（两次都被截断）的那个 Agent：字段全是空值
EMPTY_AGENT = "agent_coupon"
FIELDS = ["total_cnt", "top_store", "latest_time", "top_stores", "churn_rate"]


def artifact_of(node_id: str, i: int) -> str:
    return f"{node_id}-q{i:03d}".ljust(64, "0")


def snapshot(node_id: str, i: int, rows: int = ROWS) -> dict[str, Any]:
    """第 i 次查询的快照：每行一个门店，时间是 ISO 写法（快照里存的就是这样）。"""
    seed = sum(map(ord, node_id)) + i * 7
    return {
        "columns": list(COLUMNS),
        "column_types": dict(TYPES),
        "rows": [[f"门店{(seed + r) % 90 + 1:02d}", f"2026-06-{r % 28 + 1:02d}T08:{(seed + r) % 60:02d}:39",
                  (seed * 13 + r * 17) % 900 + 10, f"{(seed * 31 + r * 7) % 9000 + 100}.50"] for r in range(rows)],
        "truncated": False,
    }


def schema_snapshot(table: str) -> dict[str, Any]:
    return {"source": "shop", "tables": {table: {"columns": [{"name": c, "type": TYPES[c]} for c in COLUMNS]}}}


def build() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, str]]:
    """(台账, 快照 {工件 id: 内容}, state["nodes"], {节点 id: 名字})。"""
    ledger: list[dict[str, Any]] = []
    store: dict[str, Any] = {}
    nodes: dict[str, Any] = {}
    titles: dict[str, str] = {}
    for node_id, title, count, table in AGENTS:
        titles[node_id] = title
        schema_id = f"schema-{table}".ljust(64, "0")
        store[schema_id] = schema_snapshot(table)
        ledger.append({"kind": "schema", "node_id": node_id, "exec": 1, "artifact": schema_id, "source": "shop",
                       "tables": {table: list(COLUMNS)}})
        for i in range(1, count + 1):
            aid = artifact_of(node_id, i)
            store[aid] = snapshot(node_id, i)
            ledger.append({"kind": "query", "node_id": node_id, "exec": 1, "artifact": aid,
                           "via": f"tool-{aid}"[:64], "call_id": f"call_{i}", "tool": "db_query__shop", "source": "shop",
                           "columns": list(COLUMNS), "rows": ROWS, "truncated": False,
                           "schema_artifact": schema_id, "tables": [table]})
        nodes[node_id] = agent_output(node_id, store)
    return ledger, store, nodes, titles


def agent_output(node_id: str, store: dict[str, Any]) -> dict[str, Any]:
    """Agent 交出的结构化字段：本节点的 Q3 那一格、Q5 的前 5 行；EMPTY_AGENT 抽取失败，字段全空。"""
    if node_id == EMPTY_AGENT:
        reason = "结构化抽取没有完成：模型的输出额度在推理阶段就用完了（最后一次的额度为 32768 token），结构化结论没能写出来"
        return {"text": "查到了大量核销和兑换记录。", "data": {f: None for f in FIELDS},
                "data_evidence": {f: {"status": "unresolved", "reason": reason} for f in FIELDS}}
    q3, q5 = artifact_of(node_id, 3), artifact_of(node_id, 5)
    first = store[q3]["rows"][0]

    def cell(column: str, index: int) -> dict[str, Any]:
        return {"ref": f"Q3.r0.{column}", "call": "Q3", "artifact": q3, "via": f"tool-{q3}"[:64],
                "locator": {"row": 0, "column": column}, "eid": cell_eid(q3, 0, column), "status": "verified"}

    rows_locator = {"rows": [0, 4], "column": "store_name"}
    return {
        "text": "结论见字段。",
        "data": {"total_cnt": first[2], "top_store": first[0], "latest_time": first[1],
                 "top_stores": [r[0] for r in store[q5]["rows"][:5]], "churn_rate": None},
        "data_evidence": {
            "total_cnt": cell("cnt", 2), "top_store": cell("store_name", 0), "latest_time": cell("biz_time", 1),
            "top_stores": {"ref": "Q5.r0-4.store_name", "call": "Q5", "artifact": q5, "via": f"tool-{q5}"[:64],
                           "locator": rows_locator, "eid": make_eid("rows", q5, rows_locator), "status": "verified"},
            "churn_rate": {"ref": None, "status": "missing"},
        },
    }


def global_alias(node_id: str, local: int) -> str:
    """本节点第 local 次查询在报告目录里的全局编号（台账按节点先后排）。"""
    offset = 0
    for nid, _, count, _ in AGENTS:
        if nid == node_id:
            return f"Q{offset + local}"
        offset += count
    raise KeyError(node_id)
