"""自我隔离（fencing）：本进程续不上租约的时候，当作自己认领的行可能已经被别的进程接手，停下手上的执行、不再写。

**为什么。** 进程卡住了但没死（SIGSTOP、虚拟机暂停、事件循环被长同步代码卡住、库一时连不上）：别的进程看它的租约过期了，把它在跑
的运行标中断、交给别人继续运行。它一缓过来，手上的执行接着往下走：照样写事件（和接手的那一边撞号）、照样写终态（把接手的结果
盖掉）。所以：

- 续租约在单独的任务、单独的连接上做（cluster/service.py），不被同一拍别的活拖住；最近一次续上的时刻记在这里（renewed）。
- 本进程的执行在动手写之前问一句 check()：离上次续上已经超过一个租约时长（别的进程按两个租约时长才接手，中间留着一段），或者这段
  执行是在上一次隔离之前开始的（世代不对），就抛 Fenced——写事件、写断点、写终态、写待办和动作、发外部写入之前都问。
- 一旦发现续晚了（trip）：世代加一，手上的执行全部停掉（停的时候不写终态，那些行交给接手的进程按「属主不在了」处理），换一个新的
  身份重新登记，之后发起的照常。

Fenced 是 BaseException：节点、工具里常见的 except Exception 拦不住它，一路退到执行的最外层。没起协调（测试、脚本）时什么都不查。
"""
from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Fenced(BaseException):
    """本进程续不上租约了（或者这段执行属于上一次隔离之前）：不许再写。"""


_generation = 0
_renewed: float | None = None
_active = False
_handlers: list[Callable[[], Any]] = []
_bound: contextvars.ContextVar[int | None] = contextvars.ContextVar("agentlab_fence_generation", default=None)


def activate(renewed_at: float | None = None) -> None:
    """起协调、续上了第一次租约时（PostgreSQL；SQLite 只有一个进程，不用隔离）。"""
    global _active, _renewed
    _active = True
    _renewed = time.monotonic() if renewed_at is None else renewed_at


def deactivate() -> None:
    global _active, _renewed
    _active, _renewed = False, None


def renewed(at: float | None = None) -> None:
    """续上了一次租约。"""
    global _renewed
    _renewed = time.monotonic() if at is None else at


def generation() -> int:
    return _generation


def bind() -> None:
    """这段执行属于现在这一世代（运行的一段执行、后台任务、问数据的一轮开始时调）。"""
    _bound.set(_generation)


def stale(now: float | None = None) -> bool:
    """离上次续上租约是不是已经超过一个租约时长。"""
    if not _active or _renewed is None:
        return False
    from app.cluster.workers import lease_s

    return (time.monotonic() if now is None else now) - _renewed > lease_s()


def fenced_here() -> bool:
    """当前这段执行还能不能写：世代不对（隔离之前开始的）或者租约续晚了。"""
    if not _active:
        return False
    bound = _bound.get()
    return (bound is not None and bound != _generation) or stale()


def check() -> None:
    """写之前问一句：不许写就抛 Fenced（续晚了的话顺手隔离）。"""
    if not _active:
        return
    if stale():
        trip("执行要写的时候发现租约已经续晚了")
        raise Fenced()
    bound = _bound.get()
    if bound is not None and bound != _generation:
        raise Fenced()


def on_trip(handler: Callable[[], Any]) -> None:
    if handler not in _handlers:
        _handlers.append(handler)


def trip(why: str) -> bool:
    """隔离：世代加一，叫各处停掉手上的执行（cluster/service 再换身份重新登记）。已经在这一世代隔离过的不重复。交回这一次有没有动手。"""
    global _generation, _renewed
    if not _active or _renewed is None:
        return False
    _generation += 1
    _renewed = None
    logger.error("本进程的租约续晚了（%s）：当作认领的运行、问数据、后台任务可能已被别的进程接手，停掉手上的执行、不写终态，"
                 "换一个身份重新登记", why)
    for handler in list(_handlers):
        try:
            handler()
        except Exception:  # noqa: BLE001
            logger.exception("隔离时停执行出错")
    return True


__all__ = ["Fenced", "activate", "bind", "check", "deactivate", "fenced_here", "generation", "on_trip", "renewed",
           "stale", "trip"]
