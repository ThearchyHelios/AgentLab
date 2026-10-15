"""数据剖析的后台任务（data/catalog_profile_jobs.py、/api/datasources/{id}/catalog/profile-jobs）。

剖析的结论怎么进目录、安全约束（只读、守卫、遮罩、大表）在 test_catalog_profile.py；这里钉住任务本身：

- 同源排队：起草在跑时剖析排队（waiting_for = "draft"），起草结束自动开始；反过来起草排在剖析后面（不 409）；停掉排队的；
  导入目录、同步起草在剖析时 409 profile_busy；同步起草期间建的剖析任务排在它后面
- 停止：做完的项写进目录，没做的保留旧结论、记 cancelled，后面的表 cancelled；已结束的再停 409
- 服务重启标中断、不续跑；关停时同样；重试只带没做完的表
- 任务级止损：连续 3 条失败、最近 10 条 7 条超时（停下、后面的表带同一句原因），最近 6 条 4 条超时提醒、好转后清掉
- 每张表开始前重读数据源：停用、关掉剖析就停；遮罩按最新的设置
- 范围：需要剖析 / 全部 / 指定；空表一条查询记 skipped；视图跳过；没有可剖析的项；挑不出表 409；profile_due 的三种情形
- 留痕：IN 列表里抽到的键值不进 AgentLab 的库
- 设置迁移：旧键启动时删掉，老页面带着旧键保存只摘掉
- 保留：超过保留数删旧的，但不删某张表最近一次剖析所在的任务

表名、列名、数据都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.data import catalog, catalog_jobs, catalog_profile, catalog_profile_jobs, catalog_transfer
from app.data import engine as data_engine
from app.data.catalog_profile import PROFILE_OPTION, ProfileSettings, SourceRunner, SqlDialect
from app.data.guard import SqlRejected
from app.db.base import SessionLocal, utcnow
from app.db.models import CatalogProfileJob, CatalogProfileTask, DataSource
from tests.test_catalog_profile import (  # noqa: F401
    ACTOR, _detail, _finished, _job, _notes, _on, _profile, _relation, _start, _table, _url, client, make_source,
)


@pytest.fixture(autouse=True)
async def _stop_jobs():
    yield
    await catalog_jobs.manager.shutdown(grace=5)
    await catalog_profile_jobs.manager.shutdown(grace=5)


async def until(predicate, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


def _base(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog"


async def _rows(client, sid: str) -> dict[str, dict]:
    r = await client.get(_base(sid))
    assert r.status_code == 200, r.text
    return {row["table_name"]: row for row in r.json()["tables"]}


class Gate:
    """把一个异步函数换成「等放行才执行」：entered 置位说明有调用在等了。"""

    def __init__(self, real, when=lambda *a, **k: True) -> None:
        self.real, self.when = real, when
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, *args, **kwargs):
        if self.when(*args, **kwargs):
            self.entered.set()
            await self.release.wait()
        return await self.real(*args, **kwargs)


def _hold_queries(monkeypatch, when=lambda source, sql, **kw: True) -> Gate:
    gate = Gate(data_engine.run_query, when)
    monkeypatch.setattr(data_engine, "run_query", gate)
    return gate


def _db(path, ddl: str, rows: dict[str, list[tuple]] | None = None) -> str:
    db = sqlite3.connect(path)
    try:
        db.executescript(ddl)
        for table, values in (rows or {}).items():
            marks = ", ".join("?" for _ in values[0])
            db.executemany(f"INSERT INTO {table} VALUES ({marks})", values)
        db.commit()
    finally:
        db.close()
    return str(path)


# --------------------------------------------------------------------------
# 同源排队
# --------------------------------------------------------------------------


async def test_a_profile_job_waits_for_the_draft_job_and_then_starts(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on())
    gate = Gate(catalog.write_draft)
    monkeypatch.setattr(catalog, "write_draft", gate)
    draft = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)

    r = await _start(client, sid, ["channels"])
    assert r.status_code == 202, r.text                          # 不报错：排在起草后面
    job = r.json()["job"]
    assert job["status"] == "queued" and job["waiting_for"] == "draft"
    listed = (await client.get(_base(sid))).json()
    assert listed["active_profile_job"]["waiting_for"] == "draft"
    assert listed["active_job"]["waiting_for"] is None
    await asyncio.sleep(0.1)
    assert (await _job(client, sid, job["id"]))["status"] == "queued", "起草没结束，剖析不开始"

    gate.release.set()
    await catalog_jobs.manager.wait(draft["id"], timeout=10)
    done = await _finished(client, sid, job["id"])
    assert done["status"] == "succeeded" and done["waiting_for"] is None
    draft_done = (await client.get(f"{_base(sid)}/draft-jobs/{draft['id']}")).json()["job"]
    assert done["started_at"] >= draft_done["finished_at"], "起草结束后才开始"


async def test_a_draft_job_queues_behind_a_running_profile_job(client, make_source, scenic_db, monkeypatch):
    """同种任务互斥、不同种排队：剖析在跑时建起草任务不 409，排在后面（waiting_for = "profile"）。"""
    sid = await make_source(scenic_db, options=_on())
    gate = _hold_queries(monkeypatch)
    profile = (await _start(client, sid, ["channels"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)

    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})
    assert r.status_code == 202, r.text
    draft = r.json()["job"]
    assert draft["status"] == "queued" and draft["waiting_for"] == "profile"
    got = (await client.get(f"{_base(sid)}/draft-jobs/{draft['id']}")).json()["job"]
    assert got["waiting_for"] == "profile"
    assert (await client.get(_base(sid))).json()["active_job"]["waiting_for"] == "profile"
    # 同种任务照旧互斥
    again = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})
    assert again.status_code == 409 and again.json()["code"] == "catalog_draft_busy"

    gate.release.set()
    await catalog_profile_jobs.manager.wait(profile["id"], timeout=10)
    await catalog_jobs.manager.wait(draft["id"], timeout=10)
    got = (await client.get(f"{_base(sid)}/draft-jobs/{draft['id']}")).json()["job"]
    profile_done = await _job(client, sid, profile["id"])
    assert got["status"] == "succeeded" and got["started_at"] >= profile_done["finished_at"]


async def test_a_queued_profile_job_can_be_cancelled(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on())
    gate = Gate(catalog.write_draft)
    monkeypatch.setattr(catalog, "write_draft", gate)
    draft = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    job = (await _start(client, sid, ["channels", "visits"])).json()["job"]
    assert job["tasks"][0]["checks_total"] > 0, "排队的表也先列好了检查项"

    r = await client.post(f"{_url(sid)}/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    stopped = r.json()["job"]
    assert stopped["status"] == "cancelled" and stopped["finished_at"] and stopped["queries"] == 0
    assert {t["status"] for t in stopped["tasks"]} == {"cancelled"}
    detail = await _detail(client, sid, job["id"], 0)
    assert {c["state"] for c in detail["checks"]} == {"skipped"}, "结束了的表上不留排着的检查项"
    assert {c["reason"] for c in detail["checks"]} == {"cancelled"} and detail["unfinished"] == len(detail["checks"])
    assert not catalog_profile_jobs.manager.running(job["id"]), "在队里等着的也停掉了"
    again = await client.post(f"{_url(sid)}/{job['id']}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "catalog_profile_finished"

    gate.release.set()
    await catalog_jobs.manager.wait(draft["id"], timeout=10)
    assert (await client.get(f"{_base(sid)}/draft-jobs/{draft['id']}")).json()["job"]["status"] == "succeeded"


async def test_import_and_sync_draft_are_409_while_profiling(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["gates"])
    exported = (await client.get(f"{_base(sid)}/export")).json()
    gate = _hold_queries(monkeypatch)
    job = (await _start(client, sid, ["channels"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)

    # 说法按这次要做的事说下一步：是在导入、起草，不是要开始新的剖析
    r = await client.post(f"{_base(sid)}/import", json={"content": exported})
    assert r.status_code == 409 and r.json()["code"] == "profile_busy", r.text
    assert "正在剖析" in r.json()["detail"] and r.json()["detail"].endswith("请等这次剖析结束（或先停止它）再导入")
    r = await client.post(f"{_base(sid)}/draft", json={"tables": ["gates"]})
    assert r.status_code == 409 and r.json()["code"] == "profile_busy", r.text
    assert r.json()["detail"].endswith("请等这次剖析结束（或先停止它）再起草")
    again = await _start(client, sid, ["gates"])
    assert again.json()["detail"].endswith("请等这次剖析结束（或先停止它）再开始新的剖析")
    # 库里查过没有任务、队却被剖析占着（任务刚好在收尾）时同样按这次要做的事说
    row, holder = SimpleNamespace(name="x"), SimpleNamespace(kind="profile")
    assert str(catalog_jobs._lane_busy(row, holder, "import")).endswith("（或先停止它）再导入")
    assert str(catalog_jobs._lane_busy(row, holder, "sync_draft")).endswith("（或先停止它）再起草")
    assert str(catalog_jobs._lane_busy(row, SimpleNamespace(kind="draft"), "import")).endswith("（或先停止它）再导入")

    gate.release.set()
    await catalog_profile_jobs.manager.wait(job["id"], timeout=10)
    r = await client.post(f"{_base(sid)}/import", json={"content": exported})
    assert r.status_code == 200, r.text


async def test_a_profile_job_created_during_a_sync_write_runs_after_it(client, make_source, scenic_db, monkeypatch):
    """同步起草、导入目录执行期间占着这个源的队：期间新建的剖析任务排在后面，做完才开始。"""
    sid = await make_source(scenic_db, options=_on())
    gate = Gate(catalog.draft_catalog)
    monkeypatch.setattr(catalog, "draft_catalog", gate)
    sync = asyncio.create_task(client.post(f"{_base(sid)}/draft", json={"tables": ["gates"]}))
    await asyncio.wait_for(gate.entered.wait(), 10)
    job = (await _start(client, sid, ["channels"])).json()["job"]
    assert job["status"] == "queued" and job["waiting_for"] == "draft"
    await asyncio.sleep(0.1)
    assert (await _job(client, sid, job["id"]))["status"] == "queued"
    gate.release.set()
    assert (await sync).status_code == 200
    assert (await _finished(client, sid, job["id"]))["status"] == "succeeded"

    importing = Gate(catalog_transfer.run_import)
    monkeypatch.setattr(catalog_transfer, "run_import", importing)
    exported = (await client.get(f"{_base(sid)}/export")).json()
    imported = asyncio.create_task(client.post(f"{_base(sid)}/import", json={"content": exported}))
    await asyncio.wait_for(importing.entered.wait(), 10)
    job = (await _start(client, sid, ["channels"])).json()["job"]
    assert job["status"] == "queued" and job["waiting_for"] is None
    # 导入期间再导入一次：同步写操作一次一个
    busy = await client.post(f"{_base(sid)}/import", json={"content": exported})
    assert busy.status_code == 409, busy.text
    importing.release.set()
    assert (await imported).status_code == 200
    assert (await _finished(client, sid, job["id"]))["status"] == "succeeded"


# --------------------------------------------------------------------------
# 停止、服务重启、重试
# --------------------------------------------------------------------------


def _at_visits_codes(source, sql, **kw) -> bool:
    return "GROUP BY status" in sql and "FROM visits" in sql


async def test_stopping_writes_what_is_done_and_keeps_the_rest(client, make_source, scenic_db, monkeypatch):
    """停在 visits 的码值上：关系都做完了，照常写进目录；码值、日期没做，记停止、目录不动；后面的 orders 记停止。"""
    sid = await make_source(scenic_db, options=_on(), draft=["visits", "channels"])
    gate = _hold_queries(monkeypatch, _at_visits_codes)
    job = (await _start(client, sid, ["channels", "visits", "orders"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    running = await _job(client, sid, job["id"])
    row = next(t for t in running["tasks"] if t["table_name"] == "visits")
    assert row["status"] == "running" and row["current"] == "正在取 visits.status 的取值分布"
    assert row["checks_by_kind"]["relation"] == [4, 4] and row["checks_by_kind"]["codes"] == [0, 1]

    r = await client.post(f"{_url(sid)}/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    stopped = r.json()["job"]
    by = {t["table_name"]: t for t in stopped["tasks"]}
    assert stopped["status"] == "cancelled"
    assert by["channels"]["status"] == "ok"
    assert by["visits"]["status"] == "partial" and by["visits"]["current"] is None
    assert by["visits"]["error"] == "剖析已停止：已做完的 4 项照常写进了数据目录，剩下 2 项没查"
    assert by["orders"]["status"] == "cancelled" and by["orders"]["error"] is None

    detail = await _detail(client, sid, job["id"], 1)
    states = {c["kind"]: (c["state"], c["reason"]) for c in detail["checks"]}
    assert states["codes"] == ("skipped", "cancelled") and states["date"] == ("skipped", "cancelled")
    assert detail["query_log"][-1]["outcome"] == "error" and "没有等这条查询的结果" in detail["query_log"][-1]["detail"]
    notes = (await _notes(client, sid, "visits"))["notes"]
    assert _relation(notes, "gate_id")["status"] == "verified", "做完的项写进了目录"
    assert "status" not in (notes.get("columns") or {}) and "business_date" not in notes, "没做的不动"
    again = await client.post(f"{_url(sid)}/{job['id']}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "catalog_profile_finished"


async def test_shutdown_marks_the_job_interrupted(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    gate = _hold_queries(monkeypatch, _at_visits_codes)
    job = (await _start(client, sid, ["visits", "orders"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    await catalog_profile_jobs.manager.shutdown(grace=5)
    got = await _job(client, sid, job["id"])
    by = {t["table_name"]: t for t in got["tasks"]}
    assert got["status"] == "interrupted" and got["error"] == "服务重启，剖析中断"
    assert by["visits"]["status"] == "partial" and by["visits"]["error"].startswith("服务重启，剖析中断：已做完的 4 项")
    assert by["orders"]["status"] == "cancelled" and by["orders"]["error"] == "服务重启，剖析中断"
    assert _relation((await _notes(client, sid, "visits"))["notes"], "gate_id")["status"] == "verified"


async def test_startup_marks_unfinished_jobs_interrupted_and_does_not_resume(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options=_on())
    checks = [{"kind": "codes", "target": "status", "path": "columns.status.codes", "state": "done", "reason": None,
               "detail": None, "queries": 1},
              {"kind": "date", "target": "visit_time", "path": "business_date", "state": "running", "reason": None,
               "detail": None, "queries": 0}]
    async with SessionLocal() as session:
        job = CatalogProfileJob(source_id=sid, status="running", scope="selected", settings={"enabled": True})
        session.add(job)
        await session.flush()
        session.add(CatalogProfileTask(job_id=job.id, seq=0, table_name="channels", status="ok"))
        session.add(CatalogProfileTask(job_id=job.id, seq=1, table_name="visits", status="running", checks=checks))
        session.add(CatalogProfileTask(job_id=job.id, seq=2, table_name="gates", status="pending"))
        await session.commit()
        job_id = job.id
    assert await catalog_profile_jobs.startup() >= 1
    got = await _job(client, sid, job_id)
    by = {t["table_name"]: t for t in got["tasks"]}
    assert got["status"] == "interrupted" and got["error"] == "服务重启，剖析中断" and got["finished_at"]
    assert by["channels"]["status"] == "ok" and by["channels"]["error"] is None
    assert by["visits"]["status"] == by["gates"]["status"] == "cancelled"
    assert by["gates"]["error"] == "服务重启，剖析中断"
    # 进程没了，visits 没走到写目录那一步：查完的码值也没写进目录，不能还标「完成」
    assert by["visits"]["error"] == "服务重启，剖析中断：已查完的 1 项没有写进数据目录，剩下 1 项没查"
    visits = await _detail(client, sid, job_id, 1)
    assert [(c["state"], c["reason"]) for c in visits["checks"]] == [("skipped", "cancelled"), ("skipped", "cancelled")]
    assert visits["checks"][0]["detail"] == "服务重启，剖析中断，查完了但结论没有写进数据目录"
    assert visits["checks"][1]["detail"] == "服务重启，剖析中断，未检查"
    assert visits["unfinished"] == visits["skipped_count"] == 2 and visits["findings"] == []
    assert not catalog_profile_jobs.manager.running(job_id), "不自动续跑"


async def test_a_job_failing_mid_table_does_not_claim_the_finished_checks(client, make_source, scenic_db,
                                                                          monkeypatch):
    """visits 的 4 条关系核对完、取码值时快照被改动：任务整体失败，visits 没走到写目录那一步。核对完的关系不能还标「完成」：
    记为跳过（error）、写明没写进目录，算没做完；目录里那几条关系原样不动；重试带上 visits。"""
    from app.data.engine import SnapshotTampered

    sid = await make_source(scenic_db, options=_on(), draft=["visits"])

    async def tampered(*_args, **_kwargs):
        raise SnapshotTampered("数据文件与登记的版本不一致，可能被修改过，已拒绝查询")

    monkeypatch.setattr(catalog_profile, "_check_codes", tampered)
    body = await _profile(client, sid, ["visits"])
    visits = body["tables"][0]
    assert body["status"] == "failed" and visits["status"] == "failed"
    relations = [c for c in visits["checks"] if c["kind"] == "relation"]
    assert len(relations) == 4 and all(c["queries"] > 0 for c in relations), "关系确实查过了"
    assert all((c["state"], c["reason"]) == ("skipped", "error") for c in visits["checks"])
    assert relations[0]["detail"] == "数据文件与登记的版本不一致，可能被修改过，已拒绝查询，查完了但结论没有写进数据目录"
    assert visits["error"] == "数据文件与登记的版本不一致，可能被修改过，已拒绝查询：已查完的 4 项没有写进数据目录，剩下 2 项没查"
    assert visits["unfinished"] == 6 and visits["findings"] == [] and visits["query_log"], "发过的查询照样留痕"
    notes = (await _notes(client, sid, "visits"))["notes"]
    assert _relation(notes, "gate_id")["status"] == "proposed" and _relation(notes, "gate_id")["source"] == "name"
    async with SessionLocal() as session:
        failed = await session.get(CatalogProfileJob, body["id"])
        assert await catalog_profile_jobs.retry_tables(session, failed) == ["visits"]


async def test_the_catalog_and_the_table_result_commit_together(client, make_source, scenic_db, monkeypatch):
    """目录和这张表的剖析结果在同一个事务里：记结果那一步出错，目录也没写进去，收尾时照实说「没写进目录」。"""
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    real = catalog_profile_jobs._ProfileRun._save_table

    async def broken(self, session, seq, result):
        await real(self, session, seq, result)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(catalog_profile_jobs._ProfileRun, "_save_table", broken)
    body = await _profile(client, sid, ["visits"])
    visits = body["tables"][0]
    assert body["status"] == "failed" and visits["status"] == "failed"
    assert visits["error"].endswith("已查完的 6 项没有写进数据目录")
    notes = (await _notes(client, sid, "visits"))["notes"]
    assert _relation(notes, "gate_id")["status"] == "proposed", "目录跟着结果一起回滚，没写进去"


async def test_retry_takes_only_unfinished_tables(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(table_max_queries=2), draft=["visits"])
    first = await _profile(client, sid, ["visits", "channels", "nope"])
    by = {t["table_name"]: t["status"] for t in first["tasks"]}
    assert by == {"visits": "partial", "channels": "ok", "nope": "skipped"} and first["status"] == "partial"

    r = await client.patch(f"/api/datasources/{sid}", json={"options": _on()})
    assert r.status_code == 200, r.text
    r = await client.post(f"{_url(sid)}/{first['id']}/retry", headers=ACTOR)
    assert r.status_code == 202, r.text
    retry = r.json()["job"]
    assert retry["retry_of"] == first["id"] and retry["scope"] == "selected"
    assert [t["table_name"] for t in retry["tasks"]] == ["visits"]
    assert retry["settings"]["table_max_queries"] == 40, "设置取数据源当前的"
    done = await _finished(client, sid, retry["id"])
    assert done["status"] == "succeeded"
    r = await client.post(f"{_url(sid)}/{retry['id']}/retry")
    assert r.status_code == 422 and "不需要重试" in r.json()["detail"]

    monkeypatch.setattr(catalog_profile_jobs.manager, "start", lambda job_id: None)     # 排着、不开始
    queued = (await _start(client, sid, ["gates"])).json()["job"]
    r = await client.post(f"{_url(sid)}/{first['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "profile_busy"
    await client.post(f"{_url(sid)}/{queued['id']}/cancel")


# --------------------------------------------------------------------------
# 任务级止损、每张表开始前重读数据源
# --------------------------------------------------------------------------

_MANY = ["visits", "orders", "members", "channels", "ticket_types", "gates", "parks", "stores", "products",
         "payments", "refunds", "reviews"]


async def test_three_failures_in_a_row_stop_the_job(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])

    async def broken(source, sql, *, limits=None):
        raise RuntimeError("connection refused by the server\ndetails follow")

    monkeypatch.setattr(data_engine, "run_query", broken)
    job = await _profile(client, sid, ["visits", "channels", "orders"])
    assert job["status"] == "partial" and job["stop_reason"] == "failed"
    assert job["error"] == "连续 3 条查询失败，已停止剖析：connection refused by the server"
    assert (job["queries"], job["errors"], job["timeouts"]) == (3, 3, 0)
    visits, channels, orders = (_table(job, n) for n in ("visits", "channels", "orders"))
    assert visits["status"] == "partial" and visits["error"] == job["error"]
    assert "failed" in {s["reason"] for s in visits["skipped"]}
    assert channels["status"] == orders["status"] == "cancelled"
    assert channels["error"] == orders["error"] == job["error"] and channels["queries"] == 0


async def test_mostly_timeouts_stop_the_job_as_slow(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on())

    async def slow(source, sql, *, limits=None):
        raise SqlRejected("查询超过 1 秒被中断。请添加 WHERE 条件或 LIMIT 缩小范围。")

    monkeypatch.setattr(data_engine, "run_query", slow)
    job = await _profile(client, sid, _MANY)
    assert job["stop_reason"] == "slow" and job["status"] == "partial"
    assert job["error"] == "数据库响应慢：最近 10 条查询 7 条超时，已停止，避免给业务库加压"
    assert job["timeouts"] == job["queries"] == 7
    assert job["notice"] and job["notice"].startswith("数据库响应慢")
    cancelled = [t for t in job["tasks"] if t["status"] == "cancelled"]
    assert cancelled and all(t["error"] == job["error"] for t in cancelled)


def _runner(monkeypatch, outcomes: list[str]) -> SourceRunner:
    """SourceRunner 接一个假的查询层：按 outcomes 依次超时（t）、失败（e）或成功（o）。"""
    seq = iter(outcomes)

    async def fake(source, sql, *, limits=None):
        step = next(seq)
        if step == "t":
            raise SqlRejected("查询超过 1 秒被中断")
        if step == "e":
            raise RuntimeError("no such table: orders")
        return data_engine.QueryResult(columns=["n"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=1,
                                       sql=sql)

    monkeypatch.setattr(data_engine, "run_query", fake)
    source = SimpleNamespace(id="s", name="shop", kind="sqlite", options={})
    return SourceRunner(source, ProfileSettings(enabled=True), SqlDialect("sqlite"))


async def _feed(runner: SourceRunner, n: int) -> None:
    for _ in range(n):
        try:
            await runner.run("SELECT 1")
        except catalog_profile._Skip:
            pass


async def test_the_slow_notice_clears_when_queries_recover(monkeypatch):
    runner = _runner(monkeypatch, ["t"] * 4 + ["o"] * 3 + ["t"] * 3)
    await _feed(runner, 4)
    assert runner.notice == "数据库响应慢：最近 4 条查询有 4 条超时。超时再多会自动停止，也可以先停止、换个时间再剖析"
    assert runner.stopped is None
    await _feed(runner, 3)
    assert runner.notice is None, "好转后清掉"
    await _feed(runner, 3)
    # 最近 10 条里 7 条超时：停下，之后再发直接停
    assert runner.stopped == "slow" and runner.timeouts == 7 and runner.queries == 10
    assert runner.error == "数据库响应慢：最近 10 条查询 7 条超时，已停止，避免给业务库加压"
    with pytest.raises(catalog_profile._Stop):
        await runner.run("SELECT 1")


async def test_a_success_resets_the_failure_count(monkeypatch):
    runner = _runner(monkeypatch, ["e", "e", "o", "e", "e", "e"])
    await _feed(runner, 5)
    assert runner.stopped is None and runner.errors == 4
    await _feed(runner, 1)
    assert runner.stopped == "failed" and runner.error == "连续 3 条查询失败，已停止剖析：no such table: orders"


@pytest.mark.parametrize("change, said", [({"enabled": False}, "已停用"),
                                          ({"options": _on(enabled=False)}, "已关闭数据剖析")])
async def test_disabling_the_source_stops_before_the_next_table(client, make_source, scenic_db, monkeypatch,
                                                                change, said):
    sid = await make_source(scenic_db, options=_on())
    gate = _hold_queries(monkeypatch, lambda source, sql, **kw: "FROM channels" in sql)
    job = (await _start(client, sid, ["channels", "visits", "orders"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        for key, value in change.items():
            setattr(row, key, value)
        await session.commit()
    gate.release.set()
    done = await _finished(client, sid, job["id"])
    by = {t["table_name"]: t for t in done["tasks"]}
    assert by["channels"]["status"] == "ok", "正在做的那张做完"
    assert by["visits"]["status"] == by["orders"]["status"] == "cancelled"
    assert said in by["visits"]["error"] and by["visits"]["error"] == done["error"]
    assert done["status"] == "partial" and done["stop_reason"] is None


async def test_masks_follow_the_latest_settings_table_by_table(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on())
    gate = _hold_queries(monkeypatch, lambda source, sql, **kw: "FROM orders" in sql)
    job = (await _start(client, sid, ["orders", "channels"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.options = {**_on(), "mask_columns": "channel_type"}
        await session.commit()
    gate.release.set()
    done = await _finished(client, sid, job["id"])
    channels = _table(done, "channels")
    assert {(s["target"], s["reason"]) for s in channels["skipped"]} >= {("channel_type", "masked")}
    assert not any("channel_type" in q["sql"].lower() for q in channels["query_log"])


# --------------------------------------------------------------------------
# 范围
# --------------------------------------------------------------------------


async def test_scopes_needed_all_and_selected(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["visits", "channels", "gates", "suppliers"])
    rows = await _rows(client, sid)
    assert rows["v_daily_visits"]["is_view"] and rows["v_daily_visits"]["profile_checks"] == 0
    assert not rows["v_daily_visits"]["profile_due"]
    assert rows["suppliers"]["profile_checks"] == 0 and not rows["suppliers"]["profile_due"]
    assert (rows["visits"]["profile_due"], rows["visits"]["profile_due_reason"]) == (True, "never")
    assert rows["visits"]["last_profile"] is None and rows["visits"]["profile_checks"] == 6
    expected = [name for name, row in rows.items() if row["profile_checks"] > 0 and not row["is_view"]]

    monkeypatch.setattr(catalog_profile_jobs.manager, "start", lambda job_id: None)     # 只看挑了哪些表
    everything = (await _start(client, sid, scope="all")).json()["job"]
    names = [t["table_name"] for t in everything["tasks"]]
    assert everything["scope"] == "all" and sorted(names) == sorted(expected)
    # 有待核实关系的表在前（剖析首先是来核实它们的），其余按表结构顺序
    async with SessionLocal() as session:
        entries = await catalog.read_catalog(session, sid)
    pending = [catalog_profile.has_pending_relations(entries[n].notes if n in entries else {}) for n in names]
    assert pending == sorted(pending, reverse=True) and pending[0] is True
    await client.post(f"{_url(sid)}/{everything['id']}/cancel")
    needed = (await _start(client, sid)).json()["job"]
    assert needed["scope"] == "needed" and [t["table_name"] for t in needed["tasks"]] == names, "都没剖析过"
    await client.post(f"{_url(sid)}/{needed['id']}/cancel")

    picked = (await _start(client, sid, ["v_daily_visits", "nope", "visits", "VISITS", "suppliers"])).json()["job"]
    tasks = {t["table_name"]: t for t in picked["tasks"]}
    assert picked["scope"] == "selected" and list(tasks) == ["v_daily_visits", "nope", "visits", "suppliers"]
    assert tasks["v_daily_visits"]["status"] == "skipped" and "视图" in tasks["v_daily_visits"]["error"]
    assert tasks["nope"]["status"] == "skipped" and "表结构里没有 nope" in tasks["nope"]["error"]
    assert tasks["suppliers"]["status"] == "skipped" and tasks["suppliers"]["error"].startswith("没有可剖析的项")
    assert tasks["visits"]["status"] == "pending" and tasks["visits"]["checks_total"] == 6
    await client.post(f"{_url(sid)}/{picked['id']}/cancel")


async def test_profile_due_never_unfinished_and_new_relations(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options=_on(table_max_queries=2), draft=["visits", "gates"])
    await _profile(client, sid, ["visits"])
    rows = await _rows(client, sid)
    assert (rows["visits"]["profile_due"], rows["visits"]["profile_due_reason"]) == (True, "unfinished")
    assert rows["visits"]["last_profile"]["status"] == "partial" and rows["visits"]["last_profile"]["unfinished"] >= 1
    assert "paths" not in rows["visits"]["last_profile"]

    r = await client.patch(f"/api/datasources/{sid}", json={"options": _on()})
    assert r.status_code == 200, r.text
    await _profile(client, sid, ["visits", "gates"])
    rows = await _rows(client, sid)
    assert (rows["visits"]["profile_due"], rows["visits"]["profile_due_reason"]) == (False, None)
    assert rows["gates"]["last_profile"]["status"] == "ok" and not rows["gates"]["profile_due"]
    needed = await _start(client, sid, ["visits"])
    assert needed.status_code == 202, "指定了表照样能剖析"
    await catalog_profile_jobs.manager.wait(needed.json()["job"]["id"], timeout=30)

    # 剖析之后目录里冒出了新的待核实关系：这张表又要剖析了
    rel = {"id": catalog.relation_id("gates", ["gate_code"], "channels", ["channel_code"]), "columns": ["gate_code"],
           "to_table": "channels", "to_columns": ["channel_code"], "cardinality": "many_to_one", "coverage": None,
           "source": "name", "status": "proposed"}
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "gates")
        notes = {**entry.notes, "relations": [*entry.notes.get("relations", []), rel]}
        await catalog.write_entry(session, sid, "gates", notes, if_version=entry.version, actor=None)
    rows = await _rows(client, sid)
    assert (rows["gates"]["profile_due"], rows["gates"]["profile_due_reason"]) == (True, "new_relations")
    job = (await _start(client, sid)).json()["job"]
    assert "gates" in [t["table_name"] for t in job["tasks"]] and "visits" not in [t["table_name"] for t in job["tasks"]]
    await catalog_profile_jobs.manager.wait(job["id"], timeout=30)


async def test_profile_due_new_checks_after_a_column_becomes_a_status(client, make_source, scenic_db):
    """上次剖析时没有可剖析的项（指定了表，记为跳过），后来有人把一列记成状态类：多出了码值检查，又要剖析了，原因是
    new_checks；剖析过（这一次估过行数发现是空表）之后不再要。"""
    sid = await make_source(scenic_db, options=_on(), draft=["suppliers"])
    await _profile(client, sid, ["suppliers"])
    rows = await _rows(client, sid)
    assert rows["suppliers"]["last_profile"]["status"] == "skipped"
    assert (rows["suppliers"]["profile_checks"], rows["suppliers"]["profile_due"]) == (0, False)

    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "suppliers")       # 起草没推断出什么时还没有目录
        notes = dict(entry.notes) if entry else {}
        columns = dict(notes.get("columns") or {})
        columns["name"] = {**(columns.get("name") or {}),
                           "measure": {"value": "status", "source": "human", "status": "confirmed"}}
        notes["columns"] = columns
        await catalog.write_entry(session, sid, "suppliers", notes, if_version=entry.version if entry else 0,
                                  actor=None)
    rows = await _rows(client, sid)
    assert rows["suppliers"]["profile_checks"] == 1
    assert (rows["suppliers"]["profile_due"], rows["suppliers"]["profile_due_reason"]) == (True, "new_checks")
    assert "keys" not in rows["suppliers"]["last_profile"]
    job = (await _start(client, sid)).json()["job"]
    assert "suppliers" in [t["table_name"] for t in job["tasks"]], "「需要剖析的表」里有它"
    await client.post(f"{_url(sid)}/{job['id']}/cancel")
    await catalog_profile_jobs.manager.wait(job["id"], timeout=30)
    # 景区库的 suppliers 是空表：这次剖析估过行数就跳过了。空表不算要剖析（不发查询不知道有没有数据），等有了数据选
    # 「全部表」或单独剖析它
    await _profile(client, sid, ["suppliers"])
    rows = await _rows(client, sid)
    last = rows["suppliers"]["last_profile"]
    assert (last["status"], last["error"]) == ("skipped", "空表，没有可剖析的数据")
    assert (rows["suppliers"]["profile_due"], rows["suppliers"]["profile_due_reason"]) == (False, None)


async def test_nothing_to_profile_is_409(client, make_source, tmp_path):
    path = _db(tmp_path / "plain.db", "CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT);",
               {"suppliers": [(1, "甲供应商")]})
    sid = await make_source(path, options=_on())
    r = await _start(client, sid)
    assert r.status_code == 409 and r.json()["code"] == "profile_nothing"
    assert "全部表" in r.json()["detail"] and "指定" in r.json()["detail"]
    r = await _start(client, sid, scope="all")
    assert r.status_code == 409 and r.json()["code"] == "profile_nothing" and "指定" in r.json()["detail"]
    r = await _start(client, sid, [f"t{i}" for i in range(501)])
    assert r.status_code == 422 and "500" in r.json()["detail"]
    assert (await client.get(f"{_url(sid)}/latest")).json()["job"] is None


async def test_an_empty_table_costs_one_query(client, make_source, tmp_path):
    path = _db(tmp_path / "empty.db", "CREATE TABLE orders (id INTEGER PRIMARY KEY, status INTEGER, created_at TEXT);")
    sid = await make_source(path, options=_on())
    job = await _profile(client, sid, ["orders"])
    orders = _table(job, "orders")
    assert orders["status"] == "skipped" and orders["error"] == "空表，没有可剖析的数据"
    assert orders["queries"] == 1 == job["queries"] and orders["query_log"][0]["kind"] == "row_estimate"
    assert {c["reason"] for c in orders["checks"]} == {"no_data"} and orders["unfinished"] == 0
    assert job["status"] == "succeeded", "空表不算没做完"


# --------------------------------------------------------------------------
# 留痕、单表接口
# --------------------------------------------------------------------------


async def test_sampled_keys_never_reach_the_query_log(client, make_source, tmp_path):
    """抽样到的键值拼进了 IN 列表，留痕里换成「?」：AgentLab 自己的库里不留这些值。"""
    customers = [(f"CUST-{i:04d}", "active" if i % 3 else "closed") for i in range(1, 31)]
    orders = [(i, f"CUST-{1 + i % 30:04d}", "paid") for i in range(1, 61)]
    path = _db(tmp_path / "shop.db",
               "CREATE TABLE customers (code TEXT PRIMARY KEY, status TEXT);"
               "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_code TEXT, status TEXT);",
               {"customers": customers, "orders": orders})
    sid = await make_source(path, options=_on())
    detail = await _notes(client, sid, "orders")
    rel = {"columns": ["customer_code"], "to_table": "customers", "to_columns": ["code"]}
    r = await client.post(f"{_base(sid)}/orders/patch",
                          json={"changes": [{"path": "relations.new", "value": rel, "reason": "订单的客户编码"}],
                                "if_version": detail["version"]})
    assert r.status_code == 200, r.text

    job = await _profile(client, sid, ["orders"])
    orders_task = _table(job, "orders")
    matched = [q for q in orders_task["query_log"] if " IN (" in q["sql"]]
    assert matched and all(re.search(r"IN \((\?(, )?)+\)", q["sql"]) for q in matched), matched
    finding = next(f for f in orders_task["findings"] if f["kind"] == "relation")
    assert finding["sample"] == 30 and finding["coverage"] == 1.0
    async with SessionLocal() as session:
        stored = (await session.execute(select(CatalogProfileTask.query_log, CatalogProfileTask.findings)
                                        .where(CatalogProfileTask.job_id == job["id"]))).all()
    assert stored and "CUST-" not in json.dumps([row[0] for row in stored], ensure_ascii=False)


async def test_the_table_detail_and_list_carry_the_last_profile(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    gate = _hold_queries(monkeypatch, _at_visits_codes)
    job = (await _start(client, sid, ["visits"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    detail = await _notes(client, sid, "visits")
    assert detail["last_profile"]["status"] == "running" and detail["last_profile"]["job_id"] == job["id"]
    assert (await _rows(client, sid))["visits"]["last_profile"]["status"] == "running"
    assert not (await _rows(client, sid))["visits"]["profile_due"], "正在剖析的不算要剖析"
    gate.release.set()
    await catalog_profile_jobs.manager.wait(job["id"], timeout=30)

    detail = await _notes(client, sid, "visits")
    last = detail["last_profile"]
    assert last["status"] == "ok" and last["created_by"] == "周宁" and last["job_status"] == "succeeded"
    assert last["query_log"] and last["findings"] and last["seq"] == 0
    row = (await _rows(client, sid))["visits"]["last_profile"]
    assert set(row) == {"job_id", "seq", "status", "started_at", "finished_at", "summary", "skipped_count",
                        "unfinished", "error", "row_estimate"}
    assert row["summary"] == last["summary"] and row["row_estimate"] == {"rows": 1500, "method": "count",
                                                                        "at_least": None}
    assert (await _notes(client, sid, "gates"))["last_profile"] is None


async def test_the_last_profile_is_the_last_one_that_actually_ran(client, make_source, scenic_db, monkeypatch):
    """channels 剖析完（ok）；再建 [visits, channels]、停在 visits 上：channels 没开始就停了。表详情、表清单的「上次剖析」
    仍是上一次真正剖析的那一项（行数、发过的查询都在），不算没做完；保留不删它所在的任务，之后再建一个排队的任务也不删。"""
    sid = await make_source(scenic_db, options=_on(), draft=["visits", "channels"])
    first = await _profile(client, sid, ["channels"])
    gate = _hold_queries(monkeypatch, _at_visits_codes)
    second = (await _start(client, sid, ["visits", "channels"])).json()["job"]
    await asyncio.wait_for(gate.entered.wait(), 10)
    stopped = (await client.post(f"{_url(sid)}/{second['id']}/cancel")).json()["job"]
    assert [(t["table_name"], t["status"]) for t in stopped["tasks"]] == [("visits", "partial"),
                                                                          ("channels", "cancelled")]

    last = (await _notes(client, sid, "channels"))["last_profile"]
    assert (last["job_id"], last["status"]) == (first["id"], "ok")
    assert last["started_at"] and last["queries"] and last["row_estimate"] and last["query_log"]
    rows = await _rows(client, sid)
    assert rows["channels"]["last_profile"]["job_id"] == first["id"] and not rows["channels"]["profile_due"]
    assert rows["visits"]["last_profile"]["job_id"] == second["id"], "visits 真正剖析过：上次剖析是停下的这一次"
    async with SessionLocal() as session:
        assert await catalog_profile_jobs.prune(session, sid, keep=1) == 0, "first 是 channels 上次剖析所在的任务"

    # 结束了的任务超出保留数时再建一个排队的任务（建任务时顺手清）：排着的那一项不算 channels 的上次剖析
    monkeypatch.setattr(catalog_profile_jobs, "KEEP_JOBS", 0)
    monkeypatch.setattr(catalog_profile_jobs.manager, "start", lambda job_id: None)
    queued = (await _start(client, sid, ["channels"])).json()["job"]
    async with SessionLocal() as session:
        left = set((await session.execute(select(CatalogProfileJob.id)
                                          .where(CatalogProfileJob.source_id == sid))).scalars())
    assert {first["id"], second["id"], queued["id"]} == left
    assert (await _notes(client, sid, "channels"))["last_profile"]["job_id"] == first["id"]
    row = (await _rows(client, sid))["channels"]
    assert row["last_profile"]["job_id"] == first["id"] and not row["profile_due"], "已经排进任务里了"
    await client.post(f"{_url(sid)}/{queued['id']}/cancel")


# --------------------------------------------------------------------------
# 设置迁移、保留
# --------------------------------------------------------------------------


async def test_old_whole_run_budgets_are_removed_on_startup_and_on_save(client, make_source, scenic_db):
    sid = await make_source(scenic_db, options={PROFILE_OPTION: {"enabled": True, "max_queries": 60,
                                                                 "max_total_s": 120, "sample_size": 500},
                                                "query_timeout_s": "20"})
    assert await catalog_profile_jobs.startup() >= 0
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        assert row.options == {PROFILE_OPTION: {"enabled": True, "sample_size": 500}, "query_timeout_s": "20"}
    # 老页面还带着旧键：只摘掉，不报错
    r = await client.patch(f"/api/datasources/{sid}",
                           json={"options": _on(max_queries=30, max_total_s=90, table_max_queries=25)})
    assert r.status_code == 200, r.text
    assert r.json()["options"][PROFILE_OPTION] == {"enabled": True, "table_max_queries": 25}


async def test_retention_keeps_recent_jobs_and_each_tables_last_profile(client, make_source, scenic_db, monkeypatch):
    sid = await make_source(scenic_db, options=_on())
    start = utcnow() - timedelta(hours=1)
    ids: list[str] = []
    async with SessionLocal() as session:
        for i in range(5):
            job = CatalogProfileJob(id=uuid.uuid4().hex, source_id=sid, status="succeeded", scope="selected",
                                    settings={"enabled": True}, created_at=start + timedelta(minutes=i))
            session.add(job)
            await session.flush()
            session.add(CatalogProfileTask(job_id=job.id, seq=0, table_name="visits", status="ok"))
            if i == 0:
                session.add(CatalogProfileTask(job_id=job.id, seq=1, table_name="gates", status="ok"))
            ids.append(job.id)
        await session.commit()
    async with SessionLocal() as session:
        assert await catalog_profile_jobs.prune(session, sid, keep=2) == 2
        left = set((await session.execute(select(CatalogProfileJob.id)
                                          .where(CatalogProfileJob.source_id == sid))).scalars())
    assert left == {ids[4], ids[3], ids[0]}, "最早那个是 gates 最近一次剖析所在的任务，不删"
    assert (await _notes(client, sid, "gates"))["last_profile"]["job_id"] == ids[0]

    # 建任务时顺手清
    monkeypatch.setattr(catalog_profile_jobs, "KEEP_JOBS", 1)
    job = await _profile(client, sid, ["visits"])
    async with SessionLocal() as session:
        left = set((await session.execute(select(CatalogProfileJob.id)
                                          .where(CatalogProfileJob.source_id == sid))).scalars())
    assert ids[0] in left and job["id"] in left and ids[3] not in left


async def test_cancel_a_job_claimed_between_load_and_cancel(client, make_source, scenic_db, monkeypatch):
    """停止时读到的是排队中，可在停之前后台刚认领了它：排队的那一步没停成、回滚，接着按进行中的停。回滚让读到的 job
    属性全部过期，以前再读 job.id 会在异步会话里同步加载（MissingGreenlet），接口 500。起草任务同一个写法，一起钉住。"""
    sid = await make_source(scenic_db, options=_on(), draft=["visits"])
    stopped: list[str] = []

    async def fake_stop(job_id, reason, *, grace=0):
        stopped.append(job_id)
        return True

    for module, model in ((catalog_profile_jobs, CatalogProfileJob), (catalog_jobs, None)):
        monkeypatch.setattr(module.manager, "start", lambda job_id: None)       # 不在后台跑：认领由下面手工做
        monkeypatch.setattr(module.manager, "stop", fake_stop)
    job_id = (await _start(client, sid, ["visits"])).json()["job"]["id"]
    async with SessionLocal() as session:
        job = await session.get(CatalogProfileJob, job_id)
        assert job.status == "queued"
        async with SessionLocal() as other:                                    # 后台刚认领
            await other.execute(catalog_profile_jobs.update(CatalogProfileJob).where(CatalogProfileJob.id == job_id)
                                .values(status="running"))
            await other.commit()
        await catalog_profile_jobs.cancel(session, job)
    assert stopped == [job_id]

    from app.db.models import CatalogDraftJob

    r = await client.post(f"/api/datasources/{sid}/catalog/draft-jobs", json={"tables": ["visits"], "use_model": False})
    assert r.status_code == 202, r.text
    draft_id = r.json()["job"]["id"]
    async with SessionLocal() as session:
        job = await session.get(CatalogDraftJob, draft_id)
        assert job.status == "queued"
        async with SessionLocal() as other:
            await other.execute(catalog_profile_jobs.update(CatalogDraftJob).where(CatalogDraftJob.id == draft_id)
                                .values(status="running"))
            await other.commit()
        await catalog_jobs.cancel(session, job)
    assert stopped == [job_id, draft_id]
