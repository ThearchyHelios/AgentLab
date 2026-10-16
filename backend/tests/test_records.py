"""记录表（docs/design/normal-user-apps.md N8、§3.7）。

- 追加：按动作键幂等，同一个动作第二次来交回第一次写下的那一行；更正是新的一行带 _supersedes，原行不改；
- 高水位：运行开始时记下最大的 _seq，同一次运行查两遍结果一样，期间有新提交也不变；
- 行级可见：use 只看自己提交的，observe 看全部；正式运行看全部；查询里直接写 main.<表> 被只读守卫拒掉，幂等键列不露；
- 表版本：只加可选字段在同一版本上加列；改类型、删字段、加必填字段出新版本，旧版本只读保留；
- records_append 走动作：生效记录、无人值守、重放不重复写；结果未知时按幂等键查到、核实为已生效、不重发；
  探索运行演练，一行都不写；
- records_list（GET /api/apps/{id}/records）按看的人实时过滤；看板表格逐行列出记录表时应用检查给警告。

夹具：门店报损的假数据，人名是假名。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.data.engine import run_query
from app.data.guard import SqlRejected, schema_reference
from app.db.base import SessionLocal, utcnow
from app.db.models import ActionRecord, DataSource, Run, RunEvent, User
from app.db.models_apps import App, AppGrant
from app.db.models_blueprints import RecordsTable
from app.engine.runner import run_manager
from app.records import RECORDS_APPEND, RECORDS_LOOKUP, store
from app.records.tables import columns_of, ensure_table, path_of
from app.records.views import resolve

COLUMNS = [
    {"name": "门店", "label": "门店", "type": "string", "required": True},
    {"name": "数量", "label": "报损数量", "type": "integer", "required": True},
    {"name": "原因", "label": "原因", "type": "string"},
]
COUNT = "SELECT count(*) AS n FROM records"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def engine_up(data_dir):
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("interrupted", "failed", "succeeded"), timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses and not run_manager.is_active(row.id):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def make_app(name: str = "门店报损", *, grants: dict[str, str] | None = None) -> App:
    async with SessionLocal() as session:
        app = App(name=name, created_by="建立人")
        session.add(app)
        await session.flush()
        for who, role in (grants or {}).items():
            session.add(AppGrant(app_id=app.id, principal_kind="user", principal=who, role=role))
        await session.commit()
        return app


async def make_table(owner, columns=COLUMNS) -> RecordsTable:
    async with SessionLocal() as session:
        row = await ensure_table(session, owner, columns)
        await session.commit()
        return row


async def source_of(table: RecordsTable) -> DataSource:
    async with SessionLocal() as session:
        return await session.get(DataSource, table.source_id)


def add(table: RecordsTable, values: dict, *, by: str = "amy", key: str | None = None, supersedes: str | None = None):
    return store.append(path_of(table), columns_of(table), values, version=table.version, run_id=None, actor=by,
                        action_key=key or uuid.uuid4().hex, supersedes=supersedes)


def rows_of(result) -> list:
    return result.rows


# --------------------------------------------------------------------------
# 追加、幂等、更正
# --------------------------------------------------------------------------


async def test_append_is_idempotent_and_corrections_keep_the_original():
    table = await make_table(await make_app())
    first = add(table, {"门店": "一店", "数量": "3"}, key="k1")
    again = add(table, {"门店": "一店", "数量": "99"}, key="k1")
    assert not first.duplicate and again.duplicate
    assert (again.seq, again.row_id) == (first.seq, first.row_id), "同一个动作键只落一行，交回第一次的那一行"
    assert store.high_water(path_of(table)) == first.seq == 1
    assert first.values["数量"] == 3, "模板渲染出来的文字按列类型收成整数"

    fixed = add(table, {"门店": "一店", "数量": 4, "原因": "过期"}, key="k2", supersedes=first.row_id)
    cols = columns_of(table)
    current, _ = store.rows(path_of(table), cols, scope="all", viewer=None)
    assert [r["_id"] for r in current] == [fixed.row_id], "更正过的行不再出现在当前有效的行里"
    history, _ = store.rows(path_of(table), cols, scope="all", viewer=None, history=True)
    assert [(r["_id"], r["superseded"]) for r in history] == [(fixed.row_id, False), (first.row_id, True)]
    original = next(r for r in history if r["_id"] == first.row_id)
    assert original["values"]["数量"] == 3 and original["_supersedes"] is None, "原行一个字都不改"
    assert next(r for r in history if r["_id"] == fixed.row_id)["_supersedes"] == first.row_id

    for values, supersedes, code in [
        ({"门店": "一店", "数量": 1}, first.row_id, "records.supersede_twice"),
        ({"门店": "一店", "数量": 1}, "nope", "records.supersede_missing"),
        ({"门店": "一店", "数量": 1, "颜色": "红"}, None, "records.column_unknown"),
        ({"门店": "一店"}, None, "records.required_missing"),
        ({"门店": "一店", "数量": "三"}, None, "records.value_invalid"),
        ({"门店": "一店", "数量": "1.5"}, None, "records.value_invalid"),
    ]:
        with pytest.raises(store.RecordsRefused) as refused:
            add(table, values, supersedes=supersedes)
        assert refused.value.code == code, values
    assert store.high_water(path_of(table)) == 2, "被拒的写入一行都没落"


# --------------------------------------------------------------------------
# 查询：临时视图、只读守卫
# --------------------------------------------------------------------------


async def test_queries_go_through_the_temp_view_and_main_is_refused():
    table = await make_table(await make_app())
    first = add(table, {"门店": "一店", "数量": 2})
    add(table, {"门店": "一店", "数量": 5}, supersedes=first.row_id)
    add(table, {"门店": "二店", "数量": 1}, by="bob")
    source = await source_of(table)
    assert source.origin == "records" and source.readonly
    async with SessionLocal() as session:
        view = await resolve(session, source, {"seq": 3, "rows": "all"})
    total = await run_query(view, 'SELECT sum("数量") AS s, count(*) AS n FROM records')
    assert total.rows == [[6, 2]], "被更正的那一行不算"
    for sql in ("SELECT * FROM main.records", 'SELECT * FROM "main"."records"', "SELECT * FROM [main].records",
                "SELECT * FROM main . records", "SELECT a.* FROM records a JOIN MAIN.records b ON a._id = b._id"):
        with pytest.raises(SqlRejected) as refused:
            await run_query(view, sql)
        assert "main." in str(refused.value), sql
    with pytest.raises(Exception) as hidden:
        await run_query(view, "SELECT _action_key FROM records")
    assert "_action_key" in str(hidden.value), "幂等键列不在视图里"
    # 没经解析、直接拿数据源行查（数据剖析这类入口）：照样走视图
    plain = await run_query(source, COUNT)
    assert plain.rows == [[2]]
    with pytest.raises(SqlRejected):
        await run_query(source, "SELECT count(*) FROM main.records")


def test_schema_reference_ignores_strings_and_identifiers_that_merely_contain_main():
    assert schema_reference("SELECT * FROM main.records", ("main",)) == "main"
    assert schema_reference("SELECT 'main.records' AS x FROM records", ("main",)) is None
    assert schema_reference('SELECT "main.records" FROM records', ("main",)) is None
    assert schema_reference("SELECT domain.x FROM records AS domain", ("main",)) is None
    assert schema_reference("SELECT 1 /* main.records */ FROM records", ("main",)) is None


# --------------------------------------------------------------------------
# 高水位：同一次运行查两遍一样
# --------------------------------------------------------------------------


async def test_a_run_sees_the_same_rows_twice_even_when_new_ones_arrive(engine_up):
    table = await make_table(await make_app())
    add(table, {"门店": "一店", "数量": 1})
    add(table, {"门店": "二店", "数量": 2}, by="bob")
    source = await source_of(table)
    tool = f"db_query__{source.name}"
    graph = chain(node("start", "input"), node("t1", "tool", tool=tool, args={"sql": COUNT}),
                  node("gate", "human", mode="approve", title="看一眼"),
                  node("t2", "tool", tool=tool, args={"sql": COUNT}),
                  node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"},
                                                {"name": "t2", "value": "{{ nodes.t2 }}"}]))
    run = await run_manager.start(graph=graph, input_payload={}, run_class="formal")
    assert (await wait(run.id)).status == "interrupted"
    add(table, {"门店": "三店", "数量": 3}, by="cat")
    add(table, {"门店": "四店", "数量": 4})
    await run_manager.resume(run.id, {"approved": True})
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert json.loads(done.output["t1"])["rows"] == [[2]]
    assert json.loads(done.output["t2"])["rows"] == [[2]], "审批期间新来的两条没有混进这次运行"
    entry = done.data_versions[source.id]
    assert entry["seq"] == 2 and entry["rows"] == "all" and entry["origin"] == "records"
    async with SessionLocal() as session:
        started = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "run.started").order_by(RunEvent.seq))).scalars())
    assert all(e.data["data_versions"][source.id]["seq"] == 2 for e in started), "封存的发起事件里记着高水位"
    # 新的一次运行看得到新来的
    once = chain(node("start", "input"), node("t", "tool", tool=tool, args={"sql": COUNT}),
                 node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}]))
    again = await run_manager.start(graph=once, input_payload={}, run_class="formal")
    assert json.loads((await wait(again.id, ("succeeded", "failed"))).output["t"])["rows"] == [[4]]


# --------------------------------------------------------------------------
# 行级可见
# --------------------------------------------------------------------------


async def _users(*names_roles):
    async with SessionLocal() as session:
        for name, role in names_roles:
            if (await session.execute(select(User).where(User.username == name))).scalar_one_or_none() is None:
                session.add(User(username=name, display_name=name, role=role))
        await session.commit()


async def test_exploratory_runs_see_rows_by_the_starters_grant(engine_up, monkeypatch):
    import app.api.deps as deps

    suffix = uuid.uuid4().hex[:6]
    amy, bob, cat = f"amy{suffix}", f"bob{suffix}", f"cat{suffix}"
    await _users((amy, "user"), (bob, "user"), (cat, "user"))
    app = await make_app(grants={amy: "use", bob: "use", cat: "observe"})
    table = await make_table(app)
    add(table, {"门店": "一店", "数量": 1}, by=amy)
    add(table, {"门店": "一店", "数量": 2}, by=amy)
    add(table, {"门店": "二店", "数量": 5}, by=bob)
    source = await source_of(table)
    monkeypatch.setattr(deps, "auth_mode", lambda: "local")
    graph = chain(node("start", "input"), node("t", "tool", tool=f"db_query__{source.name}",
                                                args={"sql": 'SELECT count(*) AS n, sum("数量") AS s FROM records'}),
                  node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}]))

    async def seen(who: str, run_class: str = "exploratory") -> list:
        run = await run_manager.start(graph=graph, input_payload={}, started_by=who, run_class=run_class)
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        return json.loads(done.output["t"])["rows"][0]

    assert await seen(amy) == [2, 3], "use 只看自己提交的"
    assert await seen(bob) == [1, 5]
    assert await seen(cat) == [3, 8], "observe 看全部"
    assert await seen(f"zed{suffix}") == [0, None], "没有授权的一行都看不到"
    assert await seen(amy, "formal") == [3, 8], "正式运行以服务身份跑，看全部"


async def test_records_list_filters_by_viewer(client, monkeypatch):
    from app.api import personal
    from app.api.deps import Principal

    app = await make_app(grants={"amy": "use", "cat": "observe"})
    table = await make_table(app)
    a1 = add(table, {"门店": "一店", "数量": 1}, by="amy")
    add(table, {"门店": "二店", "数量": 2}, by="bob")
    fixed = add(table, {"门店": "一店", "数量": 3}, by="amy", supersedes=a1.row_id)

    def as_(name: str) -> None:
        monkeypatch.setattr(personal, "current_principal",
                            lambda: Principal(username=name, role="user", verified=True, mode="local"))

    as_("amy")
    mine = (await client.get(f"/api/apps/{app.id}/records")).json()
    assert mine["mine_only"] is True and [r["_id"] for r in mine["items"]] == [fixed.row_id]
    assert mine["items"][0]["values"] == {"门店": "一店", "数量": 3, "原因": None}
    assert [c["name"] for c in mine["columns"]] == ["门店", "数量", "原因"] and mine["table"] == table.table_key
    history = (await client.get(f"/api/apps/{app.id}/records", params={"history": "true"})).json()
    assert [(r["_id"], r["superseded"]) for r in history["items"]] == [(fixed.row_id, False), (a1.row_id, True)]
    as_("cat")
    everyone = (await client.get(f"/api/apps/{app.id}/records")).json()
    assert everyone["mine_only"] is False and len(everyone["items"]) == 2
    paged = (await client.get(f"/api/apps/{app.id}/records", params={"limit": 1})).json()
    assert len(paged["items"]) == 1 and paged["next_cursor"]
    rest = (await client.get(f"/api/apps/{app.id}/records",
                             params={"limit": 1, "cursor": paged["next_cursor"]})).json()
    assert rest["items"][0]["_id"] != paged["items"][0]["_id"]
    as_("dan")
    assert (await client.get(f"/api/apps/{app.id}/records")).status_code == 404, "没有授权的人不知道应用在不在"


# --------------------------------------------------------------------------
# 表版本
# --------------------------------------------------------------------------


async def test_compatible_changes_add_columns_incompatible_ones_make_a_new_version():
    app = await make_app()
    v1 = await make_table(app)
    old = add(v1, {"门店": "一店", "数量": 1})
    # 加一个可选字段：同一版本加列
    same = await make_table(app, [*COLUMNS, {"name": "照片", "label": "照片", "type": "file"}])
    assert same.id == v1.id and same.version == 1
    assert [c["name"] for c in same.columns] == ["门店", "数量", "原因", "照片"]
    add(same, {"门店": "二店", "数量": 2, "照片": {"file_id": "f1", "name": "a.png", "size": 1, "sha256": "0" * 64,
                                              "type": "image/png"}})
    source = await source_of(same)
    assert "照片" in [c["name"] for c in source.schema_cache["tables"]["records"]["columns"]]
    # 标敏感：进数据源的遮罩列，不出新版本
    masked = await make_table(app, [*COLUMNS[:2], {**COLUMNS[2], "sensitive": True},
                                    {"name": "照片", "label": "照片", "type": "file"}])
    assert masked.version == 1 and (await source_of(masked)).options["mask_columns"] == ["原因"]

    # 改类型：出新版本，旧版本只读保留
    v2 = await make_table(app, [COLUMNS[0], {**COLUMNS[1], "type": "string"}])
    assert v2.version == 2 and v2.table_key == v1.table_key and v2.source_id != v1.source_id
    async with SessionLocal() as session:
        versions = list((await session.execute(select(RecordsTable).where(
            RecordsTable.table_key == v1.table_key).order_by(RecordsTable.version))).scalars())
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].retired_at is not None and "类型" in versions[0].note and versions[1].retired_at is None
    assert store.high_water(path_of(versions[0])) == 2, "旧版本的记录照原样留着"
    assert (await run_query(await source_of(versions[0]), COUNT)).rows == [[2]], "旧版本照样查得到"
    # 删字段、加必填字段、可选改必填：都出新版本
    v3 = await make_table(app, [COLUMNS[0]])
    v4 = await make_table(app, [COLUMNS[0], {"name": "店长", "type": "string", "required": True}])
    v5 = await make_table(app, [COLUMNS[0], {"name": "店长", "type": "string", "required": True},
                                {"name": "备注", "type": "string"}])
    v6 = await make_table(app, [COLUMNS[0], {"name": "店长", "type": "string", "required": True},
                                {"name": "备注", "type": "string", "required": True}])
    assert [v3.version, v4.version, v5.version, v6.version] == [3, 4, 4, 5]

    # 往停用的版本写：拒绝
    from app.records.actions import append_action

    refused = await append_action({"table": versions[0].id, "values": {"门店": "一店", "数量": 1}}, idem_key="x:1",
                                  run_id=None, on_behalf=None, environment="prod")
    assert refused.status is None and "停用" in refused.error and old.seq == 1


# --------------------------------------------------------------------------
# records_append 走动作
# --------------------------------------------------------------------------


async def _sync() -> None:
    from app.capabilities.registry import sync_all

    async with SessionLocal() as session:
        await sync_all(session)


def append_graph(table_ref: str) -> dict:
    return chain(node("start", "input", fields=[{"name": "store"}, {"name": "qty"}]),
                 node("w", "tool", tool=RECORDS_APPEND, approval="never",
                      args={"table": table_ref, "values": {"门店": "{{ input.store }}", "数量": "{{ input.qty }}"}}),
                 node("out", "output", fields=[{"name": "id", "value": "{{ nodes.w.receipt._id }}"}]))


async def writer_of(app: App, graph: dict) -> str:
    """应用里钉着的那个工作流（正式运行只写自己应用的记录表：表没有蓝图时认应用钉着的工作流）。交回工作流 id。"""
    from app.db.models import Workflow

    async with SessionLocal() as session:
        wf = Workflow(name=f"报损-{uuid.uuid4().hex[:6]}", graph=graph, status="published")
        session.add(wf)
        await session.flush()
        row = await session.get(App, app.id)
        row.draft = {"pins": [{"alias": "main", "workflow_id": wf.id, "version": 1, "graph_hash": "x"}]}
        await session.commit()
        return wf.id


async def test_records_append_is_a_governed_write_that_writes_once(engine_up):
    from app.capabilities.platform import execute
    from app.capabilities.registry import get_capability

    app = await make_app()
    table = await make_table(app)
    source = await source_of(table)
    workflow_id = await writer_of(app, append_graph(source.name))
    await _sync()
    async with SessionLocal() as session:
        cap = await get_capability(session, RECORDS_APPEND)
        finder = await get_capability(session, RECORDS_LOOKUP)
    assert cap.effect_system == "write" and cap.unattended_write and cap.lookup == RECORDS_LOOKUP
    assert finder.effect_system == "read"

    run = await run_manager.start(graph=append_graph(source.name), input_payload={"store": "一店", "qty": "7"},
                                  run_class="formal", started_by="amy", workflow_id=workflow_id)
    done = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert done.status == "succeeded", done.error
    async with SessionLocal() as session:
        [rec] = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == run.id))).scalars())
    assert rec.state == "confirmed" and rec.environment == "prod" and rec.capability == RECORDS_APPEND
    [row], _ = store.rows(path_of(table), columns_of(table), scope="all", viewer=None)
    assert row["values"] == {"门店": "一店", "数量": 7, "原因": None}
    assert row["_submitted_by"] == "amy" and row["_run_id"] == run.id and done.output["id"] == row["_id"]

    # 重放（同一个动作键，第几次发都一样）：交回第一次的那一行，不再写
    for idem in (rec.idem_key, f"{rec.action_key}:2"):
        again = await execute(None, cap, {"table": source.name, "values": {"门店": "一店", "数量": 7}},
                              environment="prod", idem_key=idem, on_behalf={"sub": "amy"}, run_id=run.id)
        assert again.status == 200 and again.body["duplicate"] is True and again.body["_id"] == row["_id"]
    assert store.high_water(path_of(table)) == 1

    # 探索运行：演练，一行都不写
    trial = await run_manager.start(graph=append_graph(source.name), input_payload={"store": "二店", "qty": "1"},
                                    run_class="exploratory", started_by="amy")
    assert (await wait(trial.id, ("succeeded", "failed"))).status == "succeeded"
    async with SessionLocal() as session:
        [sim] = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == trial.id))).scalars())
    assert sim.state == "simulated" and store.high_water(path_of(table)) == 1


async def test_unknown_result_is_found_by_the_lookup_and_not_written_again(engine_up, monkeypatch):
    """写下了、回执没拿到（进程在写完之后出了错）：结果未知、运行挂起；查询能力按幂等键查到那一行，核实为已生效，
    接着跑时节点交回存下的回执，记录表里还是一行。"""
    from app.process import actions
    from app.records import actions as records_actions

    app = await make_app()
    table = await make_table(app)
    source = await source_of(table)
    workflow_id = await writer_of(app, append_graph(source.name))
    await _sync()
    real = records_actions.append_action

    async def crash_after_write(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("写完之后连接断了")

    monkeypatch.setattr(records_actions, "append_action", crash_after_write)
    run = await run_manager.start(graph=append_graph(source.name), input_payload={"store": "一店", "qty": "2"},
                                  run_class="formal", started_by="amy", workflow_id=workflow_id)
    assert (await wait(run.id)).status == "interrupted"
    async with SessionLocal() as session:
        [rec] = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == run.id))).scalars())
    assert rec.state == "unknown" and store.high_water(path_of(table)) == 1
    monkeypatch.setattr(records_actions, "append_action", real)
    for _ in range(100):
        async with SessionLocal() as session:
            if (await session.get(ActionRecord, rec.id)).next_lookup_at is not None:
                break
        await asyncio.sleep(0.05)
    assert rec.id in await actions.tick(utcnow() + timedelta(minutes=5))
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    async with SessionLocal() as session:
        rec = await session.get(ActionRecord, rec.id)
    assert rec.state == "confirmed" and rec.resolution["how"] == "lookup"
    assert store.high_water(path_of(table)) == 1, "核实之后接着跑，没有再写一行"


# --------------------------------------------------------------------------
# 应用检查：看板表格逐行列出记录表
# --------------------------------------------------------------------------


async def test_dashboard_table_listing_record_rows_is_warned():
    from app.apps.lint import _Issues, _lint_records_rows

    table = await make_table(await make_app())
    source = await source_of(table)

    def spec_with(sql: str) -> tuple[dict, dict]:
        graph = chain(node("start", "input"),
                      node("q", "tool", tool=f"db_query__{source.name}", args={"sql": sql}))
        spec = {"views": {"v": {"source": {"kind": "latest_sealed", "pin": "main"}}},
                "pages": [{"id": "home", "body": [{"id": "rows", "type": "table",
                                                   "bind": {"view": "v", "query": {"node": "q"}}}]}]}
        return spec, {"main": SimpleNamespace(graph=graph, row=object(), blueprint=None)}

    async def codes(sql: str) -> list[str]:
        spec, pins = spec_with(sql)
        out = _Issues()
        async with SessionLocal() as session:
            await _lint_records_rows(session, spec, pins, out)
        return [i["code"] for i in out.items]

    assert await codes("SELECT * FROM records") == ["app.records_rows_exposed"]
    assert await codes('SELECT "门店", "数量" FROM records WHERE "数量" > 1') == ["app.records_rows_exposed"]
    assert await codes('SELECT "门店", sum("数量") AS n FROM records GROUP BY "门店"') == []
    assert await codes("SELECT count(*) FROM records") == []


# --------------------------------------------------------------------------
# 数据源：平台维护，数据目录照常起草
# --------------------------------------------------------------------------


async def test_records_source_is_managed_by_the_platform(client):
    from app.data import catalog, table_versions

    table = await make_table(await make_app())
    source = await source_of(table)
    add(table, {"门店": "一店", "数量": 1})
    # 启动整理不把它当成「指向应用自己数据文件的手工源」停掉
    async with SessionLocal() as session:
        await table_versions._disable_manual_app_files(session)
    assert (await source_of(table)).enabled is True
    # 连接字段改不了、删不掉、不用重新探查；测连接照常
    patched = await client.patch(f"/api/datasources/{source.id}", json={"database": "/tmp/x.db"})
    assert patched.status_code == 422 and "记录表" in patched.json()["detail"]
    assert (await client.patch(f"/api/datasources/{source.id}", json={"description": "门店报损"})).status_code == 200
    assert (await client.delete(f"/api/datasources/{source.id}")).status_code == 409
    assert (await client.post(f"/api/datasources/{source.id}/introspect")).status_code == 409
    assert (await client.post(f"/api/datasources/{source.id}/test")).json()["ok"] is True
    # 手工源不许登记到记录表的文件上
    sneaky = await client.post("/api/datasources", json={"name": f"m_{uuid.uuid4().hex[:8]}", "kind": "sqlite",
                                                         "database": str(path_of(table))})
    assert sneaky.status_code == 422
    # 数据目录照常起草：表、系统列的说明都在
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, await session.get(DataSource, source.id), tables=["records"])
        await session.commit()
        entries = await catalog.read_catalog(session, source.id)
    assert [t.table for t in report.tables] == ["records"] and "records" in entries


def test_stamp_writes_the_high_water_for_records():
    from app.apps.resolve import _version_text

    assert _version_text({"name": "rec_abc", "origin": "records", "seq": 12, "rows": "all"}) == "rec_abc 截至第 12 条"
    assert _version_text({"name": "sales", "snapshot": "0123456789abcdef"}) == "sales 01234567"


# --------------------------------------------------------------------------
# 复核修复：草稿不影响线上、遮罩列不被抹掉、写入的三道核对
# --------------------------------------------------------------------------


async def test_drafting_never_retires_the_live_version_and_publishing_switches():
    """起草（draft_table）：兼容的只补列，不兼容的出下一版、线上那一版不停用；只读的 pick_table 认同一版。发布
    （publish_table）才切换：图里写的那一版成为往里写的，别的没停用的这时停用，登记按卡片整理。"""
    from app.records.tables import draft_table, pick_table, publish_table

    owner = SimpleNamespace(id=uuid.uuid4().hex, app_id=None, name="门店报损")
    async with SessionLocal() as session:
        v1 = await draft_table(session, owner, COLUMNS)
        await session.commit()
    add(v1, {"门店": "一店", "数量": 1})
    async with SessionLocal() as session:
        # 兼容（加可选字段）：同一版补列
        noted = [*COLUMNS, {"name": "备注", "type": "string"}]
        same = await draft_table(session, owner, noted)
        # 不兼容（数量改成文字）：出第 2 版，第 1 版照常收
        changed = [COLUMNS[0], {**COLUMNS[1], "type": "string"}, COLUMNS[2]]
        v2 = await draft_table(session, owner, changed)
        assert (await pick_table(session, owner.id, changed)).id == v2.id
        assert (await pick_table(session, owner.id, noted)).id == v1.id, "线上那一版还兼容的，起草优先用它"
        await session.commit()
    assert same.id == v1.id and v2.version == 2
    async with SessionLocal() as session:
        v1 = await session.get(RecordsTable, v1.id)
        assert v1.retired_at is None and "备注" in [c["name"] for c in v1.columns]
    add(v1, {"门店": "二店", "数量": 2, "备注": "照常写"})

    app = await make_app()
    async with SessionLocal() as session:
        live = await publish_table(session, await session.get(App, app.id), f"{owner.id}.v2", changed)
        await session.commit()
    async with SessionLocal() as session:
        rows = list((await session.execute(select(RecordsTable).where(RecordsTable.table_key == owner.id)
                                            .order_by(RecordsTable.version))).scalars())
    assert live.version == 2 and [r.app_id for r in rows] == [app.id, app.id]
    assert rows[0].retired_at is not None and "第 2 版" in rows[0].note and rows[1].retired_at is None
    assert store.high_water(path_of(rows[0])) == 2, "旧记录照原样留着"


async def test_a_compatible_change_keeps_masks_added_by_hand():
    """兼容的表单改动只改这次变了的那几列的遮罩：管理员在数据页手工加的遮罩列（这里是提交人）留着；取消敏感标记的去掉。"""
    from app.data.engine import MASK_COLUMNS_OPTION

    app = await make_app()
    table = await make_table(app, [*COLUMNS[:2], {**COLUMNS[2], "sensitive": True}])
    async with SessionLocal() as session:
        source = await session.get(DataSource, table.source_id)
        source.options = {MASK_COLUMNS_OPTION: ["原因", "_submitted_by"]}
        await session.commit()
    await make_table(app, [*COLUMNS[:2], {**COLUMNS[2], "sensitive": True}, {"name": "电话", "type": "string",
                                                                             "sensitive": True}])
    assert (await source_of(table)).options[MASK_COLUMNS_OPTION] == ["原因", "_submitted_by", "电话"]
    await make_table(app, [*COLUMNS[:2], COLUMNS[2], {"name": "电话", "type": "string", "sensitive": True}])
    assert (await source_of(table)).options[MASK_COLUMNS_OPTION] == ["_submitted_by", "电话"]


async def test_formal_runs_only_write_their_own_tables_and_only_bind_their_own_files():
    """正式运行只写自己应用的记录表（别的工作流的运行写不进来，records.table_foreign）；写下的行只绑这次运行提交时
    绑上的文件——值里随手写一个别的文件编号不会被挂到这一行上。"""
    from app.db.models import Workflow
    from app.db.models_blueprints import StoredFile
    from app.records.actions import append_action

    app = await make_app()
    table = await make_table(app, [*COLUMNS, {"name": "照片", "label": "照片", "type": "file"}])
    async with SessionLocal() as session:
        mine = Workflow(name=f"报损-{uuid.uuid4().hex[:6]}", graph={"nodes": [], "edges": []}, status="published")
        other = Workflow(name=f"别的-{uuid.uuid4().hex[:6]}", graph={"nodes": [], "edges": []}, status="published")
        session.add_all([mine, other])
        await session.flush()
        row = await session.get(App, app.id)
        row.draft = {"pins": [{"alias": "main", "workflow_id": mine.id, "version": 1, "graph_hash": "x"}]}
        runs = [Run(graph={"nodes": [], "edges": []}, input={}, status="running", run_class="formal", workflow_id=w.id)
                for w in (mine, other)]
        session.add_all(runs)
        await session.flush()
        files = [StoredFile(sha256="ab" * 32, name=f"{n}.png", size=1, mime="image/png", uploaded_by="amy",
                            bound_run_id=bound) for n, bound in (("本次", runs[0].id), ("别人的", None))]
        session.add_all(files)
        await session.commit()
        run_ids, file_ids = [r.id for r in runs], [f.id for f in files]

    def ref(file_id: str) -> dict:
        return {"file_id": file_id, "name": "a.png", "size": 1, "sha256": "ab" * 32, "type": "image/png"}

    refused = await append_action({"table": table.id, "values": {"门店": "一店", "数量": 1}}, idem_key="k:1",
                                  run_id=run_ids[1], on_behalf={"sub": "amy"}, environment="prod")
    assert refused.status is None and "不属于这个应用" in refused.error
    ok = await append_action({"table": table.id, "values": {"门店": "一店", "数量": 1, "照片": ref(file_ids[0])}},
                             idem_key="k2:1", run_id=run_ids[0], on_behalf={"sub": "amy"}, environment="prod")
    stray = await append_action({"table": table.id, "values": {"门店": "一店", "数量": 2, "照片": ref(file_ids[1])}},
                                idem_key="k3:1", run_id=run_ids[0], on_behalf={"sub": "amy"}, environment="prod")
    assert ok.status == 200 and stray.status == 200
    assert store.high_water(path_of(table)) == 2, "别的工作流的那一次一行都没写"
    async with SessionLocal() as session:
        bound, loose = [await session.get(StoredFile, i) for i in file_ids]
    assert bound.bound_record == {"table_id": table.id, "row_id": ok.body["_id"]}
    assert loose.bound_record is None, "不是这次运行提交时绑上的文件不挂到这一行"


async def test_a_correction_only_reaches_rows_the_submitter_can_see(monkeypatch):
    """更正只能更正看得到的那一行：使用者（use）只能更正自己提交的，别人提交的当它不在；查看全部的人都能更正。"""
    from app.records import access
    from app.records.actions import append_action

    app = await make_app(grants={"amy": "use", "cat": "observe"})
    table = await make_table(app)
    bob_row = add(table, {"门店": "二店", "数量": 2}, by="bob")
    amy_row = add(table, {"门店": "一店", "数量": 1}, by="amy")

    async def scope(session, app_id, *, run_class, started_by):
        return {"amy": {"rows": "mine", "viewer": "amy"}, "cat": {"rows": "all"}}.get(started_by, {"rows": "none"})

    monkeypatch.setattr(access, "scope_of_run", scope)

    async def fix(who: str, row_id: str, key: str):
        return await append_action({"table": table.id, "values": {"门店": "一店", "数量": 9}, "supersedes": row_id},
                                   idem_key=f"{key}:1", run_id=None, on_behalf={"sub": who}, environment="prod")

    assert "不在这张记录表里" in (await fix("amy", bob_row.row_id, "a1")).error
    assert (await fix("amy", amy_row.row_id, "a2")).status == 200
    assert (await fix("cat", bob_row.row_id, "c1")).status == 200
    assert "不在这张记录表里" in (await fix("dan", amy_row.row_id, "d1")).error


def correct_graph(table_ref: str) -> dict:
    """写记录表的那一步带上要更正的行（supersedes）。"""
    return chain(node("start", "input", fields=[{"name": "store"}, {"name": "qty"}, {"name": "fix"}]),
                 node("w", "tool", tool=RECORDS_APPEND, approval="never",
                      args={"table": table_ref, "values": {"门店": "{{ input.store }}", "数量": "{{ input.qty }}"},
                            "supersedes": "{{ input.fix }}"}),
                 node("out", "output", fields=[{"name": "id", "value": "{{ nodes.w.receipt._id }}"}]))


async def test_a_correction_keeps_the_submitter_and_records_who_corrected(engine_up):
    """别人代为更正（D6）：更正行的提交人沿用原行的——原提交人按「只看自己的」照样看得见改过的那一行；动手更正的人记进
    动作记录的回执和审计（records.correct），不记进行里。重放同一个动作不再写行、也不再记审计。"""
    from sqlalchemy import func

    from app.capabilities.platform import execute
    from app.capabilities.registry import get_capability
    from app.core.artifact_store import load
    from app.db.models import AuditEvent

    app = await make_app(grants={"amy": "use", "cat": "observe"})
    table = await make_table(app)
    source = await source_of(table)
    original = add(table, {"门店": "一店", "数量": 3}, by="amy")
    workflow_id = await writer_of(app, correct_graph(source.name))
    await _sync()

    run = await run_manager.start(graph=correct_graph(source.name),
                                  input_payload={"store": "一店", "qty": "9", "fix": original.row_id},
                                  run_class="formal", started_by="cat", workflow_id=workflow_id)
    done = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert done.status == "succeeded", done.error
    async with SessionLocal() as session:
        [rec] = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == run.id))).scalars())
        audits = list((await session.execute(select(AuditEvent).where(AuditEvent.action == "records.correct",
                                                                      AuditEvent.subject_id == table.id))).scalars())
    body = load(rec.receipt["artifact"])["body"]
    assert (body["_submitted_by"], body["corrected_by"]) == ("amy", "cat"), "回执里记着谁交的、谁改的"
    assert [(a.actor, a.data["submitted_by"], a.data["supersedes"], a.data["row_id"], a.data["run_id"])
            for a in audits] == [("cat", "amy", original.row_id, body["_id"], run.id)]

    mine, _ = store.rows(path_of(table), columns_of(table), scope="mine", viewer="amy")
    assert [(r["_id"], r["_submitted_by"], r["values"]["数量"]) for r in mine] == [(body["_id"], "amy", 9)], \
        "原提交人看得见改过的那一行"
    assert store.rows(path_of(table), columns_of(table), scope="mine", viewer="cat")[0] == [], "更正的人不算提交人"
    history, _ = store.rows(path_of(table), columns_of(table), scope="all", viewer=None, history=True)
    assert {r["_id"]: r["_run_id"] for r in history}[body["_id"]] == run.id, "这一行出自哪次运行照样查得到"

    async with SessionLocal() as session:
        cap = await get_capability(session, RECORDS_APPEND)
    again = await execute(None, cap, {"table": source.name, "values": {"门店": "一店", "数量": 9},
                                      "supersedes": original.row_id},
                          environment="prod", idem_key=rec.idem_key, on_behalf={"sub": "cat"}, run_id=run.id)
    assert again.status == 200 and again.body["duplicate"] is True and again.body["_submitted_by"] == "amy"
    async with SessionLocal() as session:
        assert int(await session.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "records.correct", AuditEvent.subject_id == table.id)) or 0) == 1

    # 自己交的新一行：提交人就是自己，回执里没有 corrected_by
    fresh = store.append(path_of(table), columns_of(table), {"门店": "二店", "数量": 1}, version=table.version,
                         run_id=None, actor="amy", action_key=uuid.uuid4().hex)
    assert fresh.submitted_by == "amy" and "corrected_by" not in fresh.receipt()


# --------------------------------------------------------------------------
# 跨版本汇总（停用的版本只读挂在连接上，UNION ALL 接上）；停用之前发起的运行照常写完
# --------------------------------------------------------------------------

#: 不兼容的改动：数量从整数放宽成数（都是数，旧记录照样并进来）、原因从文字改成是或否（意思变了，旧记录这一列为空）、
#: 加必填的店长（旧记录没有，为空）
CHANGED = [COLUMNS[0], {**COLUMNS[1], "type": "number"}, {**COLUMNS[2], "type": "boolean"},
           {"name": "店长", "label": "店长", "type": "string", "required": True}]
SUMMARY = 'SELECT count(*) AS n, sum("数量") AS s FROM records'


async def _publish_changed(app: App, columns=CHANGED) -> tuple[RecordsTable, RecordsTable]:
    """草稿出第 2 版（draft_table），发布单切换（publish_table）：第 1 版这时才停用。交回（第 1 版，第 2 版）。"""
    from app.records.tables import draft_table, publish_table

    async with SessionLocal() as session:
        owner = await session.get(App, app.id)
        drafted = await draft_table(session, owner, columns)
        await publish_table(session, owner, f"{drafted.table_key}.v{drafted.version}", columns)
        await session.commit()
        key = drafted.table_key
    async with SessionLocal() as session:
        v1, v2 = list((await session.execute(select(RecordsTable).where(RecordsTable.table_key == key)
                                             .order_by(RecordsTable.version))).scalars())
    return v1, v2


async def test_the_summary_reads_every_version_after_an_incompatible_change(engine_up):
    """表单不兼容地改过并发布之后，看板查新一版的数据源，旧版本的记录照样在汇总里：列照新一版的来，旧版本没有的、类型改得
    对不上的为空，整数放宽成数的照样并进来；各版本各按自己的高水位固定，同一次运行审批前后查到的数一样；挂上去的旧版本和
    main 一样不能直接写库名查；用户 SQL 写 ATTACH 照旧被拒。"""
    from app.apps.resolve import _version_text
    from app.records.views import cache_token

    app = await make_app()
    v1 = await make_table(app)
    first = add(v1, {"门店": "一店", "数量": 2, "原因": "破损"})
    add(v1, {"门店": "一店", "数量": 5, "原因": "破损"}, supersedes=first.row_id)
    add(v1, {"门店": "二店", "数量": 1}, by="bob")
    v1, v2 = await _publish_changed(app)
    assert v1.retired_at is not None and v2.retired_at is None and v2.version == 2
    add(v2, {"门店": "三店", "数量": 7.5, "原因": True, "店长": "钱七"})
    source = await source_of(v2)

    async with SessionLocal() as session:
        now = await resolve(session, source)
    got = await run_query(now, 'SELECT _schema_version AS v, "门店", "数量", "原因", "店长" FROM records '
                               'ORDER BY v, "门店"')
    assert got.rows == [[1, "一店", 5, None, None], [1, "二店", 1, None, None], [2, "三店", 7.5, 1, "钱七"]], \
        "旧版本的记录并进来（更正过的只留最新的一行），对不上的列为空"
    assert (await run_query(now, SUMMARY)).rows == [[3, 13.5]]
    for sql in ("SELECT count(*) FROM records_v1.records", 'SELECT count(*) FROM "records_v1"."records"',
                "SELECT count(*) FROM [RECORDS_V1].records", "SELECT count(*) FROM main.records"):
        with pytest.raises(SqlRejected):
            await run_query(now, sql)
    for sql in (f"ATTACH DATABASE '{path_of(v1)}' AS sneak", "DETACH DATABASE records_v1"):
        with pytest.raises(SqlRejected):
            await run_query(now, sql)
    # 同一个数据源不经解析查（数据剖析这类入口）：连接上挂过的旧版本摘掉了，库名写了也查不到
    with pytest.raises(Exception) as gone:
        await run_query(source, "SELECT count(*) FROM records_v1.records")
    assert "records_v1" in str(gone.value)
    assert [r[1] for r in (await run_query(source, "PRAGMA database_list")).rows] == ["main", "temp"]
    assert (await run_query(now, "PRAGMA database_list")).rows[-1][1] == "records_v1", "再按汇总查又挂上"
    # 行级过滤对每一版都生效
    async with SessionLocal() as session:
        bobs = await resolve(session, source, {"seq": 1, "rows": "mine", "viewer": "bob",
                                               "older": [{"version": 1, "seq": 3}]})
    assert (await run_query(bobs, SUMMARY)).rows == [[1, 1]]

    # 高水位：旧版本同样按发起时固定（停用之前发起的运行还能往旧版本写完）
    tool = f"db_query__{source.name}"
    graph = chain(node("start", "input"), node("t1", "tool", tool=tool, args={"sql": SUMMARY}),
                  node("gate", "human", mode="approve", title="看一眼"),
                  node("t2", "tool", tool=tool, args={"sql": SUMMARY}),
                  node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"},
                                                {"name": "t2", "value": "{{ nodes.t2 }}"}]))
    run = await run_manager.start(graph=graph, input_payload={}, run_class="formal")
    assert (await wait(run.id)).status == "interrupted"
    add(v1, {"门店": "四店", "数量": 4})
    add(v2, {"门店": "五店", "数量": 6, "店长": "孙八"})
    await run_manager.resume(run.id, {"approved": True})
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert json.loads(done.output["t1"])["rows"] == [[3, 13.5]]
    assert json.loads(done.output["t2"])["rows"] == [[3, 13.5]], "审批期间两个版本新来的都没混进这次运行"
    entry = done.data_versions[source.id]
    assert entry["seq"] == 1 and entry["older"] == [{"version": 1, "seq": 3}]
    assert _version_text(entry) == f"{source.name} 截至第 1 条（连同第 1 版截至第 3 条）"
    assert cache_token(entry) != cache_token({**entry, "older": [{"version": 1, "seq": 4}]}), \
        "旧版本多了一行，按需视图的缓存就不再算数"
    again = await run_manager.start(graph=chain(node("start", "input"),
                                                node("t", "tool", tool=tool, args={"sql": SUMMARY}),
                                                node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}])),
                                    input_payload={}, run_class="formal")
    assert json.loads((await wait(again.id, ("succeeded", "failed"))).output["t"])["rows"] == [[5, 23.5]]


async def test_a_lost_old_version_is_named_not_silently_dropped(engine_up):
    """旧版本的数据文件不在了（管理员手工删了）：汇总照常查得出来、只是少了那一版，但不是悄悄少——发起时记进 data_versions 的
    older（missing），印章写明缺了哪一版、数不全；查询快照里同样记着；不在运行里的查询也一样。"""
    from app.apps.resolve import _records_gap, _version_text
    from app.core.artifact_store import load
    from app.db.models import Artifact
    from app.records.views import pin_entry

    app = await make_app()
    v1 = await make_table(app)
    add(v1, {"门店": "一店", "数量": 2})
    v1, v2 = await _publish_changed(app)
    add(v2, {"门店": "三店", "数量": 7.5, "店长": "钱七"})
    source = await source_of(v2)
    path_of(v1).unlink()

    async with SessionLocal() as session:
        entry = await pin_entry(session, source)
        now = await resolve(session, source)
    assert entry["seq"] == 1 and entry["older"] == [{"version": 1, "seq": None, "missing": True}]
    assert now.older == ({"version": 1, "seq": None, "missing": True},) and not now.attach
    assert (await run_query(now, SUMMARY)).rows == [[1, 7.5]], "查得出来，只是没有第 1 版"
    assert _version_text({**entry, "name": source.name}) == f"{source.name} 截至第 1 条"
    assert _records_gap({**entry, "name": source.name}) == \
        f"记录表「{source.name}」第 1 版的数据文件不在了，汇总里没有这一版的记录，数不全"

    tool = f"db_query__{source.name}"
    run = await run_manager.start(graph=chain(node("start", "input"), node("t", "tool", tool=tool, args={"sql": SUMMARY}),
                                              node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}])),
                                  input_payload={}, run_class="formal")
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert json.loads(done.output["t"])["rows"] == [[1, 7.5]]
    pinned = done.data_versions[source.id]
    assert pinned["older"] == [{"version": 1, "seq": None, "missing": True}] and _records_gap(pinned)
    async with SessionLocal() as session:
        snaps = list((await session.execute(select(Artifact).where(Artifact.run_id == run.id,
                                                                   Artifact.kind == "query_snapshot"))).scalars())
    assert snaps and load(snaps[0].id)["records_view"]["older"] == [{"version": 1, "seq": None, "missing": True}]


def test_union_keeps_numbers_widened_and_drops_meanings_that_changed():
    from app.records.store import Column, union_compatible

    assert union_compatible(Column("数量", type="number"), Column("数量", type="integer"))
    assert union_compatible(Column("门店"), Column("门店"))
    assert not union_compatible(Column("原因", type="boolean"), Column("原因"))
    assert not union_compatible(Column("数量"), Column("数量", type="number")), "数改成文字：旧记录这一列为空"


async def test_runs_launched_before_a_version_is_retired_still_write_to_it(engine_up):
    """停用之前发起、审批停在半路的实例，批完照常写进它发起时的那一版（同「进行中的实例照常走完」）；停用之后发起的
    运行不能往停用的版本写，一行都不落。"""
    app = await make_app()
    v1 = await make_table(app)
    source = await source_of(v1)
    graph = chain(node("start", "input", fields=[{"name": "store"}, {"name": "qty"}]),
                  node("gate", "human", mode="approve", title="批一下"),
                  node("w", "tool", tool=RECORDS_APPEND, approval="never",
                       args={"table": source.name, "values": {"门店": "{{ input.store }}", "数量": "{{ input.qty }}"}}),
                  node("out", "output", fields=[{"name": "id", "value": "{{ nodes.w.receipt._id }}"}]))
    workflow_id = await writer_of(app, graph)
    await _sync()
    before = await run_manager.start(graph=graph, input_payload={"store": "一店", "qty": "3"}, run_class="formal",
                                     started_by="amy", workflow_id=workflow_id)
    assert (await wait(before.id)).status == "interrupted"
    v1, v2 = await _publish_changed(app)
    after = await run_manager.start(graph=graph, input_payload={"store": "二店", "qty": "4"}, run_class="formal",
                                    started_by="amy", workflow_id=workflow_id)
    assert (await wait(after.id)).status == "interrupted"
    for run in (before, after):
        await run_manager.resume(run.id, {"approved": True})

    done = await wait(before.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    [row], _ = store.rows(path_of(v1), columns_of(v1), scope="all", viewer=None)
    assert row["_run_id"] == before.id and row["_schema_version"] == 1 and row["values"]["门店"] == "一店"
    assert done.output["id"] == row["_id"]

    late = await wait(after.id, ("succeeded", "failed", "interrupted"))
    assert late.status != "succeeded"
    async with SessionLocal() as session:
        [rec] = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == after.id))).scalars())
    assert rec.state == "failed" and "停用" in (rec.error or ""), (rec.state, rec.error)
    assert store.high_water(path_of(v1)) == 1 and store.high_water(path_of(v2)) == 0, "停用之后发起的一行都没写"
