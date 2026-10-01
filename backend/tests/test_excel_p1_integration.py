"""期 1 合并后的集成：版本底座（table_versions）发布出来的快照，经连接层（engine）查询。

两边是分开开发的：连接层用替身对象测了 immutable 和哈希核对，版本底座测了发布和钉版本。
这里把真的 SourceView 交给真的 EngineCache，确认几件只有接起来才看得出的事：
- 快照按 immutable 只读打开，写错的双引号列名照样报错（关 DQS 对快照也生效）；
- 快照文件被改过，下一次建连接就拒绝查询，不会读到改过的数据；
- 重传之后，钉在旧快照上的查询读旧数据、当前版本读新数据，两份连接互不串。
"""
from __future__ import annotations

import io
import os
import stat
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.data import table_versions
from app.data.engine import SnapshotTampered, engine_args, engines, run_query
from app.db.base import SessionLocal
from app.db.models import DataSource, Run


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


def xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "明细"
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def rows_v1() -> list[list]:
    return [["区域", "数量"], ["分区甲", 1], ["分区乙", 2], ["标记", uuid.uuid4().int % 1000]]


async def publish(name: str, raw: bytes):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(id=uuid.uuid4().hex, name=name, kind="sqlite", readonly=True, origin="upload")
        result = await table_versions.publish_upload(session, row, raw, "明细.xlsx",
                                                     header_row=1, mixed="reject", raw_mode=False)
        return row.id, result


async def resolve(source_id: str, pinned: str | None = None):
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        return await table_versions.resolve_source(session, row, pinned)


def name() -> str:
    return f"p1int_{uuid.uuid4().hex[:10]}"


async def test_snapshot_view_opens_immutable_and_rejects_misspelled_columns():
    sid, _ = await publish(name(), xlsx(rows_v1()))
    view = await resolve(sid)
    try:
        assert view.immutable is True and view.readonly is True and view.expected_sha256
        url, _ = engine_args(view)
        assert "mode=ro" in url and "immutable=1" in url
        assert (await run_query(view, 'SELECT SUM("数量") FROM "明细" WHERE "区域" != \'标记\'')).rows[0][0] == 3
        # 写错列名：关 DQS 以后报错，不会把 "数目" 当成字符串、静默返回 0
        with pytest.raises(Exception, match="no such column"):
            await run_query(view, 'SELECT SUM("数目") FROM "明细"')
    finally:
        await engines.invalidate(sid)


async def test_tampered_snapshot_is_refused_on_next_connection():
    sid, _ = await publish(name(), xlsx(rows_v1()))
    view = await resolve(sid)
    try:
        await run_query(view, 'SELECT COUNT(*) FROM "明细"')
        await engines.invalidate(sid)
        path = view.database
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with open(path, "r+b") as f:       # 改动文件末尾的一个字节：数据页内容变了，哈希随之变
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
            f.seek(-1, os.SEEK_END)
            f.write(bytes([last[0] ^ 0xFF]))
        with pytest.raises(SnapshotTampered):
            await run_query(view, 'SELECT COUNT(*) FROM "明细"')
    finally:
        await engines.invalidate(sid)


async def test_pinned_old_snapshot_and_current_snapshot_do_not_share_connections():
    src = name()
    sid, first = await publish(src, xlsx(rows_v1()))
    # 一条钉在旧快照上的运行（状态是中断，等着续跑）：有它引用，重传后的回收不会删掉旧快照
    async with SessionLocal() as session:
        session.add(Run(workflow_name="钉版本", status="interrupted",
                        data_versions={sid: {"snapshot": first.snapshot_id, "name": src}}))
        await session.commit()
    _, second = await publish(src, xlsx([["区域", "数量"], ["分区甲", 100], ["标记", uuid.uuid4().int % 1000]]))
    assert first.snapshot_id != second.snapshot_id
    old = await resolve(sid, first.snapshot_id)
    new = await resolve(sid)
    try:
        q = 'SELECT SUM("数量") FROM "明细" WHERE "区域" != \'标记\''
        assert (await run_query(new, q)).rows[0][0] == 100
        assert (await run_query(old, q)).rows[0][0] == 3
        # 先查新再查旧、再查新：两份连接各走各的缓存键
        assert (await run_query(new, q)).rows[0][0] == 100
    finally:
        await engines.invalidate(sid)


async def test_unshaped_note_reaches_every_text_the_model_and_the_judge_read():
    """按原样导入的表：「未规整」的说明不只在 db_schema 查单表时看得到——查询工具的描述、db_schema 的表清单、
    报告的写作目录、裁判的摘录里都要标出（R4、D16），而且不含数字（H11）。"""
    from app.core.artifact_store import load as load_artifact
    from app.engine import judge
    from app.engine.evidence import build_catalog, catalog_prompt, extract_numbers, schema_ledger_entry
    from app.tools.datasource import build_datasource_tools
    from app.tools.registry import ToolContext, Versions
    from tests.test_tabular import crosstab

    src = name()
    async with SessionLocal() as session:
        row = DataSource(id=uuid.uuid4().hex, name=src, kind="sqlite", readonly=True, origin="upload")
        await table_versions.publish_upload(session, row, crosstab(), "客流.xlsx",
                                            header_row=4, mixed="null", raw_mode=True)
        sid = row.id
        tools = {t.name: t for t in await build_datasource_tools(
            [f"db_query__{src}", f"db_schema__{src}"], ToolContext(data_versions=Versions.CURRENT), session)}
    try:
        query, schema = tools[f"db_query__{src}"], tools[f"db_schema__{src}"]
        assert "客流汇总 按原样导入、未经规整" in query.description and "不能直接对列求和" in query.description
        assert "客流汇总（按原样导入、未经规整，不能直接对列求和）" in await schema.coroutine()
        assert extract_numbers(table_versions.UNSHAPED_NOTE) == []

        import json
        payload = json.loads(await query.coroutine(sql='SELECT COUNT(*) AS n FROM "客流汇总"'))
        frozen = schema_ledger_entry(payload["schema_artifact"], node_id="fetch", exec_no=1)
        assert frozen is not None
        catalog = build_catalog(nodes={}, ledger=[frozen])
        prompt = catalog_prompt(catalog, loader=load_artifact)
        assert "客流汇总（按原样导入、未经规整：同一列里混有不同口径的行，不能直接对列求和）" in prompt

        excerpt = judge._excerpt("t:客流汇总", catalog["t:客流汇总"], [], load_artifact, None, catalog)
        assert f"表的说明：{table_versions.UNSHAPED_NOTE}" in excerpt
    finally:
        await engines.invalidate(sid)


async def test_shaped_tables_carry_no_unshaped_note():
    """规整的表不带这句：别的表、手工源的表结构照旧。"""
    from app.tools.datasource import _query_description

    sid, _ = await publish(name(), xlsx(rows_v1()))
    view = await resolve(sid)
    try:
        assert "未经规整" not in _query_description(view)
    finally:
        await engines.invalidate(sid)
