"""周期看板 periodic_board（§3.3）：入口（统计期）→ 取数（每个库一个调用工具节点）→ 口径卡 → 成果（指标出具）。

- 生成关节：每个取数节点的 SQL、口径卡每个数的指标式（只许 cell() 取格加算术）；
- 确定性关节：统计期字段、定时任务（多久更新）、出具契约的必需指标（看哪些数）；复用已发布口径卡的项落成 caliber_from；
- 应用页：印章、指标块、复用的口径、趋势、明细表；「按条件查看」是按需视图（参数就是统计期）。

个人应用能用全部（只读、没有人工节点和写入）。
"""
from __future__ import annotations

from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "periodic_board"
VERSION = 1
#: 按条件查看的按需视图：同样的统计期一小时内用缓存，不重复查库
ON_DEMAND_TTL_S = 3600


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    groups, _ = c.data_groups(bp)
    caliber, metric_joints = c.caliber_node(bp, groups, c.app_name(bp, "周期看板"))
    reused = c.reuse_nodes(groups)
    fetches, joints = c.fetch_nodes(groups)
    nodes = [c.node(c.START, "input", "统计期", fields=c.period_fields()), *fetches]
    edges = [c.edge(c.START, f["id"]) for f in fetches]
    joints.append(Joint(id="start.fields", kind="fields", node_id=c.START, field="/fields", from_card="/cadence",
                        label="统计期"))
    if caliber is not None:
        nodes.append(caliber)
        joints += metric_joints
        edges += [c.edge(g.node_id, c.CALIBER) for g in groups if g.items]
        edges.append(c.edge(c.CALIBER, c.DONE))
    for card, _item, fetch in reused:
        nodes.append(card)
        edges += [c.edge(fetch, card["id"]), c.edge(card["id"], c.DONE)]
    done, done_joints = c.board_output(bp, caliber, reused)
    nodes.append(done)
    joints += done_joints
    return {"nodes": nodes, "edges": edges}, joints


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from app.apps.derive import PIN, VIEW, _results
    from app.apps.forms import input_schema
    from app.apps.tree import input_fields, node_map

    name = c.app_name(bp, "周期看板")
    spec = c.app_base(bp, graph, use="view", name=name, description=str(c.value(bp.get("purpose")) or ""))
    nodes = node_map(graph)
    spec["forms"][c.START] = input_schema(input_fields(graph))
    spec["views"][VIEW] = c.latest_view(c.context(bp))
    spec["views"]["custom"] = {"source": {"kind": "on_demand", "pin": PIN, "params": {"$ref": f"#/forms/{c.START}"},
                                          "ttl_s": ON_DEMAND_TTL_S}}
    spec["pages"] = [
        {"id": "home", "title": name, "body": [*_results(nodes, VIEW, "home"),
                                               *c.reused_section(bp, graph, VIEW, "home"),
                                               *c.series_section(graph, VIEW, "home")]},
        {"id": "custom", "title": "按条件查看", "body": [*_results(nodes, "custom", "custom"),
                                                         *c.reused_section(bp, graph, "custom", "custom")]},
    ]
    return c.finish_app(spec)


def schedule(bp: dict[str, Any]) -> dict[str, Any] | None:
    return c.schedule_of(bp, c.app_name(bp, "周期看板"))


def briefs(bp: dict[str, Any]) -> dict[str, str]:
    groups, _ = c.data_groups(bp)
    return c.data_briefs(bp, groups)


def _prefill(spec: dict[str, Any]) -> None:
    c.fill_cadence(spec, "weekly")


def problems(bp: dict[str, Any]) -> list[dict[str, Any]]:
    return c.data_groups(bp)[1]


def example() -> dict[str, Any]:
    """示例：每周一看上周的销售额、订单数和退款率（库 shop 的 orders 表）。"""
    item = c.user_item

    def data(item_id: str, label: str, unit: str, measure: str, filter_: str | None = None) -> dict[str, Any]:
        return {"id": item_id, "label": item(label), "unit": item(unit),
                "table": item({"source": "shop", "table": "orders"}), "measure": item(measure),
                "date": item({"column": "paid_at", "rule": "按支付日"}), "filter": item(filter_),
                "grain": item("全部门店"), "reuse": None}

    return {"format": "agentlab.blueprint/1", "purpose": item("每周一看上周各门店的销售额、订单数和退款率"),
            "template": {"id": ID, "version": VERSION},
            "data": [data("gmv", "销售额", "元", "sum(amount)", "不含已退款"), data("orders", "订单数", "单", "count(*)"),
                     data("refund_rate", "退款率", "%", "退款单数 / 订单数")],
            "cadence": item(c.default_cadence("weekly")), "people": [], "fields": [], "writeback": None,
            "access": {"use": [{"kind": "group", "name": "门店"}], "observe": [], "manage": []}, "asks": []}


def example_fill() -> dict[str, Any]:
    return {
        "sql_fetch_shop": ("SELECT SUM(CASE WHEN refunded = 0 THEN amount ELSE 0 END) AS gmv, COUNT(*) AS order_cnt,"
                           " SUM(CASE WHEN refunded = 1 THEN 1 ELSE 0 END) AS refund_cnt FROM orders"
                           " WHERE paid_at BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'"),
        "metric_gmv": "cell(nodes.fetch_shop, 0, 'gmv')",
        "metric_orders": "cell(nodes.fetch_shop, 0, 'order_cnt')",
        "metric_refund_rate": ("round(cell(nodes.fetch_shop, 0, 'refund_cnt')"
                               " / cell(nodes.fetch_shop, 0, 'order_cnt') * 100, 2)"
                               " if cell(nodes.fetch_shop, 0, 'order_cnt') else None"),
    }


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="周期看板",
    description="定期出一张看板：每天、每周或每月按同一个口径算几个数，打开就有数，能看趋势、能按条件再查。",
    applies_when="用户想定期看几个数（销售额、订单数、退款率这类），按天、周、月更新，不需要人审批、不写回别的系统。",
    cards={"purpose": "必填：一句话说清看什么、给谁看",
           "data": "必填：每一项对到一张表（或复用已发布的口径卡），写明怎么算、按哪个日期、筛选条件",
           "cadence": "必填：每天、每周还是每月，几点，统计哪一期",
           "access": "可选：谁能用、谁能看全部、谁能管理"},
    personal="全部可以做成个人应用（只读，自己点刷新）",
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=schedule, tags=["看板", "定时"],
    example=example, example_fill=example_fill, prefill=_prefill, briefs=briefs, problems=problems,
))
