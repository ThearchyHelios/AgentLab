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
    MARKER_RULES,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    describe_violations,
)
from app.engine.nodes.llm import _invoke_streaming, _model_spec, _report_call
from app.engine.schema import GraphNode, GraphSpec, NodeType, _ancestors
from app.engine.state import GraphState, message_text, thinking_text
from app.engine.toolcalls import leaked_markup, markup_warning
from app.providers.factory import ProviderNotConfigured, get_chat_model

ON_VIOLATION = ("fail", "flag")
NUMBERS = ("strict", "off")
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
        # evidence_from 只管查询、检索这类证据从哪几个节点来；指标从哪来由 metrics_from 管
        keep = {str(s) for s in sources}
        ledger = [e for e in ledger if e.get("kind") not in ("query", "retrieval") or e.get("node_id") in keep]
    return build_catalog(
        nodes=state.get("nodes") or {},
        ledger=ledger,
        inputs=state.get("input") or {},
        metrics_from=_setting(spec, node, "metrics_from") or None,
        allowed=_ancestors(spec, node.id),
    )


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


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class _RenderedStream:
    """借 _invoke_streaming 的流式与回退逻辑，只在出口把 llm.token 换成渲染后的字。

    模型写的是 [[m:gmv]]，前端要看到的是「45,678.5元」。标记会被切在两个 chunk
    中间，StreamRenderer 攒到没有未闭合的 [[ 再放；调用结束时 flush 掉剩下的。
    """

    def __init__(self, ctx: NodeContext, catalog: dict[str, Any]) -> None:
        self._ctx = ctx
        self._catalog = catalog
        self._renderer = StreamRenderer(catalog)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    def emit(self, event_type: EventType | str, **data: Any) -> None:
        if event_type == EventType.LLM_TOKEN:
            if text := self._renderer.feed(str(data.get("delta") or "")):
                self._ctx.emit(EventType.LLM_TOKEN, delta=text)
            return
        if event_type == EventType.LOG and data.get("code") == "stream_fallback":
            # 流断在半路、退回非流式重来：攒着的半截不再放出去，免得和重来的那份接在一起
            self._renderer = StreamRenderer(self._catalog)
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
    raw = ctx.cfg("max_repairs", 1)
    try:
        repairs = int(raw)
    except (TypeError, ValueError):
        raise NodeError(ctx.node.id, f"max_repairs 要写 0 到 {MAX_REPAIRS} 的整数，写的是 {raw!r}") from None
    if isinstance(raw, bool) or not 0 <= repairs <= MAX_REPAIRS:
        raise NodeError(ctx.node.id, f"max_repairs 要写 0 到 {MAX_REPAIRS} 的整数，写的是 {raw!r}")
    return numbers, repairs


def _blocking(violations: list[dict[str, Any]], numbers: str) -> list[dict[str, Any]]:
    """要让写作者改的违规。numbers=off 时裸数字只记录不拦（文档和出口复核照样看得到）。"""
    return [v for v in violations if not (numbers == "off" and v.get("code") == "uncited_number")]


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


def _messages(ctx: NodeContext, state: GraphState, catalog: dict[str, Any]) -> list[BaseMessage]:
    system = ctx.render_str(ctx.cfg("system", ""), state)
    instructions = ctx.render_str(ctx.cfg("instructions", ""), state).strip() \
        or "根据下面的证据写一份简洁的报告，先总后分。"
    return [
        SystemMessage(content="\n\n".join(p for p in (system, ROLE) if p)),
        HumanMessage(content=(
            f"{instructions}\n\n{catalog_prompt(catalog)}\n\n{MARKER_RULES}\n\n"
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
    if not any(entry.get("kind") == "metric" for entry in catalog.values()):
        ctx.emit(EventType.LOG, level="warn", code="report_no_evidence",
                 message="报告撰写节点的上游没有可引用的口径卡指标：报告里的数字都会被判为没有出处。"
                         "在它前面接一张口径卡，或者检查 metrics_from")

    messages = _messages(ctx, state, catalog)
    started = time.perf_counter()
    spent: list[dict[str, Any]] = []
    nudged = False

    async def draft(payload: list[BaseMessage]) -> tuple[BaseMessage, str]:
        nonlocal nudged
        stream = _RenderedStream(ctx, catalog)
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

    def compose(raw: str) -> dict[str, Any]:
        return compose_doc(raw, catalog, node_id=ctx.node.id, run_id=ctx.run.run_id, allow_numbers=allow)

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
             on_violation=on_violation, failed=bool(blocking) and on_violation == "fail")
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
    }
    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}, "usage": usage}
    if ctx.cfg("emit_message", True):
        updates["messages"] = [AIMessage(content=text or "(空)")]
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: text}
    return updates
