"""受信组件目录 v1（docs/design/app-platform-and-look.md §2.2、§3.5）。

一份目录，四个使用方：应用页、人工节点表单、agent 拟的询问、助手生成。模型只能从这里挑组件；前端只有一张
「名字 → 渲染器」的表，遇到不认识的组件显示「此组件需要更新前端」，不报错、不空白。

每个组件登记：名字、中文名、类别、版本、props 的 JSON Schema、允许的绑定种类、能不能有子组件。
服务端校验器和解析器由 app/apps/ 的实现按名字注册（BE-3）；开发者扩展组件时，目录项、校验器、解析器、前端渲染器
四样都要给，缺一样启动即报错（check_registry）。
"""
from __future__ import annotations

from typing import Any

CATEGORIES = ("layout", "display", "input", "process")
CATEGORY_LABEL = {"layout": "布局", "display": "展示", "input": "输入", "process": "流程"}


def _props(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties}


_TEXT = {"type": "string", "maxLength": 200}
_LONG = {"type": "string", "maxLength": 4000}

COMPONENTS: list[dict[str, Any]] = [
    # ---- 布局 ----
    {"name": "page", "label": "页面", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(title=_TEXT)},
    {"name": "section", "label": "分区", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(title=_TEXT, description=_LONG)},
    {"name": "columns", "label": "分栏", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(count={"type": "integer", "minimum": 1, "maximum": 4})},
    {"name": "tabs", "label": "页签", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(labels={"type": "array", "items": _TEXT, "maxItems": 8})},
    {"name": "divider", "label": "分隔线", "category": "layout", "version": 1, "children": False, "bindings": [],
     "props": _props()},
    # ---- 展示 ----
    {"name": "stamp", "label": "印章", "category": "display", "version": 1, "children": False,
     "bindings": ["run"], "props": _props(compact={"type": "boolean"})},
    {"name": "metric", "label": "指标块", "category": "display", "version": 1, "children": False,
     "bindings": ["metric", "cell"], "props": _props(label=_TEXT, unit=_TEXT, conv=_TEXT)},
    {"name": "series", "label": "趋势", "category": "display", "version": 1, "children": False,
     "bindings": ["series"], "props": _props(label=_TEXT, chart={"enum": ["line", "bar"]})},
    {"name": "table", "label": "表格", "category": "display", "version": 1, "children": False,
     "bindings": ["query"], "props": _props(title=_TEXT, columns={"type": "array", "items": _TEXT},
                                            sort={"type": "string"}, limit={"type": "integer", "minimum": 1})},
    {"name": "chart", "label": "图表", "category": "display", "version": 1, "children": False,
     "bindings": ["query"], "props": _props(title=_TEXT, chart={"enum": ["line", "bar"]}, x=_TEXT,
                                            y={"type": "array", "items": _TEXT, "maxItems": 4})},
    {"name": "report", "label": "报告", "category": "display", "version": 1, "children": False,
     "bindings": ["report"], "props": _props(title=_TEXT)},
    {"name": "text", "label": "文字", "category": "display", "version": 1, "children": False, "bindings": [],
     "props": _props(markdown=_LONG)},
    {"name": "notice", "label": "提示", "category": "display", "version": 1, "children": False, "bindings": [],
     "props": _props(text=_LONG, tone={"enum": ["info", "warn", "error"]})},
    {"name": "field", "label": "字段", "category": "display", "version": 1, "children": False,
     "bindings": ["input", "output", "run"], "props": _props(label=_TEXT)},
    {"name": "status", "label": "状态", "category": "display", "version": 1, "children": False,
     "bindings": ["run"], "props": _props(label=_TEXT)},
    # ---- 输入 ----
    {"name": "form", "label": "表单", "category": "input", "version": 1, "children": False, "bindings": [],
     "props": _props(form={"type": "string"}, submit_label=_TEXT)},
    # ---- 流程 ----
    {"name": "entry_button", "label": "发起按钮", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(entry={"type": "string"}, label=_TEXT)},
    {"name": "instance_list", "label": "实例列表", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(entry={"type": "string"}, who={"enum": ["mine", "all"]},
                                     limit={"type": "integer", "minimum": 1, "maximum": 200})},
    {"name": "instance_header", "label": "实例概况", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props()},
    {"name": "instance_timeline", "label": "实例进度", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props()},
    {"name": "task_list", "label": "待办列表", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(scope={"enum": ["mine", "instance"]})},
    {"name": "task_form", "label": "待办表单", "category": "process", "version": 1, "children": False,
     "bindings": ["task"], "props": _props()},
    {"name": "signoffs", "label": "会签进度", "category": "process", "version": 1, "children": False,
     "bindings": ["task"], "props": _props()},
    {"name": "action_status", "label": "动作状态", "category": "process", "version": 1, "children": False,
     "bindings": ["action"], "props": _props(label=_TEXT)},
]

BY_NAME: dict[str, dict[str, Any]] = {c["name"]: c for c in COMPONENTS}


def catalog_payload() -> dict[str, Any]:
    """GET /api/apps/catalog 的返回：组件目录 + 类别名。助手生成时也给模型看这一份（只给名字、中文名、props、绑定）。"""
    return {"version": 1, "categories": CATEGORY_LABEL, "components": COMPONENTS}
