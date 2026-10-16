"""对账结果上的「说明有用 / 看不懂」（docs/design/next-round/02-口径账本.md 做法 10 的 E2）。

对账（ledger/reconcile）把这次的答案和一份相关口径在同一份数据上各算一次，结果卡片上写差在哪、差多少。看的人觉得这段
说明有没有帮他看懂，点一下「说明有用」或「看不懂」——这是 E2 里唯一要人给的数，衡量差异说明写得好不好。

- 记在哪：对账运行上的封存后事件 ledger.feedback（{verdict, by, verified, source_run}，engine/run_append，post_seal）。
  运行本身就是记录，不另建反馈表（同统计「全部现算、不另存」）；批注不是证据，封存核对照旧一致。
- 谁能给：能读这个答案的人（同发起对账，ledger/reconcile：使用者对自己的答案也能对账）；对账要是这个答案的（origin.source_run），
  而且已经跑完封存。
- 怎么数：同一个人（开登录时按用户名；关登录时署名是自报的，按署名、没署名的算同一个）改主意再记一条，只算他最后一次。
  账本接口 reconciled 每一项带上 {mine, useful, unclear}（feedback_of），统计 E2 数整个窗口（counts）。
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent

#: 两种反馈
VERDICTS = ("useful", "unclear")
#: 被拒的机器码
REFUSED = "ledger_feedback_refused"
NOT_FOUND = "这次对账不存在，或者不是这个答案的对账"
NO_ACCESS = "你没有权限查看这个答案"
NOT_SEALED = "对账还没运行完，等结果出来再评说明有没有用"
BAD_VERDICT = "反馈只有两种：说明有用、看不懂"


class FeedbackRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str = REFUSED) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def _who(principal: Any) -> str:
    return str(getattr(principal, "username", None) or "")


async def give(answer_id: str, reconcile_id: str, verdict: str, *, principal: Any) -> dict[str, Any]:
    """记一条反馈（见模块说明），交回这次对账现在的反馈 {mine, useful, unclear}。不能记的抛 FeedbackRefused。"""
    from app.api.deps import can_read_run
    from app.engine.run_append import append_events
    from app.ledger.reconcile import ORIGIN as RECONCILE

    if verdict not in VERDICTS:
        raise FeedbackRefused(422, BAD_VERDICT)
    async with SessionLocal() as session:
        answer = await session.get(Run, answer_id)
        rec = await session.get(Run, reconcile_id)
    origin = rec.origin if rec is not None and isinstance(rec.origin, dict) else {}
    if answer is None or rec is None or rec.origin_kind != RECONCILE or origin.get("source_run") != answer_id:
        raise FeedbackRefused(404, NOT_FOUND)
    if getattr(principal, "mode", "off") != "off" and not await can_read_run(principal, answer):
        raise FeedbackRefused(403, NO_ACCESS)
    data = {"verdict": verdict, "by": _who(principal) or None, "verified": bool(getattr(principal, "verified", False)),
            "source_run": answer_id}
    if not await append_events(reconcile_id, [(EventType.LEDGER_FEEDBACK, None, data)], require="sealed"):
        raise FeedbackRefused(409, NOT_SEALED)
    async with SessionLocal() as session:
        return (await feedback_of(session, [reconcile_id], principal))[reconcile_id]


async def _latest(session: Any, run_ids: Collection[str]) -> dict[str, dict[str, str]]:
    """{对账运行: {谁: 他最后一次给的}}。"""
    out: dict[str, dict[str, str]] = {}
    if not run_ids:
        return out
    rows = (await session.execute(select(RunEvent.run_id, RunEvent.data).where(
        RunEvent.run_id.in_(list(run_ids)), RunEvent.type == EventType.LEDGER_FEEDBACK).order_by(RunEvent.seq))).all()
    for run_id, data in rows:
        if isinstance(data, dict) and data.get("verdict") in VERDICTS:
            out.setdefault(run_id, {})[str(data.get("by") or "")] = str(data["verdict"])
    return out


async def feedback_of(session: Any, run_ids: Iterable[str], principal: Any) -> dict[str, dict[str, Any]]:
    """每次对账的反馈：{对账运行: {mine: 看的这个人最后一次给的（没给过为 None）, useful: 几个人, unclear: 几个人}}。"""
    ids = list(dict.fromkeys(run_ids))
    latest = await _latest(session, ids)
    me = _who(principal)
    out: dict[str, dict[str, Any]] = {}
    for run_id in ids:
        votes = latest.get(run_id, {})
        out[run_id] = {"mine": votes.get(me), "useful": sum(v == "useful" for v in votes.values()),
                       "unclear": sum(v == "unclear" for v in votes.values())}
    return out


async def counts(session: Any, run_ids: Collection[str]) -> dict[str, Any]:
    """统计 E2 的反馈（ledger/stats._e2）：这些对账运行上，说明有用、看不懂各几个（每人只算最后一次），给过反馈的对账几次，
    有用的比率（分母为 0 时为 None）。"""
    latest = await _latest(session, run_ids)
    useful = sum(v == "useful" for votes in latest.values() for v in votes.values())
    unclear = sum(v == "unclear" for votes in latest.values() for v in votes.values())
    total = useful + unclear
    return {"available": True, "useful": useful, "unclear": unclear, "runs": sum(1 for v in latest.values() if v),
            "useful_rate": round(useful / total, 4) if total else None}


__all__ = ["BAD_VERDICT", "NOT_FOUND", "NOT_SEALED", "REFUSED", "VERDICTS", "FeedbackRefused", "counts", "feedback_of",
           "give"]
