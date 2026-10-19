"""本进程手上的「别的进程谁还活着」：心跳每一拍刷新一次；碰到不认识的属主（刚起来的进程）当场再查一次库。"""
from __future__ import annotations

import time
from collections.abc import Iterable

from app.cluster import identity
from app.cluster.workers import Liveness, heartbeat_s, liveness

_snapshot: tuple[float, Liveness] | None = None


def remember(live: Liveness) -> None:
    global _snapshot
    _snapshot = (time.monotonic(), live)


def forget() -> None:
    global _snapshot
    _snapshot = None


async def current(*, fresh: bool = False) -> Liveness:
    """最近的一份（不超过一拍心跳），fresh 时当场查库。"""
    if not fresh and _snapshot is not None and time.monotonic() - _snapshot[0] < heartbeat_s():
        return _snapshot[1]
    live = await liveness()
    remember(live)
    return live


async def live_for(owners: Iterable[str | None]) -> Liveness:
    """判断这几个属主用的一份：里面有手上这份不认识的别的进程，就当场查一次库（它可能刚起来）。"""
    live = await current()
    me = identity.owner()
    if any(o and o != me and o not in live.others for o in owners):
        live = await current(fresh=True)
    return live


__all__ = ["current", "forget", "live_for", "remember"]
