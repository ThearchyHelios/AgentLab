"""文档草稿接上一次真实运行：定义来自文档，取数路径来自运行（docs/design/next-round/02-口径账本.md 做法 8 的另一半）。

**为什么必须由运行接上。** 文档里写的是业务说法，不是库里的表、列和 SQL（见 ledger/docdraft）。取数路径只能来自一次真的跑过、
结果对得上的运行：查了什么、结果长什么样、报告引用了哪一条，都在这次运行的封存范围里。所以接上之后工作流里的取数节点照抄
运行里那条查询（跨轮变化的字面量按 certify.build_graph 的办法变成入口参数），文档只贡献定义。

**为什么只并定义、不改式子。** 草稿不知道取数，改写 expression 或指标 id 会破坏已经证明过的算法。并进同名或同义词的指标，
expression、id、单位这些原样保留；对不上的指标名进 warnings，由人决定是新建一条还是忽略，而不是让整次接入失败。

**为什么要记文档引用。** 定义的事实来源是文档里那几句原文。origin.documents 按指标定义里的引用汇总去重，记下 document_id、
chunk_id、ordinal 和逐字 quote：之后复核、追责都能落回具体段落，不必再相信「草稿就是这么写的」。ordinal 从库里补（草稿的
引用只带 chunk，段落序号只有 chunks 表知道），chunk 已被删掉时照抄引用里的值，读不到就留空。

**为什么这一路不发布、不发起运行、不写审计。** 接入只产出一份可试跑的草稿：发布是人的决定，发起运行是另一个动作。
它和 freeze 的区别只在定义从哪来、以及 origin 里多记了 draft_id 和 documents。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.db.models import Run

#: 接不上时的说法（给人看）
NOT_FOUND = "运行记录不存在，或没有权限读这次运行"
NOT_SUCCEEDED = "这次运行没有成功结束，不能接上文档草稿"
NOT_MATCHED = "这次运行还没有比对口径，比对完才能把文档草稿接上取数"
NO_QUERIES = "这次运行的报告没有引用任何查询结果，没有可接上的取数"
SNAPSHOT = "查询快照读不出来（文件缺失或与哈希不一致），不能接上文档草稿"
UNPARSED = "查询无法规范化（{reason}），不能接上文档草稿"
NOT_MERGED = "这次运行没有单行结果的数值列，草稿里的定义没有可并入的口径卡"
DESCRIPTION = ("从业务文档起草的口径：定义来自文档草稿，{n} 个指标，取数路径接自运行 {run_id}；"
               "经人工审核后再发布")


class AttachRefused(Exception):
    """接不上。status 是给接口用的 HTTP 状态（现在都是 409），message 给人看。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def status_code(self) -> int:
        return self.status


def _citation_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """草稿各指标的定义引用，按 (document_id, chunk, quote) 去重，先后照草稿。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    metrics = payload.get("metrics")
    for metric in metrics if isinstance(metrics, list) else []:
        if not isinstance(metric, Mapping):
            continue
        definition = metric.get("definition")
        citations = definition.get("citations") if isinstance(definition, Mapping) else None
        if not isinstance(citations, list):
            citations = metric.get("citations")
        for citation in citations if isinstance(citations, list) else []:
            if not isinstance(citation, Mapping):
                continue
            chunk_id = citation.get("chunk") or citation.get("chunk_id") or citation.get("chunkId")
            quote = citation.get("quote")
            if not isinstance(chunk_id, str) or not chunk_id:
                continue
            if not isinstance(quote, str) or not quote.strip():
                continue
            document_id = str(citation.get("document_id") or "")
            key = (document_id, chunk_id, quote)
            if key in seen:
                continue
            seen.add(key)
            ordinal = citation.get("ordinal")
            rows.append({"document_id": document_id, "chunk_id": chunk_id,
                         "ordinal": ordinal if isinstance(ordinal, int) else None, "quote": quote})
    return rows


async def _documents(session: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """引用补上库里记的 document_id / ordinal（见模块说明）；库里查不到的照抄引用里的值。"""
    from app.db.models import Chunk

    ids = list(dict.fromkeys(row["chunk_id"] for row in rows))
    found: dict[str, tuple[str, int]] = {}
    if ids:
        for chunk_id, document_id, ordinal in (await session.execute(
                select(Chunk.id, Chunk.document_id, Chunk.ordinal).where(Chunk.id.in_(ids)))).all():
            found[str(chunk_id)] = (str(document_id), int(ordinal))
    for row in rows:
        if row["chunk_id"] in found:
            row["document_id"], row["ordinal"] = found[row["chunk_id"]]
    return rows


async def attach(session: Any, *, payload: dict, name: str, run_id: str, draft_id: str = "",
                 principal: Any = None) -> dict:
    """把文档草稿接上一次真实运行，落库成新的草稿工作流。

    返回 {workflow_id, version, metrics, warnings}：metrics 是口径卡上并完定义之后的指标（id / expression 没动），warnings 是
    合并时对不上的那些指标（原样交给界面）。不能接时抛 AttachRefused（409）。payload 的形状是草稿接口交出来的那一份：
    {question?, metrics: [docdraft 的指标对象]}。不发布、不发起运行、不写审计。
    """
    from app.api.deps import can_read_run, current_principal, ensure_role
    from app.api.evidence import _sealed
    from app.core.artifact_store import graph_hash, load
    from app.db.models import DataSource, Workflow, WorkflowVersion
    from app.ledger import certify, docdraft, store

    ensure_role("builder")
    principal = principal if principal is not None else current_principal()
    run = await session.get(Run, run_id)
    if run is None or not await can_read_run(principal, run):
        raise AttachRefused(409, NOT_FOUND)
    if run.status != "succeeded":
        raise AttachRefused(409, NOT_SUCCEEDED)
    recorded = await store.latest(run_id)
    if recorded is None or recorded.content is None:
        raise AttachRefused(409, NOT_MATCHED)
    queries = certify.referenced_queries(recorded.content)
    if not queries:
        raise AttachRefused(409, NO_QUERIES)
    kinds = {n: k for n, k in (await session.execute(select(DataSource.name, DataSource.kind))).all()}

    # 封存的查询快照（和 freeze 同一个读法）：比对工件只认封存范围内的查询，这里再按形状解析一次
    await _sealed(run_id)
    shapes: list[certify.Shape] = []
    snaps: list[dict[str, Any]] = []
    for artifact in queries:
        try:
            snap = load(artifact)
        except ValueError:
            raise AttachRefused(409, SNAPSHOT) from None
        shape = certify.shape_of(snap, kinds)
        if shape is None:
            raise AttachRefused(409, SNAPSHOT)
        if shape.problem:
            raise AttachRefused(409, UNPARSED.format(reason=shape.problem))
        shapes.append(shape)
        snaps.append(snap)

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        question = name
    graph, _fields, _metrics, _multi = await certify.build_graph(
        shapes=shapes, snaps=snaps, kinds=kinds,
        labels=lambda snap, source: certify._labels(session, snap, source),
        name=name, question=question)

    # 定义并进同名 / 同义词的指标；表达式与 id 不动（见模块说明）
    drafts = payload.get("metrics")
    drafts = drafts if isinstance(drafts, list) else []
    card = next((n for n in graph.get("nodes") or []
                 if isinstance(n, dict) and n.get("type") == "metrics"), None)
    merged: list[dict[str, Any]] = []
    warnings: list[str] = []
    if card is None:
        warnings.append(NOT_MERGED)
    else:
        config = card.setdefault("data", {}).setdefault("config", {})
        card_metrics = config.get("metrics")
        merged, warnings = docdraft.merge_definition(
            card_metrics if isinstance(card_metrics, list) else [], drafts)
        config["metrics"] = merged
        if name.strip():
            config["caliber"] = name.strip()[:40]

    documents = await _documents(session, _citation_rows(payload))
    workflow = Workflow(
        name=(name.strip() or "文档口径草稿")[:200],
        description=DESCRIPTION.format(n=len(merged), run_id=run_id),
        graph=graph, status="draft", tags=["doc_draft"],
        origin={"run_id": run_id, "freeze": True, "sources": [{"run_id": run_id}],
                "draft_id": draft_id, "documents": documents})
    session.add(workflow)
    await session.flush()
    session.add(WorkflowVersion(workflow_id=workflow.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                                note=f"从文档草稿接上运行 {run_id}"))
    await session.commit()
    return {"workflow_id": workflow.id, "version": 1, "metrics": merged, "warnings": warnings}


__all__ = ["AttachRefused", "attach"]
