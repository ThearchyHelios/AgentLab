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


async def faithful(run_id: str) -> list[tuple]:
    """真跑出来的每张卡、每个指标，什么都不改、强制重算一遍：(卡, 指标, 状态, 重算值, 卡上的值)。
    输入的还原（标量、cell()、整份查询结果、agent 数组、nodes.*）是否忠实，在真卡上对一遍。和接口一样在工作线程里算。"""
    from app.api import whatif as api_whatif
    from app.api.evidence import _reports, _sealed, _sealed_cards
    from app.engine import recompute

    sealed = await _sealed(run_id)
    cards = _sealed_cards(sealed)
    out = []
    for node_id in dict.fromkeys(nid for nid, _, _ in cards):
        _, card, started = api_whatif._card_of(sealed, cards, node_id, _reports(sealed), None)
        sources = api_whatif._sources(sealed, cards, started, asyncio.get_running_loop())
        for metric in card["metrics"]:
            again = await asyncio.to_thread(recompute.recompute, metric, recompute.Changes(), sources)
            out.append((node_id, metric["id"], again["status"], again["new"], metric["value"]))
    return out


async def test_recomputing_real_cards_without_changes_reproduces_them(monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    assert [(m, s, new == old) for _, m, s, new, old in await faithful(row.id)] == [
        ("amount5", "ok", True), ("cnt", "ok", True), ("per", "ok", True)]


async def test_events_appended_after_the_seal_are_ignored(client, monkeypatch):
    """封存之后插进来的 node.finished，哪怕指向一张真的指标集，也不能换掉推演用的那张卡。"""
    from app.core.artifact_store import load, put_json

    row = await run_graph(monkeypatch, query_graph())
    card = (await whatif(client, row.id, {"card": "card"}))["card"]
    forged = load(card["artifact"])
    forged["metrics"][0]["value"] = 1.5
    fake = await put_json(forged, kind="metric_set", run_id=row.id, node_id="card")
    async with SessionLocal() as session:
        session.add(RunEvent(run_id=row.id, seq=row.manifest_seq + 5, type="node.finished", node_id="card", ts=0.0,
                             data={"evidence": [{"kind": "metric_set", "node_id": "card", "artifact": fake}]}))
        await session.commit()
    body = await whatif(client, row.id, {"card": "card"})
    assert body["card"]["artifact"] == card["artifact"] and by_id(body)["amount5"]["old"] == 600.5
    assert body["sealed"] is True


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
    assert metrics["amount5"]["substituted"] is None and metrics["per"]["substituted"] is None
    reasons = {(u["metric"], u["input"]): u["reason"] for u in body["unsupported"]}
    assert "遮罩" in reasons[("amount5", AMOUNT5)] and "遮罩" in reasons[("cnt", "vars.res.rows")]
    # 三个指标都用到了遮罩列：不改动也照实说推演不了，不报「无变化」；什么都没改，下游不受影响
    assert {k: (m["status"], m["changed"], m["rendered_new"]) for k, m in metrics.items()} == {
        k: ("masked", None, None) for k in ("amount5", "cnt", "per")}
    assert all(reasons[(k, None)] == metrics[k]["reason"] for k in metrics) and body["affected_cards"] == []

    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "card", "overrides": {"inputs": {AMOUNT5: 1}}})
    assert resp.status_code == 422 and "遮罩" in resp.json()["detail"]
    metrics = by_id(await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [1]}}}))
    assert metrics["cnt"]["status"] == "masked" and metrics["cnt"]["new"] is None
    assert metrics["per"]["status"] == "masked" and metrics["amount5"]["status"] == "masked"


async def test_a_metric_using_a_masked_column_offers_nothing_to_adjust(client, monkeypatch, shop):
    """指标的一个输入取自遮罩列，另一个是能改的格：整个指标推演不了，那一格也不让改、它那一行也不让勾。
    改了也只回「用到了遮罩列」，不给新值。"""
    await set_source(shop, {"mask_columns": ["region"]})
    region = "cell(nodes.fetch, 5, 'region')"
    metrics = [{"id": "mix", "name": "混合", "decimals": 1, "expression": f"len({region}) + {AMOUNT5}"}]
    row = await run_graph(monkeypatch, query_graph(metrics=metrics), text="混合 [[m:mix]]。")
    body = await whatif(client, row.id, {"card": "card", "metric": "mix", "report": "write"})
    [mix] = body["metrics"]
    assert (mix["status"], mix["changed"], mix["rendered_new"]) == ("masked", None, None) and "遮罩" in mix["reason"]
    inputs = {i["path"]: i for i in mix["inputs"]}
    assert inputs[region]["masked"] is True and inputs[AMOUNT5]["old"] == 600.5
    assert all(i["editable"] is False and "row_span" not in i for i in inputs.values())
    assert {"metric": "mix", "input": None, "reason": mix["reason"]} in body["unsupported"]
    [seg] = body["segments"]
    assert seg["rendered_new"] is None and seg["changed"] is None and body["affected_cards"] == []
    [mix] = (await whatif(client, row.id, {"card": "card", "metric": "mix",
                                           "overrides": {"inputs": {AMOUNT5: 1}}}))["metrics"]
    assert (mix["status"], mix["new"], mix["changed"]) == ("masked", None, None)


async def test_masks_recorded_at_query_time_apply_after_the_source_drops_them(client, monkeypatch, shop):
    """查询当时设了遮罩、跑完之后数据源去掉了：快照里记下的遮罩照样算，推演和勾行接口都不给那一列。"""
    await set_source(shop, {"mask_columns": ["amount"]})
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    await set_source(shop, {})
    body = await whatif(client, row.id, {"card": "card"})
    [cell] = by_id(body)["amount5"]["inputs"]
    assert cell["masked"] is True and cell["old"] is None and cell["editable"] is False
    assert by_id(body)["amount5"]["status"] == "masked"
    assert "600.5" not in json.dumps([m["inputs"] for m in body["metrics"]])
    url = f"/api/runs/{row.id}/evidence/whatif/rows"
    page = (await client.get(url, params={"artifact": snapshot, "limit": 2})).json()
    assert page["masked"] == ["amount"] and page["rows"] == [[1, "华南", "已遮罩"], [2, "华北", "已遮罩"]]


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
    assert [(m, s, new == old) for _, m, s, new, old in await faithful(row.id)] == [("n", "ok", True)]
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
    assert [(m, s, new == old) for _, m, s, new, old in await faithful(row.id)] == [
        ("via_node", "ok", True), ("via_vars", "ok", True)]
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [0]}}})
    for metric in by_id(body).values():
        assert metric["status"] == "ok" and metric["new"] == round(weeks["2026-W37"], 1), metric
        [amounts] = metric["inputs"]
        assert amounts["excluded_rows"] == [0] and (amounts["old"], amounts["new"]) == ("[2 项]", "[1 项]")


async def test_an_agent_array_whose_node_output_is_gone_is_never_unchanged(client, monkeypatch, damage):
    """agent 节点封存的产出文件不在了：nodes.bot.data.amounts 的完整值找不回来。排除它用到的一行，它一定少了一项，
    少的是多少不知道——不能报「无变化」，也不给行段让人勾。vars.kpi.amounts 另存过完整值，照常推演。"""
    AgentScript(monkeypatch)
    schema = {"type": "object", "properties": {"amounts": {"type": "array", "items": {"type": "number"}}}}
    row = await wait((await run_manager.start(graph=chain(
        node("start", "input"),
        node("bot", "agent", prompt="按周汇总金额", tools=["db_query__shop"], approval="never",
             output_schema=schema, cite_fields=True, assign_to="kpi"),
        node("card", "metrics", caliber="推演口径", metrics=[
            {"id": "via_node", "name": "合计", "decimals": 1, "expression": "sum(nodes.bot.data.amounts)"},
            {"id": "via_vars", "name": "合计", "decimals": 1, "expression": "sum(vars.kpi.amounts)"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}])), input_payload={})).id)
    assert row.status == "succeeded", row.error
    snapshot = await snapshot_of(row.id, "bot")
    async with SessionLocal() as session:
        [finished] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.finished", RunEvent.node_id == "bot"))).scalars()
    damage(finished.data["artifact"])

    baseline = by_id(await whatif(client, row.id, {"card": "card"}))
    assert baseline["via_node"]["status"] == "unsupported" and "bot" in baseline["via_node"]["reason"]
    assert all(i["editable"] is False and "row_span" not in i for i in baseline["via_node"]["inputs"])
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [0]}}})
    via_node, via_vars = by_id(body)["via_node"], by_id(body)["via_vars"]
    assert via_node["status"] == "unsupported" and via_node["changed"] is None and via_node["rendered_new"] is None
    assert via_vars["status"] == "ok" and via_vars["new"] == round(sum(a for i, _, a in ROWS if i > 3), 1)


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
    assert [(c, m, s, new == old) for c, m, s, new, old in await faithful(row.id)] == [
        ("card", "gmv", "ok", True), ("yearly", "year", "ok", True)]
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {"vars.kpi.gmv": 120}}})
    assert by_id(body)["gmv"]["new"] == 120
    [flagged] = body["affected_cards"]
    assert (flagged["node_id"], flagged["caliber"], flagged["version"], flagged["metrics"], flagged["direct"]) == (
        "yearly", "年口径", "v2", ["year"], True)


# --------------------------------------------------------------------------
# 排除行：说不准受不受影响的指标不报「无变化」
# --------------------------------------------------------------------------


async def test_excluding_rows_of_an_old_run_never_says_unchanged(client, monkeypatch):
    """老运行的 vars.res.rows 没存完整值：排除 cell 那一格以外的行，平均每单会变（分母少了一行），却算不出来。
    不能报「60.05元 → 60.05元 无变化」，报告里引用它的地方也不能报「不受影响」。"""
    from app.engine.nodes import metrics as metrics_node

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(metrics_node, "_store_value", nothing)
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card", "metric": "per", "report": "write",
                                         "overrides": {"exclude_rows": {snapshot: [3]}}})
    [per] = body["metrics"]
    assert per["status"] == "unsupported" and per["changed"] is None and per["new"] is None, per
    assert "vars.res.rows" in per["reason"]
    [seg] = body["segments"]
    assert seg["metric"] == "per" and seg["rendered_new"] is None and seg["changed"] is None
    cnt = by_id(await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [3]}}}))["cnt"]
    assert cnt["status"] == "unsupported" and cnt["changed"] is None


async def test_only_inputs_computed_after_the_snapshot_came_back_are_doubted(client, monkeypatch):
    """写在查询之前的容器（early）排除行影响不到它；查询之后才算出来、又对不上任何快照的（late）说不准。
    按位置取自整份结果的一格（vars.res.rows[0][2]）同样说不准：排除第 0 行后它取到的是另一行。"""
    metrics = [
        {"id": "m_early", "name": "加早", "decimals": 1, "expression": f"round({AMOUNT5} + sum(vars.early.items), 1)"},
        {"id": "m_late", "name": "加晚", "decimals": 1, "expression": f"round({AMOUNT5} + sum(vars.late.items), 1)"},
        {"id": "first", "name": "首单占比", "decimals": 2,
         "expression": "round(vars.res.rows[0][2] / len(vars.res.rows), 2)"},
    ]
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("early_n", "transform", mode="expression", expression="{'items': [1.5, 2.5]}", assign_to="early"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("late_n", "transform", mode="expression", expression="{'items': [1.5, 2.5]}", assign_to="late"),
        node("card", "metrics", caliber="推演口径", metrics=metrics),
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}])),
        text="加早 [[m:m_early]]，加晚 [[m:m_late]]，首单占比 [[m:first]]。")
    snapshot = await snapshot_of(row.id)
    assert [(m, s, new == old) for _, m, s, new, old in await faithful(row.id)] == [
        ("m_early", "ok", True), ("m_late", "ok", True), ("first", "ok", True)]
    metrics = by_id(await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [0]}}}))
    assert metrics["m_early"]["status"] == "unchanged" and metrics["m_early"]["changed"] is False
    assert metrics["m_late"]["status"] == "unsupported" and "vars.late.items" in metrics["m_late"]["reason"]
    assert "对不上任何查询快照" in metrics["m_late"]["reason"]
    assert metrics["first"]["status"] == "unsupported" and metrics["first"]["new"] is None
    assert "vars.res.rows[0][2]" in metrics["first"]["reason"]


# --------------------------------------------------------------------------
# 查询快照读不出来、封存核对没过、事后加的遮罩、被改过的指标集
# --------------------------------------------------------------------------


def artifact_file(artifact: str):
    return settings.data_dir / "artifacts" / artifact[:2] / f"{artifact}.json"


@pytest.fixture
def damage():
    """删掉、改坏工件文件，测完放回原样：工件按内容寻址，同样的查询、同样的卡在后面的测试里还是这一份文件，
    put_json 见文件在就不再写——不放回去，后面的测试拿到的就是坏的。"""
    saved: list[tuple] = []

    def apply(artifact: str, change=None) -> None:
        path = artifact_file(artifact)
        text = path.read_text("utf-8")
        saved.append((path, text))
        if change is None:
            path.unlink()
            return
        content = json.loads(text)
        change(content)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    yield apply
    for path, text in reversed(saved):
        path.write_text(text, encoding="utf-8")


def tamper_cell(content):
    content["rows"][5][2] = 1.0


@pytest.mark.parametrize("change, words", [(None, "无法读取"), (tamper_cell, "哈希")])
async def test_an_unreadable_snapshot_withholds_the_cells_on_it(client, monkeypatch, damage, change, words):
    """快照文件不在了、被改过：那一格遮没遮核对不了，卡上记着的值也不给、不能改，代入式也不给。"""
    row = await run_graph(monkeypatch, query_graph())
    damage(await snapshot_of(row.id), change)
    body = await whatif(client, row.id, {"card": "card"})
    metrics = by_id(body)
    [cell] = metrics["amount5"]["inputs"]
    assert cell["old"] is None and cell["new"] is None and cell["editable"] is False and cell.get("withheld") is True
    assert metrics["amount5"]["substituted"] is None and metrics["per"]["substituted"] is None
    assert "600.5" not in json.dumps([m["inputs"] for m in body["metrics"]])
    reasons = {(u["metric"], u["input"]): u["reason"] for u in body["unsupported"]}
    assert words in reasons[("amount5", AMOUNT5)]
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "card", "overrides": {"inputs": {AMOUNT5: 1}}})
    assert resp.status_code == 422 and "快照" in resp.json()["detail"], resp.text


async def test_a_mask_set_after_the_run_applies_even_if_the_snapshot_is_gone(client, monkeypatch, shop, damage):
    row = await run_graph(monkeypatch, query_graph())
    damage(await snapshot_of(row.id))
    await set_source(shop, {"mask_columns": ["amount"]})
    [cell] = by_id(await whatif(client, row.id, {"card": "card"}))["amount5"]["inputs"]
    assert cell["masked"] is True and cell["old"] is None


async def test_masks_added_after_the_run_still_apply(client, monkeypatch, shop):
    """遮罩是查询当时记下的加数据源现在设的：跑完之后才给 amount 加的遮罩照样生效。"""
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    await set_source(shop, {"mask_columns": ["amount"]})
    body = await whatif(client, row.id, {"card": "card"})
    metrics = by_id(body)
    [cell] = metrics["amount5"]["inputs"]
    assert cell["masked"] is True and cell["old"] is None and cell["editable"] is False
    assert metrics["amount5"]["substituted"] is None and "row_span" not in cell
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "card", "overrides": {"inputs": {AMOUNT5: 1}}})
    assert resp.status_code == 422 and "遮罩" in resp.json()["detail"]
    cnt = by_id(await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [1]}}}))["cnt"]
    assert cnt["status"] == "masked" and cnt["new"] is None


async def test_a_broken_seal_is_not_called_sealed(client, monkeypatch):
    """封存范围里随便一条事件被改过：照样能推演，但 sealed 为假，界面据此说「封存核对未通过」。"""
    row = await run_graph(monkeypatch, query_graph())
    async with SessionLocal() as session:
        started = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.started", RunEvent.node_id == "start"))).scalar_one()
        started.data = {**(started.data or {}), "note": "事后改过"}
        await session.commit()
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {AMOUNT5: 1000}}})
    assert body["sealed"] is False and body["seal"] == {"sealed": True, "ok": False}
    assert by_id(body)["amount5"]["new"] == 1000
    page = (await client.get(f"/api/runs/{row.id}/evidence/whatif/rows",
                             params={"artifact": await snapshot_of(row.id), "limit": 1})).json()
    assert page["sealed"] is False and len(page["rows"]) == 1


async def test_a_tampered_metric_set_is_refused(client, monkeypatch, damage):
    row = await run_graph(monkeypatch, query_graph())
    card = (await whatif(client, row.id, {"card": "card"}))["card"]["artifact"]
    damage(card, lambda content: content["metrics"][0].update(value=1.5))
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card"})
    assert resp.status_code == 409 and resp.json()["code"] == "whatif_card_tampered", resp.text


# --------------------------------------------------------------------------
# 多份报告、合并查询、循环
# --------------------------------------------------------------------------


async def test_two_reports_are_kept_apart(client, monkeypatch):
    """两个报告节点都引用了这张卡：指定 report 时只列那一份里的地方，不指定时两份都列。"""
    row = await run_graph(monkeypatch, chain(
        *query_graph()["nodes"][:4],
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("brief", "report", instructions="写简报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"},
                                      {"name": "简报", "value": "{{ nodes.brief.text }}"}])))
    changes = {"overrides": {"inputs": {AMOUNT5: 1000}}}
    both = await whatif(client, row.id, {"card": "card", **changes})
    assert sorted({s["report"] for s in both["segments"]}) == ["brief", "write"]
    one = await whatif(client, row.id, {"card": "card", "report": "brief", **changes})
    assert one["report"] == "brief" and one["segments"] and {s["report"] for s in one["segments"]} == {"brief"}
    assert len(one["segments"]) == len([s for s in both["segments"] if s["report"] == "brief"])


async def test_a_merged_query_with_a_masked_column(client, monkeypatch, shop):
    """合并查询不是数据源：遮罩按它合并的那几个数据源算，跑完之后才加的遮罩也一样。遮着的列不给值、不能推演。"""
    merged = "cell(nodes.merged, 5, 'amount')"
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("q_amount", "tool", tool="db_query__shop", args={"sql": "SELECT id, amount FROM orders ORDER BY id"}),
        node("q_region", "tool", tool="db_query__shop", args={"sql": "SELECT id, region FROM orders ORDER BY id"}),
        node("merged", "merge", inputs={"a": "q_amount", "r": "q_region"},
             sql="SELECT a.id, r.region, a.amount FROM a JOIN r ON a.id = r.id ORDER BY a.id"),
        node("card", "metrics", caliber="推演口径", metrics=[
            {"id": "m5", "name": "第六单", "decimals": 1, "expression": merged}]),
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}])),
        text="第六单 [[m:m5]]。")
    [cell] = by_id(await whatif(client, row.id, {"card": "card"}))["m5"]["inputs"]
    assert cell["editable"] is True and cell["old"] == 600.5
    await set_source(shop, {"mask_columns": ["amount"]})
    body = await whatif(client, row.id, {"card": "card"})
    [cell] = by_id(body)["m5"]["inputs"]
    assert cell["masked"] is True and cell["old"] is None and by_id(body)["m5"]["substituted"] is None
    resp = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                             json={"card": "card", "overrides": {"inputs": {merged: 1}}})
    assert resp.status_code == 422 and "遮罩" in resp.json()["detail"]


async def test_a_card_inside_a_loop_uses_the_node_outputs_from_before_that_execution(client, monkeypatch):
    """循环体里口径卡先算、fill 后算：第二轮的卡读到的是第一轮 fill 的产出（[1, 10]），不是循环结束时的（[2, 20]）。
    推演按那次执行开始之前的产出还原。"""
    row = await run_graph(monkeypatch, {
        "nodes": [
            node("start", "input"),
            node("lp", "loop", mode="foreach", items="[1, 2]", max_iterations=5),
            node("card", "metrics", caliber="循环口径", on_missing="null", metrics=[
                {"id": "s", "name": "合计", "expression": "sum(nodes.fill.items)"}]),
            node("fill", "transform", mode="expression", expression="{'items': [vars.item, vars.item * 10]}"),
            node("write", "report", instructions="写周报", on_violation="flag"),
            node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "lp"},
            {"source": "lp", "target": "card", "sourceHandle": "body"},
            {"source": "card", "target": "fill"},
            {"source": "fill", "target": "lp"},
            {"source": "lp", "target": "write", "sourceHandle": "done"},
            {"source": "write", "target": "out"},
        ],
    }, text="合计 [[m:s]]。")
    body = await whatif(client, row.id, {"card": "card", "report": "write"})
    [s] = body["metrics"]
    assert s["old"] == 11 and s["inputs"][0]["old"] == "[2 项]"
    assert [(m, st, new == old) for _, m, st, new, old in await faithful(row.id)] == [("s", "ok", True)]


# --------------------------------------------------------------------------
# 推演里能勾的行
# --------------------------------------------------------------------------


async def test_inputs_carry_the_rows_they_use(client, monkeypatch):
    row = await run_graph(monkeypatch, query_graph())
    metrics = by_id(await whatif(client, row.id, {"card": "card"}))
    [cell] = metrics["amount5"]["inputs"]
    [rows] = metrics["cnt"]["inputs"]
    assert cell["row_span"] == [5, 5] and rows["row_span"] == [0, 9]


async def test_a_container_that_picks_the_cell_offers_no_rows(client, monkeypatch):
    """cell() 的行号由整份结果算出：整份结果不给行段（勾了别的行，取到哪一格说不准，什么也推演不出来），那一格
    自己那一行照常可勾、勾了确定是排除后无法计算。那一格给了新值，取到哪一格已无关紧要，排除别的行照常算。"""
    pick = "cell(nodes.fetch, len(vars.res.rows) - 5, 'amount')"
    row = await run_graph(monkeypatch, query_graph(metrics=[
        {"id": "pick", "name": "倒数第五单金额", "unit": "元", "decimals": 1, "format": "thousands", "expression": pick}]),
        text="倒数第五单金额 [[m:pick]]。")
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card"})
    [m] = body["metrics"]
    assert m["status"] == "unchanged" and m["old"] == 600.5
    assert [(i["path"], i.get("row_span"), i["editable"]) for i in m["inputs"]] == [
        (pick, [5, 5], True), ("vars.res.rows", None, False)]
    assert [(u["input"], "取哪" in u["reason"]) for u in body["unsupported"]] == [("vars.res.rows", True)]
    [m] = (await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [5]}}}))["metrics"]
    assert m["status"] == "excluded" and "第 6 行" in m["reason"]
    body = await whatif(client, row.id, {"card": "card", "report": "write",
                                         "overrides": {"inputs": {pick: 1000}, "exclude_rows": {snapshot: [0]}}})
    [m] = body["metrics"]
    assert (m["status"], m["new"], m["rendered_new"]) == ("ok", 1000, "1,000.0元"), m
    assert [(s["rendered_old"], s["rendered_new"]) for s in body["segments"]] == [("600.5元", "1,000.0元")]


async def test_rows_of_a_sealed_snapshot_can_be_paged_with_masks_applied(client, monkeypatch, shop):
    """推演区里给整份结果勾行：按页取封存范围内快照的行，遮罩列写「已遮罩」。只读。"""
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    url = f"/api/runs/{row.id}/evidence/whatif/rows"
    before = await footprint(row.id)
    page = (await client.get(url, params={"artifact": snapshot, "offset": 4, "limit": 3})).json()
    assert page["artifact"] == snapshot and page["columns"] == ["id", "region", "amount"]
    assert page["row_index"] == [4, 5, 6] and page["row_offset"] == 4 and page["total_rows"] == 10
    assert page["rows"] == [[5, "华东", 500.5], [6, "华南", 600.5], [7, "华北", 700.5]] and page["masked"] == []
    assert page["sealed"] is True and page["hash_ok"] is True
    await set_source(shop, {"mask_columns": ["amount"]})
    page = (await client.get(url, params={"artifact": snapshot, "limit": 2})).json()
    assert page["rows"] == [[1, "华南", "已遮罩"], [2, "华北", "已遮罩"]] and page["masked"] == ["amount"]
    assert await footprint(row.id) == before
    # 越过末行的页是空的；不属于这次运行的快照、写错的参数说清楚
    assert (await client.get(url, params={"artifact": snapshot, "offset": 10})).json()["rows"] == []
    other = await run_graph(monkeypatch, query_graph(sql="SELECT id, region, amount FROM orders ORDER BY id DESC"))
    for params, words in [({"artifact": await snapshot_of(other.id)}, "封存范围"), ({}, "artifact"),
                          ({"artifact": snapshot, "limit": "很多"}, "limit"), ({"artifact": snapshot, "offset": -1}, "offset")]:
        resp = await client.get(url, params=params)
        assert resp.status_code == 422 and words in resp.json()["detail"], (params, resp.text)


async def test_rows_of_an_unreadable_snapshot_are_not_served(client, monkeypatch, damage):
    row = await run_graph(monkeypatch, query_graph())
    snapshot = await snapshot_of(row.id)
    damage(snapshot)
    page = (await client.get(f"/api/runs/{row.id}/evidence/whatif/rows", params={"artifact": snapshot})).json()
    assert page["rows"] == [] and page["hash_ok"] is None and "无法读取" in page["note"]


# --------------------------------------------------------------------------
# 下游口径卡、取整
# --------------------------------------------------------------------------


async def test_downstream_cards_through_an_intermediate_node_are_flagged(client, monkeypatch):
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 100.0}", assign_to="kpi"),
        node("card", "metrics", caliber="周口径", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "decimals": 1, "expression": "vars.kpi.gmv"}]),
        node("mid", "transform", mode="expression", expression="{'g': nodes.card.metrics[0].value * 2}", assign_to="t"),
        node("yearly", "metrics", caliber="年口径", caliber_version="v2", metrics=[
            {"id": "year", "name": "年化", "decimals": 1, "expression": "vars.t.g * 26"}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.yearly.text }}"}])))
    body = await whatif(client, row.id, {"card": "card", "overrides": {"inputs": {"vars.kpi.gmv": 120}}})
    [flagged] = body["affected_cards"]
    assert (flagged["node_id"], flagged["metrics"], flagged["direct"]) == ("yearly", ["year"], False)


async def test_a_change_that_moves_no_metric_flags_no_downstream_card(client, monkeypatch):
    """改动碰到了指标、算出来却没变（10 单里排除 2 单，min(单数, 3) 还是 3）：下游不会受影响，不标。排除到只剩 2 单
    才变，这时才标。界面不会提交和原值一样的输入，排除行却常有这种情况（又如 max(…) 勾掉的不是最大值那一行）。"""
    row = await run_graph(monkeypatch, query_graph(
        metrics=[{"id": "cap", "name": "封顶单数", "unit": "单", "format": "thousands",
                  "expression": "min(len(vars.res.rows), 3)"}],
        between=[node("yearly", "metrics", caliber="年口径", metrics=[
            {"id": "year", "name": "年化", "expression": "nodes.card.metrics[0].value * 52"}])]),
        text="封顶单数 [[m:cap]]。")
    snapshot = await snapshot_of(row.id)
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: [2, 7]}}})
    [cap] = body["metrics"]
    assert (cap["status"], cap["new"], cap["changed"], cap["inputs"][0]["excluded_rows"]) == ("ok", 3, False, [2, 7])
    assert body["affected_cards"] == []
    body = await whatif(client, row.id, {"card": "card", "overrides": {"exclude_rows": {snapshot: list(range(8))}}})
    assert body["metrics"][0]["new"] == 2 and [c["node_id"] for c in body["affected_cards"]] == ["yearly"]


async def test_downstream_cards_are_flagged_when_the_change_cannot_be_followed(client, monkeypatch):
    """改动碰到了指标、它却推演不了（还原不了、遮罩）：新值不知道，下游同样可能受影响。"""
    from app.engine.nodes import metrics as metrics_node

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(metrics_node, "_store_value", nothing)
    row = await run_graph(monkeypatch, query_graph(between=[node("yearly", "metrics", caliber="年口径", metrics=[
        {"id": "year", "name": "年化", "decimals": 1, "expression": "nodes.card.metrics[2].value * 52"}])]))
    body = await whatif(client, row.id, {"card": "card", "metric": "per", "overrides": {"inputs": {AMOUNT5: 1000}}})
    assert body["metrics"][0]["status"] == "unsupported"
    assert [c["node_id"] for c in body["affected_cards"]] == ["yearly"]
    # 什么都不改：平均每单本来就推演不了（vars.res.rows 没存完整值），一打开就照实说；那一格也不让改、它那一行
    # 也不让勾，报告里引用它的地方不说「不受影响」。什么都没改，下游不会受影响
    body = await whatif(client, row.id, {"card": "card", "metric": "per", "report": "write"})
    [per] = body["metrics"]
    assert (per["status"], per["changed"], per["rendered_new"]) == ("unsupported", None, None)
    assert "vars.res.rows" in per["reason"] and "完整值" in per["reason"]
    assert all(i["editable"] is False and "row_span" not in i for i in per["inputs"])
    assert [(s["metric"], s["rendered_new"], s["changed"]) for s in body["segments"]] == [("per", None, None)]
    assert body["affected_cards"] == []


TRICKY = [(2.675, 2), (0.125, 2), (-1.5, 0), (1234.5, -1), (-1250.0, -2), (0.0005, 3), (99.995, 2), (-0.045, 2)]


async def test_rounding_matches_the_card_for_tricky_values(monkeypatch):
    """取整和口径卡逐个对上：二进制里落在半中间的数、负数、负的小数位（取整到十位、百位）。run_metrics 的取整规则哪天
    改了（比如换成四舍五入），这里会对不上。"""
    import random

    rng = random.Random(20261005)
    pairs = TRICKY + [(round(rng.uniform(-1e6, 1e6), rng.randint(0, 6)), rng.randint(-3, 6)) for _ in range(24)]
    kpi = "{" + ", ".join(f"'v{i}': {v!r}" for i, (v, _) in enumerate(pairs)) + "}"
    row = await run_graph(monkeypatch, chain(
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression=kpi, assign_to="kpi"),
        node("card", "metrics", caliber="取整口径", metrics=[
            {"id": f"m{i}", "name": f"m{i}", "format": "plain", "decimals": d, "expression": f"vars.kpi.v{i} * 1.0"}
            for i, (_, d) in enumerate(pairs)]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}])))
    again = await faithful(row.id)
    assert len(again) == len(pairs)
    assert [(m, s) for _, m, s, new, old in again if s != "ok" or new != old] == []
