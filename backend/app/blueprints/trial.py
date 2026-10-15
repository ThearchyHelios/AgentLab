"""试运行（N4、§3.5）：跑一次还没发布的图（探索运行，origin_kind=trial），草稿应用对着这次运行解析
（apps/resolve.resolve_page 的 trial 旁路），印章写「试运行」。流程类的人工步骤派给试运行的人自己，写入走测试环境或演练，
调度不对 trial 的待办做超时升级。

包 B2 实现。
"""
from __future__ import annotations

from typing import Any


async def start_trial(session: Any, blueprint: Any, *, inputs: dict[str, Any] | None, actor: Any) -> str:
    """实例化 → 保存图到蓝图 → 发起探索运行，交回 run_id。蓝图状态记 trialed、last_trial_run_id。"""
    raise NotImplementedError


async def preview_page(session: Any, blueprint: Any, *, page_id: str, run_id: str | None, principal: Any) -> dict[str, Any]:
    """按这次试运行解析应用草稿的一页（agentlab.view/1），印章 from=试运行、trial=true。"""
    raise NotImplementedError
