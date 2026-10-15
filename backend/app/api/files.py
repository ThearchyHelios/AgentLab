"""文件的上传、下载、删除（docs/design/normal-user-apps.md §3.8）。存放、绑定、读权限都在 app/files/store.py。

- POST /api/files：multipart，file + context（JSON：{app_id, entry} | {task_id} | {blueprint_id}，可带 field 说明是哪个
  字段），交回引用 {file_id, name, size, sha256, type}。大小上限同上传表格（max_upload_mb），类型按白名单、按内容核对；
  能用那个入口、能处理那条待办、能改那个蓝图的人才传得上来；
- GET /api/files/{id}：下载。读得到绑定的那次运行、看得到绑定的那一行记录的人，以及上传的人自己；
- DELETE /api/files/{id}：只能删自己传的、还没提交的。
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.deps import Principal, actor_of, current_principal
from app.db.base import get_session
from app.db.models_blueprints import StoredFile
from app.files import store

router = APIRouter(prefix="/api/files", tags=["files"])

#: 后台预抽取文字的任务：留着引用，不让它们半路被回收
_WARMING: set[asyncio.Task[Any]] = set()


def _principal(x_actor: str | None) -> Principal:
    """这次请求的身份。关登录时 username 换成自报的署名（X-Actor）：上传人、只能删自己的都按它认（同 api/apps.py）。"""
    principal = current_principal()
    if principal.mode == "off" and principal.username is None:
        return dataclasses.replace(principal, username=actor_of(x_actor))
    return principal


def _refused(e: store.FileRefused) -> CodedHTTPException:
    return CodedHTTPException(e.status, e.message, e.code, field=e.field)


@router.post("", status_code=201)
async def upload(file: UploadFile | None = File(None), context: str = Form(""),
                 session: AsyncSession = Depends(get_session),
                 x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """multipart：file + context（{app_id, entry} | {task_id} | {blueprint_id}），交回引用
    {file_id, name, size, sha256, type}。"""
    from app.api.datasources import read_upload
    from app.files.text import warm

    principal = _principal(x_actor)
    try:
        ctx = store.parse_context(context)
        setting = await store.check_context(session, principal, ctx)
    except store.FileRefused as e:
        raise _refused(e) from e
    if file is None:
        raise CodedHTTPException(422, "请选择要上传的文件", "files.missing")
    raw = await read_upload(file)
    name = file.filename or "附件"
    try:
        mime = store.type_of(name, raw)
        if problem := store.accept_problem(setting, name=name, mime=mime, size=len(raw)):
            raise store.FileRefused(415, "files.type_not_accepted", problem, field=ctx.get("field"))
        row = await store.save(session, raw, name, uploaded_by=principal.username, context=ctx)
    except store.FileRefused as e:
        raise _refused(e) from e
    # 先在后台把文字抽一遍：运行里 file_text 多半直接读缓存
    task = asyncio.create_task(warm(row.sha256, row.name, row.mime))
    _WARMING.add(task)
    task.add_done_callback(_WARMING.discard)
    return store.ref_of(row)


@router.get("/{file_id}")
async def download(file_id: str, session: AsyncSession = Depends(get_session),
                   x_actor: str | None = Header(default=None)) -> Response:
    principal = _principal(x_actor)
    row = await session.get(StoredFile, file_id)
    if row is None:
        raise CodedHTTPException(404, "文件不存在，可能没有提交、已被清理", "files.not_found")
    if not await store.readable(session, principal, row):
        if row.bound_run_id is None and row.bound_record is None:
            # 没提交的文件只有上传的人看得到：别人不该知道它在不在
            raise CodedHTTPException(404, "文件不存在，可能没有提交、已被清理", "files.not_found")
        raise CodedHTTPException(403, "没有下载这个文件的权限：只有能看到它所属的那次运行或那一行记录的人才能下载",
                                 "files.forbidden")
    data = await asyncio.to_thread(store.read_blob, row.sha256)
    if data is None:
        raise CodedHTTPException(409, "文件内容不在了，或者和登记的不一致（可能被改动过），不能下载", "files.content_lost")
    return Response(content=data, media_type=row.mime, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.delete("/{file_id}", status_code=204)
async def delete(file_id: str, session: AsyncSession = Depends(get_session),
                 x_actor: str | None = Header(default=None)) -> None:
    try:
        await store.remove(session, _principal(x_actor), file_id)
    except store.FileRefused as e:
        raise _refused(e) from e
