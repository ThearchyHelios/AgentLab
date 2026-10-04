"""可验证报告的构建：把一次已封存运行的报告、证据和运行记录打成一个自包含的 HTML 文件。

拿到文件的人不连服务、不装任何东西，在浏览器里就能核对（verifier.js）：

- 事件：封存范围内（seq ≤ manifest_seq）的每一条都带 {seq, type, node_id, data_sha256}，data_sha256 是
  sha256(canonical_json(data))，和 runner 封存时算 manifest_hash 用的是同一个算法，浏览器按同样的拼法重算
  清单哈希、和封存哈希比。白名单里的事件另带服务端算好的规范字符串 canonical（逐字等于 canonical_json(data)），
  浏览器先核它的哈希，再从里面读出报告文档、口径卡、查询快照的 id。其余事件只给哈希：工具返回、节点产出的
  预览里可能有遮罩列的原值和提示词。
- 工件：每份报告文档（各报告节点最后一次自查记下的 doc_artifact）、它引用到的口径卡和查询快照，按原字节
  base64 嵌入，浏览器重算 sha256 和 id 比；带遮罩的快照不嵌内容、只给 id。没被报告引用的工件不嵌。
- 封存之后追加的事件（按需裁判、审批质询这类批注）单列：类型、序号、内容摘要，不进清单。

它只证明「内部一致」：数字、文档、运行记录彼此对得上，而且没被改过。真伪要拿页面上的封存哈希和系统里这次
运行的封存核对（GET /api/runs/{id}/verify）对照，页面上醒目地写着这一点。

主体是纯函数：不碰数据库，工件经 read 取原字节，数据源现在设的遮罩由调用方查好传进来（source_masks）。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.artifact_store import canonical_json, content_hash
from app.core.events import EventType
from app.engine.evidence import iter_units
from app.engine.merge_query import MERGE_SOURCE
from app.engine.schema import NodeType

SCHEMA = "agentlab.bundle/1"

#: 不论哪个节点都带规范字符串的事件：运行开始（实际生效的运行参数）、报告自查（报告文档的 id）、
#: 口径卡升版处置、出具判定（档位）
_SHOWN_EVENTS = frozenset({EventType.RUN_STARTED, EventType.REPORT_CHECKED, EventType.CALIBER_UPGRADE,
                           EventType.ISSUANCE})
#: node.finished 带规范字符串的节点：报告（预览就是嵌进来的那份文档的正文）、口径卡（指标集的 id 记在台账里）
_SHOWN_NODES = frozenset({NodeType.REPORT, NodeType.METRICS})
#: 目录条目的种类 → 嵌进来的工件种类。运行输入、检索、表和字段的条目不嵌：前者没有工件，后几样不在核对范围里
_CITED_KINDS = {"metric": "metric_set", "query": "query"}

#: 没挂在工作流上的运行（画布、问数据当场搭的）：老数据叫「临时图」，新的叫「未保存的工作流」，也可能是空的。
#: 和前端 lib/terms.ts 的 runName 同一个规则
UNSAVED_NAME = "未保存的工作流"
_UNSAVED_NAMES = frozenset({"", "临时图", UNSAVED_NAME})
FILE_SUFFIX = "-可验证报告.html"
#: 文件名里不能出现的字（Windows 最严，按它来）和控制字符
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NAME_MAX = 80

Reader = Callable[[str], "bytes | None"]


# --------------------------------------------------------------------------
# 工件：原字节、解析、遮罩
# --------------------------------------------------------------------------


class _Store:
    """这一次构建里读过的工件：原字节、解析出来的内容、查询快照带不带遮罩。每份只读一次盘。"""

    def __init__(self, read: Reader, source_masks: Mapping[str, Any]) -> None:
        self._read = read
        self._source_masks = source_masks
        self._raw: dict[str, bytes | None] = {}
        self._json: dict[str, Any] = {}
        self._masked: dict[str, bool] = {}

    def raw(self, artifact: str) -> bytes | None:
        if artifact not in self._raw:
            try:
                data = self._read(artifact)
            except (OSError, ValueError):
                data = None
            self._raw[artifact] = data if isinstance(data, (bytes, bytearray)) else None
        return self._raw[artifact]

    def content(self, artifact: str) -> Any:
        """解析出来的内容，读不出来、不是 JSON 的是 None。不看哈希：被改过的快照也要知道它记的遮罩。"""
        if artifact not in self._json:
            raw = self.raw(artifact)
            try:
                self._json[artifact] = json.loads(raw) if raw is not None else None
            except ValueError:
                self._json[artifact] = None
        return self._json[artifact]

    def intact(self, artifact: str) -> bool:
        raw = self.raw(artifact)
        return raw is not None and content_hash(bytes(raw)) == artifact

    def masked(self, artifact: str) -> bool:
        """这份查询快照算不算带遮罩：查询当时记下了遮罩列（mask_columns），或者它的数据源现在设了遮罩
        （合并查询看它合并的那几个数据源）。比证据面板粗：面板只遮那几列，这里整份不随文件附带——文件会
        离开系统，遮罩只减少暴露，宁可多挡。读不出来的快照不知道带没带遮罩，按带遮罩算。"""
        if artifact not in self._masked:
            self._masked[artifact] = _snapshot_masked(self.content(artifact), self._source_masks)
        return self._masked[artifact]


def _snapshot_masked(content: Any, source_masks: Mapping[str, Any]) -> bool:
    if not isinstance(content, dict):
        return True
    recorded = content.get("mask_columns")
    if isinstance(recorded, list) and any(isinstance(c, str) and c.strip() for c in recorded):
        return True
    sources = content.get("sources") if content.get("source") == MERGE_SOURCE else [content.get("source")]
    return any(source_masks.get(str(name or "")) for name in sources or [])


def _query_artifacts(events: Iterable[tuple[int, str, str | None, Any]]) -> list[str]:
    """封存范围内交回过的查询快照：tool.end / merge.end 的 query_artifact，节点台账里的 query 条目。"""
    out: list[str] = []
    for _, etype, _, data in events:
        if not isinstance(data, dict):
            continue
        if etype in (EventType.TOOL_END, EventType.MERGE_END) and isinstance(data.get("query_artifact"), str):
            out.append(data["query_artifact"])
        elif etype == EventType.NODE_FINISHED:
            out += [str(e["artifact"]) for e in data.get("evidence") or []
                    if isinstance(e, dict) and e.get("kind") == "query" and e.get("artifact")]
    return list(dict.fromkeys(a for a in out if a))


# --------------------------------------------------------------------------
# 报告文档和它引用到的工件
# --------------------------------------------------------------------------


def _report_events(events: Iterable[tuple[int, str, str | None, Any]]) -> list[dict[str, Any]]:
    """每个报告节点最后一次自查记下的文档，按第一次出现的顺序。和证据接口（api/evidence._reports）同一个认法：
    报告重写过的，认最后一次。"""
    latest: dict[str, dict[str, Any]] = {}
    for seq, etype, node_id, data in events:
        if etype == EventType.REPORT_CHECKED and node_id and isinstance(data, dict) and data.get("doc_artifact"):
            latest[node_id] = {"node_id": node_id, "doc_artifact": str(data["doc_artifact"]), "seq": seq}
    return list(latest.values())


def _cited_artifacts(doc: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """报告正文引用到的口径卡和查询快照：(工件 id, 种类, 来源节点)，按正文顺序、去重。

    看每个解析成功的引用（行内的数字、单元格，句末的 [[see:]]）在文档目录里指向的条目：指标条目指向口径卡，
    查询条目指向快照。整表在组装时已经展开成每格一个单元格引用，不用另看。"""
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    out: list[tuple[str, str, str | None]] = []
    for _, unit in iter_units(doc):
        if not isinstance(unit, dict):
            continue
        cites = [s.get("cite") for s in unit.get("segments") or [] if isinstance(s, dict)]
        for cite in [*cites, *(unit.get("see") or [])]:
            if not isinstance(cite, dict) or cite.get("status") != "resolved":
                continue
            entry = catalog.get(cite.get("alias"))
            kind = _CITED_KINDS.get(entry.get("kind")) if isinstance(entry, dict) else None
            if kind and isinstance(entry.get("artifact"), str) and entry["artifact"]:
                owner = entry.get("node_id")
                out.append((entry["artifact"], kind, str(owner) if owner else None))
    return list(dict.fromkeys(out))


class _Artifacts:
    """嵌进页面的工件清单，按先后：报告文档在前，接着是它引用到的。同一份只列一次。"""

    def __init__(self, store: _Store) -> None:
        self.store = store
        self.items: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def add(self, artifact: str, kind: str, node_id: str | None) -> None:
        if artifact in self._seen:
            return
        self._seen.add(artifact)
        item: dict[str, Any] = {"id": artifact, "kind": kind}
        if node_id:
            item["node_id"] = node_id
        raw = self.store.raw(artifact)
        if raw is None:
            # 服务端的文件不在了：只给 id，浏览器照实说核对不了
            item["missing"] = True
        elif kind == "query" and self.store.masked(artifact):
            item["masked"] = True
        else:
            # 原样嵌入，不在这里复验哈希：被改过的文件照实交出去，浏览器自己算出对不上
            item["b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        self.items.append(item)


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------


def _shows_content(etype: str, node_id: str | None, data: Any, node_types: Mapping[str, str], store: _Store,
                   masked_run: bool) -> bool:
    """这条封存范围内的事件带不带规范字符串（白名单）。"""
    if etype in _SHOWN_EVENTS:
        return True
    if etype == EventType.RUN_FINISHED:
        # 运行结束事件里是成果：字段可以直接引用查询结果，运行里有带遮罩的快照时只给哈希
        return not masked_run
    if etype == EventType.NODE_FINISHED:
        kind = node_types.get(node_id or "")
        return kind in _SHOWN_NODES or (kind == NodeType.OUTPUT and not masked_run)
    if etype == EventType.TOOL_END:
        # 查询类：把查询快照的 id 和运行记录连起来。预览里是查询结果的前一截，数据源带遮罩的只给哈希
        artifact = data.get("query_artifact") if isinstance(data, dict) else None
        return isinstance(artifact, str) and bool(artifact) and not store.masked(artifact)
    return False


def _clip(text: Any, limit: int = 60) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _summary(etype: str, data: Any) -> str:
    """封存之后追加的事件说一句是什么。只挑不会带出数据的字段：判了几句、问的是什么、日志那一句。"""
    if not isinstance(data, dict):
        return ""
    if etype == EventType.EVIDENCE_JUDGED:
        verdicts = data.get("verdicts")
        count = len(verdicts) if isinstance(verdicts, dict) else 0
        model = data.get("model")
        return f"按需裁判 {count} 句结论" + (f"（模型 {_clip(model, 40)}）" if isinstance(model, str) and model else "")
    if etype == EventType.APPROVAL_INQUIRY:
        question = data.get("question")
        return f"质询：{_clip(question)}" if isinstance(question, str) and question.strip() else "审批质询"
    if etype == EventType.LOG and isinstance(data.get("message"), str):
        return _clip(data["message"])
    return ""


def _tier(events: Sequence[tuple[int, str, str | None, Any]]) -> dict[str, Any] | None:
    """出具档位：封存范围内最后一条出具判定事件里的 tier，带上序号（浏览器拿那条已核对的事件对照）。"""
    for seq, etype, _, data in reversed(events):
        if etype == EventType.ISSUANCE and isinstance(data, dict) and isinstance(data.get("tier"), str):
            return {"value": data["tier"], "seq": seq}
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------


def display_name(run: Mapping[str, Any]) -> str:
    """页面标题、附件名用的运行名：工作流名；没保存的工作流一律叫「未保存的工作流」。"""
    name = str(run.get("workflow_name") or "").strip()
    if not run.get("workflow_id") and name in _UNSAVED_NAMES:
        return UNSAVED_NAME
    return name or f"运行 {str(run.get('id') or '')[:8]}".strip()


def attachment_name(run: Mapping[str, Any]) -> str:
    """附件名「<运行名>-可验证报告.html」。文件名里不能有的字换成下划线。"""
    name = _BAD_FILENAME.sub("_", display_name(run)).strip(" .")[:_NAME_MAX].strip(" .")
    return f"{name or UNSAVED_NAME}{FILE_SUFFIX}"


def content_disposition(filename: str, *, run_id: str) -> str:
    """附件头：中文名按 RFC 5987 写在 filename* 里；filename 给不认 filename* 的老客户端一个纯 ASCII 的名字。"""
    ascii_id = re.sub(r"[^A-Za-z0-9]", "", run_id)[:8]
    return f"attachment; filename=\"agentlab-{ascii_id}-bundle.html\"; filename*=UTF-8''{quote(filename, safe='')}"


def build_bundle(
    run: Mapping[str, Any],
    events: Sequence[tuple[int, str, str | None, Any]],
    *,
    manifest_seq: int,
    manifest_hash: str,
    read: Reader,
    exported_at: str,
    legacy: bool = False,
    graph: Mapping[str, Any] | None = None,
    source_masks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次已封存运行 → 页面里的数据（bundle-data）。

    - run：运行记录的几样（id、workflow_id、workflow_name、version、version_hash、run_class、status、
      started_at、finished_at）
    - events：这次运行的全部事件 (seq, type, node_id, data)，按数据库里 seq 的顺序。和封存核对
      （runner.verify_manifest）读的是同一个顺序：seq 相同的几条，浏览器按这里的先后排，清单才算得一样
    - manifest_seq / manifest_hash：封存范围（老运行是按结束事件推算的那个，legacy 为真）和封存哈希
    - read：工件 id → 原字节（不复验哈希），没有就是 None
    - source_masks：数据源名 → 它现在设的遮罩列（只列设了的）
    """
    graph = graph if isinstance(graph, Mapping) else {}
    node_types: dict[str, str] = {}
    nodes: dict[str, dict[str, str]] = {}
    for item in graph.get("nodes") or []:
        if isinstance(item, Mapping) and item.get("id"):
            nid = str(item["id"])
            data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
            node_types[nid] = str(item.get("type") or "")
            nodes[nid] = {"label": str(data.get("label") or nid), "type": node_types[nid]}

    sealed = [e for e in events if e[0] <= manifest_seq]
    later = [e for e in events if e[0] > manifest_seq]
    store = _Store(read, source_masks or {})

    artifacts = _Artifacts(store)
    reports = _report_events(sealed)
    cited_queries: list[str] = []
    for report in reports:
        doc_id, node_id = report["doc_artifact"], report["node_id"]
        artifacts.add(doc_id, "report_doc", node_id)
        doc = store.content(doc_id) if store.intact(doc_id) else None
        # 文档要真是这次运行里这个节点写的：内容寻址只保证没被改，不保证没被张冠李戴。对不上的照实嵌进去让
        # 浏览器标出来，但它的目录不可信，不顺着它去找别的工件
        if not isinstance(doc, dict) or doc.get("run_id") != run.get("id") or doc.get("node_id") != node_id:
            continue
        for artifact, kind, owner in _cited_artifacts(doc):
            artifacts.add(artifact, kind, owner)
            if kind == "query":
                cited_queries.append(artifact)

    # 运行里有没有带遮罩的查询：有的话，成果节点和运行结束事件的预览可能直接带出查询结果，只给哈希
    masked_run = any(store.masked(a) for a in dict.fromkeys([*_query_artifacts(sealed), *cited_queries]))
    shown_events = []
    for seq, etype, node_id, data in sealed:
        text = canonical_json(data)
        item: dict[str, Any] = {"seq": seq, "type": etype, "node_id": node_id, "data_sha256": content_hash(text)}
        if _shows_content(etype, node_id, data, node_types, store, masked_run):
            item["canonical"] = text
        shown_events.append(item)

    return {
        "schema": SCHEMA,
        "meta": {
            "run_id": run.get("id"), "workflow_id": run.get("workflow_id"), "workflow_name": display_name(run),
            "version": run.get("version"), "version_hash": run.get("version_hash"),
            "run_class": run.get("run_class"), "status": run.get("status"),
            "started_at": _iso(run.get("started_at")), "finished_at": _iso(run.get("finished_at")),
            "manifest_seq": manifest_seq, "manifest_hash": manifest_hash, "legacy": bool(legacy),
            "tier": _tier(sealed), "exported_at": exported_at,
        },
        "nodes": nodes,
        "events": shown_events,
        "appended": [{"seq": seq, "type": etype, "node_id": node_id, "summary": _summary(etype, data)}
                     for seq, etype, node_id, data in later],
        "reports": reports,
        "artifacts": artifacts.items,
    }


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

#: 数据放在 <script type="application/json"> 里：< > & 换成转义，正文里的 </script>、<!-- 截不断数据块；
#: U+2028 / U+2029 在老的 JS 解析器里是换行，一并转义。这几个字只会出现在 JSON 的字符串里，转义后 JSON 不变
_JSON_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                               "\u2028": "\\u2028", "\u2029": "\\u2029"})
_PLACEHOLDER = re.compile(r"__BUNDLE_(TITLE|CSP|DATA|SCRIPT)__")


@lru_cache(maxsize=1)
def _assets() -> tuple[str, str]:
    here = Path(__file__).parent
    return (here / "template.html").read_text(encoding="utf-8"), (here / "verifier.js").read_text(encoding="utf-8")


def bundle_json(bundle: Mapping[str, Any]) -> str:
    """页面数据块里的 JSON 文本。"""
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).translate(_JSON_ESCAPES)


def render_html(bundle: Mapping[str, Any]) -> str:
    """把数据和核对脚本填进模板：样式、脚本都内联，零外链。

    内容安全策略按哈希只放行这一段核对脚本，默认什么都不许连：页面打开后不会、也不能向任何地方发请求。
    占位符一次替换完，填进去的内容不会再被当成占位符。"""
    template, script = _assets()
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    title = f"{(bundle.get('meta') or {}).get('workflow_name') or UNSAVED_NAME} · 可验证报告"
    values = {"TITLE": html.escape(title), "CSP": f"'sha256-{digest}'", "DATA": bundle_json(bundle),
              "SCRIPT": script}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
