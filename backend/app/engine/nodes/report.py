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
名字不算依据），没挂的计入缺口、出口降档；judge 另外请一个模型按证据逐句判断（engine/judge.py）：

- 正式运行在节点里判：写完、核对完之后判，判定写进文档的 unit.verdict，随文档一起封存。判定只标注、
  不改写正文；裁判没跑成、触顶了照样产出，没判的记未裁判并留下缺口，出口按 on_unsupported 判档
- 探索运行不在节点里花这笔钱：候选句标「未裁判 · 按需」，点开哪句再判哪句（证据接口）
- rewrite_once（默认关）：证据不支持的句子连同理由交回写作者只改这几句，再判一次，最多一轮；
  改写稿冒出原稿没有的违规（只标注的可疑实体也算）就不采用
- 裁判调用包在 @task 里，排在节点所有已有调用之后：「接着跑」时直接取 checkpoint 里的判定，
  不重复调用、不重复计费。写作调用不进 checkpoint，接着跑时会重写一稿——所以判定带着输入指纹，
  对不上这一稿的不用

出口契约复核（io.py）用 report_catalog 按同一套参数重建目录、独立重算，不信这里的自查。
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.func import task

from app.core.errors import describe_exception, not_configured
from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine import judge as judging
from app.engine.context import NodeContext, NodeError
from app.engine.labels import field_label, option_label
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
from app.engine.schema import (
    CLAIMS_JUDGE,
    CLAIMS_VALUES,
    JUDGE_ON_UNSUPPORTED,
    GraphNode,
    GraphSpec,
    NodeType,
    _ancestors,
    claims_problem,
    judge_problems,
)
from app.engine.state import GraphState, message_text, thinking_text
from app.engine.toolcalls import leaked_markup, markup_warning
from app.providers.factory import ProviderNotConfigured, get_chat_model

ON_VIOLATION = ("fail", "flag")
NUMBERS = ("strict", "off")
#: 实体层：link（默认）核对表名、字段名并自动链接；off 整层不管（目录里不收表和字段）
ENTITIES = ("link", "off")
#: 结论句策略，和画布校验同一份（schema.CLAIMS_VALUES）：off / require_citation / judge
CLAIMS = CLAIMS_VALUES
#: 只标注、不让写作者重写也不判失败的违规：可疑实体（受管的正式出具按缺口降档）、表结构快照不全时
#: 核对不了的名字（出口只标注）
SOFT = frozenset({"unknown_entity", "unverified_entity"})
#: 重写一次就是多一次整篇的模型调用，写不对的模型多给几次也大多写不对
MAX_REPAIRS = 3

ROLE = ("你是报告撰写人。报告里的每个数字都由系统从证据里取出来、按口径卡的格式渲染；"
        "你负责挑重点、组织结构、说清变化和原因。")

MARKUP_NUDGE = ("你上一条回复把工具调用写成了文字。报告撰写不调用任何工具：能用的数据都在上面的"
                "证据目录里，直接用引用标记写报告正文。")
#: 节点失败原因（给人看）。给模型的那句是上面的 MARKUP_NUDGE，「证据目录」是模型那边的说法
MARKUP_ERROR = ("模型输出了工具调用的原始标记，而报告撰写节点不调用工具，数据已由系统提供。"
                "请换一个模型重试")
#: 接着跑时 checkpoint 里缓存的判定最多翻几条去找这一稿的（每条都是先前某一稿的，对不上就往后翻）
_STALE_LIMIT = 16


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
    """报告节点的结论句策略：off / require_citation / judge。出口判档按它（和节点执行时同一个取值规则）。

    写错的值画布校验就拦了，执行时节点也会报错，这里只把没写当成 off。
    """
    value = _setting(spec, node, "claims", "off")
    return value if isinstance(value, str) and value in CLAIMS else "off"


def report_judge(spec: GraphSpec, node: GraphNode) -> dict[str, Any]:
    """claims: judge 的子配置：节点上的 judge（没写取图级 defaults），写了的键原样保留——上限写 null 就是
    不限，没写的键运行时取设置里的默认（judge.report_budget）。rewrite_once、on_unsupported 补上缺省
    （false、degrade）：出口判档读 on_unsupported。写错的值画布校验就拦了，执行时节点也会报错。
    """
    raw = _setting(spec, node, "judge")
    raw = raw if isinstance(raw, dict) else {}
    return {**raw, "rewrite_once": raw.get("rewrite_once") is True,
            "on_unsupported": raw.get("on_unsupported") if raw.get("on_unsupported") in JUDGE_ON_UNSUPPORTED
            else "degrade"}


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
        return ("报告撰写节点的上游没有可引用的来源（" + "、".join(_CITABLE.values()) + "都没有），"
                f"报告中的数字都会被判为没有出处。请在它前面接入取数节点或口径卡，或检查「{field_label('metrics_from')}」")
    if not cells and not kinds & {"metric", "input"}:
        # 只有查询和检索：单元格引用不了，检索片段本来就只能当依据，一个数都没处引
        return (f"报告撰写节点的上游只有查询结果，没有口径卡指标，而{CELLS_REASON}，当前无法引用单元格，"
                "报告中的数字都会被判为没有出处。请在出具契约中开启「单元格引用」，或把要写的数字登记到口径卡")
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


async def _run_class(run_id: str) -> str | None:
    """按运行记录上的 run_class 取：子图、审批之后续跑都是另起的执行上下文，只有运行记录靠得住。"""
    from app.db.models import Run

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    return run.run_class if run is not None else None


async def _default_on_violation(run_id: str) -> str:
    """探索运行默认标注、照常产出；正式运行默认失败——封存的发布版不能带着裸数字出具。"""
    return "fail" if await _run_class(run_id) == "formal" else "flag"


def _options(ctx: NodeContext) -> tuple[str, int]:
    numbers = str(ctx.cfg("numbers", "strict") or "strict")
    if numbers not in NUMBERS:
        raise NodeError(ctx.node.id, _only("numbers", NUMBERS, numbers))
    # 画布校验也拦这两项；执行时再守一道，报的是同一句话，免得写错的值被悄悄当成默认
    if problem := claims_problem(ctx.cfg("claims")):
        raise NodeError(ctx.node.id, problem)
    if ctx.cfg("claims") == CLAIMS_JUDGE and (problems := judge_problems(ctx.cfg("judge"))):
        raise NodeError(ctx.node.id, "；".join(message for _, message in problems))
    entities = ctx.cfg("entities", "link") or "link"
    if entities not in ENTITIES:
        raise NodeError(ctx.node.id, _only("entities", ENTITIES, entities))
    raw = ctx.cfg("max_repairs", 1)
    try:
        repairs = int(raw)
    except (TypeError, ValueError):
        raise NodeError(ctx.node.id, _repairs_problem(raw)) from None
    if isinstance(raw, bool) or not 0 <= repairs <= MAX_REPAIRS:
        raise NodeError(ctx.node.id, _repairs_problem(raw))
    return numbers, repairs


def _only(key: str, allowed: tuple[str, ...], got: Any) -> str:
    """「「表名、字段名」只能是「核对」或「不核对」，当前为「x」」：写界面上的叫法，不写键名和枚举值。"""
    names = [f"「{option_label(key, v)}」" for v in allowed]
    listed = "或".join(names) if len(names) <= 2 else "、".join(names[:-1]) + "或" + names[-1]
    return f"「{field_label(key, 'report')}」只能是{listed}，当前为「{got}」"


def _repairs_problem(raw: Any) -> str:
    return f"「{field_label('max_repairs', 'report')}」需要填写 0 到 {MAX_REPAIRS} 的整数，当前为「{raw}」"


def _blocking(violations: list[dict[str, Any]], numbers: str) -> list[dict[str, Any]]:
    """要让写作者改的违规。numbers=off 时裸数字只记录不拦（文档和出口复核照样看得到）。

    可疑实体只标注（SOFT）：一个反引号里的业务词也可能被当成名字，为它整篇重写、判失败都不值；
    它留在文档和出口复核里，受管的正式出具按缺口降档。表结构快照不全时核对不了的名字同样只标注。
    """
    return [v for v in violations if v.get("code") not in SOFT
            and not (numbers == "off" and v.get("code") == "uncited_number")]


def _violation_key(violation: dict[str, Any]) -> tuple[str, str]:
    """认一处违规：(code, 点名的字)。点名的字按 text、ref、message 的顺序取第一个有的。"""
    named = violation.get("text") or violation.get("ref") or violation.get("message") or ""
    return str(violation.get("code") or ""), str(named)


def _new_violations(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """after 里 before 没有的违规，按 (code, 点名的字) 逐个抵消（同一个裸数字多写一处也算新的）。

    全部违规都比，包括只标注、不让重写的那几类（SOFT）：改写稿冒出一个编造的表名，受管的正式出具就会
    因此降档；去掉一处、又冒出另一处，总数没变，冒出来的也还是新的。
    """
    left = Counter(_violation_key(v) for v in before)
    fresh: list[dict[str, Any]] = []
    for violation in after:
        key = _violation_key(violation)
        if left[key] > 0:
            left[key] -= 1
        else:
            fresh.append(violation)
    return fresh


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
    rules = MARKER_RULES
    if claims in ("require_citation", CLAIMS_JUDGE):
        # 裁判只看写作者挂的依据：要判，就更得每句都挂上
        rules = f"{rules}\n{CLAIMS_RULE}" + (f"\n{judging.JUDGE_RULE}" if claims == CLAIMS_JUDGE else "")
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
                 message=f"报告文档保存失败（{describe_exception(e)}），报告中的数字将无法查看证据")
        return None


async def run_report(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    numbers, max_repairs = _options(ctx)
    on_violation = str(ctx.cfg("on_violation", "") or "") or await _default_on_violation(ctx.run.run_id)
    if on_violation not in ON_VIOLATION:
        raise NodeError(ctx.node.id, _only("on_violation", ("flag", "fail"), on_violation))

    async with SessionLocal() as session:
        try:
            model, model_id = await get_chat_model(session, _model_spec(ctx))
        except ProviderNotConfigured as e:
            raise NodeError(ctx.node.id, not_configured(e)) from e

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
                     message=f"{markup_warning(snippet)}，已要求模型重试一次")
            return await draft([*payload, response, HumanMessage(content=MARKUP_NUDGE)])
        if not text and thinking_text(response):
            ctx.emit(EventType.LOG, level="warn", code="empty_completion",
                     message="模型只输出了思考内容，报告正文为空。请调大「最大输出 token」，或关闭「思考模式」。")
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
                 message=f"报告有 {len(blocking)} 处未通过核对（{_named(blocking)}），"
                         f"已要求模型重写（第 {repairs} 次）")
        messages = [*messages, response, HumanMessage(content=_repair_request(blocking))]
        response, raw = await draft(messages)
        doc = compose(raw)

    judgement: dict[str, Any] | None = None
    if claims == CLAIMS_JUDGE:
        judge_cfg = report_judge(spec, ctx.node)
        if await _run_class(ctx.run.run_id) != "formal":
            # 探索运行默认按需裁判：点开哪句才判哪句，节点里不花这笔钱
            judgement = judging.on_demand(doc, on_unsupported=judge_cfg["on_unsupported"],
                                          rewrite_once=judge_cfg["rewrite_once"])
        elif not (blocking and on_violation == "fail"):
            # 反正要判失败的报告不值得再花钱判（节点紧接着就报错）
            doc, response, blocking, judgement, usages = await _judge_inline(
                ctx, doc, catalog, judge_cfg, model_id=model_id, messages=[*messages, response],
                draft=draft, compose=compose, numbers=numbers, blocking=blocking)
            spent.extend(usages)

    doc_artifact = await _store(doc, ctx)
    ctx.emit(EventType.REPORT_CHECKED, doc_artifact=doc_artifact, ok=not doc["violations"],
             stats=doc["stats"], violations=doc["violations"][:20], repairs=repairs,
             on_violation=on_violation, failed=bool(blocking) and on_violation == "fail",
             **({"claims": claims} if evidence_on else {}),
             **({"judge": judgement} if judgement is not None else {}))
    if blocking and on_violation == "fail":
        head = "；".join(v["message"] for v in blocking[:5])
        more = f"；…另有 {len(blocking) - 5} 处" if len(blocking) > 5 else ""
        raise NodeError(
            ctx.node.id,
            f"报告有 {len(blocking)} 处未通过核对（已要求重写 {repairs} 次）：{head}{more}。"
            f"请换用指令遵循能力更强的模型，或先将「{field_label('on_violation')}」设为"
            f"「{option_label('on_violation', 'flag')}」，查看标注后再调整",
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
        **({"judge": judgement} if judgement is not None else {}),
    }
    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}, "usage": usage}
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=text or "（空）")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: text}
    return updates


# --------------------------------------------------------------------------
# claims: judge——正式运行在节点里裁判
# --------------------------------------------------------------------------


def _rewrite_request(sentences: list[tuple[str, str]]) -> str:
    listed = "\n".join(f"{i}.「{text}」——{why or '证据不支持这句话'}" for i, (text, why) in enumerate(sentences, 1))
    return (
        "另一个模型按证据逐句核对了你的报告，下面这几句它认为证据不支持：\n"
        f"{listed}\n\n"
        "请只改这几句：补上证据目录里真有的依据 [[see:…]]，或者把说法收窄到证据能支持的程度，或者删掉；"
        "其余部分一字不改。数字仍然只能用引用标记写。不要解释改了什么，只输出改好的整篇报告正文。"
    )


def _merge(first: dict[str, Any], second: dict[str, Any], budget: judging.Budget) -> dict[str, Any]:
    """改写前后两轮裁判合成一份：判定、句数、缺口按改写后这一稿，花费、调用、用时两轮相加。"""
    merged = dict(second)
    for key in ("cost_usd", "estimated_usd"):
        merged[key] = round(first[key] + second[key], 6)
    for key in ("calls", "duration_ms"):
        merged[key] = first[key] + second[key]
    merged["notes"] = list(dict.fromkeys([*first["notes"], *second["notes"]]))
    merged["budget"] = budget.as_dict()
    merged["model"] = second.get("model") or first.get("model")
    merged["priced"] = first.get("priced")
    return merged


async def _judge_inline(ctx: NodeContext, doc: dict[str, Any], catalog: dict[str, Any], judge_cfg: dict[str, Any], *,
                        model_id: str, messages: list[BaseMessage], draft: Any, compose: Any, numbers: str,
                        blocking: list[dict[str, Any]]
                        ) -> tuple[dict[str, Any], BaseMessage, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """写完、核对完之后请裁判判一遍，判定写进文档。返回 (文档, 写作者最后一稿, 要拦的违规, 裁判摘要, 用量)。

    messages 是写作的对话、最后一条是写作者这一稿。rewrite_once 时把证据不支持的句子交回去只改这几句，
    再判一次（没改的句子直接用第一次的判定），最多一轮；改写稿冒出原稿没有的违规就不采用，保留原稿。

    摘要里的 rewrite = {units, sentences, applied, reason, changed}：
    - sentences：交回去的句子（原稿里的原文）
    - units：这几句在封存文档里的编号。没采用时封存的是原稿，就是原稿的编号；采用了封存的是改写稿，
      编号按改写稿重排，按原文认：原句原样留在改写稿里的给新编号，改掉了的是 null
    - changed：采用时改写稿里新出来的句子（原稿里没有这段原文）在封存文档里的编号；没采用是 []
    """
    from app.engine.replay import _in_graph

    async with SessionLocal() as session:
        settings = await judging.judge_settings(session)
        judge_spec = await judging.judge_model_spec(session, judge_cfg, settings=settings)
    budget = judging.report_budget(settings, judge_cfg)
    masks = await judging.source_masks(catalog)

    @task
    async def judge_step(request: judging.JudgeRequest, key: str, limits: judging.Budget, known: dict[str, Any],
                         first: bool) -> dict[str, Any]:
        # 裁判进 checkpoint：「接着跑」时直接取这里的判定，不再调用、不再计费（每日计数也记在这里面）。
        # 事件都在这里面发：取缓存时不会再发一遍。第二轮 limits 是剩下的钱和时间，说给人看的仍是配置的上限
        outcome = await judging.run_request(request, spec=judge_spec, budget=limits, known=known, emit=ctx.emit,
                                            shown=budget)
        if first and outcome.get("model") and outcome["model"] == model_id:
            outcome["same_model"] = True
            ctx.emit(EventType.LOG, level="warn", code="judge_same_model",
                     message=f"裁判模型与写作模型相同（「{model_id}」），难以发现写作模型自身的错误。"
                             "请在「设置 → 偏好设置 → 证据裁判」中选择另一个模型，"
                             f"或在节点的「{field_label('judge')}」中指定裁判模型")
        return {**outcome, "key": key}

    async def judge_round(round_doc: dict[str, Any], limits: judging.Budget, known: dict[str, Any],
                          first: bool) -> dict[str, Any]:
        request = judging.prepare(round_doc, catalog, masked=masks)
        key = judging.request_key(request, judge_spec, limits, known)
        if _in_graph():
            # 写作不进 checkpoint，接着跑时写作者会重写一稿：缓存里先前那一稿的判定对不上指纹，往后翻
            for _ in range(_STALE_LIMIT):
                outcome = await judge_step(request, key, limits, known, first)
                if outcome.get("key") == key:
                    return outcome
        return await judging.run_request(request, spec=judge_spec, budget=limits, known=known, emit=ctx.emit,
                                         shown=budget)

    outcome = await judge_round(doc, budget, {}, True)
    same_model = bool(outcome.get("same_model"))
    usages = [outcome["usage"]]
    response = messages[-1]
    rewrite: dict[str, Any] | None = None
    bad = [uid for uid, v in outcome["verdicts"].items() if v["status"] == "unsupported"]
    if judge_cfg["rewrite_once"] and bad:
        texts = judging.unit_texts(doc)
        sentences = [(texts.get(uid, ""), outcome["verdicts"][uid].get("rationale") or "") for uid in bad]
        ctx.emit(EventType.LOG, level="warn", code="report_rewrite",
                 message=f"裁判认为 {len(bad)} 句结论证据不支持（{_named([{'text': t} for t, _ in sentences])}），"
                         "已退回写作模型仅修改这几句")
        again, raw = await draft([*messages, HumanMessage(content=_rewrite_request(sentences))])
        rewritten = compose(raw)
        worse = _blocking(rewritten["violations"], numbers)
        fresh = _new_violations(doc["violations"], rewritten["violations"])
        rewrite = {"units": bad, "sentences": [t for t, _ in sentences], "applied": False, "reason": None,
                   "changed": []}
        if fresh:
            rewrite["reason"] = (f"改写稿新增了 {len(fresh)} 处原稿没有的问题（{_named(fresh)}），未予采用，"
                                 "已保留原稿及原有判定")
            ctx.emit(EventType.LOG, level="warn", code="report_rewrite_rejected", message=rewrite["reason"])
        else:
            known = {outcome["keys"][u]: v for u, v in outcome["verdicts"].items() if v["status"] in judging.JUDGED}
            # 第二轮用第一轮剩下的钱和时间：每份报告的上限管的是整份报告
            left = judging.Budget(
                max_claims=budget.max_claims,
                max_cost_usd=None if budget.max_cost_usd is None else max(0.0, budget.max_cost_usd - outcome["cost_usd"]),
                timeout_s=None if budget.timeout_s is None else max(0.0, budget.timeout_s - outcome["duration_ms"] / 1000),
                daily_max_usd=budget.daily_max_usd)
            second = await judge_round(rewritten, left, known, False)
            usages.append(second["usage"])
            outcome = _merge(outcome, second, budget)
            doc, response, blocking = rewritten, again, worse
            # 封存的是改写稿，编号按它重排：按原文认回去，改掉了的句子在改写稿里没有编号
            now = judging.unit_texts(rewritten)
            at: dict[str, str] = {}
            for uid, text in now.items():
                at.setdefault(text, uid)
            before = set(texts.values())
            rewrite.update(applied=True, units=[at.get(texts.get(uid, "")) for uid in bad],
                           changed=[uid for uid, text in now.items() if text not in before])
    summary = judging.summarize(outcome, mode="inline", on_unsupported=judge_cfg["on_unsupported"],
                                rewrite_once=judge_cfg["rewrite_once"], same_model=same_model, rewrite=rewrite)
    judging.apply_judgement(doc, outcome, summary)
    return doc, response, blocking, summary, usages

