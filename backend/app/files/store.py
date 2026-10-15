"""文件的存放、上传上下文、绑定、读权限、删除和清理（docs/design/normal-user-apps.md N9、§3.8）。

**存放。** 按 sha256 内容寻址：data/files/<前两位>/<sha>，只读（0444），和上传表格的原件存档同一种写法
（data/raw_store.put_addressed）。元数据一行一个上传（files 表）：同样的内容传两次是两行、一个文件。

**两步。** 先 POST /api/files 拿回引用 {file_id, name, size, sha256, type}，再把引用当表单值提交。上传要带上下文：
应用的入口（{app_id, entry}）、待办（{task_id}）或蓝图（{blueprint_id}，试运行、个人应用），只有能用那个入口、能处理
那条待办、能改那个蓝图的人传得上来。

**绑定。** 提交入口（apps/entries.py）、回复待办（process/tasks.py）时核对：引用是提交人自己传的、在这个上下文里传的、
还没被用过，然后绑到这次运行（一次性）。表单里的引用换成登记的那一份（名字、大小、哈希以登记为准），连同 sha256
进运行输入、随封存固定。入口要先占住（claim）再发起运行：发起失败放回去，成功了改成运行 id。

**读。** 绑到运行的，读得到那次运行的人能下载（auth/access.can_read_run）；写进记录表那一行的，看得到那一行的人能下载
（records/access.py 的行级可见）；没绑定的只有上传的人自己。文件不做遮罩。

**清理。** 24 小时前上传、还没绑定的删掉（调度循环每小时一次，cleanup_tick）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.spec import FILE_REF_KEYS
from app.core.config import settings
from app.db.base import SessionLocal, utcnow
from app.db.models_blueprints import StoredFile
from app.files import ALLOWED_MIME_PREFIXES, UNBOUND_TTL_S

logger = logging.getLogger(__name__)

#: 入口提交时先占住文件（发起运行之前），bound_run_id 写成这个前缀加一个随机串；发起成功换成运行 id
CLAIM_PREFIX = "claim:"
#: 上下文里认的几项
CONTEXT_KEYS = ("app_id", "entry", "task_id", "blueprint_id", "field")
#: 扩展名 → 类型（白名单，files.ALLOWED_MIME_PREFIXES）
EXT_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
#: 类型 → 文件开头应有的字节。对不上就拒：扩展名谁都能改，内容得是它说的那种文件
_MAGIC = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/msword": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "application/vnd.ms-excel": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}
_TEXT_TYPES = ("text/csv", "text/plain")
_SHA = re.compile(r"^[0-9a-f]{64}$")
#: 文件名最长存几个字
NAME_MAX = 300
#: 文件控件（x-widget: file）的值要是引用这个形状；校验不过时表单错误的 code（apps/spec.FORM_ERROR_CODES 的 file）
FILE_ERROR_CODE = "file"


class FileRefused(Exception):
    """上传、绑定、下载、删除不成：status 是 HTTP 状态，code 是问题编号（files.*，白话在 app/plain.py），field 是表单
    里哪个字段（绑定时有）。message 直接给人看。"""

    def __init__(self, status: int, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


# --------------------------------------------------------------------------
# 存放
# --------------------------------------------------------------------------


def files_root() -> Path:
    """文件的根目录。按调用时的 settings 现算：测试会换数据目录。"""
    return settings.data_dir / "files"


def blob_path(sha: str) -> Path:
    if not _SHA.match(sha or ""):
        raise ValueError("文件哈希的格式不对")
    return files_root() / sha[:2] / sha


def read_blob(sha: str) -> bytes | None:
    """取回文件内容并核对哈希：文件不在、内容和哈希对不上都返回 None（不交出被改过的内容）。"""
    try:
        data = blob_path(sha).read_bytes()
    except (OSError, ValueError):
        return None
    if hashlib.sha256(data).hexdigest() != sha:
        logger.warning("文件 %s 的内容和哈希对不上，不交出", sha)
        return None
    return data


def type_of(filename: str, raw: bytes) -> str:
    """按扩展名定类型，再核对文件开头的字节。不在白名单、对不上、空文件都抛 FileRefused。"""
    ext = Path(filename or "").suffix.lower()
    mime = EXT_MIME.get(ext)
    if mime is None or not mime.startswith(ALLOWED_MIME_PREFIXES):
        allowed = "、".join(sorted({e.lstrip(".").upper() for e in EXT_MIME}))
        raise FileRefused(415, "files.type_not_allowed", f"只能上传这些类型的文件：{allowed}")
    if not raw:
        raise FileRefused(422, "files.empty", "文件是空的")
    if mime in _TEXT_TYPES:
        head = raw[:65536]
        if b"\x00" in head or not _decodes(head):
            raise FileRefused(415, "files.content_mismatch", f"「{filename}」不是文字文件，和扩展名对不上")
        return mime
    if not raw.startswith(_MAGIC.get(mime, (b"",))):
        raise FileRefused(415, "files.content_mismatch", f"「{filename}」的内容和扩展名对不上，请确认文件没有改过扩展名")
    return mime


def _decodes(head: bytes) -> bool:
    for encoding in ("utf-8", "gb18030"):
        try:
            head.decode(encoding)
            return True
        except UnicodeDecodeError:
            # 截在多字节字符中间的那几个字节不算
            try:
                head[:-3].decode(encoding)
                return True
            except UnicodeDecodeError:
                continue
    return False


def ref_of(row: StoredFile) -> dict[str, Any]:
    """登记的那一份引用：表单值、运行输入里放的就是它。"""
    return {"file_id": row.id, "name": row.name, "size": row.size, "sha256": row.sha256, "type": row.mime}


def as_ref(value: Any) -> dict[str, Any] | None:
    """表单值 → 引用（字典或 JSON 文字都认）；形状不对为 None。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or not set(value) <= set(FILE_REF_KEYS):
        return None
    if not isinstance(value.get("file_id"), str) or not value["file_id"]:
        return None
    return value


async def save(session: AsyncSession, raw: bytes, filename: str, *, uploaded_by: str | None,
               context: dict[str, Any]) -> StoredFile:
    """存内容、登记一行。类型按扩展名和内容核对（type_of）。"""
    from app.data import raw_store

    name = (Path(filename or "").name or "附件").strip()[:NAME_MAX]
    mime = type_of(name, raw)
    sha, _ = await asyncio.to_thread(raw_store.put_addressed, raw, files_root())
    row = StoredFile(sha256=sha, name=name, size=len(raw), mime=mime, uploaded_by=uploaded_by,
                     context={k: v for k, v in context.items() if k in CONTEXT_KEYS})
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------
# 上传的上下文：谁能往哪里传
# --------------------------------------------------------------------------


def parse_context(raw: Any) -> dict[str, str]:
    """POST /api/files 的 context（JSON 文字或对象）→ 只留认得的几项。三种之一都没有的抛 FileRefused。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        raise FileRefused(422, "files.context_invalid", "上传需要说明是给哪个入口、哪条待办或哪个蓝图用的")
    ctx = {k: str(v) for k, v in raw.items() if k in CONTEXT_KEYS and isinstance(v, (str, int)) and str(v).strip()}
    if not (("app_id" in ctx and "entry" in ctx) or "task_id" in ctx or "blueprint_id" in ctx):
        raise FileRefused(422, "files.context_invalid", "上传需要说明是给哪个入口、哪条待办或哪个蓝图用的")
    return ctx


async def check_context(session: AsyncSession, principal: Any, ctx: dict[str, str]) -> dict[str, Any] | None:
    """这个人能不能往这个上下文传文件：能用那个入口、能处理那条待办、能改那个蓝图。交回那个字段的文件控件设置
    （x-file：accept、max_mb；没说是哪个字段、字段没有设置时为 None），上传时按它再核一遍。"""
    if "task_id" in ctx:
        return await _task_context(session, principal, ctx)
    if "app_id" in ctx:
        return await _entry_context(session, principal, ctx)
    return await _blueprint_context(session, principal, ctx)


_NO_ACCESS = "没有往这里上传文件的权限"


async def _entry_context(session: AsyncSession, principal: Any, ctx: dict[str, str]) -> dict[str, Any] | None:
    from app.apps.access import app_role, at_least
    from app.apps.tree import entry_of, resolve_form
    from app.db.models_apps import App, AppVersion

    app = await session.get(App, ctx["app_id"])
    role = await app_role(session, principal, app) if app is not None else None
    if app is None or app.status == "archived" or role is None:
        raise FileRefused(404, "files.context_invalid", "应用不存在或已归档")
    if not at_least(role, "use"):
        raise FileRefused(403, "files.forbidden", _NO_ACCESS)
    spec = app.draft or {}
    if app.published_version:
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none()
        spec = (version.spec if version is not None else None) or spec
    entry = entry_of(spec, ctx["entry"])
    if entry is None:
        raise FileRefused(404, "files.context_invalid", f"应用里没有入口「{ctx['entry']}」")
    return _field_setting(resolve_form(spec, entry.get("form")), ctx.get("field"))


async def _task_context(session: AsyncSession, principal: Any, ctx: dict[str, str]) -> dict[str, Any] | None:
    from app.db.models_process import Task, TaskAssignment
    from app.process.tasks import handles

    task = await session.get(Task, ctx["task_id"])
    if task is None or task.status != "pending":
        raise FileRefused(404, "files.context_invalid", "待办不存在或已经处理完了")
    if principal.mode != "off":
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id))).scalars())
        if not handles(rows, principal):
            raise FileRefused(403, "files.forbidden", _NO_ACCESS)
    return _field_setting(task.form, ctx.get("field"))


async def _blueprint_context(session: AsyncSession, principal: Any, ctx: dict[str, str]) -> dict[str, Any] | None:
    from app.db.models_blueprints import Blueprint

    from app.blueprints.store import BLUEPRINT_GONE, can_access

    bp = await session.get(Blueprint, ctx["blueprint_id"])
    # 同读写蓝图（blueprints/store.can_access）：拥有者；共享蓝图另有管理员、它发布出来的应用的管理者。看不到的和不存在的
    # 一个说法（同 store.ensure_access「看不到的一律 404」）：回 403 就等于告诉人这张蓝图在
    if bp is None or (principal.mode != "off" and not (
            (bp.scope != "shared" or principal.at_least("builder")) and await can_access(session, principal, bp))):
        raise FileRefused(404, "files.context_invalid", BLUEPRINT_GONE)
    return None


def _field_setting(form: Any, name: str | None) -> dict[str, Any] | None:
    props = form.get("properties") if isinstance(form, dict) else None
    prop = props.get(name) if isinstance(props, dict) and name else None
    if not isinstance(prop, dict):
        return None
    setting = prop.get("x-file")
    return setting if isinstance(setting, dict) else {}


def accept_problem(setting: dict[str, Any] | None, *, name: str, mime: str, size: int) -> str | None:
    """文件控件设置（x-file：accept 类型或扩展名列表、max_mb）核一个文件：不合返回原因。"""
    if not setting:
        return None
    accept = [str(a).lower() for a in setting.get("accept") or [] if isinstance(a, str)]
    if accept:
        ext = Path(name).suffix.lower()
        if not any(a == mime or a == ext or (a.endswith("/*") and mime.startswith(a[:-1])) for a in accept):
            return f"这里只收这些类型的文件：{'、'.join(accept)}"
    max_mb = setting.get("max_mb")
    if isinstance(max_mb, (int, float)) and not isinstance(max_mb, bool) and size > max_mb * 1024 * 1024:
        return f"这里的文件不能超过 {max_mb:g} MB"
    return None


# --------------------------------------------------------------------------
# 绑定
# --------------------------------------------------------------------------

#: 还没绑到记录上：JSON 列显式写过 None 的存的是 JSON 的 null，不是 SQL 的 NULL，两种都算
_NO_RECORD = text("(files.bound_record IS NULL OR files.bound_record = 'null')")


def file_fields(form: Any) -> list[str]:
    """表单里的文件字段（x-widget: file）。"""
    props = form.get("properties") if isinstance(form, dict) else None
    if not isinstance(props, dict):
        return []
    return [name for name, prop in props.items() if isinstance(prop, dict) and prop.get("x-widget") == "file"]


def _context_matches(row: StoredFile, expected: dict[str, str]) -> bool:
    got = row.context or {}
    return all(str(got.get(k) or "") == v for k, v in expected.items())


def _field_label(form: Any, name: str) -> str:
    prop = ((form or {}).get("properties") or {}).get(name) or {}
    return str(prop.get("title") or name)


async def _check(session: AsyncSession, form: Any, values: dict[str, Any], *, principal: Any,
                 context: dict[str, str]) -> list[tuple[str, StoredFile]]:
    """表单里每个填了的文件字段：引用要对得上一行登记、是提交人自己传的、在这个上下文里传的、还没被用过。
    交回 [(字段, 那一行)]；有一个不对就抛 FileRefused（field 是那个字段）。"""
    out: list[tuple[str, StoredFile]] = []
    seen: set[str] = set()
    for name in file_fields(form):
        value = values.get(name)
        if value in (None, ""):
            continue
        label = _field_label(form, name)
        ref = as_ref(value)
        if ref is None:
            raise FileRefused(422, "files.ref_invalid", f"「{label}」需要先上传文件", field=name)
        row = await session.get(StoredFile, ref["file_id"])
        if row is None:
            raise FileRefused(422, "files.not_found", f"「{label}」的文件不在了（超过 24 小时没提交会被清理），请重新上传",
                              field=name)
        if row.uploaded_by != getattr(principal, "username", None):
            raise FileRefused(422, "files.not_yours", f"「{label}」的文件不是你上传的，请重新上传", field=name)
        if ref.get("sha256") not in (None, row.sha256) or row.id in seen:
            raise FileRefused(422, "files.ref_invalid", f"「{label}」的文件引用对不上，请重新上传", field=name)
        if row.bound_run_id is not None or row.bound_record is not None:
            raise FileRefused(409, "files.already_bound", f"「{label}」的文件已经提交过一次，请重新上传", field=name)
        if not _context_matches(row, context):
            raise FileRefused(422, "files.context_mismatch", f"「{label}」的文件是在别处上传的，请在这里重新上传",
                              field=name)
        seen.add(row.id)
        out.append((name, row))
    return out


@dataclass
class Claim:
    """入口提交时占住的文件：发起运行成功后 finish（绑到运行），失败 release（放回去）。"""

    token: str
    ids: list[str] = field(default_factory=list)


async def claim(form: Any, values: dict[str, Any], *, principal: Any, context: dict[str, str]
                ) -> tuple[dict[str, Any], Claim]:
    """核对表单里的文件引用并占住它们（自己的会话、立刻提交，两次并发提交只有一次占得到）。交回换成登记引用的表单值
    和占位。没有文件字段时什么都不做。"""
    from app.db.base import new_id

    token = Claim(token=f"{CLAIM_PREFIX}{new_id()[:20]}")
    if not file_fields(form):
        return values, token
    async with SessionLocal() as session:
        checked = await _check(session, form, values, principal=principal, context=context)
        out = dict(values)
        for name, row in checked:
            done = await session.execute(update(StoredFile).where(
                StoredFile.id == row.id, StoredFile.bound_run_id.is_(None), _NO_RECORD)
                .values(bound_run_id=token.token))
            if done.rowcount != 1:
                await session.rollback()
                raise FileRefused(409, "files.already_bound",
                                  f"「{_field_label(form, name)}」的文件已经提交过一次，请重新上传", field=name)
            token.ids.append(row.id)
            out[name] = ref_of(row)
        await session.commit()
    return out, token


async def finish(token: Claim, run_id: str) -> None:
    if not token.ids:
        return
    async with SessionLocal() as session:
        await session.execute(update(StoredFile).where(StoredFile.bound_run_id == token.token)
                              .values(bound_run_id=run_id))
        await session.commit()


async def release(token: Claim) -> None:
    if not token.ids:
        return
    async with SessionLocal() as session:
        await session.execute(update(StoredFile).where(StoredFile.bound_run_id == token.token)
                              .values(bound_run_id=None))
        await session.commit()


async def bind_in(session: AsyncSession, form: Any, values: dict[str, Any], *, principal: Any, run_id: str,
                  context: dict[str, str]) -> dict[str, Any]:
    """在调用方的会话里把表单里的文件绑到一次已有的运行（回复待办）：和回复一起提交。交回换成登记引用的表单值。"""
    checked = await _check(session, form, values, principal=principal, context=context)
    out = dict(values)
    for name, row in checked:
        done = await session.execute(update(StoredFile).where(
            StoredFile.id == row.id, StoredFile.bound_run_id.is_(None), _NO_RECORD)
            .values(bound_run_id=run_id))
        if done.rowcount != 1:
            raise FileRefused(409, "files.already_bound",
                              f"「{_field_label(form, name)}」的文件已经提交过一次，请重新上传", field=name)
        out[name] = ref_of(row)
    return out


async def bound_files(run_id: str) -> dict[str, dict[str, Any]]:
    """这次运行绑定的文件：{file_id: 引用}。用到 file_text 的节点执行前读一次（files/text.prepare），只认这些。"""
    from app.db.models import Run

    async with SessionLocal() as session:
        out = {row.id: ref_of(row) for row in (await session.execute(
            select(StoredFile).where(StoredFile.bound_run_id == run_id))).scalars()}
        # 入口提交是先占住、发起成功后才改成运行 id（claim → finish）：运行开头的节点可能赶在改之前执行。
        # 占着的文件只要出现在这次运行的输入里、是发起人在这个应用里传的，就是这次提交占的那几个
        run = await session.get(Run, run_id)
        wanted = [ref["file_id"] for v in ((run.input if run is not None else None) or {}).values()
                  if (ref := as_ref(v)) is not None and ref["file_id"] not in out]
        if wanted and run is not None:
            origin = run.origin or {}
            for row in (await session.execute(select(StoredFile).where(
                    StoredFile.id.in_(wanted), StoredFile.bound_run_id.like(f"{CLAIM_PREFIX}%")))).scalars():
                ctx = row.context or {}
                same = (ctx.get("app_id") and ctx.get("app_id") == run.app_id) or (
                    ctx.get("blueprint_id") and ctx.get("blueprint_id") == origin.get("blueprint_id"))
                if row.uploaded_by == run.started_by and same:
                    out[row.id] = ref_of(row)
        return out


# --------------------------------------------------------------------------
# 读权限、删除
# --------------------------------------------------------------------------


async def readable(session: AsyncSession, principal: Any, row: StoredFile) -> bool:
    """能不能下载这个文件：关登录全部放行；上传的人自己；读得到绑定的那次运行；看得到绑定的那一行记录。"""
    from app.api.deps import can_read_run

    if principal.mode == "off":
        return True
    if principal.username and row.uploaded_by == principal.username:
        return True
    run_id = row.bound_run_id
    if run_id and not run_id.startswith(CLAIM_PREFIX) and await can_read_run(principal, run_id):
        return True
    if isinstance(row.bound_record, dict):
        return await _record_visible(session, principal, row.bound_record)
    return False


async def _record_visible(session: AsyncSession, principal: Any, bound: dict[str, Any]) -> bool:
    from app.db.models_blueprints import RecordsTable
    from app.records import store as records_store
    from app.records.access import scope_of
    from app.records.tables import columns_of, path_of

    table = await session.get(RecordsTable, str(bound.get("table_id") or ""))
    if table is None:
        return False
    scope = await scope_of(session, principal, table.app_id)
    if scope == "all":
        return True
    if scope != "mine":
        return False
    try:
        got = await asyncio.to_thread(records_store.row_by_id, path_of(table), columns_of(table),
                                      str(bound.get("row_id") or ""))
    except records_store.RecordsRefused:
        return False
    return got is not None and got.get("_submitted_by") == principal.username


async def remove(session: AsyncSession, principal: Any, file_id: str) -> None:
    """删一个文件：只能删自己传的、还没绑定的。内容没有别的登记引用时一起删掉。"""
    row = await session.get(StoredFile, file_id)
    if row is None or row.uploaded_by != getattr(principal, "username", None):
        raise FileRefused(404, "files.not_found", "文件不存在，或者不是你上传的")
    if row.bound_run_id is not None or row.bound_record is not None:
        raise FileRefused(409, "files.bound", "文件已经提交了，不能删除：它是那次运行的证据")
    sha = row.sha256
    await session.delete(row)
    await session.commit()
    await _drop_unreferenced(session, {sha})


async def _drop_unreferenced(session: AsyncSession, shas: set[str]) -> int:
    """这些内容没有任何登记引用了就删文件（连同抽出的文字缓存）。返回删了几个。"""
    if not shas:
        return 0
    used = set((await session.execute(select(StoredFile.sha256).where(StoredFile.sha256.in_(list(shas))))).scalars())
    gone = 0
    for sha in shas - used:
        from app.files.text import drop_cache

        try:
            blob_path(sha).unlink()
            gone += 1
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("文件 %s 删不掉，下次清理再试", sha, exc_info=True)
        drop_cache(sha)
    return gone


# --------------------------------------------------------------------------
# 清理
# --------------------------------------------------------------------------

#: 调度循环里多久清理一次（秒）
CLEANUP_EVERY_S = 3600
_last_cleanup: datetime | None = None


async def cleanup(now: datetime | None = None, *, ttl_s: int | None = None) -> int:
    """删掉 ttl_s（缺省 24 小时）之前上传、还没绑定的文件（占着却没发起成功的也算），返回删了几行。"""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=UNBOUND_TTL_S if ttl_s is None else ttl_s)
    async with SessionLocal() as session:
        stale = list((await session.execute(select(StoredFile.id, StoredFile.sha256).where(
            or_(StoredFile.bound_run_id.is_(None), StoredFile.bound_run_id.like(f"{CLAIM_PREFIX}%")),
            _NO_RECORD, StoredFile.uploaded_at < cutoff))).all())
        if not stale:
            return 0
        await session.execute(delete(StoredFile).where(StoredFile.id.in_([i for i, _ in stale])))
        await session.commit()
        await _drop_unreferenced(session, {sha for _, sha in stale})
    logger.info("清理了 %d 个超过时限没有提交的上传文件", len(stale))
    return len(stale)


async def cleanup_tick(now: datetime | None = None) -> int:
    """调度循环每一拍调；每小时真正清理一次。"""
    global _last_cleanup
    now = now or utcnow()
    if _last_cleanup is not None and (now - _last_cleanup).total_seconds() < CLEANUP_EVERY_S:
        return 0
    _last_cleanup = now
    return await cleanup(now)
