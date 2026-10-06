"""可验证报告里的口径卡和遮罩（期 4 I2a 复核）：口径卡上记着每个输入的值和代入式，有输入取自遮罩列时整张卡只给 id。

以前只有查询快照会因遮罩不随文件附带，口径卡一律原样嵌入：证据面板上已经遮住的号码（cell() 取的遮罩格、按位置取的
vars.res.rows[0][1]），在这份要发给别人的文件里照样看得到。认哪个输入取自哪份查询结果的哪几列，和证据面板、推演同一个
认法（recompute.input_origin）；核对不了的宁可多挡。那几列算不算遮罩列，除了列名对上的，SQL 里给遮罩列起了别名、套了函数、
经子查询或 WITH 转了一道的也算（同质询、双盲复算的 inquiry._derived_hidden）：卡上取的 tel 可能就是 phone AS tel。

服务端的卡文件被改过（哈希对不上）也照卡上记的输入认：嵌进去的是原字节，号码照样在里面。读得出字节、却解析不出一张卡
（截断的 JSON、不是对象）的，认不出输入，整张按带遮罩算、不嵌：和读不出来的快照按带遮罩算是同一个道理。

不嵌的工件，服务端把它知道的情况照实带上：自己的文件和工件 id（哈希）对不上的标 mismatch，解析不出来的标 unparsable，
核对页据此判不通过、照实说原因（以前原字节嵌进去，浏览器自己算出对不上；现在不嵌了，这个证据不能跟着丢）。带遮罩的
查询快照同样标。

工件、事件都是手搭的（会员、号码都是编的），照 tests/test_bundle_build.py 的样子。
"""
from __future__ import annotations

import base64

import pytest

from app.bundle.build import build_bundle, render_html
from app.core.artifact_store import content_hash
from tests.test_bundle_build import GRAPH, MASKED_ID, RUN, SNAP_MASKED, STORE, doc_of, stored

#: SNAP_MASKED 里的号码（phone 设了遮罩）
PHONE = SNAP_MASKED["rows"][0][1]
CELL = "cell(nodes.members, 0, 'phone')"


def card_with(inputs, substituted="len('000-0001')", expression=f"len({CELL})"):
    """会员号码位数一张卡：报告引用的是它（m:gmv 指过来）。"""
    return {"kind": "metric_set", "caliber": "会员口径", "caliber_version": "v1", "metrics": [
        {"id": "gmv", "name": "号码位数", "value": 1280.5, "unit": "元", "decimals": 1, "expression": expression,
         "substituted": substituted, "inputs": inputs}]}


def bundle_of(card, *, outputs=None, source_masks=None, tamper=None, rehash=False, files_changed=None):
    """一次手搭的运行：会员查询（带遮罩的快照）→ 数据整形（outputs 里的产出）→ 口径卡 → 报告。tamper 给了就把服务端的
    卡文件换成 tamper(原字节)，id 照旧（文件被改过、哈希对不上）；rehash 为真时 id 按换过的字节算（服务端存的就是这一份，
    哈希对得上）。files_changed 换掉服务端别的文件（{工件 id: 新字节}，id 照旧）。"""
    card_id, card_raw = stored(card)
    if tamper is not None:
        card_raw = tamper(card_raw)
    if rehash:
        card_id = content_hash(card_raw)
    doc = doc_of(extra_catalog={"m:gmv": {"alias": "m:gmv", "kind": "metric", "artifact": card_id, "node_id": "card",
                                          "locator": {"metric": "gmv"}, "value": 1280.5}})
    doc_id, doc_raw = stored(doc)
    files = {**STORE, card_id: card_raw, doc_id: doc_raw, **(files_changed or {})}
    events = [
        (1, "run.started", None, {"nodes": 7}),
        (2, "tool.end", "members", {"tool": "db_query__crm", "query_artifact": MASKED_ID}),
        (3, "node.finished", "members", {"evidence": [{"kind": "query", "node_id": "members", "artifact": MASKED_ID}]}),
    ]
    for node_id, content in (outputs or {}).items():
        artifact, raw = stored(content) if content is not None else ("ee" * 32, None)
        if raw is not None:
            files[artifact] = raw
        events.append((len(events) + 1, "node.finished", node_id, {"artifact": artifact}))
    events += [
        (len(events) + 1, "node.started", "card", {}),
        (len(events) + 2, "node.finished", "card", {"evidence": [{"kind": "metric_set", "node_id": "card",
                                                                  "artifact": card_id}]}),
        (len(events) + 3, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        (len(events) + 4, "run.finished", None, {"output": {}}),
    ]
    bundle = build_bundle(RUN, events, manifest_seq=len(events), manifest_hash="0" * 64, graph=GRAPH,
                          source_masks=source_masks or {}, read=files.get, exported_at="2026-10-06T08:00:00+00:00")
    entry = next(a for a in bundle["artifacts"] if a["id"] == card_id)
    return bundle, entry


def cell_input(column, value):
    return {"path": f"cell(nodes.members, 0, '{column}')", "value": value, "node_id": "members", "via": "tool_cell",
            "artifact": MASKED_ID, "locator": {"row": 0, "column": column}, "status": "verified"}


def test_a_card_reading_a_masked_cell_ships_only_its_id():
    bundle, entry = bundle_of(card_with([cell_input("phone", PHONE)]))
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True}
    assert PHONE not in render_html(bundle)


def test_a_card_reading_only_unmasked_columns_is_embedded():
    """同一份快照里没遮的列（消费额）：卡上没有遮罩列的值，照常嵌入、照常核对。"""
    card = card_with([cell_input("spend", 320.0)], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    _, entry = bundle_of(card)
    assert "masked" not in entry and base64.b64decode(entry["b64"]) == stored(card)[1]


@pytest.mark.parametrize("sql", [
    "SELECT name, phone AS tel, spend FROM members",
    "SELECT name, tel, spend FROM (SELECT name, phone AS tel, spend FROM members) t",
    "WITH c AS (SELECT name, SUBSTR(phone, 1) AS tel, spend FROM members) SELECT name, tel, spend FROM c",
])
def test_a_card_reading_a_renamed_masked_column_ships_only_its_id(sql):
    """查询给号码起了别名、经子查询或 WITH 转了一道：卡上取的 tel 那一格就是号码。改了名字的遮罩列按 SQL 认（同质询、
    双盲复算的 inquiry._derived_hidden），整张卡只给 id。以前只按列名认，号码原样进了文件。"""
    snap = {**SNAP_MASKED, "columns": ["name", "tel", "spend"], "sql": sql}
    snap_id, raw = stored(snap)
    item = {**cell_input("tel", PHONE), "artifact": snap_id, "path": "cell(nodes.members, 0, 'tel')"}
    bundle, entry = bundle_of(card_with([item], expression="len(cell(nodes.members, 0, 'tel'))"),
                              files_changed={snap_id: raw})
    assert entry.get("masked") is True and "b64" not in entry
    assert PHONE not in render_html(bundle)


@pytest.mark.parametrize("sql", [
    # 列名表按位置改了名
    "SELECT name, c AS tel, spend FROM members AS m(a, b, c)",
    # 整行引用：FROM 里的表函数不写 LATERAL 也看得见前面的来源，整行一列列摊开；直接把整行转成文字
    "SELECT name, j.value AS tel, spend FROM members m, jsonb_each_text(to_jsonb(m)) j",
    "SELECT name, to_json(m)::text AS tel, spend FROM members m",
    # UNION 一支里的 * 按位置对上第一支的列名
    "SELECT name, tel, spend FROM (SELECT 'x' AS name, 'y' AS tel, 0 AS spend UNION ALL SELECT * FROM members) t",
    # FROM 里的表函数吃进遮罩列，交出的列叫别的名字
    "SELECT name, value AS tel, spend FROM members m, json_each_text(json_build_object('p', phone)) j",
    # 不点列名也读得出表里内容；语法树认不准数据库实际执行什么
    "SELECT name, table_to_xml('members', true, false, '')::text AS tel, spend FROM members",
    "SELECT name, /*!50000 phone AS */ tel, spend FROM members",
    # 函数记法交出整行的文字写法；表函数看不见排在它后面的派生来源里叫 m 的列，m 还是整行
    "SELECT name, m.record_out AS tel, spend FROM members m",
    "SELECT name, value AS tel, spend FROM members m, jsonb_each_text(to_jsonb(m)), (SELECT 1 AS m) s",
])
def test_a_card_reading_a_column_whose_origin_the_sql_hides_ships_only_its_id(sql):
    """快照的 SQL 认不出结果里哪一列取自哪一列（快照本身因此只给哈希）：卡上取的 tel 可能就是号码，整份快照的列都按
    遮罩列算，整张卡只给 id。以前这条路只按列名和改了名字的遮罩列认，快照只给哈希，卡上记着的号码却原样进了文件。"""
    snap = {**SNAP_MASKED, "columns": ["name", "tel", "spend"], "sql": sql}
    snap_id, raw = stored(snap)
    item = {**cell_input("tel", PHONE), "artifact": snap_id, "path": "cell(nodes.members, 0, 'tel')"}
    bundle, entry = bundle_of(card_with([item], expression="len(cell(nodes.members, 0, 'tel'))"),
                              files_changed={snap_id: raw})
    assert entry.get("masked") is True and "b64" not in entry, sql
    assert PHONE not in render_html(bundle)


@pytest.mark.parametrize("sql", [
    # 每种读法都解析不了：PG17 起的 JSON_QUERY … WITH WRAPPER（这里把整行装进了 tel）、ORDER BY … USING
    "SELECT name, JSON_QUERY(to_jsonb(m), '$.*' WITH WRAPPER) AS tel, spend FROM members m",
    "SELECT name, value AS tel, spend FROM members m, jsonb_each_text(to_jsonb(m)) ORDER BY value USING <",
    "SELECT name, tel, spend FROM (SELECT * FROM members) AS x(name, tel, spend) ORDER BY spend USING <",
    # 没有 SQL（数据源查询工具落的快照都带着）、SQL 是空的
    None,
    "",
])
def test_a_card_reading_a_snapshot_whose_sql_does_not_parse_ships_only_its_id(sql):
    """快照的 SQL 每种读法都解析不了（或者根本没有）：快照本身只给哈希（_untouched），认不出结果里哪一列取自哪一列，
    卡上取自这份快照任一列的输入都按遮罩列算，整张卡只给 id——和快照同一个结论。以前这条路退回按 SQL 认遮罩列的那一套，
    解析不了时它只看 SQL 里写没写着 phone：没写就只认 phone 本身，卡上取的 tel 是整行、号码原样进了文件。"""
    snap = {**SNAP_MASKED, "columns": ["name", "tel", "spend"], "sql": sql}
    if sql is None:
        del snap["sql"]
    snap_id, raw = stored(snap)
    for column, value in (("tel", PHONE), ("spend", 320.0)):
        item = {**cell_input(column, value), "artifact": snap_id, "path": f"cell(nodes.members, 0, '{column}')"}
        bundle, entry = bundle_of(card_with([item], substituted=repr(value),
                                            expression=f"len(cell(nodes.members, 0, '{column}'))"),
                                  files_changed={snap_id: raw})
        assert entry.get("masked") is True and "b64" not in entry, (sql, column)
        assert PHONE not in render_html(bundle)


@pytest.mark.parametrize("sql, columns", [
    ("SELECT name, phone AS tel, spend FROM members", ["name", "tel", "spend"]),
    # 最外层的 * 照原来的名字展开：号码还在 phone 那一列
    ("SELECT * FROM members", ["name", "phone", "spend"]),
])
def test_a_card_reading_an_unrenamed_column_of_that_query_is_embedded(sql, columns):
    """同一条查询，卡上取的是消费额（不取自号码）：照常嵌入。"""
    snap = {**SNAP_MASKED, "columns": columns, "sql": sql}
    snap_id, raw = stored(snap)
    item = {**cell_input("spend", 320.0), "artifact": snap_id}
    card = card_with([item], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    _, entry = bundle_of(card, files_changed={snap_id: raw})
    assert "masked" not in entry and base64.b64decode(entry["b64"]) == stored(card)[1]


def test_an_error_while_finding_renamed_columns_masks_the_whole_snapshot(monkeypatch):
    """按 SQL 认改了名字的遮罩列时出了意外：认不出哪几列取自遮罩列，整份快照的列都按遮罩算，卡上取消费额也只给 id。"""
    from app.engine import inquiry

    def broken(*_args, **_kwargs):
        raise RuntimeError("解析器坏了")

    monkeypatch.setattr(inquiry, "_derived_hidden", broken)
    card = card_with([cell_input("spend", 320.0)], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    _, entry = bundle_of(card)
    assert entry.get("masked") is True and "b64" not in entry


def test_a_mask_set_on_the_source_now_counts_too():
    card = card_with([cell_input("spend", 320.0)], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    _, entry = bundle_of(card, source_masks={"crm": ["spend"]})
    assert entry.get("masked") is True and "b64" not in entry


PARSED = {"columns": SNAP_MASKED["columns"], "rows": SNAP_MASKED["rows"]}
POSITIONAL = "vars.res.rows[0][1]"


@pytest.mark.parametrize("parsed, masked", [
    # 数据整形原样解析的查询结果（带着快照的 id）：认得出取自 phone 列
    ({**PARSED, "artifact": MASKED_ID, "source": "crm"}, True),
    # 写变量的节点的产出读不出来：核对不了，宁可多挡
    (None, True),
    # 结果没存快照、只带着数据源名：查询当时记下的遮罩无从核对，宁可多挡
    ({**PARSED, "source": "crm"}, True),
    # 改过形状、既没带快照的 id 也没带数据源名：认不出出处，照常嵌入（和证据面板、推演一样，是已知的缺口）
    (PARSED, False),
])
def test_a_card_reading_a_cell_by_position_is_judged_the_same_way(parsed, masked):
    item = {"path": POSITIONAL, "value": PHONE, "node_id": "parse", "via": "transform", "field": "rows[0][1]",
            "status": "ok"}
    card = card_with([item], substituted=f"len('{PHONE}')", expression=f"len({POSITIONAL})")
    bundle, entry = bundle_of(card, outputs={"parse": parsed})
    assert (entry.get("masked") is True) is masked and ("b64" in entry) is not masked
    if masked:
        assert PHONE not in render_html(bundle)
    else:
        assert PHONE in base64.b64decode(entry["b64"]).decode()


def test_a_card_file_changed_on_the_server_is_still_judged_by_its_inputs():
    """服务端的卡文件被改过（这里改了口径版本，哈希对不上）：照样按卡上记的输入认遮罩，记着号码的只给 id。以前先看哈希、
    对不上就当卡上没有输入，原字节照样嵌进页面，号码进了文件。不嵌了，就另标 mismatch：核对页判这一项不通过，不和
    没被改过的遮罩卡一样说成正常的无法核对。"""
    card = card_with([cell_input("phone", PHONE)])
    bundle, entry = bundle_of(card, tamper=lambda raw: raw.replace(b'"caliber_version":"v1"', b'"caliber_version":"v9"'))
    assert entry["id"] == stored(card)[0]
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True, "mismatch": True}
    assert PHONE not in render_html(bundle)


def test_a_changed_card_without_masked_inputs_is_still_embedded_for_the_browser_to_judge():
    """改过的卡上没有遮罩列的值：照常原样嵌进去，浏览器自己算出哈希对不上（只是遮罩的判断不再看哈希）。"""
    card = card_with([cell_input("spend", 320.0)], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    changed = stored(card)[1].replace(b'"caliber_version":"v1"', b'"caliber_version":"v9"')
    _, entry = bundle_of(card, tamper=lambda raw: changed)
    assert "masked" not in entry and base64.b64decode(entry["b64"]) == changed


@pytest.mark.parametrize("damage", [
    # 截断的 JSON：号码还在已经写下的那半截里
    lambda raw: raw[: raw.index(b"000-0001") + len(b"000-0001")],
    # 解析得出、却不是一张卡（对象）
    lambda raw: b'["000-0001"]',
])
def test_a_card_file_that_does_not_parse_as_a_card_is_not_embedded(damage):
    """服务端的卡文件读得出字节、解析不出一张卡：认不出卡上有哪些输入，整张按带遮罩算、只给 id，不原样嵌进页面。字节和
    id 对不上（mismatch），也解析不出来（unparsable）：两样都照实标上。"""
    card = card_with([cell_input("phone", PHONE)])
    bundle, entry = bundle_of(card, tamper=damage)
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True, "mismatch": True,
                     "unparsable": True}
    assert PHONE not in render_html(bundle)


def test_a_card_stored_as_something_unparsable_is_marked_so():
    """服务端存的这一份本来就解析不出一张卡（哈希对得上，不是被改过）：整张按带遮罩算、不嵌，只标 unparsable——核对页照实
    说认不出是否含遮罩列，不说成「含遮罩列」，也不说被改过。"""
    card = card_with([cell_input("phone", PHONE)])
    bundle, entry = bundle_of(card, tamper=lambda raw: raw[: raw.index(b"000-0001") + len(b"000-0001")], rehash=True)
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True, "unparsable": True}
    assert PHONE not in render_html(bundle)


def test_a_masked_snapshot_changed_on_the_server_is_marked_so():
    """带遮罩的查询快照在服务端被改过（哈希对不上）：照旧只给 id，另标 mismatch，核对页判不通过。以前和没被改过的遮罩快照
    一模一样，总结论还是「可核对的项全部通过」。没被改过的照旧只标遮罩。"""
    card = card_with([cell_input("spend", 320.0)], substituted="320.0", expression="cell(nodes.members, 0, 'spend')")
    snapshot = lambda bundle: next(a for a in bundle["artifacts"] if a["id"] == MASKED_ID)  # noqa: E731
    bundle, _ = bundle_of(card)
    assert snapshot(bundle) == {"id": MASKED_ID, "kind": "query", "node_id": "members", "masked": True}
    changed = stored(SNAP_MASKED)[1].replace("林一".encode(), "林二".encode())
    bundle, _ = bundle_of(card, files_changed={MASKED_ID: changed})
    assert snapshot(bundle) == {"id": MASKED_ID, "kind": "query", "node_id": "members", "masked": True,
                                "mismatch": True}
    assert PHONE not in render_html(bundle)
