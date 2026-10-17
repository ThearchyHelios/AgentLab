"""沉淀：可沉淀的问题（认证队列）和「固化成口径草稿」（docs/design/next-round/02-口径账本.md 做法 8，拍板 L9）。

同一类问题被反复问、Agent 反复写出同一个形状的 SQL，就该沉淀成一份口径：以后再问，比对器认得出「等价已证明」，还能按口径
正式出具。以前的 from-run 提取保留 Agent，沉淀出的模板没有固定 SQL，以后永远比对不上；聚类又混着 history。

**认证队列**（GET /api/ledger/certify-queue，搭建者及以上）三种信号：
- 确定性重复（shape）：问数据的探索运行里执行过、被报告引用（比对工件里标签记着的查询）、又没对上任何口径（none / related）
  的查询，按（数据源、规范形）分组：sql_semantics.sql_shape 在判等价同一套规范形式上抹掉字面量、把 AND / OR 的各项、IN 列表、
  交换律成立的比较两边重排——条件的先后不同、AND 两边换位的进同一组，条件真不同的不进；认不出规范形的按 normalize_sql 的
  规范文本分组（只会分得细）。出现在 ≥ REPEAT_RUNS 次运行、来自 ≥ REPEAT_CONVERSATIONS 段对话的进队。跨运行变化的字面量
  位置（按规范形里的先后对齐）是参数候选，不变的是口径常量。展示的是代表那条的规范文本（字面量都换成了「?」，不带取值）。
  这一项的 id 按（数据源、规范形）算；以前按规范文本记的「不是口径」照样认（组里任何一条的规范文本对得上就算）；
- 问题聚类（cluster）：app/chat/questions 全站唯一的聚类（只取问题原文、不含 history、排除系统派生的运行），≥ REPEAT_RUNS
  个问题的类。这一项的 id 是类的 id（成员的稳定 id 排好取哈希：同一批问题两次读取一样）；「不是口径」记下当时的成员，之后
  这一类过半的成员是那时记下的就照样不进队（来了几句新问法 id 会变，按成员认就不失效）。以前按种子问题原文算的 id 记的
  照样认，记的成员读不懂的照实忽略；
- 被反复用的答案（reused）：导出过 ≥ 2 次（report.exported 审计，第 2 波）、做成过应用（蓝图的来源是这次运行）、在画布里
  打开过或提取过（工作流的 origin.run_id 是这次运行）的轮次。
每项给代表问题、轮次链接、（shape 的）SQL 形状和参数候选、有没有相关口径（可以对账的那份）、是不是已经固化过（frozen_into，
同一个工作流只列一次）。点「不是口径」
写一条审计（ledger.certify.dismiss，subject 是这一项的 id），之后不再进队。门槛同 ledger/stats（E3 的重复度）。

**固化成口径草稿**（freeze，from-run 加 freeze 选项后转调它）：代表运行里报告引用了的查询，各变成一个调用工具节点——SQL 用
规范化后的文本，跨轮变化的字面量换成 '{{ input.p1 }}'（同组的别的运行 peers 用来认哪些位置变了），入口字段的缺省值取
代表运行里的值；只有一行的结果每个数值列建一项口径卡指标（cell(nodes.取数, 0, '列')，指标名能从数据目录取到中文名的
用中文名，取不到用列名）；口径卡的名字用传入的 name，没传用代表问题；报告节点保留，指令在有入口参数时不写代表那一轮的问题
原文（里面的日期参数化之后就不对了），改写运行时渲染的参数，没有参数时照旧写问题原文；出口契约写 report_from +
metrics_from（有多行结果的另加 cells: true），过 validate_graph。新工作流记 origin.sources：
[{run_id, conversation_id, turn_id, question}]，这就是「口径 → 来源问题」的记录。之后由人在画布上审改、按受管级别发布——
提取器是作者，不是发布者。

**发布之后补比**（after_publish）：工作流发布时，origin.sources 里的来源运行在后台按现在的口径补比一次（trigger=certified，
标「按之后发布的口径回看」），E1 也就有了回溯数据。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models import Run

logger = logging.getLogger(__name__)

#: 门槛：同一个形状出现在几次运行里、来自几段对话；被导出几次算反复用（同 ledger/stats 的 E3）
REPEAT_RUNS = 3
REPEAT_CONVERSATIONS = 2
EXPORTS = 2
#: 看最近多少次问数据运行、每项最多列几轮
RUN_LIMIT = 500
TURNS_LIMIT = 10
QUESTION_LIMIT = 500
#: 「不是口径」的审计
DISMISS_ACTION = "ledger.certify.dismiss"
SUBJECT_KIND = "ledger_certify"
KINDS = ("shape", "cluster", "reused")

#: 固化被拒的说法
NOT_FOUND = "运行记录不存在，可能已被删除"
NOT_MATCHED = "这次运行还没有比对口径，比对完才能固化（打开它的口径账本会按需比对）"
NOT_SUCCEEDED = "这次运行没有成功结束，提取出的查询不完整"
NO_QUERIES = "这次运行的报告没有引用任何查询结果，没有可固化的取数"
UNPARSED = "这次的 SQL 无法规范化（{reason}），不能固化成固定的取数"
SNAPSHOT = "查询快照读不出来（文件缺失或与哈希不一致），不能固化"
#: 新工作流的说明、报告指令
DRAFT_NAME = "{question}（固化的口径草稿）"
DRAFT_DESCRIPTION = ("从问数据的回答固化：报告引用了的 {n} 条查询变成固定的取数，跨轮变化的 {p} 个字面量成为入口参数。"
                     "经人工审核、按受管级别发布后，再问同样的问题就能认出等价、按口径出具")
REPORT_INSTRUCTIONS = "回答：{question}"
#: 有入口参数时的报告指令：不写代表那一轮的问题原文——里面的日期（「上周（10月5日到11日）」「九月第一周」）参数化之后
#: 就不对了；改写这次运行时渲染出来的参数。{params} 是「p1 = {{ input.p1 }}，…」
REPORT_INSTRUCTIONS_PARAMS = "按取到的数回答这个口径的问题（这次的参数：{params}；时间范围、筛选的取值以参数为准）"


class CertifyRefused(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def item_id(kind: str, key: str) -> str:
    return f"{kind}:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


# ==========================================================================
# SQL 的形状和参数
# ==========================================================================


@dataclass(frozen=True)
class Shape:
    """一条查询的形状：数据源、规范文本（字面量换成「?」）、字面量（SQL 写法，字符串带引号）、规范化失败的原因。

    key 是分组用的规范形（sql_semantics.sql_shape，「shape:」开头；认不出时退回规范文本，「text:」开头）；canonical 是按
    规范形里的先后排的字面量（同一组的几条按位置对得上，认哪些位置跨轮变了用它），positions[i] 是它在 literals 里的位置
    （规范化时新算出来的字面量为 None：没法写成模板参数）。"""

    source: str
    text: str
    literals: tuple[str, ...]
    problem: str | None = None
    key: str = ""
    canonical: tuple[str, ...] = ()
    positions: tuple[int | None, ...] = ()

    def params(self, canonical_positions: Sequence[int]) -> list[int]:
        """规范形里的位置 → literals 里的位置（template_of 要的）；对不上的不要。"""
        found = (self.positions[i] for i in canonical_positions if i < len(self.positions))
        return sorted(p for p in found if p is not None)


def shape_of(snap: Any, kinds: dict[str, str | None]) -> Shape | None:
    from app.data.sql_semantics import normalize_sql, sql_shape

    if not isinstance(snap, dict) or not isinstance(snap.get("sql"), str) or not snap["sql"].strip():
        return None
    source = str(snap.get("source") or "")
    norm = normalize_sql(snap["sql"], kinds.get(source))
    found = sql_shape(snap["sql"], kinds.get(source)) if not norm.parse_error else None
    if found is None:
        return Shape(source, norm.text, norm.literals, norm.parse_error, key=f"text:{norm.text}",
                     canonical=norm.literals, positions=tuple(range(len(norm.literals))))
    return Shape(source, norm.text, norm.literals, None, key=f"shape:{found.key}", canonical=found.literals,
                 positions=found.positions)


def varying(literals: Sequence[Sequence[str]]) -> list[int]:
    """几次运行同一个形状的字面量：哪几个位置跨运行变过（参数候选），不变的是口径常量。长度不一致的那几份不算。"""
    rows = [list(row) for row in literals]
    if not rows:
        return []
    width = len(rows[0])
    same = [r for r in rows if len(r) == width]
    return [i for i in range(width) if len({r[i] for r in same}) > 1]


def _unquote(literal: str) -> str | None:
    """'2026-09-01' → 2026-09-01；不是普通单引号字符串（N'…'、x'…'、$$…$$）的为 None。"""
    if len(literal) >= 2 and literal[0] == "'" and literal[-1] == "'":
        return literal[1:-1].replace("''", "'")
    return None


def _number(literal: str) -> int | float | None:
    try:
        value = float(literal)
    except ValueError:
        return None
    return int(value) if value.is_integer() and "." not in literal and "e" not in literal.lower() else value


def template_of(shape: Shape, params: Sequence[int], dialect: str | None, *,
                start: int = 1) -> tuple[str, list[dict[str, Any]]]:
    """规范文本 + 参数位置 → (口径的取数 SQL 模板, 入口字段)。参数位置的字面量换成 '{{ input.pN }}'（字符串）或
    {{ input.pN }}（数，N 从 start 起），缺省值是这一次的值；别的位置照原样写回字面量。规范文本按方言重新切词，按「?」的
    先后对上字面量。"""
    from app.data.sql_semantics import PLACEHOLDER, _read, _tokens

    tokens = [t for t in _tokens(shape.text, _read(dialect)) if shape.text[t.start:t.end + 1] == PLACEHOLDER]
    if len(tokens) != len(shape.literals):
        raise CertifyRefused(409, UNPARSED.format(reason="字面量对不上"))
    out: list[str] = []
    fields: list[dict[str, Any]] = []
    pos = 0
    wanted = set(params)
    for i, (token, literal) in enumerate(zip(tokens, shape.literals)):
        out.append(shape.text[pos:token.start])
        pos = token.end + 1
        text, number = _unquote(literal), _number(literal)
        if i in wanted and (text is not None or number is not None):
            name = f"p{start + len(fields)}"
            if text is not None:
                out.append(f"'{{{{ input.{name} }}}}'")
                fields.append({"name": name, "type": "string", "required": True, "default": text})
            else:
                out.append(f"{{{{ input.{name} }}}}")
                fields.append({"name": name, "type": "number", "required": True, "default": number})
        else:
            out.append(literal)
    out.append(shape.text[pos:])
    return "".join(out), fields


# ==========================================================================
# 认证队列
# ==========================================================================


@dataclass
class _Member:
    run_id: str
    artifact: str
    #: 按规范形里的先后排的字面量（Shape.canonical）
    literals: tuple[str, ...]
    conversation_id: str | None
    turn_id: str | None
    question: str
    related: list[dict[str, Any]] = field(default_factory=list)
    shape: Shape | None = None


def referenced_queries(content: dict[str, Any]) -> list[str]:
    """比对工件里被报告引用了的查询（标签记着的：单元格的 query、指标的 queries），按先后去重。"""
    out: list[str] = []
    for report in (content.get("reports") or {}).values():
        for label in (report.get("labels") or {}).values():
            for artifact in [label.get("query"), *(label.get("queries") or [])]:
                if isinstance(artifact, str) and artifact and artifact not in out:
                    out.append(artifact)
    return out


async def _turns(session: Any, runs: list[Run]) -> dict[str, dict[str, Any]]:
    """运行 → {conversation_id, turn_id, question}：运行来源里封存的（origin_ref）；老运行退回 conversation_turns.run_id。"""
    from app.db.models import ConversationTurn

    out: dict[str, dict[str, Any]] = {}
    ids = [r.id for r in runs]
    if ids:
        for turn in (await session.execute(select(ConversationTurn).where(ConversationTurn.run_id.in_(ids)))).scalars():
            out.setdefault(turn.run_id, {"conversation_id": turn.conversation_id, "turn_id": turn.id,
                                         "question": turn.question or ""})
    for run in runs:
        origin = run.origin if isinstance(run.origin, dict) else {}
        found = out.setdefault(run.id, {"conversation_id": None, "turn_id": None, "question": ""})
        if origin.get("conversation_id"):
            found["conversation_id"] = str(origin["conversation_id"])
            found["turn_id"] = str(origin.get("turn_id") or found.get("turn_id") or "") or None
        if not found["question"] and isinstance(run.input, dict) and isinstance(run.input.get("question"), str):
            found["question"] = run.input["question"]
    return out


async def _dismissed(session: Any) -> set[str]:
    from app.db.models import AuditEvent

    return {sid for (sid,) in (await session.execute(select(AuditEvent.subject_id).where(
        AuditEvent.action == DISMISS_ACTION, AuditEvent.subject_kind == SUBJECT_KIND))).all() if sid}


async def _dismissed_members(session: Any) -> list[set[str]]:
    """问题聚类上点过「不是口径」时记下的成员（每条一组稳定 id）。老记录没记成员、记的读不懂（不是字符串列表）的不算。"""
    from app.db.models import AuditEvent

    out: list[set[str]] = []
    for (data,) in (await session.execute(select(AuditEvent.data).where(
            AuditEvent.action == DISMISS_ACTION, AuditEvent.subject_kind == SUBJECT_KIND))).all():
        members = data.get("members") if isinstance(data, dict) else None
        if isinstance(members, list) and members and all(isinstance(m, str) and m for m in members):
            out.append(set(members))
    return out


def _cluster_dismissed(cluster: Any, dismissed: set[str], members: list[set[str]]) -> bool:
    """这一类点过「不是口径」没有：类的 id 记过；以前按种子问题原文算的 id 记过；或者过半的成员是某一次记下的。"""
    legacy = item_id("cluster", " ".join(cluster.members[0].text.split()))
    if f"cluster:{cluster.id}" in dismissed or legacy in dismissed:
        return True
    keys = set(cluster.keys)
    return any(len(keys & seen) * 2 > len(keys) for seen in members)


async def _frozen(session: Any) -> dict[str, list[dict[str, Any]]]:
    """来源运行 → 从它固化出的工作流 [{workflow_id, name, status, published_version}]（origin.sources）。"""
    from app.db.models import Workflow

    out: dict[str, list[dict[str, Any]]] = {}
    for wf in (await session.execute(select(Workflow).where(Workflow.origin.is_not(None)))).scalars():
        for src in (wf.origin or {}).get("sources") or [] if isinstance(wf.origin, dict) else []:
            if isinstance(src, dict) and src.get("run_id"):
                out.setdefault(str(src["run_id"]), []).append(
                    {"workflow_id": wf.id, "name": wf.name, "status": wf.status,
                     "published_version": wf.published_version})
    return out


def _frozen_of(run_ids: Sequence[str | None], frozen: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """这几次运行固化进了哪些工作流：同一个工作流只列一次（固化时同组的运行都记进 origin.sources，一项里的几次运行常常
    指着同一个工作流），按先后。"""
    out: dict[str, dict[str, Any]] = {}
    for run_id in run_ids:
        for w in frozen.get(run_id or "", []):
            out.setdefault(str(w["workflow_id"]), w)
    return list(out.values())


def _turn_view(member: Any) -> dict[str, Any]:
    return {"run_id": member.run_id, "conversation_id": member.conversation_id, "turn_id": member.turn_id,
            "question": member.question}


async def queue(session: Any, *, principal: Any) -> dict[str, Any]:
    """认证队列（见模块说明）：{items: [...], threshold, considered}。只读。"""
    from app.chat.questions import cluster_questions, embedder, recent_questions
    from app.core.artifact_store import load
    from app.db.models import Artifact, DataSource
    from app.engine.origin import counted_runs
    from app.ledger import store

    dismissed = await _dismissed(session)
    dismissed_members = await _dismissed_members(session)
    frozen = await _frozen(session)
    kinds = {name: kind for name, kind in (await session.execute(select(DataSource.name, DataSource.kind))).all()}
    runs = list((await session.execute(select(Run).where(
        Run.origin_kind == "chat", Run.status == "succeeded", Run.run_class != "formal", counted_runs())
        .order_by(Run.created_at.desc()).limit(RUN_LIMIT))).scalars())
    turns = await _turns(session, runs)
    by_run = {r.id: r for r in runs}
    # 每次运行最新的一份比对（store.latest 按事件认这一次封存的；这里只要内容，按工件引用的时间取最新）
    latest: dict[str, tuple[Any, str]] = {}
    if runs:
        for artifact, run_id, at in (await session.execute(select(Artifact.id, Artifact.run_id, Artifact.created_at)
                                                           .where(Artifact.kind == store.KIND,
                                                                  Artifact.run_id.in_(list(by_run))))).all():
            if run_id not in latest or (at and latest[run_id][0] and at > latest[run_id][0]):
                latest[run_id] = (at, artifact)

    def read() -> dict[tuple[str, str], list[_Member]]:
        groups: dict[tuple[str, str], list[_Member]] = {}
        for run_id, (_, artifact) in latest.items():
            try:
                content = load(artifact)
            except ValueError:
                continue
            if not isinstance(content, dict) or content.get("format") != store.FORMAT:
                continue
            statuses = {q.get("artifact"): q for q in content.get("queries") or []}
            for query in referenced_queries(content):
                row = statuses.get(query) or {}
                if row.get("status") not in ("none", "related"):
                    continue
                try:
                    shape = shape_of(load(query), kinds)
                except ValueError:
                    shape = None
                if shape is None or shape.problem:
                    continue
                info = turns.get(run_id) or {}
                related = [{k: c.get(k) for k in ("caliber", "workflow_id", "version", "level")}
                           for c in row.get("candidates") or [] if c.get("status") == "related"]
                groups.setdefault((shape.source, shape.key), []).append(_Member(
                    run_id, query, shape.canonical, info.get("conversation_id"), info.get("turn_id"),
                    str(info.get("question") or ""), related, shape))
        return groups

    groups = await asyncio.to_thread(read)
    items: list[dict[str, Any]] = []
    for (source, key), members in groups.items():
        once = list({m.run_id: m for m in members}.values())
        conversations = {m.conversation_id or f"run:{m.run_id}" for m in once}
        if len(once) < REPEAT_RUNS or len(conversations) < REPEAT_CONVERSATIONS:
            continue
        iid = item_id("shape", f"{source}\n{key}")
        # 以前按规范文本记的「不是口径」：组里任何一条的规范文本对得上就照样不进队
        legacy = {item_id("shape", f"{source}\n{m.shape.text}") for m in once if m.shape is not None}
        if iid in dismissed or legacy & dismissed:
            continue
        rep = once[0]
        shape = rep.shape
        if shape is None:
            continue
        varied = varying([m.literals for m in once])
        params = [(i, p) for i in varied if i < len(shape.positions) and (p := shape.positions[i]) is not None]
        related = list({r["caliber"]: r for m in once for r in m.related if r.get("caliber")}.values())
        items.append({
            "id": iid, "kind": "shape", "source": source, "shape": shape.text, "runs": len(once),
            "conversations": len(conversations), "question": rep.question,
            "turns": [_turn_view(m) for m in once[:TURNS_LIMIT]],
            # index 是代表那条规范文本里的第几个「?」；distinct 按规范形对齐了数
            "params": [{"index": p, "distinct": len({m.literals[i] for m in once if len(m.literals) > i})}
                       for i, p in sorted(params, key=lambda ip: ip[1])],
            "constants": len(shape.literals) - len(params), "related": related,
            "representative": {"run_id": rep.run_id, "query": rep.artifact},
            "peers": [m.run_id for m in once[1:]],
            "frozen_into": _frozen_of([m.run_id for m in once], frozen),
        })
    # 问题聚类
    questions = await recent_questions(session, principal, limit=QUESTION_LIMIT)
    for cluster in await cluster_questions(questions) if questions else []:
        if cluster.count < REPEAT_RUNS:
            continue
        seed = cluster.members[0]
        iid = f"cluster:{cluster.id}"
        if _cluster_dismissed(cluster, dismissed, dismissed_members):
            continue
        items.append({"id": iid, "kind": "cluster", "question": seed.text, "count": cluster.count,
                      "turns": [{"run_id": q.run_id, "turn_id": q.turn_id, "question": q.text}
                                for q in cluster.members[:TURNS_LIMIT]],
                      "frozen_into": _frozen_of([q.run_id for q in cluster.members], frozen)})
    items += await _reused(session, runs, turns, dismissed, frozen)
    return {"items": items, "considered": {"runs": len(runs), "matched": len(latest), "questions": len(questions)},
            "threshold": {"runs": REPEAT_RUNS, "conversations": REPEAT_CONVERSATIONS, "exports": EXPORTS},
            "embedder": embedder()}


async def _reused(session: Any, runs: list[Run], turns: dict[str, dict[str, Any]], dismissed: set[str],
                  frozen: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """被反复用的答案：导出过 ≥ EXPORTS 次、做成过应用、在画布里打开过或提取过（见模块说明）。"""
    from sqlalchemy import func

    from app.db.models import AuditEvent, Workflow
    from app.db.models_blueprints import Blueprint

    ids = [r.id for r in runs]
    if not ids:
        return []
    exports = {sid: int(n) for sid, n in (await session.execute(
        select(AuditEvent.subject_id, func.count()).where(AuditEvent.action == "report.exported",
                                                           AuditEvent.subject_kind == "run",
                                                           AuditEvent.subject_id.in_(ids))
        .group_by(AuditEvent.subject_id))).all()}
    apps: set[str] = set()
    for source in (await session.execute(select(Blueprint.source))).scalars():
        if isinstance(source, dict) and source.get("kind") == "run" and source.get("id") in ids:
            apps.add(str(source["id"]))
    canvas: dict[str, list[str]] = {}
    for wf_id, origin, tags in (await session.execute(select(Workflow.id, Workflow.origin, Workflow.tags)
                                                      .where(Workflow.origin.is_not(None)))).all():
        if isinstance(origin, dict) and origin.get("run_id") in ids and not origin.get("sources"):
            canvas.setdefault(str(origin["run_id"]), []).append("extracted" if "extracted" in (tags or []) else "canvas")
    out: list[dict[str, Any]] = []
    for run in runs:
        uses = {"exports": exports.get(run.id, 0), "app": run.id in apps,
                "extracted": "extracted" in canvas.get(run.id, []), "canvas": "canvas" in canvas.get(run.id, [])}
        if not (uses["exports"] >= EXPORTS or uses["app"] or uses["extracted"] or uses["canvas"]):
            continue
        iid = item_id("reused", run.id)
        if iid in dismissed:
            continue
        info = turns.get(run.id) or {}
        out.append({"id": iid, "kind": "reused", "question": info.get("question") or "", "uses": uses,
                    "turns": [{"run_id": run.id, "conversation_id": info.get("conversation_id"),
                               "turn_id": info.get("turn_id"), "question": info.get("question") or ""}],
                    "representative": {"run_id": run.id}, "frozen_into": _frozen_of([run.id], frozen)})
    return out


async def dismiss(session: Any, iid: str, *, kind: str | None = None, note: str | None = None,
                  principal: Any = None) -> None:
    """「不是口径」：写一条审计，之后这一项不再进队。问题聚类另记下这一类现在的成员（服务端按同一批问题重新聚一次找到
    这一类，不收客户端传的成员；找不到——这会儿问题又多了、聚出来的已经不是这一类——就只记 id，照实）。"""
    from app.auth.audit import record

    members: list[str] = []
    if principal is not None and (kind == "cluster" or iid.startswith("cluster:")):
        from app.chat.questions import cluster_questions, recent_questions

        try:
            questions = await recent_questions(session, principal, limit=QUESTION_LIMIT)
            found = next((c for c in await cluster_questions(questions) if f"cluster:{c.id}" == iid), None) \
                if questions else None
            members = found.keys if found is not None else []
        except Exception:  # noqa: BLE001 - 取向量出错：只记 id，「不是口径」照样记上
            logger.exception("「不是口径」时重新聚类出错，只按 id 记")
    await record(session, DISMISS_ACTION, SUBJECT_KIND, iid,
                 data={k: v for k, v in {"kind": kind, "note": note, "members": members}.items() if v})
    await session.commit()


# ==========================================================================
# 固化成口径草稿
# ==========================================================================


async def _labels(session: Any, snap: dict[str, Any], source: str) -> Any:
    """结果列的中文名：先看查询当时冻结的数据目录（快照的 schema_artifact，同口径比对），没冻结的（老快照）看数据源现在
    的目录。只认列名本身就是目录里的一列（SELECT amount、SUM(amount) AS amount）；算出来起的别名（gmv）取不到，调用方
    照旧用列名。返回 _labeler：(表, 列) → 中文名或 None。"""
    from app.data import catalog
    from app.data.sql_semantics import _labeler, catalog_labels
    from app.db.models import DataSource
    from app.ledger.match import _frozen as frozen_schema

    _, names = frozen_schema(snap.get("schema_artifact"))
    if not names:
        source_id = (await session.execute(select(DataSource.id).where(DataSource.name == source))).scalar()
        if source_id:
            entries = await catalog.read_catalog(session, source_id)
            names = catalog_labels({table: entry.notes for table, entry in entries.items()})
    return _labeler(names)


async def freeze(session: Any, run_id: str, *, peers: Sequence[str] = (), name: str = "",
                 conversation_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]:
    """把一次问数据运行固化成口径草稿（见模块说明），落库成新的草稿工作流，返回 {workflow_id, name, nodes, edges, params,
    sources, ...}。peers 是同一组（同一个 SQL 形状）的别的运行：认哪些字面量位置跨轮变了。不能固化抛 CertifyRefused。"""
    from app.api.deps import can_read_run, current_principal
    from app.api.evidence import _reports, _sealed
    from app.core.artifact_store import graph_hash, load
    from app.db.models import DataSource, Workflow, WorkflowVersion
    from app.engine.governance import REPORT_POLICY
    from app.engine.layout import auto_layout
    from app.engine.schema import GraphSpec
    from app.ledger import store
    from app.tools.datasource import QUERY_PREFIX

    run = await session.get(Run, run_id)
    principal = current_principal()
    if run is None or not await can_read_run(principal, run):
        raise CertifyRefused(404, NOT_FOUND)
    if run.status != "succeeded":
        raise CertifyRefused(409, NOT_SUCCEEDED)
    recorded = await store.latest(run_id)
    if recorded is None or recorded.content is None:
        raise CertifyRefused(409, NOT_MATCHED)
    queries = referenced_queries(recorded.content)
    if not queries:
        raise CertifyRefused(409, NO_QUERIES)
    kinds = {n: k for n, k in (await session.execute(select(DataSource.name, DataSource.kind))).all()}

    # 同组的别的运行：它们封存的查询里同形状（规范形相同，见 Shape.key）的那一条的字面量，按规范形里的先后
    peer_literals: dict[tuple[str, str], list[tuple[str, ...]]] = {}
    peer_sources: list[dict[str, Any]] = []
    peer_runs = [r for r in [await session.get(Run, p) for p in dict.fromkeys(peers) if p != run_id] if r is not None]
    readable = [r for r in peer_runs if await can_read_run(principal, r)]
    for peer in readable:
        sealed = await _sealed(peer.id)
        for artifact in sealed.queries:
            try:
                shape = shape_of(load(artifact), kinds)
            except ValueError:
                continue
            if shape is not None and not shape.problem:
                peer_literals.setdefault((shape.source, shape.key), []).append(shape.canonical)
    sealed = await _sealed(run_id)
    shapes: list[Shape] = []
    snaps: list[dict[str, Any]] = []
    for artifact in queries:
        try:
            snap = load(artifact)
        except ValueError:
            raise CertifyRefused(409, SNAPSHOT) from None
        shape = shape_of(snap, kinds)
        if shape is None:
            raise CertifyRefused(409, SNAPSHOT)
        if shape.problem:
            raise CertifyRefused(409, UNPARSED.format(reason=shape.problem))
        shapes.append(shape)
        snaps.append(snap)

    def nid(i: int) -> str:
        return "fetch" if len(shapes) == 1 else f"fetch_{i + 1}"

    nodes: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    multi = False
    for i, (shape, snap) in enumerate(zip(shapes, snaps)):
        params = shape.params(varying([shape.canonical, *peer_literals.get((shape.source, shape.key), [])]))
        template, new_fields = template_of(shape, params, kinds.get(shape.source), start=len(fields) + 1)
        fields += new_fields
        nodes.append({"id": nid(i), "type": "tool", "position": {"x": 0, "y": 0},
                      "data": {"label": f"取数 {i + 1}" if len(shapes) > 1 else "取数",
                               "config": {"tool": f"{QUERY_PREFIX}{shape.source}", "args": {"sql": template}}}})
        rows = snap.get("rows") if isinstance(snap.get("rows"), list) else []
        columns = [str(c) for c in snap.get("columns") or []]
        if len(rows) == 1 and isinstance(rows[0], (list, tuple)):
            label = await _labels(session, snap, shape.source)
            for col, value in zip(columns, rows[0]):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics.append({"id": f"m{len(metrics) + 1}", "name": label((None, col)) or col,
                                    "expression": f"cell(nodes.{nid(i)}, 0, {col!r})"})
        else:
            multi = True
    question = ((await _turns(session, [run])).get(run.id) or {}).get("question") or ""
    reports = _reports(sealed)
    report_cfg: dict[str, Any] = {}
    if reports:
        found = next((n for n in (run.graph or {}).get("nodes") or [] if n.get("id") == reports[0].node_id), None)
        cfg = ((found or {}).get("data") or {}).get("config") or {}
        report_cfg = {k: cfg[k] for k in ("provider", "model", "entities") if k in cfg}
    if fields:
        report_cfg["instructions"] = REPORT_INSTRUCTIONS_PARAMS.format(
            params="，".join(f"{f['name']} = {{{{ input.{f['name']} }}}}" for f in fields))
    else:
        # 没有入口参数：取数的字面量都是口径的常量，问题原文里的日期和它们对得上
        report_cfg["instructions"] = REPORT_INSTRUCTIONS.format(question=question or "（问题）")
    # 报告的核对规则一律按受管级别的要求写（engine/governance.REPORT_POLICY，和发布门禁同一份）：草稿要能按受管发布
    report_cfg.update({key: value for key, value, _ in REPORT_POLICY})
    contract: dict[str, Any] = {"report_from": "write"}
    tail: list[dict[str, Any]] = []
    if metrics:
        tail.append({"id": "card", "type": "metrics", "position": {"x": 0, "y": 0},
                     "data": {"label": "口径卡", "config": {"caliber": (name.strip() or question or "口径")[:40],
                                                         "caliber_version": "v1", "metrics": metrics}}})
        report_cfg["metrics_from"] = ["card"]
        # 受管出具要写必需指标（不然「不予出具」这一档触发不了）：固化出的每个指标都是这个口径要给的数
        contract.update({"metrics_from": "card", "required": [m["id"] for m in metrics]})
    if multi or not metrics:
        contract["cells"] = True
    chain = [{"id": "start", "type": "input", "position": {"x": 0, "y": 0},
              "data": {"label": "入口", "config": {"fields": fields}}},
             *nodes, *tail,
             {"id": "write", "type": "report", "position": {"x": 0, "y": 0},
              "data": {"label": "报告", "config": report_cfg}},
             {"id": "out", "type": "output", "position": {"x": 0, "y": 0},
              "data": {"label": "出口", "config": {"fields": [{"name": "报告", "value": "{{ nodes.write.text }}"}],
                                                  "contract": contract}}}]
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(chain, chain[1:])]
    graph = auto_layout(GraphSpec.model_validate({"nodes": chain, "edges": edges})).model_dump(mode="json")

    info = (await _turns(session, [run])).get(run.id) or {}
    sources = [{"run_id": run.id, "conversation_id": conversation_id or info.get("conversation_id"),
                "turn_id": turn_id or info.get("turn_id"), "question": question}]
    for peer, peer_info in (await _turns(session, readable)).items():
        sources.append({"run_id": peer, "conversation_id": peer_info.get("conversation_id"),
                        "turn_id": peer_info.get("turn_id"), "question": peer_info.get("question") or ""})
    workflow = Workflow(
        name=(name or DRAFT_NAME.format(question=(question or run.workflow_name or "问数据")[:30]))[:200],
        description=DRAFT_DESCRIPTION.format(n=len(shapes), p=len(fields)), graph=graph, tags=["certified"],
        status="draft",
        origin={"run_id": run.id, **({"conversation_id": sources[0]["conversation_id"]} if sources[0]["conversation_id"]
                                     else {}), **({"turn_id": sources[0]["turn_id"]} if sources[0]["turn_id"] else {}),
                "freeze": True, "sources": sources})
    session.add(workflow)
    await session.flush()
    session.add(WorkflowVersion(workflow_id=workflow.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                                note=f"从运行 {run.id} 固化"))
    await session.commit()
    return {"workflow_id": workflow.id, "name": workflow.name, "nodes": len(chain), "edges": len(edges),
            "dropped_nodes": 0, "source_run": run.id, "frozen": True, "params": [f["name"] for f in fields],
            "metrics": [m["id"] for m in metrics], "sources": sources}


# ==========================================================================
# 发布之后补比
# ==========================================================================

_background: set[asyncio.Task[Any]] = set()


def after_publish(workflow_id: str, origin: Any) -> int:
    """工作流发布了：从问数据固化来的（origin.sources），来源运行在后台按现在的口径补比一次（trigger=certified）。返回排上了
    几个。不挡发布，失败只记日志（ledger/match 的 schedule 自己兜着）。"""
    from app.ledger import match as ledger_match

    sources = origin.get("sources") if isinstance(origin, dict) else None
    count = 0
    for src in sources or []:
        if isinstance(src, dict) and src.get("run_id"):
            try:
                if ledger_match.schedule(str(src["run_id"]), trigger="certified"):
                    count += 1
            except Exception:  # noqa: BLE001 - 补比是辅助步骤
                logger.exception("工作流 %s 发布后补比来源运行出错", workflow_id)
    return count


__all__ = ["DISMISS_ACTION", "KINDS", "REPEAT_CONVERSATIONS", "REPEAT_RUNS", "CertifyRefused", "Shape", "after_publish",
           "dismiss", "freeze", "item_id", "queue", "referenced_queries", "shape_of", "template_of", "varying"]
