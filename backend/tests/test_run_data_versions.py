"""运行固定上传表格的数据版本（期 1 WP-D）。

上传源每次重传是一个新快照。运行发起时把这张图用到的上传源的当前快照记进 runs.data_versions，
之后这次运行里的一切——工具描述、db_schema、冻结的表结构、查询结果——都出自那一版：

- 运行中途重传：审批恢复、失败后继续运行，查到的仍是发起时那一版；
- 固定的快照被回收、文件被删：工具明确报错，绝不改用当前版本；
- 只固定图里（含子工作流、图级 defaults 绑的）实际用到的上传源；工具名带模板时全部固定；手工源不进来；
- 子工作流沿用父运行固定的版本；构造工具时没拿到版本（漏传）的上传源报错，不改用当前版本；
- 发起时没算到的源，第一次用到时补固定、记进运行，并在事件流里留一条日志；
- 调用工具节点遇到数据本身用不了时判失败，报错照交原因，不让人改 SQL；
- 查询快照记下 data_version（快照 id），手工源不记；工具库试用（不在运行里）用当前版本。

夹具：表格在测试里用 openpyxl 现造，标签是假名，数字手写。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import stat
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.api.runs import missing_tools
from app.core import artifact_store
from app.core.config import settings
from app.core.events import EventType
from app.data import table_versions
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, SourceSnapshot, Workflow
from app.engine.context import RunContext
from app.engine.runner import _data_versions, run_manager
from app.engine.schema import GraphSpec
from app.tools.datasource import (
    DATA_UNAVAILABLE,
    LATE_PIN_CODE,
    RUN_VERSIONS_KEY,
    build_datasource_tools,
    run_versions,
)
from app.tools.registry import ToolContext, Versions

SUM = 'SELECT SUM("数量") AS total FROM "明细"'


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    yield data


@pytest.fixture
async def engine_up(store):
    # 依赖 store：checkpoint 库跟着 data_dir 走，要先换好目录再建
    await run_manager.setup()
    yield
    await run_manager.shutdown()


# --------------------------------------------------------------------------
# 夹具：表格、数据源
# --------------------------------------------------------------------------


def book(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


#: 第一版：一张「明细」，数量合计 3
V1 = {"明细": [["区域", "数量"], ["分区甲", 1], ["分区乙", 2]]}
#: 第二版：「明细」合计 100，另多一张「新增」。表名、数字都和第一版不同，串了版本一眼看得出
V2 = {"明细": [["区域", "数量"], ["分区甲", 60], ["分区乙", 40]], "新增": [["时段", "客流"], ["上午", 7]]}


def uniq(prefix: str = "dv") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def publish(name: str, sheets: dict[str, list[list]]) -> tuple[str, str]:
    """发布一版上传表格，返回 (源 id, 快照 id)。源不存在就新建。"""
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(id=uuid.uuid4().hex, name=name, kind="sqlite", readonly=True, origin="upload")
        result = await table_versions.publish_upload(session, row, book(sheets), "客流.xlsx",
                                                     header_row=1, mixed="reject", raw_mode=False)
        return row.id, result.snapshot_id


async def manual_source(tmp_path, total: int) -> tuple[str, str, str]:
    """一个手工登记的 SQLite 源（文件在上传目录外）。返回 (源 id, 源名, 文件路径)。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE "明细" ("区域" TEXT, "数量" INTEGER);')
    db.execute('INSERT INTO "明细" VALUES (?, ?)', ("分区甲", total))
    db.commit()
    db.close()
    name = uniq("manual")
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        return row.id, name, str(path)


async def keep(source_id: str, name: str, snapshot: str) -> str:
    """登记一条引用这个快照的中断运行，让之后重传触发的回收不删它。返回运行 id。"""
    async with SessionLocal() as session:
        run = Run(workflow_name="保留旧版本", status="interrupted",
                  data_versions={source_id: {"snapshot": snapshot, "name": name}})
        session.add(run)
        await session.commit()
        return run.id


async def tools_for(names: list[str], *, data_versions=Versions.CURRENT, run_id: str = "", emit=None) -> dict:
    """建数据源工具。缺省按不在运行里（工具库试用）建，用当前版本。"""
    async with SessionLocal() as session:
        built = await build_datasource_tools(
            names, ToolContext(run_id=run_id, node_id="n", data_versions=data_versions, emit=emit), session)
    return {t.name: t for t in built}


def total_of(text: str) -> object:
    return json.loads(text)["rows"][0][0]


def pinned_of(pins: dict, sid: str) -> str | None:
    return (pins.get(sid) or {}).get("snapshot")


# --------------------------------------------------------------------------
# 跑图的工具
# --------------------------------------------------------------------------


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("interrupted", "failed", "succeeded"), timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row and row.status in statuses and run_manager._tasks.get(run_id) is None:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"等超时了：{run_id}")


async def events_of(run_id: str, kind: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == kind)
        if node_id is not None:
            query = query.where(RunEvent.node_id == node_id)
        return list((await session.execute(query.order_by(RunEvent.seq))).scalars())


async def started_events(run_id: str) -> list[RunEvent]:
    return await events_of(run_id, "run.started")


async def late_pin_events(run_id: str) -> list[RunEvent]:
    return [e for e in await events_of(run_id, "log") if (e.data or {}).get("code") == LATE_PIN_CODE]


def loop_graph(first: str, second: str, condition: str) -> dict:
    """t1 查 first → 循环（条件执行时才报错的话停在这里）→ t2 查 second → 输出两次的合计。"""
    return {
        "nodes": [
            node("start", "input"),
            node("t1", "tool", tool=f"db_query__{first}", args={"sql": SUM}),
            node("lp", "loop", mode="while", condition=condition, max_iterations=1),
            node("body", "transform", mode="template", template="转一圈"),
            node("t2", "tool", tool=f"db_query__{second}", args={"sql": SUM}),
            node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"},
                                          {"name": "t2", "value": "{{ nodes.t2 }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "t1"}, {"source": "t1", "target": "lp"},
            {"source": "lp", "target": "body", "sourceHandle": "body"}, {"source": "body", "target": "lp"},
            {"source": "lp", "target": "t2", "sourceHandle": "done"}, {"source": "t2", "target": "out"},
        ],
    }


#: 过得了静态检查、执行时才报错的循环条件（指数超限）：运行停在循环节点上，失败
FAILS_AT_RUNTIME = "2 ** 100000 > 1"


def output_total(run: Run, field: str) -> object:
    value = (run.output or {})[field]
    return total_of(value if isinstance(value, str) else json.dumps(value))


def scripted_agent(monkeypatch, source: str) -> dict:
    """Agent 先列表结构、再查合计、然后收尾；协作成员（只绑了查询工具）直接查合计。

    记下各自看到的工具描述（按 (谁, 工具名)）和工具结果（按调用 id）。
    """
    from app.providers import mock_model

    seen: dict = {"descriptions": {}, "results": {}}
    routes = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            if "assignments" not in json.dumps(self.response_format, ensure_ascii=False):
                return AIMessage(content="{}")
            # 调度者：第一轮派给成员，第二轮收工
            routes["n"] += 1
            plan = ({"assignments": [{"agent": "analyst", "instruction": "查合计"}]} if routes["n"] == 1
                    else {"assignments": [], "done": True})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        if not self.tools:
            return AIMessage(content="收尾")
        names = {t["name"] for t in self.tools}
        who = "agent" if f"db_schema__{source}" in names else "member"
        for t in self.tools:
            seen["descriptions"][(who, t["name"])] = t["description"]
        done = set()
        for m in messages:
            if isinstance(m, ToolMessage):
                seen["results"][m.tool_call_id] = str(m.content)
                done.add(m.tool_call_id)
        plan = [(f"{who}_schema", f"db_schema__{source}", {}), (f"{who}_sum", f"db_query__{source}", {"sql": SUM})]
        for call_id, name, args in plan:
            if name in names and call_id not in done:
                return AIMessage(content="查一下。", tool_calls=[{"name": name, "args": args, "id": call_id}])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


def json_in(text: str) -> dict:
    """工具结果交给模型时前面可能标了证据编号：从第一个「{」起读。"""
    return json.loads(text[text.index("{"):])


# --------------------------------------------------------------------------
# 发起时固定哪些源
# --------------------------------------------------------------------------


async def test_start_pins_only_the_upload_sources_the_graph_uses(engine_up, monkeypatch, tmp_path):
    used, unused = uniq(), uniq()
    used_id, used_snap = await publish(used, V1)
    unused_id, _ = await publish(unused, V1)
    manual_id, manual, _ = await manual_source(tmp_path, 5)
    scripted_agent(monkeypatch, used)

    graph = chain(
        node("start", "input"),
        node("t1", "tool", tool=f"db_query__{used}", args={"sql": SUM}),
        node("bot", "agent", prompt="查一下", tools=[f"db_schema__{used}", f"db_query__{manual}"], max_steps=3),
        node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={})
    pinned = {used_id: {"snapshot": used_snap, "name": used}}
    assert run.data_versions == pinned
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert done.data_versions == pinned
    assert unused_id not in done.data_versions and manual_id not in done.data_versions
    # run.started 记下固定的版本：封存清单里查得到这次运行用的是哪一版
    assert (await started_events(run.id))[0].data["data_versions"] == pinned


async def test_subgraph_sources_are_pinned_and_templated_names_pin_every_upload_source(tmp_path):
    top, inner, other, off = uniq(), uniq(), uniq(), uniq()
    top_id, top_snap = await publish(top, V1)
    inner_id, inner_snap = await publish(inner, V1)
    other_id, _ = await publish(other, V1)
    off_id, _ = await publish(off, V1)
    _, manual, _ = await manual_source(tmp_path, 5)
    async with SessionLocal() as session:
        (await session.get(DataSource, off_id)).enabled = False
        sub = Workflow(name="子流程", graph=chain(
            node("s", "input"), node("q", "tool", tool=f"db_query__{inner}", args={"sql": SUM}), node("o", "output")))
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    graph = chain(
        node("start", "input"),
        node("t", "tool", tool=f"db_query__{top}", args={"sql": SUM}),
        node("sub", "subgraph", workflow_id=sub_id),
        node("m", "tool", tool=f"db_query__{manual}", args={"sql": SUM}),
        node("out", "output"),
    )
    async with SessionLocal() as session:
        pinned = await _data_versions(session, GraphSpec.model_validate(graph))
    # 子工作流里绑的源也固定；没用到的、手工的不固定
    assert pinned == {top_id: {"snapshot": top_snap, "name": top},
                      inner_id: {"snapshot": inner_snap, "name": inner}}

    # 工具名带模板：发起时说不准会用哪个源，所有启用着的上传源都固定上，停用的、手工的不算
    graph["nodes"][1]["data"]["config"]["tool"] = "db_query__{{ vars.which }}"
    async with SessionLocal() as session:
        everything = await _data_versions(session, GraphSpec.model_validate(graph))
        uploads = {r.id: r.current_snapshot_id for r in (await session.execute(select(DataSource).where(
            DataSource.origin == "upload", DataSource.enabled.is_(True),
            DataSource.current_snapshot_id.is_not(None)))).scalars()}
    assert {sid: v["snapshot"] for sid, v in everything.items()} == uploads
    assert {top_id, inner_id, other_id} <= set(everything) and off_id not in everything

    # 一个上传源都没用到：空 dict（不是 None，None 留给升级前发起的运行）
    async with SessionLocal() as session:
        assert await _data_versions(session, GraphSpec.model_validate(
            chain(node("start", "input"), node("out", "output")))) == {}


# --------------------------------------------------------------------------
# 运行中途重传：恢复、继续运行都还是发起时那一版
# --------------------------------------------------------------------------


async def test_reupload_while_waiting_for_approval_does_not_change_the_run(engine_up, monkeypatch):
    src = uniq()
    sid, s1 = await publish(src, V1)
    seen = scripted_agent(monkeypatch, src)
    graph = chain(
        node("start", "input"),
        node("t1", "tool", tool=f"db_query__{src}", args={"sql": SUM}),
        node("gate", "human", mode="approve", title="看一眼"),
        node("bot", "agent", prompt="查合计", tools=[f"db_schema__{src}", f"db_query__{src}"], max_steps=4),
        node("team", "supervisor", goal="查合计", max_rounds=2,
             agents=[{"name": "analyst", "system": "你是分析员", "tools": [f"db_query__{src}"], "max_steps": 3}]),
        node("t2", "tool", tool=f"db_query__{src}", args={"sql": SUM}),
        node("out", "output", fields=[{"name": "t1", "value": "{{ nodes.t1 }}"},
                                      {"name": "t2", "value": "{{ nodes.t2 }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={})
    assert (await wait(run.id)).status == "interrupted"

    # 停在审批上时重传：当前版本变成 S2（合计 100，多一张「新增」）
    _, s2 = await publish(src, V2)
    assert s2 != s1
    await run_manager.resume(run.id, {"approved": True})
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error

    assert output_total(done, "t1") == 3
    assert output_total(done, "t2") == 3, "恢复之后查到了重传的新版本"
    # 恢复后才建的 Agent 和协作成员的工具：描述、表结构、查询都还是 S1
    for who in ("agent", "member"):
        desc = seen["descriptions"][(who, f"db_query__{src}")]
        assert "明细" in desc and "新增" not in desc, who
    assert "共 1 张表" in seen["results"]["agent_schema"] and "新增" not in seen["results"]["agent_schema"]
    assert json_in(seen["results"]["agent_sum"])["rows"] == [[3]]
    assert json_in(seen["results"]["member_sum"])["rows"] == [[3]]
    # 运行记录和两段 run.started 记的都是 S1
    assert done.data_versions == {sid: {"snapshot": s1, "name": src}}
    assert [e.data["data_versions"] for e in await started_events(run.id)] == [
        {sid: {"snapshot": s1, "name": src}}] * 2


async def test_continue_after_failure_keeps_the_version(engine_up):
    src = uniq()
    sid, s1 = await publish(src, V1)

    run = await run_manager.start(graph=loop_graph(src, src, FAILS_AT_RUNTIME), input_payload={})
    assert (await wait(run.id, ("failed", "succeeded"))).status == "failed"
    await publish(src, V2)

    await run_manager.continue_failed(run.id, graph=loop_graph(src, src, "false"))
    done = await wait(run.id, ("succeeded", "failed"))
    assert done.status == "succeeded", done.error
    assert output_total(done, "t1") == 3
    assert output_total(done, "t2") == 3, "继续运行时改用了重传的新版本"
    assert [e.data["data_versions"] for e in await started_events(run.id)] == [
        {sid: {"snapshot": s1, "name": src}}] * 2


# --------------------------------------------------------------------------
# 工具层：按固定的版本生成、固定的版本没了就报错
# --------------------------------------------------------------------------


async def test_tools_follow_the_pinned_snapshot_and_record_data_version():
    src = uniq()
    sid, s1 = await publish(src, V1)
    await keep(sid, src, s1)
    _, s2 = await publish(src, V2)
    pins = {sid: {"snapshot": s1, "name": src}}
    try:
        tools = await tools_for([f"db_query__{src}", f"db_schema__{src}"], data_versions=pins)
        query, schema = tools[f"db_query__{src}"], tools[f"db_schema__{src}"]
        assert "明细" in query.description and "新增" not in query.description
        listed = await schema.coroutine()
        assert "共 1 张表" in listed and "新增" not in listed
        assert "数量" in await schema.coroutine(table="明细")

        text = await query.coroutine(sql=SUM)
        payload = json.loads(text)
        assert total_of(text) == 3
        # data_version 只进查询快照，不进交给模型的结果
        assert "data_version" not in payload
        assert artifact_store.load(payload["artifact"])["data_version"] == s1
        # 冻结的表结构是 S1 的那份，不是数据源上现在的 S2
        assert set(artifact_store.load(payload["schema_artifact"])["tables"]) == {"明细"}

        # 不在运行里（工具库试用、data_versions 为 None）用当前版本
        current = (await tools_for([f"db_query__{src}"]))[f"db_query__{src}"]
        assert "新增" in current.description
        text = await current.coroutine(sql=SUM)
        assert total_of(text) == 100
        assert artifact_store.load(json.loads(text)["artifact"])["data_version"] == s2
    finally:
        await engines.invalidate(sid)


async def test_pinned_snapshot_collected_by_gc_gives_a_clear_error_and_never_reads_current():
    src = uniq()
    sid, s1 = await publish(src, V1)
    # 没有运行引用 S1：重传后的回收把它标成已回收、删掉文件
    await publish(src, V2)
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, s1)
        assert snap.retired_at is not None and not os.path.exists(snap.db_path)
    try:
        tools = await tools_for([f"db_query__{src}", f"db_schema__{src}"],
                                data_versions={sid: {"snapshot": s1, "name": src}})
        query, schema = tools[f"db_query__{src}"], tools[f"db_schema__{src}"]
        # 描述不列表名：表名是当前版本的
        assert "明细" not in query.description and "新增" not in query.description
        assert "本次运行固定的数据版本已不存在" in query.description
        text = await query.coroutine(sql=SUM)
        assert text.startswith(f"{DATA_UNAVAILABLE}本次运行固定的数据版本已不存在")
        assert "不会改用当前版本" in text and "100" not in text
        # Agent 里这句话作为工具结果交回（tool.end），运行面板按「查询失败」开头把那一步标成失败
        assert text.startswith("查询失败")
        listed = await schema.coroutine()
        assert "本次运行固定的数据版本已不存在" in listed and "新增" not in listed

        # 版本根本不属于这个源（记录被改坏、别的源的快照）也一样
        other = uniq()
        _, foreign = await publish(other, V2)
        tools = await tools_for([f"db_query__{src}"], data_versions={sid: {"snapshot": foreign, "name": src}})
        text = await tools[f"db_query__{src}"].coroutine(sql=SUM)
        assert text.startswith(f"{DATA_UNAVAILABLE}本次运行固定的数据版本已不存在") and "100" not in text
    finally:
        await engines.invalidate(sid)


async def test_snapshot_file_deleted_after_tools_were_built():
    """工具建好之后快照文件被删：缓存的连接还开着已删的文件，也不能拿它接着查。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    await keep(sid, src, s1)
    await publish(src, V2)
    try:
        tools = await tools_for([f"db_query__{src}", f"db_schema__{src}"],
                                data_versions={sid: {"snapshot": s1, "name": src}})
        query = tools[f"db_query__{src}"]
        assert total_of(await query.coroutine(sql=SUM)) == 3      # 连接已建好、进了缓存
        async with SessionLocal() as session:
            path = (await session.get(SourceSnapshot, s1)).db_path
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        os.unlink(path)
        text = await query.coroutine(sql=SUM)
        assert text.startswith(f"{DATA_UNAVAILABLE}本次运行固定的数据版本已不存在") and "100" not in text
        assert (await tools[f"db_schema__{src}"].coroutine()).startswith(
            f"{DATA_UNAVAILABLE}本次运行固定的数据版本已不存在")
    finally:
        await engines.invalidate(sid)


async def test_source_first_used_mid_run_is_pinned_and_recorded():
    """发起时没算到的源（继续运行时改绑、子工作流中途改过）：第一次用到时固定，记进运行。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    async with SessionLocal() as session:
        run = Run(workflow_name="中途补固定", status="running", data_versions={})
        session.add(run)
        await session.commit()
        run_id = run.id
    pins: dict = {}
    seen: list = []
    emit = lambda kind, **data: seen.append((kind, data))  # noqa: E731 - 和 NodeContext.emit 一样是同步的
    try:
        tools = await tools_for([f"db_query__{src}"], data_versions=pins, run_id=run_id, emit=emit)
        assert pins == {sid: {"snapshot": s1, "name": src, "late": True}}
        async with SessionLocal() as session:
            assert (await session.get(Run, run_id)).data_versions == pins
        # 补固定这件事本身进事件流：当次这一段的 run.started 里没有这个源
        [(kind, data)] = seen
        assert kind == EventType.LOG and data["code"] == LATE_PIN_CODE
        assert (data["source_id"], data["snapshot"], data["late"]) == (sid, s1, True)
        assert src in data["message"]
        # 之后重传：同一段执行里（同一个 dict）、恢复后（从运行记录读回）都还是 S1
        await publish(src, V2)
        assert total_of(await tools[f"db_query__{src}"].coroutine(sql=SUM)) == 3
        again = await tools_for([f"db_query__{src}"], data_versions=pins, run_id=run_id, emit=emit)
        assert len(seen) == 1, "已经固定过的源不该再发一遍"
        assert total_of(await again[f"db_query__{src}"].coroutine(sql=SUM)) == 3
        async with SessionLocal() as session:
            stored = dict((await session.get(Run, run_id)).data_versions)
        resumed = await tools_for([f"db_query__{src}"], data_versions=stored, run_id=run_id)
        assert total_of(await resumed[f"db_query__{src}"].coroutine(sql=SUM)) == 3
    finally:
        await engines.invalidate(sid)


async def test_late_pin_takes_what_the_run_record_already_has():
    """运行记录里已经有这个源（上一段执行补固定的），内存里还没有：认运行记录的，不取当前版本。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    async with SessionLocal() as session:
        run = Run(workflow_name="已记过", status="running",
                  data_versions={sid: {"snapshot": s1, "name": src, "late": True}})
        session.add(run)
        await session.commit()
        run_id = run.id
    await publish(src, V2)
    pins: dict = {}
    seen: list = []

    async def emit(kind, **data):   # 协程函数也认
        seen.append((kind, data))

    try:
        tools = await tools_for([f"db_query__{src}"], data_versions=pins, run_id=run_id, emit=emit)
        assert pinned_of(pins, sid) == s1
        assert seen == [], "上一段执行补固定时已经发过事件"
        assert total_of(await tools[f"db_query__{src}"].coroutine(sql=SUM)) == 3
    finally:
        await engines.invalidate(sid)


async def test_manual_sources_are_not_versioned(tmp_path):
    sid, name, path = await manual_source(tmp_path, 5)
    pins: dict = {}
    try:
        tools = await tools_for([f"db_query__{name}"], data_versions=pins)
        text = await tools[f"db_query__{name}"].coroutine(sql=SUM)
        assert total_of(text) == 5
        assert "data_version" not in artifact_store.load(json.loads(text)["artifact"])
        assert pins == {}, "手工源不该进 data_versions"
        # 手工源查的就是那个文件此刻的内容
        db = sqlite3.connect(path)
        db.execute('UPDATE "明细" SET "数量" = 9')
        db.commit()
        db.close()
        assert total_of(await tools[f"db_query__{name}"].coroutine(sql=SUM)) == 9
    finally:
        await engines.invalidate(sid)


# --------------------------------------------------------------------------
# 子工作流、漏传版本、调用工具节点的报错、图级 defaults、补固定的事件
# --------------------------------------------------------------------------


async def test_subgraph_resumed_after_approval_reads_the_pinned_version(engine_up):
    """子工作流的工具经它自己的 RunContext 拿版本：父运行停在审批时重传，恢复后子工作流查的仍是 S1。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    async with SessionLocal() as session:
        sub = Workflow(name="子流程取数", graph=chain(
            node("s", "input"),
            node("q", "tool", tool=f"db_query__{src}", args={"sql": SUM}),
            node("o", "output", fields=[{"name": "t", "value": "{{ nodes.q }}"}])))
        session.add(sub)
        await session.commit()
        sub_id = sub.id
    graph = chain(
        node("start", "input"),
        node("gate", "human", mode="approve", title="看一眼"),
        node("sub", "subgraph", workflow_id=sub_id),
        node("out", "output", fields=[{"name": "sub", "value": "{{ nodes.sub.output.t }}"}]),
    )
    try:
        run = await run_manager.start(graph=graph, input_payload={})
        assert run.data_versions == {sid: {"snapshot": s1, "name": src}}
        assert (await wait(run.id)).status == "interrupted"

        _, s2 = await publish(src, V2)
        assert s2 != s1
        await run_manager.resume(run.id, {"approved": True})
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        assert output_total(done, "sub") == 3, "子工作流恢复后查到了重传的新版本"
        payload = json.loads((done.output or {})["sub"])
        assert artifact_store.load(payload["artifact"])["data_version"] == s1
        # 拿到的是发起时固定的那份，不是第一次用到时才补的
        assert done.data_versions == {sid: {"snapshot": s1, "name": src}}
        assert await late_pin_events(run.id) == []
    finally:
        await engines.invalidate(sid)


async def test_tools_built_without_versions_refuse_upload_sources(tmp_path):
    """构造 ToolContext 时没给版本（在运行里漏传了）：上传源报错，不当成「不在运行里」改用当前版本。"""
    src = uniq()
    sid, _ = await publish(src, V1)
    spec = GraphSpec.model_validate(chain(node("start", "input"), node("out", "output")))
    # 运行上下文里没有这个键（不是 runner 建的，或往下传时丢了）：UNSET，不是 None
    assert run_versions(RunContext(run_id="r", thread_id="r", spec=spec)) is Versions.UNSET
    assert run_versions(RunContext(run_id="r", thread_id="r", spec=spec, extra={RUN_VERSIONS_KEY: None})) is None
    pins: dict = {}
    assert run_versions(RunContext(run_id="r", thread_id="r", spec=spec, extra={RUN_VERSIONS_KEY: pins})) is pins
    assert ToolContext().data_versions is Versions.UNSET

    manual_id, manual, _ = await manual_source(tmp_path, 5)
    try:
        async with SessionLocal() as session:
            built = {t.name: t for t in await build_datasource_tools(
                [f"db_query__{src}", f"db_schema__{src}", f"db_query__{manual}"],
                ToolContext(run_id="r", node_id="n"), session)}
        query, schema = built[f"db_query__{src}"], built[f"db_schema__{src}"]
        assert "没有拿到本次运行固定的数据版本" in query.description and "明细" not in query.description
        text = await query.coroutine(sql=SUM)
        assert text.startswith(f"{DATA_UNAVAILABLE}没有拿到本次运行固定的数据版本") and "rows" not in text
        assert (await schema.coroutine()).startswith(f"{DATA_UNAVAILABLE}没有拿到本次运行固定的数据版本")
        # 手工源不分版本，照常查
        assert total_of(await built[f"db_query__{manual}"].coroutine(sql=SUM)) == 5
    finally:
        await engines.invalidate(sid)
        await engines.invalidate(manual_id)


@pytest.mark.parametrize("prefix", ["db_query__", "db_schema__"])
async def test_tool_node_fails_with_the_reason_when_its_pinned_snapshot_is_gone(engine_up, prefix):
    """停在审批时重传、再删掉 S1 的文件：恢复后调用工具节点判失败，报错照交原因，不让人改 SQL。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    tool = f"{prefix}{src}"
    graph = chain(
        node("start", "input"),
        node("gate", "human", mode="approve", title="看一眼"),
        node("t", "tool", tool=tool, args={"sql": SUM} if prefix == "db_query__" else {}),
        node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}]),
    )
    try:
        run = await run_manager.start(graph=graph, input_payload={})
        assert (await wait(run.id)).status == "interrupted"
        await publish(src, V2)      # 这次运行引用着 S1，回收不删它
        async with SessionLocal() as session:
            path = (await session.get(SourceSnapshot, s1)).db_path
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        os.unlink(path)

        await run_manager.resume(run.id, {"approved": True})
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "failed"
        error = done.error or ""
        assert f"工具 {tool} 无法使用：本次运行固定的数据版本已不存在（数据源「{src}」）" in error, error
        assert "请重新发起运行" in error
        # 改 SQL 没用，不能这么指；原话的开头也不能和外面那层叠成两遍
        assert "SQL" not in error and "查询失败" not in error and error.count(DATA_UNAVAILABLE) == 0, error
        [err] = await events_of(run.id, "tool.error", "t")
        assert err.data["error"].startswith(f"{DATA_UNAVAILABLE}本次运行固定的数据版本已不存在")
        assert not await events_of(run.id, "tool.end", "t")
    finally:
        await engines.invalidate(sid)


async def test_tools_bound_through_graph_defaults_are_pinned_at_start():
    """Agent 节点没写 tools、协作节点没写成员时运行时取图级 defaults，发起时固定版本也按它算。"""
    agent_src, member_src = uniq(), uniq()
    agent_id, agent_snap = await publish(agent_src, V1)
    member_id, member_snap = await publish(member_src, V1)
    graph = chain(node("start", "input"), node("bot", "agent", prompt="查一下"),
                  node("team", "supervisor", goal="查合计"), node("out", "output"))
    graph["defaults"] = {"tools": [f"db_query__{agent_src}"],
                         "agents": [{"name": "analyst", "tools": [f"db_schema__{member_src}"]}]}
    async with SessionLocal() as session:
        assert await _data_versions(session, GraphSpec.model_validate(graph)) == {
            agent_id: {"snapshot": agent_snap, "name": agent_src},
            member_id: {"snapshot": member_snap, "name": member_src}}

        # 发起前检查是同一个取法：defaults 里绑的工具不存在，发起前就拦下
        ghost = uniq("ghost")
        broken = {**graph, "defaults": {"tools": [f"db_query__{ghost}"]}}
        missing = await missing_tools(session, GraphSpec.model_validate(broken))
        assert [(name, where) for _, name, where in missing] == [(f"db_query__{ghost}", "data")]

        # 节点上写了（哪怕是空列表）就不看 defaults：和 NodeContext.cfg 一样
        graph["nodes"][1]["data"]["config"]["tools"] = []
        assert set(await _data_versions(session, GraphSpec.model_validate(graph))) == {member_id}


async def test_source_first_used_after_continue_is_pinned_and_leaves_an_event(engine_up):
    """继续运行时改绑了发起时没用到的源：第一次用到时补固定，记进运行，事件流里留一条日志。"""
    first, later = uniq(), uniq()
    first_id, first_snap = await publish(first, V1)
    later_id, later_snap = await publish(later, V1)
    try:
        run = await run_manager.start(graph=loop_graph(first, first, FAILS_AT_RUNTIME), input_payload={})
        failed = await wait(run.id, ("failed", "succeeded"))
        assert failed.status == "failed"
        assert failed.data_versions == {first_id: {"snapshot": first_snap, "name": first}}

        await run_manager.continue_failed(run.id, graph=loop_graph(first, later, "false"))
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        assert done.data_versions[later_id] == {"snapshot": later_snap, "name": later, "late": True}
        # 这一段的 run.started 里没有它（发起这一段时还没用到）：补固定的那条日志是事件流里唯一的记录
        assert later_id not in (await started_events(run.id))[-1].data["data_versions"]
        [event] = await late_pin_events(run.id)
        assert event.node_id == "t2"
        assert (event.data["source_id"], event.data["snapshot"], event.data["late"]) == (later_id, later_snap, True)
    finally:
        await engines.invalidate(first_id)
        await engines.invalidate(later_id)


async def test_continue_after_the_pinned_source_is_deleted_and_recreated_under_the_same_name(engine_up):
    """固定的源被删、又用同一个名字新建：续跑报错，不改查同名的新源，也不把新源当成中途新建的补固定。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    src = uniq()
    a_id, s1 = await publish(src, V1)
    b_id = None
    try:
        run = await run_manager.start(graph=loop_graph(src, src, FAILS_AT_RUNTIME), input_payload={})
        failed = await wait(run.id, ("failed", "succeeded"))
        assert failed.status == "failed"
        assert failed.data_versions == {a_id: {"snapshot": s1, "name": src}}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.delete(f"/api/datasources/{a_id}")).status_code == 204
        b_id, _ = await publish(src, V2)
        assert b_id != a_id

        await run_manager.continue_failed(run.id, graph=loop_graph(src, src, "false"))
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "failed", f"续跑改查了同名重建的新源：{done.output}"
        error = done.error or ""
        assert f"本次运行固定的数据源「{src}」已被删除或重建，不会改用同名的新数据源" in error, error
        assert "SQL" not in error and "100" not in error
        # 新源没有被补固定进运行记录，也没有补固定的日志
        assert set(done.data_versions) == {a_id}
        assert not await late_pin_events(run.id)
    finally:
        await engines.invalidate(a_id)
        if b_id:
            await engines.invalidate(b_id)


async def test_same_name_manual_source_does_not_stand_in_for_a_pinned_upload_source(tmp_path):
    """同名重建成手工源也拦住：手工源不分版本，但运行固定的是另一个源，不能拿它顶上。"""
    _, manual, _ = await manual_source(tmp_path, 5)
    pins = {"gone-source-id": {"snapshot": "0" * 64, "name": manual}}
    tools = await tools_for([f"db_query__{manual}", f"db_schema__{manual}"], data_versions=pins, run_id="r")
    text = await tools[f"db_query__{manual}"].coroutine(sql=SUM)
    assert text.startswith(f"{DATA_UNAVAILABLE}本次运行固定的数据源「{manual}」已被删除或重建"), text
    assert "5" not in text.replace(manual, "")
    assert (await tools[f"db_schema__{manual}"].coroutine()).startswith(DATA_UNAVAILABLE)
    # 运行里没固定过这个名字的手工源照常查
    tools = await tools_for([f"db_query__{manual}"], data_versions={}, run_id="r")
    assert total_of(await tools[f"db_query__{manual}"].coroutine(sql=SUM)) == 5


def _tamper(path: str) -> None:
    """改掉库文件末尾一个字节（发布后是 0444，先放开写）。"""
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    with open(path, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last[0] ^ 0xFF]))


async def test_tampered_snapshot_is_reported_as_data_unavailable_not_as_a_sql_error(engine_up):
    """快照文件被改过（哈希对不上）：工具按「数据不可用」交回，调用工具节点不让人去改 SQL。"""
    src = uniq()
    sid, s1 = await publish(src, V1)
    graph = chain(node("start", "input"), node("t", "tool", tool=f"db_query__{src}", args={"sql": SUM}),
                  node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}]))
    try:
        async with SessionLocal() as session:
            path = (await session.get(SourceSnapshot, s1)).db_path
        await engines.invalidate(sid)       # 下次查询新建引擎，建之前核对哈希
        _tamper(path)

        for pins in (Versions.CURRENT, {sid: {"snapshot": s1, "name": src}}):
            tools = await tools_for([f"db_query__{src}"], data_versions=pins, run_id="r")
            text = await tools[f"db_query__{src}"].coroutine(sql=SUM)
            assert text.startswith(f"{DATA_UNAVAILABLE}数据源「{src}」的数据文件与登记的版本不一致"), text
            assert "SQL" not in text and "请联系管理员" in text

        run = await run_manager.start(graph=graph, input_payload={})
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "failed"
        error = done.error or ""
        assert error.startswith(f"工具 db_query__{src} 无法使用：数据源「{src}」的数据文件与登记的版本不一致"), error
        assert "SQL" not in error and "查询失败" not in error, error
    finally:
        await engines.invalidate(sid)


async def test_reupload_while_a_run_is_being_started_does_not_collect_its_snapshot(engine_up, monkeypatch):
    """发起运行读完指针、运行记录还没提交时有人重传：发布后的回收看不到这条运行，以前会把刚固定的快照删掉，
    这次运行只能报「固定的数据版本已不存在」。现在读指针到提交持着版本存储的锁，回收等它提交之后才开始。"""
    from app.engine import runner

    src = uniq()
    sid, s1 = await publish(src, V1)
    real = runner._data_versions
    pending: list[asyncio.Task] = []

    async def racing(session, spec):
        pins = await real(session, spec)
        # 读完指针的这一刻有人重传。给它足够的时间做完（没有锁的话它会做完，连同回收）
        task = asyncio.create_task(publish(src, V2))
        pending.append(task)
        await asyncio.wait({task}, timeout=0.5)
        return pins

    monkeypatch.setattr(runner, "_data_versions", racing)
    graph = chain(node("start", "input"), node("t", "tool", tool=f"db_query__{src}", args={"sql": SUM}),
                  node("out", "output", fields=[{"name": "t", "value": "{{ nodes.t }}"}]))
    try:
        run = await run_manager.start(graph=graph, input_payload={})
        _, s2 = await pending[0]
        assert run.data_versions == {sid: {"snapshot": s1, "name": src}} and s2 != s1
        async with SessionLocal() as session:
            snap = await session.get(SourceSnapshot, s1)
            assert snap.retired_at is None and os.path.exists(snap.db_path), "回收删掉了刚固定的快照"
        done = await wait(run.id, ("succeeded", "failed"))
        assert done.status == "succeeded", done.error
        assert output_total(done, "t") == 3
    finally:
        await engines.invalidate(sid)


async def test_late_pin_reads_the_pointer_again_under_the_lock(monkeypatch):
    """补固定时数据源记录是建工具前读的：那之后重传过、旧快照已回收，不能拿旧指针去固定。
    在版本存储的锁里重新读指针，固定的是此刻的当前版本，而且回收不会在写进运行记录之前删掉它。"""
    from app.tools import datasource

    src = uniq()
    sid, s1 = await publish(src, V1)
    async with SessionLocal() as session:
        run = Run(workflow_name="补固定撞上重传", status="running", data_versions={})
        session.add(run)
        await session.commit()
        run_id = run.id
    real = datasource._late_lock
    reuploaded: list[str] = []

    class Racing:
        """进补固定的锁之前，先让一次重传整个做完（发布、切指针、回收掉没人引用的 S1）。"""

        async def __aenter__(self):
            reuploaded.append((await publish(src, V2))[1])
            self.lock = real()
            await self.lock.acquire()

        async def __aexit__(self, *exc):
            self.lock.release()

    monkeypatch.setattr(datasource, "_late_lock", lambda: Racing())
    pins: dict = {}
    try:
        tools = await tools_for([f"db_query__{src}"], data_versions=pins, run_id=run_id)
        [s2] = reuploaded
        assert pinned_of(pins, sid) == s2, "补固定用了建工具前读到的旧指针"
        text = await tools[f"db_query__{src}"].coroutine(sql=SUM)
        assert not text.startswith(DATA_UNAVAILABLE), text
        assert total_of(text) == 100
        async with SessionLocal() as session:
            assert pinned_of((await session.get(Run, run_id)).data_versions, sid) == s2
    finally:
        await engines.invalidate(sid)
