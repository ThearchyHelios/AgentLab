"""问数据一轮落进 conversation_turns 的那一行：服务端写（状态、运行 id、答案、复核、meta），页面只读。

以前这一行是页面写的（frontend/src/store/chat.ts 的 persist / buildMeta），一轮的编排搬到服务端之后由这里写，字段和页面以前
写的一一对应，读的一方（问数据页的 restoreTurn、助手拼历史的 copilot._history_section、列表的 conversations.last_status）不用变：

- status：running | done | error；error 列是一句原话（运行的报错、助手的说法、「已取消」「服务重启，本轮已中断」），助手拼
  历史时照它写「这一轮失败了：…」；
- answer：答案正文（成果各字段拼起来，同页面的 answerText，上限 ANSWER_CAP）；复核改写了的存改写后的；
- review：复核原样（含 verdict=ok、usage、retry_instruction）；没复核过是 {}；
- meta（v=1）：runId、runClass、runStatus、issuance（瘦过的出具信息）、noQuery、outcome（cancelled | suspended）、rejected
  （驳回的人、哪一步、备注：run.cancelled 的原样）、cause（失败的原因，见下）、attempts（用户点重试之前的各次）、ms、queries、
  scope、asked、looks、evidence、usage（{copilot, review, earlier, run}）、at。

**失败只记原因（cause），不记说法。** 页面以前记的是翻成人话之后的 failure（标题、原因、怎么办：lib/explain 那一套），说法在
界面层；服务端只记发生了什么，页面恢复时照同一套 lib/explain 讲（frontend/src/store/chat.ts 的 failureFromCause）：
  {kind: refused, message, code}        开流之前就被拒（模型没配好、限定的数据源都不在）
  {kind: copilot, message, hint, detail} 生成过程交出 error（助手本轮未完成）
  {kind: blocked, issues, self_checked} 生成的图没过检查
  {kind: start, message, code, status}  发起运行被拒（绑定的工具不在、使用者用不了的节点）
  {kind: run, error, detail}            运行失败
  {kind: gone}                          运行记录不见了
  {kind: build_interrupted}             建图时服务重启，没有可恢复的断点
  {kind: crash, message}                这一轮的后台任务自己出错
老轮次的 meta.failure 照旧认（页面优先读它）。
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

#: 落库答案的上限（同页面的 ANSWER_CAP）：只是防跑飞，不是显示上限
ANSWER_CAP = 20_000
ANSWER_CLIPPED = "\n\n…（内容过长，已截断，完整内容请查看运行记录）"
#: 归档的旧答案存多少（同页面的 ATTEMPT_ANSWER_CAP）
ATTEMPT_ANSWER_CAP = 4000
#: 停止、服务重启挂起时 error 列里的那一句（同页面落库时写的）
CANCELLED = "已取消"
SUSPENDED = "服务重启，本轮已中断"
#: 建图时服务重启
BUILD_INTERRUPTED = "服务重启，本轮在生成工作流时中断"
GONE = "运行记录不见了"
#: 用量里记的几项（同页面的 USAGE_KEYS）
USAGE_KEYS = ("cost_usd", "unpriced_calls", "calls", "unfinished_calls", "input_tokens", "output_tokens")
#: 出具信息里无法回指的数字最多存几个（同页面的 slimIssuance）
ISSUANCE_UNMATCHED_CAP = 20


def answer_text(output: Mapping[str, Any] | None) -> str:
    """成果压成一句话（同页面的 answerText）：下划线开头的、空的不要；字符串原样，别的按 JSON；超过上限截断并说出来。"""
    if not output:
        return ""
    parts = [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
             for key, value in output.items() if not str(key).startswith("_") and value not in (None, "")]
    text = "\n".join(parts)
    return text[:ANSWER_CAP] + ANSWER_CLIPPED if len(text) > ANSWER_CAP else text


def slim_issuance(issuance: Any) -> dict[str, Any] | None:
    """出具信息存进 meta 前瘦一下身（同页面的 slimIssuance）：无法回指的数字只留前 20 个。"""
    if not isinstance(issuance, Mapping) or not issuance.get("tier"):
        return None
    out = dict(issuance)
    unmatched = issuance.get("unmatched_numbers")
    if isinstance(unmatched, list):
        out["unmatched_numbers"] = unmatched[:ISSUANCE_UNMATCHED_CAP]
    return out


def has_evidence(output: Mapping[str, Any] | None) -> bool:
    """成果带不带逐段证据（同页面 lib/evidence 的 hasEvidence）：_evidence 或它的 others 里有文档、有字段。"""
    ev = (output or {}).get("_evidence") if isinstance(output, Mapping) else None
    if not isinstance(ev, Mapping):
        return False
    refs = [ev, *[r for r in ev.get("others") or [] if isinstance(r, Mapping)]]
    return any(isinstance(r.get("doc_artifact"), str) and r.get("doc_artifact") and isinstance(r.get("fields"), list)
               and r.get("fields") for r in refs)


def _names(value: Any) -> list[str]:
    return [str(v) for v in value if v not in (None, "")] if isinstance(value, list) else []


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def look_event(op: Mapping[str, Any]) -> dict[str, Any] | None:
    """助手的 look 操作 → 存进 meta.looks 的那一份（同页面 run/decode 的 lookEventOf）：下一轮服务端据此写「上一轮查看过」。"""
    if op.get("op") != "look":
        return None
    listed = op.get("objects") if isinstance(op.get("objects"), list) else op.get("sources")
    if not isinstance(listed, list):
        return None
    objects = []
    for x in listed:
        if not isinstance(x, Mapping):
            continue
        kind = x.get("kind") if x.get("kind") in ("capability", "workflow_version", "caliber") else "table"
        total, explored = _num(x.get("total")), _num(x.get("explored"))
        obj: dict[str, Any] = {"kind": kind}
        if isinstance(x.get("source"), str) and x.get("source"):
            obj["source"] = x["source"]
        if isinstance(x.get("source_id"), str) and x.get("source_id"):
            obj["source_id"] = x["source_id"]
        obj.update(named=_names(x.get("named")), picked=_names(x.get("picked")), joined=_names(x.get("joined")),
                   missing=_names(x.get("missing")))
        if total is not None:
            obj["total"] = total
            if explored is not None and explored < total:
                obj["explored"] = explored
        if _num(x.get("catalog_tables")) is not None:
            obj["catalog_tables"] = x["catalog_tables"]
        if _names(x.get("added_to_scope")):
            obj["added_to_scope"] = _names(x.get("added_to_scope"))
        if _num(x.get("prefiltered")) is not None:
            obj["prefiltered"] = x["prefiltered"]
        objects.append(obj)
    out: dict[str, Any] = {"op": "look", "id": str(op.get("id") or ""), "by": "system" if op.get("by") == "system" else "model",
                           "need": str(op.get("need") or ""),
                           "status": op.get("status") if op.get("status") in ("index", "failed") else "ok"}
    if isinstance(op.get("reason"), str) and op.get("reason"):
        out["reason"] = op["reason"]
    out["elapsed_ms"] = _num(op.get("elapsed_ms")) or 0
    out["objects"] = objects
    return out


# --------------------------------------------------------------------------
# 用量：{copilot, review, earlier, run}，每份是 UsageTotal（同页面的 TurnUsage）
# --------------------------------------------------------------------------


def usage_total(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """一份用量（providers/usage 的汇总、运行的 usage）只留要记的几项。不是对象交回 None。"""
    return add_total({}, usage) if isinstance(usage, Mapping) else None


def add_total(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> dict[str, Any]:
    """两份用量相加（缺的当 0，同页面的 addUsage）：只记不为 0 的项，金额保留 6 位。"""
    out = {k: v for k, v in (a or {}).items() if k in USAGE_KEYS}
    for key in USAGE_KEYS:
        try:
            value = float((b or {}).get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            out[key] = (out.get(key) or 0) + value
    for key in USAGE_KEYS:
        if key in out and key != "cost_usd":
            out[key] = int(out[key])
    if "cost_usd" in out:
        out["cost_usd"] = round(float(out["cost_usd"]), 6)
    return out


def retire_run(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """当前这次运行换下来了（自动重来、用户点重试）：它的用量并进 earlier，这一轮的花费不丢这一笔（同页面的 retireRun）。"""
    out = dict(usage or {})
    run = out.pop("run", None)
    if run:
        out["earlier"] = add_total(out.get("earlier"), run)
    return out


# --------------------------------------------------------------------------
# 用户点重试：上一次的结局归档
# --------------------------------------------------------------------------

#: 归档那一次的一句话（同页面以前 attemptOf 的写法）。也是重试时交给助手的原因
ATTEMPT_TEXT = {
    "cancelled": "已手动停止",
    "suspended": "服务重启，运行中断",
    "rejected": "被驳回",
    "unusable": "结论不可用：{note}",
    "partial": "存在缺口：{note}",
    "done": "已给出答案",
}


def _rejected_line(rejected: Mapping[str, Any]) -> str:
    """被驳回的那一句（同页面 REJECTED_TEXT.sub）：谁在哪一步驳回、备注；页面记过 sub 的照用。"""
    if isinstance(rejected.get("sub"), str) and rejected["sub"]:
        return rejected["sub"]
    who = str(rejected.get("actor") or "").strip()
    label = str(rejected.get("label") or "").strip()
    where = f"「{label}」" if label else "这一步"
    note = str(rejected.get("note") or "").strip()
    if rejected.get("decision") == "expired_reject":
        return f"{where}超过时限，按设置驳回"
    return (f"{who} 驳回了{where}" if who else f"{where}被驳回") + (f"：{note}" if note else "")


def attempt_record(row: Any, meta: Mapping[str, Any], *, n: int, at_ms: int | None = None) -> dict[str, Any]:
    """这一轮现在的样子折成一条留档（同页面以前的 attemptOf）：{n, outcome, summary, detail?, runId, answer?, at, cause?}。

    用户主动重试不能把上一次抹掉：那一次的答案可能只是有缺口、并非全错，新的一次也未必更好，两份要能对照。"""
    from app.api.conversations import turn_run_id

    review = row.review if isinstance(row.review, Mapping) else {}
    answer = row.answer or ""
    if len(answer) > ATTEMPT_ANSWER_CAP:
        answer = answer[:ATTEMPT_ANSWER_CAP] + "…"
    base: dict[str, Any] = {"n": n, "at": at_ms if at_ms is not None else int(time.time() * 1000),
                            "runId": turn_run_id(row)}
    if answer:
        base["answer"] = answer
    signals = "\n".join(str(s.get("detail") or "") for s in review.get("signals") or [] if isinstance(s, Mapping)) or None
    outcome = meta.get("outcome")
    if outcome == "cancelled" and isinstance(meta.get("rejected"), Mapping):
        sub = _rejected_line(meta["rejected"])
        return {**base, "outcome": "rejected", "summary": ATTEMPT_TEXT["rejected"] + (f"：{sub}" if sub else "")}
    if outcome == "cancelled" or (row.status == "error" and (row.error or "").strip() == CANCELLED):
        return {**base, "outcome": "cancelled", "summary": ATTEMPT_TEXT["cancelled"]}
    if outcome == "suspended":
        return {**base, "outcome": "suspended", "summary": ATTEMPT_TEXT["suspended"]}
    if row.status == "error":
        failure = meta.get("failure") if isinstance(meta.get("failure"), Mapping) else None
        line = (f"{failure.get('title')}：{failure.get('reason')}" if failure and failure.get("reason")
                else str(failure.get("title")) if failure and failure.get("title") else (row.error or "发生错误"))
        out = {**base, "outcome": "failed", "summary": line}
        if failure and failure.get("detail"):
            out["detail"] = str(failure["detail"])
        if isinstance(meta.get("cause"), Mapping):
            out["cause"] = dict(meta["cause"])
        return out
    note = str(review.get("note") or "")
    if review.get("severity") == "broken":
        return {**base, "outcome": "unusable", "summary": ATTEMPT_TEXT["unusable"].format(note=note), "detail": signals}
    if review.get("severity") == "degraded":
        return {**base, "outcome": "partial", "summary": ATTEMPT_TEXT["partial"].format(note=note), "detail": signals}
    return {**base, "outcome": "done", "summary": ATTEMPT_TEXT["done"]}


def attempt_reason(record: Mapping[str, Any]) -> str:
    """重试时交给助手的原因（同页面以前的写法）：那一次的一句话，有细节的括号里带前 300 字。"""
    summary = str(record.get("summary") or "")
    detail = record.get("detail")
    return f"{summary}（{str(detail)[:300]}）" if detail else summary


def with_max_steps(graph: Mapping[str, Any], steps: int) -> dict[str, Any]:
    """图里每个 agent 节点的步数上限调到 steps（「放宽步数重跑」，同页面的 withMaxSteps）。"""
    nodes = []
    for node in graph.get("nodes") or []:
        if isinstance(node, Mapping) and node.get("type") == "agent":
            data = dict(node.get("data") or {})
            data["config"] = {**dict(data.get("config") or {}), "max_steps": steps}
            node = {**node, "data": data}
        nodes.append(node)
    return {**dict(graph), "nodes": nodes}


__all__ = ["ANSWER_CAP", "ATTEMPT_ANSWER_CAP", "ATTEMPT_TEXT", "BUILD_INTERRUPTED", "CANCELLED", "GONE", "SUSPENDED",
           "USAGE_KEYS", "add_total", "answer_text", "attempt_reason", "attempt_record", "has_evidence", "look_event",
           "retire_run", "slim_issuance", "usage_total", "with_max_steps"]
