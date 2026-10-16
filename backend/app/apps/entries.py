"""应用入口：按入口表单校验 → 映射成钉住版本的运行输入 → 发起正式运行（来源 app_entry / hook）。

应用里的运行永远是钉住版本的正式运行（§2.11）：版本、graph_hash 都取应用当前发布版里记下的那一份，launch_run 再核对一遍
（expect_hash）。实例标题只能按名字引用入口字段，在这里拼好、记进 run.origin（在封存范围内）。
"""
from __future__ import annotations

import re
from typing import Any

from app.apps.tree import entry_of, pins_by_alias, resolve_form
from app.files import store as files_store

#: 入口发起被拒时的机器码
APP_GONE = "app_not_found"
APP_UNPUBLISHED = "app_unpublished"
ENTRY_GONE = "app_entry_not_found"
NEEDS_UPGRADE = "app_needs_upgrade"

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_㐀-鿿]+)\}")
TITLE_MAX = 200


class EntryRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class FormRejected(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("表单没有通过校验")
        self.errors = errors


def map_inputs(entry: dict[str, Any], values: dict[str, Any], principal: Any) -> dict[str, Any]:
    """入口映射：入口字段 ← 表单值 / 当前用户 / 常量。表单里没填的不交（入口节点补默认值）。"""
    from app.apps.lint import parse_map_source

    out: dict[str, Any] = {}
    for target, source in (entry.get("map") or {}).items():
        parsed = parse_map_source(source)
        if parsed is None:
            continue
        kind, what = parsed
        if kind == "form":
            value = values.get(what)
            if value in (None, ""):
                continue
        elif kind == "user":
            value = (getattr(principal, "display_name", None) or getattr(principal, "username", None)) \
                if what == "display_name" else getattr(principal, "username", None)
            if value is None:
                continue
        else:
            value = what
        out[target] = value
    return out


def render_title(entry: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    """实例标题：pattern 里的 {字段} 换成入口字段的值，只认 fields 里列出的名字；没写标题的为 None。"""
    title = entry.get("instance_title")
    if not isinstance(title, dict) or not isinstance(title.get("pattern"), str):
        return None
    allowed = set(title.get("fields") or [])

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in allowed:
            return m.group(0)
        value = inputs.get(name)
        return "" if value is None else str(value)

    return _PLACEHOLDER.sub(sub, title["pattern"]).strip()[:TITLE_MAX] or None


async def start_entry(app_id: str, entry_id: str, values: dict[str, Any], principal: Any, *,
                      origin_kind: str = "app_entry", hook: str | None = None, hook_key: str | None = None) -> Any:
    """发起一个实例，返回 Run。校验失败抛 FormRejected(errors)；应用需升级（钉住的版本已退役）时拒绝。

    钩子发起的（origin_kind=hook）来源里另记钩子名和这次请求的幂等键（hook_key）：处理到一半服务停了、同一个键重新
    认领时，按它找已经发起的实例（process/hooks.py），不重复发起。

    principal.username 记成发起人（关登录时是自报的署名，调用方按 X-Actor 填好）。发起被 launch_run 拒绝时抛
    LaunchRefused，原样交给调用方。"""
    from sqlalchemy import select

    from app.apps.forms import validate_form
    from app.apps.pins import load_pins
    from app.db.base import SessionLocal
    from app.db.models_apps import App, AppVersion
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        app = await session.get(App, app_id)
        if app is None or app.status == "archived":
            raise EntryRefused(404, "应用不存在或已归档", APP_GONE)
        if not app.published_version:
            raise EntryRefused(409, f"「{app.name}」还没有发布，发布之后才能发起", APP_UNPUBLISHED)
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one()
        spec = version.spec or {}
        entry = entry_of(spec, entry_id)
        if entry is None:
            raise EntryRefused(404, f"应用里没有入口「{entry_id}」", ENTRY_GONE)
        pin = pins_by_alias(spec).get(str(entry.get("pin")))
        state = (await load_pins(session, {"pins": [pin]})).get(str(entry.get("pin"))) if pin else None
        if state is None or not state.usable:
            raise EntryRefused(409, "这个入口固定的工作流版本已退役或不可用，应用需要升级后才能发起新实例", NEEDS_UPGRADE)
        form = resolve_form(spec, entry.get("form")) or {"type": "object", "properties": {}}
        if errors := validate_form(form, values if isinstance(values, dict) else None):
            raise FormRejected(errors)
        # 选人控件：开了登录时按候选池核对（auth/directory.py，05-已知遗留 (b)）。以前原样传下去，填错的名字会把待办
        # 派给一个不存在的人——没人能处理，也不报错
        from app.auth.directory import principal_errors

        if errors := await principal_errors(session, app, version.bindings, form, values,
                                            getattr(principal, "mode", "off")):
            raise FormRejected(errors)
        # 表单里的文件（docs/design/normal-user-apps.md N9）：核对是提交人在这个入口传的、还没用过，先占住；表单值换成
        # 登记的那一份引用（连同 sha256 进运行输入、随封存固定）。发起成功绑到这次运行，发起不成放回去
        values, claimed = await _claim_files(form, values, principal, app.id, entry_id)
        inputs = map_inputs(entry, values, principal)
        title = render_title(entry, inputs)
        try:
            run = await launch_run(
                session, workflow_id=pin["workflow_id"], run_class="formal", version=pin["version"],
                expect_hash=pin["graph_hash"], input_payload=inputs, actor=getattr(principal, "username", None),
                origin_kind=origin_kind, app_id=app.id, app_version=version.version,
                origin={"entry": entry_id, "spec_hash": version.spec_hash, "title": title, "hook": hook,
                        "hook_key": hook_key, "actor_verified": bool(getattr(principal, "verified", False)),
                        "auth_mode": getattr(principal, "mode", "off")})
        except BaseException:
            await files_store.release(claimed)
            raise
        await files_store.finish(claimed, run.id)
        return run


async def _claim_files(form: dict[str, Any], values: dict[str, Any], principal: Any, app_id: str,
                       entry_id: str) -> tuple[dict[str, Any], Any]:
    """占住表单里的文件；引用不对（不是自己传的、在别处传的、已经用过）按表单错误交回，落到那个字段上。"""
    try:
        return await files_store.claim(form, values, principal=principal,
                                       context={"app_id": app_id, "entry": entry_id})
    except files_store.FileRefused as e:
        raise FormRejected([{"path": e.field or "", "code": files_store.FILE_ERROR_CODE, "message": e.message}]) from e
