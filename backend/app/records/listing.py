"""记录的逐行查看（组件 records_list、GET /api/apps/{id}/records，docs/design/normal-user-apps.md §3.7、§4.7）。

每次打开按看的人实时过滤，不走封存（同实例列表）：use 只看自己提交的，observe、manage 看全部；关登录时看全部（没填署名、
又要只看自己的，分不出谁是谁，按全部给，同实例列表）。一张记录表的各版本合在一起列：新版本在前、每个版本里新提交的
在前；被更正过的行默认不列，history 打开时列出并标 superseded。

遮罩：别人提交的行里，数据源设了遮罩的列（「填什么」里标了敏感的，加上管理员在数据页手工加的）换成「已遮罩」——和看板证据
同一套遮罩列、同一个说法（P4、P5）；自己提交的行照原样给（是自己填的）。附件列遮掉之后连文件的引用也不给。

返回的形状同 frontend/src/typesBlueprint.ts 的 RecordsPage，每个键都在：{table, version, columns, items, mine_only,
next_cursor, state}。next_cursor 是下一页的游标（「版本:序号」），没有下一页为 null；state 是 ok（有这张表，items 可能
为空）、empty（这个应用还没有记录表），草稿预览（apps/resolve._records_list）是 preview（不列真实的记录）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.records import store
from app.records.access import scope_of_role
from app.records.tables import columns_of, path_of, tables_of_app, versions_of

#: 一页最多几行
PAGE_MAX = 200


async def _masked(session: AsyncSession, row: Any) -> set[str]:
    """这一版登记成的数据源设了遮罩的列（小写）。"""
    from app.data.engine import masked_columns
    from app.db.models import DataSource

    source = await session.get(DataSource, row.source_id) if row.source_id else None
    return {c.lower() for c in masked_columns(source.options if source is not None else None)}


def _shown(item: dict[str, Any], version: int, masked: set[str], viewer: str | None) -> dict[str, Any]:
    """一行交给看的人的样子：别人提交的行，遮罩列有值的换成「已遮罩」（P4）。"""
    from app.api.evidence import MASKED

    out = {**item, "_version": version}
    if not masked or (viewer and item.get("_submitted_by") == viewer):
        return out
    values = item.get("values") or {}
    out["values"] = {k: MASKED if k.lower() in masked and v is not None else v for k, v in values.items()}
    return out


def _cursor(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    version, _, seq = str(raw).partition(":")
    return (int(version), int(seq)) if version.isdigit() and seq.isdigit() else None


async def page(session: AsyncSession, app: Any, *, principal: Any, role: str | None, table: str | None = None,
               who: str | None = None, cursor: str | None = None, history: bool = False,
               limit: int = 50) -> dict[str, Any]:
    tables = await tables_of_app(session, app.id)
    current = next((t for t in tables if table in (None, "", t.table_key, t.id)), None)
    if current is None and table:
        # 也认数据源名（rec_…）：模板里手上拿着的可能是它
        from app.records.tables import find_table

        found = await find_table(session, table)
        current = next((t for t in tables if found is not None and t.table_key == found.table_key), None)
    scope = scope_of_role(principal, role)
    if who == "mine" and scope == "all":
        scope = "mine"
    viewer = principal.username
    if scope == "mine" and not viewer and principal.mode == "off":
        scope = "all"
    if current is None:
        return {"table": table, "version": 0, "columns": [], "items": [], "mine_only": scope == "mine",
                "state": "empty", "next_cursor": None}
    limit = max(1, min(int(limit), PAGE_MAX))
    versions = await versions_of(session, current.table_key)
    at = _cursor(cursor)
    items: list[dict[str, Any]] = []
    more = False
    for row in versions:
        if at is not None and row.version > at[0]:
            continue
        before = at[1] if at is not None and row.version == at[0] else None
        cols = columns_of(row)
        try:
            got, more_here = await asyncio.to_thread(
                store.rows, path_of(row), cols, scope=scope, viewer=viewer, history=history, before=before,
                limit=limit - len(items))
        except store.RecordsRefused:
            continue
        masked = await _masked(session, row)
        items += [_shown(r, row.version, masked, viewer) for r in got]
        if len(items) >= limit:
            more = more_here or row is not versions[-1]
            break
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in versions:
        for c in columns_of(row):
            if c.name.lower() not in seen:
                seen.add(c.name.lower())
                columns.append({"name": c.name, "label": c.label or c.name, "type": c.type})
    last = items[-1] if items else None
    return {"table": current.table_key, "version": versions[0].version if versions else current.version,
            "columns": columns, "items": items, "mine_only": scope == "mine",
            "next_cursor": f"{last['_version']}:{last['_seq']}" if more and last else None, "state": "ok"}
