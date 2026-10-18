"""问数据的一轮：发起、看、停（docs/design/orchestration-round/00-总方案.md §1；做法在 app/chat/job.py）。

- POST /api/chat/turns：发起一轮，202 {turn_id, turn}。新问带 conversation_id、question（可选 datasource_ids 限定数据源）；对已有
  的一轮重试、重新运行、放宽步数、跑一下、继续运行、接着收尾带 retry_of 和 mode（MODES），重试时 datasource_ids 给了就换范围
  （空数组是不限），不给沿用这一轮的。同一段对话里有一轮正在跑（停在审批上的不算）时 409 chat_turn_busy；继续运行被拒照
  POST /api/runs/{id}/continue 的说法（409，带那边的机器码）。
- GET /api/chat/turns/{id}：这一轮的任务现在怎样（status、stage、run_id、last_seq、attempt_seq……），连同那一行（turn）。老轮次
  没有任务时 job 为 null。
- GET /api/chat/turns/{id}/stream?after=<seq>：SSE。先回放 seq > after 的，再推实时；隔一拍没有新事件发一行注释心跳；这一轮不在
  进行中、也没有新的了，补一条 stream.end 再关（chat/log.py）。断了按收到的最后一个 seq 重连，补齐、不重复。
- POST /api/chat/turns/{id}/cancel：停止（取消生成、取消运行，照现有的取消规则）。已经结束的 409 chat_turn_finished。

权限照问数据的规矩：路由挂 chat_scope（使用者要管理员开了问数据），每个接口再按这一轮所属的对话核对归属（对话按人隔离，
conversations.turn_access / ensure_conversation_access）；访问密钥调不了（不在 auth/keys.KEY_ROUTES 里）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import actor_of
from app.db.base import get_session

router = APIRouter(prefix="/api/chat/turns", tags=["chat"])


class TurnStartIn(BaseModel):
    conversation_id: str = Field(min_length=1)
    question: str = ""
    #: 这一问只查这几个数据源（id 或名字）。不给：新问不限，重试沿用这一轮的；空数组：不限
    datasource_ids: list[str] | None = None
    #: 对已有的哪一轮（重试、重新运行、继续运行……）
    retry_of: str | None = None
    #: ask | retry | rerun | max_steps | run | continue | resume（app/chat/job.MODES）。不给：新问是 ask，带 retry_of 是 retry
    mode: str | None = None
    #: 放宽到几步（mode=max_steps）
    max_steps: int | None = Field(default=None, ge=1, le=10_000)


def _refused(e: Any) -> HTTPException:
    if e.code:
        return CodedHTTPException(e.status_code, e.message, e.code)
    return HTTPException(e.status_code, e.message)


@router.post("", status_code=202)
async def start_turn(payload: TurnStartIn, session: AsyncSession = Depends(get_session),
                     x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.api.conversations import _get, turn_access, turn_out
    from app.chat import job as chat_job

    retry_of = None
    if payload.retry_of:
        conversation, retry_of = await turn_access(session, payload.retry_of)
        if conversation.id != payload.conversation_id:
            raise HTTPException(404, "该轮对话不属于这段对话")
    else:
        conversation = await _get(session, payload.conversation_id)
    try:
        turn_id = await chat_job.start(session, conversation=conversation, question=payload.question,
                                       datasource_ids=payload.datasource_ids, actor=actor_of(x_actor), mode=payload.mode,
                                       retry_of=retry_of, max_steps=payload.max_steps)
    except chat_job.TurnRefused as e:
        raise _refused(e) from e
    from app.db.base import SessionLocal
    from app.db.models import ConversationTurn

    async with SessionLocal() as fresh:
        row = await fresh.get(ConversationTurn, turn_id)
        turn = (await turn_out(fresh, row)).model_dump(mode="json") if row is not None else None
    return {"turn_id": turn_id, "turn": turn}


@router.get("/{turn_id}")
async def get_turn(turn_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from app.api.conversations import turn_access, turn_out
    from app.chat import job as chat_job

    _, row = await turn_access(session, turn_id)
    return {"id": row.id, "conversation_id": row.conversation_id, "job": await chat_job.state(session, row.id),
            "turn": (await turn_out(session, row)).model_dump(mode="json")}


@router.get("/{turn_id}/stream")
async def stream_turn(turn_id: str, after: int = Query(default=0, ge=0),
                      session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    from app.api.conversations import turn_access
    from app.chat import job as chat_job
    from app.chat import log as turn_log

    await turn_access(session, turn_id)
    await session.close()   # 流可能开很久：核对完就把这次请求的会话还回去

    async def active() -> str | None:
        return await chat_job.stream_status(turn_id)

    return StreamingResponse(turn_log.stream(turn_id, after, active=active), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{turn_id}/cancel")
async def cancel_turn(turn_id: str, session: AsyncSession = Depends(get_session),
                      x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    from app.api.conversations import turn_access
    from app.chat import job as chat_job

    await turn_access(session, turn_id)
    await session.close()
    if not await chat_job.cancel(turn_id, actor_of(x_actor)):
        raise CodedHTTPException(409, "这一轮已经结束，无需停止", chat_job.TURN_FINISHED)
    return {"ok": True}
