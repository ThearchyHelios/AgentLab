"""口径账本交给界面的样子（docs/design/next-round/02-口径账本.md 做法 3）：GET /api/runs/{run_id}/ledger、证据片段接口的
ledger 键、报告导出的片段面板，三处读同一份比对工件、按同一套遮罩给。

遮罩按读的那一刻重滤（同证据面板，api/evidence._Masks）：绑定的值是这次运行查到过的遮罩值时不给值（也就不能拿它代填出具），
差异条目里的 SQL 片段走 _Shown 同一套换法。工作流名按现在的取（工件里不存名字）。

界面层不放逻辑：标签、整题判断、能做的动作（这一波还没有出具和对账，actions 是空的）都由这里给。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, Workflow
from app.ledger import labels as ledger_labels
from app.ledger import match as ledger_match
from app.ledger import store

#: state：matched 比对过；pending 已排队按需比对；ineligible 这次运行不比对（reason 写为什么）；unreadable 比对记录读不出
STATES = ("matched", "pending", "ineligible", "unreadable")


class _Hide:
    """按这次运行查到过的遮罩值遮：hide(值) 这个值给不给，shown(文字) 文字里的遮罩值换成「已遮罩」。"""

    def __init__(self, secrets: Any) -> None:
        from app.api.evidence import _Shown

        self._secrets = secrets
        self._shown = _Shown(secrets)

    def hide(self, value: Any) -> bool:
        if self._secrets is None or value is None or isinstance(value, bool):
            return False
        return bool(self._secrets.hit(value) or self._secrets.weak_hit(str(value)))

    def shown(self, value: Any) -> Any:
        return self._shown.text(value)


async def _hider(sealed: Any) -> _Hide:
    from app.api.evidence import _Masks

    return _Hide(await _Masks(sealed).run_secrets())


async def _names(ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    async with SessionLocal() as session:
        rows = await session.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(sorted(ids))))
    return {wid: name for wid, name in rows.all()}


def _workflow_ids(content: dict[str, Any]) -> set[str]:
    return {str(c.get("workflow_id")) for c in content.get("calibers") or [] if c.get("workflow_id")}


def _whole_view(whole: dict[str, Any], hider: _Hide, names: dict[str, str]) -> dict[str, Any]:
    out = {k: v for k, v in whole.items() if k != "binding"}
    if whole.get("equivalent"):
        out["binding"] = ledger_labels.binding_view(whole.get("binding"), hider.hide)
        out["workflow_name"] = names.get(str(whole.get("workflow_id") or ""))
        out["level_text"] = ledger_labels.LEVEL_TEXT.get(str(whole.get("level") or ""), "")
    return out


def _query_view(query: dict[str, Any], hider: _Hide, names: dict[str, str]) -> dict[str, Any]:
    out = {k: query[k] for k in ("artifact", "node_id", "source", "tables", "status", "reason", "note") if k in query}
    cands = []
    for cand in query.get("candidates") or []:
        item = ledger_labels.related_view(cand, hide=hider.hide, shown=hider.shown, names=names)
        item["status"] = cand.get("status")
        if cand.get("columns") is not None:
            item["columns"] = cand["columns"]
        cands.append(item)
    out["candidates"] = cands
    return out


def _labels_view(content: dict[str, Any], hider: _Hide, names: dict[str, str]) -> dict[str, Any]:
    return {node: {"labels": {sid: ledger_labels.view_label(label, hide=hider.hide, shown=hider.shown, names=names)
                              for sid, label in (report.get("labels") or {}).items()}}
            for node, report in (content.get("reports") or {}).items()}


def _stamp(recorded: store.Recorded) -> dict[str, Any]:
    """比对的来历：哪份工件、哪一版算法、谁触发的、什么时候追加的；事后补比的写明按现在的口径回看。"""
    from datetime import datetime, timezone

    at = datetime.fromtimestamp(recorded.ts, timezone.utc).isoformat(timespec="seconds") \
        if isinstance(recorded.ts, (int, float)) else None
    return {"artifact": recorded.artifact, "algo": recorded.algo, "trigger": recorded.trigger, "retro": recorded.retro,
            "matched_at": at, "post_seal": True, "note": ledger_labels.POST_SEAL_NOTE,
            **({"retro_note": ledger_labels.RETRO_NOTE} if recorded.retro else {})}


async def run_view(run_id: str) -> dict[str, Any]:
    """GET /api/runs/{run_id}/ledger 的答复：标签、整题判断、每条查询的比对、能做的动作。没比对过、能比的运行这时排上按需比对
    （trigger=on_demand），答复 state=pending；不比的照实说为什么。"""
    from app.api.coded import CodedHTTPException
    from app.api.evidence import RUN_NOT_FOUND, _sealed

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    if run is None:
        raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
    base: dict[str, Any] = {"run_id": run_id, "note": ledger_labels.POST_SEAL_NOTE}
    recorded = await store.latest(run_id)
    if recorded is None:
        why = ledger_match.ineligible(run)
        sealed = await _sealed(run_id) if why is None else None
        if sealed is not None and not sealed.queries:
            why = ledger_match.NO_QUERIES
        if why is not None:
            return {**base, "state": "ineligible", "reason": why}
        ledger_match.schedule(run_id, trigger="on_demand")
        return {**base, "state": "pending"}
    if recorded.content is None:
        return {**base, "state": "unreadable", "reason": recorded.problem, **_stamp(recorded)}
    content = recorded.content
    sealed = await _sealed(run_id)
    hider = await _hider(sealed)
    names = await _names(_workflow_ids(content))
    return {
        **base, "state": "matched", **_stamp(recorded),
        "calibers": [{**{k: c.get(k) for k in ("caliber", "workflow_id", "version", "level", "graph_hash")},
                      "workflow_name": names.get(str(c.get("workflow_id") or "")),
                      "level_text": ledger_labels.LEVEL_TEXT.get(str(c.get("level") or ""), "")}
                     for c in content.get("calibers") or []],
        "queries": [_query_view(q, hider, names) for q in content.get("queries") or []],
        "reports": _labels_view(content, hider, names),
        "primary": content.get("primary"),
        "whole": _whole_view(content.get("whole") or {}, hider, names),
        # 能做的动作（按口径正式出具、对账）在第 3 波；这一波照实给空的
        "actions": [],
    }


async def report_labels(sealed: Any, report_node: str) -> dict[str, Any] | None:
    """一份报告里每个数的标签（遮过的），给证据片段接口和报告导出：{labels: {片段 id: 标签}, 来历}。没比对过、比对记录读不出、
    这份报告没有标签的为 None（片段接口就不加 ledger 键，老运行的返回一字不差）。"""
    recorded = await store.latest(sealed.run_id)
    if recorded is None or recorded.content is None:
        return None
    report = (recorded.content.get("reports") or {}).get(report_node)
    if not isinstance(report, dict):
        return None
    hider = await _hider(sealed)
    names = await _names(_workflow_ids(recorded.content))
    return {"labels": {sid: ledger_labels.view_label(label, hide=hider.hide, shown=hider.shown, names=names)
                       for sid, label in (report.get("labels") or {}).items()}, **_stamp(recorded)}


async def segment_label(sealed: Any, report_node: str, segment_id: str) -> dict[str, Any] | None:
    """证据片段接口的 ledger 键：{label: 这个片段的标签, 比对的来历（artifact 是比对工件）}；这个片段没有标签（不是数、没比对过）
    为 None。"""
    found = await report_labels(sealed, report_node)
    if found is None or segment_id not in found["labels"]:
        return None
    return {"label": found["labels"][segment_id], **{k: v for k, v in found.items() if k != "labels"}}


__all__ = ["STATES", "report_labels", "run_view", "segment_label"]
