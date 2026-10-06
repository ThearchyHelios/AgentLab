"""可验证报告里的口径卡和遮罩（期 4 I2a 复核）：口径卡上记着每个输入的值和代入式，有输入取自遮罩列时整张卡只给 id。

以前只有查询快照会因遮罩不随文件附带，口径卡一律原样嵌入：证据面板上已经遮住的号码（cell() 取的遮罩格、按位置取的
vars.res.rows[0][1]），在这份要发给别人的文件里照样看得到。认哪个输入取自哪份查询结果的哪几列，和证据面板、推演同一个
认法（recompute.input_origin）；核对不了的宁可多挡。

服务端的卡文件被改过（哈希对不上）也照卡上记的输入认：嵌进去的是原字节，号码照样在里面。读得出字节、却解析不出一张卡
（截断的 JSON、不是对象）的，认不出输入，整张按带遮罩算、不嵌：和读不出来的快照按带遮罩算是同一个道理。

工件、事件都是手搭的（会员、号码都是编的），照 tests/test_bundle_build.py 的样子。
"""
from __future__ import annotations

import base64

import pytest

from app.bundle.build import build_bundle, render_html
from tests.test_bundle_build import GRAPH, MASKED_ID, RUN, SNAP_MASKED, STORE, doc_of, stored

#: SNAP_MASKED 里的号码（phone 设了遮罩）
PHONE = SNAP_MASKED["rows"][0][1]
CELL = "cell(nodes.members, 0, 'phone')"


def card_with(inputs, substituted="len('000-0001')", expression=f"len({CELL})"):
    """会员号码位数一张卡：报告引用的是它（m:gmv 指过来）。"""
    return {"kind": "metric_set", "caliber": "会员口径", "caliber_version": "v1", "metrics": [
        {"id": "gmv", "name": "号码位数", "value": 1280.5, "unit": "元", "decimals": 1, "expression": expression,
         "substituted": substituted, "inputs": inputs}]}


def bundle_of(card, *, outputs=None, source_masks=None, tamper=None):
    """一次手搭的运行：会员查询（带遮罩的快照）→ 数据整形（outputs 里的产出）→ 口径卡 → 报告。tamper 给了就把服务端的
    卡文件换成 tamper(原字节)，id 照旧（文件被改过、哈希对不上）。"""
    card_id, card_raw = stored(card)
    if tamper is not None:
        card_raw = tamper(card_raw)
    doc = doc_of(extra_catalog={"m:gmv": {"alias": "m:gmv", "kind": "metric", "artifact": card_id, "node_id": "card",
                                          "locator": {"metric": "gmv"}, "value": 1280.5}})
    doc_id, doc_raw = stored(doc)
    files = {**STORE, card_id: card_raw, doc_id: doc_raw}
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
    对不上就当卡上没有输入，原字节照样嵌进页面，号码进了文件。"""
    card = card_with([cell_input("phone", PHONE)])
    bundle, entry = bundle_of(card, tamper=lambda raw: raw.replace(b'"caliber_version":"v1"', b'"caliber_version":"v9"'))
    assert entry["id"] == stored(card)[0]
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True}
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
    """服务端的卡文件读得出字节、解析不出一张卡：认不出卡上有哪些输入，整张按带遮罩算、只给 id，不原样嵌进页面。"""
    card = card_with([cell_input("phone", PHONE)])
    bundle, entry = bundle_of(card, tamper=damage)
    assert entry == {"id": entry["id"], "kind": "metric_set", "node_id": "card", "masked": True}
    assert PHONE not in render_html(bundle)
