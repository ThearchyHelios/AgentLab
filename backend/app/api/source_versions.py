"""上传表格的版本页接口（期 3，WP-5；P3-SPEC 第 7 节）：版本列表、启用旧版本（回滚）、查看清单、移除一期、
作废接受。导入记录列表与清除原件仍在 datasources.py（期 1 的接口，期 3 补了字段和错误码）。

流程在 app.data.source_versions 里，这里只做三件事：手工读请求体（不触发 FastAPI 自带的 422：它的 body 是
{detail: [...]}、不带 code，前端没法分支）、取署名、把业务错误转成带机读码的 CodedHTTPException。

**写操作一律要 confirm: true 和界面看到的当前版本（expected_current_snapshot_id）**（7.7）：确认框是按那个版本
写的后果，别人在这期间提交、启用过，锁内比对不上就 409 base_changed，不能照旧切过去。移除、作废还要写理由。
署名取请求体的 signed_by，其次请求头 X-Actor，只原样记下（未认证）。

路径都是 /{source_id}/snapshots… 和 /{source_id}/imports/{import_id}/(manifest|remove|revoke-acceptance)，与
datasources.py 的 /{source_id}/imports（GET）、/{source_id}/imports/{import_id}/purge-raw 不重叠；在 main.py 里
登记在 table_imports 之后、datasources 之前。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.datasources import _out
from app.api.runs import actor_of
from app.data import source_versions, table_versions
from app.data.recipe_imports import ImportRefused
from app.db.base import get_session
from app.db.models import DataSource

router = APIRouter(prefix="/api/datasources", tags=["source-versions"])

_SIGNED_MAX = 100


def _refused(e: Exception) -> CodedHTTPException:
    if isinstance(e, table_versions.StoreUnavailable):
        return CodedHTTPException(503, str(e), "store_unavailable")
    return CodedHTTPException(e.status, e.message, e.code)  # type: ignore[attr-defined]


_REFUSALS = (ImportRefused, table_versions.StoreUnavailable)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CodedHTTPException(422, "请求体必须是一个 JSON 对象", "body_invalid")
    return data


def _signed(body: dict[str, Any], header: str | None) -> str | None:
    raw = body.get("signed_by")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:_SIGNED_MAX]
    return actor_of(header)


async def _source(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if row is None:
        raise CodedHTTPException(404, "数据源不存在，可能已被删除", "source_not_found")
    return row


def _confirmed(body: dict[str, Any]) -> None:
    if body.get("confirm") is not True:
        raise CodedHTTPException(422, "请在确认框中确认后再操作", "confirm_required")


def _expected_current(body: dict[str, Any]) -> str | None:
    """界面看到的当前版本 id：必须带这个键（值可以是 null：界面看到的就是「没有当前版本」）。"""
    if "expected_current_snapshot_id" not in body:
        raise CodedHTTPException(422, "请求里缺少界面看到的当前版本，请刷新版本列表后重试", "expected_current_required")
    value = body.get("expected_current_snapshot_id")
    if value is not None and not isinstance(value, str):
        raise CodedHTTPException(422, "当前版本的写法不对，请刷新版本列表后重试", "expected_current_required")
    return value


def _reason(body: dict[str, Any], *, required: bool) -> str | None:
    raw = body.get("reason")
    if raw is not None and not isinstance(raw, str):
        raise CodedHTTPException(422, "理由必须是一段文字", "reason_required")
    text = (raw or "").strip()
    if required and not text:
        raise CodedHTTPException(422, "请写明理由", "reason_required")
    if len(text) > source_versions.REASON_MAX:
        raise CodedHTTPException(422, f"理由不能超过 {source_versions.REASON_MAX} 字", "reason_required")
    return text or None


def _ack(body: dict[str, Any]) -> list[str] | None:
    raw = body.get("ack_mask_lost")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise CodedHTTPException(422, "确认丢失的遮罩列要写成列名的列表", "body_invalid")
    return list(raw)


async def _source_out(session: AsyncSession, source_id: str) -> dict[str, Any] | None:
    row = await session.get(DataSource, source_id, populate_existing=True)
    return (await _out(session, row)).model_dump() if row is not None else None


# --------------------------------------------------------------------------
# 列表、清单
# --------------------------------------------------------------------------


@router.get("/{source_id}/snapshots")
async def list_snapshots(source_id: str, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """最近 50 个版本，当前版本排第一（7.1）。手工源返回空列表。"""
    return await source_versions.list_snapshots(session, await _source(session, source_id))


@router.get("/{source_id}/snapshots/{snapshot_id}/manifest")
async def snapshot_manifest(source_id: str, snapshot_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    source = await _source(session, source_id)
    try:
        return await source_versions.snapshot_manifest_out(session, source, snapshot_id)
    except _REFUSALS as e:
        raise _refused(e) from e


@router.get("/{source_id}/imports/{import_id}/manifest")
async def import_manifest(source_id: str, import_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    source = await _source(session, source_id)
    try:
        return await source_versions.import_manifest(session, source, import_id)
    except _REFUSALS as e:
        raise _refused(e) from e


# --------------------------------------------------------------------------
# 写操作
# --------------------------------------------------------------------------


@router.post("/{source_id}/snapshots/{snapshot_id}/activate")
async def activate(source_id: str, snapshot_id: str, request: Request, x_actor: str | None = Header(default=None),
                   session: AsyncSession = Depends(get_session)) -> Any:
    """启用一个还保留着的旧版本（回滚，7.2）。理由可选。"""
    body = await _body(request)
    _confirmed(body)
    expected = _expected_current(body)
    reason = _reason(body, required=False)
    ack = _ack(body)
    source = await _source(session, source_id)
    try:
        out = await source_versions.activate(session, source, snapshot_id, expected_current=expected,
                                             ack_mask_lost=ack, reason=reason, signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    return {"source": await _source_out(session, source_id), **out}


@router.post("/{source_id}/imports/{import_id}/remove")
async def remove_period(source_id: str, import_id: str, request: Request, x_actor: str | None = Header(default=None),
                        session: AsyncSession = Depends(get_session)) -> Any:
    """从当前版本中移除这一期（累积模式，7.4）。配方和导入模式不变。"""
    body = await _body(request)
    _confirmed(body)
    expected = _expected_current(body)
    reason = _reason(body, required=True)
    source = await _source(session, source_id)
    try:
        out = await source_versions.remove_period(session, source, import_id, expected_current=expected,
                                                  reason=reason or "", signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    return {"source": await _source_out(session, source_id), **out}


@router.post("/{source_id}/imports/{import_id}/revoke-acceptance")
async def revoke_acceptance(source_id: str, import_id: str, request: Request,
                            x_actor: str | None = Header(default=None),
                            session: AsyncSession = Depends(get_session)) -> Any:
    """撤回这一期（作废接受，不可恢复，7.5）：累积模式移除这一期，替换模式回滚到 revoke_plan 的目标。"""
    body = await _body(request)
    _confirmed(body)
    expected = _expected_current(body)
    reason = _reason(body, required=True)
    target = body.get("expected_target_snapshot_id")
    if target is not None and not isinstance(target, str):
        raise CodedHTTPException(422, "回滚目标的写法不对，请刷新后重新确认", "body_invalid")
    ack = _ack(body)
    source = await _source(session, source_id)
    try:
        out = await source_versions.revoke_acceptance(
            session, source, import_id, expected_current=expected, expected_target=target, ack_mask_lost=ack,
            reason=reason or "", signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    return {"source": await _source_out(session, source_id), **out}
