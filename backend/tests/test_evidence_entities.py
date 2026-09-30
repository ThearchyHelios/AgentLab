"""实体层：报告里写的表名、字段名能点开出处，写了不存在的名字会被标出来。

以前 `refunds` 这种写在反引号里的名字，报告不管它存不存在：模型编一个听起来很像的表名，
读者无从分辨。三期：

- 数据源工具每次查询都把当时的表结构冻结成 schema_snapshot 工件（内容寻址），台账记一条
  schema 条目；查询条目记下 SQL 里 FROM / JOIN 的表（注释和字符串里的不算）
- 目录编出表条目 t:<表> 和列条目 c:<表>.<列>，来源是表结构快照、SQL 用到的表、查询结果的列
- [[t:]] [[c:]] 标记能解析，显示名字本身；正文里的名字按保守规则自动链接
- 反引号里写了像标识符的名字、却哪里都找不到的，标成可疑实体（state none，issue
  unknown_entity），记违规但不拦——报告节点不为它失败、不为它重写
- 流式渲染和整篇渲染一致
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.evidence import (
    ENTITIES_OFF_REASON,
    LATER_REASON,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    iter_segments,
    make_eid,
    query_entry_fields,
    render_markers,
    schema_entry_fields,
    sql_tables,
    uncited_claims,
    verify_doc,
)
from app.engine.runner import run_manager

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"

SCHEMA_ART = "5" * 64
Q_ART = "a" * 64
TOOL_SNAP = "b" * 64
QUERY = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": Q_ART, "via": TOOL_SNAP, "tool": TOOL,
         "source": SOURCE, "columns": ["region", "gmv", "order_cnt"], "rows": 2, "truncated": False,
         "schema_artifact": SCHEMA_ART, "tables": ["orders"]}
SCHEMA = {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": SCHEMA_ART, "source": SOURCE,
          "tables": {"orders": ["id", "region", "amount", "created_at", "name"],
                     "refunds": ["id", "order_id", "amount"]}}


@pytest.fixture
def catalog():
    return build_catalog(nodes={}, ledger=[QUERY, SCHEMA], inputs={"week": "2026-W37"})


def entity_segments(doc):
    return [s for s in iter_segments(doc) if s["kind"] == "entity"]


def codes(doc):
    return [v["code"] for v in doc["violations"]]


# --------------------------------------------------------------------------
# 从 SQL 里抽表名：注释、字符串里的不算
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, tables", [
    ("-- FROM ghost_a\nSELECT o.region, 'JOIN ghost_b' AS note /* FROM ghost_c */ FROM orders o "
     "LEFT JOIN \"refunds\" r ON r.order_id = o.id WHERE o.name <> 'FROM ghost_d'", ["orders", "refunds"]),
    ("WITH recent AS (SELECT * FROM orders), top AS (SELECT * FROM recent) "
     "SELECT * FROM top JOIN ANALYTICS.v_demo_table v ON 1 = 1", ["orders", "ANALYTICS.v_demo_table"]),
    ("SELECT * FROM orders a, refunds AS b, `sales_daily` WHERE a.id = b.order_id", ["orders", "refunds", "sales_daily"]),
    ("SELECT * FROM (SELECT id FROM orders) t JOIN refunds USING (id)", ["orders", "refunds"]),
    ("SELECT * FROM generate_series(1, 3)", []),
    ("select * from Orders join orders on 1=1", ["Orders"]),
    ("SELECT 'it''s FROM ghost' AS x FROM orders", ["orders"]),
    # 字符串里的 -- 和 /* 不是注释：先认字符串、再认注释，一趟扫完
    ("SELECT '--x' AS a FROM orders", ["orders"]),
    ("SELECT '/*' AS a FROM orders WHERE note <> '*/ JOIN ghost_e'", ["orders"]),
    ("SELECT \"a--b\" FROM orders JOIN refunds ON 1 = 1", ["orders", "refunds"]),
    # 引号括起来的名字里的 FROM 不是关键字（MySQL 默认把 "…" 当字符串）
    ("SELECT \"FROM ghost_f\" AS x FROM orders", ["orders"]),
    # 反斜杠转义的引号（MySQL、PostgreSQL 的 E'…'）：宁可漏认一张表，不能把字符串里的名字当表
    ("SELECT 'it\\'s FROM ghost_g' AS x FROM orders", ["orders"]),
    ("SELECT $$ FROM ghost_h $$ AS a, $tag$ JOIN ghost_i $tag$ FROM orders WHERE id = $1", ["orders"]),
    ("/* a /* nested */ FROM ghost_j */ SELECT * FROM orders", ["orders"]),
    ("# FROM ghost_k\nSELECT * FROM orders", ["orders"]),
    # SQLite 也认的方括号标识符；数组下标、数组字面量里的字符串不是
    ("SELECT * FROM [main].[orders] o JOIN [refunds] r ON r.order_id = o.id", ["main.orders", "refunds"]),
    ("SELECT ARRAY['FROM ghost_l'] AS a, tags[1] FROM orders", ["orders"]),
    ("", []),
])
def test_sql_tables_ignore_comments_strings_and_cte_names(sql, tables):
    assert sql_tables(sql) == tables


def test_query_entry_carries_tables_only_with_a_schema_snapshot():
    """没有表结构快照（数据源没探查过结构）的查询保持二期的形状：只凭 SQL 和结果列核对名字，
    反引号里写的真实表名（没进这条 SQL）会被误标成编造的，这种运行不启用实体核对。"""
    payload = {"artifact": Q_ART, "columns": ["gmv"], "rows": [[1]], "sql": SQL, "source": SOURCE}
    plain = query_entry_fields(payload)
    assert "tables" not in plain and "schema_artifact" not in plain
    fields = query_entry_fields(json.dumps({**payload, "schema_artifact": SCHEMA_ART}))
    assert fields["schema_artifact"] == SCHEMA_ART and fields["tables"] == ["orders"]


# --------------------------------------------------------------------------
# 目录
# --------------------------------------------------------------------------


def test_catalog_lists_tables_and_columns_from_all_three_sources(catalog):
    orders = catalog["t:orders"]
    assert orders["kind"] == "table" and orders["locator"] == {"table": "orders"}
    assert orders["eid"] == make_eid("table", SCHEMA_ART, {"table": "orders"}) and orders["artifact"] == SCHEMA_ART
    assert {(s["kind"], s.get("alias")) for s in orders["sources"]} == {("schema", None), ("sql", "Q1")}
    assert orders["queries"] == ["Q1"]
    assert catalog["t:refunds"]["queries"] == []

    amount = catalog["c:orders.amount"]
    assert amount["kind"] == "column" and amount["locator"] == {"table": "orders", "column": "amount"}
    assert amount["eid"] == make_eid("column", SCHEMA_ART, {"table": "orders", "column": "amount"})
    # 结果列落在 SQL 用到的表里：同一个条目多一个来源
    region = catalog["c:orders.region"]
    assert {(s["kind"], s.get("alias")) for s in region["sources"]} == {("schema", None), ("result", "Q1")}
    # 没有表名的结果列（聚合的别名）：c:<列>，出处是那份查询快照
    cnt = catalog["c:order_cnt"]
    assert cnt["locator"] == {"column": "order_cnt"} and cnt["eid"] == make_eid("column", Q_ART, {"column": "order_cnt"})
    assert cnt["sources"] == [{"kind": "result", "artifact": Q_ART, "alias": "Q1", "source": SOURCE}]
    # 同名字段在好几张表里：c:<列> 列出是哪几张
    assert catalog["c:amount"]["tables"] == ["orders", "refunds"]


def test_phase_two_ledgers_build_no_entities():
    old = {k: v for k, v in QUERY.items() if k not in ("schema_artifact", "tables")}
    catalog = build_catalog(nodes={}, ledger=[old])
    assert not [a for a, e in catalog.items() if e["kind"] in ("table", "column")]
    doc = compose_doc("看 `ghost_table`。", catalog)
    assert doc["violations"] == [] and not entity_segments(doc)


# --------------------------------------------------------------------------
# 标记
# --------------------------------------------------------------------------


def test_entity_markers_render_as_the_name(catalog):
    doc = compose_doc("数据来自 [[t:orders]] 的 [[c:orders.amount]] 和 [[c:order_cnt]]。[[see:t:refunds]]", catalog)
    assert doc["markdown"] == "数据来自 orders 的 orders.amount 和 order_cnt。"
    segs = entity_segments(doc)
    assert [(s["text"], s["cite"]["kind"], s["state"]) for s in segs] == [
        ("orders", "table", "deterministic"), ("orders.amount", "column", "deterministic"),
        ("order_cnt", "column", "deterministic")]
    assert segs[0]["cite"]["eid"] == catalog["t:orders"]["eid"]
    assert doc["violations"] == [] and doc["stats"]["entities"] == 3
    assert doc["blocks"][0]["units"][0]["cites"] == ["t:orders", "c:orders.amount", "c:order_cnt", "t:refunds"]


def test_unknown_marker_is_suspicious_not_unresolved(catalog):
    doc = compose_doc("退款看 [[t:refund_log]]。字段写错 [[t:amount]]。", catalog)
    fake, wrong = entity_segments(doc)
    assert fake["text"] == "refund_log" and fake["state"] == "none" and fake["issue"] == "unknown_entity"
    assert "疑似不存在的名称" in fake["cite"]["reason"]
    # 字段当表写：名字是真的，写法错了——这是解析不了，要重写
    assert wrong["issue"] == "unresolved_ref" and wrong["text"].startswith("⟦?t:")
    assert codes(doc) == ["unknown_entity", "unresolved_ref"]
    assert doc["stats"]["unknown_entities"] == 1 and doc["stats"]["unresolved"] == 1


def test_entity_markers_without_any_schema_keep_the_phase_one_answer():
    """这次运行没查过库（或者是升级前的运行）：没有东西可以核对，t / c 仍按一期判为解析不了。"""
    catalog = build_catalog(nodes={}, inputs={"week": "2026-W37"})
    doc = compose_doc("看 [[t:orders]]，还有 `ghost_table`。", catalog)
    [seg] = [s for s in iter_segments(doc) if s.get("ref")]
    assert seg["state"] == "none" and LATER_REASON in seg["cite"]["reason"]
    assert codes(doc) == ["unresolved_ref"]            # 反引号里的名字不核对


# --------------------------------------------------------------------------
# 反引号：一律核对
# --------------------------------------------------------------------------


def test_backticked_names_are_linked_or_flagged(catalog):
    doc = compose_doc("看 `orders` 和 `ghost_table`，`refunds.order_id` 与 `SUM(amount)`、`NULL`、`week`。", catalog)
    segs = entity_segments(doc)
    assert [(s["text"], s["state"], s.get("issue")) for s in segs] == [
        ("`orders`", "deterministic", None), ("`ghost_table`", "none", "unknown_entity"),
        ("`refunds.order_id`", "deterministic", None)]
    assert segs[0]["ref"] == "t:orders" and segs[0]["code"] is True and segs[0]["auto"] is True
    assert segs[2]["cite"]["eid"] == catalog["c:refunds.order_id"]["eid"]
    [v] = doc["violations"]
    assert v["code"] == "unknown_entity" and v["text"] == "ghost_table" and v["segment"] == segs[1]["id"]
    assert doc["markdown"][v["span"][0]:v["span"][1]] == "ghost_table"
    assert doc["stats"]["unknown_entities"] == 1
    assert verify_doc(doc, catalog)["violations"] == doc["violations"]


def test_verify_finds_suspicious_names_even_if_the_doc_hides_them(catalog):
    doc = compose_doc("看 `ghost_table` 的数。", catalog)
    for s in iter_segments(doc):
        if s.get("issue") == "unknown_entity":
            s.update(kind="text", state="neutral")
            s.pop("issue")
    checked = verify_doc(doc, catalog)
    assert [v["code"] for v in checked["violations"]] == ["unknown_entity"]
    assert checked["stats"]["unknown_entities"] == 1


def test_names_inside_code_blocks_are_not_checked(catalog):
    doc = compose_doc("```sql\nSELECT `ghost_col` FROM ghost_table\n```\n\n正文。", catalog)
    assert doc["violations"] == [] and not entity_segments(doc)


# --------------------------------------------------------------------------
# 正文里的裸名字：保守
# --------------------------------------------------------------------------


def test_bare_names_link_conservatively(catalog):
    doc = compose_doc("按 region 和 name 统计，orders 表的 order_id、created_at 与 refunds.amount；"
                      "amount、id、gmv 不链接，Orders 也不链接，order_cnt 链接。", catalog)
    assert [s["text"] for s in entity_segments(doc)] == [
        "orders", "order_id", "created_at", "refunds.amount", "order_cnt"]
    assert doc["violations"] == []
    assert all(s["state"] == "deterministic" and s.get("auto") and not s.get("code") for s in entity_segments(doc))


def test_bare_names_inside_links_and_bold_are_left_alone(catalog):
    doc = compose_doc("见 [orders 说明](http://example.com/orders) 和 **orders 汇总**。", catalog)
    assert not entity_segments(doc)
    assert "".join(s["text"] for s in iter_segments(doc)) == doc["markdown"]


# --------------------------------------------------------------------------
# 一致：流式 = 整篇；组装 = 复核
# --------------------------------------------------------------------------


def test_streaming_equals_full_render(catalog):
    text = ("## [[t:orders]]\n\n按 [[c:orders.region]] 看，[[c:order_cnt]] 最多；[[t:refund_log]] 没有。"
            "`orders` 与 [[t:amount]] 以及 [[i:week]]。[[see:t:orders]]\n\n- [[c:refunds.order_id]]")
    whole = render_markers(text, catalog)
    assert "[[" not in whole and "orders" in whole
    rng = random.Random(7)
    for _ in range(300):
        renderer = StreamRenderer(catalog)
        out, i = [], 0
        while i < len(text):
            step = rng.randint(1, 9)
            out.append(renderer.feed(text[i:i + step]))
            i += step
        out.append(renderer.flush())
        assert "".join(out) == whole


def test_compose_and_verify_agree_and_doc_catalog_stays_small(catalog):
    doc = compose_doc("[[t:orders]] 里 `refunds` 和 `ghost_table`，order_cnt。", catalog)
    checked = verify_doc(doc, catalog)
    assert checked["violations"] == doc["violations"] and checked["stats"] == doc["stats"]
    kept = {a for a, e in doc["catalog"].items() if e["kind"] in ("table", "column")}
    assert kept == {"t:orders", "t:refunds", "c:order_cnt"}          # 只留正文里用到的实体
    assert "i:week" in doc["catalog"] and "Q1" in doc["catalog"]


# --------------------------------------------------------------------------
# 表结构快照不全、实体层关掉、老目录：各说各的话
# --------------------------------------------------------------------------


def test_schema_entry_keeps_truncation():
    content = {"source": SOURCE, "tables": {"orders": {"columns": [{"name": "id"}]}}, "schema": None}
    assert schema_entry_fields(content) == {"source": SOURCE, "schema": None, "synced_at": None,
                                            "tables": {"orders": ["id"]}}
    assert schema_entry_fields({**content, "truncated": True, "total": 250}) == {
        "source": SOURCE, "schema": None, "synced_at": None, "tables": {"orders": ["id"]},
        "truncated": True, "total": 250}


def test_a_truncated_schema_cannot_call_a_name_made_up(catalog):
    """库里的表太多、快照只存了前 200 张：没列出的名字可能是真的，只能说「核对不了」（软问题，出口
    不记缺口），不能说「可能是编造的」。已列出的表的字段是全的，表.字段 写错了照样是可疑实体。"""
    cut = build_catalog(nodes={}, ledger=[QUERY, {**SCHEMA, "truncated": True, "total": 250}])
    text = "看 `ghost_table`、[[t:ghost_two]]、`orders.ghost_col` 和 `orders`。[[see:c:ghost_three]]"
    doc = compose_doc(text, cut)
    segs = entity_segments(doc)
    assert [(s["text"], s["state"], s.get("issue")) for s in segs] == [
        ("`ghost_table`", "none", "unverified_entity"), ("ghost_two", "none", "unverified_entity"),
        ("`orders.ghost_col`", "none", "unknown_entity"), ("`orders`", "deterministic", None)]
    assert segs[1]["cite"]["unverified"] is True and "unknown" not in segs[1]["cite"]
    # 违规按位置排：句末依据 [[see:]] 的位置记的是整句，排在最前
    assert codes(doc) == ["unverified_entity", "unverified_entity", "unverified_entity", "unknown_entity"]
    assert "无法核实" in doc["violations"][0]["message"] and "疑似不存在" not in doc["violations"][0]["message"]
    assert doc["stats"]["unverified_entities"] == 3 and doc["stats"]["unknown_entities"] == 1
    checked = verify_doc(doc, cut)
    assert checked["violations"] == doc["violations"] and checked["stats"] == doc["stats"]
    assert "只存了一部分" in catalog_prompt(cut) and "只存了一部分" not in catalog_prompt(catalog)
    # 同一篇放到完整的快照下：全都是可疑实体
    assert codes(compose_doc(text, catalog)) == ["unknown_entity"] * 4


def test_old_catalogs_keep_the_old_stats_keys(catalog):
    """升级前的运行（目录里没有表、字段、检索）：统计的键和以前一模一样，事件一个字不加。"""
    old = build_catalog(nodes={}, inputs={"week": "2026-W37"})
    doc = compose_doc("本周 [[i:week]]，看 [[t:orders]]。", old)
    assert list(doc["stats"]) == ["units", "segments", "claims", "connective", "headings", "numbers",
                                  "numbers_cited", "values", "uncited_numbers", "unresolved", "see",
                                  "uncited_claims", "violations"]
    assert verify_doc(doc, old)["stats"] == doc["stats"]
    for cat in (catalog, build_catalog(nodes={}, ledger=[{"kind": "retrieval", "node_id": "kb", "exec": 1,
                                                           "artifact": "c" * 64, "source": "kb", "rows": 1}])):
        stats = compose_doc("本周。", cat)["stats"]
        assert {"entities", "quotes", "unknown_entities", "unverified_entities"} <= set(stats)


def test_entities_off_says_the_node_turned_it_off(catalog):
    """entities: off 是作者有意关掉的：[[t:]] 解析不了的原因照实说，不能说「后续版本支持」。
    目录里就算还有表和字段，关掉了也不链接、不核对反引号。"""
    text = "看 [[t:orders]]、`ghost_table` 和 order_cnt。"
    doc = compose_doc(text, catalog, entities=False)
    [seg] = [s for s in iter_segments(doc) if s.get("ref")]
    assert seg["issue"] == "unresolved_ref" and seg["cite"]["reason"] == ENTITIES_OFF_REASON
    assert not entity_segments(doc) or all(s.get("ref") for s in entity_segments(doc))
    assert codes(doc) == ["unresolved_ref"] and ENTITIES_OFF_REASON in doc["violations"][0]["message"]
    assert verify_doc(doc, catalog, entities=False)["violations"] == doc["violations"]
    stripped = {a: e for a, e in catalog.items() if e["kind"] not in ("table", "column")}
    assert compose_doc(text, stripped, entities=False)["violations"] == doc["violations"]


# --------------------------------------------------------------------------
# 自动链接只是标注，不是写作者声明的依据
# --------------------------------------------------------------------------


def units(doc):
    return [u for block in doc["blocks"] for u in block["units"]]


@pytest.mark.parametrize("text", ["增长主要来自新客，见 order_id 字段。", "增长主要来自 `orders` 表的新客。",
                                  "增长主要来自新客，见 orders.region。"])
def test_an_auto_linked_name_is_not_a_citation(catalog, text):
    """结论句的依据只认写作者自己写的 [[…]] 和 [[see:…]]：提一个字段名就算挂了依据的话，
    claims: require_citation（受管级别必须写）形同虚设。"""
    doc = compose_doc(text, catalog)
    [unit] = units(doc)
    assert entity_segments(doc) and all(s.get("auto") for s in entity_segments(doc))
    assert unit["kind"] == "claim" and unit["cites"] == []
    checked = verify_doc(doc, catalog)
    assert [u["unit"] for u in checked["uncited"]] == [unit["id"]]
    assert checked["uncited"] == uncited_claims(doc) and checked["stats"]["uncited_claims"] == 1
    # 链接照样在：点得开，文档目录里也留着它的条目（证据接口要用）
    for seg in entity_segments(doc):
        assert seg["state"] == "deterministic" and seg["cite"]["alias"] in doc["catalog"]


def test_a_marker_the_writer_wrote_is_a_citation(catalog):
    doc = compose_doc("增长主要来自新客，见 [[c:refunds.order_id]]。退款来自 `refunds`。[[see:t:refunds]]", catalog)
    first, second = units(doc)
    assert first["cites"] == ["c:refunds.order_id"] and second["cites"] == ["t:refunds"]
    assert verify_doc(doc, catalog)["uncited"] == [] == uncited_claims(doc)


def test_an_auto_linked_name_alone_does_not_make_a_claim(catalog):
    """自动链接只重新切片：句子算不算结论，和它提没提表名无关——没开实体层时是连接性的话，
    开了也还是。"""
    text = "下面按 order_id 和 `orders` 展开，`ghost_table` 另说。"
    doc = compose_doc(text, catalog)
    [unit] = units(doc)
    assert unit["kind"] == "connective" and entity_segments(doc)
    plain = compose_doc(text, build_catalog(nodes={}, inputs={"week": "2026-W37"}))
    assert [u["kind"] for u in units(plain)] == ["connective"]
    # 名字里带方向词的（growth_rate 是查询结果的列，会被自动链接），开不开实体层都一样判
    rated = build_catalog(nodes={}, ledger=[{**QUERY, "columns": ["growth_rate"]}, SCHEMA])
    doc = compose_doc("见 growth_rate。", rated)
    assert [s["text"] for s in entity_segments(doc)] == ["growth_rate"]
    for cat in (rated, build_catalog(nodes={})):
        assert [u["kind"] for u in units(compose_doc("见 growth_rate。", cat))] == ["claim"]


def test_verify_recomputes_uncited_instead_of_trusting_the_doc(catalog):
    doc = compose_doc("增长主要来自新客，见 order_id 字段。收入上升。[[see:t:orders]]", catalog)
    first, second = units(doc)
    checked = verify_doc(doc, catalog)
    assert checked["uncited"] == uncited_claims(doc) == [
        {"unit": first["id"], "span": first["span"], "text": "增长主要来自新客，见 order_id 字段。"}]
    first["cites"] = ["c:refunds.order_id"]              # 文档自称挂了依据
    assert uncited_claims(doc) == []                     # 界面用的这份只照文档说
    assert [u["unit"] for u in verify_doc(doc, catalog)["uncited"]] == [first["id"]]   # 复核不信
    second["see"] = []                                   # 依据被拿掉了：复核也看得出来
    assert [u["unit"] for u in verify_doc(doc, catalog)["uncited"]] == [first["id"], second["id"]]


# --------------------------------------------------------------------------
# 跑一次：表结构快照被冻结，台账记下它
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def warehouse(tmp_path):
    """探查过结构的示例库：orders、refunds 两张表。"""
    from app.data.introspect import introspect

    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL, created_at TEXT);"
        "CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                   [(1, "east", 100.5, "2026-09-01"), (2, "west", 200.0, "2026-09-02"), (3, "east", 300.0, "2026-09-03")])
    db.commit()
    db.close()
    probe = SimpleNamespace(id=f"probe-{path.name}", name=SOURCE, kind="sqlite", database=str(path), host=None,
                            port=None, username=None, password=None, options={}, readonly=True, schema_cache={})
    cache = await introspect(probe)
    await engines.invalidate(probe.id)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, cache
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield source_id
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def tool_graph(*after, sql=SQL):
    return chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": sql}), *after,
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.fetch }}"}]))


async def finish(graph, statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def ledger(run_id: str) -> list[dict]:
    tup = await run_manager.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
    return list(tup.checkpoint["channel_values"].get("evidence") or [])


async def test_schema_snapshot_is_frozen_and_recorded(engine_up, warehouse):
    row = await finish(tool_graph())
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    schema_art = end.data["schema_artifact"]
    payload = json.loads(row.output["r"])
    assert payload["schema_artifact"] == schema_art
    snap = artifact_store.load(schema_art)
    assert snap["source"] == SOURCE and set(snap["tables"]) == {"orders", "refunds"}
    assert artifact_store.load(payload["artifact"])["schema_artifact"] == schema_art

    entries = await ledger(row.id)
    assert [e["kind"] for e in entries] == ["tool", "query", "schema"]
    query, schema = entries[1], entries[2]
    assert query["schema_artifact"] == schema_art and query["tables"] == ["orders"]
    assert schema == {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": schema_art, "source": SOURCE,
                      "schema": snap.get("schema"), "synced_at": snap.get("synced_at"),
                      "tables": {"orders": ["id", "region", "amount", "created_at"],
                                 "refunds": ["id", "order_id", "amount"]}}
    [finished] = await events(row.id, "node.finished", "fetch")
    assert finished.data["evidence"] == entries

    # 事后改了数据源的结构缓存：已有的快照、台账、目录都不变
    async with SessionLocal() as session:
        src = await session.get(DataSource, warehouse)
        cache = json.loads(json.dumps(src.schema_cache))
        cache["tables"]["ghost_new"] = {"qualified": "ghost_new", "columns": [{"name": "x", "type": "INT"}]}
        del cache["tables"]["refunds"]
        src.schema_cache = cache
        await session.commit()
    assert artifact_store.load(schema_art) == snap
    catalog = build_catalog(nodes={}, ledger=await ledger(row.id))
    assert "t:refunds" in catalog and "t:ghost_new" not in catalog

    again = await finish(tool_graph())
    [end2] = await events(again.id, "tool.end", "fetch")
    assert end2.data["schema_artifact"] != schema_art
    assert "ghost_new" in artifact_store.load(end2.data["schema_artifact"])["tables"]


async def test_agent_records_one_schema_entry_per_snapshot(engine_up, warehouse, monkeypatch):
    from app.providers import mock_model

    def decide(self, messages):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < 2:
            sql = SQL if done == 0 else "SELECT COUNT(*) AS n FROM refunds -- FROM ghost_table"
            return AIMessage(content="查。", tool_calls=[{"name": TOOL, "args": {"sql": sql}, "id": f"call_{done}"}])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await finish(chain(node("start", "input"), node("bot", "agent", prompt="查", tools=[TOOL], approval="never"),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.bot.text }}"}])))
    assert row.status == "succeeded", row.error
    ends = await events(row.id, "tool.end", "bot")
    assert len(ends) == 2 and ends[0].data["schema_artifact"] == ends[1].data["schema_artifact"]
    entries = await ledger(row.id)
    assert [e["kind"] for e in entries] == ["tool", "query", "schema", "tool", "query"]
    assert [e["tables"] for e in entries if e["kind"] == "query"] == [["orders"], ["refunds"]]
    catalog = build_catalog(nodes={}, ledger=entries)
    assert catalog["t:refunds"]["queries"] == ["Q2"] and "t:ghost_table" not in catalog


async def test_runs_from_before_the_upgrade_record_no_schema(engine_up, warehouse, monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    row = await finish(tool_graph())
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    assert "schema_artifact" not in end.data and "query_artifact" not in end.data
    [finished] = await events(row.id, "node.finished", "fetch")
    assert "evidence" not in finished.data


async def test_report_flags_a_made_up_name_without_failing(engine_up, warehouse, monkeypatch):
    """可疑实体只标注：on_violation=fail 的报告节点不为它失败，也不为它重写。"""
    from app.providers import mock_model

    seen: list = []

    def decide(self, messages):
        seen.append(messages)
        return AIMessage(content="`orders` 里东区最多，达 [[v:Q1.r0.gmv]]；`refund_log` 另算。[[see:Q1]]")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await finish(tool_graph(node("write", "report", instructions="写一句", on_violation="fail")))
    assert row.status == "succeeded", row.error
    assert len(seen) == 1, "可疑实体不该触发重写"
    [checked] = await events(row.id, "report.checked", "write")
    assert [v["code"] for v in checked.data["violations"]] == ["unknown_entity"]
    assert checked.data["stats"]["unknown_entities"] == 1 and checked.data["stats"]["entities"] == 1
    prompt = "\n".join(str(m.content) for m in seen[0])
    assert "[[t:" in prompt and "可能是编造的名字" in prompt and "orders" in prompt
    doc = artifact_store.load(checked.data["doc_artifact"])
    [linked] = [s for s in iter_segments(doc) if s.get("ref") == "t:orders"]
    assert linked["code"] is True and linked["cite"]["eid"] == doc["catalog"]["t:orders"]["eid"]
    schema_art = (await events(row.id, "tool.end", "fetch"))[0].data["schema_artifact"]
    assert doc["catalog"]["t:orders"]["artifact"] == schema_art


async def test_report_with_entities_off_leaves_names_alone(engine_up, warehouse, monkeypatch):
    """entities: off：目录里没有表和字段，写作目录不教 [[t:]]，反引号不核对；写了 [[t:]] 的，
    解析不了的原因照实说是这个节点关掉的。"""
    from app.providers import mock_model

    seen: list = []

    def decide(self, messages):
        seen.append(messages)
        return AIMessage(content="`orders` 里东区最多，达 [[v:Q1.r0.gmv]]；`refund_log` 另算，见 [[t:orders]]。[[see:Q1]]")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await finish(tool_graph(node("write", "report", instructions="写一句", entities="off", max_repairs=0)))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "write")
    assert [v["code"] for v in checked.data["violations"]] == ["unresolved_ref"]
    assert ENTITIES_OFF_REASON in checked.data["violations"][0]["message"]
    assert "unknown_entities" not in checked.data["stats"] and "entities" not in checked.data["stats"]
    doc = artifact_store.load(checked.data["doc_artifact"])
    assert not [a for a, e in doc["catalog"].items() if e["kind"] in ("table", "column")]
    assert not [s for s in iter_segments(doc) if s["kind"] == "entity" and not s.get("ref")]
    assert "[[t:" not in "\n".join(str(m.content) for m in seen[0])


async def test_report_with_a_bad_entities_value_fails(engine_up, warehouse, monkeypatch):
    from app.providers import mock_model

    seen: list = []
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: seen.append(1) or AIMessage("x"))
    row = await finish(tool_graph(node("write", "report", instructions="写一句", entities="bogus")))
    assert row.status == "failed"
    assert "「表名、字段名」只能是「核对」或「不核对」，当前为「bogus」" in (row.error or ""), row.error
    assert seen == [], "写错的配置不该先花一次模型调用"


def test_soft_violations_never_ask_for_a_rewrite():
    from app.engine.nodes.report import SOFT, _blocking

    assert SOFT == {"unknown_entity", "unverified_entity"}
    soft = [{"code": "unknown_entity"}, {"code": "unverified_entity"}]
    assert _blocking(soft, "strict") == []
    assert _blocking([*soft, {"code": "unresolved_ref"}], "strict") == [{"code": "unresolved_ref"}]


# --------------------------------------------------------------------------
# 给 B 段的导出：evidence_from 连带表结构、按名字找条目
# --------------------------------------------------------------------------


def test_evidence_from_also_filters_the_schema_entries():
    """evidence_from 只收某几个节点的证据：别的节点查询当时冻结的表结构也不收，否则报告能引用
    一张它的证据范围里根本没查过的表。"""
    from app.engine.nodes.report import report_catalog, report_entities
    from app.engine.schema import GraphSpec

    other = {**QUERY, "node_id": "other", "artifact": "c" * 64, "columns": ["n"],
             "schema_artifact": "6" * 64, "tables": ["refunds"]}
    other_schema = {**SCHEMA, "node_id": "other", "artifact": "6" * 64, "tables": {"refunds": ["id", "order_id"]}}
    state = {"nodes": {}, "input": {}, "evidence": [QUERY, SCHEMA, other, other_schema]}

    def catalog_for(**config):
        spec = GraphSpec.model_validate(chain(node("start", "input"), node("fetch", "tool", tool=TOOL),
                                              node("other", "tool", tool=TOOL), node("write", "report", **config)))
        return report_catalog(state, spec, spec.node_map()["write"]), spec

    full, _ = catalog_for()
    assert {"t:orders", "t:refunds", "c:orders.region"} <= set(full)
    only, _ = catalog_for(evidence_from=["other"])
    assert [a for a in only if a[0] == "Q"] == ["Q1"] and only["Q1"]["artifact"] == "c" * 64
    tables = {a for a, e in only.items() if e["kind"] == "table"}
    assert tables == {"t:refunds"}                       # fetch 的表结构（orders）不收
    assert all(o["artifact"] == "6" * 64 for o in only["t:refunds"]["sources"] if o["kind"] == "schema")
    off, spec = catalog_for(entities="off")
    assert not [a for a, e in off.items() if e["kind"] in ("table", "column")] and "Q1" in off
    assert report_entities(spec, spec.node_map()["write"]) == "off"


def test_find_entity_and_closest_entities(catalog):
    from app.engine.evidence import closest_entities, find_entity

    assert find_entity("ORDERS", catalog) == "t:orders"
    assert find_entity(" orders.Amount ", catalog) == "c:orders.amount"
    assert find_entity("amount", catalog) == "c:amount"             # 好几张表都有的字段
    assert find_entity("order_cnt", catalog) == "c:order_cnt"       # 没有表名的结果列
    assert find_entity("amount", catalog, "table") is None
    assert find_entity("orders", catalog, "column") is None
    assert find_entity("ghost_table", catalog) is None
    qualified = build_catalog(nodes={}, ledger=[{**SCHEMA, "schema": "public"}])
    assert find_entity("public.orders", qualified) == "t:orders" == find_entity("PUBLIC.ORDERS", qualified, "table")
    assert find_entity("public.orders.region", qualified) == "c:orders.region"

    assert closest_entities("ordrs", catalog)[0] == "t:orders"
    assert closest_entities("refund", catalog, limit=1) == ["t:refunds"]
    assert len(closest_entities("order", catalog, limit=2)) <= 2
    assert closest_entities("zzzz_qqq", catalog) == []
    assert closest_entities("ordrs", catalog) == closest_entities("ordrs", catalog)   # 同样的输入同样的顺序
