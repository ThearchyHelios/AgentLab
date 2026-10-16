"""从工作流版本确定性派生应用（A8）：不靠模型，助手失败了也有可用的应用（P3）。

入口字段 → 入口表单；人工节点 → 待办；图里引用到的岗位（派给谁、超时升级给谁、动作确认派给谁，tree.slot_refs）→
岗位，留空等人绑定；口径卡 → 指标块；报告 → 报告块；取数节点 → 表格；写入的调用工具节点 → 动作确认待办 + 动作状态。

看板（use=view）的首页看「最近封存运行」，视图优先绑定已有的定时任务（同一工作流、正式运行、启用着的）：数从定时任务的
封存运行来，打开页面不发起运行。流程（use=process）的首页只放发起和我发起的：每件事的结果在它自己的实例页看，首页没有
「最近一次」可看——派生出来只会挂一块永远空着的印章。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.forms import input_schema
from app.apps.spec import APP_FORMAT, parse_spec
from app.apps.tree import input_fields, node_map, slot_refs

PIN = "main"
VIEW = "latest"
INSTANCE_VIEW = "inst"
ENTRY = "start"
RECONCILER = "reconciler"
#: 派生不了时的机器码：工作流、版本不存在（404）
WORKFLOW_GONE = "workflow_not_found"
VERSION_GONE = "workflow_version_not_found"


class DeriveRefused(Exception):
    """派生不了。code 是给程序认的机器码，接口照它回。"""

    def __init__(self, status_code: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


async def derive_app(session: AsyncSession, workflow_id: str, version: int, *, name: str | None = None) -> dict[str, Any]:
    """返回一份 agentlab.app/1 spec（未发布的草稿）。"""
    from app.apps.pins import PinState
    from app.apps.versions import version_row
    from app.db.models import Schedule, Workflow

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise DeriveRefused(404, "工作流不存在，可能已被删除", WORKFLOW_GONE)
    row = await version_row(session, workflow_id, version)
    if row is None:
        raise DeriveRefused(404, f"「{workflow.name}」不存在 v{version} 版本", VERSION_GONE)
    state = PinState(alias=PIN, pin={"workflow_id": workflow_id, "version": version}, workflow=workflow, row=row)
    schedule = (await session.execute(select(Schedule).where(
        Schedule.workflow_id == workflow_id, Schedule.run_class == "formal", Schedule.enabled.is_(True))
        .order_by(Schedule.created_at))).scalars().first()
    return await _derive(session, row.graph or {}, title=workflow.name, name=name,
                         description=workflow.description or "",
                         pin={"alias": PIN, "workflow_id": workflow_id, "version": version, "graph_hash": state.hash,
                              "level": state.level},
                         schedule_id=schedule.id if schedule else None)


async def derive_from_graph(session: AsyncSession, graph: dict[str, Any], *, name: str, description: str = "",
                            workflow_id: str | None = None, blueprint_id: str | None = None,
                            schedule_id: str | None = None) -> dict[str, Any]:
    """从一张还没发布的图派生应用草稿（docs/design/normal-user-apps.md N4：蓝图的试运行、发布单建应用之前）。派生规则和
    derive_app 同一份，只是钉住项没有版本号（version 为空）、graph_hash 取这张图的哈希。挂在工作流上的写 workflow_id，
    还没有工作流的（蓝图刚实例化出来的图、个人应用）写 blueprint。schedule_id 给了，看板的视图绑这个定时任务。

    这样的草稿发布不了（应用检查报 app.pin_unpublished）：发布单在工作流发布之后把钉住项换成那一版再建应用。"""
    from app.core.artifact_store import graph_hash

    pin: dict[str, Any] = {"alias": PIN, "version": None, "graph_hash": graph_hash(graph), "level": None}
    pin.update({"workflow_id": workflow_id} if workflow_id else {"blueprint": blueprint_id})
    return await _derive(session, graph, title=name, name=name, description=description, pin=pin,
                         schedule_id=schedule_id)


async def _derive(session: AsyncSession, graph: dict[str, Any], *, title: str, name: str | None, description: str,
                  pin: dict[str, Any], schedule_id: str | None) -> dict[str, Any]:
    """派生规则本身：title 是工作流（或蓝图）的名字，写进首页标题、入口的说法；pin 是钉住项（还缺 use），
    schedule_id 是看板的视图要绑的定时任务（没有就看正式运行）。"""
    from app.apps.pins import write_nodes

    nodes = node_map(graph)
    fields = input_fields(graph)
    humans = {nid: n for nid, n in nodes.items() if n["type"] == "human"}
    # 写外部系统的调用工具节点：和运行时同一个认法（pins.write_nodes），只有它们开发送确认卡、结果未知时要人核实
    caps = await write_nodes(session, graph)
    writes = {nid: nodes[nid] for nid in caps}
    lookups = all(getattr(cap, "lookup", None) for cap in caps.values())
    process = bool(humans or writes)
    spec: dict[str, Any] = {
        "format": APP_FORMAT, "name": name or title, "description": description,
        "icon": "app",
        "pins": [{**pin, "use": "process" if process else "view"}],
        "slots": {}, "entries": [], "forms": {}, "task_ui": {}, "views": {}, "pages": [], "hooks": [], "notify": [],
    }
    for ref in slot_refs(graph, writes):
        spec["slots"].setdefault(ref["slot"], {"label": ref["slot"]})
    for nid, node in humans.items():
        spec["task_ui"][nid] = {"title": node["title"]}
    if writes and not lookups:
        spec["slots"][RECONCILER] = {"label": "动作核实"}
    if fields:
        spec["forms"][ENTRY] = input_schema(fields)
        names = list(spec["forms"][ENTRY]["properties"])
        first = next((n for n in names if n in spec["forms"][ENTRY]["required"]), names[0] if names else None)
        spec["entries"].append({
            "id": ENTRY, "label": f"发起{title}" if process else "按条件运行", "pin": PIN,
            "form": {"$ref": f"#/forms/{ENTRY}"}, "map": {n: f"form.{n}" for n in names},
            **({"instance_title": {"pattern": f"{title}·{{{first}}}", "fields": [first]}}
               if first and re.fullmatch(r"[A-Za-z0-9_㐀-鿿]+", first) else {}),
        })
    if not process:
        spec["views"][VIEW] = {"source": {"kind": "latest_sealed", "pin": PIN,
                                          "from": f"schedule:{schedule_id}" if schedule_id else "formal"}}
    if spec["entries"]:
        spec["views"][INSTANCE_VIEW] = {"source": {"kind": "instance"}}
    spec["pages"].append({"id": "home", "title": title,
                          "body": ([] if process else _results(nodes, VIEW, "home")) + _entries(spec)})
    if spec["entries"]:
        spec["pages"].append({"id": "instance", "title": "实例", "body": [
            {"id": "inst_header", "type": "instance_header"},
            {"id": "inst_timeline", "type": "instance_timeline"},
            *[c for nid in humans for c in (
                {"id": f"task_{_safe(nid)}", "type": "task_form", "bind": {"task": nid}},
                {"id": f"signoffs_{_safe(nid)}", "type": "signoffs", "bind": {"task": nid}})],
            *[{"id": f"action_{_safe(nid)}", "type": "action_status", "props": {"label": node["title"][:200]},
               "bind": {"action": nid}} for nid, node in writes.items()],
            *_results(nodes, INSTANCE_VIEW, "inst"),
        ]})
    # 形状对不上是派生规则的错，不是用户的：这里直接让它报出来
    return parse_spec(spec).model_dump(mode="json", by_alias=True)


def _results(nodes: dict[str, dict[str, Any]], view: str, prefix: str) -> list[dict[str, Any]]:
    """看结果的那几块：印章、口径卡的指标（分栏）、报告、取数节点的表格。每一块怎么绑定见 _metric_block 等，页面编辑的
    「加一张数据卡片」（data_candidates）用的是同一份规则。"""
    body: list[dict[str, Any]] = [{"id": f"{prefix}_stamp", "type": "stamp", "bind": {"view": view}}]
    for nid, node in nodes.items():
        metrics = _card_metrics(node)
        if not metrics:
            continue
        body.append({"id": f"{prefix}_kpis_{_safe(nid)}", "type": "section", "props": {"title": node["title"][:200]},
                     "children": [{"id": f"{prefix}_cols_{_safe(nid)}", "type": "columns",
                                   "props": {"count": min(len(metrics), 4)},
                                   "children": [_metric_block(nid, m, view, prefix) for m in metrics]}]})
    for nid, node in nodes.items():
        if node["type"] == "report":
            body.append(_report_block(nid, node, view, prefix))
    for nid, node in nodes.items():
        if _query_node(node):
            body.append(_table_block(nid, node, view, prefix))
    return body


def _card_metrics(node: dict[str, Any]) -> list[dict[str, Any]]:
    """口径卡上声明了 id 的指标；不是口径卡的是空的。"""
    if node["type"] != "metrics":
        return []
    return [m for m in node["config"].get("metrics") or [] if isinstance(m, dict) and m.get("id")]


def _query_node(node: dict[str, Any]) -> bool:
    """取数节点：调用工具节点绑的是数据源查询。"""
    return node["type"] == "tool" and str(node["config"].get("tool") or "").startswith("db_query__")


def _metric_block(nid: str, metric: dict[str, Any], view: str, prefix: str) -> dict[str, Any]:
    return {"id": f"{prefix}_m_{_safe(nid)}_{_safe(str(metric['id']))}", "type": "metric",
            "props": {"label": str(metric.get("name") or metric["id"])[:200],
                      **({"unit": str(metric["unit"])[:200]} if metric.get("unit") else {})},
            "bind": {"view": view, "metric": f"{nid}.{metric['id']}"}}


def _series_block(nid: str, metric: dict[str, Any], view: str, prefix: str) -> dict[str, Any]:
    return {"id": f"{prefix}_s_{_safe(nid)}_{_safe(str(metric['id']))}", "type": "series",
            "props": {"label": str(metric.get("name") or metric["id"])[:200]},
            "bind": {"view": view, "series": f"{nid}.{metric['id']}"}}


def _report_block(nid: str, node: dict[str, Any], view: str, prefix: str) -> dict[str, Any]:
    return {"id": f"{prefix}_report_{_safe(nid)}", "type": "report", "props": {"title": node["title"][:200]},
            "bind": {"view": view, "report": nid}}


def _table_block(nid: str, node: dict[str, Any], view: str, prefix: str) -> dict[str, Any]:
    return {"id": f"{prefix}_table_{_safe(nid)}", "type": "table", "props": {"title": node["title"][:200]},
            "bind": {"view": view, "query": {"node": nid}}}


def data_candidates(nodes: dict[str, dict[str, Any]], view: str, prefix: str) -> list[dict[str, Any]]:
    """页面编辑「加一张数据卡片」能加的（docs/design/normal-user-apps.md N6）：[{kind, label, group, component}]，
    component 可以直接交给 app_add。规则和派生的结果块（_results）同一份——口径卡的每个指标、报告、取数节点的表格——
    另加每个指标的趋势（派生不放趋势：新应用只有一期数）。group 是出自哪个节点（口径卡、报告、取数的标题）。"""
    out: list[dict[str, Any]] = []
    for nid, node in nodes.items():
        for metric in _card_metrics(node):
            name = str(metric.get("name") or metric["id"])[:200]
            out.append({"kind": "metric", "label": name, "group": node["title"][:200],
                        "component": _metric_block(nid, metric, view, prefix)})
            out.append({"kind": "series", "label": f"{name}的趋势", "group": node["title"][:200],
                        "component": _series_block(nid, metric, view, prefix)})
    for nid, node in nodes.items():
        if node["type"] == "report":
            out.append({"kind": "report", "label": node["title"][:200], "group": node["title"][:200],
                        "component": _report_block(nid, node, view, prefix)})
    for nid, node in nodes.items():
        if _query_node(node):
            out.append({"kind": "table", "label": node["title"][:200], "group": node["title"][:200],
                        "component": _table_block(nid, node, view, prefix)})
    return out


def _entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in spec["entries"]:
        out.append({"id": f"btn_{_safe(entry['id'])}", "type": "entry_button", "props": {"entry": entry["id"]}})
        out.append({"id": f"list_{_safe(entry['id'])}", "type": "instance_list",
                    "props": {"entry": entry["id"], "who": "mine"}})
    return out
