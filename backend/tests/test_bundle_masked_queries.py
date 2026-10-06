"""可验证报告里数据源设了遮罩的查询快照（期 5 前置 L2）：能证明这条查询没碰遮罩列的，快照照常随文件附带。

以前只要查询当时记下了数据源的遮罩列（mask_columns），或者数据源现在设了遮罩，整份快照就只给哈希——哪怕这条查询只按
地区汇总了金额。数据源里只要有一列设了遮罩（会员手机号），报告里引用查询格子的数字就全成了「无法核对」。

现在全部满足才嵌入（build._untouched）：认得这份快照（数据源查询工具落的那种、哈希对得上）；结果的列名对不上遮罩列；
SQL 原文里没写到遮罩列（连条件里也不行：SQL 随快照一起附带，WHERE phone = '…' 的字面量就是遮罩值），也没写着这次运行
别的查询里查到过的遮罩值；没有不点列名也读得出表里内容的写法（字符串里的 SQL 交给函数执行、统计信息里的采样值、
系统 schema）；SQL 解析得了，没有认不出取自哪一列的写法（列名表、整行引用——FROM 里的表函数不写 LATERAL 也看得见前面的
来源、可能展开出遮罩列的 *），按 SQL 认遮罩列的那一套（inquiry._derived_hidden）也认不出哪个结果列取自遮罩列。任何一条
不满足、判断过程出错，照旧只给哈希。

工件、事件都是手搭的（会员、号码、地区、金额都是编的），照 tests/test_bundle_build.py 的样子。
"""
from __future__ import annotations

import base64
import json

import pytest

from app.bundle.build import build_bundle, render_html
from app.engine.expressions import cell_value, column_kind, same_value
from app.engine.merge_query import MERGE_SOURCE
from tests.test_bundle_build import GRAPH, RUN, stored

#: 会员库的遮罩：手机号
MASKS = {"crm": ["phone"]}
PHONES = ("000-0001", "000-0002")
AGGREGATE = "SELECT region, SUM(spend) AS spend FROM members GROUP BY region ORDER BY region"


def snapshot(sql: str, columns: list[str], rows: list[list], **extra) -> dict:
    """会员库（crm）的一份查询快照，照数据源查询工具落的样子：查询当时记下了遮罩列。"""
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False, "elapsed_ms": 3,
            "sql": sql, "source": "crm", "mask_columns": ["phone"], **extra}


REGIONS = snapshot(AGGREGATE, ["region", "spend"], [["华东", 508.5], ["华南", 300.0]],
                   column_types={"region": "text", "spend": "number"})


def cite(value, row: int, column: str) -> dict:
    return {"alias": "Q1", "kind": "cell", "locator": {"row": row, "column": column}, "role": "value",
            "status": "resolved", "value": value, "rendered": str(value)}


def bundle_of(snap: dict, *, value=508.5, row: int = 0, column: str = "spend", masks=MASKS, raw: bytes | None = None,
              artifact: str | None = None, others: tuple[dict, ...] = ()):
    """一次手搭的运行：会员库一次查询 → 报告引用快照里的一格（默认第 0 行的 spend）。raw 给了就把服务端的快照文件换成
    这份字节；artifact 给了就用它当快照的 id（文件被改过、哈希对不上）。others 是同一次运行里另外几次会员库的查询
    （门店流水节点查的，报告没引用），排在这次查询之后。"""
    snap_id, snap_raw = stored(snap)
    snap_id, snap_raw = artifact or snap_id, raw if raw is not None else snap_raw
    doc = {"schema": "agentlab.report/1", "run_id": RUN["id"], "node_id": "write", "markdown": "…",
           "catalog": {"Q1": {"alias": "Q1", "kind": "query", "artifact": snap_id, "node_id": "members"}},
           "blocks": [{"id": "b0", "type": "paragraph", "units": [{
               "id": "u0", "kind": "claim", "cites": ["Q1"],
               "segments": [{"id": "s0", "kind": "number", "text": str(value), "state": "deterministic",
                             "cite": cite(value, row, column)}]}]}]}
    doc_id, doc_raw = stored(doc)
    files = {snap_id: snap_raw, doc_id: doc_raw}
    later = []
    for other in others:
        other_id, other_raw = stored(other)
        files[other_id] = other_raw
        later += [("tool.end", "fetch", {"tool": "db_query__crm", "query_artifact": other_id,
                                         "preview": json.dumps(other["rows"][:1], ensure_ascii=False)}),
                  ("node.finished", "fetch",
                   {"evidence": [{"kind": "query", "node_id": "fetch", "artifact": other_id}]})]
    events = [(seq, *event) for seq, event in enumerate([
        ("run.started", None, {"nodes": 3}),
        ("tool.end", "members", {"tool": "db_query__crm", "query_artifact": snap_id,
                                 "preview": json.dumps(snap["rows"][:1], ensure_ascii=False)}),
        ("node.finished", "members", {"evidence": [{"kind": "query", "node_id": "members", "artifact": snap_id}]}),
        *later,
        ("report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        ("node.finished", "write", {"preview": {"text": "…", "doc_artifact": doc_id}}),
        ("run.finished", None, {"output": {}}),
    ], start=1)]
    bundle = build_bundle(RUN, events, manifest_seq=len(events), manifest_hash="0" * 64, graph=GRAPH,
                          source_masks=masks, read=files.get, exported_at="2026-10-06T08:00:00+00:00")
    return bundle, next(a for a in bundle["artifacts"] if a["id"] == snap_id)


def browser_would_pass(bundle: dict, entry: dict) -> bool:
    """照浏览器 ⑤ 的办法核一遍：引用值和嵌进来的快照里那一格相等（cell_value / same_value，verifier.js 移植的就是它们）。"""
    snap = json.loads(base64.b64decode(entry["b64"]))
    doc_entry = next(a for a in bundle["artifacts"] if a["kind"] == "report_doc")
    doc = json.loads(base64.b64decode(doc_entry["b64"]))
    seg = doc["blocks"][0]["units"][0]["segments"][0]
    loc = seg["cite"]["locator"]
    index = snap["columns"].index(loc["column"])
    truth = cell_value(snap["rows"][loc["row"]][index], column_kind(snap, loc["column"]))
    return same_value(seg["cite"]["value"], truth)


def shown(bundle: dict, seq: int) -> bool:
    return "canonical" in next(e for e in bundle["events"] if e["seq"] == seq)


# --------------------------------------------------------------------------
# 没碰遮罩列：照常附带
# --------------------------------------------------------------------------


@pytest.mark.parametrize("masks, recorded", [
    (MASKS, True),        # 查询当时记下了遮罩，数据源现在也设着
    ({}, True),           # 只有查询当时记下的（数据源事后取消了遮罩、改了名）
    (MASKS, False),       # 查询之后才给数据源设的遮罩
])
def test_an_aggregate_by_region_on_a_masked_source_is_embedded(masks, recorded):
    """数据源设了手机号遮罩、查询按地区汇总金额：快照原字节随文件附带，引用它的数字核对得过；查询事件也带规范字符串。"""
    snap = REGIONS if recorded else {k: v for k, v in REGIONS.items() if k != "mask_columns"}
    bundle, entry = bundle_of(snap, masks=masks)
    assert "masked" not in entry and base64.b64decode(entry["b64"]) == stored(snap)[1]
    assert browser_would_pass(bundle, entry)
    assert shown(bundle, 2)


@pytest.mark.parametrize("sql, columns, rows", [
    # 先在 WITH 里把一张表整个选出来（* 展开的列只按名字往外传），外层按名字取
    ("WITH paid AS (SELECT * FROM members WHERE spend > 0) SELECT region, SUM(spend) AS spend FROM paid "
     "GROUP BY region ORDER BY region", ["region", "spend"], [["华东", 508.5]]),
    # FROM 里的子查询、LATERAL 里的 *：同上
    ("SELECT t.region, SUM(t.spend) AS spend FROM (SELECT * FROM members) AS t GROUP BY t.region",
     ["region", "spend"], [["华东", 508.5]]),
    ("SELECT m.region, SUM(o.amount) AS spend FROM members m JOIN LATERAL "
     "(SELECT * FROM orders o2 WHERE o2.member_id = m.id) o ON TRUE GROUP BY m.region", ["region", "spend"],
     [["华东", 508.5]]),
    # EXISTS 里的 * 只决定有没有，带不出值
    ("SELECT region, SUM(spend) AS spend FROM members m WHERE NOT EXISTS (SELECT * FROM blocked b WHERE b.id = m.id) "
     "GROUP BY region", ["region", "spend"], [["华东", 508.5]]),
    # COUNT(*) 只数个数
    ("SELECT region, COUNT(*) AS spend FROM members GROUP BY region", ["region", "spend"], [["华东", 508.5]]),
    # * 展开的是写明了列的派生来源：列名都看得见
    ("WITH s AS (SELECT region, SUM(spend) AS spend FROM members GROUP BY region) SELECT * FROM s",
     ["region", "spend"], [["华东", 508.5]]),
    # MySQL、SQLite 的反引号：通用读法解析不了，SQLite / MySQL 的读法读得通就行
    ("SELECT `region`, SUM(`spend`) AS `spend` FROM `members` GROUP BY `region`", ["region", "spend"],
     [["华东", 508.5]]),
    # 列和 WITH 里的那一段同名：那一段明写出了这一列，是列不是整行引用
    ("WITH spend AS (SELECT region, SUM(spend) AS spend FROM members GROUP BY region) "
     "SELECT region, spend FROM spend ORDER BY spend DESC", ["region", "spend"], [["华东", 508.5]]),
    # 列名碰巧像统计视图、导出函数的名字（不跟左括号的不是函数），FROM 里的表函数吃的是没设遮罩的列
    ("SELECT region, SUM(spend) AS spend FROM members WHERE pg_status = 'paid' AND crosstab IS NULL GROUP BY region",
     ["region", "spend"], [["华东", 508.5]]),
    ("SELECT m.region, SUM(m.spend) AS spend FROM members m, json_each_text(m.extra) j WHERE j.key = 'vip' "
     "GROUP BY m.region", ["region", "spend"], [["华东", 508.5]]),
    # 括号括起来的一组连接，按名字取列
    ("SELECT m.region, SUM(o.amount) AS spend FROM (members m JOIN orders o ON o.member_id = m.id) GROUP BY m.region",
     ["region", "spend"], [["华东", 508.5]]),
])
def test_queries_that_only_read_unmasked_columns_are_embedded(sql, columns, rows):
    bundle, entry = bundle_of(snapshot(sql, columns, rows))
    assert "masked" not in entry and "b64" in entry, sql
    assert browser_would_pass(bundle, entry)


# --------------------------------------------------------------------------
# 用到或可能用到遮罩列：只给哈希
# --------------------------------------------------------------------------

MASKED_SHAPES = [
    # 直接选出遮罩列
    ("SELECT name, phone, spend FROM members", ["name", "phone", "spend"]),
    # 起别名
    ("SELECT name, phone AS tel, spend FROM members", ["name", "tel", "spend"]),
    # 经子查询转一道
    ("SELECT t.x, t.spend FROM (SELECT phone AS x, spend FROM members) t", ["x", "spend"]),
    # 经 WITH 转一道
    ("WITH c AS (SELECT phone AS p, spend FROM members) SELECT p AS contact, spend FROM c", ["contact", "spend"]),
    # SELECT *：结果里有遮罩列
    ("SELECT * FROM members", ["id", "name", "phone", "region", "spend"]),
    # 函数包着遮罩列，起不起别名都一样
    ("SELECT SUBSTR(phone, 5) AS tail, spend FROM members", ["tail", "spend"]),
    ("SELECT UPPER(phone), spend FROM members", ["UPPER(phone)", "spend"]),
    # 遮罩列只在条件里：SQL 随快照一起附带，条件里的号码就是遮罩值
    ("SELECT region, SUM(spend) AS spend FROM members WHERE phone = '000-0001' GROUP BY region", ["region", "spend"]),
    # 汇总里数遮罩列的个数、只在 IS NOT NULL 里：结果里没有号码，也算碰了。有意比「按 SQL 认不出结果列取自遮罩列」
    # 更严：FROM 里的表函数吃进遮罩列（unnest(ARRAY[phone])）时，它交出的列取自哪一列认不出，只能看 SQL 写没写到
    ("SELECT region, COUNT(DISTINCT phone) AS spend FROM members GROUP BY region", ["region", "spend"]),
    ("SELECT unnest AS spend FROM members, unnest(ARRAY[phone])", ["spend"]),
    # 换个大小写、加引号照样认得出
    ('SELECT region, SUM(spend) AS spend FROM members WHERE "PHONE" IS NOT NULL GROUP BY region', ["region", "spend"]),
]


@pytest.mark.parametrize("sql, columns", MASKED_SHAPES)
def test_queries_that_use_a_masked_column_ship_only_their_id(sql, columns):
    rows = [[f"v{i}" if c != "spend" else 508.5 for i, c in enumerate(columns)]]
    if "phone" in columns:
        rows[0][columns.index("phone")] = PHONES[0]
    bundle, entry = bundle_of(snapshot(sql, columns, rows))
    assert entry == {"id": entry["id"], "kind": "query", "node_id": "members", "masked": True}, sql
    assert not shown(bundle, 2)
    assert PHONES[0] not in render_html(bundle)


@pytest.mark.parametrize("sql, columns", [
    # * 展开了数据源里的表：不知道它展开了哪些列，宁可多挡（哪怕结果的列名对不上遮罩列）
    ("SELECT * FROM orders", ["id", "region", "spend"]),
    ("SELECT o.* FROM orders o", ["id", "region", "spend"]),
    ("SELECT * FROM (SELECT * FROM orders WHERE spend > 0) t", ["id", "region", "spend"]),
    # * 落在 UNION 的一支里：按位置对上第一支的列名，列名认不出取自哪一列
    ("SELECT region, spend FROM (SELECT 'x' AS region, 0 AS spend UNION ALL SELECT * FROM members2) t",
     ["region", "spend"]),
    # 标量子查询、套在函数里的 *：值装进了另一个名字的列
    ("SELECT (SELECT * FROM members2 LIMIT 1) AS spend", ["spend"]),
    ("SELECT to_json(m.*) AS spend FROM members m", ["spend"]),
    # 整行引用（Postgres）：一整行装进一列；子查询里引用外层的别名、没起别名的表名也算
    ("SELECT to_json(m) AS spend FROM members m", ["spend"]),
    ("SELECT m AS spend FROM members m", ["spend"]),
    ("SELECT (SELECT to_json(m) FROM orders o WHERE o.member_id = m.id) AS spend FROM members m", ["spend"]),
    ("SELECT row_to_json(members) AS spend FROM members", ["spend"]),
    # FROM、JOIN 里的表函数（Postgres）不写 LATERAL 也看得见前面的来源：整行交给它，一列列竖着摊开（逐列做数据画像的
    # 常见写法，结果的 key、value 里就是 phone 和号码）。逗号、CROSS JOIN、JOIN … ON TRUE、LEFT JOIN、ROWS FROM、
    # 不带别名的 UNNEST、套在 FROM 的子查询里都一样
    ("SELECT key, value, COUNT(*) AS n FROM members m, jsonb_each_text(to_jsonb(m)) GROUP BY key, value "
     "ORDER BY n DESC LIMIT 20", ["key", "value", "n"]),
    ("SELECT j.key AS k, j.value AS spend FROM members m CROSS JOIN jsonb_each_text(to_jsonb(m)) j", ["k", "spend"]),
    ("SELECT j.value AS spend FROM members m JOIN jsonb_each_text(row_to_json(m)::jsonb) j ON TRUE", ["spend"]),
    ("SELECT r.value AS spend FROM members m LEFT JOIN jsonb_each_text(to_jsonb(m)) r ON TRUE", ["spend"]),
    ("SELECT value AS spend FROM members m, ROWS FROM (jsonb_each_text(to_jsonb(m)))", ["spend"]),
    ("SELECT value AS spend FROM members m, each(hstore(m))", ["spend"]),
    ("SELECT value AS spend FROM members, json_each_text(row_to_json(members))", ["spend"]),
    ("SELECT unnest AS spend FROM members m, unnest(ARRAY[to_jsonb(m)])", ["spend"]),
    ("SELECT v AS spend FROM (SELECT value AS v FROM members m, jsonb_each_text(to_jsonb(m))) t", ["spend"]),
    # 括号括起来的一组连接：组里的表函数看得见组里前面的来源，外层也看得见组里的名字
    ("SELECT value AS spend FROM (members m CROSS JOIN jsonb_each_text(to_jsonb(m))) AS x", ["spend"]),
    ("SELECT value AS spend FROM members m JOIN (orders o CROSS JOIN jsonb_each_text(to_jsonb(m))) ON TRUE", ["spend"]),
    ("SELECT to_json(m) AS spend FROM (members m JOIN orders o ON o.member_id = m.id)", ["spend"]),
    # WITH 里的 * 只按名字往外传，可外层把那一段整行交给了表函数
    ("WITH c AS (SELECT * FROM members) SELECT region, value AS spend FROM c, json_each_text(row_to_json(c))",
     ["region", "spend"]),
    # 函数记法：m.to_jsonb 就是 to_jsonb(m)
    ("SELECT m.to_jsonb AS spend FROM members m", ["spend"]),
    ("SELECT m.row_to_json AS spend FROM members m", ["spend"]),
    # 列名表按位置给列改名（Postgres 的表别名、子查询、WITH 都能写）
    ("SELECT c AS spend FROM members AS m(a, b, c)", ["spend"]),
    ("WITH t(a, b) AS (SELECT * FROM members2) SELECT b AS spend FROM t", ["spend"]),
    # 语法树认不准数据库实际执行什么：MySQL、MariaDB 会执行的注释，Postgres 的 Unicode 转义名字
    ("SELECT region /*! , SUM(spend) */ AS spend FROM members GROUP BY region", ["region", "spend"]),
    ("SELECT region, c AS spend FROM (SELECT 1 AS region, 2 AS c /*M! UNION ALL SELECT * FROM contacts */) t",
     ["region", "spend"]),
    ('SELECT U&"\\0073pend" AS spend FROM members', ["spend"]),
])
def test_shapes_that_could_hide_a_masked_column_ship_only_their_id(sql, columns):
    """SQL 里没写到遮罩列、结果的列名也对不上，可认不出结果里哪一列取自哪一列：照样只给哈希，查询事件也只给哈希。"""
    bundle, entry = bundle_of(snapshot(sql, columns, [[508.5] * len(columns)]), column=columns[-1])
    assert entry.get("masked") is True and "b64" not in entry, sql
    assert not shown(bundle, 2)


def test_profiling_every_column_of_a_masked_table_ships_only_its_id():
    """逐列做数据画像（整行摊成键值、按值数个数）：结果里就是 phone 和号码。快照、查询事件都只给哈希，页面里找不到号码。
    以前 FROM 里的表函数按看不见前面的来源算，整行引用认不出，快照和事件的预览原样带出了号码。"""
    sql = ("SELECT key, value, COUNT(*) AS n FROM members m, jsonb_each_text(to_jsonb(m)) GROUP BY key, value "
           "ORDER BY n DESC LIMIT 20")
    snap = snapshot(sql, ["key", "value", "n"], [["phone", PHONES[0], 3], ["region", "华东", 2]])
    bundle, entry = bundle_of(snap, value=3, column="n")
    assert entry.get("masked") is True and "b64" not in entry
    assert not shown(bundle, 2)
    assert PHONES[0] not in render_html(bundle)


@pytest.mark.parametrize("sql, columns", [
    # 表名、SQL 写在字符串里交给函数执行、整表导出：语法树里看不到 *，也看不到遮罩列（Postgres 的 XML 一族、ts_stat，
    # Oracle 的 DBMS_XMLGEN）
    ("SELECT table_to_xml('members', true, false, '') AS x, 3 AS spend", ["x", "spend"]),
    ("SELECT query_to_xml('select * from members', true, false, '') AS spend", ["spend"]),
    ("SELECT DBMS_XMLGEN.GETXML('SELECT * FROM members') AS spend FROM dual", ["spend"]),
    ("SELECT word AS region, ndoc AS spend FROM ts_stat('SELECT search_text FROM members')", ["region", "spend"]),
    # 统计信息里的采样值（Postgres 的 pg_stats、SQLite 的 sqlite_stat4、MySQL 的 column_statistics、Oracle 的直方图和
    # 列统计）
    ("SELECT attname AS region, most_common_vals::text AS vals, n_distinct AS spend FROM pg_stats "
     "WHERE tablename = 'members'", ["region", "vals", "spend"]),
    ("SELECT tbl AS region, sample AS spend FROM sqlite_stat4", ["region", "spend"]),
    ("SELECT histogram AS spend FROM information_schema.column_statistics WHERE table_name = 'members'", ["spend"]),
    ("SELECT endpoint_actual_value AS spend FROM all_tab_histograms WHERE table_name = 'MEMBERS'", ["spend"]),
    ("SELECT low_value AS spend FROM user_tab_columns WHERE table_name = 'MEMBERS'", ["spend"]),
    # 按页、按文件、按日志读出的原始数据；别的会话执行过的 SQL 原文（写入语句里就是值）
    ("SELECT get_raw_page('members', 0) AS spend", ["spend"]),
    ("SELECT pg_read_file('base/16384/16385') AS spend", ["spend"]),
    ("SELECT LOAD_FILE('/var/lib/mysql/crm/members.ibd') AS spend", ["spend"]),
    ("SELECT data AS spend FROM pg_logical_slot_peek_changes('cdc', NULL, NULL)", ["spend"]),
    ("SELECT argument AS spend FROM mysql.general_log", ["spend"]),
    # 系统 schema
    ("SELECT relname AS region, reltuples AS spend FROM pg_catalog.pg_class", ["region", "spend"]),
])
def test_reads_that_name_no_column_ship_only_their_id(sql, columns):
    """不点列名也读得出表里内容的写法：SQL 里没写到遮罩列、结果的列名也对不上、语法树里也没有 * 和整行引用，结果里照样
    可能是号码。照样只给哈希。"""
    bundle, entry = bundle_of(snapshot(sql, columns, [[508.5] * len(columns)]), column=columns[-1])
    assert entry.get("masked") is True and "b64" not in entry, sql
    assert not shown(bundle, 2)


#: 同一次运行里先查过号码的那份快照（模型在这里看到了号码）
LOOKUP = snapshot("SELECT name, phone FROM members ORDER BY id", ["name", "phone"],
                  [["林一", PHONES[0]], ["周二", PHONES[1]]])


def test_a_query_quoting_a_masked_value_seen_in_the_run_ships_only_its_id():
    """模型拿在遮罩查询里看到的号码去查别的表：SQL 里没写遮罩列，可字面量就是号码，SQL 随快照附带就带出了它。这次运行里
    查到过的遮罩值写在 SQL 里的，只给哈希，查询事件也只给哈希。"""
    sql = (f"SELECT region, SUM(spend) AS spend FROM members WHERE id IN "
           f"(SELECT member_id FROM tickets WHERE caller = '{PHONES[1]}') GROUP BY region")
    bundle, entry = bundle_of(snapshot(sql, ["region", "spend"], [["华东", 508.5]]), others=(LOOKUP,))
    assert entry.get("masked") is True and "b64" not in entry
    assert not shown(bundle, 2)
    assert PHONES[1] not in render_html(bundle)


def test_a_literal_that_is_not_a_masked_value_is_still_embedded():
    """同一次运行里查过号码，可这条 SQL 的字面量不是其中哪一个（地区名、别的号码）：照常附带。"""
    sql = ("SELECT region, SUM(spend) AS spend FROM members WHERE id IN "
           "(SELECT member_id FROM tickets WHERE caller = '000-0009') AND region = '华东' GROUP BY region")
    bundle, entry = bundle_of(snapshot(sql, ["region", "spend"], [["华东", 508.5]]), others=(LOOKUP,))
    assert "masked" not in entry and "b64" in entry
    assert browser_would_pass(bundle, entry)


@pytest.mark.parametrize("parsed", [True, False])
def test_result_columns_named_like_a_masked_column_ship_only_their_id(parsed, monkeypatch):
    """结果的列名按数据库交回的认（大小写不论）：SQL 里看不出来，列名对上遮罩列也只给哈希。这一条不靠按 SQL 认遮罩列的
    那一套（它认出的名字里本来就有遮罩列本身）：那一套什么都没认出来时照样拦得住。"""
    if not parsed:
        from app.engine import inquiry

        monkeypatch.setattr(inquiry, "_derived_hidden", lambda *_args: (set(), set()))
    snap = snapshot("SELECT region, contact FROM member_view", ["region", "Phone"], [["华东", 508.5]])
    _, entry = bundle_of(snap, column="Phone")
    assert entry.get("masked") is True and "b64" not in entry


@pytest.mark.parametrize("sql", [
    "SELECT region, SUM(spend) AS spend FROM members GROUP BY region HAVING",
    "SELEC region, spend FROM members",
    "",
])
def test_sql_that_does_not_parse_ships_only_its_id(sql):
    bundle, entry = bundle_of(snapshot(sql, ["region", "spend"], [["华东", 508.5]]))
    assert entry.get("masked") is True and "b64" not in entry
    assert not shown(bundle, 2)


# --------------------------------------------------------------------------
# 改过的、认不得的、查询时就遮过的：照现有规则
# --------------------------------------------------------------------------


def test_a_changed_aggregate_snapshot_ships_only_its_id():
    """服务端的快照文件被改过（哈希对不上）：里面的 SQL、列名都不可信，照旧只给 id、另标 mismatch。"""
    original = stored(REGIONS)[0]
    changed = stored(REGIONS)[1].replace("华东".encode(), "华北".encode())
    _, entry = bundle_of(REGIONS, raw=changed, artifact=original)
    assert entry == {"id": original, "kind": "query", "node_id": "members", "masked": True, "mismatch": True}


def test_an_unreadable_snapshot_still_counts_as_masked():
    _, entry = bundle_of(REGIONS, raw=b"{not json")
    assert entry.get("masked") is True and entry.get("unparsable") is True and "b64" not in entry


def test_a_snapshot_with_keys_the_bundle_does_not_know_ships_only_its_id():
    """快照里有不认识的键（比如以后核查 agent 在查询时就遮过、另记下了遮了哪些列）：不知道它记着什么，照旧只给哈希。"""
    snap = {**REGIONS, "masked_at_query": ["phone"]}
    _, entry = bundle_of(snap)
    assert entry.get("masked") is True and "b64" not in entry


@pytest.mark.parametrize("merged", [
    # 合并查询节点落的样子：另记着各输入和来历（inputs、sources 这几个键本身就不在认得的范围里）
    {"columns": ["region", "spend"], "rows": [["华东", 508.5]], "source": MERGE_SOURCE, "sources": ["crm"],
     "sql": AGGREGATE, "mask_columns": ["phone"]},
    # 只剩数据源查询工具也会落的键：单凭来源是合并查询就不认
    {"columns": ["region", "spend"], "rows": [["华东", 508.5]], "row_count": 1, "truncated": False,
     "source": MERGE_SOURCE, "sql": AGGREGATE, "mask_columns": ["phone"]},
])
def test_a_merged_snapshot_from_a_masked_source_ships_only_its_id(merged):
    """合并查询的 SQL 查的是各输入的结果在内存里落成的表，遮罩要按各输入的来历算，这里不认：照旧只给哈希。"""
    _, entry = bundle_of(merged)
    assert entry.get("masked") is True and "b64" not in entry


def test_an_error_while_judging_ships_only_the_id(monkeypatch):
    """判断过程出错（解析器、认遮罩列的那一套抛了意外）：按碰了遮罩列算，只给哈希。"""
    from app.engine import inquiry

    def broken(*_args, **_kwargs):
        raise RuntimeError("解析器坏了")

    monkeypatch.setattr(inquiry, "_derived_hidden", broken)
    _, entry = bundle_of(REGIONS)
    assert entry.get("masked") is True and "b64" not in entry


def test_derived_masked_columns_are_found_the_way_inquiry_finds_them(monkeypatch):
    """按 SQL 认遮罩列用的是 inquiry._derived_hidden（双盲复算、质询同一套）：它认出结果列取自遮罩列，就只给哈希。"""
    from app.engine import inquiry

    calls = []

    def flags_spend(snap, hidden, kind):
        calls.append(kind)
        return set(hidden) | {"spend"}, set(hidden)

    monkeypatch.setattr(inquiry, "_derived_hidden", flags_spend)
    _, entry = bundle_of(REGIONS)
    assert entry.get("masked") is True and "b64" not in entry
    assert calls
