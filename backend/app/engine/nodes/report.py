"""报告撰写节点：模型只写引用标记，数字由系统从口径卡取出来渲染。

以前「给人看的报告」是 llm 节点写的自由文本：数字是模型转写的，出处要到出口再按
数值回头猜——同值的指标一多就猜错，模型顺手写的数也要到出口才被发现。这里把核对
挪到写的那一刻：

1. 建目录：收齐上游口径卡的指标和运行输入，编成 [[m:gmv]]、[[i:week]] 这样的引用
2. 流式写作：模型写标记，StreamRenderer 边收边换成真值，流里看到的就是最终的数字
3. 核对：compose_doc 切块、分句、切片段，找出裸数字和解析不了的引用
4. 修复：有违规就把清单交回去重写，最多 max_repairs 次
5. 收尾：文档落成 report_doc 工件，report.checked 事件进封存；仍然违规时按
   on_violation 失败（fail）或者照常产出、把违规片段标出来（flag）

三期起目录里还有表和字段（这次运行冻结的表结构）、知识库检索的原话。可疑实体（反引号里
写了个哪里都找不到的名字）只标注：不重写、不失败，受管级别的正式出具按缺口降档；表结构快照
不全时核对不了的名字（unverified_entity）只标注。entities: off 整层不管表名、字段名。
结论句策略 claims：off（默认）不管；require_citation 在写作提示里要求结论句挂依据（自动链接的
名字不算依据），没挂的计入缺口、出口降档；judge（模型裁判）是后续版本的事，现在写了直接报错。

出口契约复核（io.py）用 report_catalog 按同一套参数重建目录、独立重算，不信这里的自查。
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.core.errors import describe_exception
from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import (
    CELLS_REASON,
    CLAIMS_RULE,
    ENTITY_KINDS,
    MARKER_RULES,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    describe_violations,
    ledger_enabled,
)
from app.engine.nodes.llm import _invoke_streaming, _model_spec, _report_call
from app.engine.schema import REPORT_CLAIMS, GraphNode, GraphSpec, NodeType, _ancestors, claims_problem
from app.engine.state import GraphState, message_text, thinking_text
from app.engine.toolcalls import leaked_markup, markup_warning
from app.providers.factory import ProviderNotConfigured, get_chat_model

ON_VIOLATION = ("fail", "flag")
NUMBERS = ("strict", "off")
#: 实体层：link（默认）核对表名、字段名并自动链接；off 整层不管（目录里不收表和字段）
ENTITIES = ("link", "off")
#: 结论句策略，和画布校验同一份（schema.REPORT_CLAIMS）；judge 是后续版本的事
CLAIMS = REPORT_CLAIMS
#: 只标注、不让写作者重写也不判失败的违规：可疑实体（受管的正式出具按缺口降档）、表结构快照不全时
#: 核对不了的名字（出口只标注）
SOFT = frozenset({"unknown_entity", "unverified_entity"})
#: 重写一次就是多一次整篇的模型调用，写不对的模型多给几次也大多写不对
MAX_REPAIRS = 3

ROLE = ("你是报告撰写人。报告里的每个数字都由系统从证据里取出来、按口径卡的格式渲染；"
        "你负责挑重点、组织结构、说清变化和原因。")

MARKUP_NUDGE = ("你上一条回复把工具调用写成了文字。报告撰写不调用任何工具：能用的数据都在上面的"
                "证据目录里，直接用引用标记写报告正文。")
MARKUP_ERROR = ("模型输出了工具调用的原始标记，而报告撰写节点不调用工具——数据已经在证据目录里。"
                "换一个模型再试")


# --------------------------------------------------------------------------
# 目录与放行名单：出口复核要用同一套参数重建，所以不依赖 NodeContext
# --------------------------------------------------------------------------


def _setting(spec: GraphSpec, node: GraphNode, key: str, default: Any = None) -> Any:
    """和 NodeContext.cfg 同一个规则：节点上没写就取图级 defaults。"""
    value = node.config.get(key)
    if value in (None, ""):
        return spec.defaults.get(key, default)
    return value


def report_catalog(state: GraphState, spec: GraphSpec, node: GraphNode) -> dict[str, Any]:
    """报告节点的证据目录。

    出口契约复核必须用这一个函数重建：参数（metrics_from、evidence_from、祖先范围）
    差一点，alias 集合就不一样——同一个指标 id 在两张卡里时，只收一张写 m:gmv，
    全收就得写 m:east.gmv——复核会把一份好好的报告判成引用解析不了。
    """
    ledger = [e for e in state.get("evidence") or [] if isinstance(e, dict)]
    sources = _setting(spec, node, "evidence_from", "ancestors")
    if isinstance(sources, str) and sources != "ancestors":
        sources = [sources]
    if isinstance(sources, list):
        # evidence_from 只管查询、检索这类证据（连同查询当时的表结构）从哪几个节点来；指标从哪来由 metrics_from 管
        keep = {str(s) for s in sources}
        ledger = [e for e in ledger
                  if e.get("kind") not in ("query", "retrieval", "schema") or e.get("node_id") in keep]
    catalog = build_catalog(
        nodes=state.get("nodes") or {},
        ledger=ledger,
        inputs=state.get("input") or {},
        metrics_from=_setting(spec, node, "metrics_from") or None,
        allowed=_ancestors(spec, node.id),
    )
    if report_entities(spec, node) == "off":
        # 目录里没有表和字段，写作目录也就不列；compose / verify 另传 entities=False，解析不了的原因照实说
        catalog = {alias: e for alias, e in catalog.items() if e.get("kind") not in ENTITY_KINDS}
    return catalog


def report_entities(spec: GraphSpec, node: GraphNode) -> str:
    """报告节点的实体层：link（默认）/ off。出口复核调 verify_doc 时传 entities=(这个值 == "link")。

    写错的值执行时节点会报错，这里只把 off 当 off，别的一律按默认。
    """
    return "off" if _setting(spec, node, "entities", "link") == "off" else "link"


def report_claims(spec: GraphSpec, node: GraphNode) -> str:
    """报告节点的结论句策略：off / require_citation。出口判档按它（和节点执行时同一个取值规则）。

    写错的值（包括 judge）画布校验就拦了，执行时节点也会报错，这里只把没写当成 off。
    """
    value = _setting(spec, node, "claims", "off")
    return value if isinstance(value, str) and value in CLAIMS else "off"


def report_allowance(spec: GraphSpec, node: GraphNode) -> list[Any]:
    """报告自查放行的字面量：出口契约 report_from 指向它时，契约的 allow_numbers。

    契约放行、报告却不放行的话，写作者会为一个最终根本不算违规的数白白重写一次。
    反过来不成立：放行名单只由契约定，报告节点没有自己的名单——出口复核是独立的裁判。
    """
    allowed: list[Any] = []
    for other in spec.nodes:
        contract = other.config.get("contract") if other.type == NodeType.OUTPUT else None
        if isinstance(contract, dict) and contract.get("report_from") == node.id:
            extra = contract.get("allow_numbers") or []
            allowed.extend(extra if isinstance(extra, list) else [extra])
    return allowed


def report_cells_allowed(spec: GraphSpec, node: GraphNode, *, governed: bool) -> bool:
    """报告能不能直接引用查询单元格：出口契约的 report_from 指着它、而那份契约不允许的，写作时就不给。

    出口复核按契约判（io.py），这里提前照同一条规则建目录：不然写作者照着提示写了单元格、自查
    通过，到出口才整段判成解析不了——受管出具直接不予出具。
    """
    from app.engine.governance import cells_allowed

    contracts = [other.config.get("contract") for other in spec.nodes if other.type == NodeType.OUTPUT]
    return all(cells_allowed(c, governed=governed) for c in contracts
               if isinstance(c, dict) and c.get("report_from") == node.id)


#: 目录里能被报告引用的几种来源（沙箱代码节点的登记只为说清为什么不能引用，不算）
_CITABLE = {"metric": "口径卡指标", "query": "查询结果", "retrieval": "知识库检索", "input": "运行输入"}


def _no_evidence(catalog: dict[str, Any], cells: bool) -> str | None:
    """目录里没有可引用的来源时给一句话，说清缺的是哪种；有就返回 None。"""
    kinds = {entry.get("kind") for entry in catalog.values()}
    if not kinds & set(_CITABLE):
        return ("报告撰写节点的上游没有可引用的来源：" + "、".join(_CITABLE.values()) + "都没有，"
                "报告里的数字都会被判为没有出处。在它前面接取数节点或口径卡，或者检查 metrics_from / evidence_from")
    if not cells and not kinds & {"metric", "input"}:
        # 只有查询和检索：单元格引用不了，检索片段本来就只能当依据，一个数都没处引
        return (f"报告撰写节点的上游只有查询结果、没有口径卡指标，而这次{CELLS_REASON}：单元格引用不了，"
                "报告里的数字都会被判为没有出处。在出口契约里写 \"cells\": true，或者把要写的数登记进口径卡")
    return None


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class _RenderedStream:
    """借 _invoke_streaming 的流式与回退逻辑，只在出口把 llm.token 换成渲染后的字。

    模型写的是 [[m:gmv]]，前端要看到的是「45,678.5元」。标记会被切在两个 chunk
    中间，StreamRenderer 攒到没有未闭合的 [[ 再放；调用结束时 flush 掉剩下的。
    """

    def __init__(self, ctx: NodeContext, catalog: dict[str, Any], cells: bool = True) -> None:
        self._ctx = ctx
        self._catalog = catalog
        self._cells = cells
        self._renderer = StreamRenderer(catalog, cells_allowed=cells)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    def emit(self, event_type: EventType | str, **data: Any) -> None:
        if event_type == EventType.LLM_TOKEN:
            if text := self._renderer.feed(str(data.get("delta") or "")):
                self._ctx.emit(EventType.LLM_TOKEN, delta=text)
            return
        if event_type == EventType.LOG and data.get("code") == "stream_fallback":
            # 流断在半路、退回非流式重来：攒着的半截不再放出去，免得和重来的那份接在一起
            self._renderer = StreamRenderer(self._catalog, cells_allowed=self._cells)
        self._ctx.emit(event_type, **data)

    def flush(self) -> None:
        if tail := self._renderer.flush():
            self._ctx.emit(EventType.LLM_TOKEN, delta=tail)


async def _default_on_violation(run_id: str) -> str:
    """探索运行默认标注、照常产出；正式运行默认失败——封存的发布版不能带着裸数字出具。"""
    from app.db.models import Run

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    return "fail" if run is not None and run.run_class == "formal" else "flag"


def _options(ctx: NodeContext) -> tuple[str, int]:
    numbers = str(ctx.cfg("numbers", "strict") or "strict")
    if numbers not in NUMBERS:
        raise NodeError(ctx.node.id, f"numbers 只能是 {' / '.join(NUMBERS)}，写的是 {numbers!r}")
    # 画布校验也拦这两项；执行时再守一道，报的是同一句话，免得写错的值被悄悄当成默认
    if problem := claims_problem(ctx.cfg("claims")):
        raise NodeError(ctx.node.id, problem)
    entities = ctx.cfg("entities", "link") or "link"
    if entities not in ENTITIES:
        raise NodeError(ctx.node.id, f"entities 只能是 {' / '.join(ENTITIES)}，写的是 {entities!r}")
    raw = ctx.cfg("max_repairs", 1)
    try:
        repairs = int(raw)
    except (TypeError, ValueError):
        raise NodeError(ctx.node.id, f"max_repairs 要写 0 到 {MAX_REPAIRS} 的整数，写的是 {raw!r}") from None
    if isinstance(raw, bool) or not 0 <= repairs <= MAX_REPAIRS:
        raise NodeError(ctx.node.id, f"max_repairs 要写 0 到 {MAX_REPAIRS} 的整数，写的是 {raw!r}")
    return numbers, repairs


def _blocking(violations: list[dict[str, Any]], numbers: str) -> list[dict[str, Any]]:
    """要让写作者改的违规。numbers=off 时裸数字只记录不拦（文档和出口复核照样看得到）。

    可疑实体只标注（SOFT）：一个反引号里的业务词也可能被当成名字，为它整篇重写、判失败都不值；
    它留在文档和出口复核里，受管的正式出具按缺口降档。表结构快照不全时核对不了的名字同样只标注。
    """
    return [v for v in violations if v.get("code") not in SOFT
            and not (numbers == "off" and v.get("code") == "uncited_number")]


def _named(violations: list[dict[str, Any]], limit: int = 3) -> str:
    """违规里点名的那几个字：「45678」「m:nope」。"""
    names = [str(v.get("text") or v.get("ref") or "") for v in violations]
    names = [n for n in dict.fromkeys(names) if n][:limit]
    return "、".join(f"「{n}」" for n in names)


def _repair_request(violations: list[dict[str, Any]]) -> str:
    return (
        "你写的报告没有通过系统核对，问题如下：\n"
        f"{describe_violations(violations)}\n\n"
        "请重写整篇报告：所有数字都用引用标记写，只引用证据目录里有的指标和输入；"
        "不要解释改了什么，只输出新的报告正文。"
    )


def _messages(ctx: NodeContext, state: GraphState, catalog: dict[str, Any], cells: bool = True,
              claims: str = "off") -> list[BaseMessage]:
    system = ctx.render_str(ctx.cfg("system", ""), state)
    instructions = ctx.render_str(ctx.cfg("instructions", ""), state).strip() \
        or "根据下面的证据写一份简洁的报告，先总后分。"
    rules = f"{MARKER_RULES}\n{CLAIMS_RULE}" if claims == "require_citation" else MARKER_RULES
    return [
        SystemMessage(content="\n\n".join(p for p in (system, ROLE) if p)),
        HumanMessage(content=(
            f"{instructions}\n\n{catalog_prompt(catalog, cells_allowed=cells)}\n\n{rules}\n\n"
            "只输出报告正文（Markdown），不要写撰写说明。"
        )),
    ]


async def _store(doc: dict[str, Any], ctx: NodeContext) -> str | None:
    from app.core.artifact_store import put_json

    try:
        return await put_json(doc, kind="report_doc", run_id=ctx.run.run_id, node_id=ctx.node.id,
                              meta={"stats": doc.get("stats") or {}})
    except Exception as e:  # noqa: BLE001 - 工件写失败不毁掉运行，只是这份报告点不开证据
        ctx.emit(EventType.LOG, level="warn", code="evidence_store_failed",
                 message=f"报告文档没能落进工件库（{describe_exception(e)}），报告里的数字点不开证据")
        return None


async def run_report(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    numbers, max_repairs = _options(ctx)
    on_violation = str(ctx.cfg("on_violation", "") or "") or await _default_on_violation(ctx.run.run_id)
    if on_violation not in ON_VIOLATION:
        raise NodeError(ctx.node.id, f"on_violation 只能是 {' / '.join(ON_VIOLATION)}，写的是 {on_violation!r}")

    async with SessionLocal() as session:
        try:
            model, model_id = await get_chat_model(session, _model_spec(ctx))
        except ProviderNotConfigured as e:
            raise NodeError(ctx.node.id, str(e)) from e

    spec = ctx.run.spec
    catalog = report_catalog(state, spec, ctx.node)
    allow = report_allowance(spec, ctx.node)
    cells = True
    # 结论句策略只对升级后发起的运行生效：升级前的运行（图里也不会有 claims）事件、产出一个字不加
    evidence_on = ledger_enabled(ctx.run)
    claims = report_claims(spec, ctx.node) if evidence_on else "off"
    if evidence_on:
        # 升级前发起的运行没有查询条目，单元格无从谈起，也就不多查这一次
        from app.engine.governance import governed_formal

        cells = report_cells_allowed(spec, ctx.node, governed=await governed_formal(ctx.run.run_id))
    if warning := _no_evidence(catalog, cells):
        ctx.emit(EventType.LOG, level="warn", code="report_no_evidence", message=warning)

    messages = _messages(ctx, state, catalog, cells, claims)
    started = time.perf_counter()
    spent: list[dict[str, Any]] = []
    nudged = False

    async def draft(payload: list[BaseMessage]) -> tuple[BaseMessage, str]:
        nonlocal nudged
        stream = _RenderedStream(ctx, catalog, cells)
        began = time.perf_counter()
        response = await _invoke_streaming(model, payload, stream, model_id)  # type: ignore[arg-type]
        stream.flush()
        spent.append(_report_call(ctx, response, model_id, began))
        text = message_text(response)
        if snippet := leaked_markup(text):
            # 写成了一段工具调用标记：纠正一次，还这样就判失败，别把标记当报告往下游送
            if nudged:
                raise NodeError(ctx.node.id, MARKUP_ERROR)
            nudged = True
            ctx.emit(EventType.LOG, level="warn", code="tool_markup_leak",
                     message=f"{markup_warning(snippet)}，已提醒它重试一次")
            return await draft([*payload, response, HumanMessage(content=MARKUP_NUDGE)])
        if not text and thinking_text(response):
            ctx.emit(EventType.LOG, level="warn", code="empty_completion",
                     message="模型只输出了思考内容，报告正文为空。把 max_tokens 调大，或把 thinking 设为 off。")
        return response, text

    entities = report_entities(spec, ctx.node) == "link"

    def compose(raw: str) -> dict[str, Any]:
        return compose_doc(raw, catalog, node_id=ctx.node.id, run_id=ctx.run.run_id, allow_numbers=allow,
                           cells_allowed=cells, entities=entities)

    response, raw = await draft(messages)
    doc = compose(raw)
    repairs = 0
    while (blocking := _blocking(doc["violations"], numbers)) and repairs < max_repairs:
        repairs += 1
        ctx.emit(EventType.LOG, level="warn", code="report_repair",
                 message=f"报告里有 {len(blocking)} 处没通过核对（{_named(blocking)}），"
                         f"已要求写作者重写（第 {repairs} 次）")
        messages = [*messages, response, HumanMessage(content=_repair_request(blocking))]
        response, raw = await draft(messages)
        doc = compose(raw)

    doc_artifact = await _store(doc, ctx)
    ctx.emit(EventType.REPORT_CHECKED, doc_artifact=doc_artifact, ok=not doc["violations"],
             stats=doc["stats"], violations=doc["violations"][:20], repairs=repairs,
             on_violation=on_violation, failed=bool(blocking) and on_violation == "fail",
             **({"claims": claims} if evidence_on else {}))
    if blocking and on_violation == "fail":
        head = "；".join(v["message"] for v in blocking[:5])
        more = f"；…另有 {len(blocking) - 5} 处" if len(blocking) > 5 else ""
        raise NodeError(
            ctx.node.id,
            f"报告有 {len(blocking)} 处没通过核对（已要求重写 {repairs} 次）：{head}{more}。"
            "换一个遵从度更高的模型，或者先把 on_violation 设成 flag，把问题标注在报告里再看",
        )

    usage = {key: sum(one[key] for one in spent) for key in spent[0]}
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    text = doc["markdown"]
    result = {
        "text": text,
        "doc_artifact": doc_artifact,
        "stats": doc["stats"],
        "violations": doc["violations"][:20],
        "repairs": repairs,
        "model": model_id,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        **({"claims": claims} if evidence_on else {}),
    }
    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}, "usage": usage}
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=text or "(空)")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: text}
    return updates
