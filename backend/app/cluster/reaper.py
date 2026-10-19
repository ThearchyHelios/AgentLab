"""接手属主不在了的行（进程没了、租约过期）：各种东西各按各的规矩，在各自的模块里登记（register），这里按登记的先后
一个个调。

谁调：每个进程启动时调一次（那时自己手上什么都没在跑，这一次也收拾自己上一次留下的——测试里同一个进程一次次起停服务），
之后当班的进程隔一会儿（或者收到别的进程走了的通知）再调。各规矩里认领那一下都是带条件的 UPDATE（状态没变、属主还是
刚才看到的那个），几个进程同时接手同一行只有一个改得动；活着的进程认领的行一概不碰（Liveness.alive）。

登记的规矩（启动时的整理就是它们）：运行（标中断 / 排队的标失败，不自动重跑）、问数据的一轮（建图中的标中断；发起了运行的
接着等、照常复核收尾）、数据目录起草 / 剖析 / 探查、评测、影子起草（标中断）、文档处理（标失败）、发布单（改成停下）……
见各模块的 reap / startup。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.cluster.workers import Liveness

logger = logging.getLogger(__name__)

Reaper = Callable[[Liveness], Awaitable[Any]]
_REAPERS: list[tuple[str, Reaper]] = []


def register(name: str, fn: Reaper) -> Reaper:
    """登记一种接手规矩（同名的换成新的：模块重新导入时不重复登记）。"""
    for i, (known, _) in enumerate(_REAPERS):
        if known == name:
            _REAPERS[i] = (name, fn)
            return fn
    _REAPERS.append((name, fn))
    return fn


def names() -> list[str]:
    return [name for name, _ in _REAPERS]


async def reap(live: Liveness) -> dict[str, Any]:
    """按登记的先后调一遍，交回各自整理了多少。一种出错不影响后面的。"""
    out: dict[str, Any] = {}
    for name, fn in list(_REAPERS):
        try:
            got = await fn(live)
        except Exception:  # noqa: BLE001
            logger.exception("接手不在了的进程留下的「%s」出错，下一次再试", name)
            continue
        if got:
            out[name] = got
    if out:
        logger.info("接手了不在了的进程留下的：%s", out)
    return out


__all__ = ["Reaper", "names", "reap", "register"]
