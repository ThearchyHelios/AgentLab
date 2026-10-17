"""口径账本交给界面的样子（docs/design/next-round/02-口径账本.md 做法 3）：GET /api/runs/{run_id}/ledger、证据片段接口的
ledger 键、报告导出的片段面板，三处读同一份比对工件、按同一套遮罩给。

遮罩按读的那一刻重滤（同证据面板，api/evidence._Masks）：绑定的值是这次运行查到过的遮罩值时不给值（也就不能拿它代填出具），
差异条目里的 SQL 片段走 _Shown 同一套换法。工作流名按现在的取（工件里不存名字）。

界面层不放逻辑：标签、整题判断、能做的动作都由这里给。actions（第 3 波）：整题等价时按那份口径正式出具（能不能、能不能
自动、不能的原因，ledger/issue.plan_issue，按看的这个人的权限判）；有相关口径的查询看差多少（口径对账，ledger/reconcile）。
issued 是这次运行按口径出具过的正式运行，reconciled 是对过的账（各自的运行、状态，第 4 波起带上结果上的「说明有用 /
看不懂」：看的这个人给过的、各几个，ledger/feedback）。

recall（第 4 波，ledger/recall.answer_marks）：这个答案身上的召回标记——「已按 vN 核对，不影响」（checked）、依据有变
（basis）、看的这个人手上说的是它的 recall 待办（tasks）。几种状态都带：没比对过、不比对的答案照样可能被标依据有变。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, Workflow
from app.ledger import labels as ledger_labels
from app.ledger import match as ledger_match
from app.ledger import store

#: state：matched 比对过；pending 已排队按需比对；ineligible 这次运行不比对（reason 写为什么）；unreadable 比对记录读不出
STATES = ("matched", "pending", "ineligible", "unreadable")

#: 成功结束、还没封存的运行，结束后这么多秒内答 pending（runner 落库之后马上封存，正常是几秒）。过了还没封存的是封存出了错、
#: 或者是封存之前的老数据：再答 pending，页面就一直转圈、等满两分钟（store/ledger.ts 的等待上限）再取还是 pending——照实说没封存
SEAL_GRACE_S = 120


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
        if cand.get("how"):
            item["how"] = cand["how"]
        if cand.get("columns") is not None:
            item["columns"] = cand["columns"]
        if isinstance(cand.get("projection"), dict):
            # 按引用的列投影之后比的（ledger/match 的 _projected）：两边各留下了哪几列、去掉了几列（列名，不带值）
            item["projection"] = cand["projection"]
        cands.append(item)
    out["candidates"] = cands
    return out


def _labels_view(content: dict[str, Any], hider: _Hide, names: dict[str, str]) -> dict[str, Any]:
    return {node: {"labels": {sid: ledger_labels.view_label(label, hide=hider.hide, shown=hider.shown, names=names)
                              for sid, label in (report.get("labels") or {}).items()}}
            for node, report in (content.get("reports") or {}).items()}


def _stamp(recorded: store.Recorded) -> dict[str, Any]:
    """比对的来历：哪份工件、哪一版算法、谁触发的、什么时候追加的；事后补比的写明按现在的口径回看。"""
    at = datetime.fromtimestamp(recorded.ts, timezone.utc).isoformat(timespec="seconds") \
        if isinstance(recorded.ts, (int, float)) else None
    return {"artifact": recorded.artifact, "algo": recorded.algo, "trigger": recorded.trigger, "retro": recorded.retro,
            "matched_at": at, "post_seal": True, "note": ledger_labels.POST_SEAL_NOTE,
            **({"retro_note": ledger_labels.CERTIFIED_NOTE if recorded.trigger == "certified"
                else ledger_labels.RETRO_NOTE} if recorded.retro else {})}


def _just_finished(finished_at: datetime | None) -> bool:
    """运行是不是刚结束（SEAL_GRACE_S 以内）。没记结束时间的不算；库里读回 naive 的按 UTC 算（同 db/base.UTCDateTime）。"""
    if finished_at is None:
        return False
    at = finished_at if finished_at.tzinfo is not None else finished_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() <= SEAL_GRACE_S


async def run_view(run_id: str) -> dict[str, Any]:
    """GET /api/runs/{run_id}/ledger 的答复：标签、整题判断、每条查询的比对、能做的动作。没比对过、能比的运行这时排上按需比对
    （trigger=on_demand），答复 state=pending；不比的照实说为什么。"""
    from app.api.coded import CodedHTTPException
    from app.api.evidence import RUN_NOT_FOUND, _sealed

    from app.api.deps import current_principal
    from app.ledger.recall import answer_marks

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
        principal = current_principal()
        marks = await answer_marks(session, run_id, principal)
    base: dict[str, Any] = {"run_id": run_id, "note": ledger_labels.POST_SEAL_NOTE, "recall": marks}
    recorded = await store.latest(run_id)
    if recorded is None:
        why = ledger_match.ineligible(run)
        if why == ledger_match.NOT_SEALED and run.status == "succeeded" \
                and ledger_match.wanted(run.run_class, run.origin_kind) and _just_finished(run.finished_at):
            # runner 先发 run.finished、落库，再封存、再排上比对（engine/runner._finalize）：页面收到 run.finished 立刻来取，
            # 这一下还没封存。这不是「不比对」，是封存完就比——照实答 pending，页面按退避接着取；这里不另排按需比对。
            # 只限刚结束的那一会儿（SEAL_GRACE_S），早就结束、一直没封存的照实答 ineligible
            return {**base, "state": "pending"}
        sealed = await _sealed(run_id) if why is None else None
        if sealed is not None and not sealed.queries:
            why = ledger_match.NO_QUERIES
        if why is not None:
            return {**base, "state": "ineligible", "reason": why}
        ledger_match.schedule(run_id, trigger="on_demand")
        return {**base, "state": "pending"}
    if recorded.content is None:
        return {**base, "state": "unreadable", "reason": recorded.problem, **_stamp(recorded)}
    from app.ledger import feedback as ledger_feedback
    from app.ledger import issue as ledger_issue
    from app.ledger import reconcile as ledger_reconcile

    content = recorded.content
    sealed = await _sealed(run_id)
    hider = await _hider(sealed)
    names = await _names(_workflow_ids(content))
    plans = await ledger_issue.plans_for(run_id, content, principal=principal, hide=hider.hide,
                                         artifact=recorded.artifact, retro=recorded.retro)
    async with SessionLocal() as session:
        issued = [ledger_issue.issued_view(r) for r in await ledger_issue.issued_runs(session, run_id)]
        reconciled = [ledger_reconcile.reconciled_view(r) for r in await ledger_reconcile.reconciled_runs(session, run_id)]
        # 对账结果上的「说明有用 / 看不懂」（ledger/feedback）：看的这个人给过哪一个、各几个
        votes = await ledger_feedback.feedback_of(session, [r["run_id"] for r in reconciled], principal)
        reconciled = [{**r, "feedback": votes[r["run_id"]]} for r in reconciled]
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
        "actions": [*(p.action() for p in plans), *ledger_reconcile.actions_of(content, names, hider.hide)],
        "issued": issued,
        "reconciled": reconciled,
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
