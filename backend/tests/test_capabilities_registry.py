"""能力登记（应用平台 A6）：同步、治理、快照、作证判定、写入能力的位置、正式运行免审批的只读能力、四条发布检查。

- 同步：主键是图里的工具名；同步得出的效果只说系统知道的（可写数据源为视参数而定，绑定端点的非 GET 为写入）；
  source_hash 没变不动；来源没了记 missing、不删行；
- 平台能力的治理默认：没人治理过的一直跟着默认走，管理员改过的同步不动；
- 治理：version 乐观锁、写审计；只读能力才能免审批；查询能力只能是同一端点上、绑定端点的只读能力；
- 写记忆这类改平台自身状态的内置工具不记成只读（不能免审批、不能当查询能力），照旧能给 Agent 用；
- is_platform_query：数据源查询，和绑定端点、声明了表格、治理后只读的 http 工具；Python 工具写了表格也不算；
- build_tools(allow_write=False) 拒绝写入能力，视参数而定的照旧放行；
- 正式运行：只读 + 免审批的能力直接放行（修 F7），其余照旧问人；总要确认的探索运行里也问；
- 发布检查：action.write_in_agent、action.unattended_write、action.prod_missing、action.reconcile_unplanned。
"""
from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy import delete, select, update

from app.capabilities import registry
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Capability, CustomTool, DataSource, Endpoint, McpServer
from app.records import RECORDS_APPEND, RECORDS_LOOKUP
from app.tools.registry import ToolBuildError, ToolContext, Versions, build_tools
from app.tools.trust import CONFIRM, EXEMPT, FORMAL_MARK, call_policy, run_snapshot
from tests.fakes import auth_env


@pytest.fixture
async def world():
    """一个登记的端点 erp（只有生产环境）、绑定它的读（GET）和写（POST）两个 http 工具、一个 python 工具、
    只读和可写两个数据源、一个缓存了工具清单的 MCP 服务。"""
    async with SessionLocal() as s:
        erp = Endpoint(name="erp", environments={"prod": {"base_url": "https://erp.example.com", "headers": {}}})
        s.add(erp)
        await s.flush()
        s.add_all([
            CustomTool(name="erp_orders", kind="http", parameters={"type": "object", "properties": {}},
                       config={"endpoint": erp.id, "method": "GET", "path": "/orders"}),
            CustomTool(name="erp_pay", kind="http", parameters={"type": "object", "properties": {}},
                       config={"endpoint": erp.id, "method": "POST", "path": "/payments"}),
            CustomTool(name="calc_py", kind="python", parameters={"type": "object", "properties": {}},
                       config={"code": "print(1)"}),
            DataSource(name="sales_ro", kind="sqlite", readonly=True),
            DataSource(name="sales_rw", kind="sqlite", readonly=False),
            McpServer(name="kb", transport="stdio", command="kb-server", tools_cache=["search"]),
        ])
        await s.commit()
        erp_id = erp.id
        await registry.sync_all(s)
    try:
        yield {"erp": erp_id}
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(CustomTool).where(CustomTool.name.in_(["erp_orders", "erp_pay", "calc_py"])))
            await s.execute(delete(DataSource).where(DataSource.name.in_(["sales_ro", "sales_rw"])))
            await s.execute(delete(McpServer).where(McpServer.name == "kb"))
            await s.execute(delete(Endpoint).where(Endpoint.name == "erp"))
            await s.execute(delete(Capability))
            await s.execute(delete(AuditEvent))
            await s.commit()
            await registry.refresh_cache(s)


async def _row(name: str) -> Capability:
    async with SessionLocal() as s:
        row = await s.get(Capability, name)
    assert row is not None, name
    return row


async def test_sync_derives_effects_and_only_touches_what_changed(world):
    effects = {n: (await _row(n)).effect_system for n in (
        "calculator", "shell_exec", "memory_recall", "memory_write", "db_query__sales_ro", "db_query__sales_rw",
        "db_schema__sales_rw", "erp_orders", "erp_pay", "calc_py", "mcp:kb/search")}
    assert effects == {"calculator": "read", "shell_exec": "dynamic", "memory_recall": "read", "memory_write": "dynamic",
                       "db_query__sales_ro": "read", "db_query__sales_rw": "dynamic", "db_schema__sales_rw": "read",
                       "erp_orders": "read", "erp_pay": "write", "calc_py": "dynamic", "mcp:kb/search": "dynamic"}, \
        "写记忆改的是平台自己的状态：不是只读"
    pay = await _row("erp_pay")
    assert (pay.system, pay.endpoint_id) == ("erp", world["erp"])
    async with SessionLocal() as s:
        assert await registry.sync_all(s) == 0, "来源没变不改任何行"
        await s.execute(delete(CustomTool).where(CustomTool.name == "calc_py"))
        await s.commit()
        assert await registry.sync_all(s) == 1
    assert (await _row("calc_py")).missing, "来源没了记 missing、不删行"


async def test_platform_defaults_follow_until_governed(world):
    """在平台默认写好之前就登记了的 records_append（不许无人值守、没有查询能力）：没人治理过的，同步时改回平台默认，
    不然填报登记的每次提交都要等人确认、结果不明时没人核实；管理员治理过的，同步不再动它。"""
    async with SessionLocal() as s:
        await s.execute(update(Capability).where(Capability.name == RECORDS_APPEND)
                        .values(unattended_write=False, lookup=None))
        await s.commit()
        assert await registry.sync_all(s) == 1
    row = await _row(RECORDS_APPEND)
    assert (row.unattended_write, row.lookup, row.version) == (True, RECORDS_LOOKUP, 1)

    c = auth_env.client()
    r = await c.put(f"/api/capabilities/{RECORDS_APPEND}/governance", json={"version": 1, "unattended_write": False})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        assert await registry.sync_all(s) == 0, "管理员改过就听管理员的"
    row = await _row(RECORDS_APPEND)
    assert (row.unattended_write, row.lookup, row.version) == (False, RECORDS_LOOKUP, 2)


async def test_governance_lock_audit_and_rules(world):
    c = auth_env.client()
    row = (await c.get("/api/capabilities/mcp:kb/search")).json()
    assert row["version"] == 1 and row["effect"] is None
    r = await c.put("/api/capabilities/mcp:kb/search/governance",
                    json={"version": 1, "effect": "read", "approval": "none", "masked": ["/phone"]})
    assert r.status_code == 200, r.text
    assert (r.json()["version"], r.json()["effect"], r.json()["approval"]) == (2, "read", "none")
    stale = await c.put("/api/capabilities/mcp:kb/search/governance", json={"version": 1, "approval": "confirm"})
    assert stale.status_code == 409, "读到的是旧版本：别人已经改过"
    bad = await c.put("/api/capabilities/erp_pay/governance", json={"version": 1, "approval": "none"})
    assert bad.status_code == 422, "写入能力不能免审批"
    async with SessionLocal() as s:
        audit = (await s.execute(select(AuditEvent).where(AuditEvent.action == "capability.govern"))).scalar_one()
    assert audit.subject_id == "mcp:kb/search" and audit.data["after"]["approval"] == "none"

    async with SessionLocal() as s:
        snap = await registry.capability_snapshot(s, ["mcp:kb/search", "erp_pay", "nope"])
        versions = await registry.capability_versions_for(s, ["mcp:kb/search", "erp_pay"])
    assert snap["mcp:kb/search"] == {**snap["mcp:kb/search"], "effect": "read", "approval": "none", "version": 2,
                                      "masked": ["/phone"]}
    assert "nope" not in snap and versions == {"mcp:kb/search": 2, "erp_pay": 1}


async def test_lookup_is_a_read_on_the_same_endpoint(world):
    c = auth_env.client()
    for wrong in ("memory_write", "db_query__sales_ro", "mcp:kb/search", "erp_pay", "nope"):
        r = await c.put("/api/capabilities/erp_pay/governance", json={"version": 1, "lookup": wrong})
        assert r.status_code == 422 and "同一端点" in r.json()["detail"], wrong
    r = await c.put("/api/capabilities/erp_pay/governance", json={"version": 1, "lookup": "erp_orders"})
    assert r.status_code == 200 and r.json()["lookup"] == "erp_orders", r.text
    r = await c.put("/api/capabilities/erp_pay/governance", json={"version": 2, "lookup": None})
    assert r.status_code == 200 and r.json()["lookup"] is None, "清掉照旧可以"
    async with SessionLocal() as s:
        await s.execute(update(Capability).where(Capability.name == "memory_write").values(effect="read"))
        await s.commit()
    r = await c.put("/api/capabilities/erp_pay/governance", json={"version": 3, "lookup": "memory_write"})
    assert r.status_code == 422, "治理成只读的内置工具也不是端点上的读取，查不了这一笔写入"
    ctx = ToolContext(run_id="t", node_id="n", data_versions=Versions.CURRENT)
    async with SessionLocal() as s:
        await s.execute(update(Capability).where(Capability.name == "memory_write").values(effect=None))
        await s.commit()
        assert [t.name for t in await build_tools(["memory_write"], ctx, session=s)] == ["memory_write"], \
            "写记忆不是对外写入，Agent 照旧能用"


async def test_tool_library_rehearses_anything_governed_as_a_write(world):
    c = auth_env.client()
    r = await c.put("/api/capabilities/mcp:kb/search/governance", json={"version": 1, "effect": "write"})
    assert r.status_code == 200, r.text
    out = await c.post("/api/tools/mcp:kb/search/run", json={"args": {"q": "x"}})
    assert out.status_code == 200, "演练不发送，没有副作用，不先要确认"
    assert out.json()["rehearsal"] is True and out.json()["result"]["args"] == {"q": "x"}


async def test_tool_list_says_which_tools_only_rehearse(world):
    """工具列表上每个工具带 write_rehearsal（和执行接口同一个判断）：详情区的提示条照它说「这里只演练，不真的发送」，
    不说「请求会实际发出」。绑定端点的 POST 是写入；GET、Python 工具、没治理成写入的内置工具不是。"""
    c = auth_env.client()
    tools = {t["id"]: t for t in (await c.get("/api/tools")).json()}
    assert tools["erp_pay"]["write_rehearsal"] is True
    assert tools["erp_orders"]["write_rehearsal"] is False and tools["calc_py"]["write_rehearsal"] is False
    assert tools["file_write"]["write_rehearsal"] is False, "写工作目录不是对外写入，照旧确认后真的执行"
    r = await c.put("/api/capabilities/http_request/governance", json={"version": 1, "effect": "write"})
    assert r.status_code == 200, r.text
    tools = {t["id"]: t for t in (await c.get("/api/tools")).json()}
    assert tools["http_request"]["write_rehearsal"] is True, "治理成写入的内置工具同样只演练"
    run = await c.post("/api/tools/http_request/run", json={"args": {"url": "https://example.com"}})
    assert run.status_code == 200 and run.json()["rehearsal"] is True, "列表上的标记和执行接口同一个判断"


async def test_platform_query_is_by_identity_not_by_shape(world):
    c = auth_env.client()
    tabular = {"rows": "/items", "columns": {"单号": "/id"}}
    assert not registry.is_platform_query("erp_orders")
    r = await c.put("/api/capabilities/erp_orders/governance", json={"version": 1, "tabular": tabular})
    assert r.status_code == 200, r.text
    assert registry.is_platform_query("erp_orders")
    assert registry.is_platform_query("db_query__sales_ro")
    r = await c.put("/api/capabilities/calc_py/governance", json={"version": 1, "tabular": tabular, "effect": "read"})
    assert r.status_code == 200
    assert not registry.is_platform_query("calc_py"), "用户代码在算的工具写了表格也不能作证"


async def test_write_capabilities_stay_out_of_agents(world):
    ctx = ToolContext(run_id="t", node_id="n", data_versions=Versions.CURRENT)
    async with SessionLocal() as s:
        with pytest.raises(ToolBuildError, match="erp_pay"):
            await build_tools(["erp_pay"], ctx, session=s)
        assert [t.name for t in await build_tools(["erp_pay"], ctx, session=s, allow_write=True)] == ["erp_pay"]
        # 视参数而定的照旧放行：每次调用按参数问审批
        assert len(await build_tools(["db_query__sales_rw", "shell_exec"], ctx, session=s)) == 2


async def test_an_endpoint_call_that_depends_on_the_arguments_counts_as_a_write(world):
    """平台执行器不看参数，分不出这一次读还是写：绑定端点的能力不能治理成视参数而定；登记里已经是视参数而定的（先治理、
    后绑定端点）也按写入对待，Agent 里拒绝。MCP 绑定端点只管连到哪儿，不算。"""
    c = auth_env.client()
    r = await c.put("/api/capabilities/erp_pay/governance", json={"version": 1, "effect": "dynamic"})
    assert r.status_code == 422 and "只读或写入" in r.json()["detail"]
    async with SessionLocal() as s:
        await s.execute(update(Capability).where(Capability.name == "erp_pay").values(effect="dynamic"))
        await s.commit()
        ctx = ToolContext(run_id="t", node_id="n", data_versions=Versions.CURRENT)
        with pytest.raises(ToolBuildError, match="erp_pay"):
            await build_tools(["erp_pay"], ctx, session=s)
    assert registry.writes_externally("erp_pay", "dynamic", world["erp"])
    assert not registry.writes_externally("mcp:kb/search", "dynamic", world["erp"])
    assert not registry.writes_externally("calc_py", "dynamic", None)


class _NoArgs(BaseModel):
    pass


def _tool(name: str, key: str | None = None, **meta) -> StructuredTool:
    async def run() -> str:
        return "ok"

    return StructuredTool(name=name, description="", coroutine=run, func=None, args_schema=_NoArgs,
                          metadata={**({"trust_key": key} if key else {}), **meta})


async def test_formal_runs_skip_approval_only_for_governed_read_only(world):
    c = auth_env.client()
    await c.put("/api/capabilities/mcp:kb/search/governance", json={"version": 1, "effect": "read", "approval": "none"})
    await c.put("/api/capabilities/calc_py/governance", json={"version": 1, "approval": "confirm"})
    async with SessionLocal() as s:
        formal = await run_snapshot(s, run_class="formal")
        exploratory = await run_snapshot(s, run_class="exploratory")
    assert formal == {FORMAL_MARK: "1", "mcp:kb/search": EXEMPT, "calc_py": CONFIRM}
    search, other = _tool("search", "mcp:kb/search"), _tool("lookup", "mcp:kb/lookup")
    assert call_policy(search, "search", {}, formal) == "safe", "只读 + 免审批：正式运行直接放行（F7）"
    assert call_policy(other, "lookup", {}, formal) == "ask", "其余照旧问人"
    calc = _tool("calc_py", "calc_py")
    assert call_policy(calc, "calc_py", {}, {**exploratory, "calc_py": CONFIRM}) == "ask"
    assert call_policy(calc, "calc_py", {}, {"calc_py": "always"}) == "safe", "没治理过的照三档"
    # 按参数判定的工具：登记写了只读，DML 照旧问
    dml = _tool("db_query__sales_rw", dangerous_if=lambda args: True)
    assert call_policy(dml, "db_query__sales_rw", {}, {FORMAL_MARK: "1", "db_query__sales_rw": EXEMPT}) == "ask"


def _spec(*nodes, defaults=None):
    from app.engine.schema import GraphSpec

    graph = {"nodes": [{"id": "in", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "入口", "config": {}}},
                       *nodes], "edges": [], "defaults": defaults or {}}
    return GraphSpec.model_validate(graph)


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _codes(spec, level="published"):
    from app.engine.governance import lint_for_publish

    return {(i.code, i.level) for i in lint_for_publish(spec, level=level).issues if (i.code or "").startswith("action.")}


async def test_publish_lints(world):
    agent = _spec(_node("a", "agent", tools=["erp_pay", "db_query__sales_rw"]))
    assert ("action.write_in_agent", "error") in _codes(agent, "governed")
    assert any(code == "action.write_in_agent" for code, _ in _codes(agent, "published"))
    team = _spec(_node("t", "supervisor", agents=[{"name": "x", "tools": ["erp_pay"]}]))
    assert any(code == "action.write_in_agent" for code, _ in _codes(team, "governed"))

    tool = _spec(_node("w", "tool", tool="erp_pay", approval="never"))
    governed = _codes(tool, "governed")
    assert ("action.unattended_write", "error") in governed
    assert ("action.reconcile_unplanned", "warning") in governed
    assert ("action.unattended_write", "warning") in _codes(tool, "published")
    assert not any(code == "action.prod_missing" for code, _ in governed)

    async with SessionLocal() as s:
        row = await s.get(Endpoint, world["erp"])
        row.environments = {"test": {"base_url": "https://erp-test.example.com", "headers": {}}}
        pay = await s.get(Capability, "erp_pay")
        pay.unattended_write, pay.lookup = True, "erp_orders"
        await s.commit()
        await registry.refresh_cache(s)
    governed = _codes(tool, "governed")
    assert ("action.prod_missing", "error") in governed
    assert not {"action.unattended_write", "action.reconcile_unplanned"} & {c for c, _ in governed}
    assert not _codes(_spec(_node("r", "tool", tool="erp_orders", approval="never")), "governed"), "读取不报"


async def test_snapshots_for_launch_and_publish_never_write(world):
    """发起运行、发布时调用方的会话开着写事务：快照只读，登记表里还没有的名字按系统此刻知道的样子补，不插行。"""
    async with SessionLocal() as s:
        s.add(DataSource(name="sales_new", kind="sqlite", readonly=False))
        await s.flush()   # 调用方自己的写事务还开着
        snap = await registry.capability_snapshot(s, ["db_query__sales_new", "erp_pay"])
        versions = await registry.capability_versions_for(s, ["db_query__sales_new"])
        await s.commit()
    assert snap["db_query__sales_new"] == {**snap["db_query__sales_new"], "effect": "dynamic", "version": 1}
    assert versions == {"db_query__sales_new": 1}
    async with SessionLocal() as s:
        assert await s.get(Capability, "db_query__sales_new") is None, "快照不往登记表里写"
        await s.execute(delete(DataSource).where(DataSource.name == "sales_new"))
        await s.commit()
