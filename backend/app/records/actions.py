"""records_append / records_lookup 的执行（平台内置能力，capabilities/platform.py）。

由 process/actions.send 经 registry.endpoint_request 调到这里：生效记录已经先写好（pending → sent），这里只管写那一行、
交回回执。幂等键就是动作键：发送用的是「动作键:第几次」，这里只取动作键，同一个动作不论第几次发都只落一行
（记录表 store.append 按它去重）。写下的行里有文件字段的，把那几个文件绑到这一行（下载按行级可见核对）。

表版本停用了（表单不兼容地改过、新一版已经发布）：停用之前发起的运行照常写进它（同「进行中的实例照常走完」：审批停在半路的
实例，批完写进它发起时的那一版）；停用之后发起的拒（records.table_retired）。没有运行的直接调用一律拒。

写之前核对三样（写进去的东西谁都看得到，不能凭图里写的一个名字就写）：
- 正式运行只写自己的记录表：表出自哪个蓝图，就只收那个蓝图发布出来的工作流的运行（没有蓝图的表，收钉着它的应用里的
  工作流）——不然一张手改过的图就能往别的应用的表里写（records.table_foreign）；
- 更正（supersedes）只能更正提交的人看得到的那一行：按行级可见（records/access），使用者只能更正自己提交的；
- 文件只绑这次运行提交时绑上的（StoredFile.bound_run_id 是这次运行）：值里随便写一个文件编号，不会把别人的文件挂到
  这一行上、让看得到这一行的人都能下载。

更正行的提交人沿用原行的（records/store.append）：原提交人照样看得见改过的那一行。动手更正的人记进回执（corrected_by，
随动作记录的回执落工件）和审计（records.correct），不记进行里。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.records import store

logger = logging.getLogger(__name__)


def _result(status: int | None, body: Any = None, *, error: str | None = None, environment: str = "prod") -> Any:
    from app.capabilities.registry import EndpointResult

    return EndpointResult(status=status, body=body, error=error, environment=environment)


def action_key_of(idem_key: str | None) -> str:
    """「动作键:第几次」→ 动作键。"""
    return str(idem_key or "").rsplit(":", 1)[0]


async def append_action(args: dict[str, Any], *, idem_key: str | None, run_id: str | None, on_behalf: Any,
                        environment: str) -> Any:
    from app.capabilities.platform import actor_of
    from app.db.models import Run
    from app.records.tables import columns_of, find_table, path_of, refuse_retired

    values = args.get("values")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            return _result(None, error="要写入的内容需要是一组字段和值（JSON 对象）", environment=environment)
    actor = actor_of(on_behalf)
    supersedes = str(args.get("supersedes") or "").strip() or None
    async with SessionLocal() as session:
        table = await find_table(session, str(args.get("table") or ""))
        source = None
        if table is not None and table.source_id:
            from app.db.models import DataSource

            source = await session.get(DataSource, table.source_id)
        try:
            if table is not None:
                # 停用的版本：停用之前发起的运行照常写完（同进行中的实例照常走完），之后发起的拒
                run = await session.get(Run, run_id) if run_id else None
                refuse_retired(table, launched_at=run.created_at if run is not None else None)
                await _check_writer(session, table, run_id)
                if supersedes:
                    await _check_supersedes(session, table, supersedes, actor)
        except store.RecordsRefused as e:
            return _result(None, error=e.message, environment=environment)
    if table is None:
        return _result(None, error=f"找不到记录表「{args.get('table')}」，可能已被删除，请重新发布应用",
                       environment=environment)
    try:
        columns = columns_of(table)
        # 多进程时几个进程会往同一个记录表文件里追加：库里的锁（store.write_lock）一个接一个，进程里的 _WRITE_LOCK 照旧
        async with store.write_lock():
            done = await asyncio.to_thread(
                store.append, path_of(table), columns, values, version=table.version, run_id=run_id,
                actor=actor, action_key=action_key_of(idem_key), supersedes=supersedes)
    except store.RecordsRefused as e:
        return _result(None, error=e.message, environment=environment)
    if not done.duplicate:
        await _bind_files(table, columns, done, run_id)
        if done.corrected_by is not None:
            await _audit_correction(table, done, supersedes, run_id)
    return _result(200, {"table": table.table_key, "version": table.version, "source": source.name if source else None,
                         **done.receipt()}, environment=environment)


async def _check_writer(session: Any, table: Any, run_id: str | None) -> None:
    """正式运行只写自己的记录表：表出自蓝图的，运行的工作流要是那个蓝图发布出来的；没有蓝图的，要是钉着这张表的应用里的
    工作流。不是正式运行的（探索运行在 actions.prepare 就定成演练，走不到这里）、没有运行的（平台内部直接调）不核对。"""
    from app.db.models import Run
    from app.db.models_apps import App
    from app.db.models_blueprints import Blueprint

    run = await session.get(Run, run_id) if run_id else None
    if run is None or run.run_class != "formal":
        return
    owners: set[str] = set()
    if table.blueprint_id:
        bp = await session.get(Blueprint, table.blueprint_id)
        if bp is not None and bp.workflow_id:
            owners.add(bp.workflow_id)
    elif table.app_id:
        app = await session.get(App, table.app_id)
        pins = ((app.draft or {}).get("pins") or []) if app is not None else []
        owners |= {str(p.get("workflow_id")) for p in pins if isinstance(p, dict) and p.get("workflow_id")}
    if run.workflow_id not in owners:
        raise store.RecordsRefused("records.table_foreign", f"记录表「{table.name}」不属于这个应用：正式运行只能写自己应用的"
                                                            "记录表，请检查写记录表那一步选的表")


async def _check_supersedes(session: Any, table: Any, row_id: str, actor: str | None) -> None:
    """更正只能更正提交的人看得到的那一行（行级可见同记录列表：使用者只看得到自己提交的）。看不到的说「不在这张表里」，
    不说它在不在。"""
    from app.records.access import scope_of_run
    from app.records.tables import columns_of, path_of

    scope = await scope_of_run(session, table.app_id, run_class=None, started_by=actor)
    if scope.get("rows") == "all":
        return
    row = await asyncio.to_thread(store.row_by_id, path_of(table), columns_of(table), row_id)
    if scope.get("rows") != "mine" or row is None or row.get("_submitted_by") != actor:
        raise store.RecordsRefused("records.supersede_missing", "要更正的那一行不在这张记录表里")


async def _audit_correction(table: Any, done: Any, supersedes: str | None, run_id: str | None) -> None:
    """更正留一条审计：谁改的（操作人）、改的是谁交的哪一行、换成了哪一行、哪次运行。审计没写成不改变已经写下的那一行
    （行已经落了，动作记录照常确认），只记日志。"""
    from app.apps.audit import record

    try:
        async with SessionLocal() as session:
            await record(session, action="records.correct", subject_kind="records_table", subject_id=table.id,
                         actor=done.corrected_by,
                         data={"table": table.table_key, "version": table.version, "name": table.name,
                               "row_id": done.row_id, "supersedes": supersedes, "submitted_by": done.submitted_by,
                               "run_id": run_id})
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("记录表的更正没能写进审计（表 %s，行 %s）", table.id, done.row_id)


async def _bind_files(table: Any, columns: list[Any], done: Any, run_id: str | None) -> None:
    """这一行的文件字段：把文件绑到这一行（StoredFile.bound_record）。下载时读得到这一行的人才能下（files/store.py）。
    只绑这次运行提交时绑上的文件（bound_run_id 是这次运行）：值里写的别的文件编号不认。"""
    from app.db.models_blueprints import StoredFile

    ids = []
    for column in columns:
        if column.type != "file":
            continue
        raw = done.values.get(column.name)
        try:
            ref = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(ref, dict) and isinstance(ref.get("file_id"), str):
            ids.append(ref["file_id"])
    if not ids:
        return
    async with SessionLocal() as session:
        for row in (await session.execute(select(StoredFile).where(StoredFile.id.in_(ids)))).scalars():
            if row.bound_record is None and run_id and row.bound_run_id == run_id:
                row.bound_record = {"table_id": table.id, "row_id": done.row_id}
        await session.commit()


async def lookup_action(args: dict[str, Any], *, environment: str) -> Any:
    """结果未知时按幂等键查：找到这次动作的生效记录，从它的预览里取写的是哪张表，再按动作键查那一行。"""
    from app.db.models import ActionRecord
    from app.records.tables import columns_of, find_table, path_of

    idem = str(args.get("idempotency_key") or "")
    async with SessionLocal() as session:
        rec = (await session.execute(select(ActionRecord).where(ActionRecord.idem_key == idem))).scalar_one_or_none()
        preview = rec.preview if rec is not None and isinstance(rec.preview, dict) else {}
        table = await find_table(session, str(preview.get("table") or "")) if preview else None
    if table is None:
        return _result(404, {"found": False}, environment=environment)
    try:
        got = await asyncio.to_thread(store.lookup, path_of(table), columns_of(table), action_key_of(idem))
    except store.RecordsRefused as e:
        return _result(None, error=e.message, environment=environment)
    if got is None:
        return _result(404, {"found": False}, environment=environment)
    return _result(200, {"table": table.table_key, "version": table.version, **got}, environment=environment)
