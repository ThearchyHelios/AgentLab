"""python -m app.db copy-to-postgres：SQLite 数据目录导入 PostgreSQL（db/to_postgres.py）。

端到端那几项要 PostgreSQL（scripts/test-postgres.sh）：另起一个进程在 SQLite 数据目录里跑一次停在审批上的运行（带事件、
审批、工件引用），导入一个新建的空库，再另起一个进程连这个 PostgreSQL、同一个数据目录，把审批批了，运行接着跑完。
不连库的几项（来源认不认、服务停没停、参数）两种库都跑。全是虚构数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db import to_postgres
from app.db.to_postgres import CopyRefused, copy_to_postgres

PG = bool(os.environ.get("AGENTLAB_TEST_DATABASE_URL", "").strip())
needs_pg = pytest.mark.skipif(not PG, reason="要 AGENTLAB_TEST_DATABASE_URL（scripts/test-postgres.sh）")
BACKEND = Path(__file__).resolve().parents[1]
#: 不会去连的地址：导入前的检查先于连目标库
NOWHERE = "postgresql+asyncpg://nobody@127.0.0.1:1/none"


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


GRAPH = {
    "nodes": [node("start", "input", fields=[{"name": "question"}]),
              node("prep", "transform", mode="template", template="第一段"),
              node("gate", "human", mode="approve", title="看一眼"),
              node("after", "transform", mode="template", template="第二段"),
              node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.after }}"}])],
    "edges": [{"source": a, "target": b} for a, b in
              (("start", "prep"), ("prep", "gate"), ("gate", "after"), ("after", "out"))],
}

MAKE_SOURCE = r'''
import asyncio, json, sys

async def main():
    from sqlalchemy import text
    from app.core import artifact_store
    from app.db.base import SessionLocal, engine, init_db
    from app.db.models import DataSource, Run
    from app.db.models_sources import SourceChange
    from app.engine.runner import run_manager

    await init_db()
    await run_manager.setup()
    run = await run_manager.start(graph=json.loads(sys.argv[1]), input_payload={"question": "虚构的问题"})
    status = None
    for _ in range(400):
        async with SessionLocal() as s:
            status = (await s.get(Run, run.id)).status
        if status in ("interrupted", "failed", "succeeded"):
            break
        await asyncio.sleep(0.05)
    await run_manager.wait_idle(run.id)
    artifact = await artifact_store.put_json({"区域": "虚构", "数": 1}, kind="test_copy", run_id=run.id)
    async with SessionLocal() as s:
        source = DataSource(name="虚构的源", kind="sqlite", database="/tmp/虚构的源.db")
        s.add(source)
        await s.flush()
        s.add(SourceChange(source_id=source.id, kind="column_added", fingerprint="f" * 64, table_name="订单"))
        dropped = SourceChange(source_id=source.id, kind="column_removed", fingerprint="d" * 64, table_name="订单")
        s.add(dropped)
        await s.flush()
        # 删掉最后一条：SQLite 的 AUTOINCREMENT 计数器停在 2，表里的 MAX 是 1。导过去之后新的变化要从 3 编起
        await s.delete(dropped)
        long = Run(workflow_name="长" * 300, last_seq=5_000_000_000, origin={"b": 2, "a": 1})
        sql_null = Run(workflow_name="SQL 的空", origin={"x": 1})
        json_null = Run(workflow_name="JSON 的空", origin=None)
        s.add_all([long, sql_null, json_null])
        await s.commit()
        await s.execute(text("UPDATE runs SET origin = NULL WHERE id = :i"), {"i": sql_null.id})
        await s.commit()
        out = {"long": long.id, "sql_null": sql_null.id, "json_null": json_null.id, "source": source.id}
    async with SessionLocal() as s:
        last_seq = (await s.get(Run, run.id)).last_seq
    await run_manager.shutdown()
    await engine.dispose()
    print(json.dumps({"run": run.id, "status": status, "last_seq": last_seq, "artifact": artifact, **out}))

asyncio.run(main())
'''

RESUME = r'''
import asyncio, json, sys

async def main():
    from sqlalchemy import select
    from app.core import artifact_store
    from app.db.base import SessionLocal, engine, init_db
    from app.db.models import Approval, Run, RunEvent
    from app.db.models_sources import SourceChange
    from app.engine.runner import run_manager

    run_id, source_id, artifact = sys.argv[1], sys.argv[2], sys.argv[3]
    await init_db()
    await run_manager.setup()
    await run_manager.resume(run_id, {"approved": True})
    status = None
    for _ in range(400):
        async with SessionLocal() as s:
            status = (await s.get(Run, run_id)).status
        if status in ("failed", "succeeded"):
            break
        await asyncio.sleep(0.05)
    await run_manager.wait_idle(run_id)
    async with SessionLocal() as s:
        seqs = list((await s.execute(select(RunEvent.seq).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.seq))).scalars())
        approvals = list((await s.execute(select(Approval.status).where(Approval.run_id == run_id))).scalars())
        row = await s.get(Run, run_id)
        change = SourceChange(source_id=source_id, kind="column_removed", fingerprint="e" * 64, table_name="订单")
        s.add(change)
        await s.commit()
        out = {"status": status, "error": row.error, "output": row.output, "seqs": seqs, "approvals": approvals,
               "new_change_seq": change.seq, "artifact": artifact_store.load(artifact)}
    await run_manager.shutdown()
    await engine.dispose()
    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
'''


def _run(script: str, *args: str, data_dir: Path, database_url: str = "") -> dict:
    env = {**os.environ, "AGENTLAB_DATA_DIR": str(data_dir), "AGENTLAB_DATABASE_URL": database_url,
           "AGENTLAB_CHECKPOINT_URL": "", "AGENTLAB_SCHEDULER": "0", "PYTHONPATH": str(BACKEND)}
    env.pop("AGENTLAB_DB_POOL_SIZE", None)
    done = subprocess.run([sys.executable, "-c", script, *args], env=env, capture_output=True, text=True,
                          timeout=180, cwd=str(data_dir))
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _sql(url: str, sql: str, *params) -> list[tuple]:
    import psycopg

    from app.db.urls import libpq_conninfo

    with psycopg.connect(libpq_conninfo(url)) as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------
# 不连库：来源、服务停没停、参数
# --------------------------------------------------------------------------


def test_the_source_must_be_an_sqlite_app_database(tmp_path):
    with pytest.raises(CopyRefused, match="找不到应用库"):
        to_postgres.resolve_source(tmp_path)
    (tmp_path / "agentlab.db").write_bytes(b"not a database at all")
    with pytest.raises(CopyRefused, match="不是 SQLite 库文件"):
        to_postgres.resolve_source(tmp_path)


async def test_a_source_still_open_is_refused_before_anything_is_written(tmp_path):
    import sqlite3

    sqlite3.connect(tmp_path / "agentlab.db").execute("CREATE TABLE t (x)").connection.close()
    (tmp_path / "agentlab.db-wal").write_bytes(b"")
    with pytest.raises(CopyRefused, match="先停服务"):
        await copy_to_postgres(tmp_path, NOWHERE, progress=lambda _: None)


def test_the_command_needs_a_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "")
    with pytest.raises(SystemExit) as exited:
        to_postgres.main([str(tmp_path)])
    assert exited.value.code == 2 and "没有目标地址" in capsys.readouterr().err
    assert to_postgres.main([str(tmp_path), "mysql://x@h/db"]) == 2
    assert "只认 PostgreSQL" in capsys.readouterr().err


def test_the_dispatcher_knows_its_commands(capsys):
    from app.db.__main__ import main

    assert main([]) == 2 and "copy-to-postgres" in capsys.readouterr().out
    assert main(["drop-everything"]) == 2 and "认不出的命令" in capsys.readouterr().err


def test_pending_writes_keep_their_order_and_special_channels_apart():
    from langgraph.checkpoint.base import WRITES_IDX_MAP

    special = next(iter(WRITES_IDX_MAP))
    groups = to_postgres._split_writes([("t1", special, 1), ("t1", "a", 2), ("t2", "b", 3), ("t1", "c", 4)])
    assert groups == {"t1": ([(special, 1)], [("a", 2), ("c", 4)]), "t2": ([], [("b", 3)])}


def test_values_sqlite_kept_but_postgresql_refuses_are_fixed_and_counted():
    from sqlalchemy import Column, Integer, MetaData, String, Table

    from app.db.base import PortableJSON

    table = Table("t", MetaData(), Column("n", Integer), Column("s", String(5)), Column("j", PortableJSON))
    fixed: dict[str, int] = {}
    fix = to_postgres._Fixer(fixed)
    assert fix.value(table.c.n, 3.0) == 3 and fix.value(table.c.n, " 12 ") == 12 and fix.value(table.c.n, 7) == 7
    assert fix.value(table.c.s, 42) == "42" and fix.value(table.c.s, "a\x00b") == "ab"
    assert fix.value(table.c.j, '{"b": 1, "a": NaN}') == '{"b": 1, "a": null}'
    assert fix.value(table.c.j, '{"b": 1, "a": 2}') == '{"b": 1, "a": 2}', "平常的 JSON 原文照搬，键的先后不变"
    assert fix.value(table.c.j, None) is None
    assert set(fixed) == {"整数列里的浮点写法", "整数列里的数字字符串", "文本列里的非文本值", "文本里的 NUL 字符",
                          "JSON 里的 NaN / Infinity / NUL"}


# --------------------------------------------------------------------------
# 端到端：SQLite 上停在审批的运行，导入 PostgreSQL 后接着跑完
# --------------------------------------------------------------------------


@pytest.fixture
def source_dir(tmp_path):
    """另起一个进程，在 SQLite 数据目录里留下一次停在审批上的运行和几行只看值的数据。"""
    data = tmp_path / "data"
    data.mkdir()
    made = _run(MAKE_SOURCE, json.dumps(GRAPH), data_dir=data)
    assert made["status"] == "interrupted"
    assert not Path(f"{data}/agentlab.db-wal").exists(), "来源进程正常退出后不该留着 -wal"
    return data, made


@needs_pg
async def test_a_waiting_run_is_imported_and_finishes_on_postgresql(source_dir):
    from tests.appdb import fresh_postgres_database

    data, made = source_dir
    lines: list[str] = []
    with fresh_postgres_database("copy") as target:
        report = await copy_to_postgres(data, target, progress=lines.append)
        assert report.ok, "\n".join(to_postgres.render(report))
        by_table = {t.name: t for t in report.tables}
        assert by_table["runs"].source == by_table["runs"].present_after == 4
        assert by_table["run_events"].source > 0 and by_table["approvals"].present_after == 1
        assert by_table["artifacts"].present_after >= 1 and by_table["source_changes"].present_after == 1
        checkpoints, writes = report.checkpoints
        assert checkpoints > 0 and report.checkpoints_target == (checkpoints, writes)
        assert report.threads == 1 and report.checkpoints_written == checkpoints

        # 值：超长的名字、过 21 亿的整数照存；SQL 的 NULL 和 JSON 的 null 分开；JSON 原文（键的先后）不变
        [(name, seq)] = _sql(target, "SELECT workflow_name, last_seq FROM runs WHERE id = %s", made["long"])
        assert name == "长" * 300 and seq == 5_000_000_000
        assert _sql(target, "SELECT CAST(origin AS TEXT) FROM runs WHERE id = %s", made["long"]) == \
            [('{"b": 2, "a": 1}',)]
        assert _sql(target, "SELECT origin IS NULL FROM runs WHERE id = %s", made["sql_null"]) == [(True,)]
        assert _sql(target, "SELECT CAST(origin AS TEXT) FROM runs WHERE id = %s", made["json_null"]) == [("null",)]

        # 再导一遍：什么都不重复，目标上已有的不动
        again = await copy_to_postgres(data, target, progress=lambda _: None)
        assert again.ok and again.checkpoints_written == 0
        assert all(t.present_before == t.source for t in again.tables)
        assert {t.name: t.target_after for t in again.tables} == {t.name: t.target_after for t in report.tables}

        # PostgreSQL 上、同一个数据目录：批了审批，运行从断点接着跑完
        done = _run(RESUME, made["run"], made["source"], made["artifact"], data_dir=data, database_url=target)
        assert done["status"] == "succeeded", done["error"]
        assert done["output"]["结果"] == "第二段"
        assert done["approvals"] == ["resolved"], "导入的那条审批就是被批掉的那条，没有另起一条"
        seqs = done["seqs"]
        assert seqs == sorted(set(seqs)) and seqs[-1] > made["last_seq"], "接着导入前的序号往下编"
        assert done["artifact"] == {"区域": "虚构", "数": 1}
        assert done["new_change_seq"] == 3, "序列接在 SQLite 的 AUTOINCREMENT 计数器后面，删过的号不复用（召回按它当游标）"


@needs_pg
async def test_dry_run_counts_and_writes_nothing(source_dir):
    from tests.appdb import fresh_postgres_database

    data, _ = source_dir
    with fresh_postgres_database("dry") as target:
        report = await copy_to_postgres(data, target, dry_run=True, progress=lambda _: None)
        assert report.ok and report.checkpoints[0] > 0
        runs = next(t for t in report.tables if t.name == "runs")
        assert runs.source == 4 and runs.present_before == runs.present_after == 0
        assert _sql(target, "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                            "('runs', 'checkpoints')") == [(0,)]
        assert "只核对，什么都没写" in to_postgres.render(report)[-1]


@needs_pg
async def test_a_target_with_the_service_still_connected_is_refused(source_dir):
    import psycopg

    from app.db.urls import libpq_conninfo
    from tests.appdb import fresh_postgres_database

    data, _ = source_dir
    with fresh_postgres_database("busy") as target:
        with psycopg.connect(libpq_conninfo(target), application_name="agentlab"):
            with pytest.raises(CopyRefused, match="服务的连接"):
                await copy_to_postgres(data, target, progress=lambda _: None)
        assert _sql(target, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'runs'") == [(0,)]


# --------------------------------------------------------------------------
# 老库：补列时没带缺省值的 NOT NULL 列、SQLite 当时没强制外键留下的孤儿行
# --------------------------------------------------------------------------


def _legacy_source(folder: Path) -> Path:
    """照模型建一个 SQLite 应用库，再做旧：定时任务的 trigger 列删掉、按第一版迁移（不带缺省值）补回来，存量行是 NULL；
    关着外键写几行孤儿行（对话轮次指向已删的运行、事件指向已删的运行、轮次指向已删的对话）。"""
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, insert

    from app.db import models  # noqa: F401
    from app.db.base import Base

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "agentlab.db"
    engine = create_engine(f"sqlite:///{path}")
    tables = Base.metadata.tables
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(insert(tables["schedules"]).values(id="s1", workflow_id="w1", cron="0 9 * * *",
                                                             timezone="Asia/Shanghai"))
            conn.execute(insert(tables["schedule_fires"]).values(
                id="f1", schedule_id="s1", fire_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
            conn.execute(insert(tables["conversations"]).values(id="c1", title="虚构的对话"))
            conn.execute(insert(tables["conversation_turns"]).values(id="t1", conversation_id="c1", seq=0,
                                                                     run_id="run-gone"))
            conn.execute(insert(tables["conversation_turns"]).values(id="t2", conversation_id="conv-gone", seq=0))
            conn.execute(insert(tables["run_events"]).values(id="e1", run_id="run-gone", seq=1, type="log", ts=0.0,
                                                             data={}))
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE schedules DROP COLUMN \"trigger\""))
            conn.execute(text("ALTER TABLE schedules ADD COLUMN \"trigger\" JSON"))
    finally:
        engine.dispose()
    return path


def test_old_sqlite_databases_get_null_not_null_columns_filled(tmp_path):
    """SQLite 这边：启动时的迁移把老版本补列留下的 NULL 填成模型的缺省值，两种库存的一样。"""
    from sqlalchemy import create_engine

    from app.db.base import _migrate

    path = _legacy_source(tmp_path / "old")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT \"trigger\" FROM schedules")).scalar_one() is None
            _migrate(conn)
            assert conn.execute(text("SELECT \"trigger\" FROM schedules")).scalar_one() == "{}"
    finally:
        engine.dispose()


def test_orphan_rows_are_nulled_or_left_out_on_the_snapshot(tmp_path):
    path = _legacy_source(tmp_path / "old")
    fixed: dict[str, int] = {}
    orphans = to_postgres.settle_orphans(path, fixed=fixed)
    assert fixed == {"外键指向已不存在的行，置空": 1}
    assert orphans == {"conversation_turns": 1, "run_events": 1}
    import sqlite3

    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT id, run_id FROM conversation_turns").fetchall() == [("t1", None)]
        assert db.execute("SELECT count(*) FROM run_events").fetchone() == (0,)
    finally:
        db.close()


@needs_pg
async def test_an_old_database_with_nulls_and_orphans_imports_cleanly(tmp_path):
    """真实开发库上核对不过的那两种：定时任务 trigger 为 NULL（NOT NULL 列），对话轮次指向已删的运行。导入照样核对通过，
    孤儿行照实列出、不算失败，重跑也一样。"""
    from tests.appdb import fresh_postgres_database

    folder = tmp_path / "old"
    _legacy_source(folder)
    with fresh_postgres_database("legacy") as target:
        report = await copy_to_postgres(folder, target, progress=lambda _: None)
        lines = to_postgres.render(report)
        assert report.ok, "\n".join(lines)
        assert report.orphans == {"conversation_turns": 1, "run_events": 1}
        assert report.fixed.get("外键指向已不存在的行，置空") == 1
        assert any("孤儿行" in line and "run_events 1 行" in line for line in lines)
        assert _sql(target, "SELECT CAST(\"trigger\" AS TEXT) FROM schedules") == [("{}",)]
        assert _sql(target, "SELECT count(*) FROM schedule_fires") == [(1,)]
        assert _sql(target, "SELECT run_id FROM conversation_turns") == [(None,)]
        again = await copy_to_postgres(folder, target, progress=lambda _: None)
        assert again.ok and again.orphans == report.orphans


def test_null_not_null_values_fall_back_to_the_model_default():
    from app.db.base import Base

    schedules = Base.metadata.tables["schedules"]
    fixed: dict[str, int] = {}
    fix = to_postgres._Fixer(fixed)
    assert fix.value(schedules.c["trigger"], None) == "{}"          # JSON 列按原文绑定
    assert fix.value(schedules.c["trigger_kind"], None) == "cron"
    assert fix.value(schedules.c["target_id"], None) is None        # 可空的照旧是 NULL
    assert fix.value(schedules.c["cron"], None) is None             # 没有缺省值的：照实交给库去拒
    assert fixed == {"NOT NULL 列的空值填成缺省值": 2}


# --------------------------------------------------------------------------
# 停服务之后数据目录里不留 -wal（导入按它认服务停没停）
# --------------------------------------------------------------------------

START_STOP = r'''
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    assert client.get("/api/health").status_code == 200
'''


def test_stopping_the_service_leaves_no_wal_behind(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "AGENTLAB_DATA_DIR": str(data), "AGENTLAB_DATABASE_URL": "", "AGENTLAB_CHECKPOINT_URL": "",
           "AGENTLAB_SCHEDULER": "0", "PYTHONPATH": str(BACKEND)}
    done = subprocess.run([sys.executable, "-c", START_STOP], env=env, capture_output=True, text=True, timeout=180,
                          cwd=str(data))
    assert done.returncode == 0, done.stderr[-4000:]
    assert (data / "agentlab.db").is_file() and (data / "checkpoints.db").is_file()
    assert sorted(p.name for p in data.iterdir() if p.name.endswith(("-wal", "-shm"))) == []
    assert to_postgres.source_in_use(data / "agentlab.db", data / "checkpoints.db") is None
