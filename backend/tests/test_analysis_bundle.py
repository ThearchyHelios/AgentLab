"""分析结论进可验证报告（第 1 波 W1-b）：bundle/build.py 带上结论工件和它的输入快照，verifier.js 在 node 里核对。

- 报告引用的分析结论按原字节嵌入，连同输入快照；服务端导出时按封存的输入复算一遍（真的 app.analysis.recompute），结果记在
  工件条目上（recheck）
- 口径卡指标上的可比性结论（不进台账、只从卡上引用）随卡附带，④ 按「已核对的口径卡记着的可比性」认它
- 分析工具的 tool.end、分析卡一项的 analysis.end：结论随文件附带得了才带规范字符串
- 页面只核哈希、核判定语和数是不是结论里写的那几个字（不在浏览器里重算统计）：原样导出全部通过，只改显示文字的伪造
  点名不通过
- 输入快照只给 id（证明不了没碰遮罩列）的，结论本身也只给 id，引用它的判定语、数无法核对
- 报告文档里系统写的「分析范围」点名的结论也随文件附带
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.analysis.contract import ARTIFACT_KIND, ledger_entry
from app.bundle.build import build_bundle
from app.core.artifact_store import canonical_json, content_hash, manifest_hash
from app.engine.evidence import build_catalog, compose_doc, iter_segments
from tests.test_analysis_evidence import SNAPSHOT, analyzed, real_result, run_sync
from tests.test_analysis_evidence_api import LAST_WEEK, THIS_WEEK
from tests.test_bundle_shown import NODE, node, reseal

pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node，无法执行页面上的核对脚本")

RUN_ID = "7b1e0c3a5d9f4a2e8c6b4d2f0a1e3c5b"
RUN = {"id": RUN_ID, "workflow_id": "wf-analysis-weekly", "workflow_name": "异动周报", "version": 2,
       "version_hash": "cd" * 32, "run_class": "formal", "status": "succeeded",
       "started_at": "2026-10-12T01:00:00+00:00", "finished_at": "2026-10-12T01:01:00+00:00"}
GRAPH = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
    ("fetch", "tool", "按地区取数"), ("an", "agent", "分析"), ("write", "report", "写周报"), ("out", "output", "成果")]]}
TEXT = "## 异动\n\n[[a:A1]]。华东占 [[a:A1.top_share|pct]]，变化 [[v:A1.r0.delta]]。[[see:A1]]"


class Files(dict):
    def put(self, obj: Any) -> str:
        raw = canonical_json(obj).encode()
        sha = content_hash(raw)
        self[sha] = raw
        return sha

    def load(self, artifact: str) -> Any:
        raw = self.get(artifact)
        return json.loads(raw) if raw is not None else None


def bundle_of(text: str = TEXT, *, snapshot: dict[str, Any] | None = None, doc_extra: dict[str, Any] | None = None,
              source_masks: dict[str, list[str]] | None = None, graph: dict[str, Any] | None = None,
              extra_events: list[tuple[str, str | None, dict[str, Any]]] = (),
              drop: tuple[str, ...] = ()) -> tuple[dict[str, Any], Files, str]:
    """一次封存好的运行的页面数据：(bundle, 工件, 分析结论的 id)。结论按快照真算（app.analysis.run）；extra_events 接在
    分析节点完成之后（{analysis} 换成结论的 id），drop 是导出时服务端已经没有的工件（"query"：输入快照）。"""
    files = Files()
    snap = snapshot or SNAPSHOT
    q = files.put(snap)
    result = real_result(q, snap)
    a = files.put(result)
    qentry = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": q, "tool": "db_query__shop",
              "source": snap.get("source"), "columns": snap["columns"], "rows": len(snap["rows"]), "truncated": False}
    aentry = ledger_entry(result, node_id="an", exec_no=1, artifact=a, item="drop")
    catalog = build_catalog(nodes={}, ledger=[qentry, aentry], loader=files.load)
    doc = compose_doc(text, catalog, node_id="write", run_id=RUN_ID, loader=files.load)
    doc.update(doc_extra or {})
    doc_id = files.put(doc)
    events: list[tuple[int, str, str | None, Any]] = []

    def add(etype: str, node_id: str | None, data: dict[str, Any]) -> None:
        events.append((len(events) + 1, etype, node_id, data))

    add("run.started", None, {"nodes": 4, "resumed": False, "memory_scope": "default", "collection": "default",
                              "approval_default": None, "tool_trust": "formal", "agent_limits": {},
                              "data_versions": None, "replay_protocol": 2})
    add("tool.end", "fetch", {"tool": "db_query__shop", "duration_ms": 5, "query_artifact": q, "preview": "…"})
    add("node.finished", "fetch", {"duration_ms": 6, "attempt": 1, "evidence": [qentry]})
    add("node.finished", "an", {"duration_ms": 8, "attempt": 1, "evidence": [aentry]})
    for etype, node_id, data in extra_events:
        add(etype, node_id, json.loads(json.dumps(data).replace("{analysis}", a)))
    add("report.checked", "write", {"doc_artifact": doc_id, "ok": True})
    add("node.finished", "write", {"duration_ms": 9, "attempt": 1, "preview": {"text": "（报告正文）", "doc_artifact": doc_id}})
    add("issuance", "out", {"tier": "formal", "mode": "citations"})
    add("run.finished", None, {"output": {"周报": "（报告正文）"}})
    if "query" in drop:
        files.pop(q)
    bundle = build_bundle(RUN, events, manifest_seq=len(events), manifest_hash=manifest_hash(events), read=files.get,
                          exported_at="2026-10-21T08:00:00+00:00", graph=graph or GRAPH,
                          source_masks=source_masks or {})
    return bundle, files, a


def item(bundle: dict[str, Any], artifact: str) -> dict[str, Any]:
    return next(a for a in bundle["artifacts"] if a["id"] == artifact)


def verdicts(bundle: dict[str, Any]) -> dict[str, Any]:
    return node("verify", data=bundle, seal=bundle["meta"]["manifest_hash"])


def test_the_analysis_and_its_input_travel_with_the_file_and_verify():
    bundle, files, a = bundle_of()
    analysis_item = item(bundle, a)
    assert analysis_item["kind"] == ARTIFACT_KIND and "b64" in analysis_item and analysis_item["node_id"] == "an"
    # 导出时服务端真的复算了一遍：按封存的输入快照（这份文件里哈希对得上的原字节）重算，逐字段相同
    assert analysis_item["recheck"] == {"status": "same", "method": "decompose.additive@1",
                                        "text": "复算一致：按封存的输入重新计算，结论逐项相同"}
    # 方法的叫法由服务端带上（和导出页同一份），核对页只显示「变化拆解（可加） · decompose.additive@1」
    assert analysis_item["method_label"] == "变化拆解（可加）"
    [query] = [x for x in bundle["artifacts"] if x["kind"] == "query"]
    assert "b64" in query

    got = verdicts(bundle)
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain", "meta") for r in got[key]), got["why"]
    numbers = {n["ref"]: n for n in got["numbers"]}
    assert set(numbers) == {"a:A1", "a:A1.top_share|pct", "v:A1.r0.delta"}
    assert all(n["result"] == "pass" and n["expected"] == n["text"] for n in numbers.values()), numbers
    assert numbers["a:A1"]["text"] == "销售额下降 194（-8.7%），主要来自华东（-120，占 61.9%）"
    assert got["overall"]["result"] == "pass"


def test_an_analysis_whose_input_is_gone_travels_only_as_a_hash():
    """导出时服务端已经没有输入快照：证明不了没碰遮罩列，结论只给 id，也就不顺着它复算、不附带别的。"""
    bundle, _, a = bundle_of(drop=("query",))
    assert item(bundle, a) == {"id": a, "kind": ARTIFACT_KIND, "node_id": "an", "masked": True}


@pytest.mark.parametrize("ref, text", [("a:A1", "销售额下降 194（-8.7%），主要来自华南（-120，占 61.9%）"),
                                       ("a:A1.top_share|pct", "71.9%"), ("v:A1.r0.delta", "-220")])
def test_forging_the_shown_text_fails_by_name(ref, text):
    bundle, _, _ = bundle_of()

    def change(doc: dict[str, Any]) -> None:
        seg = next(s for s in iter_segments(doc) if s.get("ref") == ref)
        doc["markdown"] = doc["markdown"].replace(seg["text"], text, 1)
        seg["cite"]["rendered"] = seg["text"] = text

    art = next(x for x in bundle["artifacts"] if x["kind"] == "report_doc")
    doc = json.loads(base64.b64decode(art["b64"]))
    change(doc)
    got = verdicts(reseal(bundle, art, doc))
    forged = next(n for n in got["numbers"] if n["ref"] == ref)
    assert forged["result"] == "fail" and forged["reason"].startswith(f"正文显示「{text}」")


def test_an_analysis_on_a_masked_query_travels_only_as_a_hash():
    masked = {**SNAPSHOT, "columns": ["region", "phone", "week", "gmv"], "mask_columns": ["phone"],
              "sql": "SELECT region, phone, week, SUM(gmv) AS gmv FROM orders GROUP BY 1, 2, 3",
              "rows": [[r[0], "000-0001", r[1], r[2]] for r in SNAPSHOT["rows"]]}
    bundle, _, a = bundle_of(snapshot=masked)
    assert item(bundle, a) == {"id": a, "kind": ARTIFACT_KIND, "node_id": "an", "masked": True}
    got = verdicts(bundle)
    why = next(w for w in got["why"]["artifacts"] if w["kind"] == ARTIFACT_KIND)
    assert why["result"] == "na" and why["reason"].startswith("这份分析结论的输入查询用到或可能用到设了遮罩的列")
    assert all(n["result"] == "na" for n in got["numbers"])
    assert got["overall"]["result"] != "pass"


def test_analyses_named_only_in_the_scope_travel_too():
    scope = {"analysis_scope": {"phenomena": [{"alias": "A1", "op": "decompose", "phrase": "主要来自华东"}],
                                "decomposed": [], "excluded": [], "refused": [], "not_decomposed": None,
                                "note": "拆解用的列对不上目录，候选维度核对不了", "tests": 0}}
    bundle, _, a = bundle_of("本周销售额有变化。", doc_extra=scope)
    assert "b64" in item(bundle, a)
    got = verdicts(bundle)
    assert all(r == "pass" for r in got["chain"]), got["why"]["chain"]


def test_an_analysis_card_event_shows_its_content_only_when_its_conclusions_travel():
    """分析卡（节点类型 analysis）的节点完成事件：结论都随文件附带才带规范字符串；结论只给 id 的，这条也只给哈希。
    Agent 节点的照旧只给哈希。"""
    card = {"nodes": [{**n, "type": "analysis"} if n["id"] == "an" else n for n in GRAPH["nodes"]]}

    def shown(bundle: dict[str, Any]) -> bool:
        return "canonical" in next(e for e in bundle["events"] if e["type"] == "node.finished" and e["node_id"] == "an")

    assert shown(bundle_of(graph=card)[0])
    assert not shown(bundle_of()[0])
    masked = {**SNAPSHOT, "mask_columns": ["phone"], "columns": ["region", "phone", "week", "gmv"],
              "sql": "SELECT region, phone, week, SUM(gmv) AS gmv FROM orders GROUP BY 1, 2, 3",
              "rows": [[r[0], "000-0001", r[1], r[2]] for r in SNAPSHOT["rows"]]}
    assert not shown(bundle_of(graph=card, snapshot=masked)[0])
    got = verdicts(bundle_of(graph=card)[0])
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain") for r in got[key])


def test_analysis_tool_and_card_events_show_content_only_when_the_conclusion_travels():
    """分析工具的 tool.end（预览里是结论的前一截）、分析卡一项的 analysis.end（带判定语）：和结论随文件附带同一个认法。"""
    events = [("tool.end", "an", {"tool": "analysis_decompose", "duration_ms": 3, "analysis_artifact": "{analysis}",
                                  "preview": "{\"analysis_artifact\": \"{analysis}\"}"}),
              ("analysis.end", "an", {"item": "drop", "op": "decompose", "mode": "additive", "status": "ok",
                                      "verdict": "driver", "direction": "down", "phrase": "主要来自华东",
                                      "reason_code": None, "analysis_artifact": "{analysis}"})]

    def shown(bundle: dict[str, Any]) -> dict[str, bool]:
        return {e["type"]: "canonical" in e for e in bundle["events"] if e["type"] in ("tool.end", "analysis.end")
                and e["node_id"] == "an"}

    open_ = bundle_of(extra_events=events)[0]
    assert shown(open_) == {"tool.end": True, "analysis.end": True}
    got = verdicts(open_)
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain") for r in got[key]), got["why"]
    masked = {**SNAPSHOT, "mask_columns": ["phone"], "columns": ["region", "phone", "week", "gmv"],
              "sql": "SELECT region, phone, week, SUM(gmv) AS gmv FROM orders GROUP BY 1, 2, 3",
              "rows": [[r[0], "000-0001", r[1], r[2]] for r in SNAPSHOT["rows"]]}
    assert shown(bundle_of(snapshot=masked, extra_events=events)[0]) == {"tool.end": False, "analysis.end": False}


def card_bundle() -> tuple[dict[str, Any], Files, str, str]:
    """跨两周算环比的口径卡，指标上记着口径卡执行时自动做的可比性检查（结论落成工件，不进台账）：
    (bundle, 工件, 可比性结论的 id, 口径卡的 id)。"""
    files = Files()
    last, this = files.put(LAST_WEEK), files.put(THIS_WEEK)
    result = run_sync(analyzed({"fetch·base": (LAST_WEEK, last), "fetch·target": (THIS_WEEK, this)},
                               {"op": "comparable", "mode": None, "params": {}},
                               base_query="fetch·base", target_query="fetch·target"))
    found = files.put(result)
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "metrics": [
        {"id": "wow", "name": "环比", "value": -0.0874, "unit": "", "format": "percent_of_ratio", "decimals": 3,
         "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'gmv') - 1",
         "substituted": "2026 / 2220 - 1", "recompute_ok": True, "status": "ok",
         "inputs": [{"path": "cell(nodes.fetch, 0, 'gmv')", "value": 2026, "node_id": "fetch", "via": "tool_cell",
                     "artifact": this, "locator": {"row": 0, "column": "gmv"}},
                    {"path": "cell(nodes.fetch, 0, 'gmv')", "value": 2220, "node_id": "fetch", "via": "tool_cell",
                     "artifact": last, "locator": {"row": 0, "column": "gmv"}}],
         "comparability": {"artifact": found, "verdict": result["verdict"]["code"], "phrase": result["phrase"],
                           "reasons": []}}]}
    card_id = files.put(card)
    qentries = [{"kind": "query", "node_id": "fetch", "exec": 1, "artifact": x, "tool": "db_query__shop",
                 "source": "shop", "columns": ["gmv"], "rows": 1, "truncated": False} for x in (last, this)]
    mentry = {"kind": "metric_set", "node_id": "card", "exec": 1, "artifact": card_id}
    catalog = build_catalog(nodes={"card": {**card, "artifact": card_id}}, ledger=[*qentries, mentry],
                            loader=files.load)
    doc = compose_doc("## 环比\n\n销售额环比 [[m:wow]]。", catalog, node_id="write", run_id=RUN_ID, loader=files.load)
    doc_id = files.put(doc)
    events: list[tuple[int, str, str | None, Any]] = []

    def add(etype: str, node_id: str | None, data: dict[str, Any]) -> None:
        events.append((len(events) + 1, etype, node_id, data))

    add("run.started", None, {"nodes": 4, "resumed": False, "memory_scope": "default", "collection": "default",
                              "approval_default": None, "tool_trust": "formal", "agent_limits": {},
                              "data_versions": None, "replay_protocol": 2})
    for x in (last, this):
        add("tool.end", "fetch", {"tool": "db_query__shop", "duration_ms": 5, "query_artifact": x, "preview": "…"})
    add("node.finished", "fetch", {"duration_ms": 6, "attempt": 1, "evidence": qentries})
    add("node.finished", "card", {"duration_ms": 7, "attempt": 1, "evidence": [mentry]})
    add("report.checked", "write", {"doc_artifact": doc_id, "ok": True})
    add("node.finished", "write", {"duration_ms": 9, "attempt": 1, "preview": {"text": "（报告正文）", "doc_artifact": doc_id}})
    add("run.finished", None, {"output": {"周报": "（报告正文）"}})
    graph = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
        ("fetch", "tool", "按周取数"), ("card", "metrics", "周报口径"), ("write", "report", "写周报")]]}
    bundle = build_bundle(RUN, events, manifest_seq=len(events), manifest_hash=manifest_hash(events), read=files.get,
                          exported_at="2026-10-21T08:00:00+00:00", graph=graph, source_masks={})
    return bundle, files, found, card_id


def test_the_comparability_on_a_card_travels_with_the_card_and_is_chained_through_it():
    bundle, _, found, card_id = card_bundle()
    assert "b64" in item(bundle, card_id)
    comparability = item(bundle, found)
    assert comparability["kind"] == ARTIFACT_KIND and "b64" in comparability and comparability["node_id"] == "card"
    assert comparability["recheck"]["status"] == "same" and comparability["method_label"] == "可比性检查"
    got = verdicts(bundle)
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain") for r in got[key]), got["why"]
    assert {w["kind"] for w in got["why"]["chain"]} >= {"metric_set", ARTIFACT_KIND, "query"}
    [wow] = got["numbers"]
    assert wow["result"] == "pass", wow

