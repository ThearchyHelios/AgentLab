"""用到遮罩列的指标：推演和证据面板对「哪些输入算遮罩、指标的原值给不给」是同一个判断，证据图的标签也照这个判断。

复核发现：
- 整份结果当输入（len(vars.cust.rows)）：推演只要结果里有一列遮着就把原值写成「已遮罩」，同一屏的指标步骤照写 3、报告
  正文也写 3 位；str(vars.cust.rows) 反过来，推演遮了、指标步骤把整份结果连号码给了出来
- 认不出的取值写法照样给出号码：整行 vars.cust.rows[0]、下标不是字面量的 vars.cust.rows[len(…) - 1][2]（指标遮了、同一
  路径的输入照给）、数据整形把号码取出来再赋给变量（vars.first_phone）
- 证据图的 evidence[].label（「名称 = 渲染值」）照给用到遮罩列的指标的值，报告没引用它也一样

现在推演（engine/recompute.py）和证据面板（api/evidence.py 的 _metric_masks）共用一套输入分类和一个判断
（recompute.withheld_value）：取自遮罩列的一格、一行的不给；整份结果当输入、认不出出处的输入，按值认（这次运行查到过的
遮罩值），值里有遮罩值才不给——数行数的照给。证据图的标签按同一个判断换成「已遮罩」。

真跑一遍图（虚构的 SQLite 会员库，号码是编的），报告由 MockChatModel 照剧本写。
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

CUSTOMERS = [(1, "客户甲", "19900000501"), (2, "客户乙", "19900000502"), (3, "客户丙", "19900000503")]
PHONES = [c[2] for c in CUSTOMERS]
METRICS = [
    {"id": "n_cust", "name": "客户数", "unit": "位", "format": "thousands", "expression": "len(vars.cust.rows)"},
    {"id": "dump", "name": "客户清单", "format": "plain", "expression": "str(vars.cust.rows)"},
    {"id": "row0", "name": "首位客户", "format": "plain", "expression": "str(vars.cust.rows[0])"},
    {"id": "last", "name": "末位号码", "format": "plain", "expression": "vars.cust.rows[len(vars.cust.rows) - 1][2]"},
    {"id": "phone_var", "name": "整形取出的号码", "format": "plain", "expression": "vars.first_phone"},
    {"id": "phone_raw", "name": "首位客户手机号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'phone')"},
    {"id": "id0", "name": "首位客户编号", "format": "plain", "expression": "cell(nodes.q_cust, 0, 'id')"},
]
#: 报告只写客户数和编号，别的指标只挂作依据：正文里没有号码
TEXT = "共有 [[m:n_cust]] 客户，首位编号 [[m:id0]]。[[see:m:phone_raw]]首位客户的联系方式已登记。"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def members(tmp_path, engine_up):
    path = tmp_path / "members.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "members"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="members", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = {"mask_columns": ["phone"]}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph() -> dict:
    nodes = [
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__members", args={"sql": "SELECT id, name, phone FROM customers ORDER BY id"}),
        node("parse", "transform", mode="json", template="{{ nodes.q_cust }}", assign_to="cust"),
        # 数据整形把号码取出来再赋给变量：卡上的 vars.first_phone 顺着取值链认不出是哪一列
        node("pick", "transform", mode="expression", expression="vars.cust.rows[0][2]", assign_to="first_phone"),
        node("card", "metrics", caliber="遮罩对照", caliber_version="v1", metrics=METRICS),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


@pytest.fixture
async def run(monkeypatch, members) -> Run:
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


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的（19,900,000,101）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


async def whatif(client, run_id: str) -> dict[str, dict]:
    res = await client.post(f"/api/runs/{run_id}/evidence/whatif", json={"card": "card"})
    assert res.status_code == 200, res.text
    assert leaks(res.json()) == [], "推演答复里不出现号码"
    return {m["id"]: m for m in res.json()["metrics"]}


async def metric_step(client, run_id: str, metric: str) -> dict:
    """指标步骤：报告引用了的按片段取；只挂作依据、没写进正文的，借一个引用它的片段造一个（同一张卡、同一个台账）。"""
    from app.api.evidence import _Masks, _metric_chain, _sealed

    sealed = await _sealed(run_id)
    [report] = (await client.get(f"/api/runs/{run_id}/evidence")).json()["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    entry = {**doc["catalog"]["m:id0"], "locator": {"metric": metric}}
    cite = {"alias": f"m:{metric}", "kind": "metric", "status": "resolved", "locator": {"metric": metric}}
    chain = await _metric_chain({"text": "—"}, cite, entry, doc, sealed, _Masks(sealed))
    return {"metric": chain[0], "inputs": [s for s in chain if s["step"] == "input"], "chain": chain}


# --------------------------------------------------------------------------
# 推演和指标步骤：同一个判断
# --------------------------------------------------------------------------


async def test_a_row_count_over_a_masked_result_is_shown_in_both_places(client, run):
    """len(vars.cust.rows)：整份结果有一列遮着，推演不了（勾行能反推那一格），可行数不是遮罩值：推演的原值、指标步骤的值
    都给 3。整份结果那个输入照旧不给（推演、指标步骤都写已遮罩）。"""
    m = (await whatif(client, run.id))["n_cust"]
    assert m["status"] == "masked" and m["old"] == 3 and m["rendered_old"] == "3位" and not m.get("withheld"), m
    [rows] = m["inputs"]
    assert rows["path"] == "vars.cust.rows" and rows["masked"] is True and rows["old"] is None
    step = await metric_step(client, run.id, "n_cust")
    assert step["metric"]["value"] == 3, step["metric"]
    [chip] = step["inputs"]
    assert chip["value"] == "已遮罩" and chip["masked"] == ["phone"]
    # 代入式里整份结果那一处只留路径，不写成「已遮罩」
    assert step["metric"]["substituted"] == "len(vars.cust.rows)"


@pytest.mark.parametrize("metric", ["dump", "row0", "last", "phone_var", "phone_raw"])
async def test_a_value_carrying_a_masked_value_is_withheld_in_both_places(client, run, metric):
    """值里就是号码的指标：整份结果转成文字、整行、下标不是字面量的一格、数据整形取出来的号码、cell() 取的号码。推演的原值、
    指标步骤的值都写「已遮罩」，输入也不给值，号码一个都不交出来。"""
    m = (await whatif(client, run.id))[metric]
    assert m["status"] == "masked" and m["old"] == m["rendered_old"] == "已遮罩" and m["withheld"] is True, m
    assert all(i["old"] is None for i in m["inputs"]), m["inputs"]
    step = await metric_step(client, run.id, metric)
    assert step["metric"]["value"] == "已遮罩", step["metric"]
    assert all(i["value"] == "已遮罩" for i in step["inputs"]), step["inputs"]
    assert leaks(step["chain"]) == []


async def test_a_value_from_an_unmasked_cell_is_given_in_both_places(client, run):
    m = (await whatif(client, run.id))["id0"]
    assert (m["status"], m["old"]) == ("unchanged", 1) and not m.get("withheld")
    assert (await metric_step(client, run.id, "id0"))["metric"]["value"] == 1


async def test_a_scalar_reshaped_into_a_variable_is_recognised_by_value(client, run):
    """vars.first_phone 认不出出处，值却就是号码：按值认出来，推演说清是值与遮罩列中的值相同，指标步骤的输入说明同样。"""
    m = (await whatif(client, run.id))["phone_var"]
    assert "与设置了遮罩的列中的值相同" in m["reason"], m["reason"]
    [chip] = (await metric_step(client, run.id, "phone_var"))["inputs"]
    assert chip["mask_unverified"] == "值与设置了遮罩的列中的值相同" and "masked" not in chip, chip
    res = await client.post(f"/api/runs/{run.id}/evidence/whatif",
                            json={"card": "card", "overrides": {"inputs": {"vars.first_phone": 1}}})
    assert res.status_code == 422 and "与设置了遮罩的列中的值相同" in res.json()["detail"]


# --------------------------------------------------------------------------
# 证据图的标签
# --------------------------------------------------------------------------


async def test_the_evidence_graph_labels_follow_the_same_rule(client, run):
    """证据图的 evidence[].label：用到遮罩列、值里有遮罩值的指标写「名称 = 已遮罩」，客户数、编号照给；整份答复里没有号码
    （报告只把 phone_raw 挂作依据，正文里没有号码）。"""
    body = (await client.get(f"/api/runs/{run.id}/evidence")).json()
    labels = {e["alias"]: e["label"] for e in body["evidence"] if e["kind"] == "metric"}
    assert labels["m:phone_raw"] == "首位客户手机号 = 已遮罩" and labels["m:phone_var"] == "整形取出的号码 = 已遮罩"
    assert labels["m:dump"] == "客户清单 = 已遮罩" and labels["m:row0"] == "首位客户 = 已遮罩"
    assert labels["m:n_cust"] == "客户数 = 3位" and labels["m:id0"] == "首位客户编号 = 1"
    assert leaks(body) == []
