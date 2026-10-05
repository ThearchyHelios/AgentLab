"""审批质询：审批人针对待审报告里的一句话提问，只读 agent 现查现答（规则和做法见 engine/inquiry.py）。

- POST /api/runs/{run_id}/inquiries：{report, unit, quote?, question, approval_id?} → {inquiry, recorded, note}
- GET  /api/runs/{run_id}/inquiries?report=&unit=：{items: [Inquiry]}，封存前后的都算，按先后
- GET  /api/runs/{run_id}/inquiries/artifacts/{artifact_id}：质询引用过的工件，查询快照的遮罩列照证据面板遮好

不受理：409 {detail, code}（inquiry_not_open / inquiry_busy / inquiry_disabled）；输入不合法 422 {detail, field}，
field 指明是哪一项（前后端约定的形状，不是 FastAPI 自己那种校验错误数组，所以请求体自己解析）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.coded import CodedHTTPException
from app.api.runs import actor_of
from app.engine import inquiry
from app.engine.inquiry import InquiryRefused

router = APIRouter(prefix="/api/runs", tags=["inquiry"])


def _refused(e: InquiryRefused) -> Any:
    """不受理 → HTTP 响应。422 照约定只回 {detail, field}；其余带机读码的回 {detail, code}。"""
    if e.status == 422:
        return JSONResponse(status_code=422, content={"detail": e.message, "field": e.field})
    if e.code:
        raise CodedHTTPException(e.status, e.message, e.code)
    raise HTTPException(e.status, e.message)


@router.post("/{run_id}/inquiries", response_model=None)
async def ask_inquiry(run_id: str, request: Request, x_actor: str | None = Header(default=None)) -> Any:
    """问一个问题。回答要等 agent 查完库（最长约为设置里的时长上限再加半分钟收尾），接口一直等到回答完才返回。"""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    try:
        return await inquiry.ask(run_id, inquiry.parse_ask(body), actor=actor_of(x_actor))
    except InquiryRefused as e:
        return _refused(e)


@router.get("/{run_id}/inquiries", response_model=None)
async def list_inquiries(run_id: str, report: str | None = None, unit: str | None = None) -> Any:
    try:
        return {"items": await inquiry.list_inquiries(run_id, report=report or None, unit=unit or None)}
    except InquiryRefused as e:
        return _refused(e)


@router.get("/{run_id}/inquiries/artifacts/{artifact_id}", response_model=None)
async def inquiry_artifact(run_id: str, artifact_id: str) -> Any:
    try:
        return await inquiry.inquiry_artifact(run_id, artifact_id)
    except InquiryRefused as e:
        return _refused(e)

