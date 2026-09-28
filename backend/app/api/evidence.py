"""证据接口：整次运行的证据图，和点开报告里一个片段时的出处链。

一件证据可信，当且仅当它能从封存范围内的事件（seq ≤ manifest_seq）出发走到，
并且取回时哈希复验通过：

- 报告文档：report.checked 事件里的 doc_artifact
- 口径卡的指标集：口径卡节点 node.finished 事件里的 evidence 台账
- 查询快照：tool.end 事件里的 query_artifact，或者 node.finished.evidence 里的 query 条目
- 成果（哪些字段是报告原文）：出口节点 node.finished 事件里的 node_output 工件
- 运行输入：入口节点 node.finished 事件里的 node_output 工件
- 表结构快照：tool.end 事件里的 schema_artifact，或者 node.finished.evidence 里的 schema 条目
- 检索快照：retrieve.end 事件里的 artifact，或者 node.finished.evidence 里的 retrieval 条目

artifacts 表可以事后插行，run.output 是可以改写的一列，封存之后追加的事件不在
核对范围里——这三样都不当来源。解析数字层的链：口径卡指标 → 它的输入 → 查询快照里
的那一格，或者单元格引用直接到查询快照；表名、字段名到表结构快照和查询；原话到检索
命中的那一段。旧运行按旧契约的位置信息标出 matched（legacy_contract）；没有契约的旧答案
按数值在封存的查询快照、口径卡里猜候选（legacy_text），只做展示，写明是猜的。

查询步骤只给被引用的行和前后各 2 行，完整快照仍走 /api/artifacts/{id}。数据源
options.mask_columns 里的列换成「已遮罩」——在有身份体系之前，这只减少暴露，不是
安全边界：知道工件 id 的人照样能取到整份快照。
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app.api.coded import CodedHTTPException
from app.core.artifact_store import load
from app.data.engine import masked_columns
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow
from app.engine.evidence import (
    ENTITY_KINDS,
    GUESS_NOTE,
    GUESS_SCHEMA,
    MISSING,
    UNKNOWN_ENTITY_REASON,
    UNVERIFIED_ENTITY_REASON,
    RenderError,
    build_catalog,
    closest_entities,
    find_segment,
    guess_sources,
    input_eid,
    iter_units,
    make_eid,
    normalize_quote,
    query_entry_fields,
    render_metric,
    sql_tables,
    uncited_claims,
)
from app.engine.toolcalls import QUERY_PREFIX

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
UNSEALED_RETRIEVAL = "这份检索快照不在封存范围内的任何事件里（没有哪次检索在封存前交回过它），不能当证据展示"
TAMPERED_RETRIEVAL = "检索快照和它的哈希对不上，疑似被改过，不展示原文"
MISSING_RETRIEVAL = "检索快照在工件库里取不回来，或者里面没有这条命中"


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
    #: 封存范围内 node.finished.evidence 的台账条目，按事件顺序（给可疑实体找「最接近的名字」重建目录用）
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: 封存范围内交回过的表结构快照：工件 id → {node_id, source}
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存范围内交回过的检索快照：工件 id → {node_id, source}
    retrievals: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 封存时记下的清单哈希（没封存的是 None）：旧答案的猜测按 (run_id, 它) 缓存
    manifest_hash: str | None = None
    #: verify_manifest 的完整结论（审计表照实附上）
    verdict: dict[str, Any] = field(default_factory=dict)

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
        manifest = run.manifest_hash
    verdict = await verify_manifest(run_id)
    bound = verdict.get("sealed_at") if verdict.get("sealed") else None
    # 没封存（还在跑、停在审批）的运行没有「封存之后」：现有的事件都算，但每件证据都标未封存
    events = [r for r in rows if bound is None or r[0] <= bound]
    out = _Sealed(run_id=run_id, graph=graph, events=events, seal={
        "sealed": bool(verdict.get("sealed")), "ok": verdict.get("ok"), "manifest_seq": bound,
        "legacy": bool(verdict.get("legacy", False))}, manifest_hash=manifest, verdict=verdict)
    for _, etype, node_id, data in events:
        if not node_id or not isinstance(data, dict):
            continue
        if etype == "tool.end":
            if data.get("query_artifact"):
                out.queries.setdefault(str(data["query_artifact"]), {
                    "node_id": node_id, "tool": data.get("tool"), "via": data.get("artifact")})
            if data.get("schema_artifact"):
                out.schemas.setdefault(str(data["schema_artifact"]), {"node_id": node_id})
        elif etype == "retrieve.end" and data.get("artifact"):
            out.retrievals.setdefault(str(data["artifact"]), {"node_id": node_id, "source": data.get("collection")})
        elif etype == "caliber.upgrade":
            out.upgrades[node_id] = data
        if etype != "node.finished":
            continue
        if data.get("artifact"):
            out.outputs[node_id] = data["artifact"]
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("artifact"):
                out.ledger.add((node_id, entry["artifact"]))
                out.entries.append({**entry, "node_id": node_id})
                artifact = str(entry["artifact"])
                if entry.get("kind") == "query":
                    out.queries[artifact] = {
                        "node_id": node_id, **{k: entry[k] for k in ("tool", "source", "via", "call_id", "exec")
                                               if entry.get(k) is not None}}
                elif entry.get("kind") == "schema":
                    out.schemas[artifact] = {"node_id": node_id, "source": entry.get("source")}
                elif entry.get("kind") == "retrieval":
                    out.retrievals[artifact] = {"node_id": node_id, "source": entry.get("source")}
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
    #: 写这份报告时的结论句策略（report.checked 里记的，升级前的运行没有）
    claims: str | None = None


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
                           doc=doc if isinstance(doc, dict) else None, hash_ok=hash_ok, fields=fields,
                           claims=data.get("claims") if isinstance(data.get("claims"), str) else None))
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
    artifact = str(entry.get("artifact") or "")
    if entry.get("kind") in ENTITY_KINDS:
        # 表和字段不属于哪个节点：eid 背后的表结构快照或查询快照是封存范围内交回过的就算
        return artifact in sealed.schemas or artifact in sealed.queries
    if entry.get("kind") == "retrieval" and artifact in sealed.retrievals:
        return True
    return (str(entry.get("node_id") or ""), artifact) in sealed.ledger


def _origin_sealed(sealed: _Sealed, origin: dict[str, Any]) -> bool:
    """实体的一个来历（schema / sql / result）背后的工件是不是封存范围内交回过的。"""
    artifact = str(origin.get("artifact") or "")
    pool = sealed.schemas if origin.get("kind") == "schema" else sealed.queries
    return sealed.trusted and artifact in pool


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence
# --------------------------------------------------------------------------


@router.get("/{run_id}/evidence")
async def evidence_graph(run_id: str) -> dict[str, Any]:
    """整次运行的证据图：报告、每件证据、谁引用了谁。mode 是 cited / legacy_contract / legacy_text / none。"""
    sealed = await _sealed(run_id)
    reports = _reports(sealed)
    body: dict[str, Any] = {"run_id": run_id, "schema": SCHEMA, "seal": sealed.seal,
                            "reports": [], "evidence": [], "edges": []}
    mode, extra = await _mode_of(sealed, reports)
    if mode != "cited":
        return {**body, "mode": mode, **extra}

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


async def _mode_of(sealed: _Sealed, reports: list[_Report]) -> tuple[str, dict[str, Any]]:
    """这次运行按哪种方式展示证据：(mode, 附带的 note 或 legacy)。

    有封存的报告文档就是 cited；否则看成果上的契约：引用模式的契约却没有文档照实说没有（none）；
    旧契约按它记下的 matched 标（legacy_contract）；没有契约的答案按数值猜候选（legacy_text），
    答案里没有数字、或者封存范围里没有查询和口径卡可比时是 none。
    """
    if reports:
        return "cited", {}
    issuance = _issuance_of(sealed)
    if issuance is None:
        legacy = await _legacy_text(sealed)
        return ("legacy_text", {"legacy": legacy}) if legacy is not None else ("none", {"note": NONE_NOTE})
    if issuance.get("mode") == "citations":
        # 引用模式的契约，报告却被跳过、或者文档没落下来：不是旧版出具，照实说没有文档，并带上契约记的缘由
        gaps = [str(g) for g in issuance.get("gaps") or [] if g]
        why = next((g for g in gaps if "报告" in g), gaps[0] if gaps else None)
        return "none", {"note": f"{NO_DOC_NOTE}：{why}" if why else NO_DOC_NOTE}
    return "legacy_contract", {"legacy": _legacy(sealed, issuance)}


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
        elif entry.get("kind") in ENTITY_KINDS:
            # 表和字段：叫什么、出现在哪几次查询里、来历（表结构快照 / SQL / 结果列）各自封存了没有
            item.update({k: entry[k] for k in ("name", "table", "tables", "qualified", "source") if entry.get(k)})
            item["queries"] = [str(q) for q in entry.get("queries") or []]
            item["sources"] = [{**{k: o[k] for k in ("kind", "artifact", "alias", "source", "truncated")
                                   if o.get(k) is not None}, "sealed": _origin_sealed(sealed, o)}
                               for o in entry.get("sources") or [] if isinstance(o, dict)]
            edges.extend({"from": alias, "to": q, "rel": "appears_in", "report": report.node_id}
                         for q in item["queries"])
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

    # 指标的名字、值、口径只从封存的口径卡里取，而且只看这份契约当时收的那几张卡（_issuance.calibers）
    cards = _metric_sources(sealed, [c.get("node") for c in issuance.get("calibers") or [] if isinstance(c, dict)])

    def sources_of(item: dict[str, Any]) -> list[dict[str, Any]]:
        ids = [item["metric"]] if item.get("metric") else list(item.get("candidates") or [])
        return [dict(src) for mid in ids for src in cards.get(str(mid), [])]

    matched = [{**positioned(m), "sources": sources_of(m)} for m in issuance.get("matched") or []
               if isinstance(m, dict)]
    unmatched = [positioned(u) for u in issuance.get("unmatched_numbers") or [] if isinstance(u, dict)]
    placed = [m for m in matched + unmatched if m["positioned"]]
    fields = [name for name, value in output.items()
              if not name.startswith("_") and isinstance(value, str) and placed
              and all(value[m["start"]:m["end"]] == m.get("token") for m in placed)]
    return {"note": LEGACY_NOTE, "tier": issuance.get("tier"), "matched": matched, "unmatched": unmatched,
            "field": fields[0] if len(fields) == 1 else None}


def _sealed_cards(sealed: _Sealed) -> list[tuple[str, str, dict[str, Any]]]:
    """封存范围内的口径卡：(节点, 工件, 指标集)。取回时复验哈希，对不上的、取不回来的不要。

    一期以后口径卡把指标集落成 metric_set 工件、记在台账里；一期以前没有，节点的产出（node.finished
    的 node_output 工件）本身就是指标集——只对画布上确实是口径卡的节点这样认。
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    calibers = set(sealed.nodes_of("metrics"))
    for _, etype, node_id, data in sealed.events:
        if etype != "node.finished" or not node_id or not isinstance(data, dict):
            continue
        arts = [str(e["artifact"]) for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
        if not arts and node_id in calibers and data.get("artifact"):
            arts = [str(data["artifact"])]
        for art in arts:
            if (node_id, art) in seen:
                continue
            seen.add((node_id, art))
            content, ok = _fetch(art)
            if ok is True and isinstance(content, dict) and isinstance(content.get("metrics"), list):
                out.append((node_id, art, content))
    return out


def _metric_sources(sealed: _Sealed, nodes: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """指标 id → 它在封存的口径卡里的样子（名字、值、渲染、口径、工件、eid）。nodes 为空时不限卡。"""
    wanted = {str(n) for n in nodes if n}
    out: dict[str, list[dict[str, Any]]] = {}
    for node_id, art, card in _sealed_cards(sealed):
        if wanted and node_id not in wanted:
            continue
        for metric in card["metrics"]:
            if not isinstance(metric, dict) or not metric.get("id"):
                continue
            mid = str(metric["id"])
            try:
                rendered = render_metric(metric)
            except RenderError:
                rendered = MISSING
            out.setdefault(mid, []).append({
                "kind": "metric", "ref": f"m:{mid}", "metric": mid, "name": metric.get("name") or mid,
                "value": metric.get("value"), "rendered": rendered, "unit": metric.get("unit") or "",
                "caliber": card.get("caliber") or "", "version": card.get("caliber_version") or "",
                "node_id": node_id, "artifact": art, "eid": make_eid("metric", art, {"metric": mid}),
                "sealed": sealed.trusted,
            })
    return out


# --------------------------------------------------------------------------
# 没有契约的旧答案：按数值猜候选（legacy_text）
# --------------------------------------------------------------------------

#: 猜测的缓存：(run_id, manifest_hash, 数据源遮罩的指纹) → legacy 对象（猜不出来的是 None）。
#: 只缓存封存完好的运行：还在跑、停在审批的运行事件还会变，封存链断了的运行不该被一份旧结果盖住
GUESS_CACHE_SIZE = 64
_guess_cache: OrderedDict[tuple[Any, ...], dict[str, Any] | None] = OrderedDict()
NO_CANDIDATE = "旧答案里的这个数，在这次运行封存的查询结果和口径卡里没有找到相同的值"


async def _mask_fingerprint() -> dict[str, list[str]]:
    """每个数据源现在设的遮罩（列名小写、排好序）。进缓存键：事后加了遮罩，缓存的猜测要作废——
    候选的值、渲染、差值都会把遮住的原值带出去。"""
    async with SessionLocal() as session:
        rows = (await session.execute(select(DataSource.name, DataSource.options))).all()
    masks = {str(name): sorted({c.lower() for c in masked_columns(options)}) for name, options in rows}
    return {name: cols for name, cols in masks.items() if cols}


async def _legacy_text(sealed: _Sealed) -> dict[str, Any] | None:
    """成果里每个文字字段的数字，去封存范围内的查询快照、口径卡里按数值找候选。只做展示，不是证据。

    查询快照、口径卡只认封存范围内的事件引用得到、取回时复验过哈希的（见 _legacy_pool）；artifacts 表
    里人为插的行、封存之后追加的事件都不看。猜测是同步的 CPU 活，连同读盘放进线程；结果按
    (run_id, manifest_hash) 加遮罩指纹缓存。
    """
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    texts = [(name, value) for name, value in output.items()
             if isinstance(name, str) and not name.startswith("_") and isinstance(value, str) and value.strip()]
    if not texts:
        return None
    masks = await _mask_fingerprint()
    key = (sealed.run_id, sealed.manifest_hash, tuple(sorted((n, tuple(c)) for n, c in masks.items())))
    cacheable = sealed.trusted and bool(sealed.manifest_hash)
    if cacheable and key in _guess_cache:
        _guess_cache.move_to_end(key)
        return _guess_cache[key]
    legacy = await asyncio.to_thread(_guess_legacy, sealed, texts, masks)
    if cacheable:
        _guess_cache[key] = legacy
        while len(_guess_cache) > GUESS_CACHE_SIZE:
            _guess_cache.popitem(last=False)
    return legacy


def _guess_legacy(sealed: _Sealed, texts: list[tuple[str, str]],
                  masks: dict[str, list[str]]) -> dict[str, Any] | None:
    pool = _legacy_pool(sealed)
    if not pool:
        return None
    # 遮罩：查询当时记下的（快照里的 mask_columns，guess_sources 自己会用）加数据源现在设的
    masked = {item["artifact"]: masks.get(str(item["content"].get("source") or ""), [])
              for item in pool if item["kind"] == "query"}
    fields = []
    for name, text in texts:
        guess = guess_sources(text, pool, masked=masked)
        if guess["stats"]["numbers"]:
            fields.append({"field": name, "markdown": guess["markdown"], "segments": guess["segments"],
                           "stats": guess["stats"]})
    if not fields:
        return None
    return {"schema": GUESS_SCHEMA, "mode": "legacy_text", "note": GUESS_NOTE, "fields": fields,
            "stats": {k: sum(f["stats"][k] for f in fields) for k in fields[0]["stats"]},
            "sources": {"queries": sum(1 for i in pool if i["kind"] == "query"),
                        "metric_sets": sum(1 for i in pool if i["kind"] == "metric_set")},
            "sealed": sealed.trusted}


def _legacy_pool(sealed: _Sealed) -> list[dict[str, Any]]:
    """猜候选用的已封存条目（guess_sources 的 sealed 参数），按在封存事件里第一次出现的顺序。

    查询快照三条路：tool.end.query_artifact、node.finished.evidence 的 query 条目，以及二期以前的运行
    ——那时 tool.end 只有 tool_snapshot，查询快照的 id 嵌在它的内容里（数据源工具交回的 JSON），
    内容寻址，所以同样追得到。只认数据源工具（db_query__）：别的工具拼一个同样形状的 JSON，里面的
    id 不是我们落的。查询按这个顺序编 Q1、Q2…，只为候选好认，和哪份报告的目录都无关。
    """
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()

    def query(artifact: Any, node_id: str, tool: Any) -> None:
        artifact = str(artifact or "")
        if not artifact or artifact in seen:
            return
        seen.add(artifact)
        content, ok = _fetch(artifact)
        if ok is True and isinstance(content, dict) and isinstance(content.get("rows"), list):
            number = sum(1 for item in pool if item["kind"] == "query") + 1
            pool.append({"kind": "query", "artifact": artifact, "content": content, "alias": f"Q{number}",
                         "node_id": node_id, **({"tool": str(tool)} if tool else {})})

    for _, etype, node_id, data in sealed.events:
        if etype not in ("tool.end", "node.finished") or not node_id or not isinstance(data, dict):
            continue
        if etype == "tool.end":
            tool = str(data.get("tool") or "")
            if data.get("query_artifact"):
                query(data["query_artifact"], node_id, tool)
            elif tool.startswith(QUERY_PREFIX) and data.get("artifact"):
                snap, ok = _fetch(str(data["artifact"]))
                if ok is True and isinstance(snap, dict) and str(snap.get("tool") or "").startswith(QUERY_PREFIX):
                    if fields := query_entry_fields(snap.get("result")):
                        query(fields["artifact"], node_id, tool)
            continue
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("kind") == "query":
                query(entry.get("artifact"), node_id, entry.get("tool"))
    pool.extend({"kind": "metric_set", "artifact": art, "content": card, "node_id": node_id}
                for node_id, art, card in _sealed_cards(sealed))
    return pool


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
    chain = await _chain(seg, doc, sealed, chosen.node_id)
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


async def _chain(seg: dict[str, Any], doc: dict[str, Any], sealed: _Sealed,
                 report_node: str) -> list[dict[str, Any]]:
    cite = seg.get("cite") or {}
    entry = (doc.get("catalog") or {}).get(cite.get("alias")) or {}
    masks = _Masks()
    if seg.get("kind") == "entity" and seg.get("issue") in SUSPICIOUS:
        return [_suspicious_step(seg, cite, sealed, report_node)]
    if seg.get("kind") == "entity" and seg.get("state") == "deterministic" and cite.get("status") == "resolved":
        return [_entity_step(seg, cite, entry, sealed)]
    if seg.get("kind") == "quote" and seg.get("state") == "deterministic" and cite.get("status") == "resolved":
        return [_quote_step(seg, cite, entry, sealed)]
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


# --------------------------------------------------------------------------
# 表名、字段名、原话
# --------------------------------------------------------------------------

#: 片段上的可疑实体：名字哪里都找不到（unknown_entity），或者表结构快照不全、核对不了（unverified_entity）
SUSPICIOUS = frozenset({"unknown_entity", "unverified_entity"})


def _fetch(artifact: str) -> tuple[Any, bool | None]:
    """取回一件工件：(内容, hash_ok)。hash_ok 为 True 复验通过，False 内容和哈希对不上，None 取不回来。"""
    try:
        content = load(artifact)
    except ValueError:
        return None, False
    return content, (True if content is not None else None)


def _schema_table(content: Any, name: Any) -> dict[str, Any] | None:
    """表结构快照里的一张表：表名一字不差的优先，其次不分大小写、全名（schema.表）也认。"""
    tables = content.get("tables") if isinstance(content, dict) else None
    if not isinstance(tables, dict) or not name:
        return None
    if isinstance(tables.get(name), dict):
        return tables[name]
    lowered = str(name).lower()
    return next((t for key, t in tables.items() if isinstance(t, dict)
                 and lowered in (str(key).lower(), str(t.get("qualified") or "").lower())), None)


def _schema_column(table: dict[str, Any] | None, name: Any) -> dict[str, Any] | None:
    columns = [c for c in (table or {}).get("columns") or [] if isinstance(c, dict)]
    return next((c for c in columns if c.get("name") == name), None) \
        or next((c for c in columns if str(c.get("name") or "").lower() == str(name or "").lower()), None)


def _entity_present(content: Any, kind: str, name: str, table: Any, column: Any, tables: list[Any]) -> bool:
    """目录条目说的表、字段在这份表结构快照里确实有：文档是内容寻址的，这一步防的是目录和快照张冠李戴。"""
    if kind == "table":
        return _schema_table(content, name) is not None
    owners = [table] if table else list(tables or [])
    return any(_schema_column(_schema_table(content, t), column) is not None for t in owners)


def _query_present(content: Any, kind: str, name: str, column: Any) -> bool:
    """查询快照里确实有这个名字：表在它的 SQL 里（FROM / JOIN），列在它的结果列里。"""
    if not isinstance(content, dict):
        return False
    if kind == "table":
        return name.lower() in {t.lower() for t in sql_tables(str(content.get("sql") or ""))}
    return str(column) in [str(c) for c in content.get("columns") or []]


def _entity_step(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any], sealed: _Sealed) -> dict[str, Any]:
    """实体步骤：这个名字出现在哪几次查询里（SQL 用到的表、结果的列）、表结构快照什么时候同步的、字段类型。

    来源（entry.sources）逐个核对：表结构快照要出现在封存范围内的 tool.end.schema_artifact 或 schema 台账
    条目里，查询快照要是封存范围内交回过的那几件。类型、同步时间只从核对过、复验过哈希的表结构快照取，
    取不到就不给。eid 背后的那件工件核对通过、名字确实在里面，这一步才算已封存。
    """
    kind = str(cite.get("kind") or entry.get("kind") or "")
    locator = dict(entry.get("locator") or cite.get("locator") or {})
    table, column = locator.get("table"), locator.get("column")
    name = str(entry.get("name") or cite.get("rendered") or "")
    artifact = str(entry.get("artifact") or "")
    step: dict[str, Any] = {
        "step": "entity", "status": "resolved", "kind": kind, "alias": cite.get("alias"), "name": name,
        **({"table": table} if table else {}), **({"column": column} if kind == "column" and column else {}),
        **{k: entry[k] for k in ("qualified", "tables", "source") if entry.get(k)},
        "queries": [str(q) for q in entry.get("queries") or []], "sources": [],
        "artifact": artifact or None, "eid": cite.get("eid"),
        "eid_ok": bool(artifact) and cite.get("eid") == entry.get("eid") == make_eid(kind, artifact, locator),
    }
    for flag in ("code", "auto"):
        if seg.get(flag):
            step[flag] = True
    snapshots: list[dict[str, Any]] = []            # 核对过、复验过哈希、名字确实在里面的表结构快照
    results: list[dict[str, Any]] = []              # 核对过、复验过哈希的查询快照（聚合别名的列类型从这里取）
    backed = False
    for origin in entry.get("sources") or []:
        if not isinstance(origin, dict) or not origin.get("artifact"):
            continue
        art = str(origin["artifact"])
        item: dict[str, Any] = {k: origin[k] for k in ("kind", "artifact", "alias", "source", "truncated")
                                if origin.get(k) is not None}
        if origin.get("kind") == "schema":
            item["sealed"] = art in sealed.schemas and sealed.trusted
            ok = False
            if art in sealed.schemas:
                content, item["hash_ok"] = _fetch(art)
                item["present"] = item["hash_ok"] is True and _entity_present(
                    content, kind, name, table, column, list(entry.get("tables") or []))
                ok = item["present"]
                if ok:
                    snapshots.append(content)
        else:
            info = sealed.queries.get(art)
            item.update({k: info[k] for k in ("node_id", "tool") if info and info.get(k)})
            item["sealed"] = info is not None and sealed.trusted
            ok = info is not None
            if ok and (art == artifact or (kind == "column" and origin.get("kind") == "result")):
                content, hash_ok = _fetch(art)
                present = hash_ok is True and _query_present(content, kind, name, column)
                if art == artifact:
                    item.update(hash_ok=hash_ok, present=present)
                    ok = present
                if present and origin.get("kind") == "result":
                    results.append(content)
        backed = backed or (art == artifact and ok)
        step["sources"].append(item)
    step["sealed"] = sealed.trusted and backed and step["eid_ok"]
    if snapshots:
        _describe_entity(step, snapshots[0], kind, name, table, column, list(entry.get("tables") or []))
    if kind == "column" and not any(o.get("kind") == "schema" for o in step["sources"]):
        # 聚合的别名这类只在结果里出现的列：类型取查询时从驱动的原始值记下的列类型。表结构里有的列
        # 只认表结构快照的类型，快照核对不过就不给——不拿结果列的类型顶上
        kinds = [c.get("column_types", {}).get(str(column)) for c in results if isinstance(c.get("column_types"), dict)]
        if kinds and isinstance(kinds[0], str):
            step.update(type=kinds[0], type_source="result")
    return step


def _describe_entity(step: dict[str, Any], snap: dict[str, Any], kind: str, name: str, table: Any, column: Any,
                     tables: list[Any]) -> None:
    """从表结构快照里补上同步时间、表的概况、字段的类型。"""
    if snap.get("synced_at"):
        step["synced_at"] = snap["synced_at"]
    if snap.get("truncated"):
        step["snapshot_truncated"] = True
    if kind == "table":
        info = _schema_table(snap, name) or {}
        step["columns"] = len([c for c in info.get("columns") or [] if isinstance(c, dict)])
        step["is_view"] = bool(info.get("is_view"))
        if info.get("comment"):
            step["comment"] = info["comment"]
        return
    owners = [table] if table else tables
    found = {str(t): (tinfo, _schema_column(tinfo, column)) for t in owners
             if (tinfo := _schema_table(snap, t)) is not None}
    types = {t: str(c.get("type") or "") for t, (_, c) in found.items() if c is not None}
    if table and table in found and found[table][1] is not None:
        tinfo, col = found[table]
        step.update(type=str(col.get("type") or ""), type_source="schema", nullable=bool(col.get("nullable", True)),
                    primary_key=col.get("name") in (tinfo.get("primary_key") or []))
        if col.get("comment"):
            step["comment"] = col["comment"]
    elif types:
        # 好几张表都有的同名字段：各表的类型分别列出，一样的话再给一个 type
        step["types"] = types
        if len(set(types.values())) == 1:
            step.update(type=next(iter(types.values())), type_source="schema")


def _scope_catalog(sealed: _Sealed, report_node: str) -> dict[str, Any]:
    """用封存的台账按报告节点同一套参数（祖先范围、evidence_from、entities）重建的目录。

    doc.catalog 只留了正文用到的实体，给「是不是想写…」找候选不够；运行状态（checkpoint）不在封存
    范围里，也不能用。图读不出来、找不到报告节点时退回全部封存台账。
    """
    from app.engine.nodes.report import report_catalog
    from app.engine.schema import GraphSpec

    try:
        spec = GraphSpec.model_validate(sealed.graph)
        node = spec.node_map().get(report_node)
    except Exception:  # noqa: BLE001 - 老运行的图可能已经不合现在的格式，退回不分范围
        node = None
    if node is None:
        return build_catalog(nodes={}, ledger=sealed.entries)
    return report_catalog({"evidence": sealed.entries, "nodes": {}, "input": {}}, spec, node)  # type: ignore[arg-type]


def _suspicious_step(seg: dict[str, Any], cite: dict[str, Any], sealed: _Sealed, report_node: str) -> dict[str, Any]:
    """可疑实体：照实说查过哪些地方都没有这个名字，附上最接近的已知名字（最多 3 个）当提示。

    提示只从封存的台账里找——一个编造的名字，提示里再冒出一个封存链外的名字，就是在帮它圆。
    """
    unsure = seg.get("issue") == "unverified_entity"
    name = str(seg.get("name") or cite.get("ref") or seg.get("text") or "").strip().strip("`")
    catalog = _scope_catalog(sealed, report_node)
    entities = [e for e in catalog.values() if isinstance(e, dict) and e.get("kind") in ENTITY_KINDS]
    origins = [o for e in entities for o in e.get("sources") or [] if isinstance(o, dict)]
    closest = []
    for alias in closest_entities(name, catalog) if name else []:
        found = catalog[alias]
        table = (found.get("locator") or {}).get("table") if found.get("kind") == "column" else None
        closest.append({"alias": alias, "kind": found.get("kind"), "name": found.get("name"),
                        **({"table": table} if table else {})})
    return {
        "step": "entity", "status": "unverified" if unsure else "unknown", "name": name,
        **({"kind": cite["kind"]} if cite.get("kind") in ENTITY_KINDS else {}),
        "reason": UNVERIFIED_ENTITY_REASON if unsure else UNKNOWN_ENTITY_REASON,
        "closest": closest,
        "checked": {"schemas": len({o.get("artifact") for o in origins if o.get("kind") == "schema"}),
                    "queries": len({o.get("alias") for o in origins if o.get("kind") in ("sql", "result")
                                    and o.get("alias")})},
    }


def _quote_step(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any], sealed: _Sealed) -> dict[str, Any]:
    """引文步骤：原话在哪次检索的哪条命中里、原文的 [起, 止)，带上那条命中的全文好高亮。

    检索快照要出现在封存范围内的 retrieve.end.artifact 或 retrieval 台账条目里；取回时复验哈希，
    再按记下的位置把原文切出来和引文重新比一次（空白归一化后逐字相等）。
    """
    source = dict(cite.get("source") or {})
    artifact = str(entry.get("artifact") or source.get("artifact") or "")
    locator = dict(cite.get("locator") or {})
    text = str(cite.get("rendered") or seg.get("text") or "")
    info = sealed.retrievals.get(artifact) if artifact else None
    step: dict[str, Any] = {
        "step": "quote", "alias": cite.get("alias"), "text": text, "source": source,
        "match": {k: locator.get(k) for k in ("hit", "start", "end")},
        "content": None, "collection": (info or {}).get("source") or entry.get("source"), "query": None,
        "node_id": (info or {}).get("node_id") or entry.get("node_id"), "artifact": artifact or None,
        "eid": cite.get("eid"), "eid_ok": bool(artifact) and cite.get("eid") == make_eid("quote", artifact, locator),
        "hash_ok": None, "match_ok": None, "sealed": False,
    }
    if info is None:
        return {**step, "note": UNSEALED_RETRIEVAL}
    snap, step["hash_ok"] = _fetch(artifact)
    hits = snap.get("hits") if isinstance(snap, dict) else None
    at, start, end = locator.get("hit"), locator.get("start"), locator.get("end")
    hit = hits[at] if isinstance(hits, list) and isinstance(at, int) and 0 <= at < len(hits) else None
    content = hit.get("content") if isinstance(hit, dict) else None
    if not isinstance(content, str) or not isinstance(start, int) or not isinstance(end, int):
        return {**step, "note": TAMPERED_RETRIEVAL if step["hash_ok"] is False else MISSING_RETRIEVAL}
    step.update(content=content, query=(snap or {}).get("query"),
                collection=(snap or {}).get("collection") or step["collection"],
                match_ok=0 <= start <= end <= len(content)
                and normalize_quote(content[start:end]) == normalize_quote(text))
    step["sealed"] = sealed.trusted and step["hash_ok"] is True and step["match_ok"] and step["eid_ok"]
    return step


def _note(seg: dict[str, Any], unit: dict[str, Any]) -> str:
    """片段为什么是现在这个状态，一句人话。"""
    issue = seg.get("issue")
    cite = seg.get("cite") or {}
    if issue == "uncited_number":
        return "这个数字没有出处：写作者直接写了数字，没有用引用标记，系统没法核对它"
    if issue == "unresolved_ref":
        return f"引用解析不了：{cite.get('reason') or '证据目录里没有它'}"
    if issue == "unknown_entity":
        return UNKNOWN_ENTITY_REASON
    if issue == "unverified_entity":
        return f"核对不了：{UNVERIFIED_ENTITY_REASON}"
    if seg.get("state") == "deterministic" and seg.get("kind") == "entity":
        lead = "系统在正文里认出的表名、字段名：" if seg.get("auto") else ""
        return (f"{lead}这个名字在本次运行冻结的表结构快照、查询用到的表或查询结果列里确实存在。"
                "只核对它存在、这次用过，不核对它的业务含义")
    if seg.get("state") == "deterministic" and seg.get("kind") == "quote":
        return "这段原话在知识库检索命中的片段里逐字出现（空白归一化后比对），由系统核对过"
    if seg.get("state") == "deterministic" and cite.get("kind") == "cell":
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
    cites = [str(c) for c in unit.get("cites") or []]
    if cites:
        return (f"这是结论句里的文字，这句挂了依据：{'、'.join(cites[:4])}{' 等' if len(cites) > 4 else ''}。"
                "依据支不支持这句话，要到后续版本由模型判断")
    return "这是结论句里的文字，这句没有挂依据。句子本身有没有依据要到后续版本由模型判断"


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence/audit：记录页的审计表，可以导出 JSON / CSV
# --------------------------------------------------------------------------

AUDIT_SCHEMA = "agentlab.evidence.audit/1"
BAD_FORMAT = "evidence_bad_format"
BAD_GROUP = "evidence_bad_group"
#: 分组的顺序固定：有出处 / 无证据 / 可疑实体 / 旧运行猜测
GROUPS = (("cited", "有出处"), ("none", "无证据"), ("suspicious", "可疑实体"), ("candidate", "旧运行猜测"))
#: 文档本身对不上的违规：不管落在哪个片段上都单独列一行，不能被那个片段「有出处」的行盖住
_INTEGRITY = frozenset({"bad_schema", "segment_mismatch", "structural_text", "render_mismatch", "eid_mismatch",
                        "state_mismatch"})
UNCITED_CLAIM = "这句结论没有挂依据（没有引用标记，也没有 [[see:]]）"
UNCITED_CLAIM_COUNTED = "这句结论没有挂依据：这份报告要求结论句挂依据（claims: require_citation），出具时计入缺口"
UNCITED_CLAIM_WITHHELD = "这句结论没有挂依据：出具契约要求结论句挂依据，没挂的按契约不予出具（on_uncited: withhold）"
UNCITED_CLAIM_REQUIRED = ("这句结论没有挂依据：这份报告要求结论句挂依据（claims: require_citation），不过没有出具契约"
                          "按它判档（没配契约，或者还没到出具那一步）")
LEGACY_UNMATCHED = "旧版出具里这个数字对不上任何指标"

#: CSV 的列：(表头, 行里的键)
CSV_COLUMNS = (
    ("分组", "group"), ("报告节点", "report"), ("成果字段", "field"), ("片段", "segment"), ("句子编号", "unit"),
    ("种类", "kind"), ("原文", "text"), ("状态", "state"), ("问题", "issue"), ("引用", "ref"), ("证据", "evidence"),
    ("证据种类", "evidence_kind"), ("证据编号", "eid"), ("工件", "artifact"), ("来源节点", "node_id"),
    ("已封存", "sealed"), ("句子", "sentence"), ("说明", "note"),
    # 整次运行的封存核对结果，每行都写：文件存下来以后响应头就没了，光看「已封存」分不出封存链断了还是没封存
    ("封存核对", "seal"),
)
SEAL_OK = "封存核对通过（封存于第 {seq} 条事件）"
SEAL_BROKEN = "封存核对没通过：封存之后有事件被改过、删过或插过"
SEAL_NONE = "未封存：运行还没跑完，或者停在人工审批"
_CSV_LABELS: dict[str, dict[Any, str]] = {
    "group": dict(GROUPS),
    "kind": {"number": "数字", "value": "值", "entity": "表名、字段名", "quote": "引文", "claim": "结论句",
             "violation": "违规"},
    "state": {"deterministic": "有出处", "none": "无证据", "candidate": "候选"},
    "issue": {"uncited_number": "没有出处的数字", "unresolved_ref": "引用解析不了", "unknown_entity": "可能是编造的名字",
              "unverified_entity": "核对不了", "uncited_claim": "结论句没挂依据", "no_candidate": "没找到候选",
              **{code: "文档对不上" for code in _INTEGRITY}},
    "evidence_kind": {"metric": "口径卡指标", "cell": "查询单元格", "input": "运行输入", "table": "表", "column": "字段",
                      "quote": "知识库原话", "query": "查询结果"},
    "sealed": {True: "是", False: "否", None: "—"},
}
#: 电子表格会把以这些字符开头的格子当公式算：报告、答案里的字来自模型，照原样写进去就能被人利用
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"^[+-]?[\d,.]+%?$")


@router.get("/{run_id}/evidence/audit")
async def evidence_audit(run_id: str, fmt: str | None = Query(None, alias="format"),
                         groups: str | None = None) -> Any:
    """审计视图：报告里每个有状态的片段按状态分组，附上封存核对的结果。

    ?groups=none,suspicious 只要这几组；?format=json / csv 作为附件下载（不带 format 是给页面用的 JSON）。
    每行的 sealed 是「背后的工件是封存范围内的事件交回过的，而且封存核对通过」，不逐件复验哈希——
    点开片段时的证据链才逐件复验。
    """
    wanted = _wanted_groups(groups)
    if fmt not in (None, "", "json", "csv"):
        raise CodedHTTPException(422, f"导出格式只能是 json 或 csv，写的是 {fmt!r}", BAD_FORMAT)
    sealed = await _sealed(run_id)
    reports = _reports(sealed)
    mode, extra = await _mode_of(sealed, reports)
    rows: list[dict[str, Any]] = []
    if mode == "cited":
        inputs = _input_payloads(sealed)
        issuance = _issuance_of(sealed)
        for report in reports:
            if report.doc is not None:
                rows.extend(_audit_doc(report, sealed, inputs, issuance))
    elif mode == "legacy_text":
        rows = _audit_guess(extra["legacy"])
    elif mode == "legacy_contract":
        legacy = extra["legacy"]
        field_name = legacy.get("field")
        text = (sealed.payload_of(sealed.nodes_of("output")) or {}).get(field_name) if field_name else None
        rows = _audit_contract(legacy, text)
    grouped = [{"key": key, "label": label, "count": len(members), "rows": members}
               for key, label in GROUPS if key in wanted
               for members in [[r for r in rows if r["group"] == key]]]
    body: dict[str, Any] = {
        "run_id": run_id, "schema": AUDIT_SCHEMA, "mode": mode,
        "seal": {**sealed.seal, "events": sealed.verdict.get("events"), "message": sealed.verdict.get("message")},
        "reports": [{"node_id": r.node_id, "doc_artifact": r.doc_artifact, "hash_ok": r.hash_ok,
                     "doc_sealed": sealed.trusted, "fields": r.fields, "claims": r.claims, "stats": r.stats}
                    for r in reports],
        "groups": grouped, "counts": {g["key"]: g["count"] for g in grouped},
        "total": sum(g["count"] for g in grouped),
        **({"note": extra["note"]} if extra.get("note") else {}),
        **({"legacy_note": extra["legacy"].get("note")} if isinstance(extra.get("legacy"), dict) else {}),
    }
    name = f"evidence-{run_id[:8]}"
    if fmt == "csv":
        seal = "ok" if sealed.trusted else "broken" if sealed.sealed else "unsealed"
        label = SEAL_OK.format(seq=sealed.seal["manifest_seq"]) if sealed.trusted \
            else SEAL_BROKEN if sealed.sealed else SEAL_NONE
        return Response(audit_csv([r for g in grouped for r in g["rows"]], seal=label),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"', "X-Evidence-Seal": seal})
    if fmt == "json":
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
    return body


def _wanted_groups(raw: str | None) -> set[str]:
    known = [key for key, _ in GROUPS]
    if not raw:
        return set(known)
    picked = {part.strip() for part in raw.split(",") if part.strip()}
    if unknown := picked - set(known):
        raise CodedHTTPException(422, f"分组只能是 {' / '.join(known)}，写的是 {'、'.join(sorted(unknown))}", BAD_GROUP)
    return picked


def audit_csv(rows: list[dict[str, Any]], *, seal: str | None = None) -> str:
    """审计行 → CSV 文本：中文表头，代码换成中文，RFC 4180 转义（逗号、引号、换行都进引号），开头带 BOM
    （Excel 靠它认出 UTF-8）。以公式字符开头、又不是一个数的格子前面加 '。

    seal 是整次运行的封存核对结论（SEAL_OK / SEAL_BROKEN / SEAL_NONE），填进每行的「封存核对」；不给就空着。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([head for head, _ in CSV_COLUMNS])
    for row in rows:
        line = []
        for _, key in CSV_COLUMNS:
            value = seal if key == "seal" else row.get(key)
            if value is None or isinstance(value, (str, bool)):
                value = _CSV_LABELS.get(key, {}).get(value, value)
            text = "" if value is None else str(value)
            if text.startswith(_FORMULA_LEAD) and not _PLAIN_NUMBER.match(text):
                text = "'" + text
            line.append(text)
        writer.writerow(line)
    return "\ufeff" + buf.getvalue()


def _claim_note(report: _Report, issuance: dict[str, Any] | None) -> str:
    """没挂依据的结论句那一行怎么说：按出具时实际生效的策略（_issuance.claims）说，和出具横幅一致。

    契约核对的就是这份报告时，以它记下的为准（策略可能来自契约、也可能被契约收紧）；没有契约核对它时，
    报告节点自己要求了也只能说「要求了」，不能说出具时计入缺口。"""
    if isinstance(issuance, dict) and issuance.get("mode") == "citations" \
            and (issuance.get("report") or {}).get("node_id") == report.node_id:
        claims = issuance.get("claims")
        if not isinstance(claims, dict) or claims.get("policy") != "require_citation":
            return UNCITED_CLAIM
        on_uncited = claims.get("on_uncited") or "degrade"
        return UNCITED_CLAIM_WITHHELD if on_uncited == "withhold" else \
            UNCITED_CLAIM if on_uncited == "ignore" else UNCITED_CLAIM_COUNTED
    return UNCITED_CLAIM_REQUIRED if report.claims == "require_citation" else UNCITED_CLAIM


def _audit_doc(report: _Report, sealed: _Sealed, inputs: dict[str, Any],
               issuance: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """一份报告的审计行：有出处、无证据、可疑实体的片段各一行；没挂依据的结论句一行；没有可画线文字的
    违规（结构片段里的数字、粗体和链接里的可疑名字、[[see:]] 里解析不了的依据）一行。按在正文里的位置排。"""
    doc = report.doc or {}
    catalog = doc.get("catalog") or {}
    markdown = str(doc.get("markdown") or "")
    field_name = "、".join(report.fields) or None
    rows: list[dict[str, Any]] = []
    listed: set[str] = set()
    sentences: dict[str, str] = {}
    for _, unit in iter_units(doc):
        span = unit.get("span") or [0, 0]
        sentence = markdown[span[0]:span[1]]
        sentences[str(unit.get("id"))] = sentence
        for seg in unit.get("segments") or []:
            state = seg.get("state")
            if state == "deterministic" and seg.get("cite"):
                group = "cited"
            elif state == "none" and seg.get("kind") != "structural":
                group = "suspicious" if seg.get("issue") in SUSPICIOUS else "none"
            else:
                continue
            listed.add(str(seg.get("id")))
            rows.append(_segment_row(group, report, field_name, seg, unit, sentence, catalog, sealed, inputs))
    claim_note = _claim_note(report, issuance)
    for claim in uncited_claims(doc):
        rows.append({"group": "none", "report": report.node_id, "field": field_name, "segment": None,
                     "unit": claim["unit"], "kind": "claim", "text": claim["text"], "state": "none",
                     "issue": "uncited_claim", "ref": None, "alias": None, "evidence_kind": None, "evidence": None,
                     "eid": None, "artifact": None, "node_id": None, "sealed": None, "span": claim["span"],
                     "sentence": claim["text"], "note": claim_note})
    for v in doc.get("violations") or []:
        if not isinstance(v, dict) or (str(v.get("segment")) in listed and v.get("code") not in _INTEGRITY):
            continue
        rows.append({"group": "suspicious" if v.get("code") in SUSPICIOUS else "none", "report": report.node_id,
                     "field": field_name, "segment": v.get("segment"), "unit": v.get("unit"), "kind": "violation",
                     "text": v.get("text") or v.get("ref") or "", "state": "none", "issue": v.get("code"),
                     "ref": v.get("ref"), "alias": None, "evidence_kind": None, "evidence": None, "eid": None,
                     "artifact": None, "node_id": None, "sealed": None, "span": v.get("span"),
                     "sentence": sentences.get(str(v.get("unit")), v.get("context") or ""), "note": v.get("message")})
    rows.sort(key=lambda r: ((r["span"] or [len(markdown)])[0], r["kind"] != "claim"))
    return rows


def _segment_row(group: str, report: _Report, field_name: str | None, seg: dict[str, Any], unit: dict[str, Any],
                 sentence: str, catalog: dict[str, Any], sealed: _Sealed, inputs: dict[str, Any]) -> dict[str, Any]:
    cite = seg.get("cite") or {}
    alias = cite.get("alias")
    entry = (catalog.get(alias) or {}) if alias else {}
    row: dict[str, Any] = {
        "group": group, "report": report.node_id, "field": field_name, "segment": seg.get("id"),
        "unit": unit.get("id"), "kind": seg.get("kind"), "text": seg.get("text"), "state": seg.get("state"),
        "issue": seg.get("issue"), "ref": seg.get("ref"), "alias": alias, "evidence_kind": cite.get("kind"),
        "evidence": None, "eid": cite.get("eid"), "artifact": None, "node_id": None, "sealed": None,
        "span": seg.get("span"), "sentence": sentence, "note": _note(seg, unit),
    }
    if group != "cited":
        return row
    kind = cite.get("kind")
    if kind == "cell":
        artifact = str(entry.get("artifact") or "")
        row.update(evidence=_cell_name(alias, cite.get("locator") or {}), artifact=artifact or None,
                   node_id=entry.get("node_id"), sealed=sealed.trusted and artifact in sealed.queries)
    elif kind == "quote":
        source = cite.get("source") or {}
        artifact = str(source.get("artifact") or entry.get("artifact") or "")
        title = source.get("title")
        row.update(evidence=f"{alias} · {title}" if title else alias, artifact=artifact or None,
                   node_id=entry.get("node_id"), sealed=sealed.trusted and artifact in sealed.retrievals)
    else:
        row.update(evidence=entry.get("label") or alias, artifact=entry.get("artifact"), node_id=entry.get("node_id"),
                   sealed=_entry_sealed(sealed, entry, inputs) if entry else False)
    return row


def _around(text: str, span: list[int]) -> str:
    return text[max(0, span[0] - 18):span[1] + 18].replace("\n", " ")


def _audit_guess(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    """legacy_text 的审计行：有候选的数字进「旧运行猜测」，没有的进「无证据」。候选只是猜的，
    所以这里没有 eid、没有「已封存」的说法——一律写明是猜测。"""
    rows = []
    for field_guess in legacy.get("fields") or []:
        text = str(field_guess.get("markdown") or "")
        for seg in field_guess.get("segments") or []:
            if seg.get("kind") != "number":
                continue
            candidates = list(seg.get("candidates") or [])
            top = candidates[0] if candidates else {}
            rows.append({
                "group": "candidate" if candidates else "none", "report": None, "field": field_guess.get("field"),
                "segment": seg.get("id"), "unit": None, "kind": "number", "text": seg.get("text"),
                "state": seg.get("state"), "issue": None if candidates else "no_candidate", "ref": None,
                "alias": None, "evidence_kind": top.get("kind"),
                "evidence": "；".join(f"{c.get('ref') or c.get('alias') or c.get('kind')} = {c.get('rendered')}"
                                     for c in candidates) or None,
                "eid": None, "artifact": top.get("artifact"), "node_id": top.get("node_id"), "sealed": None,
                "span": seg.get("span"), "sentence": _around(text, seg.get("span") or [0, 0]),
                "note": GUESS_NOTE if candidates else NO_CANDIDATE, "candidates": candidates,
            })
    return rows


def _audit_contract(legacy: dict[str, Any], text: Any = None) -> list[dict[str, Any]]:
    """legacy_contract 的审计行：旧契约按数值对上的进「旧运行猜测」（不是显式引用），对不上的进「无证据」。

    text 是认出来的那个成果字段的原文（legacy.field）：句子一栏取数字前后的原文，和 legacy_text 一样。
    trace_numbers 只给对不上的数记了 context，对上的没有；认不出字段时退回 context。"""
    def sentence(item: dict[str, Any]) -> str:
        if isinstance(text, str) and item.get("positioned"):
            return _around(text, [item["start"], item["end"]])
        return item.get("context") or ""

    rows = []
    for item in legacy.get("matched") or []:
        sources = list(item.get("sources") or [])
        if item.get("metric"):
            shown = "；".join(f"{s['ref']} = {s['rendered']}" for s in sources) or f"m:{item['metric']}"
        else:
            shown = "出处不唯一：候选 " + "、".join(str(c) for c in item.get("candidates") or [])
        rows.append({
            "group": "candidate", "report": None, "field": legacy.get("field"), "segment": None, "unit": None,
            "kind": "number", "text": item.get("token"), "state": "candidate", "issue": None, "ref": None,
            "alias": None, "evidence_kind": "metric", "evidence": shown, "eid": None,
            "artifact": sources[0]["artifact"] if len(sources) == 1 else None,
            "node_id": sources[0]["node_id"] if len(sources) == 1 else None,
            "sealed": all(s.get("sealed") for s in sources) if sources else False,
            "span": [item["start"], item["end"]] if item.get("positioned") else None,
            "sentence": sentence(item), "note": LEGACY_NOTE, "candidates": sources,
        })
    for item in legacy.get("unmatched") or []:
        rows.append({
            "group": "none", "report": None, "field": legacy.get("field"), "segment": None, "unit": None,
            "kind": "number", "text": item.get("token"), "state": "none", "issue": "uncited_number", "ref": None,
            "alias": None, "evidence_kind": None, "evidence": None, "eid": None, "artifact": None, "node_id": None,
            "sealed": None, "span": [item["start"], item["end"]] if item.get("positioned") else None,
            "sentence": sentence(item), "note": LEGACY_UNMATCHED,
        })
    return rows

