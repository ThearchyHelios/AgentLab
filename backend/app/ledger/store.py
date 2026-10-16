"""口径账本的记法（docs/design/next-round/02-口径账本.md 做法 2，拍板 L8）：比对结果存成内容寻址工件，不建新表。

一次比对 = 一份工件 kind=caliber_match（格式 agentlab.caliber_match/1）+ 一条封存之后追加的事件 ledger.matched
{artifact, manifest_hash, algo, counts, trigger}（engine/run_append 的 sealed 方式，同按需裁判的 evidence.judged）：

- 工件内容只放比对本身：运行 id、对着哪一次封存（manifest_hash / manifest_seq）、算法版本、考虑过的口径版本（工作流、版本、
  级别、graph_hash）、每条查询的状态和绑定、差异条目、列映射、每个数的标签、整题判断。不放时间、耗时、工作流名（名字会改，
  界面读的时候再取）：同一份封存、同一个算法版本、同一批口径，再比一次得到的工件一字不差、哈希一样。
- 工件引用的 meta 记 {calibers: [等价已证明的「工作流@版本」], related: [相关的], counts}：召回按旧版本反查、统计按种类数，
  都只扫 artifacts（kind 有索引），不逐份读内容。
- 事件说的是「这一次封存的这份比对」：读的时候只认 manifest_hash 等于运行现在封存的那几条，取最后一条（之后口径有变、补比
  过的，以最新的为准）。工件读不出、被改过、不是这次运行这次封存的，照实说读不出，不拿它标数。

trigger 是谁触发的：sealed（运行封存后马上比的）、backfill（启动时补比）、on_demand（打开账本时按需比的）。后两种是事后按
那时的口径回看的，界面据此写「按现在的口径回看」。它只在事件里，不进工件内容（不然同一份比对因为触发方式不同哈希就不同）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent

#: 工件的种类和内容格式
KIND = "caliber_match"
FORMAT = "agentlab.caliber_match/1"
#: 比对算法的版本：绑定、证明、标签的规则改了就加一，界面和统计据此分得清是哪一版算法比的
ALGO = "1"
#: 触发方式（见模块说明）
TRIGGERS = ("sealed", "backfill", "on_demand")


@dataclass(frozen=True)
class Recorded:
    """一次运行现在这次封存的最新一份比对。content 读不出时为 None，problem 写为什么。"""

    artifact: str
    seq: int
    ts: Any
    trigger: str
    algo: str | None
    content: dict[str, Any] | None
    problem: str | None = None

    @property
    def retro(self) -> bool:
        """事后按那时的口径回看的（启动时补比、打开时按需比的），不是运行一封存就比的。"""
        return self.trigger != "sealed"


#: 读不出比对工件时的说法
UNREADABLE = "比对记录读不出来（文件缺失）"
TAMPERED = "比对记录与哈希不一致，疑似被修改，不能用来标口径"
FOREIGN = "比对记录不属于这次运行的这次封存，不能用来标口径"


def counts_of(queries: list[dict[str, Any]]) -> dict[str, int]:
    """每种查询状态各几条（proven / composed / related / none / skipped），0 也写。"""
    out = {status: 0 for status in ("proven", "composed", "related", "none", "skipped")}
    for query in queries:
        status = str(query.get("status") or "")
        if status in out:
            out[status] += 1
    return out


def meta_of(content: dict[str, Any]) -> dict[str, Any]:
    """工件引用的 meta：{calibers: [等价已证明的「工作流@版本」], related: [相关的], counts}。"""
    proven: list[str] = []
    related: list[str] = []
    for query in content.get("queries") or []:
        for cand in query.get("candidates") or []:
            key = str(cand.get("caliber") or "")
            if not key:
                continue
            bucket = proven if cand.get("status") == "proven" else related
            if key not in bucket:
                bucket.append(key)
    related = [key for key in related if key not in proven]
    return {"calibers": sorted(proven), "related": sorted(related), "counts": counts_of(content.get("queries") or [])}


async def save(run_id: str, content: dict[str, Any], *, seal: tuple[int | None, str | None],
               trigger: str) -> str | None:
    """存比对工件、追加 ledger.matched。返回工件 id；运行在比对期间换了封存（没抢到）时只存了工件、没追加事件，返回 None
    （不算这次运行的比对：读的时候只认事件）。"""
    from app.core.artifact_store import put_json
    from app.engine.run_append import append_events

    if trigger not in TRIGGERS:
        raise ValueError(f"不认识的比对触发方式：{trigger}")
    meta = meta_of(content)
    artifact = await put_json(content, kind=KIND, run_id=run_id, meta=meta)
    written = await append_events(run_id, [(EventType.LEDGER_MATCHED, None, {
        "artifact": artifact, "manifest_hash": content.get("manifest_hash"), "algo": content.get("algo"),
        "counts": meta["counts"], "trigger": trigger,
    })], require="sealed", seal=seal)
    return artifact if written else None


async def latest(run_id: str) -> Recorded | None:
    """这次运行现在这次封存的最新一份比对；没比对过（或者比对的是以前的封存）为 None。"""
    from app.core.artifact_store import load

    async with SessionLocal() as session:
        manifest = await session.scalar(select(Run.manifest_hash).where(Run.id == run_id))
        if not manifest:
            return None
        rows = (await session.execute(
            select(RunEvent.seq, RunEvent.ts, RunEvent.data).where(
                RunEvent.run_id == run_id, RunEvent.type == EventType.LEDGER_MATCHED)
            .order_by(RunEvent.seq.desc()))).all()
    for seq, ts, data in rows:
        if not isinstance(data, dict) or data.get("manifest_hash") != manifest or not data.get("artifact"):
            continue
        artifact = str(data["artifact"])
        trigger = str(data.get("trigger") or "sealed")
        try:
            content = load(artifact)
        except ValueError:
            return Recorded(artifact, seq, ts, trigger, data.get("algo"), None, TAMPERED)
        if not isinstance(content, dict):
            return Recorded(artifact, seq, ts, trigger, data.get("algo"), None, UNREADABLE)
        if content.get("format") != FORMAT or content.get("run_id") != run_id \
                or content.get("manifest_hash") != manifest:
            return Recorded(artifact, seq, ts, trigger, data.get("algo"), None, FOREIGN)
        return Recorded(artifact, seq, ts, trigger, data.get("algo"), content)
    return None


async def matched_run_ids(session: Any, run_ids: list[str]) -> set[str]:
    """这几次运行里有过比对事件的（不管是哪一次封存的）：启动补比据此跳过比过的。"""
    if not run_ids:
        return set()
    rows = await session.execute(select(RunEvent.run_id).where(
        RunEvent.run_id.in_(run_ids), RunEvent.type == EventType.LEDGER_MATCHED).distinct())
    return {r for (r,) in rows}


__all__ = ["ALGO", "FORMAT", "KIND", "TRIGGERS", "Recorded", "counts_of", "latest", "matched_run_ids", "meta_of", "save"]
