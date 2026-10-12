"""证据面板认遮罩列和可验证报告是同一个函数；查询步骤里写着的遮罩值、交给裁判的摘录同样不给。

复核发现：
- 证据面板只按数据源的一种方言调 masking._derived_hidden，可验证报告另有几道（语法树认不准的写法、不点列名也读得出表里
  内容的写法、几种读法合起来认、列名表和整行引用）：MySQL 会执行的注释、U&"…"、query_to_xml、pg_stats 这几种写法，面板
  照给、可验证报告整份遮。现在面板调可验证报告的那个函数（bundle/build.py 的 _derived_columns），再按数据源的方言认一遍
- 查询步骤原样给出 SQL 里写着的遮罩值（按号码筛的 WHERE c.phone IN (…)），合并 SQL、SQL 检查的片段也一样；可验证报告
  按「这次运行查到过的遮罩值」认出来、整份只给哈希。现在展示时换成「已遮罩」，底下写明只是展示时换的
- 按需裁判交给裁判的摘录只按列名去掉 phone，c.phone AS tel 那一列的号码照样交给裁判。现在按证据面板同一个函数认出的列
  也并进去

真跑的部分用虚构的 SQLite 商城库（号码是编的），报告由 MockChatModel 照剧本写。
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

CUSTOMERS = [(1, "客户甲", "19900000601"), (2, "客户乙", "19900000602"), (3, "客户丙", "19900000603")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25)]
PHONES = [c[2] for c in CUSTOMERS]


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的（摘录里数值格按数渲染：19,900,000,601）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


# --------------------------------------------------------------------------
# 和可验证报告同一个函数
# --------------------------------------------------------------------------

#: (写法, SQL, 结果列, 数据源的方言)：可验证报告认不准、整份遮的写法，面板以前只遮 phone 或者一列都不遮
LIKE_BUNDLE = [
    ("MySQL 会执行的注释", "SELECT id, /*!50000 phone AS */ nick FROM customers", ["id", "nick"], "mysql"),
    ("Postgres 的 Unicode 转义", 'SELECT id, U&"\\0070hone" AS c FROM customers', ["id", "c"], "postgres"),
    ("SQL 写在字符串里交给函数", "SELECT query_to_xml('select phone from customers', true, false, '') AS x", ["x"],
     "postgres"),
    ("统计信息里的采样值", "SELECT most_common_vals AS v FROM pg_stats WHERE attname = 'phone'", ["v"], "postgres"),
    ("SQLite 的统计表", "SELECT tbl, idx, stat FROM sqlite_stat1", ["tbl", "idx", "stat"], "sqlite"),
    ("列名表按位置改名", "SELECT p FROM customers AS c(i, n, p)", ["p"], "postgres"),
    ("别名", "SELECT name, phone AS tel FROM customers", ["name", "tel"], "sqlite"),
    ("没碰遮罩列", "SELECT name FROM customers", ["name"], "sqlite"),
]


@pytest.mark.parametrize("label, sql, columns, kind", LIKE_BUNDLE, ids=[c[0] for c in LIKE_BUNDLE])
def test_the_panel_never_shows_what_the_bundle_hides(label, sql, columns, kind):
    """同一份快照：可验证报告算作取自遮罩列的列，证据面板一定也遮（面板可以按方言多遮，不能少遮）。"""
    from app.api.evidence import _derived
    from app.bundle.build import _derived_columns

    snap = {"sql": sql, "columns": columns, "rows": [], "source": "mall"}
    bundle = {c for c in columns if c.lower() in _derived_columns(snap, {"phone"})}
    panel = {c for c in columns if c.lower() in _derived(snap, {"phone"}, kind)}
    assert bundle <= panel, (label, bundle, panel)
    if label not in ("没碰遮罩列",):
        assert panel, label
    else:
        assert panel == set(), panel


# --------------------------------------------------------------------------
# 查询步骤里写着的遮罩值
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None):
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
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


#: 先查出号码（起了别名），再按号码筛：SQL 的字面量就是号码
SQL_BY_PHONE = ("SELECT ROUND(SUM(o.amount), 2) AS spend FROM orders o JOIN customers c ON c.id = o.customer_id "
                f"WHERE c.phone IN ('{PHONES[0]}', '{PHONES[1]}')")


async def run_by_phone(monkeypatch) -> Run:
    from app.providers import mock_model

    text = "两位重点客户共消费 [[v:Q2.r0.spend]]。"
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
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
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def test_masked_values_written_in_the_sql_are_not_shown(client, monkeypatch, mall):
    """按号码筛的 SQL：查询步骤里号码换成「已遮罩」，底下写明只是展示时换的、执行的原文没动；快照本身（哈希核对用的）
    原样。"""
    row = await run_by_phone(monkeypatch)
    [report] = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "v:Q2.r0.spend")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    [step] = [s for s in body["chain"] if s["step"] == "query"]
    assert leaks(body) == [] and "IN ('已遮罩', '已遮罩')" in step["sql"], step["sql"]
    assert "仅在展示时" in step["mask_note"] and step["hash_ok"] is True
    assert PHONES[0] in artifact_store.load(step["artifact"])["sql"], "快照原样，换的只是展示的这一份"


async def test_without_masks_the_sql_is_shown_as_it_ran(client, monkeypatch, mall):
    await set_source(mall, {})
    row = await run_by_phone(monkeypatch)
    [report] = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"]
    doc = artifact_store.load(report["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "v:Q2.r0.spend")
    [step] = [s for s in (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()["chain"]
              if s["step"] == "query"]
    assert step["sql"] == SQL_BY_PHONE and "mask_note" not in step


def test_sql_check_excerpts_and_merge_parts_are_shown_with_values_masked():
    """SQL 检查的片段、合并 SQL、合并警告、输入的检查结果同一个办法；没有遮罩值的原样，不标「展示时换过」。"""
    from app.api.evidence import _Shown
    from app.engine.masking import _Secrets

    secrets = _Secrets()
    secrets.add({"columns": ["tel"], "rows": [[p] for p in PHONES]}, {"tel"})
    shown = _Shown(secrets)
    [check] = shown.checks([{"code": "x", "message": f"按 {PHONES[0]} 筛选", "sql_excerpt": f"c.phone = '{PHONES[0]}'"}])
    view = shown.merge({"sql": f"SELECT * FROM a WHERE a.tel = '{PHONES[1]}'", "inputs": [
        {"alias": "a", "checks": [{"code": "y", "message": f"{PHONES[2]} 不存在"}]}],
        "warnings": [{"code": "rows_grew", "message": f"例如 {PHONES[2]} 出现 2 次"}], "traced": []})
    assert leaks([check, view]) == [] and shown.changed
    plain = _Shown(secrets)
    assert plain.text("SELECT name FROM customers") == "SELECT name FROM customers" and not plain.changed
    assert _Shown(None).text(f"WHERE phone = '{PHONES[0]}'") == f"WHERE phone = '{PHONES[0]}'"


# --------------------------------------------------------------------------
# 交给裁判的摘录
# --------------------------------------------------------------------------


async def test_the_judge_excerpt_drops_a_column_renamed_from_a_masked_one(engine_up, mall):
    """c.phone AS tel：交给裁判的遮罩列表把 tel 并进数据源名下，摘录里不给 tel 那一列，号码一个都不交出去。"""
    from app.api.evidence import _judge_masks
    from app.engine import judge as judging

    art = await artifact_store.put_json({"columns": ["customer", "tel", "spend"],
                                         "rows": [["客户乙", PHONES[1], 380.0], ["客户甲", PHONES[0], 220.0]],
                                         "source": "mall", "mask_columns": ["phone"],
                                         "sql": "SELECT c.name AS customer, c.phone AS tel, 1 AS spend FROM customers c"},
                                        kind="query")
    entry = {"alias": "Q1", "kind": "query", "artifact": art, "columns": ["customer", "tel", "spend"], "rows": 2,
             "node_id": "top", "source": "mall", "tool": "db_query__mall"}
    masked = await _judge_masks({"Q1": entry}, {"mall": ["phone"]})
    assert {c.lower() for c in masked["mall"]} >= {"phone", "tel"}, masked
    excerpt = judging._query_excerpt("Q1", entry, {0}, ["tel", "spend"], True, judging._Fetch(None), masked)
    assert excerpt and leaks(excerpt) == [] and "380" in excerpt, excerpt
    # 只按列名（以前的办法）：号码照样在摘录里
    before = judging._query_excerpt("Q1", entry, {0}, ["tel", "spend"], True, judging._Fetch(None), {"mall": ["phone"]})
    assert leaks(before), "对照：只按列名遮时号码在摘录里"
