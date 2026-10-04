"""定时任务的后台调度：到点按定时任务的设置发起工作流运行。

目前 start / stop 都是空的，调度循环由定时任务的工作包补上。服务启动时（main.py 的 lifespan）
只在 scheduler_enabled() 时调 start，关闭时总是调 stop——stop 要能在没 start 过时安全返回。
"""
from __future__ import annotations

from app.core.config import Settings, settings


def scheduler_enabled(config: Settings | None = None) -> bool:
    """调度循环开不开：AGENTLAB_SCHEDULER，默认开，0 / false / off 关（见 core/config.py）。

    config 只给测试用：settings 是模块级单例，改环境变量不会让它重读。"""
    return bool((config or settings).scheduler)


async def start() -> None:
    """起调度循环。调度循环在后续工作包里实现，现在什么都不做。"""


async def stop() -> None:
    """停调度循环，等手上的那一轮收尾。现在什么都不做；没 start 过也要能安全调用。"""
