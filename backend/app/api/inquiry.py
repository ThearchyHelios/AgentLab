"""审批质询：审批人针对待审报告里的一句话提问，只读 agent 现查现答（规则和做法见 engine/inquiry.py）。

- POST /api/runs/{run_id}/inquiries：{report, unit, quote?, question, approval_id?} → {inquiry, recorded, note}
- GET  /api/runs/{run_id}/inquiries?report=&unit=：{items: [Inquiry]}，封存前后的都算，按先后
- GET  /api/runs/{run_id}/inquiries/artifacts/{artifact_id}：质询引用过的工件 {id, content, masked[, mask_note]}——
  查询快照（queries[].artifact）照回答时的办法遮好：遮罩列（含起了别名、套了函数的）整列遮，别的列里写着遮罩值的格子
  也遮，masked 是有格子被遮掉的列名；content.sql 就是事件里记下的那条（遮罩值已换掉），两处一字不差。回答文档
  （answer.doc_artifact）组装时就遮好了，原样给；回答里引用的报告指标（doc.catalog 里 m: 条目的 artifact，口径卡）
  也从这里取，界面一律按 doc.catalog[alias].artifact 取。没写进运行记录的回答（recorded 为假）在本进程里留一小时，
  这段时间里它引用的工件照样能取；之后、或者服务重启过，回 404。别的工件一律 404

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

