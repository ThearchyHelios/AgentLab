"""AGENTLAB_SCHEDULER：定时任务的调度循环开不开。

默认开；0 / false / off 关。检查脚本的 --stacks 拷一份数据目录另起服务时靠它关掉，
否则拷贝里的定时任务会替原库再发起一遍运行、再发一遍 webhook。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import scheduler
from app.core.config import Settings


def flag(monkeypatch, value: str | None) -> bool:
    if value is None:
        monkeypatch.delenv("AGENTLAB_SCHEDULER", raising=False)
    else:
        monkeypatch.setenv("AGENTLAB_SCHEDULER", value)
    # 不读仓库里的 .env：测的是环境变量本身的解析
    return scheduler.scheduler_enabled(Settings(_env_file=None))


@pytest.mark.parametrize("value", [None, "", "  ", "1", "true", "on", "TRUE", "yes"])
def test_enabled_by_default_and_when_switched_on(monkeypatch, value):
    assert flag(monkeypatch, value) is True


@pytest.mark.parametrize("value", ["0", "false", "off", "False", "OFF", "no"])
def test_switched_off(monkeypatch, value):
    assert flag(monkeypatch, value) is False


def test_unrecognized_value_fails_at_startup(monkeypatch):
    """认不出的写法不猜开还是关：启动时就报错"""
    with pytest.raises(ValidationError):
        flag(monkeypatch, "maybe")


async def test_start_and_stop_are_safe_noops():
    """测试环境关着调度（conftest）：start 不起循环，没 start 过直接 stop 也不出错。开着时的循环见 test_scheduler_loop"""
    await scheduler.stop()
    await scheduler.start()
    await scheduler.stop()
