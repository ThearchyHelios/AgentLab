"""自由形 freeform（§3.3）：从已有的运行（问数据的一轮回答，经 from-run 提取）或已发布的工作流版本导入，图原样用，
没有关节。蓝图有目的、谁能看、多久更新、谁来做四张卡片；要改图只能回画布（搭建者）。

「谁来做」（docs/design/next-round/05-已知遗留.md (d)）：骨架就是导入的图，卡片只填「谁」这个关节——按图列出用到的岗位
（人工节点派给谁、超时升级给谁、写入的发送确认派给谁，people_rows），写入能力没登记查询时再加一行「写入结果不明时谁去核实」。
只定由谁做：会签、时限、超时在画布上改（改了会改图，图哈希就和发布的那一版对不上）。绑定存在 spec.people[]（Step.id 就是
岗位名、只写 who，_common.slot_of / bindings_of 原样生效），核实人存在 /writeback/reconciler。以前这张卡片不存在，第一次
发布挡在 app.slot_unbound / app.reconciler_unbound 上，白话却叫人去「谁来做」里选人。

图由实例化的上下文给（graph，导入时存在蓝图上）。应用骨架按 apps/derive.py 的派生规则：有人工节点或写入的按流程，
否则按看板；写入节点按能力登记的进程内快照认（和运行时同一个判断，capabilities.registry.writes_externally）。
"""
from __future__ import annotations

import copy
from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "freeform"
VERSION = 1


def imported(bp: dict[str, Any]) -> dict[str, Any]:
    graph = c.context(bp).get("graph")
    return copy.deepcopy(graph) if isinstance(graph, dict) else {"nodes": [], "edges": []}


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    return imported(bp), []


def _writes(graph: dict[str, Any]) -> dict[str, str]:
    from app.apps.tree import node_map
    from app.capabilities.registry import cached_view, writes_externally

    out: dict[str, str] = {}
    for nid, n in node_map(graph).items():
        tool = str(n["config"].get("tool") or "")
        view = cached_view(tool) if n["type"] == "tool" and tool else None
        if view is not None and writes_externally(tool, view.get("effect"), view.get("endpoint_id")):
            out[nid] = tool
    return out


#: 「谁来做」一行里每处用途怎么说（apps/tree.SLOT_ROLES，加上核实）
USE_TEXT = {"assignee": "处理", "escalation": "超时升级", "confirm": "发送前确认", "reconcile": "写入结果不明时去核实"}
#: 不按岗位派的步骤（只读行）：发起人自己、按表达式算出来的人、等外部系统回调
FIXED_TEXT = {"initiator": "由发起人自己处理", "expression": "按图里的表达式算出由谁处理（在画布上改）",
              "hook": "等外部系统回调，不派给人"}
#: 核实人那一行的名字
RECONCILER_LABEL = "写入结果不明时谁去核实"


def people_rows(bp: dict[str, Any]) -> list[dict[str, Any]]:
    """按导入的图列出「谁来做」：[{kind, slot, label, detail, path, who, uses}]。

    kind=slot：图里引用的一个岗位（同一个岗位用在几处合成一行，按节点出现的顺序），path 是 /people/<i>/who（i 是岗位行的
    序号），who 是卡片上绑的人或组；kind=reconciler：写入能力没登记查询时的核实人，path 是 /writeback/reconciler；
    kind=initiator / expression / hook：不按岗位派的步骤，只读（path 为空）。label 用节点在图上的名字拼（岗位名是画布上
    随手写的 id），detail 写全每一处用途。界面照这一份画，不自己从图里推。"""
    from app.apps.derive import RECONCILER
    from app.apps.tree import node_map, slot_refs

    graph = imported(bp)
    writes = _writes(graph)
    nodes = node_map(graph)
    bound = {str(step.get("id")): c.value(step.get("who")) for step in bp.get("people") or [] if isinstance(step, dict)}
    rows: list[dict[str, Any]] = []
    by_slot: dict[str, dict[str, Any]] = {}
    for ref in slot_refs(graph, writes):
        row = by_slot.get(ref["slot"])
        if row is None:
            row = by_slot[ref["slot"]] = {"kind": "slot", "slot": ref["slot"], "path": f"/people/{len(by_slot)}/who",
                                          "who": bound.get(ref["slot"]), "uses": []}
            rows.append(row)
        row["uses"].append({"node_id": ref["node_id"], "title": ref["title"], "role": ref["role"]})
    unchecked = [(nid, cap) for nid, cap in writes.items() if not c.write_lookup(cap)]
    if unchecked:
        rows.append({"kind": "reconciler", "slot": RECONCILER, "path": "/writeback/reconciler",
                     "who": c.value((bp.get("writeback") or {}).get("reconciler")),
                     "uses": [{"node_id": nid, "title": nodes[nid]["title"], "role": "reconcile"} for nid, _ in unchecked]})
    for nid, n in nodes.items():
        if n["type"] != "human" and nid not in writes:
            continue
        assignee = n["config"].get("assignee")
        kind = assignee.get("kind") if isinstance(assignee, dict) else None
        if kind in FIXED_TEXT:
            rows.append({"kind": kind, "slot": None, "path": None, "who": None,
                         "uses": [{"node_id": nid, "title": n["title"],
                                   "role": "assignee" if n["type"] == "human" else "confirm"}]})
    for row in rows:
        titles = list(dict.fromkeys(u["title"] for u in row["uses"]))
        row["label"] = RECONCILER_LABEL if row["kind"] == "reconciler" else "、".join(f"「{t}」" for t in titles)
        row["detail"] = FIXED_TEXT[row["kind"]] if row["kind"] in FIXED_TEXT else "；".join(
            f"「{u['title']}」{USE_TEXT[u['role']]}" for u in row["uses"])
    return rows


def needs_reconciler(bp: dict[str, Any]) -> bool:
    """导入的图里有写入能力没登记查询（同 app_skeleton 加核实岗位的判断）。"""
    return any(not c.write_lookup(cap) for cap in _writes(imported(bp)).values())


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from app.apps.derive import ENTRY, INSTANCE_VIEW, PIN, VIEW, _entries, _results
    from app.apps.forms import input_schema
    from app.apps.tree import input_fields, node_map

    name = c.app_name(bp, "应用")
    nodes = node_map(graph)
    writes = _writes(graph)
    process = bool(writes) or any(n["type"] == "human" for n in nodes.values())
    spec = c.app_base(bp, graph, use="process" if process else "view", name=name,
                      description=str(c.value(bp.get("purpose")) or ""), writes=writes)
    fields = input_fields(graph)
    if fields:
        spec["forms"][ENTRY] = input_schema(fields)
        names = list(spec["forms"][ENTRY]["properties"])
        spec["entries"].append({"id": ENTRY, "label": f"发起{name}" if process else "按条件运行", "pin": PIN,
                                "form": {"$ref": f"#/forms/{ENTRY}"}, "map": {n: f"form.{n}" for n in names}})
        spec["views"][INSTANCE_VIEW] = {"source": {"kind": "instance"}}
    if not process:
        spec["views"][VIEW] = c.latest_view(c.context(bp))
    spec["pages"] = [{"id": "home", "title": name, "body": ([] if process else _results(nodes, VIEW, "home"))
                      + _entries(spec)}]
    if spec["entries"]:
        spec["pages"].append(c.instance_page(graph, writes))
    return c.finish_app(spec)


def schedule(bp: dict[str, Any]) -> dict[str, Any] | None:
    """导入的图入口字段正好是统计期三个字段时按「多久更新」填；别的字段没法替人定，只建任务、不填输入。"""
    from app.apps.tree import input_fields

    names = {f["name"] for f in input_fields(imported(bp))}
    inputs = None if set(c.PERIOD_FIELDS) <= names else {}
    return c.schedule_of(bp, c.app_name(bp, "应用"), inputs=inputs)


def example() -> dict[str, Any]:
    item = c.user_item

    return {"format": "agentlab.blueprint/1", "purpose": item("把这次问数据的回答做成每周自动出的看板"),
            "template": {"id": ID, "version": VERSION}, "data": [], "cadence": item(c.default_cadence("weekly")),
            "people": [], "fields": [], "writeback": None,
            "access": {"use": [], "observe": [], "manage": []}, "asks": []}


def example_graph() -> dict[str, Any]:
    """示例导入的图：统计期 → 查库 → 口径卡 → 按指标出具（测试用）。"""
    return {"nodes": [
        c.node(c.START, "input", "统计期", fields=c.period_fields()),
        c.node("q", "tool", "查订单", tool="db_query__shop", args={
            "sql": "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at BETWEEN '{{ input.period_start }}'"
                   " AND '{{ input.period_end }}'"}),
        c.node(c.CALIBER, "metrics", "口径卡", caliber="周报口径", caliber_version="v1",
               metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.q, 0, 'gmv')"}]),
        c.node(c.DONE, "output", "出具", fields=[{"name": "销售额（元）", "value": "{{ nodes.caliber.metrics[0].value }}"}],
               contract={"metrics_from": [c.CALIBER], "required": ["gmv"], "strict": True}),
    ], "edges": [c.edge(c.START, "q"), c.edge("q", c.CALIBER), c.edge(c.CALIBER, c.DONE)]}


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="自由形",
    description="从已有的开始：一次问数据的回答，或者一个已发布的工作流，原样做成应用；改图要回画布。",
    applies_when="用户已经有一次满意的问数据回答，或者一个已发布的工作流，想直接做成应用；需求在五个场景之外时也用它。",
    cards={"purpose": "必填：一句话说清这个应用做什么",
           "cadence": "可选：要不要定期自动出数",
           "people": "可选：只定每一步由谁做；会签、时限、超时在画布上改",
           "access": "可选：谁能用、谁能看全部、谁能管理"},
    personal="导入的图满足问数据的规则（只读、没有人工节点和写入）时可以",
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=schedule, tags=["导入"],
    example=example, example_fill=dict, imports_graph=True,
))
