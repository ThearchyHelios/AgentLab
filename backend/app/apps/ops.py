"""应用操作：编辑器和助手润色共用（L1 的 AppApplier 调这里）。

操作（一行一个 JSON）：
  {"op":"app_add","parent":"<组件 id>|<页面 id>","index":0,"component":{...}}
  {"op":"app_update","id":"<组件 id>","props":{...},"bind":{...},"visible":"..."}
  {"op":"app_remove","id":"<组件 id>"}
  {"op":"app_move","id":"<组件 id>","parent":"...","index":0}
  {"op":"app_page","page":{"id","title"}}                    新增或改页面标题
  {"op":"app_set","path":"entries|views|forms|slots|task_ui","key":"…","value":{...}}
"""
from __future__ import annotations

from typing import Any

# 落地结果和图操作共用一个形状（L1 的 Applier 接口）
from app.look.base import Applied  # noqa: F401


def apply_app_op(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    """把一个应用操作应用到 spec（原地修改）。认不出、目标不存在的返回 Applied(False, False, problem)。"""
    raise NotImplementedError("BE-3")
