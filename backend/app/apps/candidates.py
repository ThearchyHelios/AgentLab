"""页面编辑的候选（docs/design/normal-user-apps.md N6、§4.5「加一张数据卡片」）：GET /api/apps/{id}/candidates。

从草稿里各视图固定的版本（共享应用是工作流版本，个人应用是蓝图上的图）列出能加的数据卡片：口径卡的指标、趋势、报告、
取数节点的表格，规则同派生（apps/derive.data_candidates）。每个候选交回一个可以直接 app_add 的组件：绑定写好了视图，
id 在这份草稿里不重名。草稿里已经有一模一样的（同类组件、同一个绑定）不再列——要再显示一次，把隐藏的那个取消隐藏。

实例视图不列：实例页上的数跟着某一次实例走，「加一张数据卡片」是给应用页的。还没有图的视图（固定的版本不存在、试运行
派生的草稿没有版本）也不列，界面照实写「没有能加的」。
"""
from __future__ import annotations

import copy
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: 候选组件 id 的前缀
PREFIX = "add"
#: 视图来源在候选分组里的说法：草稿里不止一个视图时，分组写清是哪一种来源的数
SOURCE_TEXT = {"latest_sealed": "最近一次出数", "on_demand": "按条件查看", "exploratory": "最近一次刷新"}


def _key(comp: dict[str, Any]) -> tuple[str, str]:
    return str(comp.get("type")), json.dumps(comp.get("bind"), sort_keys=True, ensure_ascii=False)


async def app_candidates(session: AsyncSession, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """[{kind: metric|series|report|table, label, group, component}]，按视图、再按派生规则的顺序。"""
    from app.apps.derive import data_candidates
    from app.apps.pins import load_pins
    from app.apps.tree import components, node_map

    pins = await load_pins(session, spec)
    views = {vid: v for vid, v in (spec.get("views") or {}).items()
             if isinstance(v, dict) and ((v.get("source") or {}).get("kind")) != "instance"}
    present = {_key(c) for c, _, _ in components(spec)}
    taken = {str(c.get("id")) for c, _, _ in components(spec)} | {
        str(p.get("id")) for p in spec.get("pages") or [] if isinstance(p, dict)}
    out: list[dict[str, Any]] = []
    for vid, view in views.items():
        source = view.get("source") or {}
        state = pins.get(source.get("pin"))
        graph = state.graph if state is not None else {}
        if not graph:
            continue
        for item in data_candidates(node_map(graph), vid, PREFIX):
            comp = copy.deepcopy(item["component"])
            if _key(comp) in present:
                continue
            comp["id"] = _unique(f"{comp['id']}_{vid}" if len(views) > 1 else comp["id"], taken)
            taken.add(comp["id"])
            group = item["group"]
            if len(views) > 1:
                group = f"{group} · {SOURCE_TEXT.get(str(source.get('kind')), vid)}"
            out.append({"kind": item["kind"], "label": item["label"], "group": group, "component": comp})
    return out


def _unique(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"
