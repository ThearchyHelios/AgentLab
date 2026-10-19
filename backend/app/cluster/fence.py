"""自我隔离（fencing）：本进程续不上租约的时候，当作自己认领的行可能已经被别的进程接手，停下手上的执行、不再写。

**为什么。** 进程卡住了但没死（SIGSTOP、虚拟机暂停、事件循环被长同步代码卡住、库一时连不上）：别的进程看它的租约过期了，把它在跑
的运行标中断、交给别人继续运行。它一缓过来，手上的执行接着往下走：照样写事件（和接手的那一边撞号）、照样写终态（把接手的结果
盖掉）。所以：

- 续租约在单独的任务、单独的连接上做（cluster/service.py），不被同一拍别的活拖住；最近一次续上的时刻记在这里（renewed）。
- 本进程的执行在动手写之前问一句 check()：离上次续上已经超过一个租约时长（别的进程按两个租约时长才接手，中间留着一段），或者这段
  执行是在上一次隔离之前开始的（世代不对），就抛 Fenced——写事件、写断点、写终态、写待办和动作、发外部写入之前都问。只对绑定了
  世代的执行（运行的一段执行、后台任务）抛；别的调用者问到时只隔离、不抛。
- 一旦发现续晚了（trip）：世代加一，手上的执行全部停掉（停的时候不写终态，那些行交给接手的进程按「属主不在了」处理），换一个新的
  身份重新登记；新身份登记上之前新开始的执行先等着（ready），登记上之后照常。

**用哪只钟（复核 R-b7）。** 别的进程判「租约过期」用的是库的时钟（cluster/workers：续租写 lease_until = 库的 now() + 租约），
跟各台机器的墙钟差多少无关。这边判「续晚了」用计入休眠的钟：time.monotonic() 在 macOS（mach_absolute_time）、Linux
（CLOCK_MONOTONIC）上都不计休眠——笔记本合盖、虚拟机挂起超过两个租约，对面早已接手，这边还以为刚续过。所以 Linux 用
CLOCK_BOOTTIME，macOS 用 clock_gettime(CLOCK_MONOTONIC)（这个计入休眠），都取不到时退回 monotonic；不论哪种，另外同时比墙钟，
两只钟里走得多的那只说过了一个租约就算续晚了。续租时再比一下两只钟（jumped）：差出一个租约以上（墙钟跳了、或者计入休眠的钟
其实没计）就隔离。续租那一句本身也按库的时钟核对：库里自己的租约已经过期（或者登记不见了），就不再续这个身份。

Fenced 是 BaseException：节点、工具里常见的 except Exception 拦不住它，一路退到执行的最外层。没起协调（测试、脚本）时什么都不查。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Fenced(BaseException):
    """本进程续不上租约了（或者这段执行属于上一次隔离之前）：不许再写。"""


def _sleep_aware_clock() -> tuple[Callable[[], float], bool]:
    """计入休眠的单调钟，和它是不是真的计入休眠。"""
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return (lambda: time.clock_gettime(time.CLOCK_BOOTTIME)), True
    if sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        return (lambda: time.clock_gettime(time.CLOCK_MONOTONIC)), True
    return time.monotonic, False


_clock, SLEEP_AWARE = _sleep_aware_clock()
#: 墙钟（单独一个名字，测试里拨它）
_wall: Callable[[], float] = time.time

_generation = 0
_renewed: float | None = None
_renewed_wall: float | None = None
_active = False
_handlers: list[Callable[[], Any]] = []
_bound: contextvars.ContextVar[int | None] = contextvars.ContextVar("agentlab_fence_generation", default=None)


def now() -> float:
    """计入休眠的单调钟的读数（本进程里判时长都用它：cluster/leader 判还当不当班也是）。"""
    return _clock()


def activate() -> None:
    """起协调、续上了第一次租约时（PostgreSQL；SQLite 只有一个进程，不用隔离）。"""
    global _active
    _active = True
    renewed()


def deactivate() -> None:
    global _active, _renewed, _renewed_wall
    _active, _renewed, _renewed_wall = False, None, None


def renewed(*, ago: float = 0.0) -> None:
    """续上了一次租约（ago：其实是多少秒以前续上的，测试里装「卡了一阵」用）。"""
    global _renewed, _renewed_wall
    _renewed, _renewed_wall = _clock() - ago, _wall() - ago


def _since() -> tuple[float, float] | None:
    """离上次续上：(计入休眠的钟走了多少, 墙钟走了多少)。"""
    if _renewed is None or _renewed_wall is None:
        return None
    return _clock() - _renewed, _wall() - _renewed_wall


def generation() -> int:
    return _generation


def bind() -> None:
    """这段执行属于现在这一世代（运行的一段执行、后台任务、问数据的一轮开始时调）。"""
    _bound.set(_generation)


def stale() -> bool:
    """离上次续上租约是不是已经超过一个租约时长：两只钟（计入休眠的单调钟、墙钟）里走得多的那只说了算。隔离之后、新身份还没
    登记上的那一段也算（pending）。"""
    if not _active:
        return False
    since = _since()
    if since is None:
        return True
    from app.cluster.workers import lease_s

    return max(since) > lease_s()


def pending() -> bool:
    """隔离过、换的新身份还没登记上。"""
    return _active and _renewed is None


async def ready(timeout: float | None = None) -> None:
    """新开始的一段执行（运行、后台任务）在 bind() 之前等一等：隔离之后、新身份还没登记上时，它认领的行在别的进程眼里属主
    不在，会被当成没人管接手（复核 R-b9）。最多等一个租约时长（timeout），等不到抛 Fenced——不开始，那几行由别的进程照「属主
    不在了」处理（cluster/service 那边同时会放弃这个一直没登记上的身份）。"""
    if not pending():
        return
    from app.cluster.workers import lease_s

    deadline = _clock() + (lease_s() if timeout is None else timeout)
    while pending():
        if _clock() >= deadline:
            raise Fenced()
        await asyncio.sleep(0.05)


def jumped() -> bool:
    """两只钟差出一个租约以上（墙钟往前或往后跳了，或者单调钟没计入休眠）：续租时问，是就隔离。"""
    if not _active:
        return False
    since = _since()
    if since is None:
        return False
    from app.cluster.workers import lease_s

    return abs(since[0] - since[1]) > lease_s()


def fenced_here() -> bool:
    """当前这段执行还能不能写：世代不对（隔离之前开始的）或者租约续晚了。只说绑定了世代的执行（bind 过的）。"""
    if not _active:
        return False
    bound = _bound.get()
    return bound is not None and (bound != _generation or stale())


def check() -> None:
    """写之前问一句。续晚了就隔离（谁先问到都一样）；抛 Fenced 只对绑定了世代的执行（运行的一段执行、后台任务：bind 过的）——
    别的调用者（HTTP 处理、调度那一拍、进程间请求）不是认领了行在跑的执行，抛给它们只会把调度循环之类的打停（复核 R-b9）。"""
    if not _active:
        return
    if _renewed is not None and stale():
        trip("执行要写的时候发现租约已经续晚了")
    bound = _bound.get()
    if bound is not None and (bound != _generation or stale()):
        raise Fenced()


def on_trip(handler: Callable[[], Any]) -> None:
    if handler not in _handlers:
        _handlers.append(handler)


def trip(why: str) -> bool:
    """隔离：世代加一，叫各处停掉手上的执行（cluster/service 再换身份重新登记）。已经在这一世代隔离过的不重复。交回这一次有没有动手。"""
    global _generation, _renewed, _renewed_wall
    if not _active or _renewed is None:
        return False
    _generation += 1
    _renewed = _renewed_wall = None
    logger.error("本进程的租约续晚了（%s）：当作认领的运行、问数据、后台任务可能已被别的进程接手，停掉手上的执行、不写终态，"
                 "换一个身份重新登记", why)
    for handler in list(_handlers):
        try:
            handler()
        except Exception:  # noqa: BLE001
            logger.exception("隔离时停执行出错")
    return True


__all__ = ["Fenced", "SLEEP_AWARE", "activate", "bind", "check", "deactivate", "fenced_here", "generation", "jumped", "now",
           "on_trip", "pending", "ready", "renewed", "stale", "trip"]
