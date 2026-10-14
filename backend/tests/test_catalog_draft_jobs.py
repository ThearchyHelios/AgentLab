"""数据目录的后台起草任务（data/catalog_jobs.py、/api/datasources/{id}/catalog/draft-jobs）与起草要求。

- 建任务 202；同一个源已有排队或进行中的任务 409 catalog_draft_busy；起草模型用不了 409
  catalog_draft_model_unavailable（不建任务）；没有表结构 409；表结构里没有的表照样建进任务、状态 skipped
- 在后台起草，每张表一结束就并入目录；进度、计数、用量落库，接口读的是库里的状态
- 停止：排队的直接 cancelled；进行中的停掉正在进行的模型调用，没写完的表记 cancelled，已经写进目录的不回滚；已结束的 409
- 服务启动时把没结束的任务标成 interrupted（没结束的表记 cancelled、写明服务重启），不自动续跑；关停时同样
- 重试：只含原任务里 failed、partial、cancelled 的表，本次说明沿用、起草要求取数据源当前的；没有可重试的 422
- 目录清单：每行 last_draft（带模型留空的列数），顶层 guidance、active_job
- 重新起草时模型这次留空、给的值没通过校验：以前起草好的模型项留着，删除只在列已经不在表结构里时才有
- 用量记超时次数、没写出来的调用次数；任务记当前的列数上限、提醒（最近 6 次调用 4 次没写出来）；逐表记模型留空的列
- 旧的同步起草：同一个源有排队或进行中的任务时 409 catalog_draft_busy
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


class HalfThenHollow:
    """假模型：第一次只给前一半列，补问时只给一个空壳（表名对上、一项都没有）。"""

    model_name = "half-model"

    def __init__(self, table: str, reply: dict) -> None:
        self.table, self.reply, self.calls = table, reply, 0

    def with_structured_output(self, schema, **_):
        outer = self

        class _S:
            async def ainvoke(self, messages):
                outer.calls += 1
                if outer.calls == 1:
                    cols = outer.reply["columns"]
                    return {"tables": [{"table": outer.table, **outer.reply, "columns": cols[:len(cols) // 2]}]}
                return {"tables": [{"table": outer.table}]}

        return _S()

    async def ainvoke(self, messages):
        raise AssertionError("这里不该走纯文本")


async def test_a_followup_that_comes_back_empty_keeps_the_earlier_model_items(client, monkeypatch):
    """补问只给空壳：表是 partial、任务是 partial，以前起草过的列（待确认的模型起草项）不因这次没写全被删掉。"""
    cols = [_col(f"c{i:02d}") for i in range(1, 9)]
    sid = await _source({"tickets": {"qualified": "tickets", "columns": cols, "primary_key": ["c01"], "comment": None,
                                      "is_view": False, "foreign_keys": []}})
    full = {"label": "门票", "kind": "fact",
            "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"} for i in range(1, 9)]}
    use(monkeypatch, FakeModel({"tickets": full}))
    first = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["tickets"], "use_model": True})).json()
    await finished(first["job"]["id"])
    before = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]["columns"]
    assert {c for c, items in before.items() if "label" in items} == {f"c{i:02d}" for i in range(1, 9)}

    model = HalfThenHollow("tickets", full)
    use(monkeypatch, model)
    second = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["tickets"], "use_model": True})).json()
    await finished(second["job"]["id"])
    job = await get_job(client, sid, second["job"]["id"])
    task = job["tasks"][0]
    assert model.calls == 2
    assert job["status"] == "partial" and task["status"] == "partial" and task["removed"] == 0
    assert "有 4 列（c05 到 c08）没写出来" in task["model_error"] and "可以只重试这张表" in task["model_error"]
    after = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]["columns"]
    assert {c for c, items in after.items() if "label" in items} == {f"c{i:02d}" for i in range(1, 9)}


def _tickets(n: int = 6) -> dict:
    return {"tickets": {"qualified": "tickets", "columns": [_col(f"c{i:02d}") for i in range(1, n + 1)],
                        "primary_key": ["c01"], "comment": None, "is_view": False, "foreign_keys": []}}


async def _draft(client, sid: str, monkeypatch, model, tables: list[str]) -> dict:
    use(monkeypatch, model)
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": tables, "use_model": True})
    assert r.status_code == 202, r.text
    await finished(r.json()["job"]["id"])
    return await get_job(client, sid, r.json()["job"]["id"])


async def test_a_redraft_keeps_what_the_model_left_blank_this_time(client, monkeypatch):
    """重新起草（比如补了起草要求再起草一次）时模型这次留空、或者给的值没通过校验：以前起草好的模型项留着，不算删除；
    给了合格新值的才替换。以前整库重跑一次删掉几百项，表仍记「已完成」。"""
    sid = await _source(_tickets())
    full = {"label": "门票", "kind": "fact",
            "columns": [{"name": f"c{i:02d}", "label": f"第{i}列", "meaning": f"第{i}列的含义"} for i in range(1, 6)]}
    first = await _draft(client, sid, monkeypatch, FakeModel({"tickets": full}), ["tickets"])
    assert first["tasks"][0]["status"] == "ok" and first["blank_columns"] == 1, "c06 第一次就留空了"
    before = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]

    # 这一次：表的中文名换了；c03 的中文名像公式（校验丢掉）、含义换了；c04 只给了中文名；其余列列出了但都留空
    again = {"label": "门票明细", "columns": [{"name": "c03", "label": "=c01+c02", "meaning": "新的含义"},
                                           {"name": "c04", "label": "第4列"}]}
    second = await _draft(client, sid, monkeypatch, FakeModel({"tickets": again}), ["tickets"])
    task = second["tasks"][0]
    assert task["status"] == "ok" and task["removed"] == 0 and second["removed"] == 0
    assert (task["added"], task["updated"]) == (0, 2)
    after = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]
    assert after["label"]["value"] == "门票明细" and after["kind"] == before["kind"]
    assert after["columns"]["c03"]["label"] == before["columns"]["c03"]["label"]
    assert after["columns"]["c03"]["meaning"]["value"] == "新的含义"
    for col in ("c01", "c02", "c05"):
        assert after["columns"][col] == before["columns"][col], col
    # 留空的列只记目录里还是没有中文名、含义的（c06）：c01、c02、c05 这次留空，以前起草好的还在，不算留空——不然照
    # 提示补了起草要求再起草，提示指着的却是目录里有中文名的列，一直消不掉
    assert task["blank_columns"] == 1 and task["blank_sample"] == ["c06"]


async def test_model_items_of_a_column_gone_from_the_schema_are_removed(client, monkeypatch):
    sid = await _source(_tickets())
    full = {"label": "门票", "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"} for i in range(1, 7)]}
    await _draft(client, sid, monkeypatch, FakeModel({"tickets": full}), ["tickets"])
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.schema_cache = {"tables": _tickets(5)}                  # 重新探查过：c06 已经没有了
        await session.commit()
    job = await _draft(client, sid, monkeypatch, FakeModel({"tickets": {"label": "门票"}}), ["tickets"])
    assert job["tasks"][0]["removed"] == 1
    after = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]
    assert set(after["columns"]) == {"c01", "c02", "c03", "c04", "c05"}


async def test_usage_counts_timeouts_and_failed_calls(client, monkeypatch):
    """超时的调用拆小再问，用量里记超时几次、没写出来几次（超时的调用拿不到用量，token 记 0）；额度用完或超时累计 2 次，
    新开始的段列数上限减半，任务上记着当前的上限。"""
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.05)
    sid = await _source(_tickets(24))
    replies = {"tickets": {"label": "门票", "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"}
                                                       for i in range(1, 25)]}}
    model = FakeModel(replies, hang=2)
    job = await _draft(client, sid, monkeypatch, model, ["tickets"])
    assert job["status"] == "succeeded" and job["tasks"][0]["status"] == "ok"
    assert job["usage"] == {"calls": 5, "input_tokens": 0, "output_tokens": 0, "timeouts": 2, "failed_calls": 2}
    assert job["part_columns"] == 20 and job["notice"] is None
    # 24 列整张问超时 → 两半同时问：前 12 列（带表级的项）超时、后 12 列写出 → 前 12 列拆成 6、6
    assert [len(requested(c)["tickets"][1] or []) for c in model.calls] == [0, 12, 12, 6, 6]

    plain = await _draft(client, sid, monkeypatch, FakeModel(replies), ["tickets"])
    assert plain["part_columns"] == 40 and plain["usage"]["timeouts"] == plain["usage"]["failed_calls"] == 0


async def test_the_notice_is_kept_on_the_job(client, monkeypatch):
    """最近 6 次调用有 4 次以上没写出来：任务上写一句提醒（次数、原因、建议换起草模型）；不用模型的任务没有。"""
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    tables = {f"t{i}": {"qualified": f"t{i}", "columns": [_col("id"), _col("v")], "primary_key": ["id"],
                        "comment": None, "is_view": False, "foreign_keys": []} for i in range(1, 6)}
    sid = await _source(tables)
    replies = {name: {"label": f"表{name}"} for name in tables}
    job = await _draft(client, sid, monkeypatch, FakeModel(replies, fail={"t1", "t2", "t3", "t4"}), list(tables))
    assert job["usage"]["calls"] == 5 and job["usage"]["failed_calls"] == 4
    assert job["notice"] == ("最近 5 次调用有 4 次没写出来（出错 4 次）。建议在「设置 → 偏好设置」的「起草模型」换一个模型、"
                             "缩小起草范围，或者停止这次起草")
    plain = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["t1"]})).json()["job"]
    await finished(plain["id"])
    plain = await get_job(client, sid, plain["id"])
    assert plain["notice"] is None and plain["part_columns"] is None and plain["blank_columns"] == 0


async def test_blank_columns_reach_the_task_the_job_and_the_list(client, monkeypatch):
    """模型列出了、中文名和含义都没给的列：逐表记列数和最多 5 个列名，任务上是合计，目录清单的 last_draft 也带列数。"""
    tables = {**_tickets(8), "gates": _tables()["gates"]}
    sid = await _source(tables)
    replies = {"tickets": {"label": "门票", "columns": [{"name": "c01", "label": "编号"}]},
               "gates": {"label": "闸口", "columns": [{"name": "id", "label": "编号"}]}}
    job = await _draft(client, sid, monkeypatch, FakeModel(replies), ["tickets", "gates"])
    by = {t["table_name"]: t for t in job["tasks"]}
    assert by["tickets"]["status"] == "ok" and by["tickets"]["blank_columns"] == 7
    assert by["tickets"]["blank_sample"] == ["c02", "c03", "c04", "c05", "c06"]
    assert by["gates"]["blank_columns"] == 1 and by["gates"]["blank_sample"] == ["gate_code"]
    assert job["blank_columns"] == 8
    rows = {r["table_name"]: r for r in (await client.get(_base(sid))).json()["tables"]}
    assert rows["tickets"]["last_draft"]["blank_columns"] == 7 and rows["gates"]["last_draft"]["blank_columns"] == 1


async def test_blank_columns_are_counted_against_the_catalog_as_written(client, monkeypatch):
    """留空的列按并入目录之后的结果算：起草进行中有人给某一列填了中文名，模型这次留空，这一列不算留空。"""
    sid = await _source(_tickets(4))
    model = Held({"tickets": {"label": "门票", "columns": [{"name": "c01", "label": "编号"}]}}, hold={"tickets"})
    use(monkeypatch, model)
    r = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["tickets"], "use_model": True})
    job_id = r.json()["job"]["id"]

    async def asking() -> bool:
        return (await get_job(client, sid, job_id))["tasks"][0]["status"] == "running"

    await until(asking)
    saved = await client.put(f"{_base(sid)}/tickets", json={"notes": {"columns": {"c02": {"label": {"value": "闸机号"}}}},
                                                           "if_version": 0})
    assert saved.status_code == 200, saved.text
    model.release.set()
    await finished(job_id)
    task = (await get_job(client, sid, job_id))["tasks"][0]
    assert task["status"] == "ok" and task["blank_columns"] == 2 and task["blank_sample"] == ["c03", "c04"]
    after = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]["columns"]
    assert after["c02"]["label"]["value"] == "闸机号" and after["c01"]["label"]["value"] == "编号"


async def test_the_sync_draft_is_409_while_a_job_is_queued_or_running(client, monkeypatch):
    """旧的同步起草不和后台任务同时写同一个源的目录、抢模型名额：有排队或进行中的任务时 409（和建任务同一句话）。"""
    sid = await _source()
    start = catalog_jobs.manager.start
    monkeypatch.setattr(catalog_jobs.manager, "start", lambda job_id: None)       # 排着、不开始
    queued = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})).json()["job"]
    r = await client.post(f"{_base(sid)}/draft", json={"tables": ["channels"]})
    assert r.status_code == 409 and r.json()["code"] == "catalog_draft_busy", r.text
    busy = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})
    assert busy.status_code == 409 and r.json()["detail"] == busy.json()["detail"]
    assert (await client.get(f"{_base(sid)}/channels")).json()["version"] == 0, "什么都没写"
    await client.post(f"{_base(sid)}/draft-jobs/{queued['id']}/cancel")

    monkeypatch.setattr(catalog_jobs.manager, "start", start)
    model = Held(REPLIES, hold={"visits"})
    use(monkeypatch, model)
    running = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["visits"], "use_model": True})).json()
    r = await client.post(f"{_base(sid)}/draft", json={"tables": ["channels"]})
    assert r.status_code == 409 and r.json()["code"] == "catalog_draft_busy", r.text
    model.release.set()
    await finished(running["job"]["id"])
    r = await client.post(f"{_base(sid)}/draft", json={"tables": ["channels"]})
    assert r.status_code == 200, r.text


def test_old_job_tables_get_the_new_columns():
    """升级前建的起草任务表：新列补上，存量行的计数是 0、提醒和列数上限为空。"""
    from sqlalchemy import create_engine, text

    from app.db.base import _migrate

    db = create_engine("sqlite://")
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE catalog_draft_jobs (id VARCHAR(32) PRIMARY KEY, calls INTEGER)"))
        conn.execute(text("CREATE TABLE catalog_draft_tasks (id VARCHAR(32) PRIMARY KEY, removed INTEGER)"))
        conn.execute(text("INSERT INTO catalog_draft_jobs (id, calls) VALUES ('j1', 3)"))
        conn.execute(text("INSERT INTO catalog_draft_tasks (id, removed) VALUES ('t1', 0)"))
        _migrate(conn)
        _migrate(conn)
        assert conn.execute(text("SELECT timeouts, failed_calls, part_columns, notice FROM catalog_draft_jobs")) \
            .one() == (0, 0, None, None)
        assert conn.execute(text("SELECT blank_columns, blank_sample FROM catalog_draft_tasks")).one() == (0, None)
        # 闭环第一步：逐段落盘的段数、块进度。存量行是 0 和 NULL：升级前没记这些
        assert conn.execute(text("SELECT parts_written, chunks_total, chunks_done FROM catalog_draft_tasks")) \
            .one() == (0, None, None)


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
    for body in ({}, {"guidance": 3}, {"guidance": ["金额按元"]}):
        r = await client.put(f"{_base(sid)}/guidance", json=body)
        assert r.status_code == 422, body
        assert isinstance(r.json()["detail"], str) and "起草要求" in r.json()["detail"], "说明是中文的一句话"
    assert (await client.get(_base(sid))).json()["guidance"] == "金额按元"


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


# --------------------------------------------------------------------------
# 闭环第一步：宽表逐段落盘、停止后的说法、块进度、停止后的留空提示、同步起草与任务互斥
# --------------------------------------------------------------------------


def _wide(n: int = 95) -> dict:
    return {"wide": {"qualified": "wide", "columns": [_col(f"c{i:03d}") for i in range(1, n + 1)],
                     "primary_key": ["c001"], "comment": None, "is_view": False, "foreign_keys": []}}


def _wide_reply(blank: set[str] = frozenset()) -> dict:
    """95 列的宽表每列都写中文名；blank 里的列只列出、不写（模型拿不准）。"""
    return {"wide": {"label": "宽表", "kind": "fact",
                     "columns": [{"name": f"c{i:03d}", "label": f"第{i}列"} for i in range(1, 96)
                                 if f"c{i:03d}" not in blank]}}


class Hold:
    """假模型：请写的表在 tables 里、或请写的列里有 columns 里的列时，等 release 才回（停止时这次调用被取消）；别的照
    FakeModel 回。waiting：有调用在等了。"""

    def __init__(self, replies: dict, *, tables: set[str] = frozenset(), columns: set[str] = frozenset()) -> None:
        self.inner = FakeModel(replies)
        self.tables, self.columns = set(tables), set(columns)
        self.release, self.waiting = asyncio.Event(), asyncio.Event()
        self.cancelled = 0
        self.model_name = "hold-model"

    def with_structured_output(self, schema, **_):
        outer = self

        class _S:
            async def ainvoke(self, messages):
                asked = requested("\n".join(str(m.content) for m in messages))
                cols = {c for _, listed in asked.values() for c in listed or []}
                if outer.tables & set(asked) or outer.columns & cols:
                    outer.waiting.set()
                    try:
                        await outer.release.wait()
                    except asyncio.CancelledError:
                        outer.cancelled += 1
                        raise
                return outer.inner._reply(messages, "structured")

        return _S()

    async def ainvoke(self, messages):
        return await self.inner.ainvoke(messages)


async def _task(client, sid: str, job_id: str, table: str) -> dict:
    return next(t for t in (await get_job(client, sid, job_id))["tasks"] if t["table_name"] == table)


async def _start_held_wide(client, sid: str, monkeypatch) -> tuple[str, Hold]:
    """95 列的宽表分 4 段：表级那段和第 2 段照常写，第 3、4 段一直等着。等到前两段都写进目录才返回。"""
    model = Hold(_wide_reply(), columns={"c033", "c065"})
    use(monkeypatch, model)
    job = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["wide"], "use_model": True})).json()["job"]

    async def two_written() -> bool:
        return (await _task(client, sid, job["id"], "wide"))["parts_written"] == 2

    await until(two_written)
    return job["id"], model


async def test_a_stopped_wide_table_keeps_the_parts_already_written(client, monkeypatch):
    """宽表每写出一段就并入目录：停止时已经写进去的段留着，这张表照旧记停止，model_error 说清写进了几段、剩几段没起草。"""
    sid = await _source(_wide())
    job_id, model = await _start_held_wide(client, sid, monkeypatch)
    task = await _task(client, sid, job_id, "wide")
    assert task["status"] == "running" and task["parts_total"] == 4 and task["parts_done"] == 2
    assert (task["chunks_total"], task["chunks_done"]) == (4, 2)
    assert task["added"] == 2 + 32 and task["version"] >= 1, "表级两项 + 第 2 段 32 列"

    r = await client.post(f"{_base(sid)}/draft-jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    task = next(t for t in r.json()["job"]["tasks"] if t["table_name"] == "wide")
    assert task["status"] == "cancelled" and task["error"] is None and task["parts_written"] == 2
    assert task["model_error"] == "已停止：已写进目录 2 段，剩下 2 段没起草"
    assert model.cancelled == 2, "第 3、4 段正在进行的调用被停掉"
    notes = (await client.get(f"{_base(sid)}/wide")).json()["notes"]
    assert notes["label"]["value"] == "宽表" and notes["kind"]["value"] == "fact"
    assert set(notes["columns"]) == {f"c{i:03d}" for i in range(1, 33)}, "第 2 段写进去的留着"
    row = next(r for r in (await client.get(_base(sid))).json()["tables"] if r["table_name"] == "wide")
    assert row["last_draft"]["status"] == "cancelled" and row["last_draft"]["parts_written"] == 2
    assert row["last_draft"]["blank_columns_from"] is None, "停止前写进了新内容：以前的留空列数不再沿用"


async def test_an_interrupted_wide_table_keeps_its_written_parts_too(client, monkeypatch):
    sid = await _source(_wide())
    job_id, _ = await _start_held_wide(client, sid, monkeypatch)
    await catalog_jobs.manager.shutdown(grace=5)
    task = await _task(client, sid, job_id, "wide")
    assert task["status"] == "cancelled" and task["error"] == "服务重启，起草中断"
    assert task["model_error"] == "已中断：已写进目录 2 段，剩下 2 段没起草"
    assert len((await client.get(f"{_base(sid)}/wide")).json()["notes"]["columns"]) == 32

    # 进程没来得及收尾（直接退出）：下次启动时同样写清
    async with SessionLocal() as session:
        job = CatalogDraftJob(source_id=sid, status="running", use_model=True)
        session.add(job)
        await session.flush()
        session.add(CatalogDraftTask(job_id=job.id, seq=0, table_name="wide", status="running", parts_total=3,
                                     parts_written=1))
        session.add(CatalogDraftTask(job_id=job.id, seq=1, table_name="gates", status="pending", parts_total=1))
        await session.commit()
        crashed = job.id
    await catalog_jobs.startup()
    by = {t["table_name"]: t for t in (await get_job(client, sid, crashed))["tasks"]}
    assert by["wide"]["model_error"] == "已中断：已写进目录 1 段，剩下 2 段没起草"
    assert by["gates"]["model_error"] is None and by["gates"]["error"] == "服务重启，起草中断"


async def test_a_finished_wide_table_counts_every_write_once(client, monkeypatch):
    """逐段落盘之后整张表收尾一次：新增、更新的计数是各次写入的合计（每一项只数一次），块进度和写进去的段数都记下。"""
    sid = await _source(_wide())
    job = await _draft(client, sid, monkeypatch, FakeModel(_wide_reply({"c010", "c040", "c070"})), ["wide"])
    task = job["tasks"][0]
    assert task["status"] == "ok" and task["parts_written"] == 4 and task["parts_done"] == 4
    assert (task["chunks_total"], task["chunks_done"]) == (4, 4)
    assert (task["added"], task["updated"], task["removed"]) == (2 + 92, 0, 0)
    assert task["blank_columns"] == 3 and task["blank_sample"] == ["c010", "c040", "c070"]
    notes = (await client.get(f"{_base(sid)}/wide")).json()["notes"]
    assert len(notes["columns"]) == 92

    plain = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["wide"]})).json()["job"]
    await finished(plain["id"])
    task = (await get_job(client, sid, plain["id"]))["tasks"][0]
    assert (task["parts_written"], task["chunks_total"], task["chunks_done"]) == (0, None, None), "不用模型时为 null"


async def test_chunk_progress_shows_a_part_that_was_split(client, monkeypatch):
    """块进度：这张表实际分成的块数和已结束的块数，一段超时拆成两半后变多。"""
    monkeypatch.setattr(catalog, "MODEL_TIMEOUT_S", 0.05)
    sid = await _source(_tickets(24))
    replies = {"tickets": {"label": "门票", "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"}
                                                       for i in range(1, 25)]}}
    job = await _draft(client, sid, monkeypatch, FakeModel(replies, hang=1), ["tickets"])
    task = job["tasks"][0]
    assert task["status"] == "ok" and (task["chunks_total"], task["chunks_done"]) == (2, 2)
    assert task["parts_written"] == 1 and task["parts_total"] == 1


async def test_a_partial_table_also_drops_model_items_of_columns_gone_from_the_schema(client, monkeypatch):
    """整张表写完时按完整覆盖的规则收尾，只写出一部分的也一样：只删已经不在表结构里的列的模型起草项。"""
    sid = await _source(_tickets(6))
    full = {"label": "门票", "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"} for i in range(1, 7)]}
    await _draft(client, sid, monkeypatch, FakeModel({"tickets": full}), ["tickets"])
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.schema_cache = {"tables": _tickets(5)}                  # 重新探查过：c06 已经没有了
        await session.commit()
    some = {"label": "门票", "columns": [{"name": f"c{i:02d}", "label": f"第{i}列"} for i in range(1, 5)]}
    job = await _draft(client, sid, monkeypatch, FakeModel({"tickets": some}, list_all=False), ["tickets"])
    task = job["tasks"][0]
    assert task["status"] == "partial" and task["removed"] == 1
    after = (await client.get(f"{_base(sid)}/tickets")).json()["notes"]["columns"]
    assert set(after) == {"c01", "c02", "c03", "c04", "c05"}, "c05 是以前起草的，这次没写出来也留着"


async def test_blank_columns_from_carries_the_last_finished_draft_over_a_stop(client, monkeypatch):
    """目录清单的 last_draft.blank_columns_from：最近一次结束于完成、部分完成的起草记下的留空列数。之后的起草停止了、
    没写进新内容时照样带着，界面据此继续提示；不用模型的起草不算。"""
    sid = await _source(_tickets(4))
    replies = {"tickets": {"label": "门票", "columns": [{"name": "c01", "label": "编号"}]}}
    await _draft(client, sid, monkeypatch, FakeModel(replies), ["tickets"])

    async def last() -> dict:
        rows = (await client.get(_base(sid))).json()["tables"]
        return next(r for r in rows if r["table_name"] == "tickets")["last_draft"]

    first = await last()
    assert first["status"] == "ok" and first["blank_columns"] == 3 and first["blank_columns_from"] == 3
    assert first["parts_written"] == 1

    plain = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["tickets"]})).json()["job"]
    await finished(plain["id"])
    assert (await last())["blank_columns_from"] == 3, "不用模型的起草不改模型留空的列"

    monkeypatch.setattr(catalog_jobs.manager, "start", lambda job_id: None)
    queued = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["tickets"], "use_model": True})).json()
    await client.post(f"{_base(sid)}/draft-jobs/{queued['job']['id']}/cancel")
    stopped = await last()
    assert stopped["status"] == "cancelled" and stopped["blank_columns"] == 0 and stopped["parts_written"] == 0
    assert stopped["blank_columns_from"] == 3

    other = await _source(_tickets(4))
    rows = (await client.get(_base(other))).json()["tables"]
    assert rows[0]["last_draft"] is None


async def test_a_sync_draft_and_draft_jobs_exclude_each_other(client, monkeypatch):
    """同步起草进行期间，同一个源建任务、重试、再开一次同步起草都 409 catalog_draft_busy（导入照 ensure_idle 认）；
    同步起草结束后照常。"""
    sid = await _source()
    use(monkeypatch, FakeModel(REPLIES))
    old = (await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"], "use_model": True})).json()["job"]
    await finished(old["id"])
    async with SessionLocal() as session:
        await session.execute(
            CatalogDraftTask.__table__.update().where(CatalogDraftTask.job_id == old["id"]).values(status="failed"))
        await session.commit()

    model = Hold(REPLIES, tables={"visits"})
    use(monkeypatch, model)
    sync = asyncio.create_task(client.post(f"{_base(sid)}/draft", json={"tables": ["visits"], "use_model": True}))
    await asyncio.wait_for(model.waiting.wait(), 5)

    busy = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})
    assert busy.status_code == 409 and busy.json()["code"] == "catalog_draft_busy", busy.text
    assert "正在起草" in busy.json()["detail"]
    retry = await client.post(f"{_base(sid)}/draft-jobs/{old['id']}/retry")
    assert retry.status_code == 409 and retry.json()["code"] == "catalog_draft_busy", retry.text
    again = await client.post(f"{_base(sid)}/draft", json={"tables": ["gates"]})
    assert again.status_code == 409 and again.json()["code"] == "catalog_draft_busy", again.text
    async with SessionLocal() as session:
        with pytest.raises(catalog_jobs.DraftBusy):
            await catalog_jobs.ensure_idle(session, await session.get(DataSource, sid))
    assert (await client.get(f"{_base(sid)}/draft-jobs/latest")).json()["job"]["id"] == old["id"], "没建新任务"

    model.release.set()
    done = await sync
    assert done.status_code == 200, done.text
    after = await client.post(f"{_base(sid)}/draft-jobs", json={"tables": ["gates"]})
    assert after.status_code == 202, after.text
    await finished(after.json()["job"]["id"])
