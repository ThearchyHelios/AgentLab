"""值取自遮罩格的口径卡指标只按「确定取自遮罩列」认：SQL 认不准、整份遮的快照里，没确定取自遮罩列的普通金额、计数、标签列，
指标照常显示、照常引用。

复核第 1 轮发现（相对基线的回退）：写作目录按证据面板宁可多遮的那一套（_mask_sets 的 hidden）认指标的出处。SQL 认不准时
（WITH 写了列名表、VALUES 当表、子查询里有没起别名的表达式）整份结果的列都算遮罩列，完全没碰号码列的销售额也被写成
「已遮罩」，正式运行引用它就判失败，写作者也改不掉；同一格按 [[v:…]] 引用却不报违规（单元格引用的自查只认确定的那一套，
_cited_masks）。可验证报告认老文档时同样多算，证据目录里写着这类普通金额的老文档整份只给哈希。

现在写作目录、报告自查、可验证报告（含老文档只给哈希的判断）认指标取不取自遮罩格，都按单元格引用自查的那一套
（_cited_masks：_mask_sets 的 sure，再按血缘排除值确实不取自遮罩列的）。证据面板、推演照旧宁可多遮。

真跑的部分用虚构的 SQLite 收银库（号码是编的），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import base64
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.artifact_store import read_bytes
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_shown import verdicts
from tests.test_report_masked_metrics import doc_of, events, finish, metric_lines, node, script

ROWS = [(1, "顾客甲", "19900001101", 1234.5), (2, "顾客乙", "19900001102", 2345.25), (3, "顾客丙", "19900001103", 3456.75)]
PHONES = [r[2] for r in ROWS]
GMV = {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
       "expression": "cell(nodes.q, 0, 'gmv')"}

#: 没碰号码列的查询，SQL 却认不准结果里哪一列取自哪一列（证据面板、可验证报告的快照按整份遮）：(SQL, 销售额的渲染)
PLAIN = [
    # WITH 写了列名表
    ("WITH s(cid, amt) AS (SELECT id, amount FROM customers) SELECT SUM(amt) AS gmv FROM s", "7,036.50元"),
    # VALUES 当参数表
    ("WITH params(lo) AS (VALUES (1000)) SELECT SUM(amount) AS gmv FROM customers, params WHERE amount >= lo",
     "7,036.50元"),
    ("WITH v(id) AS (VALUES (1), (2)) SELECT SUM(c.amount) AS gmv FROM customers c JOIN v ON v.id = c.id",
     "3,579.75元"),
    # 子查询里有没起别名的号码表达式，外层只取金额
    ("SELECT id, amount AS gmv FROM (SELECT id, UPPER(phone), amount FROM customers) ORDER BY id", "1,234.50元"),
]


def leaks(value) -> list[str]:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


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
async def pos(tmp_path, engine_up):
    """收银库：顾客的号码设了遮罩。"""
    path = tmp_path / "pos.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "n1pos"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="n1pos", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = {"mask_columns": ["phone"]}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def flow(sql: str, metrics: list[dict], **write) -> dict:
    """收银查询 → 口径卡 → 报告（违规处置默认判失败）→ 成果。"""
    nodes = [
        node("start", "input"),
        node("q", "tool", tool="db_query__n1pos", args={"sql": sql}),
        node("card", "metrics", caliber="收银口径", caliber_version="v1", metrics=metrics),
        node("write", "report", instructions="写一句", **{"on_violation": "fail", **write}),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


# --------------------------------------------------------------------------
# 写作目录、报告自查
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, shown", PLAIN)
async def test_an_amount_from_a_query_whose_sql_cannot_be_traced_is_given_and_citable(monkeypatch, pos, sql, shown):
    """没碰号码列的销售额：写作目录照写它的值（以前写「已遮罩」），正式运行引用它照常通过（以前判失败），同一格按
    [[v:…]] 引用同样不报违规——按指标引用和按格引用是同一个判断。"""
    seen = script(monkeypatch, "本期销售额 [[m:gmv]]，对照 [[v:Q1.r0.gmv]]。")
    row = await finish(flow(sql, [GMV]))
    assert row.status == "succeeded", row.error
    lines = metric_lines(seen[0])
    assert f"销售额 = {shown}" in lines and "已遮罩" not in lines, lines
    [checked] = [e.data for e in await events(row.id) if e.type == "report.checked"]
    assert checked["ok"] is True and checked["repairs"] == 0, checked
    doc = await doc_of(row.id)
    assert "masked" not in doc["catalog"]["m:gmv"] and f"本期销售额 {shown}" in doc["markdown"], doc["markdown"]


async def test_a_label_column_that_only_bands_the_masked_column_is_given_and_citable(monkeypatch, pos):
    """只装着分档标签的列（CASE 里写死的「已留」「未留」）：值里没有号码，指标照给、照常引用。"""
    seen = script(monkeypatch, "首位顾客 [[m:has]] 号码，对照 [[v:Q1.r0.has_phone]]。")
    sql = "SELECT id, CASE WHEN phone IS NULL THEN '未留' ELSE '已留' END AS has_phone FROM customers ORDER BY id"
    row = await finish(flow(sql, [{"id": "has", "name": "首位顾客留号", "format": "plain",
                                   "expression": "cell(nodes.q, 0, 'has_phone')"}]))
    assert row.status == "succeeded", row.error
    assert "首位顾客留号 = 已留" in metric_lines(seen[0])
    doc = await doc_of(row.id)
    assert "masked" not in doc["catalog"]["m:has"] and "首位顾客 已留 号码" in doc["markdown"], doc["markdown"]


async def test_a_metric_whose_column_is_surely_derived_from_the_masked_one_is_still_withheld(monkeypatch, pos):
    """对照：SQL 里认得出取自号码列的结果列（套了函数、改了名字）照旧写「已遮罩」，同一张卡上的金额照给；引用它的正式运行
    照旧判失败。"""
    seen = script(monkeypatch, "首位顾客号码 [[m:p0]]，消费 [[m:spend0]]。")
    row = await finish(flow("SELECT id, UPPER(phone) AS p, amount FROM customers ORDER BY id", [
        {"id": "p0", "name": "首位顾客号码", "format": "plain", "expression": "cell(nodes.q, 0, 'p')"},
        {"id": "spend0", "name": "首位顾客消费", "unit": "元", "decimals": 2, "format": "thousands",
         "expression": "cell(nodes.q, 0, 'amount')"}]))
    lines = metric_lines(seen[0])
    assert "首位顾客号码 = 已遮罩（取自设置了遮罩的列，不要引用）" in lines and "首位顾客消费 = 1,234.50元" in lines, lines
    assert leaks(lines) == []
    assert row.status == "failed" and "m:p0" in (row.error or "") and leaks(row.error) == [], row.error


# --------------------------------------------------------------------------
# 可验证报告：老文档只给哈希的判断
# --------------------------------------------------------------------------


async def test_an_old_doc_carrying_an_ordinary_amount_from_an_untraceable_query_is_embedded(client, monkeypatch, pos):
    """升级前写的文档（写作前不认遮罩，这里把那一步换成什么都不认）：证据目录里写着没碰号码列的销售额。以前可验证报告按
    宁可多遮的那一套认，整份文档只给哈希、页面说「有 1 份报告未能核对」；现在和写作目录同一个判断，文档照常附带、核对得上。
    口径卡照旧只给 id（卡上记着输入的值，SQL 认不准的快照按整份遮，同基线），引用它的数字无法核对。"""
    import app.api.evidence as evidence_api

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(evidence_api, "withheld_metrics", nothing)
    script(monkeypatch, "本期销售额 [[m:gmv]]。")
    row = await finish(flow(PLAIN[1][0], [GMV]))
    assert row.status == "succeeded", row.error
    resp = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert resp.status_code == 200, resp.text
    from tests.test_bundle_masked_metrics import data_of

    bundle = data_of(resp.text)
    [doc] = [a for a in bundle["artifacts"] if a["kind"] == "report_doc"]
    assert "b64" in doc and "masked" not in doc, doc
    assert json.loads(base64.b64decode(doc["b64"]))["catalog"]["m:gmv"]["value"] == 7036.5
    got = verdicts(bundle)
    assert got["docs"] == [{"node": "write", "trusted": True}], got["docs"]
    assert leaks(resp.text) == []


@pytest.mark.parametrize("sql, expected", [
    *[(sql, None) for sql, _ in PLAIN],
    ("SELECT id, UPPER(phone) AS gmv FROM customers ORDER BY id", "masked"),
    ("SELECT id, phone AS gmv FROM customers ORDER BY id", "masked"),
    ("SELECT id, CASE WHEN phone IS NULL THEN '未留' ELSE '已留' END AS gmv FROM customers ORDER BY id", None),
])
async def test_the_bundle_judges_old_docs_like_the_report_writer(monkeypatch, pos, sql, expected):
    """可验证报告认老文档里的指标给不给（_Store.metric_word 的 sure），和报告撰写节点的写作目录（_metric_masks 的
    sure、by_value=False）一样；证据面板照旧宁可多遮。"""
    from app.api.evidence import MASKED, _mask_fingerprint, _Masks, _metric_in, _metric_masks, _sealed, certain_masks
    from app.bundle.build import _Store

    script(monkeypatch, "收银完成。")
    row = await finish(flow(sql, [{**GMV, "format": "plain", "unit": "", "decimals": None}], on_violation="flag"))
    assert row.status == "succeeded", row.error
    sealed = await _sealed(row.id)
    entry = next(e for e in sealed.entries if e.get("kind") == "metric_set")
    card = json.loads(read_bytes(entry["artifact"], verify=False))
    sure, secrets = await certain_masks(sealed, _Masks(sealed))
    store = _Store(lambda a: read_bytes(a, verify=False), await _mask_fingerprint(), sealed.events,
                   sure_columns=sure, sure_secrets=secrets)
    metric = _metric_in(card, "gmv")
    _, word = await _metric_masks(metric, "card", entry["artifact"], sealed, _Masks(sealed), by_value=False, sure=True)
    writer = {MASKED: "masked"}.get(word or "", word)
    bundle = store.metric_word(entry["artifact"], metric, sealed.events, sure=True)
    assert writer == bundle == expected, (writer, bundle)
