"""可验证报告里数据源设了遮罩的查询快照（期 5 前置 L2）：能证明这条查询没碰遮罩列的，快照照常随文件附带。

以前只要查询当时记下了数据源的遮罩列（mask_columns），或者数据源现在设了遮罩，整份快照就只给哈希——哪怕这条查询只按
地区汇总了金额。数据源里只要有一列设了遮罩（会员手机号），报告里引用查询格子的数字就全成了「无法核对」。

现在全部满足才嵌入（build._untouched）：认得这份快照（数据源查询工具落的那种、哈希对得上）；结果的列名对不上遮罩列；
SQL 原文里没写到遮罩列（连条件里也不行：SQL 随快照一起附带，WHERE phone = '…' 的字面量就是遮罩值）；SQL 解析得了，
没有认不出取自哪一列的写法（列名表、整行引用、可能展开出遮罩列的 *），按 SQL 认遮罩列的那一套（inquiry._derived_hidden）
也认不出哪个结果列取自遮罩列。任何一条不满足、判断过程出错，照旧只给哈希。

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
              artifact: str | None = None):
    """一次手搭的运行：会员库一次查询 → 报告引用快照里的一格（默认第 0 行的 spend）。raw 给了就把服务端的快照文件换成
    这份字节；artifact 给了就用它当快照的 id（文件被改过、哈希对不上）。"""
    snap_id, snap_raw = stored(snap)
    snap_id, snap_raw = artifact or snap_id, raw if raw is not None else snap_raw
    doc = {"schema": "agentlab.report/1", "run_id": RUN["id"], "node_id": "write", "markdown": "…",
           "catalog": {"Q1": {"alias": "Q1", "kind": "query", "artifact": snap_id, "node_id": "members"}},
           "blocks": [{"id": "b0", "type": "paragraph", "units": [{
               "id": "u0", "kind": "claim", "cites": ["Q1"],
               "segments": [{"id": "s0", "kind": "number", "text": str(value), "state": "deterministic",
                             "cite": cite(value, row, column)}]}]}]}
    doc_id, doc_raw = stored(doc)
    events = [
        (1, "run.started", None, {"nodes": 3}),
        (2, "tool.end", "members", {"tool": "db_query__crm", "query_artifact": snap_id,
                                    "preview": json.dumps(snap["rows"][:1], ensure_ascii=False)}),
        (3, "node.finished", "members", {"evidence": [{"kind": "query", "node_id": "members", "artifact": snap_id}]}),
        (4, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        (5, "node.finished", "write", {"preview": {"text": "…", "doc_artifact": doc_id}}),
        (6, "run.finished", None, {"output": {}}),
    ]
    files = {snap_id: snap_raw, doc_id: doc_raw}
    bundle = build_bundle(RUN, events, manifest_seq=6, manifest_hash="0" * 64, graph=GRAPH, source_masks=masks,
                          read=files.get, exported_at="2026-10-06T08:00:00+00:00")
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
    # 汇总里数遮罩列的个数：也算碰了（宁可多挡）
    ("SELECT region, COUNT(DISTINCT phone) AS spend FROM members GROUP BY region", ["region", "spend"]),
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
    # 列名表按位置给列改名（Postgres 的表别名、子查询、WITH 都能写）
    ("SELECT c AS spend FROM members AS m(a, b, c)", ["spend"]),
    ("WITH t(a, b) AS (SELECT * FROM members2) SELECT b AS spend FROM t", ["spend"]),
    # 语法树认不准数据库实际执行什么：MySQL 会执行的注释、Postgres 的 Unicode 转义名字
    ("SELECT region /*! , SUM(spend) */ AS spend FROM members GROUP BY region", ["region", "spend"]),
    ('SELECT U&"\\0073pend" AS spend FROM members', ["spend"]),
])
def test_shapes_that_could_hide_a_masked_column_ship_only_their_id(sql, columns):
    """SQL 里没写到遮罩列、结果的列名也对不上，可认不出结果里哪一列取自哪一列：照样只给哈希。"""
    bundle, entry = bundle_of(snapshot(sql, columns, [[508.5] * len(columns)]), column=columns[-1])
    assert entry.get("masked") is True and "b64" not in entry, sql


def test_result_columns_named_like_a_masked_column_ship_only_their_id():
    """结果的列名按数据库交回的认（大小写不论）：SQL 里看不出来，列名对上遮罩列也只给哈希。"""
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


def test_a_merged_snapshot_from_a_masked_source_ships_only_its_id():
    """合并查询的快照没有一条 SQL 可看（记着的是各输入和来历）：照旧只给哈希。"""
    merged = {"columns": ["region", "spend"], "rows": [["华东", 508.5]], "source": MERGE_SOURCE, "sources": ["crm"],
              "sql": AGGREGATE, "mask_columns": ["phone"]}
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
