"""景区业务库（scenic.py）的一份数据目录，给 SQL 检查（data/sqlcheck.py）的测试用。

只写检查要用到的几张表：订单与订单明细（一对多，外键）、入园记录（有效记录条件、码值、业务日期）、
库存快照（存量）、会员等级（比率）、停车记录（两个时间列，业务日期按出场时间）、渠道（文字码值）。
支付记录、会员这些表故意不写目录：关系要靠表结构现推（外键 verified、命名推断 proposed）。

notes(status) 里「检查拿来当依据的项」一律用给定的状态：confirmed / verified / proposed / rejected。
来源跟着状态走（confirmed 记人工填写，verified 记外键或数据剖析，其余记模型起草），和真实目录里的组合一致。
单独改某一项的状态用 override：{("orders", "columns.total_amount.measure"): "proposed"} 这样的路径。
表名、数据全部虚构。
"""
from __future__ import annotations

import copy
from typing import Any

from app.data import catalog
from app.data.catalog import make_item

_SOURCE_OF = {"confirmed": "human", "verified": "profile", "proposed": "llm", "rejected": "llm"}


def _item(value: Any, status: str) -> dict[str, Any]:
    return make_item(value, _SOURCE_OF[status], status)


def _rel(table: str, columns: list[str], to_table: str, to_columns: list[str], cardinality: str,
         status: str) -> dict[str, Any]:
    source = {"confirmed": "human", "verified": "fk"}.get(status, "name")
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns, "to_table": to_table,
            "to_columns": to_columns, "cardinality": cardinality, "coverage": None, "source": source,
            "status": status}


#: 订单明细 → 订单 这条关系的编号（断言 relation_id 用）
ORDER_ITEMS_TO_ORDERS = catalog.relation_id("order_items", ["order_id"], "orders", ["id"])


def notes(status: str = "confirmed", override: dict[tuple[str, str], str] | None = None) -> dict[str, dict[str, Any]]:
    """{表名: notes}。名称类的项（中文名）一律人工确认，只有检查的依据随 status 变。"""
    s = status
    out: dict[str, dict[str, Any]] = {
        "orders": {
            "label": _item("订单", "confirmed"),
            "grain": _item("每笔订单一行", s),
            "keys": _item(["id"], s),
            "kind": _item("fact", s),
            "business_date": _item({"column": "ordered_at", "rule": "按下单时间计"}, s),
            "valid_filter": _item("status = 1", s),
            "columns": {
                "total_amount": {"label": _item("订单金额", "confirmed"), "unit": _item("元", s),
                                 "measure": _item("flow", s)},
                "status": {"label": _item("订单状态", "confirmed"), "measure": _item("status", s),
                           "codes": _item({"1": "已支付", "2": "已退款"}, s)},
                "ordered_at": {"label": _item("下单时间", "confirmed")},
            },
        },
        "order_items": {
            "label": _item("订单明细", "confirmed"),
            "grain": _item("每笔订单的每个票种一行", s),
            "keys": _item(["id"], s),
            "columns": {
                "amount": {"label": _item("明细金额", "confirmed"), "measure": _item("flow", s)},
                "qty": {"label": _item("张数", "confirmed"), "measure": _item("flow", s)},
            },
            "relations": [_rel("order_items", ["order_id"], "orders", ["id"], "many_to_one", s)],
        },
        "visits": {
            "label": _item("入园记录", "confirmed"),
            "keys": _item(["id"], s),
            "business_date": _item({"column": "visit_time", "rule": "按检票时间计"}, s),
            "valid_filter": _item("status = 1", s),
            "columns": {
                "visitor_count": {"label": _item("入园人数", "confirmed"), "measure": _item("flow", s)},
                "status": {"label": _item("检票状态", "confirmed"), "codes": _item({"1": "有效", "0": "作废"}, s)},
                "visit_time": {"label": _item("检票时间", "confirmed")},
            },
            "relations": [_rel("visits", ["park_id"], "parks", ["id"], "many_to_one", s)],
        },
        "inventory_snapshots": {
            "label": _item("库存快照", "confirmed"),
            "kind": _item("snapshot", s),
            "business_date": _item({"column": "snapshot_date", "rule": "每天营业结束时盘点"}, s),
            "columns": {
                "on_hand": {"label": _item("在库数量", "confirmed"), "measure": _item("stock", s)},
                "snapshot_date": {"label": _item("盘点日期", "confirmed")},
            },
        },
        "member_levels": {
            "label": _item("会员等级", "confirmed"),
            "columns": {"discount_rate": {"label": _item("折扣率", "confirmed"), "measure": _item("ratio", s)}},
        },
        "parking_records": {
            "label": _item("停车记录", "confirmed"),
            "business_date": _item({"column": "exited_at", "rule": "按出场时间计费"}, s),
            "columns": {
                "fee": {"label": _item("停车费", "confirmed"), "measure": _item("flow", s)},
                "entered_at": {"label": _item("入场时间", "confirmed")},
                "exited_at": {"label": _item("出场时间", "confirmed")},
            },
        },
        "channels": {
            "label": _item("渠道", "confirmed"),
            "columns": {"channel_type": {"label": _item("渠道类型", "confirmed"),
                                         "codes": _item({"线上直销": "官网和小程序", "线下": "窗口售票",
                                                         "分销": "旅行社和第三方平台"}, s)}},
        },
    }
    for (table, path), wanted in (override or {}).items():
        out[table] = set_status(out[table], path, wanted)
    return out


def set_status(table_notes: dict[str, Any], path: str, status: str) -> dict[str, Any]:
    """改一项的状态（不改入参）。path：表级字段名、columns.<列>.<字段>，或 relations.<编号>。"""
    notes_ = copy.deepcopy(table_notes)
    parts = path.split(".")
    if parts[0] == "relations":
        target = next(r for r in notes_["relations"] if r["id"] == parts[1])
    elif parts[0] == "columns":
        target = notes_["columns"][parts[1]][parts[2]]
    else:
        target = notes_[parts[0]]
    target["status"] = status
    target["source"] = {"confirmed": "human", "verified": "fk"}.get(status, "llm") if parts[0] == "relations" \
        else _SOURCE_OF[status]
    return notes_
