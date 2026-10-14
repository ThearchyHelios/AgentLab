"""应用入口：按入口表单校验 → 映射成钉住版本的运行输入 → 发起正式运行（来源 app_entry / hook）。"""
from __future__ import annotations

from typing import Any


async def start_entry(app_id: str, entry_id: str, values: dict[str, Any], principal: Any, *,
                      origin_kind: str = "app_entry", hook: str | None = None) -> Any:
    """发起一个实例，返回 Run。校验失败抛 FormRejected(errors)；应用需升级（钉住的版本已退役）时拒绝。"""
    raise NotImplementedError("BE-3")


class FormRejected(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("表单没有通过校验")
        self.errors = errors
