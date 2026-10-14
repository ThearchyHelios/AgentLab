"""受信组件目录 v1（docs/design/app-platform-and-look.md §2.2、§3.5）。

一份目录，四个使用方：应用页、人工节点表单、agent 拟的询问、助手生成。模型只能从这里挑组件；前端只有一张
「名字 → 渲染器」的表，遇到不认识的组件显示「此组件需要更新前端」，不报错、不空白。

每个组件登记：名字、中文名、类别、版本、props 的 JSON Schema、允许的绑定种类、能不能有子组件。
props 里每一项都写 title（属性面板上的中文名），取值不直观的另写 description；枚举写 x-enum-labels（和 enum 按位置
对应，同表单 agentlab.form/1 的写法）。这几样是注解，不参与校验。
服务端校验器和解析器由 app/apps/ 的实现按名字注册（BE-3）；开发者扩展组件时，目录项、校验器、解析器、前端渲染器
四样都要给，缺一样启动即报错（check_registry）。
"""
from __future__ import annotations

from typing import Any

CATEGORIES = ("layout", "display", "input", "process")
CATEGORY_LABEL = {"layout": "布局", "display": "展示", "input": "输入", "process": "流程"}


def _props(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties}


def _text(title: str, **more: Any) -> dict[str, Any]:
    return {"type": "string", "maxLength": 200, "title": title, **more}


def _long(title: str) -> dict[str, Any]:
    return {"type": "string", "maxLength": 4000, "title": title}


def _enum(title: str, **labels: str) -> dict[str, Any]:
    """枚举：值是键，中文是 x-enum-labels（按位置对应）。"""
    return {"enum": list(labels), "x-enum-labels": list(labels.values()), "title": title}


_CHART = _enum("图形", line="折线图", bar="柱状图")

COMPONENTS: list[dict[str, Any]] = [
    # ---- 布局 ----
    {"name": "page", "label": "页面", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(title=_text("标题"))},
    {"name": "section", "label": "分区", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(title=_text("标题"), description=_long("说明"))},
    {"name": "columns", "label": "分栏", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(count={"type": "integer", "minimum": 1, "maximum": 4, "title": "列数"})},
    {"name": "tabs", "label": "页签", "category": "layout", "version": 1, "children": True, "bindings": [],
     "props": _props(labels={"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 8,
                             "title": "页签名", "description": "按顺序对应里面的子组件"})},
    {"name": "divider", "label": "分隔线", "category": "layout", "version": 1, "children": False, "bindings": [],
     "props": _props()},
    # ---- 展示 ----
    {"name": "stamp", "label": "印章", "category": "display", "version": 1, "children": False,
     "bindings": ["run"], "props": _props(compact={"type": "boolean", "title": "紧凑显示"})},
    {"name": "metric", "label": "指标块", "category": "display", "version": 1, "children": False,
     "bindings": ["metric", "cell"],
     "props": _props(label=_text("名称"),
                     unit=_text("单位", description="绑定单元格时写在数下面；绑定指标时数里已经带着口径卡的单位"),
                     conv=_text("换算", description="万、亿、pct（百分比）、int（取整）或 .N（保留 N 位小数）"))},
    {"name": "series", "label": "趋势", "category": "display", "version": 1, "children": False,
     "bindings": ["series"], "props": _props(label=_text("名称"), chart=_CHART)},
    {"name": "table", "label": "表格", "category": "display", "version": 1, "children": False,
     "bindings": ["query"],
     "props": _props(title=_text("标题"),
                     columns={"type": "array", "items": {"type": "string", "maxLength": 200}, "title": "显示的列",
                              "description": "列名，不填显示全部列"},
                     sort={"type": "string", "title": "排序列", "description": "按哪一列排序，写列名；前面加「-」为降序"},
                     limit={"type": "integer", "minimum": 1, "title": "最多显示行数"})},
    {"name": "chart", "label": "图表", "category": "display", "version": 1, "children": False,
     "bindings": ["query"],
     "props": _props(title=_text("标题"), chart=_CHART, x=_text("横轴列"),
                     y={"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 4,
                        "title": "纵轴列", "description": "最多 4 列"})},
    {"name": "report", "label": "报告", "category": "display", "version": 1, "children": False,
     "bindings": ["report"], "props": _props(title=_text("标题"))},
    {"name": "text", "label": "文字", "category": "display", "version": 1, "children": False, "bindings": [],
     "props": _props(markdown=_long("正文（Markdown）"))},
    {"name": "notice", "label": "提示", "category": "display", "version": 1, "children": False, "bindings": [],
     "props": _props(text=_long("提示文字"), tone=_enum("样式", info="说明", warn="提醒", error="错误"))},
    {"name": "field", "label": "字段", "category": "display", "version": 1, "children": False,
     "bindings": ["input", "output", "run"], "props": _props(label=_text("名称"))},
    {"name": "status", "label": "状态", "category": "display", "version": 1, "children": False,
     "bindings": ["run"], "props": _props(label=_text("名称"))},
    # ---- 输入 ----
    {"name": "form", "label": "表单", "category": "input", "version": 1, "children": False, "bindings": [],
     "props": _props(form={"type": "string", "title": "表单"}, submit_label=_text("提交按钮文字"))},
    # ---- 流程 ----
    {"name": "entry_button", "label": "发起按钮", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(entry={"type": "string", "title": "入口"}, label=_text("按钮文字"))},
    {"name": "instance_list", "label": "实例列表", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(entry={"type": "string", "title": "入口"},
                                     who=_enum("显示谁的", mine="我发起的", all="全部实例"),
                                     limit={"type": "integer", "minimum": 1, "maximum": 200, "title": "最多显示几条"})},
    {"name": "instance_header", "label": "实例概况", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props()},
    {"name": "instance_timeline", "label": "实例进度", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props()},
    {"name": "task_list", "label": "待办列表", "category": "process", "version": 1, "children": False,
     "bindings": [], "props": _props(scope=_enum("范围", mine="派给我的", instance="本实例的"))},
    {"name": "task_form", "label": "待办表单", "category": "process", "version": 1, "children": False,
     "bindings": ["task"], "props": _props()},
    {"name": "signoffs", "label": "会签进度", "category": "process", "version": 1, "children": False,
     "bindings": ["task"], "props": _props()},
    {"name": "action_status", "label": "动作状态", "category": "process", "version": 1, "children": False,
     "bindings": ["action"], "props": _props(label=_text("名称"))},
]

BY_NAME: dict[str, dict[str, Any]] = {c["name"]: c for c in COMPONENTS}


def catalog_payload() -> dict[str, Any]:
    """GET /api/apps/catalog 的返回：组件目录 + 类别名。助手生成时也给模型看这一份（只给名字、中文名、props、绑定）。"""
    return {"version": 1, "categories": CATEGORY_LABEL, "components": COMPONENTS}
