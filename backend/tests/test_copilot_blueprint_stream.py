"""蓝图助手和填关节的流式接口（docs/design/normal-user-apps.md §2.2、§2.3、§2.11）。模型一律用剧本，不调真接口。

守的是：
- blueprint-stream：bp_* 操作落到蓝图上（来源记助手、状态「待确认」），越界的不转发、交付时照实列出；bp_ask 让这一轮就此
  结束，反问记进蓝图、对话这一轮记成反问（meta.asked），下一轮用户的话记成回答、进历史；每个操作写一条留痕；
  对话是 kind=blueprint；个人应用没开放时个人蓝图 403；
- fill-joints-stream：不同组并行填（两组同时在调模型），事件带 group；一组出错不挡别的组，等别的组做完再按顺序重试一次；
  检查过不了的组留占位、标「待填」并写明原因；填上的关节和图存回蓝图，每个关节写一条留痕。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import Conversation, ConversationTurn, DataSource
from app.db.models_blueprints import Blueprint, BlueprintEvent
from app.main import app
from tests.fixtures.sources import drop_source


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(tmp_path, name: str, ddl: str) -> DataSource:
    path = tmp_path / f"{name}.db"
    db = sqlite3.connect(path)
    db.executescript(ddl)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), description="测试库")
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
    return row


@pytest.fixture
async def sources(tmp_path):
    shop = await _source(tmp_path, f"bpshop_{uuid.uuid4().hex[:6]}",
                         "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, paid_at TEXT, refunded INTEGER);")
    crm = await _source(tmp_path, f"bpcrm_{uuid.uuid4().hex[:6]}",
                        "CREATE TABLE visits (id INTEGER PRIMARY KEY, uid TEXT, visited_at TEXT);")
    yield shop, crm
    await drop_source(shop.id)
    await drop_source(crm.id)


class Scripted:
    """按请求吐操作流：route(messages, n) 交回这一次要吐的操作（或抛错）。记下同时在调的最多有几个（并行的证据）。"""

    def __init__(self, route) -> None:
        self.route = route
        self.calls: list[list] = []
        self.active = 0
        self.peak = 0

    async def astream(self, messages):
        self.calls.append(list(messages))
        n = len(self.calls)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.05)
            lines = self.route(messages, n)
            for line in lines:
                yield AIMessageChunk(content=json.dumps(line, ensure_ascii=False) + "\n")
        finally:
            self.active -= 1


def _use(monkeypatch, model) -> None:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)


async def _events(client, monkeypatch, model, path: str, body: dict) -> list[dict]:
    _use(monkeypatch, model)
    out: list[dict] = []
    async with client.stream("POST", path, json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _ops(events: list[dict]) -> list[str]:
    return [e["op"] for e in events if e["op"] not in ("thinking", "heartbeat", "model")]


async def _blueprint(**fields) -> str:
    from app.blueprints.spec import empty_blueprint

    async with SessionLocal() as session:
        row = Blueprint(name=fields.pop("name", "测试蓝图"), spec=fields.pop("spec", empty_blueprint()), **fields)
        session.add(row)
        await session.commit()
        return row.id


async def _cleanup(bp_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp_id)
        if row is not None:
            if row.conversation_id:
                conv = await session.get(Conversation, row.conversation_id)
                if conv is not None:
                    await session.delete(conv)
            await session.delete(row)
            await session.commit()


# ---------------------------------------------------------------- 蓝图对话


async def test_blueprint_stream_proposes_asks_and_records(client, monkeypatch, sources):
    shop, _ = sources

    def route(messages, n):
        if n == 1:
            return [{"op": "plan", "summary": "整理成周期看板"},
                    {"op": "bp_template", "template": {"id": "periodic_board", "version": 1}},
                    {"op": "bp_set", "path": "/purpose", "value": "每周看门店销售额"},
                    {"op": "bp_add", "path": "/data", "value": {
                        "id": "gmv", "label": "销售额", "unit": "元", "table": {"source": shop.name, "table": "orders"},
                        "measure": "sum(amount)"}},
                    {"op": "bp_set", "path": "/data/0/table/why", "value": "orders 表有金额和支付日期"},
                    {"op": "bp_add", "path": "/access/use", "value": {"kind": "group", "name": "门店"}},
                    {"op": "bp_ask", "question": "销售额按下单日还是支付日算？", "options": ["按下单日", "按支付日"],
                     "path": "/data/0/date"},
                    {"op": "bp_set", "path": "/purpose", "value": "问了之后的不算"}]
        return [{"op": "plan", "summary": "按支付日"},
                {"op": "bp_set", "path": "/data/0/date", "value": {"column": "paid_at", "rule": "按支付日"}},
                {"op": "done", "explanation": "销售额按支付日算"}]

    model = Scripted(route)
    bp_id = await _blueprint()
    try:
        events = await _events(client, monkeypatch, model, "/api/copilot/blueprint-stream",
                               {"blueprint_id": bp_id, "instruction": "每周一看上周各门店的销售额"})
        assert _ops(events) == ["plan", "bp_template", "bp_set", "bp_add", "bp_set", "bp_ask", "check", "final"], \
            _ops(events)
        final = events[-1]
        spec = final["blueprint"]["spec"]
        assert spec["template"] == {"id": "periodic_board", "version": 1}
        assert spec["purpose"] == {"value": "每周看门店销售额", "source": "assistant", "status": "proposed", "why": None}
        assert spec["data"][0]["table"]["why"] == "orders 表有金额和支付日期"
        assert spec["access"]["use"] == [], "授权助手不替人填"
        assert spec["asks"][0]["question"] == "销售额按下单日还是支付日算？" and spec["asks"][0]["answer"] is None
        assert final["asked"] == {"question": "销售额按下单日还是支付日算？", "options": ["按下单日", "按支付日"]}
        assert [i["path"] for i in final["issues"]] == ["/access/use"]
        assert any(i["code"] == "bp.data_unmapped" or i["code"] == "bp.unconfirmed" for i in final["blueprint"]["issues"])
        system, human = model.calls[0][0][1], model.calls[0][1][1]
        assert "periodic_board" in system and "适用于" in system and "bp_ask" in system
        assert "每周一看上周各门店的销售额" in human

        async with SessionLocal() as session:
            row = await session.get(Blueprint, bp_id)
            conv = await session.get(Conversation, row.conversation_id)
            assert conv.kind == "blueprint"
            turn = (await session.execute(select(ConversationTurn).where(
                ConversationTurn.conversation_id == conv.id))).scalars().one()
            assert turn.status == "done" and turn.meta["asked"]["question"] == "销售额按下单日还是支付日算？"
            ops = [e.op for e in (await session.execute(select(BlueprintEvent).where(
                BlueprintEvent.blueprint_id == bp_id).order_by(BlueprintEvent.seq))).scalars()]
            assert ops == ["bp_template", "bp_set", "bp_add", "bp_set", "bp_ask"]

        # 下一轮：用户的话记成上一轮反问的回答，问题和回答都在历史里
        events = await _events(client, monkeypatch, model, "/api/copilot/blueprint-stream",
                               {"blueprint_id": bp_id, "instruction": "按支付日"})
        assert _ops(events) == ["plan", "bp_set", "done", "check", "final"], _ops(events)
        spec = events[-1]["blueprint"]["spec"]
        assert spec["asks"][0]["answer"] == "按支付日"
        assert spec["data"][0]["date"]["value"] == {"column": "paid_at", "rule": "按支付日"}
        human = model.calls[-1][1][1]
        assert "销售额按下单日还是支付日算？" in human and "按支付日" in human
        async with SessionLocal() as session:
            events_db = list((await session.execute(select(BlueprintEvent).where(
                BlueprintEvent.blueprint_id == bp_id).order_by(BlueprintEvent.seq))).scalars())
            answer = next(e for e in events_db if e.data.get("path") == "/asks/0/answer")
            assert answer.data["source"] == "user"
            assert events_db[-1].data["path"] == "/data/0/date" and events_db[-1].data["source"] == "assistant"
            turns = list((await session.execute(select(ConversationTurn).order_by(ConversationTurn.seq).where(
                ConversationTurn.conversation_id == (await session.get(Blueprint, bp_id)).conversation_id))).scalars())
            assert [t.question for t in turns] == ["每周一看上周各门店的销售额", "按支付日"]
            assert turns[1].answer == "销售额按支付日算"
    finally:
        await _cleanup(bp_id)


async def test_personal_blueprints_need_the_switch(client, monkeypatch):
    bp_id = await _blueprint(scope="personal", owner="someone")
    try:
        _use(monkeypatch, Scripted(lambda m, n: [{"op": "done"}]))
        r = await client.post("/api/copilot/blueprint-stream", json={"blueprint_id": bp_id, "instruction": "看看"})
        assert r.status_code == 403 and "个人应用" in r.json()["detail"]
        r = await client.post("/api/copilot/fill-joints-stream", json={"blueprint_id": bp_id})
        assert r.status_code == 403
        r = await client.post("/api/copilot/blueprint-stream", json={"blueprint_id": "nope", "instruction": "看看"})
        assert r.status_code == 404
    finally:
        await _cleanup(bp_id)


# ---------------------------------------------------------------- 填关节


def _board_spec(shop: str, crm: str) -> dict:
    from app.blueprints.templates import _common as c

    spec = c.EXTRAS["periodic_board"].example()
    for item in spec["data"]:
        item["table"]["value"] = {"source": shop, "table": "orders"}
    spec["data"] = spec["data"][:2]
    spec["data"].append({**json.loads(json.dumps(spec["data"][0])), "id": "visitors",
                         "label": c.template_item("访客数"), "unit": c.template_item("人"),
                         "table": c.template_item({"source": crm, "table": "visits"}),
                         "measure": c.template_item("count(distinct uid)"),
                         "date": c.template_item({"column": "visited_at"})})
    return spec


def _fills(shop_node: str, crm_node: str) -> dict:
    return {
        shop_node: [{"op": "plan", "summary": "写取数"},
                    {"op": "fill_joint", "joint": f"sql_{shop_node}", "value":
                        "SELECT SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders"
                        " WHERE paid_at BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'"},
                    {"op": "fill_joint", "joint": "metric_gmv", "value": f"cell(nodes.{shop_node}, 0, 'gmv')"},
                    {"op": "fill_joint", "joint": "metric_orders", "value": f"cell(nodes.{shop_node}, 0, 'order_cnt')"},
                    {"op": "update_node", "id": "done", "config": {"contract": {}}},
                    {"op": "done", "explanation": "按支付日"}],
        crm_node: [{"op": "plan", "summary": "写访客"},
                   {"op": "fill_joint", "joint": f"sql_{crm_node}", "value":
                       "SELECT COUNT(DISTINCT uid) AS visitors FROM visits"
                       " WHERE visited_at BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'"},
                   {"op": "fill_joint", "joint": "metric_visitors", "value": f"cell(nodes.{crm_node}, 0, 'visitors')"},
                   {"op": "done", "explanation": "访客按去重算"}],
    }


async def test_fill_joints_runs_groups_in_parallel_and_retries_a_failed_one(client, monkeypatch, sources):
    from app.blueprints.templates._common import safe_id

    shop, crm = sources
    shop_node, crm_node = f"fetch_{safe_id(shop.name)}", f"fetch_{safe_id(crm.name)}"
    fills = _fills(shop_node, crm_node)
    failed_once: list[int] = []

    def route(messages, n):
        human = messages[1][1]
        if "这是你刚填的关节" in human:
            # 自查交回来的只有那条越界的操作：知道了，不再改
            return [{"op": "done"}]
        group = shop_node if f"sql_{shop_node}" in human else crm_node
        if group == crm_node and not failed_once:
            failed_once.append(n)
            raise RuntimeError("模型服务限流了")
        return fills[group]

    model = Scripted(route)
    bp_id = await _blueprint(spec=_board_spec(shop.name, crm.name), template_id="periodic_board", template_version=1)
    try:
        events = await _events(client, monkeypatch, model, "/api/copilot/fill-joints-stream", {"blueprint_id": bp_id})
        assert model.peak >= 2, "两组同时在调模型"
        groups = [(e["group"], e["status"]) for e in events if e["op"] == "group"]
        assert (shop_node, "started") in groups and (crm_node, "retrying") in groups
        assert groups[-1] == (crm_node, "filled") and (shop_node, "filled") in groups
        looks = [e for e in events if e["op"] == "look"]
        assert {e["group"] for e in looks} == {shop_node, crm_node} and all(e["by"] == "system" for e in looks)
        assert all("group" in e for e in events if e["op"] in ("fill_joint", "plan", "check"))
        final = events[-1]
        assert final["op"] == "final", final
        assert final["groups"][shop_node]["status"] == "filled" and final["groups"][crm_node]["status"] == "filled"
        # 越界的那一条没有采纳，照实列出
        assert [r["code"] for r in final["groups"][shop_node]["refusals"]] == ["tpl.joint_refused"]
        joints = final["blueprint"]["joints"]
        assert all(state["status"] == "filled" for state in joints.values()), joints
        assert final["problems"] == []
        async with SessionLocal() as session:
            row = await session.get(Blueprint, bp_id)
            nodes = {n["id"]: n for n in row.graph["nodes"]}
            assert "SUM(amount)" in nodes[shop_node]["data"]["config"]["args"]["sql"]
            assert nodes["done"]["data"]["config"]["contract"]["required"] == ["gmv", "orders", "visitors"]
            from app.core.artifact_store import graph_hash

            assert row.graph_hash == graph_hash(row.graph)
            assert row.joints["metric_gmv"]["value"] == f"cell(nodes.{shop_node}, 0, 'gmv')"
            fills_db = [e.data for e in (await session.execute(select(BlueprintEvent).where(
                BlueprintEvent.blueprint_id == bp_id, BlueprintEvent.op == "fill_joint"))).scalars()]
            assert {d["joint"] for d in fills_db} == {f"sql_{shop_node}", "metric_gmv", "metric_orders",
                                                      f"sql_{crm_node}", "metric_visitors"}

        # 再请求一次：都填上了，没有要填的组，直接交付
        events = await _events(client, monkeypatch, model, "/api/copilot/fill-joints-stream", {"blueprint_id": bp_id})
        assert _ops(events) == ["final"]
    finally:
        await _cleanup(bp_id)


async def test_a_group_that_fails_its_checks_keeps_placeholders(client, monkeypatch, sources):
    from app.blueprints.templates._common import SQL_PLACEHOLDER, safe_id

    shop, crm = sources
    shop_node, crm_node = f"fetch_{safe_id(shop.name)}", f"fetch_{safe_id(crm.name)}"
    fills = _fills(shop_node, crm_node)
    bad = [{"op": "fill_joint", "joint": f"sql_{crm_node}", "value": "SELECT COUNT(*) AS n FROM visits"},
           {"op": "fill_joint", "joint": "metric_visitors", "value": f"cell(nodes.{crm_node}, 0, 'visitors')"},
           {"op": "done"}]

    def route(messages, n):
        human = messages[1][1]
        if f"sql_{shop_node}" in human:
            return fills[shop_node]
        return bad

    model = Scripted(route)
    bp_id = await _blueprint(spec=_board_spec(shop.name, crm.name), template_id="periodic_board", template_version=1)
    try:
        events = await _events(client, monkeypatch, model, "/api/copilot/fill-joints-stream", {"blueprint_id": bp_id})
        final = events[-1]
        assert final["groups"][crm_node]["status"] == "failed" and final["groups"][shop_node]["status"] == "filled"
        # 自查交回去改了两轮，还是对不上，照实报
        repairs = [e for e in events if e["op"] == "check" and e.get("group") == crm_node and e["status"] == "repairing"]
        assert len(repairs) == 2 and repairs[0]["issues"][0]["code"] == "tpl.metric_column"
        joints = final["blueprint"]["joints"]
        assert joints["metric_gmv"]["status"] == "filled"
        assert joints["metric_visitors"]["status"] == "pending" and "没通过检查" in joints["metric_visitors"]["note"]
        assert {p["code"] for p in final["problems"]} == {"tpl.joint_unfilled"}
        assert all(p.get("plain") for p in final["problems"])
        async with SessionLocal() as session:
            row = await session.get(Blueprint, bp_id)
            nodes = {n["id"]: n for n in row.graph["nodes"]}
            assert nodes[crm_node]["data"]["config"]["args"]["sql"] == SQL_PLACEHOLDER
            assert "SUM(amount)" in nodes[shop_node]["data"]["config"]["args"]["sql"]
        # 只重填失败的那一组
        model.route = lambda messages, n: fills[crm_node]
        events = await _events(client, monkeypatch, model, "/api/copilot/fill-joints-stream",
                               {"blueprint_id": bp_id, "groups": [crm_node]})
        assert {e["group"] for e in events if e["op"] == "group"} == {crm_node}
        assert all(state["status"] == "filled" for state in events[-1]["blueprint"]["joints"].values())
        r = await client.post("/api/copilot/fill-joints-stream", json={"blueprint_id": bp_id, "groups": ["nope"]})
        assert r.status_code == 400
    finally:
        await _cleanup(bp_id)


async def test_templates_api(client):
    r = await client.get("/api/templates")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == ["periodic_board", "approval", "exception_handling", "records",
                                        "reconcile_writeback", "freeform"]
    board = items[0]
    assert board["label"] == "周期看板" and board["personal"] and board["cards"][0]["card"] == "purpose"
    assert next(i for i in items if i["id"] == "approval")["personal"] is None


async def test_records_fill_uses_the_records_table(client, monkeypatch):
    """填报登记：填关节前按「填什么」建好记录表（和试运行同一个入口），汇总的 SQL 查这张表登记成的数据源；写入节点固定在
    这一个表版本上（「键.v版本」）。"""
    import re

    from app.blueprints.templates import _common as c
    from app.db.models_blueprints import RecordsTable

    def route(messages, n):
        human = messages[1][1]
        if "这是你刚填的关节" in human:
            return [{"op": "done"}]
        node = re.search(r"sql_(summary_\w+)", human).group(1)
        return [{"op": "fill_joint", "joint": f"sql_{node}", "value":
                 "SELECT SUM(amount) AS loss_amount, COUNT(*) AS loss_count FROM records"
                 " WHERE date(_submitted_at) BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'"},
                {"op": "fill_joint", "joint": "metric_loss_amount", "value": f"cell(nodes.{node}, 0, 'loss_amount')"},
                {"op": "fill_joint", "joint": "metric_loss_count", "value": f"cell(nodes.{node}, 0, 'loss_count')"},
                {"op": "done"}]

    bp_id = await _blueprint(spec=c.EXTRAS["records"].example(), template_id="records", template_version=1)
    try:
        events = await _events(client, monkeypatch, Scripted(route), "/api/copilot/fill-joints-stream",
                               {"blueprint_id": bp_id})
        final = events[-1]
        assert final["op"] == "final", final
        assert all(state["status"] == "filled" for state in final["blueprint"]["joints"].values()), final
        async with SessionLocal() as session:
            table = (await session.execute(select(RecordsTable).where(RecordsTable.table_key == bp_id))).scalars().one()
            source = await session.get(DataSource, table.source_id)
            nodes = {n["id"]: n for n in (await session.get(Blueprint, bp_id)).graph["nodes"]}
            assert nodes["append"]["data"]["config"]["args"]["table"] == f"{bp_id}.v1"
            summary = next(n for nid, n in nodes.items() if nid.startswith("summary_"))
            assert summary["data"]["config"]["tool"] == f"db_query__{source.name}"
            source_id, source_name = source.id, source.name
            await session.delete(table)
            await session.commit()
        await drop_source(source_id)
        assert source_name
    finally:
        await _cleanup(bp_id)


async def test_personal_blueprints_only_see_personal_templates(client, monkeypatch):
    """开了个人应用之后，个人蓝图的助手只列能做成个人应用的场景，能力只列只读的。"""
    from app.api.settings import APPS_GROUP
    from app.db.models import Setting

    async with SessionLocal() as session:
        row = await session.get(Setting, APPS_GROUP)
        before = dict(row.value) if row is not None else None
        if row is None:
            session.add(Setting(key=APPS_GROUP, value={"user_personal_apps": True}))
        else:
            row.value = {**row.value, "user_personal_apps": True}
        await session.commit()
    bp_id = await _blueprint(scope="personal", owner="someone")
    model = Scripted(lambda m, n: [{"op": "bp_set", "path": "/purpose", "value": "给自己看的周报"}, {"op": "done"}])
    try:
        events = await _events(client, monkeypatch, model, "/api/copilot/blueprint-stream",
                               {"blueprint_id": bp_id, "instruction": "我想每周看看自己门店的销售额"})
        assert events[-1]["op"] == "final", events[-1]
        system = model.calls[0][0][1]
        assert "个人应用" in system and "periodic_board v1" in system
        assert "approval v1" not in system and "reconcile_writeback v1" not in system
    finally:
        await _cleanup(bp_id)
        async with SessionLocal() as session:
            row = await session.get(Setting, APPS_GROUP)
            if before is None:
                await session.delete(row)
            else:
                row.value = before
            await session.commit()
