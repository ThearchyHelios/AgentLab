"""证据接口：整次运行的证据图，和点开报告里一个片段时的出处链。

一件证据可信，当且仅当它能从封存范围内的事件（seq ≤ manifest_seq）出发走到，
并且取回时哈希复验通过：

- 报告文档：report.checked 事件里的 doc_artifact
- 口径卡的指标集：口径卡节点 node.finished 事件里的 evidence 台账
- 成果（哪些字段是报告原文）：出口节点 node.finished 事件里的 node_output 工件
- 运行输入：入口节点 node.finished 事件里的 node_output 工件

artifacts 表可以事后插行，run.output 是可以改写的一列，封存之后追加的事件不在
核对范围里——这三样都不当来源。本期只解析数字层（口径卡指标、运行输入）的链；
旧运行按旧契约的位置信息标出 matched（legacy_contract），不按数值猜来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.coded import CodedHTTPException
from app.core.artifact_store import load
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import (
    RenderError,
    find_segment,
    input_eid,
    iter_units,
    make_eid,
    render_metric,
)

router = APIRouter(prefix="/api/runs", tags=["evidence"])

SCHEMA = "agentlab.evidence/1"

RUN_NOT_FOUND = "run_not_found"
REPORT_NOT_FOUND = "evidence_report_not_found"
SEGMENT_NOT_FOUND = "evidence_segment_not_found"
DOC_MISSING = "evidence_doc_missing"
DOC_TAMPERED = "evidence_doc_tampered"

LEGACY_NOTE = "旧版出具：按数值匹配，不是显式引用。同一个值对得上好几个指标时，出处不唯一"
NONE_NOTE = "这次运行没有报告文档，也没有出具契约，没有可以展示的证据"
NO_DOC_NOTE = "这次运行的出具契约用的是引用模式，但报告没有产出可以核对的文档"


# --------------------------------------------------------------------------
# 封存范围
# --------------------------------------------------------------------------


@dataclass
class _Sealed:
    run_id: str
    graph: dict[str, Any]
    seal: dict[str, Any]
    events: list[tuple[int, str, str | None, dict[str, Any]]]
    #: 封存范围内 node.finished.evidence 记过的 (节点, 工件)
    ledger: set[tuple[str, str]] = field(default_factory=set)
    #: 节点 id → 它最后一次 node.finished 的产出工件 id
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def sealed(self) -> bool:
        return bool(self.seal["sealed"])

    @property
    def trusted(self) -> bool:
        """封存了、而且封存核对通过。封存范围内有事件被改过的话，范围里的哪一条都不能再
        当「已封存」展示——只看 sealed 的消费方会把一条断掉的封存链画成绿的。"""
        return self.sealed and self.seal["ok"] is True

    def nodes_of(self, node_type: str) -> list[str]:
        return [str(n.get("id")) for n in self.graph.get("nodes") or []
                if isinstance(n, dict) and n.get("type") == node_type]

    def payload_of(self, node_ids: list[str]) -> dict[str, Any] | None:
        """这几个节点里最后跑完的那个的产出，从封存的事件指向的工件里取。"""
        for node_id in reversed([nid for _, t, nid, _ in self.events if t == "node.finished" and nid in node_ids]):
            artifact = self.outputs.get(node_id)
            try:
                payload = load(artifact) if artifact else None
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return payload
        return None


async def _sealed(run_id: str) -> _Sealed:
    from app.engine.runner import verify_manifest

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]
        graph = run.graph if isinstance(run.graph, dict) else {}
    verdict = await verify_manifest(run_id)
    bound = verdict.get("sealed_at") if verdict.get("sealed") else None
    # 没封存（还在跑、停在审批）的运行没有「封存之后」：现有的事件都算，但每件证据都标未封存
    events = [r for r in rows if bound is None or r[0] <= bound]
    out = _Sealed(run_id=run_id, graph=graph, events=events, seal={
        "sealed": bool(verdict.get("sealed")), "ok": verdict.get("ok"), "manifest_seq": bound,
        "legacy": bool(verdict.get("legacy", False))})
    for _, etype, node_id, data in events:
        if etype != "node.finished" or not node_id or not isinstance(data, dict):
            continue
        if data.get("artifact"):
            out.outputs[node_id] = data["artifact"]
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("artifact"):
                out.ledger.add((node_id, entry["artifact"]))
    return out


# --------------------------------------------------------------------------
# 报告文档
# --------------------------------------------------------------------------


@dataclass
class _Report:
    node_id: str
    doc_artifact: str
    ok: bool | None
    repairs: int | None
    stats: dict[str, Any] | None
    doc: dict[str, Any] | None
    #: True 取回并复验通过；False 内容和哈希对不上（或者不是这次运行这个节点写的）；None 文件不在了
    hash_ok: bool | None
    fields: list[str]


def _reports(sealed: _Sealed) -> list[_Report]:
    """封存范围内每个报告节点最后一次的自查结果，按第一次出现的顺序。"""
    latest: dict[str, dict[str, Any]] = {}
    for _, etype, node_id, data in sealed.events:
        if etype == "report.checked" and node_id and isinstance(data, dict) and data.get("doc_artifact"):
            latest[node_id] = data
    evidence = _output_evidence(sealed)
    out = []
    for node_id, data in latest.items():
        artifact = str(data["doc_artifact"])
        try:
            doc = load(artifact)
            hash_ok: bool | None = True if doc is not None else None
        except ValueError:
            doc, hash_ok = None, False
        # 文档要真是这次运行里这个节点写的：内容寻址只保证没被改，不保证没被张冠李戴
        if isinstance(doc, dict) and (doc.get("node_id") != node_id or doc.get("run_id") != sealed.run_id):
            doc, hash_ok = None, False
        fields = [f for r in evidence if r.get("report_node") == node_id and r.get("doc_artifact") == artifact
                  for f in r.get("fields") or []]
        out.append(_Report(node_id=node_id, doc_artifact=artifact, ok=data.get("ok"), repairs=data.get("repairs"),
                           stats=(doc or {}).get("stats") or data.get("stats"),
                           doc=doc if isinstance(doc, dict) else None, hash_ok=hash_ok, fields=fields))
    return out


def _output_evidence(sealed: _Sealed) -> list[dict[str, Any]]:
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    primary = output.get("_evidence")
    if not isinstance(primary, dict):
        return []
    return [primary, *[r for r in primary.get("others") or [] if isinstance(r, dict)]]


def _input_payloads(sealed: _Sealed) -> dict[str, Any]:
    return sealed.payload_of(sealed.nodes_of("input")) or {}


def _entry_sealed(sealed: _Sealed, entry: dict[str, Any], inputs: dict[str, Any]) -> bool:
    if not sealed.trusted:
        return False
    if entry.get("kind") == "input":
        name = (entry.get("locator") or {}).get("field")
        return name in inputs and inputs[name] == entry.get("value") \
            and entry.get("eid") == input_eid(name, entry.get("value"))
    return (str(entry.get("node_id") or ""), str(entry.get("artifact") or "")) in sealed.ledger


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence
# --------------------------------------------------------------------------


@router.get("/{run_id}/evidence")
async def evidence_graph(run_id: str) -> dict[str, Any]:
    """整次运行的证据图：报告、每件证据、谁引用了谁。mode 是 cited / legacy_contract / none。"""
    sealed = await _sealed(run_id)
    reports = _reports(sealed)
    body: dict[str, Any] = {"run_id": run_id, "schema": SCHEMA, "seal": sealed.seal,
                            "reports": [], "evidence": [], "edges": []}
    if not reports:
        issuance = _issuance_of(sealed)
        if issuance is None:
            return {**body, "mode": "none", "note": NONE_NOTE}
        if issuance.get("mode") == "citations":
            # 引用模式的契约，报告却被跳过、或者文档没落下来：不是旧版出具，照实说没有文档，并带上契约记的缘由
            gaps = [str(g) for g in issuance.get("gaps") or [] if g]
            why = next((g for g in gaps if "报告" in g), gaps[0] if gaps else None)
            return {**body, "mode": "none", "note": f"{NO_DOC_NOTE}：{why}" if why else NO_DOC_NOTE}
        return {**body, "mode": "legacy_contract", "legacy": _legacy(sealed, issuance)}

    inputs = _input_payloads(sealed)
    for report in reports:
        body["reports"].append({
            "node_id": report.node_id, "doc_artifact": report.doc_artifact,
            "doc_sealed": sealed.trusted, "hash_ok": report.hash_ok, "ok": report.ok,
            "repairs": report.repairs, "fields": report.fields, "stats": report.stats,
        })
        if report.doc is not None:
            evidence, edges = _graph_of(report, sealed, inputs)
            body["evidence"].extend(evidence)
            body["edges"].extend(edges)
    return {**body, "mode": "cited"}


def _graph_of(report: _Report, sealed: _Sealed, inputs: dict[str, Any]) -> tuple[list, list]:
    doc = report.doc or {}
    cited_by: dict[str, list[str]] = {}
    units_of: dict[str, list[str]] = {}
    edges: list[dict[str, Any]] = []
    for _, unit in iter_units(doc):
        for alias in unit.get("cites") or []:
            units_of.setdefault(alias, []).append(unit["id"])
        for seg in unit.get("segments") or []:
            cite = seg.get("cite") or {}
            if seg.get("state") == "deterministic" and cite.get("status") == "resolved":
                cited_by.setdefault(cite["alias"], []).append(seg["id"])
                edges.append({"from": seg["id"], "to": cite["alias"], "rel": "cites", "report": report.node_id})
        for support in unit.get("see") or []:
            if support.get("status") == "resolved":
                edges.append({"from": unit["id"], "to": support["alias"], "rel": "supports",
                              "report": report.node_id})

    evidence = []
    cards: dict[str, dict[str, Any] | None] = {}
    for alias, entry in (doc.get("catalog") or {}).items():
        evidence.append({
            "alias": alias, "eid": entry.get("eid"), "kind": entry.get("kind"), "label": entry.get("label"),
            "node_id": entry.get("node_id"), "artifact": entry.get("artifact"),
            "sealed": _entry_sealed(sealed, entry, inputs), "cited_by": cited_by.get(alias, []),
            "units": units_of.get(alias, []), "report": report.node_id,
        })
        if entry.get("kind") != "metric" or not entry.get("artifact"):
            continue
        artifact = entry["artifact"]
        if artifact not in cards:
            cards[artifact] = _load_card(artifact)[0]
        metric = _metric_in(cards[artifact], (entry.get("locator") or {}).get("metric"))
        for item in (metric or {}).get("inputs") or []:
            edges.append({"from": alias, "to": item.get("path"), "rel": "input", "node_id": item.get("node_id"),
                          "report": report.node_id})
    return evidence, edges


def _load_card(artifact: str) -> tuple[dict[str, Any] | None, bool]:
    try:
        card = load(artifact)
    except ValueError:
        return None, False
    return (card, True) if isinstance(card, dict) else (None, False)


def _metric_in(card: dict[str, Any] | None, metric_id: Any) -> dict[str, Any] | None:
    return next((m for m in (card or {}).get("metrics") or [] if m.get("id") == metric_id), None)


def _issuance_of(sealed: _Sealed) -> dict[str, Any] | None:
    """成果上的 _issuance；成果里取不到时退回封存范围内最后一条 issuance 事件。"""
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    issuance = output.get("_issuance")
    if not isinstance(issuance, dict):
        issuance = next((data for _, etype, _, data in reversed(sealed.events) if etype == "issuance"), None)
    return issuance if isinstance(issuance, dict) else None


def _legacy(sealed: _Sealed, issuance: dict[str, Any]) -> dict[str, Any]:
    """旧契约：把 _issuance 里的 matched 按位置标出来，认出偏移对得上的是哪个成果字段。

    偏移是 trace_numbers 在叙述串里算的；叙述模板就是某个成果字段时，每个 token 在那个
    字段的 [start, end) 上应当原样出现。对不上的字段不认——宁可不标，也不猜。
    """
    output = sealed.payload_of(sealed.nodes_of("output")) or {}

    def positioned(item: dict[str, Any]) -> dict[str, Any]:
        where = isinstance(item.get("start"), int) and isinstance(item.get("end"), int)
        return {**item, "positioned": where}

    matched = [positioned(m) for m in issuance.get("matched") or [] if isinstance(m, dict)]
    unmatched = [positioned(u) for u in issuance.get("unmatched_numbers") or [] if isinstance(u, dict)]
    placed = [m for m in matched + unmatched if m["positioned"]]
    fields = [name for name, value in output.items()
              if not name.startswith("_") and isinstance(value, str) and placed
              and all(value[m["start"]:m["end"]] == m.get("token") for m in placed)]
    return {"note": LEGACY_NOTE, "tier": issuance.get("tier"), "matched": matched, "unmatched": unmatched,
            "field": fields[0] if len(fields) == 1 else None}


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence/segments/{segment_id}
# --------------------------------------------------------------------------


@router.get("/{run_id}/evidence/segments/{segment_id}")
async def evidence_segment(run_id: str, segment_id: str, report: str | None = None) -> dict[str, Any]:
    """点开报告里的一个片段：片段、所在的句子、出处链（本期是指标步骤加它的输入）、封存状态。

    一次运行里有好几份报告时用 ?report=<节点 id> 指定；不指定取成果里标注的那一份。
    """
    sealed = await _sealed(run_id)
    reports = _reports(sealed)
    if not reports:
        raise CodedHTTPException(404, "这次运行没有封存的报告文档，没有可以点开的片段", REPORT_NOT_FOUND)
    chosen = _choose(reports, report, sealed)
    if chosen.hash_ok is False:
        raise CodedHTTPException(409, "报告文档和它的哈希对不上，疑似被改过，不能再当证据展示", DOC_TAMPERED)
    if chosen.doc is None:
        raise CodedHTTPException(404, "报告文档在工件库里取不回来", DOC_MISSING)
    found = find_segment(chosen.doc, segment_id)
    if found is None:
        raise CodedHTTPException(404, f"报告里没有片段 {segment_id}", SEGMENT_NOT_FOUND)

    block, unit, seg = found
    doc = chosen.doc
    markdown = str(doc.get("markdown") or "")
    start, end = unit.get("span") or [0, 0]
    chain = _chain(seg, doc, sealed)
    covered = sealed.trusted and all(step.get("sealed") for step in chain if "sealed" in step)
    return {
        "report": {"node_id": chosen.node_id, "doc_artifact": chosen.doc_artifact},
        "segment": {"id": seg["id"], "text": seg.get("text"), "kind": seg.get("kind"), "state": seg.get("state"),
                    "span": seg.get("span"), "unit": unit.get("id"),
                    **{k: seg[k] for k in ("ref", "issue", "strong", "cite") if k in seg}},
        "unit": {"id": unit.get("id"), "kind": unit.get("kind"), "text": markdown[start:end],
                 "span": unit.get("span"), "cites": unit.get("cites") or []},
        "block": {"id": block.get("id"), "type": block.get("type")},
        "chain": chain,
        "note": _note(seg, unit),
        "violations": [v for v in doc.get("violations") or []
                       if v.get("segment") == seg["id"] or (v.get("unit") == unit.get("id") and not v.get("segment"))],
        "seal": {"sealed": sealed.sealed, "ok": sealed.seal["ok"], "covered": covered},
        # 本期没有查询快照的行，也就没有要遮的列；字段先占位，前端不用等二期再改
        "redacted": {"columns": []},
    }


def _choose(reports: list[_Report], wanted: str | None, sealed: _Sealed) -> _Report:
    if wanted:
        chosen = next((r for r in reports if r.node_id == wanted), None)
        if chosen is None:
            raise CodedHTTPException(404, f"这次运行里没有报告节点 {wanted} 的文档", REPORT_NOT_FOUND)
        return chosen
    primary = next(iter(_output_evidence(sealed)), {})
    return next((r for r in reports if r.node_id == primary.get("report_node")), reports[0])


def _chain(seg: dict[str, Any], doc: dict[str, Any], sealed: _Sealed) -> list[dict[str, Any]]:
    cite = seg.get("cite") or {}
    if seg.get("state") != "deterministic" or cite.get("status") != "resolved":
        return []
    entry = (doc.get("catalog") or {}).get(cite.get("alias")) or {}
    if cite.get("kind") == "input":
        name = (cite.get("locator") or {}).get("field")
        inputs = _input_payloads(sealed)
        return [{"step": "run_input", "alias": cite.get("alias"), "field": name, "value": entry.get("value"),
                 "rendered": seg.get("text"), "eid": cite.get("eid"),
                 "eid_ok": cite.get("eid") == entry.get("eid") == input_eid(name, entry.get("value")),
                 "node_id": next(iter(sealed.nodes_of("input")), None),
                 "sealed": _entry_sealed(sealed, entry, inputs)}]
    if cite.get("kind") != "metric":
        return []
    return _metric_chain(seg, cite, entry, sealed)


def _metric_chain(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any],
                  sealed: _Sealed) -> list[dict[str, Any]]:
    """指标步骤：从封存台账里那件 metric_set 工件取回整张卡，按 id 找到指标，再验三件事——
    eid 能由工件和定位重算出来、卡里的值按同样的格式渲染出来就是报告上的字、这件工件
    确实记在口径卡节点封存过的台账里。"""
    artifact = str(entry.get("artifact") or "")
    metric_id = (cite.get("locator") or {}).get("metric")
    card, hash_ok = _load_card(artifact) if artifact else (None, False)
    metric = _metric_in(card, metric_id)
    try:
        rendered_now = render_metric(metric, cite.get("conv")) if metric else None
    except RenderError:
        rendered_now = None
    source = metric or entry
    step = {
        "step": "metric", "alias": cite.get("alias"), "metric": metric_id,
        "name": source.get("name") or metric_id, "value": source.get("value"), "unit": source.get("unit") or "",
        "decimals": source.get("decimals"), "format": source.get("format"), "conv": cite.get("conv"),
        "rendered": seg.get("text"),
        "caliber": (card or {}).get("caliber", entry.get("caliber")),
        "version": (card or {}).get("caliber_version", entry.get("version")),
        "expression": (metric or {}).get("expression"), "substituted": (metric or {}).get("substituted"),
        "recompute_ok": (metric or {}).get("recompute_ok"), "status": source.get("status"),
        "node_id": entry.get("node_id"), "artifact": artifact or None, "eid": cite.get("eid"),
        "eid_ok": cite.get("eid") == entry.get("eid") == make_eid("metric", artifact or None, {"metric": metric_id}),
        "hash_ok": hash_ok and metric is not None,
        "render_ok": rendered_now is not None and rendered_now == seg.get("text"),
        "sealed": sealed.trusted and (str(entry.get("node_id") or ""), artifact) in sealed.ledger,
        # 口径卡的版本还没纳入升版处置（那一套目前只管钉了版本的子工作流）
        "caliber_upgrade": None,
    }
    return [step, *({"step": "input", **item} for item in (metric or {}).get("inputs") or [])]


def _note(seg: dict[str, Any], unit: dict[str, Any]) -> str:
    """片段为什么是现在这个状态，一句人话。"""
    issue = seg.get("issue")
    if issue == "uncited_number":
        return "这个数字没有出处：写作者直接写了数字，没有用引用标记，系统没法核对它"
    if issue == "unresolved_ref":
        return f"引用解析不了：{(seg.get('cite') or {}).get('reason') or '证据目录里没有它'}"
    if seg.get("state") == "deterministic":
        return "数字由系统从证据里取出、按口径卡的格式渲染，没有经过模型转写"
    kind = seg.get("kind")
    if kind == "structural":
        return "这是 Markdown 的版式符号"
    if unit.get("kind") == "heading":
        return "这是标题"
    if unit.get("kind") == "connective":
        return "这是连接性文字，不陈述数据事实，所以不需要证据"
    return "这是结论句里的文字。本期只核对数字，句子本身有没有依据要到后续版本由模型判断"
