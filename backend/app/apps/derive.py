"""从工作流版本确定性派生应用（A8）：不靠模型，助手失败了也有可用的应用（P3）。

入口字段 → 入口表单；人工节点 → 待办（岗位留空等人绑定）；口径卡 → 指标块；报告 → 报告块；取数节点 → 表格；
写入的调用工具节点 → 动作确认待办 + 动作状态。

视图优先绑定已有的定时任务（同一工作流、正式运行、启用着的）：看板的数从定时任务的封存运行来，打开页面不发起运行。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.forms import input_schema
from app.apps.spec import APP_FORMAT, parse_spec
from app.apps.tree import input_fields, node_map

PIN = "main"
VIEW = "latest"
INSTANCE_VIEW = "inst"
ENTRY = "start"
RECONCILER = "reconciler"


class DeriveRefused(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


async def derive_app(session: AsyncSession, workflow_id: str, version: int, *, name: str | None = None) -> dict[str, Any]:
    """返回一份 agentlab.app/1 spec（未发布的草稿）。"""
    from app.apps.pins import PinState
    from app.apps.versions import version_row
    from app.capabilities.registry import effective_effect, get_capability
    from app.db.models import Schedule, Workflow

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise DeriveRefused(404, "工作流不存在，可能已被删除")
    row = await version_row(session, workflow_id, version)
    if row is None:
        raise DeriveRefused(404, f"「{workflow.name}」不存在 v{version} 版本")
    state = PinState(alias=PIN, pin={"workflow_id": workflow_id, "version": version}, workflow=workflow, row=row)
    graph = row.graph or {}
    nodes = node_map(graph)
    fields = input_fields(graph)
    humans = {nid: n for nid, n in nodes.items() if n["type"] == "human"}
    writes: dict[str, dict[str, Any]] = {}
    lookups = True
    for nid, node in nodes.items():
        tool = node["config"].get("tool") if node["type"] == "tool" else None
        if isinstance(tool, str):
            cap = await get_capability(session, tool)
            if cap is not None and effective_effect(cap) == "write":
                writes[nid] = node
                lookups = lookups and bool(getattr(cap, "lookup", None))
    process = bool(humans or writes)
    spec: dict[str, Any] = {
        "format": APP_FORMAT, "name": name or workflow.name, "description": workflow.description or "",
        "icon": "app",
        "pins": [{"alias": PIN, "workflow_id": workflow_id, "version": version, "graph_hash": state.hash,
                  "level": state.level, "use": "process" if process else "view"}],
        "slots": {}, "entries": [], "forms": {}, "task_ui": {}, "views": {}, "pages": [], "hooks": [], "notify": [],
    }
    for nid, node in humans.items():
        assignee = node["config"].get("assignee")
        if isinstance(assignee, dict) and assignee.get("kind") == "slot" and assignee.get("slot"):
            spec["slots"].setdefault(str(assignee["slot"]), {"label": str(assignee["slot"])})
        spec["task_ui"][nid] = {"title": node["title"]}
    if writes and not lookups:
        spec["slots"][RECONCILER] = {"label": "动作核实"}
    if fields:
        spec["forms"][ENTRY] = input_schema(fields)
        names = list(spec["forms"][ENTRY]["properties"])
        first = next((n for n in names if n in spec["forms"][ENTRY]["required"]), names[0] if names else None)
        spec["entries"].append({
            "id": ENTRY, "label": f"发起{workflow.name}" if process else "按条件运行", "pin": PIN,
            "form": {"$ref": f"#/forms/{ENTRY}"}, "map": {n: f"form.{n}" for n in names},
            **({"instance_title": {"pattern": f"{workflow.name}·{{{first}}}", "fields": [first]}}
               if first and re.fullmatch(r"[A-Za-z0-9_㐀-鿿]+", first) else {}),
        })
    schedule = (await session.execute(select(Schedule).where(
        Schedule.workflow_id == workflow_id, Schedule.run_class == "formal", Schedule.enabled.is_(True))
        .order_by(Schedule.created_at))).scalars().first()
    spec["views"][VIEW] = {"source": {"kind": "latest_sealed", "pin": PIN,
                                      "from": f"schedule:{schedule.id}" if schedule else "formal"}}
    if spec["entries"]:
        spec["views"][INSTANCE_VIEW] = {"source": {"kind": "instance"}}
    spec["pages"].append({"id": "home", "title": workflow.name, "body": _results(nodes, VIEW, "home")
                          + _entries(spec)})
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
    """看结果的那几块：印章、口径卡的指标（分栏）、报告、取数节点的表格。"""
    body: list[dict[str, Any]] = [{"id": f"{prefix}_stamp", "type": "stamp", "bind": {"view": view}}]
    for nid, node in nodes.items():
        if node["type"] != "metrics":
            continue
        metrics = [m for m in node["config"].get("metrics") or [] if isinstance(m, dict) and m.get("id")]
        if not metrics:
            continue
        body.append({"id": f"{prefix}_kpis_{_safe(nid)}", "type": "section", "props": {"title": node["title"][:200]},
                     "children": [{"id": f"{prefix}_cols_{_safe(nid)}", "type": "columns",
                                   "props": {"count": min(len(metrics), 4)}, "children": [
                                       {"id": f"{prefix}_m_{_safe(nid)}_{_safe(str(m['id']))}", "type": "metric",
                                        "props": {"label": str(m.get("name") or m["id"])[:200],
                                                  **({"unit": str(m["unit"])[:200]} if m.get("unit") else {})},
                                        "bind": {"view": view, "metric": f"{nid}.{m['id']}"}}
                                       for m in metrics]}]})
    for nid, node in nodes.items():
        if node["type"] == "report":
            body.append({"id": f"{prefix}_report_{_safe(nid)}", "type": "report", "props": {"title": node["title"][:200]},
                         "bind": {"view": view, "report": nid}})
    for nid, node in nodes.items():
        if node["type"] == "tool" and str(node["config"].get("tool") or "").startswith("db_query__"):
            body.append({"id": f"{prefix}_table_{_safe(nid)}", "type": "table", "props": {"title": node["title"][:200]},
                         "bind": {"view": view, "query": {"node": nid}}})
    return body


def _entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in spec["entries"]:
        out.append({"id": f"btn_{_safe(entry['id'])}", "type": "entry_button", "props": {"entry": entry["id"]}})
        out.append({"id": f"list_{_safe(entry['id'])}", "type": "instance_list",
                    "props": {"entry": entry["id"], "who": "mine"}})
    return out
