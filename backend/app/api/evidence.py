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

结论句的判定（四期）是模型给的，不是证据：正式运行的判定在报告文档里、随文档封存；探索运行
按需裁判（POST …/evidence/judge），判定作为 evidence.judged 事件追加在封存之后，只填补没判过的
句子。交给裁判的摘录同样只取封存范围内的工件。
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import re
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, get_args

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, update
from sqlalchemy.exc import StatementError

from app.api.coded import CodedHTTPException
from app.core.artifact_store import load
from app.core.events import EventType
from app.data import provenance as prov
from app.data import provenance_db
from app.data.engine import SnapshotTampered, engines, masked_columns
from app.data.names import name_key
from app.data.provenance_types import (
    DOC_PROVENANCE,
    Alert,
    CellRef,
    Chain,
    ChainProblem,
    LocateProblem,
    ProvenanceOut,
    RawState,
    Reason,
    Recheck,
    RelatedCheck,
    ReportRef,
    SelectRefusal,
    VersionView,
    alert,
    refuse,
)
from app.data.table_versions import SnapshotManifestMismatch, SnapshotMissing
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow
from app.engine import direct_select
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
    ledger_enabled,
    make_eid,
    normalize_quote,
    query_entry_fields,
    render_metric,
    schema_column,
    schema_table,
    sql_tables,
    table_fields,
    uncited_claims,
)
from app.engine.judge import FIELDS_MAX, SETTLED, UNSUPPORTED, VERDICTS, candidates, outdated
from app.engine.toolcalls import QUERY_PREFIX

router = APIRouter(prefix="/api/runs", tags=["evidence"])
logger = logging.getLogger(__name__)

SCHEMA = "agentlab.evidence/1"

RUN_NOT_FOUND = "run_not_found"
REPORT_NOT_FOUND = "evidence_report_not_found"
SEGMENT_NOT_FOUND = "evidence_segment_not_found"
DOC_MISSING = "evidence_doc_missing"
DOC_TAMPERED = "evidence_doc_tampered"

LEGACY_NOTE = "旧版出具：按数值匹配，不是显式引用。同一个值与多个指标相符时，出处不唯一"
NONE_NOTE = "本次运行没有报告文档，也没有出具契约，没有可展示的证据"
NO_DOC_NOTE = "本次运行的出具契约采用引用模式核对，但报告没有产出可核对的文档"

MASKED = "已遮罩"
MASK_NOTE = "这些列在数据源中设置了遮罩，面板上不显示原值。遮罩只减少暴露，不是安全边界"
#: 查询条目的数据源现在找不到了（改名或删掉了）：只能按快照里记下的、查询当时的遮罩来遮
SOURCE_GONE = "数据源「{source}」已不存在（可能已改名或删除），按查询时记录的遮罩处理"
#: 查询步骤给被引用的行前后各带几行
WINDOW = 2
#: 一个查询步骤最多给多少行：数组字段引用了一大段行时，窗口不能把整份快照搬过来
MAX_WINDOW_ROWS = 50
UNSEALED_QUERY = "这份查询快照不属于封存范围内的任何事件（封存前没有任何查询返回过它），不能作为证据展示"
TAMPERED_QUERY = "查询快照与哈希不一致，疑似被修改，不展示其中的行"
MISSING_QUERY = "无法读取查询快照"
UNSEALED_RETRIEVAL = "这份检索快照不属于封存范围内的任何事件（封存前没有任何检索返回过它），不能作为证据展示"
TAMPERED_RETRIEVAL = "检索快照与哈希不一致，疑似被修改，不展示原文"
MISSING_RETRIEVAL = "无法读取检索快照，或其中没有这条命中"


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
    #: 运行记录上的 manifest_seq 原值（老运行是 None）：按需裁判追加判定前核对封存没换过
    run_manifest_seq: int | None = None
    #: verify_manifest 的完整结论（审计表照实附上）
    verdict: dict[str, Any] = field(default_factory=dict)
    #: 运行记录上的几样：按需裁判只给跑完、封存了的探索运行，升级前发起的不给
    run_class: str | None = None
    status: str | None = None
    ledger_on: bool = False
    #: 按需裁判的判定（evidence.judged 事件），按先后：(seq, 节点, 载荷)。不管落在封存前后都收——
    #: 失败后接着跑的运行会把早先追加的判定封进新的清单里，它仍然是按需裁判的批注
    judged: list[tuple[int, str | None, dict[str, Any]]] = field(default_factory=list)

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
        manifest, manifest_seq = run.manifest_hash, run.manifest_seq
        run_class, status, ledger_on = run.run_class, run.status, ledger_enabled(run)
    verdict = await verify_manifest(run_id)
    bound = verdict.get("sealed_at") if verdict.get("sealed") else None
    # 没封存（还在跑、停在审批）的运行没有「封存之后」：现有的事件都算，但每件证据都标未封存
    events = [r for r in rows if bound is None or r[0] <= bound]
    out = _Sealed(run_id=run_id, graph=graph, events=events, seal={
        "sealed": bool(verdict.get("sealed")), "ok": verdict.get("ok"), "manifest_seq": bound,
        "legacy": bool(verdict.get("legacy", False))}, manifest_hash=manifest, run_manifest_seq=manifest_seq,
        verdict=verdict, run_class=run_class, status=status, ledger_on=ledger_on,
        judged=[(seq, nid, data) for seq, etype, nid, data in rows
                if etype == EventType.EVIDENCE_JUDGED and isinstance(data, dict)])
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
            **_judgement_of(report, sealed),
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
NO_CANDIDATE = "旧版答案中的这个数字，在本次运行封存的查询结果和口径卡中没有找到相同的值"


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
    chosen, (block, unit, seg) = _open_segment(sealed, report, segment_id)
    doc = chosen.doc or {}
    markdown = str(doc.get("markdown") or "")
    start, end = unit.get("span") or [0, 0]
    chain = await _chain(seg, doc, sealed, chosen.node_id)
    covered = sealed.trusted and all(step.get("sealed") for step in chain if "sealed" in step)
    masked = list(dict.fromkeys(c for step in chain if step.get("step") == "query" for c in step.get("masked") or []))
    verdict = _effective_verdicts(chosen, sealed)[0].get(str(unit.get("id")))
    return {
        "report": {"node_id": chosen.node_id, "doc_artifact": chosen.doc_artifact},
        "segment": {"id": seg["id"], "text": seg.get("text"), "kind": seg.get("kind"), "state": seg.get("state"),
                    "span": seg.get("span"), "unit": unit.get("id"),
                    **{k: seg[k] for k in ("ref", "issue", "strong", "cite") if k in seg}},
        "unit": {"id": unit.get("id"), "kind": unit.get("kind"), "text": markdown[start:end],
                 "span": unit.get("span"), "cites": unit.get("cites") or [],
                 **({"verdict": verdict} if verdict is not None else {}),
                 "on_demand": _on_demand(sealed, doc, str(unit.get("id")), verdict)},
        "block": {"id": block.get("id"), "type": block.get("type")},
        "chain": chain,
        "note": _note(seg, unit),
        "violations": [v for v in doc.get("violations") or []
                       if v.get("segment") == seg["id"] or (v.get("unit") == unit.get("id") and not v.get("segment"))],
        "seal": {"sealed": sealed.sealed, "ok": sealed.seal["ok"], "covered": covered},
        "redacted": {"columns": masked, "note": MASK_NOTE} if masked else {"columns": []},
    }


def _open_segment(sealed: _Sealed, wanted: str | None, segment_id: str
                  ) -> tuple[_Report, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """选报告、确认文档完好、找到片段：(报告, (块, 句子, 片段))。片段接口和推断来源接口共用同一套错误码。"""
    reports = _reports(sealed)
    if not reports:
        raise CodedHTTPException(404, "本次运行没有封存的报告文档，没有可查看的片段", REPORT_NOT_FOUND)
    chosen = _choose(reports, wanted, sealed)
    if chosen.hash_ok is False:
        raise CodedHTTPException(409, "报告文档与哈希不一致，疑似被修改，不能作为证据展示", DOC_TAMPERED)
    if chosen.doc is None:
        raise CodedHTTPException(404, "无法读取报告文档", DOC_MISSING)
    found = find_segment(chosen.doc, segment_id)
    if found is None:
        raise CodedHTTPException(404, "报告中没有这个片段", SEGMENT_NOT_FOUND)
    return chosen, found


def _choose(reports: list[_Report], wanted: str | None, sealed: _Sealed) -> _Report:
    if wanted:
        chosen = next((r for r in reports if r.node_id == wanted), None)
        if chosen is None:
            raise CodedHTTPException(404, "本次运行中没有所选报告撰写节点的文档", REPORT_NOT_FOUND)
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
        hidden = await _entity_hidden(entry, sealed, masks) if entry.get("kind") == "table" else set()
        return [_entity_step(seg, cite, entry, sealed, hidden=hidden)]
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
        # 只有直接引用的单元格才可能推断来源（P4-SPEC 2.8.2）。指标链里的查询步骤不带：指标片段的推断来源只会
        # 回「不是单元格」，带上提示等于让界面白发一次请求
        return [await _query_step(str(entry.get("artifact") or ""), [(locator.get("row"), locator.get("column"))],
                                  doc, sealed, masks, hint=doc.get("provenance") == DOC_PROVENANCE)]
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
        # 拿截断的查询结果整组算出来的：面板标「结果不完整」并写明原因。完整的指标没有这两个键
        **({"incomplete": True, "incomplete_reason": source.get("incomplete_reason") or ""}
           if source.get("incomplete") else {}),
        # 来源查询没通过 SQL 检查：面板标出来并写明原因（问题和改法在查询步骤的 checks 里）。没问题的没有这两个键
        **({"sql_check_failed": True, "sql_check_reason": source.get("sql_check_reason") or ""}
           if source.get("sql_check_failed") else {}),
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
                      masks: _Masks, *, hint: bool = False) -> dict[str, Any]:
    """查询步骤：被引用的格所在的行加前后各 WINDOW 行，高亮那几格，遮掉数据源设了遮罩的列。

    快照只认封存范围内的事件交回过的那些（tool.end.query_artifact、node.finished.evidence 的
    query 条目）；目录里写着、事件里追不到的，照实说不认，一行都不给。

    hint（期 4，P4-SPEC 2.8.2）：调用方是带文档标记的单元格片段时为真。这时快照若是上传表格的（有 data_version），
    步骤多一个 `provenance: true`，界面据此才去请求推断来源接口；其余情况一个键都不加，形状和期 4 之前完全相同
    （期 4 之前的文档、手工源、指标链都不多发请求）。不看封存状态：未封存的运行照常推断，响应里照实标明。

    checks：快照里记下的 SQL 检查结果（数据目录阶段 2B，见 _query_checks），只在查出问题时有这个键。
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
                column_types=snap.get("column_types") if isinstance(snap.get("column_types"), dict) else {},
                **({"provenance": True} if hint and snap.get("data_version") else {}),
                **({"checks": checks} if (checks := _query_checks(snap)) else {}))
    if len(index) > MAX_WINDOW_ROWS:
        index, step["window_truncated"] = index[:MAX_WINDOW_ROWS], True
    step["row_index"] = index
    step["row_offset"] = index[0] if index else 0
    step["rows"] = [[MASKED if i in masked else v for i, v in enumerate(rows[r])] if isinstance(rows[r], list)
                    else rows[r] for r in index]
    return step


#: 查询步骤里每条 SQL 检查结果交给界面的字段（data/sqlcheck.SqlCheck.as_dict 去掉 for_model）
_CHECK_FIELDS = ("code", "level", "message", "table", "column", "relation_id", "sql_excerpt")


def _query_checks(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """查询快照里记下的 SQL 检查结果（数据源查询工具执行后对照数据目录查的）。给模型的那句改法不上界面。
    快照里没有（没查出问题、或者是升级前的快照）返回空列表，步骤里就不加这个键。"""
    checks = snap.get("checks")
    if not isinstance(checks, list):
        return []
    return [{k: c[k] for k in _CHECK_FIELDS if k in c} for c in checks if isinstance(c, dict) and c.get("code")]


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence/segments/{segment_id}/provenance（期 4：推断的来源）
# --------------------------------------------------------------------------


@router.get("/{run_id}/evidence/segments/{segment_id}/provenance")
async def evidence_provenance(run_id: str, segment_id: str, report: str | None = None) -> dict[str, Any]:
    """点开报告里一个直接引用的查询单元格时，这一格来自原表的哪个工作表、哪一格（P4-SPEC 2.8.1）。

    选报告、错误码和片段接口相同（run_not_found、evidence_report_not_found、evidence_doc_tampered、
    evidence_doc_missing、evidence_segment_not_found）。其余情况一律 200：推断不出来不是错误，结论写在
    status、reason、alert 里。按需算、不缓存（2.9）：原件是否清除、数据文件是否被改都是当前状态，事后会变。

    返回 `asdict(ProvenanceOut)`：CellSource 的「from」在 Python 里叫 from_，asdict 已经换回「from」；返回注解
    写 dict，不写 ProvenanceOut，和其余证据路由一样不让框架按类型再校验、再序列化一遍（provenance_types 模块说明）。
    """
    sealed = await _sealed(run_id)
    chosen, (_block, _unit, seg) = _open_segment(sealed, report, segment_id)
    out = await segment_provenance(chosen, seg, sealed, masks=_Masks())
    return asdict(out)


#: R14 之后（绑定快照、取引擎、编译核对、回查、行级核对）会抛的三类异常，按 _snapshot_failure 的顺序映射
_SNAPSHOT_ERRORS: tuple[type[BaseException], ...] = (SnapshotMissing, SnapshotTampered)


def _snapshot_failure(exc: BaseException) -> tuple[Reason, Alert | None]:
    """R14 之后抛出的数据文件异常 → (原因, 标红提示)（P4-SPEC 2.3 的异常映射）。

    **顺序是守卫点**：SnapshotManifestMismatch 同时继承 SnapshotMissing 和引擎的 SnapshotTampered
    （table_versions.py 的定义），必须最先认，否则会被当成文件被改（db_tampered）或文件不在（snapshot_gone）：
    1. SnapshotManifestMismatch：登记的哈希与快照清单对不上 → chain_mismatch，标红；
    2. SnapshotTampered（含借连接时指纹不符）：数据文件与登记的版本不一致 → db_tampered，标红；
    3. SnapshotMissing：绑定时文件没了、记录不全 → snapshot_gone，不标红（被运行引用的版本受保护，出现多半是
       数据源被删或手工清理）。
    三种都是 table_only：数据版本一节只靠清单，照样给；不给格子，已经算出的行级状态一律丢掉（调用方负责）。
    不是这三类的异常原样抛出：不该被当成「推断不出来」吞掉。
    """
    if isinstance(exc, SnapshotManifestMismatch):
        return refuse("chain_mismatch"), alert("chain_mismatch")
    if isinstance(exc, SnapshotTampered):
        return refuse("db_tampered"), alert("db_tampered")
    if isinstance(exc, SnapshotMissing):
        return refuse("snapshot_gone"), None
    raise exc


class _Artifacts:
    """一次请求里取过的工件（P4-SPEC 2.9「同一工件只取一次」）。取回的结果按 _fetch 的口径记 (内容, hash_ok)。

    - `await get(id)`：在线程里取（快照、清单可能几十 KB 到几 MB，读文件、复验哈希不该卡事件循环，H9）；
    - `load(id)`：同步取，语义同 artifact_store.load（取不到 None，哈希不符抛 ValueError），给已经在线程里跑的
      纯函数当 loader 用（WP-B 的 resolve_chain 等）。两条路共用同一份备忘。
    """

    def __init__(self) -> None:
        self._memo: dict[str, tuple[Any, bool | None]] = {}

    async def get(self, artifact: str) -> tuple[Any, bool | None]:
        key = str(artifact or "")
        if not key:
            return None, None
        if key not in self._memo:
            self._memo[key] = await asyncio.to_thread(_fetch, key)
        return self._memo[key]

    def load(self, artifact: str) -> Any:
        key = str(artifact or "")
        if not key:
            return None
        if key not in self._memo:
            self._memo[key] = _fetch(key)
        content, ok = self._memo[key]
        if ok is False:
            raise ValueError(f"工件 {key[:12]} 与哈希不一致")
        return content


def _cell_ref(cite: dict[str, Any], entry: dict[str, Any]) -> CellRef | None:
    """片段本身是单元格引用（有 alias 和 row、column 齐全的定位）就给，与 status 无关（2.8.1「字段取值」）。"""
    locator = cite.get("locator") or {}
    row, column = locator.get("row"), locator.get("column")
    if cite.get("kind") != "cell" or not cite.get("alias") or not isinstance(row, int) or isinstance(row, bool) \
            or not isinstance(column, str) or not column:
        return None
    artifact = entry.get("artifact")
    return CellRef(alias=str(cite["alias"]), row=row, column=column, artifact=str(artifact) if artifact else None)


async def segment_provenance(report: _Report, seg: dict[str, Any], sealed: _Sealed, *,
                             masks: _Masks) -> ProvenanceOut:
    """一个片段的推断来源：按 P4-SPEC 2.3 的规则表顺序判断，第一条不满足的就是结论。

    **封存状态不进规则**（1.3 调整 11）：未封存（停在审批上）、封存核对不通过的运行照常判断，`sealed` 照实给
    sealed.trusted，界面另加一句。查询快照、表结构快照按「本次运行的事件交回过」认（sealed.queries、
    sealed.schemas，_sealed 对未封存的运行按全部事件拼），不经 _sealed_loader：它在 trusted 为假时一律不给，
    会把未封存的运行误判成清单无法读取（2.8.3）。

    这里判 R0–R4（只看文档、目录和两份快照），R5 起交给 _drill：清单链（provenance）、SQL 判据（direct_select）、
    遮罩、空值、数据文件（provenance_db）、找回格子和相关核对。
    """
    doc = report.doc or {}
    cite = seg.get("cite") or {}
    entry = (doc.get("catalog") or {}).get(cite.get("alias")) or {}
    base: dict[str, Any] = {
        "report": ReportRef(node_id=report.node_id, doc_artifact=report.doc_artifact),
        "segment": str(seg.get("id") or ""), "cell": _cell_ref(cite, entry), "sealed": sealed.trusted,
    }

    def none(code: Any) -> ProvenanceOut:
        return ProvenanceOut(status="none", reason=refuse(code), **base)

    # R0：期 4 之前组装的文档（没有标记）一律不推断，期 4 之前的面板不变（1.4）
    if doc.get("provenance") != DOC_PROVENANCE:
        return none("legacy_doc")
    # R1：只对直接引用的查询单元格下钻；代码节点、Agent 字段的取数（工具名不是 db_query__ 开头）不算
    if (cite.get("kind") != "cell" or cite.get("status") != "resolved" or seg.get("state") != "deterministic"
            or entry.get("kind") != "query" or not str(entry.get("tool") or "").startswith(QUERY_PREFIX)):
        return none("not_cell")
    artifacts = _Artifacts()
    # R2：查询快照是本次运行的事件交回过的，取回时哈希复验通过
    artifact = str(entry.get("artifact") or "")
    query, ok = await artifacts.get(artifact) if artifact in sealed.queries else (None, None)
    if ok is not True or not isinstance(query, dict):
        return none("not_sealed")
    # R3：上传表格的查询快照才有 data_version（datasource.py 只给上传源写）
    if not query.get("data_version"):
        return none("not_upload")
    # R4：表结构快照同样要是本次运行交回过、复验通过的；不是按配方导入的（简单导入、v0 迁移出的版本）没有溯源
    schema_id = str(query.get("schema_artifact") or "")
    schema, ok = await artifacts.get(schema_id) if schema_id in sealed.schemas else (None, None)
    if ok is not True or not isinstance(schema, dict):
        return none("not_sealed")
    if schema.get("import_mode") != "recipe":
        return none("simple_upload")
    return await _drill(base, seg=seg, entry=entry, query=query, schema=schema, artifacts=artifacts,
                        masks=masks, sealed=sealed)


async def _drill(base: dict[str, Any], *, seg: dict[str, Any], entry: dict[str, Any], query: dict[str, Any],
                 schema: dict[str, Any], artifacts: _Artifacts, masks: _Masks, sealed: _Sealed) -> ProvenanceOut:
    """R5–R17（P4-SPEC 2.3）：第一条不满足的就是结论。

    - R5、R6 只看内容寻址的工件（resolve_chain，放线程：要逐份取回清单、复验哈希）。链断了就什么来历都不给：
      status none、version 为 null，标红；
    - 之后的每一条都照样给数据版本（它只靠清单），不给格子：R7–R11 只看查询快照和冻结表结构，R12、R13 对照数据库
      里的数据文件记录，R14 起打开数据文件。R14 之后任何一步抛出的数据文件异常按 _snapshot_failure 的顺序映射，
      已经算出的格子、行级状态一律丢掉（2.3 末尾）；
    - 全部满足才给格子（inferred）。
    """
    # R5（取不回、哈希不符）、R6（链本身对不上）：resolve_chain 不往外抛，结论在 ChainProblem 里
    chain = await asyncio.to_thread(prov.resolve_chain, query, schema, artifacts.load)
    if isinstance(chain, ChainProblem):
        return ProvenanceOut(status="none", reason=refuse(chain.code, chain.detail), alert=alert(chain.code), **base)

    frozen = _frozen_tables(schema)
    tables = {name: _frozen_columns(meta) for name, meta in frozen.items()}
    sql = str(query.get("sql") or "")
    # 遮罩 = 数据源现在设的 + 查询当时记下的（同 _query_step 的口径：数据源改名、删掉了也照遮）
    source_name = (sealed.queries.get(str(entry.get("artifact") or "")) or {}).get("source") \
        or entry.get("source") or query.get("source")
    current, _found = await masks.of(source_name)
    hidden = current | _recorded_masks(query)
    async with SessionLocal() as session:
        states = await provenance_db.import_states(session, [p.import_id for p in chain.parts])
        record = await provenance_db.snapshot_record(session, chain.snapshot_id)
    # 版本页同一个口径的表：SQL 里出现的冻结表结构的表（tables_in 宁可多认，2.4）
    version = _version_of(chain, direct_select.tables_in(sql, tables), states, manifest_view=not hidden)

    def table_only(reason: Reason, red: Alert | None = None) -> ProvenanceOut:
        return ProvenanceOut(status="table_only", reason=reason, alert=red, version=version, **base)

    # R7：受限文法内的直接选取（识别器和执行这条 SQL 的 SQLite 读同一串记号）。传的是快照里原样存下的 SQL
    columns = [str(c) for c in query.get("columns") or []]
    picked = direct_select.recognize(sql, result_columns=columns, tables=tables)
    if isinstance(picked, SelectRefusal):
        return table_only(refuse(picked.code, picked.detail))
    # R8：被引用的结果列按第一次出现的位置对应选取项（locate_cell 也按第一次出现取）。识别器已拒绝重名，
    # 这里对不上只是防御
    locator = (seg.get("cite") or {}).get("locator") or {}
    row, column = locator.get("row"), locator.get("column")
    if not isinstance(column, str) or column not in columns or columns.index(column) >= len(picked.columns):
        return table_only(refuse("unparsed"))
    at = columns.index(column)
    target = picked.columns[at]
    # R9：表有主键，主键各列都以直接引用出现在结果里；结果各行的主键两两不同（与解析无关的兜底：同一张表经集合
    # 运算拼出来的结果可能重复主键）
    meta = frozen.get(picked.table)
    pk_names = [str(p) for p in (meta.get("primary_key") or [])] if isinstance(meta, dict) else []
    if not pk_names:
        return table_only(refuse("no_pk"))
    where = {name_key(c): j for j, c in enumerate(picked.columns)}
    missing = [p for p in pk_names if name_key(p) not in where]
    if missing:
        return table_only(refuse("pk_missing", 列=missing))
    rows = query.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(columns) for r in rows):
        return table_only(refuse("unparsed"))
    pk_at = [where[name_key(p)] for p in pk_names]
    if _repeats([tuple(r[j] for j in pk_at) for r in rows]):
        return table_only(refuse("multi_table", "duplicate_pk"))
    # R10：被引用的列、主键列、推断要用到的键列（维度、派生、常量：行标签原文、分段标题的定位文字都是它们的值）
    # 都不在遮罩里
    needed = [column, target, *pk_names, *_key_columns(chain, picked.table)]
    if any(str(n).lower() in hidden for n in needed):
        return table_only(refuse("masked"))
    # R11：被引用的值、主键各值都不是空值
    if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < len(rows):
        return table_only(refuse("unparsed"))
    value = rows[row][at]
    if value is None:
        return table_only(refuse("null_value"))
    pk = {p: rows[row][j] for p, j in zip(pk_names, pk_at)}
    if any(v is None for v in pk.values()):
        return table_only(refuse("null_pk"))
    # R12：数据文件记录在、没回收、文件在（不标红：被运行引用的版本受保护，出现多半是数据源被删或手工清理）
    if record is None or record.retired_at is not None or not record.db_path or not Path(record.db_path).is_file():
        return table_only(refuse("snapshot_gone"))
    # R13：登记哈希等于清单里期望的库哈希、source_id 一致（防「数据文件和登记哈希被一起改成一致的假值」：
    # 清单是内容寻址的，又挂在封存的表结构快照下面，改不了）。登记哈希为空也算不等。
    # Reason.detail 不带细分：契约只给 DETAILS 和 ChainProblem / LocateProblem 的细分留了位置，R13 规格只写
    # chain_mismatch，和异常映射（SnapshotManifestMismatch）同一个口径；哪一项对不上写进日志
    registered = str(record.db_sha256 or "").strip().lower()
    if not registered or registered != chain.expected_db_sha256.strip().lower():
        logger.warning("provenance: 快照 %s 的登记哈希与导入清单里的库哈希不一致", chain.snapshot_id)
        return table_only(refuse("chain_mismatch"), alert("chain_mismatch"))
    if str(record.source_id) != chain.source_id:
        logger.warning("provenance: 快照 %s 的 source_id 与导入清单不一致", chain.snapshot_id)
        return table_only(refuse("chain_mismatch"), alert("chain_mismatch"))
    try:
        return await _open_and_locate(base, table_only, chain=chain, record=record, version=version,
                                      states=states, hidden=hidden, sql=sql, table=picked.table, target=target,
                                      value=value, pk=pk)
    except _SNAPSHOT_ERRORS as exc:
        # R14 之后（绑定、取引擎、编译核对、回查、行级核对）任何一步：不给格子，已算出的行级状态一律丢掉
        reason, red = _snapshot_failure(exc)
        return table_only(reason, red)


async def _open_and_locate(base: dict[str, Any], table_only: Callable[..., ProvenanceOut], *, chain: Chain,
                           record: Any, version: VersionView, states: dict[str, dict[str, Any]], hidden: set[str],
                           sql: str, table: str, target: str, value: Any, pk: dict[str, Any]) -> ProvenanceOut:
    """R14–R17 和行级核对：要打开数据文件的几步。三类数据文件异常原样往外抛，由 _drill 统一映射。"""
    # R14：绑定快照（累积快照首次绑定时和快照清单交叉核对），经 EngineCache 打开数据文件（只读、immutable，本进程
    # 首次整份核对哈希）。之后的回查、行级核对都经同一个缓存键，读的就是查询读过的那个文件
    view = provenance_db.view_of(record, source_name=chain.source)
    await engines.get(view)
    # R15：第二道核对，与解析无关。读到的表恰好只有识别器认出的那一张；字节码里 ResultRow 恰好 1 个、没有 Yield
    # （UNION ALL 有多个 ResultRow，UNION、INTERSECT、EXCEPT 有 Yield）。编译不了（SQLite 自己报错）同样不下钻
    try:
        facts = await provenance_db.compile_facts(view, sql)
    except sqlite3.Error:
        logger.warning("provenance: 编译核对失败，只给表级来历", exc_info=True)
        return table_only(refuse("unparsed"))
    if {name_key(t) for t in facts.tables} != {name_key(table)}:
        return table_only(refuse("multi_table", "authorizer"))
    if facts.result_rows != 1 or facts.yields != 0:
        return table_only(refuse("multi_table", "compound"))
    # R16：按主键参数化回查，恰好一行，值和快照里那一格类型、值都相等
    try:
        found, recheck_sql, params = await provenance_db.recheck(view, table=table, column=target, pk=pk)
    except (sqlite3.Error, StatementError) as e:
        _reraise_snapshot_error(e)
        # 细分同样不进 Reason.detail（契约外的值），回查出错和回查没找到行给同一个原因，区别只在日志
        logger.warning("provenance: 回查失败，只给表级来历", exc_info=True)
        return table_only(refuse("recheck_missing"))
    if not found:
        return table_only(refuse("recheck_missing"))
    if len(found) > 1:
        return table_only(refuse("recheck_multiple"))
    rowid, now = found[0]
    if not _same_cell(now, value):
        # 值对不上先强制整份重算一次哈希（不走 EngineCache 的指纹捷径：原地改写同样字节数、再还原修改时间的改动，
        # 指纹认不出，P4-SPEC 2.6）。文件确实变了 → 标红；没变 → 这条 SQL 的含义和判据认定的不同，不标红
        if not await provenance_db.rehash(view):
            return table_only(refuse("db_tampered"), alert("db_tampered"))
        return table_only(refuse("recheck_mismatch"))
    # R17：从 (表, 列, rowid) 找回格子（并集 rowid 先换算到某一期）。纯函数，但要解析配方、还原回执，放线程
    located = await asyncio.to_thread(prov.locate, chain, table=table, column=target, rowid=rowid)
    if isinstance(located, LocateProblem):
        if located.code == "chain_mismatch":
            return table_only(refuse("chain_mismatch", located.detail), alert("chain_mismatch"))
        return table_only(refuse("no_lineage", located.detail))
    # 附带的格写出的是哪几列的值（R10 已按回执里的键列判过，这里按实际给出的再核一次，防回执和推断不一致）
    if any(str(f.column).lower() in hidden for f in located.cell.from_ if f.column):
        return table_only(refuse("masked"))
    plans = await asyncio.to_thread(prov.related_checks, chain, located, table=table, column=target)
    checks: list[RelatedCheck] = []
    for plan in plans:
        if plan.rule is None:
            checks.append(plan.check)
            continue
        # 行级状态按**快照库**的 rowid 跑（并集时是并集 rowid），条件和导入时的核对是同一段代码生成的
        try:
            status = await provenance_db.row_status(view, plan.rule, rowid)
        except (sqlite3.Error, StatementError) as e:
            _reraise_snapshot_error(e)
            logger.warning("provenance: 行级核对失败，这一行不给结论", exc_info=True)
            status = None
        checks.append(replace(plan.check, row_status=status))
    state = states.get(located.part.import_id) or {}
    cell = replace(located.cell, rowid=rowid, pk=pk, raw_purged=state.get("raw_state") == "purged",
                   recheck=Recheck(sql=recheck_sql, params=list(params), ok=True))
    version = replace(version, parts=[replace(p, has_row=p.import_id == located.part.import_id)
                                      for p in version.parts])
    return ProvenanceOut(status="inferred", version=version, cell_source=cell, checks=checks, **base)


def _reraise_snapshot_error(exc: BaseException) -> None:
    """SQLAlchemy 把连接池事件里抛出的异常包成 StatementError 时，数据文件的三类异常要原样交给 _drill 映射，
    不能被当成「这条 SQL 执行失败」吞掉（借连接时指纹不符抛的 SnapshotTampered 就在这一路上）。"""
    orig = getattr(exc, "orig", None)
    if isinstance(orig, _SNAPSHOT_ERRORS):
        raise orig from exc


def _frozen_tables(schema: dict[str, Any]) -> dict[str, Any]:
    tables = schema.get("tables") if isinstance(schema, dict) else None
    return tables if isinstance(tables, dict) else {}


def _frozen_columns(meta: Any) -> list[str]:
    """冻结表结构里一张表的列名，按定义顺序（`SELECT *` 要按这个顺序比对结果列）。"""
    cols = meta.get("columns") if isinstance(meta, dict) else None
    return [str(c["name"]) for c in cols or [] if isinstance(c, dict) and c.get("name") is not None]


def _recorded_masks(query: dict[str, Any]) -> set[str]:
    recorded = query.get("mask_columns")
    return {c.lower() for c in recorded if isinstance(c, str)} if isinstance(recorded, list) else set()


#: 推断要用到的键列（回执 tables[].columns[].role）：维度列的行标签原文、派生列由行标签解析、常量列取分段标题的
#: 定位文字，展示出来就是这几列的值（R10）
_KEY_COLUMN_ROLES = frozenset({"dim", "derive", "const"})


def _key_columns(chain: Chain, table: str) -> list[str]:
    """各期回执里这张表的键列（维度、派生、常量）。取全部各期的并集：R10 在知道格子落在哪一期之前判，宁可多遮。"""
    out: list[str] = []
    for part in chain.parts:
        receipt = part.manifest.get("receipt") if isinstance(part.manifest.get("receipt"), dict) else {}
        for t in receipt.get("tables") or []:
            if not isinstance(t, dict) or t.get("name") != table:
                continue
            out += [str(c["name"]) for c in t.get("columns") or []
                    if isinstance(c, dict) and c.get("role") in _KEY_COLUMN_ROLES and c.get("name")]
    return list(dict.fromkeys(out))


def _repeats(keys: list[tuple[Any, ...]]) -> bool:
    """主键元组有没有重复。按 Python 的相等比（5 和 5.0 算同一个）：宁可多判重复、少下钻。"""
    seen: set[Any] = set()
    for key in keys:
        try:
            marker: Any = key
            hash(marker)
        except TypeError:
            marker = repr(key)
        if marker in seen:
            return True
        seen.add(marker)
    return False


def _same_cell(now: Any, then: Any) -> bool:
    """回查的值和快照里那一格：类型和值都相等（int、float、str 分开比，8754 和 8754.0 不算相等；bool 不算数）。

    两边都经 engine._jsonable 同一口径规范化过（快照还经过一次 JSON 存取，int、float、str 原样回来），类型不同
    只可能是文件或 SQL 的含义变了，不能按数值宽松地放过。"""
    if isinstance(now, bool) or isinstance(then, bool):
        return False
    return type(now) is type(then) and now == then


def _version_of(chain: Chain, tables: list[str], states: dict[str, dict[str, Any]], *,
                manifest_view: bool) -> VersionView:
    """数据版本：清单里的部分由 provenance.version_view 给（内容寻址），原件状态、清除、作废、配方第几版取自导入
    记录（当前状态，界面标「当前状态」）。manifest_view：数据源设有任何遮罩时不在面板里出「查看导入清单」。"""
    view = prov.version_view(chain, tables=tables)
    parts = []
    for p in view.parts:
        state = states.get(p.import_id) or {}
        raw = state.get("raw_state")
        parts.append(replace(p, raw_state=raw if raw in get_args(RawState) else None, purged=state.get("purged"),
                             revoked=state.get("revoked"), recipe_seq=state.get("recipe_seq")))
    return replace(view, parts=parts, manifest_view=manifest_view)


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


#: 表结构快照里找表、找字段：和裁判的摘录同一套（engine/evidence.py）。表名实体步骤的字段清单也和裁判的
#: 摘录同一个上限（judge.FIELDS_MAX）
_schema_table = schema_table
_schema_column = schema_column


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


async def _entity_hidden(entry: dict[str, Any], sealed: _Sealed, masks: _Masks) -> set[str]:
    """表的字段清单要遮的列（小写）：数据源现在设的，加上这次运行封存的查询快照里、同一个数据源当时记下的
    （数据源改名、删掉了也照遮，和查询步骤同一个口径）。"""
    source = str(entry.get("source") or "")
    hidden, _ = await masks.of(source) if source else (set(), True)
    hidden = set(hidden)
    for artifact, info in sealed.queries.items():
        if not sealed.trusted:
            break
        snap, ok = _fetch(artifact)
        if ok is not True or not isinstance(snap, dict):
            continue
        if not source or str(info.get("source") or snap.get("source") or "") == source:
            recorded = snap.get("mask_columns")
            hidden |= {str(c).lower() for c in recorded if isinstance(c, str)} if isinstance(recorded, list) else set()
    return hidden


def _entity_step(seg: dict[str, Any], cite: dict[str, Any], entry: dict[str, Any], sealed: _Sealed, *,
                 hidden: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """实体步骤：这个名字出现在哪几次查询里（SQL 用到的表、结果的列）、表结构快照什么时候同步的、字段类型。

    来源（entry.sources）逐个核对：表结构快照要出现在封存范围内的 tool.end.schema_artifact 或 schema 台账
    条目里，查询快照要是封存范围内交回过的那几件。类型、同步时间只从核对过、复验过哈希的表结构快照取，
    取不到就不给。eid 背后的那件工件核对通过、名字确实在里面，这一步才算已封存。

    表另带字段清单 fields: [{name, type}]（同一份核对过的快照，最多 FIELDS_MAX 个，多出来的记 fields_more）：
    讲表结构的句子，读的人在「挂的依据」下直接看得到有哪些字段。hidden 里的列（遮罩）不列。
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
        _describe_entity(step, snapshots[0], kind, name, table, column, list(entry.get("tables") or []),
                         hidden=hidden)
    if kind == "column" and not any(o.get("kind") == "schema" for o in step["sources"]):
        # 聚合的别名这类只在结果里出现的列：类型取查询时从驱动的原始值记下的列类型。表结构里有的列
        # 只认表结构快照的类型，快照核对不过就不给——不拿结果列的类型顶上
        kinds = [c.get("column_types", {}).get(str(column)) for c in results if isinstance(c.get("column_types"), dict)]
        if kinds and isinstance(kinds[0], str):
            step.update(type=kinds[0], type_source="result")
    return step


def _describe_entity(step: dict[str, Any], snap: dict[str, Any], kind: str, name: str, table: Any, column: Any,
                     tables: list[Any], *, hidden: set[str] | frozenset[str] = frozenset()) -> None:
    """从表结构快照里补上同步时间、表的概况（含字段清单）、字段的类型。"""
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
        fields, more, masked = table_fields(info, hidden, limit=FIELDS_MAX)
        step["fields"] = fields
        if more:
            step["fields_more"] = more
        if masked:
            step["fields_masked"] = True
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
        return "这个数字没有出处：报告中直接写出了数字，而不是引用标记，系统无法核对"
    if issue == "unresolved_ref":
        return f"引用无法解析：{cite.get('reason') or '本次运行的证据中没有它'}"
    if issue == "unknown_entity":
        return UNKNOWN_ENTITY_REASON
    if issue == "unverified_entity":
        return f"无法核实：{UNVERIFIED_ENTITY_REASON}"
    if seg.get("state") == "deterministic" and seg.get("kind") == "entity":
        lead = "系统在正文中识别出的表名、字段名：" if seg.get("auto") else ""
        return (f"{lead}这个名字在本次运行冻结的表结构快照、查询用到的表或查询结果列中确实存在。"
                "仅核对它存在且本次用到，不核对其业务含义")
    if seg.get("state") == "deterministic" and seg.get("kind") == "quote":
        return "这段原话在知识库检索命中的片段中逐字出现（空白归一化后比对），已由系统核对"
    if seg.get("state") == "deterministic" and cite.get("kind") == "cell":
        return "该值由系统从查询快照中取出、按固定规则显示，未经模型转写"
    if seg.get("state") == "deterministic":
        return "数字由系统从证据中取出、按口径卡的格式显示，未经模型转写"
    kind = seg.get("kind")
    if kind == "structural":
        return "这是排版符号"
    if unit.get("kind") == "heading":
        return "这是标题"
    if unit.get("kind") == "connective":
        return "这是连接性文字，不陈述数据事实，无需证据"
    cites = [str(c) for c in unit.get("cites") or []]
    if cites:
        return (f"这是结论句中的文字，这句附有依据：{'、'.join(cites[:4])}{' 等' if len(cites) > 4 else ''}。"
                "依据是否支持这句话，系统无法确定性核对，只能由模型判断（模型的判断不是确定性的）")
    return "这是结论句中的文字，这句没有附依据。句子是否有依据，系统无法核对，只能由模型判断（模型的判断不是确定性的）"


# --------------------------------------------------------------------------
# 结论句的判定：封存内的（正式运行在节点里判的）和封存之后按需追加的
# --------------------------------------------------------------------------

JUDGE_FORMAL = "evidence_judge_formal"
JUDGE_UNSEALED = "evidence_judge_unsealed"
JUDGE_LEGACY = "evidence_judge_legacy"
JUDGE_BAD_UNITS = "evidence_judge_bad_units"
SEAL_BROKEN_CODE = "evidence_seal_broken"
#: 一次最多点名几句。一份报告通常几十句，再多就是请求写错了
JUDGE_MAX_UNITS = 200
#: 跑完了的几种终态。还在跑、停在审批、正在接着跑的运行，追加的判定会被封进下一次的清单，不判
_TERMINAL = ("succeeded", "failed", "cancelled")

FORMAL_JUDGED = "正式运行的结论句已在报告撰写节点内完成裁判，结果随报告一起封存，不能再按需裁判"
UNSEALED_JUDGE = "运行尚未结束并封存（仍在运行、停在人工审批或正在继续运行），请在运行结束后再请模型裁判"
LEGACY_JUDGE = "这次运行发起时系统尚未记录证据，不支持按需裁判"
BROKEN_JUDGE = "封存核对未通过：封存后有事件被修改、删除或插入，报告和证据已不可信，无法再请模型裁判"
JUDGED_ALREADY = "这句已裁判"
#: 这句封存后按需判过，但那次早于当前的裁判规则（没有规则版本，或者是旧取值「证据不支持」）：可以按当前规则重判
OUTDATED_JUDGE = "这句的判定早于当前的裁判规则，可以请模型按当前规则重新判断"
#: 裁判期间运行被接着跑了（失败的运行可以继续），或者接着跑完重新封存了：判定照样交回，不记进运行记录
NOT_RECORDED = "运行已继续执行，本次裁判结果未写入运行记录，刷新后将不再显示。请在运行结束并封存后重新裁判"
NOT_A_CLAIM = "不是结论句（标题、连接性文字、表格单元格或代码），无需模型裁判"
BAD_UNITS = (f'请求格式有误：units 应为句子编号列表，例如 {{"units": ["u4"]}}，一次最多 {JUDGE_MAX_UNITS} 句；'
             "report 应为报告撰写节点的 ID")
#: 触顶时告诉人怎么调：设置页「证据裁判」一组的各项上限都能调高，也都能设成不限
JUDGE_ADJUST = {
    "max_cost_usd": "请前往「设置 → 偏好设置 → 证据裁判」调高「每次点击的金额上限」，或设为不限",
    "daily_max_usd": "今日裁判费用已达每日上限：请前往「设置 → 偏好设置 → 证据裁判」调高「每日金额上限」或设为不限，"
                     "也可次日再裁判",
    "max_claims": "单次裁判的句数已达上限：请分几次点击，或前往「设置 → 偏好设置 → 证据裁判」调高「每份报告最多裁判句数」"
                  "（报告撰写节点的「结论句裁判」中已设置的，以节点为准）",
    "timeout_s": "裁判超时：请前往「设置 → 偏好设置 → 证据裁判」调高「每份报告的时长上限」或设为不限，然后再次点击",
}


def _verdict_ok(verdict: Any) -> bool:
    return isinstance(verdict, dict) and verdict.get("status") in VERDICTS


def _effective_verdicts(report: _Report, sealed: _Sealed
                        ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """这份报告每句的判定：(生效的, 其中封存之后按需追加的)。

    文档里的判定随文档封存（正式运行在节点里判的；探索运行的候选句记「未裁判 · 按需」）。按需裁判的判定在
    evidence.judged 事件里，只认指向这份文档、文档里确有其句的；它只填补没判过的句子——封存内判过的，后面
    追加什么都盖不掉，一条追加的事件改写不了封存的结论。同一句按需判过几次（先触顶、后判成），判成的为准，
    其次取最后一次。追加的判定一律标 post_seal。
    """
    doc = report.doc or {}
    ids = {str(u.get("id")) for _, u in iter_units(doc)}
    out = {str(u.get("id")): u["verdict"] for _, u in iter_units(doc) if _verdict_ok(u.get("verdict"))}
    # 旧数据里的 unsupported 同样算判过（SETTLED）：封存的判定用什么取值，都不能被追加的判定盖掉
    sealed_judged = {uid for uid, v in out.items() if v.get("status") in SETTLED}
    later: dict[str, dict[str, Any]] = {}
    for _, _, data in sealed.judged:
        if data.get("report") != report.node_id or data.get("doc_artifact") != report.doc_artifact:
            continue
        verdicts = data.get("verdicts") if isinstance(data.get("verdicts"), dict) else {}
        for uid, verdict in verdicts.items():
            uid = str(uid)
            if uid not in ids or uid in sealed_judged or not _verdict_ok(verdict):
                continue
            if later.get(uid, {}).get("status") in SETTLED and verdict.get("status") not in SETTLED:
                continue
            later[uid] = out[uid] = {**verdict, "post_seal": True}
    return out, later


def _judgement_of(report: _Report, sealed: _Sealed) -> dict[str, Any]:
    """证据图、审计表里每份报告带的结论句情况：写作时的策略、封存的裁判摘要、封存之后按需追加的判定，
    和最近一次按需裁判用的模型（judge_meta，没按需裁判过是 None）。"""
    doc = report.doc or {}
    return {"claims": report.claims, "judge": doc.get("judge") if isinstance(doc.get("judge"), dict) else None,
            "post_seal_verdicts": _effective_verdicts(report, sealed)[1] if report.doc is not None else {},
            "judge_meta": _judge_meta(report, sealed)}


def _writer_model(sealed: _Sealed, report: _Report) -> str | None:
    """这份报告的写作模型：文档的裁判摘要里记的（写作时当场记下），没有就取报告撰写节点封存的产出里的 model
    （实际用的模型 id，老文档也有）。都取不到是 None。"""
    judge = (report.doc or {}).get("judge")
    if isinstance(judge, dict) and isinstance(judge.get("writer_model"), str) and judge["writer_model"]:
        return judge["writer_model"]
    payload = sealed.payload_of([report.node_id]) or {}
    model = payload.get("model")
    return model if isinstance(model, str) and model else None


def _same_model(model: Any, writer: str | None) -> bool:
    return bool(model) and bool(writer) and model == writer


def _judge_meta(report: _Report, sealed: _Sealed) -> dict[str, Any] | None:
    """最近一次按需裁判（evidence.judged，指向这份文档的）用的模型：{model, same_model, priced, writer_model}。

    事件里没记 same_model、writer_model 的（改造前追加的），按这份报告的写作模型补上。"""
    data = next((d for _, _, d in reversed(sealed.judged)
                 if d.get("report") == report.node_id and d.get("doc_artifact") == report.doc_artifact), None)
    if data is None:
        return None
    writer = data.get("writer_model") if "writer_model" in data else _writer_model(sealed, report)
    same = data.get("same_model") if isinstance(data.get("same_model"), bool) else _same_model(data.get("model"), writer)
    return {"model": data.get("model"), "same_model": same,
            "priced": data.get("priced") if isinstance(data.get("priced"), bool) else None, "writer_model": writer}


def _on_demand(sealed: _Sealed, doc: dict[str, Any], uid: str, verdict: dict[str, Any] | None) -> dict[str, Any]:
    """这句能不能「请模型判断」：{available, reason, message}。和 POST …/evidence/judge 同一套条件。"""
    def no(reason: str, message: str) -> dict[str, Any]:
        return {"available": False, "reason": reason, "message": message}

    if sealed.run_class == "formal":
        return no("formal", FORMAL_JUDGED)
    if not sealed.ledger_on:
        return no("legacy", LEGACY_JUDGE)
    if not candidates(doc, [uid]):
        return no("not_a_claim", f"这句{NOT_A_CLAIM}")
    stale = _stale(verdict)
    if isinstance(verdict, dict) and verdict.get("status") in SETTLED and not stale:
        return no("judged", JUDGED_ALREADY)
    if not sealed.sealed or sealed.status not in _TERMINAL:
        return no("unsealed", UNSEALED_JUDGE)
    if not sealed.trusted:
        return no("seal_broken", BROKEN_JUDGE)
    if stale:
        return {"available": True, "reason": "outdated", "message": OUTDATED_JUDGE}
    return {"available": True, "reason": None, "message": None}


def _stale(verdict: Any) -> bool:
    """封存之后按需追加的判定早于当前规则：按需裁判当它还没按当前规则判过，重判一次（不按摘录内容判断）。

    封存的判定（文档里的，post_seal 为 False）不在此列：追加的判定盖不掉它，版本再旧也不重开。"""
    return isinstance(verdict, dict) and bool(verdict.get("post_seal")) and outdated(verdict)


def _sealed_loader(sealed: _Sealed, *, chain: bool = False) -> Callable[[str], Any]:
    """给裁判取证据的 loader：只认封存范围内的事件交回过的工件（查询、表结构、检索快照和台账里记过的），
    取回时照旧复验哈希。别的一律当取不到——人为插进工件表的行、封存之后才追加的事件引用的快照，
    都不能送进裁判的摘录。报告文档里的目录也不例外：目录写着哪件工件，不等于它在封存链上。

    chain（期 4，P4-SPEC 2.8.3）：只有带文档标记的文档传真。这时另外认封存范围内的表结构快照经哈希链列出的
    导入清单、快照清单（以及快照清单各期的导入清单）：这些 id 写在内容寻址、又在封存范围内的表结构快照里，属于
    封存链的延伸，不是数据库列。扩展集合**惰性**计算：第一次被要一件不在基本范围里的工件时才算，同一个 loader
    只算一次（一次请求一个 loader）。chain 为假时和期 4 之前完全相同，期 4 之前的文档不会因为任何一份清单出错。
    """
    allowed = {*sealed.queries, *sealed.schemas, *sealed.retrievals, *(str(a) for _, a in sealed.ledger)}
    extended: set[str] | None = None

    def loader(artifact: str) -> Any:
        nonlocal extended
        key = str(artifact)
        if not sealed.trusted:
            return None
        if key in allowed:
            return load(key)
        if not chain:
            return None
        if extended is None:
            extended = _chain_set(sealed)
        return load(key) if key in extended else None
    return loader


def _chain_set(sealed: _Sealed) -> set[str]:
    """封存范围内每份表结构快照经哈希链列出的清单 id 的并集。表结构快照取不回、哈希不符的跳过（不在链上）。"""
    out: set[str] = set()
    for schema_id in sealed.schemas:
        try:
            schema = load(schema_id)
        except ValueError:
            continue
        if isinstance(schema, dict):
            out |= _chain_artifacts(schema, load)
    return out


def _chain_artifacts(schema: dict[str, Any], loader: Callable[[str], Any]) -> set[str]:
    """一份表结构快照经哈希链列出的清单 id：provenance.chain_artifacts（永不抛异常，坏清单视为不在链上）。

    按名字现取（不用模块顶部绑定的 prov）：测试可以在 sys.modules 里换掉这个模块，核对这里确实委托给它。"""
    from app.data.provenance import chain_artifacts

    return set(chain_artifacts(schema, loader))


def _node_judge(sealed: _Sealed, node_id: str) -> dict[str, Any]:
    """这次运行的图上报告节点写的 judge（裁判模型、句数和时长上限）；图读不出来就当没写。"""
    from app.engine.nodes.report import report_judge
    from app.engine.schema import GraphSpec

    try:
        spec = GraphSpec.model_validate(sealed.graph)
        node = spec.node_map().get(node_id)
    except Exception:  # noqa: BLE001 - 老运行的图可能已经不合现在的格式
        node = None
    return report_judge(spec, node) if node is not None else {}


class _RunLocks:
    """每次运行一把锁：同一次运行的按需裁判排队，连点两下时后一下看得到前一下记下的判定，不重复花钱。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str):  # type: ignore[no-untyped-def]
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key], self._locks[key]


_JUDGING = _RunLocks()


def _judge_request(payload: Any, report: str | None) -> tuple[list[str], str | None]:
    if not isinstance(payload, dict):
        raise CodedHTTPException(422, BAD_UNITS, JUDGE_BAD_UNITS)
    units = payload.get("units")
    wanted = report if payload.get("report") is None else payload["report"]
    if not isinstance(units, list) or not units or len(units) > JUDGE_MAX_UNITS \
            or not all(isinstance(u, str) and u.strip() for u in units) \
            or (wanted is not None and not isinstance(wanted, str)):
        raise CodedHTTPException(422, BAD_UNITS, JUDGE_BAD_UNITS)
    return list(dict.fromkeys(u.strip() for u in units)), wanted or None


@router.post("/{run_id}/evidence/judge")
async def judge_units(run_id: str, payload: Any = Body(None), report: str | None = None) -> dict[str, Any]:
    """探索运行里按需裁判几句结论：请求体 {units: ["u4"], report?: 报告节点 id}。

    判过的句子直接给已有的判定，不再调用；没判过的按设置里每次点击的金额上限、每份报告的句数和时长上限、
    全局每日上限去判，触顶照实说「已到上限」，不是报错。问过模型的这一次记一条 evidence.judged 事件，
    落在封存之后（post_seal: true）——封存核对只覆盖 manifest_seq 之前的事件，追加它 verify 照旧一致；
    封存的报告文档一个字都不改。交给裁判的证据只取封存范围内的工件，数据源遮罩的列不给。
    """
    units, wanted = _judge_request(payload, report)
    async with _JUDGING.hold(run_id):
        sealed = await _sealed(run_id)
        if sealed.run_class == "formal":
            raise CodedHTTPException(409, FORMAL_JUDGED, JUDGE_FORMAL)
        if not sealed.ledger_on:
            raise CodedHTTPException(409, LEGACY_JUDGE, JUDGE_LEGACY)
        if not sealed.sealed or sealed.status not in _TERMINAL:
            raise CodedHTTPException(409, UNSEALED_JUDGE, JUDGE_UNSEALED)
        if not sealed.trusted:
            raise CodedHTTPException(409, BROKEN_JUDGE, SEAL_BROKEN_CODE)
        reports = _reports(sealed)
        if not reports:
            raise CodedHTTPException(404, "这次运行没有封存的报告文档，没有可以判断的句子", REPORT_NOT_FOUND)
        chosen = _choose(reports, wanted, sealed)
        if chosen.hash_ok is False:
            raise CodedHTTPException(409, "报告文档与哈希不一致，疑似被修改，不能作为证据展示", DOC_TAMPERED)
        if chosen.doc is None:
            raise CodedHTTPException(404, "无法读取报告文档", DOC_MISSING)
        return await _judge_now(sealed, chosen, units)


async def _judge_now(sealed: _Sealed, report: _Report, units: list[str]) -> dict[str, Any]:
    from app.engine import judge as judging

    doc = report.doc or {}
    catalog = doc.get("catalog") or {}
    known = _effective_verdicts(report, sealed)[0]
    # 当前规则下判过的直接给；早于当前规则的（封存之后追加的旧判定）重判一次，和片段接口的 outdated 同一条件
    reused = [u for u in units if known.get(u, {}).get("status") in SETTLED and not _stale(known.get(u))]
    pending = [u for u in units if u not in reused]
    verdicts = {u: known[u] for u in reused}
    node_judge = _node_judge(sealed, report.node_id)
    async with SessionLocal() as session:
        settings = await judging.judge_settings(session)
    budget = judging.click_budget(settings, node_judge)
    writer = _writer_model(sealed, report)
    outcome: dict[str, Any] | None = None
    skipped: dict[str, str] = {}
    event = None
    lost = False
    if pending:
        # 带期 4 标记的文档才让裁判取得到哈希链上的清单（摘录的来历行要用）；老文档的 loader 和以前完全相同
        loader = _sealed_loader(sealed, chain=doc.get("provenance") == DOC_PROVENANCE)
        masked = await judging.source_masks(catalog, loader=loader)
        request = judging.prepare(doc, catalog, units=pending, loader=loader, masked=masked)
        skipped = dict(request.skipped)
        if request.cands:
            async with SessionLocal() as session:
                spec = await judging.judge_model_spec(session, node_judge, settings=settings)
            outcome = await judging.run_request(request, spec=spec, budget=budget, post_seal=True, click=True)
            for uid, verdict in outcome["verdicts"].items():
                # 重判没判成（触顶、模型不可用）：旧判定照旧生效，和证据图、片段接口说的一样
                older = known.get(uid)
                keep = verdict.get("status") not in SETTLED and isinstance(older, dict) and older.get("status") in SETTLED
                verdicts[uid] = older if keep else verdict
            outcome["writer_model"] = writer
            outcome["same_model"] = _same_model(outcome.get("model"), writer)
            if outcome["calls"] or outcome["cost_usd"]:
                # 没问模型（触顶、模型没配好）就没有判定可记：说给点的人听，不往运行记录里追加
                event = await _append_judged(sealed, report, pending, outcome)
                lost = event is None
    limits = list(outcome["limits_hit"]) if outcome else []
    message = _judge_message(outcome, budget, skipped)
    if lost:
        message = "；".join(filter(None, [NOT_RECORDED, message]))
    return {
        "run_id": sealed.run_id, "report": {"node_id": report.node_id, "doc_artifact": report.doc_artifact},
        "units": units, "verdicts": {u: verdicts[u] for u in units if u in verdicts}, "reused": reused,
        "judged": [u for u in pending if (outcome or {}).get("verdicts", {}).get(u, {}).get("status") in SETTLED],
        "skipped": skipped, "unjudged": dict(outcome["unjudged"]) if outcome else {},
        "limits_hit": limits, "limited": bool(limits), "message": message,
        "adjust": [JUDGE_ADJUST[r] for r in limits if r in JUDGE_ADJUST],
        "gaps": list(outcome["gaps"]) if outcome else [], "notes": list(outcome["notes"]) if outcome else [],
        "model": (outcome or {}).get("model"), "priced": (outcome or {}).get("priced"),
        # 裁判模型和这份报告的写作模型相同：判定照常出，界面上标出来（审查缺乏独立性）
        "same_model": bool((outcome or {}).get("same_model")), "writer_model": writer,
        "cost_usd": (outcome or {}).get("cost_usd", 0.0), "calls": (outcome or {}).get("calls", 0),
        "duration_ms": (outcome or {}).get("duration_ms", 0),
        "budget": budget.as_dict() if outcome else None, "event": event, "post_seal": True,
        "spend": {**await judging.daily_spend(), "daily_max_usd": settings.get("daily_max_usd")},
    }


def _judge_message(outcome: dict[str, Any] | None, budget: Any, skipped: dict[str, str]) -> str | None:
    """这次点击的一句话结论：触顶、没跑成、点的不是结论句；判成了（或者全是判过的）就没什么要说的。"""
    from app.engine.judge import _limit_label

    if outcome and outcome["limits_hit"]:
        labels = "、".join(_limit_label(r, budget, click=True) for r in outcome["limits_hit"])
        n = sum(outcome["unjudged"].get(r, 0) for r in outcome["limits_hit"])
        return f"已达上限（{labels}）：{n} 句未裁判，已裁判的结果保留"
    if outcome and outcome["gaps"]:
        return "结论句裁判未完成：" + "；".join(outcome["gaps"])
    parts = []
    if others := [u for u, why in skipped.items() if why == "not_a_claim"]:
        parts.append(f"所选句子中有 {len(others)} 句{NOT_A_CLAIM}")
    if missing := [u for u, why in skipped.items() if why == "not_found"]:
        parts.append(f"报告中没有所选的 {len(missing)} 句")
    return "；".join(parts) or None


async def _append_judged(sealed: _Sealed, report: _Report, units: list[str],
                         outcome: dict[str, Any]) -> dict[str, Any] | None:
    """记一条 evidence.judged：问过模型的这一次（连同没判成的句子），接在运行记录的最后一条后面。

    只接在这次点击开始时的那份封存后面：裁判要花几秒（时长不限时更久），这中间有人点了「继续运行」
    （失败的运行可以接着跑），再追加就落进一次活着的运行中间，还会被封进下一份清单；接着跑完、重新封存了
    也一样，这次的判定依据的已经不是最新的封存。这两种都不记，返回 None（判定照样交回给点的人）。
    核对和写入是同一个事务里的条件更新，和接着跑占住运行（runner._claim）抢的是同一行：谁先提交算谁的。
    """
    from app.core.bus import bus
    from app.engine.runner import _stage, run_manager

    same_seal = (Run.manifest_seq.is_(None) if sealed.run_manifest_seq is None
                 else Run.manifest_seq == sealed.run_manifest_seq,
                 Run.manifest_hash.is_(None) if sealed.manifest_hash is None
                 else Run.manifest_hash == sealed.manifest_hash)
    async with SessionLocal() as session:
        run = await session.get(Run, sealed.run_id)
        if run is None:
            return None
        # 进程重启过的话内存里的序号从 0 起：接在库里最后一条后面，不和已有的撞号
        bus.set_seq(sealed.run_id, run.last_seq or 0)
        event = run_manager._event(
            sealed.run_id, EventType.EVIDENCE_JUDGED, node_id=report.node_id, data={
                "report": report.node_id, "doc_artifact": report.doc_artifact, "units": units,
                "verdicts": outcome["verdicts"],
                "keys": {u: outcome["keys"][u] for u in outcome["verdicts"] if u in outcome["keys"]},
                "model": outcome["model"], "priced": outcome["priced"], "same_model": outcome["same_model"],
                "writer_model": outcome["writer_model"], "cost_usd": outcome["cost_usd"],
                "calls": outcome["calls"], "duration_ms": outcome["duration_ms"], "budget": outcome["budget"],
                "limits_hit": outcome["limits_hit"], "unjudged": outcome["unjudged"], "gaps": outcome["gaps"],
                "notes": outcome["notes"], "skipped": outcome["skipped"], "post_seal": True})
        claimed = await session.execute(
            update(Run).where(Run.id == sealed.run_id, Run.status.in_(_TERMINAL), *same_seal)
            .values(last_seq=event.seq))
        if claimed.rowcount != 1:
            # 运行已经不是那份封存下的终态了。烧掉一个序号不要紧：不落库的事件（令牌流）本来就占序号，
            # 序号只要求递增、不撞号
            await session.rollback()
            return None
        await _stage(session, event, run)
        await session.commit()
    await bus.publish(event)
    return {"seq": event.seq}


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/evidence/audit：记录页的审计表，可以导出 JSON / CSV
# --------------------------------------------------------------------------

AUDIT_SCHEMA = "agentlab.evidence.audit/1"
BAD_FORMAT = "evidence_bad_format"
BAD_GROUP = "evidence_bad_group"
#: 分组的顺序固定：有出处 / 无证据 / 可疑名称 / 按数值猜测（叫法同前端 lib/terms.ts 的 EVIDENCE_AUDIT_TEXT.groups）
GROUPS = (("cited", "有出处"), ("none", "无证据"), ("suspicious", "可疑名称"), ("candidate", "按数值猜测"))
#: 文档本身对不上的违规：不管落在哪个片段上都单独列一行，不能被那个片段「有出处」的行盖住
_INTEGRITY = frozenset({"bad_schema", "segment_mismatch", "structural_text", "render_mismatch", "eid_mismatch",
                        "state_mismatch"})
UNCITED_CLAIM = "这句结论没有附依据（既没有引用，也没有注明依据）"
UNCITED_CLAIM_COUNTED = "这句结论没有附依据。本报告要求结论句附依据，出具时计入缺口"
UNCITED_CLAIM_WITHHELD = "这句结论没有附依据，按出具契约不予出具"
UNCITED_CLAIM_REQUIRED = ("这句结论没有附依据。本报告要求结论句附依据，但目前没有出具契约据此判档"
                          "（未配置契约，或尚未到出具步骤）")
#: claims: judge 在挂依据这件事上和 require_citation 一样严（出口按 on_uncited 计入缺口）
UNCITED_CLAIM_JUDGED = "这句结论没有附依据。本报告的结论句由模型裁判，同样要求附依据，出具时计入缺口"
UNCITED_CLAIM_JUDGE_REQUIRED = ("这句结论没有附依据。本报告的结论句由模型裁判，同样要求附依据，但目前没有出具契约"
                                "据此判档（未配置契约，或尚未到出具步骤）")
LEGACY_UNMATCHED = "旧版出具中这个数字与任何指标都不相符"
#: 模型判定有问题的结论句各一行（生效的判定：封存的，没有就是封存后按需追加的）：判定 → 审计行的 issue。
#: 旧数据里的 unsupported（当时没分矛盾和不足）记 unsupported_claim，说法照旧是「证据不支持」
CLAIM_ISSUE = {"contradicted": "contradicted_claim", "insufficient": "insufficient_claim",
               UNSUPPORTED: "unsupported_claim"}
CLAIM_VERDICT_NOTE = {"contradicted": "模型判断：证据与这句结论相矛盾", "insufficient": "模型判断：证据不足，无法判断这句结论",
                      UNSUPPORTED: "模型判断：证据不支持这句结论"}
CLAIM_POST_SEAL = "该判定为封存后按需追加，不在封存范围内"

#: CSV 的列：(表头, 行里的键)
CSV_COLUMNS = (
    ("分组", "group"), ("报告节点", "report"), ("成果字段", "field"), ("片段编号", "segment"), ("句子编号", "unit"),
    ("种类", "kind"), ("原文", "text"), ("状态", "state"), ("问题", "issue"), ("引用", "ref"), ("证据", "evidence"),
    ("证据种类", "evidence_kind"), ("证据编号", "eid"), ("工件", "artifact"), ("来源节点", "node_id"),
    ("已封存", "sealed"), ("句子", "sentence"), ("说明", "note"),
    # 整次运行的封存核对结果，每行都写：文件存下来以后响应头就没了，光看「已封存」分不出封存链断了还是没封存
    ("封存核对", "seal"),
)
SEAL_OK = "封存核对通过（封存于第 {seq} 条事件）"
SEAL_BROKEN = "封存核对未通过：封存后有事件被修改、删除或插入"
SEAL_NONE = "未封存：运行尚未结束，或正停在人工审批"
_CSV_LABELS: dict[str, dict[Any, str]] = {
    "group": dict(GROUPS),
    "kind": {"number": "数字", "value": "值", "entity": "表名、字段名", "quote": "引文", "claim": "结论句",
             "violation": "违规"},
    "state": {"deterministic": "有出处", "none": "无证据", "candidate": "候选"},
    "issue": {"uncited_number": "没有出处的数字", "unresolved_ref": "引用无法解析", "unknown_entity": "疑似不存在的名称",
              "unverified_entity": "无法核实", "uncited_claim": "结论句未附依据", "no_candidate": "未找到候选",
              "contradicted_claim": "证据相矛盾", "insufficient_claim": "证据不足", "unsupported_claim": "证据不支持",
              **{code: "文档不一致" for code in _INTEGRITY}},
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
        raise CodedHTTPException(422, f"导出格式只能是 json 或 csv，当前为「{fmt}」", BAD_FORMAT)
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
                     "doc_sealed": sealed.trusted, "fields": r.fields, "stats": r.stats, **_judgement_of(r, sealed)}
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
        raise CodedHTTPException(422, f"分组只能是 {' / '.join(known)}，当前为「{'、'.join(sorted(unknown))}」", BAD_GROUP)
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
        if not isinstance(claims, dict) or claims.get("policy") not in ("require_citation", "judge"):
            return UNCITED_CLAIM
        on_uncited = claims.get("on_uncited") or "degrade"
        return UNCITED_CLAIM_WITHHELD if on_uncited == "withhold" else \
            UNCITED_CLAIM if on_uncited == "ignore" else \
            UNCITED_CLAIM_JUDGED if claims.get("policy") == "judge" else UNCITED_CLAIM_COUNTED
    if report.claims == "judge":
        return UNCITED_CLAIM_JUDGE_REQUIRED
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
    rows.extend(_verdict_rows(report, sealed, field_name, markdown))
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


def _verdict_rows(report: _Report, sealed: _Sealed, field_name: str | None, markdown: str) -> list[dict[str, Any]]:
    """模型判定证据相矛盾、证据不足（旧数据里的证据不支持）的结论句，各一行，进「无证据」一组（问题）。

    判定是模型的判断，不是证据：sealed 为 None，说明里写明是模型判断；封存后按需追加的另外注明。"""
    effective, _ = _effective_verdicts(report, sealed)
    units = {str(u.get("id")): u for _, u in iter_units(report.doc or {})}
    rows = []
    for uid, verdict in effective.items():
        status = verdict.get("status")
        if status not in CLAIM_ISSUE or uid not in units:
            continue
        span = units[uid].get("span") or [0, 0]
        text = markdown[span[0]:span[1]]
        missing = verdict.get("missing") if status == "insufficient" and isinstance(verdict.get("missing"), str) else ""
        rationale = str(verdict.get("rationale") or "").rstrip("。")
        note = CLAIM_VERDICT_NOTE[status] + (f"（缺少：{missing}）" if missing else "") \
            + (f"。理由：{rationale}" if rationale else "") + (f"。{CLAIM_POST_SEAL}" if verdict.get("post_seal") else "")
        rows.append({"group": "none", "report": report.node_id, "field": field_name, "segment": None, "unit": uid,
                     "kind": "claim", "text": text, "state": "none", "issue": CLAIM_ISSUE[status], "ref": None,
                     "alias": None, "evidence_kind": None, "evidence": None, "eid": None, "artifact": None,
                     "node_id": None, "sealed": None, "span": span, "sentence": text, "note": note,
                     "verdict": {k: verdict[k] for k in ("status", "rationale", "missing", "judge", "post_seal")
                                 if k in verdict}})
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

