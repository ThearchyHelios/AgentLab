"""这一段执行的截止时刻：核查节点据此在运行的时长上限之内收尾，不让整次运行被中止。

runner 给每一段执行（两次人工介入之间的那一段）套一道 asyncio.timeout(max_run_seconds)，到点整次运行判失败。核查
节点一个就能把这一段的时间用光：反方审稿逐句串行反查，默认 5 句、每句 90 秒再加 30 秒收尾宽限，最坏就是 600 秒；双盲
复算每项 120 秒加宽限，指标多了也一样。被中止时节点里装着反查、盲算的那个 @task 还没做完、没进 checkpoint，「继续
运行」把它从头再做一遍，多花一份钱，再次超时。

所以 runner 进入时长上限时把截止时刻放进这里的 ContextVar（segment），退出时清掉：节点、节点里的 @task、子工作流都
在这个上下文里执行，都看得到。核查节点每开始一项（一句反查、一项盲算）之前问 allot()：给下游留出余量之后还够不够
再做一项，这一项的时长上限收紧到多少；不够就不再开始，剩下的照各自「没能核查」的路径记下来，节点正常收尾。决定都
发生在节点自己的 @task 里，接着运行时取 checkpoint 里的结果，重放确定。

截止时刻用事件循环的钟（loop.time()），和 asyncio.timeout 同一个钟。没有上限时（不经 runner 执行的节点：单元测试、
审批质询接口）为 None，allot() 原样放行，节点的行为和没有这道检查时一样。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass

#: 给下游节点留的余量：核查节点后面通常还有成果节点（出具契约复核报告），也可能还有写报告、发通知这类节点。核查把
#: 时间用到最后一刻，后面的节点一开始就撞上时长上限，整次运行照样被中止。取 RESERVE_S 和这一段总时长的 RESERVE_SHARE
#: 中大的：60 秒够成果节点复核、再做一两次普通的模型调用；管理员调大了时长上限的，多半是因为节点本来就慢，余量跟着放大
RESERVE_S = 60.0
RESERVE_SHARE = 0.1


@dataclass(frozen=True)
class Segment:
    """这一段执行：截止时刻（事件循环时间）和总时长（秒，说明里写、按它算余量）。"""

    deadline: float
    seconds: float


_SEGMENT: ContextVar[Segment | None] = ContextVar("agentlab_segment", default=None)


@contextlib.asynccontextmanager
async def segment(deadline: float | None, seconds: float) -> AsyncIterator[None]:
    """runner 用：这一段执行的截止时刻（asyncio.Timeout.when()）和总时长，退出时清掉。写成异步的上下文管理器，好和
    时长上限写在同一个 async with 里。deadline 为 None（没有上限）时什么都不放。"""
    token = _SEGMENT.set(Segment(float(deadline), float(seconds)) if deadline is not None else None)
    try:
        yield
    finally:
        _SEGMENT.reset(token)


def current() -> Segment | None:
    """这一段执行的截止时刻；不在 runner 的执行里时为 None。"""
    return _SEGMENT.get()


def reserve(seconds: float) -> float:
    """给下游节点留的余量（秒），理由见 RESERVE_S。"""
    return max(RESERVE_S, RESERVE_SHARE * seconds)


def _now() -> float:
    return asyncio.get_running_loop().time()


@dataclass(frozen=True)
class Allot:
    """allot() 的答复。start：够不够再开始一项。timeout_s：这一项的时长上限，没收紧时就是传进来的那个值（不限时为
    None）。tightened：按剩余时间收紧过（比配置的短，或者配置的是不限）。seconds：这一段的总时长，不再开始、收紧过的
    时候说明里写；没有上限时为 None。"""

    start: bool
    timeout_s: float | None
    tightened: bool = False
    seconds: float | None = None


def allot(timeout_s: float | None, *, grace: float) -> Allot:
    """开始一项核查之前问一句：够不够，这一项的时长上限收紧到多少。timeout_s 是这一项配置的时长上限（None 不限），
    grace 是到点之后收尾那次调用的宽限（verify_agent.SETTLE_GRACE_S）。

    一项最坏要用「时长上限 + 收尾宽限」。剩下的时间先扣掉给下游的余量、再扣掉收尾宽限，就是这一项查库最多能用的时长：
    比配置的短就收紧到它。收紧之后不到 min(配置的时长上限, 收尾宽限) 就不再开始：收尾宽限是留给一次模型调用的时间，
    查库的时长比它还短，连一轮都做不完，只会白花一次收尾的钱、记成没能核查。配置的时长上限本来就比它短的，按配置的
    整段算。没有上限时原样放行。"""
    found = _SEGMENT.get()
    if found is None:
        return Allot(True, timeout_s)
    room = found.deadline - _now() - reserve(found.seconds) - grace
    limit = room if timeout_s is None else min(timeout_s, room)
    floor = grace if timeout_s is None else min(timeout_s, grace)
    if limit < floor:
        return Allot(False, None, seconds=found.seconds)
    return Allot(True, limit, tightened=timeout_s is None or room < timeout_s, seconds=found.seconds)


__all__ = ["RESERVE_S", "RESERVE_SHARE", "Allot", "Segment", "allot", "current", "reserve", "segment"]
