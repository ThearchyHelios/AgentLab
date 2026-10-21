"""分析结论进可验证报告（第 1 波 W1-b）：bundle/build.py 带上结论工件和它的输入快照，verifier.js 在 node 里核对。

- 报告引用的分析结论按原字节嵌入，连同输入快照；服务端导出时按封存的输入复算一遍，结果记在工件条目上（recheck）
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

from app import analysis
from app.analysis.contract import ARTIFACT_KIND, ledger_entry
from app.analysis.testing import fake_result
from app.bundle.build import build_bundle
from app.core.artifact_store import canonical_json, content_hash, manifest_hash
from app.engine.evidence import build_catalog, compose_doc, iter_segments
from tests.test_analysis_evidence import SNAPSHOT
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
              source_masks: dict[str, list[str]] | None = None,
              graph: dict[str, Any] | None = None) -> tuple[dict[str, Any], Files, str]:
    """一次封存好的运行的页面数据：(bundle, 工件, 分析结论的 id)。"""
    files = Files()
    snap = snapshot or SNAPSHOT
    q = files.put(snap)
    result = fake_result(inputs=[{"ref": "Q2", "artifact": q, "role": "data", "columns": ["region", "week", "gmv"],
                                  "rows": "all"}])
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
    add("report.checked", "write", {"doc_artifact": doc_id, "ok": True})
    add("node.finished", "write", {"duration_ms": 9, "attempt": 1, "preview": {"text": "（报告正文）", "doc_artifact": doc_id}})
    add("issuance", "out", {"tier": "formal", "mode": "citations"})
    add("run.finished", None, {"output": {"周报": "（报告正文）"}})
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
    assert analysis_item["recheck"] == {"status": "failed", "method": "decompose.additive@1",
                                        "text": "复算失败：系统里还没有这种分析的复算实现"}
    [query] = [x for x in bundle["artifacts"] if x["kind"] == "query"]
    assert "b64" in query

    got = verdicts(bundle)
    assert all(r == "pass" for key in ("events", "manifest", "artifacts", "chain", "meta") for r in got[key]), got["why"]
    numbers = {n["ref"]: n for n in got["numbers"]}
    assert set(numbers) == {"a:A1", "a:A1.top_share|pct", "v:A1.r0.delta"}
    assert all(n["result"] == "pass" and n["expected"] == n["text"] for n in numbers.values()), numbers
    assert numbers["a:A1"]["text"] == "销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）"
    assert got["overall"]["result"] == "pass"


def test_the_server_recheck_is_recorded_when_the_operators_answer(monkeypatch):
    monkeypatch.setattr(analysis, "recompute", lambda result, load, *, load_result=None: dict(result))
    bundle, _, a = bundle_of()
    assert item(bundle, a)["recheck"]["text"] == "复算一致：按封存的输入重新计算，结论逐项相同"


@pytest.mark.parametrize("ref, text", [("a:A1", "销售额下降 194 万（-8.7%），主要来自华南（-120 万，占 62%）"),
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
