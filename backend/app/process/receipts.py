"""按幂等键去重的收件记录（docs/design/next-round/04-对外集成与触达.md A4）：入站钩子（hook_receipts）和访问密钥
（api_receipts）共用这一套状态机，各存各的表（Book 按表参数化）。

一条记录按（谁, 幂等键）唯一：钩子按钩子，访问密钥按拥有者（轮换了密钥，同一个键照样认得出是同一件事）。
- claim：先插一行 processing。插得进去就是头一回，交回 (None, False)；插不进去（撞了唯一约束）看已有的那一行：
  - accepted：交回上次的结果（duplicate: true），不再处理；
  - rejected：重新认领。被拒的（表单不对、没有在等的待办）可以用同一个键重试——外部系统多半会原样重发，拒过一次就永远拒，
    它只能换键，去重也就落空了；
  - processing 超过 STALE_S：处理一次请求用不了这么久，多半是中途服务停了，重新认领，交回 reclaimed=True。调用方先找这个键
    已经办成的事（已经发起的实例、已经解决的待办），找到就交回它，不重复办；
  - processing 没超时：正在处理，抛 ReceiptBusy（调用方回 409）。
  重新认领用条件 UPDATE（状态、收到时间都对得上才改）：并发的两次只有一次认领得到。
- finish：处理完写结果（accepted 带运行或待办，rejected 带原因）。

有指纹的表（api_receipts.fingerprint）另核对同一个键办的是不是同一件事：办成过的、还在办的，指纹对不上抛 ReceiptMismatch，
不把上一件事的结果当成这一件的交回去；被拒过的不算办过，换了内容重试照常认领（指纹换成这一次的）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal, utcnow

#: 回执停在 processing 超过这么久（秒），同一个幂等键可以重新认领
STALE_S = 600
#: 幂等键最长存多少字
KEY_MAX = 200


class ReceiptBusy(Exception):
    """同一个幂等键的请求正在处理（调用方回 409）。"""


class ReceiptMismatch(Exception):
    """同一个幂等键已经用来办过另一件事（指纹对不上，调用方回 422）。"""


@dataclass(frozen=True)
class Book:
    """一张收件记录表：model 是表，owner 是「谁」那一列的名字（hook_id / owner_user_id）。表里要有 idem_key、status、
    detail、run_id、task_id、received_at；fingerprint 列可有可无。"""

    model: Any
    owner: str
    stale_s: int = STALE_S

    def _key(self, owner: str, idem_key: str) -> list[Any]:
        return [getattr(self.model, self.owner) == owner, self.model.idem_key == idem_key]

    async def claim(self, owner: str, idem_key: str, *, fingerprint: str | None = None,
                    extra: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, bool]:
        """按（谁, 幂等键）认领这一次，交回 (上次的结果, 是不是重新认领了一条停在处理中的)。规则见模块说明。extra 是
        认领时一并记下的列（访问密钥记哪一把密钥、哪个接口）。"""
        fields = {**(extra or {}), **({"fingerprint": fingerprint} if fingerprint is not None else {})}
        async with SessionLocal() as session:
            session.add(self.model(**{self.owner: owner}, idem_key=idem_key, status="processing", **fields))
            try:
                await session.commit()
                return None, False
            except IntegrityError:
                await session.rollback()
            row = await session.scalar(select(self.model).where(*self._key(owner, idem_key)))
            if row is None:
                # 撞了约束、回头又查不到：那一行刚被删掉（钩子被删除时连同记录一起删）。当成正在处理，让调用方稍后重发
                raise ReceiptBusy
            known = getattr(row, "fingerprint", None)
            if fingerprint is not None and known and known != fingerprint and row.status != "rejected":
                raise ReceiptMismatch
            if row.status == "accepted":
                return {"status": "accepted", "duplicate": True, "run_id": row.run_id, "task_id": row.task_id}, False
            stale = row.status == "processing" and row.received_at < utcnow() - timedelta(seconds=self.stale_s)
            if row.status == "rejected" or stale:
                again = await session.execute(update(self.model).where(
                    self.model.id == row.id, self.model.status == row.status,
                    self.model.received_at == row.received_at)
                    .values(status="processing", detail=None, received_at=utcnow(), **fields))
                await session.commit()
                if again.rowcount == 1:
                    return None, stale
        raise ReceiptBusy

    async def finish(self, owner: str, idem_key: str, *, status: str, detail: str | None = None,
                     run_id: str | None = None, task_id: str | None = None) -> None:
        async with SessionLocal() as session:
            await session.execute(update(self.model).where(*self._key(owner, idem_key))
                                  .values(status=status, detail=detail, run_id=run_id, task_id=task_id))
            await session.commit()
