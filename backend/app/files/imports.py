"""平台内置能力 table_import_period：把一个上传的文件按数据源已有的配方导入成新的一期（docs/design/normal-user-apps.md N9）。

内核就是数据页的「上传新一期」（data/recipe_imports.reupload：原件存档、按当前配方自动试运行，不调模型），试运行通过、
没有要人勾的确认项、没有要写理由的核对时接着提交（commit_staging）。效果是写入（换掉数据源的当前数据），走动作和确认：
调用工具节点先开确认卡，人确认之后才执行到这里。

要人判断的情形不替人做主：试运行没通过、有必勾的确认项（单位变了、表结构变了、统计期要录入……）、有没通过要写理由的
核对——照实拒绝，说清要去数据页确认哪几项；这次的暂存留着，在数据页的导入里接着做。

只认这次运行绑定的文件（提交入口、回复待办时绑上的），不能凭一个文件编号导入别人的文件。
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal


def _result(status: int | None, body: Any = None, *, error: str | None = None, environment: str = "prod") -> Any:
    from app.capabilities.registry import EndpointResult

    return EndpointResult(status=status, body=body, error=error, environment=environment)


async def import_period_action(args: dict[str, Any], *, run_id: str | None, on_behalf: Any, environment: str) -> Any:
    from app.data import recipe_imports, table_versions
    from app.data.tabular import UnsupportedTable
    from app.db.models import DataSource
    from app.db.models_blueprints import StoredFile
    from app.files.store import as_ref, read_blob

    ref = as_ref(args.get("file"))
    if ref is None and isinstance(args.get("file"), str) and args["file"].strip():
        ref = {"file_id": args["file"].strip()}
    if ref is None:
        return _result(None, error="没有拿到要导入的文件", environment=environment)
    name = str(args.get("source") or "").strip()
    signer = _signer(on_behalf)
    async with SessionLocal() as session:
        row = await session.get(StoredFile, ref["file_id"])
        if row is None or not run_id or row.bound_run_id != run_id:
            return _result(None, error="要导入的文件没有随这次运行提交，不能导入", environment=environment)
        raw = await asyncio.to_thread(read_blob, row.sha256)
        if raw is None:
            return _result(None, error="要导入的文件内容不在了，或者和登记的不一致", environment=environment)
        source = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if source is None or (source.origin or "manual") != "upload" or not source.current_recipe_id:
            return _result(None, error=f"「{name}」不是按配方导入的上传表格，不能按配方导入一期", environment=environment)
        try:
            staging = await recipe_imports.reupload(session, source, raw=raw, filename=row.name, signed_by=signer)
            blocked = _needs_people(staging)
            if blocked:
                return _result(None, error=f"这一期要在「数据」页由人确认后才能导入：{blocked}。这次导入已经暂存，"
                                           f"在数据源「{name}」的导入里接着做", environment=environment)
            done = await recipe_imports.commit_staging(
                session, staging, trial_id=str((staging.trial or {}).get("trial_id") or ""), confirmations=[],
                acceptances=[], signed_by=signer)
        except (recipe_imports.ImportRefused, table_versions.StoreUnavailable) as e:
            return _result(None, error=getattr(e, "message", None) or str(e), environment=environment)
        except UnsupportedTable as e:
            return _result(None, error=f"文件读不出表格：{e}", environment=environment)
    return _result(200, {"source": name, "file": ref["file_id"], "import_id": done.import_id,
                         "snapshot_id": done.snapshot_id, "unchanged": done.unchanged, "parts": done.parts},
                   environment=environment)


def _signer(on_behalf: Any) -> str | None:
    """导入记录上签谁：确认这次导入的人；无人值守的（没人确认）签运行的发起人。"""
    if isinstance(on_behalf, dict):
        return str(on_behalf.get("act") or on_behalf.get("sub") or "") or None
    return str(on_behalf) if on_behalf else None


def _needs_people(staging: Any) -> str:
    """要人判断的地方（试运行没通过、必勾的确认项、要写理由的核对），用一句话列出；没有为空串。"""
    trial = staging.trial if isinstance(staging.trial, dict) else {}
    if not trial:
        return "按当前配方没能试运行（配方有问题或文件读不出）"
    if trial.get("status") != "passed":
        return "试运行没有通过"
    items = [str(i.get("label") or i.get("id")) for i in trial.get("confirm_items") or []
             if isinstance(i, dict) and i.get("required")]
    if items:
        return "确认 " + "；".join(items[:5]) + (f" 等 {len(items)} 项" if len(items) > 5 else "")
    if trial.get("acceptable"):
        return "有没通过的核对，需要写明接受的理由"
    return ""
