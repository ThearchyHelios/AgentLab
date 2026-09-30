from __future__ import annotations

import json
import re
import time
from typing import Any

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.approval import read_decision
from app.engine.context import NodeContext, NodeError
from app.engine.replay import ask, once
from app.engine.state import GraphState, merge_usage, message_text, template_context
from app.providers import catalog
from app.providers.factory import ModelSpec, get_chat_model


async def run_human(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """人工介入节点。

    执行到这里会挂起整张图并落盘 checkpoint；进程重启也不影响，
    人在前端回复之后从断点继续，而不是从头重跑。
    """
    mode = ctx.cfg("mode", "approve")  # approve | input | edit
    payload = {
        "kind": "human_node",
        "node_id": ctx.node.id,
        "mode": mode,
        "title": ctx.render_str(ctx.cfg("title", "待审批"), state),
        "message": ctx.render_str(ctx.cfg("message", ""), state),
        "schema": ctx.cfg("schema") or {},
        "context": ctx.render(ctx.cfg("context", {}) or {}, state),
    }
    if mode == "edit":
        payload["draft"] = ctx.render(ctx.cfg("draft", ""), state)

    response = await ask(ctx, payload, **payload)
    # 谁批的跟着事件走：时间线要写得出"张工驳回了"，没署名就是 None，不再默认写"你"
    await once(ctx, EventType.HUMAN_RESOLVED, response=response, actor=ctx.actor())
    decision = read_decision(response)

    if mode == "approve":
        approved = decision.approved
        result: dict[str, Any] = {
            "approved": approved,
            "note": decision.note,
            "__decision__": "approved" if approved else "rejected",
        }
        if not approved and ctx.cfg("stop_on_reject", False):
            raise NodeError(ctx.node.id, f"人工驳回：{result['note'] or '未说明原因'}")
    else:  # edit / input
        # 驳回在这两种模式下以前是个摆设：__decision__ 硬编码成 approved，
        # approved 字段看都不看。界面上按钮在、toast 说"已驳回"，运行却照常
        # 往下走——比没有这个按钮更糟。
        #
        # 这两种模式在图上只有一个 out 出口（没有 rejected 那条边），所以驳回
        # 的正确语义是终止运行，而不是当作通过继续。缺省仍是通过：只传 value
        # 不带 approved 的老调用方行为不变。
        #
        # 只有结构体里明说了"不"才算驳回：这两种模式下裸字符串就是内容本身，
        # 用户在输入框里回一句"no"是在交稿，不是在驳回。
        if isinstance(response, dict) and decision.rejected:
            raise NodeError(ctx.node.id, f"人工驳回：{decision.note or '未说明原因'}")
        note = decision.note if isinstance(response, dict) else ""   # 裸字符串是内容，不是备注
        result = {"value": decision.value, "note": note, "__decision__": "approved"}

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result.get("value", result.get("approved"))}
    return updates


# --------------------------------------------------------------------------
# 结构化校验
# --------------------------------------------------------------------------


async def run_validate(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """按 JSON Schema 校验上游产出。

    校验失败时可以把错误原样喂回模型让它改 —— 这比在 prompt 里祈祷它输出正确格式可靠得多。
    但修复只许改格式：修出来的值要能在原文里找到，找不到的就是模型编的（见 _invented）。
    """
    import jsonschema

    schema = ctx.cfg("schema")
    if not schema:
        raise NodeError(ctx.node.id, "「结构校验」节点需要填写 JSON Schema")

    source_cfg = ctx.cfg("source", "{{ last_message }}")
    original = ctx.render(source_cfg, state) if not isinstance(source_cfg, str) else ctx.render_str(source_cfg, state)
    max_retries = int(ctx.cfg("max_retries", 2) or 0)
    repair = bool(ctx.cfg("repair_with_llm", True))

    attempt = 0
    errors: list[str] = []
    raw: Any = original
    value: Any = raw
    usage: dict[str, Any] = {}

    while True:
        value, parse_error = _coerce_json(raw)
        invented = [] if parse_error or raw is original else _invented(value, original, schema)
        if parse_error:
            errors = [parse_error]
        elif invented:
            # 修复编出了原文没有的值（真实运行里是凭空的 total_count: 0）。这次修复作废，
            # 下一次仍从原文修，而不是在编出来的那份上接着改
            errors = [f"修复时出现了原文没有的值：{'、'.join(invented[:5])}"]
            ctx.emit(EventType.LOG, level="warn", code="repair_invented",
                     message=f"第 {attempt} 次修复作废：{errors[0]}")
        else:
            try:
                jsonschema.validate(value, schema)
                ctx.emit(EventType.LOG, level="info", message=f"校验通过（第 {attempt + 1} 次尝试）")
                result = {"valid": True, "data": value, "attempts": attempt + 1, "errors": []}
                updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
                if usage:
                    updates["usage"] = usage
                var_name = ctx.cfg("assign_to", "")
                if var_name:
                    updates["vars"] = {var_name: value}
                return updates
            except jsonschema.ValidationError as e:
                path = "/".join(str(p) for p in e.absolute_path) or "（根）"
                errors = [f"{path}: {e.message}"]

        if not invented:
            ctx.emit(EventType.LOG, level="warn", message=f"第 {attempt + 1} 次校验失败：{errors[0]}",
                     code="validate_retry")
        attempt += 1
        if attempt > max_retries or not repair:
            break
        raw, spent = await _repair(original, schema, errors, ctx)
        usage = merge_usage(usage, spent)

    result = {"valid": False, "data": value, "attempts": attempt, "errors": errors}
    if ctx.cfg("fail_fast", True):
        raise NodeError(ctx.node.id, f"结构化校验未通过：{'；'.join(errors)}{_upstream_cut_short(state, ctx)}")
    return {"nodes": {ctx.node.id: result}, **({"usage": usage} if usage else {})}


def _upstream_cut_short(state: GraphState, ctx: NodeContext) -> str:
    """上游有 agent 没等到自己给结论就收了尾（步数、预算、上下文……），缺字段多半出在那里。

    以前报错只有一句「regions: None is not of type 'array'」，看不出根源是上游
    agent 用满了步数、查到一半就被收了尾（run 719253eb / 6310f291）。
    """
    from app.engine.guards import LIMITED_LABEL

    nodes = ctx.run.spec.node_map()
    cut = [
        f"「{nodes[nid].title if nid in nodes else nid}」因{LIMITED_LABEL.get(out['limited'], '达到上限')}"
        for nid, out in (state.get("nodes") or {}).items()
        if isinstance(out, dict) and out.get("limited")
    ]
    if not cut:
        return ""
    return (f"。上游{'、'.join(cut)}提前结束，缺失的字段很可能源于此。"
            "请查看其结束提示，调大相应上限或缩小查询范围")


def _coerce_json(raw: Any) -> tuple[Any, str | None]:
    """尽量把模型输出解析成 JSON：剥掉 ``` 围栏，再退一步找第一个完整对象。"""
    if isinstance(raw, (dict, list)):
        return raw, None
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1 : -1 if lines[-1].strip().startswith("```") else None]).strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1
    )
    if start >= 0:
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end]), None
            except json.JSONDecodeError:
                continue
    return text, "输出不是合法 JSON"


async def _repair(raw: Any, schema: dict[str, Any], errors: list[str],
                  ctx: NodeContext) -> tuple[str, dict[str, Any]]:
    """让模型只改格式地重写一遍。返回 (重写的文本, 这次调用的用量)。"""
    async with SessionLocal() as session:
        model, model_id = await get_chat_model(
            session,
            ModelSpec(provider=ctx.cfg("provider"), model=ctx.cfg("model"), max_tokens=4096),
        )
    prompt = (
        "下面这段输出没有通过 JSON Schema 校验，请只调整格式后重新输出。\n\n"
        "规矩：你只能搬运原始输出里已经有的值，不许补数据。原始输出里没有的值一律填 null，"
        "即使 Schema 要求它不能为空——不许用 0、空字符串或任何猜测的值凑数，不许编造。"
        "值要从原文里原样抄过来。\n\n"
        f"Schema：\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"错误：\n- " + "\n- ".join(errors) + "\n\n"
        f"原始输出：\n{_as_text(raw)[:6000]}\n\n"
        "只输出修正后的 JSON，不要任何解释或代码围栏。"
    )
    started = time.perf_counter()
    response = await model.ainvoke(prompt)
    # 修复是一次真实的模型调用，以前不发 llm.end、不进 usage，钱花了账上看不见
    meta = getattr(response, "usage_metadata", None) or {}
    inp, out = int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)
    spent = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
             "cost_usd": round(catalog.estimate_cost(model_id, inp, out), 6), "calls": 1}
    ctx.emit(EventType.LLM_END, model=model_id, purpose="repair",
             duration_ms=int((time.perf_counter() - started) * 1000), **spent)
    return message_text(response), spent


def _as_text(raw: Any) -> str:
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)


# 原文里的数字：可带千分位、小数、正负号
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_PERCENT = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*[%％]")
# 字符串值里要能在原文找到的片段：数字、拉丁词、连续的汉字
_PIECE = re.compile(r"\d+(?:\.\d+)?|[A-Za-z]+|[\u4e00-\u9fff]+")

# 数字的证据不从代码里取。「假设调用工具 + 一段 SQL」正是最常见的造假形状，而 SQL 里
# 到处是数字：WHERE status > 0、deleted = 0、LIMIT 10。认它们的话，编出来的
# total_count: 0 就有了「出处」。要去掉的是：代码围栏（sql / python / shell 之类）、
# "sql": "…" 这类字符串值、以及正文里看起来像 SQL 的那一段（SELECT … FROM … 到句末，
# 后面接着的 WHERE / LIMIT 之类的行一并算进去）
_CODE_FENCE = re.compile(
    r"```[ \t]*(?:sql|mysql|postgres(?:ql)?|sqlite|plsql|python|py|bash|sh|shell|zsh"
    r"|javascript|js|typescript|ts)\b.*?(?:```|$)", re.I | re.S)
_CODE_VALUE = re.compile(r'"(?:sql|query|statement|code|script|command)"\s*:\s*"(?:[^"\\]|\\.)*"',
                         re.I)
_SQL_RUN = re.compile(
    r"\b(?:select\b|with\s+(?:recursive\s+)?\w+\s*(?:\([^)]*\))?\s+as\s*\()"
    r"(?:(?!\.\s)[^；;。]){0,400}?\bfrom\b[^\n；;。，]*"
    r"(?:\n[ \t]*(?:where|and|or|group|order|having|limit|offset|union|on|join|left|right|inner"
    r"|outer|full|cross)\b[^\n；;。，]*)*", re.I)


def _without_code(text: str) -> str:
    for pattern in (_CODE_FENCE, _CODE_VALUE, _SQL_RUN):
        text = pattern.sub(" ", text)
    return text


def _numbers_in(text: str) -> set[float]:
    """原文里（代码之外）所有数字的值。中文写法（十二条、3.5万）按出具回指那套规则一并认；
    带百分号的另认它的小数写法（12.5% → 0.125 是格式换算，不是编造）。"""
    from app.engine.issuance import extract_numbers

    text = _without_code(text)
    out: set[float] = {token.value for token in extract_numbers(text)}
    for token in _NUMBER.findall(text):
        try:
            out.add(float(token.replace(",", "")))
        except ValueError:
            continue
    for token in _PERCENT.findall(text):
        try:
            out.add(float(token.replace(",", "")) / 100)
        except ValueError:
            continue
    return out


def _literals(schema: Any) -> set[str]:
    """Schema 自己规定的取值（enum / const）：把「成功」映射成 "ok" 是格式，不是编造。"""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key in ("enum", "const"):
            value = schema.get(key)
            for item in value if isinstance(value, list) else [value]:
                if item is not None:
                    found.add(str(item))
        for value in schema.values():
            found |= _literals(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= _literals(value)
    return found


def _invented(value: Any, original: Any, schema: dict[str, Any]) -> list[str]:
    """修复结果里原文没有的值，写成 "路径=值"。

    数字按数值比（原文写 1,284，修出来 1284 算有）；非空字符串要么原样出现在原文里，
    要么它的每个数字、拉丁词、汉字片段都能在原文里找到（2026年9月1日 → 2026-09-01
    算搬运）。布尔、null 不查：null 正是「没有这个数据」的诚实写法。
    """
    text = _as_text(original)
    lowered = text.lower()
    numbers = _numbers_in(text)
    allowed = _literals(schema)
    found: list[str] = []

    def known_number(n: float) -> bool:
        return any(abs(n - m) <= 1e-9 for m in numbers)

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                walk(item, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            if not known_number(float(node)):
                found.append(f"{path or '（根）'}={node}")
        elif isinstance(node, str):
            s = node.strip()
            if not s:
                if '""' not in text:
                    found.append(f'{path or "（根）"}=""')
                return
            if s in allowed:
                return
            if _NUMBER.fullmatch(s):
                # 写成字符串的数字照数字查："0" 不能因为 SQL 里有个 > 0 就算有出处
                if not known_number(float(s.replace(",", ""))):
                    found.append(f"{path or '（根）'}={s[:40]}")
                return
            if s.lower() in lowered:
                return
            pieces = _PIECE.findall(s)
            ok = bool(pieces) and all(
                known_number(float(p)) if p[0].isdigit() else p.lower() in lowered for p in pieces
            )
            if not ok:
                found.append(f"{path or '（根）'}={s[:40]}")

    walk(value, "")
    return found
