"""可验证报告的接口：GET /api/runs/{run_id}/bundle.html。

照证据测试的夹具真跑一次：两个虚构的 SQLite 数据源（门店流水没设遮罩，会员库遮了 phone 列），口径卡、
报告引用两份快照里的格子和一个指标。核对：

- 只给已封存且封存核对一致的运行：没封存、封存后被改过都回 409；
- 导出的数据里，事件行按浏览器的拼法重算等于运行记下的 manifest_hash；
- 遮罩库的快照只有 id 没有内容，它的查询事件只有哈希；
- 附件名是「<运行名>-可验证报告.html」，中文名按 RFC 5987 编码；
- 只读：导出前后运行记录、事件一条不多一条不少。

另外核对工件库新加的 read_bytes：原样取回文件字节。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app.core.artifact_store import canonical_json, content_hash, put_json, read_bytes
from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.runner import run_manager
from app.main import app

SHOP_SQL = "SELECT id, store, amount FROM orders ORDER BY id"
CRM_SQL = "SELECT id, name, phone, spend FROM members ORDER BY id"
TEXT = ("## 门店周报\n\n青石店本周流水 [[v:Q1.r0.amount]]，会员消费 [[v:Q2.r1.spend]]，"
        "口径卡销售额 [[m:gmv]]。[[see:Q1]]")


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(name, path, options=None):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture(autouse=True)
async def sources(tmp_path):
    shop, crm = tmp_path / "shop.db", tmp_path / "crm.db"
    db = sqlite3.connect(shop)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, store TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "青石店", 1280.5), (2, "白沙店", 960.0)])
    db.commit()
    db.close()
    db = sqlite3.connect(crm)
    db.executescript("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, spend REAL);")
    db.executemany("INSERT INTO members VALUES (?, ?, ?, ?)", [(1, "林一", "000-0001", 320.0),
                                                              (2, "周二", "000-0002", 188.5)])
    db.commit()
    db.close()
    ids = [await set_source("bundle_shop", shop), await set_source("bundle_crm", crm, {"mask_columns": ["phone"]})]
    for source_id in ids:
        await engines.invalidate(source_id)
    yield
    for source_id in ids:
        await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(text_nodes=()):
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__bundle_shop", args={"sql": SHOP_SQL}),
        node("members", "tool", tool="db_query__bundle_crm", args={"sql": CRM_SQL}),
        node("card", "metrics", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
             "expression": "cell(nodes.fetch, 0, 'amount')"}]),
        node("write", "report", instructions="写周报", on_violation="flag"),
        *text_nodes,
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, text=TEXT):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def sealed_run(monkeypatch, text=TEXT) -> Run:
    script(monkeypatch, text)
    row = await wait((await run_manager.start(graph=weekly(), input_payload={})).id)
    assert row.status == "succeeded", row.error
    return row


def data_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="bundle-data">(.*?)</script>', html, re.S)
    assert m, "页面里没有数据块"
    return json.loads(m.group(1))


async def query_artifact(run_id: str, node_id: str) -> str:
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "tool.end", RunEvent.node_id == node_id))).scalars()
    return end.data["query_artifact"]


def file_of(artifact: str):
    return settings.data_dir / "artifacts" / artifact[:2] / f"{artifact}.json"


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


async def test_export_rebuilds_the_runners_manifest(client, monkeypatch):
    row = await sealed_run(monkeypatch)
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    bundle = data_of(resp.text)
    meta = bundle["meta"]
    assert meta["run_id"] == row.id and meta["manifest_hash"] == row.manifest_hash
    assert meta["manifest_seq"] == row.manifest_seq and meta["legacy"] is False

    lines = [f"{e['seq']}|{e['type']}|{e['node_id'] or ''}|{e['data_sha256']}"
             for e in sorted(bundle["events"], key=lambda e: e["seq"])]
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == row.manifest_hash
    assert max(e["seq"] for e in bundle["events"]) == row.manifest_seq
    async with SessionLocal() as session:
        rows = {r.seq: r for r in (await session.execute(
            select(RunEvent).where(RunEvent.run_id == row.id))).scalars()}
    for item in bundle["events"]:
        if "canonical" in item:
            assert item["canonical"] == canonical_json(rows[item["seq"]].data)
            assert content_hash(item["canonical"]) == item["data_sha256"]
    types = {e["type"] for e in bundle["events"] if "canonical" in e}
    assert {"run.started", "report.checked", "tool.end", "node.finished"} <= types
    assert not types & {"llm.start", "llm.end", "node.started", "tool.start", "log"}


async def test_the_masked_source_ships_only_hashes(client, monkeypatch):
    row = await sealed_run(monkeypatch)
    shop, crm = await query_artifact(row.id, "fetch"), await query_artifact(row.id, "members")
    bundle = data_of((await client.get(f"/api/runs/{row.id}/bundle.html")).text)
    by_id = {a["id"]: a for a in bundle["artifacts"]}
    assert by_id[crm] == {"id": crm, "kind": "query", "node_id": "members", "masked": True}
    assert base64.b64decode(by_id[shop]["b64"]) == file_of(shop).read_bytes()
    report = next(a for a in bundle["artifacts"] if a["kind"] == "report_doc")
    assert json.loads(base64.b64decode(report["b64"]))["run_id"] == row.id
    assert {a["kind"] for a in bundle["artifacts"]} == {"report_doc", "metric_set", "query"}

    ends = {e["node_id"]: e for e in bundle["events"] if e["type"] == "tool.end"}
    assert "canonical" in ends["fetch"] and "canonical" not in ends["members"]
    # 运行里有带遮罩的快照：成果节点、运行结束事件的预览可能带出查询结果，只给哈希
    out = next(e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "out")
    finished = next(e for e in bundle["events"] if e["type"] == "run.finished")
    assert "canonical" not in out and "canonical" not in finished
    assert "000-0001" not in json.dumps(bundle, ensure_ascii=False)


async def test_the_page_is_named_after_the_run(client, monkeypatch):
    row = await sealed_run(monkeypatch)
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    header = resp.headers["content-disposition"]
    assert header.startswith("attachment;")
    assert f"filename*=UTF-8''{quote('未保存的工作流-可验证报告.html', safe='')}" in header
    assert resp.headers.get("cache-control") == "no-store"

    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
        run.workflow_name = "门店周报"
        await session.commit()
    header = (await client.get(f"/api/runs/{row.id}/bundle.html")).headers["content-disposition"]
    assert f"filename*=UTF-8''{quote('门店周报-可验证报告.html', safe='')}" in header


async def test_report_text_cannot_close_the_data_block(client, monkeypatch):
    row = await sealed_run(monkeypatch, text="结论见下：</script><script>alert(1)</script> 流水 [[v:Q1.r0.amount]]。")
    html = (await client.get(f"/api/runs/{row.id}/bundle.html")).text
    assert html.count("</script>") == 2
    bundle = data_of(html)
    write = next(e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "write")
    assert "</script><script>alert(1)</script>" in write["canonical"]


async def test_events_after_the_seal_are_listed_apart(client, monkeypatch):
    row = await sealed_run(monkeypatch)
    async with SessionLocal() as session:
        session.add(RunEvent(run_id=row.id, seq=row.manifest_seq + 3, type="evidence.judged", node_id="write",
                             ts=0.0, data={"verdicts": {"u1": {"verdict": "supported"}}, "model": "judge-x",
                                           "post_seal": True}))
        await session.commit()
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    bundle = data_of(resp.text)
    assert max(e["seq"] for e in bundle["events"]) == row.manifest_seq
    [later] = bundle["appended"]
    assert later["seq"] == row.manifest_seq + 3 and later["type"] == "evidence.judged"


# --------------------------------------------------------------------------
# 拒绝、只读
# --------------------------------------------------------------------------


async def test_an_unsealed_run_is_refused(client):
    async with SessionLocal() as session:
        run = Run(workflow_name="门店周报", status="running", graph={"nodes": [], "edges": []})
        session.add(run)
        await session.commit()
        run_id = run.id
    resp = await client.get(f"/api/runs/{run_id}/bundle.html")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "运行尚未结束封存，结束后才能导出", "code": "bundle_unsealed"}


@pytest.mark.parametrize("status", ["running", "interrupted", "queued"])
async def test_a_run_going_again_on_an_old_seal_is_refused(client, monkeypatch, status):
    """失败后接着跑的运行留着上一次的封存，跑完会重新封存、换掉清单哈希；早年的运行停在审批时也封存过。
    这时导出去的文件，封存哈希和系统里之后记的对不上，页面上「去系统里对照」那一步就作废了：一样回 409。"""
    row = await sealed_run(monkeypatch)
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
        run.status = status
        await session.commit()
    assert (await client.get(f"/api/runs/{row.id}/verify")).json()["ok"] is True
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 409
    assert resp.json() == {"detail": "运行尚未结束封存，结束后才能导出", "code": "bundle_unsealed"}


async def test_a_failed_run_is_exportable(client, monkeypatch):
    """失败、取消的运行同样封存了：报告已经写出来的，照样导得出去。"""
    row = await sealed_run(monkeypatch)
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
        run.status = "failed"
        await session.commit()
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    assert data_of(resp.text)["meta"]["status"] == "failed"


async def test_a_broken_seal_is_refused(client, monkeypatch):
    row = await sealed_run(monkeypatch)
    async with SessionLocal() as session:
        event = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "report.checked"))).scalar_one()
        event.data = {**event.data, "ok": not event.data.get("ok")}
        await session.commit()
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "bundle_seal_broken" and "封存核对不一致" in body["detail"], body


async def test_an_unknown_run_is_404(client):
    resp = await client.get("/api/runs/nope/bundle.html")
    assert resp.status_code == 404


async def test_exporting_appends_nothing(client, monkeypatch):
    row = await sealed_run(monkeypatch)

    async def snapshot():
        async with SessionLocal() as session:
            run = await session.get(Run, row.id)
            count = (await session.execute(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == row.id))).scalar_one()
            return run.last_seq, count, run.manifest_hash, run.manifest_seq, run.status

    before = await snapshot()
    for _ in range(2):
        assert (await client.get(f"/api/runs/{row.id}/bundle.html")).status_code == 200
    assert await snapshot() == before
    assert (await client.get(f"/api/runs/{row.id}/verify")).json()["ok"] is True


# --------------------------------------------------------------------------
# 工件库：read_bytes
# --------------------------------------------------------------------------


async def test_read_bytes_returns_the_file_as_stored():
    artifact = await put_json({"门店": "青石店", "流水": 1280.5}, kind="test")
    assert read_bytes(artifact) == file_of(artifact).read_bytes()
    assert read_bytes(artifact) == canonical_json({"门店": "青石店", "流水": 1280.5}).encode()
    assert read_bytes("0" * 64) is None and read_bytes("../etc/passwd") is None and read_bytes("") is None


async def test_read_bytes_checks_the_hash_unless_told_not_to():
    artifact = await put_json({"门店": "白沙店", "流水": 960.0}, kind="test")
    path = file_of(artifact)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"960.0", b"999.0"))
    try:
        with pytest.raises(ValueError):
            read_bytes(artifact)
        assert read_bytes(artifact, verify=False) == original.replace(b"960.0", b"999.0")
    finally:
        path.write_bytes(original)
