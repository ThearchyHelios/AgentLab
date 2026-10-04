"""往一次停下的运行里追加事件：运行停在审批时的审批质询、封存之后的批注。

事件流是封存的依据：清单按 seq 排序算哈希，而 run_events 的 (run_id, seq) 没有唯一约束——两条事件撞了号，
排序就说不准，封存核对时对时不对。所以追加和 runner 发事件守同一套序号：接在库里最后一条后面，从 bus 取号
（同一进程里和执行任务共用一个计数器），写事件和条件更新 last_seq 在同一个事务里，谁先提交算谁的。做法同
api/evidence.py 的 _append_judged（按需裁判的判定）。

require 说明往哪种运行里追加：
- "interrupted"：运行停在审批、还没封存。事件落在以后的封存范围里，随运行结束一起封存。
- "sealed"：已经封存的终态运行。事件带 post_seal: true，落在 manifest_seq 之后，不进封存范围，verify 照旧一致。

没抢到（读完运行之后它被恢复、被放弃、继续运行后重新封存了，或者别处刚往里写过事件）就一条都不写、返回 None，
调用方据此说明「未写入运行记录」。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from sqlalchemy import func, select, update

from app.core.bus import bus
from app.core.events import EventType, RunEventModel
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import _seq_floor, _stage, run_manager

#: 运行的终态。封存过的运行只会停在这几种上
_TERMINAL = ("succeeded", "failed", "cancelled")
#: 宣布运行结束的事件。封存盖住了最后一条这种事件，才是这次结束之后封的（见 _current_seal）
_ENDINGS = (EventType.RUN_FINISHED, EventType.RUN_FAILED, EventType.RUN_CANCELLED)

#: 一条要追加的事件：(事件类型, node_id, 数据)
Item = tuple[EventType | str, str | None, dict[str, Any] | None]


class _RunLocks:
    """每次运行一把锁，没人用了就扔：同一次运行的追加在本进程里排队，读 last_seq 到条件更新之间不会被
    另一次追加插进来——插进来的话后提交的那次条件更新落空，明明没有冲突也成了「未写入」。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key], self._locks[key]


_APPENDING = _RunLocks()


async def append_events(
    run_id: str,
    items: Sequence[Item],
    *,
    require: Literal["interrupted", "sealed"],
    seal: tuple[int | None, str | None] | None = None,
) -> list[RunEventModel] | None:
    """把 items 依次接在运行记录的最后一条后面，返回写入的事件（seq 严格递增）；没写入返回 None。

    seal 只用于 "sealed"：调用方开始做事时看到的 (manifest_seq, manifest_hash)。做的事要花几秒（问模型），
    这期间继续运行、重新封存了的话，结果依据的已经不是现在的封存，不追加。不传就按这次读到的封存。
    items 为空时什么都不做，返回空列表。
    """
    if require not in ("interrupted", "sealed"):
        raise ValueError(f"require 只能是 interrupted 或 sealed，收到的是 {require!r}")
    # 事件类型先验过：写错了在取号之前就报出来
    entries = [(EventType(str(kind)), node_id, dict(data or {})) for kind, node_id, data in items]
    if not entries:
        return []
    sealed = require == "sealed"
    for _, _, data in entries:
        if sealed:
            data["post_seal"] = True
        else:
            # 停在审批时追加的事件要进封存范围，不能自称在封存之后
            data.pop("post_seal", None)

    async with _APPENDING.hold(run_id):
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return None
            if sealed:
                if run.status not in _TERMINAL or not run.manifest_hash \
                        or (seal is not None and (run.manifest_seq, run.manifest_hash) != tuple(seal)) \
                        or not await _current_seal(session, run):
                    return None
                guard = (Run.status.in_(_TERMINAL), _same(Run.manifest_seq, run.manifest_seq),
                         _same(Run.manifest_hash, run.manifest_hash))
            else:
                if run.status != "interrupted":
                    return None
                guard = (Run.status == "interrupted",)
            last = run.last_seq
            # 进程重启过的话内存里的序号从 0 起：接在库里最后一条后面。last_seq 按说就是最后一条，_seq_floor
            # 另取一次实际的最大序号兜底——别处的追加把 last_seq 写小了，也不至于在这里撞号
            bus.set_seq(run_id, await _seq_floor(session, run_id, last))
            events = [run_manager._event(run_id, kind, node_id=node_id, data=data)
                      for kind, node_id, data in entries]
            # 核对和写入是同一行上的条件更新，和恢复、放弃、继续运行（runner._claim / _abandon）抢的是同一行
            claimed = await session.execute(
                update(Run).where(Run.id == run_id, _same(Run.last_seq, last), *guard)
                .values(last_seq=events[-1].seq))
            if claimed.rowcount != 1:
                # 读完运行之后它变了。烧掉几个序号不要紧：不落库的事件（流式增量）本来就占序号，
                # 序号只要求递增、不撞号
                await session.rollback()
                return None
            for event in events:
                await _stage(session, event, run)
            await session.commit()
    # 先落库再广播，理由同 RunManager._emit
    for event in events:
        await bus.publish(event)
    return events


def _same(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


async def _current_seal(session: Any, run: Run) -> bool:
    """运行记录上的封存是不是这次结束之后封的：manifest_seq 盖住了最后一条宣布结束的事件。

    继续运行之后再次结束时，终态和终态事件先提交、封存后算。这中间运行记录上还是上一次结束时的封存，
    当成现在的封存去追加，事件会被接下来的封存收进去，post_seal 就成了假话。manifest_seq 为空的是加这一列
    之前封存的老运行，当时的封存范围按 verify_manifest 的老口径算，这里不拦。
    """
    if run.manifest_seq is None:
        return True
    ended = await session.scalar(
        select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id,
                                             RunEvent.type.in_([str(t) for t in _ENDINGS])))
    return ended is None or run.manifest_seq >= ended
