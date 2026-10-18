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
options.mask_columns 里的列换成「已遮罩」，SQL 里给它们起了别名、套了函数得来的结果列也换
（_mask_sets，和可验证报告同一个函数，合并查询另按合并 SQL 认）；用到这些列的指标，指标步骤的值、
证据图的标签也不给（_metric_masks，和推演同一个判断；报告撰写节点写作之前按同一个判断把这类指标在目录里写成「已遮罩」，
withheld_metrics，只是遮罩列只认确定的那一套，和单元格引用的自查同一个口径；可验证报告认老文档也按这个口径，
certain_masks；转抄上游口径卡的指标在指标依赖图上一次认，_CopyGraph → recompute.masked_closure，按整张卡认：转抄的那张卡
在这次运行的任何一次执行里有一个指标取自遮罩格，转抄出来的就不给，推演的转抄输入按同一张图，copied_inputs）；SQL、合并
警告里写着的遮罩值（这次运行里确定取自遮罩列的值）
展示时换掉。一份快照的遮罩分析、收到的值按快照工件记在进程里（_MEMO），之后的请求不再读盘、不再分析
——在有身份体系之前，这只减少暴露，不是安全边界：知道工件 id 的人照样能取到整份快照。

已知局限（遮罩只减少暴露、不是安全边界，可验证报告的真伪以服务端封存的清单哈希为准）：认遮罩列只按 SQL 和列名认，写法的
长尾认不全，取自那些列的指标、单元格在写作目录、报告文档、成果和可验证报告里照给。已知的有：先给遮罩列起别名、外层再套
没起别名的表达式（SELECT * FROM (SELECT UPPER(p) FROM (SELECT phone AS p …))，列名「UPPER(p)」里没有遮罩列的名字）、
一元加号这类认法本身漏掉的表达式（+phone，masking._derived_masks 的缺口）、Postgres 给没起别名的表达式起的列名（upper、
?column?）、视图和别的表里换了名字存着的遮罩列、自己建的函数，以及数据整形把号码取出来再赋给变量（出处认不出）。

报告导出（GET /api/runs/{run_id}/report.html，app/report_export）用的也是这套出处链：
把选中的报告文档渲染成自包含的 HTML，每个数字点得开和网页上同一个拼法的面板
（口径、来源、计算方式），同样的引用共用一条链。

期 4 的核查状态同样只读封存范围内的东西：指标步骤带双盲复算的结果（_dual_check_of，读双盲复算节点的产出和台账
条目），句子带反方审稿的判断（_challenge_of，读反方审稿节点的产出和审稿记录）。它们是核查，不是证据：没有就不带这两个键。
证据图的每份报告另带一张被反方审过的句子 → 判断的表（_challenge_verdicts，和 _challenge_of 同一个读法），正文据此挂
句末标记，不用逐句请求片段接口。

结论句的判定（四期）是模型给的，不是证据：正式运行的判定在报告文档里、随文档封存；探索运行
按需裁判（POST …/evidence/judge），判定作为 evidence.judged 事件追加在封存之后，只填补没判过的
句子。交给裁判的摘录同样只取封存范围内的工件，每份查询快照里确定取自遮罩列的列不给（_judge_loader，报告撰写节点
当场裁判同一个函数）。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import csv
import functools
import io
import logging
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
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
    REASON_TEXT,
    LocateProblem,
    MergeHop,
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
from app.engine import direct_select, recompute
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
    withhold_metrics,
)
from app.engine.judge import FIELDS_MAX, SETTLED, UNSUPPORTED, VERDICTS, candidates, outdated
from app.engine.lineage import untraced
from app.engine.merge_query import MERGE_SOURCE, trace_cell
from app.engine.schema import CLAIMS_JUDGE, GraphSpec, challenge_report, dual_check_card
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
#: 面板底下的遮罩说明。遮的列可能是遮罩列本身，也可能是 SQL 里给它起了别名、套了函数得来的结果列（c.phone AS tel）
MASK_NOTE = "这些列取自数据源中设置了遮罩的列，面板上不显示原值。遮罩只减少暴露，不是安全边界"
#: 遮没遮核对不了、不显示的值（指标步骤的值、推演的原值）：和「已遮罩」分开，读的人不该以为这里设了遮罩
UNVERIFIED_VALUE = "无法核对"
#: 认不出出处的输入（数据整形算出来再赋给变量的一格），值和这次运行查到过的遮罩值一样：界面上接在「，不显示值」前面
VALUE_MASKED = "值与设置了遮罩的列中的值相同"
#: 查询步骤的 SQL、合并警告里写着的遮罩值展示时换成了「已遮罩」（_Shown）：复制下来的不是执行的原文
SQL_MASKED_NOTE = "SQL 和提示中写着的遮罩值仅在展示时换成「已遮罩」，执行的原文未改动"
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
    #: 节点 id → node.started 上记的名字、类型。子工作流里的节点（「子工作流节点 id/…」）不在这次运行的图上，名字从这里取
    labels: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)

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
        elif etype == "node.started":
            if isinstance(data.get("label"), str) and data["label"]:
                out.labels.setdefault(node_id, data["label"])
            if isinstance(data.get("node_type"), str) and data["node_type"]:
                out.types.setdefault(node_id, data["node_type"])
        if etype != "node.finished":
            continue
        if data.get("artifact"):
            out.outputs[node_id] = data["artifact"]
        for entry in data.get("evidence") or []:
            if isinstance(entry, dict) and entry.get("artifact"):
                out.ledger.add((node_id, entry["artifact"]))
                # 条目里另记的节点 id（双盲复算复算的口径卡）是那个节点所在那一层图里的 id：换成这次运行里的
                out.entries.append({**entry, "node_id": node_id,
                                    **({"card": _scoped(node_id, entry["card"])} if entry.get("card") else {})})
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


def _reports(sealed: _Sealed, *, nested: bool = False) -> list[_Report]:
    """封存范围内每个报告节点最后一次的自查结果，按第一次出现的顺序。

    默认只认这次运行的图上的报告节点：子工作流里的报告（节点 id「子工作流节点 id/…」，子图的事件和父运行在同一个流里）
    是子工作流那一步内部的东西，父图的答案是子工作流出口的值，不是那份报告本身——证据图、取答案的评测、按成果挑报告
    （_choose）都按父图自己的认。nested 为真时连子工作流里的一起给（按 ?report=sub/write 点开它们）。可验证报告另按全部
    report.checked 核对（bundle/build._report_events：自查过的文档一份都不能少）。"""
    latest: dict[str, dict[str, Any]] = {}
    for _, etype, node_id, data in sealed.events:
        if etype == "report.checked" and node_id and isinstance(data, dict) and data.get("doc_artifact") \
                and (nested or "/" not in node_id):
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
    masks = _Masks(sealed)
    for report in reports:
        # 期 4：被反方审过的句子 → 判断，正文据此给它们挂句末标记。文档读不出来的不给；一句都没审的没有这个键
        marks = _challenge_verdicts(sealed, report) if report.doc is not None else {}
        body["reports"].append({
            "node_id": report.node_id, "doc_artifact": report.doc_artifact,
            "doc_sealed": sealed.trusted, "hash_ok": report.hash_ok, "ok": report.ok,
            "repairs": report.repairs, "fields": report.fields, "stats": report.stats,
            **_judgement_of(report, sealed),
            **({"challenge_verdicts": marks} if marks else {}),
        })
        if report.doc is not None:
            evidence, edges = _graph_of(report, sealed, inputs)
            await _withheld_labels(evidence, report.doc, sealed, masks)
            body["evidence"].extend(evidence)
            body["edges"].extend(edges)
    return {**body, "mode": "cited"}


async def _withheld_labels(evidence: list[dict[str, Any]], doc: dict[str, Any], sealed: _Sealed, masks: _Masks) -> None:
    """证据图里指标的标签（目录里写的「名称 = 渲染值」）：指标的值不给的（用到遮罩列的一格、遮没遮核对不了、值里有遮罩值，
    _metric_masks，和指标步骤同一个判断），值换成「已遮罩」「无法核对」。以前指标步骤写着「已遮罩」，证据图的标签照给
    号码；报告只写了 [[see:m:phone_raw]]、正文里没有号码的，号码也从这里露出来。"""
    catalog = doc.get("catalog") or {}
    cards: dict[str, Any] = {}
    for item in evidence:
        entry = catalog.get(item.get("alias")) or {}
        artifact = str(entry.get("artifact") or "")
        if item.get("kind") != "metric" or not artifact:
            continue
        if artifact not in cards:
            cards[artifact] = _load_card(artifact)[0]
        metric = _metric_in(cards[artifact], (entry.get("locator") or {}).get("metric"))
        if metric is None or metric.get("value") is None:
            continue
        _, word = await _metric_masks(metric, str(entry.get("node_id") or ""), artifact, sealed, masks,
                                      card=cards[artifact])
        if word is not None:
            item["label"] = f"{entry.get('name') or metric.get('name') or metric.get('id')} = {word}"


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
        if entry.get("kind") == "query" and entry.get("source") == MERGE_SOURCE:
            # 合并结果连向它合并的那几次查询：图上看得出 Q3 是 Q1、Q2 按键合并出来的
            edges.extend({"from": alias, "to": target, "rel": "merged_from", "input": source.get("alias"),
                          "report": report.node_id}
                         for source in _merge_inputs(entry.get("artifact"))
                         if (target := _query_alias(catalog, source.get("artifact"))))
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


def _merge_inputs(artifact: Any) -> list[dict[str, Any]]:
    """合并结果快照记下的输入；取不回、哈希不符的当没有（证据图只是少几条边，片段接口照样说清楚）。"""
    try:
        snap = load(str(artifact)) if artifact else None
    except ValueError:
        return []
    inputs = snap.get("inputs") if isinstance(snap, dict) else None
    return [i for i in inputs if isinstance(i, dict)] if isinstance(inputs, list) else []


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
    """成果上的 _issuance；成果里取不到时退回封存范围内最后一条 issuance 事件。子工作流里成果节点的出具判定（节点 id
    「子工作流节点 id/…」，子图的事件和父运行在同一个流里）说的是子工作流交出的那份，不是这次运行的，不认。"""
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    issuance = output.get("_issuance")
    if not isinstance(issuance, dict):
        issuance = next((data for _, etype, node_id, data in reversed(sealed.events)
                         if etype == "issuance" and "/" not in str(node_id or "")), None)
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
    # 遮罩列：查询步骤里遮着的，加上指标步骤里取自遮罩列的输入（按位置取的那一格可能没有对应的查询步骤）
    masked = _chain_masked(chain)
    verdict = _effective_verdicts(chosen, sealed)[0].get(str(unit.get("id")))
    challenged = _challenge_of(sealed, chosen, str(unit.get("id")))
    # 口径账本（next-round 02 做法 3）：这个数来自哪一份口径。比对在封存之后做、不在封存范围内，标签里写明；没比对过的运行
    # （老运行、不是数的片段）没有这个键，返回一字不差
    from app.ledger.view import segment_label

    ledger = await segment_label(sealed, chosen.node_id, str(seg["id"]))
    return {
        **({"ledger": ledger} if ledger is not None else {}),
        "report": {"node_id": chosen.node_id, "doc_artifact": chosen.doc_artifact},
        "segment": {"id": seg["id"], "text": seg.get("text"), "kind": seg.get("kind"), "state": seg.get("state"),
                    "span": seg.get("span"), "unit": unit.get("id"),
                    **{k: seg[k] for k in ("ref", "issue", "strong", "cite") if k in seg}},
        "unit": {"id": unit.get("id"), "kind": unit.get("kind"), "text": markdown[start:end],
                 "span": unit.get("span"), "cites": unit.get("cites") or [],
                 **({"verdict": verdict} if verdict is not None else {}),
                 # 期 4：反方审稿对这一句的判断，没审到这一句的没有这个键
                 **({"challenge": challenged} if challenged is not None else {}),
                 "on_demand": _on_demand(sealed, doc, str(unit.get("id")), verdict, chosen.claims)},
        "block": {"id": block.get("id"), "type": block.get("type")},
        "chain": chain,
        "note": _note(seg, unit),
        "violations": [v for v in doc.get("violations") or []
                       if v.get("segment") == seg["id"] or (v.get("unit") == unit.get("id") and not v.get("segment"))],
        "seal": {"sealed": sealed.sealed, "ok": sealed.seal["ok"], "covered": covered},
        "redacted": {"columns": masked, "note": MASK_NOTE} if masked else {"columns": []},
    }


METRIC_NOT_FOUND = "evidence_metric_not_found"


def _chain_masked(chain: list[dict[str, Any]]) -> list[str]:
    """出处链里遮着的列：查询步骤里遮着的，指标步骤里取自遮罩列的输入（按位置取的那一格可能没有对应的查询步骤），转抄的
    上游指标那几步的输入（跨卡往下追的，见 _upstream_steps）。"""
    steps = [*chain, *(i for step in chain if step.get("step") == "upstream" for i in step.get("inputs") or [])]
    return list(dict.fromkeys(c for step in steps if step.get("step") in ("query", "input")
                              for c in step.get("masked") or []))


@router.get("/{run_id}/evidence/metrics/{node}/{metric}")
async def evidence_metric(run_id: str, node: str, metric: str) -> dict[str, Any]:
    """点开应用页上的一个指标（应用平台 §3.6）：指标 → 输入 → 查询快照的出处链，和报告片段的指标链同一套（_metric_chain）。

    没有报告也能点：指标从口径卡节点最后一次封存的指标集里取。值取自遮罩列的（_metric_masks），指标步骤和展示的字都写
    「已遮罩」，同报告撰写时认出来的那样。"""
    sealed = await _sealed(run_id)
    cards = [(art, card) for node_id, art, card in _sealed_cards(sealed) if node_id == node]
    found = _metric_in(cards[-1][1], metric) if cards else None
    if found is None:
        raise CodedHTTPException(404, "这次运行封存的口径卡里没有这个指标", METRIC_NOT_FOUND)
    artifact, card = cards[-1]
    masks = _Masks(sealed)
    _, word = await _metric_masks(found, node, artifact, sealed, masks, card=card)
    try:
        shown = word or render_metric(found)
    except RenderError:
        shown = MISSING
    eid = make_eid("metric", artifact, {"metric": metric})
    entry = {"kind": "metric", "artifact": artifact, "node_id": node, "eid": eid,
             **({"masked": True, "rendered": word} if word else {})}
    chain = await _metric_chain({"text": shown}, {"kind": "metric", "locator": {"metric": metric}, "eid": eid},
                                entry, {"catalog": {}}, sealed, masks)
    covered = sealed.trusted and all(step.get("sealed") for step in chain if "sealed" in step)
    masked = _chain_masked(chain)
    return {"run_id": run_id, "node": node, "metric": metric, "rendered": shown, "eid": eid, "chain": chain,
            "seal": {"sealed": sealed.sealed, "ok": sealed.seal["ok"], "covered": covered},
            "redacted": {"columns": masked, "note": MASK_NOTE} if masked else {"columns": []}}


def _open_segment(sealed: _Sealed, wanted: str | None, segment_id: str
                  ) -> tuple[_Report, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """选报告、确认文档完好、找到片段：(报告, (块, 句子, 片段))。片段接口和推断来源接口共用同一套错误码。"""
    reports = _named_reports(sealed, wanted)
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


def _named_reports(sealed: _Sealed, wanted: str | None) -> list[_Report]:
    """点名要某一份报告时从哪些里挑：点的是子工作流里的那份（「sub/write」）连子工作流里的一起给，否则只给父图上的
    （_reports）。"""
    return _reports(sealed, nested=bool(wanted) and "/" in str(wanted))


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
    masks = _Masks(sealed)
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
        return await _query_steps(str(entry.get("artifact") or ""), [(locator.get("row"), locator.get("column"))],
                                  doc, sealed, masks, hint=doc.get("provenance") == DOC_PROVENANCE)
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
    # 报告撰写节点把这个指标的值换成了「已遮罩」（文档的证据目录里记着 masked）：正文写的就是目录里那个字，按它比渲染，
    # 不拿卡上的原值去比（比了只会报渲染不一致）
    written_masked = bool(entry.get("masked"))
    try:
        rendered_now = str(entry.get("rendered") or "") if written_masked \
            else render_metric(metric, cite.get("conv")) if metric else None
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
    # 期 4：这个指标做没做过双盲复算、结果如何。没有双盲复算复核过这份指标集时没有这个键，老运行一字不差
    if checked := _dual_check_of(sealed, str(entry.get("node_id") or ""), artifact, metric_id, metric):
        step["dual_check"] = checked
    card_node = str(entry.get("node_id") or "")
    if metric is not None and metric.get("inputs"):
        # 输入追没追到原始数据（engine/lineage.py）：和「有出处」（确定性复算一致）是两件事，面板分开说。追不到的照实说
        # 出处追到哪个节点为止；卡上没有输入（常量）的没有可追的，不给这个键
        step["untraced"] = _untraced_steps(metric, sealed, card_node)
    catalog = doc.get("catalog") or {}
    inputs: list[dict[str, Any]] = []
    #: 输入指到的查询快照 → 被引用的格；同一份快照只给一个查询步骤，格合在一起高亮
    wanted: dict[str, list[tuple[Any, Any]]] = {}
    held, word = await _metric_masks(metric or {}, str(entry.get("node_id") or ""), artifact, sealed, masks, card=card)
    for item in (metric or {}).get("inputs") or []:
        one = {"step": "input", **item, **_scoped_ids(item, card_node, sealed)}
        if item.get("through"):
            # 穿过原样解析查询结果的整形节点按位置取的格：面板写明经过的是哪个节点（图里的名字）
            one["through_label"] = _node_label(sealed, one["through"])
        artifact_of = item.get("artifact")
        if artifact_of and item.get("via") in ("tool_cell", "agent_field"):
            one["query"] = _query_alias(catalog, artifact_of)
            if cell := _input_cell(item, catalog):
                one["cell"] = cell
            wanted.setdefault(str(artifact_of), []).extend(_located(item.get("locator") or {}))
        if (found := held.get(str(item.get("path")))) is not None:
            one = _withheld(one, found)
        inputs.append(one)
    # 转抄另一张口径卡的指标（对账卡、召回卡的「口径」那一项）：跨卡往下追，接上那个指标自己的一段出处，一直到查询格子
    upstream = await _upstream_steps(metric or {}, sealed, masks, catalog, wanted, {(artifact, str(metric_id))},
                                     card_node=card_node)
    if withheld := {path: _held_word(found) for path, found in held.items() if found.kind != "table"}:
        # 代入式里那几处同样写「已遮罩」（遮没遮核对不了的写「无法核对」）：不然标签里遮着、代入式里亮着，同一个值换个
        # 地方照样看得到。整份结果当输入的不换：代入式里本来就只留着它的路径
        step["substituted"] = recompute.substituted_without(metric or {}, withheld)
    if step["value"] is not None and (word is not None or written_masked):
        # 指标的值本身也不给，和推演的原值同一个判断（_metric_masks）：指标可能就是遮罩列的那一格（cell(nodes.q, 0,
        # 'phone')），输入里遮着、这里亮着等于没遮。报告上的字（rendered）是读者已经看到的，照旧——写作时就认出来的，
        # 目录、正文里本来就是「已遮罩」。文档记着不给、这里却认不出的（数据源事后撤了遮罩），照文档不给
        step["value"] = word or MASKED
    queries = [s for a, cells in wanted.items() for s in await _query_steps(a, cells, doc, sealed, masks)]
    return [step, *inputs, *upstream, *queries]


async def _upstream_steps(metric: dict[str, Any], sealed: _Sealed, masks: "_Masks", catalog: dict[str, Any],
                          wanted: dict[str, list[tuple[Any, Any]]], seen: set[tuple[str, str]],
                          depth: int = 1, *, card_node: str = "") -> list[dict[str, Any]]:
    """指标的输入里转抄另一张口径卡的指标的（nodes.<卡>.metrics[i].value，卡上记着 upstream），每个接一步 upstream：那张
    卡的那个指标（名字、值、式子、代入式、核对状态，取值按同一套遮罩），它自己的输入（同指标步骤的输入；落在查询快照上的
    并进 wanted，后面照常给查询步骤），追没追到原始数据。转抄的转抄一层层接着追（防环：同一个指标只接一次；最多
    MAX_COPY_DEPTH 层）。of 是这一步接在哪个输入后面（输入的路径）。"""
    from app.engine.lineage import MAX_COPY_DEPTH, SNAPSHOT_VIAS

    out: list[dict[str, Any]] = []
    if depth > MAX_COPY_DEPTH:
        return out
    for item in metric.get("inputs") or []:
        up = item.get("upstream") if isinstance(item, dict) and item.get("via") == "metric" else None
        if not isinstance(up, dict) or not up.get("artifact"):
            continue
        artifact, metric_id = str(up["artifact"]), str(up.get("metric") or "")
        if (artifact, metric_id) in seen:
            continue
        seen.add((artifact, metric_id))
        # 卡上记的是卡所在那一层图里的 id（子工作流里的卡转抄同一张子图里的卡；经子工作流出口转抄的已经是这一层的）
        node = str(_scoped(card_node, item.get("node_id")) or "")
        card, hash_ok = _load_card(artifact)
        found = _metric_in(card, metric_id)
        step: dict[str, Any] = {
            "step": "upstream", "of": item.get("path"), "node_id": node or None,
            "label": _node_label(sealed, node) if node else None, "artifact": artifact, "metric": metric_id,
            "traced": up.get("traced") is True, "depth": depth, "hash_ok": hash_ok and found is not None,
            "sealed": sealed.trusted and (node, artifact) in sealed.ledger,
        }
        if found is None:
            out.append(step)
            continue
        held, word = await _metric_masks(found, node, artifact, sealed, masks, card=card)
        try:
            rendered = word or render_metric(found)
        except RenderError:
            rendered = MISSING
        step.update({
            "name": found.get("name") or metric_id, "value": word if word is not None else found.get("value"),
            "unit": found.get("unit") or "", "rendered": rendered, "caliber": (card or {}).get("caliber"),
            "version": (card or {}).get("caliber_version"), "source": await _caliber_source((card or {}).get("source")),
            "expression": found.get("expression"), "substituted": found.get("substituted"),
            "recompute_ok": found.get("recompute_ok"), "status": found.get("status"),
            **({"incomplete": True, "incomplete_reason": found.get("incomplete_reason") or ""}
               if found.get("incomplete") else {}),
            **({"sql_check_failed": True, "sql_check_reason": found.get("sql_check_reason") or ""}
               if found.get("sql_check_failed") else {}),
            "untraced": _untraced_steps(found, sealed, node),
        })
        if withheld := {path: _held_word(h) for path, h in held.items() if h.kind != "table"}:
            step["substituted"] = recompute.substituted_without(found, withheld)
        inputs: list[dict[str, Any]] = []
        for sub in found.get("inputs") or []:
            if not isinstance(sub, dict):
                continue
            one = {"step": "input", **sub, **_scoped_ids(sub, node, sealed)}
            if sub.get("artifact") and sub.get("via") in SNAPSHOT_VIAS:
                one["query"] = _query_alias(catalog, sub["artifact"])
                if cell := _input_cell(sub, catalog):
                    one["cell"] = cell
                wanted.setdefault(str(sub["artifact"]), []).extend(_located(sub.get("locator") or {}))
            if (h := held.get(str(sub.get("path")))) is not None:
                one = _withheld(one, h)
            inputs.append(one)
        step["inputs"] = inputs
        out.append(step)
        out += await _upstream_steps(found, sealed, masks, catalog, wanted, seen, depth + 1, card_node=node)
    return out


def _scoped(card_node: Any, node_id: Any) -> Any:
    """口径卡上记的节点 id 换成这次运行里的（engine/lineage.scoped_id：子工作流里的卡记的是子图里的 id）。"""
    from app.engine.lineage import scoped_id

    return scoped_id(card_node, node_id)


def _scoped_ids(item: dict[str, Any], card_node: str, sealed: _Sealed) -> dict[str, Any]:
    """输入步骤上的节点 id 换成这次运行里的（_scoped）；落在子工作流里的另给图上的名字（node_label、subgraph_label：
    「子工作流 › 节点」），画布、记录页认不出这个 id 时照它写。最外层的输入不多带键，和以前一字不差。"""
    out: dict[str, Any] = {}
    for key in ("node_id", "through", "subgraph"):
        if isinstance(item.get(key), str) and item[key]:
            out[key] = _scoped(card_node, item[key])
    if "/" in str(out.get("node_id") or ""):
        out["node_label"] = _node_label(sealed, out["node_id"])
    if out.get("subgraph"):
        out["subgraph_label"] = _node_label(sealed, out["subgraph"])
    return out


def _node_label(sealed: _Sealed, node_id: Any) -> str:
    """封存的图里这个节点的名字，没有名字的写 id。子工作流里的节点（「子工作流节点 id/…」）不在图上，按 node.started 记的
    名字一层层接上：「子工作流 › 节点」。"""
    node = next((n for n in (sealed.graph or {}).get("nodes") or [] if isinstance(n, dict) and n.get("id") == node_id), {})
    if node or not isinstance(node_id, str) or "/" not in node_id:
        return str((node.get("data") or {}).get("label") or node_id or "")
    parts = node_id.split("/")
    names = [_node_label(sealed, parts[0])]
    for i in range(2, len(parts) + 1):
        here = "/".join(parts[:i])
        names.append(sealed.labels.get(here) or parts[i - 1])
    return " › ".join(names)


def _untraced_steps(metric: dict[str, Any], sealed: _Sealed, card_node: str = "") -> list[dict[str, Any]]:
    """指标的输入里追不到原始数据的：[{path, node_id, label, type}]，label 是封存的图里那个节点的名字、type 是节点类型
    （面板按它写「数据整形」「沙箱代码」）。认不出是哪个节点的，node_id、label 为 None。节点 id 按卡所在那一层认
    （_scoped）。"""
    nodes = {n.get("id"): n for n in (sealed.graph or {}).get("nodes") or [] if isinstance(n, dict)}
    out = []
    for item in untraced(metric.get("inputs") or []):
        node_id = _scoped(card_node, item.get("node_id"))
        node = nodes.get(node_id) or {}
        out.append({"path": item.get("path"), "node_id": node_id,
                    "label": _node_label(sealed, node_id) if node_id else None,
                    "type": node.get("type") or sealed.types.get(str(node_id or "")) or None})
    return out


@dataclass(frozen=True)
class _Held:
    """一个输入不给值的原因（_InputMasks.check、_metric_masks）。kind：

    - masked：取自遮罩列的一格、一行，columns 是那几列遮罩列（列名照原样，按出现的先后）
    - table：整份结果当输入（vars.res.rows），结果里 columns 这几列遮着；它不一定把哪一格带进指标，指标的值另按值认。
      快照读不出来的，reason 写原因
    - unverified：遮没遮核对不了，columns 是用到的列（认不出是哪一列时为空），reason 给人看
    - value：认不出出处，值却就是这次运行查到过的遮罩值（按值认），reason 给人看"""

    kind: str
    columns: list[str]
    reason: str | None = None


def _withheld(step: dict[str, Any], held: _Held) -> dict[str, Any]:
    """不给值的输入：值写「已遮罩」，遮没遮核对不了的写「无法核对」（_held_word；本来就没有值的照旧是空的，缺输入还得看得
    出来），模型报的值、快照里的值一并拿掉。取自遮罩列的，masked 写明是哪几列；遮没遮核对不了的、按值认出来的
    （_InputMasks），mask_unverified 写原因、不进 masked——面板底下那句「这些列设置了遮罩」不能把它们也算进去。"""
    out = {k: v for k, v in step.items() if k not in ("model_value", "snapshot_value")}
    if out.get("value") is not None:
        out["value"] = _held_word(held)
    if held.reason is None:
        out["masked"] = held.columns
    else:
        out["mask_unverified"] = held.reason
    return out


def _held_word(held: _Held) -> str:
    """不给值的输入写在值那里的字：遮没遮核对不了的（快照、取值链上的产出读不出来）写「无法核对」——数据源可能根本没设
    遮罩，读的人不该以为这里遮着；别的（取自遮罩列的、按值认出来的）写「已遮罩」。代入式里那一处同一个字。"""
    unverified = held.kind == "unverified" or (held.kind == "table" and held.reason is not None)
    return UNVERIFIED_VALUE if unverified else MASKED


async def _metric_masks(metric: dict[str, Any], card_node: str, artifact: str, sealed: _Sealed,
                        masks: _Masks, *, card: dict[str, Any] | None = None,
                        output: Callable[[Any], Any] | None = None, by_value: bool = True, sure: bool = False
                        ) -> tuple[dict[str, _Held], str | None]:
    """一个指标的输入里哪些不给值（路径 → _Held），指标本身的值给不给（None 照给，否则是写在值那里的字：「已遮罩」
    「无法核对」）。证据面板的指标步骤、证据图的标签、报告撰写节点的写作目录（withheld_metrics）、推演里转抄的输入
    （copied_inputs）都按这一个函数认，和推演同一套分类、同一个判断（recompute.withheld_value）：

    - 输入取自哪份结果的哪几列按 _InputMasks 认（recompute.input_origin 的 wide：下标不是字面量的、整行、整份结果都认）
    - 认不出出处的输入（数据整形算出来再赋给变量的一格），值就是这次运行查到过的遮罩值的，按值认（masks.run_secrets）；
      运行输入是发起运行的人自己填的，不认
    - 转抄上游口径卡的输入（nodes.<卡>.metrics[0].value、nodes.<卡>.text、整张卡，recompute.card_input）按那张卡认：那张卡
      在这次运行的任何一次执行里有一个指标不给，转抄出来的就不给（转抄的保守规则），在这次请求的指标依赖图上一次算出
      （_CopyGraph → recompute.masked_closure），不逐个往上递归
    - 指标的值：取自遮罩列的一格、一行的不给；最外层的输入遮没遮核对不了的写「无法核对」；整份结果当输入、有认不出出处
      的输入的，值里有遮罩值才不给（len(vars.res.rows) 照给）

    card 是指标所在的那张卡（认它在卡上的位置，依赖图按 (指标集工件, 位置) 认指标），没给的按这个指标对象本身认。
    output（节点 id → 那个节点的产出）给了的，按位置取的一格、转抄的上游卡顺着它走取值链，不去封存的事件里找口径卡算这一次
    之前的产出：报告撰写节点写作时运行还没封存，产出就在运行状态里。by_value 为假（报告撰写节点的写作目录）：认不出出处的
    输入不按值认，也不因此按值认指标的值——只按出处认，见 withheld_metrics。sure 为真（写作目录、可验证报告认老文档）：
    遮罩列只按确定的那一套认（_InputMasks 的 sure），不跟着证据面板宁可多遮。"""
    graph = masks.copies(sealed, output=output, by_value=by_value, sure=sure)
    judged = await graph.judged(card_node, artifact, metric, card)
    held = dict(graph.held(card_node, artifact, metric, card))
    for path, (kind, columns, reason) in judged.copied.items():
        if kind is not None:
            held[path] = _copied_held(kind, columns, reason)
    return held, {"masked": MASKED, "unverified": UNVERIFIED_VALUE}.get(judged.word or "")


def _copied_held(kind: str, columns: tuple[str, ...], reason: str | None) -> _Held:
    """转抄上游口径卡的输入不给值的原因（recompute.Judged.copied 的一项）：转抄的那张卡有指标不给的是 masked（哪种写法都
    一样），写那张卡让它不给的遮罩列；读到的上游指标核对不了的核对不了（整张卡转抄的同 table、带原因）。"""
    if kind == "masked":
        return _Held("masked", list(columns))
    if kind == "table_unverified":
        return _Held("table", [], reason or CARD_UNVERIFIED)
    return _Held("unverified", [], reason or CARD_UNVERIFIED)


def _card_key(node_id: Any, card: dict[str, Any]) -> Any:
    """一张口径卡在依赖图上的键：它的指标集工件（每次执行一份，内容寻址）；产出里没记工件的（落工件失败）按节点和这份
    产出本身认。"""
    return str(card.get("artifact") or "") or ("node", str(node_id or ""), id(card))


def _metric_key(artifact: str, card_node: str, metric: dict[str, Any], card: dict[str, Any] | None) -> Any:
    """一个指标在依赖图上的键：(指标集工件, 指标在卡上的位置)，转抄的边按同样的键指到上游。认不出位置的（没给卡，或者
    调用方另取来的指标对象）按这个指标对象本身认，不和别的指标混在一起（依赖图留着它，对象 id 在这次请求里不会被复用）。"""
    metrics = card.get("metrics") if isinstance(card, dict) and isinstance(card.get("metrics"), list) else []
    index = next((i for i, m in enumerate(metrics) if m is metric), None)
    if index is None:
        return ("metric", id(metric))
    return (artifact or _card_key(card_node, card), index)  # type: ignore[arg-type]


def _needs_secrets(links: recompute.MetricLinks) -> bool:
    """依赖图上这个指标可能要按值认（整份结果当输入、认不出出处的输入、整张卡转抄），值又可能是遮罩值：认之前先把这次运行
    查到过的遮罩值收好（masked_closure 在同步代码里取）。"""
    return recompute.maybe_masked_value(links.metric.get("value")) and (
        links.uncertain or any(kind == "table" for kind, _ in links.direct.values())
        or any(edge.whole for edge in links.copies))


class _CopyGraph:
    """一次请求里认指标的值给不给（_metric_masks）用的指标依赖图。键是 (指标集工件, 指标在卡上的位置)：转抄以外的输入按
    _InputMasks 认（取自遮罩格的、核对不了的、按值认出来的），转抄上游口径卡的输入（recompute.card_input）连成边，交给
    recompute.masked_closure 一次算出最小不动点。认过的指标记下来：证据图给每个指标认标签、同一次请求里别的指标转抄它，
    都不再重认，工作量和图的大小成正比。

    转抄的保守规则按整张卡认（recompute.CopyEdge.card）：转抄一张卡时，把它在这次运行里每一次执行的指标集（_executions：
    封存之后按封存范围内的台账，写作时按运行状态里的台账）里的每个指标都放进图里，有一个不给，转抄出来的就不给。写作目录
    （output 给了：运行状态里每个节点最后一次的产出）里，循环中抄自己上一轮的卡读到的是它自己最后一次的产出，第一轮取过号码、
    最后一轮没取的，以前就这么照给了；封存之后（output 没给）另按每次执行往回连：卡在这次执行之前读到的是自己上一次执行的
    产出（_card_outputs）。两种都只按图算，不按追了几层算：以前追够八层就按遮罩算，没设遮罩的循环累加也成了「已遮罩」。"""

    def __init__(self, sealed: _Sealed, masks: _Masks, *, output: Callable[[Any], Any] | None, by_value: bool,
                 sure: bool) -> None:
        self.sealed, self.masks = sealed, masks
        self.given, self.by_value, self.sure = output, by_value, sure
        self._links: dict[Any, recompute.MetricLinks] = {}
        self._held: dict[Any, dict[str, _Held]] = {}
        self._judged: dict[Any, recompute.Judged] = {}
        self._runs: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._owners: dict[str, str] | None = None

    def matches(self, sealed: _Sealed, output: Callable[[Any], Any] | None, by_value: bool, sure: bool) -> bool:
        return self.sealed is sealed and self.given is output and self.by_value == by_value and self.sure == sure

    def held(self, card_node: str, artifact: str, metric: dict[str, Any], card: dict[str, Any] | None
             ) -> dict[str, _Held]:
        """认过的指标里转抄以外不给值的输入（judged 之后才有）。"""
        return self._held[_metric_key(artifact, card_node, metric, card)]

    async def judged(self, card_node: str, artifact: str, metric: dict[str, Any], card: dict[str, Any] | None
                     ) -> recompute.Judged:
        key = _metric_key(artifact, card_node, metric, card)
        if key not in self._judged:
            fresh = await self._build(key, card_node, artifact, metric)
            secrets = await self.masks.run_secrets() if any(_needs_secrets(self._links[k]) for k in fresh) else None
            self._judged.update(recompute.masked_closure({k: self._links[k] for k in fresh}, self._judged,
                                                         lambda: secrets))
        return self._judged[key]

    def _executions(self, node_id: str) -> list[tuple[str, dict[str, Any]]]:
        """口径卡 node_id 在这次运行里每一次执行的指标集：[(工件, 内容)]，按台账（sealed.entries 里的 metric_set 条目）认，
        读不出来、和哈希对不上的跳过（读到的那一次产出读不出来另有 CardInput.unreadable 兜着）。同一张卡只找一次。"""
        if node_id not in self._runs:
            found: list[tuple[str, dict[str, Any]]] = []
            for artifact in dict.fromkeys(str(e["artifact"]) for e in self.sealed.entries
                                          if e.get("kind") == "metric_set" and e.get("artifact")
                                          and str(e.get("node_id") or "") == node_id):
                content, ok = self.masks.fetch(artifact)
                if ok is True and isinstance(content, dict) and isinstance(content.get("metrics"), list):
                    found.append((artifact, content))
            self._runs[node_id] = found
        return self._runs[node_id]

    def _owner(self, artifact: str) -> str | None:
        """指标集工件 artifact 是哪张口径卡（节点 id）在这次运行里的产出，按台账认；认不出的是 None。"""
        if self._owners is None:
            self._owners = {}
            for e in self.sealed.entries:
                if e.get("kind") == "metric_set" and e.get("artifact") and e.get("node_id"):
                    self._owners.setdefault(str(e["artifact"]), str(e["node_id"]))
        return self._owners.get(artifact) if artifact else None

    async def _build(self, key: Any, card_node: str, artifact: str, metric: dict[str, Any]) -> list[Any]:
        """认好 key 和顺着转抄的边够得着、还没认过的指标（转抄以外的输入，_links、_held），交回这一批新认的键。转抄一张卡时
        它每一次执行的每个指标都放进来（_executions，转抄的保守规则按整张卡认）；上游的指标按它那一次执行之前的产出认
        （_InputMasks 按 _card_outputs 找它当时读到的产出）。卡上没记输入的指标（写死的数、升级前的老卡）不放：照给。"""
        fresh: list[Any] = []
        todo = [(key, card_node, artifact, metric)]
        while todo:
            key, node, art, item_metric = todo.pop()
            if key in self._links:
                continue
            hide = _InputMasks(self.sealed, self.masks, node, art, output=self.given, sure=self.sure)
            links = recompute.MetricLinks(item_metric, card=node or None)
            held: dict[str, _Held] = {}
            for item in item_metric.get("inputs") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path"))
                copied = hide.copied(item)
                if copied is not None:
                    upstream: list[Any] = []
                    # 转抄的是哪张卡：按读到的产出上记着的指标集认（经数据整形赋给变量、转了一道的也认得出是哪张卡），
                    # 认不出的按取值链上的那个节点
                    # 取值链上写的是卡所在那一层图里的 id（_scoped）
                    owner = _scoped(node, copied.node_id)
                    if copied.card is not None:
                        up_art = str(copied.card.get("artifact") or "")
                        owner = self._owner(up_art) or owner
                        for index in copied.picked:
                            up_metric = copied.card["metrics"][index]
                            if isinstance(up_metric, dict) and up_metric.get("inputs"):
                                up = (_card_key(copied.node_id, copied.card), index)
                                upstream.append(up)
                                todo.append((up, owner, up_art, up_metric))
                    for run_art, content in self._executions(owner):
                        for index, up_metric in enumerate(content["metrics"]):
                            if isinstance(up_metric, dict) and up_metric.get("inputs"):
                                todo.append(((run_art, index), owner, run_art, up_metric))
                    links.copies.append(recompute.CopyEdge(path, tuple(upstream), copied.whole, copied.unreadable,
                                                           card=owner))
                    continue
                found, traced = await hide.check(item)
                if found is None and not traced:
                    if not self.by_value:
                        continue
                    links.uncertain = True
                    value = item.get("value")
                    if not path.startswith("input.") and recompute.maybe_masked_value(value) \
                            and recompute.carries_masked(value, await self.masks.run_secrets()):
                        found = _Held("value", [], VALUE_MASKED)
                if found is not None:
                    held[path] = found
                    # 遮罩列只在确实取自遮罩列时往下游传（核对不了的那几列不算）
                    links.direct[path] = (found.kind, tuple(found.columns) if found.reason is None else ())
            self._links[key] = links
            self._held[key] = held
            fresh.append(key)
        return fresh


async def withheld_metrics(catalog: dict[str, Any], *, nodes: dict[str, Any], ledger: list[Any]) -> list[str]:
    """报告撰写节点写作之前：目录里值取自遮罩格的指标（别名，按目录的先后），交给 evidence.withhold_metrics 换成「已遮罩」。

    和证据面板指标步骤的值、推演的原值同一个判断（_metric_masks 判「已遮罩」的：cell() 取的遮罩格、取自改了名字的遮罩列、
    按位置取自遮罩列的一格、一行，整份结果里带着遮罩列、值里就有这次运行查到过的遮罩值的）。以前写作目录、文档的证据目录
    原样写着这些值，证据面板写「已遮罩」，同一个号码从报告正文和可验证报告里出去了。

    只按出处认（by_value=False）：认不出出处的输入（写死的目标、数据整形算出来的数）不因为值碰巧等于遮罩列里的某一格就
    算取自遮罩列。证据面板为了展示按值多遮一层，这里跟着多算，「单笔目标 20,000元」这种普通指标就在报告里成了「已遮罩」，
    引用它的正式运行判失败、写作者也改不掉（报告自查第 3 轮去掉按值认是同一个道理）。数据整形把号码取出来再赋给变量的，
    出处认不出，这里照给（已知的缺口，同证据面板对改过形状的结果）。这时运行还没封存，不看事件：
    - 卡上的指标取自运行状态里口径卡节点的产出（nodes，和台账里那份指标集是同一份内容），按位置取的一格顺着运行状态里的
      产出走取值链（口径卡算的时候读到的就是它们）
    - 查询快照的数据源按证据台账认（ledger 的 query 条目），这次运行查到过的遮罩值从台账里的查询快照收
    遮没遮核对不了的（「无法核对」）不算：写作时快照和产出刚落下，读不出来的只能是坏了的文件，宁可照给、不让写作者因此
    改不掉；可验证报告那边另有兜底（bundle/build.py 只给哈希）。

    遮罩列只按确定的那一套认（sure，和单元格引用的自查同一个口径：_cited_masks）：SQL 认不准、证据面板整份遮的快照里，
    没确定取自遮罩列的普通金额、计数、只装着分档标签的列照给。以前跟着证据面板宁可多遮，WITH 写了列名表、VALUES 当表的
    销售额也成了「已遮罩」，正式运行引用它就判失败；同一格按 [[v:…]] 引用却不报违规。转抄上游口径卡的指标
    （nodes.<卡>.metrics[0].value、nodes.<卡>.text、整张卡）按那张卡认（转抄的保守规则，_CopyGraph）：运行状态里每张卡只有
    最后一次的产出，那张卡每一次执行的指标集按台账（ledger 的 metric_set 条目）读回来，有一次执行里有一个指标取自遮罩格，
    转抄出来的就写「已遮罩」。以前只看最后一次的产出：循环里第一轮取号码、之后转抄上一轮的卡，最后一轮没碰号码列，写作目录
    照给，号码进了正文和成果；封存之后按每次执行往回认才写「已遮罩」，同一个指标两样说法。循环里抄自己上一轮的卡指回自己，
    在依赖图上是一个环，那张卡哪一次执行都没有取自遮罩格的指标就照给。"""
    queries = {str(e["artifact"]): {k: e[k] for k in ("node_id", "tool", "source", "via", "call_id", "exec")
                                    if e.get(k) is not None}
               for e in ledger if isinstance(e, dict) and e.get("kind") == "query" and e.get("artifact")}
    # 每张口径卡每一次执行的指标集：转抄的保守规则按整张卡、每一次执行认（_CopyGraph._executions 从 entries 里找）
    runs = [dict(e) for e in ledger if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
    view = _Sealed(run_id="", graph={}, events=[], queries=queries, entries=runs,
                   seal={"sealed": False, "ok": None, "manifest_seq": None, "legacy": False})
    masks = _Masks(view)
    found: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]] = []
    for alias, entry in catalog.items():
        if not isinstance(entry, dict) or entry.get("kind") != "metric" or entry.get("value") is None:
            continue
        card_node = str(entry.get("node_id") or "")
        card = nodes.get(card_node)
        metric = _metric_in(card if isinstance(card, dict) else None, (entry.get("locator") or {}).get("metric"))
        if isinstance(metric, dict) and metric.get("inputs"):
            found.append((alias, card_node, str(entry.get("artifact") or ""), metric, card))
    if not found:
        return []
    # cell()、Agent 字段落在的快照先在线程里读好（读盘、复验哈希）：在运行里认，不占着事件循环。转抄上游口径卡的指标要顺着
    # 上游卡的输入认，运行状态里每张口径卡、台账里每一次执行的指标集连同它们的输入一并读好
    await masks.prefetch(str(e["artifact"]) for e in runs)
    cards = [card for card in nodes.values() if isinstance(card, dict) and card.get("kind") == "metric_set"]
    cards += [content for e in runs for content, ok in [masks.fetch(str(e["artifact"]))]
              if ok is True and isinstance(content, dict)]
    await masks.prefetch(str(item["artifact"]) for metric in [*(m for *_, m, _ in found),
                                                              *(m for card in cards for m in card.get("metrics") or [])]
                         if isinstance(metric, dict) for item in metric.get("inputs") or []
                         if isinstance(item, dict) and isinstance(item.get("artifact"), str) and item["artifact"])

    def output(node_id: Any) -> Any:
        return nodes.get(str(node_id or ""))

    out: list[str] = []
    for alias, card_node, artifact, metric, card in found:
        _, word = await _metric_masks(metric, card_node, artifact, view, masks, card=card, output=output,
                                      by_value=False, sure=True)
        if word == MASKED:
            out.append(alias)
    return out


async def certain_masks(sealed: _Sealed, masks: _Masks) -> tuple[dict[str, frozenset[str]], Any]:
    """可验证报告认老文档时用的遮罩（bundle/build.py 的 _Store.metric_word，sure）：(封存范围内每份读得出来的查询快照 →
    确定取自遮罩列的结果列（小写，_cited_masks 的键，和写作目录同一个口径）, 这次运行查到过的遮罩值（run_secrets）)。

    可验证报告在线程里构建、不查库，这几样按证据面板的那一套先在这里认好交进去：老文档整份只给哈希的判断和写作目录
    逐个指标一样（SQL 认不准时不因此算遮罩列），不另写一套。读不出来、和哈希对不上的快照不在里面：可验证报告按它的数据源
    现在设的遮罩认，同 _InputMasks。"""
    await masks.prefetch(sealed.queries)
    out: dict[str, frozenset[str]] = {}
    for artifact, info in sealed.queries.items():
        snap, ok = masks.fetch(artifact)
        if ok is True and isinstance(snap, dict):
            out[artifact] = frozenset(await masks.cited(artifact, (info or {}).get("source"), snap))
    return out, await masks.run_secrets()


async def copied_inputs(sealed: _Sealed, card_node: str, artifact: str, card: dict[str, Any]
                        ) -> dict[int, dict[str, tuple[str | None, str | None]]]:
    """推演（api/whatif.py）用：这张卡上每个指标转抄上游口径卡的输入给不给值，{指标在卡上的位置: {输入路径: (种类, 原因)}}，
    种类同 recompute.Judged.copied（None 照给）。和证据面板的指标步骤同一个判断、同一张依赖图（_metric_masks 的
    _CopyGraph，宁可多遮的那一套）：推演据此不给转抄来的遮罩值、不让改，核对不了的不给，照给的照常推演。以前推演只按值认，
    转抄来的值不是够长的号码（短编码、从遮罩格算出来的数）就照给、还能改。"""
    masks = _Masks(sealed)
    graph = masks.copies(sealed, output=None, by_value=True, sure=False)
    out: dict[int, dict[str, tuple[str | None, str | None]]] = {}
    for index, metric in enumerate(card.get("metrics") or []):
        if isinstance(metric, dict) and metric.get("inputs"):
            judged = await graph.judged(card_node, artifact, metric, card)
            if judged.copied:
                out[index] = {path: (kind, reason or (CARD_UNVERIFIED if kind in ("unverified", "table_unverified")
                                                      else None))
                              for path, (kind, _, reason) in judged.copied.items()}
    return out


#: tool_cell、Agent 字段落在的查询快照读不出来：只知道数据源现在设的遮罩，查询当时记下的在快照里，遮没遮核对不了
SNAPSHOT_UNVERIFIED = "查询快照无法读取，核对不了这一格是否取自设置了遮罩的列"
#: 转抄的上游口径卡指标遮没遮核对不了（它的输入落在读不出来的快照上这类）
CARD_UNVERIFIED = "转抄的上游口径卡指标核对不了是否取自设置了遮罩的列"


class _InputMasks:
    """一个指标的输入里哪些不能给值。遮的列和查询步骤同一套（_mask_sets）：查询当时记下的加数据源现在设的，按 SQL
    认出改了名字的（c.phone AS tel 的 tel）也算，合并查询按它合并的那几个数据源、逐格来历和合并 SQL 算。

    取自哪份查询结果的哪几列按 recompute.input_origin 认（wide），推演是同一个认法：cell() 的格、Agent 交来的字段按
    定位里的列认；vars.res.rows[0][2] 这种按位置取的一格卡上没记定位，顺着取值链认，从写变量的节点在口径卡算这一次之前
    的产出起走（_card_outputs）；下标不是字面量的按列认，整行按整行的列认，整份结果（vars.res.rows）记 table。

    遮没遮核对不了的宁可多遮，和推演一致：快照读不出来（数据源现在没遮这一列，可查询当时记下的遮罩在快照里）、按位置取的
    一格取值链上的产出读不出来、那份结果没存快照只带着数据源名。认得出、可结果既没带快照的 id 也没带数据源名的（数据整形
    改过形状）不遮：认不出出处，卡上本来就只记着路径和值，值另按值认（_metric_masks）。

    一次请求里同一件工件只读一次、同一份快照的遮罩只认一次（记在 _Masks 上）：按位置取了十几格的指标，不必把写变量的
    节点那几 MB 的产出重读十几遍；证据图给每个指标认一遍输入，也不必把同一份快照反复读盘。

    sure（写作目录、可验证报告认老文档）：遮罩列只按确定的那一套认——读得出来的快照按单元格引用自查的 _cited_masks（_mask_sets
    的 sure，再按血缘排除值确实不取自遮罩列的），读不出来的按数据源现在设的列名。证据面板、推演照旧按 hidden 宁可多遮。

    转抄上游口径卡的输入（recompute.card_input：nodes.<卡>.metrics[0].value、vars.<卡的 assign_to>.metrics[0].value、
    nodes.<卡>.text）这里只认出转抄了哪张卡的哪几个指标（copied），给不给按那张卡在依赖图上认（_CopyGraph）。
    """

    def __init__(self, sealed: _Sealed, masks: _Masks, card_node: str, artifact: str, *,
                 output: Callable[[Any], Any] | None = None, sure: bool = False) -> None:
        self._sealed, self._masks = sealed, masks
        self._card, self._artifact = card_node, artifact
        self._outputs: dict[str, str] | None = None
        # 报告撰写节点写作时（withheld_metrics）：节点的产出就在运行状态里，不从封存的事件里找
        self._given = output
        self._sure = sure

    def _fetch(self, artifact: str) -> tuple[Any, bool | None]:
        return self._masks.fetch(artifact)

    def copied(self, item: dict[str, Any]) -> recompute.CardInput | None:
        """这个输入转抄了上游哪张口径卡的哪几个指标（recompute.card_input），不是转抄的为 None。卡当时的产出读不出来的
        （文件不在、与哈希不一致）交回 unreadable：核对不了，宁可多遮。"""
        return recompute.card_input(item, self._output)

    async def check(self, item: dict[str, Any]) -> tuple[_Held | None, bool]:
        """(不给值的原因, 认不认得出出处)：给值的原因为 None。认不出出处的（常量、运行输入、数据整形算出来的一格）第二项
        为假，值按值认（_metric_masks）。转抄上游口径卡的输入不在这里认（copied）。"""
        origin = recompute.input_origin(item, self._output, wide=True)
        if origin is None:
            return None, False
        if origin.unverified is not None:
            return _Held("unverified", list(origin.columns), origin.unverified), True
        used = list(dict.fromkeys(origin.columns))
        if not used:
            return None, True
        snap, ok = self._fetch(origin.artifact) if origin.artifact else (None, None)
        readable = ok is True and isinstance(snap, dict)
        hidden = await self._hidden(origin.artifact, origin.source, snap if readable else None)
        if masked := [c for c in used if c.lower() in hidden]:
            return _Held("table" if origin.table else "masked", masked), True
        why = SNAPSHOT_UNVERIFIED if origin.artifact and not readable \
            else recompute.POSITION_NO_SNAPSHOT if not origin.artifact and origin.source else None
        if why is not None:
            return _Held("table" if origin.table else "unverified", used, why), True
        return None, True

    def _output(self, node_id: Any) -> Any:
        # 卡的取值链里写的是卡所在那一层图里的 id（子工作流里的卡写子图里的 id），换成这次运行里的再找
        node_id = _scoped(self._card, node_id)
        if self._given is not None:
            return self._given(node_id)
        if self._outputs is None:
            self._outputs = self._masks.card_outputs(self._sealed, self._card, self._artifact)
        artifact = self._outputs.get(str(node_id or ""))
        content, ok = self._fetch(artifact) if artifact else (None, None)
        return content if ok is True else None

    async def _hidden(self, artifact: str | None, source: str | None, snap: dict[str, Any] | None) -> set[str]:
        info = self._sealed.queries.get(artifact) if artifact else None
        name = (info or {}).get("source") or source
        if self._sure and snap is not None:
            return await self._masks.cited(artifact, name, snap)
        sets = await self._masks.sets(artifact, name, snap)
        return sets.sure if self._sure else sets.hidden


def _card_outputs(sealed: _Sealed, card_node: str, artifact: str) -> dict[str, str]:
    """口径卡算出这份指标集的那一次执行开始之前，每个节点最后一次的产出工件：卡上 vars.*、nodes.* 取到的就是它们。
    认不出是哪一次执行（台账里没有这份指标集）时，取封存范围内每个节点最后一次的产出。"""
    finished = next((seq for seq, etype, node_id, data in sealed.events
                     if etype == "node.finished" and node_id == card_node and isinstance(data, dict)
                     and any(isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact") == artifact
                             for e in data.get("evidence") or [])), None)
    if finished is None:
        return dict(sealed.outputs)
    started = max((seq for seq, etype, node_id, _ in sealed.events
                   if etype == "node.started" and node_id == card_node and seq < finished), default=finished)
    out: dict[str, str] = {}
    for seq, etype, node_id, data in sealed.events:
        if seq >= started:
            break
        if etype == "node.finished" and node_id and isinstance(data, dict) and data.get("artifact"):
            out[node_id] = str(data["artifact"])
    return out


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
    """钉住别处口径卡（caliber_from）时，定义取自哪个工作流的哪一版；带上工作流现在的名字方便认。取数节点拷进这张图时
    改了名的（caliber_from.nodes，next-round 02 做法 4），来源里照带 nodes 映射：指标的输入写的是改名后的节点，面板据此
    说清它们对应那一版里的哪个取数节点。"""
    if not isinstance(source, dict) or not source.get("workflow_id"):
        return None
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, str(source["workflow_id"]))
    return {**source, "workflow_name": workflow.name if workflow else None}


def _caliber_upgrade(event: dict[str, Any] | None) -> dict[str, Any] | None:
    """这张口径卡在正式运行发起时的升版处置：钉在哪一版、上游最新是哪一版、怎么处置的。"""
    if not isinstance(event, dict):
        return None
    out = {k: event.get(k) for k in ("workflow_id", "caliber_node", "pinned", "latest", "policy", "policy_label")}
    if isinstance(event.get("caliber_nodes"), dict):
        # 取数节点拷进这张图时改了名（caliber_from.nodes）：照给映射；没改名的卡没有这个键，老运行一字不差
        out["caliber_nodes"] = dict(event["caliber_nodes"])
    return out


#: 双盲复算节点产出里一个指标的结果（engine/nodes/dual_check.py 的 SAME / DIFFERENT / UNCHECKED），原样交给界面
_DUAL_RESULTS = ("agreed", "disagreed", "unchecked")


def _spec_of(sealed: _Sealed) -> GraphSpec | None:
    """这次运行的图（认双盲复算复算哪张卡、反方审稿审哪份报告，和执行时同一个认法）。图读不懂的是 None。"""
    try:
        return GraphSpec.model_validate(sealed.graph)
    except Exception:  # noqa: BLE001 - 图读不懂只是认不出来，证据照常给
        return None


def _dual_check_of(sealed: _Sealed, card_node: str, artifact: str, metric_id: Any,
                   metric: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """这个指标的双盲复算状态：{status, node_id, choice?, blind_rendered?, reason?}。没有双盲复算复核过这份指标集的是 None。
    metric 是卡上的这个指标（认它为什么没被复算用），读不到卡时为 None。

    认的是复核了口径卡的哪一次结果：台账条目（kind dual_check）记着它复算的指标集工件，和指标步骤的工件对上才算——口径卡
    在循环里算了几次时，报告引用的那一次才是。直通了的（设置里关了、口径卡上没有来自查询的输入）没有台账条目：按画布上
    它复算的那张卡、和它跑完之前那张卡最后一次的指标集认。几个双盲复算复核了同一份指标集时取最后跑完的那个。

    - agreed / disagreed / unchecked：节点产出里这个指标的结果。不一致的带审核人的选择（a / b / neither，没选的不带）；
      一致、不一致的带盲算值（渲染好的）；无法复算的带原因
    - skipped：节点没复算这个指标，reason 说为什么（_not_checked）：关闭了（disabled，节点直通）；不在节点配置的
      「复算的指标」里、或者值不是数（not_targeted）；这个指标没有一个输入来自查询快照（no_inputs）。节点直通时产出里的
      no_inputs 说的是整张卡，不原样套到每个指标上：卡上确有来自查询的指标、只是没被选上的，写它的说法是 not_targeted
    产出读不出来（文件不在、与哈希不一致）时按台账条目说状态和选择，不带盲算值和原因。
    """
    if not card_node or not artifact or metric_id is None:
        return None
    checkers = set(sealed.nodes_of("dual_check"))
    if not checkers:
        return None
    spec: GraphSpec | None = None
    spec_read = False
    #: 口径卡节点 → 到这一刻为止它最后一次的指标集
    latest: dict[str, str] = {}
    found: dict[str, Any] | None = None
    for _, etype, node_id, data in sealed.events:
        if etype != "node.finished" or not node_id or not isinstance(data, dict):
            continue
        ledger = [e for e in data.get("evidence") or [] if isinstance(e, dict)]
        for e in ledger:
            if e.get("kind") == "metric_set" and e.get("artifact"):
                latest[node_id] = str(e["artifact"])
        if node_id not in checkers:
            continue
        output, ok = _fetch(str(data.get("artifact") or ""))
        output = output if ok is True and isinstance(output, dict) else {}
        entry = next((e for e in ledger if e.get("kind") == "dual_check"), None)
        if not spec_read:
            spec, spec_read = _spec_of(sealed), True
        node = spec.node_map().get(node_id) if spec else None
        if entry is not None:
            card, checked = str(entry.get("card") or ""), str(entry.get("metric_set") or "")
        elif output.get("status") == "skipped":
            card = (dual_check_card(spec, node)[0] or "") if spec and node else ""
            checked = latest.get(card, "")
        else:
            continue
        if card != card_node or checked != artifact:
            continue
        found = _dual_status(node_id, output, entry, str(metric_id), metric, _picked(node))
    return found


def _picked(node: Any) -> list[str] | None:
    """画布上这个双盲复算节点配的「复算的指标」，认法同 dual_check._options：没配、配了空列表都是 None（复算全部）。"""
    wanted = node.config.get("metrics") if node is not None else None
    picked = [str(m) for m in wanted if isinstance(m, str) and m.strip()] if isinstance(wanted, list) else []
    return picked or None


def _query_input(item: Any) -> bool:
    """来自查询快照的输入（同 dual_check._from_query）：双盲复算只复算有这种输入的指标。"""
    return isinstance(item, dict) and item.get("via") in ("tool_cell", "agent_field") \
        and isinstance(item.get("artifact"), str) and bool(item["artifact"])


def _not_checked(node_reason: str, metric: dict[str, Any] | None, picked: list[str] | None, metric_id: str) -> str:
    """节点没复算这个指标的原因。关闭了（disabled）照节点的说；别的按这个指标自己说：不在配置的「复算的指标」里是
    not_targeted；卡上读得到它、它没有一个输入来自查询快照是 no_inputs，有（那就是值不是数）是 not_targeted。读不到卡时
    照节点直通的原因说，节点跑了的说 not_targeted。"""
    if node_reason == "disabled":
        return node_reason
    if picked is not None and metric_id not in picked:
        return "not_targeted"
    if metric is not None:
        return "not_targeted" if any(_query_input(i) for i in metric.get("inputs") or []) else "no_inputs"
    return node_reason or "not_targeted"


def _dual_status(node_id: str, output: dict[str, Any], entry: dict[str, Any] | None, metric_id: str,
                 metric: dict[str, Any] | None, picked: list[str] | None) -> dict[str, Any]:
    """一次双盲复算里这个指标的状态（_dual_check_of 的答复）。"""
    if output.get("status") == "skipped":
        reason = _not_checked(str(output.get("reason") or ""), metric, picked, metric_id)
        return {"status": "skipped", "node_id": node_id, "reason": reason}
    row = next((m for m in output.get("metrics") or [] if isinstance(m, dict) and str(m.get("metric_id")) == metric_id),
               None)
    if row is None and not output and entry is not None:
        # 产出读不出来：退回台账条目，只有状态和选择
        brief = next((m for m in entry.get("metrics") or [] if isinstance(m, dict) and str(m.get("id")) == metric_id),
                     None)
        row = {"status": brief.get("status"), **({"choice": brief["choice"]} if "choice" in brief else {})} \
            if brief is not None else None
    if row is None:
        return {"status": "skipped", "node_id": node_id, "reason": _not_checked("", metric, picked, metric_id)}
    status = row.get("status") if row.get("status") in _DUAL_RESULTS else "unchecked"
    out: dict[str, Any] = {"status": status, "node_id": node_id}
    if status == "disagreed" and row.get("choice") in ("a", "b", "neither"):
        out["choice"] = row["choice"]
    rendered = row.get("blind_rendered")
    if status != "unchecked" and isinstance(rendered, str) and rendered:
        out["blind_rendered"] = rendered
    if status == "unchecked" and isinstance(row.get("reason"), str) and row["reason"]:
        out["reason"] = row["reason"]
    return out


#: 反方审稿的四种判断（engine/nodes/challenge.py 的 VERDICTS）：审稿概况的计数照这个顺序
_CHALLENGE_VERDICTS = ("stands", "weakened", "refuted", "inconclusive")


def _reviews(sealed: _Sealed, report: _Report
             ) -> Iterator[tuple[str, dict[str, Any], str | None, list[dict[str, Any]]]]:
    """审的就是正文这一份报告文档的每一次反方审稿，按跑完的先后：(节点, 产出, 审稿记录工件, 逐句的判断)。逐句判断的
    每一项至少是个 dict，unit、verdict 认不认得由调用方看。片段接口的 unit.challenge（_challenge_of）和证据图的
    challenge_verdicts（_challenge_verdicts）都从这里读，两边说的一定是同一次审稿。

    只认审的就是这一份文档的：审稿记录（challenge_artifact）里记着它审的报告节点和文档工件，和这份对上才算——
    报告在审稿之后又重写了（循环、接着跑），同一个句子编号在新的一稿里说的可能是另一句话。记录没存上（产出里的
    challenge_artifact 为空）或读不出来时，按画布上它审的报告、和它跑完之前那份报告最后一次自查记下的文档认，判断取
    产出里列的那几句（最多 10 句，被推翻、被削弱的排在前面），审稿记录工件照产出写（没存上的为空）。

    节点设为「报错并停止运行」、又有句子被推翻时，节点报错、没有 node.finished：审完那一刻的 challenge_done 日志记着
    审稿记录和计数，按它认（只认审稿记录存上了的），产出拼成 {challenge_artifact, counts, on_refuted: fail}。运行因此
    失败了，被推翻的是哪几句、反方凭什么，证据面板里照样看得到。
    """
    reviewers = set(sealed.nodes_of("challenge"))
    if not reviewers:
        return
    spec: GraphSpec | None = None
    #: 报告节点 → 到这一刻为止它最后一次自查记下的文档
    drafts: dict[str, str] = {}
    #: 反方审稿节点 → 这一次执行里审完时的日志：节点报错时它就是审稿结果
    done: dict[str, dict[str, Any]] = {}
    for _, etype, node_id, data in sealed.events:
        if not node_id or not isinstance(data, dict):
            continue
        if etype == "report.checked" and data.get("doc_artifact"):
            drafts[node_id] = str(data["doc_artifact"])
            continue
        if node_id not in reviewers:
            continue
        if etype == "node.started":
            done.pop(node_id, None)
            continue
        if etype == "log" and data.get("code") == "challenge_done":
            done[node_id] = data
            continue
        if etype == "node.finished":
            done.pop(node_id, None)
            output, ok = _fetch(str(data.get("artifact") or ""))
            if ok is not True or not isinstance(output, dict) or not isinstance(output.get("counts"), dict):
                continue    # 直通了（关闭、没有可审的句子）或者产出读不出来
        elif etype == "node.failed" and node_id in done:
            log = done.pop(node_id)
            if not isinstance(log.get("artifact"), str) or not log["artifact"]:
                continue
            output = {"challenge_artifact": log["artifact"], "counts": log.get("counts"), "on_refuted": "fail"}
        else:
            continue
        artifact = output.get("challenge_artifact") if isinstance(output.get("challenge_artifact"), str) else None
        record, saved = _fetch(artifact) if artifact else (None, None)
        if saved is True and isinstance(record, dict):
            if record.get("report") != report.node_id or record.get("doc_artifact") != report.doc_artifact:
                continue
            claims = record.get("claims")
        elif etype == "node.failed":
            continue    # 节点报错时没有产出可退：认不出审的是哪一稿
        else:
            spec = spec or _spec_of(sealed)
            node = spec.node_map().get(node_id) if spec else None
            target = challenge_report(spec, node)[0] if spec and node else None
            if target != report.node_id or drafts.get(target) != report.doc_artifact:
                continue
            claims = output.get("items")
        yield node_id, output, artifact, [c for c in claims or [] if isinstance(c, dict)]


def _verdict_in(claims: list[dict[str, Any]], unit_id: str) -> str | None:
    """一次审稿里这一句的判断：取列着这一句的第一条，判断不是一个词（缺了、空的、不是字符串）时这次审稿不算审到它。"""
    verdict = next((c.get("verdict") for c in claims if c.get("unit") == unit_id), None)
    return verdict if isinstance(verdict, str) and verdict else None


def _challenge_of(sealed: _Sealed, report: _Report, unit_id: str) -> dict[str, Any] | None:
    """这一句的反方审稿判断：{verdict, node_id, artifact, on_refuted, report_label, counts, unreviewed?}。没审到这一句的
    是 None。认哪一次审稿见 _reviews；几个反方审稿审了同一份文档时取最后跑完、审到了这一句的那个。artifact 是审稿记录
    （没存上的为空），on_refuted 是实际生效的处置（节点报错停止运行的是 fail），report_label 是报告节点的名字。

    counts 是这次审稿四种判断各几句——整份报告的，取节点产出里的那一份；unreviewed 是没能反查的句数，产出里有才带。
    证据面板单句展示时不画报告级的计数，可「无法判断」是不是因为反查没完成，得拿这两个数对一下。
    """
    if not unit_id:
        return None
    found: dict[str, Any] | None = None
    spec: GraphSpec | None = None
    for node_id, output, artifact, claims in _reviews(sealed, report):
        verdict = _verdict_in(claims, unit_id)
        if verdict is None:
            continue
        spec = spec or _spec_of(sealed)
        writer = spec.node_map().get(report.node_id) if spec else None
        unreviewed = output.get("unreviewed")
        found = {"verdict": verdict, "node_id": node_id, "artifact": artifact,
                 "on_refuted": str(output.get("on_refuted") or "degrade"),
                 "report_label": writer.title if writer else report.node_id,
                 "counts": _challenge_counts(output.get("counts"), claims),
                 **({"unreviewed": unreviewed} if isinstance(unreviewed, int) and not isinstance(unreviewed, bool)
                    and unreviewed >= 0 else {})}
    return found


def _challenge_verdicts(sealed: _Sealed, report: _Report) -> dict[str, str]:
    """这份报告里被反方审过的句子 → 判断（证据图 reports[] 的 challenge_verdicts）。正文据此给被审过的句子挂句末标记、
    点开整句面板：有的被审句子既没有可点的片段、也没有裁判判定，不靠这张表就打不开；也不用为了认出它们逐句请求片段接口。

    读法和 _challenge_of 一字不差（同一个 _reviews、同一个 _verdict_in）：每一句取最后跑完、审到了它的那次审稿，所以表里
    每一句的判断就是点开它时片段接口答的 unit.challenge.verdict。只有被审过的句子；一句都没审的是空表。
    """
    out: dict[str, str] = {}
    for _, _, _, claims in _reviews(sealed, report):
        units = dict.fromkeys(c.get("unit") for c in claims if isinstance(c.get("unit"), str) and c.get("unit"))
        out.update({uid: verdict for uid in units if (verdict := _verdict_in(claims, uid)) is not None})
    return out


def _challenge_counts(raw: Any, claims: list[dict[str, Any]]) -> dict[str, int]:
    """四种判断各几句：取节点产出（或审完时的日志）里的计数；一个数都认不出来时按认得的句子数（审稿记录里的是全部，
    记录没存上时只有产出里列的那几句）。"""
    given = raw if isinstance(raw, dict) else {}
    known = {v: given[v] for v in _CHALLENGE_VERDICTS
             if isinstance(given.get(v), int) and not isinstance(given[v], bool) and given[v] >= 0}
    if known:
        return {v: known.get(v, 0) for v in _CHALLENGE_VERDICTS}
    return {v: sum(1 for c in claims if c.get("verdict") == v) for v in _CHALLENGE_VERDICTS}


class _Masks:
    """一次请求里查过的数据源遮罩：数据源名 → (要遮的列（小写）, 数据源现在还在不在)。顺带记下数据源的类型（kind），
    按 SQL 认改了名字的遮罩列时照它的方言解析（_mask_sets）；数据源不在了的按通用读法。

    给了 sealed（证据面板的片段、证据图）时另能交回这次运行查到过的遮罩值（run_secrets），一次请求只收一次。一份快照的
    遮罩分析、收到的值另按快照工件记在进程里（_MEMO），之后的请求不再读、不再分析。"""

    def __init__(self, sealed: _Sealed | None = None) -> None:
        self._memo: dict[str, tuple[set[str], bool]] = {}
        self._kinds: dict[str, str | None] = {}
        self._sealed = sealed
        self._secrets: Any = None
        self._collected = False
        self._read: dict[str, tuple[Any, bool | None]] = {}
        self._sets: dict[tuple[str | None, str], _MaskSets] = {}
        self._cited: dict[tuple[str, str], set[str]] = {}
        self._graphs: list[_CopyGraph] = []
        self._outputs: dict[tuple[int, str, str], dict[str, str]] = {}

    def copies(self, sealed: _Sealed, *, output: Callable[[Any], Any] | None, by_value: bool, sure: bool) -> _CopyGraph:
        """这次请求里认指标的值给不给用的指标依赖图（_CopyGraph），按认法（output、by_value、sure）各一张：证据图给每个
        指标认标签、转抄同一个上游指标的几个指标，都在同一张图上认，认过的不再认。"""
        for graph in self._graphs:
            if graph.matches(sealed, output, by_value, sure):
                return graph
        graph = _CopyGraph(sealed, self, output=output, by_value=by_value, sure=sure)
        self._graphs.append(graph)
        return graph

    def card_outputs(self, sealed: _Sealed, card_node: str, artifact: str) -> dict[str, str]:
        """_card_outputs，一次请求里同一张卡的同一次执行只找一次：循环里每一次执行都转抄上一次的产出时，依赖图要逐次往回
        认，每次都把整次运行的事件扫一遍就成了执行次数乘事件数。调用方只读不改。"""
        key = (id(sealed), str(card_node), str(artifact))
        if key not in self._outputs:
            self._outputs[key] = _card_outputs(sealed, card_node, artifact)
        return self._outputs[key]

    def fetch(self, artifact: str) -> tuple[Any, bool | None]:
        """取回一件工件（_fetch）：一次请求里同一件只读一次。证据图要给每个指标认一遍输入，同一份快照、同一个节点的
        产出不必反复读盘、复验哈希。"""
        if artifact not in self._read:
            self._read[artifact] = _fetch(artifact)
        return self._read[artifact]

    async def prefetch(self, artifacts: Iterable[str]) -> None:
        """先在线程里把这几件工件取回来（_fetch：读盘、复验哈希），之后 fetch 直接用。报告撰写节点在运行里认遮罩
        （withheld_metrics），几 MB 的快照不该占着事件循环。"""
        todo = [a for a in dict.fromkeys(artifacts) if a and a not in self._read]
        if todo:
            self._read.update(await asyncio.to_thread(lambda: {a: _fetch(a) for a in todo}))

    async def sets(self, artifact: str | None, source: Any, snap: dict[str, Any] | None) -> _MaskSets:
        """一份查询快照的遮罩（_mask_sets），一次请求里同一份（按工件和数据源认）只认一次。snap 是这件工件读出来的内容，
        读不出来的为 None。"""
        key = (artifact, str(source or ""))
        if artifact is None or key not in self._sets:
            found = await _mask_sets(snap, source, self, artifact=artifact)
            if artifact is None:
                return found
            self._sets[key] = found
        return self._sets[key]

    async def cited(self, artifact: str | None, source: Any, snap: dict[str, Any]) -> set[str]:
        """一份读得出来的查询快照里确定取自遮罩列的结果列（小写，_cited_masks 的键：单元格引用的自查按它判违规），一次请求里
        同一份只认一次（_owned_columns 要按 SQL 血缘认，指标一个个认输入时不必反复算）。调用方只读不改。"""
        key = (str(artifact or ""), str(source or ""))
        if not artifact or key not in self._cited:
            found = set(await _cited_masks(snap, source, self, artifact))
            if not artifact:
                return found
            self._cited[key] = found
        return self._cited[key]

    async def of(self, source: Any) -> tuple[set[str], bool]:
        name = str(source or "")
        if not name:
            return set(), True
        if name not in self._memo:
            async with SessionLocal() as session:
                row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
            self._memo[name] = ({c.lower() for c in masked_columns(row.options if row else None)}, row is not None)
            self._kinds[name] = row.kind if row is not None else None
        return self._memo[name]

    async def kind(self, source: Any) -> str | None:
        name = str(source or "")
        if not name:
            return None
        await self.of(name)
        return self._kinds.get(name)

    async def memo_key(self, artifact: str | None, source: Any) -> tuple[Any, ...] | None:
        """一份快照在进程内记录（_MEMO）里的键：(工件, 数据源名, 数据源现在设的遮罩, 数据源在不在, 类型)。工件按内容
        寻址、不会变，一份快照的遮罩只取决于它的内容和后面几样；遮罩设置改了键就跟着变，不会拿旧的结果。合并查询、
        没有工件或数据源名的没有键（不记）。"""
        name = str(source or "")
        if not artifact or not name or name == MERGE_SOURCE:
            return None
        current, found = await self.of(name)
        return str(artifact), name, frozenset(current), found, self._kinds.get(name)

    async def value_keys(self, queries: dict[str, Any]) -> list[tuple[str, Any, tuple[Any, ...]]]:
        """封存范围内要收遮罩值的那几份查询快照：(工件, 数据源名, 进程内记录的键)。数据源现在设了遮罩、或者已经不在了的
        才收（masked_source）；合并查询不收：它的值都来自它的输入，输入另有快照。"""
        out: list[tuple[str, Any, tuple[Any, ...]]] = []
        for artifact, info in queries.items():
            source = (info or {}).get("source")
            if await self.masked_source(source) and (key := await self.memo_key(artifact, source)) is not None:
                out.append((str(artifact), source, key))
        return out

    async def run_secrets(self) -> Any:
        """这次运行查到过的遮罩值（masking._Secrets）：封存范围内、数据源现在设了遮罩（或者已经不在了）的每份查询快照里，
        确定取自遮罩列的那几列的值（_snapshot_secrets：_mask_sets 的 sure，SQL 认不准、整份遮的快照只遮显示，不收）。
        认不出出处、值却就是遮罩值的地方据此也不给：指标和输入的值（数据整形算出来再赋给变量的一格）、SQL 里的字面量
        （按号码筛的 WHERE phone IN (…)）、合并警告里举的例子。

        只在用到时收、一次请求只收一次；每份快照收到的值按快照工件记在进程里（_MEMO），之后的请求（推演也一样）不再读盘、
        复验哈希、做 SQL 分析，只读还没记下的那几份。没给 sealed、一个值都没收到时为 None。"""
        if self._collected:
            return self._secrets
        self._collected = True
        if self._sealed is None:
            return None
        parts: list[tuple[tuple[Any, ...], Any]] = []
        for artifact, source, key in await self.value_keys(self._sealed.queries):
            known, got = _MEMO.values(key)
            if not known:
                snap, ok = self.fetch(artifact)
                if ok is not True or not isinstance(snap, dict) or snap.get("source") == MERGE_SOURCE:
                    continue
                got = _MEMO.keep_values(key, _snapshot_secrets(snap, await self.sets(artifact, source, snap)))
            parts.append((key, got))
        self._secrets = _MEMO.pooled(parts)
        return self._secrets

    async def masked_source(self, source: Any) -> bool:
        """这个数据源上的查询快照要不要收遮罩值（run_secrets）：数据源现在设了遮罩，或者已经不在了（只能按快照里记下的
        认）。合并查询不收。"""
        if not source or source == MERGE_SOURCE:
            return False
        cols, found = await self.of(source)
        return bool(cols) or not found


def _snapshot_secrets(snap: dict[str, Any], sets: _MaskSets) -> Any:
    """一份查询快照里确定取自遮罩列的值（masking._Secrets，一个都没有为 None）：只收 sets.sure 那几列（列名就是遮罩列的、
    SQL 里认得出取自遮罩列的，没起别名、按位置认得出的 SELECT UPPER(phone) 也算），只装着分档标签的格子不收，和核查 agent
    在工具边界收值同一个口径（masking：认得清的照收，整份遮的快照只收确定的那几列）。SQL 认不准、整份遮的快照只遮显示：
    以前连它的区域名、金额一起收，之后每条 SQL 里的「'华东'」都显示成「已遮罩」。证据面板（_Masks.run_secrets）、推演
    （api/whatif.py 的 _Hidden.secrets）都按它收。"""
    from app.engine.masking import _Secrets

    secrets = _Secrets()
    secrets.add(snap, set(sets.sure), labels=dict(sets.labels), base=set(sets.base))
    return secrets if secrets.weak else None


#: 进程内记录里「没有这一项」（记下的值本身可以是 None：这份快照一个遮罩值都没有）
_ABSENT = object()


class _SnapshotMemo:
    """按查询快照工件记在进程里的遮罩分析（_MaskSets）和收到的遮罩值（masking._Secrets），跨请求共用。证据面板每点开一个
    片段、推演每算一次，都要知道这次运行查到过的遮罩值（_Masks.run_secrets、api/whatif.py 的 _Hidden.secrets）；以前缓存
    只在一次请求里有效，每次都把设了遮罩的数据源上的快照读盘、复验哈希、逐份做 SQL 分析（一份几十毫秒）。

    键见 _Masks.memo_key。只记读得出来的快照（读不出来、与哈希不一致的下次照旧去读：文件可能修好了）；合并查询不记（它的
    遮罩还取决于各个输入，输入另按自己的键记）。只记分析结果和收到的值，不记快照本身（几 MB 的行）：分析结果按份数封顶，
    收到的值按条数合计封顶，并起来的值按运行封顶，都是最近用到的留着。

    推演在工作线程里取、证据面板在事件循环里取，所以存取都加锁；记下的东西调用方只读不改：分析结果存成冻结的集合，
    并起来的值每次交出去一份新的（_shared：集合另抄一份、共用建好的索引），各自的正则缓存互不相扰。"""

    SETS_MAX = 1024
    VALUES_MAX = 200_000
    POOLS_MAX = 8

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sets: OrderedDict[tuple[Any, ...], _MaskSets] = OrderedDict()
        self._values: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._size = 0
        self._pools: OrderedDict[tuple[Any, ...], Any] = OrderedDict()

    def clear(self) -> None:
        with self._lock:
            self._sets.clear()
            self._values.clear()
            self._pools.clear()
            self._size = 0

    def sets(self, key: tuple[Any, ...]) -> _MaskSets | None:
        with self._lock:
            found = self._sets.get(key)
            if found is not None:
                self._sets.move_to_end(key)
            return found

    def keep_sets(self, key: tuple[Any, ...], sets: _MaskSets) -> _MaskSets:
        frozen = _MaskSets(frozenset(sets.hidden), frozenset(sets.sure), frozenset(sets.base),  # type: ignore[arg-type]
                           {k: frozenset(v) for k, v in sets.labels.items()}, list(sets.gone))  # type: ignore[misc]
        with self._lock:
            self._sets[key] = frozen
            self._sets.move_to_end(key)
            while len(self._sets) > self.SETS_MAX:
                self._sets.popitem(last=False)
        return frozen

    def values(self, key: tuple[Any, ...]) -> tuple[bool, Any]:
        """(记没记过, 这份快照收到的值)。"""
        with self._lock:
            found = self._values.get(key, _ABSENT)
            if found is _ABSENT:
                return False, None
            self._values.move_to_end(key)
            return True, found

    def keep_values(self, key: tuple[Any, ...], secrets: Any) -> Any:
        with self._lock:
            old = self._values.pop(key, _ABSENT)
            if old is not _ABSENT:
                self._size -= _value_count(old)
            self._values[key] = secrets
            self._size += _value_count(secrets)
            while self._size > self.VALUES_MAX and len(self._values) > 1:
                _, dropped = self._values.popitem(last=False)
                self._size -= _value_count(dropped)
        return secrets

    def pooled(self, parts: list[tuple[tuple[Any, ...], Any]]) -> Any:
        """几份快照收到的值并成一份、建好索引（上万个值建一次要几十毫秒），按这几份的键记下；交出去的是 _shared 的一份。
        一个值都没有为 None。"""
        if not parts:
            return None
        key = tuple(k for k, _ in parts)
        with self._lock:
            found = self._pools.get(key, _ABSENT)
            if found is not _ABSENT:
                self._pools.move_to_end(key)
        if found is _ABSENT:
            found = _pool([one for _, one in parts])
            with self._lock:
                self._pools[key] = found
                while len(self._pools) > self.POOLS_MAX:
                    self._pools.popitem(last=False)
        return _shared(found)


def _value_count(secrets: Any) -> int:
    return len(secrets.weak) if secrets is not None else 0


def _pool(parts: list[Any]) -> Any:
    """几份 masking._Secrets 并成一份（合起来最多 SECRETS_MAX 种，同 _Secrets.merge），建好索引；一个值都没有为 None。"""
    from app.engine.masking import _Secrets

    out = _Secrets()
    for one in parts:
        out.merge(one)
    if not out.strong:
        return None
    out.prepare()
    return out


def _shared(secrets: Any) -> Any:
    """记在进程里的一份遮罩值交给一个请求用：值的集合另抄一份（调用方往里并值、免遮不碍别人），建好的索引共用（只读），
    正则缓存各起一份（_Secrets.pattern 会改它）。"""
    if secrets is None:
        return None
    from app.engine.masking import _Secrets

    return _Secrets(strong=set(secrets.strong), weak=set(secrets.weak), shown=set(secrets.shown),
                    protected=secrets.protected, _index=secrets._index)


#: 证据面板、推演、报告自查共用的进程内记录（见 _SnapshotMemo）
_MEMO = _SnapshotMemo()


@dataclass
class _MaskSets:
    """一份查询快照的遮罩（_mask_sets）：

    - hidden：要遮的列（小写）。证据面板、推演、勾行按它遮，宁可多遮：SQL 认不准时整份的列都算
    - sure：其中能确定取自遮罩列的：列名就是遮罩列的，SQL 里认得出取自遮罩列的（别名、函数、子查询、WITH，没起别名
      却按位置认得出的表达式）。SQL 认不准、整份算进来的那几列不在里面，只装着分档标签的列（CASE 写死的字）也不在。
      收「这次运行查到过的遮罩值」只收这几列（_snapshot_secrets），交给结论句裁判的摘录逐份遮这几列（_judge_loader），
      报告自查从这几列起（_cited_masks，只排除血缘证明值取自普通表别的列的）
    - base：遮罩列本身（查询当时记下的加数据源现在设的，小写）
    - labels：只装着分档标签的列 → 标签的写法（masking._snapshot_labels），收遮罩值时不收标签
    - gone：已经不在的数据源

    记进进程内记录（_MEMO）的是冻结的集合：几个请求共用一份，调用方只读不改。"""

    hidden: set[str]
    sure: set[str]
    base: set[str]
    labels: dict[str, set[str]] = field(default_factory=dict)
    gone: list[str] = field(default_factory=list)


def _derived(snap: dict[str, Any], hidden: set[str], kind: str | None,
             tables: dict[str, list[str]] | None = None) -> set[str]:
    """一份查询快照里值取自遮罩列（hidden，小写）的结果列（小写，含 hidden 本身），证据面板、推演按它遮。

    和可验证报告是同一个函数（bundle/build.py 的 _derived_columns：按每种支持的库的读法各认一遍合起来，语法树认不准的
    写法——MySQL 会执行的注释、U&"…"、表名写在字符串里交给函数、统计信息表——和列名表、整行引用、不照原名展开的 *、
    吃进遮罩列的表函数，整份的列都算），再按数据源自己的方言认一遍（engine/masking.py 的 _derived_hidden，核查 agent
    在工具边界遮的那一套；tables 是 * 展开时数位置用的表结构，合并查询交的是各输入的列）。可验证报告整份遮的，这里不会
    照给。合并查询的快照这里不看（来源不是数据源），由 _merge_mask_sets 换个说法交进来。

    分析本身出了意外：整份的列都算（宁可多遮）。纯计算，一条 SQL 几毫秒，在调用方的线程里算：推演在工作线程里等事件循环
    查库（api/whatif.py 的 _Hidden），这里再开线程会和它争同一个线程池。"""
    if not hidden:
        return set()
    try:
        # bundle、masking 都用到这里的说法（MASKED），用到时才导入：都不在模块顶上互相导入
        from app.bundle.build import _derived_columns
        from app.engine.masking import _derived_hidden

        return set(hidden) | _derived_columns(snap, set(hidden)) | _derived_hidden(snap, set(hidden), kind, tables)[0]
    except Exception:  # noqa: BLE001 - 认不准就整份遮掉
        logger.warning("证据面板：按 SQL 认改了名字的遮罩列时出错，整份快照的列都按遮罩处理", exc_info=True)
        return set(hidden) | {str(c).lower() for c in snap.get("columns") or []}


def _sure(snap: dict[str, Any], base: set[str], kind: str | None,
          tables: dict[str, list[str]] | None = None) -> tuple[set[str], dict[str, set[str]]]:
    """(能确定取自遮罩列的结果列, 只装着分档标签的列)，都是小写（_MaskSets.sure）：收遮罩值、交给裁判的摘录、报告自查
    都从前一项起。

    按数据源自己的方言认（masking._derived_masks，工具边界遮的那一套）：结果列名就是遮罩列、SQL 里指代遮罩列的名字
    （c.phone AS tel 的 tel、UPPER(phone) AS p 的 p）、没起别名却按位置认得出取自遮罩列的（SELECT UPPER(phone) FROM
    customers）都算。认不准时那一套整份遮（whole：派生来源里没起别名的表达式、PIVOT、SQL 解析不了又写着遮罩列……），
    这时只认它的 collect——名字对上的、整份算之前已按位置认出的那几列。展示上多遮无妨，这里多算，自查就判失败、写作者
    也改不掉（最大的一笔订单金额被说成取自手机号），区域名、金额也被收成遮罩值。整份算不算以那一套交回的 whole 为准：
    以前在结果后面垫一列再认一遍来探测，那一套把「没有 * 的一支、列数和结果对不上」也判整份之后，垫过的每条 SQL 都成了
    整份算，按位置认得出的列一个都收不到（合入 K3b 之后的复核）。
    只装着分档标签的列（CASE WHEN phone IS NULL THEN '未留' ELSE '已留' END）格子里是 SQL 写死的字，不算；只数个数的
    （COUNT(*) FILTER (WHERE phone IS NULL)、SUM(phone IS NULL)）先改写成那一套认得出的样子再认（_counted）。
    出了意外只认列名对上的。
    整份算的快照里，列名本身就是取自遮罩列的表达式的（* 展开派生表、WITH 里没起别名的 UPPER(phone)）另按列名认
    （_named_from_masks）：那一套整份算之前认不出它落在哪个位置，collect 里没有，指标、单元格引用照给了号码。"""
    columns = {str(c).lower() for c in snap.get("columns") or []}
    direct = set(base) & columns
    if not base or not columns:
        return direct, {}
    try:
        from app.engine.masking import _derived_masks, _snapshot_labels

        found = _derived_masks(_counted(snap, kind), set(base), kind, tables)
        sure = ((found.collect if found.whole else found.hidden) & columns) | direct
        if found.whole:
            sure |= _named_from_masks(snap, base, kind)
        labels = _snapshot_labels(snap, kind)
        return sure - {name for name in labels if name not in base}, labels
    except Exception:  # noqa: BLE001 - 认不准：只认列名对上的
        logger.warning("报告自查：按 SQL 认取自遮罩列的结果列时出错，只认列名对上的", exc_info=True)
        return direct, {}


def _named_from_masks(snap: dict[str, Any], base: set[str], kind: str | None) -> set[str]:
    """SQL 认不准、整份算的快照里，列名本身就是一个取自遮罩列的表达式的结果列（小写，_sure）：没起别名的表达式，SQLite、
    MySQL 拿表达式的原文当列名（UPPER(phone)、phone || ''、SUBSTR(phone, 1, 11)），* 展开派生表、WITH、UNION 的一支时原样
    带出来。认遮罩列的那一套（masking._derived_masks）认不出 * 展开到了哪个位置，整份算、collect 里没有它：写作目录、
    单元格引用的自查就照给了号码。

    按列名拼一条只选这一项的 SQL，交给同一套认（同 _sure，只数个数的先改写，_counted）：认得出取自遮罩列、又不是只装着
    分档标签的（masking._label_columns）才算；COUNT(phone)、COUNT(*) FILTER (WHERE phone IS NULL)、LOWER(name) 这类不算。
    只认列名里写着遮罩列名字的（masking._mentions），别的列不拼。Postgres 给这种列起的名字是函数名（upper），认不出，
    照旧只在证据面板、可验证报告那边宁可多遮。"""
    from app.engine.masking import _derived_masks, _label_columns, _mentions

    out: set[str] = set()
    for column in snap.get("columns") or []:
        name = str(column)
        lowered = name.lower()
        if lowered in base or not _mentions(name, base):
            continue
        probe = _counted({"sql": f"SELECT {name} FROM t", "columns": [name]}, kind)
        found = _derived_masks(probe, set(base), kind)
        if found.whole or lowered not in found.hidden or lowered in _label_columns(probe["sql"], kind, [name]):
            continue
        out.add(lowered)
    return out


def _counted(snap: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """报告自查认确定取自遮罩列的列之前（_sure），把只决定算不算进来的写法改写成认遮罩列的那一套认得出的样子：

    - 汇总的 FILTER (WHERE …) 去掉：条件只决定这一行算不算进来，同写在 WHERE 里；
    - 汇总里结果为真假的条件（SUM(phone IS NULL)、AVG(LENGTH(phone) = 11)、MAX(phone LIKE '199%')）改成
      CASE WHEN … THEN 1 ELSE 0 END：那一套认它只数个数（masking._band_only）。

    COUNT(*) FILTER (WHERE phone IS NULL) 数的是没留号码的人数，值里没有号码；那一套把 FILTER 的条件、汇总里的条件当成
    值的来历，自查跟着判违规，正式运行就因为一个人数判失败。只改自查用的这一份：证据面板、推演照可验证报告的认法多遮
    （展示上多遮无妨，可验证报告同样把它算作取自遮罩列），根上的改法在 masking 里。没有这些写法、改写不了的原样交回。"""
    sql = snap.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return snap
    try:
        from sqlglot import exp

        from app.data.sql_semantics import _parse, _read

        root, _ = _parse(sql, _read(kind))
        if root is None:
            return snap
        changed = False
        for node in list(root.find_all(exp.Filter)):
            node.replace(node.this)
            changed = True
        for agg in list(root.find_all(exp.AggFunc)):
            arg = agg.this
            while isinstance(arg, exp.Paren):
                arg = arg.this
            if not isinstance(agg, exp.Count) and isinstance(arg, (exp.Predicate, exp.Connector, exp.Not)):
                agg.set("this", exp.Case(ifs=[exp.If(this=arg.copy(), true=exp.Literal.number(1))],
                                         default=exp.Literal.number(0)))
                changed = True
        return {**snap, "sql": root.sql(dialect=_read(kind))} if changed else snap
    except Exception:  # noqa: BLE001 - 改写不了就照原样认（多算的那一边）
        logger.warning("报告自查：改写只数个数的写法时出错，照原样认", exc_info=True)
        return snap


async def _mask_sets(snap: dict[str, Any] | None, source: Any, masks: _Masks, *, depth: int = 0,
                     artifact: str | None = None) -> _MaskSets:
    """一份查询快照的遮罩（_MaskSets）：证据面板的查询步骤、指标的输入、推演、报告自查（engine/nodes/report.py）都按这一个
    函数认。

    - 遮罩列是「查询当时记下的」和「数据源现在设的」两者之和：事后加的遮罩照样生效，数据源改名、删掉了也不会把当时
      遮着的列亮出来；数据源不在了的，它的名字进 gone
    - 列名对上的之外，按 SQL 认出改了名字的遮罩列：要遮的按 _derived（和可验证报告同一个函数），确定的按 _sure
    - 合并查询不是数据源：按它合并的那几个数据源、逐格来历和合并 SQL 认（_merge_mask_sets）
    - snap 为 None（快照读不出来）：只能按 source 现在设的遮罩算，没有 SQL 可认

    artifact 是 snap 的工件 id：给了的，分析结果按快照记在进程里（_MEMO，键见 _Masks.memo_key），之后的请求不再分析。"""
    if snap is not None and snap.get("source") == MERGE_SOURCE:
        return await _merge_mask_sets(snap, masks, depth=depth)
    if snap is not None and snap.get("capability"):
        # 端点快照：列是能力登记的表格映射排出来的，没有 SQL 可认、也不该按数据源认（端点不是数据源，按数据源找不到会把
        # 整张表都当成「数据源不在了」遮掉）。按列名遮：落快照时记下的加能力现在的遮罩字段，确定就是这几列
        from app.capabilities.registry import snapshot_masked_columns

        base = await snapshot_masked_columns(snap)
        return _MaskSets(set(base), set(base), base)
    name = source or (snap or {}).get("source")
    current, found = await masks.of(name)
    base = set(current) | (_recorded_masks(snap) if snap is not None else set())
    gone = [] if found or not name else [str(name)]
    if snap is None or not base:
        return _MaskSets(set(base), set(base), base, gone=gone)
    key = await masks.memo_key(artifact, name) if artifact else None
    if key is not None and (kept := _MEMO.sets(key)) is not None:
        return kept
    kind = await masks.kind(name)
    sure, labels = _sure(snap, base, kind)
    out = _MaskSets(_derived(snap, base, kind) | sure, sure, base, labels, gone)
    return _MEMO.keep_sets(key, out) if key is not None else out


async def _cited_masks(snap: dict[str, Any], source: Any, masks: _Masks,
                       artifact: str | None = None) -> dict[str, list[str]]:
    """报告自查（engine/nodes/report.py 的 _MaskedCells）认一份查询快照里引用了就记 masked_cell 的列：{结果列（小写）:
    它的出处}，违规说明点名出处（认得出表的写「表.列」，见 _owned_columns）。

    从 _mask_sets 的 sure 起（列名就是遮罩列的、SQL 里认得出取自遮罩列的；SQL 认不准、整份算进来的和只装着分档标签的
    不在里面）。遮罩按列名在整个数据源上生效，和证据面板一样：哪张表（含视图）里叫这个名字的列都算，门店名（stores.name）
    和客户姓名（customers.name）同叫 name、都设了遮罩时两者都算。普通查询再按血缘点名出处（_owned_columns）；合并查询的值
    来自它的各个输入、不是数据源里的表，照 sure 算。"""
    sets = await _mask_sets(snap, source, masks, artifact=artifact)
    if not sets.sure:
        return {}
    if snap.get("source") == MERGE_SOURCE:
        return {c: sorted(sets.base) for c in sorted(sets.sure)}
    return _owned_columns(snap, set(sets.sure), set(sets.base), await masks.kind(source or snap.get("source")))


def _owned_columns(snap: dict[str, Any], sure: set[str], base: set[str], kind: str | None) -> dict[str, list[str]]:
    """sure 里每一列的出处：{结果列: 它取自的遮罩列}，认得出表的写「表.列」（stores.name、customer_spend.name），认不出的
    写这份快照的全部遮罩列。报告自查按它判违规、写违规说明。

    遮罩按列名设在整个数据源上（data/engine.py 的 masked_columns 只认列名，不带表名）：设了 name，stores.name、
    customers.name、视图里的 name 都算，和证据面板、可验证报告的遮法一致。自查不推断「这项遮罩本意管哪张表」：第 3 轮
    这样推断过（遮罩列在几张表里都有时只认另有别的遮罩列的那几张），视图、没有第二个遮罩列的表（employees.name）因此被
    排除，正式运行照常成功、姓名写进了报告。SQL 血缘（sqlglot.lineage，按查询当时冻结的表结构展开 *、认出不带表名的列出自
    哪张表）只用来：

    - 点名出处：血缘的末端里叫遮罩列名字的列，按出现的先后写「表.列」，读的人看得出是哪一列被这项遮罩管到了
    - 排除值确实不取自遮罩列的（SELECT region AS phone FROM stores）：每个末端都落在表结构里标明是普通表（is_view 为
      false）的列上、没有一处叫遮罩列的名字，才排除
    - 认不出出处的照算（_owned_masks）：血缘认不出的列（没起别名的表达式……）、表值函数、视图（看不出它的列取自哪张表）、
      表结构里没有的表、没有冻结表结构的快照、表结构没标是不是视图的老快照

    只排除、不新增：sure 里没有的列血缘认出来也不算。分析出了意外，照 sure 算（出处写全部遮罩列）。纯计算，在调用方的
    线程里算。"""
    plain = {c: sorted(base) for c in sorted(sure)}
    sql = snap.get("sql")
    if not sure or not isinstance(sql, str) or not sql.strip():
        return plain
    try:
        from app.data.sql_semantics import _parse, _read

        read = _read(kind)
        root, _ = _parse(sql, read)
        if root is None:
            return plain
        artifact = snap.get("schema_artifact")
        tables, ordinary = _frozen_schema(artifact) if isinstance(artifact, str) else ({}, frozenset())
        schema = {name: {c: "TEXT" for c in cols} for name, cols in tables.items()} or None
        nodes = _lineages(root, schema, read, [str(c) for c in snap.get("columns") or [] if str(c).lower() in sure])
        out: dict[str, list[str]] = {}
        for column in sorted(sure):
            found = _owned_masks(nodes.get(column), base, ordinary)
            if found:
                out[column] = found
        return out
    except Exception:  # noqa: BLE001 - 核对不了出处：照 sure 算
        logger.warning("报告自查：按血缘核对遮罩列的出处时出错，照列名认", exc_info=True)
        return plain


@functools.lru_cache(maxsize=64)
def _frozen_schema(artifact: str) -> tuple[dict[str, list[str]], frozenset[str]]:
    """表结构快照工件 → ({表名: 按次序的列名}, 标明是普通表的表名)，都是小写（_owned_columns）。

    前一项只要不带库名的那份名字（表结构里一张表按好几个名字记着，masking._schema_keys），血缘按它展开 *、认不带表名的
    列。后一项是 is_view 明写为 false 的表（带不带库名的名字都记）：视图的列看不出取自哪张表，没标的老快照看不出是表还是
    视图；几个库里同名的表有一张不是普通表，这个名字就不算。取不到、被改过的两样都是空的。工件按内容寻址、不会变，记下来。"""
    from app.engine.masking import _schema_columns, _schema_keys

    try:
        schema = load(artifact)
    except Exception:  # noqa: BLE001 - 取不到就当没有冻结的表结构（出处认不出的照算）
        return {}, frozenset()
    entries = schema.get("tables") if isinstance(schema, dict) else None
    if not isinstance(entries, dict):
        return {}, frozenset()
    columns = {name: cols for name, cols in (_schema_columns(schema) or {}).items() if "." not in name}
    ordinary: dict[str, bool] = {}
    for key, meta in entries.items():
        if isinstance(meta, dict):
            for name in _schema_keys(key, meta):
                ordinary[name] = ordinary.get(name, True) and meta.get("is_view") is False
    return columns, frozenset(name for name, yes in ordinary.items() if yes)


def _lineages(root: Any, schema: dict[str, Any] | None, read: str | None, columns: list[str]) -> dict[str, Any]:
    """结果列（小写）→ 它的血缘（sqlglot.lineage 的节点）。先一次认全部的列（只解析、补全一遍）；认不了的逐列再认，还是
    认不出的不在里面（_owned_masks 当认不出出处）。"""
    from sqlglot.lineage import lineage

    try:
        found = lineage(None, root.copy(), schema=schema, dialect=read)
        every = {str(k).lower(): v for k, v in found.items()} if isinstance(found, dict) else {}
    except Exception:  # noqa: BLE001 - 有一列认不了就整条认不了：下面逐列认
        every = {}
    out: dict[str, Any] = {}
    for column in columns:
        if (node := every.get(column.lower())) is None:
            try:
                node = lineage(column, root.copy(), schema=schema, dialect=read)
            except Exception:  # noqa: BLE001 - 认不出这一列（没起别名的表达式……）
                continue
        out[column.lower()] = node
    return out


def _owned_masks(node: Any, base: set[str], ordinary: frozenset[str]) -> list[str]:
    """一个结果列的出处：血缘的末端里叫遮罩列名字的列（哪张表、哪个视图的都算），按出现的先后写「表.列」。

    一处都没有时：有认不出出处的末端——血缘认不出来（node 为 None）、_leaf_of 交回 None（表值函数、整张表的 *……）、落在
    不是普通表（ordinary，_frozen_schema）的表上（视图、表结构里没有的表）——排除不了，写全部遮罩列（照算）；每个末端都落在
    普通表的别的列上（region AS phone）的交回空：不算。"""
    if node is None:
        return sorted(base)
    found: list[str] = []
    unplaced = False
    for leaf in node.walk():
        if leaf.downstream:
            continue
        place = _leaf_of(leaf)
        if place is None:
            unplaced = True
            continue
        if place is _NO_COLUMN:
            continue
        table, column = place
        if column in base:
            if (named := f"{table}.{column}") not in found:
                found.append(named)
        elif table not in ordinary:
            # 视图、表结构里没有的表：看不出这一列取自哪里，排除不了
            unplaced = True
    return found or (sorted(base) if unplaced else [])


#: 血缘里的一个末端没有取任何列（写死的值、COUNT(*)）
_NO_COLUMN = object()


def _leaf_of(leaf: Any) -> Any:
    """血缘的一个末端出自哪张表的哪一列：(表名, 列名)，都是小写，表名照 SQL 里写的带上库名（archive.sales）；没有取任何列
    的（写死的值、COUNT(*)）交回 _NO_COLUMN；认不出出处的（sqlglot 找不到来源、表值函数、整张表的 *、派生来源里的 *）交回
    None。"""
    from sqlglot import exp

    source = leaf.source
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
        try:
            column = exp.to_column(leaf.name)
        except Exception:  # noqa: BLE001 - 名字读不出来：认不出
            return None
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or not column.name:
            return None
        table = ".".join(part for part in (source.catalog, source.db, source.name) if part)
        return table.lower(), column.name.lower()
    if isinstance(source, (exp.Table, exp.Placeholder)) or leaf.expression is source:
        return None
    return _NO_COLUMN


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
    merged = snap.get("source") == MERGE_SOURCE
    # 遮的列：查询当时记下的加数据源现在设的，按 SQL 认出改了名字的（c.phone AS tel）也遮。合并查询不是数据源，按它
    # 合并的那几个数据源、逐格来历和合并 SQL 算，不能拿「合并查询」去找数据源，找不到再说它「已不存在」（_mask_sets）
    sets = await masks.sets(artifact, step["source"], snap)
    hidden, gone = sets.hidden, sets.gone
    notes = [SOURCE_GONE.format(source="、".join(gone))] if gone else []
    # SQL、合并 SQL、SQL 检查的片段、合并警告里写着的遮罩值（按号码筛的 WHERE phone IN (…)、按号码合并时警告举的例子）
    # 换成「已遮罩」，和可验证报告认 SQL 字面量同一个口径（这次运行查到过的遮罩值）。只换展示的这一份，执行的原文、
    # 哈希核对用的快照都不动
    shown = _Shown(await masks.run_secrets() if _has_values(snap) else None)
    if merged:
        step["merge"] = shown.merge(_merge_view(snap, cells, catalog, sealed))
        if hint and _leads_to_upload(snap, cells):
            step["provenance"] = True
    masked = {i for i, c in enumerate(columns) if c.lower() in hidden}
    index = sorted({i for r in rows_hit for i in range(r - WINDOW, r + WINDOW + 1) if 0 <= i < len(rows)})
    step.update(sql=shown.text(snap.get("sql")), columns=columns, total_rows=len(rows),
                truncated=bool(snap.get("truncated")), masked=[columns[i] for i in sorted(masked)],
                # 查询时从驱动的原始值记下的列类型（老快照没有）：文本列里的 "2026" 按文本显示，不当数
                column_types=snap.get("column_types") if isinstance(snap.get("column_types"), dict) else {},
                **({"provenance": True} if hint and snap.get("data_version") and not merged else {}),
                **({"checks": shown.checks(checks)} if (checks := _query_checks(snap)) else {}),
                **_catalog_of(snap))
    if shown.changed:
        notes.append(SQL_MASKED_NOTE)
    if notes:
        step["mask_note"] = "；".join(notes)
    if len(index) > MAX_WINDOW_ROWS:
        index, step["window_truncated"] = index[:MAX_WINDOW_ROWS], True
    step["row_index"] = index
    step["row_offset"] = index[0] if index else 0
    step["rows"] = [[MASKED if i in masked else v for i, v in enumerate(rows[r])] if isinstance(rows[r], list)
                    else rows[r] for r in index]
    return step


class _Shown:
    """查询步骤上要展示的 SQL、合并 SQL、SQL 检查的片段、合并警告：里面写着的遮罩值（secrets，这次运行查到过的，
    masking._Secrets）换成「已遮罩」，换了写法的长号码也认。secrets 为 None 时原样交回。changed：换过东西，查询步骤
    底下要说清这只是展示时遮的。"""

    def __init__(self, secrets: Any) -> None:
        self._secrets = secrets
        self.changed = False

    def text(self, value: Any) -> Any:
        if self._secrets is None or not isinstance(value, str) or not value:
            return value
        out = self._secrets.scrub(value, loose=True)
        self.changed = self.changed or out != value
        return out

    def checks(self, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**c, **{k: self.text(c[k]) for k in ("message", "sql_excerpt") if isinstance(c.get(k), str)}}
                for c in checks]

    def merge(self, view: dict[str, Any]) -> dict[str, Any]:
        view["sql"] = self.text(view.get("sql"))
        view["warnings"] = [{**w, "message": self.text(w.get("message"))} for w in view.get("warnings") or []]
        view["inputs"] = [{**i, **({"checks": self.checks(i["checks"])} if i.get("checks") else {})}
                          for i in view.get("inputs") or []]
        return view


def _has_values(snap: dict[str, Any]) -> bool:
    """查询步骤上有没有可能写着遮罩值的文字：SQL 里有字符串、五位以上的数（按值认的门槛，见 masking.SECRET_DIGITS），
    或者有 SQL 检查结果、合并警告。都没有的不必去取这次运行的遮罩值（要把封存的查询快照读一遍）。"""
    sql = snap.get("sql")
    return (isinstance(sql, str) and bool(_LITERALISH.search(sql))) or bool(snap.get("checks")) \
        or bool(snap.get("warnings"))


#: SQL 里像是写着值的地方：引号括起来的字、五位以上的数（「已遮罩」只换这些，见 _Shown）
_LITERALISH = re.compile(r"['\"]|\d{5,}")


def _catalog_of(snap: dict[str, Any]) -> dict[str, Any]:
    """{schema_artifact, catalog}：这条查询当时冻结的表结构快照，以及它用到的表在快照里冻结的目录版本。

    数据源查询工具每次查询都把当时的表结构和数据目录冻结进表结构快照（tools/datasource._store_schema），SQL 检查
    对照的就是这一版。面板上写「数据目录：入园记录 第 3 版」：事后目录改了、升了版本，看得出这次查询按的是哪一版。
    catalog 只列 SQL 用到、而且冻结了目录的表：[{table, label?, version}]。这条查询没对照过目录（合并查询、老快照、
    没探查过结构或没有目录的源、表结构快照读不出来）时两个键都不加：步骤的形状和以前一字不差（期 4 的金样逐字比对）。
    """
    schema_id = snap.get("schema_artifact")
    if not isinstance(schema_id, str) or not schema_id:
        return {}
    try:
        schema = load(schema_id)
    except Exception:  # noqa: BLE001 - 读不出来：不编版本
        return {}
    frozen = schema.get("catalog") if isinstance(schema, dict) else None
    if not isinstance(frozen, dict) or not frozen:
        return {}
    from app.data.catalog import resolve_table_name

    listed: list[dict[str, Any]] = []
    for name in sql_tables(str(snap.get("sql") or "")):
        key = resolve_table_name(schema, name)
        entry = frozen.get(key) if key else None
        if not isinstance(entry, dict) or any(t["table"] == key for t in listed):
            continue
        label = ((entry.get("notes") or {}).get("label") or {}).get("value")
        listed.append({"table": key, **({"label": label} if isinstance(label, str) and label else {}),
                       "version": entry.get("version")})
    return {"schema_artifact": schema_id, "catalog": listed} if listed else {}


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
# 合并查询的结果：表级来历（输入、合并 SQL）和逐格来历（被引用的格追到输入的哪一格）
# --------------------------------------------------------------------------

#: 一条链最多追几层合并（合并的合并）。画布上不会无限嵌套，这只是防御坏快照
MERGE_DEPTH = 4


async def _query_steps(artifact: str, cells: list[tuple[Any, Any]], doc: dict[str, Any], sealed: _Sealed,
                       masks: _Masks, *, hint: bool = False, depth: int = 0) -> list[dict[str, Any]]:
    """查询步骤；快照是合并结果时，后面接上被引用的格追到的那几个输入的查询步骤（高亮追到的那一格）。

    追不到的格不接任何输入步骤：合并步骤的 merge.traced 里照实写「没有逐格来历」，只给表级来历。输入步骤带
    merged_into（合并结果的编号），界面据此标明它是哪次合并的输入。不是合并结果的快照，返回的就是原来那一个步骤。
    """
    step = await _query_step(artifact, cells, doc, sealed, masks, hint=hint)
    traced = [t for t in (step.get("merge") or {}).get("traced") or [] if t.get("input")]
    if not traced or depth >= MERGE_DEPTH:
        return [step]
    by_input: dict[str, list[tuple[Any, Any]]] = {}
    artifacts = {str(i.get("alias")): str(i.get("artifact") or "") for i in step["merge"]["inputs"]}
    for t in traced:
        by_input.setdefault(artifacts.get(str(t["input"]), ""), []).append((t["row"], t["column"]))
    out = [step]
    for source, picked in by_input.items():
        if not source:
            continue
        for sub in await _query_steps(source, picked, doc, sealed, masks, depth=depth + 1):
            out.append({**sub, "merged_into": sub.get("merged_into") or step.get("alias")})
    return out


def _merge_view(snap: dict[str, Any], cells: list[tuple[Any, Any]], catalog: dict[str, Any],
                sealed: _Sealed) -> dict[str, Any]:
    """合并步骤的 merge 一节：每个输入（别名、节点、节点名、目录编号、行数、数据源、封存与否）、合并 SQL、执行时的
    警告，以及被引用的每一格追到了哪个输入的哪一格（追不到的写明原因）。"""
    labels = {str(n.get("id")): (n.get("data") or {}).get("label") or None
              for n in sealed.graph.get("nodes") or [] if isinstance(n, dict)}
    inputs = []
    for item in snap.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        art = str(item.get("artifact") or "")
        inputs.append({"alias": item.get("alias"), "node_id": item.get("node_id"),
                       "label": labels.get(str(item.get("node_id"))), "query": _query_alias(catalog, art),
                       "rows": item.get("rows"), "source": item.get("source"), "artifact": art or None,
                       "sealed": sealed.trusted and art in sealed.queries,
                       # 输入查询的 SQL 检查结果：被引用的格追不到逐格来历时没有输入步骤，指标却因为它标了「存疑」，
                       # 问题得在合并步骤里看得到。没查出问题、快照读不出来的输入不加这个键
                       **({"checks": checks} if (checks := _input_checks(art)) else {})})
    traced = []
    for row, column in dict.fromkeys((r, c) for r, c in cells if isinstance(r, int) and c):
        hit = trace_cell(snap, row, column)
        if hit is None:
            traced.append({"cell": [row, str(column)], "input": None, "query": None, "row": None, "column": None,
                           "note": REASON_TEXT["merge_no_lineage"]})
        else:
            traced.append({"cell": [row, str(column)], "input": hit.alias, "query": _query_alias(catalog, hit.artifact),
                           "row": hit.row, "column": hit.column})
    warnings = [{"code": w.get("code"), "message": w.get("message")} for w in snap.get("warnings") or []
                if isinstance(w, dict) and w.get("message")]
    return {"sql": snap.get("sql"), "inputs": inputs, "warnings": warnings, "traced": traced}


def _input_checks(artifact: str) -> list[dict[str, Any]]:
    """合并查询某个输入的 SQL 检查结果（_query_checks 的形状）。读不出来的快照返回空列表：快照的问题输入步骤另有说法。"""
    if not artifact:
        return []
    try:
        snap = load(artifact)
    except Exception:  # noqa: BLE001 - 被改过、读不出来：这里不报
        return []
    return _query_checks(snap) if isinstance(snap, dict) else []


async def _merge_masks(snap: dict[str, Any], masks: _Masks, *, depth: int = 0) -> tuple[set[str], list[str]]:
    """合并结果要遮的列（小写）和已经不在的数据源（_merge_mask_sets 的 hidden、gone）。质询、双盲复算按这个名字调用。"""
    sets = await _merge_mask_sets(snap, masks, depth=depth)
    return sets.hidden, sets.gone


async def _merge_mask_sets(snap: dict[str, Any], masks: _Masks, *, depth: int = 0) -> _MaskSets:
    """合并结果的遮罩（_MaskSets）：

    - 合并当时记下的（快照的 mask_columns：各输入当时的遮罩列，加上来自遮罩列、换了名字的结果列）和它合并的每个数据源
      现在设的，按列名；
    - 结果列有逐格来历的，按来历遮：来自的那一列在输入里要遮（_merge_input_sets：输入快照按查询步骤同一套认，数据源
      现在设的、SQL 里改了名字的都算，输入本身是合并结果的再往下追），换了名字也遮住；
    - 合并 SQL 本身也按 SQL 认（_derived、_sure，SQLite 的方言：合并在内存 SQLite 里执行）：各输入里要遮的列名当作遮罩
      列，合并 SQL 里对它们改名、套函数、拼接、汇总、经 WITH 转一道得来的结果列都算。逐格来历只认单层直接选列的写法，
      以前 SELECT a.tel AS contact, SUM(b.amount) … GROUP BY a.tel 这种合并没有来历，号码在证据面板、勾行里原样给出；
    - 输入快照读不出来（追得太深也算）、又可能带着遮罩的：它的列全部当遮罩列（宁可多遮），来历指向它的结果列也遮；
      连它有哪几列都不知道的（老快照没记输入的列），整份结果的列都遮。

    确定的（sure）只按确定的认：输入里确定取自遮罩列的列名、来历指向它们的结果列、合并 SQL 里认得出的；输入读不出来时
    只认它的数据源现在设的列名。"""
    recorded = _recorded_masks(snap)
    base = set(recorded)
    gone: list[str] = []
    for name in snap.get("sources") or []:
        cols, found = await masks.of(name)
        base |= cols
        if not found:
            gone.append(str(name))
    columns = [str(c).lower() for c in snap.get("columns") or []]
    hidden, sure = set(base), set(base)
    by_alias = {str(i.get("alias")): i for i in snap.get("inputs") or [] if isinstance(i, dict)}
    inputs = {alias: await _merge_input_sets(item, masks, depth, bool(recorded)) for alias, item in by_alias.items()}
    lineage = snap.get("lineage") if isinstance(snap.get("lineage"), dict) else {}
    origins = lineage.get("sources") or []
    for name, target in zip(columns, lineage.get("columns") or []):
        if not isinstance(target, dict) or not isinstance(target.get("source"), int) \
                or not 0 <= target["source"] < len(origins):
            continue
        origin = origins[target["source"]] if isinstance(origins[target["source"]], dict) else {}
        got = inputs.get(str(origin.get("input")))
        column = str(target.get("column") or "").lower()
        if got is not None and column in got.hidden:
            hidden.add(name)
        if got is not None and column in got.sure:
            sure.add(name)
    if any(got.base == {_EVERY} and not got.hidden for got in inputs.values()):
        # 读不出来、又不知道有哪几列的输入：认不出哪个结果列来自它
        hidden |= set(columns)
    view = {**snap, "source": None}        # 合并 SQL 照普通查询认（_derived 不看合并查询的 SQL）
    tables = {alias.lower(): [str(c).lower() for c in item.get("columns") or []]
              for alias, item in by_alias.items() if isinstance(item.get("columns"), list)}
    names = set(base).union(*(got.hidden for got in inputs.values())) if inputs else set(base)
    hidden |= _derived(view, names, "sqlite", tables or None) & set(columns)
    certain = set(base).union(*(got.sure for got in inputs.values())) if inputs else set(base)
    labels: dict[str, set[str]] = {}
    if certain:
        found, labels = _sure(view, certain, "sqlite", tables or None)
        sure |= found
    sure &= set(columns) | base
    every = set(base).union(*(got.base for got in inputs.values() if got.base != {_EVERY})) if inputs else set(base)
    return _MaskSets(hidden | sure, sure, every, labels, list(dict.fromkeys([*gone, *(n for got in inputs.values()
                                                                                       for n in got.gone)])))


#: 合并的一个输入读不出来、又不知道有哪几列时，_merge_input_sets 交回的 base 记成它：认不出哪个结果列来自它
_EVERY = "\x00*"


async def _merge_input_sets(item: dict[str, Any], masks: _Masks, depth: int, recorded: bool) -> _MaskSets:
    """合并的一个输入（快照 inputs 里的一项）的遮罩：读得出输入快照的按 _mask_sets 认（SQL 里给遮罩列起了别名的也算，
    输入本身是合并结果的再往下追）。

    读不出来、追得太深的，遮没遮核对不了：它的数据源现在设了遮罩、已经不在了、本身是合并结果、或者合并当时记下过遮罩
    （recorded，不知道是哪个输入的）时，它的每一列都当遮罩列（hidden），确定的只认它的数据源现在设的列名；连有哪几列
    都不知道的，base 记成 _EVERY。别的（数据源还在、没设遮罩，合并时也没记）照旧没有遮罩。"""
    source = item.get("source")
    artifact = str(item.get("artifact") or "")
    content, ok = masks.fetch(artifact) if artifact and depth < MERGE_DEPTH else (None, None)
    if ok is True and isinstance(content, dict):
        return await _mask_sets(content, source, masks, depth=depth + 1, artifact=artifact)
    current, found = await masks.of(source) if source and source != MERGE_SOURCE else (set(), True)
    if not (current or recorded or not found or source == MERGE_SOURCE or not source):
        return _MaskSets(set(), set(), set())
    columns = item.get("columns")
    if not isinstance(columns, list):
        return _MaskSets(set(), set(current), {_EVERY})
    return _MaskSets(set(current) | {str(c).lower() for c in columns}, set(current), set(current),
                     gone=[] if found or not source else [str(source)])


def _leads_to_upload(snap: dict[str, Any], cells: list[tuple[Any, Any]], depth: int = 0) -> bool:
    """被引用的格经合并追下去，最后落在上传表格的查询快照上（有 data_version）：界面才去请求推断来源。"""
    if depth >= MERGE_DEPTH:
        return False
    for row, column in cells:
        hit = trace_cell(snap, row, column)
        if hit is None or not hit.artifact:
            continue
        try:
            source = load(hit.artifact)
        except ValueError:
            continue
        if not isinstance(source, dict):
            continue
        if source.get("source") == MERGE_SOURCE:
            if _leads_to_upload(source, [(hit.row, hit.column)], depth + 1):
                return True
        elif source.get("data_version"):
            return True
    return False


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


# --------------------------------------------------------------------------
# GET /api/runs/{run_id}/report.html（报告导出：自包含的 HTML，数字点得开口径、来源、计算方式）
# --------------------------------------------------------------------------


def _export_cite_key(seg: dict[str, Any]) -> tuple | None:
    """数字 / 取值片段的引用键：同一处引用（同一个指标、同一格、同样的换算）共用一条出处链。
    别的片段（表名、引文）的链跟着片段走，不归并。"""
    if seg.get("kind") not in ("number", "value"):
        return None
    cite = seg.get("cite")
    if not isinstance(cite, dict):
        return None
    loc = cite.get("locator") if isinstance(cite.get("locator"), dict) else {}
    return (cite.get("kind"), cite.get("alias"), cite.get("status"), cite.get("conv"),
            loc.get("metric"), loc.get("row"), loc.get("column"), loc.get("field"), seg.get("issue"))


async def _export_withheld(sealed: _Sealed, doc: dict[str, Any]) -> frozenset[str]:
    """报告导出用：正文引用了、证据目录里却还带着原值的取自遮罩列的指标（目录里的别名）。升级前的报告撰写节点写的老文档
    （或者写完之后数据源才设的遮罩）才有；现在写的，这类指标在目录里已经是「已遮罩」。和可验证报告认老文档同一个判断
    （certain_masks + bundle/build.py 的 withheld_metric_aliases：只按确定取自遮罩列的认，值碰巧等于遮罩值的普通指标、SQL
    认不准时的普通金额照给）。导出的文件不带证据目录，只看正文引用了的那几个；一个都没有的不去认遮罩。"""
    from app.bundle.build import withheld_metric_aliases
    from app.core.artifact_store import read_bytes

    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    cited = {str(seg["cite"]["alias"]) for _, unit in iter_units(doc) for seg in unit.get("segments") or []
             if isinstance(seg, dict) and isinstance(seg.get("cite"), dict) and seg["cite"].get("kind") == "metric"
             and seg["cite"].get("alias")}
    entries = {alias: catalog[alias] for alias in sorted(cited) if isinstance(catalog.get(alias), dict)
               and catalog[alias].get("kind") == "metric" and not catalog[alias].get("masked")
               and catalog[alias].get("value") is not None}
    if not entries:
        return frozenset()
    masks = await _mask_fingerprint()
    sure, secrets = await certain_masks(sealed, _Masks(sealed))
    # 读工件、解析口径卡是同步的活，不该占着事件循环（同可验证报告）
    return await asyncio.to_thread(lambda: withheld_metric_aliases(
        {"catalog": entries}, read=lambda artifact: read_bytes(artifact, verify=False), events=sealed.events,
        source_masks=masks, sure_columns=sure, sure_secrets=secrets))


def _withhold_in_doc(doc: dict[str, Any], aliases: frozenset[str]) -> dict[str, Any]:
    """老文档里值不该给的那几个指标照现在的报告撰写节点的写法改写，交回副本（原文档不动）：目录条目的值、渲染写「已遮罩」、
    记 masked（engine/evidence.withhold_metrics），引用它们的片段写「已遮罩」。出处链（_metric_chain）据此按目录里的那个字
    比渲染、值也写「已遮罩」，正文和点开的面板里都不出现原值。"""
    out = copy.deepcopy(doc)
    out["catalog"] = withhold_metrics(out.get("catalog") or {}, sorted(aliases), MASKED)
    for _, unit in iter_units(out):
        for seg in unit.get("segments") or []:
            cite = seg.get("cite") if isinstance(seg, dict) else None
            if isinstance(cite, dict) and cite.get("kind") == "metric" and cite.get("alias") in aliases:
                seg["text"] = MASKED
                for key in ("value", "rendered"):
                    if key in cite:
                        cite[key] = MASKED
    return out


@router.get("/{run_id}/report.html")
async def export_report_html(run_id: str, report: str | None = None) -> Response:
    """把一次运行的报告导出成一个自包含的 HTML 文件（app/report_export）。

    正文由服务端按报告文档渲染（不执行脚本也读得到）；带出处链的数据嵌在页面里，执行脚本后每个数字、
    无证据的数字、可疑的表名字段名都点得开——面板是网页上证据面板同一个拼法（_chain）：口径卡、口径与
    版本、原式、代入式、输入、复算、SQL、被引用的行。封存状态逐项标明；没封存的运行照常导出（和网页上
    读报告一个道理）。不追加运行事件、不改运行记录，只记一条导出审计（report.exported，next-round 02 做法 9：召回要找得到
    谁导出过这份报告）。口径账本比对过的运行，面板最上面多「口径」一步（同一份标签，写明封存后比对、不在封存范围内）。

    一次运行里有好几份报告时用 ?report=<节点 id> 指定；不指定取成果里标注的那一份（和片段接口同一个选法）。
    选报告、错误码和片段接口相同（run_not_found、evidence_report_not_found、evidence_doc_tampered、
    evidence_doc_missing）。
    """
    from datetime import datetime, timezone

    from app.report_export.build import (
        attachment_name, build_report, content_disposition, render_html, segment_state,
    )

    sealed = await _sealed(run_id)
    reports = _named_reports(sealed, report)
    if not reports:
        raise CodedHTTPException(404, "本次运行没有报告文档，没有可导出的报告", REPORT_NOT_FOUND)
    chosen = _choose(reports, report, sealed)
    if chosen.hash_ok is False:
        raise CodedHTTPException(409, "报告文档与哈希不一致，疑似被修改，不能导出", DOC_TAMPERED)
    if chosen.doc is None:
        raise CodedHTTPException(404, "无法读取报告文档", DOC_MISSING)
    doc = chosen.doc
    # 升级前写的老文档：证据目录里还带着取自遮罩列的指标的原值，正文引用了的话正文里也是。可验证报告把这样的文档整份只给
    # 哈希；这份文件同样会拿给系统外的人，按同一个判断把引用了它们的片段照现在的写法换成「已遮罩」
    if withheld := await _export_withheld(sealed, doc):
        doc = _withhold_in_doc(doc, withheld)
    nodes: dict[str, dict[str, str]] = {}
    for item in sealed.graph.get("nodes") or []:
        if isinstance(item, dict) and item.get("id"):
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            nodes[str(item["id"])] = {"label": str(data.get("label") or item["id"]),
                                      "type": str(item.get("type") or "")}
    # 每个有状态的片段一条出处链（和网页上点开的同一个 _chain）；同样的引用共用一条，别把整份快照抄几十遍
    masks = _Masks()
    chains: dict[str, list[dict[str, Any]]] = {}
    refs: dict[str, str] = {}
    notes: dict[str, str] = {}
    memo: dict[tuple, str] = {}
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if not isinstance(seg, dict) or not seg.get("id") or segment_state(seg) is None:
                continue
            sid = str(seg["id"])
            if note := _note(seg, unit):
                notes[sid] = note
            key: tuple | None = _export_cite_key(seg)
            if key is not None and key in memo:
                refs[sid] = memo[key]
                continue
            chain = await _chain(seg, doc, sealed, chosen.node_id)
            ck = key if key is not None else (sid,)
            chains[str(ck)] = chain
            refs[sid] = str(ck)
            if key is not None:
                memo[key] = str(ck)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:      # 两次读之间被删掉了
            raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
        info = {"id": run.id, "workflow_id": run.workflow_id, "workflow_name": run.workflow_name,
                "run_class": run.run_class, "status": run.status, "end_reason": run.end_reason,
                "started_at": run.started_at, "finished_at": run.finished_at}
    from app.ledger.view import report_labels

    ledger = await report_labels(sealed, chosen.node_id)
    data = build_report(info, report_node=chosen.node_id, doc_artifact=chosen.doc_artifact,
                        doc_hash_ok=chosen.hash_ok, doc=doc, seal=sealed.seal, chains=chains, refs=refs,
                        notes=notes, nodes=nodes, exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ledger=ledger)
    page = render_html(data, doc)
    await record_export(run_id, "html", report_node=chosen.node_id, doc_artifact=chosen.doc_artifact)
    return Response(page, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": content_disposition(attachment_name(info), run_id=run_id),
        # 导出的是这一刻的报告和出处，不该被哪一层缓存下来、换给下一个请求
        "Cache-Control": "no-store",
    })


async def record_export(run_id: str, fmt: str, **data: Any) -> None:
    """导出了一份报告（报告导出 html、可验证报告 bundle）：记一条审计 report.exported（subject 是这次运行），谁导出、导出的
    哪份报告文档。召回（口径升版后追回已交付的答案）据此找得到导出人。审计写不进去只记日志，不挡下载。"""
    from app.auth.audit import record

    try:
        async with SessionLocal() as session:
            await record(session, "report.exported", "run", run_id,
                         data={"format": fmt, **{k: v for k, v in data.items() if v is not None}})
            await session.commit()
    except Exception:  # noqa: BLE001 - 留痕失败不挡下载，但要看得见
        logger.exception("运行 %s 的报告导出审计没有写进去", run_id)


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
    # 合并查询结果里的格：先按逐格来历追到输入的那一格，再对那一格照常判断；追不到就只有表级来历
    if (cite.get("kind") == "cell" and cite.get("status") == "resolved" and seg.get("state") == "deterministic"
            and entry.get("kind") == "query" and entry.get("source") == MERGE_SOURCE):
        return await _merge_provenance(report, seg, entry, sealed, masks=masks, base=base)
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


async def _merge_provenance(report: _Report, seg: dict[str, Any], entry: dict[str, Any], sealed: _Sealed, *,
                            masks: _Masks, base: dict[str, Any], hops: tuple[MergeHop, ...] = ()) -> ProvenanceOut:
    """合并结果里的一格：沿合并快照记下的逐格来历（engine/merge_query.py）追到输入的那一格，换成对那一格的判断。

    宁可不下钻，也不下钻错：追不到（计算、聚合出来的列，合并 SQL 超出可追溯的写法，一模一样的两行）就判
    merge_no_lineage，只有表级来历（合并的输入、合并 SQL 在查询步骤的 merge 一节里）。追到的输入是数据源查询时，
    结论就是直接引用那一格时的结论（上传表格照常推断原表格子，手工源 not_upload）；输入本身也是合并结果时再追一跳。
    cell 始终是报告引用的那一格，merge 列出经过的每一跳。
    """
    def out(status: str, reason: Reason, *more: MergeHop) -> ProvenanceOut:
        return ProvenanceOut(status=status, reason=reason, merge=[*hops, *more], **base)  # type: ignore[arg-type]

    cite = seg.get("cite") or {}
    artifact = str(entry.get("artifact") or "")
    snap, ok = await _Artifacts().get(artifact) if artifact in sealed.queries else (None, None)
    if ok is not True or not isinstance(snap, dict):
        return out("none", refuse("not_sealed"))
    locator = cite.get("locator") or {}
    row, column = locator.get("row"), locator.get("column")
    if isinstance(column, str) and column.lower() in _recorded_masks(snap):
        return out("none", refuse("masked"))
    hit = trace_cell(snap, row, column)
    catalog = (report.doc or {}).get("catalog") or {}
    alias = _query_alias(catalog, hit.artifact) if hit is not None else None
    hop = MergeHop(alias=str(cite.get("alias") or ""), node_id=entry.get("node_id"),
                   input=hit.alias if hit else None, query=alias, row=hit.row if hit else None,
                   column=hit.column if hit else None)
    if hit is None or len(hops) >= MERGE_DEPTH:
        return out("none", refuse("merge_no_lineage"), replace(hop, input=None, query=None, row=None, column=None))
    if alias is None:
        # 输入不在这份报告的目录里（不在报告的上游、没有记进台账）：认不出它是封存过的查询
        return out("none", refuse("not_sealed"), hop)
    sub_entry = catalog[alias]
    sub_seg = {**seg, "cite": {**cite, "alias": alias, "locator": {"row": hit.row, "column": hit.column}}}
    if sub_entry.get("source") == MERGE_SOURCE:
        return await _merge_provenance(report, sub_seg, sub_entry, sealed, masks=masks, base=base, hops=(*hops, hop))
    found = await segment_provenance(report, sub_seg, sealed, masks=masks)
    return replace(found, cell=base["cell"], segment=base["segment"], merge=[*hops, hop])


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
#: 正式运行、报告没开结论句裁判（结论句策略是不裁判或只要求附依据，升级前的运行也一样）：节点里没判过，照实说
FORMAL_NO_JUDGE = "这份报告没有开启结论句裁判，正式运行的结论句不能按需裁判"
#: 正式运行、开了裁判节点里却没判：报告没通过核对、节点按设置紧接着判失败时，不再花钱裁判
FORMAL_UNJUDGED = "这份报告开启了结论句裁判，但报告未通过核对，结论句没有在报告撰写节点内完成裁判；正式运行的结论句不能按需裁判"
#: 按需裁判接口认不出是哪份报告（没有封存的报告、点名的不是报告撰写节点、文档读不出来）：不替它说判没判过
FORMAL_ONLY = "正式运行的结论句只在报告撰写节点内裁判，不能按需裁判"
#: 正式运行在节点里当场裁判过，可这句没判成（触顶、出错、格式不对、被截断，原因记在这句的判定里）
FORMAL_INLINE_MISSED = "这份报告在报告撰写节点内裁判时，这句没有判成（原因见判定）；正式运行的结论句不能按需裁判"
#: 同上，可文档里压根没有这句的判定（没有原因可看）
FORMAL_INLINE_NO_VERDICT = "这份报告在报告撰写节点内裁判过，但这句没有判定；正式运行的结论句不能按需裁判"
#: 同上，按需裁判接口一次点了几句、其中有没判成的
FORMAL_INLINE_MISSED_SOME = ("这份报告在报告撰写节点内裁判时，所选的句子有的没有判成（原因见各句的判定）；"
                             "正式运行的结论句不能按需裁判")
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


def _on_demand(sealed: _Sealed, doc: dict[str, Any], uid: str, verdict: dict[str, Any] | None,
               claims: str | None = None) -> dict[str, Any]:
    """这句能不能「请模型判断」：{available, reason, message}。和 POST …/evidence/judge 同一套条件。

    claims 是写这份报告时的结论句策略（_Report.claims）。不是结论句的先说：标题、连接性文字不论哪类运行都无需裁判。
    以前正式运行一律先答「已在节点内完成裁判」，标题、没开裁判的报告也这么说（_formal_message）。"""
    def no(reason: str, message: str) -> dict[str, Any]:
        return {"available": False, "reason": reason, "message": message}

    if not candidates(doc, [uid]):
        return no("not_a_claim", f"这句{NOT_A_CLAIM}")
    if sealed.run_class == "formal":
        return no("formal", _formal_message(doc, claims, [verdict]))
    if not sealed.ledger_on:
        return no("legacy", LEGACY_JUDGE)
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


def _formal_message(doc: dict[str, Any] | None, claims: str | None, verdicts: list[Any] | None = None) -> str:
    """正式运行为什么不能按需裁判，照实说节点里判没判过：文档里有当场裁判的摘要（mode inline）才是裁判过了，问到的句子
    （verdicts，这几句的判定）有没判成的（触顶、出错、被截断，判定是未裁判；没有判定）照实说没判成；开了裁判却没有摘要的，
    是报告没通过核对、节点紧接着判失败，不再花钱裁判；别的是没开结论句裁判（策略为不裁判、只要求附依据，升级前的运行
    没记策略，也没有裁判摘要）。没有文档（认不出是哪份报告）的不替它说判没判过。"""
    if doc is None:
        return FORMAL_ONLY
    judge = doc.get("judge")
    if isinstance(judge, dict) and judge.get("mode") == "inline":
        if verdicts and any(not (isinstance(v, dict) and v.get("status") in SETTLED) for v in verdicts):
            if len(verdicts) > 1:
                return FORMAL_INLINE_MISSED_SOME
            # 文档里压根没记这句的判定：不说「原因见判定」，没有判定可看
            return FORMAL_INLINE_MISSED if isinstance(verdicts[0], dict) else FORMAL_INLINE_NO_VERDICT
        return FORMAL_JUDGED
    return FORMAL_UNJUDGED if claims == CLAIMS_JUDGE else FORMAL_NO_JUDGE


def _formal_refusal(sealed: _Sealed, wanted: str | None, units: list[str] | None = None) -> str:
    """按需裁判接口拒绝正式运行时的那句话，和片段接口说的一致：按请求要的那份报告（没指定取成果里标注的那一份）、
    点的那几句结论句（不是结论句的不算）说。选不出报告、文档读不出来的，只说正式运行不能按需裁判（FORMAL_ONLY）。"""
    reports = _named_reports(sealed, wanted)
    try:
        chosen = _choose(reports, wanted, sealed) if reports else None
    except CodedHTTPException:
        chosen = None
    doc = chosen.doc if chosen else None
    verdicts = None
    if doc is not None and units:
        picked = {c.unit for c in candidates(doc, units)}
        verdicts = [u.get("verdict") for _, u in iter_units(doc) if str(u.get("id")) in picked]
    return _formal_message(doc, chosen.claims if chosen else None, verdicts)


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


async def _judge_loader(doc: dict[str, Any], catalog: dict[str, Any], units: list[str] | None = None,
                        loader: Callable[[str], Any] | None = None) -> Callable[[str], Any]:
    """交给结论句裁判的 loader（judge.prepare 的 loader）：摘录里的每份查询快照逐份遮，这份快照里确定取自遮罩列的列
    （_mask_sets 的 sure，和证据面板同一套认法：c.phone AS tel 的 tel、按位置认得出的 UPPER(phone)）也不给。

    裁判自己只按列名遮（快照记下的 mask_columns 加数据源现在设的，judge._hidden）：改了名字的号码列照样进摘录，裁判理由
    一引用就在证据面板、审计 CSV 里露出来。这里把那几列并进交给裁判的这一份快照的 mask_columns（只是交给 prepare 的
    副本，工件本身不动），裁判照它自己的办法遮掉。只并这份快照自己的：不按数据源名合并（另一份快照里同名、却取自别的列
    的照给），不用证据面板宁可多遮的 hidden（整份遮的快照、只数个数的列照给，被引用的金额、人数裁判要看得到才判得了）。
    这样摘录变了的句子复用键随之变，此前判过的会重判一次。

    只读要裁判的句子（units，None 是全部结论句，judge.candidates）挂的查询；读出来的快照另记一份交给 prepare 再取，
    不读第二遍。loader 缺省读工件库；按需裁判传只认封存工件的那个（_sealed_loader）。报告撰写节点当场裁判
    （engine/nodes/report.py）用同一个函数。"""
    from app.engine.evidence import _mask_names
    from app.engine.judge import candidates

    read = loader or load
    masks = _Masks()
    shown: dict[str, Any] = {}
    for alias in dict.fromkeys(a for c in candidates(doc, units) for a in c.aliases):
        entry = catalog.get(alias)
        if not isinstance(entry, dict) or entry.get("kind") != "query" or not entry.get("artifact"):
            continue
        artifact = str(entry["artifact"])
        if artifact in shown:
            continue
        try:
            snap = read(artifact)
        except Exception:  # noqa: BLE001 - 读不出来、不在封存范围里：摘录本来就不给它，交给 prepare 照它的办法处理
            continue
        if not isinstance(snap, dict):
            continue
        recorded = _mask_names(snap.get("mask_columns"))
        sure = set((await masks.sets(artifact, entry.get("source") or snap.get("source"), snap)).sure)
        shown[artifact] = {**snap, "mask_columns": sorted(recorded | sure)} if sure - recorded else snap

    def reader(artifact: str) -> Any:
        key = str(artifact)
        return shown[key] if key in shown else read(key)
    return reader


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
            raise CodedHTTPException(409, _formal_refusal(sealed, wanted, units), JUDGE_FORMAL)
        if not sealed.ledger_on:
            raise CodedHTTPException(409, LEGACY_JUDGE, JUDGE_LEGACY)
        if not sealed.sealed or sealed.status not in _TERMINAL:
            raise CodedHTTPException(409, UNSEALED_JUDGE, JUDGE_UNSEALED)
        if not sealed.trusted:
            raise CodedHTTPException(409, BROKEN_JUDGE, SEAL_BROKEN_CODE)
        reports = _named_reports(sealed, wanted)
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
        request = judging.prepare(doc, catalog, units=pending, masked=masked,
                                  loader=await _judge_loader(doc, catalog, pending, loader))
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
#: 落在已经列出的片段上也单列一行的违规：文档对不上的，和引用了遮罩列的格子的（masked_cell：引用的那一格落在的正是一个
#: 「有出处」的单元格片段，不单列的话审计表、导出的 CSV 里只有一行普通的「有出处」）
_LISTED_ALWAYS = _INTEGRITY | {"masked_cell"}
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
              "masked_cell": "引用了遮罩列",
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
        if not isinstance(v, dict) or (str(v.get("segment")) in listed and v.get("code") not in _LISTED_ALWAYS):
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

