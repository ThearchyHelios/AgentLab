"""可验证报告的构建（bundle/build.py）：纯函数，不碰数据库。

拿一份手搭的已封存运行（事件、报告文档、口径卡、两份查询快照，其中一份带遮罩列）核对：

- 每条封存范围内的事件都带 data_sha256，按浏览器的拼法（seq|type|node_id|data_sha256 按 seq 排、换行连接）
  重算出来的清单哈希等于 runner 记下的那个；
- 只有白名单里的事件带 canonical（逐字等于 canonical_json(data)），其余只给哈希；运行里有带遮罩的快照时，
  成果节点和运行结束事件的预览可能带出查询结果，也只给哈希；
- 报告文档、它引用到的口径卡和查询快照按原字节嵌入；带遮罩的快照只给 id；没被引用的工件不嵌；
- 封存之后追加的事件单列，不进清单；
- 页面零外链，数据里的 </script> 截不断数据块。
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
    assert "bundle-data" in script and "crypto.subtle" in script.replace("window.", "")


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_seals_are_flagged(legacy):
    assert build(legacy=legacy)["meta"]["legacy"] is legacy
