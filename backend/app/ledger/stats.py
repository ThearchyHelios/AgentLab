"""口径账本自身给出的统计 E1–E4（docs/design/next-round/02-口径账本.md 做法 10，总方案 X2：只有这一份实现）。

全部从比对工件（kind=caliber_match）、运行、查询快照和审计现算，不另存：另建统计表或埋点，数据和账本就成了两份。评测页
「口径命中与重复度」直接调 compute_stats（同一个函数、同一套口径），评测细案的 eval/signals.py 撤销。

范围：since / until 按运行的发起时间；origins 缺省是比对的那几种探索运行（ledger/match.EXPLORE_ORIGINS）；只算「计入统计」的
运行（engine/origin.counted_runs：评测、对账、召回这些系统派生的不算）。run_ids 给了就只算这几次（评测传它自己挑的运行，
这时不再按来源和计入统计筛——调用方点名的就是要算的）。

- E1 等价命中：比对过的查询里 proven / composed / related / none / skipped 各几条（skipped 单独列，不混进 none），有候选口径
  的查询里各自的比率；证明等价的绑定是在哪一级配上的；没证明的原因（不可比、参数绑不上、配上了但不等价）；不等价的差异类型
  分布（time / filter / aggregate / join / other / parse，每份口径的差异每类只计一次）；逐个数的标签各几个、整题等价几次。
- E2 对账：对账运行（origin=reconcile）发起、完成、失败各几次；相关口径的差异说明里兜底说明的占比。「说明有用 / 看不懂」的
  反馈要等对账面板（第 3 波）记成对账运行上的封存后事件，这之前照实写没有数据。
- E3 重复度：问数据运行里执行过的查询按（数据源、normalize_sql 文本）分组，出现在 ≥ REPEAT_RUNS 次运行、来自 ≥ REPEAT_CONVERSATIONS
  段对话的算重复的形状，给出这样的形状几个、有重复形状的运行占几成；问题聚类（app/chat/questions：全站唯一的聚类，只取问题
  原文、不含 history、排除系统派生的运行）里 ≥ REPEAT_RUNS 个问题的类占几成。认证队列的转化（固化、发布、驳回）在第 3 波。
- E4 召回：每个口径版本被用了多少次（比对里等价已证明的运行数、这一版的正式运行数）。每次发布召回了几组、其中数变了的比例要等
  召回（第 4 波），这之前照实写没有数据，只给召回运行的次数。
"""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.ledger import labels as ledger_labels
from app.ledger import store

#: 重复的门槛：同一个形状出现在几次运行里、来自几段对话（同认证队列的进队门槛）
REPEAT_RUNS = 3
REPEAT_CONVERSATIONS = 2
#: 差异类型怎么归到 E1 的几类
ASPECT_GROUP = {"time": "time", "time_range": "time", "filter": "filter", "aggregate": "aggregate",
                "group_by": "aggregate", "join": "join", "tables": "join", "parse": "parse"}
DIFF_GROUPS = ("time", "filter", "aggregate", "join", "other", "parse")
STATUSES = ("proven", "composed", "related", "none", "skipped")
#: 没有数据时的说法（这一块的功能还没做）
NOT_YET_RECONCILE = "对账面板的反馈在第 3 波接上，目前没有数据"
NOT_YET_CERTIFY = "认证队列在第 3 波接上，目前没有数据"
NOT_YET_RECALL = "召回在第 4 波接上，目前没有数据"
#: 聚类最多取几个问题（同治理接口的上限）
QUESTION_LIMIT = 500


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _scope(stmt: Any, *, since: datetime | None, until: datetime | None, origins: Sequence[str] | None,
           run_ids: Collection[str] | None) -> Any:
    from app.db.models import Run
    from app.engine.origin import counted_runs
    from app.ledger.match import EXPLORE_ORIGINS

    if run_ids is not None:
        return stmt.where(Run.id.in_(list(run_ids)))
    stmt = stmt.where(counted_runs(), Run.origin_kind.in_(list(origins or EXPLORE_ORIGINS)))
    if since is not None:
        stmt = stmt.where(Run.created_at >= since)
    if until is not None:
        stmt = stmt.where(Run.created_at < until)
    return stmt


async def _matches(session: Any, **scope: Any) -> list[dict[str, Any]]:
    """范围里每次运行最新的一份比对工件的内容（按工件引用的时间取最新；读不出的跳过）。"""
    from app.core.artifact_store import load
    from app.db.models import Artifact, Run

    stmt = select(Artifact.id, Artifact.run_id, Artifact.created_at).join(Run, Run.id == Artifact.run_id) \
        .where(Artifact.kind == store.KIND)
    latest: dict[str, tuple[Any, str]] = {}
    for artifact, run_id, at in (await session.execute(_scope(stmt, **scope))).all():
        if run_id not in latest or (at and latest[run_id][0] and at > latest[run_id][0]):
            latest[run_id] = (at, artifact)

    def read() -> list[dict[str, Any]]:
        out = []
        for _, artifact in latest.values():
            try:
                content = load(artifact)
            except ValueError:
                continue
            if isinstance(content, dict) and content.get("format") == store.FORMAT:
                out.append(content)
        return out

    return await asyncio.to_thread(read)


def e1_of(contents: list[dict[str, Any]]) -> dict[str, Any]:
    """E1 等价命中（见模块说明）。纯函数：评测拿自己的比对结果也能算。"""
    status = Counter({s: 0 for s in STATUSES})
    with_candidates = Counter({s: 0 for s in STATUSES})
    via: Counter[str] = Counter()
    unproven: Counter[str] = Counter()
    diffs = Counter({g: 0 for g in DIFF_GROUPS})
    labels = Counter({k: 0 for k in ledger_labels.KINDS})
    whole = 0
    queries = 0
    for content in contents:
        for query in content.get("queries") or []:
            queries += 1
            s = str(query.get("status") or "")
            status[s] += 1
            if query.get("candidates"):
                with_candidates[s] += 1
            for cand in query.get("candidates") or []:
                if cand.get("status") == "proven":
                    via[str(cand.get("via") or "parsed")] += 1
                    continue
                if cand.get("status") == "skipped":
                    continue
                if not cand.get("comparable", True):
                    unproven["不可比：" + str(cand.get("reason") or "")] += 1
                elif cand.get("reason"):
                    unproven[str(cand["reason"])] += 1
                else:
                    unproven["参数配上了，但和口径不等价"] += 1
                seen = {ASPECT_GROUP.get(str(item.get("aspect")), "other")
                        for item in (cand.get("diff") or {}).get("items") or []}
                for group in seen:
                    diffs[group] += 1
        for report in (content.get("reports") or {}).values():
            for label in (report.get("labels") or {}).values():
                labels[str(label.get("kind"))] += 1
        if (content.get("whole") or {}).get("equivalent"):
            whole += 1
    candidates = sum(with_candidates.values())
    return {
        "runs": len(contents), "queries": queries, "status": dict(status),
        "with_candidates": candidates,
        "rates": {s: _ratio(with_candidates[s], candidates) for s in STATUSES if s != "none"},
        "proven_via": dict(via), "unproven_reasons": dict(unproven.most_common(20)), "diff_aspects": dict(diffs),
        "labels": dict(labels), "whole_equivalent": whole,
    }


async def _e2(session: Any, contents: list[dict[str, Any]], **scope: Any) -> dict[str, Any]:
    from app.db.models import Run

    stmt = select(Run.status, func.count()).where(Run.origin_kind == "reconcile")
    if scope.get("run_ids") is not None:
        stmt = stmt.where(Run.id.in_(list(scope["run_ids"])))
    else:
        # 对账运行是系统派生的（不计入使用统计），这里专门数的就是它：只按时间筛
        if scope.get("since") is not None:
            stmt = stmt.where(Run.created_at >= scope["since"])
        if scope.get("until") is not None:
            stmt = stmt.where(Run.created_at < scope["until"])
    by_status = {s: int(n) for s, n in (await session.execute(stmt.group_by(Run.status))).all()}
    explained = fallback = 0
    for content in contents:
        for query in content.get("queries") or []:
            for cand in query.get("candidates") or []:
                if cand.get("status") == "related" and isinstance(cand.get("diff"), dict):
                    explained += 1
                    fallback += bool(cand["diff"].get("fallback"))
    return {"launched": sum(by_status.values()), "succeeded": by_status.get("succeeded", 0),
            "failed": by_status.get("failed", 0), "explained": explained, "fallback": fallback,
            "fallback_rate": _ratio(fallback, explained),
            "feedback": {"available": False, "note": NOT_YET_RECONCILE}}


#: 查询快照 → (数据源, 形状)。工件按内容寻址、不会变，记下来不会过期
_SHAPES: dict[str, tuple[str, str] | None] = {}
_SHAPES_MAX = 20000


def _shape_of(artifact: str, kinds: dict[str, str | None]) -> tuple[str, str] | None:
    from app.core.artifact_store import load
    from app.data.sql_semantics import normalize_sql

    if artifact in _SHAPES:
        return _SHAPES[artifact]
    try:
        snap = load(artifact)
    except ValueError:
        snap = None
    found: tuple[str, str] | None = None
    if isinstance(snap, dict) and isinstance(snap.get("sql"), str) and snap["sql"].strip():
        source = str(snap.get("source") or "")
        found = (source, normalize_sql(snap["sql"], kinds.get(source)).text)
    if len(_SHAPES) < _SHAPES_MAX:
        _SHAPES[artifact] = found
    return found


async def _e3(session: Any, *, principal: Any, **scope: Any) -> dict[str, Any]:
    from app.chat.questions import cluster_questions, embedder, recent_questions
    from app.data.catalog import QUERY_SNAPSHOT_KIND
    from app.db.models import Artifact, ConversationTurn, DataSource, Run

    chat_scope = {**scope, "origins": ["chat"]}
    runs = (await session.execute(_scope(select(Run.id, Run.origin), **chat_scope))).all()
    run_ids = [rid for rid, _ in runs]
    conversation: dict[str, str] = {rid: str((origin or {}).get("conversation_id")) for rid, origin in runs
                                    if isinstance(origin, dict) and origin.get("conversation_id")}
    if run_ids:
        for rid, cid in (await session.execute(select(ConversationTurn.run_id, ConversationTurn.conversation_id)
                                               .where(ConversationTurn.run_id.in_(run_ids)))).all():
            conversation.setdefault(rid, cid)
    snaps = (await session.execute(select(Artifact.id, Artifact.run_id).where(
        Artifact.kind == QUERY_SNAPSHOT_KIND, Artifact.run_id.in_(run_ids)))).all() if run_ids else []
    kinds = {name: kind for name, kind in (await session.execute(select(DataSource.name, DataSource.kind))).all()}
    shaped = await asyncio.to_thread(lambda: [(rid, _shape_of(aid, kinds)) for aid, rid in snaps])
    groups: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for rid, shape in shaped:
        if shape is None:
            continue
        found_runs, convs = groups.setdefault(shape, (set(), set()))
        found_runs.add(rid)
        convs.add(conversation.get(rid) or f"run:{rid}")
    repeated = [shape for shape, (rs, cs) in groups.items() if len(rs) >= REPEAT_RUNS and len(cs) >= REPEAT_CONVERSATIONS]
    with_queries = {rid for rid, shape in shaped if shape is not None}
    in_repeated = {rid for shape in repeated for rid in groups[shape][0]}
    questions = await recent_questions(session, principal, limit=QUESTION_LIMIT)
    if scope.get("since") is not None:
        questions = [q for q in questions if q.at is None or q.at >= scope["since"]]
    if scope.get("until") is not None:
        questions = [q for q in questions if q.at is None or q.at < scope["until"]]
    clusters = await cluster_questions(questions) if questions else []
    clustered = sum(c.count for c in clusters if c.count >= REPEAT_RUNS)
    return {
        "chat_runs": len(run_ids), "runs_with_queries": len(with_queries), "shapes": len(groups),
        "repeated_shapes": len(repeated), "runs_in_repeated_shapes": len(in_repeated),
        "repeated_share": _ratio(len(in_repeated), len(with_queries)),
        "threshold": {"runs": REPEAT_RUNS, "conversations": REPEAT_CONVERSATIONS},
        "questions": len(questions), "clusters": len(clusters), "questions_in_repeated_clusters": clustered,
        "cluster_share": _ratio(clustered, len(questions)), "embedder": embedder(),
        "certify": {"available": False, "note": NOT_YET_CERTIFY},
    }


async def _e4(session: Any, contents: list[dict[str, Any]], **scope: Any) -> dict[str, Any]:
    from app.db.models import Run
    from app.engine.origin import counted_runs

    proven: Counter[str] = Counter()
    for content in contents:
        keys = {c.get("caliber") for q in content.get("queries") or [] for c in q.get("candidates") or []
                if c.get("status") == "proven" and c.get("caliber")}
        for key in keys:
            proven[str(key)] += 1
    stmt = select(Run.workflow_id, Run.version, func.count()).where(
        Run.run_class == "formal", Run.workflow_id.is_not(None), Run.version.is_not(None))
    stmt = stmt.where(Run.id.in_(list(scope["run_ids"]))) if scope.get("run_ids") is not None \
        else stmt.where(counted_runs())
    if scope.get("since") is not None:
        stmt = stmt.where(Run.created_at >= scope["since"])
    if scope.get("until") is not None:
        stmt = stmt.where(Run.created_at < scope["until"])
    formal = {ledger_labels.caliber_key(w, v): int(n)
              for w, v, n in (await session.execute(stmt.group_by(Run.workflow_id, Run.version))).all()}
    recalls = int(await session.scalar(select(func.count()).select_from(Run).where(Run.origin_kind == "recall")) or 0)
    usage = [{"caliber": key, "workflow_id": key.rsplit("@", 1)[0], "version": int(key.rsplit("@", 1)[1]),
              "proven_runs": proven.get(key, 0), "formal_runs": formal.get(key, 0)}
             for key in sorted(set(proven) | set(formal))]
    return {"usage": usage, "recall_runs": recalls, "per_publish": {"available": False, "note": NOT_YET_RECALL}}


async def compute_stats(session: Any, *, since: datetime | None = None, until: datetime | None = None,
                        origins: Sequence[str] | None = None, run_ids: Collection[str] | None = None,
                        principal: Any = None) -> dict[str, Any]:
    """账本统计 E1–E4（见模块说明）。session 只读；principal 是看统计的人（问题聚类去掉他读不了的运行），不给按当前请求的身份。

    返回 {window, algo, e1, e2, e3, e4}；各块的键见 e1_of、_e2、_e3、_e4。比率在分母为 0 时为 None（不写成 0）。"""
    from app.api.deps import current_principal

    scope = {"since": since, "until": until, "origins": list(origins) if origins else None,
             "run_ids": list(run_ids) if run_ids is not None else None}
    contents = await _matches(session, **scope)
    return {
        "window": {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
                   "origins": scope["origins"], "runs": scope["run_ids"]},
        "algo": store.ALGO,
        "e1": e1_of(contents),
        "e2": await _e2(session, contents, **scope),
        "e3": await _e3(session, principal=principal or current_principal(), **scope),
        "e4": await _e4(session, contents, **scope),
    }


__all__ = ["ASPECT_GROUP", "DIFF_GROUPS", "REPEAT_CONVERSATIONS", "REPEAT_RUNS", "STATUSES", "compute_stats", "e1_of"]
