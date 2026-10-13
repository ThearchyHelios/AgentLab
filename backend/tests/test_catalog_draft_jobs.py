"""数据目录的后台起草任务（data/catalog_jobs.py、/api/datasources/{id}/catalog/draft-jobs）与起草要求。

- 建任务 202；同一个源已有排队或进行中的任务 409 catalog_draft_busy；起草模型用不了 409
  catalog_draft_model_unavailable（不建任务）；没有表结构 409；表结构里没有的表照样建进任务、状态 skipped
- 在后台起草，每张表一结束就并入目录；进度、计数、用量落库，接口读的是库里的状态
- 停止：排队的直接 cancelled；进行中的停掉正在进行的模型调用，没写完的表记 cancelled，已经写进目录的不回滚；已结束的 409
- 服务启动时把没结束的任务标成 interrupted（没结束的表记 cancelled、写明服务重启），不自动续跑；关停时同样
- 重试：只含原任务里 failed、partial、cancelled 的表，本次说明沿用、起草要求取数据源当前的；没有可重试的 422
- 目录清单：每行 last_draft，顶层 guidance、active_job
- 起草要求存在数据源 options 里（上传表格的源存在它自己那一行），数据源接口不显示、整份回写 options 时留着，拼连接串时摘掉

表名、列名、说明文字都是虚构的。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data import catalog, catalog_jobs
from app.data.engine import CATALOG_DRAFT_GUIDANCE_OPTION, build_url
from app.db.base import SessionLocal
from app.db.models import CatalogDraftJob, CatalogDraftTask, DataSource
from app.main import app as _fastapi_app
from tests.fixtures.catalog.fake_model import FakeModel, requested
from tests.fixtures.sources import drop_source

ACTOR = {"X-Actor": quote("王敏")}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _col(name: str, type_: str = "INTEGER", comment: str | None = None) -> dict:
    return {"name": name, "type": type_, "nullable": True, "comment": comment}


def _tables() -> dict:
    return {
        "visits": {"qualified": "visits", "columns": [_col("id"), _col("ticket_no", "TEXT"), _col("visit_time", "TEXT"),
                                                      _col("channel_id")],
                   "primary_key": ["id"], "comment": None, "is_view": False,
                   "foreign_keys": [{"columns": ["channel_id"], "to_table": "channels", "to_columns": ["id"]}]},
        "channels": {"qualified": "channels", "columns": [_col("id"), _col("name", "TEXT")], "primary_key": ["id"],
                     "comment": "售票渠道", "is_view": False, "foreign_keys": []},
        "gates": {"qualified": "gates", "columns": [_col("id"), _col("gate_code", "TEXT")], "primary_key": ["id"],
                  "comment": None, "is_view": False, "foreign_keys": []},
    }


REPLIES = {
    "visits": {"label": "入园记录", "kind": "fact", "columns": [{"name": "ticket_no", "label": "票号"}]},
    "channels": {"label": "渠道", "kind": "dimension"},
    "gates": {"label": "闸口", "kind": "dimension"},
}

_CREATED: list[str] = []


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    # 后台任务先收完（停不下来的停掉），再删源：删源会级联删掉任务
    await catalog_jobs.manager.shutdown(grace=5)
    while _CREATED:
        await drop_source(_CREATED.pop())


async def _source(tables: dict | None = None, **kw) -> str:
    async with SessionLocal() as session:
        row = DataSource(name=f"cdj_{uuid.uuid4().hex[:10]}", kind="sqlite", database=":memory:",
                         schema_cache={"tables": tables if tables is not None else _tables()}, **kw)
        session.add(row)
        await session.commit()
        _CREATED.append(row.id)
        return row.id


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


def _base(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog"


def use(monkeypatch, model) -> None:
    """起草模型换成假模型（建任务时查一次能不能用，开始起草时再取一次）。"""
    async def resolve(session):
        return model, "fake-model"

    monkeypatch.setattr(catalog, "resolve_draft_model", resolve)


async def finished(job_id: str) -> None:
    await catalog_jobs.manager.wait(job_id, timeout=10)


async def get_job(client, sid: str, job_id: str) -> dict:
    r = await client.get(f"{_base(sid)}/draft-jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["job"]


class Held:
    """假模型：请求里有 hold 里的表时，等 release 才回；被停止时记下这次调用被取消了。"""

    def __init__(self, replies: dict, hold: set[str]) -> None:
        self.inner, self.hold = FakeModel(replies), set(hold)
        self.release = asyncio.Event()
        self.cancelled = 0
        self.model_name = "held-model"

    def with_structured_output(self, schema, **_):
        outer = self

        class _S:
            async def ainvoke(self, messages):
                text = "\n".join(str(m.content) for m in messages)
                if outer.hold & set(requested(text)):
                    try:
                        await outer.release.wait()
                    except asyncio.CancelledError:
                        outer.cancelled += 1
                        raise
                return outer.inner._reply(messages, "structured")

        return _S()

    async def ainvoke(self, messages):
        return await self.inner.ainvoke(messages)


async def until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


# --------------------------------------------------------------------------
# 建任务、在后台起草
# --------------------------------------------------------------------------


async def test_a_job_drafts_in_the_background_and_writes_each_table(client, monkeypatch):
    sid = await _source()
    model = FakeModel(REPLIES)
    use(monkeypatch, model)
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "金额一律按元写单位"})
    r = await client.post(f"{_base(sid)}/draft-jobs", headers=ACTOR,
                          json={"tables": ["visits", "nope", "channels"], "use_model": True, "note": "  看渠道  "})
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["status"] in ("queued", "running") and job["source_id"] == sid
    assert job["use_model"] is True and job["guidance"] == "金额一律按元写单位" and job["note"] == "看渠道"
    assert job["created_by"] == "王敏" and job["total"] == 3
    assert [t["table_name"] for t in job["tasks"]] == ["visits", "nope", "channels"], "按起草顺序"
    await finished(job["id"])

    job = await get_job(client, sid, job["id"])
    assert job["status"] == "succeeded" and job["error"] is None and job["model"] == "fake-model"
    assert job["done"] == 3 and job["counts"] == {"pending": 0, "running": 0, "ok": 2, "partial": 0, "failed": 0,
                                                  "skipped": 1, "cancelled": 0}
    assert job["started_at"] and job["finished_at"] and job["usage"]["calls"] >= 1
    by = {t["table_name"]: t for t in job["tasks"]}
    assert by["nope"]["status"] == "skipped" and by["nope"]["error"] == "表结构里没有 nope，请先重新探查结构"
    visits = by["visits"]
    assert visits["status"] == "ok" and visits["parts_total"] == 1 and visits["parts_done"] == 1
    assert visits["model_items"] == 3 and visits["added"] == 4 and visits["version"] == 1 and visits["columns"] == 4
    assert visits["started_at"] and visits["finished_at"] and visits["error"] is None
    assert job["added"] == sum(t["added"] for t in job["tasks"])
    # 起草要求和本次说明都交给了模型
    assert "起草要求：金额一律按元写单位" in model.calls[0] and "本次说明：看渠道" in model.calls[0]

    detail = (await client.get(f"{_base(sid)}/visits")).json()
    assert detail["notes"]["label"]["value"] == "入园记录" and detail["updated_by"] == "王敏"


async def test_a_job_without_the_model_writes_structure_only(client):
    sid = await _source()
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": [], "use_model": False})
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["total"] == 3, "不给表时按使用次数取前 20 张"
    await finished(job["id"])
    job = await get_job(client, sid, job["id"])
    assert job["status"] == "succeeded" and job["model"] is None and job["usage"]["calls"] == 0
    assert all(t["parts_total"] is None and t["model_items"] is None for t in job["tasks"])
    assert {t["table_name"]: t["added"] for t in job["tasks"]}["visits"] == 1          # 外键一条


async def test_a_table_whose_model_part_failed_is_failed_but_structure_is_written(client, monkeypatch):
    sid = await _source()
    use(monkeypatch, FakeModel(REPLIES, fail={"visits"}))
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits", "channels"],
                                                                "use_model": True})).json()["job"]
    await finished(job["id"])
    job = await get_job(client, sid, job["id"])
    by = {t["table_name"]: t for t in job["tasks"]}
    assert job["status"] == "partial"
    assert by["visits"]["status"] == "failed" and "网关超时" in by["visits"]["model_error"]
    assert by["visits"]["error"] is None and by["visits"]["added"] == 1, "外键照常写入"
    assert by["channels"]["status"] == "ok"


async def test_a_job_that_cannot_start_is_failed_with_the_reason(client, monkeypatch):
    """建任务时起草模型还能用，开始起草时已经用不了（接入刚被停用）：任务整体 failed，error 写原因，没开始的表也记 failed。"""
    sid = await _source()
    calls: list[int] = []

    async def flaky(session):
        calls.append(1)
        if len(calls) > 1:
            raise catalog.CatalogModelUnavailable("起草模型使用的模型接入「虚构接入」已停用")
        return FakeModel(REPLIES), "fake-model"

    monkeypatch.setattr(catalog, "resolve_draft_model", flaky)
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits", "gates"],
                                                                "use_model": True})).json()["job"]
    await finished(job["id"])
    got = await get_job(client, sid, job["id"])
    assert got["status"] == "failed" and got["error"] == "起草模型使用的模型接入「虚构接入」已停用"
    assert {t["status"] for t in got["tasks"]} == {"failed"} and got["finished_at"]
    assert all(t["error"] == got["error"] for t in got["tasks"])
    retry = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/retry")
    assert retry.status_code == 409 and retry.json()["code"] == "catalog_draft_model_unavailable"


async def test_busy_source_is_409_and_the_active_job_is_reachable(client, monkeypatch):
    sid = await _source()
    model = Held(REPLIES, hold={"visits"})
    use(monkeypatch, model)
    first = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits"], "use_model": True})).json()
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"], "use_model": False})
    assert r.status_code == 409 and r.json()["code"] == "catalog_draft_busy", r.text
    active = (await client.get(f"{_base(sid)}/draft-jobs/active")).json()["job"]
    assert active["id"] == first["job"]["id"]
    listed = (await client.get(_base(sid))).json()
    assert listed["active_job"]["id"] == first["job"]["id"] and listed["active_job"]["total"] == 1
    assert listed["active_job"]["status"] in ("queued", "running") and listed["active_job"]["done"] == 0
    model.release.set()
    await finished(first["job"]["id"])
    assert (await client.get(f"{_base(sid)}/draft-jobs/active")).json()["job"] is None
    latest = (await client.get(f"{_base(sid)}/draft-jobs/latest")).json()["job"]
    assert latest["id"] == first["job"]["id"] and latest["status"] == "succeeded"
    assert (await client.get(_base(sid))).json()["active_job"] is None


async def test_unavailable_model_is_409_and_no_job_is_created(client, monkeypatch):
    sid = await _source()

    async def unavailable(session):
        raise catalog.CatalogModelUnavailable("未配置模型接入，无法由模型起草数据目录。请到「设置 → 模型接入」添加")

    monkeypatch.setattr(catalog, "resolve_draft_model", unavailable)
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"use_model": True})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "catalog_draft_model_unavailable" and "模型接入" in r.json()["detail"]
    assert (await client.get(f"{_base(sid)}/draft-jobs/latest")).json()["job"] is None


async def test_without_schema_or_with_bad_input_nothing_is_created(client):
    sid = await _source(tables={})
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"use_model": False})
    assert r.status_code == 409 and "探查结构" in r.json()["detail"]
    sid = await _source()
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"use_model": False, "note": "说" * 1001})
    assert r.status_code == 422 and "1000" in r.json()["detail"]
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": [f"t{i}" for i in range(501)]})
    assert r.status_code == 422 and "500" in r.json()["detail"]
    assert (await client.get(f"{_base(sid)}/draft-jobs/latest")).json()["job"] is None
    assert (await client.get(f"{_base('missing-source')}/draft-jobs/latest")).status_code == 404
    assert (await client.get(f"{_base(sid)}/draft-jobs/not-a-job")).status_code == 404


async def test_a_job_of_another_source_is_404(client):
    a, b = await _source(), await _source()
    job = (await client.post(f"{_base(a)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    await finished(job["id"])
    assert (await client.get(f"{_base(b)}/draft-jobs/{job['id']}")).status_code == 404
    assert (await client.post(f"{_base(b)}/draft-jobs/{job['id']}/cancel")).status_code == 404
    assert (await client.post(f"{_base(b)}/draft-jobs/{job['id']}/retry")).status_code == 404


# --------------------------------------------------------------------------
# 停止、服务重启
# --------------------------------------------------------------------------


async def test_cancelling_a_queued_job(client, monkeypatch):
    sid = await _source()
    monkeypatch.setattr(catalog_jobs.manager, "start", lambda job_id: None)     # 不让它开始
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates", "channels"]})).json()["job"]
    assert job["status"] == "queued"
    r = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "cancelled" and job["finished_at"]
    assert {t["status"] for t in job["tasks"]} == {"cancelled"}
    again = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "catalog_draft_finished"


async def test_cancelling_a_running_job_stops_the_call_and_keeps_what_is_written(client, monkeypatch):
    sid = await _source()
    model = Held(REPLIES, hold={"visits"})
    use(monkeypatch, model)
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    job = (await client.post(f"{_base(sid)}/draft-jobs",
                             json={"tables": ["channels", "visits"], "use_model": True})).json()["job"]

    async def channels_written() -> bool:
        tasks = {t["table_name"]: t for t in (await get_job(client, sid, job["id"]))["tasks"]}
        return tasks["channels"]["status"] == "ok" and tasks["visits"]["status"] == "running"

    await until(channels_written)
    r = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/cancel")
    assert r.status_code == 200, r.text
    after = r.json()["job"]
    by = {t["table_name"]: t for t in after["tasks"]}
    assert after["status"] == "cancelled" and after["finished_at"]
    assert by["channels"]["status"] == "ok" and by["visits"]["status"] == "cancelled"
    assert model.cancelled == 1, "正在进行的模型调用被停掉"
    detail = (await client.get(f"{_base(sid)}/channels")).json()
    assert detail["notes"]["kind"]["value"] == "dimension", "已经写进目录的不回滚"
    assert (await client.get(f"{_base(sid)}/draft-jobs/active")).json()["job"] is None


async def test_startup_marks_unfinished_jobs_interrupted_and_does_not_resume(client):
    sid = await _source()
    async with SessionLocal() as session:
        job = CatalogDraftJob(source_id=sid, status="running", use_model=True)
        session.add(job)
        await session.flush()
        for seq, (name, status) in enumerate([("visits", "ok"), ("channels", "running"), ("gates", "pending")]):
            session.add(CatalogDraftTask(job_id=job.id, seq=seq, table_name=name, status=status))
        await session.commit()
        job_id = job.id
    assert await catalog_jobs.startup() >= 1
    got = await get_job(client, sid, job_id)
    by = {t["table_name"]: t for t in got["tasks"]}
    assert got["status"] == "interrupted" and got["error"] == "服务重启，起草中断" and got["finished_at"]
    assert by["visits"]["status"] == "ok" and by["visits"]["error"] is None
    assert by["channels"]["status"] == by["gates"]["status"] == "cancelled"
    assert by["channels"]["error"] == by["gates"]["error"] == "服务重启，起草中断"
    assert not catalog_jobs.manager.running(job_id), "不自动续跑"


async def test_shutdown_stops_running_jobs_as_interrupted(client, monkeypatch):
    sid = await _source()
    model = Held(REPLIES, hold={"visits"})
    use(monkeypatch, model)
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits"], "use_model": True})).json()["job"]

    async def running() -> bool:
        return (await get_job(client, sid, job["id"]))["tasks"][0]["status"] == "running"

    await until(running)
    await catalog_jobs.manager.shutdown(grace=5)
    got = await get_job(client, sid, job["id"])
    assert got["status"] == "interrupted" and got["tasks"][0]["status"] == "cancelled"
    assert got["tasks"][0]["error"] == "服务重启，起草中断"


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------


async def test_retry_takes_only_failed_partial_and_cancelled_tables(client, monkeypatch):
    sid = await _source()
    use(monkeypatch, FakeModel(REPLIES, fail={"visits"}))
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "旧的要求"})
    first = (await client.post(f"{_base(sid)}/draft-jobs", json={
        "tables": ["visits", "channels", "nope"], "use_model": True, "note": "第一次"})).json()["job"]
    await finished(first["id"])
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "新的要求"})
    assert (await get_job(client, sid, first["id"]))["guidance"] == "旧的要求", "任务记下的是当时的快照"

    use(monkeypatch, FakeModel(REPLIES))
    r = await client.post(f"{_base(sid)}/draft-jobs/{first['id']}/retry", headers=ACTOR)
    assert r.status_code == 202, r.text
    retry = r.json()["job"]
    assert [t["table_name"] for t in retry["tasks"]] == ["visits"]
    assert retry["use_model"] is True and retry["note"] == "第一次" and retry["guidance"] == "新的要求"
    assert retry["retry_of"] == first["id"] and retry["created_by"] == "王敏"
    await finished(retry["id"])
    assert (await get_job(client, sid, retry["id"]))["status"] == "succeeded"

    nothing = await client.post(f"{_base(sid)}/draft-jobs/{retry['id']}/retry")
    assert nothing.status_code == 422 and "没有" in nothing.json()["detail"]


async def test_retry_while_busy_is_409(client, monkeypatch):
    sid = await _source()
    monkeypatch.setattr(catalog_jobs.manager, "start", lambda job_id: None)
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/cancel")
    other = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["channels"]})).json()["job"]
    assert other["status"] == "queued"
    r = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "catalog_draft_busy"


async def test_retry_with_an_unavailable_model_is_409(client, monkeypatch):
    sid = await _source()
    monkeypatch.setattr(catalog_jobs.manager, "start", lambda job_id: None)
    use(monkeypatch, FakeModel(REPLIES))
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"], "use_model": True})).json()["job"]
    await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/cancel")

    async def unavailable(session):
        raise catalog.CatalogModelUnavailable("当前只有演示用的模型接入，无法用于起草数据目录")

    monkeypatch.setattr(catalog, "resolve_draft_model", unavailable)
    r = await client.post(f"{_base(sid)}/draft-jobs/{job['id']}/retry")
    assert r.status_code == 409 and r.json()["code"] == "catalog_draft_model_unavailable"


# --------------------------------------------------------------------------
# 目录清单上的起草状态
# --------------------------------------------------------------------------


async def test_the_list_carries_last_draft_per_table(client, monkeypatch):
    sid = await _source()
    use(monkeypatch, FakeModel(REPLIES, fail={"visits"}))
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    first = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits", "channels"],
                                                                  "use_model": True})).json()["job"]
    await finished(first["id"])
    second = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["channels"]})).json()["job"]
    await finished(second["id"])
    body = (await client.get(_base(sid))).json()
    rows = {r["table_name"]: r for r in body["tables"]}
    assert rows["gates"]["last_draft"] is None
    visits = rows["visits"]["last_draft"]
    assert visits["status"] == "failed" and visits["job_id"] == first["id"] and visits["finished_at"]
    assert "网关超时" in visits["model_error"] and visits["error"] is None
    assert rows["channels"]["last_draft"]["job_id"] == second["id"], "取最近一次任务里的那一项"
    assert body["guidance"] is None and body["active_job"] is None


# --------------------------------------------------------------------------
# 起草要求
# --------------------------------------------------------------------------


async def test_guidance_is_saved_trimmed_cleared_and_limited(client):
    sid = await _source()
    r = await client.put(f"{_base(sid)}/guidance", json={"guidance": "  票务库，金额按元。\n状态 9 是作废  "})
    assert r.status_code == 200, r.text
    assert r.json() == {"guidance": "票务库，金额按元。\n状态 9 是作废"}
    assert (await client.get(_base(sid))).json()["guidance"] == "票务库，金额按元。\n状态 9 是作废"
    too_long = await client.put(f"{_base(sid)}/guidance", json={"guidance": "字" * 2001})
    assert too_long.status_code == 422 and "2000" in too_long.json()["detail"]
    assert (await client.get(_base(sid))).json()["guidance"] == "票务库，金额按元。\n状态 9 是作废"
    assert (await client.put(f"{_base(sid)}/guidance", json={"guidance": "字" * 2000})).status_code == 200
    cleared = await client.put(f"{_base(sid)}/guidance", json={"guidance": "   "})
    assert cleared.json() == {"guidance": None}
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        assert CATALOG_DRAFT_GUIDANCE_OPTION not in (row.options or {})
    assert (await client.put(f"{_base('missing-source')}/guidance", json={"guidance": "x"})).status_code == 404


async def test_a_table_named_guidance_can_still_be_saved(client):
    """起草要求的路径和「整份提交一张表」撞名：表名恰好叫 guidance 时，{notes, if_version} 照旧是那张表的提交，
    不会被当成清除起草要求；两样都没写的 422。"""
    tables = _tables()
    tables["guidance"] = {"qualified": "guidance", "columns": [_col("id"), _col("body", "TEXT")],
                          "primary_key": ["id"], "comment": None, "is_view": False, "foreign_keys": []}
    sid = await _source(tables)
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "金额按元"})
    r = await client.put(f"{_base(sid)}/guidance", json={"notes": {"label": {"value": "操作指引"}}, "if_version": 0},
                         headers=ACTOR)
    assert r.status_code == 200, r.text
    assert r.json()["table_name"] == "guidance" and r.json()["notes"]["label"]["value"] == "操作指引"
    assert (await client.get(_base(sid))).json()["guidance"] == "金额按元"
    assert (await client.put(f"{_base(sid)}/guidance", json={})).status_code == 422


async def test_guidance_of_an_uploaded_source_lives_on_its_own_row(client):
    sid = await _source(origin="upload")
    r = await client.put(f"{_base(sid)}/guidance", json={"guidance": "按期上传的客流日报"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        assert row.options[CATALOG_DRAFT_GUIDANCE_OPTION] == "按期上传的客流日报"


async def test_the_datasource_form_neither_shows_nor_drops_the_guidance(client):
    sid = await _source(options={"schema": "main"})
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "金额按元"})
    shown = (await client.get("/api/datasources")).json()
    mine = next(s for s in shown if s["id"] == sid)
    assert CATALOG_DRAFT_GUIDANCE_OPTION not in mine["options"] and mine["options"]["schema"] == "main"
    # 表单整份回写 options（不带起草要求，或者带着一份别的）：起草要求留着，只认 PUT /catalog/guidance
    r = await client.patch(f"/api/datasources/{sid}", json={"options": {"schema": "other",
                                                                         CATALOG_DRAFT_GUIDANCE_OPTION: "篡改"}})
    assert r.status_code == 200, r.text
    assert CATALOG_DRAFT_GUIDANCE_OPTION not in r.json()["options"]
    assert (await client.get(_base(sid))).json()["guidance"] == "金额按元"
    async with SessionLocal() as session:
        assert (await session.get(DataSource, sid)).options == {"schema": "other",
                                                                CATALOG_DRAFT_GUIDANCE_OPTION: "金额按元"}


def test_the_guidance_never_reaches_the_driver():
    src = SimpleNamespace(kind="mysql", host="db.example", port=3306, database="shop", username="reader",
                          password=None, options={"charset": "utf8mb4", CATALOG_DRAFT_GUIDANCE_OPTION: "金额按元"})
    url = build_url(src)
    assert "charset=utf8mb4" in url and CATALOG_DRAFT_GUIDANCE_OPTION not in url


async def test_the_sync_draft_uses_the_source_guidance(client, monkeypatch):
    sid = await _source(description="虚构的景区票务库")
    model = FakeModel(REPLIES)
    use(monkeypatch, model)
    await client.put(f"{_base(sid)}/guidance", json={"guidance": "渠道表的名称是对外展示名"})
    r = await client.post(f"{_base(sid)}/draft", json={"tables": ["channels"], "use_model": True})
    assert r.status_code == 200, r.text
    assert "数据源说明：虚构的景区票务库\n起草要求：渠道表的名称是对外展示名" in model.calls[0]
    assert set(r.json()) == {"tables", "total", "model_used", "model", "model_error"}, "旧的同步接口返回形状不变"


async def test_jobs_go_away_with_their_source(client):
    sid = await _source()
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    await finished(job["id"])
    await drop_source(sid)
    _CREATED.remove(sid)
    async with SessionLocal() as session:
        assert (await session.execute(select(CatalogDraftJob).where(CatalogDraftJob.id == job["id"]))).first() is None
        assert (await session.execute(select(CatalogDraftTask).where(CatalogDraftTask.job_id == job["id"]))).first() \
            is None
