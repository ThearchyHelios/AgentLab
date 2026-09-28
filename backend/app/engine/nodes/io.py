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
        result["_issuance"] = _apply_contract(contract, state, ctx, reports=reports)
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
    *, reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.engine.issuance import decide_tier, trace_numbers

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
        gaps.append(f"指标源未解析：{', '.join(unresolved[:5])}")

    # 引用模式下，缺输入记成空值的指标（口径卡 on_missing=null）就是缺：报告里引用不了它，
    # 也不能因为 id 在清单里就算「齐了」。旧模式照旧只看 id，行为不变
    present = {m["id"] for m in metrics if not citations or m.get("value") is not None}
    missing_required = [m for m in (contract.get("required") or []) if m not in present]
    missing_expected = [m for m in (contract.get("expected") or []) if m not in present]

    trace = None
    cited: dict[str, Any] = {}
    if citations:
        cited = _check_citations(str(report_from), contract, state, ctx, reports or [])
        gaps.extend(cited["gaps"])
        unmatched = cited["unmatched"]
    else:
        # 2) 叙述数字回指
        declared_narrative = str(contract.get("narrative", ""))
        narrative = ctx.render_str(declared_narrative, state)
        # 模板取不到路径时 render 出来是空串，所以"声明了叙述但渲染成空"必须单独识别：
        # 节点改名、vars 写错都会让回指校验静默变成空操作
        if declared_narrative.strip() and not narrative.strip():
            gaps.append("叙述模板渲染为空（路径可能写错了）")
        elif not declared_narrative.strip():
            gaps.append("契约没有声明叙述，数字回指未执行")

        trace = (
            trace_numbers(narrative, metrics, allow=contract.get("allow_numbers"))
            if narrative.strip()
            else None
        )
        unmatched = trace.unmatched if trace else []

    # 声明了 metrics_from 却一个指标都没收到，同样是"没查成"
    if sources and not metrics:
        gaps.append("指标集为空，叙述里的数字无从回指")

    # 上游有协作团队用完轮数、按降档交付的：叙述里的话不是调度者认可的结论，
    # 数字全都对得上也不能盖「完整出具」
    titles = {n.id: n.title for n in ctx.run.spec.nodes}
    for node_id, payload in nodes.items():
        if isinstance(payload, dict) and payload.get("exhausted"):
            gaps.append(f"协作团队「{titles.get(node_id, node_id)}」用完 {payload.get('rounds', '?')} "
                        "轮仍未完成，交来的是成员最后的原话")

    tier = decide_tier(
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=unmatched,
        strict=bool(contract.get("strict")),
        gaps=gaps,
        unresolved=cited.get("unresolved"),
    )

    if citations:
        return _declare_citations(tier, cited, calibers=calibers, metrics=metrics, gaps=gaps,
                                  missing_required=missing_required, missing_expected=missing_expected,
                                  report_from=str(report_from), declared_at=datetime.now(timezone.utc),
                                  ctx=ctx)

    # 逐个数字的出处。以前只有一个计数，界面标不出「这个数来自哪个指标」
    matched = [
        {**hit, **({"caliber": caliber_of[hit["metric"]]} if caliber_of.get(hit.get("metric")) else {})}
        for hit in (trace.matched if trace else [])
    ][:50]
    issuance = {
        "tier": tier,
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
    }
    ctx.emit(
        EventType.ISSUANCE,
        tier=tier,
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
    )
    return issuance


# --------------------------------------------------------------------------
# 引用模式：按报告文档逐段复核
# --------------------------------------------------------------------------

#: 文档本身对不上（不是某个数没出处）：这次复核没法完成，记 gap
_INTEGRITY = {"bad_schema", "segment_mismatch", "structural_text", "render_mismatch", "eid_mismatch",
              "state_mismatch"}


def _check_citations(
    report_from: str, contract: dict[str, Any], state: GraphState, ctx: NodeContext,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """独立复核报告文档：{matched, unmatched, unresolved, gaps, doc_artifact, stats}。

    不信报告节点自己的统计：文档按 id 从工件库取回（取回时复验哈希），目录按报告节点
    同一套参数从状态里重建（不用文档里存的那份），每个带引用的片段重新解析、重新渲染、
    逐字比对。一个配错的报告节点绕不过这里。
    """
    from app.core.artifact_store import load
    from app.engine.evidence import iter_units, resolve_ref, verify_doc
    from app.engine.nodes.report import report_catalog

    out: dict[str, Any] = {"matched": [], "unmatched": [], "unresolved": [], "gaps": [],
                           "doc_artifact": None, "stats": None}
    gaps = out["gaps"]
    spec = ctx.run.spec
    node = spec.node_map().get(report_from)
    if node is None or node.type != NodeType.REPORT:
        gaps.append(f"契约的 report_from 指向的「{report_from}」不是报告撰写节点，引用核对未执行")
        return out
    payload = (state.get("nodes") or {}).get(report_from)
    doc_artifact = payload.get("doc_artifact") if isinstance(payload, dict) else None
    if not doc_artifact:
        gaps.append(f"报告撰写节点「{node.title}」没有产出报告文档（没跑到、被跳过或者失败了），"
                    "引用核对未执行")
        return out
    out["doc_artifact"] = doc_artifact
    try:
        doc = load(doc_artifact)
    except ValueError:
        gaps.append(f"报告「{node.title}」的文档和它的哈希对不上，疑似被改过，引用核对未执行")
        return out
    if not isinstance(doc, dict):
        gaps.append(f"报告「{node.title}」的文档在工件库里取不回来，引用核对未执行")
        return out
    if doc.get("node_id") != report_from or doc.get("run_id") != ctx.run.run_id \
            or doc.get("markdown") != payload.get("text"):
        gaps.append(f"报告「{node.title}」的文档和这次运行里它的产出对不上，引用核对未执行")
        return out

    catalog = report_catalog(state, spec, node)
    checked = verify_doc(doc, catalog, allow_numbers=contract.get("allow_numbers"))
    out["stats"] = checked["stats"]
    broken: list[str] = []
    flagged: set[str] = set()
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
        elif v["code"] in _INTEGRITY:
            broken.append(v["message"])
    if broken:
        more = f"等 {len(broken)} 处" if len(broken) > 1 else ""
        gaps.append(f"报告「{node.title}」的文档没通过复核：{broken[0]}{more}")

    # 老前端要用的逐个数字出处，从复核通过的数字片段拼出来
    for _, unit in iter_units(doc):
        for seg in unit.get("segments") or []:
            if seg.get("kind") != "number" or not seg.get("ref") or seg.get("id") in flagged:
                continue
            cite = resolve_ref(str(seg["ref"]), catalog)
            if cite["status"] != "resolved" or cite["rendered"] != seg.get("text"):
                continue
            entry = catalog.get(cite["alias"]) or {}
            start, end = seg["span"]
            hit: dict[str, Any] = {"token": seg["text"], "metric": cite["locator"].get("metric")}
            if cite["kind"] == "metric":
                label = " @ ".join(str(v) for v in (entry.get("caliber"), entry.get("version")) if v)
                if label:
                    hit["caliber"] = label
            else:
                hit["input"] = cite["locator"].get("field")
            out["matched"].append({**hit, "segment": seg["id"], "unit": unit["id"], "start": start, "end": end,
                                   "span": [start, end], "eid": cite["eid"]})

    claims = contract.get("claims")
    policy = claims.get("policy") if isinstance(claims, dict) else claims
    if policy not in (None, "", "off"):
        gaps.append("契约声明了结论句检查（claims），这一版还不支持，结论句没有核对")

    mine = next((r for r in reports if r["node"].id == report_from), None)
    edited = mine["edited"] if mine else []
    for name in edited:
        gaps.append(f"成果字段「{name}」改动了报告，无法逐段对应")
    if not (mine and mine["verbatim"]) and not edited:
        gaps.append(f"成果里没有哪个字段是报告「{node.title}」的原文：读者看到的内容没有经过引用核对")
    return out


def _declare_citations(
    tier: str, cited: dict[str, Any], *, calibers: list[dict[str, str]], metrics: list[dict[str, Any]],
    gaps: list[str], missing_required: list[str], missing_expected: list[str], report_from: str,
    declared_at: Any, ctx: NodeContext,
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
    }
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
        metrics_checked=len(metrics),
        matched_numbers=len(cited["matched"]),
        matched=cited["matched"][:50],
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
            raise NodeError(ctx.node.id, "整形节点没有填表达式")
        try:
            output = eval_expression(expr, tctx)
        except ExpressionError as e:
            raise NodeError(ctx.node.id, f"表达式错误：{e}") from e
    elif mode == "template":
        output = ctx.render_str(ctx.cfg("template", ""), state)
    elif mode == "json":
        rendered = ctx.render_str(ctx.cfg("template", "{}"), state)
        try:
            output = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise NodeError(
                ctx.node.id,
                f"模板渲染出来的不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列附近）。"
                "检查模板里的引号、逗号，字符串值要用 | json 过滤器输出",
            ) from e
    else:
        raise NodeError(ctx.node.id, f"未知的整形模式：{mode}")

    updates: dict[str, Any] = {"nodes": {ctx.node.id: output}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output}
    return updates
