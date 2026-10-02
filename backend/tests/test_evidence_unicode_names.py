"""证据层按版本放宽中文标识符（H7）：上传的表格起中文表名、列名，报告里要能写 [[t:明细]]、[[c:明细.金额]]。

- 新组装的文档在顶层记 entity_syntax: 2（参与内容哈希）：显式标记和 SQL 里不加引号的名字认中文，
  名字按 names.name_key 找（只对 ASCII 不区分大小写，和 SQLite 一致）
- 没有 entity_syntax 的老文档按原来的 ASCII 规则复核：结果和当年一模一样，不出 state_mismatch
- 反引号里的名字核对、正文裸名自动链接仍只认 ASCII：「华东」不是可疑实体，中文正文不会整句被当成名字

表名、列名、取值都是假的（明细、汇总、分区甲……）。
"""
from __future__ import annotations

import asyncio
import copy
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.core.artifact_store import canonical_json
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine import evidence
from app.engine.evidence import (
    ENTITY_SYNTAX,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    doc_entity_syntax,
    find_entity,
    iter_segments,
    make_eid,
    query_entry_fields,
    render_markers,
    sql_tables,
    verify_doc,
)
from app.engine.runner import run_manager

SOURCE = "cn_ledger"
TOOL = f"db_query__{SOURCE}"
SCHEMA_ART = "5" * 64
Q_ART = "a" * 64
SQL = "SELECT 区域, SUM(金额) AS 合计 FROM 明细 GROUP BY 区域 ORDER BY 合计 DESC"
QUERY = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": Q_ART, "tool": TOOL, "source": SOURCE,
         "columns": ["区域", "合计"], "rows": 2, "truncated": False, "schema_artifact": SCHEMA_ART,
         "tables": ["明细"]}
SCHEMA = {"kind": "schema", "node_id": "fetch", "exec": 1, "artifact": SCHEMA_ART, "source": SOURCE,
          "tables": {"明细": ["日期", "区域", "金额", "order_id"], "汇总": ["区域", "合计"],
                     "orders": ["id", "amount"]}}
SNAPSHOT = {"columns": ["区域", "合计"], "rows": [["分区甲", 1200.5], ["分区乙", 980.0]], "sql": SQL}
FORMAT_REASON = "表名、字段名引用格式无法识别"


def loader(artifact):
    return SNAPSHOT if artifact == Q_ART else None


@pytest.fixture
def catalog():
    return build_catalog(nodes={}, ledger=[QUERY, SCHEMA])


def compose(text, catalog, **kw):
    return compose_doc(text, catalog, loader=loader, **kw)


def old_doc(text, catalog, monkeypatch):
    """升级前组装的文档：按 1 版规则组装，没有 entity_syntax 字段（那时还没有这个字段）。

    1 版这条路和升级前的代码逐字相同（组装出的文档字节一致），这里借它造老文档。"""
    with monkeypatch.context() as m:
        m.setattr(evidence, "ENTITY_SYNTAX", 1)
        doc = compose(text, catalog)
    assert doc.pop("entity_syntax") == 1
    return doc


def codes(result):
    return [v["code"] for v in result["violations"]]


def entities(doc):
    return [s for s in iter_segments(doc) if s["kind"] == "entity"]


# --------------------------------------------------------------------------
# 新文档：[[t:明细]]、[[c:明细.金额]] 解析得了
# --------------------------------------------------------------------------


def test_new_doc_records_the_syntax_and_resolves_chinese_names(catalog):
    doc = compose("本月[[t:明细]]的[[c:明细.金额]]按[[c:区域]]汇总，[[v:Q1.r0.区域]]最高。[[see:t:汇总,Q1]]", catalog)
    assert ENTITY_SYNTAX == 2 and doc["entity_syntax"] == 2 and doc_entity_syntax(doc) == 2
    assert '"entity_syntax":2' in canonical_json(doc)          # 在文档顶层，参与内容哈希
    assert doc["markdown"] == "本月明细的明细.金额按区域汇总，分区甲最高。"     # 显示写作者写的名字本身
    assert doc["violations"] == []
    table, column, shared = entities(doc)
    assert table["cite"]["alias"] == "t:明细" and table["state"] == "deterministic"
    assert table["cite"]["eid"] == make_eid("table", SCHEMA_ART, {"table": "明细"})
    assert column["cite"]["alias"] == "c:明细.金额"
    assert column["cite"]["locator"] == {"table": "明细", "column": "金额"}
    assert shared["cite"]["alias"] == "c:区域"                  # 明细、汇总都有这一列：只写列名也认
    [unit] = [u for b in doc["blocks"] for u in b["units"]]
    assert [s["alias"] for s in unit["see"]] == ["t:汇总", "Q1"]
    again = verify_doc(doc, catalog, loader=loader)
    assert again["ok"] is True and again["stats"]["entities"] == 3


def test_streaming_renders_chinese_names_like_the_full_doc(catalog):
    text = "本月[[t:明细]]的[[c:明细.金额]]上升。"
    renderer = StreamRenderer(catalog, loader=loader)
    streamed = "".join([renderer.feed("本月[[t:明"), renderer.feed("细]]的[[c:明细.金"), renderer.feed("额]]上升。"),
                        renderer.flush()])
    assert streamed == render_markers(text, catalog, loader=loader) == compose(text, catalog)["markdown"] \
        == "本月明细的明细.金额上升。"


@pytest.mark.parametrize("ref", [
    "t:ＡＢＣ",            # 全角：按 name_key 会归一成另一个名字，正文却照原样显示
    "t:明细\u3164",        # U+3164 填充字符：\w 认它是字母，肉眼看不出；NFKC 会变成 U+1160，NFKC 检查就挡住了
    "t:明细\u1160",        # U+1160、U+115F 是 NFKC 稳定的填充字符，只有 names.FILLERS 挡得住
    "c:明细.\u115f金额",
    "c:明细.金额(元)",      # 括号
    "t:明 细",             # 空格
    "t:1月明细",           # 数字开头
    "t:①期",               # 带圈数字
])
def test_names_outside_the_name_contract_are_a_format_error(catalog, ref):
    doc = compose(f"见 [[{ref}]]。", catalog)
    [seg] = [s for s in iter_segments(doc) if s.get("ref")]
    assert seg["cite"]["status"] == "unresolved" and seg["cite"]["reason"] == FORMAT_REASON
    assert codes(doc) == ["unresolved_ref"]
    fix = evidence._resolve_entity({"kind": ref[0], "ref": ref[2:], "body": ref[2:]}, catalog,
                                   evidence._Snapshots(loader)).get("for_model")
    assert "汉字" in fix                                         # 给模型的改写指令说的是新规则


def test_dollar_and_hash_names_the_old_rules_took_still_parse(catalog):
    """2 版是 1 版的超集：手工接入的库里 sales$2024 这种名字，1 版认的写法 2 版照样认（找不到是另一回事）。"""
    doc = compose("见 [[t:sales$2024]]、[[t:orders]]。", catalog)
    ghost, real = [s for s in iter_segments(doc) if s.get("ref")]
    assert ghost["issue"] == "unknown_entity" and ghost["cite"]["reason"] != FORMAT_REASON
    assert real["cite"]["status"] == "resolved"


# --------------------------------------------------------------------------
# 名字按 name_key 找：ABC 和 abc 是同一个，Дата 和 дата 不是
# --------------------------------------------------------------------------


def test_names_resolve_the_way_sqlite_compares_identifiers():
    catalog = build_catalog(nodes={}, ledger=[{**SCHEMA, "tables": {"Orders": ["Amount"], "Дата": ["Сумма"]}}])
    doc = compose("[[t:orders]]、[[t:ORDERS]]、[[c:orders.amount]]、[[t:Дата]]、[[c:Дата.Сумма]]。", catalog)
    assert doc["violations"] == []
    assert [s["cite"]["alias"] for s in entities(doc)] == ["t:Orders", "t:Orders", "c:Orders.Amount", "t:Дата",
                                                           "c:Дата.Сумма"]
    assert [s["text"] for s in entities(doc)] == ["orders", "ORDERS", "orders.amount", "Дата", "Дата.Сумма"]
    # SQLite 里「дата」是另一张表：在这次运行里找不到，是可疑实体
    assert codes(compose("[[t:дата]]。", catalog)) == ["unknown_entity"]
    assert find_entity("ORDERS", catalog) == "t:Orders" and find_entity("дата", catalog) is None
    # 1 版（复核老文档）照旧按 Python 的 lower() 找
    assert find_entity("дата", catalog, syntax=1) == "t:Дата"


@pytest.mark.parametrize("tables", [
    {"ＯＲＤＥＲＳ": ["a"], "orders": ["b"]},
    {"orders": ["b"], "ＯＲＤＥＲＳ": ["a"]},                   # 和目录顺序无关
])
def test_a_fullwidth_table_does_not_capture_the_ascii_one(tables):
    """SQLite 不做 NFKC：全角的「ＯＲＤＥＲＳ」和「orders」是两张表。先做 NFKC 再找的话，两张并存时
    [[t:orders]] 会被静默绑到全角那张，原本正确的 ASCII 引用反而指错。"""
    db = sqlite3.connect(":memory:")
    db.executescript('CREATE TABLE "ＯＲＤＥＲＳ" (a); CREATE TABLE orders (b);')
    assert [r[0] for r in db.execute("SELECT name FROM pragma_table_info('orders')")] == ["b"]

    catalog = build_catalog(nodes={}, ledger=[{**SCHEMA, "tables": tables}])
    assert {"t:ＯＲＤＥＲＳ", "t:orders"} <= set(catalog)
    doc = compose("[[t:orders]]、[[t:ORDERS]]、[[c:orders.b]]。", catalog)
    assert doc["violations"] == []
    assert [s["cite"]["alias"] for s in entities(doc)] == ["t:orders", "t:orders", "c:orders.b"]
    assert codes(compose("[[c:orders.a]]。", catalog)) == ["unknown_entity"]   # a 是全角那张表的字段
    assert find_entity("orders", catalog) == find_entity("ORDERS", catalog) == "t:orders"
    assert find_entity("orders", catalog, syntax=1) == "t:orders"


def test_a_fullwidth_table_alone_is_not_found_by_its_ascii_lookalike():
    """库里只有「ＡＢＣ」时，SQL 里写 FROM abc 是找不到表的：[[t:abc]] 也不能解析到它。"""
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE "ＡＢＣ" (x)')
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.execute("SELECT * FROM abc")
    catalog = build_catalog(nodes={}, ledger=[{**SCHEMA, "tables": {"ＡＢＣ": ["x"]}}])
    assert codes(compose("[[t:abc]]。", catalog)) == ["unknown_entity"]
    assert find_entity("abc", catalog) is None


def test_sql_tables_does_not_fold_fullwidth_names_either():
    assert sql_tables("SELECT * FROM ＯＲＤＥＲＳ JOIN orders ON 1=1") == ["ＯＲＤＥＲＳ", "orders"]
    assert sql_tables("WITH ｔ AS (SELECT 1) SELECT * FROM t") == ["t"]     # 真表 t 不是公用表表达式 ｔ
    assert sql_tables("WITH t AS (SELECT 1) SELECT * FROM T") == []          # ASCII 照旧不分大小写


@pytest.mark.parametrize("name", ["明细", "Orders", "Дата", "t_2026年", "金额_元", "ORDER_ID", "销售额2024"])
def test_the_lookup_key_is_name_key_for_every_name_the_import_can_produce(name):
    """证据层按名字找表用的就是 names.name_key（导入和证据层一套口径，不另写私有的键）：导入出来的名字都合
    name_problem（NFKC 稳定），手工接入的库里不合规的名字（全角）也不做 NFKC，和 SQLite 一致。"""
    import unicodedata

    from app.data.names import name_key, name_problem

    assert name_problem(name) is None and unicodedata.is_normalized("NFKC", name)
    assert evidence._SQL[2].key is name_key and evidence._EntityIndex({}, syntax=2).key is name_key
    assert not hasattr(evidence, "_ident_key")
    assert name_key("ＯＲＤＥＲＳ") == "ＯＲＤＥＲＳ" != name_key("orders")


def test_tables_differing_only_in_non_ascii_case_merge_in_the_catalog():
    """已知限制，钉住现状（见 evidence._entity_entries 的说明）：目录按 lower() 并表，比 SQLite 宽。

    「Дата」和「дата」在 SQLite 里是两张表，目录里却只有先出现的 t:Дата，дата 的字段记在它名下：
    [[c:Дата.y]] 能解析（y 其实是 дата 的），[[t:дата]] 判为疑似编造。上传的表格碰不到（导入按 collide_key
    判撞名）；目录没有版本，改并法会让碰上这种库的老文档复核对不上，等目录也加版本时再改。"""
    db = sqlite3.connect(":memory:")
    db.executescript('CREATE TABLE "Дата" (x); CREATE TABLE "дата" (y);')
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone() == (2,)

    catalog = build_catalog(nodes={}, ledger=[{**SCHEMA, "tables": {"Дата": ["x"], "дата": ["y"]}}])
    assert {a for a, e in catalog.items() if e["kind"] in ("table", "column")} == {"t:Дата", "c:Дата.x", "c:Дата.y"}
    doc = compose("[[c:Дата.y]]。", catalog)
    assert doc["violations"] == [] and entities(doc)[0]["cite"]["alias"] == "c:Дата.y"
    assert codes(compose("[[t:дата]]。", catalog)) == ["unknown_entity"]


# --------------------------------------------------------------------------
# 老文档：按 1 版复核，结果不变
# --------------------------------------------------------------------------

OLD_TEXT = ("本月[[t:明细]]的[[c:明细.金额]]按区域汇总，[[v:Q1.r0.区域]]最高，[[t:orders]] 另算，"
            "`order_id` 去重，`华东` 不算。[[see:Q1,t:汇总]]")


def test_old_doc_is_verified_by_the_rules_it_was_written_under(catalog, monkeypatch):
    doc = old_doc(OLD_TEXT, catalog, monkeypatch)
    assert doc_entity_syntax(doc) == 1
    # 当年中文名判为「格式无法识别」：正文是占位、状态 none
    [table, column] = [s for s in iter_segments(doc) if s.get("ref") in ("t:明细", "c:明细.金额")]
    assert table["text"] == "⟦?t:明细⟧" and table["state"] == "none" and table["issue"] == "unresolved_ref"
    assert table["cite"]["reason"] == FORMAT_REASON and column["text"] == "⟦?c:明细.金额⟧"
    stored = codes(doc)
    assert stored == ["unresolved_ref", "unresolved_ref", "unresolved_ref"]   # 两个标记 + see 里的 t:汇总

    again = verify_doc(doc, catalog, loader=loader)
    assert codes(again) == stored and again["stats"] == doc["stats"]
    assert not {"state_mismatch", "render_mismatch", "eid_mismatch"} & set(codes(again))
    # 组装时用的那份 _Snapshots（当前版）传进来也一样：以文档记下的版本为准
    assert codes(verify_doc(doc, catalog, loader=evidence._Snapshots(loader))) == stored

    # 对照：同一份文档要是按 2 版复核，当年的占位就和今天解析出的名字对不上，会被当成文档被改过
    forged = {**copy.deepcopy(doc), "entity_syntax": 2}
    assert "render_mismatch" in codes(verify_doc(forged, catalog, loader=loader))


def test_old_ascii_docs_verify_clean(catalog, monkeypatch):
    doc = old_doc("[[t:orders]] 的 [[c:orders.amount]] 已核对，`order_id` 去重。[[see:Q1]]", catalog, monkeypatch)
    assert doc["violations"] == []
    assert verify_doc(doc, catalog, loader=loader)["ok"] is True


@pytest.mark.parametrize("value", [3, 0, "2", True, None])
def test_an_unknown_syntax_version_is_a_schema_problem(catalog, value):
    doc = compose("[[t:明细]]。", catalog)
    doc["entity_syntax"] = value
    assert doc_entity_syntax(doc) is None
    assert "bad_schema" in codes(verify_doc(doc, catalog, loader=loader))


# --------------------------------------------------------------------------
# 反引号、正文裸名：两版都只认 ASCII
# --------------------------------------------------------------------------


def test_chinese_words_in_backticks_are_not_suspicious(catalog):
    doc = compose("`华东`的客流高于`分区乙`，`明细`也提到了；`ghost_table` 是编的。", catalog)
    assert codes(doc) == ["unknown_entity"]                    # 只有 ASCII 的 ghost_table
    assert doc["violations"][0]["text"] == "ghost_table"
    [flagged] = entities(doc)
    assert flagged["name"] == "ghost_table" and flagged["state"] == "none"
    assert codes(verify_doc(doc, catalog, loader=loader)) == ["unknown_entity"]


def test_writing_rules_tell_the_model_to_mark_chinese_names_explicitly(catalog):
    """反引号里的中文名不核对，写作规则就不能再教模型「或者放进反引号」：照写 `某表` 就绕开了核对。"""
    prompt = catalog_prompt(catalog, loader=loader)
    assert "或者放进反引号（`表名`）" not in prompt
    assert "中文的表名、字段名一律用 [[t:]] / [[c:]]" in prompt and "反引号里的中文名系统不核对" in prompt
    # 规矩和实际一致：显式标记的编造中文表名会被标出，反引号里的不会
    assert codes(compose("本月[[t:假表]]下降。", catalog)) == ["unknown_entity"]
    assert codes(compose("本月`假表`下降。", catalog)) == []


def test_bare_names_still_link_only_ascii(catalog):
    doc = compose("明细里的金额按区域汇总，order_id 去重后计数。", catalog)
    linked = entities(doc)
    assert [s["text"] for s in linked] == ["order_id"] and linked[0]["auto"] is True
    assert linked[0]["cite"]["alias"] == "c:明细.order_id"
    assert doc["violations"] == [] and verify_doc(doc, catalog, loader=loader)["ok"] is True


# --------------------------------------------------------------------------
# SQL 里不加引号的中文表名
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, tables", [
    ("SELECT SUM(金额) FROM 明细", ["明细"]),
    ("SELECT a.区域, SUM(b.合计) FROM 明细 a JOIN 汇总 AS b ON a.区域 = b.区域 GROUP BY a.区域", ["明细", "汇总"]),
    ("SELECT * FROM 明细, 汇总 WHERE 明细.区域 = 汇总.区域", ["明细", "汇总"]),
    ("SELECT * FROM orders o LEFT JOIN 明细 d ON d.order_id = o.id", ["orders", "明细"]),
    ("WITH 本期 AS (SELECT * FROM 明细 WHERE 月份 = '9月') SELECT * FROM 本期 JOIN 汇总 USING (区域)", ["明细", "汇总"]),
    ("SELECT '来自 FROM 幽灵甲' AS 备注 FROM 明细 -- JOIN 幽灵乙", ["明细"]),
    ("SELECT * FROM main.明细 JOIN t_2026年 ON 1 = 1", ["main.明细", "t_2026年"]),
    ("SELECT $标签$ FROM 幽灵 $标签$ AS a FROM 明细", ["明细"]),
    ("SELECT 明细#1.x FROM 明细#1", ["明细#1"]),
    ("SELECT * FROM Дата JOIN дата ON 1 = 1", ["Дата", "дата"]),        # SQLite 里是两张表
    ("select * from Orders join orders on 1=1", ["Orders"]),           # ASCII 照旧不分大小写
])
def test_sql_tables_reads_unquoted_chinese_names(sql, tables):
    assert sql_tables(sql) == tables


# 数据库怎么切分不加引号的名字：SQLite、PostgreSQL、MySQL 都把 ASCII 以外的字符算作名字的一部分（开头也算）。
# 每条都真的在 SQLite 里执行一遍：能查，且查到的就是 sql_tables 给出的那几张表
DB_TABLES = ["销售数据（2024）", "客流·总计", "明细", "汇总", "明细　甲", "明细，汇总", "１月", "　明细"]


@pytest.mark.parametrize("sql, tables", [
    ("SELECT SUM(金额) FROM 销售数据（2024）", ["销售数据（2024）"]),     # 全角括号是名字的一部分
    ("SELECT * FROM 客流·总计", ["客流·总计"]),                          # 间隔号也是
    ("SELECT * FROM 明细 JOIN 客流·总计 ON 1 = 1", ["明细", "客流·总计"]),
    ("SELECT * FROM 明细　甲", ["明细　甲"]),                            # 全角空格不是空白，是一张表
    ("SELECT * FROM 明细，汇总", ["明细，汇总"]),                        # 全角逗号不分隔
    ("SELECT * FROM １月", ["１月"]),                                    # 全角数字开头也是名字
    ("SELECT 1 FROM　明细", []),                                        # 「FROM　明细」是一个名字（这里当列别名）
    ("SELECT * FROM 　明细", ["　明细"]),                                # 全角空格开头的名字：不能当空白吃掉
    ("SELECT * FROM 明细 a JOIN 汇总 b ON a.金额 = b.金额", ["明细", "汇总"]),
])
def test_sql_tables_splits_unquoted_names_the_way_the_database_does(sql, tables):
    db = sqlite3.connect(":memory:")
    for name in DB_TABLES:
        db.execute(f'CREATE TABLE "{name}" (金额 REAL)')
    db.execute(sql).fetchall()                                         # 库里这条 SQL 是能查的
    assert sql_tables(sql) == tables
    assert sql_tables(sql, syntax=1) == []                             # 1 版照旧一个也不认


def test_a_truncated_table_name_cannot_vouch_for_a_table_that_does_not_exist():
    """以前 2 版在全角括号处把名字截断：台账记下不存在的「销售数据」，[[t:销售数据]] 判为有出处、无违规，
    出处链（api/evidence.py 的 _query_present）也判它在这条 SQL 里。"""
    from app.api.evidence import _query_present

    sql = "SELECT SUM(金额) AS 合计 FROM 销售数据（2024）"
    fields = query_entry_fields({"artifact": Q_ART, "columns": ["合计"], "rows": [[1.5]], "sql": sql,
                                 "source": SOURCE, "schema_artifact": SCHEMA_ART})
    assert fields["tables"] == ["销售数据（2024）"]
    schema = {**SCHEMA, "tables": {"销售数据（2024）": ["金额"]}}
    catalog = build_catalog(nodes={}, ledger=[{"kind": "query", "node_id": "fetch", "exec": 1, **fields}, schema])
    assert "t:销售数据" not in catalog and catalog["t:销售数据（2024）"]["queries"] == ["Q1"]

    doc = compose_doc("[[t:销售数据]]的合计见 [[see:Q1]]。", catalog,
                      loader=lambda a: {"columns": ["合计"], "rows": [[1.5]], "sql": sql} if a == Q_ART else None)
    [seg] = entities(doc)
    assert seg["issue"] == "unknown_entity" and seg["state"] == "none" and seg["cite"]["status"] == "unresolved"
    assert codes(doc) == ["unknown_entity"]
    assert _query_present({"sql": sql}, "table", "销售数据", None) is False
    assert _query_present({"sql": sql}, "table", "销售数据（2024）", None) is True


def test_sql_tables_keeps_the_old_rules_for_version_one():
    assert sql_tables("SELECT SUM(金额) FROM 明细", syntax=1) == []
    assert sql_tables('SELECT SUM(金额) FROM "明细"', syntax=1) == ["明细"]
    assert sql_tables('SELECT * FROM "Дата" JOIN "дата" ON 1 = 1', syntax=1) == ["Дата"]
    assert sql_tables('SELECT * FROM "Дата" JOIN "дата" ON 1 = 1') == ["Дата", "дата"]


def test_query_entry_ties_a_chinese_table_to_its_query():
    """以前 FROM 明细 认不出来：表条目记不下是哪次查询用到的，写作目录把它归到「其他表」。"""
    fields = query_entry_fields({"artifact": Q_ART, "columns": ["区域", "合计"], "rows": [["分区甲", 1]],
                                 "sql": SQL, "source": SOURCE, "schema_artifact": SCHEMA_ART})
    assert fields["tables"] == ["明细"]
    catalog = build_catalog(nodes={}, ledger=[{"kind": "query", "node_id": "fetch", "exec": 1, **fields}, SCHEMA])
    assert catalog["t:明细"]["queries"] == ["Q1"] and catalog["t:汇总"]["queries"] == []
    prompt = catalog_prompt(catalog, loader=loader)
    assert "- 明细（Q1 用到）" in prompt and "其他表：汇总" in prompt


# --------------------------------------------------------------------------
# 跑一次：中文表名的库，查询 → 报告 → 出口复核
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def cn_source(tmp_path):
    """探查过结构的中文表名库：明细、汇总两张表，表名、列名都不加引号也能查。"""
    from app.data.introspect import introspect

    path = tmp_path / "cn.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE 明细 (日期 TEXT, 区域 TEXT, 金额 REAL);"
                     "CREATE TABLE 汇总 (区域 TEXT, 合计 REAL);")
    db.executemany("INSERT INTO 明细 VALUES (?, ?, ?)",
                   [("2026-09-01", "分区甲", 700.5), ("2026-09-02", "分区甲", 500.0), ("2026-09-02", "分区乙", 980.0)])
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


async def finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没有结束：{row.status}")


async def events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype).order_by(RunEvent.seq)
        return list((await session.execute(q)).scalars())


async def test_a_report_on_chinese_tables_passes_the_exit_check(engine_up, cn_source, monkeypatch):
    from app.providers import mock_model

    reply = "[[t:明细]]里[[v:Q1.r0.区域]]的[[c:明细.金额]]合计最高，为 [[v:Q1.r0.合计]]。[[see:Q1]]"
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=reply))
    nodes = [node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
             node("write", "report", instructions="写一句", on_violation="fail", max_repairs=0),
             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}],
                  contract={"report_from": "write"})]
    graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    row = await finish(graph)
    assert row.status == "succeeded", row.error
    assert row.output["r"] == "明细里分区甲的明细.金额合计最高，为 1,200.5。"

    [finished] = [e for e in await events(row.id, "node.finished") if e.node_id == "fetch"]
    [query] = [e for e in finished.data["evidence"] if e["kind"] == "query"]
    assert query["tables"] == ["明细"]                           # 不加引号的中文表名记进了台账

    [checked] = await events(row.id, "report.checked")
    assert checked.data["violations"] == [], checked.data["violations"]
    doc = artifact_store.load(checked.data["doc_artifact"])
    assert doc["entity_syntax"] == 2
    assert {s["cite"]["alias"] for s in iter_segments(doc) if s["kind"] == "entity"} == {"t:明细", "c:明细.金额"}
    assert doc["catalog"]["t:明细"]["queries"] == ["Q1"]

    [issuance] = await events(row.id, "issuance")
    # 出口按同一套规则独立复核（目录从状态重建）：满档出具，没有缺口
    assert issuance.data["tier"] == "formal" and issuance.data["gaps"] == [], issuance.data
    assert issuance.data["unresolved"] == 0 and issuance.data["matched_numbers"] == 1
