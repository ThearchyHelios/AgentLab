"""定时任务的后台调度：到点按定时任务的设置发起工作流运行，结束、失败、停在审批时推送 Webhook。

- cron.py：五字段 cron 的解析、下次触发、中文描述
- service.py：输入模板、和工作流入口字段的对照、发起一次触发（调度循环和「立即运行」共用）
- loop.py：调度循环，条件 UPDATE 认领到点的任务
- webhook.py：通知推送（通用 / 飞书 / 企业微信 / 钉钉）

服务启动时（main.py 的 lifespan）只在 scheduler_enabled() 时调 start，关闭时总是调 stop——stop 要能在没 start
过时安全返回。start 自己也看一次开关：拷来的数据目录（检查脚本的 --stacks、验收副本）不管从哪条路径起服务，
都不会替原库把定时任务再执行一遍。
"""
from __future__ import annotations

import logging

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)


def scheduler_enabled(config: Settings | None = None) -> bool:
    """调度循环开不开：AGENTLAB_SCHEDULER，默认开，0 / false / off 关（见 core/config.py）。

    config 只给测试用：settings 是模块级单例，改环境变量不会让它重读。"""
    return bool((config or settings).scheduler)


async def start(*, interval: float | None = None, config: Settings | None = None) -> bool:
    """起调度循环，返回起没起。开关关着时什么都不做；已经在跑时不起第二个。interval 只给测试用。"""
    if not scheduler_enabled(config):
        logger.info("定时任务调度已关闭（AGENTLAB_SCHEDULER），不会自动发起运行")
        return False
    from app.scheduler.loop import TICK_SECONDS, runner

    await runner.start(interval if interval is not None else TICK_SECONDS)
    return True


async def stop() -> None:
    """停调度循环，等手上的那一轮收尾。没 start 过也能安全调用。"""
    from app.scheduler.loop import runner

    await runner.stop()


def running() -> bool:
    from app.scheduler.loop import runner

    return runner.running()
