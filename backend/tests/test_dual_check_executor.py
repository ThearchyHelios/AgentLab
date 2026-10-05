"""单独调用双盲复算的执行器（不在图里执行：@task 直接执行，once 直接发），测图里不好凑的几种情形。

- 盲算模型和产出原数据的 Agent 是同一个模型时提醒；调用工具节点写死的 SQL 没有模型参与，不提醒
- 合并查询、整段结果、遮罩列、文本这类输入不能单独复算：记缺口，一分钱不花
- 口径卡没执行到的直接放行；配置里写了卡上没有的指标 ID 报节点错误

查询快照是现造的工件，表名、数字都是虚构的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import delete

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.evidence import cell_eid
from app.engine.nodes.dual_check import run_dual_check
from app.engine.runner import RUN_OPTIONS_KEY
from app.engine.schema import GraphSpec
from app.tools.datasource import RUN_VERSIONS_KEY
from tests.fixtures.dual_check_shop import RIGHT, Blind, add_source, build
from tests.fixtures.sources import drop_source


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


@dataclass
class Recorder(NodeContext):
    """记下节点发的事件。不在图里执行：@task 直接执行，once 直接发。"""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_type, **data):
        self.events.append((str(event_type), data))

    def logs(self, code: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == "log" and d.get("code") == code]


@pytest.fixture
async def shop(tmp_path):
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == "verify"))
        await session.commit()
    name, source_id = await add_source(build(tmp_path / "shop.db"), options={"mask_columns": "secret_gmv"})
    yield name
    await drop_source(source_id)


async def snapshot(source: str, *, columns=("gmv",), rows=((330.0,),), sql=RIGHT, **extra) -> str:
    content = {"columns": list(columns), "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": False,
               "sql": sql, "source": source, **extra}
    return await artifact_store.put_json(content, kind="query_snapshot", run_id="unit", node_id="agent")


def context(graph: dict, node_id: str = "dual") -> Recorder:
    parsed = GraphSpec.model_validate(graph)
    run = RunContext(run_id="unit-run", thread_id="unit-run", spec=parsed,
                     extra={RUN_OPTIONS_KEY: {"verify": {}}, RUN_VERSIONS_KEY: None})
    return Recorder(node=parsed.node_map()[node_id], run=run)


AGENT_GRAPH = {"nodes": [node("start", "input"), node("agent", "agent", prompt="查销售额"),
                         node("card", "metrics", metrics=[{"id": "gmv", "expression": "vars.kpi.gmv"}]),
                         node("dual", "dual_check", "复核"), node("out", "output", fields=[])],
               "edges": [{"source": a, "target": b} for a, b in
                         (("start", "agent"), ("agent", "card"), ("card", "dual"), ("dual", "out"))]}


def card_of(*metrics: dict) -> dict:
    return {"kind": "metric_set", "caliber": "销售口径", "caliber_version": "v1", "metrics": list(metrics),
            "artifact": "card-artifact"}


def metric(artifact: str, *, path: str = "vars.kpi.gmv", locator: dict | None = None, value: Any = 330.0,
           via: str = "agent_field", node_id: str = "agent", expression: str | None = None) -> dict:
    where = locator or {"row": 0, "column": "gmv"}
    return {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands", "value": 330.0,
            "rendered": "330.0元", "expression": expression or path, "substituted": "330.0", "status": "ok",
            "inputs": [{"path": path, "value": value, "node_id": node_id, "via": via, "field": "gmv",
                        "artifact": artifact, "locator": where, "status": "verified",
                        **({"eid": cell_eid(artifact, where["row"], where["column"])} if "row" in where else {})}]}


async def test_the_same_model_as_the_agent_that_fetched_the_data_is_flagged(monkeypatch, shop):
    art = await snapshot(shop)
    Blind(monkeypatch, shop, {"销售额": (RIGHT, "gmv")})
    ctx = context(AGENT_GRAPH)
    state = {"nodes": {"agent": {"model": "mock-fast", "data": {"gmv": 330.0}}, "card": card_of(metric(art))},
             "evidence": [], "trail": []}
    out = (await run_dual_check(state, ctx))["nodes"]["dual"]
    assert out["status"] == "agreed" and out["same_model"] is True and out["model"] == "mock-fast"
    [warned] = ctx.logs("dual_check_same_model")
    assert warned["model"] == "mock-fast" and "盲算模型与产出原数据的模型相同" in warned["message"]
    assert "设置 → 偏好设置 → 核查" in warned["message"]


async def test_data_fetched_by_fixed_sql_raises_no_same_model_warning(monkeypatch, shop):
    art = await snapshot(shop)
    Blind(monkeypatch, shop, {"销售额": (RIGHT, "gmv")})
    ctx = context(AGENT_GRAPH)
    state = {"nodes": {"agent": {"model": "mock-fast"},
                       "card": card_of(metric(art, path="cell(nodes.fetch, 0, 'gmv')", via="tool_cell",
                                              node_id="fetch"))},
             "evidence": [], "trail": []}
    out = (await run_dual_check(state, ctx))["nodes"]["dual"]
    assert out["status"] == "agreed" and out["same_model"] is False and not ctx.logs("dual_check_same_model")


@pytest.mark.parametrize("case, reason", [
    ("merge", "来自合并查询"),
    ("rows", "取的是一整段查询结果"),
    ("masked", "设置了遮罩的列"),
    ("text", "不是数值"),
])
async def test_inputs_that_cannot_be_checked_alone_become_gaps(monkeypatch, shop, case, reason):
    blind = Blind(monkeypatch, shop, {"销售额": (RIGHT, "gmv")})
    if case == "merge":
        art = await snapshot("合并查询", sources=[shop])
        item = metric(art)
    elif case == "rows":
        art = await snapshot(shop, rows=((100.0,), (230.0,)))
        item = metric(art, path="vars.kpi.weeks", expression="sum(vars.kpi.weeks)",
                      locator={"rows": [0, 1], "column": "gmv"}, value="[2 项]")
    elif case == "masked":
        art = await snapshot(shop, columns=("secret_gmv",))
        item = metric(art, locator={"row": 0, "column": "secret_gmv"})
    else:
        art = await snapshot(shop, columns=("gmv",), rows=(("三百三",),))
        item = metric(art, value="三百三")    # 指标值是数，输入却是文本（比如拿它做了判断）
    ctx = context(AGENT_GRAPH)
    state = {"nodes": {"agent": {"model": "other"}, "card": card_of(item)}, "evidence": [], "trail": []}
    out = (await run_dual_check(state, ctx))["nodes"]["dual"]
    [row] = out["metrics"]
    assert row["status"] == "unchecked" and reason in row["reason"]
    assert out["issuance_gaps"] == [f"指标「销售额」无法复算：{row['reason']}"]
    assert blind.calls == [], "不能单独复算的输入也花了钱"


async def test_a_card_that_did_not_run_passes_through(shop):
    ctx = context(AGENT_GRAPH)
    out = await run_dual_check({"nodes": {}, "evidence": [], "trail": []}, ctx)
    assert out == {"nodes": {"dual": {"status": "skipped", "reason": "no_inputs"}}}
    assert [d["reason"] for d in ctx.logs("dual_check_skipped")] == ["no_inputs"]


async def test_unknown_metric_ids_fail_the_node(shop):
    art = await snapshot(shop)
    graph = {**AGENT_GRAPH, "nodes": [*AGENT_GRAPH["nodes"][:3], node("dual", "dual_check", "复核", metrics=["refund"]),
                                      AGENT_GRAPH["nodes"][4]]}
    ctx = context(graph)
    with pytest.raises(NodeError, match="「复算的指标」中的「refund」不是口径卡上的指标 ID"):
        await run_dual_check({"nodes": {"card": card_of(metric(art))}, "evidence": [], "trail": []}, ctx)
