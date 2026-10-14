from __future__ import annotations

import json
import re
from typing import Any

from app.core.events import EventType
from app.engine.context import NodeContext, NodeError
from app.engine.expressions import ExpressionError, eval_expression
from app.engine.schema import GraphNode, NodeType, _ancestors
from app.engine.state import GraphState, template_context


async def run_input(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """入口节点。把运行输入按声明的字段整理进变量池，补上默认值。"""
    raw = dict(state.get("input") or {})
    fields = ctx.cfg("fields", []) or []
    resolved: dict[str, Any] = dict(raw)

    for field in fields:
        name = field.get("name")
        if not name:
            continue
        if name not in resolved or resolved[name] in (None, ""):
            if field.get("required") and field.get("default") in (None, ""):
                raise NodeError(ctx.node.id, f"缺少必填输入：{name}")
            resolved[name] = field.get("default")

    return {
        "input": resolved,
        "vars": resolved,
        "nodes": {ctx.node.id: resolved},
    }


async def run_output(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """出口节点。按配置把散落在各节点的结果收成一个结构化成果。

    配了 contract 就升级成出具契约：核对指标齐不齐、叙述里的数字
    能不能回指到指标集，然后给出三档判定（formal / degraded / withheld）。
    判定和缺口清单一起印在成果上——降档不是悄悄地降。
    """
    mapping = ctx.cfg("fields", None)
    tctx = template_context(state)

    if mapping:
        result: dict[str, Any] = {}
        for field in mapping:
            name = field.get("name")
            if not name:
                continue
            result[name] = ctx.render(field.get("value", ""), state)
    else:
        # 没配映射就把最后一条消息当作结果，让新建的图开箱即用
        result = {"result": tctx.get("last_message", "")}

    reports = _report_fields(result, mapping, state, ctx)
    contract = ctx.cfg("contract")
    if contract:
        cells, governed = True, False
        if isinstance(contract, dict) and contract.get("report_from") not in (None, ""):
            from app.engine.governance import cells_allowed, governed_formal

            governed = await governed_formal(ctx.run.run_id)
            cells = cells_allowed(contract, governed=governed)
        result["_issuance"] = _apply_contract(contract, state, ctx, reports=reports, cells=cells, governed=governed,
                                              fields=result)
    evidence = _evidence_of(reports, contract)
    if evidence:
        result["_evidence"] = evidence

    return {"output": result, "nodes": {ctx.node.id: result}}


# --------------------------------------------------------------------------
# 成果字段和上游报告的对应
# --------------------------------------------------------------------------


def _reference(node: GraphNode) -> re.Pattern[str]:
    """字段模板里「带出了这个报告的正文」的写法。

    nodes.write.text、nodes['write']['text']、整个 nodes.write（过 json 也带着正文），以及
    存的就是正文的 vars.<assign_to>。nodes.write.repairs / stats / doc_artifact 只是把报告的
    统计或文档 id 一起放进成果，没碰正文——算成改动的话，一份全部带引用的报告会被一个
    「改写次数」字段拖成降档。模板只认路径加过滤器、没有方法调用，所以这几种写法就是全部。
    """
    nid = re.escape(node.id)
    head = rf"nodes\s*(?:\.\s*{nid}(?![\w-])|\[\s*['\"]{nid}['\"]\s*\])"
    alternatives = [rf"{head}(?:\s*\.\s*text(?![\w-])|\s*\[\s*['\"]text['\"]\s*\]|(?!\s*[.\[]))"]
    var = str(node.config.get("assign_to") or "").strip()
    if var:
        alternatives += [rf"vars\s*\.\s*{re.escape(var)}(?![\w-])",
                         rf"vars\s*\[\s*['\"]{re.escape(var)}['\"]\s*\]"]
    return re.compile("|".join(alternatives))


def _report_fields(
    result: dict[str, Any], mapping: Any, state: GraphState, ctx: NodeContext,
) -> list[dict[str, Any]]:
    """上游每个写出了文档的报告节点：哪些成果字段逐字就是它的原文，哪些引用了它却改动过。

    逐字相等的字段，界面能按文档逐段画、逐个数点开证据；模板在报告前后拼了别的字、
    或者过了过滤器，偏移就对不上了——这种字段不标证据，有契约时记一条 gap。
    首尾空白不算改动：Markdown 渲染出来一样，文档的 markdown 本来就去掉了首尾空白。
    """
    spec = ctx.run.spec
    upstream = _ancestors(spec, ctx.node.id)
    nodes = state.get("nodes") or {}
    sources = {f.get("name"): f.get("value", "") for f in mapping or [] if isinstance(f, dict) and f.get("name")}
    out = []
    for node in spec.nodes:
        payload = nodes.get(node.id)
        if node.type != NodeType.REPORT or node.id not in upstream or not isinstance(payload, dict):
            continue
        text = payload.get("text")
        if not payload.get("doc_artifact") or not isinstance(text, str) or not text.strip():
            continue
        verbatim = [name for name, value in result.items()
                    if not name.startswith("_") and isinstance(value, str) and value.strip() == text]
        pattern = _reference(node)
        edited = [name for name, src in sources.items() if name not in verbatim and pattern.search(
            src if isinstance(src, str) else json.dumps(src, ensure_ascii=False, default=str))]
        out.append({"node": node, "doc_artifact": payload["doc_artifact"], "verbatim": verbatim,
                    "edited": edited})
    return out


def _evidence_of(reports: list[dict[str, Any]], contract: Any) -> dict[str, Any] | None:
    """成果上的 _evidence：{report_node, doc_artifact, fields}。

    不管有没有契约、是不是正式运行都标——探索运行里的报告一样能点开看证据。契约的
    report_from 指着的那份优先；另有报告也被原样放进了成果的，列在 others 里。
    """
    shown = [r for r in reports if r["verbatim"]]
    if not shown:
        return None
    preferred = contract.get("report_from") if isinstance(contract, dict) else None
    shown.sort(key=lambda r: r["node"].id != preferred)
    first, *rest = [{"report_node": r["node"].id, "doc_artifact": r["doc_artifact"], "fields": r["verbatim"]}
                    for r in shown]
    return {**first, "others": rest} if rest else first


def _apply_contract(
    contract: dict[str, Any], state: GraphState, ctx: NodeContext,
    *, reports: list[dict[str, Any]] | None = None, cells: bool = True, governed: bool = False,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.engine.governance import board_only
    from app.engine.issuance import decide_tier, incomplete_gaps, sql_check_findings, trace_numbers

    nodes = state.get("nodes") or {}
    # 写了 report_from 就是引用模式：按报告文档逐段复核，不再按数值回指叙述
    report_from = contract.get("report_from")
    citations = report_from not in (None, "")

    # 1) 收指标集：contract.metrics_from 指向一个或多个口径卡节点
    metrics: list[dict[str, Any]] = []
    calibers: list[dict[str, str]] = []
    sources = contract.get("metrics_from") or []
    if isinstance(sources, str):
        sources = [sources]
    # gaps 记的是"校验本身哪里没跑起来"，和"校验跑了但没通过"是两回事。
    # 没有它，一个写错的节点名会让所有列表都是空的，看起来和"全部通过"一模一样。
    gaps: list[str] = []
    unresolved: list[str] = []
    #: 指标 id → 它来自哪张口径卡（「销售口径 @ v2」），逐个数字回指时标出处
    caliber_of: dict[str, str] = {}
    for source in sources:
        payload = nodes.get(source)
        if isinstance(payload, dict) and payload.get("kind") == "metric_set":
            metrics.extend(payload.get("metrics") or [])
            calibers.append(
                {
                    "node": source,
                    "caliber": payload.get("caliber", ""),
                    "version": payload.get("caliber_version", ""),
                }
            )
            label = " @ ".join(str(v) for v in (payload.get("caliber"), payload.get("caliber_version")) if v)
            for m in payload.get("metrics") or []:
                caliber_of.setdefault(str(m.get("id")), label)
        else:
            # 节点不存在、还没跑到、或者根本不是口径卡——指标集就是缺的，
            # 不能当作"这次没有指标要查"
            unresolved.append(source)
    if unresolved:
        gaps.append(f"指标来源未解析：{'、'.join(unresolved[:5])}")
    # 拿截断的查询结果整组算出来的指标：数回指得上，但算的只是取回的那一截，不能完整出具
    gaps.extend(incomplete_gaps(metrics))
    # 来源查询没通过 SQL 检查（error 级：重复计算、存量跨期加总）：口径卡复算得一致，算的却是错数。
    # 报告直接引用的查询要等引用核对之后才知道，缺口放在这个位置，下面一起算
    sql_at = len(gaps)

    # 引用模式下，缺输入记成空值的指标（口径卡 on_missing=null）就是缺：报告里引用不了它，
    # 也不能因为 id 在清单里就算「齐了」。旧模式照旧只看 id，行为不变
    present = {m["id"] for m in metrics if not citations or m.get("value") is not None}
    missing_required = [m for m in (contract.get("required") or []) if m not in present]
    missing_expected = [m for m in (contract.get("expected") or []) if m not in present]

    trace = None
    cited: dict[str, Any] = {}
    by_metrics = not citations and board_only(contract, ctx.run.spec, ctx.node.id)
    if citations:
        cited = _check_citations(str(report_from), contract, state, ctx, reports or [], cells=cells,
                                 governed=governed)
        gaps.extend(cited["gaps"])
        unmatched = cited["unmatched"]
    elif by_metrics:
        # 指标出具：没有叙述，给人看的是成果字段——字段里的数回指指标集，和叙述同一套规则；每个指标的输入要追到原始
        # 数据，追不到的是「这个数有算式、没出处」，照实记成缺口
        shown = _shown_text(fields or {})
        trace = trace_numbers(shown, metrics, allow=contract.get("allow_numbers")) if shown.strip() else None
        unmatched = trace.unmatched if trace else []
        gaps.extend(_untraced_gaps(metrics, ctx))
    else:
        # 2) 叙述数字回指
        declared_narrative = str(contract.get("narrative", ""))
        narrative = ctx.render_str(declared_narrative, state)
        # 模板取不到路径时 render 出来是空串，所以"声明了叙述但渲染成空"必须单独识别：
        # 节点改名、vars 写错都会让回指校验静默变成空操作
        if declared_narrative.strip() and not narrative.strip():
            gaps.append("叙述模板渲染为空（路径可能有误）")
        elif not declared_narrative.strip():
            gaps.append("出具契约未设置「叙述」，未执行数字追溯")

        trace = (
            trace_numbers(narrative, metrics, allow=contract.get("allow_numbers"))
            if narrative.strip()
            else None
        )
        unmatched = trace.unmatched if trace else []

    # 同一条查询算出几个指标、报告又引用了它的格，只记一句（按出问题的那条查询归并，列出受影响的引用）
    titles = {n.id: n.title for n in ctx.run.spec.nodes}
    aliases: dict[str, str] = cited.get("aliases") or {}

    def query_label(artifact: str, node_id: str | None) -> str:
        from app.engine.sql_problems import node_of

        title = titles.get(node_id or node_of(artifact, state.get("evidence")) or "")
        alias = aliases.get(artifact)
        if title and alias:
            return f"查询「{title}」（{alias}）"
        return f"查询「{title}」" if title else (f"查询 {alias}" if alias else "")

    findings = sql_check_findings(metrics, cited.get("queries"), label=query_label)
    gaps[sql_at:sql_at] = [f["text"] for f in findings]
    # 出具声明另存一份结构化的：报告页在显眼处说「因为 SQL 检查没通过而降档」，不从缺口的中文里认。没有就不加这个键
    from app.engine.sql_problems import node_of

    for f in findings:
        if not f.get("node_id") and f.get("artifact"):
            f["node_id"] = node_of(f["artifact"], state.get("evidence"))
    # gap 是 gaps 里对应的那一句：界面单列这几条时，据它不在「其余缺口」里重复
    sql_checks = [{**{k: f[k] for k in ("query", "node_id", "problems", "refs") if f.get(k)}, "gap": f["text"]}
                  for f in findings]

    # 声明了 metrics_from 却一个指标都没收到，同样是"没查成"
    if sources and not metrics:
        gaps.append("指标集为空，叙述中的数字无法追溯")

    # 上游有协作团队用完轮数、按降档交付的：叙述里的话不是调度者认可的结论，
    # 数字全都对得上也不能盖「完整出具」
    for node_id, payload in nodes.items():
        if isinstance(payload, dict) and payload.get("exhausted"):
            gaps.append(f"协作团队「{titles.get(node_id, node_id)}」用完 {payload.get('rounds', '?')} "
                        "轮仍未完成，交付的是成员最后的回复")
    # 上游节点自己报上来的缺口（双盲复算比不上、反方审稿的反驳没回应……）：只会降档，不会升档
    gaps.extend(_upstream_issuance_gaps(state, ctx))

    tier = decide_tier(
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=unmatched,
        strict=bool(contract.get("strict")),
        gaps=gaps,
        unresolved=cited.get("unresolved"),
        unsupported=cited.get("unsupported"),
        uncited_claims=cited.get("uncited"),
        claims_policy=cited.get("claims_policy"),
    )

    if citations:
        return _declare_citations(tier, cited, calibers=calibers, metrics=metrics, gaps=gaps,
                                  missing_required=missing_required, missing_expected=missing_expected,
                                  report_from=str(report_from), declared_at=datetime.now(timezone.utc),
                                  ctx=ctx, sql_checks=sql_checks)

    # 逐个数字的出处。以前只有一个计数，界面标不出「这个数来自哪个指标」
    matched = [
        {**hit, **({"caliber": caliber_of[hit["metric"]]} if caliber_of.get(hit.get("metric")) else {})}
        for hit in (trace.matched if trace else [])
    ][:50]
    issuance = {
        "tier": tier,
        **({"mode": "metrics"} if by_metrics else {}),
        "calibers": calibers,  # 口径版本照常印
        "metrics_checked": len(metrics),
        "missing_required": missing_required,
        "missing_expected": missing_expected,  # 缺数据声明照常印
        "unmatched_numbers": unmatched[:20],
        "matched_numbers": len(trace.matched) if trace else 0,
        "matched": matched,
        # 校验没跑全的原因照实印出来——读的人要能分辨"查过都对"和"根本没查"
        "gaps": gaps,
        "declared_at": datetime.now(timezone.utc).isoformat(),
        **({"sql_checks": sql_checks} if sql_checks else {}),
    }
    ctx.emit(
        EventType.ISSUANCE,
        tier=tier,
        **({"mode": "metrics"} if by_metrics else {}),
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=len(unmatched),
        calibers=calibers,
        # 降档的原因要跟着事件走：只有 gaps 的时候，时间线上的出具步骤以前只能写
        # 一个光秃秃的「降档出具」，横幅还说「请对照下方声明」，下方却什么都没有
        gaps=gaps,
        metrics_checked=len(metrics),
        matched_numbers=len(trace.matched) if trace else 0,
        matched=matched,
        **({"sql_checks": sql_checks} if sql_checks else {}),
    )
    return issuance


def _shown_text(fields: dict[str, Any]) -> str:
    """指标出具时给人看的文字：成果字段里的文字和数（成果自己的 _issuance、_evidence 不算）。表格、对象这类结构化的值不在
    这里核：它们是查询结果，出处是自己的快照，不是口径卡的指标。"""
    parts = []
    for name, value in fields.items():
        if str(name).startswith("_") or isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
    return "\n".join(parts)


def _untraced_gaps(metrics: list[dict[str, Any]], ctx: NodeContext) -> list[str]:
    """指标出具的缺口：输入没追到原始数据的指标，每个一句，说清停在哪个节点（engine/lineage 的认法）。"""
    from app.engine.lineage import untraced

    titles = {n.id: n.title for n in ctx.run.spec.nodes}
    out = []
    for m in metrics:
        stops = untraced(m.get("inputs") or [])
        if not stops:
            continue
        where = "、".join(dict.fromkeys(f"{s.get('path')}（停在「{titles.get(s.get('node_id') or '', s.get('node_id') or '未知来源')}」）"
                                        for s in stops[:3]))
        out.append(f"指标「{m.get('name') or m.get('id')}」的输入没追到原始数据：{where}")
    return out


def _upstream_issuance_gaps(state: GraphState, ctx: NodeContext) -> list[str]:
    """上游节点产出里的 issuance_gaps（字符串列表），每条前面加上是哪个节点说的。

    这是给「把关类」节点留的通用口子：节点觉得这次的结论不该完整出具（双盲复算两路对不上、
    反方审稿的反驳没有回应），在自己的产出里写 issuance_gaps，成果节点的出具契约照单收进缺口，
    出具随之降档。写的时候不用带节点名，这里统一加上「双盲复算「复核销售额」：」这样的前缀，
    和别的缺口一样读得出是谁报的。节点名就是类型的叫法（拖进画布没改名）时只写一次：「反方审稿：」，
    不写成「反方审稿「反方审稿」：」。缺口正文也不必再提节点类型，前缀已经说了。

    只认这个成果节点的上游：另一条支路上的节点管不到这份成果。不是字符串、空白的条目跳过——
    这个键谁都能写（数据整形的 JSON 模板也行），写坏了只当没写，不能让出具崩掉。
    """
    from app.engine.labels import type_label

    spec = ctx.run.spec
    upstream = _ancestors(spec, ctx.node.id)
    nodes = state.get("nodes") or {}
    out: list[str] = []
    for node in spec.nodes:
        payload = nodes.get(node.id)
        if node.id not in upstream or not isinstance(payload, dict):
            continue
        reported = payload.get("issuance_gaps")
        if not isinstance(reported, list):
            continue
        kind = type_label(node.type)
        prefix = f"{kind}：" if node.title == kind else f"{kind}「{node.title}」："
        out.extend(prefix + gap.strip() for gap in reported if isinstance(gap, str) and gap.strip())
    return out


# --------------------------------------------------------------------------
# 引用模式：按报告文档逐段复核
# --------------------------------------------------------------------------

#: 文档本身对不上（不是某个数没出处）：这次复核没法完成，记 gap
_INTEGRITY = {"bad_schema", "segment_mismatch", "structural_text", "render_mismatch", "eid_mismatch",
              "state_mismatch"}
#: 出具声明里列多少个可疑名字、多少句没挂依据的结论（计数照实，列表只是给人看的样本）
_LISTED = 20


def _check_citations(
    report_from: str, contract: dict[str, Any], state: GraphState, ctx: NodeContext,
    reports: list[dict[str, Any]], *, cells: bool = True, governed: bool = False,
) -> dict[str, Any]:
    """独立复核报告文档：{matched, unmatched, unresolved, gaps, doc_artifact, stats, claims_policy, uncited,
    unsupported, claims, entities}。

    不信报告节点自己的统计：文档按 id 从工件库取回（取回时复验哈希），目录按报告节点
    同一套参数从状态里重建（不用文档里存的那份），每个带引用的片段重新解析、重新渲染、
    逐字比对。一个配错的报告节点绕不过这里。

    cells：受管级别的正式运行、契约没声明 cells 时为 False，单元格引用一律判解析不了。
    governed：受管级别的正式运行。可疑实体（名字哪里都找不到）只在这时按缺口降档，别处只标注。
    """
    from app.core.artifact_store import load
    from app.engine.evidence import iter_units, ledger_enabled, resolve_ref, verify_doc
    from app.engine.nodes.report import report_catalog, report_entities

    out: dict[str, Any] = {"matched": [], "unmatched": [], "unresolved": [], "gaps": [],
                           "doc_artifact": None, "stats": None, "claims_policy": None, "uncited": None,
                           "unsupported": None, "claims": None, "entities": None, "queries": {}, "aliases": {}}
    gaps = out["gaps"]
    spec = ctx.run.spec
    node = spec.node_map().get(report_from)
    if node is None or node.type != NodeType.REPORT:
        gaps.append(f"出具契约的「报告来自」指向的「{report_from}」不是报告撰写节点，未执行引用核对")
        return out
    payload = (state.get("nodes") or {}).get(report_from)
    doc_artifact = payload.get("doc_artifact") if isinstance(payload, dict) else None
    if not doc_artifact:
        gaps.append(f"报告撰写节点「{node.title}」没有产出报告文档（未执行、被跳过或失败），"
                    "未执行引用核对")
        return out
    out["doc_artifact"] = doc_artifact
    try:
        doc = load(doc_artifact)
    except ValueError:
        gaps.append(f"报告「{node.title}」的文档与其哈希不一致，可能已被修改，未执行引用核对")
        return out
    if not isinstance(doc, dict):
        gaps.append(f"无法读取报告「{node.title}」的文档，未执行引用核对")
        return out
    if doc.get("node_id") != report_from or doc.get("run_id") != ctx.run.run_id \
            or doc.get("markdown") != payload.get("text"):
        gaps.append(f"报告「{node.title}」的文档与本次运行中的产出不一致，未执行引用核对")
        return out

    catalog = report_catalog(state, spec, node)
    checked = verify_doc(doc, catalog, allow_numbers=contract.get("allow_numbers"), cells_allowed=cells,
                         entities=report_entities(spec, node) == "link")
    out["stats"] = checked["stats"]
    broken: list[str] = []
    flagged: set[str] = set()
    suspicious: dict[str, list[dict[str, Any]]] = {"unknown_entity": [], "unverified_entity": []}
    for v in checked["violations"]:
        where = {k: v[k] for k in ("segment", "unit") if v.get(k)}
        if v.get("span"):
            where.update(start=v["span"][0], end=v["span"][1])
        if v.get("segment"):
            flagged.add(v["segment"])
        if v["code"] == "uncited_number":
            out["unmatched"].append({"token": v.get("text", ""), "context": v.get("context", ""), **where})
        elif v["code"] == "unresolved_ref":
            out["unresolved"].append({"ref": v.get("ref", ""), "message": v["message"], **where})
        elif v["code"] in suspicious:
            # 写 [[see:t:x]] 时违规上没有正文里的字，名字取自引用
            name = v.get("text") or str(v.get("ref") or "").partition(":")[2]
            suspicious[v["code"]].append({"name": name, **({"ref": v["ref"]} if v.get("ref") else {}), **where})
        elif v["code"] in _INTEGRITY:
            broken.append(v["message"])
    if broken:
        more = f"等 {len(broken)} 处" if len(broken) > 1 else ""
        gaps.append(f"报告「{node.title}」的文档未通过复核：{broken[0]}{more}")
    unknown, unverified = suspicious["unknown_entity"], suspicious["unverified_entity"]
    if unknown and governed:
        # 用户拍板：可疑实体在受管级别的正式出具里按「有缺口」降档，不拦截——反引号里的一个业务词
        # 也可能被当成名字，为它不予出具太重；探索运行、已发布级别只标注
        names = "".join(f"「{n}」" for n in dict.fromkeys(u["name"] for u in unknown[:5]))
        more = "等" if len(unknown) > 5 else ""
        gaps.append(f"报告「{node.title}」中有 {len(unknown)} 处可疑实体（{names}{more}）：本次运行的表结构、查询和"
                    "结果列中都没有这些名字，可能是编造的")
    if unknown or unverified:
        # 表结构快照不全时核对不了的名字（unverified）在哪个级别都只标注
        out["entities"] = {"unknown": unknown[:_LISTED], "unverified": unverified[:_LISTED],
                           "counted": governed and bool(unknown)}

    out["queries"], out["aliases"] = _cited_queries(doc, catalog)

    # 老前端要用的逐个数字出处，从复核通过的数字片段拼出来
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if seg.get("kind") != "number" or not seg.get("ref") or seg.get("id") in flagged:
                continue
            cite = resolve_ref(str(seg["ref"]), catalog, cells_allowed=cells)
            if cite["status"] != "resolved" or cite["rendered"] != seg.get("text"):
                continue
            entry = catalog.get(cite["alias"]) or {}
            start, end = seg["span"]
            hit: dict[str, Any] = {"token": seg["text"], "metric": cite["locator"].get("metric")}
            if cite["kind"] == "metric":
                label = " @ ".join(str(v) for v in (entry.get("caliber"), entry.get("version")) if v)
                if label:
                    hit["caliber"] = label
            elif cite["kind"] == "cell":
                # 查询单元格：Q1.r0.gmv（全局编号，和 doc.catalog 一致）
                hit["cell"] = f"{cite['alias']}.r{cite['locator']['row']}.{cite['locator']['column']}"
            else:
                hit["input"] = cite["locator"].get("field")
            out["matched"].append({**hit, "segment": seg["id"], "unit": unit["id"], "start": start, "end": end,
                                   "span": [start, end], "eid": cite["eid"]})

    _claims(out, contract, payload, evidence_on=ledger_enabled(ctx.run), governed=governed,
            uncited=checked.get("uncited") or [], doc=doc, title=node.title)

    mine = next((r for r in reports if r["node"].id == report_from), None)
    edited = mine["edited"] if mine else []
    for name in edited:
        gaps.append(f"成果字段「{name}」改动了报告，无法逐段对应")
    if not (mine and mine["verbatim"]) and not edited:
        gaps.append(f"成果中没有任何字段是报告「{node.title}」的原文：读者看到的内容未经过引用核对")
    return out


#: 没挂依据的结论句怎么处置，从松到严。认不出的写法按 degrade 算：写错一个词不能让缺口悄悄消失
_ON_UNCITED = ("ignore", "degrade", "withhold")
#: 证据相矛盾的结论句怎么处置（claims: judge 的 on_unsupported），从松到严。认不出的写法同样按 degrade 算
_ON_UNSUPPORTED = ("degrade", "withhold")
#: 要求结论句挂依据的策略：judge 在这一点上和 require_citation 一样严，另外再按裁判的判定判档
_CITING = ("require_citation", "judge")

#: 报告的正文写在代码块里、一句结论都没切出来时的缺口（_fenced_body）。不带核查节点的前缀：界面归在「校验未全部完成」下
GAP_FENCED_BODY = "报告「{title}」的正文写在代码块里，结论句未能核对"
#: 写的是文字的代码块：信息串为空、markdown、md（报告节点去整篇外层围栏时认的那几个，report._PROSE_INFO），另加 text、
#: plaintext、txt、plain 这几种纯文字的写法。sql、python、json 这类是模型真要贴的代码
_PROSE_FENCES = frozenset({"", "markdown", "md", "text", "plaintext", "txt", "plain"})
_CJK = re.compile(r"[\u4e00-\u9fff]")
#: 中文的句读：一行里有汉字、又有它们的，是在写句子（SQL 里的中文列名、别名不带这些）
_CJK_PUNCT = re.compile(r"[，。！？；：]")
#: 一行里连着六个以上汉字：信息串写明是文字的块里，这就是在写中文。表格的列名、地区名、门店名这类标签多在五个字以内
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{6,}")
#: 英文的词串：单词之间只隔空格、逗号。五个以上的单词紧跟句末标点算一句（_en_sentence）
_EN_RUN = re.compile(r"[A-Za-z]+(?:[ ,]+[A-Za-z]+)*")
_EN_WORD = re.compile(r"[A-Za-z]+")
#: 整行是注释（-- // # /* */ 开头），和行尾隔着空白的注释：写着中文句子也是代码的一部分。# 开头的 Markdown 标题本来
#: 也不算结论句（evidence._scan_blocks 把它切成 heading）
_COMMENT_LINE = re.compile(r"\s*(?:--|//|#|/\*|\*/)")
_TRAILING_COMMENT = re.compile(r"\s(?:--|//|#).*")
#: 看得出是代码的行里，-- // 紧挨着前面的字也是注释（SELECT SUM(amount)--按区域汇总）：字符串已经先去掉了
_CODE_COMMENT = re.compile(r"(?:--|//|\s#).*")
#: 写明是文字的块里顶格写的 Markdown 标题：标题前后是两段（_code_lines）
_MD_HEADING = re.compile(r"#{1,6}\s")
#: 代码里的字符串字面量（'…'、"…"，可带 f、r、b、u、N 这类前缀）：里面的中文标点是数据。引号前面紧挨着字母、数字的
#: 不算开头（英文的 it's、don't）
_STRING_LITERAL = re.compile(r"(?<![A-Za-z0-9_])[rRbBuUfFnN]{0,2}(?:'[^'\n]*'|\"[^\"\n]*\")")
#: 可以跨行的块注释、多行字符串：开头的写法，和它对应的收尾
_SPAN_OPEN = re.compile(r"/\*|\"\"\"|'''")
_SPAN_CLOSE = {"/*": "*/", '"""': '"""', "'''": "'''"}
#: SQL、Python 语句的开头。SQL 的关键字只认全小写、全大写的写法：「From 数据看……」「Update ……」这类首字母大写的是
#: 正文里的英文词；def、class 要写成定义的样子（def 名字(、class 名字: ），「class A 门店……」说的是门店的分类
_CODE_LINE = re.compile(r"\s*(?:(?:select|with|from|where|join|group\s+by|order\s+by|having|union|insert|update|delete"
                        r"|create|import|return|SELECT|WITH|FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|INSERT"
                        r"|UPDATE|DELETE|CREATE)\b|(?:def|class)\s+[A-Za-z_]\w*\s*[(:])")
#: YAML 的键（「columns:」「- name: 退款占比」），只认小写开头的：正文里的「Note:」「Q1:」是标签。值为空、下面缩进着
#: 列表或子键的另记一种（_YAML_NEST）
_YAML_KEY = re.compile(r"\s*(?:-\s+)?[a-z_][\w.-]*:(?:\s|$)")
_YAML_NEST = re.compile(r"\s*(?:-\s+)?[a-z_][\w.-]*:\s*$")
#: 块里再嵌的代码块：~~~ 或三个以上反引号开头。compose_doc 只认整行三个反引号的围栏，那样的一行外层就收了，块里剩下的
#: 嵌套写法是 ~~~sql、````sql 这些
_NESTED_FENCE = re.compile(r"\s*(`{3,}|~{3,})(.*)")
#: 像数据、公式的一行：前面有字的等号（公式、赋值、比较）和箭头（连起的流程），画目录树的制表符，JSON 的对象、数组开头
#: （{"、["）和「"键":」，函数调用（print(、groupby(），带扩展名的文件名（周报.xlsx）。行首的「→ 」「=> 」是列表的
#: 记号，不算
_DATA_LINE = re.compile(r"\S\s*(?:[=＝→⟶⇒]|->)|[\u2500-\u257f]|^\s*[\[{]\s*[\"'\[{]|[\"']\s*:"
                        r"|(?<![\w.])[A-Za-z_][\w.]*\(|(?<=[^\s.])\.[A-Za-z][A-Za-z0-9]{1,4}(?![\w.])")


def _en_sentence(line: str) -> bool:
    """一句英文：五个以上的单词（之间只隔空格、逗号）紧跟句末标点，后面是空白或行尾。按词串逐个数，不靠正则回溯：
    回溯的写法遇上很长、不带句末标点的一行，耗时随行长平方增长，出口复核又是在事件循环上同步执行的。"""
    for run in _EN_RUN.finditer(line):
        end = run.end()
        if line[end:end + 1] in (".", "!", "?") and (end + 1 == len(line) or line[end + 1].isspace()) \
                and len(_EN_WORD.findall(run.group())) >= 5:
            return True
    return False


def _sentence(line: str) -> bool:
    """成句：两个以上汉字又有中文句读，或者是一句英文。"""
    return (len(_CJK.findall(line)) >= 2 and _CJK_PUNCT.search(line) is not None) or _en_sentence(line)


def _concluding(line: str) -> bool:
    """按正文的规矩这一行会算一句结论（evidence._classify 认文字结论的那条）：带方向词、因果词，又不只是在下定义。
    只认有两个以上汉字的行：drop、rise 这类英文词在代码里也常见。数字不看，代码块里的数字报告节点按裸数字查。"""
    from app.engine.evidence import _CAUSAL, _DEFINITION, _DIRECTIONAL

    return len(_CJK.findall(line)) >= 2 and _DIRECTIONAL.search(line) is not None \
        and not (_DEFINITION.search(line) is not None and _CAUSAL.search(line) is None)


def _written(line: str) -> bool:
    """信息串写明是文字（markdown、text 这类）的块里，不是代码的这一行在写正文：成句、按正文的规矩会算结论，或者一行里
    连着六个以上汉字。不看列表标记：标记的写法太多（- + 1. 1、 一、 （1） · ●，也常不带标记），补一种漏一种；也不去
    字符串、行尾注释，# -- 和半角引号都是正文里的符号。整行是注释、标题的不算。"""
    if _COMMENT_LINE.match(line):
        return False
    return _sentence(line) or _CJK_RUN.search(line) is not None or _concluding(line)


def _prose_line(line: str, *, code: bool) -> bool:
    """代码的行（code），或者信息串为空的块里的行，在写正文：去掉字符串字面量和行尾注释之后成句；不是代码的行另认按正文
    的规矩会算结论的（不带句读的列表项、不带标记的行）。不带句读、也不像结论的不算：YAML 里的列名、diff 里改的口径和
    列表项没有分别。整行是注释的不算。"""
    if _COMMENT_LINE.match(line):
        return False
    line = (_CODE_COMMENT if code else _TRAILING_COMMENT).sub("", _STRING_LITERAL.sub(" ", line), count=1)
    return _sentence(line) or (not code and _concluding(line))


def _blank_spans(text: str) -> str:
    """块注释（/* … */）、三个引号括起的多行字符串涂成空格，换行留着，块里还是原来那几行：里面写着中文句子也是代码的
    一部分。没收尾的不动，开头那一行照注释行跳过。"""
    out: list[str] = []
    at = 0
    while (start := _SPAN_OPEN.search(text, at)) is not None:
        end = text.find(_SPAN_CLOSE[start.group()], start.end())
        if end == -1:
            break
        stop = end + len(_SPAN_CLOSE[start.group()])
        out += [text[at:start.start()], "\n".join(" " * len(part) for part in text[start.start():stop].split("\n"))]
        at = stop
    return "".join(out) + text[at:]


def _diff(lines: list[str], *, labelled: bool) -> bool:
    """整块是 diff：有 - 开头的行也有 + 开头的行，其余是空格开头的上下文行、@@ 开头的块头。diff 里一处改动先列删掉的行、
    再列加上的行，第一个改动行就是 + 的，是 + / - 开头的正负面要点（有 @@ 块头的照旧算 diff）。信息串写明是文字的块里，
    + - 后面空一格的是 Markdown 的列表项，只有紧挨着字写的（-按下单日期统计）才是 diff。"""
    heads = [line[:1] for line in lines if line.strip()]
    if not {"+", "-"} <= set(heads) <= {"+", "-", " ", "@"}:
        return False
    if labelled and any(line[:2] in ("+ ", "- ") for line in lines):
        return False
    return "@" in heads or next(h for h in heads if h in ("+", "-")) == "-"


def _data_like(line: str) -> bool:
    """像数据、公式的一行（_DATA_LINE），或者逗号隔开的一行表头、数据（两个以上半角逗号，逗号后面都不空格）。"""
    return _DATA_LINE.search(line) is not None or (line.count(",") >= 2 and ", " not in line)


def _yaml(lines: list[str], *, labelled: bool) -> bool:
    """一段是 YAML：第一行是小写开头的键，其余每一行都是键或者缩进的行（键下面的列表项、子键），注释行不看。写明是文字的
    块里另要有值为空、往下缩进的键：一行一个「键: 一句话」的（「- note: 华南销售额超过华北」）多半是正文里的标签。"""
    body = [line for line in lines if line.strip() and not _COMMENT_LINE.match(line)]
    if not body or not _YAML_KEY.match(body[0]):
        return False
    if not all(_YAML_KEY.match(line) or line[:1].isspace() for line in body[1:]):
        return False
    return not labelled or any(_YAML_NEST.match(line) for line in body)


def _bare_fence(line: str) -> str:
    """整行只写着围栏（``` 或 ~~~，不带信息串）的，返回这串围栏，能给块里再嵌的代码块收尾；别的行返回空串。"""
    fence = _NESTED_FENCE.match(line)
    return fence.group(1) if fence and not fence.group(2).strip() else ""


def _code_lines(lines: list[str], raw: list[str], *, labelled: bool) -> list[bool]:
    """逐行标出哪些是代码：块里再嵌的代码块（~~~sql），SQL、Python 的语句（从开头那一行到这一段结束，续行也算；前面的
    注释行本来就不算正文），整段是 YAML 的（_yaml），像数据、公式的行（_data_like）。

    lines 是涂掉块注释、多行字符串之后的行，raw 是原样的行：一段到空行为止，只写着注释的行不算空行；写明是文字的块里，
    顶格的标题也另起一段。不把整块当代码：正文里另贴一段 SQL、嵌一个 ~~~sql，或者有一行像 YAML 的键，别的行照样按正文认。
    再嵌的代码块要收得了才算（后面有同一种符号、不比开头短的收尾），开了没收的那一行照普通的行看。"""
    code = [False] * len(lines)
    paragraph: list[int] = []
    fence = ""
    # 每一行后面最长的收尾围栏（整行只有 ``` 或 ~~~），按符号分开记：开了收不了的围栏不算，照普通的行看，免得后面的
    # 正文都被当成代码
    reach = {"`": [0] * (len(lines) + 1), "~": [0] * (len(lines) + 1)}
    for k in range(len(lines) - 1, -1, -1):
        for after in reach.values():
            after[k] = after[k + 1]
        if bare := _bare_fence(lines[k]):
            reach[bare[0]][k] = max(reach[bare[0]][k], len(bare))

    def close() -> None:
        statement = _yaml([lines[k] for k in paragraph], labelled=labelled)
        for k in paragraph:
            statement = statement or _CODE_LINE.match(lines[k]) is not None
            code[k] = statement or _data_like(lines[k])
        paragraph.clear()

    for k, line in enumerate(lines):
        if fence:
            code[k] = True
            closing = _bare_fence(line)
            if closing[:1] == fence[0] and len(closing) >= len(fence):
                fence = ""
            continue
        if (opening := _NESTED_FENCE.match(line)) and reach[opening.group(1)[0]][k + 1] >= len(opening.group(1)):
            close()
            fence, code[k] = opening.group(1), True
            continue
        if not raw[k].strip() or (labelled and _MD_HEADING.match(line)):
            close()
            continue
        paragraph.append(k)
    close()
    return code


def _fenced_body(doc: dict[str, Any]) -> bool:
    """报告的正文写在代码块里：文档里一句结论都没有，却有写文字的代码块（_PROSE_FENCES），里面写着引用（片段里的
    [[m:…]] 这类标记、句子上挂的 [[see:…]] 依据，代码里不会这样写）或者在写正文。

    报告节点只在整篇正好是一个代码块时去掉外层围栏（report._unfenced），围栏外有前言、只有开头没有收尾、信息串写成
    text 的原样交给 compose_doc：整段正文落在代码块里，切不出结论句，裁判没有句子可判、反方审稿记 no_claims，没挂依据
    的、证据相矛盾的结论句都数出 0 句，出具就成了完整出具。有一句结论的不算：正文没被整个藏起来，结论句照常核对。

    在不在写正文逐行看。先标出哪些行是代码（_code_lines；信息串为空、整块是 diff 的全是代码），代码的行只认去掉注释、
    字符串之后成句的（_prose_line），中文别名、YAML 的值、公式里的「最高」「占比」这类词都不算。其余的行：信息串写明
    是文字的按 _written，信息串为空的块里也常是代码，去掉注释、字符串之后再看（_prose_line）。一块里只要有一行在写正文
    就算，所以宁可把拿不准的行当代码：藏起来的正文总有别的行认得出，贴的一段代码、公式里却一行认错就会记错缺口。"""
    from app.engine.evidence import iter_units

    units = list(iter_units(doc))
    if any(unit.get("kind") == "claim" for _, unit in units):
        return False
    for block, unit in units:
        lang = str(block.get("lang") or "").lower()
        if block.get("type") != "code" or lang not in _PROSE_FENCES:
            continue
        segments = [s for s in unit.get("segments") or [] if isinstance(s, dict)]
        if unit.get("see") or any(s.get("ref") for s in segments):
            return True
        # 围栏那两行是挂在块上的结构片段，不是块里写的内容：算进来的话，开头那行 ```markdown 会被当成块里再嵌的代码块，
        # 整块是不是 diff 也看不准
        text = "".join(str(s.get("text") or "") for s in segments if s.get("kind") != "structural")
        raw, lines = text.split("\n"), _blank_spans(text).split("\n")
        code = [True] * len(lines) if _diff(raw, labelled=bool(lang)) else _code_lines(lines, raw, labelled=bool(lang))
        if any(_written(line) if lang and not is_code else _prose_line(line, code=is_code)
               for line, is_code in zip(lines, code)):
            return True
    return False


def _claims(out: dict[str, Any], contract: dict[str, Any], payload: dict[str, Any], *, evidence_on: bool,
            governed: bool = False, uncited: list[dict[str, Any]], doc: dict[str, Any] | None = None,
            title: str = "") -> None:
    """结论句策略：没挂依据的、裁判判为证据不支持的结论句按 claims_policy 交给 decide_tier 判档。

    两处可以要求挂依据：报告节点写作时记下的 claims（节点产出里的，写作提示按它要求过），和契约自己写的
    require_citation / judge（对象形式可以另写 on_uncited）。两处都写了取更严的那个——契约只能收紧、不能放松：
    报告节点要求了，契约写 ignore 或 off 也照旧计入缺口，写 withhold 就收紧成不予出具。受管级别的正式运行
    里 ignore 一律当 degrade：没有哪道门禁查契约里的 claims，不能让它把用户要的底线拉低。

    报告节点写的是 judge 时，判定在文档里（按哈希取回的那份：判定是模型给的，出口复算不了，只能信封存的
    文档）。节点里判过的（正式运行，以及交给人工审批的探索运行）：证据相矛盾的（contradicted，旧文档里的 unsupported 同样算）按 on_unsupported
    判档（报告节点和契约取更严的）；证据不足的（insufficient）记一条缺口——最多降档，不会不予出具，也不会
    完整出具；没判完的（裁判没跑成、触顶）把裁判摘要里的缺口照抄进来——不能判完整出具，也不当成「矛盾」。
    其余探索运行按需裁判，这里只标注。契约要 judge、报告节点却没开的，照实记缺口：出口替不了报告节点去裁判。

    数的是这里重新核对出来的 uncited，不信文档里记的 cites。升级前写的报告（节点产出里没有 claims 这个键，
    包括跨着升级还没跑完的运行）一律不管，契约写了也只记一条缺口：报告生成于升级前，结论句未核对。

    要核对结论句、正文却写在代码块里（一句结论都没切出来，_fenced_body）的，另记一条缺口：数出 0 句不是「核对过、
    没问题」。
    """
    written = payload.get("claims") if isinstance(payload, dict) else None
    # 报告节点升级后才在产出里记 claims（off 也记）：没有这个键就是升级前写的报告
    upgraded = evidence_on and isinstance(payload, dict) and "claims" in payload
    declared = contract.get("claims")
    name = declared.get("policy") if isinstance(declared, dict) else declared
    asks: list[str] = []
    if upgraded and written in _CITING:
        asks.append("degrade")
    if upgraded and name in _CITING:
        wanted = declared.get("on_uncited") if isinstance(declared, dict) else None
        asks.append(wanted if wanted in _ON_UNCITED else "degrade")
    elif name not in (None, "", "off"):
        out["gaps"].append("出具契约要求核对结论句，但这份报告生成于系统升级前，没有结论句核对记录，结论句未核对")
    judged = upgraded and written == "judge"
    if upgraded and name == "judge" and not judged:
        out["gaps"].append(f"出具契约要求结论句由模型裁判，但报告「{title}」未开启模型裁判，结论句未裁判")
    if not asks:
        return
    if doc is not None and _fenced_body(doc):
        # 正文在代码块里，一句结论都没切出来：没挂依据的、证据相矛盾的都是 0 句，不能当成「核对过、没问题」
        out["gaps"].append(GAP_FENCED_BODY.format(title=title))
    on_uncited = max(asks, key=_ON_UNCITED.index)
    if governed and on_uncited == "ignore":
        on_uncited = "degrade"
    extra = {"on_uncited": on_uncited} if on_uncited != "degrade" else {}
    if not judged:
        policy: Any = "require_citation" if on_uncited == "degrade" else \
            {"policy": "require_citation", "on_uncited": on_uncited}
        out["claims_policy"], out["uncited"] = policy, uncited
        out["claims"] = {"policy": "require_citation", "uncited_claims": len(uncited), "uncited": uncited[:_LISTED],
                         **extra}
        return

    from app.engine.evidence import iter_units
    from app.engine.judge import COUNT_KEYS, UNSUPPORTED

    summary = (doc or {}).get("judge")
    if not isinstance(summary, dict):
        # 报告节点开了 judge、文档里却没有裁判摘要（不该发生）：当作没判，不能判完整出具
        summary = {"mode": "inline", "counts": {}, "unjudged": {}, "limits_hit": [], "complete": False,
                   "gaps": ["已开启模型裁判，但文档中没有裁判结果，结论句未裁判"]}
    asked = [summary.get("on_unsupported")]
    if name == "judge" and isinstance(declared, dict):
        asked.append(declared.get("on_unsupported"))
    on_unsupported = max((a if a in _ON_UNSUPPORTED else "degrade" for a in asked), key=_ON_UNSUPPORTED.index)
    inline = summary.get("mode") == "inline"
    flagged: dict[str, list[dict[str, Any]]] = {s: [] for s in ("contradicted", "insufficient", UNSUPPORTED, "partial")}
    if inline:
        # 按需裁判的判定不在文档里（在封存之后追加的 evidence.judged 事件里），这里只数节点里判过的
        markdown = str((doc or {}).get("markdown") or "")
        for _, unit in iter_units(doc or {}):
            verdict = unit.get("verdict")
            status = verdict.get("status") if isinstance(verdict, dict) else None
            if status in flagged:
                span = unit.get("span") or [0, 0]
                flagged[status].append({"unit": unit.get("id"), "span": span, "text": markdown[span[0]:span[1]],
                                        "rationale": verdict.get("rationale") or "",
                                        **({"missing": verdict["missing"]} if status == "insufficient"
                                           and verdict.get("missing") else {})})
        out["gaps"].extend(f"报告「{title}」{gap}" for gap in summary.get("gaps") or [])
        if lacking := flagged["insufficient"]:
            # 证据不足按缺口处理：最多降档，不因 on_unsupported 不予出具，也不能完整出具
            missing = list(dict.fromkeys(i["missing"] for i in lacking if i.get("missing")))
            what = f"（缺少：{'、'.join(missing[:5])}{'等' if len(missing) > 5 else ''}）" if missing else ""
            out["gaps"].append(f"报告「{title}」有 {len(lacking)} 句结论证据不足{what}：裁判未能在证据中找到判断所需的"
                               "信息，这些结论未经核实")
    counts = {k: int((summary.get("counts") or {}).get(k) or 0) for k in COUNT_KEYS}
    # 证据相矛盾的按 on_unsupported 判档；旧文档里的 unsupported（当时没分矛盾和不足）照旧当 contradicted
    contradicted = [*flagged["contradicted"], *flagged[UNSUPPORTED]]
    out["claims_policy"] = {"policy": "judge", "on_unsupported": on_unsupported, "on_uncited": on_uncited}
    out["uncited"], out["unsupported"] = uncited, contradicted
    out["claims"] = {
        "policy": "judge", "uncited_claims": len(uncited), "uncited": uncited[:_LISTED], **extra,
        "on_unsupported": on_unsupported, "mode": "inline" if inline else "on_demand", "counts": counts,
        "contradicted": flagged["contradicted"][:_LISTED], "insufficient": flagged["insufficient"][:_LISTED],
        "unsupported": flagged[UNSUPPORTED][:_LISTED], "partial": flagged["partial"][:_LISTED],
        "unjudged": dict(summary.get("unjudged") or {}), "limits_hit": list(summary.get("limits_hit") or []),
        "complete": bool(summary.get("complete")), "model": summary.get("model"),
    }


def _cited_queries(doc: dict[str, Any], catalog: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """报告直接引用的查询：({查询快照 id: [引用的写法…]}, {目录里每个查询的快照 id: 编号})。

    单元格（[[v:Q1.r0.gmv]]，[[table:]] 展开后也是一格一格的引用）按查询数：一格写「Q1 第 1 行「gmv」」，
    几格写「Q1 的 3 格」；只拿它当依据（[[see:Q1]]）的写「Q1（结论依据）」。出具契约据此判这些查询有没有通过
    SQL 检查（issuance.sql_check_gaps）。
    """
    from app.engine.evidence import iter_units

    cells: dict[str, list[tuple[Any, Any]]] = {}
    support: list[str] = []
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            cite = seg.get("cite") or {}
            if cite.get("kind") == "cell" and cite.get("status") == "resolved" and cite.get("alias"):
                loc = cite.get("locator") or {}
                cells.setdefault(str(cite["alias"]), [])
                if (loc.get("row"), loc.get("column")) not in cells[str(cite["alias"])]:
                    cells[str(cite["alias"])].append((loc.get("row"), loc.get("column")))
        for cite in unit.get("see") or []:
            alias = str((cite or {}).get("alias") or "")
            if (catalog.get(alias) or {}).get("kind") == "query" and alias not in support:
                support.append(alias)
    queries: dict[str, list[str]] = {}
    # 目录里全部查询的编号：合并查询的输入没被直接引用，缺口里也要写得出它是 Q 几
    aliases = {str(e["artifact"]): str(e["alias"]) for e in catalog.values()
               if isinstance(e, dict) and e.get("kind") == "query" and e.get("artifact") and e.get("alias")}
    for alias in [*cells, *(a for a in support if a not in cells)]:
        artifact = (catalog.get(alias) or {}).get("artifact")
        if not isinstance(artifact, str) or not artifact:
            continue
        hit = cells.get(alias) or []
        if len(hit) == 1 and isinstance(hit[0][0], int):
            ref = f"{alias} 第 {hit[0][0] + 1} 行「{hit[0][1]}」"
        else:
            ref = f"{alias} 的 {len(hit)} 格" if hit else f"{alias}（结论依据）"
        queries.setdefault(artifact, []).append(ref)
    return queries, aliases


def _declare_citations(
    tier: str, cited: dict[str, Any], *, calibers: list[dict[str, str]], metrics: list[dict[str, Any]],
    gaps: list[str], missing_required: list[str], missing_expected: list[str], report_from: str,
    declared_at: Any, ctx: NodeContext, sql_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """引用模式的 _issuance。旧字段一个不少（老前端照常显示），另加 mode、report、unresolved。"""
    report = {"node_id": report_from, "doc_artifact": cited["doc_artifact"]}
    issuance = {
        "mode": "citations",
        "tier": tier,
        "report": report,
        "calibers": calibers,
        "metrics_checked": len(metrics),
        "missing_required": missing_required,
        "missing_expected": missing_expected,
        "unmatched_numbers": cited["unmatched"][:20],
        "unresolved": cited["unresolved"][:20],
        "matched_numbers": len(cited["matched"]),
        "matched": cited["matched"][:50],
        "stats": cited["stats"],
        "gaps": gaps,
        "declared_at": declared_at.isoformat(),
        # 结论句策略和可疑实体：只在用上时才有这两个键，升级前的运行和没用上的契约形状不变
        **({"claims": cited["claims"]} if cited.get("claims") else {}),
        **({"entities": cited["entities"]} if cited.get("entities") else {}),
        # 没通过 SQL 检查的查询（结构化）：报告页据此在显眼处说明降档原因。没有就不加这个键
        **({"sql_checks": sql_checks} if sql_checks else {}),
    }
    entities = cited.get("entities")
    ctx.emit(
        EventType.ISSUANCE,
        mode="citations",
        tier=tier,
        report=report,
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=len(cited["unmatched"]),
        unresolved=len(cited["unresolved"]),
        calibers=calibers,
        gaps=gaps,
        **({"sql_checks": sql_checks} if sql_checks else {}),
        metrics_checked=len(metrics),
        matched_numbers=len(cited["matched"]),
        matched=cited["matched"][:50],
        # on_uncited 只在不是默认的 degrade 时才有（收紧成 withhold、探索运行的契约写了 ignore）；
        # judge 另带不支持时怎么判档、在哪判的和各判定的句数（名单只在出具声明里）
        **({"claims": {k: cited["claims"][k] for k in ("policy", "uncited_claims", "on_uncited", "on_unsupported",
                                                        "mode", "counts")
                       if k in cited["claims"]}} if cited.get("claims") else {}),
        # 计数取复核的统计（声明里的名单最多列 _LISTED 个）
        **({"entities": {"unknown": (cited["stats"] or {}).get("unknown_entities", len(entities["unknown"])),
                         "unverified": (cited["stats"] or {}).get("unverified_entities", len(entities["unverified"])),
                         "counted": entities["counted"]}} if entities else {}),
    )
    return issuance


async def run_transform(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """数据整形。用受限表达式把上游输出揉成下游要的形状，不用为此专门起一个 LLM。"""
    tctx = template_context(state)
    mode = ctx.cfg("mode", "expression")
    output: Any

    if mode == "expression":
        expr = ctx.cfg("expression", "")
        if not expr:
            raise NodeError(ctx.node.id, "「数据整形」节点还没有填写表达式")
        try:
            output = eval_expression(expr, tctx)
        except ExpressionError as e:
            raise NodeError(ctx.node.id, f"表达式有误：{e}") from e
    elif mode == "template":
        output = ctx.render_str(ctx.cfg("template", ""), state)
    elif mode == "json":
        template = ctx.cfg("template", "{}")
        rendered = ctx.render_str(template, state)
        try:
            output = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise _json_error(e, str(template or ""), tctx, ctx) from e
    else:
        raise NodeError(ctx.node.id, f"不支持的整形方式：{mode}")

    updates: dict[str, Any] = {"nodes": {ctx.node.id: output}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output}
    return updates


# --------------------------------------------------------------------------
# 按 JSON 解析失败：错在模板，还是错在上游模型写的文字
# --------------------------------------------------------------------------

#: 产出是模型写的文字的节点（llm 配了 output_schema 时 text 是系统序列化的，但那样也解析得了）
_MODEL_TEXT = (NodeType.AGENT, NodeType.LLM, NodeType.SUPERVISOR)
#: 技术细节里给出出错位置前后各多少个字
_NEAR = 30
_NODE_HEAD = re.compile(r"^nodes\s*(?:\.\s*([\w-]+)|\[\s*['\"]([^'\"]+)['\"]\s*\])")
_VAR_HEAD = re.compile(r"^vars\s*(?:\.\s*([\w-]+)|\[\s*['\"]([^'\"]+)['\"]\s*\])")


TEMPLATE_HINT = "请检查模板中的引号和逗号，字符串值需使用 | json 过滤器输出"
#: 解析器在这一段的头一个字上要的是这些：前一个值在模板里已经写完了，缺的是模板里的标点
_DELIMITED = ("Expecting ',' delimiter", "Expecting ':' delimiter", "Extra data")


def _json_error(e: json.JSONDecodeError, template: str, tctx: dict[str, Any], ctx: NodeContext) -> NodeError:
    """整形节点按 JSON 解析失败时的报错。

    真实踩过：整个模板就是 {{ nodes.X.text }}，agent 在 JSON 字符串里夹了没转义的英文双引号，
    报错却让人去检查模板、加 | json，把人引到了错的方向。反过来也不行：出错的位置落在模型
    写的那一段里，不等于错在模型——模板把一句话原样放进值的位置、放进引号里，或者紧挨着它
    漏了逗号，都是模板的错，而且改法就是那句老提示。所以分三种说（见 _diagnose）：

    - 上游确实在写 JSON 而没写对：点名上游、按实情说原因、指向结构化输出；
    - 模板少了 | json：点名是模板里哪一段，给出改好的写法；
    - 其余：原来那句提示。

    出错位置前后的原文只进技术细节（包在原异常里）：首行是给人看的一句话，不贴模型的原文。
    """
    doc, pos = e.doc, e.pos
    near = (doc[max(0, pos - _NEAR):pos] + "⟨此处⟩" + doc[pos:pos + _NEAR]).replace("\n", "\\n")
    e.args = (f"{e.args[0]}\n出错位置前后的原文：{near}",)
    verdict = _diagnose(template, tctx, ctx, e)
    if verdict is None or isinstance(verdict, str):
        return NodeError(ctx.node.id, f"模板渲染出来的不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列附近）。"
                                      f"{verdict or TEMPLATE_HINT}")
    node, text, offset, reason = verdict
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1) + 1
    where = f"第 {line} 行第 {column} 列" if line > 1 else f"第 {column} 列"
    fix = ("如需模型输出结构化数据，请为其配置「结构化输出 Schema」；数字需要进入口径卡时，"
           "请改用 Agent 并开启「按出处核对字段」"
           if node.type == NodeType.LLM else
           "如需模型输出结构化数据，请配置「结构化输出 Schema」并开启「按出处核对字段」，"
           "不要用「数据整形」节点解析模型写的文字")
    return NodeError(ctx.node.id, f"上游「{node.title}」输出的不是合法 JSON（{where}附近），{reason}；{fix}")


def _diagnose(template: str, tctx: dict[str, Any], ctx: NodeContext,
              e: json.JSONDecodeError) -> tuple[GraphNode, str, int, str] | str | None:
    """按 JSON 解析失败，错在谁。

    返回 (上游节点, 它写的那段文字, 出错位置在文字里的偏移, 原因) 表示错在上游；返回一句话
    表示错在模板、而且说得出是哪一段；返回 None 表示照原来那句提示。

    只有上游确实在写 JSON（去掉开头空白后以 { 或 [ 开头，或者包在 ``` 代码块里）、而且这一段
    不在模板的引号里，才算上游的错；整个模板就是这一段时，模板不可能写错，也算上游的。
    解析器停在这一段末尾时（紧跟着的逗号漏了），只有它自己开了括号、引号没收尾才怪它。

    每个 {{ }} 单独渲染一次再拼起来，和 render_template 的整段替换结果逐字相同。
    """
    from app.engine.expressions import _TEMPLATE_RE, render_template, resolve_path

    doc, pos = e.doc, e.pos
    at, last = 0, 0
    for m in _TEMPLATE_RE.finditer(template):
        at += m.start() - last
        piece = render_template(m.group(0), tctx)
        start, end = at, at + len(piece)
        at, last = end, m.end()
        expr = m.group(1).strip()
        head, *filters = [p.strip() for p in expr.split("|")]
        if not head or pos < start:
            continue
        # 解析器越过空白才报错：停在这一段后面、中间只隔着空白的，也是停在它末尾
        at_end = pos >= end and not doc[end:pos].strip()
        if not piece.strip():
            # 取出来是空的，解析器正好在这里要一个值
            if e.msg == "Expecting value" and not doc[start:pos].strip() and not _in_json_string(doc, start):
                return f"模板里 {{{{ {expr} }}}} 的取值为空，此处缺少一个值：请检查路径是否正确、前面的节点是否有产出"
            continue
        if pos > end and not at_end:
            continue
        quoted = _in_json_string(doc, start)
        if "json" in filters:
            # 过了 | json 就是一个合法的 JSON 值：错不在它的内容里，除非外面又套了一层引号
            return (f"模板里 {{{{ {expr} }}}} 的结果已是合法的 JSON（文字自带引号），外面不要再加引号"
                    if quoted and not at_end else None)
        value = resolve_path(tctx, head)
        if not isinstance(value, str) or value != piece:
            return None
        node = _producer(head, ctx)
        model = node is not None and node.type in _MODEL_TEXT
        lead = start + len(piece) - len(piece.lstrip())
        if pos == lead and e.msg.startswith(_DELIMITED):
            return None                          # 前一个值在模板里就写完了，缺的是它前面的标点
        if template.strip() == m.group(0):
            return (node, piece, pos - start, _why(piece, at_end)) if model else None
        if at_end and not _unclosed(piece):
            return None                          # 这一段本身完整，缺的是它后面的标点
        if quoted:
            return _needs_json(expr, model)
        if _writes_json(piece):
            return (node, piece, pos - start, _why(piece, at_end)) if model else None
        return _needs_json(expr, model)
    return None


def _needs_json(expr: str, model: bool) -> str:
    what = "模型写的文字" if model else "一段文字"
    return f"模板里 {{{{ {expr} }}}} 是{what}，放入 JSON 时请写成 {{{{ {expr} | json }}}}（外面不要再加引号）"


def _why(text: str, at_end: bool) -> str:
    """上游写的 JSON 为什么解析不了，按看得出来的实情说。"""
    body = text.lstrip()
    if body.startswith("```"):
        return "模型把 JSON 包在了 ``` 代码块中"
    if at_end and _unclosed(text):
        return "模型输出的 JSON 不完整，可能在中途被截断"
    if not body.startswith(("{", "[")):
        return "模型输出的是一段文字，而不是 JSON"
    return "常见原因是字符串中有未转义的英文引号"


def _writes_json(text: str) -> bool:
    """这段文字看得出是想写 JSON：以 { 或 [ 开头，或者是包在 ``` 代码块里的 JSON。"""
    body = text.lstrip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].lstrip() if "\n" in body else ""
    return body.startswith(("{", "["))


def _in_json_string(doc: str, index: int) -> bool:
    """doc[:index] 结束时是不是在一个 JSON 字符串里。解析器已经越过了这里，这一段前缀是
    合法的 JSON 开头，数引号（跳过转义）就数得准。"""
    inside, i = False, 0
    while i < index:
        ch = doc[i]
        if inside and ch == "\\":
            i += 2
            continue
        if ch == '"':
            inside = not inside
        i += 1
    return inside


def _unclosed(text: str) -> bool:
    """这段文字自己开了引号、括号却没收尾（单独看它，从字符串外面数起）。"""
    depth, inside, i = 0, False, 0
    while i < len(text):
        ch = text[i]
        if inside:
            if ch == "\\":
                i += 1
            elif ch == '"':
                inside = False
        elif ch == '"':
            inside = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return inside or depth > 0


def _producer(path: str, ctx: NodeContext) -> GraphNode | None:
    """模板路径取的是哪个节点的产出：nodes.X 就是 X；vars.Y 是 assign_to 为 Y 的节点。"""
    nodes = ctx.run.spec.node_map()
    if m := _NODE_HEAD.match(path):
        return nodes.get(m.group(1) or m.group(2))
    if m := _VAR_HEAD.match(path):
        var = m.group(1) or m.group(2)
        writers = [n for n in ctx.run.spec.nodes if str(n.config.get("assign_to") or "").strip() == var]
        # 好几个节点写同一个变量时说不准是谁，宁可给原来的提示也不点错名
        return writers[0] if len(writers) == 1 else None
    return None
