"""会话：把一串问答串成一次对话。

在此之前"对话"只是前端内存里的一个数组——刷新页面就没了，而每一轮又各自
从零建图，所以"上一轮问过什么"在系统里无处可查。这个模块把它落到库里，
同时服务两件事：给用户看的历史列表，和给模型看的上下文（`_history_text`
被 copilot 拿去拼进 prompt）。

轮次的写入由前端驱动，分两步：开轮（只有问题）→ 回填（图、run、答案）。
让 copilot 或 runner 自己去写会更"自动"，但建图失败、运行失败、用户中途
取消这三种半途而废的情况它们各自只知道一半，最后还是要在三个地方各接一次。
前端本来就完整地知道这一轮走到哪了。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session, utcnow
from app.db.models import Approval, Conversation, ConversationTurn, Run

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

#: 喂给模型的历史轮数上限。再多意义不大，而 prompt 是要花钱的
HISTORY_TURNS = 6
#: 单轮问答在上下文里的截断长度。一次查询结果可能有几千字，整段塞进去
#: 会把后面几轮挤掉——那还不如少说点、多留几轮
HISTORY_ANSWER_CHARS = 600


def title_from(question: str, limit: int = 24) -> str:
    """用第一问当标题。没有标题的会话在列表里是一排"新对话"，等于没有列表。"""
    text = " ".join((question or "").split())
    if not text:
        return "新对话"
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------
# 出入参
# --------------------------------------------------------------------------


class ConversationIn(BaseModel):
    kind: str = Field(default="chat", pattern="^(chat|canvas)$")
    workflow_id: str | None = None
    title: str = ""


class ConversationPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


class TurnIn(BaseModel):
    question: str = ""


class TurnPatch(BaseModel):
    """回填。全是可选——一轮可能在任何一步停下，能填多少填多少。"""

    question: str | None = None
    answer: str | None = None
    explanation: str | None = None
    graph: dict[str, Any] | None = None
    run_id: str | None = None
    status: str | None = Field(default=None, pattern="^(running|done|error)$")
    error: str | None = None
    review: dict[str, Any] | None = None
    #: 可信度元数据，前端整块写、整块读，后端不解读它的内容（列表状态除外，见 last_status）
    meta: dict[str, Any] | None = None


class TurnOut(BaseModel):
    id: str
    seq: int
    question: str
    answer: str
    explanation: str
    graph: dict[str, Any] | None = None
    run_id: str | None = None
    status: str
    error: str
    #: 复核结论。None = 这一轮没复核过，和「复核过、没发现问题」不是一回事
    review: dict[str, Any] | None = None
    #: 可信度元数据。老轮次挂在 review.meta 下，读出来时统一放到这里
    meta: dict[str, Any] | None = None
    created_at: Any = None
    #: 最后一次落库。列表判「建图断了」按它算（BUILD_STALE），前端用同一只钟
    updated_at: Any = None

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    title: str
    kind: str
    workflow_id: str | None = None
    archived: bool
    created_at: Any = None
    last_active_at: Any = None
    turn_count: int = 0
    #: 列表里给个副标题，让人一眼认出是哪次聊天
    last_question: str = ""
    #: 最后一轮停在哪：running | waiting | error | cancelled | suspended | done，
    #: 没有轮次时为 None。见 last_status()
    last_status: str | None = None
    #: 最后一轮的运行。建图阶段就断了的没有
    last_run_id: str | None = None

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    turns: list[TurnOut] = Field(default_factory=list)

    # from_attributes 不能直接吃 Conversation：pydantic 会去读同名的 turns
    # 关系属性，而那是个未加载的 lazy relationship，在异步上下文里一读就是
    # MissingGreenlet。turns 一律显式传进来
    model_config = {"from_attributes": False}


def turn_meta(turn: Any) -> dict[str, Any] | None:
    """这一轮的元数据：meta 列优先，老数据退回 review.meta。"""
    if isinstance(turn.meta, dict):
        return turn.meta
    bridged = turn.review.get("meta") if isinstance(turn.review, dict) else None
    return bridged if isinstance(bridged, dict) else None


def turn_run_id(turn: Any) -> str | None:
    """这一轮现在的运行。meta 里写了 runId 就以它为准，哪怕是 null：重试会换一次运行，
    run_id 列却清不掉（回填里 None 表示「不改」），还指着上一次的。老轮次没有这个键，看列。"""
    meta = turn_meta(turn) or {}
    if "runId" in meta:
        return meta["runId"] or None
    return turn.run_id or None


def _turn_out(turn: ConversationTurn) -> TurnOut:
    out = TurnOut.model_validate(turn)
    out.meta = turn_meta(turn)
    return out


def _detail(row: Conversation, turns: list[ConversationTurn]) -> ConversationDetail:
    base = ConversationOut.model_validate(row)
    return ConversationDetail(
        **base.model_dump(),
        turns=[_turn_out(t) for t in turns],
    )


#: 还没有运行的轮次停在 running 超过这么久，就当建图断了。建图跟着浏览器那条流走，
#: 页面一关就停，轮次却一直是 running；最长的正常情况是模型超时再加两轮自查修正
BUILD_STALE = timedelta(minutes=15)


def last_status(
    turn: Any, run_status: str | None, pending: bool, now: datetime | None = None,
) -> str:
    """一轮停在哪。轮次状态只有 running / done / error 三种，细分要看运行和元数据。

    轮次状态是前端写的：页面关掉、服务重启时它停在 running，这时要以运行的状态为准；
    运行也没有的，是断在了建图阶段。已经收尾的轮次以轮次自己为准——它写下的就是
    用户看到的结局（取消、服务重启挂起在 error 里分开记）。
    """
    outcome = (turn_meta(turn) or {}).get("outcome")
    if turn.status == "done":
        return "done"
    if turn.status == "error":
        if outcome == "cancelled" or (turn.error or "").strip() == "已取消":
            return "cancelled"
        if outcome == "suspended":
            # 挂起之后可能已经在别处接着跑了
            live = _from_run(run_status, pending)
            return live if live in ("running", "waiting") else "suspended"
        return "error"
    live = _from_run(run_status, pending)
    if live:
        return live
    updated = turn.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    stale = updated is not None and (now or utcnow()) - updated > BUILD_STALE
    return "error" if stale else "running"


def _from_run(run_status: str | None, pending: bool) -> str | None:
    return {
        "queued": "running", "running": "running",
        "interrupted": "waiting" if pending else "suspended",
        "succeeded": "done", "failed": "error", "cancelled": "cancelled",
    }.get(run_status or "")


async def _last_turns(
    session: AsyncSession, ids: list[str]
) -> dict[str, ConversationTurn]:
    """每个会话 seq 最大的那一轮。一次查完，列表长了也只是一次往返。"""
    latest = (
        select(ConversationTurn.conversation_id, func.max(ConversationTurn.seq).label("seq"))
        .where(ConversationTurn.conversation_id.in_(ids))
        .group_by(ConversationTurn.conversation_id)
        .subquery()
    )
    rows = (await session.execute(
        select(ConversationTurn).join(latest, and_(
            ConversationTurn.conversation_id == latest.c.conversation_id,
            ConversationTurn.seq == latest.c.seq,
        ))
    )).scalars()
    return {t.conversation_id: t for t in rows}


async def _statuses(
    session: AsyncSession, turns: dict[str, ConversationTurn]
) -> dict[str, tuple[str, str | None]]:
    """会话 id → (last_status, last_run_id)。运行状态和待审批各一次查询。"""
    run_of = {cid: turn_run_id(t) for cid, t in turns.items()}
    run_ids = {r for r in run_of.values() if r}
    runs: dict[str, str] = {}
    waiting: set[str] = set()
    if run_ids:
        runs = dict((await session.execute(
            select(Run.id, Run.status).where(Run.id.in_(run_ids))
        )).all())
        stopped = [r for r, st in runs.items() if st == "interrupted"]
        if stopped:
            waiting = set((await session.execute(
                select(Approval.run_id).where(
                    Approval.run_id.in_(stopped), Approval.status == "pending")
            )).scalars())
    now = utcnow()
    out: dict[str, tuple[str, str | None]] = {}
    for cid, turn in turns.items():
        run_id = run_of[cid] if run_of[cid] in runs else None
        out[cid] = (last_status(turn, runs.get(run_id or ""), run_id in waiting, now),
                    run_id)
    return out


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


async def _get(session: AsyncSession, conversation_id: str) -> Conversation:
    row = await session.get(Conversation, conversation_id)
    if not row:
        raise HTTPException(404, "这个对话不存在，可能已经被删了")
    return row


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    kind: str = "chat",
    workflow_id: str | None = None,
    include_archived: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    query = select(Conversation).where(Conversation.kind == kind)
    if workflow_id:
        query = query.where(Conversation.workflow_id == workflow_id)
    if not include_archived:
        query = query.where(Conversation.archived.is_(False))
    rows = list((await session.execute(
        query.order_by(Conversation.last_active_at.desc())
    )).scalars())
    if not rows:
        return []

    # 轮数、最后一轮、它的运行状态一次查完。每条会话各查一次的话，列表长了就是
    # 几十次往返。状态要带上：没打开过的会话也得标得出「在等审批」「失败了」
    ids = [r.id for r in rows]
    counts = dict((await session.execute(
        select(ConversationTurn.conversation_id, func.count(ConversationTurn.id))
        .where(ConversationTurn.conversation_id.in_(ids))
        .group_by(ConversationTurn.conversation_id)
    )).all())
    last = await _last_turns(session, ids)
    status = await _statuses(session, last)

    out: list[ConversationOut] = []
    for row in rows:
        item = ConversationOut.model_validate(row)
        item.turn_count = int(counts.get(row.id, 0))
        if row.id in last:
            item.last_question = last[row.id].question or ""
            item.last_status, item.last_run_id = status[row.id]
        out.append(item)
    return out


@router.post("", response_model=ConversationDetail, status_code=201)
async def create_conversation(
    payload: ConversationIn, session: AsyncSession = Depends(get_session)
) -> ConversationDetail:
    row = Conversation(
        kind=payload.kind,
        workflow_id=payload.workflow_id,
        title=payload.title or "新对话",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _detail(row, [])


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> ConversationDetail:
    row = await _get(session, conversation_id)
    turns = list((await session.execute(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation_id)
        .order_by(ConversationTurn.seq)
    )).scalars())
    out = _detail(row, turns)
    out.turn_count = len(turns)
    out.last_question = turns[-1].question if turns else ""
    if turns:
        out.last_status, out.last_run_id = (await _statuses(session, {row.id: turns[-1]}))[row.id]
    return out


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def patch_conversation(
    conversation_id: str,
    payload: ConversationPatch,
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    row = await _get(session, conversation_id)
    if payload.title is not None:
        row.title = payload.title.strip() or row.title
    if payload.archived is not None:
        row.archived = payload.archived
    await session.commit()
    await session.refresh(row)
    return ConversationOut.model_validate(row)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    row = await _get(session, conversation_id)
    # 显式删 turns 而不是只靠 ondelete=CASCADE：SQLite 的外键级联要 PRAGMA
    # 打开才生效（base.py 里是打开的），但这条路径不该依赖一个连接级的开关
    await session.execute(
        delete(ConversationTurn).where(ConversationTurn.conversation_id == conversation_id)
    )
    await session.delete(row)
    await session.commit()


# --------------------------------------------------------------------------
# 轮次
# --------------------------------------------------------------------------


@router.post("/{conversation_id}/turns", response_model=TurnOut, status_code=201)
async def create_turn(
    conversation_id: str,
    payload: TurnIn,
    session: AsyncSession = Depends(get_session),
) -> TurnOut:
    conversation = await _get(session, conversation_id)
    next_seq = int((await session.execute(
        select(func.coalesce(func.max(ConversationTurn.seq), -1))
        .where(ConversationTurn.conversation_id == conversation_id)
    )).scalar_one()) + 1

    turn = ConversationTurn(
        conversation_id=conversation_id, seq=next_seq,
        question=payload.question, status="running",
    )
    session.add(turn)

    conversation.last_active_at = utcnow()
    # 第一轮的问题就是这次对话的标题。用户改过名就不要再覆盖回去
    if next_seq == 0 and conversation.title in ("", "新对话"):
        conversation.title = title_from(payload.question)

    await session.commit()
    await session.refresh(turn)
    return _turn_out(turn)


@router.patch("/{conversation_id}/turns/{turn_id}", response_model=TurnOut)
async def patch_turn(
    conversation_id: str,
    turn_id: str,
    payload: TurnPatch,
    session: AsyncSession = Depends(get_session),
) -> TurnOut:
    turn = await session.get(ConversationTurn, turn_id)
    if not turn or turn.conversation_id != conversation_id:
        raise HTTPException(404, "这一轮对话不存在，可能已经被删了")

    for field in (
        "question", "answer", "explanation", "graph", "run_id", "status", "error", "review", "meta",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(turn, field, value)

    # 活跃时间只认人在这里开始做事：改问题、按下运行、重试（状态回到 running）。
    # 打开会话时前端的回源核对也走这里——补全截断的答案、记下运行已删、把断掉的
    # 建图写回 error、补交付错过的结果——这些不算，否则旧会话一打开就顶到列表最上面
    if payload.question is not None or payload.run_id is not None or payload.status == "running":
        conversation = await session.get(Conversation, conversation_id)
        if conversation:
            conversation.last_active_at = utcnow()
    await session.commit()
    await session.refresh(turn)
    return _turn_out(turn)


# --------------------------------------------------------------------------
# 给模型看的那一份
# --------------------------------------------------------------------------


def _answer_of(turn: ConversationTurn) -> str:
    """这一轮的结果。问数据页是 answer，画布那条路径上只有 explanation。"""
    text = (turn.answer or turn.explanation or "").strip()
    if turn.status == "error" and turn.error:
        return f"（这一轮失败了：{turn.error[:200]}）"
    if len(text) > HISTORY_ANSWER_CHARS:
        text = text[:HISTORY_ANSWER_CHARS] + "…（略）"
    # 复核判过不可信的答案，下一轮必须知道。只给正文的话，模型会拿一个已知
    # 有问题的结论当事实继续往下推，错误就这么一轮轮传下去了
    note = (turn.review or {}).get("note", "") if isinstance(turn.review, dict) else ""
    if note:
        text = f"{text}\n（复核提示：{note[:200]}）".strip()
    return text


async def recent_turns(
    session: AsyncSession, conversation_id: str, limit: int = HISTORY_TURNS
) -> list[ConversationTurn]:
    """最近 limit 轮，按时间正序。当前这一轮（还没有答案）不算。"""
    rows = list((await session.execute(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation_id)
        .where(ConversationTurn.status != "running")
        .order_by(ConversationTurn.seq.desc())
        .limit(limit)
    )).scalars())
    return list(reversed(rows))


async def history_text(
    session: AsyncSession, conversation_id: str | None, limit: int = HISTORY_TURNS
) -> str:
    """纯文本的问答历史，给运行时的图用（`{{ input.history }}`）。

    管的是指代：用户问"它们平均多少"，图里的 agent 得知道"它们"是上一轮
    那 12 家店。Copilot 侧的上下文解决不了这个——那只影响图长什么样，
    不影响图跑起来的时候模型看到什么。
    """
    if not conversation_id:
        return ""
    turns = await recent_turns(session, conversation_id, limit)
    if not turns:
        return ""
    lines = [f"问：{t.question}\n答：{_answer_of(t)}" for t in turns if t.question]
    return "\n\n".join(lines)
