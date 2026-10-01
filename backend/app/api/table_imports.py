"""按配方导入的接口（期 2，WP-5c；P2-SPEC 7.3）：暂存、回答问题、改配方、AI 起草、试运行、提交、放弃，
以及「上传新一期」「修改配方」「查看当前配方」。

流程和状态都在 app.data.recipe_imports 里，这里只做三件事：解析请求、取署名、把业务错误转成带机读码的
CodedHTTPException（body 是 {"detail": 中文, "code": 机读码}，前端按 code 分支）。

**请求模型里不放会触发 FastAPI 自带 422 的必填项**：它的 body 是 {detail: [...]}、不带 code，前端没法分支。
所以 JSON 请求体一律手工读成 dict（不是 JSON 对象才回 422 body_invalid），各字段由业务代码检查。

路由路径都以 /imports/ 开头，或是 /{source_id}/ 后面跟固定的 reupload、redraft、recipe，与 datasources.py
现有的 /{source_id}、/{source_id}/test|introspect|schema|imports 不重叠；在 main.py 里登记在 datasources
之前，免得 /imports/… 被通配的 {source_id} 先截走。

期 3（WP-5）加了 edits/preview、edits/apply、edits/undo、redraft-rules，PUT recipe 收可选的 origin；只读进程
（没拿到版本存储的守卫）里会写版本存储的入口一律回 503 store_unavailable（第 8 节遗留项 5），body 照样是
{detail, code}：前端把不是 JSON 的 503 当成网络断开。
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.datasources import _NAME_PATTERN, _out, read_upload
from app.api.runs import actor_of
from app.data import recipe_imports, table_versions
from app.data.recipe_imports import EditRequest, ImportRefused
from app.data.recipe_types import Acceptance, AiAvailability, PeriodInput, Recipe, Selection
from app.db.base import get_session
from app.db.models import DataSource, ImportStaging, TableRecipe

router = APIRouter(prefix="/api/datasources", tags=["table-imports"])
logger = logging.getLogger(__name__)

_NAME_INVALID = "名称必须以小写字母开头，只能包含字母、数字和下划线（名称会成为工具名的一部分）"
_SIGNED_MAX = 100


#: 业务上的拒绝：ImportRefused 带着状态和机读码；StoreUnavailable 是只读进程拒写（503）
_REFUSALS = (ImportRefused, table_versions.StoreUnavailable)


def _refused(e: Exception) -> CodedHTTPException:
    if isinstance(e, table_versions.StoreUnavailable):
        return CodedHTTPException(503, str(e), "store_unavailable")
    return CodedHTTPException(e.status, e.message, e.code)  # type: ignore[attr-defined]


def _signed(body: dict[str, Any] | None, header: str | None) -> str | None:
    """署名：请求体的 signed_by 优先，其次请求头 X-Actor。只原样记下（未认证）。"""
    raw = (body or {}).get("signed_by") if isinstance(body, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:_SIGNED_MAX]
    return actor_of(header)


#: JSON 请求体的字节上限和嵌套层数上限（SE-5）。最大的合法配方（200 个标签、几十个分段）也只有一两百 KB、
#: 十来层；更大更深的不是配方，照存会让静态校验跑几十秒、占着解析名额，深层嵌套还会让响应序列化失败
BODY_MAX_BYTES = 1_000_000
BODY_MAX_DEPTH = 32


def _json_depth_over(raw: bytes, limit: int) -> bool:
    depth = 0
    in_str = esc = False
    for ch in raw.decode("utf-8", errors="replace"):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return True
        elif ch in "]}":
            depth -= 1
    return False


async def _body(request: Request) -> dict[str, Any]:
    """JSON 请求体。空 body 当作 {}；超过字节上限回 413 body_too_large；不是 JSON 对象、嵌套过深回 422 body_invalid。"""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > BODY_MAX_BYTES:
        raise CodedHTTPException(413, f"请求体超过 {BODY_MAX_BYTES // 1_000_000} MB 的上限", "body_too_large")
    raw = await request.body()
    if len(raw) > BODY_MAX_BYTES:
        raise CodedHTTPException(413, f"请求体超过 {BODY_MAX_BYTES // 1_000_000} MB 的上限", "body_too_large")
    if not raw.strip():
        return {}
    if _json_depth_over(raw, BODY_MAX_DEPTH):
        raise CodedHTTPException(422, f"请求体的 JSON 嵌套超过 {BODY_MAX_DEPTH} 层", "body_invalid")
    try:
        data = await request.json()
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise CodedHTTPException(422, "请求体必须是一个 JSON 对象", "body_invalid")
    return data


async def _staging(session: AsyncSession, staging_id: str) -> ImportStaging:
    row = await session.get(ImportStaging, staging_id)
    if row is None:
        raise CodedHTTPException(404, "这次导入不存在，可能已过期并被清理。请重新开始导入", "staging_not_found")
    return row


async def _open_staging(session: AsyncSession, staging_id: str) -> ImportStaging:
    """写操作要的暂存区：先让过期的过期（不能指望只有回收时才过期），再要求它未结束。"""
    await table_versions.expire_stagings(session)
    row = await _staging(session, staging_id)
    if not table_versions.staging_is_open(row):
        raise CodedHTTPException(409, recipe_imports._CLOSED, "staging_closed")
    return row


async def _source(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if row is None:
        raise CodedHTTPException(404, "数据源不存在，可能已被删除", "source_not_found")
    return row


async def _out_of(session: AsyncSession, staging: ImportStaging) -> dict[str, Any]:
    """StagingOut。提供 AI 起草时才去查模型接入的可用性（不提供时不必多一次查询）。"""
    source = await session.get(DataSource, staging.source_id)
    pipe = recipe_imports.pipeline()
    ai: AiAvailability | None = None
    if table_versions.staging_is_open(staging) and recipe_imports.ai_offered(staging):
        try:
            ai = await pipe.ai_availability(session)
        except Exception as e:  # noqa: BLE001 - 可用性只是提示，查不到按不可用显示
            logger.warning("ai availability failed: %s", type(e).__name__)
            ai = AiAvailability(False, reason="暂时无法确认模型接入是否可用。请到「设置 → 模型接入」检查")
    return recipe_imports.staging_out(staging, source, ai=ai, units=tuple(pipe.units) or None)


def _unreadable(e: Exception) -> CodedHTTPException:
    return CodedHTTPException(400, str(e) or "无法读取这个文件，请确认是未加密的 Excel 文件", "file_unreadable")


# --------------------------------------------------------------------------
# 暂存、查看、放弃
# --------------------------------------------------------------------------


@router.post("/imports/stage", status_code=201)
async def stage(
    file: UploadFile | None = File(None),
    name: str = Form(""),
    description: str = Form(""),
    signed_by: str = Form(""),
    x_actor: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """选文件开始一次按配方导入：原件存档、扫描、网格预览、系统发现、规则起草。新名字 kind=first；
    同名的简单导入源 kind=switch。"""
    from app.data.tabular import UnsupportedTable

    if file is None:
        raise CodedHTTPException(422, "请选择要导入的文件", "body_invalid")
    if not re.match(_NAME_PATTERN, name or ""):
        raise CodedHTTPException(400, _NAME_INVALID, "name_invalid")
    raw = await read_upload(file)
    try:
        staging = await recipe_imports.stage_upload(
            session, raw=raw, filename=file.filename or name, name=name, description=description or "",
            signed_by=_signed({"signed_by": signed_by}, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    except UnsupportedTable as e:
        raise _unreadable(e) from e
    return JSONResponse(status_code=201, content=await _out_of(session, staging))


@router.get("/imports/{staging_id}")
async def get_staging(staging_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    await table_versions.expire_stagings(session)
    return await _out_of(session, await _staging(session, staging_id))


@router.delete("/imports/{staging_id}", status_code=204)
async def discard(staging_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    staging = await _open_staging(session, staging_id)
    try:
        await recipe_imports.discard_staging(session, staging)
    except _REFUSALS as e:
        raise _refused(e) from e
    return Response(status_code=204)


# --------------------------------------------------------------------------
# 回答问题、改配方
# --------------------------------------------------------------------------


@router.post("/imports/{staging_id}/answers")
async def answers(staging_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    body = await _body(request)
    given = body.get("answers", {})
    if not isinstance(given, dict):
        raise CodedHTTPException(422, "回答必须是「问题 → 选项」的对象", "body_invalid")
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.answer(session, staging, given)
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


@router.put("/imports/{staging_id}/recipe")
async def put_recipe(staging_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    body = await _body(request)
    recipe = body.get("recipe")
    if not isinstance(recipe, dict):
        raise CodedHTTPException(422, "配方必须是一个 JSON 对象", "body_invalid")
    origin = body.get("origin")
    if origin not in (None, "rules_redraft"):
        raise CodedHTTPException(422, "配方来源只能不填，或填 rules_redraft（采用按规则重新起草的结果）", "body_invalid")
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.put_recipe(session, staging, recipe, origin=origin or "manual")
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


# --------------------------------------------------------------------------
# 期 3：修复按钮、框选、撤销、按规则重新起草
# --------------------------------------------------------------------------

_EDIT_SHAPE = "请求里要么给修复（fix），要么给框选（selection），二选一"


def _edit_request(body: dict[str, Any]) -> EditRequest:
    """{"fix": {id, option, reason?}} 或 {"selection": {sheet, ref, as, options?}}，二选一（9.1）。形状不对回 422
    edit_invalid；框选的形状校验交给契约 Selection.from_json（只抛 ValueError），输出时再经 to_json 改回 as。"""
    fix, sel = body.get("fix"), body.get("selection")
    if (fix is None) == (sel is None):
        raise CodedHTTPException(422, _EDIT_SHAPE, "edit_invalid")
    if fix is not None:
        if (not isinstance(fix, dict) or not isinstance(fix.get("id"), str) or not fix["id"]
                or not isinstance(fix.get("option"), str) or not fix["option"]):
            raise CodedHTTPException(422, "修复请求要指明是哪条修复建议（id）和选的哪一项（option）", "edit_invalid")
        reason = fix.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise CodedHTTPException(422, "理由必须是一段文字", "edit_invalid")
        return EditRequest(kind="fix", fix_id=fix["id"], option=fix["option"], reason=reason)
    try:
        return EditRequest(kind="selection", selection=Selection.from_json(sel))
    except ValueError as e:
        raise CodedHTTPException(422, str(e) or "框选的请求格式不对", "edit_invalid") from e


@router.post("/imports/{staging_id}/edits/preview")
async def edits_preview(staging_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    """修复或框选的预览（不改工作配方）。seq 原样回传：界面只采用最新一次的回包。"""
    body = await _body(request)
    req = _edit_request(body)
    staging = await _open_staging(session, staging_id)
    try:
        return await recipe_imports.edit_preview(session, staging, req, seq=body.get("seq"))
    except _REFUSALS as e:
        raise _refused(e) from e


@router.post("/imports/{staging_id}/edits/apply")
async def edits_apply(staging_id: str, request: Request, x_actor: str | None = Header(default=None),
                      session: AsyncSession = Depends(get_session)) -> Any:
    """应用预览过的修复或框选：选项和理由与预览时逐字相同，expected_sha256 是预览给的 recipe_sha256_after。"""
    body = await _body(request)
    req = _edit_request(body)
    expected = body.get("expected_sha256")
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.edit_apply(
            session, staging, req, expected_sha256=expected if isinstance(expected, str) else None,
            signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


@router.post("/imports/{staging_id}/edits/undo")
async def edits_undo(staging_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    await _body(request)
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.edit_undo(session, staging)
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


@router.post("/imports/{staging_id}/redraft-rules")
async def redraft_rules(staging_id: str, request: Request, session: AsyncSession = Depends(get_session)) -> Any:
    """按规则重新起草（不调用模型、不改工作配方），名字对齐到现行配方；「采用」走 PUT recipe（origin=rules_redraft）。"""
    from app.data.tabular import UnsupportedTable

    await _body(request)
    staging = await _open_staging(session, staging_id)
    try:
        return await recipe_imports.redraft_rules(session, staging)
    except _REFUSALS as e:
        raise _refused(e) from e
    except UnsupportedTable as e:
        raise _unreadable(e) from e


# --------------------------------------------------------------------------
# AI 起草
# --------------------------------------------------------------------------


@router.get("/imports/{staging_id}/draft-ai/preview")
async def draft_ai_preview(staging_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    staging = await _open_staging(session, staging_id)
    try:
        return await recipe_imports.ai_preview(session, staging)
    except _REFUSALS as e:
        raise _refused(e) from e


@router.post("/imports/{staging_id}/draft-ai")
async def draft_ai(staging_id: str, request: Request, x_actor: str | None = Header(default=None),
                   session: AsyncSession = Depends(get_session)) -> Any:
    body = await _body(request)
    sha = body.get("preview_sha256")
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.run_ai_draft(
            session, staging, consent=body.get("consent") is True,
            preview_sha256=sha if isinstance(sha, str) else None, signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


# --------------------------------------------------------------------------
# 试运行、提交
# --------------------------------------------------------------------------


def _context_inputs(raw: Any, signed_by: str | None) -> dict[str, PeriodInput] | None:
    """{"统计期": {"start": "2026-09-01", "end": "2026-09-30"}} → PeriodInput。不合法回 422 context_invalid。"""
    if raw is None or raw == {}:
        return None
    bad = CodedHTTPException(422, "统计期的录入不对：请填写起止日期（YYYY-MM-DD），起点不能晚于终点", "context_invalid")
    if not isinstance(raw, dict):
        raise bad
    out: dict[str, PeriodInput] = {}
    for key, value in raw.items():
        if key != recipe_imports.PERIOD_KEY or not isinstance(value, dict):
            raise bad
        try:
            start = _dt.date.fromisoformat(str(value.get("start") or ""))
            end = _dt.date.fromisoformat(str(value.get("end") or ""))
        except ValueError:
            raise bad from None
        if start > end:
            raise bad
        who = value.get("signed_by")
        out[key] = PeriodInput(start=start, end=end,
                               signed_by=(str(who).strip()[:_SIGNED_MAX] if isinstance(who, str) and who.strip()
                                          else signed_by))
    return out


@router.post("/imports/{staging_id}/trial")
async def trial(staging_id: str, request: Request, x_actor: str | None = Header(default=None),
                session: AsyncSession = Depends(get_session)) -> Any:
    body = await _body(request)
    who = _signed(body, x_actor)
    inputs = _context_inputs(body.get("context_inputs"), who)
    staging = await _open_staging(session, staging_id)
    try:
        staging = await recipe_imports.run_trial(session, staging, context_inputs=inputs, signed_by=who)
    except _REFUSALS as e:
        raise _refused(e) from e
    return await _out_of(session, staging)


def _acceptances(raw: Any, who: str | None) -> list[Acceptance]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CodedHTTPException(422, "接受理由必须是「核对 → 理由」的列表", "body_invalid")
    out = []
    for a in raw:
        if not isinstance(a, dict) or not isinstance(a.get("check_id"), str):
            raise CodedHTTPException(422, "接受理由必须是「核对 → 理由」的列表", "body_invalid")
        reason = a.get("reason")
        out.append(Acceptance(check_id=a["check_id"], reason=reason if isinstance(reason, str) else "",
                              signed_by=who))
    return out


@router.post("/imports/{staging_id}/commit", status_code=201)
async def commit(staging_id: str, request: Request, x_actor: str | None = Header(default=None),
                 session: AsyncSession = Depends(get_session)) -> Any:
    body = await _body(request)
    who = _signed(body, x_actor)
    confirmations = body.get("confirmations", [])
    if not isinstance(confirmations, list) or not all(isinstance(x, str) for x in confirmations):
        raise CodedHTTPException(422, "确认项必须是 id 的列表", "body_invalid")
    trial_id = body.get("trial_id")
    staging = await _open_staging(session, staging_id)
    try:
        result = await recipe_imports.commit_staging(
            session, staging, trial_id=trial_id if isinstance(trial_id, str) else "",
            confirmations=confirmations, acceptances=_acceptances(body.get("acceptances"), who), signed_by=who)
    except _REFUSALS as e:
        raise _refused(e) from e
    source = await session.get(DataSource, result.source_id, populate_existing=True)
    return JSONResponse(status_code=201, content={
        "source": (await _out(session, source)).model_dump() if source is not None else None,
        "import_id": result.import_id, "snapshot_id": result.snapshot_id, "build_id": result.build_id,
        "recipe_id": result.recipe_id, "build_reused": result.build_reused, "unchanged": result.unchanged,
        # 期 3：同版本数据文件被改过、已用本次的文件恢复；启用后当前版本含几期；直接启用了内容相同的已有版本
        "build_restored": result.build_restored, "parts": result.parts, "snapshot_reused": result.snapshot_reused,
    })


# --------------------------------------------------------------------------
# 上传新一期、修改配方、当前配方
# --------------------------------------------------------------------------


@router.post("/{source_id}/reupload", status_code=201)
async def reupload(
    source_id: str,
    file: UploadFile | None = File(None),
    signed_by: str = Form(""),
    x_actor: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """上传新一期：按当前配方自动试运行（不调模型），回来时已带回执和差异卡。"""
    from app.data.tabular import UnsupportedTable

    source = await _source(session, source_id)
    if not source.current_recipe_id:
        raise CodedHTTPException(409, recipe_imports._NOT_RECIPE, "not_recipe_source")
    if file is None:
        raise CodedHTTPException(422, "请选择要导入的文件", "body_invalid")
    raw = await read_upload(file)
    try:
        staging = await recipe_imports.reupload(session, source, raw=raw, filename=file.filename or source.name,
                                                signed_by=_signed({"signed_by": signed_by}, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    except UnsupportedTable as e:
        raise _unreadable(e) from e
    return JSONResponse(status_code=201, content=await _out_of(session, staging))


@router.post("/{source_id}/redraft", status_code=201)
async def redraft(source_id: str, request: Request, x_actor: str | None = Header(default=None),
                  session: AsyncSession = Depends(get_session)) -> Any:
    from app.data.tabular import UnsupportedTable

    body = await _body(request)
    source = await _source(session, source_id)
    try:
        staging = await recipe_imports.redraft(session, source, signed_by=_signed(body, x_actor))
    except _REFUSALS as e:
        raise _refused(e) from e
    except UnsupportedTable as e:
        raise _unreadable(e) from e
    return JSONResponse(status_code=201, content=await _out_of(session, staging))


@router.get("/{source_id}/recipe")
async def get_recipe(source_id: str, session: AsyncSession = Depends(get_session)) -> Any:
    source = await _source(session, source_id)
    row = await session.get(TableRecipe, source.current_recipe_id) if source.current_recipe_id else None
    if row is None:
        raise CodedHTTPException(409, recipe_imports._NOT_RECIPE, "not_recipe_source")
    try:
        full = Recipe.model_validate(row.recipe).model_dump(mode="json")
    except Exception:  # noqa: BLE001 - 存下的配方都是校验过的；万一读不出，原样给出
        full = row.recipe
    return {
        "recipe_id": row.id, "seq": row.seq, "origin": row.origin, "recipe": full,
        "recipe_sha256": row.recipe_sha256, "confirmations": list(row.confirmations or []),
        "signed_by": row.signed_by, "activated_at": row.activated_at.isoformat() if row.activated_at else None,
    }
