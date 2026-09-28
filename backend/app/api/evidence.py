"""证据接口：整次运行的证据图，和点开报告里一个片段时的出处链。

一件证据可信，当且仅当它能从封存范围内的事件（seq ≤ manifest_seq）出发走到，
并且取回时哈希复验通过：

- 报告文档：report.checked 事件里的 doc_artifact
- 口径卡的指标集：口径卡节点 node.finished 事件里的 evidence 台账
- 查询快照：tool.end 事件里的 query_artifact，或者 node.finished.evidence 里的 query 条目
- 成果（哪些字段是报告原文）：出口节点 node.finished 事件里的 node_output 工件
- 运行输入：入口节点 node.finished 事件里的 node_output 工件

artifacts 表可以事后插行，run.output 是可以改写的一列，封存之后追加的事件不在
核对范围里——这三样都不当来源。解析数字层的链：口径卡指标 → 它的输入 → 查询快照里
的那一格，或者单元格引用直接到查询快照；旧运行按旧契约的位置信息标出 matched
（legacy_contract），不按数值猜来源。

查询步骤只给被引用的行和前后各 2 行，完整快照仍走 /api/artifacts/{id}。数据源
options.mask_columns 里的列换成「已遮罩」——在有身份体系之前，这只减少暴露，不是
安全边界：知道工件 id 的人照样能取到整份快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.coded import CodedHTTPException
from app.core.artifact_store import load
from app.data.engine import masked_columns
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow
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

MASKED = "已遮罩"
MASK_NOTE = "这些列在数据源里设了遮罩，面板上不显示原值。在有身份体系之前，遮罩只减少暴露，不是安全边界"
#: 查询条目的数据源现在找不到了（改名或删掉了）：只能按快照里记下的、查询当时的遮罩来遮
SOURCE_GONE = "数据源「{source}」现在找不到了（改名或删掉了），按查询当时记下的遮罩处理"
#: 查询步骤给被引用的行前后各带几行
WINDOW = 2
#: 一个查询步骤最多给多少行：数组字段引用了一大段行时，窗口不能把整份快照搬过来
MAX_WINDOW_ROWS = 50
UNSEALED_QUERY = "这份查询快照不在封存范围内的任何事件里（没有哪次查询在封存前交回过它），不能当证据展示"
TAMPERED_QUERY = "查询快照和它的哈希对不上，疑似被改过，不展示其中的行"
MISSING_QUERY = "查询快照在工件库里取不回来"


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
    #: 封存范围内交回过的查询快照：工件 id → {node_id, tool, source, via, call_id, exec}
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 口径卡节点 id → 封存范围内它的 caliber.upgrade 事件（升版处置）
    upgrades: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        if not node_id or not isinstance(data, dict):
            continue
        if etype == "tool.end" and data.get("query_artifact"):
            out.queries.setdefault(str(data["query_artifact"]), {
                "node_id": node_id, "tool": data.get("tool"), "via": data.get("artifact")})
        elif etype == "caliber.upgrade":
            out.upgrades[node_id] = data
        if etype != "node.finished":
            continue
        if data.get("artifact"):
            out.outputs[node_id] = data["artifact"]
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("artifact"):
                out.ledger.add((node_id, entry["artifact"]))
                if entry.get("kind") == "query":
                    out.queries[str(entry["artifact"])] = {
                        "node_id": node_id, **{k: entry[k] for k in ("tool", "source", "via", "call_id", "exec")
                                               if entry.get(k) is not None}}
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
    catalog = doc.get("catalog") or {}
    cells: list[str] = []
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            cite = seg.get("cite") or {}
            if cite.get("kind") == "cell" and cite.get("status") == "resolved":
                cells.append(_cell_name(cite.get("alias"), cite.get("locator") or {}))
    for alias, entry in catalog.items():
        item = {
            "alias": alias, "eid": entry.get("eid"), "kind": entry.get("kind"), "label": entry.get("label"),
            "node_id": entry.get("node_id"), "artifact": entry.get("artifact"),
            "sealed": _entry_sealed(sealed, entry, inputs), "cited_by": cited_by.get(alias, []),
            "units": units_of.get(alias, []), "report": report.node_id,
        }
        if entry.get("kind") in ("query", "retrieval"):
            # 查询、检索条目带上是什么、有多大：图上不点开也看得出这是哪次取数
            item.update({k: entry[k] for k in ("tool", "source", "columns", "rows", "truncated") if k in entry})
        evidence.append(item)
        if entry.get("kind") != "metric" or not entry.get("artifact"):
            continue
        artifact = entry["artifact"]
        if artifact not in cards:
            cards[artifact] = _load_card(artifact)[0]
        metric = _metric_in(cards[artifact], (entry.get("locator") or {}).get("metric"))
        for source in (metric or {}).get("inputs") or []:
            edge = {"from": alias, "to": source.get("path"), "rel": "input", "node_id": source.get("node_id"),
                    "report": report.node_id}
            if cell := _input_cell(source, catalog):
                edge["cell"] = cell
                cells.append(cell)
            edges.append(edge)
    for cell in dict.fromkeys(c for c in cells if c):
        edges.append({"from": cell, "to": cell.split(".", 1)[0], "rel": "cell_of", "report": report.node_id})
    return evidence, edges


def _cell_name(alias: Any, locator: dict[str, Any]) -> str:
    """Q1.r0.gmv：报告目录里的全局编号加行列，和 [[v:]] 的写法一致。"""
    if not alias or not isinstance(locator.get("row"), int) or not locator.get("column"):
        return ""
    return f"{alias}.r{locator['row']}.{locator['column']}"


def _query_alias(catalog: dict[str, Any], artifact: Any) -> str | None:
    """这份快照在报告目录里的全局编号。agent 字段的 ref 是那个节点内部的编号，靠工件对上。"""
    if not artifact:
        return None
    return next((alias for alias, e in catalog.items() if e.get("kind") == "query" and e.get("artifact") == artifact),
                None)


def _input_cell(item: dict[str, Any], catalog: dict[str, Any]) -> str:
    """口径卡的一个输入落在哪一格（cell() 取的、或者 agent 字段核对到的），按全局编号写。"""
    locator = item.get("locator") or {}
    return _cell_name(_query_alias(catalog, item.get("artifact")), locator) if item.get("artifact") else ""


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
    """点开报告里的一个片段：片段、所在的句子、出处链（指标 → 输入 → 查询快照，或者单元格直接到查询快照）、封存状态。

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
    chain = await _chain(seg, doc, sealed)
    covered = sealed.trusted and all(step.get("sealed") for step in chain if "sealed" in step)
    masked = list(dict.fromkeys(c for step in chain if step.get("step") == "query" for c in step.get("masked") or []))
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
        "redacted": {"columns": masked, "note": MASK_NOTE} if masked else {"columns": []},
    }


def _choose(reports: list[_Report], wanted: str | None, sealed: _Sealed) -> _Report:
    if wanted:
        chosen = next((r for r in reports if r.node_id == wanted), None)
        if chosen is None:
            raise CodedHTTPException(404, f"这次运行里没有报告节点 {wanted} 的文档", REPORT_NOT_FOUND)
        return chosen
    primary = next(iter(_output_evidence(sealed)), {})
    return next((r for r in reports if r.node_id == primary.get("report_node")), reports[0])


async def _chain(seg: dict[str, Any], doc: dict[str, Any], sealed: _Sealed) -> list[dict[str, Any]]:
    cite = seg.get("cite") or {}
    entry = (doc.get("catalog") or {}).get(cite.get("alias")) or {}
    masks = _Masks()
    if _missing_input(cite, entry):
        # 指标在卡里、只是这次没有值（显示「—」）：照样给出指标、输入和查询步骤，面板才指得出是哪个
        # 输入空了、那一格在哪次查询里。片段上没记定位和 eid，用目录条目的（它们指的是同一个指标）
        return await _metric_chain(seg, {**cite, "locator": entry.get("locator") or {}, "eid": entry.get("eid")},
                                   entry, doc, sealed, masks)
    if seg.get("state") != "deterministic" or cite.get("status") != "resolved":
        return []
    if cite.get("kind") == "cell":
        locator = cite.get("locator") or {}
        return [await _query_step(str(entry.get("artifact") or ""), [(locator.get("row"), locator.get("column"))],
                                  doc, sealed, masks)]
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
    return await _metric_chain(seg, cite, entry, doc, sealed, masks)


def _missing_input(cite: dict[str, Any], entry: dict[str, Any]) -> bool:
    """引用的指标存在，只是这次缺输入没有值。引用写错（目录里没有、同名指标有歧义）的不算。"""
    return (cite.get("kind") == "metric" and cite.get("status") == "unresolved" and entry.get("kind") == "metric"
            and entry.get("value") is None and entry.get("status") == "missing_input")


async def _metric_chain(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any], doc: dict[str, Any],
                        sealed: _Sealed, masks: "_Masks") -> list[dict[str, Any]]:
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
        "source": await _caliber_source((card or {}).get("source")),
        "caliber_upgrade": _caliber_upgrade(sealed.upgrades.get(str(entry.get("node_id") or ""))),
    }
    catalog = doc.get("catalog") or {}
    inputs: list[dict[str, Any]] = []
    #: 输入指到的查询快照 → 被引用的格；同一份快照只给一个查询步骤，格合在一起高亮
    wanted: dict[str, list[tuple[Any, Any]]] = {}
    for item in (metric or {}).get("inputs") or []:
        one = {"step": "input", **item}
        artifact_of = item.get("artifact")
        if artifact_of and item.get("via") in ("tool_cell", "agent_field"):
            one["query"] = _query_alias(catalog, artifact_of)
            if cell := _input_cell(item, catalog):
                one["cell"] = cell
            wanted.setdefault(str(artifact_of), []).extend(_located(item.get("locator") or {}))
        inputs.append(one)
    queries = [await _query_step(a, cells, doc, sealed, masks) for a, cells in wanted.items()]
    return [step, *inputs, *queries]


def _located(locator: dict[str, Any]) -> list[tuple[Any, Any]]:
    """输入的定位 → 被引用的格。单个格 {row, column}；数组字段是一段行 {rows:[a,b], column | columns}。"""
    if isinstance(locator.get("row"), int):
        return [(locator["row"], locator.get("column"))]
    rows = locator.get("rows")
    if isinstance(rows, list) and len(rows) == 2 and all(isinstance(r, int) for r in rows):
        cols = [locator["column"]] if locator.get("column") else list((locator.get("columns") or {}).values())
        return [(r, c) for r in range(rows[0], rows[1] + 1) for c in cols]
    return []


async def _caliber_source(source: Any) -> dict[str, Any] | None:
    """钉住别处口径卡（caliber_from）时，定义取自哪个工作流的哪一版；带上工作流现在的名字方便认。"""
    if not isinstance(source, dict) or not source.get("workflow_id"):
        return None
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, str(source["workflow_id"]))
    return {**source, "workflow_name": workflow.name if workflow else None}


def _caliber_upgrade(event: dict[str, Any] | None) -> dict[str, Any] | None:
    """这张口径卡在正式运行发起时的升版处置：钉在哪一版、上游最新是哪一版、怎么处置的。"""
    if not isinstance(event, dict):
        return None
    return {k: event.get(k) for k in ("workflow_id", "caliber_node", "pinned", "latest", "policy", "policy_label")}


class _Masks:
    """一次请求里查过的数据源遮罩：数据源名 → (要遮的列（小写）, 数据源现在还在不在)。"""

    def __init__(self) -> None:
        self._memo: dict[str, tuple[set[str], bool]] = {}

    async def of(self, source: Any) -> tuple[set[str], bool]:
        name = str(source or "")
        if not name:
            return set(), True
        if name not in self._memo:
            async with SessionLocal() as session:
                row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
            self._memo[name] = ({c.lower() for c in masked_columns(row.options if row else None)}, row is not None)
        return self._memo[name]


async def _query_step(artifact: str, cells: list[tuple[Any, Any]], doc: dict[str, Any], sealed: _Sealed,
                      masks: _Masks) -> dict[str, Any]:
    """查询步骤：被引用的格所在的行加前后各 WINDOW 行，高亮那几格，遮掉数据源设了遮罩的列。

    快照只认封存范围内的事件交回过的那些（tool.end.query_artifact、node.finished.evidence 的
    query 条目）；目录里写着、事件里追不到的，照实说不认，一行都不给。
    """
    catalog = doc.get("catalog") or {}
    alias = _query_alias(catalog, artifact)
    entry = catalog.get(alias, {}) if alias else {}
    info = sealed.queries.get(artifact) if artifact else None
    rows_hit = sorted({r for r, _ in cells if isinstance(r, int)})
    cols_hit = list(dict.fromkeys(str(c) for _, c in cells if c))
    step: dict[str, Any] = {
        "step": "query", "alias": alias, "artifact": artifact or None,
        "node_id": (info or {}).get("node_id") or entry.get("node_id"),
        "tool": (info or {}).get("tool") or entry.get("tool"),
        "source": (info or {}).get("source") or entry.get("source"),
        "sql": None, "columns": [], "rows": [], "row_offset": 0, "row_index": [], "total_rows": None,
        "truncated": None,
        "highlight": {"rows": rows_hit, "cols": cols_hit,
                      "cells": [[r, str(c)] for r, c in dict.fromkeys(cells) if isinstance(r, int) and c]},
        "masked": [], "hash_ok": None, "sealed": False,
    }
    if info is None:
        return {**step, "note": UNSEALED_QUERY}
    step["sealed"] = sealed.trusted
    try:
        snap = load(artifact)
        step["hash_ok"] = True if snap is not None else None
    except ValueError:
        snap, step["hash_ok"] = None, False
    if not isinstance(snap, dict) or not isinstance(snap.get("rows"), list):
        return {**step, "note": TAMPERED_QUERY if step["hash_ok"] is False else MISSING_QUERY}
    columns = [str(c) for c in snap.get("columns") or []]
    rows = snap["rows"]
    step["source"] = step["source"] or snap.get("source")
    # 遮的是「查询当时记下的」和「数据源现在设的」两者之和：事后加的遮罩照样生效，数据源改名、
    # 删掉了也不会把当时遮着的列亮出来
    hidden, found = await masks.of(step["source"])
    recorded = snap.get("mask_columns")
    hidden = hidden | ({str(c).lower() for c in recorded if isinstance(c, str)} if isinstance(recorded, list) else set())
    if not found:
        step["mask_note"] = SOURCE_GONE.format(source=step["source"])
    masked = {i for i, c in enumerate(columns) if c.lower() in hidden}
    index = sorted({i for r in rows_hit for i in range(r - WINDOW, r + WINDOW + 1) if 0 <= i < len(rows)})
    step.update(sql=snap.get("sql"), columns=columns, total_rows=len(rows), truncated=bool(snap.get("truncated")),
                masked=[columns[i] for i in sorted(masked)],
                # 查询时从驱动的原始值记下的列类型（老快照没有）：文本列里的 "2026" 按文本显示，不当数
                column_types=snap.get("column_types") if isinstance(snap.get("column_types"), dict) else {})
    if len(index) > MAX_WINDOW_ROWS:
        index, step["window_truncated"] = index[:MAX_WINDOW_ROWS], True
    step["row_index"] = index
    step["row_offset"] = index[0] if index else 0
    step["rows"] = [[MASKED if i in masked else v for i, v in enumerate(rows[r])] if isinstance(rows[r], list)
                    else rows[r] for r in index]
    return step


def _note(seg: dict[str, Any], unit: dict[str, Any]) -> str:
    """片段为什么是现在这个状态，一句人话。"""
    issue = seg.get("issue")
    if issue == "uncited_number":
        return "这个数字没有出处：写作者直接写了数字，没有用引用标记，系统没法核对它"
    if issue == "unresolved_ref":
        return f"引用解析不了：{(seg.get('cite') or {}).get('reason') or '证据目录里没有它'}"
    if seg.get("state") == "deterministic" and (seg.get("cite") or {}).get("kind") == "cell":
        return "这个值由系统从查询快照里取出、按固定规则渲染，没有经过模型转写"
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
