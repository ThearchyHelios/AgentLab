"""查看的对象之一：能力（总方案 L3、§5.3）。读能力登记（capabilities 表，BE-1 同步和治理）。

只给模型写图要用的：名字、所属系统、说明、参数、返回字段、读写效果、要不要确认。不给端点地址、认证头、凭据模式这些
（登记的端点在 endpoints 表里，这里根本不读）；遮罩字段只给字段名，参数的示例值、默认值一律不给。

问数据（意图为回答）时只列只读能力：写入能力不出现在概览里，点名查看也按没找到回（§2.11：问数据只能走只读）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.look.base import PICK_LIMIT, LookTarget
from app.look.pick import Picker, PickFailed

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_HEAD_PROMPT = "可查看的能力（要看参数、返回字段、读写效果时用 look，kind 写 capability）："
OVERVIEW_LINE_PROMPT = "- {system}：{n} 个（{effects}）"
OVERVIEW_MORE_PROMPT = "- …另有 {n} 个来源"
EFFECT_PROMPT = {"read": "只读", "write": "写入", "dynamic": "视参数而定"}
APPROVAL_PROMPT = {
    "confirm": "每次都要人确认",
    "none": "只读能力，正式运行里不用审批",
    "inherit": "跟随工具页的信任设置",
}
SYSTEM_LABEL_PROMPT = {"builtin": "内置工具", "custom": "自定义工具"}
DETAIL_HEAD_PROMPT = "· {name}（{system}）：{title}"
DETAIL_DESC_PROMPT = "    说明：{text}"
DETAIL_EFFECT_PROMPT = "    读写：{effect}；确认：{approval}"
DETAIL_WRITE_PROMPT = "    写入能力只能放在调用工具节点里，运行时先确认再发送，参数在节点的 args 里按引用写"
DETAIL_PARAMS_PROMPT = "    参数：{items}"
DETAIL_RETURNS_PROMPT = "    返回字段：{items}"
DETAIL_TABULAR_PROMPT = "    返回结果按表格作证，可以像查询结果一样被报告和口径卡引用"
PARAM_PROMPT = "{name}（{type}{required}）{desc}"
FIELD_MASKED_PROMPT = "{name}（遮罩，只给字段名）"
BRIEF_PROMPT = "· {name}（{system}）：字数超过上限，只列名字，要看参数时点名再 look"
MISSING_PROMPT = "没找到能力：{items}（能力登记里没有它{answer}；别拿名字相近的顶替）"
MISSING_ANSWER_PROMPT = "；问数据时只能查看只读能力"
INDEX_LINE_PROMPT = "{name}｜{title}｜{effect}"
INDEX_HEAD_PROMPT = "「{system}」："
PICK_WHAT_PROMPT = "能力"
PICK_LINE_PROMPT = "每行一个能力：名字｜标题｜读写"

#: 概览最多列几个来源
OVERVIEW_SYSTEMS = 30


@dataclass(frozen=True)
class CapabilityView:
    """能力登记的一行，开流之前读好（流里不再碰请求的会话）。只留查看要用的字段。"""

    name: str
    system: str
    title: str
    description: str
    params_schema: dict[str, Any]
    returns_schema: dict[str, Any] | None
    tabular: dict[str, Any] | None
    effect: str
    approval: str
    fields: dict[str, Any]
    masked: tuple[str, ...]
    version: int


def _system_label(system: str) -> str:
    return SYSTEM_LABEL_PROMPT.get(system, system or "未归类")


class CapabilityKind:
    kind = "capability"
    label = "能力"

    def __init__(self, rows: list[CapabilityView], *, picker: Picker | None = None, read_only: bool = False) -> None:
        self.read_only = read_only
        self.rows = [r for r in rows if not read_only or r.effect == "read"]
        self.by_name = {r.name: r for r in self.rows}
        self.picker = picker

    @classmethod
    async def load(cls, *, picker: Picker | None = None, read_only: bool = False) -> CapabilityKind:
        """读能力登记里还在的那些（来源删掉了的 missing 行不列）。效果按治理后的值（effective_effect）。"""
        from sqlalchemy import select

        from app.capabilities.registry import effective_effect, masked_paths
        from app.db.base import SessionLocal
        from app.db.models import Capability

        async with SessionLocal() as session:
            found = (await session.execute(
                select(Capability).where(Capability.missing.is_(False)).order_by(Capability.system, Capability.name)
            )).scalars().all()
            rows = [CapabilityView(
                name=c.name, system=c.system or "", title=c.title or "", description=c.description or "",
                params_schema=dict(c.params_schema or {}), returns_schema=c.returns_schema, tabular=c.tabular,
                effect=effective_effect(c), approval=c.approval or "inherit", fields=dict(c.fields or {}),
                masked=tuple(str(p) for p in masked_paths(c)), version=int(c.version or 1)) for c in found]
        return cls(rows, picker=picker, read_only=read_only)

    # ---------------------------------------------------------------- 概览、目录

    def _systems(self) -> dict[str, list[CapabilityView]]:
        out: dict[str, list[CapabilityView]] = {}
        for r in self.rows:
            out.setdefault(r.system, []).append(r)
        return out

    async def overview(self) -> list[str]:
        systems = self._systems()
        if not systems:
            return []
        lines = [OVERVIEW_HEAD_PROMPT]
        for system, rows in list(systems.items())[:OVERVIEW_SYSTEMS]:
            counts = {e: sum(1 for r in rows if r.effect == e) for e in EFFECT_PROMPT}
            effects = "、".join(f"{EFFECT_PROMPT[e]} {n} 个" for e, n in counts.items() if n)
            lines.append(OVERVIEW_LINE_PROMPT.format(system=_system_label(system), n=len(rows), effects=effects))
        if len(systems) > OVERVIEW_SYSTEMS:
            lines.append(OVERVIEW_MORE_PROMPT.format(n=len(systems) - OVERVIEW_SYSTEMS))
        return lines

    def _pool(self, sources: list[str] | None) -> list[CapabilityView]:
        if not sources:
            return list(self.rows)
        wanted = set(sources)
        return [r for r in self.rows if r.system in wanted or _system_label(r.system) in wanted]

    async def index(self, sources: list[str] | None) -> str:
        blocks: dict[str, list[str]] = {}
        for r in self._pool(sources):
            blocks.setdefault(r.system, []).append(INDEX_LINE_PROMPT.format(
                name=r.name, title=r.title or r.description[:40], effect=EFFECT_PROMPT.get(r.effect, r.effect)))
        return "\n\n".join(INDEX_HEAD_PROMPT.format(system=_system_label(s)) + "\n" + "\n".join(lines)
                           for s, lines in blocks.items())

    # ---------------------------------------------------------------- 点名、挑选

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[tuple[CapabilityView, str]], list[LookTarget]]:
        found: list[tuple[CapabilityView, str]] = []
        missing: list[LookTarget] = []
        lowered = {r.name.lower(): r for r in self.rows}
        for t in targets:
            name = str(t.get("name") or "")
            row = self.by_name.get(name) or lowered.get(name.lower())
            if row is None:
                missing.append(t)
            elif all(r.name != row.name for r, _ in found):
                found.append((row, "named"))
        return found, missing

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[tuple[CapabilityView, str]], str | None]:
        pool = self._pool(sources)
        if len(pool) <= PICK_LIMIT:
            return [(r, "picked") for r in pool], None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT,
                                           index=await self.index(sources))
        except PickFailed as e:
            return [], str(e)
        names = {r.name: r for r in pool}
        out = [(names[name], "picked") for _source, name in picks if name in names]
        out = list({r.name: (r, how) for r, how in out}.values())
        if picks and not out:
            return [], "挑出的能力在登记里都不存在"
        return out[:PICK_LIMIT], None

    def key(self, obj: tuple[CapabilityView, str]) -> str:
        return f"capability:{obj[0].name}"

    # ---------------------------------------------------------------- 详情

    async def detail(self, objects: list[tuple[CapabilityView, str]], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        used = 0
        for row, _how in objects:
            block = _detail(row)
            if used + len(block) > budget:
                block = BRIEF_PROMPT.format(name=row.name, system=_system_label(row.system))
            used += len(block) + 1
            lines.append(block)
        if missing:
            lines.append(MISSING_PROMPT.format(items="、".join(str(t.get("name") or "") for t in missing),
                                               answer=MISSING_ANSWER_PROMPT if self.read_only else ""))
        record = {"kind": "capability", "named": [r.name for r, how in objects if how == "named"],
                  "picked": [r.name for r, how in objects if how == "picked"],
                  "missing": [str(t.get("name") or "") for t in missing],
                  "versions": {r.name: r.version for r, _ in objects}}
        return "\n".join(lines), [record]


def _schema_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    kind = schema.get("type")
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    if kind:
        return str(kind)
    if "enum" in schema:
        return "enum"
    return "any"


def _params(row: CapabilityView) -> list[str]:
    """参数：名字、类型、是否必填、说明。示例值和默认值不给（遮罩字段的样例会把值带出去）。"""
    props = row.params_schema.get("properties") if isinstance(row.params_schema, dict) else None
    required = set(row.params_schema.get("required") or []) if isinstance(row.params_schema, dict) else set()
    out = []
    for name, schema in (props or {}).items():
        desc = str((schema or {}).get("description") or "").strip() if isinstance(schema, dict) else ""
        out.append(PARAM_PROMPT.format(name=name, type=_schema_type(schema), required="，必填" if name in required
                                       else "", desc=f"：{desc}" if desc else ""))
    return out


def _label(row: CapabilityView, pointer: str) -> str:
    """返回字段的中文名：fields 里按 JSON 指针记的 label 项（形状同数据目录的「项」），驳回的不用。"""
    item = (row.fields.get(pointer) or {}).get("label") if isinstance(row.fields.get(pointer), dict) else None
    if isinstance(item, dict) and item.get("status") != "rejected" and item.get("value"):
        return str(item["value"])
    return ""


def _returns(row: CapabilityView) -> list[str]:
    """返回字段：声明了 tabular 的按列（列名 ← JSON 指针），否则按 returns_schema 的顶层字段。遮罩字段只给名字。"""
    masked = set(row.masked)
    pairs: list[tuple[str, str]] = []
    columns = (row.tabular or {}).get("columns") if isinstance(row.tabular, dict) else None
    if isinstance(columns, dict) and columns:
        pairs = [(str(label), str(pointer)) for label, pointer in columns.items()]
    elif isinstance(row.returns_schema, dict):
        pairs = [(name, f"/{name}") for name in (row.returns_schema.get("properties") or {})]
    out = []
    for name, pointer in pairs:
        if pointer in masked:
            out.append(FIELD_MASKED_PROMPT.format(name=name))
            continue
        label = _label(row, pointer)
        out.append(f"{name}（{label}）" if label and label != name else name)
    return out


def _detail(row: CapabilityView) -> str:
    lines = [DETAIL_HEAD_PROMPT.format(name=row.name, system=_system_label(row.system),
                                       title=row.title or row.description or row.name)]
    if row.title and row.description and row.description != row.title:
        lines.append(DETAIL_DESC_PROMPT.format(text=row.description))
    lines.append(DETAIL_EFFECT_PROMPT.format(effect=EFFECT_PROMPT.get(row.effect, row.effect),
                                             approval=APPROVAL_PROMPT.get(row.approval, row.approval)))
    if row.effect == "write":
        lines.append(DETAIL_WRITE_PROMPT)
    if params := _params(row):
        lines.append(DETAIL_PARAMS_PROMPT.format(items="；".join(params)))
    if returns := _returns(row):
        lines.append(DETAIL_RETURNS_PROMPT.format(items="、".join(returns)))
    if row.tabular:
        lines.append(DETAIL_TABULAR_PROMPT)
    return "\n".join(lines)
