"""个人应用（N7、§3.9）：使用者自己的应用。图存在蓝图上（不进工作流表）、只有拥有者看得见、永远跑探索运行
（origin_kind=personal_app，强加 MASK_RESULTS / READ_ONLY）、受每日上限约束；想给别人用就提交审核，搭建者在共享副本上发布。

包 B4 实现。
"""
from __future__ import annotations

from typing import Any

#: 管理员开关：使用者能不能建个人应用（apps 设置组里，默认关）
SETTING_KEY = "user_personal_apps"


async def save_personal(session: Any, blueprint: Any, *, principal: Any) -> Any:
    """校验图满足 chat_policy（节点、工具、无人工、无写入），建或更新 scope=personal 的 App。"""
    raise NotImplementedError


async def refresh(session: Any, app: Any, *, principal: Any) -> str:
    """发起一次个人应用的刷新（探索运行），交回 run_id；受每日上限约束。"""
    raise NotImplementedError


async def submit_review(session: Any, app: Any, *, to: list[dict[str, str]], principal: Any) -> Any:
    """开一条 review 待办派给搭建者。"""
    raise NotImplementedError


async def copy_for_review(session: Any, blueprint: Any, *, principal: Any) -> Any:
    """搭建者处理审核时得到这个蓝图的共享副本（copied_from 指向原蓝图），原个人应用不动。"""
    raise NotImplementedError
