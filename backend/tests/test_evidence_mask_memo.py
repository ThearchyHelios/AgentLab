"""证据面板、推演收「这次运行查到过的遮罩值」：只收确定取自遮罩列的值；按快照工件在进程里记下，不再每次都读一遍。

复核发现（第 3 轮）：
- 收值按的是面板展示用的 hidden（宁可多遮）：SQL 认不准、整份遮的快照，hidden 是全部列，里面的区域名、金额都被收成
  遮罩值。之后任何查询步骤里展示的 SQL「WHERE region = '华东' AND amount > 12345.5」都显示成「已遮罩」，整份结果当输入
  的指标值等于这些数时也写「已遮罩」。现在只收确定取自遮罩列的那几列（_MaskSets.sure，同 masking 收值的口径），整份遮
  的快照只遮显示
- 每点开一个片段，都把这次运行所有设了遮罩的数据源上的快照读盘、复验哈希，再逐份做 SQL 分析（一份几十毫秒），缓存
  只在一次请求里有效。现在按快照工件 id 在进程里记下分析和收到的值（工件按内容寻址、不会变；键里带着数据源现在设的
  遮罩和类型，设置改了不会拿旧的），之后的请求不再读、不再分析；推演同一份记录

快照、号码都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

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

CUSTOMERS = [(1, "客户甲", "19900000901"), (2, "客户乙", "19900000902"), (3, "客户丙", "19900000903")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25)]
PHONES = [c[2] for c in CUSTOMERS]

#: 派生来源里有没起别名的 UPPER(c.phone)：认遮罩列的那一套认不准，整份的列都遮（只遮显示）
SQL_WHOLE = ("SELECT region, amount FROM (SELECT 'east' AS region, UPPER(c.phone), o.amount FROM customers c "
             "JOIN orders o ON o.customer_id = c.id) t")
SQL_TEL = "SELECT c.name AS customer, c.phone AS tel FROM customers c ORDER BY c.id"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "mall"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="mall", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture
async def mall(tmp_path, engine_up):
    from app.api.evidence import _MEMO

    path = tmp_path / "mall.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", ORDERS)
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    # 同一个测试进程里别的测试记下的结果（内容相同的快照工件 id 一样）不算进这里的读盘次数
    _MEMO.clear()
    yield path
    await engines.invalidate(source_id)


async def put(sql: str, columns: list[str], rows: list[list]) -> str:
    return await artifact_store.put_json({"sql": sql, "columns": columns, "rows": rows, "source": "mall"}, kind="query")


def sealed_of(*artifacts: str) -> SimpleNamespace:
    return SimpleNamespace(queries={a: {"source": "mall"} for a in artifacts})


# --------------------------------------------------------------------------
# 只收确定取自遮罩列的值
# --------------------------------------------------------------------------


async def test_values_of_a_snapshot_masked_whole_are_not_collected(engine_up, mall):
    """整份遮的快照只遮显示：里面的区域名、金额不收成遮罩值，别的查询步骤里写着它们的 SQL 照原样显示；改了名字的号码列
    （c.phone AS tel）照收。"""
    from app.api.evidence import _Masks, _Shown

    whole = await put(SQL_WHOLE, ["region", "amount"], [["华东", 12345.5], ["华南", 6789.25]])
    tel = await put(SQL_TEL, ["customer", "tel"], [[c[1], c[2]] for c in CUSTOMERS])
    assert await _Masks(sealed_of(whole)).run_secrets() is None
    secrets = await _Masks(sealed_of(whole, tel)).run_secrets()
    sql = f"SELECT SUM(amount) FROM orders WHERE region = '华东' AND amount > 12345.5 AND phone <> '{PHONES[0]}'"
    shown = _Shown(secrets)
    assert shown.text(sql) == sql.replace(PHONES[0], "已遮罩") and shown.changed, shown.text(sql)


@pytest.mark.parametrize("sql, column", [
    ("SELECT UPPER(phone) FROM customers", "UPPER(phone)"),
    ("SELECT TRIM(c.phone) FROM customers c ORDER BY c.id", "TRIM(c.phone)"),
])
async def test_values_of_an_unaliased_expression_over_every_column_are_collected(engine_up, mall, sql, column):
    """复核第 4 轮：没起别名、表达式盖住全部结果列，SQL 认得出这一列取自遮罩列（按位置），不是认不准、整份算进来的：
    算确定取自遮罩列（sure），值照收，之后别的查询步骤里写着的号码显示成「已遮罩」（第 3 轮一个值都收不到，号码原样显示）。"""
    from app.api.evidence import _Masks, _Shown, _mask_sets

    art = await put(sql, [column], [[p] for p in PHONES])
    sets = await _mask_sets(artifact_store.load(art), "mall", _Masks(), artifact=art)
    assert sets.sure == {column.lower()}, (sets.sure, sets.hidden)
    secrets = await _Masks(sealed_of(art)).run_secrets()
    probe = f"SELECT SUM(amount) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.phone = '{PHONES[1]}'"
    assert _Shown(secrets).text(probe) == probe.replace(PHONES[1], "已遮罩")


# --------------------------------------------------------------------------
# 按快照工件在进程里记下
# --------------------------------------------------------------------------


def spies(monkeypatch) -> tuple[list[str], list[str]]:
    """(读过的工件, 做过的 SQL 分析)：证据面板经 artifact_store.load 读快照（查询步骤直接读，收值经 _fetch），按 SQL 认
    遮罩列经可验证报告和 masking 的两个函数。"""
    from app.api import evidence as api
    from app.bundle import build
    from app.engine import masking

    fetched: list[str] = []
    analysed: list[str] = []
    real_load, real_bundle, real_masking = api.load, build._derived_columns, masking._derived_hidden
    monkeypatch.setattr(api, "load", lambda artifact: fetched.append(artifact) or real_load(artifact))
    monkeypatch.setattr(build, "_derived_columns",
                        lambda *a, **k: analysed.append("bundle") or real_bundle(*a, **k))
    monkeypatch.setattr(masking, "_derived_hidden", lambda *a, **k: analysed.append("masking") or real_masking(*a, **k))
    return fetched, analysed


async def test_the_values_are_read_and_analysed_once_per_process(engine_up, mall, monkeypatch):
    """第一次收：设了遮罩的快照各读一次、分析一次。之后的请求（新的 _Masks）一份都不再读、不再分析，收到的值一样。"""
    from app.api.evidence import _Masks

    tel = await put(SQL_TEL, ["customer", "tel"], [[c[1], c[2]] for c in CUSTOMERS])
    raw = await put("SELECT id, phone FROM customers ORDER BY id", ["id", "phone"], [[c[0], c[2]] for c in CUSTOMERS])
    fetched, analysed = spies(monkeypatch)
    first = await _Masks(sealed_of(tel, raw)).run_secrets()
    assert sorted(fetched) == sorted([tel, raw]) and analysed, (fetched, analysed)
    assert set(PHONES) <= first.strong
    fetched.clear()
    analysed.clear()
    second = await _Masks(sealed_of(tel, raw)).run_secrets()
    assert fetched == [] and analysed == [] and second.strong == first.strong, (fetched, analysed)


async def test_a_changed_mask_setting_is_not_served_from_the_memo(engine_up, mall, monkeypatch):
    """数据源的遮罩设置改了：键里带着现在设的遮罩，按新的设置重新收；不再设遮罩的数据源一份都不读。"""
    from app.api.evidence import _Masks, _Shown

    tel = await put(SQL_TEL, ["customer", "tel"], [[c[1], c[2]] for c in CUSTOMERS])
    assert "客户甲" not in (await _Masks(sealed_of(tel)).run_secrets()).strong
    await set_source(mall, {"mask_columns": ["phone", "name"]})
    widened = await _Masks(sealed_of(tel)).run_secrets()
    assert {"客户甲", PHONES[0]} <= widened.strong
    assert _Shown(widened).text("WHERE c.name = '客户甲'") == "WHERE c.name = '已遮罩'"
    await set_source(mall, {})
    fetched, _ = spies(monkeypatch)
    assert await _Masks(sealed_of(tel)).run_secrets() is None and fetched == []


async def test_the_whatif_collects_from_the_same_memo(engine_up, mall):
    """推演在工作线程里收（_Hidden.secrets，经推演自己的 loader 读）：第一次读一份，之后的推演、证据面板都不再读。"""
    from app.api.evidence import _Masks
    from app.api.whatif import _Hidden

    tel = await put(SQL_TEL, ["customer", "tel"], [[c[1], c[2]] for c in CUSTOMERS])
    reads: list[str] = []

    def loader(artifact: str):
        reads.append(artifact)
        return artifact_store.load(artifact)

    loop = asyncio.get_running_loop()
    first = await asyncio.to_thread(_Hidden(sealed_of(tel), loader, loop).secrets)
    assert reads == [tel] and set(PHONES) <= first.strong
    reads.clear()
    second = await asyncio.to_thread(_Hidden(sealed_of(tel), loader, loop).secrets)
    assert reads == [] and second.strong == first.strong
    assert (await _Masks(sealed_of(tel)).run_secrets()).strong == first.strong


# --------------------------------------------------------------------------
# 片段接口：之后点开只读这一步要显示的快照
# --------------------------------------------------------------------------


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


SQL_BY_PHONE = ("SELECT ROUND(SUM(o.amount), 2) AS spend FROM orders o JOIN customers c ON c.id = o.customer_id "
                f"WHERE c.phone IN ('{PHONES[0]}', '{PHONES[1]}')")


async def test_a_segment_opened_again_reads_only_its_own_snapshot(client, monkeypatch, mall):
    """点开一个片段：第一次要把这次运行设了遮罩的快照读一遍（收遮罩值，SQL 里写着号码时展示要换掉）；再点开（新的请求）
    只读这一步要显示的那一份、不再做 SQL 分析，号码照样换掉。"""
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="两位重点客户共消费 [[v:Q2.r0.spend]]。"))
    nodes = [node("start", "input"),
             node("q_cust", "tool", tool="db_query__mall", args={"sql": "SELECT id, phone AS tel FROM customers ORDER BY id"}),
             node("q_one", "tool", tool="db_query__mall", args={"sql": SQL_BY_PHONE}),
             node("write", "report", instructions="写一句", on_violation="flag"),
             node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])]
    spec = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    run_id = (await run_manager.start(graph=spec, input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    [report] = (await client.get(f"/api/runs/{run_id}/evidence")).json()["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "v:Q2.r0.spend")
    catalog = doc["catalog"]
    cust, one = catalog["Q1"]["artifact"], catalog["Q2"]["artifact"]
    fetched, analysed = spies(monkeypatch)
    for attempt in range(2):
        fetched.clear()
        analysed.clear()
        body = (await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")).json()
        [step] = [s for s in body["chain"] if s["step"] == "query"]
        assert "IN ('已遮罩', '已遮罩')" in step["sql"] and PHONES[0] not in json.dumps(body, ensure_ascii=False)
        assert one in fetched, fetched
        assert (cust in fetched) is (attempt == 0), (attempt, fetched)
        assert bool(analysed) is (attempt == 0), (attempt, analysed)
