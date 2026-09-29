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
        result["_issuance"] = _apply_contract(contract, state, ctx, reports=reports, cells=cells, governed=governed)
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
        cited = _check_citations(str(report_from), contract, state, ctx, reports or [], cells=cells,
                                 governed=governed)
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
        unsupported=cited.get("unsupported"),
        uncited_claims=cited.get("uncited"),
        claims_policy=cited.get("claims_policy"),
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
                           "unsupported": None, "claims": None, "entities": None}
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
        gaps.append(f"报告「{node.title}」的文档没通过复核：{broken[0]}{more}")
    unknown, unverified = suspicious["unknown_entity"], suspicious["unverified_entity"]
    if unknown and governed:
        # 用户拍板：可疑实体在受管级别的正式出具里按「有缺口」降档，不拦截——反引号里的一个业务词
        # 也可能被当成名字，为它不予出具太重；探索运行、已发布级别只标注
        names = "".join(f"「{n}」" for n in dict.fromkeys(u["name"] for u in unknown[:5]))
        more = "等" if len(unknown) > 5 else ""
        gaps.append(f"报告「{node.title}」里有 {len(unknown)} 处可疑实体（{names}{more}）：本次运行的表结构、查询、"
                    "结果列里都没有这些名字，可能是编造的")
    if unknown or unverified:
        # 表结构快照不全时核对不了的名字（unverified）在哪个级别都只标注
        out["entities"] = {"unknown": unknown[:_LISTED], "unverified": unverified[:_LISTED],
                           "counted": governed and bool(unknown)}

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
        gaps.append(f"成果里没有哪个字段是报告「{node.title}」的原文：读者看到的内容没有经过引用核对")
    return out


#: 没挂依据的结论句怎么处置，从松到严。认不出的写法按 degrade 算：写错一个词不能让缺口悄悄消失
_ON_UNCITED = ("ignore", "degrade", "withhold")
#: 证据不支持的结论句怎么处置（claims: judge），从松到严。认不出的写法同样按 degrade 算
_ON_UNSUPPORTED = ("degrade", "withhold")
#: 要求结论句挂依据的策略：judge 在这一点上和 require_citation 一样严，另外再按裁判的判定判档
_CITING = ("require_citation", "judge")


def _claims(out: dict[str, Any], contract: dict[str, Any], payload: dict[str, Any], *, evidence_on: bool,
            governed: bool = False, uncited: list[dict[str, Any]], doc: dict[str, Any] | None = None,
            title: str = "") -> None:
    """结论句策略：没挂依据的、裁判判为证据不支持的结论句按 claims_policy 交给 decide_tier 判档。

    两处可以要求挂依据：报告节点写作时记下的 claims（节点产出里的，写作提示按它要求过），和契约自己写的
    require_citation / judge（对象形式可以另写 on_uncited）。两处都写了取更严的那个——契约只能收紧、不能放松：
    报告节点要求了，契约写 ignore 或 off 也照旧计入缺口，写 withhold 就收紧成不予出具。受管级别的正式运行
    里 ignore 一律当 degrade：没有哪道门禁查契约里的 claims，不能让它把用户要的底线拉低。

    报告节点写的是 judge 时，判定在文档里（按哈希取回的那份：判定是模型给的，出口复算不了，只能信封存的
    文档）。正式运行在节点里判过：证据不支持的按 on_unsupported 判档（报告节点和契约取更严的），没判完的
    （裁判没跑成、触顶）把裁判摘要里的缺口照抄进来——不能判完整出具，也不当成「不支持」。探索运行按需裁判，
    这里只标注。契约要 judge、报告节点却没开的，照实记缺口：出口替不了报告节点去裁判。

    数的是这里重新核对出来的 uncited，不信文档里记的 cites。升级前写的报告（节点产出里没有 claims 这个键，
    包括跨着升级还没跑完的运行）一律不管，契约写了也照旧记「这一版还不支持」。
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
        out["gaps"].append("契约声明了结论句检查（claims），这一版还不支持，结论句没有核对")
    judged = upgraded and written == "judge"
    if upgraded and name == "judge" and not judged:
        out["gaps"].append(f"契约要求结论句由模型裁判（claims: judge），报告「{title}」没有开 claims: judge，"
                           "结论句没有裁判")
    if not asks:
        return
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

    summary = (doc or {}).get("judge")
    if not isinstance(summary, dict):
        # 报告节点开了 judge、文档里却没有裁判摘要（不该发生）：当作没判，不能判完整出具
        summary = {"mode": "inline", "counts": {}, "unjudged": {}, "limits_hit": [], "complete": False,
                   "gaps": ["开了 claims: judge，文档里却没有裁判结果，结论句没有裁判"]}
    asked = [summary.get("on_unsupported")]
    if name == "judge" and isinstance(declared, dict):
        asked.append(declared.get("on_unsupported"))
    on_unsupported = max((a if a in _ON_UNSUPPORTED else "degrade" for a in asked), key=_ON_UNSUPPORTED.index)
    inline = summary.get("mode") == "inline"
    flagged: dict[str, list[dict[str, Any]]] = {"unsupported": [], "partial": []}
    if inline:
        # 探索运行按需裁判的判定不在文档里（在封存之后追加的 evidence.judged 事件里），这里只数正式运行的
        markdown = str((doc or {}).get("markdown") or "")
        for _, unit in iter_units(doc or {}):
            verdict = unit.get("verdict")
            status = verdict.get("status") if isinstance(verdict, dict) else None
            if status in flagged:
                span = unit.get("span") or [0, 0]
                flagged[status].append({"unit": unit.get("id"), "span": span, "text": markdown[span[0]:span[1]],
                                        "rationale": verdict.get("rationale") or ""})
        out["gaps"].extend(f"报告「{title}」{gap}" for gap in summary.get("gaps") or [])
    counts = {k: int((summary.get("counts") or {}).get(k) or 0)
              for k in ("supported", "partial", "unsupported", "not_a_claim", "unjudged")}
    out["claims_policy"] = {"policy": "judge", "on_unsupported": on_unsupported, "on_uncited": on_uncited}
    out["uncited"], out["unsupported"] = uncited, flagged["unsupported"]
    out["claims"] = {
        "policy": "judge", "uncited_claims": len(uncited), "uncited": uncited[:_LISTED], **extra,
        "on_unsupported": on_unsupported, "mode": "inline" if inline else "on_demand", "counts": counts,
        "unsupported": flagged["unsupported"][:_LISTED], "partial": flagged["partial"][:_LISTED],
        "unjudged": dict(summary.get("unjudged") or {}), "limits_hit": list(summary.get("limits_hit") or []),
        "complete": bool(summary.get("complete")), "model": summary.get("model"),
    }


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
        # 结论句策略和可疑实体：只在用上时才有这两个键，升级前的运行和没用上的契约形状不变
        **({"claims": cited["claims"]} if cited.get("claims") else {}),
        **({"entities": cited["entities"]} if cited.get("entities") else {}),
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
            raise NodeError(ctx.node.id, "整形节点没有填表达式")
        try:
            output = eval_expression(expr, tctx)
        except ExpressionError as e:
            raise NodeError(ctx.node.id, f"表达式错误：{e}") from e
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
        raise NodeError(ctx.node.id, f"未知的整形模式：{mode}")

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


TEMPLATE_HINT = "检查模板里的引号、逗号，字符串值要用 | json 过滤器输出"
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
    fix = ("让模型交结构化数据请给它配 output_schema；数要进口径卡的，改用 agent 的 output_schema + cite_fields"
           if node.type == NodeType.LLM else
           "让模型交结构化数据请用 output_schema + cite_fields，别用整形节点解析它写的文字")
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
                return f"模板里 {{{{ {expr} }}}} 取出来是空的，这里缺一个值：检查路径写对没有、前面的节点有没有产出"
            continue
        if pos > end and not at_end:
            continue
        quoted = _in_json_string(doc, start)
        if "json" in filters:
            # 过了 | json 就是一个合法的 JSON 值：错不在它的内容里，除非外面又套了一层引号
            return (f"模板里 {{{{ {expr} }}}} 出来的已经是合法的 JSON（文字自带引号），外面不要再加引号"
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
    return f"模板里 {{{{ {expr} }}}} 是{what}，放进 JSON 要写成 {{{{ {expr} | json }}}}（外面不要再加引号）"


def _why(text: str, at_end: bool) -> str:
    """上游写的 JSON 为什么解析不了，按看得出来的实情说。"""
    body = text.lstrip()
    if body.startswith("```"):
        return "它把 JSON 包在了 ``` 代码块里"
    if at_end and _unclosed(text):
        return "它写的 JSON 没有收尾，可能写到一半被截断了"
    if not body.startswith(("{", "[")):
        return "它写的是一段文字，不是 JSON"
    return "常见原因是字符串里有没转义的英文引号"


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
