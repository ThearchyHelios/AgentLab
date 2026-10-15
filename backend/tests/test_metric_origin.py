"""口径卡的输入追没追到原始数据（engine/lineage.py）：一处认定，运行时、证据面板、应用检查三处用。

- 「有出处」说的是数由口径卡确定性算出、复算一致；「追到原始数据」说的是输入一路对到查询快照里的格（或运行输入）。
  证据接口的指标步骤另给 untraced：追不到的输入停在哪个节点（名字、类型），面板据此照实说，不能只写「有出处」
- 数据整形只是原样解析调用工具节点的结果（模板整串就是 {{ nodes.fetch }}）时，vars.q.rows[i][j] 穿过它接上快照；
  表达式整形、模板带过滤器、包了一层的都不算
- 受管级应用里绑定的指标，输入追不到原始数据的，应用检查给提示（app.metric_untraced，警告）；已发布级不提示

库和数字都是编的。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.apps.lint import _Issues, _lint_components
from app.apps.tree import node_map
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.lineage import RowCell, Stop, parsed_tool, row_cell, static_stops, traced, untraced
from app.engine.runner import run_manager
from app.main import app

SQL = "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path, want", [
    ("vars.q.rows[0][0]", RowCell("vars", "q", 0, 0)),
    ("nodes.parse.rows[2]['gmv']", RowCell("nodes", "parse", 2, "gmv")),
    ("vars.q.rows[-1][0]", None),            # 负数下标：数的是取回的最后一行，对不到确定的行号
    ("vars.q.rows[vars.i][0]", None),        # 下标是变量
    ("vars.q.rows[0]", None),                # 整行
    ("vars.w.res.rows[0][0]", None),         # 多包了一层
    ("cell(nodes.fetch, 0, 'gmv')", None),
    ("input.week", None),
])
def test_row_cell(path, want):
    assert row_cell(path) == want


@pytest.mark.parametrize("config, want", [
    ({"mode": "json", "template": "{{ nodes.fetch }}"}, "fetch"),
    ({"mode": "json", "template": "  {{nodes.fetch}}\n"}, "fetch"),
    ({"mode": "json", "template": "{{ nodes.fetch | trim }}"}, None),      # 带过滤器：不算原样
    ({"mode": "json", "template": '{"res": {{ nodes.fetch }}}'}, None),   # 包了一层
    ({"mode": "json", "template": "{{ nodes.note }}"}, None),             # 不是调用工具节点
    ({"mode": "template", "template": "{{ nodes.fetch }}"}, None),
    ({"mode": "expression", "expression": "nodes.fetch"}, None),
])
def test_parsed_tool(config, want):
    types = {"fetch": "tool", "note": "llm"}
    assert parsed_tool("transform", config, types.get) == want
    assert parsed_tool("code", config, types.get) is None


def test_traced_and_untraced():
    cell = {"path": "cell(nodes.fetch, 0, 'gmv')", "value": 1, "via": "tool_cell", "artifact": "a", "status": "verified"}
    mismatch = {**cell, "status": "mismatch"}                    # 对得到快照，只是值不一样：另有说法，出处是追到了的
    lost = {**cell, "status": "unresolved", "artifact": None}    # 找不到快照
    field = {"path": "vars.kpi.gmv", "value": 2, "via": "agent_field", "artifact": "a", "status": "verified"}
    run_input = {"path": "input.target", "value": 3, "via": "input", "status": "ok"}
    shaped = {"path": "vars.t.x", "value": 4, "node_id": "mid", "via": "transform", "status": "ok"}
    empty = {"path": "vars.t.y", "value": None, "node_id": "mid", "via": "transform", "status": "missing"}
    assert [traced(i) for i in (cell, mismatch, lost, field, run_input, shaped)] == [True, True, False, True, True, False]
    # 没有值的不算在追不到里：缺输入另有说法
    assert untraced([cell, lost, field, run_input, shaped, empty, "坏数据"]) == [lost, shaped]


def graph_nodes():
    return node_map(chain(
        node("start", "input", "开始", fields=[{"name": "target"}]),
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", "解析", mode="json", template="{{ nodes.fetch }}", assign_to="q"),
        node("raw_n", "transform", "解析（过滤器）", mode="json", template="{{ nodes.fetch | trim }}", assign_to="raw"),
        node("calc", "transform", "加工", mode="expression", expression="{'x': 1}", assign_to="t"),
        node("bot", "agent", "取数助手", output_schema={"type": "object"}, cite_fields=True, assign_to="kpi"),
        node("loose", "agent", "没核对的助手", output_schema={"type": "object"}, assign_to="guess"),
        node("box", "code", "沙箱", evidence_role="source", assign_to="c"),
        node("card", "metrics", "口径卡", metrics=[]),
    ))


@pytest.mark.parametrize("expression, stops", [
    ("cell(nodes.fetch, 0, 'gmv')", []),
    ("cell(vars.q, 0, 'gmv') + vars.q.rows[0][1]", []),            # 原样解析的结果：按格、按位置都追到快照
    ("nodes.parse.rows[0]['gmv']", []),
    ("input.target + vars.target", []),                           # 运行输入
    ("vars.kpi.gmv + nodes.bot.data.gmv", []),                    # 开了按出处核对字段的 Agent
    ("vars.raw.rows[0][0]", [Stop("vars.raw.rows[0][0]", "raw_n")]),
    ("cell(vars.t, 0, 0)", [Stop("cell(vars.t, 0, 0)", "calc")]),
    ("vars.t.x * 2", [Stop("vars.t.x", "calc")]),
    ("vars.guess.gmv", [Stop("vars.guess.gmv", "loose")]),
    ("vars.c.gmv", [Stop("vars.c.gmv", "box")]),                  # 沙箱取数核对不到快照
    ("vars.nowhere + 1", [Stop("vars.nowhere", None)]),
    ("vars.t.x +", []),                                           # 写错了的表达式由画布的校验报，这里不报
])
def test_static_stops(expression, stops):
    assert static_stops(graph_nodes(), expression) == stops


# --------------------------------------------------------------------------
# 证据接口：指标步骤说追没追到原始数据
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL);")
        db.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 100.5), (2, 200.0), (3, 300.0)])
    name = f"origin_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


async def finish(graph: dict) -> Run:
    run = await run_manager.start(graph=graph, input_payload={"target": 5})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"运行 {run.id} 没跑完：{row.status}")


async def test_the_metric_step_says_how_far_the_inputs_were_traced(client, shop):
    graph = chain(
        node("start", "input", "开始", fields=[{"name": "target"}]),
        node("fetch", "tool", "取数", tool=f"db_query__{shop}", args={"sql": SQL}),
        node("parse", "transform", "解析查询结果", mode="json", template="{{ nodes.fetch }}", assign_to="q"),
        node("raw_n", "transform", "整理查询结果", mode="json", template="{{ nodes.fetch | trim }}", assign_to="raw"),
        node("card", "metrics", "口径卡", caliber="口径", metrics=[
            {"id": "gmv", "name": "销售额", "decimals": 2, "expression": "vars.q.rows[0][0]"},
            {"id": "raw", "name": "销售额（过滤器）", "decimals": 2, "expression": "vars.raw.rows[0][0] + input.target"},
            {"id": "one", "name": "常量", "expression": "1"},
        ]),
        node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]),
    )
    row = await finish(graph)
    assert row.status == "succeeded", row.error

    gmv = (await client.get(f"/api/runs/{row.id}/evidence/metrics/card/gmv")).json()["chain"]
    step = gmv[0]
    assert step["step"] == "metric" and step["recompute_ok"] is True and step["untraced"] == []
    [inp] = [s for s in gmv if s["step"] == "input"]
    assert (inp["via"], inp["through"], inp["through_label"], inp["status"]) == ("tool_cell", "parse", "解析查询结果",
                                                                                 "verified")
    # 穿过整形节点接上了查询快照：链的最后一步是那次查询，高亮那一格
    assert gmv[-1]["step"] == "query" and gmv[-1]["highlight"]["cells"] == [[0, "gmv"]]

    raw = (await client.get(f"/api/runs/{row.id}/evidence/metrics/card/raw")).json()["chain"]
    # 复算照样一致（有出处），可输入停在了整形节点：运行输入那一项追到了，不列
    assert raw[0]["recompute_ok"] is True
    assert raw[0]["untraced"] == [{"path": "vars.raw.rows[0][0]", "node_id": "raw_n", "label": "整理查询结果",
                                   "type": "transform"}]
    assert not [s for s in raw if s["step"] == "query"]

    # 没有输入的常量：没有可追的，不给这个键
    one = (await client.get(f"/api/runs/{row.id}/evidence/metrics/card/one")).json()["chain"]
    assert "untraced" not in one[0]


# --------------------------------------------------------------------------
# 应用检查：受管级应用里绑定的指标
# --------------------------------------------------------------------------


def app_spec() -> dict:
    return {
        "format": "agentlab.app/1", "name": "看板", "pins": [{"alias": "main", "workflow_id": "wf", "version": 1,
                                                             "graph_hash": "h", "level": "governed", "use": "view"}],
        "views": {"latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}},
        "pages": [{"id": "home", "title": "首页", "body": [
            {"id": "m_gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "latest", "metric": "card.gmv"}},
            {"id": "m_raw", "type": "metric", "props": {"label": "整理过的"}, "bind": {"view": "latest", "metric": "card.raw"}},
            {"id": "s_raw", "type": "series", "props": {"label": "趋势"}, "bind": {"view": "latest", "series": "card.raw"}},
        ]}],
    }


def pinned_graph() -> dict:
    return chain(
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", "解析查询结果", mode="json", template="{{ nodes.fetch }}", assign_to="q"),
        node("calc", "transform", "加工", mode="expression", expression="{'gmv': 1}", assign_to="t"),
        node("card", "metrics", "口径卡", metrics=[
            {"id": "gmv", "name": "销售额", "expression": "vars.q.rows[0][0] + cell(nodes.fetch, 0, 'orders')"},
            {"id": "raw", "name": "加工后的销售额", "expression": "vars.t.gmv * 2"},
        ]),
    )


def lint(level: str) -> list[dict]:
    spec = app_spec()
    pins = {"main": SimpleNamespace(row=object(), graph=pinned_graph(), level=level, label="周报 v1")}
    out = _Issues()
    _lint_components(spec, pins, spec["views"], level, out)
    return [i for i in out.items if i["code"] == "app.metric_untraced"]


def test_governed_apps_flag_metrics_whose_inputs_stop_halfway():
    found = lint("governed")
    assert [(i["path"], i["level"], i["node_id"]) for i in found] == [
        ("pages[0].body[1].bind", "warning", "card"), ("pages[0].body[2].bind", "warning", "card")]
    message = found[0]["message"]
    assert "「加工后的销售额」" in message and "vars.t.gmv 停在「加工」（数据整形）" in message and "cell()" in message
    # 原样解析的按位置取格、cell() 取的格都追到了快照：销售额不提示
    assert not [i for i in found if i["path"].startswith("pages[0].body[0]")]


def test_published_apps_are_not_flagged():
    assert lint("published") == []
