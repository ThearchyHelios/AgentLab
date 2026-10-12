"""原样导出的可验证报告：写作者自己写在引用旁边的普通文字不能把核对过的数判成不通过、无法核对，总览要是绿的。

复核第 1 轮发现（相对基线的回退）：显示文字核对把「紧挨着负号」改判不通过，负号却连破折号「——」「—」和汉字「一」都算上了。
写作者写「全场运费统一[[m:fee]]，消费合计——[[m:total]]。」，服务端自查通过、正式运行成功，原样导出断网打开，总览却是
「有 2 项不通过」。现在只有真当负号用的三个（半角、数学减号、全角）、前面又不是字母数字的才判不通过；破折号、汉字「一」
当普通的字；总览只有一项无法核对都没有才是绿的。这里真跑一遍报告撰写节点、从接口导出，再在 node 里执行页面上的核对脚本。

数据全是虚构的。
"""
from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_masked_metrics import data_of
from tests.test_bundle_shown import verdicts
from tests.test_report_masked_metrics import events, finish, node, script

#: 写作者的写法：引用前面紧挨着「统一」「——」「—」「–」「SKU-」「共」，后面紧挨着「家」「元」
PROSE = ("全场运费统一[[m:fee]]，消费合计——[[m:total]]，退款—[[m:refund]]，共[[m:stores]]家门店，"
         "热销款 SKU-[[v:Q1.r0.sku]]，单价区间 [[v:Q1.r0.price]]–[[v:Q1.r1.price]]。")


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
async def shop(tmp_path, engine_up):
    """门店库：没设遮罩。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE items (sku INTEGER, price REAL);"
                     "CREATE TABLE stores (id INTEGER, amount REAL, refund REAL);")
    db.executemany("INSERT INTO items VALUES (?, ?)", [(1207, 19.9), (1311, 39.5)])
    db.executemany("INSERT INTO stores VALUES (?, ?, ?)", [(1, 1234.5, -20.5), (2, 2345.25, 0), (3, 3456.75, -8)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "n1shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="n1shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


async def test_ordinary_writing_next_to_citations_exports_all_green(client, monkeypatch, shop):
    """报告撰写节点照写作者的稿子组装（正式运行，违规处置判失败），自查通过；导出的可验证报告里每个数字都核对通过，总览
    是绿色的「全部通过」。"""
    script(monkeypatch, PROSE)
    metric = {"format": "thousands", "decimals": 2}
    nodes = [
        node("start", "input"),
        node("q", "tool", tool="db_query__n1shop", args={"sql": "SELECT sku, price FROM items ORDER BY sku"}),
        node("q2", "tool", tool="db_query__n1shop", args={
            "sql": "SELECT SUM(amount) AS total, SUM(refund) AS refund, COUNT(*) AS stores, 10 AS fee FROM stores"}),
        node("card", "metrics", caliber="门店口径", caliber_version="v1", metrics=[
            {"id": "fee", "name": "运费", "unit": "元", "format": "plain", "expression": "cell(nodes.q2, 0, 'fee')"},
            {"id": "total", "name": "消费合计", "unit": "元", **metric, "expression": "cell(nodes.q2, 0, 'total')"},
            {"id": "refund", "name": "退款", "unit": "元", **metric, "expression": "cell(nodes.q2, 0, 'refund')"},
            {"id": "stores", "name": "门店数", "format": "plain", "expression": "cell(nodes.q2, 0, 'stores')"}]),
        node("write", "report", instructions="写一句", on_violation="fail"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    spec = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    row = await finish(spec)
    assert row.status == "succeeded", row.error
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert checked["ok"] is True and checked["repairs"] == 0, checked
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    got = verdicts(data_of(resp.text))
    shown = {n["ref"]: (n["text"], n["result"]) for n in got["numbers"]}
    assert shown == {
        "m:fee": ("10元", "pass"), "m:total": ("7,036.50元", "pass"), "m:refund": ("-28.50元", "pass"),
        "m:stores": ("3", "pass"), "v:Q1.r0.sku": ("1,207", "pass"), "v:Q1.r0.price": ("19.9", "pass"),
        "v:Q1.r1.price": ("39.5", "pass"),
    }, shown
    assert got["overall"]["result"] == "pass" and got["overall"]["text"] == "全部通过", got["overall"]
