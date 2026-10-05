"""证据面板指标步骤的遮罩（期 4 I2a）：取自遮罩列的输入，输入标签和代入式里都不显示它的值。

以前只有查询步骤的结果窗口把遮罩列换成「已遮罩」，指标步骤的输入（「值 ← 路径」）和代入式照样写着原值——同一个
号码，表格里遮着，上面的标签里亮着。现在：

- cell() 取的格、Agent 交来的字段：按定位里的列认
- vars.res.rows[0][2] 这种按位置取自查询结果的标量引用：顺着取值链找到那份查询结果（写变量的节点当时的产出），
  按列号认出是哪一列
- 遮的是「查询当时记下的」加「数据源现在设的」，和查询步骤同一套；数据源事后去掉了遮罩，当时遮着的照样遮
- 取自遮罩列的输入值写「已遮罩」，带上是哪几列（masked），模型报的值、快照里的值一并不给；代入式里那一处写「已遮罩」，
  遮罩列也进 redacted.columns（面板底下那句「遮罩不是安全边界」）
- 没有遮罩的指标和以前一字不差；推演（api/whatif.py）同样认得出按位置取的遮罩列，不给值、不推演

真跑一遍图（虚构的 SQLite 订单库，号码是编的），报告由 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

SQL = "SELECT id, region, phone, amount FROM orders ORDER BY id"
#: 号码写成带横线的文本：全是数字的文本 cell() 会按数读（口径卡在算数），len() 就算不了
ROWS = [(1, "华东", "139-0000-0001", 120.5), (2, "华南", "139-0000-0002", 80.0), (3, "华北", "139-0000-0003", 99.5)]
PHONES = [r[2] for r in ROWS]
#: 号码那一格按位置取：vars.res 是数据整形解析好的查询结果，第 2 列是 phone
FIRST_PHONE = "vars.res.rows[0][2]"
SECOND_PHONE = "vars.res.rows[1][2]"
PHONE_CELL = "cell(nodes.fetch, 0, 'phone')"
METRICS = [
    {"id": "amt", "name": "首单金额", "unit": "元", "decimals": 1, "format": "thousands",
     "expression": "cell(nodes.fetch, 0, 'amount')"},
    {"id": "plen", "name": "号码位数", "unit": "位", "format": "thousands", "expression": f"len({PHONE_CELL})"},
    {"id": "plen2", "name": "号码位数两倍", "unit": "位", "format": "thousands", "expression": f"len({FIRST_PHONE}) * 2"},
    {"id": "mix", "name": "混合", "unit": "分", "decimals": 1, "format": "thousands",
     "expression": f"round(cell(nodes.fetch, 1, 'amount') + len({SECOND_PHONE}), 1)"},
]
TEXT = "首单金额 [[m:amt]]，号码 [[m:plen]]，两倍 [[m:plen2]]，混合 [[m:mix]]。"


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


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, phone TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    source_id = await set_source(path)
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph():
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="遮罩口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def run_graph(monkeypatch) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=TEXT))
    run_id = (await run_manager.start(graph=graph(), input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def segment(client, run_id: str, text: str) -> dict:
    graph_body = (await client.get(f"/api/runs/{run_id}/evidence")).json()
    [report] = graph_body["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if s["text"] == text)
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    return res.json()


def inputs_of(body: dict) -> dict[str, dict]:
    return {s["path"]: s for s in body["chain"] if s["step"] == "input"}


def leaks(body: dict) -> list[str]:
    raw = json.dumps(body, ensure_ascii=False)
    return [p for p in PHONES if p in raw]


# --------------------------------------------------------------------------


async def test_a_masked_cell_is_hidden_from_the_input_label_and_the_substituted_expression(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, "13位")
    metric = body["chain"][0]
    assert metric["step"] == "metric" and metric["expression"] == f"len({PHONE_CELL})"
    assert metric["substituted"] == "len(已遮罩)"
    [cell] = inputs_of(body).values()
    assert cell["value"] == "已遮罩" and cell["masked"] == ["phone"] and cell["via"] == "tool_cell"
    assert "snapshot_value" not in cell and "model_value" not in cell
    # 那一格所在的查询步骤照旧遮着；面板底下的遮罩说明带上这一列
    [query] = [s for s in body["chain"] if s["step"] == "query"]
    assert query["masked"] == ["phone"] and body["redacted"]["columns"] == ["phone"]
    assert leaks(body) == []


async def test_a_scalar_taken_by_position_from_a_masked_column_is_hidden(client, monkeypatch, shop):
    """vars.res.rows[0][2]：没有 cell() 的定位，顺着取值链认出取自查询结果的 phone 列。"""
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, "26位")
    metric = body["chain"][0]
    assert metric["substituted"] == "len(已遮罩) * 2"
    [phone] = inputs_of(body).values()
    assert phone["path"] == FIRST_PHONE and phone["value"] == "已遮罩" and phone["masked"] == ["phone"]
    assert body["redacted"]["columns"] == ["phone"]
    assert leaks(body) == []


async def test_only_the_masked_input_is_hidden_when_inputs_are_mixed(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, "93.0分")
    assert body["chain"][0]["substituted"] == "round(80.0 + len(已遮罩), 1)"
    inputs = inputs_of(body)
    amount = inputs["cell(nodes.fetch, 1, 'amount')"]
    assert amount["value"] == 80.0 and "masked" not in amount
    assert inputs[SECOND_PHONE]["value"] == "已遮罩" and inputs[SECOND_PHONE]["masked"] == ["phone"]
    assert leaks(body) == []
    # 没用到遮罩列的指标和以前一样
    plain = await segment(client, row.id, "120.5元")
    assert plain["chain"][0]["substituted"] == "120.5"
    [amt] = inputs_of(plain).values()
    assert amt["value"] == 120.5 and "masked" not in amt


async def test_masks_recorded_at_query_time_stay_after_the_source_drops_them(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    await set_source(shop, {})
    for text in ("13位", "26位", "93.0分"):
        body = await segment(client, row.id, text)
        assert "已遮罩" in body["chain"][0]["substituted"] and leaks(body) == [], text


async def test_masks_added_after_the_run_apply_too(client, monkeypatch, shop):
    row = await run_graph(monkeypatch)
    before = await segment(client, row.id, "26位")
    assert before["chain"][0]["substituted"] == "len('139-0000-0001') * 2"
    assert inputs_of(before)[FIRST_PHONE]["value"] == "139-0000-0001" and "masked" not in inputs_of(before)[FIRST_PHONE]
    await set_source(shop, {"mask_columns": "phone"})
    after = await segment(client, row.id, "26位")
    assert after["chain"][0]["substituted"] == "len(已遮罩) * 2" and leaks(after) == []


async def test_without_masks_the_metric_step_is_unchanged(client, monkeypatch, shop):
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, "93.0分")
    assert body["chain"][0]["substituted"] == "round(80.0 + len('139-0000-0002'), 1)"
    assert all("masked" not in s for s in body["chain"] if s["step"] == "input")
    assert body["redacted"] == {"columns": []}


async def test_the_whatif_withholds_a_masked_scalar_taken_by_position(client, monkeypatch, shop):
    """推演同一套：按位置取自遮罩列的输入不给值，整个指标推演不了，和证据面板说的一致。"""
    await set_source(shop, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card", "metric": "plen2"})
    assert res.status_code == 200, res.text
    [plen2] = res.json()["metrics"]
    assert plen2["status"] == "masked" and plen2["substituted"] is None
    [phone] = plen2["inputs"]
    assert phone["path"] == FIRST_PHONE and phone["masked"] is True and phone["old"] is None
    assert not [p for p in PHONES if p in res.text]
    # 改它的值同样不行：遮罩列不能拿来推演
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                            json={"card": "card", "metric": "plen2", "overrides": {"inputs": {FIRST_PHONE: 1}}})
    assert res.status_code == 422 and "遮罩" in res.json()["detail"]
