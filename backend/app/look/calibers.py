"""查看的对象之一：口径（normal-user-apps N2，补 G10：没有按名字检索的口径库）。蓝图助手看「看哪些数」时，先查数据目录，
再查已发布工作流里的口径卡：对到已有的口径卡时优先复用（实例化成 caliber_from），不重写指标式。

只列每个工作流最新的「已发布且未退役」的版本（app/apps/versions，P5 的唯一说法）里的口径卡，本身转抄别处口径卡的
（caliber_from）不列——源头才能复用（运行时也只认源头，engine/nodes/metrics._pinned_caliber）。

复用的是整张卡（caliber_from 固定到那一版的那张卡），卡上每个指标都要算得出来：卡读的每个输入都要是 cell(nodes.X, 0,
'列名')、X 是查库的调用工具节点。新图里那个库的取数节点就叫 X、交出同样的列（blueprints/templates/_common.data_groups）。
读别的（整形、沙箱、模型写的、按别的行号取）的卡照实说不能复用，请按表重新对。

点名写法：source 写工作流名（或 id），name 写指标名（或指标 id）；只写 source 列那个工作流的全部指标。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.look.base import PICK_LIMIT, LookTarget
from app.look.pick import Picker, PickFailed

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_HEAD_PROMPT = "已发布的口径（要复用已有的指标时用 look，kind 写 caliber）："
OVERVIEW_LINE_PROMPT = "- {workflow} v{version}：{metrics}"
OVERVIEW_MORE_PROMPT = "- …另有 {n} 个工作流"
DETAIL_HEAD_PROMPT = "· {name}（指标 id {metric}{unit}）：出自「{workflow}」v{version} 的口径卡「{card}」（{caliber}）"
DETAIL_EXPR_PROMPT = "    指标式：{expression}"
DETAIL_REUSE_PROMPT = ("    复用写法（「看哪些数」这一项的 reuse 照抄，不要改）：{reuse}\n"
                       "    这张卡读：{inputs}。实例化时那个库的取数节点就叫这个名字、交出这几列，复用才算得出来")
DETAIL_INPUT_PROMPT = "{node}（{tool}）的列 {columns}"
DETAIL_NOT_REUSABLE_PROMPT = "    不能复用：{reason}。请按表重新对"
BRIEF_PROMPT = "· {name}（{workflow} v{version}）：字数超过上限，只列名字，要看指标式时点名再 look"
MISSING_PROMPT = "没找到口径：{items}（只能查看已发布且未退役的版本里的口径卡；别拿名字相近的顶替）"
INDEX_LINE_PROMPT = "{workflow}｜{card}｜{name}（{metric}）｜{unit}"
PICK_WHAT_PROMPT = "口径指标"
PICK_LINE_PROMPT = "每行一个指标：工作流名｜口径卡｜指标名（指标 id）｜单位；source 写工作流名，name 写指标名"

#: 概览最多列几个工作流、每个工作流最多列几个指标名
OVERVIEW_WORKFLOWS = 30
OVERVIEW_METRICS = 8

#: 不能复用的原因（写进查看结果，也是蓝图存的时候拒收的说法）
NOT_TOOL_REASON = "这张口径卡的输入不全是查库结果里的格（有整形、代码、模型写的或别的口径卡的数）"
ROW_REASON = "这张口径卡按别的行号取格，模板的取数只出一行"
CHAINED_REASON = "这张口径卡本身也是转抄别处的口径，请复用源头那一张"


@dataclass(frozen=True)
class CaliberView:
    """一个已发布口径卡上的一个指标。inputs 是整张卡读的取数节点和列（能复用时），reason 是不能复用的原因。"""

    workflow_id: str
    workflow_name: str
    version: int
    level: str
    node_id: str
    card: str
    caliber: str
    metric_id: str
    name: str
    unit: str
    expression: str
    inputs: tuple[tuple[str, str, tuple[str, ...]], ...]
    reason: str | None

    @property
    def label(self) -> str:
        return f"{self.workflow_name} v{self.version} · {self.name}"

    def reuse(self) -> dict[str, Any] | None:
        """「看哪些数」这一项的 reuse（blueprints/ops._reuse 的形状）；不能复用为 None。"""
        if self.reason is not None:
            return None
        return {"workflow_id": self.workflow_id, "version": self.version, "node_id": self.node_id,
                "metric_id": self.metric_id,
                "inputs": [{"node": n, "tool": t, "columns": list(cols)} for n, t, cols in self.inputs]}


def card_inputs(graph: dict[str, Any], card_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """整张口径卡读的取数节点和列：([{node, tool, columns}], 不能复用的原因)。每个指标式的每个输入都要是
    cell(nodes.X, 0, '列名')、X 是查库的调用工具节点（engine/lineage 认的原始数据，且只取第一行）。"""
    from app.apps.tree import node_map
    from app.engine.expressions import ExpressionError, cell_parts, leaf_refs, parse_expression
    from app.tools.datasource import QUERY_PREFIX

    nodes = node_map(graph)
    card = nodes.get(card_id)
    if card is None or card["type"] != "metrics":
        return [], "指的不是口径卡"
    if card["config"].get("caliber_from") not in (None, "", {}):
        return [], CHAINED_REASON
    found: dict[str, dict[str, Any]] = {}
    for metric in card["config"].get("metrics") or []:
        try:
            tree = parse_expression(str((metric or {}).get("expression") or ""))[0]
        except ExpressionError:
            return [], NOT_TOOL_REASON
        for path in leaf_refs(tree):
            parts = cell_parts(path)
            if parts is None:
                return [], NOT_TOOL_REASON
            chain, row, column = parts
            node_id = chain[len("nodes."):] if chain.startswith("nodes.") else ""
            tool = str((nodes.get(node_id) or {}).get("config", {}).get("tool") or "")
            if (nodes.get(node_id) or {}).get("type") != "tool" or not tool.startswith(QUERY_PREFIX):
                return [], NOT_TOOL_REASON
            if row.strip() != "0":
                return [], ROW_REASON
            try:
                col = json.loads(column.replace("'", '"')) if column[:1] in ("'", '"') else None
            except ValueError:
                col = None
            if not isinstance(col, str):
                return [], NOT_TOOL_REASON
            entry = found.setdefault(node_id, {"node": node_id, "tool": tool, "columns": []})
            if col not in entry["columns"]:
                entry["columns"].append(col)
    return list(found.values()), None


def views_of(workflow_id: str, workflow_name: str, version: Any) -> list[CaliberView]:
    """一个已发布版本里每张口径卡（源头的）的每个指标。"""
    from app.apps.tree import node_map

    graph = getattr(version, "graph", None) or {}
    out: list[CaliberView] = []
    for nid, node in node_map(graph).items():
        if node["type"] != "metrics" or node["config"].get("caliber_from") not in (None, "", {}):
            continue
        inputs, reason = card_inputs(graph, nid)
        packed = tuple((i["node"], i["tool"], tuple(i["columns"])) for i in inputs)
        caliber = f"{node['config'].get('caliber') or node['title']} @ {node['config'].get('caliber_version') or 'v1'}"
        for metric in node["config"].get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("id"):
                continue
            out.append(CaliberView(
                workflow_id=workflow_id, workflow_name=workflow_name, version=int(version.version),
                level=str(getattr(version, "level", None) or ""), node_id=nid, card=node["title"], caliber=caliber,
                metric_id=str(metric["id"]), name=str(metric.get("name") or metric["id"]),
                unit=str(metric.get("unit") or ""), expression=str(metric.get("expression") or ""), inputs=packed,
                reason=reason))
    return out


class CaliberKind:
    kind = "caliber"
    label = "口径"

    def __init__(self, items: list[CaliberView], *, picker: Picker | None = None) -> None:
        self.items = items
        self.picker = picker

    @classmethod
    async def load(cls, session: Any, *, picker: Picker | None = None) -> CaliberKind:
        """读每个工作流最新的已发布且未退役的版本里的口径卡。用调用方的会话，开流之前读好。"""
        from sqlalchemy import select

        from app.apps import versions as app_versions
        from app.db.models import Workflow

        items: list[CaliberView] = []
        for wf in (await session.execute(select(Workflow).order_by(Workflow.name))).scalars().all():
            latest = await app_versions.latest_published(session, wf.id)
            if latest is not None:
                items += views_of(wf.id, wf.name, latest)
        return cls(items, picker=picker)

    def _workflows(self) -> dict[str, list[CaliberView]]:
        out: dict[str, list[CaliberView]] = {}
        for item in self.items:
            out.setdefault(item.workflow_id, []).append(item)
        return out

    def _pool(self, sources: list[str] | None) -> list[CaliberView]:
        return [i for i in self.items if not sources or i.workflow_name in sources or i.workflow_id in sources]

    # ---------------------------------------------------------------- 概览、目录

    async def overview(self) -> list[str]:
        flows = self._workflows()
        if not flows:
            return []
        lines = [OVERVIEW_HEAD_PROMPT]
        for items in list(flows.values())[:OVERVIEW_WORKFLOWS]:
            names = "、".join(i.name for i in items[:OVERVIEW_METRICS]) + ("…" if len(items) > OVERVIEW_METRICS else "")
            lines.append(OVERVIEW_LINE_PROMPT.format(workflow=items[0].workflow_name, version=items[0].version,
                                                     metrics=names))
        if len(flows) > OVERVIEW_WORKFLOWS:
            lines.append(OVERVIEW_MORE_PROMPT.format(n=len(flows) - OVERVIEW_WORKFLOWS))
        return lines

    async def index(self, sources: list[str] | None) -> str:
        return "\n".join(INDEX_LINE_PROMPT.format(workflow=i.workflow_name, card=i.card, name=i.name,
                                                  metric=i.metric_id, unit=i.unit or "-")
                         for i in self._pool(sources))

    # ---------------------------------------------------------------- 点名、挑选

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[tuple[CaliberView, str]], list[LookTarget]]:
        found: list[tuple[CaliberView, str]] = []
        missing: list[LookTarget] = []
        for t in targets:
            source, name = str(t.get("source") or ""), str(t.get("name") or "")
            hits = [i for i in self._pool([source] if source else None) if name in (i.name, i.metric_id)]
            if not hits:
                missing.append(t)
            for hit in hits:
                if all(i is not hit for i, _ in found):
                    found.append((hit, "named"))
        return found, missing

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[tuple[CaliberView, str]], str | None]:
        pool = self._pool(sources)
        if len(pool) <= PICK_LIMIT:
            return [(i, "picked") for i in pool], None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT,
                                           index=await self.index(sources))
        except PickFailed as e:
            return [], str(e)
        out: list[tuple[CaliberView, str]] = []
        for source, name in picks:
            for i in pool:
                if name in (i.name, i.metric_id) and (not source or source in (i.workflow_name, i.workflow_id)) \
                        and all(o is not i for o, _ in out):
                    out.append((i, "picked"))
        if picks and not out:
            return [], "挑出的指标不在已发布的口径卡里"
        return out[:PICK_LIMIT], None

    def key(self, obj: tuple[CaliberView, str]) -> str:
        i = obj[0]
        return f"caliber:{i.workflow_id}:{i.version}:{i.node_id}:{i.metric_id}"

    # ---------------------------------------------------------------- 详情

    async def detail(self, objects: list[tuple[CaliberView, str]], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        used = 0
        for item, _how in objects:
            block = [DETAIL_HEAD_PROMPT.format(name=item.name, metric=item.metric_id,
                                               unit=f"，单位 {item.unit}" if item.unit else "",
                                               workflow=item.workflow_name, version=item.version, card=item.card,
                                               caliber=item.caliber),
                     DETAIL_EXPR_PROMPT.format(expression=item.expression)]
            reuse = item.reuse()
            if reuse is not None:
                block.append(DETAIL_REUSE_PROMPT.format(
                    reuse=json.dumps({k: v for k, v in reuse.items() if k != "inputs"}, ensure_ascii=False),
                    inputs="；".join(DETAIL_INPUT_PROMPT.format(node=n, tool=t, columns="、".join(cols))
                                    for n, t, cols in item.inputs)))
            else:
                block.append(DETAIL_NOT_REUSABLE_PROMPT.format(reason=item.reason))
            text = "\n".join(block)
            if used + len(text) > budget:
                text = BRIEF_PROMPT.format(name=item.name, workflow=item.workflow_name, version=item.version)
            used += len(text) + 1
            lines.append(text)
        if missing:
            lines.append(MISSING_PROMPT.format(items="、".join(
                f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing)))
        record = {"kind": "caliber", "named": [i.label for i, how in objects if how == "named"],
                  "picked": [i.label for i, how in objects if how == "picked"],
                  "missing": [f"{t.get('source') or ''} {t.get('name') or ''}".strip() for t in missing]}
        return "\n".join(lines), [record]

    # ---------------------------------------------------------------- 复用

    def reuse_for(self, ref: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """蓝图助手写的 reuse → 按这一轮读好的口径核对并补上 inputs：(完整的 reuse, 不能复用的原因)。"""
        for item in self.items:
            if (item.workflow_id, item.version, item.node_id, item.metric_id) == (
                    ref.get("workflow_id"), ref.get("version"), ref.get("node_id"), ref.get("metric_id")):
                return (item.reuse(), None) if item.reason is None else (None, item.reason)
        return None, "只能复用已发布且未退役的版本里的口径卡（look 给出的那一版）"


async def verify_reuse(session: Any, ref: dict[str, Any]) -> dict[str, Any]:
    """蓝图存的时候按库里那一版核对复用的口径卡：已发布且未退役、是源头的口径卡、有这个指标、能复用。交回补上 inputs
    的 reuse；不行抛 BpRefused（bp.data_unmapped）。用户在卡片上选的、助手提的都过这一道。"""
    from app.apps import versions as app_versions
    from app.blueprints.ops import BpRefused
    from app.db.models import Workflow

    workflow = await session.get(Workflow, str(ref.get("workflow_id") or ""))
    published = await app_versions.published_versions(session, workflow.id) if workflow is not None else []
    version = next((v for v in published if v.version == ref.get("version")), None)
    if workflow is None or version is None:
        raise BpRefused("复用的口径卡要出自已发布且未退役的工作流版本，这一版不是（可能已退役或删除）", code="bp.data_unmapped")
    hit = next((i for i in views_of(workflow.id, workflow.name, version)
                if (i.node_id, i.metric_id) == (ref.get("node_id"), ref.get("metric_id"))), None)
    if hit is None:
        inputs, reason = card_inputs(version.graph or {}, str(ref.get("node_id") or ""))
        raise BpRefused(f"「{workflow.name}」v{version.version} 里没有这个口径指标"
                        + (f"：{reason}" if reason and not inputs else ""), code="bp.data_unmapped")
    reuse = hit.reuse()
    if reuse is None:
        raise BpRefused(f"「{workflow.name}」v{version.version} 的「{hit.name}」不能复用：{hit.reason}。请按表重新对",
                        code="bp.data_unmapped")
    return reuse
