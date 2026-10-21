"""可验证报告的构建（bundle/build.py）：纯函数，不碰数据库。

拿一份手搭的已封存运行（事件、报告文档、口径卡、两份查询快照，其中一份带遮罩列）核对：

- 每条封存范围内的事件都带 data_sha256，按浏览器的拼法（seq|type|node_id|data_sha256 按 seq 排、换行连接）
  重算出来的清单哈希等于 runner 记下的那个；
- 只有白名单里的事件带 canonical（逐字等于 canonical_json(data)），其余只给哈希；运行里有带遮罩的快照时，
  成果节点和运行结束事件的预览可能带出查询结果，也只给哈希。认不出快照的查询（升级前的运行从工具快照里认，
  协作团队成员的查询认不出）按带遮罩算，查询失败的原话不算；
- 报告文档、它引用到的口径卡和查询快照按原字节嵌入；带遮罩的快照只给 id；没被引用的工件不嵌；
- 封存之后追加的事件单列，不进清单；
- 页面零外链，数据里的 </script> 截不断数据块；封存哈希、运行 ID、工作流名不执行脚本也写在页面上；
- scripts/check-bundle.mjs 在浏览器里打开的样本页（tests/bundle_samples.py）封存哈希是真算的、形状如预期。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import quote

import pytest

from app.bundle.build import (
    SCHEMA, attachment_name, build_bundle, bundle_json, content_disposition, display_name, render_html,
)
from app.core.artifact_store import canonical_json, content_hash, manifest_hash


def stored(obj) -> tuple[str, bytes]:
    """照工件库的样子落一份：规范 JSON 的字节，id 是它的 sha256。"""
    raw = canonical_json(obj).encode()
    return content_hash(raw), raw


SNAP_OPEN = {"columns": ["store", "amount"], "rows": [["青石店", 1280.5], ["白沙店", "960.00"]],
             "column_types": {"store": "text", "amount": "number"}, "source": "shop",
             "sql": "SELECT store, amount FROM orders", "truncated": False}
SNAP_MASKED = {"columns": ["name", "phone", "spend"], "rows": [["林一", "000-0001", 320.0]],
               "source": "crm", "sql": "SELECT name, phone, spend FROM members", "mask_columns": ["phone"]}
SNAP_UNCITED = {"columns": ["n"], "rows": [[7]], "source": "shop", "sql": "SELECT 7 AS n"}
RETRIEVAL = {"hits": [{"content": "退款按发生周统计"}]}
OPEN_ID, OPEN_RAW = stored(SNAP_OPEN)
MASKED_ID, MASKED_RAW = stored(SNAP_MASKED)
UNCITED_ID, UNCITED_RAW = stored(SNAP_UNCITED)
RETRIEVAL_ID, RETRIEVAL_RAW = stored(RETRIEVAL)
CARD = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2",
        "metrics": [{"id": "gmv", "name": "销售额", "value": 1280.5, "unit": "元", "decimals": 1}]}
CARD_ID, CARD_RAW = stored(CARD)


def cite(alias, kind, value, **locator):
    return {"alias": alias, "kind": kind, "locator": locator, "role": "value", "status": "resolved",
            "value": value, "rendered": str(value)}


def doc_of(run_id="r1", node_id="write", extra_catalog=None):
    return {
        "schema": "agentlab.report/1", "run_id": run_id, "node_id": node_id, "markdown": "…",
        "catalog": {
            "m:gmv": {"alias": "m:gmv", "kind": "metric", "artifact": CARD_ID, "node_id": "card",
                      "locator": {"metric": "gmv"}, "value": 1280.5},
            "Q1": {"alias": "Q1", "kind": "query", "artifact": OPEN_ID, "node_id": "fetch"},
            "Q2": {"alias": "Q2", "kind": "query", "artifact": MASKED_ID, "node_id": "members"},
            "Q3": {"alias": "Q3", "kind": "query", "artifact": UNCITED_ID, "node_id": "fetch"},
            "K1": {"alias": "K1", "kind": "retrieval", "artifact": RETRIEVAL_ID, "node_id": "docs"},
            "i:week": {"alias": "i:week", "kind": "input", "value": "2026-W37", "locator": {"field": "week"}},
            **(extra_catalog or {}),
        },
        "blocks": [{"id": "b0", "type": "paragraph", "units": [{
            "id": "u0", "kind": "claim", "cites": ["m:gmv", "Q1", "Q2", "K1"],
            "see": [{"alias": "K1", "kind": "retrieval", "role": "support", "status": "resolved"}],
            "segments": [
                {"id": "s0", "kind": "number", "text": "1,280.5元", "state": "deterministic",
                 "cite": cite("m:gmv", "metric", 1280.5, metric="gmv")},
                {"id": "s1", "kind": "number", "text": "960", "state": "deterministic",
                 "cite": cite("Q1", "cell", 960, row=1, column="amount")},
                {"id": "s2", "kind": "number", "text": "320", "state": "deterministic",
                 "cite": cite("Q2", "cell", 320.0, row=0, column="spend")},
                {"id": "s3", "kind": "number", "text": "42", "state": "none", "issue": "uncited_number"},
            ]}]}],
    }


DOC = doc_of()
DOC_ID, DOC_RAW = stored(DOC)
STORE = {DOC_ID: DOC_RAW, CARD_ID: CARD_RAW, OPEN_ID: OPEN_RAW, MASKED_ID: MASKED_RAW,
         UNCITED_ID: UNCITED_RAW, RETRIEVAL_ID: RETRIEVAL_RAW}

RUN = {"id": "r1", "workflow_id": "wf1", "workflow_name": "门店周报", "version": 3, "version_hash": "ab" * 32,
       "run_class": "formal", "status": "succeeded",
       "started_at": "2026-09-07T01:00:00+00:00", "finished_at": "2026-09-07T01:02:03+00:00"}
GRAPH = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
    ("start", "input", "开始"), ("fetch", "tool", "门店流水"), ("members", "tool", "会员消费"),
    ("docs", "retrieve", "口径说明"), ("card", "metrics", "周报口径"), ("write", "report", "写周报"),
    ("out", "output", "成果")]]}


def sample_events(doc_id=DOC_ID):
    return [
        (1, "run.started", None, {"nodes": 7, "memory_scope": "default"}),
        (2, "node.started", "start", {"label": "开始"}),
        (3, "node.finished", "start", {"preview": {"week": "2026-W37"}, "artifact": "f" * 64}),
        (4, "tool.end", "fetch", {"tool": "db_query__shop", "preview": "[[\"青石店\", 1280.5]]",
                                  "artifact": "e" * 64, "query_artifact": OPEN_ID}),
        (5, "node.finished", "fetch", {"preview": "{\"rows\": …}", "evidence": [
            {"kind": "query", "node_id": "fetch", "artifact": OPEN_ID}]}),
        (6, "tool.end", "members", {"tool": "db_query__crm", "preview": "[[\"林一\", \"000-0001\"]]",
                                    "query_artifact": MASKED_ID}),
        (7, "tool.end", "docs", {"tool": "web_search", "preview": "外部结果"}),
        (8, "node.finished", "card", {"preview": {"kind": "metric_set"}, "evidence": [
            {"kind": "metric_set", "node_id": "card", "artifact": CARD_ID}]}),
        (9, "caliber.upgrade", "card", {"caliber_node": "card", "pinned": "v2", "latest": "v3", "policy": "pin"}),
        (10, "llm.end", "write", {"model": "mock", "output": "系统提示和草稿"}),
        (11, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
        (12, "node.finished", "write", {"preview": {"text": "青石店本周流水…", "doc_artifact": doc_id}}),
        (13, "issuance", "out", {"tier": "formal", "mode": "citations"}),
        (14, "node.finished", "out", {"preview": {"周报": "…", "明细": "[[\"林一\", \"000-0001\"]]"}}),
        (15, "run.finished", None, {"output": {"周报": "…"}}),
        # 封存之后追加：按需裁判的判定
        (16, "evidence.judged", "write", {"verdicts": {"u0": {"verdict": "supported"}}, "model": "judge-x",
                                          "post_seal": True}),
    ]


def build(events=None, store=None, source_masks=None, **kw):
    data = store if store is not None else STORE
    return build_bundle(RUN, events if events is not None else sample_events(), manifest_seq=kw.pop("seq", 15),
                        manifest_hash=kw.pop("manifest", "0" * 64), graph=GRAPH, source_masks=source_masks or {},
                        read=data.get, exported_at="2026-10-05T08:00:00+00:00", **kw)


def browser_manifest(events: list[dict]) -> str:
    """照浏览器里 verifier.js 的拼法重算：每行 seq|type|node_id|data_sha256，node_id 为空写空串，按 seq 排序。"""
    lines = [f"{e['seq']}|{e['type']}|{e['node_id'] or ''}|{e['data_sha256']}"
             for e in sorted(events, key=lambda e: e["seq"])]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# --------------------------------------------------------------------------
# 事件：哈希、清单、规范字符串
# --------------------------------------------------------------------------


def test_event_hashes_rebuild_the_runners_manifest():
    events = sample_events()
    bundle = build()
    assert bundle["schema"] == SCHEMA
    sealed = [e for e in events if e[0] <= 15]
    assert [e["seq"] for e in bundle["events"]] == [e[0] for e in sealed]
    for item, (seq, etype, node_id, data) in zip(bundle["events"], sealed):
        assert (item["seq"], item["type"], item["node_id"]) == (seq, etype, node_id)
        assert item["data_sha256"] == content_hash(canonical_json(data))
    assert browser_manifest(bundle["events"]) == manifest_hash(sealed)


def test_only_whitelisted_events_carry_their_canonical_text():
    events = {e[0]: e for e in sample_events()}
    bundle = build()
    shown = {e["seq"]: e["canonical"] for e in bundle["events"] if "canonical" in e}
    # run.started、无遮罩的查询 tool.end、口径卡和报告的 node.finished、升版处置、报告自查、出具判定
    assert sorted(shown) == [1, 4, 8, 9, 11, 12, 13], sorted(shown)
    for seq, text in shown.items():
        assert text == canonical_json(events[seq][3])
        assert hashlib.sha256(text.encode()).hexdigest() == next(
            e["data_sha256"] for e in bundle["events"] if e["seq"] == seq)


def test_output_and_finish_show_content_only_when_nothing_is_masked():
    """成果字段可以直接引用查询结果：运行里有带遮罩的快照时，成果节点和运行结束事件只给哈希。"""
    unmasked = {**SNAP_MASKED}
    unmasked.pop("mask_columns")
    plain_id, plain_raw = stored(unmasked)
    doc = doc_of(extra_catalog={"Q2": {"alias": "Q2", "kind": "query", "artifact": plain_id, "node_id": "members"}})
    doc_id, doc_raw = stored(doc)
    events = [(s, t, n, {**d, "query_artifact": plain_id} if s == 6 else d) for s, t, n, d in sample_events(doc_id)]
    store = {**STORE, plain_id: plain_raw, doc_id: doc_raw}

    bundle = build(events, store)
    shown = sorted(e["seq"] for e in bundle["events"] if "canonical" in e)
    assert shown == [1, 4, 6, 8, 9, 11, 12, 13, 14, 15], shown


def test_masks_set_on_the_source_now_count_too():
    """快照查询当时没记遮罩、数据源事后设了遮罩：照样算带遮罩——证据面板也按两者之和遮。"""
    bundle = build(source_masks={"shop": ["store"]})
    assert 4 not in {e["seq"] for e in bundle["events"] if "canonical" in e}
    entry = next(a for a in bundle["artifacts"] if a["id"] == OPEN_ID)
    assert entry["masked"] is True and "b64" not in entry


def test_an_unreadable_snapshot_counts_as_masked():
    """读不出来的快照不知道带没带遮罩：它的查询事件只给哈希，成果和运行结束事件也一样。"""
    store = {k: v for k, v in STORE.items() if k != OPEN_ID}
    bundle = build(store=store)
    shown = {e["seq"] for e in bundle["events"] if "canonical" in e}
    assert 4 not in shown and 14 not in shown and 15 not in shown
    entry = next(a for a in bundle["artifacts"] if a["id"] == OPEN_ID)
    assert entry["missing"] is True and "b64" not in entry


# 没有遮罩的报告、口径卡：成果和运行结束事件本该带规范字符串，下面几种查询让它们只给哈希
PLAIN_DOC = {**doc_of(), "catalog": {k: v for k, v in doc_of()["catalog"].items() if k != "Q2"}}
PLAIN_DOC_ID, PLAIN_DOC_RAW = stored(PLAIN_DOC)
TOOL_SNAP_CRM = {"tool": "db_query__crm", "args": {"sql": SNAP_MASKED["sql"]},
                 "result": json.dumps({**{k: v for k, v in SNAP_MASKED.items() if k != "mask_columns"},
                                       "artifact": MASKED_ID}, ensure_ascii=False)}
TOOL_SNAP_CRM_ID, TOOL_SNAP_CRM_RAW = stored(TOOL_SNAP_CRM)


def plain_events(members_end):
    """报告只引用门店流水（没有遮罩）的运行；第 6 条是会员库那次查询，形状由调用方给。"""
    events = []
    for seq, etype, node_id, data in sample_events(PLAIN_DOC_ID):
        if seq == 6:
            data = members_end
        events.append((seq, etype, node_id, data))
    return events


def shown_seqs(bundle) -> set[int]:
    return {e["seq"] for e in bundle["events"] if "canonical" in e}


def test_a_run_without_masked_queries_shows_its_output():
    """对照组：会员库那次查询有快照、没有遮罩，成果节点（14）和运行结束事件（15）带规范字符串。"""
    unmasked = {k: v for k, v in SNAP_MASKED.items() if k != "mask_columns"}
    plain_id, plain_raw = stored(unmasked)
    events = plain_events({"tool": "db_query__crm", "preview": "[[\"林一\"]]", "query_artifact": plain_id})
    bundle = build(events, {**STORE, PLAIN_DOC_ID: PLAIN_DOC_RAW, plain_id: plain_raw})
    assert {14, 15} <= shown_seqs(bundle)


def test_a_pre_ledger_query_is_traced_through_its_tool_snapshot():
    """升级前的运行：tool.end 只有工具快照（artifact），查询快照的 id 嵌在工具快照的返回里。照证据接口
    （_legacy_pool）的办法从工具快照认出查询快照：它带遮罩，成果和运行结束事件只给哈希。"""
    events = plain_events({"tool": "db_query__crm", "preview": "{\"rows\": [[\"林一\", \"000-0001\"…",
                           "artifact": TOOL_SNAP_CRM_ID})
    bundle = build(events, {**STORE, PLAIN_DOC_ID: PLAIN_DOC_RAW, TOOL_SNAP_CRM_ID: TOOL_SNAP_CRM_RAW})
    assert not {6, 14, 15} & shown_seqs(bundle)
    assert "000-0001" not in json.dumps(bundle, ensure_ascii=False)


def test_a_query_that_cannot_be_traced_counts_as_masked():
    """协作团队成员的查询：tool.end 既没有查询快照也没有工具快照，预览是截断的返回，认不出查的是什么。
    宁可多挡：按带遮罩算，成果和运行结束事件只给哈希。"""
    events = plain_events({"tool": "db_query__crm", "agent": "分析员", "call_id": "c1",
                           "preview": "{\"columns\": [\"name\", \"phone\"], \"rows\": [[\"林一\", \"000-0001\"]"})
    events[13] = (14, "node.finished", "out", {"preview": {"明细": "林一 000-0001"}})
    events[14] = (15, "run.finished", None, {"output": {"明细": "林一 000-0001"}})
    bundle = build(events, {**STORE, PLAIN_DOC_ID: PLAIN_DOC_RAW})
    assert not {6, 14, 15} & shown_seqs(bundle)
    assert "000-0001" not in json.dumps(bundle, ensure_ascii=False)


def test_a_failed_query_returned_no_data():
    """查询失败、SQL 被拒：返回的是一句原话，没有数据，不因此挡住成果。"""
    events = plain_events({"tool": "db_query__crm", "preview": "查询失败：no such table: members_x",
                           "call_id": "c2"})
    bundle = build(events, {**STORE, PLAIN_DOC_ID: PLAIN_DOC_RAW})
    assert {14, 15} <= shown_seqs(bundle)


def test_a_source_masked_now_hides_queries_without_a_snapshot():
    """数据源现在设了遮罩：不必认出快照，按工具名就知道这次查询可能带着遮罩列。"""
    events = plain_events({"tool": "db_query__crm", "preview": "查询失败：timeout"})
    bundle = build(events, {**STORE, PLAIN_DOC_ID: PLAIN_DOC_RAW}, source_masks={"crm": ["phone"]})
    assert not {14, 15} & shown_seqs(bundle)


# --------------------------------------------------------------------------
# 工件：只嵌报告文档和它引用到的口径卡、查询快照
# --------------------------------------------------------------------------


def test_report_doc_and_what_it_cites_are_embedded_byte_for_byte():
    bundle = build()
    by_id = {a["id"]: a for a in bundle["artifacts"]}
    assert [a["id"] for a in bundle["artifacts"]] == [DOC_ID, CARD_ID, OPEN_ID, MASKED_ID]
    assert by_id[DOC_ID]["kind"] == "report_doc" and by_id[DOC_ID]["node_id"] == "write"
    assert by_id[CARD_ID]["kind"] == "metric_set" and by_id[CARD_ID]["node_id"] == "card"
    assert by_id[OPEN_ID]["kind"] == "query" and by_id[OPEN_ID]["node_id"] == "fetch"
    for art in (DOC_ID, CARD_ID, OPEN_ID):
        assert base64.b64decode(by_id[art]["b64"]) == STORE[art]
    assert bundle["reports"] == [{"node_id": "write", "doc_artifact": DOC_ID, "seq": 11}]


def test_a_masked_snapshot_ships_only_its_id():
    entry = next(a for a in build()["artifacts"] if a["id"] == MASKED_ID)
    assert entry == {"id": MASKED_ID, "kind": "query", "node_id": "members", "masked": True}
    assert "000-0001" not in render_html(build())


def test_artifacts_the_report_does_not_cite_stay_out():
    """目录里有、正文没引用的查询（Q3），检索快照，节点产出、工具快照：都不嵌。"""
    ids = {a["id"] for a in build()["artifacts"]}
    assert UNCITED_ID not in ids and RETRIEVAL_ID not in ids
    assert "f" * 64 not in ids and "e" * 64 not in ids


def test_the_latest_report_checked_of_a_node_wins():
    """同一个报告节点自查了两次（重写过）：和证据接口一样认最后一次。"""
    old_id, old_raw = stored({**DOC, "markdown": "旧稿"})
    events = sample_events()
    events.insert(10, (10, "report.checked", "write", {"doc_artifact": old_id, "ok": False}))
    events = [(i + 1, t, n, d) for i, (_, t, n, d) in enumerate(events)]
    bundle = build(events, {**STORE, old_id: old_raw}, seq=16)
    assert bundle["reports"] == [{"node_id": "write", "doc_artifact": DOC_ID, "seq": 12}]
    assert old_id not in {a["id"] for a in bundle["artifacts"]}


def test_a_doc_from_another_run_is_embedded_but_not_followed():
    """文档记的运行不是这一次：照实嵌进去让浏览器标出来，但它的目录不可信，不顺着它找工件。"""
    stray = doc_of(run_id="other")
    stray_id, stray_raw = stored(stray)
    bundle = build(sample_events(stray_id), {**STORE, stray_id: stray_raw})
    assert [a["id"] for a in bundle["artifacts"]] == [stray_id]


def test_a_missing_or_changed_artifact_is_listed_for_the_browser_to_judge():
    """服务端的文件不在了：只给 id 并标明；被改过的：原样嵌进去，浏览器自己算出对不上。"""
    changed = CARD_RAW.replace(b"1280.5", b"1999.5")
    bundle = build(store={**STORE, CARD_ID: changed, OPEN_ID: None})
    by_id = {a["id"]: a for a in bundle["artifacts"]}
    assert base64.b64decode(by_id[CARD_ID]["b64"]) == changed
    assert by_id[OPEN_ID] == {"id": OPEN_ID, "kind": "query", "node_id": "fetch", "missing": True}


# --------------------------------------------------------------------------
# 元信息、封存之后追加的事件
# --------------------------------------------------------------------------


def test_meta_names_the_seal_and_the_run():
    bundle = build(manifest="ab" * 32)
    meta = bundle["meta"]
    assert meta["run_id"] == "r1" and meta["workflow_name"] == "门店周报" and meta["workflow_id"] == "wf1"
    assert meta["version"] == 3 and meta["run_class"] == "formal"
    assert meta["manifest_seq"] == 15 and meta["manifest_hash"] == "ab" * 32 and meta["legacy"] is False
    assert meta["started_at"] == RUN["started_at"] and meta["finished_at"] == RUN["finished_at"]
    assert meta["exported_at"] == "2026-10-05T08:00:00+00:00"
    # 同是 cancelled 的被驳回和手动停止靠 end_reason 分开；没有的（成功、失败、老运行）是 None
    assert meta["end_reason"] is None
    rejected = build_bundle({**RUN, "status": "cancelled", "end_reason": "rejected"}, sample_events(), manifest_seq=15,
                            manifest_hash="ab" * 32, graph=GRAPH, read=STORE.get, exported_at="x")
    assert (rejected["meta"]["status"], rejected["meta"]["end_reason"]) == ("cancelled", "rejected")
    # 出具档位取封存范围内最后一条出具判定事件，带上序号，浏览器拿已核对的事件对照
    assert meta["tier"] == {"value": "formal", "seq": 13}
    assert bundle["nodes"]["write"] == {"label": "写周报", "type": "report"}


def test_events_after_the_seal_are_listed_apart():
    bundle = build()
    assert 16 not in {e["seq"] for e in bundle["events"]}
    [later] = bundle["appended"]
    assert later["seq"] == 16 and later["type"] == "evidence.judged" and later["node_id"] == "write"
    assert "1 句" in later["summary"] and "judge-x" in later["summary"]
    assert "canonical" not in later and "data_sha256" not in later


def test_display_name_and_attachment_name():
    assert display_name({"workflow_id": None, "workflow_name": "临时图"}) == "未保存的工作流"
    assert display_name({"workflow_id": None, "workflow_name": ""}) == "未保存的工作流"
    assert display_name({"workflow_id": "w", "workflow_name": "门店周报"}) == "门店周报"
    assert attachment_name({"workflow_id": "w", "workflow_name": "门店/周报: v2?"}) == "门店_周报_ v2_-可验证报告.html"
    assert attachment_name({"workflow_id": None, "workflow_name": ""}) == "未保存的工作流-可验证报告.html"
    header = content_disposition("门店周报-可验证报告.html", run_id="0123456789abcdef")
    assert header.startswith("attachment; ")
    assert f"filename*=UTF-8''{quote('门店周报-可验证报告.html', safe='')}" in header
    # 不认 filename* 的老客户端拿到一个纯 ASCII 的名字
    fallback = re.search(r'filename="([^"]+)"', header).group(1)
    assert fallback.isascii() and "01234567" in fallback


# --------------------------------------------------------------------------
# 页面：自包含、数据块截不断
# --------------------------------------------------------------------------


def data_block(html: str) -> str:
    m = re.search(r'<script type="application/json" id="bundle-data">(.*?)</script>', html, re.S)
    assert m, "页面里没有数据块"
    return m.group(1)


def test_the_page_has_no_external_links():
    html = render_html(build())
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert not re.search(r"https?://", html), re.search(r".{0,40}https?://.{0,40}", html).group(0)
    assert not re.search(r"<script[^>]*\ssrc\s*=", html, re.I)
    assert not re.search(r"<(?:link|img|iframe|object|embed|audio|video|source)\b", html, re.I)
    assert not re.search(r"url\s*\(|@import", html, re.I)
    # 内容安全策略：默认什么都不许连，脚本只认内联的那一段核对脚本（按哈希）
    csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "default-src 'none'" in csp
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp


def test_data_cannot_break_out_of_its_script_tag():
    nasty = "</script><script>alert(1)</script><!-- & \u2028\u2029 >"
    bundle = build()
    bundle["meta"]["workflow_name"] = nasty
    bundle["events"][0]["canonical"] = nasty
    text = bundle_json(bundle)
    assert not re.search("[<>&\u2028\u2029]", text)
    assert json.loads(text) == bundle

    html = render_html(bundle)
    assert html.count("</script>") == 2          # 数据块和核对脚本各一个，再没有别的
    assert json.loads(data_block(html)) == bundle
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    assert "<script>" not in title and "&lt;/script&gt;" in title


def test_the_verifier_script_is_inline_and_cannot_close_early():
    html = render_html(build())
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    assert "</script" not in script.lower() and "<!--" not in script
    # 脚本在 <head> 里、排在数据块前面：它的注释里也不能出现「<script」，按标签找数据块的人才不会先找到它
    assert "<script" not in script.lower()
    assert html.index("<script>") < html.index('<script type="application/json" id="bundle-data">')
    assert "bundle-data" in script and "crypto.subtle" in script.replace("window.", "")


def text_of(html: str, element_id: str) -> str:
    m = re.search(rf'<(\w+)[^>]*\sid="{element_id}"[^>]*>(.*?)</\1>', html, re.S)
    assert m, f"页面里没有 #{element_id}"
    return re.sub(r"<[^>]+>", "", m.group(2))


def test_the_seal_and_run_are_written_without_script():
    """附件预览、快速查看常常不执行脚本：封存哈希、运行 ID、工作流名直接写在页面上，状态说清还没核对。"""
    nasty = {**RUN, "id": "r1<b>", "workflow_name": "门店<i>周报</i>"}
    bundle = build_bundle(nasty, sample_events(), manifest_seq=15, manifest_hash="ab" * 32, graph=GRAPH,
                          read=STORE.get, exported_at="2026-10-05T08:00:00+00:00")
    html = render_html(bundle)
    assert text_of(html, "seal-hash") == "ab" * 32
    assert "r1&lt;b&gt;" in text_of(html, "seal-note") and "<b>" not in text_of(html, "seal-note")
    assert text_of(html, "title") == "门店&lt;i&gt;周报&lt;/i&gt;"
    # 没执行脚本时总览写的是「尚未核对」，不是一直转着的「正在核对」（那句只在脚本开始执行后才显示）
    assert re.search(r'class="no-js">尚未核对：[^<]*需要在浏览器中打开本文件', html)
    assert re.search(r'class="js-only">正在核对', html)
    # 成功结束的运行没有「继续运行会重新封存」那一句；失败的有
    assert re.search(r'id="seal-failed"[^>]*\shidden', html)
    failed = render_html(build_bundle({**RUN, "status": "failed"}, sample_events(), manifest_seq=15,
                                      manifest_hash="ab" * 32, graph=GRAPH, read=STORE.get, exported_at="x"))
    assert not re.search(r'id="seal-failed"[^>]*\shidden', failed)
    assert "重新封存" in text_of(failed, "seal-failed")


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_seals_are_flagged(legacy):
    assert build(legacy=legacy)["meta"]["legacy"] is legacy


# --------------------------------------------------------------------------
# 浏览器检查用的样本页（tests/bundle_samples.py）
# --------------------------------------------------------------------------


def test_the_sample_pages_for_the_browser_check_are_sealed_for_real():
    """scripts/check-bundle.mjs 断网打开这些样本页，断言核对结果、再做篡改。pytest 不执行页面上的脚本，这里守住样本
    本身：封存哈希是对事件真算的（浏览器的拼法重算得出），各页的形状是 check-bundle 的断言预期的那样。"""
    from tests.bundle_samples import pages

    rendered = pages()
    got = {name: json.loads(data_block(page)) for name, page in rendered.items()}
    assert set(got) == {"base", "two-reports", "metric-only", "legacy", "failed", "rejected", "continued", "degraded",
                        "missing-doc", "missing-card", "nan", "stray", "masked-card", "masked-old-doc", "masked-source",
                        "formats", "formats-unit", "prose", "analysis"}
    for name, bundle in got.items():
        assert browser_manifest(bundle["events"]) == bundle["meta"]["manifest_hash"], name

    def segments(bundle, node_id="write"):
        doc_id = next(r["doc_artifact"] for r in bundle["reports"] if r["node_id"] == node_id)
        doc = json.loads(base64.b64decode(next(a["b64"] for a in bundle["artifacts"] if a["id"] == doc_id)))
        return [s for b in doc["blocks"] for u in b["units"] for s in u["segments"] if s["kind"] in ("number", "value")]

    base = got["base"]
    shapes = {(a["kind"], a.get("node_id")): {k for k in a if k in ("b64", "masked", "missing")} for a in base["artifacts"]}
    assert shapes == {("report_doc", "write"): {"b64"}, ("metric_set", "card"): {"b64"}, ("query", "fetch"): {"b64"},
                      ("query", "members"): {"masked"}}
    cited = segments(base)
    # 数字与取值超过折叠的条数（20）；没有出处的一个、引用遮罩快照的一个
    assert len(cited) == 21
    assert sum(1 for s in cited if not s.get("cite")) == 1 and sum(1 for s in cited if s["kind"] == "value") == 8
    assert [a["type"] for a in base["appended"]] == ["evidence.judged"]
    assert base["meta"]["tier"]["value"] == "formal" and base["meta"]["run_class"] == "formal"

    assert [(a["kind"], a.get("missing")) for a in got["missing-doc"]["artifacts"]] == [("report_doc", True)]
    assert next(a for a in got["missing-card"]["artifacts"] if a["kind"] == "metric_set").get("missing") is True
    assert got["legacy"]["meta"]["legacy"] is True and "run.finished" in [a["type"] for a in got["legacy"]["appended"]]
    assert [r["node_id"] for r in got["two-reports"]["reports"]] == ["write", "monthly"] and segments(got["two-reports"], "monthly")
    # 只引用指标的报告：门店流水没被正文引用，从口径卡的输入这一条路随文件附带（点开数字看得到 SQL 和被引用的行）；
    # 会员消费谁都没引用，不嵌。口径卡带着原式、代入式、输入和复算
    only = got["metric-only"]
    shapes = {(a["kind"], a.get("node_id")): {k for k in a if k in ("b64", "masked", "missing")}
              for a in only["artifacts"]}
    assert shapes == {("report_doc", "write"): {"b64"}, ("metric_set", "card"): {"b64"}, ("query", "fetch"): {"b64"}}
    card = json.loads(base64.b64decode(next(a["b64"] for a in only["artifacts"] if a["kind"] == "metric_set")))
    [gmv] = card["metrics"]
    from tests.bundle_samples import STORES

    assert gmv["expression"].startswith("cell(nodes.fetch, 0, 'amount')") and "1280.5" in gmv["substituted"]
    assert gmv["recompute_ok"] is True and len(gmv["inputs"]) == len(STORES)
    assert [s["text"] for s in segments(only)] and not [s for s in segments(only) if s.get("cite", {}).get("kind") == "cell"]
    failed = got["failed"]
    assert failed["meta"]["status"] == "failed" and failed["meta"]["tier"] is None
    rejected = got["rejected"]
    assert (rejected["meta"]["status"], rejected["meta"]["end_reason"]) == ("cancelled", "rejected")
    assert max(rejected["events"], key=lambda e: e["seq"])["type"] == "run.cancelled"
    assert re.search(r'id="seal-failed"[^>]*\shidden', rendered["rejected"]), "被驳回不是失败：不提重新封存"
    assert max(failed["events"], key=lambda e: e["seq"])["type"] == "run.failed"
    # 先失败、经继续运行跑到成功的早期运行：范围里有那条运行失败，之后运行又继续了，最后一条不是结束事件
    continued = got["continued"]
    sealed_types = [e["type"] for e in sorted(continued["events"], key=lambda e: e["seq"])]
    assert continued["meta"]["legacy"] is True and continued["meta"]["status"] == "succeeded"
    assert "run.failed" in sealed_types and sealed_types[sealed_types.index("run.failed") + 1] == "run.resumed"
    assert sealed_types[-1] == "node.finished" and "run.finished" in [a["type"] for a in continued["appended"]]
    issuances = [e["seq"] for e in got["degraded"]["events"] if e["type"] == "issuance"]
    assert len(issuances) == 2 and got["degraded"]["meta"]["tier"] == {"value": "degraded", "seq": issuances[-1]}
    assert [a["kind"] for a in got["stray"]["artifacts"]] == ["report_doc"]
    # 口径卡上有输入取自遮罩列：整张卡只给 id，页面里找不到那个号码。卡的节点完成事件（预览里逐条写着指标的值）只给
    # 哈希；普通的卡（base）照旧带规范字符串
    def card_event(bundle):
        return next(e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "card")

    card = next(a for a in got["masked-card"]["artifacts"] if a["kind"] == "metric_set")
    assert card.get("masked") is True and "b64" not in card
    assert "canonical" not in card_event(got["masked-card"]) and "canonical" in card_event(base)
    masked_doc = json.loads(base64.b64decode(next(a["b64"] for a in got["masked-card"]["artifacts"]
                                                  if a["kind"] == "report_doc")))
    assert {a for a, e in masked_doc["catalog"].items() if e.get("masked")} == {"m:contact", "m:phone_raw"}
    assert not any(phone in rendered["masked-card"] for phone in ("000-0001", "000-0002"))
    # 同一张卡、之前的报告撰写节点写的文档（证据目录里写着号码）：整份只给哈希，不附带它引用的工件，报告的节点完成
    # 事件只给哈希，报告自查事件照常带（引用链靠它认文档）
    old_doc = got["masked-old-doc"]
    assert [{k: v for k, v in a.items() if k != "id"} for a in old_doc["artifacts"]] == \
        [{"kind": "report_doc", "node_id": "write", "masked": True}]
    shown_types = {(e["type"], e["node_id"]) for e in old_doc["events"] if "canonical" in e}
    assert ("report.checked", "write") in shown_types and ("node.finished", "write") not in shown_types
    assert ("node.finished", "card") not in shown_types
    assert not any(phone in rendered["masked-old-doc"] for phone in ("000-0001", "000-0002"))
    # 会员库设了号码遮罩：按地区汇总的那份没碰号码列，随文件附带；选出了号码的那份只给 id，页面里找不到号码
    source = got["masked-source"]
    shapes = {(a["kind"], a.get("node_id")): {k for k in a if k in ("b64", "masked", "missing")}
              for a in source["artifacts"]}
    assert shapes == {("report_doc", "write"): {"b64"}, ("query", "regions"): {"b64"}, ("query", "members"): {"masked"}}
    assert [n for n in segments(source) if n.get("cite", {}).get("alias") == "Q3"]
    assert not any(phone in rendered["masked-source"] for phone in ("000-0001", "000-0002"))
    nan = got["nan"]
    assert "NaN" in next(e["canonical"] for e in nan["events"] if e["type"] == "run.started")
    assert b"NaN" in base64.b64decode(next(a["b64"] for a in nan["artifacts"] if a.get("node_id") == "fetch"))
    # 口径卡单位带尾随空白：服务端把「元 」原样接在数后面，正文里的销售额带着那个空白
    shown = {s.get("ref"): s["text"] for s in segments(got["formats-unit"])}
    assert shown["m:gmv"] == "74,613.65元 " and shown["m:gmv|万"] == "7.46万元 "
    # 异动周报：三份分析结论（导出时都复算一致）和两份输入快照随文件附带；文档带着系统写的分析范围，正文没引用的 A2、A3
    # 只在范围里点名；分析工具的 tool.end 结论随文件附带了，带规范字符串
    analysed = got["analysis"]
    kinds = sorted((a["kind"], a.get("node_id"), "b64" in a) for a in analysed["artifacts"])
    assert kinds == [("analysis_result", "bot", True)] * 3 + [("query", "bot", True)] * 2 + [("report_doc", "write", True)]
    assert {a["recheck"]["status"] for a in analysed["artifacts"] if a["kind"] == "analysis_result"} == {"same"}
    doc = json.loads(base64.b64decode(next(a["b64"] for a in analysed["artifacts"] if a["kind"] == "report_doc")))
    scope = doc["analysis_scope"]
    assert [[i["alias"] for i in scope[k]] for k in ("phenomena", "decomposed", "refused")] == [["A3"], ["A1"], ["A2"]]
    assert scope["not_decomposed"] == ["渠道"] and scope["decomposed"][0]["dims"] == ["地区"]
    assert {s["ref"] for s in segments(analysed) if s["ref"].startswith("a:")} == {"a:A1.top_share|pct"}
    assert all("canonical" in e for e in analysed["events"] if e["type"] == "tool.end")
