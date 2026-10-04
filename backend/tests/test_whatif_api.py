"""数字推演接口：POST /api/runs/{run_id}/evidence/whatif。

真跑一遍图（虚构的 SQLite 库、MockChatModel 写报告），再在这次运行上推演：
- 什么都不改：每个指标原样交回，报告里引用它们的地方一处不变
- 改输入、排除查询快照里的行：新值和手算一致，报告里会变的那几处按报告的写法渲染
- 只读：不追加事件（last_seq 不变）、不落工件、封存核对照旧一致
- 只认这次运行封存范围内的工件；遮罩列不给值、不能拿来推演；输入不合法回 422 并说清哪里不对
- 输入的还原：nodes.* 从节点产出、agent 数组字段按 locator.rows、新运行的 vars.* 从 value_artifact；
  老运行（没有 value_artifact）的 vars 容器照实说推演不了
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import func, select

from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent
from app.engine.runner import run_manager, verify_manifest
from app.main import app

SQL = "SELECT id, region, amount FROM orders ORDER BY id"
REGIONS = ["华东", "华南", "华北", "西南", "东北"]
ROWS = [(i, REGIONS[i % 5], float(i * 100) + 0.5) for i in range(1, 11)]
AMOUNT5 = "cell(nodes.fetch, 5, 'amount')"
METRICS = [
    {"id": "amount5", "name": "第六单金额", "unit": "元", "decimals": 1, "format": "thousands", "expression": AMOUNT5},
    {"id": "cnt", "name": "订单数", "unit": "单", "format": "thousands", "expression": "len(vars.res.rows)"},
    {"id": "per", "name": "平均每单", "unit": "元", "decimals": 2, "format": "thousands",
     "expression": f"round({AMOUNT5} / len(vars.res.rows), 2)"},
]
TEXT = "第六单金额 [[m:amount5]]，共 [[m:cnt]]，平均每单 [[m:per]]。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL, week TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                   [(i, r, a, "2026-W37" if i > 3 else "2026-W36") for i, r, a in ROWS])
    db.commit()
    db.close()
    source_id = await set_source(path)
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def query_graph(*, between=(), metrics=METRICS, sql=SQL):
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": sql}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="推演口径", caliber_version="v3", metrics=metrics),
        *between,
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


def script(monkeypatch, text=TEXT):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def run_graph(monkeypatch, graph, *, text=TEXT, statuses=("succeeded", "failed")) -> Run:
    script(monkeypatch, text)
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id, statuses)
    assert row.status in statuses and row.status != "failed", row.error
    return row


async def snapshot_of(run_id: str, node_id: str = "fetch") -> str:
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "tool.end", RunEvent.node_id == node_id))).scalars()
    return end.data["query_artifact"]


async def whatif(client, run_id: str, body: dict, status: int = 200) -> dict:
    resp = await client.post(f"/api/runs/{run_id}/evidence/whatif", json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


def by_id(body: dict) -> dict:
    return {m["id"]: m for m in body["metrics"]}


async def footprint(run_id: str) -> tuple:
    """这次运行留在库里、盘上的痕迹：last_seq、事件数、工件引用行数、工件文件数。"""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        events = await session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id))
        artifacts = await session.scalar(select(func.count()).select_from(Artifact))
    files = sum(1 for _ in (settings.data_dir / "artifacts").rglob("*.json"))
    return run.last_seq, run.manifest_hash, events, artifacts, files


# --------------------------------------------------------------------------
# 不改、改输入、排除行
# --------------------------------------------------------------------------


async def test_a_baseline_changes_nothing(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card"})

    assert body["run_id"] == row.id and body["sealed"] is True and body["seal"] == {"sealed": True, "ok": True}
    assert body["card"]["node_id"] == "card" and body["card"]["caliber"] == "推演口径" and body["card"]["version"] == "v3"
    metrics = by_id(body)
    assert {k: (m["old"], m["new"], m["status"], m["changed"]) for k, m in metrics.items()} == {
        "amount5": (600.5, 600.5, "unchanged", False), "cnt": (10, 10, "unchanged", False),
        "per": (60.05, 60.05, "unchanged", False)}
    assert [m["rendered_old"] for m in body["metrics"]] == ["600.5元", "10单", "60.05元"]
    [cell] = metrics["amount5"]["inputs"]
    assert cell["path"] == AMOUNT5 and cell["editable"] is True and cell["artifact"] == snapshot
    assert cell["locator"] == {"row": 5, "column": "amount"} and cell["old"] == 600.5
    [rows] = metrics["cnt"]["inputs"]
    assert rows["kind"] == "container" and rows["editable"] is False and rows["old"] == "[10 项]"
    assert rows["artifact"] == snapshot, "整份查询结果赋给变量：按内容对上了那份快照"
    assert body["unsupported"] == [] and body["affected_cards"] == []
    # 报告里引用这三个指标的地方，都不变
    assert [(s["report"], s["metric"], s["rendered_old"], s["rendered_new"], s["changed"]) for s in body["segments"]] == [
        ("write", "amount5", "600.5元", "600.5元", False), ("write", "cnt", "10单", "10单", False),
        ("write", "per", "60.05元", "60.05元", False)]


async def test_excluding_rows_recomputes_like_by_hand(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [2, 7]}}})
    metrics = by_id(body)
    assert metrics["amount5"]["status"] == "unchanged"
    assert metrics["cnt"]["new"] == 8 and metrics["cnt"]["rendered_new"] == "8单" and metrics["cnt"]["delta"] == -2
    assert metrics["per"]["new"] == round(600.5 / 8, 2) == 75.06 and metrics["per"]["rendered_delta"] == "+15.01元"
    [rows] = metrics["cnt"]["inputs"]
    assert rows["excluded_rows"] == [2, 7] and (rows["old"], rows["new"]) == ("[10 项]", "[8 项]")
    changed = [(s["metric"], s["rendered_old"], s["rendered_new"]) for s in body["segments"] if s["changed"]]
    assert changed == [("cnt", "10单", "8单"), ("per", "60.05元", "75.06元")]

    # 排除了 cell() 那一格所在的行：这个指标排除后无法计算
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [5]}}})
    metrics = by_id(body)
    assert metrics["amount5"]["status"] == "excluded" and metrics["amount5"]["new"] is None
    assert "排除后无法计算" in metrics["amount5"]["reason"] and "第 6 行" in metrics["amount5"]["reason"]
    assert metrics["amount5"]["rendered_new"] == "—" and metrics["per"]["status"] == "excluded"
    assert metrics["cnt"]["new"] == 9
    seg = next(s for s in body["segments"] if s["metric"] == "amount5")
    assert seg["rendered_new"] == "—" and seg["changed"] is True


async def test_changing_an_input_of_one_metric(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    body = await whatif(client, row.id, {"card": "card", "metric": "per", "report": "write",
                                         "overrides": {"inputs": {AMOUNT5: 1000}}})
    [per] = body["metrics"]
    assert per["id"] == "per" and per["new"] == 100.0 and per["rendered_new"] == "100.00元"
    assert per["substituted"] == "round(1000 / len(vars.res.rows), 2)"
    # 只推演一个指标：报告里只列引用它的那一处
    assert [(s["metric"], s["rendered_new"]) for s in body["segments"]] == [("per", "100.00元")]


async def test_whatif_is_read_only(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    before = await footprint(row.id)
    await whatif(client, row.id, {"card": "card"})
    await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {AMOUNT5: 1}, "exclude_rows": {snapshot: [0]}}})
    await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {"vars.nope": 1}}}, status=422)
    assert await footprint(row.id) == before, "推演不能追加事件、不能落工件"
    verdict = await verify_manifest(row.id)
    assert verdict["ok"] is True


# --------------------------------------------------------------------------
# 不合法的请求
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    [], {"card": 3}, {"card": " "}, {"card": "card", "report": 1}, {"card": "card", "metric": ["per"]},
    {"card": "card", "overrides": []}, {"card": "card", "overrides": {"inputs": []}},
    {"card": "card", "overrides": {"exclude_rows": {"abc": 3}}},
    {"card": "card", "overrides": {"exclude_rows": {"abc": ["3"]}}},
    {"card": "card", "overrides": {"exclude_rows": {"abc": [True]}}},
])
async def test_malformed_requests_are_rejected(client, monkeypatch, body):
    row = await run_graph(monkeypatch, query_graph())
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif", json=body)
    assert resp.status_code == 422 and resp.json()["code"] == "whatif_bad_request", resp.text
    assert "请求格式有误" in resp.json()["detail"]


async def test_invalid_overrides_say_what_is_wrong(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    # 别的运行的快照（内容不同，工件 id 也就不同）：工件库里确有其物，只是不在这次运行的封存范围内
    other = await run_graph(monkeypatch, query_graph(sql="SELECT id, region, amount FROM orders ORDER BY id DESC"))
    foreign = await snapshot_of(other.id)
    assert foreign != snapshot
    card_artifact = (await whatif(client, row.id, {"card": "card"}))["card"]["artifact"]
    cases = [
        ({"inputs": {"vars.nope": 1}}, ["vars.nope"]),
        ({"inputs": {AMOUNT5: "很多"}}, ["数值", "很多"]),
        ({"inputs": {AMOUNT5: True}}, ["数值"]),
        ({"inputs": {"vars.res.rows": 3}}, ["列表", "排除"]),
        ({"exclude_rows": {snapshot: [10]}}, ["行号 10", "10 行"]),
        ({"exclude_rows": {snapshot: [-1]}}, ["行号 -1"]),
        ({"exclude_rows": {foreign: [0]}}, ["不属于这次运行", "封存范围"]),
        ({"exclude_rows": {card_artifact: [0]}}, ["不属于这次运行"]),
    ]
    for overrides, words in cases:
        resp = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card", "overrides": overrides})
        assert resp.status_code == 422, (overrides, resp.text)
        body = resp.json()
        assert body["code"] == "whatif_bad_input" and all(w in body["detail"] for w in words), (overrides, body)


async def test_only_artifacts_sealed_in_this_run_are_loaded(monkeypatch):
    """推演取工件只走封存范围内的白名单：别的运行的快照、只在工件表里的东西一律当取不到。"""
    from app.api.evidence import _sealed, _sealed_cards
    from app.api.whatif import _loader
    from app.core.artifact_store import put_json

    row = await run_graph(monkeypatch, query_graph())
    other = await run_graph(monkeypatch, query_graph(sql="SELECT id, region, amount FROM orders ORDER BY id LIMIT 10"))
    stray = await put_json({"path": "vars.res.rows", "value": [1, 2, 3]}, kind="metric_input", run_id=row.id)
    sealed = await _sealed(row.id)
    loader = _loader(sealed, _sealed_cards(sealed))
    assert loader(await snapshot_of(row.id)) is not None
    assert loader(await snapshot_of(other.id)) is None and loader(stray) is None
    card = loader(sealed.outputs["card"])
    value_artifacts = {i["value_artifact"] for m in card["metrics"] for i in m["inputs"] if i.get("value_artifact")}
    assert value_artifacts and all(loader(a) is not None for a in value_artifacts)


async def test_missing_things_are_coded_404s(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    cases = [
        ("/api/runs/nope/evidence/whatif", {"card": "card"}, "run_not_found"),
        (f"/api/runs/{row.id}/evidence/whatif", {"card": "write"}, "whatif_card_not_found"),
        (f"/api/runs/{row.id}/evidence/whatif", {"card": "ghost"}, "whatif_card_not_found"),
        (f"/api/runs/{row.id}/evidence/whatif", {"card": "card", "metric": "ghost"}, "whatif_metric_not_found"),
        (f"/api/runs/{row.id}/evidence/whatif", {"card": "card", "report": "ghost"}, "evidence_report_not_found"),
    ]
    for url, body, code in cases:
        resp = await client.post(url, json=body)
        assert resp.status_code == 404 and resp.json()["code"] == code, (body, resp.text)


# --------------------------------------------------------------------------
# 遮罩、封存
# --------------------------------------------------------------------------


async def test_masked_columns_are_neither_shown_nor_used(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["amount"]})
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card"})
    metrics = by_id(body)
    [cell] = metrics["amount5"]["inputs"]
    assert cell["masked"] is True and cell["old"] is None and cell["new"] is None and cell["editable"] is False
    reasons = {(u["metric"], u["input"]): u["reason"] for u in body["unsupported"]}
    assert "遮罩" in reasons[("amount5", AMOUNT5)] and "遮罩" in reasons[("cnt", "vars.res.rows")]

    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "card", "overrides": {"inputs": {AMOUNT5: 1}}})
    assert resp.status_code == 422 and "遮罩" in resp.json()["detail"]
    metrics = by_id(await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [1]}}}))
    assert metrics["cnt"]["status"] == "masked" and metrics["cnt"]["new"] is None
    assert metrics["per"]["status"] == "masked" and metrics["amount5"]["status"] == "unchanged"


async def test_a_run_waiting_for_approval_can_be_explored_but_is_not_sealed(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph(between=[node("gate", "human", mode="approve", title="确认")]),
                          statuses=("interrupted",))
    before = await footprint(row.id)
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {AMOUNT5: 1000}}})
    assert body["sealed"] is False and body["seal"]["sealed"] is False
    assert by_id(body)["amount5"]["new"] == 1000 and body["segments"] == []
    assert await footprint(row.id) == before


# --------------------------------------------------------------------------
# 容器输入的还原
# --------------------------------------------------------------------------


async def test_an_old_run_without_full_values_says_so(client, monkeypatch):
    """加 value_artifact 之前的运行：vars 的容器没存完整值，用到它的指标推演不了，原因写清楚。"""
    from app.engine.nodes import metrics as metrics_node

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(metrics_node, "_store_value", nothing)
    row = await run_graph(monkeypatch, query_graph())
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {AMOUNT5: 1000}}})
    reasons = {(u["metric"], u["input"]): u["reason"] for u in body["unsupported"]}
    assert reasons[("cnt", "vars.res.rows")] == "无法推演：当时没有保存这个输入的完整值"
    per = by_id(body)["per"]
    assert per["status"] == "unsupported" and per["new"] is None and per["rendered_new"] is None
    assert "当时没有保存这个输入的完整值" in per["reason"]
    assert by_id(body)["amount5"]["new"] == 1000
    assert next(s for s in body["segments"] if s["metric"] == "per")["rendered_new"] is None


async def test_node_containers_come_from_the_sealed_node_output(client, monkeypatch):
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'items': [3, 5, 7], 'k': 2}", assign_to="kpi"),
        node("card", "metrics", caliber="推演口径", metrics=[
            {"id": "n", "name": "加权条数", "expression": "len(nodes.fetch.items) * vars.kpi.k"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}])))
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {"vars.kpi.k": 5}}})
    [n] = body["metrics"]
    assert n["status"] == "ok" and (n["old"], n["new"]) == (6, 15)
    items = next(i for i in n["inputs"] if i["path"] == "nodes.fetch.items")
    assert items["kind"] == "container" and items["old"] == "[3 项]"
    assert body["segments"] == []


class AgentScript:
    """agent 查一次按周汇总，抽取把两周的金额交成一个数组字段（出处是 Q1 的第 0–1 行）。"""

    WEEKLY = "SELECT week, SUM(amount) AS amount FROM orders GROUP BY week ORDER BY week"

    def __init__(self, monkeypatch):
        from app.providers import mock_model

        extracted = {"amounts": {"value": [601.5, 4903.5], "from": {"call": "Q1", "rows": "0-1", "column": "amount"}}}

        def decide(model, messages):
            if model.response_format:
                return AIMessage(content=json.dumps(extracted, ensure_ascii=False))
            if not model.tools:
                return AIMessage(content="收尾")
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="查。", tool_calls=[
                    {"name": "db_query__shop", "args": {"sql": self.WEEKLY}, "id": "call_0"}])
            return AIMessage(content="两周金额已汇总。")

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def test_agent_array_fields_follow_the_excluded_rows(client, monkeypatch):
    AgentScript(monkeypatch)
    schema = {"type": "object", "properties": {"amounts": {"type": "array", "items": {"type": "number"}}}}
    graph = chain(
        node("start", "input"),
        node("bot", "agent", prompt="按周汇总金额", tools=["db_query__shop"], approval="never",
             output_schema=schema, cite_fields=True, assign_to="kpi"),
        node("card", "metrics", caliber="推演口径", metrics=[
            {"id": "via_node", "name": "合计", "decimals": 1, "expression": "sum(nodes.bot.data.amounts)"},
            {"id": "via_vars", "name": "合计", "decimals": 1, "expression": "sum(vars.kpi.amounts)"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id)
    assert row.status == "succeeded", row.error
    snapshot = await snapshot_of(row.id, "bot")
    weeks = {"2026-W36": sum(a for i, _, a in ROWS if i <= 3), "2026-W37": sum(a for i, _, a in ROWS if i > 3)}
    total = round(weeks["2026-W36"] + weeks["2026-W37"], 1)

    baseline = by_id(await whatif(client, row.id, {"card": "card"}))
    assert baseline["via_node"]["old"] == baseline["via_vars"]["old"] == total
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [0]}}})
    for metric in by_id(body).values():
        assert metric["status"] == "ok" and metric["new"] == round(weeks["2026-W37"], 1), metric
        [amounts] = metric["inputs"]
        assert amounts["excluded_rows"] == [0] and (amounts["old"], amounts["new"]) == ("[2 项]", "[1 项]")


# --------------------------------------------------------------------------
# 下游口径卡
# --------------------------------------------------------------------------


async def test_downstream_cards_are_flagged_only_when_something_changes(client, monkeypatch):
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 100.0, 'k': 2}", assign_to="kpi"),
        node("card", "metrics", caliber="周口径", caliber_version="v1", assign_to="m", metrics=[
            {"id": "gmv", "name": "销售额", "decimals": 1, "expression": "vars.kpi.gmv"}]),
        node("yearly", "metrics", caliber="年口径", caliber_version="v2", metrics=[
            {"id": "year", "name": "年化", "decimals": 1, "expression": "vars.m.metrics[0].value * 52"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.yearly.text }}"}])))
    assert (await whatif(client, row.id, {"card": "card"}))["affected_cards"] == []
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {"vars.kpi.gmv": 120}}})
    assert by_id(body)["gmv"]["new"] == 120
    [flagged] = body["affected_cards"]
    assert (flagged["node_id"], flagged["caliber"], flagged["version"], flagged["metrics"], flagged["direct"]) == (
        "yearly", "年口径", "v2", ["year"], True)
