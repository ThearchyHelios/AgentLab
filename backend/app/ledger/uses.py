"""一份口径的旧版本被谁用过（docs/design/next-round/02-口径账本.md 做法 7 一）：口径发布新版本之后，召回要追的那些答案。

**为什么单独一个模块。** 召回（ledger/recall）要按参数去重成组、重算，发布弹窗要预告规模，版本历史要写「这一版被多少问数
回答、导出、召回用过」——三处要的是同一份「用过」的清单。各数各的，预告的规模和实际召回的就对不上（验收要求两者一致）。

反查的对象（工作流 W 的旧版本 V，只看 V < 新版本）：
- **问数回答**（answer）：caliber_match 工件的 meta 里记着 W@V 等价已证明（含助手选用）的探索运行，取这次运行现在这次封存
  的最新一份比对（ledger/store.latest：之后补比过、按新口径不再等价的不算），每条等价的查询一份参数（绑定）；
- **W@V 的正式运行**：定时任务发起的（schedule，每个定时任务最近 PERIODS 期）、应用发起的（app，每个应用最近 PERIODS 次）、
  别处发起的（formal：手动、接口、按口径出具……），参数是那次运行的输入；
- **固定了 W@V 的工作流版本发起的正式运行**（pinned：子工作流或口径卡 caliber_from 固定在 W@V，apps/versions.pinned_upstream
  同一个认法），参数是那次运行的输入里 W 的入口有的字段；
- **固定这一版的应用**（apps/versions.pinning_apps）：应用发起过的正式运行在上面；还没有运行的只列出来，没有可重算的参数；
- **导出记录**：上面这些运行上的 report.exported 审计（第 2 波），导出人也是答案的主人；
- **问数轮次**：运行 origin 里封存的 conversation_id / turn_id（origin_ref，第 2 波），老运行退回 conversation_turns.run_id。

**答案的主人**（owners，召回待办派给谁）：对话的 created_by（没有的退回运行的发起人）、导出人、定时任务的 created_by、应用的
管理者（授权里的 manage，没有就是建应用的人）；别处发起的正式运行是它的发起人。只记 (user|group, 名字)，组由开待办时展开。

只读应用库：不查业务库、不调模型。系统派生的运行（评测、对账、召回，engine/origin.counted_runs）不算「用过」。数量有上限
（MAX_ANSWERS、MAX_FORMAL），按新的在前取，超出的照实算进 truncated。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Artifact, Run, Workflow, WorkflowVersion

#: 用法的种类。answer 问数回答；schedule 定时任务发起的正式运行；app 应用发起的；formal 别处发起的；pinned 固定了这一版的
#: 别的工作流版本发起的
KINDS = ("answer", "schedule", "app", "formal", "pinned")
#: 每个定时任务、每个应用最近几期（细案「定时任务最近 K 期」）
PERIODS = 4
#: 最多看多少次问数回答、多少次正式运行（各按新的在前）
MAX_ANSWERS = 2000
MAX_FORMAL = 500
#: 种类的叫法（版本用量、召回批次、待办里写「来自问数据」……）
KIND_TEXT = {"answer": "问数回答", "schedule": "定时任务", "app": "应用", "formal": "正式运行",
             "pinned": "固定了它的工作流"}
#: 导出审计的动作（api/evidence.py、api/bundle.py 写的）
EXPORTED = "report.exported"


@dataclass
class Use:
    """一次「用过」：哪次运行、旧版本几、参数（原样，没补缺省值）、什么时候、谁是答案的主人。"""

    kind: str
    run_id: str
    version: int
    binding: dict[str, Any]
    at: datetime | None = None
    owners: list[tuple[str, str]] = field(default_factory=list)
    conversation_id: str | None = None
    turn_id: str | None = None
    schedule_id: str | None = None
    app_id: str | None = None
    #: 问数回答：等价的是哪条查询（查询快照）、口径的哪个取数节点
    query: str | None = None
    node_id: str | None = None
    #: 固定了这一版的是哪个工作流的哪一版（pinned）
    via_workflow: str | None = None
    via_version: int | None = None
    exporters: list[str] = field(default_factory=list)

    def add_owner(self, kind: str, name: str | None) -> None:
        if name and (kind, name) not in self.owners:
            self.owners.append((kind, name))

    def describe(self) -> dict[str, Any]:
        """记进召回运行 origin 的那一份（召回结束时据此开待办、在原答案上记账）：只放编号、参数的来处和主人。"""
        out: dict[str, Any] = {"kind": self.kind, "run_id": self.run_id, "version": self.version,
                               "owners": [list(o) for o in self.owners]}
        for key in ("conversation_id", "turn_id", "schedule_id", "app_id", "query", "node_id", "via_workflow",
                    "via_version"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        if self.exporters:
            out["exporters"] = list(self.exporters)
        if self.at is not None:
            out["at"] = self.at.isoformat()
        return out


@dataclass
class Found:
    """反查的结果：用法、固定着旧版本的应用（{版本: [应用]}）、有没有因为上限少看了。"""

    uses: list[Use]
    apps: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    truncated: bool = False

    def versions(self) -> list[int]:
        return sorted({u.version for u in self.uses} | set(self.apps))


# ==========================================================================
# 反查
# ==========================================================================


async def _answers(session: AsyncSession, workflow_id: str, below: int | None, only: int | None
                   ) -> tuple[list[Use], bool]:
    """问数回答：比对工件 meta 里等价已证明的那几份口径里有 W@V 的探索运行（见模块说明）。"""
    from app.engine.origin import counted_runs
    from app.ledger import store
    from app.ledger.labels import caliber_key

    prefix = f"{workflow_id}@"
    rows = (await session.execute(
        select(Artifact.run_id, Artifact.meta, Run.finished_at).join(Run, Run.id == Artifact.run_id)
        .where(Artifact.kind == store.KIND, Run.status == "succeeded", counted_runs())
        .order_by(Run.finished_at.desc()))).all()
    wanted: dict[str, datetime | None] = {}
    for run_id, meta, at in rows:
        keys = [k for k in ((meta or {}).get("calibers") or []) if isinstance(k, str) and k.startswith(prefix)]
        if not keys or run_id in wanted:
            continue
        versions = {int(k.rsplit("@", 1)[1]) for k in keys if k.rsplit("@", 1)[1].isdigit()}
        if any((only is None or v == only) and (below is None or v < below) for v in versions):
            wanted[run_id] = at
    truncated = len(wanted) > MAX_ANSWERS
    out: list[Use] = []
    for run_id, at in list(wanted.items())[:MAX_ANSWERS]:
        recorded = await store.latest(run_id)
        content = recorded.content if recorded is not None else None
        if not isinstance(content, dict):
            continue
        seen: set[tuple[int, str]] = set()
        for query in content.get("queries") or []:
            for cand in query.get("candidates") or []:
                if cand.get("status") != "proven" or cand.get("workflow_id") != workflow_id:
                    continue
                version = cand.get("version")
                if not isinstance(version, int) or (only is not None and version != only) \
                        or (below is not None and version >= below):
                    continue
                if cand.get("caliber") != caliber_key(workflow_id, version):
                    continue
                binding = dict(cand.get("binding") or {})
                key = (version, _key(binding))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Use(kind="answer", run_id=run_id, version=version, binding=binding, at=at,
                               query=query.get("artifact"), node_id=cand.get("node_id")))
    await _chat_owners(session, [u for u in out])
    return out, truncated


def _key(binding: Mapping[str, Any]) -> str:
    from app.apps.resolve import input_key

    return input_key(dict(binding))


async def _chat_owners(session: AsyncSession, found: list[Use]) -> None:
    """问数回答的轮次和主人：运行 origin 里封存的对话、轮次（origin_ref），老运行退回 conversation_turns.run_id；主人是
    对话的 created_by，没有的是运行的发起人。"""
    from app.db.models import Conversation, ConversationTurn

    ids = sorted({u.run_id for u in found})
    if not ids:
        return
    runs = {rid: (origin, started) for rid, origin, started in (await session.execute(
        select(Run.id, Run.origin, Run.started_by).where(Run.id.in_(ids)))).all()}
    turns: dict[str, tuple[str, str]] = {}
    for rid, (origin, _) in runs.items():
        if isinstance(origin, dict) and origin.get("conversation_id") and origin.get("turn_id"):
            turns[rid] = (str(origin["conversation_id"]), str(origin["turn_id"]))
    missing = [rid for rid in ids if rid not in turns]
    if missing:
        for turn_id, conv, rid in (await session.execute(
                select(ConversationTurn.id, ConversationTurn.conversation_id, ConversationTurn.run_id)
                .where(ConversationTurn.run_id.in_(missing)).order_by(ConversationTurn.created_at))).all():
            turns.setdefault(rid, (conv, turn_id))
    convs = sorted({c for c, _ in turns.values()})
    owners = dict((await session.execute(select(Conversation.id, Conversation.created_by)
                                         .where(Conversation.id.in_(convs)))).all()) if convs else {}
    for use in found:
        if use.run_id in turns:
            use.conversation_id, use.turn_id = turns[use.run_id]
        owner = owners.get(use.conversation_id) if use.conversation_id else None
        use.add_owner("user", owner or runs.get(use.run_id, (None, None))[1])


async def _app_managers(session: AsyncSession, app_ids: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """应用的管理者：授权里的 manage（人或组）；没有授权管理者的是建应用的人。"""
    from app.db.models_apps import App, AppGrant

    ids = sorted({a for a in app_ids if a})
    if not ids:
        return {}
    out: dict[str, list[tuple[str, str]]] = {a: [] for a in ids}
    for app_id, kind, name in (await session.execute(
            select(AppGrant.app_id, AppGrant.principal_kind, AppGrant.principal)
            .where(AppGrant.app_id.in_(ids), AppGrant.role == "manage"))).all():
        if kind in ("user", "group") and name and (kind, name) not in out[app_id]:
            out[app_id].append((kind, name))
    for app_id, created_by in (await session.execute(select(App.id, App.created_by).where(App.id.in_(ids)))).all():
        if not out[app_id] and created_by:
            out[app_id].append(("user", created_by))
    return out


async def _formal(session: AsyncSession, clause: Any, *, kind_of: Any) -> tuple[list[Use], bool]:
    """满足 clause 的已封存、成功的正式运行（新的在前，最多 MAX_FORMAL 次）→ 用法。定时任务、应用各只留最近 PERIODS 期。
    kind_of(run) 给出这次运行算哪一种（schedule / app / formal / pinned）和旧版本。"""
    from app.db.models import Schedule
    from app.engine.origin import counted_runs

    rows = list((await session.execute(
        select(Run).where(clause, Run.run_class == "formal", Run.status == "succeeded",
                          Run.manifest_hash.is_not(None), counted_runs())
        .order_by(Run.finished_at.desc()).limit(MAX_FORMAL + 1))).scalars())
    truncated = len(rows) > MAX_FORMAL
    per: dict[tuple[str, str], int] = {}
    out: list[Use] = []
    for run in rows[:MAX_FORMAL]:
        kind, version, via = kind_of(run)
        group = ("schedule", run.schedule_id) if run.schedule_id else ("app", run.app_id) if run.app_id else None
        if group is not None:
            per[group] = per.get(group, 0) + 1
            if per[group] > PERIODS:
                continue
        use = Use(kind=kind, run_id=run.id, version=version, binding=dict(run.input or {}), at=run.finished_at,
                  schedule_id=run.schedule_id, app_id=run.app_id)
        if via is not None:
            use.via_workflow, use.via_version = via
        out.append(use)
    schedules = sorted({u.schedule_id for u in out if u.schedule_id})
    makers = dict((await session.execute(select(Schedule.id, Schedule.created_by)
                                         .where(Schedule.id.in_(schedules)))).all()) if schedules else {}
    managers = await _app_managers(session, [u.app_id for u in out if u.app_id])
    starters = dict((await session.execute(select(Run.id, Run.started_by).where(
        Run.id.in_([u.run_id for u in out])))).all()) if out else {}
    for use in out:
        if use.schedule_id:
            use.add_owner("user", makers.get(use.schedule_id) or starters.get(use.run_id))
        elif use.app_id:
            for owner in managers.get(use.app_id) or []:
                use.add_owner(*owner)
        else:
            use.add_owner("user", starters.get(use.run_id))
    return out, truncated


def _pins(graph: Any, workflow_id: str) -> set[int]:
    """一张图里固定了 workflow_id 的哪几版（子工作流、口径卡的 caliber_from；同 apps/versions.pinned_upstream 的认法，
    直接读 JSON，不为了这个把整张图解析成 GraphSpec）。"""
    out: set[int] = set()
    for node in (graph or {}).get("nodes") or [] if isinstance(graph, dict) else []:
        if not isinstance(node, dict):
            continue
        cfg = (node.get("data") or {}).get("config") if isinstance(node.get("data"), dict) else None
        if not isinstance(cfg, dict):
            continue
        if node.get("type") == "subgraph":
            wid, version = cfg.get("workflow_id"), cfg.get("workflow_version")
        elif node.get("type") == "metrics" and isinstance(cfg.get("caliber_from"), dict):
            wid, version = cfg["caliber_from"].get("workflow_id"), cfg["caliber_from"].get("workflow_version")
        else:
            continue
        if wid == workflow_id and str(version or "").isdigit():
            out.add(int(version))
    return out


async def pinned_versions(session: AsyncSession, workflow_id: str) -> dict[tuple[str, int], set[int]]:
    """别的工作流的已发布版本里固定了 workflow_id 的：{(工作流, 版本): 固定的 W 的版本}。"""
    rows = (await session.execute(
        select(WorkflowVersion.workflow_id, WorkflowVersion.version, WorkflowVersion.graph)
        .where(WorkflowVersion.workflow_id != workflow_id, WorkflowVersion.level.is_not(None)))).all()
    return {(wid, int(v)): pins for wid, v, graph in rows if (pins := _pins(graph, workflow_id))}


async def find_uses(session: AsyncSession, workflow_id: str, *, below: int | None = None,
                    only: int | None = None) -> Found:
    """W 的旧版本被谁用过（见模块说明）。below：只看版本号小于它的（召回：新版本）；only：只看这一版（版本用量）。"""
    from app.apps.versions import pinning_apps

    def version_ok(v: Any) -> bool:
        return isinstance(v, int) and (only is None or v == only) and (below is None or v < below)

    answers, cut_a = await _answers(session, workflow_id, below, only)
    own = Run.workflow_id == workflow_id
    if only is not None:
        own = and_(own, Run.version == only)
    elif below is not None:
        own = and_(own, Run.version < below)
    formal, cut_f = await _formal(session, own, kind_of=lambda r: (
        "schedule" if r.schedule_id else "app" if r.app_id else "formal", int(r.version), None))
    pins = await pinned_versions(session, workflow_id)
    pinned: list[Use] = []
    cut_p = False
    targets = {key: sorted(v for v in versions if version_ok(v)) for key, versions in pins.items()}
    targets = {key: vs for key, vs in targets.items() if vs}
    if targets:
        clause = or_(*(and_(Run.workflow_id == wid, Run.version == v) for (wid, v) in targets))
        pinned, cut_p = await _formal(session, clause, kind_of=lambda r: (
            "pinned", targets[(r.workflow_id, int(r.version))][-1], (r.workflow_id, int(r.version))))
    uses = [*answers, *formal, *pinned]
    await _exporters(session, uses)
    apps: dict[int, list[dict[str, Any]]] = {}
    for item in await pinning_apps(session, workflow_id):
        if version_ok(item.get("version")):
            apps.setdefault(int(item["version"]), []).append(item)
    return Found(uses=uses, apps=apps, truncated=cut_a or cut_f or cut_p)


async def _exporters(session: AsyncSession, uses: list[Use]) -> None:
    """这些运行上的报告导出审计：导出人进答案的主人。"""
    from app.db.models import AuditEvent

    ids = sorted({u.run_id for u in uses})
    if not ids:
        return
    by: dict[str, list[str]] = {}
    for rid, actor in (await session.execute(select(AuditEvent.subject_id, AuditEvent.actor).where(
            AuditEvent.action == EXPORTED, AuditEvent.subject_kind == "run",
            AuditEvent.subject_id.in_(ids)).order_by(AuditEvent.at))).all():
        if actor and actor not in by.setdefault(rid, []):
            by[rid].append(actor)
    for use in uses:
        use.exporters = list(by.get(use.run_id) or [])
        for actor in use.exporters:
            use.add_owner("user", actor)


# ==========================================================================
# 版本用量（版本历史、发布弹窗）
# ==========================================================================


async def exports_count(session: AsyncSession, run_ids: Iterable[str]) -> int:
    from app.db.models import AuditEvent

    ids = sorted(set(run_ids))
    if not ids:
        return 0
    return int(await session.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == EXPORTED, AuditEvent.subject_kind == "run", AuditEvent.subject_id.in_(ids))) or 0)


def summarize(found: Found) -> dict[str, Any]:
    """用法的计数（不含参数和主人）：问数回答几次（运行数）、导出几次、定时任务几个、应用几个、各种正式运行几次。"""
    answers = {u.run_id for u in found.uses if u.kind == "answer"}
    schedules = {u.schedule_id for u in found.uses if u.schedule_id}
    apps = {u.app_id for u in found.uses if u.app_id} | {a["app_id"] for items in found.apps.values() for a in items}
    return {"answers": len(answers), "schedules": len(schedules), "apps": len(apps),
            "formal": len({u.run_id for u in found.uses if u.kind in ("formal", "schedule", "app")}),
            "pinned": len({u.run_id for u in found.uses if u.kind == "pinned"}),
            "exports": sum(len(u.exporters) for u in {u.run_id: u for u in found.uses}.values()),
            "truncated": found.truncated}


async def version_usage(session: AsyncSession, workflow_id: str, version: int) -> dict[str, Any]:
    """一个版本的用法计数（账本接口 GET /api/ledger/workflow-versions/{wf}/{v} 加的那几项）：问数回答、导出、召回。

    召回分两头：这一版作为新版本召回过几组（recall_runs：以它为新版本发起过的召回运行；changed：其中数变了的）、作为旧版本
    被召回过几组（recalled_from）。"""
    from app.ledger import recall

    found = await find_uses(session, workflow_id, only=version)
    out = summarize(found)
    out["exports"] = await exports_count(session, [u.run_id for u in found.uses])
    out["recall"] = await recall.version_recall_counts(session, workflow_id, version)
    return out


async def workflow_name(session: AsyncSession, workflow_id: str) -> str:
    return str(await session.scalar(select(Workflow.name).where(Workflow.id == workflow_id)) or "")


__all__ = ["EXPORTED", "Found", "KINDS", "KIND_TEXT", "MAX_ANSWERS", "MAX_FORMAL", "PERIODS", "Use", "exports_count",
           "find_uses", "pinned_versions", "summarize", "version_usage", "workflow_name"]
